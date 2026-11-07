#!/usr/bin/env python3
"""pr-review's per-PR ledger: what each review of a PR found, drafted and posted.

  ledger.py show --pr N [--repo OWNER/NAME] [--finding ID | --draft | --pending | --threads FILE [--head SHA [--view]]]
  ledger.py show --pr N --fixable --threads FILE --head SHA [--repo OWNER/NAME]
  ledger.py save --pr N --output FILE [--repo OWNER/NAME] [--reason TEXT]
  ledger.py next --pr N --head SHA --publish pending|auto-post|auto-fix [--repo OWNER/NAME] [--token T]
                 [--review-output FILE] [--manifest FILE] [--babysit-reading FILE]

The ledger is <git common dir>/agentrc/pr-review/<owner>__<name>/<N>.json,
one per PR, shared by every worktree of the clone. Only this script and
post.py write it, under an exclusive lock, by write-then-rename.

show prints the last review that reached the PR (posted:
head, mergeBase, verdict, status), its standing findings (open, upheld,
disputed: id, file, line, severity with its P0-P4 priority, the grading
behind it, its claim and its comment as the human edited it (published, carried to a comment raising it again), the
number of the defect it shares with other findings of that review, if any, and
the repair note post.py --fixed put on its thread (repair: head, state,
resolved), or null), ordered by
severity, and the thread answers
drafted and not yet published, each
with the replies it answers (author,
excerpt, from the threads snapshot it was judged on, null once edited), the
recheck's reason and
the thread's link; --finding prints one finding's whole
record from those reviews; --draft prints the newest review's saved draft as it
would be posted (event, body, anchored inline comments, fix notes), with its
status and digest; --pending prints the newest review saved and not yet submitted
or confirmed (pending, drafted or uncertain: head, status, event, digest) with all its
findings as show lists them, or null, since show itself lists only reviews that
reached the PR. With --threads, a threads.py snapshot, it also lists each
settled finding whose thread is still open and due to be resolved (resolveDue,
resolve_due()), and in heldThreads those it never resolves again by itself;
it marks inBody each finding the snapshot shows as a line of our review body,
unedited, so a later review states it again only once that line is gone;
--head adds the disputes on that head.

--view prints instead the pr-review workflow's view of that answer, bound to the repository, PR and head it was
read for (repo, pr, head): it leaves out what the workflow never reads, the ledger path, the unpublished answers and
each open row's impact and severityReason, which save keeps from the ledger when a result does not regrade the
finding. The workflow runs it under AGENTRC_EMIT and loads the line from that file (lib/seal.py's emit), so no
model copies it.

--fixable, with a fresh threads.py snapshot, prints what an auto-fix repair may take from the review posted on
SHA, which must be the last review on the PR: each standing finding as `fix` (its row, as show lists it, with
`published`, its comment text as the human edited it, or null, and `claimDigest`, the digest of that text or its own
words, which a repair's fix check names) when the text it stands in on the PR is the one a receipt of ours recorded,
unedited, and nobody but us or a bot has replied on its thread since our last verified answer; else in `held` with
why: `disputed`, `unmapped` (no receipt places its text), `grouped` (unmapped and `muted`: the review left it out
because its group's printed text states its defect, and that text still stands: a claim of that review whose comment
the snapshot still lists, or the group's `lead` finding, not muted and itself in `fix`), `deleted`, `edited` (the
inline comment, or the body of the review it was moved into, reads otherwise) or `replied`. `autoPost` is false when
any is held `unmapped`, `deleted` or `edited`: a recheck would judge text the ledger no longer holds, so its review
goes to the human as a pending one. A `grouped` one has no text of its own on the PR to diverge, so it does not; a
muted one whose group's text the human deleted, edited, dropped or contested, or one in a review saved before `muted`
existed, is held `unmapped`, and one in a review saved before `lead` existed is grouped only through a listed claim.

The disputes join the threads.py snapshot to the findings whose inline comment our
own verified review posted (commentId, recorded by post.py from its read-back):
on each such thread, the replies by anyone but us or a bot after our last
verified answer there are pushback to judge (pushback()). A dispute's key is the ordered reply ids and body digests; a key
already judged on this head is not listed again, and an edited reply is a new
key. Only ids, digests and authors are printed, never bodies.

A `discussion` result (same head, pushback only) posts no new review: save
merges its dispute records and statuses into the findings of the last review
of that head, and appends to that review an answer publication (its own pending
review, no findings) that post.py publishes the new answers through. save reads a pr-review Workflow output file ({"result": ...}),
checks it is a result for this PR, and appends it as a review whose draft is
`pending`: each comment anchored on a line the PR diff adds or keeps, else
moved into the body, and the draft's digest fixed before anything is posted.
A pending draft already on the ledger for that head is refused: post it
or say why a second review is due (--reason). A review records the output file it was saved from (`output`), and a
discussion's in its review's `discussions` and on its answer publication, the launch next asks after.

next prints the run's next step after a prepare on --head (its facts file names the mode, recheckFrom and repair):
{step, argv, postMode, why}, step one of review, post, repair, babysit, fixed, publish-answers, recheck, report or
blocked; argv the exact command when the step is one (report's is the lock release), postMode auto, auto-defer or
pending for a step that posts. It reads the ledger, the PR's live head and state, and the files the run has so far:
--review-output (this phase's pr-review launch), --manifest (the auto-fix repair's --fixed manifest) and
--babysit-reading (launch_result.py's JSON line for pr-babysit's latest launch). A state the recipe does not settle is
`blocked`, with why, never a guess. next_step() is the transition, pure, so one table test reads it whole.

stdout ends with one JSON line; {"error": ...} and exit 2 when the ledger or
the output cannot be read, or they disagree.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import FULL_SHA, Parser, Unusable, git, locked, report  # noqa: E402
from launch_result import ENDED, STALLED, load_output  # noqa: E402
from harvest import digest as body_digest, gh_json  # noqa: E402  the digest threads.py records
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import gh_repo  # noqa: E402
import windows  # noqa: E402

VERSION = 2
CUT = 160
OPEN = ('open', 'upheld', 'disputed')
# The one scale (finding-verifier.md's Severity section); P0-P4 are its report aliases.
LEVELS = ('critical', 'high', 'medium', 'low', 'nit')
CONFIDENCE = ('high', 'medium', 'low')


def level(severity):
    if severity is None:
        return None
    v = str(severity).lower()
    if v not in LEVELS:
        raise Unusable(f'severity {severity!r} is on no known scale')
    return v


def priority(severity):
    return f'P{LEVELS.index(severity)}' if severity in LEVELS else None


def rank(scale, v):
    return scale.index(v) if v in scale else len(scale)


def by_severity(f):
    """Report order: severity, then confidence, then place; a finding's id never moves with it."""
    return (rank(LEVELS, f.get('severity')), rank(CONFIDENCE, f.get('confidence')), f.get('file') or '', f.get('line') or 0)


