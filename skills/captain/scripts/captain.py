#!/usr/bin/env python3
"""Mechanics of the captain: find the Claude Code sessions in this host's hc terminals, pin each one's identity, and
tell whether its screen shows Claude Code's input line before anything is typed into it.

Judgment stays in SKILL.md. Every check refuses rather than guesses: an identity it cannot pin or a screen it
does not recognize is reported, never assumed safe.

  captain.py list [--json]                       every other Claude Code session, latest status report first
  captain.py enroll [--replace]                  make this session the captain
  captain.py ask <target>... [--force]           send each idle target a status request, unless unchanged
  captain.py watch <target>...|all               have sessions report each task end, new and restarted ones too
  captain.py watches                             the open watch and its members
  captain.py collect [<msg>...] [--wait MS]      print each report as it lands
  captain.py reports [<msg>[:<N>]...]            the latest report from each target, or those requests' reports
  captain.py reports --new                       every watch report since the last --new
  captain.py cancel <msg>                        close a request, relay, watch or decision no answer will close
  captain.py queue [--json] [--all] [--refresh]  the decisions waiting on the human, sending nothing
  captain.py ingest --chief <run>                record a headless chief run's attention lines as decisions
  captain.py relay <target>... [--entry <id>] [--dry-run] [--force]
                                                 relay the human's message this turn acts on; --force first
                                                 cancels a request or relay still open to a target
  captain.py relay <target> --item <id> --revision <N> --verdict <verdict> [--entry <id>]
                                                 relay it as the human's verdict on a decision
  captain.py verdict --item <id> --revision <N> --verdict <verdict> [--entry <id>]
                                                 record it as that verdict, sending nothing
  captain.py relay --retry <action>              send a held relay once its session is idle or done, its input line shown
  captain.py start <checkout> [main|ada] [--name <n>] [--space <id|name>] [--tab <id>] [--entry <id>] [--task]
                                                 start a Claude Code session there on the human's message;
                                                 --task relays that message to it as its first task
  captain.py brief --for <msg> | --pending       a target reads what a status request asks, or joins a watch
  captain.py report --for <msg> --file <json|->  a target answers a status request
  captain.py accept --action <action>            a target takes a relay and reads its words
  captain.py show --action <action>              a target reads a relay it accepted again, recording nothing
  captain.py outcome --action <action> --result <result> [--note-file <file|->]
                                                 a target says how a relay ended

A target is a terminal address (t7) or space:<herdr space id or name>, which must match exactly one session; `all`,
alone, is every live session.

A message that opens `tell <name>... [to]`, or the names alone, each an address, space id or name, or space:<id or
name>, is relayed without that opening when the names are exactly the relay's targets, and refused when they are not
or cannot be told apart from the instruction. One addressed to every session (`tell all|everyone|every session
to|:`) is relayed without it when the targets are exactly the live sessions. The message relayed is one the human wrote in the
captain's current turn, typed or read mid-turn; a message hc pasted (`<hc from=...>`) is never the human's.

A watch types into nobody: a session joins it itself, through hooks/captain (SessionStart after a compaction or
/clear, Stop) or `brief --pending`, and it stands until the human cancels it, whichever captain is enrolled or none.
A collect without ids resumes where the last one stopped, and `reports --new` where its last one did, whichever
captain ran it.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: /proc is Linux-only, so the captain is too
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))   # the agentrc checkout this runs from
import hc  # noqa: E402
import captain_read  # noqa: E402

CONFIG_DIRS = {'main': None, 'ada': os.path.expanduser('~/.claude-ada')}   # main is CLAUDE_CONFIG_DIR unset
DEFAULT_CONFIG = '~/.claude'   # where main, CLAUDE_CONFIG_DIR unset, keeps its transcripts
HC_TIMEOUT_S = 30
CLAUDE_APP = 'claude-code'
RULE = re.compile(r'^─{20,}')  # the top rule may end in a session label: ──── alo68 ─
BLANK = ' \xa0\t\r'
CLAUDE_HINTS = re.compile(r'⏵⏵|\? for shortcuts|shift\+tab|esc to interrupt')
# hc wraps what it pastes into an agent as <hc from="t7 name" ...>text</hc>; a tag anywhere else, or a forged one,
# is another program's text all the same
HC_TAG = re.compile(r'<\s*/?\s*hc\b', re.I)
HC_WRAPPED = re.compile(r'<hc from="[^"<>]*"(?: [a-z]+="[^"<>]*")*>(.*)</hc>', re.S)
STATE = os.environ.get('AGENTRC_CAPTAIN_DIR') or os.path.expanduser('~/.cache/agentrc/captain')
LEDGER, ENROLLED = 'ledger-hc.jsonl', 'enrolled-hc.json'   # ledger.jsonl and enrolled.json pinned Herdr panes
# where the enrolled captain's last collect without ids, and its last `reports --new`, stopped reading
COLLECTED, TABLED = 'collected-hc.json', 'tabled-hc.json'
TABLING = 'tabled.lock'   # serializes `reports --new` without holding the ledger lock while it prints
# serializes each observation with its record, so one taken earlier never lands after one taken later; taken before
# the ledger lock, never while holding it, and gh and git run under it with the ledger free
OBSERVING = 'observing.lock'
CHIEF_RUNS = os.path.expanduser('~/.cache/chief-runs')   # where chief_run.py's --out directories live
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')
PROGRESS_LINE = re.compile(r'(\d+) \d\d:\d\d:\d\d (.*)')
ATTENTION = 'chief: attention'
# its hardened, read-only git and gh lookups; it stops its own tool's process group after its TIMEOUT
READER = str(Path(__file__).resolve().with_name('captain_read.py'))
SOURCES = ('pr', 'worktree', 'report')
VERDICTS = ('correction', 'no-correction', 'decision')
UNSETTLED = ('pending', 'held', 'sending', 'undelivered', 'uncertain', 'refused', 'failed', 'rejected',
             'needs-clarification')   # a relayed verdict in one of these keeps its closed decision in the queue
UNCOVERED = 'Codex terminals and remote sessions are not covered'
# what `start` runs for each account: main with CLAUDE_CONFIG_DIR unset in the program itself, since hc gives it the
# core's environment; ada through its wrapper, whose argv[0] Herdr's my-agents mod draws the account icon from
LAUNCHERS = {'main': ('env', 'claude'), 'ada': ('claude-ada',)}
TERMINAL_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,31}')   # hc's rule for a name, which also refuses an address
START_WAIT_S = 20
VERIFIED = ('verified', 'pending', 'failed', 'unchecked')
INSTALLED = (os.environ.get('AGENTRC_CAPTAIN_SCRIPT')
             or os.path.expanduser('~/.claude/skills/captain/scripts/captain.py'))
IDENTITY = ('address', 'created', 'kind', 'cwd', 'session', 'pid', 'started', 'account')
ASKABLE = ('idle', 'done')
REPORT = {'task': str, 'prs': list, 'state': str, 'evidence': str, 'chief': str, 'needs': str, 'next': str}
# a report without issues names no issue; the rest are decision operations, needing an event_id
OPTIONAL = {'issues': list, 'event_id': str, 'decisions': list, 'reopen': list, 'refresh': list, 'resolved': list}
OPS = ('decisions', 'reopen', 'refresh', 'resolved')
KINDS = ('blocked', 'review', 'idea')
DECISION = {'key': str, 'kind': str, 'ask': str, 'recommendation': str}
DECISION_OPTIONAL = {'change_id': str, 'pr': str, 'worktree': str, 'reviewed_sha': str, 'grant_needed': str,
                     'evidence_refs': list}
DECISIONS_MAX = 20
KEY = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
EVENT_ID = re.compile(r'[A-Za-z0-9._:-]{1,64}')
SHA = re.compile(r'[0-9a-f]{7,40}')
PR_REF = re.compile(r'[\w.-]+/[\w.-]+#\d+')
STATES = ('ready', 'in-progress', 'blocked', 'needs-human', 'done', 'idle')
# withdraw and withdraw-requested have no writer since `withdraw` was removed: kept so an older ledger still reads
RECORDS = ('request', 'report', 'cancel', 'delivered', 'undelivered', 'uncertain',
           'relay', 'held', 'sending', 'accepted', 'outcome', 'withdraw', 'withdraw-requested', 'refused',
           'watch', 'watch-extend', 'joined', 'watch-end', 'evidence', 'observed', 'verdict',
           'start', 'started', 'start-failed', 'start-uncertain')
ITEMS = ('request', 'relay')
WATCHED = ('watch', 'joined')
FOLLOWED = ITEMS + WATCHED   # what collect can follow
WATCH_ENDS = ('cancel', 'watch-end')
MARKER = 'watch-active'   # present while a watch may be open: a hook with none skips the ledger
CLOSED = ('report', 'cancel', 'undelivered', 'outcome', 'withdraw', 'refused')
RESULTS = ('completed', 'failed', 'rejected', 'needs-clarification')
REPORT_MAX = 8192
POLL_S = 2
LOCK_POLL_S = 0.1
# The hook's lock wait alone: its 30 s timeout also spans my_session()'s hc calls, up to HC_TIMEOUT_S each.
HOOK_LOCK_S = 10
NO_ANSWER = 3
SPACE = 'space:'
TELL = re.compile(r'\s*tell\s+', re.I)
NAME = re.compile(r'[^\s,:;&]+(?::[^\s,:;&]+)*')   # space:<name> keeps its colon
ADDRESS = re.compile(r't[0-9][0-9a-z]*', re.I)   # a local hc terminal: t7, t1b
JOIN = re.compile(r'\s*,?\s*(?:and|&)\s+|\s*,\s*', re.I)
TO = re.compile(r'\s+to\s+|\s*[:,]\s*|\s+', re.I)
EVERY_WORD = re.compile(r'(?:all|everyone|every)(?=[\s:]|$)', re.I)   # never one recipient: every session
# No comma or bare space after it: "all, code" and "all tinyusb sessions" cannot be told from the instruction.
EVERY_ONE = re.compile(r'(?:all(?:\s+sessions?)?|everyone|every\s+session)(?:\s+to(?:\s+|$)|\s*:\s*)', re.I)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def get(address):
    """`hc get` for one terminal, None when hc knows none by that address."""
    return hc.get(address, timeout=HC_TIMEOUT_S)


def footer_only(lines):
    """Below the input only Claude Code's own footer may follow: its hint line, and at most one row above it, the
    user's statusline, which may say anything; all indented and without ❯. Anything else, such as a dialog, means
    the box found is not the live input."""
    shown = [line for line in lines if line]
    return (1 <= len(shown) <= 2 and all(t[0] in BLANK and '❯' not in t for t in shown)
            and bool(CLAUDE_HINTS.search(shown[-1])))


def shows_input(screen):
    """Whether an `hc read` screen ends in Claude Code's input line, not a dialog or anything else. Text already in
    the line does not matter: a paste appends to it. Claude Code draws its input between the last two full-width
    rules, the first line led by ❯."""
    lines = [line.rstrip(BLANK) for line in screen.splitlines()]
    rules = [i for i, line in enumerate(lines) if RULE.match(line)]
    if len(rules) < 2 or rules[-1] - rules[-2] < 2 or not footer_only(lines[rules[-1] + 1:]):
        return False
    return lines[rules[-2] + 1].lstrip(BLANK).startswith('❯')


def read_screen(address):
    return hc.read(address, timeout=HC_TIMEOUT_S)


def account(pid):
    """The Claude account a process runs under, from its own environment, never its terminal's name. main only with
    CLAUDE_CONFIG_DIR unset: set, even to ~/.claude, Claude Code keeps its state in <dir>/.claude.json rather than
    ~/.claude.json, another install. Any other dir reads config:<dir>, which no account name can equal."""
    try:
        with open(f'/proc/{pid}/environ', 'rb') as f:
            env = dict(v.split(b'=', 1) for v in f.read().split(b'\0') if b'=' in v)
    except OSError:
        return None
    if b'CLAUDE_CONFIG_DIR' not in env:
        return 'main'
    config = env[b'CLAUDE_CONFIG_DIR'].decode(errors='replace')
    full = os.path.expanduser(config)
    if not os.path.isabs(full):   # relative to the process's cwd, not ours
        return f'config:{config}'
    return next((name for name, d in CONFIG_DIRS.items() if d and os.path.realpath(d) == os.path.realpath(full)),
                f'config:{config}')


def registered(pid, start, account_name):
    """The session id Claude Code registered for process pid, started at start ticks, in sessions/<pid>.json under
    the config dir of the account it runs under; None when that entry is missing or names another process, or when
    another account's dir holds one for the same process too."""
    own = config_dir(account_name)
    if not own:
        return None
    own = os.path.realpath(own)
    found = []
    for d in {own, *(os.path.realpath(config_dir(name)) for name in CONFIG_DIRS)}:
        try:
            with open(os.path.join(d, 'sessions', f'{pid}.json'), encoding='utf-8') as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get('pid') == pid and entry.get('procStart') == str(start):
            found.append((d, entry.get('sessionId')))
    return found[0][1] if len(found) == 1 and found[0][0] == own and text(found[0][1]) else None


def canonical(cwd):
    """An absolute cwd resolved, else None: realpath('') would name the captain's own checkout."""
    return os.path.realpath(cwd) if cwd and os.path.isabs(cwd) else None


def described(t):
    """The session fields `hc get` shows for a live local Claude Code terminal, or None for any other. Its cwd and
    pid are its foreground process's: the terminal's own are the shell's it may have started from. said is its status
    line on one line, blocked_on a blocked one's kind."""
    status = (t or {}).get('status') or {}
    if not t or t.get('state') != 'live' or status.get('app') != CLAUDE_APP or not hc.is_local(t.get('address')):
        return None
    labels = t.get('labels') or {}
    where, fg = labels.get('where'), t.get('foreground') or {}
    return {
        'handle': f'{where or "?"} · {t.get("name") or "claude"} · {t["address"]}',
        'space_id': labels.get('herdr.space'),
        'space': where.rsplit(' / ', 1)[0] if where else None,   # where is "<space> / <tab>"
        'address': t['address'],
        'created': t.get('created_ms'),
        'kind': 'claude',
        'cwd': canonical(fg.get('cwd')),
        'foreground': fg.get('pid'),
        'status': status.get('state') or 'unknown',
        'status_ms': t.get('status_ms') or 0,
        'said': one_line(status.get('msg') or ''),
        'blocked_on': status.get('kind') if status.get('state') == 'blocked' else None,
    }


def sessions(terminals=None, me=None):
    """Every live local Claude Code terminal but me (this terminal by default, '' for none), each with a handle for
    the human, the latest status report first. Codex terminals are left out: a Codex target could never prove which
    terminal answers."""
    terminals = hc.terminals(timeout=HC_TIMEOUT_S) if terminals is None else terminals
    me = os.environ.get('HC_TERMINAL_ID') if me is None else me
    found = [described(get(t['address'])) for t in terminals
             if t.get('agent') == CLAUDE_APP and t.get('address') != me and hc.is_local(t.get('address'))]
    return sorted((s for s in found if s), key=lambda s: (-s['status_ms'], s['address']))


def pin(session):
    """Add the foreground process's incarnation, the account it runs under and the Claude Code session it
    registered; None where they cannot be read."""
    pid = session.get('foreground')
    try:
        start = hc.proc_start(pid) if type(pid) is int and pid > 0 else None
    except (hc.Gone, hc.Error):
        start = None
    session['pid'], session['started'] = (pid, start) if start else (None, None)
    session['account'] = account(pid) if start else None
    session['session'] = registered(pid, start, session['account']) if session['account'] else None
    return session


