#!/usr/bin/env python3
"""Pin a PR for pr-review and check a pinned one has not moved.

  prepare.py --pr N [--repo OWNER/NAME] [--full] [--auto-fix]       # from the checkout's top level
  prepare.py --pr N --recheck-from SHA [--repo OWNER/NAME]          # an auto-fix recheck, from the same place
  prepare.py --check --pr N --expected-head SHA [--repo OWNER/NAME]  # from the review worktree top level
  prepare.py --release --pr N --token T [--repo OWNER/NAME]           # from any worktree of the repository

prepare fetches the PR head into refs/pr-review/<N>/<sha> (old heads stay, so
a later push can be diffed against them) and the base branch, pins head, base
and merge base, and puts branch pr-review-<N> at the head in
.worktrees/pr-review-<N>, or in <checkout>--pr-review-<N> beside it when the
checkout is a linked worktree (worktree-remove refuses to remove one with a
worktree nested in it); git failing to tell refuses. An existing worktree at either
place is reused only when it is clean and its branch tip is a head this script
pinned; anything else, and a tree at both places, is refused, never reset.
The sibling tree is locked for the run (`git worktree lock --reason "pr-review
<N> running <token>"`; worktree-remove's cleanup of the checkout keeps a locked sibling),
reported as `lock`: `taken` (a new token, or --token's when a prepare for the
same run is repeated), `held` when it already carries --token's lock, `foreign`
when someone else's lock is there, left as it is, `none` for a nested tree. A
sibling another run of this PR has locked is refused before anything is checked
out, naming that run's token. It
first settles the pending reviews of ours on the ledger (post.py
--sync): what the human submitted or deleted on GitHub is recorded, and one still
pending, or a post never confirmed, refuses the run. Then it brings the last review's standing
findings to the PR as it reads now (post.py's reconcile, reported as `reconciled`): an inline comment of
ours edited since stands with its text published, which the recheck judges, and a deleted one drops its
finding; a listing it cannot read refuses. Review bodies are not reconciled: a finding moved into one keeps its
recorded text, and post.py --auto holds it once that body reads otherwise. It reads the ledger to choose the mode:
`same` when the head was reviewed already, `incremental` when the last reviewed head is an ancestor and
the merge base is unchanged (scope: last head..head), else `full` (scope:
merge base..head); --full forces full. The scope's changed directories become
at most MAX_GROUPS review groups, merging to shallower directories when they
exceed it. `tooling` lists the PR's changed paths that execute when the PR is
built or tested (build files, scripts, CI, the HIL harness): the caller reads
their diff before building anything. `ci` summarises the head's checks:
`unobserved` with none, `red`, `pending` (action_required included), `green`,
`unknown` for a state none of these covers.

--auto-fix adds `repair`: whether an auto-fix run's repairs can be pushed to the PR's head branch (headRepo,
headBranch), {state, why}: `available` with push access to the head repository, or for a cross-repository PR allowing
edits from maintainers, push access to the base; `unavailable` when this account has neither; `unknown` when gh cannot
tell. Only when available, it binds the review branch to the head branch through a remote of the head repository
(head_remote, bind_upstream) and adds {remote, addedRemote}; a binding it cannot make refuses. Without --auto-fix
nothing of this runs. On a `same` head `repair` also has `submittedReview`, the id of our review submitted on that
head (by the human included), which the repair takes its findings from, else null with `submittedWhy`; one unsent
answer bundle an earlier auto-fix deferred on that review no longer refuses the run: `deferredAnswers` counts its
answers, which wait for the repair to end.

--recheck-from R is the auto-fix recheck after a repair from our review submitted on R, which must be the last review
on the ledger. Its scope is what the repair changed: `same` when the head is still R, `incremental` from R when the
merge base is unchanged, and when a base merge moved it, `incremental` from a synthetic commit S, git's default merge
of R and the new merge base (parents R and it, fixed identity and dates, so a repeated prepare gives the same SHA),
kept by refs/pr-review-scope/<N>/<head>: `git diff S head` is the repair, later fixes and a content conflict's
resolution (`conflicts` names those paths), without what the merge brought in. Exact only for a default-strategy base
merge: what another strategy dropped reads as a change. Any other conflict kind is refused, never widened; a head R is
not an ancestor of takes `full`. `recheckFrom` is R in the JSON and the pins, null on any other prepare.

--release unlocks the pr-review-<N> sibling whose lock carries the token, once
the run that took it has ended; nothing locked by it is a no-op, and while
another run of the PR holds its tree the call is refused.

--check refuses unless the remote head, the worktree HEAD and the expected
head agree, the checkout is clean and the ledger is readable; it prints the
head's CI summary again, the checkout's real top, the repository and PR it
checked, and the pins prepare recorded for that head (mode, merge base, scope
base, groups, recheckFrom), for the caller to compare with its own.

stdout ends with one JSON line; the full changed-path list, every check and `repair` go
to the facts file the line names, beside the ledger, where ledger.py next reads them. {"error": ...}, exit 2,
when a fact cannot be read or the pins disagree.
"""

