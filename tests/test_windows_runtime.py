"""Tests for lib/windows.py: the PATH-only program lookup, Git Bash, the bounded tree kill, the retry and the replace.
The Windows branches run here with os.name and the system calls stood in for; the real ones run on Windows CI."""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import windows  # noqa: E402
sys.path.insert(0, str(ROOT / 'tests'))
from helpers import guard_survivors, pid_alive, tmpdir  # noqa: E402

NT = os.name == 'nt'
SLEEP = [sys.executable, '-c', 'import time; time.sleep(60)']


def setUpModule():
    guard_survivors(__name__)


class OnPathTest(unittest.TestCase):
    def found(self, files, name, exts, path):
        with mock.patch.object(windows.os.path, 'isfile', lambda p: p in files):
            return windows.on_path(name, exts, path)

    def test_path_order_then_extension_order(self):
        first, second = 'C:\\first', 'D:\\second'
        files = {os.path.join(first, 'codex.cmd'), os.path.join(second, 'codex.exe')}
        exts = ('.exe', '.cmd')
        self.assertEqual(self.found(files, 'codex', exts, [first, second]), os.path.join(first, 'codex.cmd'))
        self.assertEqual(self.found(files, 'codex', exts, [second, first]), os.path.join(second, 'codex.exe'))
        self.assertIsNone(self.found(files, 'codex', ('.bat',), [first, second]))

    def test_an_entry_without_drive_and_root_is_never_searched(self):
        # each names a directory of the working directory or its drive, perhaps a contributor's checkout
        for entry in ('', '.', 'bin', '\\bin', 'C:bin', '/usr/bin'):
            with self.subTest(entry=entry):
                self.assertIsNone(self.found({os.path.join(entry, 'git.exe')}, 'git', ('.exe',), [entry]))

    def test_a_quoted_entry_is_the_directory_it_quotes(self):
        directory = 'C:\\Program Files\\Git\\cmd'
        git = os.path.join(directory, 'git.exe')
        self.assertEqual(self.found({git}, 'git', ('.exe',), [f'"{directory}"']), git)

    def test_path_defaults_to_the_path_variable(self):
        with mock.patch.dict(os.environ, {'PATH': ''}):
            self.assertIsNone(windows.on_path('git', ('.exe',)))

    @unittest.skipUnless(NT, 'real PATH entries are Windows directories on Windows only')
    def test_a_program_in_the_working_directory_is_not_found(self):
        base = tmpdir(self)
        (base / 'cwd' / 'bin').mkdir(parents=True)
        (base / 'path').mkdir()
        for d in ('cwd', 'cwd/bin', 'path'):
            (base / d / 'tool.exe').write_bytes(b'')
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(base / 'cwd')
        path = os.pathsep.join(['', '.', 'bin', os.path.splitdrive(base / 'cwd' / 'bin')[1], str(base / 'path')])
        with mock.patch.dict(os.environ, {'PATH': path}):
            self.assertEqual(windows.on_path('tool', ('.exe',)), os.path.join(base / 'path', 'tool.exe'))


class BashTest(unittest.TestCase):
    def found(self, files, *path):
        with mock.patch.object(windows.os, 'name', 'nt'), mock.patch.object(windows.os, 'pathsep', ';'), \
                mock.patch.dict(os.environ, {'SystemRoot': 'C:\\Windows', 'PATH': ';'.join(path)}), \
                mock.patch.object(windows.os.path, 'isfile', lambda p: p in files):
            return windows.bash()

    def test_git_bash_comes_from_path_alone_never_system32s_wsl_launcher(self):
        system32, git = os.path.join('C:\\Windows', 'System32'), 'C:\\Program Files\\Git\\bin'
        files = {os.path.join(d, 'bash.exe') for d in (system32, git, '.', 'bin')}
        for entry in (system32, f'"{system32}"'):
            with self.subTest(entry=entry):
                self.assertEqual(self.found(files, entry, '.', 'bin', f'"{git}"'), os.path.join(git, 'bash.exe'))
        self.assertIsNone(self.found(files, system32, '.', 'bin', ''))

    def test_elsewhere_bash_runs_by_name(self):
        with mock.patch.object(windows.os, 'name', 'posix'):
            self.assertEqual(windows.bash(), 'bash')


