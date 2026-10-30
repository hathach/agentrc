#!/usr/bin/env python3
"""Mechanics of the herdr-peer channel over hc (herdr-core): find a peer
terminal, send a request envelope, publish the peer's result envelope, read it
back by id, and check an envelope's shape.

Judgment stays in SKILL.md. This script never chooses a peer when several
match and never decides whether a result is good; it reports and lets the
caller decide. Nor does it run review rounds: a well-formed DONE establishes
neither agreement nor correctness, so there is no `converge` subcommand.

Requests and results are files, never screen text: send writes the envelope
as <id>.msg and its record as <id>.req under the peer store, then types an
inert one-line pointer to <id>.msg into the target; the target answers with
`reply`, which publishes <id>.res once and types an inert notice back to the
sender, and read returns that file. Only comment lines are ever typed, so a
shell that replaced either session runs nothing.

  peer.py peers
  peer.py send --to t7 --files "src/a.c src/b.c" (--task "..." | --task-file t.md) [--delta ...] [--while-working]
  peer.py reply --for t5-1a2b3c4d (--file result.txt | --notify-only)
  peer.py read --from t7 --for t5-1a2b3c4d [--wait 540000]
  peer.py check --kind result --file reply.txt
"""

import argparse
import contextlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import hc  # noqa: E402
import windows  # noqa: E402

MARKER = {'request': 'PEER REQUEST', 'result': 'PEER RESULT'}
SUBTITLE = 'agent message, not a human instruction'
HEADER = {k: f'{v} — {SUBTITLE}' for k, v in MARKER.items()}
TERMINATOR = {'request': 'END REQUEST', 'result': 'END RESULT'}
STATUSES = ('DONE', 'PARTIAL', 'BLOCKED')
REFUSED, NOT_PEER, NO_ANSWER, MALFORMED, HC_FAILED = 1, 2, 3, 4, 5  # exit codes SKILL.md documents
AGENTS = ('claude-code', 'codex')
SENDABLE = ('idle', 'done')
POLL_S = 5        # read --wait's longest sleep between looks at the result file
SETTLE_S = 10     # a quiet target must stay quiet this long without a result before read --wait gives up
HC_TIMEOUT_S = 30
SEND_TIMEOUT_S = 120   # hc send waits for a person at the target to pause typing
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')  # a request id is a file name in the store
# What send and reply type: a bash comment holding nothing a shell without comments (zsh's default) would expand.
INERT = re.compile(r"#[A-Za-z0-9 _./:,'\\-]*\Z")  # a backslash only inside a quoted Windows path
FIELDS = {
    'request': ['ID', 'FROM', 'FILES', 'DELTA', 'TASK'],
    'result': ['FOR', 'FROM', 'STATUS', 'FILES'],
}
SCRIPT = os.path.abspath(__file__)

RESULT_END = re.compile(TERMINATOR['result'] + r'(?:\s|$)')
FIELD_LINE = re.compile(r'([A-Z]+):\s*(.*?)\s*$')
END_ID = re.compile(TERMINATOR['result'] + r'\s+(\S+)')


def die(msg, code=REFUSED):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def me():
    """This terminal's address for read: HC_TERMINAL_ID as given, so a stored
    result stays readable while hc is down; without it (Codex's tool shells
    drop it), the terminal found by ancestry. send and reply always verify
    (verified_me)."""
    addr = os.environ.get('HC_TERMINAL_ID')
    if not addr:
        return verified_me()[0]
    if not SAFE_ID.match(addr):
        die(f'HC_TERMINAL_ID "{addr}" is not a local terminal address', NOT_PEER)
    return addr


