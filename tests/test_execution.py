"""Concurrency, crash recovery, custody and atomic outbox invariants."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from relay_core import execution
from relay_core.api import create_app
from relay_core.engine import apply, seed
from relay_core.store import Store


@pytest.fixture
def network(tmp_path):
    state = seed()
    clock = [datetime.fromisoformat(state["logistics"]["starts_at"]).timestamp()]
    resources = deepcopy(state["resources"])
    resources[1]["capacity"] = 320
    resources[1]["conditional"] = False
    resources[2]["available"] = False
    resources[3]["capacity"] = 640
    store = Store(str(tmp_path / "execution.sqlite3"), clock=lambda: clock[0])
    nid = store.create_network(resources)
    a, b = (store.create(seed(), nid) for _ in range(2))
    return store, nid, a["id"], b["id"], clock


def send(store, wid, action, key=None, **kw):
    payload = {"action": action, **kw}
    return store.transact(wid, key or str(uuid4()), payload, lambda s: apply(s, payload))


def ready(store, wid):
    result = send(store, wid, "propose")
    assert result["ok"], result
    p = result["state"]["plan"]
    for rid in p["required"]:
        assert send(store, wid, "accept", resource_id=rid, revision=p["revision"])["ok"]
    return p["revision"]


def cancel(store, wid, revision):
    assert send(store, wid, "cancel", revision=revision, reason="Donor postponed collection")["ok"]
    for rid in store.read(wid)["plan"]["required"]:
        assert send(store, wid, "ack_cancel", revision=revision, resource_id=rid)["ok"]


def test_competing_rescues_one_winner_then_cancel_restart_and_recover(network):
    store, nid, a, b, clock = network
    ra, rb = ready(store, a), ready(store, b)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: send(store, args[0], "approve", revision=args[1]), [(a, ra), (b, rb)]
            )
        )
    assert sum(r["ok"] for r in results) == 1
    winner, loser = (a, b) if results[0]["ok"] else (b, a)
    assert next(r for r in results if not r["ok"])["code"] == "RESERVATION_CONFLICT"
    assert store.read(loser)["status"] == "awaiting_confirmations"
    assert (
        len([r for r in store.reservations(nid) if r["role"] == "driver" and not r["released"]])
        == 1
    )
    assert send(store, winner, "cancel", revision=1, reason="Donor rescheduling")["ok"]
    assert send(store, loser, "approve", revision=1)["code"] == "RESERVATION_CONFLICT"
    reopened = Store(store.path, clock=lambda: clock[0])
    assert reopened.read(winner)["status"] == "cancelling"
    cancel(reopened, winner, 1)
    assert reopened.read(winner)["status"] == "cancelled"
    assert send(reopened, loser, "approve", revision=1)["ok"]
    assert send(reopened, winner, "recover")["ok"]
    assert reopened.read(winner)["plans"][0]["status"] == "cancelled"
    assert send(reopened, winner, "propose")["code"] == "INSUFFICIENT_CAPACITY"
    assert reopened.read(winner)["plan"] is None


def test_collected_load_requires_return_evidence_before_capacity_release(network):
    store, _nid, a, b, _ = network
    ready(store, a)
    ready(store, b)
    send(store, a, "approve", revision=1)
    send(store, a, "pickup", revision=1, resource_id="tunde")
    cancel(store, a, 1)
    assert store.read(a)["status"] == "cancelling"
    assert send(store, b, "approve", revision=1)["code"] == "RESERVATION_CONFLICT"
    assert not send(store, a, "ack_return", revision=1, resource_id="tunde", evidence="")["ok"]
    assert send(store, a, "pickup", revision=1, resource_id="tunde")["code"] == "NOT_COMMITTED"
    assert send(
        store,
        a,
        "ack_return",
        revision=1,
        resource_id="tunde",
        evidence="Synthetic donor receipt for all 320 kg",
    )["ok"]
    assert store.read(a)["status"] == "cancelled"
    assert send(store, b, "approve", revision=1)["ok"]


def test_complete_releases_but_discrepancy_does_not(network):
    store, nid, a, _, _ = network
    ready(store, a)
    send(store, a, "approve", revision=1)
    send(store, a, "pickup", revision=1, resource_id="tunde")
    send(store, a, "receive", revision=1, resource_id="harbour", received_kg=300)
    assert all(not r["released"] for r in store.reservations(nid))
    assert (
        send(store, a, "cancel", revision=1, reason="Erase discrepancy")["code"]
        == "RECONCILIATION_REQUIRED"
    )


def test_same_command_replay_does_not_duplicate_reservations_or_jobs(network):
    store, nid, a, _, _ = network
    ready(store, a)
    first = send(store, a, "approve", revision=1, key="approval")
    assert first["ok"]
    assert send(store, a, "approve", revision=1, key="approval") == first
    assert len(store.reservations(nid)) == 2
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM jobs WHERE workspace=? AND kind='receipts'", (a,)
            ).fetchone()[0]
            == 1
        )


def test_dispatch_failure_rolls_back_state_reservations_and_outbox(network, monkeypatch):
    store, nid, a, _, _ = network
    ready(store, a)

    def fail(*args):
        raise RuntimeError("Injected crash before commit")

    monkeypatch.setattr(execution, "synchronize", fail)
    with pytest.raises(RuntimeError):
        send(store, a, "approve", revision=1, key="crashed")
    assert store.read(a)["status"] == "awaiting_confirmations"
    assert store.reservations(nid) == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM commands WHERE id='crashed'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE kind='receipts'").fetchone()[0] == 0


def test_claim_crash_reclaimed_after_restart_and_old_worker_is_fenced(network):
    store, _, a, _, clock = network
    send(store, a, "propose")
    clock[0] += 61
    abandoned = store.claim_job()
    assert abandoned
    assert store.claim_job() is None
    clock[0] += 61
    reopened = Store(store.path, clock=lambda: clock[0])
    replacement = reopened.claim_job()
    assert replacement["id"] == abandoned["id"]
    assert not reopened.deliver_job(abandoned)
    assert reopened.deliver_job(replacement)
    messages = reopened.inbox(a)
    assert len(messages) == 2
    assert not reopened.deliver_job(replacement)
    assert reopened.inbox(a) == messages


def test_concurrent_workers_cannot_duplicate_logical_reminder(network):
    store, _, a, _, clock = network
    send(store, a, "propose")
    clock[0] += 61
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.run_due(), range(8)))
    messages = store.inbox(a)
    assert len(messages) == 2
    assert len({m["id"] for m in messages}) == 2


def test_followup_is_bounded_and_escalation_has_owner(network):
    store, _, a, _, clock = network
    ready(store, a)
    send(store, a, "approve", revision=1)
    with store.connect() as db:
        clock[0] = (
            db.execute(
                "SELECT due FROM jobs WHERE workspace=? AND kind='receipts'", (a,)
            ).fetchone()[0]
            + 1
        )
    store.run_due()
    clock[0] += 301
    store.run_due()
    clock[0] += 301
    store.run_due()
    for kind in ("pickups", "receipts"):
        assert len([item for item in store.inbox(a) if f":{kind}:" in item["id"]]) == 3
    assert store.inbox(a)[-1]["recipient"] == "coordinator"
    assert store.read(a)["escalations"][0]["owner"] == "coordinator"
    clock[0] += 10000
    assert store.run_due() == 0


def test_superseded_followup_does_not_contact_old_participants(network):
    store, _, a, _, clock = network
    send(store, a, "propose")
    send(store, a, "change", resource_id="harbour", capacity=400)
    clock[0] += 61
    assert store.run_due() == 1
    assert store.inbox(a) == []


def test_global_receiving_capacity_applies_even_with_distinct_drivers(tmp_path):
    state = seed()
    state["resources"][1]["capacity"] = 320
    state["resources"][2]["capacity"] = 320
    store = Store(str(tmp_path / "recipients.sqlite3"))
    nid = store.create_network(state["resources"])
    a, b = (store.create(seed(), nid)["id"] for _ in range(2))
    send(store, a, "change", resource_id="amara", available=False)
    send(store, b, "change", resource_id="tunde", available=False)
    ready(store, a)
    ready(store, b)
    assert send(store, a, "approve", revision=1)["ok"]
    assert send(store, b, "approve", revision=1)["code"] == "RESERVATION_CONFLICT"
    assert len(store.reservations(nid)) == 2


def test_processing_failure_retries_then_escalates(network, monkeypatch):
    store, _, a, _, clock = network
    send(store, a, "propose")

    def fail(*args):
        raise RuntimeError("Injected delivery failure")

    monkeypatch.setattr(execution, "deliver", fail)
    for _ in range(3):
        clock[0] += 301
        store.run_due()
    assert store.read(a)["escalations"][0]["reason"] == "worker_failure"
    assert len(store.inbox(a)) == 1
    clock[0] += 1000
    assert store.run_due() == 0


def test_expired_proposal_escalates_instead_of_requesting_stale_acceptances(network):
    store, _, a, _, clock = network
    send(store, a, "propose")
    clock[0] += 601
    store.run_due()
    assert [m["recipient"] for m in store.inbox(a)] == ["coordinator"]


def test_successful_receipt_releases_capacity_and_obsoletes_followup(network):
    store, nid, a, b, clock = network
    ready(store, a)
    ready(store, b)
    send(store, a, "approve", revision=1)
    send(store, a, "pickup", revision=1, resource_id="tunde")
    send(store, a, "receive", revision=1, resource_id="harbour", received_kg=320)
    assert all(r["released"] for r in store.reservations(nid))
    assert send(store, b, "approve", revision=1)["ok"]
    clock[0] += 10000
    store.run_due()
    assert store.inbox(a) == []


def test_failed_delivery_rolls_back_inbox_before_retry(network, monkeypatch):
    store, _, a, _, clock = network
    send(store, a, "propose")
    clock[0] += 61
    original = execution.deliver

    def fail_after_write(*args):
        original(*args)
        raise RuntimeError("Injected crash after local delivery, before commit")

    monkeypatch.setattr(execution, "deliver", fail_after_write)
    store.run_due()
    assert store.inbox(a) == []
    monkeypatch.setattr(execution, "deliver", original)
    clock[0] += 61
    store.run_due()
    assert len(store.inbox(a)) == 2


def test_replanning_reads_shared_capacity_without_mutating_participant_facts(network):
    store, _, a, b, _ = network
    ready(store, a)
    ready(store, b)
    send(store, a, "approve", revision=1)
    before = store.read(b)
    result = send(store, b, "propose")
    assert result["code"] == "INSUFFICIENT_CAPACITY"
    assert result["state"]["plan"] is None
    assert result["state"]["resources"] == before["resources"]
    assert result["state"]["facts_version"] == before["facts_version"]


def test_http_network_and_cancellation_contract(tmp_path):
    client = TestClient(create_app(str(tmp_path / "http.sqlite3"), rehearsal=True))
    nid = client.post("/networks").json()["id"]
    state = client.post("/workspaces", json={"network_id": nid}).json()
    wid = state["id"]
    path = f"/workspaces/{wid}/commands"
    p = client.post(path, json={"action": "propose"}).json()["state"]["plan"]
    for rid in p["required"]:
        assert (
            client.post(
                path, json={"action": "accept", "revision": 1, "resource_id": rid}
            ).status_code
            == 200
        )
    assert client.post(path, json={"action": "approve", "revision": 1}).status_code == 200
    assert client.get(f"/networks/{nid}/reservations").json()
    assert (
        client.post(
            path, json={"action": "cancel", "revision": 1, "reason": "Synthetic cancellation"}
        ).status_code
        == 200
    )
    for rid in p["required"]:
        assert (
            client.post(
                path, json={"action": "ack_cancel", "revision": 1, "resource_id": rid}
            ).status_code
            == 200
        )
    assert all(r["released"] for r in client.get(f"/networks/{nid}/reservations").json())
    assert client.get(f"/workspaces/{wid}/inbox").json() == []
