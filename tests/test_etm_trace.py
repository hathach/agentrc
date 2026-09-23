"""Tests for the etm-trace scripts: etm_capture.py inherits its trace config
from an explicit reference Ozone project wherever it is run from and refuses
ambiguous or incomplete inputs; etm_profile.py parses Ozone's code-profile
report and refuses anything else. No Ozone, no hardware."""
import contextlib
import importlib.util
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'etm-trace' / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


capture = load('etm_capture')
profile = load('etm_profile')

UNKNOWN_CODE = ('Addr. %s was traced but is not covered by trace cache. More info: '
                'https://kb.segger.com/Getting_unknown_addresses_in_instruction_trace')

# objdump -h of a pico-sdk image: .data carries RAM-resident code, .bss-like
# .ram_vector_table is copied but is no code, .empty has nothing to read
SECTIONS = '''
fw.elf:     file format elf32-littlearm

Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  0 .text         000048e8  10000000  10000000  00001000  2**3
                  CONTENTS, ALLOC, LOAD, READONLY, CODE
  1 .ram_vector_table 00000110  20000000  10004a4c  00006000  2**2
                  CONTENTS, ALLOC, LOAD, DATA
  2 .data         000028e0  20000110  10004b5c  00006110  2**3
                  CONTENTS, ALLOC, LOAD, CODE
  3 .empty        00000000  20002a00  10007500  00008a00  2**0
                  CONTENTS, ALLOC, LOAD, CODE
'''


def setUpModule():
    """The ELFs here are empty files: section headers come from a stub objdump."""
    d = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(d.cleanup)
    stub = Path(d.name) / 'objdump'
    stub.write_text(f'#!{sys.executable}\nprint({SECTIONS!r})\n')
    stub.chmod(0o755)
    patch = mock.patch.dict(os.environ, ETM_OBJDUMP=str(stub))
    patch.start()
    unittest.addModuleCleanup(patch.stop)

JDEBUG = '''\
void OnProjectLoad (void) {
  // Project.SetDevice ("COMMENTED_OUT");
  Project.SetDevice ("STM32H743XI");
  Project.SetTargetIF ("JTAG");
  Project.SetTIFSpeed ("50 MHz");
  Project.SetTracePortWidth (2);
  Project.SetTraceTiming (100, 100, 100, 100);
  Edit.SysVar (VAR_TRACE_CORE_CLOCK, 200000000);
  File.Open ("$(ProjectDir)/firmware.elf");
}

void AfterTargetReset (void) {
  _SetupTarget();
}

void AfterTargetDownload (void) {
  _SetupTarget();
}

void BeforeTargetConnect (void) {
  // Project.SetJLinkScript ("commented_out.pex");
  Project.SetJLinkScript ("$(ProjectDir)/scripts/trace.pex");
  Target.WriteU32 (0x40021000, 0x1);
}

void _SetupTarget (void) {
  Target.SetReg ("SP", 0);
}

void AfterTargetConnect (void) {
  Target.WriteU32 (0xE0040004, 1);
}
'''

PROFILE = '''\
Code Coverage Summary
Module/Function                | Source Lines     | Instructions
-------------------------------+------------------+------------------
firmware.elf                   |   10 /  20  50.0% |   30 /  60  50.0%
  tud_task                     |    5 /   5 100.0% |   20 /  20 100.0%
  idle_loop                    |    1 /   5  20.0% |    2 /  10  20.0%
  Total                        |   10 /  20  50.0% |   30 /  60  50.0%

Code Profile Summary
Module/Function                | Run Count | Fetch Count
-------------------------------+-----------+------------
firmware.elf                   |           |
  tud_task                     |   1 000   |   20 000
  idle_loop                    |   5 000   |   80 000
  [Unaccounted]                |           |    1 000
  Total                        |   6 000   |  101 000
'''


