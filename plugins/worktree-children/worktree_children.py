#!/usr/bin/env python3
"""Remove the sibling child worktrees of a herdr worktree once herdr has removed it.

    worktree_children.py event               # herdr's worktree.removed hook (HERDR_PLUGIN_EVENT_JSON)
    worktree_children.py cleanup <parent>    # the same for a parent path, by hand (a retry)

Children of `<dir>/<name>` are the entries `<dir>/<name>--*` (grandchildren `<name>--a--b`
too) with a `.git` file that are linked git worktrees of any repository: the `<name>--` prefix is reserved for
them by the user rule. Nothing runs while `<dir>/<name>` exists again: a late event or a retry
must not sweep the children of a replacement parent.

A child is removed (`git worktree remove --force`, needed because submodules make git refuse
otherwise) only when every check below passes; otherwise it is kept and its reasons reported:
- herdr confirms it is not open as its own workspace (`herdr worktree list --cwd <child>`);
  herdr unreachable or an unexpected answer keeps it;
- not locked; HEAD on a branch, or a detached HEAD reachable from the branches, tags or remotes;
- in the child and in every initialized submodule, recursively: `git status` shows no tracked
  or untracked change, no index entry is flagged assume-unchanged or skip-worktree, and no
  merge, cherry-pick, revert, rebase or bisect is in progress;
- its private refs (refs/worktree, refs/bisect, refs/rewritten) are reachable from the
  repository's branches, tags or remotes;
- no repository or worktree is nested anywhere below it (ignored directories included) other
  than its submodules;
- every repository whose storage the removal deletes (submodule git dirs inside the child or
  inside its git dir, initialized or not) has no stash, no worktree outside that boundary, and
  each of its refs and HEAD either a commit reachable from its own remote-tracking refs or
  present under the same name and object id in the primary checkout's copy of that submodule
  (`<common git dir>/modules/<name>`, used only when it borrows no objects through alternates);
  an annotated tag therefore needs that copy. Remote-tracking refs are trusted as fetched.
Deleted by policy: ignored files and commits only a reflog still names. The child's branch is
never deleted. No `git worktree prune` runs: it would touch unrelated worktrees.

Each action is printed and appended to $HERDR_PLUGIN_STATE_DIR/log; kept or failed children
also raise one herdr notification naming the retry command. Exit 0 every child removed (or
none found), 1 a child kept or a removal failed, 2 bad input or a parent that exists again.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PRIVATE_REFS = ('refs/worktree/', 'refs/bisect/', 'refs/rewritten/')
OPERATIONS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_LOG')
HERDR = os.environ.get('HERDR_BIN_PATH') or 'herdr'
STATE = os.environ.get('HERDR_PLUGIN_STATE_DIR')
LOG = Path(STATE) / 'log' if STATE else None


class Keep(Exception):
    """A reason this child stays."""


def run(args, cwd=None, input=None):
    """A finished command; one that cannot start reads as exit 127, so its check keeps the child."""
    try:
        return subprocess.run(args, cwd=cwd, input=input, capture_output=True, text=True)
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
    return run(['git', '--git-dir', str(gitdir), 'rev-parse', '-q', '--verify', 'HEAD']).stdout.strip()


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


def check_herdr(child):
    done = run([HERDR, 'worktree', 'list', '--cwd', str(child)])
    try:
        if done.returncode:
            raise ValueError
        mine = [w for w in json.loads(done.stdout)['result']['worktrees']
                if os.path.realpath(w['path']) == os.path.realpath(child)]
    except (ValueError, KeyError, TypeError):
        raise Keep(f'herdr could not confirm it is closed (exit {done.returncode}: {done.stderr.strip()[:200]})')
    if len(mine) != 1:
        raise Keep('herdr does not list it')
    if mine[0].get('open_workspace_id'):
        raise Keep(f'open in herdr workspace {mine[0]["open_workspace_id"]}')


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
    """A .git file or directory below child other than at child and its submodules, or None. The whole
    tree: a repository initialized inside a tracked directory shows in no status or ignored listing."""
    allowed = {os.path.realpath(p) for p in [child, *allowed]}
    for top, dirs, files in os.walk(child):
        if ('.git' in dirs or '.git' in files) and os.path.realpath(top) not in allowed:
            return top
        if '.git' in dirs:
            dirs.remove('.git')
    return None


def doomed_stores(child, gitdir, sub_gitdirs):
    """Git dirs the removal deletes: submodule git dirs inside the child or its git dir, and every
    module repository stored under the child's git dir (deinitialized ones included)."""
    stores = {os.path.realpath(d) for d in sub_gitdirs if is_within(d, [child, gitdir])}
    for top, dirs, files in os.walk(gitdir / 'modules'):
        if 'HEAD' in files and 'objects' in dirs:
            stores.add(os.path.realpath(top))
            dirs[:] = ['modules'] if 'modules' in dirs else []
    return sorted(stores)


