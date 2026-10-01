#!/usr/bin/env python3
"""Collect a PR's CI state for a watcher, so no model spends turns waiting or listing.

  collect.py inventory --repo OWNER/NAME --pr N --head SHA [--wait-seconds S]
  collect.py failures --repo OWNER/NAME --pr N --head SHA [--check LINK...] [--gate LINK...]
  collect.py remember --repo OWNER/NAME --pr N --head SHA < VERDICTS.json
  collect.py recall --repo OWNER/NAME --pr N --head SHA --check LINK... [--offset N]

inventory: waits up to S seconds (default 0) while any check is pending, then
prints one JSON object {head, status, pending, checks}, plus `error` when set
(a relaying agent can drop a trailing null, so no line carries a null error); every
line but an error one also carries `seal`, pr-babysit's facts.py seal. `status` is
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
one file per call, and prints {head, detail, gates, bases, error}, `bases` each
--check's `base` as "runId:jobId:conclusion" (null without one); the bases
command prints {head, bases, error}, the same tokens, reading no log. Each
check's entry holds its saved `log` and the diagnostic lines of every step that reported an error.
The log is the job's whole log without ANSI codes, NULs and timestamps for an
Actions job, but only tails for the others: the last 150 lines of each failed
CircleCI step and Read the Docs' notes with 40-line tails of failed commands.
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
so two different errors can share one: same-failure and prior are leads. prior
lists the verdicts stored for the same workflow, check, cell and signature on the
newest other head of the PR that has one, [{head, verdict, firstError}], more than
one when ambiguous, and is absent when none match; the detail file's `priorErrors`
names each other head whose store could not be read and was left out. A cell row is left out of
`diagnostics` and `shared`, on both sides; `firstError` still reads it. A failed
step that ran hil_test.py but printed no parsed row gets `cellsError` instead.
When any check was read, the detail file lists the PR's `changed`
paths (null with `changedError` when they could not be read or may be
incomplete), and the PR head is read again after them. A --check that is no longer a failing check of the head is a stale
snapshot: exit 1, before anything is read. Read the Docs needs RTD_TOKEN (see rtd.py).

A --gate (a red SonarCloud gate check; failures needs a --check or a --gate,
no link twice) is stale the same way, stays out of the detail file and is
printed in `gates` (always present), one {link, failures} each: its
sonarcloud.io link naming the project and the PR is read for the quality
gate's status (SONAR_TOKEN when set, sent to sonarcloud.io only), and
`failures` is [{firstError, signature, complete}], one complete entry per
failing condition of an ERROR gate that names its metric, value, comparator
and threshold, or else one incomplete entry, its firstError saying why: the
gate lists none, reads passing now, or could not be read. The PR head is read
again after the gates too.

remember: stores a judge's verdicts for the head beside its evidence, so a
later launch recalls them instead of carrying them in its state. It reads a
JSON list on stdin of {link, bucket, failures}, each replacing any stored entry
for the same link, or {link, bucket, patch}, whose failures each replace the one
failure of the stored entry, same bucket, with the same workflow, job, cell and
signature. It stores all of them or, on any mismatch, none, and prints {head, error}. recall prints {head, verdicts, left, error}:
the stored entries for the --check links it has, unchanged and in the order
asked, while the printed line stays within RECALL_BYTES, the most a relaying
agent is trusted to copy whole. `left` lists the links held back for room, each
{link, starts}: where each page of its entry's failures starts, one page when the
entry fits a line alone. A lone --check with --offset at one of those starts prints
that page as its one verdict, the entry with only that page's failures, and the
pages in order join into the entry. A link it has none for, or whose entry has a
failure or a list of starts too large for a line, is left out of both. The caller checks what comes
back against its own digests.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
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
RECALL_BYTES = 8192  # unmeasured: a relay copied 3.5 KB whole and failed at 28 KB and 51 KB (agentrc#9, #23, #20)
SHA = re.compile(r'[0-9a-f]{40}')
HIL_ROW = re.compile(r'^(?:\d+\.\d{3} )?(\S+)\s+(\S+)\s+\.\.\.\s+(\S.*)$')  # HIL_PROFILE=1 prefixes epoch seconds
HIL_FAILED = ('Failed:', 'Flash Failed:')
LINE = 500
SONARCLOUD = 'https://sonarcloud.io'
COMPARATOR = {'GT': '>', 'LT': '<'}
CONDITION = ('metricKey', 'actualValue', 'comparator', 'errorThreshold')


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


def fnv1a(text):
    """The workflow's fnv1a: 32-bit FNV-1a over code points, 8 hex digits."""
    h = 0x811c9dc5
    for ch in text:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return f'{h:08x}'


