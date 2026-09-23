"""Tests for the read-pcb skill's pcb.py: EAGLE and KiCad schematics load into
one model, every pin keeps its own identity, and a source that cannot give a
complete, unambiguous answer is refused with a named reason."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

if os.name == 'nt':
    raise unittest.SkipTest('read-pcb requires POSIX file locking')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'read-pcb' / 'scripts'))
import pcb  # noqa: E402

DATA = ROOT / 'tests' / 'data' / 'read_pcb'
EAGLE = DATA / 'eagle.sch'
KICAD = DATA / 'kicad'
needs_kicad = unittest.skipUnless(shutil.which('kicad-cli'),
                                  'kicad-cli is not installed; the real export path is unverified here')
VBUS_NET = ['VBUS', '  R1  1 (A)  10k  t:R', '  R2  1 (A)  10k  t:R']

# A stand-in kicad-cli: FAKE_KICAD=fail exits 1; =edit writes a netlist, then
# rewrites the schematic at its old size and mtime.
FAKE_KICAD = f"""#!{sys.executable}
import os, sys
if os.environ['FAKE_KICAD'] == 'fail':
    sys.exit('Failed to load schematic')
with open(sys.argv[sys.argv.index('-o') + 1], 'w') as fh:
    fh.write('<export/>')
sch = sys.argv[-1]
st = os.stat(sch)
with open(sch) as fh:
    text = fh.read().replace('"u"', '"v"')
with open(sch, 'w') as fh:
    fh.write(text)
os.utime(sch, ns=(st.st_atime_ns, st.st_mtime_ns))
"""


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = pcb.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, data):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def assertRefused(self, why, *args, code=3):
        got, _, err = run(*args)
        self.assertEqual(got, code, why)
        self.assertIn(why, err)

    def eagle_variant(self, old, new):
        text = EAGLE.read_text()
        self.assertIn(old, text)
        return self.write('variant.sch', text.replace(old, new, 1))


class Eagle(Tmp):
    def test_a_net_named_on_two_sheets_is_one_net_and_a_repeated_pinref_counts_once(self):
        code, out, _ = run('net', EAGLE, 'SIG')
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1:], ['SIG',
                                                '  Q1  1.D  pad 6  BSS138DW  t:DUALFET',
                                                '  R1  1  10k  t:R_0603',
                                                '  R2  1  1k  t:R_0603'])

    def test_a_part_lists_every_pin_with_pads_and_says_when_a_pin_has_no_net(self):
        _, out, _ = run('part', EAGLE, 'Q1')
        self.assertEqual(out.splitlines()[2:], ['  1.D  pad 6  SIG',
                                                '  1.S  pad 1,2  (no net)',
                                                '  2.D  pad 3  (no net)',
                                                '  2.S  pad 4  GND'])

    def test_the_gate_is_shown_only_where_the_pin_name_repeats(self):
        _, out, _ = run('part', EAGLE, 'R1')
        self.assertIn('  1  SIG', out)
        self.assertNotIn('G$1', out)

    def test_a_supply_symbol_is_a_terminal_and_has_no_package(self):
        _, out, _ = run('net', EAGLE, 'GND')
        self.assertIn('  GND1  GND  t:GND', out)
        self.assertIn('  Q1  2.S  pad 4  BSS138DW  t:DUALFET', out)

    def test_bus_members_stay_separate_nets(self):
        _, out, _ = run('nets', EAGLE, 'B')
        self.assertEqual(out.splitlines()[1:], ['B0  (1)', 'B1  (1)'])

    def test_a_part_attribute_overrides_its_technology_default(self):
        _, out, _ = run('parts', EAGLE, 'R1')
        self.assertIn('MPN=RC0603FR-0710KL', out)
        _, out, _ = run('parts', EAGLE, 'rc0603-default')
        self.assertEqual(out.splitlines()[1:], ['R2  1k  t:R_0603  0603  MPN=RC0603-DEFAULT',
                                                'R3  0R  t:R_0603  0603  MPN=RC0603-DEFAULT  '
                                                '[DNP in variant lite]'])

    def test_a_do_not_populate_part_is_annotated_never_dropped(self):
        _, out, _ = run('net', EAGLE, 'B1')
        self.assertIn('  R3  1  0R  t:R_0603  [DNP in variant lite]', out)

    def test_every_answer_opens_with_its_source(self):
        _, out, _ = run('nets', EAGLE)
        self.assertRegex(out.splitlines()[0], rf'^source: {EAGLE} .*\[eagle\]$')

    def test_a_missing_net_or_part_exits_one_and_suggests_names_that_contain_it(self):
        code, out, _ = run('net', EAGLE, 'SI')
        self.assertEqual(code, 1)
        self.assertIn('did you mean SIG', out)
        self.assertEqual(run('part', EAGLE, 'U9')[0], 1)
        self.assertEqual(run('nets', EAGLE, 'nosuch')[0], 1)

    def test_a_part_ref_matching_two_parts_case_insensitively_is_ambiguous(self):
        design = pcb.Design('eagle', {r: pcb.Part(r, None, 't:R', None) for r in ('Uab', 'UaB')}, {})
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pcb.cmd_part(design, 'UAB'), 1)
            self.assertEqual(pcb.cmd_part(design, 'UaB'), 0)
        self.assertIn('no part UAB: did you mean Uab, UaB', out.getvalue())

    def test_one_pin_on_two_nets_is_refused_naming_both(self):
        path = self.eagle_variant('<pinref part="Q1" gate="2" pin="S"/>',
                                  '<pinref part="Q1" gate="2" pin="S"/><pinref part="R2" gate="G$1" pin="1"/>')
        self.assertRefused('R2 G$1.1 is on both SIG and GND', 'nets', path)

    def test_two_parts_with_one_name_are_refused(self):
        path = self.eagle_variant('<part name="R1" library="t" deviceset="R" device="_0603" value="10k">',
                                  '<part name="R1" library="t" deviceset="R" device="_0603" value="22k"/>'
                                  '<part name="R1" library="t" deviceset="R" device="_0603" value="10k">')
        self.assertRefused('two parts are named R1', 'nets', path)

    def test_a_pinref_no_part_defines_is_refused(self):
        path = self.eagle_variant('<pinref part="Q1" gate="2" pin="S"/>',
                                  '<pinref part="Q1" gate="3" pin="S"/>')
        self.assertRefused('Q1.3.S', 'nets', path)

    def test_a_part_whose_device_or_symbol_is_missing_is_refused(self):
        for old, new, why in (('device="_0603" value="1k"', 'device="_0805" value="1k"', "'_0805'"),
                              ('symbol="FET" x="0" y="10"', 'symbol="NOFET" x="0" y="10"', 'NOFET')):
            self.assertRefused(why, 'nets', self.eagle_variant(old, new))

    def test_a_missing_required_attribute_is_refused_not_a_traceback(self):
        for old, new, why in (('deviceset="R" device="_0603" value="1k"', 'device="_0603" value="1k"',
                               'R2 has no deviceset'),
                              ('pin="S" pad="4"', 'pin="S"', 'Q1 connect has no pad'),
                              ('<pinref part="Q1" gate="2" pin="S"/>', '<pinref part="Q1" pin="S"/>',
                               'a pinref on GND has no gate')):
            self.assertRefused(why, 'nets', self.eagle_variant(old, new))

    def test_hierarchical_modules_are_refused_but_an_empty_container_is_not(self):
        self.assertEqual(run('nets', self.eagle_variant('<parts>', '<moduleinsts/><parts>'))[0], 0)
        self.assertRefused('hierarchical modules', 'nets',
                           self.eagle_variant('<parts>', '<moduleinsts><moduleinst name="M1"/></moduleinsts><parts>'))

    def test_unreadable_formats_are_refused_with_what_to_do(self):
        for data, why in ((b'\x10\x80\x00binary', 'pre-6 binary EAGLE'),
                          (b'EESchema Schematic File Version 4\n', 'legacy KiCad 5'),
                          (b'<?xml version="1.0"?><eagle><drawing>', 'malformed EAGLE XML'),
                          (EAGLE.read_bytes().replace(b'<schematic>', b'<board>').replace(b'</schematic>', b'</board>'),
                           'is this a board file')):
            self.assertRefused(why, 'nets', self.write('x.sch', data))
        self.assertEqual(run('nets', self.tmp / 'absent.sch')[0], 3)


NETLIST = """<?xml version="1.0"?><export version="E"><components>
<comp ref="J1"><value>USB C</value><footprint>lib:USB_C</footprint>
  <fields><field name="Footprint">lib:USB_C</field><field name="MPN">CUSB31</field></fields>
  <libsource lib="conn" part="USB_C"/><sheetpath names="/usb/" tstamps="/a/"/></comp>
