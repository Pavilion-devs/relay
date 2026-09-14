"""Authorization must hold on reads, writes, replays and delayed model tools."""

import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from relay_core import access
from relay_core.api import create_app
from relay_core.engine import seed
from relay_core.store import Store


@pytest.fixture
def scoped(tmp_path):
    app = create_app(str(tmp_path / "access.sqlite3"))
    store = app.state.store
    network = store.create_network(seed()["resources"])
    grants = {
        name: access.issue(store, network, role, resource)
        for name, role, resource in [
            ("coordinator", "coordinator", None),
            ("donor", "donor", None),
            ("tunde", "driver", "tunde"),
            ("amara", "driver", "amara"),
            ("harbour", "recipient", "harbour"),
        ]
    }
    client = TestClient(app)

    def headers(name):
        return {"Authorization": "Bearer " + grants[name]["token"]}

    wid = client.post("/workspaces", headers=headers("coordinator")).json()["id"]
    path = f"/workspaces/{wid}"

    def send(name, action, **kw):
        return client.post(path + "/commands", headers=headers(name), json={"action": action, **kw})

    return store, client, grants, headers, wid, path, send


def test_default_requires_credentials_and_rejects_cross_network(scoped):
    store, client, _, headers, _, path, _ = scoped
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    other = store.create(seed())
    assert (
        client.get("/workspaces/" + other["id"], headers=headers("coordinator")).status_code == 403
    )
    assert client.post("/workspaces", headers=headers("tunde")).status_code == 403
    assert client.post("/networks", headers=headers("coordinator")).status_code == 403


def test_role_permissions_bind_participant_and_block_coordinator_impersonation(scoped):
    _, _, _, _, _, _, send = scoped
    assert send("coordinator", "propose").status_code == 200
    assert send("tunde", "accept", revision=1, resource_id="amara").status_code == 403
    assert send("tunde", "approve", revision=1).status_code == 403
    assert send("coordinator", "accept", revision=1, resource_id="tunde").status_code == 403
    assert send("harbour", "pickup", revision=1, resource_id="harbour").status_code == 403
    for who in ("tunde", "amara", "harbour"):
        assert send(who, "accept", revision=1, resource_id=who).status_code == 200
    assert send("coordinator", "approve", revision=1).status_code == 200
    assert send("tunde", "pickup", revision=1, resource_id="tunde").status_code == 200
    assert send("coordinator", "cancel", revision=1, reason="Synthetic return").status_code == 200
    assert (
        send(
            "tunde", "ack_return", revision=1, resource_id="tunde", evidence="Self-certified"
        ).status_code
        == 403
    )
    assert (
        send(
            "donor",
            "ack_return",
            revision=1,
            resource_id="tunde",
            evidence="Synthetic donor acknowledgment",
        ).status_code
        == 200
    )


def test_projection_excludes_other_messages_audit_and_routes(scoped):
    store, client, _, headers, wid, path, send = scoped
    send("coordinator", "propose")

    def insert(state):
        state["messages"] += [
            {"id": "private", "resource_id": "amara", "text": "Private Amara message"},
            {"id": "own", "resource_id": "tunde", "text": "Own message"},
        ]
        return {"ok": True}

    store.transact(wid, "fixture", {}, insert)
    result = client.get(path, headers=headers("tunde")).json()
    assert "resources" not in result and "audit" not in result and "events" not in result
    assert [m["id"] for m in result["messages"]] == ["own"]
    assert [r["driver"] for r in result["plan"]["routes"]] == ["tunde"]
    response = send("tunde", "accept", revision=1, resource_id="tunde").json()
    assert "resources" not in response["state"]
    assert "Private Amara message" not in str(response)


