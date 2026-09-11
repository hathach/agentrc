#!/usr/bin/env python3
"""Run coworker turns in a detached process, resuming one session per lane, one turn at a time per lane.

  cowork.py send [--to codex|claude] [--lane L] [--read-only] [--no-edit] [--model M] [--effort E] (--task TEXT | --task-file F | --task -)
  cowork.py kill <id>
  cowork.py read <id>
  cowork.py watch [<id>...]
  cowork.py status
  cowork.py tail [<id>]
  cowork.py reset <side> <lane>|all

A lane is one resumed session of a side, with files under
<git dir>/cowork/<side>/<lane>/: `session` holds the session id, the model
and the effort in use. The first send on a lane sets the model and effort
from the flags, else from the caller's own session record mapped to the same
token-cost tier on the other side; later sends reuse them until flags
replace them or reset forgets them. Three kinds of lane: `main` works in
this checkout; a lane created with --read-only works in this checkout too
and every send to it is --no-edit; any other lane works in its own worktree
.worktrees/cowork-<side>-<lane> on branch cowork/<host branch>/<side>-<lane>,
created on its first send and brought to this checkout's HEAD before every
send: its own commits, those after the `base` it was last synced to, are
rebased onto HEAD; refused when dirty or conflicting. Its kind is the
`read-only` marker file or the absence of one.
A request has files only from send until its reply is delivered; the
coworker's own session store keeps the turn. Meanwhile: .task until the
runner has read it, .lock (the runner and CLI hold its flock through
teardown, so liveness does not depend on pid reuse; the CLI pid is written
inside), .jsonl (CLI stdout), .err (stderr), and at settle .reply and .exit
(the verdict, first writer wins). One request per lane is in flight: a send
while one is refused.

send waits for the reply, prints it and removes the request; read does the
same for a reply whose send died; watch reports settled requests still
undelivered; reset removes everything of a lane, its worktree included once
its branch is merged. Exit codes: 1 failed, 3 unknown or delivered request,
a lane busy, not ready or of the wrong kind, or reset refused, 4 missing
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
FOOTER = re.compile(r'^Files touched: \S', re.M)
LANE = re.compile(r'^(?!all$)[a-z0-9-]{1,40}$')
BOOTSTRAP = (
    'You are the {side} coworker on the cowork channel of the checkout at {root}: a headless,\n'
    'resumed session driven by the other coding agent, not by a human. Load the `cowork` skill\n'
    'for the rules. Never push, open a PR or post a comment on a request from this channel.\n\n')
HEADER = ('cowork request {id} from {me} on lane {lane}, answered by {model} at {effort} effort. Scope: {scope}.\n'
          '{where}End your reply with a line "Files touched: <paths>" or "Files touched: none".\n---\n')
WHERE = 'Your checkout is the worktree {root} on branch {branch}, based on {base} of the host checkout; commit there.\n'
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


def git(cwd, *args, check=True):
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if check and done.returncode:
        die(f'git {args[0]} in {cwd}: {done.stderr.strip()}')
    return done


def box_of(gitdir, side, lane):
    """A lane's box. The pre-lane layout, files directly under the side,
    becomes its `main`: under that layout's own admission lock, and not
    while a request of it still runs, since its runner would settle at the
    old paths."""
    side_dir = gitdir / 'cowork' / side
    if side_dir.is_dir():
        with admission(side_dir):  # that layout's lock, which stays where it is: whoever is moving files holds it
            if (side_dir / 'session').is_file():
                if any(held(lock) for lock in side_dir.glob('*.lock')):
                    die(f'a request from the previous cowork layout still runs under {side_dir}; wait for it', BUSY)
                (side_dir / 'main').mkdir(exist_ok=True)
                for old in sorted(side_dir.iterdir(), key=lambda f: f.name == 'session'):  # the session last
                    if old.is_file() and old.name != 'lock':
                        old.rename(side_dir / 'main' / old.name)
    return side_dir / lane


def lanes(gitdir, side):
    side_dir = box_of(gitdir, side, 'main').parent
    return sorted(p.name for p in side_dir.iterdir() if p.is_dir()) if side_dir.is_dir() else []


def boxes(gitdir):
    return [gitdir / 'cowork' / side / lane for side in SIDES for lane in lanes(gitdir, side)]


def kind(box):
    if box.name == 'main':
        return 'main'
    return 'read-only' if (box / 'read-only').exists() else 'worktree'


def lane_root(root, box):
    return root / '.worktrees' / f'cowork-{box.parent.name}-{box.name}'


def tree_of(box, host):
    """Where a lane's coworker works: its worktree, or the host checkout."""
    return lane_root(host, box) if kind(box) == 'worktree' else host


