#!/usr/bin/env python3
"""Answer board-wiring questions from EAGLE and KiCad schematics.

Usage: pcb.py find KEYWORD...            schematics in the source repos
       pcb.py find --in PATH [KEYWORD...]  schematics under PATH
       pcb.py nets SCH [PATTERN]         net names containing PATTERN
       pcb.py parts SCH [KEYWORD...]     parts matching every keyword
       pcb.py part SCH REF               one part and the net on each pin
       pcb.py net SCH NAME               every terminal on one net

SCH is `owner/repo:path` in a source repo or a filesystem path.
READ_PCB_SOURCES lists the source repos as owner/repo (default hathach/pcb).
READ_PCB_CLONES lists this machine's clones of the source repos; a source
without one is read from a shallow, blobless clone under
~/.cache/read-pcb, fetched before every command. READ_PCB_REMOTE_BASE sets
the URL a new cache clones from (default https://github.com/;
git@github.com: for SSH); an existing cache keeps its origin.

EAGLE XML schematics are parsed directly. KiCad connectivity comes from
KiCad itself (`kicad-cli sch export netlist`), never from wire geometry, so
power symbols are absent from KiCad nets. Both print the same shape: a pin is
`REF PIN` with its name, pads and net.

Exit 0 result, 1 nothing matched, 2 bad usage or a sheet that is not a root,
3 source unavailable, unsupported or malformed, or an incomplete find.
"""
import argparse
import datetime
import fcntl
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from functools import partial


