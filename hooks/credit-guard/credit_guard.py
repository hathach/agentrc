#!/usr/bin/env python3
"""Claude Code hook: stop a session from spending extra-usage credits.

SessionStart asks /api/oauth/usage once whether the account has extra usage
enabled; when both flags say no, the guard is off for that session. Otherwise
it is armed: UserPromptSubmit blocks the prompt, and PreToolUse denies the tool
with continue:false (the only form that ends `claude -p` before another model
call, measured on 2.1.283), once any plan limit is at 100%.

An armed check reuses the config dir's last reading while it is trusted; the
trust window shrinks with headroom and with the burn rate between readings.
A failed refresh keeps the last reading, retried every FLOOR seconds. Any
other failure while armed blocks: Claude Code lets a hook that errors, prints
nothing or times out proceed. State lives in $CLAUDE_CONFIG_DIR/credit-guard/:
usage.json (the shared reading), readings.log (every fetch, for tuning
HEADROOM) and sessions/<session_id>.off (the session's guard is off).
"""
import fcntl
import json
import math
import os
import re
import sys
import time
from pathlib import Path

URL = 'https://api.anthropic.com/api/oauth/usage'
HTTP_TIMEOUT = 5  # seconds
LOCK_WAIT = 8  # seconds; with HTTP_TIMEOUT it stays inside the 15 s hooks.json timeout
FLOOR = 30  # seconds: shortest trust window, and the retry step after a failed refresh
HEADROOM = ((50, 600), (20, 300), (10, 120), (5, 60))  # (percent left above, seconds trusted)
BURN_SHARE = 4  # trust a reading for at most 1/BURN_SHARE of the projected time to 100%
LOG_MAX = 1 << 20  # bytes; readings.log then moves to readings.log.1


def state_dir():
    d = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'credit-guard'
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch():
    import urllib.request  # here, not at the top: it costs ~17 ms on every hook call
    creds = json.loads((state_dir().parent / '.credentials.json').read_text())
    req = urllib.request.Request(URL, headers={'Authorization': f"Bearer {creds['claudeAiOauth']['accessToken']}",
                                               'anthropic-beta': 'oauth-2025-04-20'})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        data = json.load(r)
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError(f'usage error body: {str(data)[:200]}')
    return data


def credits_off(data):
    """Only an explicit no from both flags turns the guard off."""
    return (data.get('extra_usage') or {}).get('is_enabled') is False and \
        (data.get('spend') or {}).get('enabled') is False


def percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'unusable limit percent {value!r}')
    return value


def limits(data):
    """{limit name: percent used}; raises unless every limit has a usable percent."""
    found = {}
    for lim in data.get('limits') or []:
        scope = lim.get('scope') or {}
        model = (scope.get('model') or {}).get('display_name') if isinstance(scope, dict) else None
        label = model or (json.dumps(scope, sort_keys=True) if scope else '')
        found[lim.get('kind', '?') + (f':{label}' if label else '')] = percent(lim.get('percent'))
    if not found:
        raise ValueError('usage payload has no limits')
    return found


def trust_window(now, used, prev):
    """Seconds a reading stays trusted: by headroom, then cut by the burn rate since prev."""
    left = 100 - max(used.values())
    window = next((secs for above, secs in HEADROOM if left > above), FLOOR)
    elapsed = now - prev['at'] if prev else 0
    if elapsed > 0:
        for name, pct in used.items():
            rate = (pct - prev['used'].get(name, pct)) / elapsed
            if rate > 0:
                window = min(window, (100 - pct) / rate / BURN_SHARE)
    return max(window, FLOOR)


def locked(d):
    """The held usage lock; raises after LOCK_WAIT so the hook still answers before its timeout."""
    f = (d / 'usage.lock').open('w')
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() > deadline:
                f.close()
                raise TimeoutError('usage lock busy')
            time.sleep(0.1)


