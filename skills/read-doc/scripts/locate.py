#!/usr/bin/env python3
"""Find a term inside one library document, by physical PDF page.

Usage: locate.py build (--all | --book ID) [--jobs N] [--skip-tag TAG]
       locate.py find --book ID --term TERM [--context N] [--limit N]
                      [--offset N] [--max-chars N]
       locate.py page --book ID --pages A[-B] [--max-chars N]

`build` extracts each PDF to text once, into <library>/.read-doc/<id>.txt: a
JSON manifest on the first line, then the pages, one form feed between them.
It re-extracts only what changed, so a run over an unchanged library costs a
stat per book. `find` searches that text and prints the physical PDF pages to
read, which is the point: read those pages, not the whole document. `page`
prints the indexed text of those pages; it locates evidence, while tables,
figures, bitfields and access types are checked on the PDF page itself.

The index lives inside the library so it travels with the library's own sync.
It is derived data and can be deleted at any time.

Exit 0 result or build done, 1 nothing matched (`page`: a page out of range
or with no text), 2 bad usage, 3 library/file/index unavailable or the build
did not finish.
"""
import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from search import DB, LIB, log_lookup, norm, resolve, tag_list  # noqa: E402

INDEX = os.path.join(LIB, ".read-doc")
# pdftotext's output changes with its options; bump VERSION when either changes.
EXTRACTOR = ["pdftotext", "-layout"]
VERSION = 2
JOBS, MAX_JOBS = 4, 32
CONTEXT, LIMIT, MAX_CHARS, MIN_CHARS = 2, 5, 4000, 80
PAGE_CHARS = 10000  # a dense -layout manual page runs 4.5-5 KB; a cut page costs another round trip
# Not technical documentation. Matched as whole tags, never a substring of one.
# Only the bulk build honours this; `find` on such a book still indexes it,
# since skipping is a build cost, not a policy about what may be read.
SKIP_TAGS = ("photography", "portrait", "hdr", "magazine")

PDFS = """
SELECT b.id, b.path, d.name,
       (SELECT group_concat(t.name, ', ') FROM tags t
          JOIN books_tags_link l ON l.tag = t.id WHERE l.book = b.id)
FROM books b JOIN data d ON d.book = b.id AND d.format = 'PDF'
"""


TRUNCATED = " …(cut)"


class Unavailable(Exception):
    """The library, a source file or the index cannot be used as asked."""


def text_path(bid):
    return os.path.join(INDEX, f"{bid}.txt")


