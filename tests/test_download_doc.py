"""Tests for download-doc's plan(): a document already filed by hand is
reported as legacy, never imported a second time."""
import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'download-doc' / 'scripts'))
import doclib  # noqa: E402
import retitle  # noqa: E402
import sync  # noqa: E402
import titles  # noqa: E402
import vendor_arm  # noqa: E402
import vendor_microchip  # noqa: E402
import vendor_ti  # noqa: E402


def doc(doc_id, aliases=()):
    return doclib.Doc(vendor='st', doc_id=doc_id, doc_type='reference-manual', version='8.0',
                      title='t', url='u', author='STMicroelectronics', aliases=list(aliases))


def legacy(*titles):
    return {doclib.norm_title(t): {'id': n, 'title': t} for n, t in enumerate(titles, 1)}


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

    def test_the_device_hint_sees_the_title_without_the_id(self):
        doc = {'code': 'RM0433', 'type': 'Reference Manual', 'version': '8', 'title': 'RM0433 reference manual'}
        self.assertEqual(titles.title(doc, None, 'STM32H7'), 'RM0433 STM32H7 reference manual Rev 8')


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

    def test_apply_refuses_while_something_holds_the_library(self):
        class Held:
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


class GuiDetection(unittest.TestCase):
    """Only the Calibre GUI holds the library; its workers and other tools do not."""

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

    def lock_then(self, results, server=None):
        """A Library whose calibredb answers each call from `results` (a lock error or output)."""
        calls = []

        def run(argv, capture_output, text):
            calls.append(argv)
            out = results.pop(0)
            return subprocess.CompletedProcess(argv, 1 if out == 'lock' else 0, out, 'Another calibre program is running' if out == 'lock' else '')
        saved = (doclib.subprocess.run, doclib.time.sleep)
        self.addCleanup(lambda: (setattr(doclib.subprocess, 'run', saved[0]), setattr(doclib.time, 'sleep', saved[1])))
        doclib.subprocess.run = run
        doclib.time.sleep = lambda s: None
        lib = doclib.Library.__new__(doclib.Library)
        lib.path, lib.server = Path('/lib'), server
        return lib, calls

    def test_a_lock_with_only_a_worker_running_is_retried(self):
        self.running(*self.WORKER)
        lib, calls = self.lock_then(['lock', 'ok'])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lib._run('list'), 'ok')
        self.assertEqual(len(calls), 2)

    def test_a_lock_with_the_gui_open_is_not_retried(self):
        self.running(*self.GUI)
        lib, calls = self.lock_then(['lock'])
        with self.assertRaisesRegex(RuntimeError, 'GUI is open'):
            lib._run('list')
        lib, calls = self.lock_then(['lock', 'via server'], server=('http://localhost:8080/#lib', 'u', 'pw'))
        self.assertEqual(lib._run('list'), 'via server')
        self.assertEqual(calls[1][2], 'http://localhost:8080/#lib')


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
                raise RuntimeError('404')
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

    def run(self, *args, check=True, tries=4):
        if args[0] in self.fail:
            raise RuntimeError(self.fail[args[0]])
        if args[0] == 'list':
            books = self.books
            if '--search' in args:
                author = args[args.index('--search') + 1].split('"')[1]
                books = [b for b in books if b['authors'] == author]
            if '-s' in args:
                book_id = int(args[args.index('-s') + 1].split(':')[1])
                books = [{'id': b['id'], 'formats': b.get('formats', [])} for b in books if b['id'] == book_id]
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



