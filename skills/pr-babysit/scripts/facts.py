"""What pr-babysit's scripts share: the {"error"} line contract and strict reads.

A gh call GitHub refused for a rate limit raises RateLimited; a script whose
caller reads it (report(limits=True)) also prints rateLimit {observedAt, resetAt,
secondary} on that error line: resetAt the latest reset of a resource `gh api
rate_limit` (which spends no quota) shows exhausted, null when it shows none, and
secondary when none is exhausted or gh names a secondary limit."""

import argparse
import json
import os
import re
import subprocess
import time

FULL_SHA = re.compile(r'\A[0-9a-f]{40}\Z')  # \Z: $ would let a trailing newline through
# The human is the sole author: no line may credit an agent, model, tool or session, nor link a Claude or ChatGPT session anywhere.
# ASCII on purpose: \b ends at a non-ASCII letter, and Unicode case folds (ſession) are not recognized spellings.
ATTRIBUTION = tuple(re.compile(p, re.I | re.ASCII) for p in (
    r'^[ \t]*co-authored-by[ \t]*:',
    r'^[ \t]*(([a-z]+-)+session(-[a-z]+)*|session-(url|id|link))[ \t]*:',
    r'^[ \t]*(🤖[ \t]*)?(generated|authored|written|created|made)[ \t-]*(with|by)[ \t]*:?[ \t]*\[?'
    r'(claude|codex|chatgpt|gpt|copilot|openai|anthropic|an? (ai|llm|agent))\b',
    r'claude\.ai/code/session_|chatgpt\.com/(c|codex|share)/',
))


def attribution_in(text):
    """The first line of text that credits an agent or links a session, stripped; None when there is none."""
    return next((line.strip() for line in text.split('\n') if any(p.search(line) for p in ATTRIBUTION)), None)


class Unusable(Exception):
    pass


class RateLimited(Unusable):
    def __init__(self, message, limit):
        super().__init__(message)
        self.limit = limit


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


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Unusable(f'usage: {message}')


def run(*argv, ok=(0,)):
    """(exit status, stdout); a status outside ok, or stdout that is not UTF-8, is Unusable."""
    try:
        done = subprocess.run(argv, capture_output=True)
    except FileNotFoundError:
        raise Unusable(f'{argv[0]} is not installed')
    if done.returncode not in ok:
        err = done.stderr.decode(errors='replace').strip()
        message = f"{' '.join(argv)}: {err}"
        limit = rate_limit(err) if argv[0] == 'gh' else None
        raise RateLimited(message, limit) if limit else Unusable(message)
    try:
        return done.returncode, done.stdout.decode()
    except UnicodeDecodeError:
        # Replacing the bad bytes could make two different paths read as one.
        raise Unusable(f"{' '.join(argv)}: output is not UTF-8")


def attempt(*argv, input=None):
    """(exit status, stdout, stderr), replacing bad bytes: unlike run, a failure is part of the receipt."""
    try:
        # bytes in: a text-mode stdin on Windows writes each \n as \r\n
        done = subprocess.run(argv, capture_output=True, input=None if input is None else input.encode('utf-8', 'replace'))
    except FileNotFoundError:
        raise Unusable(f'{argv[0]} is not installed')
    text = lambda b: b.decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n')  # as text mode reads it
    return done.returncode, text(done.stdout), text(done.stderr)


def fnv1a(text):
    """The workflow's fnv1a: 32-bit FNV-1a over code points, 8 hex digits."""
    h = 0x811c9dc5
    for ch in text:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return f'{h:08x}'


def sealed(facts):
    """facts plus the seal pr-babysit checks a relayed copy against: fnv1a over the
    canonical JSON with null members left out, so a copy that drops one still matches."""
    def bare(v):
        # Keys in UTF-16 code-unit order, as JavaScript's sort() compares them.
        if isinstance(v, dict):
            return {k: bare(v[k]) for k in sorted(v, key=lambda k: k.encode('utf-16-be', 'surrogatepass')) if v[k] is not None}
        if isinstance(v, list):
            return [bare(x) for x in v]
        return v
    # Top-level error stays out: a checked line has none, and a relay may fill in error: ''.
    text = json.dumps(bare({k: v for k, v in facts.items() if k != 'error'}), separators=(',', ':'), ensure_ascii=False)
    # JSON.stringify escapes a lone surrogate; ensure_ascii=False would hash it raw.
    return {**facts, 'seal': fnv1a(re.sub('[\ud800-\udfff]', lambda m: f'\\u{ord(m.group()):04x}', text))}


def git(*argv):
    return run('git', *argv)[1]


def checkout_top():
    """The checkout's top level, Unusable unless it is the current directory."""
    top = git('rev-parse', '--show-toplevel').strip()
    if os.path.realpath(top) != os.path.realpath('.'):
        raise Unusable(f'run from the checkout top level {top}, not {os.getcwd()}')
    return top


def report(collect, argv, seal=False, limits=False):
    """Print collect(argv) as the last stdout line, sealed when asked, and return 0, or {"error"} and 2."""
    try:
        facts = collect(argv)
    except Unusable as e:
        print(json.dumps({'error': str(e), **({'rateLimit': e.limit} if limits and isinstance(e, RateLimited) else {})}))
        return 2
    print(json.dumps(sealed(facts) if seal else facts))
    return 0
