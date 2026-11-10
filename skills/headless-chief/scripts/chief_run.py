#!/usr/bin/env python3
"""Run a headless chief and keep a numbered log of its `chief:` status lines.

    chief_run.py --out DIR --worktree PATH --task-file FILE [--task KEY] [--permission-mode MODE] [--foreground]

DIR must not exist. It receives stream.jsonl (every stdout line, raw), progress.log
(`<seq> <HH:MM:SS> <line>`: the launcher's own lines, chief's status lines and notes),
report.md (the final result's text, led by the exit reason when the session's end cut off
chief's own tasks), session (the session id), stderr.log and, when
the session's cost can be tabled, cost.md (run_cost.py's full output; its brief form,
the Launches and Usage tables, goes into report.md before chief's first `## ` section).
The exit line ends with that total, or `cost none (<why>)`.

Each launcher execution is accounted for (#490): run.json in DIR and a start and an end record in run_ledger.py's
ledger, which outlives DIR. They carry a fresh run id, KEY (the caller's task, the same on every relaunch; null when
not given), the worktree, the start and, at the end, the exit code, reason, session, wall seconds and the cost:
known, partial or unknown, with its dollars and reason. Both are best-effort and written apart: a failure is a
warning, never a stopped or re-coded run.

While the credit guard holds one of the run's hooks waiting for a plan limit to reset, progress.log says so.

A status line is the first line of one of chief's own messages (agents/chief.md);
a note is a progress note, joined onto one line, that chief's model returns as a `thinking`
block. It returns at once and leaves the launcher in its own session, so no caller's
command timeout ends the run; --foreground runs it in place. The launcher is this command
run again, detached, with the environment a fresh session gets (lead_session): herdr-core
ends every process carrying the caller's terminal's HC_SCOPE with that terminal. Exit codes
and usage are in SKILL.md.
"""
import argparse
import errno
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cost  # noqa: E402
import run_ledger  # noqa: E402
from shadows import on_path, refusal  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import lead_session  # noqa: E402
import windows  # noqa: E402

GUARD = Path(__file__).resolve().parents[3] / 'hooks' / 'credit-guard' / 'credit_guard.py'
spec = importlib.util.spec_from_file_location('credit_guard', GUARD)
credit_guard = importlib.util.module_from_spec(spec)   # its waiting hooks' records, read the way it reads them
spec.loader.exec_module(credit_guard)

MARKER = 'chief: '
TASK_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/#-]{0,159}')
RELAUNCH = [sys.executable, *sys.orig_argv[1:], '--_detached']   # this command again, as the launcher
HELD = []   # the run.lock this launcher holds until it exits
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0   # a detached launcher's console children
GUARD_WATCH = 30   # seconds between looks at the credit guard's waiting hooks

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
        self.lock = threading.Lock()   # the guard watcher writes too

    def line(self, text):
        with self.lock:
            self.seq += 1
            self.f.write(f'{self.seq} {time.strftime("%H:%M:%S")} {text}\n')
            self.f.flush()


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def unknown(why):
    return {'status': 'unknown', 'usd': None, 'reason': why}


class Accounting:
    """One launcher execution's run.json and ledger records, each written best-effort and on its own; finished once."""

    def __init__(self, a):
        self.out, self.progress, self.done, self.t0 = a.out, None, False, time.monotonic()
        self.rec = {'v': 1, 'run': uuid.uuid4().hex, 'task': a.task, 'out': str(a.out.resolve()),
                    'worktree': str(a.worktree.resolve()), 'startedAt': now()}
        self.write('start', {k: self.rec[k] for k in ('run', 'task', 'out', 'worktree')} | {'at': self.rec['startedAt']})

    def warn(self, text):
        try:
            if self.progress is None:
                raise OSError('no progress log yet')
            self.progress.line(f'launcher: warning: {text}')
        except Exception:   # noqa: BLE001 - a warning must not stop the run either
            try:
                print(f'chief_run: warning: {text}', file=sys.stderr, flush=True)
            except Exception:   # noqa: BLE001
                pass

    def write(self, kind, fields):
        try:
            tmp = self.out / f'.run.json.{os.getpid()}'
            tmp.write_text(json.dumps(self.rec, indent=2) + '\n', encoding='utf-8', newline='\n')
            os.replace(tmp, self.out / 'run.json')
        except Exception as e:   # noqa: BLE001 - accounting must never stop or re-code the run
            self.warn(f'run.json not written: {type(e).__name__}: {e}')
        try:
            run_ledger.append({'v': 1, 'type': kind, **fields})
        except Exception as e:   # noqa: BLE001
            self.warn(f'run ledger {kind} not written ({run_ledger.path()}): {type(e).__name__}: {e}')

    def finish(self, code, reason, session=None, spent=None):
        if self.done:
            return
        self.done = True
        end = {'at': now(), 'exit': code, 'reason': reason, 'session': session or None,
               'wall': round(time.monotonic() - self.t0, 1), 'cost': spent or unknown('no cost was read')}
        self.rec.update(endedAt=end['at'], **{k: end[k] for k in ('exit', 'reason', 'session', 'wall', 'cost')})
        self.write('end', {'run': self.rec['run']} | end)


