import json
import os
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

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
        time.sleep(1.0)
        os.close(self.master)
        out, err = proc.communicate(timeout=30)
        out = json.loads(out)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out['ended'], 'device gone')
        self.assertIsNone(out['terminalRestored'])
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


if __name__ == '__main__':
    unittest.main()
