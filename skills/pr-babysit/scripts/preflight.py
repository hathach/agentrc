#!/usr/bin/env python3
"""Pin the checkout and the PR that pr-babysit is about to babysit.

  preflight.py --pr N [--repo OWNER/NAME] [--needs PATH...] [--module NAME...]
  preflight.py --recheck
  preflight.py --state N [--repo OWNER/NAME]

Every gh call names the repository PR N belongs to (facts.bind_repo): --repo,
else the one the checkout's remotes name, refusing a checkout whose remotes
name several and mark none gh-resolved.

Reports what every later step must still be true of: python, this
interpreter's sys.executable, `/`-separated, the one every later script runs
under; missing, the --module names it cannot import; repo, the repository so
named; branch (`git rev-parse
--abbrev-ref HEAD`), prBranch, prHead, prBase (the base head GitHub last
recorded for the PR, stale once the base moves until the head is pushed),
prBaseBranch, prRepo (owner/name, "" for a PR no longer open whose head
repository is gone), prUrl and prState (OPEN, MERGED or CLOSED, any other an
error) from one `gh pr view N`, verbatim
even when they disagree with git; prBaseTip, the base branch's live tip in the
base repository (`gh api repos/{owner}/{repo}/git/ref/heads/<prBaseBranch>`;
"" for a PR no longer open, whose base
branch may be gone); remote, the remote
the branch tracks, "" when it tracks none; upstreamBranch, the branch it
tracks there, "" when none; pushUrls (`git remote get-url
--push --all <remote>`, which a pushurl can point away from the fetch URL);
head; dirty, the lines of `git status --porcelain`; pr, echoed; and
badPushUrl, the first push URL that is not github.com/<prRepo> over https or
ssh (case-insensitive), every one when prUrl is not https on
github.com, "(no push URL)" when there is none, "(empty push URL)" for an
empty one, "" when all are; and receipts, a new empty directory under
<tmp>/pr-babysit-receipts for this launch's reply.py and sonar.py --receipt
files.

--needs names a script the workflow will call; any that is not a file means
the session runs a workflow definition older than the installed scripts, and
nothing is pinned.

--recheck reads, before a commit, what the pin must still match, without gh:
branch, pushUrls and head as above, staged (`git diff --cached --name-only -z`
records) and status (`git status --porcelain -z` records).

--state reads PR N's state alone (`gh pr view N --json state`): state, as
prState above.

stdout ends with one JSON line with exactly those keys, plus `seal` (facts.sealed),
which pr-babysit checks its relayed copy against. Exit 0 with it; exit 2
with {"error": ...} when git or gh cannot answer; the caller then pins nothing.
"""

import importlib.util
import json
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, HOST, Parser, Unusable, bind_repo, git, github_repo, report, run  # noqa: E402


def origin_of(url):
    name = github_repo(url)
    return f'{HOST}/{name}'.lower() if name else ''


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
    return Path(tempfile.mkdtemp(dir=parent)).as_posix()  # relayed by a model and pasted into commands: no backslashes


def base_tip(name):
    """The live head of the base repository's branch name."""
    try:
        # quote: a `{` would read as a gh placeholder, `#` or `%` as URL syntax.
        path = 'repos/{owner}/{repo}/git/ref/heads/' + urllib.parse.quote(name, safe='/')
        ref = json.loads(run('gh', 'api', path)[1])
        if ref['ref'] != f'refs/heads/{name}' or ref['object']['type'] != 'commit' or not FULL_SHA.fullmatch(ref['object']['sha']):
            raise ValueError(ref)
        return ref['object']['sha']
    except (ValueError, KeyError, TypeError) as e:
        raise Unusable(f'gh api git/ref/heads/{name}: unexpected answer ({e!r})')


def pr_view(pr, fields):
    """`gh pr view N --json fields`, its state one pr-babysit knows; Unusable otherwise."""
    try:
        view = json.loads(run('gh', 'pr', 'view', str(pr), '--json', fields)[1])
        if view['state'] not in ('OPEN', 'MERGED', 'CLOSED'):
            raise ValueError(f"state {view['state']!r}")
        return view
    except (ValueError, KeyError, TypeError) as e:
        raise Unusable(f'gh pr view {pr}: unexpected answer ({e!r})')


def interpreter(modules):
    python = Path(sys.executable).as_posix() if sys.executable else ''
    if not python:
        raise Unusable('this interpreter does not know its own path (sys.executable is empty)')
    return {'python': python, 'missing': [m for m in modules if importlib.util.find_spec(m) is None]}


def pin(pr, repo, modules):
    branch = git('rev-parse', '--abbrev-ref', 'HEAD').strip()
    view = pr_view(pr, 'headRefName,headRefOid,baseRefOid,baseRefName,headRepositoryOwner,headRepository,url,state')
    try:
        owner, head_repo = view['headRepositoryOwner'], view['headRepository']
        # A deleted head fork comes back as a null headRepository: only a PR no longer open, which stops the launch, pins without one.
        gone = view['state'] != 'OPEN' and not (owner and head_repo)
        pr_facts = {'prBranch': view['headRefName'], 'prHead': view['headRefOid'], 'prBase': view['baseRefOid'],
                    'prBaseBranch': view['baseRefName'],
                    'prRepo': '' if gone else f"{owner['login']}/{head_repo['name']}",
                    'prUrl': view['url'], 'prState': view['state']}
    except (KeyError, TypeError) as e:
        raise Unusable(f'gh pr view {pr}: unexpected answer ({e!r})')
    pr_facts['prBaseTip'] = base_tip(pr_facts['prBaseBranch']) if view['state'] == 'OPEN' else ''
    remote, urls = push_urls(branch)
    expected = f"{HOST}/{pr_facts['prRepo'].lower()}"
    return {**interpreter(modules), 'repo': repo, 'branch': branch, **pr_facts, 'remote': remote, 'upstreamBranch': upstream_branch(branch, remote),
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
    which.add_argument('--state', type=int, metavar='N')
    p.add_argument('--needs', action='append', default=[])
    p.add_argument('--repo')
    p.add_argument('--module', action='append', default=[])
    a = p.parse_args(argv)
    if a.pr is None and (a.needs or a.module):
        p.error('--needs and --module are for --pr')
    if not all(m.isidentifier() for m in a.module):
        p.error(f'--module takes a top-level module name: {a.module}')
    if a.recheck and a.repo:
        p.error('--repo is for --pr and --state')
    missing = [n for n in a.needs if not Path(n).expanduser().is_file()]
    if missing:
        raise Unusable(f"stale workflow definition: {', '.join(missing)} missing; reload the workflow (a fresh session) before relaunching")
    if a.recheck:
        return recheck()
    repo = bind_repo(a.repo)
    return {'state': pr_view(a.state, 'state')['state']} if a.state is not None else pin(a.pr, repo, a.module)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
