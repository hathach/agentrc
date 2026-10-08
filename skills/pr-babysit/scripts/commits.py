#!/usr/bin/env python3
"""Commit by path, and read back what commits hold, for pr-babysit to audit before it publishes.

  commits.py commit PATH...  commit exactly PATH, with the message on stdin
  commits.py head --parent P PATH...  audit the commit at HEAD, made on P from PATH
  commits.py chain FROM TO --published P --base B --tip T  audit FROM..TO for adoption (full SHAs), P the PR's head,
    B the base head GitHub recorded for it, T its base branch's live tip

`commit` stages PATH and runs `git commit --only`, so nothing staged beside
it is taken; it prints {committed, detail}: committed false, with git's last
lines, when git made no commit (a hook that fails or modifies a file stops it),
or before staging anything when the message is blank.

Per commit: sha, parents (every parent), paths (`git diff-tree --no-renames
-r -z`, one string per filename, unquoted; for a merge `-c`, the paths whose
entry differs from both parents, so what the base alone changed is not its)
and message (`%B`).
A link of a chain, or the head commit, is refused when it is a root, does not
sit on the commit before it, touches no path, or has a message line crediting
an agent, model, tool or session or linking a session (facts.attribution_in).
A chain link may be a merge of a commit B or T holds, a merge from the base
branch, which may touch no path; any other merge, and a merge at the head, is
refused.
`chain` follows first parents only, so the base's own commits are no links. It
reports from, to, published, base and tip as given, commits (the SHAs of
FROM..TO, oldest first), paths (each path any of them touches, once),
unpublished (each path the commits after P touch, once; all of them when P is
not a chain commit) and refusal, "" or why the chain cannot be adopted: empty,
not ending at TO, or a link refused.
`head` resolves HEAD once and reads everything from that SHA; HEAD moving while
it reads is an error. It reports parent and scope (PATH) as given, sha, entries
(`git ls-tree -z <sha> -- PATH` lines) and refusal, "" or why the commit cannot
be published: HEAD still at P, the link refused, a path outside PATH, a change
to PATH left uncommitted (`git status --porcelain -z -- PATH`), or a blank
message. PATH is never read as an option or as pathspec magic.

stdout ends with one JSON line: `head` prints {parent, scope, sha, entries,
refusal}, `chain` {from, to, published, base, tip, commits, paths, unpublished,
refusal}, each line but an error one with its `seal` (facts.sealed). Exit 0
with that line; exit 2 with {"error": ...} when git cannot answer or the
arguments are wrong, or when no base in the checkout holds a merge link's
second parent while B or T is not in the checkout.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Unusable, attempt, attribution_in, git, report, run  # noqa: E402


def records(text):
    return [r for r in text.split('\0') if r]


def commit(sha):
    parents = git('show', '-s', '--format=%P', sha).split()
    return {'sha': sha, 'parents': parents,
            'paths': records(git('diff-tree', '--no-commit-id', '--no-renames', '--name-only', '-r', '-z', *(['-c'] if len(parents) > 1 else []), sha)),
            'message': git('log', '-1', '--format=%B', sha)}


def full(sha):
    if not FULL_SHA.match(sha):
        raise Unusable(f'not a full SHA: {sha!r}')
    return sha


def on_base(sha, bases):
    present = [b for b in bases if not attempt('git', 'cat-file', '-e', f'{b}^{{commit}}')[0]]
    if any(run('git', 'merge-base', '--is-ancestor', sha, b, ok=(0, 1))[0] == 0 for b in present):
        return True
    missing = [b for b in bases if b not in present]
    if missing:
        # pr-babysit.js matches this text to stop as adopt-base-missing
        raise Unusable(f"the PR's base {missing[0]} is not in the checkout: fetch it")
    return False


def link_refusal(c, parent, bases=None):
    sha = c['sha'][:7]
    merge = bases is not None and len(c['parents']) == 2
    if len(c['parents']) != 1 and not merge:
        return f"{sha} has {len(c['parents'])} parents: history this run cannot audit"
    if c['parents'][0] != parent:
        return f"{sha} sits on {c['parents'][0][:7]}, not {parent[:7]}"
    if merge and not on_base(c['parents'][1], bases):
        return f"{sha} merges {c['parents'][1][:7]}, which is not on the PR's base {' or '.join(b[:7] for b in bases)}"
    if not c['paths'] and not merge:
        return f'{sha} touches no path'
    said = attribution_in(c['message'])
    return f'commit message carries attribution: {said}' if said else ''


def head(parent, paths):
    full(parent)
    sha = git('rev-parse', 'HEAD').strip()
    c = commit(sha)
    leftover = records(git('--literal-pathspecs', 'status', '--porcelain', '-z', '--', *paths))
    entries = records(git('--literal-pathspecs', 'ls-tree', '-z', sha, '--', *paths))
    now = git('rev-parse', 'HEAD').strip()
    if now != sha:
        raise Unusable(f'HEAD moved from {sha} to {now} while it was read')
    strays = [f for f in c['paths'] if f not in paths]
    refusal = ('nothing was committed: HEAD is still the parent' if sha == parent
               else link_refusal(c, parent)
               or (strays and f"the commit carries unowned path(s): {', '.join(strays)}")
               or (leftover and f"the commit left owned change(s) behind: {', '.join(leftover)}")
               or ('the commit has no message' if not c['message'].strip() else ''))
    return {'parent': parent, 'scope': paths, 'sha': sha, 'entries': entries, 'refusal': refusal}


def touched(found):
    return list(dict.fromkeys(p for c in found for p in c['paths']))


def chain(start, end, published, base, tip):
    published, base, tip = full(published), full(base), full(tip)
    bases = list(dict.fromkeys([base, tip]))
    found = [commit(sha) for sha in git('rev-list', '--reverse', '--first-parent', f'{full(start)}..{full(end)}').split()]
    shas = [c['sha'] for c in found]
    after = shas.index(published) + 1 if published in shas else 0
    return {'from': start, 'to': end, 'published': published, 'base': base, 'tip': tip, 'commits': shas, 'paths': touched(found),
            'unpublished': touched(found[after:]), 'refusal': chain_refusal(start, end, bases, found)}


def chain_refusal(start, end, bases, found):
    if not found:
        return f'no commits in {start[:7]}..{end[:7]}'
    if found[-1]['sha'] != end:
        return f"the chain ends at {found[-1]['sha'][:7]}, not {end[:7]}"
    before = start
    for c in found:
        refusal = link_refusal(c, before, bases)
        if refusal:
            return refusal
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
    if len(argv) > 3 and argv[:2] == ['head', '--parent']:
        return head(argv[2], argv[3:])
    if len(argv) == 9 and argv[0] == 'chain' and argv[3] == '--published' and argv[5] == '--base' and argv[7] == '--tip':
        return chain(argv[1], argv[2], argv[4], argv[6], argv[8])
    raise Unusable('usage: commits.py commit PATH... | commits.py head --parent P PATH... | commits.py chain FROM TO --published P --base B --tip T')


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
