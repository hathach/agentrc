#!/usr/bin/env python3
"""Tick CodeRabbit's "Trigger review" box on PR N when it skipped HEAD for a manual review.

  coderabbit_trigger.py --pr N --head SHA

A repository below CodeRabbit's open-source threshold gets, on each push, a
CodeRabbit status "Review skipped: manual review required ..." and an unticked
"Trigger review" box in its summary comment; ticking the box starts the review.
Run from the PR checkout. The outcome:

  ticked    the box was unticked; the comment was edited to tick it and read back
  already   the box is already ticked
  notSkipped  CodeRabbit's newest status on SHA is not that skip, cannot be told
            (undated, or a tie that reads differently), or SHA is not PR N's head:
            nothing to do
  failed    the edit failed, its read-back disagrees, or not exactly one
            CodeRabbit comment holds the box; detail says which

detail names the comment ("comment N") on ticked and already; at is the UTC
time the script finished. stdout ends with one JSON line {outcome, detail, at,
seal} (seal: facts.sealed). Exit 2 with
{"error": ...} when the arguments are wrong or GitHub's answer cannot be read.
A rerun after a lost line reports `already`.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, attempt, report  # noqa: E402
from harvest import gh_json, newest_reading, pages  # noqa: E402

SKIP = 'review skipped: manual review required'
BOX = re.compile(r'^(?P<pre>(?:> )?- \[)(?P<mark>[ xX])(?P<post>\][^\n]*Trigger (?:a manual )?review[^\n]*)$', re.M)


class Failed(Exception):
    pass


def skipped(pr, head):
    """None when HEAD is PR N's head and CodeRabbit's newest status on it is the manual-review skip, else why not."""
    if gh_json('api', f'repos/{{owner}}/{{repo}}/pulls/{pr}')['head']['sha'] != head:
        return f'{head[:9]} is not the head of PR {pr}'
    statuses = [s for s in pages(f'repos/{{owner}}/{{repo}}/commits/{head}/statuses') if s.get('context') == 'CodeRabbit']
    if not statuses:
        return 'no CodeRabbit status on the head'
    s, why = newest_reading(statuses, 'updated_at', lambda x: (x['state'], x.get('description')))
    if why:
        return f'CodeRabbit statuses: {why}'
    desc = s.get('description') or ''
    if s['state'] != 'success' or not desc.lower().startswith(SKIP):
        return f"CodeRabbit status is {s['state']} '{desc}'"
    return None


def trigger(pr, head):
    why = skipped(pr, head)
    if why:
        return {'outcome': 'notSkipped', 'detail': why}
    holders = [c for c in pages(f'repos/{{owner}}/{{repo}}/issues/{pr}/comments')
               if 'coderabbit' in ((c.get('user') or {}).get('login') or '').lower() and len(BOX.findall(c.get('body') or '')) == 1]
    if len(holders) != 1:
        raise Failed(f'{len(holders)} CodeRabbit comments hold one Trigger review box, not 1')
    c = holders[0]
    if BOX.search(c['body'])['mark'] != ' ':
        return {'outcome': 'already', 'detail': f"comment {c['id']}"}
    body = BOX.sub(lambda m: f"{m['pre']}x{m['post']}", c['body'])
    path = f'repos/{{owner}}/{{repo}}/issues/comments/{c["id"]}'
    # On stdin: a long summary comment can pass the kernel's 128 KiB limit on one argument.
    code, out, err = attempt('gh', 'api', '-X', 'PATCH', path, '--input', '-', input=json.dumps({'body': body}))
    if code:
        raise Failed(f'gh api {path}: {err.strip() or f"exit {code}"}')
    try:
        m = BOX.search(json.loads(out).get('body') or '')
    except (ValueError, AttributeError):
        m = None
    if not m or m['mark'] == ' ':
        raise Failed(f'comment {c["id"]} reads back without the box ticked')
    return {'outcome': 'ticked', 'detail': f"comment {c['id']}"}


def collect(argv):
    p = Parser(prog='coderabbit_trigger.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True)
    a = p.parse_args(argv)
    if not FULL_SHA.match(a.head):
        raise Unusable(f'not a full SHA: {a.head!r}')
    try:
        out = trigger(a.pr, a.head)
    except Failed as e:
        out = {'outcome': 'failed', 'detail': str(e)}
    return {**out, 'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
