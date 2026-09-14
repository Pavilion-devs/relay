"""Server-owned Cognito authorization-code flow with browser-bound single-use state."""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import identity


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class HostedLogin:
    def __init__(self, store, verifier, domain, callback, exchange=None):
        if not re.fullmatch(r"https://[a-z0-9-]+\.auth\.[a-z0-9-]+\.amazoncognito\.com", domain):
            raise ValueError("Expected a Cognito managed domain")
        url = urlsplit(callback)
        if (
            (
                url.scheme != "https"
                and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost"))
            )
            or url.path != "/identity/callback"
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise ValueError("Expected a fixed HTTPS callback or local loopback callback")
        self.store, self.verifier = store, verifier
        self.domain, self.callback = domain, callback
        self.secure_cookie = url.scheme == "https"
        self.exchange = exchange or self.exchange_code
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
                state TEXT PRIMARY KEY, browser TEXT NOT NULL, verifier TEXT NOT NULL,
                nonce TEXT NOT NULL, target TEXT NOT NULL, expires REAL NOT NULL
            )""")

    def start(self, invitation_token=None, membership_id=None, reminder_id=None):
        if bool(invitation_token) == bool(membership_id):
            raise ValueError("Provide an invitation token or membership ID")
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        with self.store.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE expires<=?", (self.store.clock(),))
            db.execute(
                "INSERT INTO login_attempts VALUES(?,?,?,?,?,?)",
                (
                    digest(state),
                    digest(browser),
                    verifier,
                    nonce,
                    json.dumps(
                        {
                            "invitation_token": invitation_token,
                            "membership_id": membership_id,
                            "reminder_id": reminder_id,
                        }
                    ),
                    self.store.clock() + 600,
                ),
            )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        url = (
            self.domain
            + "/oauth2/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.verifier.client_id,
                    "redirect_uri": self.callback,
                    "scope": "openid email aws.cognito.signin.user.admin",
                    "state": state,
                    "nonce": nonce,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        return url, browser

    def finish(self, state, browser, code):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT browser,verifier,nonce,target,expires FROM login_attempts WHERE state=?",
                (digest(state),),
            ).fetchone()
            if (
                not row
                or not secrets.compare_digest(row[0], digest(browser))
                or row[4] <= self.store.clock()
            ):
                raise PermissionError("Login attempt unavailable")
            # Consume before exchange: timeouts require a new login, never a
            # second exchange with ambiguous provider state.
            db.execute("DELETE FROM login_attempts WHERE state=?", (digest(state),))
        token = self.exchange(code, row[1])
        if isinstance(token, dict):
            claims = self.verifier.verify(token["id_token"], require_verified=False)
        else:
            claims = self.verifier.verify(token)
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(
            claims["nonce"], row[2]
        ):
            raise PermissionError("Login nonce mismatch")
        if isinstance(token, dict) and claims.get("email_verified") is not True:
            return self.begin_verification(token["access_token"], claims, row[3])
        return self.complete(claims, row[3])

    def exchange_code(self, code, verifier):
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "client_id": self.verifier.client_id,
                "redirect_uri": self.callback,
                "code": code,
                "code_verifier": verifier,
            }
        ).encode()
        request = Request(
            self.domain + "/oauth2/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read(65537))
            token = result["id_token"]
            if not isinstance(token, str) or len(token) > 16000:
                raise ValueError("Invalid token")
            access_token = result["access_token"]
            if not isinstance(access_token, str) or len(access_token) > 16000:
                raise ValueError("Invalid access token")
            return {"id_token": token, "access_token": access_token}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PermissionError("Provider exchange failed; restart sign-in") from exc

    def verification_client(self):
        return boto3.client("cognito-idp", region_name=self.verifier.issuer.split(".")[1])

    def begin_verification(self, access_token, claims, target):
        ticket = secrets.token_urlsafe(32)
        try:
            client = self.verification_client()
            user = client.get_user(AccessToken=access_token)
            attributes = {a["Name"]: a["Value"] for a in user["UserAttributes"]}
            if attributes.get("sub") != claims["sub"] or attributes.get("email") != claims["email"]:
                raise PermissionError("Account mismatch")
            client.get_user_attribute_verification_code(
                AccessToken=access_token, AttributeName="email"
            )
        except (BotoCoreError, ClientError) as exc:
            raise PermissionError("Unable to request email verification") from exc
        with self.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS email_verifications (ticket TEXT PRIMARY KEY, access_token TEXT, claims TEXT, target TEXT, expires REAL, attempts INTEGER)"
            )
            db.execute("DELETE FROM email_verifications WHERE expires<=?", (self.store.clock(),))
            db.execute(
                "INSERT INTO email_verifications VALUES(?,?,?,?,?,0)",
                (
                    digest(ticket),
                    access_token,
                    json.dumps(claims),
                    target,
                    min(self.store.clock() + 600, claims["exp"]),
                ),
            )
        return {"email_verification_required": True, "ticket": ticket}

    def verify_email(self, ticket, code):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT access_token,claims,target,expires,attempts FROM email_verifications WHERE ticket=?",
                (digest(ticket),),
            ).fetchone()
            if not row or row[3] <= self.store.clock() or row[4] >= 5:
                raise PermissionError("Verification unavailable; sign in again")
            db.execute(
                "UPDATE email_verifications SET attempts=attempts+1 WHERE ticket=?",
                (digest(ticket),),
            )
        claims = json.loads(row[1])
        try:
            client = self.verification_client()
            client.verify_user_attribute(AccessToken=row[0], AttributeName="email", Code=code)
            user = client.get_user(AccessToken=row[0])
        except (BotoCoreError, ClientError) as exc:
            raise PermissionError("Email verification failed") from exc
        attrs = {a["Name"]: a["Value"] for a in user["UserAttributes"]}
        if (
            attrs.get("sub") != claims["sub"]
            or attrs.get("email") != claims["email"]
            or attrs.get("email_verified") != "true"
        ):
            raise PermissionError("Verified account does not match sign-in")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                "DELETE FROM email_verifications WHERE ticket=?", (digest(ticket),)
            ).rowcount
            if not deleted:
                raise PermissionError("Verification already consumed")
        claims["email_verified"] = True  # authoritative Cognito GetUser, never an admin override
        return self.complete(claims, row[2])

    def complete(self, claims, target):
        values = json.loads(target)
        reminder = values.pop("reminder_id", None)
        result = identity.session(self.store, claims, **values)
        if reminder:
            result["reminder_id"] = reminder
        return result
