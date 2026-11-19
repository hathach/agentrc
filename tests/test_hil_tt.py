"""skills/hil/scripts/hil_tt.py is the Reset_TT helper the HIL harnesses share, moved from
tinyusb's test/hil/helper/hil_tt.py with its TtPort/Helper tests: which TT a DUT sits behind
(a fake sysfs tree) and the helper's outcomes through a real child. The request itself is
hil_root.py's (test_hil_root.py). Whether the hub frees the buffer is proven on the rig."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_tt needs fcntl and usbfs')

import importlib.util
import io
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts'


def load(name, where=SCRIPTS):
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', Path(where) / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tt = load('hil_tt')
hp = load('hil_process')


def setUpModule():
    guard_survivors(__name__)


class WhenToReset(unittest.TestCase):
    """Whether a reset is requested is hil_root.tt_hub's rule, from a fake sysfs tree: only a
    full-/low-speed device behind a hub running one TT per port gets one."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.sys = Path(td.name)
        for name, value in (('SYS_USB', self.sys),):
            patcher = mock.patch.object(tt.hil_root, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(tt, '_unconfirmed', {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def hub(self, name, protocol='02', busnum=5, devnum=58, config=1, maxchild=7):
        intf = self.sys / name / f'{name}:{config}.0'
        intf.mkdir(parents=True)
        (intf / 'bInterfaceProtocol').write_text(protocol + '\n')
        for k, v in (('busnum', busnum), ('devnum', devnum), ('maxchild', maxchild), ('idVendor', '05e3'),
                     ('idProduct', '0610')):
            (self.sys / name / k).write_text(f'{v}\n')

    def dev(self, busport, speed='12'):
        (self.sys / busport).mkdir(parents=True, exist_ok=True)
        (self.sys / busport / 'speed').write_text(speed + '\n')

    def requested(self, busport, speed):
        sent = []
        with redirect_stderr(io.StringIO()):
            tt.reset_tt(busport, speed, run=lambda cmd, **kw: sent.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', None))
        return bool(sent)

    def test_full_speed_device_behind_a_multi_tt_hub(self):
        self.hub('5-1')
        self.dev('5-1.2')
        self.assertEqual(tt.hil_root.tt_hub('5-1.2'), ('5-1', 2, 5, 58, (0x05e3, 0x0610)))
        self.assertTrue(self.requested('5-1.2', '12'))

    def test_nested_hub_and_any_configuration_value(self):
        self.hub('3-1.4', busnum=3, devnum=7, config=2)
        self.dev('3-1.4.7', '1.5')
        self.assertEqual(tt.hil_root.tt_hub('3-1.4.7')[:4], ('3-1.4', 7, 3, 7))

    def test_no_reset_where_it_cannot_help_or_would_disturb_others(self):
        self.hub('5-1')
        self.hub('6-1', protocol='01')    # single TT: shared by every port
        self.hub('7-1', protocol='00')    # hub running at full speed: no TT
        for busport, speed in (('5-1.2', '480'),   # high speed: no split
                               ('5-1', '12'),      # root port
                               ('6-1.1', '12'), ('7-1.1', '12'),
                               ('8-1.1', '12')):   # hub gone
            self.dev(busport, speed)
            self.assertFalse(self.requested(busport, speed), busport)


class Helper(unittest.TestCase):
    """reset_tt through its runner: a timeout is unconfirmed and stops later helpers for that
    hub while what it left behind still runs; any other failure only reports."""

    def setUp(self):
        for obj, name, value in ((tt.hil_root, 'tt_hub', lambda b: ('5-1', 2, 5, 58, (0, 0))), (tt, '_unconfirmed', {})):
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def reset(self, *outcomes):
        seq = iter(outcomes)
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            o = next(seq)
            if isinstance(o, BaseException):
                raise o
            return subprocess.CompletedProcess(cmd, o, 'why', None)

        err = io.StringIO()
        with redirect_stderr(err):
            oks = [tt.reset_tt('5-1.2', '12', run=run) for _ in outcomes]
        return oks, calls, err.getvalue()

    def test_a_timeout_that_left_no_helper_running_costs_only_that_call(self):
        with mock.patch.object(tt, '_survivors', lambda tail: set()):
            oks, calls, _ = self.reset(124, 0)
        self.assertEqual((oks, len(calls), tt._unconfirmed), ([False, True], 2, {}))

    def test_a_hub_whose_helper_proc_cannot_show_stays_unconfirmed(self):
        with mock.patch.object(tt, '_survivors', lambda tail: None):
            oks, calls, _ = self.reset(124, 0, 0)
        self.assertEqual((oks, len(calls)), ([False] * 3, 1))

    def test_an_inconclusive_scan_is_retried_until_it_proves_none_left(self):
        scans = iter([None, None, {(1, 2)}, set()])
        tails = []
        with mock.patch.object(tt, '_survivors', lambda tail: tails.append(tail) or next(scans)), \
                mock.patch.object(tt.hil_root, 'argv', lambda *a: ['H', *a]):
            oks, calls, _ = self.reset(124, 0)                      # the second call's scan: inconclusive again
            self.assertEqual(tt._unconfirmed, {'5-1': ('H', 'reset-tt', '5-1.2')})
            seen = [tt._waiting('5-1'), dict(tt._unconfirmed)]      # the third finds a survivor
            tt._unconfirmed['5-1'] = ('H', 'reset-tt', '5-1.2')
            seen += [tt._waiting('5-1'), dict(tt._unconfirmed)]     # the fourth finds none
        self.assertEqual((oks, len(calls), seen), ([False, False], 1, [True, {'5-1': {(1, 2)}}, False, {}]))
        self.assertEqual(tails, [['H', 'reset-tt', '5-1.2']] + [('H', 'reset-tt', '5-1.2')] * 3)

    def test_other_failures_only_report(self):
        oks, calls, err = self.reset(1, FileNotFoundError('sudo'), 0)
        self.assertEqual((oks, len(calls), err.count('failed')), ([True] * 3, 3, 2))
        self.assertIn('failed: why', err)

    def test_runner_gets_the_bound_and_quiet(self):
        with mock.patch.object(tt, 'HELPER_TIMEOUT', 7):
            _, calls, _ = self.reset(0)
        self.assertEqual(calls[0][1], {'timeout': 7, 'quiet': True})

    def test_the_request_is_hil_roots_reset_tt_of_the_busport(self):
        with mock.patch.object(tt.hil_root, 'argv', lambda *a: ['H', *a]):
            _, calls, _ = self.reset(0)
        self.assertEqual(calls[0][0], ['H', 'reset-tt', '5-1.2'])

    def test_a_timeout_is_remembered_by_hub_busport_with_its_survivors(self):
        tails = []
        with mock.patch.object(tt.hil_root, 'argv', lambda *a: ['H', *a]), \
                mock.patch.object(tt, '_survivors', lambda tail: tails.append(tail) or {(1, 2)}):
            self.reset(124)
        self.assertEqual((tt._unconfirmed, tails), ({'5-1': {(1, 2)}}, [['H', 'reset-tt', '5-1.2']]))

    def test_a_timed_out_hub_stays_unconfirmed_when_it_no_longer_resolves(self):
        with mock.patch.object(tt, '_survivors', lambda tail: {(os.getpid(), tt._start(os.getpid()))}):
            self.reset(124)
        with mock.patch.object(tt.hil_root, 'tt_hub', mock.Mock(side_effect=tt.hil_root.Refused('gone'))), \
                redirect_stderr(io.StringIO()):
            self.assertFalse(tt.reset_tt('5-1.2', '12', run=lambda *a, **k: self.fail('no helper')))

    def test_a_proc_directory_fd_reads_only_its_own_process(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        fd = os.open(f'/proc/{child.pid}', os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.assertEqual(tt._start(child.pid, fd), tt._start(child.pid))
        child.communicate(b'')
        self.assertIsNone(tt._start(child.pid, fd))     # reaped: whatever runs under its pid now is not read
        with self.assertRaises((FileNotFoundError, ProcessLookupError)):
            open('cmdline', 'rb', opener=tt._at(fd)).close()

    def test_a_real_timeout_keeps_its_banner_off_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tt, 'HELPER_TIMEOUT', 1), redirect_stdout(out), redirect_stderr(err):
            ok = tt.reset_tt('5-1.2', '12', run=lambda cmd, **kw: hp.run_cmd(['sleep', '30'], reap_grace=1, **kw))
        self.assertEqual((ok, out.getvalue()), (False, ''))
        self.assertIn('COMMAND TIMEOUT', err.getvalue())
        self.assertIn('helper timed out', err.getvalue())


class Inconclusive(unittest.TestCase):
    """A /proc look that blocks past PROC_SCAN_BOUND, or a survivor it cannot read, keeps the hub held,
    and the call still returns."""

    def setUp(self):
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        for obj, name, value in ((tt, '_unconfirmed', {'5-1': {(123, 456)}}), (tt, '_scan', []), (tt, '_recheck', []),
                                 (tt._process(), 'PROC_SCAN_BOUND', 0.1)):
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def blocked(self, *_):
        self.release.wait(10)

    def test_a_blocked_recheck_keeps_the_hold_and_returns(self):
        entered, first = [], threading.Event()

        def blocked(*_):
            entered.append(1)
            first.set()
            self.blocked()
        t0 = time.monotonic()
        with mock.patch.object(tt, '_start', blocked):
            held = [tt._waiting('5-1')]
            self.assertTrue(first.wait(2))
            held.append(tt._waiting('5-1'))     # behind the first's blocked thread: no second one
        self.assertEqual((held, '5-1' in tt._unconfirmed, len(entered)), ([True, True], True, 1))
        self.assertLess(time.monotonic() - t0, 2)

    def test_an_unreadable_survivor_keeps_the_hold(self):
        with mock.patch.object(tt, '_start', mock.Mock(side_effect=PermissionError('hidepid'))):
            self.assertTrue(tt._waiting('5-1'))
        self.assertIn('5-1', tt._unconfirmed)

    def test_a_blocked_scan_is_no_proof_of_none(self):
        t0 = time.monotonic()
        with mock.patch.object(tt, '_listed', self.blocked):
            self.assertIsNone(tt._survivors(['H', 'reset-tt', '5-1.2']))
        self.assertLess(time.monotonic() - t0, 2)

    def test_no_thread_to_look_with_keeps_the_hold_until_one_proves_none(self):
        no_thread = mock.patch.object(threading.Thread, 'start', mock.Mock(side_effect=RuntimeError("can't start new thread")))
        tail = ('H', 'reset-tt', '5-1.2')
        with mock.patch.object(tt.hil_root, 'tt_hub', lambda b: ('5-1', 2, 5, 58, (0, 0))), \
                mock.patch.object(tt.hil_root, 'argv', lambda *a: ['H', *a]), \
                mock.patch.object(tt, '_listed', lambda hp, t: set()), redirect_stderr(io.StringIO()):
            with no_thread:
                held = [tt._waiting('5-1')]                     # a survivor's recheck
                self.assertEqual(tt._unconfirmed, {'5-1': {(123, 456)}})
                tt._unconfirmed.clear()
                held.append(tt.reset_tt('5-1.2', '12', run=lambda c, **k: subprocess.CompletedProcess(c, 124, '', None)))
                kept = dict(tt._unconfirmed)
            ok = tt.reset_tt('5-1.2', '12', run=lambda c, **k: subprocess.CompletedProcess(c, 0, '', None))
        self.assertEqual((held, kept, ok, tt._unconfirmed), ([True, False], {'5-1': tail}, True, {}))


# A helper the hub lock holds: its child leaves the runner's session, beyond the group kill as sudo's
# root child is, and waits until the lock is released (the file beside it); once it is, a helper succeeds.
# Its directory going is a release too: a cleanup removing it may beat the child to the file.
STALLED_HELPER = """import os, sys, time
released = __file__ + '.released'
if not os.path.exists(released):
    if os.fork() == 0:
        os.setsid()
        while not os.path.exists(released) and os.path.exists(os.path.dirname(released)):
            time.sleep(0.02)
        os._exit(0)
    time.sleep(60)
"""


class HubLockStall(unittest.TestCase):
    """A helper held past HELPER_TIMEOUT by the hub lock, through the real runner: later calls on that hub
    spawn no helper while it still waits, and once the lock is released and it exits, the next is
    attempted and confirmed."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.helper = Path(td.name) / 'helper.py'
        self.helper.write_text(STALLED_HELPER)
        self.released = Path(f'{self.helper}.released')
        self.addCleanup(self.released.touch)
        for obj, name, value in ((tt.hil_root, 'tt_hub', lambda b: ('5-1', 2, 5, 58, (0, 0))),
                                 (tt.hil_root, 'argv', lambda *a: [sys.executable, '-I', str(self.helper), *a]),
                                 (tt, '_unconfirmed', {}), (tt, 'HELPER_TIMEOUT', 1),
                                 (tt._process(), '_proc_hides_others', lambda: False)):   # our uid's: shown
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        if tt._survivors(('agentrc-no-such-tail',)) is None:     # Inconclusive covers that result
            self.skipTest("/proc withholds another uid's process (hidepid=1, no exemption): no scan proves none")
        self.spawned = []

    def run_helper(self, cmd, **kw):
        self.spawned.append(cmd)
        return hp.run_cmd(cmd, reap_grace=1, **kw)

    def reset(self, busport='5-1.2'):
        with redirect_stderr(io.StringIO()) as err:
            return tt.reset_tt(busport, '12', run=self.run_helper), err.getvalue()

    def test_a_stall_costs_the_call_it_hit_not_the_hub(self):
        ok, err = self.reset()
        self.assertEqual((ok, 'helper timed out' in err), (False, True))
        if isinstance(left := tt._unconfirmed['5-1'], tuple):
            self.released.touch()
            self.skipTest('the post-timeout /proc scan was inconclusive within PROC_SCAN_BOUND: no survivor to follow')
        [(pid, _)] = left
        self.assertEqual([self.reset()[0], self.reset('5-1.3')[0], len(self.spawned)], [False, False, 1])

        self.released.touch()
        for _ in range(250):
            if tt._start(pid) is None:
                break
            time.sleep(0.02)
        self.assertIsNone(tt._start(pid), 'the stalled helper did not exit once released')
        self.assertEqual([self.reset()[0], len(self.spawned), tt._unconfirmed], [True, 2, {}])


class RealHelper(unittest.TestCase):
    """The default runner launches the real hil_root.py, which re-derives the hub from sysfs
    itself: a busport no host has is refused there, before any device is opened."""

    def test_the_default_runner_launches_hil_root(self):
        err = io.StringIO()
        with mock.patch.object(tt.hil_root, 'tt_hub', lambda b: ('999-1', 1, 999, 2, (0, 0))), \
                mock.patch.object(tt, '_unconfirmed', {}), \
                mock.patch.object(os, 'geteuid', lambda: 0), redirect_stderr(err):   # no sudo in a test
            ok = tt.reset_tt('999-1.1', '12')
        self.assertTrue(ok)
        self.assertIn('Reset_TT /dev/bus/usb/999/002 port 1 failed: hil_root: refused: hub 999-1 ', err.getvalue())


if __name__ == '__main__':
    unittest.main()
