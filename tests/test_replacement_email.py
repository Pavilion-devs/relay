import json
import re
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_partial_replacement import committed as make_committed
from test_partial_replacement import propose

from relay_core import email_actions, email_delivery, identity
from relay_core.api import create_app
from relay_core.engine import now


@pytest.fixture
def committed(tmp_path):
    yield from make_committed.__wrapped__(tmp_path)


@pytest.fixture
def queued_replacement(committed):
    r = committed
    invitation = identity.invite(
        r.store, r.tokens["coordinator"], r.network, "donor@example.org", "donor"
    )
    r.tokens["donor"] = identity.session(
        r.store,
        {
            "iss": "offline-fixture",
            "sub": "donor",
            "email": "donor@example.org",
            "exp": r.clock + 3600,
        },
        invitation_token=invitation["invitation_token"],
    )["token"]
    with r.store.connect() as db:
        db.execute("INSERT INTO email_subscriptions VALUES(?,1)", (invitation["invitation_id"],))
    r.command("tunde", "pickup", revision=1, resource_id="tunde")
    propose(r)
    # This fixture opts in amendment actors; the unaffected driver's pickup mail is separate.
    with r.store.connect() as db:
        db.execute(
            "UPDATE email_subscriptions SET enabled=0 WHERE membership IN (SELECT id FROM invitations WHERE resource='amara')"
        )
    r.clock += 61
    r.store.run_due()
    with r.store.connect() as db:
        messages = dict(
            db.execute("""SELECT COALESCE(i.resource,i.role),e.id FROM email_outbox e
            JOIN invitations i ON i.id=e.membership""")
        )
    assert set(messages) == {"tunde", "ada", "harbour", "donor"}
    return r, messages


@pytest.mark.parametrize(
    "actor,action",
    [
        ("ada", "accept_replacement"),
        ("harbour", "accept_replacement"),
        ("tunde", "ack_stop"),
        ("donor", "confirm_return"),
    ],
)
def test_browser_login_review_and_explicit_role_response(queued_replacement, actor, action):
    r, messages = queued_replacement
    claims = {
        "iss": "offline-fixture",
        "sub": actor,
        "email": actor + "@example.org",
        "exp": r.clock + 600,
    }
    app = create_app(
        r.store.path,
        identity_verifier=SimpleNamespace(client_id="client", verify=lambda token: claims),
        login_config={
            "domain": "https://relay.auth.us-east-1.amazoncognito.com",
            "callback": "http://localhost/identity/callback",
            "exchange": lambda *args: "fixture-token",
        },
    )
    app.state.store.clock = lambda: r.clock
    with TestClient(app, base_url="http://localhost") as client:
        eid = messages[actor]
        before = r.read()["replacement"]
        redirect = client.get("/reminders/" + eid, follow_redirects=False)
        assert redirect.status_code == 303
        query = parse_qs(urlsplit(redirect.headers["location"]).query)
        claims["nonce"] = query["nonce"][0]
        page = client.get(
            "/identity/callback", params={"code": "fixture", "state": query["state"][0]}
        )
        assert page.status_code == 200 and "Review partial replacement" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert r.read()["replacement"] == before
        assert ("Return evidence" in page.text) is (actor == "donor")
        auth = json.loads(re.search(r"const auth=(.*?);const button", page.text)[1])
        body = (
            {"evidence": "SYNTHETIC: entire 192 kg returned to donor"} if actor == "donor" else {}
        )
        response = client.post(
            "/email-actions/" + eid + "/confirm",
            json=body,
            headers={"Authorization": "Bearer " + auth["token"]},
        )
        assert response.status_code == 200 and "state" not in response.json()
        assert r.read()["audit"][-1]["action"] == action
        events = len(r.read()["events"])
        replay = client.post(
            "/email-actions/" + eid + "/confirm",
            json=body,
            headers={"Authorization": "Bearer " + auth["token"]},
        )
        assert replay.status_code == 200 and len(r.read()["events"]) == events
        assert r.read()["plan"]["revision"] == 1  # Never coordinator dispatch.


def test_wrong_membership_and_missing_return_evidence(queued_replacement):
    r, messages = queued_replacement
    for actor in ("tunde", "amara", "harbour", "donor"):
        with pytest.raises(PermissionError):
            email_actions.confirm(r.store, r.tokens[actor], messages["ada"])
    with pytest.raises(PermissionError, match="evidence"):
        email_actions.confirm(r.store, r.tokens["donor"], messages["donor"])
    assert r.read()["replacement"]["return"] is None
    assert email_actions.confirm(
        r.store, r.tokens["donor"], messages["donor"], "Full 192 kg returned"
    )["ok"]


def test_refreshed_request_same_revision_invalidates_old_links_and_queue(queued_replacement):
    r, messages = queued_replacement
    state = r.read()
    old_id = state["replacement"]["id"]
    state["replacement"]["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    with r.store.connect() as db:
        db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), r.wid))
    propose(r)
    assert r.read()["plan"]["revision"] == 1 and r.read()["replacement"]["id"] != old_id
    for actor, eid in messages.items():
        assert not email_actions.review(r.store, r.tokens[actor], eid)["current"]
        with pytest.raises(PermissionError):
            email_actions.confirm(r.store, r.tokens[actor], eid, "Full return")
    assert email_delivery.claim(r.store) is None
    with r.store.connect() as db:
        assert all(x[0] == "obsolete" for x in db.execute("SELECT state FROM email_outbox"))
    assert r.read()["replacement"]["accepted"] == []