def load(d):
    """The stored reading {at, used, until, retry_at}, or None when missing or malformed."""
    try:
        rec = json.loads((d / 'usage.json').read_text())
        for key in ('at', 'until', 'retry_at'):
            percent(rec[key])
        if not rec['used']:
            raise ValueError('empty reading')
        for pct in rec['used'].values():
            percent(pct)
        return rec
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def store(d, used, now, prev):
    until = now + trust_window(now, used, prev)
    # begin refreshing FLOOR before the trust window ends
    rec = {'at': now, 'used': used, 'until': until, 'retry_at': max(now + FLOOR, until - FLOOR)}
    (d / 'usage.json').write_text(json.dumps(rec))
    log = d / 'readings.log'
    if log.exists() and log.stat().st_size > LOG_MAX:
        log.replace(d / 'readings.log.1')
    with log.open('a') as f:
        f.write(json.dumps({**rec, 'time': time.strftime('%F %T')}) + '\n')
    return rec


def reading(now):
    """The config dir's current reading; refreshes when due, raises when there is none or on a state error."""
    d = state_dir()
    with locked(d):
        now = max(now, time.time())  # time spent waiting for the lock counts against the reading
        last = load(d)
        if last and now < last['retry_at']:
            return last
        try:
            used = limits(fetch())
        except Exception:
            if not last:
                raise
            # a failed refresh assumes usage has not changed since the last reading
            (d / 'usage.json').write_text(json.dumps({**last, 'retry_at': max(now, time.time()) + FLOOR}))
            return last
        return store(d, used, now, last)


def off_marker(session_id):
    if not re.fullmatch(r'[A-Za-z0-9-]+', session_id or ''):
        raise ValueError(f'unusable session_id {session_id!r}')
    d = state_dir() / 'sessions'
    d.mkdir(exist_ok=True)
    return d / f'{session_id}.off'


def session_start(event, now):
    marker = off_marker(event.get('session_id'))
    marker.unlink(missing_ok=True)  # armed unless this check finishes and says off
    data = fetch()
    if credits_off(data):
        marker.touch()
        return
    d = state_dir()
    with locked(d):  # the first prompt reuses this reading instead of fetching again
        last = load(d)
        if not last or last['at'] <= now:  # another session may have stored a newer one meanwhile
            store(d, limits(data), now, last)


def armed(event):
    try:
        return not off_marker(event.get('session_id')).exists()
    except ValueError:
        return True


def refusal(now):
    """Why an armed session must stop now, or None."""
    full = [name for name, pct in reading(now)['used'].items() if pct >= 100]
    return f'plan limit at 100% ({", ".join(full)}); further use would spend extra-usage credits' if full else None


def block(event_name, why):
    msg = f'credit-guard: {why}'
    if event_name == 'UserPromptSubmit':
        return {'decision': 'block', 'reason': msg}
    out = {'continue': False, 'stopReason': msg}
    if event_name == 'PreToolUse':
        out['hookSpecificOutput'] = {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                     'permissionDecisionReason': msg}
    return out


def handle(event, now=None):
    """The hook's JSON reply for one event, or None to let it proceed."""
    now = time.time() if now is None else now
    name = event.get('hook_event_name')
    if name == 'SessionStart':
        session_start(event, now)
        return None
    if not armed(event):
        return None
    try:
        why = refusal(now)
    except Exception as e:  # noqa: BLE001 - an armed guard that cannot read usage must stop
        why = f'cannot read plan usage ({type(e).__name__}: {e})'
    return block(name, why) if why else None


def main():
    event = {}
    try:
        event = json.load(sys.stdin)
        out = handle(event)
    except Exception as e:  # noqa: BLE001 - see the module docstring: a failed hook must not let a turn through
        name = event.get('hook_event_name') if isinstance(event, dict) else None
        if name == 'SessionStart':
            return  # no off marker was written, so later events stay armed
        out = block(name, f'hook failed ({type(e).__name__}: {e})')
    if out:
        print(json.dumps(out))


if __name__ == '__main__':
    main()
