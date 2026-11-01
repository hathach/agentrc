import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub, tmpdir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'worktree-remove' / 'scripts' / 'worktree_children.py'

# gh: `repo view` names t/repo; `api graphql` prints the search page in $STUB_PRS, else fails
GH = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['STUB_LOG'], 'a') as f:
    f.write(json.dumps(['gh'] + sys.argv[1:]) + '\n')
prs = os.environ.get('STUB_PRS')
if not prs:
    sys.exit('gh: network unreachable')
if sys.argv[1:3] == ['repo', 'view']:
    print('t/repo')
else:
    print(open(prs).read())
'''

# hc: answers each command line from the JSON in $STUB_HC (see hc_inventory), its nth call by `<line> #<n>` first;
# an empty inventory without one
HC = r'''import json, os, sys
args = ' '.join(a for a in sys.argv[1:] if a != '--json')
try:
    with open(os.environ['STUB_HC']) as f:
        answers = json.load(f)
except FileNotFoundError:
    answers = {'ls': [0, '{"terminals": []}', '']}
counts = os.environ['STUB_HC'] + '.calls'
seen = json.load(open(counts)) if os.path.exists(counts) else {}
seen[args] = seen.get(args, 0) + 1
json.dump(seen, open(counts, 'w'))
rc, out, err, *hang = answers.get(f'{args} #{seen[args]}') or answers.get(
    args, [3, '', '{"error": {"kind": "not_found", "message": "no terminal"}}'])
if hang:
    import time
    time.sleep(hang[0])
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(rc)
'''


def hc_inventory(at, terminals, foregrounds=None, raw=None):
    """Writes the hc stub's answers to at: `hc ls` lists terminals, live unless one says otherwise, and
    `hc get <t>` gives foregrounds[t] as its foreground's cwd (no foreground when absent); raw maps a command
    line, or `<line> #<n>` its nth call, to [exit, stdout, stderr(, seconds it hangs first)] in place of either.
    The call counts restart."""
    terminals = [{'state': 'live', 'agent': 'bash', **t} for t in terminals]
    answers = {'ls': [0, json.dumps({'terminals': terminals}), '']}
    for t in terminals:
        fg = (foregrounds or {}).get(t['address'])
        answers[f'get {t["address"]}'] = [0, json.dumps({**t, 'foreground': fg and {'cwd': str(fg), 'pid': 1}}), '']
    Path(at).write_text(json.dumps({**answers, **(raw or {})}))
    Path(f'{at}.calls').unlink(missing_ok=True)


def without_hc(path, stub):
    """path with no hc on it: the stub deleted and each other directory holding one dropped."""
    Path(stub).unlink()
    return os.pathsep.join(d for d in path.split(os.pathsep) if not shutil.which('hc', path=d))


# runs the script with something at the parent path (argv[3]) only at the third look, as a replacement
# moved in and out again would be
BLINK = r'''import os, sys, runpy
lexists, parent, looks = os.path.lexists, sys.argv[3], []
def blinking(path):
    if os.fspath(path) != parent:
        return lexists(path)
    looks.append(path)
    return len(looks) == 3
os.path.lexists = blinking
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''

