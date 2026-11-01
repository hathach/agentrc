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
of seeds, one or two per family we work with, reaches the rest of the family by
breadth-first crawl. That is the only enumeration WCH offers — there is no index.

The graph reaches the whole catalogue (CH569 lists the CH132 PHY, which lists the USB
bridges), so the crawl keeps to MCU families and follows only their pages. Page
descriptions cannot tell an MCU (the CH32V003 datasheet never says "MCU"; the CH132
PHY's page names MCUs as its host); WCH's numbering can: CH32*, CH5xx and CH64x are MCUs,
QingKe the RISC-V cores, CH63x USB hubs, the rest PHYs, bridges and PD or I/O chips.

wch.cn serves the Chinese editions through the same API under the same file names,
often a revision ahead of the English site, and adds Chinese-only documents and parts.
A Chinese edition files as `<NAME>-CN` beside the English one.
"""
from __future__ import annotations

import json
import re
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, begin_enumeration, http_get, partial_enumeration   # noqa: E402

AUTHOR = "Nanjing Qinheng Microelectronics (WCH)"
API = "https://{}/api/official/website/common/relationFiles?fileName={}"
FILE = "https://{}/download/file?id={}"
# Identifier suffix -> (API host, file host, language); www.wch.cn/download answers with
# its web app.
SITES = {"": ("www.wch-ic.com", "www.wch-ic.com", "eng"),
         "-CN": ("www.wch.cn", "file.wch.cn", "zho")}

USB_SCOPE = None
USB_SCOPE_NOTE = "USB capability is read from each part's datasheet"

# Seeds: TinyUSB families, parts we hold boards for, and USB MCUs whose pages no other MCU
# page links (CH545, CH547); each pulls in the relatives its page lists. CH32V407DS0 lists
# none, so its RM is seeded too. Every seed must name a page on both sites: one the API
# does not know makes the run partial.
SEEDS = ["CH32V307DS0_PDF.html", "CH32V203DS0_PDF.html", "CH32V103DS0_PDF.html",
         "CH32F103DS0_PDF.html", "CH32F203DS0_PDF.html", "CH583DS1_PDF.html",
         "CH32FV2x_V3xRM_PDF.html", "CH585DS1_PDF.html",
         "CH32V407DS0_PDF.html", "CH32V407RM_PDF.html", "CH32H417DS0_PDF.html",
         "CH32X315DS0_PDF.html", "CH569DS1_PDF.html", "CH564DS0_PDF.html",
         "CH572DS1_PDF.html", "CH552DS1_PDF.html", "CH643DS0_PDF.html",
         "CH545DS1_PDF.html", "CH547DS1_PDF.html"]

MCU_FAMILY = re.compile(r"CH32|CH5\d\d|CH64\d|QingKe", re.I)
# Family and kind are what precede and make the name's last token, x placeholders kept as
# written: CH32FV2x_V3xRM -> CH32FV2x_V3x, CH32xRM -> CH32x, QingKeV4_Processor_Manual ->
# QingKeV4.
DOC_NAME = re.compile(r"(.+?)_?(DS\d|RM|errata|processor_manual)$", re.I)
KIND = {"RM": "reference-manual", "ERRATA": "errata", "PROCESSOR_MANUAL": "programming-manual"}


def _fetch(host: str, file_name: str) -> dict | None:
    try:
        d = http_get(API.format(host, file_name), timeout=30, accept="application/json",
                     validate=lambda b: b.lstrip()[:1] == b"{")
    except RuntimeError:
        # Its relatives are reached only through this page, so the crawl is now partial.
        partial_enumeration(f"{host} {file_name}: fetch failed")
        return None
    j = json.loads(d)
    # An unknown name is {"code": 0, "data": null} (seen 2026-10-27); any other code is a
    # failure that proves nothing about the page.
    if j.get("code") != 0:
        partial_enumeration(f"{host} {file_name}: API answered code {j.get('code')!r}: {j.get('message')}")
        return None
    return j.get("data")


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    begin_enumeration()
    want = {f.upper() for f in families} if families else None
    with ThreadPoolExecutor(len(SITES)) as ex:   # two hosts, no shared state
        crawls = list(ex.map(lambda item: _crawl(*item[1], item[0]), SITES.items()))
    return [d for docs in crawls for d in docs
            if (not types or d.doc_type in types) and (not want or d.family[0].upper() in want)]


def _crawl(host: str, file_host: str, language: str, suffix: str) -> list:
    """Every document reached from the seeds through MCU-family pages."""
    seen, docs, queue = set(), [], deque(SEEDS)
    while queue:
        page = queue.popleft()
        if page.upper() in seen:
            continue
        seen.add(page.upper())
        data = _fetch(host, page)
        if not data:
            if page in SEEDS:   # a dead seed silently loses the family it was there for
                partial_enumeration(f"{host} seed {page}: unknown to the API")
            continue
        doc = _doc(data, file_host, language, suffix)
        if doc is not None:
            docs.append(doc)
        for rel in (data.get("relationFileList") or []):   # crawl the family graph
            n = rel.get("name") or ""
            # An EVT zip or a tool has no page of its own.
            if n.upper().endswith(".PDF") and MCU_FAMILY.match(n):
                queue.append(n[:-4] + "_PDF.html")
    return docs


def _title(content: str) -> str:
    """The description's first clause: "CH32V303/305/307/317 datasheet, this series ..."
    keeps the part before the comma, "USB3.0" and "2.4G" keep their point, and an ASCII
    comma between part numbers ("CH32V407,CH32V467的...") lists them, not ends the clause."""
    content = re.sub(r"^Information change in the download page \([^)]*\):\s*", "", content)
    return re.split(r"[。，]|,(?![A-Za-z0-9])|\.\s", content)[0].strip()


def _doc(data: dict, file_host: str, language: str, suffix: str) -> Doc | None:
    name = data.get("name") or ""
    stem = re.sub(r"\.pdf$", "", name, flags=re.I)
    m = DOC_NAME.match(stem)
    if m is None:
        return None
    return Doc(
        vendor="wch", doc_id=stem + suffix,
        doc_type=KIND.get(m.group(2).upper(), "datasheet"),
        version=str(data.get("version") or "") or None,
        title=_title(data.get("content") or name),
        url=FILE.format(file_host, data["id"]), author=AUTHOR, family=[m.group(1)],
        desc=(data.get("content") or "")[:300], verify_id=False, language=language)


if __name__ == "__main__":
    for d in enumerate_docs():
        print(f"  {d.doc_id:<22} {d.doc_type:<18} {d.family[0]:<10} v{d.version}")