def watch_guard(progress, tag, stop, every=GUARD_WATCH):
    """Log when the run's hooks (CREDIT_GUARD_TAG tag) start and stop waiting on the credit guard, until stop is
    set: a wait can begin before claude names its session."""
    since = None
    while not stop.wait(every):
        waits = credit_guard.waiters(tag)
        if waits and since is None:
            since = min(w['since'] for w in waits)
            names = sorted({n for w in waits for n in w.get('limits') or []})
            progress.line(f'launcher: credit-guard waiting ({", ".join(names)}), next quota check '
                          f'{credit_guard.local_time(min(w["next_check"] for w in waits))}')
        elif not waits and since is not None:
            progress.line(f'launcher: credit-guard wait over after {credit_guard.duration(time.time() - since)}')
            since = None


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


def child_env():
    # a fresh top-level session: inheriting the caller's, chief would run as nested in, pass for, or message as the lead
    env = lead_session.child_env()
    env['CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'] = '0'   # else -p kills units still running 10 min after chief's turn ends
    env.pop('CREDIT_GUARD_TAG', None)   # launch() sets this run's own
    return env


class Tasks:
    """The session's own Agent, Bash and workflow tasks from claude's task events. A task the final result leaves
    running, or a run of one killed that its owner never took in, was cut off, not settled: a unit that returns while
    its own background command runs reads `completed`, its command still listed. An owner took a kill in when it
    stopped that run with `TaskStop`, or took a model turn on it: a message first seen after a tool result that
    followed the kill, or after a run of the unit claude resumed with that kill's notification as its prompt (chief's
    messages or the unit's own, by `parent_tool_use_id`), and for chief also a later top-level `init` or a result
    whose `origin.runId` is the killed run."""
    ENDED = {'completed', 'failed', 'killed', 'stopped'}

    def __init__(self):
        self.about, self.status, self.tool, self.run, self.stops, self.messages = {}, {}, {}, {}, set(), set()
        # killed and stops by (task, run): a resumed task keeps its id under a new run
        self.listed, self.killed, self.open = set(), {}, set()   # killed: -> a tool result or resume since

    def owner(self, task):
        """The `parent_tool_use_id` its owner's messages carry: None for chief's, '?' for a unit never started here."""
        parent = self.about.get(task, ('', None))[1]
        return parent and self.tool.get(parent, '?')

    def event(self, event):
        kind, sub, task = event.get('type'), event.get('subtype'), event.get('task_id')
        scope = event.get('parent_tool_use_id')
        if kind == 'assistant':
            content = event['message']['content']
            stopped = (str((b.get('input') or {}).get('task_id')) for b in content
                       if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'TaskStop')
            self.stops.update((t, self.run.get(t)) for t in stopped)
            if (msg := event['message'].get('id')) not in self.messages:
                self.messages.add(msg)
                self.forget(lambda key, fed: fed and self.owner(key[0]) == scope)
            self.forget(lambda key, fed: key in self.stops)
        elif kind == 'user':
            for key in self.killed:
                self.killed[key] = self.killed[key] or self.owner(key[0]) == scope
        elif kind == 'result':
            if (origin := event.get('origin') or {}).get('kind') == 'task-notification':
                self.forget(lambda key, fed: key[1] == origin.get('runId'))
            self.open = {t for t, s in self.status.items() if s not in self.ENDED} | self.listed
        elif kind != 'system':
            return
        elif sub == 'init':
            self.forget(lambda key, fed: self.owner(key[0]) is None)
        elif sub == 'background_tasks_changed':
            self.listed = {t['task_id'] for t in event.get('tasks') or []}
            for t in event.get('tasks') or []:
                self.about.setdefault(t['task_id'], (t.get('description') or '', t.get('parent_task_id')))
        elif task is None:
            return
        elif sub == 'task_started':
            self.about[task] = (event.get('description') or '', event.get('parent_task_id'))
            self.status[task], self.tool[task], self.run[task] = 'running', event.get('tool_use_id'), event.get('run_id')
            for key in self.killed:   # a notification of the latest run's end
                self.killed[key] = self.killed[key] or (key[1] == self.run.get(key[0]) and
                                                        f'<task-id>{key[0]}</task-id>' in (event.get('prompt') or ''))
        elif sub in ('task_updated', 'task_notification'):
            status = (event.get('patch') or {}).get('status') if sub == 'task_updated' else event.get('status')
            if status:
                self.status[task] = status
            key = (task, event.get('run_id') or self.run.get(task))
            if status in ('killed', 'stopped') and key not in self.stops:
                self.killed.setdefault(key, False)

    def forget(self, taken):
        self.killed = {key: fed for key, fed in self.killed.items() if not taken(key, fed)}

    def cut(self):
        """Each task cut off, `<id> <status> "<description>"`, a unit's own followed by ` under <id> "<description>"`."""
        def said(task):
            return '"' + ' '.join(self.about.get(task, ('', None))[0].split())[:60] + '"'
        rows = []
        for task in sorted(self.open | {t for t, _ in self.killed}):
            row, parent = f'{task} {self.status.get(task, "running")} {said(task)}', self.about.get(task, ('', None))[1]
            rows.append(f'{row} under {parent} {said(parent)}' if parent else row)
        return rows


