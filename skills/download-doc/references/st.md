# STMicroelectronics

The easy vendor: everything is public, enumeration needs no login, and PDFs download
directly. The adapter is thin because ST does not fight you — the only real friction
is rate limiting.

## Enumeration — the resource grid

Each product page's document list is backed by a JSON grid:

```
https://www.st.com/bin/st/selectors/cxst/en.cxst-rs-grid.html/<ID>.all.json
```

The `.all` is essential — without it you get a partial grid.

Fetch through `doclib.http_get`, which validates that the body parses as JSON and
retries with a second client. curl has been seen to fail here at the connection level
(`HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR`, zero bytes) in one process
while wget succeeded seconds later — and, in another process on the same host at the
same time, curl with browser headers worked fine. Treat client choice as luck, not
strategy; the validator is what makes it reliable.

### Grid IDs

`CL1734` is the STM32 class-wide grid. It carries cross-series material —
application notes, reference manuals, user manuals, programming manuals — but
**no datasheets and no errata sheets**. Those exist only in the per-series grids, so a
part's datasheet requires fetching its series.

| Series | ID | Series | ID | Series | ID |
|---|---|---|---|---|---|
| C0 | SS2200 | G0 | SS1960 | U0 | SS2133 |
| C5 | SS2361 | G4 | SS2024 | U3 | SS2327 |
| F0 | SS1574 | H5 | SS2186 | U5 | SS2134 |
| F1 | SS1031 | H7 | SS1951 | WB | SS1961 |
| F2 | SS1575 | L0 | SS1817 | WB0 | SS2337 |
| F3 | SS1576 | L4+ | SS2002 | WBA | SS2261 |
| F4 | SS1577 | L4 | SS1580 | WL | SS2026 |
| F7 | SS1858 | L5 | SS2025 | | |

The series a document appears under gives you its family tag for free. A document
listed in several series accumulates several family tags — merge across grids keyed
on doc ID, keeping the highest version seen.

### Row shape

```json
{ "title": "AN5142",                    // the doc ID, despite the field name
  "version": "3.0",
  "resourceType": "Application Note",
  "localizedDescriptions": { "en": "How to implement Class-D audio amplifier..." },
  "localizedLinks":        { "en": "/resource/en/application_note/an5142-....pdf" } }
```

`resourceType` values worth mapping: `Datasheet`, `Errata sheet`, `Reference Manual`,
`User Manual`, `Programming Manual`, `Application Note`, `Technical Note`,
`Data Brief`. The grids also carry non-document types (Product Presentation, Wiki,
Security Bulletin, Brochures, Certification Document) — ignore them rather than
importing marketing material into the library.

## Titles

Descriptions are usable as-is for everything except datasheets, where ST writes a
generic marketing line ("32-bit Arm Cortex-M7 480MHz MCU…") that's identical across
dozens of parts. The actual part number appears only in the PDF filename:

```
/resource/en/datasheet/stm32h743zi.pdf  ->  STM32H743ZI
```

So title datasheets `<PART> — <description> (<DSxxxxx>) Rev n`. Without this, a
library search for a part number finds nothing.

## Downloading

Use `localizedLinks.en`, prefixed with `https://www.st.com`.

When you know only a document ID, ST's paths prefix-resolve — but the **trailing
hyphen is required**, because real paths are `<id>-<slug>.pdf`:

```
https://www.st.com/resource/en/errata_sheet/es0392-.pdf    works
https://www.st.com/resource/en/errata_sheet/es0392.pdf     404
```

The type segment is the resourceType lowercased with underscores
(`errata_sheet`, `reference_manual`, `application_note`, `datasheet`).

## Rate limiting

A handful of grid fetches in quick succession and st.com stops answering for several
minutes — connections are dropped, not refused with an error page, so it looks like a
network fault. Observed after ~6 fetches; recovery took minutes.

The adapter caches each grid under `~/.cache/download-doc/st/` and paces requests a
few seconds apart. Treat an empty result as backoff rather than a wrong URL: wait and
re-run, and cached progress is reused. Enumerating all 25 grids is a slow operation by
design — scope with `--family` when you only need one series.
