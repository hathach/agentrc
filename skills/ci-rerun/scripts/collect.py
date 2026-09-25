#!/usr/bin/env python3
"""Collect a PR's CI state for a watcher, so no model spends turns waiting or listing.

  collect.py inventory --repo OWNER/NAME --pr N --head SHA [--wait-seconds S]
  collect.py failures --repo OWNER/NAME --pr N --head SHA --check LINK... [--prior-head SHA]
  collect.py remember --repo OWNER/NAME --pr N --head SHA < VERDICTS.json
  collect.py recall --repo OWNER/NAME --pr N --head SHA --check LINK...

inventory: waits up to S seconds (default 0) while any check is pending, then
prints one JSON object {head, status, pending, checks, error}. `status` is
green, red (a check failed or was cancelled) or running (still pending, or no
checks registered yet); `pending` counts the pending checks. `checks` lists
the failed and cancelled ones, each {name, workflow, bucket, link, attempt}:
`attempt` is the per-run id in a link that has one (an Actions job, a Read the
Docs build, a CircleCI job; each re-run mints a new one), and null for a link
that stays the same across runs (a docs preview, a review bot's page, none),
so only a non-null attempt can tell one run from the next. `repo` must own the
PR number, which for a fork PR is not the head repository. Exit 0 with the
object, 1 with `error` set (a gh failure, or the PR head is no longer --head),
2 on a usage error.

failures: for each --check (an inventory link), reads the failure and writes
the evidence for a judge to <tmp>/ci-collect/<repo>/<pr>/<head>/failures-<ns>.json,
one file per call, and prints {head, detail, error}. Each check's entry holds
its saved `log` and the diagnostic lines of every step that reported an error.
The log is the job's whole log without ANSI codes, NULs and timestamps for an
Actions job, but only tails for the others: the last 150 lines of each failed
CircleCI step, and Read the Docs' notes with 40-line tails of failed commands.
For an Actions job the entry also holds the newest run on the base branch in
which the same job ran, with its conclusion and the diagnostic lines both
share, and `cells`. Cells are an adapter for tinyusb's test/hil/hil_test.py:
each terminal failure row (`Failed:` or `Flash Failed:`) of its result table,
`[<time> ]<board>  <test>  ...  <outcome>`, as {cell "<board> <test>",
signature ("<cell>: " and the outcome up to the two spaces before the board's
captured output, without workspace paths and durations), lineNo, line, files,
onBase, baseLineNo, baseLine, prior}; line and baseLine are 500-character
previews of rows the saved logs hold whole. onBase compares the base run's row
for the cell: same-failure, other-failure, passed, other (a skip), not-run, or
null without a comparable base run. A signature is the error's first segment,
so two different errors can share one: same-failure and prior are leads. With
--prior-head, prior lists the verdicts stored for the same check, cell and
signature on that head, [{head, verdict, firstError}], more than one when
ambiguous, and is absent when none match. A cell row is left out of
`diagnostics` and `shared`, on both sides; `firstError` still reads it. A failed
step that ran hil_test.py but printed no parsed row gets `cellsError` instead.
When any check was read, the detail file lists the PR's `changed`
paths (null with `changedError` when they could not be read or may be
incomplete), and the PR head is read again after them. A --check that is no longer a failing check of the head is a stale
snapshot: exit 1, before anything is read. Read the Docs needs RTD_TOKEN (see rtd.py).

remember: stores a judge's verdicts for the head beside its evidence, so a
later launch recalls them instead of carrying them in its state. It reads a
JSON list of {link, bucket, failures} on stdin, replaces any stored entry for
the same link, and prints {head, error}. recall prints {head, verdicts, error}:
the stored entries for the --check links it has, unchanged; a link it has none
for is left out. The caller checks what comes back against its own digests.
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
    ('circleci', re.compile(r'^https://circleci\.com/gh/[^/]+/[^/]+/(\d+)$')),
)


ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
STAMP = re.compile(r'^\d{4}-\d\d-\d\dT[\d:.]+Z ')
WORKSPACE = re.compile(r'/(?:home/[^/\s]+/actions-runner/_work|home/runner/work)/[^/\s]+/[^/\s]+/')
DIAGNOSTIC = re.compile(r'\b(?:error|Error|ERROR|FAIL|FAILED|Failed|failed|[Aa]ssert\w*)\b')
DURATION = re.compile(r'\s+in \d+(?:\.\d+)?s\b')
FILES = re.compile(r'[\w./-]+\.(?:c|h|cc|cpp|hpp|py|S|s|ld|cmake|mk|ya?ml|json)\b')
KEEP = 20
SHA = re.compile(r'[0-9a-f]{40}')
HIL_ROW = re.compile(r'^(?:\d+\.\d{3} )?(\S+)\s+(\S+)\s+\.\.\.\s+(\S.*)$')  # HIL_PROFILE=1 prefixes epoch seconds
HIL_FAILED = ('Failed:', 'Flash Failed:')
LINE = 500


class Failed(Exception):
    pass


def gh(*args, raw=False):
    try:
        done = subprocess.run(['gh', *args], capture_output=True)
    except OSError as e:
        raise Failed(f'gh: {e}')
    if done.returncode != 0:
        err = done.stderr.decode(errors='replace').strip()
        raise Failed(f'gh {args[0]} {args[1]}: {err or f"exit {done.returncode}"}')
    # A job log is whatever the job printed; anything parsed stays strict.
    if raw:
        return done.stdout.decode(errors='replace')
    try:
        return json.loads(done.stdout.decode())
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
    m = rtd.BUILD.match(link or '')
    return f'readthedocs:{m.group(3)}' if m else None


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
    return {'head': head, 'baseRef': before['baseRefName'], 'status': status, 'counts': counts, 'checks': listed}


def printed(inv):
    """What the caller reads: the failing checks by name, the pending ones by count."""
    return {'head': inv['head'], 'status': inv['status'], 'pending': inv['counts'].get('pending', 0),
            'checks': [c for c in inv['checks'] if c['bucket'] in ('fail', 'cancel')], 'error': None}


def evidence_dir(repo, pr, head):
    path = Path(tempfile.gettempdir()) / 'ci-collect' / repo.replace('/', '_') / str(pr) / head
    path.mkdir(parents=True, exist_ok=True)
    return path


def stored_verdicts(folder):
    try:
        return json.loads((folder / 'verdicts.json').read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise Failed(f'verdicts.json: {e}')


def remember(repo, pr, head, text):
    try:
        entries = json.loads(text)
    except ValueError as e:
        raise Failed(f'verdicts on stdin: {e}')
    if not (isinstance(entries, list) and all(isinstance(e, dict) and sorted(e) == ['bucket', 'failures', 'link'] and
                                              isinstance(e['link'], str) and isinstance(e['bucket'], str) and
                                              isinstance(e['failures'], list) for e in entries)):
        raise Failed('verdicts on stdin must be a list of {link, bucket, failures}')
    folder = evidence_dir(repo, pr, head)
    stored = stored_verdicts(folder)
    stored.update((e['link'], e) for e in entries)
    tmp = folder / f'verdicts.json.{time.time_ns()}'
    tmp.write_text(json.dumps(stored, ensure_ascii=False))
    tmp.replace(folder / 'verdicts.json')
    return {'head': head, 'error': None}


def recall(repo, pr, head, links):
    stored = stored_verdicts(evidence_dir(repo, pr, head))
    return {'head': head, 'verdicts': [stored[link] for link in links if link in stored], 'error': None}


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


def files_in(line):
    return sorted(set(FILES.findall(WORKSPACE.sub('', line))))


def evidence(lines, exit_line, omit=()):
    """The first error and the diagnostics; `omit` lines are left out of the list, never out of firstError."""
    found = diagnostics(lines)
    first = found[0] if found else exit_line or next((line for line in reversed(lines) if line.strip()), '')
    return {'firstError': first, 'signature': signature(first), 'files': files_in(first),
            'diagnostics': [line for line in found if line not in omit][:KEEP]}


def hil_rows(lines):
    """{cell: (outcome, line number, line)}, the last row per cell: a retried cell's final result."""
    rows = {}
    for n, line in enumerate(lines, 1):
        m = HIL_ROW.match(line)
        if m:
            rows[f'{m[1]} {m[2]}'] = (m[3], n, line)
    return rows


