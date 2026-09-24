#!/usr/bin/env python3
"""Read back what commits hold, for pr-babysit to audit before it publishes.

  commits.py head PATH...    the commit at HEAD, and PATH as the tree has it
  commits.py chain FROM TO   every commit in FROM..TO, oldest first (full SHAs)

Per commit: sha, parents (every parent), paths (`git diff-tree --no-renames
-r -z`, one string per filename, unquoted) and message (`%B`, verbatim).
`head` resolves HEAD once, reads everything from that SHA and adds leftover
(`git status --porcelain -z -- PATH` records) and entries (`git ls-tree -z
<sha> -- PATH` lines); HEAD moving while it reads is an error. PATH is never
read as an option.

stdout ends with one JSON line: `head` prints {sha, parents, paths, leftover,
entries, message}, `chain` {commits: [...]}. Exit 0 with that line; exit 2
with {"error": ...} when git cannot answer or the arguments are wrong.
"""

import json
import re
import subprocess
import sys

FULL_SHA = re.compile(r'^[0-9a-f]{40}$')


class Unusable(Exception):
    pass


def git(*argv):
    done = subprocess.run(['git', *argv], capture_output=True)
    if done.returncode:
        raise Unusable(f"git {' '.join(argv)}: {done.stderr.decode(errors='replace').strip()}")
    try:
        return done.stdout.decode()
    except UnicodeDecodeError:
        # Replacing the bad bytes could make two different paths read as one.
        raise Unusable(f"git {' '.join(argv)}: output is not UTF-8")


def records(text):
    return [r for r in text.split('\0') if r]


def commit(sha):
    return {'sha': sha,
            'parents': git('show', '-s', '--format=%P', sha).split(),
            'paths': records(git('diff-tree', '--no-commit-id', '--no-renames', '--name-only', '-r', '-z', sha)),
            'message': git('log', '-1', '--format=%B', sha)}


def head(paths):
    sha = git('rev-parse', 'HEAD').strip()
    facts = {**commit(sha),
             'leftover': records(git('status', '--porcelain', '-z', '--', *paths)),
             'entries': records(git('ls-tree', '-z', sha, '--', *paths))}
    now = git('rev-parse', 'HEAD').strip()
    if now != sha:
        raise Unusable(f'HEAD moved from {sha} to {now} while it was read')
    return facts


def chain(start, end):
    for sha in (start, end):
        if not FULL_SHA.match(sha):
            raise Unusable(f'not a full SHA: {sha!r}')
    return {'commits': [commit(sha) for sha in git('rev-list', '--reverse', f'{start}..{end}').split()]}


def main(argv):
    try:
        if len(argv) > 1 and argv[0] == 'head':
            facts = head(argv[1:])
        elif len(argv) == 3 and argv[0] == 'chain':
            facts = chain(argv[1], argv[2])
        else:
            raise Unusable('usage: commits.py head PATH... | commits.py chain FROM TO')
    except Unusable as e:
        print(json.dumps({'error': str(e)}))
        return 2
    print(json.dumps(facts))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
