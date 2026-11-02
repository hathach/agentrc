#!/usr/bin/env python3
"""Publish the pending pr-review draft on the ledger as a pending GitHub review, or submit it under auto-post, and
settle what the human did with it.

  post.py --pr N --expected-head SHA [--repo OWNER/NAME]           # a pending review for the human to finish
  post.py --pr N --expected-head SHA --auto [--defer-answers] [--repo OWNER/NAME]  # submit it
  post.py --pr N --expected-head SHA --decline --reason TEXT [--repo OWNER/NAME]
  post.py --pr N --sync [--repo OWNER/NAME]
  post.py --pr N --expected-head SHA --fixed FILE [--repo OWNER/NAME]   # auto-fix: notes and resolves for a pushed repair

Uses only the draft ledger.py saved for that head, never a regenerated one. Its review body ends with the marker
<!-- agentrc-pr-review:<head>:<draft digest> -->. Before creating anything it refuses unless the PR head is still
the expected head, looks for a review of ours carrying the marker (one found is read back instead of created
again, so a relaunch after a crash or a lost response never creates two) and refuses while any other pending
review of ours is on the PR, as GitHub allows one.

By default the review is created PENDING, with no event: body, inline comments, commit_id the head. The draft's
fix notes and each thread answer of its findings not yet published go into it as replies on our own earlier
threads (GraphQL addPullRequestReviewThreadReply), an answer only while the thread's pushback is still the one
it was judged on and the answer still matches its digest. Nothing is public until the human submits it on
GitHub, choosing the event; deleting a comment or reply before that declines it. The review is `drafted`.

Text over its length limit, measured here (a comment moved into the body as the comment it was), is never cut: a
pending review names it in overLength for the human to shorten, and --auto refuses to submit it; an answer
counts only where it is published, never under --auto.

--auto takes the same path, refusing APPROVE: the pending review with its fix notes (never its answers), then,
while the head is still the draft's and every note reads back, a submit with the draft's event (its intent stored
first, never sent twice) and the same settling as a human's submit. A moved head, an unconfirmed note, a pending
review an earlier run left (only the human sees it, so it may have changed) or a submit whose outcome cannot be
read hands the pending review to the human instead (handedOver, exit 1), who finishes it like any draft; a later
post.py run then publishes its answers. Thread answers are never submitted: the review's are put in an answer publication on it (ledger.py's add_answers), stored with
the review's send intent, and the same run publishes that as a pending review for the human. --defer-answers, for
auto-fix, keeps it stored (answers deferred, exit 0): the pending review would block the repair (ledger.py's fixable,
GitHub's review replies) and the recheck, so a plain post.py run publishes it afterwards while the head is still the
review's. Each run takes only the expected head's publications, so one left on an earlier head is never published.

--sync, run by prepare.py first, settles each drafted or uncertain review from GitHub. Deleted: declined. Still
pending: left, and prepare refuses to start another; a verified auto one is handed to the human (an unverified one is
left for --auto to recover by its marker). Submitted: posted, with the event the human chose. There:
- a deleted inline comment drops its finding, and an edited one keeps it with the text published;
- a reply is published, edited, rejected (deleted before submitting) or lost (never confirmed);
- a concession withdraws its finding only when published unedited (ledger.py holds the withdrawal until then);
- the threads of published concessions and fix notes, and those its recheck found still due (ledger.py's
  resolve_due), are resolved while the PR head is the review's, a published reply of ours is on the thread, nobody
  has pushed back since and the thread is open; otherwise the next review finds the resolve due again. Each resolve is marked sent before the mutation, then
  resolved, deferred or observed (already resolved): one left sent (its answer unconfirmed), or resolved and
  reopened since, is never resolved again by itself.

--fixed FILE, once an auto-fix repair is pushed and the PR head is SHA: FILE is {"reviewedHead": the last review's
head, which must be submitted, "findings": [{id, claimDigest, verdict: "verified", checkedSha: SHA, note, evidence}]},
each row a fix check that verified that finding's repair on SHA, its claimDigest the one ledger.py show --fixable gives.
A bad row refuses the whole file before anything is sent. For each finding it posts `Fixed in <SHA>: <note>` as a reply
on its thread (no review), reads it back, then resolves the thread; a fresh read of the PR before each send and each
resolve skips or defers it on a closed or merged PR, a moved head, a resolved thread, a changed claim, a deleted or
edited root or note, a disputed finding, pushback since our last answer, or, for a new note only, a pending review of
ours; a PR closed or merged before the run refuses it whole. One record per
finding and root (ledger.py's repairs) across runs and heads: a rerun returns it, retries a deferred resolve only on a
fix check of the same claim, never resends an unconfirmed note, and holds a resolve left sent or a thread reopened since.
A body-moved finding has no thread to note. A later review's draft never repeats a repair note.

A write is stored as sent before it is sent, so a run that dies in it only looks for it afterwards (a review by
its marker, a reply by its thread and body digest), never sends it twice. --decline records that the human
declined a draft never created. A POST GitHub itself refused (its rate limit or Validation Failed, on a 403, 422 or
429) whose marker does not read back right after created nothing: the review stays pending and unsent, reported
failed, and a later run may POST it again; one that does read back is recovered. Any other error stays sent. stdout ends with
one JSON line; exit 0 when drafted, posted, declined, synced or repaired (--fixed: every row noted and resolved), 1 when failed, partial, uncertain or handed over,
2 with {"error": ...} when nothing was attempted.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import (DRAFTS, ONLINE, OPEN, SETTLED, UNSETTLED, add_answers, answered_ids, answers_of, claim,  # noqa: E402
                    holder, open_answers, owner_of, publications, pushback, reached, repair_hold, repairs, submitted_last,
                    thread_roots, digest as ledger_digest, ledger_path, load, locked, repo_of, store)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import FULL_SHA, Parser, Unusable, attempt  # noqa: E402
from harvest import digest, gh_json, pages  # noqa: E402
from threads import record, snapshot, threads  # noqa: E402

STATE = {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED', 'COMMENT': 'COMMENTED', None: 'PENDING'}
EVENT = {v: k for k, v in STATE.items()}
ADD_REPLY = ('mutation($r:ID!,$t:ID!,$b:String!){addPullRequestReviewThreadReply('
             'input:{pullRequestReviewId:$r,pullRequestReviewThreadId:$t,body:$b}){comment{databaseId}}}')
RESOLVE = 'mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}'


def marker(review):
    return f"<!-- agentrc-pr-review:{review['head']}:{review['draft']['digest']} -->"


def body_of(review):
    body = review['draft']['body']
    return f"{body}\n\n{marker(review)}" if body else marker(review)


def review_comments(repo, pr, rid):
    return {c['id']: c for c in pages(f'repos/{repo}/pulls/{pr}/reviews/{rid}/comments')}


def pr_head(repo, pr):
    return gh_json('pr', 'view', str(pr), '--repo', repo, '--json', 'headRefOid')['headRefOid']


def head_if_open(repo, pr):
    """(head, None) while the PR is open, else (head, why no fix note or resolve may land on it)."""
    v = gh_json('pr', 'view', str(pr), '--repo', repo, '--json', 'headRefOid,state')
    return v['headRefOid'], None if v.get('state') == 'OPEN' else f"the PR is {str(v.get('state')).lower()}"


def read_back(repo, pr, rid, review):
    """(True, comment ids by draft position, node id) on a match, (False, why, None) on a mismatch, (None, why, None)
    unread. A pending review's replies on earlier threads are among its comments: only its own threads count."""
    try:
        got = gh_json('api', f'repos/{repo}/pulls/{pr}/reviews/{rid}')
        comments = [c for c in review_comments(repo, pr, rid).values() if not c.get('in_reply_to_id')]
    except (Unusable, ValueError):
        return None, 'read-back failed', None
    if got.get('state') != 'PENDING' or digest(got.get('body')) != digest(body_of(review)):
        return False, f"read back state {got.get('state')}, body digest {digest(got.get('body'))}", None
    # GitHub gives a pending review's comments no line until it is submitted.
    key = lambda c: (c.get('path'), digest(c.get('body')))  # noqa: E731
    want = [key(c) for c in review['draft']['comments']]
    if sorted(want) != sorted(key(c) for c in comments):
        return False, f'inline comments differ: {len(comments)} read back, {len(want)} drafted', None
    by_key = {}
    for c in comments:
        by_key.setdefault(key(c), []).append(c.get('id'))
    if len(set(want)) < len(want):
        # Same path and body on two lines: which is which shows once submitted, when submitted() matches them.
        return True, None, got.get('node_id')
    return True, [by_key[k].pop(0) for k in want], got.get('node_id')


