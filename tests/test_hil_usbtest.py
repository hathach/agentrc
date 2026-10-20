"""skills/hil/scripts/hil_usbtest.py is the usbtest/testusb mechanism the HIL harnesses share,
moved from tinyusb's test/hil/usbtest.py with its mechanism tests (test_hil_usbtest_id.py,
test_hil_usbtest_verdict.py, and test_hil_bounded.py's run/sudo/sysfs_write/reap classes):
registration serialisation, binding, the exact testusb argv, timeout and reap, result
parsing and the host check. A fake testusb prints kernel tools/usb/testusb.c's own strings;
sysfs is a temp tree. The kernel side is proved by a rig run."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_usbtest needs fcntl and Linux sysfs')

import ast
import errno
import fcntl
import importlib.util
import io
import itertools
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts'


def load(name):
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hu = load('hil_usbtest')
VID, PID, REF = 'cafe', '4010', '0525 a4a0'
ENTRY = f'{VID} {PID} 0 {REF}'


def patch(test, obj, name, value):
    p = mock.patch.object(obj, name, value)
    p.start()
    test.addCleanup(p.stop)


class FakeDriver:
    """A usbtest driver dir whose new_id behaves like usb_store_new_id: every write appends
    a listing line, with no duplicate check."""

    def __init__(self, test, loaded=True, listed=''):
        td = tempfile.TemporaryDirectory()
        test.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.driver = self.root / 'drivers/usbtest'
        self.sys_usb = self.root / 'devices'
        self.sys_usb.mkdir(parents=True)
        self.locks = self.root / 'locks'
        self.listed = listed
        self.writes = []
        self.write_delay = 0.0
        self.modprobe = []
        if loaded:
            self.load()
        self.test = test
        for name, value in (('DRIVER', self.driver), ('SYS_USB', self.sys_usb),
                            ('sysfs_write', self.sysfs_write), ('_sudo_soft', self.sudo_soft)):
            self.patch(name, value)

    def patch(self, name, value):
        patch(self.test, hu, name, value)

    def register(self):
        hu.register_usbtest_id(VID, PID, REF, str(self.locks))

    def load(self):
        self.driver.mkdir(parents=True, exist_ok=True)
        (self.driver / 'new_id').write_text(self.listed)

    def sudo_soft(self, cmd, **kw):
        self.modprobe.append(cmd)
        if cmd == ['modprobe', 'usbtest']:
            self.load()
        return types.SimpleNamespace(returncode=0, stderr='', stdout='')

    def sysfs_write(self, path, data, check=True):
        time.sleep(self.write_delay)
        path = Path(path)
        self.writes.append((path.name, data))
        if path == self.driver / 'new_id':
            f = self.driver / 'new_id'
            f.write_text(f.read_text() + ' '.join(data.split()[:2]) + '\n')
        return True

    def interface(self, sysname, driver=None):
        intf = self.sys_usb / f'{sysname}:1.0'
        intf.mkdir()
        if driver:
            d = self.root / 'drivers' / driver
            d.mkdir(parents=True, exist_ok=True)
            (intf / 'driver').symlink_to(d)
            (d / intf.name).symlink_to(intf)   # the driver dir lists what it binds, as sysfs does
        return intf


class RegisterOnce(unittest.TestCase):
    def test_writes_the_profiled_entry_only_when_the_id_is_not_listed(self):
        for listed, writes in (('', [('new_id', ENTRY)]),
                               ('cafe 4010\n', []),
                               ('0525 a4a0\ncafe 4010\n', []),
                               ('0525 a4a0\ncafe 4011\ncafe 4010 ff\n', [('new_id', ENTRY)])):
            with self.subTest(listed=listed):
                fake = FakeDriver(self, listed=listed)
                fake.register()
                self.assertEqual(fake.writes, writes)

    def test_the_ids_are_the_callers(self):
        fake = FakeDriver(self)
        hu.register_usbtest_id('1209', '0001', '0525 a4a0', str(fake.locks))
        self.assertEqual(fake.writes, [('new_id', '1209 0001 0 0525 a4a0')])

    def test_a_malformed_id_or_reference_is_refused_before_anything_is_touched(self):
        fake = FakeDriver(self, loaded=False)
        for vid, pid, ref in (('CAFE', '4010', REF), ('cafe', '4010 ', REF), ('cafe', '4010', ''),
                              ('cafe', '4010', '0525'), ('cafe', '4010', '0525 a4a0 00')):
            with self.subTest(vid=vid, pid=pid, ref=ref), self.assertRaises(ValueError):
                hu.register_usbtest_id(vid, pid, ref, str(fake.locks))
        self.assertEqual((fake.writes, fake.modprobe, fake.locks.exists()), ([], [], False))

    def test_idempotent(self):
        fake = FakeDriver(self)
        for _ in range(3):
            fake.register()
        self.assertEqual(fake.writes, [('new_id', ENTRY)])

    def test_the_lock_lives_in_the_callers_dir(self):
        fake = FakeDriver(self)
        fake.register()
        self.assertTrue((fake.locks / hu.REGISTER_LOCK_NAME).is_file())

    def test_an_unusable_lock_dir_is_a_named_exit(self):
        fake = FakeDriver(self)
        fake.locks.write_text('')   # a file where the lock dir should be
        with self.assertRaises(SystemExit) as cm:
            fake.register()
        self.assertIn('registration lock', str(cm.exception))
        self.assertEqual(fake.writes, [])

    def test_a_refused_flock_is_a_named_exit(self):
        fake = FakeDriver(self)

        def no_locks(fh, op):
            raise OSError(errno.ENOLCK, 'No locks available')
        with mock.patch.object(fcntl, 'flock', no_locks):
            with self.assertRaises(SystemExit) as cm:
                fake.register()
        self.assertIn('No locks available', str(cm.exception))
        self.assertEqual(fake.writes, [])

    def test_a_peer_mid_write_holds_the_lock_until_its_attach_returns(self):
        # the kernel lists the id BEFORE driver_attach: a second initializer must not trust
        # the listing while the first's write is still in flight
        fake = FakeDriver(self)
        published = threading.Event()
        release = threading.Event()
        real = fake.sysfs_write

        def slow_write(path, data, check=True):
            real(path, data, check)      # the fake lists the id now, as the kernel does
            published.set()
            release.wait(5)              # ... and driver_attach is still running
            return True
        fake.patch('sysfs_write', slow_write)
        second_done = threading.Event()
        first = threading.Thread(target=fake.register, daemon=True)
        second = threading.Thread(target=lambda: (fake.register(), second_done.set()), daemon=True)

        def reap():   # runs first among the cleanups: before the patches and the temp dir go
            release.set()
            started = [t for t in (first, second) if t.ident is not None]
            for t in started:
                t.join(5)
            self.assertFalse([t for t in started if t.is_alive()], 'initializer thread outlived the test')
        self.addCleanup(reap)
        first.start()
        self.assertTrue(published.wait(5))
        second.start()
        self.assertFalse(second_done.wait(0.5), 'a battery would start beside the attach')
        release.set()
        first.join(5)
        self.assertTrue(second_done.wait(5))
        self.assertEqual(fake.writes, [('new_id', ENTRY)])

    def test_loads_the_module_when_missing(self):
        fake = FakeDriver(self, loaded=False)
        fake.register()
        self.assertEqual(fake.modprobe, [['modprobe', 'usbtest']])
        self.assertEqual(fake.writes, [('new_id', ENTRY)])

    def test_two_initializers_make_one_entry(self):
        fake = FakeDriver(self)
        fake.write_delay = 0.3   # both would pass an unlocked check before either wrote
        errors = []

        def init():
            try:
                fake.register()
            except BaseException as e:   # noqa: BLE001 - surfaced below
                errors.append(e)
        threads = [threading.Thread(target=init) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(fake.writes, [('new_id', ENTRY)])
        self.assertEqual((fake.driver / 'new_id').read_text(), 'cafe 4010\n')

    def test_the_lock_is_bounded_and_released(self):
        fake = FakeDriver(self)
        fake.locks.mkdir()
        holder = open(fake.locks / hu.REGISTER_LOCK_NAME, 'a')
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        patch(self, hu, 'REGISTER_LOCK_TIMEOUT', 0.3)
        outcome = []

        def contend():
            try:
                fake.register()
            except SystemExit as e:
                outcome.append(str(e))
        t0 = time.monotonic()
        t = threading.Thread(target=contend, daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), 'the registration lock wait is not bounded')
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(len(outcome), 1)
        self.assertIn('usbtest id registration lock', outcome[0])
        self.assertEqual(fake.writes, [])
        fcntl.flock(holder, fcntl.LOCK_UN)
        fake.register()
        probe = open(fake.locks / hu.REGISTER_LOCK_NAME, 'a')
        self.addCleanup(probe.close)
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if still held


class AKilledHolderReleasesTheLock(unittest.TestCase):
    """The registration flock dies with its holder, however the holder dies: the next
    initializer registers at once, against the same lock file."""

    def test_the_next_initializer_registers_without_waiting(self):
        fake = FakeDriver(self)
        fake.locks.mkdir()
        lock = fake.locks / hu.REGISTER_LOCK_NAME
        ready_r, ready_w = os.pipe()
        self.addCleanup(os.close, ready_r)
        try:
            holder = subprocess.Popen(
                [sys.executable, '-c',
                 'import fcntl, os, sys, time\n'
                 'fh = open(sys.argv[1], "a")\n'
                 'fcntl.flock(fh, fcntl.LOCK_EX)\n'
                 'os.write(int(sys.argv[2]), b"1")\n'
                 'time.sleep(60)\n',
                 str(lock), str(ready_w)], pass_fds=(ready_w,))
        finally:
            os.close(ready_w)
        self.addCleanup(lambda: holder.poll() is None and (holder.kill(), holder.wait(5)))
        ready, _, _ = select.select([ready_r], [], [], 10)
        self.assertTrue(ready and os.read(ready_r, 1) == b'1', 'holder never took the lock')
        with open(lock) as fh:   # the child really holds it: the kill below is what frees it
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inode = lock.stat().st_ino

        holder.send_signal(signal.SIGKILL)
        self.assertEqual(holder.wait(5), -signal.SIGKILL)
        patch(self, hu, 'REGISTER_LOCK_TIMEOUT', 2)
        with mock.patch.object(fcntl, 'flock', wraps=fcntl.flock) as flock:
            fake.register()
        attempts = [c for c in flock.call_args_list if c.args[1] & fcntl.LOCK_EX]
        self.assertEqual(len(attempts), 1)   # taken first try: the dead holder's lock is gone
        self.assertEqual(fake.writes, [('new_id', ENTRY)])
        self.assertEqual(lock.stat().st_ino, inode)


class BindOwnInterface(unittest.TestCase):
    def test_already_bound_returns_at_once(self):
        fake = FakeDriver(self, listed='cafe 4010\n')
        fake.interface('1-1', driver='usbtest')
        hu.bind_usbtest('1-1:1.0')
        self.assertEqual(fake.writes, [])

    def test_a_foreign_driver_is_unbound_and_only_our_interface_bound(self):
        fake = FakeDriver(self, listed='cafe 4010\n')
        intf = fake.interface('1-1', driver='cdc_acm')
        fake.interface('2-1', driver='cdc_acm')

        def write(path, data, check=True):
            # resolved now: the interface's driver link names the file the kernel gets
            fake.writes.append((str(Path(path).resolve()), data))
            if Path(path).name == 'unbind':
                (intf / 'driver').unlink()
            elif Path(path).name == 'bind':
                (intf / 'driver').symlink_to(fake.driver)
            return True
        fake.patch('sysfs_write', write)
        foreign = (intf / 'driver').resolve()
        hu.bind_usbtest('1-1:1.0')
        self.assertEqual(fake.writes, [(str(foreign / 'unbind'), '1-1:1.0'),
                                       (str(fake.driver.resolve() / 'bind'), '1-1:1.0')])

    def test_an_interface_that_never_binds_is_a_named_exit(self):
        fake = FakeDriver(self, listed='cafe 4010\n')
        fake.interface('1-1')
        patch(self, hu.time, 'sleep', lambda s: None)
        clock = itertools.count()
        patch(self, hu.time, 'monotonic', lambda: next(clock))
        with self.assertRaises(SystemExit) as cm:
            hu.bind_usbtest('1-1:1.0')
        self.assertIn('interface 1-1:1.0 did not bind to usbtest', str(cm.exception))

    def test_bind_never_touches_the_registry(self):
        src = (SCRIPTS / 'hil_usbtest.py').read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == 'bind_usbtest')
        body = ast.unparse(fn)
        for word in ('new_id', 'remove_id', 'modprobe'):
            self.assertNotIn(word, body)


class Pattern(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.param = Path(td.name) / 'pattern'
        self.writes = []
        patch(self, hu, 'PATTERN_PARAM', self.param)
        patch(self, hu, 'sysfs_write', lambda path, data, check=True: self.writes.append((path, data)))

    def test_written_only_when_it_differs(self):
        for current, writes in (('0\n', []), ('1\n', [(self.param, '0')])):
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
        patch(self, hu, 'needs_sudo', lambda node: False)
        for name in ('FAKE_OUT', 'FAKE_RC', 'FAKE_SLEEP'):
            self.addCleanup(os.environ.pop, name, None)

    def case(self, out, num=1, rc=0, timeout=30, sleep=0):
        os.environ['FAKE_OUT'], os.environ['FAKE_RC'], os.environ['FAKE_SLEEP'] = out, str(rc), str(sleep)
        return hu.run_case(self.testusb, NODE, num, ['-c', '1'], timeout, wedge_confirm=30, sudo_forbidden=False)

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

    def argv(self, node, sudo, params=('-c', '1')):
        cmds = []

        class Popen:
            def __init__(self, cmd, **kw):
                cmds.append(cmd)
                self.returncode = 0

            def communicate(self, timeout=None):
                return '', None

        with mock.patch.object(hu.subprocess, 'Popen', Popen), \
                mock.patch.object(hu.os, 'access', lambda p, m: not sudo), \
                mock.patch.object(hu.os, 'geteuid', lambda: 1000):
            hu.run_case('/x/testusb', node, 0, list(params), 30, wedge_confirm=30, sudo_forbidden=False)
        return cmds[0]

    def test_scan_is_confined_to_the_node_before_it_is_selected(self):
        for sudo, prefix in ((False, []), (True, ['sudo', '-n'])):
            for node in ('/dev/bus/usb/001/005', '/dev/bus/usb/003/017'):
                self.assertEqual(self.argv(node, sudo),
                                 prefix + ['/x/testusb', '-A', node, '-D', node, '-t', '0', '-c', '1'])

    def test_the_options_follow_verbatim(self):
        self.assertEqual(self.argv(NODE, False, ('-c', '64', '-s', '1024', '-g', '8'))[7:],
                         ['-c', '64', '-s', '1024', '-g', '8'])


class SudoAfterAdmission(unittest.TestCase):
    """A node that turns root-only after the caller's check (a re-enumeration between cases)
    must still not get a sudo-wrapped testusb under a recovery request."""

    def test_run_case_refuses_sudo_when_forbidden(self):
        patch(self, hu, 'needs_sudo', lambda node: True)
        launched = []
        patch(self, hu.subprocess, 'Popen',
              lambda *a, **k: launched.append(a) or (_ for _ in ()).throw(AssertionError))
        r = hu.run_case('/bin/true', NODE, 1, [], 5, wedge_confirm=30, sudo_forbidden=True)
        self.assertEqual((r['status'], launched), ('FAIL', []))
        self.assertIn('refusing a sudo-wrapped testusb', r['detail'])
        self.assertIn(NODE, r['detail'])


class RunCaseConfirmsByReap(unittest.TestCase):
    """HUNG is 'SIGKILL not reaped in 5 s', which a finite hold on the DUT's device lock also
    produces. Watching our child for `wedge_confirm` tells the two apart: reaped late is a
    timeout, not a wedge. Under sudo the child is the wrapper, so its reaping proves nothing.
    A stub Popen: no real child outlives SIGKILL without a held kernel lock."""

    def _run_case(self, sudo: bool, reaps_late: bool):
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
        patch(self, hu.os, 'access', lambda p, m: not sudo)
        patch(self, hu.os, 'geteuid', lambda: 1000)
        return hu.run_case('/bin/true', NODE, 1, [], 7, wedge_confirm=13, sudo_forbidden=False)

    def test_a_child_reaped_within_the_window_is_a_late_timeout(self):
        r = self._run_case(sudo=False, reaps_late=True)
        self.assertEqual(r['status'], 'FAIL')
        self.assertTrue(r['late_cleared'])
        self.assertIn('landed', r['detail'])
        self.assertNotIn('_proc', r)

    def test_a_child_still_stuck_after_the_window_is_hung_with_its_handle(self):
        r = self._run_case(sudo=False, reaps_late=False)
        self.assertEqual(r['status'], 'HUNG')
        self.assertEqual(r['_proc'].waited, 13)
        self.assertEqual(r['_proc'].communicates, [7, 5])

    def test_under_sudo_the_wrapper_reaping_proves_nothing(self):
        r = self._run_case(sudo=True, reaps_late=True)
        self.assertEqual(r['status'], 'HUNG')
        self.assertIsNone(r['_proc'], 'the wrapper is not the child to watch')


class RunHelper(unittest.TestCase):
    """run() is the bounded replacement for subprocess.run: sysfs_write feeds it input=."""

    def test_input_kwarg_is_honoured(self):
        r = hu.run(['cat'], input='payload', timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, 'payload'))

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


class Sudo(unittest.TestCase):
    def test_a_password_prompt_is_a_named_exit(self):
        patch(self, hu, 'run', lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'a password is required'))
        patch(self, hu.os, 'geteuid', lambda: 1000)
        with self.assertRaises(SystemExit) as cm:
            hu.sudo(['dmesg'])
        self.assertIn('sudo needs a password for: sudo -n dmesg', str(cm.exception))

    def test_root_runs_it_bare(self):
        seen = []
        patch(self, hu, 'run', lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', ''))
        patch(self, hu.os, 'geteuid', lambda: 0)
        hu.sudo(['dmesg'])
        self.assertEqual(seen, [['dmesg']])


class SudoSoftNeverRaises(unittest.TestCase):
    """Two of its call sites are inside run_case's timeout handler, where ANY raise costs the
    HUNG verdict -- and sudo() sys.exit()s on 'a password is required'."""

    def check(self, exc):
        def boom(*a, **k):
            raise exc
        patch(self, hu, 'sudo', boom)
        with redirect_stderr(io.StringIO()):
            r = hu._sudo_soft(['dmesg'])            # must not propagate
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'dmesg: {type(exc).__name__}: {exc}', r.stderr)   # callers quote it

    def test_systemexit_from_a_password_prompt_is_contained(self):
        self.check(SystemExit('sudo needs a password'))

    def test_oserror_is_contained(self):
        self.check(OSError('no such binary'))

    def test_subprocess_error_is_contained(self):
        self.check(subprocess.SubprocessError('timed out'))


class SysfsWrite(unittest.TestCase):
    def test_a_blocked_write_is_contention_or_a_wedge_and_stays_bounded(self):
        seen = {}

        def blocked(cmd, **kw):
            seen.update(kw)
            raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
        patch(self, hu, 'sudo', blocked)
        with self.assertRaises(SystemExit) as cm:
            hu.sysfs_write('/sys/bus/usb/drivers/usbtest/new_id', 'cafe 4010 0 0525 a4a0')
        self.assertEqual(seen.get('timeout'), 15)
        self.assertIn('possible device-lock contention or a wedged device', str(cm.exception))

    def test_tee_gets_the_data_and_check_decides(self):
        calls = []
        patch(self, hu, 'sudo', lambda cmd, **kw: calls.append((cmd, kw['input']))
              or subprocess.CompletedProcess(cmd, 1, '', 'denied\n'))
        self.assertFalse(hu.sysfs_write('/p', 'x', check=False))
        with self.assertRaises(SystemExit) as cm:
            hu.sysfs_write('/p', 'x')
        self.assertEqual(calls[0], (['tee', '/p'], 'x'))
        self.assertIn('write "x" > /p failed: denied', str(cm.exception))


class DmesgTail(unittest.TestCase):
    def test_the_last_eight_usbtest_lines(self):
        lines = [f'usbtest {i}' if i % 2 else f'other {i}' for i in range(30)]
        patch(self, hu, '_sudo_soft', lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '\n'.join(lines), ''))
        self.assertEqual(hu.dmesg_tail().splitlines(), [f'usbtest {i}' for i in range(15, 30, 2)])


class HostCompat(unittest.TestCase):
    """The upstream controller of the node's bus, from a fake sysfs: usbN -> its PCI function."""

    def host(self, vendor, device, driver='xhci_hcd', fw=None):
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
        self.setpci = []
        patch(self, hu, '_sudo_soft', lambda cmd, **kw: self.setpci.append(cmd) or (
            subprocess.CompletedProcess(cmd, 0, fw, '') if fw else subprocess.CompletedProcess(cmd, 1, '', 'no')))

    def check(self):
        hu.check_host_compat('/dev/bus/usb/005/007')

    def test_moschip_is_refused(self):
        self.host('0x9710', '0x9990', driver='ehci-pci')
        with self.assertRaises(SystemExit) as cm:
            self.check()
        self.assertIn('MosChip MCS9990 (0000:03:00.0)', str(cm.exception))

    def test_renesas_needs_current_firmware(self):
        self.host('0x1912', '0x0014', fw='00202609\n')
        self.check()
        self.assertEqual(self.setpci, [['setpci', '-s', '0000:03:00.0', '0x6c.l']])
        for fw, says in (('00202500\n', 'firmware 0x00202500 < 0x00202609'), (None, 'cannot read')):
            with self.subTest(fw=fw):
                self.host('0x1912', '0x0015', fw=fw)
                with self.assertRaises(SystemExit) as cm:
                    self.check()
                self.assertIn(says, str(cm.exception))

    def test_other_hosts_pass_without_setpci(self):
        self.host('0x8086', '0xa36d')
        self.check()
        self.assertEqual(self.setpci, [])

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
