#!/usr/bin/env python3
"""Build one side of pr-babysit's build comparison and report what ran.

  build_compare.py candidate --path P... --command CMD
  build_compare.py base --rev SHA [--setup CMD] --command CMD

Pass values as --flag=VALUE when one may start with a dash. Run from the
checkout's top level. `candidate` builds the checkout as it stands,
uncommitted changes included, and names the state of PATH by `snapshot`, a
sha256 over `git diff --binary HEAD -- PATH` and every untracked file under
PATH, taken before the build and again after it as `snapshotAfter`: a build
that rewrote the candidate's sources shows as two different digests. `base`
adds a detached temporary worktree at SHA, runs SETUP there (the dependency
preparation a fresh checkout of the repository needs), builds, and removes the
worktree; the checkout itself is not touched. Each side gets a
fresh build directory, which `<BUILD>` in CMD or SETUP names, removed
afterwards; the log of both commands is kept.

stdout ends with one JSON line {side, revision, snapshot, snapshotAfter,
command, setup, buildDir, setupExit, exit, log, cleanup: {ok, retained,
error}}: revision is HEAD or SHA, both snapshots null for the base,
command and setup with <BUILD> expanded, setupExit null without SETUP, exit the build's status or
null when the setup failed and the build never ran. Exit 0 with that line,
whatever the build did; exit 2 with {"error": ...} when the side could not be
set up, naming anything cleanup had to leave behind. A SETUP bash cannot parse
is refused before anything runs, with an error starting "setup is not a shell
command".
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, checkout_top, git, report  # noqa: E402

LOGS = Path(tempfile.gettempdir()) / 'pr-babysit-builds'


def snapshot(paths):
    """The uncommitted state of paths, as one digest."""
    h = hashlib.sha256()
    diff = subprocess.run(['git', '--literal-pathspecs', 'diff', '--binary', 'HEAD', '--', *paths], capture_output=True)
    if diff.returncode != 0:
        raise Unusable(f"git diff: {diff.stderr.decode(errors='replace').strip()}")
    h.update(diff.stdout)
    for f in sorted(p for p in git('--literal-pathspecs', 'ls-files', '--others', '--exclude-standard', '-z', '--', *paths).split('\0') if p):
        h.update(f.encode() + b'\0')
        h.update(Path(f).read_bytes() if Path(f).is_file() else b'')
    return h.hexdigest()


def shell(cmd, cwd, log):
    with open(log, 'ab') as out:
        out.write(f'$ {cmd}\n'.encode())
        out.flush()
        return subprocess.run(['bash', '-c', cmd], cwd=cwd, stdout=out, stderr=subprocess.STDOUT).returncode


def collect(argv):
    p = Parser(prog='build_compare.py')
    p.add_argument('side', choices=['candidate', 'base'])
    p.add_argument('--rev')
    p.add_argument('--setup')
    p.add_argument('--path', action='append', default=[])
    p.add_argument('--command', required=True)
    a = p.parse_args(argv)
    if a.side == 'base' and not (a.rev and FULL_SHA.match(a.rev)):
        raise Unusable('base needs --rev <full SHA>')
    if a.side == 'candidate' and (a.rev or a.setup):
        raise Unusable('candidate builds the checkout as it stands: no --rev or --setup')
    if (a.side == 'candidate') != bool(a.path):
        raise Unusable('--path names the candidate\'s sources, and only the candidate\'s')
    if a.setup:
        parsed = subprocess.run(['bash', '-n', '-c', a.setup], capture_output=True, text=True)
        if parsed.returncode:
            raise Unusable(f'setup is not a shell command: {parsed.stderr.strip() or a.setup}')
    top = checkout_top()

    LOGS.mkdir(exist_ok=True)
    fd, log = tempfile.mkstemp(prefix=f'{a.side}-', suffix='.log', dir=LOGS)
    os.close(fd)
    build = tempfile.mkdtemp(prefix='pr-babysit-build-')
    tree = None

    def cleanup():
        errors, retained = [], []
        shutil.rmtree(build, ignore_errors=True)
        if os.path.exists(build):
            retained.append(build)
        if tree and os.path.exists(tree):
            try:
                git('worktree', 'remove', '--force', tree)
            except Unusable as e:
                errors.append(str(e))
            if os.path.exists(tree):
                retained.append(tree)
        return {'ok': not errors and not retained, 'retained': retained, 'error': '; '.join(errors) or None}

    expand = (lambda cmd: cmd.replace('<BUILD>', build))
    setup_exit = build_exit = snap = after = None
    try:
        if a.side == 'candidate':
            revision, snap, cwd = git('rev-parse', 'HEAD').strip(), snapshot(a.path), top
        else:
            revision = a.rev
            tree = tempfile.mkdtemp(prefix='pr-babysit-base-')
            os.rmdir(tree)  # git worktree add creates it
            git('worktree', 'add', '--detach', tree, a.rev)
            cwd = tree
            if a.setup:
                setup_exit = shell(expand(a.setup), cwd, log)
        if setup_exit in (None, 0):
            build_exit = shell(expand(a.command), cwd, log)
        if a.side == 'candidate':
            after = snapshot(a.path)
    except (Unusable, OSError) as e:
        c = cleanup()
        raise Unusable(str(e) if c['ok'] else f"{e}; cleanup left {c['retained']} {c['error'] or ''}".rstrip())
    return {'side': a.side, 'revision': revision, 'snapshot': snap, 'snapshotAfter': after, 'command': expand(a.command),
            'setup': expand(a.setup) if a.setup else None, 'buildDir': build, 'setupExit': setup_exit, 'exit': build_exit, 'log': log, 'cleanup': cleanup()}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
