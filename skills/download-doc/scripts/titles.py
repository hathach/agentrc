#!/usr/bin/env python3
"""Title generation for vendor documentation.

Extracted from the NXP (~1,500 docs) and ST (~800 docs) imports of 2026-08.
Vendor index titles are junk far more often than you'd expect, so the index title
falls back to the summary, and the result gets a cleanup pass.

    title(doc) -> "ES0392 STM32H742xI/G ... device errata Rev 15.0"

`doc` = {code, stem, title, summary, version, type, link}   from the vendor index

Run this file to execute the self-tests.
"""
import html
import os
import re

# --------------------------------------------------------------------- junk --
# Titles that are worse than useless. All observed in the wild:
#   "https://www.nxp.com/webapp/ext_download.jsp?code=ES_MCXW23"
#   "untitled 1."                        (LPC datasheets)
JUNK = re.compile(r'^(https?://|untitled\b|nxp semiconductor\b|document number|'
                  r'rev\.|contents\b|\d+$)', re.I)
PROSE = re.compile(r'^(this|the|it|introducing)\b', re.I)

DOCWORDS = r'(reference manual|user manual|users? guide|data ?sheet|errata|' \
           r'programming manual|application note|design reference|product brief)'
DOCLINE = re.compile(DOCWORDS, re.I)

TYPE_WORD = {
    'Errata Sheet': 'Errata', 'Errata': 'Errata',
    'Datasheet': 'Datasheet', 'Data Sheet': 'Datasheet',
    'Reference Manual': 'Reference manual',
    'User Manual': 'User manual',
    'Programming Manual': 'Programming manual',
    'Application Note': 'Application note',
}


def clean(s):
    """Normalise a candidate title string.

    The (R) strip is the subtle one: removing the glyph outright welds words
    together -- "ARM(R)Cortex(R)-M4" becomes "ARMCortex-M4" -- so the known
    weldings are split back out afterwards.
    """
    s = html.unescape(html.unescape(s or ''))
    s = re.sub(r'<[^>]+>', '', s)
    s = s.replace('®', '').replace('™', '').replace('©', '').replace('�', '')
    s = re.sub(r'\b(ARM|Arm)Cortex\b', r'Arm Cortex', s)
    s = re.sub(r'\bARM\b', 'Arm', s)
    s = re.sub(r'^\s*Microsoft Word\s*-\s*', '', s, flags=re.I)
    s = re.sub(r'\.(fm|docx?|pdf)\b', '', s, flags=re.I)
    return re.sub(r'\s+', ' ', s).strip(' -–,:.')


def usable(s):
    s = clean(s)
    return bool(s) and not JUNK.match(s) and len(s) > 6


def part_from_link(link):
    """ST datasheet descriptions are generic; the part number is the filename."""
    base = os.path.splitext((link or '').rsplit('/', 1)[-1])[0]
    return base.upper() if re.fullmatch(r'stm32[a-z0-9]+', base, re.I) else ''


def trim(s, n=95):
    if len(s) <= n:
        return s
    head = re.split(r'(?<=[.;])\s', s)[0]
    if len(head) <= n:
        return head
    return s[:n].rsplit(' ', 1)[0].rstrip(' ,;-')


def _dedupe_words(s):
    """Collapse an immediately repeated word: "Flash Flash Errata" -> "Flash Errata".

    Happens when a device name already ends with the qualifier that the type
    word re-adds (ES_LPC436X_FLASH -> "LPC436x Flash" + "Flash Errata Sheet").
    """
    return re.sub(r'\b(\w+)(\s+\1\b)+', r'\1', s, flags=re.I)


def _drop_redundant_grade(s):
    """"i.MX RT1010 (Consumer grade) ... for Consumer Products" -> drop the parenthetical."""
    m = re.search(r'\((Consumer|Industrial|Automotive) grade\)', s)
    if m and re.search(r'for (Extended )?%s' % m.group(1), s, re.I):
        s = s.replace(m.group(0), '')
    return re.sub(r'\s+', ' ', s).strip(' -–,:')


