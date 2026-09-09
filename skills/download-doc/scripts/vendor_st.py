#!/usr/bin/env python3
"""STMicroelectronics adapter.

ST publishes everything openly through the resource-grid JSON behind st.com's
product pages, so enumeration needs no login and PDFs download directly.

One grid per product series plus one class-wide grid. The class-wide grid (CL1734)
carries cross-series material — application notes, reference manuals, user manuals —
but *no datasheets or errata*; those only appear in the per-series grids. So if you
want a part's datasheet you must fetch its series.

st.com rate-limits: a handful of grid fetches in quick succession and it starts
dropping connections for a while. Grids are cached on disk and paced apart for that
reason — a wget failure here usually means "backed off", not "wrong URL".
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, cap_families, http_get   # noqa: E402

GRID = "https://www.st.com/bin/st/selectors/cxst/en.cxst-rs-grid.html/{}.all.json"
BASE = "https://www.st.com"
USB_SCOPE = None
USB_SCOPE_NOTE = ("USB capability is not stated per series anywhere in the resource grid. Mining the datasheet blurbs finds it for 10 of 24 series and misses STM32G4/L5/U5/WB, which do have USB — so the heuristic produces false negatives and would silently drop whole series. No device filter is applied; narrow with --family")

AUTHOR = "STMicroelectronics"
CACHE = Path.home() / ".cache" / "download-doc" / "st"
PACE_SECONDS = 3          # be a polite guest; ST throttles fast loops

# Grid ID -> family tag. CL1734 is the class-wide grid (no family of its own).
SERIES = {
    "CL1734": None,   "SS2200": "STM32C0", "SS2361": "STM32C5", "SS1574": "STM32F0",
    "SS1031": "STM32F1", "SS1575": "STM32F2", "SS1576": "STM32F3", "SS1577": "STM32F4",
    "SS1858": "STM32F7", "SS1960": "STM32G0", "SS2024": "STM32G4", "SS2186": "STM32H5",
    "SS1951": "STM32H7", "SS1817": "STM32L0", "SS2002": "STM32L4+", "SS1580": "STM32L4",
    "SS2025": "STM32L5", "SS2328": "STM32N6", "SS2133": "STM32U0", "SS2327": "STM32U3",
    "SS2134": "STM32U5", "SS1961": "STM32WB", "SS2337": "STM32WB0", "SS2261": "STM32WBA",
    "SS2026": "STM32WL",
}

# Keyed and looked up lowercase for robustness. ST is actually consistent — audited
# across all 25 grids, 20 distinct resourceType strings, zero case-variant collisions —
# so this is defensive, not a workaround for a vendor quirk. Don't "simplify" it back
# to exact matching, though: this table originally carried "Errata sheet", a spelling
# that exists nowhere in the feed, and the exact-match lookup then dropped all 124
# errata in the catalogue while reporting a clean "0 found". Case-folding makes a
# guessed key survive; nothing else here would have caught it.
TYPE_MAP = {
    "datasheet": "datasheet", "errata sheet": "errata",
    "reference manual": "reference-manual", "user manual": "user-manual",
    "programming manual": "programming-manual", "application note": "application-note",
    "technical note": "technical-note", "data brief": "data-brief",
}


def _grid(grid_id: str, refresh: bool = False) -> list:
    """Fetch one grid, cached. Returns rows (possibly empty if ST is throttling)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{grid_id}.json"
    if f.exists() and not refresh:
        return json.loads(f.read_text()).get("rows", [])
    try:
        data = http_get(GRID.format(grid_id), timeout=25)
    except RuntimeError:
        if f.exists():
            print(f"  {grid_id}: fetch failed, using cached copy", file=sys.stderr)
            return json.loads(f.read_text()).get("rows", [])
        print(f"  {grid_id}: fetch failed and nothing cached — ST is likely throttling; "
              f"wait a few minutes and re-run (progress is cached)", file=sys.stderr)
        return []
    f.write_bytes(data)
    time.sleep(PACE_SECONDS)
    return json.loads(data).get("rows", [])


def _part_from_link(link: str) -> str | None:
    """Datasheet descriptions are generic ('32-bit Arm Cortex-M7 480MHz MCU...'), so
    the only place the actual part number appears is the PDF filename."""
    m = re.search(r"/datasheet/([a-z0-9]+)\.pdf", link or "", re.I)
    return m.group(1).upper() if m else None


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    """Enumerate ST docs, merged across grids keeping the highest revision seen.

    families: e.g. ['STM32H7'] — None means every grid (slow; ~25 fetches).
    types:    normalized kinds, e.g. ['datasheet', 'errata'].
    """
    grids = [g for g, fam in SERIES.items()
             if families is None or fam is None or fam in families]
    if families:   # a named family implies its grid; drop the class-wide one unless asked
        grids = [g for g in grids if SERIES[g] in families]

    found: dict = {}
    for gid in grids:
        fam = SERIES[gid]
        for row in _grid(gid, refresh):
            kind = TYPE_MAP.get((row.get("resourceType") or "").strip().lower())
            if kind is None or (types and kind not in types):
                continue
            doc_id = (row.get("title") or "").strip()
            link = (row.get("localizedLinks") or {}).get("en") or ""
            if not doc_id or not link:
                continue
            desc = ((row.get("localizedDescriptions") or {}).get("en") or "").strip()
            title = desc
            if kind == "datasheet":
                part = _part_from_link(link)
                if part:
                    title = f"{part} — {desc}" if desc else part
            prev = found.get(doc_id)
            if prev is None:
                found[doc_id] = Doc(
                    vendor="st", doc_id=doc_id, doc_type=kind,
                    version=str(row.get("version") or "") or None,
                    title=title, url=link if link.startswith("http") else BASE + link,
                    author=AUTHOR, family=[fam] if fam else [], desc=desc)
            else:
                if fam and fam not in prev.family:
                    prev.family.append(fam)      # doc shared across series
                from doclib import rev_newer
                if rev_newer(row.get("version"), prev.version):
                    prev.version = str(row.get("version"))
    for d in found.values():
        # AN1709 genuinely applies to all 24 series; 25 tags on one book makes the
        # tag browser unusable, so collapse the cross-cutting ones.
        d.family = cap_families(d.family, 6, "stm32-wide")
    return list(found.values())


# When you know only an ID, this prefix-resolves: the trailing hyphen is required
# (es0392 404s, es0392- works) because ST's paths are <id>-<slug>.pdf.
def url_from_id(doc_id: str, kind: str) -> str:
    return f"{BASE}/resource/en/{kind.replace('-', '_')}/{doc_id.lower()}-.pdf"


if __name__ == "__main__":
    docs = enumerate_docs(types=sys.argv[1].split(",") if len(sys.argv) > 1 else None)
    print(f"{len(docs)} docs")
    for d in docs[:10]:
        print(f"  {d.doc_id:<10} {d.doc_type:<18} Rev {d.version or '?':<5} {d.title[:56]}")