def claim(tree, box):
    """The tree is this lane's worktree, on its branch, and clean; else refuse
    before any git command that would move or delete it. Returns the branch."""
    top = git(tree, 'rev-parse', '--show-toplevel', check=False).stdout.strip()
    branch = git(tree, 'symbolic-ref', '--short', 'HEAD', check=False).stdout.strip()
    if top != str(tree) or not (branch.startswith('cowork/') and branch.endswith(f'/{box.parent.name}-{box.name}')):
        die(f'{tree} is not lane {box.name}\'s worktree (toplevel {top or "none"}, branch {branch or "none"}); '
            f'move it aside', BUSY)
    dirty = git(tree, 'status', '--porcelain').stdout
    if dirty:
        die(f'lane {box.name} has uncommitted changes in {tree}; commit or discard them first:\n{dirty}', BUSY)
    return branch


def sync_lane(root, box):
    """The lane's worktree at the host's HEAD with the lane's own commits,
    those since the base it was last synced to, rebased on top; created on
    first use. Refused while dirty or when the rebase conflicts. The base
    rather than ancestry, so a host history rewritten under the lane still
    syncs. Under admission."""
    tree, lane = lane_root(root, box), box.name
    head = git(root, 'rev-parse', 'HEAD').stdout.strip()
    if not tree.exists():
        git(root, 'worktree', 'prune')
        host = git(root, 'symbolic-ref', '--short', 'HEAD', check=False)
        if host.returncode:
            die("the host checkout is detached; a worktree lane names its branch after the host's", BUSY)
        branch = f'cowork/{host.stdout.strip()}/{box.parent.name}-{lane}'
        if git(root, 'check-ignore', '-q', '.worktrees', check=False).returncode:
            ignore = root / '.gitignore'
            text = ignore.read_text() if ignore.exists() else ''
            ignore.write_text(text + ('' if not text or text.endswith('\n') else '\n') + '.worktrees/\n')
            print(f'added .worktrees/ to {ignore}; commit it', file=sys.stderr)
        known = git(root, 'rev-parse', '-q', '--verify', f'refs/heads/{branch}', check=False).returncode == 0
        if known and not (box / 'base').exists():  # a branch left by an earlier lane: which of its commits are its own?
            die(f'branch {branch} exists but lane {lane} has no record of its base; delete or rename the branch', BUSY)
        git(root, 'worktree', 'add', '-q', *([] if known else ['-b', branch]), str(tree), branch if known else 'HEAD')
        if not known:
            (box / 'base').write_text(head + '\n')
    claim(tree, box)
    base = (box / 'base').read_text().strip()
    if git(tree, 'rev-parse', 'HEAD').stdout.strip() == base:
        git(tree, 'reset', '-q', '--hard', head)
    elif git(tree, 'rebase', '-q', '--onto', head, base, check=False).returncode:
        files = git(tree, 'diff', '--name-only', '--diff-filter=U').stdout
        git(tree, 'rebase', '--abort', check=False)
        die(f'lane {lane} does not rebase onto {head[:12]}; integrate or reset it first, conflicts in:\n{files}', BUSY)
    (box / 'base').write_text(head + '\n')


