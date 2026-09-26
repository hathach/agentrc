#!/usr/bin/env python3
"""Pin a PR for pr-review and check a pinned one has not moved.

  prepare.py --pr N [--repo OWNER/NAME] [--max-groups 6] [--full]   # from the repository's primary checkout top level
  prepare.py --check --pr N --expected-head SHA [--repo OWNER/NAME]  # from the review worktree top level

prepare fetches the PR head into refs/pr-review/<N>/<sha> (old heads stay, so
a later push can be diffed against them) and the base branch, pins head, base
and merge base, and puts branch pr-review-<N> at the head in
.worktrees/pr-review-<N>. An existing worktree is reused only when it is clean
and its branch tip is a head this script pinned; anything else is refused, never
reset. It first settles the pending reviews of ours on the ledger (post.py
--sync): what the human submitted or deleted on GitHub is recorded, and one still
pending, or a post never confirmed, refuses the run. It reads the ledger to choose the mode: `same` when the head was
reviewed already, `incremental` when the last reviewed head is an ancestor and
the merge base is unchanged (scope: last head..head), else `full` (scope:
merge base..head); --full forces full. The scope's changed directories become
at most --max-groups review groups, merging to shallower directories when they
exceed it. `tooling` lists the PR's changed paths that execute when the PR is
built or tested (build files, scripts, CI, the HIL harness): the caller reads
their diff before building anything. `ci` summarises the head's checks:
`unobserved` with none, `red`, `pending` (action_required included), `green`.

--check refuses unless the remote head, the worktree HEAD and the expected
head agree, the checkout is clean and the ledger is readable; it prints the
head's CI summary again and the pins prepare recorded for that head (mode,
merge base, scope base, groups), for the caller to compare with its own.

stdout ends with one JSON line; the full changed-path list and every check go
to the facts file the line names, beside the ledger. {"error": ...}, exit 2,
when a fact cannot be read or the pins disagree.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import ONLINE, last, ledger_dir, ledger_path, load, locked, refuse_unsettled, repo_of, store  # noqa: E402
from post import sync  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import FULL_SHA, Parser, Unusable, checkout_top, git, report, run  # noqa: E402

# Paths that run when a PR is built or tested; their diff is read before any build.
TOOLING = re.compile(r'(^|/)(CMakeLists\.txt|Makefile|[^/]+\.(cmake|mk|py|sh|ps1|bat))$'
                     r'|^\.github/|^tools/|^test/hil/|^\.pre-commit-config\.yaml$|(^|/)(package\.json|setup\.cfg|pyproject\.toml)$')
PASSED = {'SUCCESS', 'SKIPPED', 'NEUTRAL'}
PENDING = {'PENDING', 'QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED', 'EXPECTED', 'ACTION_REQUIRED'}
FAILED = {'FAILURE', 'ERROR', 'TIMED_OUT', 'STARTUP_FAILURE', 'CANCELLED', 'STALE'}


def gh_json(*argv, ok=(0,)):
    code, out = run('gh', *argv, ok=ok)
    try:
        return code, json.loads(out) if out.strip() else None
    except ValueError:
        raise Unusable(f"gh {' '.join(argv)}: not JSON")


def pr_view(repo, pr):
    fields = 'number,url,title,state,author,headRefOid,headRefName,headRepository,headRepositoryOwner,isCrossRepository,baseRefName'
    return gh_json('pr', 'view', str(pr), '--repo', repo, '--json', fields)[1]


def remote_for(repo):
    """The remote whose URL is this repository: fetching another one would pin someone else's refs."""
    names = []
    for row in git('remote', '-v').splitlines():
        name, url = row.split()[:2]
        names.append(name)
        if re.search(rf'[:/]{re.escape(repo)}(\.git)?/?$', url):
            return name
    raise Unusable(f'no remote of this checkout is {repo} (remotes: {", ".join(sorted(set(names))) or "none"})')


def facts_path(repo, pr, head):
    return ledger_dir(repo) / str(pr) / f'prepare-{head}.json'


