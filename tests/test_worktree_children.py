import json
import os
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
    path = sys.argv[sys.argv.index('--cwd') + 1]
    entry = {'path': path, 'is_linked_worktree': True}
    if mode == 'open':
        entry['open_workspace_id'] = 'w9'
    print(json.dumps({'result': {'worktrees': [None] if mode == 'null' else [entry]}}))
    sys.exit(1 if mode == 'failed' else 0)
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
        for k in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            self.env.pop(k, None)
        stub = self.t / 'herdr'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.calls, self.mode = self.t / 'calls', self.t / 'mode'
        self.mode.write_text('closed')
        self.env.update(HERDR_BIN_PATH=str(stub), STUB_LOG=str(self.calls), STUB_MODE=str(self.mode),
                        HERDR_PLUGIN_STATE_DIR=str(self.t / 'state'))
        # an upstream for the submodule, and the main repository with it
        self.git('init', '-q', '--bare', str(self.t / 'sub.git'), cwd=self.t)
        self.git('clone', '-q', str(self.t / 'sub.git'), str(self.t / 'subwork'), cwd=self.t)
        self.commit(self.t / 'subwork', 'a.txt', 'sub')
        self.git('push', '-q', 'origin', 'HEAD:refs/heads/master', cwd=self.t / 'subwork')
        self.main = self.t / 'main'
        self.git('init', '-q', '-b', 'master', str(self.main), cwd=self.t)
        self.commit(self.main, '.gitignore', 'build/\n')
        self.commit(self.main, 'README', 'main')
        self.git('submodule', 'add', '-q', str(self.t / 'sub.git'), 'lib/sub', cwd=self.main)
        self.git('commit', '-qm', 'sub', cwd=self.main)
        self.wt = self.t / 'wt'
        self.wt.mkdir()
        self.parent = self.wt / 'parent'
        self.git('worktree', 'add', '-q', str(self.parent), '-b', 'parent', cwd=self.main)
        self.sentinel = self.t / 'shared'
        self.sentinel.mkdir()
        (self.sentinel / 'keep').write_text('shared target')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd):
        done = subprocess.run(['git', *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, f'git {args}: {done.stderr}')
        return done.stdout

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        self.git('add', name, cwd=repo)
        self.git('commit', '-qm', name, cwd=repo)
        return self.git('rev-parse', 'HEAD', cwd=repo).strip()

    def child(self, name='child', repo=None, init=True):
        path = self.wt / f'parent--{name}'
        self.git('worktree', 'add', '-q', str(path), '-b', name, cwd=repo or self.main)
        if init and (path / '.gitmodules').exists():
            self.git('submodule', 'update', '-q', '--init', cwd=path)
        (path / 'build').mkdir()
        (path / 'build' / 'out.bin').write_text('ignored build output')
        (path / 'build' / 'deps').symlink_to(self.sentinel)
        return path

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

    def test_a_clean_child_goes_with_its_ignored_files_and_keeps_its_branch_and_shared_targets(self):
        child = self.child()
        self.assert_removed(child)
        self.assertIn('child', self.git('branch', '--list', 'child', cwd=self.main))
        self.assertEqual((self.sentinel / 'keep').read_text(), 'shared target')
        self.assertNotIn(str(child), self.git('worktree', 'list', cwd=self.main))
        self.assertEqual(self.notifications(), [])
        self.assertIn(f'removed {child}', (self.t / 'state' / 'log').read_text())

    def test_grandchildren_and_children_of_other_repositories_go_too_and_other_entries_stay(self):
        other = self.t / 'other'
        self.git('init', '-q', '-b', 'master', str(other), cwd=self.t)
        self.commit(other, '.gitignore', 'build/\n')
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
        for mode, reason in (('open', 'open in herdr workspace w9'), ('down', 'herdr could not confirm'),
                             ('failed', 'herdr could not confirm'), ('null', 'herdr could not confirm')):
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
            self.assertIn(f'kept {kid}: herdr could not confirm', done.stdout)

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
        sub = child / 'lib' / 'sub'
        gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=child).strip())
        old = Path(self.git('rev-parse', '--absolute-git-dir', cwd=sub).strip())
        store = gitdir / 'custom-store'
        old.rename(store)
        (sub / '.git').write_text(f'gitdir: {store}\n')
        self.git('config', '--file', str(store / 'config'), 'core.worktree', str(sub), cwd=child)
        (sub / 'a.txt').write_text('stashed')
        self.git('stash', '-q', cwd=sub)
        self.assert_kept(child, 'a stash in submodule repository')

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

    def test_a_submodule_worktree_outside_the_child_keeps_it(self):
        child = self.child()
        self.git('worktree', 'add', '-q', '--detach', str(self.t / 'outside'), cwd=child / 'lib' / 'sub')
        self.assert_kept(child, 'has a worktree at')

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

    # --- refusals and input ----------------------------------------------------------

    def test_nothing_runs_while_the_parent_path_exists(self):
        child = self.child()
        done = self.run_script('cleanup', str(self.parent))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(child.is_dir())

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
        self.assertEqual(self.run_script().returncode, 2)


if __name__ == '__main__':
    unittest.main()