def verdict(rc, result, body, cut=()):
    """(exit code, reason) from claude's status, its final `result` event, that event's text and the tasks the
    session's end cut off."""
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
    if cut:
        return 1, f'incomplete: the session ended on chief\'s own work: {"; ".join(cut)}'
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


def spent(total):
    """run_cost.report()'s total cell as accounting: `1.31` known, `1.31 (partial)` partial, anything else unknown."""
    amount, partial = (total[:-len(' (partial)')], True) if total.endswith(' (partial)') else (total, False)
    try:
        usd = Decimal(amount) if re.fullmatch(r'\d+\.\d{2}', amount) else None
    except InvalidOperation:
        usd = None
    if usd is None:
        return unknown('no cost record; see cost.md' if total == '-' else f'unreadable total {total!r}')
    return {'status': 'partial' if partial else 'known', 'usd': str(usd),
            'reason': 'some agents have no price' if partial else None}


def cost(out, sid, report):
    """(the exit line's cost field, the cost as accounting); writes cost.md and puts its brief form into the report."""
    if not sid:
        return 'cost none (no session id)', unknown('no session id')
    try:
        brief, _, total = run_cost.report(run_cost.session_dir(session_id=sid), out / 'cost.md')
        if report.exists():
            report.write_text(with_brief(report.read_text(encoding='utf-8'), brief), encoding='utf-8', newline='\n')
    except run_cost.Failed as e:
        return f'cost none (run_cost: {e})', unknown(f'run_cost: {e}')
    except Exception as e:   # noqa: BLE001 - chief has finished; a cost it cannot table must not lose the exit line
        return f'cost none ({type(e).__name__}: {e})', unknown(f'{type(e).__name__}: {e}')
    return ('cost none (no cost record; see cost.md)' if total == '-' else f'cost ${total}'), spent(total)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--worktree', required=True, type=Path)
    p.add_argument('--task-file', required=True, type=Path)
    p.add_argument('--task')
    p.add_argument('--permission-mode')
    p.add_argument('--foreground', action='store_true')
    p.add_argument('--_detached', action='store_true', help=argparse.SUPPRESS)   # the launcher, run again
    a = p.parse_args(argv)
    if a._detached:
        detached(a, lambda: detached_launch(a))
    if a.task is not None and not TASK_KEY.fullmatch(a.task):
        fail(f'--task {a.task!r} is not a task key ({TASK_KEY.pattern}); a relaunch passes the same key')
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
            a.accounting = Accounting(a)
            try:
                return launch(a, task)
            except BaseException as e:
                a.accounting.finish(1, f'launcher failed: {type(e).__name__}: {e}')
                raise
    finally:
        release()
    # this process's own command line runs again as the launcher, with a fresh session's environment (herdr-core
    # ends every process that started with the caller's terminal's HC_SCOPE), outside the caller's session (POSIX)
    # or console and group (Windows). It gets the task on its stdin: the open file on POSIX; piped on Windows, where
    # an open file would stop the caller deleting it. A console of its own with no window, not none: a venv's
    # python.exe starts the real one as a child, which DETACHED_PROCESS would leave to open a visible console
    nt = os.name == 'nt'
    with task, (a.out / 'stderr.log').open('ab') as log:
        data = task.read() if nt else None
        try:
            launcher = subprocess.Popen(RELAUNCH, stdout=log, stderr=log, env=lead_session.child_env(), **(
                dict(stdin=subprocess.PIPE, creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if nt
                else dict(stdin=task, start_new_session=True)))
        except OSError as e:
            fail(f'cannot start the launcher: {e}')
    if nt:
        try:
            with launcher.stdin:
                launcher.stdin.write(data)
        except OSError as e:
            fail(f'the launcher (pid {launcher.pid}) did not take the task: {e}; see {a.out / "stderr.log"}')
    print(f'chief_run: launcher pid {launcher.pid}, progress {a.out / "progress.log"}')
    return 0


def detached(a, run):
    """Run the launcher nobody watches and exit with its code: the exit line must come whatever happens."""
    code = 1
    try:
        code = run()
    except BaseException as e:   # noqa: BLE001 - nobody watches this process; the exit line must still come
        traceback.print_exc()
        if accounting := getattr(a, 'accounting', None):
            accounting.finish(1, f'launcher failed: {type(e).__name__}: {e}')
        Progress(a.out / 'progress.log').line(f'launcher: exit 1 (launcher failed: {type(e).__name__}: {e}) · report none')
    finally:
        sys.stderr.flush()
        if HELD:
            release()
        os._exit(code)


def detached_launch(a):
    """The launcher run again, the task on its stdin; it checks again what its caller checked, and on Windows takes
    the run lock its caller only checked."""
    if not a.out.is_dir() or (a.out / 'progress.log').exists() or (a.out / 'run.json').exists():
        print(f'chief_run: --_detached is the launcher chief_run starts, in the new --out it made; {a.out} is not one',
              file=sys.stderr)
        return 2
    a.accounting = Accounting(a)
    task = sys.stdin.buffer
    if not task.seekable():   # Windows' pipe: copied, so claude gets a file
        task = tempfile.TemporaryFile()
        task.write(sys.stdin.buffer.read())
        task.seek(0)
    if os.name == 'nt':   # the inherited log handle has no append mode: it would write over claude's stderr
        opened = os.open(a.out / 'stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(opened, 1)
        os.close(opened)
        os.dup2(1, 2)
    why = None if a.worktree.is_dir() else f'{a.worktree} is not a directory'
    if why := why or refusal(a.worktree, 'relaunch with a new --out') or lock_held(a.worktree, keep=True, wait=5):
        a.accounting.finish(2, why)
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
    a.accounting.progress = progress
    seen, result, warned, sid, tasks = set(), None, set(), '', Tasks()

    def warn(key, text):
        if key not in warned:
            warned.add(key)
            progress.line(f'launcher: warning: {text}')

    with task, (a.out / 'stderr.log').open('ab') as err, \
            (a.out / 'stream.jsonl').open('ab') as stream:
        try:
            if claude is None:
                raise FileNotFoundError('claude is not on PATH')
            # a console of its own with no window, in the foreground too: claude's tools open none
            tag = str(a.out.resolve())
            child = subprocess.Popen(cmd, cwd=a.worktree, stdin=task, stdout=subprocess.PIPE, stderr=err,
                                     env={**child_env(), 'CREDIT_GUARD_TAG': tag}, creationflags=NO_WINDOW)
        except OSError as e:
            err.write(f'chief_run: could not start claude: {e}\n'.encode())
            a.accounting.finish(127, f'could not start claude: {e}', spent=unknown('claude did not start'))
            progress.line(f'launcher: exit 127 (could not start claude: {e}) · report none')
            return 127
        unwatch = threading.Event()
        watcher = threading.Thread(target=watch_guard, args=(progress, tag, unwatch), daemon=True)
        try:
            watcher.start()
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
                    tasks.event(event)
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
        finally:   # no watcher line after the exit line
            unwatch.set()
            if watcher.ident is not None:
                watcher.join()
    body = str(result.get('result') or '') if result is not None else ''
    if result is not None and result.get('num_turns') == 0:
        body = ''   # a hook refused the prompt: not chief's report
    code, reason = verdict(rc, result, body.strip(), tasks.cut())
    report = a.out / 'report.md'
    if body.strip() and reason.startswith('incomplete'):
        body = f'**{reason}**\n\n{body}'   # the report alone must not read as chief's last word
    if body.strip():
        report.write_text(body.rstrip('\n') + '\n', encoding='utf-8', newline='\n')   # Markdown: keep its indentation
    field, spend = cost(a.out, sid, report)
    a.accounting.finish(code, reason, sid, spend)
    progress.line(f'launcher: exit {code} ({reason}) · report {report if body.strip() else "none"} · {field}')
    return code


if __name__ == '__main__':
    if os.name == 'nt':
        sys.stdout.reconfigure(encoding='utf-8')   # paths and refusals, whatever the locale
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.exit(main())
