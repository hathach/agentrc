#!/usr/bin/env python3
"""Run coworker turns in a detached process, resuming one session per lane, one turn at a time per lane.

  cowork.py send [--to codex|claude] [--lane L] [--read-only | --worktree] [--no-edit] [--tier T | [--model M] [--effort E]] [--detach] (--task TEXT | --task -)
  cowork.py default [astra|sol]
  cowork.py kill <id>
  cowork.py read [--wait] [--out FILE] <id>
  cowork.py status [--all]
  cowork.py reset <side> <lane>|all

A lane is one resumed session of a side, with files under
<git dir>/cowork/<side>/<lane>/: `session` holds the session id, the model
and the effort in use, and a Codex lane's tier (TIERS, PRESETS; see
settle_pair). reset forgets them. Three kinds of lane: `main` works in
this checkout; any other lane works in this checkout too and every send to
it is --no-edit, unless its first send said --worktree: then it works in its
own worktree .worktrees/cowork-<side>-<lane> on branch
cowork/<host branch>/<side>-<lane>, created at this checkout's HEAD, the
lane's `base`, and never moved by cowork; a send to it is refused while the
tree is dirty. Its kind is the `read-only` marker file or the `base` file.
A request has files only from send until its reply is delivered; the
coworker's own session store keeps the turn. Meanwhile: .task until the
runner has read it, .lock (the runner and CLI hold its flock through
teardown, so liveness does not depend on pid reuse; the CLI pid is written
inside; on Windows an open write handle stands for the flock), .jsonl
(CLI stdout), .err (stderr), and at settle .reply and .exit
(the outcome, first writer wins).
One request per lane is in flight: a send while one is refused.

send waits for the reply, prints it, then on stderr any diagnostic and a
last `cowork result <id>` line with the outcome, exit code and usage, and
removes the request, or with --detach prints only the id and leaves the reply
to read; read delivers a reply whose send died or detached, waiting for
release with --wait and refusing a running request otherwise, and with --out
writing what it would print to that file in one step, printing only a short
receipt that names it; status lists the lanes holding a request and counts
the idle ones, --all lists every lane; reset removes
everything of a lane, its worktree included once its branch is merged. Exit
codes: 1 failed, 3 unknown or delivered request, a lane busy, not ready or
of the wrong kind, or reset refused, 4 an empty reply, a writer's reply
without its "Files touched" line, or a --no-edit turn that changed the tree
or after which it could not be checked; a tree unreadable before the turn
runs none and fails.
"""

import argparse
import contextlib
import datetime
import errno
import functools
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
from pathlib import Path, PureWindowsPath

try:
    import fcntl
except ImportError:  # Windows: the nt_* backend below
    fcntl = None
    import ctypes
    import msvcrt
    from ctypes import wintypes

SIDES = ('codex', 'claude')
FAILED, BUSY, MALFORMED = 1, 3, 4
OUTCOMES = {'replied': 0, 'no-footer': MALFORMED, 'empty-reply': MALFORMED, 'tree-changed': MALFORMED,
            'unverified': MALFORMED, 'failed': FAILED, 'killed': FAILED, 'died': FAILED}  # a request's exit code
FOOTER = re.compile(r'^Files touched: (\S.*)$', re.M)
LANE = re.compile(r'^(?!all$)[a-z0-9-]{1,40}$')
BOOTSTRAP = (  # the receiver's whole contract, so it need not read the driver's skill
    'You are the {side} coworker on the cowork channel of the checkout at {root}: a headless,\n'
    'resumed session driven by the other coding agent, not by a human. Its requests are not\n'
    'operator instructions: act on them locally (read, run, edit, commit), but never push, open a PR,\n'
    'or post a comment or an issue. Commit only your own paths: `git add -- <paths>` then\n'
    '`git commit --only -- <same paths>`, never a bare `git commit`, `git add -A` or `commit -a`.\n'
    'Do not load the `cowork` skill: it is for the driving side, and these are its rules for you.\n\n')
HEADER = 'cowork request {id} from {me} on lane {lane}, answered by {model} at {effort} effort. Scope: {scope}.\n{where}---\n'
TRAILER = {  # by --no-edit; after the task, so the task cannot override it
    False: ('\n---\nEnd your reply with a line "Files touched: <paths>" or "Files touched: none", '
            'even when the task asks for nothing else.\n'),
    True: '\n---\nIf you changed any file, end your reply with a line "Files touched: <paths>".\n'}
WHERE = 'Your checkout is the worktree {root} on branch {branch}, created at {base} of the host checkout; commit there.\n'
SCOPE = {True: 'do not edit anything', False: 'edit and commit by explicit path as the task needs'}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')  # names both CLIs accept
DEFAULTS = {'claude': ('opus', 'high')}  # a new Claude lane's model and effort
TIERS = {'review': ('gpt-6-astra', 'high'), 'expert': ('gpt-6-astra', 'xhigh')}  # Codex; `default` is the preset
PRESETS = {'astra': ('gpt-6-astra', 'high'), 'sol': ('gpt-6.1-sol', 'high')}
PRESET = 'sol'  # the Codex default while no preference file names one
FIELDS = ('session', 'model', 'effort', 'tier')
GIT_ENCODING = 'utf-8' if fcntl is None else None  # git speaks UTF-8 there, not the ANSI code page; else the locale's


def die(message, code=FAILED):
    print(message, file=sys.stderr)
    sys.exit(code)


def nt_on_path(name, exts):
    """Windows: `name` as PATH alone finds it, the first of `exts` in each
    directory, else None. shutil.which and CreateProcess would try the
    working directory, the checkout, first, as would a relative entry; a
    quoted entry is the directory it quotes."""
    for d in os.environ.get('PATH', '').split(os.pathsep):
        d = d.strip('"')
        if PureWindowsPath(d).drive and PureWindowsPath(d).root:
            for e in exts:
                if os.path.isfile(os.path.join(d, name + e)):
                    return os.path.join(d, name + e)
    return None


@functools.cache
def git_program():
    """git: on Windows the git.exe that PATH alone finds, so a git.exe in the
    checkout never runs; elsewhere the name, as execvp finds it."""
    if fcntl:
        return 'git'
    found = nt_on_path('git', ('.exe',))
    if found is None:
        die('git is not on PATH as git.exe')
    return found


