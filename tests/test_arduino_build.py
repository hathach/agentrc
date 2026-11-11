"""skills/arduino/scripts/arduino_build.py is the build mechanics an Arduino library's build contract binds its profile
to: selection under ci-arduino's filters, compiles against the checkout, dependency and header provenance, image span
checks and the HIL build record (schema 2). Moved from Adafruit_TinyUSB_Arduino's check_build.py (2989bca) with its
.claude/test/test_build.py, run here against a fake library (tests/fixtures/arduino_lib). arduino-cli is stubbed:
stubs prove the plumbing only; a real build is evidence only from a run of a library's contract."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the arduino engines are Linux only (fcntl)')

import contextlib
import io
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, git_env, load_arduino_engine, load_arduino_fixture, tmpdir  # noqa: E402

ab = load_arduino_engine('skills/arduino/scripts/arduino_build.py')
fake = load_arduino_fixture()
DEVICE = list(fake.HIL_SKETCHES['device'])


def elf32(segments, sections=None):
    """A minimal little-endian ELF32: PT_LOAD/other segments (type, paddr, filesz), each at file offset
    0x1000 * (index + 1), and allocated PROGBITS sections (segment index, offset in it, size); by default one
    section spanning each segment."""
    if sections is None:
        sections = [(i, 0, n) for i, (t, _, n) in enumerate(segments) if t == 1 and n]
    phoff, n = 52, len(segments)
    offsets = [0x1000 * (i + 1) for i in range(n)]
    shoff = 0x1000 * (n + 1)
    head = bytearray(52)
    head[:6] = b'\x7fELF\x01\x01'
    struct.pack_into('<II', head, 28, phoff, shoff)
    struct.pack_into('<HHHH', head, 42, 32, n, 40, len(sections))
    ph = b''.join(struct.pack('<IIIIIIII', t, off, paddr, paddr, filesz, filesz, 5, 4)
                  for (t, paddr, filesz), off in zip(segments, offsets))
    body = bytearray(bytes(head) + ph)
    body += bytes(shoff - len(body))
    for seg, delta, size in sections:
        body += struct.pack('<IIIIIIIIII', 0, 1, 0x2, 0, offsets[seg] + delta, size, 0, 0, 4, 0)
    return bytes(body)


def elf32_symbols(symbols):
    """A little-endian ELF32 with a .symtab (null entry first) of {name: value} and its .strtab."""
    strtab, offsets = bytearray(b'\0'), {}
    for name in symbols:
        offsets[name] = len(strtab)
        strtab += name.encode() + b'\0'
    symtab = bytes(16) + b''.join(struct.pack('<IIIBBH', offsets[n], v, 0, 0x12, 0, 1) for n, v in symbols.items())
    str_off, sym_off, shoff = 0x100, 0x200, 0x400
    data = bytearray(shoff)
    data[:6] = b'\x7fELF\x01\x01'
    struct.pack_into('<I', data, 32, shoff)
    struct.pack_into('<HH', data, 46, 40, 3)
    data[str_off:str_off + len(strtab)] = strtab
    data[sym_off:sym_off + len(symtab)] = symtab
    data += bytes(40)                                                                    # SHT_NULL
    data += struct.pack('<IIIIIIIIII', 0, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0)   # SHT_STRTAB
    data += struct.pack('<IIIIIIIIII', 0, 2, 0, 0, sym_off, len(symtab), 1, 1, 4, 16)  # SHT_SYMTAB, link 1
    return bytes(data)


def ihex(start, data):
    lines = [f':02000004{start >> 16:04X}{(-(2 + 4 + (start >> 24) + ((start >> 16) & 0xff))) & 0xff:02X}']
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        addr = (start + off) & 0xffff
        rec = bytes([len(chunk), addr >> 8, addr & 0xff, 0]) + chunk
        lines.append(':' + (rec + bytes([(-sum(rec)) & 0xff])).hex().upper())
    lines.append(':00000001FF')
    return '\n'.join(lines) + '\n'


def setUpModule():
    guard_survivors(__name__)




def cli_compile(used, platform_id, version, install_dir, build_install_dir, success=True, err=''):
    """arduino-cli compile --json's result as compile_sketch parses it."""
    out = {'success': success, 'compiler_err': err, 'builder_result': {
        'used_libraries': used, 'board_platform': {'id': platform_id, 'version': version, 'install_dir': install_dir},
        'build_platform': {'id': platform_id, 'install_dir': build_install_dir}, 'build_properties': []}}
    return mock.Mock(returncode=0 if success else 1, stdout=json.dumps(out), stderr='')


class Lib(unittest.TestCase):
    """A fresh copy of the fake library, its build profile bound for the test."""

    def setUp(self):
        self.tmp = tmpdir(self)
        self.root = fake.library(self.tmp / 'lib')
        self.agentrc = fake.agentrc(self.tmp / 'agentrc')
        self.P = fake.build_profile(self.root, self.agentrc)
        self.patch(ab, 'P', self.P)

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = ab.main(argv)
            except SystemExit as e:
                code = e.code
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def dep(self, name, depends='', files=None):
        """An installed library in a fake user libraries dir: its arduino-cli `lib list` entry."""
        d = self.tmp / 'libraries' / name.replace(' ', '_')
        d.mkdir(parents=True)
        (d / 'library.properties').write_text(f'name={name}\nversion=1.2.3\n' + (f'depends={depends}\n' if depends
                                                                                 else ''))
        for f, text in (files or {'src/x.h': name}).items():
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text(text)
        return {'library': {'name': name, 'version': '1.2.3', 'install_dir': str(d), 'location': 'user'}}


class Binding(Lib):
    def test_bind_refuses_a_second_profile(self):
        self.patch(ab, 'P', None)
        self.assertIs(ab.bind(self.P), ab)
        self.assertIs(ab.bind(self.P), ab)   # the same one again is no change
        other = fake.build_profile(self.root, self.agentrc, LIBRARY_NAME='Other Library')
        with self.assertRaisesRegex(RuntimeError, 'bound to another profile'):
            ab.bind(other)
        self.assertIs(ab.P, self.P)

    def test_run_binds_then_runs_main(self):
        self.patch(ab, 'P', None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ab.run(self.P, ['--select-only', '--scope', 'README.md']), 0)
        self.assertIs(ab.P, self.P)
        self.assertTrue(json.loads(out.getvalue())['pass'])


