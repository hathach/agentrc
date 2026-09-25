#!/usr/bin/env python3
"""Where each auto-running review bot stands on a PR's head, and every comment its reviewers left.

  harvest.py --pr N --reviewers codex,copilot,coderabbit,greptile,code-scanning --auto-run coderabbit,greptile

--auto-run names only bots it settles: code-scanning is harvested, never settled.

Run from the PR checkout. It settles first and harvests second: the bots'
artifacts are read, then the three comment endpoints, then the head again; a
head that moved restarts the read once on the new head, and one that moves
again is an error. The settle rules are this file's, and its tests', and read
known artifact formats only: an artifact on the head whose shape it does not know,
evidence it cannot order, or a read that failed makes that bot `unknown`,
never `absent`.

stdout ends with one JSON line: headSha, observedAt, headEventAt (null when no
run or timeline event dates the head, or no bot auto-runs), headEventEvidence, bots (one record per
--auto-run name: bot, state, kind, sha, evidence, reason), and comments (every
inline comment, issue comment and review body by a --reviewers author: kind
review|issue|review-body, commentId, source, author, body verbatim, digest
(sha256 of the body, 12 hex), path, line, commitId, createdAt, updatedAt,
inReplyTo, url). Exit 2 with {"error": ...} when gh cannot answer the PR, the
head, or a comment endpoint.
"""

import base64
import functools
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, report, run  # noqa: E402

KNOWN = ('codex', 'copilot', 'coderabbit', 'greptile', 'code-scanning')
SETTLED = ('codex', 'copilot', 'coderabbit', 'greptile')
AUTHOR = {'code-scanning': 'github-advanced-security'}
HEAD_ACTIVITIES = {'opened', 'synchronize', 'reopened', 'ready_for_review'}
COPILOT_LIMITED = 'Copilot was unable to review this pull request because the user who requested the review has reached their quota limit.'
COPILOT_FAILED = 'Copilot encountered an error and was unable to review this pull request'
CODEX_MARKER = '<!-- codex-pull-request-review-summary -->'
CR_PAUSED = '<!-- This is an auto-generated comment: review paused by coderabbit.ai -->'
CR_RATE = '<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->'
GREPTILE_PAUSED = 'Greptile has paused reviews on this repository'
GREPTILE_ERROR = 'Greptile encountered an error while reviewing this PR'
GREPTILE_STATUS = '<!-- greptile-status -->'
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
    return hashlib.sha256((body or '').encode()).hexdigest()[:12]


def stamp(t):
    return datetime.fromisoformat(t.replace('Z', '+00:00')) if t else None


def record(bot, state, reason, evidence=(), sha=None, kind=None):
    return {'bot': bot, 'state': state, 'kind': kind, 'sha': sha, 'evidence': list(evidence), 'reason': reason}


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


def codex_row(body):
    """(the Code Review row's status cell, its Commit cell), or None when the table does not read that way."""
    lines = body.splitlines()
    header = next((i for i, l in enumerate(lines) if l.lstrip().startswith('|') and 'Commit' in l and 'Status' in l), None)
    row = next((l for l in lines if l.lstrip().startswith('|') and '📝' in l and 'Code Review' in l), None)
    if header is None or row is None:
        return None
    names = [c.strip() for c in lines[header].strip().strip('|').split('|')]
    cells = [c.strip() for c in row.strip().strip('|').split('|')]
    if len(cells) != len(names) or 'Status' not in names or 'Commit' not in names:
        return None
    return cells[names.index('Status')], cells[names.index('Commit')]


def codex(head, issue, reviews):
    """The newest of a review proving the head and the summary's Code Review row naming it."""
    found = []
    for r in reviews:
        proof = re.search(r'\*\*Reviewed commit:\*\*\s*`?([0-9a-f]{7,40})`?', r.get('body') or '')
        if 'codex' in (r['user']['login'] or '').lower() and r.get('commit_id') == head and proof and head.startswith(proof.group(1)):
            found.append((r['submitted_at'], record('codex', 'reviewed', 'a review proving the head', [f"review {r['id']} on {head[:8]}"], head)))
    sticky = [c for c in issue if CODEX_MARKER in (c.get('body') or '')]
    if sticky:
        c = max(sticky, key=lambda x: stamp(x['updated_at']))  # an undated one among several raises: unordered
        read = codex_row(c['body'])
        ev = [f"summary comment {c['id']} updated {c['updated_at']}"]
        sha = read and re.fullmatch(r'`?([0-9a-f]{7,40})`?', read[1])
        if read:
            ev.append(f'Code Review row: {read[0]} | {read[1]}')
        # An unreadable summary is itself the newest word when nothing proves the head after it.
        if not sha:
            found.append((c['updated_at'], record('codex', 'unknown', 'the summary has no Code Review row and commit this reads', ev)))
        elif head.startswith(sha.group(1)):
            status, why = read[0], f'Code Review row {read[0]} for this head'
            if '✅' in status and 'Completed' in status:
                rec = record('codex', 'reviewed', why, ev, head)
            elif '❌' in status and re.search(r'fail|error', status, re.I):
                rec = record('codex', 'settled', why, ev, head, 'failed')
            elif re.search(r'progress|running|queued|pending|⏳', status, re.I):
                rec = record('codex', 'working', why, ev, head)
            else:
                rec = record('codex', 'unknown', 'the Code Review row status is not one this reads', ev)
            found.append((c['updated_at'], rec))
    if not found:
        return record('codex', 'absent', 'no review proving the head and no summary row naming it')
    top = max(stamp(t) for t, _ in found)
    newest_ = [r for t, r in found if stamp(t) == top]
    if len({(r['state'], r['kind']) for r in newest_}) > 1:
        return record('codex', 'unknown', 'a review and the summary row disagree at one timestamp', [e for r in newest_ for e in r['evidence']])
    return newest_[0]


