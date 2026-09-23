"""Tests for download-doc's plan(): a document already filed by hand is
reported as legacy, never imported a second time."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'download-doc' / 'scripts'))
import doclib  # noqa: E402
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


def setattr_all(saved):
    vendor_ti.http_get, vendor_ti.last_modified, vendor_ti._parts_from_bsp = saved


if __name__ == '__main__':
    unittest.main()