<comp ref="U1"><value>AP2112</value><libsource lib="reg" part="AP2112"/>
  <property name="dnp"/></comp>
</components><nets>
<net code="1" name="/D+"><node ref="J1" pin="A6" pinfunction="D+" pintype="bidirectional"/>
  <node ref="J1" pin="B6" pinfunction="D+" pintype="bidirectional"/></net>
<net code="2" name="unconnected-(J1-SBU1-PadA8)"><node ref="J1" pin="A8" pinfunction="SBU1"
  pintype="bidirectional+no_connect"/></net>
<net code="3" name="unconnected-by-choice"><node ref="U1" pin="4" pintype="passive"/></net>
<net code="4" name="/VOUT"><node ref="U1" pin="5" pinfunction="OUT" pintype="power_out"/>
  <node ref="U1" pin="2" pinfunction="OUT" pintype="power_out"/></net>
</nets></export>"""


class Refusals(unittest.TestCase):
    @contextmanager
    def refused(self, why, code=3):
        """The block must raise a Refusal with code, naming why."""
        with self.assertRaises(pcb.Refusal, msg=why) as e:
            yield
        self.assertEqual(e.exception.code, code, why)
        self.assertIn(why, str(e.exception))


class KicadNetlist(Refusals):
    def design(self, text=NETLIST):
        return pcb.load_kicad_netlist(text.encode(), 'board.kicad_sch')

    def test_pins_are_keyed_by_number_so_a_repeated_name_keeps_both_terminals(self):
        d = self.design()
        self.assertEqual([(p.pin, p.name) for p in d.nets['/D+']], [('A6', 'D+'), ('B6', 'D+')])
        self.assertEqual([p.pin for p in d.nets['/VOUT']], ['5', '2'])

    def test_a_pin_without_a_function_name_is_its_number(self):
        pin = self.design().nets['unconnected-by-choice'][0]
        self.assertEqual((pin.name, pin.label(set())), (None, '4'))

    def test_no_connect_comes_from_the_pin_type_never_from_the_net_name(self):
        d = self.design()
        self.assertTrue(d.nets['unconnected-(J1-SBU1-PadA8)'][0].no_connect)
        self.assertFalse(d.nets['unconnected-by-choice'][0].no_connect)

    def test_a_part_keeps_its_fields_sheet_and_dnp_without_repeating_the_footprint(self):
        d = self.design()
        self.assertEqual((d.parts['J1'].attributes, d.parts['J1'].sheet), ({'MPN': 'CUSB31'}, '/usb/'))
        self.assertEqual(d.parts['U1'].dnp, ['(all)'])

    def test_one_pin_on_two_nets_is_refused_naming_both(self):
        with self.refused('U1 pin 4 is on both unconnected-by-choice and /VOUT'):
            self.design(NETLIST.replace('<node ref="U1" pin="2"', '<node ref="U1" pin="4"'))

    def test_two_components_with_one_reference_are_refused_even_on_the_same_nets(self):
        with self.refused('two components are annotated U1'):
            self.design(NETLIST.replace('<comp ref="J1">', '<comp ref="U1"><value>22k</value></comp><comp ref="J1">'))

    def test_a_node_for_an_unlisted_part_is_refused(self):
        with self.refused('references U7'):
            self.design(NETLIST.replace('<node ref="U1" pin="4"', '<node ref="U7" pin="4"'))


ROOT_SHEET = '''(kicad_sch (version 20250114) (uuid "r-uuid")
  (sheet (property "Sheetname" "a") (property "Sheetfile" "{child}"))
  (sheet_instances (path "/" (page "1"))))'''
CHILD_SHEET = ROOT_SHEET.replace('(sheet_instances (path "/" (page "1")))', '')
SUB_SHEET = '''(kicad_sch (version 20250114) (uuid "s-uuid")
  (symbol (lib_id "t:R") (instances (project "p" (path "/r-uuid/sheet-uuid" (reference "R1"))))))'''
MIN_ROOT = '(kicad_sch (uuid "u") (sheet_instances (path "/")))'


def kicad_files(prefix=''):
    """The KiCad fixture project's files, keyed under prefix."""
    return {f'{prefix}{rel}': (KICAD / rel).read_bytes() for rel in ('board.kicad_sch', 'sheets/power.kicad_sch')}


