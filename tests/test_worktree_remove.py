"""Tests for worktree-remove's worktree_remove.py on real git repos: a bare origin, a primary checkout,
a worktree `wt/parent` and its `parent--*` siblings; herdr and gh are stubs, or herdr is absent."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'worktree-remove' / 'scripts' / 'worktree_remove.py'
spec = importlib.util.spec_from_file_location('worktree_remove', SCRIPT)
wr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wr)

HERDR = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
mode = open(os.environ['STUB_MODE']).read().split()
reports = os.environ['STUB_REPORTS']
with open(os.environ['STUB_LOG'], 'a') as f:
    f.write(json.dumps({'argv': args, 'reports': sorted(os.listdir(reports)) if os.path.isdir(reports) else []}) + '\n')
if 'down' in mode:
    sys.exit('herdr server unreachable')
if args[:2] == ['worktree', 'list']:
    path = args[args.index('--cwd') + 1]
    entry = {'path': path, 'is_linked_worktree': True}
    if path == os.environ['STUB_OPEN'] and 'closed' not in mode:
        entry['open_workspace_id'] = 'w1'
    if 'strlinked' in mode:
        entry['is_linked_worktree'] = 'false'
    print(json.dumps({'result': {'worktrees': [entry]}}))
elif args[:2] == ['plugin', 'list']:
    root = '/elsewhere' if 'elsewhere' in mode else os.environ['STUB_ROOT']
    plugins = [] if 'noplugin' in mode else [{'plugin_id': 'agentrc.worktree-children', 'enabled': True,
                                             'plugin_root': root}]
    print(json.dumps({'result': {'plugins': plugins}}))
elif args[:2] == ['worktree', 'remove']:
    sys.exit('herdr: worktree_remove_failed' if 'fail' in mode else 0)
'''


def sh(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def setUpModule():
    guard_survivors(__name__)


class Rig(unittest.TestCase):
    """The repositories and stubs; herdr has wt/parent open as workspace w1."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name).resolve()
        cfg = self.t / 'gitconfig'
        cfg.write_text('[user]\n name = t\n email = t@t\n[init]\n defaultBranch = master\n'
                       '[protocol "file"]\n allow = always\n')
        bin_dir = self.t / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'herdr').write_text(HERDR)
        (bin_dir / 'gh').write_text('#!/bin/sh\necho "gh: network unreachable" >&2\nexit 1\n')
        for stub in bin_dir.iterdir():
            stub.chmod(0o755)
        self.mode, self.calls, self.reports = self.t / 'mode', self.t / 'calls', self.t / 'state' / 'reports'
        self.mode.write_text('')
        self.wt = self.t / 'wt'
        self.parent = self.wt / 'parent'
        env = {'GIT_CONFIG_GLOBAL': str(cfg), 'GIT_CONFIG_NOSYSTEM': '1', 'HERDR_BIN_PATH': str(bin_dir / 'herdr'),
               'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', 'STUB_MODE': str(self.mode),
               'STUB_LOG': str(self.calls), 'STUB_REPORTS': str(self.reports), 'STUB_OPEN': str(self.parent),
               'STUB_ROOT': str(wr.PLUGIN_DIR)}
        for patch in (mock.patch.dict(os.environ, env), mock.patch.object(wr, 'REPORTS', self.reports),
                      mock.patch.object(wr, 'LOG', self.reports / 'log'),
                      mock.patch.object(wr, 'PLUGIN_LOG', self.t / 'state' / 'herdr-plugin-log')):
            patch.start()
            self.addCleanup(patch.stop)
        wr.wc = wr.load_plugin()  # fresh: it reads the environment and caches fetches
        self.main = self.repo('main')
        self.wt.mkdir()
        sh(self.main, 'worktree', 'add', '-q', str(self.parent), '-b', 'parent')
        sh(self.parent, 'push', '-q', 'origin', 'parent')

    def repo(self, name):
        path = self.t / name
        sh(self.t, 'init', '-q', '--bare', str(self.t / f'{name}.git'))
        sh(self.t, 'clone', '-q', str(self.t / f'{name}.git'), str(path))
        self.commit(path, '.gitignore', 'build/\n')
        sh(path, 'push', '-q', 'origin', 'master')
        return path

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        sh(repo, 'add', name)
        sh(repo, 'commit', '-qm', name)
        return sh(repo, 'rev-parse', 'HEAD')

    def sibling(self, name, repo=None, push=True):
        path = self.wt / f'parent--{name}'
        sh(repo or self.main, 'worktree', 'add', '-q', str(path), '-b', name)
        if push:
            sh(path, 'push', '-q', 'origin', name)
        return path

    def run_remove(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = wr.main([str(self.parent), *argv])
        return rc, out.getvalue() + err.getvalue()

    def removals(self):
        calls = [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return [c for c in calls if c['argv'][:2] == ['worktree', 'remove']]

    def token(self, out):
        return re.search(r'--proceed (\w+)', out).group(1)

    def after_plan(self, change):
        """wr.plan patched to make change once the plan is made, as if between the gate and the removal."""
        plan = wr.plan

        def then(*args):
            p = plan(*args)
            change()
            return p

        return mock.patch.object(wr, 'plan', then)

    def assertRefused(self, text, *argv):
        rc, out = self.run_remove(*argv)
        self.assertEqual(rc, 3, out)
        self.assertIn(text, out)
        self.assertEqual(self.removals(), [])
        self.assertFalse(self.reports.exists())
        self.assertTrue(self.parent.is_dir())

    def assert_removed_by_git(self):
        sibling, dirty = self.sibling('b'), self.sibling('dirty')
        (dirty / 'wip.txt').write_text('wip')
        rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        rc, out = self.run_remove('--proceed', self.token(out))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.removals(), [])
        self.assertFalse(self.parent.exists() or sibling.exists())
        self.assertTrue((dirty / 'wip.txt').is_file())
        self.assertEqual(sh(self.main, 'branch', '--list', 'parent', 'b'), '')
        self.assertTrue(sh(self.main, 'branch', '--list', 'dirty'))
        [report] = [r for r in self.reports.iterdir() if r.suffix == '.md']
        self.assertIn(f'{self.parent}: git removes it; branch parent goes: on origin', report.read_text())
        log = (self.reports / 'log').read_text()
        for line in (f'removed {self.parent}', f'removed {sibling}', 'deleted branch parent', 'deleted branch b',
                     f'kept {dirty}: uncommitted or untracked changes'):
            self.assertIn(line, log)
            self.assertIn(line, out)


@unittest.skipIf(os.name == 'nt', 'POSIX paths and stub executables')
class Remove(Rig):
    # --- refused --------------------------------------------------------------------

    def test_the_primary_checkout_is_refused(self):
        self.parent = self.main
        self.assertRefused('a primary checkout')

    def test_a_worktree_herdr_has_not_open_is_removed_by_git_and_the_plugins_cleanup_unlinked(self):
        self.mode.write_text('closed noplugin')
        self.assert_removed_by_git()

    def test_a_token_from_the_git_route_stops_once_herdr_has_it_open(self):
        self.mode.write_text('closed')
        rc, out = self.run_remove('--pending', 'x')
        self.assertEqual(rc, 4, out)
        self.mode.write_text('')
        rc, out = self.run_remove('--pending', 'x', '--proceed', self.token(out))
        self.assertEqual(rc, 4, out)
        self.assertEqual(self.removals(), [])

    def test_a_malformed_herdr_entry_is_refused(self):
        self.mode.write_text('strlinked')
        self.assertRefused('malformed entry')

    def test_an_unreachable_herdr_is_refused(self):
        self.mode.write_text('down')
        self.assertRefused('herdr server unreachable')

    def test_a_missing_or_foreign_plugin_is_refused(self):
        self.mode.write_text('noplugin')
        self.assertRefused('no enabled agentrc.worktree-children')
        self.mode.write_text('elsewhere')
        self.assertRefused('from /elsewhere')

    def test_a_parent_holding_work_is_refused(self):
        (self.parent / 'new.txt').write_text('x')
        self.assertRefused('uncommitted or untracked changes')

    def test_a_parent_with_a_running_chief_or_a_busy_cowork_lane_is_refused(self):
        chief = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--agent', 'chief'],
                                 cwd=self.parent)
        self.addCleanup(chief.wait)
        self.addCleanup(chief.kill)
        self.assertRefused(f'a chief session (headless or interactive) runs in it: pid {chief.pid}')
        chief.kill()
        chief.wait()
        box = Path(sh(self.parent, 'rev-parse', '--absolute-git-dir')) / 'cowork' / 'claude' / 'main'
        box.mkdir(parents=True)
        (box / 'claude-main-1.lock').write_text('')
        self.assertRefused('cowork lane claude/main holds')

    def test_a_report_dir_inside_a_removed_tree_is_refused(self):
        sibling = self.sibling('b')
        for inside in (self.parent / 'build' / 'reports', sibling / 'build' / 'reports'):
            with mock.patch.object(wr, 'REPORTS', inside):
                self.assertRefused(f'{inside} lies inside a worktree this removes')
        state = self.t / 'herdr-state'
        state.symlink_to(self.parent / 'build')  # resolved: a log reached through a link counts too
        with mock.patch.object(wr, 'PLUGIN_LOG', state / 'log'):
            self.assertRefused(f'{state / "log"} lies inside a worktree this removes')

    def test_a_tree_changing_after_its_check_is_refused(self):
        self.sibling('b')
        for err in (wr.wc.Keep('cannot list /x: Permission denied'), FileNotFoundError(2, 'gone', '/x/link')):
            with self.subTest(err=err), mock.patch.object(wr, 'links_leaving', side_effect=err):
                self.assertRefused('listing the symlinks leaving the removed trees')

    def test_a_failed_github_lookup_is_refused(self):
        with mock.patch.object(wr.wc, 'GITHUB', re.compile('')):
            self.assertRefused('network unreachable')

    # --- pending ----------------------------------------------------------------------

    def test_keeps_and_unmerged_pushes_are_pending_until_the_human_agrees(self):
        dirty = self.sibling('dirty')
        (dirty / 'wip.txt').write_text('wip')
        local = self.sibling('local', push=False)
        self.commit(local, 'local.txt', 'local')
        other = self.repo('other')
        foreign = self.sibling('foreign', repo=other, push=False)
        self.commit(foreign, 'f.txt', 'f')
        sh(foreign, 'push', '-q', 'origin', 'foreign')
        rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('parent--dirty stays: uncommitted or untracked changes', out)
        self.assertIn('parent--local: branch local stays: commits on no origin branch', out)
        self.assertIn('foreign: commits on origin that master lacks', out)
        self.assertNotIn('parent:', out)
        self.assertEqual(self.removals(), [])
        rc, out = self.run_remove('--proceed', self.token(out))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.removals()), 1)

    def test_a_staying_siblings_own_children_and_link_targets_stay(self):
        target = self.sibling('dep-x')
        owner = self.sibling('z')
        (owner / 'wip.txt').write_text('wip')
        (owner / 'dep').symlink_to(target)
        owned = self.sibling('z--dep')
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 4, out)
        self.assertIn(f'{target}: stays: parent--z, which stays, links into it', out)
        self.assertIn(f'{owned}: stays: owned by parent--z, which stays', out)
        self.assertIn('parent--dep-x stays: parent--z, which stays, links into it', out)
        self.assertNotIn('the plugin removes it', out)

    def test_every_sibling_a_staying_siblings_link_passes_through_stays(self):
        bridge, target = self.sibling('bridge'), self.sibling('dep')
        owner = self.sibling('z')
        (owner / 'use').symlink_to(bridge / 'build' / 'forward')
        (bridge / 'build').mkdir()
        (bridge / 'build' / 'forward').symlink_to(target)
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 4, out)
        for sibling in (bridge, target):
            self.assertIn(f'{sibling}: stays: parent--z, which stays, links into it', out)

    def test_a_staying_siblings_link_through_a_sibling_and_back_out_holds_both(self):
        passed, target = self.sibling('a'), self.sibling('b')
        owner = self.sibling('z')
        (owner / 'use').symlink_to(f'{passed}/../{target.name}')
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 4, out)
        for sibling in (passed, target):
            self.assertIn(f'{sibling}: stays: parent--z, which stays, links into it', out)

    def test_siblings_linking_in_a_cycle_stay(self):
        a, b = self.sibling('a'), self.sibling('b')
        for one, other in ((a, b), (b, a)):
            (one / 'build').mkdir()
            (one / 'build' / 'peer').symlink_to(other)
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 4, out)
        self.assertIn(f'{a}: stays: links with parent--b in a cycle', out)
        self.assertIn(f'{b}: stays: links with parent--a in a cycle', out)

    def test_a_list_that_changed_since_the_token_stops_again(self):
        rc, out = self.run_remove('--pending', 'file the flaky test')
        self.assertEqual(rc, 4, out)
        self.assertIn('file the flaky test', out)
        token = self.token(out)
        rc, out = self.run_remove('--pending', 'file the flaky test', '--pending', 'rerun HIL', '--proceed', token)
        self.assertEqual(rc, 4, out)
        self.assertEqual(self.removals(), [])
        rc, out = self.run_remove('--pending', 'file the flaky test', '--proceed', token)
        self.assertEqual(rc, 0, out)

    def test_github_pr_state_is_pending(self):
        prs = {'parent': [dict(number=3, head='x', state='MERGED', merged_at='2026-01-01'),
                          dict(number=5, head='y', state='MERGED', merged_at='2026-02-01')],
               'open': [dict(number=8, head='z', state='OPEN', merged_at=None)], 'lone': [],
               'moved': [dict(number=9, head='old', state='CLOSED', merged_at=None)]}
        threads = {5: ['https://github.com/o/r/pull/5#discussion_r1'], 3: ['https://github.com/o/r/pull/3#old']}
        self.sibling('open')
        lone = self.sibling('lone', push=False)
        self.commit(lone, 'l.txt', 'l')
        sh(lone, 'push', '-q', 'origin', 'lone')
        moved = self.sibling('moved')
        self.commit(moved, 'm.txt', 'm')
        sh(moved, 'push', '-q', 'origin', 'moved')
        with mock.patch.object(wr.wc, 'github_repo', lambda common, remote: 'o/r'), \
                mock.patch.object(wr.wc, 'pulls', lambda repo, branch, owner=None: prs[branch]), \
                mock.patch.object(wr, 'fork_parent', lambda repo: None), \
                mock.patch.object(wr, 'unresolved_threads', lambda repo, n: threads[n]):
            rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('parent: unresolved review thread https://github.com/o/r/pull/5#discussion_r1', out)
        self.assertNotIn('#old', out)
        self.assertIn('open: open PR #8', out)
        self.assertIn('lone: commits on origin that master lacks, and no PR has them as its head', out)
        self.assertIn('moved: commits on origin that master lacks, and no PR has them', out)
        self.assertNotIn('open: commits', out)

    def test_a_fork_branch_is_gated_on_its_prs_in_the_parent_repository(self):
        sh(self.parent, 'push', '-q', 'origin', 'parent:master')  # merged upstream, then the fork synced
        search = {'me/r': [], 'up/r': [
            dict(number=7, state='MERGED', headRefName='parent', headRefOid='x', mergedAt='2026-01-01',
                 headRepositoryOwner={'login': 'me'}),
            dict(number=6, state='OPEN', headRefName='parent', headRefOid='y', mergedAt=None,
                 headRepositoryOwner={'login': 'someone'}),
            dict(number=5, state='OPEN', headRefName='parent-2', headRefOid='z', mergedAt=None,
                 headRepositoryOwner={'login': 'me'})]}

        def gh(*args):
            if args[:2] == ('repo', 'view'):
                fork = args[2] == 'me/r'
                return [json.dumps({'isFork': fork, 'parent': {'name': 'r', 'owner': {'login': 'up'}} if fork else None})]
            query = ' '.join(args)
            if 'reviewThreads' in query:
                return ['https://github.com/up/r/pull/7#discussion_r1'] if 'owner=up' in query and 'number=7' in query else []
            nodes = search[re.search(r'q=repo:(\S+)', query).group(1)]
            return [json.dumps({'issueCount': len(nodes), 'nodes': nodes})]

        with mock.patch.object(wr.wc, 'github_repo', lambda common, remote: 'me/r'), \
                mock.patch.object(wr.wc, 'gh_lines', gh):
            rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('parent: unresolved review thread https://github.com/up/r/pull/7#discussion_r1', out)
        self.assertNotIn('open PR', out)
        search['up/r'].append(dict(number=4))  # a malformed or short answer refuses, as any failed lookup
        wr.wc.pulls.cache_clear()
        with mock.patch.object(wr.wc, 'github_repo', lambda common, remote: 'me/r'), \
                mock.patch.object(wr.wc, 'gh_lines', gh):
            self.assertRefused('PR search of up/r for head:parent')

    def test_same_named_branches_from_other_owners_are_not_gated_on(self):
        nodes = [dict(number=9, state='MERGED', headRefName='parent', headRefOid='x', mergedAt='2026-03-01',
                      headRepositoryOwner={'login': 'stranger'}),
                 dict(number=8, state='OPEN', headRefName='parent', headRefOid='y', mergedAt=None,
                      headRepositoryOwner={'login': 'stranger'}),
                 dict(number=7, state='MERGED', headRefName='parent', headRefOid='z', mergedAt='2026-01-01',
                      headRepositoryOwner={'login': 'o'})]

        def gh(*args):
            if args[:2] == ('repo', 'view'):
                return [json.dumps({'isFork': False, 'parent': None})]
            query = ' '.join(args)
            if 'reviewThreads' in query:
                number = re.search(r'number=(\d+)', query).group(1)
                return [f'https://github.com/o/r/pull/{number}#discussion_r1']
            return [json.dumps({'issueCount': len(nodes), 'nodes': nodes})]

        with mock.patch.object(wr.wc, 'github_repo', lambda common, remote: 'o/r'), \
                mock.patch.object(wr.wc, 'gh_lines', gh):
            rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('parent: unresolved review thread https://github.com/o/r/pull/7#discussion_r1', out)
        self.assertNotIn('pull/9', out)
        self.assertNotIn('open PR', out)

    # --- run --------------------------------------------------------------------------

    def test_a_dry_run_reports_and_changes_nothing(self):
        sibling = self.sibling('b')
        target = self.t / 'deps'
        target.mkdir()
        (self.parent / 'build').mkdir()
        (self.parent / 'build' / 'deps').symlink_to(target)
        (sibling / 'build').mkdir()
        (sibling / 'build' / 'inner').symlink_to(sibling / '.gitignore')
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertIn(f'{self.parent}: herdr removes it; branch parent goes: on origin', out)
        self.assertIn(f'{sibling}: the plugin removes it; branch b goes: on origin', out)
        self.assertIn(f'{self.parent}/build/deps -> {target}', out)
        self.assertNotIn('inner', out)
        self.assertIn(f'Plan to remove {self.parent}', out)
        self.assertNotRegex(out, 'requested|has the result')
        self.assertEqual(self.removals(), [])
        self.assertFalse(self.reports.exists())
        self.assertTrue(sibling.is_dir() and sh(self.main, 'branch', '--list', 'b'))

    def test_the_run_writes_the_report_then_asks_herdr_to_remove(self):
        rc, out = self.run_remove()
        self.assertEqual(rc, 0, out)
        [call] = self.removals()
        self.assertEqual(call['argv'], ['worktree', 'remove', '--workspace', 'w1'])
        [report] = call['reports']
        self.assertIn(f'report: {self.reports / report}', out)
        self.assertIn('Command: `', (self.reports / report).read_text())
        self.assertTrue(self.parent.is_dir(), 'herdr removes it, not the script')

    def test_a_parent_with_submodules_is_removed_with_force(self):
        sub = self.repo('sub')
        sh(self.parent, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(self.t / 'sub.git'), 'lib/sub')
        sh(self.parent, 'commit', '-qm', 'sub')
        sh(self.parent, 'push', '-q', 'origin', 'parent', 'parent:master')
        rc, out = self.run_remove()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.removals()[0]['argv'][-1], '--force')
        self.assertTrue(sub.is_dir())

    def test_a_failed_removal_is_exit_5(self):
        self.mode.write_text('fail')
        rc, out = self.run_remove()
        self.assertEqual(rc, 5, out)
        self.assertIn('worktree_remove_failed', out)

    def test_a_worktree_herdr_opens_or_loses_sight_of_after_the_plan_is_refused(self):
        for later, text in (('', f'{self.parent} is now open in herdr workspace w1'),
                            ('down', f'herdr and {self.parent}: herdr could not list it '
                                     '(exit 1: herdr server unreachable)')):
            with self.subTest(later=later):
                self.mode.write_text('closed')  # open in no workspace while the plan looks it up
                with self.after_plan(lambda: self.mode.write_text(later)):
                    rc, out = self.run_remove()
                self.assertEqual(rc, 3, out)
                self.assertIn(f'refused, nothing changed: {text}', out)
                self.assertEqual(self.removals(), [])
                self.assertFalse(self.reports.exists())
                self.assertTrue(self.parent.is_dir())
                self.assertIn(str(self.parent), sh(self.main, 'worktree', 'list'))