class Selection(Lib):
    def test_rules(self):
        cases = {
            'src/ports/fa/port.cpp': ('family', 'fa'),                   # the profile's own rules first
            'src/ports/zz/x.cpp': ('uncovered', None),
            'src/core_bundled.h': ('uncovered', None),
            'src/fake.h': ('shared', None),
            'examples/Basic/echo/echo.ino': ('example', 'examples/Basic/echo'),
            'examples/Basic/echo/helper.py': ('example', 'examples/Basic/echo'),
            'examples/README.md': ('nothing', None),
            'examples/Basic/data.bin': ('uncovered', None),
            '.claude/skills/build/scripts/check_build.py': ('tests', 'build'),
            'test/hil/fixture.cpp': ('fixture', 'test/hil/fixture.cpp'),
            'test/hil/sketches/probe/probe.ino': ('hil_sketch', 'test/hil/sketches/probe'),
            'test/hil/ci.json': ('hil', None),
            '.claude/skills/hil-contract/scripts/hil_test.py': ('hil', None),
            '.claude/test/test_hil.py': ('tests', 'hil'),
            '.claude/test/test_hil_remote.py': ('nothing', None),        # not one of this profile's CONTRACT_TESTS
            '.claude/test/conftest.py': ('nothing', None),
            'README.md': ('nothing', None),
            'docs/x.txt': ('nothing', None),
            'library.properties': ('shared', None),      # ahead of the profile's NOTHING, which lists it
            'weird/file.c': ('uncovered', None),
        }
        for path, (effect, detail) in cases.items():
            self.assertEqual(ab.classify(path)[:2], (effect, detail), path)

    def test_family_change_builds_that_familys_representatives(self):
        sel = ab.select(['src/ports/fa/port.cpp'], roster=['rig_ocd', 'rig_jlink', 'rig_host'])
        self.assertEqual(set(sel['builds']), {'rig_ocd', 'host_ci'})
        self.assertEqual(sel['hil'], {'boards': ['rig_host', 'rig_ocd'], 'full': False, 'uncovered': []})

    def test_shared_change_builds_every_representative(self):
        sel = ab.select(['src/fake.h'], only_examples=['examples/Basic/echo'])
        self.assertEqual(set(sel['builds']), {'rig_ocd', 'rig_jlink', 'dev_b'})   # host_ci skips echo

    def test_example_narrowing_filters_every_rule(self):
        only = ['examples/Basic/echo']
        self.assertEqual(ab.select(['examples/HID/inout/inout.ino'], only_examples=only)['builds'], {})
        # unnarrowed, a build-contract change smoke-builds each SMOKE_BOARD's first HIL sketch, examples/Basic/echo
        self.assertEqual(ab.select(['.claude/skills/build/scripts/check_build.py'],
                                   only_examples=['examples/HID/inout'])['builds'], {})
        for path in ('examples/HID/inout/inout.ino', 'test/hil/fixture.cpp', 'test/hil/sketches/probe/probe.ino',
                     '.claude/skills/build/scripts/check_build.py'):
            built = ab.select([path, 'examples/Basic/echo/echo.ino'], only_examples=only)['builds']
            self.assertEqual({e for exs in built.values() for e in exs}, set(only), path)

    def test_example_change_honours_ci_filters(self):
        sel = ab.select(['examples/Basic/echo/echo.ino'])
        self.assertEqual(set(sel['builds']), {'dev_a', 'dev_b', 'rig_jlink', 'rig_ocd', 'rig_esp'})   # .skip.txt
        sel = ab.select(['examples/Only/bonly/bonly.ino'])
        self.assertEqual(set(sel['builds']), {'dev_b', 'rig_jlink', 'rig_esp'})   # .dev_b.test.only, via filters
        sel = ab.select(['examples/Skip/askip/askip.ino'])
        self.assertEqual(set(sel['builds']), {'dev_b', 'host_ci', 'rig_jlink', 'rig_host', 'rig_esp'})

    def test_unbuildable_examples_are_those_no_board_may_build(self):
        self.assertEqual(ab.unbuildable_examples(), ['examples/Nowhere/nowhere'])
        self.assertEqual(ab.select(['src/fake.h'])['uncovered'], [])   # a source change does not repeat it

    def test_board_or_example_runs_are_narrowed_and_say_so(self):
        self.patch(ab, 'source_identity', lambda: ({'head': 'abc', 'dirty': False, 'status': ''}, ''))
        self.patch(ab, 'prepared_dependencies', lambda: {})
        self.patch(ab, 'build_all', lambda *a: [])
        warning = 'explicit --board/--example selection: automatic change selection was not verified'
        for argv in (['--board', 'dev_a'], ['--scope', 'src/fake.h', '--example', 'examples/Basic/echo']):
            out = self.run_main(argv)[1]
            self.assertEqual((out['narrowed'], out['warnings']), (True, [warning]), argv)
        out = self.run_main(['--scope', 'src/fake.h'])[1]
        self.assertEqual((out['narrowed'], out['warnings']), (False, []))
        self.assertFalse(self.run_main(['--select-only', '--scope', 'src/fake.h'])[1]['narrowed'])
        self.assertTrue(self.run_main(['--select-only', '--scope', 'src/fake.h', '--example',
                                       'examples/Basic/echo'])[1]['narrowed'])
        self.patch(ab, 'make_record', lambda *a: (None, 'not recorded'))
        roster = fake.roster(self.root / 'test/hil/ci.json')
        out = self.run_main(['--board', 'rig_jlink', '--shared', '--record', str(self.root / 'build/hil/r.json'),
                             '--hil-config', str(roster)])[1]
        self.assertEqual((out['narrowed'], out['warnings'], out['firstError']), (True, [], 'not recorded'))

    def test_example_eligible_on_no_board_is_uncovered(self):
        path = 'examples/Nowhere/nowhere/nowhere.ino'
        self.assertFalse(any(ab.eligible(b, 'examples/Nowhere/nowhere') for b in self.P.BOARDS))
        sel = ab.select([path])
        self.assertEqual(sel['builds'], {})
        self.assertEqual([u['path'] for u in sel['uncovered']], [path])

    def test_example_change_runs_the_roster_boards_whose_role_runs_it(self):
        roster = ['rig_ocd', 'rig_host', 'rig_esp']
        self.assertEqual(ab.select(['examples/Host/info/info.ino'], roster=roster)['hil']['boards'], ['rig_host'])
        self.assertEqual(ab.select(['examples/Basic/echo/echo.ino'], roster=roster)['hil']['boards'],
                         ['rig_esp', 'rig_ocd'])

    def test_hil_sketch_builds_only_on_rig_boards_whose_role_runs_it(self):
        probe = 'test/hil/sketches/probe'
        self.assertEqual([b for b in self.P.BOARDS if ab.eligible(b, probe)], ['rig_jlink', 'rig_ocd', 'rig_esp'])
        sel = ab.select([f'{probe}/probe.ino'], roster=['rig_ocd', 'rig_host'])
        self.assertEqual(sel['builds'], {b: [probe] for b in ('rig_esp', 'rig_jlink', 'rig_ocd')})
        self.assertEqual(sel['tests'], ['hil'])   # hil_remote is not a test of this profile
        self.assertEqual(sel['hil'], {'boards': ['rig_host', 'rig_ocd'], 'full': True, 'uncovered': [
            {'family': 'fb', 'reason': 'no roster board of this family on this host'}]})   # rig_jlink and rig_esp

    def test_a_hil_sketch_or_fixture_reaches_the_families_of_the_rig_boards_it_builds_on(self):
        for path in ('test/hil/sketches/probe/probe.ino', 'examples/HID/inout/inout.ino', 'test/hil/fixture.cpp'):
            with self.subTest(path=path):
                self.assertEqual([u['family'] for u in ab.uncovered_by_roster([path], ['rig_ocd'])], ['fb'])
                self.assertEqual(ab.uncovered_by_roster([path], ['rig_ocd', 'rig_jlink']), [])
        # a fixture reaches only its own boards' family, an example no rig board runs reaches none
        self.assertEqual(ab.uncovered_by_roster(['test/hil/fixture.cpp'], ['rig_esp']), [])
        self.assertEqual(ab.uncovered_by_roster(['examples/Only/bonly/bonly.ino'], ['rig_ocd']), [])

    def test_a_harness_or_roster_change_reaches_no_family(self):
        for path in ('test/hil/ci.json', '.claude/skills/hil-contract/scripts/hil_test.py',
                     '.claude/test/test_hil.py', '.claude/skills/build/scripts/check_build.py'):
            with self.subTest(path=path):
                self.assertEqual(ab.uncovered_by_roster([path], ['rig_ocd']), [])

    def test_fixture_change_builds_and_selects_its_boards(self):
        sel = ab.select(['test/hil/fixture.cpp'], roster=['rig_ocd', 'rig_esp'])
        self.assertEqual(sel['builds'], {'rig_esp': DEVICE})
        self.assertEqual(sel['hil']['boards'], ['rig_esp'])

    def test_build_contract_change_smoke_builds_each_image_kind(self):
        sel = ab.select(['.claude/skills/build/scripts/check_build.py'])
        self.assertEqual(sel['builds'], {b: ['examples/Basic/echo'] for b in ('rig_esp', 'rig_jlink', 'rig_ocd')})
        self.assertEqual(sel['tests'], ['build'])

    def test_uncovered_and_hil(self):
        sel = ab.select(['src/ports/zz/x.cpp', 'test/hil/ci.json'], roster=['rig_ocd'])
        self.assertEqual([u['path'] for u in sel['uncovered']], ['src/ports/zz/x.cpp'])
        self.assertEqual(sel['hil'], {'boards': ['rig_ocd'], 'full': True, 'uncovered': []})
        self.assertEqual(sel['tests'], ['hil'])

    def test_hil_change_runs_every_contract_test_but_the_builds(self):
        self.patch(self.P, 'CONTRACT_TESTS', {**self.P.CONTRACT_TESTS, 'hil_ci': '.claude/test/test_hil_ci.py'})
        for path in ('test/hil/ci.json', 'test/hil/sketches/probe/probe.ino'):
            self.assertEqual(ab.select([path], roster=['rig_ocd'])['tests'], ['hil', 'hil_ci'], path)

    def test_hil_selection_names_families_the_roster_lacks(self):
        sel = ab.select(['src/ports/fa/x.cpp', 'src/fake.h'], roster=['rig_ocd'])
        self.assertEqual(sel['hil']['boards'], ['rig_ocd'])
        self.assertEqual([u['family'] for u in sel['hil']['uncovered']], ['fb'])

    def test_scope_file_is_checkout_relative(self):
        def scope(s):
            return ab.changed_paths(ab.parse(['--select-only', '--scope', s], None))
        self.assertEqual(scope(str(self.root / 'src/fake.h')), ['src/fake.h'])
        self.assertEqual(scope('./src/fake.h'), ['src/fake.h'])
        self.assertEqual(scope('examples/Basic/echo'), ['examples/Basic/echo/.skip.txt',
                                                        'examples/Basic/echo/echo.ino'])
        code, out = self.run_main(['--select-only', '--scope', '/'])
        self.assertEqual(code, 2)
        self.assertIn('outside this checkout', out['error'])

    def test_base_paths_with_spaces_and_non_ascii_arrive_intact(self):
        env = git_env()
        environ = mock.patch.dict(os.environ, env, clear=True)   # the engine's own git runs inherit it
        environ.start()
        self.addCleanup(environ.stop)

        def git(*a):
            subprocess.run(['git', '-C', str(self.root), *a], check=True, capture_output=True, env=env)
        git('init', '-q', '-b', 'main')
        git('add', '-A')
        git('commit', '-qm', 'base')
        for name in ('examples/Basic/echo/my notes.txt', 'src/caf\u00e9.h'):
            (self.root / name).write_text('x')
        git('add', '-A')
        git('commit', '-qm', 'change')
        (self.root / 'src/new file.h').write_text('x')
        (self.root / 'src/\u00fcber.h').write_text('x')
        self.assertEqual(ab.changed_paths(ab.parse(['--select-only', '--base', 'HEAD~1'], None)),
                         ['examples/Basic/echo/my notes.txt', 'src/caf\u00e9.h'])
        self.assertEqual(ab.changed_paths(ab.parse(['--select-only', '--base', 'HEAD~1', '--worktree'], None)),
                         ['examples/Basic/echo/my notes.txt', 'src/caf\u00e9.h', 'src/new file.h', 'src/\u00fcber.h'])

    def test_scope_dir_refuses_symlink_out_of_checkout(self):
        (self.tmp / 'outside.txt').write_text('x')
        (self.root / 'docs').mkdir()
        (self.root / 'docs' / 'link.txt').symlink_to(self.tmp / 'outside.txt')
        code, out = self.run_main(['--select-only', '--scope', 'docs'])
        self.assertEqual(code, 2)
        self.assertIn('docs/link.txt', out['error'])
        self.assertIn('outside this checkout', out['error'])

    def test_select_only_exit_and_shape(self):
        code, out = self.run_main(['--select-only', '--scope', 'README.md'])
        self.assertEqual((code, out['version'], out['build']['builds']), (0, 2, {}))
        roster = fake.roster(self.root / 'test/hil/ci.json')
        code, out = self.run_main(['--select-only', '--scope', 'test/hil/sketches/probe', '--hil-config',
                                   str(roster)])
        self.assertEqual((code, out['hil']['full']), (0, True))
        self.assertNotIn('hil', out['build'])

    def test_roster_of_a_board_without_a_rig_profile_refused(self):
        roster = self.root / 'r.json'
        roster.write_text(json.dumps({'boards': [{'name': 'rig_ocd'}, {'name': 'dev_a'}]}))
        with self.assertRaisesRegex(ValueError, r"no rig profile here: \['dev_a'\]"):
            ab.rig_roster(roster)
        code, out = self.run_main(['--select-only', '--scope', 'README.md', '--hil-config', str(roster)])
        self.assertEqual(code, 2)
        self.assertIn('dev_a', out['error'])


class Images(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)

    def test_elf_spans_skip_empty_and_merge_adjacent(self):
        p = self.dir / 'a.elf'
        p.write_bytes(elf32([(1, 0x4000, 0x100), (1, 0x4100, 0x10), (1, 0x20000000, 0), (6, 0, 0x40)]))
        self.assertEqual(ab.elf_load_spans(p), [(0x4000, 0x4110)])
        p.write_bytes(elf32([(1, 0x0, 0x4020)], [(0, 0x4000, 0x20)]))
        self.assertEqual((ab.elf_load_spans(p), ab.elf_section_spans(p)), ([(0, 0x4020)], [(0x4000, 0x4020)]))
        p.write_bytes(b'\x7fELF\x02\x01' + bytes(64))
        with self.assertRaisesRegex(ValueError, 'not a little-endian ELF32'):
            ab.elf_load_spans(p)

    def test_elf_symbols_by_name(self):
        p = self.dir / 'a.elf'
        p.write_bytes(elf32_symbols({'main': 0x10000101, 'tud_task': 0x10000200, 'tusb_inited': 0x20000010}))
        self.assertEqual(ab.elf_symbols(p, ['main', 'tusb_inited', 'absent']),
                         {'main': 0x10000101, 'tusb_inited': 0x20000010})
        self.assertEqual(ab.elf_symbols(p, []), {})
        p.write_bytes(elf32([(1, 0x4000, 16)]))   # no .symtab
        self.assertEqual(ab.elf_symbols(p, ['main']), {})

    def test_hex_runs(self):
        p = self.dir / 'a.hex'
        p.write_text(ihex(0x4000, bytes(range(40))))
        self.assertEqual(ab.hex_runs(p), [(0x4000, bytes(range(40)))])
        p.write_text(ihex(0x4000, bytes(16)) .replace(':00000001FF\n', '') + ihex(0x8000, b'\x01').split('\n', 1)[1])
        self.assertEqual(ab.hex_runs(p), [(0x4000, bytes(16)), (0x8000, b'\x01')])
        p.write_text(':0100000000FE\n')
        with self.assertRaises(ValueError):
            ab.hex_runs(p)


