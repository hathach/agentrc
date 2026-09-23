"""--hil-config/--board on rtt.py and pc_sample.py: the same resolver in both (they are
copied around alone), the same cases for both, and a refusal before anything is launched."""
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {'rtt': ROOT / 'skills' / 'rtt' / 'scripts' / 'rtt.py',
           'pc_sample': ROOT / 'skills' / 'target-debug' / 'scripts' / 'pc_sample.py'}
TINYUSB_CONFIG = Path.home() / 'code' / 'tinyusb' / 'test' / 'hil' / 'tinyusb.json'


def load(name):
    spec = importlib.util.spec_from_file_location(f'hil_{name}', SCRIPTS[name])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MODULES = {name: load(name) for name in SCRIPTS}

# one entry per flasher kind a HIL config carries, in the project's own format
BOARDS = [
    {'name': 'k64f', 'flasher': {'name': 'jlink', 'uid': '000100000001', 'args': '-device MK64FN1M0xxx12'}},
    {'name': 'max32', 'flasher': {'name': 'openocd', 'uid': 'E0000000000000A1', 'vid_pid': '0x2e8a 0x000c',
                                  'args': '-f interface/cmsis-dap.cfg -f target/max32665.cfg'}},
    {'name': 'h743', 'flasher': {'name': 'stlink', 'uid': '00AA00BB00CC00DD00EE00FF'}},
    {'name': 'p4', 'flasher': {'name': 'esptool', 'uid': '0a0b0c0d', 'args': '-b 1500000'}},
    {'name': 'tm4c', 'flasher': {'name': 'lm4flash', 'uid': '0A0B0C0D', 'args': '-v'}},
    {'name': 'bare', 'flasher': {'name': 'jlink'}},
    {'name': 'jtag', 'flasher': {'name': 'jlink', 'uid': '1', 'args': '-device X -if jtag'}},
    {'name': 'quote', 'flasher': {'name': 'jlink', 'uid': '1', 'args': '-device "X'}},
    {'name': 'blank_uid', 'flasher': {'name': 'jlink', 'uid': '  ', 'args': '-device X'}},
    {'name': 'int_uid', 'flasher': {'name': 'jlink', 'uid': 621, 'args': '-device X'}},
    {'name': 'no_kind', 'flasher': {'uid': '1'}},
    {'name': 'no_flasher'},
    {'name': 'twin', 'flasher': {'name': 'jlink', 'uid': '1', 'args': '-device X'}},
    {'name': 'twin', 'flasher': {'name': 'jlink', 'uid': '2', 'args': '-device X'}},
    {'name': 'smuggle', 'flasher': {'name': 'jlink', 'uid': '1', 'args': '-device "FAKE -USB 999"'}},
    {'name': 'blank_dev', 'flasher': {'name': 'jlink', 'uid': '1', 'args': '-device "  "'}},
    {'name': 'bad_cfg', 'flasher': {'name': 'openocd', 'uid': '1', 'args': '-f "unterminated'}},
    {'name': 'bad_ids', 'flasher': {'name': 'openocd', 'uid': '1', 'vid_pid': '2e8a:000c', 'args': '-f x.cfg'}},
    {'name': 'stlink_args', 'flasher': {'name': 'stlink', 'uid': '3', 'args': '--connect-under-reset'}},
]

# (board, what the resolver says) - a string is the refusal, a dict the flasher it returns
RESOLVE_CASES = [
    ('k64f', BOARDS[0]['flasher']),
    ('max32', BOARDS[1]['flasher']),
    ('h743', BOARDS[2]['flasher']),
    ('bare', {'name': 'jlink'}),
    ('p4', "flashed over 'esptool', which is no debug-probe route"),
    ('tm4c', "flashed over 'lm4flash'"),
    ('blank_uid', "flasher.uid in"),
    ('int_uid', "flasher.uid in"),
    ('no_kind', 'no flasher.name'),
    ('no_flasher', 'no flasher.name'),
    ('twin', '2 entries of that name'),
    ('absent', '0 entries of that name'),
]
BAD_FILES = [('[]', 'expected {"boards"'), ('{"boards": {}}', 'expected {"boards"'),
             ('{"boards": ["k64f"]}', 'expected {"boards"'), ('{"boards": [', '--hil-config'), ('{}', 'expected {"boards"')]


