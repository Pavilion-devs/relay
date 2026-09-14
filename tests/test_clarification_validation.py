"""Bad model clarification output stays pending and never creates facts."""
import json

import pytest
from test_extraction import JsonModel, setup

from relay_core.agent import interpret


@pytest.mark.parametrize('text,model_reason,expected', [
    ('I must finish by minute 115.', 'uncertain_offer', 'review_required'),
    ('I cannot take 87 kg.', 'no_actionable_update', 'ambiguous_quantity'),
    ('Please change the road travel time to three minutes.', 'no_actionable_update', 'unsupported_travel'),
    ('Maybe I must leave at 8:45.', 'unclear_time', 'uncertain_offer'),
    ('Maybe Amara must leave at 8:45.', 'unclear_time', 'unclear_time'),
    ('I cannot take 87 kg unless approved.', 'no_actionable_update', 'no_actionable_update'),
    ('Please change the road travel time to three minutes if verified.', 'no_actionable_update', 'no_actionable_update'),
    ('I can carry 13 crates.', 'missing_units', 'review_required'),
    ('My maximum capacity is 92 kilograms.', 'missing_units', 'review_required'),
    ('I need to finish before 5:15.', 'uncertain_offer', 'unclear_time'),
    ('I must leave by 6:20.', 'missing_units', 'unclear_time'),
    ('I have to leave at 4:05.', 'no_actionable_update', 'unclear_time'),
    ('I can carry 13 crates and another 9.', 'missing_units', 'missing_units'),
    ('I can carry 13.', 'missing_units', 'missing_units'),
    ('Maybe I need to finish before 5:15.', 'uncertain_offer', 'uncertain_offer'),
    ('Amara must leave by 6:20.', 'other_person', 'other_person'),
    ('I must leave by 6:20 UTC.', 'uncertain_offer', 'uncertain_offer'),
    ('I can carry 13 crates if approved.', 'missing_units', 'missing_units'),
    ('I can carry 13 crates?','missing_units','missing_units'),
])
def test_clarification_checks_never_create_a_draft(tmp_path, text, model_reason, expected):
    store, wid, mid = setup(tmp_path, text)
    before = store.read(wid)
    metrics = {}
    interpret(store, wid, mid, model=JsonModel(json.dumps({
        'decision': 'clarify', 'reason': model_reason,
    })), metrics=metrics)
    after = store.read(wid)
    assert after['suggestions'] == []
    assert all(before[k] == after[k] for k in ('resources','facts_version','status','plan','logistics'))
    response = after['messages'][-1]
    assert response['reason'] == expected
    assert response['model_reason'] == model_reason
    assert response['normalizer_version'] == 7
    assert response['clarification_validation'] == ('corrected' if expected != model_reason else 'unchanged')
    assert metrics['model_calls'] == 1
