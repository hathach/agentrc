"""skills/hil/scripts/hil_usbtest.py is the usbtest/testusb mechanism the HIL harnesses share,
moved from tinyusb's test/hil/usbtest.py with its mechanism tests (test_hil_usbtest_id.py,
test_hil_usbtest_verdict.py, and test_hil_bounded.py's run/sysfs_write/reap classes):
loading and binding, the exact testusb argv, timeout and reap, result
parsing and the host check. A fake testusb prints kernel tools/usb/testusb.c's own strings;
sysfs is a temp tree. The kernel side is proved by a rig run."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_usbtest needs fcntl and Linux sysfs')

import errno
import importlib.util
import io
import itertools
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import types
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts'


def load(name):
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hu = load('hil_usbtest')
VID, PID = 'cafe', '4010'


def patch(test, obj, name, value):
    p = mock.patch.object(obj, name, value)
    p.start()
    test.addCleanup(p.stop)


def setUpModule():
    guard_survivors(__name__)


class Binding(unittest.TestCase):
    """ensure_usbtest_bound against a temp sysfs: run() stands in for the CONNECT child and
    binds the interface the way device_attach would."""
    NODE = '/dev/bus/usb/001/005'

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.sys_usb = self.root / 'devices'
        self.sys_usb.mkdir()
        self.driver = self.root / 'drivers/usbtest'
        self.driver.mkdir(parents=True)
        self.intf = self.sys_usb / '1-1:1.0'
        self.intf.mkdir()
        self.runs, self.helpers = [], []
        self.attach, self.rc = 'usbtest', 0
        patch(self, hu, 'SYS_USB', self.sys_usb)
        patch(self, hu, 'DRIVER', self.driver)
        patch(self, hu, 'run', self.run_child)
        patch(self, hu, '_root_soft', self.root_soft)
        patch(self, hu, 'node_writable', lambda node: True)
        patch(self, hu, 'dmesg_tail', lambda: 'usbtest dmesg')
        patch(self, hu.platform, 'system', lambda: 'Linux')
        patch(self, hu.platform, 'machine', lambda: 'x86_64')

    def bind(self, driver):
        d = self.root / 'drivers' / driver
        d.mkdir(parents=True, exist_ok=True)
        (self.intf / 'driver').symlink_to(d)

    def run_child(self, cmd, **kw):
        self.runs.append((cmd, kw.get('timeout')))
        if self.attach:
            self.bind(self.attach)
        return subprocess.CompletedProcess(cmd, self.rc, '', errno.errorcode.get(self.rc, '') if self.rc else '')

    def root_soft(self, *cmd, **kw):
        self.helpers.append(list(cmd))
        if cmd == ('modprobe',):
            self.driver.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(list(cmd), 0, '', '')

    def exit_of(self):
        with self.assertRaises(SystemExit) as cm:
            hu.ensure_usbtest_bound('1-1:1.0', self.NODE)
        return str(cm.exception)

    def test_already_bound_runs_nothing(self):
        self.bind('usbtest')
        hu.ensure_usbtest_bound('1-1:1.0', self.NODE)
        self.assertEqual((self.runs, self.helpers), ([], []))

    def test_an_unbound_interface_gets_one_bounded_connect_through_its_node(self):
        hu.ensure_usbtest_bound('1-1:1.0', self.NODE)
        script = os.path.abspath(hu.__file__)
        self.assertEqual(self.runs, [([sys.executable, '-I', script, 'connect', self.NODE, '1-1:1.0'],
                                      hu.CONNECT_TIMEOUT)])

    def test_an_unwritable_node_is_a_named_exit_never_sudo(self):
        patch(self, hu, 'node_writable', lambda node: False)
        self.assertIn(f'{self.NODE} is not writable', self.exit_of())
        self.assertEqual(self.runs, [])

    def test_an_unknown_ioctl_abi_is_refused_before_any_child(self):
        patch(self, hu.platform, 'machine', lambda: 'ppc64le')
        self.assertIn('no known ioctl ABI here (Linux ppc64le)', self.exit_of())
        self.assertEqual(self.runs, [])

    def test_ebusy_from_a_probe_that_won_the_race_is_success(self):
        self.rc = errno.EBUSY
        hu.ensure_usbtest_bound('1-1:1.0', self.NODE)

    def test_an_interface_held_by_another_driver_is_never_stolen(self):
        self.bind('usbfs')
        self.assertIn('held by usbfs, not usbtest', self.exit_of())
        self.assertEqual(self.runs, [])

    def test_a_connect_that_binds_another_driver_is_named(self):
        self.attach = 'cdc_acm'
        self.assertIn('held by cdc_acm, not usbtest', self.exit_of())

    def test_a_child_that_failed_otherwise_is_named_by_its_exit(self):
        self.attach, self.rc = None, 1
        patch(self, hu, 'run', lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', ''))
        self.assertIn('(CONNECT exit 1)', self.exit_of())

    def test_a_connect_that_binds_nothing_names_the_errno_and_dmesg(self):
        self.attach, self.rc = None, errno.ENODEV
        why = self.exit_of()
        self.assertIn('1-1:1.0 did not bind to usbtest (CONNECT ENODEV)', why)
        self.assertIn('usbtest dmesg', why)

    def test_a_blocked_connect_is_a_bounded_named_exit(self):
        def blocked(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw['timeout'])
        patch(self, hu, 'run', blocked)
        self.assertIn(f'blocked >{hu.CONNECT_TIMEOUT}s', self.exit_of())

    def test_loads_the_module_when_missing(self):
        shutil.rmtree(self.driver)
        hu.ensure_usbtest_bound('1-1:1.0', self.NODE)
        self.assertEqual(self.helpers, [['modprobe']])

    def test_a_module_that_will_not_load_is_a_named_exit(self):
        shutil.rmtree(self.driver)
        patch(self, hu, '_root_soft', lambda *cmd, **kw: subprocess.CompletedProcess(list(cmd), 1, '', 'not found'))
        self.assertIn('cannot load usbtest module: not found', self.exit_of())
        self.assertEqual(self.runs, [])


class UsbfsConnect(unittest.TestCase):
    """The CONNECT child: the ioctl it issues and the enumeration check before it."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.sys_usb = self.root / 'devices'
        (self.sys_usb / '1-1').mkdir(parents=True)
        self.dev_char = self.root / 'char'
        self.dev_char.mkdir()
        self.node = self.root / 'node'
        self.node.write_text('')
        self.ioctls = []
        patch(self, hu, 'SYS_USB', self.sys_usb)
        patch(self, hu, 'SYS_DEV_CHAR', self.dev_char)
        patch(self, hu.fcntl, 'ioctl', lambda fd, req, arg: self.ioctls.append((req, arg)))
        patch(self, hu.os, 'fstat', lambda fd: types.SimpleNamespace(st_rdev=os.makedev(189, 4)))

    def test_the_requests_match_the_uapi_header(self):
        # _IOWR('U', 18, struct usbdevfs_ioctl) and _IO('U', 23)
        size = struct.calcsize('@iiP')
        self.assertEqual(hu.USBDEVFS_IOCTL, (3 << 30) | (size << 16) | (ord('U') << 8) | 18)
        self.assertEqual(hu.USBDEVFS_CONNECT, (ord('U') << 8) | 23)
        if size == 16:
            self.assertEqual(hu.USBDEVFS_IOCTL, 0xC0105512)

    def test_connects_the_named_interface_of_the_device_the_fd_holds(self):
        (self.dev_char / '189:4').symlink_to(self.sys_usb / '1-1')
        self.assertEqual(hu._usbfs_connect(str(self.node), '1-1:1.2'), 0)
        self.assertEqual(self.ioctls, [(hu.USBDEVFS_IOCTL, struct.pack('@iiP', 2, hu.USBDEVFS_CONNECT, 0))])

    def test_a_replaced_device_takes_no_connect(self):
        (self.sys_usb / '1-2').mkdir()
        (self.dev_char / '189:4').symlink_to(self.sys_usb / '1-2')
        self.assertEqual(hu._usbfs_connect(str(self.node), '1-1:1.0'), errno.ENODEV)
        self.assertEqual(self.ioctls, [])

    def test_the_ioctl_errno_is_the_result(self):
        (self.dev_char / '189:4').symlink_to(self.sys_usb / '1-1')

        def busy(fd, req, arg):
            raise OSError(errno.EBUSY, 'busy')
        patch(self, hu.fcntl, 'ioctl', busy)
        self.assertEqual(hu._usbfs_connect(str(self.node), '1-1:1.0'), errno.EBUSY)

    def test_the_script_entry_exits_with_the_errno(self):
        script = str(SCRIPTS / 'hil_usbtest.py')
        r = subprocess.run([sys.executable, '-I', script, 'connect', str(self.root / 'absent'), '1-1:1.0'],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr.strip()), (errno.ENOENT, 'ENOENT'))
        r = subprocess.run([sys.executable, '-I', script, 'bind'], capture_output=True, text=True, timeout=30)
        self.assertIn('usage: hil_usbtest.py connect NODE INTERFACE', r.stderr)

    def test_the_child_runs_from_this_file_alone(self):
        # stdlib only: no hil_load bootstrap, no sibling module (setUp's fstat
        # patch is process-wide, so the copy is a plain read and write)
        alone = self.root / 'alone'
        alone.mkdir()
        (alone / 'hil_usbtest.py').write_text((SCRIPTS / 'hil_usbtest.py').read_text())
        r = subprocess.run([sys.executable, '-I', str(alone / 'hil_usbtest.py'), 'connect',
                            str(self.root / 'absent'), '1-1:1.0'], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr.strip()), (errno.ENOENT, 'ENOENT'))