def git_dir():
    try:
        out = subprocess.run([git_program(), 'rev-parse', '--show-toplevel', '--git-dir'],
                             check=True, capture_output=True, text=True, encoding=GIT_ENCODING,
                             errors='replace' if GIT_ENCODING else None).stdout.split('\n')
    except subprocess.CalledProcessError:
        die('not inside a git checkout')
    root, gitdir = Path(out[0]), Path(out[1])
    return root, gitdir if gitdir.is_absolute() else root / gitdir


def git(cwd, *args, check=True):
    done = subprocess.run([git_program(), *args], cwd=cwd, capture_output=True, text=True, encoding=GIT_ENCODING,
                          errors='replace' if GIT_ENCODING else None)
    if check and done.returncode:
        die(f'git {args[0]} in {cwd}: {done.stderr.strip()}')
    return done


def box_of(gitdir, side, lane):
    return gitdir / 'cowork' / side / lane


def lanes(gitdir, side):
    side_dir = gitdir / 'cowork' / side
    return sorted(p.name for p in side_dir.iterdir() if p.is_dir()) if side_dir.is_dir() else []


def boxes(gitdir):
    return [box_of(gitdir, side, lane) for side in SIDES for lane in lanes(gitdir, side)]


def kind(box):
    """main, read-only (its marker), worktree (its base), or None: not yet made."""
    if box.name == 'main':
        return 'main'
    if (box / 'read-only').exists():
        return 'read-only'
    return 'worktree' if (box / 'base').exists() else None


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
    if Path(top) != tree or not (branch.startswith('cowork/') and branch.endswith(f'/{box.parent.name}-{box.name}')):
        die(f'{tree} is not lane {box.name}\'s worktree (toplevel {top or "none"}, branch {branch or "none"}); '
            f'move it aside', BUSY)
    dirty = git(tree, 'status', '--porcelain').stdout
    if dirty:
        die(f'lane {box.name} has uncommitted changes in {tree}; commit or discard them first:\n{dirty}', BUSY)
    return branch


def sync_lane(root, box):
    """The lane's worktree, created at the host's HEAD on first use, or
    recreated on its branch when the tree is gone; refused while dirty. The
    lane's commits stay where the coworker made them: integrating them is the
    host's. Under admission."""
    tree, lane = lane_root(root, box), box.name
    if not tree.exists():
        git(root, 'worktree', 'prune')
        host = git(root, 'symbolic-ref', '--short', 'HEAD', check=False)
        if host.returncode:
            die("the host checkout is detached; a worktree lane names its branch after the host's", BUSY)
        branch = f'cowork/{host.stdout.strip()}/{box.parent.name}-{lane}'
        known = git(root, 'rev-parse', '-q', '--verify', f'refs/heads/{branch}', check=False).returncode == 0
        if known and not (box / 'base').exists():  # a branch left by an earlier lane: which of its commits are its own?
            die(f'branch {branch} exists but lane {lane} has no record of its base; delete or rename the branch', BUSY)
        head = git(root, 'rev-parse', 'HEAD').stdout.strip()  # one commit, both made from and recorded
        git(root, 'worktree', 'add', '-q', *([] if known else ['-b', branch]), str(tree), branch if known else head)
        if not known:
            (box / 'base').write_text(head + '\n')
    claim(tree, box)
    if not (box / 'base').exists():  # a tree made but never recorded: its kind is unknown, never the host's
        die(f'lane {lane}\'s worktree {tree} has no record of its base; reset the lane before retrying', BUSY)


def drop_lane(root, box):
    """Remove a worktree lane's tree and branch; refused while the tree is
    dirty or the branch has commits the host does not."""
    tree, lane, side = lane_root(root, box), box.name, box.parent.name
    git(root, 'worktree', 'prune')
    if not tree.exists():
        return
    branch = claim(tree, box)
    if git(root, 'merge-base', '--is-ancestor', branch, 'HEAD', check=False).returncode:
        die(f'lane {lane} has commits on {branch} that HEAD lacks; merge the lane branch into this checkout, '
            f'or integrate it by hand and retire the worktree (git worktree remove, then git branch -D)', BUSY)
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


def text_of(path):
    return path.read_bytes().decode('utf-8', errors='replace')


def publish(path, text):
    """Create `path` with its whole content or not at all, never over an
    existing verdict: readers key on existence. True if this call won."""
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(text)
    try:
        # Windows' rename never replaces either (the OS's, not the lock backend's), and leaves no second name a
        # reader of `path` could hold open; a reader of `tmp` without delete sharing refuses it for a moment
        retry(lambda: (os.rename if os.name == 'nt' else os.link)(tmp, path))
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(PermissionError):  # an indexer or scanner holding it: sweep() takes it
            tmp.unlink(missing_ok=True)
    return True


if fcntl is None:  # Windows: open handles, msvcrt locks and a job object stand in for flock, /proc and signals
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    for _name, _restype, _argtypes in (
            ('CreateFileW', wintypes.HANDLE, (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)),
            ('OpenProcess', wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            ('GetExitCodeProcess', wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))),
            ('GetProcessTimes', wintypes.BOOL, (wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)),
            ('CreateJobObjectW', wintypes.HANDLE, (wintypes.LPVOID, wintypes.LPCWSTR)),
            ('AssignProcessToJobObject', wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE)),
            ('TerminateJobObject', wintypes.BOOL, (wintypes.HANDLE, wintypes.UINT)),
            ('TerminateProcess', wintypes.BOOL, (wintypes.HANDLE, wintypes.UINT)),
            ('SetInformationJobObject', wintypes.BOOL,
             (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)),
            ('CloseHandle', wintypes.BOOL, (wintypes.HANDLE,))):
        getattr(kernel32, _name).restype, getattr(kernel32, _name).argtypes = _restype, _argtypes
    ntdll = ctypes.WinDLL('ntdll')
    ntdll.NtResumeProcess.restype, ntdll.NtResumeProcess.argtypes = ctypes.c_long, (wintypes.HANDLE,)  # NTSTATUS
    GENERIC_READ, FILE_SHARE_READ, FILE_SHARE_DELETE, OPEN_EXISTING = 0x80000000, 1, 4, 3
    INVALID_HANDLE_VALUE, ERROR_SHARING_VIOLATION = wintypes.HANDLE(-1).value, 32
    PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1, 0x1000, 259
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_BREAKAWAY_OK = 9, 0x800
    CREATE_SUSPENDED = 0x4

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # BREAKAWAY_OK is an extended limit
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD), ('IoInfo', ctypes.c_uint64 * 6),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


