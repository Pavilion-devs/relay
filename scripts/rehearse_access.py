"""Exercise the scoped local API using a private grant bundle; never prints credentials."""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grants", required=True)
    args = parser.parse_args()
    bundle = json.loads(Path(args.grants).read_text())
    grants = {g["resource_id"] or g["role"]: g for g in bundle["grants"]}

    def request(name, path, body=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if name:
            headers["Authorization"] = "Bearer " + grants[name]["token"]
        req = Request(
            "http://127.0.0.1:8765" + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            response = urlopen(req, timeout=20)
        except HTTPError as exc:
            response = exc
        assert response.status == expected, f"Unexpected HTTP status {response.status} for {path}"
        return json.load(response)

    state = request("coordinator", "/workspaces", {})
    path = "/workspaces/" + state["id"]
    request(None, path, expected=401)

    def command(name, action, expected=200, **kw):
        return request(name, path + "/commands", {"action": action, **kw}, expected)

    plan = command("coordinator", "propose")["state"]["plan"]
    revision = plan["revision"]
    request("tunde", "/networks/" + bundle["network_id"] + "/reservations", expected=403)
    command("tunde", "accept", expected=403, resource_id="amara", revision=revision)
    command("coordinator", "accept", expected=403, resource_id="tunde", revision=revision)
    participant = request("tunde", path)
    assert "audit" not in participant and "resources" not in participant
    assert all(route["driver"] == "tunde" for route in participant["plan"]["routes"])
    for rid in plan["required"]:
        command(rid, "accept", resource_id=rid, revision=revision)
    command("coordinator", "approve", revision=revision)
    command("tunde", "pickup", resource_id="tunde", revision=revision)
    command("coordinator", "cancel", revision=revision, reason="Synthetic scoped cancellation")
    command(
        "tunde",
        "ack_return",
        expected=403,
        resource_id="tunde",
        revision=revision,
        evidence="Self-attested",
    )
    command(
        "donor",
        "ack_return",
        resource_id="tunde",
        revision=revision,
        evidence="Synthetic donor return record",
    )
    for rid in plan["required"]:
        command(rid, "ack_cancel", resource_id=rid, revision=revision)
    finished = request("coordinator", path)
    assert finished["status"] == "cancelled"
    assert {"coordinator", "donor", "driver", "recipient"} <= {a["role"] for a in finished["audit"]}
    report = {
        "scenario": "Scoped HTTP commitments and donor-confirmed cancellation",
        "checks": {
            "anonymous_read_rejected": True,
            "participant_impersonation_rejected": True,
            "coordinator_cannot_forge_acceptance": True,
            "participant_view_filtered": True,
            "driver_cannot_certify_own_return": True,
            "four_roles_in_persisted_audit": True,
            "cancellation_resolved": True,
        },
        "model_calls": 0,
        "external_messages_sent": 0,
        "identity_mode": "Local opaque grants; not Cognito or verified real identities",
    }
    Path("docs/access-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
