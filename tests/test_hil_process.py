"""skills/hil/scripts/hil_process.py is the bounded runner the HIL harnesses share, moved from
tinyusb's test/hil/helper/hil_util.py (c5099be4f) with its tests: text/bytes and merged/split
output, rc 124 on the kill, the process group killed on timeout and on an interrupt that lands
anywhere around the fork, and an explicit timeout. Real subprocesses, no hardware."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_process needs POSIX sessions and process groups')

import ast
import importlib.util
import inspect
import io
import json
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import contextmanager, redirect_stdout, suppress
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts'


def load(name):
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hp = load('hil_process')
uo = load('usb_observe')
T = 20   # a bound no test here should reach


class RunCmdModes(unittest.TestCase):
    def test_default_mode_merges_stderr(self):
        r = hp.run_cmd('printf out; printf err >&2', timeout=T)
        self.assertEqual(r.returncode, 0)
        self.assertIsInstance(r.stdout, str)
        self.assertIn('out', r.stdout)
        self.assertIn('err', r.stdout)
        self.assertIsNone(r.stderr)

    def test_argv_is_not_parsed_by_a_shell(self):
        r = hp.run_cmd(['printf', '%s', 'a b; echo no'], timeout=T)
        self.assertEqual((r.returncode, r.stdout), (0, 'a b; echo no'))

    def test_cwd_is_honoured(self):
        with tempfile.TemporaryDirectory() as d:
            r = hp.run_cmd('pwd', d, timeout=T)
        self.assertEqual(r.stdout.strip(), os.path.realpath(d))

    def test_binary_stdout_is_exact_bytes(self):
        # \xff is not valid UTF-8: text mode would mangle it via errors='replace'
        r = hp.run_cmd(r"printf 'a\377\000b'", binary=True, timeout=T)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b'a\xff\x00b')

    def test_split_stderr_keeps_stdout_clean(self):
        r = hp.run_cmd('printf out; printf err >&2', split_stderr=True, timeout=T)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, 'out')
        self.assertEqual(r.stderr, 'err')

    def test_binary_split_stderr_timeout_returns_124(self):
        t0 = time.monotonic()
        with redirect_stdout(io.StringIO()):
            r = hp.run_cmd(r"printf 'p\377re'; printf warn >&2; sleep 30",
                           binary=True, split_stderr=True, timeout=0.2)
        self.assertEqual(r.returncode, 124)
        # killpg + bounded communicate: well under sleep 30
        self.assertLess(time.monotonic() - t0, 15)
        self.assertIn(b'p\xffre', r.stdout or b'')
        # stderr collected before the timeout must survive the kill
        self.assertIn(b'warn', r.stderr or b'')

    def test_text_mode_timeout_stdout_stays_str(self):
        with redirect_stdout(io.StringIO()):
            r = hp.run_cmd('sleep 30', timeout=0.2)
        self.assertEqual(r.returncode, 124)
        # a text-mode caller must never get bytes back, even empty
        self.assertIsInstance(r.stdout, str)
        self.assertIsNone(r.stderr)

    def test_split_stderr_timeout_stderr_is_never_none(self):
        with redirect_stdout(io.StringIO()) as cap:
            r = hp.run_cmd('sleep 30', split_stderr=True, timeout=0.2)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (124, '', ''))
        self.assertIn('COMMAND TIMEOUT (0.2s): sleep 30', cap.getvalue())

    def test_failed_banner_includes_split_stderr(self):
        # with split_stderr the diagnostic is in .stderr; the banner must not go blank.
        # The text travels via env, not the command string — the banner title echoes the
        # command, which would make a literal assertion pass vacuously.
        cap = io.StringIO()
        with mock.patch.dict(os.environ, {'RUN_CMD_TEST_ERR': 'diagnostic-xyzzy'}), redirect_stdout(cap):
            r = hp.run_cmd('printf "$RUN_CMD_TEST_ERR" >&2; exit 3', split_stderr=True, timeout=T)
        self.assertEqual(r.returncode, 3)
        self.assertIn('COMMAND FAILED', cap.getvalue())
        self.assertIn('diagnostic-xyzzy', cap.getvalue())

    def test_no_group_markers_when_stdout_is_captured(self):
        # GitHub folds ::group:: only at line start of the JOB's real stdout. Pool
        # workers run tests under redirect_stdout and compact the capture into one
        # row line, where the markers land mid-line and render as literal noise.
        cap = io.StringIO()
        with mock.patch.dict(os.environ, {'CI': '1'}), redirect_stdout(cap):
            r = hp.run_cmd('printf boom; exit 3', timeout=T)
        self.assertEqual(r.returncode, 3)
        self.assertIn('COMMAND FAILED', cap.getvalue())
        self.assertNotIn(hp.GROUP_MARK, cap.getvalue())
        self.assertNotIn(hp.ENDGROUP_MARK, cap.getvalue())

    def test_quiet_suppresses_failed_banner(self):
        # retry-loop callers report failures themselves; per-poll banners are noise
        cap = io.StringIO()
        with redirect_stdout(cap):
            r = hp.run_cmd('printf boom >&2; exit 3', quiet=True, timeout=T)
        self.assertEqual(r.returncode, 3)
        self.assertNotIn('COMMAND FAILED', cap.getvalue())

    def test_verbose_echoes_a_success_only_when_asked(self):
        for verbose, want in ((False, ''), (True, 'printf hi\nhi\n')):
            cap = io.StringIO()
            with redirect_stdout(cap):
                hp.run_cmd('printf hi', timeout=T, verbose=verbose)
            self.assertEqual(cap.getvalue(), want)

    def test_a_long_argv_payload_is_named_not_dumped(self):
        body = 'x' * 61
        self.assertEqual(hp._cmd_label(['python3', '-c', body]), 'python3 -c <61-char program>')

    def test_strip_workflow_markers(self):
        self.assertEqual(hp.strip_workflow_markers(f'{hp.GROUP_MARK}title'), 'title')
        self.assertEqual(hp.strip_workflow_markers(f'{hp.ENDGROUP_MARK}'), '')
        self.assertEqual(hp.cmd_stdout_text(None), '')
        self.assertEqual(hp.cmd_stdout_text(b'a\xffb'), 'ab')


class Stuck:
    """A child that never finishes: every communicate() times out, its bound kept in `waits`."""
    pid = 999999999
    stdout = stderr = stdin = None
    waits = None

    def __init__(self, *a, **k):
        pass

    def communicate(self, timeout):
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired('x', timeout, output=b'partial')


class RootOwned(Stuck):
    """An all-root group's child: p.kill() is refused as killpg is."""

    def kill(self):
        raise PermissionError(1, 'Operation not permitted')


