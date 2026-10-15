"""Tests for worktree-remove's worktree_remove.py on real git repos: a bare origin, a primary checkout,
a herdr worktree `wt/parent` and its `parent--*` siblings; herdr and gh are stubs."""
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


@unittest.skipIf(os.name == 'nt', 'POSIX paths and stub executables')
class Remove(unittest.TestCase):
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

    # --- refused --------------------------------------------------------------------

    def assertRefused(self, text, *argv):
        rc, out = self.run_remove(*argv)
        self.assertEqual(rc, 3, out)
        self.assertIn(text, out)
        self.assertEqual(self.removals(), [])
        self.assertFalse(self.reports.exists())

    def test_the_primary_checkout_is_refused(self):
        self.parent = self.main
        self.assertRefused('a primary checkout')

    def test_a_worktree_herdr_has_not_open_is_refused_with_the_way_to_open_it(self):
        self.mode.write_text('closed')
        self.assertRefused(f'herdr worktree open --path {self.parent}')

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
        self.assertRefused(f'pid {chief.pid}')
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
                mock.patch.object(wr.wc, 'pulls', lambda repo, branch: prs[branch]), \
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
        with mock.patch.object(wr.wc, 'github_repo', lambda common, remote: 'me/r'), \
                mock.patch.object(wr.wc, 'gh_lines', gh):
            self.assertRefused('PR search of up/r for head:parent')

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
        self.assertIn(f'{self.parent}: removed by herdr; branch parent goes: on origin', out)
        self.assertIn(f'{sibling}: removed by the plugin; branch b goes: on origin', out)
        self.assertIn(f'{self.parent}/build/deps -> {target}', out)
        self.assertNotIn('inner', out)
        self.assertIn('removal requested; the plugin log has the result', out)
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


if __name__ == '__main__':
    unittest.main()
