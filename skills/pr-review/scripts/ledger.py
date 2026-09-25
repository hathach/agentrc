#!/usr/bin/env python3
"""pr-review's per-PR ledger: what each review of a PR found, drafted and posted.

  ledger.py show --pr N [--repo OWNER/NAME] [--finding ID | --draft]
  ledger.py save --pr N --output FILE [--repo OWNER/NAME] [--reason TEXT]
  ledger.py disputes --pr N --threads FILE --head SHA [--repo OWNER/NAME]

The ledger is <git common dir>/agentrc/pr-review/<owner>__<name>/<N>.json,
one per PR, shared by every worktree of the clone. Only this script and
post.py write it, under an exclusive lock, by write-then-rename.

show prints the last review that reached the PR (posted or partial:
head, mergeBase, verdict, status), its standing findings (open,
upheld, disputed: id, file, line, severity, claim cut short) and the thread
answers drafted and not yet posted; --finding prints one finding's whole
record from those reviews; --draft prints the newest review's saved draft as it
would be posted (event, body, anchored inline comments, fix notes), with its
status and digest.

disputes joins a threads.py snapshot to the findings whose inline comment our
own verified review posted (commentId, recorded by post.py from its read-back):
on each such thread, the replies by anyone but us after our last comment there
are pushback to judge. Its key is the ordered reply ids and body digests; a key
already judged on this head is not listed again, and an edited reply is a new
key. Only ids, digests and authors are printed, never bodies.

A `discussion` result (same head, pushback only) posts no new review: save
merges its dispute records and statuses into the findings of the last review
of that head instead of appending one. save reads a pr-review Workflow output file ({"result": ...}),
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

VERSION = 1
CUT = 160
OPEN = ('open', 'upheld', 'disputed')


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
UNSETTLED = ('pending', 'partial', 'uncertain')


def reached(led):
    """The reviews whose POST was verified on the PR: only their findings are the contributor's to answer."""
    return [r for r in led['reviews'] if r['status'] in REACHED]


def refuse_unsettled(led, head, statuses=UNSETTLED):
    got = [r for r in led['reviews'] if r['head'] == head and r['status'] in statuses]
    if got:
        raise Unusable(f"a {got[-1]['status']} draft {got[-1]['draft']['digest']} for {head} is on the ledger: "
                       'post, reconcile or decline it first')


def last(led):
    return (reached(led) or [None])[-1]


def cut(text):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= CUT else text[:CUT] + '…'


