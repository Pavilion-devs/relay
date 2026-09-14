import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from . import access, email_actions, email_delivery, identity
from .engine import apply, event, resource, seed
from .login import HostedLogin
from .planner import Logistics
from .store import Store


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    command_id: str = Field(default_factory=lambda: str(uuid4()), max_length=100)
    resource_id: str | None = None
    revision: int | None = None
    replacement_id: str | None = None
    replacement_request_id: str | None = None
    capacity: int | None = Field(default=None, strict=True)
    available: bool | None = Field(default=None, strict=True)
    conditional: bool | None = Field(default=None, strict=True)
    received_kg: int | None = Field(default=None, strict=True)
    source: str = Field(default="Structured update in rehearsal", max_length=2000)
    suggestion_id: str | None = None
    logistics: Logistics | None = None
    reason: str | None = Field(default=None, max_length=2000)
    evidence: str | None = Field(default=None, max_length=2000)


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    network_id: str | None = None


class Message(BaseModel):
    resource_id: str
    text: str = Field(min_length=1, max_length=2000)


class InvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    network_id: str
    email: str = Field(max_length=320)
    role: str
    resource_id: str | None = None
    hours: int = Field(default=24, ge=1, le=72)


class IdentitySession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id_token: str = Field(min_length=1, max_length=16000)
    invitation_token: str | None = Field(default=None, max_length=200)
    membership_id: str | None = Field(default=None, max_length=100)


class EmailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    received_kg: int | None = Field(default=None, strict=True, ge=0, le=10000)
    evidence: str | None = Field(default=None, max_length=2000)


class EmailConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)


class LoginStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invitation_token: str | None = Field(default=None, max_length=200)
    membership_id: str | None = Field(default=None, max_length=100)