class KicadSheets(Tmp, Refusals):
    def fake_kicad(self, mode):
        """kicad-cli on PATH is FAKE_KICAD in that mode; git stays real."""
        cli = self.write('bin/kicad-cli', FAKE_KICAD)
        cli.chmod(0o755)
        return unittest.mock.patch.dict(os.environ, {'FAKE_KICAD': mode,
                                                     'PATH': f'{cli.parent}{os.pathsep}{os.environ["PATH"]}'})

    def test_a_sub_sheet_is_refused_and_names_its_root(self):
        self.assertRefused('sub-sheet of the project whose root sheet has uuid r-uuid',
                           'nets', self.write('sub.kicad_sch', SUB_SHEET), code=2)

    def test_symbol_paths_under_the_files_own_uuid_prove_a_root(self):
        tree = pcb.sexpr(b'(kicad_sch (uuid "r") (symbol (instances (project "p" (path "/r")))))')
        self.assertEqual(pcb.sheet_role(tree), ('root', None))

    def test_a_sheet_marked_as_a_root_whose_symbols_belong_to_another_root_is_refused(self):
        mixed = SUB_SHEET[:-1] + ' (sheet_instances (path "/" (page "1"))))'
        self.assertRefused('cannot tell', 'nets', self.write('mixed.kicad_sch', mixed), code=2)
        self.assertEqual(pcb.tag(mixed.encode()), 'kicad, root unknown')

    def test_a_sheet_that_cannot_say_whether_it_is_a_root_is_refused(self):
        self.assertRefused('cannot tell', 'nets',
                           self.write('bare.kicad_sch', '(kicad_sch (version 20250114) (uuid "u"))'), code=2)

    def test_inputs_follow_sheet_files_relative_to_the_sheet_that_names_them(self):
        root = self.write('board/main.kicad_sch', ROOT_SHEET.format(child='sheets/a.kicad_sch'))
        self.write('board/sheets/a.kicad_sch', CHILD_SHEET.format(child='../shared/b.kicad_sch'))
        self.write('board/shared/b.kicad_sch', SUB_SHEET)
        self.write('board/main.kicad_pro', '{}')
        names = sorted(os.path.relpath(p, self.tmp) for p in pcb.kicad_inputs(str(root)))
        self.assertEqual(names, ['board/main.kicad_pro', 'board/main.kicad_sch',
                                 'board/shared/b.kicad_sch', 'board/sheets/a.kicad_sch'])

    def test_a_missing_sub_sheet_is_refused(self):
        root = self.write('main.kicad_sch', ROOT_SHEET.format(child='gone.kicad_sch'))
        with self.refused('gone.kicad_sch'):
            pcb.kicad_inputs(str(root))

    def test_a_missing_kicad_cli_is_named(self):
        root = self.write('main.kicad_sch', MIN_ROOT)
        with unittest.mock.patch.object(pcb.shutil, 'which', return_value=None):
            self.assertRefused('kicad-cli is not installed', 'nets', root)

    def test_an_input_replaced_during_export_is_refused_even_at_the_same_size_and_mtime(self):
        root = self.write('main.kicad_sch', MIN_ROOT)
        stat = root.stat()
        with self.fake_kicad('edit'):
            self.assertRefused('changed while KiCad exported it', 'nets', root)
        self.assertIn('"v"', root.read_text())
        self.assertEqual((root.stat().st_size, root.stat().st_mtime_ns), (stat.st_size, stat.st_mtime_ns))

    def test_a_kicad_cli_failure_is_refused_with_its_message(self):
        with self.fake_kicad('fail'):
            self.assertRefused('kicad-cli failed (1): Failed to load schematic',
                               'nets', self.write('main.kicad_sch', MIN_ROOT))

    def test_malformed_kicad_text_is_refused(self):
        self.assertRefused('malformed KiCad schematic', 'nets', self.write('bad.kicad_sch', '(kicad_sch (uuid "u")'))


