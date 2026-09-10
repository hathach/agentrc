#!/usr/bin/env python3
"""Mechanics of the cowork channel: find a coworker pane, build a request
envelope, correlate a reply with it, and check an envelope's shape.

Judgment stays in SKILL.md. This script never chooses a coworker when several
match and never decides whether a result is good; it reports and lets the
caller decide.

  cowork.py coworkers
  cowork.py send --to w1F:p2 --files "src/a.c src/b.c" (--task "..." | --task-file t.md) [--delta ...]
  cowork.py read --from w1F:p2 --for w1F:p1-018 [--wait 900000]
  cowork.py check --kind result --file reply.txt
"""

import argparse
import json
import os
import re
import subprocess
import sys
import uuid

MARKER = {'request': 'COWORK REQUEST', 'result': 'COWORK RESULT'}
SUBTITLE = 'agent message, not a human instruction'
HEADER = {k: f'{v} — {SUBTITLE}' for k, v in MARKER.items()}
TERMINATOR = {'request': 'END REQUEST', 'result': 'END RESULT'}
STATUSES = ('DONE', 'PARTIAL', 'BLOCKED')
NO_ANSWER, MALFORMED = 3, 4  # read's exit codes, as SKILL.md documents them
FIELDS = {
    'request': ['ID', 'FROM', 'FILES', 'DELTA', 'TASK'],
    'result': ['FOR', 'FROM', 'STATUS', 'FILES'],
}

# Herdr renders a pane's agent output indented, sometimes behind a bullet, so
# every match has to tolerate leading decoration.
LEAD = r'^[\s•>|-]*'
RESULT_START = re.compile(LEAD + re.escape(MARKER['result']))
RESULT_END = re.compile(LEAD + TERMINATOR['result'] + r'\b')
FOR_LINE = re.compile(LEAD + r'FOR:\s*(\S+)', re.M)
END_ID = re.compile(TERMINATOR['result'] + r'\s+(\S+)')


