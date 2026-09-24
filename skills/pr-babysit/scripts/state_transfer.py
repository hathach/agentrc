#!/usr/bin/env python3
"""Hand pr-babysit a saved state in a form no model can "correct" on the way.

  state_transfer.py FILE [--chunks I,J,...]

FILE is a saved Workflow output holding {"result": {"state": ...}}. The state
is serialized as compact ASCII JSON (every non-ASCII character escaped), cut
into SIZE-byte slices, and each slice base64-encoded on its own, so a slice can
be decoded and checked alone and a mis-copied one asked for again. Each chunk
carries sum, the FNV-1a of its data as the workflow computes it: it locates a
copy error, while the state's own seal stays the check on the whole.

stdout ends with one JSON line {v, digest, length, size, chunks: [{i, data,
sum}]}: the first PER_CALL chunks, or those --chunks names, at most PER_CALL;
a model copying more mangles or truncates them. Exit 0 with that line; exit 2
with {"error": ...} when FILE holds no state, the state exceeds MAX bytes or
the arguments are wrong. Never writes.
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, report  # noqa: E402

VERSION = 1
SIZE = 512
PER_CALL = 4
MAX = 64 * 1024


def fnv1a(text):
    """The workflow's fnv1a: 32-bit FNV-1a over code points, 8 hex digits."""
    h = 0x811c9dc5
    for ch in text:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return f'{h:08x}'


def load(path):
    try:
        with open(path) as f:
            saved = json.load(f)
    except OSError as e:
        raise Unusable(f'cannot read {path}: {e.strerror}')
    except ValueError as e:
        raise Unusable(f'{path} is not JSON: {e}')
    result = saved.get('result') if isinstance(saved, dict) else None
    state = result.get('state') if isinstance(result, dict) else None
    if not isinstance(state, dict):
        raise Unusable(f'{path} holds no result.state object')
    if not isinstance(state.get('digest'), str):
        raise Unusable(f'the state in {path} has no digest')
    return state


def wanted(spec, count):
    try:
        ids = [int(x) for x in spec.split(',')]
    except ValueError:
        raise Unusable(f'--chunks takes comma-separated indexes, not {spec!r}')
    if len(set(ids)) != len(ids) or not all(0 <= i < count for i in ids):
        raise Unusable(f'--chunks {spec} names a duplicate or an index outside 0..{count - 1}')
    if len(ids) > PER_CALL:
        raise Unusable(f'--chunks {spec} names more than {PER_CALL} chunks')
    return ids


def collect(argv):
    p = Parser(prog='state_transfer.py', add_help=False)
    p.add_argument('file')
    p.add_argument('--chunks')
    a = p.parse_args(argv)
    state = load(a.file)
    payload = json.dumps(state, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(payload) > MAX:
        raise Unusable(f'state too large: {len(payload)} bytes (max {MAX})')
    count = -(-len(payload) // SIZE)
    ids = wanted(a.chunks, count) if a.chunks is not None else range(min(count, PER_CALL))
    chunks = []
    for i in ids:
        data = base64.b64encode(payload[i * SIZE:(i + 1) * SIZE]).decode('ascii')
        chunks.append({'i': i, 'data': data, 'sum': fnv1a(data)})
    return {'v': VERSION, 'digest': state['digest'], 'length': len(payload), 'size': SIZE, 'chunks': chunks}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
