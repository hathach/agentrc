#!/usr/bin/env python3
"""List and replay searches recorded by the read-doc skill.

Usage: history.py list [--session PREFIX] [--term TEXT] [--since YYYY-MM-DD]
       history.py show ID

Reads search.HISTORY, ~/.local/state/read-doc/lookups.jsonl unless
XDG_STATE_HOME moves it. `list` prints matching lookups oldest first. `show` prints one complete record
and a shell command that repeats it with the recorded library and options.

Exit 0 printed a result, 1 nothing matched, 2 bad usage,
3 the history is unavailable.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from search import HISTORY  # noqa: E402


VERSION = 1
TITLE = 40  # characters of a book title in a list line
ID = re.compile(r"[0-9a-f]{12}")


def load():
    records = []
    try:
        with open(HISTORY, encoding="utf-8", errors="replace") as history:
            for lineno, line in enumerate(history, 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise TypeError("record is not an object")
                    if record.get("v") != VERSION:
                        raise ValueError(f"unknown version {record.get('v')!r}")
                    timestamp = datetime.datetime.strptime(record["ts"], "%Y-%m-%dT%H:%M:%SZ")
                    summary(record)
                    repeat_command(record)
                except (json.JSONDecodeError, KeyError, TypeError, AttributeError,
                        IndexError, ValueError) as e:
                    print(f"{HISTORY}: line {lineno}: malformed record: {e}", file=sys.stderr)
                    continue
                records.append((timestamp, lineno, record))
    except FileNotFoundError:
        print(f"{HISTORY}: nothing was logged yet", file=sys.stderr)
        return None, 1
    except OSError as e:
        print(f"the read-doc history at {HISTORY} is unavailable: {e}", file=sys.stderr)
        return None, 3
    return records, 0


def session_prefix(record):
    value = record["session"]["claude"] or record["session"]["codex"]
    return value[:8] if value else "-"


def matches(record, args, timestamp):
    if args.session:
        prefix = args.session.casefold()
        if not any(value and value.casefold().startswith(prefix)
                   for value in record["session"].values()):
            return False
    query = record["query"]
    if args.term:
        term = args.term.casefold()
        values = (query["keywords"] if record["op"] == "search"
                  else [] if record["op"] == "page" else [query["term"]])
        if not any(term in value.casefold() for value in values):
            return False
    return args.since is None or timestamp.date() >= args.since


def book(bid, title):
    """A book as a reader names it; records written before titles were logged have none."""
    if not title:
        return f"book {bid}"
    return f"{title if len(title) <= TITLE else title[:TITLE - 1] + '…'} ({bid})"


def summary(record):
    query, result = record["query"], record["result"]
    if record["op"] == "search":
        rendered = "query=" + json.dumps(query["keywords"], ensure_ascii=False,
                                         separators=(",", ":"))
        top = [] if result is None else [book(bid, title) for bid, _, title in result["books"][:3]]
        rendered += " books=" + ("; ".join(top) if top else "-")
    elif record["op"] == "page":
        title = result.get("title") if result else None
        first, last = query["pages"]
        rendered = f"pages={first}" + (f"-{last}" if last != first else "") + f" of {book(query['book'], title)}"
    else:
        title = result.get("title") if result else None
        rendered = (f"term={json.dumps(query['term'], ensure_ascii=False)}"
                    f" in {book(query['book'], title)}")
        top = [] if result is None else [str(hit[0]) for hit in result["hits"][:3]]
        rendered += " pages=" + (",".join(top) if top else "-")
    return (f"{record['ts']} {session_prefix(record)} {record['op']} {record['id']} "
            f"{rendered} exit={record['exit']}")


def list_records(args):
    records, code = load()
    if records is None:
        return code
    selected = [(timestamp, lineno, record) for timestamp, lineno, record in records
                if matches(record, args, timestamp)]
    if not selected:
        print("no matching lookup history")
        return 1
    for _, _, record in sorted(selected, key=lambda item: (item[0], item[1])):
        print(summary(record))
    return 0


def repeat_command(record):
    query = record["query"]
    directory = os.path.dirname(os.path.abspath(__file__))
    if record["op"] == "search":
        command = ["python3", os.path.join(directory, "search.py")]
        if query["any"]:
            command.append("--any")
        if query["kind"] is not None:
            command.extend(("--kind", query["kind"]))
        command.extend(("--limit", str(query["limit"]), "--", *query["keywords"]))
    elif record["op"] == "page":
        first, last = query["pages"]
        command = ["python3", os.path.join(directory, "locate.py"), "page", "--book", str(query["book"]),
                   "--pages", f"{first}-{last}", "--max-chars", str(query["max_chars"])]
    else:
        command = ["python3", os.path.join(directory, "locate.py"), "find",
                   "--book", str(query["book"]), "--term=" + query["term"],
                   "--context", str(query["context"]), "--limit", str(query["limit"]),
                   "--offset", str(query["offset"]), "--max-chars", str(query["max_chars"])]
    return f"CALIBRE_LIBRARY={shlex.quote(record['library'])} {shlex.join(command)}"


def show_record(args):
    records, code = load()
    if records is None:
        return code
    for _, _, record in records:
        if record["id"] == args.id:
            print(json.dumps(record, indent=2, ensure_ascii=False))
            print()
            print(repeat_command(record))
            return 0
    print(f"no lookup history with id {args.id}")
    return 1


def since(value):
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from e
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD")
    return parsed


def lookup_id(value):
    if not ID.fullmatch(value):
        raise argparse.ArgumentTypeError("must be 12 lowercase hex digits")
    return value


def parser():
    p = argparse.ArgumentParser(prog="history.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list recorded lookups oldest first")
    listing.add_argument("--session", help="only a Claude or Codex session-id prefix")
    listing.add_argument("--term", help="substring of a search keyword or find term")
    listing.add_argument("--since", type=since, help="only this UTC date or later")
    listing.set_defaults(run=list_records)

    show = sub.add_parser("show", help="print one complete record and replay command")
    show.add_argument("id", type=lookup_id)
    show.set_defaults(run=show_record)
    return p


def main(argv):
    args = parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
