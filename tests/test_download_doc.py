"""Tests for download-doc's plan(): a document already filed by hand is
reported as legacy, never imported a second time."""
import contextlib
import io
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'download-doc' / 'scripts'))
import doclib  # noqa: E402
import retitle  # noqa: E402
import sync  # noqa: E402
import titles  # noqa: E402
import vendor_allwinner  # noqa: E402
import vendor_arm  # noqa: E402
import vendor_espressif  # noqa: E402
import vendor_geehy  # noqa: E402
import vendor_hpmicro  # noqa: E402
import vendor_microchip  # noqa: E402
import vendor_nxp  # noqa: E402
import vendor_renesas  # noqa: E402
import vendor_silabs  # noqa: E402
import vendor_st  # noqa: E402
import vendor_ti  # noqa: E402
import vendor_wch  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402


def doc(doc_id, aliases=()):
    return doclib.Doc(vendor='st', doc_id=doc_id, doc_type='reference-manual', version='8.0',
                      title='t', url='u', author='STMicroelectronics', aliases=list(aliases))


def legacy(*titles):
    return {doclib.norm_title(t): {'id': n, 'title': t} for n, t in enumerate(titles, 1)}


def setUpModule():
    guard_survivors(__name__)


class Legacy(unittest.TestCase):
    def verdict(self, d, titles):
        p = doclib.plan([d], {}, legacy(*titles))
        return [hit['id'] for _, hit in p['legacy']], len(p['new'])

    def test_a_short_vendor_code_claims_a_hand_filed_title_it_opens(self):
        title = 'RM0433 STM32H742, STM32H743/753 and STM32H750 Value line - Reference manual'
        self.assertEqual(self.verdict(doc('RM0433'), [title]), ([1], 0))

    def test_a_code_does_not_claim_a_title_it_only_begins_like(self):
        self.assertEqual(self.verdict(doc('RM0433'), ['RM04331 something else']), ([], 1))

    def test_a_short_plain_word_still_needs_an_exact_title(self):
        self.assertEqual(self.verdict(doc('x', ['Pico']), ['Pico W Datasheet']), ([], 1))
        self.assertEqual(self.verdict(doc('x', ['Pico']), ['Pico']), ([1], 0))

    def test_a_long_alias_matches_on_a_whole_word_prefix(self):
        self.assertEqual(self.verdict(doc('x', ['RP2040 Datasheet']),
                                      ['RP2040 Datasheet: A microcontroller by Raspberry Pi']), ([1], 0))

    def test_a_part_number_does_not_claim_another_document_of_that_part(self):
        ti = doc('TM4C123GH6PM', ['TM4C123GH6PM Datasheet', 'TM4C123GH6PM'])
        self.assertEqual(self.verdict(ti, ['TM4C123GH6PM Errata']), ([], 1))

    def test_a_short_part_number_is_not_a_document_code(self):
        self.assertEqual(self.verdict(doc('PF3000'), ['PF3000 Evaluation Board User Guide']), ([], 1))

    def test_an_exact_title_wins_over_an_earlier_prefix(self):
        ti = doc('TM4C123GH6PM', ['TM4C123GH6PM Datasheet', 'TM4C123GH6PM'])
        self.assertEqual(self.verdict(ti, ['TM4C123GH6PM Datasheet extra', 'TM4C123GH6PM']), ([2], 0))

    def test_a_book_with_an_identifier_is_compared_by_revision_not_title(self):
        p = doclib.plan([doc('RM0433')], {'st:RM0433': {'rev': '8.0'}}, legacy('RM0433 manual'))
        self.assertEqual((p['current'], p['legacy']), ([doc('RM0433')], []))


class IdFirstTitle(unittest.TestCase):
    def test_a_new_import_is_titled_vendor_id_first(self):
        d = doclib.Doc(vendor='st', doc_id='ES0392', doc_type='errata', version='15.0',
                       title='STM32H7 device errata', url='u', author='STMicroelectronics')
        self.assertEqual(d.calibre_title(), 'ES0392 STM32H7 device errata Rev 15.0')

    def test_a_filename_stem_trails_the_title_and_keeps_it_unique(self):
        def stem_doc(doc_id, title, version=None):
            return doclib.Doc(vendor='x', doc_id=doc_id, doc_type='errata', version=version, title=title,
                              url='u', author='x', verify_id=False).calibre_title()
        self.assertEqual(stem_doc('esp32-s3_trm_en', 'ESP32-S3 Technical Reference Manual', '1.8'),
                         'ESP32-S3 Technical Reference Manual (esp32-s3_trm_en) Rev 1.8')
        self.assertEqual(stem_doc('ug-1', '_EVB USB2514 Users Guide'), '_EVB USB2514 Users Guide (ug-1)')
        self.assertEqual({stem_doc('rx130-errata', ''), stem_doc('rx210-errata', '')},
                         {'Errata (rx130-errata)', 'Errata (rx210-errata)'})

    def test_only_an_exact_copy_of_the_id_is_dropped(self):
        self.assertEqual(titles.id_first('LPC55S6x manual (UM11126)', 'UM11126'), 'UM11126 LPC55S6x manual')
        self.assertEqual(titles.id_first('MCX manual (UM11750-V3)', 'UM11750'), 'UM11750 MCX manual (UM11750-V3)')

    def test_a_leading_copy_of_the_id_is_dropped_but_not_a_qualified_one(self):
        self.assertEqual(titles.id_first('RM0433: STM32H7 manual', 'RM0433'), 'RM0433 STM32H7 manual')
        self.assertEqual(titles.id_first('UM11750-V3 MCX manual', 'UM11750'), 'UM11750 UM11750-V3 MCX manual')
        self.assertEqual(titles.id_first('RM04331 manual', 'RM0433'), 'RM0433 RM04331 manual')

    def test_a_filename_stem_loses_its_leading_id_but_keeps_its_inner_copies(self):
        self.assertEqual(titles.id_first('AN4497_LCF_for_Qorivva — Application note (AN4497) Rev 2', 'AN4497'),
                         'AN4497 LCF_for_Qorivva — Application note Rev 2')
        self.assertEqual(titles.id_first('USB251xB xBi Data Sheet DS00001692 (USB251xB-xBi-Data-Sheet-DS00001692)',
                                         'DS00001692'),
                         'DS00001692 USB251xB xBi Data Sheet (USB251xB-xBi-Data-Sheet-DS00001692)')
        self.assertEqual(titles.id_first('USB2514B Checklist DS00004541 — Application note', 'DS00004541'),
                         'DS00004541 USB2514B Checklist — Application note')
        self.assertEqual(titles.id_first('USB251xB xBi Errata DS80000627D', 'DS80000627'),
                         'DS80000627 USB251xB xBi Errata DS80000627D')
        self.assertEqual(titles.id_first('USB251xB-Sheet-DS00001692 DS00001692', 'DS00001692'),
                         'DS00001692 USB251xB-Sheet-DS00001692')
        self.assertEqual(titles.id_first('DS1 DS1_foo', 'DS1'), 'DS1 DS1_foo')

    def test_a_title_never_carries_the_id_twice(self):
        rm = {'code': 'RM0433', 'type': 'Reference Manual', 'version': '8.0'}
        self.assertEqual(titles.title({**rm, 'title': ''}), 'RM0433 Reference manual Rev 8.0')
        self.assertEqual(titles.title({**rm, 'title': 'RM0433–STM32H7 reference manual'}),
                         'RM0433 STM32H7 reference manual Rev 8.0')

    def test_a_prefixed_title_loses_the_copies_a_past_run_left(self):
        self.assertEqual(retitle.reprefixed('DS60001477 SAM L21 Data Sheet DS60001477 (SAM-L21-Data-Sheet-DS60001477)'),
                         'DS60001477 SAM L21 Data Sheet (SAM-L21-Data-Sheet-DS60001477)')
        self.assertEqual(retitle.reprefixed('AN1141, USB Host Guide'), 'AN1141 USB Host Guide')
        self.assertEqual(retitle.reprefixed('UM11750-V3 MCX manual'), 'UM11750-V3 MCX manual')
        self.assertEqual(retitle.reprefixed('RM0433 STM32H7 manual'), 'RM0433 STM32H7 manual')
        self.assertEqual(retitle.reprefixed('UM11750 UM11750-V3 MCX manual UM11750'), 'UM11750 UM11750-V3 MCX manual')

    def test_a_second_run_changes_nothing(self):
        for t in ('DS00001692 USB251xB-Sheet-DS00001692 DS00001692', 'DS100 DS100_foo', 'AN1141, USB Host Guide'):
            once = retitle.reprefixed(t)
            self.assertEqual(retitle.reprefixed(once), once, t)
        self.assertEqual(retitle.reprefixed('DS00001692 USB251xB-Sheet-DS00001692 DS00001692'),
                         'DS00001692 USB251xB-Sheet-DS00001692')


