import pytest

from relay_core.engine import seed
from relay_core.evaluation import condition_comparison
from relay_core.extraction import Extraction, NormalizationError, normalize


def extract(text, **fields):
    return normalize(Extraction(decision="draft", evidence_quote=text, **fields),
                     {"text": text, "resource_id": "tunde"}, seed())


@pytest.mark.parametrize("suffix", [".", ".   ", ""])
def test_complete_condition_without_period(suffix):
    result = extract("I can take 72 kg if another driver takes the rest" + suffix,
                     quantity_text="72 kg", condition_text="if another driver takes the rest")
    assert result["condition_kind"] == "remaining_load_covered"


@pytest.mark.parametrize("suffix", ["...", "?", "; only with a trolley.", " and my manager approves."])
def test_condition_cannot_drop_restrictions(suffix):
    with pytest.raises(NormalizationError):
        extract("I can take 72 kg if another driver takes the rest" + suffix,
                quantity_text="72 kg", condition_text="if another driver takes the rest")


@pytest.mark.parametrize("prefix", ["My availability is from", "My available window is"])
def test_explicit_window_availability(prefix):
    result = extract(prefix + " minute 20 through minute 70.", available=True,
                     opens_text="minute 20", closes_text="minute 70")
    assert result["available"] is True and result["window_closes"] == 70


@pytest.mark.parametrize("prefix", ["My availability is not", "My available window is not", "Maybe my availability is from"])
def test_negative_or_uncertain_window_not_positive(prefix):
    with pytest.raises(NormalizationError):
        extract(prefix + " minute 20 through minute 70.", available=True,
                opens_text="minute 20", closes_text="minute 70")


def test_unitless_model_quantity_gets_specific_question():
    with pytest.raises(NormalizationError) as caught:
        extract("Reduce my capacity to 7.", quantity_text="7")
    assert caught.value.reason == "missing_units"


def test_condition_comparison_preserves_meaningful_suffixes():
    assert condition_comparison("if approved.") == "if approved"
    assert condition_comparison("if approved...") != "if approved"
    assert condition_comparison("if approved?") != "if approved"
    assert condition_comparison("if approved and refrigerated.") != "if approved"


def test_v4_scoring_keeps_v3_exact_and_rejects_extra_fields():
    from copy import deepcopy

    from relay_core.evaluation import score

    text = 'I can carry 40 kg if approved.'
    case = {'sender': 'tunde', 'text': text, 'expected': {'kind': 'draft', 'fields': {
        'capacity': 40, 'conditional': True, 'condition_text': 'if approved',
        'condition_kind': 'manual_review',
    }}}
    before = seed()
    after = deepcopy(before)
    after['suggestions'] = [{**case['expected']['fields'], 'condition_text': 'if approved.',
                            'resource_id': 'tunde', 'evidence_quote': text}]
    assert not score(case, before, after)['automated_pass']
    assert score(case, before, after, version=4)['automated_pass']
    after['suggestions'][0]['available'] = True
    assert not score(case, before, after, version=4)['automated_pass']
    after['suggestions'][0].pop('available')
    after['suggestions'][0]['evidence_quote'] = 'invented'
    assert not score(case, before, after, version=4)['automated_pass']