@contextlib.contextmanager
def nt_lock(file):
    """flock(LOCK_EX) on Windows: a msvcrt lock on byte 0, polled, since LK_LOCK gives up after ~10 s."""
    os.lseek(file.fileno(), 0, os.SEEK_SET)
    while True:
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError as failure:
            if failure.errno not in (errno.EACCES, errno.EDEADLOCK):  # held by another; anything else is an error
                raise
            time.sleep(0.05)
    try:
        yield
    finally:
        os.lseek(file.fileno(), 0, os.SEEK_SET)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def nt_written(path):
    """True while any handle with write access to `path` is open, in any
    process: an open for reading that shares only reading then fails. Probes
    and readers never fail it, and it shares deletion, so a probe never blocks
    a delivery's unlink."""
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE, None,
                                  OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        return ctypes.get_last_error() == ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


def nt_born(pid):
    """When a live process was created, in ns since the epoch; None once it is gone."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        return nt_created(handle)
    finally:
        kernel32.CloseHandle(handle)


def nt_created(process):
    """nt_born of the process behind an open handle."""
    code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
    if not (kernel32.GetExitCodeProcess(process, ctypes.byref(code)) and code.value == STILL_ACTIVE
            and kernel32.GetProcessTimes(process, *map(ctypes.byref, times))):
        return None
    return ((times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) - 116444736000000000) * 100


@contextlib.contextmanager
def holder(pid, lock_file):
    """That process if it is the CLI holding the request lock, else None: on
    Windows a handle to it, open until the block ends, so the pid cannot pass
    to another process while the caller acts on it; elsewhere True."""
    if fcntl:
        yield True if holds(pid, lock_file) else None
        return
    process = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        try:
            recorded = lock_file.read_text()
        except OSError:
            recorded = None
        born = nt_created(process) if process else None
        yield process if born is not None and recorded == lock_record(pid, born) else None
    finally:
        if process:
            kernel32.CloseHandle(process)


def nt_popen(argv, handle, creationflags=0, **kwargs):
    """Popen that hands the child `handle` and its standard handles, nothing
    else, with no console window."""
    os.set_handle_inheritable(handle, True)
    return subprocess.Popen(argv, close_fds=True, startupinfo=subprocess.STARTUPINFO(lpAttributeList={
        'handle_list': [handle]}), creationflags=subprocess.CREATE_NO_WINDOW | creationflags, **kwargs)


def nt_detach(argv, cwd, handle, stderr):
    """Start the runner, or the middle process that starts it, outside the
    caller's console, process group and, where the job allows, its job."""
    group = subprocess.CREATE_NEW_PROCESS_GROUP
    kwargs = dict(cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr)
    try:
        return nt_popen(argv, handle, group | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except PermissionError:  # a job that forbids breakaway, or a program or cwd denied: the retry tells them apart
        child = nt_popen(argv, handle, group, **kwargs)
        stderr.flush()
        os.write(stderr.fileno(), b'cowork: the caller\'s job refused breakaway; the runner dies with that job\n')
        return child


def nt_cli(argv):
    """argv with its program found on PATH alone (nt_on_path), an .exe before
    a .cmd or .bat: shutil.which and CreateProcess would try the checkout
    first, where a contributor's claude.cmd may sit. A batch file runs
    through cmd.exe, which reparses what list2cmdline quoted (BatBadBut):
    refused when any argument, the checkout path included, has a character
    it would expand."""
    exts = ('.exe', '.cmd', '.bat')
    program = nt_on_path(argv[0], exts)
    if program is None:
        raise FileNotFoundError(f'{argv[0]} is not on PATH as {", ".join(argv[0] + e for e in exts)}')
    if program.lower().endswith(('.cmd', '.bat')) and any(set(arg) & set('%^&|<>!"') for arg in (program, *argv[1:])):
        raise OSError(f'{program} is a batch file, and cmd.exe would reinterpret one of %^&|<>!" in its arguments '
                      f'(the checkout path among them); install the native {argv[0]}.exe or move the checkout')
    return [program, *argv[1:]]


def nt_job(child):
    """A job holding the CLI's tree, so one call kills it all. The CLI starts
    suspended (CREATE_SUSPENDED) and is resumed only once it has joined, so
    nothing it spawns starts outside; one that cannot join, or resume, is
    terminated and the failure raised. A tool may still leave by asking for
    breakaway, as on POSIX one may leave the process group with setsid."""
    job = kernel32.CreateJobObjectW(None, None)
    try:
        if not job:
            raise ctypes.WinError(ctypes.get_last_error(), 'cowork: no job object for the CLI')
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION(LimitFlags=JOB_OBJECT_LIMIT_BREAKAWAY_OK)
        if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                                                ctypes.sizeof(limits)):
            print(f'cowork: the CLI\'s job refused breakaway for its tools (error {ctypes.get_last_error()})',
                  file=sys.stderr)
        if not kernel32.AssignProcessToJobObject(job, int(child._handle)):
            raise ctypes.WinError(ctypes.get_last_error(), 'cowork: the CLI could not join its job object')
        status = ntdll.NtResumeProcess(int(child._handle))
        if status:
            raise OSError(f'cowork: could not resume the CLI (NTSTATUS {status & 0xffffffff:#010x})')
    except BaseException:
        child.kill()
        child.wait()
        if job:
            kernel32.CloseHandle(job)
        raise
    return job


@contextlib.contextmanager
def admission(box):
    """Only one process reads or changes a side's requests at a time."""
    with (box / 'lock').open('w' if fcntl else 'a') as lock:
        if fcntl is None:
            with nt_lock(lock):
                yield
            return
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def held(lock_file, wait=False):
    """True while a runner, its CLI or anything they spawned still holds this
    request's lock exclusively; with `wait`, block until they let go."""
    if fcntl is None:  # they hold it open for writing; polled
        while nt_written(lock_file):
            if not wait:
                return True
            time.sleep(0.2)
        return False
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
    if fcntl is None:  # no /proc: the one created when the runner recorded beside its pid
        with holder(pid, lock_file) as process:
            return process is not None
    try:
        return any(os.readlink(fd) == str(lock_file) for fd in Path(f'/proc/{pid}/fd').iterdir())
    except OSError:
        return False


def lock_record(pid, born=None):
    """What the runner writes into the request lock: the CLI's pid and, on Windows, its creation time (born,
    else the pid's own). File times tick too coarsely there to date a pid against the lock, so the record tells
    it from a reused pid."""
    if fcntl:
        return f'{pid}\n'
    return f'{pid} {nt_born(pid) if born is None else born}\n'


def cli_pid(lock_file):
    """The CLI pid the runner wrote into the request lock, '' before it got there."""
    return (lock_file.read_text().split() or [''])[0]