def find_ours(repo, pr, review, login):
    """Our reviews carrying the draft's marker, and our other pending ones."""
    tag = marker(review)
    mine = [r for r in pages(f'repos/{repo}/pulls/{pr}/reviews') if (r.get('user') or {}).get('login') == login]
    return ([r for r in mine if tag in (r.get('body') or '')],
            [r for r in mine if r.get('state') == 'PENDING' and tag not in (r.get('body') or '')])


def settled(repo, pr, rid, review, sent, recovered):
    ok, got, node = read_back(repo, pr, rid, review)
    return {'sent': sent, 'reviewId': rid, 'nodeId': node, 'verified': ok, 'recovered': recovered,
            'error': None if ok else got, 'commentIds': got if ok else None}


# GitHub's own refusals, which it answers before creating anything; any other error may follow a review created.
REJECTED = re.compile(r'(API rate limit exceeded|secondary rate limit|Validation Failed).*\(HTTP (403|422|429)\)', re.S)


def post_review(repo, pr, review, login, recover_only, persist):
    """Create the review PENDING, or find the one an earlier run created."""
    found, others = find_ours(repo, pr, review, login)
    if len(found) > 1:
        return {'sent': False, 'reviewId': None, 'verified': None, 'recovered': False,
                'error': f'{len(found)} reviews carry this draft\'s marker; reconcile by hand'}
    if found:
        return settled(repo, pr, found[0]['id'], review, sent=False, recovered=True)
    if recover_only:
        # An earlier POST may have landed unseen: posting again could duplicate it.
        return {'sent': True, 'reviewId': None, 'verified': None, 'recovered': False,
                'error': 'an earlier POST was sent unverified and no review carries its marker yet; reconcile by hand'}
    if others:
        raise Unusable(f"your pending review {others[0]['id']} is open on the PR: submit or delete it on GitHub first")
    payload = {'commit_id': review['head'], 'body': body_of(review),
               'comments': [{'path': c['path'], 'line': c['line'], 'side': 'RIGHT', 'body': c['body']}
                            for c in review['draft']['comments']]}
    # Stored before the POST, so a run that dies in it only recovers by the marker.
    persist({'sent': True, 'reviewId': None, 'verified': None, 'recovered': False, 'error': 'sent; not confirmed'})
    code, out, err = attempt('gh', 'api', '--method', 'POST', f'repos/{repo}/pulls/{pr}/reviews', '--input', '-',
                             input=json.dumps(payload))
    try:
        rid = json.loads(out)['id'] if code == 0 else None
    except (ValueError, KeyError, TypeError):
        rid = None
    if rid is None and code != 0 and REJECTED.search(err or ''):
        # GitHub refused it, and the marker read back right after shows nothing: a later run may POST again.
        found, _ = find_ours(repo, pr, review, login)
        if len(found) == 1:
            return settled(repo, pr, found[0]['id'], review, sent=True, recovered=True)
        if not found:
            return {'sent': False, 'reviewId': None, 'verified': None, 'recovered': False, 'error': err.strip()[:300]}
    if rid is None:
        # A lost answer may still have posted: the marker settles it on the next run.
        return {'sent': True, 'reviewId': None, 'verified': None, 'recovered': False,
                'error': (err or out).strip()[:300] or 'no review id in the answer'}
    return settled(repo, pr, rid, review, sent=True, recovered=False)


