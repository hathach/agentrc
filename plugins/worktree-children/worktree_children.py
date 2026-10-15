#!/usr/bin/env python3
"""Remove the sibling child worktrees of a herdr worktree once herdr has removed it.

    worktree_children.py event               # herdr's worktree.removed hook (HERDR_PLUGIN_EVENT_JSON)
    worktree_children.py cleanup <parent> [--branch <common git dir> <branch> <oid>]...  # a retry

Children of `<dir>/<name>` are the entries `<dir>/<name>--*` (grandchildren `<name>--a--b`
too) with a `.git` file that are linked git worktrees of any repository: the `<name>--` prefix is reserved for
them by the user rule. Nothing runs while `<dir>/<name>` exists again, checked before each removal
too: a late event or a retry must not sweep the children of a replacement parent.

A child is removed only when every check below passes; otherwise it is kept and its reasons reported.
`git worktree remove` re-checks cleanliness at removal; `--force`, which skips that, is used only
for a child with submodules (git refuses those otherwise), so a write racing its checks is lost.
- herdr confirms it is not open as its own workspace (`herdr worktree list --cwd <child>`);
  herdr unreachable or an unexpected answer keeps it;
- no headless chief runs in it and none of its cowork lanes holds a request (running or
  undelivered); the skills' own code decides, and code that cannot load keeps it;
- not locked; HEAD on a branch, or a detached HEAD reachable from the branches, tags or remotes,
  the remotes' only after fetching each configured remote as branches are (below), first;
- in the child and in every initialized submodule, recursively: `git status` shows no tracked
  or untracked change, no index entry is flagged assume-unchanged or skip-worktree, and no
  merge, cherry-pick, revert, rebase or bisect is in progress;
- its private refs (refs/worktree, refs/bisect, refs/rewritten) are reachable from the
  repository's branches, tags or remotes, fetched the same way;
- no repository (bare ones included) or worktree is nested anywhere below it (ignored
  directories included) other than its submodules;
- every repository whose storage the removal deletes (submodule git dirs inside the child or
  inside its git dir, initialized or not) has no stash, no linked worktree (even one inside the
  child or pruned), its main worktree inside that boundary, and
  each of its refs and HEAD either a commit reachable from its own remote-tracking refs or
  present under the same name and object id in the primary checkout's copy of that submodule
  (`<common git dir>/modules/<name>`, used only when self-contained: no alternates, no commondir,
  no symlink);
  an annotated tag therefore needs that copy. Remote-tracking refs are trusted as fetched.
Deleted by policy: ignored files and commits only a reflog still names. No `git worktree prune`
runs: it would touch unrelated worktrees. herdr removes the parent before this runs, with its
own checks only: the worktree-remove skill's pending-work gate is skipped by a removal from
herdr's menu.

Branches: the parent's (the event's branch in its `repo_key` repository) and each removed
child's are deleted when nothing but the branch name is lost, otherwise kept with a reason:
- not symbolic, at the object id recorded before the removal, checked out in no worktree,
  and not the default branch its remote advertises;
- its remote (`branch.<name>.remote`, else origin) is a configured remote;
- after fetching that remote's branches into its remote-tracking refs (pruned, tags untouched),
  they reach the tip; or else, for a remote on github.com, a PR of any state has that branch and
  exactly that tip as its head, and the remote's `refs/pull/<N>/head` is that tip (squash and
  rebase merges);
- any failed or incomplete lookup keeps it.
The ref is deleted against its recorded id (`update-ref -d`), then its `branch.<name>` config.

Each action is printed and appended to $HERDR_PLUGIN_STATE_DIR/log, each branch's retry
arguments before its checkout is removed; anything kept or failed also raises one herdr
notification naming the retry command. Exit 0 every child and branch removed (or none found),
1 something kept or a removal failed, 2 bad input or a parent that exists again.
"""
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