def refuse_off_scale(result):
    """A new result stores the scale's own words; only a carried record may still lack a level."""
    for f in result['findings']:
        s = f.get('severity')
        if s not in LEVELS and not (s is None and f.get('id')):
            name = f.get('id') or f"{f.get('file')}:{f.get('line')}"
            raise Unusable(f'finding {name} has severity {s!r}, not a level of the one scale')
    for c in result.get('claims') or []:
        if c.get('verdict') == 'confirmed' and c.get('severity') not in LEVELS:
            raise Unusable(f"claim {c.get('commentId')} has severity {c.get('severity')!r}, not a level of the one scale")


def repo_of(repo):
    if repo:
        if not gh_repo.REPO.match(repo):
            raise Unusable(f'--repo must be OWNER/NAME, not {repo!r}')
        return repo
    try:
        return gh_repo.checkout_repo()
    except gh_repo.Unresolved as e:
        raise Unusable(f'{e}: name the repository (--repo OWNER/NAME)') from None


def ledger_dir(repo):
    common = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
    return common / 'agentrc' / 'pr-review' / repo.replace('/', '__')


def ledger_path(repo, pr):
    return ledger_dir(repo) / f'{pr}.json'


def facts_path(repo, pr, head):
    return ledger_dir(repo) / str(pr) / f'prepare-{head}.json'


def load(path, repo, pr):
    if not path.exists():
        return {'v': VERSION, 'repo': repo, 'pr': pr, 'reviews': []}
    try:
        led = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise Unusable(f'ledger {path} unreadable: {e}')
    if not isinstance(led, dict) or led.get('v') != VERSION:
        raise Unusable(f'ledger {path} is version {led.get("v") if isinstance(led, dict) else "?"}, this script reads {VERSION}')
    if led.get('repo') != repo or led.get('pr') != pr:
        raise Unusable(f'ledger {path} is for {led.get("repo")}#{led.get("pr")}, not {repo}#{pr}')
    return led


def store(path, led):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(led, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    windows.replace(tmp, path)  # Windows: retried while a reader without the lock has the ledger open


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]


# An uncertain review may not be on the PR at all: it is reconciled before anything builds on it.
REACHED = ('posted',)
UNSETTLED = ('pending', 'drafted', 'uncertain')
# A drafted review is a pending review of ours on GitHub; an uncertain one may be.
ONLINE = ('drafted', 'uncertain')
# Not yet on the PR, or not known to be: a second draft for the head waits for it.
DRAFTS = ('pending', *ONLINE)


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def reached(led):
    """The reviews whose POST was verified on the PR: only their findings are the contributor's to answer."""
    return [r for r in led['reviews'] if r['status'] in REACHED]


def publications(led):
    """(publication, the review whose findings it carries): each review, then each answer publication on it, a
    pending review of its own that publishes only answers to that review's findings."""
    for r in led['reviews']:
        yield r, r
        for a in r.get('answerPublications', []):
            yield a, r


def owner_of(led, pub):
    """The review whose findings a publication carries."""
    return next(r for p, r in publications(led) if p is pub)


def answers_of(led, pub):
    """(finding, dispute) for each unpublished answer a publication carries, judged on its head: a review's, those of
    its own findings; an answer publication's, the ones it was made for, once its review is on the PR."""
    owner = owner_of(led, pub)
    got = open_disputes(owner['findings'], pub['head'])
    if pub is owner:
        return got
    if owner['status'] not in REACHED:
        raise Unusable(f"the answers for {pub['head']} belong to a review that is not on the PR yet: settle it first")
    return [(f, d) for f, d in got if [f['id'], d['answer']['digest']] in pub['answers']]


def refuse_unsettled(led, head, statuses=UNSETTLED):
    got = [p for p, _ in publications(led) if p['head'] == head and p['status'] in statuses]
    if got:
        raise Unusable(f"a {got[-1]['status']} draft {got[-1]['draft']['digest']} for {head} is on the ledger: "
                       'post or reconcile it first')


def last(led):
    """The last review on the PR: its findings are the ones standing."""
    return (reached(led) or [None])[-1]


def cut(text):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= CUT else text[:CUT] + '…'


def thread_context(d, root, snapshots):
    """What an answer replies to, from the threads snapshot it was judged on: authors, excerpts (null for a reply
    gone or edited since) and the thread's link. `snapshots` caches each file's comments by path."""
    path = d.get('threadsFile')
    if path not in snapshots:
        try:
            snapshots[path] = {c['id']: c for c in json.loads(Path(path).read_text(encoding='utf-8')).get('comments', [])}
        except (OSError, TypeError, ValueError):
            snapshots[path] = None
    by_id = snapshots[path]
    if by_id is None:
        return {'replies': None, 'url': None}
    same = lambda r: r['id'] in by_id and by_id[r['id']]['digest'] == r['digest']  # noqa: E731
    return {'replies': [{'author': r.get('author'), 'excerpt': cut(by_id[r['id']]['body']) if same(r) else None}
                        for r in d.get('replies', [])],
            'url': (by_id.get(root) or {}).get('url')}


def thread_roots(snapshot):
    """{root comment id: thread} of a threads.py snapshot."""
    return {t['commentIds'][0]: t for t in snapshot.get('threads', []) if t.get('commentIds')}


def viewer_of(snapshot):
    viewer = snapshot.get('viewer')
    if not viewer:
        raise Unusable('the threads file names no viewer: rerun threads.py')
    return viewer


