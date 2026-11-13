#!/usr/bin/env python3
"""Recover PR N's GitHub Actions run orphaned in `queued` with no job: cancel it once, then re-run it whole once.

  ci_recover.py --pr N --head SHA --wait-minutes M [--repo OWNER/NAME]

pr-babysit's CI lane runs it where the same checks stayed pending through two whole CI waits (ci-stuck, #536). Run from
the PR checkout; PR N is --repo's, else the checkout's one repository (facts.bind_repo). Its relay runs it with the
600000 ms Bash timeout; the whole script keeps within BUDGET seconds of that: no read, lock wait or request starts past
it, each gh or git command is cut off at it (facts.DEADLINE: a read then fails, a request's answer is unknown), and a
run on the head it does not reach is skipped, not reached. A run on the head is a suspected orphan, and acted on, only
when all of this reads so under its lock, the cheapest first, and the PR, the run, its group's holders and its jobs read
afresh just before the cancel: no concurrency group can be holding it (its workflow file at SHA declares no top-level
`concurrency`, its group is exactly `${{ github.run_id }}`, or no other unfinished run is in the repository at all, read
a status at a time on a page of two); the repository's newest completed run was created after it was queued (Actions
runs other work); PR N is open at SHA; the run is on the PR's head branch and repository at SHA and `queued`; its
attempt has been queued at least M minutes; and that attempt has no job. A workflow file that does not read, or no
PyYAML, leaves the run alone.

Any other group can be shared by any other unfinished run: groups compare case-insensitively, concatenated values can
collide, and another workflow's group can be any expression of its own, none of which this script reads.

Each run attempt acted on is recorded under the checkout's git common dir, in
pr-babysit/ci-recover/<owner>-<repo>-<run>-<attempt>.json, under one lock held from the reads above to the last step.
Each step, the cancel and then the re-run (`POST .../rerun`, every job: none ran, so not --failed), is written there
before its request and its answer after, and is sent at most once per run attempt by any run of this script: a step
whose answer was lost is observed, never sent again. The re-run is sent only once the run reads completed after the
cancel, waited on up to CANCEL_WAIT seconds, and only when its attempt concluded `cancelled` and PR N is still open at
SHA with the run on its head; its new attempt is looked for up to RERUN_WAIT seconds. A record whose cancel was not
written yet is checked as a new one; a later run of this script on the same head resumes a record whose cancel
completed since, sending the re-run then. Each write is durable (facts.write_record) before its request goes.

The outcome, the worst of the runs' (uncertain, failed, withheld, completed, recovered, none in that order):
  recovered  each run acted on was cancelled and re-run, its new attempt observed
  completed  a run's attempt completed with a conclusion other than cancelled after the cancel: it is not re-run
  withheld   a run was cancelled, and its re-run is not sent: PR N is no longer open at SHA, or the run not on its head
  none       no queued run on the head is a suspected orphan; each run's reason says why
  uncertain  a step's answer was lost, or what it asked for was not observed in time (a cancel that has not completed
             the run, a re-run whose attempt does not show), or the budget ran out first, a record's lock held past it
             included: nothing more is sent for that attempt by this run
  failed     GitHub refused a step (HTTP 4xx): nothing more is sent for that attempt

A recovery is no CI result: the caller waits on the new attempt like any pending check. stdout ends with one JSON line
{outcome, detail, runs, at, seal} (seal: facts.sealed), runs one record per queued or recorded run on the head
{runId, attempt, workflow, outcome, reason, evidence, newAttempt, record}: outcome `skipped` with its reason for a run
left alone, else its step's outcome; evidence what made it a suspected orphan; record the attempt record's path. Exit 2
with {"error": ...} when the arguments are wrong or a read before anything was sent fails.
"""

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from coderabbit_trigger import pull_at  # noqa: E402
import facts  # noqa: E402
from facts import FULL_SHA, Busy, OutOfTime, Parser, Unusable, attempt, bind_repo, locked, read_record, record_dir, report, utc, write_record  # noqa: E402
from harvest import gh_json, pages, stamp, workflow_doc, yaml  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
from check_runs import RUN_PENDING  # noqa: E402

