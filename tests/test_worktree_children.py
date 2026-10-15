import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'plugins' / 'worktree-children' / 'worktree_children.py'

STUB = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['STUB_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
mode = open(os.environ['STUB_MODE']).read().strip()
if sys.argv[1:3] == ['worktree', 'list']:
    if mode == 'down':
        sys.exit('herdr server unreachable')
    if mode == 'recreate':
        os.makedirs(os.environ['STUB_PARENT'], exist_ok=True)
    path = sys.argv[sys.argv.index('--cwd') + 1]
    entry = {'path': path, 'is_linked_worktree': True}
    if mode == 'open':
        entry['open_workspace_id'] = 'w9'
    print(json.dumps({'result': {'worktrees': [None] if mode == 'null' else [entry]}}))
    sys.exit(1 if mode == 'failed' else 0)
'''

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
        for k in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                  'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_PREFIX', 'GIT_NAMESPACE'):
            self.env.pop(k, None)
        stub = self.t / 'herdr'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.calls, self.mode = self.t / 'calls', self.t / 'mode'
        self.mode.write_text('closed')
        self.env.update(HERDR_BIN_PATH=str(stub), STUB_LOG=str(self.calls), STUB_MODE=str(self.mode),
                        HERDR_PLUGIN_STATE_DIR=str(self.t / 'state'), STUB_PARENT=str(self.t / 'wt' / 'parent'))
        gh_dir = self.t / 'gh-bin'
        gh_dir.mkdir()
        (gh_dir / 'gh').write_text(GH)
        (gh_dir / 'gh').chmod(0o755)
        self.env['PATH'] = f'{gh_dir}{os.pathsep}{self.env["PATH"]}'
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
        t/github.com/<name>.git, a path the plugin takes for a GitHub remote."""
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

    def run_script(self, *argv, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *argv], env={**self.env, **(env or {})},
                              capture_output=True, text=True)

    def cleanup(self):
        self.drop_parent()
        return self.run_script('cleanup', str(self.parent))

    def notifications(self):
        if not self.calls.exists():
            return []
        return [c for c in map(json.loads, self.calls.read_text().splitlines()) if c[:2] == ['notification', 'show']]

    def assert_kept(self, child, reason):
        done = self.cleanup()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(child.is_dir())
        self.assertIn(reason, done.stdout)
        [note] = self.notifications()
        self.assertIn(child.name, note[note.index('--body') + 1])
        self.assertIn(f'cleanup {self.parent}', note[note.index('--body') + 1])

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
        self.assertEqual(self.notifications(), [])
        self.assertIn(f'removed {child}', (self.t / 'state' / 'log').read_text())

    def test_grandchildren_and_children_of_other_repositories_go_too_and_other_entries_stay(self):
        other = self.repo('other')
        kids = [self.child(), self.child('child--grand'), self.child('foreign', repo=other)]
        plain = self.wt / 'parent--plain'
        plain.mkdir()
        unrelated = self.wt / 'parentx'
        self.git('worktree', 'add', '-q', str(unrelated), '-b', 'parentx', cwd=self.main)
        self.assert_removed(*kids)
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

    def test_an_operation_in_progress_keeps_the_child(self):
        child = self.child()
        gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip())
        (gitdir / 'MERGE_HEAD').write_text(self.git('rev-parse', 'HEAD', cwd=child))
        self.assert_kept(child, 'MERGE_HEAD in progress')

    def test_an_open_or_unconfirmed_herdr_workspace_keeps_the_child(self):
        for mode, reason in (('open', 'open in herdr workspace w9'), ('down', 'herdr could not list it'),
                             ('failed', 'herdr could not list it'), ('null', 'herdr could not list it')):
            with self.subTest(mode):
                self.tearDown()
                self.setUp()
                child = self.child()
                self.mode.write_text(mode)
                self.assert_kept(child, reason)

    def test_a_missing_herdr_keeps_every_child_and_reports_them(self):
        kids = [self.child(), self.child('second')]
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent), env={'HERDR_BIN_PATH': str(self.t / 'no-herdr')})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for kid in kids:
            self.assertTrue(kid.is_dir())
            self.assertIn(f'kept {kid}: herdr could not list it', done.stdout)

    def test_a_private_ref_on_a_unique_commit_keeps_the_child(self):
        child = self.child()
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

    def test_a_long_report_keeps_the_retry_command_in_the_notification(self):
        kids = [self.child(f'k{i}') for i in range(3)]
        for kid in kids:
            deep = kid / 'build' / '/'.join(['d' * 200] * 3)
            self.git('init', '-q', str(deep), cwd=kid)
        self.drop_parent()
        done = self.run_script('cleanup', str(self.parent))
        self.assertEqual(done.returncode, 1)
        [note] = self.notifications()
        body = note[note.index('--body') + 1]
        self.assertIn(f'cleanup {self.parent}', body)
        self.assertIn(str(self.t / 'state' / 'log'), body)
        self.assertLessEqual(len(body), 1000)

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
        self.assertIn('kept branch child', done.stdout)
        [note] = self.notifications()
        self.assertIn('branch child: commits on no origin branch', note[note.index('--body') + 1])
        log = (self.t / 'state' / 'log').read_text()
        retry = f'cleanup {self.parent} --branch {self.main / ".git"} child {tip}'
        self.assertIn(f'its branch is retried by: {SCRIPT} {retry}', log)
        self.assertLess(log.index('its branch is retried by'), log.index(f'removed {child}'))
        self.git('push', '-q', 'origin', 'child', cwd=self.main)
        done = self.run_script(*retry.split())
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
        self.assertEqual([c for c in map(json.loads, self.calls.read_text().splitlines()) if c[:1] == ['gh']], [])

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
        [retry] = [l.split(' cleanup ', 1)[1].split() for l in done.stdout.splitlines() if l.startswith('retry: ')]
        node = {'headRefName': 'a', 'headRefOid': self.oid('a'), 'state': 'OPEN', 'mergedAt': None, 'number': 1}
        for missing in ('number', 'headRefOid'):
            answer = {'nodes': [{k: v for k, v in node.items() if k != missing}], 'issueCount': 1}
            (self.t / 'prs').write_text(json.dumps(answer))
            self.calls.unlink()
            done = self.run_script('cleanup', *retry, env={'STUB_PRS': str(self.t / 'prs')})
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn('kept branch a in', done.stdout)
            self.assertIn('malformed answer', done.stdout)
            self.assertIn('kept branch b in', done.stdout)
            [note] = self.notifications()
            self.assertIn('2 branch(es)', note[note.index('show') + 1])
        self.assertTrue(self.branch_exists('a') and self.branch_exists('b'))

    def test_the_event_settles_the_parents_branch_from_its_workspace_snapshot(self):
        self.git('push', '-q', 'origin', 'parent', cwd=self.parent)
        child = self.child()
        self.drop_parent()
        event = {'event': 'worktree.removed', 'data': {
            'type': 'worktree_removed', 'workspace_id': 'w1', 'forced': False,
            'workspace': {'workspace_id': 'w1', 'worktree': {
                'repo_key': str(self.main / '.git'), 'repo_name': 'main', 'repo_root': str(self.main),
                'checkout_path': str(self.parent), 'is_linked_worktree': True}},
            'worktree': {'path': str(self.parent), 'branch': 'parent', 'is_bare': False, 'is_detached': False,
                         'is_prunable': False, 'is_linked_worktree': True, 'label': 'parent'}}}
        done = self.run_script('event', env={'HERDR_PLUGIN_EVENT_JSON': json.dumps(event)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.branch_exists('parent'))
        self.assertFalse(child.exists())
        self.assertFalse(self.branch_exists('child'))
        self.git('branch', 'parent', 'master', cwd=self.main)
        del event['data']['workspace']
        done = self.run_script('event', env={'HERDR_PLUGIN_EVENT_JSON': json.dumps(event)})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('kept branch parent: the event names no repository', done.stdout)
        self.assertTrue(self.branch_exists('parent'))

    # --- agents ----------------------------------------------------------------------

    def test_a_running_chief_keeps_the_child(self):
        child = self.child()
        chief = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--agent', 'chief'], cwd=child)
        self.addCleanup(chief.wait)
        self.addCleanup(chief.kill)
        self.assert_kept(child, f'a headless chief runs in it: pid {chief.pid}')

    def test_a_cowork_lane_with_a_request_keeps_the_child(self):
        child = self.child()
        box = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip()) / 'cowork' / 'codex' / 'review'
        box.mkdir(parents=True)
        (box / 'codex-review-1.lock').write_text('')
        self.assert_kept(child, 'cowork lane codex/review holds a running or undelivered request')

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
        done = self.run_script('cleanup', str(self.parent))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(child.is_dir())

    def test_a_parent_recreated_during_the_checks_stops_the_sweep(self):
        kids = [self.child(), self.child('second')]
        self.mode.write_text('recreate')
        done = self.cleanup()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertTrue(self.parent.is_dir())
        for kid in kids:
            self.assertTrue(kid.is_dir(), done.stdout)
            self.assertNotIn(f'removed {kid}', done.stdout)

    def test_the_event_reads_herdrs_envelope_and_bad_events_are_usage_errors(self):
        child = self.child()
        self.drop_parent()
        event = {'event': 'worktree.removed', 'data': {'type': 'worktree_removed', 'workspace_id': 'w1',
                 'worktree': {'path': str(self.parent)}, 'forced': True}}
        done = self.run_script('event', env={'HERDR_PLUGIN_EVENT_JSON': json.dumps(event)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(child.exists())
        self.assertEqual(self.run_script('event', env={'HERDR_PLUGIN_EVENT_JSON': json.dumps(event)}).returncode, 0,
                         'a repeated event finds nothing to do')
        for bad in ('{}', 'not json', json.dumps({'data': {'worktree': {'path': 'relative'}}})):
            self.assertEqual(self.run_script('event', env={'HERDR_PLUGIN_EVENT_JSON': bad}).returncode, 2, bad)
        for argv in ([], ['cleanup', str(self.parent), '--branch', 'a', 'b'], ['cleanup', str(self.parent), 'x']):
            self.assertEqual(self.run_script(*argv).returncode, 2, argv)


if __name__ == '__main__':
    unittest.main()