import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import (ONLINE, facts_path, last, ledger_path, load, locked, publications, refuse_unsettled, repo_of, store,  # noqa: E402
                    submitted_last)
from post import reconcile, sync  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import FULL_SHA, HOST, Parser, Unusable, attempt, checkout_repo, checkout_top, git, github_repo, report, run  # noqa: E402

# Paths that run when a PR is built or tested; their diff is read before any build.
TOOLING = re.compile(r'(^|/)(CMakeLists\.txt|Makefile|[^/]+\.(cmake|mk|py|sh|ps1|bat))$'
                     r'|^\.github/|^tools/|^test/hil/|^\.pre-commit-config\.yaml$|(^|/)(package\.json|setup\.cfg|pyproject\.toml)$')
PASSED = {'SUCCESS', 'SKIPPED', 'NEUTRAL'}
PENDING = {'PENDING', 'QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED', 'EXPECTED', 'ACTION_REQUIRED'}
FAILED = {'FAILURE', 'ERROR', 'TIMED_OUT', 'STARTUP_FAILURE', 'CANCELLED', 'STALE'}
RUN_LOCK = re.compile(r'pr-review (\d+) running ([0-9a-f]+)')
WRITE = ('admin', 'maintain', 'push')
MAX_GROUPS = 6


def gh_json(*argv, ok=(0,)):
    code, out = run('gh', *argv, ok=ok)
    try:
        return code, json.loads(out) if out.strip() else None
    except ValueError:
        raise Unusable(f"gh {' '.join(argv)}: not JSON")


def pr_view(repo, pr):
    fields = ('number,url,title,state,author,headRefOid,headRefName,headRepository,headRepositoryOwner,isCrossRepository,'
              'maintainerCanModify,baseRefName')
    return gh_json('pr', 'view', str(pr), '--repo', repo, '--json', fields)[1]


def config(key):
    return run('git', 'config', '--get', key, ok=(0, 1))[1].strip()


def can_write(repo):
    """(True or False, '') from this account's permissions on repo, (None, why) when gh cannot tell."""
    try:
        perms = gh_json('api', f'repos/{repo}', '--jq', '.permissions')[1]
    except Unusable as e:
        return None, str(e)
    if isinstance(perms, dict) and any(perms.get(k) is True for k in WRITE):
        return True, ''
    # push is false for every role without write access; anything less is no answer.
    if isinstance(perms, dict) and perms.get('push') is False:
        return False, ''
    return None, f'gh api repos/{repo} shows no push permission for this account: {json.dumps(perms)}'


def push_access(repo, head_repo, info):
    """(available|unavailable|unknown, why) for pushing to the PR's head branch: push access to its repository, or a
    cross-repository PR allowing edits from maintainers and push access to the base."""
    direct, why = can_write(head_repo)
    if direct:
        return 'available', f'push access to {head_repo}'
    if not info.get('isCrossRepository') or not info.get('maintainerCanModify'):
        if direct is None:
            return 'unknown', why
        allow = '' if not info.get('isCrossRepository') else ', and the PR does not allow edits from maintainers'
        return 'unavailable', f'no push access to {head_repo}{allow}'
    base, why_base = can_write(repo)
    if base:
        return 'available', f'the PR allows edits from maintainers, and push access to {repo}'
    if direct is None or base is None:
        return 'unknown', '; '.join(w for w in (why, why_base) if w)
    return 'unavailable', f'no push access to {head_repo} or {repo}'