def resolve_due(led, snapshot):
    """({finding id: {replied}}, held) for the last review's settled findings whose thread the snapshot shows open.
    Due: fixed, or closed by a reply of ours since the finding last stood (replied: a published reply that resolves,
    or a fix note the human edited, still in the thread). Held, never resolved again by itself: one whose last resolve is unconfirmed
    (sent), that someone reopened after it was resolved, or whose repair note was deleted or edited before its resolve."""
    rev = last(led)
    if not rev:
        return {}, []
    roots = thread_roots(snapshot)
    replied, mark = {}, {}
    for r in reached(led):
        for p in (r, *r.get('answerPublications', [])):
            rec = p.get('receipts') or {}
            for e in rec.get('staged') or []:
                live = e.get('replyId') in (roots.get(e['commentId']) or {}).get('commentIds', [])
                if live and ((e['state'] == 'published' and e['resolve']) or (e['kind'] == 'fixnote' and e['state'] == 'edited')):
                    replied[e['findingId']] = True
            for m in rec.get('resolved') or []:
                mark[m['findingId']] = m['state']
        for f in r.get('findings', []):
            if f['status'] in OPEN:
                # Standing again: what closed it earlier no longer does.
                replied.pop(f['id'], None)
                mark.pop(f['id'], None)
    due, held = {}, []
    fixes = repairs(led)
    bodies = {c['id']: c['body'] for c in snapshot.get('comments') or [] if c['kind'] == 'review'}
    for f in rev.get('findings', []):
        t = roots.get(f.get('commentId'))
        fix = fixes.get((f['id'], f.get('commentId')))
        if fix and t and fix['state'] == 'published' and fix['replyId'] in t['commentIds']:
            replied[f['id']] = True
        why = fix and t and repair_hold(fix, t)
        if why:
            held.append({'findingId': f['id'], 'commentId': f['commentId'], 'why': why})
            continue
        if f['status'] in OPEN or not t or t['resolved']:
            continue
        why = fix and note_hold(fix, bodies)
        if why:
            held.append({'findingId': f['id'], 'commentId': f['commentId'], 'why': why})
        elif mark.get(f['id']) in ('sent', 'resolved', 'observed'):
            held.append({'findingId': f['id'], 'commentId': f['commentId'],
                         'why': 'resolve unconfirmed' if mark[f['id']] == 'sent' else 'reopened after it was resolved'})
        elif replied.get(f['id']) or f['status'] == 'fixed':
            due[f['id']] = {'replied': bool(replied.get(f['id']))}
    return due, held


def open_row(f, due, fixes=None):
    sev = level(f.get('severity'))
    fix = (fixes or {}).get((f['id'], f.get('commentId')))
    return {'id': f['id'], 'status': f['status'], 'file': f['file'], 'line': f['line'],
            'severity': sev, 'priority': priority(sev), 'confidence': f.get('confidence'),
            'impact': f.get('impact'), 'severityReason': f.get('severityReason'),
            'why': f['why'], 'published': f.get('published'), 'commentId': f.get('commentId'), 'defect': f.get('defect'),
            'resolveDue': due.get(f['id']),
            'repair': fix and {'head': fix['head'], 'state': fix['state'], 'resolved': fix['resolve'] in SETTLED}}


def show(led, finding=None, draft=False, snapshot=None, pending=False):
    if pending:
        rev = next((r for r in reversed(led['reviews']) if r['status'] in DRAFTS), None)
        return {'pending': rev and {'head': rev['head'], 'status': rev['status'], 'event': rev['draft'].get('event'),
                                    'digest': rev['draft'].get('digest'),
                                    'findings': sorted((open_row(f, {}) for f in rev['findings']), key=by_severity)}}
    if draft:
        if not led['reviews']:
            raise Unusable('no review on the ledger')
        rev = led['reviews'][-1]
        # Each comment names its finding's severity, which the comment's heading must match.
        sev = {f['id']: f.get('severity') for f in rev['findings']}
        draft = {**rev['draft'], 'comments': [{**c, 'severity': sev.get(c.get('findingId'))} for c in rev['draft']['comments']]}
        return {'head': rev['head'], 'status': rev['status'], 'draft': draft}
    rev = last(led)
    if finding:
        for r in reversed(reached(led)):
            for f in r.get('findings', []):
                if f['id'] == finding:
                    return {'finding': {**f, 'severity': level(f.get('severity'))}, 'reviewHead': r['head']}
        raise Unusable(f'no finding {finding} on the ledger')
    if not rev:
        return {'reviews': 0, 'last': None, 'open': []}
    due, held = resolve_due(led, snapshot) if snapshot else ({}, [])
    fixes = repairs(led)
    shown = body_shown(led, snapshot) if snapshot else (lambda f: False)
    snapshots = {}
    answers = [{'findingId': f['id'], 'commentId': answer_root(f, d), 'state': d['state'], 'resolve': d['answer']['resolve'],
                'body': d['answer']['body'], 'reason': d.get('reason'),
                **thread_context(d, answer_root(f, d), snapshots)}
               for f, d in open_disputes(rev.get('findings', []), rev['head'])]
    return {
        'reviews': len(led['reviews']),
        'last': {k: rev.get(k) for k in ('head', 'mergeBase', 'mode', 'status', 'reviewedAt')} | {'event': rev['verdict']['event']},
        'answers': answers,
        'open': sorted(({**open_row(f, due, fixes), **({'inBody': True} if shown(f) else {})}
                        for f in rev.get('findings', []) if f['status'] in OPEN or f['id'] in due), key=by_severity),
        **({'heldThreads': held} if snapshot else {}),
    }


def added_lines(merge_base, head, path):
    """Right-side lines a review comment can anchor on: those the PR diff adds or shows as context."""
    diff = git('diff', '--unified=3', merge_base, head, '--', path)
    lines, n = set(), None
    for row in diff.splitlines():
        m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', row)
        if m:
            n = int(m.group(1))
        elif n is not None and not row.startswith(('-', '\\')):
            lines.add(n)
            n += 1
    return lines


def anchor(draft, merge_base, head):
    """Comments on lines the diff does not show go into the body, which GitHub refuses inline, one line each: the
    place and the comment's first line, its one-sentence problem. The whole comment stays in `moved`."""
    inline, moved, cache = [], [], {}
    for c in draft['comments']:
        if c['path'] not in cache:
            cache[c['path']] = added_lines(merge_base, head, c['path'])
        if c['line'] in cache[c['path']]:
            inline.append(c)
        else:
            moved.append({**c, 'renderedLine': f"- `{c['path']}:{c['line']}` {c['body'].split(chr(10))[0]}"})
    body = draft['body']
    if moved:
        body = '\n\n'.join(x for x in (body, 'Not in an inline comment:\n' + '\n'.join(c['renderedLine'] for c in moved)) if x)
    return {**draft, 'body': body, 'comments': inline, 'moved': moved}