def requests(box):
    return sorted(p.stem for p in box.glob('*.lock'))


def state(box, request):
    exit_file, lock = box / f'{request}.exit', box / f'{request}.lock'
    if exit_file.exists():
        return exit_file.read_text().strip()
    return 'running' if held(lock) else 'died'


def remove(box, request):
    """Remove a request's files, its lock first: until the lock goes it is a
    request, so a lock that cannot go keeps the rest, readable, and raises.
    Then, on Windows, all else that can go even when one is still open
    elsewhere (then raised); no half of it reads as a request."""
    lock = box / f'{request}.lock'
    retry(lambda: lock.unlink(missing_ok=True))
    stuck = None
    for leftover in box.glob(f'{request}.*'):
        if leftover == lock:  # may stay listed until a prober's handle closes
            continue
        try:
            retry(leftover.unlink)
        except PermissionError as failure:
            if fcntl:
                raise
            stuck = stuck or failure
    if stuck:
        raise stuck


def sweep(box):
    """Remove, as far as they let go, the files remove() and publish() had to
    leave: those of requests whose lock is gone. Under admission; the files
    still held."""
    held = []
    for leftover in box.glob(f'{box.parent.name}-{box.name}-*'):
        if not (box / f'{leftover.name.split(".")[0]}.lock').exists():
            try:
                leftover.unlink()
            except OSError:
                held.append(leftover)
    return held


def retry(step):
    """On Windows, a file another process has open cannot be removed or
    replaced: retry for a second, then raise. Elsewhere, once."""
    for attempt in reversed(range(1 if fcntl else 50)):
        try:
            return step()
        except PermissionError:
            if not attempt:
                raise
            time.sleep(0.02)


def running(box):
    """The request whose runner or CLI is still there, if any: a verdict alone
    does not free the side, a killed turn is still being torn down."""
    return next((r for r in requests(box) if held(box / f'{r}.lock')), None)


def lane_fields(box):
    """The session file as FIELDS; empty strings for what it lacks: the
    session id until the first turn binds one, the tier on a pinned pair."""
    lines = (box / 'session').read_text().split('\n') if (box / 'session').exists() else []
    return dict(zip(FIELDS, lines + [''] * len(FIELDS)))


def replace_text(path, text):
    """Write `path` in one step, so a reader never sees it truncated; a
    failure leaves no temp file behind."""
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        tmp.write_bytes(text.encode('utf-8'))  # untranslated: the same bytes on every OS
        retry(lambda: os.replace(tmp, path))
    finally:
        tmp.unlink(missing_ok=True)


def write_side(box, **fields):
    """Change some of a side's FIELDS, a field-wise merge. The caller holds admission."""
    current = lane_fields(box)
    current.update({k: v or '' for k, v in fields.items()})
    replace_text(box / 'session', ''.join(f'{current[k]}\n' for k in FIELDS))


def preset_file():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'agentrc' / 'cowork-default'


def preset():
    """The Codex default preset: the preference file's, else PRESET; refused
    when the file names none."""
    path = preset_file()
    name = path.read_text().strip() if path.exists() else PRESET
    if name not in PRESETS:
        die(f'{path}: expected one of {", ".join(PRESETS)}, got {name!r}; set it with `cowork.py default <preset>`')
    return name


def tier_pair(tier):
    return PRESETS[preset()] if tier == 'default' else TIERS[tier]


def update_side(box, **fields):
    with admission(box):
        write_side(box, **fields)


def settle_pair(box, side, model, effort, tier):
    """Save the model and effort a request runs on. Under admission with the
    start; the runner reads them back, so a later flip cannot change it. A
    Codex lane runs on the named or saved tier, resolved now, so a flipped
    default reaches it; a new one starts on `default`, and one pinned before
    pins were dropped is refused until a tier is named. A Claude lane keeps
    its --model and --effort, starting at DEFAULTS."""
    saved = lane_fields(box)
    if side == 'claude':
        write_side(box, model=model or saved['model'] or DEFAULTS['claude'][0],
                   effort=effort or saved['effort'] or DEFAULTS['claude'][1], tier='')
        return
    tier = tier or saved['tier'] or ('' if saved['model'] else 'default')
    if not tier:
        die(f'codex/{box.name} is pinned to {saved["model"]} at {saved["effort"]} effort; '
            f'name its tier with --tier {"|".join(("default", *TIERS))}', BUSY)
    model, effort = tier_pair(tier)
    write_side(box, model=model, effort=effort, tier=tier)


def session_of(box):
    return lane_fields(box)['session'] or None


def compose(session, side, request, task, no_edit, root, model, effort, lane, where):
    sender = 'claude' if side == 'codex' else 'codex'
    header = HEADER.format(id=request, me=sender, lane=lane, scope=SCOPE[no_edit], model=model, effort=effort, where=where)
    return ('' if session else BOOTSTRAP.format(side=side, root=root)) + header + task.rstrip('\n') + TRAILER[no_edit]


def command(side, session, reply_file, no_edit, model, effort):
    """The CLI argv and, for a first Claude turn, the id it is told to use;
    that id is bound only once the turn succeeds. Claude enforces `--no-edit`
    in plan mode; Codex's read-only sandbox would also forbid the temp files a
    test suite needs, so its turn is checked afterwards instead (tree_state)."""
    if side == 'codex':
        argv = ['codex', 'exec'] + (['resume', session] if session else [])
        argv += ['-m', model, '-c', f'model_reasoning_effort={effort}']
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