PRIVATE_REFS = ('refs/worktree/', 'refs/bisect/', 'refs/rewritten/')
GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_PREFIX', 'GIT_NAMESPACE')
ENV = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION}  # so `git -C <child>` means the child
OPERATIONS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_LOG')
HERDR = os.environ.get('HERDR_BIN_PATH') or 'herdr'
STATE = os.environ.get('HERDR_PLUGIN_STATE_DIR')
LOG = Path(STATE) / 'log' if STATE else None
SKILLS = Path(__file__).resolve().parents[2] / 'skills'
SEARCH = '''query($q: String!, $endCursor: String) {
  search(query: $q, type: ISSUE, first: 100, after: $endCursor) {
    issueCount
    nodes { ... on PullRequest { number state headRefName headRefOid mergedAt headRepositoryOwner { login } } }
    pageInfo { hasNextPage endCursor } } }'''
GITHUB = re.compile(r'github\.com[:/]')


class Keep(Exception):
    """A reason this child stays."""


def run(args, cwd=None, input=None):
    """A finished command; one that cannot start reads as exit 127, so its check keeps the child."""
    try:
        return subprocess.run(args, cwd=cwd, input=input, capture_output=True, text=True, env=ENV)
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, '', str(e))


def git(*args, cwd=None, gitdir=None, input=None):
    """stdout of a git command that must succeed; Keep when it fails."""
    head = ['git'] + (['--git-dir', str(gitdir)] if gitdir else ['-C', str(cwd)])
    done = run(head + list(args), input=input)
    if done.returncode:
        raise Keep(f'`git {" ".join(args)}` failed in {gitdir or cwd}: {done.stderr.strip()}')
    return done.stdout


def head_oid(gitdir):
    """HEAD's object id, or None on a branch not yet born; Keep when HEAD cannot be read."""
    done = run(['git', '--git-dir', str(gitdir), 'rev-parse', '-q', '--verify', 'HEAD'])
    if not done.returncode and done.stdout.strip():
        return done.stdout.strip()
    branch = run(['git', '--git-dir', str(gitdir), 'symbolic-ref', '-q', 'HEAD'])
    if done.returncode == 1 and not branch.returncode and not refs(gitdir, branch.stdout.strip()):
        return None
    raise Keep(f'HEAD unreadable in {gitdir} (exit {done.returncode}: {done.stderr.strip()})')


def refs(gitdir, *patterns):
    """[(oid, object type, refname)] of the refs matching patterns (all when none)."""
    out = git('for-each-ref', '--format=%(objectname) %(objecttype) %(refname)', *patterns, gitdir=gitdir)
    return [tuple(l.split(' ', 2)) for l in out.splitlines()]


def unkept(entries, gitdir, *, against=('--branches', '--tags', '--remotes')):
    """The (oid, type, name) entries `against` does not hold: a commit none of them reaches, or any
    other object (an annotated tag is no ref's ancestor)."""
    commits = [oid for oid, kind, _ in entries if kind == 'commit']
    off = set(git('rev-list', '--stdin', '--not', *against, gitdir=gitdir,
                  input=''.join(f'{oid}\n' for oid in commits)).split()) if commits else set()
    return [e for e in entries if e[1] != 'commit' or e[0] in off]


def is_store(dirs, files):
    """git's own test for a repository (setup.c is_git_directory): HEAD, objects/ and refs/."""
    return 'HEAD' in files and 'objects' in dirs and 'refs' in dirs


def has_symlink(top):
    """Whether top is or holds a symlink."""
    return os.path.islink(top) or any(links for _, _, _, links in walk(top))


def common_dir(gitdir):
    """The common git dir git resolves for gitdir, or None when git cannot read it."""
    done = run(['git', '--git-dir', str(gitdir), 'rev-parse', '--path-format=absolute', '--git-common-dir'])
    return None if done.returncode else os.path.realpath(done.stdout.strip())