@needs_kicad
class KicadExport(unittest.TestCase):
    """The fixture's VBUS label sits on R1 in the root sheet and R2 in sheets/power."""

    def test_kicad_joins_a_net_across_sheets_and_every_pin_keeps_its_number(self):
        code, out, err = run('net', KICAD / 'board.kicad_sch', 'VBUS')
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[1:], VBUS_NET)

    def test_a_part_shows_its_sheet_and_fields(self):
        _, out, _ = run('part', KICAD / 'board.kicad_sch', 'R2')
        self.assertIn('R2  10k  t:R  R_0603  MPN=RC0603', out)
        self.assertIn('sheet /power/', out)
        self.assertIn('  2 (A)  unconnected-(R2-A-Pad2)', out)

    def test_the_sub_sheet_alone_is_refused(self):
        self.assertEqual(run('nets', KICAD / 'sheets' / 'power.kicad_sch')[0], 2)


@needs_kicad
class KicadExportVariants(Tmp):
    ROOT_UUID = '11111111-1111-1111-1111-111111111111'

    def copy(self, root=lambda t: t, child=lambda t: t):
        board = self.write('board.kicad_sch', root((KICAD / 'board.kicad_sch').read_text()))
        self.write('sheets/power.kicad_sch', child((KICAD / 'sheets' / 'power.kicad_sch').read_text()))
        return board

    def test_a_root_without_sheet_instances_is_still_known_by_its_symbol_paths(self):
        board = self.copy(root=lambda t: t.replace('(sheet_instances (path "/" (page "1")))', ''))
        code, out, err = run('net', board, 'VBUS')
        self.assertEqual(code, 0, err)
        self.assertIn('R2', out)

    def test_nets_differing_only_in_case_are_each_reachable_by_exact_spelling(self):
        board = self.copy(child=lambda t: t.replace('(global_label "VBUS"', '(global_label "vbus"'))
        design, _ = pcb.load(str(board))

        def net(name):
            with redirect_stdout(io.StringIO()) as out:
                code = pcb.cmd_net(design, name)
            return code, out.getvalue().splitlines()
        self.assertEqual(net('VBUS'), (0, ['VBUS', '  R1  1 (A)  10k  t:R']))
        self.assertEqual(net('vbus'), (0, ['vbus', '  R2  1 (A)  10k  t:R']))
        code, out = net('Vbus')
        self.assertEqual(code, 1, 'two case-insensitive matches are ambiguous')
        self.assertIn('VBUS, vbus', out[0])

    def test_a_sub_sheet_used_twice_is_read_once_and_kicad_expands_both_instances(self):
        second = ('(sheet (at 80 50) (size 20 10)\n'
                  '    (uuid "88888888-8888-8888-8888-888888888888")\n'
                  '    (property "Sheetname" "power2" (at 80 49 0))\n'
                  '    (property "Sheetfile" "sheets/power.kicad_sch" (at 80 61 0))\n'
                  f'    (instances (project "board" (path "/{self.ROOT_UUID}" (page "3")))))\n  ')
        extra = (f'(path "/{self.ROOT_UUID}/88888888-8888-8888-8888-888888888888" '
                 '(reference "R3") (unit 1))')
        board = self.copy(root=lambda t: t.replace('(sheet_instances', second + '(sheet_instances'),
                          child=lambda t: t.replace('(reference "R2") (unit 1))', '(reference "R2") (unit 1))' + extra))
        self.assertEqual(sorted(os.path.relpath(p, self.tmp) for p in pcb.kicad_inputs(str(board))),
                         ['board.kicad_sch', 'sheets/power.kicad_sch'])
        code, out, err = run('net', board, 'VBUS')
        self.assertEqual(code, 0, err)
        self.assertEqual([l.split()[0] for l in out.splitlines()[2:]], ['R1', 'R2', 'R3'])

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                          env=dict(os.environ, **GIT_ENV)).stdout.decode().strip()


