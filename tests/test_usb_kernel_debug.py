"""Tests for the usb-kernel-debug scripts: usbcap.py's bus resolution refuses
to guess between buses and reports tshark failures explicitly; usb_dyndbg.sh
reads the print flag from a fixture control file and helps without debugfs."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'usb-kernel-debug' / 'scripts' / 'usbcap.py'
DYNDBG = SCRIPT.with_name('usb_dyndbg.sh')
spec = importlib.util.spec_from_file_location('usbcap', SCRIPT)
usbcap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usbcap)
IS_ROOT = hasattr(os, 'geteuid') and os.geteuid() == 0

LSUSB = """\
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 001 Device 004: ID 046d:c52b Logitech, Inc. Unifying Receiver
Bus 001 Device 005: ID 046d:c077 Logitech, Inc. M105 Optical Mouse
Bus 003 Device 007: ID cafe:4010 TinyUSB TinyUSB usbtest
Bus 003 Device 009: ID cafe:4001 TinyUSB TinyUSB CDC MSC
Bus 005 Device 012: ID cafe:4010 TinyUSB TinyUSB usbtest
Bus 005 Device 013: ID 1366:0101 SEGGER J-Link PLUS
"""


class ResolveTest(unittest.TestCase):
    def test_bus_numbers_and_auto_never_run_lsusb(self):
        never = lambda: self.fail('lsusb must not run')
        self.assertEqual(usbcap.resolve('3', never), 3)
        self.assertEqual(usbcap.resolve('0', never), 0)
        self.assertEqual(usbcap.resolve('auto', never), 0)

    def test_a_single_match_names_its_bus(self):
        self.assertEqual(usbcap.resolve('046d:c52b', lambda: LSUSB), 1)
        self.assertEqual(usbcap.resolve('1366:', lambda: LSUSB), 5)
        self.assertEqual(usbcap.resolve('CAFE:4001', lambda: LSUSB), 3)

    def test_matches_on_one_bus_are_not_ambiguous(self):
        self.assertEqual(usbcap.resolve('046d:', lambda: LSUSB), 1)

    def test_matches_across_buses_are_refused_with_the_list(self):
        with self.assertRaises(ValueError) as ctx:
            usbcap.resolve('cafe:', lambda: LSUSB)
        self.assertIn('several buses; pass the bus number', str(ctx.exception))
        self.assertIn('bus 3 device 7: cafe:4010', str(ctx.exception))
        self.assertIn('bus 5 device 12: cafe:4010 TinyUSB TinyUSB usbtest', str(ctx.exception))
        with self.assertRaises(ValueError):
            usbcap.resolve('cafe:4010', lambda: LSUSB)

    def test_no_match_and_bad_selector_say_which(self):
        with self.assertRaisesRegex(ValueError, 'no device matching'):
            usbcap.resolve('1a86:8010', lambda: LSUSB)
        for bad in ('cafe', 'cafe:40', 'xyz:1234', '', '3a'):
            with self.assertRaisesRegex(ValueError, 'bad target'):
                usbcap.resolve(bad, lambda: LSUSB)


DATA = Path(__file__).resolve().parent / 'data' / 'usb_kernel_debug'   # real usbmon captures, host metadata stripped


@unittest.skipIf(os.name == 'nt', 'usbmon capture requires Linux')
@unittest.skipUnless(shutil.which('capinfos'), "needs Wireshark's capinfos")
class CliTest(unittest.TestCase):
    """lsusb and the capturing tshark are stubs on PATH; tshark records its arguments and
    writes a real usbmon capture, which the real capinfos counts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / 'tshark.log'
        self._stub('lsusb', f"#!/bin/sh\ncat <<'EOF'\n{LSUSB}EOF\n")
        self.tshark_rc = Path(self.tmp.name) / 'tshark.rc'
        self.tshark_rc.write_text('0')
        self._stub('tshark', '#!/bin/sh\n'
                   f'echo "$@" >> {self.log}\n'
                   f'rc=$(cat {self.tshark_rc})\n'
                   '[ "$rc" = 0 ] || { echo "tshark: The capture session could not be initiated" >&2; exit $rc; }\n'
                   'out=""; while [ $# -gt 0 ]; do [ "$1" = -w ] && out=$2; shift; done\n'
                   f'cat "$(cat {self.tmp.name}/wrote)" > "$out"\n')
        self.wrote('usbmon_msc_poll.pcapng')

    def wrote(self, fixture):
        (Path(self.tmp.name) / 'wrote').write_text(str(DATA / fixture))

    def tearDown(self):
        self.tmp.cleanup()

    def _stub(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def _run(self, *args):
        env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}'}
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def test_capture_resolves_the_bus_and_counts_packets(self):
        out = Path(self.tmp.name) / 'cap.pcapng'
        r = self._run('046d:c52b', '3', str(out), '--snaplen', '128')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('capturing usbmon1 for 3s', r.stdout)
        self.assertIn('(12 packets)', r.stdout)
        self.assertTrue(out.exists())
        self.assertIn(f'-i usbmon1 -a duration:3 -w {out} -s 128', self.log.read_text())

    def test_an_idle_bus_fails_and_keeps_the_empty_capture(self):
        self.wrote('usbmon_idle.pcapng')
        out = Path(self.tmp.name) / 'cap.pcapng'
        r = self._run('1', '3', str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn('no URB on usbmon1 in 3s', r.stderr)
        self.assertNotIn('saved', r.stdout)
        self.assertTrue(out.exists())

    def test_a_count_that_cannot_be_read_is_not_a_success(self):
        self._stub('capinfos', '#!/bin/sh\necho "File name: x"\n')
        r = self._run('1', '3', str(Path(self.tmp.name) / 'cap.pcapng'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('gave no packet count', r.stderr)

    def test_missing_tools_are_named_before_the_outfile_is_reserved(self):
        out = Path(self.tmp.name) / 'cap.pcapng'
        (self.bin / 'tshark').unlink()
        env = {**os.environ, 'PATH': str(self.bin)}
        r = subprocess.run([sys.executable, str(SCRIPT), '046d:c52b', '3', str(out)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn('tshark, capinfos not on PATH', r.stderr)
        self.assertNotIn('Traceback', r.stderr)
        self.assertFalse(out.exists())

    def test_a_tool_that_cannot_be_started_releases_the_outfile(self):
        out = Path(self.tmp.name) / 'cap.pcapng'
        self._stub('tshark', '#!/nonexistent/interpreter\n')
        for tool in ('capinfos',):
            (self.bin / tool).symlink_to(shutil.which(tool))
        env = {**os.environ, 'PATH': str(self.bin)}     # nothing to fall through to
        r = subprocess.run([sys.executable, str(SCRIPT), '1', '1', str(out)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot run tshark', r.stderr)
        self.assertNotIn('Traceback', r.stderr)
        self.assertFalse(out.exists())

    def test_other_platforms_are_refused(self):
        r = subprocess.run([sys.executable, '-c', 'import sys, runpy; sys.platform = "darwin"; '
                            f'sys.argv = ["usbcap.py", "1"]; runpy.run_path({str(SCRIPT)!r}, run_name="__main__")'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('Linux-only', r.stderr)

    def test_ambiguous_selector_captures_nothing(self):
        r = self._run('cafe:')
        self.assertEqual(r.returncode, 1)
        self.assertIn('several buses; pass the bus number', r.stderr)
        self.assertIn('bus 3 device 7', r.stderr)
        self.assertIn('bus 5 device 12', r.stderr)
        self.assertFalse(self.log.exists(), 'tshark must not run')

    def test_missing_device_and_bad_input_fail_before_capture(self):
        self.assertIn('no device matching', self._run('1a86:8010').stderr)
        self.assertIn('bad target', self._run('nope').stderr)
        self.assertIn('seconds must be positive', self._run('3', '0').stderr)
        self.assertFalse(self.log.exists())

    def test_an_existing_outfile_and_a_bad_snaplen_are_refused_before_capture(self):
        out = Path(self.tmp.name) / 'old.pcapng'
        out.write_text('old capture')
        r = self._run('3', '1', str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn('old.pcapng exists', r.stderr)
        self.assertEqual(out.read_text(), 'old capture')
        for snaplen in ('0', '-5'):
            r = self._run('3', '1', str(Path(self.tmp.name) / 'new.pcapng'), '--snaplen', snaplen)
            self.assertEqual(r.returncode, 1, snaplen)
            self.assertIn('--snaplen must be positive', r.stderr)
        self.assertFalse(self.log.exists(), 'tshark must not run')

    def test_the_outfile_is_reserved_before_the_bus_lookup(self):
        out = Path(self.tmp.name) / 'raced.pcapng'
        self._stub('lsusb', f"#!/bin/sh\n[ -f '{out}' ] || exit 1\ncat <<'EOF'\n{LSUSB}EOF\n")
        r = self._run('046d:c52b', '1', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_bytes(), (DATA / 'usbmon_msc_poll.pcapng').read_bytes(), 'the capture is what tshark wrote')
        r = self._run('nope', '1', str(Path(self.tmp.name) / 'refused.pcapng'))
        self.assertEqual(r.returncode, 1)
        self.assertFalse((Path(self.tmp.name) / 'refused.pcapng').exists(), 'a refusal leaves no reserved file')

    def test_tshark_failure_is_reported_with_the_access_hint(self):
        self.tshark_rc.write_text('2')
        out = Path(self.tmp.name) / 'failed.pcapng'
        r = self._run('3', '1', str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn('tshark exited 2', r.stderr)
        self.assertIn('could not be initiated', r.stderr)
        self.assertIn('/dev/usbmon3 access', r.stderr)
        self.assertFalse(out.exists(), 'a failed capture leaves no reserved file behind')
        self._stub('tshark', '#!/bin/sh\nout=""; while [ $# -gt 0 ]; do [ "$1" = -w ] && out=$2; shift; done\n'
                   'echo partial > "$out"; echo "tshark: interface went away" >&2; exit 2\n')
        r = self._run('3', '1', str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'partial capture kept in {out}', r.stderr)
        self.assertEqual(out.read_text(), 'partial\n')

    def test_a_failing_lsusb_is_reported_and_releases_the_outfile(self):
        self._stub('lsusb', '#!/bin/sh\necho "lsusb: cannot open" >&2; exit 2\n')
        out = Path(self.tmp.name) / 'nolsusb.pcapng'
        r = self._run('046d:c52b', '1', str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn('lsusb failed', r.stderr)
        self.assertFalse(out.exists())
        self.assertFalse(self.log.exists(), 'tshark must not run')


CONTROL = '''\
# filename:lineno [module]function flags format
init/main.c:1116 [main]initcall_blacklist =p "blacklisting initcall %s\\n"
drivers/usb/core/hub.c:100 [usbcore]hub_port_init =p "port %d reset\\n"
drivers/usb/core/hub.c:200 [usbcore]hub_events =pfl "hub event\\n"
drivers/usb/core/hub.c:300 [usbcore]hub_quiesce =_ "quiesce\\n"
drivers/usb/host/xhci-hub.c:559 [xhci_hcd]xhci_disable_port =_ "Ignoring request\\n"
drivers/usb/host/xhci-ring.c:10 [xhci_hcd]xhci_ring =flmt "no print flag\\n"
'''


@unittest.skipIf(os.name == 'nt', 'kernel dynamic debug requires Linux')
class DyndbgTest(unittest.TestCase):
    """The control file is a fixture; the kernel format is file:line [module]function =flags "format"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctl = Path(self.tmp.name) / 'dynamic_debug' / 'control'
        self.ctl.parent.mkdir()
        self.ctl.write_text(CONTROL)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args, ctl=None):
        env = {**os.environ, 'USB_DYNDBG_CTL': str(ctl or self.ctl)}
        return subprocess.run(['bash', str(DYNDBG), *args], capture_output=True, text=True, env=env)

    def test_help_works_without_debugfs(self):
        r = self._run('--help', ctl=Path(self.tmp.name) / 'missing' / 'dynamic_debug' / 'control')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('usbcore xhci_hcd', r.stdout)
        self.assertIn('lsusb -t', r.stdout)
        r = self._run(ctl=Path(self.tmp.name) / 'missing' / 'dynamic_debug' / 'control')
        self.assertEqual(r.returncode, 2, 'no action is a usage error')
        self.assertIn('usage:', r.stderr)

    @unittest.skipIf(IS_ROOT, 'root can traverse mode-000 directories')
    def test_missing_debugfs_and_unreadable_debugfs_are_distinct(self):
        missing = Path(self.tmp.name) / 'missing' / 'dynamic_debug' / 'control'
        missing.parent.parent.mkdir()
        r = self._run('status', ctl=missing)
        self.assertEqual(r.returncode, 1)
        self.assertIn('dynamic_debug unavailable', r.stderr)
        locked = Path(self.tmp.name) / 'locked'
        locked.mkdir(mode=0o000)
        r = self._run('status', ctl=locked / 'dynamic_debug' / 'control')
        locked.chmod(0o700)
        self.assertEqual(r.returncode, 1)
        self.assertIn('run with sudo', r.stderr)

    def test_status_lists_only_print_enabled_sites_of_allowlisted_modules(self):
        r = self._run('status')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual([l.split()[0] for l in lines], ['drivers/usb/core/hub.c:100', 'drivers/usb/core/hub.c:200'])
        self.assertNotIn('[main]', r.stdout, 'a non-allowlisted module is out of scope')
        r = self._run('status', 'xhci_hcd')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), '(no print sites enabled for xhci_hcd)', '=_ and =flmt are not print-enabled')
        r = self._run('status', 'usbcore')
        self.assertIn('=pfl', r.stdout)
        self.assertNotIn('quiesce', r.stdout)

    def test_status_refuses_a_module_outside_the_allowlist_or_more_than_one(self):
        r = self._run('status', 'main')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not allowlisted: main', r.stderr)
        r = self._run('status', 'xhci_hcd', 'usbcore')
        self.assertEqual(r.returncode, 2, 'a second module would be silently ignored otherwise')
        self.assertIn('usage:', r.stderr)

    @unittest.skipIf(IS_ROOT, 'root can read mode-000 files')
    def test_status_reports_an_unreadable_control_file(self):
        self.ctl.chmod(0o000)
        r = self._run('status')
        self.ctl.chmod(0o600)
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot read', r.stderr)

    def test_a_read_error_is_not_reported_as_no_sites(self):
        broken = Path(self.tmp.name) / 'dir' / 'dynamic_debug' / 'control'
        broken.mkdir(parents=True)  # readable by stat, unreadable as a file
        r = self._run('status', ctl=broken)
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot read', r.stderr)
        self.assertNotIn('no print sites', r.stdout)

    def kernel(self, order, obeys=True):
        """Serve the control path like the kernel does: a read lists the sites, a write is a
        command that changes their flags. A FIFO cannot tell which comes next, so the
        script's opens are given in `order` ('r'/'w'); a mismatch times the script out."""
        self.ctl.unlink()
        os.mkfifo(self.ctl)
        lines = CONTROL.splitlines(keepends=True)
        self.commands = []

        def apply(cmd):
            _, module, flag = cmd.split()
            for n, line in enumerate(lines):
                head, _, fmt = line.partition(' "')
                parts = head.split()
                if len(parts) == 3 and parts[1].startswith(f'[{module}]'):
                    flags = parts[2][1:].replace('_', '').replace('p', '')
                    flags = ('p' if flag == '+p' else '') + flags or '_'
                    lines[n] = f'{parts[0]} {parts[1]} ={flags} "{fmt}'

        def held_by_others(path):
            for fd in Path('/proc').glob('[0-9]*/fd/*'):
                try:
                    if fd.parts[2] != str(os.getpid()) and os.readlink(fd) == str(path):
                        return True
                except OSError:
                    pass
            return False

        def serve():
            for op in order:
                if op == 'r':
                    with open(self.ctl, 'w') as f:
                        f.write(''.join(lines))
                    while held_by_others(self.ctl):   # or the next listing lands in this reader
                        time.sleep(0.005)
                else:
                    with open(self.ctl) as f:
                        self.commands.append(f.read())
                    if obeys:
                        apply(self.commands[-1])
        threading.Thread(target=serve, daemon=True).start()

    def _run_served(self, *args):
        env = {**os.environ, 'USB_DYNDBG_CTL': str(self.ctl)}
        return subprocess.run(['bash', str(DYNDBG), *args], capture_output=True, text=True, env=env, timeout=20)

    def test_on_and_off_change_every_site_and_say_how_many(self):
        self.kernel('rwrr' * 2)
        r = self._run_served('on', 'usbcore', 'xhci_hcd')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'dynamic debug on: usbcore (3 of 3 sites print)\n'
                                   'dynamic debug on: xhci_hcd (2 of 2 sites print)\n')
        self.assertEqual(self.commands, ['module usbcore +p\n', 'module xhci_hcd +p\n'])
        self.kernel('rwrr')
        r = self._run_served('off', 'usbcore')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'dynamic debug off: usbcore (0 of 3 sites print)\n')

    def test_a_write_the_kernel_did_not_act_on_is_a_failure(self):
        self.kernel('rwrr', obeys=False)
        r = self._run_served('on', 'xhci_hcd')
        self.assertEqual(r.returncode, 1)
        self.assertIn("wrote '+p' for xhci_hcd, but 0 of its 2 sites print (expected 2)", r.stderr)
        self.assertNotIn('dynamic debug on', r.stdout)

    def test_a_status_whose_second_read_fails_is_an_error_not_a_quiet_module(self):
        b = Path(self.tmp.name) / 'bin'
        b.mkdir()
        calls = Path(self.tmp.name) / 'awk.calls'
        (b / 'awk').write_text(f'#!/bin/sh\necho x >> {calls}\n'
                               f'[ "$(wc -l < {calls})" -ge 2 ] && {{ echo "awk: read error" >&2; exit 2; }}\n'
                               f'exec {shutil.which("awk")} "$@"\n')
        (b / 'awk').chmod(0o755)
        env = {**os.environ, 'USB_DYNDBG_CTL': str(self.ctl), 'PATH': f'{b}:{os.environ["PATH"]}'}
        r = subprocess.run(['bash', str(DYNDBG), 'status', 'xhci_hcd'], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('cannot read', r.stderr)
        self.assertNotIn('no print sites enabled', r.stdout)
        self.assertNotIn('integer expression', r.stderr)

    def test_a_module_without_sites_is_not_loaded_not_disabled(self):
        r = self._run('on', 'dwc2')
        self.assertEqual(r.returncode, 1)
        self.assertIn('module dwc2 has no dynamic-debug site: not loaded', r.stderr)
        self.assertEqual(self.ctl.read_text(), CONTROL, 'nothing is written for a module that is not there')
        r = self._run('status', 'dwc2')
        self.assertEqual(r.returncode, 0)
        self.assertIn('module dwc2 has no dynamic-debug site', r.stdout)

    def test_a_module_outside_the_allowlist_stops_everything(self):
        r = self._run('on', 'usbcore', 'ext4')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not allowlisted: ext4', r.stderr)
        self.assertEqual(self.ctl.read_text(), CONTROL, 'nothing written when any module is refused')
        self.assertEqual(self._run('on').returncode, 2)


if __name__ == '__main__':
    unittest.main()
