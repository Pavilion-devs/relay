import io
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from demo import server
from relay_core.api import create_app


@pytest.fixture
def demo(monkeypatch, tmp_path):
    database = str(tmp_path / "demo.sqlite3")
    engine = TestClient(create_app(database))
    monkeypatch.setattr(server, "DB", database)
    monkeypatch.setattr(server, "sessions", {})
    monkeypatch.setattr(server, "rate", {})
    monkeypatch.setenv("RELAY_DEMO_SECURE_COOKIE", "0")

    def engine_http(request, timeout=10):
        response = engine.request(
            request.method,
            urlsplit(request.full_url).path,
            headers=dict(request.header_items()),
            content=request.data,
        )
        stream = io.BytesIO(response.content)
        stream.status = response.status_code
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url, response.status_code, "Expected engine rejection", {}, stream
            )
        return stream

    monkeypatch.setattr(server, "urlopen", engine_http)
    with TestClient(server.app, headers={"Origin": "http://testserver"}) as client:
        yield client
    engine.close()


def act(client, actor, action, **fields):
    return client.post("/demo/action", json={"actor": actor, "action": action, **fields})


def committed(client):
    assert client.post("/demo/start", json={}).json()["plan"] is None
    result = act(client, "coordinator", "propose")
    assert result.status_code == 200
    plan = result.json()["state"]["plan"]
    for actor in plan["required"]:
        assert act(client, actor, "accept", resource_id=actor, revision=1).status_code == 200
    assert act(client, "coordinator", "approve", revision=1).status_code == 200
    return plan


def test_dashboard_full_recovery_and_session_isolation(demo):
    original = committed(demo)
    with TestClient(server.app, headers={"Origin": "http://testserver"}) as other:
        assert other.get("/demo/state").status_code == 401
        assert other.post("/demo/start", json={}).status_code == 200
        assert act(demo, "tunde", "pickup", resource_id="tunde", revision=1).status_code == 200
        proposed = act(
            demo,
            "coordinator",
            "propose_replacement",
            resource_id="tunde",
            replacement_id="ada",
            revision=1,
            available=True,
            reason="Synthetic vehicle failure",
        )
        assert proposed.status_code == 200
        replacement = proposed.json()["state"]["replacement"]
        ref = {"revision": 1, "replacement_request_id": replacement["id"]}
        assert act(demo, "tunde", "ack_stop", resource_id="tunde", **ref).status_code == 200
        for actor in replacement["required"]:
            assert (
                act(demo, actor, "accept_replacement", resource_id=actor, **ref).status_code == 200
            )
        blocked = act(demo, "coordinator", "commit_replacement", **ref)
        assert (
            blocked.status_code == 409 and blocked.json()["detail"]["code"] == "CUSTODY_UNRESOLVED"
        )
        assert act(demo, "amara", "pickup", resource_id="amara", revision=1).status_code == 200
        assert (
            act(
                demo,
                "donor",
                "confirm_return",
                resource_id="tunde",
                evidence="Synthetic donor return",
                **ref,
            ).status_code
            == 200
        )
        result = act(demo, "coordinator", "commit_replacement", command_id="same-commit", **ref)
        assert result.status_code == 200
        plan = result.json()["state"]["plan"]
        assert plan["revision"] == 2 and plan["picked_up"] == ["amara"]
        assert next(r for r in plan["routes"] if r["driver"] == "amara") == next(
            r for r in original["routes"] if r["driver"] == "amara"
        )
        assert (
            act(
                demo, "coordinator", "commit_replacement", command_id="same-commit", **ref
            ).status_code
            == 200
        )
        assert act(demo, "ada", "pickup", resource_id="ada", revision=1).status_code == 409
        assert act(demo, "ada", "pickup", resource_id="ada", revision=2).status_code == 200
        final = act(
            demo, "harbour", "receive", resource_id="harbour", revision=2, received_kg=320
        ).json()["state"]
        assert final["status"] == "complete"
        assert other.get("/demo/state").json()["plan"] is None
        assert "token" not in json.dumps(final)