BUDGET = 420  # seconds of the relay's 600 s Bash timeout the script may spend; the rest covers its start and its line
CANCEL_WAIT = 45
RERUN_WAIT = 30
POLL = 5
PER_RUN = re.compile(r'\$\{\{\s*github\.run_id\s*\}\}')  # as the whole group: no other run can share it
REFUSED = re.compile(r'\bHTTP 4\d\d\b')
ORDER = ('uncertain', 'failed', 'withheld', 'completed', 'recovered')
# A read that fails or answers in another shape: one run's, which leaves the others' outcomes standing.
READ_FAILED = (Unusable, KeyError, TypeError, AttributeError, IndexError, ValueError)


END = None  # time.monotonic() when BUDGET runs out, set by collect


def left():
    """Seconds of BUDGET left."""
    return END - time.monotonic()


def due():
    """Before each read: OutOfTime once BUDGET is spent, so no more work starts."""
    if left() <= 0:
        raise OutOfTime('the time budget ran out')


def read(path):
    record = read_record(path)
    if record is None:
        return None
    if not isinstance(record, dict) or not isinstance(record.get('steps'), dict) or not isinstance(record.get('attempt'), int):
        raise Unusable(f'attempt record {path} is not one this script wrote')
    return record


def now():
    return datetime.now(timezone.utc)


def iso(t):
    return utc(t.timestamp())


def queued_at(r):
    """When the run's current attempt was queued: a re-run's attempt starts after the run was created."""
    return max(t for t in (stamp(r.get('created_at')), stamp(r.get('run_started_at'))) if t)


def run_of(repo, run_id):
    due()
    return gh_json('api', f'repos/{repo}/actions/runs/{run_id}')


def jobs_of(repo, r):
    due()
    return gh_json('api', f"repos/{repo}/actions/runs/{r['id']}/attempts/{r['run_attempt']}/jobs?per_page=1")['total_count']


def group_of(repo, r):
    """The run's workflow-level concurrency group as written, '' when its workflow declares none; Unusable when its
    workflow file cannot be read for one."""
    if yaml is None:
        raise Unusable('PyYAML is not installed')
    due()
    doc = workflow_doc(repo, r['path'], r['head_sha'])
    if doc is None:
        raise Unusable(f"{r['path']} at {r['head_sha'][:9]} does not parse into a workflow")
    c = doc.get('concurrency')
    if c is None:
        return ''
    return str((c.get('group') if isinstance(c, dict) else c) or '')


def holder(repo, r, group):
    """Why another unfinished run could share the run's concurrency group, None when none can: never for its own run
    id, else when any other run in the repository is unfinished, read a status at a time on a page of two."""
    if PER_RUN.fullmatch(group):
        return None
    for status in RUN_PENDING:
        due()
        page = gh_json('api', f'repos/{repo}/actions/runs?status={status}&per_page=2')
        others = [x for x in page['workflow_runs'] if x.get('id') != r['id']]
        if others or page['total_count'] > len(page['workflow_runs']):
            return (f"its workflow's concurrency group {group!r} may be held by another unfinished run"
                    + (f" (run {others[0]['id']}, {status})" if others else f' ({status})'))
    return None


def on_head(repo, pr, head, run_id):
    """(None, the run as read) when PR N is open at head and the run is on its head branch and repository at head, else
    (why not, None)."""
    due()
    pull, why = pull_at(pr, head)
    if why:
        return why, None
    r = run_of(repo, run_id)
    if r.get('head_sha') != head or r.get('head_branch') != pull['head']['ref'] or \
            (r.get('head_repository') or {}).get('full_name') != (pull['head']['repo'] or {}).get('full_name'):
        return "it is not on the PR's head branch and repository at the head", None
    return None, r


