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
command timeout ends the run; --foreground runs it in place. Exit codes and usage are
in SKILL.md.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cost  # noqa: E402

MARKER = 'chief: '


def fail(msg):
    print(f'chief_run: {msg}', file=sys.stderr)
    sys.exit(2)


class Progress:
    def __init__(self, path):
        self.f = path.open('a+', encoding='utf-8')
        self.f.seek(0)
        self.seq = sum(1 for _ in self.f)   # a line appended after a failed launch keeps the count

    def line(self, text):
        self.seq += 1
        self.f.write(f'{self.seq} {time.strftime("%H:%M:%S")} {text}\n')
        self.f.flush()


def running_chiefs(worktree):
    """Pids of `--agent chief` processes whose working directory is the worktree:
    a second chief there would write the same checkout."""
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


def child_env():
    # a fresh top-level session: the caller's pane variables would let pane-keyed hooks act on its pane
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE' and not k.startswith('HERDR_')}
    env['CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'] = '0'   # else -p kills units still running 10 min after chief's turn ends
    return env


def verdict(rc, result, body):
    """(exit code, reason) from claude's status, its final `result` event and that event's text."""
    if rc < 0:
        return 128 - rc, f'claude killed by signal {-rc}'
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
            report.write_text(with_brief(report.read_text(encoding='utf-8'), brief), encoding='utf-8')
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
    a = p.parse_args(argv)
    if a.out.exists():
        fail(f'{a.out} exists; each run needs a new directory')
    if not a.worktree.is_dir():
        fail(f'{a.worktree} is not a directory')
    if not a.task_file.is_file():
        fail(f'{a.task_file} is not a file')
    if pids := running_chiefs(a.worktree):
        fail(f'a chief already runs in {a.worktree} (pid {", ".join(map(str, pids))})')
    try:
        task = a.task_file.open('rb')
    except OSError as e:
        fail(f'{a.task_file} cannot be read: {e}')
    a.out.mkdir(parents=True)
    if a.foreground:
        return launch(a, task)
    if pid := os.fork():
        print(f'chief_run: launcher pid {pid}, progress {a.out / "progress.log"}')
        return 0
    os.setsid()
    for fd, path, flags in ((0, os.devnull, os.O_RDONLY), (1, a.out / 'stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND)):
        opened = os.open(path, flags, 0o644)
        os.dup2(opened, fd)
        os.close(opened)
    os.dup2(1, 2)
    code = 1
    try:
        code = launch(a, task)
    except BaseException as e:   # noqa: BLE001 - nobody watches this process; the exit line must still come
        traceback.print_exc()
        Progress(a.out / 'progress.log').line(f'launcher: exit 1 (launcher failed: {type(e).__name__}: {e}) · report none')
    finally:
        sys.stderr.flush()
        os._exit(code)


def launch(a, task):
    cmd = ['claude', '-p', '--agent', 'chief', '--output-format', 'stream-json', '--verbose']
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
            child = subprocess.Popen(cmd, cwd=a.worktree, stdin=task, stdout=subprocess.PIPE, stderr=err,
                                     env=child_env())
        except OSError as e:
            err.write(f'chief_run: could not start claude: {e}\n'.encode())
            progress.line(f'launcher: exit 127 (could not start claude: {e}) · report none')
            return 127
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda signum, _frame: child.send_signal(signum))
        try:
            progress.line(f'launcher: started pid {child.pid}, out {a.out}')
            for raw in iter(child.stdout.readline, b''):
                stream.write(raw)
                stream.flush()
                try:
                    event = json.loads(raw)
                    kind = event.get('type')
                    if kind == 'system' and event.get('subtype') == 'init':
                        sid = event.get('session_id', '')
                        (a.out / 'session').write_text(f'{sid}\n')
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
            child.terminate()
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
        report.write_text(body.rstrip('\n') + '\n', encoding='utf-8')   # Markdown: keep its indentation
    progress.line(f'launcher: exit {code} ({reason}) · report {report if body.strip() else "none"} · {cost(a.out, sid, report)}')
    return code


if __name__ == '__main__':
    sys.exit(main())