class NoHerdr(Rig):
    """No herdr: none at HERDR_BIN_PATH, HERDR_ENV unset."""
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {'HERDR_BIN_PATH': str(self.t / 'no-herdr'), 'HERDR_ENV': ''})
        patch.start()
        self.addCleanup(patch.stop)
        wr.wc = wr.load_plugin()

    def test_git_removes_it_then_the_plugins_cleanup_its_siblings_and_branches(self):
        self.assert_removed_by_git()

    def test_a_dry_run_names_the_git_removal_and_changes_nothing(self):
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertIn('open in no herdr workspace', out)
        self.assertIn(f"worktree remove {self.parent}`, then the plugin's cleanup", out)
        self.assertTrue(self.parent.is_dir())
        self.assertFalse(self.reports.exists())

    def test_herdr_env_without_a_herdr_is_refused(self):
        with mock.patch.dict(os.environ, {'HERDR_ENV': '1'}):
            self.assertRefused('HERDR_ENV is set')

    def test_a_working_directory_inside_the_worktree_or_any_sibling_is_refused(self):
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        dirty = self.sibling('dirty')  # kept now, but cleanup removes it should it turn clean meanwhile
        (dirty / 'wip.txt').write_text('wip')
        for inside in (self.parent, self.sibling('b') / 'build', dirty):
            inside.mkdir(exist_ok=True)
            os.chdir(inside)
            self.assertRefused('run this from outside them')
        os.chdir(self.main)
        rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('parent--dirty stays: uncommitted or untracked changes', out)

    def test_a_branch_switched_after_the_gate_is_refused(self):
        with self.after_plan(lambda: sh(self.parent, 'switch', '-q', '-c', 'other')):
            self.assertRefused('its branch or tip moved since the gate')

    def test_a_parent_with_submodules_is_removed_by_git_with_force(self):
        sub = self.repo('sub')
        sh(self.parent, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(self.t / 'sub.git'), 'lib/sub')
        sh(self.parent, 'commit', '-qm', 'sub')
        sh(self.parent, 'push', '-q', 'origin', 'parent', 'parent:master')
        rc, out = self.run_remove()
        self.assertEqual(rc, 0, out)
        self.assertIn('worktree remove --force', out)
        self.assertFalse(self.parent.exists())
        self.assertTrue(sub.is_dir())

    def test_a_failed_git_removal_is_exit_5_and_cleans_up_nothing(self):
        sibling = self.sibling('b')
        failed = wr.wc.Keep('`git worktree remove` failed, possibly part way: boom')
        with mock.patch.object(wr.wc, 'remove', side_effect=failed):
            rc, out = self.run_remove()
        self.assertEqual(rc, 5, out)
        self.assertIn(f'worktree_remove: {self.parent}: `git worktree remove` failed, possibly part way: boom', out)
        self.assertTrue(self.parent.is_dir() and sibling.is_dir())
        self.assertTrue(sh(self.main, 'branch', '--list', 'parent', 'b'))


if __name__ == '__main__':
    unittest.main()
