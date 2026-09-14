"""Single authorized Claude development run, with pre-call counted-token reservations."""

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from botocore.config import Config
from strands.models import BedrockModel

from relay_core.access import ScopedStore, issue
from relay_core.agent import interpret
from relay_core.engine import seed
from relay_core.evaluation import score, validate_cases
from relay_core.store import Store

MODEL = "us.anthropic.claude-sonnet-4-6"
# Conservative bounds: double the published base $3/$15 rates, also covering geo premium.
INPUT_BOUND, OUTPUT_BOUND = 6, 30


class CountedClient:
    def __init__(self, client, reserve):
        self.client, self.reserve, self.called = client, reserve, False
        self.response = None

    def converse(self, **request):
        if self.called:
            raise RuntimeError("Exactly one inference attempt per case")
        self.called = True
        if set(request) - {"modelId", "messages", "system", "inferenceConfig"}:
            raise ValueError("Unexpected billable request options")
        if request["modelId"] != MODEL or request["inferenceConfig"]["maxTokens"] != 1000:
            raise ValueError("Unexpected model or output bound")
        count = self.client.count_tokens(
            modelId="anthropic.claude-sonnet-4-6",
            input={"converse": {k: request[k] for k in ("messages", "system") if k in request}},
        )["inputTokens"]
        if type(count) is not int or not 0 < count <= 10000:
            raise ValueError("Prompt outside short-context evaluation bound")
        self.reserve(((count + 256) * INPUT_BOUND + 1000 * OUTPUT_BOUND) / 1e6, count)
        self.response = self.client.converse(**request)
        return self.response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/eval-claude-v3-01.json"))
    parser.add_argument("--cases", type=Path, default=Path("evaluation/extraction-v2-cases.json"))
    parser.add_argument("--budget-from", type=Path)
    parser.add_argument("--score-version", type=int, choices=[3, 4], default=3)
    args = parser.parse_args()
    output = args.output
    if output.exists():
        raise SystemExit("Existing report; refusing another billed run")
    raw = args.cases.read_bytes()
    cases = json.loads(raw)["cases"]
    validate_cases(cases)
    report = {
        "model_id": MODEL,
        "authorized_additional_cap_usd": 5,
        "reserved_cost_usd": 0,
        "rates_are_conservative_bounds": True,
        "input_bound_per_million": INPUT_BOUND,
        "output_bound_per_million": OUTPUT_BOUND,
        "status": "running",
        "runs": [],
        "scope": "Synthetic development evaluation; not independent held-out or head-to-head evidence",
        "score_version": args.score_version,
        "dataset_path": str(args.cases),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), Path("relay_core/agent.py"), Path("relay_core/extraction.py")]
        },
    }
    if args.budget_from:
        if args.resume_from:
            raise ValueError("Use only one budget parent")
        parent = json.loads(args.budget_from.read_text())
        if parent["authorized_additional_cap_usd"] != 5 or parent["status"] != "complete":
            raise ValueError("Invalid completed budget parent")
        report["reserved_cost_usd"] = parent["reserved_cost_usd"]
        report["prior_reservation_usd"] = parent["reserved_cost_usd"]
        report["budget_parent"] = str(args.budget_from)
    if args.resume_from:
        parent = json.loads(args.resume_from.read_text())
        assert (
            parent["model_id"] == MODEL
            and parent["status"] == "stopped_on_provider_or_runtime_error"
        )
        assert parent["dataset_sha256"] == report["dataset_sha256"]
        for source in ("relay_core/agent.py", "relay_core/extraction.py"):
            assert parent["source_sha256"][source] == report["source_sha256"][source]
        report["reserved_cost_usd"] = parent["reserved_cost_usd"]
        report["prior_reservation_usd"] = parent["reserved_cost_usd"]
        report["parent_report"] = str(args.resume_from)
        completed = {r["id"] for r in parent["runs"] if not r.get("error_type")}
        cases = [c for c in cases if c["id"] not in completed]
    with output.open("x") as f:
        json.dump(report, f, indent=2)

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="relay-claude-eval-") as directory:
        store = Store(str(Path(directory) / "eval.sqlite3"))
        for case in cases:
            time.sleep(6)
            state = seed()
            state["logistics"]["starts_at"] = "2026-09-13T12:00:00+00:00"
            state["deadline"] = "2026-09-13T15:00:00+00:00"
            mid = str(uuid4())
            state["messages"].append(
                {
                    "id": mid,
                    "resource_id": case["sender"],
                    "text": case["text"],
                    "direction": "inbound",
                }
            )
            state = store.create(state)
            role = next(r["role"] for r in state["resources"] if r["id"] == case["sender"])
            grant = issue(store, state["network_id"], role, case["sender"])
            run = {
                "id": case["id"],
                "text": case["text"],
                "expected": case["expected"],
                "metrics": {},
            }
            report["runs"].append(run)
            save()

            def reserve(cost, count, run=run):
                if report["reserved_cost_usd"] + cost > 5:
                    raise RuntimeError("Budget cap reached")
                report["reserved_cost_usd"] += cost
                run.update(reserved_usd=cost, counted_input_tokens=count, inference_attempted=True)
                save()

            model = BedrockModel(
                model_id=MODEL,
                region_name="us-east-1",
                max_tokens=1000,
                temperature=0,
                streaming=False,
                boto_client_config=Config(
                    retries={"total_max_attempts": 1}, read_timeout=60, connect_timeout=10
                ),
            )
            wrapped = CountedClient(model.client, reserve)
            model.client = wrapped
            try:
                interpret(
                    ScopedStore(store, grant["token"], case["sender"]),
                    state["id"],
                    mid,
                    model=model,
                    metrics=run["metrics"],
                )
                run.update(score(case, state, store.read(state["id"]), version=args.score_version))
            except Exception as exc:  # noqa: BLE001 - persist error and stop; never auto retry
                run.update(error_type=type(exc).__name__, automated_pass=False)
                # Synthetic evaluation inputs only. Retain concise diagnostics, never credentials.
                run["error_detail"] = str(exc)[:500]
            if wrapped.response:
                run["raw_model_message"] = wrapped.response.get("output", {}).get("message")
                run["stop_reason"] = wrapped.response.get("stopReason")
            save()
            print(case["id"] + ": " + ("pass" if run["automated_pass"] else "FAIL"), flush=True)
            if run.get("error_type"):
                report["status"] = "stopped_on_provider_or_runtime_error"
                save()
                break
        else:
            report["status"] = "complete"
    report["automated_passes"] = sum(r["automated_pass"] for r in report["runs"])
    report["observed_token_cost_upper_estimate_usd"] = sum(
        (
            r["metrics"].get("usage", {}).get("inputTokens", 0) * INPUT_BOUND
            + r["metrics"].get("usage", {}).get("outputTokens", 0) * OUTPUT_BOUND
        )
        / 1e6
        for r in report["runs"]
    )
    save()
    print(
        json.dumps(
            {
                k: report[k]
                for k in [
                    "status",
                    "automated_passes",
                    "reserved_cost_usd",
                    "observed_token_cost_upper_estimate_usd",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()