def walk(top):
    """Top-down os.walk (symlinks listed, not entered) that also yields each directory's symlink names,
    and keeps the child on an entry it cannot list or classify, a symlink whose target cannot be read
    included: os.walk and os.path.isdir file an entry whose stat fails as a non-directory."""
    stack = [top]
    while stack:
        top = stack.pop()
        dirs, files, links, real = [], [], [], set()
        try:
            with os.scandir(top) as listing:
                for entry in listing:
                    try:
                        if entry.is_symlink():
                            links.append(entry.name)
                        if entry.is_dir(follow_symlinks=False):
                            real.add(entry.name)
                        elif not entry.is_dir():  # a dangling link is a file; a looping one raises
                            files.append(entry.name)
                            continue
                    except OSError as err:
                        raise Keep(f'cannot classify {entry.path}: {err.strerror}')
                    dirs.append(entry.name)
        except OSError as err:
            raise Keep(f'cannot list {err.filename}: {err.strerror}')
        yield top, dirs, files, links
        stack += [os.path.join(top, d) for d in dirs if d in real]


def is_within(path, roots):
    path = os.path.realpath(path)
    return any(path == r or path.startswith(r + os.sep) for r in map(os.path.realpath, roots))


def worktree_entries(gitdir):
    """[{'worktree': path, 'locked': ..., ...}] from `git worktree list --porcelain -z`."""
    entries, cur = [], {}
    for field in git('worktree', 'list', '--porcelain', '-z', gitdir=gitdir).split('\0'):
        if not field:
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, value = field.partition(' ')
        cur[key] = value
    return entries


def children(parent):
    parent = Path(parent)
    if not parent.parent.is_dir():
        return []
    return sorted(p for p in parent.parent.iterdir() if p.name.startswith(parent.name + '--') and p.is_dir()
                  and not p.is_symlink() and (p / '.git').is_file())


def linked_worktree(child):
    """(private git dir, common git dir) of a linked worktree rooted at child; Keep otherwise."""
    top, gitdir, common = git('rev-parse', '--path-format=absolute', '--show-toplevel', '--absolute-git-dir',
                              '--git-common-dir', cwd=child).splitlines()
    if os.path.realpath(top) != os.path.realpath(child):
        raise Keep(f'its git top level is {top}')
    if gitdir == common:
        raise Keep('a primary checkout, not a linked worktree')
    return Path(gitdir), Path(common)


def herdr_entry(path):
    """herdr's `worktree list` entry for the checkout at path; Keep when herdr cannot say."""
    done = run([HERDR, 'worktree', 'list', '--cwd', str(path)])
    try:
        if done.returncode:
            raise ValueError
        mine = [w for w in json.loads(done.stdout)['result']['worktrees']
                if os.path.realpath(w['path']) == os.path.realpath(path)]
    except (ValueError, KeyError, TypeError):
        raise Keep(f'herdr could not list it (exit {done.returncode}: {done.stderr.strip()[:200]})')
    if len(mine) != 1:
        raise Keep('herdr does not list it')
    return mine[0]


def check_herdr(child):
    workspace = herdr_entry(child).get('open_workspace_id')
    if workspace:
        raise Keep(f'open in herdr workspace {workspace}')


def check_repo(path):
    """Uncommitted state of one checkout (the child or a submodule); returns its git dir."""
    if git('status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none', cwd=path).strip():
        raise Keep(f'uncommitted or untracked changes in {path}')
    flagged = [l for l in git('ls-files', '-v', cwd=path).splitlines() if l[:1].islower() or l[:1] == 'S']
    if flagged:
        raise Keep(f'{len(flagged)} index entries flagged assume-unchanged or skip-worktree in {path}')
    gitdir = Path(git('rev-parse', '--absolute-git-dir', cwd=path).strip())
    busy = [op for op in OPERATIONS if (gitdir / op).exists()]
    if busy:
        raise Keep(f'{busy[0]} in progress in {path}')
    return gitdir


def submodules(child):
    """Initialized submodule checkouts below child, recursively."""
    out = git('submodule', 'foreach', '--quiet', '--recursive', 'printf "%s/%s\\0" "$toplevel" "$sm_path"', cwd=child)
    return [Path(p) for p in out.split('\0') if p]


def nested_repo(child, allowed):
    """A .git file or directory or a bare repository below child other than at child and its submodules,
    or None. The whole tree: a repository initialized inside a tracked directory shows in no status or
    ignored listing."""
    allowed = {os.path.realpath(p) for p in [child, *allowed]}
    for top, dirs, files, _ in walk(child):
        if ('.git' in dirs or '.git' in files or is_store(dirs, files)) and os.path.realpath(top) not in allowed:
            return top
        if '.git' in dirs:
            dirs.remove('.git')
    return None


