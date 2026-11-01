#!/usr/bin/env python3
"""Run the repository's pre-commit hooks on the given paths and report what they did.

  hooks.py [--deadline-seconds N] PATH...

Run from the checkout's top level. Prints the tree status and a snapshot of
PATH plus every path the status names, runs `pre-commit run --files PATH...`
when .pre-commit-config.yaml exists (once more if it failed after a hook
modified files), then the status and snapshot again. All runs, the HEAD reruns below included, get N
seconds (default 540, inside the 600 a relay's Bash call allows); past that
the pre-commit process tree is killed and the result is an error. Status lines are `git status --porcelain -z`
records, each untracked file its own; snapshot lines are `<644|755> <blob> <path>` or `absent - <path>`.

stdout ends with one JSON line {ran, passed, modifiedBy, failed, baselineFailed, before, after,
snapshotBefore, snapshotAfter, seal} (seal: facts.sealed): passed is the last run's exit status,
modifiedBy the ids, across both runs, of the hooks pre-commit itself said
modified files. Without a config: ran false, passed true, and the lists empty.
failed and baselineFailed explain a failure: failed, {hook, error} for each hook the last
run failed, error its first output line; and baselineFailed, the ids among
those that, rerun alone in a detached worktree of HEAD on the same PATHs, fail
there too, so they are no evidence against the change (#378). A hook whose
last failure was modifying files is never rerun there, and each rerun gets a
clean HEAD tree. When every failure is excused, the hooks run once more on the
change with SKIP naming the excused ones, so a fail_fast stop cannot hide a
hook the change breaks; what fails there joins failed, and a failure no hook
owns excuses nothing. pre-commit runs with core.abbrev pinned (#377).
Exit 0 with that line; exit 2 with {"error": ...} when the facts cannot be
collected or pre-commit's output contradicts its exit status.
"""

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Unusable, checkout_top, git, report, status  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import windows  # noqa: E402

CONFIG = '.pre-commit-config.yaml'
DEADLINE = 540
HOOK_ID = '- hook id: '
MODIFIED = '- files were modified by this hook'
# pre-commit pads a hook's name with dots to its result, then prints the
# hook's `- ` block only when it failed or runs verbose (commands/run.py).
RESULT = re.compile(r'\.(Passed|Failed)$')
# Any hook's result line, `(no files to check)Skipped` included: where the hook output above it ends.
NEXT = re.compile(r'[.)](Passed|Failed|Skipped)$')
ERROR_CHARS = 200
# Seconds past the deadline the HEAD worktree's removal may take.
CLEANUP = 20


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


def first_error(lines, block):
    """The first line with a word the hook printed, up to the next hook's result, skipping rules such as unittest's
    `=====`; its last `- ` line when it printed none."""
    for line in lines:
        if NEXT.search(line):
            break
        if re.search(r'[^\W_]', line):
            return line.strip()[:ERROR_CHARS]
    return block[-1][2:] if block else 'no output'


def results(output, exit_code):
    """(ids of the hooks whose own `- ` block says they modified files, {hook, error} of each failed hook).

    A block counts only directly under a result line; the hook's output follows
    the block after a blank line, so a marker it echoes is not read as one."""
    lines = output.splitlines()
    ids, failures = [], []
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
        if failed:
            failures.append({'hook': hook, 'error': first_error(lines[i + 1 + len(block):], block)})
    return ids, failures


def kill_tree(proc):
    if os.name == 'nt':
        windows.kill_tree(proc, 10)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def pinned_abbrev(env):
    """env with core.abbrev at full length. pre-commit decides a hook modified files by comparing `git diff` text
    before and after it; a hook that rewrites a file unchanged can lengthen the unstaged diff's abbreviated `index`
    hashes at the object store's abbrev boundary, and that alone failed it (#377)."""
    count = env.get('GIT_CONFIG_COUNT') or '0'
    if not count.isdigit():
        raise Unusable(f'GIT_CONFIG_COUNT is {count!r}, not a count')
    n = int(count)
    return {**env, 'GIT_CONFIG_COUNT': str(n + 1), f'GIT_CONFIG_KEY_{n}': 'core.abbrev', f'GIT_CONFIG_VALUE_{n}': '40'}


