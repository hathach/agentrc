"""skills/hil/scripts/esptool_session.py is the esptool child a HIL runner times out and kills, moved from
tinyuf2's .claude/skills/hil-contract/scripts/esp_session.py (ce6db76) with its tests (test_hil.py EspSession):
its dispatch, arguments, guards, reset order and JSON/exit contract, run as a real child over a stub esptool
package. Stubs prove the plumbing only: the ROM loader and the chip's answers are proven only on the rig."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'esptool_session.py'
T = 30   # a bound no test here should reach

STUB_ESPTOOL = {
    'esptool/__init__.py': "import os\n__version__ = os.environ.get('STUB_VERSION', '5.3.1')\n",
    'esptool/cmds.py': textwrap.dedent('''
        import json, os
        LOG = os.environ['STUB_LOG']
        def log(*a):
            open(LOG, 'a').write(json.dumps(a) + '\\n')
        class Port:
            def close(self): log('close')
        class Esp:
            CHIP_NAME = os.environ.get('STUB_CHIP', 'ESP32-S3')
            _port = Port()
            def change_baud(self, b): log('baud', b)
            def read_flash(self, addr, size):
                if os.environ.get('STUB_FAIL_READ'): raise IOError('timed out')
                return bytes([addr & 0xff]) * size
            def flash_md5sum(self, addr, size): return f'md5-{addr:x}-{size:x}'
            IS_STUB = bool(os.environ.get('STUB_IS_STUB'))
            SEC = json.loads(os.environ.get('STUB_SECURITY', '[0, 0, 0, 0]'))   # flags, flash_crypt_cnt, eFuse FE, eFuse SB
            def get_security_info(self, cache=True):
                log('security', cache)
                return {'flags': self.SEC[0], 'flash_crypt_cnt': self.SEC[1],
                        'parsed_flags': {'SECURE_BOOT_EN': bool(self.SEC[0] & 1)}}
            def get_flash_encryption_enabled(self): log('efuse'); return bin(self.SEC[2]).count('1') % 2 == 1
            def get_secure_boot_enabled(self): return self.SEC[3] << 20
        def detect_chip(port, connect_mode):
            log('connect', port, connect_mode); return Esp()
        def run_stub(esp): log('stub'); return esp
        def attach_flash(esp): log('attach')
        def reset_chip(esp, mode): log('reset', mode)
    '''),
}


def setUpModule():
    guard_survivors(__name__)


class EsptoolSession(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        stub = self.tmp / 'stub'
        for rel, text in STUB_ESPTOOL.items():
            (stub / rel).parent.mkdir(parents=True, exist_ok=True)
            (stub / rel).write_text(text)
        self.log = self.tmp / 'stub.log'
        self.env = dict(os.environ, PYTHONPATH=str(stub), STUB_LOG=str(self.log))

    def session(self, ops, after='hard-reset', **env):
        spec = self.tmp / 'ops.json'
        spec.write_text(json.dumps(ops))
        r = subprocess.run([sys.executable, '-B', SCRIPT, '--port', '/dev/x', '--chip', 'esp32s3', '--baud', '921600',
                            '--after', after, spec], capture_output=True, text=True, env=dict(self.env, **env),
                           timeout=T)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1]), calls

    def test_ops_run_in_order_in_one_connection_then_one_reset(self):
        code, out, calls = self.session([{'op': 'read', 'addr': 0x10, 'size': 4, 'file': str(self.tmp / 'r.bin')},
                                         {'op': 'md5', 'addr': 0x9000, 'size': 0x5000}])
        self.assertEqual(code, 0, out)
        self.assertEqual(out['esptool'], '5.3.1')
        self.assertEqual([r['op'] for r in out['results']], ['read', 'md5'])
        self.assertEqual(out['results'][0]['size'], 4)
        self.assertEqual(out['results'][1]['md5'], 'md5-9000-5000')
        self.assertEqual((self.tmp / 'r.bin').read_bytes(), b'\x10' * 4)
        self.assertEqual([c[0] for c in calls], ['connect', 'stub', 'baud', 'attach', 'reset', 'close'])
        self.assertEqual(calls[0], ['connect', '/dev/x', 'default-reset'])
        self.assertEqual(calls[2], ['baud', 921600])
        self.assertEqual(calls[-2], ['reset', 'hard-reset'])

    def test_another_esptool_is_refused_before_connecting(self):
        for version in ('5.2.0', '5.3.0', '5.3'):
            with self.subTest(version):
                code, out, calls = self.session([{'op': 'md5', 'addr': 0, 'size': 0x1000}], STUB_VERSION=version)
                self.assertEqual((code, out['ok'], out['results'], calls), (1, False, [], []))
                self.assertIn(f'esptool {version} is installed', out['error'])
                self.assertIn('needs esptool 5.3.1', out['error'])

    def test_a_write_is_refused(self):
        code, out, calls = self.session([{'op': 'write', 'addr': 0x8000, 'file': str(self.tmp / 'seg.bin')}])
        self.assertEqual((code, out['ok'], out['results']), (1, False, []))
        self.assertIn("unknown op 'write'", out['error'])
        self.assertEqual(calls[-1], ['close'])
        self.assertNotIn('reset', [c[0] for c in calls])

    def test_no_reset_leaves_the_chip_alone(self):
        code, out, calls = self.session([{'op': 'md5', 'addr': 0, 'size': 0x1000}], after='no-reset')
        self.assertEqual(code, 0)
        self.assertNotIn('reset', [c[0] for c in calls])
        self.assertEqual(calls[-1], ['close'])

    def test_another_chip_and_a_failing_op_are_reported_and_the_port_closed(self):
        code, out, calls = self.session([{'op': 'md5', 'addr': 0, 'size': 0x1000}], STUB_CHIP='ESP32-S2')
        self.assertEqual(code, 1)
        self.assertIn('not an esp32s3', out['error'])
        self.assertEqual([c[0] for c in calls], ['connect', 'close'])
        code, out, calls = self.session([{'op': 'md5', 'addr': 0, 'size': 0x1000},
                                         {'op': 'read', 'addr': 0, 'size': 1, 'file': str(self.tmp / 'x')}],
                                        STUB_FAIL_READ='1')
        self.assertEqual((code, [r['op'] for r in out['results']]), (1, ['md5']))
        self.assertIn('timed out', out['error'])
        self.assertEqual(calls[-1], ['close'])
        self.assertNotIn('reset', [c[0] for c in calls])

    def test_the_security_state_is_read_first_on_the_rom_loader_from_two_sources(self):
        off = {'flashEncryption': 'off', 'secureBoot': 'off'}
        code, out, calls = self.session([{'op': 'security'}, {'op': 'md5', 'addr': 0, 'size': 0x1000}],
                                        after='no-reset')
        self.assertEqual(code, 0, out)
        self.assertEqual(out['results'][0], {'op': 'security', 'flags': 0, 'flashCryptCnt': 0, 'rom': off, 'efuse': off})
        self.assertEqual([c[0] for c in calls][:4], ['connect', 'security', 'efuse', 'stub'])   # before the stub
        self.assertEqual(calls[1], ['security', False])   # never esptool's cached connect-time answer
        for sec, rom, efuse in (([1, 0, 0, 0], {'secureBoot': 'on'}, {}), ([0, 3, 7, 0], {}, {'flashEncryption': 'on'}),
                                ([0, 1, 1, 1], {'flashEncryption': 'on'}, {'flashEncryption': 'on', 'secureBoot': 'on'})):
            code, out, _ = self.session([{'op': 'security'}], after='no-reset', STUB_SECURITY=json.dumps(sec))
            self.assertEqual((out['results'][0]['rom'], out['results'][0]['efuse']), (dict(off, **rom), dict(off, **efuse)))
        code, out, calls = self.session([{'op': 'security'}], after='no-reset', STUB_IS_STUB='1')
        self.assertEqual(code, 1)
        self.assertIn('ROM loader', out['error'])
        self.assertEqual(calls[-1], ['close'])
        code, out, calls = self.session([{'op': 'md5', 'addr': 0, 'size': 0x1000}, {'op': 'security'}], after='no-reset')
        self.assertEqual((code, calls), (1, []))
        self.assertIn('comes first', out['error'])


if __name__ == '__main__':
    unittest.main()
