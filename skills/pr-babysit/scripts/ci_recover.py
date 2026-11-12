#!/usr/bin/env python3
"""Recover PR N's GitHub Actions run orphaned in `queued` with no job: cancel it once, then re-run it whole once.

  ci_recover.py --pr N --head SHA --wait-minutes M [--repo OWNER/NAME]

pr-babysit's CI lane runs it where the same checks stayed pending through two whole CI waits (ci-stuck, #536). Run from
the PR checkout; PR N is --repo's, else the checkout's one repository (facts.bind_repo). A run on the head is a suspected
orphan, and acted on, only when all of this reads so under its lock, the last four just before the first request: no
concurrency group can be holding it (its workflow file at SHA declares no top-level `concurrency`, or no other unfinished
run in the repository can share the group: one of the same workflow when the group names github.workflow, on the same
branch when it names github.ref, github.head_ref, github.sha or a PR number; a group naming github.run_id is its own);
the repository's newest completed run was created after it was queued (Actions runs other work); PR N is open at SHA;
the run is on the PR's head branch and repository at SHA and `queued`; its attempt has been queued at least M minutes;
and that attempt has no job. A workflow file that does not read, or no PyYAML, leaves the run alone.

Each run attempt acted on is recorded under the checkout's git common dir, in
pr-babysit/ci-recover/<owner>-<repo>-<run>-<attempt>.json, under one lock held from the reads above to the last step.
Each step, the cancel and then the re-run (`POST .../rerun`, every job: none ran, so not --failed), is written there
before its request and its answer after, and is sent at most once per run attempt by any run of this script: a step
whose answer was lost is observed, never sent again. The re-run is sent only once the run reads completed after the
cancel, waited on up to CANCEL_WAIT seconds; its new attempt is looked for up to RERUN_WAIT seconds. A later run of
this script on the same head resumes a record whose cancel completed since, sending the re-run then.

The outcome, the worst of the runs' (uncertain, failed, recovered, none in that order):
  recovered  each run acted on was cancelled and re-run, its new attempt observed
  none       no queued run on the head is a suspected orphan; each run's reason says why
  uncertain  a step's answer was lost, or what it asked for was not observed in time (a cancel that has not completed
             the run, a re-run whose attempt does not show): nothing more is sent for that attempt
  failed     GitHub refused a step (HTTP 4xx): nothing more is sent for that attempt

A recovery is no CI result: the caller waits on the new attempt like any pending check. stdout ends with one JSON line
{outcome, detail, runs, at, seal} (seal: facts.sealed), runs one record per queued or recorded run on the head
{runId, attempt, workflow, outcome, reason, evidence, newAttempt, record}: outcome `skipped` with its reason for a run
left alone, else its step's outcome; evidence what made it a suspected orphan; record the attempt record's path. Exit 2
with {"error": ...} when the arguments are wrong or a read before anything was sent fails.
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, attempt, bind_repo, git, locked, report, utc  # noqa: E402
from harvest import gh_json, pages, stamp, yaml  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import windows  # noqa: E402

CANCEL_WAIT = 45  # seconds: the whole script stays inside a relay's two-minute command
RERUN_WAIT = 30
POLL = 5
UNFINISHED = ('queued', 'in_progress', 'waiting', 'pending', 'requested')
PER_BRANCH = re.compile(r'github\.(?:ref|head_ref|sha)\b|pull_request\.number|github\.event\.number')
PER_WORKFLOW = re.compile(r'github\.workflow\b')
PER_RUN = re.compile(r'github\.run_id\b')
REFUSED = re.compile(r'\bHTTP 4\d\d\b')
ORDER = ('uncertain', 'failed', 'recovered')
# A read that fails or answers in another shape: one run's, which leaves the others' outcomes standing.
READ_FAILED = (Unusable, KeyError, TypeError, AttributeError, IndexError, ValueError)


def record_dir():
    common = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
    return common / 'pr-babysit' / 'ci-recover'


def read(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unusable(f'attempt record {path} unreadable: {e}')
    if not isinstance(record, dict) or not isinstance(record.get('steps'), dict) or not isinstance(record.get('attempt'), int):
        raise Unusable(f'attempt record {path} is not one this script wrote')
    return record


def write(path, record, new=False):
    """The record on disk before anything acts on it: a new one created exclusively, a later one replaced whole."""
    target = path if new else path.with_name(path.name + '.tmp')
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | (os.O_EXCL if new else os.O_TRUNC))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    if not new:
        windows.replace(target, path)  # Windows: retried while another process has the record open


def now():
    return datetime.now(timezone.utc)


def iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def queued_at(r):
    """When the run's current attempt was queued: a re-run's attempt starts after the run was created."""
    return max(t for t in (stamp(r.get('created_at')), stamp(r.get('run_started_at'))) if t)


def run_of(repo, run_id):
    return gh_json('api', f'repos/{repo}/actions/runs/{run_id}')


def jobs_of(repo, r):
    return gh_json('api', f"repos/{repo}/actions/runs/{r['id']}/attempts/{r['run_attempt']}/jobs?per_page=1")['total_count']


def group_of(repo, r):
    """The run's workflow-level concurrency group as written, '' when its workflow declares none; Unusable when its
    workflow file cannot be read for one."""
    if yaml is None:
        raise Unusable('PyYAML is not installed')
    meta = gh_json('api', f"repos/{repo}/contents/{r['path']}?ref={r['head_sha']}")
    try:
        doc = yaml.safe_load(base64.b64decode(meta['content']).decode())
    except (KeyError, ValueError, yaml.YAMLError) as e:
        raise Unusable(f"{r['path']} at {r['head_sha'][:9]} does not parse: {e}")
    if not isinstance(doc, dict):
        raise Unusable(f"{r['path']} at {r['head_sha'][:9]} is no workflow")
    c = doc.get('concurrency')
    if c is None:
        return ''
    return str((c.get('group') if isinstance(c, dict) else c) or '')


def holders(repo, r, group):
    """The unfinished runs that could share the run's concurrency group, read for what the group names."""
    if PER_RUN.search(group):
        return []
    others = [x for s in UNFINISHED for x in pages(f'repos/{repo}/actions/runs?status={s}', 'workflow_runs') if x.get('id') != r['id']]
    if PER_WORKFLOW.search(group):
        others = [x for x in others if x.get('workflow_id') == r.get('workflow_id')]
    if PER_BRANCH.search(group):
        others = [x for x in others if x.get('head_branch') == r.get('head_branch') and
                  (x.get('head_repository') or {}).get('full_name') == (r.get('head_repository') or {}).get('full_name')]
    return others


