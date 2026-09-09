#!/usr/bin/env python3
"""Espressif adapter.

The simplest vendor of the three: one HTML page lists the entire catalogue with
version and release date per row, every PDF is public, and there is no login, no
licence, and no throttling worth pacing around.

There is no document-code scheme like ST's DS/RM/ES or NXP's mask codes — Espressif
identifies a document only by its filename. So the PDF filename stem *is* the
document ID (`esp32-s3_technical_reference_manual_en`), which is stable across
revisions because the version lives in a separate column rather than in the name.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, http_get   # noqa: E402

INDEX = "https://www.espressif.com/en/support/documents/technical-documents"
AUTHOR = "Espressif Systems"
CACHE = Path.home() / ".cache" / "download-doc" / "espressif"

# Chips whose USB peripheral is a real OTG controller (device *and* host), as opposed
# to the fixed-function USB-Serial/JTAG bridge on C3/C6/H2 etc. This is the set that
# matters for TinyUSB work.
#
# ⚠️ This list goes stale, and stale here means silently returning fewer documents than
# the user asked for. It was first written from memory and omitted the ESP32-S31 —
# a part that shipped after the model's knowledge cutoff and carries USB 2.0 *High-Speed*
# OTG. Re-derive it from the vendor rather than from recall: pull each candidate's
# datasheet and grep page 1-6 for "OTG", e.g.
#   pdftotext -f 1 -l 6 esp32-<chip>_datasheet_en.pdf - | grep -i otg
# "USB Serial/JTAG controller" alone is not OTG; look for the explicit "USB 2.0 ... OTG".
# Default device scope: parts with a programmable USB controller. USB-Serial/JTAG-only
# parts (C3, C6, H2, C5, C61) are deliberately absent — that peripheral is a fixed
# function bridge you cannot write a USB stack against. Add them here if you want their
# documents by default; naming a chip explicitly bypasses this either way.
USB_SCOPE = {"ESP32-S2", "ESP32-S3", "ESP32-S31", "ESP32-P4"}
USB_SCOPE_NOTE = "chips with a programmable USB (OTG) controller; USB-Serial/JTAG-only parts excluded"

OTG_CHIPS = {
    "esp32-s2": "ESP32-S2",     # USB OTG, Full-Speed
    "esp32-s3": "ESP32-S3",     # USB OTG, Full-Speed
    "esp32-s31": "ESP32-S31",   # USB 2.0 High-Speed OTG (verified in datasheet v0.5)
    "esp32-p4": "ESP32-P4",     # USB 2.0 High-Speed OTG
}

TYPE_PATTERNS = [
    (r"technical reference manual", "reference-manual"),
    (r"\berrata\b", "errata"),
    (r"application note", "application-note"),
    (r"\bdatasheet\b", "datasheet"),
    (r"hardware design guidelines|design guide", "user-manual"),
    (r"user guide|getting started", "user-manual"),
]

_SDK_DOC = re.compile(r"/projects/(esp-idf|esp-at|esp-adf|esp-sr|esp-matter|esp-zigbee)/", re.I)
_SDK_TITLE = re.compile(r",\s*SDK\b|programming guide\b", re.I)

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r'views-field-([a-z0-9-]+)"?\s*>\s*(.*?)\s*</td>', re.S)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def _index_html(refresh: bool = False) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "technical-documents.html"
    if f.exists() and not refresh:
        return f.read_text(encoding="utf-8", errors="replace")
    data = http_get(INDEX, timeout=45, accept="text/html",
                    validate=lambda b: b"views-field-field-version" in b)
    f.write_bytes(data)
    return data.decode("utf-8", errors="replace")


def _classify(title: str) -> str | None:
    t = title.lower()
    for pattern, kind in TYPE_PATTERNS:
        if re.search(pattern, t):
            return kind
    return None


def _resolve_pdf(url: str) -> str | None:
    """Some entries link to an HTML doc on docs.espressif.com rather than a PDF —
    every SoC errata does, which is how they silently vanish from a `.pdf`-only
    filter. Those pages carry a link to their own PDF build, so follow it rather
    than guessing the filename: the obvious `...-en-latest-<chip>.pdf` 404s while
    the real one is `...-en-master-<chip>.pdf`."""
    if url.lower().endswith(".pdf"):
        return url
    if "docs.espressif.com" not in url:
        return None
    try:
        page = http_get(url, timeout=30, accept="text/html",
                        validate=lambda b: b"<html" in b.lower()).decode("utf-8", "replace")
    except RuntimeError:
        return None
    m = re.search(r'href="([^"]+\.pdf)"', page)
    return urljoin(url, m.group(1)) if m else None


def enumerate_docs(chips=None, types=None, refresh=False) -> list:
    """Enumerate Espressif documents.

    chips: filenames/titles must mention one of these (default: the USB-OTG parts).
           Pass an empty list to disable chip filtering entirely.
    """
    want = OTG_CHIPS if chips is None else {c.lower(): c.upper() for c in chips}
    docs, seen = [], set()
    for row in _ROW.findall(_index_html(refresh)):
        cells = {k: v for k, v in _CELL.findall(row)}
        href = re.search(r'href="([^"]+)"', row)
        if not href or "title" not in cells:
            continue
        title = _text(cells["title"])
        url = href.group(1)
        kind = _classify(title)
        if kind is None or (types and kind not in types):
            continue
        # Espressif lists its *software* documentation alongside the silicon docs, one
        # entry per SDK release — 28 ESP-IDF/ESP-AT/ESP-ADF/ESP-SR guides for the USB
        # parts alone, nearly all HTML-only. They are not MCU technical documents and
        # they republish on every point release, so they are excluded by default; ask
        # for them with --all-devices --types user-manual if you actually want them.
        if _SDK_DOC.search(url) or _SDK_TITLE.search(title):
            continue
        hay = f"{url} {title}".lower()
        # Match the chip as a whole token. A plain substring test is wrong in both
        # directions here: "esp32-s3" occurs inside "esp32-s31" (mislabelling every
        # S31 document as S3), and a module doc like "ESP32-S3-WROOM-1 Datasheet"
        # names the chip without being about it.
        fams = [label for key, label in want.items()
                if re.search(rf"{key}(?![a-z0-9])", hay)] if want else []
        if want and not fams:
            continue
        pdf = _resolve_pdf(url)
        if pdf is None:
            print(f"  no PDF build for {title!r} ({url}) — skipped", file=sys.stderr)
            continue
        stem = Path(pdf).stem            # the document's only stable ID
        if stem in seen:                 # the index lists every document twice
            continue
        seen.add(stem)
        docs.append(Doc(
            vendor="espressif", doc_id=stem, doc_type=kind,
            version=_text(cells.get("field-version", "")) or None,
            title=title, url=pdf, author=AUTHOR, family=fams,
            desc=_text(cells.get("body", "")),
            # Books added by hand were titled from the PDF filename with underscores
            # turned into spaces — that is exactly what the pre-existing Espressif
            # books here look like ("esp32-s3 errata en"), so match rather than
            # duplicate them. Errata also moved from a per-chip PDF to a shared
            # esp-chip-errata build, so offer the old name too.
            # The ID is the PDF filename, which appears nowhere in the document, so
            # there is nothing on page 1 to check it against.
            verify_id=False,
            aliases=[stem.replace("_", " "), title,
                     *(f"{c.lower()} errata en" for c in fams if kind == "errata")]))
    return docs


if __name__ == "__main__":
    kinds = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    for d in enumerate_docs(types=kinds):
        print(f"  {d.doc_id:<46} {d.doc_type:<18} {d.version or '?':<7} {','.join(d.family)}")
