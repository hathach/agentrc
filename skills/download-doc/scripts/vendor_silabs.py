#!/usr/bin/env python3
"""Silicon Labs adapter (EFM32).

Like TI, Silabs needs no index: document URLs are derived from the family slug.

    data-sheets/<family>-datasheet.pdf      reference-manuals/<family>-rm.pdf
    errata/<family>-errata.pdf

Verified for efm32gg12 (datasheet 3.2 MB, reference manual 17.2 MB).

The family slug is the part number with its memory/package suffix removed:
`efm32gg12b810f1024gm64` -> `efm32gg12`. The part list comes from the TinyUSB BSP,
which is both the source of the requirement and a natural bound on scope. Every
candidate URL is probed, so a slug that guesses wrong contributes nothing rather than
a broken entry.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, http_get, last_modified   # noqa: E402

AUTHOR = "Silicon Labs"
BSP = Path.home() / "code" / "tinyusb" / "hw" / "bsp" / "efm32"
BASE = "https://www.silabs.com/documents/public"

USB_SCOPE = None
USB_SCOPE_NOTE = "USB capability is read from each family's datasheet"

KINDS = {
    "datasheet":        ("data-sheets", "{}-datasheet.pdf"),
    "reference-manual": ("reference-manuals", "{}-rm.pdf"),
    "errata":           ("errata", "{}-errata.pdf"),
}

# efm32gg12b810f1024gm64 -> efm32gg12 : strip the die/flash/package tail.
_PART = re.compile(r"\b(efm32[a-z]{2}\d{1,2})[a-z]?\d*[a-z]\d+[a-z]{2}\d+\b", re.I)
_FAM = re.compile(r"\befm32[a-z]{2}\d{1,2}\b", re.I)


def _families_from_bsp() -> set:
    fams = set()
    if not BSP.is_dir():
        return fams
    for f in BSP.rglob("*"):
        text = f.name
        if f.is_file():
            try:
                text += " " + f.read_text(errors="ignore")
            except OSError:
                pass
        for m in _PART.findall(text):
            fams.add(m.lower())
        for m in _FAM.findall(text):
            fams.add(m.lower())
    return fams


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    want = {f.lower() for f in families} if families else None
    docs = []
    for fam in sorted(_families_from_bsp()):
        if want and fam not in want:
            continue
        for kind, (folder, pattern) in KINDS.items():
            if types and kind not in types:
                continue
            url = f"{BASE}/{folder}/{pattern.format(fam)}"
            try:
                http_get(url, timeout=30, accept="application/pdf",
                         validate=lambda b: b.startswith(b"%PDF"))
            except RuntimeError:
                continue          # this family simply has no document of that kind
            docs.append(Doc(
                vendor="silabs", doc_id=f"{fam}-{kind}", doc_type=kind,
                version=last_modified(url),
                title=f"{fam.upper()} {kind.replace('-', ' ').title()}",
                url=url, author=AUTHOR, family=[fam.upper()], desc="",
                verify_id=False, aliases=[f"{fam.upper()} {kind}"]))
    return docs


if __name__ == "__main__":
    print("families from BSP:", ", ".join(sorted(_families_from_bsp())) or "(none)")
    for d in enumerate_docs():
        print(f"  {d.doc_id:<28} {d.doc_type:<18} {d.version}")
