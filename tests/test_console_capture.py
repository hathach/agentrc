import contextlib
import ctypes
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    import termios
except ImportError:  # Windows: only the Win32 backend cases run
    termios = None

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts' / 'console_capture.py'


@unittest.skipIf(os.name == 'nt', 'ptys are POSIX')
class ConsoleCaptureTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        self.master, self.slave = os.openpty()
        self.addCleanup(self.close)
        self.port = os.ttyname(self.slave)
        self.out = self.d / 'cap.bin'

    def close(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def start(self, *args, port=None):
        return subprocess.Popen([sys.executable, str(SCRIPT), '--port', port or self.port, '--baud', '115200',
                                 '--out', str(self.out), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def run_cli(self, *args, port=None):
        proc = self.start(*args, port=port)
        out, err = proc.communicate(timeout=30)
        return proc.returncode, json.loads(out) if out.strip() else None, err

    def feed(self, *parts, delay=0.3):
        def go():
            time.sleep(delay)
            for part in parts:
                os.write(self.master, part)
                time.sleep(0.1)
        t = threading.Thread(target=go)
        t.start()
        self.addCleanup(t.join)

    def test_bytes_times_marks_and_restored_settings(self):
        before = termios.tcgetattr(self.slave)
        self.feed(b'ESP-ROM:esp32p4\r\nrst:0x17 (CHIP_USB_UART_RESET)\r\n', b'\x00\xff binary\n')
        rc, out, err = self.run_cli('--seconds', '1.5', '--lines', 'leave', '--mark', r'rst:0x[0-9a-f]+')
        self.assertEqual(rc, 0, err)
        data = self.out.read_bytes()
        self.assertEqual(data, b'ESP-ROM:esp32p4\r\nrst:0x17 (CHIP_USB_UART_RESET)\r\n\x00\xff binary\n')
        self.assertEqual(out['bytes'], len(data))
        self.assertEqual(out['ended'], 'time')
        self.assertEqual(out['baseline'], 'the open')
        self.assertGreater(out['firstByteAt'], 0.1)
        self.assertIsNone(out['lines']['afterSetup'])    # a pty has no modem lines
        self.assertTrue(out['terminalRestored'])
        times = [json.loads(line) for line in Path(out['times']).read_text().splitlines()]
        self.assertEqual(sum(t['len'] for t in times), len(data))
        self.assertEqual(out['marks'], [{'pattern': r'rst:0x[0-9a-f]+', 'offset': 17, 't': times[0]['t'],
                                         'text': 'rst:0x17'}])
        self.assertEqual(termios.tcgetattr(self.slave), before)

    def test_an_empty_capture_is_no_baseline(self):
        rc, out, err = self.run_cli('--seconds', '0.5', '--lines', 'leave')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['bytes'], 0)
        self.assertIsNone(out['firstByteAt'])
        self.assertTrue(out['baseline'].startswith('none'))
        self.assertEqual(self.out.read_bytes(), b'')

    def test_an_empty_capture_with_a_zero_width_mark(self):
        rc, out, err = self.run_cli('--seconds', '0.3', '--lines', 'leave', '--mark', '^')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['marks'], [])

    def test_a_capture_that_cannot_be_read_back_keeps_its_summary(self):
        self.feed(b'rst:0x1\n', delay=0.2)
        proc = self.start('--seconds', '1', '--lines', 'leave', '--mark', 'rst')
        deadline = time.monotonic() + 10
        while not self.out.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.out.rename(self.d / 'moved.bin')
        out, err = proc.communicate(timeout=30)
        out = json.loads(out)
        self.assertEqual(proc.returncode, 3, err)
        self.assertIn('reading', out['error'])
        self.assertEqual(out['bytes'], 8)

    def test_each_chunk_time_is_written_as_it_arrives(self):
        self.feed(b'boot\n', delay=0.2)
        proc = self.start('--seconds', '10', '--lines', 'leave')
        self.addCleanup(proc.communicate, timeout=30)
        self.addCleanup(proc.send_signal, signal.SIGTERM)
        times = Path(str(self.out) + '.times')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (times.exists() and times.read_bytes()):
            time.sleep(0.02)
        self.assertIsNone(proc.poll())
        self.assertEqual([json.loads(line)['len'] for line in times.read_text().splitlines()], [5])

    def test_a_failed_close_keeps_the_summary(self):
        # the .times file stands in for a full disk: its close fails
        run = f'''
import errno, importlib.util, sys
spec = importlib.util.spec_from_file_location('console_capture', {str(SCRIPT)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
class Full:
    def __init__(self, f):
        self.f, self.name = f, f.name
    def write(self, b):
        return self.f.write(b)
    def flush(self):
        self.f.flush()
    def close(self):
        self.f.close()
        raise OSError(errno.ENOSPC, 'No space left on device')
def fake_open(path, mode='r', *args, **kw):
    f = open(path, mode, *args, **kw)
    return Full(f) if mode == 'xb' and path.endswith('.times') else f
m.open = fake_open
sys.exit(m.main(sys.argv[1:]))
'''
        r = subprocess.run([sys.executable, '-c', run, '--port', self.port, '--baud', '115200', '--out', str(self.out),
                            '--seconds', '0.3', '--lines', 'leave'], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 3, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn('closing', out['error'])
        self.assertIn('No space left', out['error'])
        self.assertTrue(out['terminalRestored'])

    def test_a_signal_before_the_capture_leaves_no_files(self):
        # the capture never starts its own handlers here: the signal lands in the window after the files exist
        run = f'''
import importlib.util, os, signal, sys
spec = importlib.util.spec_from_file_location('console_capture', {str(SCRIPT)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.capture_posix = lambda *args: os.kill(os.getpid(), signal.SIGTERM)
if sys.argv[1] == 'create':    # the signal lands as the first file is made, before main lists it
    real = open
    def opening(path, mode='r', *args, **kw):
        f = real(path, mode, *args, **kw)
        if mode == 'xb':
            os.kill(os.getpid(), signal.SIGTERM)
        return f
    m.open = opening
sys.exit(m.main(sys.argv[2:]))
'''
        for when in ('create', 'capture'):
            with self.subTest(when=when):
                r = subprocess.run([sys.executable, '-c', run, when, '--port', self.port, '--baud', '115200',
                                    '--out', str(self.out), '--seconds', '0.3', '--lines', 'leave'],
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn('before the capture began', r.stderr)
                self.assertEqual(list(self.d.iterdir()), [])

    def test_a_signal_as_the_capture_takes_over_ends_it_with_a_summary(self):
        run = f'''
import importlib.util, os, signal, sys
spec = importlib.util.spec_from_file_location('console_capture', {str(SCRIPT)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
real = signal.signal
def install(sig, handler):
    old = real(sig, handler)
    if getattr(handler, '__name__', '') == 'on_signal' and sig == signal.SIGTERM:
        os.kill(os.getpid(), signal.SIGTERM)    # the moment the capture's own handler is in place
    return old
signal.signal = install
sys.exit(m.main(sys.argv[1:]))
'''
        r = subprocess.run([sys.executable, '-c', run, '--port', self.port, '--baud', '115200', '--out', str(self.out),
                            '--seconds', '5', '--lines', 'leave'], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['ended'], 'signal 15')

    def test_a_port_that_is_not_a_terminal_fails_with_json(self):
        plain = self.d / 'plain'
        plain.write_bytes(b'')
        rc, out, err = self.run_cli('--seconds', '0.3', '--lines', 'leave', port=str(plain))
        self.assertEqual(rc, 3, err)
        self.assertIn('plain', out['error'])

    def test_lines_low_is_refused_without_modem_lines(self):
        rc, out, err = self.run_cli('--seconds', '0.5', '--lines', 'low')
        self.assertEqual(rc, 3)
        self.assertIn('--lines low refused', out['error'])

    def test_the_device_going_away_ends_the_capture(self):
        self.feed(b'boot\n', delay=0.2)
        proc = self.start('--seconds', '10', '--lines', 'leave')
        deadline = time.monotonic() + 9    # the capture is reading once the boot line is written out
        while not (self.out.exists() and self.out.read_bytes()) and time.monotonic() < deadline:
            time.sleep(0.05)
        os.close(self.master)
        out, err = proc.communicate(timeout=30)
        out = json.loads(out)
        self.assertEqual(proc.returncode, 3, err)    # a hung-up port's settings could not be put back
        self.assertEqual(out['ended'], 'device gone')
        self.assertFalse(out['terminalRestored'])
        self.assertIn('restoring the port settings', out['error'])
        self.assertEqual(self.out.read_bytes(), b'boot\n')

    def test_a_signal_ends_the_capture_and_restores(self):
        before = termios.tcgetattr(self.slave)
        proc = self.start('--seconds', '10', '--lines', 'leave')
        time.sleep(0.8)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        out = json.loads(out)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out['ended'], 'signal 15')
        self.assertTrue(out['terminalRestored'])
        self.assertEqual(termios.tcgetattr(self.slave), before)

    def test_refusals(self):
        self.out.write_bytes(b'old')
        rc, _, err = self.run_cli('--seconds', '1', '--lines', 'leave')
        self.assertEqual(rc, 2)
        self.assertIn('never overwrites', err)
        self.assertEqual(self.out.read_bytes(), b'old')
        self.out.unlink()
        for args, want in ((('--port', '/dev/ttyACM0'), 'drift'), (('--baud', '123'), 'not a rate'),
                           (('--seconds', '0'), '--seconds'), (('--mark', '('), '--mark')):
            cmd = {'--port': self.port, '--baud': '115200', '--seconds': '1', '--mark': None}
            cmd[args[0]] = args[1]
            argv = [sys.executable, str(SCRIPT), '--out', str(self.out), '--lines', 'leave']
            for k, v in cmd.items():
                if v is not None:
                    argv += [k, v]
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
            self.assertIn(want, r.stderr)
            self.assertFalse(self.out.exists())
        rc, _, err = self.run_cli('--seconds', '1', '--lines', 'leave', port=str(self.d / 'nope'))
        self.assertEqual(rc, 2)
        self.assertIn('no such device', err)


def load_module():
    spec = importlib.util.spec_from_file_location('console_capture_nt', str(SCRIPT))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class NoKernel32:
    def __getattr__(self, name):
        raise AssertionError(f'a test reached the real kernel32.{name}')


class FakePort:
    """The module's Win32 layer over a port that exists only here: its DCB, timeouts and lines.

    The DTR/RTS levels are modelled: every state the lines may pass through is kept in `reached`, and
    a SetCommState that moves both lines may pass through either intermediate state (kernel32 picks
    the order), so (DTR low, RTS high), which resets an Espressif chip, shows up if any step allows it.
    `dtr`/`rts` are the levels the driver holds and reports, `wire` what the device sees: like Windows'
    CDC-ACM driver (ESP32-S3 TRM 33.4.2), a DTR change alone reaches the device only with an RTS write.
    """

    def __init__(self, m):
        self.m = m
        self.log = []
        self.handle = 0x7FFE_1234_5678_0040    # a 64-bit handle value
        self.exists = {'COM7', 'COM12'}
        self.open_error = None
        self.state = m.DCB(DCBlength=ctypes.sizeof(m.DCB), BaudRate=9600, ByteSize=7, Parity=2, StopBits=2,
                           fParity=1, fOutxCtsFlow=1, fOutxDsrFlow=1, fDsrSensitivity=1, fOutX=1, fInX=1,
                           fErrorChar=1, fNull=1, fAbortOnError=1, fDtrControl=m.DTR_CONTROL_HANDSHAKE,
                           fRtsControl=m.RTS_CONTROL_TOGGLE, XonLim=2048, XoffLim=512, EofChar=b'\x1a')
        self.timeouts = m.COMMTIMEOUTS(10, 20, 30, 40, 50)
        self.saved_state, self.saved_timeouts = bytes(self.state), bytes(self.timeouts)
        self.dtr, self.rts = True, False    # as the open left them
        self.wire = self.lines()
        self.reached = []
        self.reports_lines = True
        self.stores_baud = None             # a driver that keeps another rate than the one asked for
        self.reads = []                     # bytes (b'' = a timeout), an exception, or a callable returning bytes
        self.fail = {}                      # wrapper name -> [exception or None per call]

    def opened_at(self, dtr, rts):
        self.dtr, self.rts = dtr, rts
        self.wire = self.lines()

    def patch(self):
        for name in ('nt_port_exists', 'nt_open', 'nt_get_state', 'nt_set_state', 'nt_get_timeouts',
                     'nt_set_timeouts', 'nt_escape', 'nt_lines', 'nt_read', 'nt_close'):
            setattr(self.m, name, getattr(self, name))

    def _call(self, name, *args):
        self.log.append((name, *args))
        queue = self.fail.get(name)
        if queue:
            e = queue.pop(0)
            if e is not None:
                raise e

    def names(self):
        return [c[0] for c in self.log]

    def lines(self):
        return self.dtr, self.rts

    def _move(self, dtr, rts, rts_written=True):
        self.dtr, self.rts = dtr, rts
        if not rts_written:
            return
        if dtr != self.wire[0] and rts != self.wire[1]:
            self.reached += [(dtr, self.wire[1]), (self.wire[0], rts)]
        self.wire = self.lines()
        self.reached.append(self.wire)

    def nt_port_exists(self, name):
        return name in self.exists

    def nt_open(self, device):
        self._call('nt_open', device)
        if self.open_error:
            raise self.open_error
        self.reached.append(self.wire)
        return self.handle

    def _h(self, h):
        assert h == self.handle, h

    def nt_get_state(self, h):
        self._h(h)
        self._call('nt_get_state')
        return self.m.DCB.from_buffer_copy(self.state)

    def nt_set_state(self, h, d):
        self._h(h)
        before = self.lines()
        self._call('nt_set_state', bytes(d), before)
        if d.XonChar == d.XoffChar:
            raise ctypes.WinError(87)    # SetCommState refuses equal XON/XOFF characters
        self.state = self.m.DCB.from_buffer_copy(d)
        if self.stores_baud:
            self.state.BaudRate = self.stores_baud
        # SetCommState drives the lines per the control modes; handshake idles high, toggle low
        self._move(d.fDtrControl != self.m.DTR_CONTROL_DISABLE,
                   d.fRtsControl in (self.m.RTS_CONTROL_ENABLE, self.m.RTS_CONTROL_HANDSHAKE))

    def nt_get_timeouts(self, h):
        self._h(h)
        self._call('nt_get_timeouts')
        return self.m.COMMTIMEOUTS.from_buffer_copy(self.timeouts)

    def nt_set_timeouts(self, h, t):
        self._h(h)
        self._call('nt_set_timeouts', bytes(t))
        self.timeouts = self.m.COMMTIMEOUTS.from_buffer_copy(t)

    def nt_escape(self, h, func):
        self._h(h)
        self._call('nt_escape', func)
        if func == self.m.CLRDTR:
            self._move(False, self.rts, rts_written=False)
        elif func == self.m.CLRRTS:
            self._move(self.dtr, False)

    def nt_lines(self, h):
        self._h(h)
        self._call('nt_lines')
        return {'dtr': self.dtr, 'rts': self.rts} if self.reports_lines else None

    def nt_read(self, h, n):
        self._h(h)
        t = self.timeouts
        assert (t.ReadIntervalTimeout, t.ReadTotalTimeoutMultiplier) == (self.m.MAXDWORD, self.m.MAXDWORD)
        ms = t.ReadTotalTimeoutConstant
        assert 1 <= ms <= self.m.READ_POLL_MS, ms
        self._call('nt_read', ms)
        item = self.reads.pop(0) if self.reads else b''
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        if not item:
            time.sleep(ms / 1000)
            return b''
        time.sleep(0.05)
        return item[:n]

    def nt_close(self, h):
        self._h(h)
        self._call('nt_close', self.lines())


class SignalArrivesAtTheEnd:
    """The module's `signal`, delivering SIGINT twice just as the capture starts ignoring it."""

    def __init__(self):
        self.fired = False

    def __getattr__(self, name):
        return getattr(signal, name)

    def signal(self, sig, handler):
        if handler is signal.SIG_IGN and sig == signal.SIGINT and not self.fired:
            self.fired = True
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
        return signal.signal(sig, handler)


RESET = (False, True)    # DTR low, RTS high: an Espressif auto-reset circuit holds EN low
DOWNLOAD = (True, False)    # DTR high, RTS low: IO0 low, or a USB-Serial-JTAG download flag set


@unittest.skipUnless(os.name == 'nt', 'the Win32 backend; the POSIX one has the pty cases')
class WindowsBackendTest(unittest.TestCase):
    """The plumbing of the Win32 backend against a fake of its kernel32 layer, not a real port."""

    def setUp(self):
        self.m = load_module()
        self.assertIsNone(self.m.termios)
        self.m._k32 = NoKernel32()
        self.port = FakePort(self.m)
        self.port.patch()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        self.out = self.d / 'cap.bin'

    def run_main(self, *args, port='COM7', baud='115200', seconds='0.3', lines='leave'):
        argv = ['--port', port, '--baud', baud, '--seconds', seconds, '--lines', lines, '--out', str(self.out), *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = self.m.main(argv)
            except SystemExit as e:
                rc = e.code
        return rc, json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None, stderr.getvalue()

    def calls(self, name):
        return [c for c in self.port.log if c[0] == name]

    def configured(self):
        return self.m.DCB.from_buffer_copy(self.calls('nt_set_state')[0][1])

    def assert_restored_and_closed(self, out):
        """The saved DCB and timeouts are back, but for DTR/RTS control, which holds the lines: no line moves."""
        self.assertTrue(out['terminalRestored'])
        self.assertEqual(self.port.names()[-4:], ['nt_lines', 'nt_set_state', 'nt_set_timeouts', 'nt_close'])
        _, restored, before = self.calls('nt_set_state')[-1]
        want = self.m.DCB.from_buffer_copy(self.port.saved_state)
        want.fDtrControl, want.fRtsControl = int(before[0]), int(before[1])    # ENABLE 1 / DISABLE 0
        if want.XonChar == want.XoffChar:    # as saved, unless SetCommState would refuse them
            want.XonChar, want.XoffChar = b'\x11', b'\x13'
        self.assertEqual(restored, bytes(want))
        self.assertEqual(self.calls('nt_close')[-1][1], before)
        self.assertEqual(self.calls('nt_set_timeouts')[-1][1], self.port.saved_timeouts)

    def assert_no_reset(self):
        self.assertNotIn(RESET, self.port.reached)

    def test_raw_8n1_at_the_baud_then_the_saved_settings_back(self):
        self.port.state.XonChar = self.port.state.XoffChar = b'\x00'    # equal: SetCommState would refuse them
        self.port.saved_state = bytes(self.port.state)
        rc, out, err = self.run_main(baud='921600', seconds='0.2')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['device'], '\\\\.\\COM7')
        self.assertEqual(self.port.log[0], ('nt_open', '\\\\.\\COM7'))
        configured = self.configured()
        self.assertEqual((configured.BaudRate, configured.ByteSize, configured.Parity, configured.StopBits),
                         (921600, 8, 0, 0))
        self.assertEqual(configured.fBinary, 1)
        for flag in ('fParity', 'fOutxCtsFlow', 'fOutxDsrFlow', 'fDsrSensitivity', 'fOutX', 'fInX', 'fErrorChar',
                     'fNull', 'fAbortOnError'):
            self.assertEqual(getattr(configured, flag), 0, flag)
        # leave: the levels the open left, as plain enable/disable (no handshake, no toggle)
        self.assertEqual((configured.fDtrControl, configured.fRtsControl), (1, 0))
        self.assertEqual((configured.XonChar, configured.XoffChar), (b'\x11', b'\x13'))
        self.assertEqual((configured.XonLim, configured.XoffLim, configured.EofChar), (2048, 512, b'\x1a'))
        self.assertEqual(out['lines'], {'policy': 'leave', 'afterSetup': {'dtr': True, 'rts': False}})
        self.assertNotIn('nt_escape', self.port.names())
        self.assertNotIn('effectiveBaud', out)
        self.assertEqual(out['ended'], 'time')
        self.assertTrue(out['baseline'].startswith('none'))
        self.assertNotIn('otherOpeners', out)
        self.assertEqual(set(self.port.reached), {(True, False)})    # no line ever moved
        self.assert_restored_and_closed(out)

    def test_distinct_xon_xoff_characters_are_kept(self):
        self.port.state.XonChar, self.port.state.XoffChar = b'\x01', b'\x02'
        self.port.saved_state = bytes(self.port.state)
        rc, out, err = self.run_main(seconds='0.1')
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.configured().XonChar, self.configured().XoffChar), (b'\x01', b'\x02'))
        restored = self.m.DCB.from_buffer_copy(self.calls('nt_set_state')[-1][1])
        self.assertEqual((restored.XonChar, restored.XoffChar), (b'\x01', b'\x02'))
        self.assert_restored_and_closed(out)

    def test_leave_without_a_line_report_follows_the_saved_control_modes(self):
        self.port.reports_lines = False
        rc, out, err = self.run_main(seconds='0.1')
        self.assertEqual(rc, 0, err)
        # DTR handshake idles high, RTS toggle idles low
        self.assertEqual((self.configured().fDtrControl, self.configured().fRtsControl), (1, 0))
        self.assertIsNone(out['lines']['afterSetup'])
        restored = self.m.DCB.from_buffer_copy(self.calls('nt_set_state')[-1][1])
        self.assertEqual((restored.fDtrControl, restored.fRtsControl), (1, 0))    # the levels it set, held

    def test_lines_low_clears_rts_then_dtr_after_the_configuration(self):
        self.port.opened_at(True, True)
        rc, out, err = self.run_main(lines='low', seconds='0.1')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.port.names()[:10], ['nt_open', 'nt_get_state', 'nt_get_timeouts', 'nt_lines',
                                                  'nt_set_state', 'nt_get_state', 'nt_escape', 'nt_escape',
                                                  'nt_escape', 'nt_lines'])
        self.assertEqual((self.configured().fDtrControl, self.configured().fRtsControl), (1, 1))    # held
        # the second CLRRTS carries DTR low to the device
        self.assertEqual(self.calls('nt_escape'), [('nt_escape', self.m.CLRRTS), ('nt_escape', self.m.CLRDTR),
                                                   ('nt_escape', self.m.CLRRTS)])
        self.assertEqual(self.port.reached, [(True, True), (True, True), DOWNLOAD, (False, False),
                                             (False, False)])
        self.assertEqual(out['lines'], {'policy': 'low', 'afterSetup': {'dtr': False, 'rts': False}})
        self.assert_no_reset()
        self.assert_restored_and_closed(out)
        self.assertEqual(self.port.lines(), (False, False))    # the restore did not raise them again
        self.assertEqual(self.port.wire, (False, False))

    def test_lines_low_with_unknown_levels_never_passes_the_reset_state(self):
        for opened in ((True, True), (False, False), DOWNLOAD, RESET):
            with self.subTest(opened=opened):
                self.setUp()
                self.port.reports_lines = False
                self.port.opened_at(*opened)
                rc, out, err = self.run_main(lines='low', seconds='0.05')
                self.assertEqual(rc, 0, err)
                self.assertEqual((self.configured().fDtrControl, self.configured().fRtsControl), (0, 0))
                if opened == RESET:
                    # out of reset with IO0 high (RTS cleared alone), and IO0 never low after it
                    self.assertEqual(self.port.reached[:2], [RESET, (False, False)])
                    self.assertNotIn(RESET, self.port.reached[1:])
                    self.assertNotIn(DOWNLOAD, self.port.reached[1:])
                else:
                    self.assert_no_reset()
                self.assertEqual(self.port.lines(), (False, False))
                self.assertEqual(self.port.wire, (False, False))
                restored = self.m.DCB.from_buffer_copy(self.calls('nt_set_state')[-1][1])
                self.assertEqual((restored.fDtrControl, restored.fRtsControl), (0, 0))

    def test_lines_low_refused_when_the_driver_cannot_clear_them(self):
        self.port.fail['nt_escape'] = [ctypes.WinError(1)]
        rc, out, err = self.run_main(lines='low')
        self.assertEqual(rc, 3, err)
        self.assertIn('--lines low refused', out['error'])
        self.assertNotIn('nt_read', self.port.names())
        self.assert_restored_and_closed(out)

    def test_lines_low_with_unknown_levels_refused_when_rts_cannot_be_cleared_first(self):
        self.port.reports_lines = False
        self.port.opened_at(*RESET)
        self.port.fail['nt_escape'] = [ctypes.WinError(1)]
        rc, out, err = self.run_main(lines='low')
        self.assertEqual(rc, 3, err)
        self.assertIn('--lines low refused', out['error'])
        # refused before any DCB, which could leave the reset with IO0 low
        self.assertEqual([c[0] for c in self.port.log if c[0] in ('nt_escape', 'nt_set_state')], ['nt_escape'])
        # nothing of ours was set: the saved modes are not written back, which could move the lines
        self.assertTrue(out['terminalRestored'])
        self.assertEqual(self.port.names()[-2:], ['nt_set_timeouts', 'nt_close'])
        self.assertEqual(self.port.reached, [RESET])

    def test_lines_low_with_unknown_levels_and_a_refused_dcb_keeps_rts_low(self):
        for opened in ((True, True), (False, False), DOWNLOAD, RESET):
            with self.subTest(opened=opened):
                self.setUp()
                self.port.state.fRtsControl = self.m.RTS_CONTROL_ENABLE    # writing the saved modes raises RTS
                self.port.saved_state = bytes(self.port.state)
                self.port.reports_lines = False
                self.port.opened_at(*opened)
                self.port.fail['nt_set_state'] = [ctypes.WinError(87)]    # the driver refuses our DCB
                rc, out, err = self.run_main(lines='low')
                self.assertEqual(rc, 3, err)
                self.assertNotIn('nt_read', self.port.names())
                self.assertTrue(out['terminalRestored'])
                self.assertEqual(self.port.names()[-6:], ['nt_escape', 'nt_set_state', 'nt_lines', 'nt_set_state',
                                                          'nt_set_timeouts', 'nt_close'])
                refused, restored = (self.m.DCB.from_buffer_copy(c[1]) for c in self.calls('nt_set_state'))
                self.assertEqual((refused.fDtrControl, refused.fRtsControl), (0, 0))
                self.assertEqual((restored.fDtrControl, restored.fRtsControl), (0, 0))
                self.assertEqual(self.port.wire, (False, False))
                left = self.port.reached.index((opened[0], False))    # the CLRRTS, or the open if RTS was already low
                self.assertNotIn(RESET, self.port.reached[left:])
                if opened == RESET:
                    self.assertNotIn(DOWNLOAD, self.port.reached[left:])

    def test_an_early_error_restores_without_moving_a_line(self):
        self.port.opened_at(False, False)    # the saved modes (handshake/toggle) would raise DTR
        self.port.fail['nt_set_state'] = [ctypes.WinError(87)]    # the driver refuses the rate
        rc, out, err = self.run_main(baud='12345678')
        self.assertEqual(rc, 3, err)
        self.assertIn('COM7: ', out['error'])
        self.assertNotIn('nt_read', self.port.names())
        self.assert_restored_and_closed(out)
        self.assertEqual(set(self.port.reached), {(False, False)})

    def test_a_rate_the_driver_stores_differently_is_refused(self):
        self.port.stores_baud = 230400
        rc, out, err = self.run_main(baud='250000')
        self.assertEqual(rc, 3, err)
        self.assertEqual(out['effectiveBaud'], 230400)
        self.assertIn('the driver set 230400 baud, not 250000', out['error'])
        self.assertNotIn('nt_read', self.port.names())
        self.assert_restored_and_closed(out)

    def test_chunks_times_and_marks(self):
        self.port.reads = [b'', b'ESP-ROM:esp32p4\r\nrst:0x17 (CHIP_USB_UART_RESET)\r\n', b'', b'\x00\xff binary\n']
        t0 = time.monotonic()
        rc, out, err = self.run_main('--mark', r'rst:0x[0-9a-f]+', seconds='0.6')
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 0, err)
        data = self.out.read_bytes()
        self.assertEqual(data, b'ESP-ROM:esp32p4\r\nrst:0x17 (CHIP_USB_UART_RESET)\r\n\x00\xff binary\n')
        self.assertEqual((out['bytes'], out['chunks']), (len(data), 2))
        self.assertEqual(out['ended'], 'time')
        self.assertEqual(out['baseline'], 'the open')
        times = [json.loads(line) for line in Path(out['times']).read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(t['offset'], t['len']) for t in times], [(0, 49), (49, 10)])
        self.assertGreater(times[0]['t'], 0.05)
        self.assertLess(times[0]['t'], times[1]['t'])
        self.assertEqual(out['firstByteAt'], times[0]['t'])
        self.assertEqual(out['marks'], [{'pattern': r'rst:0x[0-9a-f]+', 'offset': 17, 't': times[0]['t'],
                                         'text': 'rst:0x17'}])
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertLess(elapsed, 0.6 + 0.5)
        polls = [c[1] for c in self.calls('nt_read')]
        self.assertTrue(all(1 <= ms <= self.m.READ_POLL_MS for ms in polls), polls)
        self.assert_restored_and_closed(out)

    def test_each_chunk_time_is_written_as_it_arrives(self):
        times = Path(str(self.out) + '.times')
        seen = []
        self.port.reads = [b'boot\n', lambda: (seen.append(times.read_bytes()), b'')[1]]
        rc, out, err = self.run_main(seconds='0.2')
        self.assertEqual(rc, 0, err)
        self.assertEqual([json.loads(line)['len'] for line in seen[0].decode().splitlines()], [5])

    def test_ctrl_c_and_ctrl_break_end_the_capture_and_restore(self):
        for sig in (signal.SIGINT, signal.SIGBREAK):
            with self.subTest(sig=sig):
                self.setUp()
                self.port.reads = [b'boot\n', lambda: (signal.raise_signal(sig), b'')[1]]
                t0 = time.monotonic()
                rc, out, err = self.run_main(seconds='10')
                self.assertLess(time.monotonic() - t0, 2)
                self.assertEqual(rc, 0, err)
                self.assertEqual(out['ended'], f'signal {int(sig)}')
                self.assertEqual(self.out.read_bytes(), b'boot\n')
                self.assert_restored_and_closed(out)

    def test_a_signal_as_the_capture_ends_still_restores_and_reports(self):
        self.m.signal = SignalArrivesAtTheEnd()
        rc, out, err = self.run_main(seconds='0.1')
        self.assertTrue(self.m.signal.fired)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['ended'], 'time')
        self.assert_restored_and_closed(out)

    def test_a_busy_port_is_refused_without_its_holder(self):
        for code in (5, 32):    # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
            with self.subTest(code=code):
                self.setUp()
                self.port.open_error = ctypes.WinError(code)
                rc, out, err = self.run_main()
                self.assertEqual(rc, 3, err)
                self.assertIn('COM7: busy or being removed: ' + ctypes.WinError(code).strerror, out['error'])
                self.assertNotIn('otherOpeners', out)
                self.assertNotIn('terminalRestored', out)
                self.assertEqual(self.port.names(), ['nt_open'])
                self.assertEqual(self.out.read_bytes(), b'')

    def test_another_open_failure_is_not_busy(self):
        self.port.open_error = ctypes.WinError(2)
        rc, out, err = self.run_main()
        self.assertEqual(rc, 3, err)
        self.assertNotIn('busy', out['error'])

    def test_a_read_error_fails_and_restores(self):
        self.port.reads = [b'boot\n', ctypes.WinError(87)]
        rc, out, err = self.run_main(seconds='5')
        self.assertEqual(rc, 3, err)
        self.assertEqual(out['bytes'], 5)
        self.assertIn('COM7: ', out['error'])
        self.assert_restored_and_closed(out)

    def test_the_device_going_away_ends_the_capture(self):
        for code in (1167, 995):    # ERROR_DEVICE_NOT_CONNECTED, ERROR_OPERATION_ABORTED
            with self.subTest(code=code):
                self.setUp()
                self.port.reads = [b'boot\n', ctypes.WinError(code)]
                self.port.fail['nt_set_state'] = [None, ctypes.WinError(code)]
                self.port.fail['nt_set_timeouts'] = [None, ctypes.WinError(code)]
                rc, out, err = self.run_main(seconds='5')
                self.assertEqual(rc, 3, err)
                self.assertEqual(out['ended'], 'device gone')
                self.assertFalse(out['terminalRestored'])
                self.assertIn('restoring the port settings', out['error'])
                self.assertEqual(self.port.names()[-3:], ['nt_set_state', 'nt_set_timeouts', 'nt_close'])
                self.assertEqual(self.out.read_bytes(), b'boot\n')

    def test_reads_that_keep_ending_empty_at_once_are_a_gone_device(self):
        self.port.reads = [b'boot\n'] + [lambda: b''] * 3
        t0 = time.monotonic()
        rc, out, err = self.run_main(seconds='5')
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['ended'], 'device gone')
        self.assertEqual(out['bytes'], 5)

    def test_a_few_early_empty_reads_do_not_end_the_capture(self):
        quick = lambda: b''    # noqa: E731
        self.port.reads = [quick, quick, b'', quick, quick, b'', b'boot\n', quick, quick]
        rc, out, err = self.run_main(seconds='0.4')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['ended'], 'time')
        self.assertEqual(out['bytes'], 5)

    def test_port_names_and_refusals(self):
        for port, device in (('COM7', 'COM7'), ('com7', 'COM7'), ('\\\\.\\COM7', 'COM7'), ('\\\\.\\com12', 'COM12')):
            with self.subTest(port=port):
                self.setUp()
                rc, out, err = self.run_main(port=port, seconds='0.05')
                self.assertEqual(rc, 0, err)
                self.assertEqual((out['port'], out['device']), (port, '\\\\.\\' + device))
                self.assertEqual(self.port.log[0], ('nt_open', '\\\\.\\' + device))
        for kw, want in (({'port': '/dev/ttyACM0'}, 'COM<n>'), ({'port': '/dev/serial/by-id/x'}, 'COM<n>'),
                         ({'port': 'COM0'}, 'COM<n>'), ({'port': 'COM7x'}, 'COM<n>'),
                         ({'port': 'COM9'}, 'no such device'), ({'baud': '0'}, 'not a rate'),
                         ({'baud': str(1 << 32)}, 'not a rate'), ({'seconds': '0'}, '--seconds')):
            with self.subTest(kw=kw):
                self.setUp()
                rc, out, err = self.run_main(**kw)
                self.assertEqual(rc, 2, err)
                self.assertIn(want, err)
                self.assertIsNone(out)
                self.assertEqual(self.port.log, [])
                self.assertFalse(self.out.exists())
        self.out.write_bytes(b'old')
        rc, _, err = self.run_main()
        self.assertEqual(rc, 2)
        self.assertIn('never overwrites', err)
        self.assertEqual(self.out.read_bytes(), b'old')
        self.assertEqual(self.port.log, [])


