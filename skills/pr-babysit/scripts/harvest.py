#!/usr/bin/env python3
"""Where each requested review bot stands on a PR's head, and every comment its reviewers left.

  harvest.py --pr N --reviewers copilot,coderabbit,greptile,code-scanning --auto-run coderabbit,greptile [--repo OWNER/REPO] [--probe | --carry JSON]

--auto-run names the bots settled from their artifacts, only coderabbit and greptile. Copilot is read
from its newest submitted review on the head; any other reviewer is `harvested`, its state not read.

Run from the PR checkout; --repo names the PR's repository, else the checkout's one repository does (facts.bind_repo). It settles first and harvests second: the bots'
artifacts are read, then the three comment endpoints, then the head again; a
head that moved restarts the read once on the new head, and one that moves
again is an error. The settle rules are this file's, and its tests', and read
known artifact formats only: an artifact on the head whose shape it does not know,
evidence it cannot order, or a read that failed makes that bot `unknown`,
never `absent`. An auto-run Greptile with nothing on the PR is read against
the repository's newest Greptile pause notice too, from PAUSE_GRACE after a dated head event (repo_paused, #515).

stdout ends with one JSON line: headSha, observedAt, headEventAt (null when no
run or timeline event dates the head, when the workflow file of a run that might date it could not be read twice, or
when no bot auto-runs), headEventEvidence (naming that unread file, or saying so when, without
PyYAML, no pull_request run on the head could be read to date it), bots (one record per
--reviewers name: bot, state, kind, sha, evidence, reason, retryAt: when a rate-limited
bot's notice says its next review is available, an hour after a CodeRabbit notice
giving no countdown, else null), and comments (every
inline comment, issue comment and review body by a --reviewers author: kind
review|issue|review-body, commentId, source, author, body verbatim, digest
(pr-reply's comment_digest.py: sha256 of the body, 12 hex, without what
CodeRabbit rewrites at its end), aliases (the digests a body's earlier forms
may have been recorded under; left out when there are none), path, line,
commitId, createdAt, updatedAt, inReplyTo, url), and fingerprint: `<reviewers>|<auto-run>|<32 hex>`, a
sha256 over what the bots left that a review of the head could rest on (each comment's kind, id and digest, each
submitted review by a reviewer, empty ones too, and the head's statuses and check runs from CodeRabbit and Greptile),
never over a time, so a new, edited or dismissed review or a changed bot status changes it. Two bodies no finding comes from are left
out, with body null, bodyOmitted and bodyLength: a CodeRabbit issue comment that opens
with its walkthrough marker (CR_WALKTHROUGH), the auto-generated summary,
"coderabbit-walkthrough"; and a Copilot review body saying it could not review,
"copilot-refusal". Their digest and aliases are still the real body's and identify them,
and its url shows it (`gh api repos/<repo>/issues/comments/<commentId>` for the walkthrough). Exit 2 with {"error": ...} when gh cannot answer the PR, the
head, or a comment endpoint, plus facts.py's rateLimit when GitHub refused gh for a rate limit. A read GitHub answered
HTTP 502, 503 or 504 is made again after lib/gh_retry.py's waits first; a write through gh_json, never.

--carry takes a JSON list of {commentId, digest, judgedSha, paths}: comments whose findings were all settled at
judgedSha, paths the repository-relative files their verdicts rest on (null or [] when unbounded). The full output then
has carry: one record per entry, {commentId, eligible, reason}, plus the comment's current commentDigest,
commentAliases (when it has some) and source when eligible. An entry is eligible only when its comment is in this
harvest with that digest (or an alias of it), the checkout's HEAD is the head, and git shows its paths unchanged, committed
or not, since judgedSha; unbounded paths only on judgedSha the head, with a clean tree. Paths are literal pathspecs.
Any git failure, missing commit or path outside the repository makes it ineligible, never eligible. An eligible
comment's body is left out like the two below, bodyOmitted "carried": the caller restores its findings.

--probe prints only headSha, observedAt, headEventAt, headEventEvidence, bots and fingerprint, sealed (facts.sealed): what a workflow waiting out
CodeRabbit's rate limit compares with its last full harvest (#359).
"""

