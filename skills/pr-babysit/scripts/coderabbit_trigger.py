#!/usr/bin/env python3
"""Tick CodeRabbit's "Trigger review" box on PR N when it skipped HEAD for a manual review,
or, with --request, ask it to review once its rate limit on HEAD has reset, or when no tick asked for a skipped HEAD.

  coderabbit_trigger.py --pr N --head SHA [--request [--not-before ISO]] [--repo OWNER/NAME]

A repository below CodeRabbit's open-source threshold gets, on each push, a
CodeRabbit status "Review skipped: manual review required ...", or a skipped check
run while its summary comment holds an unticked "Trigger review" box or says the
repository gets no automatic reviews, read as harvest.py reads them; ticking the
box starts the review.
Run from the PR checkout; PR N is --repo's, else the checkout's one repository
(facts.bind_repo). The outcome:

  ticked    the box was unticked; the comment was edited to tick it and read back
  already   the box is already ticked
  notSkipped  CodeRabbit's newest status or check run on SHA is not that skip,
            cannot be told (undated, or a tie that reads differently), or SHA is
            not PR N's head: nothing to do
  failed    the edit failed, its read-back disagrees, or not exactly one
            CodeRabbit comment holds the box; detail says which

With --request, CodeRabbit is read as harvest.py reads it, and a rate-limit
notice leaves no box to tick (CodeRabbit's docs: comment `@coderabbitai review`
after the reset); nor does a manual-review skip whose summary lost its box, or
whose tick failed (#274). The outcome:

  requested   it is limited on HEAD and its reset plus a minute has passed, or
              it skipped HEAD for a manual review and no box is ticked; the
              comment was posted and read back
  already     such a comment was posted at or after the reset, or, on a skip,
              at or after the head event (harvest.py's), or the box is ticked
  notDue      the reset plus a minute is still ahead (retryAt), or no reset
              time can be read (retryAt null) and no --not-before stands in:
              nothing posted

--not-before, the caller's own deadline for this limit (pr-babysit's: an hour
after its last request, or after it first saw a limit with no reset time),
moves the reset later, never earlier: a request is posted only once both have
passed, and only one posted at or after both counts as already.
  notLimited  the PR is not open, SHA is not its head (both read again just
              before posting), or CodeRabbit is neither rate-limited on it nor
              skipped it for a manual review
  failed      a read before the post, the post or its read-back failed, or on
              a skip a standing comment cannot be placed against an unknown
              head event time

and the line adds retryAt, the reset it read, null when it read none. A failed
line adds phase: `read` when it failed before the POST, so nothing was posted;
`post` when the POST or its read-back failed, so the comment may stand.

detail names the comment ("comment N") on ticked, already and requested, and
commentId is its id there; at is the UTC time the script finished, or the
request comment's time on requested and already. stdout ends with one JSON line
{outcome, detail, at, commentId?, seal} (seal: facts.sealed). Exit 2 with {"error": ...} when the arguments are wrong or
GitHub's answer cannot be read (with --request, a read that fails before the POST is a failed line instead). A rerun
after a lost line reports `already`.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, attempt, bind_repo, report  # noqa: E402
from harvest import CR_TRIGGER_BOX as BOX, coderabbit, gh_json, observe, pages, stamp  # noqa: E402

SKIP = 'review skipped: manual review required'
REQUEST = '@coderabbitai review'
# Asked a minute past the reset, not at it, as pr-babysit naps to it.
MARGIN = timedelta(minutes=1)
ISO = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')


class Failed(Exception):
    def __init__(self, why, phase='post'):
        super().__init__(why)
        self.phase = phase


def utc(text):
    """Whether TEXT is a real UTC time written as GitHub writes one."""
    if not ISO.fullmatch(text):
        return False
    try:
        stamp(text)
    except ValueError:
        return False
    return True


def on_comment(outcome, c, **members):
    """OUTCOME naming comment C: detail "comment N" unless MEMBERS give one, and commentId."""
    return {'outcome': outcome, 'detail': f"comment {c['id']}", 'commentId': c['id'], **members}


def by_coderabbit(c):
    return 'coderabbit' in ((c.get('user') or {}).get('login') or '').lower()


def state_of(cr):
    return f"CodeRabbit is {cr['state']}{'/' + cr['kind'] if cr['kind'] else ''}"


def skipped(pr, head):
    """(None, the PR's comments) when HEAD is PR N's head and CodeRabbit's newest status or check run on it is the
    manual-review skip, (why not, None) otherwise."""
    if gh_json('api', f'repos/{{owner}}/{{repo}}/pulls/{pr}')['head']['sha'] != head:
        return f'{head[:9]} is not the head of PR {pr}', None
    statuses = pages(f'repos/{{owner}}/{{repo}}/commits/{head}/statuses')
    checks = pages(f'repos/{{owner}}/{{repo}}/commits/{head}/check-runs', 'check_runs')
    comments = pages(f'repos/{{owner}}/{{repo}}/issues/{pr}/comments')
    # No head event: a SHA-less notice newer than the status or run cannot be placed, and reads unknown.
    cr = coderabbit(head, statuses, checks, comments, None)
    if (cr['state'], cr['kind']) != ('settled', 'skipped') or not cr['reason'].lower().startswith(SKIP):
        return f"{state_of(cr)}: {cr['reason']}", None
    return None, comments


def trigger(pr, head):
    why, comments = skipped(pr, head)
    if why:
        return {'outcome': 'notSkipped', 'detail': why}
    holders = [c for c in comments if by_coderabbit(c) and len(BOX.findall(c.get('body') or '')) == 1]
    if len(holders) != 1:
        raise Failed(f'{len(holders)} CodeRabbit comments hold one Trigger review box, not 1')
    c = holders[0]
    if BOX.search(c['body'])['mark'] != ' ':
        return on_comment('already', c)
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
    return on_comment('ticked', c)


def pull_at(pr, head):
    """(PR N's pull object, None) when it is open at HEAD, else (it, why not)."""
    pull = gh_json('api', f'repos/{{owner}}/{{repo}}/pulls/{pr}')
    if pull['state'] != 'open' or pull['head']['sha'] != head:
        return pull, f"PR {pr} is {pull['state']} at {pull['head']['sha'][:9]}, not open at {head[:9]}"
    return pull, None


def unopened(pr, head):
    """None when PR N is open at HEAD, else why not."""
    return pull_at(pr, head)[1]


def unasked(repo, pr, head, not_before=None):
    """(None, the retryAt member) when the request is to be posted now, else (the outcome, None); it only reads."""
    why = unopened(pr, head)
    if why:
        return {'outcome': 'notLimited', 'detail': why}, None
    seen = observe(repo, pr, ['coderabbit'], ['coderabbit'])
    cr = seen['bots'][0]
    if seen['headSha'] == head and (cr['state'], cr['kind']) == ('settled', 'skipped') and cr['reason'].lower().startswith(SKIP):
        return unasked_skip(pr, head, seen['headEventAt'])
    if seen['headSha'] != head or (cr['state'], cr['kind']) != ('settled', 'limited'):
        return {'outcome': 'notLimited', 'detail': f"{state_of(cr)} on {seen['headSha'][:9]}: {cr['reason']}"}, None
    reset = {'retryAt': cr['retryAt']}
    starts = [stamp(x) for x in (cr['retryAt'], not_before) if x]
    due = [stamp(cr['retryAt']) + MARGIN] if cr['retryAt'] else []
    if not starts or max(due + starts) > stamp(seen['observedAt']):
        return {'outcome': 'notDue', 'detail': cr['reason'], **reset}, None
    asked = [c for c in pages(f'repos/{{owner}}/{{repo}}/issues/{pr}/comments')
             if (c.get('body') or '').strip() == REQUEST and stamp(c['created_at']) >= max(starts)]
    if asked:
        return on_comment('already', asked[0], at=asked[0]['created_at'], **reset), None
    # The reads above take a while: a PR closed or pushed meanwhile is not asked about.
    why = unopened(pr, head)
    if why:
        return {'outcome': 'notLimited', 'detail': why}, None
    return None, reset


def unasked_skip(pr, head, event_at):
    """unasked's answer on a manual-review skip: an ask stands when the box is ticked or a request follows the head event."""
    comments = pages(f'repos/{{owner}}/{{repo}}/issues/{pr}/comments')
    reset = {'retryAt': None}
    ticked = [c for c in comments if by_coderabbit(c) and any(m['mark'] != ' ' for m in BOX.finditer(c.get('body') or ''))]
    if ticked:
        c = ticked[0]
        return on_comment('already', c, detail=f"comment {c['id']} has its Trigger review box ticked", at=c['updated_at'], **reset), None
    standing = [c for c in comments if (c.get('body') or '').strip() == REQUEST]
    if standing and event_at is None:
        raise Failed(f"the head event time is unknown, so comment {standing[-1]['id']} cannot be placed before or after it: nothing posted", 'read')
    # One at the head event's own second is taken as asked: a duplicate comment is the worse error here.
    asked = [c for c in standing if stamp(c['created_at']) >= stamp(event_at)]
    if asked:
        return on_comment('already', asked[0], at=asked[0]['created_at'], **reset), None
    why = unopened(pr, head)
    if why:
        return {'outcome': 'notLimited', 'detail': why}, None
    return None, reset


def request(repo, pr, head, not_before=None):
    try:
        out, reset = unasked(repo, pr, head, not_before)
    except Unusable as e:
        raise Failed(str(e), 'read')
    if out:
        return out
    path = f'repos/{{owner}}/{{repo}}/issues/{pr}/comments'
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
    return on_comment('requested', posted, at=back['created_at'], **reset)


def collect(argv):
    p = Parser(prog='coderabbit_trigger.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--request', action='store_true')
    p.add_argument('--not-before')
    p.add_argument('--repo')
    a = p.parse_args(argv)
    if not FULL_SHA.match(a.head):
        raise Unusable(f'not a full SHA: {a.head!r}')
    if a.not_before is not None and (not a.request or not utc(a.not_before)):
        raise Unusable(f'--not-before takes a UTC time like 2026-10-13T11:00:00Z, with --request: {a.not_before!r}')
    repo = bind_repo(a.repo)
    try:
        out = request(repo, a.pr, a.head, a.not_before) if a.request else trigger(a.pr, a.head)
    except Failed as e:
        out = {'outcome': 'failed', 'detail': str(e), **({'phase': e.phase} if a.request else {})}
    except (KeyError, TypeError, AttributeError) as e:
        raise Unusable(f'gh answered in a shape coderabbit_trigger.py does not read ({e!r})')
    if a.request:
        out = {'retryAt': None, **out}
    return {'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), **out}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
