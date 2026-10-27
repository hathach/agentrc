"""skills/hil/scripts/hil_root.py is the one root entry the HIL scripts use: which copy and sudo
argv() picks, and each verb's refusals and its one write, ioctl or program, against a temp sysfs
tree with the device node and the programs stubbed. That a rig's sudo grant covers only the
installed copy is the rig's own check."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_root needs Linux sysfs and usbfs')

import importlib.util
import io
import stat
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
HERE_ROOT = str(SCRIPTS / 'hil_root.py')


def load():
    spec = importlib.util.spec_from_file_location('agentrc_hil_root', HERE_ROOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = load()


def setUpModule():
    guard_survivors(__name__)


class Argv(unittest.TestCase):
    def argv(self, euid, installed):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'hil_root'
            if installed == 'dangling':
                path.symlink_to(Path(td) / 'gone')
            elif installed:
                path.write_text('')
            with mock.patch.object(hr.os, 'geteuid', lambda: euid), mock.patch.object(hr, 'INSTALLED', str(path)):
                cmd = hr.argv('dmesg')
            return [hr.INSTALLED if c == str(path) else c for c in cmd]

    def test_root_runs_this_file(self):
        self.assertEqual(self.argv(0, True), [sys.executable, '-I', HERE_ROOT, 'dmesg'])

    def test_an_installed_copy_is_the_only_path_under_sudo(self):
        self.assertEqual(self.argv(1000, True), ['sudo', '-n', hr.INSTALLED, 'dmesg'])

    def test_a_broken_installed_entry_is_still_the_only_path(self):
        self.assertEqual(self.argv(1000, 'dangling'), ['sudo', '-n', hr.INSTALLED, 'dmesg'])

    def test_an_unreadable_installed_entry_is_still_the_only_path(self):
        with mock.patch.object(hr.os, 'geteuid', lambda: 1000), \
                mock.patch.object(hr.os, 'lstat', mock.Mock(side_effect=PermissionError(13, 'denied'))):
            self.assertEqual(hr.argv('dmesg'), ['sudo', '-n', hr.INSTALLED, 'dmesg'])

    def test_without_one_this_file_under_sudo(self):
        self.assertEqual(self.argv(1000, False), ['sudo', '-n', sys.executable, '-I', HERE_ROOT, 'dmesg'])


class Sysfs(unittest.TestCase):
    """A temp tree standing in for /sys: usbtest's pattern param, PCI and USB devices."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.usb = self.root / 'devices'
        self.param = self.root / 'pattern'
        self.param.write_text('0')
        for name, value in (('SYS_USB', self.usb), ('PATTERN_PARAM', self.param)):
            p = mock.patch.object(hr, name, value)
            p.start()
            self.addCleanup(p.stop)

    def dev(self, busport, vid='cafe', pid='4010', **attrs):
        d = self.usb / busport
        d.mkdir(parents=True)
        for k, v in {'idVendor': vid, 'idProduct': pid, **attrs}.items():
            (d / k).write_text(f'{v}\n')
        return d

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = hr.main(list(args))
        return rc, out.getvalue(), err.getvalue()


class Usage(Sysfs):
    def test_unknown_verb_and_wrong_argc_refuse(self):
        # new-id, bind and unbind went with usbtest's new_id (kernel 6.12.108), renesas-fw to the rig
        # owner's manual check: refused like any unknown verb
        for args in ((), ('chmod', '/etc/shadow'), ('dmesg', 'x'), ('new-id', 'cafe', '4010', '0525', 'a4a0'),
                     ('bind', '1-2:1.0'), ('unbind', '1-2:1.0'), ('renesas-fw', '0000:03:00.0')):
            with self.subTest(args=args):
                self.assertEqual(self.main(*args)[0], 2)


class Writes(Sysfs):
    def test_pattern_is_zero_or_one(self):
        self.assertEqual(self.main('pattern', '1')[0], 0)
        self.assertEqual(self.param.read_text(), '1')
        for v in ('2', '-1', '01', '1\n'):
            self.assertEqual(self.main('pattern', v)[0], 2)

    def test_a_failed_write_is_rc1_with_its_error(self):
        self.param.unlink()
        self.param.mkdir()             # an open for writing fails even as root
        rc, _, err = self.main('pattern', '1')
        self.assertEqual(rc, 1)
        self.assertIn('pattern:', err)


class Programs(Sysfs):
    def run_with(self, stdout='', rc=0):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, rc, stdout, '')
        p = mock.patch.object(hr.subprocess, 'run', run)
        p.start()
        self.addCleanup(p.stop)
        return seen

    def test_dmesg_keeps_only_usbtest_lines(self):
        seen = self.run_with('[1] usbtest 1-2:1.0: x\n[2] secret\n[3] usbtest y\n')
        self.assertEqual(self.main('dmesg'), (0, '[1] usbtest 1-2:1.0: x\n[3] usbtest y\n', ''))
        self.assertEqual(seen, [[hr.DMESG]])

    def test_modprobe_is_usbtest_only(self):
        seen = self.run_with()
        self.assertEqual(self.main('modprobe')[0], 0)
        self.assertEqual(seen, [[hr.MODPROBE, 'usbtest']])


