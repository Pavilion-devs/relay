"""Transparent development/held-out extraction evaluation; dry validation is the default."""

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from botocore.config import Config
from strands.models import BedrockModel

from .access import ScopedStore, issue
from .agent import interpret
from .engine import seed
from .store import Store

FIELDS = (
    "capacity",
    "available",
    "conditional",
    "window_opens",
    "window_closes",
    "condition_text",
    "condition_kind",
)


def validate_cases(cases):
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Case IDs must be unique and the dataset nonempty")
    for case in cases:
        if case["split"] not in ("development", "held_out") or not case["text"].strip():
            raise ValueError("Case split and message are required")
        expected = case["expected"]
        if expected["kind"] == "draft":
            if not expected.get("fields") or set(expected["fields"]) - set(FIELDS):
                raise ValueError("Expected draft fields are invalid")
        elif expected["kind"] != "clarification" or not expected.get("topics"):
            raise ValueError("Expected clarification topics are required")


def condition_comparison(value):
    """V4 comparison ignores one terminal period; source evidence remains exact."""
    if isinstance(value, str) and value.endswith(".") and not value.endswith(".."):
        return value[:-1]
    return value


def score(case, before, after, *, version=3):
    if version not in (3, 4):
        raise ValueError("Unsupported scoring version")
    expected = case["expected"]
    drafts = after["suggestions"]
    questions = [m["text"] for m in after["messages"] if m.get("direction") == "outbound"]
    invariant = all(
        before.get(k) == after.get(k)
        for k in ("resources", "logistics", "facts_version", "plan", "status")
    )
    checks = {"confirmed_facts_unchanged": invariant}
    if expected["kind"] == "draft":
        checks["one_draft_no_question"] = len(drafts) == 1 and not questions
        found = drafts[0] if len(drafts) == 1 else {}
        checks["exact_fields"] = all(found.get(k) == expected["fields"].get(k) for k in FIELDS)
        if version == 4:
            checks["exact_fields"] = all(
                (condition_comparison(found.get(k)) == condition_comparison(expected["fields"].get(k)))
                if k == "condition_text" else found.get(k) == expected["fields"].get(k)
                for k in FIELDS
            )
        quote = found.get("evidence_quote", "")
        checks["source_quote"] = bool(quote.strip()) and quote in case["text"]
        checks["sender_scope"] = found.get("resource_id") == case["sender"]
    else:
        checks["one_question_no_draft"] = len(questions) == 1 and not drafts
        checks["topic_heuristic"] = any(
            term.lower() in " ".join(questions).lower() for term in expected["topics"]
        )
        if "reason" in expected:
            recorded = [m for m in after["messages"] if m.get("direction") == "outbound"]
            checks.pop("topic_heuristic", None)
            checks["clarification_reason"] = (
                len(recorded) == 1 and recorded[0].get("reason") == expected["reason"]
            )
    return {
        "checks": checks,
        "automated_pass": all(checks.values()),
        "human_review_required": True,
        "drafts": drafts,
        "questions": questions,
    }


