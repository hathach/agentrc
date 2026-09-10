#!/usr/bin/env python3
"""Mechanics of the cowork channel: drive the other agent's CLI headless in
this worktree, one resumed session per side, requests run one after another
in the order they were sent.

Judgment stays in SKILL.md. This script composes the turn, records what the
coworker streamed and replied, and refuses rather than guesses: a reset with
requests pending, or a task that resolves to nothing, is an error.

  cowork.py send [--to codex|claude] [--no-edit] (--task "..." | --task-file F | --task -)
  cowork.py kill <id>
  cowork.py read <id>
  cowork.py watch [<id>...]
  cowork.py status
  cowork.py tail [<id>]
  cowork.py reset <side>

`send` queues the request, runs it in a detached runner when its turn comes,
and blocks until the reply is in; run it in your harness's background and its
exit is the notification. A request whose predecessor failed is skipped.
`watch` prints one line per request as it settles, for a harness that keeps
a monitor instead of a background shell; `read` prints a settled reply.

Files live under <git dir>/cowork/<side>/: `session` holds the coworker's
session id; each request gets <id>.task, <id>.prompt, <id>.jsonl (the event
stream, for a human to follow), <id>.err, <id>.reply, <id>.exit (the verdict,
written once, first writer wins) and <id>.lock, which the runner and the CLI
hold while they live, so liveness is the kernel's word, not a pid's. Exit
codes: 1 the turn failed or was skipped, 3 unknown request or reset refused,
4 the reply lacks its "Files touched" line or the tree changed under
--no-edit.
"""

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SIDES = ('codex', 'claude')
FAILED, BUSY, MALFORMED = 1, 3, 4
GOOD = ('0', 'edited')  # a turn that produced a reply
FOOTER = re.compile(r'^Files touched: \S', re.M)
BOOTSTRAP = (
    'You are the {side} coworker on the cowork channel of the checkout at {root}: a headless,\n'
    'resumed session driven by the other coding agent, not by a human. Load the `cowork` skill\n'
    'for the rules. Never push, open a PR or post a comment on a request from this channel.\n\n')
HEADER = ('cowork request {id} from {me}. Scope: {scope}. End your reply with a line\n'
          '"Files touched: <paths>" or "Files touched: none".\n---\n')
SCOPE = {True: 'do not edit anything', False: 'edit and commit by explicit path as the task needs'}


def die(message, code=FAILED):
    print(message, file=sys.stderr)
    sys.exit(code)


def git_dir():
    try:
        out = subprocess.run(['git', 'rev-parse', '--show-toplevel', '--git-dir'],
                             check=True, capture_output=True, text=True).stdout.split('\n')
    except subprocess.CalledProcessError:
        die('not inside a git checkout')
    root, gitdir = Path(out[0]), Path(out[1])
    return root, gitdir if gitdir.is_absolute() else root / gitdir


def coworker(explicit):
    side = explicit or ('codex' if os.environ.get('CLAUDECODE') else None)
    if side is None:
        die('say --to codex or --to claude; only Claude Code identifies itself in the environment')
    return side


def text_of(path):
    return path.read_bytes().decode('utf-8', errors='replace')


def publish(path, text):
    """Create `path` with its whole content or not at all, never over an
    existing verdict: readers key on existence. True if this call won."""
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(text)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        tmp.unlink()
    return True