def bounded(argv, deadline, start, what, cwd=None, env=None):
    """(exit code, stdout, stderr) of argv, its whole process tree killed once `deadline` seconds from `start` pass."""
    group = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
                            errors='replace', cwd=cwd, env=env, **group)
    try:
        out, err = proc.communicate(timeout=max(deadline - (time.monotonic() - start), 0))
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        late = f'{argv[0]} did not finish within {deadline} s ({what})'
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
    return proc.returncode, out, err


def pre_commit(paths, deadline, start, what, hook=None, cwd=None, skip=()):
    """(exit code, stdout) of one pre-commit run, skip added to the SKIP it inherits."""
    env = pinned_abbrev(os.environ)
    if skip:
        env['SKIP'] = ','.join([*filter(None, env.get('SKIP', '').split(',')), *skip])
    try:
        code, out, _ = bounded(['pre-commit', 'run', '--color', 'never', *([hook] if hook else []), '--files', *paths],
                               deadline, start, what, cwd, env)
    except FileNotFoundError:
        raise Unusable(f'{CONFIG} exists but pre-commit is not installed')
    return code, out


def git_within(deadline, start, *argv):
    """git argv, held to the same deadline as the hooks: a checkout filter or hook of its own can hang it."""
    code, _, err = bounded(['git', *argv], deadline, start, f'git {argv[0]}')
    if code:
        raise Unusable(f"git {' '.join(argv)}: {err.strip()}")


def baseline(hooks, paths, deadline, start):
    """The ids among hooks that fail on HEAD too: each rerun alone on the same paths in a detached worktree of HEAD,
    so what the host lacks (a compiler, a codepage, /dev/null) fails it there as well, without the change.
    Only the hook's own Failed block counts, never pre-commit refusing an id HEAD's config does not have."""
    failing = []
    with tempfile.TemporaryDirectory(prefix='hooks-base-', ignore_cleanup_errors=True) as tmp:
        tree = os.path.join(tmp, 'head')
        git_within(deadline, start, 'worktree', 'add', '--detach', '--quiet', tree, 'HEAD')
        try:
            for i, hook in enumerate(hooks):
                if i:
                    # What an earlier rerun rewrote or left behind could fail the next one: each gets HEAD as is.
                    git_within(deadline, start, '-C', tree, 'reset', '--quiet', '--hard', 'HEAD')
                    git_within(deadline, start, '-C', tree, 'clean', '-qffdx')
                code, out = pre_commit(paths, deadline, start, f'HEAD {hook}', hook=hook, cwd=tree)
                if hook in (f['hook'] for f in results(out, code)[1]):
                    failing.append(hook)
        finally:
            try:
                git_within(deadline + CLEANUP, start, 'worktree', 'remove', '--force', tree)
            except Unusable:
                git('worktree', 'prune')
    return failing


def run_hooks(paths, deadline=DEADLINE):
    if not os.path.exists(CONFIG):
        return False, True, [], [], []
    ids = []
    start = time.monotonic()
    for attempt in (1, 2):
        code, out = pre_commit(paths, deadline, start, f'run {attempt}')
        modified, failures = results(out, code)
        ids += [h for h in modified if h not in ids]
        # A rerun repairs what a hook rewrote; the same failure again only spends another pass (#140).
        if not code or not modified:
            break
    # A hook still rewriting files is judged by what it wrote, never excused by HEAD.
    rerun = [f['hook'] for f in failures if f['hook'] not in modified]
    excused = baseline(rerun, paths, deadline, start) if rerun else []
    if excused and len(excused) == len(failures):
        # fail_fast may have stopped the run at an excused hook: every other hook must pass on the change, as the
        # commit will run them with the same SKIP.
        again, out = pre_commit(paths, deadline, start, 'without the excused', skip=excused)
        modified, others = results(out, again)
        ids += [h for h in modified if h not in ids]
        failures += others
        if again and not others:
            excused = []  # a failure no hook owns: nothing is excused
    return True, not code, ids, failures, excused


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
    ran, passed, ids, failures, base_failed = run_hooks(paths, deadline)
    after = status()
    return {'ran': ran, 'passed': passed, 'modifiedBy': ids, 'failed': failures, 'baselineFailed': base_failed,
            'before': before, 'after': after, 'snapshotBefore': snapshot_before, 'snapshotAfter': snapshot(paths, after)}


if __name__ == '__main__':
    # SIGTERM unwinds like Ctrl-C, through pre_commit's kill of the hook group.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    sys.exit(report(collect, sys.argv[1:], seal=True))