def number(led, result):
    """Carried findings keep their id and earlier record, updated; new ones take the next pr<N>-f<k>."""
    prior = {f['id']: f for r in reached(led) for f in r.get('findings', [])}
    # Numbering counts every review, so an id is never reused.
    k = max((int(f['id'].rsplit('-f', 1)[1]) for r in led['reviews'] for f in r.get('findings', [])), default=0)
    out = []
    for f in result['findings']:
        if f.get('id'):
            if f['id'] not in prior:
                raise Unusable(f"the output carries {f['id']}, which is not on the ledger")
            f = hold_withdrawal({**prior[f['id']], **f, 'disputes': prior[f['id']].get('disputes', []) + f.get('disputes', [])},
                                prior[f['id']]['status'])
        else:
            k += 1
            f = {**f, 'id': f"pr{led['pr']}-f{k}"}
        out.append(f)
    return out


def answered_ids(led):
    """Our replies a receipt records as published, the human's edits of them included: the only comments of ours
    that settle pushback."""
    ids = set()
    for r, _ in publications(led):
        rec = r.get('receipts') or {}
        ids |= {x.get('replyId') for x in rec.get('staged') or [] if x.get('state') in ('published', 'edited')}
    # A repair note settles pushback only once its resolve saw none: one deferred by a reply must not hide that reply.
    ids |= {x['replyId'] for x in repairs(led).values() if x['state'] == 'published' and x['resolve'] in SETTLED}
    ids.discard(None)
    return ids


SETTLED = ('resolved', 'observed')


def repair_hold(fix, t):
    """Why a repair record keeps its thread (`t`, as a snapshot shows it) from any further resolve, or None: its note
    or resolve unconfirmed, or the thread reopened after it was resolved."""
    return ('repair note unconfirmed' if fix['state'] in ('sent', 'uncertain') else
            'resolve unconfirmed' if fix['resolve'] == 'sent' else
            'reopened after it was resolved' if fix['resolve'] in SETTLED and t and not t['resolved'] else None)


def note_hold(fix, bodies):
    """Why a published repair note whose resolve never settled keeps its thread from any resolve, or None: the note
    deleted or edited since (`bodies`, the live review comments' text by id), so it no longer says what was verified."""
    if fix['state'] != 'published' or fix['resolve'] in SETTLED:
        return None
    body = bodies.get(fix['replyId'])
    return 'our repair note was deleted' if body is None else 'our repair note was edited' if digest(body) != fix['digest'] else None


def submitted_last(led, head):
    """The last review on the PR, which must be the one submitted on `head`."""
    rev = last(led)
    if not rev or rev['head'] != head or not ((rev.get('receipts') or {}).get('review') or {}).get('submitted'):
        raise Unusable(f"the last review on the PR is {rev and rev['head']} ({rev and rev['status']}), not one submitted on {head}")
    return rev


def repairs(led):
    """{(finding id, root comment id): the latest repair note record}: post.py --fixed's, across our reviews."""
    return {(x['findingId'], x['commentId']): x for r in reached(led) for x in r.get('repairs', [])}


def claim(f):
    """The text a repair is judged against: the comment as the human edited it, else the finding's own words."""
    return f.get('published') or f['why']


def pushback(replies, answered, viewer):
    """threads.py records replying to a root, after our last verified answer (an id in `answered`), neither ours
    nor a bot's: the pushback still to answer."""
    last = max((k for k, c in enumerate(replies) if c['id'] in answered), default=-1)
    return [c for c in replies[last + 1:] if c['author'] != viewer and not c['bot']]


def disputes(led, snapshot, head):
    rev = last(led)
    if not rev:
        return []
    viewer = viewer_of(snapshot)
    by_id = {c['id']: c for c in snapshot.get('comments', [])}
    known = answered_ids(led)
    roots = thread_roots(snapshot)
    out = []
    for f in rev.get('findings', []):
        t = roots.get(f.get('commentId'))
        if f['status'] not in OPEN or not t:
            continue
        thread = [by_id[i] for i in t['commentIds'] if i in by_id]
        replies = pushback(thread[1:], known, viewer)
        if not replies:
            continue
        key = digest([[c['id'], c['digest']] for c in replies])
        if any(d['key'] == key and d.get('judgedHead') == head for d in f.get('disputes', [])):
            continue
        out.append({'findingId': f['id'], 'threadId': t['threadId'], 'rootCommentId': f['commentId'], 'key': key,
                    'outdated': t['outdated'], 'resolved': t['resolved'],
                    'replies': [{'id': c['id'], 'digest': c['digest'], 'author': c['author']} for c in replies]})
    return out


def published_at(led, f):
    """(kind, id, digest) of where f's text stands on the PR by our receipts: its inline root comment, or the body of
    the review that moved it there; None when no receipt places it."""
    from post import body_of  # post imports this module
    for r in reversed(reached(led)):
        draft, got = r.get('draft') or {}, (r.get('receipts') or {}).get('review') or {}
        for c, cid in zip(draft.get('comments', []), got.get('commentIds') or []):
            if c.get('findingId') == f['id'] and cid is not None and cid == f.get('commentId'):
                return 'review', cid, body_digest(f.get('published') or c['body'])
        if f.get('commentId') is None and any(c.get('findingId') == f['id'] for c in draft.get('moved', [])):
            return ('review-body', got['reviewId'], body_digest(body_of(r))) if got.get('reviewId') else None
    return None


def body_shown(led, snapshot):
    """shown(f): whether f is a line of a review body of ours that the snapshot shows as we posted it."""
    bodies = {c['id']: c['digest'] for c in snapshot.get('comments', []) if c.get('kind') == 'review-body'}

    def shown(f):
        at = f.get('commentId') is None and published_at(led, f)
        return bool(at and at[0] == 'review-body' and bodies.get(at[1]) == at[2])
    return shown


def holder(led, snapshot):
    """hold(f): why a standing finding is not ours to repair as the snapshot shows the PR, or None."""
    viewer = viewer_of(snapshot)
    live = {(c['kind'], c['id']): c for c in snapshot.get('comments', [])}
    roots = thread_roots(snapshot)
    known = answered_ids(led)

    def hold(f):
        at = published_at(led, f)
        if at is None:
            return 'unmapped'
        kind, cid, want = at
        c = live.get((kind, cid))
        if c is None:
            return 'deleted'
        if c['digest'] != want:
            return 'edited'
        if f['status'] == 'disputed':
            return 'disputed'
        if kind == 'review-body':
            return None
        # A root without its thread, or a thread naming a comment the snapshot lacks, could hide pushback.
        t = roots.get(cid)
        if t is None or any(('review', i) not in live for i in t['commentIds']):
            raise Unusable(f"the threads file does not hold {f['id']}'s whole thread: rerun threads.py")
        return 'replied' if pushback([live[('review', i)] for i in t['commentIds'][1:]], known, viewer) else None
    return hold


