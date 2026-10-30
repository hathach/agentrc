"""Tests for worktree-remove's worktree_remove.py on real git repos: a bare origin, a primary checkout,
a worktree `wt/parent` and its `parent--*` siblings; gh is a stub."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, make_stub  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'worktree-remove' / 'scripts' / 'worktree_remove.py'
spec = importlib.util.spec_from_file_location('worktree_remove', SCRIPT)
wr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wr)

posix = unittest.skipIf(os.name == 'nt', 'POSIX paths and stub executables')


def sh(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def setUpModule():
    guard_survivors(__name__)


class Rig(unittest.TestCase):
    """The repositories and the gh stub."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name).resolve()
        cfg = self.t / 'gitconfig'
        cfg.write_text('[user]\n name = t\n email = t@t\n[init]\n defaultBranch = master\n'
                       '[protocol "file"]\n allow = always\n')
        bin_dir = self.t / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'gh').write_text('#!/bin/sh\necho "gh: network unreachable" >&2\nexit 1\n')
        (bin_dir / 'gh').chmod(0o755)
        self.bin_dir, self.reports = bin_dir, self.t / 'state' / 'agentrc' / 'worktree-remove'
        self.wt = self.t / 'wt'
        self.parent = self.wt / 'parent'
        env = git_env(GIT_CONFIG_GLOBAL=str(cfg), PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                      XDG_STATE_HOME=str(self.t / 'state'))
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        wr.wc = wr.load_module()  # fresh: it reads the environment, the log's place included, and caches fetches
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

    def tips(self, *branches):
        return {b: sh(self.main, 'rev-parse', '--verify', '--quiet', f'refs/heads/{b}') for b in branches}

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
        self.assertFalse(self.reports.exists())
        self.assertTrue((self.parent / '.git').exists())

    def assertCleanupKept(self, rc, out):
        self.assertEqual(rc, 6, out)
        self.assertIn(f'worktree_remove: removed {self.parent}, but the cleanup kept a sibling or branch', out)
        self.assertFalse(self.parent.exists())


