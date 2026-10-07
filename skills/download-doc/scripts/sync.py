#!/usr/bin/env python3
"""download-doc CLI: enumerate a vendor, diff against Calibre, import what's new,
replace what's outdated.

Dry-run is the default and prints the resolved document ID plus the old -> new
revision for every book it would touch. That listing is not decoration: the one
serious failure mode here is resolving the *wrong* doc ID, and a wrong ID produces
a confident, plausible, completely wrong plan. Reading the pairs is how you catch
it. Pass --apply once the plan looks right.

  sync.py st  --family STM32H7 --types datasheet,errata
  sync.py st  --family STM32H7 --types datasheet,errata --apply
  sync.py nxp --types errata --device "i.MX RT"
  sync.py ti  --parts ina3221,tca9548a

`sync.py add` imports one document by hand under the same conventions, for a vendor
with no adapter or a document an adapter does not list (`sync.py add --help`);
`sync.py refresh` replaces such a book with a newer revision (`sync.py refresh --help`).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import doclib                       # noqa: E402
import vendor_espressif, vendor_microchip, vendor_nxp, vendor_renesas   # noqa: E402
import vendor_allwinner, vendor_geehy, vendor_hpmicro, vendor_silabs   # noqa: E402
import vendor_ti                                                       # noqa: E402
import vendor_wch                                                      # noqa: E402
import vendor_arm, vendor_rpi, vendor_st                              # noqa: E402

VENDORS = {"st": vendor_st, "nxp": vendor_nxp, "espressif": vendor_espressif,
           "rpi": vendor_rpi, "renesas": vendor_renesas,
           "microchip": vendor_microchip, "ti": vendor_ti, "silabs": vendor_silabs,
           "allwinner": vendor_allwinner,
           "wch": vendor_wch, "hpmicro": vendor_hpmicro,
           "geehy": vendor_geehy, "arm": vendor_arm}


def enumerate_vendor(name: str, args) -> list:
    """Dispatch to the vendor adapter.

    Explicit table, and an unknown name raises. This used to be a chain of `if`s
    ending in a bare `return vendor_nxp.enumerate_docs(...)`, so a vendor missing
    from the chain silently enumerated NXP instead — `sync.py allwinner` planned an
    import of 353 NXP documents and looked entirely normal doing it. A fallback that
    substitutes a different vendor is worse than a crash.
    """
    common = dict(families=args.family.split(",") if args.family else None,
                  types=args.types.split(",") if args.types else None)
    if name == "espressif":
        return vendor_espressif.enumerate_docs(
            chips=args.chips.split(",") if args.chips else None,
            types=common["types"], refresh=args.refresh)
    if name == "nxp":
        return vendor_nxp.enumerate_docs(types=common["types"], device=args.device,
                                         limit=args.limit)
    if name == "st":
        return vendor_st.enumerate_docs(refresh=args.refresh, **common)
    if name == "ti":
        return vendor_ti.enumerate_docs(
            parts=args.parts.split(",") if args.parts else None, **common)
    # VENDORS is the single source of truth: argparse takes its choices from it and
    # dispatch reads it here. A second copy of this table is what let "wch" be
    # accepted by one and rejected by the other.
    mod = VENDORS.get(name)
    if mod is None:
        raise SystemExit(f"no adapter wired for vendor {name!r} — add it to enumerate_vendor")
    return mod.enumerate_docs(**common)


def verified(pdf: Path, doc, mod) -> tuple:
    """Confirm the file is the document asked for -> (pdf, identity verdict) or (None, why)."""
    verdict, found = doclib.verify_identity(pdf, doc)
    if verdict == "mismatch":
        # The vendor served a different document. Filing it under this identifier
        # would be worse than missing it: it looks right forever after.
        return None, f"served {found}, not {doc.doc_id} — not imported"
    if verdict == "absent":
        print(f"  note: {doc.doc_id} not found on page 1 — identity unverified",
              file=sys.stderr)
    # What an adapter can only learn from the file itself (Microchip prints the
    # letter revision in the page footer) is kept as description.
    if hasattr(mod, "describe_pdf"):
        extra = mod.describe_pdf(pdf)
        if extra:
            doc.desc = f"{doc.desc}\n{extra}".strip()
    return pdf, verdict


def fetch(doc, mod, tmp: Path) -> tuple:
    """Download, then verify -> (pdf, identity verdict) or (None, why)."""
    got, why = doclib.fetch_document(doc.url, tmp / f"{doc.doc_id}.pdf", doc.doc_id)
    if got is None and mod is vendor_nxp:
        got, why = doclib.fetch_document(vendor_nxp.direct_url(doc),
                                         tmp / f"{doc.doc_id}.pdf", doc.doc_id)
    if got is None:
        return None, why
    return verified(got, doc, mod)


# The shared tag vocabulary: the kinds the adapters normalize to.
KINDS = sorted(doclib.TECHNICAL_TYPES | {"technical-note", "data-brief"})


def library_conflict(doc, idx: dict, legacy: dict) -> str | None:
    """Why this document must not be added to the library, or None."""
    have = {k.lower(): v for k, v in idx.items()}.get(doc.ident.lower())
    if have:
        refresh = (f"`sync.py {doc.vendor}` refreshes it when the vendor publishes a newer "
                   f"revision, or `sync.py refresh` with the newer one" if doc.vendor in VENDORS
                   else "`sync.py refresh` with its newer revision replaces it")
        return (f"{doc.ident} is already book #{have['id']} {have['title']!r} "
                f"(Rev {have['rev'] or 'unknown'}); add never replaces a book — {refresh}")
    why = spelling_conflict(doc, idx)
    if why:
        return why
    p = doclib.plan([doc], idx, legacy)
    if p["legacy"]:
        _, book = p["legacy"][0]
        return (f"probably already filed by hand as #{book['id']} {book['title']!r} — "
                f"compare the two before adding a second copy")
    return None


def spelling_conflict(doc, idx: dict) -> str | None:
    """A second spelling of either name splits one vendor's books in two."""
    for ident, book in idx.items():
        scheme = ident.split(":", 1)[0]
        if scheme == doc.vendor and book["author"] != doc.author:
            return (f"{scheme}: books are filed under author {book['author']!r} "
                    f"(#{book['id']}), not {doc.author!r}")
        if book["author"] == doc.author and scheme != doc.vendor:
            return (f"author {doc.author!r} already files its books as {scheme}: "
                    f"(#{book['id']}), not {doc.vendor}:")
    return None


