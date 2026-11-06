#!/usr/bin/env python3
"""Pin an audit's range and report the repository's growth over it.

  audit.py start --repo PATH [--base REF] --head REF --since TIME --until TIME --out DIR
  audit.py finish --run DIR --end REF

start resolves the base (--base, else the previous completion marker: the one annotated `audit/*` tag whose
message starts with MARKER and whose commit descends from every other such tag reachable from head), writes
DIR/run.json and DIR/inventory.json, and prints the growth summary. finish checks the run's repository and range
again, writes DIR/finish-inventory.json and DIR/tag-message.txt, a draft whose completed, outcome and prs lines the
lead fills, and prints the summary; it creates no tag. --since/--until bound the transcript window usage.py reads
(ISO 8601). Output paths are create-only. Exit 0 done, 2 refused (with the reason on stderr).
"""
import argparse
import codecs
import datetime
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

MARKER = 'repo-audit-marker: 1'
TEST_DIRS = {'tests', 'test', 'fixtures'}
TEST_DEF = re.compile(r'^\s*(?:async\s+)?def test_\w+|^\s*(?:test|it)\(')
TOP = 25


class Refused(Exception):
    pass


def git(repo, *args, check=True):
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    if check and r.returncode:
        raise Refused(f'git {" ".join(args)}: {r.stderr.strip()}')
    return r


def commit(repo, ref):
    r = git(repo, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False)
    if r.returncode:
        raise Refused(f'{ref}: not a commit in {repo}')
    return r.stdout.strip()


def is_ancestor(repo, a, b):
    return git(repo, 'merge-base', '--is-ancestor', a, b, check=False).returncode == 0


def identity(repo):
    """The root commits: the same in every clone and worktree of the repository, unlike a path or a remote URL."""
    return sorted(git(repo, 'rev-list', '--max-parents=0', 'HEAD').stdout.split())


def markers(repo, head):
    """(qualifying (tag, commit) reachable from head, malformed audit/* names)."""
    fmt = '%(refname:short)%00%(objecttype)%00%(*objecttype)%00%(*objectname)%00%(contents)%00%00'
    out = git(repo, 'for-each-ref', f'--format={fmt}', 'refs/tags/audit/').stdout
    good, bad = [], []
    for rec in out.split('\0\0'):
        rec = rec.lstrip('\n')
        if not rec:
            continue
        name, kind, target_kind, target, contents = (rec.split('\0') + [''] * 5)[:5]
        if kind != 'tag' or target_kind != 'commit' or not valid_marker(contents, target):
            bad.append(name)
        elif is_ancestor(repo, target, head):
            good.append((name, target))
    return good, bad


def valid_marker(message, target):
    """The marker format finish drafts: MARKER first, then the previous end and this end, the tag's own commit."""
    lines = message.splitlines()
    fields = dict(ln.split(': ', 1) for ln in lines[1:] if ': ' in ln)
    return bool(lines) and lines[0] == MARKER and re.fullmatch(r'[0-9a-f]{40}', fields.get('previous', '')) \
        and fields.get('end') == target


def previous_marker(repo, head):
    good, bad = markers(repo, head)
    if bad:
        print(f'malformed audit/* tags ignored: {", ".join(bad)}', file=sys.stderr)
    if not good:
        raise Refused('no completion marker reachable from head: pass --base')
    latest = [t for t in good if all(is_ancestor(repo, o[1], t[1]) for o in good)]
    if len({c for _, c in latest}) != 1:
        raise Refused(f'no single latest marker among {", ".join(n for n, _ in good)}: pass --base')
    return latest[0]


def is_test_path(path):
    p = PurePosixPath(path)
    return bool(TEST_DIRS & set(p.parts[:-1])) or p.name.startswith('test_') or '.test.' in p.name


def area(path):
    parts = PurePosixPath(path).parts
    return '/'.join(parts[:2])


def tree(repo, rev):
    """{path: bytes} of every tracked blob at rev."""
    out = git(repo, 'ls-tree', '-r', '-l', '-z', rev).stdout
    files = {}
    for rec in out.split('\0'):
        if not rec:
            continue
        meta, path = rec.split('\t', 1)
        kind, size = meta.split()[1], meta.split()[3]
        if kind == 'blob':
            files[path] = int(size)
    return files


def numstat(repo, base, end):
    """[(added|None, deleted|None, path)] with None for a binary file; a rename counts under its new path."""
    out = git(repo, 'diff', '--numstat', '-z', '-M', base, end).stdout.split('\0')
    rows, i = [], 0
    while i < len(out):
        rec = out[i]
        if not rec:
            i += 1
            continue
        a, d, path = rec.split('\t', 2)
        if not path:            # rename: the old and new paths follow as their own fields
            path = out[i + 2]
            i += 2
        rows.append((None if a == '-' else int(a), None if d == '-' else int(d), path))
        i += 1
    return rows


