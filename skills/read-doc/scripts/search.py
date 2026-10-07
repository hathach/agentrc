#!/usr/bin/env python3
"""Search the Calibre library by metadata and print matching books.

Usage: search.py KEYWORD [KEYWORD...]        all keywords must match (AND)
       search.py --any KEYWORD [KEYWORD...]  any keyword matches (OR)
       search.py --kind reference-manual --limit 20 stm32h7

Matches title, authors, tags, series, publisher, description and stored
filename; a part number also matches the family name a vendor filed it under
(STM32F407 finds STM32F4xx), and such a row says "(family match)". Prints the
book id to pass to locate.py, a count per document kind, and the base
documentation first: the manual, specification or datasheet a
register question is answered from, before the errata that amend it and the
application notes that use it.

Exit 0 matched, 1 nothing matched, 2 bad usage, 3 the library is unavailable.

Each lookup, here and in locate.py find, is appended to HISTORY and named on
stderr by its id; history.py lists and shows them. READ_DOC_HISTORY=0 skips it.
"""
import argparse
import collections
import contextlib
import fcntl
import glob
import json
import os
import re
import socket
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
import uuid

LIB = os.path.realpath(os.path.expanduser(os.environ.get("CALIBRE_LIBRARY") or "~/Documents/calibre-library"))
DB = os.path.join(LIB, "metadata.db")
# Per machine, outside the library: the library syncs between machines.
HISTORY = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                       "read-doc", "lookups.jsonl")
LIMIT = 10

# Display order: the document a register question is answered from comes first.
# Errata sit below the base documentation they amend, not because they matter
# less — they override it — but because they are read in addition to it.
KINDS = ("reference-manual", "specification", "datasheet", "errata",
         "programming-manual", "user-manual", "application-note", "schematic", "other")
# Which kind wins when a book carries several kind tags, or its title several
# document words. A different question from display order: an errata amending a
# reference manual names both, and is errata.
TAG_PRECEDENCE = ("errata", "reference-manual", "datasheet", "specification",
                  "programming-manual", "user-manual", "application-note", "schematic", "other")
# One vocabulary for the document words, read from a book's tags and from its
# title. Never a substring of a tag: many books carry a whole abstract as one,
# and "this application note..." is not a kind.
ALIASES = {
    "errata": "errata", "board errata": "errata",
    "reference manual": "reference-manual", "technical reference": "reference-manual",
    "databook": "reference-manual", "core reference": "reference-manual",
    "programming manual": "programming-manual", "programming guide": "programming-manual",
    "datasheet": "datasheet", "data sheet": "datasheet",
    # Every spelling below is one the library actually uses.
    "user manual": "user-manual", "users manual": "user-manual",
    "user's manual": "user-manual",
    "user guide": "user-manual", "users guide": "user-manual",
    "user's guide": "user-manual",
    "application note": "application-note",
    "schematic": "schematic",
    # A product or protocol specification is a document type, not a claim about
    # hardware: an API specification is one too.
    "specification": "specification",
}
# The vendor prefix that opens a title, read before any document word.
TITLE_PREFIXES = (
    (r"^rm\d", "reference-manual"), (r"^es\d", "errata"), (r"^ds\d", "datasheet"),
    (r"^um\d", "user-manual"), (r"^pm\d", "programming-manual"), (r"^an\d", "application-note"),
)
# One compiled table, used two ways: fullmatch against a whole tag, search
# inside a title. Ordered by TAG_PRECEDENCE, so a title carrying two document
# words resolves the same way a book carrying two kind tags does: "Reference
# Manual Errata" is errata. Length breaks ties within one kind. Trailing s? for
# the plural in a title ("...User Guides"); no other inflection is accepted.
DOC_WORDS = tuple(
    (re.compile(r"\b" + r"\s+".join(map(re.escape, a.split())) + r"s?\b"), kind)
    for a, kind in sorted(ALIASES.items(),
                          key=lambda kv: (TAG_PRECEDENCE.index(kv[1]), -len(kv[0])))
)

# A USB controller core's own documentation is the register reference for every
# MCU that licensed it, and some of it is titled with no document word at all
# ("Mentor MUSBMHDRC USB 2.0 Multi-Point Dual-Role Controller").
CORE_TAGS = ("dwc2", "dwc3", "chipidea", "musb")

# Vendors file documents under a family name that stands for its members:
# STM32F4xx covers STM32F407, LPC55S6x LPC55S69, USB251xB USB2514B. An x is a
# wildcard only after a digit or another wildcard x, so RX65N, MAX32690 and
# PIC32MX stay literal; a literal one in that position (CH32X035) still reads
# as a wildcard, so such a row is marked rather than passed off as exact.
FAMILY_TOKEN = re.compile(r"[0-9a-z]*\dx[0-9a-z]*")
PART_CHARS = frozenset("0123456789abcdefghijklmnopqrstuvwxyz")

