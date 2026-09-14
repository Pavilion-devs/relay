"""Scripted protocol and scoring checks; these do not measure language accuracy."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from test_strands_adapter import ScriptedModel

from relay_core.agent_legacy import interpret
from relay_core.engine import apply, seed
from relay_core.evaluation import reserve_cost, score, validate_cases
from relay_core.store import Store


def draft_run(tmp_path, text, fields):
    store = Store(str(tmp_path / "interpret.sqlite3"))
    s = seed()
    mid = str(uuid4())
    s["messages"].append({"id": mid, "resource_id": "tunde", "text": text})
    store.create(s)
    model = ScriptedModel(draft={"resource_id": "tunde", "evidence_quote": text, **fields})
    metrics = {}
    interpret(store, s["id"], mid, model=model, metrics=metrics)
    return store.read(s["id"]), metrics


def test_time_draft_applies_atomically_and_invalidates_previous_plan(tmp_path):
    s, metrics = draft_run(
        tmp_path, "I must finish by minute 100 after rehearsal start.", {"window_closes": 100}
    )
    assert s["logistics"]["participants"]["tunde"]["window"]["closes"] == 170
    assert metrics["model_calls"] == 3
    assert apply(s, {"action": "propose"})["ok"]
    sid = s["suggestions"][0]["id"]
    assert apply(s, {"action": "apply_suggestion", "suggestion_id": sid})["ok"]
    assert s["plan"] is None
    assert s["logistics"]["participants"]["tunde"]["window"]["closes"] == 100
    assert s["facts_version"] == 2


def test_invalid_merged_window_does_not_partially_apply_capacity(tmp_path):
    s, _ = draft_run(
        tmp_path,
        "My maximum is 80 kg. Available only after minute 180.",
        {"capacity": 80, "window_opens": 180},
    )
    before = deepcopy(s["resources"])
    result = apply(s, {"action": "apply_suggestion", "suggestion_id": s["suggestions"][0]["id"]})
    assert result["code"] == "INVALID_WINDOW"
    assert s["resources"] == before and s["facts_version"] == 1


def test_manual_condition_cannot_become_automated_commitment(tmp_path):
    s, _ = draft_run(
        tmp_path,
        "My maximum is 80 kg only if my manager approves.",
        {
            "capacity": 80,
            "conditional": True,
            "condition_text": "only if my manager approves",
            "condition_kind": "manual_review",
        },
    )
    result = apply(s, {"action": "apply_suggestion", "suggestion_id": s["suggestions"][0]["id"]})
    assert result["code"] == "UNSUPPORTED_CONDITION"
    assert s["facts_version"] == 1


def test_supported_condition_keeps_its_exact_wording(tmp_path):
    s, _ = draft_run(
        tmp_path,
        "I can take 20 crates if another driver takes the rest.",
        {
            "capacity": 160,
            "conditional": True,
            "condition_text": "if another driver takes the rest",
            "condition_kind": "remaining_load_covered",
        },
    )
    assert apply(s, {"action": "apply_suggestion", "suggestion_id": s["suggestions"][0]["id"]})[
        "ok"
    ]
    r = next(r for r in s["resources"] if r["id"] == "tunde")
    assert r["condition_text"] == "if another driver takes the rest"


def test_fabricated_evidence_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="did not record"):
        draft_run(
            tmp_path,
            "My maximum is 80 kg.",
            {"capacity": 80, "evidence_quote": "My maximum is 160 kg."},
        )


def test_empty_draft_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="did not record"):
        draft_run(tmp_path, "Hello.", {})


def test_time_draft_becomes_stale_after_other_facts_change(tmp_path):
    s, _ = draft_run(tmp_path, "I must finish by minute 100.", {"window_closes": 100})
    apply(s, {"action": "change", "resource_id": "harbour", "capacity": 240})
    assert (
        apply(s, {"action": "apply_suggestion", "suggestion_id": s["suggestions"][0]["id"]})["code"]
        == "STALE_SUGGESTION"
    )


def test_dataset_has_separate_twenty_and_ten_case_splits():
    cases = json.loads(Path("evaluation/cases.json").read_text())["cases"]
    validate_cases(cases)
    assert sum(c["split"] == "development" for c in cases) == 20
    assert sum(c["split"] == "held_out" for c in cases) == 10
    with pytest.raises(ValueError):
        validate_cases([cases[0], cases[0]])


def test_scorer_detects_dropped_condition_and_extra_fact():
    case = {
        "sender": "tunde",
        "text": "80 kg if another driver takes the rest",
        "expected": {"kind": "draft", "fields": {"capacity": 80, "conditional": True}},
    }
    before = seed()
    after = deepcopy(before)
    after["suggestions"] = [{"resource_id": "tunde", "capacity": 80, "evidence_quote": "80 kg"}]
    assert not score(case, before, after)["automated_pass"]
    after["suggestions"][0]["conditional"] = True
    assert score(case, before, after)["automated_pass"]
    after["suggestions"][0]["available"] = True
    assert not score(case, before, after)["automated_pass"]
    after["resources"][0]["capacity"] = 1
    assert not score(case, before, after)["checks"]["confirmed_facts_unchanged"]


def test_clarification_heuristic_is_not_claimed_as_human_validation():
    case = {"text": "20", "expected": {"kind": "clarification", "topics": ["units"]}}
    before = seed()
    after = deepcopy(before)
    after["messages"] = [{"direction": "outbound", "text": "Which units?"}]
    result = score(case, before, after)
    assert result["automated_pass"] and result["human_review_required"]
    assert reserve_cost(0.06, 0.24, calls=4) == pytest.approx(0.07296)
