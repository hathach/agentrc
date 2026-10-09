#!/usr/bin/env python3
"""Pick a stratified sample of extracted cases for adjudication, in clusters a batched verifier would see together.

A cluster is up to --size findings on one file of one pinned head, each within --span lines of the previous
one, hardware claims apart from the rest: the shared source context batching would group by, and the
separation #64 keeps. Clusters are drawn in a seeded shuffle: first one per PR, then those that advance a
quota short of its target, then any, until the sample holds --total cases. A sample short of its total, a
quota or a PR fails; the seed and counts are printed so a sample can be drawn again.

    sample.py --cases cases.jsonl --out sample.jsonl --seed N --total 60
"""
import argparse
import collections
import json
import random
import re
import sys
from pathlib import Path

# a claim that rests on how the hardware behaves, by its own text
HARDWARE = re.compile(r'datasheet|errat|reference manual|user manual|\bregister|W1C|write-1-to-clear|silicon|read-doc|'
                      r'\b(?:UM|RM|AN|DS)\d{4,}', re.I)
# code-audit.js's `graded`: a confirmed verdict missing any of these is a partial response
LEVELS = ('critical', 'high', 'medium', 'low', 'nit')
CONFIDENCE = ('high', 'medium', 'low')
IMPACT = ('consequence', 'path', 'variants', 'recovery')


def graded(v):
    impact = v.get('impact') or {}
    return v.get('severity') in LEVELS and v.get('confidence') in CONFIDENCE and bool(v.get('severityReason')) \
        and all(impact.get(k) for k in IMPACT)


def tags(case):
    v, f = case['verifier'], case['finding']
    out = {'confirmed' if v.get('real') else 'refuted'}
    if HARDWARE.search(f.get('why', '') + '\n' + f.get('snippet', '')):
        out.add('hardware')
    if any('sonnet' in m for m in v.get('models') or []):
        out.add('sonnet')
    if v.get('real') and not graded(v):
        out.add('ungraded')
    return out


def clusters(cases, tag, span, size):
    """tag: each case's tags by id."""
    by = collections.defaultdict(list)
    for c in cases:
        by[(c['repo'], c['head'], c['finding']['file'], 'hardware' in tag[c['id']])].append(c)
    out = []
    for group in by.values():
        group.sort(key=lambda c: (c['finding']['line'], c['id']))
        cur = []
        for c in group:
            if cur and (c['finding']['line'] - cur[-1]['finding']['line'] > span or len(cur) == size):
                out.append(cur)
                cur = []
            cur.append(c)
        out.append(cur)
    return out


def counts(cl, tag):
    """Cases per tag in one cluster, with `multi` for a cluster of several and `duplicate` for those sharing a line."""
    out = collections.Counter(t for c in cl for t in tag[c['id']])
    if len(cl) > 1:
        out['multi'] = len(cl)
    lines = collections.Counter(c['finding']['line'] for c in cl)
    out['duplicate'] = sum(n for n in lines.values() if n > 1)
    return out


def pr_of(c):
    return f"{c['repo']}#{c['pr']}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--cases', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seed', type=int, required=True)
    ap.add_argument('--total', type=int, required=True)
    ap.add_argument('--span', type=int, default=15)
    ap.add_argument('--size', type=int, default=4)
    a = ap.parse_args(argv)
    cases = [json.loads(l) for l in a.cases.read_text().splitlines()]
    tag = {c['id']: tags(c) for c in cases}
    pool = clusters(cases, tag, a.span, a.size)
    random.Random(a.seed).shuffle(pool)
    count = {id(cl): counts(cl, tag) for cl in pool}
    quota = {'refuted': a.total * 2 // 5, 'confirmed': a.total * 2 // 5, 'hardware': a.total // 4, 'sonnet': a.total // 10,
             'ungraded': 2, 'multi': a.total * 3 // 5, 'duplicate': a.total // 6}
    prs = collections.Counter(map(pr_of, cases))
    picked, have, per_pr = [], collections.Counter(), collections.Counter()

    def take(cl):
        pool.remove(cl)
        picked.append(cl)
        have.update(count[id(cl)])
        per_pr.update(map(pr_of, cl))

    def room(cl):
        # each PR's share follows its size, at least one case
        return all(per_pr[p] < max(1, round(a.total * prs[p] / len(cases))) for p in map(pr_of, cl))

    size = lambda: sum(map(len, picked))
    for p in prs:
        if per_pr[p]:
            continue
        cl = next((cl for cl in pool if p in map(pr_of, cl)), None)
        if cl:
            take(cl)
    # a cluster holding a short quota's rarest tag first, so the total does not fill before one turns up
    while size() < a.total:
        short = sorted((k for k, q in quota.items() if have[k] < q), key=lambda k: sum(count[id(cl)][k] > 0 for cl in pool))
        cl = next((cl for k in short for cl in pool if count[id(cl)][k] and room(cl)), None)
        if not cl:
            break
        take(cl)
    for cl in list(pool):
        if size() >= a.total:
            break
        if room(cl):
            take(cl)
    sample = [{**c, 'cluster': i, 'tags': sorted(tag[c['id']])} for i, cl in enumerate(picked) for c in cl]
    with a.out.open('w') as f:
        for c in sample:
            f.write(json.dumps(c) + '\n')
    print(f'{len(sample)}/{a.total} cases in {len(picked)} clusters, seed {a.seed}: ' +
          ', '.join(f'{k} {have[k]}/{q}' for k, q in quota.items()))
    print('per PR: ' + ', '.join(f'{k} {v}' for k, v in sorted(per_pr.items())))
    problems = [f'{k} {have[k]}/{q}' for k, q in quota.items() if have[k] < q]
    problems += [f'{p} not sampled' for p in prs if not per_pr[p]]
    if len(sample) < a.total:
        problems.append(f'{len(sample)} of {a.total} cases')
    if problems:
        print('short: ' + ', '.join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
