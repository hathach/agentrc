#!/usr/bin/env python3
"""Pin the checkout and the PR that pr-babysit is about to babysit.

  preflight.py --pr N

Reports what every later step must still be true of: branch (`git rev-parse
--abbrev-ref HEAD`), prBranch, prHead, prRepo (owner/name) and prUrl from one
`gh pr view N`, verbatim even when they disagree with git; remote, the remote
the branch tracks, "" when it tracks none; pushUrls (`git remote get-url
--push --all <remote>`, which a pushurl can point away from the fetch URL);
head; and dirty, the lines of `git status --porcelain`.

stdout ends with one JSON line with exactly those keys. Exit 0 with it; exit 2
with {"error": ...} when git or gh cannot answer; the caller then pins nothing.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, git, report, run  # noqa: E402


def pin(pr):
    branch = git('rev-parse', '--abbrev-ref', 'HEAD').strip()
    fields = 'headRefName,headRefOid,headRepositoryOwner,headRepository,url'
    try:
        view = json.loads(run('gh', 'pr', 'view', str(pr), '--json', fields)[1])
        pr_facts = {'prBranch': view['headRefName'], 'prHead': view['headRefOid'],
                    'prRepo': f"{view['headRepositoryOwner']['login']}/{view['headRepository']['name']}",
                    'prUrl': view['url']}
    except (ValueError, KeyError, TypeError) as e:
        # A deleted head fork comes back as a null headRepository.
        raise Unusable(f'gh pr view {pr}: unexpected answer ({e!r})')
    # Git alone says whether the branch tracks anything (a remote without a merge
    # ref does not); the config names the remote whole, a `/` in it included.
    tracks = not run('git', 'rev-parse', '--abbrev-ref', '@{u}', ok=(0, 128))[0]
    remote = git('config', '--get', f'branch.{branch}.remote').strip() if tracks else ''
    urls = git('remote', 'get-url', '--push', '--all', remote).splitlines() if remote else []
    return {'branch': branch, **pr_facts, 'remote': remote, 'pushUrls': urls,
            'head': git('rev-parse', 'HEAD').strip(),
            'dirty': git('status', '--porcelain').splitlines()}


def collect(argv):
    p = Parser(prog='preflight.py', add_help=False)
    p.add_argument('--pr', type=int, required=True)
    return pin(p.parse_args(argv).pr)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
