"""New one-call Strands path and deterministic source-span conversion."""

import json
from uuid import uuid4

import pytest
from test_strands_adapter import ScriptedModel

from relay_core.agent import interpret
from relay_core.engine import apply, seed
from relay_core.extraction import (
    Extraction,
    NormalizationError,
    capacity_kg,
    condition_kind,
    minute_offset,
    normalize,
)
from relay_core.store import Store


class JsonModel(ScriptedModel):
    def __init__(self, output, callback=None):
        super().__init__()
        self.output, self.callback = output, callback

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        assert not tool_specs
        self.calls += 1
        if self.callback:
            self.callback()
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": self.output}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def setup(tmp_path, text):
    store = Store(str(tmp_path / "extract.sqlite3"))
    s = seed()
    mid = str(uuid4())
    s["messages"].append({"id": mid, "resource_id": "tunde", "text": text})
    s = store.create(s)
    return store, s["id"], mid


@pytest.mark.parametrize(
    ("text", "expected"),
    [("80 kg", 80), ("20 crates", 160), ("0 kg", 0), ("7 crates", 56), ("80 kilograms", 80)],
)
def test_unit_conversion_is_domain_owned(text, expected):
    assert capacity_kg(text, 8) == expected


@pytest.mark.parametrize(
    "text", ["1.5 crates", "1.25 kg", "-1 kg", "20", "80 kg plus 10 crates", "1e3 kg", "10001 kg"]
)
def test_unsupported_quantities_never_round_or_guess(text):
    with pytest.raises(NormalizationError):
        capacity_kg(text, 8)


def test_explicit_time_offsets_and_timezone_arithmetic():
    start = "2026-09-13T12:00:00+00:00"
    assert minute_offset("minute 45", start) == 45
    assert minute_offset("13:30 UTC", start) == 90
    assert minute_offset("14:30 +01:00", start) == 90
    for bad in ["4:30", "minute 1441", "11:30 UTC", "25:00 UTC"]:
        with pytest.raises(NormalizationError):
            minute_offset(bad, start)


def test_condition_support_is_determined_by_domain_grammar():
    assert condition_kind("if another driver takes the rest") == "remaining_load_covered"
    assert condition_kind("only if my manager approves") == "manual_review"
    assert (
        condition_kind("if another driver takes the rest and my manager approves")
        == "manual_review"
    )


def test_one_model_call_records_reviewable_source_and_exact_kg(tmp_path):
    text = "My maximum is 80 kg."
    store, wid, mid = setup(tmp_path, text)
    model = JsonModel(
        json.dumps({"decision": "draft", "quantity_text": "80 kg", "evidence_quote": text})
    )
    metrics = {}
    assert "review" in interpret(store, wid, mid, model=model, metrics=metrics)
    assert model.calls == metrics["model_calls"] == 1
    state = store.read(wid)
    assert state["suggestions"][0]["capacity"] == 80
    assert state["suggestions"][0]["extraction"]["quantity_text"] == "80 kg"
    assert state["facts_version"] == 1


def test_model_cannot_supply_converted_capacity_or_a_fake_unit_span(tmp_path):
    store, wid, mid = setup(tmp_path, "My maximum is 80 kg.")
    for output in [
        {"decision": "draft", "capacity": 640, "evidence_quote": "My maximum is 80 kg."},
        {
            "decision": "draft",
            "quantity_text": "80 crates",
            "evidence_quote": "My maximum is 80 kg.",
        },
    ]:
        model = JsonModel(json.dumps(output))
        interpret(store, wid, mid, model=model)
        assert store.read(wid)["suggestions"] == []
    assert len(store.read(wid)["messages"]) == 2  # same message has one durable outcome


def test_clarification_question_cannot_invent_a_quantity(tmp_path):
    store, wid, mid = setup(tmp_path, "My maximum is 9.")
    model = JsonModel(json.dumps({"decision": "clarify", "reason": "missing_units"}))
    reply = interpret(store, wid, mid, model=model)
    assert "20" not in reply and "units" in reply


def test_malformed_response_becomes_visible_clarification_not_a_draft(tmp_path):
    store, wid, mid = setup(tmp_path, "My maximum is 80 kg.")
    interpret(store, wid, mid, model=JsonModel("not json"))
    state = store.read(wid)
    assert not state["suggestions"]
    assert state["messages"][-1]["reason"] == "invalid_extraction"


