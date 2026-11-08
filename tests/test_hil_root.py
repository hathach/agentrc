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
        for args in ((), ('chmod', '/etc/shadow'), ('dmesg', 'x'), ('netns', '1-2'), ('netns', '1-2', '9', 'x'),
                     ('new-id', 'cafe', '4010', '0525', 'a4a0'),
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
            seen.append((cmd, kw.get('env')))
            return subprocess.CompletedProcess(cmd, rc, stdout, '')
        for p in (mock.patch.object(hr.subprocess, 'run', run),
                  mock.patch.dict(os.environ, {'MODPROBE_OPTIONS': '-C /tmp/evil.conf'})):
            p.start()
            self.addCleanup(p.stop)
        return seen

    def test_dmesg_keeps_only_usbtest_lines(self):
        seen = self.run_with('[1] usbtest 1-2:1.0: x\n[2] secret\n[3] usbtest y\n')
        self.assertEqual(self.main('dmesg'), (0, '[1] usbtest 1-2:1.0: x\n[3] usbtest y\n', ''))
        self.assertEqual(seen, [([hr.DMESG], {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})])

    def test_modprobe_is_usbtest_only_without_the_callers_environment(self):
        seen = self.run_with()
        self.assertEqual(self.main('modprobe')[0], 0)
        self.assertEqual(seen, [([hr.MODPROBE, 'usbtest'], {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})])


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


class Netns(Sysfs):
    """netns finds a board's (NETNS_VIDS) one USB network interface, opens a sandbox's namespace and
    moves the interface into it by ifindex; the kernel move itself is stubbed."""

    def setUp(self):
        super().setUp()
        self.owner_uid = 1000
        self.proc = self.root / 'proc'
        (self.proc / '1' / 'ns').mkdir(parents=True)
        (self.proc / '1' / 'ns' / 'user').write_text('')
        (self.proc / '4321' / 'ns').mkdir(parents=True)
        (self.proc / '4321' / 'ns' / 'net').write_text('')
        self.owner = self.root / 'sandbox-userns'
        self.owner.write_text('')
        self.netdev('1-2')
        self.moved, self.closed = [], []
        real_open, real_close = os.open, os.close

        def ioctl(fd, req, *arg):
            if req == 0xb704:                   # NS_GET_OWNER_UID
                self.assertEqual(arg, (bytes(4),))
                return struct.pack('=I', self.owner_uid)
            self.assertEqual(req, 0xb701)
            return real_open(self.owner, os.O_RDONLY)

        def close(fd):
            self.closed.append(fd)
            real_close(fd)
        for target, name, value in ((hr, 'PROC', self.proc), (hr.fcntl, 'ioctl', ioctl), (hr.os, 'close', close),
                                    (hr, 'move_link', lambda idx, fd: self.moved.append((idx, fd)))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {'SUDO_UID': '1000'})
        p.start()
        self.addCleanup(p.stop)

    def netdev(self, busport, name='usb7', driver='cdc_ncm', mac='02:02:84:6a:96:00', ifindex=7, intf='1.0',
               vid='cafe'):
        d = self.usb / busport
        if not d.exists():
            self.dev(busport, vid=vid)
        i = d / f'{busport}:{intf}'
        (i / 'net' / name).mkdir(parents=True)
        drivers = self.root / 'drivers' / driver
        drivers.mkdir(parents=True, exist_ok=True)
        if not (i / 'driver').is_symlink():
            (i / 'driver').symlink_to(drivers)
        (i / 'net' / name / 'address').write_text(f'{mac}\n')
        (i / 'net' / name / 'ifindex').write_text(f'{ifindex}\n')

    def refused(self, *args):
        rc, out, err = self.main('netns', *args)
        self.assertEqual((rc, out, self.moved), (2, '', []), err)
        return err

    def test_the_boards_interface_moves_by_index_into_the_sandbox(self):
        rc, out, err = self.main('netns', '1-2', '4321')
        self.assertEqual((rc, out, err), (0, '7 usb7\n', ''))
        (idx, fd), = self.moved
        self.assertEqual(idx, 7)
        self.assertIn(fd, self.closed)          # the namespace fd is released after the move

    def test_a_host_or_root_owned_namespace_refuses(self):
        self.owner = self.proc / '1' / 'ns' / 'user'
        self.assertIn('not owned by an unprivileged user namespace', self.refused('1-2', '4321'))

    def test_a_user_namespace_root_created_refuses(self):
        # `sudo unshare --user --map-root-user --net`: not the initial user namespace, but root's
        self.owner_uid = 0
        self.assertIn('created by root', self.refused('1-2', '4321'))
        with mock.patch.dict(os.environ):
            os.environ.pop('SUDO_UID')
            self.assertIn('created by root', self.refused('1-2', '4321'))

    def test_a_user_namespace_another_user_created_refuses(self):
        self.owner_uid = 1001
        self.assertIn('created by uid 1001, not the sudo caller 1000', self.refused('1-2', '4321'))

    def test_without_sudo_any_unprivileged_creator_is_accepted(self):
        self.owner_uid = 1001
        with mock.patch.dict(os.environ):
            os.environ.pop('SUDO_UID')
            self.assertEqual(self.main('netns', '1-2', '4321')[:2], (0, '7 usb7\n'))

    def test_a_missing_process_and_malformed_pids_refuse(self):
        for pid in ('999', '1', '0', '-5', '4321 ', '../1', ''):
            with self.subTest(pid=pid):
                self.refused('1-2', pid)

    def test_malformed_busports_refuse(self):
        for busport in ('1-0', '../1-2', '1-2/x', '1-2 ', 'usb7'):
            with self.subTest(busport=busport):
                self.refused(busport, '4321')

    def test_a_host_nic_refuses(self):
        self.netdev('1-3', name='enx1', driver='r8152')
        self.assertIn('driven by r8152', self.refused('1-3', '4321'))

    def test_a_cdc_nic_that_is_not_a_board_refuses_whatever_its_mac(self):
        # a host USB Ethernet adapter or tethered phone on a CDC driver, with a cloned or
        # randomized locally administered MAC: its vendor id is the boundary, not its MAC
        for busport, driver, vid in (('1-4', 'cdc_ether', '0bda'), ('1-5.1', 'cdc_ncm', '18d1'),
                                     ('1-6', 'rndis_host', '04e8')):
            with self.subTest(busport=busport):
                self.netdev(busport, name=f'enx{busport}', driver=driver, vid=vid)
                self.assertIn(f'vendor id {vid} is not a board', self.refused(busport, '4321'))

    def test_an_interface_replaced_while_checked_refuses(self):
        ifindex = self.usb / '1-2' / '1-2:1.0' / 'net' / 'usb7' / 'ifindex'
        real_readlink = os.readlink

        def readlink(path):              # the device re-enumerates between the two ifindex reads
            ifindex.write_text('9\n')
            return real_readlink(path)
        with mock.patch.object(hr.os, 'readlink', readlink):
            self.assertIn('changed while it was checked', self.refused('1-2', '4321'))

    def test_none_or_several_interfaces_refuse(self):
        self.dev('1-3')
        self.assertIn('expected one network interface', self.refused('1-3', '4321'))
        self.netdev('1-2', name='usb8', intf='1.2', ifindex=8)
        self.assertIn("['usb7', 'usb8']", self.refused('1-2', '4321'))

    def test_a_failed_move_is_rc1_with_its_error(self):
        with mock.patch.object(hr, 'move_link', mock.Mock(side_effect=OSError(19, 'No such device'))):
            rc, out, err = self.main('netns', '1-2', '4321')
        self.assertEqual((rc, out), (1, ''))
        self.assertIn('netns: No such device', err)


class MoveLink(unittest.TestCase):
    """move_link's one RTM_NEWLINK request and its ack, against a stubbed netlink socket."""

    def run_move(self, err):
        sent = []

        class Sock:
            def __init__(self, *args):
                self.args = args

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def settimeout(self, t):
                pass

            def send(self, msg):
                sent.append((self.args, msg))

            def recv(self, n):
                return struct.pack('=IHHII', 36, 2, 0, 1, 0) + struct.pack('=i', err) + sent[0][1][:16]
        with mock.patch.object(hr.socket, 'socket', Sock):
            hr.move_link(7, 42)
        return sent

    def test_the_request_names_the_index_and_the_namespace_fd(self):
        (args, msg), = self.run_move(0)
        self.assertEqual(args[0], hr.socket.AF_NETLINK)
        self.assertEqual(struct.unpack_from('=IHHII', msg), (len(msg), 16, 0x5, 1, 0))
        self.assertEqual(struct.unpack_from('=BxHiII', msg, 16), (0, 0, 7, 0, 0))
        self.assertEqual(struct.unpack_from('=HHI', msg, 32), (8, 28, 42))

    def test_a_kernel_error_raises_its_errno(self):
        with self.assertRaises(OSError) as cm:
            self.run_move(-19)
        self.assertEqual(cm.exception.errno, 19)


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
