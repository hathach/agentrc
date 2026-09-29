"""Tests for the target-debug skill's rp2040_verify.py through a fake OpenOCD and
nm on PATH. They prove the plumbing only: the batch's hardware behaviour is
verified on a board, recorded in the tinyusb project note."""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts' / 'rp2040_verify.py'
_spec = importlib.util.spec_from_file_location('rp2040_verify', SCRIPT)
rp2040_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp2040_verify)

# Prints the nm table in FAKE_NM (default: tud_task_ext in flash, a RAM function).
FAKE_NM = '''#!/usr/bin/env python3
import os, sys
if os.environ.get('FAKE_NM_FAIL'):
    print('nm: bad elf', file=sys.stderr); sys.exit(1)
print(os.environ.get('FAKE_NM', '10002be1 T tud_task_ext\\n20000215 T get_bootsel_button'))
'''

# Saves its argv to FAKE_ARGV, prints the lines in FAKE_OUT and exits FAKE_RC.
FAKE_OPENOCD = '''#!/usr/bin/env python3
import json, os, sys
open(os.environ['FAKE_ARGV'], 'w').write(json.dumps(sys.argv[1:]))
print(os.environ.get('FAKE_OUT', '').replace('|', '\\n'))
sys.exit(int(os.environ.get('FAKE_RC', '0')))
'''

VERIFIED = 'Info : something|RESULT verified 31136 bytes in 0.043000s|DHCSR 0x01000001'


@unittest.skipIf(os.name == 'nt', 'OpenOCD and nm stubs require POSIX executable semantics')
class Rp2040VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        for name, body in (('nm', FAKE_NM), ('openocd', FAKE_OPENOCD)):
            (d / name).write_text(body)
            (d / name).chmod((d / name).stat().st_mode | stat.S_IEXEC)
        self.env = dict(os.environ, RP2040_VERIFY_NM=str(d / 'nm'), RP2040_VERIFY_OPENOCD=str(d / 'openocd'),
                        FAKE_ARGV=str(d / 'argv.json'))
        self.argv_file = d / 'argv.json'

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, symbol='tud_task_ext', elf=None, probe='P1', **env):
        e = dict(self.env, **env)
        if elf is None:
            elf = str(Path(self.tmp.name) / 'fw.elf')
            Path(elf).write_bytes(b'')
        r = subprocess.run([sys.executable, str(SCRIPT), '--probe', probe, '--interface-cfg', 'interface/cmsis-dap.cfg',
                            '--speed', '5000', '--elf', elf, '--symbol', symbol],
                           capture_output=True, text=True, env=e, timeout=60)
        out = json.loads(r.stdout) if r.stdout.strip() else None
        return r.returncode, out

    def test_verified_and_running_is_exit_0(self):
        rc, out = self.run_script(FAKE_OUT=VERIFIED)
        self.assertEqual(rc, 0)
        self.assertTrue(out['running'])
        self.assertEqual(out['address'], '0x10002be0', 'the Thumb bit is dropped')

    def test_batch_owns_one_breakpoint_checks_pc_and_resumes_outside_the_catch(self):
        self.run_script(FAKE_OUT=VERIFIED)
        argv = json.loads(self.argv_file.read_text())
        self.assertLess(argv.index('set USE_CORE 0'), argv.index('target/rp2040.cfg'))
        batch = argv[-1]
        self.assertIn('bp 0x10002be0 2 hw', batch)
        self.assertIn('rbp 0x10002be0', batch)
        self.assertNotIn('rbp all', batch)
        self.assertIn('$pc != 0x10002be0', batch)
        self.assertIn('0x03000000', batch)
        catch_end = batch.index('} msg]')
        self.assertGreater(batch.index('if {[catch {resume} e]}'), catch_end)
        self.assertGreater(batch.index('read_memory 0xE000EDF0'), catch_end)

    def test_breakpoint_not_reached_or_mismatch_is_exit_1_when_running(self):
        for result in ('not-at-breakpoint pc=0x20000214', 'mismatch checksum mismatch | diff 0 ...'):
            rc, out = self.run_script(FAKE_OUT=f'RESULT {result}|DHCSR 0x01000001')
            self.assertEqual(rc, 1, result)
            self.assertTrue(out['running'])

    def test_core_left_halted_or_resume_failed_is_exit_3(self):
        rc, out = self.run_script(FAKE_OUT='RESULT verified 1 bytes|DHCSR 0x01030003')
        self.assertEqual(rc, 3)
        self.assertFalse(out['running'])
        rc, out = self.run_script(FAKE_OUT='RESULT verified 1 bytes|RESUME failed not halted|DHCSR 0x01000001')
        self.assertEqual(rc, 3)
        rc, out = self.run_script(FAKE_OUT='RESULT mismatch x|CLEANUP failed rbp: no bp|DHCSR 0x01000001')
        self.assertEqual(rc, 3, 'a failed cleanup step is not a clean mismatch')
        self.assertEqual(out['restoreErrors'], ['rbp: no bp'])

    def test_a_detected_reset_is_exit_3(self):
        rc, out = self.run_script(FAKE_OUT='Info : [rp2040.core0] external reset detected|' + VERIFIED)
        self.assertEqual(rc, 3)
        self.assertTrue(out['resetDetected'])

    def test_tool_error_is_exit_3_even_with_the_core_running(self):
        rc, out = self.run_script(FAKE_OUT='RESULT tool-error no hardware breakpoint available|DHCSR 0x01000001')
        self.assertEqual(rc, 3)
        self.assertTrue(out['running'])

    def test_arguments_outside_the_safe_set_are_refused_before_openocd(self):
        d = Path(self.tmp.name)
        for name in ('fw [x].elf', 'fw$x.elf', 'fw{x}.elf', 'fw x.elf'):
            (d / name).write_bytes(b'')
            self.assertEqual(self.run_script(elf=str(d / name))[0], 2, name)
        self.assertEqual(self.run_script(probe='P1;shutdown')[0], 2)
        self.assertEqual(self.run_script(elf=str(d / 'absent.elf'))[0], 2)
        self.assertFalse(self.argv_file.exists(), 'OpenOCD was never started')

    def test_probe_failure_without_markers_is_exit_3(self):
        rc, out = self.run_script(FAKE_OUT='Error: unable to find a matching CMSIS-DAP device', FAKE_RC='1')
        self.assertEqual(rc, 3)
        self.assertIsNone(out['result'])

    def test_symbol_must_be_unique_and_flash_resident(self):
        self.assertEqual(self.run_script(symbol='get_bootsel_button')[0], 2)
        self.assertEqual(self.run_script(symbol='missing')[0], 2)
        self.assertEqual(self.run_script(FAKE_NM='10000001 T f\n10000011 t f', symbol='f')[0], 2)
        self.assertEqual(self.run_script(FAKE_NM_FAIL='1')[0], 3)


