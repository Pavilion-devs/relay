"""Adversarial model-output fixtures; these do not measure live model accuracy."""

import json

import pytest
from test_extraction import JsonModel, setup

from relay_core.agent import interpret
from relay_core.engine import seed
from relay_core.extraction import Extraction, NormalizationError, normalize


@pytest.mark.parametrize(
    "text,fields,reason",
    [
        (
            "My maximum is 24 crates. Actually, correct that to 16 crates.",
            {"quantity_text": "24 crates"},
            "ambiguous_quantity",
        ),
        (
            "My maximum is 16 crates and my maximum is 24 crates.",
            {"quantity_text": "16 crates"},
            "ambiguous_quantity",
        ),
        (
            "I can take 40 kg. I can take 80 kg. Both limits apply.",
            {"quantity_text": "80 kg"},
            "ambiguous_quantity",
        ),
        ("I cannot carry 80 kg.", {"quantity_text": "80 kg"}, "ambiguous_quantity"),
        ("My maximum is 80 kg.", {"quantity_text": "0 kg"}, "ambiguous_quantity"),
        ("My maximum is -80 kg.", {"quantity_text": "80 kg"}, "ambiguous_quantity"),
        ("My maximum is 1,000 kg.", {"quantity_text": "000 kg"}, "ambiguous_quantity"),
        ("Maybe I can carry 80 kg.", {"quantity_text": "80 kg"}, "uncertain_offer"),
        ("I might carry 80 kg.", {"quantity_text": "80 kg"}, "uncertain_offer"),
        ("Amara can carry 80 kg.", {"quantity_text": "80 kg"}, "other_person"),
        (
            "My maximum is 80 kg. I must finish by minute 100.",
            {"quantity_text": "80 kg"},
            "unclear_time",
        ),
        ("My maximum is 80 kg. I must finish by 4:30.", {"quantity_text": "80 kg"}, "unclear_time"),
        (
            "I am available from minute 30 until minute 90.",
            {"available": True, "opens_text": "minute 90", "closes_text": "minute 30"},
            "unclear_time",
        ),
        (
            "I am available from minute 30 until minute 90.",
            {"available": True, "opens_text": "minute 30"},
            "unclear_time",
        ),
        ("I am unavailable for this rescue.", {"available": True}, "uncertain_offer"),
        ("My maximum is 80 kg.", {"quantity_text": "80 kg", "available": True}, "uncertain_offer"),
        ("My maximum is 80 kg. I am unavailable.", {"quantity_text": "80 kg"}, "uncertain_offer"),
        ("I am available. My maximum is 20.", {"available": True}, "missing_units"),
    ],
)
def test_unsafe_literal_excerpts_are_rejected_with_specific_reason(tmp_path, text, fields, reason):
    store, wid, mid = setup(tmp_path, text)
    metrics = {}
    interpret(
        store,
        wid,
        mid,
        model=JsonModel(json.dumps({"decision": "draft", "evidence_quote": text, **fields})),
        metrics=metrics,
    )
    state = store.read(wid)
    assert not state["suggestions"] and state["facts_version"] == 1
    assert state["messages"][-1]["reason"] == reason
    assert metrics["normalization_reason"] == reason
    assert metrics["model_calls"] == 1


@pytest.mark.parametrize(
    "text,fields,expected",
    [
        (
            "My maximum is 24 crates. Actually, correct that to 16 crates.",
            {"quantity_text": "16 crates"},
            {"capacity": 128},
        ),
        ("I am unavailable for this rescue.", {"available": False}, {"available": False}),
        (
            "I cannot participate in this rescue any more.",
            {"available": False},
            {"available": False},
        ),
        (
            "I am available from minute 30 until minute 90.",
            {"available": True, "opens_text": "minute 30", "closes_text": "minute 90"},
            {"available": True, "window_opens": 30, "window_closes": 90},
        ),
        (
            "My maximum is 80 kg. I must finish by minute 100.",
            {"quantity_text": "80 kg", "closes_text": "minute 100"},
            {"capacity": 80, "window_closes": 100},
        ),
        ("My maximum is 0 kg.", {"quantity_text": "0 kg"}, {"capacity": 0}),
    ],
)
def test_complete_supported_extractions_survive(text, fields, expected):
    result = normalize(
        Extraction(decision="draft", evidence_quote=text, **fields),
        {"text": text, "resource_id": "tunde"},
        seed(),
    )
    assert result["kind"] == "draft"
    assert all(result[k] == v for k, v in expected.items())


def test_v7_draft_records_provenance_without_applying_facts(tmp_path):
    text = "My maximum is 80 kg."
    store, wid, mid = setup(tmp_path, text)
    interpret(
        store,
        wid,
        mid,
        model=JsonModel(
            json.dumps({"decision": "draft", "quantity_text": "80 kg", "evidence_quote": text})
        ),
    )
    state = store.read(wid)
    assert state["suggestions"][0]["normalizer_version"] == 7
    assert state["facts_version"] == 1


def test_numbers_in_explicit_conditions_are_conservatively_reviewed():
    text = "I can carry 80 kg if another driver takes 40 kg."
    with pytest.raises(NormalizationError) as caught:
        normalize(
            Extraction(
                decision="draft",
                quantity_text="80 kg",
                condition_text="if another driver takes 40 kg.",
                evidence_quote=text,
            ),
            {"text": text, "resource_id": "tunde"},
            seed(),
        )
    assert caught.value.reason == "ambiguous_quantity"
