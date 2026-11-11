#!/usr/bin/env python3
"""Hand a workflow a saved file whole, so no model copies it on the way.

  state_transfer.py FILE

FILE is a saved Workflow output holding {"result": {"state": ...}}: the line is
{digest, state, checkout?}, digest the state's own. The state must carry its digest and stay
within MAX bytes as compact ASCII JSON, the measure pr-babysit's STATE_MAX takes.
checkout, when the state pins a checkoutDir other than '.', is {dir, same, seal}
(seal: facts.sealed): same whether git names this directory's worktree top level
and dir's as one, false when either cannot be read.

The workflow runs it under AGENTRC_EMIT and loads the file that names (facts.py's
report, lib/seal.py's emit), which then gets the line instead of stdout. Exit 0
with that line; exit 2 with {"error": ...} when FILE cannot be read, holds no
state, the state exceeds MAX bytes or the arguments are wrong. Never writes but
the emitted file.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, attempt, report, sealed  # noqa: E402

MAX = 64 * 1024


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
    size = len(json.dumps(state, ensure_ascii=True, separators=(',', ':')))
    if size > MAX:
        raise Unusable(f'state too large: {size} bytes (max {MAX})')
    return state


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
    state = state_of(a.file)
    checkout = checkout_of(state)
    return {'digest': state['digest'], 'state': state, **({'checkout': checkout} if checkout else {})}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
