---
name: download-doc
description: Enumerate, download, and import vendor hardware documentation — datasheets, reference manuals, errata, programming/user manuals, application notes — into the Calibre library at ~/Documents/calibre-library, and refresh local copies when the vendor has published a newer revision. Use this whenever the user wants a datasheet or reference manual for a chip, asks to update/check/audit their documentation library, mentions ST/STM32 or NXP/i.MX/LPC/Kinetis/MCX documents, or wants to bulk-fetch a vendor's docs — even if they never say "Calibre".
---

# download-doc

Fetch vendor documentation into the Calibre library, and keep it current.
`CALIBRE_LIBRARY` overrides the library location for every script here, the same
variable `read-doc` honours; imports and replacements then target that library.

The library at `~/Documents/calibre-library` is the house archive of hardware docs
(~3,000 books). It is also the first place to look before searching the web for a
manual — so its value depends on being both complete and *not stale*.

## The pipeline

```
vendor adapter          shared core
enumerate()  ──▶  diff vs Calibre identifiers  ──▶  download  ──▶  import / replace  ──▶  report
                  (st:DS12930, nxp:MCXA344...)                     (backup first)
```

Everything vendor-specific lives in an adapter; everything else is shared. Adding a
vendor should mean writing one adapter, not another pipeline.

| File | Role |
|---|---|
| `scripts/sync.py` | CLI. Enumerate → plan → (with `--apply`) import. Dry-run by default. |
| `scripts/doclib.py` | Transport, revision comparison, Calibre I/O, planning. Vendor-neutral. |
| `scripts/vendor_st.py` | STMicroelectronics adapter |
| `scripts/vendor_nxp.py` | NXP adapter |
| `scripts/vendor_espressif.py` | Espressif adapter |
| `scripts/vendor_rpi.py` | Raspberry Pi adapter (RP2040/RP2350, Pico boards) — probes a name list, no index exists |
| `scripts/retitle.py` | Move each document number to the front of its Calibre title. Dry-run by default. |
| `references/st.md`, `references/nxp.md` | Per-vendor endpoints, quirks, and the gated-download flow |

Books added by hand before this skill existed carry no identifier, so an
identifier-only diff would import a second copy of each. Adapters declare `aliases`
(the title shapes their vendor's files were historically filed under) and those land
in a **legacy** bucket: reported, never touched. They can't be revision-compared — the
old title records no revision — so the only automatic action would be replacing a
book the user filed themselves, on a guess.

## What a bare run fetches

Two defaults apply unless the request names otherwise. Both are announced on every
run, because a filtered run and an unfiltered one must not look alike:

- **Technical documents only** — datasheet, errata, reference/user/programming manual,
  user guide, application note. Product briefs, presentations, packaging and
  certification paperwork are the bulk of a vendor catalogue and noise in a reference
  library. `--all-types` widens it; naming `--types` replaces it outright.
- **Parts with a USB controller** — where the vendor's data supports deciding that.
  `--all-devices` widens it; naming `--family`/`--chips`/`--device` replaces it.

The USB scope is per-adapter and honest about what it cannot know:

| Vendor | Scope | Why |
|---|---|---|
| Espressif | S2, S3, S31, P4 | datasheet-verified USB OTG; USB-Serial/JTAG-only parts (C3/C6/H2) excluded on purpose |
| Raspberry Pi | everything | every RP2040/RP2350 part has a USB controller |
| ST | **no filter** | not derivable — see below |
| NXP | **no filter** | taxonomy classifies by product line, not peripheral |

⚠️ Don't be tempted to mine ST's datasheet blurbs for "USB". It looks like it works —
it finds USB in 10 of 24 series — and it is wrong: STM32G4, L5, U5 and WB all have USB
and all score zero, because those descriptions are truncated marketing text. That
heuristic silently drops whole series. When capability can't be derived, the adapter
says so in the run output rather than applying a filter that quietly under-reports.

A chip list written from memory goes stale the same way: the ESP32-S31 was missing from
the USB scope until the user asked about it, because the model's snapshot predated the
part. Re-derive from datasheets (`pdftotext -f 1 -l 6 … | grep -i otg`), not recall.

## Before you touch the library

Run the plan first and *read it*:

```bash
python3 <skill dir>/scripts/sync.py st --family STM32H7 --types datasheet,errata
```

This prints, per document, the resolved doc ID and the `old -> new` revision pair,
and changes nothing. Check those pairs before going further. The one serious failure
mode in this whole workflow is resolving the **wrong document ID** — and a wrong ID
doesn't error, it produces a confident, plausible, entirely wrong plan. (A regex bug
that turned `RM0433` into `RM433` once reported "6 outdated" when the truth was 31.)
The listing is how you catch it.