class ResolveJdebug(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.ref = Path(self._dir.name) / 'ozone' / 'board.jdebug'
        self.ref.parent.mkdir()
        self.ref.write_text(JDEBUG)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir('/')   # nothing is resolved against the cwd or the script's location

    def test_inherits_every_field_from_the_reference(self):
        cfg = capture.resolve_jdebug(str(self.ref))
        self.assertEqual(cfg['device'], 'STM32H743XI')
        self.assertEqual(cfg['target_if'], 'JTAG')
        self.assertEqual(cfg['tif_speed'], '50 MHz')
        self.assertEqual(cfg['port_width'], '2')
        self.assertEqual(cfg['timing'], '100, 100, 100, 100')
        self.assertEqual(cfg['core_clock'], '200000000')
        self.assertEqual(cfg['ref'], str(self.ref))
        # $(ProjectDir) resolves against the reference's own directory
        self.assertEqual(cfg['jlink_script'], str(self.ref.parent / 'scripts' / 'trace.pex'))

    def test_hooks_ride_along_except_the_ones_the_template_owns(self):
        cfg = capture.resolve_jdebug(str(self.ref))
        self.assertIn('_SetupTarget();', cfg['reset_hook'])
        self.assertIn('AfterTargetDownload', cfg['download_hook'])
        self.assertIn('void _SetupTarget (void)', cfg['connect_hook'])
        self.assertIn('void AfterTargetConnect (void)', cfg['connect_hook'])
        for owned in ('OnProjectLoad', 'BeforeTargetConnect'):
            self.assertNotIn(owned, cfg['connect_hook'])

    def refusal(self, text):
        self.ref.write_text(text)
        with self.assertRaises(SystemExit) as cm:
            capture.resolve_jdebug(str(self.ref))
        return str(cm.exception)

    def test_before_connect_keeps_its_statements_and_one_resolved_script_call(self):
        hook = capture.before_connect_hook(capture.resolve_jdebug(str(self.ref)), None)
        self.assertIn('Target.WriteU32 (0x40021000, 0x1);', hook)
        self.assertEqual(hook.count('Project.SetJLinkScript ('), 1)   # the commented-out one is gone
        self.assertIn(f'Project.SetJLinkScript ("{self.ref.parent / "scripts" / "trace.pex"}");', hook)
        self.assertNotIn('$(ProjectDir)', hook)
        self.assertEqual(hook.count('void BeforeTargetConnect'), 1)

    def test_a_guarded_script_call_stays_guarded_and_is_not_hoisted(self):
        self.ref.write_text(JDEBUG.replace('  Project.SetJLinkScript ("$(ProjectDir)/scripts',
                                           '  if (0) Project.SetJLinkScript ("$(ProjectDir)/scripts'))
        hook = capture.before_connect_hook(capture.resolve_jdebug(str(self.ref)), None)
        self.assertEqual(hook.count('Project.SetJLinkScript'), 1)
        self.assertIn(f'if (0) Project.SetJLinkScript ("{self.ref.parent / "scripts" / "trace.pex"}");', hook)

    def test_a_commented_out_function_stays_dead(self):
        self.ref.write_text(JDEBUG + '/*\nvoid AfterTargetHalt (void) {\n  Target.WriteU32 (0x1, 0x2);\n}\n*/\n')
        cfg = capture.resolve_jdebug(str(self.ref))
        self.assertNotIn('AfterTargetHalt', cfg['connect_hook'])
        self.assertNotIn('0x1, 0x2', cfg['connect_hook'])

    def test_a_double_slash_inside_a_path_is_not_a_comment(self):
        self.ref.write_text(JDEBUG.replace('$(ProjectDir)/scripts/trace.pex', '$(ProjectDir)//scripts/trace.pex'))
        self.assertEqual(capture.resolve_jdebug(str(self.ref))['jlink_script'],
                         str(self.ref.parent / 'scripts' / 'trace.pex'))

    def test_the_generated_project_carries_the_interface_and_one_hook(self):
        import argparse
        args = argparse.Namespace(trace_timing=None, core_clock=None, no_timestamps=False,
                                  trace_only=None, profile_lines_csv=False, profile_insts_csv=False,
                                  os_plugin=None, attach=False, sample=None, sample_hz=1000,
                                  power=False, power_hz=10000, jlink_script=None, trace_width=None,
                                  probe_serial='123', tracepoints='', max_inst=1000, elf='/fw.elf')
        proj = capture.gen_project(capture.resolve_jdebug(str(self.ref)), args, self._dir.name)
        text = Path(proj).read_text()
        self.assertIn('Project.SetTargetIF ("JTAG");', text)
        self.assertEqual(text.count('void BeforeTargetConnect'), 1)
        self.assertIn('Target.WriteU32 (0x40021000, 0x1);', text)

    def test_a_script_set_in_onprojectload_lands_in_the_existing_empty_hook(self):
        text = JDEBUG.replace('  Project.SetJLinkScript ("$(ProjectDir)/scripts/trace.pex");\n', '')
        text = text.replace('  Target.WriteU32 (0x40021000, 0x1);\n', '')
        text = text.replace('  File.Open', '  Project.SetJLinkScript ("trace.pex");\n  File.Open')
        self.ref.write_text(text)
        hook = capture.before_connect_hook(capture.resolve_jdebug(str(self.ref)), None)
        self.assertEqual(hook.count('void BeforeTargetConnect'), 1)
        self.assertIn(f'Project.SetJLinkScript ("{self.ref.parent / "trace.pex"}");', hook)

    def test_no_hook_and_no_script_emits_nothing_and_a_cli_script_is_synthesized(self):
        cfg = {'ref': '--device'}
        self.assertEqual(capture.before_connect_hook(cfg, None), '')
        script = str(Path('/abs/demo.pex').resolve())
        hook = capture.before_connect_hook(cfg, script)
        self.assertIn(f'Project.SetJLinkScript ("{script}");', hook)

    def test_a_cli_script_conflicts_with_the_references_own(self):
        cfg = capture.resolve_jdebug(str(self.ref))
        with self.assertRaises(SystemExit) as cm:
            capture.before_connect_hook(cfg, 'other.pex')
        self.assertIn('conflicts', str(cm.exception))

    def test_refuses_a_file_without_a_device_interface_speed_or_width(self):
        self.assertIn('Project.SetDevice', self.refusal('void OnProjectLoad (void) {\n}\n'))
        for call in ('SetTargetIF', 'SetTIFSpeed', 'SetTracePortWidth'):
            text = '\n'.join(ln for ln in JDEBUG.splitlines() if f'Project.{call} ' not in ln)
            self.assertIn(f'no Project.{call}', self.refusal(text + '\n'), call)

    def test_refuses_what_it_cannot_carry_over(self):
        for extra, why in (
                ('int Counter;\n', "'int Counter;'"),
                ('void _Poke (unsigned int Addr) {\n  Target.WriteU32 (Addr, 1);\n}\n', '_Poke'),
                ('int _Read (void) {\n  return 1;\n}\n', '_Read'),
                ('  void _Indented (void) {\n  }\n', '_Indented')):
            self.assertIn(why, self.refusal(JDEBUG + extra), extra)

    def test_refuses_a_function_that_swallows_the_next(self):
        text = JDEBUG.replace('void _SetupTarget (void) {\n  Target.SetReg ("SP", 0);\n}',
                              'void _SetupTarget (void) {\n  Target.SetReg ("SP", 0);\n  }')
        self.assertIn("'_SetupTarget' does not end", self.refusal(text))

    def test_refuses_an_onprojectload_statement_it_would_drop(self):
        for stmt, why in (('Target.WriteU32 (0x40021000, 1);', 'Target.WriteU32'),
                          ('Project.SetTraceSource ("SWO");', 'SetTraceSource'),
                          ('Edit.SysVar (VAR_HSS_SPEED, 100);', 'VAR_HSS_SPEED'),
                          ('if (0) Project.SetJLinkScript ("x.pex");', 'if (0)'),
                          ('Project.SetTracePortWidth (1 << 2);', '1 << 2'),
                          ('Edit.SysVar (VAR_TRACE_CORE_CLOCK, 120 * 1000000);', '120 * 1000000'),
                          ('Project.SetTIFSpeed (Speed);', 'Speed')):
            text = JDEBUG.replace('  File.Open', f'  {stmt}\n  File.Open')
            self.assertIn(why, self.refusal(text), stmt)

    def test_onprojectload_statements_the_template_replaces_are_accepted(self):
        text = JDEBUG.replace('  File.Open', '  Project.SetHostIF ("USB", "123");\n'
                              '  Project.AddSvdFile ("$(InstallDir)/Config/CPU/Cortex-M7.svd");\n'
                              '  Project.SetSWO (4000000, 0);\n  Project.SetTraceSource ("Trace Pins");\n'
                              '  Edit.SysVar (VAR_POWER_SAMPLING_SPEED, FREQ_100_KHZ);\n  File.Open')
        self.ref.write_text(text)
        self.assertEqual(capture.resolve_jdebug(str(self.ref))['device'], 'STM32H743XI')

    def test_refuses_a_script_call_outside_the_two_supported_hooks(self):
        text = JDEBUG.replace('  Target.SetReg ("SP", 0);', '  Project.SetJLinkScript ("x.pex");')
        self.assertIn("inside '_SetupTarget'", self.refusal(text))

    def test_a_quoted_value_is_kept_byte_for_byte(self):
        self.ref.write_text(JDEBUG.replace('  File.Open', '  Project.SetJLinkScript ("trace  probe.pex");\n  File.Open')
                            .replace('  Project.SetJLinkScript ("$(ProjectDir)/scripts/trace.pex");\n', ''))
        self.assertEqual(capture.resolve_jdebug(str(self.ref))['jlink_script'],
                         str(self.ref.parent / 'trace  probe.pex'))

    def test_refuses_a_script_set_in_both_places_even_to_the_same_value(self):
        text = JDEBUG.replace('  File.Open', '  Project.SetJLinkScript ("$(ProjectDir)/scripts/trace.pex");\n  File.Open')
        self.assertIn('more than once', self.refusal(text))

    def test_refuses_conflicting_settings_and_unknown_path_macros(self):
        self.assertIn('target_if more than once', self.refusal(
            JDEBUG.replace('  Project.SetTIFSpeed', '  Project.SetTargetIF ("SWD");\n  Project.SetTIFSpeed')))
        self.assertIn('cannot resolve the macro', self.refusal(
            JDEBUG.replace('$(ProjectDir)/scripts', '$(AppDir)/scripts')))

    def test_refuses_a_missing_file(self):
        with self.assertRaises(SystemExit) as cm:
            capture.resolve_jdebug(str(self.ref.parent / 'absent.jdebug'))
        self.assertIn('not found', str(cm.exception))


@unittest.skipIf(os.name == 'nt', 'ETM capture is Linux-only')
class CaptureCli(unittest.TestCase):
    """Argument contract only: every run here exits before Ozone is looked up."""

    DEVICE = ('--device', 'X', '--target-if', 'SWD', '--tif-speed', '4 MHz', '--trace-width', '4',
              '--cortex-m-default-hooks')

    def run_cli(self, *args, probe=('--probe', '1')):
        return subprocess.run([sys.executable, str(SCRIPTS / 'etm_capture.py'), *probe, *args],
                              capture_output=True, text=True, timeout=30, cwd='/')

    def test_the_probe_is_never_assumed(self):
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf:
            r = self.run_cli(*self.DEVICE, '--elf', elf.name, probe=())
            self.assertEqual(r.returncode, 2)
            self.assertIn('--probe', r.stderr)
            for blank in ('', '  '):
                r = self.run_cli(*self.DEVICE, '--elf', elf.name, probe=('--probe', blank))
                self.assertEqual(r.returncode, 2, repr(blank))
                self.assertIn('--probe is empty', r.stderr)

    def test_missing_boot_hooks_are_the_callers_choice(self):
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf, tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / 'run')
            r = self.run_cli(*self.DEVICE[:-1], '--elf', elf.name, '--out', out)
            self.assertEqual(r.returncode, 1)
            self.assertIn('no AfterTargetReset / AfterTargetDownload from --device', r.stderr)
            self.assertIn('--cortex-m-default-hooks', r.stderr)
            self.assertFalse(os.path.exists(out))   # refused before anything is written

    def test_an_attach_runs_no_boot_hook_so_asks_for_none(self):
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf, socket.socket() as taken:
            taken.bind(('127.0.0.1', 0))
            taken.listen()
            r = self.run_cli(*self.DEVICE[:-1], '--attach', '--elf', elf.name,
                             '--port', str(taken.getsockname()[1]))
            self.assertIn('already listens', r.stderr)   # the refusal after the hooks one

    def test_a_device_without_a_reference_needs_its_interface_speed_and_width(self):
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf:
            r = self.run_cli('--device', 'X', '--elf', elf.name, '--tif-speed', '4 MHz')
            self.assertEqual(r.returncode, 1)
            self.assertIn('--target-if, --trace-width', r.stderr)
            self.assertNotIn('--tif-speed,', r.stderr)

    def test_interface_and_speed_are_refused_beside_a_reference(self):
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf:
            r = self.run_cli('--jdebug', 'x.jdebug', '--elf', elf.name, '--tif-speed', '4 MHz')
            self.assertEqual(r.returncode, 1)
            self.assertIn('come from the reference project', r.stderr)

    def test_needs_exactly_one_of_jdebug_or_device(self):
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf:
            for args in ((), ('--jdebug', 'x.jdebug', '--device', 'X')):
                r = self.run_cli('--elf', elf.name, *args)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn('exactly one of --jdebug', r.stderr)

    def test_elf_is_required_and_must_exist(self):
        r = self.run_cli('--device', 'X')
        self.assertEqual(r.returncode, 2)
        self.assertIn('--elf', r.stderr)
        r = self.run_cli('--device', 'X', '--elf', '/nonexistent/fw.elf')
        self.assertEqual(r.returncode, 1)
        self.assertIn('ELF not found', r.stderr)

    def test_max_inst_is_clamped_with_a_warning(self):
        # exits at the reference-project check, after the clamp warning
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf:
            r = self.run_cli('--jdebug', '/nonexistent.jdebug', '--elf', elf.name,
                             '--max-inst', '20000000')
            self.assertEqual(r.returncode, 1)
            self.assertIn('clamped to 10000000', r.stderr)
            self.assertIn('reference project not found', r.stderr)

    @unittest.skipUnless(shutil.which('xvfb-run'), 'needs xvfb-run')
    def test_a_used_output_dir_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix='.elf') as elf, tempfile.TemporaryDirectory() as d:
            stale = Path(d) / 'code_profile.txt'
            stale.write_text('Code Profile Report from an older run')
            r = self.run_cli(*self.DEVICE, '--elf', elf.name, '--out', d)
            self.assertEqual(r.returncode, 1)
            self.assertIn('not an empty directory', r.stderr)
            self.assertEqual(os.listdir(d), ['code_profile.txt'])   # nothing written beside it
            r = self.run_cli(*self.DEVICE, '--elf', elf.name, '--out', str(stale))
            self.assertIn('not an empty directory', r.stderr)

    def test_help_runs_from_anywhere(self):
        r = self.run_cli('--help')
        self.assertEqual(r.returncode, 0)
        self.assertIn('--jdebug', r.stdout)
        self.assertNotIn('--board', r.stdout)