def sealed(facts):
    """facts plus the seal pr-babysit checks a relayed copy against: fnv1a over the
    canonical JSON with null members left out, so a copy that drops one still matches.
    A copy of facts.py's, so ci-rerun stays usable without pr-babysit; test_ci_collect holds them equal."""
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


def printed(inv):
    """What the caller reads: the failing checks by name, the pending ones by count."""
    return {'head': inv['head'], 'status': inv['status'], 'pending': inv['counts'].get('pending', 0),
            'checks': [c for c in inv['checks'] if c['bucket'] in ('fail', 'cancel')]}


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
    if not (isinstance(entries, list) and all(isinstance(e, dict) and sorted(e) in (['bucket', 'failures', 'link'], ['bucket', 'link', 'patch']) and
                                              isinstance(e['link'], str) and isinstance(e['bucket'], str) and
                                              isinstance(fs := e.get('failures', e.get('patch')), list) and
                                              all(isinstance(f, dict) for f in fs) for e in entries)):
        raise Failed('verdicts on stdin must be a list of {link, bucket, failures} or {link, bucket, patch}')
    folder = evidence_dir(repo, pr, head)
    stored = stored_verdicts(folder)
    for e in entries:
        stored[e['link']] = patched(stored.get(e['link']), e) if 'patch' in e else e
    tmp = folder / f'verdicts.json.{time.time_ns()}'
    tmp.write_text(json.dumps(stored, ensure_ascii=False))
    tmp.replace(folder / 'verdicts.json')
    return {'head': head}


def failure_key(f):
    return (f.get('workflow'), f.get('job'), f.get('cell'), f.get('signature'))


def patched(entry, patch):
    """The stored entry with each patch failure in place of the one stored failure of its key."""
    if not entry or entry['bucket'] != patch['bucket']:
        raise Failed(f'patch for {patch["link"]}: no stored entry with bucket {patch["bucket"]}')
    keys = [failure_key(f) for f in entry['failures']]
    new = {failure_key(f): f for f in patch['patch']}
    missing = [k for k in new if keys.count(k) != 1]
    if missing or len(new) != len(patch['patch']):
        raise Failed(f'patch for {patch["link"]}: its failures must each match one stored failure, once: {missing}')
    return {**entry, 'failures': [new.get(k, f) for k, f in zip(keys, entry['failures'])]}


def line(head, verdicts=(), left=()):
    return {'head': head, 'verdicts': list(verdicts), 'left': list(left)}


def fits_line(out):
    return len(json.dumps(sealed(out))) + 1 <= RECALL_BYTES


def page(entry, start, end):
    return {**entry, 'failures': entry['failures'][start:end]}


def page_starts(head, entry):
    """Where each page of the entry's failures starts, each page as long as its line allows,
    so [0] for an entry that fits a line whole; None when one failure fits no line, or the starts."""
    starts, start, n = [], 0, len(entry['failures'])
    while True:
        end = min(start + 1, n)
        if not fits_line(line(head, [page(entry, start, end)])):
            return None
        while end < n and fits_line(line(head, [page(entry, start, end + 1)])):
            end += 1
        starts.append(start)
        if end == n:
            return starts if fits_line(line(head, left=[{'link': entry['link'], 'starts': starts}])) else None
        start = end


def recall(repo, pr, head, links, offset=None):
    stored = stored_verdicts(evidence_dir(repo, pr, head))
    known = [link for link in links if link in stored]
    starts = {link: page_starts(head, stored[link]) for link in known}
    if offset is not None:
        pages = starts.get(links[0]) or []
        if offset not in pages:
            raise Failed(f'--offset {offset} starts no page of a stored verdict for {links[0]}: its pages start at {pages}')
        return line(head, [page(stored[links[0]], offset, next((s for s in pages if s > offset), None))])

    def fits(verdicts, left):
        return fits_line(line(head, verdicts, left))

    def held(links):
        return [{'link': link, 'starts': starts[link]} for link in links if starts[link]]

    # Room is kept for every link still to come, so the line holds whatever is held back;
    # a link held back comes back in the pages its starts name, each from a recall of its own.
    verdicts, left = [], []
    for i, link in enumerate(known):
        rest = held(known[i + 1:])
        if fits([*verdicts, stored[link]], left + rest):
            verdicts.append(stored[link])
        elif starts[link] and fits(verdicts, [*left, *held([link]), *rest]):
            left += held([link])
    return line(head, verdicts, left)


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