def stat_of(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def split_pages(body):
    """Physical pages of an extraction; index 0 is page 1.

    pdftotext ends the last page with a form feed as well, so a plain split
    leaves an empty chunk that would shift every later page number by one.
    """
    pages = body.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def read_header(bid):
    """Just the manifest, without reading the pages.

    A bulk staleness check must not read gigabytes of text, so it stops at the
    first line; the body is validated by read_index() when it is actually used.
    """
    try:
        with open(text_path(bid), encoding="utf-8", errors="replace") as f:
            meta = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def read_index(bid):
    """(manifest, pages), or (None, None) when the file is missing or damaged.

    Manifest and pages share one file, so no reader can pair the manifest of
    one extraction with the text of another; a body that lost pages disagrees
    with the count the manifest recorded and is rejected rather than renumbered.
    """
    try:
        with open(text_path(bid), encoding="utf-8", errors="replace") as f:
            head, body = f.read().split("\n", 1)
        meta = json.loads(head)
    except (OSError, ValueError):
        return None, None
    if not isinstance(meta, dict):
        return None, None
    pages = split_pages(body)
    # Page count alone misses a body truncated inside its last page.
    intact = meta.get("pages") == len(pages) and meta.get("chars") == len(body)
    return (meta, pages) if intact else (None, None)


def is_current(meta, bid, src):
    """Whether this manifest was written from exactly this file."""
    if not meta:
        return False
    size, mtime_ns = stat_of(src)
    return (meta.get("version") == VERSION and meta.get("extractor") == EXTRACTOR
            and meta.get("id") == bid and meta.get("size") == size
            and meta.get("mtime_ns") == mtime_ns
            and meta.get("path") == os.path.relpath(src, LIB))


def page_count(src):
    """Pages according to pdfinfo. Explicitly unavailable when it cannot say."""
    done = subprocess.run(["pdfinfo", src], capture_output=True, text=True)
    m = re.search(r"^Pages:\s+(\d+)$", done.stdout, re.M)
    if done.returncode != 0 or not m:
        raise Unavailable(f"pdfinfo gave no page count for {os.path.basename(src)}"
                          f" (exit {done.returncode}); the extraction cannot be verified")
    return int(m.group(1))


def extract(bid, src):
    """Extract one PDF into the index and return its manifest.

    Written to a temporary name and renamed, so neither a reader nor a sync
    running at the same time ever sees a half-written file.
    """
    os.makedirs(INDEX, exist_ok=True)
    before = stat_of(src)
    expected = page_count(src)
    tmp = text_path(bid) + f".tmp{os.getpid()}"
    try:
        done = subprocess.run([*EXTRACTOR, src, tmp], capture_output=True, text=True)
        if done.returncode != 0:
            last = done.stderr.strip().splitlines()[-1:] or ["no diagnostic"]
            raise Unavailable(f"{bid}: pdftotext exit {done.returncode}: {last[0]}")
        with open(tmp, encoding="utf-8", errors="replace") as f:
            body = f.read()
        pages = len(split_pages(body))
        if pages != expected:
            raise Unavailable(f"{bid}: extracted {pages} pages, pdfinfo says {expected}")
        if not body.strip():
            raise Unavailable(f"{bid}: {expected} page(s) with no text layer (scanned or drawn);"
                              " read the pages themselves, `find` cannot search it")
        if stat_of(src) != before:
            raise Unavailable(f"{bid}: the source changed while it was being extracted")
        meta = {"id": bid, "path": os.path.relpath(src, LIB), "size": before[0],
                "mtime_ns": before[1], "pages": pages, "chars": len(body),
                "extractor": EXTRACTOR, "version": VERSION}
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta) + "\n" + body)
        os.replace(tmp, text_path(bid))
        return meta
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def pdf_row(db, bid):
    row = db.execute("SELECT b.path, d.name FROM books b JOIN data d ON d.book = b.id"
                     " AND d.format = 'PDF' WHERE b.id = ?", (bid,)).fetchone()
    if not row:
        raise Unavailable(f"{bid}: no such book with a PDF in {LIB}")
    return row


def source_of(bid, path, name):
    src = resolve(bid, path, "PDF", name)
    if not src:
        raise Unavailable(f"{bid}: the PDF is not on disk (library mid-sync, or the file was deleted)")
    return src


def open_db():
    if not os.path.exists(DB):
        raise Unavailable(f"no Calibre database at {DB}")
    return sqlite3.connect("file:" + urllib.parse.quote(DB) + "?mode=ro", uri=True)


def ensure(bid):
    """(manifest, pages) for one book, extracting it if missing or stale."""
    with contextlib.closing(open_db()) as db:
        src = source_of(bid, *pdf_row(db, bid))
    meta, pages = read_index(bid)
    if is_current(meta, bid, src):
        return meta, pages
    if not os.access(LIB, os.W_OK):
        raise Unavailable(f"{bid}: not indexed and {LIB} is not writable; run `build` where it is")
    print(f"{bid}: indexing {os.path.basename(src)}", file=sys.stderr)
    extract(bid, src)
    return read_index(bid)


# The rows that follow a register's name where the whole definition is a block.
DEFINITION_ROWS = re.compile(r"^(offset|address offset|reset( value)?|name|access|bits?)\b\s*[:\s]", re.I)


