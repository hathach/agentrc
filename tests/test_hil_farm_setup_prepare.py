"""prepare-stm32cubeprogrammer.sh against a fake install tree and a temp copy of farm.toml's pin."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the HIL farm setup runs on Debian: bash, fcntl, Linux paths')

import re
import shutil
import subprocess
import tarfile
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

FARM = Path(__file__).resolve().parent.parent / 'skills' / 'hil-farm-setup' / 'scripts'
PIN = re.compile(r'(\[tool\.stm32cubeprogrammer\][^\[]*?sha256 = ")([0-9a-f]{64})(")', re.S)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.scripts = self.tmp / 'scripts'
        self.scripts.mkdir()
        for f in ('prepare-stm32cubeprogrammer.sh', 'farm.toml'):
            shutil.copy(FARM / f, self.scripts)
        self.app = self.tmp / 'inst' / 'STM32CubeProgrammer'
        for rel, mode in (('bin/STM32_Programmer_CLI', 0o755), ('lib/a.so', 0o644), ('SVD/x.svd', 0o644),
                          ('updater/u', 0o755), ('uninstaller/u', 0o755), ('bin/hs_err_pid42.log', 0o644)):
            f = self.app / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(rel)
            f.chmod(mode)
        self.out = self.tmp / 'out'
        self.out.mkdir()

    def prepare(self, app=None):
        return subprocess.run(['bash', str(self.scripts / 'prepare-stm32cubeprogrammer.sh'), str(app or self.app),
                               str(self.out)], capture_output=True, text=True, timeout=60)

    def pin(self, sha):
        toml = self.scripts / 'farm.toml'
        toml.write_text(PIN.sub(lambda m: m.group(1) + sha + m.group(3), toml.read_text(), count=1))

    def packed_sha(self):
        """The tree's hash, from prepare's refusal against a pin it cannot match."""
        self.pin('0' * 64)
        r = self.prepare()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(list(self.out.iterdir()), [])
        return re.search(r'packs to ([0-9a-f]{64}), farm.toml pins 0{64} \(2\.23\.0\); install 2\.23\.0', r.stderr).group(1)

    def test_packs_the_pinned_tree_normalised_and_without_the_gui_parts(self):
        sha = self.packed_sha()
        self.pin(sha)
        r = self.prepare()
        self.assertEqual(r.returncode, 0, r.stderr)
        tar = self.out / 'stm32cubeprogrammer.tar'
        with tarfile.open(tar) as tf:
            members = tf.getmembers()
        names = [m.name for m in members]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names, ['STM32CubeProgrammer', 'STM32CubeProgrammer/bin',
                                 'STM32CubeProgrammer/bin/STM32_Programmer_CLI', 'STM32CubeProgrammer/lib',
                                 'STM32CubeProgrammer/lib/a.so'])
        self.assertEqual({(m.uid, m.gid, m.mtime) for m in members}, {(0, 0, 0)})
        self.assertEqual(tar.stat().st_mode & 0o777, 0o644)
        # times are not part of the pin
        os.utime(self.app / 'lib' / 'a.so', (1, 1))
        tar.unlink()
        self.assertEqual(self.prepare().returncode, 0)
        r = self.prepare()
        self.assertEqual(r.returncode, 1)
        self.assertIn('exists; remove it first', r.stderr)

    def test_a_changed_tree_is_refused(self):
        sha = self.packed_sha()
        (self.app / 'lib' / 'a.so').write_text('other')
        self.pin(sha)
        r = self.prepare()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'farm.toml pins {sha}', r.stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_refuses_what_is_not_an_install(self):
        for app, msg in ((self.tmp / 'none', 'no '), (self.app / 'bin', 'name the STM32CubeProgrammer directory itself')):
            with self.subTest(app=app):
                r = self.prepare(app)
                self.assertEqual(r.returncode, 1)
                self.assertIn(msg, r.stderr)
        (self.app / 'bin' / 'STM32_Programmer_CLI').unlink()
        self.assertIn('holds no bin/STM32_Programmer_CLI', self.prepare().stderr)
        r = subprocess.run(['bash', str(self.scripts / 'prepare-stm32cubeprogrammer.sh')], capture_output=True, text=True)
        self.assertIn('usage', r.stderr)


def setUpModule():
    guard_survivors(__name__)


if __name__ == '__main__':
    unittest.main()
