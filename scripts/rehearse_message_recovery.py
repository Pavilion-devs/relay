"""Authenticated message-to-receipt rehearsal; model response is an explicit fixture."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from relay_core import agent
from scripts.rehearse_partial_replacement import run

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_extraction import JsonModel


def intake(r):
    text = "My maximum capacity is 192 kg."
    model = JsonModel(json.dumps({
        "decision": "draft", "quantity_text": "192 kg", "evidence_quote": text,
    }))
    before = deepcopy(r.read())
    with patch.dict(os.environ, {"RELAY_MODEL_ID": "offline-fixture"}), patch.object(
        agent, "BedrockModel", return_value=model
    ):
        response = r.client.post(
            f"/workspaces/{r.wid}/messages",
            json={"resource_id": "tunde", "text": text},
            headers={"Authorization": "Bearer " + r.tokens["tunde"]},
        )
    r.check("authenticated_intake", response.status_code == 200 and model.calls == 1)
    after = r.read()
    r.check("intake_does_not_apply", all(
        before[k] == after[k] for k in ("resources", "facts_version", "plan", "status")
    ))
    suggestion = after["suggestions"][0]["id"]
    r.command("amara", "apply_suggestion", suggestion_id=suggestion, expected=403)
    r.command("coordinator", "apply_suggestion", suggestion_id=suggestion, key="review-intake")
    r.check("coordinator_review_applied", r.read()["facts_version"] == before["facts_version"] + 1)
    version = r.read()["facts_version"]
    r.command("coordinator", "apply_suggestion", suggestion_id=suggestion, key="review-intake")
    r.check("duplicate_review_inert", r.read()["facts_version"] == version)
    r.command("coordinator", "apply_suggestion", suggestion_id=suggestion, expected=409)
    r.check("already_applied_review_rejected", r.read()["facts_version"] == version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preserve existing reports")
    with tempfile.TemporaryDirectory(prefix="relay-message-recovery-") as directory:
        result = run(Path(directory) / "rehearsal.sqlite3", intake=intake)
    result["scope"] = "Offline authenticated HTTP, real Strands loop, explicitly scripted model output; synthetic identities, travel, custody and recipient receipt. No AWS calls, email or real food movement."
    root = Path(__file__).resolve().parents[1]
    result["source_sha256"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [Path(__file__), root / "scripts/rehearse_partial_replacement.py",
                  root / "scripts/rehearse_commitment_failure.py", *sorted((root / "relay_core").glob("*.py"))]
    }
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"checks": len(result["checks"]), "status": result["final_status"]}))


if __name__ == "__main__":
    main()
