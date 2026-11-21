#!/usr/bin/env python3
"""Build pr-babysit's fix batch as the checkout stands and report what ran.

  build.py --path P... --command CMD [--setup LINES] [--require TOOL ...] [--shell bash|powershell]
           [--job DIR --wait-seconds S --timeout-seconds T [--abandon-seconds A]]

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
The shell is bash (on Windows bash.exe from PATH), or with --shell powershell
Windows PowerShell from System32 (pwsh off Windows), SETUP and CMD then
PowerShell code, for a Windows host that runs no bash: a failed cmdlet stops
the shell wherever it is, and a step's status is otherwise its last native
program's.

stdout ends with one JSON line {revision, snapshot, snapshotAfter, command,
setup, requires, stage, buildDir, exit, usageError, log, retained, seal} (seal:
facts.sealed): revision is HEAD, command with <BUILD> expanded, setup and
requires as given ('' and [] without them), exit the shell's status,
retained what cleanup had to leave behind. usageError is the line a command
that refused its own arguments printed, else null: one that exited 2 in the
build step and opened its output with argparse's `usage:` (its `<prog>: error:`
line reported when it printed one) or Python's `can't open file`, or printed an
`error:` line (`error: ...` or `<prog>: error: ...`) naming one of the
command's own options, as check_build.py's `error: --worktree needs --base`
for `--worktree`; it built nothing, so its failure says nothing of the change
(#557). A compiler's `error:`, such as rustc's or `clang: error: no such
file`, names no option of the command and is the build's.
Exit 0 with that line, whatever the build did; exit 2 with {"error": ...} when
the build could not be set up, naming anything cleanup had to leave behind.

With --job, the build outlives the call that asks for it, so a caller whose
shell calls are bounded waits it out in several: DIR names it, the first call
starts it in a process of its own and every call, the first included, waits up
to S seconds for it. A call that sees it finish prints its line with `state`
`done`, or `timedOut` when it ran past T seconds and was killed (exit is then
the stopped shell's status, which on Windows, whose tree kill may end the
shell's children before it, can be 0); one that does not prints {state: running,
revision, elapsed, seal}, and the next call with the same arguments and DIR
waits again. A build no call has waited on for A seconds (900) is killed, so a
caller that stops asking leaves nothing running. A stopped build takes every
process below it along, one in a session of its own included (on Linux; on
other POSIX hosts its process group, on Windows its process tree), and one
still running after is an error that no later call builds over while it runs. DIR
holds one request: other arguments, or another HEAD, are an error; a finished
build whose HEAD or sources have changed since, or that was abandoned, is set
aside and built again.
"""

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from commits import bounded  # noqa: E402
from facts import Parser, Unusable, checkout_top, git, report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import windows  # noqa: E402

LOGS = Path(tempfile.gettempdir()) / 'pr-babysit-builds'
STAGES = ('setup', 'prerequisite', 'build')
ABANDON = 900  # seconds without a waiting call before a --job build is killed
ALIVE = 60  # seconds the runner's heartbeat may age before it counts as gone
TICK = 0.2  # seconds between the runner's and a waiting call's looks
STOP_GRACE = 10  # seconds a stopped build gets after SIGTERM, then after SIGKILL
REQUEST_WAIT = 10  # seconds an attaching call waits for the claiming call's request.json
PR_SET_CHILD_SUBREAPER = 36  # prctl(2)
# The line the shell prints as the build step starts: what follows is the command's own output.
BUILD_MARK = '--- pr-babysit build.py: build ---'
USAGE_EXIT = 2  # argparse's, and Python's for a script it cannot open
USAGE_ERROR = re.compile(r'(?:[\w.-]+: )?error: ')
NO_SCRIPT = re.compile(r".+: can't open file '")
OPTION = re.compile(r'--?[A-Za-z][\w-]*')  # a command's `-x` or `--xx` word, short of any `=value`
USAGE_LINES = 20  # how far into the output a usage error's `error:` line is looked for


def script(setup, requires, command):
    """One shell for every step, each announcing itself in $PR_BABYSIT_STAGE first."""
    mark = lambda stage: f'echo {stage} > "$PR_BABYSIT_STAGE"\n'
    tools = ''.join(f'command -v {shlex.quote(t)} >/dev/null || {{ echo "missing tool: "{shlex.quote(t)}; exit 127; }}\n'
                    for t in requires)
    # setup runs bare, never as a condition, so a `set -e` in it still stops the shell
    return (mark('setup') + (f'{setup}\npr_babysit_setup=$?; [ "$pr_babysit_setup" -eq 0 ] || exit "$pr_babysit_setup"\n' if setup else '') +
            mark('prerequisite') + tools + mark('build') + f'echo {shlex.quote(BUILD_MARK)}\n' + command + '\n')


