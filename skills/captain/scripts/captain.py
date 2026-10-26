#!/usr/bin/env python3
"""Mechanics of the captain: find the agent sessions in this Herdr, pin each one's identity, and tell
whether its input line is empty before anything is typed into it.

Judgment stays in SKILL.md. Every check refuses rather than guesses: an identity it cannot pin or a screen it
does not recognize is reported, never assumed safe.

  captain.py list [--json]                       every agent session, asking none of them
  captain.py enroll [--replace]                  make this session the captain
  captain.py ask <target>...                     send each idle target a status request
  captain.py collect [<msg>...] [--wait MS]      print each report as it lands
  captain.py reports [<msg>...]                  the latest report from each target, or those requests' reports
  captain.py cancel <msg>                        close a request or relay no answer will close
  captain.py relay <target>... [--entry <uuid>]  show, then relay, the human's newest message
  captain.py withdraw <action>                   take back a relay
  captain.py open --checkout <dir> --account <main|ada> [--name <name>]
                                                 start a Claude Code session in a new workspace
  captain.py brief --for <msg>                   a target reads what a status request asks
  captain.py report --for <msg> --file <json|->  a target answers a status request
  captain.py accept --action <action>            a target takes a relay and reads its words
  captain.py outcome --action <action> --result <result> [--note-file <file|->]
                                                 a target says how a relay ended

A target is a pane or a selector: pr:N, pr:<owner>:<repo>:N, issue:N, issue:<owner>:<repo>:N, matched against each
live session's latest report, or ws:<workspace id or label>. A selector must match exactly one session; a relay with
--entry names panes only.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import namedtuple
from contextlib import contextmanager

try:
    import fcntl
except ImportError:   # Windows: Herdr and /proc are POSIX-only, so the captain is too
    fcntl = None

CONFIG_DIRS = {'main': None, 'ada': os.path.expanduser('~/.claude-ada')}   # main is CLAUDE_CONFIG_DIR unset
START_MS = 60000
HERDR_TIMEOUT_S = 30
SGR = re.compile(r'\x1b\[([0-9;]*)m')
OTHER_ESCAPE = re.compile(r'\x1b\[[0-9;?]*[A-Za-ln-z]')
RULE = re.compile(r'^─{20,}')  # the top rule may end in a session label: ──── alo68 ─
BLANK = '  \t\r'
AGENT_KINDS = ('claude', 'codex')
CLAUDE_HINTS = re.compile(r'⏵⏵|\? for shortcuts|shift\+tab|esc to interrupt')
STATE = os.environ.get('AGENTRC_CAPTAIN_DIR') or os.path.expanduser('~/.cache/agentrc/captain')
INSTALLED = (os.environ.get('AGENTRC_CAPTAIN_SCRIPT')
             or os.path.expanduser('~/.claude/skills/captain/scripts/captain.py'))
IDENTITY = ('pane_id', 'terminal_id', 'kind', 'cwd', 'session', 'pid', 'started', 'account')
ASKABLE = ('idle', 'done')
REPORT = {'task': str, 'prs': list, 'state': str, 'evidence': str, 'chief': str, 'needs': str, 'next': str}
OPTIONAL = {'issues': list}   # a report without it names no issue
STATES = ('ready', 'in-progress', 'blocked', 'needs-human', 'done', 'idle')
RECORDS = ('request', 'report', 'cancel', 'delivered', 'undelivered', 'uncertain',
           'relay', 'accepted', 'outcome', 'withdraw', 'withdraw-requested')
ITEMS = ('request', 'relay')
CLOSED = ('report', 'cancel', 'undelivered', 'outcome', 'withdraw')
RESULTS = ('completed', 'failed', 'rejected', 'needs-clarification')
REPORT_MAX = 8192
POLL_S = 2
NO_ANSWER = 3
SELECTORS = ('pr:', 'issue:', 'ws:')
SELECTOR = re.compile(r'(pr|issue):(?:([\w.-]+):([\w.-]+):)?(\d+)|ws:(.+)')


def die(msg, code=1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def run_herdr(*args, timeout=HERDR_TIMEOUT_S):
    """A hung herdr must not hold the ledger lock: past timeout it fails like any herdr error."""
    try:
        return subprocess.run(['herdr', *args], capture_output=True, encoding='utf-8', timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(['herdr', *args], 124, '', f'no answer within {timeout} s')


def herdr_text(*args):
    out = run_herdr(*args)
    if out.returncode != 0:
        die(f'herdr {" ".join(args)} failed: {out.stderr.strip()}', 5)
    return out.stdout


def herdr(*args):
    return json.loads(herdr_text(*args))['result']


Cell = namedtuple('Cell', 'char dim')


def styled(line):
    """The Cells of one ANSI line, dim marking faint text: Claude Code draws its suggestions dim."""
    dim = False
    cells = []
    pos = 0
    line = OTHER_ESCAPE.sub('', line)
    for m in SGR.finditer(line):
        cells += [Cell(c, dim) for c in line[pos:m.start()]]
        pos = m.end()
        codes = (m.group(1) or '0').split(';')
        i = 0
        while i < len(codes):
            n = int(codes[i]) if codes[i].isdigit() else 0
            if n in (0, 22):
                dim = False
            elif n == 2:
                dim = True
            elif n in (38, 48):   # 38;2;r;g;b or 38;5;n: their numbers are not codes
                i += 4 if codes[i + 1:i + 2] == ['2'] else 2
            i += 1
    cells += [Cell(c, dim) for c in line[pos:]]
    return cells


def typed(cells):
    """What a human typed on these cells: neither a suggestion (dim) nor blank."""
    return ''.join(c.char for c in cells if not c.dim and c.char not in BLANK)


def plain(cells):
    return ''.join(c.char for c in cells).rstrip(BLANK)


def footer_only(lines):
    """Below the input only Claude Code's own footer may follow: its hint line, and at most one row above it, the
    user's statusline, which may say anything; all indented and without ❯. Anything else, such as a dialog, means
    the box found is not the live input."""
    shown = [plain(cells) for cells in lines if plain(cells)]
    return (1 <= len(shown) <= 2 and all(t[0] in BLANK and '❯' not in t for t in shown)
            and bool(CLAUDE_HINTS.search(shown[-1])))


def input_state(screen):
    """'empty', 'draft' or 'unknown' for the input line on an ANSI screen capture; only 'empty' is safe to type
    into, since herdr's prompt appends to whatever a human has half typed. Claude Code draws its input between
    the last two full-width rules, the first line led by ❯."""
    lines = [styled(line) for line in screen.splitlines()]
    rules = [i for i, cells in enumerate(lines) if RULE.match(plain(cells))]
    if len(rules) < 2 or rules[-1] - rules[-2] < 2 or not footer_only(lines[rules[-1] + 1:]):
        return 'unknown'
    box = lines[rules[-2] + 1:rules[-1]]
    head = [c.char for c in box[0]]
    if not plain(box[0]).lstrip(BLANK).startswith('❯'):
        return 'unknown'
    first = box[0][head.index('❯') + 1:]
    return 'draft' if typed(first) or any(typed(cells) for cells in box[1:]) else 'empty'


def read_screen(pane):
    return herdr_text('agent', 'read', pane, '--source', 'visible', '--ansi')


def stat(pid):
    """The /proc/<pid>/stat fields after the command name, which may itself hold spaces and parentheses."""
    with open(f'/proc/{pid}/stat') as f:
        return f.read().rsplit(')', 1)[1].split()


def started(pid):
    """Process start time in clock ticks since boot: a pid alone may be reused by a restarted agent."""
    return int(stat(pid)[19])


def ancestors(pid):
    """pid and every process above it."""
    chain = []
    while pid > 1:
        chain.append(pid)
        try:
            pid = int(stat(pid)[1])
        except (OSError, ValueError, IndexError):
            break
    return chain


def account(pid):
    """The Claude account a process runs under, from its own environment, never its pane name. main only with
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


