#!/usr/bin/env python3
"""Geehy adapter (APM32).

Geehy publishes no index a headless client can read — the download page renders its
list client-side, and the browser route is unusable here because the site freezes the
renderer (two 45-second CDP timeouts before I gave up on it). What it does have is a
flat, guessable document path:

    https://www.geehy.com/uploads/tool/<PART> <kind> <Vx.y>.pdf

⚠️ The separators are **non-breaking spaces** (%C2%A0), not ordinary ones — a plain
%20 URL 404s for most documents. Both are tried.

So enumeration here is probing a small candidate grid (part x kind x version) built
from the TinyUSB BSP part names, keeping only URLs that return a real PDF. Every entry
is therefore verified, but a document whose name falls outside the grid is invisible —
say so rather than presenting this as a catalogue read.

Versions are part of the filename, so several revisions of the same document coexist;
only the newest of each (part, kind) is offered, which is what a library wants.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, http_get, rev_newer   # noqa: E402

AUTHOR = "Geehy Semiconductor"
BASE = "https://www.geehy.com/uploads/tool/"
NBSP = "%C2%A0"

USB_SCOPE = None
USB_SCOPE_NOTE = "USB capability is read from the part datasheet"

# From hw/bsp/apm32f0xx. Add parts here as TinyUSB gains ports.
PARTS = ["APM32F072x8xB"]
KINDS = {"datasheet": "datasheet", "User Manual": "reference-manual"}
VERSIONS = [f"V{a}.{b}" for a in (1, 2) for b in range(0, 8)]


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    want = {f.upper() for f in families} if families else None
    best: dict = {}
    for part in PARTS:
        fam = re.match(r"(APM32F\d{3})", part, re.I).group(1).upper()
        if want and fam not in want:
            continue
        for label, kind in KINDS.items():
            if types and kind not in types:
                continue
            for ver in VERSIONS:
                for sep in (NBSP, "%20"):
                    url = f"{BASE}{part}{sep}{label.replace(' ', sep)}{sep}{ver}.pdf"
                    try:
                        http_get(url, timeout=20, accept="application/pdf",
                                 validate=lambda b: b.startswith(b"%PDF"))
                    except RuntimeError:
                        continue
                    key = (part, kind)
                    v = ver.lstrip("Vv")
                    if key not in best or rev_newer(v, best[key][0]):
                        best[key] = (v, url, fam)
                    break
    docs = []
    for (part, kind), (ver, url, fam) in sorted(best.items()):
        docs.append(Doc(
            vendor="geehy", doc_id=f"{part}-{kind}", doc_type=kind, version=ver,
            title=f"{part} {kind.replace('-', ' ').title()}", url=url, author=AUTHOR,
            family=[fam], desc="", verify_id=False, aliases=[f"{part} {kind}"]))
    return docs


if __name__ == "__main__":
    for d in enumerate_docs():
        print(f"  {d.doc_id:<34} {d.doc_type:<18} {d.family[0]:<12} v{d.version}")