def ps_quote(s):
    """A PowerShell single-quoted literal: ' and the curly single quotes PowerShell also takes for it, each doubled."""
    return "'" + re.sub("['‘’‚‛]", lambda m: m[0] * 2, s) + "'"


def ps_script(setup, requires, command):
    """script() for Windows PowerShell: the same steps and marks. A failed cmdlet stops the shell with status 1
    wherever it is ($ErrorActionPreference Stop: no later success masks it); a step's status is otherwise its last
    native program's, as bash's is its last command's."""
    mark = lambda stage: ("$ErrorActionPreference = 'Stop'\n" if stage == 'setup' else '') + \
        f"[IO.File]::WriteAllText($env:PR_BABYSIT_STAGE, '{stage}')\n"
    # $? first: any statement, the `if` reading LASTEXITCODE too, resets it
    status = ('$pr_babysit_ok = $?\nif ($LASTEXITCODE) { exit $LASTEXITCODE }\n'
              'if (-not $pr_babysit_ok) { exit 1 }\n')
    tools = ''.join(f"if (-not (Get-Command {ps_quote(t)} -ErrorAction SilentlyContinue)) "
                    f"{{ Write-Output ('missing tool: ' + {ps_quote(t)}); exit 127 }}\n" for t in requires)
    return (mark('setup') + ('$global:LASTEXITCODE = 0\n' + f'{setup}\n' + status if setup else '') +
            mark('prerequisite') + tools + mark('build') + f'Write-Output {ps_quote(BUILD_MARK)}\n' +
            '$global:LASTEXITCODE = 0\n' + command + '\n' + status + 'exit 0\n')


def usage_error(log, stage, build_exit, command):
    """The line command printed in log refusing its own arguments, else None."""
    if stage != 'build' or build_exit != USAGE_EXIT:
        return None
    lines = Path(log).read_text(encoding='utf-8', errors='replace').splitlines()
    # the header shows the marker only inside its `$ echo` line
    printed = lines[lines.index(BUILD_MARK) + 1:] if BUILD_MARK in lines else []
    out = [t for t in (bounded(line).strip() for line in printed) if t]
    if not out:
        return None
    errors = [t for t in out[:USAGE_LINES] if USAGE_ERROR.match(t)]
    if out[0].startswith('usage: '):
        return next(iter(errors), out[0])
    if NO_SCRIPT.match(out[0]):
        return out[0]
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    options = {m[0] for w in words if (m := OPTION.match(w))}
    return next((t for t in errors if any(re.search(rf'(?<![\w-]){re.escape(o)}(?![\w-])', t) for o in options)), None)


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
    if found := windows.bash():
        return found
    raise Unusable('no bash.exe on PATH (System32 holds only the WSL launcher)')


def powershell():
    if found := windows.powershell():
        return found
    raise Unusable('no Windows PowerShell in System32' if os.name == 'nt' else 'no pwsh on PATH')


def prepare(a):
    """Everything a build needs before its shell runs, as one JSON-able record."""
    sh = powershell() if a.shell == 'powershell' else bash()
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
    b = {'sh': sh, 'top': top, 'log': log, 'stageFile': stage_file, 'buildDir': build, 'paths': a.path,
         'command': a.command.replace('<BUILD>', build), 'setup': a.setup or '', 'requires': a.require}
    if a.shell == 'powershell':
        b['shell'] = ps_script(a.setup, a.require, b['command'])
        # a file, -File: -Command would take the script's own quotes as the command line's; a BOM, which Windows
        # PowerShell 5.1 needs to read the file as UTF-8
        fd, ps1 = tempfile.mkstemp(prefix='build-', suffix='.ps1', dir=LOGS)
        with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='\n') as f:
            f.write(b['shell'])
        b['argv'] = [sh, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ps1]
    else:
        b['shell'] = script(a.setup, a.require, b['command'])
        b['argv'] = [sh, '-c', b['shell']]
    try:
        b['revision'], b['snapshot'] = git('rev-parse', 'HEAD').strip(), snapshot(a.path)
        with open(log, 'ab') as out:
            out.write(''.join(f'$ {line}\n' for line in b['shell'].splitlines()).encode())
    except (Unusable, OSError) as e:
        fail(b, e)
    return b


def cleanup(b):
    shutil.rmtree(b['buildDir'], ignore_errors=True)
    Path(b['stageFile']).unlink(missing_ok=True)
    if b['argv'][-2] == '-File':
        Path(b['argv'][-1]).unlink(missing_ok=True)
    return [b['buildDir']] if os.path.exists(b['buildDir']) else []