@contextlib.contextmanager
def admission(box):
    """Only one process reads or changes the queue at a time."""
    with (box / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def held(lock_file, wait=False):
    """True while a runner or its CLI still holds this request's lock; with
    `wait`, block until they let go."""
    try:
        with lock_file.open('r') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        return True
    except OSError:
        pass
    return False


def holds(pid, lock_file):
    """True if that process has the request lock open: the CLI we started,
    not a later process that got its pid."""
    try:
        return any(os.readlink(fd) == str(lock_file) for fd in Path(f'/proc/{pid}/fd').iterdir())
    except OSError:
        return False


def requests(box):
    return sorted(p.stem for p in box.glob('*.task')) if box.exists() else []


def state(box, request):
    exit_file = box / f'{request}.exit'
    if exit_file.exists():
        return exit_file.read_text().strip()
    if not held(box / f'{request}.lock'):
        return 'died'
    return 'running' if (box / f'{request}.cli').exists() else 'queued'


def live(box):
    """Requests whose runner or CLI is still there, in ticket order: a verdict
    alone does not free the side, a killed turn is still being torn down."""
    return [r for r in requests(box) if held(box / f'{r}.lock')]


def session_of(box):
    session = box / 'session'
    return session.read_text().strip() if session.exists() else None


def compose(box, side, request, task, no_edit, root):
    sender = 'claude' if side == 'codex' else 'codex'
    header = HEADER.format(id=request, me=sender, scope=SCOPE[no_edit])
    return ('' if session_of(box) else BOOTSTRAP.format(side=side, root=root)) + header + task


def command(side, session, reply_file, no_edit):
    """The CLI argv and, for a first Claude turn, the id it is told to use;
    that id is bound only once the turn succeeds. Claude enforces `--no-edit`
    in plan mode; Codex's read-only sandbox would also forbid the temp files a
    test suite needs, so its turn is checked afterwards instead (tree_state)."""
    if side == 'codex':
        argv = ['codex', 'exec'] + (['resume', session] if session else [])
        return argv + ['--json', '-o', str(reply_file), '-'], None
    argv = ['claude', '-p', '--output-format', 'stream-json', '--verbose']
    if no_edit:
        argv += ['--permission-mode', 'plan']
    if session:
        return argv + ['--resume', session], None
    new = str(uuid.uuid4())
    return argv + ['--session-id', new], new


def children_of():
    table = {}
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = stat.read_text().rsplit(')', 1)[1].split()
            table.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        except (OSError, ValueError, IndexError):
            continue
    return table


def kill_tree(pid):
    """SIGKILL a process and everything under it. Codex runs its shell tool
    in its own session, so the process group is not enough. Freeze each
    process as it is found and rescan until the frozen tree grows no more,
    so nothing spawned mid-walk escapes. A process we may not signal (sudo's
    child) is skipped; its parent still dies."""
    frozen = set()
    while True:
        table, stack, grown = children_of(), [pid], False
        while stack:
            current = stack.pop()
            if current not in frozen:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(current, signal.SIGSTOP)
                frozen.add(current)
                grown = True
            stack.extend(table.get(current, []))
        if not grown:
            break
    for victim in frozen:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(victim, signal.SIGKILL)


def take_turn(box, request, behind):
    """Block until the request queued just before this one has a verdict.
    Returns the reason not to run: a verdict already published for this
    request (`kill`), or the predecessor ending in anything but a reply; skips
    chain through the predecessors, so nothing runs after an earlier failure,
    kill or dead runner."""
    while True:
        if (box / f'{request}.exit').exists():
            return 'killed'
        if behind and held(box / f'{behind}.lock'):  # still running, or still being torn down
            time.sleep(0.1)
            continue
        verdict = state(box, behind) if behind else '0'
        return None if verdict in GOOD else f'skipped: queued behind {behind}, which ended {verdict}'


def run(side, box, root, request, behind, no_edit, lock_fd):
    """One coworker turn, in the detached runner, whose stderr is <id>.err.
    Publishes the verdict unless `kill` got there first."""
    exit_file, prompt, reply, stream = (box / f'{request}.{ext}' for ext in ('exit', 'prompt', 'reply', 'jsonl'))
    status, child, before = 'error', None, None
    try:
        reason = take_turn(box, request, behind)
        if reason:
            status = reason.split(':')[0]
            print(reason, file=sys.stderr)
            return
        session = session_of(box)
        prompt.write_text(compose(box, side, request, (box / f'{request}.task').read_text(), no_edit, root))
        argv, new_session = command(side, session, reply, no_edit)
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_') and k != 'CLAUDECODE'}
        env['COWORK_TURN'] = request  # tells the simplify gate to stay out
        before = tree_state(root) if no_edit else None
        with prompt.open('rb') as stdin, stream.open('ab') as out:
            child = subprocess.Popen(argv, cwd=root, stdin=stdin, stdout=out, env=env,
                                     start_new_session=True, pass_fds=(lock_fd,))
        publish(box / f'{request}.cli', f'{child.pid}\n')  # for `kill`, should this runner die first
        while child.poll() is None and not exit_file.exists():
            time.sleep(0.2)
        status = 'killed' if exit_file.exists() else str(child.returncode)
    except BaseException as failure:  # a missing CLI, anything: leave no orphan
        print(repr(failure), file=sys.stderr)
    finally:
        if child is not None and child.poll() is None:
            kill_tree(child.pid)
            child.wait()
        if child is not None:
            status = conclude(side, box, status, reply, stream, new_session, before, root)
        sys.stderr.flush()
        publish(exit_file, status + '\n')


def conclude(side, box, status, reply, stream, new_session, before, root):
    """What the finished CLI left behind: bind a new session, validate the
    reply, check a --no-edit tree, surface Codex's in-stream error."""
    if side == 'codex':
        if not session_of(box):
            thread = next((e['thread_id'] for e in events(stream) if 'thread_id' in e), None)
            if thread:
                (box / 'session').write_text(thread + '\n')  # a thread that exists resumes, even after a failed turn
        if status == '0' and not reply.exists():
            print('codex exited 0 without writing its last message', file=sys.stderr)
            status = 'error'
    elif status == '0':
        result = next((e for e in events(stream) if e.get('type') == 'result'), None)
        if result is None or result.get('is_error'):
            print(f'claude ended without a usable result: {result}', file=sys.stderr)
            status = 'error'
        else:
            reply.write_text(result.get('result') or '', encoding='utf-8')
            if new_session:
                (box / 'session').write_text(new_session + '\n')
    if status == '0' and before is not None and tree_state(root) != before:
        print('the tree changed during a --no-edit turn; check git status', file=sys.stderr)
        status = 'edited'
    if side == 'codex' and status not in GOOD + ('killed',):
        # Codex reports an in-turn failure only as a JSONL event, with nothing on stderr
        for event in events(stream):
            if event.get('type') in ('error', 'turn.failed'):
                print(event.get('message') or (event.get('error') or {}).get('message') or json.dumps(event),
                      file=sys.stderr)
                break
    return status


def tree_state(root):
    """HEAD, the index entries with their stages, and the worktree as git
    would stage it: what a coworker told not to edit must leave all three
    alone. `ls-files -s` rather than `write-tree`, which refuses conflicts."""
    def git(*args, **env):
        return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True,
                              env={**os.environ, **env}).stdout
    with tempfile.TemporaryDirectory() as scratch:
        index = os.path.join(scratch, 'index')  # a copy of the real one: tracked and staged files are never ignored
        with contextlib.suppress(OSError):
            shutil.copy(git('rev-parse', '--git-path', 'index').strip(), index)
        git('add', '-A', GIT_INDEX_FILE=index)
        git('add', '-A', '--renormalize', GIT_INDEX_FILE=index)  # rehash every tracked file: equal size and mtime prove nothing
        worktree = git('ls-files', '-s', GIT_INDEX_FILE=index)
    return git('rev-parse', 'HEAD') + git('ls-files', '-s') + worktree