class ResetTt(Sysfs):
    """reset-tt re-derives the hub, checks it, then checks the node it opened is that hub."""

    def setUp(self):
        super().setUp()
        self.dev('5-1', vid='05e3', pid='0610', busnum=5, devnum=58, maxchild=4)
        intf = self.usb / '5-1' / '5-1:1.0'
        intf.mkdir()
        (intf / 'bInterfaceProtocol').write_text('02\n')
        self.dev('5-1.2', speed=12)
        self.sent = []
        self.node = {'mode': stat.S_IFCHR | 0o664, 'rdev': os.makedev(189, 4 * 128 + 57),
                     'desc': bytes([18, 1, 0, 2, 9, 0, 1, 64]) + struct.pack('<HH', 0x05e3, 0x0610) + bytes(6)}
        opened = []

        def fake_open(path, flags):
            opened.append(path)
            return 99
        for name, value in (('open', fake_open), ('close', lambda fd: None),
                            ('fstat', lambda fd: mock.Mock(st_mode=self.node['mode'], st_rdev=self.node['rdev'])),
                            ('read', lambda fd, n: self.node['desc'][:n])):
            p = mock.patch.object(hr.os, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(hr.fcntl, 'ioctl', lambda fd, req, arg: self.sent.append((fd, req, arg)))
        p.start()
        self.addCleanup(p.stop)
        self.opened = opened

    def test_the_request_goes_to_the_derived_hub_port(self):
        self.assertEqual(self.main('reset-tt', '5-1.2')[0], 0)
        self.assertEqual(self.opened, ['/dev/bus/usb/005/058'])
        (fd, req, arg), = self.sent
        # bmRequestType, bRequest, wValue, wIndex, wLength, timeout ms, data pointer
        self.assertEqual((fd, struct.unpack('@BBHHHIP', arg)), (99, (0x23, 9, 0, 2, 0, 1000, 0)))
        if struct.calcsize('P') == 8:   # the ioctl the rig's LP64 kernel answers
            self.assertEqual((req, len(arg)), (0xC0185500, 24))

    def refused(self, *args, opened=False):
        rc, _, err = self.main('reset-tt', *args)
        self.assertEqual((rc, self.sent), (2, []), err)
        self.assertEqual(bool(self.opened), opened)
        return err

    def test_a_gone_device_is_reset_by_its_hub_port(self):
        for f in (self.usb / '5-1.2').iterdir():     # rmtree would meet the patched os.open
            f.unlink()
        (self.usb / '5-1.2').rmdir()
        self.assertEqual(self.main('reset-tt', '5-1.2')[0], 0)
        self.assertEqual(len(self.sent), 1)

    def test_root_port_and_malformed_busports_refuse_before_opening(self):
        for busport in ('5-1', '5-1.0', '../5-1.2', '5-1.2/x', '5-1.2 '):
            with self.subTest(busport=busport):
                self.refused(busport)

    def test_a_single_tt_hub_or_a_missing_port_refuses(self):
        (self.usb / '5-1' / '5-1:1.0' / 'bInterfaceProtocol').write_text('01\n')
        self.assertIn('one TT per port', self.refused('5-1.2'))
        (self.usb / '5-1' / '5-1:1.0' / 'bInterfaceProtocol').write_text('02\n')
        self.dev('5-1.5', speed=12)
        self.assertIn('no port 5', self.refused('5-1.5'))

    def test_a_node_that_is_not_the_hub_refuses_after_opening(self):
        for change in ({'rdev': os.makedev(189, 4 * 128 + 58)}, {'rdev': os.makedev(1, 3)},
                       {'mode': stat.S_IFREG | 0o644},
                       {'desc': bytes([18, 1, 0, 2, 0, 0, 1, 64]) + struct.pack('<HH', 0x05e3, 0x0610) + bytes(6)},
                       {'desc': bytes([18, 1, 0, 2, 9, 0, 1, 64]) + struct.pack('<HH', 0x1234, 0x0610) + bytes(6)}):
            with self.subTest(change=change):
                saved = dict(self.node)
                self.node.update(change)
                self.opened.clear()
                self.refused('5-1.2', opened=True)
                self.node.update(saved)


class Standalone(unittest.TestCase):
    def test_a_lone_copy_runs_and_refuses(self):
        """The installed copy has nothing beside it."""
        with tempfile.TemporaryDirectory() as td:
            lone = Path(td) / 'hil_root'
            lone.write_bytes(Path(HERE_ROOT).read_bytes())
            r = subprocess.run([sys.executable, '-I', str(lone), 'reset-tt', '999-1.1'],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn('hil_root: refused: hub 999-1 ', r.stderr)
        self.assertNotIn('Error', r.stderr)


if __name__ == '__main__':
    unittest.main()
