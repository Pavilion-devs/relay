"""Exercise the local HTTP flow without model calls or external notifications."""

import json
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:5173/api/relay"


def request(path, body=None):
    req = Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urlopen(req, timeout=20)
    except HTTPError as exc:
        response = exc
    return response.status, json.load(response)


status, state = request("/workspaces", {})
assert status == 200
wid = state["id"]


def command(action, expected=200, **values):
    status, result = request(f"/workspaces/{wid}/commands", {"action": action, **values})
    assert status == expected, result
    return result


original = command("propose")["state"]["plan"]
command("accept", resource_id="tunde", revision=original["revision"])
command("change", resource_id="harbour", capacity=240)
logistics = deepcopy(state["logistics"])
logistics["participants"]["harbour"]["window"]["closes"] = 40
command("set_logistics", logistics=logistics)
blocked = command("propose", expected=409)
assert blocked["code"] == "NO_FEASIBLE_ROUTE"
assert blocked["state"]["plan"] is None
command("change", resource_id="garden", capacity=320)
recovered = command("propose")["state"]["plan"]
assert {leg["recipient"] for leg in recovered["legs"]} == {"garden"}
stale = command("accept", expected=409, resource_id="tunde", revision=original["revision"])
assert stale["code"] == "STALE_REVISION"
for person in recovered["required"]:
    command("accept", resource_id=person, revision=recovered["revision"])
command("approve", revision=recovered["revision"])
for driver in recovered["drivers"]:
    command("pickup", resource_id=driver["resource_id"], revision=recovered["revision"])
finished = command("receive", resource_id="garden", received_kg=320, revision=recovered["revision"])
assert finished["state"]["status"] == "complete"
report = {
    "test": "Synthetic HTTP route recovery through the frontend proxy",
    "workspace_id": wid,
    "checks": {
        "capacity_sufficient_but_route_rejected": True,
        "alternative_destination_used": True,
        "obsolete_acceptance_rejected": True,
        "recipient_acknowledged_320_kg": True,
    },
    "source": logistics["source"],
    "model_calls": 0,
    "routes": recovered["routes"],
    "status": finished["state"]["status"],
}
Path("docs/route-recovery-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps(
        {"workspace_id": wid, "checks": report["checks"], "status": report["status"]}, indent=2
    )
)
