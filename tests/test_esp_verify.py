"""Manifest and esptool 5.5.0 output plumbing; no hardware is accessed."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/target-debug/scripts/esp_verify.py'
_spec = importlib.util.spec_from_file_location('esp_verify', SCRIPT)
esp_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(esp_verify)

# The headers/verdicts are from esptool 5.5.0 cmds.py:2671-2700; fatal exit is 2.
FAKE_ESPTOOL = '''#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
args = sys.argv[1:]
mode = os.environ.get('FAKE_MODE', 'match')
pathlib.Path(os.environ['FAKE_CALL']).write_text(json.dumps({
    'args': args, 'open_attempts': os.environ.get('ESPTOOL_OPEN_PORT_ATTEMPTS')}))
if mode == 'failure':
    print("ERROR: A fatal error occurred: Could not open /dev/serial/by-id/fake, "
          "the port is busy or doesn't exist.", file=sys.stderr)
    sys.exit(2)
if mode == 'wrong_chip':
    print('ERROR: A fatal error occurred: This chip is ESP32-S3, not ESP32-P4. '
          'Wrong chip argument?', file=sys.stderr)
    sys.exit(2)
if mode == 'unknown_chip':
    print("WARNING: This chip doesn't appear to be an ESP32-P4 (read chip ID 999). "
          "Probably it is unsupported by this version of esptool. Will attempt to continue anyway.", file=sys.stderr)
print('esptool v5.5.0')
start = args.index('verify-flash') + 7
pairs = list(zip(args[start::2], args[start+1::2]))
if mode == 'duplicate':
    pairs.append(pairs[0])
for i, (offset, file) in enumerate(pairs):
    data = pathlib.Path(file).read_bytes()
    size = (len(data) + 3) & ~3
    address = int(offset, 0) + (4 if mode == 'wrong_range' else 0)
    source = file + ('.wrong' if mode == 'wrong_file' else '')
    print(f"Verifying {size:#x} ({size}) bytes at {address:#010x} in flash against '{source}'...")
    if mode == 'missing' and i == len(pairs) - 1:
        break
    if mode in ('mismatch', 'mismatch_failure') and i == 1:
        print('Verification failed (digest mismatch).')
    else:
        print('Verification successful (digest matched).')
if mode in ('mismatch', 'mismatch_failure'):
    reason = 'Verification failed.' if mode == 'mismatch' else 'Lost connection during reset.'
    print('ERROR: A fatal error occurred: ' + reason, file=sys.stderr)
    sys.exit(2)
if mode == 'late_failure':
    print('ERROR: A fatal error occurred: Lost connection during reset.', file=sys.stderr)
    sys.exit(2)
'''


@unittest.skipIf(os.name == 'nt', 'POSIX executable stub')
class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / 'flasher_args.json'
        self.call = self.root / 'call.json'
        self.tool = self.root / 'fake esptool'
        self.tool.write_text(FAKE_ESPTOOL)
        self.tool.chmod(self.tool.stat().st_mode | stat.S_IEXEC)
        self.files = {'0x2000': 'bootloader/bootloader.bin', '0x8000': 'partition_table/partition-table.bin',
                      '0x10000': 'app space [1].bin'}
        for i, filename in enumerate(self.files.values()):
            path = self.root / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(bytes([i]) * (9 + i))
        self.data = {'flash_files': self.files, 'flash_settings': {
            'flash_mode': 'dio', 'flash_size': '2MB', 'flash_freq': '80m'},
            'write_flash_args': ['--flash-mode', 'dio', '--flash-size', '2MB', '--flash-freq', '80m'],
            'extra_esptool_args': {'chip': 'esp32p4', 'stub': True, 'before': 'default-reset', 'after': 'hard-reset'}}
        self.write_manifest()
        self.env = {**os.environ, 'ESP_VERIFY_ESPTOOL': shlex.quote(str(self.tool)),
                    'FAKE_CALL': str(self.call), 'ESPTOOL_OPEN_PORT_ATTEMPTS': '100'}

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data))

    def run_cli(self, *args, **env):
        proc = subprocess.run([sys.executable, str(SCRIPT), '--manifest', str(self.manifest),
                               '--chip', 'esp32p4', '--port', '/dev/serial/by-id/fake',
                               '--before', 'no-reset', '--after', 'no-reset', *args],
                              capture_output=True, text=True, env={**self.env, **env}, cwd='/', timeout=30)
        self.assertEqual(len(proc.stdout.splitlines()), 1, proc.stderr + proc.stdout)
        return proc.returncode, json.loads(proc.stdout)

    def assert_refused(self, *args, **env):
        code, result = self.run_cli(*args, **env)
        self.assertEqual(code, 2, result)
        self.assertEqual(result['status'], 'refused')
        self.assertFalse(self.call.exists())
        return result

    def test_match_covers_every_manifest_file_with_hash_and_settings(self):
        code, result = self.run_cli()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'match')
        self.assertEqual(result['coverage'], 'manifest ranges only')
        self.assertIn("caller's board lock", result['ownership'])
        self.assertEqual(len(result['ranges']), 3)
        for image, (offset, filename) in zip(result['ranges'], self.files.items()):
            data = (self.root / filename).read_bytes()
            self.assertEqual(image, {'offset': int(offset, 0), 'size': len(data),
                                    'file': str(self.root / filename), 'sha256': hashlib.sha256(data).hexdigest(),
                                    'comparison_size': 12, 'verdict': 'match'})
        call = json.loads(self.call.read_text())
        self.assertEqual(call['open_attempts'], '1')
        args = call['args']
        self.assertEqual(args[:11], ['--chip', 'esp32p4', '--port', '/dev/serial/by-id/fake',
                                    '--before', 'no-reset', '--after', 'no-reset', '--connect-attempts', '1',
                                    'verify-flash'])
        self.assertEqual(args[11:17], self.data['write_flash_args'])
        self.assertEqual(args[17::2], list(self.files))
        for path in args[18::2]:
            self.assertFalse(Path(path).exists(), 'snapshot not cleaned up')

    def test_mismatch_is_not_a_tool_failure(self):
        code, result = self.run_cli(FAKE_MODE='mismatch')
        self.assertEqual(code, 1, result)
        self.assertEqual([i['verdict'] for i in result['ranges']], ['match', 'mismatch', 'match'])

    def test_missing_verdict_is_failure_even_with_zero_exit(self):
        code, result = self.run_cli(FAKE_MODE='missing')
        self.assertEqual(code, 3, result)
        self.assertEqual(result['tool_exit'], 0)

    def test_port_failure_is_reported_with_transcript(self):
        code, result = self.run_cli(FAKE_MODE='failure')
        self.assertEqual(code, 3, result)
        self.assertIn('Could not open', result['stderr'])

    def test_inconsistent_or_duplicate_evidence_is_failure(self):
        for mode in ('late_failure', 'mismatch_failure', 'wrong_range', 'wrong_file', 'duplicate'):
            with self.subTest(mode=mode):
                code, result = self.run_cli(FAKE_MODE=mode)
                self.assertEqual(code, 3, result)

    def test_actual_chip_disagreement_is_refused(self):
        for mode in ('wrong_chip', 'unknown_chip'):
            with self.subTest(mode=mode):
                code, result = self.run_cli(FAKE_MODE=mode)
                self.assertEqual(code, 2, result)
                self.assertNotIn('chipState', result)    # --before no-reset left the chip alone
                code, result = self.run_cli('--before', 'default-reset', FAKE_MODE=mode)
                self.assertEqual(code, 2, result)
                self.assertIn('ROM loader', result['chipState'])

    def test_a_refusal_before_the_tool_says_nothing_of_the_chip(self):
        self.files['0x2004'] = 'app space [1].bin'
        self.write_manifest()
        self.assertNotIn('chipState', self.assert_refused())

    def test_overlap_is_refused_before_tool(self):
        self.files['0x2004'] = 'app space [1].bin'
        self.write_manifest()
        self.assertIn('overlapping', self.assert_refused()['error'])

    def test_padding_overlap_is_also_refused(self):
        self.files['0x2009'] = 'app space [1].bin'
        self.write_manifest()
        self.assert_refused()

    def test_manifest_chip_disagreement(self):
        self.data['extra_esptool_args']['chip'] = 'esp32s3'
        self.write_manifest()
        self.assert_refused()

    def test_missing_file(self):
        (self.root / 'bootloader/bootloader.bin').unlink()
        self.assertIn('missing image', self.assert_refused()['error'])

    def test_bad_offset(self):
        for offset in ('-1', 'xyz', '0xzz', '1.5', ' 0x20', '0x100000000'):
            with self.subTest(offset=offset):
                self.data['flash_files'] = {offset: 'bootloader/bootloader.bin'}
                self.write_manifest()
                self.assert_refused()

    def test_malformed_manifest_and_duplicate_key(self):
        for content in ('{', '[]', '{}', '{"flash_files": {}, "flash_files": {}}'):
            with self.subTest(content=content):
                self.manifest.write_text(content)
                self.assert_refused()

    def test_empty_images_are_refused(self):
        (self.root / 'bootloader/bootloader.bin').write_bytes(b'')
        self.assert_refused()

    def test_drifting_or_nonstable_port_is_refused(self):
        for port in ('/dev/ttyACM0', '/dev/ttyUSB12', '/dev/pts/1', '/dev/serial/by-id/../ttyUSB0',
                     '/dev/serial/by-id/..', '/dev/serial/by-id/.'):
            with self.subTest(port=port):
                self.assert_refused('--port', port)

    def test_by_path_port_is_accepted(self):
        code, result = self.run_cli('--port', '/dev/serial/by-path/pci-fake')
        self.assertEqual(code, 0, result)

    def test_command_line_override_and_missing_tool(self):
        code, result = self.run_cli(ESP_VERIFY_ESPTOOL=shlex.join([sys.executable, str(self.tool)]))
        self.assertEqual(code, 0, result)
        self.call.unlink()
        for command in ('', '/no/such/esptool', "'"):
            with self.subTest(command=command):
                self.assert_refused(ESP_VERIFY_ESPTOOL=command)

    def test_path_fallback(self):
        link = self.root / 'esptool'
        link.symlink_to(self.tool)
        self.env.pop('ESP_VERIFY_ESPTOOL')
        code, result = self.run_cli(PATH=str(self.root) + os.pathsep + os.environ['PATH'])
        self.assertEqual(code, 0, result)

    def test_settings_conflict_or_unsafe_option_is_refused(self):
        self.data['flash_settings']['flash_mode'] = 'qio'
        self.write_manifest()
        self.assert_refused()
        self.data['flash_settings']['flash_mode'] = 'dio'
        self.data['write_flash_args'] += ['--force']
        self.write_manifest()
        self.assert_refused()

    def test_missing_settings_and_auto_detect_size_are_refused(self):
        for settings in (None, {}, {'flash_mode': 'dio', 'flash_size': 'detect', 'flash_freq': '80m'}):
            self.data['flash_settings'] = settings
            self.write_manifest()
            self.assert_refused()

    def test_encrypted_image_is_refused(self):
        self.data['app'] = {'offset': '0x10000', 'file': 'app space [1].bin', 'encrypted': 'true'}
        self.write_manifest()
        self.assert_refused()

    def test_stub_false_is_preserved(self):
        self.data['extra_esptool_args']['stub'] = False
        self.write_manifest()
        code, result = self.run_cli()
        self.assertEqual(code, 0, result)
        self.assertIn('--no-stub', json.loads(self.call.read_text())['args'])

    def test_reset_choices_and_chip_are_explicit(self):
        self.assert_refused('--before', 'default_reset')
        self.assert_refused('--chip', 'auto')
        proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)['status'], 'refused')


if __name__ == '__main__':
    unittest.main()