def head(path: Path) -> bytes:
    """The bytes classify_payload reads, without loading a whole manual."""
    with path.open("rb") as f:
        return f.read(2048)


def blocked(lib, apply: bool) -> bool:
    """Whether the library's blockers stop this run: each is BLOCKED under --apply, a note otherwise."""
    blockers = lib.blockers()
    for b in blockers:
        print(f"{'BLOCKED' if apply else 'note'}: {b}", file=sys.stderr)
    return bool(blockers) and apply


def doc_parser(prog: str, description: str) -> argparse.ArgumentParser:
    """The arguments add and refresh share: one document and where its PDF comes from."""
    ap = argparse.ArgumentParser(prog=prog, description=description,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vendor", required=True,
                    help="identifier scheme, lowercase: an adapter's name "
                         f"({', '.join(sorted(VENDORS))}) or a new one (nordic)")
    ap.add_argument("--author",
                    help="the vendor's company name as Calibre files it (Nordic "
                         "Semiconductor); required for a vendor without an adapter")
    ap.add_argument("--id", required=True, dest="doc_id",
                    help="the vendor's stable document number, without the revision")
    ap.add_argument("--id-not-printed", action="store_true",
                    help="the ID is a filename stem or a name you derived (part-kind), not "
                         "printed in the document: no identity check, and the title carries "
                         "it in parentheses, as adapters file such IDs")
    ap.add_argument("--type", required=True, choices=KINDS)
    ap.add_argument("--title", required=True, help="description, without the ID or revision")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="download the PDF from this URL")
    src.add_argument("--pdf", type=Path, help="import this local PDF (a browser download)")
    ap.add_argument("--source", help="with --pdf, required: the URL it came from")
    ap.add_argument("--family", help="comma list of family tags: nRF52,...")
    ap.add_argument("--desc", default="", help="description line for comments")
    ap.add_argument("--apply", action="store_true", help="actually write to the library")
    return ap