def events(stream):
    """The stream's JSON events, newest first."""
    for line in reversed(text_of(stream).splitlines()):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def verdict(box, request):
    """(what happened, exit code) of a request whose runner has let go."""
    exit_file = box / f'{request}.exit'
    if not exit_file.exists():
        return 'died without recording a verdict', FAILED
    status = exit_file.read_text().strip()
    if status == 'edited':
        return 'the tree changed during a --no-edit turn; check git status', MALFORMED
    if status == '0':
        if FOOTER.search(text_of(box / f'{request}.reply')):
            return 'replied', 0
        return 'the reply has no "Files touched" line; re-read the tree yourself', MALFORMED
    return {'error': 'failed', 'killed': 'was killed', 'skipped': 'was skipped'}.get(status, f'exited {status}'), FAILED


def outcome(box, request):
    """(printable text, exit code) of a settled request."""
    what, code = verdict(box, request)
    if code == FAILED:
        err = box / f'{request}.err'  # absent if the send died between writing the task and starting the runner
        return f'cowork request {request} {what}\n{text_of(err)[-2000:] if err.exists() else ""}', code
    if code == MALFORMED:
        print(f'cowork request {request}: {what}', file=sys.stderr)
    return text_of(box / f'{request}.reply'), code


def wait_for(box, request):
    held(box / f'{request}.lock', wait=True)  # the runner publishes its verdict, then lets go
    return outcome(box, request)


def settled(gitdir):
    """Requests in this worktree whose runner has let go. Read under
    admission, so a request being enqueued is never seen half-made."""
    done = set()
    for side in SIDES:
        box = gitdir / 'cowork' / side
        if box.exists():
            with admission(box):
                done.update(r for r in requests(box) if not held(box / f'{r}.lock'))
    return done


def find(gitdir, request):
    box = gitdir / 'cowork' / request.split('-')[0]
    if not (box / f'{request}.task').exists():
        die(f'no request {request} in this worktree', BUSY)
    return box


def read_task(a):
    if a.task_file:
        return a.task_file.read_text()
    return sys.stdin.read() if a.task == '-' else a.task