class Refusal(Exception):
    """A result the caller must not build on; carries the exit code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass
class Pin:
    ref: str
    pin: str                    # EAGLE pin name; KiCad pin number
    gate: str | None = None     # EAGLE only; shown when the pin name repeats across gates
    name: str | None = None     # KiCad pinfunction; optional display text
    pads: tuple = ()
    no_connect: bool = False
    net: str | None = None

    def label(self, repeated):
        pin = f"{self.gate}.{self.pin}" if self.pin in repeated else self.pin
        return pin + (f" ({self.name})" if self.name and self.name != self.pin else "")


@dataclass
class Part:
    ref: str
    value: str | None
    device: str
    package: str | None
    attributes: dict = field(default_factory=dict)
    sheet: str | None = None
    dnp: list = field(default_factory=list)
    pins: list = field(default_factory=list)

    def dnp_notes(self):
        return [f"[DNP in variant {v}]" for v in self.dnp]

    def repeated(self):
        """EAGLE pin names that more than one gate carries, such as D on a dual FET."""
        return {n for n, c in Counter(p.pin for p in self.pins if p.gate is not None).items() if c > 1}


@dataclass
class Design:
    fmt: str
    parts: dict
    nets: dict                  # name -> [Pin]


HEAD = 4096                     # classify() looks no further


def classify(data):
    head = data[:HEAD].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<eagle"):
        return "eagle" if b"<eagle" in head else "xml"
    if head.startswith(b"EESchema"):
        return "kicad-legacy"
    if head.startswith(b"(kicad_sch"):
        return "kicad"
    return "eagle-binary"


# ---- EAGLE ------------------------------------------------------------------

def load_eagle(data, path):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise Refusal(3, f"{path}: malformed EAGLE XML: {e}")
    if root.find(".//moduleinst") is not None:
        raise Refusal(3, f"{path}: hierarchical modules are unsupported")
    sch = root.find("drawing/schematic")
    if sch is None:
        raise Refusal(3, f"{path}: no <schematic> element; is this a board file?")

    def req(el, attr, what):
        value = el.get(attr)
        if value is None:
            raise Refusal(3, f"{path}: malformed EAGLE schematic: {what} has no {attr} attribute")
        return value

    libs = {}
    for lib in sch.iterfind("libraries/library"):
        libs.setdefault(lib.get("name"), {})[lib.get("urn") or ""] = lib

    def library(part):
        by_urn = libs.get(part.get("library"), {})
        lib = by_urn.get(part.get("library_urn") or "")
        if lib is None and len(by_urn) == 1:
            lib = next(iter(by_urn.values()))
        return lib

    parts, index = {}, {}
    for p in sch.iterfind("parts/part"):
        ref = req(p, "name", "a part")
        if ref in parts:
            raise Refusal(3, f"{path}: two parts are named {ref}")
        lib = library(p)
        ds = lib.find(f"devicesets/deviceset[@name={quote(req(p, 'deviceset', ref))}]") if lib is not None else None
        dev = ds.find(f"devices/device[@name={quote(p.get('device') or '')}]") if ds is not None else None
        if dev is None:
            raise Refusal(3, f"{path}: part {ref}: library {p.get('library')} deviceset "
                             f"{p.get('deviceset')} device {p.get('device')!r} is not in the schematic")
        attributes = {}
        tech = dev.find(f"technologies/technology[@name={quote(p.get('technology') or '')}]")
        for a in (tech.iterfind("attribute") if tech is not None else ()):
            attributes[a.get("name")] = a.get("value") or ""
        for a in p.iterfind("attribute"):
            attributes[a.get("name")] = a.get("value") or ""
        pads = {(req(c, "gate", f"{ref} connect"), req(c, "pin", f"{ref} connect")):
                tuple(req(c, "pad", f"{ref} connect").split()) for c in dev.iterfind("connects/connect")}
        gates = ds.findall("gates/gate")
        part = Part(ref, p.get("value"), f"{p.get('library')}:{p.get('deviceset')}{p.get('device') or ''}",
                    dev.get("package"), attributes,
                    dnp=[v.get("name") for v in p.iterfind("variant") if v.get("populate") == "no"])
        for g in gates:
            symbol = lib.find(f"symbols/symbol[@name={quote(req(g, 'symbol', f'{ref} gate'))}]")
            if symbol is None:
                raise Refusal(3, f"{path}: part {ref}: symbol {g.get('symbol')} is not in the schematic")
            gate = req(g, "name", f"{ref} gate")
            for s in symbol.iterfind("pin"):
                name = req(s, "name", f"symbol {g.get('symbol')} pin")
                pin = Pin(ref, name, gate, pads=pads.get((gate, name), ()))
                part.pins.append(pin)
                index[(ref, gate, name)] = pin
        parts[ref] = part

    nets = {}
    for net in sch.iterfind("sheets/sheet/nets/net"):
        name = req(net, "name", "a net")
        members = nets.setdefault(name, [])
        for ref in net.iterfind("segment/pinref"):
            key = tuple(req(ref, a, f"a pinref on {name}") for a in ("part", "gate", "pin"))
            pin = index.get(key)
            if pin is None:
                raise Refusal(3, f"{path}: net {name} references {'.'.join(key)}, which no part defines")
            if pin.net not in (None, name):
                raise Refusal(3, f"{path}: {key[0]} {key[1]}.{key[2]} is on both {pin.net} and {name}")
            if pin.net is None:
                pin.net = name
                members.append(pin)
    return Design("eagle", parts, nets)


def quote(s):
    """An ElementTree predicate literal; EAGLE names may hold either quote."""
    return f'"{s}"' if '"' not in s else f"'{s}'"


# ---- KiCad ------------------------------------------------------------------

TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def sexpr(data):
    """Nested lists of strings from UTF-8 bytes; ValueError when not well formed."""
    stack, cur = [], []
    for m in TOKEN.finditer(data.decode("utf-8")):
        if m.group(1):
            stack.append(cur)
            cur = []
        elif m.group(2):
            if not stack:
                raise ValueError("unbalanced ')'")
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(m.group(3) if m.group(3) is not None else m.group(4))
    if stack or len(cur) != 1:
        raise ValueError("unbalanced '('")
    return cur[0]


def children(node, head):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == head]


def value(node, i=1):
    """The string at node[i]; ValueError when the node lacks it."""
    if len(node) <= i or not isinstance(node[i], str):
        raise ValueError(f"({node[0]} ...) has no value")
    return node[i]


def sheet_role(tree):
    """("root", None), or ("sub", or None when the file cannot tell, and why it is not a root).

    KiCad writes sheet_instances only into a root sheet, and every symbol's
    instance path starts with the root's uuid.
    """
    marked = bool(children(tree, "sheet_instances"))
    own = next((value(c) for c in children(tree, "uuid")), None)
    tops = {value(p).strip("/").split("/")[0]
            for sym in children(tree, "symbol") for inst in children(sym, "instances")
            for proj in children(inst, "project") for p in children(proj, "path")}
    others = sorted(tops - {own})
    if others and marked:
        return None, ("cannot tell whether this is a project's root sheet: it has sheet_instances, "
                      f"but its symbols belong to the root sheet with uuid {others[0]}")
    if others:
        return "sub", f"a sub-sheet of the project whose root sheet has uuid {others[0]}; query that root schematic"
    if marked or own in tops:
        return "root", None
    return None, "cannot tell whether this is a project's root sheet (no sheet_instances, no symbol instance paths)"


def sheet_files(tree):
    return [value(prop, 2) for sheet in children(tree, "sheet") for prop in children(sheet, "property")
            if len(prop) > 1 and prop[1] == "Sheetfile"]


def read_file(path, head=False):
    """The file's bytes; with head, only what classify() reads unless it is a KiCad sheet."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(HEAD if head else -1)
            if head and classify(data) == "kicad":
                data += fh.read()
            return data
    except OSError as e:
        raise Refusal(3, f"{path}: unreadable: {e}")


