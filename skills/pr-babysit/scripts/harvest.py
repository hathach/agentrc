#!/usr/bin/env python3
"""Where each requested review bot stands on a PR's head, and every comment its reviewers left.

  harvest.py --pr N --reviewers copilot,coderabbit,greptile,code-scanning --auto-run coderabbit,greptile

--auto-run names the bots settled from their artifacts, only coderabbit and greptile. Copilot is read
from its newest submitted review on the head; any other reviewer is `harvested`, its state not read.

Run from the PR checkout. It settles first and harvests second: the bots'
artifacts are read, then the three comment endpoints, then the head again; a
head that moved restarts the read once on the new head, and one that moves
again is an error. The settle rules are this file's, and its tests', and read
known artifact formats only: an artifact on the head whose shape it does not know,
evidence it cannot order, or a read that failed makes that bot `unknown`,
never `absent`.

stdout ends with one JSON line: headSha, observedAt, headEventAt (null when no
run or timeline event dates the head, or no bot auto-runs), headEventEvidence (saying so when, without
PyYAML, no pull_request run on the head could be read to date it), bots (one record per
--reviewers name: bot, state, kind, sha, evidence, reason, retryAt: when a rate-limited
bot's notice says its next review is available, an hour after a CodeRabbit notice
giving no countdown, else null), and comments (every
inline comment, issue comment and review body by a --reviewers author: kind
review|issue|review-body, commentId, source, author, body verbatim, digest
(pr-reply's comment_digest.py: sha256 of the body, 12 hex, without what
CodeRabbit rewrites at its end), aliases (the digests a body's earlier forms
may have been recorded under; left out when there are none), path, line,
commitId, createdAt, updatedAt, inReplyTo, url). Two bodies no finding comes from are left
out, with body null, bodyOmitted and bodyLength: a CodeRabbit issue comment that opens
with its walkthrough marker (CR_WALKTHROUGH), the auto-generated summary,
"coderabbit-walkthrough"; and a Copilot review body saying it could not review,
"copilot-refusal". Their digest and aliases are still the real body's and identify them,
and its url shows it (`gh api repos/<repo>/issues/comments/<commentId>` for the walkthrough). Exit 2 with {"error": ...} when gh cannot answer the PR, the
head, or a comment endpoint, plus facts.py's rateLimit when GitHub refused gh for a rate limit.
"""

import base64
import functools
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Only reading a workflow file needs PyYAML; sonar.py and coderabbit_trigger.py import this file under a python3 that may lack it.
try:
    import yaml
except ImportError:
    yaml = None

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, RateLimited, Unusable, report, run  # noqa: E402

# reply.py checks a comment against the same digest, so both skills share one definition.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-reply' / 'scripts'))
import comment_digest  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import greptile as greptile_check  # noqa: E402  the confidence check's recogniser, which collect.py shares

KNOWN = ('copilot', 'coderabbit', 'greptile', 'code-scanning')
SETTLED = ('coderabbit', 'greptile')
AUTHOR = {'code-scanning': 'github-advanced-security'}
HEAD_ACTIVITIES = {'opened', 'synchronize', 'reopened', 'ready_for_review'}
CR_PAUSED = '<!-- This is an auto-generated comment: review paused by coderabbit.ai -->'
CR_RATE = '<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->'
CR_WALKTHROUGH = '<!-- This is an auto-generated comment: summarize by coderabbit.ai -->'
CR_RATE_STATUS = 'review rate limited'
CR_COUNTDOWN = re.compile(r'Next included review available in ([^.*\n]+)\.')
COUNTDOWN_TERM = re.compile(r'(\d+) (hour|minute|second)s?')
# CodeRabbit's included reviews come back within the hour: the reset a notice without a countdown is given.
ESTIMATED_RESET = timedelta(hours=1)
COPILOT_QUOTA = 'Copilot was unable to review this pull request because the user who requested the review has reached their quota limit.'
COPILOT_NO_FILES = "Copilot wasn't able to review any files in this pull request."
COPILOT_OVERVIEW = ('<!-- ccr-overview', '## Pull request overview')
REVIEW_SUBMITTED = ('COMMENTED', 'APPROVED', 'CHANGES_REQUESTED')
GREPTILE_PAUSED = 'Greptile has paused reviews on this repository'
GREPTILE_ERROR = 'Greptile encountered an error while reviewing this PR'
GREPTILE_STATUS = '<!-- greptile-status -->'
# How long a below-threshold confidence result may stand before its review on the head counts as not observed: pr-babysit's REVIEW_CAP_MIN.
GREPTILE_REVIEW_WAIT = timedelta(minutes=10)
FAILED_RUN = ('failure', 'cancelled', 'timed_out', 'action_required')
RUN_PROGRESS = {'queued': ('queued', 'check run queued'), 'in_progress': ('working', 'check run in progress')}


