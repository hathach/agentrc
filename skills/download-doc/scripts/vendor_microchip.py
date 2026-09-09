#!/usr/bin/env python3
"""Microchip adapter (32-bit: SAM Arm Cortex-M, PIC32; and the USB25xx/USB8xxx hubs).

⚠️ **Enumeration needs a browser; downloading does not.** www.microchip.com answers
every headless client with an Akamai "Access Denied" — sitemap, product pages, search,
all of it, while robots.txt from the same host fetches fine and explicitly permits
document paths. But `ww1.microchip.com` serves the PDFs to a plain wget. So the URL
list is harvested once through a signed-in browser and cached here; after that the
downloads are ordinary.

The list comes from `https://www.microchip.com/documents_{1,2}.xml`, two sitemaps
holding 36,328 direct PDF URLs — the whole document catalogue, no scraping of product
pages required. Re-harvest with the browser when it goes stale.

Filenames are the only identifier: `SAM-D20-Family-Silicon-Errata-...-DS80000747.pdf`.
Note the hyphens — matching `SAMD20` contiguously finds 358 documents where the
hyphen-tolerant pattern finds 540, so a third of the catalogue hides behind a dash.

A lettered filename pins a revision (`…-DS00001692D.pdf` is Rev D); the unlettered
one is the rolling latest — `…-DS00001692.pdf` served Rev E on 2026-09-01. When the
catalogue lists both, the pinned copy is by construction the older, so it is dropped.
A rolling file's letter exists only inside the PDF, so its *version* is the sitemap
`<lastmod>` date, cached as `url<TAB>lastmod`: that is what moves when Microchip
replaces the file, and a date compares against a date. The footer letter
(`DS00001692E-page 1`) is kept in the comments as description, not as the version.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import doclib                     # noqa: E402
from doclib import Doc            # noqa: E402

AUTHOR = "Microchip Technology"
CACHE = Path.home() / ".cache" / "download-doc" / "microchip" / "docs.txt"

USB_SCOPE = None
USB_SCOPE_NOTE = ("USB capability is not in the filename, so it is read out of each "
                  "family's datasheet")

TYPE_MAP = {
    "DataSheets": "datasheet",
    "Errata": "errata",
    "ReferenceManuals": "reference-manual",
    "TechnicalReferenceManual": "reference-manual",
    "ProgrammingSpecifications": "programming-manual",
    "UserGuides": "user-manual",
    "DesignChecklist": "application-note",
}

# SAM-D20 / SAMD20 / ATSAMD20, and PIC32MZ / PIC32-MZ / PIC32CX.
_SAM = re.compile(r"(?:AT)?SAM[-_ ]?([A-Z])[-_ ]?(\d{1,2})", re.I)
_PIC32 = re.compile(r"PIC32[-_ ]?(MX|MZ|MK|MM|CM|CZ|CX)", re.I)
# USB2517 / USB2514B / USB251xB (family wildcard) / USB82514; the letter is a silicon
# grade and stays in the tag, EVB-USB2514BC (a board) still yields USB2514B.
_USBHUB = re.compile(r"USB(\d{3}x|\d{3,5})([A-Z])?(?![0-9])")
# DS80000440N is revision N of DS80000440. The prefix is not always there:
# USB2517-USB2517i-Data-Sheet-00001598C.pdf.
_DS = re.compile(r"(?:[Dd][Ss](\d{5,8})|(?<![0-9A-Za-z])(\d{8}))([A-Za-z])?\b")
_FOOTER = re.compile(r"DS(\d{8})([A-Z])-page \d")


def family_of(name: str) -> str | None:
    m = _PIC32.search(name or "")
    if m:
        return f"PIC32{m.group(1).upper()}"
    m = _SAM.search(name or "")
    if m:
        return f"SAM{m.group(1).upper()}{m.group(2)}"
    m = _USBHUB.search(name or "")
    return f"USB{m.group(1)}{m.group(2) or ''}" if m else None


def _ds(name: str) -> tuple:
    """(document number, revision letter or None) from a filename."""
    m = _DS.search(name or "")
    if not m:
        return (None, None)
    return (m.group(1) or m.group(2), m.group(3).upper() if m.group(3) else None)


def _rev(name: str) -> str | None:
    return _ds(name)[1]


def describe_pdf(path: Path) -> str | None:
    m = _FOOTER.search(doclib.page1_text(path))
    return f"Rev {m.group(2)} per the page footer (DS{m.group(1)})" if m else None


def _entries():
    """(url, lastmod) per cache line; a harvest that predates lastmod holds bare URLs."""
    for line in CACHE.read_text().splitlines():
        url, _, mod = line.strip().partition("\t")
        if url:
            yield url, (mod.strip() or None)


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    if not CACHE.exists():
        print(f"  no harvested URL list at {CACHE} — the catalogue must be collected "
              f"through a browser (see this module's docstring)", file=sys.stderr)
        return []
    want = {f.upper() for f in families} if families else None
    docs, seen = [], set()
    for url, mod in _entries():
        name = url.rsplit("/", 1)[-1]
        cat = (re.search(r"ProductDocuments/([A-Za-z]+)/", url) or [None, ""])[1]
        kind = TYPE_MAP.get(cat)
        if kind is None or (types and kind not in types):
            continue
        fam = family_of(name)
        if fam is None or (want and fam.upper() not in want):
            continue
        stem = re.sub(r"\.pdf$", "", name, flags=re.I)
        if stem in seen:
            continue
        seen.add(stem)
        docs.append(Doc(
            vendor="microchip", doc_id=stem, doc_type=kind, version=_rev(name) or mod,
            title=stem.replace("-", " ").replace("_", " "), url=url, author=AUTHOR,
            family=[fam], desc="", verify_id=False,
            aliases=[stem.replace("-", " ")]))
    rolling = {num for num, letter in map(_ds, (d.doc_id for d in docs))
               if num and letter is None}
    return [d for d in docs if _rev(d.doc_id) is None or _ds(d.doc_id)[0] not in rolling]


if __name__ == "__main__":
    import collections
    ds = enumerate_docs(types=sys.argv[1].split(",") if len(sys.argv) > 1 else None)
    fams = collections.Counter(f for d in ds for f in d.family)
    print(f"{len(ds)} documents, {len(fams)} families")
    for k, v in collections.Counter(d.doc_type for d in ds).most_common():
        print(f"  {k:<20} {v}")
    print("  families:", ", ".join(sorted(fams)))