class RetitlePlan(unittest.TestCase):
    """A rename is planned only for a book whose files are where the database says."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lib = Path(tmp.name)
        saved = retitle.LIB
        self.addCleanup(lambda: setattr(retitle, 'LIB', saved))
        retitle.LIB = self.lib
        self.con = sqlite3.connect(self.lib / 'metadata.db')
        self.addCleanup(self.con.close)
        self.con.executescript('create table books (id integer primary key, title text, path text);'
                               'create table identifiers (book integer, type text, val text);'
                               'create table data (book integer, format text, name text);')

    def book(self, id, title, folder=True, formats={'PDF': True}):
        """formats: each format the database lists, and whether its file is on disk."""
        path = f'NXP/{title} ({id})'
        self.con.execute('insert into books values (?, ?, ?)', (id, title, path))
        self.con.execute('insert into identifiers values (?, ?, ?)', (id, 'nxp', 'UM10503'))
        for fmt in formats:
            self.con.execute('insert into data values (?, ?, ?)', (id, fmt, 'manual'))
        self.con.commit()
        if folder:
            (self.lib / path).mkdir(parents=True)
            for fmt, on_disk in formats.items():
                if on_disk:
                    (self.lib / path / f'manual.{fmt.lower()}').write_bytes(b'%PDF')

    def test_a_book_whose_files_are_not_on_disk_is_withheld(self):
        self.book(1, 'LPC43xx manual (UM10503)')
        self.book(2, 'LPC43xx guide (UM10503)', folder=False)
        self.book(3, 'LPC43xx notes (UM10503)', formats={'PDF': False})
        self.book(4, 'UM10503 LPC43xx sheet UM10503', folder=False)
        self.book(5, 'LPC43xx book (UM10503)', formats={'PDF': True, 'EPUB': False})
        self.book(6, 'LPC43xx ebook (UM10503)', formats={'PDF': True, 'EPUB': True})
        self.book(7, 'LPC43xx card (UM10503)', folder=False, formats={})
        self.book(8, 'LPC43xx leaflet (UM10503)', formats={})
        plan, stats, conflicts, absent = retitle.build_plan(use_pdf=False)
        self.assertEqual(plan, [(1, 'LPC43xx manual (UM10503)', 'UM10503 LPC43xx manual'),
                                (6, 'LPC43xx ebook (UM10503)', 'UM10503 LPC43xx ebook'),
                                (8, 'LPC43xx leaflet (UM10503)', 'UM10503 LPC43xx leaflet')])
        self.assertEqual(absent, [2, 3, 4, 5, 7])

    def test_a_dry_run_prints_titles_outside_the_console_encoding(self):
        # Most ST titles carry ®, which a GBK or ASCII pipe cannot encode.
        self.book(1, 'LPC43xx® manual (UM10503)')
        self.book(2, 'LPC43xx guide (UM10503)', folder=False)
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONIOENCODING', 'PYTHONUTF8')}
        done = subprocess.run([sys.executable, str(Path(retitle.__file__)), '--no-pdf'], capture_output=True,
                              env=dict(env, CALIBRE_LIBRARY=str(self.lib)))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout.decode('utf-8')
        self.assertIn('+  UM10503 LPC43xx® manual', out)
        self.assertIn('⚠️  1 renames withheld', out)

    def test_apply_refuses_while_something_holds_the_library(self):
        class Held:
            def __init__(self, path):
                pass

            def blockers(self):
                return ['The Calibre GUI is open']

            def _run(self, *a):
                raise AssertionError('wrote to a held library')
        saved = (retitle.build_plan, retitle.doclib.Library, sys.argv)
        self.addCleanup(lambda: (setattr(retitle, 'build_plan', saved[0]),
                                 setattr(retitle.doclib, 'Library', saved[1]), setattr(sys, 'argv', saved[2])))
        retitle.build_plan = lambda use_pdf: ([(1, 'old', 'new')], retitle.collections.Counter(), [], [])
        retitle.doclib.Library = Held
        sys.argv = ['retitle.py', '--apply']
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(retitle.main(), 2)

    def test_apply_to_another_library_never_renames_through_the_content_server(self):
        # The plan's book ids come from this library's metadata.db; in the served one
        # they are other books.
        secret = self.lib / 'secret.yml'
        secret.write_text('calibre:\n  username: u\n  password: pw\n', encoding='utf-8')
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, '', 'Another calibre program is running')
        gui = staticmethod(lambda: iter([['/usr/bin/python3.13', '/usr/bin/calibre']]))
        plan = ([(1, 'old', 'new')], retitle.collections.Counter(), [], [])
        err = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {'DOWNLOAD_DOC_SECRET': str(secret)}), \
                unittest.mock.patch.object(doclib.Library, '_cmdlines', gui), \
                unittest.mock.patch.object(doclib.Library, '_gui_library', staticmethod(lambda: str(doclib.DEFAULT_LIBRARY))), \
                unittest.mock.patch.object(doclib.subprocess, 'run', run), \
                unittest.mock.patch.object(retitle, 'build_plan', lambda use_pdf: plan), \
                unittest.mock.patch.object(sys, 'argv', ['retitle.py', '--apply']), \
                unittest.mock.patch.object(Path, 'home', lambda: self.lib), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(retitle.main(), 2)
        self.assertIn('GUI is open', err.getvalue())
        self.assertEqual(calls, [])


    def rollback_tsv(self, *rows):
        tsv = self.lib / 'retitle.tsv'
        tsv.write_text(''.join(f'{row}\n' for row in rows), encoding='utf-8')
        return tsv

    def test_a_rollback_restores_only_books_still_titled_as_the_rename_left_them(self):
        self.book(1, 'UM10503 LPC43xx manual')
        self.book(2, 'LPC43xx guide (UM10503)')
        self.book(3, 'UM10503 LPC43xx notes, edited')
        self.book(4, 'UM10503 LPC43xx sheet', folder=False)
        self.book(5, 'UM10503 LPC43xx book', formats={'PDF': True, 'EPUB': False})
        tsv = self.rollback_tsv(*(f'{n}\tLPC43xx {kind} (UM10503)\tUM10503 LPC43xx {kind}' for n, kind in
                                  ((1, 'manual'), (2, 'guide'), (3, 'notes'), (4, 'sheet'), (5, 'book'), (9, 'card'))))
        plan, withheld, restored = retitle.rollback_plan(tsv)
        self.assertEqual(plan, [(1, 'UM10503 LPC43xx manual', 'LPC43xx manual (UM10503)')])
        self.assertEqual(withheld, [(3, "retitled since: 'UM10503 LPC43xx notes, edited'"),
                                    (4, 'the database names files that are not on disk'),
                                    (5, 'the database names files that are not on disk'), (9, 'no such book')])
        self.assertEqual(restored, 1)

    def test_a_title_is_split_from_the_next_row_by_a_newline_alone(self):
        self.book(1, 'UM10503 LPC43xx\u2028manual')
        tsv = self.rollback_tsv('1\tLPC43xx\u2028manual (UM10503)\tUM10503 LPC43xx\u2028manual')
        self.assertEqual(retitle.rollback_plan(tsv)[0], [(1, 'UM10503 LPC43xx\u2028manual', 'LPC43xx\u2028manual (UM10503)')])
        tsv.write_text('', encoding='utf-8')
        self.assertEqual(retitle.rollback_plan(tsv), ([], [], 0))

    def test_a_file_that_is_not_an_apply_tsv_is_refused_whole(self):
        self.book(1, 'UM10503 LPC43xx manual')
        row = '1\tLPC43xx manual (UM10503)\tUM10503 LPC43xx manual'
        for bad in ('x\told\tnew', '2\tonly two', row, '2\t\tnew', '2\told\tnew\textra'):
            with self.subTest(bad):
                with self.assertRaisesRegex(ValueError, 'not a row of a retitle --apply TSV'):
                    retitle.rollback_plan(self.rollback_tsv(row, bad))
                err = io.StringIO()
                with unittest.mock.patch.object(retitle.doclib, 'Library', lambda path: self.fail('touched calibre')), \
                        unittest.mock.patch.object(sys, 'argv', ['retitle.py', '--rollback', str(self.lib / 'retitle.tsv'),
                                                                 '--apply']), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    self.assertEqual(retitle.main(), 2)
                self.assertIn('REFUSED', err.getvalue())


@unittest.skipUnless(shutil.which('calibredb'), 'renames go through calibredb')
class RetitleRollbackInCalibre(unittest.TestCase):
    """--rollback renames back through calibredb in a real library, and each book's PDF moves with it."""

    def setUp(self):
        if doclib.Library._gui_running():
            self.skipTest("the Calibre GUI's lock refuses calibredb on every library")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lib = self.root / 'library'
        env = {k: v for k, v in os.environ.items() if k != 'DOWNLOAD_DOC_SECRET'}
        for patch in (unittest.mock.patch.dict(os.environ, dict(env, CALIBRE_CONFIG_DIRECTORY=str(self.root / 'config')),
                                               clear=True),
                      unittest.mock.patch.object(retitle, 'LIB', self.lib),
                      unittest.mock.patch.object(Path, 'home', lambda: self.root)):
            patch.start()
            self.addCleanup(patch.stop)
        pdf = self.root / 'manual.pdf'
        pdf.write_bytes(pdf_with('UM10503 LPC43xx manual'))
        for title in ('LPC43xx manual (UM10503)', 'LPC43xx guide (UM10503)'):
            self.calibredb('add', '-t', title, '-a', 'NXP Semiconductors', '-I', 'nxp:UM10503', str(pdf))

    def calibredb(self, *args):
        return subprocess.run(['calibredb', '--with-library', str(self.lib), *args], check=True,
                              capture_output=True, text=True).stdout

    def titles(self):
        return {b['id']: b['title'] for b in json.loads(self.calibredb('list', '--for-machine', '-f', 'title'))}

    def assert_pdfs_on_disk(self):
        _, files, books = retitle.read_library()
        for book, _, folder in books:
            self.assertEqual([f.suffix for f in files[book]], ['.pdf'])
            self.assertTrue(retitle.on_disk(folder, files[book]), folder)

    def retitle(self, *argv):
        out = io.StringIO()
        with unittest.mock.patch.object(sys, 'argv', ['retitle.py', *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            return retitle.main(), out.getvalue()

    def test_a_rollback_restores_the_titles_and_a_second_one_changes_nothing(self):
        code, out = self.retitle('--no-pdf', '--apply')
        self.assertEqual(code, 0, out)
        tsv = out.split('rollback: ')[1].split()[0]
        self.assertEqual(self.titles(), {1: 'UM10503 LPC43xx manual', 2: 'UM10503 LPC43xx guide'})
        self.assert_pdfs_on_disk()
        self.calibredb('set_metadata', '--field', 'title:UM10503 LPC43xx guide v2', '2')
        code, out = self.retitle('--rollback', tsv, '--apply')
        self.assertEqual(code, 1, out)
        self.assertIn('#2 withheld: retitled since', out)
        restored = {1: 'LPC43xx manual (UM10503)', 2: 'UM10503 LPC43xx guide v2'}
        self.assertEqual(self.titles(), restored)
        self.assert_pdfs_on_disk()
        code, out = self.retitle('--rollback', tsv, '--apply')
        self.assertIn('0 titles to restore, 1 already restored', out)
        self.assertNotIn('rollback: ', out)
        self.assertEqual(self.titles(), restored)
        self.assert_pdfs_on_disk()


class GuiDetection(unittest.TestCase):
    """Only the Calibre GUI holds the library; its workers and other tools do not."""

    def test_the_process_list_is_read_on_this_os(self):
        # It once walked /proc on Windows too, and every sync.py command died in blockers().
        # Only its shape: a PID-namespaced container may show no other process.
        listed = list(doclib.Library._cmdlines())
        self.assertIsInstance(listed, list)
        self.assertTrue(all(argv and isinstance(argv[0], str) for argv in listed), listed[:3])

    @unittest.skipUnless(os.name == 'nt', 'tasklist is how Windows lists its processes')
    def test_a_process_list_tasklist_cannot_give_is_a_blocker_not_no_gui(self):
        def missing(argv, **kw):
            raise FileNotFoundError(2, 'The system cannot find the file specified', 'tasklist')
        runs = {'no tasklist': missing,
                'exit 1': lambda argv, **kw: subprocess.CompletedProcess(argv, 1, '', 'ERROR: Access denied.'),
                'ERROR line': lambda argv, **kw: subprocess.CompletedProcess(argv, 0, 'ERROR: Invalid query\r\n', '')}
        lib = doclib.Library.__new__(doclib.Library)
        lib.server = ('http://localhost:8080', 'u', 'pw')  # even when an open GUI would not block
        for name, run in runs.items():
            with self.subTest(name), unittest.mock.patch.object(doclib.subprocess, 'run', run):
                blockers = lib.blockers()
                self.assertEqual(len(blockers), 1, blockers)
                self.assertIn('cannot list processes to check for the Calibre GUI', blockers[0])
                with self.assertRaisesRegex(RuntimeError, 'cannot list processes'):
                    doclib.Library._gui_running()

    @unittest.skipUnless(os.name == 'nt', 'tasklist is how Windows lists its processes')
    def test_on_windows_the_gui_is_calibre_exe_alone(self):
        rows = {'calibre.exe': True, 'CALIBRE.EXE': True, 'calibre-parallel.exe': False,
                'calibredb.exe': False, 'calibre-server.exe': False}
        for image, gui in rows.items():
            out = f'"System","4","Services","0","144 K"\r\n"{image}","4242","Console","1","90,000 K"\r\n'
            with self.subTest(image), unittest.mock.patch.object(
                    doclib.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, '')):
                self.assertEqual(doclib.Library._gui_running(), gui)

    GUI = [['/usr/bin/python3.13', '/usr/bin/calibre']]
    WORKER = [['/usr/bin/python3.13', '/usr/bin/calibre-parallel', '--pipe-worker', 'from calibre.utils.ipc.pool ...']]

    def running(self, *cmdlines):
        saved = doclib.Library.__dict__['_cmdlines']
        self.addCleanup(lambda: setattr(doclib.Library, '_cmdlines', saved))
        doclib.Library._cmdlines = staticmethod(lambda: iter(cmdlines))

    def test_the_gui_is_its_launcher_however_it_was_started(self):
        for argv in (['/usr/bin/python3.13', '/usr/bin/calibre'], ['/usr/bin/python3.14', '/usr/bin/calibre', '--detach'],
                     ['/usr/bin/python3', '/usr/local/bin/calibre'], ['/opt/calibre/calibre']):
            with self.subTest(argv):
                self.running(argv)
                self.assertTrue(doclib.Library._gui_running())

    def test_workers_tools_and_mentions_are_not_the_gui(self):
        for argv in (*self.WORKER, ['/opt/calibre/calibre-parallel'], ['/usr/bin/python3.13', '/usr/bin/calibre-server'],
                     ['/usr/bin/python3.13', '/usr/bin/calibredb', 'list'], ['/bin/bash', '-c', '/usr/bin/calibre'],
                     ['/usr/bin/python3', '-c', 'import os; os.system("/usr/bin/calibre")'], ['/usr/bin/python3.13']):
            with self.subTest(argv):
                self.running(argv)
                self.assertFalse(doclib.Library._gui_running())

    def test_blockers_name_the_gui_only_when_it_is_open(self):
        lib = doclib.Library.__new__(doclib.Library)
        lib.server = None
        self.running(*self.WORKER)
        self.assertEqual(lib.blockers(), [])
        self.running(*self.GUI, *self.WORKER)
        self.assertEqual(len(lib.blockers()), 1)
        self.assertIn('GUI is open', lib.blockers()[0])
        self.running(['/usr/bin/FreeFileSync_x86_64', '/home/u/calibre-library.ffs_batch'])
        self.assertIn('FreeFileSync', lib.blockers()[0])

    def lock_then(self, results, server=None, path=Path('/lib')):
        """A Library whose calibredb answers each call from `results` (a lock error, a
        ('fail', stderr) or output)."""
        calls = []

        def run(argv, capture_output, text):
            calls.append(argv)
            out = results.pop(0)
            if isinstance(out, tuple):
                return subprocess.CompletedProcess(argv, 1, '', out[1])
            return subprocess.CompletedProcess(argv, 1 if out == 'lock' else 0, out, 'Another calibre program is running' if out == 'lock' else '')
        saved = (doclib.subprocess.run, doclib.time.sleep)
        self.addCleanup(lambda: (setattr(doclib.subprocess, 'run', saved[0]), setattr(doclib.time, 'sleep', saved[1])))
        doclib.subprocess.run = run
        doclib.time.sleep = lambda s: None
        lib = doclib.Library.__new__(doclib.Library)
        lib.path, lib.server, lib._url = path, server, None
        return lib, calls

    def gui_on(self, library):
        """The GUI has `library` open, as calibre's prefs record it."""
        patch = unittest.mock.patch.object(doclib.Library, '_gui_library', staticmethod(lambda: str(library)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_lock_with_only_a_worker_running_is_retried(self):
        self.running(*self.WORKER)
        lib, calls = self.lock_then(['lock', 'ok'])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lib._run('list'), 'ok')
        self.assertEqual(len(calls), 2)

    def test_a_lock_with_the_gui_open_is_not_retried(self):
        self.running(*self.GUI)
        self.gui_on('/lib')
        lib, calls = self.lock_then(['lock'])
        with self.assertRaisesRegex(RuntimeError, 'GUI is open'):
            lib._run('list')
        lib, calls = self.lock_then(['lock', 'via server'], server=('http://localhost:8080/#lib', 'u', 'pw'))
        self.assertEqual(lib._run('list'), 'via server')
        self.assertEqual(calls[1][2], 'http://localhost:8080/#lib')

    def secret(self, server_url=None):
        """Content-server credentials in a secret.yml -> a scratch library path."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        url = f'  server_url: {server_url}\n' if server_url else ''
        (Path(tmp.name) / 'secret.yml').write_text(f'calibre:\n  username: u\n  password: pw\n{url}',
                                                   encoding='utf-8')
        env = unittest.mock.patch.dict(os.environ, {'DOWNLOAD_DOC_SECRET': str(Path(tmp.name) / 'secret.yml')})
        env.start()
        self.addCleanup(env.stop)
        return Path(tmp.name) / 'My Library'

    def test_the_content_server_is_used_only_for_the_library_the_gui_has_open(self):
        # The GUI's lock refuses calibredb on every library, so a scratch library run
        # with the GUI open must stop, not read and write the GUI's library instead.
        self.running(*self.GUI)
        scratch = self.secret()
        self.gui_on(doclib.DEFAULT_LIBRARY)
        lib = doclib.Library(scratch)
        self.assertIn(f'GUI is open on {doclib.DEFAULT_LIBRARY}, not {scratch}', lib.blockers()[0])
        lib, calls = self.lock_then(['lock'], server=lib.server, path=scratch)
        with self.assertRaisesRegex(RuntimeError, 'GUI is open'):
            lib._run('add', 'x.pdf')
        self.assertEqual([argv[2] for argv in calls], [str(scratch)])

    def test_a_server_url_without_a_library_id_is_refused_whatever_the_server_lists(self):
        # calibre 8.5 lists only the ids an account may see: GUI library /gui/My Library is
        # My_Library, /other/My_Library is My_Library1, and an account allowed only the
        # latter sees a lone My_Library1, which is not the GUI's library.
        self.running(*self.GUI)
        target = self.secret()
        self.gui_on(target)
        for ids in ('My_Library1\n', 'My_Library\n', 'My_Library\nMy_Library1\n', ''):
            with self.subTest(ids):
                lib, calls = self.lock_then(['lock', ids, ids], server=doclib.Library(target).server, path=target)
                with self.assertRaisesRegex(RuntimeError, r"server_url names no library id; set it to "
                                                          r"http://localhost:8080/#<id> .*ids it shows this account: "
                                                          + (', '.join(ids.split()) or 'none')):
                    lib._run('add', 'x.pdf')
                self.assertEqual([argv[2] for argv in calls], [str(target), 'http://localhost:8080/#-'])
                self.assertIn('server_url names no library id', lib.blockers()[0])
        self.secret(server_url='http://localhost:8080/#My_Library1')
        lib, calls = self.lock_then(['lock', 'via server'], server=doclib.Library(target).server, path=target)
        self.assertEqual(lib._run('add', 'x.pdf'), 'via server')
        self.assertEqual(calls[1][2], 'http://localhost:8080/#My_Library1')

    def test_an_id_the_server_does_not_serve_is_refused_not_retried_without_it(self):
        self.running(*self.GUI)
        self.gui_on('/lib')
        lib, calls = self.lock_then(['lock', ('fail', 'Not Found')], server=('http://localhost:8080/#lib', 'u', 'pw'))
        with self.assertRaisesRegex(RuntimeError, r"http://localhost:8080/#lib serves no library by that id; "
                                                  r".*'http://localhost:8080/#-' list"):
            lib._run('add', 'x.pdf')
        self.assertEqual([argv[2] for argv in calls], ['/lib', 'http://localhost:8080/#lib'])


class LibraryCalls(unittest.TestCase):
    """A failed calibredb call is reported, never read as an empty answer."""

    def calibredb(self, fails):
        """calibredb that fails the `fails` command and adds every book as #5."""
        def run(argv, **kw):
            cmd = argv[3]
            if cmd == fails:
                return subprocess.CompletedProcess(argv, 1, '', 'database read failed')
            return subprocess.CompletedProcess(argv, 0, 'Added book ids: 5' if cmd == 'add' else '', '')
        patch = unittest.mock.patch.object(doclib.subprocess, 'run', run)
        patch.start()
        self.addCleanup(patch.stop)
        lib = doclib.Library.__new__(doclib.Library)
        lib.path, lib.server = Path('/lib'), None
        return lib

    def test_a_failed_legacy_read_raises(self):
        # An empty legacy index lets a hand-filed book be imported a second time.
        lib = self.calibredb(fails='list')
        with self.assertRaisesRegex(RuntimeError, 'database read failed'):
            lib.legacy_index(vendor_ti.AUTHOR)

    def test_a_book_added_without_its_metadata_is_warned_and_marked(self):
        lib = self.calibredb(fails='set_metadata')
        d = doclib.Doc(vendor='ti', doc_id='SBOS548', doc_type='datasheet', version='C',
                       title='INA3221', url='https://ti.com/x.pdf', author=vendor_ti.AUTHOR)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(lib.add(d, Path('x.pdf')), 5)
        self.assertEqual(lib.last_add_status, 'added-no-metadata')
        self.assertIn('book #5 filed without comments/revision: database read failed', err.getvalue())


class LastModified(unittest.TestCase):
    def head(self, run):
        saved = doclib.subprocess.run
        self.addCleanup(setattr, doclib.subprocess, 'run', saved)
        doclib.subprocess.run = run
        with contextlib.redirect_stderr(io.StringIO()) as err:
            return doclib.last_modified('https://example.com/x.pdf'), err.getvalue()

    def test_the_head_request_carries_an_accept_encoding_the_cdn_requires(self):
        def run(argv, **kw):
            out = '200 Wed, 28 Oct 2020 20:09:52 GMT' if '--compressed' in argv else '403 '
            return subprocess.CompletedProcess(argv, 0, out, '')
        self.assertEqual(self.head(run), ('Wed, 28 Oct 2020 20:09:52 GMT', ''))

    def test_the_body_is_discarded_to_this_os_null_device(self):
        # curl cannot write "/dev/null" on Windows: exit 23, and no revision ever.
        seen = []

        def run(argv, **kw):
            seen.append(argv[argv.index('-o') + 1])
            return subprocess.CompletedProcess(argv, 0, '200 Wed, 28 Oct 2020 20:09:52 GMT', '')
        self.head(run)
        self.assertEqual(seen, [os.devnull])

    def test_a_missing_wget_falls_through_to_curl(self):
        def run(argv, **kw):
            if argv[0] == 'wget':
                raise FileNotFoundError(2, 'No such file', 'wget')
            return subprocess.CompletedProcess(argv, 0, b'{"ok": 1}\n200', b'')
        with unittest.mock.patch.object(doclib.subprocess, 'run', run):
            self.assertEqual(doclib.get_json('https://example.com/x.json'), {'ok': 1})

    def test_a_missing_last_modified_names_the_final_status(self):
        for out, code, why in (('403 ', 0, 'HTTP 403'),
                               ('200 ', 0, 'HTTP 200'),
                               ('404 Mon, 01 Jan 2024 00:00:00 GMT', 0, 'HTTP 404'),
                               ('000 ', 28, 'curl exit 28'),
                               ('301 ', 6, 'curl exit 6')):
            with self.subTest(why):
                version, err = self.head(lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, ''))
                self.assertIsNone(version)
                self.assertIn(f'no Last-Modified from https://example.com/x.pdf ({why})', err)


class HttpGet(unittest.TestCase):
    """Only the server's 404 or 410 says a document is missing; any other failure proves nothing."""

    def get(self, curl_out, curl_exit=0):
        """http_get for a PDF with wget failing and curl printing `curl_out` (body, then its -w status)."""
        def run(argv, **kw):
            if argv[0] == 'wget':
                return subprocess.CompletedProcess(argv, 8, b'', b'')
            return subprocess.CompletedProcess(argv, curl_exit, curl_out, b'')
        with unittest.mock.patch.object(doclib.subprocess, 'run', run):
            return doclib.http_get('https://example.com/x.pdf', validate=lambda b: b.startswith(b'%PDF'))

    def test_a_pdf_comes_back_without_the_status(self):
        self.assertEqual(self.get(b'%PDF-1.7\nbody\n200'), b'%PDF-1.7\nbody')

    def test_only_a_completed_404_or_410_is_a_missing_document(self):
        for out, code, missing in ((b'<html>gone</html>\n404', 0, True),
                                   (b'<html>gone</html>\n410', 0, True),
                                   (b'%PDF-1.7 not the document\n404', 0, True),
                                   (b'\n301', 0, False),
                                   (b'<html>busy</html>\n503', 0, False),
                                   (b'<html>login</html>\n200', 0, False),
                                   (b'\n000', 28, False),
                                   (b'<html>gone</html>\n404', 56, False)):
            with self.subTest(out=out, code=code), self.assertRaises(RuntimeError) as raised:
                self.get(out, code)
            self.assertEqual(isinstance(raised.exception, doclib.NotFound), missing, out)


class MicrochipNumber(unittest.TestCase):
    """The title leads with the number the filename carries; the identifier keeps the stem."""

    def test_the_adapter_titles_by_the_printed_number(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = 'https://ww1.microchip.com/downloads/aemDocuments/documents/UNG/ProductDocuments'
        cache = Path(tmp.name) / 'docs.txt'
        cache.write_text(f'{base}/DataSheets/USB251xB-xBi-Data-Sheet-DS00001692.pdf\t2024-10-04\n'
                         f'{base}/DataSheets/USB2517-USB2517i-Data-Sheet-00001598C.pdf\t2024-05-23\n'
                         f'{base}/UserGuides/EVB-USB2514BCEvaluationBoardUsersGuide_A_0p4.pdf\t2025-11-20\n')
        saved = vendor_microchip.CACHE
        self.addCleanup(lambda: setattr(vendor_microchip, 'CACHE', saved))
        vendor_microchip.CACHE = cache
        got = {d.ident: d.calibre_title() for d in vendor_microchip.enumerate_docs()}
        self.assertEqual(got, {
            'microchip:USB251xB-xBi-Data-Sheet-DS00001692': 'DS00001692 USB251xB xBi Data Sheet',
            'microchip:USB2517-USB2517i-Data-Sheet-00001598C':
                'DS00001598 USB2517 USB2517i Data Sheet 00001598C',
            'microchip:EVB-USB2514BCEvaluationBoardUsersGuide_A_0p4':
                'EVB USB2514BCEvaluationBoardUsersGuide A 0p4 — User manual '
                '(EVB-USB2514BCEvaluationBoardUsersGuide_A_0p4)',
        })


class TiParts(unittest.TestCase):
    """Named parts replace the BSP scan; a named part with no datasheet is an error."""

    def setUp(self):
        saved = (vendor_ti.http_get, vendor_ti.last_modified, vendor_ti._parts_from_bsp)
        self.addCleanup(lambda: setattr_all(saved))
        vendor_ti.last_modified = lambda url: 'Fri, 27 Mar 2026 05:34:41 GMT'
        vendor_ti._parts_from_bsp = lambda: self.fail('BSP scanned despite named parts')

    def serve(self, *present):
        def http_get(url, **kw):
            if not any(url.endswith(f'/{p}.pdf') for p in present):
                raise doclib.NotFound('404')
            return b'%PDF'
        vendor_ti.http_get = http_get

    def test_named_parts_replace_the_bsp_list(self):
        self.serve('ina3221', 'tca9548a')
        docs = vendor_ti.enumerate_docs(parts=['INA3221', 'tca9548a'])
        self.assertEqual([(d.ident, d.url, d.family) for d in docs], [
            ('ti:INA3221', 'https://www.ti.com/lit/ds/symlink/ina3221.pdf', ['INA3221']),
            ('ti:TCA9548A', 'https://www.ti.com/lit/ds/symlink/tca9548a.pdf', ['TCA9548A'])])

    def test_a_named_part_without_a_datasheet_fails(self):
        self.serve('ina3221')
        with self.assertRaisesRegex(SystemExit, 'ina32211'):
            vendor_ti.enumerate_docs(parts=['ina3221', 'ina32211'])


class LetterSubRevisions(unittest.TestCase):
    """Arm labels an issue by letter and its re-releases by a second letter: B.y, B.z, C."""

    def test_a_minor_letter_orders_between_its_issue_and_the_next(self):
        self.assertEqual(doclib.compare_rev('B.z', 'B.y')[0], 'newer')
        self.assertEqual(doclib.compare_rev('E.e', 'E')[0], 'newer')
        self.assertEqual(doclib.compare_rev('C', 'B.z')[0], 'newer')
        self.assertEqual(doclib.compare_rev('B.z', 'B.z')[0], 'current')


class SelfTests(unittest.TestCase):
    """The fixtures beside the revision and title code: compare_rev decides what is replaced."""

    def run_script(self, module):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runpy.run_path(module.__file__, run_name='__main__')
        return out.getvalue()

    def test_doclib(self):
        self.assertIn('doclib self-test OK', self.run_script(doclib))

    def test_titles(self):
        out = self.run_script(titles)
        self.assertTrue(out.rstrip().endswith('ALL PASS'), out)


class ArmCatalogue(unittest.TestCase):
    """Each listed document resolves through Arm's documentation service to its PDF."""

    def setUp(self):
        saved = vendor_arm.get_json
        self.addCleanup(lambda: setattr(vendor_arm, 'get_json', saved))

    def serve(self, resources):
        def get_json(url, **kw):
            code = url.split('/documentation/')[1].split('/')[0]
            return {'document': code, 'title': f'{code} title', 'versionLabel': 'B.z',
                    '_links': {'resources': resources(code)}}
        vendor_arm.get_json = get_json

    def test_a_document_resolves_to_its_pdf_and_revision(self):
        self.serve(lambda code: [
            {'href': f'https://documentation-service.arm.com/static/{code}x?token=',
             'name': f'{code}.xlsx', 'extension': 'xlsx'},
            {'href': f'https://documentation-service.arm.com/static/{code}?token=',
             'name': f'{code.upper()}B_z.pdf', 'extension': 'pdf'}])
        d = next(d for d in vendor_arm.enumerate_docs() if d.doc_id == 'DDI0553')
        self.assertEqual((d.ident, d.version, d.url, d.author, d.aliases), (
            'arm:DDI0553', 'B.z', 'https://documentation-service.arm.com/static/ddi0553?token=',
            'ARM Limited', ['ddi0553 title']))

    def test_a_document_without_a_pdf_fails(self):
        self.serve(lambda code: [])
        with self.assertRaisesRegex(SystemExit, 'ddi0419 has no PDF'):
            vendor_arm.enumerate_docs()


def pdf_with(text):
    """A one-page PDF whose page text pdftotext reads back as `text`."""
    objs = ['<</Type/Catalog/Pages 2 0 R>>', '<</Type/Pages/Kids[3 0 R]/Count 1>>',
            '<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>'
            '/Contents 5 0 R>>',
            '<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>']
    stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'
    objs.append(f'<</Length {len(stream)}>>\nstream\n{stream}\nendstream')
    out = '%PDF-1.4\n%' + 'x' * 1100 + '\n'    # past the size floor a download must clear
    offsets = []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f'{n} 0 obj\n{obj}\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'
    out += ''.join(f'{o:010d} 00000 n \n' for o in offsets)
    out += f'trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n'
    return out.encode('latin-1')


def patch_library(test):
    """Route Library's calibredb calls to test.library, looked up per call so a test may
    swap it, with nothing blocking and no content server."""
    saved = (doclib.Library._run, doclib.Library.blockers, doclib.Library.__dict__['_server_creds'])

    def restore():
        doclib.Library._run, doclib.Library.blockers, doclib.Library._server_creds = saved
    test.addCleanup(restore)
    doclib.Library._run = lambda lib, *a, **k: test.library.run(*a, **k)
    doclib.Library.blockers = lambda lib: []
    doclib.Library._server_creds = staticmethod(lambda: None)


class FakeCalibre:
    """calibredb over an in-memory book list, taking the argv Library passes it.
    `fail` maps a command to the error it raises; `added` keeps each added file's bytes."""

    def __init__(self, *books):
        self.books = [dict(b) for b in books]
        self.writes = []
        self.fail = {}
        self.added = []
        self.next_id = max((b['id'] for b in books), default=0) + 1

    def run(self, *args, tries=4):
        if args[0] in self.fail:
            raise RuntimeError(self.fail[args[0]])
        if args[0] == 'list':
            books = self.books
            if '--search' in args:
                author = args[args.index('--search') + 1].split('"')[1]
                books = [b for b in books if b['authors'] == author]
            return json.dumps(books)
        self.writes.append(args[0])
        if args[0] == 'add':
            opt = dict(zip(args[1:-1:2], args[2:-1:2]))
            scheme, code = opt['-I'].split(':', 1)
            self.added.append(Path(args[-1]).read_bytes())
            book_id, self.next_id = self.next_id, self.next_id + 1
            self.books.append({'id': book_id, 'title': opt['-t'], 'authors': opt['-a'],
                               'tags': opt['-T'].split(','), 'identifiers': {scheme: code},
                               'comments': ''})
            return f'Added book ids: {book_id}'
        if args[0] == 'set_metadata':
            book = next(b for b in self.books if b['id'] == int(args[-1]))
            for spec in args[2:-1:2]:
                name, value = spec.split(':', 1)
                book[name] = value
            return ''
        if args[0] == 'remove':
            book = next(b for b in self.books if b['id'] == int(args[1]))
            for path in book.get('formats', []):
                Path(path).unlink(missing_ok=True)
            self.books.remove(book)
            return ''
        raise AssertionError(f'unexpected calibredb {args}')


@unittest.skipUnless(shutil.which('pdftotext'), 'page 1 is read with pdftotext')
class PageOneText(unittest.TestCase):
    """Page 1 reads back as UTF-8 whichever pdftotext is on PATH: Git for Windows'
    xpdf writes Latin-1 unless asked, and one undecodable byte lost the whole page."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf = Path(tmp.name) / 'es.pdf'
        # StandardEncoding: 0xD0 is an em dash, 0xA8 a currency sign, 0xE8 Ł, whose UTF-8
        # 0xC5 0x81 a cp1252 locale cannot decode either
        self.pdf.write_bytes(pdf_with('ES0182 Rev 3 \xd0 STM32\xa8 \xe8 errata'))

    def test_page1_text_keeps_the_page_and_its_non_ascii_text(self):
        self.assertIn('ES0182 Rev 3 — STM32¤ Ł errata', doclib.page1_text(self.pdf))

    def test_retitle_reads_the_number_off_such_a_page(self):
        self.assertEqual(retitle.from_pdf(self.pdf), ('ES0182', 'both', None))


@unittest.skipUnless(shutil.which('pdftotext'), 'identity checks read the PDF with pdftotext')
class AddOne(unittest.TestCase):
    """`sync.py add` files one document the way an adapter's import would, or refuses."""

    URL = 'https://example.com/ps.pdf'

    def setUp(self):
        self.library = FakeCalibre()
        self.served = pdf_with('nRF52820 Product Specification PS1234 v1.4')
        self.fetched = []
        patch_library(self)
        saved = doclib.http_get
        self.addCleanup(lambda: setattr(doclib, 'http_get', saved))
        doclib.http_get = self.http_get

    def http_get(self, url, *a, **k):
        self.fetched.append(url)
        return self.served

    def add(self, *args, doc_id='PS1234', source=('--url', URL)):
        argv = ['--vendor', 'nordic', '--author', 'Nordic Semiconductor', '--id', doc_id,
                '--type', 'datasheet', '--title', 'nRF52820 Product Specification',
                *source, '--revision', '1.4', '--family', 'nRF52', *args]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.add_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_dry_run_prints_the_plan_and_changes_nothing(self):
        code, out, err = self.add()
        self.assertEqual(code, 0, err)
        for line in ('ADD nordic:PS1234', 'author      Nordic Semiconductor',
                     'title       PS1234 nRF52820 Product Specification — Datasheet Rev 1.4',
                     'tags        datasheet, nordic, nRF52', 'identity ok',
                     'Document ID: PS1234', 'Revision: 1.4', f'Source: {self.URL}'):
            self.assertIn(line, out)
        self.assertIn('dry run', err)
        self.assertEqual((self.fetched, self.library.writes), ([self.URL], []))

    def test_apply_imports_under_the_conventions(self):
        code, out, err = self.add('--apply')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.library.books, [{
            'id': 1, 'title': 'PS1234 nRF52820 Product Specification — Datasheet Rev 1.4',
            'authors': 'Nordic Semiconductor', 'tags': ['datasheet', 'nordic', 'nRF52'],
            'identifiers': {'nordic': 'PS1234'}, 'publisher': 'Nordic Semiconductor',
            'comments': f'\n\nDocument ID: PS1234\nRevision: 1.4\nSource: {self.URL}'}])
        self.assertIn('IMPORTED #1 nordic:PS1234', out)
        self.assertIn('locate.py build --all', out)

    def test_an_identifier_already_filed_is_refused_before_any_download(self):
        self.library = FakeCalibre({'id': 7, 'title': 'PS1234 old', 'authors': 'Nordic Semiconductor',
                                    'identifiers': {'nordic': 'ps1234'}, 'comments': 'Revision: 1.3'})
        code, _, err = self.add('--apply')
        self.assertEqual(code, 1)
        self.assertIn('already book #7', err)
        self.assertIn('`sync.py refresh` with its newer revision replaces it', err)
        self.assertEqual((self.fetched, self.library.writes), ([], []))

    def test_a_second_spelling_of_the_vendor_is_refused(self):
        self.library = FakeCalibre({'id': 3, 'title': 'other', 'authors': 'Nordic Semiconductor',
                                    'identifiers': {'nordicsemi': 'X'}, 'comments': ''})
        code, _, err = self.add('--apply')
        self.assertEqual(code, 1)
        self.assertIn('already files its books as nordicsemi:', err)

    def test_a_download_that_is_not_a_pdf_is_refused(self):
        self.served = b'<!doctype html><html>sign in</html>' + b' ' * 1024
        code, _, err = self.add('--apply')
        self.assertEqual(code, 1)
        self.assertIn('login page', err)
        self.assertEqual(self.library.writes, [])

    def test_a_local_file_that_is_not_a_pdf_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        page = Path(tmp.name) / 'ps.pdf'
        page.write_bytes(b'<html>sign in</html>')
        code, _, err = self.add('--apply', source=('--pdf', str(page), '--source', self.URL))
        self.assertEqual(code, 1)
        self.assertIn('is not a PDF', err)
        self.assertEqual(self.library.writes, [])

    def test_the_wrong_document_is_refused(self):
        self.served = pdf_with('DS9999 Rev 2 nRF52820 Product Specification')
        code, _, err = self.add('--apply', doc_id='DS1234')
        self.assertEqual(code, 1)
        self.assertIn('served DS9999, not DS1234', err)
        self.assertEqual(self.library.writes, [])

    def test_an_id_the_document_does_not_print_is_refused_unless_declared(self):
        code, _, err = self.add('--apply', doc_id='nRF52820-datasheet')
        self.assertEqual((code, self.library.writes), (1, []))
        self.assertIn('--id-not-printed', err)
        code, out, err = self.add('--id-not-printed', doc_id='nRF52820-datasheet')
        self.assertEqual(code, 0, err)
        self.assertIn('title       nRF52820 Product Specification — Datasheet (nRF52820-datasheet) Rev 1.4', out)
        self.assertIn('identity skipped', out)

    def test_a_document_that_neither_confirms_nor_contradicts_the_id_is_refused(self):
        self.served = pdf_with('Some unrelated application note without a number')
        code, _, err = self.add('--apply', doc_id='DS1234')
        self.assertEqual((code, self.library.writes), (1, []))
        self.assertIn('do not confirm DS1234', err)
        self.assertIn('--id-not-printed', err)

    def test_a_vendor_without_an_adapter_needs_an_author(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            sync.add_main(['--vendor', 'nordic', '--id', 'PS1234', '--type', 'datasheet',
                           '--title', 't', '--url', self.URL])
        self.assertIn('--author is required', err.getvalue())


def setattr_all(saved):
    vendor_ti.http_get, vendor_ti.last_modified, vendor_ti._parts_from_bsp = saved



class RemoveOne(unittest.TestCase):
    """`sync.py remove` moves one book, by its Calibre id, to Calibre's Recycle Bin."""

    def setUp(self):
        self.library = FakeCalibre({'id': 3, 'title': 'J-Link / J-Trace Getting Started',
                                    'authors': 'SEGGER Microcontroller GmbH'})
        patch_library(self)

    def remove(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.remove_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_dry_run_names_the_book_and_changes_nothing(self):
        code, out, err = self.remove('3')
        self.assertEqual(code, 0, err)
        self.assertIn('REMOVE #3 J-Link / J-Trace Getting Started', out)
        self.assertIn('identifiers none', out)
        self.assertIn('dry run', err)
        self.assertEqual(self.library.writes, [])

    def test_apply_removes_it_to_the_recycle_bin(self):
        code, out, err = self.remove('3', '--apply')
        self.assertEqual(code, 0, err)
        self.assertEqual((self.library.books, self.library.writes), ([], ['remove']))
        self.assertIn('REMOVED #3', out)
        self.assertIn("moved to Calibre's Recycle Bin", out)

    def test_an_unknown_id_is_refused(self):
        code, _, err = self.remove('9', '--apply')
        self.assertEqual(code, 1)
        self.assertIn('no book #9', err)
        self.assertEqual(self.library.writes, [])

    def test_a_failed_remove_is_reported(self):
        self.library.fail['remove'] = 'permission denied'
        code, _, err = self.remove('3', '--apply')
        self.assertEqual(code, 1)
        self.assertIn('book #3: calibredb remove failed (permission denied)', err)


@unittest.skipUnless(shutil.which('pdftotext'), 'identity checks read the PDF with pdftotext')
class RefreshOne(unittest.TestCase):
    """`sync.py refresh` replaces the one book carrying an identifier with a newer revision."""

    URL = 'https://example.com/ps.pdf'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.old_pdf = self.root / 'lib' / 'ps.pdf'
        self.old_pdf.parent.mkdir()
        self.old_pdf.write_bytes(b'%PDF old')
        self.library = FakeCalibre(self.filed())
        self.served = pdf_with('nRF52820 Product Specification PS1234 v1.5')
        self.fetched = []
        patch_library(self)
        saved = doclib.http_get
        self.addCleanup(lambda: setattr(doclib, 'http_get', saved))
        doclib.http_get = self.http_get

    def filed(self, book_id=7, rev='1.4', code='PS1234'):
        return {'id': book_id, 'title': f'PS1234 nRF52820 Product Specification — Datasheet Rev {rev}',
                'authors': 'Nordic Semiconductor', 'identifiers': {'nordic': code},
                'comments': f'Revision: {rev}', 'formats': [str(self.old_pdf)]}

    def http_get(self, url, *a, **k):
        self.fetched.append(url)
        return self.served

    def refresh(self, *args, revision='1.5', source=('--url', URL)):
        argv = ['--vendor', 'nordic', '--author', 'Nordic Semiconductor', '--id', 'PS1234',
                '--type', 'datasheet', '--title', 'nRF52820 Product Specification',
                *source, '--revision', revision, '--family', 'nRF52', *args]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.refresh_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_dry_run_plans_the_replacement_and_changes_nothing(self):
        code, out, err = self.refresh()
        self.assertEqual(code, 0, err)
        for line in ('REFRESH #7 nordic:PS1234 (1.4 -> 1.5)',
                     'was         PS1234 nRF52820 Product Specification — Datasheet Rev 1.4',
                     'title       PS1234 nRF52820 Product Specification — Datasheet Rev 1.5', 'identity ok'):
            self.assertIn(line, out)
        self.assertIn('dry run', err)
        self.assertEqual((self.fetched, self.library.writes), ([self.URL], []))

    def test_apply_removes_and_adds(self):
        code, out, err = self.refresh('--apply')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.library.writes, ['remove', 'add', 'set_metadata'])
        self.assertEqual([b['id'] for b in self.library.books], [8])
        self.assertEqual(self.library.books[0]['title'], 'PS1234 nRF52820 Product Specification — Datasheet Rev 1.5')
        self.assertIn('REFRESHED #7 -> #8 nordic:PS1234', out)

    def test_a_local_pdf_inside_the_removed_book_is_still_added(self):
        self.old_pdf.write_bytes(self.served)
        code, _, err = self.refresh('--apply', source=('--pdf', str(self.old_pdf), '--source', self.URL))
        self.assertEqual(code, 0, err)
        self.assertFalse(self.old_pdf.exists())
        self.assertEqual(self.library.added, [self.served])

    def test_the_same_revision_is_nothing_to_do_and_downloads_nothing(self):
        code, out, _ = self.refresh('--apply', revision='1.4')
        self.assertEqual(code, 0)
        self.assertIn('nothing to refresh', out)
        self.assertEqual((self.fetched, self.library.writes), ([], []))

    def test_an_unreadable_local_revision_is_refreshed(self):
        self.library = FakeCalibre({**self.filed(), 'title': 'PS1234 nRF52820', 'comments': ''})
        code, out, err = self.refresh()
        self.assertEqual(code, 0, err)
        self.assertIn('local revision None unreadable', out)

    def test_an_older_revision_is_nothing_to_do_and_an_incomparable_one_is_refused(self):
        for revision, expected, said in (('1.3', 0, 'nothing to refresh'),
                                         ('2026-01-05', 1, 'cannot tell Rev 2026-01-05 is newer')):
            with self.subTest(revision):
                code, out, err = self.refresh('--apply', revision=revision)
                self.assertEqual(code, expected)
                self.assertIn(said, out + err)
        self.assertEqual((self.fetched, self.library.writes), ([], []))

    def test_no_book_or_several_books_are_refused(self):
        for books, said in (((), 'not in the library — use `sync.py add`'),
                            ((self.filed(), self.filed(9)), 'is on 2 books (#7, #9)')):
            with self.subTest(said):
                self.library = FakeCalibre(*books)
                code, _, err = self.refresh('--apply')
                self.assertEqual(code, 1)
                self.assertIn(said, err)
                self.assertEqual((self.fetched, self.library.writes), ([], []))

    def test_a_second_spelling_of_the_author_is_refused(self):
        self.library = FakeCalibre({**self.filed(), 'authors': 'Nordic'})
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn("filed under author 'Nordic'", err)

    def test_two_spellings_of_the_identifier_on_one_book_are_one_book(self):
        self.library = FakeCalibre({**self.filed(), 'identifiers': {'nordic': 'PS1234', 'Nordic': 'ps1234'}})
        code, _, err = self.refresh()
        self.assertEqual(code, 1)
        self.assertNotIn('is on 2 books', err)
        self.assertIn('already files its books as Nordic:', err)  # the second scheme spelling

    def test_a_case_only_difference_in_the_identifier_is_noted(self):
        self.library = FakeCalibre(self.filed(code='ps1234'))
        code, _, err = self.refresh()
        self.assertEqual(code, 0, err)
        self.assertIn('the library spells it nordic:ps1234', err)

    def test_the_wrong_document_is_refused_and_nothing_written(self):
        self.served = pdf_with('nRF52833 Product Specification PS9999 v1.5')
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn('PS1234 is not printed on pages 1-2', err)
        self.assertEqual(self.library.writes, [])

    def test_a_blocker_or_an_unreadable_library_stops_it(self):
        doclib.Library.blockers = lambda lib: ['The Calibre GUI is open']
        self.assertEqual(self.refresh('--apply')[0], 2)
        doclib.Library.blockers = lambda lib: []
        self.library.fail['list'] = 'database locked'
        code, _, err = self.refresh()
        self.assertEqual(code, 2)
        self.assertIn('cannot read the library', err)

    def test_a_book_changed_since_the_plan_is_left_alone(self):
        saved = sync.acquire

        def acquire(*a):
            self.library.books[0]['comments'] = 'Revision: 1.5'
            self.library.books[0]['title'] = 'PS1234 nRF52820 Product Specification — Datasheet Rev 1.5'
            return saved(*a)
        sync.acquire = acquire
        self.addCleanup(lambda: setattr(sync, 'acquire', saved))
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn('changed since the plan', err)
        self.assertEqual(self.library.writes, [])

    def test_a_failed_removal_adds_nothing(self):
        self.library.fail['remove'] = 'permission denied'
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn('removal failed, nothing added', err)
        self.assertIn('book #7: calibredb remove failed (permission denied)', err)
        self.assertNotIn('add', self.library.writes)

    def test_a_failed_add_after_the_removal_is_reported(self):
        for error, said in (('disk full', '(disk full)'), ('', '(RuntimeError)')):
            with self.subTest(error=error):
                self.old_pdf.write_bytes(b'%PDF old')
                self.library = FakeCalibre(self.filed())
                self.library.fail['add'] = error
                code, _, err = self.refresh('--apply')
                self.assertEqual(code, 1)
                self.assertIn(f'book #7 was removed but the new copy was not added {said}', err)
                self.assertIn("restore #7 from Calibre's Recycle Bin", err)

    def test_a_standard_identifier_beside_the_vendor_one_is_no_second_spelling(self):
        self.library = FakeCalibre({**self.filed(), 'identifiers': {'nordic': 'PS1234', 'isbn': '9780000000000'}})
        code, _, err = self.refresh()
        self.assertEqual(code, 0, err)

    def test_an_add_that_files_nothing_is_reported(self):
        saved = doclib.Library.add
        self.addCleanup(lambda: setattr(doclib.Library, 'add', saved))

        def add(lib, doc, pdf):
            lib.last_add_status = 'failed'
        doclib.Library.add = add
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn('was removed but the new copy was not added (failed)', err)


def run_sync(*argv):
    """sync.py <argv> -> (exit code, stdout, stderr); a usage error is its exit code."""
    out, err = io.StringIO(), io.StringIO()
    with unittest.mock.patch.object(sys, 'argv', ['sync.py', *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = sync.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class VendorReplace(unittest.TestCase):
    """`sync.py <vendor> --apply` reports a replacement that fails by stage and exits 1."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        doc = doclib.Doc(vendor='ti', doc_id='SBOS548', doc_type='datasheet', version='C',
                         title='INA3221', url='https://ti.com/x.pdf', author=vendor_ti.AUTHOR)
        self.library = FakeCalibre({'id': 4, 'title': 'SBOS548 INA3221 Rev B', 'authors': vendor_ti.AUTHOR,
                                    'identifiers': {'ti': 'SBOS548'}, 'comments': 'Revision: B'})
        new = self.root / 'new.pdf'
        new.write_bytes(b'%PDF new')
        patch_library(self)
        saved = (sync.enumerate_vendor, sync.fetch)

        def restore():
            sync.enumerate_vendor, sync.fetch = saved
        self.addCleanup(restore)
        sync.enumerate_vendor = lambda vendor, args: [doc]
        sync.fetch = lambda d, mod, tmp: (new, 'ok')

    def run_sync(self):
        return run_sync('ti', '--parts', 'ina3221', '--apply')

    def test_a_failed_removal_skips_the_replacement(self):
        self.library.fail['remove'] = 'permission denied'
        code, out, _ = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn('removal failed, nothing added', out)
        self.assertNotIn('add', self.library.writes)

    def test_a_failed_add_after_the_removal_is_reported(self):
        self.library.fail['add'] = 'disk full'
        code, out, _ = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn('book #4 was removed but the new copy was not added (disk full)', out)

    def test_a_replacement_that_succeeds_exits_zero(self):
        code, out, _ = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn('1 replaced', out)

    def test_apply_leaves_no_download_dir_behind(self):
        with unittest.mock.patch.object(tempfile, 'tempdir', str(self.root)):
            self.run_sync()
        self.assertEqual(list(self.root.glob('download-doc-*')), [])

    def test_a_failed_add_of_a_new_document_is_skipped_and_the_batch_goes_on(self):
        docs = [doclib.Doc(vendor='ti', doc_id=f'SBOS{n}', doc_type='datasheet', version='A', title=f'INA{n}',
                           url=f'https://ti.com/{n}.pdf', author=vendor_ti.AUTHOR) for n in (901, 902)]
        fetched = []
        sync.enumerate_vendor = lambda vendor, args: docs
        sync.fetch = lambda d, mod, tmp: (fetched.append(d.doc_id), (self.root / 'new.pdf', 'ok'))[1]
        self.library.fail['add'] = 'content-server write failed: Forbidden'
        code, out, _ = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(fetched, ['SBOS901', 'SBOS902'])
        self.assertIn('IMPORTED  0 new', out)
        for doc_id in fetched:
            self.assertRegex(out, rf'SKIPPED {doc_id} +content-server write failed: Forbidden')

    def test_a_book_added_without_its_metadata_is_listed(self):
        self.library.fail['set_metadata'] = 'database is locked'
        code, out, _ = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn('1 replaced', out)
        self.assertRegex(out, r'ADDED WITHOUT METADATA.*\n +#5 +SBOS548')

    def test_an_unreadable_legacy_index_blocks_before_any_download(self):
        read = self.library.run

        def run(*args, **k):
            if '--search' in args:
                raise RuntimeError('database read failed')
            return read(*args, **k)
        self.library.run = run
        sync.fetch = lambda *a: self.fail('downloaded with the library unread')
        code, _, err = self.run_sync()
        self.assertEqual(code, 2)
        self.assertIn('BLOCKED: cannot read the library: database read failed', err)
        self.assertEqual(self.library.writes, [])


def st_doc(doc_id, family, doc_type='datasheet'):
    return doclib.Doc(vendor='st', doc_id=doc_id, doc_type=doc_type, version='1.0', title=doc_id,
                      url=f'https://st.com/{doc_id}.pdf', author=vendor_st.AUTHOR, family=family)


class VendorSync(unittest.TestCase):
    """`sync.py <vendor>` selects what it was asked for, or says why nothing was selected."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.library = FakeCalibre()
        patch_library(self)
        self.enumerated = []
        self.docs = []
        saved = (sync.enumerate_vendor, doclib.UsbIndex)

        def restore():
            sync.enumerate_vendor, doclib.UsbIndex = saved
        self.addCleanup(restore)
        sync.enumerate_vendor = lambda vendor, args: (self.enumerated.append(vendor), list(self.docs))[1]

    def usb(self, verdicts, resolved=None):
        """A UsbIndex answering get() from `verdicts` {family: usb}; resolve() fills `resolved`."""
        class Usb:
            def hint_from_index(self, docs):
                pass

            def get(self, vendor, fam):
                return None if verdicts.get(fam) is None else {'usb': verdicts[fam], 'source': 's', 'evidence': 'e'}

            def resolve(self, vendor, fam, cands, tmp, complete):
                if resolved is None:
                    return None
                (tmp / f'{fam}.pdf').write_bytes(b'%PDF')
                resolved.append(tmp)
                verdicts[fam] = True
        doclib.UsbIndex = Usb

    def test_a_selector_the_vendor_cannot_honour_is_refused_before_anything_runs(self):
        doclib.Library.blockers = lambda lib: self.fail('touched the library')
        for argv in (('st', '--chips', 'esp32-s3'), ('st', '--device', 'i.MX RT'), ('rpi', '--parts', 'x'),
                     ('nxp', '--family', 'LPC55'), ('espressif', '--family', 'x'), ('microchip', '--refresh'),
                     ('st', '--limit', '0'), ('st', '--limit', '-1')):
            with self.subTest(argv):
                code, _, err = run_sync(*argv)
                self.assertEqual(code, 2, err)
                self.assertEqual(self.enumerated, [])

    def test_a_selector_the_vendor_honours_is_accepted(self):
        for argv in (('st', '--refresh'), ('st', '--family', 'STM32H7'), ('espressif', '--chips', 'esp32-s3'),
                     ('espressif', '--refresh'), ('nxp', '--device', 'i.MX RT'), ('ti', '--parts', 'ina3221'),
                     ('rpi', '--limit', '1')):
            with self.subTest(argv):
                self.enumerated.clear()
                code, _, err = run_sync(*argv)
                self.assertEqual((code, self.enumerated), (1, [argv[0]]), err)

    def test_an_empty_result_is_enumerated_once_and_reported_as_such(self):
        code, _, err = run_sync('rpi', '--family', 'nope')
        self.assertEqual(code, 1)
        self.assertEqual(self.enumerated, ['rpi'])
        self.assertNotIn('throttling', err)

    def test_an_empty_selection_after_the_usb_filter_stops_before_the_library(self):
        self.library.fail['list'] = 'read the library'
        for argv, verdicts in ((('st',), {'STM32F0': False}), (('st', '--usb-only'), {})):
            with self.subTest(argv):
                self.docs = [st_doc('DS1', ['STM32F0'])]
                self.usb(verdicts)
                code, _, err = run_sync(*argv)
                self.assertEqual(code, 1, err)
                self.assertIn('left out: 1 document(s)', err)
                self.assertIn('no documents left after the USB filter', err)

    def test_a_partly_excluded_selection_goes_on_to_the_plan(self):
        self.docs = [st_doc('DS1', ['STM32F0']), st_doc('AN1', [])]
        self.usb({'STM32F0': False})
        code, out, err = run_sync('st')
        self.assertEqual(code, 0, err)
        self.assertIn('NEW       AN1', out)

    def test_peeked_documents_last_only_for_the_run(self):
        self.docs = [st_doc('DS1', ['STM32H7'])]
        resolved = []
        self.usb({}, resolved)
        with unittest.mock.patch.object(Path, 'home', lambda: self.root):
            code, _, err = run_sync('st')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(resolved), 1)
        self.assertFalse(resolved[0].exists())
        self.assertEqual(list(self.root.rglob('*.pdf')), [])

    def adapter(self, vendor):
        """Dispatch through sync's own enumerate_vendor to `vendor`'s adapter, answering self.docs."""
        sync.enumerate_vendor = self.enumerate_vendor
        patch = unittest.mock.patch.object(sync.VENDORS[vendor], 'enumerate_docs', lambda **k: [
            d for d in self.docs if not k.get('types') or d.doc_type in k['types']])
        patch.start()
        self.addCleanup(patch.stop)

    enumerate_vendor = staticmethod(sync.enumerate_vendor)

    def test_a_vendor_that_never_reports_is_not_partial_after_one_that_did(self):
        self.docs = [st_doc('AN1', [])]

        def enumerate_vendor(vendor, args):
            if vendor == 'st':
                doclib.partial_enumeration('SS1: fetch failed')
            return list(self.docs)
        sync.enumerate_vendor = enumerate_vendor
        code, _, err = run_sync('st')
        self.assertEqual(code, 0, err)
        self.assertIn('enumeration incomplete', err)
        code, _, err = run_sync('rpi')
        self.assertEqual(code, 0, err)
        self.assertNotIn('enumeration incomplete', err)

    def test_any_vendor_plans_at_most_the_limit(self):
        self.docs = [st_doc(f'DS{n}', []) for n in range(5)]
        self.adapter('rpi')
        code, out, err = run_sync('rpi', '--limit', '2')
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count('NEW       DS'), 2)

    def test_a_limited_run_does_not_exclude_a_family_from_later_runs(self):
        # Only the fourth document shows USB; a resolver shown three would cache "no USB".
        self.docs = [st_doc(f'DS{n}', ['STM32X0']) for n in range(1, 5)]
        self.adapter('st')
        self.peek_usb_in('DS4')
        code, out, err = run_sync('st', '--limit', '3')
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count('NEW       DS'), 3)
        code, out, err = run_sync('st')
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count('NEW       DS'), 4)
        self.assertNotIn('left out', err)

    def peek_usb_in(self, doc_id):
        """UsbIndex.peek finding USB in doc_id alone, every document readable."""
        def peek(index, doc, tmp):
            index.last_evidence = 'USB OTG' if doc.doc_id == doc_id else 'no USB peripheral found'
            return doc.doc_id == doc_id
        patch = unittest.mock.patch.object(doclib.UsbIndex, 'peek', peek)
        patch.start()
        self.addCleanup(patch.stop)
        home = unittest.mock.patch.object(Path, 'home', lambda: self.root)
        home.start()
        self.addCleanup(home.stop)

    def test_a_narrowed_run_does_not_exclude_a_family_from_later_runs(self):
        # Only the reference manual shows USB; three datasheets alone would read as "no USB".
        self.docs = [*(st_doc(f'DS{n}', ['STM32X0']) for n in range(1, 4)),
                     st_doc('RM1', ['STM32X0'], 'reference-manual')]
        self.adapter('st')
        self.peek_usb_in('RM1')
        code, out, err = run_sync('st', '--types', 'datasheet')
        self.assertEqual((code, out.count('NEW       ')), (0, 3), err)
        code, out, err = run_sync('st')
        self.assertEqual((code, out.count('NEW       ')), (0, 4), err)
        self.assertNotIn('left out', err)

    def test_a_recorded_negative_is_honoured(self):
        cache = self.root / '.cache' / 'download-doc' / 'usb-capability.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({'st:STM32X0': {'usb': False, 'source': 'pdf:3 docs', 'evidence': 'old',
                                                    'complete': True}}))
        self.docs = [st_doc(f'DS{n}', ['STM32X0']) for n in range(1, 4)]
        self.adapter('st')
        self.peek_usb_in('DS1')
        code, _, err = run_sync('st')
        self.assertEqual(code, 1, err)
        self.assertIn('left out: 3 document(s)', err)
        self.assertEqual(json.loads(cache.read_text())['st:STM32X0']['source'], 'pdf:3 docs')

    def test_a_negative_recorded_before_adapters_reported_partial_lists_is_read_again(self):
        cache = self.root / '.cache' / 'download-doc' / 'usb-capability.json'
        cache.parent.mkdir(parents=True)
        stale = {'usb': False, 'source': 'pdf:3 docs', 'evidence': 'old'}
        self.docs = [st_doc(f'DS{n}', ['STM32X0']) for n in range(1, 5)]
        self.adapter('st')
        self.peek_usb_in('none')
        cache.write_text(json.dumps({'st:STM32X0': stale}))
        code, out, err = run_sync('st', '--types', 'datasheet')
        self.assertEqual((code, out.count('NEW       ')), (0, 4), err)
        self.assertEqual(json.loads(cache.read_text())['st:STM32X0'], stale)
        self.peek_usb_in('DS4')
        code, out, err = run_sync('st')
        self.assertEqual((code, out.count('NEW       ')), (0, 4), err)
        self.assertIs(json.loads(cache.read_text())['st:STM32X0']['usb'], True)

    def st_grids(self, grids, failing=()):
        """vendor_st over `grids` {grid id: (family, [doc ids])}, its cache under self.root, `failing` unreachable."""
        def http_get(url, *a, **k):
            gid = url.rsplit('/', 1)[1].split('.')[0]
            if gid in failing:
                raise RuntimeError('fetch failed')
            return json.dumps({'rows': [{'resourceType': 'Datasheet', 'title': d, 'version': '1.0',
                                         'localizedLinks': {'en': f'/{d}.pdf'}} for d in grids[gid][1]]}).encode()
        shutil.rmtree(self.root / 'st', ignore_errors=True)
        for name, value in (('SERIES', {g: fam for g, (fam, _) in grids.items()}), ('CACHE', self.root / 'st'),
                            ('http_get', http_get)):
            patch = unittest.mock.patch.object(vendor_st, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        sync.enumerate_vendor = self.enumerate_vendor

    def test_a_grid_st_could_not_fetch_records_no_negative(self):
        cache = self.root / '.cache' / 'download-doc' / 'usb-capability.json'
        self.peek_usb_in('none')
        grids = {'SS1': ('STM32X0', ['DS1', 'DS2', 'DS3']), 'SS2': ('STM32Y0', ['DS9'])}
        with unittest.mock.patch.object(vendor_st.time, 'sleep', lambda s: None):
            self.st_grids(grids, failing={'SS2'})
            code, out, err = run_sync('st')
            self.assertEqual((code, out.count('NEW       ')), (0, 3), err)
            self.assertIn('enumeration incomplete', err)
            self.assertFalse(cache.exists())
            self.st_grids(grids, failing={'SS1', 'SS2'})
            code, _, err = run_sync('st')
            self.assertEqual(code, 1)
            self.assertIn('st: enumeration failed (see above) — nothing to plan', err)
            # The default types are the default scope; the same list named explicitly narrows the run.
            self.st_grids(grids)
            code, _, err = run_sync('st', '--types', ','.join(sorted(doclib.TECHNICAL_TYPES)))
            self.assertEqual(code, 0, err)
            self.assertFalse(cache.exists())
            code, _, err = run_sync('st')
            self.assertEqual(code, 0, err)
            self.assertNotIn('enumeration incomplete', err)
        verdicts = json.loads(cache.read_text())
        self.assertEqual((verdicts['st:STM32X0']['usb'], verdicts['st:STM32X0']['complete']), (False, True))

    def test_a_failed_page_recovers_on_the_next_run(self):
        # Page two holds the one document showing USB; a run without it must not settle the family.
        cache = self.root / '.cache' / 'download-doc' / 'usb-capability.json'

        def nxp_run(fail):
            def page(tax, device_tax, start):
                if tax != vendor_nxp.TYPE_TAX['datasheet']:
                    return {'totalcount': 0, 'results': []}
                if start and fail:
                    raise RuntimeError('HTTP 503')
                codes = ['LPC99DS0', 'LPC99DS1', 'LPC99DS2'] if not start else ['LPC99DS3']
                return {'totalcount': 4, 'results': [
                    {'metaData': {'code': c}, 'url': f'https://nxp.com/{c}.pdf', 'title': c} for c in codes]}
            with unittest.mock.patch.object(vendor_nxp, '_page', page), \
                    unittest.mock.patch.object(vendor_nxp.time, 'sleep', lambda s: None):
                return run_sync('nxp')
        sync.enumerate_vendor = self.enumerate_vendor
        self.peek_usb_in('LPC99DS3')
        code, out, err = nxp_run(fail=True)
        self.assertEqual((code, out.count('NEW       ')), (0, 3), err)
        self.assertFalse(cache.exists())
        code, out, err = nxp_run(fail=False)
        self.assertEqual((code, out.count('NEW       ')), (0, 4), err)
        self.assertIs(json.loads(cache.read_text())['nxp:LPC99']['usb'], True)

    def test_a_failed_index_page_does_not_exclude_a_family_from_later_runs(self):
        # Three readable datasheets without USB settle "no USB", unless a page went missing.
        def nxp_run(fail):
            def page(tax, device_tax, start):
                if tax != vendor_nxp.TYPE_TAX['datasheet']:
                    return {'totalcount': 0, 'results': []}
                if start and fail:
                    raise RuntimeError('HTTP 503')
                return {'totalcount': 3 if not fail else 6, 'results': [
                    {'metaData': {'code': f'LPC99DS{start + n}'}, 'url': f'https://nxp.com/LPC99DS{start + n}.pdf',
                     'title': f'LPC99 datasheet {start + n}'} for n in range(3)]}
            cache = self.root / '.cache' / 'download-doc' / 'usb-capability.json'
            cache.unlink(missing_ok=True)
            with unittest.mock.patch.object(vendor_nxp, '_page', page), \
                    unittest.mock.patch.object(vendor_nxp.time, 'sleep', lambda s: None):
                code, _, err = run_sync('nxp')
            return code, err, json.loads(cache.read_text()) if cache.exists() else {}
        sync.enumerate_vendor = self.enumerate_vendor
        self.peek_usb_in('none')
        code, err, verdicts = nxp_run(fail=False)
        self.assertEqual((code, verdicts['nxp:LPC99']['usb']), (1, False), err)
        code, err, verdicts = nxp_run(fail=True)
        self.assertEqual(code, 0, err)
        self.assertIn('page at 3 failed', err)
        self.assertNotIn('nxp:LPC99', verdicts)

class UsbResolution(unittest.TestCase):
    """A family is recorded as having no USB only from documents actually read."""

    def test_an_unreadable_reference_manual_records_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index = doclib.UsbIndex(Path(tmp.name) / 'usb.json')
        rm = st_doc('RM1', ['STM32X0'], 'reference-manual')
        with unittest.mock.patch.object(doclib.UsbIndex, 'peek', lambda index, doc, tmp: None):
            self.assertIsNone(index.resolve('st', 'STM32X0', [rm], Path(tmp.name), complete=True))
        self.assertIsNone(index.get('st', 'STM32X0'))
        self.assertFalse((Path(tmp.name) / 'usb.json').exists())

    def test_a_complete_run_still_records_a_family_it_could_only_sample_as_kept(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index = doclib.UsbIndex(Path(tmp.name) / 'usb.json')
        cands = [st_doc(f'DS{n}', ['STM32X0']) for n in range(8)]
        with unittest.mock.patch.object(doclib.UsbIndex, 'peek', lambda index, doc, tmp: False):
            self.assertIsNone(index.resolve('st', 'STM32X0', cands, Path(tmp.name), complete=True))
        self.assertEqual(index.get('st', 'STM32X0')['usb'], None)
        self.assertEqual(index.get('st', 'STM32X0')['source'], 'pdf:sampled 6/8')

    def test_a_manual_whose_text_cannot_be_extracted_is_unread_not_usb_less(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rm = st_doc('RM1', ['STM32X0'], 'reference-manual')
        (Path(tmp.name) / 'RM1.pdf').write_bytes(b'%PDF' + b' ' * 2048)   # already downloaded
        no_usb = 'STM32X0 reference manual: GPIO, SPI, I2C'
        cases = {'no pdftotext': (None, no_usb, 0), 'pdftotext fails': ('pdftotext', no_usb, 1),
                 'no text layer': ('pdftotext', ' \n\f ', 0)}
        for name, (which, stdout, exit_code) in cases.items():
            with self.subTest(name), \
                    unittest.mock.patch.object(doclib.shutil, 'which', lambda cmd: which), \
                    unittest.mock.patch.object(doclib.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(
                        argv, exit_code, stdout, 'Syntax Error')):
                index = doclib.UsbIndex(Path(tmp.name) / f'{name}.json')
                self.assertIsNone(index.peek(rm, Path(tmp.name)))
                self.assertIsNone(index.resolve('st', 'STM32X0', [rm], Path(tmp.name), complete=True))
                self.assertFalse((Path(tmp.name) / f'{name}.json').exists())


@unittest.skipUnless(shutil.which('pdftotext'), 'the USB peek and the identity check read the PDF with pdftotext')
class RunDownloads(unittest.TestCase):
    """A document the USB peek downloaded is imported from the run's one download dir,
    once it passes the checks a fresh download would; the dir goes with the run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'tmp').mkdir()
        self.library = FakeCalibre()
        patch_library(self)
        self.good = pdf_with('DS1234 Rev 1 STM32X0 MCU with USB OTG full speed')
        self.fetched = []
        doc = st_doc('DS1234', ['STM32X0'])
        for target, name, value in ((sync, 'enumerate_vendor', lambda vendor, args: [doc]),
                                    (doclib, 'http_get', self.http_get), (Path, 'home', lambda: self.root),
                                    (tempfile, 'tempdir', str(self.root / 'tmp'))):
            patch = unittest.mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def http_get(self, url, *a, **k):
        self.fetched.append(url)
        return self.good

    def peeked(self, data):
        """UsbIndex.peek leaving `data` where a peek's download lands, and finding USB."""
        def peek(index, doc, tmp):
            (tmp / f'{doc.doc_id}.pdf').write_bytes(data)
            index.last_evidence = 'USB OTG'
            return True
        patch = unittest.mock.patch.object(doclib.UsbIndex, 'peek', peek)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_peeked_document_is_downloaded_once(self):
        code, out, err = run_sync('st', '--apply')
        self.assertEqual(code, 0, err)
        self.assertIn('IMPORTED  1 new', out)
        self.assertEqual(self.fetched, ['https://st.com/DS1234.pdf'])
        self.assertEqual(self.library.added, [self.good])

    def test_a_peeked_copy_that_fails_the_import_checks_is_downloaded_again(self):
        for name, data in (('not a pdf', b'<html>' + b'x' * 2048), ('too short', b'%PDF-1.4'),
                           ('another document', pdf_with('DS9999 Rev 2 STM32X0 USB OTG'))):
            with self.subTest(name):
                self.fetched.clear()
                self.library = FakeCalibre()
                self.peeked(data)
                code, out, err = run_sync('st', '--apply')
                self.assertEqual(code, 0, err)
                self.assertEqual(self.fetched, ['https://st.com/DS1234.pdf'])
                self.assertEqual(self.library.added, [self.good])

    def test_the_download_dir_goes_with_the_run(self):
        self.peeked(self.good)
        for argv in (('st',), ('st', '--apply')):
            with self.subTest(argv):
                code, _, err = run_sync(*argv)
                self.assertEqual(code, 0, err)
                self.assertEqual(list((self.root / 'tmp').iterdir()), [])
        self.library.fail['list'] = 'database read failed'
        code, _, err = run_sync('st', '--apply')
        self.assertEqual(code, 2, err)
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])


class EnumerationComplete(unittest.TestCase):
    """An adapter whose fetch failed says its list is partial; a 404 or an empty answer is the vendor's own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def patch(self, mod, **attrs):
        for name, value in attrs.items():
            patch = unittest.mock.patch.object(mod, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def complete(self, mod, http_get, **kw):
        """mod.enumerate_docs(**kw) with http_get answering -> whether it reported its list complete."""
        with unittest.mock.patch.object(mod, 'http_get', http_get), \
                contextlib.redirect_stderr(io.StringIO()):
            mod.enumerate_docs(**kw)
        return doclib.enumeration_complete

    @staticmethod
    def fails(*a, **k):
        raise RuntimeError('fetch failed')

    def test_an_adapter_with_one_index_page_reports_its_failure(self):
        cases = {vendor_allwinner: lambda *a, **k: b'<html>' + b' ' * 3000,
                 vendor_wch: lambda *a, **k: b'{"code": 0, "message": null, "data": null}',
                 vendor_st: lambda *a, **k: b'{"rows": []}'}
        self.patch(vendor_st, SERIES={'SS1': 'STM32X0'}, CACHE=self.root / 'st')
        sleep = unittest.mock.patch.object(vendor_st.time, 'sleep', lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        for mod, answers in cases.items():
            with self.subTest(mod.__name__):
                self.assertFalse(self.complete(mod, self.fails, refresh=True))
                self.assertTrue(self.complete(mod, answers, refresh=True))

    def test_each_enumeration_reports_only_its_own_fetches(self):
        page = lambda *a, **k: b'<html>' + b' ' * 3000
        self.assertEqual([self.complete(vendor_allwinner, get) for get in (self.fails, page, self.fails, page)],
                         [False, True, False, True])

    def test_a_partial_enumeration_does_not_carry_over_to_another_vendor(self):
        self.assertFalse(self.complete(vendor_allwinner, self.fails))
        self.assertTrue(self.complete(vendor_wch, lambda *a, **k: b'{"code": 0, "message": null, "data": null}'))

    def test_wch_reports_an_unsuccessful_answer_from_a_related_page(self):
        # The seed answers; the page it links to answers with an error code instead.
        def http_get(url, *a, **k):
            if 'CH32V307DS0_PDF' in url:
                return json.dumps({'code': 0, 'data': {
                    'id': 1, 'name': 'CH32V307DS0.PDF', 'content': 'CH32V307 datasheet',
                    'relationFileList': [{'name': 'CH32FV2x_V3xRM.PDF'}]}}).encode()
            return b'{"code": 500, "message": "busy", "data": null}' if 'V3XRM' in url.upper() else \
                b'{"code": 0, "message": null, "data": null}'
        self.patch(vendor_wch, SEEDS=['CH32V307DS0_PDF.html'])
        self.assertFalse(self.complete(vendor_wch, http_get))

    def test_st_falls_back_to_its_cached_grid(self):
        self.patch(vendor_st, SERIES={'SS1': 'STM32X0'}, CACHE=self.root / 'st')
        (self.root / 'st').mkdir()
        (self.root / 'st' / 'SS1.json').write_text('{"rows": []}')
        self.assertTrue(self.complete(vendor_st, self.fails, refresh=True))

    def test_espressif_reports_a_document_page_it_could_not_fetch(self):
        row = ('<tr><td class="views-field-title">ESP32-S3 Datasheet</td>'
               '<a href="https://docs.espressif.com/esp32-s3/ds.html">x</a></tr>')
        self.patch(vendor_espressif, _index_html=lambda refresh: row)
        self.assertFalse(self.complete(vendor_espressif, self.fails))
        self.assertTrue(self.complete(vendor_espressif, lambda *a, **k: b'<html><a href="ds.pdf"></a></html>'))

    def test_a_probing_adapter_tells_a_missing_document_from_a_failed_fetch(self):
        cases = {vendor_silabs: dict(_families_from_bsp=lambda: {'efm32gg12'}),
                 vendor_ti: dict(_parts_from_bsp=lambda: {'tm4c123gh6pm': 'TM4C'}),
                 vendor_geehy: dict(VERSIONS=['V1.0'])}
        for mod, attrs in cases.items():
            with self.subTest(mod.__name__):
                self.patch(mod, **attrs)
                for error, missing in ((doclib.NotFound, True), (RuntimeError, False)):
                    def http_get(*a, **k):
                        raise error('fetch failed')
                    self.assertEqual(self.complete(mod, http_get), missing)

    def test_nxp_reports_an_empty_page_short_of_its_total(self):
        self.patch(vendor_nxp, PACE_SECONDS=0)
        for total, complete in ((5, False), (0, True)):
            with self.subTest(total), \
                    unittest.mock.patch.object(vendor_nxp, '_page', lambda *a: {'totalcount': total, 'results': []}), \
                    contextlib.redirect_stderr(io.StringIO()):
                vendor_nxp.enumerate_docs(types=['errata'])
                self.assertIs(doclib.enumeration_complete, complete)


class DocumentLimit(unittest.TestCase):
    """NXP's own --limit stops paging at the cap."""

    def test_nxp_stops_at_the_cap_without_another_page_or_kind(self):
        pages = []

        def page(tax, device_tax, start):
            pages.append((tax, start))
            return {'totalcount': 6, 'results': [{'metaData': {'code': f'{tax}-{start + n}'}, 'url': 'u', 'title': 't'}
                                                 for n in range(3)]}
        with unittest.mock.patch.object(vendor_nxp, '_page', page), \
                unittest.mock.patch.object(vendor_nxp.time, 'sleep', lambda s: None):
            docs = vendor_nxp.enumerate_docs(types=['datasheet', 'errata'], limit=1)
        self.assertEqual(len(docs), 1)
        self.assertEqual(pages, [('t520', 0)])


class CacheOnlyAdapters(unittest.TestCase):
    """An adapter that only reads a hand-harvested cache names the missing file."""

    def test_a_missing_cache_stops_with_its_path(self):
        missing = Path(tempfile.gettempdir()) / 'download-doc-no-such-cache' / 'docs.txt'
        for mod in (vendor_renesas, vendor_microchip, vendor_hpmicro):
            with self.subTest(mod.__name__), unittest.mock.patch.object(mod, 'CACHE', missing):
                with self.assertRaises(SystemExit) as raised:
                    mod.enumerate_docs()
                self.assertIn(str(missing), str(raised.exception.code))

if __name__ == '__main__':
    unittest.main()
