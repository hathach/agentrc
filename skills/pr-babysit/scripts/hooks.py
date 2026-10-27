#!/usr/bin/env python3
"""Run the repository's pre-commit hooks on the given paths and report what they did.

  hooks.py [--deadline-seconds N] PATH...

Run from the checkout's top level. Prints the tree status and a snapshot of
PATH plus every path the status names, runs `pre-commit run --files PATH...`
when .pre-commit-config.yaml exists (once more if it failed after a hook
modified files), then the status and snapshot again. Both runs together get N
seconds (default 540, inside the 600 a relay's Bash call allows); past that
the pre-commit process tree is killed and the result is an error. Status lines are `git status --porcelain -z`
records, each untracked file its own; snapshot lines are `<644|755> <blob> <path>` or `absent - <path>`.

stdout ends with one JSON line {ran, passed, modifiedBy, before, after,
snapshotBefore, snapshotAfter, seal} (seal: facts.sealed): passed is the last run's exit status,
modifiedBy the ids, across both runs, of the hooks pre-commit itself said
modified files. Without a config: ran false, passed true, modifiedBy [].
Exit 0 with that line; exit 2 with {"error": ...} when the facts cannot be
collected or pre-commit's output contradicts its exit status.
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Unusable, checkout_top, git, report  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import windows  # noqa: E402

CONFIG = '.pre-commit-config.yaml'
DEADLINE = 540
HOOK_ID = '- hook id: '
MODIFIED = '- files were modified by this hook'
# pre-commit pads a hook's name with dots to its result, then prints the
# hook's `- ` block only when it failed or runs verbose (commands/run.py).
RESULT = re.compile(r'\.(Passed|Failed)$')


def status():
    # -uall: a new directory's files, not one `?? dir/` record no file scope can hold (#322)
    return [r for r in git('status', '--porcelain', '-z', '--untracked-files=all').split('\0') if r]


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


def index_mode(f):
    """The mode git add gives f with core.fileMode false, Windows' default (true there is unsupported): a tracked
    file keeps its index mode, a new one is 644. A symlink (120000) checked out as a file stays 644, as os.access
    reports it on POSIX, so it never matches ls-tree's 000 and its commit is refused."""
    for entry in git('--literal-pathspecs', 'ls-files', '-s', '-z', '--', f).split('\0'):
        meta, _, path = entry.partition('\t')
        if path == f and meta.startswith('100'):
            return meta[3:6]
    return '644'


def snapshot(paths, records):
    lines = []
    for f in sorted(set(paths) | set(status_paths(records))):
        if not os.path.lexists(f):
            lines.append(f'absent - {f}')
        elif not os.path.isdir(f):
            mode = index_mode(f) if os.name == 'nt' else '755' if os.access(f, os.X_OK) else '644'
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


def kill_tree(proc):
    if os.name == 'nt':
        windows.kill_tree(proc, 10)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def pre_commit(paths, deadline, start, attempt):
    """(exit code, stdout) of one run, its whole process tree killed once `deadline` seconds from `start` pass."""
    group = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    try:
        proc = subprocess.Popen(['pre-commit', 'run', '--color', 'never', '--files', *paths], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding='utf-8', errors='replace', **group)
    except FileNotFoundError:
        raise Unusable(f'{CONFIG} exists but pre-commit is not installed')
    try:
        out, _ = proc.communicate(timeout=max(deadline - (time.monotonic() - start), 0))
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        late = f'pre-commit did not finish within {deadline} s (run {attempt})'
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A descendant in another session outlived the kill and still holds the output pipe.
            raise Unusable(f'{late}; a process it started survived the kill and may still be running')
        raise Unusable(f'{late}; its process group was killed, though a process that left it may survive')
    except BaseException:
        # Its own process group spares the hooks the caller's Ctrl-C or kill, so they go down with this run.
        kill_tree(proc)
        raise
    return proc.returncode, out


def run_hooks(paths, deadline=DEADLINE):
    if not os.path.exists(CONFIG):
        return False, True, []
    ids = []
    start = time.monotonic()
    for attempt in (1, 2):
        code, out = pre_commit(paths, deadline, start, attempt)
        modified = modified_by(out, code)
        ids += [h for h in modified if h not in ids]
        # A rerun repairs what a hook rewrote; the same failure again only spends another pass (#140).
        if not code or not modified:
            break
    return True, not code, ids


def collect(argv):
    deadline = DEADLINE
    if argv[:1] == ['--deadline-seconds']:
        if len(argv) < 2 or not argv[1].isdigit() or int(argv[1]) < 1:
            raise Unusable('usage: hooks.py [--deadline-seconds N] PATH...')
        deadline, argv = int(argv[1]), argv[2:]
    paths = argv
    if not paths:
        raise Unusable('usage: hooks.py [--deadline-seconds N] PATH...')
    checkout_top()
    before = status()
    snapshot_before = snapshot(paths, before)
    ran, passed, ids = run_hooks(paths, deadline)
    after = status()
    return {'ran': ran, 'passed': passed, 'modifiedBy': ids, 'before': before, 'after': after,
            'snapshotBefore': snapshot_before, 'snapshotAfter': snapshot(paths, after)}


if __name__ == '__main__':
    # SIGTERM unwinds like Ctrl-C, through pre_commit's kill of the hook group.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    sys.exit(report(collect, sys.argv[1:], seal=True))