def score(line, term, following=()):
    """Rank a definition above a mention: the register's own section, not a cross-reference.

    Every shape below is a real heading in this library, and each rule matches
    the structure of one: prose that merely contains the name cannot collect
    them by accident. A table-of-contents entry is demoted, though one whose
    dot leaders fall on the following line still reads as a heading here.
    """
    stripped = line.strip()
    low, t = norm(stripped), re.escape(norm(term))
    heading = len(stripped) < 60
    points = 0
    # Only the peripheral-prefixed heading, "USB: SIE_CTRL Register". A bare
    # "SIE_CTRL register" line is just as often prose that wrapped, and ST's
    # numbered headings are carried by the section rule below instead.
    if re.match(r"\S{1,12}:\s+" + t + r"\s+register\s*$", low):
        points += 4
    if re.match(r"bits?\s+[\d:\[\]\s]*" + t + r"\b", low):  # "Bit 15 VTRX: ..."
        points += 3
    if re.match(r"\d+(\.\d+)+\s.*" + t, low):                # "40.6.7 ... (USB_CHEPnR)"
        points += 3
    if re.search(t + r"\s*[:\u2013-]", low):                  # "ERR006223: ..."
        points += 1
    if re.search(t + r"\b.{0,20}\b(register|bit|field|erratum)\b", low):
        points += 1
    if heading:
        points += 1
    # A contents entry, not the section: ST spaces its leaders, Renesas does not.
    # Four, not three: a bare "..." is prose, on 8k lines of a 250-book sample.
    if re.search(r"(?:\.\s*){4,}", low):
        points -= 4
    if sum(bool(DEFINITION_ROWS.match(f.strip())) for f in following) >= 2:
        points += 3
    return points


def find(pages, term, context, limit, offset, max_chars):
    """(distinct hits, page-numbered excerpts, (page, matched line) per excerpt).

    The first excerpt is emitted even when it alone exceeds max_chars, then
    truncated: a caller paging with --offset must always make progress.
    """
    hits = []
    for pno, page in enumerate(pages, 1):
        lines = page.split("\n")
        for i, line in enumerate(lines):
            if norm(term) in norm(line):
                hits.append((-score(line, term, lines[i + 1:i + 4]), pno, i, lines))
    if not hits:
        return 0, [], []
    hits.sort(key=lambda h: h[:3])
    # Two hits a line apart would print nearly the same window twice. Checking
    # only the lines already taken on that page keeps a common term like "EN"
    # from turning this into a scan of every hit so far.
    merged, taken = [], {}
    for hit in hits:
        lines_taken = taken.setdefault(hit[1], set())
        if not any(i in lines_taken for i in range(hit[2] - context, hit[2] + context + 1)):
            merged.append(hit)
            lines_taken.add(hit[2])
    blocks, matched, used = [], [], 0
    for _, pno, i, lines in merged[offset:offset + limit]:
        window = [l.rstrip() for l in lines[max(0, i - context):i + context + 1] if l.strip()]
        # -layout indents the whole page; drop the shared margin, keep relative columns.
        margin = min(len(l) - len(l.lstrip()) for l in window)
        text = "\n".join([f"p{pno}"] + ["  " + l[margin:] for l in window])
        if used + len(text) > max_chars:
            if blocks:
                break
            # The first excerpt is always emitted, so paging makes progress; the
            # page number leads it and MIN_CHARS keeps it inside the cap.
            text = text[:max_chars - len(TRUNCATED)] + TRUNCATED
        blocks.append(text)
        matched.append([pno, lines[i].strip()])
        used += len(text)
    return len(merged), blocks, matched


def render_page(pno, page):
    """One page headed by its number, its -layout margin dropped and its blank runs folded."""
    lines = [l.rstrip() for l in page.split("\n")]
    margin = min((len(l) - len(l.lstrip()) for l in lines if l), default=0)
    body, blank = [], False
    for l in lines:
        if l:
            body.append("  " + l[margin:])
        elif body and not blank:
            body.append("")
        blank = not l
    while body and not body[-1]:
        body.pop()
    return "\n".join([f"p{pno}"] + body)


def page_text(pages, first, last, max_chars):
    """(blocks, first page not shown or None). Same cap rule as find: the first
    page is always emitted, truncated if it alone exceeds max_chars."""
    blocks, used = [], 0
    for pno in range(first, last + 1):
        text = render_page(pno, pages[pno - 1])
        if used + len(text) > max_chars:
            if blocks:
                return blocks, pno
            text = text[:max_chars - len(TRUNCATED)] + TRUNCATED
        blocks.append(text)
        used += len(text)
    return blocks, None


def wanted(db, skip_tags):
    """(PDF rows the bulk build should index, how many its tags skipped)."""
    skip = {re.sub(r"[-_\s]+", " ", norm(t)).strip() for t in skip_tags}
    todo, skipped = [], 0
    for bid, path, name, tags in db.execute(PDFS):
        if skip & set(tag_list(tags)):
            skipped += 1
        else:
            todo.append((bid, path, name))
    return todo, skipped


