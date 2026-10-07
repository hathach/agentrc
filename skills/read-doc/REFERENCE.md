# Read Doc: reference

Material a lookup rarely needs. SKILL.md says when to open each section.

## Family names and IP cores

Vendors file the base document under a family name: the part number finds it
(`USB2514` → `USB251xB`, `STM32F407` → `STM32F4xx`) and the row says
`(family match)`; confirm the document's part list names your part. An `x`
after a letter stays literal (`PIC32MX`), so for `STM32H7Rx` or `LPC55Sxx`
search the prefix before the `x`; so does a result holding only guides and
notes with no base document.

Vendors name the base document differently — a reference manual, a family data
sheet, a product specification or an IP core's databook — so the kind that
carries the registers varies by vendor, not by the question you asked.

A peripheral is often a licensed IP core whose own databook is the register
authority the MCU manual abbreviates. Those documents carry no part number, so a
search for the MCU alone never returns them: search the core name too. Get the
core from the MCU manual's own USB chapter, or in a tinyusb checkout from the
supported-device table in `README.rst`, whose driver column names it per part,
rather than assuming: neighbouring parts from one vendor do not always share
one. Read the integration chapter as well as the core document: wiring, clocks
and errata are the MCU's, and only the MCU's manual is authoritative for them.

## File formats and paths

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

## Building the index

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

## Lookup history

Each `search.py`, `locate.py find` and `locate.py page` lookup is appended to
the per-machine history and named on stderr by its id (`READ_DOC_HISTORY=0`
skips it). These commands inspect earlier lookups and give
a command that repeats one:

```bash
python3 <skill dir>/scripts/history.py list [--session PREFIX] [--term TEXT] [--since YYYY-MM-DD]
python3 <skill dir>/scripts/history.py show <id>   # the record, and a command that repeats it
```
