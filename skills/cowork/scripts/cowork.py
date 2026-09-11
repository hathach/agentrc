#!/usr/bin/env python3
"""Run queued coworker turns in a detached process, resuming one session per side.

  cowork.py send [--to codex|claude] [--no-edit] [--model M] [--effort E] (--task TEXT | --task-file F | --task -)
  cowork.py kill <id>
  cowork.py read <id>
  cowork.py watch [<id>...]
  cowork.py status
  cowork.py tail [<id>]
  cowork.py reset <side>

Files live under <git dir>/cowork/<side>/: `session` holds the session id,
the model and the effort in use. The first send on a side sets the model
and effort from the flags, else from the caller's own session record mapped
to the same token-cost tier on the other side; later sends reuse them until
flags replace them or reset forgets them.
A request has files only from send until its reply is delivered; the
coworker's own session store keeps the turn. Meanwhile: .task until the
runner has read it, .lock (the runner and CLI hold its flock through
teardown, so liveness does not depend on pid reuse; the CLI pid and then
the verdict are written inside), .jsonl (CLI stdout), .err (stderr), and at
settle .reply and .exit (the verdict, first writer wins). A successor reads
its predecessor's verdict through a descriptor of that lock opened at send,
so delivery may delete the file underneath; a failed predecessor skips it.

send waits for the reply, prints it and removes the request; read does the
same for a reply whose send died; watch reports settled requests still
undelivered; reset removes everything of a side. Exit codes: 1 failed or
skipped, 3 unknown or delivered request, busy, or reset refused, 4 missing
"Files touched" line or a changed tree during --no-edit.
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
HEADER = ('cowork request {id} from {me}, answered by {model} at {effort} effort. Scope: {scope}. End your reply\n'
          'with a line "Files touched: <paths>" or "Files touched: none".\n---\n')
SCOPE = {True: 'do not edit anything', False: 'edit and commit by explicit path as the task needs'}
STORE = {'codex': '~/.codex/sessions', 'claude': '~/.claude/projects'}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')  # Claude's names; Codex has minimal..xhigh
TIERS = {  # the same token cost on the other side, by the family word in the model name (2026-09 list prices)
    'fable': 'gpt-6-astra', 'opus': 'gpt-5.6-sol', 'sonnet': 'gpt-5.6-terra', 'haiku': 'gpt-5.6-luna',
    'astra': 'fable', 'sol': 'opus', 'terra': 'sonnet', 'luna': 'haiku'}


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


def caller():
    """Which CLI is driving, from its environment: Claude Code sets CLAUDECODE,
    Codex sets CODEX_THREAD_ID."""
    if os.environ.get('CLAUDECODE'):
        return 'claude'
    if os.environ.get('CODEX_THREAD_ID'):
        return 'codex'
    return None


def coworker(explicit):
    side = explicit or {'claude': 'codex', 'codex': 'claude'}.get(caller())
    if side is None:
        die('say --to codex or --to claude; neither Claude Code nor Codex identifies itself in the environment')
    return side


def own_model_and_effort():
    """What the driving session runs right now, from its own record: Claude
    Code's transcript (last assistant message) and CLAUDE_EFFORT; Codex's
    rollout, last turn_context."""
    if caller() is None:
        die('neither Claude Code nor Codex identifies itself in the environment; pass --model and --effort')
    if caller() == 'claude':
        sid = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
        home = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        transcript = next(iter(home.glob(f'projects/*/{sid}.jsonl')), None) if sid else None
        if transcript is None:
            die('cannot find this Claude Code session\'s transcript to read its model; pass --model')
        model = next((e['message']['model'] for e in events(transcript)
                      if e.get('type') == 'assistant' and not e['message'].get('model', '<').startswith('<')), None)
        if model is None:
            die(f'{transcript} has no assistant message naming a model yet; pass --model')
        return model, os.environ.get('CLAUDE_EFFORT') or 'medium'
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    thread = os.environ['CODEX_THREAD_ID']
    rollout = next(iter(home.glob(f'sessions/**/rollout-*-{thread}.jsonl')), None)
    if rollout is None:
        die(f'cannot find the rollout of Codex thread {thread} under {home}/sessions; pass --model')
    context = next((e['payload'] for e in events(rollout) if e.get('type') == 'turn_context'), None)
    if not context or not context.get('model'):
        die(f'{rollout} has no turn_context naming a model yet; pass --model')
    return context['model'], {'minimal': 'low'}.get(context.get('effort'), context.get('effort') or 'medium')


def equivalent(model):
    """The other side's model at the same token cost."""
    word = next((w for w in TIERS if w in model), None)
    if word is None:
        die(f'no known price tier for {model}: pass --model')
    return TIERS[word]


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
    """True while a runner, its CLI or anything they spawned still holds this
    request's lock exclusively; with `wait`, block until they let go. Probed
    shared, so a successor reading the verdict through its own shared lock is
    not mistaken for them."""
    try:
        with lock_file.open('r') as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | (0 if wait else fcntl.LOCK_NB))
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


