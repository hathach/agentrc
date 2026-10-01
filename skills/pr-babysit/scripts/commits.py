#!/usr/bin/env python3
"""Commit by path, and read back what commits hold, for pr-babysit to audit before it publishes.

  commits.py commit PATH...  commit exactly PATH, with the message on stdin
  commits.py head PATH...    the commit at HEAD, and PATH as the tree has it
  commits.py chain FROM TO   audit FROM..TO for adoption (full SHAs)

`commit` stages PATH and runs `git commit --only`, so nothing staged beside
it is taken; it prints {committed, detail}: committed false, with git's last
lines, when git made no commit (a hook that fails or modifies a file stops it),
or before staging anything when the message is blank.

Per commit: sha, parents (every parent), paths (`git diff-tree --no-renames
-r -z`, one string per filename, unquoted) and message (`%B` without its
trailing whitespace, which a relay drops and the seal would then refuse).
`chain` reports from and to as given, commits (the SHAs of FROM..TO, oldest
first), paths (each path any of them touches, once) and refusal, "" or why
the chain cannot be adopted: empty, not ending at TO, a merge or a root, a
commit not on the one before it, a commit touching no path, or a message line
crediting an agent, model, tool or session.
`head` resolves HEAD once, reads everything from that SHA and adds leftover
(`git status --porcelain -z -- PATH` records) and entries (`git ls-tree -z
<sha> -- PATH` lines); HEAD moving while it reads is an error. PATH is never
read as an option or as pathspec magic.

stdout ends with one JSON line: `head` prints {sha, parents, paths, leftover,
entries, message}, `chain` {from, to, commits, paths, refusal}, each line but an error one with its
`seal` (facts.sealed). Exit 0 with that line; exit 2
with {"error": ...} when git cannot answer or the arguments are wrong.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Unusable, attempt, git, report  # noqa: E402


# The human is the sole author: no commit message line may credit an agent, model, tool or session.
ATTRIBUTION = [re.compile(p, re.I | re.ASCII) for p in (
    r'^[ \t]*co-authored-by[ \t]*:',
    r'^[ \t]*(([a-z]+-)+session(-[a-z]+)*|session-(url|id|link))[ \t]*:',
    r'^[ \t]*(🤖[ \t]*)?(generated|authored|written|created|made)[ \t-]*(with|by)[ \t]*:?[ \t]*\[?'
    r'(claude|codex|chatgpt|gpt|copilot|openai|anthropic|an? (ai|llm|agent))\b',
    r'^[ \t]*https?://claude\.ai/code/session_[a-z0-9]+[ \t]*$',
)]


def attribution_in(message):
    return next((line for line in message.split('\n') if any(r.search(line) for r in ATTRIBUTION)), None)


def records(text):
    return [r for r in text.split('\0') if r]


def commit(sha):
    return {'sha': sha,
            'parents': git('show', '-s', '--format=%P', sha).split(),
            'paths': records(git('diff-tree', '--no-commit-id', '--no-renames', '--name-only', '-r', '-z', sha)),
            'message': git('log', '-1', '--format=%B', sha).rstrip()}


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
    found = [commit(sha) for sha in git('rev-list', '--reverse', f'{start}..{end}').split()]
    return {'from': start, 'to': end, 'commits': [c['sha'] for c in found],
            'paths': list(dict.fromkeys(p for c in found for p in c['paths'])),
            'refusal': chain_refusal(start, end, found)}


def chain_refusal(start, end, found):
    if not found:
        return f'no commits in {start[:7]}..{end[:7]}'
    if found[-1]['sha'] != end:
        return f"the chain ends at {found[-1]['sha'][:7]}, not {end[:7]}"
    before = start
    for c in found:
        sha = c['sha'][:7]
        if len(c['parents']) != 1:
            return f'{sha} is a merge or a root: history this run cannot audit'
        if c['parents'][0] != before:
            return f'{sha} does not sit on the commit before it in the chain from {start[:7]}'
        if not c['paths']:
            return f'{sha} touches no path'
        if attribution_in(c['message']):
            return f"commit message carries attribution: {attribution_in(c['message']).strip()}"
        before = c['sha']
    return ''


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
    sys.exit(report(collect, sys.argv[1:], seal=True))
