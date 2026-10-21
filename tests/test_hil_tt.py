"""skills/hil/scripts/hil_tt.py is the Reset_TT helper the HIL harnesses share, moved from
tinyusb's test/hil/helper/hil_tt.py with its TtPort/Request/Helper tests: which TT a DUT sits
behind (a fake sysfs tree), the request bytes, the helper's outcomes through a real child,
and the child's launch closure (this file alone). Whether the hub frees the buffer is proven
on the rig."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_tt needs fcntl and usbfs')

import importlib.util
import io
import shutil
import struct
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


class TtPort(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.sys = Path(td.name)
        patcher = mock.patch.object(tt, 'SYS_USB', self.sys)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hub(self, name, protocol='02', busnum=5, devnum=58, config=1):
        intf = self.sys / name / f'{name}:{config}.0'
        intf.mkdir(parents=True)
        (intf / 'bInterfaceProtocol').write_text(protocol + '\n')
        (self.sys / name / 'busnum').write_text(f'{busnum}\n')
        (self.sys / name / 'devnum').write_text(f'{devnum}\n')

    def test_full_speed_device_behind_a_multi_tt_hub(self):
        self.hub('5-1')
        self.assertEqual(tt.tt_port('5-1.2', '12'), ('/dev/bus/usb/005/058', 2))

    def test_nested_hub_and_any_configuration_value(self):
        self.hub('3-1.4', busnum=3, devnum=7, config=2)
        self.assertEqual(tt.tt_port('3-1.4.7', '1.5'), ('/dev/bus/usb/003/007', 7))

    def test_no_reset_where_it_cannot_help_or_would_disturb_others(self):
        self.hub('5-1')
        self.hub('6-1', protocol='01')    # single TT: shared by every port
        self.hub('7-1', protocol='00')    # hub running at full speed: no TT
        for busport, speed in (('5-1.2', '480'),   # high speed: no split
                               ('5-1', '12'),      # root port
                               ('6-1.1', '12'), ('7-1.1', '12'),
                               ('8-1.1', '12')):   # hub gone
            self.assertIsNone(tt.tt_port(busport, speed), busport)


class Request(unittest.TestCase):
    def test_reset_tt_request_bytes(self):
        sent = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tt.fcntl, 'ioctl', lambda f, req, arg: sent.append((req, arg))):
            node = os.path.join(td, 'node')
            open(node, 'w').close()
            tt.reset_tt_ioctl(node, 3)
        (req, arg), = sent
        # bmRequestType, bRequest, wValue, wIndex, wLength, timeout ms, data pointer
        self.assertEqual(struct.unpack('@BBHHHIP', arg), (0x23, 9, 0, 3, 0, 1000, 0))
        if struct.calcsize('P') == 8:   # the ioctl the rig's LP64 kernel answers
            self.assertEqual((req, len(arg)), (0xC0185500, 24))


class Helper(unittest.TestCase):
    """reset_tt through its runner: a timeout is unconfirmed and stops later helpers for that
    hub; any other failure only reports."""

    def setUp(self):
        for name, value in (('tt_port', lambda b, s: ('/dev/bus/usb/005/058', 2)), ('_unconfirmed', set())):
            patcher = mock.patch.object(tt, name, value)
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

    def test_the_child_argv_is_this_file_isolated_under_sudo_unless_root(self):
        here = os.path.join(SCRIPTS, 'hil_tt.py')
        for euid, prefix in ((1000, ['sudo', '-n']), (0, [])):
            with mock.patch.object(tt.os, 'geteuid', lambda: euid):
                _, calls, _ = self.reset(0)
            self.assertEqual(calls[0][0], prefix + [sys.executable, '-I', here, '/dev/bus/usb/005/058', '2'])

    def test_a_real_timeout_keeps_its_banner_off_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tt, 'HELPER_TIMEOUT', 1), redirect_stdout(out), redirect_stderr(err):
            ok = tt.reset_tt('5-1.2', '12', run=lambda cmd, **kw: hp.run_cmd(['sleep', '30'], reap_grace=1, **kw))
        self.assertEqual((ok, out.getvalue()), (False, ''))
        self.assertIn('COMMAND TIMEOUT', err.getvalue())
        self.assertIn('helper timed out', err.getvalue())


class ChildClosure(unittest.TestCase):
    """The child is launched from the pinned file by its own path and needs nothing beside it:
    a copy of hil_tt.py alone in an empty dir reaches the ioctl. A regular file as the node
    answers that ioctl with ENOTTY, so no USB device is touched."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        self.node = self.dir / 'node'
        self.node.write_text('')
        patcher = mock.patch.object(os, 'geteuid', lambda: 0)   # no sudo prefix in a test
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset(self, mod, run=None):
        err = io.StringIO()
        with mock.patch.object(mod, 'tt_port', lambda b, s: (str(self.node), 2)), \
                mock.patch.object(mod, '_unconfirmed', set()), redirect_stderr(err):
            ok = mod.reset_tt('5-1.2', '12', run=run)
        return ok, err.getvalue()

    def test_a_lone_copy_reaches_the_ioctl(self):
        alone = self.dir / 'alone'
        alone.mkdir()
        shutil.copy(SCRIPTS / 'hil_tt.py', alone / 'hil_tt.py')
        copy = load('hil_tt', alone)
        ran = []

        def run(cmd, **kw):
            ran.append(cmd)
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  timeout=kw['timeout'])
        # the parent reads the failure through the hil_process a deployment keeps beside it
        with mock.patch.object(copy, '_hil_process', hp):
            ok, err = self.reset(copy, run)
        self.assertEqual(ran[0][2], str(alone / 'hil_tt.py'))
        self.assertTrue(ok)
        self.assertIn('Inappropriate ioctl', err)
        self.assertNotIn('Error while finding', err)
        self.assertNotIn('ModuleNotFoundError', err)
        self.assertEqual([p.name for p in alone.glob('*.py')], ['hil_tt.py'])

    def test_the_default_runner_launches_the_real_child(self):
        ok, err = self.reset(tt)
        self.assertTrue(ok)
        self.assertIn(f'Reset_TT {self.node} port 2 failed:', err)
        self.assertIn('Inappropriate ioctl', err)


if __name__ == '__main__':
    unittest.main()