class Repos(Tmp):
    """Temp git repositories: working clones with an origin, and file:// remotes."""

    def setUp(self):
        super().setUp()
        env = {'XDG_CACHE_HOME': str(self.tmp / 'cache'),
               'READ_PCB_REMOTE_BASE': f'file://{self.tmp}/remotes/', 'READ_PCB_CLONES': ''}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, name, files, origin=None, commit=True):
        path = self.tmp / name
        path.mkdir(parents=True)
        git(path, 'init', '-q', '-b', 'main')
        for rel, data in files.items():
            self.write(f'{name}/{rel}', data)
        if commit:
            git(path, 'add', '-A')
            git(path, 'commit', '-qm', 'init', '--allow-empty')
        if origin:
            git(path, 'remote', 'add', 'origin', origin)
        return path

    def remote(self, source, files):
        """A bare file:// remote for source, and the work tree that pushes to it."""
        work = self.repo(f'work/{source}', files)
        bare = self.tmp / 'remotes' / f'{source}.git'
        git(work, 'clone', '-q', '--bare', '.', str(bare))
        for key in ('uploadpack.allowFilter', 'uploadpack.allowAnySHA1InWant'):
            git(bare, 'config', key, 'true')
        git(work, 'remote', 'add', 'origin', str(bare))
        return work

    def push(self, work, files, branch='main'):
        for rel, data in files.items():
            self.write(f'{os.path.relpath(work, self.tmp)}/{rel}', data)
        git(work, 'add', '-A')
        git(work, 'commit', '-qm', 'next')
        git(work, 'push', '-q', 'origin', f'HEAD:{branch}')

    def clones(self, *paths):
        os.environ['READ_PCB_CLONES'] = os.pathsep.join(str(p) for p in paths)


class Clones(Repos, Refusals):
    def test_origins_in_any_github_form_map_to_their_source(self):
        for i, url in enumerate(('https://github.com/hathach/pcb', 'git@github.com:hathach/pcb.git',
                                 'ssh://git@github.com/Hathach/PCB.git/')):
            path = self.repo(f'c{i}', {}, url)
            self.clones(path)
            self.assertEqual(pcb.clones(), {'hathach/pcb': str(path)}, url)

    def test_a_tilde_entry_is_expanded(self):
        path = self.repo('home/pcb', {}, 'git@github.com:hathach/pcb.git')
        with unittest.mock.patch.dict(os.environ, {'HOME': str(self.tmp / 'home')}):
            self.clones('~/pcb')
            self.assertEqual(pcb.clones(), {'hathach/pcb': str(path)})

    def test_a_lookalike_host_is_not_github(self):
        for i, url in enumerate(('https://notgithub.com/adafruit/MBAdafruitBoards.git',
                                 'https://example.invalid/github.com/adafruit/MBAdafruitBoards.git',
                                 'git@github.com.evil:adafruit/MBAdafruitBoards.git')):
            self.clones(self.repo(f'h{i}', {}, url))
            with self.refused('is not one of'):
                pcb.clones()

    def test_doubtful_entries_are_refused_by_name(self):
        other = self.repo('other', {}, 'git@github.com:someone/else.git')
        a = self.repo('a', {}, 'git@github.com:hathach/pcb.git')
        b = self.repo('b', {}, 'https://github.com/hathach/pcb')
        for entries, why in (([other], 'not one of'), ([self.tmp / 'nowhere'], 'not a git repository'),
                             ([a, b], 'two clones of hathach/pcb')):
            self.clones(*entries)
            with self.refused(why):
                pcb.clones()


