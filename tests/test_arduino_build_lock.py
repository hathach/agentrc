"""Real subprocess contention and owner-exit checks for the shared Arduino build lock."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import load_arduino_engine, load_arduino_fixture, tmpdir  # noqa: E402

ab = load_arduino_engine('skills/arduino/scripts/arduino_build.py')
CHILD = '''
import os, sys
sys.path.insert(0, sys.argv[1])
from helpers import load_arduino_engine
ab = load_arduino_engine('skills/arduino/scripts/arduino_build.py')
try:
    lock = ab.build_lock(sys.argv[2])
except BlockingIOError:
    sys.exit(73)
if len(sys.argv) > 3:
    print('locked', flush=True)
    sys.stdin.readline()
    os._exit(7)
lock.close()
'''


class BuildLock(unittest.TestCase):
    def setUp(self):
        self.path = tmpdir(self) / '.lock'
        self.argv = [sys.executable, '-c', CHILD, str(Path(__file__).resolve().parent), str(self.path)]

    def probe(self, expected):
        result = subprocess.run(self.argv, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)

    def test_contention_close_and_existing_contents(self):
        self.path.write_bytes(b'keep this record')
        with ab.build_lock(self.path):
            self.probe(73)
        self.probe(0)
        self.assertEqual(self.path.read_bytes(), b'keep this record')

    def test_empty_file_and_abrupt_owner_exit(self):
        with subprocess.Popen(self.argv + ['hold'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as owner:
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'locked')
                self.probe(73)
                owner.communicate('\n', timeout=10)
                self.assertEqual(owner.returncode, 7)
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.communicate(timeout=10)
        self.probe(0)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_build_error_releases_lock_before_exception_is_discarded(self):
        fake = load_arduino_fixture()
        root = fake.library(self.path.parent / 'lib')
        profile = fake.build_profile(root, fake.agentrc(self.path.parent / 'agentrc'))
        self.path = root / 'build/hil/.lock'
        self.argv[-1] = str(self.path)

        def fail_build(*args):
            self.probe(73)
            raise RuntimeError('build failed')

        with mock.patch.object(ab, 'P', profile), \
                mock.patch.object(ab, 'source_identity', return_value=({'head': 'a' * 40, 'dirty': False}, '')), \
                mock.patch.object(ab, 'build_all', side_effect=fail_build):
            try:
                ab.main(['--shared', '--board', 'dev_a', '--example', 'examples/Nowhere/nowhere'])
            except RuntimeError as e:
                self.assertEqual(str(e), 'build failed')
                self.probe(0)   # the exception still owns the build stack here
            else:
                self.fail('the build should have raised')
