"""Transport primitives for herdr-core's CLI, `hc` (0.1.14), shared by the scripts that talk to hc terminals
(herdr-peer, captain). Judgment stays with the caller: these only ask hc, classify its answer, and name processes.

Every call takes an explicit deadline in seconds. hc prints its result as JSON on stdout and `{error: {kind,
message}}` on stderr; options go before `--`, so a terminal name or a text never reads as one. A malformed answer,
a timeout or an unknown failure stays a failure (Error), never an empty result; only `get` turns exit 3 with kind
not_found into None. `read` returns hc's plain text as is.

A script reaches it through its resolved path: `sys.path.insert(0, str(Path(__file__).resolve().parents[N] /
'lib'))`, then `import hc`."""
import json
import os
import subprocess
import sys

import windows

NOT_FOUND = 3   # hc's exit code for an unknown terminal
# hc send error kinds raised before anything is typed, and a failure to start hc at all. Anything else, a timeout
# and a success that does not say the program started working, may have typed: uncertain.
NOT_TYPED = ('blocked', 'status_mismatch', 'not_found', 'finished', 'spawn_failed')
SENT, NOT_TYPED_OUTCOME, UNCERTAIN = 'sent', 'not-typed', 'uncertain'


class Error(Exception):
    """hc could not be asked, or answered something unusable; or /proc could not be read."""


class Gone(Exception):
    """The process hc named has exited."""


class Unverified(Exception):
    """This process cannot be tied to an hc terminal: HC_TERMINAL_ID is unset, or names a terminal whose foreground
    process is not one of this process's ancestors."""


def program():
    """hc to launch: 'hc', or on Windows the hc.exe PATH alone finds, never one in the working directory, which
    CreateProcess tries first; None when there is none."""
    return windows.on_path('hc', ('.exe',)) if os.name == 'nt' else 'hc'


def run(*args, timeout, caller=None):
    """(exit status, parsed stdout or None, error kind or None, message) of `hc args`; exit None with kind 'timeout'
    past the deadline, or 'spawn_failed' when hc did not start (not on PATH, an argument past the OS limit). caller,
    a verified terminal address, is passed as HC_TERMINAL_ID, which hc takes as the caller (the rest of the
    environment as is): a shell that dropped it, as Codex's tool shells do, would otherwise send untagged."""
    env = {**os.environ, 'HC_TERMINAL_ID': caller} if caller else None
    exe = program()
    if exe is None:
        return None, None, 'spawn_failed', f'hc {args[0]} did not start: no hc.exe on PATH'
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, None, 'timeout', f'hc {args[0]} gave no answer within {timeout} s'
    except OSError as e:
        return None, None, 'spawn_failed', f'hc {args[0]} did not start: {e}'
    try:
        body = json.loads(out.stdout) if out.stdout.strip() else None
    except ValueError:
        body = None
    try:
        err = json.loads(out.stderr).get('error')
    except (ValueError, AttributeError):
        err = None
    if not isinstance(err, dict):   # no error object, or a malformed one: the kind stays unknown
        err = {}
    kind, message = err.get('kind'), err.get('message')
    msg = (message if isinstance(message, str) else '') or out.stderr.strip() or out.stdout.strip()[:200]
    return out.returncode, body, kind if isinstance(kind, str) else None, msg or f'exit {out.returncode}'


def call(*args, timeout):
    """hc's JSON object for `hc args` (pass --json yourself); Error on anything else."""
    rc, body, kind, msg = run(*args, timeout=timeout)
    if rc != 0 or not isinstance(body, dict):
        raise Error(f'hc {" ".join(args[:1])} failed: {msg if rc != 0 else "no JSON object in its answer"}')
    return body


def get(address, *, timeout):
    """`hc get` of one terminal, None only when hc knows none by that address (exit 3, kind not_found)."""
    rc, body, kind, msg = run('get', '--json', '--', address, timeout=timeout)
    if rc == NOT_FOUND and kind == 'not_found':
        return None
    if rc != 0 or not isinstance(body, dict):
        raise Error(f'hc get {address} failed: {msg if rc != 0 else "no JSON object in its answer"}')
    return body


def terminals(*, timeout):
    """The records of `hc ls`: live terminals, local and remote."""
    found = call('ls', '--json', timeout=timeout).get('terminals')
    if not isinstance(found, list) or not all(isinstance(t, dict) for t in found):
        raise Error('hc ls answered without a terminal list')
    return found