import base64
import functools
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Only reading a workflow file needs PyYAML; sonar.py and coderabbit_trigger.py import this file under a python3 that may lack it.
try:
    import yaml
except ImportError:
    yaml = None

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, RateLimited, Unusable, attempt, bind_repo, checkout_top, report, run, status  # noqa: E402

PROBE = ('headSha', 'observedAt', 'headEventAt', 'headEventEvidence', 'bots', 'fingerprint')
# reply.py checks a comment against the same digest, so both skills share one definition.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-reply' / 'scripts'))
import comment_digest  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import greptile as greptile_check  # noqa: E402  the confidence check's recogniser, which collect.py shares
from check_runs import latest_run, newest, newest_reading, stamp  # noqa: E402,F401  which collect.py shares
import gh_retry  # noqa: E402

KNOWN = ('copilot', 'coderabbit', 'greptile', 'code-scanning')
SETTLED = ('coderabbit', 'greptile')
AUTHOR = {'code-scanning': 'github-advanced-security'}
HEAD_ACTIVITIES = {'opened', 'synchronize', 'reopened', 'ready_for_review'}
CR_PAUSED = '<!-- This is an auto-generated comment: review paused by coderabbit.ai -->'
CR_RESUME = '`@coderabbitai resume` resumes it'
CR_RATE = '<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->'
CR_WALKTHROUGH = '<!-- This is an auto-generated comment: summarize by coderabbit.ai -->'
CR_RATE_STATUS = 'review rate limited'
CR_REVIEW_FAILED = 'review failed'
# A repository CodeRabbit reviews only on request: a box in its summary comment, ticked once asked, or these words beside it (#324).
CR_TRIGGER_BOX = re.compile(r'^(?P<pre>(?:> )?- \[)(?P<mark>[ xX])(?P<post>\][^\n]*Trigger (?:a manual )?review[^\n]*)$', re.M)
CR_MANUAL_ONLY = 'does not receive automatic reviews'
CR_MANUAL_SKIP = 'review skipped: manual review required'
CR_COUNTDOWN = re.compile(r'Next included review available in ([^.*\n]+)\.')
COUNTDOWN_TERM = re.compile(r'(\d+) (hour|minute|second)s?')
# CodeRabbit's included reviews come back within the hour: the reset a notice without a countdown is given.
ESTIMATED_RESET = timedelta(hours=1)
# The quota notice's known wordings (#308), each alone or followed by the effort hint (#453); any other "unable to
# review" body stays unknown.
COPILOT_QUOTA = ('Copilot was unable to review this pull request because the user who requested the review has reached their quota limit.',
                 'Copilot was unable to review this pull request because the user who requested the review has insufficient remaining quota.')
COPILOT_QUOTA_HINT = 'Try requesting a review at a lower effort level.'
COPILOT_NO_FILES = "Copilot wasn't able to review any files in this pull request."
COPILOT_OVERVIEW = ('<!-- ccr-overview', '## Pull request overview')
REVIEW_SUBMITTED = ('COMMENTED', 'APPROVED', 'CHANGES_REQUESTED')
GREPTILE_PAUSED = 'Greptile has paused reviews on this repository'
# Greptile posts its pause once, on one PR (#515): an absent Greptile looks for it among the most recently updated PRs
# Greptile reviewed, and only Greptile's own account (a login no person can hold) counts.
GREPTILE_BOT = f'{greptile_check.APP}[bot]'
PAUSE_PRS = 10
# The most PRs updated since a notice whose reviews are read to rule out a newer review: one search page.
PAUSE_SINCE = 100
# How long after its head event an absent Greptile is left unread for a pause: pr-babysit's GREPTILE_GRACE_MIN, so the
# notices are read before the workflow may post an @greptileai.
PAUSE_GRACE = timedelta(minutes=2)
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December')
PAUSE_RESUME = re.compile(rf"Reviews resume automatically on ({'|'.join(MONTHS)}) (\d{{1,2}})\b")
# A notice that names no resume date stands at most one billing period.
PAUSE_UNDATED = timedelta(days=31)
GREPTILE_ERROR = 'Greptile encountered an error while reviewing this PR'
GREPTILE_STATUS = '<!-- greptile-status -->'
# How long a below-threshold confidence result may stand before its review on the head counts as not observed: pr-babysit's REVIEW_CAP_MIN.
GREPTILE_REVIEW_WAIT = timedelta(minutes=10)
FAILED_RUN = ('failure', 'cancelled', 'timed_out', 'action_required')
RUN_PROGRESS = {'queued': ('queued', 'check run queued'), 'in_progress': ('working', 'check run in progress')}
WORKFLOW_RETRY_S = 2


