#!/usr/bin/env python3
"""pr-review's per-PR ledger: what each review of a PR found, drafted and posted.

  ledger.py show --pr N [--repo OWNER/NAME] [--finding ID | --draft | --pending | --threads FILE]
  ledger.py save --pr N --output FILE [--repo OWNER/NAME] [--reason TEXT]
  ledger.py disputes --pr N --threads FILE --head SHA [--repo OWNER/NAME]

The ledger is <git common dir>/agentrc/pr-review/<owner>__<name>/<N>.json,
one per PR, shared by every worktree of the clone. Only this script and
post.py write it, under an exclusive lock, by write-then-rename.

show prints the last review that reached the PR (posted or partial:
head, mergeBase, verdict, status), its standing findings (open, upheld,
disputed: id, file, line, severity with its P0-P4 priority, the grading
behind it, its claim, and the number of the defect it shares with other findings
of that review, if any), ordered by
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
resolve_due()), and in heldThreads those it never resolves again by itself.

disputes joins a threads.py snapshot to the findings whose inline comment our
own verified review posted (commentId, recorded by post.py from its read-back):
on each such thread, the replies by anyone but us or a bot after our last
verified answer there are pushback to judge (pushback()). Its key is the ordered reply ids and body digests; a key
already judged on this head is not listed again, and an edited reply is a new
key. Only ids, digests and authors are printed, never bodies.

A `discussion` result (same head, pushback only) posts no new review: save
merges its dispute records and statuses into the findings of the last review
of that head, and appends to that review an answer publication (its own pending
review, no findings) that post.py publishes the new answers through. save reads a pr-review Workflow output file ({"result": ...}),
checks it is a result for this PR, and appends it as a review whose draft is
`pending`: each comment anchored on a line the PR diff adds or keeps, else
moved into the body, and the draft's digest fixed before anything is posted.
A pending draft already on the ledger for that head is refused: post it,
decline it, or say why a second review is due (--reason).

stdout ends with one JSON line; {"error": ...} and exit 2 when the ledger or
the output cannot be read, or they disagree.
"""

import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import FULL_SHA, Parser, Unusable, git, report, run  # noqa: E402
from launch_result import load_output  # noqa: E402

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
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
            raise Unusable(f'--repo must be OWNER/NAME, not {repo!r}')
        return repo
    return run('gh', 'repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner')[1].strip()


def ledger_dir(repo):
    common = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
    return common / 'agentrc' / 'pr-review' / repo.replace('/', '__')


def ledger_path(repo, pr):
    return ledger_dir(repo) / f'{pr}.json'


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


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'w') as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield


def store(path, led):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(led, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]


# An uncertain review may not be on the PR at all: it is reconciled before anything builds on it.
REACHED = ('posted', 'partial')
UNSETTLED = ('pending', 'drafted', 'partial', 'uncertain')
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
                       'post, reconcile or decline it first')


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


def resolve_due(led, snapshot):
    """({finding id: {replied}}, held) for the last review's settled findings whose thread the snapshot shows open.
    Due: fixed, or closed by a reply of ours since the finding last stood (replied: a published reply that resolves,
    or a fix note the human edited, still in the thread). Held, never resolved again by itself: one whose last resolve is unconfirmed
    (sent) or that someone reopened after it was resolved."""
    rev = last(led)
    if not rev:
        return {}, []
    roots = {t['commentIds'][0]: t for t in snapshot.get('threads', []) if t.get('commentIds')}
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
    for f in rev.get('findings', []):
        t = roots.get(f.get('commentId'))
        if f['status'] in OPEN or not t or t['resolved']:
            continue
        if mark.get(f['id']) in ('sent', 'resolved', 'observed'):
            held.append({'findingId': f['id'], 'commentId': f['commentId'],
                         'why': 'resolve unconfirmed' if mark[f['id']] == 'sent' else 'reopened after it was resolved'})
        elif replied.get(f['id']) or f['status'] == 'fixed':
            due[f['id']] = {'replied': bool(replied.get(f['id']))}
    return due, held