def publish_review(repo, pr, review, login, recover_only, persist_all):
    """post_review with its receipt kept on the ledger; the review's comment ids go to its findings once verified."""
    rec = review['receipts']
    sent = bool((rec.get('review') or {}).get('sent'))
    if not (rec.get('review') or {}).get('verified'):
        before = review['status']

        def persist(intent):
            rec['review'] = intent
            # From its send intent on, the review may be on GitHub, whatever head the PR has moved to.
            review['status'] = 'uncertain' if review['status'] == 'pending' else review['status']
            persist_all()
        got = post_review(repo, pr, review, login, sent or recover_only, persist)
        # Once sent, always sent: no later receipt may clear it, or a run would POST again.
        rec['review'] = {**got, 'sent': got['sent'] or sent}
        if not rec['review']['sent']:
            review['status'] = before
        persist_all()
    return rec['review'].get('verified') is True


def own_comments(led):
    """Inline comment ids our reviews on the PR posted, as read back: the only threads we answer."""
    return {i for r in reached(led) for i in ((r.get('receipts') or {}).get('review') or {}).get('commentIds') or [] if i}


def live_thread(comments, root):
    """The replies to root, oldest first, as threads.py records them, so ledger.pushback applies unchanged."""
    return [record('review', c) for c in sorted((c for c in comments if c.get('in_reply_to_id') == root),
                                                key=lambda c: (c.get('created_at') or '', c['id']))]


def live_key(thread, login, known):
    """The pushback in the thread, keyed as ledger.py show --head keys its disputes."""
    return ledger_digest([[c['id'], c['digest']] for c in pushback(thread, known, login)])


def graphql(query, **fields):
    """(data, None), or (None, why): a failure goes into the receipt."""
    try:
        got = gh_json('api', 'graphql', '-f', f'query={query}', *(x for k, v in fields.items() for x in ('-f', f'{k}={v}')))
    except Unusable as e:
        return None, str(e)[:300]
    return got.get('data'), '; '.join(e.get('message', '?') for e in got.get('errors') or [])[:300] or None


def replies_of(led, review, answers=True):
    """What a review publishes on our earlier threads: its fix notes, then each unpublished answer of its findings."""
    fixes = repairs(led)  # a repair note of any state is the finding's fix note: a draft saved before it never repeats it
    out = [{'kind': 'fixnote', 'findingId': r['findingId'], 'commentId': r['commentId'], 'body': r['body'], 'resolve': True}
           for r in review['draft']['replies'] if (r['findingId'], r['commentId']) not in fixes]
    return out + [{'kind': 'answer', 'findingId': f['id'], 'commentId': f.get('commentId'), 'body': d['answer']['body'],
                   'resolve': d['answer']['resolve'], 'dispute': d} for f, d in (answers_of(led, review) if answers else [])]


def refresh(e, comments, final):
    """Settle a staged reply against its review's comments as they stand; `final` once the review is submitted."""
    if e['state'] not in ('sent', 'staged', 'uncertain'):
        return
    c = comments.get(e.get('replyId'))
    if c is None and e['state'] != 'staged':
        # Only our staging adds replies to our own pending review: identical ones are the answer, duplicated.
        hits = [x for x in comments.values()
                if x.get('in_reply_to_id') == e['commentId'] and ledger_digest(x.get('body') or '') == e['digest']]
        c = min(hits, key=lambda x: x['id']) if hits else None
    if c is None:
        # A staged reply gone from the review was deleted by the human; one never confirmed is not re-added.
        e['state'] = 'rejected' if e['state'] == 'staged' else 'lost' if final else 'uncertain'
        return
    e.update(replyId=c['id'], error=None)
    if not final:
        e['state'] = 'staged'
    elif ledger_digest(c.get('body') or '') == e['digest']:
        e['state'] = 'published'
    else:
        e.update(state='edited', published=c.get('body'))


