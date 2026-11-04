"""job_hook.py's hold decisions against a fake cgroup tree and /proc: the runner's chain is
runsvc.sh 100 -> node 110 -> Runner.Listener 120 -> Runner.Worker 130 -> this hook 500."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the HIL farm setup runs on Debian: bash, fcntl, Linux paths')

import importlib.util
import shutil
import tempfile
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

FARM = Path(__file__).resolve().parent.parent / 'skills' / 'hil-farm-setup' / 'scripts'
spec = importlib.util.spec_from_file_location('job_hook', FARM / 'job_hook.py')
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

UNIT = 'hil-runner-tinyusb'
CHAIN = {100: 1, 110: 100, 120: 110, 130: 120, 500: 130}
JOB = {'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'hil-tinyusb'}


class JobHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cg = self.tmp / 'cg' / f'{UNIT}.service'
        self.cg.mkdir(parents=True)
        self.proc = self.tmp / 'proc'
        self.procs = dict(CHAIN)
        for name, value in (('CGROUP_ROOT', self.tmp / 'cg'), ('PROC', self.proc), ('MARKER', self.tmp / 'marker')):
            p = mock.patch.object(hook, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.object(hook.os, 'getpid', lambda: 500), mock.patch.dict(os.environ, JOB)):
            p.start()
            self.addCleanup(p.stop)
        self.sync()

    def sync(self, cgroup=None):
        """Write /proc and the unit's cgroup.procs from self.procs (`cgroup`: which pids are listed)."""
        shutil.rmtree(self.proc, ignore_errors=True)
        for pid, ppid in self.procs.items():
            d = self.proc / str(pid)
            d.mkdir(parents=True)
            (d / 'stat').write_text(f'{pid} (a) b) S {ppid} 1 1 0\n')
            (d / 'cmdline').write_bytes(f'cmd{pid}\0arg\0'.encode())
        listed = sorted(self.procs) if cgroup is None else cgroup
        (self.cg / 'cgroup.procs').write_text(''.join(f'{p}\n' for p in listed))

    def run_hook(self, verb):
        with mock.patch.object(hook.sys, 'stderr') as err:
            rc = hook.main([verb, UNIT])
        return rc, ''.join(c.args[0] for c in err.write.call_args_list)

    def marker(self):
        return (self.tmp / 'marker').read_text().strip() if (self.tmp / 'marker').exists() else None

    def test_a_clean_start_writes_this_jobs_id_and_complete_marks_it(self):
        self.assertEqual(self.run_hook('start'), (0, ''))
        self.assertEqual(self.marker(), '77-1-hil-tinyusb')
        self.assertEqual(self.run_hook('complete')[0], 0)
        self.assertEqual(self.marker(), '77-1-hil-tinyusb done')
        with mock.patch.dict(os.environ, {'GITHUB_RUN_ID': '78'}):
            self.assertEqual(self.run_hook('start')[0], 0)
        self.assertEqual(self.marker(), '78-1-hil-tinyusb')

    def test_a_job_that_never_completed_holds(self):
        (self.tmp / 'marker').write_text('70-1-hil-tinyusb\n')
        rc, err = self.run_hook('start')
        self.assertEqual(rc, 1)
        self.assertIn('job 70-1-hil-tinyusb never reached its completed hook', err)
        self.assertEqual(self.marker(), '70-1-hil-tinyusb')

    def test_a_refused_jobs_complete_leaves_the_hold(self):
        (self.tmp / 'marker').write_text('70-1-hil-tinyusb\n')
        self.run_hook('start')
        self.assertEqual(self.run_hook('complete')[0], 0)
        self.assertEqual(self.marker(), '70-1-hil-tinyusb')

    def test_a_leftover_process_holds_root_owned_and_orphaned_ones_included(self):
        for left in ({900: 1}, {900: 130}, {900: 1, 901: 900}):
            with self.subTest(left=left):
                self.procs = {**CHAIN, **left}
                self.sync()
                rc, err = self.run_hook('start')
                self.assertEqual(rc, 1)
                self.assertIn('processes left from an earlier job: 900 cmd900 arg', err)
                self.assertIsNone(self.marker())

    def test_a_process_in_a_child_cgroup_counts(self):
        self.procs = {**CHAIN, 900: 1}
        self.sync(cgroup=sorted(CHAIN))
        (self.cg / 'sub').mkdir()
        (self.cg / 'sub' / 'cgroup.procs').write_text('900\n')
        self.assertEqual(self.run_hook('start')[0], 1)

    def test_the_hooks_own_children_and_vanished_pids_do_not(self):
        self.procs = {**CHAIN, 600: 500}
        self.sync(cgroup=sorted(CHAIN) + [600, 950])     # 950 exited between the listing and the look
        self.assertEqual(self.run_hook('start')[0], 0)

    def test_a_live_process_whose_parent_exits_mid_walk_still_counts(self):
        self.procs = {**CHAIN, 900: 901, 901: 1}
        self.sync()
        real = hook.parent

        def parent(pid):
            return None if pid == 901 else real(pid)    # 901 exits between the listing and the walk
        with mock.patch.object(hook, 'parent', parent):
            self.assertEqual(hook.survivors(UNIT), [900])

    def test_a_pid_that_joins_during_the_scan_is_seen(self):
        self.procs = {**CHAIN, 900: 1}
        self.sync(cgroup=sorted(CHAIN))
        listings = iter([set(CHAIN), set(CHAIN) | {900}, set(CHAIN) | {900}])
        with mock.patch.object(hook, 'unit_pids', lambda unit: next(listings)):
            self.assertEqual(hook.survivors(UNIT), [900])

    def test_a_cgroup_that_never_settles_holds(self):
        counter = iter(range(1000, 2000))
        with mock.patch.object(hook, 'unit_pids', lambda unit: set(CHAIN) | {next(counter)}):
            with self.assertRaises(hook.Hold):
                hook.survivors(UNIT)

    def test_doubt_holds(self):
        cases = {'unreadable cgroup': lambda: shutil.rmtree(self.cg),
                 'empty cgroup': lambda: (self.cg / 'cgroup.procs').write_text(''),
                 'no job id': lambda: os.environ.pop('GITHUB_JOB')}
        for why, make in cases.items():
            with self.subTest(why), mock.patch.dict(os.environ, JOB):
                self.cg.mkdir(parents=True, exist_ok=True)
                self.sync()
                make()
                self.assertEqual(self.run_hook('start')[0], 1, why)
                self.assertIsNone(self.marker())

    def test_only_its_own_units_cgroup_counts(self):
        other = self.tmp / 'cg' / 'hil-runner-tinyuf2.service'
        other.mkdir()
        (other / 'cgroup.procs').write_text('900\n')
        self.procs = {**CHAIN, 900: 1}
        self.sync(cgroup=sorted(CHAIN))
        self.assertEqual(self.run_hook('start'), (0, ''))
        self.assertEqual(hook.survivors('hil-runner-tinyuf2'), [900])

    def test_bad_usage(self):
        self.assertEqual(hook.main(['stop', UNIT]), 2)
        self.assertEqual(hook.main(['start']), 2)


def setUpModule():
    guard_survivors(__name__)


if __name__ == '__main__':
    unittest.main()