def fail(b, e):
    retained = cleanup(b)
    raise Unusable(f'{e}; cleanup left {retained}' if retained else str(e))


def spawn(b, **kw):
    with open(b['log'], 'ab') as out:
        return subprocess.Popen(b['argv'], cwd=b['top'], stdout=out, stderr=subprocess.STDOUT,
                                env={**os.environ, 'PR_BABYSIT_STAGE': b['stageFile']}, **kw)


def finish(b, build_exit):
    try:
        stage = Path(b['stageFile']).read_text().strip()
        if stage not in STAGES:
            raise Unusable(f'the build shell left no stage: {stage!r}')
        after = snapshot(b['paths'])
        usage = usage_error(b['log'], stage, build_exit, b['command'])
    except (Unusable, OSError) as e:
        fail(b, e)
    return {'revision': b['revision'], 'snapshot': b['snapshot'], 'snapshotAfter': after, 'command': b['command'],
            'setup': b['setup'], 'requires': b['requires'], 'stage': stage, 'buildDir': b['buildDir'], 'exit': build_exit,
            'usageError': usage, 'log': Path(b['log']).as_posix(), 'retained': cleanup(b)}


def parse(argv):
    p = Parser(prog='build.py')
    p.add_argument('--path', action='append', required=True)
    p.add_argument('--command', required=True)
    p.add_argument('--setup')
    p.add_argument('--require', action='append', default=[])
    p.add_argument('--shell', choices=('bash', 'powershell'), default='bash')
    p.add_argument('--job')
    p.add_argument('--wait-seconds', type=int)
    p.add_argument('--timeout-seconds', type=int)
    p.add_argument('--abandon-seconds', type=int, default=ABANDON)
    p.add_argument('--stop-grace-seconds', type=int, default=STOP_GRACE, help=argparse.SUPPRESS)  # tests only
    a = p.parse_args(argv)
    if (a.job is None) != (a.wait_seconds is None) or (a.job is None) != (a.timeout_seconds is None):
        raise Unusable('usage: --job, --wait-seconds and --timeout-seconds go together')
    if a.job is not None and (a.wait_seconds < 0 or a.timeout_seconds < 1 or a.stop_grace_seconds < 1):
        raise Unusable('usage: --wait-seconds must be >= 0, --timeout-seconds and --stop-grace-seconds >= 1')
    return a


def collect(argv):
    a = parse(argv)
    if a.job is not None:
        return job(a)
    b = prepare(a)
    try:
        build_exit = spawn(b).wait()
    except OSError as e:
        fail(b, e)
    return finish(b, build_exit)


def subreaper():
    """Make every orphaned descendant this process's child (Linux), so a stop still finds one that left the build's
    session; elsewhere the build's process group is all a stop reaches."""
    with contextlib.suppress(OSError, AttributeError, TypeError):
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)


def below():
    """{pid: (ppid, state)} of every process below this one, from /proc ({} without it)."""
    procs = {}
    for stat in Path('/proc').glob('[0-9]*/stat'):
        with contextlib.suppress(OSError, IndexError, ValueError):
            fields = stat.read_text(errors='replace').rsplit(')', 1)[1].split()
            procs[int(stat.parent.name)] = (int(fields[1]), fields[0])
    children = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    found, frontier = {}, [os.getpid()]
    while frontier:
        for pid in children.get(frontier.pop(), []):
            if pid not in found:
                found[pid] = procs[pid]
                frontier.append(pid)
    return found


def kill(proc, grace):
    """Stop proc's shell and every process below this runner, one in a session of its own included; the pids that
    still run after."""
    if os.name == 'nt':
        return [] if windows.kill_tree(proc, 30) is not None else [proc.pid]
    # SIGKILL after the grace whatever went: a child that ignores SIGTERM outlives the shell
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        for pid in below():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        end = time.monotonic() + grace
        while time.monotonic() < end and (proc.poll() is None or running(proc)):
            time.sleep(0.25)
    return running(proc)


def running(proc):
    """The live processes below this one, after reaping the dead ones left to it but proc, whose status is Popen's."""
    procs = below()
    for pid, (ppid, state) in procs.items():
        if state == 'Z' and ppid == os.getpid() and pid != proc.pid:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
    return [pid for pid, (_, state) in procs.items() if state != 'Z']


def write_json(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value), encoding='utf-8')
    windows.replace(tmp, path)


def age(path):
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return float('inf')


