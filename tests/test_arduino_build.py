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
        self.assertEqual(sel['hil'], {'boards': ['rig_host', 'rig_ocd'], 'full': True, 'uncovered': []})

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
        got = [(h['counterpart'], h['matches']) for h in ab.foreign_headers(build)]
        self.assertEqual(sorted(got), [('src/fake.h', False), ('src/fake.h', True), ('src/only_there.h', False),
                                       ('src/ports/fa/port.cpp', False)])

    def fake_compile(self, used=None, success=True, err='', platform='/x'):
        used = [{'name': fake.NAME, 'install_dir': str(self.root)}] if used is None else used
        out = {'success': success, 'compiler_err': err, 'builder_result': {
            'used_libraries': used, 'board_platform': {'id': 'vend:arch', 'version': '1.0', 'install_dir': platform},
            'build_platform': {'id': 'vend:arch', 'install_dir': '/y'}}}
        return mock.Mock(returncode=0 if success else 1, stdout=json.dumps(out), stderr='')

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
        row = self.compile(self.fake_compile())
        self.assertEqual((row['status'], row['vidPid'], row['foreignHeaders']), ('ok', '1234:5678', []))
        self.assertEqual(row['libraries'], [{'name': fake.NAME, 'version': None, 'dir': str(self.root)}])
        self.assertIn('--library', (self.root / row['log']).read_text())

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
        self.patch(ab, 'platform_git', lambda d: None)

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

    def test_drifted_core_header_refused_unless_experimental(self):
        hdr = [{'path': '/core/x.h', 'matches': False}]
        _, err = self.record(self.samd_row(headers=hdr))
        self.assertIn('differ from this checkout', err)
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
        self.assertEqual(src, self.root / 'build/hil/rig_esp/.sketch/echo')
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


if __name__ == '__main__':
    unittest.main()
