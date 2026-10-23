#!/usr/bin/env python3
"""Run a headless chief and keep a numbered log of its `chief:` status lines.

    chief_run.py --out DIR --worktree PATH --task-file FILE [--permission-mode MODE] [--foreground]

DIR must not exist. It receives stream.jsonl (every stdout line, raw), progress.log
(`<seq> <HH:MM:SS> <line>`: the launcher's own lines, chief's status lines and notes),
report.md (the final result's text), session (the session id), stderr.log and, when
the session's cost can be tabled, cost.md (run_cost.py's full output; its brief form,
the Launches and Usage tables, goes into report.md before chief's first `## ` section).
The exit line ends with that total, or `cost none (<why>)`.

A status line is the first line of one of chief's own messages (agents/chief.md);
a note is a progress note, joined onto one line, that chief's model returns as a `thinking`
block. It returns at once and leaves the launcher in its own session, so no caller's
command timeout ends the run; --foreground runs it in place. Windows has no fork: there
the launcher is this command run again, detached. Exit codes and usage are in SKILL.md.
"""
import argparse
import errno
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cost  # noqa: E402
from shadows import on_path, refusal  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import windows  # noqa: E402

MARKER = 'chief: '
HELD = []   # the run.lock this launcher holds until it exits
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0   # a detached launcher's console children

if os.name == 'nt':
    import msvcrt


def fail(msg):
    print(f'chief_run: {msg}', file=sys.stderr)
    sys.exit(2)


class Progress:
    def __init__(self, path):
        self.f = path.open('a+', encoding='utf-8', newline='\n')
        self.f.seek(0)
        self.seq = sum(1 for _ in self.f)   # a line appended after a failed launch keeps the count

    def line(self, text):
        self.seq += 1
        self.f.write(f'{self.seq} {time.strftime("%H:%M:%S")} {text}\n')
        self.f.flush()


def running_chiefs(worktree):
    """Pids of `--agent chief` processes whose working directory is the worktree:
    a second chief there would write the same checkout."""
    if os.name == 'nt':
        return []   # no /proc: lock_held covers the chiefs chief_run starts
    want, pids = worktree.resolve(), []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            agent = b'--agent=chief' in argv or any(
                argv[i] == b'--agent' and argv[i + 1] == b'chief' for i in range(len(argv) - 1))
            if agent and (proc / 'cwd').resolve(strict=True) == want:
                pids.append(int(proc.name))
        except OSError:
            continue   # gone, or not ours to read
    return pids


def lock_held(worktree, keep, wait=0.0):
    """Windows, where another process's cwd cannot be read: why the launch is refused when another launcher holds
    the worktree's `<git-path chief>/run.lock` or it cannot be taken, else None after taking it, kept until release()
    when keep. It sees only chiefs chief_run starts, not interactive ones; elsewhere, and outside a repository, None."""
    if os.name != 'nt':
        return None
    if (git := on_path('git', ('.exe',))) is None:
        return None   # as for git failing: no repository to hold the lock in
    try:
        got = subprocess.run([git, '-C', str(worktree), 'rev-parse', '--path-format=absolute', '--git-path', 'chief'],
                             capture_output=True, encoding='utf-8', creationflags=NO_WINDOW,
                             env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')})
    except OSError:
        return None
    if got.returncode:
        return None
    path = Path(got.stdout.strip()) / 'run.lock'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f, deadline = path.open('ab'), time.monotonic() + wait
    except OSError as e:
        return f'cannot take the run lock {path}: {e}'
    os.lseek(f.fileno(), 1 << 30, os.SEEK_SET)   # msvcrt locks are mandatory: lock a byte past any content
    while True:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EDEADLOCK):
                f.close()
                return f'cannot take the run lock {path}: {e}'
            if time.monotonic() >= deadline:
                f.close()
                return f'a chief already runs in {worktree}; its launcher holds {path}'
            time.sleep(0.05)
    HELD.append(f)
    if not keep:
        release()
    return None