def cell_signature(cell, outcome):
    # The runner prints `Failed: <error>`, then two spaces before the board's captured
    # output, a `COMMAND FAILED:` tail or the duration: only the error is the same run to run.
    return f'{cell}: {signature(outcome.split("  ")[0])}'


def failed_rows(lines):
    return {cell: row for cell, row in hil_rows(lines).items() if row[0].startswith(HIL_FAILED)}


def on_base(cell, sig, theirs):
    if theirs is None:
        return 'not-run'
    if theirs[0].startswith(HIL_FAILED):
        return 'same-failure' if cell_signature(cell, theirs[0]) == sig else 'other-failure'
    return 'passed' if theirs[0].startswith('OK') else 'other'


def cells(failed, base_rows):
    """One record per failed cell; base_rows is None without a comparable base log."""
    out = []
    for cell, (outcome, n, line) in failed.items():
        sig, theirs = cell_signature(cell, outcome), (base_rows or {}).get(cell)
        out.append({'cell': cell, 'signature': sig, 'lineNo': n, 'line': line[:LINE], 'files': files_in(line),
                    'onBase': None if base_rows is None else on_base(cell, sig, theirs),
                    'baseLineNo': theirs[1] if theirs else None, 'baseLine': theirs[2][:LINE] if theirs else None})
    return out