def read(address, *, timeout, lines=None):
    """`hc read`: the terminal's screen and history as plain text, unaltered."""
    exe = program()
    if exe is None:
        raise Error('hc read did not start: no hc.exe on PATH')
    try:
        out = subprocess.run([exe, 'read', *(['--lines', str(lines)] if lines else []), '--', address],
                             capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Error(f'hc read {address} gave no answer within {timeout} s') from None
    except OSError as e:
        raise Error(f'hc read did not start: {e}') from None
    if out.returncode != 0:
        raise Error(f'hc read {address} failed: {(out.stderr or out.stdout).strip()}')
    return out.stdout


def send(address, text, *, timeout, only_if=None, was_working=False, caller=None):
    """Paste text into address and press Enter: (SENT | NOT_TYPED_OUTCOME | UNCERTAIN, detail); only_if ('idle' or
    'working') is hc's --if. SENT needs exit 0 and a JSON object with `started: true`: hc 0.1.14's schema defines
    SendResult.started as "whether it went `working` within a few seconds; null when it doesn't report status", and
    says nothing of a program already working, which has no such change to show. So with was_working (the status the
    caller read just before) the object alone counts, unless its `status`, the program's status "when the text went
    in", says it had stopped working by then: started is meaningful again. caller is run's: into an agent hc wraps
    the text as <hc from="<caller> ...">, untagged without one, which reads as a person's input."""
    rc, body, kind, msg = run('send', '--json', *(['--if', only_if] if only_if else []), '--', address, text,
                              timeout=timeout, caller=caller)
    if rc == 0:
        if isinstance(body, dict) and (body.get('started') is True
                                       or was_working and body.get('status') in ('working', None)):
            return SENT, body
        return UNCERTAIN, f'hc send answered {body!r}, not started: true'
    return (NOT_TYPED_OUTCOME if kind in NOT_TYPED else UNCERTAIN), f'{kind or "unknown"}: {msg}'


def is_local(address):
    """Whether address names a terminal on this machine: remote ones read machine/t7."""
    return isinstance(address, str) and bool(address) and '/' not in address


def proc_start(pid):
    """pid's start time in clock ticks since boot (/proc/<pid>/stat field 22), which with the pid names one process
    across pid reuse; None off Linux. Gone once it has exited; Error when /proc cannot be read otherwise."""
    if not sys.platform.startswith('linux'):
        return None
    try:
        with open(f'/proc/{pid}/stat') as f:
            return int(f.read().rsplit(')', 1)[1].split()[19])
    except (FileNotFoundError, ProcessLookupError):
        raise Gone(pid) from None
    except (OSError, IndexError, ValueError) as e:
        raise Error(f'cannot inspect /proc/{pid}/stat: {e!r}') from None


def ancestors(pid):
    """pid and every process above it (Linux; [pid] elsewhere)."""
    chain = [pid]
    while sys.platform.startswith('linux') and pid > 1:
        try:
            with open(f'/proc/{pid}/stat') as f:
                pid = int(f.read().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        chain.append(pid)
    return chain


def foreground(info):
    """{pid, start, cwd} of a get record's foreground process; None when it has none or it has exited."""
    fg = info.get('foreground') if info and info.get('state') == 'live' else None
    if not isinstance(fg, dict) or not isinstance(fg.get('pid'), int):
        return None
    try:
        return {'pid': fg['pid'], 'start': proc_start(fg['pid']), 'cwd': fg.get('cwd')}
    except Gone:
        return None


def own_terminal(*, timeout):
    """(address, get record, foreground) of the terminal this process runs in. HC_TERMINAL_ID, which any process can
    set, is only a hint: on Linux its foreground process must be this process or one of its ancestors. Without the
    hint (Codex's tool shells drop it) the terminal is found by ancestry instead: the one live local terminal whose
    foreground process is an ancestor. Off Linux, without /proc, the hint is taken as given and none is a refusal."""
    address = os.environ.get('HC_TERMINAL_ID')
    linux = sys.platform.startswith('linux')
    if not address:
        if not linux:
            raise Unverified('HC_TERMINAL_ID is unset, and without /proc this process cannot be traced to an hc '
                             'terminal')
        return by_ancestry(timeout)
    if not is_local(address):
        raise Unverified(f'HC_TERMINAL_ID={address} is not a local hc terminal')
    info = get(address, timeout=timeout)
    fg = foreground(info)
    if fg is None:
        raise Unverified(f'hc has no live foreground process for this terminal {address}')
    if linux and fg['pid'] not in ancestors(os.getpid()):
        raise Unverified(f'HC_TERMINAL_ID={address}, but its foreground process {fg["pid"]} does not run this one')
    return address, info, fg


def by_ancestry(timeout):
    chain = set(ancestors(os.getpid()))
    mine = []
    for t in terminals(timeout=timeout):
        address = t.get('address')
        if is_local(address) and t.get('state') == 'live':
            info = get(address, timeout=timeout)
            fg = foreground(info)
            if fg and fg['pid'] in chain:
                mine.append((address, info, fg))
    if len(mine) != 1:
        raise Unverified('HC_TERMINAL_ID is unset and ' + (
            f'{len(mine)} hc terminals ({", ".join(m[0] for m in mine)}) run this process' if mine
            else 'no live local hc terminal runs this process: not inside hc'))
    return mine[0]