class Unreadable(Unusable):
    """A workflow file a run on the head names could not be read, so whether that run dates the head is not known."""


def gh_json(*argv):
    """gh's JSON answer, a read made again on a transient server error (lib/gh_retry.py)."""
    text = gh_retry.read(argv, lambda: run('gh', *argv), Unusable)[1]
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


def applies(created, event_at):
    """A SHA-less artifact counts for the head only when created after its latest head event."""
    if event_at is None:
        return None
    return stamp(created) > stamp(event_at)


def coderabbit(head, statuses, checks, issue, event_at):
    s, tied = newest_reading([x for x in statuses if x.get('context') == 'CodeRabbit'], 'updated_at', lambda x: (x['state'], x.get('description')))
    if tied:
        return record('coderabbit', 'unknown', f'statuses {tied}')
    run_, unordered = latest_run([c for c in checks if c.get('name') == 'CodeRabbit'], title=True)
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
    # CodeRabbit's own rate-limit status or check run on the head dates the notice it edits alongside it.
    by_status = bool(s) and s['state'] == 'success' and (s.get('description') or '').lower().startswith(CR_RATE_STATUS) and \
        (run_at is None or run_at < s_at or run_at == s_at and rate_run(run_))
    by_run = bool(run_) and rate_run(run_) and run_at is not None and (s_at is None or s_at < run_at)
    tied = next((c for c in [pause, *([] if by_status or by_run else rate)] if c and latest_at is not None and stamp(c['updated_at']) == latest_at), None)
    if tied:
        return record('coderabbit', 'unknown', 'a notice and a status or run on the head share one timestamp', [*ev, f"comment {tied['id']}"])
    if pause and (latest_at is None or stamp(pause['updated_at']) > latest_at):
        return record('coderabbit', 'settled', f'reviews paused on this PR; {CR_RESUME}', [*ev, f"pause in comment {pause['id']}"], None, 'paused')
    if by_status or by_run:
        # Its notice may be edited a second before the status or run (#138, #336): one after the head event with no
        # other CodeRabbit status or run between them is the limit's too.
        at = s_at if by_status else run_at
        dated = [stamp(x['updated_at']) for x in statuses if x.get('context') == 'CodeRabbit'] + \
            [stamp(x.get('completed_at') or x.get('started_at')) for x in checks if x.get('name') == 'CodeRabbit']
        floor = max([t for t in (event_at and stamp(event_at), *(t for t in dated if t and t < at)) if t], default=None)
        paired = [c for c in rate if stamp(c['updated_at']) >= at or (floor is not None and stamp(c['updated_at']) > floor)]
        notice, why = newest_reading(paired, 'updated_at', retry_at)
        reason = s.get('description') if by_status else f"check run: {(run_.get('output') or {}).get('title')}"
        return limited_record(reason, notice, why, ev, head, at)
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
        a, b = coderabbit_status(s, ev, head), coderabbit_run(run_, ev, head, stickies)
        # A manual-review skip is askable and a no-files one is not (#372): which of them each reads must agree too.
        def reading(r):
            return r['state'], r['kind'], r['reason'].lower().startswith(CR_MANUAL_SKIP)
        if reading(a) != reading(b):
            return record('coderabbit', 'unknown', 'its status and check run disagree at one timestamp', ev)
    if s and (run_ is None or s_at >= run_at):
        return coderabbit_status(s, ev, head)
    if run_:
        return coderabbit_run(run_, ev, head, stickies)
    return record('coderabbit', 'absent', 'no status and no check run on the head')


