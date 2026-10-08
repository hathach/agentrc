#!/usr/bin/env python3
"""Status-line segment for the PRs a Claude Code session is linked to, e.g. `#4066✓ #4164●`.

Claude Code links a session to one PR at a time (its own `gh pr create`, `gh pr view`) and records
each link as a `pr-link` transcript entry; this session's PRs are every PR it ever linked.

  render <cache_dir> <session_id> <transcript>   print the segment from the session's cache only,
                                                 and start a detached refresh when one is due
  refresh <cache_dir> <session_id> <transcript>  rescan the transcript, then poll GitHub once,
                                                 in one GraphQL request, for every PR not merged

Cache `<cache_dir>/<session_id>.json`: {"prs": ["owner/name#N", ...] in first-link order,
"status": {pr: {state, rollup}}, "attempt_at", "retry_after"}.
A failed poll keeps the last-good status.

One flock on `<cache_dir>/.lock` covers each whole refresh, for every session; a refresh that finds
it held leaves without writing, so a later render retries (best effort, no fairness). The
sweep removes caches, temp files a killed worker left and the per-session `.lock` files of the
earlier scheme once idle for KEEP_S; a resumed session rebuilds its cache from the transcript.
"""
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

SESSION_ID = re.compile(r'[A-Za-z0-9-]+')
REPO = re.compile(r'[\w.-]+/[\w.-]+')
PR_ID = re.compile(REPO.pattern + r'#[1-9][0-9]*')
SHOWN = 5
TERMINAL = ('MERGED', 'CLOSED')
REFRESH_S = 120
BACKOFF_S = 300
LOW_POINTS = 200  # the gh budget is shared with chief and pr-babysit: yield it when low
GH_TIMEOUT_S = 20
WORKER_S = 60
KEEP_S = 7 * 86400
SWEPT = re.compile(SESSION_ID.pattern + r'\.(json|lock|json\.[0-9]+\.tmp)')
FIELDS = 'state commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }'
RED, ORANGE, GREEN, DIM, RESET = '\033[91m', '\033[38;5;208m', '\033[92m', '\033[90m', '\033[0m'
BADGE = '\033[38;2;255;193;7m'  # Claude Code's PR badge colour (dark theme `warning`)


def link_id(entry, session_id):
    """`owner/name#N` of a valid github.com pr-link entry of this session, else None."""
    if not isinstance(entry, dict) or entry.get('type') != 'pr-link' or entry.get('sessionId') != session_id:
        return None
    repo, number, url = entry.get('prRepository'), entry.get('prNumber'), entry.get('prUrl')
    if not (isinstance(repo, str) and REPO.fullmatch(repo) and type(number) is int and number > 0
            and isinstance(url, str) and re.fullmatch(rf'https://github\.com/{re.escape(repo)}/pull/{number}/?', url)):
        return None
    return f'{repo}#{number}'


def scan(transcript, session_id):
    """This session's linked PRs in first-link order; re-stamps of the same link never reorder."""
    prs = {}
    with open(transcript, 'rb') as f:
        for line in f:
            if b'pr-link' not in line:
                continue
            try:
                pr = link_id(json.loads(line), session_id)
            except ValueError:
                continue  # a torn last line parses on the next scan
            if pr:
                prs.setdefault(pr)
    return list(prs)


def load(path):
    try:
        cache = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def num(value):
    return value if isinstance(value, (int, float)) else 0


def cached_prs(cache):
    prs = cache.get('prs')
    return [pr for pr in prs if isinstance(pr, str) and PR_ID.fullmatch(pr)] if isinstance(prs, list) else []


def cached_status(cache, prs):
    status = cache.get('status')
    status = status if isinstance(status, dict) else {}
    return {pr: status[pr] for pr in prs if isinstance(status.get(pr), dict)}


def due(cache, now):
    return now - num(cache.get('attempt_at')) >= REFRESH_S and now >= num(cache.get('retry_after'))


def glyph(status):
    """The PR's check rollup: passed, failed, running, or unknown (none yet, or never fetched)."""
    rollup = status.get('rollup')
    if rollup == 'SUCCESS':
        return GREEN + '✓'
    if rollup in ('FAILURE', 'ERROR'):
        return RED + '✗'
    if rollup in ('PENDING', 'EXPECTED'):
        return ORANGE + '●'
    return DIM + '?'


def link(url, text):
    """OSC 8 hyperlink, BEL-terminated as Claude Code writes its own."""
    return f'\033]8;;{url}\a{text}\033]8;;\a'


def render(cache):
    """Newest-linked first; only PRs GitHub confirmed merged or closed are hidden."""
    prs = cached_prs(cache)
    status = cached_status(cache, prs)
    live = [pr for pr in reversed(prs) if status.get(pr, {}).get('state') not in TERMINAL]
    shown = live[:SHOWN]
    qualify = len({pr.split('#')[0] for pr in shown}) > 1
    parts = []
    for pr in shown:
        repo, number = pr.split('#')
        label = pr.split('/', 1)[1] if qualify else '#' + number
        url = f'https://github.com/{repo}/pull/{number}'
        parts.append(f'{BADGE}{link(url, label)}{glyph(status.get(pr, {}))}{RESET}')
    if len(live) > SHOWN:
        parts.append(f'{DIM}+{len(live) - SHOWN}{RESET}')
    return ' '.join(parts)


