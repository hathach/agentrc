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
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, make_stub  # noqa: E402
from test_worktree_children import HC, hc_inventory, without_hc  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'worktree-remove' / 'scripts' / 'worktree_remove.py'
spec = importlib.util.spec_from_file_location('worktree_remove', SCRIPT)
wr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wr)

posix = unittest.skipIf(os.name == 'nt', 'POSIX paths and stub executables')
linux = unittest.skipUnless(Path('/proc/self/cwd').exists(), 'processes listed by /proc')


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
        self.hc_stub, self.hc = make_stub(bin_dir, 'hc', HC), self.t / 'hc.json'
        self.bin_dir, self.reports = bin_dir, self.t / 'state' / 'agentrc' / 'worktree-remove'
        self.wt = self.t / 'wt'
        self.parent = self.wt / 'parent'
        env = git_env(GIT_CONFIG_GLOBAL=str(cfg), PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                      XDG_STATE_HOME=str(self.t / 'state'), STUB_HC=str(self.hc))
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        wr.wc = wr.load_module()  # fresh: it reads the environment, the log's place included, and caches fetches
        self.main = self.repo('main')
        self.wt.mkdir()
        sh(self.main, 'worktree', 'add', '-q', str(self.parent), '-b', 'parent')
        sh(self.parent, 'push', '-q', 'origin', 'parent')

    def patch(self, target, name, value):
        patch = mock.patch.object(target, name, value)
        patch.start()
        self.addCleanup(patch.stop)

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

    def moving_in_during_check_repo(self, after=1):
        """A process starts working in the worktree during the after-th check_repo of it, past the content
        check's look at its processes; the list holds it once started."""
        check, seen, holder = wr.wc.check_repo, [], []

        def then_moved_in(tree):
            if Path(tree) == self.parent:
                seen.append(tree)
                if len(seen) == after:
                    holder.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=tree))
                    self.addCleanup(holder[0].wait)
                    self.addCleanup(holder[0].kill)
            return check(tree)

        self.patch(wr.wc, 'check_repo', then_moved_in)
        return holder

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

    def test_a_herdr_core_terminal_in_the_worktree_or_any_sibling_is_refused(self):
        sibling, dirty = self.sibling('b'), self.sibling('dirty')
        (dirty / 'wip.txt').write_text('wip')  # kept: the cleanup removes it should it turn clean meanwhile
        hc_inventory(self.hc, [{'address': 't5', 'cwd': str(self.t)}], {'t5': self.parent})
        self.assertRefused(f'herdr-core terminals work there: t5 (in no herdr-app space) in {self.parent}; '
                           'close them, then retry')
        for tree in (sibling, dirty):
            with self.subTest(tree=tree.name):
                hc_inventory(self.hc, [{'address': 't7', 'name': 'rev', 'cwd': str(tree), 'where': 'parent / 1'}])
                self.assertRefused(f'herdr-core terminals work there: t7 rev (parent / 1) in {tree}')

    def test_an_hc_that_cannot_list_its_terminals_is_refused(self):
        hc_inventory(self.hc, [], raw={'ls': [1, '', 'hc: no core']})
        self.assertRefused('cannot list herdr-core terminals: `hc ls` exit 1: hc: no core; retry once `hc ls` answers')

    def test_an_hc_that_does_not_answer_is_refused(self):
        hc_inventory(self.hc, [], raw={'ls': [0, '{"terminals": []}', '', 5]})
        with mock.patch.object(wr.wc, 'HC_TIMEOUT', 0.5):
            self.assertRefused('cannot list herdr-core terminals: `hc ls` exit 124: no answer in 0.5 s')

    def test_a_terminal_arriving_during_the_last_content_check_refuses_the_worktree(self):
        check = wr.wc.content_check

        def then_moved_in(child, *spared):
            result = check(child, *spared)
            hc_inventory(self.hc, [{'address': 't5', 'cwd': str(self.parent)}])
            return result

        def arm():
            patch = mock.patch.object(wr.wc, 'content_check', then_moved_in)
            patch.start()
            self.addCleanup(patch.stop)

        with self.after_plan(arm):
            rc, out = self.run_remove()
        self.assertEqual(rc, 3, out)
        self.assertIn(f'{self.parent} changed: herdr-core terminals work there: t5', out)
        self.assertTrue((self.parent / '.git').exists())
        self.assertFalse(self.reports.exists())

    @linux
    def test_a_process_arriving_during_the_last_content_check_refuses_the_worktree(self):
        holder = self.moving_in_during_check_repo(after=2)  # the plan's check, then the last one
        rc, out = self.run_remove()
        self.assertEqual(rc, 3, out)
        self.assertIn(f'{self.parent} changed: a process works in it: ', out)
        self.assertIn(f'(pid {holder[0].pid})', out)
        self.assertTrue((self.parent / '.git').exists())
        self.assertFalse(self.reports.exists())

    def test_a_terminal_arriving_after_the_gate_refuses_the_worktree_or_keeps_its_sibling(self):
        sibling = self.sibling('b')
        for tree, rc in ((self.parent, 3), (sibling, 6)):
            with self.subTest(tree=tree.name):
                with self.after_plan(lambda: hc_inventory(self.hc, [{'address': 't5', 'cwd': str(tree)}])):
                    got, out = self.run_remove()
                self.assertEqual(got, rc, out)
                self.assertIn(f'herdr-core terminals work there: t5 (in no herdr-app space) in {tree}', out)
                self.assertTrue(sibling.is_dir())
                self.hc.unlink()
        self.assertFalse(self.parent.exists())

    @linux
    def test_a_process_working_in_the_worktree_or_a_sibling_refuses_it_or_keeps_the_sibling(self):
        sibling = self.sibling('b')
        for tree in (self.parent, sibling):
            holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=tree)
            self.addCleanup(holder.wait)
            self.addCleanup(holder.kill)
            rc, out = self.run_remove('--dry-run')
            self.assertEqual(rc, 3 if tree == self.parent else 4, out)
            fate = f'{self.parent}: ' if tree == self.parent else f'- {sibling}: stays: '
            self.assertRegex(out, rf'{re.escape(fate)}a process works in it: .*\(pid {holder.pid}\)')
            holder.kill()
            holder.wait()

    def test_no_proc_is_reported_unchecked(self):
        wr.wc = wr.wc or wr.load_module()
        with mock.patch.object(wr.wc, 'PROC', self.t / 'no-proc'):
            rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertIn('- other processes working in them: not checked, no /proc on this system', out)

    def test_no_hc_installed_is_reported_unchecked(self):
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('herdr-core terminals', out)
        with mock.patch.dict(os.environ, {'PATH': without_hc(os.environ['PATH'], self.hc_stub)}):
            wr.wc = wr.load_module()
            hc_inventory(self.hc, [{'address': 't5', 'cwd': str(self.parent)}])  # no hc answers it
            rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertIn('- herdr-core terminals working in them: not checked, no hc installed', out)

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



