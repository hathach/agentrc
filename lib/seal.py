"""The seal pr-babysit checks a script's fact line against, the rate-limit facts beside a refused gh call, and the
line's way to the workflow (emit).

One copy for every script whose line a relay runs: pr-babysit's facts.py, ci-rerun's collect.py and
pr-reply's reply.py.
"""

import json
import os
import re
import subprocess
import sys
import time

RATE_LIMITED = re.compile(r'rate limit|HTTP 429', re.I)


def utc(seconds):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


def rate_limit(stderr):
    """rateLimit for a gh failure GitHub refused for a rate limit, else None."""
    if not RATE_LIMITED.search(stderr):
        return None
    try:
        resources = json.loads(subprocess.run(['gh', 'api', 'rate_limit'], capture_output=True).stdout)['resources']
        spent = [r['reset'] for r in resources.values() if r.get('remaining') == 0 and isinstance(r.get('reset'), int)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        spent = []
    return {'observedAt': utc(time.time()), 'resetAt': utc(max(spent)) if spent else None,
            'secondary': not spent or 'secondary' in stderr.lower()}


def fnv1a(text):
    """The workflow's fnv1a: 32-bit FNV-1a over code points, 8 hex digits."""
    h = 0x811c9dc5
    for ch in text:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return f'{h:08x}'


def canonical(v, nulls=False):
    """The workflow's canonical JSON: keys in UTF-16 code-unit order, as JavaScript's sort() compares them, and null
    members left out unless nulls."""
    def bare(v):
        if isinstance(v, dict):
            return {k: bare(v[k]) for k in sorted(v, key=lambda k: k.encode('utf-16-be', 'surrogatepass')) if nulls or v[k] is not None}
        if isinstance(v, list):
            return [bare(x) for x in v]
        return v
    text = json.dumps(bare(v), separators=(',', ':'), ensure_ascii=False)
    # JSON.stringify escapes a lone surrogate; ensure_ascii=False would hash it raw.
    return re.sub('[\ud800-\udfff]', lambda m: f'\\u{ord(m.group()):04x}', text)


def sealed(facts):
    """facts plus the seal pr-babysit checks a relayed copy against: fnv1a over the
    canonical JSON with null members left out, so a copy that drops one still matches."""
    # Top-level error stays out: a checked line has none, and a relay may fill in error: ''.
    return {**facts, 'seal': fnv1a(canonical({k: v for k, v in facts.items() if k != 'error'}))}


# The exit status when the file AGENTRC_EMIT names cannot be claimed: a file there is an earlier run's, and this run
# does nothing.
EMIT_REFUSED = 3


def claim():
    """Claim the file AGENTRC_EMIT names before the script does anything: created empty with O_EXCL, so a rerun that
    meets an earlier run's file, finished or cut short, exits EMIT_REFUSED at once with nothing done; popped, so no
    child the script starts (a hook running these scripts) claims it too."""
    path = os.environ.pop('AGENTRC_EMIT', None)
    if not path:
        return None, None
    try:
        return path, os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as e:
        why = f'AGENTRC_EMIT {path}: {e.strerror}: nothing was run'
        print(why, file=sys.stderr)
        print(json.dumps({'error': why}))
        sys.exit(EMIT_REFUSED)


EMIT_TO, emit_fd = claim()


def module_text(path, line):
    """The data module a workflow loads with workflow({scriptPath}) for line written to path: compact ASCII JSON."""
    return ("export const meta = { name: 'emit', description: 'a script line' }\n"
            f"return {json.dumps({'emit': path, 'line': line}, separators=(',', ':'))}\n")


def emit(line, code):
    """Print line as the last stdout line and return code; under AGENTRC_EMIT write it instead to the file claimed at
    import, as a data module a workflow loads with workflow({scriptPath}) (`return {"emit": <that path>, "line":
    line}`, compact ASCII JSON), so no model copies it, and print only a note naming that file. A claimed file a run
    leaves empty is one that died before its line."""
    global emit_fd
    if emit_fd is None:
        print(json.dumps(line))
        return code
    fd, emit_fd = emit_fd, None
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write(module_text(EMIT_TO, line))
    print(f'the line is in {EMIT_TO}')
    return code
