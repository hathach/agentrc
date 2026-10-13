#!/usr/bin/env python3
"""Tick CodeRabbit's "Trigger review" box on PR N when it skipped HEAD for a manual review,
or, with --request, ask it to review once its rate limit on HEAD has reset.

  coderabbit_trigger.py --pr N --head SHA [--request]

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

With --request, CodeRabbit is read as harvest.py reads it, and a rate-limit
notice leaves no box to tick (CodeRabbit's docs: comment `@coderabbitai review`
after the reset). The outcome:

  requested   it is limited on HEAD, its reset has passed, and the comment
              was posted and read back
  already     such a comment was posted at or after the reset
  notDue      the reset is still ahead (retryAt) or no reset time can be read
              (retryAt null): nothing posted
  notLimited  the PR is not open, SHA is not its head (both read again just
              before posting), or CodeRabbit is not rate-limited on it
  failed      the post failed or reads back otherwise

and the line adds retryAt, the reset it read, null when it read none.

detail names the comment ("comment N") on ticked, already and requested; at is
the UTC time the script finished, or the request comment's time on requested
and already. stdout ends with one JSON line {outcome, detail, at, seal} (seal:
facts.sealed). Exit 2 with {"error": ...} when the arguments are wrong or
GitHub's answer cannot be read. A rerun after a lost line reports `already`.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, attempt, report  # noqa: E402
from harvest import gh_json, newest_reading, observe, pages, stamp  # noqa: E402

SKIP = 'review skipped: manual review required'
REQUEST = '@coderabbitai review'
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


def unopened(pr, head):
    """None when PR N is open at HEAD, else why not."""
    pull = gh_json('api', f'repos/{{owner}}/{{repo}}/pulls/{pr}')
    if pull['state'] != 'open' or pull['head']['sha'] != head:
        return f"PR {pr} is {pull['state']} at {pull['head']['sha'][:9]}, not open at {head[:9]}"
    return None


def request(pr, head):
    why = unopened(pr, head)
    if why:
        return {'outcome': 'notLimited', 'detail': why}
    seen = observe(gh_json('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner'], pr, ['coderabbit'], ['coderabbit'])
    cr = seen['bots'][0]
    if seen['headSha'] != head or (cr['state'], cr['kind']) != ('settled', 'limited'):
        return {'outcome': 'notLimited', 'detail': f"CodeRabbit is {cr['state']}{f'/' + cr['kind'] if cr['kind'] else ''} on {seen['headSha'][:9]}: {cr['reason']}"}
    reset = {'retryAt': cr['retryAt']}
    if cr['retryAt'] is None or stamp(cr['retryAt']) > stamp(seen['observedAt']):
        return {'outcome': 'notDue', 'detail': cr['reason'], **reset}
    path = f'repos/{{owner}}/{{repo}}/issues/{pr}/comments'
    asked = [c for c in pages(path) if (c.get('body') or '').strip() == REQUEST and stamp(c['created_at']) >= stamp(cr['retryAt'])]
    if asked:
        return {'outcome': 'already', 'detail': f"comment {asked[0]['id']}", 'at': asked[0]['created_at'], **reset}
    # The reads above take a while: a PR closed or pushed meanwhile is not asked about.
    why = unopened(pr, head)
    if why:
        return {'outcome': 'notLimited', 'detail': why}
    code, out, err = attempt('gh', 'api', '-X', 'POST', path, '-f', f'body={REQUEST}')
    if code:
        raise Failed(f'gh api {path}: {err.strip() or f"exit {code}"}')
    try:
        posted = json.loads(out)
        back = gh_json('api', f"repos/{{owner}}/{{repo}}/issues/comments/{posted['id']}")
    except (ValueError, KeyError, TypeError, Unusable) as e:
        raise Failed(f'the posted request does not read back ({e})')
    if (back.get('body') or '').strip() != REQUEST:
        raise Failed(f"comment {posted['id']} reads back as another body")
    return {'outcome': 'requested', 'detail': f"comment {posted['id']}", 'at': back['created_at'], **reset}


def collect(argv):
    p = Parser(prog='coderabbit_trigger.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--request', action='store_true')
    a = p.parse_args(argv)
    if not FULL_SHA.match(a.head):
        raise Unusable(f'not a full SHA: {a.head!r}')
    try:
        out = request(a.pr, a.head) if a.request else trigger(a.pr, a.head)
    except Failed as e:
        out = {'outcome': 'failed', 'detail': str(e)}
    if a.request:
        out = {'retryAt': None, **out}
    return {'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), **out}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