def copilot_outcome(review):
    body = review.get('body') or ''
    return 'limited' if body.startswith(COPILOT_LIMITED) else 'failed' if body.startswith(COPILOT_FAILED) else None


def copilot(head, reviews, requested):
    mine = [r for r in reviews if 'copilot' in (r['user']['login'] or '').lower()]
    on_head = [r for r in mine if r.get('commit_id') == head]
    older = [f"review {r['id']} on {(r.get('commit_id') or '')[:8]}" for r in mine if r.get('commit_id') != head]
    if on_head:
        undated = [r for r in on_head if not r.get('submitted_at')]
        if undated and len(on_head) > 1:
            return record('copilot', 'unknown', f"review {undated[0]['id']} on the head has no time to order it by",
                          [f"review {r['id']} on {head[:8]}" for r in on_head])
        r, tied = (undated[0], '') if undated else newest_reading(on_head, 'submitted_at', copilot_outcome)
        if tied:
            return record('copilot', 'unknown', f'reviews on the head {tied}', [f"review {x['id']} on {head[:8]}" for x in on_head])
        ev = [f"review {r['id']} on {head[:8]}", *older]
        kind = copilot_outcome(r)
        if kind == 'limited':
            return record('copilot', 'settled', 'quota limit on the head', ev, head, 'limited')
        if kind == 'failed':
            return record('copilot', 'settled', 'review failed on the head', ev, head, 'failed')
        return record('copilot', 'reviewed', 'a review on the head', ev, head)
    if any('copilot' in (x.get('login') or x.get('name') or '').lower() for x in requested):
        return record('copilot', 'queued', 'requested, no review on the head yet', older)
    return record('copilot', 'absent', 'no request outstanding and no review on the head', older)


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
    tied = next((c for c in [pause, *rate] if c and latest_at is not None and stamp(c['updated_at']) == latest_at), None)
    if tied:
        return record('coderabbit', 'unknown', 'a notice and a status or run on the head share one timestamp', [*ev, f"comment {tied['id']}"])
    if pause and (latest_at is None or stamp(pause['updated_at']) > latest_at):
        return record('coderabbit', 'settled', 'reviews paused on this PR', [*ev, f"pause in comment {pause['id']}"], None, 'paused')
    # A notice last touched before a status or run on the head is superseded, and an edited
    # one last touched before the head event is about an older push; any other edit hides when it was posted.
    rate = [c for c in rate if latest_at is None or stamp(c['updated_at']) > latest_at]
    edited = next((c for c in rate if c['created_at'] != c['updated_at'] and (event_at is None or stamp(c['updated_at']) > stamp(event_at))), None)
    if edited:
        return record('coderabbit', 'unknown', 'a rate-limit notice in an edited comment cannot be dated', [*ev, f"comment {edited['id']}"])
    if rate and event_at is None:
        return record('coderabbit', 'unknown', 'a rate-limit notice, and the head event time is unknown', [*ev, f"comment {rate[0]['id']}"])
    limited = newest([c for c in rate if c['created_at'] == c['updated_at'] and applies(c['created_at'], event_at)], 'created_at')
    if limited:
        return record('coderabbit', 'settled', 'rate-limited after the head event', [*ev, f"comment {limited['id']}"], None, 'limited')
    if s and run_ and s_at == run_at:
        a, b = coderabbit_status(s, ev, head), coderabbit_run(run_, ev, head)
        if (a['state'], a['kind']) != (b['state'], b['kind']):
            return record('coderabbit', 'unknown', 'its status and check run disagree at one timestamp', ev)
    if s and (run_ is None or s_at >= run_at):
        return coderabbit_status(s, ev, head)
    if run_:
        return coderabbit_run(run_, ev, head)
    return record('coderabbit', 'absent', 'no status and no check run on the head')


def coderabbit_status(s, ev, head):
    desc, state = s.get('description') or '', s['state']
    if state == 'pending':
        return record('coderabbit', 'working' if 'in progress' in desc.lower() else 'queued', desc or 'pending', ev, head)
    if state == 'success':
        if desc.lower().startswith('review skipped'):
            return record('coderabbit', 'settled', desc, ev, head, 'skipped')
        if desc.lower().startswith('review completed'):
            return record('coderabbit', 'reviewed', desc, ev, head)
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