def doomed_stores(child, gitdir, sub_gitdirs):
    """Git dirs the removal deletes: submodule git dirs inside the child or its git dir, and every
    repository stored anywhere under the child's git dir (deinitialized submodules included)."""
    stores = {os.path.realpath(d) for d in sub_gitdirs if is_within(d, [child, gitdir])}
    # the whole tree, so a repository placed in a store's refs/ or logs/ is found; remote branches named
    # both objects/* and refs/* make refs/remotes/<remote> pass as a store, which then keeps the child
    stores.update(os.path.realpath(top) for top, dirs, files, _ in walk(gitdir) if is_store(dirs, files))
    return sorted(stores)


def check_store(store, child, gitdir, common, doomed):
    if git('for-each-ref', 'refs/stash', gitdir=store).strip():
        raise Keep(f'a stash in submodule repository {store}')
    # a linked worktree's HEAD and refs/worktree are its own and checked nowhere, even when inside the child
    for i, wt in enumerate(worktree_entries(store)):
        if 'worktree' in wt and (i or not is_within(wt['worktree'], [child, gitdir])):
            raise Keep(f'submodule repository {store} has a worktree at {wt["worktree"]}')
    own = [r for r in refs(store) if not r[2].startswith('refs/remotes/')]
    head = head_oid(store)
    own += [(head, 'commit', 'HEAD')] if head else []
    pending = unkept(own, store, against=('--remotes',))
    if not pending:
        return
    modules = Path(os.path.realpath(gitdir / 'modules'))
    survivor = common / 'modules' / Path(store).relative_to(modules) if is_within(store, [modules]) else None
    # the copy vouches only when self-contained: alternates, a commondir or a symlink, none of which git
    # puts in a module store, could make it borrow from what the removal deletes
    if survivor and (is_within(survivor, [child, gitdir, *doomed]) or not (survivor / 'objects').is_dir()
                     or (survivor / 'objects' / 'info' / 'alternates').exists()
                     or common_dir(survivor) != os.path.realpath(survivor) or has_symlink(survivor)):
        survivor = None
    kept = {}
    if survivor:
        kept = {name: oid for oid, _, name in refs(survivor)}
        kept['HEAD'] = head_oid(survivor)
    lost = [name for oid, _, name in pending if kept.get(name) != oid]
    if lost:
        raise Keep(f'{lost[0]} in submodule repository {store} is on no remote and not in '
                   f'{survivor or "a surviving copy"} under that name')


def check_agents(path, gitdir):
    """Keep a checkout a headless chief or a cowork lane still works in."""
    sys.path[:0] = [str(SKILLS / 'headless-chief' / 'scripts'), str(SKILLS / 'cowork' / 'scripts')]
    try:
        import chief_run
        import cowork
    except Exception as err:  # whatever stops them loading, the child stays
        raise Keep(f'cannot check for running agents: {err!r}')
    finally:
        del sys.path[:2]
    chiefs = chief_run.running_chiefs(Path(path))
    if chiefs:
        raise Keep(f'a headless chief runs in it: pid {", ".join(map(str, chiefs))}')
    for box in cowork.boxes(gitdir):
        if cowork.requests(box):
            raise Keep(f'cowork lane {box.parent.name}/{box.name} holds a running or undelivered request')


