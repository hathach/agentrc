---
name: read-doc
description: Use when you need authoritative hardware/protocol facts from a primary source rather than model memory — an MCU/peripheral datasheet, reference manual (RM/TRM), errata, pinout, register/bitfield layout, memory map, a vendor evaluation-board schematic PDF, or the USB spec — before answering register/electrical/timing/protocol questions from training knowledge or the web; or when the user asks to read/open/look up a manual, datasheet, book, or PDF/EPUB from their Calibre library. Board wiring in EAGLE/KiCad design sources is `read-pcb`'s. Requires a local Calibre library at ~/Documents/calibre-library; no-ops if absent.
---

# Read Doc

## Overview

Datasheets, manuals and books live in the Calibre library at
`$HOME/Documents/calibre-library/`. For hardware/protocol facts — registers,
bitfields, memory maps, pinouts, electrical/timing specs, errata, USB spec —
read the doc instead of answering from training knowledge or the web.

Search the library's `metadata.db`, never the filesystem. The database indexes
title, authors, tags, series, publisher, description and the stored filename;
most part numbers live in the tags, which the filesystem does not carry.

## Gate first

The library is per-user and usually on a network mount, so test the database
file, not the directory — an unmounted or half-synced mountpoint is still a
directory:

```bash
[ -f "${CALIBRE_LIBRARY:-$HOME/Documents/calibre-library}/metadata.db" ] && echo present || echo absent
```

Absent → the skill does not apply; fall back to normal sources silently (don't
mention the library unless the user named it). For a claim record (below),
report the failed gate as unavailable instead of silently falling back.

## When to use

- About to state a register/bitfield/reset-value/memory-map/pinout/timing spec
  for a specific MCU or peripheral.
- User says "read the RP2040 datasheet", "open the CH569 manual", "what does the
  STM32H7 RM say about…".

Not for general concepts, repo/code questions, or when no such doc is likely.

## Find the book

Keywords supplied as skill arguments, else derived from the question (part
number, peripheral, spec name). `scripts/search.py` sits under this skill; run
it from wherever the skill was loaded. It ANDs the keywords across every
metadata field and prints the book id, a count per document kind, and the base
documentation first — the manual, specification or datasheet a register question
is answered from, ahead of the errata and application notes:

```bash
python3 <skill dir>/scripts/search.py errata RT1064            # AND (default)
python3 <skill dir>/scripts/search.py RT1060 RT1064 --any
python3 <skill dir>/scripts/search.py --kind reference-manual stm32h7
```

Both scripts use one exit-code contract: 0 a result, 1 searched and found
nothing, 2 bad usage, 3 the library, a PDF or the index was unavailable. Only 1
is evidence; 2 and 3 mean the search never ran, so fix the invocation, or
report the component the message names as unavailable, instead of broadening
the keywords. An unsearchable PDF is not an unreachable library.

One match → use it. Several → the kind counts say whether the right kind is
even present; narrow with `--kind` or another keyword rather than reading the
wrong document. The base document is ranked first but an erratum overrides it,
so a register claim is checked against both: the count line says whether one
exists. Vendors name the base document differently — a reference manual, a
family data sheet, a product specification or an IP core's databook — so the
kind that carries the registers varies by vendor, not the question you asked.
Vendors file that document under a family name: the part number finds it
(`USB2514` → `USB251xB`, `STM32F407` → `STM32F4xx`) and the row says
`(family match)`; confirm the document's part list names your part. An `x`
after a letter stays literal (`PIC32MX`), so for `STM32H7Rx` or `LPC55Sxx`
search the prefix before the `x`; so does a result holding only guides and
notes with no base document.
Genuinely ambiguous → ask the user which to read. Nothing (exit 1) → retry with
fewer keywords; the part number alone often works where
`<part> datasheet` does not, because words like "datasheet" and "manual" are
rarely in the metadata. `--any` only changes anything with two or more
keywords. Still nothing → say the document is missing rather than answering
from memory.

A peripheral is often a licensed IP core whose own databook is the register
authority the MCU manual abbreviates. Those documents carry no part number, so a
search for the MCU alone never returns them: search the core name too. Get the
core from the MCU manual's own USB chapter, or in a tinyusb checkout from the
supported-device table in `README.rst`, whose driver column names it per part,
rather than assuming: neighbouring parts from one vendor do not always share
one. Read the integration chapter as well as the core document: wiring, clocks
and errata are the MCU's, and only the MCU's manual is authoritative for them.

Set `CALIBRE_LIBRARY` to search a library elsewhere.

## Find the page

Never read a 1000-page manual looking for one register. `scripts/locate.py`
searches the document's text and answers with the physical PDF pages to open:

