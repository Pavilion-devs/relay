from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_email_delivery import queued as make_queue

from relay_core.api import create_app


@pytest.fixture
def queued(tmp_path):
    return make_queue.__wrapped__(tmp_path)


def test_link_login_review_then_explicit_confirmation(queued):
    store, _, _, wid = queued
    claims = {"iss": "test", "sub": "test", "email": "test@example.org", "exp": store.clock() + 600}
    verifier = SimpleNamespace(client_id="client", verify=lambda token: claims)
    app = create_app(
        store.path,
        identity_verifier=verifier,
        login_config={
            "domain": "https://relay.auth.us-east-1.amazoncognito.com",
            "callback": "http://localhost/identity/callback",
            "exchange": lambda *args: "signed-test-token",
        },
    )
    with store.connect() as db:
        eid = db.execute("SELECT id FROM email_outbox").fetchone()[0]
    client = TestClient(app, base_url="http://localhost")
    first = client.get("/reminders/" + eid, follow_redirects=False)
    assert first.status_code == 303
    assert "test@example.org" not in first.headers["location"]
    assert "tunde" not in store.read(wid)["plan"]["accepted"]
    query = parse_qs(urlsplit(first.headers["location"]).query)
    claims["nonce"] = query["nonce"][0]
    page = client.get("/identity/callback", params={"code": "test", "state": query["state"][0]})
    assert page.status_code == 200
    assert "Confirm my commitment" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "tunde" not in store.read(wid)["plan"]["accepted"]
    # Read the issued bearer from the test-rendered page, as the button does.
    import json
    import re

    auth = json.loads(re.search(r"const auth=(.*?);const button", page.text)[1])
    result = client.post(
        "/email-actions/" + eid + "/confirm", headers={"Authorization": "Bearer " + auth["token"]}
    )
    assert result.status_code == 200 and "state" not in result.json()
    assert "tunde" in store.read(wid)["plan"]["accepted"]
