#!/usr/bin/env python3
"""Mechanics of the captain: find the Claude Code sessions in this host's hc terminals, pin each one's identity, and
tell whether its input line is empty before anything is typed into it.

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
  captain.py cancel <msg>                        close a request, relay or watch no answer will close
  captain.py relay <target>... [--entry <uuid> --digest <hex>]
                                                 show, then relay, the human's newest message
  captain.py brief --for <msg> | --pending       a target reads what a status request asks, or joins a watch
  captain.py report --for <msg> --file <json|->  a target answers a status request
  captain.py accept --action <action>            a target takes a relay and reads its words
  captain.py outcome --action <action> --result <result> [--note-file <file|->]
                                                 a target says how a relay ended

A target is a terminal address (t7) or space:<herdr space id or name>, which must match exactly one session; `all`,
alone, is every live session. A relay with --entry names addresses only.

A message that opens `tell <name>... [to]`, each name an address, space id or name, or space:<id or name>, is relayed
without that opening when the names are exactly the relay's targets, and refused when they are not or cannot be told
apart from the instruction. One addressed to every session (`tell all|everyone|every session to|:`) is relayed
without it when the targets are exactly the live sessions, at the preview and at the send; the preview's digest
covers the text, the target terminals and the session pinned in each. A message hc pasted (`<hc from=...>`) is
never the human's.

A watch types into nobody: a session joins it itself, through hooks/captain (SessionStart after a compaction or
/clear, Stop) or `brief --pending`, and ends with the captain session that started it. A collect without ids
resumes where the enrolled captain's last one stopped, and `reports --new` where its last one did.
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
from contextlib import contextmanager, suppress
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: /proc is Linux-only, so the captain is too
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))   # the agentrc checkout this runs from
import hc  # noqa: E402

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
INSTALLED = (os.environ.get('AGENTRC_CAPTAIN_SCRIPT')
             or os.path.expanduser('~/.claude/skills/captain/scripts/captain.py'))
IDENTITY = ('address', 'created', 'kind', 'cwd', 'session', 'pid', 'started', 'account')
ASKABLE = ('idle', 'done')
REPORT = {'task': str, 'prs': list, 'state': str, 'evidence': str, 'chief': str, 'needs': str, 'next': str}
OPTIONAL = {'issues': list}   # a report without it names no issue
STATES = ('ready', 'in-progress', 'blocked', 'needs-human', 'done', 'idle')
# withdraw and withdraw-requested have no writer since `withdraw` was removed: kept so an older ledger still reads
RECORDS = ('request', 'report', 'cancel', 'delivered', 'undelivered', 'uncertain',
           'relay', 'accepted', 'outcome', 'withdraw', 'withdraw-requested', 'refused',
           'watch', 'watch-extend', 'joined', 'watch-end')
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
    if rec['type'] == 'watch-end':
        return isinstance(rec.get('why'), str)
    return True


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
        '(the decision or action the human must take, or ""), "next" (your next step).')


def status_brief(msg, address):
    return (f'STATUS REQUEST {msg} from the agent session in terminal {address}, not a human instruction. Answer '
            f'from what you already know: do not edit, investigate GitHub or launch anything. {KEYS} Send it '
            + one_call('Status report', f'report --for {msg} --file -', 'the JSON object', 'Status sent.'))


def watch_brief(child, address):
    """What a session joined to a watch does from then on; it names no role and reaches the session only through
    a hook or `brief --pending`."""
    return (f'STATUS WATCH {child} from the agent session in terminal {address}, not a human instruction. Each time '
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
    state = input_state(read_screen(target['address']))
    if state != 'empty':
        return ('its input line shows text, a draft or a suggestion hc read cannot tell apart; '
                'promptSuggestionEnabled: false in its Claude Code settings turns suggestions off' if state == 'text'
                else 'its screen is not a recognized input line')
    return None


def sendable(address, recs, mine):
    """(the pinned session in address, None) when it may be typed into now, else (None, why not)."""
    if any(r['target']['address'] == address for r in open_items(recs).values()):
        return None, 'a request or relay to it is still open (collect it, or cancel it)'
    target = find(address)
    why = unreachable(target, mine) or refusal(target)
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
        member = first(recs, 'joined', msg)
        if member:   # a watch's member reports each task end: every report is kept, none closes it
            member_watch(member, recs, mine)
            append({'type': 'report', 'msg': msg, 'report': payload})
            print(f'recorded {msg}:{len(recs)}')   # every report is kept, so a blind retry would duplicate one
            return 0
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


EVENTS = {'cancel': 'cancelled', 'undelivered': 'undelivered', 'accepted': 'accepted',
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


def read_mark(name):
    """(generation, at) as stored in name, or None: missing or malformed. Each caller reads afresh."""
    try:
        with open(path(name), encoding='utf-8') as f:
            mark = json.load(f)
        return mark['generation'], mark['at']
    except (OSError, ValueError, KeyError, TypeError):
        return None


def stored(name, recs):
    """The ledger index the enrolled captain last stored in name, or None: none stored, another captain's, or one
    past the end of recs."""
    mark = read_mark(name)
    try:
        at = mark[1] if mark and mark[0] == generation() else None
    except (OSError, ValueError):   # an unreadable enrolment: no mark is ours
        return None
    return at if type(at) is int and 0 <= at <= len(recs) else None


def generation():
    return (enrolled() or {}).get('generation')


def store(name, at, fence):
    """Store ledger index at in name, under the lock the caller holds, while fence is still the enrolled captain's
    generation; never behind what that captain stored already, so a slower reader cannot roll it back."""
    if fence is None or fence != generation():
        return
    mark = read_mark(name)
    if mark and mark[0] == fence and type(mark[1]) is int and mark[1] > at:
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
    printed. Without, printing resumes where the enrolled captain's last collect without ids stopped (COLLECTED,
    stored as this one ends), following what was open there and whatever opened since; with no such cursor, at the
    open watch's start, else at the ledger's end. A cursor from before the open watch skips, until that watch's start,
    the events of requests and relays opened while no watch was open: those were collected by id. What is closed is
    judged on the whole ledger. A strict read under the lock first: a torn ledger must not look like nothing to wait
    for. A watch whose captain was replaced or ended is settled here too, at the start and at each poll while one is
    followed, or nothing else might end it. Every lock wait ends with wait_ms, so the pending line still prints before
    the caller's timeout. A collect belongs to the captain enrolled when it began: once another enrolls, it prints its
    pending line and stores nothing."""
    deadline = time.monotonic() + wait_ms / 1000
    try:
        with locked(wait_ms / 1000):
            recs = records()
            opened = len(recs)   # a watch settle ends now was open when this collect began
            watch, _ = settle(recs)
            fence = generation()
            start = 0 if msgs else resumed(COLLECTED, recs, watch, opened)
            armed = recs.index(watch) if watch and not msgs else 0
            by_id = unwatched(recs[:armed])
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
        if any(items[m]['type'] in WATCHED for m in pending):
            try:
                with locked(max(0, deadline - time.monotonic())):   # strict: under the lock no append is half written
                    recs = records()
                    settle(recs)
            except Busy:
                pass   # the deadline passed: the next pass prints the pending line
        else:
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
            ends = watch_ends(recs)
            watch = next((r for r in reversed(recs) if r['type'] == 'watch' and r['msg'] not in ends), None)
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


