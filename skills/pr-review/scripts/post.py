#!/usr/bin/env python3
"""Publish the pending pr-review draft on the ledger, read it back, record receipts.

  post.py --pr N --expected-head SHA [--repo OWNER/NAME] [--event COMMENT] [--review-only] [--auto]
  post.py --pr N --expected-head SHA --decline --reason TEXT [--repo OWNER/NAME]
  post.py --pr N --expected-head SHA --threads --approve ID[,ID...] [--repo OWNER/NAME]

Posts only the draft ledger.py saved for that head, never a regenerated one:
one GitHub review (event, body, inline comments, commit_id the head) whose body
ends with the marker <!-- agentrc-pr-review:<head>:<draft digest> -->, then the
draft's replies to our own earlier threads through pr-reply's reply.py, which
reads each back and resolves its thread. Before posting it refuses unless the
PR head is still the expected head, and looks for a review of ours carrying the
marker: one found is read back instead of posted again, so a relaunch after a
crash or a lost response never posts twice. --event COMMENT posts a stricter
draft as a comment; --review-only skips the replies; --auto refuses an APPROVE
draft, which auto-post never sends. --decline records that the human declined.

A review counts as posted only on a matching read-back: its event, its body's
digest, and each inline comment's path, line and body digest. A POST whose
answer was lost, or a read-back that failed, is `uncertain`, never retried
blind: the next run finds the marker or reports it for a human. The ledger
review's status becomes posted, partial (review verified, a reply not),
uncertain or declined, with every receipt.

--threads posts the thread answers the human approved, by finding id: the
concession or rebuttal each dispute record of the last review of that head
holds, stored with its digest before this runs, never regenerated. It posts no
review. Each goes to our own inline comment only, through reply.py, a
concession resolving its thread and a rebuttal leaving it open; the receipt
lands on the dispute record. An answer already verified is not sent again.

stdout ends with one JSON line {status, review, replies} ({status, answers}
for --threads); exit 0 when posted, 1 when partial or uncertain, 2 with
{"error": ...} when nothing was attempted.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import after_ours, answered_ids, reached, digest as ledger_digest, ledger_path, load, locked, repo_of, store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import FULL_SHA, Parser, Unusable, attempt  # noqa: E402
from harvest import digest, gh_json, pages  # noqa: E402
from state_transfer import fnv1a  # noqa: E402

REPLY = Path(__file__).resolve().parents[2] / 'pr-reply' / 'scripts' / 'reply.py'
STATE = {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED', 'COMMENT': 'COMMENTED'}


def marker(review):
    return f"<!-- agentrc-pr-review:{review['head']}:{review['draft']['digest']} -->"


def body_of(review):
    return f"{review['draft']['body']}\n\n{marker(review)}"


def pr_head(repo, pr):
    return gh_json('pr', 'view', str(pr), '--repo', repo, '--json', 'headRefOid')['headRefOid']


def require_head(repo, pr, expected, what):
    head = pr_head(repo, pr)
    if head != expected:
        raise Unusable(f'the PR head moved to {head}; {what} {expected}')


def read_back(repo, pr, rid, review, event):
    """(True, comment ids by draft position) on a match, (False, why) on a mismatch, (None, why) unread."""
    try:
        got = gh_json('api', f'repos/{repo}/pulls/{pr}/reviews/{rid}')
        comments = pages(f'repos/{repo}/pulls/{pr}/reviews/{rid}/comments')
    except (Unusable, ValueError):
        return None, 'read-back failed'
    if got.get('state') != STATE[event] or digest(got.get('body')) != digest(body_of(review)):
        return False, f"read back state {got.get('state')}, body digest {digest(got.get('body'))}"
    key = lambda c: (c.get('path'), c.get('line'), digest(c.get('body')))  # noqa: E731
    want = [key(c) for c in review['draft']['comments']]
    if sorted(want) != sorted(key(c) for c in comments):
        return False, f'inline comments differ: {len(comments)} read back, {len(want)} drafted'
    by_key = {}
    for c in comments:
        by_key.setdefault(key(c), []).append(c.get('id'))
    return True, [by_key[k].pop(0) for k in want]


def find_ours(repo, pr, review, login):
    tag = marker(review)
    return [r for r in pages(f'repos/{repo}/pulls/{pr}/reviews')
            if tag in (r.get('body') or '') and (r.get('user') or {}).get('login') == login]


def settled(repo, pr, rid, review, event, sent, recovered):
    ok, got = read_back(repo, pr, rid, review, event)
    return {'sent': sent, 'reviewId': rid, 'verified': ok, 'recovered': recovered, 'event': event,
            'error': None if ok else got, 'commentIds': got if ok else None}


def post_review(repo, pr, review, event, login, recover_only, persist):
    found = find_ours(repo, pr, review, login)
    if len(found) > 1:
        return {'sent': False, 'reviewId': None, 'verified': None, 'recovered': False,
                'error': f'{len(found)} reviews carry this draft\'s marker; reconcile by hand'}
    if found:
        return settled(repo, pr, found[0]['id'], review, event, sent=False, recovered=True)
    if recover_only:
        # An earlier POST may have landed unseen: posting again could duplicate it.
        return {'sent': True, 'reviewId': None, 'verified': None, 'recovered': False, 'event': event,
                'error': 'an earlier POST was sent unverified and no review carries its marker yet; reconcile by hand'}
    payload = {'commit_id': review['head'], 'body': body_of(review), 'event': event,
               'comments': [{'path': c['path'], 'line': c['line'], 'side': 'RIGHT', 'body': c['body']}
                            for c in review['draft']['comments']]}
    # Stored before the POST, so a run that dies in it only recovers by the marker.
    persist({'sent': True, 'reviewId': None, 'verified': None, 'recovered': False, 'event': event, 'error': 'sent; not confirmed'})
    code, out, err = attempt('gh', 'api', '--method', 'POST', f'repos/{repo}/pulls/{pr}/reviews', '--input', '-',
                             input=json.dumps(payload))
    try:
        rid = json.loads(out)['id'] if code == 0 else None
    except (ValueError, KeyError, TypeError):
        rid = None
    if rid is None:
        # A lost answer may still have posted: the marker settles it on the next run.
        return {'sent': True, 'reviewId': None, 'verified': None, 'recovered': False, 'event': event,
                'error': (err or out).strip()[:300] or 'no review id in the answer'}
    return settled(repo, pr, rid, review, event, sent=True, recovered=False)


def own_comments(led):
    """Inline comment ids our verified reviews posted, as read back: the only threads we answer."""
    return {i for r in led['reviews'] for i in ((r.get('receipts') or {}).get('review') or {}).get('commentIds') or []}


def run_reply(repo, pr, replies):
    manifest = {'replies': [{'commentId': r['commentId'], 'body': r['body'], 'digest': fnv1a(r['body']),
                             **({'resolve': r['resolve']} if 'resolve' in r else {})} for r in replies]}
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
    got = reply_json('--manifest', f.name, repo=repo, pr=pr)
    Path(f.name).unlink(missing_ok=True)
    return got.get('receipts') or [{'commentId': r['commentId'], 'sent': None, 'verified': None,
                                    'error': f"reply.py gave no receipts: {got.get('error')}"} for r in replies]


def answered(a):
    """Done only when verified and, for a concession, its thread resolved."""
    rc = a.get('receipt') or {}
    return rc.get('verified') is True and (not a['resolve'] or rc.get('resolved') is True)


def live_thread(comments, root):
    return sorted((c for c in comments if c.get('in_reply_to_id') == root), key=lambda c: (c.get('created_at') or '', c['id']))


def live_key(thread, login, known):
    """The pushback in the thread, keyed as ledger.py disputes keys it."""
    replies = after_ours(thread, lambda c: c['id'] in known, lambda c: (c.get('user') or {}).get('login') == login)
    return ledger_digest([[c['id'], digest(c.get('body'))] for c in replies])


def reconcile(repo, pr, root, mine, thread, a, login):
    """Settle an answer of ours already in the thread; never posts."""
    rc = {'sent': True, 'posted': True, 'verified': True, 'replyId': mine['id'], 'resolved': None, 'error': None}
    later = [c for c in thread[thread.index(mine) + 1:] if (c.get('user') or {}).get('login') != login]
    if not a['resolve']:
        return rc
    if later:
        return {**rc, 'resolved': False, 'error': 'new replies after our answer; thread left open'}
    inspected = reply_json('--inspect', f"{root}:{mine['id']}", repo=repo, pr=pr).get('inspected') or [{}]
    i = inspected[0]
    if i.get('error') or not i.get('bodyDigest'):
        return {**rc, 'verified': None, 'error': i.get('error') or 'inspection failed'}
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump({'reuses': [{'commentId': root, 'replyId': mine['id'], 'bodyDigest': i['bodyDigest'], 'originalDigest': i['originalDigest']}]}, f)
    got = (reply_json('--reuse', f.name, repo=repo, pr=pr).get('receipts') or [{}])[0]
    Path(f.name).unlink(missing_ok=True)
    return {**rc, 'verified': got.get('verified'), 'resolved': got.get('resolved'), 'error': got.get('error')}


def reply_json(*argv, repo, pr):
    done = subprocess.run([sys.executable, str(REPLY), '--pr', str(pr), '--repo', repo, *argv], capture_output=True, text=True)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'error': done.stderr.strip()[:200]}


def post_answers(repo, pr, led, head, approve, persist):
    reviews = [r for r in reached(led) if r['head'] == head]
    if not reviews:
        raise Unusable(f'no review of {head} on the PR per the ledger')
    todo = {}
    for f in reviews[-1]['findings']:
        d = (f.get('disputes') or [None])[-1]
        if d and d.get('answer') and not answered(d['answer']):
            todo[f['id']] = (f, d)
    unknown = sorted(set(approve) - set(todo))
    if unknown:
        raise Unusable(f'no unposted answer for {", ".join(unknown)}; pending: {", ".join(sorted(todo)) or "none"}')
    own, known = own_comments(led), answered_ids(led)
    login = gh_json('api', 'user')['login']
    comments = pages(f'repos/{repo}/pulls/{pr}/comments')
    out, send = [], []
    for fid in approve:
        f, d = todo[fid]
        a = d['answer']
        thread = live_thread(comments, f['commentId'])
        # Only a reply after the pushback it answers can be this answer; an older identical one is not.
        judged = {r['id'] for r in d['replies']}
        start = max((k + 1 for k, c in enumerate(thread) if c['id'] in judged), default=len(thread))
        visible = [c for c in thread[start:] if (c.get('user') or {}).get('login') == login and c.get('body') == a['body']]
        if f.get('commentId') not in own:
            a['receipt'] = {'sent': False, 'verified': None, 'error': 'not a comment our reviews posted; not answered'}
        elif visible:
            a['receipt'] = reconcile(repo, pr, f['commentId'], visible[-1], thread, a, login)
        elif (a.get('receipt') or {}).get('sent'):
            # A lost answer may land late: posting again could duplicate it.
            a['receipt'] = {**a['receipt'], 'error': 'sent earlier and not visible yet; reconcile by hand'}
        elif live_key(thread, login, known) != d['key']:
            a['receipt'] = {'sent': False, 'verified': None, 'error': 'the thread changed since it was judged; review again'}
        elif a['digest'] != ledger_digest(a['body']):
            a['receipt'] = {'sent': False, 'verified': None, 'error': 'the stored answer no longer matches its digest'}
        else:
            # Stored before sending, so a run that dies mid-send is reconciled, never resent.
            a['receipt'] = {'sent': True, 'posted': None, 'verified': None, 'resolved': None, 'error': 'sent; not confirmed'}
            send.append((fid, {'commentId': f['commentId'], 'body': a['body'], 'resolve': a['resolve']}))
            continue
        out.append({'findingId': fid, 'done': answered(a), **a['receipt']})
    if send:
        persist()
    got = {rc.get('commentId'): rc for rc in run_reply(repo, pr, [r for _, r in send])} if send else {}
    for fid, r in send:
        a = todo[fid][1]['answer']
        a['receipt'] = got.get(r['commentId']) or {**a['receipt'], 'error': 'reply.py returned no receipt for it'}
        out.append({'findingId': fid, 'done': answered(a), **a['receipt']})
    return out


def post_replies(repo, pr, review, own):
    replies = review['draft']['replies']
    if not replies:
        return []
    foreign = [r['commentId'] for r in replies if r['commentId'] not in own]
    if foreign:
        return [{'commentId': c, 'sent': False, 'verified': None, 'error': 'not a comment our reviews posted; not answered'}
                for c in foreign]
    return run_reply(repo, pr, replies)


def collect(argv):
    p = Parser(prog='post.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--expected-head', required=True)
    p.add_argument('--repo')
    p.add_argument('--event', choices=['COMMENT'])
    p.add_argument('--review-only', action='store_true')
    p.add_argument('--auto', action='store_true')
    p.add_argument('--decline', action='store_true')
    p.add_argument('--reason')
    p.add_argument('--threads', action='store_true')
    p.add_argument('--approve')
    a = p.parse_args(argv)
    if a.approve and not a.threads:
        raise Unusable('--approve names thread answers: it goes with --threads')
    if a.threads and (a.auto or a.decline or a.event or a.review_only or not a.approve):
        raise Unusable('--threads takes only --approve ID[,ID...]: thread answers are posted on the human\'s approval, never under --auto')
    if not FULL_SHA.match(a.expected_head):
        raise Unusable('--expected-head must be a full SHA')
    if a.decline and not a.reason:
        raise Unusable('--decline needs --reason')
    repo = repo_of(a.repo)
    path = ledger_path(repo, a.pr)
    with locked(path):
        led = load(path, repo, a.pr)
        if a.threads:
            require_head(repo, a.pr, a.expected_head, 'the answers judged')
            answers = post_answers(repo, a.pr, led, a.expected_head, [i for i in a.approve.split(',') if i],
                                   lambda: store(path, led))
            store(path, led)
            ok = all(r['done'] for r in answers)
            return {'status': 'posted' if ok else 'partial' if any(r.get('verified') for r in answers) else 'uncertain',
                    'answers': answers}
        todo = [r for r in led['reviews'] if r['head'] == a.expected_head and r['status'] in ('pending', 'partial', 'uncertain')]
        if not todo:
            raise Unusable(f'no pending draft for {a.expected_head} on the ledger')
        review = todo[-1]
        if a.decline and review['status'] != 'pending':
            raise Unusable(f"the draft is {review['status']}: part of it may be on GitHub; reconcile it, do not decline it")
        if a.decline:
            review.update(status='declined', declineReason=a.reason)
            store(path, led)
            return {'status': 'declined', 'review': None, 'replies': []}
        event = a.event or review['draft']['event']
        if a.auto and event == 'APPROVE':
            raise Unusable('auto-post never approves; post it by hand or with --event COMMENT')
        rec = review['receipts']
        sent = bool((rec.get('review') or {}).get('sent')) and not (rec.get('review') or {}).get('verified')
        head = pr_head(repo, a.pr)
        # After a push only a sent-unverified review is touched, and only to find it by its marker: never POSTed again.
        recovering = head != a.expected_head
        if recovering and not sent:
            raise Unusable(f'the PR head moved to {head}; the draft reviewed {a.expected_head}')
        login = gh_json('api', 'user')['login']
        if not (rec.get('review') or {}).get('verified'):
            def persist(intent):
                rec['review'] = intent
                store(path, led)
            got = post_review(repo, a.pr, review, event, login, sent or recovering, persist)
            # Once sent, always sent: no later receipt may clear it, or a run would POST again.
            rec['review'] = {**got, 'sent': got['sent'] or sent}
            store(path, led)
        if rec['review'].get('verified'):
            ids = dict(zip((c.get('findingId') for c in review['draft']['comments']), rec['review']['commentIds']))
            for f in review['findings']:
                if f['id'] in ids:
                    f['commentId'] = ids[f['id']]
        replies = rec.get('replies', [])
        if rec['review'].get('verified') and not a.review_only and not recovering:
            replies = post_replies(repo, a.pr, review, own_comments(led))
            rec['replies'] = replies
        ok = rec['review'].get('verified') is True
        all_replies = a.review_only or all(r.get('verified') and r.get('resolved', True) is not False for r in replies)
        review['status'] = 'posted' if ok and all_replies else 'partial' if ok else 'uncertain'
        store(path, led)
        return {'status': review['status'], 'review': rec['review'], 'replies': replies}


def main(argv):
    try:
        out = collect(argv)
    except Unusable as e:
        print(json.dumps({'error': str(e)}))
        return 2
    print(json.dumps(out))
    return 0 if out['status'] in ('posted', 'declined') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
