"""skills/hil/scripts/hil_flash.py builds and runs each flasher backend's commands from a
flasher dict, moved from tinyusb's test/hil/hil_flash.py (c5099be4f). The golden test replays
every flasher of that commit's tinyusb.json and hfp.json (copies in tests/data/hil_flash),
plus a pyOCD entry, and requires exactly what tinyusb's module produced
(tests/data/hil_flash/gen_golden.py): commands, J-Link scripts, cwd, timeouts, retries,
output and the result handed back. Only the pyOCD launcher path and its --project-dir
argument are normalised. Equality there does not establish timing, signals or pyOCD's
project-directory behaviour; test_hil_process and test_hil_pyocd do."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_flash is Linux only')

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'hil' / 'scripts'
DATA = Path(__file__).resolve().parent / 'data' / 'hil_flash'


def load(name, path):
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hf = load('agentrc_hil_flash', SCRIPTS / 'hil_flash.py')
gen = load('hil_flash_gen_golden', DATA / 'gen_golden.py')


def provider_invoke(case, run, ctx):
    """What a project adapter passes for each case (tinyusb: board name as the J-Link command
    file stem, 2 crash retries, the build's IDF_TARGET, flash_args and serial port)."""
    f, op, t = case['flasher'], case['op'], case['timeout']
    name = f['name'].lower()
    if op == 'rescue':
        return hf.rescue_openocd(f, case['flash_out'], timeout=t, run=run)
    if name == 'esptool':
        return hf.flash_esptool(f, chip=gen.ESP_CHIP[case['board']], port=gen.ESP_PORT.format(uid=f['uid']),
                                flash_args=gen.ESP_FLASH_ARGS.strip().replace('\n', ' '),
                                cwd=str(ctx['fw_dir']), timeout=t, run=run)
    kw = {'jlink': {'command_file_stem': case['board'], 'crash_retries': 2},
          'pyocd': {'project_dir': str(ctx['project_dir'])}}.get(name, {})
    args = (gen.FIRMWARE,) if op == 'flash' else ()
    return getattr(hf, f'{op}_{name}')(f, *args, timeout=t, run=run, **kw)


def provider_normalise(cmd, ctx):
    if isinstance(cmd, list) and cmd[1:4] == [hf.PYOCD_LAUNCHER, '--project-dir', str(ctx['project_dir'])]:
        return [cmd[0], '{LAUNCHER}', *cmd[4:]]
    return cmd


class Golden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        golden = json.loads((DATA / 'golden.json').read_text())
        cls.observed = {tuple(c['case']): c['observed'] for c in golden['cases']}

    def test_the_fixture_covers_every_case(self):
        self.assertEqual(list(self.observed), [gen.key(c) for c in gen.cases()])

    def test_every_roster_flasher_kind_is_covered(self):
        names = {f['name'].lower() for *_, f in gen.flashers()}
        self.assertEqual(names, {'jlink', 'stlink', 'openocd', 'lm4flash', 'esptool', 'pyocd'})

    def test_the_provider_matches_the_pre_move_module(self):
        with mock.patch.object(hf, '_pyocd_python', return_value='/py'):
            for case in gen.cases():
                with self.subTest(case=gen.key(case)):
                    self.assertEqual(gen.record(case, hf, provider_invoke, provider_normalise),
                                     self.observed[gen.key(case)])


def capture(fn, *args, rcs=(0,), took=0, **kw):
    """Run fn with run= answering rcs in turn, each call taking `took` s on a fake clock, in a
    fresh cwd; (result, calls, cwd listing, stdout)."""
    calls, rc_iter, now = [], iter(rcs), [0.0]

    def run(cmd, **k):
        calls.append((cmd, k))
        now[0] += took
        return subprocess.CompletedProcess(cmd, next(rc_iter), '', '')
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out, \
            mock.patch.object(hf, 'time', types.SimpleNamespace(monotonic=lambda: now[0])):
        os.chdir(d)
        try:
            ret = fn(*args, run=run, **kw)
            left = sorted(os.listdir(d))
        finally:
            os.chdir(cwd)
    return ret, calls, left, out.getvalue()


JLINK = {'name': 'jlink', 'uid': 'S1', 'args': '-device x'}


class JLink(unittest.TestCase):
    def test_flash_writes_the_script_runs_it_and_removes_it(self):
        _, calls, left, _ = capture(hf.flash_jlink, JLINK, '/fw/a.hex', command_file_stem='b', timeout=7)
        self.assertEqual(calls, [('JLinkExe -USB S1 -device x -if swd -JTAGConf -1,-1 -speed auto -NoGui 1 '
                                  '-ExitOnError 1 -CommandFile b_a.hex.jlink', {'timeout': 7})])
        self.assertEqual(left, [])
        case = {'board': 'b', 'rcs': [0], 'took': 0}
        observed = gen.record(case, hf, lambda c, run, ctx: hf.flash_jlink(
            JLINK, '/fw/a.hex', command_file_stem='b', timeout=7, run=run), lambda cmd, ctx: cmd)
        self.assertEqual(observed['calls'][0]['script'], 'halt\nr\nloadfile /fw/a.hex\nr\ngo\nexit\n')

    def test_reset_keeps_its_script(self):
        _, calls, left, _ = capture(hf.reset_jlink, JLINK, command_file_stem='b', timeout=11)
        self.assertEqual(calls[0][1], {'timeout': 11})
        self.assertTrue(calls[0][0].endswith('-CommandFile b_reset.jlink'))
        self.assertEqual(left, ['b_reset.jlink'])

    def test_no_crash_rerun_by_default(self):
        ret, calls, _, _ = capture(hf.reset_jlink, JLINK, command_file_stem='b', timeout=30, rcs=[139])
        self.assertEqual((ret.returncode, len(calls)), (139, 1))

    def test_crash_reruns_share_one_deadline(self):
        def timeouts(fn, *args, **kw):
            ret, calls, _, out = capture(fn, JLINK, *args, command_file_stem='b', crash_retries=2, **kw)
            return ret.returncode, [k['timeout'] for _, k in calls], out.count('rerunning')
        self.assertEqual(timeouts(hf.flash_jlink, '/fw/a.hex', timeout=30, rcs=[139, -6, 0], took=10),
                         (0, [30, 20, 10], 2))
        self.assertEqual(timeouts(hf.reset_jlink, timeout=30, rcs=[134, -11, 139], took=1),
                         (139, [30, 29, 28], 2))
        # the bound is spent: no rerun
        self.assertEqual(timeouts(hf.reset_jlink, timeout=5, rcs=[139], took=5), (139, [5], 0))

    def test_other_failures_are_not_rerun(self):
        for rc in (1, 124):
            ret, calls, _, _ = capture(hf.reset_jlink, JLINK, command_file_stem='b', timeout=5,
                                       crash_retries=2, rcs=[rc])
            self.assertEqual((ret.returncode, len(calls)), (rc, 1))

    def test_the_crash_rcs_are_shell_and_direct_signals(self):
        self.assertEqual(hf.JLINK_CRASH_RCS, {139, -11, 134, -6})


class StlinkLm4flashEsptool(unittest.TestCase):
    def test_stlink_verify_is_opt_in(self):
        for extra, want in (({}, ''), ({'verify': True}, ' --verify'), ({'verify': False}, '')):
            _, calls, _, _ = capture(hf.flash_stlink, {'name': 'stlink', 'uid': 'S1', **extra}, 'fw.elf', timeout=7)
            self.assertEqual(calls, [(f'STM32_Programmer_CLI --connect port=swd sn=S1 --write fw.elf{want} --go',
                                      {'timeout': 7})])

    def test_stlink_reset_forwards_the_bound(self):
        _, calls, _, _ = capture(hf.reset_stlink, {'name': 'stlink', 'uid': 'S1'}, timeout=11)
        self.assertEqual(calls, [('STM32_Programmer_CLI --connect port=swd sn=S1 --rst --go', {'timeout': 11})])

    def test_lm4flash(self):
        _, calls, _, _ = capture(hf.flash_lm4flash, {'name': 'lm4flash', 'uid': 'S1', 'args': '-v'}, 'fw.bin',
                                 timeout=7)
        self.assertEqual(calls, [('lm4flash -s S1 -v fw.bin', {'timeout': 7})])

    def test_esptool_runs_in_the_build_dir_and_never_resets_alone(self):
        _, calls, _, _ = capture(hf.flash_esptool, {'name': 'esptool', 'uid': 'U', 'args': '-b 1500000'},
                                 chip='esp32s3', port='/dev/ttyACM0', flash_args='0x0 a.bin', cwd='/b', timeout=7)
        self.assertEqual(calls, [('esptool --chip esp32s3 -p /dev/ttyACM0 -b 1500000 --before=default_reset '
                                  '--after=hard_reset write_flash 0x0 a.bin', {'cwd': '/b', 'timeout': 7})])
        self.assertFalse(hasattr(hf, 'reset_esptool'))
        self.assertFalse(hasattr(hf, 'reset_lm4flash'))

    def test_timeout_is_explicit_everywhere(self):
        for fn, args in ((hf.flash_stlink, ({}, 'fw')), (hf.reset_stlink, ({},)), (hf.flash_openocd, ({}, 'fw')),
                         (hf.reset_openocd, ({},)), (hf.flash_lm4flash, ({}, 'fw')),
                         (hf.flash_jlink, ({}, 'fw')), (hf.reset_jlink, ({},)), (hf.rescue_openocd, ({},))):
            with self.subTest(fn=fn.__name__), self.assertRaises(TypeError):
                fn(*args)


class DefaultRunner(unittest.TestCase):
    def test_run_defaults_to_the_provider_runner(self):
        """No run= reaches hil_process.run_cmd with the caller's bound: a fake tool on PATH."""
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / 'lm4flash'
            tool.write_text('#!/bin/sh\necho "args: $*"\n')
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}'}):
                r = hf.flash_lm4flash({'name': 'lm4flash', 'uid': 'S1', 'args': '-v'}, 'fw.bin', timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, 'args: -s S1 -v fw.bin\n'))
        self.assertIs(hf._runner(None), hf.hil_process.run_cmd)