def stage(repo, pr, led, review, login, persist, answers=True):
    """Add the review's replies to its pending review, once each; returns the staged entries."""
    rid, node = review['receipts']['review']['reviewId'], review['receipts']['review']['nodeId']
    staged = review['receipts'].setdefault('staged', [])
    todo = replies_of(led, review, answers)
    if staged:
        comments = review_comments(repo, pr, rid)
        for e in staged:
            refresh(e, comments, final=False)
    new = [r for r in todo if not any((e['kind'], e['findingId']) == (r['kind'], r['findingId']) for e in staged)]
    added = []
    if new:
        own, known = own_comments(led), answered_ids(led)
        public = pages(f'repos/{repo}/pulls/{pr}/comments')
        thread_of = {t['commentIds'][0]: t['threadId'] for t in threads(repo, pr) if t['commentIds']}
    for r in new:
        e = {'kind': r['kind'], 'findingId': r['findingId'], 'commentId': r['commentId'], 'digest': ledger_digest(r['body']),
             'resolve': r['resolve'], 'threadId': thread_of.get(r['commentId']), 'replyId': None}
        staged.append(e)
        added.append(e)
        d = r.get('dispute')
        if r['commentId'] not in own:
            e.update(state='skipped', error='not a comment our reviews posted; not answered')
        elif not e['threadId']:
            e.update(state='skipped', error='no review thread holds the comment')
        elif d and live_key(live_thread(public, r['commentId']), login, known) != d['key']:
            e.update(state='skipped', error='the thread changed since it was judged; review again')
        elif d and d['answer']['digest'] != e['digest']:
            e.update(state='skipped', error='the stored answer no longer matches its digest')
        else:
            # Stored before sending, so a run that dies mid-send looks for it, never adds it again.
            e.update(state='sent', error='sent; not confirmed')
            persist()
            data, err = graphql(ADD_REPLY, r=node, t=e['threadId'], b=r['body'])
            cid = (((data or {}).get('addPullRequestReviewThreadReply') or {}).get('comment') or {}).get('databaseId')
            e.update(state='staged', replyId=cid, error=None) if cid else e.update(state='uncertain', error=err or 'no reply id in the answer')
    if any(e['state'] == 'staged' for e in added):
        comments = review_comments(repo, pr, rid)
        for e in added:
            if e['state'] == 'staged' and ledger_digest((comments.get(e['replyId']) or {}).get('body') or '') != e['digest']:
                e.update(state='uncertain', error='not read back on the pending review')
    return staged


def due_resolves(review):
    """(finding, root comment) for the due resolves the review's recheck reconfirmed."""
    by_id = {f['id']: f for f in review.get('findings', [])}
    return [(by_id[x['findingId']], x['commentId']) for x in review['draft'].get('resolves', []) if x['findingId'] in by_id]


def resolve(thread_id):
    """(True, None) once GitHub answers the thread resolved, else (False, why)."""
    data, err = graphql(RESOLVE, t=thread_id)
    ok = bool((((data or {}).get('resolveReviewThread') or {}).get('thread') or {}).get('isResolved'))
    return ok, None if ok else f'resolve unconfirmed: {err}'


def resolve_threads(repo, pr, led, review, want, login, persist):
    """Resolve each (finding, root comment) thread while the PR head is the review's, a published reply of ours is on
    it and nobody pushed back after the last one, marking each in receipts.resolved."""
    if not want:
        return
    head = pr_head(repo, pr)
    public = pages(f'repos/{repo}/pulls/{pr}/comments')
    by_root = {t['commentIds'][0]: t for t in threads(repo, pr) if t['commentIds']}
    known, marks, fixes = answered_ids(led), review['receipts'].setdefault('resolved', []), repairs(led)
    for f, root in want:
        t = by_root.get(root)
        replies = live_thread(public, root)
        fix = fixes.get((f['id'], root))
        # A draft saved before a repair's resolve was reopened or left unconfirmed never resolves it again.
        why = ('no review thread holds the comment' if not t else None if t['resolved'] else
               (fix and repair_hold(fix, t)) or
               (f"the PR head moved to {head} since the review" if head != review['head'] else
                'no published reply of ours on the thread' if not any(c['id'] in known for c in replies) else
                'new replies since our last one' if pushback(replies, known, login) else None))
        mark = {'findingId': f['id'], 'commentId': root, 'state': 'deferred' if why else 'observed', 'error': why}
        marks.append(mark)
        if t and not why and not t['resolved']:
            # Stored before the mutation: a resolve not confirmed resolved is never tried again.
            mark.update(state='sent')
            persist()
            ok, err = resolve(t['threadId'])
            mark.update(state='resolved') if ok else mark.update(error=err)


def declined(led, review, reason):
    review.update(status='declined', declineReason=reason)
    try:
        answers = answers_of(led, review) if 'answers' in review else []
    except Unusable:
        answers = []  # the review they answer never reached the PR: nothing of theirs stands on it
    for _, d in answers:
        d['answer']['outcome'] = 'rejected'


def match_roots(drafted, roots):
    """Comment ids by draft position for a review whose creation answer was lost, place (path, line) by place: a
    place one draft comment has takes its one live comment when that still reads as drafted. An edit and a
    replacement look alike, as does any comment on a shared place: unsure while a live comment is left on the
    place, deleted (None) once none is."""
    place = lambda x: (x.get('path'), x.get('line'))  # noqa: E731
    ids, unsure = [None] * len(drafted), set()
    for p in {place(c) for c in drafted}:
        ks = [k for k, c in enumerate(drafted) if place(c) == p]
        live = [r for r in roots if place(r) == p]
        if len(ks) == 1 and len(live) == 1 and digest(live[0].get('body')) == digest(drafted[ks[0]]['body']):
            ids[ks[0]] = live[0]['id']
        elif live:
            unsure |= set(ks)
    return ids, unsure