def limited_record(reason, notice, why, ev, sha, limited_at=None):
    """CodeRabbit settled as rate-limited, with the notice's reset time, or an hour after the notice when it gives no
    countdown (#138), or after the limiting status or run (limited_at) when no notice is paired with it; none when a
    countdown does not read; `why` says why the notices give none."""
    at = None if why else retry_at(notice)
    if not at and not notice and not why and limited_at:
        at = (limited_at + ESTIMATED_RESET).strftime('%Y-%m-%dT%H:%M:%SZ')
        reason += '; no notice, reset estimated an hour after the limit'
    elif not at and notice and not why:
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


def coderabbit_run(run_, ev, head, stickies):
    if run_['status'] in RUN_PROGRESS:
        return record('coderabbit', *RUN_PROGRESS[run_['status']], ev, head)
    if run_['status'] == 'completed' and run_.get('conclusion') == 'success':
        return record('coderabbit', 'reviewed', 'check run completed', ev, head)
    if run_['status'] == 'completed' and run_.get('conclusion') in ('failure', 'cancelled', 'timed_out'):
        return record('coderabbit', 'settled', f"check run {run_['conclusion']}", ev, head, 'failed')
    if rate_run(run_):
        return record('coderabbit', 'settled', f"check run: {(run_.get('output') or {}).get('title')}", ev, head, 'limited')
    title = ((run_.get('output') or {}).get('title') or '').strip()
    if run_['status'] == 'completed' and run_.get('conclusion') == 'neutral' and title.lower().startswith(CR_REVIEW_FAILED):
        # An error on CodeRabbit's side (#499): its review of the head failed, which a request may ask again.
        return record('coderabbit', 'settled', f'check run: {title}', ev, head, 'failed')
    if run_['status'] == 'completed' and run_.get('conclusion') == 'skipped':
        lowered = title.lower()
        if lowered.startswith('review skipped'):
            # A bare "Review skipped" gives no reason: the summary comment tells a manual-review skip, read as its status says
            # it, from any other; a title naming the manual review says it alone (#530), box or no box.
            why = manual_only(stickies) or (f'titled "{title}"' if lowered.startswith(CR_MANUAL_SKIP) else None)
            return record('coderabbit', 'settled', f'Review skipped: manual review required (check run; {why})' if why else f'check run skipped: {title}',
                          ev, head, 'skipped')
        if lowered.startswith('no files to review'):
            # Terminal for the head (#372): no Trigger review box asks it again, whatever the summary holds.
            return record('coderabbit', 'settled', f'check run skipped: {title}', ev, head, 'skipped')
        if lowered.startswith('review paused'):
            return record('coderabbit', 'settled', f'check run: {title}; {CR_RESUME}', ev, head, 'paused')
    return record('coderabbit', 'unknown', f"check run {run_['status']}/{run_.get('conclusion')} is not one this reads", ev)


def rate_run(run_):
    """CodeRabbit's check run for a rate-limited head (#336): completed neutral, titled as the rate-limit status reads."""
    return run_['status'] == 'completed' and run_.get('conclusion') == 'neutral' and \
        ((run_.get('output') or {}).get('title') or '').strip().lower().startswith(CR_RATE_STATUS)


