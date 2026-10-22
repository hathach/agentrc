#!/usr/bin/env python3
"""Open one follow-up issue, or add one comment to an open one, and prove it landed.

  publish.py create --repo OWNER/NAME --allow-repo OWNER/NAME [...] --title T --body-file F [--label L ...]
  publish.py comment --repo OWNER/NAME --allow-repo OWNER/NAME [...] --issue N --body-file F

Nothing is sent to a repository outside --allow-repo, with a title or body that
credits an agent or links a Claude or ChatGPT session, or with a label the
repository lacks; the allowlist guards the caller's grant, it grants nothing. The
attribution patterns are pr-babysit's (scripts/facts.py), installed beside it.
create first lists the open issues: ours with the same title and body is the
earlier run's issue, another with the same title is a collision for the caller
to judge, so nothing is created; a listing that fails creates nothing. comment
posts on an open issue unless ours with the same body is there. A created issue
or comment is read back and compared; when the response was lost or unusable
the listing is read again for ours with the same body before anything is said.
Nothing is ever edited, closed or deleted.

A valid invocation's stdout ends with one JSON line {"repo", "action", "issue",
"url", "outcome", "posted", "candidates", "error"}, null fields left out (posted
is null after a lost response): outcome is "verified" (read back as sent, or
ours already there with posted false), "collision" (candidates: [{"issue",
"url", "title"}]), "refused" (nothing sent), "mismatch" (a repair for a human:
the read-back of what this run posted differs, our earlier post lacks what was
asked, or several of ours match) or "uncertain" (sent, and whether it landed is
unknown: for a human, since a lost write can surface after the relisting, so a
rerun could post it twice). Exit 0 on verified, 1 on collision, mismatch or
uncertain, 2 on refused; a usage error exits 2 with argparse's message and no
receipt.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pr-babysit' / 'scripts'))
from facts import attribution_in  # noqa: E402


class ApiError(Exception):
    pass


class Refused(Exception):
    pass


def gh(args, stdin=None):
    # replace: a stray byte must reach the error and read-back paths, not raise past them
    r = subprocess.run(['gh', *args], input=stdin, capture_output=True, encoding='utf-8', errors='replace')
    return r.returncode, r.stdout, r.stderr


def usable(x, need):
    """An object holding the keys in need, whose fields the checks read, when there, have the shape they read."""
    if not isinstance(x, dict) or any(x.get(k) is None for k in need):
        return False
    labels = x.get('labels', [])
    return (isinstance(x.get('user') or {}, dict) and all(isinstance(x.get(k, ''), str) for k in ('title', 'html_url'))
            and isinstance(labels, list)
            and all(isinstance(lb, dict) and isinstance(lb.get('name'), str) for lb in labels))


def api(method, path, body=None, paginate=False, need=()):
    """An object, or a listing's objects, each usable; anything else is an ApiError."""
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
    try:
        data = json.loads(out)
        if paginate and not (isinstance(data, list) and data and all(isinstance(page, list) for page in data)):
            raise ValueError('not a list of pages')   # else {} or [{}] would read as no issues and post again
        items = [x for page in data for x in page] if paginate else [data]
        if not all(usable(x, need) for x in items):
            raise ValueError(f'not objects with {", ".join(need) or "fields"} of the expected shape')
    except (ValueError, TypeError) as e:
        raise ApiError(f'unusable response ({e}): {out.strip()[:200]}')
    return items if paginate else data


class Issues:
    def __init__(self, repo):
        self.repo = repo
        self.me = api('GET', 'user', need=('login',))['login']

    def open_issues(self):
        """Open issues only: the issues endpoint lists pull requests too."""
        items = api('GET', f'repos/{self.repo}/issues?state=open&per_page=100', paginate=True,
                    need=('number', 'html_url', 'title'))
        return [i for i in items if 'pull_request' not in i]

    def issue(self, number):
        return api('GET', f'repos/{self.repo}/issues/{number}', need=('number',))

    def comments(self, number):
        return api('GET', f'repos/{self.repo}/issues/{number}/comments?per_page=100', paginate=True, need=('html_url',))

    def ours(self, item, body):
        return (item.get('user') or {}).get('login') == self.me and item.get('body') == body

    def differs(self, issue, args, body):
        """What keeps issue from being the one args asks for, by name."""
        return [name for name, ok in (('title', issue.get('title') == args.title), ('body', self.ours(issue, body)),
                                      ('labels', set(args.label) <= {x['name'] for x in issue.get('labels', [])}),
                                      ('state', issue.get('state') == 'open')) if not ok]

    def check_labels(self, labels):
        for label in labels:
            try:
                api('GET', f'repos/{self.repo}/labels/{quote(label, safe="")}')
            except ApiError as e:
                raise Refused(f'label {label!r} not in {self.repo}: {e}')

    def check_open_issue(self, number):
        try:
            i = self.issue(number)
        except ApiError as e:
            raise Refused(f'issue {number} unreadable in {self.repo}: {e}')
        if 'pull_request' in i:
            raise Refused(f'#{number} in {self.repo} is a pull request')
        if i.get('state') != 'open':
            raise Refused(f'issue {number} in {self.repo} is {i.get("state")}')
        return i


def receipt(repo, action, outcome, issue=None, url=None, posted=False, **extra):
    r = {'repo': repo, 'action': action, 'issue': issue, 'url': url, 'outcome': outcome, 'posted': posted, **extra}
    return {k: v for k, v in r.items() if v is not None}


def only(mine):
    """The one post of ours, None when there is none; Ambiguous when several match."""
    if len(mine) > 1:
        raise Ambiguous(', '.join(m['html_url'] for m in mine))
    return mine[0] if mine else None


