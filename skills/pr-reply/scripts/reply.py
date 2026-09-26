#!/usr/bin/env python3
"""Post PR review replies from a manifest, read each back, resolve its thread.

  reply.py --pr N --manifest FILE [--repo OWNER/NAME]
  reply.py --pr N --inspect COMMENT:REPLY [COMMENT:REPLY ...] [--repo OWNER/NAME]
  reply.py --pr N --reuse FILE [--repo OWNER/NAME]

FILE: {"replies": [{"commentId": <int>, "body": "<text>", "digest": "<fnv1a>", "resolve": <bool>}, ...]},
digest being the caller's FNV-1a (32-bit, over code points, 8 hex) of the body,
which the body must match before anything is posted; resolve (default true)
false leaves a review reply's thread open, for an answer that upholds a point. Each commentId names one
of three things on the PR: a review comment (an inline thread), an issue comment,
or a review whose body carries the finding (a bot's summary, or a point GitHub
would not anchor inline). A review reply is read back and must match the body,
the parent, our login and the PR before its thread is resolved; the other two
have no thread: the reply is an issue comment whose body is the original's URL
as a quote line plus the text. A reply of ours with the identical body already
there is reused, never posted twice. That thread is never resolved, so a run
that lost its state would answer it again in new words: a reply of ours quoting
it with the same kind of answer (a fix note, which starts with "Fixed in ", or
anything else) in other words is not posted over and not verified; its receipt
names it with verified false, for a human to reconcile. Nothing is ever edited
or deleted.

stdout ends with one JSON line {"receipts": [{"commentId", "kind", "replyId",
"digest", "sent", "posted", "verified", "resolved", "error"}]}: kind is
"review", "issue" or "review-body", "none" when all three were searched and the
id is on none of them (the caller owes it nothing), null when a lookup failed
before that was known; sent says a POST was issued (a lost response leaves sent true and replyId null: the
reply may exist), posted that GitHub answered it, verified is true on a
matching read-back, false on a mismatch and null when the read-back could not
be fetched. `reply.py --digest TEXT` prints TEXT's digest for a manifest
written by hand. Exit 0 when every reply is verified and, for a review
reply, resolved; 1 otherwise; 2 on a usage or manifest error. A null resolved or
error is left out of the line: a model relaying it drops a trailing null.

--inspect reads, never writes: for each pair, whether REPLY is ours answering
COMMENT on this PR, its exact body with the body's digest, and the original's
digest as pr-babysit's harvest.py computes it (sha256, 12 hex). stdout ends with
{"inspected": [{"commentId", "replyId", "kind", "body", "bodyDigest",
"originalDigest", "error"}]}; body is null when error is set. Exit 0 when every
pair was read and is ours, 1 otherwise.

--reuse settles a comment on a reply already there, and never posts: FILE is
{"reuses": [{"commentId", "replyId", "bodyDigest", "originalDigest"}]}, the
digests an inspection returned. Each pair is read again, and only a reply still
ours, still that body, on a comment still that body, counts as verified; its
review thread is then resolved. Receipts as above, sent and posted false, digest
being the reply body's as read now.
"""

import argparse
import hashlib
import json
import subprocess
import sys

THREADS_QUERY = ('query($o:String!,$r:String!,$p:Int!,$c:String){repository(owner:$o,name:$r){'
                 'pullRequest(number:$p){reviewThreads(first:100,after:$c){pageInfo{hasNextPage endCursor}'
                 'nodes{id isResolved comments(first:50){nodes{databaseId}}}}}}}')
RESOLVE_MUTATION = 'mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}'


class ApiError(Exception):
    pass


def fnv1a(text):
    """The same checksum pr-babysit computes over a body it hands out."""
    h = 0x811c9dc5
    for ch in text:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xffffffff
    return f'{h:08x}'