class FakeHc:
    """hc for the handoff: terminals by address, each {address, cwd, pid, created_ms} backed by a real sleeping
    process; `hc run` starts the worker in a thread unless start is False; `hc kill` ends a terminal's process."""

    def __init__(self, test, cwds, start=True):
        self.test, self.start, self.calls, self.threads, self.procs = test, start, [], [], {}
        self.terminals = {}
        for address, cwd in cwds.items():
            proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=cwd)
            test.addCleanup(proc.wait)
            test.addCleanup(proc.kill)
            self.procs[address] = proc
            self.terminals[address] = dict(address=address, cwd=str(cwd), state='live', pid=proc.pid, created_ms=1)
        self.real_run = wr.wc.run
        self.on_kill = lambda address: None

    def json(self, hc, *args):
        if args[0] == 'ls':
            return {'terminals': [{k: t[k] for k in ('address', 'cwd', 'state')} for t in self.terminals.values()]}
        t = self.terminals.get(args[1])
        return dict(t, foreground=None) if t else None

    def run(self, argv, cwd=None, input=None, timeout=None):
        if argv[0] != wr.wc.hc_path():
            return self.real_run(argv, cwd=cwd, input=input, timeout=timeout)
        self.calls.append(argv[1:])
        if argv[1] == 'run' and self.start:
            name, worker = argv[argv.index('--name') + 1], argv[argv.index('--') + 1:]
            self.terminals[name] = dict(address=name, cwd=argv[argv.index('--cwd') + 1], state='live', pid=1,
                                        created_ms=1)

            def work():
                try:
                    wr.worker(worker[-1])
                finally:
                    self.terminals.pop(name, None)  # --rm
            thread = threading.Thread(target=work)
            thread.start()
            self.threads.append(thread)
        elif argv[1] == 'kill' and argv[2] in self.procs:
            self.on_kill(argv[2])
            self.procs[argv[2]].kill()
            self.procs[argv[2]].wait()
            self.terminals[argv[2]]['state'] = 'finished'
        return subprocess.CompletedProcess(argv, 0, '', '')

    def join(self):
        for thread in self.threads:
            thread.join(30)

    def killed(self):
        return [c[1] for c in self.calls if c[0] == 'kill']


