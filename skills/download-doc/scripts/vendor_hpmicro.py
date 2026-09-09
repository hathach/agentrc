#!/usr/bin/env python3
"""HPMicro adapter (HPM5300 / HPM5E00 / HPM62xx / HPM63xx / HPM67xx / HPM68xx / HPM6E00).

⚠️ **English documents live on a different path from the Chinese ones.** The Chinese
portal serves `/Public/Uploads/uploadfile/files/…`; the English portal serves
`/Public/Uploads/uploadfile2/files/…` — note the `2`. Probing the Chinese page's URLs
finds only Chinese PDFs and makes the vendor look Chinese-only, which is the wrong
conclusion: HPMicro publishes full English datasheets, user manuals and errata.

Worth recording how that was nearly missed. A WebFetch of the English page reports
"No Relevant Data Available", because the list is built by JavaScript and a
pre-render fetch sees an empty shell. Only opening it in a browser shows the 27
documents. Treat "the page says there is nothing" as unproven until something that
executes JavaScript has looked.

Enumeration therefore needs a browser once (the page's data endpoint refuses headless
clients); the resulting paths are cached here and the PDFs themselves download fine
headlessly.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc   # noqa: E402

AUTHOR = "HPMicro Semiconductor"
BASE = "https://www.hpmicro.com/Public/Uploads/uploadfile2/files/"
CACHE = Path.home() / ".cache" / "download-doc" / "hpmicro" / "docs.txt"

USB_SCOPE = None
USB_SCOPE_NOTE = "USB capability is read from each family's datasheet"

KIND = [
    (re.compile(r"Errata", re.I), "errata"),
    (re.compile(r"UM|UserGuide|User_Guide", re.I), "reference-manual"),
    (re.compile(r"DS", re.I), "datasheet"),
]
# Lazy, with a lookahead at the document-type token. Two traps here: HPM\d{2} rejects
# HPM5E00 and HPM6E00 (letter in the second position) and silently dropped 7 of 18
# documents, and a greedy match swallows the D of "DS" and yields families like
# "HPM5300D". Both are the same mistake made on WCH ("CH583D").
FAM = re.compile(r"(HPM[0-9][0-9A-Z]{2,9}?)(?=DS|UM|Errata|EVK|Series|Flyer|$)", re.I)


def _kind(name: str) -> str | None:
    for rx, k in KIND:
        if rx.search(name):
            return k
    return None


def _version(name: str) -> str | None:
    m = re.search(r"V\.?(\d+(?:\.\d+)*)", name, re.I)
    return m.group(1) if m else None


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    if not CACHE.exists():
        print(f"  no harvested list at {CACHE} — collect it through a browser "
              f"(see this module's docstring)", file=sys.stderr)
        return []
    want = {f.upper() for f in families} if families else None
    docs = []
    for rel in CACHE.read_text().split():
        name = rel.rsplit("/", 1)[-1]
        kind = _kind(name)
        if kind is None or (types and kind not in types):
            continue
        m = FAM.match(name)
        fam = m.group(1).upper() if m else None
        if fam is None or (want and fam not in want):
            continue
        stem = re.sub(r"\.pdf$", "", name, flags=re.I)
        docs.append(Doc(
            vendor="hpmicro", doc_id=stem, doc_type=kind, version=_version(name),
            title=f"{fam} {kind.replace('-', ' ').title()}", url=BASE + rel,
            author=AUTHOR, family=[fam], desc="", verify_id=False,
            aliases=[stem]))
    return docs


if __name__ == "__main__":
    for d in enumerate_docs():
        print(f"  {d.doc_id:<40} {d.doc_type:<18} {d.family[0]:<9} v{d.version}")