def unpinned(session):
    """The identity fields a pinned session lacks; a session missing any is never typed into."""
    return [k for k in ('cwd', 'created', 'pid', 'started', 'session', 'account') if not session.get(k)]


class Corrupt(Exception):
    """A ledger line that is not a whole JSON record: what it held cannot be known, so nothing may be decided."""


def path(name):
    return os.path.join(STATE, name)


class Busy(Exception):
    """The ledger lock stayed held past a bounded wait."""


@contextmanager
def locked(wait_s=None, name='lock'):
    """Every ledger read that decides something and every append happen under this one lock, so two invocations
    (the captain, or targets reporting) cannot both see a request open and both act on it. wait_s bounds the wait,
    raising Busy once it lapses: `ask` and `relay` hold the lock for up to a minute. name picks another lock."""
    if not fcntl:
        die('this script needs POSIX file locks', 2)
    os.makedirs(STATE, exist_ok=True)
    with open(path(name), 'a') as f:
        if wait_s is None:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Busy(f'the ledger lock stayed held for {wait_s:.0f} s') from None
                    time.sleep(LOCK_POLL_S)
        yield


def well_formed(rec):
    """Every field a decision reads has the shape it needs: a record that closes or opens a request on a wrong
    value would decide as wrongly as a torn one."""
    if not isinstance(rec, dict) or rec.get('type') not in RECORDS or not isinstance(rec.get('msg'), str):
        return False
    if rec['type'] == 'report':
        return validate(rec.get('report')) is None
    if rec['type'] == 'outcome':
        return rec.get('result') in RESULTS and isinstance(rec.get('note'), str)
    if ('item' in rec or rec['type'] == 'verdict') and not well_bound(rec):
        return False
    if rec['type'] == 'verdict':
        captain = rec.get('captain')
        return text(rec.get('entry')) and text(rec.get('quote')) and pinned_identity(captain)
    if rec['type'] in ITEMS:
        captain = rec.get('captain')
        relayed = rec['type'] != 'relay' or all(text(rec.get(k)) for k in ('entry', 'quote', 'context'))
        return (relayed and isinstance(rec.get('handle'), str) and pinned_identity(rec.get('target'))
                and pinned_identity(captain) and text(captain.get('generation')))
    if rec['type'] == 'watch':
        captain = rec.get('captain')
        return pinned_identity(captain) and text(captain.get('generation')) and well_scoped(rec.get('scope'))
    if rec['type'] == 'watch-extend':
        return well_scoped(rec.get('scope'))
    if rec['type'] == 'joined':
        return text(rec.get('watch')) and isinstance(rec.get('handle'), str) and pinned_identity(rec.get('target'))
    if rec['type'] in ('watch-end', 'held'):
        return isinstance(rec.get('why'), str)
    if rec['type'] == 'start':
        return (all(text(rec.get(k)) for k in ('entry', 'quote', 'dir', 'name', 'space'))
                and os.path.isabs(rec['dir']) and rec.get('account') in LAUNCHERS
                and (rec.get('tab') is None or text(rec.get('tab'))) and pinned_identity(rec.get('captain')))
    if rec['type'] == 'started':
        return (text(rec.get('address')) and rec.get('verification') in VERIFIED and isinstance(rec.get('why'), str)
                and (rec.get('session') is None or pinned_identity(rec.get('session'))))
    if rec['type'] in ('start-failed', 'start-uncertain'):
        return isinstance(rec.get('why'), str)
    if rec['type'] == 'observed':
        return (type(rec.get('revision')) is int and rec.get('source') in SOURCES and type(rec.get('ok')) is bool
                and (rec.get('value') is None or text(rec.get('value'))))
    if rec['type'] == 'evidence':
        return (text(rec.get('run')) and rec.get('source') in ('attention', 'report') and text(rec.get('digest'))
                and (rec['source'] == 'report' or type(rec.get('seq')) is int and text(rec.get('text'))))
    return True


def well_bound(rec):
    """A verdict's binding to the decision it settles."""
    return (text(rec.get('item')) and type(rec.get('revision')) is int and rec.get('verdict') in VERDICTS
            and (rec.get('change_id') is None or text(rec.get('change_id'))))


def well_scoped(scope):
    """'all', or anchors as anchors() gives them."""
    return scope == 'all' or isinstance(scope, list) and bool(scope) and all(
        isinstance(a, list) and len(a) == 3 and (a[0] == 'terminal' and text(a[1]) and type(a[2]) is int
                                                 or a[0] == 'session' and text(a[1]) and text(a[2]))
        for a in scope)


def pinned_identity(ident):
    """A whole identity as identity() records it: every field unpinned() requires present and typed."""
    if not isinstance(ident, dict) or ident.get('kind') != 'claude':
        return False
    return (all(text(ident.get(k)) for k in ('address', 'cwd', 'session', 'account'))
            and os.path.isabs(ident['cwd'])
            and all(type(ident.get(k)) is int and ident[k] > 0 for k in ('created', 'pid', 'started')))


def text(value):
    return isinstance(value, str) and bool(value)


