#!/usr/bin/env python3
"""Put each document's vendor number at the front of its Calibre title.

    UM10503 LPC43xx/LPC43Sxx User manual        (was: LPC43xx_LPC43Sxx User manual)
    ES0182 STM32F405 407 415 417 device errata  (was: ... device errata (ES0182))

Dry-run by default; --apply writes, and always after saving a rollback TSV of
id/old/new so every rename is individually reversible.

Two sources for the number, in order:

1. The book's vendor identifier, when it *is* a document number (UM10053, RM0433).
   Identifiers that are filename stems or part numbers are skipped — prepending
   "esp32-s3_technical_reference_manual_en" to its own title helps nobody.
2. Page 1 of the PDF, for books with no identifier at all — the hand-added ones,
   which is where this is most useful.

⚠️ The page-1 rule is the subtle part. Requiring the number to sit adjacent to the
revision ("ES0392 - Rev 15") is what stops an ST errata sheet being labelled with the
reference manual it cites on its cover. But NXP prints the number first and the
revision seventy characters later, so adjacency *alone* rejected UM10503 sitting in
plain sight and found 122 ids where 2,030 exist. Both rules run: a number leading
page 1, or a number beside its revision. Measured across the library they never
disagreed — worth re-checking with --audit if the rule is ever widened.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import doclib   # noqa: E402

LIB = doclib.LIBRARY
VENDORS = {"st", "nxp", "espressif", "rpi", "renesas", "microchip", "ti", "silabs",
           "allwinner", "wch", "hpmicro", "geehy"}
LEAD = re.compile(r"^(UM|AN|DS|ES|RM|PM|TN|UG|DRM)\d{3,8}\b", re.I)
DOCNUM = re.compile(r"^(?:UM|AN|DS|ES|RM|PM|TN|UG|DRM)[_-]?\d{3,6}$", re.I)
# Microchip trails its id, not always with the DS: USB2517-…-Data-Sheet-00001598C
MCHP = re.compile(r"(?:DS(\d{5,8})|(?<![0-9A-Za-z])(\d{8}))[A-Z]?$", re.I)
ANY = re.compile(r"\b(UM|AN|DS|ES|RM|PM|TN|UG|DRM)\s?(\d{3,8})\b", re.I)
ADJ = re.compile(r"\b(UM|AN|DS|ES|RM|PM|TN|UG|DRM)\s?(\d{3,8})\b\s*[-–—]?\s*(?:Rev|Revision|V\d)", re.I)


def _clean(title: str, doc: str) -> str:
    """Prepend the id, removing a copy it already carries — but only an exact one.
    A qualified parenthetical like "(UM11750-V3)" is what distinguishes three
    otherwise identically titled books, so it must survive."""
    t = re.sub(r"\s*\(\s*" + re.escape(doc) + r"\s*\)\s*", " ", title, flags=re.I)
    t = re.sub(r"[\s\-–—,:]*\b" + re.escape(doc) + r"\b\s*$", "", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", f"{doc} {t.strip(' -–—,:')}").strip()


def from_pdf(pdf: pathlib.Path) -> tuple:
    """-> (doc_id, 'lead'|'adj'|'both', disagreement) reading only page 1."""
    try:
        txt = re.sub(r"\s+", " ", subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", str(pdf), "-"],
            capture_output=True, text=True, timeout=25).stdout or "")
    except Exception:
        return (None, None, None)
    lead, adj = ANY.match(txt.lstrip()), ADJ.search(txt)
    if not (lead or adj):
        return (None, None, None)
    pick = lead or adj
    doc = f"{pick.group(1).upper()}{pick.group(2)}"
    if lead and adj:
        other = f"{adj.group(1).upper()}{adj.group(2)}"
        return (doc, "both", None if other == doc else other)
    return (doc, "lead" if lead else "adj", None)


def build_plan(use_pdf: bool) -> tuple:
    con = sqlite3.connect(f"file:{LIB / 'metadata.db'}?mode=ro", uri=True)
    ids = collections.defaultdict(dict)
    for book, typ, val in con.execute("select book, type, val from identifiers"):
        ids[book][typ] = val
    pdfs = {}
    for book, path, name in con.execute(
            "select b.id, b.path, d.name from books b join data d on d.book=b.id "
            "where upper(d.format)='PDF'"):
        pdfs.setdefault(book, LIB / path / f"{name}.pdf")

    plan, stats, conflicts = [], collections.Counter(), []
    for book, title in con.execute("select id, title from books"):
        title = (title or "").strip()
        if LEAD.match(title):
            stats["already prefixed"] += 1
            continue
        vend = {k: v for k, v in ids.get(book, {}).items() if k in VENDORS}
        doc = None
        if vend:
            code = next(iter(vend.values()))
            m = MCHP.search(code)
            doc = code.upper() if DOCNUM.match(code) else (
                f"DS{m.group(1) or m.group(2)}" if m else None)
            if doc:
                stats["from identifier"] += 1
        if not doc and use_pdf and book in pdfs and pdfs[book].exists():
            doc, how, clash = from_pdf(pdfs[book])
            if doc:
                stats[f"from pdf ({how})"] += 1
            if clash:
                conflicts.append((book, title, doc, clash))
        if not doc:
            stats["no document number"] += 1
            continue
        new = _clean(title, doc)
        if new != title:
            plan.append((book, title, new))
    return plan, stats, conflicts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--no-pdf", action="store_true", help="identifiers only, skip PDF reading")
    ap.add_argument("--audit", action="store_true", help="only report where the two page-1 rules disagree")
    args = ap.parse_args()

    plan, stats, conflicts = build_plan(use_pdf=not args.no_pdf)
    for k, v in stats.most_common():
        print(f"  {v:>6}  {k}")
    print(f"\n  {len(plan)} renames planned")
    if conflicts:
        print(f"  ⚠️  {len(conflicts)} books where the leading id and the rev-adjacent id differ:")
        for b, t, a, c in conflicts[:10]:
            print(f"      #{b} leading={a} adjacent={c}  {t[:50]}")
    if args.audit:
        return 0
    for b, o, n in plan[:10]:
        print(f"    #{b}\n      -  {o[:88]}\n      +  {n[:88]}")
    if not args.apply:
        print("\n  dry run — nothing written. Re-run with --apply")
        return 0

    roll = pathlib.Path.home() / ".local/share/download-doc" / f"retitle-{time.strftime('%Y%m%d-%H%M%S')}.tsv"
    roll.parent.mkdir(parents=True, exist_ok=True)
    roll.write_text("".join(f"{b}\t{o}\t{n}\n" for b, o, n in plan))
    print(f"  rollback: {roll}")

    lib, ok, fail = doclib.Library(), 0, 0
    for n, (b, o, new) in enumerate(plan, 1):
        try:
            lib._run("set_metadata", "--field", f"title:{new}", str(b))
            ok += 1
        except RuntimeError as e:
            fail += 1
            print(f"  FAILED #{b}: {str(e)[:90]}", file=sys.stderr)
        if n % 100 == 0:
            print(f"  {n}/{len(plan)} ok={ok} fail={fail}", flush=True)
    print(f"  done: {ok} renamed, {fail} failed")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
