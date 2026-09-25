#!/usr/bin/env python3
"""One pr-review launch, condensed for its caller from the saved Workflow output.

  result.py --output FILE

Counts, never bodies: the status and reason, the head and mode, the proposed
event and why, findings by status and severity, coverage lost (dropped scan
units, unverified findings, claims left unjudged), the CI and HIL evidence the
verdict used, and the draft's size. Every count is labelled with the finding
statuses the workflow uses (open, fixed, covered, withdrawn, na) and the
verdicts of the thread claims (confirmed, refuted, stale). The draft's text
stays in the file for the human, and on the ledger once saved.

stdout ends with one JSON line; {"error": ...}, exit 2, when FILE is not a
pr-review Workflow output.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import Parser, Unusable, report  # noqa: E402
from launch_result import load_output  # noqa: E402


def condense(r):
    if r.get('status') != 'reviewed':
        return {'status': r.get('status'), 'reason': r.get('reason'), 'detail': r.get('detail')}
    findings = r.get('findings', [])
    open_ = [f for f in findings if f.get('status') == 'open']
    draft = r.get('draft') or {}
    return {
        'status': 'reviewed', 'pr': r['pr'], 'head': r['head'], 'mode': r.get('mode'),
        'event': (r.get('verdict') or {}).get('event'), 'reasons': (r.get('verdict') or {}).get('reasons', []),
        'findings': dict(Counter(f.get('status') for f in findings)),
        'openBySeverity': dict(Counter(f.get('severity') for f in open_)),
        'claims': dict(Counter(c.get('verdict') for c in r.get('claims', []))),
        'coverage': {k: len((r.get('coverage') or {}).get(k, [])) for k in ('dropped', 'unverified', 'unjudged')},
        'ci': (r.get('ci') or {}).get('state'), 'hil': r.get('hil'),
        'draft': {'inline': len(draft.get('comments', [])), 'replies': len(draft.get('replies', [])),
                  'bodyChars': len(draft.get('body', ''))},
    }


def collect(argv):
    p = Parser(prog='result.py')
    p.add_argument('--output', required=True)
    a = p.parse_args(argv)
    out = load_output(a.output)
    if out is None:
        return {'status': 'no-result', 'reason': 'the launch returned nothing (it threw or was stopped)'}
    r = out.get('result')
    if not isinstance(r, dict) or 'status' not in r:
        raise Unusable('--output holds no pr-review result')
    return condense(r)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
