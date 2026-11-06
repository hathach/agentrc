#!/usr/bin/env python3
"""Hand a workflow a saved file whole, so no model copies it on the way.

  state_transfer.py FILE [--raw]

FILE is a saved Workflow output holding {"result": {"state": ...}}: the line is
{digest, state}, digest the state's own. The state must carry its digest and stay
within MAX bytes as compact ASCII JSON, the measure pr-babysit's STATE_MAX takes.
--raw takes FILE's text as it is, such as ledger.py show --snapshot writes: the
line is {digest, text}, digest the text's fnv1a, and parsing it is the reader's.

The workflow runs it under AGENTRC_EMIT and loads the file that names (facts.py's
report, lib/seal.py's emit); stdout gets the same line. Exit 0 with that line;
exit 2 with {"error": ...} when FILE cannot be read, holds no state, the state
exceeds MAX bytes or the arguments are wrong. Never writes but the emitted file.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, fnv1a, report  # noqa: E402

MAX = 64 * 1024


def read(path):
    try:
        with open(path, encoding='utf-8', newline='') as f:  # newline='': the text its digest is taken over, as written
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


def collect(argv):
    p = Parser(prog='state_transfer.py', add_help=False)
    p.add_argument('file')
    p.add_argument('--raw', action='store_true')
    a = p.parse_args(argv)
    if a.raw:
        text = read(a.file)
        return {'digest': fnv1a(text), 'text': text}
    state = state_of(a.file)
    return {'digest': state['digest'], 'state': state}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