class Pattern(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.param = Path(td.name) / 'pattern'
        self.writes = []
        patch(self, hu, 'PATTERN_PARAM', self.param)
        patch(self, hu, 'sysfs_write', lambda verb, *args: self.writes.append((verb, *args)))

    def test_written_only_when_it_differs(self):
        for current, writes in (('0\n', []), ('1\n', [('pattern', '0')])):
            self.writes.clear()
            self.param.write_text(current)
            hu.set_pattern(0)
            self.assertEqual(self.writes, writes)

    def test_a_module_without_the_param_is_a_named_exit(self):
        with self.assertRaises(SystemExit) as cm:
            hu.set_pattern(0)
        self.assertIn('"pattern" param', str(cm.exception))


NODE = '/dev/bus/usb/001/005'
# testusb -A <node> -D <node> -t <n>: stderr first, then stdout. FAKE_OUT is a format string over the
# node and case; '%s' is the device name as testusb prints it.
FAKE_TESTUSB = f'''#!{sys.executable}
import os, sys, time
node = sys.argv[sys.argv.index('-D') + 1]
case = int(sys.argv[sys.argv.index('-t') + 1])
time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))
sys.stdout.write(os.environ['FAKE_OUT'].replace('%s', node).replace('%d', str(case)))
sys.exit(int(os.environ.get('FAKE_RC', '0')))
'''
SPEED = '%s: %s may see only control tests\nhigh speed\t%s\t0\n'   # testusb.c main, handle_testdev


class RunCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.testusb = os.path.join(td.name, 'testusb')
        with open(self.testusb, 'w') as f:
            f.write(FAKE_TESTUSB)
        os.chmod(self.testusb, stat.S_IRWXU)
        patch(self, hu, 'dmesg_tail', lambda: 'dmesg')
        patch(self, hu, 'node_writable', lambda node: True)
        for name in ('FAKE_OUT', 'FAKE_RC', 'FAKE_SLEEP'):
            self.addCleanup(os.environ.pop, name, None)

    def case(self, out, num=1, rc=0, timeout=30, sleep=0):
        os.environ['FAKE_OUT'], os.environ['FAKE_RC'], os.environ['FAKE_SLEEP'] = out, str(rc), str(sleep)
        return hu.run_case(self.testusb, NODE, num, ['-c', '1'], timeout, wedge_confirm=30)

    def test_pass_line(self):
        r = self.case(SPEED + '%s test %d,    3.004567 secs\n')   # "%s test %d, %4d.%.06d secs"
        self.assertEqual(r, {'status': 'PASS', 'secs': 3.004567})

    def test_error_line(self):
        r = self.case(SPEED + '%s test %d --> 110 (Connection timed out)\n')   # "%s test %d --> %d (%s)"
        self.assertEqual(r, {'status': 'FAIL', 'detail': 'errno 110 (Connection timed out)', 'dmesg': 'dmesg'})

    def test_another_cases_result_line_is_not_this_ones(self):
        r = self.case(SPEED + '%s test 2,    1.000000 secs\n', num=1)
        self.assertEqual(r['status'], 'NOTRUN')

    def test_opened_but_no_result_is_a_kernel_skip(self):
        r = self.case(SPEED)                           # -EOPNOTSUPP: testusb continues silently
        self.assertEqual(r['status'], 'NOTRUN')

    def test_never_opened_the_node_is_a_failure_not_a_skip(self):
        for out, rc in (("can't open dev file r/w: Permission denied\n", 0),
                        ("must specify '-a' or '-D dev', or DEVICE=/dev/bus/usb/BBB/DDD in env\n", 1),
                        ('USB device files are missing\n', 255),
                        ('', 0)):
            r = self.case(out, rc=rc)
            self.assertEqual(r['status'], 'FAIL', out)
            self.assertEqual(r['detail'], f'testusb did not run the case (rc {rc})')
            self.assertEqual(r['stderr'], out.strip())

    def test_speed_line_for_another_node_does_not_count(self):
        r = self.case('high speed\t/dev/bus/usb/001/006\t0\n')
        self.assertEqual(r['status'], 'FAIL')

    def test_a_timeout_kills_and_reaps_the_child(self):
        t0 = time.monotonic()
        r = self.case(SPEED, timeout=1, sleep=30)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(r, {'status': 'FAIL', 'detail': 'timeout after 1s', 'dmesg': 'dmesg'})


class ScanScope(unittest.TestCase):
    """testusb -D alone still ftw()s and opens every usbfs node; -A <node> scans only the DUT
    (#4047). -A falls through to -a and clears -D, so it must come first."""

    def argv(self, node, params=('-c', '1')):
        cmds = []

        class Popen:
            def __init__(self, cmd, **kw):
                cmds.append(cmd)
                self.returncode = 0

            def communicate(self, timeout=None):
                return '', None

        with mock.patch.object(hu.subprocess, 'Popen', Popen), \
                mock.patch.object(hu, 'node_writable', lambda node: True):
            hu.run_case('/x/testusb', node, 0, list(params), 30, wedge_confirm=30)
        return cmds[0]

    def test_scan_is_confined_to_the_node_before_it_is_selected(self):
        for node in ('/dev/bus/usb/001/005', '/dev/bus/usb/003/017'):
            self.assertEqual(self.argv(node), ['/x/testusb', '-A', node, '-D', node, '-t', '0', '-c', '1'])

    def test_the_options_follow_verbatim(self):
        self.assertEqual(self.argv(NODE, ('-c', '64', '-s', '1024', '-g', '8'))[7:],
                         ['-c', '64', '-s', '1024', '-g', '8'])


class UnwritableNode(unittest.TestCase):
    """A node this user cannot write (no udev rule, or a re-enumeration between cases) fails
    the case without running testusb, never under sudo."""

    def test_run_case_fails_without_launching(self):
        patch(self, hu, 'node_writable', lambda node: False)
        launched = []
        patch(self, hu.subprocess, 'Popen',
              lambda *a, **k: launched.append(a) or (_ for _ in ()).throw(AssertionError))
        r = hu.run_case('/bin/true', NODE, 1, [], 5, wedge_confirm=30)
        self.assertEqual((r['status'], launched), ('FAIL', []))
        self.assertIn('udev rule', r['detail'])
        self.assertIn(NODE, r['detail'])


class RunCaseConfirmsByReap(unittest.TestCase):
    """HUNG is 'SIGKILL not reaped in 5 s', which a finite hold on the DUT's device lock also
    produces. Watching our child for `wedge_confirm` tells the two apart: reaped late is a
    timeout, not a wedge.
    A stub Popen: no real child outlives SIGKILL without a held kernel lock."""

    def _run_case(self, reaps_late: bool):
        class Popen:
            def __init__(self, cmd, **kw):
                self.cmd = cmd
                self.communicates = []

            def communicate(self, timeout=None):
                self.communicates.append(timeout)
                raise subprocess.TimeoutExpired(self.cmd, timeout)

            def kill(self):
                pass

            def wait(self, timeout=None):
                self.waited = timeout
                if not reaps_late:
                    raise subprocess.TimeoutExpired(self.cmd, timeout)

        patch(self, hu.subprocess, 'Popen', Popen)
        patch(self, hu, 'dmesg_tail', lambda: '')
        patch(self, hu, 'node_writable', lambda node: True)
        return hu.run_case('/bin/true', NODE, 1, [], 7, wedge_confirm=13)

    def test_a_child_reaped_within_the_window_is_a_late_timeout(self):
        r = self._run_case(reaps_late=True)
        self.assertEqual(r['status'], 'FAIL')
        self.assertTrue(r['late_cleared'])
        self.assertIn('landed', r['detail'])
        self.assertNotIn('_proc', r)

    def test_a_child_still_stuck_after_the_window_is_hung_with_its_handle(self):
        r = self._run_case(reaps_late=False)
        self.assertEqual(r['status'], 'HUNG')
        self.assertEqual(r['_proc'].waited, 13)
        self.assertEqual(r['_proc'].communicates, [7, 5])


class RunHelper(unittest.TestCase):
    """run() is the bounded replacement for subprocess.run the root helpers go through."""

    def test_capture_output_kwarg_is_accepted(self):
        r = hu.run(['printf', 'x'], capture_output=True, timeout=10)
        self.assertEqual(r.stdout, 'x')

    def test_timeout_is_bounded_raises_and_reaps(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = os.path.join(td, 'pid')
            t0 = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                hu.run([sys.executable, '-c', 'import os, sys, time\n'
                        'open(sys.argv[1], "w").write(str(os.getpid()))\n'
                        'time.sleep(30)', pidfile], timeout=1)
            self.assertLess(time.monotonic() - t0, 15)
            pid = int(Path(pidfile).read_text())
        with self.assertRaises(ProcessLookupError):   # killed and reaped, not a zombie
            os.kill(pid, 0)

    def test_the_helper_stays_in_our_process_group(self):
        r = hu.run([sys.executable, '-c', 'import os; print(os.getpgrp())'], timeout=10)
        self.assertEqual(int(r.stdout), os.getpgrp())

    def test_default_bound(self):
        seen = {}

        class Popen:
            def __init__(self, cmd, **kw):
                self.returncode = 0

            def communicate(self, input=None, timeout=None):
                seen['timeout'] = timeout
                return '', ''
        with mock.patch.object(hu.subprocess, 'Popen', Popen):
            hu.run(['true'])
        self.assertEqual(seen['timeout'], hu.HELPER_TIMEOUT)


class Root(unittest.TestCase):
    """root() runs a hil_root.py verb through hil_root.argv (test_hil_root.py has the argv)."""

    def test_a_password_prompt_is_a_named_exit(self):
        patch(self, hu, 'run', lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'a password is required'))
        patch(self, hu.hil_root, 'argv', lambda *a: ['sudo', '-n', '/x/hil_root.py', *a])
        with self.assertRaises(SystemExit) as cm:
            hu.root('dmesg')
        self.assertIn('sudo needs a password for: sudo -n /x/hil_root.py dmesg', str(cm.exception))

    def test_the_verb_goes_through_argv_with_the_callers_bound(self):
        seen = []
        patch(self, hu, 'run', lambda cmd, **kw: seen.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0, '', ''))
        patch(self, hu.hil_root, 'argv', lambda *a: ['H', *a])
        hu.root('pattern', '1', timeout=4)
        self.assertEqual(seen, [(['H', 'pattern', '1'], {'timeout': 4})])