def url_like(spelled, repo):
    """repo's github.com URL spelled as `spelled` is: https, ssh:// or scp-like."""
    if spelled.startswith('https://'):
        return f'https://{HOST}/{repo}.git'
    if spelled.startswith('ssh://'):
        return f'ssh://git@{HOST}/{repo}.git'
    if github_repo(spelled):
        return f'git@{HOST}:{repo}.git'
    return None


def head_remote(repo, head_repo, branch):
    """(name, added) of the remote auto-fix pushes through: the one whose every fetch and push URL is head_repo, else
    one added as pr-head-<owner>, spelled like the base remote; gh's default is pinned to the base first, as `gh repo
    set-default` would, so scripts reading the checkout's one repository still read the base."""
    names = git('remote').split()
    urls = lambda n: git('remote', 'get-url', '--all', n).splitlines() + git('remote', 'get-url', '--push', '--all', n).splitlines()  # noqa: E731
    found = [n for n in names if all(github_repo(u).lower() == head_repo.lower() for u in urls(n))]
    if len(found) > 1:
        tracked = config(f'branch.{branch}.remote')
        if tracked not in found:
            raise Unusable(f"remotes {', '.join(found)} all name {head_repo}: remove the ones you do not need")
        return tracked, False
    if found:
        return found[0], False
    name = f"pr-head-{head_repo.split('/')[0]}"
    if name in names:
        raise Unusable(f'remote {name} exists and does not name only {head_repo}: fix or remove it by hand')
    base = remote_for(repo)
    spelled = config(f'remote.{base}.url')
    url = url_like(spelled, head_repo)
    if not url:
        raise Unusable(f'no remote names {head_repo}, and the {base} remote URL {spelled!r} is not a github.com one to '
                       'spell it like: add a remote for it by hand')
    if not run('git', 'config', '--get-regexp', r'^remote\..+\.gh-resolved$', ok=(0, 1))[1].strip():
        git('config', f'remote.{base}.gh-resolved', 'base')
    git('remote', 'add', name, url)
    try:
        reads = checkout_repo()
    except Unusable as e:
        reads = str(e)
    if reads.lower() != repo.lower():
        # Removed again, so a rerun adds and checks it anew rather than finding it.
        git('remote', 'remove', name)
        raise Unusable(f'with remote {name} added the checkout no longer reads as {repo} ({reads}): add a remote for {head_repo} by hand')
    return name, True


def fetch_head_branch(remote, branch):
    git('fetch', '--quiet', remote, f'+refs/heads/{branch}:refs/remotes/{remote}/{branch}')


def bind_upstream(branch, remote, head_branch, head):
    """Make the review branch track the PR's head branch, as pr-babysit's preflight requires, once that branch is
    fetched at head; another upstream already set is refused, never replaced."""
    want, have = (remote, f'refs/heads/{head_branch}'), (config(f'branch.{branch}.remote'), config(f'branch.{branch}.merge'))
    if any(have) and have != want:
        raise Unusable(f'branch {branch} already tracks {have[0]} {have[1]}, not {remote} {want[1]}: '
                       f'unset it by hand (git branch --unset-upstream {branch})')
    fetch_head_branch(remote, head_branch)
    got = git('rev-parse', f'refs/remotes/{remote}/{head_branch}').strip()
    if got != head:
        raise Unusable(f'{remote}/{head_branch} is at {got}, not the PR head {head}: run again')
    git('config', f'branch.{branch}.remote', remote)
    git('config', f'branch.{branch}.merge', want[1])
    # The upstream is read through the remote's fetch refspec: one that does not map the head branch leaves none.
    tracked = run('git', 'rev-parse', '--verify', '--quiet', f'{branch}@{{upstream}}', ok=(0, 1))[1].strip()
    if tracked != head:
        if not any(have):
            git('config', '--unset', f'branch.{branch}.remote')
            git('config', '--unset', f'branch.{branch}.merge')
        raise Unusable(f"remote {remote}'s fetch refspec does not map {want[1]}, so the branch would track nothing: "
                       f'add it (git remote set-branches --add {remote} {head_branch}) and run again')


def head_repo_of(info):
    owner, name = (info.get('headRepositoryOwner') or {}).get('login'), (info.get('headRepository') or {}).get('name')
    return f'{owner}/{name}' if owner and name else None