def fixable(led, snapshot, head):
    online = [x for x, _ in publications(led) if x['status'] in ONLINE]
    if online:
        raise Unusable(f"a {online[-1]['status']} publication for {online[-1]['head']} is not settled, so the last review on the PR "
                       'is unknown: reconcile it first')
    rev = submitted_last(led, head)
    hold = holder(led, snapshot)
    standing = [(f, hold(f)) for f in rev.get('findings', []) if f['status'] in OPEN]
    # The defects whose group's printed text still stands: a claim whose comment is on the PR, or the lead's own text
    # as we posted it, uncontested (a recheck may withdraw a disputed or replied lead and judge its mate without it);
    # a mate posted beside its group states only itself.
    on_pr = {c['id'] for c in snapshot.get('comments', [])}
    stated = ({c.get('defect') for c in rev.get('claims') or [] if c.get('commentId') in on_pr}
              | {f.get('defect') for f, why in standing
                 if f.get('lead') and not f.get('muted') and why is None}) - {None}
    fix, held = [], []
    for f, why in standing:
        if why == 'unmapped' and f.get('muted') and f.get('defect') in stated:
            why = 'grouped'
        if why:
            held.append({'findingId': f['id'], 'commentId': f.get('commentId'), 'why': why})
        else:
            # The text the PR shows, as the human edited it, and the digest a repair's fix check names.
            fix.append({**open_row(f, {}), 'claimDigest': digest(claim(f))})
    return {'fix': sorted(fix, key=by_severity), 'held': held,
            'autoPost': not any(h['why'] in ('unmapped', 'deleted', 'edited') for h in held)}


def add_answers(review, head, answers, auto=False, output=None):
    """Append to `review` the publication of `answers` ((finding id, answer digest) pairs) to its findings;
    `auto` marks the one an auto-post run keeps with its send intent, `output` the discussion it came from."""
    pubs = review.setdefault('answerPublications', [])
    draft = {'event': None, 'body': '', 'comments': [], 'replies': []}
    draft['digest'] = digest({'head': head, 'answers': answers, 'n': len(pubs)})
    pubs.append({'head': head, 'answers': [list(a) for a in answers], 'status': 'pending',
                 'draft': draft, 'receipts': {}, 'reviewedAt': now(), **({'auto': True} if auto else {}),
                 **({'output': output} if output else {})})


def open_disputes(findings, head=None):
    """(finding, dispute) for each finding whose last dispute holds an answer not yet published or settled, judged
    on `head` when named: an answer to an earlier head is never published on a later one."""
    return [(f, d) for f in findings for d in f.get('disputes', [])[-1:]
            if d.get('answer') and not d['answer'].get('outcome') and head in (None, d.get('judgedHead'))]


def answer_root(f, d):
    """The comment an answer replies to: the thread it was judged on, which a comment raising its finding again
    replaces as the finding's commentId."""
    return d.get('rootCommentId', f.get('commentId'))


def hold_withdrawal(f, standing):
    """A concession still to publish leaves its finding as it stood: the answer carries the withdrawal, which
    post.py applies once the PR shows the concession as drafted."""
    d = (f.get('disputes') or [None])[-1]
    if f['status'] == 'withdrawn' and d and d.get('answer') and not d['answer'].get('outcome'):
        d['answer']['status'] = 'withdrawn'
        f['status'] = standing
    return f


def open_answers(findings):
    return [(f['id'], d['answer']['digest']) for f, d in open_disputes(findings)]


def merge_discussion(led, result, output=None):
    rev = last(led)
    if not rev or rev['head'] != result['head']:
        raise Unusable(f"a discussion result is for the last reviewed head, and {result['head']} is not it")
    refuse_unsettled(led, result['head'], DRAFTS)
    by_id = {f['id']: f for f in rev['findings']}
    for f in result['findings']:
        old = by_id.get(f.get('id'))
        if old is None:
            raise Unusable(f"the discussion result names {f.get('id')}, not a finding of the last review")
        standing = old['status']
        old.update({k: v for k, v in f.items() if k != 'disputes'})
        old['disputes'] = old.get('disputes', []) + f.get('disputes', [])
        hold_withdrawal(old, standing)
    answers = open_answers(by_id[f['id']] for f in result['findings'])
    if answers:
        add_answers(rev, result['head'], answers, output=output)
    if output:
        rev.setdefault('discussions', []).append(output)
    return {'saved': True, 'head': result['head'], 'mode': 'discussion', 'findings': len(result['findings']),
            'answers': len(answers)}


def with_answer_digests(result):
    """Each drafted thread answer gets its digest before anything can post it."""
    for f in result['findings']:
        for d in f.get('disputes', []):
            if d.get('answer'):
                d['answer'] = {**d['answer'], 'digest': digest(d['answer']['body'])}
    return result