class RemoveBacksUp(unittest.TestCase):
    """Library.remove() removes a book only once its PDF is backed up."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.backup = self.root / 'superseded'
        patch_library(self)
        self.lib = doclib.Library.__new__(doclib.Library)

    def book(self, *names):
        paths = []
        for name in names:
            path = self.root / 'lib' / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'%PDF old')
            paths.append(str(path))
        self.library = FakeCalibre({'id': 3, 'title': 't', 'authors': 'a', 'formats': paths})
        return paths

    def test_the_files_are_copied_then_the_book_removed(self):
        pdf, epub = self.book('m.pdf', 'm.epub')
        copies = self.lib.remove(3, self.backup)
        self.assertEqual(copies, [self.backup / 'm.book3.pdf', self.backup / 'm.book3.epub'])
        self.assertEqual([c.read_bytes() for c in copies], [b'%PDF old'] * 2)
        self.assertEqual((self.library.books, self.library.writes), ([], ['remove']))

    def test_an_earlier_backup_of_the_same_name_is_kept(self):
        self.backup.mkdir()
        (self.backup / 'm.book3.pdf').write_bytes(b'earlier')
        self.book('m.pdf')
        self.assertEqual(self.lib.remove(3, self.backup), [self.backup / 'm.book3.1.pdf'])
        self.assertEqual((self.backup / 'm.book3.pdf').read_bytes(), b'earlier')

    def test_no_pdf_to_back_up_removes_nothing(self):
        for names, missing in ((('m.epub',), False), (('m.pdf',), True), ((), False)):
            with self.subTest(names=names, missing=missing):
                paths = self.book(*names)
                if missing:
                    Path(paths[0]).unlink()
                with self.assertRaisesRegex(doclib.BackupError, 'no PDF on disk'):
                    self.lib.remove(3, self.backup)
                self.assertEqual(self.library.writes, [])

    def test_a_listing_or_copy_failure_removes_nothing(self):
        self.book('m.pdf')
        self.library.fail['list'] = 'database locked'
        with self.assertRaisesRegex(doclib.BackupError, 'could not be listed'):
            self.lib.remove(3, self.backup)
        del self.library.fail['list']
        self.backup.write_bytes(b'')  # a file where the directory should be
        with self.assertRaisesRegex(doclib.BackupError, 'backing its files up failed'):
            self.lib.remove(3, self.backup)
        self.assertEqual(self.library.writes, [])

    def test_a_pdf_that_vanishes_before_its_copy_removes_nothing(self):
        pdf, = self.book('m.pdf')
        checks = []
        real = Path.is_file

        def is_file(path):
            if str(path) == pdf:
                checks.append(path)
                return len(checks) == 1  # there for the check, gone by the copy
            return real(path)
        saved = Path.is_file
        self.addCleanup(lambda: setattr(Path, 'is_file', saved))
        Path.is_file = is_file
        with self.assertRaisesRegex(doclib.BackupError, 'vanished before it was copied'):
            self.lib.remove(3, self.backup)
        self.assertEqual(self.library.writes, [])

    def test_a_failed_remove_names_the_backup(self):
        self.book('m.pdf')
        self.library.fail['remove'] = 'permission denied'
        with self.assertRaisesRegex(doclib.RemoveError, 'backed up to .*m.book3.pdf'):
            self.lib.remove(3, self.backup)


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
        saved = doclib.http_get, sync.SUPERSEDED

        def restore():
            doclib.http_get, sync.SUPERSEDED = saved
        self.addCleanup(restore)
        doclib.http_get = self.http_get
        sync.SUPERSEDED = self.root / 'superseded'

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

    def test_apply_backs_up_removes_and_adds(self):
        code, out, err = self.refresh('--apply')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.library.writes, ['remove', 'add', 'set_metadata'])
        self.assertEqual([b['id'] for b in self.library.books], [8])
        self.assertEqual(self.library.books[0]['title'], 'PS1234 nRF52820 Product Specification — Datasheet Rev 1.5')
        self.assertEqual((sync.SUPERSEDED / 'ps.book7.pdf').read_bytes(), b'%PDF old')
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

    def test_a_failed_backup_leaves_the_book_in_place(self):
        self.old_pdf.unlink()
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn('backup failed, book #7 left in place: book #7: no PDF on disk', err)
        self.assertEqual(self.library.writes, [])

    def test_a_failed_removal_adds_nothing(self):
        self.library.fail['remove'] = 'permission denied'
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn('removal failed, nothing added', err)
        self.assertIn('calibredb remove failed (permission denied); backed up to', err)
        self.assertNotIn('add', self.library.writes)

    def test_a_failed_add_after_the_removal_names_the_backup(self):
        for error, said in (('disk full', '(disk full)'), ('', '(RuntimeError)')):
            with self.subTest(error=error):
                self.old_pdf.write_bytes(b'%PDF old')
                self.library = FakeCalibre(self.filed())
                self.library.fail['add'] = error
                code, _, err = self.refresh('--apply')
                self.assertEqual(code, 1)
                self.assertIn(f'book #7 was removed but the new copy was not added {said}', err)
                self.assertRegex(err, r'its files are in \S*ps\.book7(\.1)?\.pdf')

    def test_a_standard_identifier_beside_the_vendor_one_is_no_second_spelling(self):
        self.library = FakeCalibre({**self.filed(), 'identifiers': {'nordic': 'PS1234', 'isbn': '9780000000000'}})
        code, _, err = self.refresh()
        self.assertEqual(code, 0, err)

    def test_an_add_that_files_nothing_names_the_backup(self):
        saved = doclib.Library.add
        self.addCleanup(lambda: setattr(doclib.Library, 'add', saved))

        def add(lib, doc, pdf):
            lib.last_add_status = 'failed'
        doclib.Library.add = add
        code, _, err = self.refresh('--apply')
        self.assertEqual(code, 1)
        self.assertIn('was removed but the new copy was not added', err)
        self.assertIn('ps.book7.pdf', err)


class VendorReplace(unittest.TestCase):
    """`sync.py <vendor> --apply` reports a replacement that fails by stage and exits 1."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        doc = doclib.Doc(vendor='ti', doc_id='SBOS548', doc_type='datasheet', version='C',
                         title='INA3221', url='https://ti.com/x.pdf', author=vendor_ti.AUTHOR)
        self.library = FakeCalibre({'id': 4, 'title': 'SBOS548 INA3221 Rev B', 'authors': vendor_ti.AUTHOR,
                                    'identifiers': {'ti': 'SBOS548'}, 'comments': 'Revision: B',
                                    'formats': [str(self.root / 'old.pdf')]})
        new = self.root / 'new.pdf'
        new.write_bytes(b'%PDF new')
        patch_library(self)
        saved = (sync.enumerate_vendor, sync.fetch, sync.SUPERSEDED, sys.argv)

        def restore():
            sync.enumerate_vendor, sync.fetch, sync.SUPERSEDED, sys.argv = saved
        self.addCleanup(restore)
        sync.enumerate_vendor = lambda vendor, args: [doc]
        sync.fetch = lambda d, mod, tmp: (new, 'ok')
        sync.SUPERSEDED = self.root / 'superseded'
        sys.argv = ['sync.py', 'ti', '--parts', 'ina3221', '--apply']

    def run_sync(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = sync.main()
        return code, out.getvalue()

    def test_a_failed_backup_skips_the_replacement(self):
        code, out = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn('backup failed, book #4 left in place', out)
        self.assertEqual(self.library.writes, [])

    def test_a_failed_add_after_the_removal_names_the_backup(self):
        (self.root / 'old.pdf').write_bytes(b'%PDF old')
        self.library.fail['add'] = 'disk full'
        code, out = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn('book #4 was removed but the new copy was not added (disk full)', out)
        self.assertIn('old.book4.pdf', out)

    def test_a_replacement_that_succeeds_exits_zero(self):
        (self.root / 'old.pdf').write_bytes(b'%PDF old')
        code, out = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn('1 replaced', out)

if __name__ == '__main__':
    unittest.main()
