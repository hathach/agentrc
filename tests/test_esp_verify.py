"""Manifest and esptool 5.5.0 output plumbing; no hardware is accessed."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/target-debug/scripts/esp_verify.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import make_stub, pid_alive, tmpdir  # noqa: E402
SPEC = importlib.util.spec_from_file_location('esp_verify', SCRIPT)
esp_verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(esp_verify)
NT = os.name == 'nt'
# never a port that is attached: the stub, not esptool, is the only reader of --port
PORT = 'COM250' if NT else '/dev/serial/by-id/fake'
cmdline = subprocess.list2cmdline if NT else shlex.join


def native(path):
    # MSYS2's python uses '/' for os.sep; give Windows the backslash form an IDF venv path has
    return path.replace('/', '\\') if NT else path


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
if mode == 'hang':
    import subprocess, time
    # a child holding the pipes, as a Windows venv python.exe launcher's interpreter does (passed explicitly:
    # Windows' Popen hands a child no handle it is not given)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             stdout=sys.stdout, stderr=sys.stderr)
    pathlib.Path(os.environ['FAKE_CALL'] + '.child').write_text(str(child.pid))
    print('Connected to ESP32-P4 on /dev/serial/by-id/fake:', flush=True)
    time.sleep(30)
if mode == 'hang_alone':
    print('Connected to ESP32-P4 on /dev/serial/by-id/fake:', flush=True)
    import time
    time.sleep(30)
if mode == 'ansi':
    # a standalone (PyInstaller) esptool.exe: the ANSI code page, whatever PYTHONIOENCODING says
    sys.stdout.reconfigure(encoding='mbcs')
if mode == 'native':
    sys.stdout.buffer.write(b'esptool \\xff\\xfe not UTF-8\\r\\n')
    sys.stdout.flush()
    sys.stdout.reconfigure(newline='\\r\\n')
if mode == 'stub_failure':
    print('Connected to ESP32-P4 on /dev/serial/by-id/fake:')
    print('Uploading stub flasher...')
    print('ERROR: A fatal error occurred: Serial data stream stopped: Possible serial noise or corruption.',
          file=sys.stderr)
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


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)  # esp_verify reports resolved image paths
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
        command = [native(sys.executable), native(str(self.tool))]
        self.env = {**os.environ, 'ESP_VERIFY_ESPTOOL': cmdline(command),
                    'FAKE_CALL': str(self.call), 'ESPTOOL_OPEN_PORT_ATTEMPTS': '100'}
        # the stub is what runs, never a real esptool
        self.assertEqual(esp_verify.split_command(self.env['ESP_VERIFY_ESPTOOL']), command)
        self.assertEqual(esp_verify.find_tool(command[0]), command[0])

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data))

    def run_cli(self, *args, cwd='/', **env):
        proc = subprocess.run([sys.executable, str(SCRIPT), '--manifest', str(self.manifest),
                               '--chip', 'esp32p4', '--port', PORT,
                               '--before', 'no-reset', '--after', 'no-reset', *args],
                              capture_output=True, text=True, env={**self.env, **env}, cwd=cwd, timeout=30)
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
        self.assertEqual(args[:11], ['--chip', 'esp32p4', '--port', PORT,
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

    def test_a_failed_run_leaves_the_chip_state_unknown(self):
        # the caller put the chip in the ROM loader and asked --after to reset it; the stub upload failed first
        code, result = self.run_cli('--after', 'hard-reset', FAKE_MODE='stub_failure')
        self.assertEqual(code, 3, result)
        self.assertIn('ROM loader', result['chipState'])
        code, result = self.run_cli(FAKE_MODE='stub_failure')
        self.assertEqual(code, 3, result)
        self.assertNotIn('chipState', result)    # no-reset both ways: the caller keeps the chip where it was

    def test_a_hung_esptool_is_killed_at_the_timeout(self):
        child = Path(str(self.call) + '.child')
        self.addCleanup(lambda: child.exists() and self.reap(int(child.read_text())))
        code, result = self.run_cli('--after', 'hard-reset', '--timeout', '1', FAKE_MODE='hang')
        self.assertEqual(code, 3, result)
        self.assertIn('did not finish within 1 s', result['error'])
        self.assertIsNone(result['tool_exit'])
        self.assertIn('Connected to', result['stdout'])
        self.assertIn('ROM loader', result['chipState'])
        if NT:    # the whole tree: its child held the pipes
            self.assertFalse(pid_alive(int(child.read_text())))

    @unittest.skipUnless(NT, 'taskkill')
    def test_an_unlaunchable_taskkill_still_kills_the_tool(self):
        for key in [k for k in self.env if k.upper() == 'SYSTEMROOT']:
            del self.env[key]
        code, result = self.run_cli('--timeout', '1', SYSTEMROOT=str(self.root / 'nowhere'), FAKE_MODE='hang_alone')
        self.assertEqual(code, 3, result)
        self.assertIn('did not finish within 1 s', result['error'])
        self.assertIn('Connected to', result['stdout'])
        # a child that holds the pipes outlives the tool's own kill: the wait for its output is bounded
        child = Path(str(self.call) + '.child')
        self.addCleanup(lambda: child.exists() and self.reap(int(child.read_text())))
        t0 = time.monotonic()
        code, result = self.run_cli('--timeout', '1', SYSTEMROOT=str(self.root / 'nowhere'), FAKE_MODE='hang')
        self.assertLess(time.monotonic() - t0, 1 + esp_verify.KILLED_DRAIN_S + 5)
        self.assertEqual(code, 3, result)
        self.assertIn('did not finish within 1 s', result['error'])
        self.assertIsNone(result['tool_exit'])
        self.assertTrue(pid_alive(int(child.read_text())))    # what taskkill would have ended
        self.assertIn('a process it started survived and may still hold', result['error'])

    @staticmethod
    def reap(pid):
        if pid_alive(pid):
            os.kill(pid, signal.SIGTERM if NT else signal.SIGKILL)

    def test_native_output_is_decoded_as_utf8_with_crlf_and_stray_bytes(self):
        # the snapshot path esptool echoes is outside the ANSI code page; its lines end CRLF
        temp = self.root / 'tmp \u0151'
        temp.mkdir()
        code, result = self.run_cli(FAKE_MODE='native', TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp))
        self.assertEqual(code, 0, result)
        self.assertIn(' not UTF-8\n', result['stdout'])    # the stray bytes' line, read as ANSI
        self.assertNotIn('\r', result['stdout'])
        self.assertIn('\u0151', result['stdout'])

    @unittest.skipUnless(NT, 'the ANSI code page')
    def test_ansi_output_of_a_standalone_esptool_exe_matches(self):
        # a snapshot path in the ANSI code page, echoed in it: e9 in cp1252, a8 a6 in cp936, neither UTF-8
        temp = self.root / 'tmp \u00e9'
        temp.mkdir()
        try:
            str(temp).encode('mbcs').decode('utf-8')
        except UnicodeEncodeError:
            self.skipTest('this ANSI code page has no \u00e9')
        except UnicodeDecodeError:
            pass
        else:
            self.skipTest('the ANSI code page is UTF-8')
        code, result = self.run_cli(FAKE_MODE='ansi', TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp))
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'match')
        self.assertIn(f"against '{temp}", result['stdout'])

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
                     '/dev/serial/by-id/..', '/dev/serial/by-id/.', *(() if NT else ('COM9',))):
            with self.subTest(port=port):
                self.assert_refused('--port', port)

    @unittest.skipUnless(NT, 'Windows COM ports')
    def test_windows_com_ports(self):
        for port in ('COM0', 'COM', 'COMx', 'COM3:', 'COM3 ', 'LPT1', r'\\.\COM', r'\\?\COM3', r'\\.\LPT1',
                     '/dev/serial/by-id/fake', '/dev/serial/by-path/pci-fake'):
            with self.subTest(port=port):
                self.assert_refused('--port', port)
        for port in ('COM250', 'com250', r'\\.\COM250'):
            with self.subTest(port=port):
                code, result = self.run_cli('--port', port)
                self.assertEqual(code, 0, result)
                # the name esptool matches against pyserial's to find a USB-Serial-JTAG port
                self.assertEqual(json.loads(self.call.read_text())['args'][2:4], ['--port', 'COM250'])

    @unittest.skipIf(NT, 'POSIX /dev/serial path')
    def test_by_path_port_is_accepted(self):
        code, result = self.run_cli('--port', '/dev/serial/by-path/pci-fake')
        self.assertEqual(code, 0, result)

    def test_command_line_override_and_missing_tool(self):
        # an unquoted path keeps its backslashes on Windows, as in the IDF venv's 'C:\...\python.exe -m esptool'
        plain = native(str(self.root / 'esptool_stub.py'))
        shutil.copy(self.tool, plain)
        # unquoted where the temp directory allows it: no rule keeps whitespace in an unquoted argument
        arg = cmdline([plain]) if any(c.isspace() for c in plain) else plain
        code, result = self.run_cli(ESP_VERIFY_ESPTOOL=cmdline([native(sys.executable)]) + ' ' + arg)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['command'][1], plain)
        if not NT:
            code, result = self.run_cli(ESP_VERIFY_ESPTOOL=shlex.quote(str(self.tool)))
            self.assertEqual(code, 0, result)
        self.call.unlink()
        empty = self.root / 'empty'
        empty.mkdir()
        for command in ('', ' ', '/no/such/esptool', "'", 'esptool'):
            with self.subTest(command=command):
                self.assert_refused(ESP_VERIFY_ESPTOOL=command, PATH=str(empty))

    @unittest.skipIf(NT, 'symlinked executable script')
    def test_path_fallback(self):
        link = self.root / 'esptool'
        link.symlink_to(self.tool)
        self.env.pop('ESP_VERIFY_ESPTOOL')
        code, result = self.run_cli(PATH=str(self.root) + os.pathsep + os.environ['PATH'])
        self.assertEqual(code, 0, result)

    @unittest.skipUnless(NT, 'Windows launcher')
    def test_path_fallback_is_path_only_on_windows(self):
        for target in ('bin', 'cwd'):
            (self.root / target).mkdir()
            make_stub(self.root / target, 'esptool', FAKE_ESPTOOL.split('\n', 1)[1])
        self.env.pop('ESP_VERIFY_ESPTOOL')
        # the cwd's esptool.exe (a checkout's) is a decoy that shutil.which and CreateProcess would run first
        code, result = self.run_cli(cwd=str(self.root / 'cwd'), PATH=str(self.root / 'bin'))
        self.assertEqual(code, 0, result)
        self.assertEqual(result['command'][0], os.path.join(self.root, 'bin', 'esptool.exe'))

    @unittest.skipUnless(NT, 'Windows PATH lookup')
    def test_a_path_entry_without_drive_and_root_is_not_searched(self):
        # '.', 'bin' and '\bin' name directories of the cwd or its drive: a checkout's esptool.exe
        cwd = self.root / 'cwd'
        (cwd / 'bin').mkdir(parents=True)
        for d in (cwd, cwd / 'bin'):
            (d / 'esptool.exe').write_bytes(b'')
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd)
        for entry in ('.', 'bin', os.path.splitdrive(str(cwd))[1], '', os.pathsep):    # '': an empty entry
            with self.subTest(entry=entry), mock.patch.dict(os.environ, {'PATH': entry}):
                self.assertIsNone(esp_verify.find_tool('esptool'))
        with mock.patch.dict(os.environ, {'PATH': str(cwd / 'bin')}):
            self.assertEqual(esp_verify.find_tool('esptool'), os.path.join(cwd / 'bin', 'esptool.exe'))

    @unittest.skipUnless(NT, 'Windows batch files')
    def test_cmd_and_bat_tools_on_path(self):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for name in ('esptool.cmd', 'other.bat'):
            (bin_dir / name).write_text(f'@"{native(sys.executable)}" "{native(str(self.tool))}" %*\n',
                                        encoding='ascii')
        self.env.pop('ESP_VERIFY_ESPTOOL')
        # a directory and no extension: the file found, since CreateProcess would add only .exe
        for command, found in ((None, 'esptool.cmd'), ('esptool.cmd', 'esptool.cmd'), ('other', 'other.bat'),
                               ('other.bat', 'other.bat'), (cmdline([str(bin_dir / 'other')]), 'other.bat')):
            with self.subTest(command=command):
                env = {'PATH': str(bin_dir), **({'ESP_VERIFY_ESPTOOL': command} if command else {})}
                code, result = self.run_cli(**env)
                self.assertEqual(code, 0, result)
                self.assertEqual(result['command'][0], os.path.join(bin_dir, found))

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
        self.assert_refused('--timeout', '0')
        proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)['status'], 'refused')


if __name__ == '__main__':
    unittest.main()
