"""Tests for the rtt skill's rtt.py: the JlinkRtt and OpenocdRtt console
classes and the CLI, against a fake JLinkExe or openocd on PATH -- real
subprocesses and sockets, no hardware, stdlib only."""
import contextlib
import importlib.util
import io
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress as contextlib_suppress
from pathlib import Path
from unittest import mock

CLI = Path(__file__).resolve().parents[1] / 'skills' / 'rtt' / 'scripts' / 'rtt.py'

# loaded by path and registered under a fixed name, as a harness importing the
# classes would: RttError must be picklable across a fork Pool
_spec = importlib.util.spec_from_file_location('rtt_skill', CLI)
rtt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rtt
_spec.loader.exec_module(rtt)

# Serves -RTTTelnetPort like J-Link Commander: greets, echoes input uppercased, exits on
# stdin 'exit' (JlinkRtt.close()'s contract). FAKE_JLINK_MODE=die_after_greet sends the
# greeting then drops the connection and exits — the probe-unplug/crash case;
# FAKE_JLINK_MODE=tick also streams a line every 50 ms — the continuous-capture case.
FAKE_JLINK = '''#!/usr/bin/env python3
import os, socket, sys, threading, time
port = int(sys.argv[sys.argv.index('-RTTTelnetPort') + 1])
if os.environ.get('FAKE_JLINK_PIDFILE'):
    open(os.environ['FAKE_JLINK_PIDFILE'], 'w').write(str(os.getpid()))
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', port)); srv.listen(1)
mode = os.environ.get('FAKE_JLINK_MODE', '')
def serve():
    conn, _ = srv.accept()
    # the real server sends its banner AT CONNECT, before the control block is
    # found — target data only flows later; the CLI's -i gate must not release
    # on the banner
    conn.sendall(b'SEGGER J-Link fake - Real time terminal output\\r\\n'
                 b'J-Link FakeProbe V1.0, SN=000\\r\\nProcess: JLinkExe\\r\\n')
    if mode == 'banner_only':
        while True:
            if not conn.recv(4096): os._exit(0)
    if mode == 'deaf':
        conn.sendall(b'hello from target\\r\\n')
        time.sleep(600)   # never reads: the client's writes back up
    if mode == 'rst':
        import struct
        conn.recv(4096)   # wait for the client to speak, then reset the connection
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        conn.close(); os._exit(0)
    if mode == 'late_cb':
        # models JLinkExe before it finds the control block: client bytes sent in
        # this window are silently dropped, output starts only after the "attach"
        end = time.time() + 1.0
        conn.setblocking(False)
        while time.time() < end:
            try:
                conn.recv(4096)   # discard early input like the real server
            except OSError:
                pass
            time.sleep(0.05)
        conn.setblocking(True)
    conn.sendall(b'hello from target\\r\\n')
    if mode == 'die_after_greet':
        conn.close(); os._exit(0)
    if mode == 'tick':
        def tick():
            try:
                while True:
                    time.sleep(0.05); conn.sendall(b'tick\\r\\n')
            except OSError:
                pass
        threading.Thread(target=tick, daemon=True).start()
    while True:
        d = conn.recv(4096)
        if not d: return
        conn.sendall(d.upper())
threading.Thread(target=serve, daemon=True).start()
for line in sys.stdin:
    if line.strip() == 'exit': break
'''

LINK = ('--interface', 'swd', '--speed', 'auto')
BOARD = {'flasher': {'uid': '000', 'args': '-device FAKE'}}