def sheet_path(referrer, name):
    return os.path.join(os.path.dirname(referrer), name)


def kicad_inputs(path, read=read_file, join=sheet_path, exists=os.path.exists):
    """The root and every sub-sheet it pulls in, plus its .kicad_pro: {path: bytes}."""
    inputs, pending = {}, [path]
    while pending:
        f = pending.pop()
        if f in inputs:
            continue
        inputs[f] = read(f)
        try:
            tree = sexpr(inputs[f])
            role, why = sheet_role(tree) if f == path else ("root", None)
            sheets = sheet_files(tree)
        except ValueError as e:
            raise Refusal(3, f"{f}: malformed KiCad schematic: {e}")
        if role != "root":
            raise Refusal(2, f"{path}: {why}")
        pending += [join(f, s) for s in sheets]
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    if exists(pro):
        inputs[pro] = read(pro)
    return inputs


def export_netlist(path):
    """KiCad's own netlist for the root sheet at path, and the inputs it read."""
    before = kicad_inputs(path)
    cli = shutil.which("kicad-cli")
    if cli is None:
        raise Refusal(3, "kicad-cli is not installed; KiCad schematics need it for connectivity")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "netlist.xml")
        run = subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, path],
                             capture_output=True, text=True)
        if run.returncode != 0 or not os.path.exists(out):
            raise Refusal(3, f"{path}: kicad-cli failed ({run.returncode}): {(run.stderr or run.stdout).strip()}")
        with open(out, "rb") as fh:
            netlist = fh.read()
    for f, data in before.items():
        try:
            same = read_file(f) == data
        except Refusal:
            same = False
        if not same:
            raise Refusal(3, f"{path}: {f} changed while KiCad exported it; run the query again")
    return netlist, before


def load_kicad_netlist(data, path):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise Refusal(3, f"{path}: malformed KiCad netlist: {e}")
    parts = {}
    for c in root.iterfind("components/comp"):
        ref = c.get("ref")
        if ref in parts:
            raise Refusal(3, f"{path}: two components are annotated {ref}; annotate the schematic again")
        lib = c.find("libsource")
        sheet = c.find("sheetpath")
        parts[ref] = Part(
            ref, c.findtext("value"),
            f"{lib.get('lib')}:{lib.get('part')}" if lib is not None else "",
            c.findtext("footprint") or None,
            {f.get("name"): f.text or "" for f in c.iterfind("fields/field") if f.get("name") != "Footprint"},
            sheet=sheet.get("names") if sheet is not None else None,
            dnp=["(all)"] if c.find("property[@name='dnp']") is not None else [])
    nets = {}
    seen = {}
    for net in root.iterfind("nets/net"):
        name = net.get("name")
        members = nets.setdefault(name, [])
        for node in net.iterfind("node"):
            key = (node.get("ref"), node.get("pin"))
            if key in seen:
                if seen[key] != name:
                    raise Refusal(3, f"{path}: {key[0]} pin {key[1]} is on both {seen[key]} and {name}")
                continue
            part = parts.get(key[0])
            if part is None:
                raise Refusal(3, f"{path}: net {name} references {key[0]}, which the netlist does not list")
            seen[key] = name
            pin = Pin(key[0], key[1], name=node.get("pinfunction") or None,
                      no_connect="no_connect" in (node.get("pintype") or ""), net=name)
            part.pins.append(pin)
            members.append(pin)
    return Design("kicad", parts, nets)


# ---- sources -----------------------------------------------------------------