def save(led, result, reason, output=None):
    for key in ('pr', 'head', 'mergeBase', 'verdict', 'findings', 'draft'):
        if key not in result:
            raise Unusable(f'the output result has no {key}: not a pr-review result')
    if result['pr'] != led['pr']:
        raise Unusable(f"the output reviews PR {result['pr']}, the ledger is for {led['pr']}")
    refuse_off_scale(result)
    result = with_answer_digests(result)
    if result.get('mode') == 'discussion':
        return merge_discussion(led, result, output)
    for key in ('head', 'mergeBase'):
        if not FULL_SHA.match(str(result[key])):
            raise Unusable(f'the output {key} is not a full SHA')
    if result['verdict'].get('event') != result['draft'].get('event'):
        raise Unusable(f"the draft's event {result['draft'].get('event')} is not the verdict's {result['verdict'].get('event')}")
    refuse_unsettled(led, result['head'])
    same = [r for r in led['reviews'] if r['head'] == result['head'] and r['status'] == 'posted']
    if same and not reason:
        raise Unusable(f"{result['head']} was already reviewed and posted; a second review needs --reason")
    # On Windows a backslash is a separator and `\x`, `\\srv\x` and `C:x` are absolute too; elsewhere they are names.
    windows = os.name == 'nt'
    if any(p.startswith('/') or windows and (re.match(r'[A-Za-z]:', p) or '\\' in p)
           for p in (str(c.get('path', '')) for c in result['draft']['comments'])):
        raise Unusable('a draft comment has an absolute path or a backslash; GitHub needs repository-relative paths with /')
    findings = number(led, result)
    try:
        comments = [{'path': c['path'], 'line': c['line'], 'body': c['body'], 'findingId': findings[c['finding']]['id'],
                     **({'firstLineUnchecked': True} if c.get('firstLineUnchecked') else {})}
                    for c in result['draft']['comments']]
    except (KeyError, IndexError, TypeError):
        raise Unusable('a draft comment names no finding of the result')
    draft = anchor({**result['draft'], 'comments': comments}, result['mergeBase'], result['head'])
    draft['digest'] = digest({k: draft[k] for k in ('event', 'body', 'comments', 'replies')})
    review = {**result, 'findings': findings, 'draft': draft, 'status': 'pending', 'receipts': {},
              'reason': reason, 'reviewedAt': now(), **({'output': output} if output else {})}
    led['reviews'].append(review)
    return {'saved': True, 'head': result['head'], 'draftDigest': draft['digest'], 'event': draft['event'],
            'inline': len(draft['comments']), 'moved': len(result['draft']['comments']) - len(draft['comments']),
            'replies': len(draft['replies']), 'findings': len(review['findings'])}


# What the workflow never reads of show's answer; a carried row's impact and severityReason it would only echo back.
UNREAD = ('ledger', 'answers')
UNREAD_ROW = ('impact', 'severityReason')


def workflow_view(answer, repo, pr, head):
    view = {k: v for k, v in answer.items() if k not in UNREAD}
    view['open'] = [{k: v for k, v in f.items() if k not in UNREAD_ROW} for f in answer['open']]
    return {'repo': repo, 'pr': pr, 'head': head, **view}


def read_snapshot(path, repo, pr):
    try:
        snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise Unusable(f'--threads {path} unreadable: {e}')
    # PR numbers repeat across repositories; a snapshot naming neither is threads.py's to write again.
    if (snapshot.get('repo'), snapshot.get('pr')) != (repo, pr):
        raise Unusable(f"--threads is a snapshot of {snapshot.get('repo')}#{snapshot.get('pr')}, not {repo}#{pr}")
    return snapshot


HERE = Path(__file__).resolve().parent
# The post.py flags of each publishing choice; a discussion run and an answer bundle always take the plain one.
POST_MODE = {'pending': 'pending', 'auto-post': 'auto', 'auto-fix': 'auto-defer'}
POST_FLAGS = {'pending': (), 'auto': ('--auto',), 'auto-defer': ('--auto', '--defer-answers')}
PUBLISHED = ('pushed', 'already-published')


def step(name, why, argv=None, post=None):
    return {'step': name, 'argv': argv, 'postMode': post, 'why': why}


def post_outcome(pub):
    """What post.py left of a publication: unposted (never tried), failed, uncertain, handed over, drafted, posted or
    declined."""
    got = (pub.get('receipts') or {}).get('review') or {}
    if pub['status'] == 'pending':
        return 'failed' if got else 'unposted'
    return 'handed over' if got.get('handedOver') else pub['status']


def deferred_bundle(rev):
    """The answers post.py --auto --defer-answers kept unsent on our review, or None."""
    return next((a for a in rev.get('answerPublications', []) if a.get('auto') and a['status'] == 'pending'
                 and a['head'] == rev['head'] and not (a.get('receipts') or {}).get('review')), None)


def bundle_sent(rev):
    """Whether this run published the deferred answers: prepare refuses while a publication of ours is online."""
    return any(a.get('auto') and a['head'] == rev['head'] and post_outcome(a) not in ('unposted', 'posted', 'declined')
               for a in rev.get('answerPublications', []))


# pr-babysit stops another launch continues from, and stops that end it (launch_result's stalled stops and a spent budget).
CONTINUED = ('yielded', 'reviews-pending', 'github-rate-limited')
FINAL = ('budget-exhausted', 'held-awaiting-caller', *STALLED, *ENDED)


def next_step(led, facts, live, publish, cmd, review=None, manifest=None, reading=None, fixable_now=None, token=None):
    """The run's next step after a prepare whose facts are `facts` (None: none on the ledger), from the ledger, the
    PR's `live` {head, state} and the run's files so far: `review` {result} of this phase's launch, `manifest`
    {path, reviewedHead, findings}, `reading` (launch_result.py's), fixable_now(head) (show --fixable on a fresh
    snapshot); `cmd(script, *args)` spells a command."""
    release = cmd('prepare.py', '--release', '--token', token) if token else None

    def report(why):
        return step('report', why, release)
    if facts is None:
        return step('blocked', 'no prepare facts for --head on the ledger: prepare.py on it first')
    head, recheck_from, repair = facts['head'], facts.get('recheckFrom'), facts.get('repair')
    if live['state'] != 'OPEN':
        return report(f"the PR is {str(live['state']).lower()}")
    if (publish == 'auto-fix') != bool(recheck_from or repair):
        return step('blocked', f"--publish {publish} does not match the prepare of {head}, "
                               f"{'a recheck' if recheck_from else 'with --auto-fix' if repair else 'without --auto-fix'}")
    same = facts['mode'] == 'same'
    if recheck_from:
        return recheck_step(led, facts, live, cmd, report, review, reading, fixable_now)
    skip = publish == 'auto-fix' and same and repair.get('submittedReview') and (
        repair['state'] == 'available' or repair.get('deferredAnswers'))
    if skip:
        return repair_step(led, head, repair, live, cmd, report, manifest, reading, token)
    mode = 'pending' if same else POST_MODE[publish]
    got = launch_of(led, review, head, None, cmd, mode, live, report)
    if got['step']:
        return got['step']
    outcome = post_outcome(got['pub']) if got['pub'] else 'nothing to publish'
    if outcome == 'unposted':
        what = 'the discussion\'s answers' if same else 'the review'
        return step('post', f'{mode} post of {what} saved on {head}', cmd('post.py', '--expected-head', head, *POST_FLAGS[mode]), mode)
    if publish != 'auto-fix':
        return report(f'post: {outcome}')
    if same:
        return report(f"post: {outcome}; review submitted; auto-fix blocked: {repair['why']}" if repair.get('submittedReview')
                      else f"post: {outcome}; the auto-fix repairs nothing: {repair.get('submittedWhy')}")
    if outcome != 'posted':
        return report(f'post: {outcome}, which ends auto-fix there')
    return repair_step(led, head, repair, live, cmd, report, manifest, reading, token)


