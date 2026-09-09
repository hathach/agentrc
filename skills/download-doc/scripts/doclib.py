#!/usr/bin/env python3
"""Shared core for the download-doc skill.

Transport, revision comparison, and Calibre I/O live here; vendor adapters supply
only enumerate(). That split is the point — adding a vendor should mean writing one
adapter, not another pipeline.

Nothing here is vendor-specific. If you find yourself special-casing ST or NXP in
this file, the abstraction is leaking and the fix probably belongs in the adapter.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

LIBRARY = Path.home() / "Documents" / "calibre-library"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/139.0.0.0 Safari/537.36")


# ---------------------------------------------------------------- transport
#
# Don't trust the exit status; validate the payload. Both vendors sit behind Akamai
# and both serve HTML error/login pages with a 200, so "it worked" has to mean "the
# JSON parsed" or "the file starts with %PDF" — never "the status was 0".
#
# Which client succeeds is not stable, so we try more than one and let validation
# decide. Two failure modes look like blocking but aren't, and both cost real time
# to diagnose:
#
#   * curl globs `{ }`. NXP's API takes its query as a brace-wrapped path segment,
#     and without -g curl silently strips the braces and requests a malformed URL —
#     which comes back 406, or 404 with a 745-byte "Page not available" page. That
#     reads exactly like bot-blocking and is not.
#   * `%{size_download}` counts *wire* bytes. With --compressed a 29,896-byte JSON
#     body reports 3,878, which reads like a truncated or blocked response.
#
# Genuine blocking does happen (st.com drops connections after a burst of index
# fetches, sometimes for every client in the process) but it is transient and has
# been observed to differ between processes on the same host at the same moment.
# So: retry with the other client, then back off — and don't hard-code a winner.

def _wget(url: str, timeout: int, accept: str) -> bytes:
    p = subprocess.run(["wget", "-q", "-O", "-", "-T", str(timeout), "-t", "2",
                        f"--user-agent={UA}", f"--header=Accept: {accept}", url],
                       capture_output=True)
    return p.stdout if p.returncode == 0 else b""


def _curl(url: str, timeout: int, accept: str) -> bytes:
    # -g is mandatory: NXP's brace-wrapped query is a literal path segment.
    p = subprocess.run(["curl", "-sg", "--compressed", "-m", str(timeout),
                        "-A", UA, "-H", f"Accept: {accept}", url], capture_output=True)
    return p.stdout if p.returncode == 0 else b""


def http_get(url: str, timeout: int = 60, accept: str = "application/json",
             validate=None) -> bytes:
    """GET a URL, returning validated bytes. Raises RuntimeError if no client
    produced a payload that passes `validate`."""
    problems = []
    for name, client in (("wget", _wget), ("curl", _curl)):
        data = client(url, timeout, accept)
        if not data:
            problems.append(f"{name}: no payload")
            continue
        if validate and not validate(data):
            problems.append(f"{name}: {len(data)} bytes but failed validation")
            continue
        return data
    raise RuntimeError(f"fetch failed [{'; '.join(problems)}]: {url}")


def _is_json(b: bytes) -> bool:
    try:
        json.loads(b)
        return True
    except Exception:
        return False


def get_json(url: str, timeout: int = 60) -> dict:
    return json.loads(http_get(url, timeout, validate=_is_json))


def download(url: str, dest: Path, timeout: int = 120) -> Path | None:
    """Download a PDF. Returns the path, or None if nothing that is actually a PDF
    came back — a login or error page wearing a .pdf name must not reach the library."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = http_get(url, timeout, accept="application/pdf",
                        validate=lambda b: len(b) > 1024 and b.startswith(b"%PDF"))
    except RuntimeError:
        return None
    dest.write_bytes(data)
    return dest


def fetch_document(url: str, dest: Path, code: str) -> tuple:
    """Fetch and classify -> (path, reason_it_failed). A 'download' does not always
    yield a PDF, and each non-PDF outcome needs a different answer, not a retry."""
    try:
        data = http_get(url, 120, accept="application/pdf", validate=lambda b: len(b) > 512)
    except RuntimeError:
        return (None, "download failed (gated, moved, or dead link)")
    kind = classify_payload(data)
    if kind == "pdf":
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return (dest, None)
    if kind == "zip":
        got = pdf_from_zip(data, code, dest)
        return (got, None) if got else (None, "archive held no PDF matching the code")
    if kind == "html":
        return (None, "resolves to an online HTML doc or a login page — nothing to import")
    return (None, f"unrecognised payload ({len(data)} bytes)")


# ---------------------------------------------------------------- revisions
#
# A revision field is not a version number. ST's is clean ("12.0", "7.0"), but NXP's
# RevisionNo is free text and roughly a third of errata put a *date* in it. Formats
# observed across a ~1,500-document import (2026-08):
#
#   1.1  2.0  11  0  ""                 numeric / absent
#   30 January 2024   06 Jul 2020       spaced date
#   10DEC2013  16OCT2015  20JULY2016    squashed date, 3-4 letter month
#   Rev 29 SEP 2013                     "Rev" prefix inside the value
#   11 Sep 019                          typo'd year, and it is a real record
#
# So parsing returns a *kind* alongside the value, and comparison refuses to order
# across kinds. A date is not larger or smaller than "2.0" — it's unknowable — and
# guessing there would delete a good local PDF on the strength of a parse artifact.
# Under-replacing is recoverable (it shows up in the report); over-replacing is not.
#
# No alphabetic revisions appear for either vendor: NXP puts silicon letters in the
# document *code* (IMXRT1060CE_A / _B), not the revision.

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_DATE_RE = re.compile(
    r"(?P<d>\d{1,2})\s*(?P<m>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*(?P<y>\d{2,4})",
    re.I)


