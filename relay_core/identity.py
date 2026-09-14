"""Cognito identity verification and locally administered participant memberships."""

import hashlib
import json
import re
import secrets
from uuid import uuid4

import jwt

from . import access


class CognitoVerifier:
    def __init__(self, issuer, client_id):
        if not re.fullmatch(
            r"https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9-]+_[A-Za-z0-9]+", issuer
        ):
            raise ValueError("Expected a Cognito user-pool issuer")
        if not client_id:
            raise ValueError("Cognito client ID is required")
        self.issuer, self.client_id = issuer, client_id
        self.keys = jwt.PyJWKClient(issuer + "/.well-known/jwks.json", timeout=5)

    def verify(self, token, *, require_verified=True):
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.client_id,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "iss",
                        "aud",
                        "sub",
                        "token_use",
                        "email",
                    ]
                },
            )
            if claims["token_use"] != "id" or (
                require_verified and claims.get("email_verified") is not True
            ):
                raise ValueError("Verified email ID token required")
            if not isinstance(claims["email"], str) or not claims["email"] or not claims["sub"]:
                raise ValueError("Missing identity")
            return claims
        except (jwt.PyJWTError, ValueError) as exc:
            raise PermissionError("Identity could not be verified") from exc


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS invitations (
        id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, network TEXT NOT NULL,
        role TEXT NOT NULL, resource TEXT, email TEXT NOT NULL, expires REAL NOT NULL,
        issuer TEXT, subject TEXT, revoked INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS identity_sessions (
        grant_id TEXT PRIMARY KEY, membership_id TEXT NOT NULL
    )""")


def coordinator(db, store, token, network):
    actor = access.principal(db, token, store.clock())
    if actor["role"] != "coordinator" or actor["network_id"] != network:
        raise PermissionError("Network coordinator required")
    return actor


def invite(store, token, network, email, role, resource=None, hours=24):
    if role not in ("driver", "recipient", "donor") or not 0 < hours <= 72:
        raise ValueError("Invalid participant role or lifetime")
    if not email or email != email.strip() or "@" not in email or len(email) > 320:
        raise ValueError("An exact email address is required")
    secret, iid = secrets.token_urlsafe(32), str(uuid4())
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        actor = coordinator(db, store, token, network)
        resources = json.loads(
            db.execute("SELECT resources FROM networks WHERE id=?", (network,)).fetchone()[0]
        )
        if role != "donor" and (resource not in resources or resources[resource]["role"] != role):
            raise ValueError("Participant must match the registry")
        if role == "donor":
            resource = None
        db.execute(
            "INSERT INTO invitations VALUES(?,?,?,?,?,?,?,NULL,NULL,0,?)",
            (
                iid,
                hashlib.sha256(secret.encode()).hexdigest(),
                network,
                role,
                resource,
                email,
                store.clock() + hours * 3600,
                actor["id"],
            ),
        )
    return {"invitation_id": iid, "invitation_token": secret}


def session(store, claims, invitation_token=None, membership_id=None):
    """Claims must come exclusively from the configured verifier, never request JSON."""
    now = store.clock()
    if claims["exp"] <= now:
        raise PermissionError("Identity expired")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if invitation_token:
            row = db.execute(
                "SELECT id,network,role,resource,email,expires,issuer,subject,revoked FROM invitations WHERE digest=?",
                (hashlib.sha256(invitation_token.encode()).hexdigest(),),
            ).fetchone()
            if (
                not row
                or row[8]
                or row[5] <= now
                or row[7] is not None
                or row[4] != claims["email"]
            ):
                raise PermissionError("Invitation cannot be redeemed")
            db.execute(
                "UPDATE invitations SET issuer=?,subject=? WHERE id=?",
                (claims["iss"], claims["sub"], row[0]),
            )
        else:
            row = db.execute(
                "SELECT id,network,role,resource,email,expires,issuer,subject,revoked FROM invitations WHERE id=?",
                (membership_id,),
            ).fetchone()
            if not row or row[8] or (row[6], row[7]) != (claims["iss"], claims["sub"]):
                raise PermissionError("Membership unavailable")
        secret, gid = secrets.token_urlsafe(32), str(uuid4())
        expires = min(now + 900, claims["exp"])
        db.execute(
            "INSERT INTO access_grants VALUES(?,?,?,?,?,?,0)",
            (gid, hashlib.sha256(secret.encode()).hexdigest(), row[1], row[2], row[3], expires),
        )
        db.execute("INSERT INTO identity_sessions VALUES(?,?)", (gid, row[0]))
    return {
        "token": secret,
        "expires_at": expires,
        "membership_id": row[0],
        "role": row[2],
        "resource_id": row[3],
        "network_id": row[1],
    }


def revoke(store, token, iid):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT network FROM invitations WHERE id=?", (iid,)).fetchone()
        if not row:
            raise PermissionError("Invitation unavailable")
        coordinator(db, store, token, row[0])
        db.execute("UPDATE invitations SET revoked=1 WHERE id=?", (iid,))
        db.execute(
            "UPDATE access_grants SET revoked=1 WHERE id IN (SELECT grant_id FROM identity_sessions WHERE membership_id=?)",
            (iid,),
        )
    return {"revoked": True}