class Provenance(Lib):
    def test_foreign_header_detected_and_compared(self):
        d = self.tmp
        lib = d / 'core/libraries/Fake_USB'
        (lib / 'src').mkdir(parents=True)
        (lib / 'library.properties').write_text(f'name={fake.NAME}\n')
        same = lib / 'src/fake.h'
        same.write_bytes((self.root / 'src/fake.h').read_bytes())
        other = lib / 'src/ports/fa/port.cpp'
        other.parent.mkdir(parents=True)
        other.write_text('// drifted\n')
        gone = lib / 'src/only_there.h'
        gone.write_text('')
        unrelated = d / 'core/cores/x.h'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('')
        notours = d / 'core/libraries/Other/src/fake.h'
        notours.parent.mkdir(parents=True)
        (notours.parents[1] / 'library.properties').write_text('name=Other\n')
        notours.write_text('')
        spaced = d / 'my core/libraries/Fake_USB'
        (spaced / 'src').mkdir(parents=True)
        (spaced / 'library.properties').write_text(f'name={fake.NAME}\n')
        (spaced / 'src/fake.h').write_text('// drifted\n')
        build = d / 'b'
        build.mkdir()
        (build / 'main.cpp.d').write_text(f'main.cpp.o: \\\n {same} \\\n {other} {unrelated} {notours} {gone} '
                                          f'{self.root / "src/fake.h"} ' +
                                          str(spaced / 'src/fake.h').replace(' ', '\\ ') + '\n')
        got = [(h['counterpart'], h['matches']) for h in ab.foreign_headers(ab.dep_paths(build))]
        self.assertEqual(sorted(got), [('src/fake.h', False), ('src/fake.h', True), ('src/only_there.h', False),
                                       ('src/ports/fa/port.cpp', False)])
        core = {Path(h['path']): h['core'] for h in ab.foreign_headers(ab.dep_paths(build), [None, str(d / 'core')])}
        self.assertEqual(core, {same: True, other: True, gone: True, spaced / 'src/fake.h': False})
        self.assertFalse(any(h['core'] for h in ab.foreign_headers(ab.dep_paths(build))))

    def test_core_tinyusb_names_the_esp_cores_bundled_copy_its_version_and_headers(self):
        d = self.tmp / 'esp32s3 libs/3.3.12'
        tusb = d / 'include/arduino_tinyusb'
        (tusb / 'tinyusb/src/device').mkdir(parents=True)
        (tusb / 'include').mkdir()
        (tusb / 'tinyusb/src/tusb_option.h').write_text('#define TUSB_VERSION_MAJOR     0\n'
                                                         '#define TUSB_VERSION_MINOR     21\n'
                                                         '#define TUSB_VERSION_REVISION  0\n')
        (tusb / 'tinyusb/src/device/usbd.h').write_text('')
        (tusb / 'include/tusb_config.h').write_text('')
        (d / 'versions.txt').write_text('esp-idf: v5.5.5 b774170ff46\ntinyusb: master 88c7f0b81\n')
        other = self.tmp / 'old/include/arduino_tinyusb'   # a second copy: no tusb_option.h, no versions.txt
        (other / 'include').mkdir(parents=True)
        (other / 'include/tusb_config.h').write_text('')
        build = self.tmp / 'b'
        (build / 'sketch').mkdir(parents=True)
        esc = lambda p: str(p).replace(' ', '\\ ')
        (build / 'sketch/a.cpp.d').write_text(f'a.cpp.o: \\\n {esc(tusb / "tinyusb/src/tusb_option.h")} \\\n '
                                              f'{esc(tusb / "include/tusb_config.h")} {self.root / "src/fake.h"}\n')
        (build / 'b.cpp.d').write_text(f'b.cpp.o: {esc(tusb / "tinyusb/src/device/usbd.h")} '
                                       f'{esc(tusb / "include/tusb_config.h")} {other / "include/tusb_config.h"}\n')
        self.assertEqual(ab.core_tinyusb(ab.dep_paths(build)), [
            {'root': str(tusb), 'version': '0.21.0', 'ref': 'master 88c7f0b81',
             'headers': sorted(str(tusb / h) for h in ('include/tusb_config.h', 'tinyusb/src/device/usbd.h',
                                                       'tinyusb/src/tusb_option.h'))},
            {'root': str(other), 'version': None, 'ref': None, 'headers': [str(other / 'include/tusb_config.h')]}])
        (tusb / 'tinyusb/src/tusb_option.h').write_text('#define TUSB_VERSION_MAJOR 0\n')   # incomplete
        self.assertIsNone(ab.core_tinyusb(ab.dep_paths(build))[0]['version'])
        self.assertEqual(ab.core_tinyusb(ab.dep_paths(self.tmp / 'nothing')), [])

    def fake_compile(self, used=None, success=True, err='', platform='/x'):
        used = [{'name': fake.NAME, 'install_dir': str(self.root)}] if used is None else used
        return cli_compile(used, 'vend:arch', '1.0', platform, '/y', success, err)

    def compile(self, proc, board='rig_jlink', prepared=None):
        with mock.patch.object(ab.subprocess, 'run', **({'side_effect': proc} if isinstance(proc, Exception) else
                                                          {'return_value': proc})):
            return ab.compile_sketch(board, 'examples/Basic/echo', self.root / 'b', [],
                                     {} if prepared is None else prepared)

    def test_library_from_another_checkout_fails(self):
        row = self.compile(self.fake_compile([{'name': fake.NAME, 'install_dir': '/home/x/Arduino/libraries/F'}]))
        self.assertEqual(row['status'], 'failed')
        self.assertIn('not this checkout', row['firstError'])
        row = self.compile(self.fake_compile([]))
        self.assertIn('came from nowhere', row['firstError'])

    def test_missing_arduino_cli_is_an_error(self):
        row = self.compile(FileNotFoundError('arduino-cli'))
        self.assertEqual((row['status'], row['remedy']), ('error', 'install arduino-cli on PATH'))

    def test_ok_build_row(self):
        with mock.patch.object(ab.subprocess, 'run', return_value=self.fake_compile()) as run:
            row = ab.compile_sketch('rig_jlink', 'examples/Basic/echo', self.root / 'b', [], {})
        self.assertEqual(run.call_count, 1)   # a library's build maps no prefix: it has no per-process dir
        self.assertEqual((row['status'], row['vidPid'], row['foreignHeaders']), ('ok', '1234:5678', []))
        self.assertEqual(row['coreTinyUSB'], [])
        self.assertEqual(row['libraries'], [{'name': fake.NAME, 'version': None, 'dir': str(self.root)}])
        self.assertIn('--library', (self.root / row['log']).read_text())

    def test_both_header_reports_come_from_one_read_of_the_dep_files(self):
        tusb = self.tmp / 'core/include/arduino_tinyusb'

        def run(*_, **__):
            (self.root / 'b/a.cpp.d').write_text(f'a.cpp.o: {tusb / "include/tusb_config.h"}\n')
            return self.fake_compile()
        with mock.patch.object(ab.subprocess, 'run', side_effect=run), \
                mock.patch.object(ab, 'dep_paths', wraps=ab.dep_paths) as deps:
            row = ab.compile_sketch('rig_jlink', 'examples/Basic/echo', self.root / 'b', [], {})
        self.assertEqual(deps.call_count, 1)
        self.assertEqual((row['foreignHeaders'], row['coreTinyUSB']), ([], [
            {'root': str(tusb), 'version': None, 'ref': None, 'headers': [str(tusb / 'include/tusb_config.h')]}]))

    def test_missing_core_is_an_error_with_remedy(self):
        row = self.compile(self.fake_compile(success=False, err='Error resolving FQBN: platform x'), board='dev_a')
        self.assertEqual((row['status'], row['remedy']), ('error', 'arduino-cli core install vend:arch'))

    def test_compile_error_names_its_first_error_line(self):
        row = self.compile(self.fake_compile(success=False, err='a.ino:1: note\na.ino:2: error: boom\n'))
        self.assertEqual((row['status'], row['firstError']), ('failed', 'a.ino:2: error: boom'))

    def test_unexplained_used_library_fails_the_row(self):
        dep = self.dep('Dep A')['library']['install_dir']
        own = {'name': fake.NAME, 'install_dir': str(self.root)}
        prepared = {'Dep A': {'dir': dep}}
        cases = (([own, {'name': 'Dep A', 'install_dir': dep}], 'ok'),                       # a prepared dependency
                 ([own, {'name': 'Wire', 'install_dir': '/x/libraries/Wire'}], 'ok'),         # bundled with the core
                 ([own, {'name': 'SPI', 'install_dir': '/y/libraries/SPI'}], 'ok'),           # with the build core
                 ([own, {'name': 'Dep A', 'install_dir': '/home/u/Arduino/libraries/A'}], 'failed'),   # another copy
                 ([own, {'name': 'Stray', 'install_dir': '/home/u/Arduino/libraries/Stray'}], 'failed'))
        for used, status in cases:
            with self.subTest(used=used[1]):
                row = self.compile(self.fake_compile(used), prepared=prepared)
                self.assertEqual(row['status'], status, row.get('firstError'))
                if status == 'failed':
                    self.assertIn(f'{used[1]["name"]} from {used[1]["install_dir"]}', row['firstError'])
                    self.assertIn('EXTRA_DEPENDENCIES', row['remedy'])


    def test_a_prepared_user_copy_shadows_only_a_core_bundling_it(self):
        core = self.tmp / 'core'
        (core / 'libraries/SPI').mkdir(parents=True)
        (core / 'libraries/SPI/library.properties').write_text('name=SPI\n')
        user = self.dep('SPI')['library']
        own = {'name': fake.NAME, 'install_dir': str(self.root)}
        prepared = {'SPI': {'dir': user['install_dir'], 'sha256': 'h', 'shadowOnly': True}}
        frozen = json.dumps(prepared)
        # in either order: a core without SPI refuses the copy, one bundling it admits it, nothing shared changes
        for platform, status in (('/x', 'failed'), (str(core), 'ok'), ('/x', 'failed')):
            with self.subTest(platform=platform):
                row = self.compile(self.fake_compile([own, user], platform=platform), prepared=prepared)
                self.assertEqual(row['status'], status, row.get('firstError'))
                self.assertEqual(json.dumps(prepared), frozen)
        self.assertEqual(row.get('shadows'), None)
        row = self.compile(self.fake_compile([own, user], platform=str(core)), prepared=prepared)
        self.assertEqual(row['shadows'], {'SPI': str(core / 'libraries/SPI')})
        other = {'name': 'SPI', 'install_dir': str(self.tmp / 'elsewhere/SPI'), 'location': 'user'}
        row = self.compile(self.fake_compile([own, other], platform=str(core)), prepared=prepared)
        self.assertIn('SPI from', row['firstError'])   # a second copy is not the one prepared
        row = self.compile(self.fake_compile([own, user], platform=str(core)), prepared={})
        self.assertEqual(row['status'], 'failed')   # only a prepared copy shadows the core's

    def test_used_dependencies_keep_declared_and_used_shadows(self):
        prepared = {'Dep A': {'dir': 'a'}, 'SPI': {'dir': 's', 'shadowOnly': True},
                    'Wire': {'dir': 'w', 'shadowOnly': True}}
        rows = [{'shadows': {'SPI': '/c2/SPI'}}, {'shadows': {'SPI': '/c1/SPI'}}, {}]
        self.assertEqual(ab.used_dependencies(prepared, rows), {'Dep A': {'dir': 'a'},
                                                                'SPI': {'dir': 's', 'shadows': '/c1/SPI'}})

class Dependencies(Lib):
    def installed(self, *libs):
        return mock.patch.object(ab, '_cli_json', return_value={'installed_libraries': list(libs)})

    def test_declared_takes_depends_without_versions_then_the_extras(self):
        self.assertEqual(ab.declared_dependencies(), ['Dep A', 'Dep B', 'Dep C'])
        self.assertEqual(ab.depends(self.root / 'library.properties'), ['Dep A', 'Dep B'])
        (self.root / 'library.properties').write_text('name=x\n')
        self.assertEqual(ab.declared_dependencies(), ['Dep C'])

    def test_prepared_fingerprints_shadowing_user_copies_before_any_compile(self):
        data = self.tmp / 'arduino15'
        for core, name in (('packages/v/hardware/a/1.0', 'SPI'), ('packages/v/hardware/b/2.0', 'Wire')):
            (data / core / 'libraries' / name).mkdir(parents=True)
            (data / core / 'libraries' / name / 'library.properties').write_text(f'name={name}\n')
        libs = [self.dep('Dep A'), self.dep('Dep B'), self.dep('Dep C'), self.dep('SPI'), self.dep('Stray')]

        def cli(*args):
            if args == ('config', 'dump'):
                return {'config': {'directories': {'data': str(data)}}}
            return {'installed_libraries': libs}
        with mock.patch.object(ab, '_cli_json', side_effect=cli):
            prepared = ab.prepared_dependencies()
        self.assertEqual(list(prepared), ['Dep A', 'Dep B', 'Dep C', 'SPI'])
        self.assertTrue(prepared['SPI']['shadowOnly'])
        self.assertEqual(prepared['SPI']['sha256'], ab.tree_digest(libs[3]['library']['install_dir']))

    def test_prepared_resolves_the_transitive_closure_once(self):
        libs = [self.dep('Dep A', 'Dep D (>=2), Dep B'), self.dep('Dep B'), self.dep('Dep C', fake.NAME),
                self.dep('Dep D', 'Dep A')]
        bundled = {'library': {'name': 'Dep E', 'install_dir': '/core/libraries/E', 'location': 'platform'}}
        with self.installed(bundled, *libs):
            prepared = ab.prepared_dependencies()
        self.assertEqual(list(prepared), ['Dep A', 'Dep B', 'Dep C', 'Dep D'])   # no cycle, never this library
        a = prepared['Dep A']
        self.assertEqual((a['version'], a['location'], a['dir']), ('1.2.3', 'user', libs[0]['library']['install_dir']))
        self.assertEqual(a['sha256'], ab.tree_digest(a['dir']))
        self.assertNotIn('git', a)   # not a git checkout

    def test_missing_dependency_exits_2_naming_the_install_line(self):
        for libs, missing in (([self.dep('Dep A')], ['Dep B', 'Dep C']),
                              ([self.dep('Dep B'), self.dep('Dep C', 'Dep Z'), self.dep('Dep Y', 'Dep A')], ['Dep A',
                                                                                                         'Dep Z'])):
            with self.subTest(missing=missing), self.installed(*libs), \
                    contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as e:
                ab.prepared_dependencies()
            got = json.loads(out.getvalue())
            self.assertEqual(e.exception.code, 2)
            self.assertIn(repr(missing), got['error'])
            self.assertEqual(got['remedy'], 'arduino-cli lib install ' + ' '.join(f'"{n}"' for n in missing))

    def test_main_checks_dependencies_before_any_compile(self):
        ident = {'head': 'abc', 'dirty': False, 'diffSha256': 'd', 'status': ''}
        self.patch(ab, 'source_identity', lambda: (ident, ''))
        self.patch(ab, 'build_all', mock.Mock(side_effect=AssertionError('compiled before the dependency check')))
        with self.installed(self.dep('Dep A')):
            code, out = self.run_main(['--board', 'dev_a', '--example', 'examples/Basic/echo'])
        self.assertEqual(code, 2)
        self.assertIn('arduino-cli lib install "Dep B" "Dep C"', out['remedy'])

    def test_tree_digest_binds_paths_and_bytes(self):
        d = Path(self.dep('Dep A')['library']['install_dir'])
        first = ab.tree_digest(d)
        (d / '.git').mkdir()
        (d / '.git' / 'HEAD').write_text('ref')
        self.assertEqual(ab.tree_digest(d), first)   # .git is not the library's files
        (d / 'src/x.h').write_text('changed')
        self.assertNotEqual(ab.tree_digest(d), first)
        (d / 'src/x.h').write_text('Dep A')
        (d / 'src/x.h').rename(d / 'src/y.h')
        self.assertNotEqual(ab.tree_digest(d), first)
        (d / 'link.h').symlink_to(d / 'src/y.h')
        linked = ab.tree_digest(d)
        (d / 'src/y.h').write_text('through the link')
        self.assertNotEqual(ab.tree_digest(d), linked)   # a symlinked file is its target's bytes

    def test_tree_digest_follows_symlinked_dirs_once_and_takes_dangling_links(self):
        d = Path(self.dep('Dep A')['library']['install_dir'])
        shared = self.tmp / 'shared'
        shared.mkdir()
        (shared / 'h.h').write_text('one')
        (d / 'linked').symlink_to(shared, target_is_directory=True)
        (shared / 'loop').symlink_to(shared, target_is_directory=True)   # walked once, not forever
        first = ab.tree_digest(d)
        (shared / 'h.h').write_text('two')
        self.assertNotEqual(ab.tree_digest(d), first)   # a header edited behind a symlinked dir
        (d / 'z').symlink_to(shared, target_is_directory=True)   # an alias of a dir walked already
        aliased = ab.tree_digest(d)
        other = self.tmp / 'other'
        other.mkdir()
        (other / 'h.h').write_text('two')
        (d / 'other').symlink_to(other, target_is_directory=True)
        both = ab.tree_digest(d)
        (d / 'z').unlink()
        (d / 'z').symlink_to(other, target_is_directory=True)
        self.assertNotEqual(ab.tree_digest(d), both)   # z retargeted between two walked dirs
        self.assertNotEqual(aliased, first)
        (d / 'gone.h').symlink_to(self.tmp / 'nowhere.h')
        dangling = ab.tree_digest(d)
        (self.tmp / 'nowhere.h').write_text('')
        self.assertNotEqual(ab.tree_digest(d), dangling)

    def test_drift_after_the_build_fails_the_run(self):
        lib = self.dep('Dep A')
        with self.installed(lib):
            prepared = {'Dep A': ab._lib_identity(lib['library'])}
        self.assertEqual(ab.dependency_drift(prepared), [])
        ident = {'head': 'abc', 'dirty': False, 'diffSha256': 'd', 'status': ''}

        def build_all(*a):
            (Path(prepared['Dep A']['dir']) / 'src/x.h').write_text('updated mid-build')
            return [{'board': 'dev_a', 'example': 'examples/Basic/echo', 'status': 'ok', 'buildDir': 'build/x'}]
        self.patch(ab, 'source_identity', lambda: (ident, ''))
        self.patch(ab, 'prepared_dependencies', lambda: prepared)
        self.patch(ab, 'build_all', build_all)
        code, out = self.run_main(['--board', 'dev_a', '--example', 'examples/Basic/echo'])
        self.assertEqual((code, out['pass']), (1, False))
        self.assertEqual(out['firstError'], "dependencies changed during the build: ['Dep A']")
        self.assertEqual(out['dependencies'], [prepared['Dep A']])


