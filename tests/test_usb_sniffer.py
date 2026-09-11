"""Tests for the usb-sniffer skill's sniff.py: capture refuses to guess the
speed or run unbounded, derives the speed from sysfs, and reports a leftover
capture; addr reads SET_ADDRESS out of a capture and names every device
when several enumerated."""
import os
import subprocess
import sys
import tempfile
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


class CliTest(unittest.TestCase):
    """usb_sniffer, tshark, capinfos and pgrep are stubs on PATH."""

    def run_cli(self, args, tshark='', sniffer_rc=0, pids='', capinfos_rc=0, pgrep_rc=None, existing=False):
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / 'bin'
            b.mkdir()
            if existing:
                (Path(d) / 'c.pcapng').write_text('old capture')
            (b / 'usb_sniffer').write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {d}/argv\n'
                                           'while [ "$1" != --fifo ]; do shift; done; echo data > "$2"\n'
                                           'case "$*" in *--limit*) ;; *) sleep 30 </dev/null >/dev/null 2>&1 ;; esac\n'  # no packet bound: runs until killed
                                           f'exit {sniffer_rc}\n')
            (b / 'editcap').write_text(f'#!/bin/sh\necho repaired > "$2"; echo "$1 $2" > {d}/editcap_argv\n')
            (b / 'tshark').write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {d}/tshark_argv; printf "{tshark}"\n')
            (b / 'capinfos').write_text(f'#!/bin/sh\necho "Number of packets:   42"; echo "bad capture" >&2; exit {capinfos_rc}\n')
            (b / 'pgrep').write_text(f'#!/bin/sh\nprintf "{pids}"; [ -n "{pids}" ]\n' if pgrep_rc is None else
                                     f'#!/bin/sh\necho "no /proc" >&2; exit {pgrep_rc}\n')
            for f in b.iterdir():
                f.chmod(0o755)
            env = dict(os.environ, PATH=f'{b}:{os.environ["PATH"]}')
            r = subprocess.run([sys.executable, str(SCRIPT), *[a.replace('$D', d) for a in args]],
                               capture_output=True, text=True, env=env)
            argv = (Path(d) / 'argv').read_text().split() if (Path(d) / 'argv').exists() else None
            self.tshark_argv = (Path(d) / 'tshark_argv').read_text().splitlines() if (Path(d) / 'tshark_argv').exists() else None
            self.repaired = (Path(d) / 'editcap_argv').exists() and (Path(d) / 'c.pcapng').read_text() == 'repaired\n'
        return r, argv

    def test_capture_runs_the_tool_bounded_and_counts_packets(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '5'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(argv[-4:], ['--speed', 'hs', '--limit', '5'])
        self.assertIn('42 packets', r.stdout)

    def test_capture_refuses_unbounded_both_speed_sources_and_a_leftover_process(self):
        for args, why, pids in (
                (['capture', '$D/c.pcapng', '--speed', 'hs'], 'bound the capture', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '-1'], '--limit must be positive', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '0'], '--limit must be positive', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--seconds', 'inf'], '--seconds must be positive and finite', ''),
                (['capture', '$D/c.pcapng', '--limit', '5'], 'exactly one of', ''),
                (['capture', '$D/c.pcapng', '--limit', '5', '--speed', 'hs', '--port', '3-2.7'], 'exactly one of', ''),
                (['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '5'], 'already running (pid 4242)', '4242\\n')):
            r, argv = self.run_cli(args, pids=pids)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(why, r.stderr)
            self.assertIsNone(argv, args)

    def test_capture_refuses_an_existing_destination_and_a_failing_pgrep(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '5'], existing=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('c.pcapng exists', r.stderr)
        self.assertIsNone(argv)
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '5'], pgrep_rc=2)
        self.assertEqual(r.returncode, 1)
        self.assertIn('pgrep failed (2): no /proc', r.stderr)
        self.assertIsNone(argv)

    def test_an_unreadable_capture_is_reported(self):
        r, _ = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'hs', '--limit', '5'], capinfos_rc=2)
        self.assertEqual(r.returncode, 1)
        self.assertIn('not a readable capture: bad capture', r.stderr)

    def test_tool_failure_is_the_result(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--limit', '5'], sniffer_rc=3)
        self.assertEqual(r.returncode, 1)
        self.assertIn('usb_sniffer exited 3', r.stderr)
        self.assertFalse(self.repaired)

    def test_a_time_bound_kills_the_tool_and_repairs_the_cut_file_in_place(self):
        r, argv = self.run_cli(['capture', '$D/c.pcapng', '--speed', 'fs', '--seconds', '0.5'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('--limit', argv)
        self.assertTrue(self.repaired)
        self.assertIn('42 packets', r.stdout)

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
