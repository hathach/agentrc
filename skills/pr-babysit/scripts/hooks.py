#!/usr/bin/env python3
"""Run the repository's pre-commit hooks on the given paths and report what they did.

  hooks.py PATH...

Run from the checkout's top level. Prints the tree status and a snapshot of
PATH plus every path the status names, runs `pre-commit run --files PATH...`
when .pre-commit-config.yaml exists (once more if it exits non-zero), then the
status and snapshot again. Status lines are `git status --porcelain -z`
records; snapshot lines are `<644|755> <blob> <path>` or `absent - <path>`.

stdout ends with one JSON line {ran, passed, modifiedBy, before, after,
snapshotBefore, snapshotAfter}: passed is the last run's exit status,
modifiedBy the ids, across both runs, of the hooks pre-commit itself said
modified files. Without a config: ran false, passed true, modifiedBy [].
Exit 0 with that line; exit 2 with {"error": ...} when the facts cannot be
collected or pre-commit's output contradicts its exit status.
"""

import json
import os
import re
import subprocess
import sys

CONFIG = '.pre-commit-config.yaml'
HOOK_ID = '- hook id: '
MODIFIED = '- files were modified by this hook'
# pre-commit pads a hook's name with dots to its result, then prints the
# hook's `- ` block only when it failed or runs verbose (commands/run.py).
RESULT = re.compile(r'\.(Passed|Failed)$')


class Unusable(Exception):
    pass


def git(*argv):
    done = subprocess.run(['git', *argv], capture_output=True)
    if done.returncode:
        raise Unusable(f"git {' '.join(argv)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout.decode()


def status():
    return [r for r in git('status', '--porcelain', '-z').split('\0') if r]


def status_paths(records):
    """Every path the records name, a rename's or copy's source included."""
    paths, i = [], 0
    while i < len(records):
        paths.append(records[i][3:])
        if records[i][0] in 'RC':
            i += 1
            paths.append(records[i])
        i += 1
    return paths


def snapshot(paths, records):
    lines = []
    for f in sorted(set(paths) | set(status_paths(records))):
        if not os.path.lexists(f):
            lines.append(f'absent - {f}')
        elif not os.path.isdir(f):
            mode = '755' if os.access(f, os.X_OK) else '644'
            lines.append(f"{mode} {git('hash-object', '--', f).strip()} {f}")
    return lines


def modified_by(output, exit_code):
    """Ids of the hooks whose own `- ` block says they modified files.

    A block counts only directly under a result line; the hook's output follows
    the block after a blank line, so a marker it echoes is not read as one."""
    lines = output.splitlines()
    ids = []
    for i, line in enumerate(lines):
        if not (line.startswith(HOOK_ID) and i and RESULT.search(lines[i - 1])):
            continue
        hook = line[len(HOOK_ID):].strip()
        failed = lines[i - 1].endswith('Failed')
        block = []
        for tail in lines[i + 1:]:
            if not tail.startswith('- '):
                break
            block.append(tail)
        if not hook:
            raise Unusable(f'pre-commit printed an empty hook id under: {lines[i - 1]}')
        if failed and not exit_code:
            raise Unusable(f'pre-commit exited 0 but reported hook {hook} failed')
        if MODIFIED in block:
            if not failed:
                raise Unusable(f'pre-commit reported hook {hook} passed and modified files')
            ids.append(hook)
    return ids


def run_hooks(paths):
    if not os.path.exists(CONFIG):
        return False, True, []
    ids = []
    for _ in range(2):
        try:
            done = subprocess.run(['pre-commit', 'run', '--color', 'never', '--files', *paths],
                                  capture_output=True, text=True, errors='replace')
        except FileNotFoundError:
            raise Unusable(f'{CONFIG} exists but pre-commit is not installed')
        ids += [h for h in modified_by(done.stdout, done.returncode) if h not in ids]
        if not done.returncode:
            break
    return True, not done.returncode, ids


def collect(paths):
    top = git('rev-parse', '--show-toplevel').strip()
    if os.path.realpath(top) != os.path.realpath('.'):
        raise Unusable(f'run from the checkout top level {top}, not {os.getcwd()}')
    before = status()
    snapshot_before = snapshot(paths, before)
    ran, passed, ids = run_hooks(paths)
    after = status()
    return {'ran': ran, 'passed': passed, 'modifiedBy': ids, 'before': before, 'after': after,
            'snapshotBefore': snapshot_before, 'snapshotAfter': snapshot(paths, after)}


def main(argv):
    if not argv:
        print(json.dumps({'error': 'usage: hooks.py PATH...'}))
        return 2
    try:
        facts = collect(argv)
    except Unusable as e:
        print(json.dumps({'error': str(e)}))
        return 2
    print(json.dumps(facts))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