def lock_lines(content):
    """(CLI pid, verdict) written into a request lock by its runner; either
    is '' before the runner got there."""
    lines = content.split('\n') + ['', '']
    return lines[0], lines[1]


def requests(box):
    return sorted(p.stem for p in box.glob('*.lock'))


def state(box, request):
    exit_file, lock = box / f'{request}.exit', box / f'{request}.lock'
    if exit_file.exists():
        return exit_file.read_text().strip()
    if not held(lock):
        return 'died'
    return 'running' if lock_lines(lock.read_text())[0] else 'queued'


def materialise(box, request):
    """A runner killed outright leaves an unheld lock and no verdict: record
    one, so the request can be delivered like any other."""
    if not held(box / f'{request}.lock'):
        publish(box / f'{request}.exit', 'died\n')


def remove(box, request):
    for leftover in box.glob(f'{request}.*'):
        leftover.unlink()


def live(box):
    """Requests whose runner or CLI is still there, in ticket order: a verdict
    alone does not free the side, a killed turn is still being torn down."""
    return [r for r in requests(box) if held(box / f'{r}.lock')]


def side_state(box):
    """(session id or None, model, effort) of a side; the session id is empty
    until the first turn binds one."""
    lines = (box / 'session').read_text().split('\n') if (box / 'session').exists() else []
    lines += [''] * 3
    return lines[0] or None, lines[1], lines[2]


def write_side(box, **fields):
    """Change some of a side's session id, model and effort: a field-wise
    merge, replaced in one step so a reader never sees a truncated file. The
    caller holds admission, so a runner binding its thread and a send
    changing the model never overwrite each other."""
    current = dict(zip(('session', 'model', 'effort'), side_state(box)))
    current.update(fields)
    tmp = box / f'session.{os.getpid()}.tmp'
    tmp.write_text(f'{current["session"] or ""}\n{current["model"] or ""}\n{current["effort"] or ""}\n')
    os.replace(tmp, box / 'session')


def update_side(box, **fields):
    with admission(box):
        write_side(box, **fields)


def settle_pair(box, model, effort):
    """The model and effort for a request: the flags, else the side's saved
    pair, else the caller's own tier; whatever was missing on the side is
    saved for later sends. Under admission with the enqueue."""
    saved_model, saved_effort = side_state(box)[1:]
    model, effort = model or saved_model, effort or saved_effort
    if not (model and effort):  # first send on this side: start from what drives it
        own_model, own_effort = own_model_and_effort()
        model, effort = model or equivalent(own_model), effort or own_effort
    write_side(box, model=model, effort=effort)
    return model, effort


def session_of(box):
    return side_state(box)[0]


def compose(session, side, request, task, no_edit, root, model, effort):
    sender = 'claude' if side == 'codex' else 'codex'
    header = HEADER.format(id=request, me=sender, scope=SCOPE[no_edit], model=model, effort=effort)
    return ('' if session else BOOTSTRAP.format(side=side, root=root)) + header + task


