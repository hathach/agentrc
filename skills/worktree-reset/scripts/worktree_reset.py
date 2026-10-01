#!/usr/bin/env python3
"""Reset this git worktree slot for its next task once its branch's work is merged.

Run inside a linked worktree whose origin is a GitHub repository. Nothing changes before
every check below passes.

Refused (exit 3): the primary checkout, a detached HEAD, a merge/rebase/cherry-pick/
revert/bisect in progress, any tracked change or untracked file `git status` reports
(explicit options, so user config cannot hide one), a headless chief running here, an
ignored path here that the base tracks at, above or below (the switch would have to
replace it), an unreachable origin or GitHub, and a branch whose commits are not on the base: HEAD must
be an ancestor of the base, or the head of a merged PR whose merge commit is on the base
(squash and rebase merges). There is no force flag.

Base: the default branch origin advertises, fetched explicitly and pinned by sha; the
local origin/HEAD is never trusted. New branch: `<worktree dir name>-<N>`, N one above
the highest suffix among local branches, origin's branches (live and tracking) and the
head branches of the repository's PRs (GitHub search, deleted branches included; an
incomplete answer refuses); the bare name counts as 1. A numbered branch deleted before
it was ever pushed or PR'd can be reused.

Follow-ups (exit 4 unless --proceed): each --pending item the caller names and each
unresolved review thread of the branch's latest merged PR are listed and nothing
changes; --proceed is the human's go-ahead after seeing that list.

Then, in order: `cowork.py reset codex all` and `reset claude all` (while the old HEAD is
still checked out: cowork drops a worktree lane only when its branch is on HEAD), switch
to the new branch at the base, delete the old branch. Ignored files (dependency links,
build dirs, run reports) and chief's state dir stay: each run reads only what it wrote
itself. The worktree must be quiet while this runs.

--dry-run fetches git metadata and queries GitHub, then prints the plan; it switches or
deletes no branch and resets no agent state.

Exit 0 done or planned, 2 usage, 3 refused before any change, 4 follow-ups pending,
5 failed after a change (the output names what already changed).
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
IN_PROGRESS = ('MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
               'BISECT_LOG')
SEARCH = '''query($q: String!, $endCursor: String) {
  search(query: $q, type: ISSUE, first: 100, after: $endCursor) {
    issueCount
    nodes { ... on PullRequest { number headRefName headRefOid mergedAt mergeCommit { oid } } }
    pageInfo { hasNextPage endCursor } } }'''
THREADS = '''query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $endCursor) {
      nodes { isResolved comments(first: 1) { nodes { url } } }
      pageInfo { hasNextPage endCursor } } } } }'''


class Refused(Exception):
    pass


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise Refused(f'{" ".join(cmd)}: {(r.stdout + r.stderr).strip()}')
    return r.stdout


def git(root, *args):
    return run(['git', *args], cwd=root)


def ok(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True).returncode == 0


def gh_lines(*args):
    """gh's output, one item per line; any error refuses, never reads as empty."""
    return [line for line in run(['gh', *args]).splitlines() if line]


def github_repo(root):
    url = git(root, 'remote', 'get-url', 'origin').strip()
    return run(['gh', 'repo', 'view', url, '--json', 'nameWithOwner', '-q', '.nameWithOwner']).strip()


def pulls(repo, prefix):
    """The repository's PRs whose head branch starts with prefix: `head:` search matches by
    prefix and keeps deleted branches. Search stops at 1000 results, so a short answer refuses."""
    pages = [json.loads(line) for line in gh_lines(
        'api', 'graphql', '--paginate', '-f', f'query={SEARCH}', '-f', f'q=repo:{repo} is:pr head:{prefix}',
        '--jq', '.data.search')]
    nodes = [n for page in pages for n in page['nodes']]
    if pages and len(nodes) != pages[0]['issueCount']:
        raise Refused(f'PR search for head:{prefix} returned {len(nodes)} of {pages[0]["issueCount"]}')
    return [dict(number=n['number'], ref=n['headRefName'], sha=n['headRefOid'], merged_at=n['mergedAt'],
                 merge_commit_sha=(n['mergeCommit'] or {}).get('oid')) for n in nodes]


def unresolved_threads(repo, number):
    owner, name = repo.split('/')
    return gh_lines('api', 'graphql', '--paginate', '-f', f'query={THREADS}', '-F', f'owner={owner}',
                    '-F', f'name={name}', '-F', f'number={number}', '--jq',
                    '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)'
                    ' | .comments.nodes[0].url')


def running_chiefs(root):
    sys.path.insert(0, str(SKILLS / 'headless-chief' / 'scripts'))
    try:
        import chief_run
    except ImportError as e:
        raise Refused(f'cannot check for a running chief: {e}')
    return chief_run.running_chiefs(root)


def cowork_reset(root, side):
    return run([sys.executable, str(SKILLS / 'cowork' / 'scripts' / 'cowork.py'), 'reset', side, 'all'], cwd=root)


def check_base(root, base):
    """Refuse an ignored path here that base tracks at, below or above (file <-> directory):
    the switch would have to replace it."""
    ignored = [i.rstrip('/') for i in git(root, 'ls-files', '-z', '-o', '-i', '--exclude-standard',
                                          '--directory').split('\0') if i]
    clash = [t for t in git(root, 'ls-tree', '-r', '-z', '--name-only', base).split('\0') if t and any(
        t == i or t.startswith(i + '/') or i.startswith(t + '/') for i in ignored)]
    if clash:
        raise Refused(f'the base tracks paths this worktree holds as ignored: {", ".join(clash[:5])}')