class Openocd(unittest.TestCase):
    """The roster's optional flasher `vid_pid` field (openocd-verbatim, e.g. "0x1a86 0x8010",
    more pairs appended) pins openocd's probe discovery so it never opens foreign usbfs
    nodes. It must be emitted BEFORE the args: the rescue cfgs run `init` internally."""

    def setUp(self):
        hf._VID_PID_WARNED.clear()

    def test_vid_pid_flag_precedes_args(self):
        cmd = hf.openocd_cmd_base({'uid': 'S1', 'args': '-f target/wch-riscv.cfg', 'vid_pid': '0x1a86 0x8010'})
        self.assertEqual(cmd, 'openocd -c "tcl_port disabled" -c "gdb_port disabled" -c "telnet_port disabled" '
                              '-c "adapter serial S1" -c "adapter usb vid_pid 0x1a86 0x8010" -f target/wch-riscv.cfg')

    def test_vid_pid_multiple_pairs(self):
        cmd = hf.openocd_cmd_base({'uid': 'S1', 'args': '-f i.cfg', 'vid_pid': '0x2e8a 0x000c 0x2e8a 0x000d'})
        self.assertIn('-c "adapter usb vid_pid 0x2e8a 0x000c 0x2e8a 0x000d"', cmd)

    def test_no_pin_warns_once_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(2):
                cmd = hf.openocd_cmd_base({'name': 'openocd', 'uid': 'S-warn', 'args': '-f i.cfg'})
        self.assertNotIn('vid_pid', cmd)
        self.assertEqual(err.getvalue().count('has no vid_pid pin'), 1)

    def test_a_malformed_pin_is_dropped_with_a_warning(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cmd = hf.openocd_cmd_base({'name': 'openocd', 'uid': 'S-bad', 'args': '-f i.cfg', 'vid_pid': '1a86_55d4'})
        self.assertNotIn('vid_pid', cmd)
        self.assertIn("malformed vid_pid '1a86_55d4'", err.getvalue())

    def test_openocd_over_jlink_does_not_warn(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hf.openocd_cmd_base({'name': 'openocd', 'uid': 'S-jl', 'args': '-f interface/jlink.cfg'})
        self.assertEqual(err.getvalue(), '')

    def test_flash_uses_program_with_verify_by_default(self):
        f = {'name': 'openocd', 'uid': 'S1', 'args': '-f interface/jlink.cfg -f target/k60.cfg'}
        _, calls, _, _ = capture(hf.flash_openocd, f, '/tmp/fw.elf', timeout=7)
        self.assertTrue(calls[0][0].endswith('-f target/k60.cfg -c "program /tmp/fw.elf verify reset exit"'))
        self.assertEqual(calls[0][1], {'timeout': 7})
        _, calls, _, _ = capture(hf.flash_openocd, {**f, 'verify': False}, '/tmp/fw.elf', timeout=7)
        self.assertTrue(calls[0][0].endswith('-c "program /tmp/fw.elf reset exit"'))

    def test_reset_is_bounded(self):
        f = {'name': 'openocd', 'uid': 'S1', 'args': '-f interface/jlink.cfg'}
        _, calls, _, _ = capture(hf.reset_openocd, f, timeout=9)
        self.assertTrue(calls[0][0].endswith('-f interface/jlink.cfg -c "init; reset run; exit"'))
        self.assertEqual(calls[0][1], {'timeout': 9})


class Rescue(unittest.TestCase):
    RP2040 = {'name': 'openocd', 'uid': 'S1', 'vid_pid': '0x2e8a 0x000c',
              'args': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg'}
    RP2350 = {**RP2040, 'args': '-f interface/cmsis-dap.cfg -f target/rp2350.cfg'}

    def test_rp2040_rescue_keeps_the_pin_before_init(self):
        ret, calls, _, _ = capture(hf.rescue_openocd, self.RP2040, f'x {hf.DAP_WEDGED[0]} y', timeout=13)
        self.assertIs(ret, True)
        cmd = calls[0][0]
        self.assertIn('-c "adapter usb vid_pid 0x2e8a 0x000c" -c "set RESCUE 1" -f interface/cmsis-dap.cfg '
                      '-f target/rp2040.cfg', cmd)
        self.assertEqual(calls[0][1], {'timeout': 13})

    def test_rp2350_rescue_swaps_the_cfg_and_shuts_down(self):
        _, calls, _, _ = capture(hf.rescue_openocd, self.RP2350, hf.DAP_WEDGED[1], timeout=13)
        self.assertTrue(calls[0][0].endswith('-f target/rp2350-rescue.cfg -c "shutdown"'))

    def test_the_result_is_the_rescue_commands_rc(self):
        for rc, want in ((0, True), (1, False), (124, False)):
            ret, _, _, _ = capture(hf.rescue_openocd, self.RP2040, hf.DAP_WEDGED[0], timeout=13, rcs=[rc])
            self.assertIs(ret, want)

    def test_no_wedge_or_no_rp_target_runs_nothing(self):
        for f, out in ((self.RP2040, 'Error: flash write failed'), (self.RP2040, ''),
                       ({**self.RP2040, 'args': '-f target/stm32f4x.cfg'}, hf.DAP_WEDGED[0]),
                       ({**self.RP2040, 'name': 'jlink'}, hf.DAP_WEDGED[0])):
            ret, calls, _, _ = capture(hf.rescue_openocd, f, out, timeout=13)
            self.assertEqual((ret, calls), (False, []))


class ConvoySafe(unittest.TestCase):
    """convoy_safe is true ONLY for flashers that reach their probe without opening a
    poisoned usbfs node (tinyusb test_hil_bounded.py ConvoySafeFlasher and
    test_change_impact.py FlasherRecoverEntry)."""

    f = staticmethod(hf.convoy_safe)

    def test_pinned_openocd_is_safe(self):
        self.assertTrue(self.f({'name': 'openocd', 'vid_pid': '0x2e8a 0x000c'}))
        self.assertTrue(self.f({'name': 'openocd', 'vid_pid': '0x2e8a 0x000c', 'args': '-f interface/cmsis-dap.cfg'}))

    def test_unpinned_openocd_is_not(self):
        self.assertFalse(self.f({'name': 'openocd'}))
        self.assertFalse(self.f({'name': 'openocd', 'vid_pid': ''}))
        self.assertFalse(self.f({'name': 'openocd', 'args': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg'}))

    def test_esptool_and_stlink_are_safe_without_a_pin(self):
        self.assertTrue(self.f({'name': 'esptool'}))
        self.assertTrue(self.f({'name': 'stlink', 'uid': 'X'}))

    def test_openocd_over_a_filtering_driver_is_safe_without_a_pin(self):
        for cfg in hf.CONVOY_SAFE_CFGS:
            self.assertTrue(self.f({'name': 'openocd', 'args': f'-f {cfg} -f target/stm32h7x.cfg'}), cfg)

    def test_enumerating_flashers_are_not(self):
        for name in ('jlink', 'lm4flash', 'dfu-util', 'openocd_wch'):
            self.assertFalse(self.f({'name': name, 'vid_pid': '0x1366 0x1024'}), name)

    def test_missing_or_odd_name_is_not_safe(self):
        for flasher in ({}, {'name': None}, {'name': ''}):
            self.assertFalse(self.f(flasher))

    def test_valid_vid_pid(self):
        for value, want in (('0x2e8a 0x000c', True), (' 0x2e8a  0x000c ', True), ('0x2e8a 0x000c 0x2e8a 0x000d', True),
                            ('0x2e8a', False), ('2e8a 000c', False), ('1a86_55d4', False), (None, False), ('', False)):
            self.assertEqual(hf.valid_vid_pid(value), want, value)


class Pyocd(unittest.TestCase):
    FLASHER = {'name': 'pyocd', 'uid': 'GSA0CQEQ', 'vid_pid': '0x1fc9 0x0090', 'args': '-t lpc55s28'}

    def test_flash_and_reset_run_the_launcher(self):
        head = ['/py', hf.PYOCD_LAUNCHER, '--project-dir', '/proj', '0x1fc9', '0x0090']
        tail = ['-W', '-u', 'cmsisdap:GSA0CQEQ', '-t', 'lpc55s28']
        with mock.patch.object(hf, '_pyocd_python', return_value='/py'):
            for fn, fw, verb in ((hf.flash_pyocd, ('/tmp/a b/fw.elf',), 'flash'), (hf.reset_pyocd, (), 'reset')):
                _, calls, _, _ = capture(fn, self.FLASHER, *fw, project_dir='/proj', timeout=7)
                self.assertEqual(calls, [([*head, verb, *tail, *fw], {'timeout': 7})])
        self.assertTrue(Path(hf.PYOCD_LAUNCHER).is_file())

    def test_a_relative_firmware_is_made_absolute(self):
        with mock.patch.object(hf, '_pyocd_python', return_value='/py'):
            _, calls, _, _ = capture(hf.flash_pyocd, self.FLASHER, 'fw.elf', project_dir='/proj', timeout=7)
        self.assertTrue(os.path.isabs(calls[0][0][-1]))

    def test_an_unfiltered_entry_is_refused(self):
        for bad in ({'vid_pid': None}, {'vid_pid': '0x1fc9 0x0090 0x1fc9 0x0143'},
                    {'vid_pid': '1fc9:0090'}, {'uid': ''}):
            flasher = {**self.FLASHER, **bad}
            self.assertIsNone(hf.pyocd_vid_pid(flasher), bad)
            self.assertFalse(hf.convoy_safe(flasher), bad)
            with self.assertRaises(ValueError):
                hf._pyocd_argv(flasher, 'reset', project_dir='/proj')
        self.assertTrue(hf.convoy_safe(self.FLASHER))
        self.assertEqual(hf.pyocd_vid_pid(self.FLASHER), ['0x1fc9', '0x0090'])

    def test_the_interpreter_comes_from_pyocds_shebang(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / 'pyocd'
            for shebang, want in ((f'#!{sys.executable}', sys.executable),
                                  ('#!/usr/bin/env python3', None), ('#!/bin/sh', None), ('', None)):
                exe.write_text(f'{shebang}\nimport sys\n')
                exe.chmod(0o755)
                with mock.patch.dict(os.environ, {'PATH': td}):
                    if want:
                        self.assertEqual(hf._pyocd_python(), want)
                    else:
                        self.assertRaises(RuntimeError, hf._pyocd_python)
            exe.unlink()
            with mock.patch.dict(os.environ, {'PATH': td}):
                self.assertRaises(RuntimeError, hf._pyocd_python)


if __name__ == '__main__':
    unittest.main()