class LocalClone(Repos):
    def setUp(self):
        super().setUp()
        self.clone = self.repo('mb clone', {'boards/Hub Rev A.sch': EAGLE.read_bytes(),
                                            'boards/Hub Rev B.sch': EAGLE.read_bytes(),
                                            'boards/notes.txt': 'x'},
                               'git@github.com:adafruit/MBAdafruitBoards.git')
        self.pcb = self.repo('pcb', {}, 'git@github.com:hathach/pcb.git')
        self.clones(self.clone, self.pcb)
        self.name = 'adafruit/MBAdafruitBoards:boards/Hub Rev B.sch'

    def header(self, spec=None):
        code, out, err = run('nets', spec or self.name)
        self.assertEqual(code, 0, err)
        return out.splitlines()[0]

    def test_find_lists_every_revision_with_its_source_name(self):
        code, out, _ = run('find', 'hub')
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ['adafruit/MBAdafruitBoards:boards/Hub Rev A.sch  [eagle]',
                                            'adafruit/MBAdafruitBoards:boards/Hub Rev B.sch  [eagle]'])
        self.assertEqual(run('find', 'hub', 'rev b')[1].count('\n'), 1, 'keywords AND together')
        self.assertEqual(run('find', 'nosuch')[0], 1)

    def test_find_marks_modified_and_untracked_files(self):
        self.write('mb clone/boards/Hub Rev B.sch', EAGLE.read_text().replace('10k', '22k'))
        self.write('mb clone/boards/Hub Rev C.sch', EAGLE.read_bytes())
        out = run('find', 'hub')[1]
        self.assertIn('Hub Rev B.sch  [eagle] [modified]', out)
        self.assertIn('Hub Rev C.sch  [eagle] [untracked]', out)

    def test_a_staged_rename_marks_only_the_new_path(self):
        git(self.clone, 'mv', 'boards/Hub Rev A.sch', 'boards/Hub Rev A2.sch')
        out = run('find', 'hub')[1].splitlines()
        self.assertIn('adafruit/MBAdafruitBoards:boards/Hub Rev A2.sch  [eagle] [modified]', out)
        self.assertIn('adafruit/MBAdafruitBoards:boards/Hub Rev B.sch  [eagle]', out)

    def test_every_input_is_compared_with_the_one_commit_the_header_cites(self):
        old = git(self.clone, 'rev-parse', 'HEAD')
        path = self.write('mb clone/boards/Hub Rev B.sch', EAGLE.read_text().replace('10k', '22k'))
        git(self.clone, 'commit', '-qam', 'newer')
        self.assertEqual(pcb.worktree_state(str(self.clone), old, str(path), path.read_bytes()), 'modified')
        self.assertIn('working tree clean', run('nets', self.name)[1])

    def test_a_path_after_the_colon_cannot_leave_the_repository(self):
        self.write('outside.sch', EAGLE.read_bytes())
        for spec in ('adafruit/MBAdafruitBoards:../outside.sch', f'adafruit/MBAdafruitBoards:{self.tmp}/outside.sch',
                     'adafruit/MBAdafruitBoards:boards/../../outside.sch'):
            self.assertRefused('must stay inside adafruit/MBAdafruitBoards', 'nets', spec, code=2)

    def test_a_name_starting_with_two_dots_is_inside_the_repository(self):
        self.write('mb clone/..board.sch', EAGLE.read_bytes())
        git(self.clone, 'add', '..board.sch')
        git(self.clone, 'commit', '-qm', 'dots')
        name = 'adafruit/MBAdafruitBoards:..board.sch'
        self.assertIn(f'source: {name} @', self.header(str(self.clone / '..board.sch')))
        self.assertIn('working tree clean', self.header(name))

    def test_a_path_through_a_symlink_is_resolved_to_the_clone(self):
        link = self.tmp / 'link'
        link.symlink_to(self.clone)
        self.assertIn(f'source: {self.name} @', self.header(str(link / 'boards' / 'Hub Rev B.sch')))
        self.assertIn('working tree clean', self.header(str(link / 'boards' / 'Hub Rev B.sch')))
        code, out, err = run('find', '--in', link / 'boards', 'rev b')
        self.assertEqual((code, out.splitlines()), (0, [f'{self.clone}/boards/Hub Rev B.sch  [eagle]']), err)

    def test_a_deleted_file_is_skipped_and_the_rest_still_listed(self):
        (self.clone / 'boards' / 'Hub Rev A.sch').unlink()
        code, out, _ = run('find', 'hub')
        self.assertEqual((code, out.splitlines()), (0, ['adafruit/MBAdafruitBoards:boards/Hub Rev B.sch  [eagle]']))
        self.assertEqual(run('find', '--in', self.clone)[1].splitlines(), [f'{self.clone}/boards/Hub Rev B.sch  [eagle]'])

    def test_an_unreadable_candidate_is_reported_and_the_rest_still_listed(self):
        path = self.clone / 'boards' / 'Hub Rev A.sch'
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        if os.access(path, os.R_OK):
            self.skipTest('running as a user that can read mode-000 files')
        code, out, _ = run('find', 'hub')
        self.assertIn('Hub Rev A.sch  [unreadable:', out)
        self.assertIn('Hub Rev B.sch  [eagle]', out)
        self.assertEqual(code, 3)
        self.assertIn('search incomplete: 1 file(s) could not be classified', out)

    def test_a_git_failure_is_provenance_unavailable_not_a_state(self):
        self.write('mb clone/boards/New.sch', EAGLE.read_bytes())
        (self.clone / '.git' / 'index').write_bytes(b'corrupt')
        self.assertIn('working tree clean', self.header(), 'a committed file needs only the commit')
        self.assertIn('working tree provenance unavailable', self.header('adafruit/MBAdafruitBoards:boards/New.sch'))

    def test_a_clean_file_cites_its_source_commit_and_clone(self):
        sha = git(self.clone, 'rev-parse', 'HEAD')[:12]
        self.assertEqual(self.header(), f'source: {self.name} @ {sha} | local clone {self.clone}, '
                                        'working tree clean [eagle]')

    def test_a_filesystem_path_inside_a_clone_is_cited_by_its_source_name(self):
        self.assertIn(f'source: {self.name} @', self.header(str(self.clone / 'boards' / 'Hub Rev B.sch')))

    def test_modified_means_the_bytes_read_differ_from_head_including_staged_only(self):
        path = self.write('mb clone/boards/Hub Rev B.sch', EAGLE.read_text().replace('10k', '22k'))
        self.assertIn('working tree modified', self.header())
        git(self.clone, 'add', str(path))
        self.assertIn('working tree modified', self.header(), 'staged but not committed')

    def test_untracked_and_no_head_are_named(self):
        self.write('mb clone/boards/New.sch', EAGLE.read_bytes())
        self.assertIn('working tree untracked', self.header('adafruit/MBAdafruitBoards:boards/New.sch'))
        empty = self.repo('empty', {'x.sch': EAGLE.read_bytes()}, commit=False)
        self.assertIn('@ (no HEAD)', self.header(str(empty / 'x.sch')))

    def test_a_file_outside_git_says_so(self):
        path = self.write('loose/x.sch', EAGLE.read_bytes())
        self.assertEqual(self.header(str(path)), f'source: {path} | not in git [eagle]')

    def test_a_missing_head_blob_in_a_partial_clone_is_never_downloaded(self):
        self.remote('hathach/pcb', {'a/x.sch': EAGLE.read_bytes()})
        part = self.tmp / 'partial'
        git(self.tmp, 'clone', '-q', '--filter=blob:none', '--no-checkout',
            f'file://{self.tmp}/remotes/hathach/pcb.git', str(part))
        git(part, 'reset', '-q')
        git(part, 'remote', 'set-url', 'origin', 'https://github.com/hathach/pcb')
        self.write('partial/a/x.sch', EAGLE.read_bytes())
        self.clones(self.clone, part)
        self.assertIn('provenance unavailable', self.header('hathach/pcb:a/x.sch'))
        still = subprocess.run(['git', '-C', str(part), 'cat-file', '-e', 'HEAD:a/x.sch'],
                               env=dict(os.environ, GIT_NO_LAZY_FETCH='1'))
        self.assertNotEqual(still.returncode, 0, 'the blob was fetched')

    @needs_kicad
    def test_a_child_sheet_edit_shows_in_the_header_though_the_root_is_clean(self):
        pcb_clone = self.repo('pcb2', kicad_files(), 'git@github.com:hathach/pcb.git')
        self.clones(self.clone, pcb_clone)
        self.write('pcb2/sheets/power.kicad_sch',
                   (KICAD / 'sheets/power.kicad_sch').read_text().replace('"10k"', '"22k"'))
        header = self.header('hathach/pcb:board.kicad_sch')
        self.assertIn('working tree clean; inputs sheets/power.kicad_sch modified [kicad]', header)


