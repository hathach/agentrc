"""Tests for the usb-sniffer skill's sniff.py: capture refuses to guess the
speed, the unit or run unbounded, derives the speed from sysfs, pins the tool
to the selected unit, and reports a capture already holding it; addr reads
SET_ADDRESS out of a capture and names every device when several enumerated."""
import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'usb-sniffer' / 'scripts' / 'sniff.py'
sys.path.insert(0, str(SCRIPT.parent))
import sniff  # noqa: E402


class PortSpeedTest(unittest.TestCase):
    def test_sysfs_speed_maps_to_the_tool_names(self):
        with tempfile.TemporaryDirectory() as d:
            sysfs = Path(d)
            for port, mbps, want in (('3-2.7', '480', 'hs'), ('1-5', '12', 'fs'), ('1-6', '1.5', 'ls')):
                (sysfs / port).mkdir()
                (sysfs / port / 'speed').write_text(mbps + '\n')
                self.assertEqual(sniff.port_speed(port, sysfs), want)
            (sysfs / '2-4').mkdir()
            (sysfs / '2-4' / 'speed').write_text('5000\n')
            with self.assertRaises(sniff.Refused):
                sniff.port_speed('2-4', sysfs)
            with self.assertRaises(sniff.Refused):
                sniff.port_speed('3-9', sysfs)


def fake_sysfs(root, units):
    """units: (port, serial, dev, busnum, devnum[, vid:pid]); serial None leaves the file out."""
    root.mkdir(exist_ok=True)
    for port, serial, dev, busnum, devnum, *ids in units:
        vid, pid = (ids[0] if ids else '6666:6620').split(':')
        d = root / port
        d.mkdir()
        for name, value in (('idVendor', vid), ('idProduct', pid), ('dev', dev),
                            ('busnum', busnum), ('devnum', devnum), ('serial', serial)):
            if value is not None:
                (d / name).write_text(value + '\n')
    return root


ONE = [('5-2.2', 'S1', '1:3', '5', '59')]                       # 1:3 is /dev/null, so tests can hold "the unit"
TWO = ONE + [('5-2.7', 'S2', '1:5', '5', '93')]                 # 1:5 is /dev/zero


def hold(test, path):
    """pid of a process holding `path` open until the test ends."""
    with open(path, 'rb') as f:
        p = subprocess.Popen(['sleep', '30'], stdin=f, stdout=f, stderr=f)
    test.addCleanup(lambda: (p.kill(), p.wait()))
    return str(p.pid)


def fake_units(units):
    with tempfile.TemporaryDirectory() as d:
        sysfs = fake_sysfs(Path(d) / 'sys', [*units, ('5-3', None, '189:600', '5', '89', '1366:0105')])
        (sysfs / '5-2.2:1.0').mkdir(exist_ok=True)                    # an interface: no idVendor
        with mock.patch.object(sniff, 'SYSFS', sysfs):
            return sniff.units()


class SelectTest(unittest.TestCase):
    def test_units_are_the_6666_6620_devices_with_their_usbfs_node(self):
        self.assertEqual(fake_units(TWO), [sniff.Unit('S1', '5-2.2', '1:3', Path('/dev/bus/usb/005/059')),
                                           sniff.Unit('S2', '5-2.7', '1:5', Path('/dev/bus/usb/005/093'))])
        with self.assertRaises(sniff.Refused) as e:
            fake_units([])
        self.assertIn('no sniffer (6666:6620) attached', str(e.exception))

    def test_one_unit_needs_no_serial_and_several_refuse_without_one(self):
        one, two = fake_units(ONE), fake_units(TWO)
        self.assertEqual(sniff.select(one, None).serial, 'S1')
        self.assertEqual(sniff.select(two, 'S2').port, '5-2.7')
        for attached, serial, why in ((two, None, '2 sniffers attached, give --serial: S1 at 5-2.2; S2 at 5-2.7'),
                                      (one, 'S9', 'no sniffers with serial S9'),
                                      (fake_units([ONE[0], ('5-2.7', 'S1', '1:5', '5', '93')]), 'S1',
                                       '2 sniffers with serial S1')):
            with self.assertRaises(sniff.Refused) as e:
                sniff.select(attached, serial)
            self.assertIn(why, str(e.exception))

    def test_a_unit_without_a_serial_is_used_alone_and_named_among_several(self):
        alone = fake_units([('5-2.2', None, '1:3', '5', '59')])
        self.assertEqual(str(sniff.select(alone, None)), '(no serial) at 5-2.2')
        attached = fake_units([('5-2.2', None, '1:3', '5', '59'), TWO[1]])
        with self.assertRaises(sniff.Refused) as e:
            sniff.select(attached, None)
        self.assertIn('(no serial) at 5-2.2; S2 at 5-2.7', str(e.exception))
        self.assertEqual(sniff.select(attached, 'S2').port, '5-2.7')


@unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc')
class HolderTest(unittest.TestCase):
    """Real processes holding real char devices; /dev/null and /dev/zero stand in for two units."""

    def setUp(self):
        self.attached = fake_units(TWO)

    def test_a_capture_on_the_unit_is_busy_and_one_on_another_unit_is_not(self):
        pid = hold(self, '/dev/null')
        holds = sniff.held([pid])
        self.assertEqual(sniff.holder(self.attached[0], self.attached, holds), ('busy', pid))
        self.assertEqual(sniff.holder(self.attached[1], self.attached, holds), ('idle', None))

    def test_a_process_holding_no_attached_unit_is_unknown(self):
        with tempfile.NamedTemporaryFile() as f:
            pid = hold(self, f.name)
            self.assertEqual(sniff.holder(self.attached[1], self.attached, sniff.held([pid])), ('unknown', pid))

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads every process')
    def test_a_process_whose_fds_cannot_be_read_is_unknown(self):
        holds = sniff.held(['1'])
        self.assertEqual(holds, {'1': None})
        self.assertEqual(sniff.holder(self.attached[0], self.attached, holds), ('unknown', '1'))

    def test_a_process_gone_during_the_look_is_skipped(self):
        p = subprocess.Popen(['true'])
        p.wait()
        self.assertEqual(sniff.held([str(p.pid)]), {})

    def test_list_names_each_unit_and_its_holder(self):
        pid = hold(self, '/dev/null')
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sniff, 'SYSFS', fake_sysfs(Path(d) / 'sys', TWO)), \
                mock.patch.object(sniff, 'running', return_value=[pid]), contextlib.redirect_stdout(out):
            sniff.list_units(None)
        self.assertEqual(out.getvalue(), f'S1\t5-2.2\t/dev/bus/usb/005/059\tbusy {pid}\n'
                                         'S2\t5-2.7\t/dev/bus/usb/005/093\tidle\n')


class UsbfsRoomTest(unittest.TestCase):
    def room(self, limit, others):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'limit').write_text(f'{limit}\n')
            with mock.patch.object(sniff, 'USBFS_LIMIT', Path(d) / 'limit'):
                sniff.usbfs_room(others)

    def test_one_capture_fits_the_default_limit_and_a_second_needs_it_raised(self):
        self.room(16, 0)
        self.room(0, 3)    # 0 is no limit
        self.room(32, 1)
        for limit, others in ((15, 0), (31, 1), (127, 7)):
            with self.assertRaises(sniff.Refused, msg=(limit, others)):
                self.room(limit, others)
        with self.assertRaises(sniff.Refused) as e:
            self.room(16, 1)
        self.assertIn('1 other capture(s) running; with this one usb_sniffer needs about 32 MiB', str(e.exception))
        self.assertIn('usbcore.usbfs_memory_mb is 16: `echo 32 | sudo tee', str(e.exception))


# runs sniff.py's main with its host paths replaced: argv is SYSFS USBFS USBFS_LIMIT ARGS...
RUNNER = ('import sys; from pathlib import Path; sys.path.insert(0, %r); import sniff; '
          'sniff.SYSFS, sniff.USBFS, sniff.USBFS_LIMIT = map(Path, sys.argv[1:4]); '
          'sys.argv = ["sniff.py", *sys.argv[4:]]; sys.exit(sniff.main())') % str(SCRIPT.parent)


def userns_works():
    try:
        sniff.userns()
        return True
    except sniff.Refused:
        return False


@unittest.skipUnless(sys.platform.startswith('linux') and userns_works(),
                     'needs unprivileged user and mount namespaces')