```bash
python3 <skill dir>/scripts/locate.py find --book 2125 --term SIE_CTRL
```

It prefers a register's own definition to a passing mention, prints a few
context lines per hit, and pages with `--offset`. `--context`, `--limit` and
`--max-chars` bound the output. The ranking is a heuristic over the page text,
not a guarantee: when the first excerpts are cross-references, keep going with
`--offset` rather than concluding the document does not define the term.

Exit 1 means the term is in no page's *extracted text*. That is evidence about
the text layer, not about the document: a drawn schematic, a scanned page or a
figure carries none. On a document you expect to be graphical, open the PDF
pages directly before reporting the register as undocumented.

The first lookup of a book extracts it (one to three seconds for a large
manual); later ones are immediate. The text lives in `<library>/.read-doc/`,
travels with the library's own sync, and is derived data you may delete.

`build` pre-extracts instead of waiting for the first lookup:

```bash
python3 <skill dir>/scripts/locate.py build --all          # the whole library
python3 <skill dir>/scripts/locate.py build --book 2125    # one book
```

It reuses current indexes and extracts PDFs whose indexes are missing or
stale, including retrying PDFs with no text layer, and prunes index entries
for books that no longer have a PDF in the library metadata. Run it after
importing documents, or nightly:

```cron
17 3 * * * python3 ~/.claude/skills/read-doc/scripts/locate.py build --all >/dev/null
```

Photography books are skipped by tag (`--skip-tag` replaces that list); a
`find` on one still indexes it, since skipping is a build cost, not a reading
policy. Exit 3 names what was unavailable — including a PDF with no text layer
at all — and the build did not silently do less than it claims.

Calibre's Check Library lists `.read-doc` as an invalid author directory. Add
it to that dialog's ignore-names field once per library; nothing else in
Calibre is affected.

## Read

Read the pages `locate.py find` named, not the document. One to three pages
answers a register or erratum question; expand only when the text continues
past them. The excerpt locates the evidence, the PDF page carries the
authority: bitfield tables, diagrams and footnotes do not survive text
extraction, so a claim about a bit's access type is checked on the page.

`search.py --path` prints one `FORMAT path` line per stored file:

- **PDF** — read the pages by number with the runtime's PDF-reading capability.
  When extraction failed and `find` cannot help, fall back to pages 1-20 for
  the table of contents, then sections on demand.
- **Any other format** (EPUB, MOBI, CHM, ZIP…) — not indexed; if the runtime
  has no decoder, say the document is not in a readable format and do not paste
  mojibake.
- **`MISSING`** — the metadata is real but the file is not on disk (library
  mid-sync, or the file was deleted). Report the file as unavailable, not the
  document as nonexistent.

Cite the book id, the page and the document title for anything you assert, so
the next reader can reopen it.

## Lookup history

When stderr reports `lookup <id> logged`, cite that id beside the book and
page. The commands below inspect previous lookups and give a command that
repeats one.

```bash
python3 <skill dir>/scripts/history.py list [--session PREFIX] [--term TEXT] [--since YYYY-MM-DD]
python3 <skill dir>/scripts/history.py show <id>   # the record, and a command that repeats it
```

## Reporting a claim

When a lookup decides whether a claim or a proposed change holds, such as a
review finding, record one outcome per claim:

- **verified** or **refuted**: book id, title, revision, section, page and
  lookup ids, and the reasoning that ties them to the claim. For a proposed
  change, explain how the source change preserves the cited semantics for
  every affected variant, or where it breaks them.
- **undocumented in the checked sources**: after searching and reading the
  relevant pages of the base document, the errata and any applicable IP-core
  documentation of every affected part; name the books, terms and lookup ids,
  and the premise nothing supports.
- **unavailable**: exit 3, a `MISSING` file or a failed gate; the attempted
  command, its diagnostic and the lookup id when one was logged.
- **pending**: the lookup did not run or did not finish, an exit 2 not yet
  corrected and retried included. Never report it as undocumented.

## Common mistakes

- Searching with `find`/`grep` over the library tree. It sees only truncated
  filenames, missing the tags, series and descriptions where part numbers and
  errata IDs actually live. Query the database.
- Skipping the gate on a machine with no library.
- Answering a register/spec question from memory when the datasheet is on disk.
- Reading pages 1-20 of a manual to hunt for a register that `locate.py find`
  would have named the page for.
- Quoting a bitfield table from a text excerpt. Open the page.
- Requiring all keywords to match — broaden, or use `--any`, on zero hits.
- Treating a `MISSING` file, or an exit 2 or 3, as proof the document is absent.
