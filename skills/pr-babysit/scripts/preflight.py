#!/usr/bin/env python3
"""Pin the checkout and the PR that pr-babysit is about to babysit.

  preflight.py --pr N [--needs PATH...]
  preflight.py --recheck

Reports what every later step must still be true of: branch (`git rev-parse
--abbrev-ref HEAD`), prBranch, prHead, prBase (the base branch's head), prRepo
(owner/name) and prUrl from one `gh pr view N`, verbatim even when they disagree with git; remote, the remote
the branch tracks, "" when it tracks none; upstreamBranch, the branch it
tracks there, "" when none; pushUrls (`git remote get-url
--push --all <remote>`, which a pushurl can point away from the fetch URL);
head; dirty, the lines of `git status --porcelain`; pr, echoed; and
badPushUrl, the first push URL that is not github.com/<prRepo> over https or
ssh (case-insensitive), every one when prUrl is not https on
github.com, "(no push URL)" when there is none, "(empty push URL)" for an
empty one, "" when all are; and receipts, a new empty directory under
<tmp>/pr-babysit-receipts for this launch's reply.py --receipt files.

--needs names a script the workflow will call; any that is not a file means
the session runs a workflow definition older than the installed scripts, and
nothing is pinned.

--recheck reads, before a commit, what the pin must still match, without gh:
branch, pushUrls and head as above, staged (`git diff --cached --name-only -z`
records) and status (`git status --porcelain -z` records).

stdout ends with one JSON line with exactly those keys, plus `seal` (facts.sealed),
which pr-babysit checks its relayed copy against. Exit 0 with it; exit 2
with {"error": ...} when git or gh cannot answer; the caller then pins nothing.
"""

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, git, report, run  # noqa: E402


HOST = 'github.com'
# scp-like needs `:` and a URL `/` after the host: accepting either makes `git@github.com/owner/repo` look right while git reads a local path.
ORIGIN = re.compile(r'(?:(?:https|ssh)://(?:[^@/]*@)?([^/:]+)(?::[0-9]+)?/|(?:[^@/\s]+@)([^/:]+):)([^/]+)/([^/]+?)(?:\.git)?')


def origin_of(url):
    m = ORIGIN.fullmatch(url.strip().rstrip('/'))
    if not m:
        return ''
    host = (m[1] or m[2]).lower()
    return f'{host}/{m[3]}/{m[4]}'.lower() if host == HOST else ''


def bad_push_url(pr_url, expected, urls):
    """The first push URL `git push` would send elsewhere; the PR URL names the base repo, so it gives only the host."""
    if not urls:
        return '(no push URL)'
    m = re.match(r'https://([^/:?#]+)/', pr_url.strip())
    on_host = bool(m) and m[1].lower() == HOST
    bad = next((u for u in urls if not on_host or origin_of(u) != expected), None)
    return '' if bad is None else bad or '(empty push URL)'


def records(text):
    return [r for r in text.split('\0') if r]


def push_urls(branch):
    """The remote the branch tracks, "" when none, and its push URLs."""
    # Git alone says whether the branch tracks anything (a remote without a merge
    # ref does not); the config names the remote whole, a `/` in it included.
    tracks = not run('git', 'rev-parse', '--abbrev-ref', '@{u}', ok=(0, 128))[0]
    remote = git('config', '--get', f'branch.{branch}.remote').strip() if tracks else ''
    return remote, git('remote', 'get-url', '--push', '--all', remote).splitlines() if remote else []


def upstream_branch(branch, remote):
    """The branch name the local branch tracks on its remote, "" when none."""
    if not remote:
        return ''
    ref = git('for-each-ref', '--format=%(upstream:remoteref)', f'refs/heads/{branch}').strip()
    return ref.removeprefix('refs/heads/') if ref.startswith('refs/heads/') else ''


def recheck():
    branch = git('rev-parse', '--abbrev-ref', 'HEAD').strip()
    return {'branch': branch, 'pushUrls': push_urls(branch)[1], 'head': git('rev-parse', 'HEAD').strip(),
            'staged': records(git('diff', '--cached', '--name-only', '-z')),
            'status': records(git('status', '--porcelain', '-z'))}


def receipts_dir():
    parent = Path(tempfile.gettempdir()) / 'pr-babysit-receipts'
    parent.mkdir(exist_ok=True)
    return tempfile.mkdtemp(dir=parent)


def pin(pr):
    branch = git('rev-parse', '--abbrev-ref', 'HEAD').strip()
    fields = 'headRefName,headRefOid,baseRefOid,headRepositoryOwner,headRepository,url'
    try:
        view = json.loads(run('gh', 'pr', 'view', str(pr), '--json', fields)[1])
        pr_facts = {'prBranch': view['headRefName'], 'prHead': view['headRefOid'], 'prBase': view['baseRefOid'],
                    'prRepo': f"{view['headRepositoryOwner']['login']}/{view['headRepository']['name']}",
                    'prUrl': view['url']}
    except (ValueError, KeyError, TypeError) as e:
        # A deleted head fork comes back as a null headRepository.
        raise Unusable(f'gh pr view {pr}: unexpected answer ({e!r})')
    remote, urls = push_urls(branch)
    expected = f"{HOST}/{pr_facts['prRepo'].lower()}"
    return {'branch': branch, **pr_facts, 'remote': remote, 'upstreamBranch': upstream_branch(branch, remote),
            'pushUrls': urls,
            'head': git('rev-parse', 'HEAD').strip(),
            'dirty': git('status', '--porcelain').splitlines(),
            'pr': pr, 'badPushUrl': bad_push_url(pr_facts['prUrl'], expected, urls),
            'receipts': receipts_dir()}


def collect(argv):
    p = Parser(prog='preflight.py', add_help=False)
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument('--pr', type=int)
    which.add_argument('--recheck', action='store_true')
    p.add_argument('--needs', action='append', default=[])
    a = p.parse_args(argv)
    if a.recheck and a.needs:
        p.error('--needs is for --pr')
    missing = [n for n in a.needs if not Path(n).expanduser().is_file()]
    if missing:
        raise Unusable(f"stale workflow definition: {', '.join(missing)} missing; reload the workflow (a fresh session) before relaunching")
    return recheck() if a.recheck else pin(a.pr)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
