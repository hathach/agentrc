#!/usr/bin/env python3
"""Mechanics of the captain: find the Claude Code sessions in this host's hc terminals, pin each one's identity, and
tell whether its input line is empty before anything is typed into it.

Judgment stays in SKILL.md. Every check refuses rather than guesses: an identity it cannot pin or a screen it
does not recognize is reported, never assumed safe.

  captain.py list [--json]                       every other Claude Code session, latest status report first
  captain.py enroll [--replace]                  make this session the captain
  captain.py ask <target>... [--force]           send each idle target a status request, unless unchanged
  captain.py collect [<msg>...] [--wait MS]      print each report as it lands
  captain.py reports [<msg>...]                  the latest report from each target, or those requests' reports
  captain.py cancel <msg>                        close a request or relay no answer will close
  captain.py relay <target>... [--entry <uuid> --digest <hex>]
                                                 show, then relay, the human's newest message
  captain.py withdraw <action>                   take back a relay
  captain.py open --checkout <dir> --account <main|ada> [--name <name>]
                                                 start a Claude Code session in a new terminal
  captain.py brief --for <msg>                   a target reads what a status request asks
  captain.py report --for <msg> --file <json|->  a target answers a status request
  captain.py accept --action <action>            a target takes a relay and reads its words
  captain.py outcome --action <action> --result <result> [--note-file <file|->]
                                                 a target says how a relay ended

A target is a terminal address (t7) or a selector: pr:N, pr:<owner>:<repo>:N, issue:N, issue:<owner>:<repo>:N,
matched against each live session's latest report, or space:<herdr space id or name>. A selector must match exactly
one session; `all`, alone, is every live session. A relay with --entry names addresses only.

A message that opens `tell <name>... [to]`, each name an address, space id or name, or selector, is relayed without
that opening when the names are exactly the relay's targets, and refused when they are not or cannot be told apart
from the instruction. One addressed to every session (`tell all|everyone|every session to|:`) is relayed without it
when the targets are exactly the live sessions, at the preview and at the send; the preview's digest covers the text,
the target terminals and the session pinned in each. A message hc pasted (`<hc from=...>`) is never the human's.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: /proc is Linux-only, so the captain is too
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))   # the agentrc checkout this runs from
import hc  # noqa: E402

CONFIG_DIRS = {'main': None, 'ada': os.path.expanduser('~/.claude-ada')}   # main is CLAUDE_CONFIG_DIR unset
DEFAULT_CONFIG = '~/.claude'   # where main, CLAUDE_CONFIG_DIR unset, keeps its transcripts
START_S = 60   # how long open waits for Claude Code to report a status
HC_TIMEOUT_S = 30
ENV = '/usr/bin/env'   # starts Claude Code with the account's environment
CLAUDE_APP = 'claude-code'
TERMINAL_NAME = re.compile(r'[a-z0-9_-]{1,32}')   # what hc takes as a terminal name
RULE = re.compile(r'^─{20,}')  # the top rule may end in a session label: ──── alo68 ─
BLANK = ' \xa0\t\r'
CLAUDE_HINTS = re.compile(r'⏵⏵|\? for shortcuts|shift\+tab|esc to interrupt')
# hc wraps what it pastes into an agent as <hc from="t7 name" ...>text</hc>; a tag anywhere else, or a forged one,
# is another program's text all the same
HC_TAG = re.compile(r'<\s*/?\s*hc\b', re.I)
HC_WRAPPED = re.compile(r'<hc from="[^"<>]*"(?: [a-z]+="[^"<>]*")*>(.*)</hc>', re.S)
STATE = os.environ.get('AGENTRC_CAPTAIN_DIR') or os.path.expanduser('~/.cache/agentrc/captain')
LEDGER, ENROLLED = 'ledger-hc.jsonl', 'enrolled-hc.json'   # ledger.jsonl and enrolled.json pinned Herdr panes
INSTALLED = (os.environ.get('AGENTRC_CAPTAIN_SCRIPT')
             or os.path.expanduser('~/.claude/skills/captain/scripts/captain.py'))
IDENTITY = ('address', 'created', 'kind', 'cwd', 'session', 'pid', 'started', 'account')
ASKABLE = ('idle', 'done')
REPORT = {'task': str, 'prs': list, 'state': str, 'evidence': str, 'chief': str, 'needs': str, 'next': str}
OPTIONAL = {'issues': list}   # a report without it names no issue
STATES = ('ready', 'in-progress', 'blocked', 'needs-human', 'done', 'idle')
RECORDS = ('request', 'report', 'cancel', 'delivered', 'undelivered', 'uncertain',
           'relay', 'accepted', 'outcome', 'withdraw', 'withdraw-requested', 'refused')
ITEMS = ('request', 'relay')
CLOSED = ('report', 'cancel', 'undelivered', 'outcome', 'withdraw', 'refused')
RESULTS = ('completed', 'failed', 'rejected', 'needs-clarification')
REPORT_MAX = 8192
POLL_S = 2
NO_ANSWER = 3
SELECTORS = ('pr:', 'issue:', 'space:')
SELECTOR = re.compile(r'(pr|issue):(?:([\w.-]+):([\w.-]+):)?(\d+)|space:(.+)')
TELL = re.compile(r'\s*tell\s+', re.I)
NAME = re.compile(r'[^\s,:;&]+(?::[^\s,:;&]+)*')   # a selector keeps its inner colons
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


def input_state(screen):
    """'empty', 'text' or 'unknown' for the input line on an `hc read` screen; only 'empty' is safe to type into,
    since a paste appends to whatever a human has half typed. hc read is plain text, so Claude Code's dim suggestion
    cannot be told from a draft: any text counts. Claude Code draws its input between the last two full-width rules,
    the first line led by ❯."""
    lines = [line.rstrip(BLANK) for line in screen.splitlines()]
    rules = [i for i, line in enumerate(lines) if RULE.match(line)]
    if len(rules) < 2 or rules[-1] - rules[-2] < 2 or not footer_only(lines[rules[-1] + 1:]):
        return 'unknown'
    head, *rest = (line.lstrip(BLANK) for line in lines[rules[-2] + 1:rules[-1]])
    if not head.startswith('❯'):
        return 'unknown'
    return 'text' if head[1:].strip(BLANK) or any(rest) else 'empty'


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
    pid are its foreground process's: the terminal's own are the shell's it may have started from."""
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