def parse_doc(ap: argparse.ArgumentParser, argv: list) -> tuple:
    """-> (args, Doc, adapter module or None), or a usage error."""
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]*", a.vendor):
        ap.error(f"--vendor {a.vendor!r}: a lowercase identifier scheme like st, nxp, nordic")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", a.doc_id):
        ap.error(f"--id {a.doc_id!r}: letters, digits, '.', '_' and '-' only")
    if a.pdf is not None:
        if not a.source:
            ap.error("--pdf needs --source: the URL the file came from")
        if not a.pdf.is_file():
            ap.error(f"--pdf {a.pdf}: no such file")
    elif a.source:
        ap.error("--source goes with --pdf; --url is already the source")
    mod = VENDORS.get(a.vendor)
    if mod is not None:
        if a.author and a.author != mod.AUTHOR:
            ap.error(f"{a.vendor}: books are filed under author {mod.AUTHOR!r}; drop --author")
        author = mod.AUTHOR
    elif not a.author:
        ap.error(f"--author is required: {a.vendor!r} has no adapter to name the vendor")
    else:
        author = a.author
    doc = doclib.Doc(vendor=a.vendor, doc_id=a.doc_id, doc_type=a.type, version=a.revision,
                     title=a.title, url=a.url or a.source, author=author,
                     family=a.family.split(",") if a.family else [], desc=a.desc,
                     verify_id=not a.id_not_printed)
    return a, doc, mod


def acquire(a, doc, mod, tmp: Path) -> tuple:
    """Download or copy the PDF into tmp and confirm it -> (pdf, identity verdict) or
    (None, why refused). A --pdf is copied first: it may sit in the very book folder a
    refresh removes."""
    if a.url:
        pdf, why = fetch(doc, mod, tmp)
    else:
        copy = Path(shutil.copy2(a.pdf, tmp / a.pdf.name))
        if doclib.classify_payload(head(copy)) != "pdf":
            return None, f"{a.pdf} is not a PDF"
        pdf, why = verified(copy, doc, mod)
    if pdf is None:
        return None, why
    # A hand-typed ID is filed only once the document confirms it, or the caller
    # declared there is nothing printed to confirm it against.
    if why != ("skipped" if a.id_not_printed else "ok"):
        unconfirmed = {"absent": f"{doc.doc_id} is not printed on pages 1-2",
                       "inconclusive": f"pages 1-2 do not confirm {doc.doc_id} (no "
                                       f"readable text, or no '<ID> Rev' marking)"}
        return None, (f"{unconfirmed.get(why, f'identity check returned {why!r}')} — "
                      f"use the number the document prints, or pass --id-not-printed")
    return pdf, why


def show(heading: str, doc, pdf: Path, why: str) -> None:
    print(f"\n{heading}\n"
          f"  author      {doc.author}\n"
          f"  title       {doc.calibre_title()}\n"
          f"  tags        {', '.join(doc.tags())}\n"
          f"  file        {pdf.stat().st_size} bytes, identity {why}\n"
          f"  comments    " + doc.comments().strip().replace("\n", "\n              "))


REINDEX = "reindex so read-doc finds it: python3 ~/.claude/skills/read-doc/scripts/locate.py build --all"
SUPERSEDED = Path.home() / ".local" / "share" / "download-doc" / "superseded"


