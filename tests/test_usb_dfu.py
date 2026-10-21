"""skills/hil/scripts/usb_dfu.py is the DFU runtime child a HIL runner times out and kills, moved from tinyuf2's
.claude/skills/hil-contract/scripts/usb_dfu.py (ce6db76) with its tests (test_hil.py UsbDfu): device selection,
the interface it picks, the request it sends and its JSON/exit contract, run as a real child over a stub pyusb.
The DETACH on a real device is proven only on the rig."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'usb_dfu.py'
T = 30   # a bound no test here should reach

STUB_USB = {
    'usb/__init__.py': '',
    'usb/core.py': """import json, os

class Itf:
    def __init__(self, c, sub, proto, num, istr):
        self.bInterfaceClass, self.bInterfaceSubClass, self.bInterfaceProtocol = c, sub, proto
        self.bInterfaceNumber, self.iInterface = num, istr

class Dev:
    def __init__(self, d):
        self.d, self.bus, self.port_numbers, self.serial_number = d, d['bus'], tuple(d['ports']), d['serial']
    def get_active_configuration(self):
        return [Itf(*i) for i in self.d['itfs']]
    def ctrl_transfer(self, *a):
        with open(os.environ['STUB_LOG'], 'a') as f:
            f.write(json.dumps(list(a)) + '\\n')

def find(find_all, idVendor, idProduct):
    return [Dev(d) for d in json.loads(os.environ['STUB_USB']) if (d['vid'], d['pid']) == (idVendor, idProduct)]
""",
    'usb/util.py': """def get_string(dev, index):
    return dev.d['strings'][str(index)]
""",
}


def setUpModule():
    guard_survivors(__name__)


class UsbDfu(unittest.TestCase):
    APP = {'vid': 0xCAFE, 'pid': 0x4000, 'bus': 3, 'ports': [1, 2], 'serial': '7CDFA1E06FD0',
           'itfs': [[0xFE, 1, 1, 2, 4]], 'strings': {'4': 'HIL 00ff'}}

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.stub = self.tmp / 'stub'
        for rel, text in STUB_USB.items():
            (self.stub / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.stub / rel).write_text(text)

    def call(self, op, devs, path='3-1.2'):
        log = self.tmp / 'usb.log'
        log.unlink(missing_ok=True)
        r = subprocess.run([sys.executable, '-B', SCRIPT, '--vid', '0xcafe', '--pid', '0x4000', '--serial', '7CDFA1E06FD0',
                            '--path', path, op], capture_output=True, text=True, timeout=T,
                           env=dict(os.environ, PYTHONPATH=str(self.stub), STUB_USB=json.dumps(devs), STUB_LOG=str(log)))
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1]), calls

    def test_string_and_detach_go_to_the_dfu_runtime_interface(self):
        other = dict(self.APP, ports=[1, 3], strings={'4': 'HIL other'})
        code, out, calls = self.call('string', [other, self.APP])
        self.assertEqual((code, out, calls), (0, {'ok': True, 'interface': 2, 'string': 'HIL 00ff'}, []))
        code, out, calls = self.call('detach', [self.APP])
        self.assertEqual((code, out), (0, {'ok': True, 'interface': 2}))
        self.assertEqual(calls, [[0x21, 0, 1000, 2, None, 1000]])

    def test_the_runtime_interface_is_picked_among_others_and_may_have_no_string(self):
        dev = dict(self.APP, itfs=[[0x02, 2, 0, 0, 5], [0xFE, 1, 2, 1, 6], [0xFE, 1, 1, 3, 0]])
        code, out, _ = self.call('string', [dev])
        self.assertEqual((code, out), (0, {'ok': True, 'interface': 3, 'string': None}))
        code, out, calls = self.call('detach', [dev])
        self.assertEqual((code, calls), (0, [[0x21, 0, 1000, 3, None, 1000]]))

    def test_no_exact_identity_two_matches_or_not_one_dfu_interface_is_an_error_with_no_request(self):
        cases = {'another path': ([self.APP], '3-1.3', '0 devices'),
                 'another bus': ([dict(self.APP, bus=4)], '3-1.2', '0 devices'),
                 'another serial at the path': ([dict(self.APP, serial='7CDFA1E06FD1')], '3-1.2', '0 devices'),
                 'another pid at the path': ([dict(self.APP, pid=0x4001)], '3-1.2', '0 devices'),
                 'two at one path': ([self.APP, self.APP], '3-1.2', '2 devices'),
                 'no dfu interface': ([dict(self.APP, itfs=[[0xFF, 0, 0, 0, 0]])], '3-1.2', '0 DFU runtime interfaces'),
                 'dfu mode only': ([dict(self.APP, itfs=[[0xFE, 1, 2, 0, 0]])], '3-1.2', '0 DFU runtime interfaces'),
                 'two dfu interfaces': ([dict(self.APP, itfs=[[0xFE, 1, 1, 0, 4], [0xFE, 1, 1, 1, 4]])], '3-1.2',
                                        '2 DFU runtime interfaces')}
        for what, (devs, path, err) in cases.items():
            for op in ('string', 'detach'):
                with self.subTest(what, op=op):
                    code, out, calls = self.call(op, devs, path)
                    self.assertEqual((code, out['ok'], calls), (1, False, []))
                    self.assertEqual(set(out), {'ok', 'error'})
                    self.assertIn(err, out['error'])


if __name__ == '__main__':
    unittest.main()