def run_job(d):
    """The detached runner: build what d/request.json holds, beating d/alive, then write d/receipt.json."""
    d = Path(d)
    req = json.loads((d / 'request.json').read_text(encoding='utf-8'))
    b = req['build']
    deadline = req['started'] + req['request']['timeoutSeconds']
    state, left = 'done', []
    try:
        if os.name != 'nt':
            subreaper()
        group = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        proc = spawn(b, **group)
        while proc.poll() is None:
            (d / 'alive').touch()
            if time.time() > deadline:
                state = 'timedOut'
            elif age(d / 'poll') > req['abandon']:
                state = 'abandoned'
            if state != 'done':
                left = kill(proc, req['stopGrace'])
                break
            time.sleep(TICK)
        # Nothing is waited on or removed under a process that outlived its stop: it may still use them.
        if not left:
            try:
                code = proc.wait(ALIVE)
            except subprocess.TimeoutExpired:
                left = [proc.pid]
        if left:
            raise Unusable(f'the build was stopped {state}, and process(es) {left} still run; '
                           f'its build dir {Path(b["buildDir"]).as_posix()} and log {Path(b["log"]).as_posix()} are left to them')
        out = {**finish(b, code), 'state': state}
    except (Unusable, OSError) as e:
        # survivors: what a later call checks before it builds again
        out = {'error': str(e), **({'survivors': left} if left else {})}
    write_json(d / 'receipt.json', out)


def start(d, a):
    """Claim d, record the request and start its runner; d exists already when another call claimed it first."""
    d.mkdir()
    try:
        b = prepare(a)
    except BaseException:
        d.rmdir()
        raise
    req = {'request': request_of(a, b['revision']), 'build': b, 'started': time.time(), 'abandon': a.abandon_seconds,
           'stopGrace': a.stop_grace_seconds}
    write_json(d / 'request.json', req)
    (d / 'alive').touch()
    (d / 'poll').touch()
    detach = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
              else {'start_new_session': True})
    with open(d / 'runner.log', 'ab') as err:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--run-job', str(d)], cwd=b['top'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, close_fds=True, **detach)
    return req


def request_of(a, revision):
    return {'paths': a.path, 'command': a.command, 'setup': a.setup or '', 'requires': a.require,
            'timeoutSeconds': a.timeout_seconds, 'revision': revision, **({'shell': a.shell} if a.shell != 'bash' else {})}


def alive(pid):
    if os.name == 'nt':
        return windows.alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # another user's: it exists
        pass
    return True


def stale(receipt, paths):
    """Why a finished build no longer speaks for the checkout, or None; a stop whose survivors still run is never
    built over: its error stands until they are gone."""
    if 'error' in receipt:
        return None if any(alive(p) for p in receipt.get('survivors', [])) else receipt['error']
    if receipt['state'] == 'abandoned':
        return 'no call waited on it'
    return 'the sources changed since' if snapshot(paths) != receipt['snapshotAfter'] else None


def job(a):
    end = time.monotonic() + a.wait_seconds
    d = Path(a.job)
    d.parent.mkdir(parents=True, exist_ok=True)
    head = git('rev-parse', 'HEAD').strip()
    try:
        req = start(d, a)
    except FileExistsError:
        req = attached(d, a, head)
    while True:
        (d / 'poll').touch()
        if (d / 'receipt.json').exists():
            out = json.loads((d / 'receipt.json').read_text(encoding='utf-8'))
            if 'error' in out:
                raise Unusable(out['error'])
            return out
        elapsed = int(time.time() - req['started'])
        if age(d / 'alive') > ALIVE:
            raise Unusable(f'the runner of {d.as_posix()} stopped without a receipt after {elapsed} s')
        if time.monotonic() >= end:
            return {'state': 'running', 'revision': req['request']['revision'], 'elapsed': elapsed}
        time.sleep(TICK)


def attached(d, a, head):
    """The request another call started in d, after checking it is this one; a finished one gone stale is set aside."""
    end = time.monotonic() + REQUEST_WAIT  # the claiming call writes request.json right after its mkdir
    while not (d / 'request.json').exists():
        if time.monotonic() >= end:
            raise Unusable(f'{d.as_posix()} holds no build request')
        time.sleep(TICK)
    req = json.loads((d / 'request.json').read_text(encoding='utf-8'))
    if req['request'] != request_of(a, head):
        raise Unusable(f'{d.as_posix()} holds another build request: {json.dumps(req["request"])}')
    if (d / 'receipt.json').exists() and (why := stale(json.loads((d / 'receipt.json').read_text(encoding='utf-8')), a.path)):
        d.rename(d.with_name(f'{d.name}.{time.time_ns()}.old'))
        print(f'build.py: {d.as_posix()} set aside: {why}', file=sys.stderr)
        return start(d, a)
    return req


if __name__ == '__main__':
    if sys.argv[1:2] == ['--run-job']:
        sys.exit(run_job(sys.argv[2]))
    sys.exit(report(collect, sys.argv[1:], seal=True))