def drop_lane(root, box):
    """Remove a worktree lane's tree and branch; refused while the tree is
    dirty or the branch has commits the host does not."""
    tree, lane, side = lane_root(root, box), box.name, box.parent.name
    git(root, 'worktree', 'prune')
    if not tree.exists():
        return
    branch = claim(tree, box)
    if git(root, 'merge-base', '--is-ancestor', branch, 'HEAD', check=False).returncode:
        die(f'lane {lane} has commits on {branch} that HEAD lacks; merge or cherry-pick them first, '
            f'or delete the branch', BUSY)
    git(root, 'worktree', 'remove', str(tree))
    git(root, 'branch', '-q', '-d', branch)
    print(f'{side}/{lane}: worktree {tree} removed, branch {branch} deleted')


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
    """Only one process reads or changes a side's requests at a time."""
    with (box / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def held(lock_file, wait=False):
    """True while a runner, its CLI or anything they spawned still holds this
    request's lock exclusively; with `wait`, block until they let go."""
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


def cli_pid(lock_file):
    """The CLI pid the runner wrote into the request lock, '' before it got there."""
    return lock_file.read_text().strip()


def requests(box):
    return sorted(p.stem for p in box.glob('*.lock'))


def state(box, request):
    exit_file, lock = box / f'{request}.exit', box / f'{request}.lock'
    if exit_file.exists():
        return exit_file.read_text().strip()
    return 'running' if held(lock) else 'died'


def remove(box, request):
    for leftover in box.glob(f'{request}.*'):
        leftover.unlink()


def running(box):
    """The request whose runner or CLI is still there, if any: a verdict alone
    does not free the side, a killed turn is still being torn down."""
    return next((r for r in requests(box) if held(box / f'{r}.lock')), None)


def side_state(box):
    """(session id or None, model, effort) of a side; the session id is empty
    until the first turn binds one."""
    lines = (box / 'session').read_text().split('\n') if (box / 'session').exists() else []
    lines += [''] * 3
    return lines[0] or None, lines[1], lines[2]


def write_side(box, **fields):
    """Change some of a side's session id, model and effort: a field-wise
    merge, replaced in one step so a reader never sees a truncated file.
    The caller holds admission."""
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
    saved for later sends. Under admission with the start."""
    saved_model, saved_effort = side_state(box)[1:]
    model, effort = model or saved_model, effort or saved_effort
    if not (model and effort):  # first send on this side: start from what drives it
        own_model, own_effort = own_model_and_effort()
        model, effort = model or equivalent(own_model), effort or own_effort
    write_side(box, model=model, effort=effort)


def session_of(box):
    return side_state(box)[0]


def compose(session, side, request, task, no_edit, root, model, effort, lane, where):
    sender = 'claude' if side == 'codex' else 'codex'
    header = HEADER.format(id=request, me=sender, lane=lane, scope=SCOPE[no_edit], model=model, effort=effort, where=where)
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


def run(side, box, host, request, no_edit, lock_fd):
    """One coworker turn, in the detached runner, whose stderr is <id>.err.
    Publishes the verdict unless `kill` got there first, then lets go."""
    exit_file, reply, stream = (box / f'{request}.{ext}' for ext in ('exit', 'reply', 'jsonl'))
    status, child, before = 'error', None, None
    root = tree_of(box, host)
    try:
        if exit_file.exists():  # killed before it started
            status = 'killed'
            return
        session, model, effort = side_state(box)  # settled by start, under the lock this runner holds
        where = ''
        if kind(box) == 'worktree':
            where = WHERE.format(root=root, branch=git(root, 'symbolic-ref', '--short', 'HEAD').stdout.strip(),
                                 base=git(host, 'rev-parse', '--short=12', 'HEAD').stdout.strip())
        task = box / f'{request}.task'
        prompt = compose(session, side, request, task.read_text(), no_edit, root, model, effort, box.name, where)
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
        publish(exit_file, status + '\n')  # `kill` may have won during conclude()


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
    if side == 'codex' and status not in ('0', 'edited', 'killed'):  # a turn that produced a reply, or was cut
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
    """(what happened, exit code) of a request whose runner has let go. A
    runner killed outright left no verdict: record one, so the request is
    delivered like any other."""
    exit_file = box / f'{request}.exit'
    publish(exit_file, 'died\n')
    status = exit_file.read_text().strip()
    if status == 'edited':
        return 'the tree changed during a --no-edit turn; check git status', MALFORMED
    if status == '0':
        if FOOTER.search(text_of(box / f'{request}.reply')):
            return 'replied', 0
        return 'the reply has no "Files touched" line; re-read the tree yourself', MALFORMED
    return {'error': 'failed', 'killed': 'was killed',
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
    so a request being created or delivered is never seen half-made."""
    done = {}
    for box in boxes(gitdir):
        with admission(box):
            for request in requests(box):
                if not held(box / f'{request}.lock'):
                    done[request] = verdict(box, request)[0]
    return done


def locate(gitdir, request):
    return next((box for box in boxes(gitdir) if (box / f'{request}.lock').exists()), None)


def find(gitdir, request):
    box = locate(gitdir, request)
    if box is None:
        die(f'no request {request} in this worktree: delivered already, or never sent', BUSY)
    return box


def follow(box, request):
    """Print the event stream as it grows, until the runner lets go."""
    stream, lock = box / f'{request}.jsonl', box / f'{request}.lock'
    try:
        handle = stream.open('rb')  # not exists() first: a delivery may remove it in between
    except FileNotFoundError:
        die(f'{request} has no stream here: delivered already, or never sent; its turn is in the '
            f'coworker\'s own session store ({STORE[box.parent.name]})', BUSY)
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


def start(box, root, side, task, no_edit, read_only, model, effort):
    """Record the request and start its runner, which holds the request lock
    from birth; the caller keeps nothing open. Refused while one is in flight,
    and for a lane not ready: --read-only on a worktree lane, a worktree that
    is dirty or does not rebase."""
    lane = box.name
    with admission(box):
        busy = running(box)
        if busy:
            die(f'{side}/{lane} is busy with {busy}; wait for it, or kill it', BUSY)
        if lane == 'main':
            if read_only:
                die('main is this checkout and writable; --read-only creates a named lane', BUSY)
        else:
            if read_only and (box / 'session').exists() and kind(box) != 'read-only':
                die(f'{side}/{lane} is a worktree lane; --read-only creates a lane, it cannot convert one', BUSY)
            if read_only:
                (box / 'read-only').touch()
            if kind(box) == 'read-only':
                no_edit = True
            else:
                sync_lane(root, box)
        settle_pair(box, model, effort)
        request = f'{side}-{lane}-{datetime.datetime.now():%Y%m%d-%H%M%S-%f}'
        (box / f'{request}.task').write_text(task)
        (box / f'{request}.jsonl').touch()  # so `tail` has a file the instant the id is printed
        with (box / f'{request}.lock').open('w') as lock, (box / f'{request}.err').open('ab') as err:
            fcntl.flock(lock, fcntl.LOCK_EX)
            argv = [sys.executable, __file__, '_run', side, lane, request, str(int(no_edit)), str(lock.fileno())]
            # Double fork: the runner's parent exits at once, so it hangs off init, in its own
            # session, and a harness that kills `send` with its descendants cannot reach it.
            middle = os.fork()
            if middle == 0:
                subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                 start_new_session=True, pass_fds=(lock.fileno(),))
                os._exit(0)
            os.waitpid(middle, 0)
    return request


