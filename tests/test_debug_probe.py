"""skills/debug-probe/scripts/probe.py against a fake sysfs and stubbed wlink-iap, wlink and wchisp: selection,
refusals and the values handed to the backends. Stubs prove the plumbing only; flashing and config writes are
hardware paths."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/debug-probe'
spec = importlib.util.spec_from_file_location('probe_under_test', SKILL / 'scripts/probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

# Each stub logs its argv to log.jsonl and answers from state.json, which a test edits between calls.
STUB = '''
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
with open(here / 'log.jsonl', 'a') as f:
    f.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
state = json.loads((here / 'state.json').read_text())
args = sys.argv[1:]
'''
WLINK_IAP = STUB + '''
if not args:
    print('wlink-iap -p <port> [-s <serial>] -V' if state.get('patched', True) else 'wlink-iap -f <file>')
    sys.exit(2)
port = args[args.index('-p') + 1]
if '-V' in args:
    v = state['version'][port]
    print(f"model={v['model']} version={v['v'] // 10}.{v['v'] % 10} v={v['v']}")
    sys.exit(0)
if '-f' in args:
    import hashlib
    state['flashed'] = hashlib.sha256(Path(args[args.index('-f') + 1]).read_bytes()).hexdigest()
    state['version'][port] = state.get('after', state['version'].get(port))
    (here / 'state.json').write_text(json.dumps(state))
    sys.exit(state.get('flash_exit', 0))
sys.exit(1)
'''
WLINK = STUB + '''
for i, s in enumerate(state.get('wlink_list', [])):
    print(f'<WCH-Link#{i} nusb device> ID 1a86:8010 Serial {s} (USB-FS 12 Mbps) (RV mode)')
'''
WCHISP = STUB + '''
if args[:2] == ['config', '--help']:
    print('set-word' if state.get('patched', True) else 'set')
    sys.exit(0)
if args == ['-u', 'info']:
    c = state['chip']
    print(f"Chip: {c['name']}[0x{c['id']:02x}{c['type']:02x}] (Code Flash: 448KiB)")
    print(f"Chip UID: {c['uid']}")
    print(f"{c['register']}: 0x{c['word']:08X}")
    sys.exit(0)
if 'set-word' in args:
    sys.exit(state.get('set_exit', 0))
sys.exit(1)
'''
IMAGE_V42 = b'\x01' * 64
IMAGE_V39 = b'\x02' * 64
TOOLS = f'''
[[firmware]]
model = "WCH-LinkE-CH32V305"
v = 42
sha256 = "{hashlib.sha256(IMAGE_V42).hexdigest()}"
[[firmware]]
model = "WCH-LinkE-CH32V305"
v = 39
sha256 = "{hashlib.sha256(IMAGE_V39).hexdigest()}"
'''
LINKE = 'WCH-LinkE-CH32V305'
ROOT_OPENS_ANY_NODE = unittest.skipIf(not hasattr(os, 'geteuid') or os.geteuid() == 0,
                                      'needs POSIX node permissions that root does not bypass')


def setUpModule():
    guard_survivors(__name__)


class ProbeTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for name, source in (('wlink-iap', WLINK_IAP), ('wlink', WLINK), ('wchisp', WCHISP)):
            make_stub(self.bin, name, source)
        self.sysfs = self.tmp / 'sysfs'
        self.sysfs.mkdir()
        self.dev_usb = self.tmp / 'dev-bus-usb'
        (self.tmp / 'tools.toml').write_text(TOOLS)
        for image, data in (('v42.bin', IMAGE_V42), ('v39.bin', IMAGE_V39)):
            (self.tmp / image).write_bytes(data)
        self.state = {'version': {}}
        self.save()
        for patch in (mock.patch.object(probe.usb_observe, 'SYS_USB_DEVICES', str(self.sysfs)),
                      mock.patch.object(probe, 'DEV_USB', str(self.dev_usb)),
                      mock.patch.object(probe, 'TOOLS', self.tmp / 'tools.toml'),
                      mock.patch.object(probe.time, 'sleep', lambda s: None),
                      mock.patch.dict(os.environ, {'PATH': str(self.bin)})):
            patch.start()
            self.addCleanup(patch.stop)

    def save(self):
        (self.bin / 'state.json').write_text(json.dumps(self.state))

    def device(self, port, vid, pid, serial=None):
        d = self.sysfs / port
        d.mkdir(exist_ok=True)
        (d / 'idVendor').write_text(vid + '\n')
        (d / 'idProduct').write_text(pid + '\n')
        if serial:
            (d / 'serial').write_text(serial + '\n')
        devnum = len(list(self.sysfs.iterdir()))
        (d / 'busnum').write_text('5\n')
        (d / 'devnum').write_text(f'{devnum}\n')
        (self.dev_usb / '005').mkdir(parents=True, exist_ok=True)
        (self.dev_usb / '005' / f'{devnum:03d}').touch(mode=0o600)
        return d

    def node(self, d):
        return self.dev_usb / '005' / f'{int((d / "devnum").read_text()):03d}'

    def linke(self, port, serial, v):
        d = self.device(port, '1a86', '8010', serial)
        self.state['version'][port] = {'model': LINKE, 'v': v}
        self.save()
        return d

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['probe.py', *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = probe.main()
        return code, out.getvalue(), err.getvalue()

    def flashed(self):
        return json.loads((self.bin / 'state.json').read_text()).get('flashed')

    def calls(self, name):
        log = self.bin / 'log.jsonl'
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [r[1:] for r in rows if r[0] == name]

    # probes

    def test_probes_lists_modes_index_and_version(self):
        self.linke('5-2.4', 'AAA', 42)
        self.linke('5-2.5', 'BBB', 39)
        self.device('5-2.6', '4348', '55e0')
        self.device('5-2.7', '1d6b', '0002')
        self.state['wlink_list'] = ['AAA', 'BBB']
        self.save()
        code, out, _ = self.main('probes', '--version', '--json')
        self.assertEqual(code, 0)
        got = {d['port']: d for d in json.loads(out)}
        self.assertEqual(sorted(got), ['5-2.4', '5-2.5', '5-2.6'])
        self.assertEqual((got['5-2.4']['index'], got['5-2.4']['v']), (0, 42))
        self.assertEqual((got['5-2.5']['index'], got['5-2.5']['v']), (1, 39))
        self.assertEqual(got['5-2.6']['mode'], 'isp')
        self.assertNotIn('v', got['5-2.6'])
        self.assertIn(['-p', '5-2.5', '-V', '-s', 'BBB'], self.calls('wlink-iap'))

    def test_probes_version_refuses_unpatched_wlink_iap(self):
        self.linke('5-2.4', 'AAA', 42)
        self.state['patched'] = False
        self.save()
        code, _, err = self.main('probes', '--version')
        self.assertEqual(code, 1)
        self.assertIn('not the patched build', err)

    # fw-update

    def test_fw_update_flashes_selected_probe_and_checks_new_version(self):
        self.linke('5-2.4', 'AAA', 42)
        self.linke('5-2.5', 'BBB', 39)
        self.state['after'] = {'model': LINKE, 'v': 42}
        self.save()
        code, out, err = self.main('fw-update', '--serial', 'BBB', '--image', str(self.tmp / 'v42.bin'))
        self.assertEqual(code, 0, err)
        self.assertIn('BBB at 5-2.5: v39 -> v42', out)
        flash = [c for c in self.calls('wlink-iap') if '-f' in c]
        self.assertEqual([c[:5] for c in flash], [['-p', '5-2.5', '-s', 'BBB', '-f']])
        self.assertEqual(self.flashed(), hashlib.sha256(IMAGE_V42).hexdigest())

    def test_fw_update_flashes_the_checked_bytes_when_the_file_is_replaced(self):
        self.linke('5-2.5', 'BBB', 39)
        self.state['after'] = {'model': LINKE, 'v': 42}
        self.save()
        image = self.tmp / 'v42.bin'
        real_entry = probe.firmware_entry

        def entry_then_replace(*a):
            result = real_entry(*a)
            image.write_bytes(b'\x66' * 64)
            return result
        with mock.patch.object(probe, 'firmware_entry', entry_then_replace):
            code, _, err = self.main('fw-update', '--serial', 'BBB', '--image', str(image))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.flashed(), hashlib.sha256(IMAGE_V42).hexdigest())

    @ROOT_OPENS_ANY_NODE
    def test_fw_update_waits_for_udev_to_open_the_node(self):
        d = self.linke('5-2.5', 'BBB', 39)
        self.state['after'] = {'model': LINKE, 'v': 42}
        self.save()
        self.node(d).chmod(0)
        polls = []

        def sleep(_):
            polls.append(1)
            if len(polls) == 2:
                self.node(d).chmod(0o600)
        with mock.patch.object(probe.time, 'sleep', sleep):
            code, out, err = self.main('fw-update', '--serial', 'BBB', '--image', str(self.tmp / 'v42.bin'))
        self.assertEqual(code, 0, err)
        self.assertIn('v39 -> v42', out)
        self.assertEqual(len(polls), 2)

    @ROOT_OPENS_ANY_NODE
    def test_fw_update_names_a_node_that_never_opens(self):
        d = self.linke('5-2.5', 'BBB', 39)
        self.state['after'] = {'model': LINKE, 'v': 42}
        self.save()
        self.node(d).chmod(0)
        code, _, err = self.main('fw-update', '--serial', 'BBB', '--image', str(self.tmp / 'v42.bin'),
                                 '--timeout', '0')
        self.assertEqual(code, 1)
        self.assertIn('back at 5-2.5 but its usbfs node is not accessible after 0 s', err)

    def test_fw_update_fails_on_another_model_after_flashing(self):
        self.linke('5-2.5', 'BBB', 39)
        self.state['after'] = {'model': 'WCH-LinkB-CH32V208', 'v': 42}
        self.save()
        code, _, err = self.main('fw-update', '--serial', 'BBB', '--image', str(self.tmp / 'v42.bin'))
        self.assertEqual(code, 1)
        self.assertIn('reports WCH-LinkB-CH32V208 v42 after flashing a WCH-LinkE-CH32V305 v42 image', err)

    def test_fw_update_fails_when_version_did_not_change(self):
        self.linke('5-2.5', 'BBB', 39)
        code, _, err = self.main('fw-update', '--serial', 'BBB', '--image', str(self.tmp / 'v42.bin'))
        self.assertEqual(code, 1)
        self.assertIn('reports WCH-LinkE-CH32V305 v39 after flashing a WCH-LinkE-CH32V305 v42 image', err)

    def test_fw_update_refusals_before_flashing(self):
        self.linke('5-2.4', 'AAA', 42)
        (self.tmp / 'unknown.bin').write_bytes(b'\x03' * 64)
        cases = [(('--serial', 'ZZZ', '--image', 'v42.bin'), '0 RV/DAP-mode probes with serial ZZZ'),
                 (('--serial', 'AAA', '--image', 'unknown.bin'), 'is not an image tools.toml lists'),
                 (('--serial', 'AAA', '--image', 'v39.bin'), '--allow-downgrade'),
                 (('--serial', 'AAA', '--image', 'v42.bin'), '--reinstall')]
        for args, message in cases:
            with self.subTest(message):
                args = [a if not a.endswith('.bin') else str(self.tmp / a) for a in args]
                code, _, err = self.main('fw-update', *args)
                self.assertEqual(code, 1)
                self.assertIn(message, err)
        self.assertFalse([c for c in self.calls('wlink-iap') if '-f' in c])

    def test_fw_update_refuses_image_of_another_model(self):
        self.linke('5-2.4', 'AAA', 42)
        self.state['version']['5-2.4']['model'] = 'WCH-LinkB-CH32V208'
        self.save()
        code, _, err = self.main('fw-update', '--serial', 'AAA', '--image', str(self.tmp / 'v39.bin'),
                                 '--allow-downgrade')
        self.assertEqual(code, 1)
        self.assertIn('the probe is WCH-LinkB-CH32V208', err)

    # fw-recover

    def test_fw_recover_needs_the_iap_device_at_the_port(self):
        self.device('5-2.6', '4348', '55e0')
        code, _, err = self.main('fw-recover', '--port', '5-2.5', '--model', LINKE,
                                 '--image', str(self.tmp / 'v39.bin'))
        self.assertEqual(code, 1)
        self.assertIn('no 4348:55e0 device at 5-2.5', err)
        self.assertFalse([c for c in self.calls('wlink-iap') if '-f' in c])

    @ROOT_OPENS_ANY_NODE
    def test_fw_recover_refuses_an_iap_node_it_cannot_open(self):
        d = self.device('5-2.5', '4348', '55e0')
        self.node(d).chmod(0)
        code, _, err = self.main('fw-recover', '--port', '5-2.5', '--model', LINKE,
                                 '--image', str(self.tmp / 'v39.bin'))
        self.assertEqual(code, 1)
        self.assertIn('4348:55e0 at 5-2.5: its usbfs node', err)
        self.assertFalse([c for c in self.calls('wlink-iap') if '-r' in c])

    def test_fw_recover_flashes_and_waits_for_rv_mode(self):
        d = self.device('5-2.5', '4348', '55e0')
        self.device('5-2.6', '4348', '55e0')                       # a chip in its bootloader elsewhere
        self.state['after'] = {'model': LINKE, 'v': 39}
        self.save()
        real_run = probe.run

        def run(argv, timeout=120):
            result = real_run(argv, timeout)
            if '-r' in argv:   # the probe re-enumerates in RV mode
                (d / 'idVendor').write_text('1a86\n')
                (d / 'idProduct').write_text('8010\n')
            return result
        with mock.patch.object(probe, 'run', run):
            code, out, err = self.main('fw-recover', '--port', '5-2.5', '--model', LINKE, '--image',
                                       str(self.tmp / 'v39.bin'))
        self.assertEqual(code, 0, err)
        self.assertIn('probe at 5-2.5: unknown -> v39', out)
        self.assertEqual([c[:4] for c in self.calls('wlink-iap') if '-f' in c], [['-p', '5-2.5', '-r', '-f']])
        self.assertEqual(self.flashed(), hashlib.sha256(IMAGE_V39).hexdigest())

    def test_usb_devices_keeps_probes_without_a_readable_serial(self):
        self.linke('5-2.4', 'AAA', 42)
        self.device('5-2.5', '1a86', '8010')                       # no serial descriptor
        real = probe.usb_observe.read_sysfs
        with mock.patch.object(probe.usb_observe, 'read_sysfs',
                               lambda path, *a: None if '5-2.4' in path else real(path, *a)):   # did not answer
            got = [(d['port'], d['serial']) for d in probe.usb_devices()]
        self.assertEqual(got, [('5-2.4', ''), ('5-2.5', '')])

    # config

    def chip(self, name, chip_id, dtype, register, word, uid='11-22-33-44-55-66-77-88'):
        d = self.device('5-2.6', '4348', '55e0')
        self.state['chip'] = {'name': name, 'id': chip_id, 'type': dtype, 'uid': uid, 'register': register,
                              'word': word}
        self.save()
        return d

    def set_word_calls(self):
        return [c for c in self.calls('wchisp') if 'set-word' in c]

    def test_ch582_set_reset_en_keeps_other_bits(self):
        self.chip('CH582', 0x82, 0x16, 'USER_CFG', 0x4FFF0FD5)
        code, out, err = self.main('config', '--chip', 'CH582', '--set', 'CFG_RESET_EN=1')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.set_word_calls(), [['-u', 'config', 'set-word', '--register', 'USER_CFG', '--expect-uid',
                                                  '11-22-33-44-55-66-77-88', '--expect', '4FFF0FD5',
                                                  '--value', '4FFF0FDD']])
        self.assertIn('0x4FFF0FD5 -> 0x4FFF0FDD', out)

    def test_ch582_show_decodes_without_writing(self):
        self.chip('CH582', 0x82, 0x16, 'USER_CFG', 0x4FFF0FD5)
        code, out, _ = self.main('config', '--chip', 'CH582', '--show')
        self.assertEqual(code, 0)
        self.assertIn("{'CFG_RESET_EN': 0, 'CFG_DEBUG_EN': 1, 'CFG_ROM_READ': 1}", out)
        self.assertEqual(self.set_word_calls(), [])

    def test_ch582_refusals(self):
        cases = [(0x4FFF0FD5, 'CFG_ROM_READ=0', 'CFG_ROM_READ=0 is not allowed'),
                 (0x4FFF0F55, 'CFG_RESET_EN=1', 'CFG_ROM_READ=0 is not allowed'),   # read-protected already
                 (0x4FFF0FD5, 'CFG_BOOT_EN=0', 'settable fields are'),
                 (0x4FFF0FD5, 'CFG_RESET_EN=2', 'does not fit'),
                 (0xD50FFF4F, 'CFG_RESET_EN=1', 'VALID_SIG'),
                 (0x4FFF0F95, 'CFG_RESET_EN=1', 'CFG_BOOT_EN is off')]
        for word, assignment, message in cases:
            with self.subTest(assignment, word=hex(word)):
                self.chip('CH582', 0x82, 0x16, 'USER_CFG', word)
                code, _, err = self.main('config', '--chip', 'CH582', '--set', assignment)
                self.assertEqual(code, 1)
                self.assertIn(message, err)
        self.assertEqual(self.set_word_calls(), [])

    def test_ch582_no_change_writes_nothing(self):
        self.chip('CH582', 0x82, 0x16, 'USER_CFG', 0x4FFF0FDD)
        code, out, _ = self.main('config', '--chip', 'CH582', '--set', 'CFG_RESET_EN=1')
        self.assertEqual(code, 0)
        self.assertIn('nothing to change', out)
        self.assertEqual(self.set_word_calls(), [])

    def test_config_refuses_other_chip_two_isp_devices_and_unpatched_wchisp(self):
        self.chip('CH583', 0x83, 0x16, 'USER_CFG', 0x4FFF0FD5)
        code, _, err = self.main('config', '--chip', 'CH582', '--show')
        self.assertEqual((code, 'not CH582' in err), (1, True), err)
        self.device('5-2.7', '1a86', '55e0')
        code, _, err = self.main('config', '--chip', 'CH582', '--show')
        self.assertEqual((code, '2 ISP devices' in err), (1, True), err)
        self.state['patched'] = False
        self.save()
        code, _, err = self.main('config', '--chip', 'CH582', '--show')
        self.assertEqual((code, 'no config set-word' in err), (1, True), err)

    def test_config_waits_for_no_device_then_fails(self):
        code, _, err = self.main('config', '--chip', 'CH582', '--show', '--wait', '0')
        self.assertEqual(code, 1)
        self.assertIn('no WCH ISP device appeared', err)

    @ROOT_OPENS_ANY_NODE
    def test_config_waits_for_the_isp_node_to_open(self):
        d = self.chip('CH582', 0x82, 0x16, 'USER_CFG', 0x4FFF0FD5)
        self.node(d).chmod(0)
        code, _, err = self.main('config', '--chip', 'CH582', '--show', '--wait', '0')
        self.assertEqual(code, 1)
        self.assertIn('ISP device at 5-2.6 but its usbfs node is not accessible', err)
        self.assertEqual([c for c in self.calls('wchisp') if c[:2] == ['-u', 'info']], [])
        polls = []

        def sleep(_):
            polls.append(1)
            self.node(d).chmod(0o600)
        with mock.patch.object(probe.time, 'sleep', sleep):
            code, out, err = self.main('config', '--chip', 'CH582', '--show', '--wait', '5')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(polls), 1)

    def test_config_reports_failed_write(self):
        self.chip('CH582', 0x82, 0x16, 'USER_CFG', 0x4FFF0FD5)
        self.state['set_exit'] = 1
        self.save()
        code, _, err = self.main('config', '--chip', 'CH582', '--set', 'CFG_RESET_EN=1')
        self.assertEqual(code, 1)
        self.assertIn('set-word exited 1', err)

    def test_ch569_enables_debug_and_code_read(self):
        self.chip('CH569', 0x69, 0x10, 'NV_INFO', 0x8FFFF245)
        code, _, err = self.main('config', '--chip', 'CH569', '--set', 'DEBUG_EN=1,CODE_READ_EN=1')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.set_word_calls()[0][-4:], ['--expect', '8FFFF245', '--value', '8FFFF2E5'])

    def test_ch569_refuses_debug_with_reset(self):
        self.chip('CH569', 0x69, 0x10, 'NV_INFO', 0x8FFFF2E5)
        code, _, err = self.main('config', '--chip', 'CH569', '--set', 'RESET_EN=1')
        self.assertEqual(code, 1)
        self.assertIn('DEBUG_EN with RESET_EN', err)
        self.assertEqual(self.set_word_calls(), [])

    def test_ch569_refuses_a_word_without_its_signature(self):
        self.chip('CH569', 0x69, 0x10, 'NV_INFO', 0x8FFFF240)
        code, _, err = self.main('config', '--chip', 'CH569', '--set', 'DEBUG_EN=1')
        self.assertEqual(code, 1)
        self.assertIn('[3:0] are not 0101b', err)
        self.assertEqual(self.set_word_calls(), [])

    def test_x035_rst_mode_rewrites_user_and_its_inverse(self):
        self.chip('CH32X035C8T6', 81, 0x23, 'RDPR_USER', 0x00FF5AA5)
        code, _, err = self.main('config', '--chip', 'CH32X035C8T6', '--set', 'RST_MODE=0')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.set_word_calls()[0][-4:], ['--expect', '00FF5AA5', '--value', '18E75AA5'])

    def test_x035_refusals(self):
        cases = [(0x00FF5AA5, 'RST_MODE=1', 'RST_MODE=1 is not allowed'),
                 (0x00FF5AFF, 'RST_MODE=0', 'not the unprotected A5/5A'),
                 (0xFFFF5AA5, 'RST_MODE=0', 'is not the inverse')]
        for word, assignment, message in cases:
            with self.subTest(assignment, word=hex(word)):
                self.chip('CH32X035C8T6', 81, 0x23, 'RDPR_USER', word)
                code, _, err = self.main('config', '--chip', 'CH32X035C8T6', '--set', assignment)
                self.assertEqual(code, 1)
                self.assertIn(message, err)
        self.assertEqual(self.set_word_calls(), [])


class ToolsTomlTest(unittest.TestCase):

    def test_firmware_entries_are_app_only_images(self):
        for entry in tomllib.loads((SKILL / 'tools.toml').read_text())['firmware']:
            self.assertNotIn('IAP', entry['file'])
            self.assertEqual(len(entry['sha256']), 64)