_SEP_DATE = re.compile(r"^(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})$")


def _separated_date(s):
    r"""Dotted/slashed/ISO dates -> date | 'ambiguous' | None (not a date at all).

    This runs BEFORE the numeric branch because "30.01.2024" matches \d+(\.\d+)*
    perfectly and parses as version (30, 1, 2024). That is not a stall, it is a
    confident wrong answer: (31,12,2023) vs (1,1,2024) compares as *older*, so the
    skill silently refuses every real update across a month or year boundary.

    DD/MM vs MM/DD is only resolved when one field exceeds 12. A genuinely ambiguous
    01.02.2024 stays unknown — guessing gets it right half the time, and being wrong
    here means replacing a newer local PDF with an older one.
    """
    m = _SEP_DATE.match(s)
    if not m:
        return None
    a, b, c = (int(x) for x in m.groups())
    if len(m.group(1)) == 4:                       # ISO 2024-01-30, unambiguous
        try:
            return date(a, b, c)
        except ValueError:
            return "ambiguous"
    if len(m.group(3)) != 4:
        return None                                # 1.2.99 is a version, not a date
    if a == b:                                     # 01.01.2024 reads the same either way
        try:
            return date(c, a, b)
        except ValueError:
            return "ambiguous"
    if a > 12 and b <= 12:                         # 30.01.2024 -> DD MM YYYY
        try:
            return date(c, b, a)
        except ValueError:
            return "ambiguous"
    if b > 12 and a <= 12:                         # 01/30/2024 -> MM DD YYYY
        try:
            return date(c, a, b)
        except ValueError:
            return "ambiguous"
    return "ambiguous"


def parse_rev(raw):
    """-> ('num', ints) | ('date', date) | ('alpha', ords) | ('unknown', raw)"""
    s = str(raw or "").strip()
    if not s:
        return ("unknown", s)
    s = re.sub(r"^rev(ision)?\.?\s*:?\s*", "", s, flags=re.I).strip()
    s = re.sub(r"^v(?=\d)", "", s, flags=re.I)   # Espressif reports "v1.8"
    if not s:
        return ("unknown", raw)

    sep = _separated_date(s)
    if sep == "ambiguous":
        return ("unknown", raw)
    if sep is not None:
        return ("date", sep)

    # Letter revisions are the house style at TI, Microchip and Renesas — "no
    # alphabetic revisions" held only for ST and NXP and generalised badly.
    if re.fullmatch(r"[A-Za-z]{1,2}", s):
        # Length leads, because letter revisions carry like odometer digits: AA is the
        # one *after* Z, not a rewind to A. A bare ordinal tuple gets that backwards,
        # ordering (26,) above (1,1).
        return ("alpha", (len(s),) + tuple(ord(c) - 64 for c in s.upper()))

    m = re.fullmatch(r"(\d+(?:\.\d+)*)\s*([A-Za-z])?", s)
    if m:
        parts = tuple(int(p) for p in m.group(1).split("."))
        if m.group(2):                             # 2.1A sorts after 2.1
            parts += (ord(m.group(2).upper()) - 64,)
        return ("num", parts)

    m = _DATE_RE.search(s)
    if m:
        y = int(m.group("y"))
        if y < 1000:                      # "019" -> 2019, "13" -> 2013
            y += 2000
        try:
            return ("date", date(y, MONTHS[m.group("m")[:3].lower()], int(m.group("d"))))
        except ValueError:
            return ("unknown", raw)
    return ("unknown", raw)


def compare_rev(new, old) -> tuple:
    """-> (verdict, detail) where verdict is
    'newer' | 'current' | 'incomparable' | 'unparseable'.

    This returns a verdict rather than a bool on purpose. A boolean cannot express
    the difference between "already up to date" and "can never be checked", and that
    difference is the one this skill keeps getting wrong: every silent failure we've
    found came from a correct refusal that was indistinguishable from a success.
    No amount of reporting discipline at the call site recovers information the
    signature cannot carry — so the channel lives here, and `rev_newer` below is a
    convenience for callers that genuinely only need the boolean.
    """
    kn, vn = parse_rev(new)
    ko, vo = parse_rev(old)
    if kn == "unknown":
        return ("unparseable", f"vendor revision {new!r} cannot be parsed")
    if ko == "unknown":
        # A local revision we can't read is exactly the one worth refreshing.
        return ("newer", f"local revision {old!r} unreadable")
    if kn != ko:
        return ("incomparable", f"local {old!r} ({ko}) vs vendor {new!r} ({kn})")
    if kn in ("num", "alpha"):
        n = max(len(vo), len(vn))         # "7" == "7.0", "18" < "22"
        newer = tuple(vn) + (0,) * (n - len(vn)) > tuple(vo) + (0,) * (n - len(vo))
    else:
        newer = vn > vo
    return ("newer", f"{old} -> {new}") if newer else ("current", f"at {old}")


def rev_newer(new, old) -> bool:
    """True only when `new` is *provably* later than `old`. Ambiguity means no.

    Prefer compare_rev() anywhere the outcome is reported to a human — this collapses
    'current', 'incomparable' and 'unparseable' into one indistinguishable False.
    """
    return compare_rev(new, old)[0] == "newer"


