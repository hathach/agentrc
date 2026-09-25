#!/usr/bin/env python3
"""Run a headless chief and keep a numbered log of its `chief:` status lines.

    chief_run.py --out DIR --worktree PATH --task-file FILE [--permission-mode MODE]

DIR must not exist. It receives stream.jsonl (every stdout line, raw), progress.log
(`<seq> <HH:MM:SS> <line>`: the launcher's own lines, chief's status lines and notes),
report.md (the final result's text), session (the session id), stderr.log and, when
the session's cost can be tabled, cost.md (run_cost.py's table, also appended to
report.md). The exit line ends with that total, or `cost none (<why>)`.

A status line is the first line of one of chief's own messages (agents/chief.md);
a note is a progress note, joined onto one line, that chief's model returns as a `thinking`
block. Exit codes and usage are in SKILL.md.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cost  # noqa: E402

MARKER = 'chief: '


def fail(msg):
    print(f'chief_run: {msg}', file=sys.stderr)
    sys.exit(2)


class Progress:
    def __init__(self, path):
        self.f = path.open('a', encoding='utf-8')
        self.seq = 0

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
    if not body:
        return 1, 'result has no text'
    return 0, 'result ok'


def cost(out, sid, report):
    """The exit line's cost field; writes cost.md and appends it to the report."""
    if not sid:
        return 'cost none (no session id)'
    try:
        md, total = run_cost.summary(run_cost.session_dir(session_id=sid))
        (out / 'cost.md').write_text(md + '\n', encoding='utf-8')
        if report.exists():
            with report.open('a', encoding='utf-8') as f:
                f.write(f'\n## Cost by stage\n\n{md}\n')
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
    a = p.parse_args(argv)
    if a.out.exists():
        fail(f'{a.out} exists; each run needs a new directory')
    if not a.worktree.is_dir():
        fail(f'{a.worktree} is not a directory')
    if not a.task_file.is_file():
        fail(f'{a.task_file} is not a file')
    if pids := running_chiefs(a.worktree):
        fail(f'a chief already runs in {a.worktree} (pid {", ".join(map(str, pids))})')
    a.out.mkdir(parents=True)
    cmd = ['claude', '-p', '--agent', 'chief', '--output-format', 'stream-json', '--verbose']
    if a.permission_mode:
        cmd += ['--permission-mode', a.permission_mode]

    progress = Progress(a.out / 'progress.log')
    seen, result, warned, sid = set(), None, set(), ''

    def warn(key, text):
        if key not in warned:
            warned.add(key)
            progress.line(f'launcher: warning: {text}')

    with a.task_file.open('rb') as task, (a.out / 'stderr.log').open('ab') as err, \
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
    body = str(result.get('result') or '') if result is not None else ''
    code, reason = verdict(rc, result, body.strip())
    report = a.out / 'report.md'
    if body.strip():
        report.write_text(body.rstrip('\n') + '\n', encoding='utf-8')   # Markdown: keep its indentation
    progress.line(f'launcher: exit {code} ({reason}) · report {report if body.strip() else "none"} · {cost(a.out, sid, report)}')
    return code


if __name__ == '__main__':
    sys.exit(main())
