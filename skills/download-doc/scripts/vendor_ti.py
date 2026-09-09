#!/usr/bin/env python3
"""Texas Instruments adapter (MSP430, MSP432E4, TM4C).

TI needs no document index at all, because its datasheet URLs are derived from the
part number: `ti.com/lit/ds/symlink/<part>.pdf`. Verified for msp430f5529,
msp432e401y, tm4c123gh6pm and tm4c1294ncpdt.

That leaves the opposite problem — where does the *part list* come from? Here it comes
from the TinyUSB BSP tree, which is the reason we want these documents in the first
place. A part TinyUSB doesn't support isn't in scope, so scanning `hw/bsp` both
supplies the list and bounds it. Every candidate is probed and only real PDFs are kept,
so a wrong guess yields nothing rather than a phantom entry.

⚠️ Datasheets only. Errata and technical reference manuals are published under TI
literature numbers (`/lit/er/slaz…`, `/lit/ug/spmu…`) that cannot be derived from a
part number — those need the browser route, and are reported as missing rather than
silently omitted.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, http_get, last_modified   # noqa: E402

AUTHOR = "Texas Instruments"
BSP = Path.home() / "code" / "tinyusb" / "hw" / "bsp"
DS = "https://www.ti.com/lit/ds/symlink/{}.pdf"

USB_SCOPE = None
USB_SCOPE_NOTE = "USB capability is read from each part's datasheet"

# BSP directory -> family label, and the part-number shape to look for inside it.
FAMILIES = {
    "msp430":    ("MSP430",   re.compile(r"\bmsp430[a-z]\d{3,5}\b", re.I)),
    "msp432e4":  ("MSP432E4", re.compile(r"\bmsp432e\d{3}[a-z]?\b", re.I)),
    "tm4c":      ("TM4C",     re.compile(r"\btm4c\d{3,4}[a-z0-9]{3,7}\b", re.I)),
}


def _parts_from_bsp() -> dict:
    """{part: family} for every TI part named anywhere under its BSP directory."""
    out = {}
    for d, (fam, rx) in FAMILIES.items():
        root = BSP / d
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            out.update({m.lower(): fam for m in rx.findall(f.name)})
            if not f.is_file() or f.suffix.lower() not in (".c", ".h", ".mk", ".cmake", ".txt", ".ld", ""):
                continue
            try:
                for m in rx.findall(f.read_text(errors="ignore")):
                    out[m.lower()] = fam
            except OSError:
                continue
    return out


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    if types and "datasheet" not in types:
        return []
    want = {f.upper() for f in families} if families else None
    docs = []
    for part, fam in sorted(_parts_from_bsp().items()):
        if want and fam not in want:
            continue
        url = DS.format(part)
        try:                       # probe: only real PDFs become documents
            http_get(url, timeout=30, accept="application/pdf",
                     validate=lambda b: b.startswith(b"%PDF"))
        except RuntimeError:
            print(f"  no datasheet at ti.com/lit/ds/symlink/{part}", file=sys.stderr)
            continue
        docs.append(Doc(
            vendor="ti", doc_id=part.upper(), doc_type="datasheet",
            version=last_modified(url), title=f"{part.upper()} Datasheet",
            url=url, author=AUTHOR, family=[fam], desc="", verify_id=False,
            aliases=[f"{part.upper()} Datasheet", part.upper()]))
    return docs


if __name__ == "__main__":
    for d in enumerate_docs():
        print(f"  {d.doc_id:<18} {d.family[0]:<10} {d.version}")