def manual_only(stickies):
    """Which CodeRabbit comment says it reviews the repository only on request, None when none does."""
    for c in stickies:
        body = c.get('body') or ''
        if CR_TRIGGER_BOX.search(body):
            return f"comment {c['id']} holds a Trigger review box"
        if CR_MANUAL_ONLY in body:
            return f"comment {c['id']} says the repository gets no automatic reviews"
    return None


def greptile(head, checks, issue, reviews, event_at, now=None):
    runs = [c for c in checks if (c.get('app') or {}).get('slug') == greptile_check.APP and c.get('name') == greptile_check.CHECK]
    r, unordered = latest_run(runs, title=True)
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


def repo_paused(repo, n, reviews, absent, event_at, now):
    """An absent Greptile settled limited by its newest pause notice on PR n, the PAUSE_PRS most recently updated PRs it
    reviewed or the repository's newest issue comments, until a newer Greptile review or the notice's resume date
    (PAUSE_UNDATED after it when it names none); `absent` itself when no notice stands or within PAUSE_GRACE of a dated
    head event; unknown when that evidence does not read whole. A newer review is looked for on PR n and every PR
    Greptile reviewed updated since the notice, all of them on one search page or the pause is not asserted, until the
    first one found. `reviews` are PR n's, already read."""
    read = {n: reviews}

    def greptile_reviews(number):
        if number not in read:
            read[number] = pages(f'repos/{repo}/pulls/{number}/reviews')
        return [{'at': v['submitted_at'], 'body': v.get('body') or '', 'source': f"review {v['id']} on PR #{number}"}
                for v in read[number] if (v.get('user') or {}).get('login') == GREPTILE_BOT and v.get('state') in REVIEW_SUBMITTED
                and stamp(v.get('submitted_at'))]

    try:
        event = stamp(event_at)
        if event is not None and now - event < PAUSE_GRACE:
            return {**absent, 'reason': f"{absent['reason']}; the repository's Greptile pause notices are read from "
                                        f"{int(PAUSE_GRACE.total_seconds() // 60)} minutes after the head event at {event_at}"}
        prs = {n, *reviewed_prs(repo, '', PAUSE_PRS)[0]}
        notices = [x for number in sorted(prs) for x in greptile_reviews(number) if GREPTILE_PAUSED in x['body']]
        for c in gh_json('api', f'repos/{repo}/issues/comments?sort=created&direction=desc&per_page=100'):
            if (c.get('user') or {}).get('login') == GREPTILE_BOT and GREPTILE_PAUSED in (c.get('body') or '') and stamp(c.get('created_at')):
                notices.append({'at': c['created_at'], 'body': c['body'], 'source': f"comment {c['id']} on {c.get('html_url') or 'the repository'}"})
        notice = max(notices, key=lambda x: stamp(x['at']), default=None)
        if notice is None:
            return {**absent, 'reason': f"{absent['reason']}; no Greptile pause notice on this PR, among the {PAUSE_PRS} most recently "
                                        'updated PRs Greptile reviewed or in the newest issue comments'}
        at = stamp(notice['at'])
        resume = resume_at(notice['body'], at)
        until = resume or at + PAUSE_UNDATED
        if now >= until:
            return {**absent, 'reason': f"{absent['reason']}; its pause in {notice['source']} at {notice['at']} ended {until:%Y-%m-%dT%H:%M:%SZ}"}
        # Only a notice still in force needs every newer review ruled out; a PR given one after it was updated after it too.
        since, whole = reviewed_prs(repo, f" updated:>={notice['at']}", PAUSE_SINCE)
        if not whole:
            raise Unusable(f"more than {PAUSE_SINCE} PRs Greptile reviewed were updated since {notice['at']}, so no newer review can be ruled out")
        for number in dict.fromkeys([n, *since]):
            later = next((x for x in greptile_reviews(number) if GREPTILE_PAUSED not in x['body'] and stamp(x['at']) > at), None)
            if later:
                return {**absent, 'reason': f"{absent['reason']}; its pause in {notice['source']} at {notice['at']} ended by {later['source']} at {later['at']}"}
    except RateLimited:
        raise
    except (Unusable, KeyError, TypeError, AttributeError, ValueError) as e:
        return record('greptile', 'unknown', f"{absent['reason']}; the repository's Greptile pause notices did not read ({e})")
    stated = 'as it states' if resume else 'a billing period, as it names no resume date'
    return record('greptile', 'settled', f"Greptile paused reviews on this repository at {notice['at']}, until {until:%Y-%m-%dT%H:%M:%SZ} ({stated}); "
                  f'no newer Greptile review on the {len(since)} PR(s) it reviewed updated since', [notice['source']], None, 'limited',
                  f'{resume:%Y-%m-%dT%H:%M:%SZ}' if resume else None)