@linux
class Handoff(Rig):
    """From an hc terminal, the terminals in the worktree and its siblings close and a worker removes it."""

    def setUp(self):
        super().setUp()
        self.sib = self.sibling('b')
        for name, value in (('READY_S', 5), ('ACK_S', 0.3), ('END_S', 5)):
            self.patch(wr, name, value)

    def fake(self, start=True, own=True):
        hc = FakeHc(self, {'t5': self.parent, 't7': self.sib}, start)
        self.patch(wr.wc, 'hc_json', hc.json)
        self.patch(wr.wc, 'run', hc.run)
        mine = hc.terminals['t5'] if own else None
        self.patch(wr, 'caller_terminal', lambda _: mine)
        return hc

    def admission(self):
        return wr.admission_of(self.parent)

    def test_the_dry_run_lists_the_terminals_it_closes_and_binds_other_sessions(self):
        hc = self.fake()
        rc, out = self.run_remove('--dry-run')
        self.assertEqual(rc, 4, out)
        self.assertIn(f"- closes terminal t5 (in no herdr-app space) in {self.parent} (this session's own)", out)
        t7 = hc.terminals['t7']
        self.assertIn(f'closes terminal t7 (in no herdr-app space) in {self.sib}: pid {t7["pid"]}, created 1', out)
        self.assertNotIn(f'closes terminal t5 (in no herdr-app space) in {self.parent}: pid', out)
        token = self.token(out)
        t7['created_ms'] = 2  # another program in that terminal: another list to agree to
        self.assertNotEqual(self.token(self.run_remove('--dry-run')[1]), token)
        self.assertEqual(hc.killed(), [])

    def test_not_from_hc_the_terminals_refuse_as_before(self):
        self.fake(own=False)
        self.assertRefused('herdr-core terminals work there: t5 (in no herdr-app space)')

    def test_the_caller_is_proved_by_its_ancestry_not_by_its_variable(self):
        got = {'address': 't5', 'state': 'live', 'pid': os.getppid()}
        with mock.patch.object(wr.wc, 'hc_json', lambda *a: dict(got)), \
                mock.patch.dict(os.environ, {'HC_TERMINAL_ID': 't5'}):
            self.assertEqual(wr.caller_terminal('hc')['pid'], os.getppid())
            got['pid'] = 1 << 22  # no ancestor
            self.assertIsNone(wr.caller_terminal('hc'))
        self.assertIsNone(wr.caller_terminal('hc'))  # no variable

    def test_the_handoff_closes_every_terminal_then_removes(self):
        hc = self.fake()
        rc, out = self.run_remove('--proceed', self.token(self.run_remove('--dry-run')[1]))
        hc.join()
        self.assertEqual(rc, 0, out)
        self.assertIn('handed off to hc terminal wtrm-', out)
        self.assertEqual(sorted(hc.killed()), ['t5', 't7'])
        self.assertFalse(self.parent.exists())
        self.assertFalse(self.sib.exists())
        self.assertEqual([f.name for f in self.admission().parent.iterdir()], ['admissions.lock'])
        launch = next(c for c in hc.calls if c[0] == 'run')
        self.assertEqual(launch[:5], ['run', '--bg', '--no-parent', '--no-notify', '--rm'])
        self.assertIn('closing terminal t7', (self.reports / 'log').read_text(encoding='utf-8'))

    def test_a_worker_that_never_starts_closes_nothing_and_admits_again(self):
        hc = self.fake(start=False)
        with mock.patch.object(wr, 'READY_S', 0.3):
            rc, out = self.run_remove('--proceed', self.token(self.run_remove('--dry-run')[1]))
        self.assertEqual(rc, 3, out)
        self.assertIn('did not answer', out)
        self.assertEqual(hc.killed()[-1:], [next(c[c.index('--name') + 1] for c in hc.calls if c[0] == 'run')])
        self.assertNotIn('t5', hc.killed())
        self.assertTrue(self.parent.exists())
        self.assertEqual([f.name for f in self.admission().parent.iterdir()], ['admissions.lock'])

    def prepared(self, op='abc'):
        """A plan and its admission as a caller leaves them before the worker starts."""
        base = self.admission().parent
        base.mkdir(parents=True)
        files = {k: str(base / f'{op}.{k}') for k in ('plan', 'ready', 'ack')}
        self.admission().write_text(json.dumps(dict(op=op, root=str(self.parent), worker=f'wtrm-{op}', caller=1,
                                                    caller_start=0)))
        hc = self.fake()
        closing = [dict(address=a, name=None, where=None, at=hc.terminals[a]['cwd'], pid=hc.terminals[a]['pid'],
                        created_ms=1) for a in ('t5', 't7')]
        Path(files['plan']).write_text(json.dumps(dict(
            op=op, admission=str(self.admission()), root=str(self.parent),
            common=sh(self.parent, 'rev-parse', '--git-common-dir'),
            head=list(wr.wc.head_branch(self.parent)), force=False, closing=closing,
            siblings=[[str(self.sib), wr.wc.common_dir(self.sib / '.git'), list(wr.wc.head_branch(self.sib))]],
            hc=wr.wc.hc_path(),
            log=str(wr.wc.LOG), report='r', **files)))
        return hc, files

    def test_a_caller_gone_before_its_answer_leaves_everything_running(self):
        hc, files = self.prepared()
        self.assertEqual(wr.worker(files['plan']), 3)
        self.assertEqual(hc.killed(), [])
        self.assertTrue(self.parent.exists())
        self.assertFalse(self.admission().exists())
        self.assertIn('the handoff was lapsed; closed nothing', (self.reports / 'log').read_text(encoding='utf-8'))

    def test_a_terminal_running_another_program_than_planned_stops_the_worker(self):
        hc, files = self.prepared()
        Path(files['ack']).write_text('go')
        hc.terminals['t7']['created_ms'] = 2
        self.assertEqual(wr.worker(files['plan']), 3)
        self.assertEqual(hc.killed(), [])
        self.assertTrue(self.parent.exists())
        self.assertIn('another program runs in t7', (self.reports / 'log').read_text(encoding='utf-8'))

    def test_a_terminal_changed_while_another_closes_is_left_running(self):
        hc, files = self.prepared()
        Path(files['ack']).write_text('go')
        hc.on_kill = lambda address: hc.terminals['t7'].update(created_ms=2) if address == 't5' else None
        self.assertEqual(wr.worker(files['plan']), 3)
        self.assertEqual(hc.killed(), ['t5'])
        self.assertTrue(self.parent.exists())

    def test_a_process_arriving_during_the_workers_content_check_stops_it(self):
        hc, files = self.prepared()
        Path(files['ack']).write_text('go')
        holder = self.moving_in_during_check_repo()
        self.assertEqual(wr.worker(files['plan']), 3)
        self.assertTrue((self.parent / '.git').exists())
        self.assertIn(f'(pid {holder[0].pid})', (self.reports / 'log').read_text(encoding='utf-8'))

    def test_the_worker_removes_only_the_siblings_the_plan_approved(self):
        hc, files = self.prepared()
        late = self.sibling('late')  # clean, but found after the human agreed
        Path(files['ack']).write_text('go')
        self.assertEqual(wr.worker(files['plan']), 6)
        self.assertFalse(self.parent.exists())
        self.assertFalse(self.sib.exists())
        self.assertTrue(late.exists())
        self.assertIn(f'kept {late}: not among the siblings the approved plan removes',
                      (self.reports / 'log').read_text(encoding='utf-8'))

    def test_the_worker_keeps_another_checkout_found_at_an_approved_path(self):
        hc, files = self.prepared()
        hc.procs['t7'].kill()
        hc.procs['t7'].wait()
        sh(self.main, 'worktree', 'remove', '--force', str(self.sib))
        other = self.repo('other')
        sh(other, 'worktree', 'add', '-q', str(self.sib), '-b', 'replacement-task')
        sh(self.sib, 'push', '-q', 'origin', 'replacement-task')
        Path(files['ack']).write_text('go')
        self.assertEqual(wr.worker(files['plan']), 6)
        self.assertFalse(self.parent.exists())
        self.assertTrue(self.sib.exists())
        self.assertTrue(sh(other, 'rev-parse', '--verify', '--quiet', 'refs/heads/replacement-task'))
        self.assertIn(f'kept {self.sib}: not the checkout the approved plan found there',
                      (self.reports / 'log').read_text(encoding='utf-8'))

    def test_the_worker_keeps_a_checkout_replaced_during_the_cleanups_checks(self):
        hc, files = self.prepared()
        hc.procs['t7'].kill()
        hc.procs['t7'].wait()
        other, check = self.repo('other'), wr.wc.check_processes

        def then_replaced(tree, *spared):  # the cleanup's last look before removing the sibling passes no spared
            if tree == self.sib and not spared and wr.wc.head_branch(tree)[0] == 'b':
                sh(self.main, 'worktree', 'remove', '--force', str(self.sib))
                sh(other, 'worktree', 'add', '-q', str(self.sib), '-b', 'replacement-task')
                sh(self.sib, 'push', '-q', 'origin', 'replacement-task')
            return check(tree, *spared)

        self.patch(wr.wc, 'check_processes', then_replaced)
        Path(files['ack']).write_text('go')
        self.assertEqual(wr.worker(files['plan']), 6)
        self.assertTrue(self.sib.exists())
        self.assertTrue(sh(other, 'rev-parse', '--verify', '--quiet', 'refs/heads/replacement-task'))
        self.assertIn(f'kept {self.sib}: not the checkout the approved plan found there',
                      (self.reports / 'log').read_text(encoding='utf-8'))

    def test_a_published_file_is_whole_and_never_replaced(self):
        target = self.t / 'ack'
        self.assertTrue(wr.created(target, 'go'))
        self.assertFalse(wr.created(target, 'lapsed'))
        self.assertEqual(target.read_text(), 'go')
        self.assertEqual([p.name for p in self.t.glob('ack*')], ['ack'])

    def test_a_second_removal_of_the_same_tree_is_refused_however_spelled(self):
        hc = self.fake()
        token = self.token(self.run_remove('--dry-run')[1])
        self.admission().parent.mkdir(parents=True)
        self.admission().write_text(json.dumps(dict(op='other', root=str(self.parent), worker='wtrm-other',
                                                    caller=os.getpid(), caller_start=wr.wc.hc.proc_start(os.getpid()))))
        hc.terminals['wtrm-other'] = dict(address='wtrm-other', cwd=str(self.t), state='live', pid=1, created_ms=1)
        rc, out = self.run_remove('--proceed', token)
        self.assertEqual(rc, 3, out)
        self.assertIn('another removal of', out)
        alias = self.t / 'alias'
        alias.symlink_to(self.wt)
        with mock.patch.object(self, 'parent', alias / 'parent'):
            rc, out = self.run_remove('--proceed', token)
        self.assertEqual(rc, 3, out)
        self.assertIn('runs (op other, worker terminal wtrm-other)', out)
        self.assertEqual(hc.killed(), [])

    def test_a_dead_workers_admission_is_released_only_once_its_caller_is_gone_too(self):
        hc = self.fake()
        token = self.token(self.run_remove('--dry-run')[1])
        self.admission().parent.mkdir(parents=True)
        record = dict(op='dead', root=str(self.parent), worker='wtrm-dead', caller=os.getpid(),
                      caller_start=wr.wc.hc.proc_start(os.getpid()))
        self.admission().write_text(json.dumps(record))
        rc, out = self.run_remove('--proceed', token)  # its caller still runs: it may yet launch
        self.assertEqual(rc, 3, out)
        gone = subprocess.Popen([sys.executable, '-c', '0'])
        gone.wait()
        self.admission().write_text(json.dumps(dict(record, caller=gone.pid)))
        rc, out = self.run_remove('--proceed', token)
        hc.join()
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.parent.exists())
        self.assertIn('released the stale admission of op dead', (self.reports / 'log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