QUERY = """
SELECT b.id, b.title, b.path,
       (SELECT group_concat(a.name, ', ') FROM authors a
          JOIN books_authors_link l ON l.author = a.id WHERE l.book = b.id),
       (SELECT group_concat(t.name, ', ') FROM tags t
          JOIN books_tags_link l ON l.tag = t.id WHERE l.book = b.id),
       (SELECT group_concat(s.name, ', ') FROM series s
          JOIN books_series_link l ON l.series = s.id WHERE l.book = b.id),
       (SELECT group_concat(p.name, ', ') FROM publishers p
          JOIN books_publishers_link l ON l.publisher = p.id WHERE l.book = b.id),
       (SELECT c.text FROM comments c WHERE c.book = b.id),
       (SELECT group_concat(d.format || '/' || d.name, char(10)) FROM data d WHERE d.book = b.id)
FROM books b
"""

_authors = None


def norm(s):
    # NFKC + casefold so MICRO SIGN/GREEK MU and dashes compare equal. NFKC
    # leaves the curly apostrophe alone, so fold it too: the library spells
    # "User's Manual" both ways.
    return unicodedata.normalize("NFKC", s).casefold().replace("\u2019", "'")


def tag_list(tags):
    """Calibre joins tags with ', '; normalize each for whole-tag comparison."""
    return [re.sub(r"[-_\s]+", " ", norm(t)).strip() for t in (tags or "").split(", ") if t.strip()]


def kind_summary(counts):
    """'2 datasheet, 1 errata': each kind's count, in KINDS order."""
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda c: KINDS.index(c[0])))


def kind_of(title, tags):
    """The document kind, from a whole tag, else the title, else "other".

    A book tagged both "errata" and "datasheet" classifies by TAG_PRECEDENCE,
    so the answer does not depend on the order Calibre returns the tags in.
    """
    named = tag_list(tags)
    kinds = {kind for pattern, kind in DOC_WORDS for tag in named if pattern.fullmatch(tag)}
    if kinds:
        return min(kinds, key=TAG_PRECEDENCE.index)
    t = norm(title)
    for pattern, kind in TITLE_PREFIXES:
        if re.match(pattern, t):
            return kind
    for pattern, kind in DOC_WORDS:
        if pattern.search(t):
            return kind
    return "reference-manual" if set(CORE_TAGS) & set(named) else "other"


def family_covers(token, keyword):
    """Whether keyword is part of some member of the family token names.

    The keyword must carry the family's own characters, starting on one and
    at least half of it, so a short word like "adc" cannot hide in RT1xxx.
    """
    wild = [False] * len(token)
    for i, c in enumerate(token):
        wild[i] = c == "x" and i > 0 and (token[i - 1].isdigit() or wild[i - 1])
    for i in range(len(token) - len(keyword) + 1):
        window = range(i, i + len(keyword))
        if wild[i] or 2 * sum(not wild[w] for w in window) < len(keyword):
            continue
        if all(token[w] == c or (wild[w] and c in PART_CHARS) for w, c in zip(window, keyword)):
            return True
    return False


def contains(hay, keyword):
    return keyword in hay or any(family_covers(t, keyword) for t in FAMILY_TOKEN.findall(hay))


def resolve(bid, path, fmt, name):
    """Absolute path of one format row, or None if the file is not on disk.

    Calibre renames `<author>/<title> (<id>)` when metadata is edited and leaves
    the old directory behind, so on a miss retry by the stable book id.
    """
    ext = "." + fmt.lower()
    exact = os.path.join(LIB, path, name + ext)
    if os.path.exists(exact):
        return exact
    global _authors
    if _authors is None:
        _authors = {}
        for d in os.listdir(LIB):  # case-only duplicates exist on a case-sensitive mount
            _authors.setdefault(d.lower(), []).append(d)
    for author in _authors.get(path.split("/")[0].lower(), ()):
        for d in glob.glob(os.path.join(glob.escape(os.path.join(LIB, author)), "* (%d)" % bid)):
            for f in sorted(glob.glob(os.path.join(glob.escape(d), "*" + ext))):
                return f
    return None