# Doc IDs as they appear in titles and on PDF cover pages. The trailing `Rev` is
# load-bearing: ST errata sheets cite their reference manual on the cover, so a bare
# ID match tags an errata sheet as RMxxxx. Requiring the adjacent revision marker
# keeps you on the document's *own* ID.
#
# Do NOT "helpfully" write 0?(\d{3,5}) to absorb a leading zero — the optional zero
# eats the first digit, RM0433 becomes RM433, and matching silently stops working.
DOCID_IN_TITLE = re.compile(r"\((DS|RM|ES|PM|UM|AN|TN)\s?(\d{3,5})\)", re.I)
DOCID_IN_PDF = re.compile(r"\b(DS|RM|ES|PM|UM|AN|TN)\s?(\d{3,5})\b\s*[-–]?\s*Rev", re.I)


def doc_id_from_pdf(path: Path) -> str | None:
    """Last-resort ID recovery: read the cover page. Only used for books whose title
    doesn't carry the ID (older hand-added entries)."""
    if not shutil.which("pdftotext"):
        return None
    p = subprocess.run(["pdftotext", "-f", "1", "-l", "2", str(path), "-"],
                       capture_output=True, text=True)
    m = DOCID_IN_PDF.search(p.stdout or "")
    return f"{m.group(1).upper()}{m.group(2)}" if m else None


# ---------------------------------------------------------------- documents

# The default document scope. "Technical" here means the documents you consult to
# actually program a part — not product briefs, presentations, packaging or
# certification paperwork, which are the bulk of a vendor catalogue and noise in a
# reference library. Naming --types explicitly overrides this entirely.
TECHNICAL_TYPES = {"datasheet", "errata", "reference-manual", "user-manual",
                   "user-guide", "programming-manual", "application-note"}


# titles.py keys its type-word table on the vendors' own wording, so map back from
# our normalized kinds.
VENDOR_TYPE = {"errata": "Errata Sheet", "datasheet": "Datasheet",
               "reference-manual": "Reference Manual", "user-manual": "User Manual",
               "programming-manual": "Programming Manual",
               "application-note": "Application Note"}


@dataclass
class Doc:
    """One vendor document. Adapters return these; the core consumes them."""
    vendor: str          # identifier prefix, e.g. "st" / "nxp"
    doc_id: str          # vendor's own code, e.g. "DS12930", "MCXA344VLL_0P25N"
    doc_type: str        # normalized kind: datasheet, errata, reference-manual, ...
    version: str | None  # as the vendor reports it; compared via parse_rev
    title: str           # human description, without ID/revision
    url: str
    author: str          # vendor name as it appears in Calibre's author field
    family: list = field(default_factory=list)   # extra tags: STM32H7, i.MX RT, ...
    desc: str = ""
    # Titles this document may already be filed under from before identifiers existed.
    # Hand-added books predate this skill and carry no `vendor:id`, so an identifier-only
    # diff calls them missing and cheerfully imports a second copy. Adapters supply the
    # shapes their vendor's files were historically named with.
    aliases: list = field(default_factory=list)
    # Whether the downloaded PDF can be checked against this ID. False where the ID
    # is a filename stem rather than something printed in the document (Espressif).
    verify_id: bool = True

    @property
    def ident(self) -> str:
        return f"{self.vendor}:{self.doc_id}"

    def calibre_title(self) -> str:
        """`<description> (<DOCID>) Rev <n>` — the convention the ~1,800 imported
        books already follow. The heuristics live in titles.py because vendor index
        titles are junk far more often than you would expect (raw URLs, "untitled",
        "Microsoft Word - FRDM-K32L3A6_Errata.doc")."""
        import titles
        return titles.title({"code": self.doc_id, "title": self.title,
                             "summary": self.desc, "version": self.version,
                             "type": VENDOR_TYPE.get(self.doc_type, ""),
                             "link": self.url})

    def tags(self) -> list:
        return [self.doc_type, self.vendor] + list(self.family)

    def comments(self) -> str:
        return (f"{self.desc}\n\nDocument ID: {self.doc_id}\n"
                f"Revision: {self.version or 'unknown'}\nSource: {self.url}")


# ---------------------------------------------------------------- calibre

def rev_from_comments(text) -> str | None:
    """The `Revision:` line Doc.comments() writes. titles.py carries only numeric
    revisions in the title, so a letter (Microchip 'E') or a date (a sitemap
    lastmod) survives only here — unread, those books could never be update-checked."""
    m = re.search(r"Revision:\s*([^<\n]+)", text or "")
    v = m.group(1).strip() if m else None
    return v if v and v.lower() != "unknown" else None


