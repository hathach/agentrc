"""Tests for pr-babysit's hooks.py against real pre-commit runs in a temp repository."""
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'hooks.py'
spec = importlib.util.spec_from_file_location('pr_babysit_hooks', SCRIPT)
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)

CONFIG = '''repos:
- repo: local
  hooks:
{}'''
HOOK = '''  - id: {id}
    name: {id}
    entry: sh hooks/{id}.sh
    language: system
    files: \\.txt$
'''
SCRIPTS = {
    'ok': 'exit 0\n',
    'broken': 'echo nope; exit 1\n',
    # Appends once, so the second run passes: the case pre-commit's retry is for.
    'fmt': 'for f; do grep -q formatted "$f" || echo formatted >> "$f"; done\n',
    # Rewrites a tracked file outside the paths it was given, as a doc generator does.
    'gen': 'echo generated > b.txt\n',
    'echo': 'printf -- "- hook id: fake\\n- files were modified by this hook\\n"; exit 1\n',
    'counted': 'echo run >> "$HOOK_LOG"; exit 1\n',
    # Outlasts a short deadline; its pid shows whether the timeout took it down.
    'slow': 'echo $$ > "$HOOK_PID"; sleep 30\n',
    # Fails only on what the change brings in: HEAD's tree passes it.
    'picky': 'if grep -l bad "$@"; then echo "bad content"; exit 1; fi\n',
    # Rewrites a file with what it already holds, as a doc generator does: only its stat changes (#377).
    'same': 'cp b.txt b.tmp && mv b.tmp b.txt\n',
    # Rewrites its files on every run: the retry never settles it.
    'grow': 'for f; do echo more >> "$f"; done\n',
    'abbrev': 'test "$(git config core.abbrev)" = 40 || { echo "core.abbrev is $(git config core.abbrev)"; exit 1; }\n',
    # Fails everywhere and leaves a file behind; the next one fails wherever that file is.
    'litter': 'echo litter > litter.txt; echo "no compiler"; exit 1\n',
    'nolitter': 'test ! -e litter.txt || { echo "found litter.txt"; exit 1; }\n',
}


def running(pid):
    """A killed hook is a zombie until whoever inherited it reaps it: that is not running."""
    if os.path.isdir('/proc'):
        try:
            with open(f'/proc/{pid}/stat') as f:
                return f.read().rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def setUpModule():
    guard_survivors(__name__)


class RepoConfigTest(unittest.TestCase):
    def test_the_suite_hook_runs_for_a_change_to_any_tracked_file(self):
        config = yaml.safe_load((SCRIPT.parents[3] / hooks.CONFIG).read_text(encoding='utf-8'))
        hook = next(h for repo in config['repos'] for h in repo['hooks'] if h['id'] == 'skill-tests')
        # a path filter, or the default `types: [file]` that skips a symlink-only change, would leave a component untested
        self.assertTrue(hook.get('always_run'))