def save(folder, name, lines):
    path = folder / name
    path.write_text('\n'.join(lines) + '\n')
    return str(path)


def actions_log(repo, job):
    return clean(gh('api', f'repos/{repo}/actions/jobs/{job}/logs', '--allow-escape-sequences', raw=True))


def base_run(repo, base_ref, workflow, name, cache):
    """The newest completed run on the base branch in which a job of this name ran."""
    runs = cache.setdefault('runs', {})
    if workflow not in runs:
        runs[workflow] = [run for run in gh('run', 'list', '--repo', repo, '--branch', base_ref, '--workflow', workflow,
                                             '--limit', '20', '--json', 'databaseId,headSha,status')
                           if run['status'] == 'completed']
    for run in runs[workflow]:
        if 'jobs' not in run:
            run['jobs'] = gh('run', 'view', str(run['databaseId']), '--repo', repo, '--json', 'jobs')['jobs']
        job = next((j for j in run['jobs'] if j['name'] == name and j.get('conclusion') not in (None, '', 'skipped', 'cancelled')), None)
        if job:
            return {'sha': run['headSha'], 'runId': run['databaseId'], 'jobId': job['databaseId'], 'conclusion': job['conclusion']}
    return None


def actions(repo, head, base_ref, job, folder, cache):
    record = gh('api', f'repos/{repo}/actions/jobs/{job}')
    if record.get('head_sha') != head:
        raise Failed(f'job {job} ran on {record.get("head_sha")}, not the head {head}')
    full = actions_log(repo, job)
    section, exit_line = failed_steps(full)
    failed = failed_rows(full)
    entry = {'name': record['name'], 'workflow': record.get('workflow_name', ''), 'runId': record.get('run_id'),
             'runAttempt': record.get('run_attempt'), 'log': save(folder, f'actions-{job}.log', full),
             **evidence(section, exit_line, {row[2] for row in failed.values()})}
    base = base_run(repo, base_ref, entry['workflow'], record['name'], cache)
    base_rows = None
    if base and (base['conclusion'] == 'failure' or (failed and base['conclusion'] == 'success')):
        base_full = actions_log(repo, base['jobId'])
        base_rows = hil_rows(base_full)
        base = {**base, 'log': save(folder, f'base-actions-{base["jobId"]}.log', base_full)}
        if base['conclusion'] == 'failure':
            theirs = [line for line in diagnostics(failed_steps(base_full)[0]) if line not in {row[2] for row in base_rows.values()}]
            ours = {signature(line) for line in entry['diagnostics']}
            base.update(shared=[line for line in theirs if signature(line) in ours][:KEEP], diagnostics=theirs[:KEEP])
    entry['base'] = base
    entry['cells'] = cells(failed, base_rows)
    if not failed and any('hil_test.py' in line for line in section):
        entry['cellsError'] = 'a failed hil_test.py step printed no result row this reads; read its log'
    return entry


def readthedocs(link, folder, cache):
    if 'token' not in cache:
        cache['token'] = rtd.token()
    build, record, notes, failed = rtd.reason(link, cache['token'])
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


