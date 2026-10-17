#!/usr/bin/env python3
"""Score one paired repetition of replay.mjs, per finding against batched, on the corpus truth.

    score.py brief --corpus corpus.jsonl --single DIR --batch DIR > brief.md
    score.py report --corpus corpus.jsonl --single DIR --batch DIR [--evidence EV.json] [--out report.json]

`brief` renders, for every case batch mode verified in a batch, the two modes' reasons as A and B in an order
fixed by the case id, with the adjudicators' evidence as the reference, for a blind reviewer. The reviewer
returns a JSON array of {id, verdict, why}, one per case of the brief, verdict one of preserved, lost-A,
lost-B, lost-both or inconclusive: whether a reason drops a fact the reference shows the verdict rests on.

`report` prints the repetition's figures and its gate, and exits 1 unless every part of the gate passes:
- environment: both runs took every verifier under the one env.json, before and after, over the corpus being
  scored, byte for byte; each case was asked of the same models and efforts in both, and the models that
  answered, every dispatch naming its own, are the same;
- accounting: both runs hold exactly the corpus's cases, and no case per finding mode settled is left
  unverified in batch mode;
- correctness: on the settled truth, no case per finding mode gets right that batch mode gets wrong;
- cost: every dispatch reports a cost, and the batched cases cost at least 20% less in batch mode, its
  fallbacks and retries included;
- evidence: the blind review covers every batched case once and finds no batch-mode reason that lost its
  decisive fact, and none inconclusive (unscored, so failing, without --evidence).
"""
import argparse
import collections
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

CITES = re.compile(r'\S+\.\w+:\d+')
SAVING = 0.2
VERDICTS = ('preserved', 'lost-A', 'lost-B', 'lost-both', 'inconclusive')


def load(path):
    return [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]


def run(directory):
    """(records by case id, calls, env) of one replay output; a case recorded twice is refused."""
    d = Path(directory)
    rows = load(d / 'verdicts.jsonl')
    records = {v['id']: v for v in rows}
    if len(records) != len(rows):
        raise SystemExit(f'{d}: a case is recorded twice')
    return records, load(d / 'calls.jsonl'), json.loads((d / 'env.json').read_text(encoding='utf-8'))


def batched(calls):
    return {i for c in calls if c['label'].startswith('verify:b') for i in c['caseIds']}


def costed(calls):
    """Whether every dispatch reports a finite, nonnegative cost: a missing one is unknown, never zero."""
    return all(isinstance(c.get('cost'), (int, float)) and not isinstance(c['cost'], bool) and math.isfinite(c['cost']) and c['cost'] >= 0
               for c in calls)


def cost_by_case(calls):
    """Each dispatch's cost shared among the cases it carried."""
    out = collections.Counter()
    for c in calls:
        for i in c['caseIds']:
            out[i] += c['cost'] / len(c['caseIds'])
    return out


def letters(case_id):
    """{A: mode, B: mode}, fixed by the id so the brief and the report agree without a key file."""
    a, b = ('single', 'batch') if int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % 2 else ('batch', 'single')
    return {'A': a, 'B': b}


def reason(record):
    return (record.get('verdict') or {}).get('reason') or '(no verdict)'


def brief(corpus, single, batch):
    by_id = {c['id']: c for c in corpus}
    out = ['Compare two verifier reasons per finding against the reference evidence. For each, say whether a reason '
           'drops a fact the reference shows its verdict rests on: preserved when both keep it (or neither verdict needs '
           'it), lost-A, lost-B or lost-both for the one or two that lack it, inconclusive when you cannot tell. Judge '
           'evidence, not wording or length.\n'
           'Return only a JSON array, one entry per finding: [{"id": "<id>", "verdict": "preserved|lost-A|lost-B|lost-both|inconclusive", "why": "<one line>"}]\n']
    for i in sorted(batched(batch[1])):
        c, runs, key = by_id[i], {'single': single[0][i], 'batch': batch[0][i]}, letters(i)
        evidence = '\n'.join(f'- {ans["evidence"]}' for ans in c['truth']['answers'].values())
        out.append(f"\n## {i}\nfinding: {c['path']}:{c['finding']['line']}: {c['finding']['why']}\nreference evidence:\n{evidence}\n"
                   f"A ({runs[key['A']]['kind']}): {reason(runs[key['A']])}\nB ({runs[key['B']]['kind']}): {reason(runs[key['B']])}")
    return '\n'.join(out) + '\n'


def evidence_findings(expected, evidence):
    """{lost: {mode: [ids]}, inconclusive: [ids]} from the blind review of exactly the expected cases."""
    seen = collections.Counter(e.get('id') for e in evidence)
    bad = [e for e in evidence if e.get('verdict') not in VERDICTS or e.get('id') not in expected]
    if bad or any(n > 1 for n in seen.values()) or set(seen) != set(expected):
        raise SystemExit(f'evidence: want one review per batched case {sorted(expected)}; got bad {bad}, '
                         f'twice {sorted(i for i, n in seen.items() if n > 1)}, missing {sorted(set(expected) - set(seen))}')
    lost, inconclusive = collections.defaultdict(list), []
    for e in evidence:
        key = letters(e['id'])
        if e['verdict'] == 'inconclusive':
            inconclusive.append(e['id'])
        for letter in {'lost-A': 'A', 'lost-B': 'B', 'lost-both': 'AB'}.get(e['verdict'], ''):
            lost[key[letter]].append(e['id'])
    return {'lost': {m: sorted(ids) for m, ids in lost.items()}, 'inconclusive': sorted(inconclusive)}


