from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from relay_core.api import create_app
from relay_core.engine import apply, now, seed
from relay_core.store import Store


@pytest.fixture
def service(tmp_path):
    store = Store(str(tmp_path / "relay.sqlite3"))
    state = store.create(seed())

    def send(action, **kw):
        body = {"action": action, **kw}
        return store.transact(state["id"], str(uuid4()), body, lambda s: apply(s, body))

    return store, state["id"], send


def confirm(send, p):
    for participant in p["required"]:
        assert send("accept", resource_id=participant, revision=p["revision"])["ok"]
    return send("approve", revision=p["revision"])


def test_recovery_to_receipts(service):
    store, wid, send = service
    assert send("change", resource_id="harbour", capacity=240)["ok"]
    p = send("propose")["state"]["plan"]
    assert [a["kg"] for a in p["drivers"]] == [192, 128]
    assert [a["kg"] for a in p["recipients"]] == [240, 80]
    assert sum(x["kg"] for x in p["legs"]) == 320
    assert confirm(send, p)["ok"]
    assert store.read(wid)["status"] == "committed"
    for d in p["drivers"]:
        assert send("pickup", revision=p["revision"], resource_id=d["resource_id"])["ok"]
    assert store.read(wid)["status"] == "awaiting_receipt"
    for r in p["recipients"]:
        assert send(
            "receive",
            revision=p["revision"],
            resource_id=r["resource_id"],
            received_kg=r["kg"],
        )["ok"]
    assert store.read(wid)["status"] == "complete"


def test_capacity_change_invalidates_accepted_plan(service):
    _, _, send = service
    p = send("propose")["state"]["plan"]
    send("accept", resource_id="tunde", revision=p["revision"])
    send("change", resource_id="harbour", capacity=240)
    p2 = send("propose")["state"]["plan"]
    assert p2["revision"] > p["revision"]
    rejected = send("accept", resource_id="tunde", revision=p["revision"])
    assert rejected["code"] == "STALE_REVISION"
    assert rejected["state"]["plan"]["accepted"] == []


def test_conditional_offer_cannot_dispatch_without_other_acceptances(service):
    _, _, send = service
    p = send("propose")["state"]["plan"]
    send("accept", resource_id="tunde", revision=p["revision"])
    assert send("approve", revision=p["revision"])["code"] == "MISSING_CONFIRMATIONS"


def test_impossible_rescue_escalates(service):
    _, _, send = service
    send("change", resource_id="amara", available=False)
    result = send("propose")
    assert result["code"] == "INSUFFICIENT_CAPACITY"
    assert result["state"]["plan"] is None
    assert result["state"]["status"] == "needs_attention"


def test_restart_preserves_confirmations(service):
    store, wid, send = service
    p = send("propose")["state"]["plan"]
    send("accept", resource_id="tunde", revision=p["revision"])
    reopened = Store(store.path)
    assert reopened.read(wid)["plan"]["accepted"] == ["tunde"]


def test_duplicate_concurrent_commands_apply_once(service):
    store, wid, _ = service
    body = {"action": "change", "resource_id": "harbour", "capacity": 240}

    def submit(_):
        return store.transact(wid, "same-request", body, lambda s: apply(s, body))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(20)))
    assert all(r["ok"] for r in results)
    assert store.read(wid)["facts_version"] == 2
    assert len(store.read(wid)["events"]) == 2
    altered = {**body, "capacity": 100}
    assert (
        store.transact(wid, "same-request", altered, lambda s: apply(s, altered))["code"]
        == "IDEMPOTENCY_CONFLICT"
    )


def test_concurrent_approval_and_change_cannot_commit_stale_capacity(service):
    store, wid, send = service
    p = send("propose")["state"]["plan"]
    for r in p["required"]:
        send("accept", resource_id=r, revision=p["revision"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(send, "approve", revision=p["revision"])
        b = pool.submit(send, "change", resource_id="harbour", capacity=20)
        outcomes = [a.result(), b.result()]
    assert sum(result["ok"] for result in outcomes) == 1
    s = store.read(wid)
    if s["status"] == "committed":
        assert next(r for r in s["resources"] if r["id"] == "harbour")["capacity"] == 320
    else:
        assert s["plan"] is None


def test_short_receipt_is_not_success(service):
    store, wid, send = service
    p = send("propose")["state"]["plan"]
    confirm(send, p)
    for d in p["drivers"]:
        send("pickup", resource_id=d["resource_id"], revision=p["revision"])
    assert send("receive", resource_id="harbour", revision=p["revision"], received_kg=300)["ok"]
    assert store.read(wid)["status"] == "discrepancy"


def test_receipt_without_pickup_is_rejected(service):
    _, _, send = service
    p = send("propose")["state"]["plan"]
    confirm(send, p)
    assert (
        send("receive", resource_id="harbour", revision=p["revision"], received_kg=320)["code"]
        == "PICKUP_UNCONFIRMED"
    )


def test_expired_approval_and_closed_window():
    s = seed()
    apply(s, {"action": "propose"})
    s["plan"]["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    assert (
        apply(s, {"action": "accept", "resource_id": "tunde", "revision": 1})["code"]
        == "EXPIRED_PROPOSAL"
    )
    s["deadline"] = (now() - timedelta(seconds=1)).isoformat()
    assert apply(s, {"action": "propose"})["code"] == "WINDOW_CLOSED"


def test_invalid_capacity_cannot_mutate_facts(service):
    store, wid, send = service
    for capacity in (-1, True, 3.2, 10001):
        assert not send("change", resource_id="harbour", capacity=capacity)["ok"]
    assert store.read(wid)["facts_version"] == 1


def test_different_workspaces_are_isolated(tmp_path):
    client = TestClient(create_app(str(tmp_path / "api.sqlite3"), rehearsal=True))
    a = client.post("/workspaces").json()
    b = client.post("/workspaces").json()
    response = client.post(
        f"/workspaces/{a['id']}/commands",
        json={"action": "change", "resource_id": "harbour", "capacity": 240},
    )
    assert response.status_code == 200
    assert client.get(f"/workspaces/{b['id']}").json()["facts_version"] == 1
    assert (
        client.post(
            f"/workspaces/{a['id']}/commands",
            json={"action": "change", "capacity": True},
        ).status_code
        == 422
    )
    assert (
        client.post("/workspaces", headers={"Origin": "https://unrelated.example"}).status_code
        == 403
    )


def test_missing_model_never_fakes_inference(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_MODEL_ID", raising=False)
    client = TestClient(create_app(str(tmp_path / "api.sqlite3"), rehearsal=True))
    s = client.post("/workspaces").json()
    r = client.post(
        f"/workspaces/{s['id']}/messages",
        json={"resource_id": "tunde", "text": "I can take half"},
    )
    assert r.status_code == 503
    assert client.get(f"/workspaces/{s['id']}").json()["suggestions"] == []


def test_expired_proposal_can_be_replaced_without_reusing_approvals():
    s = seed()
    apply(s, {"action": "propose"})
    apply(s, {"action": "accept", "resource_id": "tunde", "revision": 1})
    s["plan"]["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    assert apply(s, {"action": "propose"})["ok"]
    assert s["plan"]["revision"] == 2
    assert s["plan"]["accepted"] == []
    assert s["plans"][0]["status"] == "expired"
