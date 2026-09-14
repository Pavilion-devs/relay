"""Replay completed synthetic model outputs with current normalization and frozen scoring."""
import argparse
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    raw = args.dataset.read_bytes()
    if args.output.exists() or report['status'] != 'complete':
        raise ValueError('Need a completed report and a new output path')
    assert report['dataset_sha256'] == hashlib.sha256(raw).hexdigest()
    cases = {c['id']: c for c in json.loads(raw)['cases']}
    runs=[]
    for old in report['runs']:
        case=cases[old['id']]
        with tempfile.TemporaryDirectory() as directory:
            store,wid,mid=setup(Path(directory),case['text'])
            def anchor(state, sender=case['sender']):
                state['logistics']['starts_at']='2026-09-13T12:00:00+00:00'
                state['messages'][-1]['resource_id']=sender
                return {'ok':True}
            store.transact(wid,'anchor',{},anchor)
            before=store.read(wid)
            output=''.join(c.get('text','') for c in old['raw_model_message']['content'])
            metrics={}
            interpret(store,wid,mid,model=JsonModel(output),metrics=metrics)
            runs.append({'id':case['id'],'metrics':metrics,**score(case,before,store.read(wid),version=4)})
    result={'mode':'offline_saved_response_replay','provider_calls':0,'source_report':str(args.report),
            'dataset_sha256':report['dataset_sha256'],'passes':sum(r['automated_pass'] for r in runs),
            'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('relay_core/agent.py'),Path('relay_core/extraction.py'),Path('relay_core/evaluation.py')]},'runs':runs}
    with args.output.open('x') as f:
        json.dump(result,f,indent=2)
        f.write('\n')
    print(json.dumps({'passes':result['passes'],'total':len(runs),'failed':[r['id'] for r in runs if not r['automated_pass']]}))


if __name__ == '__main__':
    main()
