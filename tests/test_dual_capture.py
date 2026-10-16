import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts' / 'dual_capture.py'


def alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


@unittest.skipIf(not sys.platform.startswith('linux'), 'reads /proc')
class DualCaptureTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        self.out = self.d / 'out'

    def side(self, name, code):
        f = self.d / f'{name}.json'
        f.write_text(json.dumps([sys.executable, '-c', code]))
        return f'{name}={f}'

    def start(self, a, b, *args):
        return subprocess.Popen([sys.executable, str(SCRIPT), '--a', a, '--b', b, '--out', str(self.out), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def run_cli(self, a, b, *args):
        proc = self.start(a, b, *args)
        out, err = proc.communicate(timeout=60)
        return proc.returncode, json.loads(out) if out.strip() else None, err

    def test_both_sides_run_and_their_output_is_kept(self):
        rc, out, err = self.run_cli(self.side('host', 'import time; print("urb"); time.sleep(0.6)'),
                                    self.side('target', 'import sys, time; time.sleep(0.2); sys.stderr.write("log")'),
                                    '--timeout', '10')
        self.assertEqual(rc, 0, err)
        host, target = out['sides']
        self.assertEqual((self.out / 'host.out').read_text(), 'urb\n')
        self.assertEqual((self.out / 'target.err').read_text(), 'log')
        self.assertEqual([host['returncode'], target['returncode']], [0, 0])
        self.assertGreater(out['bothRunning']['seconds'], 0.1)
        self.assertLessEqual(out['bothRunning']['to'], host['exitedAt'])
        self.assertTrue(out['readiness'].startswith('not observed'))

    def test_the_bound_stops_a_side_and_an_escaped_descendant(self):
        pidfile = self.d / 'grandchild.pid'
        code = ('import subprocess, sys, time; '
                f'p = subprocess.Popen(["setsid", "sleep", "60"]); open({str(pidfile)!r}, "w").write(str(p.pid)); '
                'print("partial", flush=True); time.sleep(60)')
        rc, out, err = self.run_cli(self.side('host', code), self.side('target', 'print("done")'),
                                    '--timeout', '1.5', '--grace', '1')
        self.assertEqual(rc, 1, err)
        host = out['sides'][0]
        self.assertTrue(host['stoppedAtBound'])
        self.assertEqual((self.out / 'host.out').read_text(), 'partial\n')
        time.sleep(0.3)
        self.assertFalse(alive(int(pidfile.read_text())))
        self.assertFalse(out['sides'][1]['stoppedAtBound'])

    def test_a_side_that_cannot_start_stops_the_other(self):
        bad = self.d / 'bad.json'
        bad.write_text(json.dumps(['/nonexistent/tool']))
        rc, out, err = self.run_cli(self.side('host', 'import time; time.sleep(30)'), f'target={bad}',
                                    '--timeout', '20', '--grace', '1')
        self.assertEqual(rc, 3, err)
        self.assertIn('startError', out['sides'][1])
        self.assertTrue(out['sides'][0]['stoppedAtBound'])

    def test_a_failing_side_fails_the_run(self):
        rc, out, err = self.run_cli(self.side('host', 'raise SystemExit(4)'), self.side('target', 'pass'),
                                    '--timeout', '10')
        self.assertEqual(rc, 1)
        self.assertEqual(out['sides'][0]['returncode'], 4)

    def test_a_signal_stops_both(self):
        proc = self.start(self.side('host', 'import time; time.sleep(30)'),
                          self.side('target', 'import time; time.sleep(30)'), '--timeout', '30', '--grace', '1')
        time.sleep(1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        out = json.loads(out)
        self.assertEqual(proc.returncode, 1, err)
        self.assertEqual(out['interrupted'], [15])
        self.assertTrue(all(s['stoppedAtBound'] for s in out['sides']))

    def test_refusals(self):
        good = self.side('host', 'pass')
        notlist = self.d / 'n.json'
        notlist.write_text('"echo hi"')
        for args, want in (((good, good, '--timeout', '1'), 'different names'),
                           ((good, f'x={notlist}', '--timeout', '1'), 'JSON list of strings'),
                           ((good, 'nameonly', '--timeout', '1'), '<name>=<argv.json>'),
                           ((good, f'a/b={notlist}', '--timeout', '1'),
                            'letters, digits'),
                           ((good, self.side('target', 'pass'), '--timeout', '0'), '--timeout')):
            rc, _, err = self.run_cli(*args)
            self.assertEqual(rc, 2, (args, err))
            self.assertIn(want, err)
        self.out.mkdir()
        rc, _, err = self.run_cli(good, self.side('target', 'pass'), '--timeout', '1')
        self.assertEqual(rc, 2)
        self.assertIn('exists', err)


if __name__ == '__main__':
    unittest.main()
