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
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, redirect_stdout
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
                           binary=True, split_stderr=True, timeout=1)
        self.assertEqual(r.returncode, 124)
        # killpg + bounded communicate: well under sleep 30
        self.assertLess(time.monotonic() - t0, 15)
        self.assertIn(b'p\xffre', r.stdout or b'')
        # stderr collected before the timeout must survive the kill
        self.assertIn(b'warn', r.stderr or b'')

    def test_text_mode_timeout_stdout_stays_str(self):
        with redirect_stdout(io.StringIO()):
            r = hp.run_cmd('sleep 30', timeout=1)
        self.assertEqual(r.returncode, 124)
        # a text-mode caller must never get bytes back, even empty
        self.assertIsInstance(r.stdout, str)
        self.assertIsNone(r.stderr)

    def test_split_stderr_timeout_stderr_is_never_none(self):
        with redirect_stdout(io.StringIO()) as cap:
            r = hp.run_cmd('sleep 30', split_stderr=True, timeout=1)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (124, '', ''))
        self.assertIn('COMMAND TIMEOUT (1s): sleep 30', cap.getvalue())

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

        class Stuck:
            pid = 999999999
            stdout = stderr = stdin = None

            def __init__(self, *a, **k):
                pass

            def communicate(self, timeout):
                waits.append(timeout)
                raise subprocess.TimeoutExpired('x', timeout, output=b'partial')

        for fn in (lambda: hp.run_cmd('x', timeout=3, reap_grace=0.5),
                   lambda: hp.run_alongside(['x'], lambda: None, 3, reap_grace=0.5)):
            waits.clear()
            with mock.patch.object(hp.subprocess, 'Popen', Stuck), redirect_stdout(io.StringIO()), \
                    mock.patch.object(hp.os, 'killpg'):
                r = fn()
            self.assertEqual((r.returncode, waits), (124, [3, 0.5]))

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
        r = hp.run_alongside(['sleep', '30'], lambda: None, 1)
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
            hp.run_alongside(['sleep', '30'], boom, 1)
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
    """run_cmd's two cleanup paths, asserted structurally.

    Both must kill the process GROUP: start_new_session puts the child in its own group, so
    a flasher run through a shell keeps children a p.kill() cannot reach, and on the
    BaseException path the child never receives the terminal's SIGINT either. Both times
    this block was mis-edited, an `else:` ended up attached to the `try` instead of the `if`
    it belonged to, so `p.kill()` ran when killpg had SUCCEEDED and its ProcessLookupError
    masked the caller's exception: a shape an AST can pin.
    """

    def _run_cmd_ast(self):
        src = Path(hp.__file__).read_text()
        return next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.FunctionDef) and n.name == 'run_cmd')

    def test_no_cleanup_try_has_an_else(self):
        for n in ast.walk(self._run_cmd_ast()):
            if isinstance(n, ast.Try) and n.orelse:
                self.fail(f'try/else at line {n.lineno}: an else here runs when the kill SUCCEEDED')

    def test_both_cleanup_paths_kill_the_group(self):
        killers = [getattr(c.func, 'attr', '') for c in ast.walk(self._run_cmd_ast())
                   if isinstance(c, ast.Call) and getattr(c.func, 'attr', '') in ('killpg', 'kill')]
        self.assertEqual(killers.count('killpg'), 2,
                         'both the timeout and the BaseException path must killpg')
        self.assertEqual(killers.count('kill'), 0,
                         'p.kill() reaches only the direct child')

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
        with self._signal_after_popen_returns(hp.run_cmd, signal.SIGINT):
            with self.assertRaises(KeyboardInterrupt):
                hp.run_cmd(self.shell_cmd, timeout=T)
        self._assert_group_gone(self.procs[0].pid)

    def test_run_cmd_sigterm_right_after_popen_returns(self):
        self._sigterm_raises()
        with self._signal_after_popen_returns(hp.run_cmd, signal.SIGTERM):
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
                r = hp.run_cmd(self.shell_cmd, timeout=1)
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


if __name__ == '__main__':
    unittest.main()
