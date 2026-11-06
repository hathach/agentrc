"""Tests for pr-babysit's hooks.py against real pre-commit runs in a temp repository."""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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
    'abbrev': 'test "$(git config core.abbrev)" = "$ABBREV" || { echo "core.abbrev is $(git config core.abbrev)"; exit 1; }\n',
    # Adds a one-object pack, as a hook running git gc or fetch may: git's automatic abbrev follows the packed count.
    'packer': 'echo "packer $$ $(date +%s%N)" | git hash-object -w --stdin '
              '| git pack-objects -q "$(git rev-parse --git-path objects/pack/pack)" >/dev/null\n',
    # Fails everywhere and leaves a file behind; the next one fails wherever that file is.
    'litter': 'echo litter > litter.txt; echo "no compiler"; exit 1\n',
    'nolitter': 'test ! -e litter.txt || { echo "found litter.txt"; exit 1; }\n',
    # Names each violation: HEAD's old one and a new one the change adds read differently.
    'lint': 'grep -n bad "$@" && exit 1; exit 0\n',
    # Names a violation in a tracked directory called temp, its source named as gcc names a temporary file.
    'templint': 'grep -H bad ./src/temp/*.c; test $? -eq 1\n',
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
    # Prints, after a blank line, a line shaped as pre-commit's next result, then what only the change brings in.
    'mimic': f'printf "no compiler\\n\\nReport{"." * 67}Passed\\n"; grep -q edited "$@" && echo "a.txt: new"; exit 1\n',
    # The same with a `- hook id:` line under it, as pre-commit prints a block under its result.
    'header': f'printf "no compiler\\nReport{"." * 67}Passed\\n- hook id: nested\\n\\n"; '
              'grep -q edited "$@" && echo "a.txt: new"; exit 1\n',
    # Prints, after a blank line, a verbose result, its block and its output, as a hook running pre-commit does, then
    # after another what only the change brings in.
    'nested': f'printf "x.c:1: bad\\n\\nReport{"." * 67}Passed\\n- hook id: nested\\n- duration: 0.1s\\n\\n'
              'checked\\n"; grep -q edited "$@" && printf "\\nx.c:2: new\\n"; exit 1\n',
    # The same on HEAD and the change.
    'nestedsame': f'printf "x.c:1: bad\\n\\nReport{"." * 67}Passed\\n- hook id: nested\\n- duration: 0.1s\\n\\n'
                  'checked\\n"; exit 1\n',
    # Under what it prints on HEAD too, prints on the change only a nested run's verbose result and its output.
    'appends': f'echo "x.c:1: bad"; grep -q edited "$@" && printf "\\nReport{"." * 67}Passed\\n- hook id: sub\\n'
               '- duration: 0.1s\\n\\nx.c:2: new\\n"; exit 1\n',
    # Prints, after a blank line, a nested run's failed result, its block and its output, alike on HEAD and the change.
    'nestedfail': f'printf "x.c:1: bad\\n\\nReport{"." * 67}Failed\\n- hook id: nested\\n- exit code: 1\\n'
                  '- files were modified by this hook\\n\\nchecked\\n"; exit 1\n',
    # Prints, between blank lines, a failed result of target's in pre-commit's shape, then what target prints first.
    'lead': f'printf "old\\n\\ntarget{"." * 67}Failed\\n- hook id: target\\n- exit code: 1\\n\\nmarker\\n"; exit 1\n',
    # On HEAD prints lead's tail and that result before what it prints on the change too.
    'target': f'grep -q edited "$@" || printf "marker\\n\\ntarget{"." * 67}Failed\\n- hook id: target\\n'
              '- exit code: 1\\n\\n"; echo new; exit 1\n',
    # Passes, printing two failed results of hooks the config lacks with no blank line between them.
    'reports': f'printf "Report{"." * 67}Failed\\n- hook id: nested1\\n- exit code: 1\\n'
               f'Report{"." * 67}Failed\\n- hook id: nested2\\n- exit code: 1\\n"\n',
    # Passes, printing a failed result of nocc's and nocc's output.
    'pv': f'printf "Report{"." * 67}Failed\\n- hook id: nocc\\n- exit code: 1\\n\\nno compiler\\n"\n',
    'zhook': 'grep -q edited "$@" && { echo "z: new"; exit 1; }; exit 0\n',
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


