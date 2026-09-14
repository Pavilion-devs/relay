"""Driver-only amendment with live reservations, custody and revision provenance."""

from copy import deepcopy
from datetime import timedelta

import pytest

from relay_core.engine import now
from scripts.rehearse_commitment_failure import Rehearsal


@pytest.fixture
def committed(tmp_path):
    r = Rehearsal(tmp_path / "partial.sqlite3")
    r.command("coordinator", "propose")
    for rid in r.read()["plan"]["required"]:
        r.command(rid, "accept", revision=1, resource_id=rid)
    r.command("coordinator", "approve", revision=1)
    yield r
    r.client.close()


def propose(r, expected=200):
    return r.command(
        "coordinator",
        "propose_replacement",
        revision=1,
        resource_id="tunde",
        replacement_id="ada",
        available=True,
        reason="Driver breakdown; Ada explicitly offered the replacement load",
        expected=expected,
    )


def evidence(r, action, actor, **fields):
    return r.command(
        actor, action, revision=1, replacement_request_id=r.read()["replacement"]["id"], **fields
    )


def ready(r):
    propose(r)
    evidence(r, "ack_stop", "tunde", resource_id="tunde")
    for rid in r.read()["replacement"]["required"]:
        evidence(r, "accept_replacement", rid, resource_id=rid)


def test_partial_recovery_preserves_unaffected_pickup_and_route(committed):
    r = committed
    old = deepcopy(r.read()["plan"])
    r.command("tunde", "pickup", revision=1, resource_id="tunde")
    ready(r)
    held = r.active_reservations()
    assert (
        evidence(r, "commit_replacement", "coordinator", expected=409)["code"]
        == "CUSTODY_UNRESOLVED"
    )
    assert r.active_reservations() == held
    evidence(
        r, "confirm_return", "tunde", resource_id="tunde", evidence="forged donor", expected=403
    )
    evidence(r, "confirm_return", "donor", resource_id="tunde", evidence="192 kg returned to donor")
    # Unaffected driver can still record pickup while the amendment is pending.
    r.command("amara", "pickup", revision=1, resource_id="amara")
    evidence(r, "commit_replacement", "coordinator", key="swap")
    p = r.read()["plan"]
    assert p["revision"] == 2 and p["picked_up"] == ["amara"]
    assert next(d for d in p["drivers"] if d["resource_id"] == "amara") == old["drivers"][1]
    assert next(route for route in p["routes"] if route["driver"] == "amara") == next(
        route for route in old["routes"] if route["driver"] == "amara"
    )
    assert next(d["kg"] for d in p["drivers"] if d["resource_id"] == "ada") == 192
    assert not any(
        a["action"] == "accept_replacement" and a["resource_id"] == "amara"
        for a in r.read()["audit"]
    )
    assert {x["resource"] for x in r.active_reservations()} == {"ada", "amara", "harbour"}
    assert all(x["revision"] == 2 for x in r.active_reservations())
    with r.store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE workspace=? AND revision=2 AND kind='receipts'",
                (r.wid,),
            ).fetchone()[0]
            == 1
        )
    r.command("tunde", "pickup", revision=1, resource_id="tunde", expected=409)
    r.command("ada", "pickup", revision=1, resource_id="ada", expected=409)
    r.command("ada", "pickup", revision=2, resource_id="ada")
    r.command("harbour", "receive", revision=2, resource_id="harbour", received_kg=320)
    assert r.read()["status"] == "complete" and not r.active_reservations()


def test_unaffected_original_revision_still_authorizes_unchanged_pickup(committed):
    r = committed
    ready(r)
    draft_id = r.read()["replacement"]["id"]
    evidence(r, "commit_replacement", "coordinator", key="swap")
    held = r.active_reservations()
    r.command(
        "coordinator", "commit_replacement", revision=1, replacement_request_id=draft_id, key="swap"
    )
    assert r.active_reservations() == held and len(r.read()["plans"]) == 1
    r.command("amara", "pickup", revision=1, resource_id="amara")
    r.command("harbour", "accept", revision=1, resource_id="harbour", expected=409)
    r.command("tunde", "pickup", revision=2, resource_id="tunde", expected=409)
    assert r.read()["plan"]["picked_up"] == ["amara"]


def test_affected_confirmation_and_stop_required(committed):
    r = committed
    propose(r)
    assert (
        evidence(r, "commit_replacement", "coordinator", expected=409)["code"]
        == "CUSTODY_UNRESOLVED"
    )
    evidence(r, "ack_stop", "tunde", resource_id="tunde")
    assert (
        evidence(r, "commit_replacement", "coordinator", expected=409)["code"]
        == "MISSING_CONFIRMATIONS"
    )
    evidence(r, "accept_replacement", "amara", resource_id="amara", expected=409)
    evidence(r, "accept_replacement", "tunde", resource_id="ada", expected=403)
    evidence(r, "accept_replacement", "ada", resource_id="ada")
    assert (
        evidence(r, "commit_replacement", "coordinator", expected=409)["code"]
        == "MISSING_CONFIRMATIONS"
    )
    assert r.read()["plan"]["revision"] == 1