def prune(db):
    """Remove index files whose book no longer has a PDF in the library."""
    alive = {r[0] for r in db.execute("SELECT b.id FROM books b JOIN data d ON d.book = b.id"
                                      " AND d.format = 'PDF'")}
    removed = 0
    for name in os.listdir(INDEX):
        m = re.fullmatch(r"(\d+)\.txt(?:\.tmp\d+)?", name)  # only names this script writes
        path = os.path.join(INDEX, name)
        if m and int(m.group(1)) not in alive and os.path.isfile(path):
            os.remove(path)
            removed += 1
    return removed


def build(args):
    if not 1 <= args.jobs <= MAX_JOBS:
        raise ValueError(f"--jobs must be 1..{MAX_JOBS}, got {args.jobs}")
    if not os.access(LIB, os.W_OK):
        raise Unavailable(f"{LIB} is not writable; the index lives inside the library")
    with contextlib.closing(open_db()) as db:
        if args.book is not None:
            row = pdf_row(db, args.book)
            todo, skipped = [(args.book, row[0], row[1])], 0
        else:
            todo, skipped = wanted(db, args.skip_tag or SKIP_TAGS)
    os.makedirs(INDEX, exist_ok=True)

    counts = {"indexed": 0, "unchanged": 0, "skipped": skipped, "unavailable": 0, "failed": 0}
    problems = []

    def one_book(item):
        bid, path, name = item
        try:
            src = source_of(bid, path, name)
            if is_current(read_header(bid), bid, src):
                return "unchanged", None
            extract(bid, src)
            return "indexed", None
        except Unavailable as e:
            return "unavailable", str(e)
        except (OSError, sqlite3.Error) as e:
            return "failed", f"{bid}: {e}"

    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for n, (state, problem) in enumerate(pool.map(one_book, todo), 1):
            counts[state] += 1
            if problem:
                problems.append(problem)
            if n % 200 == 0:
                print(f"  {n}/{len(todo)} ({time.time() - started:.0f}s)", file=sys.stderr)

    if args.book is None:
        with contextlib.closing(open_db()) as db:
            pruned = prune(db)
    else:
        pruned = 0
    size = sum(os.path.getsize(os.path.join(INDEX, f)) for f in os.listdir(INDEX))
    print(f"{len(todo) + skipped} book(s) in {time.time() - started:.0f}s: "
          + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
          + (f", {pruned} pruned" if pruned else "")
          + f"; index {INDEX} {size // 1048576} MB")
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    return 3 if counts["unavailable"] or counts["failed"] else 0


def check_max_chars(n):
    if n < MIN_CHARS:
        raise ValueError(f"--max-chars must be at least {MIN_CHARS}, got {n};"
                         " a smaller cap cannot hold a page number and a line")


def indexed(bid):
    """(manifest, pages, title, the result fields every lookup records)."""
    meta, pages = ensure(bid)
    with contextlib.closing(open_db()) as db:
        row = db.execute("SELECT title FROM books WHERE id = ?", (bid,)).fetchone()
    title = row[0] if row else None  # a label only: a row gone since ensure() costs the title, not the lookup
    if not meta:
        raise Unavailable(f"{bid}: the index file is unreadable; delete it and retry")
    return meta, pages, {"title": title,
                         "source": {k: meta[k] for k in ("path", "size", "mtime_ns", "pages")}}


def find_cmd(args):
    """Exit code; the lookup history's result is left in args.result."""
    if args.limit < 1:
        raise ValueError(f"--limit must be at least 1, got {args.limit}")
    check_max_chars(args.max_chars)
    if args.context < 0 or args.offset < 0:
        raise ValueError("--context and --offset cannot be negative")
    if not args.term.strip():
        raise ValueError("--term cannot be blank")
    meta, pages, recorded = indexed(args.book)
    total, blocks, matched = find(pages, args.term, args.context, args.limit, args.offset, args.max_chars)
    args.result = {**recorded, "total_hits": total, "hits": matched}
    if not total:
        print(f"{args.book}: {args.term!r} is not in the extracted text of any page "
              f"({meta['pages']} pages searched); a figure or scan holds none")
        return 1
    print(f"{args.book}: {total} line(s) contain {args.term!r} in {meta['pages']} pages;"
          f" showing {len(blocks)}" + (f" from {args.offset}" if args.offset else "")
          + f" — read these pages of {meta['path']}")
    for b in blocks:
        print(b)
    seen = args.offset + len(blocks)
    if seen < total:
        print(f"({total - seen} more; --offset {seen})")
    return 0


