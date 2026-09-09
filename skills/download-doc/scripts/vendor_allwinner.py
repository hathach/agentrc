#!/usr/bin/env python3
"""Allwinner adapter (F1C100s / F1C200s / F1C600).

⚠️ **These are community-hosted documents, not vendor-published ones.** Allwinner
publishes no public document portal for the F1C series; the datasheets and user
manuals in circulation are mirrored on the linux-sunxi wiki, which is where TinyUSB's
own f1c100s port points people. Provenance therefore differs from every other adapter
here, and the imported books say so in their comments — a reader should know a
document came from a community mirror rather than the vendor.

The wiki page carries direct PDF links, so enumeration is a single fetch and a regex.

Note F1C100s and F1C200s share silicon documentation: the F1C200s datasheet and user
manual are the reference for both, which is why they appear under the F1C100s page.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, http_get   # noqa: E402

AUTHOR = "Allwinner Technology"
PAGE = "https://linux-sunxi.org/F1C100s"
PDF = re.compile(r"https://linux-sunxi\.org/images/[^\s\"']+\.pdf")

USB_SCOPE = None
USB_SCOPE_NOTE = "USB capability is read from the user manual"

KIND = [
    (re.compile(r"user[_ -]?manual", re.I), "reference-manual"),
    (re.compile(r"datasheet", re.I), "datasheet"),
]


def _kind(name: str) -> str | None:
    for rx, k in KIND:
        if rx.search(name):
            return k
    return None


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    try:
        html = http_get(PAGE, timeout=30, accept="text/html",
                        validate=lambda b: len(b) > 2000).decode("utf-8", "replace")
    except RuntimeError:
        print("  linux-sunxi wiki unreachable", file=sys.stderr)
        return []
    want = {f.upper() for f in families} if families else None
    docs, seen = [], set()
    for url in sorted(set(PDF.findall(html))):
        name = url.rsplit("/", 1)[-1]
        kind = _kind(name)
        if kind is None or (types and kind not in types):
            continue
        m = re.search(r"(F1C\d{3}[A-Za-z]?)", name, re.I)
        fam = m.group(1).upper() if m else None
        if fam is None or (want and fam not in want):
            continue
        stem = re.sub(r"\.pdf$", "", name, flags=re.I)
        if stem in seen:
            continue
        seen.add(stem)
        ver = (re.search(r"_V(\d+\.\d+)", name, re.I) or [None, None])[1]
        docs.append(Doc(
            vendor="allwinner", doc_id=stem, doc_type=kind, version=ver,
            title=stem.replace("_", " "), url=url, author=AUTHOR,
            family=[fam], desc="Mirrored on the linux-sunxi wiki; Allwinner publishes "
                                "no public document portal for this family.",
            verify_id=False, aliases=[stem.replace("_", " ")]))
    return docs


if __name__ == "__main__":
    for d in enumerate_docs():
        print(f"  {d.doc_id:<44} {d.doc_type:<18} {d.family[0]:<8} v{d.version}")