def content_check(child):
    """Raise Keep when removing the linked worktree child could lose anything but ignored files and
    reflog-only commits, or would cut off a running agent; returns the common git dir to remove it
    with and whether that needs --force."""
    gitdir, common = linked_worktree(child)
    check_agents(child, gitdir)
    if (gitdir / 'locked').exists():
        raise Keep('locked')
    subs = submodules(child)
    sub_gitdirs = [check_repo(repo) for repo in [child, *subs]][1:]
    held = refs(gitdir, *PRIVATE_REFS)
    if run(['git', '-C', str(child), 'symbolic-ref', '-q', 'HEAD']).returncode:
        head = head_oid(gitdir)
        held += [(head, 'commit', 'HEAD')] if head else []
    held = unkept(held, gitdir, against=('--branches', '--tags'))
    if held:  # the branch settling's pruning fetch must not drop a remote-tracking ref after it vouched
        for remote in git('remote', gitdir=common).split():
            refresh(common, remote)
    for _, _, name in unkept(held, gitdir):
        raise Keep('detached HEAD on a commit no branch, tag or remote holds' if name == 'HEAD'
                   else f'private ref {name} holds an object no shared ref holds')
    nested = nested_repo(child, subs)
    if nested:
        raise Keep(f'nested repository or worktree at {nested}')
    doomed = doomed_stores(child, gitdir, sub_gitdirs)
    for store in doomed:
        check_store(store, child, gitdir, common, doomed)
    # plain removal refuses a populated submodule or any modules/ store, deinitialized ones included
    return common, bool(subs) or (gitdir / 'modules').exists()


def check(child):
    """content_check for a child herdr has not open as a workspace of its own."""
    check_herdr(child)
    return content_check(child)


def head_branch(path):
    """(branch name, tip) of the checkout at path, or None when HEAD is detached or unborn."""
    branch = run(['git', '-C', str(path), 'symbolic-ref', '-q', 'HEAD'])
    tip = run(['git', '-C', str(path), 'rev-parse', '-q', '--verify', 'HEAD'])
    if branch.returncode or tip.returncode or not branch.stdout.startswith('refs/heads/'):
        return None
    return branch.stdout.strip().removeprefix('refs/heads/'), tip.stdout.strip()


def gh_lines(*args):
    """gh's output, one item per line; any error keeps, never reads as empty."""
    done = run(['gh', *args])
    if done.returncode:
        raise Keep(f'`gh {args[0]} {args[1]}` failed: {(done.stdout + done.stderr).strip()[:300]}')
    return [line for line in done.stdout.splitlines() if line]


@functools.lru_cache(maxsize=None)
def github_repo(common, remote):
    """owner/name of the GitHub repository remote points to, None for a remote not on github.com;
    Keep when gh cannot name it."""
    url = git('remote', 'get-url', remote, gitdir=common).strip()
    if not GITHUB.search(url):
        return None
    repo = gh_lines('repo', 'view', url, '--json', 'nameWithOwner', '-q', '.nameWithOwner')
    if len(repo) != 1 or repo[0].count('/') != 1:
        raise Keep(f'gh names no repository for {url}: {repo}')
    return repo[0]


@functools.lru_cache(maxsize=None)
def pulls(repo, branch, owner=None):
    """Every PR of repo whose head branch is exactly branch, deleted branches included, as
    {number, head (oid), state, merged_at}; with owner, only those from owner's repository (a
    fork's). `head:` search matches by prefix, across forks, and stops at 1000 results, so a short
    answer keeps, as does a malformed one."""
    lines = gh_lines('api', 'graphql', '--paginate', '-f', f'query={SEARCH}',
                     '-f', f'q=repo:{repo} is:pr head:{branch}', '--jq', '.data.search')
    try:
        pages = [json.loads(line) for line in lines]
        nodes = [n for page in pages for n in page['nodes']]
        if not pages or len(nodes) != pages[0]['issueCount']:
            raise Keep(f'PR search of {repo} for head:{branch} returned {len(nodes)} of '
                       f'{pages[0]["issueCount"] if pages else "an unknown number"}')
        return [dict(number=int(n['number']), head=str(n['headRefOid']), state=str(n['state']),
                     merged_at=n['mergedAt']) for n in nodes
                if n['headRefName'] == branch
                and (owner is None or (n['headRepositoryOwner'] or {}).get('login') == owner)]
    except (ValueError, KeyError, TypeError, AttributeError) as err:
        raise Keep(f'PR search of {repo} for head:{branch} gave a malformed answer: {err!r}')



def branch_remote(common, branch):
    """The configured remote branch pushes to: its upstream's, else origin; Keep for `.` or none."""
    done = run(['git', '--git-dir', str(common), 'config', f'branch.{branch}.remote'])
    remote = done.stdout.strip() if not done.returncode else 'origin'
    if remote not in git('remote', gitdir=common).split():
        raise Keep(f'{remote} is not a configured remote')
    return remote


