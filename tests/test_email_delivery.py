import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from relay_core import access, identity
from relay_core import email_delivery as mail
from relay_core.engine import apply, seed
from relay_core.store import Store


@pytest.fixture
def queued(tmp_path):
    now = [datetime.fromisoformat(seed()["logistics"]["starts_at"]).timestamp()]
    store = Store(str(tmp_path / "mail.db"), clock=lambda: now[0])
    state = store.create(seed())
    admin = access.issue(store, state["network_id"], "coordinator")
    invite = identity.invite(
        store, admin["token"], state["network_id"], "test@example.org", "driver", "tunde"
    )
    identity.session(
        store,
        {"exp": now[0] + 3600, "iss": "test", "sub": "test", "email": "test@example.org"},
        invitation_token=invite["invitation_token"],
    )
    with store.connect() as db:
        db.execute("INSERT INTO email_subscriptions VALUES(?,1)", (invite["invitation_id"],))
    store.transact(state["id"], "propose", {}, lambda s: apply(s, {"action": "propose"}))
    now[0] += 61
    store.run_due()
    return store, now, invite["invitation_id"], state["id"]


def status(store):
    with store.connect() as db:
        return db.execute("SELECT state FROM email_outbox").fetchone()[0]


def test_accepted_is_not_delivered_and_not_resent(queued):
    store, _, _, _ = queued
    calls = []
    p = SimpleNamespace(send=lambda *args: calls.append(args) or "provider-id")
    assert mail.run_once(store, p)
    assert status(store) == "accepted"
    assert not mail.run_once(store, p)
    assert len(calls) == 1


def test_timeout_and_crashed_claim_are_uncertain(queued):
    store, now, _, _ = queued
    ticket = mail.claim(store)
    now[0] += 61
    assert mail.claim(store) is None
    assert status(store) == "uncertain"
    assert not mail.finish(store, ticket, "accepted", "late")


def test_timeout_never_automatically_retries(queued):
    store, now, _, _ = queued

    def send(*args):
        raise TimeoutError()

    mail.run_once(store, SimpleNamespace(send=send))
    now[0] += 1000
    assert status(store) == "uncertain"
    assert not mail.run_once(store, SimpleNamespace(send=send))


def test_explicit_throttling_retries_are_bounded(queued):
    store, now, _, _ = queued

    def send(*args):
        raise mail.RetryableRejection()

    for _ in range(3):
        assert mail.run_once(store, SimpleNamespace(send=send))
        now[0] += 500
    assert status(store) == "failed"


@pytest.mark.parametrize("change", ["revoked", "unsubscribe", "revision"])
def test_obsolete_or_disabled_recipient_suppressed(queued, change):
    store, _, membership, wid = queued
    with store.connect() as db:
        if change == "revoked":
            db.execute("UPDATE invitations SET revoked=1 WHERE id=?", (membership,))
        elif change == "unsubscribe":
            db.execute("UPDATE email_subscriptions SET enabled=0")
        else:
            s = store.read(wid)
            s["plan"]["revision"] += 1
            db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(s), wid))
    assert mail.claim(store) is None
    assert status(store) == "obsolete"


def test_confirmation_received_before_send_suppresses_email(queued):
    store, _, _, wid = queued
    store.transact(
        wid,
        "accept",
        {},
        lambda s: apply(s, {"action": "accept", "resource_id": "tunde", "revision": 1}),
    )
    assert mail.claim(store) is None
    assert status(store) == "obsolete"


def member_session(store, membership):
    return identity.session(
        store, {"exp": store.clock() + 600, "iss": "test", "sub": "test"}, membership_id=membership
    )["token"]


def test_participant_consent_is_bound_and_audited(queued):
    store, _, membership, _ = queued
    token = member_session(store, membership)
    assert mail.consent(store, token, False)["enabled"] is False
    assert status(store) == "obsolete"
    assert mail.consent(store, token, True)["enabled"] is True
    assert status(store) == "obsolete"  # renewed consent never revives an old reminder
    with store.connect() as db:
        rows = db.execute(
            "SELECT membership,enabled FROM email_consent_audit ORDER BY recorded,rowid"
        ).fetchall()
    assert rows == [(membership, 0), (membership, 1)]