@contextmanager
def locked():
    """Every ledger read that decides something and every append happen under this one lock, so two invocations
    (the captain, or targets reporting) cannot both see a request open and both act on it."""
    if not fcntl:
        die('this script needs POSIX file locks', 2)
    os.makedirs(STATE, exist_ok=True)
    with open(path('lock'), 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
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
    if rec['type'] in ITEMS:
        captain = rec.get('captain')
        relayed = rec['type'] != 'relay' or all(text(rec.get(k)) for k in ('entry', 'quote', 'context'))
        return (relayed and isinstance(rec.get('handle'), str) and pinned_identity(rec.get('target'))
                and pinned_identity(captain) and text(captain.get('generation')))
    return True


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


def selected(match, session, recs):
    """Whether session is one a selector match names: by herdr space id or name, or by a PR or issue in the latest
    report this session, as pinned now, gave; a bare number matches it in any repository."""
    kind, owner, repo, number, space = match.groups()
    if space is not None:
        return space in (session.get('space_id'), session.get('space'))
    last = last_report(session, recs)
    named = last['report'].get('prs' if kind == 'pr' else 'issues', []) if last else []
    if owner:
        return f'{owner}/{repo}#{number}'.lower() in (r.lower() for r in named)
    return any(r.endswith(f'#{number}') for r in named)


def resolve(args):
    """{address: the argument naming it} for the target arguments, an address as given, a selector replaced by the
    address of the one session it matches; one terminal named twice goes once. Dies, so nothing is sent, when any
    selector matches none or several. `all` is every live session, and only alone."""
    if 'all' in args:
        if len(args) > 1:
            die('all names every live session, not one to join with other targets; nothing sent', 2)
        found = {s['address']: 'all' for s in everyone()}
        if not found:
            die('all: no live session; nothing sent', 2)
        return found
    if any(a.startswith(SELECTORS) for a in args):
        with locked():
            recs = records()
        live = everyone()
    found = {}
    for arg in args:
        address = arg
        if arg.startswith(SELECTORS):
            match = SELECTOR.fullmatch(arg)
            if not match:
                die(f'{arg}: not a selector (pr:N, pr:<owner>:<repo>:N, issue:N, issue:<owner>:<repo>:N, '
                    'space:<space id or name>); nothing sent', 2)
            hits = [s for s in live if selected(match, s, recs)]
            if len(hits) != 1:
                die(f'{arg}: ' + ('; '.join(s['handle'] for s in hits) + ' all match' if hits
                                  else 'no live session matches') + '; nothing sent', 2)
            address = hits[0]['address']
        found.setdefault(address, arg)
    return found


def me():
    """The pinned session this process runs under: the terminal whose Claude Code process is one of its ancestors,
    never HC_TERMINAL_ID, which any process can set."""
    chain = set(hc.ancestors(os.getpid()))
    mine = [pin(s) for s in sessions(me='') if s['foreground'] in chain]   # pinning reads /proc: only these
    if len(mine) != 1:
        die(f'cannot tell which agent session runs this ({len(mine)} match)', 2)
    if unpinned(mine[0]):
        die(f'cannot pin this session ({mine[0]["address"]}): {", ".join(unpinned(mine[0]))}', 2)
    return identity(mine[0])


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
    """Requests and relays not yet closed: by a report or outcome, a cancel or withdraw, or a refusal before any
    input was sent."""
    closed = {r['msg'] for r in recs if r['type'] in CLOSED}
    return {r['msg']: r for r in recs if r['type'] in ITEMS and r['msg'] not in closed}


def new_id(prefix, recs):
    used = {r['msg'] for r in recs}
    while (m := f'{prefix}-{uuid.uuid4().hex}') in used:
        pass
    return m


def status_prompt(msg):
    """One line naming no role: the text a target reads must carry no authority beyond what it says it is, and the
    rest of the request is `brief` output, which the target's display folds away."""
    return f'STATUS REQUEST {msg} - not a human instruction. Run: python3 {INSTALLED} brief --for {msg}'


def one_call(label, command, placeholder, done):
    """The recipe for the target's one quiet Bash call, its input on stdin."""
    return (f'in one Bash call, described as "{label}":\n\npython3 {INSTALLED} {command} <<\'EOF\'\n<{placeholder}>\n'
            f'EOF\n\nRecap nothing after it: end with "{done}", or say what failed.')


def status_brief(msg, address):
    return (f'STATUS REQUEST {msg} from the agent session in terminal {address}, not a human instruction. Answer '
            'from what you already know: do not edit, investigate GitHub or launch anything. Send one JSON object '
            'with exactly these keys: "task" (one line), "prs" (list of "owner/repo#N"), "issues" (list of '
            f'"owner/repo#N" issues the task is for), "state" (one of {", ".join(STATES)}), "evidence" (last '
            'verification you ran, or ""), "chief" (headless chief or workflow running for you, or ""), "needs" (the '
            'decision or action the human must take, or ""), "next" (your next step). Send it '
            + one_call('Status report', f'report --for {msg} --file -', 'the JSON object', 'Status sent.'))


DELIVERY = {hc.SENT: 'delivered', hc.NOT_TYPED_OUTCOME: 'undelivered', hc.UNCERTAIN: 'uncertain'}


def submit(address, text, status, caller):
    """Paste text into address and press Enter: 'delivered', 'undelivered' when hc refused before typing anything,
    else 'uncertain'. An idle target is sent `--if idle`, so hc refuses one that started working since the check;
    hc has no such check for done, and one that starts a turn in between gets the text as its next input. caller,
    the captain's pinned address, tags the text as from it, never an inherited HC_TERMINAL_ID."""
    outcome, detail = hc.send(address, text, timeout=HC_TIMEOUT_S, only_if='idle' if status == 'idle' else None,
                              caller=caller)
    return DELIVERY[outcome], '' if outcome == hc.SENT else str(detail)


def refusal(target):
    """Why target must not be typed into now, or None: re-read right before every submit."""
    if not target:
        return 'no Claude Code terminal at that address'
    if unpinned(target):
        return f'identity not pinned: {", ".join(unpinned(target))}'
    if target['status'] not in ASKABLE:
        return f'it is {target["status"]}'
    state = input_state(read_screen(target['address']))
    if state != 'empty':
        return ('its input line shows text, a draft or a suggestion hc read cannot tell apart' if state == 'text'
                else 'its screen is not a recognized input line')
    return None


def sendable(address, recs, mine):
    """(the pinned session in address, None) when it may be typed into now, else (None, why not)."""
    if any(r['target']['address'] == address for r in open_items(recs).values()):
        return None, 'a request or relay to it is still open (collect it, or cancel it)'
    target = find(address)
    why = 'it is this session' if target and target['address'] == mine['address'] else refusal(target)
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


def unchanged(target, recs):
    """target's last report when its transcript starts no turn after the request that report answered, so asking
    again would only repeat it; None, so the target is asked, whenever that cannot be shown."""
    last = last_report(target, recs)
    if not last:
        return None
    config = config_dir(target['account'])
    found = transcripts(config, target['session']) if config else []
    if len(found) != 1:
        return None
    try:
        with open(found[0], 'rb') as f:
            data = f.read()
    except OSError:
        return None
    at = data.find(last['msg'].encode())
    *lines, torn = data[data.rfind(b'\n', 0, at) + 1:].split(b'\n') if at >= 0 else [b'']
    if not lines or torn:   # not there, or a partly written line that may be a new turn
        return None
    try:
        entries = (json.loads(line) for line in lines)   # lazily: the first new turn ends the walk
        for entry in entries:   # a record of the API request that carried it may quote it first
            words = plain_text(content(entry))
            if human_input(entry) and words and unwrapped(words) == status_prompt(last['msg']):
                return None if any(new_turn(e) for e in entries) else last
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
        last = not force and unchanged(target, recs)
        if last:
            at = time.strftime('%H:%M', time.localtime(last['at']))
            return f'{address}: not asked, nothing new since its report {last["msg"]} at {at}'
        msg = new_id('m', recs)
        return deliver('request', msg, captain, target, status_prompt(msg))


def deliver(kind, msg, captain, target, prompt, **fields):
    """Record the item, then type its prompt and record how that went: a crash between leaves the item open, never
    typed unrecorded."""
    append({'type': kind, 'msg': msg, 'captain': captain, 'target': identity(target), 'handle': target['handle'],
            **fields})
    address = target['address']
    outcome, err = submit(address, prompt, target['status'], captain['address'])
    append({'type': outcome, 'msg': msg, 'error': err} if err else {'type': outcome, 'msg': msg})
    return f'{address}: {msg} {outcome}' + (f' ({err})' if err else '')


def validate(report):
    if not isinstance(report, dict) or not set(REPORT) <= set(report) <= set(REPORT) | set(OPTIONAL):
        return f'keys must be exactly {", ".join(REPORT)}, and optionally {", ".join(OPTIONAL)}'
    for key, kind in {**REPORT, **OPTIONAL}.items():
        if key in report and not isinstance(report[key], kind):
            return f'"{key}" must be a {kind.__name__}'
    for key in ('prs', 'issues'):
        if not all(isinstance(p, str) and re.fullmatch(r'[\w.-]+/[\w.-]+#\d+', p) for p in report.get(key, [])):
            return f'"{key}" must hold "owner/repo#N" strings'
    if report['state'] not in STATES:
        return f'"state" must be one of {", ".join(STATES)}'
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
    """The request or relay msg, which must have been sent to mine, the session running this, as pinned then."""
    item = first(recs, kind, msg)
    if not item:
        die(f'no {kind} {msg}', 3)
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
    """What request msg asks, printed for the session it was sent to while it is open; writes nothing."""
    with locked():
        mine = me()
        recs = records()
        request = requested(msg, recs, mine)
        if msg not in open_items(recs):
            die(f'request {msg} is closed', 2)
    print(status_brief(msg, request['captain']['address']))
    return 0


def report(msg, file):
    """A target answers msg: only the session it was sent to, as pinned then, only while it is open, only once."""
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
        requested(msg, recs, mine)
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


EVENTS = {'cancel': 'cancelled', 'undelivered': 'undelivered', 'withdraw': 'withdrawn', 'accepted': 'accepted',
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


def collect(msgs, wait_ms):
    """Print each awaited request's or relay's events as they land, its end included: a report or outcome, its
    cancellation, withdrawal or refusal; exit 0 once none is pending, NO_ANSWER when wait_ms lapse first. Which
    are awaited is decided once, strictly, under the lock: a torn ledger must not look like nothing to wait for."""
    with locked():
        recs = records()
    items = {r['msg']: r for r in recs if r['type'] in ITEMS}
    waiting = set(msgs) if msgs else set(open_items(recs))
    unknown = waiting - set(items)
    if unknown:
        die(f'no request or relay {", ".join(sorted(unknown))}', NO_ANSWER)
    deadline = time.monotonic() + wait_ms / 1000
    ended = set()
    shown = 0   # the ledger is append-only: records before this were printed already
    while True:
        for rec in recs[shown:]:
            line = rec['msg'] in waiting and event(items[rec['msg']], rec)
            if line:
                print(line, flush=True)
            if rec['type'] in CLOSED and rec['msg'] in waiting:
                ended.add(rec['msg'])
        shown = len(recs)
        pending = waiting - ended
        if not pending:
            return 0
        if time.monotonic() >= deadline:
            print(f'pending {" ".join(sorted(pending))}', flush=True)
            return NO_ANSWER
        time.sleep(POLL_S)
        recs = records(lenient=True)


def reports(msgs=()):
    """The latest report per target terminal, newest first; with msgs, the report answering each of those requests, in
    order, a later report from the same session notwithstanding, and a row naming why for one with none."""
    recs = records(lenient=True)
    requests = {r['msg']: r for r in recs if r['type'] == 'request'}
    answers = {r['msg']: r for r in recs if r['type'] == 'report'}
    if msgs:
        return [row(requests[m], answers[m]) if m in answers else
                {'msg': m, 'missing': 'not answered' if m in requests else 'no such request'} for m in msgs]
    latest = {}
    for rep in answers.values():
        latest[requests[rep['msg']]['target']['address']] = (requests[rep['msg']], rep)
    return [row(req, rep) for req, rep in sorted(latest.values(), key=lambda pair: -pair[1]['at'])]


def row(request, rep):
    return {'handle': request['handle'], 'msg': request['msg'], **rep['report']}


def cancel(msg, mine):
    with locked():
        require_captain(mine)
        recs = records()
        if msg not in open_items(recs):
            die(f'no open request or relay {msg}', 3)
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
    hc pasted is typed too, so human_inputs() tells it apart."""
    origin = entry.get('origin') if isinstance(entry, dict) else None
    return (isinstance(origin, dict) and origin.get('kind') == 'human' and entry.get('type') == 'user'
            and entry.get('promptSource') in ('typed', 'queued') and not entry.get('isMeta')
            and not entry.get('isSidechain'))


def human_inputs(file):
    """(inputs, torn): the human's messages in a transcript, in order, each (uuid, text or None when not plain
    text); torn when a line is not whole JSON, which may hide a newer message. One that opens with an hc tag is
    another program's, hc's paste or made to look like one, and is left out."""
    with open(file, 'rb') as f:
        lines = f.read().split(b'\n')
    torn = bool(lines[-1])
    inputs = []
    for line in lines[:-1]:
        try:
            entry = json.loads(line)
        except ValueError:
            torn = True
            continue
        if not human_input(entry):
            continue
        words = plain_text(content(entry))
        if not (words and HC_TAG.match(words.lstrip())):
            inputs.append((entry.get('uuid'), words))
    return inputs, torn


def transcript(captain):
    """The captain's own Claude Code transcript: the human's words are read from it, never passed by the captain."""
    if os.environ.get('CLAUDE_CODE_SESSION_ID') != captain['session']:
        die("relay runs only in the enrolled captain's own Claude Code session", 2)
    found = transcripts(os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or DEFAULT_CONFIG), captain['session'])
    if len(found) != 1:
        die(f"cannot find this session's transcript ({len(found)} match)", 2)
    return found[0]


def newest(captain, sending):
    """The human's newest message in the captain's transcript, as (entry, text). A torn transcript may hide a newer
    one: sending refuses it, a preview only warns."""
    inputs, torn = human_inputs(transcript(captain))
    if torn and sending:
        die('the transcript ends in a partly written line that may hold a newer message; retry', 2)
    if not inputs:
        die('no message from the human in this session yet', 3)
    entry, words = inputs[-1]
    if words is None:
        die(f"the human's newest message ({entry}) is not plain text, so it cannot be relayed verbatim", 2)
    if HC_TAG.search(words):   # hc pastes into a half-typed draft: what follows the tag is not the human's
        die(f"the human's newest message ({entry}) holds an hc tag, so it may carry another program's words; "
            'ask the human to restate it', 2)
    if torn:
        print('the transcript ends in a partly written line: relaying refuses until it is whole')
    return entry, words


def named(token, live, recs):
    """The addresses of the live sessions token names: by address, herdr space id or name, or selector."""
    if token.startswith(SELECTORS):
        match = SELECTOR.fullmatch(token)
        return {s['address'] for s in live if selected(match, s, recs)} if match else set()
    token = token.casefold()
    return {s['address'] for s in live
            if token in (s.get(k) and s[k].casefold() for k in ('address', 'space_id', 'space'))}


def instruction(words, addresses, live, recs):
    """(text, None): what a relay to addresses sends, words without an opening `tell <name>... [to]` whose names are
    exactly addresses; (None, why) when that opening names anyone else or cannot be split from the instruction. Preview
    and send both call this, and the send checks the preview's digest of it."""
    tell = TELL.match(words)
    if not tell:
        return words, None
    pos, found = tell.end(), set()
    first = EVERY_WORD.match(words, pos)
    if first:
        return broadcast(words, first, addresses, live, recs)
    while True:
        name = NAME.match(words, pos)
        if name and EVERY_WORD.fullmatch(name.group()):
            return None, f'"{name.group()}" addresses every session, not one to join with other names'
        hits = named(name.group(), live, recs) if name else set()
        if not hits:
            return None, f'it opens with "tell" and {name.group()!r} names no live session' if name else \
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
        # an address or selector is a recipient even when it names no one, never the instruction's first word
        if not (following and (':' in following.group() or ADDRESS.fullmatch(following.group())
                               or EVERY_WORD.fullmatch(following.group())
                               or named(following.group(), live, recs))):
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
    """A refusal a new preview cures: the sessions changed, not the human's words."""


def broadcast(words, first, addresses, live, recs):
    """instruction() for a message opening `tell all|everyone|every session`, first its opening word."""
    if named(first.group(), live, recs):
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


def pins(found):
    """{address: the identity of the session found in it, None for none}."""
    return {address: identity(s) if s else None for address, s in found.items()}


def digest(text, pinned):
    """The preview's digest of what goes to which session, pinned as pins() gives it: target order or a status change
    does not change it, a session opened, closed or replaced in a target terminal does."""
    return hashlib.sha256(json.dumps([text, sorted(pinned.items())]).encode('utf-8')).hexdigest()[:16]


def relay_text(words, addresses, recs):
    """The text a relay to addresses sends; dies, so nothing is sent, when instruction() refuses it."""
    if not TELL.match(words):   # unaddressed: sent as written, no session listing needed
        return words
    text, why = instruction(words, addresses, everyone(), recs)
    if isinstance(why, Stale):
        die(f'{why}: run list, then preview again with every address it shows', 2)
    if why:
        die(f"the human's message cannot be relayed as written: {why}; ask the human to restate it", 2)
    return text


def preview(targets, mine):
    """The human's newest message as it would be sent and who would get it, targets as resolve() gave them; sends
    nothing."""
    with locked():
        captain = require_captain(mine)
        recs = records()
    entry, words = newest(captain, sending=False)
    found = {address: find(address) for address in targets}
    for address, arg in targets.items():
        target = found[address]
        print(f'to {arg}: {target["handle"]} ({target["status"]})' if target
              else f'to {arg}: no Claude Code terminal there')
    text = relay_text(words, list(targets), recs)
    print(f'entry {entry} digest {digest(text, pins(found))}\n{text}')
    return 0


def last_report(target, recs):
    """The latest report record from target as pinned now, or None: one from an earlier session in the same terminal is
    not this session's."""
    requests = {r['msg']: r for r in recs if r['type'] == 'request'}
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


def relay_prompt(action):
    """One line naming no role and holding no quote: the target acts only on what `accept` prints from the
    ledger."""
    return (f'RELAYED HUMAN INSTRUCTION {action} - not the message itself. '
            f'Run: python3 {INSTALLED} accept --action {action}')


def relay(addresses, entry, sent, mine):
    """Relay the human's newest message, which must still be entry and still read as the preview's digest sent, to
    each address, recorded before it is typed. The transcript is read once for every target: a message the human sends
    after that is not seen."""
    if any(p.startswith(SELECTORS) or p == 'all' for p in addresses):
        die('a relay names the addresses the preview printed, never a selector or all', 2)
    if not sent:
        die('a relay names the digest the preview printed (--digest)', 2)
    with locked():
        captain = require_captain(mine)
        latest, words = newest(captain, sending=True)
        if latest != entry:
            die(f"{entry} is not the human's newest message; preview again", 2)
        pinned = pins({address: find(address) for address in addresses})
        quote = relay_text(words, addresses, records())
        if sent != digest(quote, pinned):
            die('the text, terminals or sessions this relay would send to are not the ones the preview showed; '
                'preview again', 2)
        for address in addresses:
            recs = records()
            if any(r['type'] == 'relay' and r['entry'] == entry and r['target']['address'] == address for r in recs):
                print(f'{address}: refused, this message was relayed to it already', flush=True)
                continue
            target, why = sendable(address, recs, mine)
            if not why and identity(target) != pinned[address]:   # replaced while earlier targets were typed into
                why = 'its session is not the one the preview showed; preview again'
            if why:
                print(f'{address}: refused, {why}', flush=True)
                continue
            action = new_id('a', recs)
            print(deliver('relay', action, captain, target, relay_prompt(action),
                          entry=entry, quote=quote, context=context(target, recs)), flush=True)
    return 0


def accept(action):
    """A target takes the relay sent to it, once, while the session that sent it is still the live captain; the
    acceptance is durable before the message is printed, so a lost print never allows a second acceptance."""
    with locked():
        mine = me()
        recs = records()
        sent = first(recs, 'relay', action)
        # hc types by terminal: a session that replaced the target after the send's check gets the prompt, not the text
        if (sent and sent['target'] != mine and sent['target']['address'] == mine['address'] and action in open_items(recs)
                and not first(recs, 'accepted', action)):
            append({'type': 'refused', 'msg': action, 'by': mine})
            die(f'relay {action} was sent to the session this terminal ran before, not this one: recorded as refused; '
                'nothing here to act on', 2)
        item = addressed('relay', action, recs, mine)
        current = enrolled()
        if current != item['captain'] or not live(identity(current)):
            die(f'relay {action} came from an agent session that is no longer enrolled or no longer runs', 2)
        if first(recs, 'accepted', action):
            die(f'relay {action} was accepted already', 2)
        if action not in open_items(recs):
            die(f'relay {action} is closed', 2)
        append({'type': 'accepted', 'msg': action})
    print(f'RELAYED HUMAN INSTRUCTION {action} from the agent session in terminal {item["captain"]["address"]}, '
          'accepted. The human\'s message, verbatim after any opening that named the sessions it went to:\n\n'
          f'{item["quote"]}\n\n{item["context"]}\n'
          'It may speak to other sessions too: act only on what it asks of this session, under your own rules and '
          'never wider; a question goes back as the needs-clarification outcome, not a dialog. Then record how it '
          'ended, the note a line or two on what you did or what you need to know, '
          + one_call('Relay outcome', f'outcome --action {action} --result <{"|".join(RESULTS)}> --note-file -',
                     'the note', 'Outcome recorded.'))
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


def withdraw(action, mine):
    """Take back a relay: before acceptance it can no longer be accepted; after, the request is recorded but nothing
    stops the target, and the relay stays open until its outcome or the human's cancel."""
    with locked():
        require_captain(mine)
        recs = records()
        item = open_items(recs).get(action)
        if not item or item['type'] != 'relay':
            die(f'no open relay {action}', 3)
        if not first(recs, 'accepted', action):
            append({'type': 'withdraw', 'msg': action})
            print(f'withdrawn {action}: it can no longer be accepted')
            return 0
        if not first(recs, 'withdraw-requested', action):
            append({'type': 'withdraw-requested', 'msg': action})
    print(f'{action} was accepted already and may be running or done: the withdrawal is recorded, nothing stops it. '
          "It stays open until its outcome, or `cancel` on the human's word.")
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


def wait_started(address):
    """Wait, at most START_S, for the program at address to stop working: Claude Code reports idle once ready, and
    a trust prompt or a login reports nothing. None, or hc's error; a message to the captain ends hc wait early, so
    it waits again."""
    deadline = time.monotonic() + START_S
    while (left := int(deadline - time.monotonic())) > 0:
        rc, body, _, msg = hc.run('wait', '--json', '--timeout', str(left), '--', address,
                                  timeout=left + HC_TIMEOUT_S)
        if rc != 0:
            return msg
        if not isinstance(body, dict):
            return 'hc wait answered no JSON object'
        if body.get('reason') != 'message':
            return None
    return None


def not_ready(session, checkout, account_name):
    """Why a session just opened is not the one asked for, idle at an empty input line, or None."""
    if not session:
        return 'no Claude Code status from the terminal'
    if unpinned(session):
        return f'identity not pinned: {", ".join(unpinned(session))}'
    if session['cwd'] != checkout:
        return f'it runs in {session["cwd"]}'
    if session['account'] != account_name:
        return f'it runs under account {session["account"]}'
    return refusal(session)


def terminal_name(name, root):
    """name, or one made from the checkout's directory name, as hc takes it."""
    if name is None:
        name = re.sub(r'[^a-z0-9_-]+', '-', os.path.basename(root).lower()).strip('-')[:32] or 'claude'
    if not TERMINAL_NAME.fullmatch(name):
        die(f'{name}: an hc terminal name is 1-32 of a-z, 0-9, - and _', 2)
    return name


def open_session(checkout, account_name, name, mine):
    """A new terminal on an existing checkout, a tab in the captain's herdr space, with Claude Code started under the
    account named; ready only once it is pinned there with an empty input line. Anything else, such as a trust
    prompt, is left for the human: nothing is answered, closed or retried. It has no parent, so it outlives the
    captain and hc types no notes about it into the captain."""
    root = canonical(os.path.expanduser(checkout))
    if not root or not os.path.isdir(root):
        die(f'{checkout} is not an existing directory', 2)
    name = terminal_name(name, root)
    config = CONFIG_DIRS[account_name]
    # explicit either way: the terminal's environment comes from hc, not from this process
    account_env = [f'CLAUDE_CONFIG_DIR={config}'] if config else ['-u', 'CLAUDE_CONFIG_DIR']
    with locked():   # held to the end: a captain replaced meanwhile must not go on opening
        require_captain(mine)
        space = ((get(mine['address']) or {}).get('labels') or {}).get('herdr.space')
        labels = ['--label', f'herdr.space={space}'] if space else []
        address = hc.call('run', '--json', '--bg', '--no-parent', '--name', name, '--cwd', root, *labels, '--',
                          ENV, *account_env, 'claude', timeout=HC_TIMEOUT_S).get('address')
        if not hc.is_local(address):
            die(f'hc run answered no local terminal address ({address!r}); look at `hc ls`', 5)
        # first, so a failure below still names the terminal
        print(f'{address}: opened {name} · {root}' + ('' if space else f' · in no space: hc attach {address}'),
              flush=True)
        err = wait_started(address)
        why = f'Claude Code did not start: {err}' if err else not_ready(find(address), root, account_name)
    if why:
        print(f'{address}: not ready: {why}. Look at the terminal; nothing was answered or closed')
        return 2
    print(f'{address}: ready · claude/{account_name}')
    return 0


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
    rp = sub.add_parser('reports', help="the latest report from each target, or the named requests' reports")
    rp.add_argument('msgs', nargs='*')
    rp.add_argument('--json', action='store_true')
    sub.add_parser('cancel', help='close a request or relay no answer will close').add_argument('msg')
    rl = sub.add_parser('relay', help="show the human's newest message; with --entry and --digest, relay it")
    rl.add_argument('addresses', nargs='+')
    rl.add_argument('--entry', metavar='UUID')
    rl.add_argument('--digest', metavar='HEX', help='the digest the preview printed')
    sub.add_parser('withdraw', help='take back a relay').add_argument('action')
    op = sub.add_parser('open', help='start a Claude Code session in a new terminal on an existing checkout')
    op.add_argument('--checkout', required=True)
    op.add_argument('--account', required=True, choices=sorted(CONFIG_DIRS))
    op.add_argument('--name')
    ac = sub.add_parser('accept', help='take a relay and print its message (run by the target)')
    ac.add_argument('--action', required=True)
    o = sub.add_parser('outcome', help='say how a relay ended (run by the target)')
    o.add_argument('--action', required=True)
    o.add_argument('--result', required=True, choices=RESULTS)
    o.add_argument('--note-file', help='the note, or - for stdin')
    b = sub.add_parser('brief', help='print what a status request asks (run by the target)')
    b.add_argument('--for', dest='msg', required=True)
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
                print(f'{s["handle"]}\t{s["kind"]}/{who}\t{s["status"]}, reported {at}\t{s["cwd"] or "?"}{note}')
            return 0
        if a.cmd == 'enroll':
            return enroll(a.replace)
        if a.cmd == 'brief':
            return brief(a.msg)
        if a.cmd == 'report':
            return report(a.msg, a.file)
        if a.cmd == 'accept':
            return accept(a.action)
        if a.cmd == 'outcome':
            return outcome(a.action, a.result, read_text(a.note_file) if a.note_file else '')
        if a.cmd == 'collect':
            return collect(a.msgs, a.wait)
        if a.cmd == 'reports':
            rows = reports(a.msgs)
            if a.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
                return 0
            for r in rows:
                if 'missing' in r:
                    print(f'{r["msg"]}\t{r["missing"]}')
                    continue
                prs = ' '.join(r['prs']) or '-'
                print(f'{r["handle"]}\t{one_line(r["task"])}\t{prs}\t{r["state"]}\t{one_line(r["needs"]) or "-"}')
            return 0
        mine = me()
        if a.cmd == 'ask':
            for address in resolve(a.addresses):
                print(ask(address, mine, a.force), flush=True)
            return 0
        if a.cmd == 'cancel':
            return cancel(a.msg, mine)
        if a.cmd == 'relay':
            return relay(a.addresses, a.entry, a.digest, mine) if a.entry else preview(resolve(a.addresses), mine)
        if a.cmd == 'withdraw':
            return withdraw(a.action, mine)
        if a.cmd == 'open':
            return open_session(a.checkout, a.account, a.name, mine)
    except Corrupt as e:
        die(f'ledger corrupt, nothing decided until it is reconciled by hand: {e}', 4)
    except hc.Error as e:
        die(str(e), 5)


if __name__ == '__main__':
    sys.exit(main())
