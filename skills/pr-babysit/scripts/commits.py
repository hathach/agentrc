#!/usr/bin/env python3
"""Commit by path, and read back what commits hold, for pr-babysit to audit before it publishes.

  commits.py commit [--receipt FILE --request R] PATH...  commit exactly PATH, with the message on stdin
  commits.py receipt FILE --request R --parent P PATH...  read back what a `commit --receipt FILE` printed
  commits.py head --parent P PATH...  audit the commit at HEAD, made on P from PATH
  commits.py chain FROM TO --published P --base B --tip T  audit FROM..TO for adoption (full SHAs), P the PR's head,
    B the base head GitHub recorded for it, T its base branch's live tip
  commits.py cited HEAD SHA...  whether a rewritten PR head still holds each commit our fix notes cite

`commit` stages PATH and runs `git commit --only`, so nothing staged beside
it is taken; it prints {committed, detail}: committed false, with git's last
lines, when git made no commit (a hook that fails or modifies a file stops it),
or before staging anything when the message is blank. detail is bounded to
DETAIL_MAX characters, terminal escapes and control characters dropped, so a
relay can copy it. With --receipt, FILE must not exist yet; the line, with R,
the parent P (HEAD before) and PATH, is saved there for a caller whose copy of
it was lost, and `receipt` prints it again as {request, parent, scope,
committed, detail, sha} (sha HEAD after, "" without a commit), refusing a
receipt that is absent or for another R, P or PATH. It never commits.

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
not a chain commit), mergeOnly (each unpublished path only merge links touch),
changes ({sha, paths} per commit after P, oldest first, so a protected path is
covered commit by commit) and refusal, "" or why the chain cannot be adopted: empty,
not ending at TO, or a link refused.
`head` resolves HEAD once and reads everything from that SHA; HEAD moving while
it reads is an error. It reports parent and scope (PATH) as given, sha, entries
(`git ls-tree -z <sha> -- PATH` lines) and refusal, "" or why the commit cannot
be published: HEAD still at P, the link refused, a path outside PATH, a change
to PATH left uncommitted (`git status --porcelain -z --untracked-files=all -- PATH`), or a blank
message. PATH is never read as an option or as pathspec magic.

`cited` (full SHAs) reports head as given and cited, one {sha, reachable, replacement} per SHA in order:
reachable true when HEAD holds it, null when the checkout lacks the commit, else false, with replacement
the one commit between the fork point and HEAD carrying the same patch (`git patch-id --stable`), null
when none or several do, or the histories share no fork point.

stdout ends with one JSON line: `commit` prints {committed, detail}, `head` {parent, scope, sha, entries,
refusal}, `chain` {from, to, published, base, tip, commits, paths, unpublished,
mergeOnly, changes, refusal}, `cited` {head, cited}, each line but an error one with its `seal` (facts.sealed). Exit 0
with that line; exit 2 with {"error": ...} when git cannot answer or the
arguments are wrong, or when no base in the checkout holds a merge link's
second parent while B or T is not in the checkout.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Unusable, attempt, attribution_in, git, report, run, sealed, status  # noqa: E402

DETAIL_MAX = 500
ESCAPE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|.)')


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
    leftover = status(*paths)
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
    unpublished = touched(found[after:])
    ordinary = set(touched(c for c in found[after:] if len(c['parents']) == 1))
    return {'from': start, 'to': end, 'published': published, 'base': base, 'tip': tip, 'commits': shas, 'paths': touched(found),
            'unpublished': unpublished, 'mergeOnly': [p for p in unpublished if p not in ordinary],
            'changes': [{'sha': c['sha'], 'paths': c['paths']} for c in found[after:]],
            'refusal': chain_refusal(start, end, bases, found)}


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


def bounded(text):
    """Hook output a relay can copy (#266): no terminal escapes or control characters, at most DETAIL_MAX characters."""
    text = ''.join(c if c.isprintable() else ' ' for c in ESCAPE.sub('', text))
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX - 1] + '…'


def outcome(paths):
    try:
        # -sig: a leading BOM would hide an attribution first line from attribution_in
        message = sys.stdin.buffer.read().decode('utf-8-sig')
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
            raise Unusable(f'git commit failed yet HEAD moved from {before} to {after}: {bounded(tail)}')
        return {'committed': False, 'detail': bounded(tail or f'git commit exited {code}')}
    return {'committed': True, 'detail': bounded(said[0] if said else 'committed')}