def refresh(common, remote):
    """The branch remote advertises as its default, after fetching its branches into its
    remote-tracking refs and nothing else: the empty --refmap drops the configured mappings,
    which would also update the refs they name, and tags are neither fetched nor pruned.
    Once per repository and remote: the checks rely on the refs that fetch left."""
    return fetched(os.path.realpath(common), remote)


@functools.lru_cache(maxsize=None)
def fetched(common, remote):
    m = re.search(r'^ref: refs/heads/(\S+)\tHEAD$', git('ls-remote', '--symref', remote, 'HEAD', gitdir=common), re.M)
    if not m:
        raise Keep(f'{remote} advertises no default branch')
    git('fetch', '--quiet', '--no-tags', '--no-prune-tags', '--prune', '--refmap=', remote,
        f'+refs/heads/*:refs/remotes/{remote}/*', gitdir=common)
    return m.group(1)


def owners(common, branch):
    return [wt['worktree'] for wt in worktree_entries(common) if wt.get('branch') == f'refs/heads/{branch}']


def branch_proof(common, branch, tip, leaving=()):
    """How branch at tip survives its deletion ('on origin', 'head of PR #N'); Keep when it might not.
    A worktree in leaving, about to be removed, does not count as checking it out."""
    common = str(common)
    ref = f'refs/heads/{branch}'
    if not run(['git', '--git-dir', common, 'symbolic-ref', '-q', ref]).returncode:
        raise Keep(f'{ref} is a symbolic ref')
    now = run(['git', '--git-dir', common, 'rev-parse', '-q', '--verify', ref]).stdout.strip()
    if now != tip:
        raise Keep(f'{ref} is at {now or "nothing"}, not {tip}')
    held = [w for w in owners(common, branch) if not is_within(w, leaving)]
    if held:
        raise Keep(f'checked out at {held[0]}')
    remote = branch_remote(common, branch)
    if refresh(common, remote) == branch:
        raise Keep(f'the default branch of {remote}')
    if not git('rev-list', tip, '--not', f'--remotes={remote}', gitdir=common).strip():
        return f'on {remote}'
    try:
        repo = github_repo(common, remote)
        heads = [pr['number'] for pr in pulls(repo, branch) if pr['head'] == tip] if repo else []
    except Keep as why:
        raise Keep(f'commits on no {remote} branch, and no PR head: {why}')
    for number in heads:
        if git('ls-remote', remote, f'refs/pull/{number}/head', gitdir=common).split()[:1] == [tip]:
            return f'head of PR #{number}'
    raise Keep(f'commits on no {remote} branch and no PR head')


def delete_branch(common, branch, tip):
    """Delete branch at tip once branch_proof holds; Keep otherwise."""
    proof = branch_proof(common, branch, tip)
    held = owners(common, branch)  # again: a checkout can race the network lookups
    if held:
        raise Keep(f'checked out at {held[0]}')
    git('update-ref', '--no-deref', '-d', f'refs/heads/{branch}', tip, gitdir=common)
    run(['git', '--git-dir', str(common), 'config', '--remove-section', f'branch.{branch}'])
    return proof


def remove(child, common, force):
    done = run(['git', '--git-dir', str(common), 'worktree', 'remove', *['--force'] * force, str(child)])
    if done.returncode:
        raise Keep(f'`git worktree remove` failed, possibly part way: {done.stderr.strip()}')


def log(line):
    print(line, flush=True)
    if LOG:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {line}\n')


def retry(parent, branches):
    return shlex.join([str(Path(__file__).resolve()), 'cleanup', str(parent),
                       *[a for b in branches for a in ('--branch', *map(str, b))]])


def notify(parent, kept, branches, retried):
    tail = f' Retry: {Path(__file__).resolve()} cleanup {parent}'
    tail += ' with the --branch arguments the log names' if retried else ''
    tail += f' Full report: {LOG}' if LOG else ''
    reasons = '; '.join(f'{name}: {why}' for name, why in kept + [(f'branch {b}', why) for b, why in branches])
    room = 999 - len(tail)  # a toast, not the report: 1000 characters, the '.' included
    if len(reasons) > room:
        reasons = reasons[:max(0, room - 3)] + '...'
    run([HERDR, 'notification', 'show',
         f'Kept {len(kept)} child worktree(s) and {len(branches)} branch(es) of {Path(parent).name}',
         '--body', reasons + '.' + tail, '--sound', 'request'])