def gate_conditions(link, pr):
    """The PR's quality gate on SonarCloud: its status and a line for each failing condition."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    if not link.startswith(f'{SONARCLOUD}/') or len(q.get('id', [])) != 1 or q.get('pullRequest') != [str(pr)]:
        raise Failed(f'not a SonarCloud link naming one project and PR #{pr}: {link}')
    project = q['id'][0]
    query = urllib.parse.urlencode({'projectKey': project, 'pullRequest': pr})
    req = urllib.request.Request(f'{SONARCLOUD}/api/qualitygates/project_status?{query}')
    token = os.environ.get('SONAR_TOKEN', '').strip()
    if token:
        req.add_header('Authorization', 'Basic ' + base64.b64encode(f'{token}:'.encode()).decode())
    try:
        with rtd.OPENER.open(req, timeout=30) as r:
            gate = json.loads(r.read().decode())['projectStatus']
    except urllib.error.HTTPError as e:
        raise Failed(f'SonarCloud quality gate of {project} PR #{pr}: HTTP {e.code} {e.msg}: {e.read().decode(errors="replace")[:200]}')
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise Failed(f'SonarCloud quality gate of {project} PR #{pr}: {e}')
    conditions = gate.get('conditions') or [] if isinstance(gate, dict) else None
    if not isinstance(conditions, list) or not all(isinstance(c, dict) for c in conditions):
        raise Failed(f'SonarCloud quality gate of {project} PR #{pr}: unexpected answer {json.dumps(gate)[:200]}')
    status = gate.get('status')
    failing = [c for c in conditions if c.get('status') == 'ERROR']
    if status != 'ERROR' and failing:
        raise Failed(f'SonarCloud quality gate of {project} PR #{pr} is {status} with failing conditions listed')
    if any(c.get(k) in (None, '') for c in failing for k in CONDITION):
        raise Failed(f'SonarCloud quality gate of {project} PR #{pr} lists a failing condition without its {", ".join(CONDITION)}')
    return status, [
        f'condition failed: {c["metricKey"]} {c["actualValue"]} {COMPARATOR.get(c["comparator"], c["comparator"])} {c["errorThreshold"]}'
        for c in failing]


def gate(link, pr):
    """A red gate check's failures for the caller's records: one per failing condition, complete,
    or one incomplete when the gate lists none or could not be read."""
    try:
        status, lines = gate_conditions(link, pr)
    except Failed as e:
        lines, first = [], f'SonarCloud gate not read: {e}'
    else:  # a gate read passing now had its check report an earlier analysis
        first = ('quality gate ERROR with no failing condition listed' if status == 'ERROR' else
                 f'quality gate {status} now, though its check failed: SonarCloud may have analysed again since')
    failures = [{'firstError': line, 'signature': signature(line), 'complete': True} for line in lines]
    return {'link': link, 'failures': failures or [{'firstError': first, 'signature': signature(first), 'complete': False}]}


def circle(repo, number, folder):
    lines = []
    for name, tail in circleci.failed_steps(repo, number, 150):
        lines += [f'== {name}', *clean(tail)]
    entry = evidence(lines, None)
    return {'name': f'job {number}', 'log': save(folder, f'circleci-{number}.log', lines), 'base': None, **entry}


def prior_verdicts(repo, pr, head, entries):
    """Each cell's verdicts from the newest other head of the PR that stored one for its workflow,
    check, cell and signature: a head judged only in part leaves the rest to an older one. Two
    workflows may name a job alike, so a verdict with no workflow name matches nothing. Returns the
    other heads whose store could not be read, which are left out."""
    if not any(entry.get('cells') for entry in entries):
        return []
    index, unread, stores = {}, [], []
    for folder in evidence_dir(repo, pr, head).parent.iterdir():
        try:
            stores.append(((folder / 'verdicts.json').stat().st_mtime, folder))
        except FileNotFoundError:
            pass
        except OSError as e:
            unread.append(f'{folder.name}: {e}')
    for _, folder in sorted(stores, reverse=True):
        if folder.name == head:
            continue
        found = {}
        try:
            for stored in stored_verdicts(folder).values():
                for f in stored.get('failures') or []:
                    if f.get('workflow'):
                        found.setdefault((f['workflow'], f.get('check'), f.get('cell'), f.get('signature')), []).append(
                            {'head': folder.name, 'verdict': f.get('verdict'), 'firstError': f.get('firstError')})
        except (Failed, OSError, AttributeError, TypeError) as e:
            unread.append(f'{folder.name}: {e}')
            continue
        index = {**found, **index}
    for entry in entries:
        for c in entry.get('cells') or []:
            prior = index.get((entry.get('workflow'), entry['name'], c['cell'], c['signature']))
            if prior:
                c['prior'] = prior
    return unread


def changed_paths(repo, pr):
    """(the PR's paths, None) or (None, why). GitHub lists at most 3000 files."""
    try:
        names = [f['filename'] for page in gh('api', '--paginate', '--slurp', f'repos/{repo}/pulls/{pr}/files?per_page=100') for f in page]
    except (Failed, KeyError, TypeError) as e:
        return None, f'the PR files could not be read: {e}'
    if len(names) >= 3000:
        return None, f'GitHub listed {len(names)} files, its cap: the list may be incomplete'
    return names, None


def failures(repo, pr, head, links, gates=()):
    now = inventory(repo, pr, head, 0)
    failing = {c['link']: c for c in now['checks'] if c['bucket'] in ('fail', 'cancel')}
    stale = [link for link in (*links, *gates) if link not in failing]
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
    unread = prior_verdicts(repo, pr, head, checks)
    read = [gate(link, pr) for link in gates]
    out = {'head': head, 'baseRef': now['baseRef'], **({'priorErrors': unread} if unread else {})}
    if any(c['error'] is None for c in checks):
        out['changed'], why = changed_paths(repo, pr)
        if why:
            out['changedError'] = why
    # The paths and gates must be this head's, like every log above.
    if read or 'changed' in out:
        after = pull(repo, pr)['headRefOid']
        if after != head:
            raise Failed(f'PR #{pr} head moved to {after} while collecting')
    detail = folder / f'failures-{time.time_ns()}.json'
    detail.write_text(json.dumps({**out, 'checks': checks}, indent=1))
    return {'head': head, 'detail': str(detail), 'gates': read,
            'bases': [{'link': c['link'], 'base': base_token(c.get('base'))} for c in checks]}


def base_token(base):
    return f"{base['runId']}:{base['jobId']}:{base['conclusion']}" if base else None


def bases(repo, pr, head, links):
    """Each --check's base job token as `failures` prints it, without reading a log."""
    now = inventory(repo, pr, head, 0)
    failing = {c['link']: c for c in now['checks'] if c['bucket'] in ('fail', 'cancel')}
    stale = [link for link in links if link not in failing]
    if stale:
        raise Failed('stale snapshot: no longer failing checks of the head: ' + ', '.join(stale))
    cache, out = {}, []
    for link in links:
        kind, _, ident = (failing[link]['attempt'] or 'other:').partition(':')
        base = None
        if kind == 'actions':
            record = gh('api', f'repos/{repo}/actions/jobs/{ident}')
            if record.get('head_sha') != head:
                raise Failed(f'job {ident} ran on {record.get("head_sha")}, not the head {head}')
            base = base_run(repo, now['baseRef'], record.get('workflow_name', ''), record['name'], cache)
        out.append({'link': link, 'base': base_token(base)})
    after = pull(repo, pr)['headRefOid']
    if after != head:
        raise Failed(f'PR #{pr} head moved to {after} while collecting')
    return {'head': head, 'bases': out}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['inventory', 'failures', 'bases', 'remember', 'recall'])
    p.add_argument('--repo', required=True, help='OWNER/NAME of the repository that owns the PR number')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True, help='the head SHA the caller expects')
    p.add_argument('--wait-seconds', type=int, default=0)
    p.add_argument('--check', action='append', default=[], metavar='LINK')
    p.add_argument('--gate', action='append', default=[], metavar='LINK', help='failures: a red SonarCloud gate check, read for the caller')
    p.add_argument('--offset', type=int, help='recall: where the page to print starts, of the lone --check')
    a = p.parse_args(argv)
    if a.offset is not None and (a.command != 'recall' or len(a.check) != 1):
        p.error('--offset is for recall with exactly one --check')
    if a.gate and a.command != 'failures':
        p.error('--gate is for failures')
    if (a.command in ('failures', 'bases', 'recall')) != bool(a.check or a.gate):
        p.error('--check is for failures, bases and recall, which need at least one (failures: a --check or a --gate)')
    if len({*a.check, *a.gate}) != len(a.check) + len(a.gate):
        p.error('a link is given more than once')
    if not SHA.fullmatch(a.head):
        p.error('--head must be a full 40-hex SHA')
    try:
        if a.command == 'inventory':
            out = printed(inventory(a.repo, a.pr, a.head, max(0, a.wait_seconds)))
        elif a.command == 'failures':
            out = failures(a.repo, a.pr, a.head, a.check, a.gate)
        elif a.command == 'bases':
            out = bases(a.repo, a.pr, a.head, a.check)
        elif a.command == 'remember':
            out = remember(a.repo, a.pr, a.head, sys.stdin.read())
        else:
            out = recall(a.repo, a.pr, a.head, a.check, a.offset)
        out, rc = sealed(out), 0
    except Failed as e:
        out, rc = {'head': a.head, 'error': str(e)}, 1
    print(json.dumps(out))
    return rc


if __name__ == '__main__':
    sys.exit(main())
