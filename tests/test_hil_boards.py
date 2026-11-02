"""skills/hil/scripts/hil_boards.py: the farm's board registry gives each roster entry its
board's flasher by board uid. The shipped registry validates, aliases match exactly, and a
registered board's own flasher, an unknown board without one, or a bench board claiming a
registered probe are refused."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'hil' / 'scripts' / 'hil_boards.py'

spec = importlib.util.spec_from_file_location('agentrc_hil_boards', SCRIPT)
hb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hb)

JLINK = {'name': 'jlink', 'uid': '000621000000', 'args': '-device MK64FN1M0xxx12'}
RECOVER = {'name': 'openocd', 'args': '-f interface/jlink.cfg -f target/k60.cfg'}
REG = {'boards': {
    'AAAA': {'name': 'k64f', 'flasher': JLINK, 'flasher_recover': RECOVER},
    'BBBB': {'name': 'max32', 'aliases': ['BBBB0000'],
             'flasher': {'name': 'openocd', 'uid': 'E661', 'vid_pid': '0x2e8a 0x000c', 'args': '-f x.cfg'}},
}}


def setUpModule():
    guard_survivors(__name__)


class Base(unittest.TestCase):
    def registry(self, doc):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / 'boards.json'
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return str(path)


class Shipped(Base):
    def test_it_validates(self):
        self.assertGreater(len(hb.registry()), 30)


class Resolve(Base):
    def setUp(self):
        self.path = self.registry(REG)

    def test_a_registered_board_gets_its_flashers_in_a_new_dict(self):
        entry = {'name': 'frdm_k64f', 'uid': 'AAAA', 'tests': {'device': True}}
        out = hb.resolve(entry, self.path)
        self.assertEqual(out, {**entry, 'flasher': JLINK, 'flasher_recover': RECOVER})
        out['flasher']['uid'] = 'x'
        self.assertEqual(hb.resolve(entry, self.path)['flasher'], JLINK)
        self.assertNotIn('flasher', entry)

    def test_an_alias_matches_exactly(self):
        self.assertEqual(hb.resolve({'name': 'm', 'uid': 'BBBB0000'}, self.path)['flasher']['uid'], 'E661')
        key, entry = hb.lookup('BBBB0000', self.path)
        entry['aliases'].append('x')
        self.assertEqual((key, hb.lookup('BBBB0000', self.path)[1]['aliases']), ('BBBB', ['BBBB0000']))
        for uid in ('bbbb0000', 'BBBB000', 'BBBB00000'):
            with self.subTest(uid), self.assertRaisesRegex(ValueError, 'not in agentrc'):
                hb.resolve({'name': 'm', 'uid': uid}, self.path)

    def test_a_registered_board_with_its_own_flasher_is_refused(self):
        for extra in ({'flasher': JLINK}, {'flasher_recover': RECOVER}):
            with self.subTest(extra), self.assertRaisesRegex(ValueError, 'drop them from the roster'):
                hb.resolve({'name': 'k', 'uid': 'AAAA', **extra}, self.path)

    def test_an_unregistered_bench_board_keeps_its_own(self):
        bench = {'name': 'bench', 'uid': 'CCCC', 'flasher': {'name': 'stlink', 'uid': '0673'}}
        self.assertEqual(hb.resolve(bench, self.path), bench)

    def test_unregistered_without_a_flasher_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'bench: board CCCC is not in'):
            hb.resolve({'name': 'bench', 'uid': 'CCCC'}, self.path)

    def test_a_bench_board_on_a_registered_probe_is_refused(self):
        for probe in ('000621000000', '621000000', 'e661'):
            with self.subTest(probe), self.assertRaisesRegex(ValueError, 'is registered board'):
                hb.resolve({'name': 'b', 'uid': 'CCCC', 'flasher': {'name': 'jlink', 'uid': probe}}, self.path)

    def test_a_malformed_bench_flasher_or_entry_is_refused(self):
        for entry, why in (({'name': 'b', 'uid': 'C', 'flasher': {'name': 'jtag', 'uid': '1'}}, 'name'),
                           ({'name': 'b', 'uid': 'C', 'flasher': {'name': 'jlink'}}, 'no uid'),
                           ({'name': 'b', 'uid': 'C', 'flasher': {'name': 'jlink', 'uid': '1', 'verify': 1}},
                            'verify'),
                           ({'name': 'b', 'uid': 'C', 'flasher': {'name': 'jlink', 'uid': '1'},
                             'flasher_recover': {'name': 'openocd', 'uid': '1'}}, 'unknown key'),
                           ({'name': 'b', 'uid': 'C', 'flasher': {'name': 'openocd', 'uid': '1',
                                                                  'vid_pid': '2e8a:000c'}}, 'vid_pid'),
                           ({'name': 'b', 'uid': 'C', 'flasher': {'name': 'openocd', 'uid': '1'},
                             'flasher_recover': {'name': 'openocd', 'vid_pid': '0x2e8a 0x000c\nshutdown'}}, 'vid_pid'),
                           ({'name': 'b'}, 'no uid'), ({'name': 'b', 'uid': ' '}, 'no uid')):
            with self.subTest(entry), self.assertRaisesRegex(ValueError, why):
                hb.resolve(entry, self.path)

    def test_a_roster_resolves_both_lists_and_names_every_problem(self):
        roster = {'boards': [{'name': 'a', 'uid': 'AAAA'}], 'boards-skip': [{'name': 'b', 'uid': 'BBBB'}],
                  'other': 1}
        out = hb.resolve_roster(roster, self.path)
        self.assertEqual(out['boards'][0]['flasher'], JLINK)
        self.assertEqual(out['boards-skip'][0]['flasher']['uid'], 'E661')
        self.assertEqual(out['other'], 1)
        with self.assertRaises(ValueError) as cm:
            hb.resolve_roster({'boards': [{'name': 'x', 'uid': 'X1'}, {'name': 'y', 'uid': 'AAAA', 'flasher': JLINK}]},
                              self.path)
        self.assertIn('x: board X1', str(cm.exception))
        self.assertIn('y: flasher of board AAAA', str(cm.exception))


class Registry(Base):
    def refused(self, doc, why):
        with self.assertRaisesRegex(ValueError, why):
            hb.registry(self.registry(doc))

    def test_duplicate_json_keys(self):
        self.refused('{"boards": {"A": {"name": "a", "flasher": {"name": "jlink", "uid": "1"}}, '
                     '"A": {"name": "b", "flasher": {"name": "jlink", "uid": "2"}}}}', 'duplicate key')

    def test_identity_collisions(self):
        fl = lambda uid: {'name': 'jlink', 'uid': uid}  # noqa: E731
        self.refused({'boards': {'A': {'name': 'a', 'flasher': fl('1')},
                                 'B': {'name': 'b', 'aliases': ['a'], 'flasher': fl('2')}}}, "board uid a is also A's")
        self.refused({'boards': {'A': {'name': 'a', 'flasher': fl('000123')},
                                 'B': {'name': 'b', 'flasher': fl('123')}}}, "probe 123 is also A's")

    def test_malformed_entries(self):
        for doc, why in (({'boards': []}, 'expected'), ({'boards': {}, 'x': 1}, 'expected'),
                         ({'boards': {'A': {'flasher': {'name': 'jlink', 'uid': '1'}}}}, 'no name'),
                         ({'boards': {'A': {'name': 'a'}}}, 'no flasher'),
                         ({'boards': {'A': {'name': 'a', 'flasher': {'name': 'jlink', 'uid': '1'}, 'x': 1}}},
                          'unknown key'),
                         ({'boards': {'A': {'name': 'a', 'aliases': 'B', 'flasher': {'name': 'jlink', 'uid': '1'}}}},
                          'aliases'),
                         ({'boards': {'A': {'name': 'a', 'flasher': {'name': 'jlink', 'uid': '1'},
                                            'flasher_recover': {'name': 'openocd', 'uid': '1'}}}}, 'unknown key')):
            with self.subTest(doc):
                self.refused(doc, why)

    def test_a_missing_registry_names_its_path(self):
        with self.assertRaisesRegex(ValueError, 'absent.json'):
            hb.registry('/nonexistent/absent.json')


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_roster_prints_the_resolved_roster(self):
        with tempfile.TemporaryDirectory() as td:
            roster = Path(td) / 'r.json'
            uid = next(iter(hb.registry()))
            roster.write_text(json.dumps({'boards': [{'name': 'x', 'uid': uid}]}))
            r = self.run_cli('roster', str(roster))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)['boards'][0]['flasher'], hb.registry()[uid]['flasher'])
            roster.write_text(json.dumps({'boards': [{'name': 'x', 'uid': 'NOPE'}]}))
            r = self.run_cli('roster', str(roster))
            self.assertEqual((r.returncode, r.stdout), (1, ''))
            self.assertIn('board NOPE is not in', r.stderr)
        self.assertEqual(self.run_cli().returncode, 2)


if __name__ == '__main__':
    unittest.main()