def gh(args, stdin=None):
    r = subprocess.run(['gh', *args], input=stdin, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def api(method, path, body=None, paginate=False):
    """One REST call through gh; a failure becomes ApiError carrying gh's diagnostic."""
    args = ['api', '-X', method, path]
    if paginate:
        args += ['--paginate', '--slurp']
    stdin = None
    if body is not None:
        args += ['--input', '-']
        stdin = json.dumps(body)
    rc, out, err = gh(args, stdin)
    if rc != 0:
        raise ApiError(err.strip() or out.strip())
    if not out.strip():
        return None
    data = json.loads(out)
    if paginate:
        return [x for page in data for x in page]
    return data


def graphql(query, variables):
    """-f keeps a string a string (a repo named 123 must not become a number);
    -F is for the integer PR number."""
    args = ['api', 'graphql', '-f', f'query={query}']
    for k, v in variables.items():
        args += ['-F' if isinstance(v, int) else '-f', f'{k}={v}']
    rc, out, err = gh(args)
    if rc != 0:
        raise ApiError(err.strip() or out.strip())
    return json.loads(out)


class Poster:
    def __init__(self, repo, pr):
        self.repo, self.pr = repo, pr
        self.me = api('GET', 'user')['login']
        self._review = None
        self._issue = None
        self._reviews = None

    def review_comments(self):
        if self._review is None:
            self._review = api('GET', f'repos/{self.repo}/pulls/{self.pr}/comments?per_page=100', paginate=True)
        return self._review

    def issue_comments(self):
        if self._issue is None:
            self._issue = api('GET', f'repos/{self.repo}/issues/{self.pr}/comments?per_page=100', paginate=True)
        return self._issue

    def reviews(self):
        if self._reviews is None:
            self._reviews = api('GET', f'repos/{self.repo}/pulls/{self.pr}/reviews?per_page=100', paginate=True)
        return self._reviews

    def kind_of(self, comment_id):
        """('review', comment) for an inline review comment on this PR, ('issue',
        comment) for an issue comment on it, ('review-body', review) for a review
        whose body is the target, ('none', None) when all three were searched and
        none has the id; ApiError when a lookup failed or the id is ambiguous."""
        found = [(kind, c) for kind, pool in (('review', self.review_comments()), ('issue', self.issue_comments()),
                                              ('review-body', self.reviews()))
                 for c in pool if c['id'] == comment_id]
        if len(found) > 1:
            raise ApiError(f'id {comment_id} is ambiguous on PR #{self.pr}: {", ".join(k for k, _ in found)}')
        return found[0] if found else ('none', None)

    def existing(self, kind, comment_id, body):
        """The (id, body) of our identical reply, else of our quoting reply giving the same kind of answer, or None."""
        if kind == 'review':
            return next(((c['id'], c['body']) for c in self.review_comments() if c['user']['login'] == self.me
                         and c.get('in_reply_to_id') == comment_id and c['body'] == body), None)
        quote = body.partition('\n\n')[0] + '\n\n'
        ours = [c for c in self.issue_comments() if c['user']['login'] == self.me and c['body'].startswith(quote)]
        same = [c for c in ours if c['body'] == body] or [c for c in ours if is_fix_note(c['body']) == is_fix_note(body)]
        return (same[0]['id'], same[0]['body']) if same else None

    def post(self, kind, comment_id, body):
        if kind == 'review':
            c = api('POST', f'repos/{self.repo}/pulls/{self.pr}/comments/{comment_id}/replies', {'body': body})
        else:
            c = api('POST', f'repos/{self.repo}/issues/{self.pr}/comments', {'body': body})
        return c['id']

    def read_original(self, kind, comment_id):
        """The comment as it stands now, not as the cached listing had it."""
        if kind == 'review-body':
            return api('GET', f'repos/{self.repo}/pulls/{self.pr}/reviews/{comment_id}')
        return self.read_reply(kind, comment_id)

    def read_reply(self, kind, reply_id):
        return api('GET', f'repos/{self.repo}/{"pulls" if kind == "review" else "issues"}/comments/{reply_id}')

    def misplaced(self, kind, comment_id, c):
        """What makes reply c not ours answering comment_id on this PR, by name."""
        if kind == 'review':
            checks = [('parent', c.get('in_reply_to_id') == comment_id),
                      ('author', c.get('user', {}).get('login') == self.me),
                      ('pr', str(c.get('pull_request_url', '')).endswith(f'/pulls/{self.pr}'))]
        else:
            checks = [('author', c.get('user', {}).get('login') == self.me),
                      ('pr', str(c.get('issue_url', '')).endswith(f'/issues/{self.pr}'))]
        return [name for name, ok in checks if not ok]

    def verify(self, kind, comment_id, reply_id, body):
        """(True, None) on a matching read-back, (False, why) on a mismatch,
        (None, why) when the reply could not be fetched."""
        try:
            c = self.read_reply(kind, reply_id)
        except ApiError as e:
            return None, f'read-back unavailable: {e}'
        bad = ([] if c.get('body') == body else ['body']) + self.misplaced(kind, comment_id, c)
        return (True, None) if not bad else (False, f'read-back mismatch on {", ".join(bad)}')

    def resolve(self, comment_id):
        owner, name = self.repo.split('/', 1)
        cursor = None
        while True:
            v = {'o': owner, 'r': name, 'p': self.pr}
            if cursor:
                v['c'] = cursor
            page = graphql(THREADS_QUERY, v)['data']['repository']['pullRequest']['reviewThreads']
            for t in page['nodes']:
                if any(c['databaseId'] == comment_id for c in t['comments']['nodes']):
                    if t['isResolved']:
                        return None
                    r = graphql(RESOLVE_MUTATION, {'id': t['id']})
                    ok = r.get('data', {}).get('resolveReviewThread', {}).get('thread', {}).get('isResolved')
                    return None if ok else 'resolve mutation did not report isResolved'
            if not page['pageInfo']['hasNextPage']:
                return f'no review thread contains comment {comment_id}'
            cursor = page['pageInfo']['endCursor']


def issue_body(original, body):
    return f"> {original['html_url']}\n\n{body}"


def is_fix_note(body):
    """pr-babysit's note for a landed fix, a different answer from a refutation of the same comment."""
    return body.partition('\n\n')[2].startswith('Fixed in ')


def comment_digest(body):
    """harvest.py's digest of a reviewer's comment: sha256 of its body, 12 hex."""
    return hashlib.sha256((body or '').encode()).hexdigest()[:12]


def read_pair(poster, kind, original, comment_id, reply_id):
    """(reply, why): why names what makes the reply not ours answering that
    comment, None when it is."""
    if kind == 'none':
        return None, f'comment {comment_id} is not on PR #{poster.pr}'
    c = poster.read_reply(kind, reply_id)
    bad = poster.misplaced(kind, comment_id, c)
    if kind != 'review' and not str(c.get('body', '')).startswith(issue_body(original, '')):
        bad.append('quote')
    return c, f'reply {reply_id} is not ours on comment {comment_id}: mismatch on {", ".join(bad)}' if bad else None


def inspect(poster, comment_id, reply_id):
    out = {'commentId': comment_id, 'replyId': reply_id, 'kind': None, 'body': None, 'bodyDigest': None,
           'originalDigest': None, 'error': None}
    try:
        kind, original = poster.kind_of(comment_id)
        out['kind'] = kind
        if original is not None:
            out['originalDigest'] = comment_digest(original.get('body'))
        c, why = read_pair(poster, kind, original, comment_id, reply_id)
        if why:
            out['error'] = why
        else:
            out['body'], out['bodyDigest'] = c['body'], fnv1a(c['body'])
    except ApiError as e:
        out['error'] = str(e)
    return out


def reuse(poster, item):
    rc = {'commentId': item['commentId'], 'kind': None, 'replyId': item['replyId'], 'digest': item['bodyDigest'],
          'sent': False, 'posted': False, 'verified': False, 'resolved': None, 'error': None}
    try:
        kind, _ = poster.kind_of(item['commentId'])
        rc['kind'] = kind
        # Afresh for each entry: settling an earlier one may have moved this one.
        original = poster.read_original(kind, item['commentId']) if kind != 'none' else None
        c, why = read_pair(poster, kind, original, item['commentId'], item['replyId'])
        if c is not None:
            rc['digest'] = fnv1a(c.get('body', ''))
        if not why and comment_digest(original.get('body')) != item['originalDigest']:
            why = f'comment {item["commentId"]} was edited since the inspection'
        if not why and rc['digest'] != item['bodyDigest']:
            why = f'reply {item["replyId"]} was edited since the inspection'
        if why:
            rc['error'] = why
            return rc
        rc['verified'] = True
        if kind == 'review':
            rc['error'] = poster.resolve(item['commentId'])
            rc['resolved'] = rc['error'] is None
    except ApiError as e:
        # Once the comment is found, a failed read leaves the reply unknown; a
        # failed resolve leaves it verified and the thread open.
        if not rc['verified']:
            rc['verified'] = None if rc['kind'] is not None else False
        rc['error'] = str(e)
    return rc


def handle(poster, item):
    rc = {'commentId': item['commentId'], 'kind': None, 'replyId': None, 'digest': fnv1a(item['body']),
          'sent': False, 'posted': False, 'verified': False, 'resolved': None, 'error': None}
    if item['digest'] != rc['digest']:
        rc['error'] = 'manifest body does not match its digest'
        return rc
    try:
        kind, original = poster.kind_of(item['commentId'])
        rc['kind'] = kind
        if kind == 'none':
            rc['error'] = f'comment {item["commentId"]} is not on PR #{poster.pr}'
            return rc
        body = item['body'] if kind == 'review' else issue_body(original, item['body'])
        found = poster.existing(kind, item['commentId'], body)
        if found and found[1] != body:
            rc['replyId'] = found[0]
            rc['error'] = f'reply {found[0]} of ours already answers this in other words; reconcile by hand'
            return rc
        if found:
            reply_id = found[0]
        else:
            rc['sent'] = True
            reply_id = poster.post(kind, item['commentId'], body)
            rc['posted'] = True
        rc['replyId'] = reply_id
        rc['verified'], rc['error'] = poster.verify(kind, item['commentId'], reply_id, body)
        if rc['verified'] and kind == 'review' and item.get('resolve', True):
            rc['error'] = poster.resolve(item['commentId'])
            rc['resolved'] = rc['error'] is None
    except ApiError as e:
        rc['error'] = str(e)
    return rc


def load_entries(path, key, what, check):
    """The non-empty `key` list of the JSON file at path, each entry passing check, one per commentId."""
    with open(path) as f:
        m = json.load(f)
    entries = m.get(key) if isinstance(m, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f'{what} needs a non-empty "{key}" list')
    seen = set()
    for r in entries:
        if not isinstance(r, dict) or not isinstance(r.get('commentId'), int):
            raise ValueError(f'bad {what} entry: {r!r}')
        check(r)
        if r['commentId'] in seen:
            raise ValueError(f'commentId {r["commentId"]} listed twice')
        seen.add(r['commentId'])
    return entries


def check_reply(r):
    if not isinstance(r.get('body'), str) or not r['body'].strip():
        raise ValueError(f'bad manifest entry: {r!r}')
    if not isinstance(r.get('digest'), str):
        raise ValueError(f'manifest entry without a digest: {r!r}')
    if not isinstance(r.get('resolve', True), bool):
        raise ValueError(f'manifest entry resolve must be true or false: {r!r}')


def check_reuse(r):
    if not (isinstance(r.get('replyId'), int) and isinstance(r.get('bodyDigest'), str) and isinstance(r.get('originalDigest'), str)):
        raise ValueError(f'bad reuse file entry: {r!r}')


def pair(text):
    comment, sep, reply_id = text.partition(':')
    if not (sep and comment.isdigit() and reply_id.isdigit()):
        raise argparse.ArgumentTypeError(f'expected COMMENT:REPLY ids, got {text!r}')
    return int(comment), int(reply_id)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pr', type=int)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--manifest')
    mode.add_argument('--inspect', nargs='+', type=pair, metavar='COMMENT:REPLY')
    mode.add_argument('--reuse', metavar='FILE')
    p.add_argument('--repo', help='OWNER/NAME (default: gh repo view)')
    p.add_argument('--digest', metavar='TEXT', help='print the digest of TEXT and exit')
    a = p.parse_args(argv)
    if a.digest is not None:
        print(fnv1a(a.digest))
        return 0
    if a.pr is None or (a.manifest, a.inspect, a.reuse) == (None, None, None):
        p.error('--pr and one of --manifest, --inspect or --reuse are required')
    try:
        replies = load_entries(a.manifest, 'replies', 'manifest', check_reply) if a.manifest else None
        reuses = load_entries(a.reuse, 'reuses', 'reuse file', check_reuse) if a.reuse else None
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f'reply.py: {e}', file=sys.stderr)
        return 2
    repo = a.repo
    if not repo:
        rc, out, err = gh(['repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'])
        if rc != 0:
            print(f'reply.py: {err.strip()}', file=sys.stderr)
            return 2
        repo = out.strip()
    try:
        poster = Poster(repo, a.pr)
    except ApiError as e:
        print(f'reply.py: {e}', file=sys.stderr)
        return 2
    if a.inspect:
        inspected = [inspect(poster, c, r) for c, r in a.inspect]
        print(json.dumps({'inspected': inspected}))
        return 0 if all(i['error'] is None for i in inspected) else 1
    receipts = [reuse(poster, item) for item in reuses] if reuses else [handle(poster, item) for item in replies]
    print(json.dumps({'receipts': [{k: v for k, v in r.items() if v is not None or k not in ('resolved', 'error')}
                                   for r in receipts]}))
    kept_open = {item['commentId'] for item in replies or [] if item.get('resolve') is False}
    ok = all(r['verified'] is True and (r['kind'] != 'review' or r['resolved'] or r['commentId'] in kept_open) for r in receipts)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