def submitted(repo, pr, led, review, state, login, persist):
    """A pending review the human submitted: what survived of it becomes the record, then its threads resolve."""
    rec = review['receipts']
    got = rec['review']
    comments = review_comments(repo, pr, got['reviewId'])
    by_id = {f['id']: f for f in owner_of(led, review)['findings']}
    unsure = set()
    if got.get('commentIds') is None:
        got['commentIds'], unsure = match_roots(review['draft']['comments'], [c for c in comments.values() if not c.get('in_reply_to_id')])
    ids = []
    for k, (c, cid) in enumerate(zip(review['draft']['comments'], got['commentIds'])):
        f, live = by_id[c['findingId']], comments.get(cid)
        if k in unsure:
            pass  # which comment is its is unknown: the finding stands as it was, with no thread of ours
        elif live is None:
            f['status'] = 'dropped'
        else:
            f['commentId'] = cid
            # A finding raised again keeps the claim the PR shows: its new comment restates the human's edit.
            if digest(live.get('body')) != digest(c['body']) or f.get('published'):
                f['published'] = live.get('body')
        ids.append(live and cid)
    inline = {c['findingId'] for c in review['draft']['comments']}
    for c in review['draft'].get('moved', []):
        if c['findingId'] not in inline:
            # Raised again in the body: it has no thread now, and its old one keeps the repair that resolved it.
            by_id[c['findingId']]['commentId'] = None
    got.update(commentIds=ids, submitted=state, event=EVENT.get(state))
    review['status'] = 'posted'
    want = []
    for e in rec.get('staged', []):
        refresh(e, comments, final=True)
        f = by_id.get(e['findingId'])
        if e['kind'] == 'answer' and f:
            a = next((d['answer'] for d in reversed(f.get('disputes', [])) if (d.get('answer') or {}).get('digest') == e['digest']), None)
            if a and e['state'] in ('published', 'edited', 'rejected', 'lost', 'skipped'):
                a['outcome'] = e['state']
                if e['state'] == 'edited':
                    a['published'] = e['published']
                elif e['state'] == 'published' and a.get('status'):
                    # Only a concession the PR shows as drafted withdraws its finding.
                    f['status'] = a['status']
        if f and e['state'] == 'published' and e['resolve']:
            want.append((f, e['commentId']))
    resolve_threads(repo, pr, led, review, want + due_resolves(review), login, persist)


def sync(repo, pr, led, persist):
    """Settle every drafted or uncertain pending review of ours from GitHub; the ledger is stored on a change."""
    out, login = [], None

    def me():
        nonlocal login
        login = login or gh_json('api', 'user')['login']
        return login

    for review, _ in list(publications(led)):
        got = review['receipts'].get('review') or {}
        if review['status'] not in ONLINE:
            continue
        if not got.get('reviewId'):
            found, _ = find_ours(repo, pr, review, me())
            if len(found) != 1:
                out.append({'head': review['head'], 'status': review['status'], 'error': f'{len(found)} reviews carry its marker'})
                continue
            got.update(reviewId=found[0]['id'], nodeId=found[0].get('node_id'))
            persist()
        code, state, err = review_state(repo, pr, got['reviewId'])
        if code != 0 and 'HTTP 404' in err:
            declined(led, review, 'deleted on GitHub before it was submitted')
            persist()
        elif state is None:
            raise Unusable(f"review {got['reviewId']} unreadable: {err.strip()[:200]}")
        elif state != 'PENDING':
            submitted(repo, pr, led, review, state, me(), persist)
            persist()
        elif review.get('publish') == 'auto' and got.get('verified'):
            # The run that made it pending is gone: only the human may submit it now; an unverified one is left for
            # --auto to recover by its marker.
            hand_over(review, "the submit's outcome is unknown" if got.get('submitIntent') else EARLIER)
            persist()
        out.append({'head': review['head'], 'status': review['status'], 'event': got.get('event') if review['status'] == 'posted' else None})
    return out


EARLIER = 'an earlier run left it pending, open to changes since'


def review_state(repo, pr, rid):
    """(exit code, state or None when unread, stderr) of one of our reviews on GitHub."""
    code, text, err = attempt('gh', 'api', f"repos/{repo}/pulls/{pr}/reviews/{rid}")
    try:
        return code, json.loads(text).get('state') if code == 0 else None, err
    except ValueError:
        return code, None, err


def hand_over(review, why):
    """Leave an auto review pending on GitHub for the human, who finishes it like any draft: nothing is sent again."""
    review.update(publish='draft', status='drafted')
    review['receipts']['review']['handedOver'] = why


def submit(repo, pr, review, event, persist):
    """Submit the pending review with its event, once: the intent is stored first, and only a read-back no longer
    PENDING shows it went through. Returns that state, or None."""
    got = review['receipts']['review']
    got['submitIntent'] = event
    persist()
    attempt('gh', 'api', '--method', 'POST', f"repos/{repo}/pulls/{pr}/reviews/{got['reviewId']}/events", '--input', '-',
            input=json.dumps({'event': event}))
    state = review_state(repo, pr, got['reviewId'])[1]
    return state if state not in (None, 'PENDING') else None