def test_pending_failed_driver_movement_and_receipt_blocked(committed):
    r = committed
    propose(r)
    assert (
        r.command("tunde", "pickup", revision=1, resource_id="tunde", expected=409)["code"]
        == "REPLACEMENT_PENDING"
    )
    r.command(
        "harbour", "receive", revision=1, resource_id="harbour", received_kg=320, expected=409
    )
    assert r.read()["plan"]["picked_up"] == [] and r.read()["plan"]["receipts"] == {}


def test_expired_replacement_does_not_release_or_reuse_consent(committed):
    r = committed
    ready(r)
    held = r.active_reservations()
    old = r.read()
    old["replacement"]["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    import json

    with r.store.connect() as db:
        db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(old), r.wid))
    assert (
        evidence(r, "commit_replacement", "coordinator", expected=409)["code"]
        == "EXPIRED_REPLACEMENT"
    )
    propose(r)
    new = r.read()["replacement"]
    assert new["id"] != old["replacement"]["id"] and new["accepted"] == [] and new["stopped"]
    r.command(
        "ada",
        "accept_replacement",
        revision=1,
        resource_id="ada",
        replacement_request_id=old["replacement"]["id"],
        expected=409,
    )
    assert r.active_reservations() == held


def test_capacity_race_rolls_back_entire_swap(committed):
    r = committed
    ready(r)
    held = r.active_reservations()
    # Another workspace acquired Ada after this proposal; inject the competing
    # reservation at the same persistent boundary used by committed workspaces.
    with r.store.connect() as db:
        db.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?,?,0)",
            ("competing-workspace", 1, r.network, "ada", "driver", 192),
        )
    assert (
        evidence(r, "commit_replacement", "coordinator", expected=409)["code"]
        == "RESERVATION_CONFLICT"
    )
    assert r.read()["plan"]["revision"] == 1 and r.read()["plans"] == []
    assert [x for x in r.active_reservations() if x["workspace"] == r.wid] == held
    assert r.read()["replacement"]["accepted"] == ["ada", "harbour"]


def test_infeasible_route_and_shared_driver_are_rejected(committed):
    r = committed
    with r.store.connect() as db:
        db.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?,?,0)",
            ("other", 1, r.network, "ada", "driver", 10),
        )
    assert propose(r, expected=409)["code"] == "INSUFFICIENT_CAPACITY"
    assert not r.read().get("replacement") and r.read()["status"] == "committed"


def test_receipt_evidence_cannot_be_amended_away(committed):
    r = committed
    for rid in ("tunde", "amara"):
        r.command(rid, "pickup", revision=1, resource_id=rid)
    r.command("harbour", "receive", revision=1, resource_id="harbour", received_kg=300)
    propose(r, expected=409)
    assert r.read()["plan"]["receipts"] == {"harbour": 300}


def test_infeasible_replacement_holds_failed_driver_and_escalates(committed):
    r = committed
    with r.store.connect() as db:
        db.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?,?,0)",
            ("other", 1, r.network, "ada", "driver", 10),
        )
    propose(r, expected=409)
    assert (
        r.command("tunde", "pickup", revision=1, resource_id="tunde", expected=409)["code"]
        == "REPLACEMENT_PENDING"
    )
    r.clock += 61
    r.store.run_due()
    assert any(
        x["kind"] == "escalation" and x["recipient"] == "coordinator" for x in r.store.inbox(r.wid)
    )
    assert r.read()["plan"]["revision"] == 1 and len(r.active_reservations()) == 4


def test_unanswered_replacement_escalates_with_opted_in_email_queue(committed):
    r = committed
    propose(r)
    r.clock += 61
    r.store.run_due()
    assert {"tunde", "ada", "harbour"} <= {x["recipient"] for x in r.store.inbox(r.wid)}
    for _ in range(2):
        r.clock += 301
        r.store.run_due()
    assert any(
        x["kind"] == "escalation" and x["recipient"] == "coordinator" for x in r.store.inbox(r.wid)
    )
    with r.store.connect() as db:
        rows = db.execute("SELECT state FROM email_outbox").fetchall()
        assert rows and all(row[0] == "pending" for row in rows)
    assert r.read()["plan"]["revision"] == 1 and r.active_reservations()


def test_missing_travel_preserves_committed_plan_and_holds_failure(committed):
    r = committed
    import json

    state = r.read()
    del state["logistics"]["travel_minutes"]["ada"]
    with r.store.connect() as db:
        db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), r.wid))
    assert propose(r, expected=409)["code"] == "MISSING_TRAVEL"
    assert r.read()["plan"] == state["plan"]
    assert r.read()["replacement_failure"]["driver"] == "tunde"