def verified_me():
    """(address, hc get record, foreground) of the terminal running this
    process, its foreground an ancestor of this one (hc.own_terminal):
    otherwise any process setting HC_TERMINAL_ID could send or answer as it.
    Outside hc this is the refusal, exit 2."""
    try:
        addr, info, fg = hc.own_terminal(timeout=HC_TIMEOUT_S)
    except hc.Unverified as e:
        die(str(e), NOT_PEER)
    if not SAFE_ID.match(addr):
        die(f'HC_TERMINAL_ID "{addr}" is not a local terminal address', NOT_PEER)
    if not (isinstance(fg['cwd'], str) and os.path.isabs(fg['cwd'])):
        die(f'hc names no absolute cwd for this terminal {addr}', NOT_PEER)
    return addr, info, fg


def store():
    base = os.environ.get('XDG_CACHE_HOME', '')
    path = os.path.join(base if os.path.isabs(base) else os.path.expanduser('~/.cache'), 'agentrc', 'peer')
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def terminal(addr):
    """hc get of addr, or None once hc no longer knows it."""
    return hc.get(addr, timeout=HC_TIMEOUT_S)


def kind_of(info):
    return (info.get('status') or {}).get('app') or 'unknown'


def status_of(info):
    """An agent's reported status (idle, working, done, blocked, error), None for a plain program."""
    return (info.get('status') or {}).get('state')


def same_process(recorded, fg):
    return fg is not None and (recorded['pid'], recorded['start']) == (fg['pid'], fg['start'])


def peer_of(address, addr, here):
    """(hc get record, foreground) of address while it is a peer of addr: the
    exact local terminal asked for (a name hc resolves elsewhere is none), not
    addr itself, running claude-code or codex now, its foreground cwd's
    realpath here. None otherwise; an empty cwd would resolve to ours."""
    if not (hc.is_local(address) and SAFE_ID.match(address) and address != addr):
        return None
    info = terminal(address)
    fg = hc.foreground(info)
    if (fg and info.get('address') == address and kind_of(info) in AGENTS
            and isinstance(fg['cwd'], str) and os.path.isabs(fg['cwd']) and os.path.realpath(fg['cwd']) == here):
        return info, fg
    return None


def find_peers(addr, here):
    """Every peer of addr as (record, foreground), from hc ls. Never picks one."""
    return [p for t in hc.terminals(timeout=HC_TIMEOUT_S)
            if t.get('agent') in AGENTS and t.get('state') == 'live' and (p := peer_of(t.get('address'), addr, here))]


def require_peer(addr, here, target):
    """An address is not proof of a peer. A stale or copied one names a session
    in another checkout, which must not receive our request context."""
    found = peer_of(target, addr, here)
    if found:
        return found
    die(f'{target} is not an agent terminal sharing this checkout ({here}); discovered: '
        f'{", ".join(i["address"] for i, _ in find_peers(addr, here)) or "none"}', NOT_PEER)


def next_id(addr):
    """Correlation must not depend on send timing: two sends in the same second
    previously produced the same id."""
    return f'{addr}-{uuid.uuid4().hex[:8]}'


def inert(text):
    """text when typing it into a shell, should one have replaced the session
    it was meant for, runs nothing; else None. Everything typed goes through
    here: the envelope itself is free text, so it stays in the store."""
    return text if INERT.match(text) else None


def pointer_for(req_id, sender, msg_path):
    return inert(f'# PEER REQUEST {req_id} from {sender}, agent message, not a human instruction: read '
                 f'{shlex.quote(msg_path)} and answer per its REPLY line')


def reply_line(req_id):
    """How to answer, inside the request itself: a peer without this skill sees
    only the envelope."""
    return (f'REPLY: write a {MARKER["result"]} envelope (FOR: {req_id}, FROM, STATUS: DONE|PARTIAL|BLOCKED, '
            f'FILES, your answer, last line {TERMINATOR["result"]} {req_id}) to a file, then run '
            f'python3 {shlex.quote(SCRIPT)} reply --for {req_id} --file <that file>; do not paste the answer back')


