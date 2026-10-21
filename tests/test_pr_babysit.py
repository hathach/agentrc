import shutil
import subprocess
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    guard_survivors(__name__)


class PrBabysitWorkflow(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_stub_harness_passes(self):
        done = subprocess.run(['node', '--test', str(ROOT / 'tests' / 'pr_babysit_harness.mjs')], capture_output=True, encoding='utf-8', errors='replace')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
