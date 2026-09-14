"""Offline evidence for a 192 kg replacement preserving the other 128 kg commitment."""

import argparse
import hashlib
import json
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from scripts.rehearse_commitment_failure import Rehearsal


def run(path, intake=None):
    r = Rehearsal(path)
    if intake:
        intake(r)
    r.command("coordinator", "propose")
    for rid in r.read()["plan"]["required"]:
        r.command(rid, "accept", resource_id=rid, revision=1)
    r.command("coordinator", "approve", revision=1)
    original = deepcopy(r.read()["plan"])
    r.command("tunde", "pickup", resource_id="tunde", revision=1)
    r.command(
        "coordinator",
        "propose_replacement",
        resource_id="tunde",
        replacement_id="ada",
        revision=1,
        available=True,
        reason="Synthetic vehicle breakdown; Ada offers 192 kg",
    )
    request = r.read()["replacement"]["id"]

    def submit(actor, action, **fields):
        return r.command(actor, action, revision=1, replacement_request_id=request, **fields)

    submit("tunde", "ack_stop", resource_id="tunde")
    for rid in ("ada", "harbour"):
        submit(rid, "accept_replacement", resource_id=rid)
    held = r.active_reservations()
    blocked = submit("coordinator", "commit_replacement", expected=409)
    r.check(
        "collected_load_requires_return",
        blocked["code"] == "CUSTODY_UNRESOLVED" and r.active_reservations() == held,
    )
    r.command("amara", "pickup", resource_id="amara", revision=1)
    submit(
        "donor",
        "confirm_return",
        resource_id="tunde",
        evidence="Synthetic donor receipt: 192 kg returned",
    )
    submit("coordinator", "commit_replacement", key="swap")
    current = r.read()["plan"]
    r.check("new_revision", current["revision"] == 2)
    r.check(
        "only_192kg_replaced",
        next(d["kg"] for d in current["drivers"] if d["resource_id"] == "ada") == 192,
    )
    r.check(
        "unaffected_load_preserved",
        next(d for d in current["drivers"] if d["resource_id"] == "amara")
        == next(d for d in original["drivers"] if d["resource_id"] == "amara"),
    )
    r.check(
        "unaffected_route_preserved",
        next(d for d in current["routes"] if d["driver"] == "amara")
        == next(d for d in original["routes"] if d["driver"] == "amara"),
    )
    r.check("unaffected_pickup_preserved", current["picked_up"] == ["amara"])
    r.check(
        "unaffected_confirmation_not_repeated",
        not any(
            a["resource_id"] == "amara" and a["action"] == "accept_replacement"
            for a in r.read()["audit"]
        ),
    )
    held = r.active_reservations()
    submit("coordinator", "commit_replacement", key="swap")
    r.check(
        "duplicate_swap_is_inert", r.active_reservations() == held and len(r.read()["plans"]) == 1
    )
    r.command("ada", "pickup", resource_id="ada", revision=1, expected=409)
    r.check(
        "old_revision_does_not_authorize_new_driver", r.read()["plan"]["picked_up"] == ["amara"]
    )
    r.command("ada", "pickup", resource_id="ada", revision=2)
    r.command("harbour", "receive", resource_id="harbour", revision=2, received_kg=320)
    r.check(
        "receipt_completes_and_releases",
        r.read()["status"] == "complete" and not r.active_reservations(),
    )
    result = {
        "checks": r.checks,
        "trace": r.trace,
        "events": r.read()["events"],
        "metrics": {
            "replacement_kg": 192,
            "preserved_kg": 128,
            "recipient_reported_kg": 320,
            "unchanged_driver_reconfirmations": 0,
        },
        "final_status": r.read()["status"],
    }
    r.client.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preserve existing evidence; select a new output path")
    with tempfile.TemporaryDirectory(prefix="relay-partial-") as directory:
        result = run(Path(directory) / "partial.sqlite3")
    root = Path(__file__).resolve().parents[1]
    sources = [
        Path(__file__),
        root / "scripts/rehearse_commitment_failure.py",
        *sorted((root / "relay_core").glob("*.py")),
        root / "uv.lock",
    ]
    result.update(
        at=datetime.now(UTC).isoformat(),
        scope="Offline authenticated HTTP; synthetic identities, travel and custody evidence. No external emails, AWS/model calls, actual food movement or measured human time.",
        source_sha256={
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        },
    )
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps({"passed_checks": sum(result["checks"].values()), "metrics": result["metrics"]})
    )


if __name__ == "__main__":
    main()