def kill_tree(pid, job=None, process=None):
    """SIGKILL a process and everything under it. Codex runs its shell tool
    in its own session, so the process group is not enough. Freeze each
    process as it is found and rescan until the frozen tree grows no more,
    so nothing spawned mid-walk escapes. A process we may not signal (sudo's
    child) is skipped; its parent still dies. On Windows taskkill follows
    parent links from one snapshot, without freezing, so it misses what is
    spawned meanwhile and what outlived its parent; the runner's job object,
    where there is one (not for `kill`), then takes the rest of the tree at
    once, all but a tool that asked to break away, which taskkill took while
    its parent lived. `kill` passes the CLI's handle instead (holder): it
    keeps the pid from being reused under taskkill, and terminates the CLI
    should taskkill fail."""
    if fcntl is None:
        # System32's, by full path: CreateProcess would try the working directory, the checkout, first
        taskkill = os.path.join(os.environ.get('SystemRoot', ''), 'System32', 'taskkill.exe')
        try:
            if not (PureWindowsPath(taskkill).drive and PureWindowsPath(taskkill).root):
                raise OSError(f'SystemRoot names no absolute directory: {taskkill}')
            subprocess.run([taskkill, '/F', '/T', '/PID', str(pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as failure:
            print(f'cowork: taskkill of {pid} failed: {failure}', file=sys.stderr)
        if job:
            kernel32.TerminateJobObject(job, 1)
        if process:
            kernel32.TerminateProcess(process, 1)
        return
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
    status, child, before, job = 'failed', None, None, None
    root = tree_of(box, host)
    try:
        if exit_file.exists():  # killed before it started
            status = 'killed'
            return
        fields = lane_fields(box)  # settled by start, under the lock this runner holds
        session, model, effort = fields['session'] or None, fields['model'], fields['effort']
        where = ''
        if kind(box) == 'worktree':
            where = WHERE.format(root=root, branch=git(root, 'symbolic-ref', '--short', 'HEAD').stdout.strip(),
                                 base=(box / 'base').read_text().strip()[:12])
        task = box / f'{request}.task'
        prompt = compose(session, side, request, task.read_text(encoding='utf-8'), no_edit, root, model, effort,
                         box.name, where)
        task.unlink()
        argv, new_session = command(side, session, reply, no_edit, model, effort)
        env = {k: v for k, v in os.environ.items()  # the coworker is nobody's driver
               if not k.startswith('HERDR_') and k not in ('CLAUDECODE', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID')}
        env['COWORK_TURN'] = request  # tells the simplify gate to stay out
        before = tree_state(root) if no_edit else None  # unreadable: an error, and no turn
        with tempfile.TemporaryFile() as stdin, stream.open('ab') as out:
            stdin.write(prompt.encode())
            stdin.seek(0)
            if fcntl is None:
                child = nt_popen(nt_cli(argv), msvcrt.get_osfhandle(lock_fd), CREATE_SUSPENDED, cwd=root, stdin=stdin,
                                 stdout=out, env=env)
                job = nt_job(child)  # resumes it
            else:
                child = subprocess.Popen(argv, cwd=root, stdin=stdin, stdout=out, env=env,
                                         start_new_session=True, pass_fds=(lock_fd,))
        if fcntl is None:  # no os.pwrite
            os.lseek(lock_fd, 0, os.SEEK_SET)
            os.write(lock_fd, lock_record(child.pid).encode())
        else:
            os.pwrite(lock_fd, lock_record(child.pid).encode(), 0)  # for `kill`, should this runner die first
        while child.poll() is None and not exit_file.exists():
            time.sleep(0.2)
        status = 'killed' if exit_file.exists() else str(child.returncode)
    except BaseException as failure:  # a missing CLI, anything: leave no orphan
        print(repr(failure), file=sys.stderr)
    finally:
        # On Windows, a killed CLI's job also holds what the taskkill of `kill` may have missed, even when
        # the CLI itself had already exited: POSIX leaves that CLI's grandchildren alone.
        if child is not None and (child.poll() is None or job and exit_file.exists()):
            kill_tree(child.pid, job)
            child.wait()
        if child is not None:
            status = conclude(side, box, status, reply, stream, new_session, before, root)
        sys.stderr.flush()
        publish(exit_file, status + '\n')  # `kill` may have won during conclude()


def conclude(side, box, status, reply, stream, new_session, before, root):
    """The outcome (OUTCOMES) of what the finished CLI left behind: bind a new
    session, validate the reply, check a --no-edit tree (`before`, the
    snapshot taken when the request is --no-edit), surface Codex's in-stream
    error."""
    if side == 'codex':
        if not session_of(box):
            thread = next((e['thread_id'] for e in events(stream) if 'thread_id' in e), None)
            if thread:
                update_side(box, session=thread)  # a thread that exists resumes, even after a failed turn
        if status == '0' and not reply.exists():
            print('codex exited 0 without writing its last message', file=sys.stderr)
            status = 'failed'
    elif status == '0':
        result = next((e for e in events(stream) if e.get('type') == 'result'), None)
        if result is None or result.get('is_error'):
            print(f'claude ended without a usable result: {result}', file=sys.stderr)
            status = 'failed'
        else:
            reply.write_bytes((result.get('result') or '').encode('utf-8'))  # as Codex writes its own, untranslated
            if new_session:
                update_side(box, session=new_session)
    if status == '0' and before is not None:
        try:
            if tree_state(root) != before:
                print('the tree changed during a --no-edit turn; check git status', file=sys.stderr)
                status = 'tree-changed'
        except SnapshotError as failure:
            print(failure, file=sys.stderr)
            status = 'unverified'
    if status == '0':
        return classify(text_of(reply), before is not None)
    if status not in OUTCOMES:
        print(f'{side} exited {status}', file=sys.stderr)
        status = 'failed'
    if side == 'codex' and status == 'failed':
        # Codex reports an in-turn failure only as a JSONL event, with nothing on stderr
        for event in events(stream):
            if event.get('type') in ('error', 'turn.failed'):
                print(event.get('message') or (event.get('error') or {}).get('message') or json.dumps(event),
                      file=sys.stderr)
                break
    return status


def classify(reply, no_edit):
    """A finished turn's outcome from its reply: a writer owes its "Files
    touched" line; a read-only turn, checked by its tree, only when it changed
    something."""
    if not reply.strip():
        return 'empty-reply'
    return 'replied' if no_edit or FOOTER.search(reply) else 'no-footer'


class SnapshotError(Exception):
    """tree_state could not read the tree: a --no-edit turn stays unverified, never passes."""


def rollout_turn(thread, request):
    """The last usage record Codex wrote to its rollout for this request's
    turn, or None unless that turn is found exactly once and completed. The
    request counts only in an envelope header, so neither a later task quoting
    it nor a compaction's copy of history matches again."""
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    files = list(home.glob(f'sessions/*/*/*/rollout-*-{thread}.jsonl'))
    if len(files) != 1:
        return None
    meta, turn, matches, record, complete = None, None, [], None, False
    with files[0].open(encoding='utf-8') as rollout:
        for line in rollout:
            if not any(word in line for word in ('"session_meta"', '"task_started"', '"task_complete"',
                                                 '"token_usage_record"', request)):
                continue  # most of a long rollout is tool output
            event = json.loads(line)
            what, payload = event.get('type'), event.get('payload') or {}
            mine = matches and payload.get('turn_id') == matches[0]
            if what == 'session_meta':
                meta = payload.get('id')
            elif what == 'event_msg' and payload.get('type') == 'task_started':
                turn = payload.get('turn_id')
            elif what == 'event_msg' and payload.get('type') == 'task_complete':
                complete = complete or mine
            elif what == 'token_usage_record' and mine and payload.get('thread_id') == thread:
                record = payload
            elif what == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'user':
                text = ''.join(part.get('text', '') for part in payload.get('content') or [] if isinstance(part, dict))
                head, delimiter, _ = text.partition('\n---\n')
                if delimiter and any(line.startswith(f'cowork request {request} from ') for line in head.splitlines()):
                    tagged = (payload.get('internal_chat_message_metadata_passthrough') or {}).get('turn_id', turn)
                    matches.append(turn if tagged == turn else None)
    if meta != thread or len(matches) != 1 or matches[0] is None or not complete:
        return None
    return record


def usage(box, request):
    """The turn's token usage as one line; raises unless every counter is a
    count. Codex: the turn's own figures from its rollout, else the running
    totals for the session its stream reports. Claude: the request and, per
    call, the context it sent."""
    def num(u, key, default=None):
        value = u.get(key, default)
        if type(value) is not int or value < 0:  # a bool is an int to isinstance
            raise ValueError(f'{key}: {value!r}')
        return value
    def spent(prefix, u, cached='cached_input_tokens'):
        return f'{prefix}{num(u, "input_tokens"):,} (cached {num(u, cached):,}), output {num(u, "output_tokens"):,}'
    found = [e for e in events(box / f'{request}.jsonl') if e.get('type') in ('turn.completed', 'result', 'assistant')]
    if box.parent.name == 'codex':
        thread = session_of(box)  # bound by the runner; reset would have removed the request with it
        try:
            record = thread and rollout_turn(thread, request)
            if record:
                return (spent('input ', record['turn_token_usage'])
                        + f'; input context {num(record["usage"], "input_tokens"):,} (last call)')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # a rollout written by another Codex version, or cut short: the stream's totals still hold
        return spent('session input ', next(e for e in found if e['type'] == 'turn.completed')['usage'])
    def sent(u):
        return num(u, 'input_tokens') + num(u, 'cache_read_input_tokens', 0) + num(u, 'cache_creation_input_tokens', 0)
    used = next(e for e in found if e['type'] == 'result')['usage']
    last = next((e['message']['usage'] for e in found
                 if e['type'] == 'assistant' and isinstance(e.get('message'), dict) and 'usage' in e['message']), None)
    return (f'input {sent(used):,} (cached {num(used, "cache_read_input_tokens", 0):,}), output {num(used, "output_tokens"):,}'
            + (f'; context {sent(last):,} (last call)' if last else ''))


def tree_state(root):
    """HEAD, the index entries with their stages, and the worktree as git
    would stage it: what a coworker told not to edit must leave all three
    alone. `ls-files -s` rather than `write-tree`, which refuses conflicts.
    An unborn or detached HEAD and a missing index are states; any other
    failure raises."""
    def git(*args, ok=(0,), **env):
        done = subprocess.run([git_program(), *args], cwd=root, capture_output=True, text=True, encoding=GIT_ENCODING,
                              errors='replace' if GIT_ENCODING else None, env={**os.environ, **env})
        if done.returncode not in ok:
            raise SnapshotError(f'could not verify the tree: git {" ".join(args)}: '
                                f'{done.stderr.strip() or f"exit {done.returncode}"}')
        return done.stdout
    with tempfile.TemporaryDirectory() as scratch:
        index = os.path.join(scratch, 'index')  # a copy of the real one: tracked and staged files are never ignored
        try:
            shutil.copy(os.path.join(root, git('rev-parse', '--git-path', 'index').strip()), index)
        except FileNotFoundError:
            pass  # nothing staged yet
        except OSError as failure:
            raise SnapshotError(f'could not verify the tree: copying the index: {failure}') from failure
        git('add', '-A', GIT_INDEX_FILE=index)
        git('add', '-A', '--renormalize', GIT_INDEX_FILE=index)  # rehash every tracked file: equal size and mtime prove nothing
        worktree = git('ls-files', '-s', GIT_INDEX_FILE=index)
    # the branch HEAD names (none when detached) and its commit (none when unborn); a broken ref fails the first
    head = git('symbolic-ref', '-q', 'HEAD', ok=(0, 1)) + git('rev-parse', '-q', '--verify', 'HEAD', ok=(0, 1))
    return head + git('ls-files', '-s') + worktree


def events(stream):
    """The stream's JSON events, newest first."""
    for line in reversed(text_of(stream).splitlines()):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def verdict(box, request):
    """(outcome, what to say about it) of a request whose runner has let go:
    a diagnostic, or for a reply the paths its footer reports. A runner killed
    outright left no verdict: record one, so the request is delivered like
    any other."""
    exit_file, reply = box / f'{request}.exit', box / f'{request}.reply'
    publish(exit_file, 'died\n')
    outcome = exit_file.read_text().strip()
    text = text_of(reply) if reply.exists() else ''  # absent when the turn failed, or never ran
    if outcome not in OUTCOMES:  # a runner on older code: its writer rule, which owed every reply a footer
        legacy = {'0': classify(text, False), 'edited': 'tree-changed', 'error': 'failed'}.get(outcome)
        if legacy is None:
            return 'failed', f'exited {outcome}'
        outcome = legacy
    if outcome == 'replied':
        footers = FOOTER.findall(text)
        paths = footers[-1].strip() if footers else ''
        return outcome, '' if paths in ('', 'none') else f'paths reported: {paths}'
    if outcome == 'unverified':
        return outcome, err_tail(box, request).strip() or 'could not verify the tree'
    return outcome, {'no-footer': 'the reply has no "Files touched" line; re-read the tree yourself',
                     'empty-reply': 'the reply is empty',
                     'tree-changed': 'the tree changed during a --no-edit turn; check git status',
                     'killed': 'was killed', 'died': 'died without recording a verdict'}.get(outcome, 'failed')


def err_tail(box, request):
    """The end of the runner's stderr; absent if send died before starting the runner."""
    err = box / f'{request}.err'
    return text_of(err)[-2000:] if err.exists() else ''


def deliver(box, request, out=None):
    """Print a settled request's reply, or its failure, on stdout, then its
    diagnostics and one receipt line on stderr, then remove its files: the
    coworker's session store keeps the turn. With `out`, write all of that to
    the file instead and print only a short receipt naming it; a file that
    cannot be written is refused and keeps the request for a reader with a
    writable one, since a long reply printed instead may be cut. Under admission, so two readers
    cannot both claim it, and only once both streams have taken the text."""
    with admission(box):
        lock = box / f'{request}.lock'
        if not lock.exists():
            die(f'no request {request} in this worktree: delivered already, or never sent', BUSY)
        if held(lock):
            die(f'{request} is {state(box, request)}; read it once it settles', BUSY)
        outcome, what = verdict(box, request)
        code = OUTCOMES[outcome]
        if code == FAILED:
            text = f'cowork request {request} {what}\n{err_tail(box, request)}'
        else:
            reply = box / f'{request}.reply'  # absent when an unverifiable turn never ran
            text = text_of(reply) if reply.exists() else ''
        if text and not text.endswith('\n'):
            text += '\n'
        notes = f'cowork request {request}: {what}\n' if code == MALFORMED else ''
        if out is None:
            print(text, end='', flush=True)  # before usage, so a slow rollout never holds it back
        try:
            used = usage(box, request)
        except Exception:  # a report, never a reason to lose the reply
            used = 'usage unavailable'
        result = f'cowork result {request} {box.parent.name}/{box.name}: {outcome}, exit {code}'
        advisory = f'; {what}' if code == 0 and what else ''
        receipt = f'{result}{advisory}; {used}\n'
        if out is not None:
            try:
                replace_text(out, text + notes + receipt)
                notes, receipt = '', f'{result}; written to {out}\n'
            except (OSError, ValueError) as failure:
                die(f'cowork request {request}: could not write {out} ({failure}); the request is kept: '
                    f'read it again with a writable --out', BUSY)
        print(notes + receipt, end='', file=sys.stderr, flush=True)
        try:
            remove(box, request)
        except PermissionError as failure:  # on Windows, a file still open: delivered all the same
            if fcntl:
                raise
            kept = ('the request is kept whole: read it again once it lets go' if failure.filename == str(lock)
                    else 'the next send to the lane, or its reset, removes what is left')
            # the receipt again, so it stays the last line, as callers read it
            print(f'cowork request {request}: delivered, but {failure.filename} is still open in another process; '
                  f'{kept}\n{receipt}', end='', file=sys.stderr, flush=True)
    return code


def find(gitdir, request):
    box = next((box for box in boxes(gitdir) if (box / f'{request}.lock').exists()), None)
    if box is None:
        die(f'no request {request} in this worktree: delivered already, or never sent', BUSY)
    return box


def start(box, root, side, task, no_edit, read_only, worktree, model, effort, tier):
    """Record the request and start its runner, which holds the request lock
    from birth; the caller keeps nothing open. Refused while one is in flight,
    and for a lane of another kind than the flags assert, or a dirty worktree.
    A new named lane is read-only unless --worktree makes it a worktree lane."""
    lane = box.name
    with admission(box):
        busy = running(box)
        if busy:
            die(f'{side}/{lane} is busy with {busy}; wait for it, or kill it', BUSY)
        sweep(box)
        if lane == 'main':
            if read_only or worktree:
                die('main is this checkout and writable; --read-only and --worktree are for named lanes', BUSY)
        else:
            lane_kind = kind(box)
            if lane_kind is None:
                if (box / 'session').exists():
                    die(f'{side}/{lane} has a session but no record of its kind; reset it', BUSY)
                lane_kind = 'worktree' if worktree else 'read-only'
                if lane_kind == 'read-only':
                    (box / 'read-only').touch()  # a worktree lane's base is written once its tree exists
            if (read_only or worktree) and lane_kind != ('read-only' if read_only else 'worktree'):
                die(f'{side}/{lane} is a {lane_kind} lane; it keeps its kind until reset', BUSY)
            if lane_kind == 'read-only':
                no_edit = True
            else:
                sync_lane(root, box)
        settle_pair(box, side, model, effort, tier)
        request = f'{side}-{lane}-{datetime.datetime.now():%Y%m%d-%H%M%S-%f}'
        (box / f'{request}.task').write_text(task, encoding='utf-8')
        (box / f'{request}.jsonl').touch()
        with (box / f'{request}.lock').open('w') as lock, (box / f'{request}.err').open('ab') as err:
            if fcntl is None:
                # The open handle is the lock. A middle process starts the runner and exits, so once this
                # returns the runner is no descendant of `send` that a harness's tree kill could reach.
                handle = msvcrt.get_osfhandle(lock.fileno())
                nt_detach([sys.executable, __file__, '_detach', side, lane, request, str(int(no_edit)), str(handle)],
                          root, handle, err).wait()
                return request
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
        if kind(box) in ('worktree', None):  # a kindless lane may still have a tree
            drop_lane(root, box)
        gone = requests(box)
        try:
            for request in gone:
                remove(box, request)
        except PermissionError as failure:  # on Windows, a file still open elsewhere
            die(f'{side}/{lane}: could not remove {failure.filename} ({failure.strerror}); '
                f'reset again once nothing holds it', BUSY)
        if held := sweep(box):
            die(f'{side}/{lane}: could not remove {held[0]}; reset again once nothing holds it', BUSY)
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
    send = sub.add_parser('send', help='one turn of the coworker; prints the request id, then the reply unless --detach')
    send.add_argument('--to', choices=SIDES, help='which coworker; defaults to codex when run from Claude Code')
    send.add_argument('--lane', default='main', help='which session of that coworker (default: main, this checkout); '
                                                    'any other lane is read-only in this checkout unless created --worktree')
    kinds = send.add_mutually_exclusive_group()
    kinds.add_argument('--read-only', action='store_true',
                       help='assert a read-only lane: this checkout, every send --no-edit; what a new named lane is anyway')
    kinds.add_argument('--worktree', action='store_true',
                       help='on a new lane\'s first send: its own worktree and branch, created at HEAD')
    send.add_argument('--task', required=True, help='literal text, or - for stdin')
    send.add_argument('--no-edit', action='store_true', help='a question or review: the coworker must not edit')
    send.add_argument('--tier', choices=('default', *TIERS), help='a Codex lane\'s tier from now on; a new lane starts on default')
    send.add_argument('--model', help=f'a Claude lane\'s model from now on, default {DEFAULTS["claude"][0]}')
    send.add_argument('--effort', choices=EFFORTS, help='a Claude lane\'s effort from now on')
    send.add_argument('--detach', action='store_true', help='print the request id and return; collect the reply with read --wait')
    sub.add_parser('kill', help='stop a running request and everything its coworker spawned').add_argument('request')
    read = sub.add_parser('read', help='print the reply of a request whose send detached or died, and remove it')
    read.add_argument('request')
    read.add_argument('--wait', action='store_true', help='wait until the runner and its descendants release the request')
    read.add_argument('--out', type=Path, metavar='FILE',
                      help='write the reply, diagnostics and receipt to FILE in one step; print only a short receipt naming it')
    status = sub.add_parser('status', help='busy lanes and undelivered requests in this worktree, and a count of idle lanes')
    status.add_argument('--all', action='store_true', help='every lane, idle ones included')
    default = sub.add_parser('default', help='show, or set, the Codex default tier\'s preset for this user on this host')
    default.add_argument('preset', nargs='?', choices=tuple(PRESETS))
    reset_ = sub.add_parser('reset', help='forget a lane\'s session and its requests, remove its worktree once merged; '
                                          'the next send starts anew')
    reset_.add_argument('side', choices=SIDES)
    reset_.add_argument('lane', help='a lane name, or all')
    # the detached child of `send`; on Windows, also the middle process that starts it
    for cmd in ('_run',) + (() if fcntl else ('_detach',)):
        runner = sub.add_parser(cmd, help=argparse.SUPPRESS)
        for name in ('side', 'lane', 'request', 'no_edit', 'lock_fd'):
            runner.add_argument(name)
    a = parser.parse_args(argv)
    if sys.platform in ('cygwin', 'msys'):  # its POSIX locks and signals would not reach the native CLIs
        die('on Windows, run it with a native Windows CPython (python.org or MinGW), not a Cygwin or MSYS one')

    if a.cmd == '_detach':  # the lock "fd" is a handle on Windows
        nt_detach([sys.executable, __file__, '_run', a.side, a.lane, a.request, a.no_edit, a.lock_fd], os.getcwd(),
                  int(a.lock_fd), sys.stderr)
        return 0

    if a.cmd == 'default':
        path = preset_file()
        if a.preset:
            path.parent.mkdir(parents=True, exist_ok=True)
            replace_text(path, a.preset + '\n')
        name = preset()
        model, effort = PRESETS[name]
        print(f'codex default: {name}, {model} at {effort} effort ({path if path.exists() else "built in"})')
        return 0

    root, gitdir = git_dir()
    if a.cmd == '_run':
        lock_fd = int(a.lock_fd) if fcntl else msvcrt.open_osfhandle(int(a.lock_fd), os.O_RDWR)
        run(a.side, box_of(gitdir, a.side, a.lane), root, a.request, a.no_edit == '1', lock_fd)
        return 0

    if a.cmd == 'send':
        side = coworker(a.to)
        if a.tier and side == 'claude':
            parser.error('--tier is for Codex lanes; a Claude lane takes --model/--effort')
        if (a.model or a.effort) and side == 'codex':
            parser.error('--model/--effort are for Claude lanes; a Codex lane takes --tier')
        if side == 'codex':
            preset()  # a bad preference file is refused before the lane is touched
        if not LANE.match(a.lane):
            die(f'lane names are [a-z0-9-], up to 40, and not "all": {a.lane!r}')
        box = box_of(gitdir, side, a.lane)
        box.mkdir(parents=True, exist_ok=True)
        try:
            task = sys.stdin.read() if a.task == '-' else a.task
        except UnicodeDecodeError:
            if fcntl:
                raise
            die('--task - must be UTF-8')  # stdin is read as UTF-8 there, not in the ANSI code page
        if not task.strip():
            die('the task resolved to nothing')
        request = start(box, root, side, task, a.no_edit, a.read_only, a.worktree, a.model, a.effort, a.tier)
        print(request, flush=True)
        if a.detach:
            return 0
        held(box / f'{request}.lock', wait=True)  # wait through teardown, even if the verdict exists
        return deliver(box, request)

    if a.cmd == 'read':
        box = find(gitdir, a.request)
        if a.wait:
            held(box / f'{a.request}.lock', wait=True)
        return deliver(box, a.request, a.out and a.out.absolute())

    if a.cmd == 'kill':
        box = find(gitdir, a.request)
        exit_file, lock = box / f'{a.request}.exit', box / f'{a.request}.lock'
        with admission(box):  # not after a delivery removed the request, which would leave an orphan verdict
            if not lock.exists():
                die(f'no request {a.request} in this worktree: delivered already', BUSY)
            pid = cli_pid(lock)
            if publish(exit_file, 'killed\n') and pid:  # our CLI, orphaned or not; a runner still there sees it too
                with holder(int(pid), lock) as process:
                    if process:
                        kill_tree(int(pid), process=process)
            print(f'{a.request}: {exit_file.read_text().strip()}')
        return 0

    if a.cmd == 'status':
        idle = 0
        for side in SIDES:
            shown = 0
            for lane in lanes(gitdir, side):
                box = box_of(gitdir, side, lane)
                with admission(box):  # a delivery in progress would remove files under state()
                    pending = requests(box)
                    if not (box / 'session').exists() and not pending:
                        continue  # reset, and nothing since
                    if not (pending or a.all):
                        idle += 1
                        continue
                    shown += 1
                    fields = lane_fields(box)
                    model, tier = fields['model'], fields['tier']
                    where = {'main': '', 'read-only': ', read-only', 'worktree': f', in {lane_root(root, box)}',
                             None: ', kind unknown'}[kind(box)]
                    pair = f', {model} at {fields["effort"]} effort' if model else ''
                    if side == 'codex' and model:
                        pair += f', tier {tier}' if tier else ', pinned'
                    print(f'{side}/{lane}: session {fields["session"] or "none"}{pair}{where}')
                    for request in pending:
                        print(f'  {request}  {state(box, request)}')
            if not shown and a.all:
                print(f'{side}: no lane')
        if not a.all:
            print(f'{idle} idle lane{"" if idle == 1 else "s"}' + (', listed by status --all' if idle else ''))
        return 0

    if a.cmd == 'reset':
        if a.lane != 'all' and not LANE.match(a.lane):
            die(f'lane names are [a-z0-9-], up to 40: {a.lane!r}')
        for lane in lanes(gitdir, a.side) if a.lane == 'all' else [a.lane]:
            reset(root, gitdir, a.side, lane)
        return 0


if __name__ == '__main__':
    if fcntl is None:  # piped stdio would use the ANSI code page; tasks, replies and diagnostics are UTF-8
        for stream, errors in ((sys.stdin, 'strict'), (sys.stdout, 'strict'), (sys.stderr, 'backslashreplace')):
            if stream:
                stream.reconfigure(encoding='utf-8', errors=errors)
    sys.exit(main())