def add_main(argv: list) -> int:
    """Import one document under the conventions an adapter's import follows.

    Dry run by default; it still downloads and checks the file, so every refusal
    shows up before --apply. Exit 0 planned or imported, 1 refused, 2 a usage
    error, a blocker, or a library that could not be read.
    """
    ap = doc_parser("sync.py add", add_main.__doc__)
    ap.add_argument("--revision", help="as the document prints it; without one the book "
                                       "can never be checked for a newer revision")
    a, doc, mod = parse_doc(ap, argv)

    lib = doclib.Library()
    if blocked(lib, a.apply):
        return 2
    try:
        idx, legacy = lib.index(), lib.legacy_index(doc.author)
    except RuntimeError as e:
        if a.apply:
            print(f"BLOCKED: cannot read the library: {e}", file=sys.stderr)
            return 2
        idx = None
        print(f"note: cannot read the library, duplicate checks not run: {e}", file=sys.stderr)
    if idx is not None:
        why = library_conflict(doc, idx, legacy)
        if why:
            print(f"REFUSED: {why}", file=sys.stderr)
            return 1

    tmp = Path(tempfile.mkdtemp(prefix="download-doc-"))
    try:
        pdf, why = acquire(a, doc, mod, tmp)
        if pdf is None:
            print(f"REFUSED: {why}", file=sys.stderr)
            return 1
        show(f"ADD {doc.ident}", doc, pdf, why)
        if not a.revision:
            print("  note: no --revision — recorded as unknown, so this book can never be "
                  "checked for a newer revision", file=sys.stderr)
        elif doclib.parse_rev(a.revision)[0] == "unknown":
            print(f"  note: revision {a.revision!r} is unparseable — this book can never be "
                  f"checked for a newer revision", file=sys.stderr)

        if not a.apply:
            if idx is None:
                print("\ndry run — library not checked for duplicates; nothing changed",
                      file=sys.stderr)
                return 2
            print("\ndry run — nothing changed. Check the identifier, author and title "
                  "above, then re-run with --apply", file=sys.stderr)
            return 0
        book_id = lib.add(doc, pdf)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if book_id is None:
        why = ("a book with this title and author already exists"
               if lib.last_add_status == "duplicate" else "calibredb add reported no book id")
        print(f"REFUSED: {why}", file=sys.stderr)
        return 1
    print(f"\nIMPORTED #{book_id} {doc.ident}\n{REINDEX}")
    return 0


def refresh_target(lib, doc) -> tuple:
    """-> (the one book carrying doc's identifier, None) or (None, why refused). Counted
    from the book list itself: index() keeps one book per identifier."""
    books = lib.books()
    found = [(b, ident) for b in books if (ident := stored_ident(b, doc.ident))]
    if not found:
        return None, f"{doc.ident} is not in the library — use `sync.py add`"
    if len(found) > 1:
        return None, (f"{doc.ident} is on {len(found)} books "
                      f"({', '.join('#' + str(b['id']) for b, _ in found)}); keep one by hand first")
    why = spelling_conflict(doc, lib.index(books))
    if why:
        return None, why
    book, ident = found[0]
    return {**lib.entry(book), "ident": ident}, None


def stored_ident(book: dict, ident: str) -> str | None:
    """How `book` spells `ident`, matched case-insensitively, or None."""
    return next((f"{s}:{c}" for s, c in (book.get("identifiers") or {}).items()
                 if f"{s}:{c}".lower() == ident.lower()), None)


def replace(lib, old_id: int, doc, pdf: Path) -> tuple:
    """Back book old_id up, remove it and add doc in its place -> (new id, backup paths,
    None), or (None, backup paths, why it stopped, naming the stage). A failed backup
    removes nothing."""
    try:
        copies = lib.remove(old_id, SUPERSEDED)
    except doclib.BackupError as e:
        return None, [], f"backup failed, book #{old_id} left in place: {e}"
    except doclib.RemoveError as e:
        return None, [], f"removal failed, nothing added: {e}"
    try:
        new_id = lib.add(doc, pdf)
        why = None if new_id else lib.last_add_status
    except RuntimeError as e:
        new_id, why = None, str(e) or type(e).__name__
    if new_id:
        return new_id, copies, None
    return None, copies, (f"book #{old_id} was removed but the new copy was not added ({why}); "
                          f"its files are in {', '.join(map(str, copies))}")


