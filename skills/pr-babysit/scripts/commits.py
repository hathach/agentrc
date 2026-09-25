#!/usr/bin/env python3
"""Commit by path, and read back what commits hold, for pr-babysit to audit before it publishes.

  commits.py commit PATH...  commit exactly PATH, with the message on stdin
  commits.py head PATH...    the commit at HEAD, and PATH as the tree has it
  commits.py chain FROM TO   every commit in FROM..TO, oldest first (full SHAs)

`commit` stages PATH and runs `git commit --only`, so nothing staged beside
it is taken; it prints {committed, detail}: committed false, with git's last
lines, when git made no commit (a hook that fails or modifies a file stops it),
or before staging anything when the message is blank.

Per commit: sha, parents (every parent), paths (`git diff-tree --no-renames
-r -z`, one string per filename, unquoted) and message (`%B`, verbatim).
`head` resolves HEAD once, reads everything from that SHA and adds leftover
(`git status --porcelain -z -- PATH` records) and entries (`git ls-tree -z
<sha> -- PATH` lines); HEAD moving while it reads is an error. PATH is never
read as an option or as pathspec magic.

stdout ends with one JSON line: `head` prints {sha, parents, paths, leftover,
entries, message}, `chain` {commits: [...]}. Exit 0 with that line; exit 2
with {"error": ...} when git cannot answer or the arguments are wrong.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Unusable, attempt, git, report  # noqa: E402


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
             'leftover': records(git('--literal-pathspecs', 'status', '--porcelain', '-z', '--', *paths)),
             'entries': records(git('--literal-pathspecs', 'ls-tree', '-z', sha, '--', *paths))}
    now = git('rev-parse', 'HEAD').strip()
    if now != sha:
        raise Unusable(f'HEAD moved from {sha} to {now} while it was read')
    return facts


def chain(start, end):
    for sha in (start, end):
        if not FULL_SHA.match(sha):
            raise Unusable(f'not a full SHA: {sha!r}')
    return {'commits': [commit(sha) for sha in git('rev-list', '--reverse', f'{start}..{end}').split()]}


def make(paths):
    try:
        message = sys.stdin.read()
    except UnicodeDecodeError:
        raise Unusable('the message on stdin is not UTF-8')
    if not message.strip():
        return {'committed': False, 'detail': 'the commit message is blank'}
    # --literal-pathspecs: a file named like pathspec magic, such as `:(top)*`, is that file only.
    before = git('rev-parse', 'HEAD').strip()
    git('--literal-pathspecs', 'add', '--', *paths)
    code, out, err = attempt('git', '--literal-pathspecs', 'commit', '--only', '-F', '-', '--', *paths, input=message)
    said = (out + err).strip().splitlines()
    tail = ' / '.join(said[-5:])
    after = git('rev-parse', 'HEAD').strip()
    if code:
        if after != before:
            raise Unusable(f'git commit failed yet HEAD moved from {before} to {after}: {tail}')
        return {'committed': False, 'detail': tail or f'git commit exited {code}'}
    return {'committed': True, 'detail': said[0] if said else 'committed'}


def collect(argv):
    if len(argv) > 1 and argv[0] == 'commit':
        return make(argv[1:])
    if len(argv) > 1 and argv[0] == 'head':
        return head(argv[1:])
    if len(argv) == 3 and argv[0] == 'chain':
        return chain(argv[1], argv[2])
    raise Unusable('usage: commits.py commit PATH... | commits.py head PATH... | commits.py chain FROM TO')


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
