#!/usr/bin/env python3
"""Build pr-babysit's fix batch as the checkout stands and report what ran.

  build.py --path P... --command CMD

Pass values as --flag=VALUE when one may start with a dash. Run from the
checkout's top level. The checkout is built as it stands, uncommitted changes
included, in a fresh build directory that `<BUILD>` in CMD names, removed
afterwards; the log is kept. `snapshot` names the state of PATH, a sha256 over
`git diff --binary HEAD -- PATH` and every untracked file under PATH, taken
before the build and again after it as `snapshotAfter`: a build that rewrote
the batch's sources shows as two different digests.

stdout ends with one JSON line {revision, snapshot, snapshotAfter, command,
buildDir, exit, log, retained, seal} (seal: facts.sealed): revision is
HEAD, command with <BUILD> expanded, exit the build's status, retained what
cleanup had to leave behind. Exit 0 with that line, whatever the build did; exit 2 with
{"error": ...} when the build could not be set up, naming anything cleanup had
to leave behind.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, checkout_top, git, report  # noqa: E402

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


def collect(argv):
    p = Parser(prog='build.py')
    p.add_argument('--path', action='append', required=True)
    p.add_argument('--command', required=True)
    a = p.parse_args(argv)
    top = checkout_top()

    LOGS.mkdir(exist_ok=True)
    fd, log = tempfile.mkstemp(prefix='candidate-', suffix='.log', dir=LOGS)
    os.close(fd)
    build = tempfile.mkdtemp(prefix='pr-babysit-build-')

    def cleanup():
        shutil.rmtree(build, ignore_errors=True)
        return [build] if os.path.exists(build) else []

    command = a.command.replace('<BUILD>', build)
    try:
        revision, snap = git('rev-parse', 'HEAD').strip(), snapshot(a.path)
        with open(log, 'ab') as out:
            out.write(f'$ {command}\n'.encode())
            out.flush()
            build_exit = subprocess.run(['bash', '-c', command], cwd=top, stdout=out, stderr=subprocess.STDOUT).returncode
        after = snapshot(a.path)
    except (Unusable, OSError) as e:
        retained = cleanup()
        raise Unusable(f'{e}; cleanup left {retained}' if retained else str(e))
    return {'revision': revision, 'snapshot': snap, 'snapshotAfter': after, 'command': command,
            'buildDir': build, 'exit': build_exit, 'log': log, 'retained': cleanup()}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