@unittest.skipIf(os.name == 'nt', 'POSIX PATH/exec semantics')
class JlinkRttFakeProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        fake = Path(cls._dir.name) / 'JLinkExe'
        fake.write_text(FAKE_JLINK)
        fake.chmod(0o755)
        cls._path = f'{cls._dir.name}{os.pathsep}{os.environ["PATH"]}'

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def _fake_path(self):
        # register the restore BEFORE mutating, then prepend the fake tool dir
        self.addCleanup(os.environ.__setitem__, 'PATH', os.environ['PATH'])
        os.environ['PATH'] = self._path

    def _console(self, mode=''):
        self._fake_path()
        if mode:
            os.environ['FAKE_JLINK_MODE'] = mode
        self.addCleanup(os.environ.pop, 'FAKE_JLINK_MODE', None)
        con = rtt.JlinkRtt(BOARD, timeout=0.1)
        self.addCleanup(con.close)
        return con

    def _read_until(self, con, want, timeout=3):
        out = b''
        end = time.monotonic() + timeout
        while want not in out and time.monotonic() < end:
            out += con.read(con.in_waiting or 1)
        return out

    def test_read_and_echo_write(self):
        con = self._console()
        self.assertIn(b'hello from target', self._read_until(con, b'hello from target'))
        self.assertEqual(con.write(b'ping'), 4)
        self.assertIn(b'PING', self._read_until(con, b'PING'))

    def test_eof_latched_when_server_dies(self):
        con = self._console(mode='die_after_greet')
        self._read_until(con, b'hello from target')
        end = time.monotonic() + 3
        while not con.eof and time.monotonic() < end:
            time.sleep(0.05)
        self.assertTrue(con.eof)               # dead server is detected, not spun on
        t0 = time.monotonic()
        self.assertEqual(con.read(64), b'')    # empty, paced like a serial timeout
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)          # bounded by the 0.1 s timeout, not hung
        self.assertGreater(elapsed, 0.02)      # ...but not a busy-spin fast return
        con.timeout = None                     # pyserial's block-forever mode must
        t0 = time.monotonic()                  # ALSO pace (0.1 s default), not spin
        self.assertEqual(con.read(64), b'')
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        self.assertGreater(elapsed, 0.02)
        con.timeout = 0.1

    def test_reset_input_buffer(self):
        con = self._console()
        self._read_until(con, b'hello from target')
        con.write(b'x')
        time.sleep(0.3)
        con.reset_input_buffer()
        self.assertEqual(con.in_waiting, 0)

    def test_write_after_close_raises_runtimeerror(self):
        con = self._console()
        con.close()
        with self.assertRaises(RuntimeError):
            con.write(b'x')

    def test_write_after_server_death_raises(self):
        # TCP accepts one send after peer death — write() must refuse instead of
        # "succeeding" into the void
        con = self._console(mode='die_after_greet')
        self._read_until(con, b'hello from target')
        end = time.monotonic() + 3
        while not con.eof and time.monotonic() < end:
            time.sleep(0.05)
        with self.assertRaises(RuntimeError):
            con.write(b'ping')

    def test_read_after_close_raises_runtimeerror(self):
        con = self._console()
        self._read_until(con, b'hello from target')
        con.close()
        with self.assertRaises(RuntimeError):
            con.read(1)

    def test_missing_jlinkexe_raises_runtimeerror(self):
        self._fake_path()
        os.environ['PATH'] = self._dir.name  # no python3 either, but JLinkExe fails first
        os.rename(f'{self._dir.name}/JLinkExe', f'{self._dir.name}/JLinkExe.off')
        self.addCleanup(os.rename, f'{self._dir.name}/JLinkExe.off', f'{self._dir.name}/JLinkExe')
        with self.assertRaises(RuntimeError):
            rtt.JlinkRtt(BOARD, timeout=0.1)

    def test_close_escalates_to_kill_on_a_server_ignoring_term(self):
        # the POSIX ladder: gentle stop, group SIGTERM, then SIGKILL — a server that
        # shrugs off TERM must still be gone, and reaped, when close() returns
        con = rtt._SocketRtt()
        con._spawn([sys.executable, '-c',
                    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                    'print("ready", flush=True); time.sleep(60)'])
        proc = con._proc
        end = time.monotonic() + 5
        while 'ready' not in con._server_tail() and time.monotonic() < end:
            time.sleep(0.05)
        self.assertIn('ready', con._server_tail(), 'the server never armed its handler')
        started = time.monotonic()
        con.close()
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 20)

    def test_close_reaps_the_server(self):
        con = self._console()
        proc = con._proc
        con.close()
        self.assertIsNotNone(proc.poll())      # no zombie, no probe held

    def test_cli_exits_when_server_dies(self):
        # --seconds 0 must end on server EOF (rc 1), not hang forever
        env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='die_after_greet')
        r = subprocess.run([sys.executable, str(CLI),
                            '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', '0'],
                           env=env, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b'hello from target', r.stdout)
        self.assertIn(b'server closed', r.stderr)

    def test_peer_reset_latches_eof(self):
        # a killed server closes with RST when bytes are unread; the read side must
        # LATCH eof (so the harness's `assert not ser.eof` triage fires) and never
        # leak ConnectionResetError/ValueError to in_waiting/eof callers
        con = self._console(mode='rst')
        # rst mode sends only the banner (it RSTs on first input) -- wait for the
        # banner tail, not target output that never comes
        self._read_until(con, b'Process: JLinkExe')
        con.write(b'x')                       # fake resets the connection on input
        end = time.monotonic() + 3
        try:
            while not con.eof and time.monotonic() < end:
                con.in_waiting                # must not raise across the RST
                time.sleep(0.05)
        except Exception as e:                # noqa: BLE001 - the regression this guards
            self.fail(f'{type(e).__name__} escaped the latch-only contract: {e}')
        self.assertTrue(con.eof)
        with self.assertRaises(rtt.RttError):
            con.write(b'y')                   # dead server refuses writes

    def test_write_timeout_env_rejects_inf(self):
        # the module's twin rejects inf for the same reason: an unbounded write is what
        # this knob exists to bound
        import importlib.util as ilu
        from pathlib import Path as _P
        spec = ilu.spec_from_file_location('rtt_env_probe', _P(CLI))
        mod = ilu.module_from_spec(spec)
        old = os.environ.get('HIL_SERIAL_WRITE_TIMEOUT')
        os.environ['HIL_SERIAL_WRITE_TIMEOUT'] = 'inf'
        self.addCleanup(lambda: os.environ.__setitem__('HIL_SERIAL_WRITE_TIMEOUT', old)
                        if old is not None else os.environ.pop('HIL_SERIAL_WRITE_TIMEOUT', None))
        spec.loader.exec_module(mod)
        self.assertEqual(mod.RTT_WRITE_TIMEOUT, 10)

    def test_cli_rejects_bad_seconds_and_jlink_channel(self):
        def run(*a):
            return subprocess.run([sys.executable, str(CLI), *a], capture_output=True, timeout=15)
        for bad in ('-5', 'nan'):
            r = run('--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', bad)
            self.assertEqual(r.returncode, 2, f'--seconds {bad} was accepted')
        # the jlink telnet route serves channel 0 only; asking for another is an error,
        # not silence (--dump can read any ring, so it stays allowed there)
        r = run('--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--channel', '1')
        self.assertEqual(r.returncode, 2)
        self.assertIn(b'channel 0 only', r.stderr)
        # a negative index would walk backwards off aUp[] (dump route included)
        r = run('--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--channel', '-1')
        self.assertEqual(r.returncode, 2)
        self.assertIn(b'>= 0', r.stderr)

    def test_pyserial_surface_contracts(self):
        con = self._console()
        self._read_until(con, b'hello from target')
        con.write(b'abcdef')
        self._read_until(con, b'ABC')          # echo queued
        before = con.in_waiting
        self.assertEqual(con.read(0), b'')     # pyserial: consumes nothing
        self.assertEqual(con.read(-1), b'')    # never hand over/destroy bytes
        self.assertEqual(con.in_waiting, before)
        con.timeout = None                     # pyserial: block until satisfied
        con.write(b'xy')                       # fresh echo guarantees the read returns
        self.assertEqual(len(con.read(2)), 2)
        con.timeout = 0.1
        con.close()
        with self.assertRaises(rtt.RttError):
            con.in_waiting                     # closed console reports closed, not healthy
        self.assertTrue(con.eof)

    def test_context_manager_closes(self):
        self._fake_path()
        with rtt.JlinkRtt(BOARD, timeout=0.1) as con:
            proc = con._proc
        self.assertIsNotNone(proc.poll())      # __exit__ released the probe

    def test_banner_filter_drops_all_three_banner_lines(self):
        # the shared RTT banner filter must drop ALL THREE J-Link banner lines,
        #     including the middle one, which is the PROBE MODEL string and in
        #     libjlinkarm carries no 'SEGGER ' prefix (J-Link OH3, J-Trace H9...)
        banner_re = rtt.RTT_BANNER_RE
        for line in ('SEGGER J-Link V9.66 - Real time terminal output',
                     'SEGGER J-Link LPC-Link 2 V1.0, SN=611000000',
                     'J-Link OH3 V1.0, SN=123456789',
                     'J-Trace H9 V2.0, SN=123456789002',
                     'Process: JLinkExe'):
            self.assertTrue(banner_re.match(line), f'banner line not filtered: {line!r}')
        for line in ('Hello from TinyUSB', 'USBD init on controller 0',
                     'ID 1a86:8010 SN 7FD88F0604B5', 'echo:p'):
            self.assertFalse(banner_re.match(line), f'target line wrongly filtered: {line!r}')

    def test_cli_arg_contract(self):
        # --backend is explicit (no default); vid-pid is openocd-only; the openocd
        # backend accepts --addr instead of --elf and --vid-pid instead of --probe
        def run(*a, inp=b''):
            return subprocess.run([sys.executable, str(CLI), *a],
                                  input=inp, capture_output=True, timeout=15)
        r = run('--probe', '000', '--device', 'FAKE', *LINK)          # no --backend
        self.assertEqual(r.returncode, 2)
        self.assertIn(b'--backend', r.stderr)
        r = run('--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--vid-pid', '0x1 0x2')
        self.assertEqual(r.returncode, 2)                       # vid-pid is openocd-only
        r = run('--backend', 'openocd', '--cfg', '-f x.cfg', '--addr', '0x20000000')
        self.assertEqual(r.returncode, 2)                       # needs --probe or --vid-pid
        self.assertIn(b'vid-pid', r.stderr)
        r = run('--backend', 'openocd', '--probe', '000', '--cfg', '-f x.cfg', '--addr', 'nothex')
        self.assertEqual(r.returncode, 2)
        self.assertIn(b'hex', r.stderr)

    def test_cli_interactive_echo(self):
        env = dict(os.environ, PATH=self._path)
        r = subprocess.run([sys.executable, str(CLI),
                            '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', '2', '-i'],
                           env=env, input=b'hi', capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b'HI', r.stdout)         # bytes forwarded without needing a newline
        self.assertNotIn(b'never forwarded', r.stderr)   # forwarding happened: no false alarm

    def test_cli_interactive_input_held_until_output(self):
        # input piped at process start must survive the server's control-block hunt
        # (the real JLinkExe drops client bytes until the block is found — measured
        # on the rig: instant 'ping' lost, delayed 'ping' echoed)
        env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='late_cb')
        r = subprocess.run([sys.executable, str(CLI),
                            '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', '3', '-i'],
                           env=env, input=b'hi', capture_output=True, timeout=25)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b'HI', r.stdout)

    def test_cli_interactive_no_input_diagnostic(self):
        # -i with stdin closed immediately: the diagnostic must say stdin was never
        # forwarded (true), keyed on actual forwarding -- not on the attach gate,
        # which releases after 5 s and forwards anyway on longer runs
        env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='banner_only')
        r = subprocess.run([sys.executable, str(CLI),
                            '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', '1', '-i'],
                           env=env, input=b'', capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b'never forwarded', r.stderr)
        self.assertIn(b'no target output', r.stderr)

    def test_cli_downstream_pipe_close(self):
        # a real `rtt.py | head`-style consumer: close the read end mid-stream
        # and the CLI must exit 0 via its BrokenPipe path, not traceback (this test
        # fails if the handler is removed — subprocess.run capture can't cover it)
        env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='tick')
        p = subprocess.Popen([sys.executable, str(CLI),
                              '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', '8'],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdout.read(10)          # let it stream a little
        p.stdout.close()           # downstream hangs up
        rc = p.wait(timeout=20)
        err = p.stderr.read()
        p.stderr.close()
        self.assertEqual(rc, 0, err)
        self.assertNotIn(b'Traceback', err)

    def test_cli_feeder_races_shutdown(self):
        # a feeder still writing when --seconds expires must not crash the CLI
        # (pump thread vs close() race: historically tracebacks and SIGABRT rc 134)
        env = dict(os.environ, PATH=self._path)
        for _ in range(3):
            p = subprocess.Popen([sys.executable, str(CLI),
                                  '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', '1', '-i'],
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
            try:
                while True:
                    p.stdin.write(b'hi\n')
                    p.stdin.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, OSError):
                pass
            rc = p.wait(timeout=20)
            err = p.stderr.read()
            p.stderr.close()
            with contextlib_suppress(OSError, ValueError):
                p.stdin.close()
            self.assertEqual(rc, 0, err)
            self.assertNotIn(b'Exception in thread', err)


    def test_cli_input_the_target_never_got_fails_the_capture(self):
        env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='deaf', HIL_SERIAL_WRITE_TIMEOUT='0.5')
        with tempfile.TemporaryFile() as flood:
            flood.write(b'x' * (32 << 20))   # more than the socket buffers will hold
            flood.seek(0)
            r = subprocess.run([sys.executable, str(CLI), '--backend', 'jlink', '--probe', '000',
                                '--device', 'FAKE', *LINK, '--seconds', '20', '-i'],
                               stdin=flood, capture_output=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(b'rtt: -i input did not reach the target: RTT console write stalled', r.stderr)
        self.assertIn(b'hello from target', r.stdout)

    def test_cli_a_write_under_way_when_the_capture_ends_still_decides_the_result(self):
        # the capture window closes long before the stalled write gives up
        env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='deaf', HIL_SERIAL_WRITE_TIMEOUT='2')
        with tempfile.TemporaryFile() as flood:
            flood.write(b'x' * (32 << 20))
            flood.seek(0)
            r = subprocess.run([sys.executable, str(CLI), '--backend', 'jlink', '--probe', '000',
                                '--device', 'FAKE', *LINK, '--seconds', '0.3', '-i'],
                               stdin=flood, capture_output=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(b'rtt: -i input did not reach the target: RTT console write stalled', r.stderr)

    def test_cli_a_term_while_waiting_out_a_write_still_closes_the_server(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryFile() as flood:
            pidfile = Path(d) / 'server.pid'
            env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='deaf', HIL_SERIAL_WRITE_TIMEOUT='3',
                       FAKE_JLINK_PIDFILE=str(pidfile))
            flood.write(b'x' * (32 << 20))
            flood.seek(0)
            p = subprocess.Popen([sys.executable, str(CLI), '--backend', 'jlink', '--probe', '000',
                                  '--device', 'FAKE', *LINK, '--seconds', '0.3', '-i'],
                                 stdin=flood, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            time.sleep(1.5)                 # the window is over, the stalled write is not
            p.send_signal(signal.SIGTERM)
            _, err = p.communicate(timeout=60)
            server = int(pidfile.read_text())
        self.assertNotIn(b'Traceback', err)
        self.assertEqual(p.returncode, 1, err)
        self.assertIn(b'RTT console write stalled', err)
        with self.assertRaises(ProcessLookupError):
            os.kill(server, 0)

    def test_cli_stop_file_closes_a_continuous_capture(self):
        env = dict(os.environ, PATH=self._path, FAKE_JLINK_MODE='tick')
        with tempfile.TemporaryDirectory() as temp_dir:
            stop_file = Path(temp_dir) / 'capture.stop'
            proc = subprocess.Popen(
                [sys.executable, str(CLI), '--backend', 'jlink', '--probe', '000',
                 '--device', 'FAKE', *LINK, '--seconds', '0', '--stop-file', str(stop_file)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # the marker goes down only once the capture is demonstrably streaming
            seen = b''
            try:
                end = time.monotonic() + 10
                while b'tick' not in seen:
                    ready, _, _ = select.select([proc.stdout], [], [], max(0, end - time.monotonic()))
                    self.assertTrue(ready, 'no tick within 10 s')
                    chunk = os.read(proc.stdout.fileno(), 4096)   # readline could block on a partial line
                    self.assertTrue(chunk, 'capture ended before the first tick')
                    seen += chunk
                stop_file.touch()
                stdout, stderr = proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertIn(b'hello from target', seen + stdout)

    def test_cli_stop_file_created_during_connect_ends_the_capture(self):
        # a server that never opens its port keeps the CLI in the connect loop, which
        # is where a marker created after spawn must be noticed: deterministic, unlike
        # racing the marker against a missing executable's immediate failure
        never = Path(self._dir.name) / 'never_listens'
        never.write_text('#!/usr/bin/env python3\nimport os, sys, time\n'
                         'open(os.environ["NEVER_STARTED"], "w").close()\ntime.sleep(60)\n')
        never.chmod(0o755)
        with tempfile.TemporaryDirectory() as temp_dir:
            stop_file = Path(temp_dir) / 'capture.stop'
            started = Path(temp_dir) / 'server.started'
            env = dict(os.environ, RTT_JLINK_EXE=str(never), NEVER_STARTED=str(started))
            proc = subprocess.Popen(
                [sys.executable, str(CLI), '--backend', 'jlink', '--probe', '000',
                 '--device', 'FAKE', *LINK, '--stop-file', str(stop_file)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                end = time.monotonic() + 10
                while not started.exists() and time.monotonic() < end:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), 'the server never started')
                self.assertIsNone(proc.poll(), 'the CLI must still be connecting')
                stop_file.touch()
                _, stderr = proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        self.assertEqual(proc.returncode, 0, stderr)


class CliSelection(unittest.TestCase):
    """What the CLI refuses before any server is started."""

    def run_cli(self, *a, env=None):
        return subprocess.run([sys.executable, str(CLI), *a], capture_output=True, text=True,
                              timeout=15, env=dict(os.environ, **(env or {})))

    def fake_sysfs(self, devices):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, attrs in devices.items():
            (Path(d.name) / name).mkdir()
            for k, v in attrs.items():
                (Path(d.name) / name / k).write_text(v + '\n')
        return d.name

    def test_a_blank_probe_is_refused_by_either_backend(self):
        for backend in (('--backend', 'jlink', '--device', 'X', *LINK), ('--backend', 'openocd', '--cfg', 'x.cfg')):
            r = self.run_cli(*backend, '--probe', '   ', '--addr', '20000000')
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn('--probe is empty', r.stderr)

    def test_the_jlink_link_is_never_assumed(self):
        base = ('--backend', 'jlink', '--probe', '000', '--device', 'X')
        for given in ((), ('--interface', 'swd'), ('--speed', '4000')):
            r = self.run_cli(*base, *given)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn('needs --probe, --device, --interface and --speed', r.stderr)
        for bad in ('fast', '0', '4000kHz', '-1'):
            r = self.run_cli(*base, '--interface', 'swd', f'--speed={bad}')
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn('--speed must be kHz', r.stderr)
        r = self.run_cli('--backend', 'openocd', '--probe', '000', '--cfg', 'x.cfg', '--addr', '20000000',
                         '--interface', 'swd')
        self.assertEqual(r.returncode, 2)
        self.assertIn('belong to --cfg', r.stderr)

    @unittest.skipIf(os.name == 'nt', 'POSIX exec semantics')
    def test_the_link_reaches_jlinkexe_after_its_defaults_for_a_stream_and_a_dump(self):
        with tempfile.TemporaryDirectory() as d:
            argv = Path(d) / 'argv'
            exe = Path(d) / 'jlink'
            exe.write_text(f'#!{sys.executable}\nimport sys\n'
                           f'open({str(argv)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")\nsys.exit(1)\n')
            exe.chmod(0o755)
            base = ('--backend', 'jlink', '--probe', '000', '--device', 'X', '--interface', 'jtag', '--speed', '1000')
            self.run_cli(*base, '--seconds', '1', env={'RTT_JLINK_EXE': str(exe)})
            self.run_cli(*base, '--dump', str(Path(d) / 'ring.bin'), '--addr', '20000000',
                         env={'RTT_JLINK_EXE': str(exe)})
            stream, dump = argv.read_text().splitlines()[:2]
        self.assertLess(stream.index('-if swd'), stream.index('-if jtag -speed 1000'))
        self.assertIn('-if jtag -speed 1000', dump)
        self.assertNotIn('swd', dump)

    def test_usb_serials_counts_only_matching_devices(self):
        sysfs = self.fake_sysfs({
            '1-1': {'idVendor': '2e8a', 'idProduct': '000c', 'serial': 'E661AAAA'},
            '1-2': {'idVendor': '2e8a', 'idProduct': '000c', 'serial': 'E661BBBB'},
            '1-3': {'idVendor': '2e8a', 'idProduct': '000c'},               # no serial string
            '1-4': {'idVendor': '0483', 'idProduct': '374b', 'serial': 'STLINK'},
            ('1-0_1.0' if os.name == 'nt' else '1-0:1.0'): {},              # an interface node
        })
        self.assertEqual(rtt.usb_serials('0x2e8a 0x000c', sysfs), ['E661AAAA', 'E661BBBB', ''])
        self.assertEqual(rtt.usb_serials('0x0483 0x374B', sysfs), ['STLINK'])
        self.assertEqual(rtt.usb_serials('0x1366 0x0101', sysfs), [])
        self.assertIsNone(rtt.usb_serials('0x1366 0x0101', sysfs + '/absent'))

    def main_with(self, serials, *argv):
        err = io.StringIO()
        with mock.patch.object(rtt, 'usb_serials', return_value=serials), \
                mock.patch.object(sys, 'argv', ['rtt.py', *argv]), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            rtt.main()
        return cm.exception.code, err.getvalue()

    def test_usb_ids_alone_must_name_exactly_one_probe(self):
        argv = ('--backend', 'openocd', '--vid-pid', '0x2e8a 0x000c', '--cfg', '-f x.cfg',
                '--addr', '0x20000000')
        code, err = self.main_with(['E661AAAA', 'E661BBBB'], *argv)
        self.assertEqual(code, 2)
        self.assertIn('matches 2 attached device(s) (serials: E661AAAA, E661BBBB)', err)
        self.assertIn('--probe', err)
        code, err = self.main_with([], *argv)
        self.assertIn('matches 0 attached device(s)', err)
        code, err = self.main_with(None, *argv)
        self.assertIn('cannot count the attached probes', err)

    def test_a_malformed_vid_pid_is_refused_before_counting(self):
        r = self.run_cli('--backend', 'openocd', '--vid-pid', '2e8a:000c', '--cfg', '-f x.cfg',
                         '--addr', '0x20000000')
        self.assertEqual(r.returncode, 2)
        self.assertIn('0xVVVV 0xPPPP', r.stderr)

    def test_one_source_for_the_control_block_address(self):
        r = self.run_cli('--backend', 'openocd', '--probe', '000', '--cfg', '-f x.cfg',
                         '--addr', '0x20000000', '--elf', 'fw.elf')
        self.assertEqual(r.returncode, 2)
        self.assertIn('pass one', r.stderr)

    def test_seconds_must_be_finite(self):
        r = self.run_cli('--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', 'inf')
        self.assertEqual(r.returncode, 2)
        self.assertIn('finite', r.stderr)

    def test_the_cli_refuses_a_write_timeout_it_cannot_honour(self):
        for bad in ('soon', '0', 'inf'):
            r = self.run_cli('--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK,
                             env={'HIL_SERIAL_WRITE_TIMEOUT': bad})
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn('HIL_SERIAL_WRITE_TIMEOUT', r.stderr)


# JLinkExe for the dump route: answers mem32/savebin from a synthetic target whose
# control block sits at 0x20000000 with aUp[0] = 0x400 B at 0x20001000. FAKE_DUMP picks
# what is wrong with it.
FAKE_DUMP_JLINK = r"""
import os, re, struct, sys
mode = os.environ.get('FAKE_DUMP', '')
CB, RING, SIZE = 0x20000000, 0x20001000, 0x400
ident = b'SEGGER XXX' if mode == 'no_signature' else b'SEGGER RTT'
wroff = SIZE if mode == 'wroff_out_of_range' else 0x10
mem = ident.ljust(16, b'\0') + struct.pack('<2I', 2, 2)
mem += struct.pack('<6I', 0x10001234, RING, SIZE, wroff, 0, 0)       # aUp[0]
mem += struct.pack('<6I', 0, 0, 0, 0, 0, 0)                           # aUp[1]: never set up
for line in sys.stdin:
    m = re.match(r'mem32 (0x[0-9a-f]+), (\d+)', line)
    if m:
        a, n = int(m.group(1), 16), int(m.group(2))
        if mode == 'read_fails' and a != CB:
            print('Could not read memory.')
            continue
        shown = a + 4 if mode == 'shifted' and a != CB else a
        if a - CB + 4 * n > len(mem):
            print('Could not read memory.')
            continue
        w = struct.unpack_from(f'<{n}I', mem, a - CB)
        for i in range(0, n, 4):
            print(f'J-Link>{shown + 4 * i:08X} = ' + ' '.join(f'{x:08X}' for x in w[i:i + 4]))
    m = re.match(r'savebin (\S+), (0x[0-9a-f]+), (0x[0-9a-f]+)', line)
    if m and mode != 'no_file':
        n = int(m.group(3), 16) + {'short': -1, 'long': 1}.get(mode, 0)
        open(m.group(1), 'wb').write(b'R' * n)
if mode in ('transport_dies', 'transport_dies_late'):
    if mode == 'transport_dies' or os.path.exists(os.environ['FAKE_DUMP_OUT']):
        print('USB communication error: probe disconnected')
        sys.exit(1)
"""


@unittest.skipIf(os.name == 'nt', 'POSIX exec semantics')
class DumpRing(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        self.exe = self.tmp / 'jlink'
        self.exe.write_text(f'#!{sys.executable}\n{FAKE_DUMP_JLINK}')
        self.exe.chmod(0o755)
        self.out = self.tmp / 'ring.bin'

    def dump(self, mode='', out=None, channel='0'):
        return subprocess.run(
            [sys.executable, str(CLI), '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK,
             '--addr', '20000000', '--channel', channel, '--dump', str(out or self.out)],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, RTT_JLINK_EXE=str(self.exe), FAKE_DUMP=mode,
                     FAKE_DUMP_OUT=str(out or self.out)))

    def test_a_whole_ring_is_dumped(self):
        r = self.dump()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.out.read_bytes(), b'R' * 0x400)
        self.assertIn('ring: 1024 B at 0x20001000, WrOff=0x10 RdOff=0x0', r.stderr)

    def test_an_existing_file_is_never_overwritten(self):
        self.out.write_text('an earlier dump')
        r = self.dump()
        self.assertEqual(r.returncode, 1)
        self.assertIn('already exists', r.stderr)
        self.assertEqual(self.out.read_text(), 'an earlier dump')
        r = self.dump(out=self.tmp / 'no-such-dir' / 'ring.bin')
        self.assertIn('no such directory', r.stderr)

    def test_a_descriptor_that_cannot_be_trusted_is_refused_before_the_ring_is_read(self):
        for mode, says in (('no_signature', 'no RTT control block at 0x20000000'),
                           ('wroff_out_of_range', 'WrOff=0x400 RdOff=0x0 must be below its size 0x400'),
                           ('read_fails', 'could not read aUp[0] at 0x20000018'),
                           ('shifted', 'could not read aUp[0] at 0x20000018')):
            r = self.dump(mode)
            self.assertEqual(r.returncode, 1, mode)
            self.assertIn(says, r.stderr, mode)
            self.assertFalse(self.out.exists(), mode)
        r = self.dump(channel='1')
        self.assertIn('up-buffer 1 is not initialized', r.stderr)
        r = self.dump(channel='2')
        self.assertIn('this firmware has 2 up-buffer(s)', r.stderr)

    def test_a_commander_that_fails_after_answering_fails_the_dump(self):
        for mode in ('transport_dies', 'transport_dies_late'):   # during the descriptor read; during savebin
            r = self.dump(mode)
            self.assertEqual(r.returncode, 1, mode)
            self.assertIn('jlink exited 1:', r.stderr, mode)
            self.assertIn('USB communication error: probe disconnected', r.stderr, mode)
            self.assertFalse(self.out.exists(), mode)
            self.assertNotIn('ring: 1024 B', r.stderr, mode)

    def test_only_a_dump_of_exactly_the_ring_is_kept(self):
        for mode, says in (('short', 'savebin wrote 1023 B for a 1024 B ring'),
                           ('long', 'savebin wrote 1025 B for a 1024 B ring'),
                           ('no_file', 'savebin produced no data')):
            r = self.dump(mode)
            self.assertEqual(r.returncode, 1, mode)
            self.assertIn(says, r.stderr, mode)
            self.assertFalse(self.out.exists(), mode)


class StripBanner(unittest.TestCase):
    # both harness consumers (device_info verdict, pool_check aliveness) judge
    # target-aliveness through this ONE filter -- pin its shape here
    def test_drops_banner_keeps_target(self):
        raw = (b'SEGGER J-Link V9.66 - Real time terminal output\r\n'
               b'J-Link OH3 V1.0, SN=123456789\r\nProcess: JLinkExe\r\n'
               b'Hello from TinyUSB\r\n')
        self.assertEqual(rtt.strip_banner(raw), b'Hello from TinyUSB')

    def test_complete_only_drops_split_banner_fragment(self):
        # a poll loop can catch the banner mid-line at a read boundary; the
        # fragment must not defeat the prefix regex and score as target output
        frag = b'SEGGER J-Link V9.66 - Real time terminal output\r\nProce'
        self.assertEqual(rtt.strip_banner(frag, complete_only=True), b'')
        # the final verdict keeps a genuine unterminated target tail
        self.assertEqual(rtt.strip_banner(b'tud_task\r\nrunn'), b'tud_task\nrunn')
        self.assertEqual(rtt.strip_banner(b'', complete_only=True), b'')


# Serves like `openocd ... -c "rtt server start PORT CH"`: parses the port from its
# single shell-quoted command line, greets, echoes uppercased. No banner (matches the
# real openocd rtt server, which sends target data only).
FAKE_OPENOCD = '''#!/usr/bin/env python3
import os, re, socket, sys, threading, time
if os.environ.get('FAKE_OPENOCD_ARGV'):
    open(os.environ['FAKE_OPENOCD_ARGV'], 'w').write(' '.join(sys.argv))
port = int(re.search(r'rtt server start (\\d+)', ' '.join(sys.argv)).group(1))
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', port)); srv.listen(1)
conn, _ = srv.accept()
conn.sendall(b'hello from target\\r\\n')
while True:
    d = conn.recv(4096)
    if not d: break
    conn.sendall(d.upper())
'''


@unittest.skipIf(os.name == 'nt', 'POSIX PATH/exec semantics')
class OpenocdRttFakeProbe(unittest.TestCase):
    """The openocd-backend class shares its whole read/write/eof contract with
    JlinkRtt via the base class (covered above); this exercises the parts it owns:
    spawn/connect, echo round-trip, teardown."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        fake = Path(cls._dir.name) / 'openocd'
        fake.write_text(FAKE_OPENOCD)
        fake.chmod(0o755)
        cls._path = f'{cls._dir.name}{os.pathsep}{os.environ["PATH"]}'

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def _fake_path(self):
        self.addCleanup(os.environ.__setitem__, 'PATH', os.environ['PATH'])
        os.environ['PATH'] = self._path

    def test_reset_before_attach_shapes_the_command(self):
        # SystemView-style consumers need the server draining WHEN the target boots
        # (its Init record is emitted once); the opt-in flag must put `reset run`
        # between init and rtt setup, and must not appear otherwise
        self._fake_path()
        argv_file = os.path.join(self._dir.name, 'argv.txt')
        os.environ['FAKE_OPENOCD_ARGV'] = argv_file
        self.addCleanup(os.environ.pop, 'FAKE_OPENOCD_ARGV', None)
        for flag, want in ((True, True), (False, False)):
            con = rtt.OpenocdRtt('-f fake.cfg', 0x20000000, 1, serial_no='000',
                                      reset_before_attach=flag)
            try:
                argv = Path(argv_file).read_text()
            finally:
                con.close()
            self.assertEqual('reset run' in argv, want, argv)
            if want:   # ordering is the whole point: reset, settle, THEN attach
                self.assertLess(argv.index('reset run'), argv.index('rtt setup'), argv)
                self.assertIn('sleep 2000', argv)
            self.assertIn('rtt server start', argv)
            self.assertTrue(argv.rstrip().endswith('1'), argv)   # channel threaded through

    def test_openocd_route_echo_and_teardown(self):
        self._fake_path()
        con = rtt.OpenocdRtt('-f fake.cfg', 0x20000000, 0,
                                  serial_no='000', vid_pid='0x1234 0x5678')
        self.addCleanup(con.close)
        out = b''
        end = time.monotonic() + 3
        while b'hello from target' not in out and time.monotonic() < end:
            out += con.read(con.in_waiting or 1)
        self.assertIn(b'hello from target', out)
        con.write(b'ping')
        end = time.monotonic() + 3
        while b'PING' not in out and time.monotonic() < end:
            out += con.read(con.in_waiting or 1)
        self.assertIn(b'PING', out)
        proc = con._proc
        con.close()
        self.assertIsNotNone(proc.poll())      # no zombie, no probe held
        with self.assertRaises(RuntimeError):
            con.write(b'x')                    # same post-close contract as JlinkRtt


class RttPlatformLifecycle(unittest.TestCase):
    def test_cli_missing_jlink_is_a_clean_error_on_this_platform(self):
        env = dict(os.environ, RTT_JLINK_EXE='definitely-not-a-jlink-tool')
        r = subprocess.run([sys.executable, str(CLI), '--backend', 'jlink',
                            '--probe', '000', '--device', 'FAKE', *LINK, '--seconds', '0.1'],
                           env=env, capture_output=True, timeout=15)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(b'not on PATH', r.stderr)
        self.assertNotIn(b'Traceback', r.stderr)

    def test_process_group_creation_matches_platform(self):
        options = rtt._popen_group_options()
        if os.name == 'nt':
            self.assertEqual(options, {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP})
        else:
            self.assertEqual(options, {'start_new_session': True})

    def test_windows_termination_uses_process_api_and_taskkill(self):
        class FakeProc:
            pid = 123

            def __init__(self):
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        proc = FakeProc()
        with mock.patch.object(rtt, 'IS_WINDOWS', True), \
             mock.patch.object(rtt.subprocess, 'run') as taskkill:
            rtt._terminate_process_group(proc, force=False)
        taskkill.assert_called_once()
        self.assertEqual(taskkill.call_args.args[0][:4], ['taskkill', '/PID', '123', '/T'])
        self.assertIn('/F', taskkill.call_args.args[0])
        self.assertTrue(proc.terminated)

        proc = FakeProc()
        with mock.patch.object(rtt, 'IS_WINDOWS', True), \
             mock.patch.object(rtt.subprocess, 'run') as taskkill:
            rtt._terminate_process_group(proc, force=True)
        self.assertIn('/F', taskkill.call_args.args[0])
        self.assertTrue(proc.killed)

    def test_windows_close_stops_the_tree_before_the_parent_can_exit(self):
        class FakeProc:
            pid = 123
            stdin = None
            stdout = None

            def __init__(self):
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def wait(self, timeout):
                if self.alive:
                    raise subprocess.TimeoutExpired('fake', timeout)
                return 0

        class FakeConsole(rtt._SocketRtt):
            def __init__(self, proc):
                super().__init__()
                self._proc = proc
                self.gentle_called = False

            def _gentle_stop(self, proc):
                self.gentle_called = True
                proc.alive = False

        proc = FakeProc()
        con = FakeConsole(proc)

        def stop_tree(stopped_proc, force):
            self.assertIs(stopped_proc, proc)
            self.assertFalse(force)
            stopped_proc.alive = False

        with mock.patch.object(rtt, 'IS_WINDOWS', True), \
             mock.patch.object(rtt, '_terminate_process_group', side_effect=stop_tree) as stop:
            con.close()
        stop.assert_called_once_with(proc, force=False)
        self.assertFalse(con.gentle_called)

    @unittest.skipUnless(os.name == 'nt', 'Windows process-tree semantics')
    def test_windows_close_reaps_a_real_child_process(self):
        import ctypes

        def pid_exists(pid):
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

        child_code = 'import time; time.sleep(60)'
        parent_code = ('import subprocess, sys, time; '
                       f'p = subprocess.Popen([sys.executable, "-c", {child_code!r}]); '
                       'print(p.pid, flush=True); time.sleep(60)')
        con = rtt._SocketRtt()
        con._spawn([sys.executable, '-c', parent_code])
        proc = con._proc
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while child_pid is None and time.monotonic() < deadline:
                match = re.search(r'\d+', con._server_tail())
                if match:
                    child_pid = int(match.group())
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child_pid, 'child PID was not written to the server log')
            taskkill_results = []
            real_run = subprocess.run

            def run_taskkill(cmd, **kwargs):
                kwargs['stdout'] = subprocess.PIPE
                kwargs['stderr'] = subprocess.PIPE
                result = real_run(cmd, **kwargs)
                taskkill_results.append(result)
                return result

            with mock.patch.object(rtt.subprocess, 'run', side_effect=run_taskkill):
                con.close()
            self.assertEqual(taskkill_results[0].returncode, 0, taskkill_results[0].stderr)
            self.assertIsNotNone(proc.poll())
            deadline = time.monotonic() + 2
            while pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(pid_exists(child_pid))
        finally:
            con.close()
            for pid in (proc.pid, child_pid):
                if pid and pid_exists(pid):
                    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                   capture_output=True, check=False)

    @unittest.skipUnless(os.name == 'nt', 'Windows temporary-file sharing semantics')
    def test_server_log_can_be_reopened_and_is_removed(self):
        class DoneProc:
            stdin = None
            stdout = None

            def poll(self):
                return 0

        con = rtt._SocketRtt()
        with mock.patch.object(rtt.subprocess, 'Popen', return_value=DoneProc()):
            con._spawn(['fake-server'])
        log_name = con._log.name
        con._log.write(b'useful server failure')
        con._log.flush()
        self.assertIn('useful server failure', con._server_tail())
        con.close()
        self.assertFalse(os.path.exists(log_name))

    def test_posix_server_log_keeps_automatic_deletion(self):
        class DoneProc:
            stdin = None
            stdout = None

            def poll(self):
                return 0

        con = rtt._SocketRtt()
        named_temporary_file = tempfile.NamedTemporaryFile
        with mock.patch.object(rtt, 'IS_WINDOWS', False), \
             mock.patch.object(rtt.tempfile, 'NamedTemporaryFile',
                               wraps=named_temporary_file) as named_log, \
             mock.patch.object(rtt.subprocess, 'Popen', return_value=DoneProc()):
            con._spawn(['fake-server'])
        self.assertTrue(named_log.call_args.kwargs['delete'])
        con.close()

    def test_connect_honors_a_stop_request_during_setup(self):
        class FakeProc:
            def poll(self):
                return None

        con = rtt._SocketRtt()
        con._proc = FakeProc()
        con.close = mock.Mock()
        stop = mock.Mock(side_effect=(False, True))
        with mock.patch.object(rtt.socket, 'create_connection', side_effect=OSError), \
             mock.patch.object(rtt.time, 'sleep'), \
             self.assertRaises(rtt._StopCapture):
            con._connect(1234, stop=stop)
        con.close.assert_called_once()

    def test_symbol_lookup_honors_a_stop_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_nm = Path(temp_dir) / 'slow_nm.py'
            fake_nm.write_text('import time\ntime.sleep(30)\n')
            stop = mock.Mock(side_effect=(False, False, True))
            started = time.monotonic()
            with self.assertRaises(rtt._StopCapture):
                rtt.nm_rtt_addr(str(fake_nm), nm=sys.executable, stop=stop)
        self.assertLess(time.monotonic() - started, 2)

    def test_symbol_lookup_reaps_nm_when_interrupted(self):
        class InterruptedNm:
            returncode = None

            def __init__(self):
                self.terminated = False
                self.reaped = False

            def communicate(self, timeout=None):
                if not self.terminated:
                    raise KeyboardInterrupt
                self.reaped = True
                self.returncode = -1
                return '', ''

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.terminated = True

        proc = InterruptedNm()
        with mock.patch.object(rtt.subprocess, 'Popen', return_value=proc), \
             self.assertRaises(KeyboardInterrupt):
            rtt.nm_rtt_addr('fake.elf', nm='fake-nm', stop=lambda: False)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)

    def test_windows_defaults_to_jlink_exe(self):
        with mock.patch.object(rtt, 'IS_WINDOWS', True), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rtt._tool_exe('RTT_JLINK_EXE', 'JLinkExe', 'JLink.exe'),
                             'JLink.exe')

    def test_cli_accepts_an_existing_stop_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stop_file = Path(temp_dir) / 'capture.stop'
            stop_file.touch()
            r = subprocess.run(
                [sys.executable, str(CLI), '--backend', 'jlink', '--probe', '000',
                 '--device', 'FAKE', *LINK, '--stop-file', str(stop_file)],
                capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_validates_arguments_before_honoring_an_existing_stop_file(self):
        # a stale marker must not turn a bad invocation into a silent success
        with tempfile.TemporaryDirectory() as temp_dir:
            stop_file = Path(temp_dir) / 'capture.stop'
            stop_file.touch()
            for extra in (['--backend', 'jlink', '--probe', '000'],                       # no --device
                          ['--backend', 'openocd', '--probe', '000', '--elf', 'x.elf'],   # no --cfg
                          ['--backend', 'openocd', '--probe', '000', '--cfg', '-f x.cfg'],  # no --elf/--addr
                          ['--backend', 'openocd', '--probe', '000', '--cfg', '-f x.cfg', '--addr', 'zz'],
                          ['--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK,
                           '--dump', str(Path(temp_dir) / 'ring.bin'), '--addr', '0x20000000']):
                r = subprocess.run([sys.executable, str(CLI), *extra, '--stop-file', str(stop_file)],
                                   capture_output=True, timeout=20)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertNotIn(b'Traceback', r.stderr)
            self.assertFalse((Path(temp_dir) / 'ring.bin').exists())

    def test_symbol_lookup_with_a_stop_hook_reads_the_symbol_and_reports_failures(self):
        # the CLI always takes the cancellable branch; it must match the plain one
        with tempfile.TemporaryDirectory() as temp_dir:
            good = Path(temp_dir) / 'nm_ok.py'
            good.write_text('print("20000400 D _SEGGER_RTT")\n')
            self.assertEqual(rtt.nm_rtt_addr(str(good), nm=sys.executable, stop=lambda: False), 0x20000400)
            bad = Path(temp_dir) / 'nm_bad.py'
            bad.write_text('import sys; sys.stderr.write("file format not recognized"); sys.exit(1)\n')
            with self.assertRaises(SystemExit) as cm:
                rtt.nm_rtt_addr(str(bad), nm=sys.executable, stop=lambda: False)
            self.assertIn('could not read', str(cm.exception))
            with self.assertRaises(SystemExit) as cm:
                rtt.nm_rtt_addr('x.elf', nm='definitely-not-an-nm', stop=lambda: False)
            self.assertIn('not on PATH', str(cm.exception))

    def test_cli_stop_file_cancels_connection_setup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stop_file = Path(temp_dir) / 'capture.stop'

            def start_console(*_args, **kwargs):
                stop_file.touch()
                self.assertTrue(kwargs['stop']())
                raise rtt._StopCapture

            argv = [str(CLI), '--backend', 'jlink', '--probe', '000', '--device', 'FAKE', *LINK,
                    '--stop-file', str(stop_file)]
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(rtt, 'JlinkRtt', side_effect=start_console):
                self.assertEqual(rtt.main(), 0)

    def test_cli_stop_file_cancels_symbol_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stop_file = Path(temp_dir) / 'capture.stop'

            def find_symbol(_elf, nm=None, stop=None):
                self.assertIsNone(nm)
                stop_file.touch()
                self.assertTrue(stop())
                raise rtt._StopCapture

            argv = [str(CLI), '--backend', 'openocd', '--probe', '000', '--cfg', '-f fake.cfg',
                    '--elf', 'fake.elf', '--stop-file', str(stop_file)]
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(rtt, 'nm_rtt_addr', side_effect=find_symbol):
                self.assertEqual(rtt.main(), 0)


if __name__ == '__main__':
    unittest.main()
