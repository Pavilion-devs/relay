import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from relay_core import access, identity
from relay_core.api import create_app
from relay_core.engine import seed


@pytest.fixture
def verifier():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = identity.CognitoVerifier(
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example", "client"
    )
    v.keys = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private.public_key())
    )
    claims = {
        "iss": v.issuer,
        "aud": "client",
        "sub": "person-a",
        "email": "driver@example.org",
        "email_verified": True,
        "token_use": "id",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    return v, private, claims


@pytest.mark.parametrize(
    "patch",
    [
        {"aud": "other"},
        {"iss": "https://attacker.example"},
        {"exp": 1},
        {"token_use": "access"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"sub": ""},
    ],
)
def test_signed_but_wrong_identity_claims_rejected(verifier, patch):
    v, key, claims = verifier
    with pytest.raises(PermissionError):
        v.verify(jwt.encode(claims | patch, key, algorithm="RS256"))


def test_signature_and_required_claims(verifier):
    v, key, claims = verifier
    assert v.verify(jwt.encode(claims, key, algorithm="RS256"))["sub"] == "person-a"
    bad = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(PermissionError):
        v.verify(jwt.encode(claims, bad, algorithm="RS256"))
    del claims["exp"]
    with pytest.raises(PermissionError):
        v.verify(jwt.encode(claims, key, algorithm="RS256"))


def test_http_invitation_binding_renewal_and_revocation(tmp_path, verifier):
    v, key, claims = verifier
    app = create_app(str(tmp_path / "identity.db"), identity_verifier=v)
    store = app.state.store
    nid = store.create_network(seed()["resources"])
    coordinator = access.issue(store, nid, "coordinator")
    headers = {"Authorization": "Bearer " + coordinator["token"]}
    client = TestClient(app)
    invitation = client.post(
        "/identity/invitations",
        headers=headers,
        json={
            "network_id": nid,
            "email": claims["email"],
            "role": "driver",
            "resource_id": "tunde",
        },
    ).json()

    def redeem(c, **kw):
        return client.post(
            "/identity/session", json={"id_token": jwt.encode(c, key, algorithm="RS256"), **kw}
        )

    assert (
        redeem(
            claims | {"email": "other@example.org"}, invitation_token=invitation["invitation_token"]
        ).status_code
        == 403
    )
    first = redeem(claims, invitation_token=invitation["invitation_token"])
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    membership = first.json()["membership_id"]
    assert redeem(claims, invitation_token=invitation["invitation_token"]).status_code == 403
    assert redeem(claims | {"sub": "someone-else"}, membership_id=membership).status_code == 403
    second = redeem(claims | {"email": "changed@example.org"}, membership_id=membership)
    assert second.status_code == 200  # stable subject, not a reusable email identity
    participant = {"Authorization": "Bearer " + first.json()["token"]}
    assert (
        client.post(
            "/identity/invitations",
            headers=participant,
            json={"network_id": nid, "email": "x@y.z", "role": "donor"},
        ).status_code
        == 403
    )
    assert (
        client.post(f"/identity/invitations/{membership}/revoke", headers=headers).status_code
        == 200
    )
    for issued in (first, second):
        with store.connect() as db, pytest.raises(PermissionError):
            access.principal(db, issued.json()["token"], store.clock())
    assert redeem(claims, membership_id=membership).status_code == 403


def test_expiry_and_simultaneous_redemption(tmp_path, verifier):
    _, _, claims = verifier
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    nid = store.create_network(seed()["resources"])
    coordinator = access.issue(store, nid, "coordinator")
    invitation = identity.invite(
        store, coordinator["token"], nid, claims["email"], "driver", "tunde"
    )

    def redeem(_):
        try:
            return identity.session(store, claims, invitation_token=invitation["invitation_token"])
        except PermissionError:
            return None

    with ThreadPoolExecutor(2) as pool:
        assert sum(r is not None for r in pool.map(redeem, range(2))) == 1
    expired = identity.invite(store, coordinator["token"], nid, claims["email"], "donor")
    with store.connect() as db:
        db.execute("UPDATE invitations SET expires=0 WHERE id=?", (expired["invitation_id"],))
    with pytest.raises(PermissionError):
        identity.session(store, claims, invitation_token=expired["invitation_token"])


def test_unconfigured_identity_fails_closed(tmp_path):
    client = TestClient(create_app(str(tmp_path / "disabled.db")))
    assert (
        client.post("/identity/session", json={"id_token": "x", "membership_id": "y"}).status_code
        == 503
    )