def repair_target(repo, head_repo, info, branch, head):
    """auto-fix's push destination and whether repairs can be pushed there; when they can, the review branch is
    bound to it."""
    if not head_repo:
        return {'state': 'unavailable', 'why': 'the PR head repository is gone'}
    state, why = push_access(repo, head_repo, info)
    if state != 'available':
        return {'state': state, 'why': why}
    remote, added = head_remote(repo, head_repo, branch)
    bind_upstream(branch, remote, info['headRefName'], head)
    return {'state': state, 'why': why, 'remote': remote, 'addedRemote': added}


def reused_review(led, head):
    """repair's submittedReview or submittedWhy, and deferredAnswers when the one pending publication on `head` is
    an unsent bundle an auto-fix deferred on that review (post.py --defer-answers); any other pending draft refuses."""
    try:
        rev = submitted_last(led, head)
    except Unusable as e:
        return {'submittedReview': None, 'submittedWhy': str(e)}
    reuse = {'submittedReview': rev['receipts']['review'].get('reviewId')}
    pending = [p for p, _ in publications(led) if p['head'] == head and p['status'] == 'pending']
    one = pending[0] if len(pending) == 1 else None
    if one and one.get('auto') and not one.get('receipts') and any(one is p for p in rev.get('answerPublications', [])):
        reuse['deferredAnswers'] = len(one['answers'])
    return reuse


def remote_for(repo):
    """The remote whose URL is this repository: fetching another one would pin someone else's refs."""
    names = []
    for row in git('remote', '-v').splitlines():
        name, url = row.split()[:2]
        names.append(name)
        if re.search(rf'[:/]{re.escape(repo)}(\.git)?/?$', url):
            return name
    raise Unusable(f'no remote of this checkout is {repo} (remotes: {", ".join(sorted(set(names))) or "none"})')


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
    remain; past the top level everything is one group, the whole tree, which the diff still scopes.
    A directory inside another group is dropped: that group's pathspec already covers it."""
    parts = [p.split('/') for p in paths]
    depth = max([len(x) - 1 for x in parts] + [1])
    while depth >= 1:
        dirs = {'/'.join(x[:min(depth, len(x) - 1)]) if len(x) > 1 else x[0] for x in parts}
        got = sorted(d for d in dirs if not any(d.startswith(o + '/') for o in dirs))
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


def beside(top):
    """Whether a new worktree goes beside the checkout: it is a linked worktree."""
    dirs = git('-C', str(top), 'rev-parse', '--path-format=absolute', '--absolute-git-dir', '--git-common-dir').splitlines()
    if len(dirs) != 2 or not all(os.path.isabs(d) for d in dirs):
        raise Unusable(f'cannot tell where the review worktree goes: git rev-parse gave {dirs}')
    return os.path.realpath(dirs[0]) != os.path.realpath(dirs[1])