def reviewed_prs(repo, qualifier, size):
    """(the numbers of the PRs Greptile reviewed matching qualifier, most recently updated first, one search page of
    size, whether that page holds them all); an incomplete search is Unusable."""
    found = gh_json('api', '-X', 'GET', 'search/issues', '-f', f'q=repo:{repo} is:pr reviewed-by:app/{greptile_check.APP}{qualifier}',
                    '-f', 'sort=updated', '-f', 'order=desc', '-f', f'per_page={size}')
    if found['incomplete_results'] is not False:
        raise Unusable('GitHub search returned incomplete results')
    numbers = [p['number'] for p in found['items']]
    return numbers, found['total_count'] == len(numbers)


def resume_at(body, at):
    """The first moment, UTC, of the day a pause notice posted at `at` says reviews resume, None when it names none."""
    m = PAUSE_RESUME.search(body)
    if not m:
        return None
    month = MONTHS.index(m[1]) + 1
    for year in (at.year, at.year + 1):
        try:
            day = datetime(year, month, int(m[2]), tzinfo=timezone.utc)
        except ValueError:
            return None
        if day > at:
            return day
    return None


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
    if any(body.startswith(q) and body[len(q):].strip() in ('', COPILOT_QUOTA_HINT) for q in COPILOT_QUOTA):
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
    """The pull_request activity types a workflow revision runs on, or None when its file does not parse into them, PyYAML
    missing too; a file that does not read raises Unreadable."""
    if yaml is None:
        return None
    doc = workflow_doc(repo, path, sha)
    on = doc.get('on', doc.get(True)) if doc else None  # YAML 1.1 reads a bare `on` as True
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


def workflow_file(repo, path, sha):
    """The contents API's answer for a workflow revision, read twice; Unreadable after that, never taken for a workflow
    without pull_request (#242). A gateway failure gh_json made again already is Unreadable at once."""
    for left in (1, 0):
        try:
            return gh_json('api', f'repos/{repo}/contents/{path}?ref={sha}')
        except RateLimited:
            raise
        except Unusable as e:
            if not left or gh_retry.TRANSIENT.search(str(e)):
                raise Unreadable(f'workflow {path} at {sha[:9]} could not be read ({e})')
            time.sleep(WORKFLOW_RETRY_S)


def workflow_doc(repo, path, sha):
    """A workflow revision's YAML as a mapping, None when it does not parse into one; Unreadable when it does not read.
    The caller checks PyYAML is there."""
    meta = workflow_file(repo, path, sha)
    try:
        doc = yaml.safe_load(base64.b64decode(meta['content']).decode())
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


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


def fingerprint(reviewers, auto, harvested, reviews, statuses, checks):
    """`<reviewers>|<auto-run>|<32 hex>` over the review evidence on the PR, so a later harvest can tell whether any of
    it changed; the reviewer lists are in it because they decide what is read."""
    records = [['comment', c['kind'], c['commentId'], c['digest']] for c in harvested]
    records += [['review', v['id'], v.get('commit_id'), v.get('state'), digest(v.get('body'))]
                for v in reviews if source_of(v['user']['login'], reviewers)]
    records += [['status', x.get('id'), x.get('context'), x['state'], x.get('description')] for x in statuses if x.get('context') == 'CodeRabbit']
    records += [['check', c['id'], c.get('name'), c['status'], c.get('conclusion'), (c.get('output') or {}).get('title'),
                 digest((c.get('output') or {}).get('summary'))]
                for c in checks if c.get('name') == 'CodeRabbit' or (c.get('app') or {}).get('slug') == greptile_check.APP]
    blob = json.dumps(sorted(json.dumps(r) for r in records))
    return f"{','.join(reviewers)}|{','.join(auto)}|{hashlib.sha256(blob.encode()).hexdigest()[:32]}"