Then apply:

```bash
python3 <skill dir>/scripts/sync.py st --family STM32H7 --types datasheet,errata --apply
python3 <skill dir>/scripts/sync.py nxp --types errata --device "i.MX RT" --apply
```

`--apply` refuses to run while something else holds the library:

- **The Calibre GUI holds an exclusive write lock.** Ask the user to close it; don't
  kill it. With a content-server user configured in the gitignored `secret.yml`
  beside this file (`calibre:` block with `server_url`, `username`, `password`;
  `Library._server_creds` documents the shape) imports go through the running
  GUI's server instead, and the lock is not a blocker.
- **A FreeFileSync `calibre-library.ffs_batch` mirror.** Importing mid-sync races a
  17 GB mirror to omv and produces spurious "another calibre program is running"
  errors. Let it finish. (That batch is the scheduled FreeFileSync mirror of the
  library to omv.)

## Conventions that must not drift

The ~1,800 already-imported books follow these, and dedup depends on matching them
exactly:

- `authors` — the vendor: `STMicroelectronics`, `NXP Semiconductors`
- `title` — `<description> (<DOCID>) Rev <n>`, e.g. `Errata sheet LPC55S6x (ES_LPC55S6X) Rev 2.9`
- `identifiers` — `st:<DOCID>` / `nxp:<DOCID>`, **set at import time**
- `tags` — kind (`datasheet`, `errata`, `reference-manual`, `user-manual`,
  `programming-manual`, `application-note`) + vendor + family (`STM32H7`, `i.MX RT`, `LPC`)
- `comments` — description, Document ID, Revision, Source URL. The `Revision:` line is
  read back by `Library.index()` when the title carries no `Rev n` — titles only hold
  numeric revisions, so a letter or date revision lives here and nowhere else.

Identifiers are the single source of truth for "do we already have this?". Don't add
an import log alongside them — two sources of truth drift, and one `calibredb list
--for-machine` at startup gives you the same answer in one query, survives log loss,
and sees books added by any other route. Cache it in memory for the run, not on disk.

`calibredb add` has no `--comments`, so import is add-then-`set_metadata`. If the
second call fails you get a book with the right identifier and no comments — which an
identifier-only check will call "done" forever. When auditing, treat *has identifier
but empty comments* as needing repair rather than as complete.

## Things that will bite you

**Validate the payload; never trust the exit status.** Both vendors serve HTML error
and login pages with a 200, so success has to mean "the JSON parsed" or "the file
starts with `%PDF`". `doclib.http_get` takes a validator, tries wget then curl, and
raises only when no client returned something that passes. Keep it that way rather
than picking a favourite client — which one works has been observed to differ between
processes on the same host at the same minute.

Two artifacts masquerade as bot-blocking; both cost hours if you take them at face
value:

- **curl globs `{ }`.** NXP's API takes its query as a brace-wrapped path segment.
  Without `-g`, curl strips the braces and requests a malformed URL, which returns
  406 — or 404 with a 745-byte "Page not available" body that looks exactly like an
  Akamai block. It isn't. `curl -g` returns a clean 200.
- **`%{size_download}` counts wire bytes.** With `--compressed`, a 29,896-byte JSON
  body reports 3,878. That reads as truncation. Compare *file* sizes, not counters.