class RootSoftNeverRaises(unittest.TestCase):
    """Two of its call sites are inside run_case's timeout handler, where ANY raise costs the
    HUNG verdict -- and root() sys.exit()s on 'a password is required'."""

    def check(self, exc):
        def boom(*a, **k):
            raise exc
        patch(self, hu, 'root', boom)
        with redirect_stderr(io.StringIO()):
            r = hu._root_soft('dmesg')            # must not propagate
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'dmesg: {type(exc).__name__}: {exc}', r.stderr)   # callers quote it

    def test_systemexit_from_a_password_prompt_is_contained(self):
        self.check(SystemExit('sudo needs a password'))

    def test_oserror_is_contained(self):
        self.check(OSError('no such binary'))

    def test_subprocess_error_is_contained(self):
        self.check(subprocess.SubprocessError('timed out'))


class SysfsWrite(unittest.TestCase):
    def test_a_blocked_write_is_a_bounded_named_exit(self):
        seen = {}

        def blocked(*cmd, **kw):
            seen.update(kw)
            raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
        patch(self, hu, 'root', blocked)
        with self.assertRaises(SystemExit) as cm:
            hu.sysfs_write('pattern', '0')
        self.assertEqual(seen.get('timeout'), 15)
        self.assertIn('blocked >15s', str(cm.exception))

    def test_the_verb_passes_through_and_a_failure_is_a_named_exit(self):
        calls = []
        patch(self, hu, 'root', lambda *cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1, '', 'denied\n'))
        with self.assertRaises(SystemExit) as cm:
            hu.sysfs_write('pattern', '1')
        self.assertEqual(calls, [('pattern', '1')])
        self.assertIn('hil_root pattern 1 failed: denied', str(cm.exception))