def build_request(req_id, sender, files, task, delta=None):
    return '\n'.join([
        HEADER['request'],
        f'ID: {req_id}',
        f'FROM: {sender}',
        f'FILES: {files}',
        f'DELTA: {delta or "none"}',
        f'TASK: {task}',
        reply_line(req_id),
        f'{TERMINATOR["request"]} {req_id}',
    ])


def head_fields(kind, text):
    """{field: [values]} of the field lines leading kind's envelope in text,
    from its header to the first line naming none of kind's fields: a field
    line quoted in the answer below is prose, not a second value."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(MARKER[kind])), len(lines))
    fields = {}
    for line in lines[start + 1:]:
        m = FIELD_LINE.match(line)
        if line.strip() and not (m and m.group(1) in FIELDS[kind]):
            break
        if m:
            fields.setdefault(m.group(1), []).append(m.group(2))
    return fields


def result_ids(text):
    """The distinct values, verbatim, of the FOR lines of a result envelope's head."""
    return sorted(set(head_fields('result', text).get('FOR', [])))


def the_envelope(text, req_id):
    """(the one result envelope in text answering req_id, None), or (None, why
    not). Anything but exactly one complete envelope is refused rather than
    resolved: silently taking one of two is how a stale answer gets stored."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(MARKER['result'])]
    ends = [i for i, line in enumerate(lines) if RESULT_END.match(line)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None, (f'expected exactly one {MARKER["result"]} ... {TERMINATOR["result"]} {req_id} '
                      f'envelope, each line at the start of its line; found {len(starts)} header and '
                      f'{len(ends)} closing lines')
    body = '\n'.join(lines[starts[0]:ends[0] + 1])
    problems = check('result', body)
    ids, end = result_ids(body), END_ID.search(lines[ends[0]])
    if ids != [req_id] or not end or end.group(1) != req_id:
        problems.append(f'it must answer {req_id} alone: FOR gives {" and ".join(ids) or "nothing"}, '
                        f'the closing line "{lines[ends[0]].strip()}"')
    return (None, '; '.join(problems)) if problems else (body + '\n', None)


def check(kind, text):
    """Shape only. Says nothing about whether the content is true. A result's
    fields come from its head (head_fields), so its answer may quote field
    lines; a request's ID, FROM and FILES from every line before its DELTA or
    TASK, since those may span lines and quote a field line."""
    problems = []
    if not re.match(r'\s*' + re.escape(MARKER[kind]), text):
        problems.append(f'missing the {kind} header line')
    if kind == 'result':
        fields = head_fields(kind, text)
    else:
        body = re.search(r'^(?:DELTA|TASK):', text, re.M)
        head = text[:body.start()] if body else text
        fields = {f: re.findall('^' + f + r':[ \t]*(.*?)[ \t]*$',
                                text if f in ('DELTA', 'TASK') else head, re.M)
                  for f in FIELDS[kind]}
    for field in FIELDS[kind]:
        if not fields.get(field):
            problems.append(f'missing required field {field}')
    # FILES is the ownership contract; a blank one leaves it unstated.
    if '' in fields.get('FILES', []):
        problems.append('FILES is blank; list the paths, or "none"')

    marker = TERMINATOR[kind]
    close = re.search('^' + marker + r'[ \t]+(\S+)', text, re.M)
    id_field = FIELDS[kind][0]
    ids = fields.get(id_field, [])
    if '' in ids:
        problems.append(f'{id_field} is blank; it carries the envelope id')
    problems += [f'{id_field} "{v}" is not a single id'
                 for v in sorted(set(ids)) if v and v.split() != [v]]
    opened = next((v.split()[0] for v in ids if v), None)
    if not close:
        problems.append(f'missing the closing {marker} <id> line')
    elif opened and opened != close.group(1):
        problems.append(f'{marker} names {close.group(1)} but the envelope is {opened}')

    if kind == 'result':
        statuses = sorted(set(fields.get('STATUS', [])))
        problems += [f'STATUS "{s}" is not one of {", ".join(STATUSES)}'
                     for s in statuses if s not in STATUSES]
        fors = sorted({v for v in fields.get('FOR', []) if v})
        for field, values in (('FOR', fors), ('STATUS', statuses)):
            if len(values) > 1:
                problems.append(f'the envelope gives {field} as both {" and ".join(values)}')
    return problems


def read_arg(literal, path, flag):
    """Text from --<flag>, file contents from --<flag>-file, stdin from either
    as `-`. A literal that also names a file is refused rather than guessed:
    the probe this replaces read the file whenever a question happened to match
    a path, with no way to say which reading was meant."""
    if path is not None:
        return (sys.stdin.read() if path == '-' else open(path).read()).strip()
    if literal is None:
        return None
    if literal == '-':
        return sys.stdin.read().strip()
    if os.path.exists(literal):
        die(f'--{flag} value "{literal}" also names an existing path; pass '
            f'--{flag}-file {literal} to send its contents, or the text on '
            f'stdin as --{flag} - to send it literally')
    return literal


def paths(req_id):
    """(the JSON record, the stored result, the request envelope as plain text) of req_id."""
    if not SAFE_ID.match(req_id):
        die(f'"{req_id}" is not a request id', NOT_PEER)
    base = os.path.join(store(), req_id)
    return base + '.req', base + '.res', base + '.msg'


def load_record(req_id):
    """The request record; a start time stored as a string (records written
    before hc.proc_start returned an int) is read as the int it spells."""
    req, _, _ = paths(req_id)
    try:
        with open(req, encoding='utf-8') as f:
            record = json.load(f)
        for side in ('sender', 'target'):
            if isinstance(record[side]['start'], str):
                record[side]['start'] = int(record[side]['start'])
        return record
    except FileNotFoundError:
        die(f'no request {req_id} in {store()}', NOT_PEER)
    except (ValueError, KeyError, TypeError) as e:
        die(f'{req} is not a request record: {e!r}', NOT_PEER)


def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.tmp-')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def drop(tmp):
    with contextlib.suppress(FileNotFoundError, PermissionError):  # a scanner holding it: a stray .tmp- file
        windows.retry(lambda: os.unlink(tmp))


def save_record(path, record, new=False):
    """A new record is created once (never over another id's); an update
    replaces it whole, so a peer reading it mid-write sees one version. Both
    retry while another process holds a file open on Windows."""
    tmp = write_atomic(path, json.dumps(record, indent=1) + '\n')
    try:
        if new:
            windows.retry(lambda: os.link(tmp, path))
        else:
            windows.replace(tmp, path)
    finally:
        drop(tmp)


def publish(path, body):
    """Store body at path once: temp file, then a link that fails if path
    exists, never a rename that would replace it. 'stored', 'same' when an
    identical body is already there, or 'conflict'."""
    tmp = write_atomic(path, body)
    try:
        windows.retry(lambda: os.link(tmp, path))
        return 'stored'
    except FileExistsError:
        with open(path, encoding='utf-8') as f:
            return 'same' if f.read() == body else 'conflict'
    finally:
        drop(tmp)


def cmd_peers():
    addr, _, fg = verified_me()
    found = find_peers(addr, os.path.realpath(fg['cwd']))
    if not found:
        print('no peer agent shares this checkout')
        return REFUSED
    for info, _ in found:
        print(f'{info["address"]}\t{kind_of(info)}\t{status_of(info) or "?"}')
    if len(found) > 1:
        print('several peers match; choose one deliberately', file=sys.stderr)
    return 0


def cmd_send(a):
    addr, sender_info, sender_fg = verified_me()
    here = os.path.realpath(sender_fg['cwd'])
    info, fg = require_peer(addr, here, a.to)
    status = status_of(info)
    if status == 'working' and not a.while_working:
        die(f'{a.to} is working; its turn may not be about this request. Wait (hc wait {a.to}) '
            'or pass --while-working to queue it into that turn', REFUSED)
    if status not in SENDABLE + ('working',):
        die(f'{a.to} is {status or "reporting no status"}; it is not sendable '
            '(blocked needs its human, error needs reading)', REFUSED)
    sender_kind = kind_of(sender_info)
    req_id = next_id(addr)
    task = read_arg(a.task, a.task_file, 'task')
    if not task:
        die('--task resolved to nothing')
    body = build_request(req_id, f'{a.from_name or sender_kind}, {addr}', a.files, task,
                         read_arg(a.delta, a.delta_file, 'delta'))
    problems = check('request', body)
    if problems:
        die('refusing to send a malformed request:\n  ' + '\n  '.join(problems))
    if a.dry_run:
        print(body)
        return 0
    if fg['start'] is None or sender_fg['start'] is None:
        print('note: no /proc on this OS; sender and target are recorded by pid only', file=sys.stderr)
    req, _, msg_path = paths(req_id)
    pointer = pointer_for(req_id, addr, msg_path)
    if pointer is None:
        die(f'the store path {msg_path} cannot be written into an inert request line', REFUSED)
    record = {
        'id': req_id, 'checkout': here, 'files': a.files, 'sent_ms': int(time.time() * 1000),
        'sender': {'address': addr, 'kind': sender_kind, 'pid': sender_fg['pid'], 'start': sender_fg['start']},
        'target': {'address': a.to, 'kind': kind_of(info), 'pid': fg['pid'], 'start': fg['start'],
                   'status': status, 'turn': info.get('turn')},
        'delivery': 'pending',
    }
    # Both exist before anything is typed, so a crash mid-send still leaves the
    # id on disk with its delivery unknown rather than nothing.
    # Only a fresh publication counts: an existing file is a collision or a
    # crash's orphan, and a pointer to it would hand the peer an old task.
    if publish(msg_path, body + '\n') != 'stored':
        die(f'{msg_path} already exists; nothing sent, nothing replaced', REFUSED)
    try:
        save_record(req, record, new=True)
    except FileExistsError:
        drop(msg_path)
        die(f'{req} already exists; nothing sent, nothing replaced', REFUSED)
    # --if covers idle and working; `done` is not expressible, so a target that
    # starts a turn between our get and this send still receives the request.
    outcome, detail = hc.send(a.to, pointer, timeout=SEND_TIMEOUT_S,
                              only_if=status if status in ('idle', 'working') else None,
                              was_working=status == 'working', caller=addr)
    if outcome == hc.SENT:
        record.update(delivery='sent', hc=detail)
    else:
        record.update(delivery='not sent' if outcome == hc.NOT_TYPED_OUTCOME else 'uncertain', error=detail)
    save_record(req, record)
    if record['delivery'] == 'not sent':
        die(f'{a.to} was not sent {req_id}: {detail}', REFUSED)
    print(req_id)
    if record['delivery'] == 'uncertain':
        die(f'delivery of {req_id} is uncertain ({detail}); it is recorded, not resent: '
            f'hc read {a.to} before deciding, and a resend is a new request', HC_FAILED)
    return 0


def notice_for(record):
    return inert(f'# PEER NOTICE result for {record["id"]} stored, read it with: python3 {shlex.quote(SCRIPT)} '
                 f'read --from {record["target"]["address"]} --for {record["id"]}')


def notify(record, caller):
    """None once the notice is typed into the sender, else ('failed' or
    'uncertain', why). It is skipped when the sender's session has visibly
    gone; that check cannot see a shell that replaced the session after it,
    or one that ran send itself, so the notice is inert too. caller, the
    verified replying terminal, makes hc tag it as from there."""
    sender = record['sender']
    notice = notice_for(record)
    if notice is None:
        return 'failed', f'the path {SCRIPT} cannot be written into an inert notice'
    try:
        info = terminal(sender['address'])
        if not same_process(sender, hc.foreground(info)):
            return 'failed', f'{sender["address"]} no longer runs the session that sent {record["id"]}'
        outcome, detail = hc.send(sender['address'], notice, timeout=SEND_TIMEOUT_S,
                                  was_working=status_of(info) == 'working', caller=caller)
    except hc.Error as e:
        return 'failed', str(e)
    if outcome == hc.SENT:
        return None
    return ('failed' if outcome == hc.NOT_TYPED_OUTCOME else 'uncertain'), detail


def cmd_reply(a):
    addr, _, fg = verified_me()
    record = load_record(a.req_id)
    target = record['target']
    if addr != target['address']:
        die(f'{a.req_id} was sent to {target["address"]}, not to this terminal {addr}', NOT_PEER)
    if not same_process(target, fg):
        die(f'{a.req_id} was sent to the session in {addr} that was pid {target["pid"]}; this terminal now '
            f'runs pid {fg["pid"]}', NOT_PEER)
    _, res, _ = paths(a.req_id)
    if not a.notify_only:
        if a.file == '-':
            text = sys.stdin.read()
        else:
            with open(a.file, encoding='utf-8') as f:
                text = f.read()
        body, why = the_envelope(text, a.req_id)
        if body is None:
            die(f'refusing to store the reply to {a.req_id}: {why}', MALFORMED)
        outcome = publish(res, body)
        if outcome == 'conflict':
            die(f'a different reply to {a.req_id} is already stored at {res}; it stays', REFUSED)
        if outcome == 'same':
            print(f'this reply to {a.req_id} is already stored; nothing to do (if its notice failed, '
                  f'rerun with --notify-only)')
            return 0
    elif not os.path.exists(res):
        die(f'no result stored for {a.req_id}; reply with --file first', REFUSED)
    failed = notify(record, addr)
    if failed is not None:
        word, why = failed
        die(f'result for {a.req_id} stored; notice to {record["sender"]["address"]} {word}: {why}. '
            f'Retry with: python3 {shlex.quote(SCRIPT)} reply --for {a.req_id} --notify-only'
            + (', unless the sender has already read it' if word == 'uncertain' else ''), HC_FAILED)
    print(f'result for {a.req_id} stored; {record["sender"]["address"]} notified')
    return 0


def stored_result(res, req_id):
    """The stored result, or None while there is none; exits MALFORMED on one
    that is not the single envelope answering req_id."""
    try:
        with open(res, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return None
    body, why = the_envelope(text, req_id)
    if body is None:
        print(text, end='')
        die(f'the stored result for {req_id} is malformed: {why}', MALFORMED)
    return body


def quiet_reason(record, info):
    """(why the target can no longer, or for now does not, work on this
    request, whether that is final), (None, False) while it is working. Only
    idle or done is not final: a turn can end just before the next one, or the
    request, starts."""
    target = record['target']
    addr = target['address']
    if info is None or info.get('state') != 'live':
        return f'{addr} has exited', True
    if not same_process(target, hc.foreground(info)):
        return f'{addr} no longer runs the session the request went to', True
    status = status_of(info)
    if status == 'blocked':
        return f'{addr} is blocked and needs its human', True
    if status == 'error':
        return f'{addr} reports an error; hc read {addr}', True
    if status not in SENDABLE:
        return None, False
    if info.get('turn') == target['turn'] and target['status'] != 'working':
        return (f'{addr} is {status} and never started a turn after the request '
                f'(its delivery was {record.get("delivery")})'), False
    return f'{addr} is {status} without a result', False


def await_result(record, res, wait_ms):
    """Poll until the result is stored, the target can no longer produce it, or
    wait_ms lapse. The file comes first and again after each look at hc, so a
    result stored as the target ends its turn, or exits, is never missed, nor
    one already stored while hc cannot be asked. An idle or done target must
    stay so for SETTLE_S before that counts."""
    target = record['target']['address']
    if record.get('delivery') == 'not sent':
        return None, f'{record["id"]} was never sent: {record.get("error")}'
    deadline = time.monotonic() + wait_ms / 1000
    quiet_since = None
    while True:
        body = stored_result(res, record['id'])
        if body is not None:
            return body, None
        try:
            info, failure = terminal(target), None
        except hc.Error as e:
            info, failure = None, e
        body = stored_result(res, record['id'])
        if body is not None:
            return body, None
        if failure:
            raise failure
        reason, final = quiet_reason(record, info)
        now = time.monotonic()
        quiet_since = (now if quiet_since is None else quiet_since) if reason else None
        if final or (reason and now - quiet_since >= SETTLE_S):
            return None, reason
        left = deadline - now
        if left <= 0:
            return None, f'no result for {record["id"]} after {wait_ms} ms; {reason or f"{target} is working"}'
        time.sleep(min(POLL_S, left))


def cmd_read(a):
    addr = me()
    record = load_record(a.req_id)
    if record['sender']['address'] != addr:
        die(f'{a.req_id} was sent by {record["sender"]["address"]}, not by this terminal {addr}', NOT_PEER)
    if record['target']['address'] != a.target:
        die(f'{a.req_id} went to {record["target"]["address"]}, not {a.target}', NOT_PEER)
    _, res, _ = paths(a.req_id)
    if a.wait:
        body, note = await_result(record, res, a.wait)
    else:
        body = stored_result(res, a.req_id)
        note = f'no result stored for {a.req_id} yet; its delivery was {record.get("delivery")}'
    if body is None:
        die(note, NO_ANSWER)
    print(body, end='')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('peers', help='list agent terminals sharing this checkout')

    s = sub.add_parser('send', help='record and type a request envelope into a peer')
    s.add_argument('--to', required=True, help='the peer terminal, e.g. t7')
    s.add_argument('--files', required=True,
                   help='paths the peer owns until it replies, or "none" for a question')
    task = s.add_mutually_exclusive_group(required=True)
    task.add_argument('--task', help='the task or question itself, or - for stdin')
    task.add_argument('--task-file', metavar='PATH', help='read the task from a file, or -')
    delta = s.add_mutually_exclusive_group()
    delta.add_argument('--delta', help='what changed since the last round, or - for stdin')
    delta.add_argument('--delta-file', metavar='PATH', help='read the delta from a file, or -')
    s.add_argument('--while-working', action='store_true',
                   help='send even though the peer is mid-turn; it arrives as its next input')
    s.add_argument('--from-name', help='override the agent kind hc reports')
    s.add_argument('--dry-run', action='store_true')

    p = sub.add_parser('reply', help='store the result envelope for a request sent to you and notify its sender')
    p.add_argument('--for', dest='req_id', required=True)
    how = p.add_mutually_exclusive_group(required=True)
    how.add_argument('--file', help='the result envelope, or - for stdin')
    how.add_argument('--notify-only', action='store_true', help='resend the notice for a stored result')

    r = sub.add_parser('read', help='print the stored result for a request you sent')
    r.add_argument('--from', dest='target', required=True)
    r.add_argument('--for', dest='req_id', required=True)
    r.add_argument('--wait', type=int, metavar='MS',
                   help='wait until the result is stored, the peer can no longer answer, or MS lapse')

    c = sub.add_parser('check', help="validate an envelope's shape")
    c.add_argument('--kind', choices=tuple(MARKER), required=True)
    c.add_argument('--file', required=True, help='path, or - for stdin')

    a = ap.parse_args()

    if a.cmd == 'check':
        text = sys.stdin.read() if a.file == '-' else open(a.file).read()
        problems = check(a.kind, text)
        if problems:
            print('\n'.join(problems), file=sys.stderr)
            return 1
        print('ok')
        return 0

    try:
        if a.cmd == 'peers':
            return cmd_peers()
        return {'send': cmd_send, 'reply': cmd_reply, 'read': cmd_read}[a.cmd](a)
    except hc.Error as e:
        die(str(e), HC_FAILED)


if __name__ == '__main__':
    sys.exit(main())