def show(led, finding=None, draft=False):
    if draft:
        if not led['reviews']:
            raise Unusable('no review on the ledger')
        rev = led['reviews'][-1]
        return {'head': rev['head'], 'status': rev['status'], 'draft': rev['draft']}
    rev = last(led)
    if finding:
        for r in reversed(reached(led)):
            for f in r.get('findings', []):
                if f['id'] == finding:
                    return {'finding': f, 'reviewHead': r['head']}
        raise Unusable(f'no finding {finding} on the ledger')
    if not rev:
        return {'reviews': 0, 'last': None, 'open': []}
    answers = [{'findingId': f['id'], 'commentId': f.get('commentId'), 'state': d['state'], 'resolve': d['answer']['resolve'],
                'body': d['answer']['body']}
               for f in rev.get('findings', []) for d in f.get('disputes', [])[-1:]
               if d.get('answer') and not (d['answer'].get('receipt') or {}).get('verified')]
    return {
        'reviews': len(led['reviews']),
        'last': {k: rev.get(k) for k in ('head', 'mergeBase', 'mode', 'status', 'reviewedAt')} | {'event': rev['verdict']['event']},
        'answers': answers,
        'open': [{'id': f['id'], 'status': f['status'], 'file': f['file'], 'line': f['line'], 'severity': f.get('severity'),
                  'claim': cut(f['why']), 'commentId': f.get('commentId')}
                 for f in rev.get('findings', []) if f['status'] in OPEN],
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
    return {**draft, 'body': body, 'comments': inline}


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
            f = {**prior[f['id']], **f, 'disputes': prior[f['id']].get('disputes', []) + f.get('disputes', [])}
        else:
            k += 1
            f = {**f, 'id': f"pr{led['pr']}-f{k}"}
        out.append(f)
    return out


def answered_ids(led):
    """Our replies that a verified receipt records: the only comments of ours that settle pushback."""
    ids = set()
    for r in led['reviews']:
        ids |= {x.get('replyId') for x in (r.get('receipts') or {}).get('replies') or [] if x.get('verified')}
        for f in r['findings']:
            for d in f.get('disputes') or []:
                rc = (d.get('answer') or {}).get('receipt') or {}
                if rc.get('verified'):
                    ids.add(rc.get('replyId'))
    ids.discard(None)
    return ids


def after_ours(replies, answered, mine):
    """The replies to a thread's root after our last verified answer, ours excluded: the pushback still to answer."""
    last = max((k for k, c in enumerate(replies) if answered(c)), default=-1)
    return [c for c in replies[last + 1:] if not mine(c)]


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
        replies = after_ours(thread[1:], lambda c: c['id'] in known, lambda c: c['author'] == viewer)
        if not replies:
            continue
        key = digest([[c['id'], c['digest']] for c in replies])
        if any(d['key'] == key and d.get('judgedHead') == head for d in f.get('disputes', [])):
            continue
        out.append({'findingId': f['id'], 'threadId': t['threadId'], 'rootCommentId': f['commentId'], 'key': key,
                    'outdated': t['outdated'], 'resolved': t['resolved'],
                    'replies': [{'id': c['id'], 'digest': c['digest'], 'author': c['author'], 'bot': c['bot']} for c in replies]})
    return out


def merge_discussion(led, result):
    rev = last(led)
    if not rev or rev['head'] != result['head']:
        raise Unusable(f"a discussion result is for the last reviewed head, and {result['head']} is not it")
    by_id = {f['id']: f for f in rev['findings']}
    for f in result['findings']:
        old = by_id.get(f.get('id'))
        if old is None:
            raise Unusable(f"the discussion result names {f.get('id')}, not a finding of the last review")
        old.update({k: v for k, v in f.items() if k != 'disputes'})
        old['disputes'] = old.get('disputes', []) + f.get('disputes', [])
    return {'saved': True, 'head': result['head'], 'mode': 'discussion', 'findings': len(result['findings']),
            'answers': sum(bool(d.get('answer')) for f in result['findings'] for d in f.get('disputes', []))}


def with_answer_digests(result):
    """Each drafted thread answer gets its digest before anything can post it."""
    for f in result['findings']:
        for d in f.get('disputes', []):
            if d.get('answer'):
                d['answer'] = {**d['answer'], 'digest': digest(d['answer']['body']), 'receipt': None}
    return result


def save(led, result, reason):
    for key in ('pr', 'head', 'mergeBase', 'verdict', 'findings', 'draft'):
        if key not in result:
            raise Unusable(f'the output result has no {key}: not a pr-review result')
    if result['pr'] != led['pr']:
        raise Unusable(f"the output reviews PR {result['pr']}, the ledger is for {led['pr']}")
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
              'reason': reason, 'reviewedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    led['reviews'].append(review)
    return {'saved': True, 'head': result['head'], 'draftDigest': draft['digest'], 'event': draft['event'],
            'inline': len(draft['comments']), 'moved': len(result['draft']['comments']) - len(draft['comments']),
            'replies': len(draft['replies']), 'findings': len(review['findings'])}


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
    sub.choices['disputes'].add_argument('--threads', required=True)
    sub.choices['disputes'].add_argument('--head', required=True)
    sub.choices['save'].add_argument('--output', required=True)
    sub.choices['save'].add_argument('--reason')
    a = p.parse_args(argv)
    repo = repo_of(a.repo)
    path = ledger_path(repo, a.pr)
    if a.cmd == 'show':
        return {'ledger': str(path), **show(load(path, repo, a.pr), a.finding, a.draft)}
    if a.cmd == 'disputes':
        if not FULL_SHA.match(a.head):
            raise Unusable('--head must be a full SHA')
        try:
            snapshot = json.loads(Path(a.threads).read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise Unusable(f'--threads {a.threads} unreadable: {e}')
        if snapshot.get('pr') != a.pr:
            raise Unusable(f"--threads is a snapshot of PR {snapshot.get('pr')}, not {a.pr}")
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