def test_local_grant_cannot_supply_participant_consent(queued):
    store, _, _, wid = queued
    network = store.read(wid)["network_id"]
    for role, resource in [("coordinator", None), ("driver", "tunde")]:
        grant = access.issue(store, network, role, resource)
        with pytest.raises(PermissionError):
            mail.consent(store, grant["token"], True)


def test_uncertain_send_blocks_later_reminder_for_same_job(queued):
    store, now, _, wid = queued
    state = store.read(wid)
    state["plan"]["expires_at"] = datetime.fromtimestamp(
        now[0] + 1000, tz=datetime.now().astimezone().tzinfo
    ).isoformat()
    with store.connect() as db:
        db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), wid))

    def fail(*args):
        raise TimeoutError()

    mail.run_once(store, SimpleNamespace(send=fail))
    now[0] += 301
    store.run_due()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 2
    assert mail.claim(store) is None
    with store.connect() as db:
        assert {r[0] for r in db.execute("SELECT state FROM email_outbox")} == {
            "uncertain",
            "pending",
        }


def test_status_only_visible_to_own_network_coordinator(queued):
    store, _, membership, wid = queued
    network = store.read(wid)["network_id"]
    admin = access.issue(store, network, "coordinator")
    rows = mail.status_for_network(store, admin["token"], network)
    assert len(rows) == 1 and rows[0]["state"] == "pending"
    assert "email" not in rows[0]
    with pytest.raises(PermissionError):
        mail.status_for_network(store, member_session(store, membership), network)
    other = store.create_network(seed()["resources"])
    with pytest.raises(PermissionError):
        mail.status_for_network(store, admin["token"], other)


def test_authenticated_email_confirmation_and_wrong_member(queued):
    from relay_core import email_actions

    store, _, membership, wid = queued
    token = member_session(store, membership)
    with store.connect() as db:
        eid = db.execute("SELECT id FROM email_outbox").fetchone()[0]
    local = access.issue(store, store.read(wid)["network_id"], "driver", "tunde")
    with pytest.raises(PermissionError):
        email_actions.confirm(store, local["token"], eid)
    assert email_actions.confirm(store, token, eid)["ok"]
    assert "tunde" in store.read(wid)["plan"]["accepted"]
    assert email_actions.confirm(store, token, eid)["ok"]


def test_email_confirmation_rejects_stale_revision(queued):
    from relay_core import email_actions

    store, _, membership, wid = queued
    with store.connect() as db:
        eid = db.execute("SELECT id FROM email_outbox").fetchone()[0]
        state = store.read(wid)
        state["plan"]["revision"] += 1
        db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), wid))
    assert not email_actions.confirm(store, member_session(store, membership), eid)["ok"]
    assert "tunde" not in store.read(wid)["plan"]["accepted"]


def test_provider_events_deduplicate_and_preserve_complaint(queued):
    from relay_core import email_events

    store, _, membership, _ = queued
    ticket = mail.claim(store)
    mail.finish(store, ticket, "uncertain")
    assert email_events.record(store, "event1", ticket["id"], "ses-id", "complaint")
    assert not email_events.record(store, "event1", ticket["id"], "ses-id", "complaint")
    with pytest.raises(ValueError):
        email_events.record(store, "event1", ticket["id"], "ses-id", "delivery")
    email_events.record(store, "event2", ticket["id"], "ses-id", "delivery")
    with store.connect() as db:
        assert email_events.outcome(db, ticket["id"]) == "complaint"
        assert (
            db.execute(
                "SELECT enabled FROM email_subscriptions WHERE membership=?", (membership,)
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(PermissionError):
        mail.consent(store, member_session(store, membership), True)
    with pytest.raises(ValueError):
        email_events.record(store, "event3", ticket["id"], "other-id", "delivery")