def observe(repo, n, reviewers, auto):
    view = gh_json('pr', 'view', str(n), '-R', repo, '--json', 'headRefOid,headRefName,headRepository')
    head = view['headRefOid']
    if not FULL_SHA.match(head or ''):
        raise Unusable(f'gh pr view {n}: no head SHA')
    try:
        event_at, event_why = head_event(repo, n, view, head) if auto else (None, 'no reviewer auto-runs, so no head event is read')
    except Unreadable as e:
        event_at, event_why = None, f'{e}, so whether its run dates the head is not known'
    statuses = pages(f'repos/{repo}/commits/{head}/statuses') if 'coderabbit' in auto else []
    checks = pages(f'repos/{repo}/commits/{head}/check-runs', 'check_runs') if auto else []
    issue = pages(f'repos/{repo}/issues/{n}/comments') if auto else None
    reviews = pages(f'repos/{repo}/pulls/{n}/reviews')
    harvested = comments(repo, n, reviewers, issue, reviews)
    now = datetime.now(timezone.utc)
    bots = []
    for bot in reviewers:
        if bot == 'coderabbit' and bot in auto:
            bots.append(coderabbit(head, statuses, checks, issue, event_at))
        elif bot == 'greptile' and bot in auto:
            g = greptile(head, checks, issue, reviews, event_at, now)
            bots.append(repo_paused(repo, n, reviews, g, event_at, now) if g['state'] == 'absent' else g)
        elif bot == 'copilot':
            bots.append(copilot(head, reviews))
        elif bot == 'greptile' and (paused := greptile_paused(head, reviews, harvested)):
            bots.append(paused)
        else:
            n_ = sum(c['source'] == bot for c in harvested)
            bots.append(record(bot, 'harvested', f'{n_} comment(s) harvested; it does not auto-run here, so its state is not read'))
    return {'headSha': head, 'observedAt': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'headEventAt': event_at, 'headEventEvidence': event_why, 'bots': bots, 'comments': harvested,
            'fingerprint': fingerprint(reviewers, auto, harvested, reviews, statuses, checks)}


CARRY_KEYS = {'commentId', 'digest', 'judgedSha', 'paths'}


def carry_entries(text):
    """--carry's list, or Unusable: the workflow built it, so a malformed one is no entry to judge."""
    try:
        entries = json.loads(text)
    except ValueError as e:
        raise Unusable(f'--carry is not JSON: {e}')
    shaped = isinstance(entries, list) and all(
        isinstance(e, dict) and set(e) == CARRY_KEYS and type(e['commentId']) is int and isinstance(e['digest'], str) and
        isinstance(e['judgedSha'], str) and FULL_SHA.match(e['judgedSha']) and
        (e['paths'] is None or isinstance(e['paths'], list) and all(isinstance(x, str) for x in e['paths'])) for e in entries)
    if not shaped:
        raise Unusable('--carry must be a JSON list of {commentId, digest, judgedSha (full SHA), paths (list of strings or null)}')
    if len({e['commentId'] for e in entries}) != len(entries):
        raise Unusable('--carry names a comment twice')
    return entries