def release():
    """Unlock the run lock now: Windows frees a closed handle's lock only after a delay."""
    while HELD:
        f = HELD.pop()
        os.lseek(f.fileno(), 1 << 30, os.SEEK_SET)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()


def stop(child):
    """Terminate claude; on Windows its tree, as an npm claude.cmd is cmd.exe running node and its tools are children."""
    if os.name != 'nt':
        return child.terminate()
    windows.kill_tree(child, 10)


# the caller's session: chief inheriting it would run as nested in, pass for, or message as the lead
LEAD_SESSION = {'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_MESSAGING_SOCKET',
                'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_BRIDGE_SESSION_ID', 'CLAUDE_CODE_SESSION_ATTENDED'}


def child_env():
    # a fresh top-level session: the caller's pane variables would let pane-keyed hooks act on its pane
    env = {k: v for k, v in os.environ.items() if k not in LEAD_SESSION and not k.startswith('HERDR_')}
    env['CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'] = '0'   # else -p kills units still running 10 min after chief's turn ends
    return env


def verdict(rc, result, body):
    """(exit code, reason) from claude's status, its final `result` event and that event's text."""
    if rc < 0:
        return 128 - rc, f'claude killed by signal {-rc}'
    if rc > 0x7FFFFFFF:   # Windows: an NTSTATUS (0xC0000005 a crash, 0xC000013A Ctrl+C), beyond an exit code
        return 1, f'claude exited 0x{rc:08X}'
    if rc != 0:
        return rc, f'claude exited {rc}'
    if result is None:
        return 1, 'no result event'
    if result.get('is_error'):
        return 1, f'result is an error ({result.get("subtype", "?")})'
    if result.get('num_turns') == 0:
        return 1, 'no model turn; see stream.jsonl'
    if not body:
        return 1, 'result has no text'
    return 0, 'result ok'


def with_brief(text, brief):
    """The report with the brief tables before its first `## ` section outside a fenced block, or after it all."""
    lines = text.rstrip('\n').split('\n')
    fence, at = None, len(lines)
    for i, line in enumerate(lines):
        mark = re.match(r' {0,3}(`{3,}|~{3,})', line)
        if mark and fence is None:
            fence = mark.group(1)
        elif mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence) and not line.strip()[len(mark.group(1)):].strip():
            fence = None
        elif fence is None and line.startswith('## '):
            at = i
            break
    return '\n'.join(lines[:at]).rstrip('\n') + f'\n\n{brief}\n' + ('\n' + '\n'.join(lines[at:]) + '\n' if at < len(lines) else '')


