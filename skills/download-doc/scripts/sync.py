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
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import doclib                       # noqa: E402
import vendor_espressif, vendor_microchip, vendor_nxp, vendor_renesas   # noqa: E402
import vendor_allwinner, vendor_geehy, vendor_hpmicro, vendor_silabs   # noqa: E402
import vendor_ti                                                       # noqa: E402
import vendor_wch                                                      # noqa: E402
import vendor_rpi, vendor_st                                          # noqa: E402

VENDORS = {"st": vendor_st, "nxp": vendor_nxp, "espressif": vendor_espressif,
           "rpi": vendor_rpi, "renesas": vendor_renesas,
           "microchip": vendor_microchip, "ti": vendor_ti, "silabs": vendor_silabs,
           "allwinner": vendor_allwinner,
           "wch": vendor_wch, "hpmicro": vendor_hpmicro,
           "geehy": vendor_geehy}


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
    # VENDORS is the single source of truth: argparse takes its choices from it and
    # dispatch reads it here. A second copy of this table is what let "wch" be
    # accepted by one and rejected by the other.
    mod = VENDORS.get(name)
    if mod is None:
        raise SystemExit(f"no adapter wired for vendor {name!r} — add it to enumerate_vendor")
    return mod.enumerate_docs(**common)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vendor", choices=sorted(VENDORS))
    ap.add_argument("--types", help="comma list: datasheet,errata,reference-manual,...")
    ap.add_argument("--family", help="ST only: STM32H7,STM32U5,... (default: all grids)")
    ap.add_argument("--device", default="Arm MCU", help="NXP only: taxonomy branch")
    ap.add_argument("--chips", help="Espressif only: esp32-s3,esp32-p4 (default: USB-OTG parts)")
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

    lib = doclib.Library()
    blockers = lib.blockers()
    if blockers and args.apply:
        for b in blockers:
            print(f"BLOCKED: {b}", file=sys.stderr)
        return 2
    for b in blockers:
        print(f"note: {b}", file=sys.stderr)

    # Defaults, applied only where the user has not said otherwise. Naming --types or a
    # device scope explicitly turns the corresponding default off entirely.
    mod = VENDORS[args.vendor]
    if not args.types and not args.all_types:
        args.types = ",".join(sorted(doclib.TECHNICAL_TYPES))
        print("scope: technical documents only (datasheet, errata, manuals, app notes) "
              "— --all-types to widen", file=sys.stderr)
    named_device = any([args.family, args.chips, args.vendor == "nxp" and
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
    backup = Path.home() / ".local" / "share" / "download-doc" / "superseded"
    added = replaced = failed = 0
    skipped: list = []

    def fetch(doc):
        """Fetch, then confirm the file is the document we asked for."""
        got, why = doclib.fetch_document(doc.url, tmp / f"{doc.doc_id}.pdf", doc.doc_id)
        if got is None and args.vendor == "nxp":
            got, why = doclib.fetch_document(vendor_nxp.direct_url(doc),
                                             tmp / f"{doc.doc_id}.pdf", doc.doc_id)
        if got is None:
            return None, why
        verdict, found = doclib.verify_identity(got, doc)
        if verdict == "mismatch":
            # The vendor served a different document. Filing it under this identifier
            # would be worse than missing it: it looks right forever after.
            return None, f"served {found}, not {doc.doc_id} — not imported"
        if verdict == "absent":
            print(f"  note: {doc.doc_id} not found on page 1 — importing unverified",
                  file=sys.stderr)
        # What an adapter can only learn from the file itself (Microchip prints the
        # letter revision in the page footer) is kept as description.
        if hasattr(mod, "describe_pdf"):
            extra = mod.describe_pdf(got)
            if extra:
                doc.desc = f"{doc.desc}\n{extra}".strip()
        return got, None

    for doc in fetchable:
        pdf, why = fetch(doc)
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
        pdf, why = fetch(doc)
        if pdf is None:
            skipped.append((doc.doc_id, f"{why}; local copy left alone"))
            failed += 1
            continue
        lib.remove(have["id"], backup)      # backs the old PDF up first
        if lib.add(doc, pdf):
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
        print(f"superseded PDFs backed up to {backup}")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