def test_replay_is_actor_bound_and_revocation_checked_before_receipt(scoped):
    store, _, grants, _, wid, _, send = scoped
    send("coordinator", "propose")
    assert (
        send("tunde", "accept", revision=1, resource_id="tunde", command_id="repeat").status_code
        == 200
    )
    assert (
        send("amara", "accept", revision=1, resource_id="amara", command_id="repeat").status_code
        == 409
    )
    with store.connect() as db:
        db.execute("UPDATE access_grants SET revoked=1 WHERE id=?", (grants["tunde"]["id"],))
    assert (
        send("tunde", "accept", revision=1, resource_id="tunde", command_id="repeat").status_code
        == 401
    )
    assert store.read(wid)["plan"]["accepted"] == ["tunde"]


def test_only_hash_is_stored_and_grants_expire(scoped):
    store, client, grants, headers, _, path, _ = scoped
    with store.connect() as db:
        row = db.execute(
            "SELECT digest FROM access_grants WHERE id=?", (grants["tunde"]["id"],)
        ).fetchone()
        assert row[0] == hashlib.sha256(grants["tunde"]["token"].encode()).hexdigest()
        db.execute("UPDATE access_grants SET expires=0 WHERE id=?", (grants["tunde"]["id"],))
    assert client.get(path, headers=headers("tunde")).status_code == 401


def test_revocation_between_request_and_transaction_blocks_write(scoped):
    store, _, grants, _, wid, _, _ = scoped
    scoped_store = access.ScopedStore(store, grants["tunde"]["token"], "tunde")
    assert scoped_store.read(wid)
    with store.connect() as db:
        db.execute("UPDATE access_grants SET revoked=1 WHERE id=?", (grants["tunde"]["id"],))
    with pytest.raises(PermissionError):
        scoped_store.transact(wid, str(uuid4()), {}, lambda state: state.update(status="complete"))
    assert store.read(wid)["status"] == "disrupted"


def test_participant_inbox_filters_other_recipients(scoped):
    store, client, _, headers, wid, path, _ = scoped
    with store.connect() as db:
        for rid in ("tunde", "amara", "coordinator"):
            db.execute(
                "INSERT INTO inbox VALUES(?,?,?,?,?,?)", (rid, wid, rid, "reminder", "Attention", 1)
            )
    assert [
        m["recipient"] for m in client.get(path + "/inbox", headers=headers("tunde")).json()
    ] == ["tunde"]
    assert len(client.get(path + "/inbox", headers=headers("coordinator")).json()) == 3


def test_sender_spoofing_rejected_before_model_call(scoped):
    _, client, _, headers, _, path, _ = scoped
    response = client.post(
        path + "/messages",
        headers=headers("tunde"),
        json={"resource_id": "amara", "text": "I accept"},
    )
    assert response.status_code == 403


def test_scoped_grants_survive_store_restart(scoped):
    store, _, grants, _, wid, _, _ = scoped
    reopened = Store(store.path)
    with reopened.connect() as db:
        actor = access.authorize(
            db, grants["tunde"]["token"], reopened.clock(), reopened.read(wid), "read"
        )
    assert actor["resource_id"] == "tunde"


def test_model_context_is_scoped_to_sender(scoped):
    store, _, grants, _, wid, _, send = scoped
    send("coordinator", "propose")
    adapter = access.ScopedStore(store, grants["tunde"]["token"], "tunde")
    context = adapter.model_context(store.read(wid), {"resource_id": "tunde", "text": "20 crates"})
    assert [r["id"] for r in context["resources"]] == ["tunde"]
    assert "travel_minutes" not in context["logistics"]
    assert [r["driver"] for r in context["plan"]["routes"]] == ["tunde"]


def test_failed_agent_write_after_revocation_does_not_create_audit_or_receipt(scoped):
    store, _, grants, _, wid, _, _ = scoped
    adapter = access.ScopedStore(store, grants["tunde"]["token"], "tunde")
    with store.connect() as db:
        db.execute("UPDATE access_grants SET revoked=1 WHERE id=?", (grants["tunde"]["id"],))
    with pytest.raises(PermissionError):
        adapter.transact(wid, "revoked-tool", {}, lambda state: {"ok": True})
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM commands WHERE id=?", ("revoked-tool",)).fetchone()[0]
            == 0
        )
    assert not store.read(wid).get("audit")