class Library:
    """calibredb wrapper.

    The GUI takes an exclusive write lock, so imports need it closed. Its content
    server on :8080 accepts reads but rejects writes ("Forbidden") because no server
    users are configured — so it is not a way around the lock, just a read path.
    """

    def __init__(self, path: Path = LIBRARY):
        self.path = path
        self.server = self._server_creds()

    @staticmethod
    def _server_creds():
        """Optional write-through-the-GUI credentials, read from the gitignored
        `secret.yml` beside SKILL.md (or `$DOWNLOAD_DOC_SECRET`) so they are never
        typed into a command by hand:

            calibre:
              server_url: http://localhost:8080
              username: <name>
              password: <secret>

        Calibre's content server accepts reads unauthenticated but answers writes with
        "Forbidden" until a user exists (Preferences -> Sharing over the net -> Require
        username and password -> Add user, with write access). With one defined,
        imports work while the GUI is open instead of waiting for the lock.
        """
        env = os.environ.get("DOWNLOAD_DOC_SECRET")
        if env:
            f = Path(env).expanduser()
            if not f.exists():
                raise FileNotFoundError(f"DOWNLOAD_DOC_SECRET points at a missing file: {f}")
        else:
            f = Path(__file__).resolve().parent.parent / "secret.yml"
            if not f.exists():
                return None
        blk = re.search(r"^calibre:\s*$(.*?)(?=^\S|\Z)", f.read_text(), re.M | re.S)
        if not blk:
            return None
        g = dict(re.findall(r"^\s+(\w+):\s*(\S+)\s*$", blk.group(1), re.M))
        if not (g.get("username") and g.get("password")):
            return None
        return (g.get("server_url", "http://localhost:8080"), g["username"], g["password"])

    # calibredb reports "Another calibre program ... is running" even when Calibre is
    # closed, if a FreeFileSync mirror is touching the library directory at that moment.
    # It is transient and clears on retry, so a single sample must not sink a whole run.
    _LOCK_MSG = "another calibre program"

    def _run(self, *args, check=True, tries=4) -> str:
        for attempt in range(tries):
            p = subprocess.run(["calibredb", "--with-library", str(self.path), *args],
                               capture_output=True, text=True)
            if p.returncode == 0:
                return p.stdout
            err = (p.stderr or p.stdout or "").strip()
            if self._LOCK_MSG in err.lower():
                # Two different causes, and only one of them clears by waiting. Retrying
                # a GUI lock just burns 30 seconds and then reports the wrong problem.
                if self._running("/usr/bin/calibre") or self._running("python3.13", "/usr/bin/calibre"):
                    if self.server:
                        # Write through the running GUI's own content server rather than
                        # wait for a lock that cannot clear while the GUI is open.
                        url, user, pw = self.server
                        # Never put the password in argv: /proc/<pid>/cmdline is world
                        # readable, so `ps` would show it for the life of every call —
                        # which would defeat keeping it in a gitignored file at all.
                        # calibredb takes `<f:PATH>` and rstrip()s the contents, so hand
                        # it a 0600 file on tmpfs (never touches disk) and delete it
                        # immediately. Its other option, `<stdin>`, routes through
                        # getpass() and needs a TTY we do not have here.
                        rt = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
                        fd, tmp_pw = tempfile.mkstemp(dir=str(rt), prefix=".calpw-")
                        try:
                            os.fchmod(fd, 0o600)
                            with os.fdopen(fd, "w") as fh:
                                fh.write(pw)
                            p2 = subprocess.run(
                                ["calibredb", "--with-library", url, "--username", user,
                                 "--password", f"<f:{tmp_pw}>", *args],
                                capture_output=True, text=True)
                        finally:
                            try:
                                os.unlink(tmp_pw)
                            except OSError:
                                pass
                        if p2.returncode == 0:
                            return p2.stdout
                        raise RuntimeError("content-server write failed: "
                                           + (p2.stderr or p2.stdout or "").strip()[:160])
                    raise RuntimeError(
                        "the Calibre GUI is open and holds the library's write lock. "
                        "Close it, or define a content-server user and put the credentials "
                        "in secret.yml under `calibre:` (see Library._server_creds).")
                if attempt < tries - 1:
                    print(f"  calibredb reported a lock (attempt {attempt + 1}/{tries}); "
                          f"retrying — a sync is probably touching the library",
                          file=sys.stderr)
                    time.sleep(2 + 2 * attempt)
                    continue
            if check:
                raise RuntimeError(err)
            return p.stdout
        return ""

    @staticmethod
    def _running(argv0_suffix: str, needle: str = "") -> bool:
        """Is a process running whose *executable* is argv0_suffix?

        Deliberately not `pgrep -f`: that matches any command line containing the
        string, including the shell that invoked us. A shell running
        `grep ... calibre-library.ffs_batch` makes pgrep report the mirror as running,
        and a `pkill -f` with the same pattern kills the caller — which happened three
        times while building this. Matching argv[0] cannot hit a wrapper, because a
        shell's argv[0] is the shell.
        """
        me = os.getpid()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if not argv or not argv[0]:
                continue
            exe = argv[0].decode("utf-8", "replace")
            rest = b" ".join(argv[1:]).decode("utf-8", "replace")
            if exe.endswith(argv0_suffix) and (not needle or needle in rest or needle in exe):
                return True
        return False

    def blockers(self) -> list:
        """Reasons an import would be unsafe right now. Report these and let the
        user clear them — killing someone's GUI or sync job is not ours to do."""
        out = []
        gui = self._running("/usr/bin/calibre") or self._running("python3.13", "/usr/bin/calibre")
        if gui and not self.server:
            out.append("The Calibre GUI is open and holds the library's write lock — "
                       "close it before importing.")
        # With content-server credentials configured, an open GUI is not a blocker:
        # _run writes through its server instead of competing for the lock. Reporting
        # it anyway made sync.py refuse imports that would have succeeded.
        if self._running("FreeFileSync_x86_64", "calibre-library"):
            out.append("A FreeFileSync calibre-library mirror is running — importing "
                       "now races the sync to omv, and calibredb may report a "
                       "spurious lock error. Let it finish first.")
        return out

    def index(self) -> dict:
        """{'st:DS12930': {'id': 42, 'title': ..., 'rev': '3.1'}} for every book that
        carries a vendor identifier. One bulk read beats per-doc `calibredb search`."""
        books = json.loads(self._run("list", "--for-machine", "-f",
                                     "id,title,identifiers,comments") or "[]")
        idx = {}
        for b in books:
            ids = b.get("identifiers") or {}
            if not isinstance(ids, dict):
                continue
            for scheme, code in ids.items():
                if scheme in ("isbn", "mobi-asin", "uri", "doi"):
                    continue
                m = re.search(r"Rev\.?\s*([0-9A-Za-z.]+)\s*$", b.get("title", ""))
                rev = m.group(1) if m else rev_from_comments(b.get("comments"))
                idx[f"{scheme}:{code}"] = {"id": b["id"], "title": b.get("title", ""),
                                           "rev": rev}
        return idx

    def legacy_index(self, author: str) -> dict:
        """{normalized title: {id, title}} for this vendor's books that carry no
        identifier — i.e. everything filed by hand before the skill existed."""
        books = json.loads(self._run("list", "--for-machine", "-f", "id,title,identifiers",
                                     "--search", f'authors:"{author}"', check=False) or "[]")
        return {norm_title(b["title"]): {"id": b["id"], "title": b["title"]}
                for b in books if not (b.get("identifiers") or {})}

    def add(self, doc: Doc, pdf: Path) -> int | None:
        out = self._run("add", "-t", doc.calibre_title(), "-a", doc.author,
                        "-T", ",".join(doc.tags()), "-I", doc.ident, "-l", "eng", str(pdf))
        m = re.search(r"Added book ids?:\s*(\d+)", out)
        book_id = int(m.group(1)) if m else None
        if book_id is None:
            # calibredb refuses a book whose title+author already exist and echoes the
            # rejected file path instead of an id. That is "already present", not a
            # failure, and reporting it as one hid the real cause for 700 documents.
            self.last_add_status = ("duplicate" if str(pdf) in out or pdf.name in out
                                    else "failed")
        else:
            self.last_add_status = "added"
        if book_id:   # `add` has no comments flag, so set it in a second call
            self._run("set_metadata", "--field", f"comments:{doc.comments()}",
                      "--field", f"publisher:{doc.author}", str(book_id), check=False)
        return book_id

    def remove(self, book_id: int, backup_dir: Path) -> None:
        """Back the PDF up before removing. Calibre's remove is immediate and there is
        no undo; a superseded revision is still the only copy of that revision."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        for line in self._run("list", "--for-machine", "-f", "formats",
                              "-s", f"id:{book_id}", check=False).splitlines():
            for path in re.findall(r'"(/[^"]+\.\w+)"', line):
                src = Path(path)
                if src.exists():
                    shutil.copy2(src, backup_dir / src.name)
        self._run("remove", str(book_id))


# ---------------------------------------------------------------- planning

def page1_text(path: Path, pages: int = 2) -> str:
    if not shutil.which("pdftotext"):
        return ""
    p = subprocess.run(["pdftotext", "-f", "1", "-l", str(pages), str(path), "-"],
                       capture_output=True, text=True)
    return re.sub(r"\s+", " ", p.stdout or "")


def verify_identity(pdf: Path, doc: "Doc") -> tuple:
    """Confirm the file we got is the document we asked for -> (verdict, found).

    Vendors do serve the wrong file: NXP returned SAC57D5x_1N87P for a request for
    SAC57D54H_1N87P. Nothing else in the pipeline would notice, and the wrong PDF
    would be filed under the right identifier — worse than a missing book, because
    it looks correct forever after.

    The check MUST use the Rev-adjacent pattern rather than a bare ID match. On a
    real ST errata sheet (ES0392) the first ID-shaped token on page 1 is `RM0433`,
    the reference manual it cross-references — a bare match rejects 124 valid errata
    sheets as "wrong document".
    """
    if not doc.verify_id:
        return ("skipped", None)
    text = page1_text(pdf)
    if not text:
        return ("inconclusive", None)
    if re.fullmatch(r"(DS|RM|ES|PM|UM|AN|TN)\s?\d{3,5}", doc.doc_id, re.I):
        m = DOCID_IN_PDF.search(text)
        if not m:
            return ("inconclusive", None)
        found = f"{m.group(1).upper()}{m.group(2)}"
        return ("ok", found) if found == doc.doc_id.upper() else ("mismatch", found)
    # Vendor codes with no shared grammar (NXP mask codes, module part numbers):
    # all we can do is look for the code itself, and absence is a warning, not proof.
    return ("ok", doc.doc_id) if doc.doc_id.lower() in text.lower() else ("absent", None)


def classify_payload(data: bytes) -> str:
    """PDF is not the only thing a 'download' returns. NXP DRM* design reference
    manuals arrive as a zip bundling the PDF with HW/SW packages, and some links
    resolve to an HTML documentation site with no file to import at all."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    head = data[:2048].lower()
    return "html" if b"<html" in head or b"<!doctype html" in head else "unknown"