def suspect(repo, pr, head, wait, listed):
    """(None, the evidence, the run as read last) when the run is a suspected orphan, else (why not, [], None); the
    reads that cost least come first, and those that change fastest last, freshly just before the caller acts."""
    waited = now() - queued_at(listed)
    if waited < timedelta(minutes=wait):
        return f'its attempt has been queued {int(waited.total_seconds() // 60)} minutes, under the {wait}-minute CI wait', [], None
    jobs = jobs_of(repo, listed)
    if jobs:
        return f"its attempt {listed['run_attempt']} has {jobs} job(s)", [], None
    try:
        group = group_of(repo, listed)
    except Unusable as e:
        return f'whether a concurrency group holds it cannot be told ({e})', [], None
    due()
    newest = gh_json('api', f'repos/{repo}/actions/runs?status=completed&per_page=1')['workflow_runs']
    if not newest or stamp(newest[0]['created_at']) <= queued_at(listed):
        return 'no run in the repository created since it was queued has completed', [], None
    why, r = on_head(repo, pr, head, listed['id'])
    if why:
        return why, [], None
    if r['status'] != 'queued' or r['run_attempt'] != listed['run_attempt']:
        return f"it is now {r['status']}, attempt {r['run_attempt']}", [], None
    waited = now() - queued_at(r)
    if waited < timedelta(minutes=wait):
        return f'its attempt has been queued {int(waited.total_seconds() // 60)} minutes, under the {wait}-minute CI wait', [], None
    held = holder(repo, r, group) if group else None
    if held:
        return held, [], None
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
    deadline = time.monotonic() + min(seconds, max(left(), 0))
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
        due()
        record['steps'][name] = {'at': utc(time.time())}
        write_record(path, record)
        code, _, err = attempt('gh', 'api', '-X', 'POST', f"repos/{repo}/actions/runs/{record['runId']}/{endpoint}")
        record['steps'][name]['answer'] = answer(code, err)
        write_record(path, record)
    return record['steps'][name].get('answer') or 'unknown: the record holds no answer (a run cut short after sending)'


def advance(repo, path, record):
    """(outcome, reason): the record's next step taken, or what its last one shows."""
    run_id, n = record['runId'], record['attempt']
    if 'cancel' not in record['steps'] and left() <= 0:
        return 'skipped', 'the time budget ran out before the cancel: nothing was sent'
    sent = step(repo, path, record, 'cancel', 'cancel')
    if sent.startswith('refused'):
        return 'failed', f'GitHub refused the cancel ({sent}); nothing more is sent for attempt {n}'
    r = observe(repo, run_id, lambda r: r['status'] == 'completed' or r['run_attempt'] != n, CANCEL_WAIT)
    if r is None:
        return 'uncertain', (f"the cancel ({sent}) has not completed the run within {CANCEL_WAIT} seconds; it is not sent again, and a "
                             'later run of this script re-runs the run once it reads cancelled')
    if r['run_attempt'] == n and 'rerun' not in record['steps']:
        if r.get('conclusion') != 'cancelled':
            return 'completed', (f"attempt {n} completed {r.get('conclusion')} after the cancel ({sent}), not cancelled: it is not "
                                 're-run')
        why, r = on_head(repo, record['pr'], record['head'], run_id)
        if why:
            return 'withheld', f'the run was cancelled, and its re-run is not sent: {why}'
        if left() <= 0:
            return 'uncertain', 'the run was cancelled, and the time budget ran out before its re-run: a later run of this script sends it'
    if r['run_attempt'] == n or 'rerun' in record['steps']:
        sent = step(repo, path, record, 'rerun', 'rerun')
        if sent.startswith('refused'):
            return 'failed', f'the run was cancelled, and GitHub refused its re-run ({sent}); nothing more is sent for attempt {n}'
        r = observe(repo, run_id, lambda r: r['run_attempt'] > n, RERUN_WAIT)
        if r is None:
            return 'uncertain', f'the run was cancelled and its re-run ({sent}) shows no new attempt within {RERUN_WAIT} seconds; it is not sent again'
    record['newAttempt'] = r['run_attempt']
    write_record(path, record)
    if 'rerun' not in record['steps']:
        return 'recovered', f"cancelled, and attempt {r['run_attempt']} ({r['status']}) was started by someone else: no re-run was sent"
    return 'recovered', f"cancelled and re-run: attempt {r['run_attempt']} is {r['status']}"