def suspect(repo, pr, head, wait, listed):
    """(None, the evidence, the run as read last) when the run is a suspected orphan, else (why not, [], None); the
    reads that change fastest come last, just before the caller acts."""
    try:
        group = group_of(repo, listed)
    except Unusable as e:
        return f'whether a concurrency group holds it cannot be told ({e})', [], None
    held = holders(repo, listed, group) if group else []
    if held:
        return f"its workflow's concurrency group {group!r} may be held by run {held[0]['id']} ({held[0].get('status')})", [], None
    newest = gh_json('api', f'repos/{repo}/actions/runs?status=completed&per_page=1')['workflow_runs']
    if not newest or stamp(newest[0]['created_at']) <= queued_at(listed):
        return 'no run in the repository created since it was queued has completed', [], None
    view = gh_json('pr', 'view', str(pr), '-R', repo, '--json', 'state,headRefOid,headRefName,headRepository')
    if view['state'] != 'OPEN' or view['headRefOid'] != head:
        return f"PR {pr} is {view['state']} at {view['headRefOid'][:9]}, not open at {head[:9]}", [], None
    r = run_of(repo, listed['id'])
    if r.get('head_sha') != head or r.get('head_branch') != view['headRefName'] or \
            (r.get('head_repository') or {}).get('full_name') != view['headRepository']['nameWithOwner']:
        return "it is not on the PR's head branch and repository at the head", [], None
    if r['status'] != 'queued' or r['run_attempt'] != listed['run_attempt']:
        return f"it is now {r['status']}, attempt {r['run_attempt']}", [], None
    waited = now() - queued_at(r)
    if waited < timedelta(minutes=wait):
        return f'its attempt has been queued {int(waited.total_seconds() // 60)} minutes, under the {wait}-minute CI wait', [], None
    jobs = jobs_of(repo, r)
    if jobs:
        return f"its attempt {r['run_attempt']} has {jobs} job(s)", [], None
    return None, [f"queued since {iso(queued_at(r))} ({int(waited.total_seconds() // 60)} minutes), attempt {r['run_attempt']} with no job",
                  f"run {newest[0]['id']} created {newest[0]['created_at']} has completed since",
                  f'concurrency group {group!r}, no unfinished run can share it' if group else 'its workflow declares no concurrency group'], r


def answer(code, err):
    """What a POST's answer proves: sent, refused (GitHub answered 4xx: nothing done) or unknown (anything else)."""
    if code == 0:
        return 'sent'
    return f"{'refused' if REFUSED.search(err) else 'unknown'}: {err.strip()[:300] or f'exit {code}'}"


def observe(repo, run_id, done, seconds):
    """The run once done(run) holds, polled every POLL seconds for up to seconds; None when it never did."""
    deadline = time.monotonic() + seconds
    while True:
        r = run_of(repo, run_id)
        if done(r):
            return r
        if time.monotonic() + POLL > deadline:
            return None
        time.sleep(POLL)


