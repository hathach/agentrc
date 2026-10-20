"""root_port_cycle.py against a fake sysfs tree, with the module's os and fcntl replaced by
recorders: every open and every usbfs request is logged, the root hub's node is a fake
character device, and nothing reaches a real one. Covers the selection of exactly one node, the
refusals before any open or request, the identity token across runs, and that on needs no
descriptor. The kernel's side is the skill's hardware trial, not this file's."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('root_port_cycle drives Linux usbfs')

import ctypes
import errno
import importlib.util
import io
import shutil
import stat
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'usb-kernel-recover' / 'scripts' / 'root_port_cycle.py'
spec = importlib.util.spec_from_file_location('root_port_cycle_under_test', SCRIPT)
rpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpc)

FD = 1000
RDEV = os.makedev(189, 1536)
NODE = '/fake-dev/bus/usb/013/001'
USB2_DESC = bytes([9, 0x29, 4, 0x09, 0x00, 10, 0, 0x00, 0xff])           # per-port LPSM, 4 ports
SS_DESC = bytes([12, 0x2A, 4, 0x09, 0x00, 10, 0, 0, 0, 0, 0, 0])


class FakeOS:
    """The real os, except that the root hub's node and its fd are fakes and every open is logged."""

    def __init__(self, dev):
        self.dev = dev

    def __getattr__(self, name):
        return getattr(os, name)

    def lstat(self, path):
        if path == NODE and self.dev.node_mode is not None:
            return types.SimpleNamespace(st_mode=self.dev.node_mode, st_rdev=self.dev.node_rdev)
        return os.lstat(path)

    def open(self, path, flags, *a):
        self.dev.opens.append((path, flags))
        if path != NODE:
            return os.open(path, flags, *a)
        if self.dev.on_open:
            self.dev.on_open()
        return FD

    def fstat(self, fd):
        return types.SimpleNamespace(st_mode=stat.S_IFCHR | 0o664, st_rdev=self.dev.fd_rdev) if fd == FD else os.fstat(fd)

    def close(self, fd):
        if fd == FD:
            self.dev.closed += 1
        else:
            os.close(fd)


class FakeDevice:
    def __init__(self):
        self.opens, self.requests, self.closed = [], [], 0
        self.node_mode, self.node_rdev, self.fd_rdev = stat.S_IFCHR | 0o664, RDEV, RDEV
        self.desc, self.fail, self.on_open = USB2_DESC, {}, None
        self.pad = True       # a root hub returns the whole requested buffer (hcd.c:647-648), not bDescLength

    def ioctl(self, fd, request, ct, mutate):
        assert fd == FD and mutate
        self.requests.append((request, ct.bRequestType, ct.bRequest, ct.wValue, ct.wIndex, ct.wLength, ct.timeout))
        if ct.bRequest in self.fail:
            raise OSError(self.fail[ct.bRequest], os.strerror(self.fail[ct.bRequest]))
        if ct.bRequest == rpc.GET_DESCRIPTOR:
            data = self.desc.ljust(ct.wLength, b'\0') if self.pad else self.desc
            n = min(len(data), ct.wLength)
            ctypes.memmove(ct.data, data, n)
            return n
        return 0