**Vendors throttle for real, though.** A burst of index fetches and st.com stops
answering — dropped connections, no error page, so it presents as a network fault, and
it can take out every client in the process at once. Indexes are cached under
`~/.cache/download-doc/` and paced apart for this reason. An empty result usually
means "backed off", not "wrong URL": wait and re-run, and cached progress is kept.

**The channel has to exist in the signature, not just in the printing.** Every silent
failure this skill has had was a *correct* refusal that looked like a success, and the
fix each time was widening what the code can say — never making the decision smarter.
The decisions were right all along. So `compare_rev()` returns
`newer | current | incomparable | unparseable` with a detail string, and `rev_newer()`
is a thin boolean wrapper for callers that genuinely don't care. Report from
`compare_rev`. A boolean cannot distinguish "up to date" from "cannot be checked", and
no discipline at the call site recovers information the return type has already
thrown away — a passing test on the boolean can't see the difference either, which is
how this survived a test suite that asserted the correct value.

**So is a revision the vendor re-schemed.** If the local book records `1.1` and the
vendor now reports `30 January 2024` — or `2` becomes `B` after a rewrite — both parse
fine, but they cannot be ordered against each other, and `rev_newer` refuses forever.
That refusal is correct and invisible: it reads as "already current". Those pairs get
their own `UNCHECKABLE` line and an `incomparable` column in the coverage arithmetic,
so a convention change surfaces the first time it appears.

**An unparseable revision is a silent, permanent stall.** A document whose revision
can't be parsed enumerates, matches and imports perfectly — and can then never be
compared, so it reports "0 outdated" forever, which looks exactly like "everything is
current". Espressif's `v1.8` did this. The coverage block therefore names them:

```
  errata      catalogue    4 = 2 current + 2 legacy
              (2 revision(s) unparseable — cannot be checked for updates: AN123='DRAFT', ...)
```

so the next unhandled format announces itself the first time it appears rather than
after someone notices a vendor has never once published an update. `--strict` turns
that into a non-zero exit for automation.

**Revisions are not version numbers.** ST's are clean (`12.0`), but NXP's `RevisionNo`
is free text and roughly a third of errata put a date in it (`10DEC2013`,
`30 January 2024`, even `11 Sep 019`). `doclib.parse_rev` returns a *kind* —
`num` / `date` / `alpha` / `unknown` — and comparison refuses to order across kinds.
Separated dates are detected **before** the numeric branch, because `30.01.2024`
matches `\d+(\.\d+)*` and parses as version `(30, 1, 2024)` — which orders *older*
than `(1, 1, 2024)` and so silently refuses every real update across a month or year
boundary. DD/MM vs MM/DD is resolved only when a field exceeds 12 or both agree;
a genuinely ambiguous `01.02.2024` stays unknown rather than guessing. A date is
neither newer nor older than `2.0`; it's unknowable. Under-replacing shows up in the
report and is recoverable; over-replacing deletes a good local PDF on the strength of
a parse artifact. Keep that asymmetry.

**Check the file is the document you asked for.** Vendors do serve the wrong one —
NXP returned `SAC57D5x_1N87P` for a request for `SAC57D54H_1N87P`. A wrong PDF filed
under the right identifier is worse than a missing book, because it looks correct
forever. `verify_identity` reads page 1 and compares, and it **must** use the
Rev-adjacent pattern: on ST's ES0392 errata sheet the first ID-shaped token on page 1
is `RM0433`, the reference manual it cites, so a bare match would reject 124 valid
errata sheets. A mismatch blocks the import; an unfindable code only warns.

**Reconcile coverage per type, and print it.** `catalogue == new + current + legacy +
outdated + gated`, per document type, every run. This is the one check that catches a
silent drop, which is the failure this skill has actually suffered twice — and unlike
an exception, a silent drop reports as a confident success. It's also what lets you
*prove* a fix cost nothing rather than argue about it: `124 = 91 + 33 + 0`.

**A download isn't always a PDF.** `fetch_document` classifies before importing: NXP
`DRM*` bundles arrive as a zip holding `<CODE>.pdf` plus HW/SW packages (extract the
PDF, discard the rest); some codes resolve to an online HTML doc site with no file at
all; and a few open only through a session-signed `cache.nxp.com` URL that is
unobtainable headlessly. Each needs a different answer, and none of them is a retry.

