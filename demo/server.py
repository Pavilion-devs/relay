"""Session-isolated public operations sandbox with metered live interpretation."""

import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from relay_core import access
from relay_core.api import Command
from relay_core.engine import event, seed
from relay_core.store import Store

HERE = Path(__file__).parent
API = os.environ.get("RELAY_DEMO_API", "http://api:8765")
DB = os.environ.get("RELAY_DB", ".data/relay.sqlite3")
sessions = {}
mutex = threading.Lock()
rate = {}
MAX_RUNS = 300
TTL = 2700


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("RELAY_DEMO_MANAGED") == "1":
        from relay_core.operations import lock, volume

        volume()
        with lock():
            yield
    else:
        yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def boundary(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin", "")
        host = request.headers.get("host", "")
        if origin not in ("https://" + host, "http://" + host):
            return JSONResponse({"detail": "Open the demo directly to continue."}, status_code=403)
        if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
            return JSONResponse({"detail": "Cross-site actions are disabled."}, status_code=403)
        body = b""
        async for part in request.stream():
            body += part
            if len(body) > 4096:
                return JSONResponse({"detail": "Request too large."}, status_code=413)
        request._body = body
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


class Sandbox:
    def __init__(self):
        self.store = Store(DB)
        with self.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_demo_budget (id INTEGER PRIMARY KEY, runs INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO public_demo_budget VALUES (1,0)")
            changed = db.execute(
                "UPDATE public_demo_budget SET runs=runs+1 WHERE id=1 AND runs<?", (MAX_RUNS,)
            ).rowcount
            if not changed:
                raise HTTPException(
                    429, "Demo capacity reached. Please watch the walkthrough or ask the presenter."
                )
        self.network = self.store.create_network(seed()["resources"])
        grants = [
            access.issue(self.store, self.network, role, hours=1)
            for role in ("coordinator", "donor")
        ]
        grants += [
            access.issue(self.store, self.network, r["role"], r["id"], hours=1)
            for r in seed()["resources"]
        ]
        self.tokens = {g["resource_id"] or g["role"]: g["token"] for g in grants}
        self.trace = []
        self.expires = time.time() + TTL
        self.lock = threading.Lock()
        self.requests = {}
        self.actions = 0
        self.ai_calls = 0
        self.workspace = self.call("coordinator", "POST", "/workspaces", {})["id"]

    def call(self, actor, method, path, payload=None, expected=200):
        req = UrlRequest(
            API + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + self.tokens[actor],
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(req, timeout=10) as response:
                status, result = response.status, json.load(response)
        except HTTPError as error:
            status, result = error.code, json.load(error)
        if payload is not None:
            self.trace.append(
                {
                    "actor": actor,
                    "action": payload.get("action", "create"),
                    "http_status": status,
                    "code": result.get("code"),
                }
            )
        if status != expected:
            raise HTTPException(
                status,
                {
                    "code": result.get("code", "ENGINE_REJECTED"),
                    "message": result.get("message", "Action rejected."),
                },
            )
        return result

    def read(self):
        return self.call("coordinator", "GET", "/workspaces/" + self.workspace)

    def command(self, actor, action, expected=200, key=None, **fields):
        return self.call(
            actor,
            "POST",
            "/workspaces/" + self.workspace + "/commands",
            {"action": action, "command_id": key or str(uuid4()), **fields},
            expected,
        )

    def public(self):
        state = self.read()
        fields = (
            "name",
            "status",
            "total_kg",
            "crates",
            "kg_per_crate",
            "facts_version",
            "deadline",
            "resources",
            "plan",
            "plans",
            "messages",
            "suggestions",
            "events",
            "replacement",
            "replacement_history",
            "replacement_failure",
            "planning",
            "logistics",
        )
        return {
            **{k: state.get(k) for k in fields},
            "synthetic": True,
            "trace": self.trace[-100:],
            "ai": {
                "provider": "Claude Sonnet 4.6 · Amazon Bedrock",
                "remaining_requests": max(0, 4 - self.ai_calls),
                "enabled": os.environ.get("RELAY_DEMO_AI") == "1",
            },
        }


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/style.css")
def style():
    return FileResponse(HERE / "style.css", media_type="text/css")


@app.get("/app.js")
def javascript():
    return FileResponse(HERE / "app.js", media_type="application/javascript")


@app.get("/health")
def health():
    return {"ok": True, "mode": "synthetic-public-demo"}


def get_session(request):
    sid = request.cookies.get("relay_demo", "")
    with mutex:
        session = sessions.get(sid)
    if not session or session.expires < time.time():
        raise HTTPException(401, "Start a fresh demo to continue.")
    return session


@app.post("/demo/start")
def start(request: Request):
    # CF-Connecting-IP is supplied by the sole public-facing Cloudflare tunnel.
    ip = request.headers.get("cf-connecting-ip", request.client.host)
    now = time.time()
    with mutex:
        for sid in list(sessions):
            if sessions[sid].expires < now:
                del sessions[sid]
        for address in list(rate):
            if now - rate[address][-1] > 60:
                del rate[address]
        recent = [t for t in rate.get(ip, []) if now - t < 60]
        if len(recent) >= 3 or len(sessions) >= 100:
            raise HTTPException(429, "Please wait a minute before starting another rescue.")
        rate[ip] = recent + [now]
        old = request.cookies.get("relay_demo", "")
        if old in sessions:
            return JSONResponse(sessions[old].public())
        session = Sandbox()
        sid = secrets.token_urlsafe(32)
        sessions[sid] = session
    response = JSONResponse(session.public())
    response.set_cookie(
        "relay_demo",
        sid,
        httponly=True,
        secure=os.environ.get("RELAY_DEMO_SECURE_COOKIE", "1") == "1",
        samesite="lax",
        max_age=TTL,
        path="/",
    )
    return response


@app.get("/demo/state")
def state(request: Request):
    session = get_session(request)
    with session.lock:
        return session.public()


ALLOWED = {
    "coordinator": {
        "propose",
        "approve",
        "change",
        "apply_suggestion",
        "cancel",
        "recover",
        "propose_replacement",
        "commit_replacement",
    },
    "driver": {"accept", "pickup", "ack_cancel", "ack_stop", "accept_replacement"},
    "recipient": {"accept", "receive", "ack_cancel", "accept_replacement"},
    "donor": {"ack_return", "confirm_return"},
}


class Action(Command):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(max_length=32)


@app.post("/demo/action")
def action(request: Request, incoming: Action):
    session = get_session(request)
    with session.lock:
        if session.actions >= 500:
            raise HTTPException(429, "Session action limit reached. Start a new rescue.")
        session.actions += 1
        resources = {r["id"]: r["role"] for r in session.read()["resources"]}
        role = resources.get(incoming.actor, incoming.actor)
        if incoming.actor not in session.tokens or incoming.action not in ALLOWED.get(role, set()):
            raise HTTPException(403, "That role cannot perform this action.")
        fields = incoming.model_dump(exclude_none=True, exclude={"actor", "action", "command_id"})
        # Upstream authorization also checks resource and role inside the transaction.
        result = session.command(incoming.actor, incoming.action, key=incoming.command_id, **fields)
        return {"message": result.get("message", "Action recorded."), "state": session.public()}


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_id: str = Field(max_length=32)
    text: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


@app.post("/demo/interpret")
def message(request: Request, incoming: Message):
    session = get_session(request)
    with session.lock:
        if not os.environ.get("RELAY_DEMO_AI") == "1":
            raise HTTPException(503, "Live interpretation is temporarily unavailable.")
        payload = (incoming.resource_id, incoming.text)
        old = session.requests.get(incoming.request_id)
        if old:
            if old["payload"] != payload:
                raise HTTPException(409, "Request ID already belongs to another message.")
            if old["status"] != 200:
                raise HTTPException(old["status"], old["message"])
            return {"message": old["message"], "metrics": old["metrics"], "state": session.public()}
        if incoming.resource_id not in {r["id"] for r in session.read()["resources"]}:
            raise HTTPException(422, "Choose a participant in this rescue.")
        if session.ai_calls >= 4:
            raise HTTPException(
                429,
                "This rescue has used its four live AI requests. All other controls remain available.",
            )
        session.ai_calls += 1
        saved = {
            "payload": payload,
            "status": 502,
            "message": "Interpretation failed. No facts were applied.",
        }
        session.requests[incoming.request_id] = saved
        scoped = access.ScopedStore(
            session.store, session.tokens[incoming.resource_id], incoming.resource_id
        )
        mid = str(uuid4())

        def record(state):
            state["messages"].append(
                {
                    "id": mid,
                    "resource_id": incoming.resource_id,
                    "text": incoming.text,
                    "direction": "inbound",
                    "via": "participant",
                }
            )
            event(state, "Participant message received", incoming.text)
            return {"ok": True}

        scoped.transact(session.workspace, mid, incoming.model_dump(), record)
        try:
            reply, metrics = run_interpretation(session.store, scoped, session.workspace, mid)
        except HTTPException as exc:
            saved.update(status=exc.status_code, message=exc.detail)
            raise
        except Exception:  # noqa: BLE001 - sanitize model provider failures
            raise HTTPException(502, saved["message"]) from None
        saved.update(status=200, message=reply, metrics=metrics)
        return {"message": reply, "metrics": metrics, "state": session.public()}


def run_interpretation(store, scoped, workspace, mid):
    from demo.demo_ai import run

    return run(store, scoped, workspace, mid)


@app.post("/demo/reset")
def reset(request: Request):
    session = get_session(request)
    with mutex:
        session.expires = 0
    return start(request)


@app.exception_handler(Exception)
async def failed(request, exc):
    return JSONResponse(
        {
            "detail": "The demo could not complete this step. Please refresh or contact the presenter."
        },
        status_code=503,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8780,
        access_log=False,
        proxy_headers=False,
        limit_concurrency=24,
        timeout_keep_alive=5,
    )
