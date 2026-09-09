#!/usr/bin/env python3
"""Renesas adapter (RA and RX microcontrollers).

Renesas publishes no document API and its product pages render client-side, so the
catalogue is recovered from the **sitemap**: pages 2-59 of
`https://www.renesas.com/sitemap.xml?page=N` are document URLs. That scan is cached
under ~/.cache/download-doc/renesas/ra_rx_docs.json; re-run scripts/renesas_scan to
refresh it.

The document URL *is* the PDF (no landing page), and the type code sits in the path:
`/en/document/dst/rx210-group-datasheet-rev150`.

⚠️ Family matching must be tight. A loose `ra[0-9a-z]{2,4}` also matches "**ra**nge",
"**ra**ms" and "**ra**te", which pulled 426 unrelated documents into an RA/RX scope —
the slugs are English words as often as part numbers.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc   # noqa: E402

AUTHOR = "Renesas Electronics"
CACHE = Path.home() / ".cache" / "download-doc" / "renesas" / "ra_rx_docs.json"

# USB capability is per family and not stated in the sitemap, so it is resolved by
# reading documents (see doclib.UsbIndex).
USB_SCOPE = None
USB_SCOPE_NOTE = ("the sitemap carries no peripheral data, so USB capability is read "
                  "out of each family's datasheet or hardware manual")

# Renesas type codes, from the URL path.
TYPE_MAP = {
    "dst": "datasheet",
    "mah": "reference-manual",      # "User's Manual: Hardware" — the RA/RX reference manual
    "man": "reference-manual",
    "apn": "application-note",
    "tcu": "errata",                # Technical Customer Update — carries silicon errata
    "tnn": "technical-note",
    "tnr": "technical-note",
    "qsg": "user-guide",
}

# RA0E1 / RA2A1 / RA4M1 / RA6M5 / RA8M1, and RX210 / RX65N / RX72N.
FAMILY = re.compile(r"\b(ra[0-9][a-z][0-9]|ra[0-9]m[0-9]|rx[0-9]{2,3}[a-z]?)\b", re.I)


# Strings that look like an RX family and are not MCUs at all: RX850 and RX830 are
# 1990s V850 real-time operating systems (RX850 Pro, RX830/ITRON) and RX00 is part of a
# wireless-charging kit part number. 42 documents' worth of V850 OS manuals would
# otherwise be filed as RX microcontroller documentation.
NOT_MCU = {"RX850", "RX830", "RX00"}


def family_of(slug: str) -> str | None:
    m = FAMILY.search(slug or "")
    if not m:
        return None
    fam = m.group(1).upper()
    return None if fam in NOT_MCU else fam


def _rev(slug: str) -> str | None:
    """Some slugs carry the revision: '...-datasheet-rev150' -> '1.50'. Where they
    don't, the revision is unknown and the document is reported as uncheckable rather
    than silently assumed current."""
    m = re.search(r"-rev(\d)(\d{2})$", slug or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    if not CACHE.exists():
        print(f"  no cached sitemap scan at {CACHE} — run the scan first", file=sys.stderr)
        return []
    want = {f.upper() for f in families} if families else None
    seen, docs = set(), []
    # The sitemap carries every document once per locale (2,475 en + 1,736 ja), same
    # slug, different path. Deduping by URL kept both, so each document was enumerated
    # twice, counted twice in the plan, and the second copy was refused by calibredb as
    # a duplicate title — which the importer then reported as ~700 failures. Dedupe by
    # document id, English first.
    rows = sorted(json.loads(CACHE.read_text()),
                  key=lambda r: 0 if "/en/" in r["url"] else 1)
    for row in rows:
        kind = TYPE_MAP.get(row["type"])
        if kind is None or (types and kind not in types):
            continue
        fam = family_of(row["slug"])
        if fam is None or (want and fam not in want):
            continue
        if row["slug"] in seen:
            continue
        seen.add(row["slug"])
        docs.append(Doc(
            vendor="renesas", doc_id=row["slug"], doc_type=kind, version=_rev(row["slug"]),
            title=row["slug"].replace("-", " ").title(), url=row["url"], author=AUTHOR,
            family=[fam], desc="",
            verify_id=False,            # slugs are not printed inside the documents
            aliases=[row["slug"].replace("-", " ")]))
    return docs


if __name__ == "__main__":
    import collections
    ds = enumerate_docs(types=sys.argv[1].split(",") if len(sys.argv) > 1 else None)
    print(f"{len(ds)} documents, {len({f for d in ds for f in d.family})} families")
    for k, v in collections.Counter(d.doc_type for d in ds).most_common():
        print(f"  {k:<20} {v}")