class Registry(Lib):
    """Rosters resolve through agentrc's board registry (skills/hil/boards.json): its metro_m4_express entry."""
    def setUp(self):
        super().setUp()
        self.probe, self.reg = next((k, v) for k, v in ab.hil_boards.registry().items()
                                    if v['name'] == 'metro_m4_express')
        for name in ('metro_m4_express', 'metro_m4'):
            self.P.BOARDS[name] = {'fqbn': 'adafruit:samd:x', 'family': 'fb', 'span': (0x4000, 0x80000)}
        self.path = self.root / 'r.json'

    def roster(self, **entry):
        self.path.write_text(json.dumps({'boards': [{'uid': 'DUT', 'probe': self.probe, **entry}]}))
        return self.path

    def test_a_registered_board_takes_the_registrys_flasher(self):
        board = ab.rig_roster(self.roster(name='metro_m4_express'))['metro_m4_express']
        self.assertEqual(board['flasher'], {**self.reg['flasher'], 'uid': self.probe})
        self.assertEqual(board['uid'], 'DUT')

    def test_a_registered_board_keeps_the_registrys_name(self):
        with self.assertRaisesRegex(ValueError, "lock 'metro_m4' is not its registered board's, metro_m4_express"):
            ab.rig_roster(self.roster(name='metro_m4'))


class RecordCase(Lib):
    def setUp(self):
        super().setUp()
        self.roster = fake.roster(self.root / 'test/hil/ci.json')
        self.patch(ab, 'tool_version', lambda: '1.5.1')
        self.patch(ab, 'platform_git', lambda d, content=False: None)

    def flasher(self, flasher):
        fake.roster(self.roster, rig_jlink={'flasher': flasher})

    def row(self, segments, hexdata=None, headers=(), sections=None):
        b = self.root / 'build/hil/rig_jlink/echo'
        b.mkdir(parents=True, exist_ok=True)
        (b / 'echo.ino.elf').write_bytes(elf32([(1, s, n) for s, n in segments], sections))
        if hexdata:
            (b / 'echo.ino.hex').write_text(ihex(*hexdata))
        return {'example': 'examples/Basic/echo', 'buildDir': 'build/hil/rig_jlink/echo', 'vidPid': '1234:5678',
                'foreignHeaders': list(headers), 'libraries': [{'name': fake.NAME}],
                'platform': {'id': 'vend:arch', 'version': '1.0', 'install_dir': '/x'}}

    def samd_row(self, **kw):
        """The SAMD core's layout: one segment from 0 (ELF headers, padding) holding .text at 0x4000."""
        return self.row([(0x0, 0x4020)], (0x4000, bytes(32)), sections=[(0, 0x4000, 0x20)], **kw)

    def record(self, row, experimental=False, board='rig_jlink', prepared=None):
        args = mock.Mock(experimental=experimental, build_property=[], record=str(self.root / 'build/r.json'))
        ident = {'head': 'abc', 'dirty': False, 'diffSha256': 'd', 'status': ''}
        with mock.patch.object(ab, 'source_identity', return_value=(ident, '')):
            return ab.make_record(args, board, [row], ident, '', self.roster, prepared or {})