def strip_id(title, doc):
    """Remove a copy of the id the title already carries — but only an exact one.
    A qualified parenthetical like "(UM11750-V3)" is what distinguishes three
    otherwise identically titled books, so it must survive."""
    if not doc:
        return title
    d = re.escape(doc)
    t = re.sub(r"\s*\(\s*" + d + r"\s*\)\s*", " ", title, flags=re.I)
    # a bare hyphen before a trailing id glues it into a filename stem
    t = re.sub(r"[\s\-–—,:]*(?<![^\s–—,:])" + d + r"\b\s*$", "", t, flags=re.I)
    # a hyphen with no space after it qualifies the id rather than separating it;
    # an underscore joins it to the rest of a filename stem
    t = re.sub(r"^\s*" + d + r"(?:_|(?=$|[\s–—:,]|-(?:\s|$)))", "", t, flags=re.I)
    # a standalone word only: "-Sheet-DS00001692" inside a filename stem is not a copy.
    # Last, so removing it never leaves a stem's own copy at an edge for the rules above.
    t = re.sub(r"(?<!\S)" + d + r"(?!\S)", " ", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(' -–—,:')


def id_first(title, doc):
    return f"{doc} {strip_id(title, doc)}".strip()


def title(doc):
    code = (doc.get('code') or '').strip()
    stem = (doc.get('stem') or '').strip()
    word = TYPE_WORD.get(doc.get('type', ''), doc.get('type') or '')

    # vendor index title, then summary -- neither if it reads as prose
    candidates = [clean(src) for src in (doc.get('title'), doc.get('summary'))
                  if usable(src) and not PROSE.match(clean(src))]
    head = candidates[0] if candidates else code
    head = strip_id(strip_id(head, code), stem)

    # datasheets: prefix the part number, the description alone is generic
    part = part_from_link(doc.get('link', ''))
    if word == 'Datasheet' and part and part.lower() not in head.lower():
        head = f"{part} — {trim(head, 62)}"

    # NXP errata titles repeat the ES_ family prefix: "ES_LPC436x Flash Errata Sheet".
    # Drop the prefix if the remainder still names a part, else just unprefix it.
    w = head.split()
    if w and re.match(r'^ES[_-]\S+$', w[0], re.I):
        rest = ' '.join(w[1:])
        head = rest if re.search(r'[A-Za-z]+\d', rest) else \
               (re.sub(r'^ES[_-]', '', w[0]) + (' ' + rest if rest else ''))

    head = trim(head)
    if word and not DOCLINE.search(head):
        head = f"{head} — {word}" if head else word

    head = _drop_redundant_grade(_dedupe_words(head))
    ver = str(doc.get('version') or '').strip()
    # Espressif reports "v1.8"; strip the marker rather than discarding the revision.
    numeric_ver = re.sub(r'^v', '', ver, flags=re.I) \
        if re.fullmatch(r'v?\d+(\.\d+)*', ver, re.I) else ''
    return f"{code} {head}".strip() + (f" ({stem})" if stem else '') + \
        (f" Rev {numeric_ver}" if numeric_ver else '')


# ------------------------------------------------------------------- tests --
if __name__ == '__main__':
    T = [
        # --- the three cleanup cases you asked for -----------------------------
        # the (R) glyph sits BETWEEN the words here, so stripping it welds them
        ("ARMCortex re-split (real ST string, DS10062)",
         {'code': 'DS10062', 'type': 'Datasheet', 'version': '4.0',
          'link': '/resource/en/datasheet/stm32f378cc.pdf',
          'summary': 'ARM®Cortex®-M4 32b MCU+FPU, up to 256KB Flash+32KB SRAM'},
         "DS10062 STM32F378CC — Arm Cortex-M4 32b MCU+FPU, up to 256KB Flash+32KB SRAM — Datasheet Rev 4.0"),

        # and the hyphenated variant must NOT be re-split into "Arm Cortex"
        ("ARM-based stays hyphenated (DS10036)",
         {'code': 'DS10036', 'type': 'Datasheet', 'version': '4.0',
          'link': '/resource/en/datasheet/stm32f358cc.pdf',
          'summary': 'ARM®-based Cortex®-M4 32b MCU+FPU, up to 256KB Flash'},
         "DS10036 STM32F358CC — Arm-based Cortex-M4 32b MCU+FPU, up to 256KB Flash — Datasheet Rev 4.0"),

        ("ES_ prefix dropped, no doubled 'Flash' (real NXP title)",
         {'code': 'ES_LPC436X_FLASH', 'type': 'Errata Sheet', 'version': '2.3',
          'title': 'ES_LPC436x Flash Errata Sheet'},
         "ES_LPC436X_FLASH LPC436x Flash Errata Sheet Rev 2.3"),

        ("redundant grade parenthetical",
         {'code': 'IMXRT1010CEC', 'type': 'Datasheet', 'version': '0',
          'summary': 'i.MX RT1010 (Consumer grade) Crossover Processors Data Sheet for Consumer Products.'},
         "IMXRT1010CEC i.MX RT1010 Crossover Processors Data Sheet for Consumer Products Rev 0"),

        # --- junk index titles -------------------------------------------------
        ("url title falls through to summary",
         {'code': 'ES_MCXW23', 'type': 'Errata Sheet', 'version': '1.2',
          'title': 'https://www.nxp.com/webapp/ext_download.jsp?code=ES_MCXW23',
          'summary': 'MCX W23 Mask set Errata'},
         "ES_MCXW23 MCX W23 Mask set Errata Rev 1.2"),

        ("Microsoft Word filename title is cleaned",
         {'code': 'UM12442', 'type': 'User Manual', 'version': '1.0',
          'title': 'Microsoft Word - FRDM-MCXA174 Board User Manual.doc'},
         "UM12442 FRDM-MCXA174 Board User Manual Rev 1.0"),

        ("prose summary is not a title",
         {'code': 'IMXRT1170ACE', 'type': 'Errata Sheet', 'version': '1.6',
          'title': 'This document details all known silicon errata for the i.MX RT1170A.',
          'summary': 'Chip Errata for i.MX RT1170A'},
         "IMXRT1170ACE Chip Errata for i.MX RT1170A Rev 1.6"),

        # --- normal path -------------------------------------------------------
        ("ST errata straight from the index",
         {'code': 'ES0392', 'type': 'Errata Sheet', 'version': '15.0',
          'summary': 'STM32H742xI/G, STM32H743xI/G, STM32H750xB, STM32H753xI device errata'},
         "ES0392 STM32H742xI/G, STM32H743xI/G, STM32H750xB, STM32H753xI device errata Rev 15.0"),

        ("non-numeric revision is omitted from the title",
         {'code': 'KINETIS_L_1N52N', 'type': 'Errata Sheet', 'version': '11 Sep 019',
          'title': 'Mask Set Errata for Mask 1N52N'},
         "KINETIS_L_1N52N Mask Set Errata for Mask 1N52N"),
    ]

    ok = True
    for name, doc, expect in T:
        got = title(doc)
        flag = 'ok  ' if got == expect else 'FAIL'
        if got != expect:
            ok = False
        print(f"{flag} {name}\n       got: {got}")
        if got != expect:
            print(f"       want: {expect}")
    print('\nALL PASS' if ok else '\nFAILURES')