class DmesgTail(unittest.TestCase):
    def test_the_last_eight_lines_of_the_helpers_usbtest_log(self):
        lines = [f'usbtest {i}' for i in range(30)]
        patch(self, hu, '_root_soft', lambda *cmd, **kw: subprocess.CompletedProcess(cmd, 0, '\n'.join(lines), ''))
        self.assertEqual(hu.dmesg_tail().splitlines(), lines[-8:])


class FindDevice(unittest.TestCase):
    """find_device is usbtest's lookup on usb_observe.usb_scan: a full scan reads every
    vid:pid peer's bounded `serial`; a device found by serial is cached and re-confirmed by
    its own serial alone while the lock-free picture of its peers holds."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        uo = load('usb_observe')            # a fresh strand memo per test
        patch(self, uo, 'SYS_USB_DEVICES', td.name)
        patch(self, hu, 'usb_observe', uo)
        patch(self, hu, '_DEV_CACHE', {})
        self.reads = []
        real = uo.read_sysfs

        self.on_read = {}                   # busport -> called once before its serial is read

        def counting(path, *a, **k):
            busport = os.path.basename(os.path.dirname(path))
            self.reads.append(busport)
            if busport in self.on_read:
                self.on_read.pop(busport)()
            return real(path, *a, **k)
        patch(self, uo, 'read_sysfs', counting)
        self.devnums = itertools.count(2)   # each enumeration gets a new address, as the kernel's

    def dev(self, busport, serial='UID1', pid=PID, **attrs):
        d = self.root / busport
        d.mkdir()
        values = {'idVendor': VID, 'idProduct': pid, 'busnum': busport.split('-')[0],
                  'devnum': str(next(self.devnums)), 'speed': '480', 'bcdDevice': '0102',
                  'serial': serial, **attrs}
        for name, value in values.items():
            if value is not None:
                (d / name).write_text(value + '\n')
        return d

    def gone(self, busport):
        shutil.rmtree(self.root / busport)

    def replace(self, busport, serial='UID1', **attrs):
        """A new enumeration at `busport`: made before the old one is removed, so its dir
        cannot reuse the old inode, as kernfs never does."""
        old = self.root / busport
        os.rename(old, self.root / f'old{next(self.devnums)}')
        self.dev(busport, serial, **attrs)

    def find(self, serial='UID1', timeout=0.05):
        self.reads.clear()
        return hu.find_device(VID, PID, serial, timeout=timeout)

    def test_the_device_answering_the_serial_is_described(self):
        self.dev('3-1', serial='UID1', bcdDevice='0103')
        self.dev('3-2', serial='OTHER')
        self.dev('3-3', serial='UID1', pid='4011')
        self.assertEqual(self.find('uid1'), {'sysname': '3-1', 'serial': 'UID1',
                                             'node': '/dev/bus/usb/003/002', 'speed': '480', 'tier': 3})

    def test_ids_match_as_ints_or_hex_strings_of_either_case(self):
        self.dev('3-1')
        for ids in ((0xcafe, 0x4010), ('CAFE', '4010'), (VID, PID)):
            with self.subTest(ids=ids):
                self.assertEqual(hu.find_device(*ids, 'UID1', timeout=0.05)['sysname'], '3-1')

    def test_a_wedged_peer_costs_one_bounded_read_per_scan(self):
        self.dev('1-1')
        self.dev('1-3', serial='OTHER')
        os.mkfifo(self.dev('1-2', serial=None) / 'serial')       # a read that never answers
        self.assertEqual(self.find(timeout=0.5)['sysname'], '1-1')
        self.assertEqual(self.reads.count('1-2'), 1)
        t0 = time.monotonic()
        for _ in range(3):
            self.assertEqual(self.find(timeout=0.5)['sysname'], '1-1')
            self.assertEqual(sorted(self.reads), ['1-1', '1-2'],
                             'the cached path reads its own serial and the unread peer, once each')
        self.assertLess(time.monotonic() - t0, 0.5, 'the stranded peer cost its grace again')

    def test_a_dual_port_enumeration_is_ambiguous_until_it_drops(self):
        self.dev('1-1')
        self.dev('1-2')
        self.assertEqual(self.find(), {'ambiguous': ['1-1', '1-2']})
        self.assertEqual(hu._DEV_CACHE, {})
        self.gone('1-2')
        self.assertEqual(self.find()['sysname'], '1-1')

    def test_a_second_device_after_the_cache_is_ambiguous(self):
        self.dev('1-1')
        self.find()
        self.assertTrue(hu._DEV_CACHE)
        self.dev('1-2')
        self.assertEqual(self.find(), {'ambiguous': ['1-1', '1-2']})

    def test_a_peer_unreadable_at_scan_time_that_answers_later_is_rescanned(self):
        """The serial of a device that was readable to nobody may be ours: once it answers,
        the cache no longer knows the match count."""
        self.dev('1-1')
        self.dev('1-2', serial=None)
        self.find()
        (self.root / '1-2' / 'serial').write_text('UID1\n')     # same enumeration, now answers
        self.assertEqual(self.find(), {'ambiguous': ['1-1', '1-2']})

    def test_a_renumbered_sysname_is_re_resolved_by_serial(self):
        self.dev('1-1')
        self.assertEqual(self.find()['node'], '/dev/bus/usb/001/002')
        self.gone('1-1')
        self.dev('1-1', serial='OTHER')                         # another board took the port
        self.dev('2-3')
        self.assertEqual(self.find(), {'sysname': '2-3', 'serial': 'UID1',
                                       'node': '/dev/bus/usb/002/004', 'speed': '480', 'tier': 2})

    def test_a_re_enumeration_during_the_cached_read_is_rescanned(self):
        self.dev('1-1')
        self.find()
        self.on_read['1-1'] = lambda: self.replace('1-1')
        self.assertEqual(self.find()['node'], '/dev/bus/usb/001/004',
                         "the old enumeration's node went out with the new one's serial")

    def test_a_peer_re_enumerated_at_the_same_address_is_rescanned(self):
        """devnum wraps (v6.12 hub.c choose_devnum), so busport, busnum and devnum can all
        repeat: only the dir's inode tells the new enumeration apart."""
        self.dev('1-1')
        self.dev('1-2', serial='OTHER')
        self.find()
        self.replace('1-2', devnum=(self.root / '1-2' / 'devnum').read_text().strip())
        self.assertEqual(self.find(), {'ambiguous': ['1-1', '1-2']})

    def test_a_device_seen_only_by_the_scan_is_not_cached(self):
        """Picture and selection from one observation: a device that came for the scan and
        left before the picture must not be cached against a picture that never held it."""
        uo, real = hu.usb_observe, hu.usb_observe.usb_scan

        def visiting(**kw):
            self.dev('1-1')
            try:
                return real(**kw)
            finally:
                self.gone('1-1')
        patch(self, uo, 'usb_scan', visiting)
        self.assertEqual(self.find()['sysname'], '1-1')
        patch(self, uo, 'usb_scan', real)
        self.assertIsNone(self.find())

    def test_the_cached_port_answering_another_serial_is_never_trusted(self):
        self.dev('1-1')
        self.find()
        (self.root / '1-1' / 'serial').write_text('OTHER\n')
        self.assertIsNone(self.find())
        self.assertEqual(hu._DEV_CACHE, {})

    def test_a_missing_or_malformed_attribute_is_refused_not_guessed(self):
        for attrs, named in (({'busnum': None}, 'busnum'), ({'devnum': 'x'}, 'devnum'),
                             ({'speed': None}, 'speed'), ({'speed': 'fast'}, 'speed'),
                             ({'speed': 'unknown'}, 'speed'), ({'bcdDevice': '12'}, 'bcdDevice')):
            with self.subTest(named, **attrs):
                self.dev('1-1', **attrs)
                try:
                    self.assertEqual([d['busport'] for d in hu.usb_observe.usb_scan()], ['1-1'],
                                     'usb_scan keeps the device its callers always received')
                    with self.assertRaises(SystemExit) as cm:
                        self.find()
                    self.assertIn(named, str(cm.exception.code))
                finally:
                    self.gone('1-1')

    def test_a_device_that_left_mid_walk_is_not_found(self):
        gone = {'busport': '1-1', 'dir': str(self.root / '1-1'), 'ino': 1, 'vid': VID, 'pid': PID,
                'busnum': None, 'devnum': None, 'speed': None, 'bcdDevice': None, 'serial': 'UID1'}
        patch(self, hu.usb_observe, 'usb_scan', lambda **kw: [gone])
        self.assertIsNone(self.find())

    def test_several_devices_without_a_serial_is_a_named_exit(self):
        self.dev('1-1', serial='A')
        self.assertEqual(self.find(None)['serial'], 'A')
        self.assertEqual(hu._DEV_CACHE, {}, 'only a serial lookup is cached')
        self.dev('1-2', serial='B')
        with self.assertRaises(SystemExit) as cm:
            self.find(None)
        self.assertIn('use --serial: ', str(cm.exception.code))