def make(paths, receipt=None, request=None):
    if receipt is None:
        return outcome(paths)
    if os.path.lexists(receipt):
        raise Unusable(f'receipt {receipt} already exists: a receipt names one commit attempt')
    parent = git('rev-parse', 'HEAD').strip()
    line = outcome(paths)
    sha = git('rev-parse', 'HEAD').strip() if line['committed'] else ''
    saved = {'request': request, 'parent': parent, 'scope': paths, 'line': sealed({**line, 'sha': sha})}
    try:
        Path(receipt + '.tmp').write_text(json.dumps(saved), encoding='utf-8')
        os.replace(receipt + '.tmp', receipt)
    except OSError as e:
        print(f'commits.py: receipt not saved: {e}', file=sys.stderr)
    return line


def read_receipt(path, request, parent, paths):
    try:
        saved = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise Unusable(f'no receipt at {path}: the commit script saved none')
    except (OSError, ValueError) as e:
        raise Unusable(f'receipt {path} is unreadable: {e}')
    line = saved.get('line') if isinstance(saved, dict) else None
    if not isinstance(line, dict) or not {'committed', 'detail', 'sha', 'seal'} <= set(line):
        raise Unusable(f'receipt {path} is malformed')
    for name, want in (('request', request), ('parent', parent), ('scope', paths)):
        if saved.get(name) != want:
            raise Unusable(f'receipt {path} is for another {name}: {json.dumps(saved.get(name))[:200]}')
    facts = {k: line[k] for k in ('committed', 'detail', 'sha')}
    if sealed(facts)['seal'] != line['seal']:
        raise Unusable(f'receipt {path} does not match its seal')
    return {'request': request, 'parent': parent, 'scope': paths, **facts}


def patch_ids(*revs):
    """{commit: git's stable patch id} of the non-merge commits `git log revs` lists, one pipeline for them all;
    a commit with no patch (an empty one) has none."""
    log = git('log', '--no-merges', '-p', '--format=commit %H', *revs)
    status, out, err = attempt('git', 'patch-id', '--stable', input=log)
    if status:
        raise Unusable(f'git patch-id: {err.strip()}')
    return {commit: pid for pid, commit in (line.split() for line in out.splitlines() if line)}


def cited(head, shas):
    """#361: our fix notes cite repair commits; a rewrite may have replaced them."""
    head, found, ranges = full(head), [], {}
    for sha in map(full, shas):
        reachable, replacement = None, None
        if not attempt('git', 'cat-file', '-e', f'{sha}^{{commit}}')[0]:
            reachable = run('git', 'merge-base', '--is-ancestor', sha, head, ok=(0, 1))[0] == 0
        mine = reachable is False and patch_ids('-1', sha).get(sha)
        fork = mine and run('git', 'merge-base', sha, head, ok=(0, 1))[1].strip()
        if fork:  # none for unrelated histories, which share no range to search
            if fork not in ranges:
                ranges[fork] = patch_ids(f'{fork}..{head}')
            same = [c for c, pid in ranges[fork].items() if pid == mine]
            replacement = same[0] if len(same) == 1 else None
        found.append({'sha': sha, 'reachable': reachable, 'replacement': replacement})
    return {'head': head, 'cited': found}


def collect(argv):
    if len(argv) > 5 and argv[:2] == ['commit', '--receipt'] and argv[3] == '--request':
        return make(argv[5:], argv[2], argv[4])
    if len(argv) > 1 and argv[0] == 'commit':
        return make(argv[1:])
    if len(argv) > 6 and argv[0] == 'receipt' and argv[2] == '--request' and argv[4] == '--parent':
        return read_receipt(argv[1], argv[3], full(argv[5]), argv[6:])
    if len(argv) > 3 and argv[:2] == ['head', '--parent']:
        return head(argv[2], argv[3:])
    if len(argv) == 9 and argv[0] == 'chain' and argv[3] == '--published' and argv[5] == '--base' and argv[7] == '--tip':
        return chain(argv[1], argv[2], argv[4], argv[6], argv[8])
    if len(argv) > 2 and argv[0] == 'cited':
        return cited(argv[1], argv[2:])
    raise Unusable('usage: commits.py commit [--receipt FILE --request R] PATH... | commits.py receipt FILE --request R --parent P PATH... | commits.py head --parent P PATH... | commits.py chain FROM TO --published P --base B --tip T | commits.py cited HEAD SHA...')


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