class Remove(Rig):
    # --- refused --------------------------------------------------------------------

    def test_the_primary_checkout_is_refused(self):
        self.parent = self.main
        self.assertRefused('a primary checkout')

    def test_a_parent_holding_work_is_refused(self):
        (self.parent / 'new.txt').write_text('x')
        self.assertRefused('uncommitted or untracked changes')

    @posix
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

    def test_a_log_and_its_reports_inside_the_worktree_or_any_sibling_are_refused(self):
        sibling, dirty = self.sibling('b'), self.sibling('dirty')
        (dirty / 'wip.txt').write_text('wip')  # kept: the cleanup removes it should it turn clean meanwhile
        for tree in (self.parent, sibling, dirty):
            log = tree / 'build' / 'reports' / 'log'
            with self.subTest(tree=tree.name), mock.patch.object(wr.wc, 'LOG', log):
                self.assertRefused(f'the log {log} lies inside {tree}; run this from outside it')

    @posix
    def test_a_log_reached_through_a_link_into_the_worktree_is_refused(self):
        state = self.t / 'log-state'
        state.symlink_to(self.parent / 'build')
        with mock.patch.object(wr.wc, 'LOG', state / 'log'):
            self.assertRefused(f'the log {state / "log"} lies inside {self.parent}')

    @posix
    def test_a_log_climbing_out_of_a_link_into_the_worktree_is_refused(self):
        (self.t / 'home').mkdir()
        (self.parent / 'build').mkdir()
        (self.t / 'home' / 'alias').symlink_to(self.parent / 'build')  # dangling once PATH goes: no mkdir past it
        log = self.t / 'home' / 'alias' / '..' / '..' / 'state' / 'log'
        with mock.patch.object(wr.wc, 'LOG', log):
            self.assertRefused(f'the log {log} lies inside {self.parent}')

    @posix
    def test_a_log_under_a_link_in_the_worktree_out_of_it_is_refused(self):
        elsewhere = self.t / 'elsewhere'
        elsewhere.mkdir()
        (self.parent / 'build').mkdir()
        (self.parent / 'build' / 'state').symlink_to(elsewhere)  # git would delete the link, the log recreate it
        log = self.parent / 'build' / 'state' / 'log'
        unchecked = mock.patch.object(wr.wc, 'content_check', side_effect=AssertionError('content_check ran'))
        with unchecked, mock.patch.object(wr.wc, 'LOG', log):
            self.assertRefused(f'the log {log} lies inside {self.parent}; run this from outside it')

    def test_the_inside_refusal_comes_before_any_content_check(self):
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        sibling = self.sibling('b')
        unchecked = mock.patch.object(wr.wc, 'content_check', side_effect=AssertionError('content_check ran'))
        with unchecked, mock.patch.object(wr.wc, 'LOG', sibling / 'build' / 'log'):
            self.assertRefused(f'the log {sibling / "build" / "log"} lies inside {sibling}')
        os.chdir(self.parent)
        with unchecked:
            self.assertRefused(f'the working directory {self.parent} lies inside {self.parent}')

    def test_a_tree_changing_after_its_check_is_refused(self):
        self.sibling('b')
        for err in (wr.wc.Keep('cannot list /x: Permission denied'), FileNotFoundError(2, 'gone', '/x/link')):
            with self.subTest(err=err), mock.patch.object(wr, 'links_leaving', side_effect=err):
                self.assertRefused('listing the symlinks leaving the removed trees')

    @posix
    def test_a_failed_github_lookup_is_refused(self):
        with mock.patch.object(wr.wc, 'GITHUB', re.compile('')):
            self.assertRefused('network unreachable')

    def test_a_working_directory_inside_the_worktree_or_any_sibling_is_refused(self):
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        dirty = self.sibling('dirty')  # kept now, but cleanup removes it should it turn clean meanwhile
        (dirty / 'wip.txt').write_text('wip')
        sibling = self.sibling('b')
        for inside, tree in ((self.parent, self.parent), (sibling / 'build', sibling), (dirty, dirty)):
            inside.mkdir(exist_ok=True)
            os.chdir(inside)
            self.assertRefused(f'the working directory {inside} lies inside {tree}; run this from outside it')
        os.chdir(self.main)
        rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('parent--dirty stays: uncommitted or untracked changes', out)

    def assertJunctionRefusedOrKept(self, junction):
        """junction(path) makes a junction at path: one in PATH refuses it, one in a sibling keeps the sibling."""
        sibling = self.sibling('b')
        for tree in (self.parent, sibling):
            (tree / 'build').mkdir()
            junction(tree / 'build' / 'dep')
        why = 'holds a junction at {}, which `git worktree remove` leaves in place: remove the junction first'
        self.assertRefused(f'{self.parent}: ' + why.format(self.parent / 'build' / 'dep'))
        os.rmdir(self.parent / 'build' / 'dep')  # the junction itself, not its target
        rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('parent--b stays: ' + why.format(sibling / 'build' / 'dep'), out)

    def test_a_junction_in_the_worktree_is_refused_and_one_in_a_sibling_keeps_it(self):
        scandir, isjunction = os.scandir, os.path.isjunction

        class Entry:  # a directory named dep reports itself a junction, as on Windows
            def __init__(self, entry):
                self.entry = entry

            def __getattr__(self, name):
                return getattr(self.entry, name)

            def is_junction(self):
                return self.entry.name == 'dep' or self.entry.is_junction()

        @contextmanager
        def junctions(path='.'):
            with scandir(path) as listing:
                yield (Entry(entry) for entry in listing)

        with mock.patch('os.scandir', junctions), \
                mock.patch('os.path.isjunction', lambda p: os.path.basename(p) == 'dep' or isjunction(p)):
            self.assertJunctionRefusedOrKept(os.mkdir)

    @unittest.skipUnless(os.name == 'nt', 'Windows junctions')
    def test_a_real_junction_in_the_worktree_is_refused_and_one_in_a_sibling_keeps_it(self):
        import _winapi
        target = self.t / 'elsewhere'
        target.mkdir()
        (target / 'keep').write_text('shared target')
        self.assertJunctionRefusedOrKept(lambda path: _winapi.CreateJunction(str(target), str(path)))
        self.assertEqual((target / 'keep').read_text(), 'shared target')

    def test_a_branch_switched_after_the_gate_is_refused(self):
        with self.after_plan(lambda: sh(self.parent, 'switch', '-q', '-c', 'other')):
            self.assertRefused('its branch or tip moved since the gate')

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
        self.assertTrue(self.parent.is_dir())
        self.assertFalse(self.reports.exists())
        rc, out = self.run_remove('--proceed', self.token(out))
        self.assertCleanupKept(rc, out)
        self.assertTrue((dirty / 'wip.txt').is_file())
        self.assertTrue(sh(self.main, 'branch', '--list', 'local'))

    @posix
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
        self.assertNotIn('the cleanup removes it', out)

    @posix
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

    @posix
    def test_a_staying_siblings_link_through_a_sibling_and_back_out_holds_both(self):
        passed, target = self.sibling('a'), self.sibling('b')
        owner = self.sibling('z')
        (owner / 'use').symlink_to(f'{passed}/../{target.name}')
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 4, out)
        for sibling in (passed, target):
            self.assertIn(f'{sibling}: stays: parent--z, which stays, links into it', out)

    @posix
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
        self.assertTrue(self.parent.is_dir())
        self.assertFalse(self.reports.exists())
        rc, out = self.run_remove('--pending', 'file the flaky test', '--proceed', token)
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.parent.exists())

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

    @posix
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
        self.assertIn(f'{self.parent}: git removes it; branch parent goes: on origin', out)
        self.assertIn(f'{sibling}: the cleanup removes it; branch b goes: on origin', out)
        self.assertIn(f'{self.parent}/build/deps -> {target}', out)
        self.assertNotIn('inner', out)
        self.assertIn(f'Plan to remove {self.parent}', out)
        self.assertNotRegex(out, 'requested|has the result')
        self.assertFalse(self.reports.exists())
        self.assertTrue(self.parent.is_dir() and sibling.is_dir() and sh(self.main, 'branch', '--list', 'b'))

    def test_a_dry_run_names_the_git_removal_and_changes_nothing(self):
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertIn(f'worktree remove {self.parent}`, then the cleanup of its siblings and branches', out)
        self.assertTrue(self.parent.is_dir())
        self.assertFalse(self.reports.exists())

    def test_git_removes_it_then_the_cleanup_its_siblings_and_branches(self):
        sibling = self.sibling('b')
        remove, reports = wr.wc.remove, []

        def after_the_report(child, common, force):
            reports.append(sorted(r.name for r in self.reports.glob('*.md')))
            remove(child, common, force)

        with mock.patch.object(wr.wc, 'remove', after_the_report):
            rc, out = self.run_remove()
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.parent.exists() or sibling.exists())
        self.assertEqual(sh(self.main, 'branch', '--list', 'parent', 'b'), '')
        [report] = [r for r in self.reports.iterdir() if r.suffix == '.md']
        self.assertEqual(reports[0], [report.name], 'the report is written before git removes the worktree')
        self.assertIn(f'report: {report}', out)
        self.assertIn(f'{self.parent}: git removes it; branch parent goes: on origin', report.read_text())
        log = (self.reports / 'log').read_text(encoding='utf-8')
        for line in (f'removed {self.parent}', f'removed {sibling}', 'deleted branch parent', 'deleted branch b'):
            self.assertIn(line, log)
            self.assertIn(line, out)
        self.assertNotIn('retry:', log)

    def test_a_sibling_the_cleanup_keeps_is_exit_6(self):
        sibling, dirty = self.sibling('b'), self.sibling('dirty')
        (dirty / 'wip.txt').write_text('wip')
        rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        rc, out = self.run_remove('--proceed', self.token(out))
        self.assertCleanupKept(rc, out)
        self.assertFalse(sibling.exists())
        self.assertTrue((dirty / 'wip.txt').is_file())
        self.assertEqual(sh(self.main, 'branch', '--list', 'parent', 'b'), '')
        self.assertTrue(sh(self.main, 'branch', '--list', 'dirty'))
        log = (self.reports / 'log').read_text(encoding='utf-8')
        for line in (f'removed {sibling}', f'kept {dirty}: uncommitted or untracked changes', 'retry: '):
            self.assertIn(line, log)

    def test_a_branch_the_cleanup_keeps_is_exit_6(self):
        self.commit(self.parent, 'p.txt', 'p')  # on no origin branch
        rc, out = self.run_remove()
        self.assertEqual(rc, 4, out)
        self.assertIn('branch parent stays', out)
        rc, out = self.run_remove('--proceed', self.token(out))
        self.assertCleanupKept(rc, out)
        self.assertTrue(sh(self.main, 'branch', '--list', 'parent'))
        self.assertIn('kept branch parent', (self.reports / 'log').read_text(encoding='utf-8'))

    def test_something_appearing_at_the_path_during_the_cleanup_is_exit_6_and_cleans_up_nothing(self):
        sibling = self.sibling('b')
        tips = self.tips('parent', 'b')
        remove = wr.wc.remove

        def then_replaced(child, common, force):
            remove(child, common, force)
            Path(child).mkdir()

        with mock.patch.object(wr.wc, 'remove', then_replaced):
            rc, out = self.run_remove()
        self.assertEqual(rc, 6, out)
        self.assertIn(f'worktree_remove: removed {self.parent}, but something appeared at that path again', out)
        self.assertTrue(sibling.is_dir())
        self.assertEqual(self.tips('parent', 'b'), tips)
        log = (self.reports / 'log').read_text(encoding='utf-8')
        self.assertIn(f'{self.parent} exists: not touching its children', log)
        self.assertIn('retry: ', log)

    def test_a_worktree_name_a_narrow_code_page_cannot_encode_is_removed(self):
        self.parent = self.wt / 'parent-\u4e2d'
        sh(self.main, 'worktree', 'add', '-q', str(self.parent), '-b', 'zh')
        sh(self.parent, 'push', '-q', 'origin', 'zh')
        # cp1252 stands in for a Windows code page; the C locale makes the POSIX default encoding narrow too
        env = {**os.environ, 'PYTHONIOENCODING': 'cp1252:strict', 'LC_ALL': 'C', 'PYTHONCOERCECLOCALE': '0',
               'PYTHONUTF8': '0'}
        done = subprocess.run([sys.executable, '-X', 'utf8=0', str(SCRIPT), str(self.parent)], env=env,
                              capture_output=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.parent.exists())
        self.assertIn(b'report: ', done.stdout)
        [report] = self.reports.glob('*.md')
        self.assertIn(f'{self.parent}: git removes it', report.read_text(encoding='utf-8'))

    def test_a_herdr_on_path_or_in_the_environment_is_never_asked(self):
        asked = self.t / 'herdr-asked'
        stub = make_stub(self.bin_dir, 'herdr', f'import sys\nopen({str(asked)!r}, "a").write(" ".join(sys.argv))\n'
                                                'sys.exit("herdr: should not run")\n')
        with mock.patch.dict(os.environ, {'HERDR_ENV': '1', 'HERDR_BIN_PATH': str(stub)}):
            wr.wc = wr.load_module()
            rc, out = self.run_remove()
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.parent.exists())
        self.assertFalse(asked.exists(), asked.exists() and asked.read_text())

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
        tips = self.tips('parent', 'b')
        failed = wr.wc.Keep('`git worktree remove` failed, possibly part way: boom')
        with mock.patch.object(wr.wc, 'remove', side_effect=failed):
            rc, out = self.run_remove()
        self.assertEqual(rc, 5, out)
        self.assertIn(f'worktree_remove: {self.parent}: `git worktree remove` failed, possibly part way: boom', out)
        self.assertTrue(self.parent.is_dir() and sibling.is_dir())
        self.assertEqual(self.tips('parent', 'b'), tips)


if __name__ == '__main__':
    unittest.main()