class HostCompat(unittest.TestCase):
    """The upstream controller of the node's bus, from a fake sysfs: usbN -> its PCI function."""

    def host(self, vendor, device, driver='xhci_hcd'):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        pci = root / 'pci' / '0000:03:00.0'
        (pci / 'usb5').mkdir(parents=True)
        (pci / 'vendor').write_text(vendor + '\n')
        (pci / 'device').write_text(device + '\n')
        (root / 'drivers' / driver).mkdir(parents=True)
        (pci / 'driver').symlink_to(root / 'drivers' / driver)
        (root / 'devices').mkdir()
        (root / 'devices' / 'usb5').symlink_to(pci / 'usb5')
        patch(self, hu, 'SYS_USB', root / 'devices')
        self.rooted = []
        patch(self, hu, '_root_soft', lambda *cmd, **kw: self.rooted.append(list(cmd)))

    def check(self):
        hu.check_host_compat('/dev/bus/usb/005/007')

    def test_moschip_is_refused(self):
        self.host('0x9710', '0x9990', driver='ehci-pci')
        with self.assertRaises(SystemExit) as cm:
            self.check()
        self.assertIn('MosChip MCS9990 (0000:03:00.0)', str(cm.exception))

    def test_other_hosts_pass_without_a_root_call(self):
        for vendor, device in (('0x8086', '0xa36d'), ('0x1912', '0x0014'), ('0x1912', '0x0015')):
            with self.subTest(host=(vendor, device)):
                self.host(vendor, device)
                self.check()
                self.assertEqual(self.rooted, [])

    def test_an_unprobeable_host_warns_and_passes(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        patch(self, hu, 'SYS_USB', Path(td.name))
        patch(self, hu.time, 'sleep', lambda s: None)
        err = io.StringIO()
        with redirect_stderr(err):
            self.check()
        self.assertIn('skipping the host compatibility check', err.getvalue())


if __name__ == '__main__':
    unittest.main()