def check_store(store, child, gitdir, common):
    if git('for-each-ref', 'refs/stash', gitdir=store).strip():
        raise Keep(f'a stash in submodule repository {store}')
    for wt in worktree_entries(store):
        if 'worktree' in wt and not is_within(wt['worktree'], [child, gitdir]):
            raise Keep(f'submodule repository {store} has a worktree at {wt["worktree"]}')
    own = [r for r in refs(store) if not r[2].startswith('refs/remotes/')]
    own += [(head_oid(store), 'commit', 'HEAD')] if head_oid(store) else []
    pending = unkept(own, store, against=('--remotes',))
    if not pending:
        return
    modules = Path(os.path.realpath(gitdir / 'modules'))
    survivor = common / 'modules' / Path(store).relative_to(modules) if is_within(store, [modules]) else None
    kept = {}
    if survivor and (survivor / 'objects').is_dir() and not (survivor / 'objects' / 'info' / 'alternates').exists():
        kept = {name: oid for oid, _, name in refs(survivor)}
        kept['HEAD'] = head_oid(survivor)
    lost = [name for oid, _, name in pending if kept.get(name) != oid]
    if lost:
        raise Keep(f'{lost[0]} in submodule repository {store} is on no remote and not in '
                   f'{survivor or "a surviving copy"} under that name')


def check(child):
    """Raise Keep when removing child could lose anything but ignored files and reflog-only commits;
    returns the common git dir to remove it with."""
    gitdir, common = linked_worktree(child)
    check_herdr(child)
    if (gitdir / 'locked').exists():
        raise Keep('locked')
    subs = submodules(child)
    sub_gitdirs = [check_repo(repo) for repo in [child, *subs]][1:]
    held = refs(gitdir, *PRIVATE_REFS)
    if run(['git', '-C', str(child), 'symbolic-ref', '-q', 'HEAD']).returncode:
        held.append((head_oid(gitdir), 'commit', 'HEAD'))
    for _, _, name in unkept(held, gitdir):
        raise Keep('detached HEAD on a commit no branch, tag or remote holds' if name == 'HEAD'
                   else f'private ref {name} holds an object no shared ref holds')
    nested = nested_repo(child, subs)
    if nested:
        raise Keep(f'nested repository or worktree at {nested}')
    for store in doomed_stores(child, gitdir, sub_gitdirs):
        check_store(store, child, gitdir, common)
    return common


def remove(child, common):
    done = run(['git', '--git-dir', str(common), 'worktree', 'remove', '--force', str(child)])
    if done.returncode:
        raise Keep(f'`git worktree remove` failed, possibly part way: {done.stderr.strip()}')


def log(line):
    print(line, flush=True)
    if LOG:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {line}\n')


def notify(parent, kept):
    tail = f' Retry: {Path(__file__).resolve()} cleanup {parent}'
    tail += f' Full report: {LOG}' if LOG else ''
    reasons = '; '.join(f'{p.name}: {why}' for p, why in kept)
    room = 999 - len(tail)  # a toast, not the report: 1000 characters, the '.' included
    if len(reasons) > room:
        reasons = reasons[:max(0, room - 3)] + '...'
    run([HERDR, 'notification', 'show', f'Kept {len(kept)} child worktree(s) of {Path(parent).name}',
         '--body', reasons + '.' + tail, '--sound', 'request'])


def cleanup(parent):
    parent = Path(parent)
    if parent.exists():
        log(f'{parent} exists: not touching its children')
        return 2
    kept = []
    for child in children(parent):
        try:
            remove(child, check(child))
            log(f'removed {child}')
        except Keep as why:
            kept.append((child, str(why)))
            log(f'kept {child}: {why}')
    if kept:
        notify(parent, kept)
    return 1 if kept else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['cleanup'] and len(argv) == 2:
        return cleanup(os.path.abspath(argv[1]))
    if argv == ['event']:
        try:
            path = json.loads(os.environ['HERDR_PLUGIN_EVENT_JSON'])['data']['worktree']['path']
            if not os.path.isabs(path):
                raise ValueError(f'{path!r} is not absolute')
        except (KeyError, ValueError, TypeError) as err:
            print(f'worktree.removed event without an absolute data.worktree.path: {err!r}', file=sys.stderr)
            return 2
        return cleanup(path)
    print(__doc__.split('\n\n')[1], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