def publish(path, cache):
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(cache))
    os.replace(tmp, path)


def query(prs):
    """One GraphQL document for every PR: aliased repository fields holding aliased pullRequest fields."""
    repos = {}
    for pr in prs:
        repo, number = pr.split('#')
        repos.setdefault(repo, []).append(int(number))
    fields = []
    for i, (repo, numbers) in enumerate(repos.items()):
        owner, name = repo.split('/')
        pulls = ' '.join(f'p{n}: pullRequest(number: {n}) {{ {FIELDS} }}' for n in numbers)
        fields.append(f'r{i}: repository(owner: "{owner}", name: "{name}") {{ {pulls} }}')
    return 'query { rateLimit { remaining resetAt } ' + ' '.join(fields) + ' }', list(repos.items())


def pr_status(node):
    try:
        state, commits = node['state'], node['commits']['nodes']
        checks = commits[0]['commit']['statusCheckRollup'] if commits else None
        rollup = None if checks is None else checks['state']
    except (KeyError, TypeError, IndexError):
        raise ValueError('unexpected pull request shape')
    if state not in ('OPEN',) + TERMINAL or not isinstance(commits, list) or not isinstance(rollup, (str, type(None))):
        raise ValueError('unexpected pull request fields')
    return {'state': state, 'rollup': rollup}


def fetch(prs):
    """({pr: status} for every PR GitHub returned, epoch to wait for when points run low or None).
    Any error or unexpected shape raises ValueError, so nothing partial replaces the last-good status."""
    doc, repos = query(prs)
    done = subprocess.run(['gh', 'api', 'graphql', '-f', f'query={doc}'], capture_output=True, text=True,
                          timeout=GH_TIMEOUT_S)
    answer = json.loads(done.stdout)
    if done.returncode or not isinstance(answer, dict) or answer.get('errors') or not isinstance(answer.get('data'), dict):
        raise ValueError(f'gh api graphql failed: {done.stderr.strip()[:200]}')
    data, status = answer['data'], {}
    for i, (repo, numbers) in enumerate(repos):
        fields = data.get(f'r{i}')
        if not isinstance(fields, dict) or any(f'p{n}' not in fields for n in numbers):
            raise ValueError(f'incomplete answer for {repo}')
        for n in numbers:
            if fields[f'p{n}'] is not None:  # null is unverified, never terminal
                status[f'{repo}#{n}'] = pr_status(fields[f'p{n}'])
    rate = data.get('rateLimit')
    try:
        remaining, reset = rate['remaining'], datetime.datetime.fromisoformat(rate['resetAt'].replace('Z', '+00:00'))
    except (KeyError, TypeError, AttributeError):
        raise ValueError('no rateLimit')
    if type(remaining) is not int:
        raise ValueError('no rateLimit')
    return status, reset.timestamp() if remaining < LOW_POINTS else None


def refresh(cache_dir, session_id, transcript):
    """Under the directory's flock (the kernel drops it with the worker): recheck, mark the attempt, poll.
    The lock stays held to the last publish; attempt_at is wall-clock, so it cannot exclude a worker."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f'{session_id}.json'
    with open(cache_dir / '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        cache, now = load(path), time.time()
        if not due(cache, now):
            return
        cache['attempt_at'] = now
        publish(path, cache)
        try:
            sweep(cache_dir, now)
        except OSError:
            pass  # best effort
        try:
            cache['prs'] = scan(transcript, session_id)
        except OSError:
            pass  # no transcript: keep the known membership
        prs = cached_prs(cache)
        status = cached_status(cache, prs)
        polled = [pr for pr in prs if status.get(pr, {}).get('state') != 'MERGED']  # closed ones may reopen
        if polled:
            try:
                fresh, wait = fetch(polled)
            except (OSError, ValueError, subprocess.SubprocessError):
                cache['retry_after'] = now + BACKOFF_S  # due() let us in, so no later wait is overwritten
            else:
                status.update(fresh)
                if wait:
                    cache['retry_after'] = wait
        cache['status'] = status
        publish(path, cache)


def sweep(cache_dir, now):
    """Remove this helper's files idle for KEEP_S; a directory fails to unlink, a symlink loses only itself."""
    with os.scandir(cache_dir) as entries:
        for entry in entries:
            try:
                if SWEPT.fullmatch(entry.name) and now - entry.stat(follow_symlinks=False).st_mtime >= KEEP_S:
                    os.unlink(entry.path)
            except OSError:
                pass


def spawn_refresh(cache_dir, session_id, transcript):
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), 'refresh', str(cache_dir), session_id, transcript],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def main(argv):
    if len(argv) == 5 and argv[1] in ('render', 'refresh') and SESSION_ID.fullmatch(argv[3]):
        cache_dir, session_id, transcript = Path(argv[2]), argv[3], argv[4]
        if argv[1] == 'refresh':
            if fcntl:  # no flock (Windows): never refresh
                signal.alarm(WORKER_S)  # bounds the scan and parsing too, not just gh
                refresh(cache_dir, session_id, transcript)
            return 0
        cache, now = load(cache_dir / f'{session_id}.json'), time.time()
        print(render(cache))
        if fcntl and due(cache, now) and (os.path.isfile(transcript) or cached_prs(cache)):
            spawn_refresh(cache_dir, session_id, transcript)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
