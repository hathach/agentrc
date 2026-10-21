"""helpers.guard_survivors, the guard every module that spawns processes runs its tests under: a process a test
leaves running, a double-forked one or a zombie leader with a live thread included, is named and killed, whether
the test passed, failed or timed out; a process that was already running is left alone; the child-subreaper flag
comes back as it was, and a flag it cannot set or restore fails."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402
from helpers import SURVIVOR_GUARD_OFF, end, guard_survivors, proc_stat  # noqa: E402

# the guard in an interpreter of its own, so nothing of this suite is in its baselines: subreaper flag argv[2]
# before the run; tests that each write the pid they leave behind to argv[3]/<test name>; a process started
# before them; each test's outcome, whether each leaked pid is gone, the earlier process still running, the flag,
# and how the zombie leader looked while its thread ran
DRIVER = r'''
import io, json, os, subprocess, sys, time, unittest
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import helpers
from test_hil_process import THREAD_LEADER
helpers.set_subreaper(int(sys.argv[2]))
helpers.SURVIVOR_GRACE = 0.2
out = Path(sys.argv[3])
leader = {}


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

    def test_thread_leader(self):
        p = subprocess.Popen([sys.executable, '-c', THREAD_LEADER, str(out / 'thread_leader')])
        deadline = time.monotonic() + 10
        while (helpers.proc_stat(p.pid) or (0, ''))[1] != 'Z' and time.monotonic() < deadline:
            time.sleep(0.02)
        leader.update(zombie=helpers.proc_stat(p.pid)[1] == 'Z', alive=helpers.pid_alive(p.pid), poll=p.poll())

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
                  'earlier': running, 'leader': leader, 'subreaper': helpers.get_subreaper(), 'run': result.testsRun}))
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
                self.assertEqual(res['run'], 6)
                self.assertEqual(sorted(res['pids']),
                                 ['child', 'double_forked', 'failed', 'thread_leader', 'timed_out'])
                self.assertEqual(sorted(res['outcomes']), sorted(res['pids']))
                for name, pid in res['pids'].items():
                    command = '' if name == 'thread_leader' else ' sleep 60'
                    self.assertIn(f'killed processes that outlived their test: {pid}{command}', res['outcomes'][name])
                    self.assertTrue(res['gone'][name], name)
                self.assertEqual(res['leader'], {'zombie': True, 'alive': True, 'poll': None})
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

    def test_a_listed_pid_that_is_not_the_listing_processs_child_is_skipped(self):
        real = helpers.children

        def with_a_stranger(pid):    # as a child that exited and whose pid was reused after the list was read
            return real(pid) + [os.getppid()] if pid == os.getpid() else real(pid)
        with mock.patch.object(helpers, 'children', with_a_stranger):
            self.assertNotIn(os.getppid(), helpers.descendants())

    def test_the_children_of_a_parent_whose_pid_was_reused_are_skipped(self):
        parent, kid = 2 ** 30, 2 ** 30 + 1      # past any pid_max: no real process
        tree = {os.getpid(): [parent], parent: [kid]}
        stats = {kid: [(parent, 'S', '3')]}
        stats[parent] = [(os.getpid(), 'S', '1'), (os.getpid(), 'S', '2')]    # reused after its list was read
        real = helpers.proc_stat

        def proc_stat(pid):
            return (stats[pid].pop(0) if len(stats[pid]) > 1 else stats[pid][0]) if pid in stats else real(pid)
        with mock.patch.object(helpers, 'children', lambda pid: tree.get(pid, [])), \
                mock.patch.object(helpers, 'proc_stat', proc_stat):
            self.assertEqual(helpers.descendants(), {parent: (os.getpid(), 'S', '1')})

    def test_a_survivor_reparented_while_a_walk_runs_is_still_found(self):
        baseline = {(pid, st[2]) for pid, st in helpers.descendants().items()}
        p = subprocess.Popen(['sleep', '60'])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        real, missed = helpers.children, []

        def first_walk_misses_it(pid):    # its parent exited after the list was read, so no list held it
            kids = real(pid)
            if pid == os.getpid() and not missed:
                missed.append(p.pid)
                return [k for k in kids if k != p.pid]
            return kids
        with mock.patch.object(helpers, 'children', first_walk_misses_it), \
                mock.patch.object(helpers, 'SURVIVOR_GRACE', 0), \
                self.assertRaisesRegex(AssertionError, f'outlived their test: {p.pid} sleep 60'):
            helpers.check_no_survivors(baseline)
        self.assertEqual(missed, [p.pid])

    def test_a_flag_it_cannot_set_or_restore_fails(self):
        class Case:
            def addCleanup(self, check):
                self.check = check
        with mock.patch.object(helpers, 'set_subreaper', return_value=False), \
                self.assertRaisesRegex(AssertionError, 'could not set the child-subreaper flag'):
            helpers.start_survivor_guard(Case())
        real, first = helpers.set_subreaper, [True]

        def sets_but_never_restores(flag):
            if first:
                first.pop()
                return real(flag)
            return False
        case = Case()
        with mock.patch.object(helpers, 'set_subreaper', sets_but_never_restores):
            helpers.start_survivor_guard(case)
            with self.assertRaisesRegex(AssertionError, 'could not restore the child-subreaper flag to'):
                case.check()


if __name__ == '__main__':
    unittest.main()