**The GUI keeps a sqlite lock after content-server imports.** With the Calibre GUI
open, `sync.py --apply` writes through its content server fine — but afterwards the
idle GUI held `metadata.db` locked for well over five minutes (2026-09-01), so anything
that reads the database directly (`retitle.py`, `sqlite3`) fails with "database is
locked". `calibredb` through the server still works; route reads that way or wait.

**Microchip has no revision field; a file is pinned or rolling.** `…-DS00001692D.pdf`
pins Rev D and never changes; `…-DS00001692.pdf` is the rolling latest (it served Rev E
while the D file still existed, so when both are listed the pinned one is dropped). A
rolling file's version is the sitemap `<lastmod>` date, harvested into the cache as
`url<TAB>lastmod` — a date compares against a date, and it moves when the file does.
Consequence: books imported before lastmod was recorded carry `Revision: unknown`, and
the core's "local unreadable → refresh" rule marks every one of them OUTDATED on the
next run. That is one deliberate re-download (or a backfill of `Revision:` with the
date each book was added), not a bug — but it is ~100 documents, so read the plan first.

**Back up before replacing.** `calibredb remove` is immediate with no undo, and a
superseded revision is still the only copy of *that* revision. `Library.remove()`
copies the PDF to `~/.local/share/download-doc/superseded/` first.

**Sweep the download directory case-insensitively and extension-agnostically.**
Browser-mediated downloads don't match the doc code: casing differs
(`MKMxxZxxACxx5RM.pdf` for code `MKMXXZXXACXX5RM`), Chrome appends ` (1)`, and some
docs arrive as a `.zip` with the PDF inside. A strict match "misses" files that
downloaded fine and re-fetches them every retry. `doclib.sweep_download_dir` handles
this and lives in the core because any browser-mediated vendor hits it.

**Never dedupe NXP mask-set errata on title or mask similarity.** `KINETIS_K_0N50M`
and `KINETIS_V_0N50M` look like revisions of each other and are different products
(MK22FN vs MKV31F). Dedupe on the document code only. If two really do look
redundant, confirm with `pdftotext` against the "applies to mask X for these
products" list inside the PDF before removing either.

## Documents that need a human

Roughly half of NXP's Arm-MCU catalogue — and nearly every reference manual — is
gated. Those are reported, not silently dropped:

- **Login-gated** (`webapp/Download?colCode=`) — needs a signed-in browser;
  `references/nxp.md` has the flow.
- **Per-document EULA** (`sps/download/license.jsp`) — a licence agreement.
  **Get the user's explicit say-so before accepting one on their behalf.** Accepting
  a licence is a legal act performed in their name; it is not yours to click.
- **Moderated request** (`mod_download.jsp?appType=moderated`) — not a EULA at all,
  but a form asking for the user's NXP salesperson and email, sent for approval.
  Never submit it automatically.
- **`*-NDA` codes** — unobtainable without an NDA. A few codes in NXP's own index
  are simply dead 404s.

Report what was skipped and *why*, per document. Across a 1,500-document import the
interesting output was not the successes — it was the ~70 that needed a person. A run
that says only "done" hides precisely the part the user has to act on.

## Adding a vendor

Write `scripts/vendor_<name>.py` exposing:

```python
enumerate_docs(...) -> list[doclib.Doc]   # Doc(vendor, doc_id, doc_type, version,
                                          #     title, url, author, family, desc)
```

Normalize `doc_type` to the shared tag vocabulary above so the library stays
searchable across vendors, and put the vendor's quirks in `references/<name>.md`
rather than in the core. Then add it to `VENDORS` in `sync.py`. If you find yourself
special-casing a vendor inside `doclib.py`, the abstraction is leaking.

Run `python3 <skill dir>/scripts/doclib.py` for the self-test (revision parsing and doc-ID regexes,
with the real-world values as fixtures) after touching the core.
