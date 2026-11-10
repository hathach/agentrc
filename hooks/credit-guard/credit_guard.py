#!/usr/bin/env python3
"""Claude Code hook: stop a session from spending extra-usage credits.

SessionStart asks /api/oauth/usage once whether the account has extra usage
enabled; when both flags say no, the guard is off for that session. Otherwise
it is armed: once any plan limit is at 100%, UserPromptSubmit and PreToolUse
wait in the hook until the limits reset, then let the call through. A session
that cannot wait (no budget recorded at its start, a reset beyond the budget,
a refused usage read) is stopped instead: UserPromptSubmit blocks the prompt,
and PreToolUse denies the tool with continue:false (the only form that ends
`claude -p` before another model call, measured on 2.1.283).

Claude Code keeps the hook commands and timeouts its process started with
(measured on 2.1.322) and lets a timed-out or killed hook's call through. So
the registration passes its own timeout as `--timeout N`, which a session
then runs with however settings.json changes, and a waiting hook gives up
WAIT_MARGIN before it; a command without it never waits.

An armed check reuses the config dir's last reading while it is trusted; the
trust window shrinks with headroom and with the burn rate between readings.
A failed refresh keeps the last reading, retried after FLOOR seconds backing
off to SLICE. Any other failure while armed blocks: Claude Code lets a hook
that errors, prints nothing or times out proceed. State lives in $CLAUDE_CONFIG_DIR/credit-guard/:
usage.json (the shared reading), readings.log (every fetch, for tuning
HEADROOM), sessions/<session_id>.off (the session's guard is off) and waits/<pid>.json
(a hook waiting now; `credit_guard.py status` lists them).
"""
import contextlib
import errno
import json
import math
import os
import re
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows; a failed import would exit 1, which lets the turn through
    fcntl = None
    import msvcrt

URL = 'https://api.anthropic.com/api/oauth/usage'
HTTP_TIMEOUT = 5  # seconds
LOCK_WAIT = 8  # seconds; with HTTP_TIMEOUT it stays inside the 15 s hooks.json timeout
FLOOR = 30  # seconds: shortest trust window, and the first retry step after a failed refresh
HEADROOM = ((50, 600), (20, 300), (10, 120), (5, 60))  # (percent left above, seconds trusted)
BURN_SHARE = 4  # trust a reading for at most 1/BURN_SHARE of the projected time to 100%
LOG_MAX = 1 << 20  # bytes; readings.log then moves to readings.log.1
MAX_WAIT = 8 * 86400  # seconds a hook waits for a full limit to reset: a weekly limit plus a day
WAIT_MARGIN = 300  # seconds a waiting hook keeps below its registered timeout; hooks.json's is MAX_WAIT + this
WAIT_EVENTS = ('UserPromptSubmit', 'PreToolUse')
SLICE = 300  # seconds: longest sleep between a waiting hook's checks, and its record's heartbeat
RESET_SLACK = 60  # seconds past resets_at before the first check


def guard_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'credit-guard'


def state_dir():
    d = guard_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch():
    import urllib.request  # here, not at the top: it costs ~17 ms on every hook call
    creds = json.loads((state_dir().parent / '.credentials.json').read_text(encoding='utf-8'))
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


def limit_name(lim):
    scope = lim.get('scope') or {}
    model = (scope.get('model') or {}).get('display_name') if isinstance(scope, dict) else None
    label = model or (json.dumps(scope, sort_keys=True) if scope else '')
    return lim.get('kind', '?') + (f':{label}' if label else '')


def limits(data):
    """{limit name: percent used}; raises unless every limit has a usable percent."""
    found = {limit_name(lim): percent(lim.get('percent')) for lim in data.get('limits') or []}
    if not found:
        raise ValueError('usage payload has no limits')
    return found


def resets(data):
    """{limit name: epoch seconds of its reset, or None when missing or unparseable}."""
    import datetime  # here, not at the top: only a fetch needs it
    found = {}
    for lim in data.get('limits') or []:
        try:
            found[limit_name(lim)] = datetime.datetime.fromisoformat(lim['resets_at']).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            found[limit_name(lim)] = None
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


@contextlib.contextmanager
def locked(d):
    """The held usage lock; raises after LOCK_WAIT so the hook still answers before its timeout."""
    f = (d / 'usage.lock').open('w')
    deadline = time.monotonic() + LOCK_WAIT
    if not fcntl:  # msvcrt locks are mandatory and per handle: lock a byte past the (empty) content
        os.lseek(f.fileno(), 1 << 30, os.SEEK_SET)
    while True:
        try:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EDEADLOCK):  # held elsewhere; anything else is a failure
                        f.close()
                        raise
                    raise BlockingIOError from None
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                f.close()
                raise TimeoutError('usage lock busy')
            time.sleep(0.1)
    try:
        yield f
    finally:
        try:
            if not fcntl:  # Windows frees the lock of a closed handle only "after a delay"
                os.lseek(f.fileno(), 1 << 30, os.SEEK_SET)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f.close()


