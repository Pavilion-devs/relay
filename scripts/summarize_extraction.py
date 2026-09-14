"""Report quality and safety categories separately from a completed frozen evaluation."""
import argparse
import hashlib
import json
from pathlib import Path


def summarize(report, dataset):
    if report['status'] != 'complete':
        raise ValueError('Evaluation must complete before scoring release gates')
    cases = {c['id']: c for c in dataset['cases']}
    runs = report['runs']
    if {r['id'] for r in runs} != set(cases) or len(runs) != len(cases):
        raise ValueError('Every case must have exactly one completed run')
    drafts = [r for r in runs if r['expected']['kind'] == 'draft']
    clarifications = [r for r in runs if r['expected']['kind'] == 'clarification']
    rejected = [r['id'] for r in drafts if not r.get('drafts')]
    unexpected = [r['id'] for r in clarifications if r.get('drafts')]
    changed = [r['id'] for r in runs if not r['checks']['confirmed_facts_unchanged']]
    failed = [r['id'] for r in runs if not r['automated_pass']]
    limits = dataset['acceptance_gates']
    gates = {
        'confirmed_state_changes': len(changed) <= limits['confirmed_state_changes'],
        'drafts_on_clarification_cases': len(unexpected) <= limits['drafts_on_clarification_cases'],
        'valid_draft_rejections': len(rejected)/len(drafts) <= limits['valid_draft_rejection_fraction_max'],
    }
    return {'total': len(runs), 'strict_passes': len(runs)-len(failed), 'failed_cases': failed,
                'valid_draft_cases': len(drafts), 'valid_draft_rejections': rejected,
                'expected_clarification_cases': len(clarifications), 'unexpected_drafts': unexpected,
                'confirmed_state_changes': changed, 'predeclared_gates': gates,
                'all_predeclared_gates_pass': all(gates.values()),
                'caveat': 'Synthetic same-author development evidence. Incorrect reviewable drafts are not executed commitments. Gate passing is not production or practitioner validation.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args=p.parse_args()
    raw=args.dataset.read_bytes()
    report=json.loads(args.report.read_text())
    assert report['dataset_sha256'] == hashlib.sha256(raw).hexdigest()
    result=summarize(report,json.loads(raw))
    result.update(report=str(args.report), dataset_sha256=report['dataset_sha256'])
    with args.output.open('x') as f:
        json.dump(result,f,indent=2)
        f.write('\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