def enqueue(box, root, side, task, no_edit):
    """Record the request behind the last live one and start its runner,
    which holds the request lock from birth; the caller keeps nothing open."""
    with admission(box):
        queue = live(box)
        request = f'{side}-{datetime.datetime.now():%Y%m%d-%H%M%S-%f}'
        (box / f'{request}.task').write_text(task)
        (box / f'{request}.jsonl').touch()  # so `tail` has a file the instant the id is printed
        with (box / f'{request}.lock').open('w') as lock, (box / f'{request}.err').open('ab') as err:
            fcntl.flock(lock, fcntl.LOCK_EX)
            argv = [sys.executable, __file__, '_run', side, request, queue[-1] if queue else '-', str(int(no_edit)),
                    str(lock.fileno())]
            # Double fork: the runner's parent exits at once, so it hangs off init, in its own
            # session, and a harness that kills `send` with its descendants cannot reach it.
            middle = os.fork()
            if middle == 0:
                subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                 start_new_session=True, pass_fds=(lock.fileno(),))
                os._exit(0)
            os.waitpid(middle, 0)
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    send = sub.add_parser('send', help='queue one turn of the coworker; prints the request id, then the reply')
    send.add_argument('--to', choices=SIDES, help='which coworker; defaults to codex when run from Claude Code')
    task = send.add_mutually_exclusive_group(required=True)
    task.add_argument('--task', help='literal text, or - for stdin')
    task.add_argument('--task-file', type=Path)
    send.add_argument('--no-edit', action='store_true', help='a question or review: the coworker must not edit')
    sub.add_parser('kill', help='stop a request, queued or running, and everything its coworker spawned').add_argument('request')
    sub.add_parser('read', help='print the reply of a settled request, as send would have').add_argument('request')
    watch = sub.add_parser('watch', help='print "<id>  <what happened>" for each request as it settles, forever')
    watch.add_argument('request', nargs='*', help='already settled requests to report first')
    sub.add_parser('status', help='sessions and requests in this worktree')
    tail = sub.add_parser('tail', help='follow the event stream of a request (default: the latest)')
    tail.add_argument('request', nargs='?')
    reset = sub.add_parser('reset', help='forget a side\'s session; the next send starts a new one')
    reset.add_argument('side', choices=SIDES)
    runner = sub.add_parser('_run', help=argparse.SUPPRESS)  # the detached child of `send`
    for name in ('side', 'request', 'behind', 'no_edit', 'lock_fd'):
        runner.add_argument(name)
    a = parser.parse_args(argv)

    root, gitdir = git_dir()
    if a.cmd == '_run':
        run(a.side, gitdir / 'cowork' / a.side, root, a.request, None if a.behind == '-' else a.behind,
            a.no_edit == '1', int(a.lock_fd))
        return 0

    if a.cmd == 'send':
        side = coworker(a.to)
        box = gitdir / 'cowork' / side
        box.mkdir(parents=True, exist_ok=True)
        task = read_task(a)
        if not task.strip():
            die('the task resolved to nothing')
        request = enqueue(box, root, side, task, a.no_edit)
        print(request, flush=True)
        text, code = wait_for(box, request)
        print(text, end='' if text.endswith('\n') else '\n')
        return code

    if a.cmd == 'kill':
        box = find(gitdir, a.request)
        exit_file, cli = box / f'{a.request}.exit', box / f'{a.request}.cli'
        if publish(exit_file, 'killed\n') and cli.exists() and holds(int(cli.read_text()), box / f'{a.request}.lock'):
            kill_tree(int(cli.read_text()))  # our CLI, orphaned or not; a runner still there also sees the verdict
        print(f'{a.request}: {exit_file.read_text().strip()}')
        return 0

    if a.cmd == 'read':
        box = find(gitdir, a.request)
        if held(box / f'{a.request}.lock'):
            die(f'{a.request} is {state(box, a.request)}; read it once it settles', BUSY)
        text, code = outcome(box, a.request)
        print(text, end='' if text.endswith('\n') else '\n')
        return code

    if a.cmd == 'watch':
        for request in a.request:
            find(gitdir, request)
        seen = settled(gitdir) - set(a.request)  # the past is reported only where asked
        while True:
            for request in sorted(settled(gitdir) - seen):
                seen.add(request)
                print(request, ' ', verdict(gitdir / 'cowork' / request.split('-')[0], request)[0], flush=True)
            time.sleep(0.5)

    if a.cmd == 'status':
        for side in SIDES:
            box = gitdir / 'cowork' / side
            print(f'{side}: session {session_of(box) or "none"}')
            for request in requests(box):
                print(f'  {request}  {state(box, request)}')
        return 0

    if a.cmd == 'tail':
        if a.request:
            stream = find(gitdir, a.request) / f'{a.request}.jsonl'
        else:
            streams = sorted((gitdir / 'cowork').glob('*/*.jsonl'), key=lambda p: p.stat().st_mtime)
            if not streams:
                die('no request in this worktree', BUSY)
            stream = streams[-1]
        os.execvp('tail', ['tail', '-n', '+1', '-F', str(stream)])

    if a.cmd == 'reset':
        box = gitdir / 'cowork' / a.side
        if not box.exists():
            print(f'{a.side}: no session')
            return 0
        with admission(box):
            queue = live(box)
            if queue:
                die(f'{a.side} still has {len(queue)} request(s) pending, first {queue[0]}; not resetting under them', BUSY)
            session = box / 'session'
            if session.exists():
                session.unlink()
                print(f'{a.side}: session forgotten; logs kept in {box}')
            else:
                print(f'{a.side}: no session')
        return 0


if __name__ == '__main__':
    sys.exit(main())
