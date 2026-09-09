#!/usr/bin/env python3
"""NXP adapter.

Harder than ST: enumeration is open, but roughly half the Arm-MCU catalogue —
and nearly every Reference Manual — sits behind a login. This adapter handles the
open path completely and marks the rest `gated`, for the browser flow described in
references/nxp.md. It deliberately does not try to log in or accept licences.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, get_json   # noqa: E402

USB_SCOPE = None
USB_SCOPE_NOTE = ("the document taxonomy classifies by product line, not by peripheral, so USB capability cannot be derived from the index. No device filter is applied; narrow with --device")

AUTHOR = "NXP Semiconductors"
PACE_SECONDS = 2

# The literal braces are part of the path — the API takes its query as a brace-wrapped
# blob, not as normal query parameters. Keep them.
API = ("https://www.nxp.com/webapp-rest/api/search/getAsset/allResults/"
       "{{collection=documents&depth=2&start={start}&max={max}&sorting=sort_date.desc"
       "&language=en&query=typeTax>>{type_tax}::deviceTax>>{device_tax}&app=DocLibrary"
       "&parameters=typeTax.type.deviceTax.applicationTax.DocLanguage_en."
       "DocLanguage_zh.DocLanguage_ja.application}}")

TYPE_TAX = {
    "errata": "t522", "datasheet": "t520", "reference-manual": "t877",
    "user-manual": "t1009", "application-note": "t789", "user-guide": "t792",
}
# NXP's own URL slug per kind, for the direct-PDF fallback.
TYPE_SLUG = {
    "errata": "errata", "datasheet": "data-sheet", "reference-manual": "reference-manual",
    "user-manual": "user-manual", "application-note": "application-note",
    "user-guide": "user-guide",
}
# deviceTax needs the FULL path — a bare leaf code returns 0 results.
DEVICE_TAX = {
    "Arm MCU": "c731_c1770",          # Processors and Microcontrollers > Arm Microcontrollers
    "general-purpose": "c731_c1770_c173",
    "i.MX RT": "c731_c1770_c1508",
    "LPC2000": "c731_c1770_c750",     # ARM7 — legacy
    "LPC3000": "c731_c1770_c753",     # ARM9 — legacy
}


# NXP's index carries no family field — the family lives in the document code, which is
# why a USB filter had nothing to attach to. These patterns cover the Arm-MCU catalogue;
# a code that matches nothing gets no family and is therefore never excluded by the USB
# filter (fail open: an unclassified document is kept, not silently dropped).
_FAMILY_RULES = [
    (re.compile(r"^IMXRT(\d{3,4})", re.I),      lambda m: f"i.MX RT{m.group(1)}"),
    (re.compile(r"^MCX([ANWEC])", re.I),         lambda m: f"MCX {m.group(1).upper()}"),
    # One LPC rule, not two: LPC55S6X -> LPC55 (two leading digits are the family),
    # while LPC8N04 keeps its whole code because "LPC8N0" is a truncation, not a family.
    (re.compile(r"^LPC(\d{2}|\d[A-Z]\d{0,2})", re.I), lambda m: f"LPC{m.group(1).upper()}"),
    (re.compile(r"^(?:S9?)?KEA", re.I),          lambda m: "KEA"),   # KEA128 as well as S9KEA
    (re.compile(r"^FRDM[-_]?([A-Z0-9]+)", re.I), lambda m: f"FRDM {m.group(1).upper()}"),
    (re.compile(r"^S32K", re.I),                 lambda m: "S32K"),
    (re.compile(r"^KINETIS[_-]?([A-Z])", re.I),  lambda m: f"Kinetis {m.group(1).upper()}"),
    (re.compile(r"^M?K([VLEMW])\d", re.I),       lambda m: f"Kinetis {m.group(1).upper()}"),
    (re.compile(r"^M?K(\d{2})", re.I),           lambda m: f"Kinetis K{m.group(1)}"),
]


# Document codes carry a type prefix as often as not — ES_IMXRT1160BCE, ES-RT600,
# ES_LPC55S6X — and the family rules are anchored, so an unstripped prefix made 1,087
# of 1,683 imported books look unclassifiable. Strip it before matching.
_CODE_PREFIX = re.compile(r"^(ES|UM|AN|DS|RM)[_-]", re.I)
# i.MX RT parts also appear without the IMX: RT600, RT1170.
_RT_BARE = re.compile(r"^RT(\d{3,4})", re.I)


def family_of(code: str) -> str | None:
    code = _CODE_PREFIX.sub("", (code or "").strip())
    m = _RT_BARE.match(code)
    if m:
        return f"i.MX RT{m.group(1)}"
    for rx, fmt in _FAMILY_RULES:
        m = rx.match(code)
        if m:
            return fmt(m)
    return None


def _page(type_tax: str, device_tax: str, start: int, max_: int = 100) -> dict:
    return get_json(API.format(start=start, max=max_, type_tax=type_tax,
                               device_tax=device_tax), timeout=45)


def enumerate_docs(types=None, device="Arm MCU", limit=None) -> list:
    """Enumerate NXP docs for one device-taxonomy branch.

    `device` is a key of DEVICE_TAX. Use 'Arm MCU' for everything modern; the LPC2000
    and LPC3000 branches are ARM7/ARM9 legacy parts you usually want to exclude.
    """
    device_tax = DEVICE_TAX.get(device, device)
    out: dict = {}
    for kind in (types or list(TYPE_TAX)):
        tax = TYPE_TAX.get(kind)
        if tax is None:
            continue
        start, total = 0, None
        while total is None or start < total:
            try:
                d = _page(tax, device_tax, start)
            except (RuntimeError, json.JSONDecodeError) as e:
                print(f"  {kind}: page at {start} failed ({e}); keeping what we have",
                      file=sys.stderr)
                break
            total = int(d.get("totalcount") or 0)
            results = d.get("results") or []
            if not results:
                break
            for r in results:
                md = r.get("metaData") or {}
                code = (md.get("code") or "").strip()
                if not code:
                    continue
                # `language=en` in the query is not honoured for translated variants:
                # AN10974_ZH comes back alongside AN10974. Importing both files the
                # same application note twice, once in Chinese.
                if re.search(r"_(ZH|JA|CN|JP)$", code, re.I):
                    continue
                url = r.get("url") or ""
                out[code] = Doc(
                    vendor="nxp", doc_id=code, doc_type=kind,
                    version=str(md.get("RevisionNo") or r.get("RevisionNo") or "") or None,
                    title=(r.get("title") or "").strip(),
                    url=url, author=AUTHOR,
                    family=[f for f in [family_of(code)] if f] or
                           ([device] if device != "Arm MCU" else []),
                    desc=(r.get("summary") or "").strip())
            start += len(results)
            if limit and len(out) >= limit:
                break
            if start < total:
                time.sleep(PACE_SECONDS)
    return list(out.values())


def is_gated(doc: Doc) -> bool:
    """`webapp/Download?colCode=` and `ext_download.jsp` redirect to login.nxp.com."""
    return "webapp/" in doc.url or "ext_download" in doc.url


def direct_url(doc: Doc) -> str:
    """Unauthenticated path that rescues a good share of gated-looking docs. Always
    worth trying before falling back to the browser."""
    return f"https://www.nxp.com/docs/en/{TYPE_SLUG.get(doc.doc_type, doc.doc_type)}/{doc.doc_id}.pdf"


# Mask-set errata are the one place where "same-ish name = same doc" is badly wrong:
# KINETIS_K_0N50M covers MK22FN parts and KINETIS_V_0N50M covers MKV31F — different
# products, not revisions of each other. Dedup on the doc code alone (which the
# identifier already does) and never on mask/title similarity. If two entries really
# do look like duplicates, confirm by reading the "applies to mask X for these
# products" list inside the PDF with pdftotext before removing either.

if __name__ == "__main__":
    kinds = sys.argv[1].split(",") if len(sys.argv) > 1 else ["errata"]
    docs = enumerate_docs(types=kinds, limit=20)
    print(f"{len(docs)} docs")
    for d in docs[:10]:
        flag = "GATED" if is_gated(d) else "open"
        print(f"  {d.doc_id:<22} {d.doc_type:<16} Rev {d.version or '?':<5} {flag}")