# runs the script with DirEntry.is_dir failing on `inner`, as a stat can on a filesystem without d_type
UNCLASSIFIABLE = r'''import os, runpy, sys
from contextlib import contextmanager
scandir = os.scandir
class Entry:
    def __init__(self, entry):
        self.entry = entry
    def __getattr__(self, name):
        return getattr(self.entry, name)
    def is_dir(self, follow_symlinks=True):
        if self.entry.name == 'inner':
            raise OSError(5, 'Input/output error', self.entry.path)
        return self.entry.is_dir(follow_symlinks=follow_symlinks)
@contextmanager
def failing_scandir(path='.'):
    with scandir(path) as listing:
        yield (Entry(entry) for entry in listing)
os.scandir = failing_scandir
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


def setUpModule():
    guard_survivors(__name__)


@unittest.skipIf(os.name == 'nt', 'POSIX paths and a shell submodule foreach')
class WorktreeChildren(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name).resolve()
        home = self.t / 'home'
        home.mkdir()
        self.env = {**os.environ, 'HOME': str(home), 'GIT_CONFIG_GLOBAL': str(home / 'gitconfig'),
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_COUNT': '1',
                    'GIT_CONFIG_KEY_0': 'protocol.file.allow', 'GIT_CONFIG_VALUE_0': 'always'}
        self.calls, self.log = self.t / 'calls', self.t / 'state' / 'agentrc' / 'worktree-remove' / 'log'
        self.env.update(STUB_LOG=str(self.calls), XDG_STATE_HOME=str(self.t / 'state'))
        gh_dir = self.t / 'gh-bin'
        gh_dir.mkdir()
        (gh_dir / 'gh').write_text(GH)
        (gh_dir / 'gh').chmod(0o755)
        self.hc_stub, self.hc = make_stub(gh_dir, 'hc', HC), self.t / 'hc.json'
        self.env.update(PATH=f'{gh_dir}{os.pathsep}{self.env["PATH"]}', STUB_HC=str(self.hc))
        # an upstream for the submodule, and the main repository with it
        self.git('init', '-q', '--bare', str(self.t / 'sub.git'), cwd=self.t)
        self.git('clone', '-q', str(self.t / 'sub.git'), str(self.t / 'subwork'), cwd=self.t)
        self.commit(self.t / 'subwork', 'a.txt', 'sub')
        self.git('push', '-q', 'origin', 'HEAD:refs/heads/master', cwd=self.t / 'subwork')
        self.main = self.repo('main', github=True)
        self.commit(self.main, 'README', 'main')
        self.git('submodule', 'add', '-q', str(self.t / 'sub.git'), 'lib/sub', cwd=self.main)
        self.git('commit', '-qm', 'sub', cwd=self.main)
        self.git('push', '-q', 'origin', 'master', cwd=self.main)
        self.wt = self.t / 'wt'
        self.wt.mkdir()
        self.parent = self.wt / 'parent'
        self.git('worktree', 'add', '-q', str(self.parent), '-b', 'parent', cwd=self.main)
        self.sentinel = self.t / 'shared'
        self.sentinel.mkdir()
        (self.sentinel / 'keep').write_text('shared target')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd, ok=()):
        done = subprocess.run(['git', *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertIn(done.returncode, (0, *ok), f'git {args}: {done.stderr}')
        return done.stdout

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        self.git('add', name, cwd=repo)
        self.git('commit', '-qm', name, cwd=repo)
        return self.git('rev-parse', 'HEAD', cwd=repo).strip()

    def repo(self, name, github=False):
        """A repository at t/<name> on master, pushed to its bare origin t/<name>.git, or with github
        t/github.com/<name>.git, a path the module takes for a GitHub remote."""
        path = self.t / name
        origin = self.t / ('github.com' if github else '') / f'{name}.git'
        self.git('init', '-q', '--bare', '-b', 'master', str(origin), cwd=self.t)
        self.git('init', '-q', '-b', 'master', str(path), cwd=self.t)
        self.git('remote', 'add', 'origin', str(origin), cwd=path)
        self.commit(path, '.gitignore', 'build/\n')
        self.git('push', '-q', 'origin', 'master', cwd=path)
        return path

    def child(self, name='child', repo=None, init=True, push=True):
        """A clean child on branch name, pushed to origin unless push is false."""
        path = self.wt / f'parent--{name}'
        self.git('worktree', 'add', '-q', str(path), '-b', name, cwd=repo or self.main)
        if push:
            self.git('push', '-q', 'origin', name, cwd=path)
        if init and (path / '.gitmodules').exists():
            self.git('submodule', 'update', '-q', '--init', cwd=path)
        (path / 'build').mkdir()
        (path / 'build' / 'out.bin').write_text('ignored build output')
        (path / 'build' / 'deps').symlink_to(self.sentinel)
        (path / 'build' / 'stale').symlink_to(self.t / 'gone')  # a dangling link never keeps a child
        return path

    def hand_made_store(self, at):
        """A bare repository at `at`: no remote-tracking refs, so its branch is on no remote."""
        self.git('clone', '-q', '--bare', str(self.t / 'sub.git'), str(at), cwd=self.t)

    def unpublished_sub_tag(self, child, primary_ref=None):
        """Tag `saved` on a submodule commit only the child has, the checkout back on the published one;
        with primary_ref, the primary checkout's copy of the submodule gets that commit under it."""
        sub = child / 'lib' / 'sub'
        oid = self.commit(sub, 'b.txt', 'local')
        self.git('tag', 'saved', cwd=sub)
        self.git('checkout', '-q', 'HEAD~1', cwd=sub)
        if primary_ref:
            self.git('submodule', 'update', '-q', '--init', cwd=self.main)
            self.git('fetch', '-q', '--no-tags', str(sub), f'{oid}:{primary_ref}', cwd=self.main / 'lib' / 'sub')
        return sub

    def relocated_sub_store(self, child):
        """Moves the child's submodule store to `custom-store` in its git dir and stashes a change there."""
        sub = child / 'lib' / 'sub'
        gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip())
        old = Path(self.git('rev-parse', '--absolute-git-dir', cwd=sub).strip())
        store = gitdir / 'custom-store'
        old.rename(store)
        (sub / '.git').write_text(f'gitdir: {store}\n')
        self.git('config', '--file', str(store / 'config'), 'core.worktree', str(sub), cwd=child)
        (sub / 'a.txt').write_text('stashed')
        self.git('stash', '-q', cwd=sub)
        return store

    def git_wrapper(self, case):
        """PATH for a `git` that runs the shell `case` arm on its arguments first."""
        bin_dir = self.t / 'bin'
        bin_dir.mkdir(exist_ok=True)
        wrapper = bin_dir / 'git'
        wrapper.write_text(f'#!/bin/sh\ncase " $* " in {case};; esac\n'
                           f'exec {shutil.which("git", path=self.env["PATH"])} "$@"\n')
        wrapper.chmod(0o755)
        return {'PATH': f'{bin_dir}{os.pathsep}{self.env["PATH"]}'}

    def drop_parent(self):
        self.git('worktree', 'remove', '--force', str(self.parent), cwd=self.main)

    def replace_parent(self, mode):
        """Shell moving a replacement in at the parent path, a directory or with `dangle` a dangling symlink,
        unless one is there."""
        parent = shlex.quote(str(self.parent))
        make = f'ln -s {parent}-gone {parent}' if mode == 'dangle' else f'mkdir {parent}'
        return f'[ -e {parent} ] || [ -L {parent} ] || {make}'

    def run_script(self, *argv, env=None, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *argv], env={**self.env, **(env or {})},
                              capture_output=True, text=True, cwd=cwd)

    def cleanup(self):
        self.drop_parent()
        return self.run_script('cleanup', str(self.parent))

    def retry(self, *branches):
        """The retry command the script logs for the parent and branches, (common, name, tip) each."""
        return shlex.join([sys.executable, str(SCRIPT), 'cleanup', str(self.parent),
                           *[a for b in branches for a in ('--branch', *map(str, b))]])

    def logged_retries(self):
        """The retry commands in the log, each split into its arguments."""
        lines = [line.split(' ', 2)[2] for line in self.log.read_text(encoding='utf-8').splitlines()]
        return [shlex.split(line.removeprefix('retry: ')) for line in lines if line.startswith('retry: ')]

    def gh_calls(self):
        if not self.calls.exists():
            return []
        return [c for c in map(json.loads, self.calls.read_text().splitlines()) if c[:1] == ['gh']]

    def assert_kept(self, child, reason):
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(child.is_dir())
        self.assertIn(f'kept {child}: ', done.stdout)
        self.assertIn(reason, done.stdout)
        self.assertIn(f'retry: {self.retry()}\n', done.stdout)
        self.assertIn(f'retry: {self.retry()}\n', self.log.read_text(encoding='utf-8'))

    def assert_removed(self, *children):
        done = self.cleanup()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for child in children:
            self.assertFalse(child.exists(), child)
            self.assertIn(f'removed {child}', done.stdout)
        return done

    # --- removed ------------------------------------------------------------------

    def test_a_clean_child_goes_with_its_ignored_files_and_pushed_branch_and_keeps_shared_targets(self):
        child = self.child()
        self.git('config', 'branch.child.description', 'notes', cwd=self.main)
        done = self.assert_removed(child)
        self.assertEqual(self.git('branch', '--list', 'child', cwd=self.main), '')
        self.assertEqual(self.git('config', '--get-regexp', '^branch\\.child\\.', cwd=self.main, ok=(1,)), '')
        self.assertIn('deleted branch child', done.stdout)
        self.assertEqual((self.sentinel / 'keep').read_text(), 'shared target')
        self.assertNotIn(str(child), self.git('worktree', 'list', cwd=self.main))
        self.assertNotIn('retry: ', done.stdout)
        self.assertIn(f'removed {child}', self.log.read_text(encoding='utf-8'))

    def test_grandchildren_and_children_of_other_repositories_go_too_and_other_entries_stay(self):
        other = self.repo('other')
        kids = [self.child(), self.child('child--grand'), self.child('foreign', repo=other)]
        plain = self.wt / 'parent--plain'
        plain.mkdir()
        unrelated = self.wt / 'parentx'
        self.git('worktree', 'add', '-q', str(unrelated), '-b', 'parentx', cwd=self.main)
        (other / '.gitignore').write_text('local change\n')
        self.assert_removed(*kids)
        self.assertEqual((other / '.gitignore').read_text(), 'local change\n')
        self.assertTrue(plain.is_dir())
        self.assertTrue(unrelated.is_dir())

    def test_a_detached_head_on_a_kept_commit_goes(self):
        child = self.child()
        self.git('checkout', '-q', '--detach', 'master', cwd=child)
        self.assert_removed(child)

    def test_a_submodule_tag_the_primary_copy_holds_does_not_keep_the_child(self):
        child = self.child()
        self.unpublished_sub_tag(child, 'refs/tags/saved')
        self.assert_removed(child)

    def test_each_submodule_ref_is_preserved_on_its_own_route(self):
        child = self.child()
        sub = child / 'lib' / 'sub'
        self.git('submodule', 'update', '-q', '--init', cwd=self.main)
        primary = self.main / 'lib' / 'sub'
        oid = self.commit(sub, 'b.txt', 'local')
        self.git('tag', '-a', '-m', 'saved', 'saved', cwd=sub)
        self.git('fetch', '-q', '--no-tags', str(sub), 'refs/tags/saved:refs/tags/saved', cwd=primary)
        self.git('checkout', '-q', '-b', 'topic', 'HEAD~1', cwd=sub)
        self.commit(sub, 'c.txt', 'pushed after the primary cloned')
        self.git('push', '-q', 'origin', 'topic', cwd=sub)
        self.git('checkout', '-q', '--detach', 'origin/master', cwd=sub)
        self.assertNotIn(oid, self.git('rev-parse', 'topic', cwd=sub))
        self.assert_removed(child)

    def test_a_submodule_remote_branch_named_like_a_store_does_not_keep_the_child(self):
        child = self.child()
        self.git('push', '-q', 'origin', 'HEAD:refs/heads/objects/x', cwd=self.t / 'subwork')
        self.git('fetch', '-q', 'origin', cwd=child / 'lib' / 'sub')
        self.assert_removed(child)

    def test_stray_git_location_variables_do_not_redirect_the_checks(self):
        clean, dirty = self.child(), self.child('dirty')
        (dirty / 'notes.txt').write_text('wip')
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent), env={
            'GIT_DIR': str(self.main / '.git'), 'GIT_WORK_TREE': str(self.main),
            'GIT_INDEX_FILE': str(self.main / '.git' / 'index'), 'GIT_COMMON_DIR': str(self.main / '.git')})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertFalse(clean.exists(), done.stdout)
        self.assertIn(f'kept {dirty}: uncommitted or untracked changes', done.stdout)

    def test_an_unrelated_prunable_worktree_is_not_pruned(self):
        stale = self.wt / 'stale'
        self.git('worktree', 'add', '-q', str(stale), '-b', 'stale', cwd=self.main)
        subprocess.run(['rm', '-rf', str(stale)], check=True)
        self.assert_removed(self.child())
        self.assertIn('stale', self.git('worktree', 'list', '--porcelain', cwd=self.main))

    # --- kept -----------------------------------------------------------------------

    def test_an_untracked_file_keeps_the_child_even_when_status_hides_untracked_files(self):
        child = self.child()
        self.git('config', 'status.showUntrackedFiles', 'no', cwd=self.main)
        (child / 'notes.txt').write_text('wip')
        self.assert_kept(child, 'uncommitted or untracked changes')

    def test_a_change_inside_a_submodule_keeps_the_child(self):
        child = self.child()
        (child / 'lib' / 'sub' / 'a.txt').write_text('edited')
        self.assert_kept(child, 'uncommitted or untracked changes')

    def test_an_assume_unchanged_edit_keeps_the_child(self):
        child = self.child()
        self.git('update-index', '--assume-unchanged', 'README', cwd=child)
        (child / 'README').write_text('hidden edit')
        self.assert_kept(child, 'assume-unchanged or skip-worktree')

    def test_a_locked_child_is_kept(self):
        child = self.child()
        self.git('worktree', 'lock', str(child), cwd=self.main)
        self.assert_kept(child, 'locked')

    def test_a_detached_head_on_an_unkept_commit_keeps_the_child(self):
        child = self.child()
        self.git('checkout', '-q', '--detach', cwd=child)
        self.commit(child, 'lost', 'only here')
        self.assert_kept(child, 'detached HEAD')

    def detached_on_remote_branch(self, name, remote_branch):
        """A child detached on a commit only origin's remote_branch, and its remote-tracking ref, hold."""
        child = self.child(name, push=False)
        self.commit(child, f'{name}.txt', 'pushed elsewhere')
        self.git('push', '-q', 'origin', f'HEAD:refs/heads/{remote_branch}', cwd=child)
        self.git('checkout', '-q', '--detach', cwd=child)
        self.git('branch', '-q', '-D', name, cwd=self.main)
        return child

    def test_a_detached_head_held_only_by_a_stale_remote_tracking_ref_keeps_the_child(self):
        stale = self.detached_on_remote_branch('a', 'obsolete')
        self.git('update-ref', '-d', 'refs/heads/obsolete', cwd=self.t / 'github.com' / 'main.git')
        later = self.child('b')  # settled after every check; its fetch prunes origin/obsolete
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(stale.is_dir())
        self.assertIn('detached HEAD', done.stdout)
        self.assertFalse(later.exists())

    def test_a_detached_head_held_only_by_a_remote_branch_goes_once_a_fetch_confirms_it(self):
        child = self.detached_on_remote_branch('a', 'elsewhere')
        origin = self.git('remote', 'get-url', 'origin', cwd=self.main).strip()
        self.git('remote', 'set-url', 'origin', str(self.t / 'missing.git'), cwd=self.main)
        self.assert_kept(child, 'ls-remote')
        self.git('remote', 'set-url', 'origin', origin, cwd=self.main)
        done = self.run_script('cleanup', str(self.parent))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(child.exists())

    def test_an_operation_in_progress_keeps_the_child(self):
        child = self.child()
        gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip())
        (gitdir / 'MERGE_HEAD').write_text(self.git('rev-parse', 'HEAD', cwd=child))
        self.assert_kept(child, 'MERGE_HEAD in progress')

    def test_a_private_ref_on_a_unique_commit_keeps_the_child(self):
        child = self.child()
        oid = self.commit(child, 'saved', 'saved work')
        self.git('update-ref', 'refs/worktree/saved', oid, cwd=child)
        self.git('reset', '-q', '--hard', 'HEAD~1', cwd=child)
        self.assert_kept(child, 'private ref refs/worktree/saved')

    def reviewr_refs(self, child, base_pick=None):
        """herdr-reviewr's pane state: turn-base on a snapshot tree, base-pick on a blob, or on the
        object id base_pick, written by hand since update-ref refuses a missing object."""
        self.git('update-ref', 'refs/worktree/reviewr/turn-base', self.git('write-tree', cwd=child).strip(), cwd=child)
        if base_pick:
            gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip())
            (gitdir / 'refs' / 'worktree' / 'reviewr' / 'base-pick').write_text(f'{base_pick}\n')
            return
        (self.t / 'pick').write_text('master')
        blob = self.git('hash-object', '-w', str(self.t / 'pick'), cwd=child).strip()
        self.git('update-ref', 'refs/worktree/reviewr/base-pick', blob, cwd=child)

    def test_reviewr_pane_state_does_not_keep_the_child(self):
        child = self.child()
        self.reviewr_refs(child)
        self.assert_removed(child)

    def test_a_reviewr_ref_on_a_missing_object_does_not_keep_the_child(self):
        child = self.child()
        self.reviewr_refs(child, base_pick='1' * 40)
        self.assert_removed(child)

    def test_another_ref_under_reviewr_keeps_the_child(self):
        child = self.child()
        self.git('update-ref', 'refs/worktree/reviewr/other', self.git('write-tree', cwd=child).strip(), cwd=child)
        self.assert_kept(child, 'private ref refs/worktree/reviewr/other')

    def test_reviewr_pane_state_beside_unique_work_keeps_the_child(self):
        child = self.child()
        self.reviewr_refs(child)
        oid = self.commit(child, 'saved', 'saved work')
        self.git('update-ref', 'refs/worktree/saved', oid, cwd=child)
        self.git('reset', '-q', '--hard', 'HEAD~1', cwd=child)
        self.assert_kept(child, 'private ref refs/worktree/saved')

    def test_a_nested_repository_in_an_ignored_directory_keeps_the_child(self):
        child = self.child()
        self.git('init', '-q', str(child / 'build' / 'inner'), cwd=child)
        self.assert_kept(child, 'nested repository or worktree')

    def test_a_repository_initialized_inside_a_tracked_directory_keeps_the_child(self):
        child = self.child()
        (child / 'tracked').mkdir()
        self.commit(child, 'tracked/file', 'tracked')
        self.git('init', '-q', str(child / 'tracked'), cwd=child)
        self.git('add', 'file', cwd=child / 'tracked')
        self.git('commit', '-qm', 'history only here', cwd=child / 'tracked')
        self.assertEqual(self.git('status', '--porcelain', cwd=child), '', 'invisible to the outer status')
        self.assert_kept(child, f'nested repository or worktree at {child / "tracked"}')

    def test_a_submodule_store_elsewhere_in_the_git_dir_is_checked(self):
        child = self.child()
        self.relocated_sub_store(child)
        self.assert_kept(child, 'a stash in submodule repository')

    def test_a_deinitialized_submodule_store_elsewhere_in_the_git_dir_is_checked(self):
        child = self.child()
        store = self.relocated_sub_store(child)
        self.git('submodule', 'deinit', '-q', 'lib/sub', cwd=child)
        self.assertEqual(list((child / 'lib' / 'sub').iterdir()), [])
        self.assert_kept(child, f'a stash in submodule repository {store}')

    def test_a_nested_repository_under_a_submodules_ignored_path_keeps_the_child(self):
        child = self.child()
        sub = child / 'lib' / 'sub'
        store = Path(self.git('rev-parse', '--absolute-git-dir', cwd=sub).strip())
        (store / 'info').mkdir(exist_ok=True)
        (store / 'info' / 'exclude').write_text('out/\n')
        self.git('init', '-q', str(sub / 'out' / 'inner'), cwd=sub)
        self.assert_kept(child, f'nested repository or worktree at {sub / "out" / "inner"}')

    def test_a_submodule_stash_keeps_the_child(self):
        child = self.child()
        sub = child / 'lib' / 'sub'
        (sub / 'a.txt').write_text('stashed')
        self.git('stash', '-q', cwd=sub)
        self.assert_kept(child, 'a stash in submodule repository')

    def test_a_submodule_tag_on_an_unpublished_commit_keeps_the_child(self):
        child = self.child()
        self.unpublished_sub_tag(child)
        self.assert_kept(child, 'is on no remote')

    def test_an_annotated_submodule_tag_on_a_published_commit_keeps_the_child_unless_the_primary_has_it(self):
        child = self.child()
        self.git('tag', '-a', '-m', 'release notes', 'v1', cwd=child / 'lib' / 'sub')
        self.assert_kept(child, 'refs/tags/v1 in submodule repository')

    def test_a_submodule_tag_whose_commit_the_primary_holds_under_another_name_keeps_the_child(self):
        child = self.child()
        self.unpublished_sub_tag(child, 'refs/heads/other')
        self.assert_kept(child, 'refs/tags/saved in submodule repository')

    def test_a_primary_copy_borrowing_objects_through_alternates_is_no_fallback(self):
        child = self.child()
        sub = self.unpublished_sub_tag(child, 'refs/tags/saved')
        primary = self.main / 'lib' / 'sub'
        store = Path(self.git('rev-parse', '--absolute-git-dir', cwd=primary).strip())
        (store / 'objects' / 'info' / 'alternates').write_text(str(Path(self.git(
            'rev-parse', '--absolute-git-dir', cwd=sub).strip()) / 'objects') + '\n')
        self.assert_kept(child, 'is on no remote')

    def test_a_primary_copy_linked_into_the_doomed_store_is_no_fallback(self):
        child = self.child()
        sub = self.unpublished_sub_tag(child)
        copy = self.main / '.git' / 'modules' / 'lib' / 'sub'
        copy.rename(copy.with_name('sub.orig'))
        copy.symlink_to(self.git('rev-parse', '--absolute-git-dir', cwd=sub).strip())
        self.assert_kept(child, 'refs/tags/saved in submodule repository')

    def test_a_primary_copy_that_is_not_self_contained_is_no_fallback(self):
        for part in ('objects', 'refs', 'objects/pack', 'fanout', 'outside', 'commondir'):
            with self.subTest(part):
                self.tearDown()
                self.setUp()
                child = self.child()
                sub = self.unpublished_sub_tag(child, 'refs/tags/saved')
                copy = self.main / '.git' / 'modules' / 'lib' / 'sub'
                doomed = Path(self.git('rev-parse', '--absolute-git-dir', cwd=sub).strip())
                if part == 'commondir':
                    (copy / 'commondir').write_text(f'{doomed}\n')
                else:
                    name = {'fanout': next(f'objects/{d.name}' for d in (copy / 'objects').iterdir()
                                           if len(d.name) == 2), 'outside': 'objects/pack'}.get(part, part)
                    # moved, not copied: every object stays readable through the link
                    target = (self.t if part == 'outside' else doomed) / f'moved-{name.replace("/", "-")}'
                    (copy / name).rename(target)
                    (copy / name).symlink_to(target)
                self.assert_kept(child, 'refs/tags/saved in submodule repository')

    def test_a_looping_symlink_keeps_the_child(self):
        child = self.child()
        (child / 'build' / 'loop').symlink_to(child / 'build' / 'loop')
        self.assert_kept(child, f'cannot classify {child / "build" / "loop"}')

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root lists any directory')
    def test_a_directory_the_checks_cannot_list_keeps_the_child(self):
        for case in ('child', 'git dir'):
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                child = self.child()
                if case == 'child':
                    locked = child / 'build' / 'locked'
                    self.git('init', '-q', str(locked / 'inner'), cwd=child)
                else:
                    sub = child / 'lib' / 'sub'
                    (sub / 'a.txt').write_text('stashed')
                    self.git('stash', '-q', cwd=sub)
                    self.git('submodule', 'deinit', '-q', 'lib/sub', cwd=child)
                    locked = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip()) / 'modules' / 'lib'
                locked.chmod(0o300)
                try:
                    self.assert_kept(child, f'cannot list {locked}')
                finally:
                    locked.chmod(0o700)

    def test_an_entry_the_checks_cannot_classify_keeps_the_child(self):
        child = self.child()
        self.git('init', '-q', str(child / 'build' / 'inner' / 'nested'), cwd=child)
        self.drop_parent()
        done = subprocess.run([sys.executable, '-c', UNCLASSIFIABLE, str(SCRIPT), 'cleanup', str(self.parent)],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(child.is_dir())
        self.assertIn(f'kept {child}: cannot classify {child / "build" / "inner"}', done.stdout)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root stats any path')
    def test_a_store_whose_objects_link_where_the_checks_cannot_stat_keeps_the_child(self):
        child = self.child()
        store = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip()) / 'modules' / 'extra'
        self.hand_made_store(store)
        locked = self.t / 'locked'
        locked.mkdir()
        (store / 'objects').rename(locked / 'objects')
        (store / 'objects').symlink_to(locked / 'objects')
        locked.chmod(0)
        try:
            self.assert_kept(child, f'cannot classify {store / "objects"}')
        finally:
            locked.chmod(0o700)

    def test_a_repository_placed_in_a_stores_refs_or_logs_keeps_the_child(self):
        for part in ('refs', 'logs'):
            with self.subTest(part):
                self.tearDown()
                self.setUp()
                child = self.child()
                store = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child / 'lib' / 'sub').strip())
                self.hand_made_store(store / part / 'hidden')
                self.assert_kept(child, f'in submodule repository {store / part / "hidden"} is on no remote')

    def test_an_unreadable_head_keeps_the_child(self):
        for case in ('child', 'submodule'):
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                child = self.child()
                repo = child if case == 'child' else child / 'lib' / 'sub'
                self.git('checkout', '-q', '--detach', cwd=repo)
                self.commit(repo, 'lost', 'only here')
                if case == 'submodule':
                    self.git('submodule', 'deinit', '-q', '-f', 'lib/sub', cwd=child)
                self.drop_parent()
                done = self.run_script('cleanup', str(self.parent),
                                       env=self.git_wrapper('*" rev-parse -q --verify HEAD "*) exit 128'))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertTrue(child.is_dir())
                self.assertIn(f'kept {child}: HEAD unreadable', done.stdout)

    def test_a_submodule_worktree_outside_the_child_keeps_it(self):
        child = self.child()
        self.git('worktree', 'add', '-q', '--detach', str(self.t / 'outside'), cwd=child / 'lib' / 'sub')
        self.assert_kept(child, 'has a worktree at')

    def test_a_bare_repository_in_an_ignored_directory_keeps_the_child(self):
        child = self.child()
        self.git('init', '-q', '--bare', str(child / 'build' / 'store.git'), cwd=child)
        self.assert_kept(child, f'nested repository or worktree at {child / "build" / "store.git"}')

    def test_a_deleted_submodule_worktree_inside_the_child_on_a_unique_commit_keeps_it(self):
        child = self.child()
        subwt = child / 'build' / 'subwt'
        self.git('worktree', 'add', '-q', '--detach', str(subwt), cwd=child / 'lib' / 'sub')
        self.commit(subwt, 'c.txt', 'only in this worktree')
        shutil.rmtree(subwt)
        self.assert_kept(child, 'has a worktree at')

    def test_a_file_written_after_the_checks_keeps_a_child_without_submodules(self):
        other = self.repo('other')
        child = self.child('foreign', repo=other)
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent),
                               env=self.git_wrapper(f'*" worktree remove "*) echo late > "{child}/late.txt"'))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual((child / 'late.txt').read_text(), 'late\n')
        self.assertIn(f'kept {child}: `git worktree remove` failed', done.stdout)

    # --- branches ----------------------------------------------------------------------

    def oid(self, ref, repo=None):
        return self.git('rev-parse', ref, cwd=repo or self.main).strip()

    def branch_exists(self, name, repo=None):
        return bool(self.git('branch', '--list', name, cwd=repo or self.main).strip())

    def pr_search(self, *prs):
        """Makes the gh stub answer the PR search with prs, (number, head branch, head oid) each."""
        page = {'issueCount': len(prs), 'pageInfo': {'hasNextPage': False, 'endCursor': None},
                'nodes': [{'number': n, 'state': 'MERGED', 'headRefName': b, 'headRefOid': o, 'mergedAt': 'x'}
                          for n, b, o in prs]}
        (self.t / 'prs').write_text(json.dumps(page))
        return {'STUB_PRS': str(self.t / 'prs')}

    def squashed(self, name):
        """A removable child whose branch has a commit origin holds only as PR 7's head."""
        child = self.child(name, push=False)
        tip = self.commit(child, 'feature.txt', 'feature')
        self.git('push', '-q', 'origin', f'{tip}:refs/pull/7/head', cwd=child)
        return child, tip

    def test_a_never_pushed_branch_stays_and_a_retry_deletes_it_once_pushed(self):
        child = self.child(push=False)
        tip = self.commit(child, 'local.txt', 'local')
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertFalse(child.exists())
        self.assertTrue(self.branch_exists('child'))
        self.assertIn(f'kept branch child in {self.main / ".git"}: commits on no origin branch', done.stdout)
        log = self.log.read_text(encoding='utf-8')
        retry = ['cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'child', tip]
        self.assertIn(f'its branch is retried by: {self.retry((self.main / ".git", "child", tip))}', log)
        self.assertLess(log.index('its branch is retried by'), log.index(f'removed {child}'))
        self.git('push', '-q', 'origin', 'child', cwd=self.main)
        done = self.run_script(*retry)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.branch_exists('child'))

    def test_a_never_pushed_branch_of_a_remote_off_github_stays_without_asking_gh(self):
        other = self.repo('other')
        child = self.child('foreign', repo=other, push=False)
        self.commit(child, 'local.txt', 'local')
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('kept branch foreign', done.stdout)
        self.assertIn('commits on no origin branch and no PR head', done.stdout)
        self.assertEqual(self.gh_calls(), [])

    def test_a_pr_head_branch_goes_and_a_wrong_head_or_failed_lookup_keeps_it(self):
        child, tip = self.squashed('feat')
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('kept branch feat', done.stdout)
        self.assertIn('network unreachable', done.stdout)
        retry = ['cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'feat', tip]
        done = self.run_script(*retry, env=self.pr_search((7, 'feat', self.oid('master'))))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(self.branch_exists('feat'))
        done = self.run_script(*retry, env=self.pr_search((7, 'feat-other', tip)))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        done = self.run_script(*retry, env=self.pr_search((8, 'feat', tip)))  # PR 8's head ref is not on origin
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        done = self.run_script(*retry, env=self.pr_search((8, 'feat', tip), (7, 'feat', tip)))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('deleted branch feat', done.stdout)
        self.assertIn('head of PR #7', done.stdout)
        self.assertFalse(self.branch_exists('feat'))

    def test_a_detached_sibling_held_only_by_the_parents_pr_head_branch_goes_before_the_branch(self):
        tip = self.commit(self.parent, 'feature.txt', 'feature')
        self.git('push', '-q', 'origin', f'{tip}:refs/pull/7/head', cwd=self.parent)
        child = self.child('side')
        self.git('checkout', '-q', '--detach', tip, cwd=child)
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'parent', tip,
                               env=self.pr_search((7, 'parent', tip)))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(child.exists())
        self.assertIn('head of PR #7', done.stdout)
        self.assertFalse(self.branch_exists('parent'))

    def test_a_truncated_pr_search_keeps_the_branch(self):
        child, tip = self.squashed('feat')
        env = self.pr_search((7, 'feat', tip))
        page = json.loads((self.t / 'prs').read_text())
        page['issueCount'] = 2
        (self.t / 'prs').write_text(json.dumps(page))
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent), env=env)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('returned 1 of 2', done.stdout)
        self.assertTrue(self.branch_exists('feat'))

    def test_a_branch_checked_out_moved_symbolic_default_or_on_a_local_remote_stays(self):
        self.git('worktree', 'add', '-q', str(self.t / 'elsewhere'), '-b', 'busy', cwd=self.main)
        self.git('push', '-q', 'origin', 'busy', cwd=self.main)
        self.git('branch', 'moved', cwd=self.main)
        self.git('push', '-q', 'origin', 'moved', cwd=self.main)
        old = self.oid('moved')
        self.commit(self.t / 'elsewhere', 'x.txt', 'x')
        self.git('branch', '-f', 'moved', 'busy', cwd=self.main)
        self.git('symbolic-ref', 'refs/heads/alias', 'refs/heads/moved', cwd=self.main)
        self.git('branch', 'local', cwd=self.main)
        self.git('config', 'branch.local.remote', '.', cwd=self.main)
        self.git('branch', 'dev', 'master', cwd=self.main)
        self.git('push', '-q', 'origin', 'dev', cwd=self.main)
        self.git('symbolic-ref', 'HEAD', 'refs/heads/dev', cwd=self.t / 'github.com' / 'main.git')
        self.drop_parent()
        common = str(self.main / '.git')
        cases = {'busy': (self.oid('busy'), 'checked out at'), 'moved': (old, 'is at'),
                 'alias': (self.oid('alias'), 'symbolic ref'), 'local': (self.oid('local'), '. is not a configured'),
                 'dev': (self.oid('dev'), 'default branch of origin')}
        for name, (tip, why) in cases.items():
            done = self.run_script('cleanup', str(self.parent), '--branch', common, name, tip)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn(why, done.stdout, name)
            self.assertTrue(self.branch_exists(name), name)

    def test_the_fetch_leaves_local_tags_alone_whatever_the_prune_config(self):
        child = self.child()
        self.git('tag', 'local-only', cwd=self.main)
        self.git('push', '-q', 'origin', 'master:refs/heads/gone', cwd=self.main)
        self.git('fetch', '-q', 'origin', cwd=self.main)
        self.git('push', '-q', 'origin', ':refs/heads/gone', cwd=self.main)
        self.git('config', 'fetch.pruneTags', 'true', cwd=self.main)
        self.git('config', 'remote.origin.pruneTags', 'true', cwd=self.main)
        self.git('config', 'remote.origin.tagOpt', '--tags', cwd=self.main)
        self.assert_removed(child)
        self.assertEqual(self.git('tag', '--list', 'local-only', cwd=self.main).strip(), 'local-only')
        self.assertEqual(self.git('branch', '-r', '--list', 'origin/gone', cwd=self.main), '', 'pruned branch')

    def test_the_fetch_updates_no_ref_a_configured_mapping_names(self):
        child = self.child()
        self.git('branch', 'notes', 'master', cwd=self.main)
        self.git('push', '-q', 'origin', 'notes', cwd=self.main)
        self.git('tag', 'v1', 'master', cwd=self.main)
        self.git('push', '-q', 'origin', 'v1', cwd=self.main)
        local = self.commit(self.main, 'mine.txt', 'mine')
        self.git('reset', '-q', '--hard', 'HEAD~1', cwd=self.main)
        self.git('update-ref', 'refs/heads/notes', local, cwd=self.main)
        self.git('tag', '-f', 'v1', local, cwd=self.main)
        for spec in ('+refs/heads/notes:refs/heads/notes', '+refs/tags/*:refs/tags/*'):
            self.git('config', '--add', 'remote.origin.fetch', spec, cwd=self.main)
        self.assert_removed(child)
        self.assertEqual(self.oid('notes'), local)
        self.assertEqual(self.oid('v1'), local)

    def test_a_malformed_pr_search_keeps_the_branch_and_the_sweep_goes_on(self):
        kids = [self.child('a', push=False), self.child('b', push=False)]
        for kid in kids:
            self.commit(kid, f'{kid.name}.txt', 'local')
        (self.t / 'prs').write_text(json.dumps({'nodes': None, 'issueCount': 0}))
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent), env={'STUB_PRS': str(self.t / 'prs')})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for kid in kids:
            self.assertFalse(kid.exists())
        self.assertEqual(done.stdout.count('malformed answer'), 2)
        [retry] = [shlex.split(l.removeprefix('retry: '))[3:] for l in done.stdout.splitlines() if l.startswith('retry: ')]
        node = {'headRefName': 'a', 'headRefOid': self.oid('a'), 'state': 'OPEN', 'mergedAt': None, 'number': 1}
        for missing in ('number', 'headRefOid'):
            answer = {'nodes': [{k: v for k, v in node.items() if k != missing}], 'issueCount': 1}
            (self.t / 'prs').write_text(json.dumps(answer))
            done = self.run_script('cleanup', *retry, env={'STUB_PRS': str(self.t / 'prs')})
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn('kept branch a in', done.stdout)
            self.assertIn('malformed answer', done.stdout)
            self.assertIn('kept branch b in', done.stdout)
            self.assertIn(f"retry: {shlex.join([sys.executable, str(SCRIPT), 'cleanup', *retry])}\n", done.stdout)
        self.assertTrue(self.branch_exists('a') and self.branch_exists('b'))

    def test_the_parents_branch_is_settled_from_its_branch_arguments_and_a_repeat_finds_nothing_to_do(self):
        self.git('push', '-q', 'origin', 'parent', cwd=self.parent)
        tip = self.oid('parent')
        child = self.child()
        self.drop_parent()
        retry = ['cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'parent', tip]
        done = self.run_script(*retry)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.branch_exists('parent'))
        self.assertFalse(child.exists())
        self.assertFalse(self.branch_exists('child'))
        done = self.run_script('cleanup', str(self.parent))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn('retry: ', done.stdout)

    # --- agents ----------------------------------------------------------------------

    def test_a_running_chief_keeps_the_child(self):
        child = self.child()
        chief = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--agent', 'chief'], cwd=child)
        self.addCleanup(chief.wait)
        self.addCleanup(chief.kill)
        self.assert_kept(child, f'a chief session (headless or interactive) runs in it: pid {chief.pid}')

    def test_a_cowork_lane_with_a_request_keeps_the_child(self):
        child = self.child()
        box = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip()) / 'cowork' / 'codex' / 'review'
        box.mkdir(parents=True)
        (box / 'codex-review-1.lock').write_text('')
        self.assert_kept(child, 'cowork lane codex/review holds a running or undelivered request')

    def test_a_herdr_core_terminal_working_in_the_child_keeps_it(self):
        child, other = self.child(), self.child('other')
        hc_inventory(self.hc, [
            {'address': 't7', 'name': 'rev', 'cwd': str(self.t), 'where': 'parent / 1'},  # its foreground inside
            {'address': 't8', 'cwd': str(child)},  # a terminal in no herdr-app space
            {'address': 't9', 'cwd': str(child), 'state': 'finished'},
            {'address': 'ci/t3', 'cwd': str(child)},  # another machine's
            {'address': 't10', 'cwd': str(self.t)},  # ended between the listing and the look
            {'address': 't11', 'cwd': str(self.t)}],  # moved in since the listing
            {'t7': child / 'build'}, raw={'get t10': [3, '', '{"error": {"kind": "not_found"}}'],
                                           'get t11': [0, json.dumps({'cwd': str(child), 'foreground': None}), '']})
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {child}: herdr-core terminals work there: t7 rev (parent / 1) in {child / "build"}; '
                      f't8 (in no herdr-app space) in {child}; t11 (in no herdr-app space) in {child}; close them, then retry\n',
                      done.stdout)
        self.assertTrue(child.is_dir())
        self.assertFalse(other.exists())

    def test_an_hc_that_cannot_list_its_terminals_keeps_the_child(self):
        child = self.child()
        listed = [{'address': 't7', 'cwd': str(self.t)}]
        cases = {'hc fails': [{'ls': [1, '', 'no core']}], 'not JSON': [{'ls': [0, 'oops', '']}],
                 'no terminal list': [{'ls': [0, '{}', '']}],
                 'an entry without cwd': [{'ls': [0, '{"terminals": [{"address": "t7", "state": "live"}]}', '']}],
                 'an entry without state': [{'ls': [0, '{"terminals": [{"address": "t7", "cwd": "/"}]}', '']}],
                 'an unknown state': [{}, [{'address': 't7', 'cwd': str(self.t), 'state': 'parked'}]],
                 'an empty cwd': [{}, [{'address': 't7', 'cwd': ''}]],
                 'a relative cwd': [{}, [{'address': 't7', 'cwd': 'parent--child'}]],
                 'get with an empty cwd': [{'get t7': [0, '{"cwd": "", "foreground": null}', '']}, listed],
                 'a relative foreground cwd': [
                     {'get t7': [0, json.dumps({'cwd': str(self.t), 'foreground': {'cwd': '.'}}), '']}, listed],
                 'get fails': [{'get t7': [1, '', 'lost']}, listed],
                 'exit 3 but not not_found': [{'get t7': [3, '', '{"error": {"kind": "needs_target"}}']}, listed],
                 'get without foreground': [{'get t7': [0, json.dumps({'cwd': str(self.t)}), '']}, listed],
                 'get without cwd': [{'get t7': [0, '{"foreground": null}', '']}, listed],
                 'a foreground without cwd': [{'get t7': [0, json.dumps({'cwd': str(self.t), 'foreground': {}}), '']},
                                              listed]}
        self.drop_parent()
        for case, (raw, *terminals) in cases.items():
            with self.subTest(case):
                hc_inventory(self.hc, terminals[0] if terminals else [], raw=raw)
                done = self.run_script('cleanup', str(self.parent))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn(f'kept {child}: cannot list herdr-core terminals', done.stdout)
                self.assertTrue(child.is_dir())

    def test_a_terminal_whose_listing_is_inside_keeps_the_child_without_a_look(self):
        child = self.child()
        hc_inventory(self.hc, [{'address': 't7', 'cwd': str(child)}], raw={'get t7': [1, '', 'never asked']})
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {child}: herdr-core terminals work there: t7 (in no herdr-app space) in {child}',
                      done.stdout)

    def test_a_terminal_arriving_after_the_childs_checks_keeps_it(self):
        child = self.child()
        moved_in = json.dumps({'terminals': [{'address': 't7', 'cwd': str(child), 'state': 'live'}]})
        hc_inventory(self.hc, [], raw={'ls #2': [0, moved_in, ''],
                                       'get t7': [0, json.dumps({'cwd': str(child), 'foreground': None}), '']})
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {child}: herdr-core terminals work there: t7 (in no herdr-app space) in {child}',
                      done.stdout)
        self.assertTrue(child.is_dir())

    def test_a_parent_recreated_during_a_childs_last_terminal_check_keeps_it(self):
        child = self.child()
        # the second `hc ls` is the one just before the removal
        recreate = (f'if sys.argv[1:2] == ["ls"] and os.path.exists(os.environ["STUB_HC"] + ".calls"):\n'
                    f'    os.makedirs({str(self.parent)!r}, exist_ok=True)\n')
        make_stub(self.hc_stub.parent, 'hc', HC.replace('\ntry:', f'\n{recreate}try:', 1))
        hc_inventory(self.hc, [])
        done = self.cleanup()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn(f'{self.parent} exists again: not touching {child}', done.stdout)
        self.assertTrue(child.is_dir())

    @unittest.skipUnless(Path('/proc/self/cwd').exists(), 'processes listed by /proc')
    def test_a_process_working_in_the_child_keeps_it(self):
        child, other = self.child(), self.child('other')
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=child)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {child}: a process works in it: ', done.stdout)
        self.assertIn(f' (pid {holder.pid})\n', done.stdout)
        self.assertTrue(child.is_dir())
        self.assertFalse(other.exists())

    def test_no_hc_installed_skips_the_terminal_check(self):
        child = self.child()
        self.env['PATH'] = without_hc(self.env['PATH'], self.hc_stub)
        hc_inventory(self.hc, [{'address': 't7', 'cwd': str(child)}])  # no hc answers it
        self.assert_removed(child)
        self.assertIn('no hc installed: herdr-core terminals in the children not checked',
                      self.log.read_text(encoding='utf-8'))

    def test_a_staying_childs_own_children_and_link_targets_stay(self):
        target = self.child('dep-x')  # sorted before the child linking to it
        chained = self.child('dep-y')
        owner = self.child('z')
        (owner / 'notes.txt').write_text('wip')
        (owner / 'build' / 'dep').symlink_to(Path('..') / '..' / target.name)
        owned = self.child('z--dep')
        (owned / 'build' / 'next').symlink_to(chained / 'lib')
        other = self.child('other')
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for kid in (target, chained, owner, owned):
            self.assertTrue(kid.is_dir(), kid)
        self.assertFalse(other.exists())
        self.assertIn(f'kept {owned}: owned by parent--z, which stays', done.stdout)
        self.assertIn(f'kept {target}: parent--z, which stays, links into it', done.stdout)
        self.assertIn(f'kept {chained}: parent--z--dep, which stays, links into it', done.stdout)
        self.assertEqual(self.logged_retries(), [shlex.split(self.retry())])

    def test_every_child_a_staying_childs_link_passes_through_stays(self):
        bridge, target = self.child('bridge'), self.child('dep')
        owner = self.child('z')
        (owner / 'notes.txt').write_text('wip')
        (owner / 'build' / 'use').symlink_to(Path('..') / '..' / bridge.name / 'build' / 'forward')
        (bridge / 'build' / 'forward').symlink_to(Path('..') / '..' / target.name)
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for kid in (bridge, target):
            self.assertTrue(kid.is_dir(), kid)
            self.assertIn(f'kept {kid}: parent--z, which stays, links into it', done.stdout)
            self.assertTrue(self.branch_exists(kid.name.removeprefix('parent--')))

    def test_a_staying_childs_link_through_a_sibling_and_back_out_holds_both(self):
        passed, target = self.child('a'), self.child('b')
        owner = self.child('z')
        (owner / 'notes.txt').write_text('wip')
        (owner / 'build' / 'use').symlink_to(f'../../{passed.name}/../{target.name}')
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for kid in (passed, target):
            self.assertTrue(kid.is_dir(), kid)
            self.assertIn(f'kept {kid}: parent--z, which stays, links into it', done.stdout)

    def test_children_linking_in_a_cycle_stay(self):
        a, b, plain = self.child('a'), self.child('b'), self.child('w')
        (a / 'build' / 'peer').symlink_to(Path('..') / '..' / b.name)
        (b / 'build' / 'peer').symlink_to(Path('..') / '..' / a.name)
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {a}: links with parent--b in a cycle', done.stdout)
        self.assertIn(f'kept {b}: links with parent--a in a cycle', done.stdout)
        self.assertTrue(a.is_dir() and b.is_dir() and self.branch_exists('a') and self.branch_exists('b'))
        self.assertFalse(plain.exists())

    def test_a_child_whose_removal_fails_keeps_what_it_holds(self):
        other = self.repo('other')  # no submodules: a plain removal, which re-checks cleanliness
        target, owner = self.child('dep-x', repo=other), self.child('z', repo=other)
        (owner / 'build' / 'dep').symlink_to(Path('..') / '..' / target.name)
        owned, plain = self.child('z--dep', repo=other), self.child('w', repo=other)
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent), env=self.git_wrapper(
            f'*" worktree remove "*"{owner} "*) echo late > "{owner}/late.txt"'))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {owner}: `git worktree remove` failed', done.stdout)
        self.assertIn(f'kept {owned}: owned by parent--z, which stays', done.stdout)
        self.assertIn(f'kept {target}: parent--z, which stays, links into it', done.stdout)
        for kid in (target, owner, owned):
            self.assertTrue(kid.is_dir(), kid)
            self.assertTrue(self.branch_exists(kid.name.removeprefix('parent--'), other))
        self.assertFalse(plain.exists())

    def test_a_childs_store_inside_another_child_keeps_the_holder(self):
        holder, user = self.child('a'), self.child('b')
        sub = user / 'lib' / 'sub'
        old = Path(self.git('rev-parse', '--absolute-git-dir', cwd=sub).strip())
        store = holder / 'build' / 'store'
        old.rename(store)
        (sub / '.git').write_text(f'gitdir: {store}\n')
        self.git('config', '--file', str(store / 'config'), 'core.worktree', str(sub), cwd=user)
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {holder}: nested repository or worktree at {store}', done.stdout)
        self.assertTrue((store / 'HEAD').is_file())

    # --- refusals and input ----------------------------------------------------------

    def test_nothing_runs_while_the_parent_path_exists(self):
        child = self.child()
        tip = self.git('rev-parse', 'parent', cwd=self.main).strip()
        done = self.run_script('cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'parent', tip)
        self.assertEqual(done.returncode, 2)
        self.assertTrue(child.is_dir())
        self.assertTrue(self.branch_exists('parent'))
        self.assertIn(f'kept branch parent in {self.main / ".git"}: {self.parent} exists', done.stdout)
        self.assertIn(f'retry: {self.retry((self.main / ".git", "parent", tip))}', done.stdout)

    def test_a_dangling_symlink_at_the_parent_path_stops_the_sweep(self):
        child = self.child()
        self.drop_parent()
        self.parent.symlink_to(self.t / 'gone')
        done = self.run_script('cleanup', str(self.parent))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertTrue(child.is_dir())
        self.assertIn(f'{self.parent} exists: not touching its children', done.stdout)
        self.assertIn(f'retry: {self.retry()}\n', done.stdout)

    def test_a_parent_recreated_during_the_checks_stops_the_sweep(self):
        for mode in ('recreate', 'dangle'):  # a dangling symlink occupies the path too
            with self.subTest(mode):
                self.tearDown()
                self.setUp()
                kids = [self.child(), self.child('second')]
                self.git('push', '-q', 'origin', 'parent', cwd=self.parent)
                tip = self.git('rev-parse', 'parent', cwd=self.main).strip()
                self.drop_parent()
                checked = self.t / 'checked'
                done = self.run_script('cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'parent', tip,
                                       env=self.git_wrapper(f'*" --show-toplevel "*) echo "$*" >> "{checked}"; '
                                                            f'{self.replace_parent(mode)}'))
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertTrue(os.path.lexists(self.parent))
                for kid in kids:
                    self.assertTrue(kid.is_dir(), done.stdout)
                    self.assertNotIn(f'removed {kid}', done.stdout)
                self.assertTrue(self.branch_exists('parent'), done.stdout)
                self.assertIn(f'kept branch parent in {self.main / ".git"}: {self.parent} exists again', done.stdout)
                self.assertIn(f'retry: {self.retry((self.main / ".git", "parent", tip))}\n', done.stdout)
                [check] = checked.read_text().splitlines()
                self.assertIn(f'-C {kids[0]} ', check, 'the second child is not checked once the parent is back')

    def test_a_parent_seen_once_during_the_checks_stops_the_sweep_though_it_is_gone_again(self):
        kids = [self.child(), self.child('second')]
        self.git('push', '-q', 'origin', 'parent', cwd=self.parent)
        tip = self.git('rev-parse', 'parent', cwd=self.main).strip()
        self.drop_parent()
        done = subprocess.run([sys.executable, '-c', BLINK, str(SCRIPT), 'cleanup', str(self.parent),
                               '--branch', str(self.main / '.git'), 'parent', tip],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        for kid in kids:
            self.assertTrue(kid.is_dir(), done.stdout)
        self.assertTrue(self.branch_exists('parent') and self.branch_exists('child'), done.stdout)
        self.assertIn(f'{self.parent} exists again: not touching its children', done.stdout)
        self.assertIn(f'retry: {self.retry((self.main / ".git", "parent", tip))}\n', done.stdout)
        done = subprocess.run([sys.executable, '-c', BLINK, str(SCRIPT), 'cleanup', str(self.parent)],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        for kid in kids:
            self.assertTrue(kid.is_dir(), done.stdout)
        self.assertIn(f'retry: {self.retry()}\n', done.stdout)

    def test_a_parent_recreated_during_a_failing_check_keeps_the_queued_branches(self):
        for mode in ('recreate', 'dangle'):
            with self.subTest(mode):
                self.tearDown()
                self.setUp()
                kid = self.child()
                (kid / 'dirty.txt').write_text('uncommitted')
                self.git('push', '-q', 'origin', 'parent', cwd=self.parent)
                tip = self.git('rev-parse', 'parent', cwd=self.main).strip()
                self.drop_parent()
                done = self.run_script('cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'parent', tip,
                                       env=self.git_wrapper(f'*" status --porcelain "*) {self.replace_parent(mode)}'))
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(f'kept {kid}: uncommitted or untracked changes', done.stdout)
                self.assertTrue(kid.is_dir(), done.stdout)
                self.assertTrue(self.branch_exists('parent'), done.stdout)
                self.assertIn(f'kept branch parent in {self.main / ".git"}: {self.parent} exists again', done.stdout)
                self.assertIn(f'retry: {self.retry((self.main / ".git", "parent", tip))}\n', done.stdout)

    def test_an_unknown_subcommand_or_malformed_arguments_print_usage_and_change_nothing(self):
        child = self.child()
        self.drop_parent()
        for argv in ([], ['event'], ['frobnicate', str(self.parent)], ['cleanup'],
                     ['cleanup', str(self.parent), '--branch', 'a', 'b'], ['cleanup', str(self.parent), 'x']):
            done = self.run_script(*argv)
            self.assertEqual(done.returncode, 2, argv)
            self.assertIn('cleanup <parent> [--branch', done.stderr, argv)
            self.assertTrue(child.is_dir(), argv)
        self.assertFalse(self.log.exists())

    def test_a_retry_from_inside_a_child_or_with_the_script_or_log_inside_one_is_refused(self):
        clean, dirty = self.child(), self.child('dirty')
        (dirty / 'notes.txt').write_text('wip')
        self.drop_parent()
        copy = dirty / 'build' / 'skills' / 'worktree-remove' / 'scripts' / 'worktree_children.py'
        copy.parent.mkdir(parents=True)
        shutil.copy(SCRIPT, copy)
        (dirty / 'build' / 'lib').mkdir()
        for name in ('windows.py', 'hc.py'):
            shutil.copy(ROOT / 'lib' / name, dirty / 'build' / 'lib')
        state = dirty / 'build' / 'state'
        log = state / 'agentrc' / 'worktree-remove' / 'log'
        for what, path, script, env, cwd in (
                ('the working directory', dirty / 'build', SCRIPT, {}, dirty / 'build'),
                ('the log', log, SCRIPT, {'XDG_STATE_HOME': str(state)}, None),
                ('worktree_children', copy, copy, {}, None)):
            with self.subTest(what):
                done = subprocess.run([sys.executable, str(script), 'cleanup', str(self.parent)],
                                      env={**self.env, **env}, cwd=cwd, capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(f'refused: {what} {path} lies inside {dirty}; run this from outside it', done.stderr)
                self.assertEqual(done.stdout, '')
                self.assertTrue(clean.is_dir() and dirty.is_dir())
                self.assertTrue(self.branch_exists('child'))
        self.assertFalse(self.log.exists())
        self.assertFalse(state.exists())

    def test_a_retry_with_the_log_under_the_removed_parent_is_refused(self):
        child = self.child()
        tip = self.git('rev-parse', 'HEAD', cwd=self.parent).strip()
        self.drop_parent()
        state = self.parent / 'build' / 'state'
        done = self.run_script('cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'parent', tip,
                               env={'XDG_STATE_HOME': str(state)})
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn(f'refused: the log {state / "agentrc" / "worktree-remove" / "log"} lies inside {self.parent}',
                      done.stderr)
        self.assertFalse(os.path.lexists(self.parent))
        self.assertTrue(child.is_dir() and self.branch_exists('parent'))

    def test_an_incomplete_cleanup_logs_a_retry_that_finishes_it_from_outside(self):
        dirty, unpushed = self.child('dirty'), self.child('side', push=False)
        (dirty / 'notes.txt').write_text('wip')
        side = self.commit(unpushed, 'side.txt', 'side')
        tip = self.commit(self.parent, 'parent.txt', 'parent')
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent), '--branch', str(self.main / '.git'), 'parent', tip)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f'kept {dirty}: uncommitted or untracked changes', done.stdout)
        self.assertFalse(unpushed.exists())
        self.assertTrue(self.branch_exists('parent') and self.branch_exists('side'))
        [retry] = self.logged_retries()
        common = str(self.main / '.git')
        self.assertEqual(retry[:4], [sys.executable, str(SCRIPT), 'cleanup', str(self.parent)])
        self.assertEqual(sorted(zip(*[iter(retry[4:])] * 4)),
                         [('--branch', common, 'parent', tip), ('--branch', common, 'side', side)])
        (dirty / 'notes.txt').unlink()
        self.git('push', '-q', 'origin', 'parent', 'side', cwd=self.main)
        done = subprocess.run(retry, env=self.env, cwd=self.t, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(dirty.exists())
        for branch in ('parent', 'side', 'dirty'):
            self.assertFalse(self.branch_exists(branch), branch)
        self.assertEqual(self.logged_retries(), [retry])

    def test_a_retry_is_logged_when_only_children_were_kept(self):
        kept, gone = self.child('a'), self.child('b')
        (kept / 'notes.txt').write_text('wip')
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(kept.is_dir())
        self.assertFalse(gone.exists() or self.branch_exists('b'))
        self.assertNotIn('kept branch', done.stdout)
        self.assertEqual(self.logged_retries(), [shlex.split(self.retry())])
        self.assertTrue(self.log.read_text(encoding='utf-8').endswith(f' retry: {self.retry()}\n'))


@unittest.skipIf(os.name == 'nt', 'symlinks')
class Processes(unittest.TestCase):
    """check_processes on a /proc stand-in: a live process with a name that is no UTF-8 would trip other suites'
    own /proc scans run beside this one."""
    def test_a_name_that_is_no_utf8_is_shown_escaped_and_spared_pids_are_skipped(self):
        spec = importlib.util.spec_from_file_location('worktree_children', SCRIPT)
        wc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wc)
        proc, tree = tmpdir(self) / 'proc', tmpdir(self) / 'tree'
        (proc / 'self').mkdir(parents=True)
        (proc / 'self' / 'cwd').symlink_to('/')
        tree.mkdir()
        for pid, name, cwd in (('7', b'worker-\xff\n', tree), ('8', b'spared\n', tree), ('9', b'away\n', proc)):
            (proc / pid).mkdir()
            (proc / pid / 'cwd').symlink_to(os.path.realpath(cwd))
            (proc / pid / 'comm').write_bytes(name)
        with mock.patch.object(wc, 'PROC', proc), self.assertRaises(wc.Keep) as kept:
            wc.check_processes(tree, frozenset({8}))
        self.assertEqual(str(kept.exception), 'a process works in it: worker-\\xff (pid 7)')


@unittest.skipIf(os.name == 'nt', 'symlinks')
class Planning(unittest.TestCase):
    def test_each_tree_is_searched_at_most_once_and_only_when_it_can_matter(self):
        spec = importlib.util.spec_from_file_location('worktree_children', SCRIPT)
        wc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wc)
        walked, walk = [], wc.walk

        def counted(top):
            walked.append(Path(top).name)
            return walk(top)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(wc, 'walk', counted):
            s, a, b, c, d = (Path(tmp) / n for n in 'sabcd')
            for tree, target in ((s, a), (a, b), (b, c), (c, d), (d, None)):
                tree.mkdir()
                if target:
                    (tree / 'link').symlink_to(target)
            self.assertEqual(wc.removal_plan([], [s, a]), ([], {}))
            self.assertEqual(walked, [])
            self.assertEqual(set(wc.removal_plan([a, b, c], [s])[1]), {a, b, c})
            self.assertEqual(walked, ['s', 'a', 'b'], 'c holds nothing left to protect')
            walked.clear()
            self.assertEqual(wc.removal_plan([a, b, c, d], []), ([a, b, c, d], {}))
            self.assertEqual(sorted(walked), ['a', 'b', 'c', 'd'])


class HcPath(unittest.TestCase):
    def test_hc_comes_from_path_alone_never_the_working_directory(self):
        spec = importlib.util.spec_from_file_location('worktree_children', SCRIPT)
        wc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wc)
        t = tmpdir(self)
        name = 'hc.exe' if os.name == 'nt' else 'hc'
        empty, bin_dir, here = t / 'empty', t / 'bin', t / 'here'
        for d in (empty, bin_dir, here):
            d.mkdir()
        for d in (bin_dir, here):
            (d / name).write_text('')
            (d / name).chmod(0o755)
        cwd = os.getcwd()
        os.chdir(here)  # a checkout holding an hc: shutil.which on Windows takes it first
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(wc.ENV, PATH=str(empty)):
            self.assertIsNone(wc.hc_path())
        with mock.patch.dict(wc.ENV, PATH=os.pathsep.join([str(empty), str(bin_dir)])):
            self.assertEqual(os.path.normcase(wc.hc_path()), os.path.normcase(str(bin_dir / name)))


class LinkHops(unittest.TestCase):
    """link_hops and holds on this platform's own paths, Windows drives included."""
    def setUp(self):
        spec = importlib.util.spec_from_file_location('worktree_children', SCRIPT)
        self.wc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wc)
        self.t = tmpdir(self)
        try:
            self.link(self.t / 'probe', self.t)
        except (OSError, NotImplementedError) as err:  # Windows without the symlink privilege
            self.skipTest(f'cannot create a symlink here: {err}')

    def link(self, link, target):
        os.symlink(target, link, target_is_directory=True)

    def test_hops_are_absolute_from_the_anchor(self):
        kept, clean = self.t / 'parent--z', self.t / 'parent--b'
        for tree in (kept, clean / 'lib'):
            tree.mkdir(parents=True)
        self.link(kept / 'abs', clean / 'lib')
        self.link(kept / 'rel', Path('..') / clean.name / 'lib')
        anchor = os.path.splitdrive(str(self.t))[0] + os.sep
        for name in ('abs', 'rel'):
            hops = self.wc.link_hops(str(kept / name))
            self.assertTrue(all(os.path.isabs(h) and h.startswith(anchor) for h in hops), hops)
            self.assertIn(str(kept / name), hops)
            self.assertEqual(hops[-2:], [str(clean), str(clean / 'lib')])
        self.link(kept / 'up', os.sep.join(['..'] * (len(self.t.parts) + 3) + ['zz']))  # `..` stops at the anchor
        self.assertEqual(self.wc.link_hops(str(kept / 'up'))[-2:], [str(kept / 'up'), anchor + 'zz'])

    def test_a_kept_siblings_symlink_keeps_the_clean_sibling_it_reaches(self):
        kept, clean, plain = self.t / 'parent--z', self.t / 'parent--b', self.t / 'parent--w'
        for tree in (kept, clean / 'lib', plain):
            tree.mkdir(parents=True)
        self.link(kept / 'dep', clean / 'lib')
        self.assertEqual(self.wc.removal_plan([clean, plain], [kept]),
                         ([plain], {clean: 'parent--z, which stays, links into it'}))

    @unittest.skipUnless(os.name == 'nt', 'Windows spells a path in any case')
    def test_a_link_spelled_in_another_case_still_holds_its_target(self):
        kept, clean = self.t / 'parent--z', self.t / 'parent--b'
        for tree in (kept, clean / 'lib'):
            tree.mkdir(parents=True)
        self.link(kept / 'dep', str(clean / 'lib').swapcase())
        self.assertEqual(self.wc.removal_plan([clean], [kept]),
                         ([], {clean: 'parent--z, which stays, links into it'}))

    @unittest.skipUnless(os.name == 'nt', 'Windows junctions')
    def test_a_link_ending_at_a_junction_holds_the_junctions_target(self):
        import _winapi
        kept, clean = self.t / 'parent--z', self.t / 'parent--b'
        for tree in (kept, clean / 'lib'):
            tree.mkdir(parents=True)
        _winapi.CreateJunction(str(clean / 'lib'), str(self.t / 'alias'))
        self.link(kept / 'dep', self.t / 'alias')
        self.assertEqual(self.wc.removal_plan([clean], [kept]),
                         ([], {clean: 'parent--z, which stays, links into it'}))

    @unittest.skipUnless(os.name == 'nt', 'Windows junctions')
    def test_a_kept_siblings_junction_keeps_the_clean_sibling_it_reaches(self):
        import _winapi
        kept, clean = self.t / 'parent--z', self.t / 'parent--b'
        for tree in (kept, clean / 'lib'):
            tree.mkdir(parents=True)
        _winapi.CreateJunction(str(clean / 'lib'), str(kept / 'dep'))
        self.assertEqual(self.wc.removal_plan([clean], [kept]),
                         ([], {clean: 'parent--z, which stays, links into it'}))

    def assertJunctionThroughASymlinkHolds(self, junction):
        """junction(path, target) makes a junction: kept/dep through clean/lib, a symlink out of it, holds clean."""
        kept, clean, outside = self.t / 'parent--z', self.t / 'parent--b', self.t / 'outside'
        for tree in (kept, clean, outside):
            tree.mkdir()
        self.link(clean / 'lib', outside)
        junction(kept / 'dep', clean / 'lib')
        self.assertEqual(self.wc.removal_plan([clean], [kept]),
                         ([], {clean: 'parent--z, which stays, links into it'}))

    def test_a_junction_through_a_symlink_in_the_clean_sibling_holds_it(self):
        islink, isjunction = os.path.islink, os.path.isjunction  # dep a symlink that reports itself a junction
        with mock.patch('os.path.islink', lambda p: os.path.basename(p) != 'dep' and islink(p)), \
                mock.patch('os.path.isjunction', lambda p: os.path.basename(p) == 'dep' or isjunction(p)):
            self.assertJunctionThroughASymlinkHolds(self.link)

    @unittest.skipUnless(os.name == 'nt', 'Windows junctions')
    def test_a_real_junction_through_a_symlink_in_the_clean_sibling_holds_it(self):
        import _winapi
        self.assertJunctionThroughASymlinkHolds(lambda path, target: _winapi.CreateJunction(str(target), str(path)))

    @unittest.skipUnless(os.name == 'nt', 'Windows path forms')
    def test_windows_targets_are_anchored_on_their_own_drive(self):
        anchored = self.wc.anchored
        self.assertEqual(anchored('\\\\?\\D:\\x\\y', 'C:\\w'), ('D:\\', ['x', 'y']))
        self.assertEqual(anchored('\\\\?\\UNC\\srv\\share\\x', 'C:\\w'), ('\\\\srv\\share\\', ['x']))
        self.assertEqual(anchored('\\\\srv\\share', 'C:\\w'), ('\\\\srv\\share\\', []))
        self.assertEqual(anchored('\\\\?\\UNC\\srv\\share', 'C:\\w'), ('\\\\srv\\share\\', []))
        self.assertEqual(anchored('\\\\?\\unc\\srv\\share\\x', 'C:\\w'), ('\\\\srv\\share\\', ['x']))
        self.assertEqual(anchored('\\\\?\\Volume{1}\\x', 'C:\\w'), ('\\\\?\\Volume{1}\\', ['x']))  # nothing mounts it
        import ctypes
        drive = os.path.splitdrive(str(self.t))[0] + '\\'
        volume = ctypes.create_unicode_buffer(64)
        self.assertTrue(ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, volume, 64))
        root, names = anchored(volume.value + 'x\\y', 'C:\\w')
        self.assertEqual((root.upper(), names), (drive.upper(), ['x', 'y']))
        self.assertEqual(anchored('D:/x/y', 'C:\\w'), ('D:\\', ['x', 'y']))
        self.assertEqual(anchored('\\x', 'C:\\w'), ('C:\\', ['x']))
        self.assertEqual(anchored('x/y', 'C:\\w'), ('C:\\w', ['x', 'y']))
        self.assertRaises(OSError, anchored, 'D:x', 'C:\\w')


# prints the locale encoding, then the bytes of linked_worktree(argv[2]) as UTF-8 JSON, with the module at argv[1]
LINKED = r'''import importlib.util, json, locale, os, sys
spec = importlib.util.spec_from_file_location('worktree_children', sys.argv[1])
wc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc)
print(locale.getpreferredencoding(False))
print(json.dumps([os.fsencode(p).decode('utf-8') for p in wc.linked_worktree(sys.argv[2])]))
'''

# prints the locale encoding, then logs each line JSON-encoded in argv[2:]
LOGGED = r'''import importlib.util, json, locale, sys
spec = importlib.util.spec_from_file_location('worktree_children', sys.argv[1])
wc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc)
print(locale.getpreferredencoding(False), flush=True)
for line in sys.argv[2:]:
    wc.log(json.loads(line))