def own_worktree(path, top):
    """Refuse an existing tree unless it is a linked worktree of this repository rooted at path: a symlink,
    the launcher, another repository's tree or a stray directory would be switched to the PR head."""
    if path.is_symlink():
        raise Unusable(f'{path} is a symlink, not a review worktree; resolve by hand')
    got = run('git', '-C', str(path), 'rev-parse', '--path-format=absolute', '--show-toplevel', '--git-dir',
              '--git-common-dir')[1].splitlines()
    ours = git('rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    real = [os.path.realpath(p) for p in got]
    if (len(real) != 3 or real[0] != os.path.realpath(path) or real[0] == os.path.realpath(top) or real[1] == real[2]
            or real[2] != os.path.realpath(ours)):
        raise Unusable(f'{path} is not a linked worktree of this repository; resolve by hand')


def places(top, pr):
    branch = f'pr-review-{pr}'
    return Path(top) / '.worktrees' / branch, Path(top).parent / f'{Path(top).name}--{branch}'


def locks():
    """{real path: lock reason} of this repository's locked worktrees."""
    found = {}
    for entry in git('worktree', 'list', '--porcelain', '-z').split('\0\0'):
        fields = dict(f.partition(' ')[::2] for f in entry.split('\0') if f)
        if 'locked' in fields:
            found[os.path.realpath(fields['worktree'])] = fields['locked']
    return found


def run_of(reason, pr):
    """The token of the pr-review run of PR pr whose lock reason this is, else None."""
    m = RUN_LOCK.fullmatch(reason or '')
    return m[2] if m and int(m[1]) == pr else None


def worktree(top, pr, head, token):
    branch = f'pr-review-{pr}'
    nested, sibling = places(top, pr)
    found = [p for p in (nested, sibling) if p.exists() or p.is_symlink()]
    if len(found) > 1:
        raise Unusable(f'both {nested} and {sibling} exist; remove the one you do not need by hand')
    pinned = {line.split()[0] for line in git('for-each-ref', '--format=%(objectname)', f'refs/pr-review/{pr}/').splitlines()}
    if not found:
        path = sibling if beside(top) else nested
        if git('branch', '--list', branch).strip():
            tip = git('rev-parse', branch).strip()
            if tip not in pinned:
                raise Unusable(f'branch {branch} has commits this script did not pin ({tip}); resolve by hand')
            git('branch', '-f', branch, head)
            git('worktree', 'add', '--quiet', str(path), branch)
        else:
            git('worktree', 'add', '--quiet', '-b', branch, str(path), head)
        return path.as_posix(), branch
    path = found[0]
    own_worktree(path, top)
    other = run_of(locks().get(os.path.realpath(path)), pr) if path == sibling else None
    if other and other != token:
        raise Unusable(f'{path} is locked by pr-review run {other} of PR {pr}: once you know that run ended, release it with '
                       f'prepare.py --release --pr {pr} --token {other}, then prepare again')
    dirty = run('git', '-C', str(path), 'status', '--porcelain')[1].strip()
    if dirty:
        raise Unusable(f'{path} has uncommitted changes, left by an earlier run: {dirty.splitlines()[0]} ...; restore them first')
    tip = run('git', '-C', str(path), 'rev-parse', 'HEAD')[1].strip()
    if tip not in pinned:
        raise Unusable(f'{path} is at {tip}, not a head this script pinned; resolve by hand')
    run('git', '-C', str(path), 'checkout', '--quiet', '-B', branch, head)
    return path.as_posix(), branch


def hold(top, pr, wt, token):
    """Lock the sibling tree for this run, which worktree-remove's cleanup then keeps; a nested tree needs none:
    worktree-remove refuses a checkout holding one."""
    if Path(wt) != places(top, pr)[1]:
        return {'state': 'none', 'token': None}
    reason = locks().get(os.path.realpath(wt))
    if reason is None:
        git('worktree', 'lock', '--reason', f'pr-review {pr} running {token}', wt)
        return {'state': 'taken', 'token': token}
    return {'state': 'held', 'token': token} if run_of(reason, pr) == token else {'state': 'foreign', 'token': None}


def ancestor(a, b):
    return run('git', 'merge-base', '--is-ancestor', a, b, ok=(0, 1))[0] == 0


def mode_of(led, head, merge_base, full):
    rev = last(led)
    if full or not rev:
        return 'full', merge_base, rev and rev['head']
    if rev['head'] == head:
        return 'same', merge_base, head
    if ancestor(rev['head'], head) and rev.get('mergeBase') == merge_base:
        return 'incremental', rev['head'], rev['head']
    return 'full', merge_base, rev['head']


def merged_base(pr, reviewed, merge_base, head):
    """(S, content-conflict paths): the commit `git diff S head` scopes a recheck through, git's default merge of the
    reviewed head and the moved merge base. A conflict of any kind but contents is refused: its resolution need not
    show in that diff."""
    code, out = run('git', 'merge-tree', '--write-tree', '--name-only', '-z', reviewed, merge_base, ok=(0, 1))
    fields = out.split('\0')
    tree, conflicts = fields[0], []
    if code == 1:
        if '' not in fields[1:]:
            raise Unusable(f'git merge-tree {reviewed} {merge_base} reported a conflict it did not name')
        i = fields.index('', 1)
        conflicts = fields[1:i]
        named = False
        i += 1
        while i < len(fields) and fields[i].isdigit():
            n = int(fields[i])
            paths, kind = fields[i + 1:i + 1 + n], fields[i + 1 + n]
            if kind.startswith('CONFLICT'):
                named = True
                if kind != 'CONFLICT (contents)':
                    raise Unusable(f"the base merge since {reviewed} has a {kind} conflict in {', '.join(paths)}: a scoped "
                                   'recheck reads only a contents conflict; the recheck is pending')
            i += 3 + n
        if not named or not conflicts:
            raise Unusable(f'git merge-tree {reviewed} {merge_base} reported a conflict it did not name')
    if not FULL_SHA.match(tree):
        raise Unusable(f'git merge-tree {reviewed} {merge_base} wrote no tree: {out[:200]!r}')
    who = 'pr-review <pr-review@localhost> 0 +0000'
    code, sha, err = attempt('git', 'hash-object', '-t', 'commit', '-w', '--stdin',
                             input=f'tree {tree}\nparent {reviewed}\nparent {merge_base}\nauthor {who}\ncommitter {who}\n\n'
                                   f'pr-review recheck base of {head}\n')
    if code or not FULL_SHA.match(sha.strip()):
        raise Unusable(f'git hash-object could not write the recheck base: {err.strip()}')
    git('update-ref', f'refs/pr-review-scope/{pr}/{head}', sha.strip())
    return sha.strip(), conflicts


def recheck_of(led, pr, reviewed, head, merge_base):
    """(mode, scope base, conflicts) of the recheck after a repair from our review submitted on reviewed: mode_of's,
    but a base merge since, which moved the merge base, scopes from merged_base's commit instead of the whole PR."""
    submitted_last(led, reviewed)
    mode, scope_base, _ = mode_of(led, head, merge_base, False)
    if mode != 'full' or not ancestor(reviewed, head):
        return mode, scope_base, []
    return ('incremental', *merged_base(pr, reviewed, merge_base, head))


def prepare(a):
    top = checkout_top()
    repo = repo_of(a.repo)
    info = pr_view(repo, a.pr)
    if info.get('state') != 'OPEN':
        raise Unusable(f"PR {a.pr} is {info.get('state')}; pr-review reviews open PRs")
    head, base, merge_base = pin(remote_for(repo), repo, a.pr, info)
    path = ledger_path(repo, a.pr)
    token = a.token or secrets.token_hex(8)
    # Serialized with other prepares and releases of the PR from the ledger read to the tree's lock.
    with locked(path):
        led = load(path, repo, a.pr)
        synced = sync(repo, a.pr, led, lambda: store(path, led))
        online = [r for r, _ in publications(led) if r['status'] in ONLINE]
        if online and online[-1]['status'] == 'drafted':
            raise Unusable(f"your pending review of {online[-1]['head']} is still open on the PR: submit or delete it on GitHub first")
        if online:
            auto = ' --auto' if online[-1].get('publish') == 'auto' else ''
            raise Unusable(f"a review POST for {online[-1]['head']} may have landed unseen: reconcile it first with "
                           f"post.py --pr {a.pr} --expected-head {online[-1]['head']}{auto}, which only looks for it by its marker")
        reconciled = reconcile(repo, a.pr, led)
        if reconciled:
            store(path, led)
        conflicts = []
        if a.recheck_from:
            mode, scope_base, conflicts = recheck_of(led, a.pr, a.recheck_from, head, merge_base)
            prior = a.recheck_from
        else:
            mode, scope_base, prior = mode_of(led, head, merge_base, a.full)
        reuse = reused_review(led, head) if a.auto_fix and mode == 'same' else {}
        if 'deferredAnswers' not in reuse:
            refuse_unsettled(led, head, ('pending',))
        wt, branch = worktree(top, a.pr, head, token)
        repair = {**repair_target(repo, head_repo_of(info), info, branch, head), **reuse} if a.auto_fix else None
        pr_paths = changed(merge_base, head)
        scope = pr_paths if mode != 'incremental' else changed(scope_base, head)
        groups, over = groups_of(scope, MAX_GROUPS)
        ci, checks = ci_summary(repo, a.pr)
        facts = facts_path(repo, a.pr, head)
        facts.parent.mkdir(parents=True, exist_ok=True)
        pins = {'head': head, 'mergeBase': merge_base, 'scopeBase': scope_base, 'mode': mode, 'groups': groups,
                'recheckFrom': a.recheck_from}
        facts.write_text(json.dumps({**pins, 'changed': pr_paths, 'scope': scope, 'conflicts': conflicts, 'checks': checks,
                                     'repair': repair}, indent=1) + '\n', encoding='utf-8')
        # The selector reads one path a line: a path that line splitting breaks cannot go in the file.
        listing = ''.join(f'{p}\n' for p in pr_paths)
        changed_file = facts.with_name(f'changed-{head}.txt') if listing.splitlines() == pr_paths else None
        if changed_file:
            # Bytes: text mode would end each line with CRLF on Windows.
            changed_file.write_bytes(listing.encode('utf-8'))
        # Last, so a prepare that fails leaves no lock behind.
        lock = hold(top, a.pr, wt, token)
    head_repo = f"{(info.get('headRepositoryOwner') or {}).get('login')}/{(info.get('headRepository') or {}).get('name')}"
    return {
        'repo': repo, 'pr': a.pr, 'url': info['url'], 'title': info['title'],
        'author': (info.get('author') or {}).get('login'), 'fork': bool(info.get('isCrossRepository')),
        'headRepo': head_repo, 'headBranch': info['headRefName'], 'baseBranch': info['baseRefName'],
        'head': head, 'base': base, 'mergeBase': merge_base, 'worktree': wt, 'branch': branch,
        'mode': mode, 'scopeBase': scope_base, 'priorHead': prior, 'recheckFrom': a.recheck_from, 'conflicts': conflicts,
        'lock': lock,
        'changed': len(pr_paths), 'scoped': len(scope), 'groups': groups, 'overCap': over,
        'tooling': [p for p in pr_paths if TOOLING.search(p)], 'ci': ci,
        'ledger': path.as_posix(), 'facts': facts.as_posix(), 'synced': synced, 'reconciled': reconciled, 'changedFile': changed_file and changed_file.as_posix(),
        **({'repair': repair} if a.auto_fix else {}),
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
        pinned = json.loads(facts.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise Unusable(f'no prepare facts for {local} at {facts}: run prepare first')
    return {'ok': True, 'head': local, 'top': Path(os.path.realpath(top)).as_posix(), 'repo': repo, 'pr': a.pr, 'ci': ci_summary(repo, a.pr)[0],
            # A prepare from before --recheck-from pinned no recheck.
            'pins': {**{k: pinned[k] for k in ('mergeBase', 'scopeBase', 'mode', 'groups')}, 'recheckFrom': pinned.get('recheckFrom')}}


def release(a):
    repo = repo_of(a.repo)
    with locked(ledger_path(repo, a.pr)):
        runs = {p: t for p, t in ((p, run_of(r, a.pr)) for p, r in locks().items()) if t}
        ours = [p for p, t in runs.items() if t == a.token]
        if runs and not ours:
            raise Unusable(f"no tree carries run {a.token}'s lock and another pr-review run of PR {a.pr} holds "
                           f"{', '.join(runs)}: nothing released")
        for p in ours:
            if not Path(p).name.endswith(f'--pr-review-{a.pr}'):
                raise Unusable(f"{p} carries run {a.token}'s lock but is not a pr-review-{a.pr} tree: unlock it by hand")
        for p in ours:
            git('worktree', 'unlock', p)
    return {'pr': a.pr, 'token': a.token, 'released': [Path(p).as_posix() for p in ours]}


def collect(argv):
    p = Parser(prog='prepare.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--repo')
    p.add_argument('--check', action='store_true')
    p.add_argument('--release', action='store_true')
    p.add_argument('--token')
    p.add_argument('--expected-head')
    p.add_argument('--full', action='store_true')
    p.add_argument('--auto-fix', action='store_true')
    p.add_argument('--recheck-from')
    a = p.parse_args(argv)
    if a.token is not None and not re.fullmatch(r'[0-9a-f]+', a.token):
        raise Unusable(f'--token must be the hex token prepare reported, not {a.token!r}')
    if a.auto_fix and (a.check or a.release):
        raise Unusable('--auto-fix is for a prepare, not --check or --release')
    if a.recheck_from is not None:
        if not FULL_SHA.match(a.recheck_from):
            raise Unusable(f'--recheck-from takes the reviewed head, a full SHA, not {a.recheck_from!r}')
        if a.full or a.auto_fix or a.check or a.release:
            raise Unusable('--recheck-from is a prepare of its own: not with --full, --auto-fix, --check or --release')
    if a.release:
        if not a.token:
            raise Unusable('--release needs --token, the token of the lock to release')
        return release(a)
    return check(a) if a.check else prepare(a)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
