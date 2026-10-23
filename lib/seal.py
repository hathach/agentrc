"""The seal pr-babysit checks a relayed fact line against, and the rate-limit facts beside a refused gh call.

One copy for every script whose line a relay carries: pr-babysit's facts.py, ci-rerun's collect.py and
pr-reply's reply.py.
"""

import json
import re
import subprocess
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