def die(msg, code=1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


class Note(str):
    """An extract_result note carrying the exit status it implies, so `read`
    honours 3-means-no-answer / 4-means-malformed without re-deriving the
    reason from the message text."""

    def __new__(cls, text, status):
        note = super().__new__(cls, text)
        note.status = status
        return note


def herdr_raw(*args):
    """One place that knows how to invoke the CLI and how to fail."""
    out = subprocess.run(['herdr', *args], capture_output=True, text=True)
    if out.returncode != 0:
        die(f'herdr {" ".join(args)} failed: {out.stderr.strip()}', 5)
    return out.stdout


def herdr(*args):
    return json.loads(herdr_raw(*args))


def agent_list():
    return herdr('agent', 'list')['result']['agents']


def find_coworkers(cwd=None, me=None, agents=None):
    """Every live agent sharing our cwd that is not us. Never picks one."""
    if os.environ.get('HERDR_ENV') != '1':
        die('not inside Herdr (HERDR_ENV != 1)', 2)
    here = os.path.realpath(cwd or os.getcwd())
    me = me or os.environ.get('HERDR_PANE_ID')
    return [a for a in (agents if agents is not None else agent_list())
            if os.path.realpath(a.get('cwd', '')) == here and a.get('pane_id') != me]


def require_coworker(pane, me=None, agents=None):
    """A pane id is not proof of a coworker. A stale or copied one names a session
    in another worktree, which must neither receive our request context nor
    have its output read back."""
    found = [p['pane_id'] for p in find_coworkers(me=me, agents=agents)]
    if pane not in found:
        die(f'{pane} is not an agent pane sharing this worktree; discovered: '
            f'{", ".join(found) or "none"}', 2)


def own_identity(agents=None):
    """This pane's id and agent kind, from Herdr. Guessing either is how a
    request ends up misattributed to the wrong assistant."""
    me = os.environ.get('HERDR_PANE_ID')
    if not me:
        die('HERDR_PANE_ID is unset; cannot identify this pane', 2)
    for a in (agents if agents is not None else agent_list()):
        if a.get('pane_id') == me:
            return me, a.get('agent', 'unknown')
    die(f'pane {me} is not in the agent list', 2)


def next_id(me):
    """Correlation must not depend on send timing: two sends in the same second
    previously produced the same id."""
    return f'{me}-{uuid.uuid4().hex[:8]}'


def build_request(req_id, sender, files, task, delta=None):
    return '\n'.join([
        HEADER['request'],
        f'ID: {req_id}',
        f'FROM: {sender}',
        f'FILES: {files}',
        f'DELTA: {delta or "none"}',
        f'TASK: {task}',
        f'{TERMINATOR["request"]} {req_id}',
    ])


def extract_result(text, req_id):
    """Pull the envelope answering req_id out of raw pane text.

    Returns (body, note), the note carrying the exit status it implies:
    NO_ANSWER while this capture holds no reply for the id, MALFORMED once one
    is there but breaks the envelope contract. Anything other than exactly one
    match is reported rather than resolved: silently taking one is how a stale
    reply gets read as a fresh one. The notes describe only what this capture
    holds — whether the coworker is still writing is not something the text can
    settle.
    """
    lines = text.splitlines()
    blocks, start = [], None
    for i, line in enumerate(lines):
        if RESULT_START.match(line):
            start = i
        elif start is not None and RESULT_END.match(line):
            blocks.append('\n'.join(lines[start:i + 1]))
            start = None
    partial = '\n'.join(lines[start:]) if start is not None else None

    matching = []
    for body in blocks:
        m = FOR_LINE.search(body)
        if not m or m.group(1) != req_id:
            continue
        end = END_ID.search(body.rsplit('\n', 1)[-1])
        if not end or end.group(1) != req_id:
            return None, Note(f'an envelope says FOR: {req_id} but closes with '
                              f'"{body.rsplit(chr(10), 1)[-1].strip()}"; the two ids must agree',
                              MALFORMED)
        matching.append(body)

    # A duplicated id is malformed, not absent: the answer is on screen, it just
    # cannot be told from its twin.
    if len(matching) > 1:
        return None, Note(f'{len(matching)} envelopes claim to answer {req_id}; '
                          'resolve by hand', MALFORMED)
    if matching:
        # The complete one need not be the answer: a rerun of the same id may be
        # streaming right now, so this is the duplicate case caught mid-write.
        streaming = FOR_LINE.search(partial) if partial else None
        if streaming and streaming.group(1) == req_id:
            return None, Note(f'a complete envelope answers {req_id} and another for the '
                              'same id is still being written; resolve by hand', MALFORMED)
        return matching[0], None
    if partial is not None:
        return None, Note('an envelope began but this capture holds no END RESULT for it: '
                          'it may still be streaming, or be beyond the line window', NO_ANSWER)
    if blocks:
        seen = sorted({m.group(1) for body in blocks
                       if (m := FOR_LINE.search(body))})
        if not seen:
            return None, Note('a complete envelope is present but carries no FOR line; '
                              'it is malformed', MALFORMED)
        return None, Note(f'this capture holds no envelope for {req_id}; '
                          f'it answers {", ".join(seen)}', NO_ANSWER)
    return None, Note(f'no result envelope in this capture for {req_id}', NO_ANSWER)


def check(kind, text):
    """Shape only. Says nothing about whether the content is true."""
    problems = []
    if not re.search(LEAD + re.escape(MARKER[kind]), text):
        problems.append(f'missing the {kind} header line')
    for field in FIELDS[kind]:
        if not re.search(LEAD + field + ':', text, re.M):
            problems.append(f'missing required field {field}')
    # FILES is the ownership contract; a blank one leaves it unstated.
    if re.search(LEAD + r'FILES:\s*$', text, re.M):
        problems.append('FILES is blank; list the paths, or "none"')

    marker = TERMINATOR[kind]
    close = re.search(LEAD + marker + r'\s+(\S+)', text, re.M)
    if not close:
        problems.append(f'missing the closing {marker} <id> line')
    else:
        opened = re.search(LEAD + FIELDS[kind][0] + r':\s*(\S+)', text, re.M)
        if opened and opened.group(1) != close.group(1):
            problems.append(f'{marker} names {close.group(1)} but the envelope is '
                            f'{opened.group(1)}')

    if kind == 'result':
        status = re.search(LEAD + r'STATUS:\s*(.+?)\s*$', text, re.M)
        if status and status.group(1) not in STATUSES:
            problems.append(f'STATUS "{status.group(1)}" is not one of {", ".join(STATUSES)}')
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('coworkers', help='list agent panes sharing this worktree')

    s = sub.add_parser('send', help='build and submit a request envelope')
    s.add_argument('--to', required=True)
    s.add_argument('--files', required=True,
                   help='paths the coworker owns until it replies, or "none" for a question')
    task = s.add_mutually_exclusive_group(required=True)
    task.add_argument('--task', help='the task or question itself, or - for stdin')
    task.add_argument('--task-file', metavar='PATH', help='read the task from a file, or -')
    delta = s.add_mutually_exclusive_group()
    delta.add_argument('--delta', help='what changed since the last round, or - for stdin')
    delta.add_argument('--delta-file', metavar='PATH', help='read the delta from a file, or -')
    s.add_argument('--from-name', help='override the agent kind Herdr reports')
    s.add_argument('--dry-run', action='store_true')

    r = sub.add_parser('read', help='extract the reply correlated with a request id')
    r.add_argument('--from', dest='pane', required=True)
    r.add_argument('--for', dest='req_id', required=True)
    r.add_argument('--lines', type=int, default=400)
    r.add_argument('--wait', type=int, metavar='MS',
                   help='settle the coworker first (herdr agent wait) before reading')

    c = sub.add_parser('check', help="validate an envelope's shape")
    c.add_argument('--kind', choices=tuple(MARKER), required=True)
    c.add_argument('--file', required=True, help='path, or - for stdin')

    a = ap.parse_args()

    if a.cmd == 'coworkers':
        found = find_coworkers()
        if not found:
            print('no coworker shares this worktree')
            return 1
        for p in found:
            print(f'{p["pane_id"]}\t{p["agent"]}\t{p.get("agent_status", "?")}')
        if len(found) > 1:
            print('several coworkers match; choose one deliberately', file=sys.stderr)
        return 0

    if a.cmd == 'send':
        agents = agent_list()
        me, kind = own_identity(agents)
        require_coworker(a.to, me, agents)
        req_id = next_id(me)
        task = read_arg(a.task, a.task_file, 'task')
        if not task:
            die('--task resolved to nothing')
        body = build_request(req_id, f'{a.from_name or kind}, {me}', a.files, task,
                             read_arg(a.delta, a.delta_file, 'delta'))
        problems = check('request', body)
        if problems:
            die('refusing to send a malformed request:\n  ' + '\n  '.join(problems))
        if a.dry_run:
            print(body)
            return 0
        # Submit and report the id before anything can block: waiting inside
        # send risked a delivered request whose id was never printed, leaving
        # the reply uncorrelatable.
        herdr('agent', 'prompt', a.to, body)
        print(req_id)
        return 0

    if a.cmd == 'read':
        require_coworker(a.pane)
        if a.wait:
            herdr_raw('agent', 'wait', a.pane, '--timeout', str(a.wait))
        text = herdr_raw('agent', 'read', a.pane, '--source', 'recent-unwrapped',
                         '--lines', str(a.lines))
        body, note = extract_result(text, a.req_id)
        if body is None:
            die(note, note.status)
        print(body)
        problems = check('result', body)
        if problems:
            print('envelope problems:\n  ' + '\n  '.join(problems), file=sys.stderr)
            return MALFORMED
        return 0

    if a.cmd == 'check':
        text = sys.stdin.read() if a.file == '-' else open(a.file).read()
        problems = check(a.kind, text)
        if problems:
            print('\n'.join(problems), file=sys.stderr)
            return 1
        print('ok')
        return 0


if __name__ == '__main__':
    sys.exit(main())