def reserve_cost(input_rate, output_rate, calls=1):
    # Pinned Nova Lite/Pro context ceiling, max_tokens=1000, one model call,
    # no provider/SDK retries. Charge the full conservative reservation even on error.
    return calls * (300_000 * input_rate + 1000 * output_rate) / 1_000_000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="evaluation/cases.json")
    parser.add_argument("--split", choices=["development", "held_out"], default="development")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--model-id", choices=["us.amazon.nova-lite-v1:0", "us.amazon.nova-pro-v1:0"]
    )
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raw = Path(args.cases).read_bytes()
    cases = json.loads(raw)["cases"]
    validate_cases(cases)
    assert 1 <= args.repeats <= 5
    selected = [c for c in cases if c["split"] == args.split]
    report = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "extraction_sha256": hashlib.sha256(
            Path(__file__).with_name("extraction.py").read_bytes()
        ).hexdigest(),
        "agent_sha256": hashlib.sha256(
            Path(__file__).with_name("agent.py").read_bytes()
        ).hexdigest(),
        "sdk_version": version("strands-agents"),
        "split": args.split,
        "mode": "live" if args.live else "dataset_validation_only",
        "selected_cases": len(selected),
        "repeats": args.repeats,
        "runs": [],
        "reserved_cost_usd": 0,
        "status": "validated",
        "provenance": json.loads(raw)["provenance"],
    }
    output = Path(args.output)
    if output.exists():
        raise SystemExit("Choose a new report path; prior evaluations must not be overwritten.")
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    if not args.live:
        save()
        print(f"Validated {len(selected)} cases. No inference or accuracy measurement performed.")
        return
    if not args.max_usd or not args.input_usd_per_million or not args.output_usd_per_million:
        raise SystemExit(
            "Live runs require an authorized dollar cap and verified positive model rates."
        )
    if any(
        not math.isfinite(v) or v <= 0
        for v in (args.max_usd, args.input_usd_per_million, args.output_usd_per_million)
    ):
        raise SystemExit("Budget and rates must be positive.")
    model_id = args.model_id or os.environ.get("RELAY_MODEL_ID")
    if model_id not in ("us.amazon.nova-lite-v1:0", "us.amazon.nova-pro-v1:0"):
        raise SystemExit("This cost guard is pinned to us.amazon.nova-lite-v1:0.")
    ceiling = reserve_cost(args.input_usd_per_million, args.output_usd_per_million)
    report.update(
        status="running",
        model_id=model_id,
        cost_cap_usd=args.max_usd,
        per_case_reserve_usd=ceiling,
        rates={"input": args.input_usd_per_million, "output": args.output_usd_per_million},
    )
    save()
    with tempfile.TemporaryDirectory(prefix="relay-eval-") as directory:
        store = Store(str(Path(directory) / "evaluation.sqlite3"))
        for repeat in range(args.repeats):
            for case in selected:
                if report["reserved_cost_usd"] + ceiling > args.max_usd:
                    report["status"] = "budget_stopped"
                    save()
                    return
                report["reserved_cost_usd"] += ceiling
                report["current_case"] = {"id": case["id"], "repeat": repeat}
                save()  # Reserve before a billable call, retain failures/interrupted runs.
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
                adapter = ScopedStore(store, grant["token"], case["sender"])
                metrics, started = {}, time.monotonic()
                error = None
                try:
                    model = BedrockModel(
                        model_id=model_id,
                        region_name="us-east-1",
                        max_tokens=1000,
                        temperature=0,
                        boto_client_config=Config(
                            retries={"total_max_attempts": 1}, read_timeout=60, connect_timeout=10
                        ),
                    )
                    interpret(adapter, state["id"], mid, model=model, metrics=metrics)
                except Exception as exc:  # noqa: BLE001 - retain provider failures without account details
                    error = type(exc).__name__
                usage = metrics.get("usage", {})
                metrics["estimated_inference_usd"] = (
                    (
                        (
                            usage.get("inputTokens", 0) * args.input_usd_per_million
                            + usage.get("outputTokens", 0) * args.output_usd_per_million
                        )
                        / 1_000_000
                    )
                    if usage.get("totalTokens", 0) > 0
                    else None
                )
                after = store.read(state["id"])
                result = score(case, state, after)
                if error:
                    result["automated_pass"] = False
                report["runs"].append(
                    {
                        "id": case["id"],
                        "repeat": repeat,
                        "text": case["text"],
                        "expected": case["expected"],
                        "error_type": error,
                        "elapsed_seconds": time.monotonic() - started,
                        "metrics": metrics,
                        **result,
                    }
                )
                report.pop("current_case", None)
                save()
                print(f"{case['id']}: {'pass' if result['automated_pass'] else 'FAIL'}", flush=True)
    report.pop("current_case", None)
    report["status"] = "complete"
    report["automated_passes"] = sum(r["automated_pass"] for r in report["runs"])
    save()


if __name__ == "__main__":
    main()
