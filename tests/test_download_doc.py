"""Tests for download-doc's plan(): a document already filed by hand is
reported as legacy, never imported a second time."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'download-doc' / 'scripts'))
import doclib  # noqa: E402
import titles  # noqa: E402
import vendor_arm  # noqa: E402
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

    def test_only_an_exact_copy_of_the_id_is_dropped(self):
        self.assertEqual(titles.id_first('LPC55S6x manual (UM11126)', 'UM11126'), 'UM11126 LPC55S6x manual')
        self.assertEqual(titles.id_first('MCX manual (UM11750-V3)', 'UM11750'), 'UM11750 MCX manual (UM11750-V3)')

    def test_a_leading_copy_of_the_id_is_dropped_but_not_a_qualified_one(self):
        self.assertEqual(titles.id_first('RM0433: STM32H7 manual', 'RM0433'), 'RM0433 STM32H7 manual')
        self.assertEqual(titles.id_first('UM11750-V3 MCX manual', 'UM11750'), 'UM11750 UM11750-V3 MCX manual')
        self.assertEqual(titles.id_first('RM04331 manual', 'RM0433'), 'RM0433 RM04331 manual')

    def test_a_title_never_carries_the_id_twice(self):
        rm = {'code': 'RM0433', 'type': 'Reference Manual', 'version': '8.0'}
        self.assertEqual(titles.title({**rm, 'title': ''}), 'RM0433 Reference manual Rev 8.0')
        self.assertEqual(titles.title({**rm, 'title': 'RM0433–STM32H7 reference manual'}),
                         'RM0433 STM32H7 reference manual Rev 8.0')

    def test_the_device_hint_sees_the_title_without_the_id(self):
        doc = {'code': 'RM0433', 'type': 'Reference Manual', 'version': '8', 'title': 'RM0433 reference manual'}
        self.assertEqual(titles.title(doc, None, 'STM32H7'), 'RM0433 STM32H7 reference manual Rev 8')


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


def setattr_all(saved):
    vendor_ti.http_get, vendor_ti.last_modified, vendor_ti._parts_from_bsp = saved


if __name__ == '__main__':
    unittest.main()