class PinnedTest(unittest.TestCase):
    """The real namespace and mounts: /dev/null is the unit's node, a regular file the other unit's."""

    def run_pinned(self, dev='1:3', serial='S1', sysfs_serial='S1', tool=('sh', '-c')):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sysfs = fake_sysfs(d / 'sys', [('5-2.2', sysfs_serial, '1:3', '5', '59')])
            (d / 'usb' / '005').mkdir(parents=True)
            (d / 'usb' / '005' / '059').symlink_to('/dev/null')
            (d / 'usb' / '005' / '093').write_text('the other unit')
            (d / 'usb' / '001').mkdir()
            before = Path('/proc/self/mountinfo').read_text()
            probe = f'ls -A {d}/usb {d}/usb/005; echo data > {d}/usb/005/059 && echo wrote; touch {d}/ran'
            r = subprocess.run([*sniff.UNSHARE, sys.executable, '-I', '-c', RUNNER, str(sysfs), str(d / 'usb'), '-',
                                '_pinned', serial, '5-2.2', dev, str(d / 'usb' / '005' / '059'), *tool, probe],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(Path('/proc/self/mountinfo').read_text(), before)
            self.assertEqual(sorted(p.name for p in (d / 'usb' / '005').iterdir()), ['059', '093'])
            self.ran = (d / 'ran').exists()
        return r

    def test_only_the_units_node_is_left_and_it_is_the_device(self):
        for kw in ({}, {'serial': '', 'sysfs_serial': None}):   # a unit without a serial is pinned by its node too
            r = self.run_pinned(**kw)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(self.ran, kw)
            listing = r.stdout.splitlines()
            self.assertEqual([l for l in listing if l and not l.endswith(':')], ['005', '059', 'wrote'])

    def test_a_node_or_sysfs_entry_that_changed_never_runs_the_tool(self):
        for kw, why in (({'dev': '1:5'}, 'is no longer its usbfs node 1:5'),
                        ({'sysfs_serial': 'S2'}, 'sniffer at 5-2.2 changed: serial S2'),
                        ({'serial': '', 'sysfs_serial': None, 'dev': '1:5'}, 'is no longer its usbfs node 1:5'),
                        ({'tool': ('/nonexistent/usb_sniffer',)},
                         'cannot run /nonexistent/usb_sniffer: No such file or directory')):
            r = self.run_pinned(**kw)
            self.assertEqual(r.returncode, sniff.PIN_FAILED, kw)
            self.assertIn(why, r.stderr)
            self.assertNotIn('Traceback', r.stderr)
            self.assertFalse(self.ran, kw)


class SetAddressesTest(unittest.TestCase):
    def test_groups_frames_by_address_and_skips_blank_values(self):
        self.assertEqual(sniff.set_addresses('12\t9\n40\t\n77\t9\n90\t12\n'), {9: [12, 77], 12: [90]})

    def test_address_filter_is_anchored_so_9_does_not_match_19(self):
        import re
        pattern = re.search(r'matches "([^"]+)"', sniff.addr_filter(9)).group(1)
        self.assertTrue(re.search(pattern, '9.0'))
        self.assertFalse(re.search(pattern, '19.0'))
        self.assertFalse(re.search(pattern, '29.1'))


DATA = ROOT / 'tests' / 'data' / 'usb_sniffer'   # slices of real captures of an rp2040 enumerating at FS


@unittest.skipIf(os.name == 'nt', 'capture harness requires POSIX process semantics')
@unittest.skipUnless(shutil.which('tshark') and shutil.which('editcap'), 'needs Wireshark\'s tshark and editcap')
class CaptureTest(unittest.TestCase):
    """Only the hardware is a stub: usb_sniffer writes a real capture and, like the
    tool, runs on unless --limit ends it; pgrep is a stub too, and so is unshare: it
    skips _pinned (PinnedTest runs the real one) and execs the tool. tshark and editcap are real."""

    def run_cli(self, args, wrote='enum_fs.pcapng', sniffer_rc=0, pids='', pgrep_rc=None, existing=False,
                without=(), signal_after=None, units=ONE, unshare='', setup_delay=0, usbfs_limit=16):
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / 'bin'
            b.mkdir()
            (Path(d) / 'usbfs_memory_mb').write_text(f'{usbfs_limit}\n')
            sysfs = fake_sysfs(Path(d) / 'sys', units)
            (b / 'unshare').write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {d}/unshare_argv\n{unshare}\n'
                                       'while [ "$1" != -- ]; do shift; done; shift\n'
                                       f'if [ "$4" = _pinned ]; then shift 8; echo $$ > {d}/setup_pid; '
                                       f'sleep {setup_delay} </dev/null >/dev/null 2>&1; fi\n'
                                       'exec "$@"\n')
            if existing:
                (Path(d) / 'c.pcapng').write_text('old capture')
            (b / 'usb_sniffer').write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {d}/argv\necho $$ > {d}/pid\n'
                                           f'while [ "$1" != --fifo ]; do shift; done; cat "{DATA / wrote}" > "$2"\n'
                                           'case "$*" in *--limit*) ;; *) exec sleep 30 </dev/null >/dev/null 2>&1 ;; esac\n'
                                           f'exit {sniffer_rc}\n')
            (b / 'pgrep').write_text(f'#!/bin/sh\nprintf "{pids}"; [ -n "{pids}" ]\n' if pgrep_rc is None else
                                     f'#!/bin/sh\necho "no /proc" >&2; exit {pgrep_rc}\n')
            for f in b.iterdir():
                f.chmod(0o755)
            if 'unshare' in without:
                (b / 'unshare').unlink()
            for name in ('tshark', 'editcap', 'sh', 'cat', 'sleep', 'printf'):
                if name not in without and shutil.which(name):
                    (b / name).symlink_to(shutil.which(name))
            cmd = [sys.executable, '-c', RUNNER, str(sysfs), '/dev/bus/usb', str(Path(d) / 'usbfs_memory_mb'),
                   *[a.replace('$D', d) for a in args]]
            if signal_after is None:
                r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, PATH=str(b)), timeout=60)
            else:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env=dict(os.environ, PATH=str(b)))
                deadline = time.time() + 10
                while not (Path(d) / ('setup_pid' if setup_delay else 'pid')).exists() and time.time() < deadline:
                    time.sleep(0.05)
                time.sleep(signal_after)
                p.send_signal(signal.SIGTERM)
                out, err = p.communicate(timeout=30)
                r = subprocess.CompletedProcess(cmd, p.returncode, out, err)
                self.setup_pid = int((Path(d) / 'setup_pid').read_text()) if setup_delay else None
                self.tool_pid = int((Path(d) / 'pid').read_text()) if (Path(d) / 'pid').exists() else None
            argv = (Path(d) / 'argv').read_text().split() if (Path(d) / 'argv').exists() else None
            self.unshare_argv = (Path(d) / 'unshare_argv').read_text().splitlines() \
                if (Path(d) / 'unshare_argv').exists() else None
            cap = Path(d) / 'c.pcapng'
            self.kept = cap.read_bytes() if cap.exists() else None
            self.leftovers = sorted(p.name for p in Path(d).iterdir()
                                    if p.name not in ('bin', 'sys', 'argv', 'pid', 'unshare_argv', 'setup_pid',
                                                      'usbfs_memory_mb', 'c.pcapng'))
        return r, argv

    def test_a_packet_limit_ends_the_capture_and_usb_packets_are_told_from_records(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '20', '--limit', '5'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(argv[-4:], ['--speed', 'fs', '--limit', '5'])
        self.assertIn('52 USB packets, 9 sniffer records', r.stdout)
        self.assertIn('sniffer S1 at 5-2.2, ended by --limit 5', r.stdout)
        self.assertEqual(self.kept, (DATA / 'enum_fs.pcapng').read_bytes())

    def test_the_tool_runs_pinned_to_the_selected_unit(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '20', '--limit', '5',
                                '--serial', 'S2'], units=TWO)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('(sniffer S2 at 5-2.7, at most 20 s)', r.stdout)
        launch = [*sniff.UNSHARE[1:], sys.executable, '-I', str(SCRIPT), '_pinned', 'S2', '5-2.7', '1:5',
                  '/dev/bus/usb/005/093', 'usb_sniffer', *argv]
        self.assertEqual(self.unshare_argv[-len(launch):], launch)

    def test_several_units_need_a_serial_and_a_failed_pin_is_no_capture(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '5'], units=TWO)
        self.assertEqual(r.returncode, 1)
        self.assertIn('2 sniffers attached, give --serial: S1 at 5-2.2; S2 at 5-2.7', r.stderr)
        self.assertIsNone(self.unshare_argv)
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '5'],
                               unshare='case "$*" in *_pinned*) exit 125 ;; esac')
        self.assertEqual(r.returncode, 1)
        self.assertIn('usb_sniffer never ran: pinning it to sniffer S1 at 5-2.2 failed', r.stderr)
        self.assertIsNone(argv)

    def test_a_capture_on_another_unit_needs_room_for_both_in_the_usbfs_limit(self):
        other = hold(self, '/dev/zero')   # /dev/zero is S2's fake node: a capture running on S2
        args = ['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '20', '--limit', '5', '--serial', 'S1']
        r, argv = self.run_cli(args, units=TWO, pids=f'{other}\\n')
        self.assertEqual(r.returncode, 1)
        self.assertIn('1 other capture(s) running; with this one usb_sniffer needs about 32 MiB', r.stderr)
        self.assertIsNone(argv)
        r, argv = self.run_cli(args, units=TWO, pids=f'{other}\\n', usbfs_limit=32)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_host_without_user_namespaces_is_refused_before_the_tool_runs(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '5'],
                               unshare='echo "unshare: unshare failed: Operation not permitted" >&2; exit 1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot create a user and mount namespace (unshare: unshare failed: Operation not permitted)',
                      r.stderr)
        self.assertIsNone(argv)

    def test_a_term_during_setup_means_the_tool_never_starts(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '25'], signal_after=0.5,
                               setup_delay=1)
        self.assertNotEqual(r.returncode, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(self.setup_pid, 0)
        self.assertIsNone(self.tool_pid)
        self.assertIsNone(argv)

    def test_other_platforms_are_refused_before_any_tool_runs(self):
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / 'bin'
            b.mkdir()
            (b / 'usb_sniffer').write_text(f'#!/bin/sh\ntouch {d}/launched\n')
            (b / 'usb_sniffer').chmod(0o755)
            code = (f'import sys, runpy; sys.platform = "darwin"; sys.argv = ["sniff.py", "capture", "{d}/c.pcapng", '
                    f'"--speed", "fs", "--seconds", "1"]; runpy.run_path({str(SCRIPT)!r}, run_name="__main__")')
            r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                               env=dict(os.environ, PATH=f'{b}:{os.environ["PATH"]}'), timeout=60)
            self.assertEqual(r.returncode, 1)
            self.assertIn('Linux-only', r.stderr)
            self.assertNotIn('Traceback', r.stderr)
            self.assertFalse((Path(d) / 'launched').exists())
            self.assertFalse((Path(d) / 'c.pcapng').exists())

    def test_capture_refuses_before_the_tool_runs(self):
        holding = hold(self, '/dev/null')   # /dev/null is the fake unit's node
        for args, why, pids in (
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5', '--limit', '-1'], '--limit must be positive', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5', '--limit', '0'], '--limit must be positive', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', 'inf'], '--seconds must be positive and finite', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '0'], '--seconds must be positive and finite', ''),
                (['capture', '$D/c.pcapng', '--seconds', '5'], 'exactly one of', ''),
                (['capture', '$D/c.pcapng', '--seconds', '5', '--speed', 'hs', '--port', '3-2.7'], 'exactly one of', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5'],
                 f'sniffer S1 at 5-2.2 is in use by usb_sniffer pid {holding}', f'{holding}\\n')):
            r, argv = self.run_cli(args, pids=pids)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(why, r.stderr)
            self.assertIsNone(argv, args)

    def test_a_packet_limit_alone_is_no_bound(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '5'])
        self.assertEqual(r.returncode, 2)
        self.assertIn('--seconds', r.stderr)
        self.assertIsNone(argv)

    def test_a_missing_helper_is_refused_before_anything_is_captured(self):
        for helper in ('tshark', 'editcap', 'unshare'):
            r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '5'], without=(helper,))
            self.assertEqual(r.returncode, 1, helper)
            self.assertIn(f'{helper} not on PATH', r.stderr)
            self.assertNotIn('Traceback', r.stderr)
            self.assertIsNone(argv, helper)
        r, _ = self.run_cli(['addr', str(DATA / 'enum_fs.pcapng')], without=('tshark',))
        self.assertEqual(r.returncode, 1)
        self.assertIn('tshark not found', r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_capture_refuses_an_existing_destination_and_a_failing_pgrep(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5'], existing=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('c.pcapng exists', r.stderr)
        self.assertIsNone(argv)
        self.assertEqual(self.kept, b'old capture')
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5'], pgrep_rc=2)
        self.assertEqual(r.returncode, 1)
        self.assertIn('pgrep failed (2): no /proc', r.stderr)
        self.assertIsNone(argv)

    def test_tool_failure_is_the_result(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '5', '--limit', '5'], sniffer_rc=3)
        self.assertEqual(r.returncode, 1)
        self.assertIn('usb_sniffer exited 3', r.stderr)

    def test_the_time_bound_kills_the_tool_and_keeps_the_whole_packets_of_a_cut_file(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '0.5'], wrote='enum_fs_cut.pcapng')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('--limit', argv)
        self.assertIn('51 USB packets, 9 sniffer records', r.stdout)      # the cut packet is gone
        self.assertIn('ended by time bound 0.5 s', r.stdout)
        self.assertEqual(self.leftovers, [])
        with tempfile.TemporaryDirectory() as d:                           # and what is left reads cleanly
            (Path(d) / 'c.pcapng').write_bytes(self.kept)
            self.assertEqual(sniff.contents(Path(d) / 'c.pcapng')[0], 51)

    def test_a_tap_with_no_usb_packet_fails_and_keeps_the_line_state(self):
        r, _ = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '0.5'], wrote='linestate_only.pcapng')
        self.assertEqual(r.returncode, 1)
        self.assertIn('0 USB packets, 5 sniffer records', r.stdout)
        self.assertIn('no USB packets on the tap', r.stderr)
        self.assertIn('Line state: SE0', r.stderr)
        self.assertIsNotNone(self.kept)

    def test_a_trigger_that_never_fired_is_not_a_capture(self):
        args = ['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '0.5', '--trigger', 'falling']
        r, argv = self.run_cli(args, wrote='trigger_never_fired.pcapng')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(argv[-2:], ['--trigger', 'falling'])
        self.assertIn('--trigger falling never fired within 0.5 s', r.stderr)
        self.assertIn('Waiting for a trigger', r.stderr)
        r, _ = self.run_cli(args, wrote='trigger_fired.pcapng')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('51 USB packets', r.stdout)

    def test_a_term_takes_the_tool_down_too(self):
        r, _ = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '25'], signal_after=0.5)
        self.assertNotEqual(r.returncode, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(self.tool_pid, 0)


@unittest.skipIf(os.name == 'nt', 'tshark stub requires POSIX executable semantics')
class AddrTest(unittest.TestCase):
    def run_cli(self, args, tshark=''):
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / 'bin'
            b.mkdir()
            (b / 'tshark').write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {d}/tshark_argv; printf "{tshark}"\n')
            (b / 'tshark').chmod(0o755)
            r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                               env=dict(os.environ, PATH=f'{b}:{os.environ["PATH"]}'))
            self.tshark_argv = (Path(d) / 'tshark_argv').read_text().splitlines()
        return r, None

    @unittest.skipUnless(shutil.which('tshark'), 'needs tshark')
    def test_addr_reads_set_address_out_of_a_real_capture(self):
        r = subprocess.run([sys.executable, str(SCRIPT), 'addr', str(DATA / 'enum_fs.pcapng')], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '9\tframes 50\tfilter: usbll.src matches "^9[.]" || usbll.dst matches "^9[.]"\n')

    def test_addr_lists_every_device_and_flags_several(self):
        r, _ = self.run_cli(['addr', 'c.pcapng'], tshark='100\\t9\\n2000\\t12\\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '9\tframes 100\tfilter: usbll.src matches "^9[.]" || usbll.dst matches "^9[.]"\n'
                                   '12\tframes 2000\tfilter: usbll.src matches "^12[.]" || usbll.dst matches "^12[.]"\n')
        self.assertIn('2 devices enumerated', r.stderr)
        self.assertEqual(self.tshark_argv[:4], ['-r', 'c.pcapng', '-Y', 'usb.setup.bRequest == 5 && usb.bmRequestType == 0x00'])
        self.assertEqual(self.tshark_argv[-4:], ['-e', 'frame.number', '-e', 'usb.device_address'])

    def test_addr_refuses_a_capture_without_set_address(self):
        r, _ = self.run_cli(['addr', 'c.pcapng'])
        self.assertEqual(r.returncode, 1)
        self.assertIn('no SET_ADDRESS', r.stderr)


if __name__ == '__main__':
    unittest.main()