@unittest.skipUnless(shutil.which('pre-commit'), 'pre-commit is not installed')
class HooksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        self.env = git_env(PRE_COMMIT_HOME=str(Path(tmp.name) / 'cache'))
        self.git('init', '-q')
        (self.repo / 'hooks').mkdir()
        for name, body in SCRIPTS.items():
            (self.repo / 'hooks' / f'{name}.sh').write_text(body)
        (self.repo / 'a.txt').write_text('a\n')
        (self.repo / 'b.txt').write_text('b\n')

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def configure(self, *ids):
        if ids:
            (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(''.join(HOOK.format(id=i) for i in ids)))
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')

    def run_script(self, *paths, cwd=None, env=None):
        done = subprocess.run([sys.executable, str(SCRIPT), *paths], cwd=cwd or self.repo,
                              env=env or self.env, capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout.splitlines()[-1])

    def wait_for(self, done, failure, seconds=10):
        deadline = time.monotonic() + seconds
        while not done():
            if time.monotonic() > deadline:
                self.fail(failure)
            time.sleep(0.05)

    def snap(self, lines):
        return {line.split(' ', 2)[2]: line.split(' ', 2)[:2] for line in lines}

    def test_no_config_reports_hooks_not_run(self):
        self.configure()
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 0)
        self.assertEqual((out['ran'], out['passed'], out['modifiedBy']), (False, True, []))
        self.assertEqual((out['failed'], out['baselineFailed']), ([], []))
        self.assertEqual(out['before'], [' M a.txt'])
        self.assertEqual(out['before'], out['after'])
        self.assertEqual(self.snap(out['snapshotBefore'])['a.txt'][0], '644')
        self.assertEqual(out['snapshotBefore'], out['snapshotAfter'])

    def test_passing_hooks(self):
        self.configure('ok')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['ran'], out['passed'], out['modifiedBy']), (0, True, True, []))

    def test_a_modifying_hook_is_named_and_the_retry_decides(self):
        self.configure('fmt', 'gen')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 0)
        self.assertEqual((out['ran'], out['passed']), (True, True))
        self.assertEqual(out['modifiedBy'], ['fmt', 'gen'])
        self.assertEqual(out['after'], [' M a.txt', ' M b.txt'])
        before, after = self.snap(out['snapshotBefore']), self.snap(out['snapshotAfter'])
        self.assertNotEqual(before['a.txt'], after['a.txt'])
        self.assertNotIn('b.txt', before)
        self.assertEqual(after['b.txt'][1], self.git('hash-object', 'b.txt').strip())

    def test_a_failing_hook_that_modified_nothing_runs_once(self):
        # A second run repairs only what a hook rewrote (#140): the same failure again costs a whole slow pass.
        self.configure('counted')
        log = Path(self.repo.parent) / 'runs.log'
        code, out = self.run_script('a.txt', env={**self.env, 'HOOK_LOG': str(log)})
        self.assertEqual((code, out['ran'], out['passed'], out['modifiedBy']), (0, True, False, []))
        # the HEAD rerun is the hook alone in another tree, never a second pass here
        self.assertEqual(log.read_text().splitlines(), ['run', 'run'])
        self.assertEqual(out['baselineFailed'], ['counted'])

    @unittest.skipIf(os.name == 'nt', 'POSIX process groups; Windows takes the tree with taskkill')
    def test_hooks_past_the_deadline_are_killed_and_reported(self):
        self.configure('slow')
        pid = Path(self.repo.parent) / 'hook.pid'
        code, out = self.run_script('--deadline-seconds', '2', 'a.txt', env={**self.env, 'HOOK_PID': str(pid)})
        self.assertEqual(code, 2)
        self.assertRegex(out['error'], r'pre-commit did not finish within 2 s')
        hook = int(pid.read_text())
        self.wait_for(lambda: not running(hook), f'the hook ({hook}) outlived the kill')

    @unittest.skipIf(os.name == 'nt', 'POSIX signals')
    def test_hooks_go_down_with_an_interrupted_or_terminated_run(self):
        self.configure('slow')
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig.name):
                pid = Path(self.repo.parent) / f'hook-{sig.name}.pid'
                proc = subprocess.Popen([sys.executable, str(SCRIPT), 'a.txt'], cwd=self.repo,
                                        env={**self.env, 'HOOK_PID': str(pid)}, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                self.addCleanup(proc.kill)
                self.wait_for(lambda: pid.exists() and pid.read_text().strip(), 'the hook never started', 20)
                hook = int(pid.read_text())
                self.addCleanup(subprocess.run, ['kill', '-9', str(hook)], capture_output=True)
                proc.send_signal(sig)
                self.assertNotEqual(proc.wait(timeout=10), 0)
                self.wait_for(lambda: not running(hook), f'the hook ({hook}) outlived the {sig.name}')

    @unittest.skipUnless(shutil.which('setsid') and os.name != 'nt', 'needs setsid')
    def test_a_process_that_escapes_the_kill_does_not_hold_the_result(self):
        # pre-commit keeps a hook's output on its own pipes; a pre-commit whose child starts another session
        # and keeps ours outlives the group kill.
        self.configure('ok')
        bin_dir = Path(self.repo.parent) / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'pre-commit').write_text('#!/bin/sh\nsetsid sleep 30 &\necho $! > "$HOOK_PID"\nsleep 30\n')
        (bin_dir / 'pre-commit').chmod(0o755)
        pid = Path(self.repo.parent) / 'hook.pid'
        started = time.monotonic()
        env = {**self.env, 'HOOK_PID': str(pid), 'PATH': f"{bin_dir}{os.pathsep}{self.env['PATH']}"}
        code, out = self.run_script('--deadline-seconds', '2', 'a.txt', env=env)
        self.addCleanup(lambda: subprocess.run(['kill', '-9', pid.read_text().strip()], capture_output=True))
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(code, 2)
        self.assertRegex(out['error'], r'did not finish within 2 s .*may still be running')

    def worktrees(self):
        return [line for line in self.git('worktree', 'list', '--porcelain').splitlines() if line.startswith('worktree ')]

    def test_a_failing_hook_is_named_with_its_first_line_and_rerun_on_head(self):
        # A host that cannot run a hook fails it on HEAD too: that is no evidence against the change (#378).
        self.configure('ok', 'broken')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['ran'], out['passed']), (0, True, False))
        self.assertEqual(out['failed'], [{'hook': 'broken', 'error': 'nope'}])
        self.assertEqual(out['baselineFailed'], ['broken'])
        self.assertEqual(len(self.worktrees()), 1, 'the HEAD worktree is removed')
        self.assertEqual(out['snapshotBefore'], out['snapshotAfter'])

    def test_each_head_rerun_starts_from_a_clean_head(self):
        # nolitter fails here only on what litter left: on HEAD it must not inherit litter's rerun.
        self.configure('litter', 'nolitter')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual([f['hook'] for f in out['failed']], ['litter', 'nolitter'])
        self.assertEqual(out['baselineFailed'], ['litter'])
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_fail_fast_stop_at_an_excused_hook_still_runs_the_rest_on_the_change(self):
        self.configure('broken', 'picky')
        (self.repo / '.pre-commit-config.yaml').write_text(
            'fail_fast: true\n' + CONFIG.format(HOOK.format(id='broken') + HOOK.format(id='picky')))
        self.git('commit', '-qam', 'fail fast')
        (self.repo / 'a.txt').write_text('bad\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'broken', 'error': 'nope'}, {'hook': 'picky', 'error': 'a.txt'}])
        self.assertEqual(out['baselineFailed'], ['broken'])

    def test_a_hook_the_change_breaks_passes_on_head(self):
        self.configure('picky')
        (self.repo / 'a.txt').write_text('bad\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'picky', 'error': 'a.txt'}])
        self.assertEqual(out['baselineFailed'], [])
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_hook_head_does_not_configure_is_never_excused_by_it(self):
        # pre-commit refusing an unknown id exits 1 on HEAD, but that is not the hook failing there.
        self.configure('ok')
        (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(HOOK.format(id='ok') + HOOK.format(id='broken')))
        code, out = self.run_script('a.txt', '.pre-commit-config.yaml')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual([f['hook'] for f in out['failed']], ['broken'])
        self.assertEqual(out['baselineFailed'], [])

    def test_a_hook_still_rewriting_files_is_not_rerun_on_head(self):
        self.configure('grow')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], out['modifiedBy']), (0, False, ['grow']))
        self.assertEqual(out['failed'], [{'hook': 'grow', 'error': 'files were modified by this hook'}])
        self.assertEqual(out['baselineFailed'], [])
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_hook_rewriting_a_file_unchanged_beside_an_unstaged_diff_passes(self):
        # pre-commit compares `git diff` text around each hook; a stat-only rewrite once lengthened the
        # unstaged diff's abbreviated index hashes and failed it (#377). The hooks see full-length ones.
        self.configure('same', 'abbrev')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt', 'b.txt')
        self.assertEqual((code, out['passed'], out['modifiedBy'], out['failed']), (0, True, [], []))

    def test_markers_a_hook_prints_are_not_read_as_pre_commits(self):
        self.configure('echo')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], out['modifiedBy']), (0, False, []))

    def test_deleted_and_renamed_paths_are_snapshotted(self):
        self.configure()
        self.git('mv', 'b.txt', 'c.txt')
        (self.repo / 'a.txt').unlink()
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 0)
        snap = self.snap(out['snapshotBefore'])
        self.assertEqual(snap['a.txt'], ['absent', '-'])
        self.assertEqual(snap['b.txt'], ['absent', '-'])
        self.assertEqual(snap['c.txt'][0], '644')

    def test_files_in_a_new_directory_are_each_a_record_and_snapshotted(self):
        # One `?? new/` record matched no scope file and refused the fix (#322); a stray neighbour stays outside.
        self.configure()
        for f in ('new/x.txt', 'new/y.txt', 'stray/z.txt'):
            (self.repo / f).parent.mkdir(exist_ok=True)
            (self.repo / f).write_text(f'{f}\n')
        scope = ['new/x.txt', 'new/y.txt']
        code, out = self.run_script(*scope)
        self.assertEqual(code, 0)
        self.assertEqual(out['before'], ['?? new/x.txt', '?? new/y.txt', '?? stray/z.txt'])
        self.assertEqual([r[3:] for r in out['before'] if r[3:] not in scope], ['stray/z.txt'])
        self.assertEqual(set(self.snap(out['snapshotAfter'])), {*scope, 'stray/z.txt'})

    def test_an_executable_is_snapshotted_with_the_mode_git_commits(self):
        self.configure()
        if os.name == 'nt':
            self.git('update-index', '--chmod=+x', 'a.txt')  # Windows keeps the exec bit in the index alone
        else:
            os.chmod(self.repo / 'a.txt', 0o755)
        (self.repo / 'a.txt').write_text('edited\n')
        (self.repo / 'new.txt').write_text('new\n')
        code, out = self.run_script('a.txt', 'new.txt')
        self.assertEqual(code, 0)
        snap = self.snap(out['snapshotAfter'])
        self.git('add', '--', 'a.txt', 'new.txt')
        staged = {line.split('\t')[1]: line.split()[0][3:] for line in self.git('ls-files', '-s', '--', 'a.txt', 'new.txt').splitlines()}
        self.assertEqual({f: snap[f][0] for f in staged}, {'a.txt': '755', 'new.txt': '644'})
        self.assertEqual({f: snap[f][0] for f in staged}, staged, 'what git add commits')

    def test_a_symlink_checked_out_as_a_file_never_snapshots_as_the_mode_it_commits(self):
        self.configure()
        self.git('config', 'core.symlinks', 'false')  # Windows' default: the link is a file holding its target
        (self.repo / 'link').write_text('a.txt')
        self.git('update-index', '--add', '--cacheinfo', f"120000,{self.git('hash-object', '-w', 'link').strip()},link")
        self.git('commit', '-qm', 'link')
        (self.repo / 'link').write_text('b.txt')
        code, out = self.run_script('link')
        self.assertEqual(code, 0)
        self.git('add', '--', 'link')
        self.assertTrue(self.git('ls-files', '-s', '--', 'link').startswith('120000 '), 'git add keeps the symlink')
        self.assertEqual(self.snap(out['snapshotAfter'])['link'][0], '644', "never ls-tree's 000: the workflow refuses its commit")

    @unittest.skipIf(os.name == 'nt', 'Windows file names are Unicode: none is not UTF-8')
    def test_a_status_that_is_not_utf8_is_an_error(self):
        self.configure()
        (self.repo / b'bad\xff.c'.decode('utf-8', 'surrogateescape')).write_text('stray\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 2)
        self.assertIn('not UTF-8', out['error'])

    def test_refuses_outside_the_top_level(self):
        self.configure()
        code, out = self.run_script('a.txt', cwd=self.repo / 'hooks')
        self.assertEqual(code, 2)
        self.assertIn('top level', out['error'])

    def test_a_config_without_pre_commit_is_an_error(self):
        self.configure('ok')
        if os.name == 'nt':
            # Windows runs no symlinked git, and Git for Windows' launcher needs its own install around it.
            path = os.path.dirname(shutil.which('git'))
            if shutil.which('pre-commit', path=path):
                self.skipTest(f'pre-commit sits beside git in {path}')
        else:
            path = self.repo.parent / 'bin'
            path.mkdir()
            (path / 'git').symlink_to(shutil.which('git'))
        code, out = self.run_script('a.txt', env={**self.env, 'PATH': str(path)})
        self.assertEqual(code, 2)
        self.assertIn('pre-commit is not installed', out['error'])

    def test_no_paths_is_a_usage_error(self):
        code, out = self.run_script()
        self.assertEqual(code, 2)
        self.assertIn('usage', out['error'])


class KillTreeTest(unittest.TestCase):
    def test_windows_takes_the_tree_with_a_bounded_taskkill(self):
        # the Windows branch, run anywhere with taskkill stood in for: it must not hang the deadline's cleanup
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        with mock.patch.object(hooks.os, 'name', 'nt'), mock.patch.dict(os.environ, {'SystemRoot': 'C:\\Windows'}), \
                mock.patch.object(hooks.windows.subprocess, 'run') as taskkill:
            hooks.kill_tree(proc)
        self.assertEqual(taskkill.call_args.args[0][1:], ['/T', '/F', '/PID', str(proc.pid)])
        self.assertEqual(taskkill.call_args.kwargs['timeout'], 10)
        self.assertIsNotNone(proc.poll(), 'the run itself is killed whatever taskkill did')


class PinnedAbbrevTest(unittest.TestCase):
    def test_appends_after_the_callers_own_config_entries(self):
        env = hooks.pinned_abbrev({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.abbrev', 'GIT_CONFIG_VALUE_0': '7'})
        self.assertEqual((env['GIT_CONFIG_COUNT'], env['GIT_CONFIG_KEY_1'], env['GIT_CONFIG_VALUE_1']), ('2', 'core.abbrev', '40'))
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], '7')
        self.assertEqual(hooks.pinned_abbrev({})['GIT_CONFIG_KEY_0'], 'core.abbrev')
        with self.assertRaisesRegex(hooks.Unusable, 'not a count'):
            hooks.pinned_abbrev({'GIT_CONFIG_COUNT': 'x'})


class ResultsTest(unittest.TestCase):
    FAILED = 'fmt' + '.' * 70 + 'Failed'

    def test_output_contradicting_the_exit_status_is_refused(self):
        with self.assertRaisesRegex(hooks.Unusable, 'exited 0'):
            hooks.results(f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n', 0)
        with self.assertRaisesRegex(hooks.Unusable, 'passed and modified'):
            hooks.results(f'fmt{"." * 70}Passed\n- hook id: fmt\n- files were modified by this hook\n', 1)

    def test_a_failed_hook_carries_its_first_output_line(self):
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n\n{"=" * 70}\n  error: no compiler  \nmore\n'
               f'gen{"." * 70}Failed\n- hook id: gen\n- exit code: 2\n'
               f'doc{"." * 50}(no files to check)Skipped\n'
               f'cut{"." * 70}Failed\n- hook id: cut\n- exit code: 1\n\n{"x" * 300}\n')
        self.assertEqual(hooks.results(out, 1)[1], [
            {'hook': 'fmt', 'error': 'error: no compiler'},
            {'hook': 'gen', 'error': 'exit code: 2'},
            {'hook': 'cut', 'error': 'x' * hooks.ERROR_CHARS},
        ])

    def test_only_the_block_under_a_result_line_counts(self):
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n'
               '- hook id: fake\n- files were modified by this hook\n\n'
               f'gen{"." * 70}Failed\n- hook id: gen\n- files were modified by this hook\n')
        self.assertEqual(hooks.results(out, 1)[0], ['gen'])


if __name__ == '__main__':
    unittest.main()