def test_public_boundary_role_checks_and_validation(demo):
    assert (
        demo.post("/demo/start", json={}, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert demo.post("/demo/start", content=b"x" * 4097).status_code == 413
    committed(demo)
    assert act(demo, "coordinator", "send_email").status_code == 403
    assert act(demo, "tunde", "approve", revision=1).status_code == 403
    assert act(demo, "tunde", "pickup", resource_id="amara", revision=1).status_code == 403
    assert act(demo, "unknown", "pickup").status_code == 403
    assert act(demo, "coordinator", "propose", network_id="external").status_code == 422
    assert demo.post("/workspaces", json={}).status_code == 404
    assert demo.get("/.env").status_code == 404
    assert demo.get("/openapi.json").status_code == 404


def test_public_run_budget_is_durable_and_cookie_is_private(demo, monkeypatch):
    monkeypatch.setattr(server, "MAX_RUNS", 1)
    response = demo.post("/demo/start", json={})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    monkeypatch.setattr(server, "sessions", {})
    demo.cookies.clear()
    assert demo.post("/demo/start", json={}).status_code == 429


def test_expired_demo_cannot_act_and_reset_is_fresh(demo):
    committed(demo)
    assert demo.post("/demo/reset", json={}).json()["plan"] is None
    for session in server.sessions.values():
        session.expires = 0
    assert act(demo, "coordinator", "propose").status_code == 401


def test_ai_draft_requires_review_and_duplicate_request_does_not_call_again(demo, monkeypatch):
    from test_extraction import JsonModel

    from relay_core.agent import interpret

    monkeypatch.setenv("RELAY_DEMO_AI", "1")
    calls = []

    def run(store, scoped, wid, mid):
        calls.append(mid)
        text = scoped.read(wid)["messages"][-1]["text"]
        reply = interpret(
            scoped,
            wid,
            mid,
            model=JsonModel(
                json.dumps({"decision": "draft", "quantity_text": "160 kg", "evidence_quote": text})
            ),
        )
        return reply, {"model_calls": 1, "latency_seconds": 0.1}

    monkeypatch.setattr(server, "run_interpretation", run)
    demo.post("/demo/start", json={})
    data = {"resource_id": "amara", "text": "I can carry 160 kg.", "request_id": "unique-message-1"}
    result = demo.post("/demo/interpret", json=data)
    assert result.status_code == 200, result.text
    state = result.json()["state"]
    assert next(r for r in state["resources"] if r["id"] == "amara")["capacity"] == 128
    suggestion = state["suggestions"][0]
    assert suggestion["capacity"] == 160 and not suggestion["applied"]
    assert demo.post("/demo/interpret", json=data).status_code == 200 and len(calls) == 1
    assert (
        demo.post("/demo/interpret", json={**data, "text": "I can carry 200 kg."}).status_code
        == 409
    )
    applied = act(
        demo, "coordinator", "apply_suggestion", resource_id="amara", suggestion_id=suggestion["id"]
    )
    assert applied.status_code == 200
    assert (
        next(r for r in applied.json()["state"]["resources"] if r["id"] == "amara")["capacity"]
        == 160
    )
    assert (
        demo.post(
            "/demo/interpret",
            json={**data, "request_id": "unknown-person", "resource_id": "outside"},
        ).status_code
        == 422
    )


def test_ai_provider_failure_does_not_retry_same_message(demo, monkeypatch):
    monkeypatch.setenv("RELAY_DEMO_AI", "1")
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError("secret provider details")

    monkeypatch.setattr(server, "run_interpretation", fail)
    demo.post("/demo/start", json={})
    data = {"resource_id": "amara", "text": "I can carry 160 kg.", "request_id": "failed-message-1"}
    for _ in range(2):
        response = demo.post("/demo/interpret", json=data)
        assert response.status_code == 502 and "secret" not in response.text
    assert len(calls) == 1
    assert demo.get("/demo/state").json()["suggestions"] == []


def test_metered_ai_reserves_before_call_and_never_exceeds_shared_cap(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import HTTPException

    from demo import demo_ai
    from relay_core.store import Store

    store = Store(str(tmp_path / "budget.sqlite3"))
    monkeypatch.setattr(demo_ai, "CAP_MICRO_USD", 100)

    def attempt(_):
        try:
            demo_ai.reserve(store, 30, 1)
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert sum(outcomes) == 3
    with store.connect() as db:
        assert tuple(db.execute("SELECT reserved,calls FROM public_ai_budget").fetchone()) == (
            90,
            3,
        )

    class Provider:
        calls = 0

        def count_tokens(self, **kwargs):
            return {"inputTokens": 200}

        def converse(self, **kwargs):
            self.calls += 1
            return {}

    provider = Provider()
    wrapped = demo_ai.CountedClient(provider, store)
    with pytest.raises(HTTPException):
        wrapped.converse(modelId=demo_ai.MODEL, messages=[], inferenceConfig={"maxTokens": 1000})
    assert provider.calls == 0