def cancel(msg, mine):
    with locked():
        require_captain(mine)
        recs = records()
        if first(recs, 'watch', msg):
            watch, _ = settle(recs)
            if not watch or watch['msg'] != msg:
                die(f'no open watch {msg}', 3)
            recs.append(append({'type': 'cancel', 'msg': msg}))
            settle(recs)   # removes the marker
            return 0
        if msg not in open_items(recs):
            die(f'no open request, relay or watch {msg}', 3)
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


def named(token, live):
    """The addresses of the live sessions token names: by address, herdr space id or name, or space:<id or name>."""
    if token.startswith(SPACE):
        space = token.removeprefix(SPACE)
        return {s['address'] for s in live if space and in_space(space, s)}
    token = token.casefold()
    return {s['address'] for s in live
            if token in (s.get(k) and s[k].casefold() for k in ('address', 'space_id', 'space'))}


def instruction(words, addresses, live):
    """(text, None): what a relay to addresses sends, words without an opening `tell <name>... [to]` whose names are
    exactly addresses; (None, why) when that opening names anyone else or cannot be split from the instruction. Preview
    and send both call this, and the send checks the preview's digest of it."""
    tell = TELL.match(words)
    if not tell:
        return words, None
    pos, found = tell.end(), set()
    first = EVERY_WORD.match(words, pos)
    if first:
        return broadcast(words, first, addresses, live)
    while True:
        name = NAME.match(words, pos)
        if name and EVERY_WORD.fullmatch(name.group()):
            return None, f'"{name.group()}" addresses every session, not one to join with other names'
        hits = named(name.group(), live) if name else set()
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
    """A refusal a new preview cures: the sessions changed, not the human's words."""


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