def unchanged_since(judged, paths, head):
    """None when the checkout holds what was judged at `judged` for these paths, else why not."""
    if not paths and judged != head:
        return 'its verdicts name no bounded dependencies, and were judged on another head'
    bad = [x for x in paths or [] if not x or x.startswith('/') or '\\' in x or '..' in x.split('/')]
    if bad:
        return f'not repository-relative: {bad}'
    try:
        dirty = status(*(paths or []))
    except Unusable as e:
        return f'git status failed: {e}'
    if dirty:
        return f'uncommitted changes: {sorted(re.sub(r"^.. ", "", x) for x in dirty)}'
    if judged == head:
        return None
    # NUL-separated: a name of only whitespace is still a change.
    code, out, err = attempt('git', '--literal-pathspecs', 'diff', '--name-only', '-z', '--no-renames', '--no-ext-diff', judged, head, '--', *(paths or []))
    if code != 0:
        return f'git cannot compare {judged[:8]} with the head: {err.strip()}'
    return f'changed since {judged[:8]}: {sorted(x for x in out.split(chr(0)) if x)}' if out else None


def carried(entries, harvested, head):
    """One record per --carry entry: whether its comment keeps its settled findings. An eligible comment's body is
    omitted from `harvested` (bodyOmitted "carried"): the caller restores its findings, so nobody reads it."""
    if not entries:
        return []
    by_id = {c['commentId']: c for c in harvested}
    try:
        checkout_top()  # paths are the repository's, which git reads relative to the working directory
        code, out, _ = attempt('git', 'rev-parse', 'HEAD')
        at = out.strip() if code == 0 else None
        away = None if at == head else f'the checkout is at {at or "an unreadable HEAD"}, not the head'
    except Unusable as e:
        away = str(e)
    got = []
    for e in entries:
        c = by_id.get(e['commentId'])
        why = (away if away
               else 'not in this harvest' if c is None
               else 'its body changed' if e['digest'] not in (c['digest'], *c.get('aliases', ()))
               else unchanged_since(e['judgedSha'], e['paths'], head))
        if why is None:
            if c['body'] is not None:
                c.update(body=None, bodyOmitted='carried', bodyLength=len(c['body']))
            got.append({'commentId': e['commentId'], 'eligible': True, 'reason': f"unchanged since {e['judgedSha'][:8]}",
                        'commentDigest': c['digest'], **({'commentAliases': c['aliases']} if c.get('aliases') else {}), 'source': c['source']})
        else:
            got.append({'commentId': e['commentId'], 'eligible': False, 'reason': why})
    return got


def collect(argv):
    p = Parser(prog='harvest.py', add_help=False)
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--reviewers', required=True)
    p.add_argument('--auto-run', default='')
    p.add_argument('--repo')
    p.add_argument('--probe', action='store_true')
    p.add_argument('--carry')
    a = p.parse_args(argv)
    if a.probe and a.carry is not None:
        raise Unusable('--probe reads no comments, so it takes no --carry')
    entries = carry_entries(a.carry) if a.carry is not None else []
    reviewers = [r for r in a.reviewers.split(',') if r]
    auto = [r for r in a.auto_run.split(',') if r]
    unknown = [r for r in reviewers if r not in KNOWN] + [r for r in auto if r not in SETTLED]
    if unknown or not set(auto) <= set(reviewers):
        raise Unusable(f'reviewers are {", ".join(KNOWN)}, and --auto-run a subset of them among {", ".join(SETTLED)}: {unknown or auto}')
    repo = bind_repo(a.repo)
    try:
        for _ in range(2):
            seen = observe(repo, a.pr, reviewers, auto)
            again = gh_json('pr', 'view', str(a.pr), '-R', repo, '--json', 'headRefOid')['headRefOid']
            if again == seen['headSha']:
                return {k: seen[k] for k in PROBE} if a.probe else {**seen, 'carry': carried(entries, seen['comments'], seen['headSha'])}
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise Unusable(f'gh answered in a shape harvest.py does not read ({e!r})')
    raise Unusable(f'the head moved during two reads in a row (now {again}); harvest again once it settles')


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal='--probe' in sys.argv[1:], limits=True))