def launch_of(led, review, head, recheck_from, cmd, mode, live, report):
    """{step} when this phase's review launch still decides the step, else {pub}: the publication saved from it, None
    for a discussion with no answer."""
    r = (review or {}).get('result') if review else None
    ours = review is not None and (not isinstance(r, dict) or r.get('head') == head and (
        r.get('status') != 'reviewed' or r.get('recheckFrom') == recheck_from))
    if not ours:
        if live['head'] != head:
            return {'step': report(f"the PR head moved to {live['head']} since prepare pinned {head}")}
        what = 'recheck' if recheck_from else 'review'
        return {'step': step('review', f'launch the pr-review {what} of {head}, then result.py; save it when reviewed; '
                                       'pass its output as --review-output', post=mode)}
    if not isinstance(r, dict):
        return {'step': report('the review launch returned nothing')}
    if r.get('status') != 'reviewed':
        return {'step': report(f"the review launch ended {r.get('status')}: {r.get('reason')}")}
    save = cmd('ledger.py', 'save', '--output', review['path'])
    if r.get('mode') == 'discussion':
        return discussion_of(led, head, review['path'], save, mode)
    on_head = [x for x in led['reviews'] if x['head'] == head]
    # prepare refused an unsettled draft on this head, so one there now is this run's save, recorded or not.
    mine = [x for x in on_head if x.get('output') == review['path'] or x['status'] in DRAFTS]
    if mine:
        return {'step': None, 'pub': mine[-1]}
    if any(x['status'] == 'posted' and 'output' not in x for x in on_head):
        return {'step': step('blocked', f'a review of {head} posted before saves recorded their launch may be this one: if it is, '
                                        f"report it; else {' '.join(save)} --reason <the request's words>")}
    again = any(x['status'] == 'posted' for x in on_head)
    return {'step': step('review', 'save the reviewed launch' + (", with --reason and the request's words: the head was "
                                                                  'posted before' if again else ''), None if again else save, mode)}


def discussion_of(led, head, path, save, mode):
    """launch_of for a discussion: saved once the last review lists its output, its publication the answers carrying
    that output, or, saved before discussions recorded it, the unsettled one prepare left none of."""
    rev = last(led) or {}
    pubs = [a for a in rev.get('answerPublications', []) if not a.get('auto') and a['head'] == head]
    mine = [a for a in pubs if a.get('output') == path] or [a for a in pubs if 'output' not in a and a['status'] in DRAFTS]
    if not mine and path not in rev.get('discussions', []):
        if any('output' not in a for a in pubs):
            return {'step': step('blocked', f'answers on {head} published before saves recorded their launch may be this one: if '
                                            f"they are, report them; else {' '.join(save)}")}
        return {'step': step('review', 'save the reviewed discussion', save, mode)}
    return {'step': None, 'pub': (mine or [None])[-1]}


def repair_step(led, head, repair, live, cmd, report, manifest, reading, token):
    """Auto-fix after our review submitted on `head`: repair, pr-babysit, fix notes, then the recheck."""
    rev = last(led)
    if not rev or rev['head'] != head or not ((rev.get('receipts') or {}).get('review') or {}).get('submitted'):
        return step('blocked', f'the last review on the PR is not one submitted on {head}')
    bundle = deferred_bundle(rev)

    def end(why):
        if bundle and live['head'] == head:
            return step('publish-answers', f'{why}; the deferred answers go into a pending review, then report',
                        cmd('post.py', '--expected-head', head), 'pending')
        return report(why)
    if repair['state'] != 'available':
        return end(f"review submitted; auto-fix blocked: {repair['why']}")
    if manifest and manifest.get('reviewedHead') != head:
        return step('blocked', f"the --fixed manifest is for {manifest.get('reviewedHead')}, not the reviewed head {head}")
    if reading is None:
        if live['head'] != head:
            return end(f"head moved before repair: the PR head is {live['head']}")
        if manifest is None:
            return step('repair', f'repair the fix findings of our review submitted on {head}; pass the --fixed manifest')
        return step('babysit', 'launch pr-babysit on the repairs; pass its reading as --babysit-reading')
    if manifest is None:
        return step('blocked', 'a pr-babysit reading without the repair\'s --fixed manifest')
    adoption = ((reading.get('receipts') or {}).get('adoption') or {}).get('publication')
    launch = (reading.get('stateRef') or {}).get('digest') if isinstance(reading.get('stateRef'), dict) else None
    if adoption in PUBLISHED and manifest.get('findings'):
        if not launch:
            return step('blocked', 'the pr-babysit reading names no stateRef digest to key its fix notes by')
        if launch not in rev.get('fixedLaunches', []):
            return step('fixed', f"launch {launch}'s adoption is {adoption}: fix notes on {live['head']}",
                        cmd('post.py', '--expected-head', live['head'], '--fixed', manifest['path'], '--launch', launch))
    result = reading.get('result')
    if not isinstance(result, dict):
        return step('babysit', 'the launch returned nothing: relaunch it from the stateRef it was given')
    if result.get('pass') is not True:
        reason, budget = result.get('reason'), reading.get('budget') or {}
        if isinstance(budget.get('cyclesUsed'), int) and isinstance(budget.get('maxCycles'), int) and \
                budget['cyclesUsed'] >= budget['maxCycles']:
            return end(f"pr-babysit stopped {reason} with its cycle budget spent ({budget['cyclesUsed']} of {budget['maxCycles']}): a "
                       "larger maxCycles is the user's decision; the recheck is pending")
        if reason in CONTINUED:
            return step('babysit', f"pr-babysit stopped {reason}: relaunch it under chief's rules")
        if reason in FINAL:
            return end(f'pr-babysit ended {reason} without passing; the recheck is pending')
        first = (reading.get('blockers') or ['none named'])[0]
        return step('blocked', f'pr-babysit stopped {reason}, which the recipe does not place; first blocker: {first}')
    left = (reading.get('checkout') or {}).get('head')
    if not left:
        return step('blocked', 'the pr-babysit reading names no checkout.head: read the launch with --checkout')
    if live['head'] != left:
        return end(f"head moved after repair: pr-babysit left {left}, the PR head is {live['head']}")
    if left == head and (bundle or bundle_sent(rev)):
        return end('pr-babysit left the reviewed head')
    return step('recheck', f'prepare the recheck of {left} from the launcher\'s checkout top level, then pass its head as --head',
                cmd('prepare.py', '--recheck-from', head, *(('--token', token) if token else ())))