def publish_auto(repo, pr, led, review, event, login, recovering, persist):
    """The draft's pending review with its fix notes, submitted with its event; handed to the human instead when the
    head moved, a note is unconfirmed or an earlier run left it pending before the submit, or when the submit's outcome
    cannot be read."""
    rec = review['receipts']
    review['publish'] = 'auto'
    answers = open_answers(review['findings'])
    if answers and not any(a.get('auto') for a in review.get('answerPublications', [])):
        # Stored with the review's send intent, so a relaunch after it lands never appends a second bundle.
        add_answers(review, review['head'], answers, auto=True)
    # Only the human sees a pending review: one an earlier run left may have been changed since.
    earlier = bool((rec.get('review') or {}).get('sent'))
    if not publish_review(repo, pr, review, login, recovering, persist):
        if not rec['review'].get('sent'):
            return {'status': 'failed', 'review': rec['review'], 'staged': rec.get('staged', [])}
        review['status'] = 'uncertain'
        persist()
        return {'status': 'uncertain', 'review': rec['review'], 'staged': rec.get('staged', [])}
    review['status'] = 'drafted'
    persist()
    moved = 'the PR head moved before the submit'
    why = (moved if recovering or pr_head(repo, pr) != review['head'] else
           EARLIER if earlier else None)
    if not why:
        staged = stage(repo, pr, led, review, login, persist, answers=False)
        why = ('a fix note was not confirmed on the pending review' if any(e['state'] in ('sent', 'uncertain') for e in staged)
               else moved if pr_head(repo, pr) != review['head'] else None)
    if not why:
        state = submit(repo, pr, review, event, persist)
        if state:
            submitted(repo, pr, led, review, state, login, persist)
        else:
            why = "the submit's outcome is unknown"
    if why:
        hand_over(review, why)
    persist()
    return {'status': review['status'], 'review': rec['review'], 'staged': rec.get('staged', []),
            'resolved': rec.get('resolved', []), **({'handedOver': why} if why else {})}


def publish_draft(repo, pr, led, review, login, recovering, persist):
    rec = review['receipts']
    review['publish'] = 'draft'
    ok = publish_review(repo, pr, review, login, recovering, persist)
    if not ok and not rec['review'].get('sent'):
        return {'status': 'failed', 'review': rec['review'], 'staged': rec.get('staged', [])}
    # After a push the review is only found again: answers judged on the old head are not added to it.
    staged = stage(repo, pr, led, review, login, persist) if ok and not recovering else rec.get('staged', [])
    review['status'] = 'drafted' if ok else 'uncertain'
    persist()
    done = ok and not any(e['state'] in ('sent', 'uncertain') for e in staged)
    return {'status': 'drafted' if done else 'partial' if ok else 'uncertain', 'review': rec['review'], 'staged': staged}


# The workflow's limits (workflows/pr-review.js LIMIT), measured again at the gate: a saved draft is not trusted to
# carry its own marks.
COMMENT_WORDS, REPLY_WORDS, LINE_CHARS = 80, 60, 300


def too_long(text, limit):
    text = text or ''
    return len([w for w in text.split() if w not in ('-', '*', '+')]) > limit or any(len(x) > LINE_CHARS for x in text.split('\n'))


def over_length(led, review, answers=True):
    """What is too long for its limit, named for the human to shorten before submitting; a comment moved into the
    body is measured as the comment it was."""
    d = review['draft']
    long = [f"{c['path']}:{c['line']}" for c in d['comments'] + d.get('moved', []) if too_long(c['body'], COMMENT_WORDS)]
    long += [f"fix note on {r['findingId']}" for r in d['replies'] if too_long(r['body'], REPLY_WORDS)]
    if answers:
        long += [f"answer on {f['id']}" for f, x in answers_of(led, review)
                 if too_long(x['answer']['body'], REPLY_WORDS)]
    return list(dict.fromkeys(long))


def read_manifest(path, head):
    """--fixed FILE: {"reviewedHead": SHA, "findings": [{id, claimDigest, verdict, checkedSha, note, evidence}]}, each
    row a fix check that verified its finding's repair on `head`."""
    try:
        m = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise Unusable(f'--fixed {path} unreadable: {e}')
    if not isinstance(m, dict):
        m = {}
    rows = m.get('findings')
    if not FULL_SHA.match(str(m.get('reviewedHead'))) or not isinstance(rows, list) or not rows:
        raise Unusable('--fixed needs {"reviewedHead": SHA, "findings": [...]} with at least one finding')
    ids = [r.get('id') for r in rows if isinstance(r, dict)]
    if len(ids) != len(rows) or len(set(ids)) != len(ids):
        raise Unusable('--fixed names a finding twice, or a row that is not an object')
    for r in rows:
        note = r.get('note')
        why = (f"its fix check is {r.get('verdict')!r}, not 'verified'" if r.get('verdict') != 'verified' else
               f"its fix check judged {r.get('checkedSha')}, not the expected head" if r.get('checkedSha') != head else
               'it names no claimDigest' if not r.get('claimDigest') else
               'its note is not one short line' if not isinstance(note, str) or not note.strip() or '\n' in note.strip()
               or too_long(fix_body(head, note), REPLY_WORDS) else
               f"its evidence {r.get('evidence')!r} is not a file" if not isinstance(r.get('evidence'), str) or not Path(r['evidence']).is_file()
               else None)
        if why:
            raise Unusable(f"{r['id']}: {why}")
    return m


def fix_body(head, note):
    return f'Fixed in {head[:12]}: {note.strip()}'


