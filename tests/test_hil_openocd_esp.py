"""skills/hil/scripts/hil_openocd_esp.py, the openocd_esp flasher: roster admission, the verified
openocd-esp32 build and its scripts, the USJ's bus-port from a temp sysfs and udev tree, and the
order of the selectors in the server's argv. Fake tools only: no openocd or board is touched."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_openocd_esp is Linux only')

import importlib.util
import stat
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'hil' / 'scripts'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oe = load('agentrc_hil_openocd_esp', SCRIPTS / 'hil_openocd_esp.py')
hf = load('agentrc_hil_flash', SCRIPTS / 'hil_flash.py')

SERIAL = '30:ED:A0:E0:C9:02'
BOARD = {'name': 'openocd_esp', 'uid': SERIAL, 'args': '-f board/esp32p4-builtin.cfg'}
BANNER = f'Open On-Chip Debugger {oe.VERIFIED_OPENOCD} (2026-07-03-13:49)'


def executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def fake_install(root, banner=BANNER, chips=('esp32p4', 'esp32c3'), board_body=None):
    """<root>/bin/openocd printing `banner`, and <root>/share/openocd/scripts as an install has it."""
    (root / 'bin').mkdir(parents=True, exist_ok=True)
    executable(root / 'bin' / 'openocd', f'#!/bin/sh\necho "{banner}" >&2\n')
    scripts = root / 'share' / 'openocd' / 'scripts'
    for sub in ('board', 'target', 'interface'):
        (scripts / sub).mkdir(parents=True, exist_ok=True)
    (scripts / 'target' / 'esp_common.cfg').write_text('proc program_esp {filename args} {\n}\n')
    (scripts / 'interface' / 'esp_usb_jtag.cfg').write_text('adapter driver esp_usb_jtag\n')
    for chip in chips:
        (scripts / 'target' / f'{chip}.cfg').write_text('source [find target/esp_common.cfg]\n')
        (scripts / 'board' / f'{chip}-builtin.cfg').write_text(
            board_body or f'# example\n\nsource [find interface/esp_usb_jtag.cfg]\nsource [find target/{chip}.cfg]\n')
    (scripts / 'target' / 'esp32.cfg').write_text('')    # a chip without a USJ
    return root / 'bin' / 'openocd', scripts


class FakeBus:
    """A temp /sys/bus/usb/devices and /run/udev/data, swapped into the module's usb_observe."""

    def __init__(self, test):
        td = tempfile.TemporaryDirectory()
        test.addCleanup(td.cleanup)
        self.sys, self.udev = Path(td.name) / 'sys', Path(td.name) / 'udev'
        self.sys.mkdir()
        self.udev.mkdir()
        for name, path in (('SYS_USB_DEVICES', self.sys), ('UDEV_DATA', self.udev)):
            p = mock.patch.object(oe.usb_observe, name, str(path))
            p.start()
            test.addCleanup(p.stop)

    def add(self, busport, serial=SERIAL, *, vid_pid=oe.USJ_VID_PID, bus=5, dev=14, udev=True, sysfs=True):
        d = self.sys / busport
        d.mkdir()
        for name, value in (('idVendor', vid_pid[0]), ('idProduct', vid_pid[1]), ('busnum', bus), ('devnum', dev)):
            (d / name).write_text(f'{value}\n')
        if serial is not None and sysfs:
            (d / 'serial').write_text(f'{serial}\n')
        if serial is not None and udev:
            (self.udev / f'c189:{(bus - 1) * 128 + dev - 1}').write_text(f'E:ID_SERIAL_SHORT={serial}\n')
        return d