def pdf_from_zip(zip_bytes: bytes, code: str, dest: Path) -> Path | None:
    """Pull `<CODE>.pdf` out of a bundle. Case-insensitive: NXP's archives use the
    vendor's own casing, not the code's."""
    import io
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None
    names = [n for n in z.namelist() if n.lower().endswith(".pdf")]
    pick = next((n for n in names if Path(n).stem.lower() == code.lower()), None) or \
        (names[0] if len(names) == 1 else None)
    if pick is None:
        return None
    dest.write_bytes(z.read(pick))
    return dest


def cap_families(fams: list, limit: int = 6, wide: str = "wide") -> list:
    """A cross-cutting application note legitimately belongs to every series — AN1709
    covers all 24 STM32 families — and 25 tags on one book makes the tag browser
    useless. Collapse past a threshold instead."""
    return fams if len(fams) <= limit else [wide]


def reconcile(docs: list, buckets: dict, extra: dict | None = None) -> list:
    """Per-type arithmetic: catalogue == imported + current + legacy + failed.

    This is the check that catches a silent drop, which is the failure mode that has
    actually bitten this skill twice. A mismatch means documents evaporated between
    enumeration and import — and unlike an exception, that otherwise reports as a
    clean, confident success.
    """
    lines, per_type = [], {}
    for d in docs:
        per_type.setdefault(d.doc_type, {"catalogue": 0})["catalogue"] += 1
    for name, items in buckets.items():
        for it in items:
            d = it[0] if isinstance(it, tuple) else it
            per_type.setdefault(d.doc_type, {"catalogue": 0}).setdefault(name, 0)
            per_type[d.doc_type][name] += 1
    for kind, c in sorted(per_type.items()):
        total = c["catalogue"]
        parts = {k: v for k, v in c.items() if k != "catalogue"}
        accounted = sum(parts.values()) + (extra or {}).get(kind, 0)
        flag = "" if accounted == total else f"  <-- MISMATCH, {total - accounted} unaccounted"
        detail = " + ".join(f"{v} {k}" for k, v in sorted(parts.items())) or "0"
        lines.append(f"  {kind:<20} catalogue {total:>4} = {detail}{flag}")
    return lines