def load(d):
    """The stored reading {at, used, resets, until, retry_at}, or None when missing or malformed; `at` is its fetch's
    time, kept when a later refresh fails. A reading without usable resets has them unknown."""
    try:
        rec = json.loads((d / 'usage.json').read_text(encoding='utf-8'))
        for key in ('at', 'until', 'retry_at'):
            percent(rec[key])
        if not rec['used']:
            raise ValueError('empty reading')
        for pct in rec['used'].values():
            percent(pct)
        got = rec.get('resets')
        rec['resets'] = {k: v for k, v in got.items() if v is None or isinstance(v, (int, float))
                         and not isinstance(v, bool) and math.isfinite(v)} if isinstance(got, dict) else {}
        fails = rec.get('fails')
        rec['fails'] = fails if isinstance(fails, int) and not isinstance(fails, bool) and fails > 0 else 0
        return rec
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def store(d, used, reset, now, prev):
    until = now + trust_window(now, used, prev)
    # begin refreshing FLOOR before the trust window ends
    rec = {'at': now, 'used': used, 'resets': reset, 'until': until, 'retry_at': max(now + FLOOR, until - FLOOR)}
    full = full_limits(rec)
    if full and (at := reset_at(rec, full)):  # nothing changes before the reset: hooks reuse it until then
        rec['until'] = rec['retry_at'] = max(rec['retry_at'], at + RESET_SLACK)
    (d / 'usage.json').write_text(json.dumps(rec))
    log = d / 'readings.log'
    if log.exists() and log.stat().st_size > LOG_MAX:
        log.replace(d / 'readings.log.1')
    with log.open('a', newline='\n') as f:  # not %F %T: MSYS2's Python formats them as nothing
        f.write(json.dumps({**rec, 'time': time.strftime('%Y-%m-%d %H:%M:%S')}) + '\n')
    return rec


def reading(now):
    """(the config dir's current reading, the exception its refresh raised or None); refreshes when due, raises when
    there is no reading or on a state error."""
    d = state_dir()
    with locked(d):
        now = max(now, time.time())  # time spent waiting for the lock counts against the reading
        last = load(d)
        if last and now < last['retry_at']:
            return last, None
        try:
            data = fetch()
            used, reset = limits(data), resets(data)
        except Exception as e:  # noqa: BLE001 - any failed refresh falls back to the last reading
            if not last:
                raise
            # a failed refresh assumes usage has not changed since the last reading; retries back off to SLICE
            fails = last['fails'] + 1
            retry = max(now, time.time()) + min(FLOOR * 2 ** min(fails - 1, 4), SLICE)
            (d / 'usage.json').write_text(json.dumps({**last, 'retry_at': retry, 'fails': fails}))
            return last, e
        return store(d, used, reset, now, last), None


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
            store(d, limits(data), resets(data), now, last)


def armed(event):
    try:
        return not off_marker(event.get('session_id')).exists()
    except ValueError:
        return True


def wait_budget(timeout):
    """Seconds a hook registered with this timeout may wait: less WAIT_MARGIN, at most MAX_WAIT; 0 without one."""
    return max(0, min(MAX_WAIT, timeout - WAIT_MARGIN)) if timeout else 0


def refused(err):
    """The HTTP status of a usage read the server refused (401, 403), or None: nothing in the hook renews a login."""
    code = getattr(err, 'code', None)
    return code if code in (401, 403) else None


def full_limits(rec):
    return [name for name, pct in rec['used'].items() if pct >= 100]


def reset_at(rec, full):
    """When the last of the full limits resets, or None when any of them has no known reset."""
    times = [rec['resets'].get(name) for name in full]
    return max(times) if times and None not in times else None


def local_time(t):
    lt = time.localtime(t)
    off = lt.tm_gmtoff // 60
    return time.strftime('%Y-%m-%d %H:%M', lt) + f' {"+-"[off < 0]}{abs(off) // 60:02}{abs(off) % 60:02}'


def duration(secs):
    m = int(secs) // 60
    return f'{m // 1440}d{m // 60 % 24}h{m % 60:02}m' if m >= 1440 else f'{m // 60}h{m % 60:02}m'


def full_reason(rec, full):
    at = reset_at(rec, full)
    when = f', resets {local_time(at)}' if at else ''
    return f'plan limit at 100% ({", ".join(full)}{when}); further use would spend extra-usage credits'


