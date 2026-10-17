#!/usr/bin/env python3
"""Build pr-babysit's fix batch as the checkout stands and report what ran.

  build.py --path P... --command CMD [--setup LINES] [--require TOOL ...]

Pass values as --flag=VALUE when one may start with a dash. Run from the
checkout's top level. The checkout is built as it stands, uncommitted changes
included, in a fresh build directory that `<BUILD>` in CMD names, removed
afterwards; the log is kept. `snapshot` names the state of PATH, a sha256 over
`git diff --binary HEAD -- PATH` and every untracked file under PATH, taken
before the build and again after it as `snapshotAfter`: a build that rewrote
the batch's sources shows as two different digests.

One shell runs SETUP (the environment the build contract requires, such as
sourcing a toolchain's export script), checks that each TOOL is on PATH, then
runs CMD, so the build sees what setup exported; `stage` names the step the
shell last entered: `setup`, `prerequisite` or `build`. A shell that stopped
before `build` never built: its environment is missing, whatever its status.

stdout ends with one JSON line {revision, snapshot, snapshotAfter, command,
setup, requires, stage, buildDir, exit, log, retained, seal} (seal:
facts.sealed): revision is HEAD, command with <BUILD> expanded, setup and
requires as given ('' and [] without them), exit the shell's status,
retained what cleanup had to leave behind. Exit 0 with that line, whatever the build did; exit 2 with
{"error": ...} when the build could not be set up, naming anything cleanup had
to leave behind.
"""

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, checkout_top, git, report  # noqa: E402

LOGS = Path(tempfile.gettempdir()) / 'pr-babysit-builds'
STAGES = ('setup', 'prerequisite', 'build')


def script(setup, requires, command):
    """One shell for every step, each announcing itself in $PR_BABYSIT_STAGE first."""
    mark = lambda stage: f'echo {stage} > "$PR_BABYSIT_STAGE"\n'
    tools = ''.join(f'command -v {shlex.quote(t)} >/dev/null || {{ echo "missing tool: "{shlex.quote(t)}; exit 127; }}\n'
                    for t in requires)
    # setup runs bare, never as a condition, so a `set -e` in it still stops the shell
    return (mark('setup') + (f'{setup}\npr_babysit_setup=$?; [ "$pr_babysit_setup" -eq 0 ] || exit "$pr_babysit_setup"\n' if setup else '') +
            mark('prerequisite') + tools + mark('build') + command + '\n')


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


def bash():
    """'bash'; on Windows bash.exe from PATH alone: not the cwd, the PR's checkout, nor System32's WSL launcher."""
    if os.name != 'nt':
        return 'bash'
    wsl = os.path.normcase(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'bash.exe'))
    for d in os.environ.get('PATH', '').split(os.pathsep):
        path = os.path.join(d, 'bash.exe')
        if os.path.isabs(d) and os.path.isfile(path) and os.path.normcase(os.path.normpath(path)) != wsl:
            return path
    raise Unusable('no bash.exe on PATH (System32 holds only the WSL launcher)')


def collect(argv):
    p = Parser(prog='build.py')
    p.add_argument('--path', action='append', required=True)
    p.add_argument('--command', required=True)
    p.add_argument('--setup')
    p.add_argument('--require', action='append', default=[])
    a = p.parse_args(argv)
    sh = bash()
    # <BUILD> goes into the command unquoted: a temp dir with a blank or shell syntax in it would split or run
    if os.name == 'nt' and re.search(r'[\s\'"$`;&|<>()*?[\]{}!#]', Path(tempfile.gettempdir()).as_posix()):
        raise Unusable(f'the temp dir {tempfile.gettempdir()} holds a blank or shell syntax: point TMP and TEMP at a plain path')
    top = checkout_top()

    LOGS.mkdir(exist_ok=True)
    fd, log = tempfile.mkstemp(prefix='candidate-', suffix='.log', dir=LOGS)
    os.close(fd)
    fd, stage_file = tempfile.mkstemp(prefix='stage-', dir=LOGS)
    os.close(fd)
    build = Path(tempfile.mkdtemp(prefix='pr-babysit-build-')).as_posix()  # C:/... survives an unquoted <BUILD>

    def cleanup():
        shutil.rmtree(build, ignore_errors=True)
        Path(stage_file).unlink(missing_ok=True)
        return [build] if os.path.exists(build) else []

    command = a.command.replace('<BUILD>', build)
    try:
        revision, snap = git('rev-parse', 'HEAD').strip(), snapshot(a.path)
        shell = script(a.setup, a.require, command)
        with open(log, 'ab') as out:
            out.write(''.join(f'$ {line}\n' for line in shell.splitlines()).encode())
            out.flush()
            build_exit = subprocess.run([sh, '-c', shell], cwd=top, stdout=out, stderr=subprocess.STDOUT,
                                        env={**os.environ, 'PR_BABYSIT_STAGE': stage_file}).returncode
        stage = Path(stage_file).read_text().strip()
        if stage not in STAGES:
            raise Unusable(f'the build shell left no stage: {stage!r}')
        after = snapshot(a.path)
    except (Unusable, OSError) as e:
        retained = cleanup()
        raise Unusable(f'{e}; cleanup left {retained}' if retained else str(e))
    return {'revision': revision, 'snapshot': snap, 'snapshotAfter': after, 'command': command,
            'setup': a.setup or '', 'requires': a.require, 'stage': stage,
            'buildDir': build, 'exit': build_exit, 'log': Path(log).as_posix(), 'retained': cleanup()}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