def incarnation(pane):
    """The pane's single foreground agent process as (pid, start ticks), else None: with none or several, a
    restart in the same pane could not be told from the session we pinned."""
    procs = herdr('pane', 'process-info', '--pane', pane)['process_info'].get('foreground_processes', [])
    agents = [p for p in procs if p.get('name') in AGENT_KINDS]
    if len(agents) != 1 or agents[0]['name'] != 'claude':
        return None
    pid = agents[0]['pid']
    try:
        return pid, started(pid)
    except (OSError, ValueError, IndexError):
        return None


def canonical(cwd):
    """An absolute cwd resolved, else None: realpath('') would name the captain's own checkout."""
    return os.path.realpath(cwd) if cwd and os.path.isabs(cwd) else None


def sessions(agents=None, workspaces=None, me=None):
    """Every Claude Code pane but me (this pane by default, '' for none), each with a handle for the human. Codex
    panes are left out: Codex runs its commands outside its pane's process tree, so a Codex target could never
    prove which pane answers."""
    agents = herdr('agent', 'list')['agents'] if agents is None else agents
    workspaces = herdr('workspace', 'list')['workspaces'] if workspaces is None else workspaces
    labels = {w['workspace_id']: w.get('label', w['workspace_id']) for w in workspaces}
    me = os.environ.get('HERDR_PANE_ID') if me is None else me
    found = []
    for a in agents:
        kind = a.get('agent')
        if a['pane_id'] == me or kind != 'claude':
            continue
        label = labels.get(a.get('workspace_id'))
        found.append({
            'handle': f'{label or "?"} · {a.get("name") or kind} · {a["pane_id"]}',
            'workspace_id': a.get('workspace_id'),
            'workspace': label,
            'pane_id': a['pane_id'],
            'terminal_id': a.get('terminal_id'),
            'kind': kind,
            'cwd': canonical(a.get('cwd')),
            'session': (a.get('agent_session') or {}).get('value'),
            'status': a.get('agent_status', 'unknown'),
        })
    return found