class Admit(unittest.TestCase):
    def test_a_board_cfg_or_the_usj_interface_with_a_target(self):
        self.assertEqual(oe.admit(BOARD), ('esp32p4', ['board/esp32p4-builtin.cfg']))
        pair = {**BOARD, 'args': '-f interface/esp_usb_jtag.cfg  -f target/esp32c3.cfg'}
        self.assertEqual(oe.admit(pair), ('esp32c3', ['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg']))

    def test_anything_that_selects_runs_commands_or_inits_is_refused(self):
        for args in ('', '-f board/esp32p4-builtin.cfg -c init', '-c "adapter serial X" -f board/esp32p4-builtin.cfg',
                     '-f board/esp32p4-builtin.cfg -c "adapter usb location 1-1"', '-s /tmp -f board/esp32p4-builtin.cfg',
                     '-f board/esp32p4-lpcore-builtin.cfg', '-f board/../board/esp32p4-builtin.cfg',
                     '-f board/esp32p4-builtin.cfg -f target/esp32p4.cfg', '-f board/esp32s3-ftdi.cfg',
                     '-f interface/ftdi/esp32_devkitj_v1.cfg -f target/esp32.cfg',
                     '-f target/esp32p4.cfg -f interface/esp_usb_jtag.cfg', '-f board/esp32p4-builtin.cfg -d3',
                     '-f interface/esp_usb_jtag.cfg -f target/esp32p4.cfg -f target/esp32c3.cfg'):
            with self.subTest(args=args), self.assertRaises(ValueError):
                oe.admit({**BOARD, 'args': args})

    def test_a_vid_pid_another_name_or_an_odd_uid_is_refused(self):
        for f in ({**BOARD, 'vid_pid': '0x303a 0x1001'}, {**BOARD, 'name': 'openocd'}, {**BOARD, 'uid': ''},
                  {**BOARD, 'uid': 'a b'}, {**BOARD, 'uid': 'x;shutdown'}, {**BOARD, 'uid': None}):
            with self.subTest(f=f), self.assertRaises(ValueError):
                oe.admit(f)

    def test_never_convoy_safe(self):
        self.assertFalse(hf.convoy_safe(BOARD))