def cleanup(parent, branches=(), kept_branches=()):
    """Remove parent's clean children, then delete the given branches and each removed child's;
    a branch is (common git dir, name, tip); kept_branches pairs a branch name with why it stays."""
    parent = Path(parent)
    if parent.exists():
        log(f'{parent} exists: not touching its children')
        return 2
    kept, kept_branches, unsettled = [], list(kept_branches), []

    def settle(branch):
        try:
            log(f'deleted branch {branch[1]} in {branch[0]}: {delete_branch(*branch)}')
        except (Keep, OSError) as why:
            kept_branches.append((branch[1], str(why)))
            unsettled.append(branch)
            log(f'kept branch {branch[1]} in {branch[0]}: {why}')

    for name, why in kept_branches:
        log(f'kept branch {name}: {why}')
    if branches:
        log(f'branches to settle: {retry(parent, branches)}')
    branches, stopped = list(branches), False
    for child in children(parent):
        try:
            removal = check(child)
            head = head_branch(child)
            branch = head and (str(removal[0]), *head)
            if branch:
                log(f'removing {child}; its branch is retried by: {retry(parent, [branch])}')
            if parent.exists():
                log(f'{parent} exists again: not touching {child} or the children after it')
                stopped = True
                break
            remove(child, *removal)
            log(f'removed {child}')
        except (Keep, OSError) as why:  # an unexpected filesystem error keeps the child too
            kept.append((child.name, str(why)))
            log(f'kept {child}: {why}')
            continue
        if branch:
            branches.append(branch)
    stopped = stopped or parent.exists()  # it may have returned during a check that kept its child
    for branch in branches:  # only now: a detached child checked above may hang on one of them
        if stopped:  # the parent's replacement may not have checked its branch out yet
            kept_branches.append((branch[1], f'{parent} exists again'))
            unsettled.append(branch)
        else:
            settle(branch)
    if unsettled:
        log(f'retry: {retry(parent, unsettled)}')
    if kept or kept_branches:
        notify(parent, kept, kept_branches, unsettled)
    return 2 if stopped else 1 if kept or kept_branches else 0


def event_branch(data):
    """The removed parent's branch as (common git dir, name, tip); Keep when the event names a
    branch it gives no repository holding; None when it names no branch."""
    branch = (data['worktree'].get('branch') or '').strip()
    if not branch:
        return None
    space = (data.get('workspace') or {}).get('worktree') or {}
    common = space.get('repo_key') or (space.get('repo_root') and common_dir(Path(space['repo_root']) / '.git'))
    if not common:
        raise Keep('the event names no repository')
    return common, branch, git('rev-parse', '--verify', f'refs/heads/{branch}', gitdir=common).strip()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['cleanup'] and len(argv) >= 2:
        rest = argv[2:]
        if len(rest) % 4 == 0 and all(f == '--branch' for f in rest[::4]):
            return cleanup(os.path.abspath(argv[1]), [tuple(rest[i + 1:i + 4]) for i in range(0, len(rest), 4)])
    if argv == ['event']:
        try:
            data = json.loads(os.environ['HERDR_PLUGIN_EVENT_JSON'])['data']
            path = data['worktree']['path']
            if not os.path.isabs(path):
                raise ValueError(f'{path!r} is not absolute')
        except (KeyError, ValueError, TypeError) as err:
            print(f'worktree.removed event without an absolute data.worktree.path: {err!r}', file=sys.stderr)
            return 2
        try:
            branch = event_branch(data)
        except Keep as why:  # the parent's branch stays; its children are still cleaned up
            return cleanup(path, kept_branches=[(data['worktree']['branch'], str(why))])
        return cleanup(path, [branch] if branch else [])
    print(__doc__.split('\n\n')[1], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