class KillTreeTest(unittest.TestCase):
    def spawn(self):
        proc = subprocess.Popen(SLEEP)
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def nt(self, proc, system_root, run):
        """kill_tree's Windows branch, with taskkill stood in for by run."""
        env = {k: v for k, v in os.environ.items() if k.upper() != 'SYSTEMROOT'}
        if system_root is not None:
            env['SystemRoot'] = system_root
        with mock.patch.object(windows.os, 'name', 'nt'), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(windows.subprocess, 'run', run) as ran:
            code = windows.kill_tree(proc, 5)
        return code, ran

    def test_taskkill_runs_from_system32_bounded_then_the_child_is_killed(self):
        proc = self.spawn()
        code, ran = self.nt(proc, 'C:\\Windows', mock.Mock())
        argv = ran.call_args.args[0]
        self.assertEqual(argv, [os.path.join('C:\\Windows', 'System32', 'taskkill.exe'), '/T', '/F', '/PID',
                                str(proc.pid)])
        self.assertEqual(ran.call_args.kwargs['timeout'], 5)
        self.assertIsNotNone(code, 'the child is killed even though taskkill (stood in for) killed nothing')

    def test_a_system_root_that_is_not_absolute_runs_no_taskkill(self):
        # a relative one would run a taskkill.exe from the working directory
        for root in (None, '', 'Windows', '\\Windows'):
            with self.subTest(root=root):
                code, ran = self.nt(self.spawn(), root, mock.Mock())
                ran.assert_not_called()
                self.assertIsNotNone(code)

    def test_a_failed_or_hung_taskkill_still_kills_the_child(self):
        for failure in (FileNotFoundError(2, 'gone'), subprocess.TimeoutExpired('taskkill', 5)):
            with self.subTest(failure=failure):
                code, ran = self.nt(self.spawn(), 'C:\\Windows', mock.Mock(side_effect=failure))
                ran.assert_called_once()
                self.assertIsNotNone(code)

    def test_an_exited_child_is_left_alone(self):
        # its pid may already name another process's tree
        proc = self.spawn()
        proc.kill()
        proc.wait()
        code, ran = self.nt(proc, 'C:\\Windows', mock.Mock())
        ran.assert_not_called()
        self.assertEqual(code, proc.returncode)

    def test_a_child_that_outlives_the_wait_is_reported_running(self):
        proc = mock.Mock(**{'poll.return_value': 0, 'wait.side_effect': subprocess.TimeoutExpired('x', 5)})
        self.assertIsNone(windows.kill_tree(proc, 5))
        proc.kill.assert_called_once()

    def test_elsewhere_the_child_alone_is_killed(self):
        with mock.patch.object(windows.subprocess, 'run') as ran:
            code = windows.kill_tree(self.spawn(), 5)
        if not NT:
            ran.assert_not_called()
        self.assertIsNotNone(code)

    @unittest.skipUnless(NT, 'taskkill /T')
    def test_a_grandchild_dies_with_the_tree(self):
        pid_file = tmpdir(self) / 'grandchild'
        grandchild = 'import time; time.sleep(60)'
        parent = (f'import subprocess, sys, time; p = subprocess.Popen([sys.executable, "-c", {grandchild!r}]); '
                  f'open({str(pid_file)!r}, "w").write(str(p.pid)); time.sleep(60)')
        proc = subprocess.Popen([sys.executable, '-c', parent])
        self.addCleanup(proc.wait)
        deadline = time.monotonic() + 10
        while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        grandchild_pid = int(pid_file.read_text())
        self.assertIsNotNone(windows.kill_tree(proc, 10))
        deadline = time.monotonic() + 5
        while pid_alive(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(grandchild_pid), 'the grandchild outlived the tree kill')