class FindIn(Repos):
    def test_names_that_sort_alike_come_out_in_one_order(self):
        top = self.repo('ties', {n: EAGLE.read_bytes() for n in ('board.sch', 'Board.sch', 'board1.sch', 'board01.sch')})
        outs = {subprocess.run([sys.executable, str(ROOT / 'skills/read-pcb/scripts/pcb.py'), 'find', '--in', str(top)],
                               capture_output=True, text=True, env=dict(os.environ, PYTHONHASHSEED=str(seed))).stdout
                for seed in range(6)}
        self.assertEqual(outs, {''.join(f'{top}/{n}  [eagle]\n'
                                        for n in ('board01.sch', 'board1.sch', 'Board.sch', 'board.sch'))})

    def test_a_git_directory_is_searched_beneath_the_path_only_and_needs_no_keyword(self):
        top = self.repo('top', {'board/a.sch': EAGLE.read_bytes(), 'other/b.sch': EAGLE.read_bytes()})
        self.write('top/board/new.sch', EAGLE.read_bytes())
        code, out, _ = run('find', '--in', top / 'board')
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f'{top}/board/a.sch  [eagle]',
                                            f'{top}/board/new.sch  [eagle] [untracked]'])

    def test_a_plain_directory_is_walked(self):
        self.write('plain/x/board.sch', EAGLE.read_bytes())
        self.write('plain/x/sub.kicad_sch', SUB_SHEET)
        out = run('find', '--in', self.tmp / 'plain', 'board')[1]
        self.assertEqual(out.splitlines(), [f'{self.tmp}/plain/x/board.sch  [eagle]'])

    def test_an_unreadable_directory_refuses_the_search_with_or_without_git(self):
        self.repo('git', {'visible.sch': EAGLE.read_bytes()})
        for top in ('plain', 'git'):
            self.write(f'{top}/visible.sch', EAGLE.read_bytes())
            self.write(f'{top}/locked/hidden.sch', EAGLE.read_bytes())
            (self.tmp / top / 'locked').chmod(0)
            self.addCleanup((self.tmp / top / 'locked').chmod, 0o755)
        if os.access(self.tmp / 'plain' / 'locked', os.R_OK):
            self.skipTest('running as a user that can read mode-000 directories')
        self.assertRefused(f'{self.tmp}/plain/locked unreadable', 'find', '--in', self.tmp / 'plain')
        self.assertRefused("could not open directory 'locked/'", 'find', '--in', self.tmp / 'git')

    def test_a_kicad_sheet_with_a_valueless_field_is_root_unknown_and_refused_on_query(self):
        for i, (text, tag) in enumerate(
                (('(kicad_sch (uuid) (sheet_instances (path "/" (page "1"))))', 'kicad, root unknown'),
                 ('(kicad_sch (uuid "u") (symbol (instances (project "p" (path)))))', 'kicad, root unknown'),
                 ('(kicad_sch (uuid "u") (sheet (property "Sheetfile" (x))) (sheet_instances (path "/")))', 'kicad'))):
            path = self.write(f'odd{i}/odd.kicad_sch', text)
            self.write(f'odd{i}/ok.sch', EAGLE.read_bytes())
            code, out, err = run('find', '--in', path.parent)
            self.assertEqual((code, out.splitlines()), (0, [f'{path.parent}/odd.kicad_sch  [{tag}]',
                                                            f'{path.parent}/ok.sch  [eagle]']), err)
            self.assertRefused('malformed KiCad schematic', 'nets', path)

    def test_find_without_keyword_or_path_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as e, redirect_stderr(io.StringIO()):
            pcb.main(['find'])
        self.assertEqual(e.exception.code, 2)