def open_row(f, due):
    sev = level(f.get('severity'))
    return {'id': f['id'], 'status': f['status'], 'file': f['file'], 'line': f['line'],
            'severity': sev, 'priority': priority(sev), 'confidence': f.get('confidence'),
            'impact': f.get('impact'), 'severityReason': f.get('severityReason'),
            'why': f['why'], 'commentId': f.get('commentId'), 'defect': f.get('defect'),
            'resolveDue': due.get(f['id'])}


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
    snapshots = {}
    answers = [{'findingId': f['id'], 'commentId': f.get('commentId'), 'state': d['state'], 'resolve': d['answer']['resolve'],
                'body': d['answer']['body'], 'reason': d.get('reason'),
                **thread_context(d, f.get('commentId'), snapshots)}
               for f, d in open_disputes(rev.get('findings', []), rev['head'])]
    return {
        'reviews': len(led['reviews']),
        'last': {k: rev.get(k) for k in ('head', 'mergeBase', 'mode', 'status', 'reviewedAt')} | {'event': rev['verdict']['event']},
        'answers': answers,
        'open': sorted((open_row(f, due) for f in rev.get('findings', []) if f['status'] in OPEN or f['id'] in due), key=by_severity),
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
    """Comments on lines the diff does not show go into the body: GitHub refuses them inline."""
    inline, moved, cache = [], [], {}
    for c in draft['comments']:
        ok = cache.setdefault(c['path'], added_lines(merge_base, head, c['path']))
        (inline if c['line'] in ok else moved).append(c)
    body = draft['body']
    if moved:
        body += '\n\n' + '\n\n'.join(f"**{c['path']}:{c['line']}**: {c['body']}" for c in moved)
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
    ids.discard(None)
    return ids


def pushback(replies, answered, viewer):
    """threads.py records replying to a root, after our last verified answer (an id in `answered`), neither ours
    nor a bot's: the pushback still to answer."""
    last = max((k for k, c in enumerate(replies) if c['id'] in answered), default=-1)
    return [c for c in replies[last + 1:] if c['author'] != viewer and not c['bot']]


def disputes(led, snapshot, head):
    rev = last(led)
    if not rev:
        return []
    viewer = snapshot.get('viewer')
    if not viewer:
        raise Unusable('the threads file names no viewer: rerun threads.py')
    by_id = {c['id']: c for c in snapshot.get('comments', [])}
    known = answered_ids(led)
    roots = {t['commentIds'][0]: t for t in snapshot.get('threads', []) if t.get('commentIds')}
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


def add_answers(review, head, answers, auto=False):
    """Append to `review` the publication of `answers` ((finding id, answer digest) pairs) to its findings;
    `auto` marks the one an auto-post run keeps with its send intent."""
    pubs = review.setdefault('answerPublications', [])
    draft = {'event': None, 'body': '', 'comments': [], 'replies': []}
    draft['digest'] = digest({'head': head, 'answers': answers, 'n': len(pubs)})
    pubs.append({'head': head, 'answers': [list(a) for a in answers], 'status': 'pending',
                 'draft': draft, 'receipts': {}, 'reviewedAt': now(), **({'auto': True} if auto else {})})


def open_disputes(findings, head=None):
    """(finding, dispute) for each finding whose last dispute holds an answer not yet published or settled, judged
    on `head` when named: an answer to an earlier head is never published on a later one."""
    return [(f, d) for f in findings for d in f.get('disputes', [])[-1:]
            if d.get('answer') and not d['answer'].get('outcome') and head in (None, d.get('judgedHead'))]


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


def merge_discussion(led, result):
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
        add_answers(rev, result['head'], answers)
    return {'saved': True, 'head': result['head'], 'mode': 'discussion', 'findings': len(result['findings']),
            'answers': len(answers)}


def with_answer_digests(result):
    """Each drafted thread answer gets its digest before anything can post it."""
    for f in result['findings']:
        for d in f.get('disputes', []):
            if d.get('answer'):
                d['answer'] = {**d['answer'], 'digest': digest(d['answer']['body'])}
    return result


def save(led, result, reason):
    for key in ('pr', 'head', 'mergeBase', 'verdict', 'findings', 'draft'):
        if key not in result:
            raise Unusable(f'the output result has no {key}: not a pr-review result')
    if result['pr'] != led['pr']:
        raise Unusable(f"the output reviews PR {result['pr']}, the ledger is for {led['pr']}")
    refuse_off_scale(result)
    result = with_answer_digests(result)
    if result.get('mode') == 'discussion':
        return merge_discussion(led, result)
    for key in ('head', 'mergeBase'):
        if not FULL_SHA.match(str(result[key])):
            raise Unusable(f'the output {key} is not a full SHA')
    if result['verdict'].get('event') != result['draft'].get('event'):
        raise Unusable(f"the draft's event {result['draft'].get('event')} is not the verdict's {result['verdict'].get('event')}")
    refuse_unsettled(led, result['head'])
    same = [r for r in led['reviews'] if r['head'] == result['head'] and r['status'] == 'posted']
    if same and not reason:
        raise Unusable(f"{result['head']} was already reviewed and posted; a second review needs --reason")
    if any(str(c.get('path', '')).startswith('/') for c in result['draft']['comments']):
        raise Unusable('a draft comment has an absolute path; GitHub needs repository-relative paths')
    findings = number(led, result)
    try:
        comments = [{'path': c['path'], 'line': c['line'], 'body': c['body'], 'findingId': findings[c['finding']]['id']}
                    for c in result['draft']['comments']]
    except (KeyError, IndexError, TypeError):
        raise Unusable('a draft comment names no finding of the result')
    draft = anchor({**result['draft'], 'comments': comments}, result['mergeBase'], result['head'])
    draft['digest'] = digest({k: draft[k] for k in ('event', 'body', 'comments', 'replies')})
    review = {**result, 'findings': findings, 'draft': draft, 'status': 'pending', 'receipts': {},
              'reason': reason, 'reviewedAt': now()}
    led['reviews'].append(review)
    return {'saved': True, 'head': result['head'], 'draftDigest': draft['digest'], 'event': draft['event'],
            'inline': len(draft['comments']), 'moved': len(result['draft']['comments']) - len(draft['comments']),
            'replies': len(draft['replies']), 'findings': len(review['findings'])}


def read_snapshot(path, pr):
    try:
        snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise Unusable(f'--threads {path} unreadable: {e}')
    if snapshot.get('pr') != pr:
        raise Unusable(f"--threads is a snapshot of PR {snapshot.get('pr')}, not {pr}")
    return snapshot


def collect(argv):
    p = Parser(prog='ledger.py')
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('show', 'save', 'disputes'):
        s = sub.add_parser(name)
        s.add_argument('--pr', type=int, required=True)
        s.add_argument('--repo')
    which = sub.choices['show'].add_mutually_exclusive_group()
    which.add_argument('--finding')
    which.add_argument('--draft', action='store_true')
    which.add_argument('--pending', action='store_true')
    which.add_argument('--threads')
    sub.choices['disputes'].add_argument('--threads', required=True)
    sub.choices['disputes'].add_argument('--head', required=True)
    sub.choices['save'].add_argument('--output', required=True)
    sub.choices['save'].add_argument('--reason')
    a = p.parse_args(argv)
    repo = repo_of(a.repo)
    path = ledger_path(repo, a.pr)
    snapshot = read_snapshot(a.threads, a.pr) if getattr(a, 'threads', None) else None
    if a.cmd == 'show':
        return {'ledger': str(path), **show(load(path, repo, a.pr), a.finding, a.draft, snapshot, a.pending)}
    if a.cmd == 'disputes':
        if not FULL_SHA.match(a.head):
            raise Unusable('--head must be a full SHA')
        return {'disputes': disputes(load(path, repo, a.pr), snapshot, a.head)}
    result = (load_output(a.output) or {}).get('result')
    if not isinstance(result, dict):
        raise Unusable(f'--output {a.output} holds no Workflow result')
    if result.get('status') != 'reviewed':
        raise Unusable(f"the launch ended {result.get('status')!r} ({result.get('reason')}): nothing to save")
    with locked(path):
        led = load(path, repo, a.pr)
        out = save(led, result, a.reason)
        store(path, led)
    return {'ledger': str(path), **out}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
