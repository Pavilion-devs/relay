import base64
import hashlib
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from relay_core import access, identity
from relay_core.api import create_app
from relay_core.engine import seed


def setup(tmp_path, wrong_nonce=False):
    claims = {"iss": "issuer", "sub": "subject", "email": "a@b.test", "exp": time.time() + 600}
    verifier = SimpleNamespace(client_id="client", verify=lambda token: claims)
    exchanges = []

    def exchange(code, proof):
        exchanges.append((code, proof))
        return "verified-test-token"

    app = create_app(
        str(tmp_path / "login.db"),
        identity_verifier=verifier,
        login_config={
            "domain": "https://relay.auth.us-east-1.amazoncognito.com",
            "callback": "http://localhost/identity/callback",
            "exchange": exchange,
        },
    )
    store = app.state.store
    nid = store.create_network(seed()["resources"])
    admin = access.issue(store, nid, "coordinator")
    invitation = identity.invite(store, admin["token"], nid, claims["email"], "driver", "tunde")
    client = TestClient(app, base_url="http://localhost")
    start = client.post(
        "/identity/login", json={"invitation_token": invitation["invitation_token"]}
    )
    assert start.status_code == 200
    assert "HttpOnly" in start.headers["set-cookie"]
    query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    claims["nonce"] = "wrong" if wrong_nonce else query["nonce"][0]
    return client, query, exchanges, store


def test_pkce_callback_and_replay(tmp_path):
    client, query, exchanges, store = setup(tmp_path)
    params = {"state": query["state"][0], "code": "provider-code"}
    other = TestClient(client.app, base_url="http://localhost")
    assert other.get("/identity/callback", params=params).status_code == 403
    assert not exchanges
    result = client.get("/identity/callback", params=params)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    proof = exchanges[0][1]
    assert (
        base64.urlsafe_b64encode(hashlib.sha256(proof.encode()).digest()).decode().rstrip("=")
        == query["code_challenge"][0]
    )
    assert query["code_challenge_method"] == ["S256"]
    with store.connect() as db:
        assert access.principal(db, result.json()["token"], store.clock())["resource_id"] == "tunde"
        assert db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0
    assert client.get("/identity/callback", params=params).status_code == 403
    assert len(exchanges) == 1


def test_wrong_nonce_cannot_create_session(tmp_path):
    client, query, _, store = setup(tmp_path, wrong_nonce=True)
    assert (
        client.get(
            "/identity/callback", params={"state": query["state"][0], "code": "code"}
        ).status_code
        == 403
    )
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM identity_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["expired", "state", "duplicate"])
def test_invalid_callback_never_exchanges_code(tmp_path, kind):
    client, query, exchanges, store = setup(tmp_path)
    state = query["state"][0]
    if kind == "expired":
        with store.connect() as db:
            db.execute("UPDATE login_attempts SET expires=0")
    if kind == "state":
        state = "wrong"
    params = [("state", state), ("code", "code")]
    if kind == "duplicate":
        params.append(("state", state))
    assert client.get("/identity/callback", params=params).status_code in (400, 403)
    assert exchanges == []


def test_email_verification_requires_provider_proof_and_consumes_once(tmp_path):
    import json

    from relay_core.login import HostedLogin
    from relay_core.store import Store

    store = Store(str(tmp_path / "verification.db"))
    claims = {"iss": "issuer", "sub": "person", "email": "a@b.test", "exp": time.time() + 600}
    network = store.create_network(seed()["resources"])
    admin = access.issue(store, network, "coordinator")
    invitation = identity.invite(store, admin["token"], network, claims["email"], "driver", "tunde")
    login = HostedLogin(
        store,
        SimpleNamespace(client_id="client"),
        "https://relay.auth.us-east-1.amazoncognito.com",
        "http://localhost/identity/callback",
    )
    verified = [False]

    def get_user(**kw):
        return {
            "UserAttributes": [
                {"Name": k, "Value": v}
                for k, v in {
                    "sub": "person",
                    "email": claims["email"],
                    "email_verified": "true" if verified[0] else "false",
                }.items()
            ]
        }

    def verify(**kw):
        assert kw["Code"] == "123456"
        verified[0] = True

    login.verification_client = lambda: SimpleNamespace(
        get_user=get_user,
        get_user_attribute_verification_code=lambda **kw: {},
        verify_user_attribute=verify,
    )
    pending = login.begin_verification(
        "private-test-access",
        claims,
        json.dumps({"invitation_token": invitation["invitation_token"]}),
    )
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM identity_sessions").fetchone()[0] == 0
    result = login.verify_email(pending["ticket"], "123456")
    assert result["resource_id"] == "tunde"
    with pytest.raises(PermissionError):
        login.verify_email(pending["ticket"], "123456")