class FindOpenocd(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def test_the_verified_build_and_its_own_scripts(self):
        exe, scripts = fake_install(self.root)
        got = oe.find_openocd(str(exe))
        self.assertEqual(got, {'exe': str(exe), 'banner': BANNER, 'scripts': str(scripts)})

    def test_the_env_override_comes_before_path(self):
        exe, _ = fake_install(self.root)
        with mock.patch.dict(os.environ, {oe.OPENOCD_ENV: str(exe), 'PATH': '/nonexistent'}):
            self.assertEqual(oe.find_openocd()['exe'], str(exe))

    def test_another_build_or_upstream_is_refused(self):
        for banner in ('Open On-Chip Debugger v0.12.0-esp32-20251215 (2025-12-15-10:00)',
                       'Open On-Chip Debugger 0.12.0+dev-01850-geb6f2745b (2026-01-01-00:00)', ''):
            exe, _ = fake_install(self.root, banner=banner)
            with self.subTest(banner=banner), self.assertRaisesRegex(RuntimeError, 'not openocd-esp32'):
                oe.find_openocd(str(exe))

    def test_missing_tool_or_scripts_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            oe.find_openocd(str(self.root / 'nope'))
        exe, scripts = fake_install(self.root)
        (scripts / 'target' / 'esp_common.cfg').write_text('# no procs\n')
        with self.assertRaisesRegex(RuntimeError, 'no program_esp'):
            oe.find_openocd(str(exe))
        (scripts / 'target' / 'esp_common.cfg').unlink()
        with self.assertRaisesRegex(RuntimeError, 'no openocd-esp32 scripts'):
            oe.find_openocd(str(exe))


class CheckCfgs(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def test_the_installed_usj_route_passes(self):
        _, scripts = fake_install(self.root)
        oe.check_cfgs('esp32p4', ['board/esp32p4-builtin.cfg'], str(scripts))
        oe.check_cfgs('esp32c3', ['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg'], str(scripts))

    def test_a_chip_without_a_usj_board_cfg_is_refused(self):
        _, scripts = fake_install(self.root)
        with self.assertRaisesRegex(ValueError, 'no board/esp32-builtin.cfg'):
            oe.check_cfgs('esp32', ['interface/esp_usb_jtag.cfg', 'target/esp32.cfg'], str(scripts))

    def test_a_board_cfg_that_does_more_is_refused(self):
        _, scripts = fake_install(self.root, board_body='source [find interface/esp_usb_jtag.cfg]\n'
                                                        'adapter serial X\nsource [find target/esp32p4.cfg]\n')
        with self.assertRaisesRegex(ValueError, 'not the plain USJ board cfg'):
            oe.check_cfgs('esp32p4', ['board/esp32p4-builtin.cfg'], str(scripts))

    def test_a_missing_target_cfg_is_refused(self):
        _, scripts = fake_install(self.root)
        (scripts / 'target' / 'esp32c3.cfg').unlink()
        with self.assertRaisesRegex(ValueError, 'target/esp32c3.cfg is not in'):
            oe.check_cfgs('esp32c3', ['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg'], str(scripts))


class UsjLocation(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus(self)

    def test_the_one_match_by_serial(self):
        self.bus.add('5-3.4')
        self.bus.add('5-3.1', 'AA:BB:CC:DD:EE:FF', dev=9)
        self.bus.add('5-2', SERIAL, vid_pid=('239a', '00a5'), dev=3)     # same serial, not a USJ
        (self.bus.sys / '5-3.4:1.0').mkdir()                             # an interface
        self.assertEqual(oe.usj_location(SERIAL), '5-3.4')

    def test_udevs_record_comes_before_the_locked_serial(self):
        d = self.bus.add('5-3.4', sysfs=False)
        os.mkfifo(d / 'serial')        # a read of it would block
        self.assertEqual(oe.usj_location(SERIAL), '5-3.4')

    def test_without_a_udev_record_the_bounded_sysfs_read(self):
        self.bus.add('5-3.4', udev=False)
        self.assertEqual(oe.usj_location(SERIAL), '5-3.4')

    def test_missing_is_refused_without_a_serial_only_fallback(self):
        self.bus.add('5-3.1', 'AA:BB:CC:DD:EE:FF')
        with self.assertRaisesRegex(RuntimeError, 'no 303a:1001 with that serial'):
            oe.usj_location(SERIAL)
        with self.assertRaisesRegex(RuntimeError, 'no 303a:1001'):
            oe.usj_location(SERIAL.lower())     # openocd compares exactly

    def test_ambiguous_is_refused(self):
        self.bus.add('5-3.4')
        self.bus.add('3-1', bus=3, dev=2)
        with self.assertRaisesRegex(RuntimeError, 'ambiguous, at 3-1, 5-3.4'):
            oe.usj_location(SERIAL)

    def test_a_usj_whose_serial_cannot_be_read_is_not_ruled_out(self):
        self.bus.add('5-3.4')
        self.bus.add('5-3.1', None, dev=9)
        with self.assertRaisesRegex(RuntimeError, r'cannot rule out 5-3\.1'):
            oe.usj_location(SERIAL)

    def test_a_usj_that_disappears_mid_walk_is_refused(self):
        d = self.bus.add('5-3.4')
        (d / 'devnum').unlink()
        with self.assertRaisesRegex(RuntimeError, 'disappeared during the walk'):
            oe.usj_location(SERIAL)

    def test_a_change_between_the_two_walks_is_refused(self):
        self.bus.add('5-3.4')
        walks = iter([[('5-3.4', 5, 14)], [('5-3.4', 5, 15)]])
        with mock.patch.object(oe, '_usj_walk', lambda serial: next(walks)), \
                self.assertRaisesRegex(RuntimeError, 're-enumerated during resolution'):
            oe.usj_location(SERIAL)

    def test_a_location_openocd_would_truncate_is_refused(self):
        self.bus.add('5-1.2.3.4.5.6.17')
        with self.assertRaisesRegex(RuntimeError, 'not a location openocd can match'):
            oe.usj_location(SERIAL)


class ServerArgv(unittest.TestCase):
    TOOL = {'exe': '/opt/ocd/bin/openocd', 'scripts': '/opt/ocd/share/openocd/scripts'}

    def argv(self, cfgs):
        return oe.server_argv(self.TOOL, cfgs, '5-3.4', SERIAL, tcl_port=4444, token='t0k', log='/w/openocd.log')

    def test_location_before_serial_both_after_the_interface_and_before_init(self):
        argv = self.argv(['board/esp32p4-builtin.cfg'])
        self.assertEqual(argv, [
            '/opt/ocd/bin/openocd', '-s', '/opt/ocd/share/openocd/scripts', '-c', 'gdb port disabled',
            '-c', 'telnet port disabled', '-c', 'tcl port 4444', '-c', 'set ESPS_SESSION t0k',
            '-f', '/opt/ocd/share/openocd/scripts/board/esp32p4-builtin.cfg', '-c', 'adapter usb location 5-3.4', '-c', f'adapter serial {SERIAL}',
            '-c', 'noinit', '-l', '/w/openocd.log'])

    def test_with_a_pair_the_selectors_sit_between_interface_and_target(self):
        argv = self.argv(['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg'])
        i, scripts = argv.index, self.TOOL['scripts']
        self.assertLess(i(f'{scripts}/interface/esp_usb_jtag.cfg'), i('adapter usb location 5-3.4'))
        self.assertLess(i('adapter usb location 5-3.4'), i(f'adapter serial {SERIAL}'))
        self.assertLess(i(f'adapter serial {SERIAL}'), i(f'{scripts}/target/esp32c3.cfg'))
        self.assertLess(i(f'{scripts}/target/esp32c3.cfg'), i('noinit'))
        self.assertNotIn('init', argv)


if __name__ == '__main__':
    unittest.main()