def unparseable_revisions(docs: list) -> dict:
    """{doc_type: [(doc_id, raw_revision), ...]} for documents whose revision cannot
    be parsed.

    These are the quiet ones. A document that enumerates, matches and imports fine
    but whose revision is uncomparable passes every other check while being
    permanently un-updatable — Espressif's "v1.8" reported "0 outdated" forever, and
    that looked exactly like "everything is current". Reporting it makes the next
    unhandled format announce itself the first time it appears, instead of waiting
    for someone to notice a vendor has never once published an update.
    """
    out: dict = {}
    for d in docs:
        if parse_rev(d.version)[0] == "unknown":
            out.setdefault(d.doc_type, []).append((d.doc_id, d.version or "<none>"))
    return out


# --------------------------------------------------------------- USB capability
#
# Deciding "does this part have a USB controller" has one reliable source and several
# unreliable ones. Vendor index blurbs are trustworthy when they *mention* USB and
# worthless when they don't: mining ST's descriptions finds USB in 10 of 24 series and
# misses STM32G4/L5/U5/WB, which all have it. So a positive hint is accepted, a
# negative hint proves nothing, and anything unresolved is settled by reading the
# document itself — once per family, cached, with the evidence kept so a verdict can
# be audited instead of trusted.

USB_EVIDENCE = re.compile(
    r"(?i)universal serial bus|USB OTG|USB[- ]?2\.0|USB full[- ]speed|USB high[- ]speed|"
    r"\bOTG_[FH]S\b|USB device controller|USB host controller")
# "USB-Serial/JTAG" and "USB power delivery" name peripherals you cannot write a USB
# stack against, so they must not count on their own.
USB_DECOYS = re.compile(r"(?i)USB[- ]serial/?JTAG|USB power delivery|USB[- ]PD\b|USB Type-C")


class UsbIndex:
    """Cached per-family USB verdicts: {'st:STM32G4': {usb, source, evidence}}."""

    last_evidence = ""

    def __init__(self, path: Path | None = None):
        self.path = path or (Path.home() / ".cache" / "download-doc" / "usb-capability.json")
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, vendor: str, family: str):
        return self.data.get(f"{vendor}:{family}")

    def set(self, vendor: str, family: str, usb, source: str, evidence: str = ""):
        self.data[f"{vendor}:{family}"] = {"usb": usb, "source": source,
                                           "evidence": evidence[:120]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))

    def hint_from_index(self, docs: list) -> None:
        """Accept positive mentions in the vendor's own descriptions. Never record a
        negative from silence — that is the false-negative trap."""
        for d in docs:
            for fam in d.family or []:
                if self.get(d.vendor, fam):
                    continue
                blob = f"{d.title} {d.desc}"
                if USB_EVIDENCE.search(USB_DECOYS.sub("", blob)):
                    self.set(d.vendor, fam, True, "vendor index", blob[:120])

    @staticmethod
    def spread(cands: list, n: int) -> list:
        """Evenly spaced sample, not the first n.

        Candidate lists are sorted by document code, and code order is not random:
        low numbers are the cut-down parts. Taking the first six of Kinetis L sampled
        KL02/KL03/KL16/KL17 — none of which have USB — and never reached KL25 or KL82,
        which do. Any ordered sample of an ordered catalogue inherits its bias.
        """
        if len(cands) <= n:
            return list(cands)
        step = len(cands) / n
        return [cands[int(i * step)] for i in range(n)]

    def resolve(self, vendor: str, family: str, cands: list, tmp: Path,
                budget: int = 6) -> bool | None:
        """Settle one family. Stops at the first positive; a negative is only recorded
        after the budget is spent *and* a reference manual has been consulted, because
        a datasheet proves USB present, never absent."""
        seen_rm, read = False, 0
        for cand in self.spread(cands, budget):
            v = self.peek(cand, tmp, record=False)
            # Count documents actually READ. A gated or dead link teaches nothing, and
            # counting attempts let families be excluded on one readable datasheet while
            # the rest were behind a login — absence of evidence wearing the costume of
            # evidence of absence.
            read += v is not None
            seen_rm = seen_rm or cand.doc_type == "reference-manual"
            if v:
                self.set(vendor, family, True, f"pdf:{cand.doc_id}", self.last_evidence)
                return True
        if not seen_rm:
            rm = next((c for c in cands if c.doc_type == "reference-manual"), None)
            if rm is not None and self.peek(rm, tmp, record=False):
                self.set(vendor, family, True, f"pdf:{rm.doc_id}", self.last_evidence)
                return True
            seen_rm = rm is not None
        if not cands:
            return None
        # Refuse to record a negative on thin evidence. A "no" drops every document for
        # the family, so it needs either a reference manual (which covers the whole
        # series) or a decent sample. Otherwise stay unresolved and keep the documents:
        # fetching a few extra costs bandwidth, dropping real ones costs them silently.
        # A family has USB if ANY member does, so a negative is only safe once every
        # candidate has been read. Where that is too expensive, stay unresolved and keep
        # the documents rather than dropping a whole family on a partial sample.
        if len(cands) > budget:
            self.set(vendor, family, None,
                     f"pdf:sampled {min(budget, len(cands))}/{len(cands)}",
                     "no USB in the sample, but not exhaustive — kept")
            return None
        if read < 3 and not seen_rm:
            self.set(vendor, family, None, f"pdf:{read} readable doc(s)",
                     "not enough readable documents to exclude — kept")
            return None
        self.set(vendor, family, False, f"pdf:{len(cands[:budget])} docs"
                 + (" incl. reference manual" if seen_rm else ""),
                 "no USB peripheral found")
        return False

    def peek(self, doc: "Doc", tmp: Path, record: bool = True) -> bool | None:
        """Settle a family by reading one of its documents. Returns the verdict."""
        pdf = tmp / f"peek-{doc.doc_id}.pdf"
        got = pdf if pdf.exists() and pdf.stat().st_size > 1024 else download(doc.url, pdf)
        if got is None:
            return None
        text = USB_DECOYS.sub("", page1_text(got, pages=60))
        m = USB_EVIDENCE.search(text)
        self.last_evidence = m.group(0) if m else "no USB peripheral found"
        if record:
            for fam in doc.family or []:
                self.set(doc.vendor, fam, bool(m), f"pdf:{doc.doc_id}", self.last_evidence)
        return bool(m)


