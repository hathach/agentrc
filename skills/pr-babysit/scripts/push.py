#!/usr/bin/env python3
"""Push one commit to a branch and read back where it landed.

  push.py --remote R --branch B --sha S --push-url U [--push-url U ...] [--open-pr N] [--pr N] [--repo OWNER/NAME]

Refuses unless R's push URLs (`git remote get-url --push --all R`) are exactly
the U given, in order: those are the destinations the caller vetted. With
--open-pr, it reads PR N's state first and, unless it reads OPEN, pushes
nothing: pushed false, heads empty and prState MERGED, CLOSED or UNREAD when
unreadable. Then runs
`git push R S:refs/heads/B`, no flags, and reads refs/heads/B back from every
U with `git ls-remote`, never through R, whose fetch URL may differ. With
--pr, it also reads the PR's head from GitHub, retrying while it is not S,
since GitHub takes a moment to see a push. Both read PR N of --repo, else of
the checkout's one repository (facts.bind_repo).

stdout ends with one JSON line {pushed, detail, heads: [{url, head}],
prState? (--open-pr), prHead? (--pr), seal} (seal: facts.sealed): pushed is git push's exit status, detail git's refusal line or else
its last stderr line, head the SHA the branch holds at that URL, "" when the
branch is absent there, null when it could not be read; prHead is null when
unreadable. A failed read is never a sign that nothing was pushed. Exit 0 with
that line; exit 2 with {"error": ...} when the arguments are wrong or R's push
URLs changed, and then nothing was pushed.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, attempt, bind_repo, report  # noqa: E402

PR_TRIES = 5
PR_WAIT = 3  # seconds between reads of the PR head


def branch_head(url, branch):
    code, out, _ = attempt('git', 'ls-remote', url, f'refs/heads/{branch}')
    if code:
        return None
    heads = [line.split('\t')[0] for line in out.splitlines() if line.endswith(f'\trefs/heads/{branch}')]
    return heads[0] if heads else ''


def pr_head(pr, sha):
    head = None
    for tried in range(PR_TRIES):
        if tried:
            time.sleep(PR_WAIT)
        code, out, _ = attempt('gh', 'pr', 'view', str(pr), '--json', 'headRefOid', '-q', '.headRefOid')
        head = out.strip() if not code else None
        if head == sha:
            break
    return head


def pr_state(pr):
    code, out, _ = attempt('gh', 'pr', 'view', str(pr), '--json', 'state', '-q', '.state')
    return 'UNREAD' if code else out.strip() or 'UNREAD'


def publish(argv):
    p = Parser(prog='push.py', add_help=False)
    p.add_argument('--remote', required=True)
    p.add_argument('--branch', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--push-url', action='append', required=True, dest='urls')
    p.add_argument('--open-pr', type=int)
    p.add_argument('--pr', type=int)
    p.add_argument('--repo')
    a = p.parse_args(argv)
    if not FULL_SHA.match(a.sha):
        raise Unusable(f'not a full SHA: {a.sha!r}')
    if a.open_pr is not None or a.pr is not None:
        bind_repo(a.repo)
    code, out, err = attempt('git', 'remote', 'get-url', '--push', '--all', a.remote)
    if code:
        raise Unusable(f'git remote get-url --push --all {a.remote}: {err.strip()}')
    if out.splitlines() != a.urls:
        raise Unusable(f"{a.remote} now pushes to {', '.join(out.splitlines()) or '(nowhere)'}, not {', '.join(a.urls)}")
    state = pr_state(a.open_pr) if a.open_pr is not None else None
    if a.open_pr is not None and state != 'OPEN':
        why = 'state is unreadable' if state == 'UNREAD' else f'is {state.lower()}'
        return {'pushed': False, 'detail': f'PR #{a.open_pr} {why}: nothing pushed', 'heads': [], 'prState': state}
    code, _, err = attempt('git', 'push', a.remote, f'{a.sha}:refs/heads/{a.branch}')
    said = [line for line in err.splitlines() if line.strip() and not line.startswith('hint:')]
    refused = [line for line in said if line.startswith(' ! ')]
    receipt = {'pushed': not code, 'detail': (refused or said or [''])[-1].strip(),
               'heads': [{'url': u, 'head': branch_head(u, a.branch)} for u in a.urls]}
    if a.open_pr is not None:
        receipt['prState'] = state
    if a.pr is not None:
        receipt['prHead'] = pr_head(a.pr, a.sha)
    return receipt


if __name__ == '__main__':
    sys.exit(report(publish, sys.argv[1:], seal=True))