class Config(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        self.config = self.tmp / 'hil.json'
        self.config.write_text(json.dumps({'boards': BOARDS}))


class Resolver(Config):
    def test_both_scripts_carry_the_same_resolver(self):
        for func in ('hil_flasher', 'hil_jlink_device', 'hil_merge'):
            self.assertEqual(inspect.getsource(getattr(MODULES['rtt'], func)),
                             inspect.getsource(getattr(MODULES['pc_sample'], func)), func)
        self.assertEqual(MODULES['rtt'].HIL_PROBE_ROUTES, MODULES['pc_sample'].HIL_PROBE_ROUTES)

    def test_a_board_resolves_or_is_refused_the_same_way_in_both(self):
        for name, mod in MODULES.items():
            for board, want in RESOLVE_CASES:
                with self.subTest(script=name, board=board):
                    if isinstance(want, dict):
                        self.assertEqual(mod.hil_flasher(self.config, board), want)
                    else:
                        with self.assertRaises(ValueError) as cm:
                            mod.hil_flasher(self.config, board)
                        self.assertIn(want, str(cm.exception))

    def test_a_file_of_the_wrong_shape_is_refused(self):
        for name, mod in MODULES.items():
            for text, want in BAD_FILES:
                with self.subTest(script=name, text=text):
                    self.config.write_text(text)
                    with self.assertRaises(ValueError) as cm:
                        mod.hil_flasher(self.config, 'k64f')
                    self.assertIn(want, str(cm.exception))
            with self.assertRaises(ValueError) as cm:
                mod.hil_flasher(self.tmp / 'absent.json', 'k64f')
            self.assertIn('absent.json', str(cm.exception))

    def test_a_jlink_entry_carries_a_device_and_nothing_else(self):
        for name, mod in MODULES.items():
            flasher = {b['name']: b.get('flasher') for b in BOARDS if b['name'] != 'twin'}
            self.assertEqual(mod.hil_jlink_device('k64f', flasher['k64f']), 'MK64FN1M0xxx12')
            self.assertIsNone(mod.hil_jlink_device('bare', flasher['bare']))
            for board, want in (('jtag', "must be exactly '-device NAME'"), ('quote', 'No closing quotation')):
                with self.assertRaises(ValueError) as cm:
                    mod.hil_jlink_device(board, flasher[board])
                self.assertIn(want, str(cm.exception), name)

    @unittest.skipUnless(TINYUSB_CONFIG.is_file(), 'no tinyusb checkout beside this one')
    def test_every_board_of_the_real_tinyusb_config_resolves_or_is_refused_cleanly(self):
        routes = {}
        for board in json.loads(TINYUSB_CONFIG.read_text())['boards']:
            try:
                kind = MODULES['rtt'].hil_flasher(TINYUSB_CONFIG, board['name'])['name']
            except ValueError as e:
                kind = 'refused'
                self.assertIn('no debug-probe route', str(e))
            routes.setdefault(kind, []).append(board['name'])
        self.assertIn('jlink', routes)
        for name in routes['jlink']:
            flasher = MODULES['rtt'].hil_flasher(TINYUSB_CONFIG, name)
            self.assertTrue(MODULES['rtt'].hil_jlink_device(name, flasher), name)


@unittest.skipIf(os.name == 'nt', 'probe CLI stubs require POSIX executable semantics')
class Cli(Config):
    """Every probe tool is a recorder that must not run on a refusal."""

    def setUp(self):
        super().setUp()
        self.launched = self.tmp / 'launched'
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for tool in ('JLinkExe', 'openocd'):
            f = self.bin / tool
            f.write_text(f'#!{sys.executable}\nimport json, sys\n'
                         f'open({str(self.launched)!r}, "a").write(json.dumps(sys.argv) + "\\n")\nsys.exit(1)\n')
            f.chmod(0o755)
        self.elf = self.tmp / 'fw.elf'
        self.elf.write_bytes(b'\x7fELF')
        self.env = dict(os.environ, PATH=f'{self.bin}{os.pathsep}{os.environ["PATH"]}')

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(SCRIPTS[name]), '--hil-config', str(self.config), *args],
                              capture_output=True, text=True, timeout=60, env=self.env, cwd=self.tmp)

    def argvs(self):
        return [json.loads(l) for l in self.launched.read_text().splitlines()] if self.launched.exists() else []

    def command(self):
        """Every launch as text; argvs() where argument boundaries matter."""
        return '\n'.join(' '.join(argv) for argv in self.argvs())

    def refused(self, name, args, want):
        r = self.run_script(name, *args)
        self.assertEqual(r.returncode, 2, (name, args, r.stderr))
        self.assertIn(want, r.stderr, (name, args))
        self.assertEqual(self.command(), '', 'a refusal must not reach the probe')
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['bin', 'fw.elf', 'hil.json'])

    LINK = ('--interface', 'swd', '--speed', '4000')

    def test_a_jlink_board_supplies_the_probe_and_the_device(self):
        self.run_script('rtt', '--board', 'k64f', *self.LINK, '--seconds', '1')
        self.assertIn('-USB 000100000001', self.command())
        self.assertIn('-device MK64FN1M0xxx12 -if swd -speed 4000', self.command())
        self.launched.unlink()
        self.run_script('pc_sample', '--board', 'k64f', *self.LINK, '--elf', str(self.elf), '--samples', '1')
        self.assertIn('-device MK64FN1M0xxx12 -SelectEmuBySN 000100000001', self.command())

    def test_an_openocd_board_supplies_probe_ids_and_cfg_and_a_stlink_board_needs_the_cfg(self):
        self.run_script('rtt', '--board', 'max32', '--addr', '20000000', '--seconds', '1')
        self.assertIn('adapter usb vid_pid 0x2e8a 0x000c', self.command())
        self.assertIn('adapter serial E0000000000000A1 -f interface/cmsis-dap.cfg -f target/max32665.cfg', self.command())
        self.launched.unlink()
        self.refused('rtt', ['--board', 'h743', '--addr', '20000000'], '--backend openocd needs --cfg')
        self.run_script('rtt', '--board', 'h743', '--addr', '20000000', '--seconds', '1',
                        '--cfg', '-f interface/stlink.cfg -f target/stm32h7x.cfg')
        self.assertIn('adapter serial 00AA00BB00CC00DD00EE00FF -f interface/stlink.cfg', self.command())

    def test_flags_fill_what_the_file_leaves_out_and_must_agree_with_what_it_has(self):
        self.run_script('rtt', '--board', 'bare', '--probe', '77', '--device', 'Y', *self.LINK, '--seconds', '1')
        self.assertIn('-USB 77', self.command())
        self.assertIn('-device Y', self.command())
        self.launched.unlink()
        self.run_script('rtt', '--board', 'k64f', '--probe', '000100000001', '--backend', 'jlink', *self.LINK, '--seconds', '1')
        self.assertIn('-USB 000100000001', self.command())       # saying the same thing twice is fine
        self.launched.unlink()
        for name, extra in (('rtt', ('--seconds', '1')), ('pc_sample', ('--elf', str(self.elf)))):
            self.refused(name, ['--board', 'k64f', '--probe', '999', *self.LINK, *extra],
                         "--probe '999' contradicts --board k64f, which has '000100000001'")
            self.refused(name, ['--board', 'k64f', '--device', 'OTHER', *self.LINK, *extra],
                         "--device 'OTHER' contradicts --board k64f")
            self.refused(name, ['--board', 'bare', *self.LINK, *extra], '--probe')
        self.refused('rtt', ['--board', 'k64f', '--backend', 'openocd', '--seconds', '1'], "--backend 'openocd' contradicts")
        self.refused('rtt', ['--board', 'max32', '--addr', '20000000', '--cfg', '-f other.cfg'], "--cfg '-f other.cfg' contradicts")

    def test_a_device_with_spaces_stays_one_argument(self):
        self.run_script('rtt', '--board', 'smuggle', *self.LINK, '--seconds', '1')
        (argv,) = self.argvs()
        self.assertEqual(argv[argv.index('-device') + 1], 'FAKE -USB 999')
        self.assertEqual([argv[n + 1] for n, a in enumerate(argv) if a == '-USB'], ['1'])
        self.launched.unlink()
        self.run_script('pc_sample', '--board', 'smuggle', *self.LINK, '--elf', str(self.elf), '--samples', '1')
        (argv,) = self.argvs()
        self.assertEqual(argv[argv.index('-device') + 1], 'FAKE -USB 999')
        self.assertEqual([argv[n + 1] for n, a in enumerate(argv) if a == '-SelectEmuBySN'], ['1'])
        self.assertNotIn('-USB', argv)

    def test_openocd_inputs_are_checked_before_anything_starts(self):
        self.refused('rtt', ['--board', 'bad_cfg', '--addr', '20000000'], 'No closing quotation')
        self.refused('rtt', ['--board', 'bad_ids', '--addr', '20000000'], '--vid-pid must be "0xVVVV 0xPPPP"')
        self.refused('rtt', ['--board', 'h743', '--addr', '20000000', '--cfg', '-f "open'], 'No closing quotation')
        self.refused('rtt', ['--board', 'h743', '--addr', '20000000', '--cfg', '-f x.cfg', '--vid-pid', 'junk'],
                     '--vid-pid must be')
        self.refused('rtt', ['--board', 'h743', '--addr', '20000000', '--cfg', '   '], '--cfg is empty')

    def test_what_cannot_be_a_probe_route_is_refused_by_both(self):
        for name, extra in (('rtt', ('--seconds', '1')), ('pc_sample', ('--elf', str(self.elf), *self.LINK))):
            for board, want in (('p4', "flashed over 'esptool'"), ('tm4c', "flashed over 'lm4flash'"),
                                ('jtag', "must be exactly '-device NAME'"), ('quote', 'No closing quotation'),
                                ('blank_uid', 'must be a non-empty string'), ('blank_dev', "must be exactly '-device NAME'"), ('twin', '2 entries of that name'),
                                ('absent', '0 entries of that name')):
                self.refused(name, ['--board', board, *extra], want)
        self.refused('rtt', ['--board', 'stlink_args', '--addr', '20000000', '--cfg', '-f x.cfg'], "st-flash's, not")
        self.refused('pc_sample', ['--board', 'max32', '--elf', str(self.elf), *self.LINK], 'samples over a J-Link only')
        self.refused('pc_sample', ['--board', 'h743', '--elf', str(self.elf), *self.LINK], 'samples over a J-Link only')

    def test_the_two_flags_go_together(self):
        for name, extra in (('rtt', ('--backend', 'jlink')), ('pc_sample', ('--elf', str(self.elf), *self.LINK))):
            self.refused(name, [*extra], '--hil-config and --board go together')
            r = subprocess.run([sys.executable, str(SCRIPTS[name]), '--board', 'k64f', *extra],
                               capture_output=True, text=True, timeout=60, env=self.env)
            self.assertEqual(r.returncode, 2)
            self.assertIn('--hil-config and --board go together', r.stderr)


if __name__ == '__main__':
    unittest.main()