'''


class NonUtf8Locale(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'the C locale stands in for a non-UTF-8 code page')
    def test_a_non_ascii_path_round_trips_whatever_the_locale(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(HOME=tempfile.gettempdir(), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        with tempfile.TemporaryDirectory() as tmp:
            main, child = Path(tmp).resolve() / 'ré', Path(tmp).resolve() / 'ré--kid'
            for args in (['init', '-q', str(main)], ['-C', str(main), 'commit', '-q', '--allow-empty', '-m', 'x'],
                         ['-C', str(main), 'worktree', 'add', '-q', str(child)]):
                subprocess.run(['git', *args], env=env, check=True, capture_output=True)
            env.update(LC_ALL='C', PYTHONCOERCECLOCALE='0', PYTHONUTF8='0')
            done = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', LINKED, str(SCRIPT), str(child)],
                                  env=env, capture_output=True, encoding='utf-8')
            encoding, _, answer = done.stdout.partition('\n')
            if encoding.lower().replace('-', '') == 'utf8':
                self.skipTest(f'the C locale encoding is {encoding} here')
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(answer), [str(main / '.git' / 'worktrees' / 'ré--kid'), str(main / '.git')])

    @unittest.skipIf(os.name == 'nt', 'the C locale stands in for a non-UTF-8 code page')
    def test_a_line_the_locale_cannot_encode_is_printed_and_logged_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'XDG_STATE_HOME': tmp, 'LC_ALL': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0'}
            env.pop('PYTHONIOENCODING', None)
            done = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', LOGGED, str(SCRIPT),
                                   json.dumps('kept /r\u00e9--k' + os.fsdecode(b'\xff'))], env=env, capture_output=True)
            encoding, _, printed = done.stdout.partition(b'\n')
            if encoding.decode().lower().replace('-', '') == 'utf8':
                self.skipTest(f'the C locale encoding is {encoding.decode()} here')
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(printed, b'kept /r\\xe9--k\\udcff\n')
            log = Path(tmp) / 'agentrc' / 'worktree-remove' / 'log'
            self.assertTrue(log.read_bytes().endswith(b' kept /r\xc3\xa9--k\xff\n'))

    def test_a_line_a_narrow_code_page_cannot_encode_is_escaped_and_still_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'XDG_STATE_HOME': tmp, 'PYTHONIOENCODING': 'cp1252:strict'}
            done = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', LOGGED, str(SCRIPT),
                                   json.dumps('kept /r\u00e9--\u4e2d'), json.dumps('kept /x\ud800y'),
                                   json.dumps('kept /m\udcff\ud800')],
                                  env=env, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.splitlines()[1:],
                             [b'kept /r\xe9--\\u4e2d', b'kept /x\\ud800y', b'kept /m\\udcff\\ud800'])
            log = (Path(tmp) / 'agentrc' / 'worktree-remove' / 'log').read_bytes()
            self.assertEqual([line.split(b' ', 2)[2] for line in log.split(os.linesep.encode())[:-1]],
                             [b'kept /r\xc3\xa9--\xe4\xb8\xad', b'kept /x\\ud800y', b'kept /m\xff\\ud800'])


if __name__ == '__main__':
    unittest.main()
