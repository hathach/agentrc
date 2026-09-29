"""Tests for the usb-sniffer skill's sniff.py: capture refuses to guess the
speed or run unbounded, derives the speed from sysfs, and reports a leftover
capture; addr reads SET_ADDRESS out of a capture and names every device
when several enumerated."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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
    tool, runs on unless --limit ends it; pgrep is a stub too. tshark and editcap are real."""

    def run_cli(self, args, wrote='enum_fs.pcapng', sniffer_rc=0, pids='', pgrep_rc=None, existing=False,
                without=(), signal_after=None, unstartable=False):
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / 'bin'
            b.mkdir()
            if existing:
                (Path(d) / 'c.pcapng').write_text('old capture')
            (b / 'usb_sniffer').write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {d}/argv\necho $$ > {d}/pid\n'
                                           f'while [ "$1" != --fifo ]; do shift; done; cat "{DATA / wrote}" > "$2"\n'
                                           'case "$*" in *--limit*) ;; *) exec sleep 30 </dev/null >/dev/null 2>&1 ;; esac\n'
                                           f'exit {sniffer_rc}\n')
            (b / 'pgrep').write_text(f'#!/bin/sh\nprintf "{pids}"; [ -n "{pids}" ]\n' if pgrep_rc is None else
                                     f'#!/bin/sh\necho "no /proc" >&2; exit {pgrep_rc}\n')
            if unstartable:   # found on PATH, but exec fails; PATH holds nothing else to fall back to
                (b / 'usb_sniffer').write_text('#!/nonexistent/interpreter\n')
            for f in b.iterdir():
                f.chmod(0o755)
            for name in ('tshark', 'editcap', 'sh', 'cat', 'sleep', 'printf'):
                if name not in without and shutil.which(name):
                    (b / name).symlink_to(shutil.which(name))
            cmd = [sys.executable, str(SCRIPT), *[a.replace('$D', d) for a in args]]
            if signal_after is None:
                r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, PATH=str(b)), timeout=60)
            else:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env=dict(os.environ, PATH=str(b)))
                deadline = time.time() + 10
                while not (Path(d) / 'pid').exists() and time.time() < deadline:
                    time.sleep(0.05)
                time.sleep(signal_after)
                p.send_signal(signal.SIGTERM)
                out, err = p.communicate(timeout=30)
                r = subprocess.CompletedProcess(cmd, p.returncode, out, err)
                self.tool_pid = int((Path(d) / 'pid').read_text())
            argv = (Path(d) / 'argv').read_text().split() if (Path(d) / 'argv').exists() else None
            cap = Path(d) / 'c.pcapng'
            self.kept = cap.read_bytes() if cap.exists() else None
            self.leftovers = sorted(p.name for p in Path(d).iterdir() if p.name not in ('bin', 'argv', 'pid', 'c.pcapng'))
        return r, argv

    def test_a_packet_limit_ends_the_capture_and_usb_packets_are_told_from_records(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '20', '--limit', '5'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(argv[-4:], ['--speed', 'fs', '--limit', '5'])
        self.assertIn('52 USB packets, 9 sniffer records', r.stdout)
        self.assertIn('ended by --limit 5', r.stdout)
        self.assertEqual(self.kept, (DATA / 'enum_fs.pcapng').read_bytes())

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
        for args, why, pids in (
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5', '--limit', '-1'], '--limit must be positive', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5', '--limit', '0'], '--limit must be positive', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', 'inf'], '--seconds must be positive and finite', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '0'], '--seconds must be positive and finite', ''),
                (['capture', '$D/c.pcapng', '--seconds', '5'], 'exactly one of', ''),
                (['capture', '$D/c.pcapng', '--seconds', '5', '--speed', 'hs', '--port', '3-2.7'], 'exactly one of', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', '5'], 'already running (pid 4242)', '4242\\n')):
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
        for helper in ('tshark', 'editcap'):
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

    def test_a_tool_that_cannot_be_started_is_a_refusal(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '2'], unstartable=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot run usb_sniffer', r.stderr)
        self.assertNotIn('Traceback', r.stderr)

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
