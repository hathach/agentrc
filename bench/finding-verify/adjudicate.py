#!/usr/bin/env python3
"""Blind adjudication of sampled cases: render the brief, then merge two adjudicators' answers into the corpus.

    adjudicate.py brief --sample sample.jsonl --clusters 0-4 --checkout owner/repo=PATH ...   # without the verifier's verdict
    adjudicate.py merge --sample sample.jsonl --a NAME=a.json --b NAME=b.json [--lead NAME=lead.json] --out corpus.jsonl

An answer file is a JSON array of {id, label: true|false|unclear, evidence, needs: none|document|hardware}, exactly
one per case of the clusters it covers; several files for one adjudicator are joined with commas. Two agreeing
true/false labels settle a case. The lead's true/false answer settles one they split on or either left unclear; a
case it leaves unclear or does not answer stays disputed, without a label. Each answer is kept under its
adjudicator's name, the names distinct.
"""
import argparse
import collections
import json
import sys
from pathlib import Path

LABELS = ('true', 'false', 'unclear')
NEEDS = ('none', 'document', 'hardware')

RULES = """\
You are adjudicating review findings for a ground-truth corpus. Each finding below is a claim a code reviewer
made about one pull request, pinned to a head commit. Decide, for each one, whether the claim is TRUE or FALSE as
stated, reading the code yourself:

- TRUE: the defect exists at the pinned head as the finding describes it, and the diff from base to head
  introduces it or breaks something into it. Severity does not matter: a real nit is TRUE.
- FALSE: the code does not do what the finding says, the finding misreads it, the consequence cannot happen, or
  the defect was already there at base and the diff neither introduces nor worsens it.
- UNCLEAR: deciding needs evidence you cannot get here (behaviour only a board shows, a document you cannot
  find). Say exactly what would decide it.

Read the code at the pinned commits only, never a working tree: `git -C <repo> show <head>:<path>`,
`git -C <repo> diff <base> <head> -- <dir>`, `git -C <repo> grep -n <pattern> <head>`. For a claim about hardware
or protocol behaviour, use the `read-doc` skill and cite the document and page; name a missing document rather
than answering from memory. Findings in one cluster sit close together in one file: judge each on its own.
Quote the lines your evidence rests on, as path:line at the head.

Return only a JSON array, one object per finding, in this order:
[{"id": "<id>", "label": "true|false|unclear", "evidence": "<path:line quotes and reasoning>", "needs": "none|document|hardware"}]
`needs` names what the decision rested on beyond the code: a document you read, or hardware you could not run.
"""


def load(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def span(text):
    lo, _, hi = text.partition('-')
    return set(range(int(lo), int(hi or lo) + 1))


def brief(sample, clusters, checkouts):
    """checkouts: {owner/repo: local clone holding the pinned commits}."""
    picked = [c for c in sample if c['cluster'] in clusters]
    if not picked:
        raise SystemExit(f'no case in clusters {sorted(clusters)}')
    missing = sorted({c['repo'] for c in picked} - set(checkouts))
    if missing:
        raise SystemExit(f'no --checkout for {", ".join(missing)}')
    out = [RULES]
    for c in picked:
        f = c['finding']
        out.append(f"\n## {c['id']} (cluster {c['cluster']})\n"
                   f"repo {checkouts[c['repo']]}  base {c['base']}  head {c['head']}  dir {c['dir']}\n"
                   f"review dimension: {c['dimension']}\n"
                   f"finding: {c['path']}:{f['line']}\n"
                   f"snippet:\n{f.get('snippet', '')}\n"
                   f"claim: {f['why']}" +
                   # the documents the scanner cited are leads the verifier got too
                   (f"\ndocuments the reviewer cited: {json.dumps(f['docs'])}" if f.get('docs') else ''))
    return '\n'.join(out) + '\n'


def answers(spec, ids):
    """(name, {id: answer}) from NAME=file[,file...]; every answer well formed, none twice, none unknown."""
    name, _, files = spec.partition('=')
    if not name or not files:
        raise SystemExit(f'{spec!r}: expected NAME=file[,file...]')
    got = {}
    for path in files.split(','):
        for a in json.loads(Path(path).read_text()):
            checks = ((a.get('id') in ids, 'unknown id'), (a.get('id') not in got, 'answered twice'),
                      (a.get('label') in LABELS, f'label not one of {LABELS}'), (a.get('needs') in NEEDS, f'needs not one of {NEEDS}'),
                      (isinstance(a.get('evidence'), str) and a['evidence'].strip(), 'no evidence'))
            problem = next((why for ok, why in checks if not ok), None)
            if problem:
                raise SystemExit(f'{name}: {a.get("id")}: {problem}')
            got[a['id']] = {k: a[k] for k in ('label', 'evidence', 'needs')}
    return name, got


def settle(a, b, lead):
    if a['label'] == b['label'] and a['label'] != 'unclear':
        return 'agreed', a['label']
    if lead and lead['label'] != 'unclear':
        return 'lead', lead['label']
    return 'disputed', None


def merge(sample, a_spec, b_spec, lead_spec):
    ids = {c['id'] for c in sample}
    (na, a), (nb, b) = answers(a_spec, ids), answers(b_spec, ids)
    nl, lead = answers(lead_spec, ids) if lead_spec else (None, {})
    names = [n for n in (na, nb, nl) if n]
    if len(set(names)) != len(names):
        raise SystemExit(f'adjudicator names must be distinct: {names}')
    if set(a) ^ set(b):
        raise SystemExit(f'{na} and {nb} answer different cases: {sorted(set(a) ^ set(b))}')
    out = []
    for c in sample:
        if c['id'] not in a:
            continue
        status, label = settle(a[c['id']], b[c['id']], lead.get(c['id']))
        given = {na: a[c['id']], nb: b[c['id']], **({nl: lead[c['id']]} if c['id'] in lead else {})}
        # the journal path names a private local session; the unit label is enough to tell cases apart
        out.append({**c, 'source': {'label': c['source']['label']}, 'truth': {'label': label, 'status': status, 'answers': given}})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('brief')
    b.add_argument('--sample', required=True)
    b.add_argument('--clusters', required=True, type=span, help='N or N-M')
    b.add_argument('--checkout', action='append', default=[], metavar='OWNER/REPO=PATH', help='a local clone, once per repository')
    m = sub.add_parser('merge')
    m.add_argument('--sample', required=True)
    m.add_argument('--a', required=True)
    m.add_argument('--b', required=True)
    m.add_argument('--lead')
    m.add_argument('--out', required=True, type=Path)
    a = ap.parse_args(argv)
    sample = load(a.sample)
    if a.cmd == 'brief':
        checkouts = dict(c.split('=', 1) for c in a.checkout if '=' in c)
        if len(checkouts) != len(a.checkout):
            raise SystemExit('--checkout takes OWNER/REPO=PATH')
        sys.stdout.write(brief(sample, a.clusters, checkouts))
        return 0
    corpus = merge(sample, a.a, a.b, a.lead)
    with a.out.open('w') as f:
        for c in corpus:
            f.write(json.dumps(c) + '\n')
    by = collections.Counter(c['truth']['status'] for c in corpus)
    print(f'{len(corpus)} cases -> {a.out}: ' + ', '.join(f'{k} {v}' for k, v in sorted(by.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