def shortstat(rows):
    """git diff --shortstat's line from numstat rows: a binary file counts as changed, its lines do not."""
    if not rows:
        return ''
    ins = sum(a for a, _, _ in rows if a is not None)
    dels = sum(d for _, d, _ in rows if d is not None)
    parts = [f'{len(rows)} file{"s" * (len(rows) != 1)} changed']
    if ins or not dels:
        parts.append(f'{ins} insertion{"s" * (ins != 1)}(+)')
    if dels or not ins:
        parts.append(f'{dels} deletion{"s" * (dels != 1)}(-)')
    return ', '.join(parts)


def diff_path(text):
    """A path from a ---/+++ header: None for /dev/null, git's C-style quoting undone."""
    text = text.rstrip('\t')         # git ends a header naming a path with a space in a tab
    if text == '/dev/null':
        return None
    if text.startswith('"'):
        text = codecs.escape_decode(text[1:-1].encode())[0].decode()
    return text[2:]


def count_test_defs(repo, base, end):
    """{test file: [added, removed]} counting test function heads in the diff of test paths."""
    out = git(repo, '-c', 'core.quotePath=false', 'diff', '-U0', '--no-color', '--no-ext-diff', '--no-textconv',
              '-M', '--src-prefix=a/', '--dst-prefix=b/', base, end).stdout
    counts, old, path = defaultdict(lambda: [0, 0]), None, None
    for line in out.splitlines():
        if line.startswith('--- '):
            old = diff_path(line[4:])
        elif line.startswith('+++ '):
            path = diff_path(line[4:]) or old       # a deleted file's removals count under its old path
        elif path and is_test_path(path) and line[:1] in '+-' and TEST_DEF.match(line[1:]):
            counts[path][line[0] == '-'] += 1
    return counts


def big_commits(repo, base, end):
    out = git(repo, 'log', '--first-parent', '-m', '--format=%x00%h %s', '--shortstat', f'{base}..{end}').stdout
    rows = []
    for rec in out.split('\0')[1:]:
        head, _, stat = rec.partition('\n')
        ins = re.search(r'(\d+) insertion', stat)
        dels = re.search(r'(\d+) deletion', stat)
        rows.append((int(ins.group(1)) if ins else 0, int(dels.group(1)) if dels else 0, head.strip()))
    return sorted(rows, reverse=True)


def inventory(repo, base, end):
    before, after = tree(repo, base), tree(repo, end)
    stat = numstat(repo, base, end)
    areas = defaultdict(lambda: [0, 0])
    binary = []
    for a, d, path in stat:
        if a is None:
            binary.append(path)
            continue
        areas[area(path)][0] += a
        areas[area(path)][1] += d
    sizes = Counter()
    for files, sign in ((after, 1), (before, -1)):
        for path, size in files.items():
            sizes[area(path)] += sign * size
    prose = {p: after.get(p, 0) - before.get(p, 0) for p in set(before) | set(after)
             if p.endswith('.md') and not is_test_path(p)}
    tests = count_test_defs(repo, base, end)
    return {
        'base': base, 'end': end,
        'commits': int(git(repo, 'rev-list', '--count', f'{base}..{end}').stdout),
        'shortstat': shortstat(stat),
        'files': [len(before), len(after)],
        'bytes': [sum(before.values()), sum(after.values())],
        'test_bytes': [sum(s for p, s in before.items() if is_test_path(p)),
                       sum(s for p, s in after.items() if is_test_path(p))],
        'prose_bytes': [sum(s for p, s in before.items() if p.endswith('.md') and not is_test_path(p)),
                        sum(s for p, s in after.items() if p.endswith('.md') and not is_test_path(p))],
        'areas': sorted(([a, d, k] for k, (a, d) in areas.items()), reverse=True),
        'area_bytes': sorted(([v, k] for k, v in sizes.items() if v), reverse=True),
        'binary_changed': sorted(binary),
        'largest': sorted(([s, p] for p, s in after.items()), reverse=True)[:TOP],
        'prose_growth': sorted(([after.get(p, 0), g, p] for p, g in prose.items() if g), key=lambda r: -abs(r[1])),
        'tests_added_removed': sorted(([a, r, p] for p, (a, r) in tests.items()), reverse=True),
        'largest_commits': big_commits(repo, base, end)[:TOP],
    }