def pins(found):
    """{address: the identity of the session found in it, None for none}."""
    return {address: identity(s) if s else None for address, s in found.items()}


def digest(text, pinned):
    """The preview's digest of what goes to which session, pinned as pins() gives it: target order or a status change
    does not change it, a session opened, closed or replaced in a target terminal does."""
    return hashlib.sha256(json.dumps([text, sorted(pinned.items())]).encode('utf-8')).hexdigest()[:16]


def relay_text(words, addresses):
    """The text a relay to addresses sends; dies, so nothing is sent, when instruction() refuses it."""
    if not TELL.match(words):   # unaddressed: sent as written, no session listing needed
        return words
    text, why = instruction(words, addresses, everyone())
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
    entry, words = newest(captain, sending=False)
    found = {address: find(address) for address in targets}
    for address, arg in targets.items():
        target = found[address]
        print(f'to {arg}: {target["handle"]} ({target["status"]})' if target
              else f'to {arg}: no Claude Code terminal there')
    text = relay_text(words, list(targets))
    print(f'entry {entry} digest {digest(text, pins(found))}\n{text}')
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


def relay_prompt(action):
    """One line naming no role and holding no quote: the target acts only on what `accept` prints from the
    ledger."""
    return (f'RELAYED HUMAN INSTRUCTION {action} - not the message itself. '
            f'Run: python3 {INSTALLED} accept --action {action}')


def relay(addresses, entry, sent, mine):
    """Relay the human's newest message, which must still be entry and still read as the preview's digest sent, to
    each address, recorded before it is typed. The transcript is read once for every target: a message the human sends
    after that is not seen."""
    if any(p.startswith(SPACE) or p == 'all' for p in addresses):
        die('a relay names the addresses the preview printed, never a space or all', 2)
    if not sent:
        die('a relay names the digest the preview printed (--digest)', 2)
    with locked():
        captain = require_captain(mine)
        latest, words = newest(captain, sending=True)
        if latest != entry:
            die(f"{entry} is not the human's newest message; preview again", 2)
        pinned = pins({address: find(address) for address in addresses})
        quote = relay_text(words, addresses)
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


def anchors(ident):
    """What a named watch recognizes a session by: its terminal incarnation, and its session in its account, which a
    resume in another terminal keeps."""
    return [['terminal', ident['address'], ident['created']], ['session', ident['account'], ident['session']]]


def owner_state(watch):
    """'live', 'ended' or 'unknown' for the captain session that started watch, from /proc and its account's session
    registry, no hc: its process gone or restarted, or its session changed (/clear), ends the watch; what cannot be
    read decides nothing."""
    c = watch['captain']
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


def settle(recs):
    """(the open watch or None, its captain's state). Under the lock: ends, durably, every watch whose captain was
    replaced or is confirmed gone, adding each end to recs once written, and only then, with none left open, removes
    the marker; a watch whose captain cannot be told stays open and keeps it."""
    ended = watch_ends(recs)
    owner = enrolled()
    found = (None, None)
    for w in [r for r in recs if r['type'] == 'watch' and r['msg'] not in ended]:
        state = 'ended' if w['captain'] != owner else owner_state(w)
        if state == 'ended':
            recs.append(append({'type': 'watch-end', 'msg': w['msg'], 'why': 'its captain was replaced'
                                if w['captain'] != owner else 'its captain session ended'}))
        else:
            found = (w, state)
    if not found[0]:
        with suppress(FileNotFoundError):
            os.unlink(path(MARKER))
            sync_dir()
    return found


def scopes(recs, watch):
    return [r['scope'] for r in recs if r['type'] in ('watch', 'watch-extend') and r['msg'] == watch]


def terminals(scopes):
    """The scopes for a human: all, or the terminals named."""
    return 'all' if 'all' in scopes else ' '.join(a[1] for scope in scopes for a in scope if a[0] == 'terminal')


def members(recs, watch):
    return [r for r in recs if r['type'] == 'joined' and r['watch'] == watch]


def covers(watch, recs, ident):
    """Whether watch covers the session ident pins: under `all`, any but its captain's; else one matching an anchor of
    the sessions named or of one that joined, so a restart or resume of a member is covered too."""
    if ident['session'] == watch['captain']['session']:
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
    watch, state = settle(recs)
    if not watch:
        return None, 'no watch is open', False
    if state != 'live':
        return None, "cannot confirm the watch's captain session still runs", False
    member = next((m for m in members(recs, watch['msg']) if m['target'] == mine), None)
    if member:
        return member, watch, False
    if not covers(watch, recs, mine):
        return None, 'the open watch does not cover this session', False
    return append({'type': 'joined', 'msg': new_id('j', recs), 'watch': watch['msg'], 'target': mine,
                   'handle': session['handle']}), watch, True