def test_already_answered_email_suppressed_and_consent_respected(queued_replacement):
    r, messages = queued_replacement
    assert email_actions.confirm(r.store, r.tokens["ada"], messages["ada"])["ok"]
    with r.store.connect() as db:
        db.execute(
            "UPDATE email_subscriptions SET enabled=0 WHERE membership IN (SELECT id FROM invitations WHERE resource!=? OR resource IS NULL)",
            ("ada",),
        )
    assert email_delivery.claim(r.store) is None
    assert email_actions.review(r.store, r.tokens["ada"], messages["ada"])["already_confirmed"]


def test_return_requires_donor_and_no_command_injection(queued_replacement):
    r, messages = queued_replacement
    response = r.client.post(
        "/email-actions/" + messages["tunde"] + "/confirm",
        headers={"Authorization": "Bearer " + r.tokens["tunde"]},
        json={"action": "confirm_return", "evidence": "forged"},
    )
    assert response.status_code == 422
    assert r.read()["replacement"]["return"] is None


def test_replacement_body_and_queue_send_use_existing_provider_adapter(queued_replacement):
    r, messages = queued_replacement
    captured = []
    provider = SimpleNamespace(
        send_queued=lambda ticket: captured.append(ticket) or "fixture-provider-id"
    )
    assert email_delivery.run_once(r.store, provider)
    assert len(captured) == 1 and captured[0]["id"] in messages.values()
    assert "partial replacement" in captured[0]["body"]
    with r.store.connect() as db:
        assert (
            db.execute(
                "SELECT state FROM email_outbox WHERE id=?", (captured[0]["id"],)
            ).fetchone()[0]
            == "accepted"
        )


def test_request_refresh_during_confirmation_rechecks_under_write_lock(
    queued_replacement, monkeypatch
):
    r, messages = queued_replacement
    original = r.store.transact

    def changed(*args, **kwargs):
        state = r.read()
        state["replacement"]["id"] = "different-proposal"
        with r.store.connect() as db:
            db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), r.wid))
        return original(*args, **kwargs)

    monkeypatch.setattr(r.store, "transact", changed)
    with pytest.raises(PermissionError):
        email_actions.confirm(r.store, r.tokens["ada"], messages["ada"])
    assert r.read()["replacement"]["accepted"] == []


def test_email_responses_gate_partial_commit_through_receipt(queued_replacement):
    r, messages = queued_replacement
    original = r.read()["plan"]
    request_id = r.read()["replacement"]["id"]
    for actor in ("tunde", "ada", "harbour"):
        assert email_actions.confirm(r.store, r.tokens[actor], messages[actor])["ok"]
    result = r.command(
        "coordinator",
        "commit_replacement",
        revision=1,
        replacement_request_id=request_id,
        expected=409,
    )
    assert result["code"] == "CUSTODY_UNRESOLVED"
    assert email_actions.confirm(
        r.store, r.tokens["donor"], messages["donor"], "Synthetic complete 192 kg return"
    )["ok"]
    r.command("coordinator", "commit_replacement", revision=1, replacement_request_id=request_id)
    assert r.read()["plan"]["revision"] == 2
    for actor, eid in messages.items():
        assert not email_actions.review(r.store, r.tokens[actor], eid)["current"]
        with pytest.raises(PermissionError):
            email_actions.confirm(r.store, r.tokens[actor], eid, "Another return")
    r.command("amara", "pickup", revision=1, resource_id="amara")
    r.command("ada", "pickup", revision=2, resource_id="ada")
    r.command("harbour", "receive", revision=2, resource_id="harbour", received_kg=320)
    assert r.read()["status"] == "complete" and not r.active_reservations()
    assert next(x for x in r.read()["plan"]["routes"] if x["driver"] == "amara") == next(
        x for x in original["routes"] if x["driver"] == "amara"
    )
    assert email_delivery.claim(r.store) is None


def test_revoked_membership_cannot_review_or_respond(queued_replacement):
    r, messages = queued_replacement
    with r.store.connect() as db:
        membership = db.execute(
            "SELECT membership FROM email_outbox WHERE id=?", (messages["ada"],)
        ).fetchone()[0]
    identity.revoke(r.store, r.tokens["coordinator"], membership)
    with pytest.raises(PermissionError):
        email_actions.review(r.store, r.tokens["ada"], messages["ada"])
    with pytest.raises(PermissionError):
        email_actions.confirm(r.store, r.tokens["ada"], messages["ada"])


def test_expired_page_has_no_response_form(queued_replacement):
    r, messages = queued_replacement
    state = r.read()
    state["replacement"]["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    with r.store.connect() as db:
        db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), r.wid))
    page = email_actions.review_page(
        r.store, {"token": r.tokens["ada"], "reminder_id": messages["ada"]}
    )
    assert "no longer current" in page and "<button" not in page
    assert email_delivery.claim(r.store) is None
