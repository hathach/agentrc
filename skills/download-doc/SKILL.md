---
name: download-doc
description: List, fetch, download, import or refresh vendor hardware docs (datasheets, reference manuals, errata, user manuals, app notes) in the Calibre library — a chip's document, a vendor's docs in bulk, or a documentation-library update, check or audit — even when Calibre is not named.
---

# download-doc

Fetch vendor documentation into the Calibre library, and keep it current.
`CALIBRE_LIBRARY` overrides the library location for every script here, the same
variable `read-doc` honours. The library is the first place to look before searching
the web for a manual, so its value depends on being complete and *not stale*.

| File | Role |
|---|---|
| `scripts/sync.py` | CLI: sync a vendor (enumerate → plan → `--apply` imports/replaces), or `add`, `refresh` or `remove` one document. Dry run by default. |
| `scripts/vendor_<name>.py` | One adapter per vendor; `sync.py --help` lists them, each docstring gives its scope. Renesas, Microchip and HPMicro only read a hand-harvested cache; their docstrings say how to build it |
| `scripts/doclib.py` | Shared core: transport, revisions, Calibre I/O, planning |
| `scripts/retitle.py` | Move each document number to the front of its Calibre title. Dry run by default; `--rollback <tsv>` restores the titles an `--apply` saved. |
| `references/st.md`, `references/nxp.md` | Per-vendor endpoints, quirks, the NXP gated-download flow |
| `references/maintaining.md` | Why the conventions, the failure modes, adding a vendor |

## Sync a vendor

Run the plan first and *read it*:

```bash
python3 <skill dir>/scripts/sync.py st --family STM32H7 --types datasheet,errata
```

It prints, per document, the resolved doc ID and the `old -> new` revision pair, and
changes nothing. Check those pairs: the one serious failure mode is resolving the
**wrong document ID**, which doesn't error — it produces a confident, plausible,
entirely wrong plan. Then apply and reindex so `read-doc` finds the new pages:

```bash
python3 <skill dir>/scripts/sync.py st --family STM32H7 --types datasheet,errata --apply
python3 <skill dir>/scripts/sync.py nxp --types errata --device "i.MX RT" --apply
python3 ~/.claude/skills/read-doc/scripts/locate.py build --all
```

A bare run fetches **technical documents only** (datasheet, errata, reference/user/
programming manual, user guide, application note; `--all-types` widens, `--types`
replaces) for **parts with a USB controller** where the adapter can decide that
(`--all-devices` widens, `--family`/`--chips`/`--device`/`--parts` replace). Both are
announced on every run.

Plan lines: `NEW`, `OUTDATED old -> new`, `GATED` (needs a person, below), `LEGACY`
(filed by hand before identifiers existed; reported, never touched), `UNCHECKABLE`
(revision cannot be compared). The coverage block must add up per type — catalogue
equals the sum of the buckets — and names unparseable revisions; `--strict` makes them
a non-zero exit.

## Add one document

For a vendor with no adapter, or a document an adapter does not list, `add` is the
import path — not curl, a hand-built `Doc`, or `doclib.Library` calls:

```bash
python3 <skill dir>/scripts/sync.py add --vendor gigadevice \
    --author "GigaDevice Semiconductor" --id GD32F303xB-datasheet --id-not-printed \
    --type datasheet --title "GD32F303xB Arm Cortex-M4 32-bit MCU" --revision 1.1 \
    --family GD32F303 --url "https://download.gigadevice.com/Datasheet/GD32F303xB%20Datasheet_Rev1.1.pdf"
```

- `--vendor`: the lowercase identifier scheme. An adapter's name fixes the author; a
  new vendor needs `--author`, the company name, spelled the same every time.
- `--id`: the vendor's stable document number, never including the revision. When the
  document prints none, use `<part>-<kind>` (or the filename stem) with
  `--id-not-printed`.
- `--type`: one of the shared kinds (`--help` lists them); `--title` is the description
  without ID or revision; `--family` adds tags.
- `--url URL`, or `--pdf FILE --source URL` for a file downloaded in a browser — the
  route for vendors that block scripted clients (Nordic answers 403).

The dry run still downloads and checks the file, then prints the identifier, author,
title, tags and comments it would file. It refuses (exit 1) an identifier already in
the library — `add` never replaces — a second
spelling of the vendor's scheme or author, a title already filed by hand, a download
that is not a PDF, and an ID pages 1-2 do not confirm (unless `--id-not-printed`). Exit 2 is a
usage error, a blocker, or a library it could not read. `--apply` imports; reindex after.

A newer revision of a book an adapter does not refresh goes through `sync.py refresh`,
with `add`'s arguments and a required `--revision`; the given metadata replaces the
book's (`sync.py refresh --help`).

An old copy `refresh` cannot reach, such as a book filed by hand with no identifier,
goes through `sync.py remove <book id>`: dry run first, then `--apply`.

## Conventions

Dedup depends on matching these exactly; `maintaining.md` gives the reasons.

- `authors` — the vendor's company name: `STMicroelectronics`, `NXP Semiconductors`
- `title` — `<DOCID> <description> Rev <n>`, e.g. `ES0392 STM32H7 device errata Rev 15`;
  an unprinted ID trails in parentheses (`retitle.py` fixes id-last ones)
- `identifiers` — `<vendor>:<DOCID>`, set at import time; the only record of "do we
  have this?" — no import log beside it
- `tags` — kind (`datasheet`, `errata`, `reference-manual`, …) + vendor + family
- `comments` — description, Document ID, Revision, Source URL

## Safety rules

- `--apply` refuses while something holds the library. **The Calibre GUI's write lock**:
  ask the user to close it, never kill it — or configure a content-server user in the
  gitignored `secret.yml` beside this file (`calibre:` block; `Library._server_creds`
  documents it), its `server_url` ending in `#<id>`, calibre's id for the GUI's library
  (`calibre-library` for `~/Documents/calibre-library`). The server is used only for the
  library the GUI has open; with the GUI open, no other library (a scratch copy included)
  can be written. **A FreeFileSync `calibre-library.ffs_batch` mirror** to omv: let it
  finish. On Windows the mirror check cannot see command lines; pause the mirror yourself
  before `--apply`.
- Replacing or removing a book moves the old one to Calibre's Recycle Bin (kept 14
  days by default; restore it there); remove through `refresh` or `sync.py remove`,
  never `calibredb remove` by hand.
- Gated documents (login, per-document EULA, NXP moderated request, `*-NDA`) are
  reported per document with the reason. **Never accept a licence or submit a request
  form without the user's explicit say-so**; `references/nxp.md` has the flow.
- Never dedupe NXP mask-set errata on title similarity; the code is the identity.

## When to open references/maintaining.md

Before writing or changing an adapter or `doclib.py`; and when a run misbehaves — 404,
406 or empty answers that look like blocking, a revision that never updates or reads
`UNCHECKABLE`, an identity mismatch, a download that is not a PDF, "database is
locked" after imports, every Microchip book turning OUTDATED, or a question about the
LEGACY bucket or USB scope.