@unittest.skipUnless(shutil.which('openocd'), 'needs OpenOCD for its Tcl interpreter')
class TclBatchTest(unittest.TestCase):
    """The generated batch run by OpenOCD's own interpreter, target commands stubbed."""

    STUBS = {
        'init': '',
        'halt': 'lappend ::calls halt',
        'resume': 'lappend ::calls resume',
        'bp': 'lappend ::calls bp; if {$::bp_fail} {error "no hardware breakpoint available"}',
        'rbp': 'lappend ::calls rbp; if {$::rbp_fail} {error "rbp refused"}',
        'wait_halt': 'if {$::timeout ne 0} {error $::timeout}',
        'target': 'return fakecore',
        'fakecore': 'return $::state',
        'get_reg': 'return [dict create pc $::pc]',
        'verify_image': 'if {$::verify ne ""} {error $::verify}; return "verified 31136 bytes"',
        'read_memory': 'return 0x01000001',
    }

    def run_batch(self, pc='0x10002be0', timeout=0, state='running', verify='', bp_fail=0, rbp_fail=0):
        prelude = f'set ::calls {{}}; set ::pc {pc}; set ::timeout {timeout}; set ::state {state}; set ::verify {{{verify}}}; ' \
                  f'set ::bp_fail {bp_fail}; set ::rbp_fail {rbp_fail}; '
        prelude += ' '.join(f'proc {n} {{args}} {{{body}}};' for n, body in self.STUBS.items())
        batch = rp2040_verify.tcl_batch(0x10002be0, '/tmp/fw.elf', 3000).replace('shutdown', 'echo CALLS:$::calls; shutdown')
        r = subprocess.run(['openocd', '-c', prelude, '-c', batch], capture_output=True, text=True, timeout=60)
        text = r.stdout + r.stderr
        result, dhcsr, restore = rp2040_verify.parse(text)
        calls = [l for l in text.splitlines() if l.startswith('CALLS:')][-1].split(':', 1)[1].split()
        return result, restore, calls

    def test_success_removes_its_breakpoint_and_resumes_once(self):
        result, restore, calls = self.run_batch()
        self.assertEqual(result, 'verified 31136 bytes')
        self.assertEqual(restore, [])
        self.assertEqual(calls, ['halt', 'bp', 'resume', 'rbp', 'resume'])

    def test_timeout_and_wrong_pc_are_not_at_breakpoint(self):
        self.assertTrue(self.run_batch(timeout='{{}}')[0].startswith('not-at-breakpoint '), 'the real timeout error is empty')
        self.assertTrue(self.run_batch(pc='0x20000214')[0].startswith('not-at-breakpoint pc=0x20000214'))

    def test_other_wait_errors_are_tool_errors_and_still_clean_up(self):
        for state in ('unknown', 'reset', 'halted'):
            result, restore, calls = self.run_batch(timeout='{SWD transaction timed out}', state=state)
            self.assertEqual(result, f'tool-error wait_halt: state={state} SWD transaction timed out')
            self.assertEqual(calls[-3:], ['halt', 'rbp', 'resume'])

    def test_differing_bytes_are_a_mismatch_other_verify_errors_a_tool_error(self):
        self.assertTrue(self.run_batch(verify='checksum mismatch\ndiff 0 address 0x13004112')[0].startswith('mismatch '))
        self.assertTrue(self.run_batch(verify="couldn't open /tmp/fw.elf")[0].startswith('tool-error verify_image:'))

    def test_breakpoint_failure_is_a_tool_error_and_still_resumes(self):
        result, restore, calls = self.run_batch(bp_fail=1)
        self.assertTrue(result.startswith('tool-error '))
        self.assertEqual(calls[-1], 'resume')

    def test_failed_breakpoint_removal_is_reported(self):
        result, restore, calls = self.run_batch(rbp_fail=1)
        self.assertTrue(result.startswith('tool-error '))
        self.assertEqual(restore, ['rbp: rbp refused'])
        self.assertEqual(calls[-1], 'resume')


if __name__ == '__main__':
    unittest.main()
