"""Run a synthetic rescue against a real loopback HTTP API, using private test grants."""

import argparse
import json
import time
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--grants", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pause", action="store_true", help="Press Enter at each demo checkpoint")
    args = parser.parse_args()
    endpoint = urlsplit(args.base_url)
    if endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost"):
        parser.error("Use the local API or a verified loopback SSM tunnel")
    if args.output.exists():
        parser.error("Preserve prior evidence; choose a new output")
    grants = json.loads(args.grants.read_text())
    tokens = {g["resource_id"] or g["role"]: g["token"] for g in grants["grants"]}
    trace, checks = [], {}

    def call(actor, method, path, payload=None, expected=200):
        request = Request(
            args.base_url.rstrip("/") + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + tokens[actor],
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=20) as response:
                status, value = response.status, json.load(response)
        except HTTPError as error:
            status, value = error.code, json.load(error)
        if method == "POST":
            trace.append(
                {
                    "actor": actor,
                    "action": (payload or {}).get("action", "create"),
                    "http_status": status,
                    "code": value.get("code"),
                }
            )
        if status != expected:
            raise RuntimeError(f"{actor} {method} {path}: expected {expected}, got {status}")
        return value

    workspace = call("coordinator", "POST", "/workspaces", {})["id"]
    path = "/workspaces/" + workspace

    def read():
        return call("coordinator", "GET", path)

    def command(actor, action, expected=200, key=None, **fields):
        return call(
            actor,
            "POST",
            path + "/commands",
            {"action": action, "command_id": key or str(uuid4()), **fields},
            expected,
        )

    def check(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    def checkpoint(message):
        print(message, flush=True)
        if args.pause:
            input("Press Enter to continue… ")

    started = time.time()
    checkpoint(
        "SYNTHETIC DEMO · real HTTP engine · scripted human decisions · no food or email sent"
    )
    command("coordinator", "propose")
    original = deepcopy(read()["plan"])
    for actor in original["required"]:
        command(actor, "accept", resource_id=actor, revision=1)
    command("coordinator", "approve", revision=1)
    checkpoint("1. COMMITTED: Tunde 192 kg + Amara 128 kg. Both commitments accepted.")
    command("tunde", "pickup", resource_id="tunde", revision=1)
    command(
        "coordinator",
        "propose_replacement",
        resource_id="tunde",
        replacement_id="ada",
        revision=1,
        available=True,
        reason="SYNTHETIC: vehicle failure after collection; Ada offers replacement",
    )
    replacement_id = read()["replacement"]["id"]

    def replacement(actor, action, **fields):
        return command(actor, action, revision=1, replacement_request_id=replacement_id, **fields)

    checkpoint("2. DISRUPTED: Tunde cannot finish. Relay proposes replacing only his 192 kg.")
    replacement("tunde", "ack_stop", resource_id="tunde")
    for actor in ("ada", "harbour"):
        replacement(actor, "accept_replacement", resource_id=actor)
    held = replacement("coordinator", "commit_replacement", expected=409)
    check("custody_blocks_premature_replacement", held.get("code") == "CUSTODY_UNRESOLVED")
    checkpoint(
        "3. SAFETY HOLD: replacement cannot commit while the collected food is unaccounted for."
    )
    command("amara", "pickup", resource_id="amara", revision=1)
    replacement(
        "donor",
        "confirm_return",
        resource_id="tunde",
        evidence="SYNTHETIC: donor confirms 192 kg returned; no physical transfer occurred",
    )
    swap_key = str(uuid4())
    replacement("coordinator", "commit_replacement", key=swap_key)
    current = read()["plan"]
    check("revision_advanced", current["revision"] == 2)
    check(
        "unaffected_route_preserved",
        next(r for r in current["routes"] if r["driver"] == "amara")
        == next(r for r in original["routes"] if r["driver"] == "amara"),
    )
    check("unaffected_pickup_preserved", current["picked_up"] == ["amara"])
    check(
        "only_192kg_replaced",
        next(r["kg"] for r in current["drivers"] if r["resource_id"] == "ada") == 192,
    )
    check(
        "no_unaffected_reconfirmation",
        not any(
            a["resource_id"] == "amara" and a["action"] == "accept_replacement"
            for a in read()["audit"]
        ),
    )
    checkpoint("4. RECOVERED: Ada takes 192 kg. Amara's 128 kg route and pickup remain intact.")
    replacement("coordinator", "commit_replacement", key=swap_key)
    check("duplicate_commit_is_inert", read()["plan"] == current)
    command("ada", "pickup", resource_id="ada", revision=1, expected=409)
    check("stale_revision_rejected", read()["plan"]["picked_up"] == ["amara"])
    checkpoint(
        "5. RETRY-SAFE: duplicate commit is inert; the old revision cannot authorize pickup."
    )
    command("ada", "pickup", resource_id="ada", revision=2)
    command("harbour", "receive", resource_id="harbour", revision=2, received_kg=320)
    final = read()
    reservations = call("coordinator", "GET", "/networks/" + grants["network_id"] + "/reservations")
    check("receipt_closes_rescue", final["status"] == "complete")
    check("capacity_released", not any(not r["released"] for r in reservations))
    checkpoint(
        "6. COMPLETE: synthetic recipient reports 320 kg received; reserved capacity is released."
    )
    report = {
        "scope": "Real HTTP; synthetic opaque test grants, travel, participant decisions, custody and receipt. No model extraction or email in this run.",
        "workspace": workspace,
        "checks": checks,
        "trace": trace,
        "final_status": final["status"],
        "elapsed_seconds": round(time.time() - started, 3),
        "metrics": {
            "replaced_kg": 192,
            "preserved_kg": 128,
            "reported_received_kg": 320,
            "unchanged_driver_reconfirmations": 0,
        },
        "events": final["events"],
    }
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(f"PASS: {len(checks)} checks. Evidence saved to {args.output}", flush=True)


if __name__ == "__main__":
    main()