def gh_json(*argv):
    text = run('gh', *argv)[1]
    try:
        return json.loads(text)
    except ValueError:
        raise Unusable(f"gh {' '.join(argv)}: not JSON")


def pages(path, key=None):
    """Every item of a paginated REST list, 100 a page; `key` names the list inside an object-wrapped page."""
    got = gh_json('api', '--paginate', '--slurp', f"{path}{'&' if '?' in path else '?'}per_page=100")
    return [x for page in got for x in (page[key] if key else page)]


def source_of(login, reviewers):
    login = (login or '').lower()
    return next((r for r in reviewers if AUTHOR.get(r, r) in login), None)


def digest(body):
    """The raw body's: what pr-review and sonar.py compare, with no CodeRabbit rule."""
    return comment_digest.sha12(body or '')


def stamp(t):
    return datetime.fromisoformat(t.replace('Z', '+00:00')) if t else None


def record(bot, state, reason, evidence=(), sha=None, kind=None, retry_at=None):
    return {'bot': bot, 'state': state, 'kind': kind, 'sha': sha, 'evidence': list(evidence), 'reason': reason, 'retryAt': retry_at}


def retry_at(notice):
    """When a CodeRabbit rate-limit notice says the next review is available, counted from its last edit; None when its countdown does not read."""
    m = CR_COUNTDOWN.search((notice or {}).get('body') or '')
    if not m:
        return None
    terms = COUNTDOWN_TERM.findall(m.group(1))
    units = [unit for _, unit in terms]
    if not terms or len(set(units)) != len(units) or COUNTDOWN_TERM.sub('', m.group(1)).replace('and', '').replace(',', '').strip():
        return None
    return after(notice, timedelta(**{f'{unit}s': int(n) for n, unit in terms}))


def after(notice, wait):
    """The notice's last edit plus wait, as an ISO 8601 UTC string."""
    return (stamp(notice['updated_at']) + wait).strftime('%Y-%m-%dT%H:%M:%SZ')


def newest(items, key):
    dated = [x for x in items if x.get(key)]
    return max(dated, key=lambda x: stamp(x[key])) if dated else None


def newest_reading(items, key, reading):
    """(the newest item, '') or (None, why) when one is undated, or the newest share one timestamp and read differently."""
    undated = next((x for x in items if not x.get(key)), None)
    if undated:
        return None, f"{undated.get('id')} has no {key} to order it by"
    top = newest(items, key)
    tied = [x for x in items if top and x.get(key) and stamp(x[key]) == stamp(top[key])]
    if len({repr(reading(x)) for x in tied}) > 1:
        return None, f"{', '.join(str(x.get('id')) for x in tied)} share {top[key]} and read differently"
    return top, ''


def latest_run(runs):
    """(the newest check run, '') or (None, why) when runs cannot be ordered: one has no start time
    (GitHub leaves a queued run undated), or the newest start differently."""
    undated = [r for r in runs if not r.get('started_at')]
    if undated and len(runs) > 1:
        return None, f"check run {undated[0]['id']} has no start time to order it by"
    if undated:
        return undated[0], ''
    return newest_reading(runs, 'started_at', lambda r: (r['status'], r.get('conclusion'), (r.get('output') or {}).get('summary')))


def applies(created, event_at):
    """A SHA-less artifact counts for the head only when created after its latest head event."""
    if event_at is None:
        return None
    return stamp(created) > stamp(event_at)