def our_pending(repo, pr, led, login):
    return any(p['status'] in ONLINE for p, _ in publications(led)) or any(
        r.get('state') == 'PENDING' and (r.get('user') or {}).get('login') == login for r in pages(f'repos/{repo}/pulls/{pr}/reviews'))


def note_reply(snap, x):
    """Our note as the PR shows it: by its recorded id, else the one reply of ours on its thread carrying its text."""
    replies = [c for c in snap['comments'] if c['kind'] == 'review' and c['inReplyTo'] == x['commentId']]
    if x['replyId']:
        return next((c for c in replies if c['id'] == x['replyId']), None)
    hits = [c for c in replies if c['id'] not in x['before'] and c['author'] == snap['viewer'] and ledger_digest(c['body']) == x['digest']]
    return hits[0] if len(hits) == 1 else None


def unfit(repo, pr, led, f, want, head, login):
    """(snapshot, why f's thread may not take a note or a resolve now, or None), read afresh: the root and claim as
    the fix check judged them (`want`, its claim digest), no pushback since our last answer, the PR open on the repaired
    head."""
    snap = snapshot(repo, pr, login)
    t = thread_roots(snap).get(f.get('commentId'))
    at, shut = head_if_open(repo, pr)
    return snap, (shut or holder(led, snap)(f) or
                  ('the claim changed since its fix check' if ledger_digest(claim(f)) != want else
                   'no review thread holds the comment' if not t else
                   f'the PR head moved to {at} since the repair' if at != head else None))


def send_note(repo, pr, x, persist):
    # Stored before the POST, so a run that dies in it only looks for the note afterwards, never sends it twice.
    x.update(state='sent', error='sent; not confirmed')
    persist()
    code, out, err = attempt('gh', 'api', '--method', 'POST', f"repos/{repo}/pulls/{pr}/comments/{x['commentId']}/replies",
                             '--input', '-', input=json.dumps({'body': x['body']}))
    try:
        x['replyId'] = json.loads(out)['id'] if code == 0 else None
    except (ValueError, KeyError, TypeError):
        x['replyId'] = None
    if code:
        x['error'] = (err or out).strip()[:300] or 'no reply id in the answer'


def settle_note(repo, pr, led, f, x, row, head, login, persist):
    """Read the note back, then resolve its thread while it still stands as the note answered it and this run's fix
    check (`row`, on `head`) judged that same claim: a note never acknowledges a reply, an edit or a push it was not
    sent against."""
    snap, why = unfit(repo, pr, led, f, x['claimDigest'], head, login)
    c = note_reply(snap, x)
    if x['state'] in ('sent', 'uncertain'):
        if c and ledger_digest(c['body']) == x['digest']:
            x.update(state='published', replyId=c['id'], error=None)
        else:
            x.update(state='uncertain', error=x['error'] if x['error'] != 'sent; not confirmed' else 'not read back on the thread')
            persist()
            return
    t = thread_roots(snap).get(x['commentId'])
    if t and t['resolved']:
        x.update(resolve='observed', error=None)
        persist()
        return
    why = (why or ('the claim changed since its fix check' if row['claimDigest'] != x['claimDigest'] else
                   'our note was deleted' if c is None else
                   'our note was edited' if ledger_digest(c['body']) != x['digest'] else None))
    if why:
        x.update(resolve='deferred', error=why)
        persist()
        return
    # Stored before the mutation: a resolve not confirmed resolved is never tried again. resolveCheck is the fix check
    # the resolve stands on, kept with the note's own head and evidence.
    x.update(resolve='sent', error=None, resolveCheck={'head': head, 'evidence': row['evidence']})
    persist()
    ok, err = resolve(t['threadId'])
    x.update(resolve='resolved') if ok else x.update(error=err)
    persist()


def fix_notes(repo, pr, led, m, head, persist):
    """A fix note and a resolve on each repaired finding's thread of the last review, which must be the one submitted on
    the reviewed head; per finding, one record across runs and heads (ledger.py's repairs). Each send and each resolve
    reads the PR afresh first."""
    rev = submitted_last(led, m['reviewedHead'])
    at, shut = head_if_open(repo, pr)
    if shut:
        raise Unusable(shut)
    if at != head:
        raise Unusable(f'the PR head moved to {at}; the repair was checked on {head}')
    login = gh_json('api', 'user')['login']
    by_id, fixes, out, settled = {f['id']: f for f in rev['findings']}, repairs(led), [], None
    for row in m['findings']:
        f = by_id.get(row['id'])
        x = f and fixes.get((f['id'], f.get('commentId')))
        if x and x['resolve'] in ('sent', *SETTLED):
            # Nothing is sent for these: one read of the PR serves them all.
            settled = settled or thread_roots({'threads': threads(repo, pr)})
            t = settled.get(x['commentId'])
            why = repair_hold(x, t) or (None if t else 'no review thread holds the comment')
            out.append({**receipt(x), 'state': 'held', 'error': why} if why else receipt(x))
            continue
        if not x:
            root = f and f.get('commentId')
            why = ('not a finding of the last review' if not f else 'not standing' if f['status'] not in OPEN else
                   'no thread: it stands in the review body' if root is None else None)
            if not why:
                snap, why = unfit(repo, pr, led, f, row['claimDigest'], head, login)
                t = thread_roots(snap).get(root)
                why = why or ('its thread is resolved' if t['resolved'] else
                              'a pending review of ours is open on the PR' if our_pending(repo, pr, led, login) else None)
            if why:
                out.append({'findingId': row['id'], 'commentId': root, 'state': 'skipped', 'replyId': None, 'resolve': None, 'error': why})
                continue
            body = fix_body(head, row['note'])
            # evidence: the fix check the note stands on, kept for audit.
            x = {'findingId': f['id'], 'commentId': root, 'head': head, 'claimDigest': row['claimDigest'], 'evidence': row['evidence'],
                 'body': body, 'digest': ledger_digest(body), 'before': t['commentIds'], 'state': 'sent', 'replyId': None,
                 'resolve': None, 'error': None}
            rev.setdefault('repairs', []).append(x)
            send_note(repo, pr, x, persist)
        settle_note(repo, pr, led, f, x, row, head, login, persist)
        out.append(receipt(x))
    done = all(r['resolve'] in SETTLED and r['state'] == 'published' for r in out)
    return {'status': 'repaired' if done else 'partial', 'repairs': out}