def one(repo, pr, head, wait, listed):
    """One run's record: left alone, or its attempt's steps taken under its lock."""
    path = record_dir('ci-recover') / f"{repo.replace('/', '-')}-{listed['id']}-{listed['run_attempt']}.json"
    shown = {'runId': listed['id'], 'attempt': listed['run_attempt'], 'workflow': listed.get('name') or listed.get('path'),
             'evidence': [], 'newAttempt': None, 'record': None}
    if listed.get('status') != 'queued' and not path.exists():
        return None
    try:
        with locked(path, timeout=max(left(), 0)):
            return under_lock(repo, pr, head, wait, listed, path, shown)
    except Busy as e:
        return {**shown, 'outcome': 'uncertain', 'reason': f'{e}, past the time budget: nothing was sent by this run', 'record': str(path)}


def under_lock(repo, pr, head, wait, listed, path, shown):
    """one()'s work under the record's lock."""
    record = read(path)
    if record is None and listed.get('status') != 'queued':
        return None
    if record is None or 'cancel' not in record['steps']:  # a record cut short before its cancel proves nothing
        try:
            why, evidence, r = suspect(repo, pr, head, wait, listed)
        except OutOfTime as e:
            why = f'{e} before it was read through: nothing was sent'
        except READ_FAILED as e:
            why = f'a read failed ({e!r}), so nothing was sent'
        if why:
            return {**shown, 'outcome': 'skipped', 'reason': why, 'record': record and str(path)}
        fresh = record is None
        record = {'repo': repo, 'pr': pr, 'head': head, 'runId': r['id'], 'attempt': r['run_attempt'], 'evidence': evidence, 'steps': {}}
        write_record(path, record, new=fresh)
    try:
        outcome, why = advance(repo, path, record)
    except OutOfTime as e:
        outcome, why = 'uncertain', f'{e} after the record was written: nothing more is sent for this attempt by this run'
    except READ_FAILED as e:
        outcome, why = 'uncertain', f'a read after the record was written failed ({e!r}); nothing is sent again for this attempt'
    return {**shown, 'outcome': outcome, 'reason': why, 'evidence': record['evidence'], 'newAttempt': record.get('newAttempt'),
            'record': str(path)}


def not_reached(repo, listed):
    """A run on the head left for a later run of this script, the time budget spent before it."""
    path = record_dir('ci-recover') / f"{repo.replace('/', '-')}-{listed['id']}-{listed['run_attempt']}.json"
    if listed.get('status') != 'queued' and not path.exists():
        return None
    return {'runId': listed['id'], 'attempt': listed['run_attempt'], 'workflow': listed.get('name') or listed.get('path'),
            'outcome': 'skipped', 'reason': 'not reached: the time budget ran out first; nothing was sent for it by this run',
            'evidence': [], 'newAttempt': None, 'record': str(path) if path.exists() else None}


def recover(repo, pr, head, wait):
    why = pull_at(pr, head)[1]
    if why:
        return {'outcome': 'none', 'detail': why, 'runs': []}
    listed = [r for r in pages(f'repos/{repo}/actions/runs?head_sha={head}', 'workflow_runs') if r.get('head_sha') == head]
    runs = [x for x in (one(repo, pr, head, wait, r) if left() > 0 else not_reached(repo, r) for r in listed) if x]
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
    global END
    END = facts.DEADLINE = time.monotonic() + BUDGET  # each gh and git command too is bounded by what is left
    try:
        repo = bind_repo(a.repo)
        out = recover(repo, a.pr, a.head, a.wait_minutes)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise Unusable(f'gh answered in a shape ci_recover.py does not read ({e!r})')
    finally:
        facts.DEADLINE = None
    return {**out, 'at': iso(now())}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