class Fixture(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.devices = self.root / 'sys/bus/usb/devices'
        self.devices.mkdir(parents=True)
        self.gen = 0
        self.root_hub(self.make_root())
        self.dev = FakeDevice()
        for target, value in ((rpc, 'SYSFS'), (rpc, 'DEV')):
            self.addCleanup(setattr, target, value, getattr(target, value))
        rpc.SYSFS, rpc.DEV = str(self.root / 'sys'), '/fake-dev'
        for patch in (mock.patch.object(rpc, 'os', FakeOS(self.dev)),
                      mock.patch.object(rpc, 'fcntl', types.SimpleNamespace(ioctl=self.dev.ioctl)),
                      mock.patch.object(rpc.platform, 'machine', return_value='x86_64'),
                      mock.patch.object(rpc.platform, 'system', return_value='Linux')):
            patch.start()
            self.addCleanup(patch.stop)

    def make_root(self, **attrs):
        """A root hub directory, a new kobject each time (so a new inode)."""
        self.gen += 1
        d = self.root / f'usb13.gen{self.gen}'
        d.mkdir()
        values = {'devpath': '0', 'bDeviceClass': '09', 'busnum': '13', 'devnum': '1', 'maxchild': '4',
                  'dev': '189:1536', 'bDeviceProtocol': '01', **attrs}
        for k, v in values.items():
            if v is not None:
                (d / k).write_text(v + '\n')
        for p in range(1, 5):
            (d / '13-0:1.0' / f'usb13-port{p}').mkdir(parents=True)
        return d

    def root_hub(self, d):
        link = self.devices / 'usb13'
        if link.is_symlink():
            link.unlink()
        link.symlink_to(d)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = rpc.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def check(self):
        rc, out, err = self.run_main('check', '13', '1')
        self.assertEqual(rc, 0, err)
        return out.strip()

    def assert_untouched(self):
        self.assertEqual((self.dev.opens, self.dev.requests), ([], []))


class Selection(Fixture):
    def test_check_off_on_open_only_the_root_hub_node_and_send_one_request_each(self):
        token = self.check()
        st = os.stat(self.devices / 'usb13/')
        self.assertEqual(token, f'189:1536:{st.st_ino}')
        self.assertEqual(self.run_main('off', '13', '1', token)[0], 0)
        rc, out, _ = self.run_main('on', '13', '1', token)
        self.assertEqual(rc, 0)
        self.assertIn('power-on request to usb13 port 1 done', out)
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NOCTTY
        self.assertEqual(self.dev.opens, [(NODE, flags)] * 3)
        self.assertEqual(self.dev.closed, 3)
        control = 0xC0185500 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0xC0105500
        self.assertEqual(self.dev.requests, [
            (control, 0xA0, 6, 0x2900, 0, 15, 5000),       # GetHubDescriptor
            (control, 0x23, 1, 8, 1, 0, 5000),             # ClearPortFeature(PORT_POWER), port 1
            (control, 0x23, 3, 8, 1, 0, 5000),             # SetPortFeature(PORT_POWER), port 1
        ])

    def test_a_superspeed_root_hub_is_asked_for_its_own_descriptor_type(self):
        self.root_hub(self.make_root(bDeviceProtocol='03'))
        self.dev.desc = SS_DESC
        self.check()
        self.assertEqual(self.dev.requests[0][1:6], (0xA0, 6, 0x2A00, 0, 12))

    def test_on_sends_no_descriptor_request_so_a_descriptor_failure_after_off_cannot_block_it(self):
        token = self.check()
        self.assertEqual(self.run_main('off', '13', '1', token)[0], 0)
        self.dev.fail[rpc.GET_DESCRIPTOR] = errno.EPIPE
        self.dev.desc = b''
        self.assertEqual(self.run_main('on', '13', '1', token)[0], 0)
        self.assertEqual([r[2] for r in self.dev.requests], [6, 1, 3])

    def test_both_usb2_descriptor_lengths_are_accepted_and_bdesclength_bounds_the_data(self):
        """8 ports: DeviceRemovable 2 bytes; PortPwrCtrlMask 1 (USB 2.0 Table 11-13) or 2 (Linux)."""
        self.root_hub(self.make_root(maxchild='8'))
        for length, pad in ((10, False), (11, False), (11, True)):
            with self.subTest(length=length, pad=pad):
                self.dev.pad = pad
                self.dev.desc = bytes([length, 0x29, 8, 0x01, 0, 10, 0]) + b'\xff' * (length - 7)
                self.check()

    def test_a_failed_off_or_on_request_is_exit_1_not_a_refusal(self):
        token = self.check()
        self.dev.fail = {rpc.CLEAR_FEATURE: errno.ETIMEDOUT, rpc.SET_FEATURE: errno.EPIPE}
        rc, _, err = self.run_main('off', '13', '1', token)
        self.assertEqual(rc, 1)
        self.assertIn('power-off request to usb13 port 1 failed', err)
        self.assertEqual(self.run_main('on', '13', '1', token)[0], 1)
        self.assertEqual(self.dev.closed, 3)


class Refusals(Fixture):
    def assert_refused(self, says, *argv):
        rc, out, err = self.run_main(*(argv or ('check', '13', '1')))
        self.assertEqual(rc, 3, err)
        self.assertIn(says, err)
        self.assertIn('no switching request sent', err)
        self.assertEqual(out, '')
        return err

    def test_anything_unexpected_in_sysfs_refuses_before_any_open(self):
        cases = [({'devpath': '1'}, 'is not the root hub'), ({'bDeviceClass': '00'}, 'is not the root hub'),
                 ({'busnum': '14'}, 'is not the root hub'), ({'maxchild': '0'}, 'has no port 1'),
                 ({'dev': None}, 'cannot read'), ({'dev': 'x'}, 'has no device number'),
                 ({'devnum': 'one'}, 'not a number')]
        for attrs, says in cases:
            with self.subTest(attrs=attrs):
                self.root_hub(self.make_root(**attrs))
                self.assert_refused(says)
                self.assert_untouched()
        self.root_hub(self.make_root())
        shutil.rmtree(self.devices / 'usb13' / '13-0:1.0' / 'usb13-port1')
        self.assert_refused('no port directory usb13-port1')
        self.assert_refused('no root hub usb14', 'check', '14', '1')
        self.assert_untouched()

    def test_a_node_that_is_not_the_root_hubs_character_device_is_never_opened(self):
        for mode, rdev in ((stat.S_IFREG | 0o644, RDEV), (stat.S_IFCHR | 0o664, os.makedev(189, 1537)),
                           (None, RDEV)):
            with self.subTest(mode=mode, rdev=rdev):
                self.dev.node_mode, self.dev.node_rdev = mode, rdev
                self.assert_refused(NODE)
                self.assert_untouched()

    def test_a_node_replaced_during_the_open_is_closed_without_a_request(self):
        self.dev.fd_rdev = os.makedev(189, 1537)
        self.assert_refused('changed while it was opened')
        self.assertEqual((len(self.dev.opens), self.dev.closed, self.dev.requests), (1, 1, []))

    def test_power_switching_that_is_not_per_port_or_a_bad_descriptor_refuses(self):
        cases = [(bytes([9, 0x29, 4, 0x00, 0, 10, 0, 0, 0xff]), 'declares ganged power switching'),
                 (bytes([9, 0x29, 4, 0x02, 0, 10, 0, 0, 0xff]), 'declares no power switching'),
                 (bytes([9, 0x2A, 4, 0x01, 0, 10, 0, 0, 0xff]), 'malformed hub descriptor'),
                 (bytes([9, 0x29, 0, 0x01, 0, 10, 0, 0, 0xff]), 'malformed hub descriptor'),
                 (bytes([0, 0x29, 4, 0x01, 0, 10, 0, 0, 0xff]), 'malformed hub descriptor'),       # bDescLength 0
                 (bytes([15, 0x29, 4, 0x01, 0, 10, 0, 0, 0xff]), 'malformed hub descriptor'),      # 4 ports: 9
                 (bytes([11, 0x2A, 4, 0x01, 0, 10, 0, 0, 0, 0, 0]), 'malformed hub descriptor')]  # SS: 12
        for desc, says in cases:
            with self.subTest(desc=desc.hex()):
                self.dev.desc = desc
                self.assert_refused(says)
        self.dev.pad = False
        for desc in (bytes([9, 0x29, 4, 0x01, 0]),                       # short
                     bytes([11, 0x29, 8, 0x01, 0, 10, 0, 0, 0xff])):     # declares 11 for 8 ports, returns 9
            with self.subTest(desc=desc.hex()):
                self.dev.desc = desc
                self.assert_refused('malformed hub descriptor')
        self.dev.fail[rpc.GET_DESCRIPTOR] = errno.EPIPE
        self.assert_refused('hub descriptor request failed')
        self.assertEqual({r[2] for r in self.dev.requests}, {rpc.GET_DESCRIPTOR})

    def test_a_root_hub_replaced_between_runs_or_during_the_open_refuses_without_a_request(self):
        token = self.check()
        self.root_hub(self.make_root())                      # same bus, same dev_t, a new kobject
        self.dev.opens.clear()
        self.assert_refused('is not the root hub check saw', 'off', '13', '1', token)
        self.assert_refused('is not the root hub check saw', 'on', '13', '1', token)
        self.assertEqual(self.dev.opens, [])
        token = self.check()
        self.dev.on_open = lambda: self.root_hub(self.make_root())
        self.assert_refused('was replaced while', 'off', '13', '1', token)
        self.assertEqual([r[2] for r in self.dev.requests], [6, 6])

    def test_an_unsupported_ioctl_abi_refuses_before_anything_is_read_or_opened(self):
        with mock.patch.object(rpc.platform, 'machine', return_value='ppc64le'):
            self.assert_refused('unsupported ioctl ABI')
        self.assert_untouched()

    def test_usage_errors_exit_2(self):
        for argv in ((), ('check', '13'), ('off', '13', '1'), ('check', '13', '0'), ('cycle', '13', '1'),
                     ('check', '13', '1', 'extra'), ('on', '../13', '1', 'x')):
            with self.subTest(argv=argv):
                rc, _, err = self.run_main(*argv)
                self.assertEqual(rc, 2)
                self.assertIn('root_port_cycle.py check <bus> <port>', err)
        self.assert_untouched()


if __name__ == '__main__':
    unittest.main()