def worktrees(git):
    return [line for line in git('worktree', 'list', '--porcelain').splitlines() if line.startswith('worktree ')]


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

    def abbrev(self):
        """The length git abbreviates HEAD to here, unpinned."""
        return len(self.git('rev-parse', '--short', 'HEAD').strip())

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
        # the change's run, then HEAD's rerun of the hook alone in another tree; without fail_fast no pass follows
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
        return worktrees(self.git)

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

    def test_heads_tree_is_checked_out_without_the_repositorys_git_hooks(self):
        # A post-checkout hook failing, or writing into the tree, would fail or change the first rerun.
        self.configure('ok', 'broken')
        hook = self.repo / '.git' / 'hooks' / 'post-checkout'
        hook.write_text('#!/bin/sh\necho hooked > hooked.txt; exit 1\n')
        hook.chmod(0o755)
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out.get('error'), out['baselineFailed']), (0, None, ['broken']))
        self.assertEqual(len(self.worktrees()), 1)

    def test_heads_tree_is_reset_between_reruns_without_the_repositorys_git_hooks(self):
        # reset --hard updates HEAD through a ref transaction; a failing reference-transaction hook would fail it.
        self.configure('broken', 'nocc')
        hook = self.repo / '.git' / 'hooks' / 'reference-transaction'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out.get('error'), out['baselineFailed']), (0, None, ['broken', 'nocc']))
        self.assertEqual(len(self.worktrees()), 1)

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

    def test_a_new_violation_in_a_tracked_temp_directory_is_not_excused(self):
        # HEAD fails on ccdriver.c, the change on ccsensor.c: a lowercase cc name in a temp directory is a source.
        sources = self.repo / 'src' / 'temp'
        sources.mkdir(parents=True)
        (sources / 'ccdriver.c').write_text('bad\n')
        (sources / 'ccsensor.c').write_text('good\n')
        self.configure('templint')
        (sources / 'ccdriver.c').write_text('good\n')
        (sources / 'ccsensor.c').write_text('bad\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'templint', 'error': './src/temp/ccsensor.c:bad'}])
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

    def test_a_head_rerun_past_the_deadline_is_killed_its_worktree_removed_and_the_failure_kept(self):
        self.configure('headslow')
        (self.repo / 'a.txt').write_text('edited\n')
        pid = Path(self.repo.parent) / 'hook.pid'
        # The deadline covers the change's run and `worktree add` too: Windows' slow pre-commit startup needs more.
        deadline = '20' if os.name == 'nt' else '6'
        code, out = self.run_script('--deadline-seconds', deadline, 'a.txt', env={**self.env, 'HOOK_PID': str(pid)})
        self.assertEqual((code, out['passed'], out['failed']), (0, False, [{'hook': 'headslow', 'error': 'fails'}]))
        self.assertEqual(([n['hook'] for n in out['notRerun']], out['baselineFailed']), (['headslow'], []))
        late = rf'did not finish within {deadline} s \((HEAD headslow|git worktree)\)'
        self.assertRegex(out['notRerun'][0]['why'], rf'^HEAD ran out of time: \S+ {late}')
        self.assertEqual(len(self.worktrees()), 1, 'the HEAD worktree is removed')
        # an MSYS pid is not a Windows one; no pid, the kill came before the hook started
        if os.name != 'nt' and pid.exists() and pid.read_text().strip():
            hook = int(pid.read_text())
            self.wait_for(lambda: not running(hook), f'the hook ({hook}) outlived the kill')

    def test_a_head_tree_git_cannot_check_out_leaves_the_failure_counted_and_unjudged(self):
        # A required smudge filter that fails stops `worktree add`; the change's failures must survive it.
        (self.repo / 'c.bin').write_text('c\n')
        (self.repo / '.gitattributes').write_text('c.bin filter=broken\n')
        for key, value in (('clean', 'cat'), ('smudge', 'false'), ('required', 'true')):
            self.git('config', f'filter.broken.{key}', value)
        self.configure('nocc')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], out['failed']), (0, False, [{'hook': 'nocc', 'error': 'no compiler'}]))
        self.assertEqual(([n['hook'] for n in out['notRerun']], out['baselineFailed']), (['nocc'], []))
        self.assertRegex(out['notRerun'][0]['why'], r'^HEAD could not judge it: git worktree: .*smudge filter')
        self.assertNotIn('\n', out['notRerun'][0]['why'])
        self.assertEqual(len(self.worktrees()), 1, 'the HEAD worktree is removed')

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

    def test_what_a_hook_prints_after_a_result_line_of_its_own_is_compared_with_head(self):
        self.configure('mimic', 'ok')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'mimic', 'error': 'no compiler'}])
        self.assertEqual(out['baselineFailed'], [])

    def test_what_a_hook_prints_after_a_block_of_its_own_is_compared_with_head(self):
        self.configure('header', 'ok')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'header', 'error': 'no compiler'}])
        self.assertEqual(out['baselineFailed'], [])

    def test_what_a_hook_prints_after_a_verbose_result_of_its_own_is_compared_with_head(self):
        # HEAD's run of nested alone ends with its block; the change's line after it is no part of HEAD's output.
        for ids in (('nested', 'ok'), ('nested',)):
            with self.subTest(ids):
                (self.repo / 'a.txt').write_text('a\n')
                self.configure(*ids)
                (self.repo / 'a.txt').write_text('edited\n')
                code, out = self.run_script('a.txt')
                self.assertEqual((code, out['passed']), (0, False))
                self.assertEqual(out['failed'], [{'hook': 'nested', 'error': 'x.c:1: bad'}])
                self.assertEqual(out['baselineFailed'], [])

    def test_a_verbose_result_a_hook_prints_alike_on_head_is_excused_with_it(self):
        self.configure('nestedsame', 'ok')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(out['failed'], [{'hook': 'nestedsame', 'error': 'x.c:1: bad'}])
        self.assertEqual(out['baselineFailed'], ['nestedsame'])

    def test_a_result_the_change_adds_under_heads_output_ends_it_only_naming_a_later_hook(self):
        # sub is no hook the config runs after appends: its result is one appends printed, x.c:2 its own.
        self.configure('appends', 'ok')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(out['failed'], [{'hook': 'appends', 'error': 'x.c:1: bad'}])
        self.assertEqual(out['baselineFailed'], [])

    def test_a_failed_result_a_hook_prints_alike_on_head_is_no_failure_of_a_hook_the_config_lacks(self):
        self.configure('nestedfail', 'ok')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], out['modifiedBy']), (0, False, []))
        self.assertEqual(out['failed'], [{'hook': 'nestedfail', 'error': 'x.c:1: bad'}])
        self.assertEqual((out['baselineFailed'], out['notRerun']), (['nestedfail'], []))

    def test_a_failed_result_a_hook_prints_naming_a_configured_hook_never_hides_that_hooks_own(self):
        self.configure('lead', 'target')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'lead', 'error': 'old'}, {'hook': 'target', 'error': 'marker'},
                                         {'hook': 'target', 'error': 'new'}])
        self.assertEqual(out['baselineFailed'], ['lead'])

    def test_a_failed_result_a_passing_hook_prints_never_hides_a_later_hooks_failure(self):
        verbose = HOOK.format(id='pv') + '    verbose: true\n'
        (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(verbose + HOOK.format(id='zhook')
                                                                         + HOOK.format(id='nocc')))
        self.configure()
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual([f['hook'] for f in out['failed']], ['nocc', 'zhook', 'nocc'])
        self.assertNotIn('zhook', out['baselineFailed'])

    def test_a_failed_result_with_no_end_pre_commits_next_may_be_at_stays_a_counted_failure(self):
        verbose = HOOK.format(id='reports') + '    verbose: true\n'
        (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(verbose + HOOK.format(id='broken')))
        self.configure()
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed']), (0, False))
        self.assertEqual(out['failed'], [{'hook': 'nested1', 'error': 'exit code: 1'},
                                         {'hook': 'broken', 'error': 'nope'}])
        self.assertEqual(out['baselineFailed'], ['broken'])

    def test_hooks_failing_alike_on_head_are_each_excused_whatever_follows(self):
        (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(''.join(
            HOOK.format(id=i).replace(f'hooks/{i}.sh', 'hooks/nocc.sh') for i in ('aaa', 'bbb', 'ccc'))))
        self.configure()
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(out['failed'], [{'hook': i, 'error': 'no compiler'} for i in ('aaa', 'bbb', 'ccc')])
        self.assertEqual(out['baselineFailed'], ['aaa', 'bbb', 'ccc'])

    def test_a_verbose_hook_after_a_failure_ends_its_output_skipped_or_not(self):
        # pre-commit prints a verbose hook's `- hook id:` under its Skipped result too: no output of broken's.
        ok = HOOK.format(id='ok') + '    verbose: true\n'
        for case, hook, env in (('no files', ok.replace('\\.txt$', '\\.none$'), {}), ('SKIP', ok, {'SKIP': 'ok'}),
                                ('passed', ok, {})):
            with self.subTest(case):
                (self.repo / 'a.txt').write_text('a\n')
                (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(HOOK.format(id='broken') + hook))
                self.git('add', '-A')
                self.git('commit', '-qm', case, '--allow-empty')
                (self.repo / 'a.txt').write_text('edited\n')
                code, out = self.run_script('a.txt', env={**self.env, **env})
                self.assertEqual((code, out['failed']), (0, [{'hook': 'broken', 'error': 'nope'}]))
                self.assertEqual(out['baselineFailed'], ['broken'])

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

    def test_hooks_run_with_the_checkouts_abbrev_and_a_same_content_rewrite_passes(self):
        # pre-commit compares `git diff` text around each hook: same's stat-only rewrite leaves it alike, pinned or
        # not. abbrev checks the hooks get the length the checkout abbreviates to, not a full one; the pack test
        # below is the one that fails unpinned (#377).
        self.configure('same', 'abbrev')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt', 'b.txt', env={**self.env, 'ABBREV': str(self.abbrev())})
        self.assertEqual((code, out['passed'], out['modifiedBy'], out['failed']), (0, True, [], []))

    def pack(self, count):
        """Add count blobs in one pack: 16383 packed objects abbreviate to 7 hex digits, 16384 to 8."""
        blobs = ''.join(f'blob\ndata {len(str(i)) + 1}\n{i}\n' for i in range(count))
        # Bytes: text mode writes '\n' as '\r\n' on Windows and corrupts the stream.
        subprocess.run(['git', 'fast-import', '--quiet'], input=blobs.encode(), cwd=self.repo, env=self.env, check=True,
                       capture_output=True)

    def test_a_hook_adding_a_pack_across_the_abbrev_boundary_is_no_modification(self):
        # Unpinned, the pack packer adds lengthens the unstaged diff's index hashes and pre-commit reports it
        # modified files (#377).
        self.pack(16383)
        self.configure('packer')
        (self.repo / 'a.txt').write_text('edited\n')
        self.assertEqual(self.abbrev(), 7)
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], out['modifiedBy'], out['failed']), (0, True, [], []))
        self.assertEqual(self.abbrev(), 8, 'the pack crossed the boundary')

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
    """forget's cleanup of the HEAD worktree, and baseline's around it, with git alone: no pre-commit runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        self.env = git_env()
        self.git('init', '-q')
        # git 2.56's auto maintenance, detached by the commit below, prunes the prunable entries these tests make
        self.git('config', 'maintenance.auto', 'false')
        (self.repo / 'a.txt').write_text('a\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def worktrees(self):
        return worktrees(self.git)

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
            hooks.forget(tmp, 10, time.monotonic())
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(len(self.worktrees()), 1)

    def test_another_worktree_whose_tree_is_missing_keeps_its_registration(self):
        # A moved or unmounted worktree of the user's is prunable, but not this run's to drop.
        other = self.repo.parent / 'other'
        self.git('worktree', 'add', '--detach', '--quiet', str(other), 'HEAD')
        shutil.rmtree(other)
        tmp, tree = self.head_tree()
        with mock.patch.object(hooks, 'git_within', side_effect=hooks.Unusable('git worktree remove: refused')):
            hooks.forget(tmp, 10, time.monotonic())
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(len(self.worktrees()), 2, 'the head tree goes, the other stays registered')

    def forget_with(self, tmp, rmtree, deadline=10):
        """forget(tmp) with its `worktree remove` refused and shutil.rmtree(tmp) replaced by rmtree(tmp, the real
        rmtree); its stderr."""
        real = shutil.rmtree

        def removal(path, *a, **k):
            return rmtree(path, real) if os.fspath(path) == tmp else real(path, *a, **k)
        stderr = io.StringIO()
        with mock.patch.object(hooks, 'git_within', side_effect=hooks.Unusable('git worktree remove: refused')), \
                mock.patch.object(hooks.shutil, 'rmtree', removal), contextlib.redirect_stderr(stderr):
            hooks.forget(tmp, deadline, time.monotonic())
        return stderr.getvalue()

    def test_what_cannot_be_removed_is_named_and_never_raises(self):
        tmp, tree = self.head_tree()
        admin = hooks.registration(tree)
        stderr = self.forget_with(tmp, lambda *_: None)
        self.assertIn(f"HEAD's worktree could not be removed: {tmp}, {admin}", stderr)
        self.assertTrue(admin.exists(), 'a registration whose tree stays is kept')

    def test_an_entry_another_worktree_took_once_the_tree_went_is_kept(self):
        # Another session's prune, then its `worktree add`, may reuse the name of an entry whose tree is gone.
        tmp, tree = self.head_tree()
        admin = hooks.registration(tree)
        theirs = self.repo.parent / 'theirs' / '.git'

        def removed_then_taken(path, rmtree):
            rmtree(path)
            (admin / 'gitdir').write_text(f'{theirs}\n')
        self.forget_with(tmp, removed_then_taken)
        self.assertEqual((admin / 'gitdir').read_text(), f'{theirs}\n')

    def test_a_tree_slow_to_delete_is_left_past_the_cleanups_budget_and_named(self):
        tmp, tree = self.head_tree()
        released, removing = threading.Event(), []

        def slow(*_):
            removing.append(threading.current_thread())
            released.wait(30)
        started = time.monotonic()
        try:
            with mock.patch.object(hooks, 'CLEANUP', 0):
                stderr = self.forget_with(tmp, slow, deadline=0)
            self.assertLess(time.monotonic() - started, 10)
            self.assertIn(f"HEAD's worktree could not be removed: {tmp}", stderr)
        finally:
            # a thread alive past the test would make another test's fork warn
            released.set()
            for thread in removing:
                thread.join(10)

    def test_heads_tree_stays_locked_while_it_runs_and_goes_after(self):
        # Unlocked, another session's prune could drop its entry once the tree goes, and its `worktree add` take the
        # name before forget deletes the entry.
        seen = []

        def pre_commit(*_, **__):
            seen.append(self.git('worktree', 'list', '--porcelain'))
            return 0, ''
        with mock.patch.object(hooks, 'pre_commit', pre_commit):
            hooks.baseline({'a': []}, ['a.txt'], 10, time.monotonic())
        self.assertRegex(seen[0], r'\nlocked\b')
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_rerun_head_cannot_judge_leaves_it_and_those_after_it_unjudged_and_keeps_those_before(self):
        said = '- exit code: 1\n\nbad'
        runs = [(1, f'a{"." * 70}Failed\n- hook id: a\n{said}\n'),
                # pre-commit's output contradicting its exit status
                (0, f'b{"." * 70}Failed\n- hook id: b\n{said}\n')]
        expected = {h: [[hooks.comparable(said, '/x')]] for h in 'abc'}
        with mock.patch.object(hooks, 'pre_commit', side_effect=runs):
            failing, unjudged = hooks.baseline(expected, ['a.txt'], 10, time.monotonic())
        self.assertEqual(failing, ['a'])
        self.assertEqual(list(unjudged), ['b', 'c'])
        self.assertTrue(all(why.startswith('HEAD could not judge it: ') for why in unjudged.values()), unjudged)
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_rerun_the_deadline_cuts_short_leaves_it_and_those_after_it_unjudged(self):
        late = hooks.Late('pre-commit did not finish within 9 s (HEAD a)')
        with mock.patch.object(hooks, 'pre_commit', side_effect=late):
            failing, unjudged = hooks.baseline({'a': [], 'b': []}, ['a.txt'], 10, time.monotonic())
        self.assertEqual(failing, [])
        self.assertEqual(unjudged, {h: f'HEAD ran out of time: {late}' for h in 'ab'})
        self.assertEqual(len(self.worktrees()), 1)

    @unittest.skipUnless(threading.current_thread() is threading.main_thread(), 'handlers run on the main thread')
    def test_a_signal_during_the_cleanup_is_delivered_after_it(self):
        tmp, tree = self.head_tree()
        held = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        self.addCleanup(signal.signal, signal.SIGTERM, held)

        def terminated_remove(*_, **__):
            signal.raise_signal(signal.SIGTERM)
            raise hooks.Unusable('git worktree remove: killed')
        with mock.patch.object(hooks, 'git_within', terminated_remove), self.assertRaises(SystemExit):
            hooks.forget(tmp, 10, time.monotonic())
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(len(self.worktrees()), 1)

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
        env = hooks.pinned_abbrev({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.abbrev', 'GIT_CONFIG_VALUE_0': '7'}, 9)
        self.assertEqual((env['GIT_CONFIG_COUNT'], env['GIT_CONFIG_KEY_1'], env['GIT_CONFIG_VALUE_1']), ('2', 'core.abbrev', '9'))
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], '7')
        self.assertEqual(hooks.pinned_abbrev({}, 9)['GIT_CONFIG_KEY_0'], 'core.abbrev')
        with self.assertRaisesRegex(hooks.Unusable, 'not a count'):
            hooks.pinned_abbrev({'GIT_CONFIG_COUNT': 'x'}, 9)


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
        # fail_fast: once every failure is excused, the hooks run again without them
        Path(tmp.name, hooks.CONFIG).write_text('fail_fast: true\nrepos: []\n')
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)

    def test_a_run_without_the_excused_that_fails_naming_no_hook_is_its_own_failure(self):
        def pre_commit(paths, deadline, start, what, hook=None, cwd=None, skip=()):
            return (1, '') if skip else (1, self.BROKEN)
        with mock.patch.object(hooks, 'pre_commit', pre_commit), \
                mock.patch.object(hooks, 'baseline', return_value=(['broken'], {})):
            ran, passed, ids, failures, excused, _ = hooks.run_hooks(['a.txt'])
        self.assertEqual((ran, passed, ids, excused), (True, False, [], ['broken']))
        self.assertEqual([(f['hook'], f['error']) for f in failures], [
            ('broken', 'nope'), (hooks.UNOWNED, 'pre-commit exited 1 with broken skipped, naming no hook')])

    def test_a_run_without_the_excused_past_the_deadline_is_its_own_failure(self):
        late = hooks.Late('pre-commit did not finish within 540 s (without the excused 1)')

        def pre_commit(paths, deadline, start, what, hook=None, cwd=None, skip=()):
            if skip:
                raise late
            return 1, self.BROKEN
        with mock.patch.object(hooks, 'pre_commit', pre_commit), \
                mock.patch.object(hooks, 'baseline', return_value=(['broken'], {})):
            _, passed, _, failures, excused, _ = hooks.run_hooks(['a.txt'])
        self.assertEqual((passed, excused), (False, ['broken']))
        self.assertEqual([(f['hook'], f['error']) for f in failures], [('broken', 'nope'), (hooks.UNOWNED, str(late))])

    def test_the_ids_a_run_without_the_excused_names_reach_its_failure_copyable(self):
        escaped = '\x1b[31mbroken\x07'

        def pre_commit(paths, deadline, start, what, hook=None, cwd=None, skip=()):
            return (1, '') if skip else (1, self.BROKEN.replace('- hook id: broken', f'- hook id: {escaped}'))
        with mock.patch.object(hooks, 'pre_commit', pre_commit), \
                mock.patch.object(hooks, 'baseline', return_value=([escaped], {})):
            failures = hooks.run_hooks(['a.txt'])[3]
        self.assertEqual(failures[-1], {'hook': hooks.UNOWNED,
                                        'error': 'pre-commit exited 1 with broken  skipped, naming no hook'})

    def test_a_padded_id_is_read_as_its_block_names_it_and_never_rerun_on_head(self):
        # pre-commit prints a padded id as is, and SKIP strips each name it gives, so it cannot skip that hook.
        Path(hooks.CONFIG).write_text(CONFIG.format(HOOK.format(id='broken') + HOOK.format(id="'pad '")))
        pad = f'pad {"." * 72}Failed\n- hook id: pad \n- exit code: 1\n\nworse\n'
        with mock.patch.object(hooks, 'pre_commit', return_value=(1, f'{self.BROKEN}\n{pad}')), \
                mock.patch.object(hooks, 'baseline', return_value=(['broken'], {})) as baseline:
            _, _, _, failures, excused, not_rerun = hooks.run_hooks(['a.txt'])
        self.assertEqual(list(baseline.call_args.args[0]), ['broken'])
        self.assertEqual(([f['hook'] for f in failures], excused), (['broken', 'pad'], ['broken']))
        self.assertEqual(not_rerun, [{'hook': 'pad', 'why': 'SKIP cannot name it'}])

    def test_a_head_rerun_the_deadline_cut_short_says_why(self):
        why = 'HEAD ran out of time: pre-commit did not finish within 540 s (HEAD broken)'
        with mock.patch.object(hooks, 'pre_commit', return_value=(1, self.BROKEN)), \
                mock.patch.object(hooks, 'baseline', return_value=([], {'broken': why})):
            *_, failures, excused, not_rerun = hooks.run_hooks(['a.txt'])
        self.assertEqual(([f['hook'] for f in failures], excused), (['broken'], []))
        self.assertEqual(not_rerun, [{'hook': 'broken', 'why': why}])

    def test_without_fail_fast_every_hook_ran_and_none_runs_again(self):
        remote = 'repos:\n- repo: https://example.com/hooks\n  rev: v1\n  hooks:\n  - id: broken\n'
        # a remote repository's manifest may set fail_fast unless the hook's entry says
        for config in ('repos: []\n', CONFIG.format(HOOK.format(id='broken')), remote + '    fail_fast: false\n',
                       CONFIG.format(HOOK.format(id='broken') + '    fail_fast: true\n'), remote):
            with self.subTest(config):
                Path(hooks.CONFIG).write_text(config)
                runs = mock.Mock(side_effect=lambda *_, skip=(), **__: (0, '') if skip else (1, self.BROKEN))
                with mock.patch.object(hooks, 'pre_commit', runs) as pre_commit, \
                        mock.patch.object(hooks, 'baseline', return_value=(['broken'], {})):
                    *_, failures, excused, _ = hooks.run_hooks(['a.txt'])
                self.assertEqual(([f['hook'] for f in failures], excused), (['broken'], ['broken']))
                self.assertEqual(pre_commit.call_count, 2 if config.endswith(('true\n', 'broken\n')) else 1)

    def test_the_run_without_the_excused_gets_its_retry(self):
        # A formatter fail_fast hid rewrites in that run and passes on its retry, as it would with fail_fast off.
        fmt = 'fmt' + '.' * 70 + 'Failed\n- hook id: fmt\n- exit code: 1\n- files were modified by this hook\n'
        runs = iter([(1, self.BROKEN), (1, fmt), (0, '')])
        with mock.patch.object(hooks, 'pre_commit', lambda *a, **k: next(runs)), \
                mock.patch.object(hooks, 'baseline', return_value=(['broken'], {})):
            ran, passed, ids, failures, excused, _ = hooks.run_hooks(['a.txt'])
        self.assertEqual((ids, [f['hook'] for f in failures], excused), (['fmt'], ['broken'], ['broken']))

    def test_a_hook_the_run_without_the_excused_finds_unexcusable_is_never_rerun_and_says_why(self):
        # fail_fast hid them behind broken: one rewrites files there, one SKIP cannot name, one shares its id. The
        # config runs them in that order, and pre-commit ends each hook's output with a blank line.
        ids = ('broken', 'fmt', 'a,b', 'dup', 'dup')
        Path(hooks.CONFIG).write_text('fail_fast: true\n' + CONFIG.format(''.join(HOOK.format(id=i) for i in ids)))
        fmt = 'fmt' + '.' * 70 + 'Failed\n- hook id: fmt\n- exit code: 1\n- files were modified by this hook\n'
        hidden = fmt + self.BROKEN.replace('broken', 'a,b') + '\n' + self.BROKEN.replace('broken', 'dup')
        with mock.patch.object(hooks, 'pre_commit', lambda *a, skip=(), **k: (1, hidden if skip else self.BROKEN)), \
                mock.patch.object(hooks, 'baseline', return_value=(['broken'], {})) as baseline:
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
                mock.patch.object(hooks, 'baseline', return_value=(['broken'], {})) as baseline:
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
    def test_without_pyyaml_why_holds_no_terminal_escapes_or_control_characters(self):
        # Its interpreter's path and its last stderr line are copied into why, which a relay copies on.
        os.mkdir('e\x1b[31mx')
        python = os.path.abspath('e\x1b[31mx/python3')
        Path(python).write_text('#!/bin/sh\nprintf "\\033[31mModuleNotFoundError:\\a\\tyaml\\033[0m\\n" >&2; exit 1\n')
        os.chmod(python, 0o755)
        baseline, excused, not_rerun = self.without_pyyaml('repos: []\n', python)
        baseline.assert_not_called()
        self.assertEqual((excused, not_rerun), ([], [{'hook': 'broken', 'why': (
            f"pre-commit's {os.path.abspath('ex/python3')} cannot read {hooks.CONFIG} (ModuleNotFoundError:  yaml)")}]))

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
    def test_a_temp_dir_inside_the_checkout_is_no_temp_dir(self):
        # Read as the checkout first, its path would end at a boundary the temp dir's could follow.
        root = '/srv/co'
        self.assertNotEqual(hooks.comparable(f'{root}{tempfile.gettempdir()}/ccdriver.c: bad\n', root),
                            hooks.comparable(f'{root}{tempfile.gettempdir()}/ccsensor.c: bad\n', root))

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
        for head, change in (('usb2.c:1: bad\n', 'usb3.c:1: bad\n'), ('a.c:1: E501 bad\n', 'a.c:1: E502 bad\n'),
                             ('a.c:1: x1 unused\n', 'a.c:1: x2 unused\n'),
                             ('src/ccdriver.c: bad\n', 'src/ccsensor.c: bad\n'),
                             ('./src/temp/ccdriver.c:bad\n', './src/temp/ccsensor.c:bad\n'),
                             # a source directory whose path ends as the temp dir's does is no temp dir
                             (f'./src{tempfile.gettempdir()}/ccdriver.c: bad\n',
                              f'./src{tempfile.gettempdir()}/ccsensor.c: bad\n'),
                             (f'/srv/build{tempfile.gettempdir()}/ccdriver.c: bad\n',
                              f'/srv/build{tempfile.gettempdir()}/ccsensor.c: bad\n'),
                             ('gw1 unused\n', 'gw2 unused\n'),
                             # a source named as gcc names a temporary file, outside the temp dir
                             ('hw/bsp/cc2640r2.c: bad\n', 'hw/bsp/cc1352p7.c: bad\n'),
                             ('src/ccDriver.c: bad\n', 'src/ccSensor.c: bad\n'),
                             # an identifier or a source named as tempfile names a file
                             ('a.c:1: tmpcounter1 unused\n', 'a.c:1: tmpindexes2 unused\n'),
                             ('src/tmpcounter1.c: bad\n', 'src/tmpindexes2.c: bad\n'),
                             ('src/tmpl_engine.c:3: E1 bad\n', 'src/tmpl_loader.c:3: E1 bad\n'),
                             ('/x/src/tmpl_engine.c:3: E1 bad\n', '/x/src/tmpl_loader.c:3: E1 bad\n'),
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
                             (f'{tempfile.gettempdir()}/ccAb12Cd.o: undefined reference\n',
                              f'{tempfile.gettempdir()}/cco1y76V.o: undefined reference\n'),
                             (f'{tempfile.gettempdir()}/ccabcdef.s: Error\n',
                              f'{tempfile.gettempdir()}/ccAb12Cd.s: Error\n'),
                             ('/tmp/tmp.Ab3dE5fG7h: denied\n', '/tmp/tmp.yiJuMi1EqI: denied\n'),
                             # Python's repr of a Windows path, and one tempfile name inside another
                             ("'C:\\\\Users\\\\u\\\\AppData\\\\Local\\\\Temp\\\\tmpl1lb44ri'\n",
                              "'C:\\\\Users\\\\u\\\\AppData\\\\Local\\\\Temp\\\\tmpld2ovkrs'\n"),
                             ('/tmp/tmpab12cd_x/tmpabcdefgh.json: bad\n', '/tmp/tmpzz98yy_w/tmpzyxwvuts.json: bad\n')):
            self.assertEqual(hooks.comparable(head, '/x'), hooks.comparable(change, '/x'), change)


class ResultsTest(unittest.TestCase):
    FAILED = 'fmt' + '.' * 70 + 'Failed'

    def test_output_contradicting_the_exit_status_is_refused(self):
        with self.assertRaisesRegex(hooks.Unusable, 'exited 0'):
            hooks.results(f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n', 0)
        with self.assertRaisesRegex(hooks.Unusable, 'passed and modified'):
            hooks.results(f'fmt{"." * 70}Passed\n- hook id: fmt\n- files were modified by this hook\n', 1)

    def test_a_failed_hook_carries_its_first_output_line(self):
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n\n{"=" * 70}\n  error: no compiler  \nmore\n\n'
               f'gen{"." * 70}Failed\n- hook id: gen\n- exit code: 2\n'
               f'doc{"." * 50}(no files to check)Skipped\n'
               f'cut{"." * 70}Failed\n- hook id: cut\n- exit code: 1\n\n{"x" * 300}\n')
        self.assertEqual([{k: f[k] for k in ('hook', 'error')} for f in hooks.results(out, 1)[1]], [
            {'hook': 'fmt', 'error': 'error: no compiler'},
            {'hook': 'gen', 'error': 'exit code: 2'},
            {'hook': 'cut', 'error': 'x' * hooks.ERROR_CHARS},
        ])
        # what the HEAD rerun is compared on: the block and everything printed, up to each result that may be the
        # next, with the id its block names, or the end
        failures = hooks.results(out, 1)[1]
        self.assertEqual([f['outputs'][0] for f in failures], [
            (f'- exit code: 1\n\n\n{"=" * 70}\n  error: no compiler  \nmore', 'gen'), ('- exit code: 2', 'cut'),
            (f'- exit code: 1\n\n{"x" * 300}', None)])
        self.assertEqual([[after for _, after in f['outputs']] for f in failures], [['gen', None], ['cut'], [None]])
        self.assertEqual(failures[0]['outputs'][-1][0], out[out.index('- exit code: 1'):].rstrip())

    def test_a_result_line_the_hook_printed_is_its_output(self):
        # pre-commit pads its own result lines to one width and prints a hook's output between blank lines.
        said = f'x.c:1: bad\n\nReport.....Passed\nx.c:2: worse\nstep{"." * 69}Failed\nx.c:3: worst'
        out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n{said}\n\ngen{"." * 70}Passed\n'
        self.assertEqual([f['outputs'][0] for f in hooks.results(out, 1)[1]], [(f'- exit code: 1\n\n{said}', None)])

    def test_a_result_line_the_hook_printed_after_a_blank_line_is_its_output_too(self):
        # Only the result lines below pre-commit's own blank line end the output: what follows a mimic stays in it.
        said = f'x.c:1: bad\n\nReport{"." * 67}Passed\nx.c:2: new'
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n{said}\n\ngen{"." * 70}Passed\n'
               f'doc{"." * 50}(no files to check)Skipped\ncut{"." * 70}Failed\n- hook id: cut\n- exit code: 1\n')
        self.assertEqual([f['outputs'][0] for f in hooks.results(out, 1)[1]],
                         [(f'- exit code: 1\n\n{said}', 'cut'), ('- exit code: 1', None)])

    def test_a_result_line_and_block_the_hook_printed_are_its_output_too(self):
        # A result line ends the output only below the blank line under its last line, with a block pre-commit
        # prints under that result and all that follows in pre-commit's shape. nested is no hook the config runs.
        for said in (f'x.c:1: bad\nReport{"." * 67}Passed\n- hook id: nested\n- duration: 0.1s\n\nx.c:2: new',
                     f'x.c:1: bad\n\nReport{"." * 67}Passed\n- hook id: nested\n\nx.c:2: new',
                     f'x.c:1: bad\n\nReport{"." * 67}Failed\n- hook id: nested\n\nx.c:2: new',
                     f'x.c:1: bad\n\nReport{"." * 66}Skipped\n- hook id: nested\n\nx.c:2: new'):
            with self.subTest(said):
                out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n{said}\n\ngen{"." * 70}Passed\n'
                failures = hooks.results(out, 1, ['fmt', 'gen'])[1]
                self.assertEqual([f['hook'] for f in failures], ['fmt'])
                self.assertEqual(failures[0]['outputs'][0], (f'- exit code: 1\n\n{said}', None))

    def test_a_result_of_another_width_the_hook_printed_never_ends_its_output(self):
        # pre-commit pads all its result lines to one width: a narrower one in pre-commit's shape is the hook's own.
        said = f'x.c:1: bad\n\nReport{"." * 60}Passed\n- hook id: nested\n- duration: 0.1s\n\nx.c:2: new'
        out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n{said}\n\ngen{"." * 70}Passed\n'
        self.assertEqual([after for _, after in hooks.results(out, 1)[1][0]['outputs']], [None, None])

    def test_a_result_under_two_blank_lines_is_the_hooks_own_output(self):
        # pre-commit strips a hook's output and ends it with one blank line: under two, a result shaped as pre-commit's
        # next, or a block over two, is what the hook printed.
        for said in (f'x.c:1: bad\n\n\ngen{"." * 70}Passed',
                     f'x.c:1: bad\n\ngen{"." * 70}Failed\n- hook id: gen\n- exit code: 1\n\n\nmore'):
            with self.subTest(said):
                out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n{said}\n'
                self.assertEqual(len(hooks.results(out, 1, ['fmt', 'gen'])[1][0]['outputs']), 1)

    def test_a_wide_character_counts_two_columns_in_a_result_line(self):
        # pre-commit pads by display width (_len_cjk): 检查's two characters take four columns, as gen's 79.
        out = f'检查{"." * 69}Failed\n- hook id: check\n- exit code: 1\n\nx.c:1: bad\n\ngen{"." * 70}Passed\n'
        self.assertEqual(hooks.results(out, 1)[1][0]['outputs'][0], ('- exit code: 1\n\nx.c:1: bad', None))

    def test_a_failed_result_the_hook_printed_counts_only_where_pre_commits_next_may_be(self):
        # Under a blank line in pre-commit's shape, nested's result may end fmt's output only where the config runs
        # nested after fmt, or where the config is unread; it counts wherever the config runs nested.
        said = (f'x.c:1: bad\n\nReport{"." * 67}Failed\n- hook id: nested\n- exit code: 1\n'
                '- files were modified by this hook\n\nx.c:2: new')
        out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n{said}\n\ngen{"." * 70}Passed\n'
        for order, found in ((None, ['fmt', 'nested']), (['fmt', 'nested', 'gen'], ['fmt', 'nested']),
                             (['fmt', 'gen'], ['fmt']), (['nested', 'fmt', 'gen'], ['fmt', 'nested'])):
            with self.subTest(order):
                ids, failures = hooks.results(out, 1, order)
                self.assertEqual(([f['hook'] for f in failures], ids), (found, found[1:]))
        # pre-commit's own results past an end stay counted, a passed and a skipped one between them
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\nx.c:1: bad\n\ngen{"." * 70}Passed\n'
               f'doc{"." * 50}(no files to check)Skipped\ncut{"." * 70}Failed\n- hook id: cut\n- exit code: 1\n')
        self.assertEqual([f['hook'] for f in hooks.results(out, 1, ['fmt', 'gen', 'doc', 'cut'])[1]], ['fmt', 'cut'])

    def test_a_verbose_result_the_hook_printed_may_end_its_output_naming_a_later_hook(self):
        # Either result may be pre-commit's next; nested's ends fmt only where the config runs nested after fmt.
        said = f'x.c:1: bad\n\nReport{"." * 67}Passed\n- hook id: nested\n- duration: 0.1s\n\nx.c:2: new'
        out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n{said}\n\ngen{"." * 70}Passed\n'
        fmt = hooks.results(out, 1)[1][0]
        self.assertEqual(fmt['outputs'], [('- exit code: 1\n\nx.c:1: bad', 'nested'),
                                          (f'- exit code: 1\n\n{said}', None),
                                          (f'- exit code: 1\n\n{said}\n\ngen{"." * 70}Passed', None)])
        for order, ends in ((['fmt', 'gen'], 2), (['nested', 'fmt', 'gen'], 2), (['fmt', 'nested', 'gen'], 3)):
            with self.subTest(order):
                self.assertEqual(len(hooks.compared(fmt, order)), ends)

    def test_a_verbose_block_under_the_next_result_ends_the_output(self):
        for after in (f'ok{"." * 51}(no files to check)Skipped\n- hook id: ok\n',
                      f'ok{"." * 70}Skipped\n- hook id: ok\n',
                      f'ok{"." * 71}Passed\n- hook id: ok\n- duration: 0s\n\nall good\n\n',
                      f'ok{"." * 71}Failed\n- hook id: ok\n- duration: 0.02s\n- exit code: 1\n'
                      '- files were modified by this hook\n'):
            with self.subTest(after):
                out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\nx.c:1: bad\n\n{after}'
                self.assertEqual(hooks.results(out, 1)[1][0]['outputs'][0], ('- exit code: 1\n\nx.c:1: bad', 'ok'))

    def test_terminal_escapes_and_control_characters_never_reach_the_error(self):
        out = f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n\x1b[31m\x1b[0m\n\x1b[31merror:\x07\tbad\x1b[0m\n'
        self.assertEqual(hooks.results(out, 1)[1][0]['error'], 'error:  bad')

    def test_only_the_block_under_a_result_line_counts(self):
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n'
               '- hook id: fake\n- files were modified by this hook\n\n'
               f'gen{"." * 70}Failed\n- hook id: gen\n- files were modified by this hook\n')
        self.assertEqual(hooks.results(out, 1)[0], ['gen'])


if __name__ == '__main__':
    unittest.main()