def peek_order(docs: list) -> list:
    """Cheapest informative document first: a datasheet lists peripherals on page 1 and
    is a few MB; a reference manual says the same thing and is often 15-20 MB.

    A *family* has USB if ANY part in it does, so one datasheet can only ever prove
    yes — never no. Sampling a single one judged STM32F0/F3/G0/WB as USB-less, when
    F072, F303, G0B1 and WB55 all have USB full-speed; the sampled part simply didn't.
    So negatives must be escalated: try several datasheets, then the reference manual,
    which covers the whole series and is the only document that can settle a no.
    """
    rank = {"datasheet": 0, "data-brief": 1, "reference-manual": 2, "user-manual": 3}
    return sorted(docs, key=lambda d: (rank.get(d.doc_type, 9), d.doc_id))


def norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def plan(docs: list, idx: dict, legacy: dict | None = None) -> dict:
    """Split enumerated docs into new / outdated / current / legacy.

    `legacy` maps normalized titles to books that carry no vendor identifier. A doc
    matching one of those is *probably* already in the library under a hand-made
    title. It is reported rather than acted on: it can't be revision-compared (the
    old title records no revision), so the only automatic move would be to replace a
    book the user added themselves, on a guess. That is theirs to approve.
    """
    out = {"new": [], "outdated": [], "current": [], "legacy": [], "incomparable": []}
    for d in docs:
        have = idx.get(d.ident)
        if have is not None:
            verdict, detail = compare_rev(d.version, have["rev"])
            if verdict == "newer":
                out["outdated"].append((d, have))
            elif verdict == "current":
                out["current"].append(d)
            else:
                # incomparable (vendor re-schemed) or unparseable — either way this
                # document can never be checked for updates, which without a channel
                # of its own reads identically to "already current".
                out["incomparable"].append((d, have, verdict, detail))
            continue
        hit = None
        for alias in [d.doc_id, *d.aliases]:
            key = norm_title(alias)
            hit = (legacy or {}).get(key)
            if hit is None and len(key) > 12:
                # Hand-filed titles often append the vendor's cover blurb or a date —
                # "RP2040 Datasheet: A microcontroller by Raspberry Pi. Feb 2025".
                # Match on a whole-word prefix so "Pico Datasheet" still can't claim
                # "Pico W Datasheet".
                hit = next((v for k, v in (legacy or {}).items()
                            if k == key or k.startswith(key + " ")), None)
            if hit:
                break
        (out["legacy"].append((d, hit)) if hit else out["new"].append(d))
    return out


def sweep_download_dir(dl_dir: Path, code: str) -> Path | None:
    """Find an already-downloaded file for `code`.

    Filenames don't reliably match the code: casing differs, some docs arrive as
    .zip bundles, and Chrome appends ' (1)', ' (2)' on repeats. Match
    case-insensitively and extension-agnostically or you re-download the same file
    on every retry.
    """
    want = code.lower()
    for p in sorted(dl_dir.iterdir() if dl_dir.is_dir() else []):
        stem = re.sub(r"\s*\(\d+\)$", "", p.stem).lower()
        if stem == want:
            return p
    return None