def same_env(single, batch):
    """Why the two runs' environments differ, or None."""
    for name, (_, _, env) in (('single', single), ('batch', batch)):
        if env['before'] != env['after']:
            return f'{name} changed during its run: {sorted(k for k in env["before"] if env["before"][k] != env["after"].get(k))}'
    a, b = single[2]['before'], batch[2]['before']
    differ = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return f'the runs differ in {differ}' if differ else None


def same_models(single, batch):
    """Why the two runs' verifiers differ, or None: per case, the models and efforts asked and the models that answered."""
    if any(not c.get('models') for _, calls, _ in (single, batch) for c in calls):
        return 'a dispatch names no model that answered it'
    asked = lambda calls, i: sorted({(c['model'], c['effort']) for c in calls if i in c['caseIds']})
    answered = lambda calls, i: sorted({m for c in calls if i in c['caseIds'] for m in c['models']})
    cases = sorted({i for c in single[1] + batch[1] for i in c['caseIds']})
    for what, of in (('asked of other models or efforts', asked), ('answered by other models', answered)):
        differ = [i for i in cases if of(single[1], i) != of(batch[1], i)]
        if differ:
            return f'cases {what}: {differ}'
    return None


def report(corpus, single, batch, evidence=None, corpus_digest=None):
    """corpus_digest: sha256 of the corpus file scored, which both runs must have replayed."""
    ids = {c['id'] for c in corpus}
    modes = {'single': single, 'batch': batch}
    eligible = batched(batch[1])
    figures, wrong, said = {}, {}, {}
    for name, (records, calls, _) in modes.items():
        kinds = collections.Counter(r['kind'] for r in records.values())
        settled = [c for c in corpus if c['truth']['label'] is not None and c['id'] in records]
        said[name] = {i: {'confirmed': True, 'refuted': False}.get(r['kind']) for i, r in records.items()}
        wrong[name] = {c['id'] for c in settled if said[name][c['id']] is not None and said[name][c['id']] != (c['truth']['label'] == 'true')}
        costs = cost_by_case(calls) if costed(calls) else None
        figures[name] = {
            'records': len(records), 'kinds': dict(kinds), 'settled': len(settled),
            'right': sum(said[name][c['id']] is not None for c in settled) - len(wrong[name]), 'wrong': sorted(wrong[name]),
            'falseRefutations': sorted(i for i in wrong[name] if not said[name][i]), 'falseConfirmations': sorted(i for i in wrong[name] if said[name][i]),
            'citing': sum(bool(CITES.search(reason(r))) for r in records.values()),
            'cost': round(sum(c['cost'] for c in calls), 4) if costs is not None else None,
            'costBatched': round(sum(costs[i] for i in eligible), 4) if costs is not None else None,
            'dispatches': len(calls), 'retries': sum(c['label'].endswith('.retry') for c in calls),
            'models': sorted({(c['model'], c['effort']) for c in calls}), 'seconds': round(sum(c['durationMs'] for c in calls) / 1000),
            'byHardware': {str(h): {'cases': sum(r['routing']['hardware'] is h for r in records.values()),
                                    'right': sum(r['routing']['hardware'] is h and said[name][i] is not None and i not in wrong[name] for i, r in records.items())}
                           for h in (True, False, None)},
        }
    s, b = figures['single'], figures['batch']
    regressions = sorted(wrong['batch'] - wrong['single'])
    # coverage by case: a verdict per finding mode gave is not traded for another case's
    lost_coverage = sorted(i for i in ids & set(single[0]) & set(batch[0]) if said['single'][i] is not None and said['batch'][i] is None)
    review = evidence_findings(eligible, evidence) if evidence is not None else None
    env_why = same_env(single, batch) or same_models(single, batch) or (
        None if corpus_digest == single[2]['before']['corpus'] == batch[2]['before']['corpus'] else 'the corpus scored is not the one replayed')
    gate = {
        'environment': env_why is None and single[2]['before']['cases'] == batch[2]['before']['cases'] == sorted(ids),
        'accounting': set(single[0]) == set(batch[0]) == ids and not lost_coverage,
        'correctness': not regressions,
        'cost': s['costBatched'] is not None and b['costBatched'] is not None and bool(eligible) and b['costBatched'] <= (1 - SAVING) * s['costBatched'],
        'evidence': None if review is None else not review['lost'].get('batch') and not review['inconclusive'],
    }
    return {'cases': len(ids), 'batched': len(eligible), 'figures': figures, 'regressions': regressions,
            'improvements': sorted(wrong['single'] - wrong['batch']), 'lostCoverage': lost_coverage,
            'environment': env_why, 'evidence': review, 'gate': gate, 'pass': all(v is True for v in gate.values())}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('brief', 'report'):
        p = sub.add_parser(name)
        p.add_argument('--corpus', required=True)
        p.add_argument('--single', required=True)
        p.add_argument('--batch', required=True)
        if name == 'report':
            p.add_argument('--evidence', type=Path)
            p.add_argument('--out', type=Path)
    a = ap.parse_args(argv)
    corpus, single, batch = load(a.corpus), run(a.single), run(a.batch)
    if a.cmd == 'brief':
        sys.stdout.write(brief(corpus, single, batch))
        return 0
    r = report(corpus, single, batch, json.loads(a.evidence.read_text(encoding='utf-8')) if a.evidence else None,
               hashlib.sha256(Path(a.corpus).read_bytes()).hexdigest())
    text = json.dumps(r, indent=1)
    if a.out:
        a.out.write_text(text + '\n', encoding='utf-8', newline='\n')
    print(text)
    return 0 if r['pass'] else 1


if __name__ == '__main__':
    if os.name == 'nt':  # the brief quotes findings and verdicts
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
