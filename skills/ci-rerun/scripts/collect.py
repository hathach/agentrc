#!/usr/bin/env python3
"""Collect a PR's CI state for a watcher, so no model spends turns waiting or listing.

  collect.py inventory --repo OWNER/NAME --pr N --head SHA [--wait-seconds S]
  collect.py failures --repo OWNER/NAME --pr N --head SHA --check LINK...

inventory: waits up to S seconds (default 0) while any check is pending, then
prints one JSON object {repo, pr, head, baseRef, baseSha, status, waited,
counts, checks, error}. `status` is green, red (a check failed or was
cancelled) or running (still pending, or no checks registered yet). `checks`
lists only the checks that did not pass or skip, each {name, workflow, bucket,
link, attempt}: `attempt` is the per-run id in a link that has one (an Actions
job, a Read the Docs build, a CircleCI job; each re-run mints a new one), and
null for a link that stays the same across runs (a docs preview, a review
bot's page, none), so only a non-null attempt can tell one run from the next.
`repo` must own the PR number, which for a fork PR is not the head repository. Exit 0
with the object, 1 with `error` set (a gh failure, or the PR head is no longer
--head), 2 on a usage error.

failures: for each --check (an inventory link), reads the failure and writes
the evidence for a judge to <tmp>/ci-collect/<repo>/<pr>/<head>/failures-<ns>.json,
one file per call: the job's log without ANSI codes, NULs and timestamps
saved whole, the diagnostic lines of every step that reported an error, and for an Actions job the newest run on the base
branch in which the same job ran, with its conclusion and the diagnostic lines
both share. It prints a compact copy, {head, detail, checks: [{link, attempt,
name, provider, firstError (its first 200 characters), files, complete, base
(with the count of shared lines), error}]}; the lines themselves, each check's
signature and its log path are in the detail file. `complete` is always false here: a log's diagnostic
lines do not prove every failure was listed. A --check that is no longer a
failing check of the head is stale: exit 1 with `stale` listing them, before
anything is read. Read the Docs needs RTD_TOKEN (see rtd.py).
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import circleci  # noqa: E402
import rtd  # noqa: E402

POLL = 30
ATTEMPT = (
    ('actions', re.compile(r'^https://github\.com/[^/]+/[^/]+/actions/runs/\d+/job/(\d+)')),
    ('readthedocs', re.compile(r'^https://app\.readthedocs\.(?:org|com)/projects/[\w-]+/builds/(\d+)/?$')),
    ('circleci', re.compile(r'^https://circleci\.com/gh/[^/]+/[^/]+/(\d+)$')),
)


ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
STAMP = re.compile(r'^\d{4}-\d\d-\d\dT[\d:.]+Z ')
WORKSPACE = re.compile(r'/(?:home/[^/\s]+/actions-runner/_work|home/runner/work)/[^/\s]+/[^/\s]+/')
DIAGNOSTIC = re.compile(r'\b(?:error|Error|ERROR|FAIL|FAILED|Failed|failed|[Aa]ssert\w*)\b')
DURATION = re.compile(r'\s+in \d+(?:\.\d+)?s\b')
FILES = re.compile(r'[\w./-]+\.(?:c|h|cc|cpp|hpp|py|S|s|ld|cmake|mk|ya?ml|json)\b')
KEEP = 20


class Failed(Exception):
    pass


class Stale(Failed):
    def __init__(self, links):
        super().__init__('stale snapshot: no longer failing checks of the head: ' + ', '.join(links))
        self.links = links


def gh(*args, raw=False):
    try:
        done = subprocess.run(['gh', *args], capture_output=True, text=True)
    except OSError as e:
        raise Failed(f'gh: {e}')
    if done.returncode != 0:
        raise Failed(f'gh {args[0]} {args[1]}: {done.stderr.strip() or f"exit {done.returncode}"}')
    if raw:
        return done.stdout
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise Failed(f'gh {args[0]} {args[1]}: not JSON: {done.stdout[:200]!r}')


def pull(repo, pr):
    return gh('pr', 'view', str(pr), '--repo', repo, '--json', 'headRefOid,baseRefName,baseRefOid')


def listing(repo, pr):
    try:
        return gh('pr', 'checks', str(pr), '--repo', repo, '--json', 'name,workflow,bucket,link')
    except Failed as e:
        if 'no checks reported' in str(e):
            return []
        raise


def attempt(link):
    for kind, pattern in ATTEMPT:
        m = pattern.match(link or '')
        if m:
            return f'{kind}:{m.group(1)}'
    return None


def summary(checks):
    counts = {}
    for c in checks:
        counts[c['bucket']] = counts.get(c['bucket'], 0) + 1
    if not checks or counts.get('pending'):
        return 'running', counts
    return ('red' if counts.get('fail') or counts.get('cancel') else 'green'), counts


def inventory(repo, pr, head, wait):
    before = pull(repo, pr)
    if before['headRefOid'] != head:
        raise Failed(f'PR #{pr} head is {before["headRefOid"]}, not {head}')
    start = time.monotonic()
    while True:
        checks = listing(repo, pr)
        status, counts = summary(checks)
        waited = int(time.monotonic() - start)
        if status != 'running' or waited + POLL > wait:
            break
        time.sleep(POLL)
    # The listing reads the PR's last commit: a push during it would describe another head.
    after = pull(repo, pr)['headRefOid']
    if after != head:
        raise Failed(f'PR #{pr} head moved to {after} while collecting')
    listed = [{**c, 'attempt': attempt(c.get('link'))} for c in checks if c['bucket'] not in ('pass', 'skipping')]
    return {'repo': repo, 'pr': pr, 'head': head, 'baseRef': before['baseRefName'], 'baseSha': before['baseRefOid'],
            'status': status, 'waited': waited, 'counts': counts, 'checks': listed, 'error': None}


def clean(text):
    return [STAMP.sub('', line).rstrip() for line in ANSI.sub('', text.replace('\x00', '')).splitlines()]


def failed_steps(lines):
    """Each step that reported an error, from its `Run` group's end through its last `##[error]`,
    headed by that Run line: a continue-on-error or always() step can fail besides the first."""
    last, run = {}, -1
    for i, line in enumerate(lines):
        if line.startswith('##[group]Run '):
            run = i
        elif line.startswith('##[error]'):
            last[run] = i
    out, exit_line = [], None
    for run, end in last.items():
        start = next((i + 1 for i in range(run, end) if lines[i].startswith('##[endgroup]')), run + 1)
        out += [f'== {lines[run][len("##[group]"):]}' if run >= 0 else '== job', *lines[start:end + 1]]
        exit_line = exit_line or lines[end]
    return out, exit_line


def diagnostics(lines):
    seen = []
    for line in lines:
        if line.startswith('##[error]'):
            line = line[len('##[error]'):]
            if line.startswith('Process completed'):
                continue
        elif line.startswith(('##[', '== ')) or not DIAGNOSTIC.search(line):
            continue
        if line not in seen:
            seen.append(line)
    return seen


def signature(line):
    return DURATION.sub('', WORKSPACE.sub('', line or '')).strip()


def evidence(lines, exit_line):
    found = diagnostics(lines)
    first = found[0] if found else exit_line or next((line for line in reversed(lines) if line.strip()), '')
    return {'firstError': first, 'signature': signature(first), 'files': sorted(set(FILES.findall(WORKSPACE.sub('', first)))),
            'diagnostics': found[:KEEP]}


def save(folder, name, lines):
    path = folder / name
    path.write_text('\n'.join(lines) + '\n')
    return str(path)


def actions_log(repo, job):
    return clean(gh('api', f'repos/{repo}/actions/jobs/{job}/logs', '--allow-escape-sequences', raw=True))


def base_run(repo, base_ref, workflow, name, cache):
    """The newest completed run on the base branch in which a job of this name ran."""
    if (workflow, name) not in cache:
        cache[workflow, name] = None
        for run in gh('run', 'list', '--repo', repo, '--branch', base_ref, '--workflow', workflow,
                      '--limit', '20', '--json', 'databaseId,headSha,status'):
            if run['status'] != 'completed':
                continue
            jobs = gh('run', 'view', str(run['databaseId']), '--repo', repo, '--json', 'jobs')['jobs']
            job = next((j for j in jobs if j['name'] == name and j.get('conclusion') not in (None, '', 'skipped', 'cancelled')), None)
            if job:
                cache[workflow, name] = {'sha': run['headSha'], 'runId': run['databaseId'], 'jobId': job['databaseId'],
                                         'conclusion': job['conclusion']}
                break
    return cache[workflow, name]


def actions(repo, head, base_ref, job, folder, cache):
    record = gh('api', f'repos/{repo}/actions/jobs/{job}')
    if record.get('head_sha') != head:
        raise Failed(f'job {job} ran on {record.get("head_sha")}, not the head {head}')
    full = actions_log(repo, job)
    section, exit_line = failed_steps(full)
    entry = {'name': record['name'], 'workflow': record.get('workflow_name', ''), 'runId': record.get('run_id'),
             'log': save(folder, f'actions-{job}.log', full), **evidence(section, exit_line)}
    base = base_run(repo, base_ref, entry['workflow'], record['name'], cache)
    if base and base['conclusion'] == 'failure':
        base_full = actions_log(repo, base['jobId'])
        theirs = diagnostics(failed_steps(base_full)[0])
        ours = {signature(line) for line in entry['diagnostics']}
        base = {**base, 'log': save(folder, f'base-actions-{base["jobId"]}.log', base_full),
                'shared': [line for line in theirs if signature(line) in ours][:KEEP], 'diagnostics': theirs[:KEEP]}
    entry['base'] = base
    return entry


def readthedocs(link, folder):
    build, record, notes, failed = rtd.reason(link, rtd.token())
    lines = [f'error: {record["error"]}'] * bool(record.get('error')) + [f'{h}: {b}' for _, h, b in notes]
    for code, command, tail in failed:
        lines += [f'command exited {code}: {command}', *tail.splitlines()]
    first = (failed[0][2].splitlines() or [''])[-1] if failed else record.get('error') or (notes[0][1] if notes else '')
    return {'name': f'build {build}', 'log': save(folder, f'readthedocs-{build}.log', lines), 'base': None,
            'firstError': first, 'signature': signature(first), 'files': [], 'diagnostics': lines[:KEEP]}


def circle(repo, number, folder):
    lines = []
    for name, tail in circleci.failed_steps(repo, number, 150):
        lines += [f'== {name}', *clean(tail)]
    entry = evidence(lines, None)
    return {'name': f'job {number}', 'log': save(folder, f'circleci-{number}.log', lines), 'base': None, **entry}


def failures(repo, pr, head, links):
    now = inventory(repo, pr, head, 0)
    failing = {c['link']: c for c in now['checks'] if c['bucket'] in ('fail', 'cancel')}
    stale = [link for link in links if link not in failing]
    if stale:
        raise Stale(stale)
    folder = Path(tempfile.gettempdir()) / 'ci-collect' / repo.replace('/', '_') / str(pr) / head
    folder.mkdir(parents=True, exist_ok=True)
    cache, checks = {}, []
    for link in links:
        check = failing[link]
        kind, _, ident = (check['attempt'] or 'other:').partition(':')
        entry = {'link': link, 'attempt': check['attempt'], 'bucket': check['bucket'], 'provider': kind,
                 'name': check['name'], 'complete': False, 'error': None}
        try:
            if kind == 'actions':
                entry.update(actions(repo, head, now['baseRef'], ident, folder, cache))
            elif kind == 'readthedocs':
                entry.update(readthedocs(link, folder))
            elif kind == 'circleci':
                entry.update(circle(repo, ident, folder))
            else:
                entry['error'] = 'no reader for this check: its link names no run'
        except (Failed, rtd.Failed, circleci.Failed) as e:
            entry['error'] = str(e)
        checks.append(entry)
    detail = folder / f'failures-{time.time_ns()}.json'
    detail.write_text(json.dumps({'head': head, 'baseRef': now['baseRef'], 'checks': checks}, indent=1))
    compact = [{k: v for k, v in c.items() if k not in ('diagnostics', 'log', 'signature')} for c in checks]
    for c in compact:
        c['firstError'] = (c.get('firstError') or '')[:200]
        if c.get('base') and 'diagnostics' in c['base']:
            c['base'] = {k: v for k, v in c['base'].items() if k not in ('diagnostics', 'log')}
            c['base']['shared'] = len(c['base']['shared'])
    return {'head': head, 'detail': str(detail), 'checks': compact, 'error': None}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['inventory', 'failures'])
    p.add_argument('--repo', required=True, help='OWNER/NAME of the repository that owns the PR number')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True, help='the head SHA the caller expects')
    p.add_argument('--wait-seconds', type=int, default=0)
    p.add_argument('--check', action='append', default=[], metavar='LINK')
    a = p.parse_args(argv)
    if (a.command == 'failures') != bool(a.check):
        p.error('--check is for failures, which needs at least one')
    if not re.fullmatch(r'[0-9a-f]{40}', a.head):
        p.error('--head must be a full 40-hex SHA')
    try:
        out = (inventory(a.repo, a.pr, a.head, max(0, a.wait_seconds)) if a.command == 'inventory'
               else failures(a.repo, a.pr, a.head, a.check))
        rc = 0
    except Failed as e:
        out, rc = {'repo': a.repo, 'pr': a.pr, 'head': a.head, 'error': str(e)}, 1
        if isinstance(e, Stale):
            out['stale'] = e.links
    print(json.dumps(out))
    return rc


if __name__ == '__main__':
    sys.exit(main())