def records(lenient=False):
    """The ledger's records in order. A torn or garbled line raises Corrupt; lenient (collect and reports, which
    decide nothing) instead stops before a torn last line another process may still be writing."""
    try:
        with open(path(LEDGER), 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return []
    lines = data.split(b'\n')
    if lines[-1] and not lenient:
        raise Corrupt(f'{path(LEDGER)}: the last line is unterminated')
    out = []
    for n, line in enumerate(lines[:-1], 1):
        try:
            rec = json.loads(line)
        except ValueError:
            raise Corrupt(f'{path(LEDGER)}:{n} is not a JSON record') from None
        if not well_formed(rec):
            raise Corrupt(f'{path(LEDGER)}:{n} is not a well-formed record')
        out.append(rec)
    return out


def sync_dir():
    """A created or replaced file survives a crash only once its directory entry is synced too."""
    fd = os.open(STATE, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append(record):
    """One whole line, synced before returning: a caller acts only on what is durable, and a failed or short
    write raises before anything is typed anywhere."""
    record = {**record, 'at': time.time()}
    data = json.dumps(record, ensure_ascii=False).encode() + b'\n'
    created = not os.path.exists(path(LEDGER))
    fd = os.open(path(LEDGER), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        sync_dir()
    return record


def enrolled():
    try:
        with open(path(ENROLLED), encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def write_json(name, data):
    fd, tmp = tempfile.mkstemp(dir=STATE, prefix=f'.{name}.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path(name))
    sync_dir()


def identity(session):
    return {k: session.get(k) for k in IDENTITY}


def find(address):
    """The pinned session in the local terminal at address, with its handle and status, or None: a name hc would
    resolve is not an address."""
    s = described(get(address)) if hc.is_local(address) else None
    return pin(s) if s and s['address'] == address else None


def everyone():
    """Every Claude Code session but this one, pinned where it can be."""
    return [pin(s) for s in sessions()]


def in_space(space, session):
    """Whether session runs in the herdr space with this id or name."""
    return space in (session.get('space_id'), session.get('space'))


def resolve(args):
    """{address: the argument naming it} for the target arguments, an address as given, space:<id or name> replaced
    by the address of the one session in it; one terminal named twice goes once. Dies, so nothing is sent, when a
    space holds none or several. `all` is every live session, and only alone."""
    if 'all' in args:
        if len(args) > 1:
            die('all names every live session, not one to join with other targets; nothing sent', 2)
        found = {s['address']: 'all' for s in everyone()}
        if not found:
            die('all: no live session; nothing sent', 2)
        return found
    live = everyone() if any(a.startswith(SPACE) for a in args) else []
    found = {}
    for arg in args:
        address = arg
        if arg.startswith(SPACE):
            space = arg.removeprefix(SPACE)
            if not space:
                die(f'{arg}: not a space (space:<space id or name>); nothing sent', 2)
            hits = [s for s in live if in_space(space, s)]
            if len(hits) != 1:
                die(f'{arg}: ' + ('; '.join(s['handle'] for s in hits) + ' all match' if hits
                                  else 'no live session matches') + '; nothing sent', 2)
            address = hits[0]['address']
        found.setdefault(address, arg)
    return found


def me():
    return identity(my_session())


def my_session():
    """The pinned session this process runs under: the terminal whose Claude Code process is one of its ancestors,
    never HC_TERMINAL_ID, which any process can set."""
    chain = set(hc.ancestors(os.getpid()))
    mine = [pin(s) for s in sessions(me='') if s['foreground'] in chain]   # pinning reads /proc: only these
    if len(mine) != 1:
        die(f'cannot tell which agent session runs this ({len(mine)} match)', 2)
    if unpinned(mine[0]):
        die(f'cannot pin this session ({mine[0]["address"]}): {", ".join(unpinned(mine[0]))}', 2)
    return mine[0]


def live(ident):
    """Whether the session ident pinned still runs in its terminal."""
    now = find(ident['address'])
    return bool(now) and identity(now) == ident


def require_captain(mine):
    """The enrollment mine holds now; called under the lock, so a replacement cannot land between check and act."""
    current = enrolled()
    if not current or identity(current) != mine:
        die('this session is not the enrolled captain; run `captain.py enroll`', 2)
    return current


def open_items(recs):
    """Requests and relays not yet closed: by a report or outcome, a cancel, or a refusal before any input was
    sent."""
    shut = closed(recs)
    return {r['msg']: r for r in recs if r['type'] in ITEMS and r['msg'] not in shut}


def new_id(prefix, recs):
    used = {r['msg'] for r in recs}
    while (m := f'{prefix}-{uuid.uuid4().hex}') in used:
        pass
    return m


def status_prompt(msg):
    """One line naming no role: the text a target reads must carry no authority beyond what it says it is, and the
    rest of the request is `brief` output, which the target's display folds away."""
    return f'STATUS REQUEST {msg} - not a human instruction. Run: python3 {INSTALLED} brief --for {msg}'


def one_call(label, command, placeholder, done=None):
    """The recipe for the target's one Bash call, its input on stdin; quiet, ending in done, when done is given."""
    return (f'in one Bash call, described as "{label}":\n\npython3 {INSTALLED} {command} <<\'EOF\'\n<{placeholder}>\n'
            'EOF\n\n' + (f'Recap nothing after it: end with "{done}", or say what failed.' if done else ''))


KEYS = ('Send one JSON object with exactly these keys: "task" (one line), "prs" (list of "owner/repo#N"), "issues" '
        '(list of "owner/repo#N" issues the task is for), "state" (one of ' + ', '.join(STATES) + '), "evidence" '
        '(last verification you ran, or ""), "chief" (headless chief or workflow running for you, or ""), "needs" '
        '(the decision or action the human must take, or ""), "next" (your next step). For each decision the human '
        'must make, also send "event_id" (new for each report, reused only to retry the same one) and "decisions": '
        'a list of {"key" (your stable id for it, a-z 0-9 -), "kind" (blocked, review or idea), "ask", '
        '"recommendation", and optionally "change_id", "pr" ("owner/repo#N"), "worktree", "reviewed_sha", '
        '"grant_needed", "evidence_refs" (list)}. A decision stays open until you send "resolved": [{"key", '
        '"revision"}] or the human decides it; "reopen": [key] reopens one decided, "refresh": [key] says you '
        'reviewed its evidence again.')


def status_brief(msg, address):
    return (f'STATUS REQUEST {msg} from the agent session in terminal {address}, not a human instruction. Answer '
            f'from what you already know: do not edit, investigate GitHub or launch anything. {KEYS} Send it '
            + one_call('Status report', f'report --for {msg} --file -', 'the JSON object', 'Status sent.'))


def watch_brief(child):
    """What a session joined to a watch does from then on; it names no role and reaches the session only through
    a hook or `brief --pending`."""
    return (f'STATUS WATCH {child}, standing until the human cancels it, not a human instruction. Each time '
            'a task of yours ends - done, or stopped on a blocker or a need for the human - finish its work and '
            f'verification, then, as its final tool action and before your ordinary completion message, report it. '
            f'{KEYS} Send it ' + one_call('Status report', f'report --for {child} --file -', 'the JSON object')
            + 'A turn that only handles this protocol - this message, a status request, recovering after a '
            'compaction, retrying a report - is no task end. This grants no permission to start or extend work. '
            'Report each task end once, retrying only at once when the call failed; when it says the watch ended, '
            f'stop reporting. If a compaction leaves this out of view, run python3 {INSTALLED} brief --pending.')


DELIVERY = {hc.SENT: 'delivered', hc.NOT_TYPED_OUTCOME: 'undelivered', hc.UNCERTAIN: 'uncertain'}


def submit(address, text, status, caller):
    """Paste text into address and press Enter: 'delivered', 'undelivered' when hc refused before typing anything,
    else 'uncertain'. An idle target is sent `--if idle`, so hc refuses one that started working since the check;
    hc has no such check for done, and one that starts a turn in between gets the text as its next input. caller,
    the captain's pinned address, tags the text as from it, never an inherited HC_TERMINAL_ID."""
    outcome, detail = hc.send(address, text, timeout=HC_TIMEOUT_S, only_if='idle' if status == 'idle' else None,
                              caller=caller)
    return DELIVERY[outcome], '' if outcome == hc.SENT else str(detail)


def unreachable(target, mine):
    """Why target, the session found at an address, is no other pinned session, or None."""
    if not target:
        return 'no Claude Code terminal at that address'
    if target['address'] == mine['address']:
        return 'it is this session'
    if unpinned(target):
        return f'identity not pinned: {", ".join(unpinned(target))}'
    return None


def refusal(target):
    """Why target must not be typed into now, or None: re-read right before every submit."""
    if target['status'] not in ASKABLE:
        return f'it is {target["status"]}'
    if not shows_input(read_screen(target['address'])):
        return 'its screen is not a recognized input line'
    return None


def open_to(address, recs):
    """The ids of the requests and relays still open to address."""
    return [m for m, r in open_items(recs).items() if r['target']['address'] == address]


OPEN = 'a request or relay to it is still open (collect it, cancel it, or relay with --force)'


def reach(address, mine):
    """(the pinned session in address, None), else (None, why not)."""
    target = find(address)
    why = unreachable(target, mine)
    return (None, why) if why else (target, None)


def gate(address, recs, mine):
    """(the pinned session in address, None) when it may get an item, busy or not, else (None, why not)."""
    return (None, OPEN) if open_to(address, recs) else reach(address, mine)


def sendable(address, recs, mine):
    """(the pinned session in address, None) when it may be typed into now, else (None, why not)."""
    target, why = gate(address, recs, mine)
    why = why or refusal(target)
    return (None, why) if why else (target, None)


def config_dir(account_name):
    """The config dir a session under account_name keeps its transcripts in, or None when it cannot be told."""
    if account_name in CONFIG_DIRS:
        return CONFIG_DIRS[account_name] or os.path.expanduser(DEFAULT_CONFIG)
    config = os.path.expanduser(account_name.removeprefix('config:'))
    return config if account_name.startswith('config:') and os.path.isabs(config) else None


def transcripts(config, session):
    return glob.glob(os.path.join(glob.escape(config), 'projects', '*', glob.escape(session) + '.jsonl'))


def content(entry):
    """A transcript entry's message content, None for any other shape."""
    message = entry.get('message') if isinstance(entry, dict) else None
    return message.get('content') if isinstance(message, dict) else None


def new_turn(entry):
    """Whether a transcript entry starts a turn: the human's prompt, a task notification or a compaction summary,
    not a tool result or text injected into a running turn; an entry of another shape counts as one."""
    if not isinstance(entry, dict):
        return True
    blocks = content(entry)
    results = isinstance(blocks, list) and blocks and all(
        isinstance(b, dict) and b.get('type') == 'tool_result' for b in blocks)
    return entry.get('type') == 'user' and not entry.get('isMeta') and not results


def anchor(entry, ref, member):
    """Whether a transcript entry is what a report's turn starts from: a watch member's own report call returning,
    its output naming the report; a request's prompt as typed."""
    if member:
        blocks = content(entry)
        return (isinstance(entry, dict) and entry.get('type') == 'user' and isinstance(blocks, list)
                and len(blocks) == 1 and isinstance(blocks[0], dict) and blocks[0].get('type') == 'tool_result'
                and not blocks[0].get('is_error') and (plain_text(blocks[0].get('content')) or '').strip()
                == f'recorded {ref}')
    words = plain_text(content(entry))
    return bool(human_input(entry) and words and unwrapped(words) == status_prompt(ref))


def unchanged(target, recs):
    """(target's last report, the id `reports` gives it by) when its transcript starts no turn after the request that
    report answered or, for a watch member's, after the report call, so asking again would only repeat it; None, so
    the target is asked, whenever that cannot be shown."""
    last = last_report(target, recs)
    if not last:
        return None
    member = asked(recs)[last['msg']]['type'] == 'joined'
    # a member reports many times: its report call's output names this one by ledger index, `reports` as well
    ref = f'{last["msg"]}:{next(i for i, r in enumerate(recs) if r is last)}' if member else last['msg']
    config = config_dir(target['account'])
    found = transcripts(config, target['session']) if config else []
    if len(found) != 1:
        return None
    try:
        with open(found[0], 'rb') as f:
            data = f.read()
    except OSError:
        return None
    at = data.find((f'recorded {ref}' if member else ref).encode())
    *lines, torn = data[data.rfind(b'\n', 0, at) + 1:].split(b'\n') if at >= 0 else [b'']
    if not lines or torn:   # not there, or a partly written line that may be a new turn
        return None
    try:
        entries = (json.loads(line) for line in lines)   # lazily: the first new turn ends the walk
        for entry in entries:   # a record of the API request that carried it may quote it first
            if anchor(entry, ref, member):
                return None if any(new_turn(e) for e in entries) else (last, ref)
            if new_turn(entry):
                return None   # a summary or tool output quoting the id is not the request
        return None
    except ValueError:
        return None


def ask(address, mine, force=False):
    """One status request to address, recorded before it is typed: prints the msg id, or why it was not sent."""
    with locked():
        captain = require_captain(mine)
        recs = records()
        target, why = sendable(address, recs, mine)
        if why:
            return f'{address}: refused, {why}'
        found = not force and unchanged(target, recs)
        if found:
            last, ref = found
            at = time.strftime('%H:%M', time.localtime(last['at']))
            return f'{address}: not asked, nothing new since its report {ref} at {at}'
        msg = new_id('m', recs)
        return deliver('request', msg, captain, target, status_prompt(msg))


def item(kind, msg, captain, target, **fields):
    """The ledger record of a request or relay to target."""
    return {'type': kind, 'msg': msg, 'captain': captain, 'target': identity(target), 'handle': target['handle'],
            **fields}


def deliver(kind, msg, captain, target, prompt, **fields):
    """Record the item, then type its prompt and record how that went: a crash between leaves the item open, never
    typed unrecorded."""
    append(item(kind, msg, captain, target, **fields))
    return type_prompt(msg, target, prompt, captain)


def type_prompt(msg, target, prompt, captain):
    """Type msg's prompt into target, record how that went, and return the line saying so."""
    outcome, err = submit(target['address'], prompt, target['status'], captain['address'])
    return record_delivery(msg, target['address'], outcome, err)


def record_delivery(msg, address, outcome, err=None):
    """Record msg's delivery outcome and return the line saying so."""
    append({'type': outcome, 'msg': msg, 'error': err} if err else {'type': outcome, 'msg': msg})
    return f'{address}: {msg} {outcome}' + (f' ({err})' if err else '')


def shaped(obj, required, optional):
    """Why obj is not a dict of exactly the required keys and any of the optional ones, each of its type, or None."""
    if not isinstance(obj, dict) or not required.keys() <= obj.keys() <= required.keys() | optional.keys():
        return f'keys must be exactly {", ".join(required)}, and optionally {", ".join(optional)}'
    for key, value in obj.items():
        kind = required.get(key) or optional[key]
        if not isinstance(value, kind):
            return f'"{key}" must be a {kind.__name__}'
    return None


def validate(report):
    why = shaped(report, REPORT, OPTIONAL)
    if why:
        return why
    for key in ('prs', 'issues'):
        if not all(isinstance(p, str) and PR_REF.fullmatch(p) for p in report.get(key, [])):
            return f'"{key}" must hold "owner/repo#N" strings'
    if report['state'] not in STATES:
        return f'"state" must be one of {", ".join(STATES)}'
    return validate_ops(report)


def validate_ops(report):
    """Why the decision operations of report are malformed or contradict each other, or None."""
    if 'event_id' in report and not EVENT_ID.fullmatch(report['event_id']):
        return '"event_id" must be 1-64 of A-Z a-z 0-9 . _ : -'
    if not report.keys() & set(OPS):
        return None
    if any(report.get(op) for op in OPS) and 'event_id' not in report:
        return '"event_id" is required with decisions, reopen, refresh or resolved'
    decided = report.get('decisions', [])
    if len(decided) > DECISIONS_MAX:
        return f'"decisions" holds at most {DECISIONS_MAX}'
    for d in decided:
        why = validate_decision(d)
        if why:
            return f'a decision {why}'
    keys = [d['key'] for d in decided]
    for op in ('reopen', 'refresh'):
        named = report.get(op, [])
        if not all(isinstance(k, str) and KEY.fullmatch(k) for k in named) or len(set(named)) != len(named):
            return f'"{op}" must hold distinct decision keys'
    resolved = report.get('resolved', [])
    if not all(isinstance(r, dict) and set(r) == {'key', 'revision'} and isinstance(r['key'], str)
               and KEY.fullmatch(r['key']) and type(r['revision']) is int and r['revision'] > 0 for r in resolved):
        return '"resolved" must hold {"key", "revision"} objects'
    closing = [r['key'] for r in resolved]
    for name, group in (('decisions', keys), ('resolved', closing)):
        if len(set(group)) != len(group):
            return f'"{name}" names a key twice'
    reopen, refresh = set(report.get('reopen', [])), set(report.get('refresh', []))
    if reopen & refresh or set(closing) & (reopen | refresh | set(keys)):
        return 'one key is reopened and refreshed, or resolved and changed, in the same report'
    return None


def validate_decision(d):
    why = shaped(d, DECISION, DECISION_OPTIONAL)
    if why:
        return why
    if not KEY.fullmatch(d['key']):
        return '"key" must be 1-40 of a-z 0-9 -, starting with a letter or digit'
    if d['kind'] not in KINDS:
        return f'"kind" must be one of {", ".join(KINDS)}'
    if not d['ask'].strip():
        return '"ask" must say what the human decides'
    if 'pr' in d and not PR_REF.fullmatch(d['pr']):
        return '"pr" must be "owner/repo#N"'
    if 'reviewed_sha' in d and not SHA.fullmatch(d['reviewed_sha']):
        return '"reviewed_sha" must be a lowercase hex SHA'
    if 'worktree' in d and not os.path.isabs(d['worktree']):
        return '"worktree" must be an absolute path'
    if 'change_id' in d and not d['change_id'].strip():
        return '"change_id" must not be empty'
    if not all(text(e) for e in d.get('evidence_refs', [])):
        return '"evidence_refs" must hold non-empty strings'
    return None


def read_text(file):
    """A target's file, or stdin for '-', at most REPORT_MAX bytes of UTF-8."""
    if file == '-':
        data, file = sys.stdin.buffer.read(REPORT_MAX + 1), 'stdin'
    else:
        with open(file, 'rb') as f:
            data = f.read(REPORT_MAX + 1)
    if len(data) > REPORT_MAX:
        die(f'{file} is over {REPORT_MAX} bytes', 4)
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as e:
        die(f'{file} is not UTF-8: {e}', 4)


def first(recs, kind, msg):
    return next((r for r in recs if r['type'] == kind and r['msg'] == msg), None)


def addressed(kind, msg, recs, mine):
    """The request or relay msg, which must have been sent to mine, the session running this, as pinned then. Called
    under the lock: a session that replaced the target in its terminal records its refusal of an open, unaccepted one."""
    item = first(recs, kind, msg)
    if not item:
        die(f'no {kind} {msg}', 3)
    # hc types by terminal: a session that replaced the target after the send's check gets the prompt, not the text
    if (item['target'] != mine and item['target']['address'] == mine['address'] and msg in open_items(recs)
            and not first(recs, 'accepted', msg)):
        append({'type': 'refused', 'msg': msg, 'by': mine})
        die(f'{kind} {msg} was sent to the session this terminal ran before, not this one: recorded as refused; '
            'nothing here to act on', 2)
    if item['target'] != mine:
        die(f'{kind} {msg} was sent to {item["target"]["address"]}, not this session as pinned', 2)
    return item


def requested(msg, recs, mine):
    """Request msg, which must have been sent to mine by the session still enrolled."""
    request = addressed('request', msg, recs, mine)
    if request['captain'] != enrolled():
        die(f'request {msg} came from an agent session that is no longer enrolled', 2)
    return request


def brief(msg):
    """What request msg asks, printed for the session it was sent to while it is open; writes nothing but the
    refusal of a session that replaced that one in its terminal (addressed)."""
    with locked():
        mine = me()
        recs = records()
        request = requested(msg, recs, mine)
        if msg not in open_items(recs):
            die(f'request {msg} is closed', 2)
    print(status_brief(msg, request['captain']['address']))
    return 0


def session_of(ident):
    """Whose an identity is: its (account, session), or ('run', path) for a chief run, which has neither."""
    return ('run', ident['run']) if 'run' in ident else (ident.get('account'), ident.get('session'))


def earlier_event(recs, source, payload):
    """The ref, `<msg>:<index>`, of the report this session sent before under payload's event_id, through any request
    or watch, or None; dies when that report differs, so a reused id never applies other operations."""
    event = payload.get('event_id')
    if not event:
        return None
    sources = asked(recs)
    me = session_of(source['target'])
    for i, r in enumerate(recs):
        owner = sources.get(r['msg']) if r['type'] == 'report' else None
        if owner and r['report'].get('event_id') == event and session_of(owner['target']) == me:
            if r['report'] != payload:
                die(f'event_id {event} was sent before with a different report', 2)
            return f'{r["msg"]}:{i}'
    return None


def reported_from(recs, source):
    """The target source's session last reported from, through any request or watch, or None: its items' owner."""
    sources = asked(recs)
    me = session_of(source['target'])
    return next((sources[r['msg']]['target'] for r in reversed(recs)
                 if r['type'] == 'report' and r['msg'] in sources and session_of(sources[r['msg']]['target']) == me),
                None)


def unknown_keys(recs, source, payload):
    """The keys payload reopens, refreshes or resolves that are no decision of this session's, nor in payload."""
    named = {*payload.get('reopen', []), *payload.get('refresh', []), *(r['key'] for r in payload.get('resolved', []))}
    named -= {d['key'] for d in payload.get('decisions', [])}
    if not named:
        return []
    owner = session_of(source['target'])
    return sorted(named - {item['key'] for item in decisions(recs).values() if session_of(item['owner']) == owner})


def report(msg, file):
    """A target answers msg: only the session it was sent to, as pinned then, only while it is open, only once. A
    report repeating an event_id this session sent is the same report again, its operations applied once; through a
    watch it is recorded again only from where the session moved since, so its items follow it."""
    try:
        payload = json.loads(read_text(file))
    except ValueError as e:
        die(f'the report is not JSON: {e}', 4)
    problem = validate(payload)
    if problem:
        die(f'report rejected: {problem}', 4)
    with locked():
        mine = me()   # under the lock: a restart while waiting for it must not pass
        recs = records()
        member = first(recs, 'joined', msg)
        source = member or first(recs, 'request', msg)
        unknown = unknown_keys(recs, source, payload) if source else []
        if unknown:
            die(f'report rejected: no decision {", ".join(unknown)} of this session to reopen, refresh or resolve', 4)
        if member:   # a watch's member reports each task end: every report is kept, none closes it
            member_watch(member, recs, mine)
            again = earlier_event(recs, member, payload)
            if again is not None and reported_from(recs, member) == member['target']:
                print(f'recorded {again}')
                return 0
            append({'type': 'report', 'msg': msg, 'report': payload})
            print(f'recorded {msg}:{len(recs)}')   # every report is kept, so a blind retry would duplicate one
            return 0
        requested(msg, recs, mine)
        # dies on a reused event_id; an identical one still answers this request, its operations applied once
        earlier_event(recs, source, payload)
        earlier = first(recs, 'report', msg)
        if earlier:
            if earlier['report'] == payload:
                return 0
            die(f'request {msg} already has a different report', 2)
        if msg not in open_items(recs):
            die(f'request {msg} is closed', 2)
        append({'type': 'report', 'msg': msg, 'report': payload})
    return 0


def one_line(text):
    """A target's free text on one line: it may hold tabs or newlines that would break a row."""
    return ' '.join(text.split())


EVENTS = {'cancel': 'cancelled', 'undelivered': 'undelivered', 'accepted': 'accepted', 'held': 'held',
          'refused': 'refused by the session that replaced its target'}


def event(item, rec):
    """The collect line for rec, a record on item, or None for one the human need not hear about."""
    if rec['type'] == 'report':   # bounded, so a display cutting long lines cannot cut it: `reports` has the rest
        r = rec['report']
        return f'reported {item["msg"]} · {r["state"]}' + (' · needs you' if r['needs'] else '')
    tail = f'{item["msg"]} · {item["handle"]}'
    if rec['type'] == 'outcome':
        note = one_line(rec['note'])
        return f'{rec["result"]} {tail}' + (f' · {note}' if note else '')
    return f'{EVENTS[rec["type"]]} {tail}' if rec['type'] in EVENTS else None


def watch_ends(recs):
    return {r['msg'] for r in recs if r['type'] in WATCH_ENDS}


def closed(recs):
    """The ids of requests, relays, watches and watch members no record can still extend: a report closes a request,
    never a member, which ends with its watch."""
    ends = watch_ends(recs)
    members = {r['msg']: r['watch'] for r in recs if r['type'] == 'joined'}
    done = {r['msg'] for r in recs if r['type'] in CLOSED and r['msg'] not in members}
    return done | ends | {m for m, w in members.items() if w in ends}


def followed(recs, msgs, base):
    """What a collect follows: the ids named and the members of a watch named, joining or not yet; without ids,
    whatever was open after records[:base] and whatever opened after it."""
    if msgs:
        wanted = set(msgs)
    else:
        shut = closed(recs[:base])
        wanted = {r['msg'] for r in recs[:base] if r['type'] in FOLLOWED and r['msg'] not in shut}
        wanted |= {r['msg'] for r in recs[base:] if r['type'] in FOLLOWED}
    return wanted | {r['msg'] for r in recs if r['type'] == 'joined' and r['watch'] in wanted}


def unwatched(recs):
    """The ids of the requests and relays in recs opened while no watch was open: those are collected by id."""
    watches, found = set(), set()
    for r in recs:
        if r['type'] == 'watch':
            watches.add(r['msg'])
        elif r['type'] in WATCH_ENDS:
            watches.discard(r['msg'])
        elif r['type'] in ITEMS and not watches:
            found.add(r['msg'])
    return found


def watch_event(i, rec, items):
    """The collect line for record i on a watch or a member of one, or None."""
    if rec['type'] == 'joined':
        return f'watching {rec["msg"]} · {rec["handle"]}'
    if rec['type'] == 'report':   # its ledger index names this report among the member's many
        r = rec['report']
        return f'reported {rec["msg"]}:{i} · {r["state"]}' + (' · needs you' if r['needs'] else '')
    if rec['type'] == 'watch-end':
        return f'watch ended {rec["msg"]} · {rec["why"]}'
    if rec['type'] == 'cancel' and items[rec['msg']]['type'] == 'watch':
        return f'cancelled {rec["msg"]}'
    return None


def stored(name, recs):
    """The ledger index last stored in name, by whichever captain, or None: none stored, malformed, or past the end
    of recs. A standing watch outlives its captains, so a new one resumes where the last stopped. Each caller reads
    afresh."""
    try:
        with open(path(name), encoding='utf-8') as f:
            mark = json.load(f)
        at = mark['at'] if text(mark['generation']) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at if type(at) is int and 0 <= at <= len(recs) else None


def generation():
    return (enrolled() or {}).get('generation')


def store(name, at, fence):
    """Store ledger index at in name, under the lock the caller holds, while fence is still the enrolled captain's
    generation; never behind what any captain stored already, so a slower reader cannot roll it back, unless that
    is malformed or past the ledger's end."""
    if fence is None or fence != generation():
        return
    if (stored(name, records(lenient=True)) or 0) > at:
        return
    write_json(name, {'generation': fence, 'at': at})


def resumed(name, recs, watch, default):
    """Where a reader without ids starts: the index stored in name; with none, the open watch's own record, so a lost
    cursor loses none of its reports, or else default."""
    at = stored(name, recs)
    if at is None and watch:
        at = recs.index(watch)
    return default if at is None else at


def collect(msgs, wait_ms):
    """Print each followed item's events as they land: a request's or relay's report or outcome, cancellation or
    refusal; a watch's members joining and every report they send, and its end. Exit 0, printing `done`, once none
    is pending; NO_ANSWER when wait_ms lapse first, printing `pending: <ids>`. With ids, every event on them is
    printed. Without, printing resumes where the last collect without ids stopped, whichever captain ran it (COLLECTED,
    stored as this one ends), following what was open there and whatever opened since; with no such cursor, at the
    open watch's start, else at the ledger's end. A cursor from before the open watch skips, until that watch's start,
    the events of requests and relays opened while no watch was open: those were collected by id. What is closed is
    judged on the whole ledger. A strict read under the lock first: a torn ledger must not look like nothing to wait
    for. Every lock wait ends with wait_ms, so the pending line still prints before
    the caller's timeout. A collect belongs to the captain enrolled when it began: once another enrolls, it prints its
    pending line and stores nothing."""
    deadline = time.monotonic() + wait_ms / 1000
    try:
        with locked(wait_ms / 1000):
            recs = records()
            opened = len(recs)
            watch = settle(recs)
            fence = generation()
            start = 0 if msgs else resumed(COLLECTED, recs, watch, opened)
            armed = recs.index(watch) if watch and not msgs else 0
            by_id = unwatched(recs[:armed]) if start < armed else set()
    except Busy:
        print('pending: the ledger lock stayed held', flush=True)
        return NO_ANSWER
    items = {r['msg']: r for r in recs if r['type'] in FOLLOWED}
    unknown = set(msgs) - set(items)
    if unknown:
        die(f'no request, relay, watch or member {", ".join(sorted(unknown))}', NO_ANSWER)
    base = shown = start   # printing starts at shown
    pending = set()
    while True:
        if generation() != fence:   # read after recs: an enrollment precedes anything its captain records
            print(f'pending: {" ".join(sorted(pending))}', flush=True)
            return NO_ANSWER
        items.update((r['msg'], r) for r in recs[shown:] if r['type'] in FOLLOWED)
        following = followed(recs, msgs, base)
        for i in range(shown, len(recs)):
            rec = recs[i]
            item = items.get(rec['msg'])
            if rec['msg'] not in following or not item or (i < armed and rec['msg'] in by_id):
                continue
            line = watch_event(i, rec, items) if item['type'] in WATCHED else event(item, rec)
            if line:
                print(line, flush=True)
        shown = len(recs)
        pending = following - closed(recs)
        lapsed = time.monotonic() >= deadline
        if not msgs and (lapsed or not pending):
            with suppress(Busy), locked(max(0, deadline - time.monotonic())):
                store(COLLECTED, shown, fence)   # unstored, the next collect prints these lines again
        if not pending:
            print('done', flush=True)
            return 0
        if lapsed:
            print(f'pending: {" ".join(sorted(pending))}', flush=True)
            return NO_ANSWER
        time.sleep(POLL_S)
        recs = records(lenient=True)


def reports(msgs=()):
    """The latest report per target terminal, newest first; with msgs, the report answering each of those requests, in
    order, a later report from the same session notwithstanding, and a row naming why for one with none. A watch
    member's id gives its latest report, `<id>:<N>` the one at ledger index N."""
    recs = records(lenient=True)
    requests = asked(recs)
    if msgs:
        answers = {r['msg']: r for r in recs if r['type'] == 'report'}   # a member's latest of many
        return [indexed(m, requests, recs) if ':' in m else row(requests[m], answers[m]) if m in answers
                else unanswered(m, requests, recs) for m in msgs]
    latest = {}   # by ledger position, never the clock: newest first
    for rec in reversed(recs):
        if rec['type'] == 'report':
            req = requests[rec['msg']]
            latest.setdefault(req['target']['address'], (req, rec))
    return [row(req, rep) for req, rep in latest.values()]


def new_reports(emit=lambda rows: None):
    """A row for every report a watch member sent since the enrolled captain's last call, oldest first, each named
    `<member>:<N>` as collect printed it; resumes as collect does (TABLED), from the open watch's start when nothing
    is stored. Under TABLING, so two calls never give one row twice, and stored only once emit has shown the rows:
    rows a failed print lost come again. The ledger lock is released while emit runs, which a slow reader may hold
    up."""
    with locked(name=TABLING):
        with locked():
            recs = records()
            fence = generation()
            watch = open_watch(recs)
            requests, start = asked(recs), resumed(TABLED, recs, watch, len(recs))
        rows = [{**row(requests[r['msg']], r), 'msg': f'{r["msg"]}:{i}'} for i, r in enumerate(recs[start:], start)
                if r['type'] == 'report' and requests[r['msg']]['type'] == 'joined']
        emit(rows)
        with locked():
            store(TABLED, len(recs), fence)
    return rows


def asked(recs):
    """Requests and watch members by id: what a report answers."""
    return {r['msg']: r for r in recs if r['type'] in ('request', 'joined')}


def indexed(ref, requests, recs):
    """The row for `<member>:<N>`, the report at ledger index N, which must be that member's."""
    msg, _, n = ref.partition(':')
    rec = recs[int(n)] if n.isdigit() and int(n) < len(recs) else None
    if not rec or rec['type'] != 'report' or rec['msg'] != msg or msg not in requests:
        return {'msg': ref, 'missing': f'no report of {msg} at {n}'}
    return {**row(requests[msg], rec), 'msg': ref}


def row(request, rep):
    return {'handle': request['handle'], 'msg': request['msg'], **rep['report']}


def unanswered(msg, requests, recs):
    """The row for request msg with no report: the record that closed it, such as a refusal, when one did."""
    if requests.get(msg, {}).get('type') == 'joined':
        return {'handle': requests[msg]['handle'], 'msg': msg,
                'missing': 'its watch ended' if msg in closed(recs) else 'watching, no report received'}
    if msg not in requests:
        return {'msg': msg, 'missing': 'no such request'}
    end = next((r for r in recs if r['msg'] == msg and r['type'] in CLOSED and r['type'] in EVENTS), None)
    if not end:
        return {'msg': msg, 'missing': 'not answered'}
    return {'handle': requests[msg]['handle'], 'msg': msg, 'missing': EVENTS[end['type']]}


def item_id(prefix, *parts):
    return f'{prefix}-' + hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:16]


def new_item(iid, i, source, key, content, legacy=False):
    return {'id': iid, 'key': key, 'owner': source['target'], 'handle': source['handle'], 'content': content,
            'change_id': content.get('change_id') or content.get('pr'), 'revision': 1, 'state': 'open',
            'closure': None, 'opened': i, 'updated': i, 'legacy': legacy, 'observations': {}, 'verdicts': [],
            'resolved': None}


def revise(item, i, content=None, reopen=False):
    item['revision'] += 1
    item['updated'] = i
    item['observations'] = {}   # what was observed is of the revision before
    if content is not None:
        item['content'] = content
        item['change_id'] = content.get('change_id') or content.get('pr')
    if reopen:
        item['state'], item['closure'] = 'open', None


def close(item, i, why, by=None):
    if item['state'] == 'open':
        item['state'], item['closure'] = 'closed', {'why': why, 'at': i, 'revision': item['revision'], 'by': by}
        item['updated'] = i


def unclose(item, i, by):
    """Reopen item, at its latest revision, when by, a relayed verdict, closed it and its session resolved neither
    that revision nor a later one since: its target never acted on the verdict."""
    closure, resolved = item['closure'], item['resolved']
    if closure and closure['by'] == by and (resolved is None or resolved < closure['revision']):
        item['state'], item['closure'], item['updated'] = 'open', None, i


def apply_report(found, i, source, payload, needs_of):
    """Fold one report's decision operations into found. The owner is the reporting session's account and session,
    never a payload field. needs_of holds each session's needs item as its last report named it."""
    def did(key):
        return item_id('d', *session_of(source['target']), key)

    ids = {}
    for d in payload.get('decisions', []):
        iid = ids[d['key']] = did(d['key'])
        item = found.get(iid)
        if not item:
            found[iid] = new_item(iid, i, source, d['key'], d)
            continue
        reopen = d['key'] in payload.get('reopen', [])
        if d != item['content'] or reopen or d['key'] in payload.get('refresh', []):
            revise(item, i, d, reopen)
    for op in ('reopen', 'refresh'):
        for key in payload.get(op, []):
            item = found.get(did(key))
            if item and key not in ids:
                revise(item, i, reopen=op == 'reopen')
    for r in payload.get('resolved', []):
        item = found.get(did(r['key']))
        if item and item['revision'] == r['revision']:
            item['resolved'] = r['revision']
            close(item, i, 'resolved')
    needs(found, i, source, payload, needs_of)


def needs(found, i, source, payload, needs_of):
    """A report's bare needs, with no decisions, as an item: the session's current needs, superseding the item its
    last report named. Text superseded before reopens; text the human cancelled stays closed."""
    session = session_of(source['target'])
    named = item_id('n', *session, payload['needs']) if payload['needs'] and 'decisions' not in payload else None
    current = needs_of.get(session)
    if current and current != named:
        close(found[current], i, 'superseded')   # a no-op on one already closed, a cancelled one included
    item = found.get(named)
    if named and not item:
        found[named] = new_item(named, i, source, None, {'kind': 'blocked', 'ask': payload['needs']}, legacy=True)
    elif item and (item['closure'] or {}).get('why') == 'superseded':
        revise(item, i, reopen=True)
    needs_of[session] = named


def decisions(recs):
    """Every decision item the ledger holds, by id in opening order: what sessions reported, as revised, resolved,
    reopened or cancelled since. An event_id a session sent before is not applied again, whichever request or
    watch carried it. Every report, a repeated event's too, says where its session runs now: its items follow it,
    relay targets never."""
    sources = asked(recs)
    found, seen, reports, latest, bound, accepted, needs_of = {}, set(), {}, {}, {}, set(), {}
    for i, r in enumerate(recs):
        if r['type'] == 'report' and r['msg'] in sources:
            source, payload = sources[r['msg']], r['report']
            latest[session_of(source['target'])] = source
            event = payload.get('event_id')
            if event:
                ident = (*session_of(source['target']), event)
                if ident in seen:
                    continue
                seen.add(ident)
            apply_report(found, i, source, payload, needs_of)
        elif r['type'] == 'evidence':
            apply_evidence(found, reports, i, r)
        elif r['type'] in ('relay', 'verdict') and r.get('item') in found:
            linked = found[r['item']]
            linked['verdicts'].append({'msg': r['msg'], 'via': r['type'], 'verdict': r['verdict'],
                                       'revision': r['revision'], 'change_id': r.get('change_id'), 'at': i})
            if linked['revision'] == r['revision']:
                close(linked, i, 'decided', r['msg'])
            if r['type'] == 'relay':
                bound[r['msg']] = linked
        elif r['type'] == 'accepted':
            accepted.add(r['msg'])
        elif r.get('msg') in bound and r['msg'] not in accepted and (
                r['type'] in ('undelivered', 'refused', 'cancel')
                or r['type'] == 'outcome' and r['result'] in ('rejected', 'needs-clarification')):
            unclose(bound[r['msg']], i, r['msg'])
        elif r['type'] == 'observed' and r['msg'] in found and found[r['msg']]['revision'] == r['revision']:
            found[r['msg']]['observations'][r['source']] = {'ok': r['ok'], 'value': r['value'], 'at': i}
        elif r['type'] == 'cancel' and r['msg'] in found:
            item = found[r['msg']]
            if (item['closure'] or {}).get('why') == 'superseded':   # cancelled while still open, before supersession
                item['state'] = 'open'
            close(item, i, 'cancelled')
    for item in found.values():
        now = latest.get(session_of(item['owner']))
        if now:
            item['owner'], item['handle'] = now['target'], now['handle']
    return found


def apply_evidence(found, reports, i, rec):
    """A chief's attention line opens an item with no relay target, its run as owner, bound to the run's report.md as
    last recorded; a later report.md digest is a revision of each of that run's items, open or closed, never reopening
    one. reports holds each run's latest digest."""
    run = rec['run']
    if rec['source'] == 'report':
        reports[run] = rec['digest']
        for item in found.values():
            if item['owner'].get('run') == run and item['report'] != rec['digest']:
                revise(item, i)
                item['report'] = rec['digest']
    elif rec['msg'] not in found:
        source = {'target': {'run': run}, 'handle': f'chief {os.path.basename(run)}'}
        content = {'kind': 'blocked', 'ask': rec['text'], 'evidence_refs': [f'{run}/progress.log:{rec["seq"]}']}
        found[rec['msg']] = {**new_item(rec['msg'], i, source, None, content), 'report': reports.get(run)}


def run_dir(run_id):
    """The chief run directory run_id names under CHIEF_RUNS, resolved; dies for anything else."""
    if not RUN_ID.fullmatch(run_id):
        die(f'{run_id}: a chief run is named by its directory under {CHIEF_RUNS}', 2)
    root = os.path.realpath(CHIEF_RUNS)
    run = os.path.realpath(os.path.join(root, run_id))
    if os.path.dirname(run) != root or not os.path.isdir(run):
        die(f'no chief run {run_id} in {CHIEF_RUNS}', 3)
    return run


def run_file(run, name):
    """The bytes of name in run, or None when it is missing; never through a symlink, which could leave CHIEF_RUNS."""
    try:
        fd = os.open(os.path.join(run, name), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        die(f'{name} in {run} cannot be read safely: {e}', 2)
    with os.fdopen(fd, 'rb') as f:
        return f.read()


def progress_lines(run):
    """(seq, text) of each complete progress.log line; a last line still being written waits for the next ingest."""
    data = run_file(run, 'progress.log') or b''
    found = []
    for line in data.split(b'\n')[:-1]:
        m = PROGRESS_LINE.fullmatch(line.decode('utf-8', 'replace'))
        if m:
            found.append((int(m.group(1)), m.group(2)))
    return found


def digest(data):
    return hashlib.sha256(data).hexdigest()


def report_digest(run):
    data = run_file(run, 'report.md')
    return None if data is None else digest(data)


def ingest(run_id, mine):
    """Record a headless chief run's attention lines as decision items and its report.md as their evidence; nothing
    is sent anywhere and no repository, grant or session changes. Each line once by (run, seq); a seq that comes back
    with other text refuses the whole ingest, recording nothing; a shorter progress.log removes nothing."""
    run = run_dir(run_id)
    with locked():   # the files are read under it too, so a slower ingest never records an older snapshot
        require_captain(mine)
        report = report_digest(run)
        recs = records()
        known = {r['seq']: r['text'] for r in recs
                 if r['type'] == 'evidence' and r['run'] == run and r['source'] == 'attention'}
        lines = {}
        for seq, line in progress_lines(run):   # every line, so one rewritten as another kind is caught too
            if lines.setdefault(seq, line) != line or known.get(seq, line) != line:
                die(f'{run_id}: progress.log line {seq} differs from what was read or ingested; nothing recorded', 2)
        new = []
        for seq, line in lines.items():
            if line.startswith(ATTENTION) and seq not in known:
                new.append({'type': 'evidence', 'msg': item_id('c', run, str(seq)), 'run': run, 'source': 'attention',
                            'seq': seq, 'text': line, 'digest': digest(line.encode())})
        last = next((r['digest'] for r in reversed(recs) if r['type'] == 'evidence' and r['run'] == run
                     and r['source'] == 'report'), None)
        changed = report is not None and report != last
        if changed:   # first, so the lines recorded with it are bound to it
            append({'type': 'evidence', 'msg': item_id('r', run), 'run': run, 'source': 'report', 'digest': report})
        for rec in new:
            append(rec)
    print(f'{run_id}: {len(new)} attention line(s) recorded' + (', report.md changed' if changed else ''))
    return 0


def sources(item):
    """What item's evidence is checked against: a chief run's report.md; a session's PR head and worktree HEAD when it
    named a reviewed SHA and them; None when it named no reviewed SHA."""
    if 'run' in item['owner']:
        return ['report']
    content = item['content']
    if 'reviewed_sha' not in content:
        return None
    return [s for s in ('pr', 'worktree') if s in content]


def matches(item, source, value):
    if source == 'report':
        return value == item['report']
    return value is not None and value.startswith(item['content']['reviewed_sha'])


def freshness(item):
    """'stale' on any known mismatch; 'current' when every source was observed to match this revision; else
    'unknown', which a review with no reviewed SHA, or one with no source to check it against, also is; other kinds
    with none are 'not-applicable'."""
    named = sources(item)
    if named is None:
        return 'unknown' if item['content']['kind'] == 'review' else 'not-applicable'
    seen = [(s, item['observations'].get(s)) for s in named]
    if any(o and o['ok'] and not matches(item, s, o['value']) for s, o in seen):
        return 'stale'
    return 'current' if seen and all(o and o['ok'] for _, o in seen) else 'unknown'


def lookup(*args):
    """What captain_read.py prints for args, stripped, or None when it refuses or fails. No timeout here: killing
    the reader would orphan the tool it runs, which its own timer stops."""
    try:
        done = subprocess.run([sys.executable, READER, *args], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def observe(item, look=None):
    """Each of item's sources as it is now: {source, ok, value}, a failed lookup ok false, through look (lookup's
    arguments and result). No lock is held."""
    look = look or lookup
    found = []
    for source in sources(item) or []:
        if source == 'report':
            try:
                value, ok = report_digest(item['owner']['run']), True   # None: no report.md yet
            except (OSError, SystemExit):   # unreadable, or a link run_file() refuses
                value, ok = None, False
        elif source == 'pr':
            repo, number = item['content']['pr'].split('#')
            try:
                value = json.loads(look('gh', 'pr', 'view', number, '-R', repo, '--json', 'headRefOid') or '{}'
                                   ).get('headRefOid')
            except (ValueError, AttributeError):
                value = None
            ok = isinstance(value, str) and bool(SHA.fullmatch(value))
        else:
            value = look('git', '-C', item['content']['worktree'], 'rev-parse', '--verify', 'HEAD')
            ok = bool(value and SHA.fullmatch(value))
        found.append({'source': source, 'ok': ok, 'value': value if ok else None})
    return found


def observe_all(items):
    """[(id, revision, observe(item))] for items, each distinct lookup run once, a few at a time."""
    futures, guard = {}, threading.Lock()
    with ThreadPoolExecutor(4) as lookups, ThreadPoolExecutor(4) as observers:
        def look(*args):
            with guard:
                if args not in futures:
                    futures[args] = lookups.submit(lookup, *args)
            return futures[args].result()
        return [(i['id'], i['revision'], seen) for i, seen in zip(items, observers.map(lambda i: observe(i, look),
                                                                                          items))]


def record_observed(observations):
    """Under the ledger lock, and OBSERVING since each observation began: append what observe() saw of each item
    (id, revision, seen), unless it moved past that revision meanwhile."""
    found = decisions(records())
    for iid, revision, seen in observations:
        if iid in found and found[iid]['revision'] == revision:
            for o in seen:
                append({'type': 'observed', 'msg': iid, 'revision': revision, **o})


def rebound(iid, revision, seen):
    """(decision iid as it is now, why a verdict bound to revision of it is obsolete, or None), after recording seen,
    what observing() saw of it, when there was any. Under the ledger lock and OBSERVING."""
    if seen:
        record_observed([seen])
    linked = decisions(records()).get(iid)
    return linked, obsolete(linked, revision)


def obsolete(linked, revision):
    """Why a verdict bound to revision of linked must not be made, sent or acted on now, or None: the decision
    revised since, that revision resolved by its session, its evidence known stale, or a review whose evidence is not
    confirmed current."""
    if linked is None:
        return 'its decision is gone'
    if linked['revision'] != revision:
        return f'its decision was revised to {linked["revision"]} since revision {revision} was decided'
    if linked['resolved'] == revision:
        return 'its session resolved that revision'
    state = freshness(linked)
    if state == 'stale':
        return 'its evidence changed since it was reviewed'
    if linked['content']['kind'] == 'review' and state != 'current':
        return 'its review evidence could not be confirmed current'
    return None


@contextmanager
def observing(iid):
    """OBSERVING held while it yields (iid, revision, what observe() sees of it now), record_observed's entry for it,
    looked up with the ledger lock free; None for no such item. For no iid, no lock and None."""
    if not iid:
        yield None
        return
    with locked(name=OBSERVING):
        linked = decisions(records(lenient=True)).get(iid)
        yield (iid, linked['revision'], observe(linked)) if linked else None


def observing_relay(action):
    """observing() the decision relay action is bound to, if any: taken before the ledger lock, so read without it."""
    relayed = first(records(lenient=True), 'relay', action)
    return observing(relayed and relayed.get('item'))


def refresh(mine):
    """Observe each item with sources to check that is open, or whose relayed verdict is not yet accepted, outside the
    ledger lock, then record it for the revision it was taken of; all under OBSERVING."""
    with locked(name=OBSERVING):
        with locked():
            require_captain(mine)
            recs = records()
            waiting = open_items(recs).keys() - {r['msg'] for r in recs if r['type'] == 'accepted'}
            wanted = [i for i in decisions(recs).values() if sources(i) and (
                i['state'] == 'open' or any(v['via'] == 'relay' and v['msg'] in waiting for v in i['verdicts']))]
        seen = observe_all(wanted)
        with locked():
            require_captain(mine)
            record_observed(seen)
    return len(seen)


def cancel(msg, mine):
    with locked():
        require_captain(mine)
        recs = records()
        if first(recs, 'watch', msg):
            watch = settle(recs)
            if not watch or watch['msg'] != msg:
                die(f'no open watch {msg}', 3)
            recs.append(append({'type': 'cancel', 'msg': msg}))
            settle(recs)   # removes the marker
            return 0
        item = decisions(recs).get(msg)
        if item:
            if item['state'] != 'open':
                die(f'decision {msg} is closed', 3)
        elif msg not in open_items(recs):
            die(f'no open request, relay, watch or decision {msg}', 3)
        append({'type': 'cancel', 'msg': msg})
    return 0


def plain_text(content):
    """A message's text when it is only text, else None: an image or attachment cannot be relayed verbatim."""
    if isinstance(content, str):
        return content or None
    if isinstance(content, list) and content and all(
            isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str) for b in content):
        return '\n'.join(b['text'] for b in content) or None
    return None


def unwrapped(words):
    """words without the one <hc from=...> wrapper hc puts around what it pastes into an agent, if it has one."""
    wrapped = HC_WRAPPED.fullmatch(words)
    return wrapped.group(1) if wrapped else words


def human_input(entry):
    """Whether a transcript entry is a message typed or queued at the terminal, not hook, skill or task text; one
    hc pasted is typed too, so turn_inputs() tells it apart."""
    origin = entry.get('origin') if isinstance(entry, dict) else None
    return (isinstance(origin, dict) and origin.get('kind') == 'human' and entry.get('type') == 'user'
            and entry.get('promptSource') in ('typed', 'queued') and not entry.get('isMeta')
            and not entry.get('isSidechain'))


def queued_input(entry):
    """The attachment a message the human typed while this session worked becomes once the model reads it, or None:
    it is never a `user` entry. Its enqueue record alone is not one: the model has not seen that message yet."""
    attachment = entry.get('attachment') if isinstance(entry, dict) and entry.get('type') == 'attachment' else None
    if (isinstance(attachment, dict) and attachment.get('type') == 'queued_command'
            and (attachment.get('origin') or {}).get('kind') == 'human'
            and not entry.get('isMeta') and not entry.get('isSidechain')):
        return attachment
    return None


def queued_other(entry):
    """The id of a queued command the model read that is not the human's (a task notification, a Monitor event), or
    None: its absorption hides no message of the human's."""
    attachment = entry.get('attachment') if entry.get('type') == 'attachment' else None
    if not (isinstance(attachment, dict) and attachment.get('type') == 'queued_command') or entry.get('isSidechain'):
        return None
    kind = (attachment.get('origin') or {}).get('kind')
    return attachment.get('source_uuid') if kind and kind != 'human' else None


TURN_ENDS = ('turn_duration', 'compact_boundary')


def turn_inputs(file):
    """(inputs, why): the human's messages in the transcript's current turn as the model read them, in order, each
    (id, text or None when not plain text); why is set when that turn may hold a message not readable yet. A turn
    starts at new_turn() and ends at a completion or compaction record. One opening with an hc tag is another
    program's, hc's paste or made to look like one, and is left out."""
    with open(file, 'rb') as f:
        lines = f.read().split(b'\n')
    if lines[-1]:
        return [], 'the transcript ends in a partly written line that may hold a newer message; retry'
    inputs, read, absorbed, other = [], {}, set(), set()
    for line in lines[:-1]:
        try:
            entry = json.loads(line)
        except ValueError:
            return [], 'a transcript line is not whole JSON, so a message of the human\'s may be hidden in it'
        if not isinstance(entry, dict):
            return [], 'a transcript line is not a record, so where a turn starts cannot be told'
        if (new_turn(entry) and not entry.get('isSidechain')
                or entry.get('type') == 'system' and entry.get('subtype') in TURN_ENDS):
            inputs, read, absorbed, other = [], {}, set(), set()
        queued = queued_input(entry)
        if entry.get('type') == 'queue-operation' and entry.get('operation') == 'remove' \
                and entry.get('reason') == 'absorbed_mid_turn':
            absorbed.add(entry.get('commandUuid'))
            continue
        if queued:
            uid, words = queued.get('source_uuid') or entry.get('uuid'), plain_text(queued.get('prompt'))
            if uid in read:
                if read[uid] != words:
                    return [], f'the queued message {uid} is recorded twice with different words'
                continue
            read[uid] = words
        elif human_input(entry):
            uid, words = entry.get('uuid'), plain_text(content(entry))
        else:
            if uid := queued_other(entry):
                other.add(uid)
            continue
        if not (words and HC_TAG.match(words.lstrip())):
            inputs.append((uid, words))
    unread = absorbed - set(read) - other
    if unread:
        ids = ', '.join(sorted(map(str, unread)))
        return [], f'a message the human typed meanwhile ({ids}) is not readable yet; retry'
    return inputs, None


def transcript(captain):
    """The captain's own Claude Code transcript: the human's words are read from it, never passed by the captain."""
    if os.environ.get('CLAUDE_CODE_SESSION_ID') != captain['session']:
        die("relay runs only in the enrolled captain's own Claude Code session", 2)
    found = transcripts(os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or DEFAULT_CONFIG), captain['session'])
    if len(found) != 1:
        die(f"cannot find this session's transcript ({len(found)} match)", 2)
    return found[0]


def bound(captain, entry=None):
    """(id, text): the human's message this turn of the captain acts on, the only one it read this turn, or with entry
    the one of them it names. A turn no message of the human's started (a notification, a compaction) has none."""
    inputs, why = turn_inputs(transcript(captain))
    if why:
        die(why, 2)
    if not inputs:
        die('no message from the human in this turn: relay only what the human asked in it', 3)
    if entry:
        inputs = [i for i in inputs if i[0] == entry]
        if not inputs:
            die(f'{entry} is not a message from the human in this turn', 2)
    elif len(inputs) > 1:
        listed = '\n'.join(f'  {uid}  {one_line(words or "(not plain text)")[:60]}' for uid, words in inputs)
        die(f'the human wrote more than one message this turn; relay again with --entry <id> naming the one to '
            f'relay:\n{listed}', 2)
    uid, words = inputs[0]
    if not text(uid):
        die("the human's message has no id in the transcript, so it cannot be recorded", 2)
    if words is None:
        die(f"the human's message {uid} is not plain text, so it cannot be relayed verbatim", 2)
    if HC_TAG.search(words):   # hc pastes into a half-typed draft: what follows the tag is not the human's
        die(f"the human's message {uid} holds an hc tag, so it may carry another program's words; "
            'ask the human to restate it', 2)
    return uid, words


def named(token, live):
    """The addresses of the live sessions token names: by address, herdr space id or name, or space:<id or name>."""
    if token.startswith(SPACE):
        space = token.removeprefix(SPACE)
        return {s['address'] for s in live if space and in_space(space, s)}
    token = token.casefold()
    return {s['address'] for s in live
            if token in (s.get(k) and s[k].casefold() for k in ('address', 'space_id', 'space'))}


def instruction(words, addresses, live):
    """(text, None): what a relay to addresses sends, words without an opening `[tell] <name>... [to]` whose names
    are exactly addresses; (None, why) when that opening names anyone else or cannot be split from the instruction.
    Without `tell`, a first word is an opening only when it names a live session or is shaped as an address or
    space:, so an instruction that starts any other way is sent as written."""
    tell = TELL.match(words)
    if tell:
        pos = tell.end()
        first = EVERY_WORD.match(words, pos)
        if first:
            return broadcast(words, first, addresses, live)
    else:
        pos = len(words) - len(words.lstrip())
        lead = NAME.match(words, pos)
        if not lead or EVERY_WORD.fullmatch(lead.group()) or not (
                lead.group().startswith(SPACE) or ADDRESS.fullmatch(lead.group()) or named(lead.group(), live)):
            return words, None
    found = set()
    while True:
        name = NAME.match(words, pos)
        if name and EVERY_WORD.fullmatch(name.group()):
            return None, f'"{name.group()}" addresses every session, not one to join with other names'
        hits = named(name.group(), live) if name else set()
        if not hits:
            return None, f'{name.group()!r} names no live session' if name else \
                'it opens with "tell" and no session name follows'
        if len(hits) > 1:
            return None, f'"{name.group()}" names {", ".join(sorted(hits))}, not one session'
        if not hits <= set(addresses):
            return None, (f'"{name.group()}" names {", ".join(sorted(hits - set(addresses)))}, '
                          'not a target of this relay')
        found |= hits
        pos = name.end()
        join = JOIN.match(words, pos)
        following = join and NAME.match(words, join.end())
        # an address or space: is a recipient even when it names no one, never the instruction's first word
        if not (following and (':' in following.group() or ADDRESS.fullmatch(following.group())
                               or EVERY_WORD.fullmatch(following.group())
                               or named(following.group(), live))):
            joiner = join and join.group().strip(' \t\r\n,')
            if joiner:
                return None, f'{joiner!r} after {name.group()!r} may join another name or start the instruction'
            break
        pos = join.end()
    if found != set(addresses):
        return None, f'it addresses {", ".join(sorted(found))}, not every target of this relay'
    to = TO.match(words, pos)
    text = words[to.end():] if to else ''
    if not text.strip():
        return None, 'nothing follows the names'
    return text, None


class Stale(str):
    """A refusal a new relay cures: the sessions changed, not the human's words."""


def broadcast(words, first, addresses, live):
    """instruction() for a message opening `tell all|everyone|every session`, first its opening word."""
    if named(first.group(), live):
        return None, f'"{first.group()}" also names a session; address it by space:<name>, or every session another way'
    every = EVERY_ONE.match(words, first.start())
    if not every:
        return None, (f'it opens with "tell {first.group()}" but not as "all [session|sessions]", "everyone" or '
                      '"every session" followed by "to" or ":"')
    text = words[every.end():]
    if not text.strip():
        return None, 'nothing follows the names'
    alive, targets = {s['address'] for s in live}, set(addresses)
    missing, extra = alive - targets, targets - alive
    if missing or extra or not targets:
        return None, Stale('it addresses every session, and the targets are not exactly the live sessions'
                           + (f'; not targeted: {", ".join(sorted(missing))}' if missing else '')
                           + (f'; not live: {", ".join(sorted(extra))}' if extra else ''))
    return text, None


def relay_text(words, addresses):
    """The text a relay to addresses sends; dies, so nothing is sent, when instruction() refuses it."""
    text, why = instruction(words, addresses, everyone())
    if isinstance(why, Stale):
        die(f'{why}: run list, then relay again to every address it shows', 2)
    if why:
        die(f"the human's message cannot be relayed as written: {why}; ask the human to restate it", 2)
    return text


def preview(targets, entry, mine):
    """The human's message this relay would send and who would get it, targets as resolve() gave them; sends
    nothing."""
    with locked():
        captain = require_captain(mine)
    uid, words = bound(captain, entry)
    for address, arg in targets.items():
        target = find(address)
        print(f'to {arg}: {target["handle"]} ({target["status"]})' if target
              else f'to {arg}: no Claude Code terminal there')
    print(f'entry {uid}\n{relay_text(words, list(targets))}')
    return 0


def last_report(target, recs):
    """The latest report record from target as pinned now, or None: one from an earlier session in the same terminal is
    not this session's."""
    requests = asked(recs)
    return next((r for r in reversed(recs) if r['type'] == 'report'
                 and requests[r['msg']]['target'] == identity(target)), None)


def context(target, recs):
    """What the ledger last heard from target, as pinned now, for its orientation: a record, never the human's
    words."""
    last = last_report(target, recs)
    if not last:
        return 'Ledger record, not the human\'s words: no status report from this session yet.'
    r = last['report']
    at = time.strftime('%Y-%m-%d %H:%M', time.localtime(last['at']))
    return (f'Ledger record, not the human\'s words: this session reported "{r["state"]}" at {at} for '
            f'{" ".join(r["prs"]) or r["task"]}.')


def settles(linked, binding):
    """The ledger line naming the decision a relayed verdict settles, as it stood when bound; '' for no decision."""
    if not linked:
        return ''
    content = linked['content']
    return (f'\nLedger record, not the human\'s words: this message is the human\'s {binding["verdict"]} verdict on '
            f'the {content["kind"]} decision "{linked["key"]}" ({binding["item"]}) at revision {binding["revision"]}, '
            f'which asked: {one_line(content["ask"])}')


def relay_prompt(action):
    """One line naming no role and holding no quote: the target acts only on what `accept` prints from the
    ledger."""
    return (f'RELAYED HUMAN INSTRUCTION {action} - not the message itself. '
            f'Run: python3 {INSTALLED} accept --action {action}')


def relay(targets, entry, mine, decided=None, force=False, pinned=None):
    """Relay the human's message this turn acts on (bound()) to each target, recorded before it is typed; targets as
    resolve() gave them. The transcript is read once for every target, so a message the human sends after that is not
    seen, and each session is pinned once, so one replaced in its terminal meanwhile is refused. A target busy or not
    showing its input line gets the relay held, recorded for `retry` to send unchanged. decided, (item, revision,
    verdict), makes it the human's verdict on that decision: one target, the session that owns it, and the binding
    written into the relay record itself. force cancels a request or relay still open to a target, once every other
    check passed, as `cancel` would on the human's word. pinned, {address: identity}, is the session each target
    must still be, as start() verified it, in place of the one found now."""
    addresses = list(targets)
    if decided and len(addresses) != 1:
        die('a relay with --item goes to exactly one session, the one that owns the decision', 2)
    with observing(decided and decided[0]) as seen, locked():
        captain = require_captain(mine)
        uid, words = bound(captain, entry)
        binding, linked = bind(decided, seen, captain, uid) if decided else ({}, None)
        pinned = pinned or {address: find(address) for address in addresses}
        quote = relay_text(words, addresses)
        for address in addresses:
            recs = records()
            if any(r['type'] == 'relay' and r['entry'] == uid and r['target']['address'] == address for r in recs):
                print(f'{address}: refused, this message was relayed to it already', flush=True)
                continue
            stale = open_to(address, recs)
            target, why = (None, OPEN) if stale and not force else reach(address, mine)
            if not why and (not pinned[address] or identity(target) != identity(pinned[address])):
                why = 'its session changed during this relay; relay again'
            if why:
                print(f'{address}: refused, {why}', flush=True)
                continue
            if linked and session_of(linked['owner']) != session_of(target):
                print(f'{address}: refused, the decision {binding["item"]} is not this session\'s', flush=True)
                continue
            for msg in stale:
                append({'type': 'cancel', 'msg': msg})
                print(f'{address}: cancelled {msg}', flush=True)
            action = new_id('a', recs)
            fields = {'entry': uid, 'quote': quote, 'context': context(target, recs) + settles(linked, binding),
                      **binding}
            busy = refusal(target)
            if busy:
                append(item('relay', action, captain, target, **fields))
                append({'type': 'held', 'msg': action, 'why': busy})
                print(f'{address}: {action} held, {busy}; `relay --retry {action}` sends it once it is idle or done '
                      'with its input line shown',
                      flush=True)
                continue
            print(deliver('relay', action, captain, target, relay_prompt(action), **fields), flush=True)
    return 0


def bind(decided, seen, captain, uid):
    """(the fields binding a verdict to its decision, the decision as it is now), after recording what was just observed
    of it. The same verdict from the same message again prints its receipt and exits, recording nothing; dies on
    another verdict or revision from that message, a report's bare needs, a decision not open at that revision, and
    evidence stale or unconfirmed."""
    iid, revision, verdict = decided
    recs = records()
    linked = decisions(recs).get(iid)
    if not linked:
        die(f'no decision {iid}', 3)
    for v in linked['verdicts']:
        rec = first(recs, v['via'], v['msg'])
        if session_of(rec['captain']) == session_of(captain) and rec['entry'] == uid:
            if (rec['verdict'], rec['revision']) != (verdict, revision):
                die(f'{iid} already has a {rec["verdict"]} verdict on revision {rec["revision"]} from this message', 2)
            print(f'{iid}: {v["msg"]} recorded already')
            raise SystemExit(0)
    if linked['legacy']:
        die(f'{iid} is a report\'s needs, not a decision: it takes no verdict', 2)
    linked, why = rebound(iid, revision, seen)
    if linked['state'] != 'open':
        closure = linked['closure']
        die(f'decision {iid} is closed ({closure["why"]}' + (f' by {closure["by"]})' if closure['by'] else ')'), 2)
    if why:
        die(f'decision {iid}: {why}; nothing recorded', 2)
    sha = {'reviewed_sha': linked['content']['reviewed_sha']} if 'reviewed_sha' in linked['content'] else {}
    binding = {'item': iid, 'revision': revision, 'verdict': verdict, 'change_id': linked['change_id'], **sha}
    return binding, linked


def verdict(decided, entry, mine):
    """Record the human's verdict on a decision without sending anything: bound to the human's message this turn,
    never to words a caller supplies."""
    with observing(decided[0]) as seen, locked():
        captain = require_captain(mine)
        if not live(mine):
            die('this captain session no longer runs in its terminal', 2)
        uid, words = bound(captain, entry)
        binding, _ = bind(decided, seen, captain, uid)
        msg = new_id('v', records())
        append({'type': 'verdict', 'msg': msg, 'captain': captain, 'entry': uid, 'quote': words, **binding})
    print(f'{binding["item"]}: {msg} recorded, {binding["verdict"]}')
    return 0


def checkout_root(arg):
    """The canonical top level of the git checkout or worktree arg names: an absolute or ~/ path, since nothing
    anchors a relative one, to an existing directory that is its work tree's top level. Dies otherwise."""
    if not (arg.startswith('/') or arg.startswith('~/')):
        die(f'{arg}: name the checkout by an absolute or ~/ path; nothing started', 2)
    try:
        root = os.path.realpath(os.path.expanduser(arg), strict=True)
    except OSError:
        die(f'{arg}: no such directory; nothing started', 2)
    if not os.path.isdir(root):
        die(f'{arg}: not a directory; nothing started', 2)
    try:
        found = captain_read.plain(['git', *captain_read.HARDEN, '-C', root], captain_read.environment('git'),
                                   'rev-parse', '--path-format=absolute', '--show-toplevel')
    except (subprocess.SubprocessError, OSError) as e:
        die(f'{arg}: git could not be asked ({e}); nothing started', 2)
    top = os.fsdecode(found.stdout).strip()
    if found.returncode or not top:
        die(f'{arg}: not a git checkout with a work tree; nothing started', 2)
    if os.path.realpath(top) != root:
        die(f'{arg}: inside the checkout {top}, not its top level; nothing started', 2)
    return root


def launcher(account):
    """The argv that starts Claude Code under account, each program as PATH finds it now; dies when one is missing,
    rather than run another."""
    if account not in LAUNCHERS:
        die(f'{account}: not an account ({", ".join(LAUNCHERS)}); nothing started', 2)
    found = {p: shutil.which(p) for p in LAUNCHERS[account]}
    missing = [p for p, f in found.items() if not f]
    if missing:
        die(f'{missing[0]} is not on PATH, so {account} cannot be started; nothing started', 2)
    return [found['env'], '-u', 'CLAUDE_CONFIG_DIR', found['claude']] if account == 'main' else [found['claude-ada']]


def herdr_spaces():
    """The spaces `herdr-app ls` shows, each with its tabs; dies when the app does not answer."""
    exe = shutil.which('herdr-app')
    if not exe:
        die('herdr-app is not on PATH, so no space can be named for the session; nothing started', 2)
    try:
        out = subprocess.run([exe, 'ls'], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                             timeout=HC_TIMEOUT_S)
        spaces = json.loads(out.stdout).get('spaces') if out.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError):
        spaces = None
    if not isinstance(spaces, list) or not all(isinstance(sp, dict) for sp in spaces):
        die('herdr-app ls gave no list of spaces (is the Herdr window open?); nothing started', 2)
    return spaces


def placement(root, space, tab, spaces):
    """The id of the space for the session: the one named by exact id, else by its one exact name, else the one local
    space whose folder is root; a tab must be one of that space's."""
    def named(sp):
        return f'{sp.get("name")!r} ({sp.get("id")})'
    if space:
        hits = [sp for sp in spaces if sp.get('id') == space] or [sp for sp in spaces if sp.get('name') == space]
        what = f'space {space}'
    else:
        hits = [sp for sp in spaces if not sp.get('machine') and isinstance(sp.get('folder'), str)
                and canonical(sp['folder']) == root]
        what = f'a space whose folder is {root}'
    if len(hits) != 1:
        listed = '; '.join(map(named, hits)) or 'none'
        die(f'{what} matches {listed}: name it with --space <id>; nothing started', 2)
    chosen = hits[0]
    if chosen.get('machine'):
        die(f'space {named(chosen)} is on {chosen["machine"]}: only local sessions start; nothing started', 2)
    if not text(chosen.get('id')):
        die(f'space {named(chosen)} has no id; nothing started', 2)
    if tab and tab not in [t.get('id') for t in chosen.get('tabs') or [] if isinstance(t, dict)]:
        die(f'tab {tab} is not in space {named(chosen)}; nothing started', 2)
    return chosen['id']


def terminal_name(root, account, name):
    """The hc name for the session, <checkout>-<account> by default, never reshaped to fit; dies when it breaks hc's
    rule or a live terminal holds it."""
    name = name or f'{os.path.basename(root)}-{account}'
    if not TERMINAL_NAME.fullmatch(name) or ADDRESS.fullmatch(name):
        die(f'{name}: hc names are 1-32 of a-z 0-9 - _ . from a letter or digit, never an address such as t7; '
            'give one with --name; nothing started', 2)
    holder = get(name)
    if holder and holder.get('state') == 'live':
        die(f'{name}: the live terminal {holder.get("address")} has that name; give another with --name; '
            'nothing started', 2)
    return name


def verify(address, root, account):
    """(verification, why, identity or None) of the session started at address, after one bounded hc wait for it to
    come up: verified once pinned under account in root, pending while it is not yet a Claude Code session or not
    yet pinned, failed when it is gone, finished or another account's or directory's, unchecked when hc could not
    be asked."""
    hc.run('wait', '--json', '--timeout', str(START_WAIT_S), '--', address, timeout=START_WAIT_S + HC_TIMEOUT_S)
    try:
        t = get(address)
    except hc.Error as e:
        return 'unchecked', str(e), None
    if t is None:
        return 'failed', 'its terminal is gone', None
    if t.get('state') != 'live':
        return 'failed', f'its terminal is {t.get("state")}', None
    session = described(t)
    if not session:
        return 'pending', 'not yet a Claude Code session', None
    pin(session)
    if session['account'] and session['account'] != account:
        return 'failed', f'it runs under {session["account"]}, not {account}', None
    if session['cwd'] and session['cwd'] != root:
        return 'failed', f'it runs in {session["cwd"]}, not {root}', None
    missing = unpinned(session)
    if missing:
        return 'pending', f'unpinned: {", ".join(missing)}', None
    return 'verified', '', identity(session)


def start(arg, account, name, space, tab, entry, mine, task=False):
    """Start an interactive Claude Code session in the checkout arg names, in a new hc terminal shown in a Herdr
    space, on the human's message this turn: each check refuses before anything runs, the intent is recorded before
    hc runs, and one message starts a checkout under an account once. Captain never closes what it started. task
    relays that same message to the session as its first task once it is verified, through relay() and its
    receipt."""
    tab = tab or None   # well_formed() refuses an empty tab, which would leave the ledger unreadable
    root = checkout_root(arg)
    argv = launcher(account)
    space_id = placement(root, space, tab, herdr_spaces())
    name = terminal_name(root, account, name)
    with locked():
        captain = require_captain(mine)
        if not live(mine):
            die('this captain session no longer runs in its terminal', 2)
        uid, words = bound(captain, entry)
        recs = records()
        prior = next((r for r in recs if r['type'] == 'start' and identity(r['captain']) == identity(captain)
                      and (r['entry'], r['dir'], r['account']) == (uid, root, account)), None)
        if prior:
            die(f'{prior["msg"]}: this message already started {root} under {account}; nothing started again', 2)
        msg = new_id('s', recs)
        append({'type': 'start', 'msg': msg, 'captain': captain, 'entry': uid, 'quote': words, 'dir': root,
                'account': account, 'name': name, 'space': space_id, 'tab': tab})
    labels = ['--label', f'herdr.space={space_id}', *(['--label', f'herdr.tab={tab}'] if tab else [])]
    outcome, body = hc.launch('--bg', '--keep', '--no-parent', '--name', name, '--cwd', root, *labels, '--', *argv,
                              timeout=HC_TIMEOUT_S)
    if outcome != hc.STARTED or not hc.is_local(body['address']):
        failed = outcome == hc.NOT_STARTED_OUTCOME
        why = body if isinstance(body, str) else f'a terminal elsewhere: {body["address"]}'
        with locked():
            append({'type': 'start-failed' if failed else 'start-uncertain', 'msg': msg, 'why': why})
        die(f'{msg}: hc refused, nothing started: {why}' if failed else
            f'{msg}: hc gave no local terminal address, so a session may have started: {why}; '
            f'check `list` before starting again', 1 if failed else 3)
    address = body['address']
    verification, why, session = verify(address, root, account)
    with locked():
        append({'type': 'started', 'msg': msg, 'address': address, 'created': body.get('created_ms'),
                'verification': verification, 'why': why, 'session': session})
    print(f'{msg}: started {name} ({address}) in {root} under {account}, space {space_id}'
          f'{f", tab {tab}" if tab else ", a new tab"}')
    print({'verified': f'verified: {session and session["session"]}, pid {session and session["pid"]}',
           'pending': f'verification pending ({why}): `list` shows it once it is up',
           'failed': f'VERIFICATION FAILED: {why}; the human closes {address}',
           'unchecked': f'VERIFICATION UNCHECKED: {why}; `list` shows whether it is up'}[verification])
    if task and verification == 'verified':
        relay({address: address}, uid, mine, pinned={address: session})
    elif task:
        print(f'{msg}: first task not relayed, the session is not verified' + (
            '' if verification == 'failed' else f'; once `list` shows it running in {root} under {account}, '
            f'`relay {address} --entry {uid}` sends it'))
    return {'failed': 1, 'unchecked': 3}.get(verification, 0)


def metric(found):
    """Distinct correction verdicts over the distinct changes with a correction or no-correction verdict, each change
    the one its decision named when the verdict was made; decision verdicts and those bound to no change id are
    counted apart, in neither."""
    rated = [(v['change_id'], v['verdict']) for i in found.values() for v in i['verdicts']]
    attributed = [(c, v) for c, v in rated if c]
    corrections = sum(v == 'correction' for _, v in attributed)
    changes = {c for c, v in attributed if v != 'decision'}
    return {'correction_verdicts': corrections, 'changes_with_verdicts': len(changes),
            'ratio': corrections / len(changes) if changes else None,
            'decision_verdicts': sum(v == 'decision' for _, v in attributed),
            'unattributed_verdicts': len(rated) - len(attributed)}


def retry(action, mine):
    """Send a held relay as it was recorded: the same words to the same session, never re-read or re-pinned; one
    bound to a decision first observes its evidence again and closes undelivered once that binding is obsolete."""
    with observing_relay(action) as seen, locked():
        captain = require_captain(mine)
        recs = records()
        relayed = first(recs, 'relay', action)
        if not relayed or not first(recs, 'held', action):
            die(f'no held relay {action}', 3)
        if action not in open_items(recs) or any(r['msg'] == action and r['type'] in DELIVERY.values() for r in recs):
            die(f'relay {action} is no longer held', 2)
        if relayed['captain'] != captain:
            die(f'relay {action} was held by an earlier captain', 2)
        address = relayed['target']['address']
        if first(recs, 'sending', action):   # typed or not, unrecorded: never type it twice
            line = record_delivery(action, address, 'uncertain', 'an earlier retry stopped while sending it')
        else:
            target = find(address)
            stale = 'item' in relayed and rebound(relayed['item'], relayed['revision'], seen)[1]
            if not target or identity(target) != relayed['target']:
                line = record_delivery(action, address, 'undelivered',
                                       'its session was replaced while the relay was held')
            elif stale:
                line = record_delivery(action, address, 'undelivered', stale)
            elif busy := refusal(target):
                line = f'{address}: {action} still held, {busy}'
            else:
                append({'type': 'sending', 'msg': action})
                line = type_prompt(action, target, relay_prompt(action), captain)
    print(line)
    return 0


def instruction_text(header, relayed):
    """The relayed message as a target reads it, under header."""
    return (f'RELAYED HUMAN INSTRUCTION {header}. The human\'s message, verbatim after any opening that named the '
            f'sessions it went to:\n\n{relayed["quote"]}\n\n{relayed["context"]}\n')


def outcome_call(action):
    return one_call('Relay outcome', f'outcome --action {action} --result <{"|".join(RESULTS)}> --note-file -',
                    'the note', 'Outcome recorded.')


def accept(action):
    """A target takes the relay sent to it, once, while the session that sent it is still the live captain; the
    acceptance is durable before the message is printed, so a lost print never allows a second acceptance. One bound
    to a decision is refused while that binding is obsolete, its evidence observed again first."""
    with observing_relay(action) as seen, locked():
        mine = me()
        recs = records()
        relayed = addressed('relay', action, recs, mine)
        current = enrolled()
        if current != relayed['captain'] or not live(identity(current)):
            die(f'relay {action} came from an agent session that is no longer enrolled or no longer runs', 2)
        if first(recs, 'accepted', action):
            die(f'relay {action} was accepted already; `show --action {action}` prints it again', 2)
        if action not in open_items(recs):
            die(f'relay {action} is closed', 2)
        if 'item' in relayed:
            _, why = rebound(relayed['item'], relayed['revision'], seen)
            if why:
                die(f'relay {action} decided {relayed["item"]}, but {why}: record it with `outcome --action '
                    f'{action} --result needs-clarification` and act on nothing', 2)
        append({'type': 'accepted', 'msg': action})
    lead = f'{action} from the agent session in terminal {relayed["captain"]["address"]}, accepted'
    print(instruction_text(lead, relayed)
          + 'It may speak to other sessions too: act only on what it asks of this session, under your own rules and '
          'never wider; a question goes back as the needs-clarification outcome, not a dialog. Then record how it '
          'ended, the note a line or two on what you did or what you need to know, ' + outcome_call(action))
    return 0


def show_relay(action):
    """The relay this session accepted, printed again with nothing recorded, for a session that lost it to a
    compaction: it neither accepts again nor renews or widens anything."""
    mine = me()
    with locked():
        recs = records()
        relayed = addressed('relay', action, recs, mine)
        if not first(recs, 'accepted', action):
            die(f'relay {action} is not accepted; `accept --action {action}` takes it', 2)
    print(instruction_text(f'{action}, accepted earlier and shown again; nothing is recorded', relayed)
          + ('Its outcome is recorded already.' if first(recs, 'outcome', action) else
             'Once it has ended, record its outcome, the note a line or two on what you did or what you need to '
             'know, ' + outcome_call(action)))
    return 0


def outcome(action, result, note):
    """A target says how its relay ended, once; completed or failed only after accepting it. Needs no live
    captain: an outcome is recorded even after the captain was replaced."""
    with locked():
        mine = me()
        recs = records()
        addressed('relay', action, recs, mine)
        earlier = first(recs, 'outcome', action)
        if earlier:
            if (earlier['result'], earlier['note']) == (result, note):
                return 0
            die(f'relay {action} already has a different outcome', 2)
        if action not in open_items(recs):
            die(f'relay {action} is closed', 2)
        if result in ('completed', 'failed') and not first(recs, 'accepted', action):
            die(f'relay {action} was not accepted, so it cannot have {result}', 2)
        append({'type': 'outcome', 'msg': action, 'result': result, 'note': note})
    return 0


def anchors(ident):
    """What a named watch recognizes a session by: its terminal incarnation, and its session in its account, which a
    resume in another terminal keeps."""
    return [['terminal', ident['address'], ident['created']], ['session', ident['account'], ident['session']]]


def captain_state(c):
    """'live', 'ended' or 'unknown' for the captain session c pins, from /proc and its account's session registry, no
    hc: its process gone or restarted, or its session changed (/clear), has ended; what cannot be read is unknown."""
    try:
        start = hc.proc_start(c['pid'])
    except hc.Gone:
        return 'ended'
    except hc.Error:
        return 'unknown'
    if start != c['started']:
        return 'ended' if start else 'unknown'   # None off Linux
    session = registered(c['pid'], start, c['account'])
    return 'unknown' if session is None else 'live' if session == c['session'] else 'ended'


def mark():
    """Synced before the first watch record is appended: a hook skips the ledger only while this is absent."""
    if not marked():
        write_json(MARKER, {})


def marked():
    """Whether a watch may be open; anything but a confirmed absence counts as one."""
    try:
        os.stat(path(MARKER))
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def open_watch(recs):
    """The open watch, or None. A watch stands until cancelled, whatever becomes of the captain that started it; one a
    captain's end closed before watches stood stays closed."""
    ended = watch_ends(recs)
    return next((r for r in reversed(recs) if r['type'] == 'watch' and r['msg'] not in ended), None)


def settle(recs):
    """open_watch(recs). Under the lock: with none open, removes the marker."""
    watch = open_watch(recs)
    if not watch:
        with suppress(FileNotFoundError):
            os.unlink(path(MARKER))
            sync_dir()
    return watch


def scopes(recs, watch):
    return [r['scope'] for r in recs if r['type'] in ('watch', 'watch-extend') and r['msg'] == watch]


def terminals(scopes):
    """The scopes for a human: all, or the terminals named."""
    return 'all' if 'all' in scopes else ' '.join(a[1] for scope in scopes for a in scope if a[0] == 'terminal')


def members(recs, watch):
    return [r for r in recs if r['type'] == 'joined' and r['watch'] == watch]


def covers(watch, recs, ident):
    """Whether watch covers the session ident pins: under `all`, any but a captain's, the one that started it or the
    one enrolled; else one matching an anchor of the sessions named or of one that joined, so a restart or resume of a
    member is covered too."""
    if ident['session'] in (watch['captain']['session'], (enrolled() or {}).get('session')):
        return False
    named = scopes(recs, watch['msg'])
    if 'all' in named:
        return True
    known = {tuple(a) for scope in named for a in scope}
    known |= {tuple(a) for m in members(recs, watch['msg']) for a in anchors(m['target'])}
    return any(tuple(a) in known for a in anchors(ident))


def join(session):
    """(member record, its watch, new) for session, as my_session() pinned it, in the open watch covering it, joining
    it first; (None, why, False) otherwise. Under the lock, with no hc call."""
    mine = identity(session)
    recs = records()
    watch = settle(recs)
    if not watch:
        return None, 'no watch is open', False
    if not covers(watch, recs, mine):   # a member that became the captain is no longer covered
        return None, 'the open watch does not cover this session', False
    member = next((m for m in members(recs, watch['msg']) if m['target'] == mine), None)
    if member:
        return member, watch, False
    return append({'type': 'joined', 'msg': new_id('j', recs), 'watch': watch['msg'], 'target': mine,
                   'handle': session['handle']}), watch, True


def member_watch(member, recs, mine):
    """Dies, so nothing is recorded, unless member is mine and its watch is open; no captain need run."""
    if member['target'] != mine:
        die(f'{member["msg"]} is another session\'s; run `brief --pending` for this one\'s', 2)
    watch = settle(recs)
    if not watch or watch['msg'] != member['watch']:
        die(f'the watch {member["watch"]} ended: stop reporting', 2)
    if not covers(watch, recs, mine):
        die(f'the watch {member["watch"]} no longer covers this session, now a captain: stop reporting', 2)


def pending():
    """A session's own lookup: join the open watch covering it, if any, and print what to do."""
    with locked():
        member, watch, _ = join(my_session())
    print(watch_brief(member['msg']) if member else f'none: {watch}')
    return 0


def hook(payload):
    """What a SessionStart (compact, clear) or Stop hook prints, or None: SessionStart joins or re-briefs a covered
    session; Stop joins one not yet a member and holds the stop once with the brief, the member record its loop
    guard, not stop_hook_active, which another Stop hook may have set. A session my_session() cannot pin, as at
    startup, raises and is left to its first Stop, and one finding the lock held past HOOK_LOCK_S to its next. Only the
    terminal's own session acts: a `claude -p` run from its Bash tool pins as that session but its payload names
    another, as does a registry not yet updated after a /clear, which the next Stop joins."""
    if not marked():
        return None
    session = my_session()   # hc calls: never under the lock
    if payload.get('session_id') != session['session']:
        return None
    with locked(HOOK_LOCK_S):
        member, watch, new = join(session)
    if not member:
        return None
    brief = watch_brief(member['msg'])
    if payload.get('hook_event_name') == 'SessionStart':
        return json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': brief}})
    if payload.get('hook_event_name') == 'Stop' and new:
        return json.dumps({'decision': 'block', 'reason': brief + '\n\nIf this turn ended a task, report it now; '
                           'otherwise stop without reporting.'})
    return None


def arm(targets, mine):
    """Start the enrolled captain's watch, or widen the one open: `all`, or the named sessions. A new watch sets the
    captain's unset cursors to its start, so a session joining before the first collect is not missed, and keeps a set
    one, so the last watch's unseen reports stay due; collect skips the requests and relays opened before it while no
    watch was open."""
    with locked():
        captain = require_captain(mine)
        if targets == ['all']:
            scope = 'all'
        else:
            scope = []
            for address in targets:
                target = find(address)
                why = unreachable(target, mine)
                if why:
                    print(f'{address}: refused, {why}')
                else:
                    scope += anchors(identity(target))
            if not scope:
                die('nothing to watch; nothing recorded', 2)
        recs = records()
        watch = settle(recs)
        mark()
        if watch:
            append({'type': 'watch-extend', 'msg': watch['msg'], 'scope': scope})
            msg = watch['msg']
        else:
            msg = new_id('w', recs)
            for name in (COLLECTED, TABLED):   # kept when set: the last watch's untabled reports stay due
                if stored(name, recs) is None:
                    store(name, len(recs), captain['generation'])
            append({'type': 'watch', 'msg': msg, 'captain': captain, 'scope': scope})
    named = 'every session, new ones too' if scope == 'all' else terminals([scope])
    print(f'watch {msg} {"widened" if watch else "armed"}: {named}')
    return 0


def watches():
    """The open watch and its members, each live, gone or unknown, without changing anything but what settle does; the
    captain named is the one enrolled now, which may be none."""
    with locked():
        recs = records()
        watch = settle(recs)
        current = enrolled()
    if not watch:
        print('no watch is open')
        return 0
    who = f'{current["address"]} {captain_state(current)}' if current else 'none enrolled'
    print(f'{watch["msg"]}\t{terminals(scopes(recs, watch["msg"]))}\tcaptain {who}')
    reported = {r['msg']: i for i, r in enumerate(recs) if r['type'] == 'report'}
    for m in members(recs, watch['msg']):
        last = f'{m["msg"]}:{reported[m["msg"]]}' if m['msg'] in reported else 'no report'
        print(f'  {m["msg"]}\t{m["handle"]}\t{member_state(m)}\t{last}')
    return 0


def member_state(member):
    """live, gone or unknown: whether the session that joined still runs in its terminal, as far as hc can tell."""
    try:
        now = find(member['target']['address'])
    except hc.Error:
        return 'unknown'
    return 'unknown' if now and unpinned(now) else 'live' if now and identity(now) == member['target'] else 'gone'


def relay_states(recs):
    """Each relay's status: pending until something happens to it, then what last did, an outcome by its result."""
    states = {}
    for r in recs:
        if r['type'] == 'relay':
            states[r['msg']] = 'pending'
        elif r.get('msg') in states and r['type'] not in ITEMS:
            states[r['msg']] = r['result'] if r['type'] == 'outcome' else 'cancelled' if r['type'] == 'cancel' \
                else r['type']
    return states


def missing(item):
    """What a decision lacks for the human to settle it from the queue alone."""
    content = item['content']
    gaps = [] if 'recommendation' in content else ['recommendation']
    if content['kind'] == 'review' and 'run' not in item['owner']:
        if 'reviewed_sha' not in content:
            gaps.append('reviewed_sha')
        elif not sources(item):
            gaps.append('pr or worktree')
    return gaps


def queue_row(item, states):
    content, owner = item['content'], item['owner']
    pr = content.get('pr')
    return {'id': item['id'], 'key': item['key'], 'change_id': item['change_id'], 'kind': content['kind'],
            'owner': owner, 'handle': item['handle'], 'relay_target': owner.get('address'),
            'repo': pr.split('#')[0] if pr else None, 'pr': pr, 'worktree': content.get('worktree'),
            'reviewed_sha': content.get('reviewed_sha'),
            'observations': [{'source': s, 'revision': item['revision'], 'ok': o['ok'], 'value': o['value'],
                              'ledger_position': o['at']} for s, o in item['observations'].items()],
            'ask': content['ask'], 'recommendation': content.get('recommendation'),
            'grant_needed': content.get('grant_needed'), 'evidence_refs': content.get('evidence_refs', []),
            'revision': item['revision'], 'state': item['state'], 'freshness': freshness(item),
            'missing': missing(item), 'opened_at': item['opened'], 'updated_at': item['updated'],
            'closure': item['closure'],
            'verdicts': [{**v, 'status': states[v['msg']]} if v['via'] == 'relay' else v for v in item['verdicts']]}


def coverage(recs, watch):
    """Where decisions come from: the open watch's members as hc sees them, the chief runs ingested, and what no
    feed reaches."""
    runs = sorted({r['run'] for r in recs if r['type'] == 'evidence'})
    members_ = [{'msg': m['msg'], 'handle': m['handle'], 'state': member_state(m)}
                for m in (members(recs, watch['msg']) if watch else [])]
    return {'watch': watch and watch['msg'], 'scope': watch and terminals(scopes(recs, watch['msg'])),
            'members': members_, 'chief_runs': runs, 'not_covered': UNCOVERED}


def queue(show_all, as_json, mine=None):
    """The decisions waiting on the human, blocked first, then reviews, then ideas, each in the order it opened;
    with show_all, closed ones too, else only those whose latest verdict is a relay that has not gone through. Reads
    the ledger and hc only; given mine, the captain, observes evidence first, as `refresh` does. Sends nothing."""
    if mine:
        refresh(mine)
    with locked():
        recs = records()
        watch = settle(recs)
    states = relay_states(recs)
    found = decisions(recs)
    rows = [queue_row(i, states) for i in sorted(found.values(), key=lambda i: (KINDS.index(i['content']['kind']),
                                                                               i['opened'], i['id']))]
    if not show_all:
        rows = [r for r in rows
                if r['state'] == 'open' or r['verdicts'] and r['verdicts'][-1].get('status') in UNSETTLED]
    covered = coverage(recs, watch)
    if as_json:
        print(json.dumps({'version': 1, 'ledger_position': len(recs), 'items': rows, 'coverage': covered,
                          'metrics': metric(found)}, indent=2, ensure_ascii=False))
        return 0
    for kind in KINDS:
        listed = [r for r in rows if r['kind'] == kind]
        if listed:
            print(kind)
        for r in listed:
            state = r['state'] if r['state'] == 'open' else f'closed {r["closure"]["why"]}'
            gaps = f'\tmissing {", ".join(r["missing"])}' if r['missing'] else ''
            print(f'  {r["id"]} r{r["revision"]}\t{state}\t{r["freshness"]}\t{r["handle"]}\t{one_line(r["ask"])}'
                  f'\t-> {one_line(r["recommendation"] or "-")}{gaps}')
            for v in r['verdicts']:
                print(f'    {v["verdict"]} r{v["revision"]} {v["msg"]}' + (f' {v["status"]}' if 'status' in v else ''))
    if not rows:
        print('no decision waits on you')
    live = [m['state'] for m in covered['members']]
    watched = (f'watch {covered["watch"]} {covered["scope"]}: '
               + ', '.join(f'{live.count(s)} {s}' for s in ('live', 'gone', 'unknown') if s in live)
               if watch else 'no watch')
    print(f'coverage: {watched}; chief runs: {" ".join(covered["chief_runs"]) or "none"}; {UNCOVERED}')
    m = metric(found)
    if m['changes_with_verdicts']:
        print(f'corrections: {m["correction_verdicts"]} over {m["changes_with_verdicts"]} changes')
    return 0


def enroll(replace):
    """Make this session the one captain; another live one stays unless the human asked to replace it."""
    mine = me()
    with locked():
        current = enrolled()
        was = current and identity(current)
        if was == mine:
            print('already enrolled')
            return 0
        if current and live(was) and not replace:
            die(f'{current["address"]} is the enrolled captain and still runs; pass --replace only on the '
                'human\'s word', 2)
        write_json(ENROLLED, {**mine, 'generation': uuid.uuid4().hex})   # A, B, A again strands A's old
    print(f'enrolled {mine["address"]}' + (f', replacing {current["address"]}' if current else ''))
    return 0


def show_rows(rows, as_json):
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    for r in [] if as_json else rows:
        if 'missing' in r:
            print(f'{r["msg"]}\t{r["missing"]}')
            continue
        prs = ' '.join(r['prs']) or '-'
        print(f'{r["handle"]}\t{one_line(r["task"])}\t{prs}\t{r["state"]}\t{one_line(r["needs"]) or "-"}')
    sys.stdout.flush()   # a failed write raises here, before --new stores its cursor


def decide(parser, required=False):
    """The options binding a relay or verdict to the decision it settles."""
    parser.add_argument('--item', required=required, metavar='ID', help='the decision, as `queue` shows it')
    parser.add_argument('--revision', required=required, type=int, metavar='N', help='its revision `queue` showed')
    parser.add_argument('--verdict', required=required, choices=VERDICTS,
                        help='correction or no-correction of its recommendation, or a decision with no quality call')


def decided(a):
    """(item, revision, verdict) from a relay's options, None without --item; dies on a partial set."""
    given = (a.item, a.revision, a.verdict)
    if all(v is None for v in given):
        return None
    if any(v is None for v in given):
        die('--item, --revision and --verdict go together', 2)
    return given


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    ls = sub.add_parser('list', help='every other Claude Code session in hc, latest status report first, '
                                      'without asking any of them')
    ls.add_argument('--json', action='store_true')
    sub.add_parser('enroll', help='make this session the captain').add_argument('--replace', action='store_true')
    ak = sub.add_parser('ask', help='send each target a status request')
    ak.add_argument('addresses', nargs='+')
    ak.add_argument('--force', action='store_true', help='ask a target even when nothing is new since its report')
    c = sub.add_parser('collect', help='print each report as it lands')
    c.add_argument('msgs', nargs='*')
    c.add_argument('--wait', type=int, default=60000, metavar='MS')
    wt = sub.add_parser('watch', help='have sessions report each task end until the human cancels it')
    wt.add_argument('targets', nargs='+')
    sub.add_parser('watches', help='the open watch and its members')
    rp = sub.add_parser('reports', help="the latest report from each target, or the named requests' reports")
    rp.add_argument('msgs', nargs='*')
    rp.add_argument('--json', action='store_true')
    rp.add_argument('--new', action='store_true', help='every watch report since the last --new')
    sub.add_parser('cancel', help='close a request, relay, watch or decision no answer will close').add_argument('msg')
    qu = sub.add_parser('queue', help='the decisions waiting on the human, sending nothing')
    qu.add_argument('--json', action='store_true')
    qu.add_argument('--all', action='store_true', help='closed decisions too')
    qu.add_argument('--refresh', action='store_true', help='observe each PR head, worktree and chief report first')
    sub.add_parser('ingest', help="record a headless chief run's attention lines as decisions").add_argument(
        '--chief', required=True, metavar='RUN', help=f'its directory name under {CHIEF_RUNS}')
    rl = sub.add_parser('relay', help="relay the human's message this turn acts on")
    rl.add_argument('addresses', nargs='*')
    rl.add_argument('--entry', metavar='ID', help="which of this turn's messages, when the human wrote several")
    rl.add_argument('--dry-run', action='store_true', help='show what would go where, sending nothing')
    rl.add_argument('--retry', metavar='ACTION', help='send a held relay unchanged')
    rl.add_argument('--force', action='store_true', help='cancel a request or relay still open to a target first')
    decide(rl)
    st = sub.add_parser('start', help="start a Claude Code session in a checkout, on the human's message")
    st.add_argument('checkout', help='an absolute or ~/ path to a checkout or worktree top level')
    st.add_argument('account', nargs='?', default='main', help=f'{" or ".join(LAUNCHERS)} (default main)')
    st.add_argument('--name', help='its hc terminal name (default <checkout>-<account>)')
    st.add_argument('--space', help='the Herdr space, by id or exact name (default the one whose folder it is)')
    st.add_argument('--tab', help="a tab id in that space (default a new tab)")
    st.add_argument('--entry', metavar='ID', help="which of this turn's messages, when the human wrote several")
    st.add_argument('--task', action='store_true', help='relay that same message to the new session as its first task')
    vd = sub.add_parser('verdict', help="record the human's verdict on a decision, sending nothing")
    vd.add_argument('--entry', metavar='ID', help="which of this turn's messages, when the human wrote several")
    decide(vd, required=True)
    ac = sub.add_parser('accept', help='take a relay and print its message (run by the target)')
    ac.add_argument('--action', required=True)
    sh = sub.add_parser('show', help='print a relay this session accepted again, recording nothing (run by the target)')
    sh.add_argument('--action', required=True)
    o = sub.add_parser('outcome', help='say how a relay ended (run by the target)')
    o.add_argument('--action', required=True)
    o.add_argument('--result', required=True, choices=RESULTS)
    o.add_argument('--note-file', help='the note, or - for stdin')
    b = sub.add_parser('brief', help='print what a status request asks, or join a watch (run by the target)')
    which = b.add_mutually_exclusive_group(required=True)
    which.add_argument('--for', dest='msg')
    which.add_argument('--pending', action='store_true', help='join the open watch covering this session')
    r = sub.add_parser('report', help='answer a status request (run by the target)')
    r.add_argument('--for', dest='msg', required=True)
    r.add_argument('--file', required=True, help="the JSON report, or - for stdin")
    a = ap.parse_args()

    try:
        if a.cmd == 'list':
            found = everyone()
            if a.json:
                print(json.dumps(found, indent=2))
                return 0
            for s in found:
                who = s['account'] or '?'
                missing = unpinned(s)
                note = f'  (unpinned: {", ".join(missing)})' if missing else ''
                at = time.strftime('%H:%M', time.localtime(s['status_ms'] / 1000)) if s['status_ms'] else '?'
                state = s['status'] + (f' ({s["blocked_on"]})' if s['blocked_on'] else '')
                said = f'\t"{s["said"]}"' if s['said'] else ''
                print(f'{s["handle"]}\t{s["kind"]}/{who}\t{state}\t{at}\t{s["cwd"] or "?"}{note}{said}')
            return 0
        if a.cmd == 'enroll':
            return enroll(a.replace)
        if a.cmd == 'brief':
            return pending() if a.pending else brief(a.msg)
        if a.cmd == 'report':
            return report(a.msg, a.file)
        if a.cmd == 'accept':
            return accept(a.action)
        if a.cmd == 'show':
            return show_relay(a.action)
        if a.cmd == 'outcome':
            return outcome(a.action, a.result, read_text(a.note_file) if a.note_file else '')
        if a.cmd == 'collect':
            return collect(a.msgs, a.wait)
        if a.cmd == 'watches':
            return watches()
        if a.cmd == 'queue' and not a.refresh:
            return queue(a.all, a.json)
        if a.cmd == 'reports':
            if a.new and a.msgs:
                die('reports --new takes no ids', 2)
            show = lambda rows: show_rows(rows, a.json)  # noqa: E731
            if a.new:
                new_reports(show)
            else:
                show(reports(a.msgs))
            return 0
        mine = me()
        if a.cmd == 'ask':
            for address in resolve(a.addresses):
                print(ask(address, mine, a.force), flush=True)
            return 0
        if a.cmd == 'ingest':
            return ingest(a.chief, mine)
        if a.cmd == 'queue':
            return queue(a.all, a.json, mine)
        if a.cmd == 'cancel':
            return cancel(a.msg, mine)
        if a.cmd == 'watch':   # all stays all, sessions to come included: resolve() would list today's
            return arm(['all'] if a.targets == ['all'] else list(resolve(a.targets)), mine)
        if a.cmd == 'relay':
            linked = decided(a)
            if a.retry:
                if a.addresses or a.entry or a.dry_run or a.force or linked:
                    die('relay --retry takes only the held action', 2)
                return retry(a.retry, mine)
            if not a.addresses:
                die('relay names its targets', 2)
            if a.dry_run:
                return preview(resolve(a.addresses), a.entry, mine)
            return relay(resolve(a.addresses), a.entry, mine, linked, a.force)
        if a.cmd == 'verdict':
            return verdict(decided(a), a.entry, mine)
        if a.cmd == 'start':
            return start(a.checkout, a.account, a.name, a.space, a.tab, a.entry, mine, a.task)
    except Corrupt as e:
        die(f'ledger corrupt, nothing decided until it is reconciled by hand: {e}', 4)
    except hc.Error as e:
        die(str(e), 5)


if __name__ == '__main__':
    sys.exit(main())
