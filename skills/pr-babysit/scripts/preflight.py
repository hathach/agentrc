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

import argparse
import json
import subprocess
import sys


class Unusable(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Unusable(f'usage: {message}')


def run(*argv, ok=(0,)):
    done = subprocess.run(argv, capture_output=True)
    if done.returncode not in ok:
        raise Unusable(f"{' '.join(argv)}: {done.stderr.decode(errors='replace').strip()}")
    try:
        return done.returncode, done.stdout.decode()
    except UnicodeDecodeError:
        raise Unusable(f"{' '.join(argv)}: output is not UTF-8")


def pin(pr):
    branch = run('git', 'rev-parse', '--abbrev-ref', 'HEAD')[1].strip()
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
    remote = run('git', 'config', '--get', f'branch.{branch}.remote')[1].strip() if tracks else ''
    urls = run('git', 'remote', 'get-url', '--push', '--all', remote)[1].splitlines() if remote else []
    return {'branch': branch, **pr_facts, 'remote': remote, 'pushUrls': urls,
            'head': run('git', 'rev-parse', 'HEAD')[1].strip(),
            'dirty': run('git', 'status', '--porcelain')[1].splitlines()}


def main(argv):
    try:
        p = Parser(prog='preflight.py', add_help=False)
        p.add_argument('--pr', type=int, required=True)
        facts = pin(p.parse_args(argv).pr)
    except Unusable as e:
        print(json.dumps({'error': str(e)}))
        return 2
    print(json.dumps(facts))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
