#!/usr/bin/env python3
"""Hand a workflow a saved file whole, so no model copies it on the way.

  state_transfer.py FILE

FILE is a saved Workflow output holding {"result": {"state": ...}}: the line is
{digest, state, checkout?}, digest the state's own. The state must carry its digest and stay
within MAX bytes as compact ASCII JSON, the measure pr-babysit's STATE_MAX takes; MAX bounds the
memory a load takes, not how far a state may grow (#539). The state goes as saved, its long arrays
still packed (facts.CHUNKS): the line crosses the same VM boundary, and the workflow unpacks it.
checkout, when the state pins a checkoutDir other than '.', is {dir, same, seal}
(seal: facts.sealed): same whether git names this directory's worktree top level
and dir's as one, false when either cannot be read.

Under AGENTRC_EMIT a state over PART bytes, whose one data module could pass the runtime's limit
on a Workflow script file (524288 bytes in Claude Code 2.1.322), comes instead as
{digest, parts, checkout?}: its compact ASCII JSON cut in order into strings of PART // 2
characters, at most PART bytes once escaped, each the line of its own data module
<AGENTRC_EMIT>.partN.js (N from 1), created before the line as that file is claimed, so one already
there makes the line an error.

The workflow runs it under AGENTRC_EMIT and loads the file that names (facts.py's
report, lib/seal.py's emit), which then gets the line instead of stdout. Exit 0
with that line; exit 2 with {"error": ...} when FILE cannot be read, holds no
state, the state exceeds MAX bytes, a part's file cannot be created or the
arguments are wrong. Never writes but the emitted files.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import EMIT_TO, Parser, Unusable, attempt, emit_beside, report, sealed  # noqa: E402

MAX = 8 * 1024 * 1024
PART = 384 * 1024


def read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError as e:
        raise Unusable(f'cannot read {path}: {e.strerror}')
    except UnicodeDecodeError:
        raise Unusable(f'{path} is not UTF-8')


def state_of(path):
    try:
        saved = json.loads(read(path))
    except ValueError as e:
        raise Unusable(f'{path} is not JSON: {e}')
    result = saved.get('result') if isinstance(saved, dict) else None
    state = result.get('state') if isinstance(result, dict) else None
    if not isinstance(state, dict):
        raise Unusable(f'{path} holds no result.state object')
    if not isinstance(state.get('digest'), str):
        raise Unusable(f'the state in {path} has no digest')
    text = json.dumps(state, ensure_ascii=True, separators=(',', ':'))
    if len(text) > MAX:
        raise Unusable(f'state too large: {len(text)} bytes (max {MAX})')
    return state, text


def cut(text):
    """text in order, in strings that stay within PART bytes as a JSON string: compact ASCII JSON escapes only '"' and
    '\\' there, each to two bytes."""
    return [text[at:at + PART // 2] for at in range(0, len(text), PART // 2)]


def parts_of(text):
    try:
        return {'parts': [emit_beside(f'.part{n}.js', piece) for n, piece in enumerate(cut(text), 1)]}
    except OSError as e:
        raise Unusable(f'cannot create {e.filename}: {e.strerror}')


def top_level(*at):
    code, out, _ = attempt('git', *at, 'rev-parse', '--show-toplevel')
    return out.strip() if code == 0 and out.strip() else None


def checkout_of(state):
    """Whether this directory is in the worktree the state's checkoutDir names, sealed apart from the state, which a
    resume that names no checkoutDir inherits on it (#517); None when the state pins none but '.'."""
    config = state.get('config')
    pinned = config.get('checkoutDir') if isinstance(config, dict) else None
    if not isinstance(pinned, str) or pinned == '.':
        return None
    here = top_level()
    return sealed({'dir': pinned, 'same': here is not None and here == top_level('-C', pinned)})


def collect(argv):
    p = Parser(prog='state_transfer.py', add_help=False)
    p.add_argument('file')
    a = p.parse_args(argv)
    state, text = state_of(a.file)
    checkout = checkout_of(state)
    whole = {'state': state} if EMIT_TO is None or len(text) <= PART else parts_of(text)
    return {'digest': state['digest'], **whole, **({'checkout': checkout} if checkout else {})}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