class Ambiguous(Exception):
    pass


def create(gi, args, body):
    repo = args.repo
    gi.check_labels(args.label)
    try:
        listed = gi.open_issues()
    except ApiError as e:
        raise Refused(f'open issues of {repo} unreadable, so none created: {e}')
    same = [i for i in listed if i.get('title', '').strip().casefold() == args.title.strip().casefold()]
    try:
        mine = only([i for i in same if i.get('title') == args.title and gi.ours(i, body)])
    except Ambiguous as e:
        return receipt(repo, 'create', 'mismatch', error=f'several of ours match: {e}')
    if mine:
        return settle(gi, args, body, mine, posted=False)
    if same:
        return receipt(repo, 'create', 'collision',
                       candidates=[{'issue': i['number'], 'url': i['html_url'], 'title': i['title']} for i in same])
    try:
        made = api('POST', f'repos/{repo}/issues', {'title': args.title, 'body': body, 'labels': args.label},
                   need=('number',))
    except ApiError as e:
        return recover_issue(gi, args, body, f'create response lost: {e}')
    try:
        back = gi.issue(made['number'])
    except ApiError as e:
        return receipt(repo, 'create', 'uncertain', made['number'], made.get('html_url'), posted=True,
                       error=f'read-back unavailable: {e}')
    return settle(gi, args, body, back, posted=True)


def settle(gi, args, body, issue, posted):
    bad = gi.differs(issue, args, body)
    return receipt(args.repo, 'create', 'mismatch' if bad else 'verified', issue['number'], issue.get('html_url'),
                   posted=posted, error=f'differs on {", ".join(bad)}' if bad else None)


def recover_issue(gi, args, body, why):
    try:
        mine = only([i for i in gi.open_issues() if i.get('title') == args.title and gi.ours(i, body)])
    except ApiError as e:
        return receipt(args.repo, 'create', 'uncertain', posted=None, error=f'{why}; relisting failed: {e}')
    except Ambiguous as e:
        return receipt(args.repo, 'create', 'mismatch', posted=None, error=f'{why}; several of ours match: {e}')
    if mine:
        return settle(gi, args, body, mine, posted=True)
    return receipt(args.repo, 'create', 'uncertain', posted=None, error=f'{why}; not found on relisting')


def comment(gi, args, body):
    repo, number = args.repo, args.issue
    gi.check_open_issue(number)
    try:
        mine = only([c for c in gi.comments(number) if gi.ours(c, body)])
    except ApiError as e:
        raise Refused(f'comments of issue {number} unreadable, so none posted: {e}')
    except Ambiguous as e:
        return receipt(repo, 'comment', 'mismatch', number, error=f'several of ours match: {e}')
    if mine:
        return receipt(repo, 'comment', 'verified', number, mine['html_url'])
    try:
        made = api('POST', f'repos/{repo}/issues/{number}/comments', {'body': body}, need=('id',))
    except ApiError as e:
        return recover_comment(gi, args, body, f'comment response lost: {e}')
    try:
        back = api('GET', f'repos/{repo}/issues/comments/{made["id"]}')
    except ApiError as e:
        return receipt(repo, 'comment', 'uncertain', number, made.get('html_url'), posted=True,
                       error=f'read-back unavailable: {e}')
    ok = gi.ours(back, body) and str(back.get('issue_url', '')).endswith(f'/repos/{repo}/issues/{number}')
    return receipt(repo, 'comment', 'verified' if ok else 'mismatch', number, back.get('html_url'), posted=True,
                   error=None if ok else 'read-back differs on body, author or issue')


def recover_comment(gi, args, body, why):
    repo, number = args.repo, args.issue
    try:
        mine = only([c for c in gi.comments(number) if gi.ours(c, body)])
    except ApiError as e:
        return receipt(repo, 'comment', 'uncertain', number, posted=None, error=f'{why}; relisting failed: {e}')
    except Ambiguous as e:
        return receipt(repo, 'comment', 'mismatch', number, posted=None, error=f'{why}; several of ours match: {e}')
    if mine:
        return receipt(repo, 'comment', 'verified', number, mine['html_url'], posted=True)
    return receipt(repo, 'comment', 'uncertain', number, posted=None, error=f'{why}; not found on relisting')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    sub = p.add_subparsers(dest='action', required=True)
    for name in ('create', 'comment'):
        s = sub.add_parser(name)
        s.add_argument('--repo', required=True)
        s.add_argument('--allow-repo', action='append', required=True)
        s.add_argument('--body-file', required=True)
        if name == 'create':
            s.add_argument('--title', required=True)
            s.add_argument('--label', action='append', default=[])
        else:
            s.add_argument('--issue', type=int, required=True)
    args = p.parse_args(argv)

    try:
        if args.repo not in args.allow_repo:
            raise Refused(f'{args.repo} is not in --allow-repo {", ".join(args.allow_repo)}')
        with open(args.body_file, encoding='utf-8') as f:
            body = f.read()
        if not body.strip():
            raise Refused('empty body')
        found = attribution_in(f'{getattr(args, "title", "")}\n{body}')
        if found:
            raise Refused(f'attribution or session link: {found!r}')
        gi = Issues(args.repo)
        r = (create if args.action == 'create' else comment)(gi, args, body)
    except Refused as e:
        r = receipt(args.repo, args.action, 'refused', error=str(e))
    except ApiError as e:
        r = receipt(args.repo, args.action, 'refused', error=f'GitHub unreachable: {e}')
    print(json.dumps(r))
    return {'verified': 0, 'refused': 2}.get(r['outcome'], 1)


if __name__ == '__main__':
    sys.exit(main())