def cost(out, sid, report):
    """The exit line's cost field; writes cost.md and puts its brief form into the report."""
    if not sid:
        return 'cost none (no session id)'
    try:
        brief, _, total = run_cost.report(run_cost.session_dir(session_id=sid), out / 'cost.md')
        if report.exists():
            report.write_text(with_brief(report.read_text(encoding='utf-8'), brief), encoding='utf-8', newline='\n')
    except run_cost.Failed as e:
        return f'cost none (run_cost: {e})'
    except Exception as e:   # noqa: BLE001 - chief has finished; a cost it cannot table must not lose the exit line
        return f'cost none ({type(e).__name__}: {e})'
    return 'cost none (no cost record; see cost.md)' if total == '-' else f'cost ${total}'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--worktree', required=True, type=Path)
    p.add_argument('--task-file', required=True, type=Path)
    p.add_argument('--permission-mode')
    p.add_argument('--foreground', action='store_true')
    if os.name == 'nt':
        p.add_argument('--_detached', action='store_true', help=argparse.SUPPRESS)   # the launcher, run again
    a = p.parse_args(argv)
    if getattr(a, '_detached', False):
        detached(a, lambda: detached_launch(a))
    if a.out.exists():
        fail(f'{a.out} exists; each run needs a new directory')
    if not a.worktree.is_dir():
        fail(f'{a.worktree} is not a directory')
    if not a.task_file.is_file():
        fail(f'{a.task_file} is not a file')
    if pids := running_chiefs(a.worktree):
        fail(f'a chief already runs in {a.worktree} (pid {", ".join(map(str, pids))})')
    if why := lock_held(a.worktree, keep=a.foreground):
        fail(why)
    try:
        if why := refusal(a.worktree, 'relaunch with a new --out'):
            fail(why)
        try:
            task = a.task_file.open('rb')
        except OSError as e:
            fail(f'{a.task_file} cannot be read: {e}')
        a.out.mkdir(parents=True)
        if a.foreground:
            return launch(a, task)
    finally:
        release()
    if os.name == 'nt':
        # no fork: this process's own command line runs again as the launcher, without a console or the caller's
        # group; the task goes on its stdin, as a forked launcher keeps the file open whatever the caller does with it
        with task:
            data = task.read()
        with (a.out / 'stderr.log').open('ab') as log:
            launcher = subprocess.Popen([sys.executable, *sys.orig_argv[1:], '--_detached'], stdin=subprocess.PIPE, stdout=log,
                                        stderr=log, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            with launcher.stdin:
                launcher.stdin.write(data)
        except OSError as e:
            fail(f'the launcher (pid {launcher.pid}) did not take the task: {e}; see {a.out / "stderr.log"}')
        print(f'chief_run: launcher pid {launcher.pid}, progress {a.out / "progress.log"}')
        return 0
    if pid := os.fork():
        print(f'chief_run: launcher pid {pid}, progress {a.out / "progress.log"}')
        return 0
    os.setsid()
    for fd, path, flags in ((0, os.devnull, os.O_RDONLY), (1, a.out / 'stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND)):
        opened = os.open(path, flags, 0o644)
        os.dup2(opened, fd)
        os.close(opened)
    os.dup2(1, 2)
    detached(a, lambda: launch(a, task))


def detached(a, run):
    """Run the launcher nobody watches and exit with its code: the exit line must come whatever happens."""
    code = 1
    try:
        code = run()
    except BaseException as e:   # noqa: BLE001 - nobody watches this process; the exit line must still come
        traceback.print_exc()
        Progress(a.out / 'progress.log').line(f'launcher: exit 1 (launcher failed: {type(e).__name__}: {e}) · report none')
    finally:
        sys.stderr.flush()
        if HELD:
            release()
        os._exit(code)


def detached_launch(a):
    """Windows: the launcher run again, the task on its stdin; it checks again what its caller checked, and takes the
    run lock its caller only checked."""
    task = tempfile.TemporaryFile()
    task.write(sys.stdin.buffer.read())
    task.seek(0)
    if not a.out.is_dir() or (a.out / 'progress.log').exists():
        print(f'chief_run: --_detached is the launcher chief_run starts, in the new --out it made; {a.out} is not one',
              file=sys.stderr)
        return 2
    # the inherited log handle has no append mode: it would write over claude's stderr
    opened = os.open(a.out / 'stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(opened, 1)
    os.close(opened)
    os.dup2(1, 2)
    why = None if a.worktree.is_dir() else f'{a.worktree} is not a directory'
    if why := why or refusal(a.worktree, 'relaunch with a new --out') or lock_held(a.worktree, keep=True, wait=5):
        Progress(a.out / 'progress.log').line(f'launcher: exit 2 ({why}) · report none')
        return 2
    return launch(a, task)


def launch(a, task):
    # Windows: CreateProcess would try only claude.exe, and the working directory and System32 before PATH
    claude = on_path('claude', ('.exe', '.cmd', '.bat'))
    cmd = [claude, '-p', '--agent', 'chief', '--output-format', 'stream-json', '--verbose']
    if a.permission_mode:
        cmd += ['--permission-mode', a.permission_mode]

    progress = Progress(a.out / 'progress.log')
    seen, result, warned, sid = set(), None, set(), ''

    def warn(key, text):
        if key not in warned:
            warned.add(key)
            progress.line(f'launcher: warning: {text}')

    with task, (a.out / 'stderr.log').open('ab') as err, \
            (a.out / 'stream.jsonl').open('ab') as stream:
        try:
            if claude is None:
                raise FileNotFoundError('claude is not on PATH')
            # a detached launcher has no console: CREATE_NO_WINDOW keeps claude's tools from opening windows
            child = subprocess.Popen(cmd, cwd=a.worktree, stdin=task, stdout=subprocess.PIPE, stderr=err, env=child_env(),
                                     creationflags=NO_WINDOW)
        except OSError as e:
            err.write(f'chief_run: could not start claude: {e}\n'.encode())
            progress.line(f'launcher: exit 127 (could not start claude: {e}) · report none')
            return 127
        try:
            # Windows has no SIGHUP and can send claude no signal but termination
            for sig in filter(None, (getattr(signal, n, None) for n in ('SIGTERM', 'SIGINT', 'SIGHUP', 'SIGBREAK'))):
                signal.signal(sig, lambda signum, _frame: stop(child) if os.name == 'nt' else child.send_signal(signum))
            progress.line(f'launcher: started pid {child.pid}, out {a.out}')
            for raw in iter(child.stdout.readline, b''):
                stream.write(raw)
                stream.flush()
                try:
                    event = json.loads(raw)
                    kind = event.get('type')
                    if kind == 'system' and event.get('subtype') == 'init':
                        sid = event.get('session_id', '')
                        (a.out / 'session').write_text(f'{sid}\n', encoding='utf-8', newline='\n')
                        progress.line(f'launcher: session {sid}')
                    elif kind == 'assistant' and event.get('parent_tool_use_id') is None:
                        msg = event['message']
                        for block in msg['content']:
                            note = (block.get('thinking') or '').strip() if block.get('type') == 'thinking' else ''
                            if note:   # a between-tool note that Opus 5.5 returns as thinking
                                progress.line('note: ' + ' '.join(note.splitlines()))
                            if block.get('type') != 'text':
                                continue
                            text = block['text'].lstrip('\n')
                            if msg.get('id') not in seen:   # the message's first text: where a status line goes
                                seen.add(msg.get('id'))
                                first, _, text = text.partition('\n')
                                if first.startswith(MARKER):
                                    progress.line(first)
                            if any(line.startswith(MARKER) for line in text.splitlines()):
                                warn('late', 'a status line later in a message was not forwarded; see stream.jsonl')
                    elif kind == 'result':
                        result = event
                except Exception as e:   # noqa: BLE001 - any bad record is skipped: chief's stdout must keep draining
                    err.write(f'chief_run: unusable stdout line ({type(e).__name__}: {e}): '.encode() + raw)
                    err.flush()
                    warn('unusable', 'unusable stdout lines; see stderr.log')
            rc = child.wait()
        except BaseException:   # the exit line must not come while chief still runs in the worktree
            stop(child)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
    body = str(result.get('result') or '') if result is not None else ''
    if result is not None and result.get('num_turns') == 0:
        body = ''   # a hook refused the prompt: not chief's report
    code, reason = verdict(rc, result, body.strip())
    report = a.out / 'report.md'
    if body.strip():
        report.write_text(body.rstrip('\n') + '\n', encoding='utf-8', newline='\n')   # Markdown: keep its indentation
    progress.line(f'launcher: exit {code} ({reason}) · report {report if body.strip() else "none"} · {cost(a.out, sid, report)}')
    return code


if __name__ == '__main__':
    if os.name == 'nt':
        sys.stdout.reconfigure(encoding='utf-8')   # paths and refusals, whatever the locale
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.exit(main())