def reset(root, gitdir, side, lane):
    """Forget a lane: its session, requests, marker, base and worktree. The
    box and its admission lock stay, so a send waiting on that lock is not
    left holding an unlinked inode."""
    box = box_of(gitdir, side, lane)
    if not box.exists():
        print(f'{side}/{lane}: no session')
        return
    with admission(box):
        busy = running(box)
        if busy:
            die(f'{side}/{lane} is busy with {busy}; not resetting under it', BUSY)
        if kind(box) == 'worktree':
            drop_lane(root, box)
        gone = requests(box)
        for request in gone:
            remove(box, request)
        had = (box / 'session').exists()
        for name in ('session', 'read-only', 'base'):  # a lane half-made by a failed first send goes too
            (box / name).unlink(missing_ok=True)
        if had:
            print(f'{side}/{lane}: session forgotten' + (f', {len(gone)} undelivered request(s) removed' if gone else ''))
        else:
            print(f'{side}/{lane}: no session')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    send = sub.add_parser('send', help='one turn of the coworker; prints the request id, then the reply')
    send.add_argument('--to', choices=SIDES, help='which coworker; defaults to codex when run from Claude Code')
    send.add_argument('--lane', default='main', help='which session of that coworker (default: main, this checkout); '
                                                    'any other lane works in its own worktree unless created --read-only')
    send.add_argument('--read-only', action='store_true',
                      help='on a lane\'s first send: it works in this checkout and every send to it is --no-edit')
    task = send.add_mutually_exclusive_group(required=True)
    task.add_argument('--task', help='literal text, or - for stdin')
    task.add_argument('--task-file', type=Path)
    send.add_argument('--no-edit', action='store_true', help='a question or review: the coworker must not edit')
    send.add_argument('--model', help='the coworker\'s model from now on; first send defaults to your own tier')
    send.add_argument('--effort', choices=EFFORTS, help='its reasoning effort from now on; first send defaults to yours')
    sub.add_parser('kill', help='stop a running request and everything its coworker spawned').add_argument('request')
    sub.add_parser('read', help='print the reply of a settled request whose send died, and remove it').add_argument('request')
    watch = sub.add_parser('watch', help='print "<id>  <what happened>" for each request as it settles; '
                                        'with ids, exit once each is reported or delivered, else run forever')
    watch.add_argument('request', nargs='*', help='requests to wait for, reported even if already settled')
    sub.add_parser('status', help='lanes and undelivered requests in this worktree')
    tail = sub.add_parser('tail', help='follow the event stream of a request (default: the latest) until it settles')
    tail.add_argument('request', nargs='?')
    reset_ = sub.add_parser('reset', help='forget a lane\'s session and its requests, remove its worktree once merged; '
                                          'the next send starts anew')
    reset_.add_argument('side', choices=SIDES)
    reset_.add_argument('lane', help='a lane name, or all')
    runner = sub.add_parser('_run', help=argparse.SUPPRESS)  # the detached child of `send`
    for name in ('side', 'lane', 'request', 'no_edit', 'lock_fd'):
        runner.add_argument(name)
    a = parser.parse_args(argv)

    root, gitdir = git_dir()
    if a.cmd == '_run':
        run(a.side, gitdir / 'cowork' / a.side / a.lane, root, a.request, a.no_edit == '1', int(a.lock_fd))
        return 0

    if a.cmd == 'send':
        side = coworker(a.to)
        if not LANE.match(a.lane):
            die(f'lane names are [a-z0-9-], up to 40, and not "all": {a.lane!r}')
        box = box_of(gitdir, side, a.lane)
        box.mkdir(parents=True, exist_ok=True)
        if a.task_file:
            task = a.task_file.read_text()
        else:
            task = sys.stdin.read() if a.task == '-' else a.task
        if not task.strip():
            die('the task resolved to nothing')
        request = start(box, root, side, task, a.no_edit, a.read_only, a.model, a.effort)
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
            pid = cli_pid(lock)
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
            if a.request and all(r in seen or locate(gitdir, r) is None
                                 for r in a.request):  # each named request reported, or delivered by its own send
                return 0
            time.sleep(0.5)

    if a.cmd == 'status':
        for side in SIDES:
            shown = 0
            for lane in lanes(gitdir, side):
                box = gitdir / 'cowork' / side / lane
                with admission(box):  # a delivery in progress would remove files under state()
                    if not (box / 'session').exists() and not requests(box):
                        continue  # reset, and nothing since
                    shown += 1
                    session, model, effort = side_state(box)
                    where = {'main': '', 'read-only': ', read-only', 'worktree': f', in {lane_root(root, box)}'}[kind(box)]
                    print(f'{side}/{lane}: session {session or "none"}' + (f', {model} at {effort} effort' if model else '') + where)
                    for request in requests(box):
                        print(f'  {request}  {state(box, request)}')
            if not shown:
                print(f'{side}: no lane')
        return 0

    if a.cmd == 'tail':
        if a.request:
            box = locate(gitdir, a.request)
            if box is None:
                side = a.request.split('-')[0]
                die(f'{a.request} has no stream here: delivered already, or never sent; its turn is in the '
                    f'coworker\'s own session store ({STORE.get(side, "?")})', BUSY)
            follow(box, a.request)
            return 0
        streams = []
        for box in boxes(gitdir):
            for stream in box.glob('*.jsonl'):
                with contextlib.suppress(FileNotFoundError):  # delivered between glob and stat
                    streams.append((stream.stat().st_mtime, stream))
        if not streams:
            die('no undelivered request in this worktree', BUSY)
        latest = max(streams)[1]
        follow(latest.parent, latest.stem)
        return 0

    if a.cmd == 'reset':
        if a.lane != 'all' and not LANE.match(a.lane):
            die(f'lane names are [a-z0-9-], up to 40: {a.lane!r}')
        for lane in lanes(gitdir, a.side) if a.lane == 'all' else [a.lane]:
            reset(root, gitdir, a.side, lane)
        return 0


if __name__ == '__main__':
    sys.exit(main())