class CoreContent(unittest.TestCase):
    """platform_git(content=True): a core checkout's bytes beyond its HEAD, by real git."""

    def setUp(self):
        self.tmp = tmpdir(self)
        self.env = git_env(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='protocol.file.allow', GIT_CONFIG_VALUE_0='always')
        environ = mock.patch.dict(os.environ, self.env, clear=True)
        environ.start()
        self.addCleanup(environ.stop)
        self.core = self.tmp / 'core'
        self.core.mkdir()
        for f, text in (('platform.txt', 'name=X\n'), ('cores/x/main.cpp', 'int main() {}\n'),
                        ('.gitignore', 'out/\n')):
            (self.core / f).parent.mkdir(parents=True, exist_ok=True)
            (self.core / f).write_text(text)
        self.git(self.core, 'init', '-q', '-b', 'main')
        self.git(self.core, 'add', '-A')
        self.git(self.core, 'commit', '-qm', 'core')

    def git(self, where, *a):
        subprocess.run(['git', '-C', str(where), *a], check=True, capture_output=True, env=self.env)

    def content(self):
        return ab.platform_git(str(self.core), content=True)

    def add_submodule(self):
        up = self.tmp / 'lib_upstream'
        up.mkdir()
        (up / 'lib.h').write_text('// lib\n')
        self.git(up, 'init', '-q', '-b', 'main')
        self.git(up, 'add', '-A')
        self.git(up, 'commit', '-qm', 'lib')
        self.git(self.core, 'submodule', '-q', 'add', str(up), 'libraries/Lib')
        self.git(self.core, 'commit', '-qm', 'sub')

    def test_a_clean_core_is_stable_and_without_content_the_entry_is_unchanged(self):
        first = self.content()
        self.assertEqual((first['dirty'], first['content']), (False, self.content()['content']))
        (self.core / 'out').mkdir()
        (self.core / 'out/x.o').write_text('ignored')   # ignored files are not bound
        self.assertEqual(self.content(), first)
        self.assertEqual(set(ab.platform_git(str(self.core))), {'commit', 'dirty'})

    def test_tracked_untracked_and_deleted_bytes_each_change_the_content(self):
        clean = self.content()['content']
        seen = {clean}
        main = self.core / 'cores/x/main.cpp'
        main.write_text('int main() { return 1; }\n')
        one = self.content()
        self.assertTrue(one['dirty'])
        main.write_text('int main() { return 2; }\n')   # same commit, same dirty flag, other bytes
        two = self.content()
        self.assertEqual((two['commit'], two['dirty']), (one['commit'], one['dirty']))
        seen |= {one['content'], two['content']}
        main.write_text('int main() {}\n')
        self.assertEqual(self.content()['content'], clean)
        extra = self.core / 'cores/x/extra.c'
        extra.write_text('a')
        untracked = self.content()
        self.assertFalse(untracked['dirty'])   # `dirty` keeps its meaning: tracked files only
        extra.write_text('b')
        seen |= {untracked['content'], self.content()['content']}
        extra.unlink()
        (self.core / 'platform.txt').unlink()
        seen.add(self.content()['content'])
        self.assertEqual(len(seen), 6)

    def test_a_submodules_own_changes_change_the_content(self):
        self.add_submodule()
        clean = self.content()['content']
        (self.core / 'libraries/Lib/lib.h').write_text('// changed\n')
        changed = self.content()['content']
        (self.core / 'libraries/Lib/lib.h').write_text('// lib\n')
        (self.core / 'libraries/Lib/new.h').write_text('')
        self.assertEqual(len({clean, changed, self.content()['content']}), 3)

    def test_an_unparsed_submodule_status_line_is_refused_not_skipped(self):
        status = mock.Mock(returncode=0, stdout=b' 3e5369cd libs/ok\nwarning: odd\n')
        with mock.patch.object(ab, '_git_at', return_value=status), \
                self.assertRaisesRegex(RuntimeError, "unparsed line 'warning: odd'"):
            ab.content_digest(self.core)

    def test_untracked_bytes_holding_separators_do_not_collide_with_two_files(self):
        (self.core / 'a').write_bytes(b'x\0b\0y')
        one = self.content()['content']
        (self.core / 'a').write_bytes(b'x')
        (self.core / 'b').write_bytes(b'y')
        self.assertNotEqual(one, self.content()['content'])

    def test_git_presentation_settings_do_not_enter_the_content(self):
        self.add_submodule()
        (self.core / 'cores/x/main.cpp').write_text('int main() { return 1; }\n')
        (self.core / 'cores/x/ü.c').write_text('')
        (self.core / 'platform.txt').write_text('name=Y\n')
        first = self.content()['content']
        order = self.tmp / 'order'
        order.write_text('platform.txt\n')
        for key, value in (('diff.noprefix', 'true'), ('diff.mnemonicPrefix', 'true'), ('core.quotePath', 'false'),
                           ('diff.context', '9'), ('diff.algorithm', 'histogram'), ('diff.orderFile', str(order))):
            self.git(self.core, 'config', key, value)
        self.git(self.core / 'libraries/Lib', 'tag', 'v1')   # submodule status names it: `(v1)`
        self.assertEqual(self.content()['content'], first)

    def test_non_git_cores_and_unreadable_inputs_are_explicit(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        self.assertIsNone(ab.platform_git(str(plain), content=True))
        nested = self.core / 'cores/vendored'
        nested.mkdir()
        self.git(nested, 'init', '-q')
        (nested / 'f').write_text('')
        got = self.content()
        self.assertIsNone(got['content'])
        self.assertIn('an untracked repository', got['contentError'])
        shutil.rmtree(nested)
        (self.core / 'cores/x/locked.c').write_text('')
        (self.core / 'cores/x/locked.c').chmod(0)
        self.addCleanup((self.core / 'cores/x/locked.c').chmod, 0o644)
        if os.access(self.core / 'cores/x/locked.c', os.R_OK):
            self.skipTest('running as root: an unreadable file is readable')
        got = self.content()
        self.assertIsNone(got['content'])
        self.assertIn('locked.c', got['contentError'])


class Record(RecordCase):
    def test_jlink_board_records_the_hex_and_schema_2_identity(self):
        dep = {'name': 'Dep A', 'version': '1', 'dir': '/l/A', 'location': 'user', 'sha256': 'f' * 64}
        rec, err = self.record(self.samd_row(), prepared={'Dep A': dep})
        self.assertIsNone(err)
        s = rec['sketches']['echo']
        self.assertEqual((s['image'], s['loadSpans'], s['vidPid']), ('hex', [[0x4000, 0x4020]], '1234:5678'))
        self.assertEqual(sorted(s['sha256']), ['build/hil/rig_jlink/echo/echo.ino.elf',
                                               'build/hil/rig_jlink/echo/echo.ino.hex'])
        self.assertEqual((rec['version'], rec['kind'], rec['library'], rec['role']),
                         (2, 'arduino-hil-build', fake.NAME, 'device'))
        self.assertEqual(rec['roster'], {'path': 'test/hil/ci.json', 'sha256': ab.sha256(self.roster)})
        self.assertEqual(rec['dependencies'], [dep])
        self.assertEqual(rec['contract'], {f: ab.sha256(self.root / f) for f in self.P.CONTRACT_FILES})
        self.assertEqual(rec['agentrc'], {'commit': 'a' * 40, 'files': {
            f: ab.sha256(self.agentrc['root'] / f) for f in self.agentrc['files']}})
        self.assertEqual(rec['agentrcSource'], {'source': 'installed', 'dirty': False})   # provenance, not compared

    def test_record_binds_its_cores_content(self):
        self.patch(ab, 'platform_git', lambda d, content=False: {'commit': 'c', 'dirty': True,
                                                                 **({'content': 'f' * 64} if content else {})})
        rec, err = self.record(self.samd_row())
        self.assertIsNone(err)
        self.assertEqual(rec['platform']['git'], {'commit': 'c', 'dirty': True, 'content': 'f' * 64})

    def test_record_takes_each_sketchs_fqbn(self):
        rec, err = self.record(self.samd_row())
        self.assertEqual(rec['sketches']['echo']['fqbn'], rec['fqbn'])   # a library's: the board's
        rec, err = self.record({**self.samd_row(), 'fqbn': 'vend:arch:rig_jlink:usb=b'})
        self.assertIsNone(err)
        self.assertEqual((rec['fqbn'], rec['sketches']['echo']['fqbn']), ('vend:arch:rig_jlink',
                                                                          'vend:arch:rig_jlink:usb=b'))

    def test_record_refused_when_agentrc_changed_after_it_was_loaded(self):
        (self.agentrc['root'] / 'skills/hil/scripts/hil_lock.py').write_text('# pulled while building\n')
        _, err = self.record(self.samd_row())
        self.assertEqual(err, "agentrc changed since this process loaded it: ['skills/hil/scripts/hil_lock.py']; "
                              "build again")

    def test_contract_identity_skips_absent_contract_files_and_always_binds_this_engine(self):
        (self.root / 'contract/pin.json').unlink()
        self.agentrc['files'] = ['skills/hil/scripts/hil_lock.py']
        ident = ab.contract_identity()
        self.assertEqual(list(ident['contract']), ['contract/check.py'])
        self.assertEqual(sorted(ident['agentrc']['files']), [ab.SELF, 'skills/hil/scripts/hil_lock.py'])

    def test_record_refuses_two_sketches_of_one_basename(self):
        row = self.samd_row()
        args = mock.Mock(experimental=False, build_property=[], record=str(self.root / 'build/r.json'))
        ident = {'head': 'abc', 'dirty': False, 'diffSha256': 'd', 'status': ''}
        with mock.patch.object(ab, 'source_identity', return_value=(ident, '')):
            _, err = ab.make_record(args, 'rig_jlink', [row, {**row, 'example': 'examples/Other/echo'}], ident, '',
                                    self.roster, {})
        self.assertEqual(err, 'examples/Other/echo: another sketch of this record has its basename')

    def test_elf_flash_over_the_bootloader_refused(self):
        self.flasher({'name': 'openocd', 'uid': 'E66'})
        _, err = self.record(self.samd_row())
        self.assertIn('the ELF writes', err)
        self.assertIn('outside the board span', err)

    def test_any_segment_out_of_span_refused(self):
        self.flasher({'name': 'openocd', 'uid': 'E66'})
        _, err = self.record(self.row([(0x4000, 0x80000), (0x5000, 0x20)]))
        self.assertIn('outside the board span', err)

    def test_hex_disagreeing_with_elf_sections_refused(self):
        _, err = self.record(self.row([(0x4000, 32)], (0x4000, bytes(16))))
        self.assertIn('differ from the ELF sections', err)

    def test_flasher_without_an_image_kind_refused(self):
        self.flasher({'name': 'stlink', 'uid': 'S'})
        _, err = self.record(self.samd_row())
        self.assertIn("flasher 'stlink' has no image kind", err)


    def test_jlink_board_without_hex_refused(self):
        _, err = self.record(self.row([(0x4000, 32)]))
        self.assertIn('flashes the HEX', err)

    def test_unresolved_vid_pid_refused(self):
        self.P.vid_pid = mock.Mock(return_value=None)
        _, err = self.record(self.samd_row())
        self.assertIn('VID:PID could not be resolved', err)
        self.assertEqual(self.P.vid_pid.call_args[0][1:], ('rig_jlink', ab.rig_roster(self.roster)['rig_jlink']))

    def test_profile_record_check_adds_fields_or_refuses(self):
        seen = []

        def check(board, row, build_dir, roster_entry):
            seen.append((board, row['loadSpans'], build_dir.name, roster_entry))
            return ({'symbols': {'main': 1}}, '') if len(seen) == 1 else (None, 'too big')
        self.P.record_check = check
        rec, err = self.record(self.samd_row())
        self.assertEqual((err, rec['sketches']['echo']['symbols']), (None, {'main': 1}))
        _, err = self.record(self.samd_row())
        self.assertEqual(err, 'examples/Basic/echo: too big')
        self.assertEqual(seen[0], ('rig_jlink', [(0x4000, 0x4020)], 'echo', ab.rig_roster(self.roster)['rig_jlink']))

    def test_drifted_header_of_the_build_core_is_recorded(self):
        hdr = [{'path': '/x/libraries/Lib/src/a.h', 'counterpart': 'src/a.h', 'matches': False, 'core': True},
               {'path': '/x/libraries/Lib/src/b.h', 'counterpart': 'src/b.h', 'matches': True, 'core': True}]
        rec, err = self.record(self.samd_row(headers=hdr))
        self.assertIsNone(err)
        self.assertEqual(rec['sketches']['echo']['foreignHeaders'], hdr)

    def test_drifted_header_outside_the_core_refused_unless_experimental(self):
        hdr = [{'path': '/core/x.h', 'matches': False, 'core': False},
               {'path': '/x/y.h', 'matches': False, 'core': True}]
        _, err = self.record(self.samd_row(headers=hdr))
        self.assertIn("outside the core that differ from this checkout: ['/core/x.h']", err)
        rec, err = self.record(self.samd_row(headers=hdr), experimental=True)
        self.assertIsNone(err)
        self.assertTrue(rec['experimental'])
        self.assertEqual(rec['patch'], {'path': 'build/r.patch', 'sha256': ab.sha256(self.root / 'build/r.patch')})

    def test_experimental_record_into_a_new_dir(self):
        args = mock.Mock(experimental=True, build_property=[], record=str(self.root / 'build/new/x.json'))
        ident = {'head': 'abc', 'dirty': True, 'diffSha256': 'd', 'status': ' M a'}
        with mock.patch.object(ab, 'source_identity', return_value=(ident, 'diff')):
            rec, err = ab.make_record(args, 'rig_jlink', [self.samd_row()], ident, 'diff', self.roster, {})
        self.assertIsNone(err)
        self.assertEqual((self.root / 'build/new/x.patch').read_text(), 'diff')

    def test_source_change_during_build_refused(self):
        args = mock.Mock(experimental=False, build_property=[], record=str(self.root / 'build/r.json'))
        before = {'head': 'abc', 'dirty': False, 'diffSha256': 'd', 'status': ''}
        after = dict(before, status=' M src/fake.h\n', diffSha256='e')
        with mock.patch.object(ab, 'source_identity', return_value=(after, '')):
            _, err = ab.make_record(args, 'rig_jlink', [self.samd_row()], before, '', self.roster, {})
        self.assertIn('changed while building', err)

    def test_record_needs_a_clean_tree_and_no_untracked_source(self):
        argv = ['--board', 'rig_jlink', '--shared', '--hil-config', str(self.roster), '--record',
                str(self.root / 'build/hil/rig_jlink.build.json')]
        for status, extra, why in ((' M src/fake.h\n', [], 'clean tree'),
                                   ('?? src/new.c\n', ['--experimental'], "untracked source would be missing from "
                                                                          "the record patch: ['src/new.c']")):
            ident = {'head': 'abc', 'dirty': True, 'diffSha256': 'd', 'status': status}
            with self.subTest(why=why), mock.patch.object(ab, 'source_identity', return_value=(ident, '')):
                code, out = self.run_main(argv + extra)
            self.assertEqual(code, 2)
            self.assertIn(why, out['error'])

    def test_record_board_must_be_in_the_roster(self):
        fake.roster(self.roster)
        data = json.loads(self.roster.read_text())
        data['boards'] = [b for b in data['boards'] if b['name'] != 'rig_jlink']
        self.roster.write_text(json.dumps(data))
        code, out = self.run_main(['--board', 'rig_jlink', '--shared', '--hil-config', str(self.roster), '--record',
                                   str(self.root / 'build/hil/rig_jlink.build.json')])
        self.assertEqual(code, 2)
        self.assertIn('rig_jlink is not in', out['error'])


class BaseRecord(RecordCase):
    """A rig board's base image (BOARDS base): its runs outside base_exclude, bound by sha256 and segment."""

    def base(self, *files, exclude=()):
        for name, (start, data) in files:
            (self.root / name).write_text(ihex(start, data))
        self.P.BOARDS['rig_jlink'].update(base=tuple(n for n, _ in files), base_exclude=exclude)

    def test_the_record_binds_the_base_files_and_their_runs_outside_the_excluded_ranges(self):
        self.base(('sd.hex', (0x1000, bytes(32))), ('settings.hex', (0xff000, b'\x01\0\0\0')),
                  ('uicr.hex', (0x10001014, bytes(8))), exclude=((0x10001000, 0x10002000),))
        rec, err = self.record(self.samd_row())
        self.assertIsNone(err)
        self.assertEqual(rec['base'], {'files': {f: ab.sha256(self.root / f) for f in ('sd.hex', 'settings.hex',
                                                                                         'uicr.hex')},
                                       'segments': [[0x1000, 0x1020], [0xff000, 0xff004]]})   # no UICR run
        del self.P.BOARDS['rig_jlink']['base']
        self.assertNotIn('base', self.record(self.samd_row())[0])   # a board without one records none

    def test_a_base_the_board_cannot_hold_is_refused(self):
        cases = ((((0x3ff0, bytes(32)),), (), 'overlaps the board span'),
                 (((0x1000, bytes(32)), (0x1010, bytes(32))), (), 'base runs at 0x1000 and 0x1010 overlap'),
                 (((0x1000, bytes(32)),), ((0x1010, 0x2000),), 'partly inside an excluded range'))
        for runs, exclude, why in cases:
            with self.subTest(why=why):
                self.base(*((f'b{i}.hex', r) for i, r in enumerate(runs)), exclude=exclude)
                _, err = self.record(self.samd_row())
                self.assertIn(why, err)

    def test_a_base_with_nothing_to_verify_is_refused(self):
        self.base(('uicr.hex', (0x10001014, bytes(8))), exclude=((0x10001000, 0x10002000),))   # all excluded
        _, err = self.record(self.samd_row())
        self.assertIn('uicr.hex: no runs outside the excluded ranges', err)
        (self.root / 'eof.hex').write_text(':00000001FF\n')
        self.P.BOARDS['rig_jlink'].update(base=('eof.hex',), base_exclude=())
        _, err = self.record(self.samd_row())
        self.assertIn('eof.hex: no runs outside the excluded ranges', err)

    def test_a_base_is_written_through_jlink_only(self):
        self.base(('sd.hex', (0x1000, bytes(32))))
        self.flasher({'name': 'openocd', 'uid': 'E66'})
        _, err = self.record(self.row([(0x4000, 0x20)]))
        self.assertIn('a base image is checked and written through J-Link only', err)

    def test_write_hex_is_hex_runs_inverse(self):
        runs = [(0x1000, bytes(range(40))), (0x1fff8, bytes(16)), (0x10001014, bytes(8))]   # across 64 KiB records
        ab.write_hex(runs, self.root / 'w.hex')
        self.assertEqual(ab.hex_runs(self.root / 'w.hex'), runs)


class EspRecord(RecordCase):
    """An esptool board's record: the core's flash_args segments."""

    ARGS = '--flash-mode dio --flash-freq 80m --flash-size 4MB\n0x0 b.bin\n0x8000 p.bin\n0xe000 boot_app0.bin\n' \
           '0x10000 a.bin\n'
    SIZES = {'b.bin': 0x5000, 'p.bin': 0xc00, 'boot_app0.bin': 0x2000, 'a.bin': 0x58000}

    def esp_row(self, flash_args, sizes):
        b = self.root / 'build/hil/rig_esp/echo'
        b.mkdir(parents=True, exist_ok=True)
        (b / 'echo.ino.elf').write_bytes(elf32([(1, 0x42000000, 16)]))
        for name, n in sizes.items():
            (b / name).write_bytes(bytes(n))
        (b / 'flash_args').write_text(flash_args)
        return {'example': 'examples/Basic/echo', 'buildDir': 'build/hil/rig_esp/echo', 'vidPid': '303a:1001',
                'foreignHeaders': [], 'platform': {'id': 'esp:esp32', 'version': '3.3.7', 'install_dir': '/x'}}

    def esp(self, flash_args, sizes):
        return self.record(self.esp_row(flash_args, sizes), board='rig_esp')

    def test_segments_hashes_chip_and_fixture_recorded(self):
        rec, err = self.esp(self.ARGS, self.SIZES)
        self.assertIsNone(err)
        s = rec['sketches']['echo']
        self.assertEqual(s['image'], 'esptool')
        self.assertEqual(s['segments'], [[0, 'b.bin'], [0x8000, 'p.bin'], [0xe000, 'boot_app0.bin'],
                                         [0x10000, 'a.bin']])
        self.assertEqual(len(s['sha256']), 6)   # ELF, flash_args, four images
        self.assertEqual((rec['chip'], rec['role'], rec['fixture']),
                         ('esp32s3', 'device', {'path': 'test/hil/fixture.cpp',
                                                'sha256': ab.sha256(self.root / 'test/hil/fixture.cpp')}))

    def test_overlapping_segments_refused(self):
        _, err = self.esp(self.ARGS, dict(self.SIZES, **{'b.bin': 0x9000}))
        self.assertIn('overlaps', err)

    def test_missing_image_refused(self):
        _, err = self.esp(self.ARGS + '0x200000 gone.bin\n', self.SIZES)
        self.assertIn('gone.bin', err)

    def test_image_past_the_flash_refused(self):
        _, err = self.esp(self.ARGS, dict(self.SIZES, **{'a.bin': 0x400000}))
        self.assertIn('outside the board span', err)

    def test_image_on_the_options_line_refused(self):
        _, err = self.esp(self.ARGS.replace('4MB\n0x0 b.bin', '4MB 0x0 b.bin'), self.SIZES)
        self.assertIn('first line', err)
        _, err = self.esp('--flash-mode dio gone.bin\n0x0 b.bin\n', self.SIZES)
        self.assertIn('first line', err)

    def test_malformed_line_refused(self):
        _, err = self.esp(self.ARGS + '0x300000\n', self.SIZES)
        self.assertIn('want "<offset> <file>"', err)

    def test_host_board_needs_no_vid_pid(self):
        self.P.vid_pid = mock.Mock(side_effect=AssertionError('a host board has no DUT to identify'))
        b = self.root / 'build/hil/rig_host/info'
        b.mkdir(parents=True)
        (b / 'info.ino.elf').write_bytes(elf32([(1, 0x10000000, 64)]))
        row = {'example': 'examples/Host/info', 'buildDir': 'build/hil/rig_host/info', 'vidPid': None,
               'foreignHeaders': [], 'platform': {'id': 'vend:arch', 'version': '1', 'install_dir': '/x'}}
        rec, err = self.record(row, board='rig_host')
        self.assertIsNone(err)
        self.assertEqual((rec['role'], rec['sketches']['info']['vidPid']), ('host', None))


class Fixture(Lib):
    def test_fixture_board_compiles_a_copy_with_the_fixture(self):
        build = self.root / 'build/hil/rig_esp/echo'
        src = ab.sketch_source('rig_esp', 'examples/Basic/echo', build)
        self.assertEqual(src, self.root / 'build/hil/rig_esp/.sketch/echo/echo')
        self.assertEqual(sorted(p.name for p in src.iterdir()), ['.skip.txt', 'echo.ino', 'fixture.cpp'])
        self.assertEqual(sorted(p.name for p in (self.root / 'examples/Basic/echo').iterdir()),
                         ['.skip.txt', 'echo.ino'])   # the example untouched
        self.assertEqual(ab.sketch_source('rig_ocd', 'examples/Basic/echo', build), self.root / 'examples/Basic/echo')

    def test_esp_build_snapshots_boot_app0_or_fails(self):
        core = self.tmp / 'core'
        (core / 'tools/partitions').mkdir(parents=True)
        out = {'success': True, 'builder_result': {
            'used_libraries': [{'name': fake.NAME, 'install_dir': str(self.root)}],
            'board_platform': {'id': 'esp:esp32', 'install_dir': str(core)}}}
        proc = mock.Mock(returncode=0, stdout=json.dumps(out), stderr='')
        build = self.root / 'build/x'
        with mock.patch.object(ab.subprocess, 'run', return_value=proc), \
                mock.patch.object(ab, 'foreign_headers', return_value=[]):
            row = ab.compile_sketch('rig_esp', 'examples/Basic/echo', build, [], {})
            self.assertEqual(row['status'], 'failed')
            self.assertIn('boot_app0.bin missing', row['firstError'])
            (core / 'tools/partitions/boot_app0.bin').write_bytes(b'\x01' * 8)
            row = ab.compile_sketch('rig_esp', 'examples/Basic/echo', build, [], {})
        self.assertEqual(row['status'], 'ok')
        self.assertEqual((build / 'boot_app0.bin').read_bytes(), b'\x01' * 8)


class Usage(Lib):
    def test_refusals(self):
        for argv in (['--worktree', '--scope', 'x'], ['--board', 'nope'], ['--board', 'rig_ocd', '--record', 'x.json'],
                     ['--board', 'rig_ocd', '--experimental'], ['--select-only', '--board', 'rig_ocd'],
                     ['--board', 'rig_ocd', '--example', 'examples/nope'], []):
            code, out = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertFalse(out['pass'])

    def test_record_options(self):
        base = ['--board', 'rig_ocd', '--shared', '--hil-config', 'test/hil/ci.json', '--record']
        for argv, why in ((base + ['build/hil/x.json', '--build-property', 'a=b'], '--experimental'),
                          (base + [str(self.tmp / 'x.json')], 'inside this checkout'),
                          (base + [str(self.root / 'build/x.json'), '--example', 'examples/Basic/echo'],
                           'drop --example')):
            code, out = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(why, out['error'])


    def test_sketches_sharing_a_basename_on_one_board_are_refused_before_compiling(self):
        shutil.copytree(self.root / 'examples/Basic/echo', self.root / 'examples/Other/echo')
        self.patch(ab, 'source_identity', lambda: ({'head': 'abc', 'dirty': False, 'status': ''}, ''))
        self.patch(ab, 'prepared_dependencies', lambda: {})
        self.patch(ab.subprocess, 'run', mock.Mock(side_effect=AssertionError('compiled')))
        code, out = self.run_main(['--board', 'dev_a', '--example', 'examples/Basic/echo',
                                   '--example', 'examples/Other/echo'])
        self.assertEqual(code, 2)
        self.assertIn('dev_a: examples/Basic/echo and examples/Other/echo', out['error'])
        self.assertEqual(ab.basename_clashes({'dev_a': ['examples/Basic/echo'], 'dev_b': ['examples/Other/echo']}),
                         [])   # one basename per board's build dir

    def test_a_sketch_named_twice_is_built_once(self):
        plans = []
        self.patch(ab, 'source_identity', lambda: ({'head': 'abc', 'dirty': False, 'status': ''}, ''))
        self.patch(ab, 'prepared_dependencies', lambda: {})
        self.patch(ab, 'build_all', lambda jobs, plan, *a: plans.append(plan) or [])
        self.run_main(['--board', 'dev_a', '--example', 'examples/HID/inout', '--example', 'examples/Basic/echo',
                       '--example', 'examples/HID/inout/', '--example', 'examples/Basic/echo'])
        self.assertEqual(plans, [{'dev_a': ['examples/HID/inout', 'examples/Basic/echo']}])


class Core(Lib):
    """Core mode: the checkout is an Arduino platform (PLATFORM), its bundled libraries' sketches the examples."""

    def setUp(self):
        super().setUp()
        self.root = fake.core(self.tmp / 'core')
        self.P = fake.core_profile(self.root, self.agentrc)
        self.patch(ab, 'P', self.P)

    def fake_compile(self, platform=None, used=()):
        platform = str(self.root) if platform is None else platform
        return cli_compile(list(used), 'fake:core', '1.0.0', platform, platform)

    def core_roster(self):
        roster = self.root / 'test/hil/ci.json'
        roster.write_text(json.dumps({'boards': [{'name': 'rig_m4', 'uid': 'U', 'flasher': {
            'name': 'jlink', 'uid': '1', 'args': '-device X'}, 'arduino': {'tests': ['cdc']}}]}))
        return roster

    def compile(self, proc, example='libraries/Wire/examples/scan', board='m4', prepared=None):
        with mock.patch.object(ab.subprocess, 'run', return_value=proc) as run:
            row = ab.compile_sketch(board, example, self.root / 'b', [], prepared or {}, self.tmp / 'cli.yaml')
        return row, run.call_args[0][0]

    def test_sketches_are_every_ino_dir_under_the_sketch_roots(self):
        self.assertEqual(ab.examples(), sorted(s for s in fake.CORE_SKETCHES if s.startswith('libraries/')))

    def test_eligibility_takes_the_build_all_markers_and_exclude(self):
        got = {b: [e for e in ab.examples() if ab.eligible(b, e)] for b in ('m0', 'm0_tusb', 'm4')}
        self.assertNotIn('libraries/Wire/examples/skipped', sum(got.values(), []))     # .all.test.skip
        self.assertEqual([b for b in got if 'libraries/Wire/examples/only' in got[b]], ['m4'])   # .m4.test.only
        self.assertEqual([b for b in got if 'libraries/USB/examples/echo' in got[b]], ['m0_tusb'])   # exclude()
        self.assertTrue(ab.eligible('rig_m4', 'test/hil/core_cdc'))   # a HIL sketch, its rig board only
        self.assertFalse(ab.eligible('m4', 'test/hil/core_cdc'))

    def test_selection_rules(self):
        cases = {
            'libraries/USB/examples/scan/scan.ino': ('example', 'libraries/USB/examples/scan'),   # before the profile
            'libraries/USB/src/usb.cpp': ('sketches', [s for s in fake.CORE_SKETCHES if 'libraries/USB/' in s]),
            'platform.txt': ('boards', ('m0', 'm0_tusb', 'm4')),
            'cores/fake/main.cpp': ('shared', None),
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(ab.classify(path)[:2], want)
        sel = ab.select(['platform.txt'], roster=['rig_m4'])
        self.assertEqual(sorted(sel['builds']), ['m0', 'm0_tusb', 'm4'])
        self.assertIn('libraries/SDU/extras/Boot', sel['builds']['m4'])
        self.assertEqual(sel['hil']['boards'], ['rig_m4'])   # a board of a family it reaches
        sel = ab.select(['libraries/USB/src/usb.cpp'])
        self.assertEqual(sel['builds'], {'m0': ['libraries/USB/examples/scan'],
                                         'm0_tusb': ['libraries/USB/examples/echo', 'libraries/USB/examples/scan'],
                                         'm4': ['libraries/USB/examples/scan']})
        self.assertEqual(sel['uncovered'], [])
        self.assertEqual(ab.uncovered_by_roster(['platform.txt'], ['rig_m4']),
                         [{'family': 'c21', 'reason': 'no roster board of this family on this host'}])

    def test_a_hil_sketch_change_names_the_family_of_a_rig_board_the_roster_lacks(self):
        self.P.BOARDS['rig_m0'] = {'fqbn': 'fake:core:m0', 'family': 'c21', 'filters': 'm0', 'span': (0x2000, 0x40000)}
        sel = ab.select(['test/hil/core_cdc/core_cdc.ino'], roster=['rig_m4'])
        self.assertEqual(sorted(sel['builds']), ['rig_m0', 'rig_m4'])
        self.assertEqual(sel['hil'], {'boards': ['rig_m4'], 'full': True, 'uncovered': [
            {'family': 'c21', 'reason': 'no roster board of this family on this host'}]})

    def test_a_sketch_no_representative_builds_is_uncovered(self):
        self.P.classify_source = lambda path: ('sketches', ['libraries/Wire/examples/skipped'], 'w')
        self.assertEqual(ab.select(['libraries/Wire/Wire.cpp'])['uncovered'],
                         [{'path': 'libraries/Wire/Wire.cpp',
                           'reason': 'libraries/Wire/examples/skipped is eligible on no representative'}])

    def test_examples_build_under_their_path_and_hil_sketches_under_their_name(self):
        self.assertEqual(ab.build_key('libraries/Wire/examples/scan'), 'libraries--Wire--examples--scan')
        self.assertEqual(ab.build_key('test/hil/core_cdc'), 'core_cdc')
        self.assertEqual(ab.basename_clashes({'m4': ['libraries/Wire/examples/scan', 'libraries/USB/examples/scan']}),
                         [])

    def test_same_named_examples_on_a_fixture_board_compile_their_own_copies(self):
        self.P.BOARDS['m4']['fixture'] = 'cores/fake/main.cpp'
        examples = ('libraries/Wire/examples/scan', 'libraries/USB/examples/scan')
        for e in examples:
            (self.root / e / 'scan.ino').write_text(f'// {e}\n')
        srcs =[ab.sketch_source('m4', e, self.root / 'b/m4' / ab.build_key(e)) for e in examples]
        self.assertNotEqual(*srcs)
        for e, src in zip(examples, srcs):
            self.assertEqual(src.name, 'scan')   # arduino-cli needs the dir named as its .ino
            self.assertEqual((src / 'scan.ino').read_text(), f'// {e}\n')
            self.assertTrue((src / 'main.cpp').is_file())

    def test_compile_uses_the_isolated_config_and_the_sketchs_fqbn(self):
        row, cmd = self.compile(self.fake_compile(), example='test/hil/core_cdc_tusb', board='rig_m4')
        self.assertEqual(row['status'], 'ok', row.get('firstError'))
        self.assertNotIn('--library', cmd)
        self.assertEqual(cmd[cmd.index('--config-file') + 1], str(self.tmp / 'cli.yaml'))
        self.assertEqual((cmd[cmd.index('--fqbn') + 1], row['fqbn']), ('fake:core:m4:usb=tusb',) * 2)
        self.assertEqual(row['platform']['install_dir'], str(self.root))

    def test_compile_maps_the_isolated_dir_out_of_file_keeping_every_extra_flag(self):
        listed = {'success': True, 'builder_result': {'build_properties': [
            'compiler.c.extra_flags=-DPLATFORM', 'compiler.cpp.extra_flags=-DUSER {build.x}', 'build.extra_flags=-DB']}}
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=json.dumps(listed), stderr='') if '--show-properties=unexpanded' \
                in cmd else self.fake_compile()
        config = self.tmp / 'iso dir/arduino-cli.yaml'
        user = ['compiler.cpp.extra_flags=-DUSER {build.x}', 'build.foo=1']
        with mock.patch.object(ab.subprocess, 'run', side_effect=run):
            row = ab.compile_sketch('rig_m4', 'test/hil/core_cdc', self.root / 'b', user, {}, config)
        self.assertEqual(row['status'], 'ok', row.get('firstError'))
        show, compile_ = calls
        self.assertEqual(show[show.index('--config-file') + 1], str(config))
        self.assertEqual([show[i + 1] for i, a in enumerate(show) if a == '--build-property'], user)
        flag = f'"-ffile-prefix-map={self.tmp / "iso dir"}=.arduino-cli"'
        self.assertEqual([compile_[i + 1] for i, a in enumerate(compile_) if a == '--build-property'],
                         ['build.foo=1', f'compiler.c.extra_flags=-DPLATFORM {flag}',
                          f'compiler.cpp.extra_flags=-DUSER {{build.x}} {flag}', f'compiler.S.extra_flags={flag}'])
        with mock.patch.object(ab.subprocess, 'run', side_effect=run):   # resolved once per FQBN and properties
            ab.compile_sketch('rig_m4', 'test/hil/core_cdc', self.root / 'b', user, {}, config)
        self.assertEqual(len(calls), 3)

    def test_unresolvable_properties_are_an_error_before_compiling(self):
        failed = mock.Mock(returncode=1, stdout='', stderr='Error resolving FQBN\nplatform not installed\n')
        with mock.patch.object(ab.subprocess, 'run', return_value=failed) as run:
            row = ab.compile_sketch('rig_m4', 'test/hil/core_cdc', self.root / 'b', [], {}, self.tmp / 'x/cli.yaml')
        self.assertEqual((row['status'], row['firstError']),
                         ('error', 'arduino-cli --show-properties: platform not installed'))
        self.assertEqual(run.call_count, 1)

    def test_platform_through_a_link_to_the_checkout_is_this_checkout(self):
        link = self.tmp / 'sketchbook/hardware/fake/core'
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root)
        row, _ = self.compile(self.fake_compile(str(link)))
        self.assertEqual(row['status'], 'ok', row.get('firstError'))
        self.assertEqual(row['platform']['install_dir'], str(self.root))   # resolved for the record

    def test_platform_from_another_copy_fails(self):
        row, _ = self.compile(self.fake_compile('/home/u/Arduino/hardware/fake/core'))
        self.assertEqual(row['status'], 'failed')
        self.assertIn('not this checkout', row['firstError'])

    def test_bundled_library_from_a_user_copy_fails(self):
        user = self.dep('Wire')['library']
        row, _ = self.compile(self.fake_compile(used=[user]))
        self.assertEqual(row['status'], 'failed')
        self.assertIn('Wire from', row['firstError'])
        row, _ = self.compile(self.fake_compile(used=[{'name': 'Wire', 'install_dir': str(self.root /
                                                                                       'libraries/Wire')}]))
        self.assertEqual(row['status'], 'ok', row.get('firstError'))

    def test_prepared_dependencies_never_take_a_user_copy_of_a_bundled_library(self):
        libs = [self.dep('Dep C', 'Fake USB'), self.dep('Dep U'), self.dep('Wire'), self.dep('Fake USB')]
        with mock.patch.object(ab, '_cli_json', return_value={'installed_libraries': libs}):
            prepared = ab.prepared_dependencies()
        self.assertEqual(list(prepared), ['Dep C', 'Dep U'])   # Fake USB bundled; its depends= from the bundled copy
        self.assertFalse(any(d.get('shadowOnly') for d in prepared.values()))
        (self.root / 'library.properties').write_text('depends=Never\n')
        self.assertEqual(ab.declared_dependencies(), ['Dep C'])   # a core declares no depends=

    def test_cli_env_links_the_checkout_and_only_the_prepared_libraries(self):
        dep = self.dep('Dep C')['library']
        data = {'config': {'directories': {'data': '/d', 'user': '/home/u/Arduino'}, 'x': 1}}
        with mock.patch.object(ab, '_cli_json', return_value=data):
            cfg = ab.cli_env(self.tmp / 'env', {'Dep C': {'dir': dep['install_dir']}})
        conf = json.loads(cfg.read_text())
        sketchbook = Path(conf['directories']['user'])
        self.assertEqual((conf['directories']['data'], conf['x']), ('/d', 1))
        self.assertEqual((sketchbook / 'hardware/fake/core').resolve(), self.root)
        self.assertEqual([p.name for p in (sketchbook / 'libraries').iterdir()], ['Dep_C'])

    def test_a_failed_cli_env_leaves_no_environment_behind(self):
        self.patch(ab, 'source_identity', lambda: ({'head': 'abc', 'dirty': False, 'status': ''}, ''))
        self.patch(ab, 'prepared_dependencies', lambda: {})
        self.patch(ab, 'build_all', mock.Mock(side_effect=AssertionError('compiled')))
        with mock.patch.object(ab.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr='bad')):
            code, out = self.run_main(['--board', 'm4', '--example', 'libraries/Wire/examples/scan'])
        self.assertEqual(code, 2)
        self.assertIn('config dump', out['error'])
        self.assertFalse((self.root / f'build/.arduino-cli-{os.getpid()}').exists())

    def test_foreign_core_header_is_flagged_and_the_linked_checkout_is_not(self):
        other = self.tmp / 'other_core'
        shutil.copytree(self.root, other)
        (other / 'cores/fake/main.cpp').write_text('// drifted\n')
        link = self.tmp / 'sketchbook/hardware/fake/core'
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root)
        build = self.tmp / 'b'
        build.mkdir()
        (build / 'main.cpp.d').write_text(f'main.cpp.o: {other}/cores/fake/main.cpp {link}/cores/fake/main.cpp\n')
        self.assertEqual([(h['counterpart'], h['matches']) for h in ab.foreign_headers(ab.dep_paths(build))],
                         [('cores/fake/main.cpp', False)])

    def test_submodules_bind_at_their_gitlinks_clean(self):
        ident = {'submodules': [
            {'path': 'libraries/USB', 'commit': 'a', 'state': ' ', 'dirty': True},
            {'path': 'libraries/Z', 'commit': 'b', 'state': '-', 'dirty': False},
            {'path': 'libraries/Y', 'commit': 'c', 'state': '+', 'dirty': False}]}
        self.assertEqual(ab.submodule_problems(ident), ['libraries/Z is not initialised',
                                                        'libraries/Y is not at its gitlink',
                                                        'libraries/USB has changes'])
        clean = {**ident['submodules'][0], 'dirty': False}
        self.assertEqual(ab.submodule_problems({'submodules': [clean]}), [])

    def test_record_refused_unless_submodules_are_at_their_gitlinks(self):
        roster = self.core_roster()
        ident = {'head': 'abc', 'dirty': False, 'diffSha256': 'd', 'status': '',
                 'submodules': [{'path': 'libraries/USB', 'commit': 'a', 'state': '-'}]}
        self.patch(ab, 'source_identity', lambda: (ident, ''))
        self.patch(ab, 'build_all', mock.Mock(side_effect=AssertionError('compiled')))
        for extra in ([], ['--experimental']):
            with self.subTest(extra=extra):
                code, out = self.run_main(['--board', 'rig_m4', '--shared', '--hil-config', str(roster), '--record',
                                           str(self.root / 'build/hil/rig_m4.build.json'), *extra])
                self.assertEqual(code, 2)
                self.assertIn('libraries/USB is not initialised', out['error'])

    def submodule_core(self):
        """The core as a git checkout with libraries/USB a submodule holding its own `nested` one, both initialised;
        returns a git runner on the env the engine inherits, whose git drops GIT_CONFIG_COUNT (a local var): only the
        runner's file:// clones need protocol.file.allow."""
        env = git_env(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='protocol.file.allow', GIT_CONFIG_VALUE_0='always')
        environ = mock.patch.dict(os.environ, env, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

        def git(where, *a):
            subprocess.run(['git', '-C', str(where), *a], check=True, capture_output=True, env=env)
        nested = self.tmp / 'nested_upstream'
        nested.mkdir()
        (nested / 'header.h').write_text('// nested\n')
        git(nested, 'init', '-q', '-b', 'main')
        git(nested, 'add', '-A')
        git(nested, 'commit', '-qm', 'nested')
        sub = self.tmp / 'usb_upstream'
        shutil.copytree(self.root / 'libraries/USB', sub)
        git(sub, 'init', '-q', '-b', 'main')
        git(sub, 'submodule', '-q', 'add', str(nested), 'nested')
        git(sub, 'add', '-A')
        git(sub, 'commit', '-qm', 'usb')
        shutil.rmtree(self.root / 'libraries/USB')
        git(self.root, 'init', '-q', '-b', 'main')
        git(self.root, 'submodule', '-q', 'add', str(sub), 'libraries/USB')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'core')
        git(self.root, 'submodule', '-q', 'update', '--init', '--recursive')
        return git

    def test_a_dirty_submodule_counts_whatever_its_ignore_setting(self):
        """At any depth: an ignore=dirty at the top and one inside the submodule for its own nested one."""
        git = self.submodule_core()
        git(self.root, 'config', 'submodule.libraries/USB.ignore', 'dirty')
        git(self.root / 'libraries/USB', 'config', 'submodule.nested.ignore', 'dirty')
        ident, _ = ab.source_identity()
        self.assertEqual((ident['dirty'], ab.submodule_problems(ident)), (False, []))
        for edited, path in (('libraries/USB/nested/header.h', 'libraries/USB/nested'),
                             ('libraries/USB/library.properties', 'libraries/USB')):
            with self.subTest(edited=edited):
                original = (self.root / edited).read_text()
                (self.root / edited).write_text(original + '// edited\n')
                ident, _ = ab.source_identity()
                self.assertTrue(ident['dirty'])
                self.assertEqual(ab.submodule_problems(ident), [f'{path} has changes'])
                self.assertIn(f'~~ {path}', ident['status'])
                (self.root / edited).write_text(original)

    def test_a_nested_gitlink_staged_inside_a_submodule_counts(self):
        """Recursive status compares the nested one to the submodule's index, so only the submodule's own status
        shows its gitlink moved, whatever either ignore setting says."""
        git = self.submodule_core()
        git(self.root, 'config', 'submodule.libraries/USB.ignore', 'dirty')
        git(self.root / 'libraries/USB', 'config', 'submodule.nested.ignore', 'all')
        nested = self.root / 'libraries/USB/nested'
        (nested / 'header.h').write_text('// moved\n')
        git(nested, 'commit', '-qam', 'moved')
        git(self.root / 'libraries/USB', 'add', 'nested')
        ident, _ = ab.source_identity()
        self.assertEqual([s['state'] for s in ident['submodules']], [' ', ' '])
        self.assertTrue(ident['dirty'])
        self.assertEqual(ab.submodule_problems(ident), ['libraries/USB has changes'])

    def test_a_callers_git_dir_does_not_hide_a_dirty_submodule(self):
        """A hook's GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE aimed at the superproject would turn the per-submodule status
        into the superproject's, where ignore=all hides the change."""
        git = self.submodule_core()
        git(self.root, 'config', 'submodule.libraries/USB.ignore', 'all')
        (self.root / 'libraries/USB/library.properties').write_text('// edited\n')
        with mock.patch.dict(os.environ, {'GIT_DIR': str(self.root / '.git'), 'GIT_WORK_TREE': str(self.root),
                                          'GIT_INDEX_FILE': str(self.root / '.git/index')}):
            self.assertIn('~~ libraries/USB\n', ab.worktree_status())

    def test_a_describe_name_with_parentheses_is_not_part_of_the_path(self):
        git = self.submodule_core()
        git(self.root / 'libraries/USB', 'tag', 'v1(legacy)')
        ident, _ = ab.source_identity()
        self.assertEqual([s['path'] for s in ident['submodules']], ['libraries/USB', 'libraries/USB/nested'])
        self.assertEqual((ident['dirty'], ab.submodule_problems(ident)), (False, []))

    def test_a_core_record_refuses_any_untracked_file(self):
        self.patch(ab, 'source_identity', lambda: ({'head': 'abc', 'dirty': True, 'diffSha256': 'd',
                                                    'status': '?? variants/new/variant.h\n', 'submodules': []}, ''))
        self.patch(ab, 'build_all', mock.Mock(side_effect=AssertionError('compiled')))
        code, out = self.run_main(['--board', 'rig_m4', '--shared', '--hil-config', str(self.core_roster()), '--record',
                                   str(self.root / 'build/hil/r.json'), '--experimental'])
        self.assertEqual(code, 2)
        self.assertIn('variants/new/variant.h', out['error'])

    def test_a_header_from_another_copy_of_the_core_fails_the_sketch(self):
        other = self.tmp / 'other_core'
        shutil.copytree(self.root, other)
        build = self.root / 'b'

        def run(cmd, **kw):   # the compile leaves a .d naming the other copy's (identical) header
            (build / 'main.cpp.d').write_text(f'main.cpp.o: {other}/cores/fake/main.cpp\n')
            return self.fake_compile()
        with mock.patch.object(ab.subprocess, 'run', side_effect=run):
            row = ab.compile_sketch('m4', 'libraries/Wire/examples/scan', build, [], {}, self.tmp / 'cli.yaml')
        self.assertEqual(row['status'], 'failed')
        self.assertIn('another copy of the core', row['firstError'])

    def test_a_submodule_off_its_gitlink_or_unreadable_is_never_clean(self):
        subs = [{'path': 'libraries/USB', 'commit': 'a', 'state': '+', 'dirty': False},
                {'path': 'libraries/Z', 'commit': 'b', 'state': '-', 'dirty': False},
                {'path': 'libraries/W', 'commit': 'c', 'state': ' ', 'dirty': False}]
        with mock.patch.object(ab, 'git', return_value=''), mock.patch.object(ab, 'submodules', return_value=subs):
            self.assertEqual(ab.worktree_status(), '~+ libraries/USB\n~- libraries/Z\n')   # what admission compares
        listed = mock.Mock(returncode=0, stdout=' 3e5369cd libraries/USB (3.7.3)\n', stderr='')
        failed = mock.Mock(returncode=128, stdout='', stderr='fatal: not a git repository')
        with mock.patch.object(ab, '_git_local_vars', return_value=frozenset()), \
                mock.patch.object(ab.subprocess, 'run', side_effect=[listed, failed]), \
                contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as e:
            ab.submodules()
        self.assertEqual(e.exception.code, 2)
        error = json.loads(out.getvalue())['error']
        self.assertIn('-C libraries/USB status', error)
        self.assertIn('fatal: not a git repository', error)

    def test_submodule_status_is_parsed_recursively(self):
        out = (' 3e5369cd libraries/USB (3.7.3)\n-5b48867c libraries/Z\n+0123abcd libraries/USB/lib/x (v1)\n')
        with mock.patch.object(ab, 'git', side_effect=lambda *a: out if a[0] == 'submodule' else '') as git:
            self.assertEqual(ab.submodules(), [
                {'path': 'libraries/USB', 'commit': '3e5369cd', 'state': ' ', 'dirty': False},
                {'path': 'libraries/Z', 'commit': '5b48867c', 'state': '-', 'dirty': False},
                {'path': 'libraries/USB/lib/x', 'commit': '0123abcd', 'state': '+', 'dirty': False}])
        self.assertEqual([c.args for c in git.call_args_list], [('submodule', 'status', '--recursive')] + [
            ('-C', d, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=dirty')
            for d in ('libraries/USB', 'libraries/USB/lib/x')])

    def test_a_submodule_path_keeps_its_spaces_and_parentheses(self):
        out = (' 3e5369cd libs/my lib (v1)\n+0123abcd libs/odd (x) (heads/main)\n-5b48867c libs/new (y)\n'
               'U0000000 libs/in conflict\n')
        with mock.patch.object(ab, 'git', side_effect=lambda *a: out if a[0] == 'submodule' else '') as git:
            self.assertEqual([s['path'] for s in ab.submodules()],
                             ['libs/my lib', 'libs/odd (x)', 'libs/new (y)', 'libs/in conflict'])
        self.assertEqual([c.args[1] for c in git.call_args_list[1:]], ['libs/my lib', 'libs/odd (x)',
                                                                        'libs/in conflict'])



class NamedBuilds(Lib):
    """Feature 'named': one HIL sketch dir built under two names with different menus."""

    def setUp(self):
        super().setUp()
        self.root = fake.core(self.tmp / 'core')
        self.P = fake.core_profile(self.root, self.agentrc, HIL_SKETCHES={'device': (
            'test/hil/core_cdc', {'core_cdc_tusb2': ('test/hil/core_cdc', 'usb=tusb,debug=on')})})
        self.patch(ab, 'P', self.P)

    def test_feature_advertised_and_existing_profiles_unchanged(self):
        self.assertIn('named', ab.PROFILE_FEATURES)
        self.assertEqual(ab.PROFILE_API, 1)
        self.patch(ab, 'P', fake.core_profile(self.root, self.agentrc))
        self.assertEqual(ab.hil_sketches('rig_m4'), fake.CORE_HIL_SKETCHES['device'])
        self.assertEqual(ab.named_builds(), {})
        self.assertEqual(ab.fqbn('rig_m4', 'test/hil/core_cdc_tusb'), 'fake:core:m4:usb=tusb')

    def test_bind_refuses_a_malformed_entry(self):
        self.patch(ab, 'P', None)
        for entry in ({'a/b': ('test/hil/core_cdc', '')}, {'x': 'test/hil/core_cdc'}, {'x': ('s', 'nomenu')},
                      {'x': ('s', ''), 'y': ('s', '')}, ('x', 's'), {'.': ('s', '')}, {'..': ('s', '')},
                      {'.sketch': ('s', '')}, {'a\\b': ('s', '')}, {'': ('s', '')}):
            with self.subTest(entry=entry), self.assertRaisesRegex(RuntimeError, 'HIL_SKETCHES: entries neither'):
                ab.bind(fake.core_profile(self.root, self.agentrc, HIL_SKETCHES={'device': ('test/hil/core_cdc',
                                                                                            entry)}))
            self.assertIsNone(ab.P)
        self.assertIs(ab.bind(self.P), ab)

    def test_bind_refuses_a_build_id_twice_in_a_role_or_defined_two_ways(self):
        self.patch(ab, 'P', None)
        cdc, tusb = 'test/hil/core_cdc', 'test/hil/core_cdc_tusb'
        for table, why in (
                ({'device': (cdc, cdc)}, f"'device' lists {cdc} twice"),
                ({'device': (cdc, {'n': (cdc, '')}, {'n': (tusb, '')})}, "'device' lists n twice"),
                ({'device': (cdc, {'cdc': (cdc, 'usb=arduino')}), 'host': ({'cdc': (tusb, 'usb=tusb')},)},
                 'cdc is defined two ways'),
                ({'device': (cdc, {'core_x': (cdc, '')}), 'host': ('core_x',)}, 'core_x is defined two ways')):
            with self.subTest(table=table), self.assertRaisesRegex(RuntimeError, why):
                ab.bind(fake.core_profile(self.root, self.agentrc, HIL_SKETCHES=table))
            self.assertIsNone(ab.P)
        same = {'device': (cdc, {'n': (cdc, 'usb=tusb')}), 'host': ({'n': (cdc, 'usb=tusb')},)}
        self.assertIs(ab.bind(fake.core_profile(self.root, self.agentrc, HIL_SKETCHES=same)), ab)

    def test_names_key_the_build_dir_the_fqbn_and_the_source(self):
        self.assertEqual(ab.hil_sketches('rig_m4'), ('test/hil/core_cdc', 'core_cdc_tusb2'))
        self.assertEqual([ab.source_of(e) for e in ab.hil_sketches('rig_m4')], ['test/hil/core_cdc'] * 2)
        self.assertEqual([ab.build_key(e) for e in ab.hil_sketches('rig_m4')], ['core_cdc', 'core_cdc_tusb2'])
        self.assertEqual(ab.fqbn('rig_m4', 'test/hil/core_cdc'), 'fake:core:m4:usb=arduino')
        self.assertEqual(ab.fqbn('rig_m4', 'core_cdc_tusb2'), 'fake:core:m4:usb=tusb,debug=on')
        self.assertTrue(ab.eligible('rig_m4', 'core_cdc_tusb2'))
        self.assertFalse(ab.eligible('m4', 'core_cdc_tusb2'))   # a rig board's HIL build only
        self.assertEqual(ab.with_options('v:a:b', 'x=1'), 'v:a:b:x=1')

    def test_a_change_to_the_source_builds_every_name(self):
        sel = ab.select(['test/hil/core_cdc/core_cdc.ino'], roster=['rig_m4'])
        self.assertEqual(sel['builds'], {'rig_m4': ['core_cdc_tusb2', 'test/hil/core_cdc']})
        self.assertEqual(sel['hil']['boards'], ['rig_m4'])
        self.patch(self.P, 'HIL_SKETCHES', {'device': ({'only_named': ('test/hil/core_cdc_tusb', '')},)})
        self.assertEqual(ab.classify('test/hil/core_cdc_tusb/x.h')[:2], ('hil_sketch', 'test/hil/core_cdc_tusb'))
        self.assertEqual(ab.select(['test/hil/core_cdc_tusb/x.h'])['builds'], {'rig_m4': ['only_named']})

    def test_a_name_clashing_with_a_sketch_basename_is_refused(self):
        self.patch(self.P, 'HIL_SKETCHES', {'device': ('test/hil/core_cdc', {'core_cdc': ('test/hil/core_cdc', '')})})
        self.assertEqual(ab.basename_clashes({'rig_m4': list(ab.hil_sketches('rig_m4'))}),
                         ['rig_m4: test/hil/core_cdc and core_cdc'])

    def test_both_names_compile_the_one_source_into_their_own_dirs(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return cli_compile([], 'fake:core', '1.0.0', str(self.root), str(self.root))
        with mock.patch.object(ab.subprocess, 'run', side_effect=run):
            rows = ab.build_all(2, {'rig_m4': list(ab.hil_sketches('rig_m4'))}, self.root / 'build/hil', [], {},
                                self.tmp / 'cli/arduino-cli.yaml')
        self.assertEqual([(r['example'], r['status'], r['fqbn'], r['buildDir']) for r in rows],
                         [('core_cdc_tusb2', 'ok', 'fake:core:m4:usb=tusb,debug=on', 'build/hil/rig_m4/core_cdc_tusb2'),
                          ('test/hil/core_cdc', 'ok', 'fake:core:m4:usb=arduino', 'build/hil/rig_m4/core_cdc')])
        compiles = [c for c in calls if '--build-path' in c]
        self.assertEqual({c[-1] for c in compiles}, {str(self.root / 'test/hil/core_cdc')})
        self.P.BOARDS['rig_m4']['fixture'] = 'cores/fake/main.cpp'   # a fixture board copies the source per name
        src = ab.sketch_source('rig_m4', 'core_cdc_tusb2', self.root / 'build/hil/rig_m4/core_cdc_tusb2')
        self.assertEqual((src.name, src.parent.name), ('core_cdc', 'core_cdc_tusb2'))
        self.assertTrue((src / 'core_cdc.ino').is_file() and (src / 'main.cpp').is_file())

    def test_the_record_keys_each_build_by_its_name_with_its_source(self):
        roster = self.root / 'test/hil/ci.json'
        roster.write_text(json.dumps({'boards': [{'name': 'rig_m4', 'uid': 'U', 'flasher': {
            'name': 'jlink', 'uid': '1', 'args': '-device X'}, 'arduino': {'tests': ['cdc']}}]}))
        rows = []
        for e in ab.hil_sketches('rig_m4'):
            b = self.root / 'build/hil/rig_m4' / ab.build_key(e)
            b.mkdir(parents=True)
            (b / 'core_cdc.ino.elf').write_bytes(elf32([(1, 0x4000, 0x20)]))
            (b / 'core_cdc.ino.hex').write_text(ihex(0x4000, bytes(0x20)))
            rows.append({'example': e, 'buildDir': ab.rel(b), 'fqbn': ab.fqbn('rig_m4', e), 'foreignHeaders': [],
                         'platform': {'id': 'fake:core', 'version': '1.0.0', 'install_dir': str(self.root)}})
        args = mock.Mock(experimental=False, build_property=[], record=str(self.root / 'build/r.json'))
        ident = {'head': 'abc', 'dirty': False, 'diffSha256': 'd', 'status': ''}
        with mock.patch.object(ab, 'source_identity', return_value=(ident, '')), \
                mock.patch.object(ab, 'tool_version', return_value='1.5.1'), \
                mock.patch.object(ab, 'platform_git', return_value=None):
            rec, err = ab.make_record(args, 'rig_m4', rows, ident, '', roster, {})
        self.assertIsNone(err)
        self.assertEqual(list(rec['sketches']), ['core_cdc', 'core_cdc_tusb2'])
        named = rec['sketches']['core_cdc_tusb2']
        self.assertEqual((named['example'], named['source'], named['fqbn'], named['elf']),
                         ('core_cdc_tusb2', 'test/hil/core_cdc', 'fake:core:m4:usb=tusb,debug=on',
                          'build/hil/rig_m4/core_cdc_tusb2/core_cdc.ino.elf'))
        self.assertNotIn('source', rec['sketches']['core_cdc'])


if __name__ == '__main__':
    unittest.main()