def page_range(value):
    """[A, B] from "A" or "A-B", physical PDF pages as `find` prints them."""
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", value.strip())
    first, last = (int(m.group(1)), int(m.group(2) or m.group(1))) if m else (0, 0)
    if not m or not 1 <= first <= last:
        raise argparse.ArgumentTypeError(f"must be A or A-B with 1 <= A <= B, got {value!r}")
    return [first, last]


def page_cmd(args):
    """Exit code; the lookup history's result is left in args.result."""
    check_max_chars(args.max_chars)
    first, last = args.pages
    asked = f"pages {first}-{last}" if last > first else f"page {first}"
    meta, pages, recorded = indexed(args.book)
    args.result = {**recorded, "shown": [], "empty": []}
    if last > meta["pages"]:
        print(f"{args.book}: has {meta['pages']} pages; {asked} is out of range")
        return 1
    blocks, rest = page_text(pages, first, last, args.max_chars)
    shown = list(range(first, rest or last + 1))
    empty = [p for p in shown if not pages[p - 1].strip()]
    args.result.update(shown=shown, empty=empty)
    print(f"{args.book}: {asked} of {meta['pages']}, extracted text of {meta['path']}")
    for b in blocks:
        print(b)
    if blocks[0].endswith(TRUNCATED):
        print(f"(p{first} cut at --max-chars {args.max_chars})")
    if rest:
        print(f"(not shown; --pages {rest}" + (f"-{last})" if last > rest else ")"))
    if empty:
        print("(no extracted text on p" + ", p".join(map(str, empty))
              + ": a figure, scan or blank page)")
    span = f"{first}-{shown[-1]}" if shown[-1] > first else str(first)
    print("Tables, figures, register bitfields and access types are not reliable here: check them"
          f" on the PDF page itself (Read {os.path.join(LIB, meta['path'])} with pages={span}).")
    return 1 if empty else 0


LOGGED = {"find": ("book", "term", "context", "limit", "offset", "max_chars"),
          "page": ("book", "pages", "max_chars")}


def parser():
    p = argparse.ArgumentParser(prog="locate.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="extract PDFs to the index")
    which = b.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true", help="every PDF except skipped tags")
    which.add_argument("--book", type=int, help="one book id")
    b.add_argument("--jobs", type=int, default=JOBS, help=f"parallel extractions (1..{MAX_JOBS})")
    b.add_argument("--skip-tag", action="append", help=f"replaces the default: {', '.join(SKIP_TAGS)}")
    b.set_defaults(run=build)

    f = sub.add_parser("find", help="search one indexed book")
    f.add_argument("--book", type=int, required=True)
    f.add_argument("--term", required=True)
    f.add_argument("--context", type=int, default=CONTEXT, help="lines around each hit")
    f.add_argument("--limit", type=int, default=LIMIT, help="how many excerpts")
    f.add_argument("--offset", type=int, default=0, help="skip this many excerpts")
    f.add_argument("--max-chars", type=int, default=MAX_CHARS, help=f"output cap (>= {MIN_CHARS})")
    f.set_defaults(run=find_cmd)

    g = sub.add_parser("page", help="print the indexed text of pages of one book")
    g.add_argument("--book", type=int, required=True)
    g.add_argument("--pages", type=page_range, required=True, help="A or A-B, physical PDF pages")
    g.add_argument("--max-chars", type=int, default=PAGE_CHARS, help=f"output cap (>= {MIN_CHARS})")
    g.set_defaults(run=page_cmd)
    return p


def main(argv):
    args = parser().parse_args(argv)
    started, error, args.result = time.monotonic(), None, None
    try:
        code = args.run(args)
    except ValueError as e:
        code, error = 2, str(e)
    except Unavailable as e:
        code, error = 3, str(e)
    except (OSError, sqlite3.Error) as e:
        code, error = 3, f"{type(e).__name__}: {e}"
    if error:
        print(error, file=sys.stderr)
    if args.command in LOGGED:
        log_lookup(LIB, args.command, {k: getattr(args, k) for k in LOGGED[args.command]},
                   started, code, error, args.result if code in (0, 1) else None)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