def refresh_main(argv: list) -> int:
    """Replace the book carrying a document's identifier with a newer revision of it,
    under the conventions add files it with: for a vendor with no adapter, or a
    document its adapter does not list. The supplied metadata replaces the book's,
    so give --family and --desc again if it should keep them.

    The --revision is compared before anything is downloaded: newer (or a book whose
    revision cannot be read) goes ahead, the same one is nothing to do, and one that
    cannot be compared is refused. --apply backs the old PDF up to
    ~/.local/share/download-doc/superseded, then removes the book and adds the new
    one; a failed backup removes nothing. Exit 0 planned, refreshed or already
    current, 1 refused or failed, 2 a usage error, a blocker, or a library that
    could not be read.
    """
    ap = doc_parser("sync.py refresh", refresh_main.__doc__)
    ap.add_argument("--revision", required=True, help="the newer revision, as the document prints it")
    a, doc, mod = parse_doc(ap, argv)

    lib = doclib.Library()
    if blocked(lib, a.apply):
        return 2
    try:
        have, why = refresh_target(lib, doc)
    except RuntimeError as e:
        print(f"BLOCKED: cannot read the library: {e}", file=sys.stderr)
        return 2
    if why:
        print(f"REFUSED: {why}", file=sys.stderr)
        return 1
    verdict, detail = doclib.compare_rev(doc.version, have["rev"])
    if verdict == "current":
        print(f"{doc.ident} is book #{have['id']} at Rev {have['rev']}: nothing to refresh")
        return 0
    if verdict != "newer":
        print(f"REFUSED: cannot tell Rev {doc.version} is newer than book #{have['id']}'s: "
              f"{detail}", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="download-doc-"))
    try:
        pdf, why = acquire(a, doc, mod, tmp)
        if pdf is None:
            print(f"REFUSED: {why}", file=sys.stderr)
            return 1
        show(f"REFRESH #{have['id']} {doc.ident} ({detail})\n  was         {have['title']}",
             doc, pdf, why)
        if have["ident"] != doc.ident:
            print(f"  note: the library spells it {have['ident']}; the new book takes "
                  f"{doc.ident}, as --id gives it", file=sys.stderr)
        if not a.apply:
            print(f"\ndry run — nothing changed. --apply backs #{have['id']}'s files up to "
                  f"{SUPERSEDED}, removes it and adds the above", file=sys.stderr)
            return 0
        again, why = refresh_target(lib, doc)
        if not why and (again["id"], again["rev"]) != (have["id"], have["rev"]):
            why = f"now Rev {again['rev']} as #{again['id']}"
        if why:
            print(f"REFUSED: book #{have['id']} changed since the plan ({why}); run it again",
                  file=sys.stderr)
            return 1
        book_id, copies, failure = replace(lib, have["id"], doc, pdf)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failure:
        print(f"FAILED: {failure}", file=sys.stderr)
        return 1
    print(f"\nREFRESHED #{have['id']} -> #{book_id} {doc.ident}\n"
          f"backed up: {', '.join(map(str, copies))}\n{REINDEX}")
    return 0


