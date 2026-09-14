"""Exercise a real Cognito-issued Relay driver session against synthetic rescue facts."""

import json
from pathlib import Path
from uuid import uuid4

import httpx

from relay_core import access
from relay_core.store import Store


def main():
    bundle = json.loads(Path(".data/cognito-participant-rehearsal.json").read_text())
    secret_path = Path(".data/cognito-live-driver-session.json")
    session = json.loads(secret_path.read_text())
    store = Store(".data/relay.sqlite3")
    with store.connect() as db:
        driver = access.principal(db, session["token"], store.clock())
        linked = db.execute(
            "SELECT membership_id FROM identity_sessions WHERE grant_id=?", (driver["id"],)
        ).fetchone()
    assert linked and linked[0] == bundle["invitation_id"]
    assert driver["network_id"] == bundle["network_id"] and driver["resource_id"] == "tunde"
    tokens = {"tunde": session["token"]}
    fixture_grants = []

    def fixture(name, role, resource=None):
        grant = access.issue(store, bundle["network_id"], role, resource)
        tokens[name] = grant["token"]
        fixture_grants.append(grant["id"])

    fixture("coordinator", "coordinator")
    checks = {}
    with httpx.Client(base_url="http://localhost:8765", timeout=20) as client:

        def request(actor, path, body=None, expected=200):
            headers = {"Authorization": "Bearer " + tokens[actor]}
            response = (
                client.get(path, headers=headers)
                if body is None
                else client.post(path, headers=headers, json=body)
            )
            assert response.status_code == expected, (
                f"{path}: expected {expected}, got {response.status_code}"
            )
            return response.json()

        state = request("coordinator", "/workspaces", {"network_id": bundle["network_id"]})
        path = "/workspaces/" + state["id"]

        def command(actor, action, expected=200, **fields):
            return request(
                actor,
                path + "/commands",
                {"action": action, "command_id": str(uuid4()), **fields},
                expected,
            )

        plan = command("coordinator", "propose")["state"]["plan"]
        revision = plan["revision"]
        command("tunde", "accept", 403, resource_id="amara", revision=revision)
        command("tunde", "approve", 403, revision=revision)
        command("tunde", "receive", 403, resource_id="harbour", revision=revision, received_kg=320)
        checks["impersonation_dispatch_and_receipt_rejected"] = True
        view = request("tunde", path)
        assert "audit" not in view and "resources" not in view
        checks["participant_view_scoped"] = True
        for rid in plan["required"]:
            if rid != "tunde":
                r = next(r for r in state["resources"] if r["id"] == rid)
                fixture(rid, r["role"], rid)
            command(rid, "accept", resource_id=rid, revision=revision)
        checks["cognito_driver_confirmation_accepted"] = True
        command("coordinator", "approve", revision=revision)
        for r in plan["drivers"]:
            command(r["resource_id"], "pickup", resource_id=r["resource_id"], revision=revision)
        checks["cognito_driver_pickup_accepted"] = True
        for r in plan["recipients"]:
            command(
                r["resource_id"],
                "receive",
                resource_id=r["resource_id"],
                revision=revision,
                received_kg=r["kg"],
            )
        final = request("coordinator", path)
        assert final["status"] == "complete"
        audit = [a for a in final["audit"] if a["actor_id"] == driver["id"] and a["ok"]]
        assert {"accept", "pickup"} <= {a["action"] for a in audit}
        checks["driver_identity_in_persisted_action_audit"] = True
        checks["synthetic_rescue_complete"] = True
        with store.connect() as db:
            db.execute("UPDATE access_grants SET revoked=1 WHERE id=?", (driver["id"],))
        request("tunde", path, expected=401)
        checks["revoked_driver_session_rejected"] = True
    with store.connect() as db:
        db.executemany(
            "UPDATE access_grants SET revoked=1 WHERE id=?", [(g,) for g in fixture_grants]
        )
    secret_path.unlink()
    report = {
        "checks": checks,
        "identity": "Real Cognito-issued session for tunde; coordinator and other participants are local test grants",
        "data": "Synthetic rescue; no physical pickup/delivery occurred",
        "workspace_id": state["id"],
        "model_calls": 0,
        "external_messages_sent": 0,
        "captured_session_revoked_and_deleted": True,
    }
    Path("docs/cognito-driver-recovery-result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