def ci_summary(repo, pr):
    # gh exits 8 while checks are pending and 1 when the PR has none.
    code, checks = gh_json('pr', 'checks', str(pr), '--repo', repo, '--json', 'name,state,workflow,link', ok=(0, 1, 8))
    checks = checks or []
    states = [c.get('state', '').upper() for c in checks]
    counts = {}
    for s in states:
        counts[s] = counts.get(s, 0) + 1
    state = ('unobserved' if not checks else 'red' if any(s in FAILED for s in states)
             else 'pending' if any(s in PENDING for s in states) else 'green' if all(s in PASSED for s in states) else 'unknown')
    return {'state': state, 'counts': counts, 'actionRequired': counts.get('ACTION_REQUIRED', 0)}, checks


def groups_of(paths, cap):
    """Changed directories (a top-level file stands for itself), made shallower until at most cap
    remain; past the top level everything is one group, the whole tree, which the diff still scopes."""
    parts = [p.split('/') for p in paths]
    depth = max([len(x) - 1 for x in parts] + [1])
    while depth >= 1:
        got = sorted({'/'.join(x[:min(depth, len(x) - 1)]) if len(x) > 1 else x[0] for x in parts})
        if len(got) <= cap:
            return got, False
        depth -= 1
    return ['.'], True


def changed(a, b):
    return [p for p in git('diff', '--name-only', '-z', '--no-renames', a, b).split('\0') if p]


def pin(remote, repo, pr, info):
    head = info['headRefOid']
    git('fetch', '--quiet', remote, f'+refs/pull/{pr}/head:refs/pr-review/{pr}/head',
        f"+refs/heads/{info['baseRefName']}:refs/pr-review/{pr}/base")
    got = git('rev-parse', f'refs/pr-review/{pr}/head').strip()
    if got != head:
        raise Unusable(f'the PR head moved while fetching: gh says {head}, fetched {got}; run again')
    git('update-ref', f'refs/pr-review/{pr}/{head}', head)
    base = git('rev-parse', f'refs/pr-review/{pr}/base').strip()
    return head, base, git('merge-base', base, head).strip()


def worktree(top, pr, head):
    path, branch = Path(top) / '.worktrees' / f'pr-review-{pr}', f'pr-review-{pr}'
    pinned = {line.split()[0] for line in git('for-each-ref', '--format=%(objectname)', f'refs/pr-review/{pr}/').splitlines()}
    if not path.exists():
        if git('branch', '--list', branch).strip():
            tip = git('rev-parse', branch).strip()
            if tip not in pinned:
                raise Unusable(f'branch {branch} has commits this script did not pin ({tip}); resolve by hand')
            git('branch', '-f', branch, head)
            git('worktree', 'add', '--quiet', str(path), branch)
        else:
            git('worktree', 'add', '--quiet', '-b', branch, str(path), head)
        return str(path), branch
    dirty = run('git', '-C', str(path), 'status', '--porcelain')[1].strip()
    if dirty:
        raise Unusable(f'{path} has uncommitted changes, left by an earlier run: {dirty.splitlines()[0]} ...; restore them first')
    tip = run('git', '-C', str(path), 'rev-parse', 'HEAD')[1].strip()
    if tip not in pinned:
        raise Unusable(f'{path} is at {tip}, not a head this script pinned; resolve by hand')
    run('git', '-C', str(path), 'checkout', '--quiet', '-B', branch, head)
    return str(path), branch


def mode_of(led, head, merge_base, full):
    rev = last(led)
    if full or not rev:
        return 'full', merge_base, rev and rev['head']
    if rev['head'] == head:
        return 'same', merge_base, head
    ancestor = run('git', 'merge-base', '--is-ancestor', rev['head'], head, ok=(0, 1))[0] == 0
    if ancestor and rev.get('mergeBase') == merge_base:
        return 'incremental', rev['head'], rev['head']
    return 'full', merge_base, rev['head']