class Cache(Repos, Refusals):
    name = 'adafruit/MBAdafruitBoards:hub/Hub Rev A.sch'

    def setUp(self):
        super().setUp()
        self.mb = self.remote('adafruit/MBAdafruitBoards', {'hub/Hub Rev A.sch': EAGLE.read_bytes()})
        self.pcb = self.remote('hathach/pcb', kicad_files('k/'))

    def test_find_lists_eagle_untyped_and_only_kicad_roots(self):
        code, out, err = run('find', 'hub')
        self.assertEqual((code, out.splitlines()), (0, ['adafruit/MBAdafruitBoards:hub/Hub Rev A.sch  [?]']), err)
        self.assertEqual(run('find', 'kicad_sch')[1].splitlines(), ['hathach/pcb:k/board.kicad_sch  [kicad]'])

    def test_a_query_reads_the_fetched_commit_and_cites_it(self):
        sha = git(self.mb, 'rev-parse', 'HEAD')[:12]
        code, out, err = run('net', self.name, 'SIG')
        self.assertEqual(code, 0, err)
        self.assertRegex(out.splitlines()[0], rf'^source: {self.name} @ {sha} '
                                              r'\| cache .*, origin HEAD fetched .* \[eagle\]$')

    def test_each_command_fetches_so_a_new_commit_is_read(self):
        self.assertIn('10k', run('parts', self.name, 'R1')[1])
        self.push(self.mb, {'hub/Hub Rev A.sch': EAGLE.read_text().replace('value="10k"', 'value="47k"')})
        self.assertIn('47k', run('parts', self.name, 'R1')[1])

    def test_a_renamed_default_branch_is_followed(self):
        run('nets', self.name)
        self.push(self.mb, {'hub/Hub Rev A.sch': EAGLE.read_text().replace('value="10k"', 'value="68k"')},
                  branch='trunk')
        git(self.tmp / 'remotes' / 'adafruit/MBAdafruitBoards.git', 'symbolic-ref', 'HEAD', 'refs/heads/trunk')
        self.assertIn('68k', run('parts', self.name, 'R1')[1])

    def test_two_first_uses_at_once_share_one_cache(self):
        procs = [subprocess.Popen([sys.executable, str(ROOT / 'skills/read-pcb/scripts/pcb.py'), 'find', 'hub'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
        for p in procs:
            out, err = p.communicate()
            self.assertEqual(p.returncode, 0, err)
            self.assertIn('Hub Rev A.sch', out)
        self.assertEqual({p.name for p in (self.tmp / 'cache' / 'read-pcb' / 'adafruit').iterdir()},
                         {'MBAdafruitBoards', 'MBAdafruitBoards.lock'})

    def test_a_kicad_blob_that_cannot_be_read_makes_the_search_incomplete(self):
        real = pcb.Cache.read

        def read(cache, rel):
            if rel.endswith('.kicad_sch'):
                raise pcb.Refusal(3, 'remote went away')
            return real(cache, rel)

        with unittest.mock.patch.object(pcb.Cache, 'read', read):
            code, out, _ = run('find', 'kicad_sch')
        self.assertEqual(code, 3)
        self.assertIn('[unreadable: remote went away]', out)
        self.assertIn('search incomplete', out)

    def test_a_reachable_source_answers_while_the_other_fails(self):
        self.push(self.pcb, {'e/board.sch': EAGLE.read_bytes()})
        shutil.rmtree(self.tmp / 'remotes' / 'adafruit')
        code, out, _ = run('find', 'kicad_sch')
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], 'hathach/pcb:k/board.kicad_sch  [kicad]')
        self.assertIn('search incomplete: adafruit/MBAdafruitBoards unavailable', out)
        self.assertEqual(run('net', 'hathach/pcb:e/board.sch', 'SIG')[0], 0)
        self.assertRefused('cannot clone', 'nets', self.name)

    def test_kicad_inputs_are_laid_out_by_repo_path_and_bounded_to_the_repo(self):
        self.push(self.pcb, {'b/main.kicad_sch': ROOT_SHEET.format(child='sheets/a.kicad_sch'),
                             'b/sheets/a.kicad_sch': CHILD_SHEET.format(child='../../shared/c.kicad_sch'),
                             'shared/c.kicad_sch': SUB_SHEET, 'b/main.kicad_pro': '{}',
                             'x/main.kicad_sch': ROOT_SHEET.format(child='../../outside.kicad_sch')})
        cache = pcb.Cache('hathach/pcb')
        into = self.tmp / 'into'
        cache.materialize('b/main.kicad_sch', str(into))
        self.assertEqual(sorted(str(p.relative_to(into)) for p in into.rglob('*') if p.is_file()),
                         ['b/main.kicad_pro', 'b/main.kicad_sch', 'b/sheets/a.kicad_sch', 'shared/c.kicad_sch'])
        with self.refused('outside the repository'):
            cache.materialize('x/main.kicad_sch', str(self.tmp / 'into2'))

    def test_a_symlinked_or_missing_sheet_is_refused(self):
        os.symlink('real.kicad_sch', self.tmp / 'work/hathach/pcb/k/link.kicad_sch')
        self.push(self.pcb, {'k/real.kicad_sch': SUB_SHEET,
                             'k/l.kicad_sch': ROOT_SHEET.format(child='link.kicad_sch'),
                             'k/m.kicad_sch': ROOT_SHEET.format(child='gone.kicad_sch')})
        cache = pcb.Cache('hathach/pcb')
        for rel, why in (('k/l.kicad_sch', 'symlink'), ('k/m.kicad_sch', 'is not in')):
            with self.refused(why):
                cache.materialize(rel, str(self.tmp / rel))

    @needs_kicad
    def test_kicad_answers_from_the_cache(self):
        code, out, err = run('net', 'hathach/pcb:k/board.kicad_sch', 'VBUS')
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[1:], VBUS_NET)


if __name__ == '__main__':
    unittest.main()