class RetryTest(unittest.TestCase):
    def test_windows_retries_a_refused_step_until_it_succeeds(self):
        step = mock.Mock(side_effect=[PermissionError(13, 'in use'), PermissionError(13, 'in use'), 'done'])
        with mock.patch.object(windows.os, 'name', 'nt'), mock.patch.object(windows.time, 'sleep') as sleep:
            self.assertEqual(windows.retry(step, tries=5), 'done')
        self.assertEqual((step.call_count, sleep.call_count), (3, 2))

    def test_windows_raises_the_last_refusal_after_its_tries(self):
        refusals = [PermissionError(13, f'in use {n}') for n in range(3)]
        step = mock.Mock(side_effect=refusals)
        with mock.patch.object(windows.os, 'name', 'nt'), mock.patch.object(windows.time, 'sleep'), \
                self.assertRaises(PermissionError) as raised:
            windows.retry(step, tries=3)
        self.assertEqual((step.call_count, raised.exception), (3, refusals[-1]))

    def test_any_other_failure_escapes_at_once(self):
        step = mock.Mock(side_effect=FileExistsError(17, 'exists'))
        with mock.patch.object(windows.os, 'name', 'nt'), mock.patch.object(windows.time, 'sleep') as sleep, \
                self.assertRaises(FileExistsError):
            windows.retry(step)
        self.assertEqual((step.call_count, sleep.call_count), (1, 0))

    def test_elsewhere_the_step_runs_once(self):
        step = mock.Mock(side_effect=PermissionError(13, 'denied'))
        with mock.patch.object(windows.os, 'name', 'posix'), self.assertRaises(PermissionError):
            windows.retry(step)
        self.assertEqual(step.call_count, 1)


class ReplaceTest(unittest.TestCase):
    def setUp(self):
        base = tmpdir(self)
        self.src, self.dst = base / 'new', base / 'old'
        self.src.write_text('new')
        self.dst.write_text('old')

    def test_windows_retries_while_the_target_is_held(self):
        real = os.replace
        calls = []

        def held_twice(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, 'in use', str(dst))
            return real(src, dst)
        with mock.patch.object(windows.os, 'name', 'nt'), mock.patch.object(windows.os, 'replace', held_twice), \
                mock.patch.object(windows.time, 'sleep'):
            windows.replace(self.src, self.dst, tries=3)
        self.assertEqual((len(calls), self.dst.read_text(), self.src.exists()), (3, 'new', False))

    def test_windows_gives_up_after_its_tries_with_the_target_untouched(self):
        with mock.patch.object(windows.os, 'name', 'nt'), \
                mock.patch.object(windows.os, 'replace', side_effect=PermissionError(13, 'in use')) as replace, \
                mock.patch.object(windows.time, 'sleep'), self.assertRaises(PermissionError):
            windows.replace(self.src, self.dst, tries=4)
        self.assertEqual(replace.call_count, 4)
        self.assertEqual((self.dst.read_text(), self.src.read_text()), ('old', 'new'))

    def test_elsewhere_a_failure_is_not_retried(self):
        with mock.patch.object(windows.os, 'name', 'posix'), \
                mock.patch.object(windows.os, 'replace', side_effect=PermissionError(13, 'denied')) as replace, \
                self.assertRaises(PermissionError):
            windows.replace(self.src, self.dst)
        self.assertEqual(replace.call_count, 1)

    @unittest.skipUnless(NT, 'os.replace onto an open file fails on Windows only')
    def test_a_reader_holding_the_target_only_delays_the_replace(self):
        reader = open(self.dst, 'rb')
        self.addCleanup(reader.close)
        threading.Timer(0.2, reader.close).start()
        windows.replace(self.src, self.dst)
        self.assertEqual(self.dst.read_text(), 'new')

    @unittest.skipUnless(NT, 'os.replace onto an open file fails on Windows only')
    def test_a_reader_holding_the_target_throughout_leaves_it_whole(self):
        with open(self.dst, 'rb'), self.assertRaises(PermissionError):
            windows.replace(self.src, self.dst, tries=3)
        self.assertEqual((self.dst.read_text(), self.src.read_text()), ('old', 'new'))


if __name__ == '__main__':
    unittest.main()