def coderabbit(head, statuses, checks, issue, event_at):
    s, tied = newest_reading([x for x in statuses if x.get('context') == 'CodeRabbit'], 'updated_at', lambda x: (x['state'], x.get('description')))
    if tied:
        return record('coderabbit', 'unknown', f'statuses {tied}')
    run_, unordered = latest_run([c for c in checks if c.get('name') == 'CodeRabbit'])
    if unordered:
        return record('coderabbit', 'unknown', unordered)
    ev = []
    if s:
        ev.append(f"status {s['state']} '{s.get('description') or ''}' at {s['updated_at']}")
    if run_:
        ev.append(f"check run {run_['status']}/{run_.get('conclusion')} at {run_.get('started_at')}")
    s_at = stamp(s['updated_at']) if s else None
    run_at = stamp(run_.get('completed_at') or run_.get('started_at')) if run_ else None
    if s and run_ and run_at is None:
        return record('coderabbit', 'unknown', 'a status and an undated check run cannot be ordered', ev)
    latest_at = max((t for t in (s_at, run_at) if t), default=None)
    stickies = [c for c in issue if 'coderabbit' in (c['user']['login'] or '').lower()]
    pause, why = newest_reading([c for c in stickies if CR_PAUSED in (c.get('body') or '')], 'updated_at', lambda c: 'paused')
    if why:
        return record('coderabbit', 'unknown', f'pause comments: {why}', ev)
    rate = [c for c in stickies if CR_RATE in (c.get('body') or '')]
    # CodeRabbit's own rate-limit status on the head dates the notice it edits alongside it.
    by_status = bool(s) and s['state'] == 'success' and (s.get('description') or '').lower().startswith(CR_RATE_STATUS) and (run_at is None or run_at < s_at)
    tied = next((c for c in [pause, *([] if by_status else rate)] if c and latest_at is not None and stamp(c['updated_at']) == latest_at), None)
    if tied:
        return record('coderabbit', 'unknown', 'a notice and a status or run on the head share one timestamp', [*ev, f"comment {tied['id']}"])
    if pause and (latest_at is None or stamp(pause['updated_at']) > latest_at):
        return record('coderabbit', 'settled', 'reviews paused on this PR', [*ev, f"pause in comment {pause['id']}"], None, 'paused')
    if by_status:
        # Its notice may be edited a second before the status (#138): one after the head event with no other
        # CodeRabbit status or run between them is the status's too.
        before = [stamp(x['updated_at']) for x in statuses if x.get('context') == 'CodeRabbit' and x is not s and stamp(x['updated_at']) < s_at]
        floor = max([t for t in (event_at and stamp(event_at), run_at, *before) if t], default=None)
        paired = [c for c in rate if stamp(c['updated_at']) >= s_at or (floor is not None and stamp(c['updated_at']) > floor)]
        notice, why = newest_reading(paired, 'updated_at', retry_at)
        return limited_record(s.get('description'), notice, why, ev, head)
    # A notice last touched before a status or run on the head is superseded, and an edited
    # one last touched before the head event is about an older push; any other edit hides when it was posted.
    rate = [c for c in rate if latest_at is None or stamp(c['updated_at']) > latest_at]
    edited = next((c for c in rate if c['created_at'] != c['updated_at'] and (event_at is None or stamp(c['updated_at']) > stamp(event_at))), None)
    if edited:
        return record('coderabbit', 'unknown', 'a rate-limit notice in an edited comment cannot be dated', [*ev, f"comment {edited['id']}"])
    if rate and event_at is None:
        return record('coderabbit', 'unknown', 'a rate-limit notice, and the head event time is unknown', [*ev, f"comment {rate[0]['id']}"])
    fresh = [c for c in rate if c['created_at'] == c['updated_at'] and applies(c['created_at'], event_at)]
    if fresh:
        limited, why = newest_reading(fresh, 'created_at', retry_at)
        return limited_record('rate-limited after the head event', limited or fresh[0], why, ev, None)
    if s and run_ and s_at == run_at:
        a, b = coderabbit_status(s, ev, head), coderabbit_run(run_, ev, head)
        if (a['state'], a['kind']) != (b['state'], b['kind']):
            return record('coderabbit', 'unknown', 'its status and check run disagree at one timestamp', ev)
    if s and (run_ is None or s_at >= run_at):
        return coderabbit_status(s, ev, head)
    if run_:
        return coderabbit_run(run_, ev, head)
    return record('coderabbit', 'absent', 'no status and no check run on the head')


