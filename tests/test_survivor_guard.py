"""helpers.guard_survivors, the guard every module that spawns processes runs its tests under: a process a test
leaves running, a double-forked one included, is named and killed, whether the test passed, failed or timed out;
a process that was already running is left alone; the child-subreaper flag comes back as it was."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import SURVIVOR_GUARD_OFF, end, guard_survivors, proc_stat  # noqa: E402

# the guard in an interpreter of its own, so nothing of this suite is in its baselines: subreaper flag argv[2]
# before the run; tests that each write the pid they leave behind to argv[3]/<test name>; a process started
# before them; each test's outcome, whether each leaked pid is gone, the earlier process still running, and the flag
DRIVER = r'''
import io, json, os, subprocess, sys, time, unittest
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import helpers
helpers.set_subreaper(int(sys.argv[2]))
helpers.SURVIVOR_GRACE = 0.2
out = Path(sys.argv[3])


def leaked(name, pid):
    (out / name).write_text(str(pid))


def started(pid):
    deadline = time.monotonic() + 5
    while helpers.cmdline(pid) != 'sleep 60' and time.monotonic() < deadline:
        time.sleep(0.02)


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: None)


class Leaks(Base):
    def setUp(self):
        super().setUp()

    def test_child(self):
        leaked('child', subprocess.Popen(['sleep', '60']).pid)

    def test_double_forked(self):
        pid = int(subprocess.run(['sh', '-c', 'setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $!'],
                                 capture_output=True, text=True, check=True, timeout=10).stdout)
        started(pid)
        leaked('double_forked', pid)

    def test_failed(self):
        leaked('failed', subprocess.Popen(['sleep', '60']).pid)
        self.fail('the test fails')

    def test_timed_out(self):
        subprocess.run(['sh', '-c', f'sleep 60 & echo $! > {out}/timed_out; wait'], timeout=1)

    def test_clean(self):
        subprocess.run(['sleep', '0'], check=True)


earlier = subprocess.Popen(['sleep', '60'])
helpers.guard_survivors(__name__)
result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(Leaks))
outcomes = {}
for t, e in result.failures + result.errors:
    name = t.id().rsplit('.', 1)[1].removeprefix('test_')
    outcomes[name] = outcomes.get(name, '') + e
pids = {p.name: int(p.read_text()) for p in out.iterdir()}
running = earlier.poll() is None
earlier.kill()
earlier.wait()
gone = {n: helpers.proc_stat(p) is None for n, p in pids.items()}
print(json.dumps({'outcomes': outcomes, 'pids': pids, 'gone': gone,
                  'earlier': running, 'subreaper': helpers.get_subreaper(), 'run': result.testsRun}))
'''


def setUpModule():
    guard_survivors(__name__)


@unittest.skipIf(SURVIVOR_GUARD_OFF, f'survivor guard off: {SURVIVOR_GUARD_OFF}')
class SurvivorGuard(unittest.TestCase):
    def test_survivors_are_named_and_killed_others_left_and_the_flag_restored(self):
        for before in (0, 1):
            with self.subTest(subreaper=before), tempfile.TemporaryDirectory() as out:
                r = subprocess.run([sys.executable, '-c', DRIVER, str(Path(__file__).resolve().parent), str(before),
                                    out], capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr)
                res = json.loads(r.stdout)
                self.assertEqual(res['run'], 5)
                self.assertEqual(sorted(res['pids']), ['child', 'double_forked', 'failed', 'timed_out'])
                self.assertEqual(sorted(res['outcomes']), sorted(res['pids']))
                for name, pid in res['pids'].items():
                    self.assertIn(f'killed processes that outlived their test: {pid} sleep 60', res['outcomes'][name])
                    self.assertTrue(res['gone'][name], name)
                self.assertIn('the test fails', res['outcomes']['failed'])
                self.assertIn('TimeoutExpired', res['outcomes']['timed_out'])
                self.assertTrue(res['earlier'])
                self.assertEqual(res['subreaper'], before)

    def test_a_pid_whose_start_time_differs_is_left_alone(self):
        p = subprocess.Popen(['sleep', '60'])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        end(p.pid, '1')    # its pid, an earlier process's start time
        self.assertIsNone(p.poll())
        self.assertIn(proc_stat(p.pid)[1], 'RS')


if __name__ == '__main__':
    unittest.main()