@unittest.skipIf(os.name == 'nt', 'ETM capture requires POSIX process semantics')
class Session(unittest.TestCase):
    """Session ownership with a stub `ozone` on PATH; nothing here speaks the
    automation protocol, so a capture that works is proven on hardware only."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.stub('xvfb-run', 'import os, sys\nos.execvp(sys.argv[2], sys.argv[2:])\n')
        self.elf = self.tmp / 'fw.elf'
        self.elf.write_bytes(b'')
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\n{body}')
        path.chmod(0o755)

    def popen(self, out):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}')
        return subprocess.Popen(
            [sys.executable, str(SCRIPTS / 'etm_capture.py'), '--probe', '1', *CaptureCli.DEVICE,
             '--elf', str(self.elf), '--out', str(out), '--port', str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd='/')

    def test_an_occupied_port_is_refused_before_anything_is_written(self):
        self.stub('ozone', 'raise SystemExit("must not be launched")\n')
        with socket.socket() as other:
            other.bind(('127.0.0.1', self.port))
            other.listen()
            p = self.popen(self.tmp / 'run')
            _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertIn(f'already listens on 127.0.0.1:{self.port}', err)
        self.assertFalse((self.tmp / 'run').exists())

    def test_an_ozone_that_dies_is_reported_at_once_not_after_the_timeout(self):
        self.stub('ozone', 'raise SystemExit(3)\n')
        t0 = time.time()
        p = self.popen(self.tmp / 'run')
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertIn('Ozone exited (rc=3)', err)
        self.assertLess(time.time() - t0, 10)

    def wait_for(self, path, timeout=20):
        deadline = time.time() + timeout
        while not (path.exists() and path.read_text()) and time.time() < deadline:
            time.sleep(0.1)
        return path.read_text()

    def assert_gone(self, pid):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        self.fail('the stub Ozone outlived the capture script')

    def run_to_refusal(self, *extra, env=None):
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / 'etm_capture.py'), '--probe', '1', *CaptureCli.DEVICE,
             '--elf', str(self.elf), '--out', str(self.tmp / 'run'), '--port', str(self.port), *extra],
            capture_output=True, text=True, timeout=30, cwd='/',
            env=dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', **(env or {})))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertFalse((self.tmp / 'run').exists(), 'refused only after writing')
        return p.stderr

    def test_the_rtos_plugin_is_looked_up_in_the_ozone_that_runs(self):
        self.stub('ozone', 'raise SystemExit("must not be launched")\n')
        other = self.tmp / 'other_install'
        (other / 'Plugins' / 'OS').mkdir(parents=True)
        (other / 'Plugins' / 'OS' / 'FreeRTOSPlugin_CM7.js').write_text('')
        err = self.run_to_refusal('--os-plugin', 'FreeRTOSPlugin_CM7')
        self.assertIn(f"not in {self.bin / 'Plugins' / 'OS'}", err)
        (other / 'Ozone').write_text('')
        err = self.run_to_refusal('--os-plugin', 'NoSuchPlugin', env={'ETM_OZONE': str(other / 'Ozone')})
        self.assertIn(f"not in {other / 'Plugins' / 'OS'}", err)

    def test_without_xvfb_the_callers_display_is_never_used(self):
        self.stub('ozone', 'raise SystemExit("must not be launched")\n')
        (self.bin / 'xvfb-run').unlink()
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / 'etm_capture.py'), '--probe', '1', *CaptureCli.DEVICE,
             '--elf', str(self.elf), '--out', str(self.tmp / 'run'), '--port', str(self.port)],
            capture_output=True, text=True, timeout=30, cwd='/',
            env={'PATH': str(self.bin), 'DISPLAY': ':0'})
        self.assertEqual(p.returncode, 1)
        self.assertIn('xvfb-run not found', p.stderr)
        self.assertFalse((self.tmp / 'run').exists())

    def test_other_platforms_are_refused(self):
        real = sys.platform
        self.addCleanup(setattr, sys, 'platform', real)
        self.addCleanup(setattr, sys, 'argv', sys.argv)
        sys.platform = 'darwin'
        sys.argv = ['etm_capture.py', '--probe', '1', *CaptureCli.DEVICE, '--elf', str(self.elf)]
        with self.assertRaises(SystemExit) as cm:
            capture.main()
        self.assertIn('Linux-only', str(cm.exception))

    def test_a_bad_trace_timing_is_refused_before_writing(self):
        self.stub('ozone', 'raise SystemExit("must not be launched")\n')
        for bad in ('fast', '1,2', '9000'):
            self.assertIn('--trace-timing takes', self.run_to_refusal('--trace-timing', bad), bad)

    def test_a_missing_ozone_or_binutils_is_named_with_its_override(self):
        err = self.run_to_refusal(env={'ETM_OZONE': '/nonexistent/Ozone'})
        self.assertIn('set ETM_OZONE', err)
        self.stub('ozone', 'raise SystemExit("must not be launched")\n')
        err = self.run_to_refusal('--trace-only', 'tud_task', env={'ETM_NM': '/nonexistent/nm'})
        self.assertIn('set ETM_NM', err)
        (self.bin / 'not-executable').write_text('')
        err = self.run_to_refusal('--trace-only', 'tud_task', env={'ETM_NM': str(self.bin / 'not-executable')})
        self.assertIn('Permission denied', err)
        self.assertIn('set ETM_NM', err)
        self.assertNotIn('Traceback', err)
        self.stub('fail-nm', 'import sys\nsys.stderr.write("file format not recognized")\nsys.exit(1)\n')
        err = self.run_to_refusal('--trace-only', 'tud_task', env={'ETM_NM': str(self.bin / 'fail-nm')})
        self.assertIn('file format not recognized', err)

    def test_a_listener_appearing_after_the_preflight_is_not_driven(self):
        pidfile = self.tmp / 'ozone.pid'
        self.stub('ozone', f'import os, time\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\n'
                           'time.sleep(600)\n')
        p = self.popen(self.tmp / 'run')
        ozone = int(self.wait_for(pidfile))
        with socket.socket() as other:
            other.bind(('127.0.0.1', self.port))
            other.listen()
            other.settimeout(0.2)
            _, err = p.communicate(timeout=30)
            with self.assertRaises(socket.timeout):
                other.accept()          # never even connected to
        self.assertEqual(p.returncode, 1)
        self.assertIn('held by a process outside this capture', err)
        self.assert_gone(ozone)

    def test_a_listener_on_another_address_does_not_vouch_for_loopback(self):
        pidfile = self.tmp / 'ozone.pid'
        self.stub('ozone', f'''import os, socket, sys, time
port = int(sys.argv[sys.argv.index("-port") + 1])
srv = socket.socket(); srv.bind(("127.0.0.2", port)); srv.listen()
open({str(pidfile)!r}, "w").write(str(os.getpid()))
time.sleep(600)
''')
        p = self.popen(self.tmp / 'run')
        ozone = int(self.wait_for(pidfile))
        with socket.socket() as other:
            other.bind(('127.0.0.1', self.port))
            other.listen()
            other.settimeout(0.2)
            _, err = p.communicate(timeout=30)
            with self.assertRaises(socket.timeout):
                other.accept()
        self.assertEqual(p.returncode, 1)
        self.assertIn('held by a process outside this capture', err)
        self.assert_gone(ozone)

    def test_a_listener_swapped_after_the_ownership_scan_is_not_driven(self):
        # the scan is told the listener is ours; the one really accepting is this process
        ours = subprocess.Popen(['sleep', '600'], start_new_session=True)
        self.addCleanup(ours.wait)
        self.addCleanup(ours.kill)
        real = capture.listener_owner
        capture.listener_owner = lambda port: {'1': {ours.pid}}
        self.addCleanup(setattr, capture, 'listener_owner', real)
        with socket.socket() as other, open(os.devnull, 'w') as log:
            other.bind(('127.0.0.1', self.port))
            other.listen()
            other.settimeout(5)
            ses = capture.OzoneSession(self.port, log)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as cm:
                ses.connect(20, ours)
            conn, _ = other.accept()
            conn.settimeout(0.5)
            self.assertEqual(conn.recv(100), b'')     # closed without a byte sent
        self.assertIn("not accepted by this capture's Ozone", str(cm.exception))

    def test_only_the_loopback_connection_itself_names_its_peer(self):
        for addr, expect in (('127.0.0.1', {os.getpid()}), ('127.0.0.2', set())):
            with socket.socket() as srv, socket.socket() as cli:
                srv.bind((addr, self.port))
                srv.listen()
                cli.bind((addr, 0))
                cli.connect((addr, self.port))
                conn, _ = srv.accept()
                with conn:
                    self.assertEqual(capture.peer_owner(self.port, cli.getsockname()[1]), expect, addr)

    def test_a_second_sigterm_during_teardown_changes_nothing(self):
        pidfile = self.tmp / 'ozone.pid'
        self.stub('ozone', f'import os, time\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\n'
                           'time.sleep(600)\n')
        p = self.popen(self.tmp / 'run')
        ozone = int(self.wait_for(pidfile))
        for _ in range(50):
            try:
                p.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assert_gone(ozone)

    def protocol_stub(self, pidfile, exiting, leave, halt_says=''):
        """Answers the automation socket well enough to reach File.Exit: plumbing
        only, the protocol itself is proven on hardware."""
        self.stub('ozone', f'''import os, re, socket, sys, time
port = int(sys.argv[sys.argv.index("-port") + 1])
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port)); srv.listen()
open({str(pidfile)!r}, "w").write(str(os.getpid()))
conn, _ = srv.accept()
for line in conn.makefile():
    cmd = line.strip()
    m = re.match(r'Export\\.CodeProfile \\("([^"]+)"', cmd)
    if m:
        open(m.group(1), "w").write("Code Profile Report\\n  Total | 1 | 100\\n")
    m = re.match(r'Export\\.Trace \\("([^"]+)"', cmd)
    if m:
        open(m.group(1), "w").write("Timestamp[s],Address\\n")
    open({str(self.tmp / 'commands')!r}, "a").write(cmd + "\\n")
    if cmd == "Debug.Halt" and {halt_says!r}:
        conn.sendall(({halt_says!r} + "\\n").encode())
    reply = "Debug.IsHalted (); // returns 0x1" if cmd == "Debug.IsHalted" else cmd.split("(")[0].strip() + " ();"
    conn.sendall((reply + "\\n").encode())
    if cmd == "File.Exit":
        open({str(exiting)!r}, "w").write("1")
        if {leave!r}:
            sys.exit(0)
time.sleep(600)
''')

    def popen_short(self, *extra):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}')
        return subprocess.Popen(
            [sys.executable, str(SCRIPTS / 'etm_capture.py'), '--probe', '1', *CaptureCli.DEVICE,
             '--elf', str(self.elf), '--out', str(self.tmp / 'run'), '--port', str(self.port),
             '--duration-ms', '1', *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd='/')

    def test_sigterm_while_waiting_for_ozone_to_exit_still_cleans_up(self):
        pidfile, exiting = self.tmp / 'ozone.pid', self.tmp / 'exiting'
        self.protocol_stub(pidfile, exiting, leave=False)
        p = self.popen_short()
        ozone = int(self.wait_for(pidfile))
        self.assertEqual(self.wait_for(exiting, timeout=90), '1', 'the session never reached File.Exit')
        time.sleep(3)                   # inside the wait for Ozone to go away by itself
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assert_gone(ozone)

    def test_a_failed_probe_power_off_fails_the_capture(self):
        pidfile, exiting = self.tmp / 'ozone.pid', self.tmp / 'exiting'
        self.protocol_stub(pidfile, exiting, leave=True)
        self.stub('JLinkExe', 'import sys\nsys.stderr.write("Cannot connect to J-Link")\nsys.exit(1)\n')
        p = self.popen_short('--power')
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 1)
        self.assertIn('probe power may still be ON', err)
        self.assertIn('Cannot connect to J-Link', err)
        self.assertNotIn('capture OK', out)

    def test_a_first_sigterm_inside_the_teardown_is_not_reported_as_success(self):
        pidfile, exiting, powering = self.tmp / 'ozone.pid', self.tmp / 'exiting', self.tmp / 'powering'
        self.protocol_stub(pidfile, exiting, leave=True)
        # teardown's probe power-off, slow enough to aim a signal into
        self.stub('JLinkExe', f'import time\nopen({str(powering)!r}, "w").write("1")\ntime.sleep(3)\n')
        p = self.popen_short('--power')
        self.assertEqual(self.wait_for(powering, timeout=90), '1', 'the teardown never ran')
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertNotIn('capture OK', out)

    def test_ram_code_is_cached_and_a_run_through_unknown_code_fails_the_itrace(self):
        pidfile, exiting = self.tmp / 'ozone.pid', self.tmp / 'exiting'
        self.protocol_stub(pidfile, exiting, leave=True, halt_says=UNKNOWN_CODE % '0x00003930')
        p = self.popen_short('--trace-csv')
        out, err = p.communicate(timeout=120)
        commands = (self.tmp / 'commands').read_text().splitlines()
        cached = commands.index('Debug.ReadIntoInstCache (0x20000110, 10464)')
        self.assertLess(commands.index('Debug.Start'), cached)
        self.assertLess(cached, commands.index('Debug.Continue'))
        self.assertEqual(p.returncode, 1)
        self.assertIn('itrace.csv is truncated', err)
        self.assertIn('0x00003930', err)
        self.assertNotIn('capture OK', out)

    def test_a_profile_through_unknown_code_is_kept_but_says_so(self):
        pidfile, exiting = self.tmp / 'ozone.pid', self.tmp / 'exiting'
        self.protocol_stub(pidfile, exiting, leave=True, halt_says=UNKNOWN_CODE % '0x00003930')
        p = self.popen_short()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn('capture OK', out)
        self.assertIn('warning: the run executed code Ozone has no image of (at 0x00003930)', out)

    def test_sigterm_takes_the_ozone_group_down_with_it(self):
        pidfile = self.tmp / 'ozone.pid'
        self.stub('ozone', f'import os, time\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\n'
                           'time.sleep(600)\n')
        p = self.popen(self.tmp / 'run')
        ozone = int(self.wait_for(pidfile))
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assert_gone(ozone)


@unittest.skipIf(os.name == 'nt', 'ETM capture is Linux-only')
class InstructionCache(unittest.TestCase):
    def test_only_code_that_startup_copies_is_read_into_the_cache(self):
        self.assertEqual(capture.ram_code_sections('fw.elf'), [('.data', 0x20000110, 0x28e0)])

    def test_a_failing_objdump_is_an_error(self):
        with mock.patch.dict(os.environ, ETM_OBJDUMP='false'), self.assertRaises(SystemExit) as cm:
            capture.ram_code_sections('fw.elf')
        self.assertIn('false failed (section headers', str(cm.exception))

    def test_unknown_code_counts_only_inside_the_traced_run(self):
        boot = UNKNOWN_CODE % '0x3930'
        run = UNKNOWN_CODE % '0x20000260'
        mark = f'[23:41:35] {capture.RUN_MARK} 3000 ms ==='
        self.assertEqual(capture.unknown_code(f'{boot}\n{mark}\n'), [])
        self.assertEqual(capture.unknown_code(f'{boot}\n{mark}\n{run}\n{run}\n'), ['0x20000260'])


class RunTool(unittest.TestCase):
    def test_a_tool_that_hangs_is_not_reported_as_missing(self):
        with self.assertRaises(SystemExit) as cm:
            capture.run_tool('ETM_TEST_TOOL', 'sleep', ['30'], 'test step', timeout=0.2)
        msg = str(cm.exception)
        self.assertIn('sleep hung (test step): no answer in 0.2 s', msg)
        self.assertNotIn('install', msg)


class Exports(unittest.TestCase):
    def args(self, **on):
        ns = dict(profile_lines_csv=False, profile_insts_csv=False, trace_csv=False,
                  sample=None, power=False)
        ns.update(on)
        return type('Args', (), ns)

    def test_every_requested_export_is_expected(self):
        self.assertEqual(capture.requested_exports(self.args()), ['code_profile.txt'])
        self.assertEqual(
            capture.requested_exports(self.args(profile_lines_csv=True, profile_insts_csv=True,
                                                trace_csv=True, sample='ticks', power=True)),
            ['code_profile.txt', 'profile_lines.csv', 'profile_insts.csv', 'itrace.csv',
             'samples.csv', 'power.csv'])

    def test_absent_and_empty_exports_are_both_missing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'code_profile.txt').write_text('Code Profile Report')
            (Path(d) / 'samples.csv').write_text('')
            self.assertEqual(
                capture.missing_exports(d, ['code_profile.txt', 'samples.csv', 'power.csv']),
                ['samples.csv', 'power.csv'])

    def test_a_new_or_empty_dir_is_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(capture.fresh_outdir(d, 'X'), d)
            new = str(Path(d) / 'run1')
            self.assertEqual(capture.fresh_outdir(new, 'X'), new)
            self.assertTrue(os.path.isdir(new))


class ParseProfile(unittest.TestCase):
    def test_reads_both_sections(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'code_profile.txt'
            p.write_text(PROFILE)
            funcs, totals = profile.parse_profile(str(p))
        self.assertEqual(funcs['tud_task'], {'module': 'firmware.elf', 'run': 1000,
                                             'fetch': 20000, 'inst_pct': 100.0})
        self.assertEqual(funcs['idle_loop']['fetch'], 80000)
        self.assertEqual(totals, {'run': 6000, 'fetch': 101000, 'unaccounted': 1000,
                                  'src_cov': (10, 20), 'inst_cov': (30, 60)})

    def test_refuses_a_non_profile_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.log'
            p.write_text('device=STM32H743XI\n')
            with self.assertRaises(SystemExit) as cm:
                profile.parse_profile(str(p))
        self.assertIn('not an Ozone code-profile', str(cm.exception))

    def test_itrace_unit_comes_from_the_header(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'itrace.csv'
            p.write_text('Index;Timestamp[us];PC\n')
            self.assertEqual(profile.itrace_unit(str(p)), 'us')
            p.write_text('Index;PC\n')
            self.assertEqual(profile.itrace_unit(str(p)), '?')


TICK, ISR = 0x1000, 0x2000
SYMS = [(TICK, 0x20, 'isr_systick'), (ISR, 0x40, 'USB_IRQHandler')]


IDLE = 0x5000


def itrace_rows(period_s, isr_s, beats=30, skip_beat=None, tick_loops=1):
    """One tick beat and one 20-instruction ISR episode per period, each entered
    from an idle loop. tick_loops > 1 re-executes the tick's entry instruction
    from inside the handler, as a compiler-placed loop head would."""
    rows = []
    for beat in range(beats):
        t0 = beat * period_s
        if beat != skip_beat:
            rows.append((t0 - 1e-7, IDLE))
            for loop in range(tick_loops):
                rows += [(t0 + (loop * 8 + i) * 1e-7, TICK + 2 * i) for i in range(8)]
        t1 = t0 + period_s / 2
        rows.append((t1 - 1e-7, IDLE))
        rows += [(t1 + i * isr_s / 19, ISR + 2 * i) for i in range(20)]
    return rows


def write_itrace(path, period_s=1e-3, isr_s=110e-6, raw_per_s=1.07e9, timestamps=True, rows=None):
    """Newest row first, as Ozone exports it. raw_per_s is deliberately not a round
    unit: only the tick calibration can turn these timestamps into seconds."""
    rows = sorted(rows or itrace_rows(period_s, isr_s), reverse=True)
    with open(path, 'w') as f:
        f.write('Timestamp[ticks],Address,Instruction\n' if timestamps else 'Address,Instruction\n')
        for t, a in rows:
            f.write((f'{t * raw_per_s:.3f},' if timestamps else '') + f'{a:08X},nop\n')


class IsrCalibration(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.itrace = str(Path(self._dir.name) / 'itrace.csv')
        real = profile.load_symbols
        profile.load_symbols = lambda elf: SYMS
        self.addCleanup(setattr, profile, 'load_symbols', real)

    def report(self, tick_symbol='isr_systick', tick_hz=1000.0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            profile.isr_report(self.itrace, 'fw.elf', 'USB_IRQHandler', tick_symbol, tick_hz)
        return out.getvalue()

    def median_us(self, text):
        return float(text.split('median ')[1].split(' us')[0])

    def test_duration_follows_the_stated_tick_rate(self):
        for hz in (1000.0, 100.0):
            write_itrace(self.itrace, 1 / hz, 110e-6, raw_per_s=1.07e9)
            text = self.report(tick_hz=hz)
            self.assertIn(f'30 isr_systick beats at {hz:g} Hz', text)
            self.assertAlmostEqual(self.median_us(text), 110.0, delta=1.0)

    def test_a_wrong_rate_scales_the_result_instead_of_being_corrected(self):
        # the script cannot know the period: the caller's rate is the only source
        write_itrace(self.itrace, 10e-3, 110e-6, raw_per_s=1.07e9)
        self.assertAlmostEqual(self.median_us(self.report(tick_hz=1000.0)), 11.0, delta=0.1)

    def test_without_a_tick_the_timing_is_unavailable(self):
        write_itrace(self.itrace, 1e-3, 110e-6, raw_per_s=1.07e9)
        text = self.report(tick_symbol=None, tick_hz=None)
        self.assertIn('unavailable', text)
        self.assertNotIn(' us', text)

    def test_a_timestamp_free_capture_has_no_beats(self):
        write_itrace(self.itrace, 1e-3, 110e-6, raw_per_s=1.07e9, timestamps=False)
        text = self.report()
        self.assertIn('not enough isr_systick beats', text)
        self.assertNotIn('median', text)

    def test_a_missing_tick_symbol_is_named(self):
        write_itrace(self.itrace, 1e-3, 110e-6, raw_per_s=1.07e9)
        text = self.report(tick_symbol='SysTick_Handler')
        self.assertIn("symbol 'SysTick_Handler' not found", text)
        self.assertIn('skipped', text)

    def test_a_lost_beat_does_not_rescale_its_neighbours(self):
        write_itrace(self.itrace, rows=itrace_rows(1e-3, 110e-6, skip_beat=12))
        text = self.report()
        self.assertIn('29 isr_systick beats', text)
        self.assertAlmostEqual(float(text.split('fastest ')[1].split(' us')[0]), 110.0, delta=1.0)

    def test_a_loop_head_at_the_tick_entry_is_one_beat(self):
        write_itrace(self.itrace, rows=itrace_rows(1e-3, 110e-6, tick_loops=5))
        text = self.report()
        self.assertIn('30 isr_systick beats', text)
        self.assertAlmostEqual(self.median_us(text), 110.0, delta=1.0)

    def test_an_interrupted_episode_is_excluded_not_reported_short(self):
        rows = itrace_rows(1e-3, 110e-6)
        t = 15.25e-3   # 12 instructions over 11 us, preempted 100 us, 8 more
        rows += [(t - 1e-7, IDLE)] + [(t + i * 1e-6, ISR + 2 * i) for i in range(12)]
        rows += [(t + 111e-6 + i * 1e-6, ISR + 24 + 2 * i) for i in range(8)]
        write_itrace(self.itrace, rows=rows)
        text = self.report()
        self.assertIn('1 episode(s) excluded', text)
        self.assertAlmostEqual(float(text.split('fastest ')[1].split(' us')[0]), 110.0, delta=1.0)

    def test_an_ambiguous_symbol_is_refused(self):
        write_itrace(self.itrace, 1e-3, 110e-6, raw_per_s=1.07e9)
        profile.load_symbols = lambda elf: SYMS + [(0x3000, 0x20, 'isr_systick')]
        with self.assertRaises(SystemExit) as cm:
            self.report()
        self.assertIn('ambiguous', str(cm.exception))


class ProfileCli(unittest.TestCase):
    """Argument contract only: every run here exits before the capture dir is read."""

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'etm_profile.py'), '/nonexistent', *args],
                              capture_output=True, text=True, timeout=30, cwd='/')

    def test_tick_symbol_and_rate_are_a_pair(self):
        for args in (('--tick-symbol', 'isr_systick'), ('--tick-hz', '1000')):
            r = self.run_cli('--isr', 'USB_IRQHandler', '--elf', 'fw.elf', *args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn('go together', r.stderr)

    def test_rate_must_be_positive_and_finite(self):
        for hz in ('0', '-100', 'nan', 'inf'):
            r = self.run_cli('--isr', 'USB_IRQHandler', '--elf', 'fw.elf',
                             '--tick-symbol', 'isr_systick', '--tick-hz', hz)
            self.assertEqual(r.returncode, 2, (hz, r.stderr))
            self.assertIn('positive, finite', r.stderr)

    def test_tick_without_isr_or_with_an_empty_symbol_is_refused(self):
        r = self.run_cli('--tick-symbol', 'isr_systick', '--tick-hz', '1000')
        self.assertEqual(r.returncode, 2)
        self.assertIn('only calibrate --isr', r.stderr)
        r = self.run_cli('--isr', 'USB_IRQHandler', '--tick-symbol', ' ', '--tick-hz', '1000')
        self.assertEqual(r.returncode, 2)
        self.assertIn('empty', r.stderr)


if __name__ == '__main__':
    unittest.main()