def pin(session):
    """Add the process incarnation and the account it runs under; None where they cannot be read."""
    inc = incarnation(session['pane_id'])
    session['pid'], session['started'] = inc or (None, None)
    session['account'] = account(inc[0]) if inc else None
    return session


def unpinned(session):
    """The identity fields a pinned session lacks; a session missing any is never typed into."""
    return [k for k in ('cwd', 'terminal_id', 'pid', 'started', 'session', 'account') if not session.get(k)]


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
    return (all(text(ident.get(k)) for k in ('pane_id', 'terminal_id', 'cwd', 'session', 'account'))
            and os.path.isabs(ident['cwd'])
            and all(type(ident.get(k)) is int and ident[k] > 0 for k in ('pid', 'started')))


def text(value):
    return isinstance(value, str) and bool(value)


def records(lenient=False):
    """The ledger's records in order. A torn or garbled line raises Corrupt; lenient (collect and reports, which
    decide nothing) instead stops before a torn last line another process may still be writing."""
    try:
        with open(path('ledger.jsonl'), 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return []
    lines = data.split(b'\n')
    if lines[-1] and not lenient:
        raise Corrupt(f'{path("ledger.jsonl")}: the last line is unterminated')
    out = []
    for n, line in enumerate(lines[:-1], 1):
        try:
            rec = json.loads(line)
        except ValueError:
            raise Corrupt(f'{path("ledger.jsonl")}:{n} is not a JSON record') from None
        if not well_formed(rec):
            raise Corrupt(f'{path("ledger.jsonl")}:{n} is not a well-formed record')
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
    created = not os.path.exists(path('ledger.jsonl'))
    fd = os.open(path('ledger.jsonl'), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
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
        with open(path('enrolled.json'), encoding='utf-8') as f:
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


def find(pane):
    """The pinned session in pane, with its handle and status, or None."""
    for s in sessions(me=''):
        if s['pane_id'] == pane:
            return pin(s)
    return None


def everyone():
    """Every Claude Code session but this one, pinned where it can be."""
    return [pin(s) for s in sessions()]


def selected(match, session, recs):
    """Whether session is one a selector match names: by workspace id or label, or by a PR or issue in the latest
    report this session, as pinned now, gave; a bare number matches it in any repository."""
    kind, owner, repo, number, ws = match.groups()
    if ws is not None:
        return ws in (session.get('workspace_id'), session.get('workspace'))
    last = last_report(session, recs)
    named = last['report'].get('prs' if kind == 'pr' else 'issues', []) if last else []
    if owner:
        return f'{owner}/{repo}#{number}'.lower() in (r.lower() for r in named)
    return any(r.endswith(f'#{number}') for r in named)


def resolve(args):
    """{pane: the argument naming it} for the target arguments, a pane as given, a selector replaced by the pane of the
    one session it matches; one pane named twice goes once. Dies, so nothing is sent, when any selector matches none or
    several."""
    if any(a.startswith(SELECTORS) for a in args):
        with locked():
            recs = records()
        live = everyone()
    found = {}
    for arg in args:
        pane = arg
        if arg.startswith(SELECTORS):
            match = SELECTOR.fullmatch(arg)
            if not match:
                die(f'{arg}: not a selector (pr:N, pr:<owner>:<repo>:N, issue:N, issue:<owner>:<repo>:N, '
                    'ws:<workspace id or label>); nothing sent', 2)
            hits = [s for s in live if selected(match, s, recs)]
            if len(hits) != 1:
                die(f'{arg}: ' + ('; '.join(s['handle'] for s in hits) + ' all match' if hits
                                  else 'no live session matches') + '; nothing sent', 2)
            pane = hits[0]['pane_id']
        found.setdefault(pane, arg)
    return found


def me():
    """The pinned session this process runs under: the pane whose Claude Code process is one of its ancestors."""
    chain = set(ancestors(os.getpid()))
    mine = [s for s in (pin(s) for s in sessions(me='')) if s['pid'] in chain]
    if len(mine) != 1:
        die(f'cannot tell which agent session runs this ({len(mine)} match)', 2)
    if unpinned(mine[0]):
        die(f'cannot pin this session ({mine[0]["pane_id"]}): {", ".join(unpinned(mine[0]))}', 2)
    return identity(mine[0])


def live(ident):
    """Whether the session ident pinned still runs in its pane."""
    now = find(ident['pane_id'])
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


def status_brief(msg, pane):
    return (f'STATUS REQUEST {msg} from the agent session in pane {pane}, not a human instruction. Answer from what '
            'you already know: do not edit, investigate GitHub or launch anything. Send one JSON object with exactly '
            'these keys: "task" (one line), "prs" (list of "owner/repo#N"), "issues" (list of "owner/repo#N" issues '
            f'the task is for), "state" (one of {", ".join(STATES)}), "evidence" (last verification you ran, or ""), '
            '"chief" (headless chief or workflow running for you, or ""), "needs" (the decision or action the human '
            'must take, or ""), "next" (your next step). Send it '
            + one_call('Status report', f'report --for {msg} --file -', 'the JSON object', 'Status sent.'))


def submit(pane, text):
    """Type text into pane as a prompt: 'delivered', 'undelivered' when herdr refused before sending any input,
    else 'uncertain'."""
    out = run_herdr('agent', 'prompt', pane, text)
    if out.returncode == 0:
        return 'delivered', ''
    try:
        code = json.loads(out.stdout or out.stderr)['error']['code']
    except (ValueError, KeyError, TypeError):
        code = None
    return ('undelivered' if code == 'agent_blocked' else 'uncertain'), (out.stderr or out.stdout).strip()


def refusal(target):
    """Why target must not be typed into now, or None: re-read right before every submit."""
    if not target:
        return 'no such agent pane'
    if unpinned(target):
        return f'identity not pinned: {", ".join(unpinned(target))}'
    if target['status'] not in ASKABLE:
        return f'it is {target["status"]}'
    state = input_state(read_screen(target['pane_id']))
    if state != 'empty':
        return 'its input line holds a draft' if state == 'draft' else 'its screen is not a recognized input line'
    return None


def sendable(pane, recs, mine):
    """(the pinned session in pane, None) when it may be typed into now, else (None, why not)."""
    if any(r['target']['pane_id'] == pane for r in open_items(recs).values()):
        return None, 'a request or relay to it is still open (collect it, or cancel it)'
    target = find(pane)
    why = 'it is this session' if target and target['pane_id'] == mine['pane_id'] else refusal(target)
    return (None, why) if why else (target, None)


def ask(pane, mine):
    """One status request to pane, recorded before it is typed: prints the msg id, or why it was not sent."""
    with locked():
        captain = require_captain(mine)
        recs = records()
        target, why = sendable(pane, recs, mine)
        if why:
            return f'{pane}: refused, {why}'
        msg = new_id('m', recs)
        return deliver('request', msg, captain, target, status_prompt(msg))


def deliver(kind, msg, captain, target, prompt, **fields):
    """Record the item, then type its prompt and record how that went: a crash between leaves the item open, never
    typed unrecorded."""
    append({'type': kind, 'msg': msg, 'captain': captain, 'target': identity(target), 'handle': target['handle'],
            **fields})
    pane = target['pane_id']
    outcome, err = submit(pane, prompt)
    append({'type': outcome, 'msg': msg, 'error': err} if err else {'type': outcome, 'msg': msg})
    return f'{pane}: {msg} {outcome}' + (f' ({err})' if err else '')


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
        die(f'{kind} {msg} was sent to {item["target"]["pane_id"]}, not this session as pinned', 2)
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
    print(status_brief(msg, request['captain']['pane_id']))
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


EVENTS = {'cancel': 'cancelled', 'undelivered': 'undelivered', 'withdraw': 'withdrawn', 'accepted': 'accepted'}


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
    """The latest report per target pane, newest first; with msgs, the report answering each of those requests, in
    order, a later report from the same session notwithstanding, and a row naming why for one with none."""
    recs = records(lenient=True)
    requests = {r['msg']: r for r in recs if r['type'] == 'request'}
    answers = {r['msg']: r for r in recs if r['type'] == 'report'}
    if msgs:
        return [row(requests[m], answers[m]) if m in answers else
                {'msg': m, 'missing': 'not answered' if m in requests else 'no such request'} for m in msgs]
    latest = {}
    for rep in answers.values():
        latest[requests[rep['msg']]['target']['pane_id']] = (requests[rep['msg']], rep)
    return [row(req, rep) for req, rep in sorted(latest.values(), key=lambda pair: -pair[1]['at'])]


def row(request, rep):
    return {'handle': request['handle'], 'msg': request['msg'], 'age_s': int(time.time() - rep['at']), **rep['report']}


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


def human_input(entry):
    """Whether a transcript entry is a message the human typed or queued, not hook, skill or task text."""
    origin = entry.get('origin') if isinstance(entry, dict) else None
    return (isinstance(origin, dict) and origin.get('kind') == 'human' and entry.get('type') == 'user'
            and entry.get('promptSource') in ('typed', 'queued') and not entry.get('isMeta')
            and not entry.get('isSidechain'))


def human_inputs(file):
    """(inputs, torn): the human's messages in a transcript, in order, each (uuid, text or None when not plain
    text); torn when a line is not whole JSON, which may hide a newer message."""
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
        if human_input(entry):
            inputs.append((entry.get('uuid'), plain_text((entry.get('message') or {}).get('content'))))
    return inputs, torn


def transcript(captain):
    """The captain's own Claude Code transcript: the human's words are read from it, never passed by the captain."""
    if os.environ.get('CLAUDE_CODE_SESSION_ID') != captain['session']:
        die("relay runs only in the enrolled captain's own Claude Code session", 2)
    config = os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude')
    found = glob.glob(os.path.join(glob.escape(config), 'projects', '*', glob.escape(captain['session']) + '.jsonl'))
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
    if torn:
        print('the transcript ends in a partly written line: relaying refuses until it is whole')
    return entry, words


def preview(targets, mine):
    """The human's newest message and who would get it, targets as resolve() gave them; sends nothing."""
    with locked():
        captain = require_captain(mine)
    entry, words = newest(captain, sending=False)
    for pane, arg in targets.items():
        target = find(pane)
        print(f'to {arg}: {target["handle"] if target else "no such agent pane"}')
    print(f'entry {entry}\n{words}')
    return 0


def last_report(target, recs):
    """The latest report record from target as pinned now, or None: one from an earlier session in the same pane is
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


def relay(panes, entry, mine):
    """Relay the human's newest message, which must still be entry, to each pane, recorded before it is typed.
    The transcript is read once for every pane: a message the human sends after that is not seen."""
    if any(p.startswith(SELECTORS) for p in panes):
        die('a relay names the panes the preview printed, never a selector', 2)
    with locked():
        captain = require_captain(mine)
        latest, quote = newest(captain, sending=True)
        if latest != entry:
            die(f"{entry} is not the human's newest message; preview again", 2)
        for pane in panes:
            recs = records()
            if any(r['type'] == 'relay' and r['entry'] == entry and r['target']['pane_id'] == pane for r in recs):
                print(f'{pane}: refused, this message was relayed to it already', flush=True)
                continue
            target, why = sendable(pane, recs, mine)
            if why:
                print(f'{pane}: refused, {why}', flush=True)
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
    print(f'RELAYED HUMAN INSTRUCTION {action} from the agent session in pane {item["captain"]["pane_id"]}, '
          f'accepted. The human\'s message, verbatim:\n\n{item["quote"]}\n\n{item["context"]}\n'
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
            die(f'{current["pane_id"]} is the enrolled captain and still runs; pass --replace only on the '
                'human\'s word', 2)
        write_json('enrolled.json', {**mine, 'generation': uuid.uuid4().hex})   # A, B, A again strands A's old
    print(f'enrolled {mine["pane_id"]}' + (f', replacing {current["pane_id"]}' if current else ''))
    return 0


def start(name, pane):
    """Start Claude Code in pane: None once herdr sees it ready for input, else herdr's error."""
    out = run_herdr('agent', 'start', name, '--kind', 'claude', '--pane', pane, '--timeout', str(START_MS),
                    timeout=START_MS / 1000 + HERDR_TIMEOUT_S)
    return None if out.returncode == 0 else (out.stderr or out.stdout).strip() or f'exit {out.returncode}'


def not_ready(session, checkout, account_name):
    """Why a session just opened is not the one asked for, idle at an empty input line, or None."""
    if not session:
        return 'no Claude Code session in the pane'
    if unpinned(session):
        return f'identity not pinned: {", ".join(unpinned(session))}'
    if session['cwd'] != checkout:
        return f'it runs in {session["cwd"]}'
    if session['account'] != account_name:
        return f'it runs under account {session["account"]}'
    return refusal(session)


def open_session(checkout, account_name, name, mine):
    """A new workspace on an existing checkout with Claude Code started under the account named; ready only once
    it is pinned there with an empty input line. Anything else, such as a trust prompt, is left for the human:
    nothing is answered, closed or retried."""
    root = canonical(os.path.expanduser(checkout))
    if not root or not os.path.isdir(root):
        die(f'{checkout} is not an existing directory', 2)
    name = name or os.path.basename(root)
    config = CONFIG_DIRS[account_name]
    env = ['--env', f'CLAUDE_CONFIG_DIR={config}'] if config else []
    with locked():   # held to the end: a captain replaced meanwhile must not go on opening
        require_captain(mine)
        pane = herdr('workspace', 'create', '--cwd', root, '--label', name, '--no-focus', *env)['root_pane']['pane_id']
        print(f'{pane}: opened {name} · {root}', flush=True)   # first, so a failure below still names the pane
        err = start(name, pane)
        why = f'Claude Code did not start: {err}' if err else not_ready(find(pane), root, account_name)
    if why:
        print(f'{pane}: not ready: {why}. Look at the pane; nothing was answered or closed')
        return 2
    print(f'{pane}: ready · claude/{account_name}')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    ls = sub.add_parser('list', help='every agent session in this Herdr, without asking any of them')
    ls.add_argument('--json', action='store_true')
    sub.add_parser('enroll', help='make this session the captain').add_argument('--replace', action='store_true')
    sub.add_parser('ask', help='send each target a status request').add_argument('panes', nargs='+')
    c = sub.add_parser('collect', help='print each report as it lands')
    c.add_argument('msgs', nargs='*')
    c.add_argument('--wait', type=int, default=60000, metavar='MS')
    rp = sub.add_parser('reports', help="the latest report from each target, or the named requests' reports")
    rp.add_argument('msgs', nargs='*')
    rp.add_argument('--json', action='store_true')
    sub.add_parser('cancel', help='close a request or relay no answer will close').add_argument('msg')
    rl = sub.add_parser('relay', help="show the human's newest message; with --entry, relay it")
    rl.add_argument('panes', nargs='+')
    rl.add_argument('--entry', metavar='UUID')
    sub.add_parser('withdraw', help='take back a relay').add_argument('action')
    op = sub.add_parser('open', help='start a Claude Code session in a new workspace on an existing checkout')
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
                print(f'{s["handle"]}\t{s["kind"]}/{who}\t{s["status"]}\t{s["cwd"] or "?"}{note}')
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
                print(f'{r["handle"]}\t{one_line(r["task"])}\t{prs}\t{r["state"]}\t'
                      f'{one_line(r["needs"]) or "-"}\t{r["age_s"] // 60} min')
            return 0
        mine = me()
        if a.cmd == 'ask':
            for pane in resolve(a.panes):
                print(ask(pane, mine), flush=True)
            return 0
        if a.cmd == 'cancel':
            return cancel(a.msg, mine)
        if a.cmd == 'relay':
            return relay(a.panes, a.entry, mine) if a.entry else preview(resolve(a.panes), mine)
        if a.cmd == 'withdraw':
            return withdraw(a.action, mine)
        if a.cmd == 'open':
            return open_session(a.checkout, a.account, a.name, mine)
    except Corrupt as e:
        die(f'ledger corrupt, nothing decided until it is reconciled by hand: {e}', 4)


if __name__ == '__main__':
    sys.exit(main())
