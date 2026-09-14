"""A model time draft only constrains routing after authorized review."""
import json
from copy import deepcopy

from test_extraction import JsonModel

from relay_core import agent
from scripts.rehearse_commitment_failure import Rehearsal


def test_reviewed_time_limit_invalidates_old_acceptances_and_blocks_infeasible_dispatch(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path / 'time-review.sqlite3')
    try:
        r.command('coordinator', 'propose')
        for rid in r.read()['plan']['required']:
            r.command(rid, 'accept', resource_id=rid, revision=1)
        before = deepcopy(r.read())
        text = 'I must finish by minute 25.'
        monkeypatch.setenv('RELAY_MODEL_ID', 'offline-fixture')
        monkeypatch.setattr(agent, 'BedrockModel', lambda **kw: JsonModel(json.dumps({
            'decision':'draft', 'closes_text':'minute 25', 'evidence_quote':text,
        })))
        response = r.client.post(f'/workspaces/{r.wid}/messages',
                                 json={'resource_id':'tunde','text':text},
                                 headers={'Authorization':'Bearer '+r.tokens['tunde']})
        assert response.status_code == 200
        state = r.read()
        assert all(before[k] == state[k] for k in ('resources','logistics','plan','facts_version'))
        draft = state['suggestions'][0]
        assert draft['prompt_version'] == agent.PROMPT_VERSION
        r.command('coordinator', 'apply_suggestion', suggestion_id=draft['id'])
        assert r.read()['logistics']['participants']['tunde']['window']['closes'] == 25
        r.command('coordinator','approve',revision=1,expected=409)
        r.command('coordinator','propose',expected=409)
        assert r.read()['status'] != 'committed'
        assert not r.active_reservations()
    finally:
        r.client.close()
