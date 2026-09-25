#!/usr/bin/env python3
"""Every comment on a PR, human and bot, with its review thread's state.

  threads.py --pr N --out FILE [--repo OWNER/NAME]

Writes FILE: {"viewer": <our login>, "comments": [...], "threads": [...]}:
threads lists each review thread's id, resolved, outdated and comment ids in
thread order (a thread past 100 comments is refused, not cut); comments has one record per inline review comment
(kind `review`), review body (`review-body`, empty bodies skipped) and issue
comment (`issue`): id, author, bot, body verbatim, digest (sha256, 12 hex, as
pr-babysit's harvest.py computes it), path, line, commitId, inReplyTo,
reviewId, threadId, resolved and outdated (the last two null outside a
thread), url, createdAt. A comment is `ours` when it belongs to a review by the
authenticated user whose body carries pr-review's marker: quoting the marker
makes nobody else's comment ours.

stdout ends with one JSON line: the file, its sha256, and counts; the bodies
stay in the file. {"error": ...}, exit 2, when an endpoint cannot be read.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import repo_of  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import Parser, Unusable, report  # noqa: E402
from harvest import digest, gh_json, pages  # noqa: E402

MARKER = '<!-- agentrc-pr-review:'
THREADS = ('query($o:String!,$r:String!,$p:Int!,$c:String){repository(owner:$o,name:$r){pullRequest(number:$p){'
           'reviewThreads(first:100,after:$c){pageInfo{hasNextPage endCursor}'
           'nodes{id isResolved isOutdated comments(first:100){nodes{databaseId}}}}}}}')


def threads(repo, pr):
    """Every review thread: id, resolved, outdated and its comment ids in thread order."""
    owner, name = repo.split('/')
    out, cursor = [], None
    while True:
        argv = ['api', 'graphql', '-f', f'query={THREADS}', '-F', f'o={owner}', '-F', f'r={name}', '-F', f'p={pr}']
        if cursor:
            argv += ['-F', f'c={cursor}']
        page = gh_json(*argv)['data']['repository']['pullRequest']['reviewThreads']
        for t in page['nodes']:
            if len(t['comments']['nodes']) >= 100:
                raise Unusable(f"thread {t['id']} has 100+ comments; this script reads the first 100 only")
            out.append({'threadId': t['id'], 'resolved': t['isResolved'], 'outdated': t['isOutdated'],
                        'commentIds': [c['databaseId'] for c in t['comments']['nodes']]})
        if not page['pageInfo']['hasNextPage']:
            return out
        cursor = page['pageInfo']['endCursor']


def record(kind, c, **extra):
    user = c.get('user') or {}
    return {'kind': kind, 'id': c['id'], 'author': user.get('login'), 'bot': user.get('type') == 'Bot',
            'body': c.get('body') or '', 'digest': digest(c.get('body')), 'path': None, 'line': None,
            'commitId': c.get('commit_id'), 'inReplyTo': None, 'reviewId': None, 'threadId': None,
            'resolved': None, 'outdated': None, 'url': c.get('html_url'),
            'createdAt': c.get('created_at') or c.get('submitted_at'), 'ours': False, **extra}


def collect(argv):
    p = Parser(prog='threads.py')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--repo')
    a = p.parse_args(argv)
    repo = repo_of(a.repo)
    reviews = pages(f'repos/{repo}/pulls/{a.pr}/reviews')
    inline = pages(f'repos/{repo}/pulls/{a.pr}/comments')
    issue = pages(f'repos/{repo}/issues/{a.pr}/comments')
    ordered = threads(repo, a.pr)
    where = {i: (t['threadId'], t['resolved'], t['outdated']) for t in ordered for i in t['commentIds']}
    login = gh_json('api', 'user')['login']
    ours = {r['id'] for r in reviews if MARKER in (r.get('body') or '') and (r.get('user') or {}).get('login') == login}
    out = [record('review-body', r, ours=r['id'] in ours) for r in reviews if (r.get('body') or '').strip()]
    for c in inline:
        tid, resolved, outdated = where.get(c['id'], (None, None, None))
        out.append(record('review', c, path=c.get('path'), line=c.get('line') or c.get('original_line'),
                          inReplyTo=c.get('in_reply_to_id'), reviewId=c.get('pull_request_review_id'),
                          threadId=tid, resolved=resolved, outdated=outdated,
                          ours=c.get('pull_request_review_id') in ours))
    out += [record('issue', c) for c in issue]
    text = json.dumps({'repo': repo, 'pr': a.pr, 'viewer': login, 'comments': out, 'threads': ordered}, indent=1) + '\n'
    Path(a.out).write_text(text, encoding='utf-8')
    kinds = {}
    for c in out:
        kinds[c['kind']] = kinds.get(c['kind'], 0) + 1
    return {'file': a.out, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'count': len(out), 'kinds': kinds,
            'unresolvedThreads': len({c['threadId'] for c in out if c['threadId'] and c['resolved'] is False}),
            'ours': sum(c['ours'] for c in out)}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