def limited_record(reason, notice, why, ev, sha):
    """CodeRabbit settled as rate-limited, with the notice's reset time, or an hour after the notice when it gives no
    countdown (#138), none when its countdown does not read; `why` says why the notices give none."""
    at = None if why else retry_at(notice)
    if not at and notice and not why:
        if CR_COUNTDOWN.search(notice.get('body') or ''):
            reason += '; its countdown did not read, no reset time'
        else:
            at = after(notice, ESTIMATED_RESET)
            reason += '; no countdown read, reset estimated an hour after the notice'
    elif not at:
        reason += f"; no reset time read{f' ({why})' if why else ''}"
    return record('coderabbit', 'settled', reason, [*ev, *([f"comment {notice['id']}"] if notice else [])], sha, 'limited', at)


def coderabbit_status(s, ev, head):
    desc, state = s.get('description') or '', s['state']
    if state == 'pending':
        return record('coderabbit', 'working' if 'in progress' in desc.lower() else 'queued', desc or 'pending', ev, head)
    if state == 'success':
        if desc.lower().startswith('review skipped'):
            return record('coderabbit', 'settled', desc, ev, head, 'skipped')
        if desc.lower().startswith('review completed'):
            return record('coderabbit', 'reviewed', desc, ev, head)
        if desc.lower().startswith(CR_RATE_STATUS):
            return record('coderabbit', 'settled', desc, ev, head, 'limited')
        return record('coderabbit', 'unknown', f'a success status this does not read: {desc!r}', ev)
    if state in ('failure', 'error'):
        return record('coderabbit', 'settled', desc or state, ev, head, 'failed')
    return record('coderabbit', 'unknown', f'status state {state!r} is not one this reads', ev)


def coderabbit_run(run_, ev, head):
    if run_['status'] in RUN_PROGRESS:
        return record('coderabbit', *RUN_PROGRESS[run_['status']], ev, head)
    if run_['status'] == 'completed' and run_.get('conclusion') == 'success':
        return record('coderabbit', 'reviewed', 'check run completed', ev, head)
    if run_['status'] == 'completed' and run_.get('conclusion') in ('failure', 'cancelled', 'timed_out'):
        return record('coderabbit', 'settled', f"check run {run_['conclusion']}", ev, head, 'failed')
    return record('coderabbit', 'unknown', f"check run {run_['status']}/{run_.get('conclusion')} is not one this reads", ev)


def greptile(head, checks, issue, reviews, event_at, now=None):
    runs = [c for c in checks if (c.get('app') or {}).get('slug') == greptile_check.APP and c.get('name') == greptile_check.CHECK]
    r, unordered = latest_run(runs)
    if unordered:
        return record('greptile', 'unknown', unordered)
    refusals = []
    for c in issue:
        body = c.get('body') or ''
        if 'greptile' not in (c['user']['login'] or '').lower():
            continue
        notice = body.lstrip().startswith(GREPTILE_STATUS)
        if notice and 'Too many files changed for review' in body:
            kind = 'skipped'
        elif notice and GREPTILE_ERROR in body:
            kind = 'failed'
        elif GREPTILE_PAUSED in body:
            kind = 'limited'
        else:
            continue
        refusals.append((kind, c['created_at'], f"comment {c['id']}", applies(c['created_at'], event_at)))
    for v in reviews:
        if 'greptile' in (v['user']['login'] or '').lower() and GREPTILE_PAUSED in (v.get('body') or ''):
            refusals.append(('limited', v['submitted_at'], f"review {v['id']} on {(v.get('commit_id') or '')[:8]}", v.get('commit_id') == head))
    run_at = stamp((r or {}).get('started_at'))
    if r and run_at is None and any(fresh for *_, fresh in refusals):
        return record('greptile', 'unknown', 'an undated check run and a refusal cannot be ordered', [f"check run {r['id']} {r['status']}"])
    ev = [f"check run {r['id']} {r['status']}/{r.get('conclusion')} at {r.get('started_at')}"] if r else []
    for kind, at, what, fresh in sorted(refusals, key=lambda x: stamp(x[1]), reverse=True):
        if fresh is None:
            return record('greptile', 'unknown', f'a {kind} notice, and the head event time is unknown', [*ev, what])
        if fresh and run_at is not None and stamp(at) == run_at:
            return record('greptile', 'unknown', f'a {kind} notice and the check run share one timestamp', [*ev, what])
        if fresh and (run_at is None or stamp(at) > run_at):
            if any(f and k != kind and stamp(a) == stamp(at) for k, a, _, f in refusals):
                return record('greptile', 'unknown', f'refusals of different kinds share {at}', [*ev, what])
            return record('greptile', 'settled', f'{kind} notice after the head event', [*ev, what], None, kind)
    if not r:
        return record('greptile', 'absent', 'no check run and no applicable refusal on the head')
    summary = ((r.get('output') or {}).get('summary') or '').strip()
    title = ((r.get('output') or {}).get('title') or '').strip()
    if r['status'] in RUN_PROGRESS:
        return record('greptile', *RUN_PROGRESS[r['status']], ev, head)
    if r['status'] == 'completed' and r.get('conclusion') == 'success' and summary.startswith('Greptile has reviewed the Pull Request.'):
        return record('greptile', 'reviewed', 'check run completed with a review', ev, head)
    if r['status'] == 'completed' and r.get('conclusion') == 'neutral' and summary.startswith(GREPTILE_ERROR + '.'):
        return record('greptile', 'settled', 'check run reports an error', ev, head, 'failed')
    below = greptile_check.threshold(r)
    if r['status'] == 'completed' and below == 'complete':
        return below_threshold(head, r, title, reviews, ev, now or datetime.now(timezone.utc))
    if r['status'] == 'completed' and r.get('conclusion') in FAILED_RUN and below is None:
        return record('greptile', 'settled', f"check run {r['conclusion']}", ev, head, 'failed')
    return record('greptile', 'unknown', f"check run {r['status']}/{r.get('conclusion')} with a summary this does not read", ev)