def prepare(a):
    top = checkout_top()
    repo = repo_of(a.repo)
    info = pr_view(repo, a.pr)
    if info.get('state') != 'OPEN':
        raise Unusable(f"PR {a.pr} is {info.get('state')}; pr-review reviews open PRs")
    head, base, merge_base = pin(remote_for(repo), repo, a.pr, info)
    path = ledger_path(repo, a.pr)
    with locked(path):
        led = load(path, repo, a.pr)
        synced = sync(repo, a.pr, led, lambda: store(path, led))
    # A partial review is on the PR, so its threads can still be discussed; save refuses a second draft.
    refuse_unsettled(led, head, ('pending',))
    online = [r for r in led['reviews'] if r['status'] in ONLINE]
    if online and online[-1]['status'] == 'drafted':
        raise Unusable(f"your pending review of {online[-1]['head']} is still open on the PR: submit or delete it on GitHub first")
    if online:
        auto = ' --auto' if online[-1].get('publish') == 'auto' else ''
        raise Unusable(f"a review POST for {online[-1]['head']} may have landed unseen: reconcile it first with "
                       f"post.py --pr {a.pr} --expected-head {online[-1]['head']}{auto}, which only looks for it by its marker")
    mode, scope_base, prior = mode_of(led, head, merge_base, a.full)
    wt, branch = worktree(top, a.pr, head)
    pr_paths = changed(merge_base, head)
    scope = pr_paths if mode != 'incremental' else changed(scope_base, head)
    groups, over = groups_of(scope, a.max_groups)
    ci, checks = ci_summary(repo, a.pr)
    facts = facts_path(repo, a.pr, head)
    facts.parent.mkdir(parents=True, exist_ok=True)
    pins = {'head': head, 'mergeBase': merge_base, 'scopeBase': scope_base, 'mode': mode, 'groups': groups}
    facts.write_text(json.dumps({**pins, 'changed': pr_paths, 'scope': scope, 'checks': checks}, indent=1) + '\n')
    # The selector reads one path a line: a path that line splitting breaks cannot go in the file.
    listing = ''.join(f'{p}\n' for p in pr_paths)
    changed_file = facts.with_name(f'changed-{head}.txt') if listing.splitlines() == pr_paths else None
    if changed_file:
        changed_file.write_text(listing)
    head_repo = f"{(info.get('headRepositoryOwner') or {}).get('login')}/{(info.get('headRepository') or {}).get('name')}"
    return {
        'repo': repo, 'pr': a.pr, 'url': info['url'], 'title': info['title'],
        'author': (info.get('author') or {}).get('login'), 'fork': bool(info.get('isCrossRepository')),
        'headRepo': head_repo, 'headBranch': info['headRefName'], 'baseBranch': info['baseRefName'],
        'head': head, 'base': base, 'mergeBase': merge_base, 'worktree': wt, 'branch': branch,
        'mode': mode, 'scopeBase': scope_base, 'priorHead': prior,
        'changed': len(pr_paths), 'scoped': len(scope), 'groups': groups, 'overCap': over,
        'tooling': [p for p in pr_paths if TOOLING.search(p)], 'ci': ci,
        'ledger': str(path), 'facts': str(facts), 'synced': synced, 'changedFile': changed_file and str(changed_file),
    }


def check(a):
    top = checkout_top()
    if not FULL_SHA.match(a.expected_head or ''):
        raise Unusable('--check needs --expected-head, a full SHA')
    repo = repo_of(a.repo)
    remote = pr_view(repo, a.pr)['headRefOid']
    local = git('rev-parse', 'HEAD').strip()
    if not remote == local == a.expected_head:
        raise Unusable(f'head moved: expected {a.expected_head}, PR head {remote}, checkout {local}')
    dirty = git('status', '--porcelain').strip()
    if dirty:
        raise Unusable(f'the checkout has uncommitted changes, so it is not the head: {dirty.splitlines()[0]} ...')
    load(ledger_path(repo, a.pr), repo, a.pr)
    facts = facts_path(repo, a.pr, local)
    try:
        pinned = json.loads(facts.read_text())
    except (OSError, ValueError):
        raise Unusable(f'no prepare facts for {local} at {facts}: run prepare first')
    return {'ok': True, 'head': local, 'top': os.path.realpath(top), 'ci': ci_summary(repo, a.pr)[0],
            'pins': {k: pinned[k] for k in ('mergeBase', 'scopeBase', 'mode', 'groups')}}


def collect(argv):
    p = Parser(prog='prepare.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--repo')
    p.add_argument('--check', action='store_true')
    p.add_argument('--expected-head')
    p.add_argument('--max-groups', type=int, default=6)
    p.add_argument('--full', action='store_true')
    a = p.parse_args(argv)
    if a.max_groups < 1:
        raise Unusable('--max-groups must be at least 1')
    return check(a) if a.check else prepare(a)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
