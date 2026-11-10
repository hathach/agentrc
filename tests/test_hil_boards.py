"""skills/hil/scripts/hil_boards.py: the farm's board registry gives each roster entry its
board's flasher by probe serial. The shipped registry validates, a probe matches exactly, and
a registered board's own flasher, an unknown probe, an entry with neither, or a bench board
claiming a registered probe are refused. Its `dev_attached` reaches the resolved entry's top
level; a roster's own copy is compared and warned about, never refused, during the migration."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, tmpdir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'hil' / 'scripts' / 'hil_boards.py'

spec = importlib.util.spec_from_file_location('agentrc_hil_boards', SCRIPT)
hb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hb)

JLINK = {'name': 'jlink', 'args': '-device MK64FN1M0xxx12'}
RECOVER = {'name': 'openocd', 'args': '-f interface/jlink.cfg -f target/k60.cfg'}
OPENOCD = {'name': 'openocd', 'vid_pid': '0x2e8a 0x000c', 'args': '-f x.cfg'}
REG = {'boards': {
    '000621000000': {'name': 'k64f', 'flasher': JLINK, 'flasher_recover': RECOVER},
    'E661': {'name': 'max32', 'flasher': OPENOCD},
}}


def setUpModule():
    guard_survivors(__name__)


class Base(unittest.TestCase):
    def registry(self, doc):
        path = tmpdir(self) / 'boards.json'
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return str(path)


class Shipped(Base):
    def test_it_validates(self):
        self.assertGreater(len(hb.registry()), 30)
        self.assertTrue(any('dev_attached' in e for e in hb.registry().values()))


class Resolve(Base):
    def setUp(self):
        self.path = self.registry(REG)

    def test_a_registered_board_gets_its_flashers_in_a_new_dict(self):
        entry = {'name': 'frdm_k64f', 'uid': 'AAAA', 'probe': '000621000000', 'tests': {'device': True}}
        out = hb.resolve(entry, self.path)
        self.assertEqual(out, {**entry, 'flasher': {**JLINK, 'uid': '000621000000'}, 'flasher_recover': RECOVER})
        out['flasher_recover']['args'] = 'x'
        self.assertEqual(hb.resolve(entry, self.path)['flasher_recover'], RECOVER)
        self.assertNotIn('flasher', entry)

    def test_the_uid_is_the_rosters_own(self):
        for uid in ('BBBB', 'BBBB0000'):
            out = hb.resolve({'name': 'm', 'uid': uid, 'probe': 'E661'}, self.path)
            self.assertEqual((out['uid'], out['flasher']), (uid, {**OPENOCD, 'uid': 'E661'}))

    def test_a_probe_matches_exactly(self):
        hb.resolve({'name': 'm', 'uid': 'M', 'probe': 'E661'}, self.path)['flasher']['args'] = 'x'
        self.assertEqual(hb.registry(self.path)['E661']['flasher'], OPENOCD)
        for probe in ('e661', '621000000', 'E6610', 1):
            with self.subTest(probe), self.assertRaisesRegex(ValueError, 'is not in agentrc'):
                hb.resolve({'name': 'm', 'uid': 'M', 'probe': probe}, self.path)

    def test_a_registered_board_with_its_own_flasher_is_refused(self):
        for extra in ({'flasher': {**JLINK, 'uid': '000621000000'}}, {'flasher_recover': RECOVER}):
            with self.subTest(extra), self.assertRaisesRegex(ValueError, 'drop them from the roster'):
                hb.resolve({'name': 'k', 'uid': 'K', 'probe': '000621000000', **extra}, self.path)

    def test_an_unregistered_bench_board_keeps_its_own(self):
        bench = {'name': 'bench', 'uid': 'CCCC', 'flasher': {'name': 'stlink', 'uid': '0673'}}
        self.assertEqual(hb.resolve(bench, self.path), bench)

    def test_an_entry_with_neither_probe_nor_flasher_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'bench: board CCCC names neither a registered probe nor a flasher'):
            hb.resolve({'name': 'bench', 'uid': 'CCCC'}, self.path)

    def test_a_bench_board_on_a_registered_probe_is_refused(self):
        for probe, key in (('000621000000', '000621000000'), ('621000000', '000621000000'), ('e661', 'E661')):
            with self.subTest(probe), self.assertRaisesRegex(ValueError, f'is registered .*"probe": "{key}"'):
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
                           ({'name': 'b', 'flasher': {'name': 'jlink', 'uid': '1'}}, 'no uid'),
                           ({'name': 'b', 'uid': ' ', 'probe': 'E661'}, 'no uid')):
            with self.subTest(entry), self.assertRaisesRegex(ValueError, why):
                hb.resolve(entry, self.path)

    def test_a_roster_resolves_both_lists_and_names_every_problem(self):
        roster = {'boards': [{'name': 'a', 'uid': 'A', 'probe': '000621000000'}],
                  'boards-skip': [{'name': 'b', 'uid': 'B', 'probe': 'E661'}],
                  'other': 1}
        out = hb.resolve_roster(roster, self.path)
        self.assertEqual(out['boards'][0]['flasher'], {**JLINK, 'uid': '000621000000'})
        self.assertEqual(out['boards-skip'][0]['flasher']['uid'], 'E661')
        self.assertEqual(out['other'], 1)
        with self.assertRaises(ValueError) as cm:
            hb.resolve_roster({'boards': [{'name': 'x', 'uid': 'X', 'probe': 'X1'},
                                          {'name': 'y', 'uid': 'Y', 'probe': 'E661', 'flasher': OPENOCD}]}, self.path)
        self.assertIn("x: probe 'X1' is not in", str(cm.exception))
        self.assertIn('y: flasher of probe E661', str(cm.exception))


UDISK = {'vid_pid': 'abcd:1234', 'serial': '\u0409', 'is_msc': True, 'block_size': 512, 'block_count': 1966080,
         'msc_inquiry': 'General  UDisk            5.00'}
CH340 = {'vid_pid': '1a86:7523', 'note': 'no serial read'}


class DevAttached(Base):
    def setUp(self):
        hb._warned.clear()
        self.path = self.registry({'boards': {**REG['boards'], 'E663': {'name': 'pico2', 'flasher': OPENOCD,
                                                                        'dev_attached': [UDISK, CH340]}}})

    def resolve(self, entry):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = hb.resolve(entry, self.path)
        return out, err.getvalue()

    def test_the_registry_copy_is_at_the_top_level_in_a_new_dict(self):
        out, err = self.resolve({'name': 'p', 'uid': 'P', 'probe': 'E663', 'tests': {'host': True}})
        self.assertEqual((out['dev_attached'], out['tests'], err), ([UDISK, CH340], {'host': True}, ''))
        out['dev_attached'][0]['serial'] = 'x'
        self.assertEqual(hb.registry(self.path)['E663']['dev_attached'][0], UDISK)
        self.assertNotIn('dev_attached', self.resolve({'name': 'k', 'uid': 'K', 'probe': '000621000000'})[0])

    def test_an_equal_legacy_copy_stays_nested_with_a_warning(self):
        tinyusb = [{**UDISK, 'vid_pid': 'ABCD_1234'}, {'vid_pid': '1a86_7523', 'serial': '0'}]
        arduino = [{'vid_pid': 'abcd:1234', 'serial': '\u0409'}, {'vid_pid': '1a86:7523', 'serial': '0'}]
        entry = {'name': 'p', 'uid': 'P', 'probe': 'E663', 'tests': {'dev_attached': tinyusb},
                 'arduino': {'dev_attached': arduino}}
        out, err = self.resolve(entry)
        self.assertEqual((out['tests']['dev_attached'], out['arduino']['dev_attached']), (tinyusb, arduino))
        self.assertEqual(out['dev_attached'], [UDISK, CH340])
        self.assertIn('p: tests.dev_attached duplicates agentrc', err)
        self.assertIn('p: arduino.dev_attached duplicates agentrc', err)
        self.assertEqual(self.resolve(entry)[1], '')   # once per message

    def test_a_differing_or_malformed_legacy_copy_warns_and_is_kept(self):
        for local, why in (([{'vid_pid': '0951:1603', 'serial': 'K'}], "0951:1603 'K'"),
                           ([{'vid_pid': 'abcd:1234', 'serial': '\u0409'}], '1a86:7523 \\(no serial\\)'),
                           ([{**UDISK, 'block_count': 1}, CH340], 'block_count differ'),
                           ([UDISK, UDISK, CH340], 'listed twice'),
                           ([{'serial': 'x'}], 'without a vid_pid'),
                           ('nope', 'not a list of objects')):
            with self.subTest(local):
                hb._warned.clear()
                out, err = self.resolve({'name': 'p', 'uid': 'P', 'probe': 'E663', 'arduino': {'dev_attached': local}})
                self.assertEqual((out['arduino']['dev_attached'], out['dev_attached']), (local, [UDISK, CH340]))
                self.assertRegex(err, f'p: arduino.dev_attached differs from .*{why}.*still get the roster')

    def test_a_null_legacy_copy_warns_at_every_path(self):
        for where, entry in (('dev_attached', {'dev_attached': None}),
                             ('tests.dev_attached', {'tests': {'dev_attached': None}}),
                             ('arduino.dev_attached', {'arduino': {'dev_attached': None}})):
            with self.subTest(where):
                err = self.resolve({'name': 'p', 'uid': 'P', 'probe': 'E663', **entry})[1]
                self.assertIn(f'p: {where} differs from agentrc\'s board registry (pico2): not a list of objects', err)

    def test_a_top_level_copy_is_replaced_by_the_registrys(self):
        out, err = self.resolve({'name': 'p', 'uid': 'P', 'probe': 'E663', 'dev_attached': [UDISK]})
        self.assertEqual(out['dev_attached'], [UDISK, CH340])
        self.assertIn("the registry's replaces it", err)

    def test_without_a_registry_copy_and_on_a_bench_board_the_rosters_stands(self):
        legacy = {'tests': {'dev_attached': [{'vid_pid': '0951_1603', 'serial': 'K'}]}}
        out, err = self.resolve({'name': 'k', 'uid': 'K', 'probe': '000621000000', **legacy})
        self.assertEqual((out['tests'], 'dev_attached' in out, err), (legacy['tests'], False, ''))
        bench = {'name': 'b', 'uid': 'C', 'flasher': {'name': 'stlink', 'uid': '0673'}, 'dev_attached': ['any']}
        self.assertEqual(self.resolve(bench), (bench, ''))

    def test_malformed_registry_inventories_are_refused(self):
        for devices, why in (([], 'non-empty list'), ({'a': 1}, 'non-empty list'), (['x'], 'not an object'),
                             ([{**UDISK, 'x': 1}], 'unknown key'), ([{**UDISK, 'vid_pid': 'ABCD:1234'}], 'lowercase'),
                             ([{**UDISK, 'vid_pid': 'abcd_1234'}], 'lowercase'), ([{'serial': 'x'}], 'vid_pid'),
                             ([{**UDISK, 'serial': ''}], 'serial'), ([{**UDISK, 'serial': None}], 'serial'),
                             ([{**UDISK, 'serial': '0'}], 'serial'),
                             ([{**UDISK, 'is_msc': 1}], 'is_msc'), ([{**UDISK, 'block_count': True}], 'block_count'),
                             ([{**UDISK, 'block_size': 0}], 'block_size'), ([{**UDISK, 'note': ' '}], 'note'),
                             ([UDISK, UDISK], 'listed twice'), ([CH340, CH340], 'listed twice')):
            with self.subTest(devices), self.assertRaisesRegex(ValueError, f'A: dev_attached: .*{why}'):
                hb.registry(self.registry({'boards': {'A': {'name': 'a', 'flasher': {'name': 'jlink'},
                                                            'dev_attached': devices}}}))


class LockName(Base):
    def setUp(self):
        self.path = self.registry(REG)

    def test_a_registered_board_locks_as_its_registry_name_whatever_the_roster_calls_it(self):
        for entry in ({'name': 'frdm_k64f', 'probe': '000621000000'}, {'name': 'k64f', 'probe': '000621000000'}):
            self.assertEqual(hb.lock_name(entry, path=self.path), 'k64f')
        self.assertEqual(hb.lock_name({'name': 'x', 'probe': 'E661'}, 'max32', self.path), 'max32')

    def test_a_registered_board_with_another_lock_is_refused(self):
        with self.assertRaisesRegex(ValueError, "x: lock 'mimxrt' is not its registered board's, max32"):
            hb.lock_name({'name': 'x', 'probe': 'E661'}, 'mimxrt', self.path)
        with self.assertRaisesRegex(ValueError, "probe 'NOPE' is not in agentrc"):
            hb.lock_name({'name': 'x', 'probe': 'NOPE'}, path=self.path)

    def test_a_bench_board_locks_as_its_own(self):
        self.assertEqual(hb.lock_name({'name': 'bench'}, path=self.path), 'bench')
        self.assertEqual(hb.lock_name({'name': 'bench'}, 'shared', self.path), 'shared')


class Registry(Base):
    def refused(self, doc, why):
        with self.assertRaisesRegex(ValueError, why):
            hb.registry(self.registry(doc))

    def test_duplicate_json_keys(self):
        self.refused('{"boards": {"A": {"name": "a", "flasher": {"name": "jlink"}}, '
                     '"A": {"name": "b", "flasher": {"name": "jlink"}}}}', 'duplicate key')

    def test_one_probe_under_two_keys(self):
        self.refused({'boards': {'000123': {'name': 'a', 'flasher': {'name': 'jlink'}},
                                 '123': {'name': 'b', 'flasher': {'name': 'jlink'}}}}, '123: the same probe as 000123')

    def test_a_name_is_a_unique_lock_file_name(self):
        fl = {'name': 'jlink'}
        self.refused({'boards': {'A': {'name': 'k64f', 'flasher': fl}, 'B': {'name': 'K64F', 'flasher': fl}}},
                     "B: name K64F is also A's")
        for name in ('a/b', '.hidden', 'a b', 'a+b', 7):
            with self.subTest(name):
                self.refused({'boards': {'A': {'name': name, 'flasher': fl}}}, 'not a plain file name')

    def test_malformed_entries(self):
        fl = {'name': 'jlink'}
        for doc, why in (({'boards': []}, 'expected'), ({'boards': {}, 'x': 1}, 'expected'),
                         ({'boards': {'A': {'flasher': fl}}}, 'name None'),
                         ({'boards': {' ': {'name': 'a', 'flasher': fl}}}, 'empty probe serial'),
                         ({'boards': {'A': {'name': 'a'}}}, 'no flasher'),
                         ({'boards': {'A': {'name': 'a', 'flasher': fl, 'x': 1}}}, 'unknown key'),
                         ({'boards': {'A': {'name': 'a', 'flasher': {'name': 'jlink', 'uid': '1'}}}}, 'unknown key'),
                         ({'boards': {'A': {'name': 'a', 'flasher': fl,
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
            probe = next(iter(hb.registry()))
            roster.write_text(json.dumps({'boards': [{'name': 'x', 'uid': 'X', 'probe': probe}]}))
            r = self.run_cli('roster', str(roster))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)['boards'][0]['flasher'],
                             {**hb.registry()[probe]['flasher'], 'uid': probe})
            roster.write_text(json.dumps({'boards': [{'name': 'x', 'uid': 'X', 'probe': 'NOPE'}]}))
            r = self.run_cli('roster', str(roster))
            self.assertEqual((r.returncode, r.stdout), (1, ''))
            self.assertIn("probe 'NOPE' is not in", r.stderr)
        self.assertEqual(self.run_cli().returncode, 2)

    def test_lock_prints_a_boards_lock_parked_ones_included(self):
        probe, reg = next(iter(hb.registry().items()))
        with tempfile.TemporaryDirectory() as td:
            roster = Path(td) / 'r.json'
            bench = {'name': 'bench', 'uid': 'B', 'flasher': {'name': 'jlink', 'uid': '1'}}
            parked = {'name': 'its-name', 'uid': 'X', 'probe': probe}
            roster.write_text(json.dumps({'boards': [bench], 'boards-skip': [parked]}))
            self.assertEqual(self.run_cli('lock', str(roster), 'its-name').stdout, reg['name'] + '\n')
            self.assertEqual(self.run_cli('lock', str(roster), 'bench').stdout, 'bench\n')
            r = self.run_cli('lock', str(roster), 'absent')
            self.assertEqual((r.returncode, r.stdout), (1, ''))
            self.assertIn("0 entries named 'absent'", r.stderr)
        self.assertEqual(self.run_cli('lock', 'x').returncode, 2)


if __name__ == '__main__':
    unittest.main()