def create_app(path=None, rehearsal=False, identity_verifier=None, login_config=None):
    managed = os.environ.get("RELAY_MANAGED") == "1"
    if managed:
        from .operations import volume
        database = volume()
        if rehearsal or Path(path or os.environ.get("RELAY_DB", "")) != database:
            raise ValueError("Managed API requires authenticated mode and the mounted database")
    app = FastAPI(title="Relay recovery rehearsal", version="0.1.0")
    store = Store(path or os.environ.get("RELAY_DB", ".data/relay.sqlite3"))
    app.state.store = store
    if identity_verifier is None and os.environ.get("RELAY_COGNITO_ISSUER"):
        identity_verifier = identity.CognitoVerifier(
            os.environ["RELAY_COGNITO_ISSUER"], os.environ.get("RELAY_COGNITO_CLIENT_ID")
        )
    hosted = None
    if login_config is None and os.environ.get("RELAY_COGNITO_DOMAIN"):
        login_config = {
            "domain": os.environ["RELAY_COGNITO_DOMAIN"],
            "callback": os.environ["RELAY_LOGIN_CALLBACK"],
        }
    if login_config:
        if identity_verifier is None:
            raise ValueError("Hosted login requires a configured identity verifier")
        hosted = HostedLogin(store, identity_verifier, **login_config)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @app.middleware("http")
    async def local_write_guard(request: Request, call_next):
        if managed and request.url.path not in ("/health", "/ready"):
            try:
                volume()
            except (OSError, RuntimeError):
                return JSONResponse({"detail": "Storage unavailable or quarantined"}, status_code=503)
        origin = request.headers.get("origin")
        if (
            request.method == "POST"
            and origin
            and origin
            not in ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8765")
        ):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if (
            not rehearsal
            and request.url.path
            not in (
                "/health",
                "/ready",
                "/docs",
                "/openapi.json",
                "/redoc",
                "/identity/session",
                "/identity/login",
                "/identity/callback",
                "/identity/verify-email",
            )
            and not (request.method == "GET" and request.url.path.startswith("/reminders/"))
            and request.method != "OPTIONS"
        ):
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer "):
                return JSONResponse({"detail": "Bearer access grant required"}, status_code=401)
            request.state.token = header[7:]
            try:
                with store.connect() as db:
                    request.state.actor = access.principal(db, request.state.token, store.clock())
            except PermissionError:
                return JSONResponse(
                    {"detail": "Access grant is invalid or expired"}, status_code=401
                )
        return await call_next(request)

    @app.exception_handler(PermissionError)
    async def forbidden(request, exc):
        return JSONResponse({"detail": "Access denied for this operation"}, status_code=403)

    def check(request, state, action, rid=None):
        if rehearsal:
            return None
        with store.connect() as db:
            return access.authorize(db, request.state.token, store.clock(), state, action, rid)

    def visible(state, actor):
        return access.project(state, actor) if actor else state

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "Workspace not found"}, status_code=404)

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "mode": "local_rehearsal" if rehearsal else "scoped_local_api",
            "model_configured": bool(os.environ.get("RELAY_MODEL_ID")),
            "model_id": os.environ.get("RELAY_MODEL_ID"),
            "live_inference_verified": getattr(app.state, "live_inference_verified", False),
        }

    @app.get("/ready")
    def ready():
        if not managed:
            return JSONResponse({"ok": False, "mode": "unmanaged"}, status_code=503)
        from .operations import readiness
        result = readiness()
        return JSONResponse(result, status_code=200 if result["ok"] else 503)

    @app.get("/reminders/{message_id}")
    def open_reminder(message_id: str):
        if hosted is None:
            raise HTTPException(503, "Hosted login is not configured")
        with store.connect() as db:
            row = db.execute(
                "SELECT e.membership FROM email_outbox e JOIN invitations i ON i.id=e.membership WHERE e.id=? AND i.revoked=0",
                (message_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Reminder unavailable")
        url, cookie = hosted.start(membership_id=row[0], reminder_id=message_id)
        response = RedirectResponse(
            url,
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.set_cookie(
            "relay_login",
            cookie,
            max_age=600,
            httponly=True,
            secure=hosted.secure_cookie,
            samesite="lax",
            path="/identity/callback",
        )
        return response

    @app.post("/identity/login")
    def start_login(incoming: LoginStart):
        if hosted is None:
            raise HTTPException(503, "Hosted login is not configured")
        try:
            url, browser = hosted.start(incoming.invitation_token, incoming.membership_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            "relay_login",
            browser,
            max_age=600,
            httponly=True,
            secure=hosted.secure_cookie,
            samesite="lax",
            path="/identity/callback",
        )
        return response

    @app.get("/identity/callback")
    def finish_login(request: Request):
        if hosted is None:
            raise HTTPException(503, "Hosted login is not configured")
        params = request.query_params
        if any(len(params.getlist(k)) != 1 for k in ("state", "code")) or "error" in params:
            raise HTTPException(400, "Sign-in failed; start again")
        state, code = params["state"], params["code"]
        if len(state) > 200 or len(code) > 8192:
            raise HTTPException(400, "Invalid sign-in callback")
        result = hosted.finish(state, request.cookies.get("relay_login", ""), code)
        if result.get("email_verification_required"):
            ticket = result["ticket"]
            page = """<!doctype html><meta charset="utf-8"><title>Verify Relay email</title>
<h1>Verify your email</h1><p>Cognito sent a verification code to your email. Enter it here to finish signing in.</p>
<form id="verify"><input id="code" aria-label="Email verification code" autocomplete="one-time-code" required maxlength="20"><button>Verify email</button></form><p id="status"></p>
<script>document.getElementById('verify').onsubmit=async(e)=>{e.preventDefault();
const r=await fetch('/identity/verify-email',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Verification':'TICKET'},body:JSON.stringify({code:document.getElementById('code').value})});
const result=await r.json();document.getElementById('status').textContent=r.ok?'Email verified. Relay sign-in completed. You can return to the conversation.':result.detail;
if(r.ok){if(result.review_html){document.open();document.write(result.review_html);document.close();}else{document.getElementById('verify').remove();}}};</script>""".replace(
                "TICKET", ticket
            )
            response = HTMLResponse(
                page,
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Frame-Options": "DENY",
                },
            )
            response.set_cookie(
                "relay_verify",
                ticket,
                max_age=600,
                httponly=True,
                secure=hosted.secure_cookie,
                samesite="strict",
                path="/identity/verify-email",
            )
        elif result.get("reminder_id"):
            response = HTMLResponse(
                email_actions.review_page(store, result),
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Frame-Options": "DENY",
                },
            )
        else:
            response = JSONResponse(
                result, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            )
        response.delete_cookie(
            "relay_login",
            path="/identity/callback",
            secure=hosted.secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/identity/verify-email")
    async def verify_email(request: Request):
        if hosted is None:
            raise HTTPException(503, "Hosted login is not configured")
        ticket = request.cookies.get("relay_verify", "")
        import secrets

        if not ticket or not secrets.compare_digest(
            ticket, request.headers.get("x-relay-verification", "")
        ):
            raise PermissionError()
        incoming = await request.json()
        code = incoming.get("code") if isinstance(incoming, dict) else None
        if not isinstance(code, str) or not code.isdigit() or not 1 <= len(code) <= 20:
            raise HTTPException(422, "Enter the code from your email")
        result = hosted.verify_email(ticket, code)
        if result.get("reminder_id"):
            result = {"review_html": email_actions.review_page(store, result)}
        response = JSONResponse(result, headers={"Cache-Control": "no-store"})
        response.delete_cookie("relay_verify", path="/identity/verify-email")
        return response

    @app.post("/identity/session")
    def identity_session(incoming: IdentitySession):
        if identity_verifier is None:
            raise HTTPException(503, "Identity provider is not configured")
        if bool(incoming.invitation_token) == bool(incoming.membership_id):
            raise HTTPException(422, "Provide an invitation token or membership ID")
        claims = identity_verifier.verify(incoming.id_token)
        result = identity.session(store, claims, incoming.invitation_token, incoming.membership_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/identity/invitations")
    def invitation(incoming: InvitationCreate, request: Request):
        if rehearsal:
            raise PermissionError()
        try:
            result = identity.invite(
                store,
                request.state.token,
                incoming.network_id,
                incoming.email,
                incoming.role,
                incoming.resource_id,
                incoming.hours,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/identity/invitations/{invitation_id}/revoke")
    def revoke_invitation(invitation_id: str, request: Request):
        if rehearsal:
            raise PermissionError()
        return identity.revoke(store, request.state.token, invitation_id)

    @app.get("/identity/email-consent")
    def get_email_consent(request: Request):
        if rehearsal:
            raise PermissionError()
        return email_delivery.consent(store, request.state.token)

    @app.post("/identity/email-consent")
    def set_email_consent(incoming: EmailConsent, request: Request):
        if rehearsal:
            raise PermissionError()
        return email_delivery.consent(store, request.state.token, incoming.enabled)

    @app.get("/networks/{network}/email-delivery")
    def email_status(network: str, request: Request):
        if rehearsal:
            raise PermissionError()
        return email_delivery.status_for_network(store, request.state.token, network)

    @app.post("/email-actions/{message_id}/confirm")
    def confirm_email(message_id: str, request: Request, incoming: EmailResponse | None = None):
        if rehearsal:
            raise PermissionError()
        try:
            result = email_actions.confirm(
                store,
                request.state.token,
                message_id,
                incoming.evidence if incoming else None,
                incoming.received_kg if incoming else None,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # The store response contains coordinator-only state; never expose it here.
        return JSONResponse(
            {k: v for k, v in result.items() if k != "state"},
            status_code=200 if result["ok"] else 409,
        )

    @app.post("/workspaces")
    def create(request: Request, incoming: WorkspaceCreate | None = None):
        network = incoming.network_id if incoming else None
        if not rehearsal:
            actor = request.state.actor
            if actor["role"] != "coordinator" or (network and network != actor["network_id"]):
                raise PermissionError()
            network = actor["network_id"]

        def guard(db, state):
            return access.authorize(db, request.state.token, store.clock(), state, "propose")

        return store.create(seed(), network_id=network, guard=None if rehearsal else guard)

    @app.post("/networks")
    def create_network():
        if not rehearsal:
            raise PermissionError("Provision networks through the local administrator CLI")
        return {"id": store.create_network(seed()["resources"])}

    @app.get("/networks/{network}/reservations")
    def reservations(network: str, request: Request):
        if not rehearsal and (
            request.state.actor["role"] != "coordinator"
            or request.state.actor["network_id"] != network
        ):
            raise PermissionError()
        return store.reservations(network)

    @app.get("/workspaces/{workspace}/inbox")
    def inbox(workspace: str, request: Request):
        actor = check(request, store.read(workspace), "inbox")
        messages = store.inbox(workspace)
        return (
            messages
            if not actor or actor["role"] == "coordinator"
            else [m for m in messages if m["recipient"] == (actor["resource_id"] or actor["role"])]
        )

    @app.get("/workspaces/{workspace}")
    def read(workspace: str, request: Request):
        state = store.read(workspace)
        return visible(state, check(request, state, "read"))

    @app.post("/workspaces/{workspace}/commands")
    def command(workspace: str, command: Command, request: Request):
        payload = command.model_dump(mode="json", exclude_none=True, exclude={"command_id"})
        actor = check(request, store.read(workspace), command.action, command.resource_id)
        if actor:
            payload["actor_id"] = actor["id"]

        def guard(db, state):
            return access.authorize(
                db, request.state.token, store.clock(), state, command.action, command.resource_id
            )

        result = store.transact(
            workspace,
            command.command_id,
            payload,
            lambda s: apply(s, payload),
            guard=guard if actor else None,
        )
        if "state" in result:
            result["state"] = visible(result["state"], actor)
        return JSONResponse(result, status_code=200 if result["ok"] else 409)

    @app.post("/workspaces/{workspace}/messages")
    def message(workspace: str, incoming: Message, request: Request):
        actor = check(request, store.read(workspace), "messages", incoming.resource_id)
        message_store = (
            access.ScopedStore(store, request.state.token, incoming.resource_id) if actor else store
        )
        if not os.environ.get("RELAY_MODEL_ID"):
            raise HTTPException(
                503,
                "Connect AWS and select a Bedrock model to interpret messages. Structured rehearsal controls remain available.",
            )
        if not resource(store.read(workspace), incoming.resource_id):
            raise HTTPException(422, "Unknown participant")
        mid = str(uuid4())

        def record(s):
            s["messages"].append(
                {
                    "id": mid,
                    "resource_id": incoming.resource_id,
                    "text": incoming.text,
                    "direction": "inbound",
                    "via": "participant",
                }
            )
            event(s, "Participant message received", incoming.text)
            return {"ok": True}

        message_store.transact(workspace, mid, incoming.model_dump(), record)
        from .agent import interpret

        try:
            reply = interpret(message_store, workspace, mid)
            app.state.live_inference_verified = True
        except Exception:  # noqa: BLE001 - sanitize provider errors at the HTTP boundary
            # Provider exceptions can contain account or request details; don't expose them to the client.
            raise HTTPException(
                502,
                "Model interpretation failed. Your message is stored; no resource change was applied.",
            ) from None
        return {"reply": reply, "state": visible(message_store.read(workspace), actor)}

    return app


app = create_app()