class ExplicitBounds(unittest.TestCase):
    """The provider holds no bound or switch of its own: every caller states its timeout."""

    def test_timeout_has_no_default(self):
        with self.assertRaises(TypeError):
            hp.run_cmd('true')
        with self.assertRaises(ValueError):
            hp.run_cmd('true', timeout=None)

    def test_no_module_knobs(self):
        for name in ('CMD_TIMEOUT', 'verbose', 'TINYUSB_ROOT'):
            self.assertFalse(hasattr(hp, name), name)

    def test_reap_grace_bounds_the_post_kill_reap(self):
        waits = []
        for fn in (lambda: hp.run_cmd('x', timeout=3, reap_grace=0.5),
                   lambda: hp.run_alongside(['x'], lambda: None, 3, reap_grace=0.5)):
            waits.clear()
            with mock.patch.object(hp.subprocess, 'Popen', Stuck), redirect_stdout(io.StringIO()), \
                    mock.patch.object(Stuck, 'waits', waits), mock.patch.object(hp.os, 'killpg'):
                r = fn()
            self.assertEqual((r.returncode, waits), (124, [3, 0.5]))

    def test_a_refused_kill_still_reaps_and_returns_124(self):
        """An all-root group refuses killpg AND p.kill(); a raise there skipped the bounded
        reap and masked work()'s own error."""
        waits = []

        def boom():
            raise AssertionError('the write failed')
        with mock.patch.object(hp.subprocess, 'Popen', RootOwned), mock.patch.object(RootOwned, 'waits', waits), \
                mock.patch.object(hp.os, 'killpg', side_effect=PermissionError(1, 'Operation not permitted')):
            r = hp.run_alongside(['x'], lambda: None, 3, reap_grace=0.5)
            self.assertEqual((r.returncode, waits), (124, [3, 0.5]))
            with self.assertRaisesRegex(AssertionError, 'the write failed'):
                hp.run_alongside(['x'], boom, 3, reap_grace=0.5)

    def test_the_stdlib_closure(self):
        # a project loads this on a bare CI runner: stdlib only, at module level
        for name in ('hil_process', 'hil_flash'):
            tree = ast.parse((SCRIPTS / f'{name}.py').read_text())
            for node in tree.body:
                roots = []
                if isinstance(node, ast.Import):
                    roots = [a.name.split('.')[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots = [node.module.split('.')[0]]
                for root in roots:
                    self.assertIn(root, sys.stdlib_module_names | {'__future__'}, f'{name} imports {root}')


class RunAlongsideKeepsStderrOffThePayload(unittest.TestCase):
    """A caller byte-compares run_alongside's stdout against the payload it wrote. Merging
    stderr into that stream turns any stray child stderr byte -- a PYTHONWARNINGS chirp, a
    sitecustomize print, a venv .pth deprecation -- into a data mismatch."""

    def test_child_stderr_does_not_contaminate_stdout(self):
        argv = [sys.executable, '-c',
                'import sys; sys.stderr.write("noise\\n"); sys.stdout.write("PAYLOAD")']
        r = hp.run_alongside(argv, lambda: time.sleep(0.2), timeout=T)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b'PAYLOAD', 'child stderr leaked into the payload stream')
        self.assertEqual(r.stderr, b'noise\n')

    def test_a_timeout_returns_124(self):
        r = hp.run_alongside(['sleep', '30'], lambda: None, 0.2)
        self.assertEqual(r.returncode, 124)


class RunWhileContract(unittest.TestCase):
    """The read-while-we-write runner (tinyusb test_hil_bounded.py RunWhileContract)."""

    def test_an_error_in_work_is_not_swallowed(self):
        """A `return` inside the reap's `finally` discarded it: an assert in the write half
        vanished and the caller went on to compare data it never sent."""
        def boom():
            raise AssertionError('the write failed')
        with self.assertRaises(AssertionError):
            hp.run_alongside(['sh', '-c', 'printf X'], boom, 5)

    def test_the_child_is_reaped_even_when_work_raises(self):
        procs = []
        real_popen = subprocess.Popen

        def spy(*a, **k):
            procs.append(real_popen(*a, **k))
            return procs[-1]

        def boom():
            raise AssertionError('x')
        with mock.patch.object(hp.subprocess, 'Popen', spy), self.assertRaises(AssertionError):
            hp.run_alongside(['sleep', '30'], boom, 0.2)
        self.assertIsNotNone(procs[0].returncode, 'work() raising leaked the child')

    def test_the_child_is_in_its_own_session(self):
        """killpg on it reaps whatever it spawned, and it cannot take our group with it."""
        pgids = {}
        real_popen = subprocess.Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            pgids['pid'] = p.pid
            return p

        def check():
            time.sleep(0.2)
            pgids['child'] = os.getpgid(pgids['pid'])
        with mock.patch.object(hp.subprocess, 'Popen', spy):
            hp.run_alongside(['sleep', '0.5'], check, 5)
        self.assertEqual(pgids['child'], pgids['pid'])
        self.assertNotEqual(pgids['child'], os.getpgid(0))


class RunCmdCleanupShape(unittest.TestCase):
    """run_cmd's two cleanup paths, asserted structurally on _run, its body.

    Both must kill the process GROUP: start_new_session puts the child in its own group, so
    a flasher run through a shell keeps children a p.kill() cannot reach, and on the
    BaseException path the child never receives the terminal's SIGINT either. Both times
    this block was mis-edited, an `else:` ended up attached to the `try` instead of the `if`
    it belonged to, so `p.kill()` ran when killpg had SUCCEEDED and its ProcessLookupError
    masked the caller's exception: a shape an AST can pin.
    """

    def _ast(self, name='_run'):
        src = Path(hp.__file__).read_text()
        return next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    _run_cmd_ast = _ast

    @staticmethod
    def _calls(tree):
        return [getattr(c.func, 'attr', getattr(c.func, 'id', '')) for c in ast.walk(tree) if isinstance(c, ast.Call)]

    def test_run_cmd_is_the_shared_body_and_nothing_more(self):
        self.assertEqual(self._calls(self._ast('run_cmd')), ['_run'])

    def test_no_cleanup_try_has_an_else(self):
        for name in ('_run', '_kill_group'):
            for n in ast.walk(self._ast(name)):
                if isinstance(n, ast.Try) and n.orelse:
                    self.fail(f'{name}: try/else at line {n.lineno}: an else here runs when the kill SUCCEEDED')

    def test_both_cleanup_paths_kill_the_group(self):
        killers = self._calls(self._run_cmd_ast())
        self.assertEqual(killers.count('_kill_group'), 2,
                         'both the timeout and the BaseException path must kill the group')
        self.assertEqual(killers.count('kill') + killers.count('killpg'), 0,
                         'p.kill() reaches only the direct child')
        group = self._calls(self._ast('_kill_group'))
        self.assertEqual((group.count('killpg'), group.count('kill')), (1, 0))

    def test_the_interrupt_path_reraises(self):
        base = [h for n in ast.walk(self._run_cmd_ast()) if isinstance(n, ast.Try) for h in n.handlers
                if isinstance(h.type, ast.Name) and h.type.id == 'BaseException']
        self.assertTrue(base, 'the BaseException cleanup path is gone')
        for h in base:
            self.assertTrue(any(isinstance(x, ast.Raise) for x in ast.walk(h)),
                            'the interrupt path must re-raise, or Ctrl-C is swallowed')


def _live_in_group(pgid: int) -> list:
    """Pids in process group `pgid` that are neither zombie nor dead."""
    live = []
    for d in os.listdir('/proc'):
        st = d.isdigit() and uo._proc_stat(d)
        if st and st[1] not in 'ZX' and st[4] == str(pgid):
            live.append(int(d))
    return live


@unittest.skipUnless(os.path.isdir('/proc'), 'reads process groups from /proc')
class InterruptAcrossTheFork(unittest.TestCase):
    """tinyusb #4022: Popen forks before the cleanup `try` is entered, so a SIGINT/SIGTERM
    handled inside Popen, or between its return and the `try`, unwound with the child alive.
    Each case spawns a shell holding a grandchild and checks that no member of its process
    group survives."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.pidfile = os.path.join(tmp, 'grandchild')
        self.shell_cmd = f'sleep 30 & echo $! > {shlex.quote(self.pidfile)}; wait'
        self.shell_argv = ['sh', '-c', 'sleep 30 & echo $! > "$1"; wait', 'sh', self.pidfile]
        self.procs = []
        self.addCleanup(self._kill_leftovers)
        prev = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, prev)

    def _kill_leftovers(self):
        # the unfixed code leaves the group alive; never leak it past the failing test
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
            hp._close_pipes(p)

    def _wait_for_grandchild(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if Path(self.pidfile).read_text().endswith('\n'):
                    return
            except OSError:
                pass
            time.sleep(0.005)
        self.fail('the shell never started its grandchild')

    def _assert_group_gone(self, pgid):
        deadline = time.monotonic() + 5
        while _live_in_group(pgid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(_live_in_group(pgid), [],
                         'a child outlived the interrupt that unwound its runner')

    @contextmanager
    def _popen_hook(self, before_return=None):
        """Popen that forks the real child, waits until its grandchild exists, then runs
        `before_return(p)` -- still INSIDE Popen as far as the caller can tell."""
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            p = real_popen(*args, **kwargs)
            self.procs.append(p)
            self._wait_for_grandchild()
            if before_return:
                before_return(p)
            return p

        with mock.patch.object(hp.subprocess, 'Popen', popen):
            yield

    @contextmanager
    def _signal_in(self, fn, sig, fire):
        """Deliver `sig` once, from a trace hook in `fn`'s frame, at the first line or
        opcode event `fire(event, frame)` accepts: bytecode no patched callable reaches."""
        done = []

        def local(frame, event, arg):
            if not done and fire(event, frame):
                done.append(True)
                signal.raise_signal(sig)
            return local

        def tracer(frame, event, arg):
            if frame.f_code is fn.__code__:
                frame.f_trace = local   # 3.13 enables opcode events only on a traced frame
                frame.f_trace_opcodes = True
                return local
            return None

        prev = sys.gettrace()
        sys.settrace(tracer)
        try:
            yield
        finally:
            sys.settrace(prev)

    @contextmanager
    def _signal_after_popen_returns(self, fn, sig):
        """Deliver `sig` at the first instruction `fn` runs after Popen returns, between
        the call and the `try`."""
        armed = []
        with self._popen_hook(lambda p: armed.append(True)), \
                self._signal_in(fn, sig, lambda event, frame: event == 'opcode' and armed):
            yield

    def _sigterm_raises(self):
        def handler(sig, frame):
            raise SystemExit(128 + sig)
        prev = signal.signal(signal.SIGTERM, handler)
        self.addCleanup(signal.signal, signal.SIGTERM, prev)

    def test_run_cmd_sigint_inside_popen(self):
        with self._popen_hook(lambda p: signal.raise_signal(signal.SIGINT)):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_cmd(self.shell_cmd, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_cmd_sigterm_inside_popen(self):
        self._sigterm_raises()
        with self._popen_hook(lambda p: signal.raise_signal(signal.SIGTERM)):
            with self.assertRaises(SystemExit):
                hp.run_cmd(self.shell_cmd, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_cmd_sigint_right_after_popen_returns(self):
        with self._signal_after_popen_returns(hp._run, signal.SIGINT):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_cmd(self.shell_cmd, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_cmd_sigterm_right_after_popen_returns(self):
        self._sigterm_raises()
        with self._signal_after_popen_returns(hp._run, signal.SIGTERM):
            with self.assertRaises(SystemExit):
                hp.run_cmd(self.shell_cmd, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    @contextmanager
    def _never_reaped(self):
        """A deferred interrupt is killed at once: no reap waits out the timeout first. Enter
        it before _popen_hook, which replaces subprocess.Popen."""
        with mock.patch.object(subprocess.Popen, 'communicate', autospec=True,
                               side_effect=subprocess.Popen.communicate) as comm:
            yield
        self.assertEqual(comm.call_count, 0, 'the deferred interrupt waited in a reap')

    def test_run_alongside_sigint_inside_popen(self):
        with self._never_reaped(), self._popen_hook(lambda p: signal.raise_signal(signal.SIGINT)):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_alongside(self.shell_argv, lambda: None, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_alongside_sigint_right_after_popen_returns(self):
        with self._never_reaped(), \
                self._signal_after_popen_returns(hp.run_alongside, signal.SIGINT):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_alongside(self.shell_argv, lambda: None, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_alongside_sigint_while_reaping(self):
        # the reap after work() is a blocking wait of up to `timeout`; it once ran outside any try
        def interrupt_first_wait(p):
            real = p.communicate

            def communicate(*args, **kwargs):
                signal.raise_signal(signal.SIGINT)
                return real(*args, **kwargs)
            p.communicate = communicate

        with self._popen_hook(interrupt_first_wait):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_alongside(self.shell_argv, lambda: None, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_alongside_sigint_between_work_and_reap(self):
        src, first = inspect.getsourcelines(hp.run_alongside)
        reap_line = next(first + i for i, line in enumerate(src)
                         if line.strip() == 'return _reap()')
        with self._popen_hook(), \
                self._signal_in(hp.run_alongside, signal.SIGINT,
                                lambda event, frame: event == 'line'
                                and frame.f_lineno == reap_line):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_alongside(self.shell_argv, lambda: None, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_alongside_sigint_after_the_leader_is_reaped(self):
        # CPython's communicate() reaps the direct child on KeyboardInterrupt before
        # re-raising, so a set returncode says nothing about the rest of the group
        argv = ['sh', '-c', 'sleep 30 & echo $! > "$1"; exit 0', 'sh', self.pidfile]

        def reap_leader_then_interrupt(p):
            def communicate(*args, **kwargs):
                p.wait()
                signal.raise_signal(signal.SIGINT)
            p.communicate = communicate

        with self._popen_hook(reap_leader_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_alongside(argv, lambda: None, timeout=T)
        p = self.procs[0]
        self.assertEqual(p.returncode, 0)
        self._assert_group_gone(p.pid)
        self.assertTrue(p.stdout.closed and p.stderr.closed, 'our pipe ends leaked')

    def test_run_alongside_sigint_in_work_is_killed_without_a_reap(self):
        # a reap first would wait out the whole timeout before Ctrl-C returned
        def work():
            signal.raise_signal(signal.SIGINT)
        with self._never_reaped(), self._popen_hook():
            with self.assertRaises(KeyboardInterrupt):
                hp.run_alongside(self.shell_argv, work, timeout=T)
        p = self.procs[0]
        self._assert_group_gone(p.pid)
        self.assertTrue(p.stdout.closed and p.stderr.closed, 'our pipe ends leaked')

    def test_run_cmd_sigint_in_the_post_kill_reap_closes_the_pipes(self):
        def interrupt_second_wait(p):
            real, calls = p.communicate, []

            def communicate(*args, **kwargs):
                calls.append(1)
                if len(calls) == 2:
                    signal.raise_signal(signal.SIGINT)
                return real(*args, **kwargs)
            p.communicate = communicate

        with self._popen_hook(interrupt_second_wait), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_cmd(self.shell_cmd, timeout=0.2, split_stderr=True)
        p = self.procs[0]
        self._assert_group_gone(p.pid)
        self.assertTrue(p.stdout.closed and p.stderr.closed, 'our pipe ends leaked')

    def test_sigterm_while_holding_sigint_restores_it(self):
        # SIGTERM's raising handler fires right after SIGINT's handler was swapped, before
        # SIGTERM's is: the constructor's cleanup must still put SIGINT's back
        self._sigterm_raises()
        real_signal = signal.signal

        def swap(sig, handler):
            old = real_signal(sig, handler)
            if sig == signal.SIGINT and handler is not signal.default_int_handler:
                signal.raise_signal(signal.SIGTERM)
            return old

        with mock.patch.object(hp.signal, 'signal', swap):
            with self.assertRaises(SystemExit):
                hp.run_cmd('true', timeout=T)
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        with self.assertRaises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)

    def test_timeout_kills_the_grandchild(self):
        with self._popen_hook():
            with redirect_stdout(io.StringIO()):
                r = hp.run_cmd(self.shell_cmd, timeout=0.2)
        self.assertEqual(r.returncode, 124)
        self._assert_group_gone(self.procs[0].pid)

    def test_handlers_are_restored(self):
        before = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        self.assertEqual(hp.run_cmd('true', timeout=T).returncode, 0)
        with self.assertRaises(FileNotFoundError):
            hp.run_cmd(['/nonexistent/hil-run-cmd'], timeout=T)
        r = hp.run_alongside(['true'], lambda: None, timeout=T)
        self.assertEqual(r.returncode, 0)
        after = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        self.assertIs(after[0], before[0])
        self.assertIs(after[1], before[1])

    def test_runs_off_the_main_thread(self):
        # signal.signal raises ValueError off the main thread; handlers never run there
        results = []
        t = threading.Thread(target=lambda: results.append(
            (hp.run_cmd('printf ok', timeout=T),
             hp.run_alongside(['printf', 'ok'], lambda: None, timeout=T))))
        t.start()
        t.join(30)
        self.assertEqual(len(results), 1, 'run_cmd raised off the main thread')
        r, ra = results[0]
        self.assertEqual((r.returncode, r.stdout), (0, 'ok'))
        self.assertEqual((ra.returncode, ra.stdout), (0, b'ok'))

# The prelude of every run_tracked case: hil_process loaded by file path as `hp`, the case's
# state dir as `S`, scratch dir `D`, and report(), whose JSON line the test reads back.
TRACKED_CHILD = """
import ctypes, glob, importlib.util, json, os, shlex, signal, subprocess, sys, threading, time
spec = importlib.util.spec_from_file_location('agentrc_hil_process', sys.argv[1])
hp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hp)
D = sys.argv[2]
S = os.path.join(D, 'state')
os.mkdir(S, 0o700)


