import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_partial_replacement import committed as make_committed
from test_partial_replacement import propose

from relay_core import email_actions, email_delivery
from relay_core.api import create_app


@pytest.fixture
def movement(tmp_path):
    fixture = make_committed.__wrapped__(tmp_path)
    r = next(fixture)
    # Deliver scheduled reminders using an advanced worker clock; retain real grant time.
    real = r.clock
    r.clock += 1801
    r.store.run_due()
    r.clock = real
    with r.store.connect() as db:
        rows = db.execute("""SELECT i.resource,e.id,b.id FROM email_outbox e
            JOIN invitations i ON i.id=e.membership JOIN inbox b ON b.id=e.inbox_id""").fetchall()
    messages = {
        (rid, "pickups" if ":pickups:" in inbox else "receipts"): eid for rid, eid, inbox in rows
    }
    yield r, messages
    try:
        next(fixture)
    except StopIteration:
        pass


def post(r, eid, actor, body=None):
    return r.client.post(
        "/email-actions/" + eid + "/confirm",
        json=body or {},
        headers={"Authorization": "Bearer " + r.tokens[actor]},
    )


@pytest.mark.parametrize("role,kind", [("tunde", "pickups"), ("harbour", "receipts")])
def test_login_review_then_explicit_movement_submission(movement, role, kind):
    r, messages = movement
    if kind == "receipts":
        for driver in ("tunde", "amara"):
            assert post(r, messages[driver, "pickups"], driver).status_code == 200
    claims = {
        "iss": "offline-fixture",
        "sub": role,
        "email": role + "@example.org",
        "exp": r.clock + 600,
    }
    app = create_app(
        r.store.path,
        identity_verifier=SimpleNamespace(client_id="client", verify=lambda token: claims),
        login_config={
            "domain": "https://relay.auth.us-east-1.amazoncognito.com",
            "callback": "http://localhost/identity/callback",
            "exchange": lambda *args: "test",
        },
    )
    eid = messages[role, kind]
    with TestClient(app, base_url="http://localhost") as client:
        before = r.read()["plan"]
        redirect = client.get("/reminders/" + eid, follow_redirects=False)
        q = parse_qs(urlsplit(redirect.headers["location"]).query)
        claims["nonce"] = q["nonce"][0]
        page = client.get("/identity/callback", params={"code": "test", "state": q["state"][0]})
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert r.read()["plan"] == before
        assert ('id="quantity"' in page.text) == (kind == "receipts")
        assert 'value="320"' not in page.text
        auth = json.loads(re.search(r"const auth=(.*?);const button", page.text)[1])
        response = client.post(
            "/email-actions/" + eid + "/confirm",
            json={"received_kg": 320} if kind == "receipts" else {},
            headers={"Authorization": "Bearer " + auth["token"]},
        )
        assert response.status_code == 200 and "state" not in response.json()
        assert r.read()["audit"][-1]["resource_id"] == role


@pytest.mark.parametrize(
    "amount,status", [(320, "complete"), (300, "discrepancy"), (0, "discrepancy")]
)
def test_receipt_quantities_retry_and_immutable_record(movement, amount, status):
    r, m = movement
    for driver in ("tunde", "amara"):
        assert post(r, m[driver, "pickups"], driver).status_code == 200
    eid = m["harbour", "receipts"]
    first = post(r, eid, "harbour", {"received_kg": amount})
    assert first.status_code == 200 and first.json()["movement_status"] == status
    assert r.read()["status"] == status
    events = len(r.read()["events"])
    assert post(r, eid, "harbour", {"received_kg": amount}).status_code == 200
    assert len(r.read()["events"]) == events
    assert post(r, eid, "harbour", {"received_kg": 1 if amount == 0 else 0}).status_code == 409
    assert r.read()["plan"]["receipts"] == {"harbour": amount}
    page = email_actions.review_page(r.store, {"token": r.tokens["harbour"], "reminder_id": eid})
    assert "<button" not in page and f"Recorded receipt: {amount} kg" in page
    if status == "complete":
        assert not r.active_reservations()
    else:
        assert r.active_reservations()
        r.clock += 2200
        r.store.run_due()
        r.clock += 301
        r.store.run_due()
        assert any(
            x["kind"] == "escalation" and x["recipient"] == "coordinator"
            for x in r.store.inbox(r.wid)
        )


def test_no_pickup_blocks_receipt_but_does_not_poison_retry(movement):
    r, m = movement
    eid = m["harbour", "receipts"]
    page = email_actions.review_page(r.store, {"token": r.tokens["harbour"], "reminder_id": eid})
    assert "<button" not in page and "not yet recorded" in page
    assert post(r, eid, "harbour", {"received_kg": 320}).status_code == 403
    for driver in ("tunde", "amara"):
        post(r, m[driver, "pickups"], driver)
    assert post(r, eid, "harbour", {"received_kg": 320}).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"received_kg": True},
        {"received_kg": 1.5},
        {"received_kg": -1},
        {"received_kg": 321},
        {"received_kg": "320"},
    ],
)
def test_invalid_receipt_requires_correction_without_consuming_key(movement, body):
    r, m = movement
    for driver in ("tunde", "amara"):
        post(r, m[driver, "pickups"], driver)
    assert post(r, m["harbour", "receipts"], "harbour", body).status_code == 422
    assert not r.read()["plan"]["receipts"]
    assert post(r, m["harbour", "receipts"], "harbour", {"received_kg": 320}).status_code == 200


def test_other_membership_and_pending_failure_cannot_record_movement(movement):
    r, m = movement
    assert post(r, m["tunde", "pickups"], "amara").status_code == 403
    propose(r)
    assert post(r, m["tunde", "pickups"], "tunde").status_code == 403
    assert post(r, m["harbour", "receipts"], "harbour", {"received_kg": 320}).status_code == 403
    assert post(r, m["amara", "pickups"], "amara").status_code == 200


def test_completed_pickup_not_emailed_and_cancelled_link_cannot_write(movement):
    r, m = movement
    post(r, m["tunde", "pickups"], "tunde")
    post(r, m["amara", "pickups"], "amara")
    # Move queue clock to due time without using expired identity sessions.
    r.clock += 1801
    ticket = email_delivery.claim(r.store)
    assert ticket["id"] == m["harbour", "receipts"]
    r.clock -= 1801
    r.command("coordinator", "cancel", revision=1, reason="Synthetic cancellation")
    assert post(r, m["harbour", "receipts"], "harbour", {"received_kg": 320}).status_code == 403
