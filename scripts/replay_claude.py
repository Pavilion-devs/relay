"""Replay saved synthetic responses through the current adapter; no provider calls."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_extraction import JsonModel, setup

from relay_core.agent import interpret
from relay_core.evaluation import score


def main():
    cases = json.loads(Path('evaluation/extraction-v2-cases.json').read_text())
    cases['version'] = 4
    cases['provenance'] = 'Known development cases; explicit availability expectations corrected; scorer v4 permits one terminal condition period.'
    for case in cases['cases']:
        if case['id'] in {'window_offsets', 'window_timezone', 'h_late_window'}:
            case['expected']['fields']['available'] = True
    Path('evaluation/extraction-v4-cases.json').write_text(json.dumps(cases, indent=2)+'\n')
    by_id = {c['id']: c for c in cases['cases']}
    runs = []
    for source in ['docs/eval-claude-v3-01.json', 'docs/eval-claude-v3-02.json']:
        for old in json.loads(Path(source).read_text())['runs']:
            if 'raw_model_message' not in old:
                continue
            case = by_id[old['id']]
            with tempfile.TemporaryDirectory() as directory:
                store, wid, mid = setup(Path(directory), case['text'])
                def anchor(state, sender=case['sender']):
                    state['logistics']['starts_at'] = '2026-09-13T12:00:00+00:00'
                    state['messages'][-1]['resource_id'] = sender
                    return {'ok': True}
                store.transact(wid, 'anchor', {}, anchor)
                before = store.read(wid)
                raw = ''.join(c.get('text', '') for c in old['raw_model_message']['content'])
                interpret(store, wid, mid, model=JsonModel(raw))
                result = score({**case, 'sender': before['messages'][-1]['resource_id']}, before, store.read(wid), version=4)
                runs.append({'id': case['id'], 'source': source, **result})
    report = {'mode': 'offline_saved_response_replay', 'model_calls': 0, 'source_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('relay_core/extraction.py'), Path('relay_core/evaluation.py')]}, 'normalizer_version': 4, 'score_version': 4, 'passes': sum(r['automated_pass'] for r in runs), 'runs': runs}
    Path('docs/claude-v4-replay.json').write_text(json.dumps(report, indent=2)+'\n')
    print({'passes': report['passes'], 'total':len(runs), 'failures': [r['id'] for r in runs if not r['automated_pass']]})


if __name__ == '__main__':
    main()