REPO = r"[\w.-]+/[\w.-]+"
SCHEMATICS = (".sch", ".kicad_sch")
ORIGIN = re.compile(r"^(?:https://(?:[^@/\s]+@)?github\.com/|git@github\.com:|ssh://git@github\.com(?::22)?/)"
                    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
SPEC = re.compile(rf"^({REPO}):(.+)$")


def git(repo, *args, lazy=False, fail=None):
    """A finished git command in repo; with fail, a failure refuses as "fail: why".

    Only the cache may download objects.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1")
    if not lazy:
        env["GIT_NO_LAZY_FETCH"] = "1"
    run = subprocess.run(["git", "-C", repo, *args], capture_output=True, env=env)
    if fail and run.returncode != 0:
        raise Refusal(3, f"{fail}: {why(run)}")
    return run


def text(data):
    return data.decode("utf-8", "replace")


def out(run):
    return text(run.stdout).strip()


def why(run):
    lines = text(run.stderr).strip().splitlines()
    return lines[-1] if lines else f"git exited {run.returncode}"


def escapes(rel):
    """True when a normalized relative path leaves the directory it is relative to."""
    return rel == ".." or rel.startswith("../") or rel.startswith("/")


def sources():
    listed = list(filter(None, os.environ.get("READ_PCB_SOURCES", "").split(os.pathsep)))
    folded = [s.lower() for s in listed]
    for i, s in enumerate(listed):
        if not re.fullmatch(REPO, s) or {".", ".."} & set(s.split("/")):
            raise Refusal(3, f"READ_PCB_SOURCES: {s!r} is not owner/repo")
        if folded.index(folded[i]) != i:
            raise Refusal(3, f"READ_PCB_SOURCES names {s} twice")
    return tuple(listed) or ("hathach/pcb",)


def source_named(name):
    return next((s for s in sources() if s.lower() == name.lower()), None)


def toplevel(path):
    run = git(path if os.path.isdir(path) else os.path.dirname(path), "rev-parse", "--show-toplevel")
    return out(run) if run.returncode == 0 else None


def clones():
    """{source: clone dir} from READ_PCB_CLONES; any doubt about an entry refuses."""
    found, listed = {}, sources()
    for entry in filter(None, os.environ.get("READ_PCB_CLONES", "").split(os.pathsep)):
        path = os.path.expanduser(entry)
        top = toplevel(path) if os.path.isdir(path) else None
        if top is None:
            raise Refusal(3, f"READ_PCB_CLONES: {entry} is not a git repository")
        url = git(top, "remote", "get-url", "origin")
        m = ORIGIN.match(out(url)) if url.returncode == 0 else None
        repo = source_named(f"{m.group(1)}/{m.group(2)}") if m else None
        if repo is None:
            raise Refusal(3, f"READ_PCB_CLONES: {entry} has origin {out(url) or '(none)'}, "
                             f"which is not one of {', '.join(listed)}; list it in READ_PCB_SOURCES")
        if repo in found:
            raise Refusal(3, f"READ_PCB_CLONES names two clones of {repo}: {found[repo]} and {top}")
        found[repo] = top
    return found


def unavailable(run):
    return f"provenance unavailable ({why(run)})"


def head_sha(top):
    """(sha, None), (None, "no HEAD") or (None, the git failure)."""
    head = git(top, "rev-parse", "--verify", "-q", "HEAD")
    if head.returncode == 0:
        return out(head), None
    return None, unavailable(head) if head.stderr.strip() else "no HEAD"


def worktree_state(top, sha, path, data):
    """How the bytes read compare with commit sha, the one baseline of a header."""
    rel = os.path.relpath(path, top)
    if escapes(rel):
        return "outside the repository"
    entry = git(top, "ls-tree", "-z", sha, "--", rel)
    if entry.returncode != 0:
        return unavailable(entry)
    if not entry.stdout:
        index = git(top, "ls-files", "-z", "--", rel)
        if index.returncode != 0:
            return unavailable(index)
        return "not in HEAD" if index.stdout else "untracked"
    blob = git(top, "cat-file", "blob", f"{sha}:{rel}")
    if blob.returncode != 0:
        return unavailable(blob)
    return "clean" if blob.stdout == data else "modified"


@dataclass
class Source:
    path: str                   # the local file loaded
    name: str                   # owner/repo:path, or the path itself
    top: str | None = None      # the checkout its bytes are compared with
    place: str = ""             # where top is, as the citation words it
    cached: str | None = None   # a cache's own citation, which replaces the comparison


class Cache:
    """A shallow, blobless clone of one source, pinned to origin's HEAD as fetched now."""

    def __init__(self, repo):
        self.repo = repo
        self.dir = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                                "read-pcb", repo)
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        with open(self.dir + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # one clone or fetch at a time; FETCH_HEAD is shared
            self._sync()

    def _sync(self):
        if not os.path.isdir(os.path.join(self.dir, ".git")):
            url = os.environ.get("READ_PCB_REMOTE_BASE", "https://github.com/") + self.repo + ".git"
            with tempfile.TemporaryDirectory(prefix=".clone-", dir=os.path.dirname(self.dir)) as tmp:
                git(tmp, "clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout", url, "repo",
                    lazy=True, fail=f"{self.repo}: cannot clone {url}")
                os.replace(os.path.join(tmp, "repo"), self.dir)
        git(self.dir, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", "HEAD", lazy=True,
            fail=f"{self.repo}: cannot fetch origin")
        self.sha = out(git(self.dir, "rev-parse", "--verify", "FETCH_HEAD",
                           fail=f"{self.repo}: fetched no FETCH_HEAD"))
        self.fetched = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        listing = git(self.dir, "ls-tree", "-r", "-z", self.sha, fail=f"{self.repo}: cannot list {self.sha}")
        self.tree = {}
        for entry in text(listing.stdout).split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                self.tree[path] = meta.split()[0]

    def read(self, rel):
        mode = self.tree.get(rel)
        if mode is None:
            raise Refusal(3, f"{self.repo}:{rel} is not in {self.sha[:12]}")
        if mode == "120000":
            raise Refusal(3, f"{self.repo}:{rel} is a symlink; not followed")
        return git(self.dir, "cat-file", "blob", f"{self.sha}:{rel}", lazy=True,
                   fail=f"{self.repo}:{rel}: cannot read its blob").stdout

    def join(self, referrer, name):
        rel = posixpath.normpath(posixpath.join(posixpath.dirname(referrer), name))
        if escapes(rel):
            raise Refusal(3, f"{self.repo}:{referrer} names sheet {name}, outside the repository")
        return rel

    def materialize(self, rel, into):
        """Copy the file, and for KiCad every input KiCad reads, into a temp tree."""
        data = self.read(rel)
        files = kicad_inputs(rel, self.read, self.join, lambda f: f in self.tree) \
            if classify(data) == "kicad" else {rel: data}
        for f, blob in files.items():
            dest = os.path.join(into, f)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(blob)
        return os.path.join(into, rel)

    def cite(self):
        return f"@ {self.sha[:12]} | cache {self.dir}, origin HEAD fetched {self.fetched}"


def tag(data):
    """find's format tag; None hides a KiCad sub-sheet."""
    fmt = classify(data)
    if fmt != "kicad":
        return fmt
    try:
        role, _ = sheet_role(sexpr(data))
    except ValueError:
        role = None
    if role == "sub":
        return None
    return "kicad" if role == "root" else "kicad, root unknown"


def local_listing(top, where="."):
    """{path relative to top: git state or ""} for files under where."""
    files = git(top, "ls-files", "-z", "--cached", "--", where, fail=f"{top}: git ls-files failed")
    status = git(top, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all", "--", where,
                 fail=f"{top}: git status failed")
    if status.stderr.strip():  # e.g. a directory it could not open; its files would be missing
        raise Refusal(3, f"{top}: git status: {why(status)}; the search would be incomplete")
    states = {e[3:]: "deleted" if "D" in e[:2] else "untracked" if e[:2] == "??" else "modified"
              for e in text(status.stdout).split("\0") if len(e) > 3}
    listed = set(text(files.stdout).split("\0")) | {f for f, st in states.items() if st == "untracked"}
    return {f: states.get(f, "") for f in listed if f and states.get(f) != "deleted"}


def unreadable(e):
    raise Refusal(3, f"{e.filename} unreadable ({e.strerror}); the search would be incomplete")


def dir_candidates(where):
    """find's (name, path matched, git state, read) for every file under where."""
    base = os.path.realpath(os.path.expanduser(where))  # git reports its top level resolved
    if not os.path.isdir(base):
        raise Refusal(2, f"--in {where}: not a directory")
    top = toplevel(base)
    if top:
        return git_candidates(top, base, base + os.sep)
    rows = []
    for d, _, fs in os.walk(base, onerror=unreadable):
        for f in fs:
            p = os.path.join(d, f)
            rows.append((p, os.path.relpath(p, base), "", partial(read_file, p, head=True)))
    return rows


def git_candidates(top, base, prefix):
    """find's rows for the files git lists under base, matched and named relative to base."""
    rows = []
    for f, st in local_listing(top, os.path.relpath(base, top)).items():
        rel = os.path.relpath(os.path.join(top, f), base)
        rows.append((prefix + rel, rel, st, partial(read_file, os.path.join(base, rel), head=True)))
    return rows


def cache_candidates(repo):
    """Only KiCad sheets are read, to hide sub-sheets; other blobs stay on the server."""
    cache = Cache(repo)
    return [(f"{repo}:{rel}", rel, "", partial(cache.read, rel) if rel.endswith(".kicad_sch") else None)
            for rel in cache.tree]


def cmd_find(keywords, where):
    failures = []
    if where:
        candidates = dir_candidates(where)
    else:
        candidates, local = [], clones()
        for repo in sources():
            try:
                candidates += (git_candidates(local[repo], local[repo], f"{repo}:") if repo in local
                               else cache_candidates(repo))
            except Refusal as e:
                failures.append(f"{repo} unavailable ({e})")
    keys = [k.lower() for k in keywords]
    rows, unread = [], 0
    for name, rel, state, read in candidates:
        if not (rel.endswith(SCHEMATICS) and all(k in rel.lower() for k in keys)):
            continue
        try:
            t = tag(read()) if read else "?"
        except Refusal as e:
            t, unread = f"unreadable: {e}", unread + 1
        if t:
            rows.append((name, t, state))
    for name, t, state in sorted(rows, key=lambda r: (natural(r[0]), r[0])):
        print(f"{name}  [{t}]" + (f" [{state}]" if state else ""))
    if unread:
        failures.append(f"{unread} file(s) could not be classified")
    if failures:
        print("search incomplete: " + "; ".join(failures))
        return 3
    return 0 if rows else 1


def open_source(spec, tmp):
    """The Source spec names; a cache copy goes into tmp."""
    m = SPEC.match(spec)
    repo = source_named(m.group(1)) if m else None
    local = clones()
    if repo is None:
        path = os.path.realpath(os.path.expanduser(spec))
        for r, top in local.items():
            rel = os.path.relpath(path, top)
            if not escapes(rel):
                return Source(path, f"{r}:{rel}", top, f"local clone {top}")
        top = toplevel(path)
        return Source(path, path, top, top or "")
    rel = posixpath.normpath(m.group(2))
    if escapes(rel):
        raise Refusal(2, f"{spec}: the path after the colon must stay inside {repo}; "
                         "name a file elsewhere by its filesystem path")
    if repo in local:
        return Source(os.path.join(local[repo], rel), f"{repo}:{rel}", local[repo], f"local clone {local[repo]}")
    cache = Cache(repo)
    return Source(cache.materialize(rel, tmp), f"{repo}:{rel}", cached=cache.cite())


def describe(src, inputs, fmt):
    """The citation line every answer opens with."""
    if src.cached:
        return f"source: {src.name} {src.cached} [{fmt}]"
    if src.top is None:
        return f"source: {src.name} | not in git [{fmt}]"
    sha, trouble = head_sha(src.top)
    states = {f: trouble or worktree_state(src.top, sha, f, data) for f, data in inputs.items()}
    state = states.pop(src.path)
    others = [f"{os.path.relpath(f, src.top)} {st}" for f, st in states.items() if st != "clean"]
    return (f"source: {src.name} @ {sha[:12] if sha else '(no HEAD)'} | {src.place}, working tree {state}"
            + (f"; inputs {', '.join(others)}" if others else "") + f" [{fmt}]")


# ---- loading and queries -----------------------------------------------------

def load(path):
    """(design, {path: bytes} of every file the answer came from)."""
    data = read_file(path)
    fmt = classify(data)
    if fmt == "eagle":
        return load_eagle(data, path), {path: data}
    if fmt == "kicad":
        netlist, inputs = export_netlist(path)
        return load_kicad_netlist(netlist, path), inputs
    if fmt == "kicad-legacy":
        raise Refusal(3, f"{path}: legacy KiCad 5 schematic; open and save it in KiCad 6 or later")
    if fmt == "eagle-binary":
        raise Refusal(3, f"{path}: pre-6 binary EAGLE schematic; open and save it in EAGLE 6 or later")
    raise Refusal(3, f"{path}: XML that is not an EAGLE schematic")


def pin_line(pin, part, with_part=False):
    cells = [pin.ref] if with_part else []
    cells.append(pin.label(part.repeated()))
    if pin.pads and pin.pads != (pin.pin,):
        cells.append("pad " + ",".join(pin.pads))
    if with_part:
        cells += [part.value or "", part.device] + part.dnp_notes()
    else:
        cells.append(pin.net or "(no net)")
    if pin.no_connect:
        cells.append("[no-connect]")
    return "  ".join(c for c in cells if c)


def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def cmd_nets(design, pattern):
    names = sorted((n for n in design.nets if not pattern or pattern.lower() in n.lower()), key=natural)
    for n in names:
        print(f"{n}  ({len(design.nets[n])})")
    return 0 if names else 1


def part_line(p):
    cells = [p.ref, p.value or "", p.device, p.package or ""]
    cells += [f"{k}={v}" for k, v in p.attributes.items() if v]
    cells += p.dnp_notes()
    return "  ".join(c for c in cells if c)


def cmd_parts(design, keywords):
    keys = [k.lower() for k in keywords]
    hits = []
    for p in design.parts.values():
        hay = " ".join([p.ref, p.value or "", p.device, p.package or ""]
                       + [f"{k} {v}" for k, v in p.attributes.items()]).lower()
        if all(k in hay for k in keys):
            hits.append(p)
    for p in sorted(hits, key=lambda p: natural(p.ref)):
        print(part_line(p))
    return 0 if hits else 1


def lookup(kind, names, name):
    """name itself, else its one case-insensitive match; a miss prints close names and gives None."""
    found = [name] if name in names else [n for n in names if n.lower() == name.lower()]
    if len(found) == 1:
        return found[0]
    close = sorted((n for n in names if name.lower() in n.lower()), key=natural)
    print(f"no {kind} {name}" + (": did you mean " + ", ".join(close[:10]) if close else ""))
    return None


def cmd_part(design, ref):
    ref = lookup("part", design.parts, ref)
    if ref is None:
        return 1
    part = design.parts[ref]
    print(part_line(part))
    if part.sheet:
        print(f"sheet {part.sheet}")
    for pin in sorted(part.pins, key=lambda p: (natural(p.gate or ""), natural(p.pin))):
        print("  " + pin_line(pin, part))
    return 0


def cmd_net(design, name):
    net = lookup("net", design.nets, name)
    if net is None:
        return 1
    print(net)
    for pin in sorted(design.nets[net], key=lambda p: (natural(p.ref), natural(p.pin))):
        print("  " + pin_line(pin, design.parts[pin.ref], with_part=True))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="pcb.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("find")
    s.add_argument("--in", dest="where", metavar="PATH")
    s.add_argument("keyword", nargs="*")
    s = sub.add_parser("nets")
    s.add_argument("sch")
    s.add_argument("pattern", nargs="?")
    s = sub.add_parser("parts")
    s.add_argument("sch")
    s.add_argument("keyword", nargs="*")
    s = sub.add_parser("part")
    s.add_argument("sch")
    s.add_argument("ref")
    s = sub.add_parser("net")
    s.add_argument("sch")
    s.add_argument("name")
    return p


def main(argv):
    p = parser()
    args = p.parse_args(argv)
    if args.cmd == "find" and not args.where and not args.keyword:
        p.error("find needs a keyword, or --in PATH")
    try:
        if args.cmd == "find":
            return cmd_find(args.keyword, args.where)
        with tempfile.TemporaryDirectory() as tmp:
            src = open_source(args.sch, tmp)
            design, inputs = load(src.path)
            print(describe(src, inputs, design.fmt))
        if args.cmd == "nets":
            return cmd_nets(design, args.pattern)
        if args.cmd == "parts":
            return cmd_parts(design, args.keyword)
        if args.cmd == "part":
            return cmd_part(design, args.ref)
        return cmd_net(design, args.name)
    except Refusal as e:
        print(str(e), file=sys.stderr)
        return e.code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