def main() -> int:
    if sys.argv[1:2] == ["add"]:
        return add_main(sys.argv[2:])
    if sys.argv[1:2] == ["refresh"]:
        return refresh_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vendor", choices=sorted(VENDORS))
    ap.add_argument("--types", help="comma list: datasheet,errata,reference-manual,...")
    ap.add_argument("--family", help="family tags: STM32H7,STM32U5,... for ST, Armv8-M,ADIv5 for Arm (default: all)")
    ap.add_argument("--device", default="Arm MCU", help="NXP only: taxonomy branch")
    ap.add_argument("--chips", help="Espressif only: esp32-s3,esp32-p4 (default: USB-OTG parts)")
    ap.add_argument("--parts", help="TI only: ina3221,tca9548a (default: parts in the TinyUSB BSP)")
    ap.add_argument("--limit", type=int, help="stop after N documents (smoke tests)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch cached indexes")
    ap.add_argument("--apply", action="store_true", help="actually import/replace")
    ap.add_argument("--all-types", action="store_true",
                    help="include non-technical docs (product briefs, technical notes)")
    ap.add_argument("--usb-only", action="store_true",
                    help="import only families CONFIRMED to have USB; drop unresolved ones "
                         "too (default keeps unresolved rather than dropping on thin evidence)")
    ap.add_argument("--all-devices", action="store_true",
                    help="ignore the default USB-controller device scope")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any revision cannot be parsed (for automation)")
    args = ap.parse_args()
    if args.parts and args.vendor != "ti":
        ap.error("--parts is TI only")

    lib = doclib.Library()
    if blocked(lib, args.apply):
        return 2

    # Defaults, applied only where the user has not said otherwise. Naming --types or a
    # device scope explicitly turns the corresponding default off entirely.
    mod = VENDORS[args.vendor]
    if not args.types and not args.all_types:
        args.types = ",".join(sorted(doclib.TECHNICAL_TYPES))
        print("scope: technical documents only (datasheet, errata, manuals, app notes) "
              "— --all-types to widen", file=sys.stderr)
    named_device = any([args.family, args.chips, args.parts, args.vendor == "nxp" and
                        args.device != "Arm MCU"])
    usb_scope = getattr(mod, "USB_SCOPE", None)
    note = getattr(mod, "USB_SCOPE_NOTE", "")
    apply_usb = not named_device and not args.all_devices
    if apply_usb:
        if usb_scope == "all":
            print(f"scope: USB-capable parts only — {note}", file=sys.stderr)
        elif usb_scope:
            scope = ",".join(sorted(usb_scope))
            if args.vendor == "espressif":
                args.chips = scope
            else:
                args.family = scope
            print(f"scope: USB-capable parts only ({note}) — --all-devices to widen",
                  file=sys.stderr)
        else:
            print(f"scope: USB-capable parts only, resolved per family from the "
                  f"documents themselves ({note}) — --all-devices to widen",
                  file=sys.stderr)

    print(f"enumerating {args.vendor} ...", file=sys.stderr)
    docs = enumerate_vendor(args.vendor, args)
    if not docs:
        # "0 found" has two very different causes and guessing wastes the user's time:
        # either the index never arrived, or it arrived and the filters matched nothing.
        # Re-enumerate unfiltered to tell them apart before blaming the network.
        probe_args = argparse.Namespace(**vars(args))
        probe_args.types = None
        unfiltered = enumerate_vendor(args.vendor, probe_args)
        if unfiltered:
            kinds = sorted({d.doc_type for d in unfiltered})
            print(f"index is fine ({len(unfiltered)} documents) but no document matched "
                  f"--types {args.types}. Available here: {', '.join(kinds)}",
                  file=sys.stderr)
        else:
            print("no index available — the vendor is likely throttling; wait a few "
                  "minutes and re-run (progress is cached)", file=sys.stderr)
        return 1

    # Resolve USB capability per family, peeking into one document per unresolved
    # family. Declared scopes and cached verdicts cost nothing; only genuinely unknown
    # families trigger a download, and the verdict is remembered with its evidence.
    non_usb = []
    if apply_usb and usb_scope is None:
        usb = doclib.UsbIndex()
        usb.hint_from_index(docs)
        # Cross-cutting tags aren't a part and can't be peeked; those documents apply
        # to every series, including the USB ones, so they are always kept.
        PSEUDO = {"stm32-wide", "wide"}
        fams = sorted({f for d in docs for f in (d.family or []) if f not in PSEUDO})
        unresolved = [f for f in fams if usb.get(args.vendor, f) is None]
        if unresolved:
            print(f"resolving USB capability for {len(unresolved)} famil(ies) by reading "
                  f"one document each: {', '.join(unresolved)}", file=sys.stderr)
        peek_tmp = Path.home() / ".cache" / "download-doc" / "peek"
        for fam in unresolved:
            cands = doclib.peek_order([d for d in docs if fam in (d.family or [])])
            usb.resolve(args.vendor, fam, cands, peek_tmp)
            v = usb.get(args.vendor, fam)
            label = ("USB" if v and v["usb"] is True else
                     "no USB" if v and v["usb"] is False else "unresolved")
            print(f"  {fam:<12} {label:<10} "
                  f"({v['source'] if v else 'unresolved'}: "
                  f"{v['evidence'][:48] if v else '-'})", file=sys.stderr)
        keep = []
        for d in docs:
            fams_d = [f for f in (d.family or []) if f not in PSEUDO]
            v = [usb.get(args.vendor, f) for f in fams_d]
            # Default keeps unresolved families — dropping a whole family on partial
            # evidence is the expensive mistake. --usb-only inverts that: nothing ships
            # unless a document was actually read and showed a USB peripheral.
            if args.usb_only:
                excluded = bool(fams_d) and not any(x and x["usb"] is True for x in v)
            else:
                excluded = bool(fams_d) and bool(v) and all(
                    x is not None and x["usb"] is False for x in v)
            if excluded:
                non_usb.append(d)
            else:
                keep.append(d)          # cross-series docs (no family) are kept
        docs = keep
        if non_usb:
            why = ("not confirmed USB (--usb-only)" if args.usb_only
                   else "families with no USB controller")
            print(f"left out: {len(non_usb)} document(s) — {why}", file=sys.stderr)

    idx = lib.index()
    p = doclib.plan(docs, idx, lib.legacy_index(docs[0].author))
    needs_human = [d for d in p["new"]
                   if args.vendor == "nxp" and vendor_nxp.is_gated(d)]
    fetchable = [d for d in p["new"] if d not in needs_human]

    print(f"\n{args.vendor}: {len(docs)} enumerated | {len(p['new'])} new | "
          f"{len(p['outdated'])} outdated | {len(p['current'])} already current\n")

    for d in p["outdated"]:
        doc, have = d
        print(f"  OUTDATED  {doc.doc_id:<22} {str(have['rev']):>12} -> {doc.version}")
    for doc in fetchable[:40]:
        print(f"  NEW       {doc.doc_id:<22} Rev {doc.version or '?':<10} {doc.title[:44]}")
    if len(fetchable) > 40:
        print(f"  ... and {len(fetchable) - 40} more new documents")
    for doc in needs_human:
        print(f"  GATED     {doc.doc_id:<22} needs a signed-in browser — see references/nxp.md")
    for doc, book in p["legacy"]:
        print(f"  LEGACY    {doc.doc_id:<22} already filed by hand as #{book['id']} "
              f"{book['title'][:34]!r} — left alone")

    print("\ncoverage — catalogue must equal the sum of what happened to it:")
    for line in doclib.reconcile(docs, {"new": fetchable, "current": p["current"],
                                        "legacy": p["legacy"], "outdated": p["outdated"],
                                        "incomparable": p["incomparable"],
                                        "gated": needs_human}):
        print(line)
    # A document whose revision cannot be parsed passes every other check while being
    # permanently un-updatable. Unreported, that is indistinguishable from "current".
    for doc, _have, verdict, detail in p["incomparable"]:
        print(f"  UNCHECKABLE {doc.doc_id:<20} {verdict}: {detail}")
    unparseable = doclib.unparseable_revisions(docs)
    for kind, items in sorted(unparseable.items()):
        sample = ", ".join(f"{c}={v!r}" for c, v in items[:3])
        more = f", +{len(items) - 3} more" if len(items) > 3 else ""
        print(f"  {'':<20} ({len(items)} revision(s) unparseable — cannot be checked "
              f"for updates: {sample}{more})")

    if args.strict and (unparseable or p["incomparable"]):
        n = sum(len(v) for v in unparseable.values()) + len(p["incomparable"])
        print(f"\n--strict: {n} document(s) have an unparseable revision", file=sys.stderr)
        return 3

    if not args.apply:
        print("\ndry run — nothing changed. Check the ID and revision pairs above, "
              "then re-run with --apply", file=sys.stderr)
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="download-doc-"))
    added = replaced = failed = unsafe = 0
    skipped: list = []

    for doc in fetchable:
        pdf, why = fetch(doc, mod, tmp)
        if pdf is None:
            skipped.append((doc.doc_id, why))
            failed += 1
            continue
        if lib.add(doc, pdf):
            added += 1
        elif getattr(lib, "last_add_status", "") == "duplicate":
            skipped.append((doc.doc_id, "already in the library under this title"))
        else:
            skipped.append((doc.doc_id, "calibredb add reported no book id"))
            failed += 1

    for doc, have in p["outdated"]:
        pdf, why = fetch(doc, mod, tmp)
        if pdf is None:
            skipped.append((doc.doc_id, f"{why}; local copy left alone"))
            failed += 1
            continue
        _, _, failure = replace(lib, have["id"], doc, pdf)
        if failure:
            skipped.append((doc.doc_id, failure))
            unsafe += 1
        else:
            replaced += 1

    print("\n" + "=" * 68)
    print(f"IMPORTED  {added} new" + (f" + {replaced} replaced" if replaced else ""))
    skipped_ids = {s_id for s_id, _ in skipped}
    for doc in fetchable:
        if doc.doc_id not in skipped_ids:
            print(f"    + {doc.doc_id[:44]:<44} {doc.doc_type}")
    print("LEFT OUT")
    if non_usb:
        print(f"    {len(non_usb):>3} " + ("not confirmed USB (--usb-only)" if args.usb_only
                                             else "no USB controller in that family"))
        for d in non_usb[:8]:
            print(f"        - {d.doc_id[:40]:<40} {','.join(d.family)}")
        if len(non_usb) > 8:
            print(f"        ... and {len(non_usb) - 8} more")
    if p["legacy"]:
        print(f"    {len(p['legacy']):>3} already in the library, filed by hand")
    if p["current"]:
        print(f"    {len(p['current']):>3} already current")
    if p["incomparable"]:
        print(f"    {len(p['incomparable']):>3} uncheckable revision (see UNCHECKABLE above)")
    if needs_human:
        print(f"    {len(needs_human):>3} gated, need a signed-in browser")
    if skipped:
        print(f"    {len(skipped):>3} failed:")
        for doc_id, why in skipped[:8]:
            print(f"        - {doc_id[:40]:<40} {why[:46]}")
    print("=" * 68)
    if replaced:
        print(f"superseded PDFs backed up to {SUPERSEDED}")
    for doc_id, why in skipped:
        print(f"  SKIPPED {doc_id:<22} {why}")
    print("\ncoverage — catalogue must equal the sum of what happened to it:")
    for line in doclib.reconcile(docs, {"imported": fetchable, "current": p["current"],
                                        "legacy": p["legacy"], "outdated": p["outdated"],
                                        "incomparable": p["incomparable"],
                                        "gated": needs_human}):
        print(line)
    if failed:
        print(f"  ({failed} of the above failed to download and are counted as imported "
              f"attempts — see SKIPPED lines)")

    if needs_human:
        print(f"\n{len(needs_human)} documents need a signed-in browser; "
              f"see references/nxp.md for the flow (and get the user's OK before "
              f"accepting any licence on their behalf).")
    # A failed download leaves the library as it was; a failed replacement may not.
    return 1 if unsafe else 0


if __name__ == "__main__":
    sys.exit(main())