def receipt(x):
    return {k: x[k] for k in ('findingId', 'commentId', 'state', 'replyId', 'resolve', 'error')}


def collect(argv):
    p = Parser(prog='post.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--expected-head')
    p.add_argument('--repo')
    p.add_argument('--auto', action='store_true')
    p.add_argument('--defer-answers', action='store_true')
    p.add_argument('--decline', action='store_true')
    p.add_argument('--reason')
    p.add_argument('--sync', action='store_true')
    p.add_argument('--fixed')
    a = p.parse_args(argv)
    if a.fixed and (a.auto or a.decline or a.sync or a.defer_answers):
        raise Unusable('--fixed takes only --pr, --repo and --expected-head')
    if a.sync and (a.auto or a.decline or a.expected_head):
        raise Unusable('--sync takes only --pr and --repo')
    if not a.sync and not FULL_SHA.match(a.expected_head or ''):
        raise Unusable('--expected-head must be a full SHA')
    if a.decline and not a.reason:
        raise Unusable('--decline needs --reason')
    if a.defer_answers and not a.auto:
        raise Unusable('--defer-answers goes with --auto')
    repo = repo_of(a.repo)
    path = ledger_path(repo, a.pr)
    with locked(path):
        led = load(path, repo, a.pr)
        persist = lambda: store(path, led)  # noqa: E731
        if a.sync:
            return {'status': 'synced', 'reviews': sync(repo, a.pr, led, persist)}
        # What the human already submitted or deleted on GitHub is recorded first: it is no longer a draft.
        sync(repo, a.pr, led, persist)
        if a.fixed:
            return fix_notes(repo, a.pr, led, read_manifest(a.fixed, a.expected_head), a.expected_head, persist)
        todo = [p for p, r in publications(led) if p['head'] == a.expected_head and p['status'] in UNSETTLED
                and (p is r or not a.auto)]
        if not todo:
            raise Unusable(f'no pending draft for {a.expected_head} on the ledger')
        review = todo[-1]
        if a.decline and review['status'] != 'pending':
            raise Unusable(f"the draft is {review['status']}: part of it may be on GitHub; reconcile it, do not decline it")
        if a.decline:
            declined(led, review, a.reason)
            persist()
            return {'status': 'declined', 'review': None}
        handed = (review['receipts'].get('review') or {}).get('handedOver')
        if a.auto and handed:
            return {'status': review['status'], 'review': review['receipts']['review'], 'handedOver': handed}
        mode = 'auto' if a.auto else 'draft'
        if review.get('publish', mode) != mode:
            raise Unusable(f"the draft was published {'with' if review['publish'] == 'auto' else 'without'} --auto: finish it the same way")
        event = review['draft']['event']
        if a.auto and event == 'APPROVE':
            raise Unusable('auto-post never approves; publish it without --auto for the human to submit')
        long = over_length(led, review, answers=not a.auto)
        if a.auto and long and review['status'] == 'pending':
            raise Unusable(f"over-length text ({', '.join(long)}): publish it without --auto for the human to shorten")
        rec = review['receipts']
        sent = bool((rec.get('review') or {}).get('sent')) and not (rec.get('review') or {}).get('verified')
        head = pr_head(repo, a.pr)
        # After a push only a sent-unverified review is touched, and only to find it by its marker: never POSTed again.
        recovering = head != a.expected_head
        if recovering and not sent:
            raise Unusable(f'the PR head moved to {head}; the draft reviewed {a.expected_head}')
        login = gh_json('api', 'user')['login']
        if not a.auto:
            return {**publish_draft(repo, a.pr, led, review, login, recovering, persist), 'overLength': long}
        out = publish_auto(repo, a.pr, led, review, event, login, recovering, persist)
        answers = [p for p in review.get('answerPublications', []) if p.get('auto') and p['status'] in DRAFTS]
        if answers and review['status'] == 'posted' and not recovering:
            out['answers'] = ({'status': 'deferred'} if a.defer_answers else
                              {**publish_draft(repo, a.pr, led, answers[-1], login, False, persist),
                               'overLength': over_length(led, answers[-1])})
        return out


def main(argv):
    try:
        out = collect(argv)
    except Unusable as e:
        print(json.dumps({'error': str(e)}))
        return 2
    print(json.dumps(out))
    done = out['status'] in ('posted', 'declined', 'drafted', 'synced', 'repaired') and 'handedOver' not in out
    return 0 if done and ('answers' not in out or out['answers']['status'] in ('drafted', 'deferred')) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