def prior_verdicts(repo, pr, head, entries):
    """Each cell's stored verdicts on another head, matched on check, cell and signature."""
    index = {}
    for stored in stored_verdicts(evidence_dir(repo, pr, head)).values():
        for f in stored.get('failures') or []:
            index.setdefault((f.get('check'), f.get('cell'), f.get('signature')), []).append(f)
    for entry in entries:
        for c in entry.get('cells') or []:
            found = index.get((entry['name'], c['cell'], c['signature']))
            if found:
                c['prior'] = [{'head': head, 'verdict': f.get('verdict'), 'firstError': f.get('firstError')} for f in found]


def changed_paths(repo, pr):
    """(the PR's paths, None) or (None, why). GitHub lists at most 3000 files."""
    try:
        names = [f['filename'] for page in gh('api', '--paginate', '--slurp', f'repos/{repo}/pulls/{pr}/files?per_page=100') for f in page]
    except (Failed, KeyError, TypeError) as e:
        return None, f'the PR files could not be read: {e}'
    if len(names) >= 3000:
        return None, f'GitHub listed {len(names)} files, its cap: the list may be incomplete'
    return names, None


def failures(repo, pr, head, links, prior_head=None):
    now = inventory(repo, pr, head, 0)
    failing = {c['link']: c for c in now['checks'] if c['bucket'] in ('fail', 'cancel')}
    stale = [link for link in links if link not in failing]
    if stale:
        raise Failed('stale snapshot: no longer failing checks of the head: ' + ', '.join(stale))
    folder = evidence_dir(repo, pr, head)
    cache, checks = {}, []
    for link in links:
        check = failing[link]
        kind, _, ident = (check['attempt'] or 'other:').partition(':')
        entry = {'link': link, 'attempt': check['attempt'], 'bucket': check['bucket'], 'provider': kind,
                 'name': check['name'], 'error': None}
        try:
            if kind == 'actions':
                entry.update(actions(repo, head, now['baseRef'], ident, folder, cache))
            elif kind == 'readthedocs':
                entry.update(readthedocs(link, folder, cache))
            elif kind == 'circleci':
                entry.update(circle(repo, ident, folder))
            else:
                entry['error'] = 'no reader for this check: its link names no run'
        except (Failed, rtd.Failed, circleci.Failed) as e:
            entry['error'] = str(e)
        checks.append(entry)
    if prior_head:
        prior_verdicts(repo, pr, prior_head, checks)
    out = {'head': head, 'baseRef': now['baseRef']}
    if any(c['error'] is None for c in checks):
        out['changed'], why = changed_paths(repo, pr)
        if why:
            out['changedError'] = why
        # The paths must be this head's, like every log above.
        after = pull(repo, pr)['headRefOid']
        if after != head:
            raise Failed(f'PR #{pr} head moved to {after} while collecting')
    detail = folder / f'failures-{time.time_ns()}.json'
    detail.write_text(json.dumps({**out, 'checks': checks}, indent=1))
    return {'head': head, 'detail': str(detail), 'error': None}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['inventory', 'failures', 'remember', 'recall'])
    p.add_argument('--repo', required=True, help='OWNER/NAME of the repository that owns the PR number')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True, help='the head SHA the caller expects')
    p.add_argument('--wait-seconds', type=int, default=0)
    p.add_argument('--check', action='append', default=[], metavar='LINK')
    p.add_argument('--prior-head', help='failures: the head whose stored verdicts to show beside matching cells')
    a = p.parse_args(argv)
    if a.prior_head and (a.command != 'failures' or not SHA.fullmatch(a.prior_head) or a.prior_head == a.head):
        p.error('--prior-head is for failures: a full 40-hex SHA other than --head')
    if (a.command in ('failures', 'recall')) != bool(a.check):
        p.error('--check is for failures and recall, which need at least one')
    if not SHA.fullmatch(a.head):
        p.error('--head must be a full 40-hex SHA')
    try:
        if a.command == 'inventory':
            out = printed(inventory(a.repo, a.pr, a.head, max(0, a.wait_seconds)))
        elif a.command == 'failures':
            out = failures(a.repo, a.pr, a.head, a.check, a.prior_head)
        elif a.command == 'remember':
            out = remember(a.repo, a.pr, a.head, sys.stdin.read())
        else:
            out = recall(a.repo, a.pr, a.head, a.check)
        rc = 0
    except Failed as e:
        out, rc = {'head': a.head, 'error': str(e)}, 1
    print(json.dumps(out))
    return rc


if __name__ == '__main__':
    sys.exit(main())