def plan(root, pending):
    """Every check and every fact the reset needs, changing nothing but fetched refs."""
    git_dir = Path(git(root, 'rev-parse', '--absolute-git-dir').strip())
    if git_dir == Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()):
        raise Refused('this is the primary checkout, not a worktree slot')
    if not ok(root, 'symbolic-ref', '--quiet', 'HEAD'):
        raise Refused('HEAD is detached')
    branch = git(root, 'symbolic-ref', '--short', 'HEAD').strip()
    busy = [n for n in IN_PROGRESS if (git_dir / n).exists()]
    if busy:
        raise Refused(f'git operation in progress: {", ".join(busy)}')
    dirty = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none')
    if dirty:
        raise Refused('the tree is not clean:\n  ' + '\n  '.join(e for e in dirty.split('\0') if e))
    chiefs = running_chiefs(root)
    if chiefs:
        raise Refused(f'a chief is running in this worktree: pid {", ".join(map(str, chiefs))}')

    repo = github_repo(root)
    remote = git(root, 'ls-remote', '--symref', 'origin', 'HEAD', 'refs/heads/*')
    m = re.search(r'^ref: refs/heads/(\S+)\tHEAD$', remote, re.M)
    if not m:
        raise Refused('origin advertises no default branch')
    base_branch = m.group(1)
    git(root, 'fetch', '--quiet', 'origin', f'+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}')
    base = git(root, 'rev-parse', f'refs/remotes/origin/{base_branch}').strip()
    check_base(root, base)

    name = root.name
    prs = pulls(repo, name) + ([] if branch.startswith(name) else pulls(repo, branch))
    head = git(root, 'rev-parse', 'HEAD').strip()
    if ok(root, 'merge-base', '--is-ancestor', head, base):
        proof = f'HEAD is on origin/{base_branch}'
    else:
        proof = next((f'merged as PR #{pr["number"]}' for pr in prs if pr['ref'] == branch and pr['sha'] == head
                      and pr['merged_at'] and pr['merge_commit_sha']
                      and ok(root, 'merge-base', '--is-ancestor', pr['merge_commit_sha'], base)), None)
    if not proof:
        raise Refused(f'{branch} has work not on origin/{base_branch}:\n' + git(root, 'log', '--oneline', f'{base}..{head}'))

    family = re.compile(rf'^{re.escape(name)}(?:-(\d+))?$')
    taken = git(root, 'for-each-ref', '--format=%(refname:lstrip=2)', 'refs/heads').split()
    taken += git(root, 'for-each-ref', '--format=%(refname:lstrip=3)', 'refs/remotes/origin').split()
    taken += [line.split('refs/heads/', 1)[1] for line in remote.splitlines() if '\trefs/heads/' in line]
    taken += [pr['ref'] for pr in prs]
    new = f'{name}-{max([int(m.group(1) or 1) for m in map(family.match, taken) if m], default=1) + 1}'
    git(root, 'check-ref-format', '--branch', new)

    last = max((pr for pr in prs if pr['ref'] == branch and pr['merged_at']), key=lambda pr: pr['merged_at'],
               default=None)
    followups = list(pending) + [f'unresolved review thread {url}'
                                 for url in (unresolved_threads(repo, last['number']) if last else [])]
    return dict(branch=branch, base=base, base_branch=base_branch, proof=proof, new=new, followups=followups)


def summary(p):
    return (f'{p["branch"]} -> {p["new"]} at {p["base"][:12]} (origin/{p["base_branch"]}); '
            f'old branch {p["proof"]}')


def apply(root, p):
    done = []
    try:
        for side in ('codex', 'claude'):
            done.append(f'cowork {side} reset run (its output above)')
            print(cowork_reset(root, side), end='')
        git(root, 'switch', '--quiet', '--no-overwrite-ignore', '--no-recurse-submodules', '--no-track',
            '-c', p['new'], p['base'])
        done.append(f'switched to {p["new"]}')
        git(root, 'branch', '-q', '-D', p['branch'])
        done.append(f'deleted {p["branch"]}')
    except (Refused, OSError) as e:
        print(f'worktree_reset: failed after a change: {e}\nalready done: {"; ".join(done)}', file=sys.stderr)
        return 5
    print(summary(p) + '\ndone: ' + '; '.join(done))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='check and print the plan; change nothing')
    ap.add_argument('--pending', action='append', default=[], metavar='TEXT',
                    help='a follow-up from this task neither filed nor done (repeatable)')
    ap.add_argument('--proceed', action='store_true', help='the human agreed to reset despite the listed follow-ups')
    args = ap.parse_args(argv)
    try:
        root = Path(run(['git', 'rev-parse', '--show-toplevel']).strip())
        p = plan(root, args.pending)
    except Refused as e:
        print(f'worktree_reset: refused, nothing changed: {e}', file=sys.stderr)
        return 3
    if p['followups'] and not args.proceed:
        print('follow-ups not filed or done; nothing changed. Rerun with --proceed once the human agrees:\n'
              + '\n'.join(f'  - {f}' for f in p['followups']))
        return 4
    if args.dry_run:
        print(f'would: reset cowork lanes; {summary(p)}')
        return 0
    return apply(root, p)


if __name__ == '__main__':
    sys.exit(main())
