"""skills/hil/scripts/usb_observe.py is the bounded USB observation the HIL harnesses share:
a sysfs read gives up on a wedged device and memoises it by inode, the enumeration walk
touches the locked `serial` attribute last and only for devices the free descriptor pair
could not rule out, and a D-state usbfs holder is found from /proc and identified from the
udev database before any locked read. Every root is a temp tree here."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('usb_observe reads Linux sysfs and /proc')

import importlib.util
import inspect
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'usb_observe.py'
NODE = '/dev/bus/usb/003/007'
TESTUSB_ARGV = ['testusb', '-A', NODE, '-D', NODE, '-t', '11', '-c', '256']


def load():
    """By file path, as a project harness loads it."""
    spec = importlib.util.spec_from_file_location('agentrc_usb_observe', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


uo = load()


def patch(test, obj, name, value):
    test.addCleanup(setattr, obj, name, getattr(obj, name))
    setattr(obj, name, value)


class BoundedRead(unittest.TestCase):
    """`serial` is served under the device lock a wedged usbfs ioctl holds, so the read is
    bounded BY DEFAULT, not opt-in: usb_scan reads it on every device matching the VID to
    find the one it wants, so one wedged DUT would stall every caller rather than one."""

    def setUp(self):
        for name in ('_stranded', '_strand_hits'):
            self.addCleanup(setattr, uo, name, dict(getattr(uo, name)))
            getattr(uo, name).clear()
        self.td = TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.fifo = os.path.join(self.td.name, 'serial')
        os.mkfifo(self.fifo)          # a read that never answers

    def test_a_wedged_attribute_gives_up_instead_of_hanging(self):
        t0 = time.monotonic()
        self.assertIsNone(uo.read_sysfs(self.fifo, timeout=0.05))
        self.assertLess(time.monotonic() - t0, 5, 'the bounded read did not give up')

    def test_the_bound_is_the_default_not_an_opt_in(self):
        """An opt-in bound that ONE call site forgets lets a single wedged DUT stall every
        worker, not one. Three call sites forgot it once."""
        for fn in (uo.read_sysfs, uo.usb_scan, uo.usb_node_serial):
            default = inspect.signature(fn).parameters['timeout'].default
            self.assertEqual(default, uo.SYSFS_READ_GRACE,
                             f'{fn.__name__} must be bounded without being asked')

    def test_a_node_that_returns_during_the_grace_is_not_memoised_as_wedged(self):
        """The inode must be captured BEFORE the reader starts. Stat it afterwards and a
        board that came back mid-read has its brand-new HEALTHY inode recorded as the
        wedged one; only a SECOND re-enumeration could ever clear it."""
        def swap():
            time.sleep(0.15)
            os.unlink(self.fifo)
            Path(self.fifo).write_text('CAFE01\n')

        threading.Thread(target=swap, daemon=True).start()
        uo.read_sysfs(self.fifo, timeout=0.6)
        self.assertEqual(uo.read_sysfs(self.fifo, timeout=1), 'CAFE01',
                         'the healthy new inode was recorded as the wedged one')

    def test_concurrent_readers_of_one_path_spend_one_credit(self):
        """Several threads reading one path: counting each READER would spend the
        per-path cap on a single wedge."""
        ts = [threading.Thread(target=lambda: uo.read_sysfs(self.fifo, timeout=0.05))
              for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(uo._stranded), 1)
        self.assertEqual(uo._strand_hits[self.fifo], 1, 'four readers of one path spent four credits')

    def test_a_flapping_wedged_device_cannot_leak_without_bound(self):
        """The inode all-clear re-arms on every re-enumeration, so a device that flaps
        while STILL wedged strands again each pass: a thread and an fd per cycle."""
        for _ in range(uo._PATH_STRAND_MAX + 4):
            uo.read_sysfs(self.fifo, timeout=0.05)
            os.unlink(self.fifo)
            os.mkfifo(self.fifo)                      # back on the same path, still wedged
        self.assertEqual(uo._strand_hits[self.fifo], uo._PATH_STRAND_MAX,
                         'a flapping device kept stranding past its per-path cap')

    def test_a_value_that_arrived_at_the_deadline_is_not_a_strand(self):
        """`out` is checked BEFORE is_alive(): a reader can deposit its value and still be
        alive for a moment after join() returns. Counting that as a strand blacklists a
        healthy attribute by inode forever."""
        good = Path(self.td.name) / 'idVendor'
        good.write_text('cafe\n')

        class Lingering(threading.Thread):        # deposits, then outlives the join
            def run(self):
                super().run()
                time.sleep(2)

        patch(self, uo.threading, 'Thread', Lingering)
        self.assertEqual(uo.read_sysfs(str(good), timeout=0.05), 'cafe')
        self.assertNotIn(str(good), uo._stranded)

    def test_path_stranded_answers_per_device(self):
        other = Path(self.td.name) / 'peer'
        other.write_text('PEER\n')
        uo.read_sysfs(self.fifo, timeout=0.05)
        self.assertTrue(uo.path_stranded(self.fifo))
        self.assertFalse(uo.path_stranded(str(other)))

    def test_a_stat_that_races_the_reader_still_memoises(self):
        """The pre-read stat is the memo KEY, and it can fail while the open that follows
        succeeds and blocks: a node replaced between the two. Without a key the give-up
        records nothing, so a poller starts another permanent thread and fd for the same
        path on every pass."""
        real_stat = uo.os.stat
        calls = []

        def flaky(path, *a, **kw):
            calls.append(path)
            if len(calls) == 1:          # only the pre-read stat loses the race
                raise OSError('vanished between stat and open')
            return real_stat(path, *a, **kw)

        patch(self, uo.os, 'stat', flaky)
        self.assertIsNone(uo.read_sysfs(self.fifo, timeout=0.05))
        self.assertIn(self.fifo, uo._stranded, 'a lost stat race leaks a fresh reader on every later poll')

    def test_a_recovered_device_is_seen_again_on_the_same_busport(self):
        """THE reflash flow: a board is flashed, then the caller polls the scan for the NEW
        inode. A busport does not change when the board comes back on the same physical
        port, so a path-only blacklist would make that poll look at everything except the
        device it is waiting for. A re-enumeration destroys the kernfs node, so a changed
        inode is the all-clear."""
        self.assertIsNone(uo.read_sysfs(self.fifo, timeout=0.05))
        os.unlink(self.fifo)                      # re-enumeration: same path, new node
        Path(self.fifo).write_text('CAFE01\n')
        self.assertEqual(uo.read_sysfs(self.fifo, timeout=1), 'CAFE01',
                         'a board that came back on the same busport stayed blacklisted')

    def test_a_lock_released_without_re_enumeration_clears_the_memo(self):
        """The device lock can be released with the kernfs node unchanged, so the inode
        all-clear never fires: only the stranded reader returning can say it is readable."""
        self.assertIsNone(uo.read_sysfs(self.fifo, timeout=0.05))
        with open(self.fifo, 'w') as f:            # releases the stranded reader
            f.write('CAFE01\n')
        deadline = time.monotonic() + 5
        while uo.path_stranded(self.fifo) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(uo.path_stranded(self.fifo), 'a finished reader left its path stranded')

        def answer():
            with open(self.fifo, 'w') as f:
                f.write('CAFE02\n')

        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(uo.read_sysfs(self.fifo, timeout=1), 'CAFE02',
                         'a board whose lock was released stayed hidden on the same inode')
        self.assertEqual(uo._strand_hits[self.fifo], 1)

    def test_a_stranded_path_is_never_read_twice(self):
        """Each expiry strands a thread and an fd for the life of the process, and callers
        poll. Re-reading would leak one pair per poll."""
        uo.read_sysfs(self.fifo, timeout=0.3)
        t0 = time.monotonic()
        for _ in range(5):
            self.assertIsNone(uo.read_sysfs(self.fifo, timeout=0.3))
        self.assertLess(time.monotonic() - t0, 0.3, 'repeat reads of a known-stranded path paid the grace again')

    def test_a_healthy_attribute_is_not_blacklisted(self):
        good = os.path.join(self.td.name, 'idVendor')
        Path(good).write_text('cafe\n')
        for _ in range(3):
            self.assertEqual(uo.read_sysfs(good, timeout=1), 'cafe')
        self.assertFalse(uo.path_stranded(good))

    def test_an_absent_attribute_reads_as_none(self):
        good = os.path.join(self.td.name, 'busnum')
        Path(good).write_text('3\n')
        self.assertEqual(uo.read_sysfs(good), '3')
        self.assertIsNone(uo.read_sysfs(os.path.join(self.td.name, 'nope')))


class UsbScan(unittest.TestCase):
    """The expensive read is `serial`, served under the device lock a wedged usbfs ioctl
    holds, so it comes LAST, only for devices the free descriptor fields could not rule out."""

    def setUp(self):
        td = TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        patch(self, uo, 'SYS_USB_DEVICES', td.name)
        self.reads = []
        real = uo.read_sysfs

        def counting(path, *a, **k):
            self.reads.append(path)
            return real(path, *a, **k)
        patch(self, uo, 'read_sysfs', counting)

    def dev(self, name, vid, pid, serial='S1'):
        d = self.root / name
        d.mkdir()
        (d / 'idVendor').write_text(vid + '\n')
        (d / 'idProduct').write_text(pid + '\n')
        if serial is not None:
            (d / 'serial').write_text(serial + '\n')
        return d

    def test_a_mismatched_vid_pid_costs_no_serial_read(self):
        self.dev('1-1', '1234', '5678')
        self.dev('1-2', 'cafe', '4010', serial='UID1')
        devs = uo.usb_scan(vid_pid=('cafe', '4010'))
        self.assertEqual(devs, [{'busport': '1-2', 'dir': str(self.root / '1-2'),
                                 'vid': 'cafe', 'pid': '4010', 'serial': 'UID1'}])
        self.assertNotIn(str(self.root / '1-1' / 'serial'), self.reads)

    def test_root_hubs_interfaces_and_serialless_devices_are_left_out(self):
        self.dev('usb1', '1d6b', '0002', serial='0000:00:14.0')
        self.dev('1-1', 'cafe', '4010', serial='UID1')
        self.dev('1-1:1.0', 'cafe', '4010', serial='UID1')
        self.dev('1-2', 'cafe', '4011', serial=None)
        self.assertEqual([d['busport'] for d in uo.usb_scan()], ['1-1'])
        self.assertEqual([d['busport'] for d in uo.usb_scan(vid='cafe', serial='uid1')], ['1-1'],
                         'serial matches case-insensitively')
        self.assertEqual(uo.usb_scan(vid='cafe', serial='UID2'), [])


class FakeRig:
    """/proc, /sys/dev/char and /run/udev/data under one temp dir."""

    def __init__(self, test):
        tmp = TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, attr in (('proc', 'PROC_ROOT'), ('sys', 'SYS_DEV_CHAR'), ('udev', 'UDEV_DATA')):
            (self.root / name).mkdir()
            patch(test, uo, attr, str(self.root / name))

    def process(self, pid, comm='testusb', state='D', argv=TESTUSB_ARGV, start='4242'):
        d = self.root / 'proc' / str(pid)
        d.mkdir(exist_ok=True)
        # fields 4..21 are filler; starttime is field 22
        (d / 'stat').write_text(f'{pid} ({comm}) {state} ' + ' '.join(['0'] * 18) + f' {start} 0 0\n')
        (d / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')

    def reap(self, pid):
        d = self.root / 'proc' / str(pid)
        for f in d.iterdir():
            f.unlink()
        d.rmdir()

    def device(self, bus=3, dev=7, serial='ABC123', udev_serial=None):
        devt = f'189:{(bus - 1) * 128 + dev - 1}'
        d = self.root / 'sys' / devt
        d.mkdir()
        if serial is not None:
            (d / 'serial').write_text(f'{serial}\n')
        if udev_serial is not None:
            (self.root / 'udev' / f'c{devt}').write_text(
                f'I:1\nE:ID_SERIAL=X_{udev_serial}\nE:ID_SERIAL_SHORT={udev_serial}\n')


class HolderLookup(unittest.TestCase):
    def setUp(self):
        self.rig = FakeRig(self)

    def test_only_a_dstate_process_with_a_node_is_a_holder(self):
        self.rig.process(10)
        self.rig.process(11, state='S')                    # a case in flight between ioctls
        self.rig.process(12, comm='sudo', argv=['sudo', '-n'] + TESTUSB_ARGV)
        self.rig.process(13, argv=['testusb', '-a'])       # no -D node
        self.rig.process(14, comm='a) b', argv=TESTUSB_ARGV)
        self.assertEqual(uo.dstate_holders('testusb'), [{'pid': 10, 'start': '4242', 'node': NODE}])
        self.assertEqual([h['pid'] for h in uo.dstate_holders('a) b')], [14], 'comm may contain ") "')

    def test_reaped_zombie_and_recycled_pids_no_longer_hold(self):
        self.rig.process(30)
        h = uo.dstate_holders('testusb')[0]
        self.assertTrue(uo.proc_holds(h))
        self.rig.process(30, state='Z')
        self.assertFalse(uo.proc_holds(h), 'a zombie has closed the node')
        self.rig.process(30, start='9999')
        self.assertFalse(uo.proc_holds(h), 'a recycled pid is another process')
        self.rig.reap(30)
        self.assertFalse(uo.proc_holds(h))

    def test_the_udev_record_spares_the_locked_serial_read(self):
        self.rig.device(serial=None, udev_serial='ABC123')
        reads = []
        patch(self, uo, 'read_sysfs', lambda path, timeout=0: reads.append(path))
        self.assertEqual(uo.usb_node_serial(NODE), 'ABC123')
        self.assertEqual(reads, [], 'a held device would strand this read for its grace')

    def test_without_a_udev_record_the_bounded_read_decides(self):
        self.rig.device(serial='ABC123')
        self.assertEqual(uo.usb_node_serial(NODE), 'ABC123')
        self.rig.device(dev=8, serial=None)
        self.assertIsNone(uo.usb_node_serial('/dev/bus/usb/003/008'), 'unidentified, not a guess')
        self.assertIsNone(uo.usb_node_serial('/dev/bus/usb/003/099'))
        self.assertIsNone(uo.usb_node_serial('/dev/null'))


if __name__ == '__main__':
    unittest.main()
