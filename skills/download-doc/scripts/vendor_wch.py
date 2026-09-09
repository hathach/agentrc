#!/usr/bin/env python3
"""WCH / Nanjing Qinheng adapter (CH32F, CH32V, CH58x).

WCH looks browser-only and isn't. The download listing renders client-side, and the
document page injects its download link after load, so a plain fetch of either yields
nothing — which is where the browser route would normally start. But the page gets that
link from a JSON API that answers headless clients perfectly well:

    /api/official/website/common/relationFiles?fileName=<NAME>_PDF.html
    -> {id, name, content, version, size, uploadTime, relationFileList[...]}

and the file itself comes from `/download/file?id=<id>`, also headless. So the whole
vendor is reachable without Chrome; finding that took looking at the page's own network
requests rather than its HTML.

Enumeration follows `relationFileList`: each document names its siblings, so a handful
of seeds derived from the TinyUSB BSP directory names reaches the rest of the family by
breadth-first crawl. That is the only enumeration WCH offers — there is no index.
"""
from __future__ import annotations

import json
import re
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, http_get   # noqa: E402

AUTHOR = "Nanjing Qinheng Microelectronics (WCH)"
API = "https://www.wch-ic.com/api/official/website/common/relationFiles?fileName={}"
FILE = "https://www.wch-ic.com/download/file?id={}"

USB_SCOPE = None
USB_SCOPE_NOTE = "USB capability is read from each part's datasheet"

# Seeds: TinyUSB ports these families, and each seed pulls in its relatives.
SEEDS = ["CH32V307DS0_PDF.html", "CH32V203DS0_PDF.html", "CH32V103DS0_PDF.html",
         "CH32F103DS0_PDF.html", "CH32F203DS0_PDF.html", "CH583DS1_PDF.html",
         "CH32FV2x_V3xRM_PDF.html", "CH32V10xRM_PDF.html", "CH585DS1_PDF.html"]

KIND = [
    (re.compile(r"RM\b|reference", re.I), "reference-manual"),
    (re.compile(r"DS\d|datasheet", re.I), "datasheet"),
    (re.compile(r"errata", re.I), "errata"),
]


def _kind(name: str) -> str | None:
    for rx, k in KIND:
        if rx.search(name):
            return k
    return None


def _fetch(file_name: str) -> dict | None:
    try:
        d = http_get(API.format(file_name), timeout=30, accept="application/json",
                     validate=lambda b: b.lstrip()[:1] == b"{")
    except RuntimeError:
        return None
    j = json.loads(d)
    return j.get("data") if j.get("code") == 0 else None


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    want = {f.upper() for f in families} if families else None
    seen, docs, queue, have = set(), [], deque(SEEDS), set()
    while queue:
        page = queue.popleft()
        if page in seen:
            continue
        seen.add(page)
        data = _fetch(page)
        if not data:
            continue
        for rel in (data.get("relationFileList") or []):   # crawl the family graph
            n = rel.get("name") or ""
            if n:
                queue.append(re.sub(r"\.(pdf|zip|exe)$", "_\\1.html", n, flags=re.I).upper()
                             .replace("_PDF.HTML", "_PDF.html"))
        name = data.get("name") or ""
        kind = _kind(name)
        if kind is None or (types and kind not in types):
            continue
        # Family is the part prefix that precedes the document-type token. Matching
        # (CH\d{2,3}[A-Z]?\d?) instead swallowed the D of "DS1" and produced CH583D.
        m = re.match(r"(CH\d+[A-Z]*\d*?)(?=DS\d|RM\b|_|\.)", name, re.I)
        fam = m.group(1).upper() if m else None
        if fam is None or (want and fam not in want):
            continue
        stem = re.sub(r"\.pdf$", "", name, flags=re.I)
        if stem in have:            # the relation graph reaches the same doc many ways
            continue
        have.add(stem)
        docs.append(Doc(
            vendor="wch", doc_id=re.sub(r"\.pdf$", "", name, flags=re.I),
            doc_type=kind, version=str(data.get("version") or "") or None,
            title=(data.get("content") or name).split(".")[0][:110],
            url=FILE.format(data["id"]), author=AUTHOR, family=[fam],
            desc=(data.get("content") or "")[:300], verify_id=False,
            aliases=[re.sub(r"\.pdf$", "", name, flags=re.I)]))
    return docs


if __name__ == "__main__":
    for d in enumerate_docs():
        print(f"  {d.doc_id:<22} {d.doc_type:<18} {d.family[0]:<10} v{d.version}")
