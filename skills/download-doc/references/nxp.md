# NXP

The hard vendor. Enumeration is open and complete; *downloading* is where it gets
difficult — roughly half the Arm-MCU catalogue and nearly every reference manual sits
behind a login, and some of it behind a licence or an approval form.

The adapter handles the open path end to end and marks the rest `gated`. It does not
log in, and it does not accept licences.

## Enumeration — the Documentation Library REST API

```
https://www.nxp.com/webapp-rest/api/search/getAsset/allResults/{collection=documents&depth=2&start=0&max=100&sorting=sort_date.desc&language=en&query=typeTax>><TYPE>::deviceTax>><PATH>&app=DocLibrary&parameters=typeTax.type.deviceTax.applicationTax.DocLanguage_en.DocLanguage_zh.DocLanguage_ja.application}
```

The literal `{ }` are **part of the path**, not syntax — they must be sent.

⚠️ **With curl you must pass `-g`.** Otherwise curl treats the braces as a glob set,
silently strips them, and requests a malformed URL. The API answers that with 406, or
404 plus a 745-byte "Page not available" page — which looks precisely like an Akamai
bot-block and is nothing of the sort. `curl -g` returns 200 and a full result set;
this cost real debugging time and was misdiagnosed as blocking twice. wget doesn't
glob, so it needs no equivalent flag.

Where NXP *does* discriminate by client shape is the plain HTML paths
(`https://www.nxp.com/…` pages, not the API): plain curl has been seen to get the
745-byte page there where a full browser header set gets a 200. That's why
`doclib.http_get` validates the payload and retries with the other client instead of
trusting any single one.

### Taxonomy

`typeTax`:

| Kind | Code | Kind | Code |
|---|---|---|---|
| Errata | `t522` | User Manual | `t1009` |
| Data Sheet | `t520` | Application Note | `t789` |
| Reference Manual | `t877` | User Guide | `t792` |

`deviceTax` needs the **full path** — a bare leaf code returns zero results:

| Branch | Path |
|---|---|
| Arm Microcontrollers (all modern) | `c731_c1770` |
| General-purpose MCUs | `c731_c1770_c173` |
| i.MX RT | `c731_c1770_c1508` |
| LPC2000 (ARM7, legacy) | `c731_c1770_c750` |
| LPC3000 (ARM9, legacy) | `c731_c1770_c753` |

"Modern Arm Cortex MCU" = `c731_c1770` minus the LPC2000/LPC3000 branches.

Don't guess codes. With `depth=2` or higher the response carries a `filters[]` facet
tree with counts — read the taxonomy out of it.

### Response shape

`totalcount` plus `results[]`, each with `url` (often a direct PDF), `title`,
`summary`, and `metaData` carrying `code` (the document code — this becomes the
identifier) `RevisionNo`, and `Size` in KB. Paginate with `start`/`max`; `max=100`
works.

`RevisionNo` is free text and often a **date** — see SKILL.md on why revision
comparison refuses to order dates against numbers.

## Downloading

1. **Try the indexed `url`.** For most data sheets and errata it's a direct PDF.
2. **Fall back to the direct path**, which is unauthenticated and rescues a good share
   of docs whose indexed URL is a `webapp/` link:
   ```
   https://www.nxp.com/docs/en/{errata,data-sheet,reference-manual,user-manual,application-note}/<CODE>.pdf
   ```
3. **Otherwise it's gated** — `webapp/Download?colCode=` and `ext_download.jsp`
   redirect to `login.nxp.com`.

Always verify you got a PDF: NXP serves HTML login and error pages with a 200 status,
so check the `%PDF` magic rather than the status code. `doclib.download` does this.

## The gated flow (signed-in browser)

Requires the user's own NXP session. Ask before starting — this uses their account.

- Chrome blocks the 2nd and later automatic download per origin unless the site
  permission is set. It must be `[*.]nxp.com`; a bare `nxp.com` exception does **not**
  match `www.nxp.com`. Fix this first or everything after the first doc silently fails.
- Fastest reliable pattern: 4 tabs, round-robin `navigate` to
  `webapp/Download?colCode=<CODE>`, no explicit waits, ~12 per batch, then retry the
  misses at the end.
- Filenames don't match the code — casing differs, Chrome appends ` (1)`, and `DRM*`
  design reference manuals arrive as a `.zip` containing `<CODE>.pdf` plus HW/SW
  archives. Sweep case-insensitively and extension-agnostically
  (`doclib.sweep_download_dir`), or the same file re-downloads on every retry. This
  cost 51 redundant downloads and 8 copies of one document before it was caught.

### Licences and approval forms — get consent

- **`sps/download/license.jsp`** is a click-through EULA, in two flavours: a short
  confidentiality note, and the much longer `LA_OPT_BASE_LICENSE` software licence
  agreement. The long one **only submits after the licence text box has been scrolled
  to the bottom** — scroll inside the box, then click. Find the button by element
  ref, never by coordinates: its position shifts with viewport and description length,
  and refs are invalidated on every page load. Acceptance persists for the rest of the
  browser session, so later documents skip straight to `preDownload.jsp`.

  **Accepting a licence is a legal act in the user's name. Ask first, every session.**

- **`mod_download.jsp?appType=moderated`** is *not* a EULA. It's a request form asking
  for the user's NXP salesperson/FAE name and email, submitted for approval. Never
  submit it automatically — report it and let the user decide.

- **Approval-gated ("unknown state")** is a third gate, distinct from both the EULA
  and the moderated form. Navigating `webapp/Download?colCode=<CODE>` while signed in
  lands on a page reading:

  > The request by \<account hash\> to download AN11069 is in an unknown state.
  > Valid states are "Pending", "Approved", "Rejected"

  No form, no licence — the document needs a download request approved by NXP before
  it will ever serve. Detect it by the URL staying on `webapp/Download?colCode=`
  instead of redirecting to `preDownload.jsp`, then matching `is in an unknown state`
  in the page text. Treat as unobtainable and report it, like NDA.

  Measured share: of 272 app notes curl couldn't fetch, 124 came through the browser
  and the remaining **148 were this class** — 106 AN10xxx–AN12xxx plus all 25 TN000xx
  technical notes. It skews to LPC-era material but isn't predictable from the code:
  some AN12xxx are gated while AN13xxx/AN14xxx generally aren't. Probe per document;
  don't infer from the number.

- **`*-NDA` codes** need an NDA and are unobtainable this way. A few codes in NXP's
  own index are dead 404s. Both belong in the skipped report, not in a retry loop.

**Distinguish "will never come" from "came too fast".** With tabs round-robining,
a document that *did* reach `preDownload.jsp` can still produce no file if its tab is
renavigated too soon, and succeeds on a slower retry. So: reached `preDownload.jsp`
but no file → retry with more pacing; `unknown state`, moderated, or NDA → stop and
report. Conflating them burns rounds retrying documents that can never arrive.

## Mask-set errata

Errata codes encode a mask set, and codes that look like revisions of each other are
usually **different products**: `KINETIS_K_0N50M` covers MK22FN parts while
`KINETIS_V_0N50M` covers MKV31F. Never dedupe on mask or title similarity — dedupe on
the document code, which the identifier already does.

Genuine supersessions do happen (a rebrand replacing an older generic document, e.g.
`KEA64_2N22J` for `KINETIS_E_2N22J`). Confirm one by extracting the "applies to mask X
for these products" list with `pdftotext` and comparing the product lists — not by
comparing titles.
