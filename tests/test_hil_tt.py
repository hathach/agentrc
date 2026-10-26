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
        patcher = mock.patch.object(tt, '_unconfirmed', set())
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
    hub; any other failure only reports."""

    def setUp(self):
        for obj, name, value in ((tt.hil_root, 'tt_hub', lambda b: ('5-1', 2, 5, 58, (0, 0))), (tt, '_unconfirmed', set())):
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

    def test_timeout_is_unconfirmed_and_not_repeated_for_that_hub(self):
        oks, calls, err = self.reset(124, 0)
        self.assertEqual((oks, len(calls), 'timed out' in err), ([False, False], 1, True))

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

    def test_a_timeout_is_remembered_by_hub_busport(self):
        self.reset(124)
        self.assertEqual(tt._unconfirmed, {'5-1'})

    def test_a_timed_out_hub_stays_unconfirmed_when_it_no_longer_resolves(self):
        self.reset(124)
        with mock.patch.object(tt.hil_root, 'tt_hub', mock.Mock(side_effect=tt.hil_root.Refused('gone'))), \
                redirect_stderr(io.StringIO()):
            self.assertFalse(tt.reset_tt('5-1.2', '12', run=lambda *a, **k: self.fail('no helper')))

    def test_a_real_timeout_keeps_its_banner_off_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tt, 'HELPER_TIMEOUT', 1), redirect_stdout(out), redirect_stderr(err):
            ok = tt.reset_tt('5-1.2', '12', run=lambda cmd, **kw: hp.run_cmd(['sleep', '30'], reap_grace=1, **kw))
        self.assertEqual((ok, out.getvalue()), (False, ''))
        self.assertIn('COMMAND TIMEOUT', err.getvalue())
        self.assertIn('helper timed out', err.getvalue())


class RealHelper(unittest.TestCase):
    """The default runner launches the real hil_root.py, which re-derives the hub from sysfs
    itself: a busport no host has is refused there, before any device is opened."""

    def test_the_default_runner_launches_hil_root(self):
        err = io.StringIO()
        with mock.patch.object(tt.hil_root, 'tt_hub', lambda b: ('999-1', 1, 999, 2, (0, 0))), \
                mock.patch.object(tt, '_unconfirmed', set()), \
                mock.patch.object(os, 'geteuid', lambda: 0), redirect_stderr(err):   # no sudo in a test
            ok = tt.reset_tt('999-1.1', '12')
        self.assertTrue(ok)
        self.assertIn('Reset_TT /dev/bus/usb/999/002 port 1 failed: hil_root: refused: hub 999-1 ', err.getvalue())


if __name__ == '__main__':
    unittest.main()