def log_lookup(library, op, query, started, code, error, result):
    """Append one lookup record to HISTORY. Failing to is a warning: the lookup
    already answered, and its exit code stays the lookup's."""
    if os.environ.get("READ_DOC_HISTORY") == "0":
        return
    rid = uuid.uuid4().hex[:12]
    record = {"v": 1, "id": rid, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "host": socket.gethostname(), "elapsed_ms": round((time.monotonic() - started) * 1000),
              "library": library, "op": op,
              "session": {"claude": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
                          "codex": os.environ.get("CODEX_THREAD_ID") or None},
              "query": query, "exit": code, "error": error, "result": result}
    line = (json.dumps(record) + "\n").encode()
    try:
        os.makedirs(os.path.dirname(HISTORY), mode=0o700, exist_ok=True)
        fd = os.open(HISTORY, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            end = os.lseek(fd, 0, os.SEEK_END)
            if end and os.pread(fd, 1, end - 1) != b"\n":
                line = b"\n" + line  # a writer killed mid-line must not swallow this record
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    except OSError as e:
        print(f"lookup history not written: {e}", file=sys.stderr)
        return
    print(f"lookup {rid} logged", file=sys.stderr)


def parser():
    p = argparse.ArgumentParser(prog="search.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("keyword", nargs="*")
    p.add_argument("--any", action="store_true", help="any keyword matches, instead of all")
    p.add_argument("--kind", choices=KINDS, help="only this document kind")
    p.add_argument("--limit", type=int, default=LIMIT, help="how many to print (0 for all)")
    p.add_argument("--path", action="store_true", help="print each result's file path")
    return p


def run(args):
    """(exit code, the result recorded in the lookup history)."""
    keywords = [norm(k) for k in args.keyword]
    if not keywords:
        parser().print_help(sys.stderr)
        raise ValueError("no keyword given")
    if args.limit < 0:
        raise ValueError("--limit cannot be negative")
    db_mtime_ns = os.stat(DB).st_mtime_ns
    with contextlib.closing(sqlite3.connect("file:" + urllib.parse.quote(DB) + "?mode=ro",
                                            uri=True)) as db:
        rows = db.execute(QUERY).fetchall()
    hits, other_kinds = [], collections.Counter()
    for bid, title, path, authors, tags, series, publisher, comments, files in rows:
        entries = [e.split("/", 1) for e in (files or "").split("\n") if e]
        hay = norm(" ".join(x for x in (title, authors, tags, series, publisher, comments) if x)
                   + " " + " ".join(n for _, n in entries))
        matched = [k for k in keywords if contains(hay, k)]
        found = len(matched)
        if not found or (not args.any and found < len(keywords)):
            continue
        kind = kind_of(title, tags)
        if args.kind and kind != args.kind:
            other_kinds[kind] += 1
            continue
        in_title = sum(contains(norm(title), k) for k in keywords)
        family = any(k not in hay for k in matched)
        hits.append((KINDS.index(kind), -found, -in_title, title, kind, bid, tags, path, entries, family))

    if not hits:
        # A part's base document is not always the kind asked for (NXP files many as user manuals).
        print(f"no {args.kind}; without --kind: {kind_summary(other_kinds)}" if other_kinds else "no match")
        return 1, {"db_mtime_ns": db_mtime_ns, "total": 0, "kinds": {}, "books": []}

    hits.sort(key=lambda h: h[:4])  # tags/path are not comparable across rows
    counts = dict(collections.Counter(h[4] for h in hits))
    shown = hits if args.limit == 0 else hits[:args.limit]
    print(f"{len(hits)} book(s): {kind_summary(counts)}")
    if len(shown) < len(hits):
        print(f"showing {len(shown)}; --limit N for more, --kind K to filter, or add a keyword")
    for _, _, _, title, kind, bid, tags, path, entries, family in shown:
        # Some books carry a whole abstract as one "tag"; the line is a label, not the metadata.
        label = (tags or "")[:60].rstrip(", ")
        print(f"{bid}  {title[:96]}  [{kind}]" + ("  (family match)" if family else "")
              + (f"  {label}" if label else ""))
        if args.path and not entries:
            print("  (no file in this library)")
        for fmt, name in entries if args.path else ():
            p = resolve(bid, path, fmt, name)
            print(f"  {fmt} {p}" if p else f"  {fmt} MISSING (library mid-sync or file deleted)")
    return 0, {"db_mtime_ns": db_mtime_ns, "total": len(hits), "kinds": counts,
               "books": [[bid, kind, title] for _, _, _, title, kind, bid, *_ in shown]}


def main(argv):
    """Exit 3 covers the whole run, not just the query: --path walks the
    library tree, and a half-synced one must not read as "nothing matched"."""
    args = parser().parse_intermixed_args(argv)
    started, error, result = time.monotonic(), None, None
    try:
        code, result = run(args)
    except ValueError as e:
        code, error = 2, str(e)
    except (OSError, sqlite3.Error) as e:
        code, error = 3, f"the Calibre library at {LIB} is unavailable: {e}"
    if error:
        print(error, file=sys.stderr)
    log_lookup(LIB, "search", {"keywords": args.keyword, "any": args.any, "kind": args.kind,
                               "limit": args.limit}, started, code, error, result)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