def windows():
    """lib/windows.py, whose retries outlast a reader holding a file open there; imported only where needed."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'lib'))
    import windows as w
    return w


def write_atomic(path, rec):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(rec))
    windows().replace(tmp, path)


def waiters(tag=None):
    """The records of hooks waiting now, those of the run tagged tag when given; one not refreshed for two slices
    is a hook that died."""
    d, found = guard_dir() / 'waits', []  # not created: a reader must not make it
    for f in sorted(d.glob('*.json')) if d.is_dir() else []:
        try:
            rec = json.loads(f.read_text(encoding='utf-8'))
            if time.time() - rec['seen'] < 2 * SLICE + 60 and (tag is None or rec.get('tag') == tag):
                found.append(rec)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return found


def wait(event, rec, budget):
    """Wait in the hook until every limit is back below 100%: the reply that lets the call through, or the block
    when the reset lies beyond the budget, the budget runs out or the usage read is refused."""
    name = event.get('hook_event_name')
    deadline = time.monotonic() + budget
    since, polls = time.time(), 0
    d = state_dir() / 'waits'
    d.mkdir(exist_ok=True)
    path = d / f'{os.getpid()}.json'
    try:
        while True:
            full = full_limits(rec)
            at, left = reset_at(rec, full), deadline - time.monotonic()
            first = at + RESET_SLACK if at else 0  # the first usage read a known reset allows
            if first - time.time() > left:
                return block(name, f'{full_reason(rec, full)}; beyond this hook\'s wait ({duration(left)} left)')
            if left <= 0:
                return block(name, f'{full_reason(rec, full)}; still full after waiting {duration(time.time() - since)}')
            if time.time() < first:
                pause = first - time.time()
            else:  # past the reset, or none known: poll, backing off from FLOOR to SLICE
                pause, polls = min(FLOOR * 2 ** polls, SLICE), min(polls + 1, 4)
            pause = min(pause, SLICE, left)
            write_atomic(path, {'pid': os.getpid(), 'session_id': event.get('session_id'),
                                'agent_id': event.get('agent_id'), 'event': name, 'limits': full, 'since': since,
                                'seen': time.time(), 'next_check': max(time.time() + pause, first),
                                'tag': os.environ.get('CREDIT_GUARD_TAG')})
            time.sleep(pause)
            if time.time() < first:
                continue  # a slice of a known wait: the record's heartbeat, no usage read
            rec, err = reading(time.time())
            if code := refused(err):
                return block(name, f'{full_reason(rec, full_limits(rec) or full)}; usage read refused (HTTP {code}) '
                                   'while waiting')
            # a reading counts only while trusted: a failed refresh keeps an expired one
            if not full_limits(rec) and time.time() < rec['until']:
                return {'systemMessage': f'credit-guard: waited {duration(time.time() - since)} for '
                                         f'{", ".join(full)} to reset'}
    finally:
        windows().retry(lambda: path.unlink(missing_ok=True))


def block(event_name, why):
    msg = f'credit-guard: {why}'
    if event_name == 'UserPromptSubmit':
        return {'decision': 'block', 'reason': msg}
    out = {'continue': False, 'stopReason': msg}
    if event_name == 'PreToolUse':
        out['hookSpecificOutput'] = {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                     'permissionDecisionReason': msg}
    return out


def handle(event, now=None, timeout=None):
    """The hook's JSON reply for one event, or None to let it proceed; timeout is the hook's registered one."""
    now = time.time() if now is None else now
    name = event.get('hook_event_name')
    if name == 'SessionStart':
        session_start(event, now)
        return None
    if not armed(event):
        return None
    try:
        rec, err = reading(now)
        if not (full := full_limits(rec)):
            return None
        code = refused(err)
        budget = wait_budget(timeout) if name in WAIT_EVENTS and not code else 0
        if budget:
            return wait(event, rec, budget)
        return block(name, full_reason(rec, full) + (f'; usage read refused (HTTP {code})' if code else ''))
    except Exception as e:  # noqa: BLE001 - an armed guard that cannot read usage must stop
        return block(name, f'cannot read plan usage ({type(e).__name__}: {e})')


def status():
    for w in waiters():
        print(f'pid {w.get("pid")} session {w.get("session_id")} {w.get("event")}'
              f'{" agent " + str(w["agent_id"]) if w.get("agent_id") else ""}: waiting '
              f'{duration(time.time() - w["since"])} for {", ".join(w.get("limits") or [])}, '
              f'next check {local_time(w["next_check"])}')


def main():
    args = sys.argv[1:]
    if args == ['status']:
        return status()
    event = {}
    try:
        timeout = None
        if args:  # only `--timeout N`, the registered hook timeout in seconds
            if len(args) != 2 or args[0] != '--timeout' or not args[1].isdigit():
                raise ValueError(f'usage: credit_guard.py [--timeout SECONDS | status], not {args}')
            timeout = int(args[1])
        event = json.load(sys.stdin.buffer)
        out = handle(event, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - see the module docstring: a failed hook must not let a turn through
        name = event.get('hook_event_name') if isinstance(event, dict) else None
        if name == 'SessionStart':
            return  # no off marker was written, so later events stay armed
        out = block(name, f'hook failed ({type(e).__name__}: {e})')
    if out:
        print(json.dumps(out))


if __name__ == '__main__':
    main()