def summary(inv):
    t = inv['tests_added_removed']
    lines = [
        f"{inv['base'][:12]}..{inv['end'][:12]}: {inv['commits']} commits, {inv['shortstat']}",
        f"files {inv['files'][0]} -> {inv['files'][1]}, bytes {inv['bytes'][0]} -> {inv['bytes'][1]}, "
        f"test bytes {inv['test_bytes'][0]} -> {inv['test_bytes'][1]}, "
        f"non-test markdown {inv['prose_bytes'][0]} -> {inv['prose_bytes'][1]}",
        f"test functions +{sum(r[0] for r in t)} -{sum(r[1] for r in t)} (heads matched by pattern)",
        '== lines added/deleted by area', *(f'{a:7d} {d:7d} {k}' for a, d, k in inv['areas'][:TOP]),
        '== byte change by area', *(f'{v:+9d} {k}' for v, k in inv['area_bytes'][:TOP]),
        '== largest files', *(f'{s:9d} {p}' for s, p in inv['largest']),
        '== non-test markdown growth (bytes now, change)', *(f'{s:7d} {g:+7d} {p}' for s, g, p in inv['prose_growth'][:TOP]),
        '== test functions added, removed', *(f'{a:5d} {r:5d} {p}' for a, r, p in t[:TOP]),
        '== largest commits (insertions, deletions)', *(f'{a:6d} {d:6d} {s}' for a, d, s in inv['largest_commits']),
    ]
    if inv['binary_changed']:
        lines.append(f"binary files changed: {len(inv['binary_changed'])}")
    return '\n'.join(lines)


def create(path, text, mode=0o666):
    """Write text to a new file, mode masked by the umask; FileExistsError when path exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)    # newlines translated once, by Python
    with os.fdopen(os.open(path, flags, mode), 'w', encoding='utf-8') as f:
        f.write(text)


def aware(text):
    """An ISO 8601 time, a naive one taken as local; ValueError when text is not one."""
    t = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    return t if t.tzinfo else t.astimezone()


def when(text):
    try:
        return aware(text)
    except ValueError:
        raise Refused(f'{text}: not an ISO 8601 time') from None


def start(args):
    repo = Path(git(args.repo, 'rev-parse', '--show-toplevel').stdout.strip())
    head = commit(repo, args.head)
    if args.base:
        base, marker = commit(repo, args.base), None
    else:
        marker, base = previous_marker(repo, head)
    if not is_ancestor(repo, base, head):
        raise Refused(f'base {base[:12]} is not an ancestor of head {head[:12]}')
    since, until = when(args.since), when(args.until)
    if since >= until:
        raise Refused('--since must be before --until')
    since, until = since.isoformat(), until.isoformat()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    status = git(repo, 'status', '--porcelain').stdout.splitlines()
    tool = Path(__file__).resolve().parent
    tool_rev = git(tool, 'rev-parse', 'HEAD', check=False).stdout.strip() or None
    run = {'format': 1, 'repo': str(repo), 'identity': identity(repo), 'base': base, 'marker': marker,
           'start_head': head, 'since': since, 'until': until, 'tool_revision': tool_rev,
           'dirty': sum(not s.startswith('??') for s in status), 'untracked': sum(s.startswith('??') for s in status)}
    inv = inventory(repo, base, head)
    create(out / 'run.json', json.dumps(run, indent=1) + '\n')
    create(out / 'inventory.json', json.dumps(inv, indent=1) + '\n')
    if run['dirty'] or run['untracked']:
        print(f"note: {run['dirty']} modified and {run['untracked']} untracked paths are not in this inventory")
    print(summary(inv))


def finish(args):
    out = Path(args.run)
    run = json.loads((out / 'run.json').read_text(encoding='utf-8'))
    repo = Path(run['repo'])
    if not repo.is_dir() or identity(repo) != run['identity']:
        raise Refused(f'{repo} is not the repository this run audited')
    end = commit(repo, args.end)
    for name in ('base', 'start_head'):
        if not is_ancestor(repo, run[name], end):
            raise Refused(f'end {end[:12]} does not descend from the run\'s {name} {run[name][:12]}')
    inv = inventory(repo, run['base'], end)
    msg = '\n'.join([MARKER, f"previous: {run['base']}", f'end: {end}', f"range: {inv['commits']} commits, "
                     f"{inv['shortstat']}", 'completed: <local time of acceptance>', 'outcome: <one line>',
                     'prs: <owner/repo#N ...>']) + '\n'
    create(out / 'finish-inventory.json', json.dumps(inv, indent=1) + '\n')
    create(out / 'tag-message.txt', msg)
    print(summary(inv))
    print(f'\ntag message draft: {out / "tag-message.txt"}')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('start')
    s.add_argument('--repo', required=True)
    s.add_argument('--base')
    s.add_argument('--head', required=True)
    s.add_argument('--since', required=True)
    s.add_argument('--until', required=True)
    s.add_argument('--out', required=True)
    f = sub.add_parser('finish')
    f.add_argument('--run', required=True)
    f.add_argument('--end', required=True)
    args = p.parse_args(argv)
    try:
        (start if args.cmd == 'start' else finish)(args)
    except (Refused, FileExistsError, FileNotFoundError) as e:
        print(f'refused: {e}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
