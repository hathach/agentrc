"""Tests for pr-babysit's hooks.py against real pre-commit runs in a temp repository."""
import importlib.util
import json
import os
import re
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
    'nocc': 'echo "no compiler"; exit 1\n',
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
    # Names each violation: HEAD's old one and a new one the change adds read differently.
    'lint': 'grep -n bad "$@" && exit 1; exit 0\n',
    # Prints where it runs as a native tool would (Git for Windows' sh says /tmp for %TEMP%): HEAD's tree and the
    # checkout must read alike.
    'where': 'echo "$(pwd -W 2>/dev/null || pwd)/a.txt: no compiler"; exit 1\n',
    # Rewrites a tracked file on HEAD only; showb then reads it.
    'scribble': 'grep -q edited a.txt || echo scribbled > b.txt; echo "no compiler"; exit 1\n',
    'showb': 'cat b.txt; exit 1\n',
    # Fails on the change at once and outlasts a short deadline on HEAD.
    'headslow': 'if grep -q edited "$@"; then echo fails; exit 1; fi; echo $$ > "$HOOK_PID"; sleep 30\n',
    # Empties b.txt on its first run anywhere, then fails without modifying anything, on HEAD in the same words.
    'once': 'if [ ! -e "$ONCE" ]; then : > "$ONCE"; : > b.txt; fi; echo "no compiler"; exit 1\n',
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
        self.assertEqual((out['failed'], out['baselineFailed'], out['notRerun']), ([], [], []))
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

    def test_a_failing_hook_that_modified_nothing_runs_once_on_the_change(self):
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

    @unittest.skipIf(os.name == 'nt', "a Windows pre-commit is a launcher, not a #! script")
    def test_without_pyyaml_a_hook_failing_on_head_too_is_excused_through_pre_commits_python(self):
        if not hooks.pre_commit_python():
            self.skipTest("the pre-commit on PATH has no #! line naming a python")
        self.configure('ok', 'broken')
        (self.repo / 'a.txt').write_text('edited\n')
        no_yaml = ("import runpy, sys; sys.modules['yaml'] = None; sys.argv.pop(0); "
                   "runpy.run_path(sys.argv[0], run_name='__main__')")
        done = subprocess.run([sys.executable, '-c', no_yaml, str(SCRIPT), 'a.txt'], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)
        self.assertTrue(done.stdout.strip(), done.stderr)
        out = json.loads(done.stdout.splitlines()[-1])
        self.assertEqual((done.returncode, out['baselineFailed'], out['notRerun']), (0, ['broken'], []))

    def test_each_head_rerun_starts_from_a_clean_head(self):
        # nolitter fails here only on what litter left: on HEAD it must not inherit litter's rerun.
        self.configure('litter', 'nolitter')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual([f['hook'] for f in out['failed']], ['litter', 'nolitter'])
        self.assertEqual(out['baselineFailed'], ['litter'])
        self.assertEqual(len(self.worktrees()), 1)

    def test_head_reruns_never_reach_the_checkout_a_callers_git_dir_names(self):
        # The resets between HEAD reruns, and the reruns, must act on HEAD's tree, not on what GIT_DIR names.
        self.configure('broken', 'litter')
        (self.repo / 'a.txt').write_text('edited\n')
        (self.repo / 'c.txt').write_text('staged\n')
        self.git('add', 'c.txt')
        (self.repo / 'd.txt').write_text('untracked\n')
        git_dir = self.repo / '.git'
        env = {**self.env, 'GIT_DIR': str(git_dir), 'GIT_WORK_TREE': str(self.repo),
               'GIT_INDEX_FILE': str(git_dir / 'index')}
        code, out = self.run_script('a.txt', env=env)
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['baselineFailed'], ['broken', 'litter'])
        self.assertEqual((self.repo / 'a.txt').read_text(), 'edited\n')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'c.txt\n')
        self.assertEqual((self.repo / 'd.txt').read_text(), 'untracked\n')
        self.assertEqual(len(self.worktrees()), 1)

    def test_head_reruns_reach_an_object_store_a_callers_git_object_directory_names(self):
        # The tree shares the checkout's objects: without them its reset before the second rerun cannot resolve HEAD.
        self.configure('broken', 'litter')
        (self.repo / 'a.txt').write_text('edited\n')
        shutil.move(self.repo / '.git' / 'objects', self.repo.parent / 'objects')
        (self.repo / '.git' / 'objects').mkdir()
        # relative to the checkout, where HEAD's tree's commands do not run
        env = {**self.env, 'GIT_OBJECT_DIRECTORY': os.path.join('..', 'objects')}
        code, out = self.run_script('a.txt', env=env)
        self.assertEqual((code, out.get('error'), out.get('passed')), (0, None, False))
        self.assertEqual(out['baselineFailed'], ['broken', 'litter'])

    def test_each_head_rerun_starts_from_heads_tracked_files(self):
        # scribble rewrites b.txt on HEAD (so is not excused: pre-commit says it modified files there); showb must
        # read HEAD's b.txt, not scribble's.
        self.configure('scribble', 'showb')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual([f['hook'] for f in out['failed']], ['scribble', 'showb'])
        self.assertEqual(out['baselineFailed'], ['showb'])

    def test_a_hook_failing_on_head_in_other_words_is_not_excused(self):
        # HEAD already fails lint; the change adds a violation of its own.
        (self.repo / 'a.txt').write_text('old bad typo\n')
        self.configure('lint')
        (self.repo / 'a.txt').write_text('old bad typo\nnew bad regression\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'lint', 'error': '1:old bad typo'}])
        self.assertEqual(out['baselineFailed'], [])

    def test_where_a_hook_runs_is_read_alike_on_head_and_the_checkout(self):
        self.configure('where')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], [f['hook'] for f in out['failed']]), (0, False, ['where']))
        self.assertEqual(out['baselineFailed'], ['where'])

    def test_an_id_entries_share_is_never_rerun_on_head(self):
        # pre-commit runs, and SKIP drops, every entry with the id: one failing on HEAD excuses none of the others.
        (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(
            HOOK.format(id='dup').replace('hooks/dup.sh', 'hooks/broken.sh')
            + HOOK.format(id='dup').replace('hooks/dup.sh', 'hooks/picky.sh')))
        self.configure()
        (self.repo / 'a.txt').write_text('bad\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], [f['hook'] for f in out['failed']]), (0, False, ['dup', 'dup']))
        self.assertEqual(out['baselineFailed'], [])
        self.assertEqual(out['notRerun'], [{'hook': 'dup', 'why': 'SKIP would skip another hook by it'}])

    def test_an_id_entries_share_is_never_excused_past_a_fail_fast_stop(self):
        # The first entry fails alike on HEAD and stops both runs; SKIP=dup would skip the second, which the change breaks.
        (self.repo / '.pre-commit-config.yaml').write_text('fail_fast: true\n' + CONFIG.format(
            HOOK.format(id='dup').replace('hooks/dup.sh', 'hooks/broken.sh')
            + HOOK.format(id='dup').replace('hooks/dup.sh', 'hooks/picky.sh')))
        self.configure()
        (self.repo / 'a.txt').write_text('bad\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'dup', 'error': 'nope'}])
        self.assertEqual(out['baselineFailed'], [])

    def test_a_hook_that_modified_files_in_any_run_is_never_excused(self):
        # The emptied b.txt would otherwise be committed with the hook skipped.
        self.configure('once')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt', env={**self.env, 'ONCE': str(Path(self.repo.parent) / 'once')})
        self.assertEqual((code, out['passed'], out['modifiedBy']), (0, False, ['once']))
        self.assertEqual(out['failed'], [{'hook': 'once', 'error': 'no compiler'}])
        self.assertEqual(out['baselineFailed'], [])

    def test_a_head_rerun_past_the_deadline_is_killed_and_its_worktree_removed(self):
        self.configure('headslow')
        (self.repo / 'a.txt').write_text('edited\n')
        pid = Path(self.repo.parent) / 'hook.pid'
        # The deadline covers the change's run and `worktree add` too: Windows' slow pre-commit startup needs more.
        deadline = '20' if os.name == 'nt' else '6'
        code, out = self.run_script('--deadline-seconds', deadline, 'a.txt', env={**self.env, 'HOOK_PID': str(pid)})
        self.assertEqual(code, 2)
        self.assertRegex(out['error'], rf'did not finish within {deadline} s \(HEAD headslow\)')
        self.assertEqual(len(self.worktrees()), 1, 'the HEAD worktree is removed')
        # an MSYS pid is not a Windows one; no pid, the kill came before the hook started
        if os.name != 'nt' and pid.exists() and pid.read_text().strip():
            hook = int(pid.read_text())
            self.wait_for(lambda: not running(hook), f'the hook ({hook}) outlived the kill')

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

    def test_a_hook_fail_fast_hid_that_fails_on_head_too_is_excused_in_turn(self):
        # Each stop hides the next hook: nocc fails on HEAD as well, picky only on the change.
        (self.repo / '.pre-commit-config.yaml').write_text(
            'fail_fast: true\n' + CONFIG.format(''.join(HOOK.format(id=i) for i in ('broken', 'nocc', 'picky'))))
        self.configure()
        (self.repo / 'a.txt').write_text('bad\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual([f['hook'] for f in out['failed']], ['broken', 'nocc', 'picky'])
        self.assertEqual(out['baselineFailed'], ['broken', 'nocc'])
        self.assertEqual(out['notRerun'], [], 'picky was rerun on HEAD and passed there')

    def test_an_id_another_hook_takes_as_its_alias_is_never_excused(self):
        # SKIP=broken would skip picky too (pre-commit matches aliases), and fail_fast stopped the run before it.
        picky = HOOK.format(id='picky') + '    alias: broken\n'
        (self.repo / '.pre-commit-config.yaml').write_text(
            'fail_fast: true\n' + CONFIG.format(HOOK.format(id='broken') + picky))
        self.configure()
        (self.repo / 'a.txt').write_text('bad\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'broken', 'error': 'nope'}])
        self.assertEqual(out['baselineFailed'], [])
        self.assertEqual(out['notRerun'], [{'hook': 'broken', 'why': 'SKIP would skip another hook by it'}])

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
        self.assertEqual(out['notRerun'], [{'hook': 'grow', 'why': 'it modified files'}])
        self.assertEqual(len(self.worktrees()), 1)

    def test_hooks_run_with_full_length_abbrev_and_a_same_content_rewrite_passes(self):
        # pre-commit compares `git diff` text around each hook; a stat-only rewrite once lengthened the
        # unstaged diff's abbreviated index hashes and failed it (#377). Reproducing that takes an object store
        # at the abbrev boundary: abbrev guards the pin itself, and same shows such a rewrite is not a modification.
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


class ForgetTest(unittest.TestCase):
    """forget's cleanup of the HEAD worktree, with git alone: no pre-commit runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        self.env = git_env()
        self.git('init', '-q')
        (self.repo / 'a.txt').write_text('a\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def worktrees(self):
        return [line for line in self.git('worktree', 'list', '--porcelain').splitlines() if line.startswith('worktree ')]

    def head_tree(self):
        tmp = tempfile.mkdtemp(prefix='hooks-base-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tree = os.path.join(tmp, 'head')
        self.git('worktree', 'add', '--detach', '--quiet', tree, 'HEAD')
        return tmp, tree

    def test_a_failed_removal_still_drops_the_head_worktree_even_left_locked(self):
        # A killed `worktree add` leaves its entry locked; prune drops it only once the tree is gone and unlocked.
        tmp, tree = self.head_tree()
        self.git('worktree', 'lock', '--reason', 'initializing', tree)
        real = hooks.git_within

        def failing_remove(deadline, start, *argv, cwd=None):
            if argv[:2] == ('worktree', 'remove'):
                raise hooks.Unusable('git worktree remove: refused')
            return real(deadline, start, *argv, cwd=cwd)
        with mock.patch.object(hooks, 'git_within', failing_remove):
            hooks.forget(tree, tmp, 10, time.monotonic())
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_lock_that_cannot_be_removed_never_raises(self):
        tmp, tree = self.head_tree()
        with mock.patch.object(hooks, 'git_within', side_effect=hooks.Unusable('git worktree remove: refused')), \
                mock.patch.object(hooks.Path, 'unlink', side_effect=PermissionError('read-only')) as unlink:
            hooks.forget(tree, tmp, 10, time.monotonic())
        unlink.assert_called_once_with(missing_ok=True)
        self.assertFalse(os.path.exists(tmp))

    def admin(self, gitdir):
        admin = self.repo / '.git' / 'worktrees' / 'other'
        admin.mkdir(parents=True)
        (admin / 'gitdir').write_bytes(gitdir)
        return admin

    def test_another_worktree_registered_at_a_path_that_is_not_utf8_is_passed_over(self):
        self.admin(b'/tmp/bad\xff/.git\n')
        tmp, tree = self.head_tree()
        self.assertIsNotNone(hooks.registration(tree))
        self.assertIsNone(hooks.registration(os.path.join(tmp, 'gone')))

    def test_a_relative_gitdir_is_read_against_its_admin_dir(self):
        # worktree.useRelativePaths (git 2.48) writes the tree's .git relative to the admin dir.
        tree = self.repo.parent / 'rel'
        admin = self.admin(f'{os.path.relpath(tree / ".git", self.repo / ".git" / "worktrees" / "other")}\n'.encode())
        # registration builds on the resolved common dir: Windows' TEMP may be an 8.3 short name (RUNNER~1)
        self.assertEqual(hooks.registration(str(tree)), admin.resolve())


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


class OtherRepoEnvTest(unittest.TestCase):
    REPO = {'GIT_DIR': '.git', 'GIT_WORK_TREE': '.', 'GIT_COMMON_DIR': '.git', 'GIT_INDEX_FILE': 'index'}

    def test_the_object_store_stays_absolute_and_the_repository_goes(self):
        alternates = os.pathsep.join(['alt', os.path.abspath('abs'), hooks.c_quoted(os.path.abspath('quoted'))])
        with mock.patch.dict(os.environ, {**self.REPO, 'GIT_OBJECT_DIRECTORY': 'objs',
                                          'GIT_ALTERNATE_OBJECT_DIRECTORIES': alternates}):
            env = hooks.other_repo_env()
        self.assertFalse(set(self.REPO) & set(env))
        self.assertEqual(env['GIT_OBJECT_DIRECTORY'], os.path.abspath('objs'))
        self.assertEqual(env['GIT_ALTERNATE_OBJECT_DIRECTORIES'],
                         os.pathsep.join([os.path.abspath('alt'), os.path.abspath('abs'),
                                          hooks.c_quoted(os.path.abspath('quoted'))]))

    def test_git_without_a_tree_keeps_the_callers_repository_and_drops_only_its_index(self):
        seen = []
        with mock.patch.dict(os.environ, self.REPO), \
                mock.patch.object(hooks, 'bounded', lambda *a: seen.append(a[-1]) or (0, '', '')):
            hooks.git_within(10, time.monotonic(), 'worktree', 'add')
            hooks.git_within(10, time.monotonic(), 'reset', cwd='tree')
        self.assertEqual({k: seen[0].get(k) for k in self.REPO}, {**self.REPO, 'GIT_INDEX_FILE': None})
        self.assertFalse(set(self.REPO) & set(seen[1]))

    def test_a_quoted_alternate_holding_the_separator_stays_whole_and_is_made_absolute(self):
        # git reads a quoted entry up to its closing quote, past any separator inside it, and resolves it relative
        # to where it runs.
        quoted = '"../a' + os.pathsep + 'l\\"t\\303\\251"'
        alternates = os.pathsep.join([quoted, 'alt', '', '#note', quoted])
        with mock.patch.dict(os.environ, {'GIT_ALTERNATE_OBJECT_DIRECTORIES': alternates}):
            env = hooks.other_repo_env()
        made = hooks.c_quoted(os.path.abspath('../a' + os.pathsep + 'l"t\u00e9'))
        self.assertEqual(env['GIT_ALTERNATE_OBJECT_DIRECTORIES'],
                         os.pathsep.join([made, os.path.abspath('alt'), '', '#note', made]))
        self.assertEqual(hooks.c_unquoted(made[1:-1]), os.path.abspath('../a' + os.pathsep + 'l"t\u00e9'))

    def test_an_alternate_quoted_wrongly_is_read_as_a_plain_entry(self):
        # unquote_c_style refuses an unknown escape, so git reads the entry unquoted, up to the separator.
        alternates = os.pathsep.join(['"bad\\q', 'x"'])
        with mock.patch.dict(os.environ, {'GIT_ALTERNATE_OBJECT_DIRECTORIES': alternates}):
            env = hooks.other_repo_env()
        self.assertEqual(env['GIT_ALTERNATE_OBJECT_DIRECTORIES'],
                         os.pathsep.join([os.path.abspath('"bad\\q'), os.path.abspath('x"')]))


class RunHooksTest(unittest.TestCase):
    BROKEN = 'broken' + '.' * 70 + 'Failed\n- hook id: broken\n- exit code: 1\n\nnope\n'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, hooks.CONFIG).write_text('repos: []\n')
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)

    def test_a_run_without_the_excused_that_fails_naming_no_hook_is_its_own_failure(self):
        def pre_commit(paths, deadline, start, what, hook=None, cwd=None, skip=()):
            return (1, '') if skip else (1, self.BROKEN)
        with mock.patch.object(hooks, 'pre_commit', pre_commit), \
                mock.patch.object(hooks, 'baseline', return_value=['broken']):
            ran, passed, ids, failures, excused, _ = hooks.run_hooks(['a.txt'])
        self.assertEqual((ran, passed, ids, excused), (True, False, [], ['broken']))
        self.assertEqual([(f['hook'], f['error']) for f in failures], [
            ('broken', 'nope'), (hooks.UNOWNED, 'pre-commit exited 1 with broken skipped, naming no hook')])

    def test_the_run_without_the_excused_gets_its_retry(self):
        # A formatter fail_fast hid rewrites in that run and passes on its retry, as it would with fail_fast off.
        fmt = 'fmt' + '.' * 70 + 'Failed\n- hook id: fmt\n- exit code: 1\n- files were modified by this hook\n'
        runs = iter([(1, self.BROKEN), (1, fmt), (0, '')])
        with mock.patch.object(hooks, 'pre_commit', lambda *a, **k: next(runs)), \
                mock.patch.object(hooks, 'baseline', return_value=['broken']):
            ran, passed, ids, failures, excused, _ = hooks.run_hooks(['a.txt'])
        self.assertEqual((ids, [f['hook'] for f in failures], excused), (['fmt'], ['broken'], ['broken']))

    def test_a_hook_the_run_without_the_excused_finds_unexcusable_is_never_rerun_and_says_why(self):
        # fail_fast hid them behind broken: one rewrites files there, one SKIP cannot name, one shares its id.
        Path(hooks.CONFIG).write_text(CONFIG.format(''.join(HOOK.format(id=i) for i in ('broken', 'dup', 'dup'))))
        fmt = 'fmt' + '.' * 70 + 'Failed\n- hook id: fmt\n- exit code: 1\n- files were modified by this hook\n'
        hidden = fmt + self.BROKEN.replace('broken', 'a,b') + self.BROKEN.replace('broken', 'dup')
        with mock.patch.object(hooks, 'pre_commit', lambda *a, skip=(), **k: (1, hidden if skip else self.BROKEN)), \
                mock.patch.object(hooks, 'baseline', return_value=['broken']) as baseline:
            _, _, ids, failures, excused, not_rerun = hooks.run_hooks(['a.txt'])
        baseline.assert_called_once()
        self.assertEqual((ids, excused), (['fmt'], ['broken']))
        self.assertEqual([f['hook'] for f in failures], ['broken', 'fmt', 'a,b', 'dup'])
        self.assertEqual(not_rerun, [{'hook': 'fmt', 'why': 'it modified files'},
                                     {'hook': 'a,b', 'why': 'SKIP cannot name it'},
                                     {'hook': 'dup', 'why': 'SKIP would skip another hook by it'}])

    def test_nothing_is_rerun_on_head_when_the_config_cannot_be_read(self):
        # Its aliases unknown, SKIP could skip a hook that never ran on the change.
        Path(hooks.CONFIG).write_text('repos: [\n')
        with mock.patch.object(hooks, 'pre_commit', return_value=(1, self.BROKEN)), \
                mock.patch.object(hooks, 'baseline') as baseline:
            *_, excused, not_rerun = hooks.run_hooks(['a.txt'])
        baseline.assert_not_called()
        self.assertEqual(excused, [])
        self.assertEqual([n['hook'] for n in not_rerun], ['broken'])
        self.assertRegex(not_rerun[0]['why'], rf'^{re.escape(hooks.CONFIG)} cannot be read \(\w+Error\)$')

    def test_nothing_is_rerun_on_head_without_pyyaml_or_a_known_pre_commit_python_and_the_failure_says_why(self):
        with mock.patch.object(hooks, 'yaml', None), mock.patch.object(hooks, 'pre_commit_python', return_value=None), \
                mock.patch.object(hooks, 'pre_commit', return_value=(1, self.BROKEN)), \
                mock.patch.object(hooks, 'baseline') as baseline:
            *_, failures, excused, not_rerun = hooks.run_hooks(['a.txt'])
        baseline.assert_not_called()
        self.assertEqual(([f['hook'] for f in failures], excused), (['broken'], []))
        self.assertEqual(not_rerun, [{'hook': 'broken', 'why': (
            "PyYAML is not installed, nor is pre-commit's interpreter known, so the config was not read")}])

    def without_pyyaml(self, config, interpreter=sys.executable):
        """run_hooks' excused and notRerun without PyYAML here, the pre-commit on PATH a symlink to an entry script
        whose #! line names interpreter."""
        if ' ' in interpreter or not hooks.SHEBANG_PYTHON.fullmatch(os.path.basename(interpreter)):
            self.skipTest(f'a #! line cannot name {interpreter} as a python: a space splits it, or its name is not one')
        Path(hooks.CONFIG).write_text(config)
        for d in ('bin', 'venv'):
            os.mkdir(d)
        Path('venv', 'pre-commit').write_text(f'#!{interpreter}\n')
        os.symlink(os.path.abspath('venv/pre-commit'), 'bin/pre-commit')
        os.chmod('venv/pre-commit', 0o755)
        with mock.patch.object(hooks, 'yaml', None), mock.patch.dict(os.environ, {'PATH': os.path.abspath('bin')}), \
                mock.patch.object(hooks, 'pre_commit', return_value=(1, self.BROKEN)), \
                mock.patch.object(hooks, 'baseline', return_value=['broken']) as baseline:
            *_, excused, not_rerun = hooks.run_hooks(['a.txt'])
        return baseline, excused, not_rerun

    @unittest.skipIf(os.name == 'nt', "a Windows pre-commit is a launcher, not a #! script")
    def test_without_pyyaml_pre_commits_own_python_reads_the_config(self):
        baseline, excused, not_rerun = self.without_pyyaml(CONFIG.format(HOOK.format(id='broken')))
        baseline.assert_called_once()
        self.assertEqual((excused, not_rerun), (['broken'], []))

    @unittest.skipIf(os.name == 'nt', "a Windows pre-commit is a launcher, not a #! script")
    def test_without_pyyaml_an_id_pre_commits_python_reads_as_shared_is_never_rerun(self):
        baseline, excused, not_rerun = self.without_pyyaml(CONFIG.format(HOOK.format(id='broken') * 2))
        baseline.assert_not_called()
        self.assertEqual((excused, not_rerun), ([], [{'hook': 'broken', 'why': 'SKIP would skip another hook by it'}]))

    @unittest.skipIf(os.name == 'nt', "a Windows pre-commit is a launcher, not a #! script")
    def test_without_pyyaml_a_pre_commit_python_that_cannot_load_the_config_reruns_nothing(self):
        Path('python3').write_text('#!/bin/sh\necho "ModuleNotFoundError: No module named \'yaml\'" >&2; exit 1\n')
        os.chmod('python3', 0o755)
        python = os.path.abspath('python3')
        baseline, excused, not_rerun = self.without_pyyaml('repos: []\n', python)
        baseline.assert_not_called()
        self.assertEqual((excused, not_rerun), ([], [{'hook': 'broken', 'why': (
            f"pre-commit's {python} cannot read {hooks.CONFIG} (ModuleNotFoundError: No module named 'yaml')")}]))

    @unittest.skipIf(os.name == 'nt', "a Windows pre-commit is a launcher, not a #! script")
    def test_without_pyyaml_a_pre_commit_python_that_cannot_start_reruns_nothing(self):
        python = os.path.abspath('python3')
        baseline, excused, not_rerun = self.without_pyyaml('repos: []\n', python)
        baseline.assert_not_called()
        self.assertEqual((excused, not_rerun), ([], [{'hook': 'broken', 'why': (
            f"pre-commit's {python} cannot run (FileNotFoundError)")}]))

    @unittest.skipIf(os.name == 'nt', "a Windows pre-commit is a launcher, not a #! script")
    def test_without_pyyaml_a_pre_commit_python_printing_no_json_reruns_nothing(self):
        Path('python3').write_text('#!/bin/sh\necho not json\n')
        os.chmod('python3', 0o755)
        baseline, excused, not_rerun = self.without_pyyaml('repos: []\n', os.path.abspath('python3'))
        baseline.assert_not_called()
        self.assertEqual((excused, not_rerun), ([], [{'hook': 'broken', 'why': (
            f'{hooks.CONFIG} cannot be read (JSONDecodeError)')}]))

    def test_an_id_skip_cannot_name_is_never_rerun_on_head(self):
        out = self.BROKEN.replace('broken', 'a,b')
        with mock.patch.object(hooks, 'pre_commit', return_value=(1, out)), \
                mock.patch.object(hooks, 'baseline') as baseline:
            *_, excused, not_rerun = hooks.run_hooks(['a.txt'])
        baseline.assert_not_called()
        self.assertEqual((excused, not_rerun), ([], [{'hook': 'a,b', 'why': 'SKIP cannot name it'}]))


class PreCommitPythonTest(unittest.TestCase):
    def entry(self, first_line):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name, 'pre-commit')
        script.write_bytes(first_line + b'\nfrom pre_commit.main import main\n')
        with mock.patch.object(hooks.shutil, 'which', return_value=str(script)):
            return hooks.pre_commit_python(), os.path.realpath(tmp.name)

    def test_the_interpreter_and_its_one_argument_are_read_as_the_kernel_does(self):
        found, where = self.entry(b'#! /usr/bin/python3 -s -E')
        self.assertEqual(found, (['/usr/bin/python3', '-s -E'], where))
        for python in (b'/opt/venvs/pre-commit/bin/python3.12', b'/usr/bin/python3.13t', b'/usr/bin/python3-dbg',
                       b'/opt/pypy/bin/pypy3'):
            found, where = self.entry(b'#!' + python)
            self.assertEqual(found, ([python.decode()], where))

    def test_a_line_naming_no_python_by_absolute_path_is_no_interpreter(self):
        # env, distlib's /bin/sh form for a long path, a launcher .exe, a relative path, or no #! at all
        for line in (b'#!/usr/bin/env python3', b'#!/bin/sh', b'MZ\x90\x00', b'#!python', b'#!', b'import sys'):
            self.assertIsNone(self.entry(line)[0], line)

    def test_no_pre_commit_on_path_is_no_interpreter(self):
        with mock.patch.object(hooks.shutil, 'which', return_value=None):
            self.assertIsNone(hooks.pre_commit_python())


class AsCheckoutTest(unittest.TestCase):
    def test_every_spelling_of_the_tree_reads_as_the_placeholder(self):
        tree = os.path.join(tempfile.gettempdir(), 'hooks-base-x', 'head')
        posix = tree.replace('\\', '/')
        said = f'{tree}/a.c: no compiler\n{posix}/b.c'
        if os.name == 'nt':
            said += f'\n/{posix[0].lower()}{posix[2:]}/c.c\n{tree.upper()}'
        self.assertNotIn('hooks-base-x', hooks.as_checkout(said, tree).lower())

    def test_two_runs_of_one_failure_compare_equal_and_a_new_violation_does_not(self):
        # What tinyusb's unit-test hooks printed on HEAD and on the change (#378): timings, tempfile names, order.
        change = "FAIL: a\nPermissionError: 'C:/T/tmpl1lb44ri'\nPermissionError: 'C:/T/tmpabcdefgh.json'\nRan 9 tests in 4.357s\n"
        head = "PermissionError: 'C:/T/tmpld2ovkrs'\nFAIL: a\nPermissionError: 'C:/T/tmpzyxwvuts.json'\n\nRan 9 tests in 6.3s\n"
        self.assertEqual(hooks.comparable(change, '/x'), hooks.comparable(head, '/x'))
        self.assertNotEqual(hooks.comparable('1:old bad\n', '/x'), hooks.comparable('1:old bad\n2:new bad\n', '/x'))

    def test_only_numbers_standing_alone_are_masked(self):
        # A pid, a line, a duration or an address differs between runs; a file, rule or symbol naming a digit does not.
        self.assertEqual(hooks.comparable('a.c:12:3: E501 in 0.5s (pid 4242) at 0x7f00ab\n', '/x'),
                         hooks.comparable('a.c:14:9: E501 in 125ms (pid 77) at 0x7f11cd\n', '/x'))
        for head, change in (('usb2.c:1: bad\n', 'cdc1.c:1: bad\n'), ('a.c:1: E501 bad\n', 'a.c:1: E502 bad\n'),
                             ('a.c:1: x1 unused\n', 'a.c:1: x2 unused\n'),
                             ('src/ccdriver.c: bad\n', 'src/ccsensor.c: bad\n'),
                             ('gw1 unused\n', 'gw2 unused\n'),
                             ('a.c:1: v2m bad\n', 'a.c:1: v3m bad\n')):
            self.assertNotEqual(hooks.comparable(head, '/x'), hooks.comparable(change, '/x'), change)

    def test_times_compound_durations_worker_numbers_and_tool_temp_names_are_masked(self):
        # Run-varying words that hold a digit: an ISO time, 3m12s, pytest-xdist's [gwN] failure header, gcc's and
        # mktemp's temporary names.
        for head, change in (('at 2026-11-03T12:34:56Z\n', 'at 2026-11-04T01:02:03.250Z\n'),
                             ('at 2026-11-03T12:34:56+07:00\n', 'at 2026-11-03T12:35:07+07:00\n'),
                             ('took 3m12s\n', 'took 4m01s\n'), ('took 12.5sec\n', 'took 9.1sec\n'),
                             ('[gw0] linux -- Python 3.12.3 /usr/bin/python\n',
                              '[gw3] linux -- Python 3.12.3 /usr/bin/python\n'),
                             ('/tmp/ccAb12Cd.o: undefined reference\n', '/tmp/cco1y76V.o: undefined reference\n'),
                             ('/tmp/ccabcdef.o: undefined reference\n', '/tmp/ccAb12Cd.o: undefined reference\n'),
                             ('C:\\T\\Temp\\ccqwerty.o: undefined\n', 'C:\\T\\Temp\\cc3Xy7Zq.o: undefined\n'),
                             ('/tmp/tmp.Ab3dE5fG7h: denied\n', '/tmp/tmp.yiJuMi1EqI: denied\n')):
            self.assertEqual(hooks.comparable(head, '/x'), hooks.comparable(change, '/x'), change)


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
        self.assertEqual([{k: f[k] for k in ('hook', 'error')} for f in hooks.results(out, 1)[1]], [
            {'hook': 'fmt', 'error': 'error: no compiler'},
            {'hook': 'gen', 'error': 'exit code: 2'},
            {'hook': 'cut', 'error': 'x' * hooks.ERROR_CHARS},
        ])
        # what the HEAD rerun is compared on: the block and everything printed, up to the next result
        self.assertEqual([f['output'] for f in hooks.results(out, 1)[1]], [
            f'- exit code: 1\n\n\n{"=" * 70}\n  error: no compiler  \nmore', '- exit code: 2',
            f'- exit code: 1\n\n{"x" * 300}'])

    def test_only_the_block_under_a_result_line_counts(self):
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n'
               '- hook id: fake\n- files were modified by this hook\n\n'
               f'gen{"." * 70}Failed\n- hook id: gen\n- files were modified by this hook\n')
        self.assertEqual(hooks.results(out, 1)[0], ['gen'])


if __name__ == '__main__':
    unittest.main()
