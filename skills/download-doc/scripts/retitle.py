#!/usr/bin/env python3
"""Put each document's vendor number at the front of its Calibre title.

    UM10503 LPC43xx/LPC43Sxx User manual        (was: LPC43xx_LPC43Sxx User manual)
    ES0182 STM32F405 407 415 417 device errata  (was: ... device errata (ES0182))

Dry-run by default; --apply writes, and always after saving a rollback TSV of
id/old/new so every rename is individually reversible. `--rollback <tsv>` plans
restoring the old titles from one, through the same checked rename (--apply writes).

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
import contextlib
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import doclib   # noqa: E402
import sync     # noqa: E402
from titles import id_first   # noqa: E402

LIB = doclib.LIBRARY
VENDORS = set(sync.VENDORS)
LEAD = re.compile(r"^(UM|AN|DS|ES|RM|PM|TN|UG|DRM)\d{3,8}\b", re.I)
DOCNUM = re.compile(r"^(?:UM|AN|DS|ES|RM|PM|TN|UG|DRM)[_-]?\d{3,6}$", re.I)
# Microchip trails its id, not always with the DS: USB2517-…-Data-Sheet-00001598C
MCHP = re.compile(r"(?:DS(\d{5,8})|(?<![0-9A-Za-z])(\d{8}))[A-Z]?$", re.I)
ANY = re.compile(r"\b(UM|AN|DS|ES|RM|PM|TN|UG|DRM)\s?(\d{3,8})\b", re.I)
ADJ = re.compile(r"\b(UM|AN|DS|ES|RM|PM|TN|UG|DRM)\s?(\d{3,8})\b\s*[-–—]?\s*(?:Rev|Revision|V\d)", re.I)


def from_pdf(pdf: pathlib.Path) -> tuple:
    """-> (doc_id, 'lead'|'adj'|'both', disagreement) reading only page 1."""
    try:
        txt = re.sub(r"\s+", " ", subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", "1", str(pdf), "-"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=25).stdout or "")
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


def reprefixed(title: str) -> str:
    """An already-prefixed title with the copies of its leading id that strip_id
    now drops, so a later strip_id fix reaches books a past run prefixed. A
    qualified lead ("UM11750-V3 ...") is its own id and stays as it is."""
    lead = LEAD.match(title)
    if re.match(r"-\S", title[lead.end():]):
        return title
    return id_first(title, lead.group(0))


def read_library() -> tuple:
    """-> ({book: {type: val}}, {book: [file paths]}, [(id, title, folder)]) from metadata.db."""
    # Read up front and closed: Windows cannot delete or replace a database still open.
    with contextlib.closing(sqlite3.connect(f"file:{LIB / 'metadata.db'}?mode=ro", uri=True)) as con:
        identifiers = con.execute("select book, type, val from identifiers").fetchall()
        data = con.execute(
            "select b.id, b.path, d.name, d.format from books b join data d on d.book=b.id").fetchall()
        books = con.execute("select id, title, path from books").fetchall()
    ids = collections.defaultdict(dict)
    for book, typ, val in identifiers:
        ids[book][typ] = val
    files = collections.defaultdict(list)
    for book, path, name, fmt in data:
        files[book].append(LIB / path / f"{name}.{fmt.lower()}")
    return ids, files, books


def on_disk(folder: str, files: list) -> bool:
    """Whether a book's folder and every file the database lists for it exist. calibre
    moves a renamed book's files out of the folder the database names; when they are not
    there, the rename can empty the folder that does hold them."""
    return (LIB / folder).is_dir() and all(f.is_file() for f in files)


def build_plan(use_pdf: bool) -> tuple:
    ids, files, books = read_library()
    plan, stats, conflicts, absent = [], collections.Counter(), [], []

    def propose(book, folder, title, new):
        if new == title:
            return
        if not on_disk(folder, files[book]):
            absent.append(book)
            return
        plan.append((book, title, new))

    for book, title, folder in books:
        title = (title or "").strip()
        if LEAD.match(title):
            stats["already prefixed"] += 1
            propose(book, folder, title, reprefixed(title))
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
        pdf = next((f for f in files[book] if f.suffix == ".pdf"), None)
        if not doc and use_pdf and pdf and pdf.exists():
            doc, how, clash = from_pdf(pdf)
            if doc:
                stats[f"from pdf ({how})"] += 1
            if clash:
                conflicts.append((book, title, doc, clash))
        if not doc:
            stats["no document number"] += 1
            continue
        propose(book, folder, title, id_first(title, doc))
    return plan, stats, conflicts, absent


def rollback_plan(tsv: pathlib.Path) -> tuple:
    """-> (plan of (book, title now, title restored), [(book, why withheld)], books already
    restored) from a TSV --apply saved. A book is restored only while it still carries the
    title that run gave it and its files are on disk. A row that is not id<TAB>old<TAB>new,
    or a second row for one book, is a ValueError: the file is not such a TSV."""
    rows = {}
    text = tsv.read_text(encoding="utf-8").removesuffix("\n")
    # Split on newlines alone: splitlines() also breaks a title at \x1c or U+2028.
    for n, line in enumerate(text.split("\n") if text else [], 1):
        f = line.split("\t")
        if len(f) != 3 or not f[0].isdigit() or not f[1].strip() or not f[2].strip() or int(f[0]) in rows:
            raise ValueError(f"{tsv}:{n}: not a row of a retitle --apply TSV: {line[:80]!r}")
        rows[int(f[0])] = (f[1], f[2])
    _, files, books = read_library()
    now = {book: ((title or "").strip(), folder) for book, title, folder in books}
    plan, withheld, restored = [], [], 0
    for book, (old, new) in rows.items():
        title, folder = now.get(book, (None, None))
        if title == old:
            restored += 1
        elif title is None:
            withheld.append((book, "no such book"))
        elif title != new:
            withheld.append((book, f"retitled since: {title[:60]!r}"))
        elif not on_disk(folder, files[book]):
            withheld.append((book, "the database names files that are not on disk"))
        else:
            plan.append((book, new, old))
    return plan, withheld, restored


def rename(plan: list) -> int:
    """Set each (book, old, new) title through calibredb, after saving the TSV that
    --rollback reverses. Exit 0, 1 a rename failed, 2 blocked."""
    if not plan:
        return 0
    lib = doclib.Library(LIB)  # the library the plan was read from
    blockers = lib.blockers()
    for b in blockers:
        print(f"BLOCKED: {b}", file=sys.stderr)
    if blockers:
        return 2

    folder = pathlib.Path.home() / ".local/share/download-doc"
    folder.mkdir(parents=True, exist_ok=True)
    # Never over a TSV saved the same second: a rollback right after its --apply would.
    fd, roll = tempfile.mkstemp(prefix=f"retitle-{time.strftime('%Y%m%d-%H%M%S')}-", suffix=".tsv",
                                dir=folder)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(f"{b}\t{o}\t{n}\n" for b, o, n in plan))
    print(f"  rollback: {roll}")

    ok, fail = 0, 0
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


def rollback(tsv: pathlib.Path, apply: bool) -> int:
    try:
        plan, withheld, restored = rollback_plan(tsv)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(f"  {len(plan)} titles to restore, {restored} already restored")
    for b, why in withheld:
        print(f"  ⚠️  #{b} withheld: {why}")
    for b, now, old in plan[:10]:
        print(f"    #{b}\n      -  {now[:88]}\n      +  {old[:88]}")
    if not apply:
        print("\n  dry run — nothing written. Re-run with --apply")
        return 0
    code = rename(plan)
    return code or (1 if withheld else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--no-pdf", action="store_true", help="identifiers only, skip PDF reading")
    ap.add_argument("--audit", action="store_true", help="only report where the two page-1 rules disagree")
    ap.add_argument("--rollback", type=pathlib.Path, metavar="TSV",
                    help="restore the titles a TSV saved by --apply records")
    args = ap.parse_args()
    if args.rollback:
        return rollback(args.rollback, args.apply)

    plan, stats, conflicts, absent = build_plan(use_pdf=not args.no_pdf)
    for k, v in stats.most_common():
        print(f"  {v:>6}  {k}")
    print(f"\n  {len(plan)} renames planned")
    if absent:
        print(f"  ⚠️  {len(absent)} renames withheld: the database names files that are not on disk "
              f"(repair the library first): #{', #'.join(map(str, absent[:20]))}")
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
    return rename(plan)


if __name__ == "__main__":
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")  # titles, and the warning signs
    sys.exit(main())
