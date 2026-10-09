#!/usr/bin/env python3
"""Collect every finding-verifier verdict a pr-review run journaled into replayable cases, one JSON line each.

A case is the prompt code-audit's verify unit was given and the model that answered it, both read from the
unit's own transcript, with the run's pinned head and what the unit returned. Runs resumed or replayed
journal the same verdict again, without a transcript of their own: a case is keyed by its head and prompt,
and the first one with a transcript is kept. A verdict without a pinned head (the run's `check` result),
without its transcript, or whose prompt names another head is not replayable and is only counted.

A scanner sometimes reported an absolute path into the run's worktree; the verifier read that path. Such a
case keeps the prompt as sent and names the `worktree` prefix, which a replay replaces with its own
checkout of the head, and `path`, the file relative to the repository root. The prefix is the one whose
encoding is the session's project directory name, which Claude Code derives from its working directory; a
path with no such prefix, or a run in a repository not in REPOS, leaves the case unreplayable.

    extract.py --out cases.jsonl [JOURNAL_ROOT ...]   # default: ~/.claude/projects, ~/.claude-ada/projects
"""
import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

VERIFY = re.compile(r'verify:d\d+x\d+:\d+')
# code-audit.js's verify prompt; its scope sentence is there only for a diff-scoped run
PROMPT = re.compile(r'Adversarially verify ONE review finding about (?P<dir>.*?)\.\nDimension: (?P<dimension>.*?)\nFinding: (?P<finding>[^\n]*)\n', re.S)
SCOPE = re.compile(r'The checkout is at [0-9a-f]{40}\. Judge only what `git diff ([0-9a-f]{40}) ([0-9a-f]{40})')
HARNESS = 'The computed task text follows:\n'
REPOS = {'tinyusb': 'hathach/tinyusb', 'agentrc': 'hathach/agentrc'}


def events(path):
    with path.open(errors='replace') as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def results(path):
    """{label: (first result, the agent that produced it)} of one journal."""
    started, out = {}, {}
    for e in events(path):
        if e.get('type') == 'started':
            started[e.get('key')] = (e.get('label') or '', e.get('agentId'))
        elif e.get('type') == 'result' and e.get('key') in started:
            label, agent = started[e['key']]
            out.setdefault(label, (e.get('result'), agent))
    return out


def origin(where):
    """(owner/repo, PR number) from a pr-review worktree path or its Claude project directory name, or Nones."""
    repo = next((r for name, r in REPOS.items() if re.search(rf'(^|[-/]){name}([-/]|$)', where)), None)
    pr = re.search(r'pr-review-(\d+)$', where.rstrip('/'))
    return repo, int(pr.group(1)) if pr else None


def worktree_of(file, project):
    """The prefix of an absolute path that is the session's working directory, or None without exactly one."""
    parts = file.split('/')
    roots = ['/'.join(parts[:i]) for i in range(2, len(parts)) if re.sub(r'[^A-Za-z0-9]', '-', '/'.join(parts[:i])) == project]
    return roots[0] if len(roots) == 1 else None


def transcript(path):
    """(the task prompt as the agent got it, the models that answered), or None without one."""
    if not path.is_file():
        return None
    prompt, models = None, []
    for e in events(path):
        message = e.get('message') or {}
        if e.get('type') == 'user' and prompt is None:
            content = message.get('content')
            text = content if isinstance(content, str) else ''.join(b.get('text', '') for b in content or [] if isinstance(b, dict))
            if HARNESS in text:  # the harness frames a computed task and indents its every line by two spaces
                text = '\n'.join(l[2:] if l.startswith('  ') else l for l in text.split(HARNESS, 1)[1].split('\n'))
            prompt = text
        elif e.get('type') == 'assistant' and message.get('model') and message['model'] not in models:
            models.append(message['model'])
    return prompt, models


def cases(path, skipped):
    """The replayable cases of one journal; each verdict that is not counts its reason in `skipped`."""
    got = results(path)
    check = got.get('check', ({},))[0] or {}
    head, project = check.get('head'), path.parents[4].name
    # an older check result names no worktree: the session's project directory still does
    repo, pr = origin(check.get('top') or project)
    for label, (verdict, agent) in got.items():
        if not VERIFY.fullmatch(label) or verdict is None:
            continue
        if not head or not repo:
            skipped['no pinned head' if not head else 'repository not in REPOS'] += 1
            continue
        seen = transcript(path.parent / f'agent-{agent}.jsonl')
        if not seen:
            skipped['no transcript'] += 1
            continue
        prompt, models = seen
        m = PROMPT.search(prompt)
        if not m:
            skipped['unparsable prompt'] += 1
            continue
        # the scope sentence follows the finding, whose own text may quote another diff
        scope = SCOPE.search(prompt, m.end())
        if scope and scope.group(2) != head:
            skipped['prompt names another head'] += 1
            continue
        finding = json.loads(m.group('finding'))
        file, worktree = finding['file'], None
        if file.startswith('/'):
            worktree = worktree_of(file, project)
            if not worktree:
                skipped['absolute path outside the session worktree'] += 1
                continue
            file = file[len(worktree) + 1:]
        yield {
            'id': hashlib.sha1(json.dumps([repo, head, prompt]).encode()).hexdigest()[:12],
            'repo': repo, 'pr': pr, 'head': head, 'base': scope.group(1) if scope else None,
            'dir': m.group('dir'), 'dimension': m.group('dimension'), 'finding': finding,
            'path': file, 'worktree': worktree,
            'prompt': prompt, 'verifier': {'models': models, **verdict},
            'source': {'journal': str(path), 'label': label},
        }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('roots', nargs='*', type=Path,
                    default=[Path.home() / '.claude' / 'projects', Path.home() / '.claude-ada' / 'projects'])
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args(argv)
    seen, skipped = {}, collections.Counter()
    # one projects tree may be reachable under two config directories
    for root in dict.fromkeys(r.resolve() for r in a.roots):
        for path in sorted(root.glob('*/*/subagents/workflows/*/journal.jsonl')):
            for c in cases(path, skipped):
                seen.setdefault(c['id'], c)
    with a.out.open('w') as f:
        for c in seen.values():
            f.write(json.dumps(c) + '\n')
    real = sum(1 for c in seen.values() if c['verifier'].get('real'))
    print(f'{len(seen)} cases ({real} confirmed, {len(seen) - real} refuted) -> {a.out}')
    for reason, n in skipped.most_common():
        print(f'not replayable: {n} verdict(s), {reason}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