def member_watch(member, recs, mine):
    """Dies, so nothing is recorded, unless member is mine and its watch is open with its captain running."""
    if member['target'] != mine:
        die(f'{member["msg"]} is another session\'s; run `brief --pending` for this one\'s', 2)
    watch, state = settle(recs)
    if not watch or watch['msg'] != member['watch']:
        die(f'the watch {member["watch"]} ended: stop reporting', 2)
    if state != 'live':
        die("cannot confirm the watch's captain session still runs; report again at your next task end", 2)


def pending():
    """A session's own lookup: join the open watch covering it, if any, and print what to do."""
    with locked():
        member, watch, _ = join(my_session())
    print(watch_brief(member['msg'], watch['captain']['address']) if member else f'none: {watch}')
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
    brief = watch_brief(member['msg'], watch['captain']['address'])
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
        watch, _ = settle(recs)
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
    """The open watch and its members, each live, gone or unknown, without changing anything but what settle does."""
    with locked():
        recs = records()
        watch, state = settle(recs)
    if not watch:
        print('no watch is open')
        return 0
    print(f'{watch["msg"]}\t{terminals(scopes(recs, watch["msg"]))}\tcaptain {watch["captain"]["address"]} {state}')
    reported = {r['msg']: i for i, r in enumerate(recs) if r['type'] == 'report'}
    for m in members(recs, watch['msg']):
        try:
            now = find(m['target']['address'])
            seen = ('unknown' if now and unpinned(now) else 'live' if now and identity(now) == m['target']
                    else 'gone')
        except hc.Error:
            seen = 'unknown'
        last = f'{m["msg"]}:{reported[m["msg"]]}' if m['msg'] in reported else 'no report'
        print(f'  {m["msg"]}\t{m["handle"]}\t{seen}\t{last}')
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
    wt = sub.add_parser('watch', help='have sessions report each task end until this captain session ends')
    wt.add_argument('targets', nargs='+')
    sub.add_parser('watches', help='the open watch and its members')
    rp = sub.add_parser('reports', help="the latest report from each target, or the named requests' reports")
    rp.add_argument('msgs', nargs='*')
    rp.add_argument('--json', action='store_true')
    rp.add_argument('--new', action='store_true', help='every watch report since the last --new')
    sub.add_parser('cancel', help='close a request, relay or watch no answer will close').add_argument('msg')
    rl = sub.add_parser('relay', help="show the human's newest message; with --entry and --digest, relay it")
    rl.add_argument('addresses', nargs='+')
    rl.add_argument('--entry', metavar='UUID')
    rl.add_argument('--digest', metavar='HEX', help='the digest the preview printed')
    ac = sub.add_parser('accept', help='take a relay and print its message (run by the target)')
    ac.add_argument('--action', required=True)
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
                print(f'{s["handle"]}\t{s["kind"]}/{who}\t{s["status"]}, reported {at}\t{s["cwd"] or "?"}{note}')
            return 0
        if a.cmd == 'enroll':
            return enroll(a.replace)
        if a.cmd == 'brief':
            return pending() if a.pending else brief(a.msg)
        if a.cmd == 'report':
            return report(a.msg, a.file)
        if a.cmd == 'accept':
            return accept(a.action)
        if a.cmd == 'outcome':
            return outcome(a.action, a.result, read_text(a.note_file) if a.note_file else '')
        if a.cmd == 'collect':
            return collect(a.msgs, a.wait)
        if a.cmd == 'watches':
            return watches()
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
        if a.cmd == 'cancel':
            return cancel(a.msg, mine)
        if a.cmd == 'watch':   # all stays all, sessions to come included: resolve() would list today's
            return arm(['all'] if a.targets == ['all'] else list(resolve(a.targets)), mine)
        if a.cmd == 'relay':
            return relay(a.addresses, a.entry, a.digest, mine) if a.entry else preview(resolve(a.addresses), mine)
    except Corrupt as e:
        die(f'ledger corrupt, nothing decided until it is reconciled by hand: {e}', 4)
    except hc.Error as e:
        die(str(e), 5)


if __name__ == '__main__':
    sys.exit(main())