if __name__ == "__main__":   # run `python3 doclib.py` to sanity check
    # revision kinds, against values actually seen in the two vendors' feeds
    assert parse_rev("2.0")[0] == "num" and parse_rev("11")[0] == "num"
    assert parse_rev("10DEC2013")[0] == "date" and parse_rev("30 January 2024")[0] == "date"
    assert parse_rev("Rev 29 SEP 2013")[0] == "date"
    assert parse_rev("11 Sep 019") == ("date", date(2019, 9, 11))   # typo'd year, real record
    assert parse_rev("")[0] == "unknown"
    assert parse_rev("v1.8") == parse_rev("1.8") == ("num", (1, 8))   # Espressif
    assert rev_newer("v1.8", "v1.3") and not rev_newer("v1.3", "v1.8")
    assert parse_rev("latest")[0] == "unknown"
    assert parse_rev("Rev A") == ("alpha", (1, 1)) and parse_rev("Rev. B") == ("alpha", (1, 2))
    assert rev_newer("AA", "Z") and not rev_newer("Z", "AA")     # AA follows Z
    assert rev_newer("AB", "AA")
    # 2.1 -> 2.1A -> 2.2 is one scheme, so the letter folds into the numeric tuple
    # rather than becoming a separate kind that would refuse to compare.
    assert rev_newer("2.1A", "2.1") and rev_newer("2.2", "2.1A")
    assert rev_newer("Rev B", "Rev A") and not rev_newer("Rev A", "Rev B")
    assert parse_rev("2.1A") == ("num", (2, 1, 1))       # sorts after 2.1
    assert rev_newer("2.1A", "2.1") and not rev_newer("2.1", "2.1A")
    assert parse_rev("2024-01-30")[1] == date(2024, 1, 30)      # ISO
    assert parse_rev("01/30/2024")[1] == date(2024, 1, 30)      # MM/DD, day > 12
    assert parse_rev("30.01.2024")[1] == date(2024, 1, 30)      # DD.MM, day > 12
    assert parse_rev("01.01.2024")[1] == date(2024, 1, 1)       # same either way
    assert parse_rev("01.02.2024")[0] == "unknown"              # genuinely ambiguous
    assert parse_rev("1.2.99") == ("num", (1, 2, 99))           # a version, not a date
    # the dotted-date bug: these parsed as version tuples and ordered backwards
    assert rev_newer("01.01.2024", "31.12.2023"), "must cross the year boundary"
    assert not rev_newer("31.12.2023", "01.01.2024")
    assert not rev_newer("Rev A", "1.0")     # alpha vs num stays incomparable
    # the verdict channel: a boolean cannot tell these three apart
    assert compare_rev("1.2", "1.1")[0] == "newer"
    assert compare_rev("1.1", "1.1")[0] == "current"
    assert compare_rev("30 January 2024", "1.1")[0] == "incomparable"
    assert compare_rev("B", "2")[0] == "incomparable"
    assert compare_rev("latest", "1.1")[0] == "unparseable"
    assert compare_rev("1.1", "latest")[0] == "newer"      # local unreadable -> refresh
    assert all(rev_newer(*a) == (compare_rev(*a)[0] == "newer")
               for a in [("1.2", "1.1"), ("1.1", "1.1"), ("B", "2"), ("latest", "1.1")])
    # ordering
    assert rev_newer("22", "18") and not rev_newer("18", "22")
    assert not rev_newer("7.0", "7") and not rev_newer("7", "7.0")   # equal, no churn
    assert rev_newer("3.1", "3")
    assert rev_newer("2", "")                    # local unreadable -> refresh
    assert not rev_newer("", "2")                # nothing to upgrade to
    assert not rev_newer("06 Jul 2020", "1.1")   # date vs numeric is NOT comparable
    assert not rev_newer("1.1", "06 Jul 2020")
    assert rev_newer("16OCT2015", "10DEC2013") and not rev_newer("10DEC2013", "16OCT2015")
    # doc ids
    assert DOCID_IN_PDF.search("RM0433 Rev 8") and not DOCID_IN_PDF.search("see RM0433 for details")
    assert DOCID_IN_TITLE.search("Errata sheet (ES0392) Rev 5").group(2) == "0392"
    assert DOCID_IN_TITLE.search("(RM0433)").group(2) == "0433"      # not "433"
    # Real page-1 text from ST's ES0392 errata sheet: the reference manual it cites
    # appears BEFORE its own ID, so a bare ID match picks the wrong document.
    es0392 = ("STM32H742xI/G STM32H743xI/G ... device errata ... based on RM0433 "
              "... ES0392 - Rev 15 - page 1")
    assert re.search(r"\b(DS|RM|ES|PM|UM|AN|TN)\s?(\d{3,5})\b", es0392).group(0) == "RM0433"
    m = DOCID_IN_PDF.search(es0392)
    assert m and f"{m.group(1)}{m.group(2)}" == "ES0392", "Rev-adjacency must win over the cross-reference"
    assert classify_payload(b"%PDF-1.7 ...") == "pdf"
    assert classify_payload(b"PK\x03\x04zip") == "zip"
    assert classify_payload(b"<!DOCTYPE html><html>") == "html"
    assert cap_families(list("abc")) == list("abc")
    assert cap_families(list("abcdefghij"), 6, "stm32-wide") == ["stm32-wide"]
    # the stored revision comes back out of the comments when the title has none
    assert rev_from_comments("<div><p>x</p><p>Document ID: A<br>Revision: E<br>Source: u</p></div>") == "E"
    assert rev_from_comments("Document ID: A\nRevision: 2024-10-04\nSource: u") == "2024-10-04"
    assert rev_from_comments("Revision: 30 January 2024<br>Source: u") == "30 January 2024"
    assert rev_from_comments("Revision: unknown\nSource: u") is None and rev_from_comments(None) is None
    print("doclib self-test OK")