def recheck_step(led, facts, live, cmd, report, review, reading, fixable_now):
    """The recheck, after a repair from our review submitted on facts' recheckFrom."""
    head, reviewed = facts['head'], facts['recheckFrom']
    left = ((reading or {}).get('checkout') or {}).get('head')
    if not left:
        return step('blocked', "a recheck needs pr-babysit's last reading with its checkout.head (--babysit-reading)")
    if left != head:
        return report(f'head moved after repair: pr-babysit left {left}, the recheck pinned {head}')
    same = facts['mode'] == 'same'
    got = launch_of(led, review, head, reviewed, cmd, 'pending' if same else None, live, report)
    if got['step']:
        return got['step']
    outcome = post_outcome(got['pub']) if got['pub'] else 'nothing to publish'
    if outcome != 'unposted':
        return report(f'recheck post: {outcome}')
    if same:
        return step('post', 'the recheck discussion\'s answers, pending', cmd('post.py', '--expected-head', head), 'pending')
    try:
        fx = fixable_now(reviewed)
    except Unusable as e:
        return step('blocked', f'ledger.py show --fixable on {reviewed}: {e}')
    held = ', '.join(f"{h['findingId']} ({h['why']})" for h in fx['held']) or 'none'
    mode = 'auto' if fx['autoPost'] else 'pending'
    return step('post', f'recheck post, {mode}: held {held}', cmd('post.py', '--expected-head', head, *POST_FLAGS[mode]), mode)


def read_json(path, what):
    """A file's JSON object, or its last nonempty line's (a script's stdout saved whole); None for an empty file."""
    try:
        text = Path(path).read_text(encoding='utf-8')
        lines = [x for x in text.splitlines() if x.strip()]
        try:
            value = json.loads(text)
        except ValueError:
            value = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError) as e:
        raise Unusable(f'{what} {path} unreadable: {e}')
    if value is not None and not isinstance(value, dict):
        raise Unusable(f'{what} {path} is not an object')
    return value


def next_of(a, repo):
    """ledger.py next's I/O around next_step."""
    from threads import snapshot  # threads imports this module
    led = load(ledger_path(repo, a.pr), repo, a.pr)
    path = facts_path(repo, a.pr, a.head)
    facts = read_json(path, 'prepare facts') if path.exists() else None
    facts = facts and {**facts, 'head': a.head}
    pr = gh_json('pr', 'view', str(a.pr), '--repo', repo, '--json', 'headRefOid,state')
    manifest = a.manifest and read_json(a.manifest, '--manifest')
    reading = a.babysit_reading and read_json(a.babysit_reading, '--babysit-reading')
    review = a.review_output and {'path': Path(a.review_output).resolve().as_posix(),
                                  'result': (load_output(a.review_output) or {}).get('result')}

    def cmd(script, *args):
        return [sys.executable, (HERE / script).as_posix(), *args, '--pr', str(a.pr), '--repo', repo]
    return next_step(led, facts, {'head': pr['headRefOid'], 'state': pr.get('state')}, a.publish, cmd, review,
                     manifest and {**manifest, 'path': a.manifest}, reading,
                     lambda h: fixable(led, snapshot(repo, a.pr), h), a.token)


def collect(argv):
    p = Parser(prog='ledger.py')
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('show', 'save', 'next'):
        s = sub.add_parser(name)
        s.add_argument('--pr', type=int, required=True)
        s.add_argument('--repo')
    which = sub.choices['show'].add_mutually_exclusive_group()
    which.add_argument('--finding')
    which.add_argument('--draft', action='store_true')
    which.add_argument('--pending', action='store_true')
    which.add_argument('--threads')
    sub.choices['show'].add_argument('--head')
    sub.choices['show'].add_argument('--fixable', action='store_true')
    sub.choices['show'].add_argument('--view', action='store_true')
    sub.choices['save'].add_argument('--output', required=True)
    sub.choices['save'].add_argument('--reason')
    nxt = sub.choices['next']
    nxt.add_argument('--head', required=True)
    nxt.add_argument('--publish', required=True, choices=tuple(POST_MODE))
    for flag in ('--token', '--review-output', '--manifest', '--babysit-reading'):
        nxt.add_argument(flag)
    a = p.parse_args(argv)
    repo = repo_of(a.repo)
    if a.cmd == 'next':
        if not FULL_SHA.match(a.head):
            raise Unusable('--head must be a full SHA')
        return next_of(a, repo)
    path = ledger_path(repo, a.pr)
    if a.cmd == 'show' and a.fixable and not (a.threads and a.head):
        p.error('--fixable needs --threads and --head')
    if a.cmd == 'show' and a.view and (a.fixable or not a.head):
        p.error('--view needs --threads and --head, and not --fixable')
    if getattr(a, 'head', None) is not None:
        if not a.threads:
            p.error('--head needs --threads')
        if not FULL_SHA.match(a.head):
            raise Unusable('--head must be a full SHA')
    snapshot = read_snapshot(a.threads, repo, a.pr) if getattr(a, 'threads', None) else None
    if a.cmd == 'show':
        led = load(path, repo, a.pr)
        if a.fixable:
            return {'ledger': path.as_posix(), 'fixable': fixable(led, snapshot, a.head)}
        out = {'ledger': path.as_posix(), **show(led, a.finding, a.draft, snapshot, a.pending)}
        out = {**out, 'disputes': disputes(led, snapshot, a.head)} if a.head else out
        return workflow_view(out, repo, a.pr, a.head) if a.view else out
    result = (load_output(a.output) or {}).get('result')
    if not isinstance(result, dict):
        raise Unusable(f'--output {a.output} holds no Workflow result')
    if result.get('status') != 'reviewed':
        raise Unusable(f"the launch ended {result.get('status')!r} ({result.get('reason')}): nothing to save")
    with locked(path):
        led = load(path, repo, a.pr)
        out = save(led, result, a.reason, Path(a.output).resolve().as_posix())  # next's launch identity
        store(path, led)
    return {'ledger': path.as_posix(), **out}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