def head_reviews(reviews, bot, head):
    """A bot's submitted reviews on the head."""
    return [v for v in reviews or [] if bot in (v['user']['login'] or '').lower() and v.get('commit_id') == head and v.get('state') in REVIEW_SUBMITTED]


def below_threshold(head, r, title, reviews, ev, now):
    """A confidence result below the threshold is Greptile's review scored, once its review on the head is posted; the
    review may land either side of the check, so the result alone waits GREPTILE_REVIEW_WAIT for it."""
    mine = [v for v in head_reviews(reviews, 'greptile', head) if GREPTILE_PAUSED not in (v.get('body') or '')]
    if mine:
        v = newest(mine, 'submitted_at') or mine[0]
        return record('greptile', 'reviewed', f'review posted, its confidence below the threshold: {title}', [*ev, f"review {v['id']} on {head[:8]}"], head)
    done = stamp(r.get('completed_at'))
    if done is None:
        return record('greptile', 'unknown', 'a confidence result with no completion time, and no Greptile review on the head', ev)
    if now - done < GREPTILE_REVIEW_WAIT:
        return record('greptile', 'working', f"confidence result at {r['completed_at']}, its review on the head not posted yet", ev, head)
    wait = int(GREPTILE_REVIEW_WAIT.total_seconds() // 60)
    return record('greptile', 'settled', f"review not observed: no Greptile review on the head {wait} minutes after its confidence result at {r['completed_at']}",
                  ev, head, 'failed')


def greptile_paused(head, reviews, harvested):
    """Greptile outside autoRun: settled limited when its only output on the head is a pause notice (#193), else None."""
    mine = head_reviews(reviews, 'greptile', head)
    inline = any(c['source'] == 'greptile' and c['kind'] == 'review' and c['commitId'] == head for c in harvested)
    if not mine or inline or not all(GREPTILE_PAUSED in (v.get('body') or '') for v in mine):
        return None
    v = newest(mine, 'submitted_at') or mine[0]
    return record('greptile', 'settled', 'paused notice on the head: Greptile did not review it', [f"review {v['id']} on {head[:8]}"], head, 'limited')


def copilot_reading(body):
    """(state, kind, reason) for a Copilot review body."""
    body = (body or '').strip()
    if body == COPILOT_QUOTA:
        return 'settled', 'limited', 'the requesting user reached the Copilot quota'
    if body.startswith(COPILOT_NO_FILES):
        return 'settled', 'skipped', 'no file in the PR is one Copilot reviews'
    if body.startswith(COPILOT_OVERVIEW):
        return 'reviewed', None, 'review on the head'
    return 'unknown', None, 'a Copilot review body this does not read'


def copilot(head, reviews):
    mine = head_reviews(reviews, 'copilot', head)
    if not mine:
        return record('copilot', 'absent', 'no Copilot review on the head')
    v, why = newest_reading(mine, 'submitted_at', lambda v: copilot_reading(v.get('body')))
    if why:
        return record('copilot', 'unknown', f'reviews: {why}')
    state, kind, reason = copilot_reading(v.get('body'))
    return record('copilot', state, reason, [f"review {v['id']} on {head[:8]}"], head if state != 'unknown' else None, kind)


def head_event(repo, n, view, head):
    """(the latest head event's time, its evidence), (None, why) when nothing dates it."""
    branch, head_repo = view['headRefName'], view['headRepository']['nameWithOwner']
    runs = sorted(pages(f'repos/{repo}/actions/runs?head_sha={head}', 'workflow_runs'), key=lambda x: stamp(x['created_at']), reverse=True)
    best = next((x for x in runs if dates_head(repo, x, branch, head_repo)), None)
    timeline = pages(f'repos/{repo}/issues/{n}/timeline')
    # A reopen or ready event keeps the head (GitHub leaves its commit_id null); one naming another commit is about an earlier push.
    tl = newest([e for e in timeline if e.get('event') in ('reopened', 'ready_for_review') and e.get('commit_id') in (None, head)], 'created_at')
    if tl and (best is None or stamp(tl['created_at']) > stamp(best['created_at'])):
        at, why = tl['created_at'], f"timeline {tl['event']} at {tl['created_at']}"
    elif best:
        at, why = best['created_at'], f"run {best['id']} ({best['event']}, {best['path']}) created {best['created_at']}"
    else:
        at, why = None, 'no push or head-activity pull_request run on this branch and head, and no reopen or ready event'
    if yaml is None and any(x.get('event') == 'pull_request' for x in runs):
        why += '; PyYAML is not installed, so no pull_request run was read'
    return at, why


def dates_head(repo, run_, branch, head_repo):
    """Whether a run on the head was started by the head's arrival: a push, or a pull_request run on head activity only."""
    if run_.get('head_branch') != branch or (run_.get('head_repository') or {}).get('full_name') != head_repo:
        return False
    if run_.get('event') == 'push':
        return True
    types = pr_types(repo, run_['path'], run_['head_sha']) if run_.get('event') == 'pull_request' else None
    return types is not None and types <= HEAD_ACTIVITIES


@functools.cache
def pr_types(repo, path, sha):
    """The pull_request activity types a workflow revision runs on, or None when they cannot be read, PyYAML missing too."""
    if yaml is None:
        return None
    try:
        meta = gh_json('api', f'repos/{repo}/contents/{path}?ref={sha}')
        doc = yaml.safe_load(base64.b64decode(meta['content']).decode())
    except RateLimited:
        raise
    except (Unusable, KeyError, ValueError, yaml.YAMLError):
        return None
    on = doc.get('on', doc.get(True)) if isinstance(doc, dict) else None  # YAML 1.1 reads a bare `on` as True
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {k: None for k in on}
    if not isinstance(on, dict) or 'pull_request' not in on:
        return None
    spec = on['pull_request'] or {}
    types = spec.get('types') if isinstance(spec, dict) else None
    if types is None:
        return {'opened', 'synchronize', 'reopened'}
    return set([types] if isinstance(types, str) else types)


def entry(kind, c, src, body, created, updated, path=None, line=None, commit=None, reply_to=None):
    author = c['user']['login']
    first, *also = comment_digest.digests(body, author)
    walkthrough = kind == 'issue' and src == 'coderabbit' and (body or '').lstrip().startswith(CR_WALKTHROUGH)
    refusal = kind == 'review-body' and src == 'copilot' and copilot_reading(body)[0] == 'settled'
    omitted = 'coderabbit-walkthrough' if walkthrough else 'copilot-refusal' if refusal else None
    shown = {'body': None, 'bodyOmitted': omitted, 'bodyLength': len(body)} if omitted else {'body': body or ''}
    return {'kind': kind, 'commentId': c['id'], 'source': src, 'author': author, **shown,
            'digest': first, **({'aliases': also} if also else {}),
            'path': path, 'line': line, 'commitId': commit, 'createdAt': created, 'updatedAt': updated, 'inReplyTo': reply_to, 'url': c['html_url']}


def comments(repo, n, reviewers, issue=None, reviews=None):
    """Every comment by a reviewer; `issue` and `reviews` are those endpoints already read, None when they were not."""
    got = []
    for c in pages(f'repos/{repo}/pulls/{n}/comments'):
        src = source_of(c['user']['login'], reviewers)
        if src:
            got.append(entry('review', c, src, c.get('body'), c['created_at'], c['updated_at'], c.get('path'),
                             c.get('line') or c.get('original_line'), c.get('commit_id'), c.get('in_reply_to_id')))
    for c in issue if issue is not None else pages(f'repos/{repo}/issues/{n}/comments'):
        src = source_of(c['user']['login'], reviewers)
        if src:
            got.append(entry('issue', c, src, c.get('body'), c['created_at'], c['updated_at']))
    for r in reviews if reviews is not None else pages(f'repos/{repo}/pulls/{n}/reviews'):
        src = source_of(r['user']['login'], reviewers)
        if src and (r.get('body') or '').strip():
            got.append(entry('review-body', r, src, r['body'], r.get('submitted_at'), r.get('submitted_at'), commit=r.get('commit_id')))
    return got


def observe(repo, n, reviewers, auto):
    view = gh_json('pr', 'view', str(n), '--json', 'headRefOid,headRefName,headRepository')
    head = view['headRefOid']
    if not FULL_SHA.match(head or ''):
        raise Unusable(f'gh pr view {n}: no head SHA')
    event_at, event_why = head_event(repo, n, view, head) if auto else (None, 'no reviewer auto-runs, so no head event is read')
    statuses = pages(f'repos/{repo}/commits/{head}/statuses') if 'coderabbit' in auto else []
    checks = pages(f'repos/{repo}/commits/{head}/check-runs', 'check_runs') if auto else []
    issue = pages(f'repos/{repo}/issues/{n}/comments') if auto else None
    reviews = pages(f'repos/{repo}/pulls/{n}/reviews') if 'greptile' in reviewers or 'copilot' in reviewers else None
    harvested = comments(repo, n, reviewers, issue, reviews)
    now = datetime.now(timezone.utc)
    bots = []
    for bot in reviewers:
        if bot == 'coderabbit' and bot in auto:
            bots.append(coderabbit(head, statuses, checks, issue, event_at))
        elif bot == 'greptile' and bot in auto:
            bots.append(greptile(head, checks, issue, reviews, event_at, now))
        elif bot == 'copilot':
            bots.append(copilot(head, reviews))
        elif bot == 'greptile' and (paused := greptile_paused(head, reviews, harvested)):
            bots.append(paused)
        else:
            n_ = sum(c['source'] == bot for c in harvested)
            bots.append(record(bot, 'harvested', f'{n_} comment(s) harvested; it does not auto-run here, so its state is not read'))
    return {'headSha': head, 'observedAt': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'headEventAt': event_at, 'headEventEvidence': event_why, 'bots': bots, 'comments': harvested}


def collect(argv):
    p = Parser(prog='harvest.py', add_help=False)
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--reviewers', required=True)
    p.add_argument('--auto-run', default='')
    a = p.parse_args(argv)
    reviewers = [r for r in a.reviewers.split(',') if r]
    auto = [r for r in a.auto_run.split(',') if r]
    unknown = [r for r in reviewers if r not in KNOWN] + [r for r in auto if r not in SETTLED]
    if unknown or not set(auto) <= set(reviewers):
        raise Unusable(f'reviewers are {", ".join(KNOWN)}, and --auto-run a subset of them among {", ".join(SETTLED)}: {unknown or auto}')
    try:
        repo = gh_json('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner']
        for _ in range(2):
            seen = observe(repo, a.pr, reviewers, auto)
            again = gh_json('pr', 'view', str(a.pr), '--json', 'headRefOid')['headRefOid']
            if again == seen['headSha']:
                return seen
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise Unusable(f'gh answered in a shape harvest.py does not read ({e!r})')
    raise Unusable(f'the head moved during two reads in a row (now {again}); harvest again once it settles')


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], limits=True))