def command(side, session, reply_file, no_edit, model, effort):
    """The CLI argv and, for a first Claude turn, the id it is told to use;
    that id is bound only once the turn succeeds. Claude enforces `--no-edit`
    in plan mode; Codex's read-only sandbox would also forbid the temp files a
    test suite needs, so its turn is checked afterwards instead (tree_state)."""
    if side == 'codex':
        argv = ['codex', 'exec'] + (['resume', session] if session else [])
        argv += ['-m', model, '-c', f'model_reasoning_effort={"xhigh" if effort == "max" else effort}']
        return argv + ['--json', '-o', str(reply_file), '-'], None
    argv = ['claude', '-p', '--output-format', 'stream-json', '--verbose', '--model', model, '--effort', effort]
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


def take_turn(box, request, behind, behind_fd):
    """Block until the request queued just before this one lets go of its
    lock, then read the verdict its runner wrote inside, through the
    descriptor `send` opened while the file still existed: delivery may have
    removed it since. Returns the reason not to run: a verdict already
    published for this request (`kill`), or the predecessor ending in
    anything but a reply; skips chain, so nothing runs after an earlier
    failure, kill or dead runner."""
    while True:
        if (box / f'{request}.exit').exists():
            return 'killed'
        if behind_fd is None:
            return None
        try:
            fcntl.flock(behind_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # still running, or still being torn down
        except BlockingIOError:
            time.sleep(0.1)
            continue
        verdict = lock_lines(os.pread(behind_fd, 4096, 0).decode())[1] or 'died'
        os.close(behind_fd)
        return None if verdict in GOOD else f'skipped: queued behind {behind}, which ended {verdict}'


def run(side, box, root, request, behind, behind_fd, no_edit, lock_fd, model, effort):
    """One coworker turn, in the detached runner, whose stderr is <id>.err.
    Publishes the verdict unless `kill` got there first, then leaves it in
    the lock for the successor and lets go."""
    exit_file, reply, stream = (box / f'{request}.{ext}' for ext in ('exit', 'reply', 'jsonl'))
    status, child, before = 'error', None, None
    try:
        reason = take_turn(box, request, behind, behind_fd)
        if reason:
            status = reason.split(':')[0]
            print(reason, file=sys.stderr)
            return
        session = session_of(box)
        task = box / f'{request}.task'
        prompt = compose(session, side, request, task.read_text(), no_edit, root, model, effort)
        task.unlink()
        argv, new_session = command(side, session, reply, no_edit, model, effort)
        env = {k: v for k, v in os.environ.items()  # the coworker is nobody's driver
               if not k.startswith('HERDR_') and k not in ('CLAUDECODE', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID')}
        env['COWORK_TURN'] = request  # tells the simplify gate to stay out
        before = tree_state(root) if no_edit else None
        with tempfile.TemporaryFile() as stdin, stream.open('ab') as out:
            stdin.write(prompt.encode())
            stdin.seek(0)
            child = subprocess.Popen(argv, cwd=root, stdin=stdin, stdout=out, env=env,
                                     start_new_session=True, pass_fds=(lock_fd,))
        os.pwrite(lock_fd, f'{child.pid}\n'.encode(), 0)  # for `kill`, should this runner die first
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
        verdict = exit_file.read_text().strip()  # `kill` may have won during conclude()
        os.pwrite(lock_fd, f'{child.pid if child else ""}\n{verdict}\n'.encode(), 0)


def conclude(side, box, status, reply, stream, new_session, before, root):
    """What the finished CLI left behind: bind a new session, validate the
    reply, check a --no-edit tree, surface Codex's in-stream error."""
    if side == 'codex':
        if not session_of(box):
            thread = next((e['thread_id'] for e in events(stream) if 'thread_id' in e), None)
            if thread:
                update_side(box, session=thread)  # a thread that exists resumes, even after a failed turn
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
                update_side(box, session=new_session)
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
    return {'error': 'failed', 'killed': 'was killed', 'skipped': 'was skipped',
            'died': 'died without recording a verdict'}.get(status, f'exited {status}'), FAILED


def deliver(box, request):
    """Print a settled request's outcome, then remove its files: the
    coworker's session store keeps the turn. Under admission, so two readers
    cannot both claim it, and only once stdout has taken the text."""
    with admission(box):
        lock = box / f'{request}.lock'
        if not lock.exists():
            die(f'no request {request} in this worktree: delivered already, or never sent', BUSY)
        if held(lock):
            die(f'{request} is {state(box, request)}; read it once it settles', BUSY)
        materialise(box, request)
        what, code = verdict(box, request)
        if code == FAILED:
            err = box / f'{request}.err'  # absent if send died before starting the runner
            text = f'cowork request {request} {what}\n{text_of(err)[-2000:] if err.exists() else ""}'
        else:
            if code == MALFORMED:
                print(f'cowork request {request}: {what}', file=sys.stderr)
            text = text_of(box / f'{request}.reply')
        print(text, end='' if text.endswith('\n') else '\n', flush=True)
        remove(box, request)
    return code


def settled(gitdir):
    """{request: what happened} for every request in this worktree whose
    runner has let go and whose reply is still here. Read under admission,
    so a request being enqueued or delivered is never seen half-made."""
    done = {}
    for side in SIDES:
        box = gitdir / 'cowork' / side
        if box.exists():
            with admission(box):
                for request in requests(box):
                    if not held(box / f'{request}.lock'):
                        materialise(box, request)
                        done[request] = verdict(box, request)[0]
    return done


def find(gitdir, request):
    box = gitdir / 'cowork' / request.split('-')[0]
    if not (box / f'{request}.lock').exists():
        die(f'no request {request} in this worktree: delivered already, or never sent', BUSY)
    return box


def follow(box, request):
    """Print the event stream as it grows, until the runner lets go."""
    stream, lock = box / f'{request}.jsonl', box / f'{request}.lock'
    try:
        handle = stream.open('rb')  # not exists() first: a delivery may remove it in between
    except FileNotFoundError:
        die(f'{request} has no stream here: delivered already, or never sent; its turn is in the '
            f'coworker\'s own session store ({STORE[box.name]})', BUSY)
    try:
        with handle:
            done = False
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
                elif done:
                    return
                elif not held(lock):
                    done = True  # one more read: the runner may have appended and let go since the last one
                else:
                    time.sleep(0.2)
    except BrokenPipeError:  # the reader left, e.g. `tail | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def enqueue(box, root, side, task, no_edit, model, effort):
    """Record the request behind the last live one and start its runner,
    which holds the request lock from birth; the caller keeps nothing open."""
    with admission(box):
        model, effort = settle_pair(box, model, effort)
        for request in requests(box):
            materialise(box, request)
        queue = live(box)
        request = f'{side}-{datetime.datetime.now():%Y%m%d-%H%M%S-%f}'
        (box / f'{request}.task').write_text(task)
        (box / f'{request}.jsonl').touch()  # so `tail` has a file the instant the id is printed
        behind = os.open(box / f'{queue[-1]}.lock', os.O_RDONLY) if queue else None
        with (box / f'{request}.lock').open('w') as lock, (box / f'{request}.err').open('ab') as err:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fds = (lock.fileno(),) + ((behind,) if queue else ())
            argv = [sys.executable, __file__, '_run', side, request, queue[-1] if queue else '-',
                    str(behind if queue else '-'), str(int(no_edit)), str(lock.fileno()), model, effort]
            # Double fork: the runner's parent exits at once, so it hangs off init, in its own
            # session, and a harness that kills `send` with its descendants cannot reach it.
            middle = os.fork()
            if middle == 0:
                subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                 start_new_session=True, pass_fds=fds)
                os._exit(0)
            os.waitpid(middle, 0)
        if queue:
            os.close(behind)
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
    send.add_argument('--model', help='the coworker\'s model from now on; first send defaults to your own tier')
    send.add_argument('--effort', choices=EFFORTS, help='its reasoning effort from now on; first send defaults to yours')
    sub.add_parser('kill', help='stop a request, queued or running, and everything its coworker spawned').add_argument('request')
    sub.add_parser('read', help='print the reply of a settled request whose send died, and remove it').add_argument('request')
    watch = sub.add_parser('watch', help='print "<id>  <what happened>" for each request as it settles, forever')
    watch.add_argument('request', nargs='*', help='already settled requests to report first')
    sub.add_parser('status', help='sessions and undelivered requests in this worktree')
    tail = sub.add_parser('tail', help='follow the event stream of a request (default: the latest) until it settles')
    tail.add_argument('request', nargs='?')
    reset = sub.add_parser('reset', help='forget a side\'s session and its requests; the next send starts anew')
    reset.add_argument('side', choices=SIDES)
    runner = sub.add_parser('_run', help=argparse.SUPPRESS)  # the detached child of `send`
    for name in ('side', 'request', 'behind', 'behind_fd', 'no_edit', 'lock_fd', 'model', 'effort'):
        runner.add_argument(name)
    a = parser.parse_args(argv)

    root, gitdir = git_dir()
    if a.cmd == '_run':
        run(a.side, gitdir / 'cowork' / a.side, root, a.request, None if a.behind == '-' else a.behind,
            None if a.behind_fd == '-' else int(a.behind_fd), a.no_edit == '1', int(a.lock_fd), a.model, a.effort)
        return 0

    if a.cmd == 'send':
        side = coworker(a.to)
        box = gitdir / 'cowork' / side
        box.mkdir(parents=True, exist_ok=True)
        if a.task_file:
            task = a.task_file.read_text()
        else:
            task = sys.stdin.read() if a.task == '-' else a.task
        if not task.strip():
            die('the task resolved to nothing')
        request = enqueue(box, root, side, task, a.no_edit, a.model, a.effort)
        print(request, flush=True)
        held(box / f'{request}.lock', wait=True)  # wait through teardown, even if the verdict exists
        return deliver(box, request)

    if a.cmd == 'read':
        return deliver(find(gitdir, a.request), a.request)

    if a.cmd == 'kill':
        box = find(gitdir, a.request)
        exit_file, lock = box / f'{a.request}.exit', box / f'{a.request}.lock'
        with admission(box):  # not after a delivery removed the request, which would leave an orphan verdict
            if not lock.exists():
                die(f'no request {a.request} in this worktree: delivered already', BUSY)
            pid = lock_lines(lock.read_text())[0]
            if publish(exit_file, 'killed\n') and pid and holds(int(pid), lock):
                kill_tree(int(pid))  # our CLI, orphaned or not; a runner still there also sees the verdict
            print(f'{a.request}: {exit_file.read_text().strip()}')
        return 0

    if a.cmd == 'watch':
        for request in a.request:
            find(gitdir, request)
        seen = set(settled(gitdir)) - set(a.request)  # the past is reported only where asked
        while True:
            for request, what in sorted(settled(gitdir).items()):
                if request not in seen:
                    seen.add(request)
                    print(request, ' ', what, flush=True)
            time.sleep(0.5)

    if a.cmd == 'status':
        for side in SIDES:
            box = gitdir / 'cowork' / side
            if not box.exists():
                print(f'{side}: session none')
                continue
            with admission(box):  # a delivery in progress would remove files under state()
                session, model, effort = side_state(box)
                print(f'{side}: session {session or "none"}' + (f', {model} at {effort} effort' if model else ''))
                for request in requests(box):
                    print(f'  {request}  {state(box, request)}')
        return 0

    if a.cmd == 'tail':
        if a.request:
            follow(gitdir / 'cowork' / a.request.split('-')[0], a.request)
            return 0
        streams = []
        for stream in (gitdir / 'cowork').glob('*/*.jsonl'):
            with contextlib.suppress(FileNotFoundError):  # delivered between glob and stat
                streams.append((stream.stat().st_mtime, stream))
        if not streams:
            die('no undelivered request in this worktree', BUSY)
        latest = max(streams)[1]
        follow(latest.parent, latest.stem)
        return 0

    if a.cmd == 'reset':
        box = gitdir / 'cowork' / a.side
        if not box.exists():
            print(f'{a.side}: no session')
            return 0
        with admission(box):
            queue = live(box)
            if queue:
                die(f'{a.side} still has {len(queue)} request(s) pending, first {queue[0]}; not resetting under them', BUSY)
            gone = requests(box)
            for request in gone:
                remove(box, request)
            session = box / 'session'
            if session.exists():
                session.unlink()
                print(f'{a.side}: session forgotten' + (f', {len(gone)} undelivered request(s) removed' if gone else ''))
            else:
                print(f'{a.side}: no session')
        return 0


if __name__ == '__main__':
    sys.exit(main())