def greptile(head, checks, issue, reviews, event_at):
    runs = [c for c in checks if (c.get('app') or {}).get('slug') == 'greptile-apps' and c.get('name') == 'Greptile Review']
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
    if r['status'] in RUN_PROGRESS:
        return record('greptile', *RUN_PROGRESS[r['status']], ev, head)
    if r['status'] == 'completed' and r.get('conclusion') == 'success' and summary.startswith('Greptile has reviewed the Pull Request.'):
        return record('greptile', 'reviewed', 'check run completed with a review', ev, head)
    if r['status'] == 'completed' and r.get('conclusion') == 'neutral' and summary.startswith(GREPTILE_ERROR + '.'):
        return record('greptile', 'settled', 'check run reports an error', ev, head, 'failed')
    return record('greptile', 'unknown', f"check run {r['status']}/{r.get('conclusion')} with a summary this does not read", ev)


def head_event(repo, n, view, head):
    """(the latest head event's time, its evidence), (None, why) when nothing dates it."""
    branch, head_repo = view['headRefName'], view['headRepository']['nameWithOwner']
    runs = sorted(pages(f'repos/{repo}/actions/runs?head_sha={head}', 'workflow_runs'), key=lambda x: stamp(x['created_at']), reverse=True)
    best = next((x for x in runs if dates_head(repo, x, branch, head_repo)), None)
    timeline = pages(f'repos/{repo}/issues/{n}/timeline')
    # A reopen or ready event keeps the head (GitHub leaves its commit_id null); one naming another commit is about an earlier push.
    tl = newest([e for e in timeline if e.get('event') in ('reopened', 'ready_for_review') and e.get('commit_id') in (None, head)], 'created_at')
    if tl and (best is None or stamp(tl['created_at']) > stamp(best['created_at'])):
        return tl['created_at'], f"timeline {tl['event']} at {tl['created_at']}"
    if best:
        return best['created_at'], f"run {best['id']} ({best['event']}, {best['path']}) created {best['created_at']}"
    return None, 'no push or head-activity pull_request run on this branch and head, and no reopen or ready event'


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
    """The pull_request activity types a workflow revision runs on, or None when they cannot be read."""
    try:
        meta = gh_json('api', f'repos/{repo}/contents/{path}?ref={sha}')
        doc = yaml.safe_load(base64.b64decode(meta['content']).decode())
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
    return {'kind': kind, 'commentId': c['id'], 'source': src, 'author': c['user']['login'], 'body': body or '', 'digest': digest(body),
            'path': path, 'line': line, 'commitId': commit, 'createdAt': created, 'updatedAt': updated, 'inReplyTo': reply_to, 'url': c['html_url']}


def comments(repo, n, reviewers):
    got = []
    for c in pages(f'repos/{repo}/pulls/{n}/comments'):
        src = source_of(c['user']['login'], reviewers)
        if src:
            got.append(entry('review', c, src, c.get('body'), c['created_at'], c['updated_at'], c.get('path'),
                             c.get('line') or c.get('original_line'), c.get('commit_id'), c.get('in_reply_to_id')))
    for c in pages(f'repos/{repo}/issues/{n}/comments'):
        src = source_of(c['user']['login'], reviewers)
        if src:
            got.append(entry('issue', c, src, c.get('body'), c['created_at'], c['updated_at']))
    for r in pages(f'repos/{repo}/pulls/{n}/reviews'):
        src = source_of(r['user']['login'], reviewers)
        if src and (r.get('body') or '').strip():
            got.append(entry('review-body', r, src, r['body'], r.get('submitted_at'), r.get('submitted_at'), commit=r.get('commit_id')))
    return got


def observe(repo, n, reviewers, auto):
    view = gh_json('pr', 'view', str(n), '--json', 'headRefOid,headRefName,headRepository,reviewRequests')
    head = view['headRefOid']
    if not FULL_SHA.match(head or ''):
        raise Unusable(f'gh pr view {n}: no head SHA')
    event_at, event_why = head_event(repo, n, view, head) if auto else (None, 'no reviewer auto-runs, so no head event is read')
    statuses = pages(f'repos/{repo}/commits/{head}/statuses') if 'coderabbit' in auto else []
    checks = pages(f'repos/{repo}/commits/{head}/check-runs', 'check_runs') if {'coderabbit', 'greptile'} & set(auto) else []
    issue = pages(f'repos/{repo}/issues/{n}/comments') if {'codex', 'coderabbit', 'greptile'} & set(auto) else []
    reviews = pages(f'repos/{repo}/pulls/{n}/reviews') if {'codex', 'copilot', 'greptile'} & set(auto) else []
    bots = []
    for bot in auto:
        if bot == 'codex':
            bots.append(codex(head, issue, reviews))
        elif bot == 'copilot':
            bots.append(copilot(head, reviews, view.get('reviewRequests') or []))
        elif bot == 'coderabbit':
            bots.append(coderabbit(head, statuses, checks, issue, event_at))
        elif bot == 'greptile':
            bots.append(greptile(head, checks, issue, reviews, event_at))
    harvested = comments(repo, n, reviewers)
    return {'headSha': head, 'observedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
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
    sys.exit(report(collect, sys.argv[1:]))
