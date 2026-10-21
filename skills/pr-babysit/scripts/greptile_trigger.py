#!/usr/bin/env python3
"""Ask Greptile to review PR N's head: post exactly `@greptileai` as a top-level comment, once per head.

  greptile_trigger.py --pr N --head SHA

A repository whose Greptile config reviews only on PR open has a pushed head
reviewed only on request. Run from the PR checkout. Each head's attempt is
recorded under the checkout's git common dir, in
pr-babysit/greptile/<owner>-<repo>-<N>-<SHA>.json: claimed, with the exact
`@greptileai` comments already on the PR, before the POST, and given the
comment as its receipt after, all under one lock held from the first check to
the receipt. Of two runs on one head at most one posts, and a run that finds a
claim without a receipt only looks for the comment that claim made. The outcome:

  requested  the comment was posted and read back, or, its POST answered with
             an error or not at all, found posted since the claim
  already    the record holds a receipt for SHA, an earlier claim's comment is
             found posted, or an exact `@greptileai` comment is on the PR after
             the head's arrival (harvest.py's head event, a reopen or ready
             event on the same SHA left out), then recorded as the receipt
  notOpen    the PR is not open at SHA, read again just before the claim:
             nothing posted
  failed     the head event time is unknown while an `@greptileai` comment
             stands, which cannot be placed before or after it: nothing posted
  ambiguous  no such comment is after the head event, and one is at the very
             same second, which cannot be placed either: nothing posted
  uncertain  this head was claimed and no comment of that claim can be found
             or read back: nothing is posted for SHA from this checkout again
             while the record stands; detail names it

detail names the comment ("comment N") on requested and already; at is that
comment's time on those, else the UTC time the script finished. stdout ends with
one JSON line {outcome, detail, at, seal} (seal: facts.sealed). Exit 2 with
{"error": ...} when the arguments are wrong or GitHub's answer cannot be read.
A rerun after a lost line reports `already`.
"""

import errno
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

sys.path.insert(0, str(Path(__file__).parent))
from coderabbit_trigger import unopened  # noqa: E402
from facts import FULL_SHA, Parser, Unusable, attempt, git, report  # noqa: E402
from harvest import gh_json, head_event, newest, pages, stamp  # noqa: E402

REQUEST = '@greptileai'


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def record_dir():
    common = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
    return common / 'pr-babysit' / 'greptile'


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'a') as fd:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
            return
        # msvcrt locks are mandatory and per handle: lock a byte past the content; LK_LOCK gives up after ~10 s.
        os.lseek(fd.fileno(), 1 << 30, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EDEADLOCK):
                    raise
                time.sleep(0.05)
        try:
            yield
        finally:
            # Windows may release a closed handle's lock only after a delay.
            os.lseek(fd.fileno(), 1 << 30, os.SEEK_SET)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


def read(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unusable(f'attempt record {path} unreadable: {e}')
    if not isinstance(record, dict) or not isinstance(record.get('seen'), list) or not isinstance(record.get('claimedAt'), str):
        raise Unusable(f'attempt record {path} is not one this script wrote')
    return record


def claim(path, seen):
    record = {'claimedAt': now(), 'seen': seen}
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Unusable(f'attempt record {path} appeared under the lock: another writer ignores it')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(record, f)
    return record


def receipt(path, record, comment, at):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({**record, 'receipt': {'id': comment, 'at': at}}), encoding='utf-8')
    os.replace(tmp, path)


def asks(pr):
    return [c for c in pages(f'repos/{{owner}}/{{repo}}/issues/{pr}/comments') if (c.get('body') or '').strip() == REQUEST]


def uncertain(path, why):
    return {'outcome': 'uncertain', 'detail': f'{why}; nothing is posted for this head from this checkout while {path} stands'}


def found(path, record, outcome, pr, why):
    """The comment a claim made: an exact one not among those it saw, recorded as its receipt."""
    new = [c for c in asks(pr) if c['id'] not in record['seen']]
    if not new:
        return uncertain(path, why)
    c = min(new, key=lambda c: stamp(c['created_at']))
    receipt(path, record, c['id'], c['created_at'])
    return {'outcome': outcome, 'detail': f"comment {c['id']}", 'at': c['created_at']}


def request(pr, head):
    why = unopened(pr, head)
    if why:
        return {'outcome': 'notOpen', 'detail': why}
    repo = gh_json('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner']
    path = record_dir() / f"{repo.replace('/', '-')}-{pr}-{head}.json"
    with locked(path):
        record = read(path)
        if record and record.get('receipt'):
            r = record['receipt']
            return {'outcome': 'already', 'detail': f"comment {r['id']}", 'at': r['at']}
        if record:
            return found(path, record, 'already', pr, f"an attempt claimed this head at {record['claimedAt']} and its comment is not on the PR")
        standing = asks(pr)
        view = gh_json('pr', 'view', str(pr), '--json', 'headRefOid,headRefName,headRepository')
        event_at, evidence = head_event(repo, pr, view, head, reopens=False)
        if standing and event_at is None:
            return {'outcome': 'failed', 'detail': f"the head event time is unknown ({evidence}), so comment {standing[-1]['id']} "
                                                   'cannot be placed before or after it: nothing posted'}
        after = [c for c in standing if stamp(c['created_at']) > stamp(event_at)]
        if after:
            c = newest(after, 'created_at')
            receipt(path, {'claimedAt': now(), 'seen': [x['id'] for x in standing]}, c['id'], c['created_at'])
            return {'outcome': 'already', 'detail': f"comment {c['id']}", 'at': c['created_at']}
        tied = [c for c in standing if stamp(c['created_at']) == stamp(event_at)]
        if tied:
            return {'outcome': 'ambiguous', 'detail': f"comment {tied[0]['id']} was made at {event_at}, the head event's own second ({evidence}): "
                                                      'nothing posted'}
        # The reads above take a while: a PR closed or pushed meanwhile is not asked about.
        why = unopened(pr, head)
        if why:
            return {'outcome': 'notOpen', 'detail': why}
        mine = claim(path, [c['id'] for c in standing])
        code, out, err = attempt('gh', 'api', '-X', 'POST', f'repos/{{owner}}/{{repo}}/issues/{pr}/comments', '-f', f'body={REQUEST}')
        try:
            posted = None if code else json.loads(out)['id']
        except (ValueError, KeyError, TypeError):
            posted = None
        if posted is None:
            # An error or a lost answer does not say the comment was not made: look for it rather than post again.
            answer = (err.strip() or f'exit {code}') if code else 'an unreadable line'
            return found(path, mine, 'requested', pr, f'the POST answered {answer}, and no new comment is on the PR')
        try:
            back = gh_json('api', f'repos/{{owner}}/{{repo}}/issues/comments/{posted}')
        except Unusable as e:
            return uncertain(path, f'comment {posted} was posted and does not read back ({e})')
        if (back.get('body') or '').strip() != REQUEST:
            return uncertain(path, f'comment {posted} reads back as another body')
        receipt(path, mine, posted, back['created_at'])
        return {'outcome': 'requested', 'detail': f'comment {posted}', 'at': back['created_at']}


def collect(argv):
    p = Parser(prog='greptile_trigger.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True)
    a = p.parse_args(argv)
    if not FULL_SHA.match(a.head):
        raise Unusable(f'not a full SHA: {a.head!r}')
    try:
        return {'at': now(), **request(a.pr, a.head)}
    except (KeyError, TypeError, AttributeError) as e:
        raise Unusable(f'gh answered in a shape greptile_trigger.py does not read ({e!r})')


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