@unittest.skipUnless(os.name == 'nt', 'kernel32')
class Win32LayerTest(unittest.TestCase):
    """The ctypes declarations against real kernel32 on a plain file: no COM port is opened."""

    def setUp(self):
        self.m = load_module()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / 'plain.bin'
        self.path.write_bytes(b'abc')

    def test_dcb_layout_matches_winbase_h(self):
        m = self.m
        self.assertEqual(ctypes.sizeof(m.DCB), 28)
        raw = bytes(m.DCB(fDtrControl=2, fRtsControl=3, fAbortOnError=1, ByteSize=8, StopBits=2))
        self.assertEqual(int.from_bytes(raw[8:12], 'little'), 2 << 4 | 3 << 12 | 1 << 14)
        self.assertEqual((raw[18], raw[20]), (8, 2))
        self.assertIs(m._k32.CreateFileW.restype, ctypes.wintypes.HANDLE)

    def test_wrappers_on_a_plain_file(self):
        m = self.m
        h = m.nt_open(str(self.path))
        try:
            self.assertGreater(h, 0)
            with self.assertRaises(OSError) as busy:    # opened without sharing, as a comm port must be
                m.nt_open(str(self.path))
            self.assertEqual(busy.exception.winerror, m.ERROR_SHARING_VIOLATION)
            self.assertEqual(m.nt_read(h, 16), b'abc')
            self.assertIsNone(m.nt_lines(h))
            with self.assertRaises(OSError) as e:
                m.nt_get_state(h)
            self.assertIsNotNone(e.exception.winerror)
        finally:
            m.nt_close(h)
        self.assertFalse(m.nt_port_exists('NO_SUCH_DOS_DEVICE_4f1c'))


if __name__ == '__main__':
    unittest.main()
