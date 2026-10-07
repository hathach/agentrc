---
name: read-doc
description: Use when you need authoritative hardware/protocol facts from a primary source rather than memory (datasheet, reference manual/TRM, errata, pinout, register/bitfield layout, memory map, eval-board schematic PDF, USB spec) before stating, changing or reviewing anything that depends on hardware or protocol behaviour, or to read/open/look up a manual, book or PDF/EPUB in the user's Calibre library. Board wiring in EAGLE/KiCad is `read-pcb`'s.
---

# Read Doc

For hardware or protocol facts read the doc in the Calibre library
(`~/Documents/calibre-library`, or `CALIBRE_LIBRARY`), not memory or the web.
The scripts sit under this skill's directory and query the library's
`metadata.db` and page index; never search the library tree, whose names lack the tags part
numbers live in.

Hardware-facing agents carry this trigger verbatim in their own prompts:

> Whenever a conclusion, hypothesis, review finding, experiment or code change
> depends on hardware or protocol behaviour (registers and bitfields, access
> order and side effects, interrupts, DMA or cache, clocks, timing, the USB IP's
> state machine and FIFO rules, errata, the USB specification), load the
> `read-doc` skill and check the base document and the errata of every affected
> part and variant before treating it as verified.

## Exit codes

Every script: 0 a result, 1 looked and found nothing, 2 bad usage, 3 the
library, a PDF or the index was unavailable. Only 1 is evidence; on 2 fix the
invocation and retry; on 3 report what the message names as unavailable,
never broaden the keywords.

An absent or unmounted library is exit 3 naming its `metadata.db`: a
non-hardware question then falls back to normal sources silently, while a
hardware or protocol claim stays unverified, reported unavailable, never
settled from memory, the web or a filesystem search. Code and observed
behaviour are not the documented semantics.

## Find the book

Keywords: the skill arguments, else the part number, peripheral or spec name.
`search.py` ANDs them over all metadata and prints book ids, a count per
document kind, and the base documentation first:

```bash
python3 <skill dir>/scripts/search.py errata RT1064            # AND (default)
python3 <skill dir>/scripts/search.py RT1060 RT1064 --any
python3 <skill dir>/scripts/search.py --kind reference-manual stm32h7  # none of that kind: names the kinds found
```

Several matches → narrow with `--kind` or a keyword; genuinely ambiguous → ask.
An erratum overrides the base document, so check a register claim against
both; the count line says whether errata exist. Nothing → retry with fewer
keywords (the part number alone; "datasheet" and "manual" are rarely in the
metadata). Still nothing → report the document missing, never answer from memory.

## Find the page

Never read a manual hunting for one register:

```bash
python3 <skill dir>/scripts/locate.py find --book 2125 --term SIE_CTRL
```

It prints physical PDF page numbers with context lines, a definition ranked
above a mention. The ranking is a heuristic: when the first excerpts are
cross-references, continue with `--offset N` before concluding the term is
undefined. `--context`, `--limit` and `--max-chars` bound the output. Exit 1
means no page's *extracted text* holds it; a schematic, scan or figure has
none, so open the PDF pages of a graphical document first.

## Read

Read the pages `find` named, usually one to three. Tables, bitfields, access
types, register layouts, figures and footnotes do not survive extraction: for
a claim about one, errata included, go from `find` straight to the PDF page.
For prose (a description, an erratum's text, a sequence) use `page` (A or A-B
as `find` numbers them), not `pdftotext` or `search.py --path`; its
`--max-chars` default is 10000, about two dense pages:

```bash
python3 <skill dir>/scripts/locate.py page --book 2125 --pages 403-404
```

Exit 1 is a page out of range or without text. Text locates evidence; the PDF
page is the authority (`page`'s last line names the path and pages to read).

Cite book id, title, page and the `lookup <id> logged` ids from stderr.

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
- **unavailable**: exit 3 or a `MISSING` file; the attempted command, its
  diagnostic and the lookup id when one was logged.
- **pending**: the lookup did not run or did not finish, an exit 2 not yet
  corrected and retried included. Never report it as undocumented.

## More in REFERENCE.md

- A `(family match)`, an `x` in a part name, or a peripheral that is a licensed
  IP core: "Family names and IP cores".
- A non-PDF format, a `MISSING` file, or a PDF with no text layer: "File formats and paths".
- Pre-extracting the index, nightly builds, Calibre's Check Library: "Building the index".
- Listing or replaying earlier lookups: "Lookup history".
