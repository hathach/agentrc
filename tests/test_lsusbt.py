import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'bin' / 'lsusbt'

TREE = '''\
/:  Bus 001.Port 001: Dev 001, Class=root_hub, Driver=xhci_hcd/4p, 480M
    |__ Port 001: Dev 002, If 0, Class=Hub, Driver=hub/4p, 480M
        |__ Port 001: Dev 011, If 0, Class=Vendor Specific Class, Driver=[none], 12M
        |__ Port 002: Dev 012, If 0, Class=Communications, Driver=cdc_acm, 12M
        |__ Port 002: Dev 012, If 1, Class=CDC Data, Driver=cdc_acm, 12M
        |__ Port 003: Dev 013, If 0, Class=Vendor Specific Class, Driver=cp210x, 12M
        |__ Port 004: Dev 014, If 0, Class=Human Interface Device, Driver=usbhid, 12M
'''
NAMES = {'001:001': 'Linux Foundation 2.0 root hub', '001:002': 'Terminus Technology Inc. Hub',
         '001:011': 'SEGGER J-Link', '001:012': 'Espressif USB JTAG/serial debug unit',
         '001:013': 'Silicon Labs CP210x', '001:014': 'Logitech Keyboard'}
SERIALS = {(1, 1): '0000:00:14.0', (1, 11): '000681295394', (1, 12): '84:F7:03:C0:84:E4', (1, 13): 'ABC123'}
BOARDS = {'boards': [{'name': 'esp32s3', 'uid': '84F703C084E4'},
                     {'name': 'stm32h7', 'uid': '1234', 'flasher': {'uid': '681295394'}}]}


@unittest.skipIf(os.name == 'nt', 'reads Linux sysfs')
@unittest.skipUnless(shutil.which('jq'), 'needs jq')
class LsusbtTest(unittest.TestCase):
    """lsusb is a stub on PATH that records its arguments; sysfs is a fake tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.bin, self.sysfs, self.json, self.log = tmp / 'bin', tmp / 'sysfs', tmp / 'boards.json', tmp / 'lsusb.log'
        self.bin.mkdir()
        names = ''.join(f'Bus {k[:3]} Device {k[4:]}: ID 1234:5678 {v}\n' for k, v in NAMES.items())
        stub = self.bin / 'lsusb'
        stub.write_text(f'#!/bin/bash\necho "$*" >> {self.log}\n'
                        f"if [ \"$1\" = -t ]; then cat <<'EOF'\n{TREE}EOF\nelse cat <<'EOF'\n{names}EOF\nfi\n")
        stub.chmod(0o755)
        for (bus, dev), serial in SERIALS.items():
            d = self.sysfs / f'{bus}-{dev}'
            d.mkdir(parents=True)
            (d / 'busnum').write_text(f'{bus}\n')
            (d / 'devnum').write_text(f'{dev}\n')
            (d / 'serial').write_text(f'{serial}\n')
        self.json.write_text(json.dumps(BOARDS))

    def tearDown(self):
        self.tmp.cleanup()

    def run_lsusbt(self, *args):
        env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'LSUSBT_SYSFS': str(self.sysfs)}
        done = subprocess.run([str(SCRIPT), '-j', str(self.json), *args], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.splitlines()

    def test_default_keeps_board_lines_under_their_hubs(self):
        self.assertEqual(self.run_lsusbt(), [
            '/:  Bus 001.Port 001: Dev 001: [480M] Linux Foundation 2.0 root hub',
            '    |__ Port 001: Dev 002: [480M] Terminus Technology Inc. Hub',
            '        |__ Port 001: Dev 011: [12M] SEGGER J-Link [000681295394] stm32h7 flasher',
            '        |__ Port 002: Dev 012: [12M] Espressif USB JTAG/serial debug unit [84:F7:03:C0:84:E4] esp32s3 device',
        ])

    def test_verbose_prints_every_line_and_names_each_device_once(self):
        lines = self.run_lsusbt('-v')
        self.assertEqual(len(lines), len(TREE.splitlines()))
        self.assertIn('        |__ Port 002: Dev 012: [12M]', lines, 'a later interface stays bare')
        self.assertIn('        |__ Port 003: Dev 013: [12M] Silicon Labs CP210x [ABC123]', lines)
        self.assertIn('        |__ Port 004: Dev 014: [12M] Logitech Keyboard', lines)
        self.assertNotIn('0000:00:14.0', '\n'.join(lines), "a root hub's serial is its PCI address")

    def test_other_args_reach_lsusb_t_and_verbose_does_not(self):
        self.run_lsusbt('-v', '-x')
        self.assertIn('-t -x', self.log.read_text().splitlines())


if __name__ == '__main__':
    unittest.main()