def step(repo, path, record, name, endpoint):
    """Send one step once: written before its POST, its answer after; an earlier answer when it was sent already."""
    if name not in record['steps']:
        record['steps'][name] = {'at': utc(time.time())}
        write(path, record)
        code, _, err = attempt('gh', 'api', '-X', 'POST', f"repos/{repo}/actions/runs/{record['runId']}/{endpoint}")
        record['steps'][name]['answer'] = answer(code, err)
        write(path, record)
    return record['steps'][name].get('answer') or 'unknown: the record holds no answer (a run cut short after sending)'


def advance(repo, path, record):
    """(outcome, reason): the record's next step taken, or what its last one shows."""
    run_id, n = record['runId'], record['attempt']
    sent = step(repo, path, record, 'cancel', 'cancel')
    if sent.startswith('refused'):
        return 'failed', f'GitHub refused the cancel ({sent}); nothing more is sent for attempt {n}'
    r = observe(repo, run_id, lambda r: r['status'] == 'completed' or r['run_attempt'] != n, CANCEL_WAIT)
    if r is None:
        return 'uncertain', (f"the cancel ({sent}) has not completed the run within {CANCEL_WAIT} seconds; it is not sent again, and a "
                             'later run of this script re-runs the run once it reads completed')
    if r['run_attempt'] == n or 'rerun' in record['steps']:
        sent = step(repo, path, record, 'rerun', 'rerun')
        if sent.startswith('refused'):
            return 'failed', f'the run was cancelled, and GitHub refused its re-run ({sent}); nothing more is sent for attempt {n}'
        r = observe(repo, run_id, lambda r: r['run_attempt'] > n, RERUN_WAIT)
        if r is None:
            return 'uncertain', f'the run was cancelled and its re-run ({sent}) shows no new attempt within {RERUN_WAIT} seconds; it is not sent again'
    record['newAttempt'] = r['run_attempt']
    write(path, record)
    if 'rerun' not in record['steps']:
        return 'recovered', f"cancelled, and attempt {r['run_attempt']} ({r['status']}) was started by someone else: no re-run was sent"
    return 'recovered', f"cancelled and re-run: attempt {r['run_attempt']} is {r['status']}"


def one(repo, pr, head, wait, listed):
    """One run's record: left alone, or its attempt's steps taken under its lock."""
    path = record_dir() / f"{repo.replace('/', '-')}-{listed['id']}-{listed['run_attempt']}.json"
    shown = {'runId': listed['id'], 'attempt': listed['run_attempt'], 'workflow': listed.get('name') or listed.get('path'),
             'evidence': [], 'newAttempt': None, 'record': None}
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        record = read(path)
        if record is None:
            if listed.get('status') != 'queued':
                return None
            try:
                why, evidence, r = suspect(repo, pr, head, wait, listed)
            except READ_FAILED as e:
                why = f'a read failed ({e!r}), so nothing was sent'
            if why:
                return {**shown, 'outcome': 'skipped', 'reason': why}
            record = {'repo': repo, 'pr': pr, 'head': head, 'runId': r['id'], 'attempt': r['run_attempt'], 'evidence': evidence, 'steps': {}}
            write(path, record, new=True)
        try:
            outcome, why = advance(repo, path, record)
        except READ_FAILED as e:
            outcome, why = 'uncertain', f'a read after the record was written failed ({e!r}); nothing is sent again for this attempt'
    return {**shown, 'outcome': outcome, 'reason': why, 'evidence': record['evidence'], 'newAttempt': record.get('newAttempt'),
            'record': str(path)}


def recover(repo, pr, head, wait):
    view = gh_json('pr', 'view', str(pr), '-R', repo, '--json', 'state,headRefOid')
    if view['state'] != 'OPEN' or view['headRefOid'] != head:
        return {'outcome': 'none', 'detail': f"PR {pr} is {view['state']} at {view['headRefOid'][:9]}, not open at {head[:9]}", 'runs': []}
    listed = [r for r in pages(f'repos/{repo}/actions/runs?head_sha={head}', 'workflow_runs') if r.get('head_sha') == head]
    runs = [x for x in (one(repo, pr, head, wait, r) for r in listed) if x]
    outcome = next((o for o in ORDER if any(x['outcome'] == o for x in runs)), 'none')
    detail = '; '.join(f"run {x['runId']} attempt {x['attempt']}: {x['outcome']}, {x['reason']}" for x in runs) or 'no queued run on the head'
    return {'outcome': outcome, 'detail': detail, 'runs': runs}


def collect(argv):
    p = Parser(prog='ci_recover.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--wait-minutes', type=int, required=True)
    p.add_argument('--repo')
    a = p.parse_args(argv)
    if not FULL_SHA.match(a.head):
        raise Unusable(f'not a full SHA: {a.head!r}')
    if a.wait_minutes < 1:
        raise Unusable(f'--wait-minutes must be at least 1: {a.wait_minutes}')
    repo = bind_repo(a.repo)
    try:
        out = recover(repo, a.pr, a.head, a.wait_minutes)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise Unusable(f'gh answered in a shape ci_recover.py does not read ({e!r})')
    return {**out, 'at': iso(now())}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