def report(**kw):
    print('\\nRESULT ' + json.dumps(kw), flush=True)


def kill(*pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, TypeError):
            pass


def wait_file(path, budget=10):
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        try:
            with open(path) as f:
                text = f.read()
            if text.endswith('\\n'):
                return int(text)
        except OSError:
            pass
        time.sleep(0.005)
    raise SystemExit(f'{path} never written')


def settled(receipt, want='done', budget=10):
    deadline = time.monotonic() + budget
    while hp.recheck(receipt) != want and time.monotonic() < deadline:
        time.sleep(0.02)
    return hp.recheck(receipt)


def subreaper():
    v = ctypes.c_int()
    ctypes.CDLL(None).prctl(37, ctypes.byref(v), 0, 0, 0)     # PR_GET_CHILD_SUBREAPER
    return v.value


def surveyed(receipt):
    return [p['pid'] for p in ((receipt or {}).get('survivors') or {}).get('processes', ())]
"""

# forks a child that starts its own session and exits at once, over and over, so at any moment
# only the newest hop lives; the last one writes its pid to argv[1] and sleeps
HOPPER = ('import os, sys, time\n'
          'end = time.monotonic() + float(sys.argv[2])\n'
          'while time.monotonic() < end:\n'
          '    if os.fork():\n'
          '        os._exit(0)\n'
          '    os.setsid()\n'
          'open(sys.argv[1], "w").write(f"{os.getpid()}\\n")\n'
          'time.sleep(30)\n')

# a process whose main thread leaves by pthread_exit while another thread lives: a zombie
# thread-group leader that is still running
THREAD_LEADER = ('import ctypes, os, sys, threading, time\n'
                 'threading.Thread(target=time.sleep, args=(30,)).start()\n'
                 'open(sys.argv[1], "w").write(f"{os.getpid()}\\n")\n'
                 'ctypes.CDLL(None).pthread_exit(None)\n')


@unittest.skipUnless(os.path.isdir('/proc'), 'reads processes from /proc')
class RunTracked(unittest.TestCase):
    """run_tracked's completion evidence from its operation-owned supervisor, with real
    processes, each case in a fresh interpreter. Every case kills what it leaves behind; the
    test kills anything it reported too."""

    def child(self, body: str, timeout=90) -> dict:
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, '-c', TRACKED_CHILD + textwrap.dedent(body),
                                str(SCRIPTS / 'hil_process.py'), d], capture_output=True, text=True, timeout=timeout)
        line = next((x for x in r.stdout.splitlines() if x.startswith('RESULT ')), None)
        self.assertIsNotNone(line, f'rc {r.returncode}\n{r.stdout}\n{r.stderr}')
        res = json.loads(line[len('RESULT '):])
        for pid in res.get('leftovers', ()):
            with suppress(ProcessLookupError, TypeError):
                os.kill(pid, signal.SIGKILL)
        res['stdout'] = r.stdout
        return res

    def test_a_clean_exit_is_done_and_leaves_no_files(self):
        res = self.child("""
            r = hp.run_tracked('printf ok', timeout=20, settle=5, state_dir=S)
            r2 = hp.run_tracked(['sh', '-c', 'exit 3'], timeout=20, settle=5, state_dir=S, quiet=True)
            r3 = hp.run_tracked(r"printf 'a\\377'; printf e >&2", timeout=20, settle=5, state_dir=S, binary=True,
                                split_stderr=True)
            report(a=[r.returncode, r.stdout, r.stderr, r.cleanup, r.receipt], b=[r2.returncode, r2.cleanup, r2.receipt],
                   c=[r3.stdout.hex(), r3.stderr.hex(), r3.cleanup], files=os.listdir(S), subreaper=subreaper(),
                   handlers=[signal.getsignal(signal.SIGINT) is signal.default_int_handler,
                             signal.getsignal(signal.SIGTERM) is signal.SIG_DFL])
        """)
        self.assertEqual(res['a'], [0, 'ok', None, 'done', None])
        self.assertEqual(res['b'], [3, 'done', None])
        self.assertEqual(res['c'], [b'a\xff'.hex(), b'e'.hex(), 'done'])
        self.assertEqual(res['files'], [])
        self.assertEqual(res['subreaper'], 0, 'the caller became a subreaper')
        self.assertEqual(res['handlers'], [True, True], "the caller's handlers were not restored")

    def test_a_descendant_left_behind_is_pending_until_it_ends(self):
        res = self.child("""
            r = hp.run_tracked('sleep 30 </dev/null >/dev/null 2>&1 & echo $!', timeout=20, settle=0.5, state_dir=S)
            pid = int(r.stdout)
            before = hp.recheck(r.receipt)
            kill(pid)
            report(rc=r.returncode, pid=pid, cleanup=r.cleanup, receipt=r.receipt, before=before,
                   after=settled(r.receipt), subreaper=subreaper(), leftovers=[pid])
        """)
        self.assertEqual((res['rc'], res['cleanup'], res['before'], res['after']), (0, 'pending', 'pending', 'done'))
        self.assertIn(res['pid'], [p['pid'] for p in res['receipt']['survivors']['processes']])
        self.assertIsInstance(res['receipt']['supervisor']['start'], int)
        self.assertEqual(res['subreaper'], 0)

    def test_a_timeout_kill_is_done_once_the_group_is_gone(self):
        res = self.child("""
            t0 = time.monotonic()
            r = hp.run_tracked('sleep 30 & sleep 30; wait', timeout=0.3, settle=5, state_dir=S)
            report(rc=r.returncode, cleanup=r.cleanup, receipt=r.receipt, took=time.monotonic() - t0)
        """)
        self.assertEqual((res['rc'], res['cleanup'], res['receipt']), (124, 'done', None))
        self.assertLess(res['took'], 10)
        self.assertIn('COMMAND TIMEOUT (0.3s): sleep 30 & sleep 30; wait', res['stdout'])

    def test_a_grandchild_in_its_own_session_is_owned(self):
        """It outlives its parent and reparents to the supervisor: pending until it ends."""
        res = self.child("""
            out = []
            for cmd, timeout in (('setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $!', 20),
                                 ('setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $!; sleep 30', 0.5)):
                r = hp.run_tracked(cmd, timeout=timeout, settle=0.5, state_dir=S)
                pid = int(r.stdout.split()[0])
                with open(f'/proc/{pid}/stat') as f:
                    fields = f.read().rpartition(')')[2].split()
                out.append({'rc': r.returncode, 'pid': pid, 'sid': int(fields[3]), 'ppid': int(fields[1]),
                            'cleanup': r.cleanup, 'supervisor': r.receipt['supervisor']['pid'],
                            'surveyed': surveyed(r.receipt)})
                kill(pid)
                out[-1]['after'] = settled(r.receipt)
            report(out=out, leftovers=[o['pid'] for o in out])
        """)
        for case, rc in zip(res['out'], (0, 124)):
            self.assertEqual((case['rc'], case['cleanup'], case['after']), (rc, 'pending', 'done'))
            self.assertEqual(case['sid'], case['pid'], 'the grandchild did not start its own session')
            self.assertEqual(case['ppid'], case['supervisor'], 'not reparented to the supervisor')
            self.assertIn(case['pid'], case['surveyed'])

    def test_a_descendant_forking_away_while_its_parent_exits_is_never_done(self):
        """Each hop forks a new session leader and exits at once, the race that beat a /proc
        scan; the wait sees every reparented hop."""
        res = self.child(f"""
            out = []
            for _ in range(5):
                pidfile = os.path.join(D, 'hop')
                argv = [sys.executable, '-c', {HOPPER!r}, pidfile, '0.3']
                r = hp.run_tracked(shlex.join(argv) + ' </dev/null >/dev/null 2>&1', timeout=20, settle=0.1,
                                   state_dir=S)
                last = wait_file(pidfile)
                out.append({{'cleanup': r.cleanup, 'before': hp.recheck(r.receipt), 'last': last}})
                kill(last)
                out[-1]['after'] = settled(r.receipt)
                os.unlink(pidfile)
            report(out=out, leftovers=[o['last'] for o in out])
        """)
        for case in res['out']:
            self.assertEqual((case['cleanup'], case['before'], case['after']), ('pending', 'pending', 'done'))

    def test_a_zombie_leader_with_a_live_thread_is_pending(self):
        res = self.child(f"""
            pidfile = os.path.join(D, 'leader')
            argv = [sys.executable, '-c', {THREAD_LEADER!r}, pidfile]
            r = hp.run_tracked(shlex.join(argv) + ' </dev/null >/dev/null 2>&1 &', timeout=20, settle=0.5, state_dir=S)
            pid = wait_file(pidfile)
            with open(f'/proc/{{pid}}/stat') as f:
                state = f.read().rpartition(')')[2].split()[0]
            tasks = os.listdir(f'/proc/{{pid}}/task')
            kill(pid)
            report(cleanup=r.cleanup, state=state, tasks=len(tasks), after=settled(r.receipt), leftovers=[pid])
        """)
        self.assertEqual(res['state'], 'Z', 'the leader thread did not exit')
        self.assertGreater(res['tasks'], 1)
        self.assertEqual((res['cleanup'], res['after']), ('pending', 'done'))

    def test_an_unreadable_survey_never_decides(self):
        """The supervisor itself, driven directly with its /proc listing failing: the
        survivor still keeps it pending, and done comes only from its wait."""
        res = self.child("""
            def unreadable(root):
                raise PermissionError(13, 'Permission denied')
            hp._survey = unreadable
            rfd, wfd = os.pipe()
            pidfile = os.path.join(D, 'gc')
            job = {'cmd': f'setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}', 'cwd': None,
                   'timeout': 20, 'settle': 0.3, 'reap_grace': 2, 'binary': False, 'split_stderr': False,
                   'report_fd': wfd, 'token': 't', 'lock': os.path.join(S, 'l'), 'record': os.path.join(S, 'r')}
            msgs = []

            def reader():
                with os.fdopen(rfd) as f:
                    for line in f:
                        msgs.append(json.loads(line))
                        if msgs[-1].get('cleanup') == 'pending':
                            kill(wait_file(pidfile))
            t = threading.Thread(target=reader)
            t.start()
            rc = hp._supervise(job)
            t.join(10)
            with open(job['record']) as f:
                rec = json.load(f)
            report(rc=rc, msgs=[{k: m.get(k) for k in ('cleanup', 'survivors')} for m in msgs if 'result' in m],
                   record=rec['cleanup'], leftovers=[wait_file(pidfile)])
        """)
        self.assertEqual(res['rc'], 0)
        self.assertEqual(res['msgs'], [{'cleanup': 'pending', 'survivors': None}])
        self.assertEqual(res['record'], 'done')

    def test_an_interrupt_during_the_first_wait_kills_and_carries_the_evidence(self):
        res = self.child("""
            def alarm(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGALRM, alarm)
            out = []
            for cmd in ('sleep 30', f'setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {D}/gc; sleep 30'):
                signal.setitimer(signal.ITIMER_REAL, 0.5)
                t0 = time.monotonic()
                try:
                    hp.run_tracked(cmd, timeout=20, settle=0.5, state_dir=S)
                    out.append('not interrupted')
                except KeyboardInterrupt as e:
                    o = {'cleanup': e.cleanup, 'receipt': e.receipt, 'took': time.monotonic() - t0}
                    if e.receipt:
                        o['gc'] = wait_file(f'{D}/gc')
                        o['before'] = hp.recheck(e.receipt)
                        kill(o['gc'])
                        o['after'] = settled(e.receipt)
                    out.append(o)
            report(out=out, leftovers=[o.get('gc') for o in out if isinstance(o, dict)])
        """)
        killed, escaped = res['out']
        self.assertEqual((killed['cleanup'], killed['receipt']), ('done', None))
        self.assertLess(killed['took'], 10)
        self.assertEqual((escaped['cleanup'], escaped['before'], escaped['after']), ('pending', 'pending', 'done'))

    def test_an_interrupt_during_spawn_still_reaches_the_operation(self):
        res = self.child("""
            real = subprocess.Popen

            def popen(*a, **k):
                p = real(*a, **k)
                signal.raise_signal(signal.SIGINT)      # deferred by the hold across the fork
                return p
            hp.subprocess.Popen = popen
            try:
                hp.run_tracked('sleep 30', timeout=20, settle=2, state_dir=S)
                report(error='not interrupted')
            except KeyboardInterrupt as e:
                hp.subprocess.Popen = real
                report(cleanup=e.cleanup, receipt=e.receipt, files=os.listdir(S))
        """)
        self.assertEqual((res['cleanup'], res['receipt'], res['files']), ('done', None, []))

    def test_a_second_interrupt_while_waiting_for_the_report_is_unknown_with_a_receipt(self):
        res = self.child("""
            def alarm(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGALRM, alarm)
            signal.setitimer(signal.ITIMER_REAL, 0.5, 0.7)
            try:
                hp.run_tracked(f'setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {D}/gc; sleep 30',
                               timeout=20, settle=5, state_dir=S)
                report(error='not interrupted')
            except KeyboardInterrupt as e:
                signal.setitimer(signal.ITIMER_REAL, 0)
                gc = wait_file(f'{D}/gc')
                first = e.__context__
                o = {'cleanup': e.cleanup, 'has_receipt': e.receipt is not None,
                     'first': [getattr(first, 'cleanup', None), getattr(first, 'receipt', None) is not None],
                     'before': hp.recheck(e.receipt)}
                kill(gc)
                report(after=settled(e.receipt), leftovers=[gc], **o)
        """)
        self.assertEqual((res['cleanup'], res['has_receipt'], res['first']), ('unknown', True, ['unknown', True]))
        self.assertEqual((res['before'], res['after']), ('pending', 'done'))

    def test_a_killed_supervisor_is_unknown(self):
        res = self.child("""
            r = hp.run_tracked('setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $!', timeout=20, settle=0.3,
                               state_dir=S)
            pid = int(r.stdout)
            before = hp.recheck(r.receipt)
            sup = r.receipt['supervisor']['pid']
            kill(sup)
            os.waitpid(sup, 0)
            after = hp.recheck(r.receipt)
            kill(pid)
            report(cleanup=r.cleanup, before=before, after=after, leftovers=[pid])
        """)
        self.assertEqual((res['cleanup'], res['before'], res['after']), ('pending', 'pending', 'unknown'))

    def test_a_late_report_is_unknown_and_recheck_follows_the_supervisor(self):
        res = self.child("""
            hp.SUPERVISOR_MARGIN = 0.5
            stopped = []

            def stop():
                while not (recs := glob.glob(os.path.join(S, '*.json'))):
                    time.sleep(0.005)
                with open(recs[0]) as f:
                    pid = json.load(f)['pid']
                os.kill(pid, signal.SIGSTOP)
                stopped.append(pid)
            t = threading.Thread(target=stop)
            t.start()
            t0 = time.monotonic()
            try:
                hp.run_tracked('sleep 0.2', timeout=1, settle=0.2, reap_grace=0.5, state_dir=S)
                report(error='no error')
            except RuntimeError as e:
                took = time.monotonic() - t0
                t.join()
                before = hp.recheck(e.receipt)
                os.kill(stopped[0], signal.SIGCONT)
                report(cleanup=e.cleanup, took=took, before=before, after=settled(e.receipt),
                       supervisor=e.receipt['supervisor']['pid'] == stopped[0])
        """)
        self.assertEqual((res['cleanup'], res['before'], res['after'], res['supervisor']),
                         ('unknown', 'pending', 'done', True))
        self.assertLess(res['took'], 10)

    def test_an_untrusted_state_dir_is_refused_before_anything_starts(self):
        res = self.child("""
            out = {}
            open_dir = os.path.join(D, 'open')
            os.mkdir(open_dir)
            os.chmod(open_dir, 0o777)
            loose = os.path.join(D, 'loose')
            os.mkdir(loose)
            os.chmod(loose, 0o770)
            os.mkdir(os.path.join(loose, 'state'), 0o700)
            sticky = os.path.join(D, 'sticky')
            os.mkdir(sticky)
            os.chmod(sticky, 0o1777)
            os.mkdir(os.path.join(sticky, 'state'), 0o700)
            for name, d in (('open', open_dir), ('loose parent', os.path.join(loose, 'state')),
                            ('sticky parent', os.path.join(sticky, 'state'))):
                try:
                    r = hp.run_tracked(f'touch {D}/ran-{name.split()[0]}', timeout=20, settle=1, state_dir=d)
                    out[name] = [r.cleanup, os.listdir(d)]
                except PermissionError as e:
                    out[name] = ['refused', e.cleanup, e.receipt, os.listdir(d)]
            report(out=out, ran=sorted(x for x in os.listdir(D) if x.startswith('ran-')))
        """)
        self.assertEqual(res['out'], {'open': ['refused', 'done', None, []],
                                      'loose parent': ['refused', 'done', None, []],
                                      'sticky parent': ['done', []]})
        self.assertEqual(res['ran'], ['ran-sticky'])

    def test_a_replaced_lock_or_record_is_never_done(self):
        """What a user who could write the state dir would do: an unlocked lock and a matching
        `done` record put in place while the survivor lives."""
        res = self.child("""
            r = hp.run_tracked('sleep 30 </dev/null >/dev/null 2>&1 & echo $!', timeout=20, settle=0.3, state_dir=S)
            pid = int(r.stdout)
            os.unlink(r.receipt['lock'])
            os.close(os.open(r.receipt['lock'], os.O_CREAT | os.O_WRONLY, 0o600))
            st = os.stat(r.receipt['lock'])
            os.unlink(r.receipt['record'])
            with open(os.open(r.receipt['record'], os.O_CREAT | os.O_WRONLY, 0o600), 'w') as f:
                json.dump({'token': r.receipt['token'], 'cleanup': 'done', 'lock': [st.st_dev, st.st_ino]}, f)
            forged = hp.recheck(r.receipt)
            kill(pid)
            time.sleep(0.5)
            report(cleanup=r.cleanup, forged=forged, after=hp.recheck(r.receipt), leftovers=[pid])
        """)
        self.assertEqual((res['cleanup'], res['forged'], res['after']), ('pending', 'unknown', 'unknown'))

    def test_a_timeout_is_reported_by_the_runner_not_inferred_from_the_status(self):
        """`exit 124` with a descendant holding the pipe is killed by the timeout: the banner
        prints although the leader's own status is 124; a plain exit 124 prints none."""
        res = self.child("""
            out = {}
            for name, run in (('cmd', lambda c, t: hp.run_cmd(c, timeout=t, quiet=True)),
                              ('tracked', lambda c, t: hp.run_tracked(c, timeout=t, settle=1, state_dir=S, quiet=True))):
                for cmd, t in (('sleep 30 & exit 124', 0.3), ('exit 124', 20)):
                    import io, contextlib
                    with contextlib.redirect_stdout(io.StringIO()) as cap:
                        r = run(cmd, t)
                    out[f'{name} {cmd}'] = [r.returncode, 'COMMAND TIMEOUT' in cap.getvalue()]
            report(out=out)
        """)
        for name in ('cmd', 'tracked'):
            self.assertEqual(res['out'][f'{name} sleep 30 & exit 124'], [124, True], name)
            self.assertEqual(res['out'][f'{name} exit 124'], [124, False], name)

    def test_an_interrupt_as_the_pipe_closes_stays_an_interrupt_with_the_evidence(self):
        """SIGINT on the return of the pipe's close, after the descriptor is released: the
        handler's close is a no-op, so the interrupt, not EBADF, escapes with the evidence."""
        res = self.child("""
            fired = []

            def local(frame, event, arg):
                if event == 'return' and not fired:
                    fired.append(1)
                    signal.raise_signal(signal.SIGINT)
                return local

            def tracer(frame, event, arg):
                return local if frame.f_code is hp._Reports.close.__code__ else None
            sys.settrace(tracer)
            try:
                hp.run_tracked(f'sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {D}/gc', timeout=20, settle=0.3,
                               state_dir=S)
                sys.settrace(None)
                report(error='not interrupted')
            except BaseException as e:
                sys.settrace(None)
                gc = wait_file(f'{D}/gc')
                o = {'type': type(e).__name__, 'cleanup': getattr(e, 'cleanup', None),
                     'before': hp.recheck(e.receipt)}
                kill(gc)
                report(after=settled(e.receipt), fired=len(fired), leftovers=[gc], **o)
        """)
        self.assertEqual(res['fired'], 1)
        self.assertEqual((res['type'], res['cleanup'], res['before'], res['after']),
                         ('KeyboardInterrupt', 'pending', 'pending', 'done'))

    def _interrupt_after(self, marker: str, nth: int, cmd: str, alarm: float | None) -> dict:
        """SIGINT at the line after the `nth` line of run_tracked equal to `marker`, optionally
        after a first interrupt by SIGALRM at `alarm`."""
        return self.child(f"""
            import inspect
            src, first = inspect.getsourcelines(hp.run_tracked)
            hits = [i for i, x in enumerate(src) if x.strip() == {marker!r}]
            line = first + hits[{nth}] + 1
            fired = []

            def local(frame, event, arg):
                if event == 'line' and frame.f_lineno == line and not fired:
                    fired.append(1)
                    signal.raise_signal(signal.SIGINT)
                return local

            def tracer(frame, event, arg):
                return local if frame.f_code is hp.run_tracked.__code__ else None

            def alarm(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGALRM, alarm)
            if {alarm!r}:
                signal.setitimer(signal.ITIMER_REAL, {alarm!r})
            sys.settrace(tracer)
            try:
                hp.run_tracked({cmd!r}, timeout=20, settle=1, state_dir=S)
                sys.settrace(None)
                report(error='not interrupted')
            except KeyboardInterrupt as e:
                sys.settrace(None)
                report(fired=len(fired), cleanup=e.cleanup, receipt=e.receipt, files=os.listdir(S),
                       recheck=None if e.receipt is None else settled(e.receipt))
        """)

    def test_an_interrupt_right_after_the_files_go_still_says_done(self):
        for name, nth, cmd, alarm in (('normal completion', 0, 'true', None),
                                      ('interrupted completion', 1, 'sleep 30', 0.5)):
            with self.subTest(name):
                res = self._interrupt_after('_tidy(receipt)', nth, cmd, alarm)
                self.assertEqual(res['fired'], 1)
                self.assertEqual((res['cleanup'], res['receipt'], res['files']), ('done', None, []))

    def test_an_interrupt_before_the_verdict_is_known_keeps_the_files(self):
        """Interrupted between the report and the verdict the caller can show: unknown, but
        every file stays, so recheck still reaches done."""
        res = self._interrupt_after("r, killed = _follow(sup, hold, reports, receipt, cmd, timeout, settle, reap_grace)",
                                    0, 'true', None)
        self.assertEqual(res['fired'], 1)
        self.assertEqual((res['cleanup'], res['recheck']), ('unknown', 'done'))
        self.assertEqual(len(res['files']), 3)

    def test_an_interrupt_as_the_report_arrives_is_unknown_with_a_receipt(self):
        res = self.child("""
            real = hp._final

            def interrupted(*a, **k):
                hp._final = real
                real(*a, **k)
                raise KeyboardInterrupt
            hp._final = interrupted
            try:
                hp.run_tracked('true', timeout=20, settle=1, state_dir=S)
                report(error='not interrupted')
            except KeyboardInterrupt as e:
                report(cleanup=e.cleanup, has_receipt=e.receipt is not None, after=settled(e.receipt))
        """)
        self.assertEqual((res['cleanup'], res['has_receipt'], res['after']), ('unknown', True, 'done'))

    def test_a_spawn_error_started_nothing(self):
        res = self.child("""
            try:
                hp.run_tracked(['/nonexistent/hil-run-tracked'], timeout=20, settle=1, state_dir=S)
            except FileNotFoundError as e:
                report(cleanup=e.cleanup, receipt=e.receipt, filename=e.filename)
        """)
        self.assertEqual((res['cleanup'], res['receipt'], res['filename']), ('done', None, '/nonexistent/hil-run-tracked'))

    def test_the_callers_other_children_are_not_the_operations(self):
        res = self.child("""
            pre = subprocess.Popen(['sleep', '30'])
            other = []

            def spawn():
                time.sleep(0.2)
                other.append(subprocess.Popen(['sleep', '30']))
            t = threading.Thread(target=spawn)
            t.start()
            r = hp.run_tracked('sleep 0.6', timeout=20, settle=0.3, state_dir=S)
            t.join()
            kill(pre.pid, other[0].pid)
            pre.wait()
            other[0].wait()
            report(result=[r.cleanup, r.receipt], leftovers=[pre.pid, other[0].pid])
        """)
        self.assertEqual(res['result'], ['done', None])

    def test_bounds_and_state_dir_are_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            for kw in ({'timeout': T, 'settle': 1}, {'timeout': T, 'state_dir': d}, {'settle': 1, 'state_dir': d}):
                with self.subTest(kw=kw), self.assertRaises(TypeError):
                    hp.run_tracked('true', **kw)
            for kw in ({'timeout': None}, {'settle': None}, {'state_dir': None}):
                with self.subTest(kw=kw), self.assertRaises(ValueError):
                    hp.run_tracked('true', **{'timeout': T, 'settle': 1, 'state_dir': d, **kw})

    def test_reports_are_read_bounded_and_malformed_ones_refused(self):
        rfd, wfd = os.pipe()
        reports = hp._Reports(rfd)
        self.addCleanup(reports.close)
        t0 = time.monotonic()
        self.assertIsNone(reports.next(time.monotonic() + 0.2))
        self.assertLess(time.monotonic() - t0, 5)
        os.write(wfd, b'{"hello": {"start": 7}}\nnot json\n[1]\n{"result": {}, "cleanup": "done"}\n')
        os.close(wfd)
        deadline = time.monotonic() + 5
        self.assertEqual(reports.next(deadline), {'hello': {'start': 7}})
        self.assertIsNone(reports.next(deadline))
        self.assertIsNone(reports.next(deadline))
        self.assertEqual(reports.next(deadline), {'result': {}, 'cleanup': 'done'})
        self.assertIsNone(reports.next(deadline), 'EOF')
        reports.close()
        reports.close()     # owned and idempotent: the descriptor is released once

    def test_recheck_without_a_supervisor_is_unknown(self):
        with tempfile.TemporaryDirectory() as top:
            d = os.path.join(top, 'state')
            os.mkdir(d, 0o700)
            receipt = {'token': 't', 'lock': os.path.join(d, 'l'), 'record': os.path.join(d, 'r')}
            self.assertEqual(hp.recheck(receipt), 'unknown')
            os.close(os.open(receipt['lock'], os.O_CREAT | os.O_WRONLY, 0o600))
            st = os.stat(receipt['lock'])
            lock_id = [st.st_dev, st.st_ino]
            self.assertEqual(hp.recheck(receipt), 'unknown')
            record = Path(receipt['record'])
            for text in ('{"token": "other", "cleanup": "done", "lock": %s}', '{"token": "t", "cleanup": "done"}',
                         '{"token": "t", "cleanup": "pending", "lock": %s}', 'not json'):
                record.write_text(text.replace('%s', json.dumps(lock_id)))
                self.assertEqual(hp.recheck(receipt), 'unknown', text)
            record.write_text(json.dumps({'token': 't', 'cleanup': 'done', 'lock': lock_id}))
            record.chmod(0o600)
            self.assertEqual(hp.recheck(receipt), 'done')
            record.chmod(0o620)
            self.assertEqual(hp.recheck(receipt), 'unknown', 'a record another user could write')
            record.chmod(0o600)
            self.assertEqual(hp.recheck({**receipt, 'supervisor': {'lock': [0, 0]}}), 'unknown', 'another lock')
            os.chmod(d, 0o770)
            self.assertEqual(hp.recheck(receipt), 'unknown', 'an untrusted state_dir')

    def test_the_survey_lists_unreadable_entries_apart(self):
        p = subprocess.Popen(['sleep', '30'])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        real_open = open

        def guarded(path, *a, **k):
            if path == f'/proc/{p.pid}/stat':
                raise PermissionError(13, 'Permission denied')
            return real_open(path, *a, **k)
        self.assertIn(p.pid, [x['pid'] for x in hp._survey(os.getpid())['processes']])
        with mock.patch('builtins.open', guarded):
            got = hp._survey(os.getpid())
        self.assertNotIn(p.pid, [x['pid'] for x in got['processes']])
        self.assertIn(p.pid, got['unreadable'])

    def test_the_supervisor_entry_refuses_other_usage(self):
        r = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'hil_process.py')], capture_output=True, text=True,
                           timeout=T)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--supervise', r.stderr)

    def test_run_cmd_reports_no_evidence(self):
        r = hp.run_cmd('true', timeout=T)
        self.assertFalse(hasattr(r, 'cleanup') or hasattr(r, 'receipt'))


if __name__ == '__main__':
    unittest.main()
