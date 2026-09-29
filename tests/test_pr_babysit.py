import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PrBabysitWorkflow(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_stub_harness_passes(self):
        done = subprocess.run(['node', '--test', str(ROOT / 'tests' / 'pr_babysit_harness.mjs')], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
