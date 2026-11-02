"""fw/renesas_fw_check.sh, the manual firmware check, with lspci, setpci and the driver link stubbed; the real read needs the farm."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the HIL farm setup runs on Debian: bash, fcntl, Linux paths')

import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

FARM = Path(__file__).resolve().parent.parent / 'skills' / 'hil-farm-setup' / 'scripts'
BDFS = [f'0000:0{i}:00.0' for i in range(1, 9)]


class TestRenesas(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stubs = self.tmp / 'stubs'
        self.stubs.mkdir()
        self.controllers(BDFS)
        self.stub('setpci', 'echo 00202609')
        self.stub('readlink', 'echo /sys/bus/pci/drivers/xhci-pci-renesas')

    def stub(self, name, body):
        p = self.stubs / name
        p.write_text(f'#!/bin/sh\n{body}\n')
        p.chmod(0o755)

    def controllers(self, bdfs, rc=0, ids=None):
        """lspci -Dn -d 1912: lists `bdfs` with device ids `ids` (all 0014 by default); other arguments exit 9."""
        ids = ids or ['0014'] * len(bdfs)
        lines = ''.join(f'echo "{b} 0c03: 1912:{i} (rev 03)"; ' for b, i in zip(bdfs, ids))
        self.stub('lspci', f'[ "$*" = "-Dn -d 1912:" ] || exit 9\n{lines}exit {rc}')

    def run_check(self):
        env = {**os.environ, 'PATH': f'{self.stubs}:{os.environ["PATH"]}'}
        return subprocess.run(['bash', str(FARM / 'fw' / 'renesas_fw_check.sh')], capture_output=True, text=True, env=env, timeout=60)

    def assertFails(self, says):
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f'FAIL  {says}', r.stdout)
        return r

    def test_every_current_controller_passes_whatever_the_count(self):
        for bdfs in (BDFS, BDFS[:3], []):
            with self.subTest(n=len(bdfs)):
                self.controllers(bdfs)
                r = self.run_check()
                self.assertEqual(r.returncode, 0, r.stdout)
                self.assertNotIn('FAIL', r.stdout)
                self.assertEqual(r.stdout.count('firmware 00202609'), len(bdfs))

    def test_both_upd720201_and_upd720202_are_checked_and_nothing_else(self):
        bdfs = BDFS[:4]
        for ids, checked in ((['0015'] * 4, bdfs), (['0014', '0015', '0014', '0015'], bdfs),
                             (['0014', '0025', '0015', '0000'], [bdfs[0], bdfs[2]])):
            with self.subTest(ids=ids):
                self.controllers(bdfs, ids=ids)
                r = self.run_check()
                self.assertEqual(r.returncode, 0, r.stdout)
                self.assertEqual([l.split()[1] for l in r.stdout.splitlines() if 'firmware' in l], checked)

    def test_an_old_upd720202_fails(self):
        self.controllers(BDFS[:2], ids=['0014', '0015'])
        self.stub('setpci', 'if [ "$2" = 0000:02:00.0 ]; then echo 00202509; else echo 00202609; fi')
        self.assertFails("0000:02:00.0 firmware '00202509'")

    def test_a_failed_lspci_fails_even_with_plausible_output(self):
        self.controllers(BDFS, rc=1)
        r = self.assertFails('lspci -d 1912: failed')
        self.assertNotIn('ok', r.stdout)

    def test_reserved_bits_are_masked(self):
        self.stub('setpci', 'echo ff202609')
        self.assertEqual(self.run_check().returncode, 0)

    def test_bad_versions_fail(self):
        for out in ('00202509', 'ffffffff', '0020260', 'xyz', ''):
            with self.subTest(out=out):
                self.stub('setpci', f'echo "{out}"')
                self.assertFails(f"0000:01:00.0 firmware '{out}'")

    def test_a_failed_setpci_fails_even_with_plausible_output(self):
        self.stub('setpci', 'echo 00202609; exit 1')
        self.assertFails("0000:01:00.0 firmware '00202609'")

    def test_one_old_controller_among_good_ones_fails(self):
        self.stub('setpci', 'if [ "$2" = 0000:05:00.0 ]; then echo 00202509; else echo 00202609; fi')
        r = self.assertFails("0000:05:00.0 firmware '00202509'")
        self.assertEqual(r.stdout.count('FAIL'), 1)

    def test_another_driver_fails(self):
        self.stub('readlink', 'echo /sys/bus/pci/drivers/xhci_hcd')
        self.assertFails("0000:01:00.0 driver 'xhci_hcd'")


def setUpModule():
    guard_survivors(__name__)


if __name__ == '__main__':
    unittest.main()