def test_fact_change_during_inference_prevents_stale_draft(tmp_path):
    store, wid, mid = setup(tmp_path, "My maximum is 80 kg.")

    def update():
        store.transact(
            wid,
            "change",
            {},
            lambda s: apply(s, {"action": "change", "resource_id": "harbour", "capacity": 240}),
        )

    model = JsonModel(
        json.dumps(
            {
                "decision": "draft",
                "quantity_text": "80 kg",
                "evidence_quote": "My maximum is 80 kg.",
            }
        ),
        update,
    )
    with pytest.raises(RuntimeError):
        interpret(store, wid, mid, model=model)
    assert store.read(wid)["suggestions"] == []


def test_every_span_must_belong_to_the_quoted_source():
    extract = Extraction(decision="draft", quantity_text="80 kg", evidence_quote="20 crates")
    with pytest.raises(NormalizationError):
        normalize(extract, {"text": "20 crates, not 80 kg"}, seed())


def test_markdown_json_envelope_does_not_weaken_schema_validation(tmp_path):
    text = "My maximum is 80 kg."
    store, wid, mid = setup(tmp_path, text)
    payload = json.dumps({"decision": "draft", "quantity_text": "80 kg", "evidence_quote": text})
    interpret(store, wid, mid, model=JsonModel("```json\n" + payload + "\n```"))
    assert store.read(wid)["suggestions"][0]["capacity"] == 80


def test_clarification_score_checks_reason_not_incidental_keywords():
    from relay_core.evaluation import score

    before = seed()
    import copy

    after = copy.deepcopy(before)
    after["messages"] = [
        {
            "direction": "outbound",
            "text": "Please confirm units and conditions.",
            "reason": "invalid_extraction",
        }
    ]
    case = {"expected": {"kind": "clarification", "topics": ["units"], "reason": "missing_units"}}
    assert not score(case, before, after)["automated_pass"]


@pytest.mark.parametrize(
    "cue",
    ["if", "provided that", "unless", "only when", "as long as", "subject to", "on condition that"],
)
def test_omitted_condition_cannot_be_hidden_by_an_authentic_excerpt(tmp_path, cue):
    text = f"I can carry 80 kg {cue} my manager approves."
    store, wid, mid = setup(tmp_path, text)
    output = {"decision": "draft", "quantity_text": "80 kg", "evidence_quote": "I can carry 80 kg"}
    interpret(store, wid, mid, model=JsonModel(json.dumps(output)))
    state = store.read(wid)
    assert state["suggestions"] == []
    assert state["facts_version"] == 1
    assert state["messages"][-1]["reason"] == "invalid_extraction"


@pytest.mark.parametrize(
    "suffix",
    [
        " and my manager approves.",
        ". Also, my manager must approve.",
        "; unless the van breaks down.",
    ],
)
def test_shortened_supported_condition_cannot_hide_an_additional_dependency(suffix):
    condition = "if another driver takes the rest"
    text = "I can carry 80 kg " + condition + suffix
    extraction = Extraction(
        decision="draft", quantity_text="80 kg", condition_text=condition, evidence_quote=text
    )
    with pytest.raises(NormalizationError):
        normalize(extraction, {"text": text}, seed())
    extraction.condition_text = condition + suffix
    result = normalize(extraction, {"text": text}, seed())
    assert result["conditional"] is True
    assert result["condition_kind"] == "manual_review"


def test_complete_supported_condition_still_normalizes():
    condition = "if another driver takes the rest."
    text = "I can carry 80 kg " + condition + "  "
    result = normalize(
        Extraction(
            decision="draft", quantity_text="80 kg", condition_text=condition, evidence_quote=text
        ),
        {"text": text},
        seed(),
    )
    assert result["capacity"] == 80
    assert result["condition_kind"] == "remaining_load_covered"


def test_repeated_condition_cannot_cover_an_earlier_omitted_restriction():
    condition = "if another driver takes the rest."
    text = "If my manager approves, I can carry 80 kg " + condition
    with pytest.raises(NormalizationError):
        normalize(
            Extraction(
                decision="draft",
                quantity_text="80 kg",
                condition_text=condition,
                evidence_quote=text,
            ),
            {"text": text},
            seed(),
        )
