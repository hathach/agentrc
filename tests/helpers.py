"""Helpers the tests share: programs stubbed in Python that start by name on every OS, process liveness, a gh
stub, temporary directories named as git and realpath name them, and the guard against processes that outlive
their test."""
import contextlib
import ctypes
import functools
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def make_stub(bin_dir, name, source):
    """An executable `name` in bin_dir running the Python `source`, its path: a shebang script, or on Windows,
    which cannot start one, a `name`.exe launcher, so no real `name` further down PATH ever runs instead."""
    bin_dir = Path(bin_dir)
    if os.name != 'nt':
        stub = bin_dir / name
        stub.write_text(f'#!{sys.executable}\n{source}', encoding='utf-8')
        stub.chmod(0o755)
        return stub
    try:
        from pip._vendor.distlib.scripts import ScriptMaker
    except ImportError:
        # a failure, not a skip: a runner without pip would otherwise pass while running none of these tests
        raise AssertionError("Windows test stubs are launchers built by pip's vendored distlib: install pip") from None
    with tempfile.TemporaryDirectory() as source_dir:   # the launcher embeds the script
        (Path(source_dir) / name).write_text(f'#!python\n{source}', encoding='utf-8')
        maker = ScriptMaker(source_dir, str(bin_dir))
        maker.executable = sys.executable
        maker.make(name)
    stub = bin_dir / f'{name}.exe'
    if not stub.is_file():
        raise AssertionError(f'no {stub} stub')
    return stub


def pid_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return not exited(pid)    # an orphan left to the survivor guard's subreaper stays a zombie
    kernel32 = ctypes.WinDLL('kernel32')    # os.kill(pid, 0) would terminate it on Windows
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102   # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def run_gh_as(stub):
    """Patch subprocess.run so `gh ...` runs stub under this Python, never a gh found on PATH; anything but git fails."""
    real = subprocess.run

    def run(argv, *a, **kw):
        if argv[0] == 'gh':
            argv = [sys.executable, str(stub), *argv[1:]]
        elif argv[0] not in ('git', sys.executable):
            raise AssertionError(f'{argv[0]} must not run in this test: only git, and gh as the stub')
        return real(argv, *a, **kw)
    return mock.patch.object(subprocess, 'run', run)


def git_env(**extra):
    """os.environ without git's GIT_* variables, with a fixture identity and no global or system config: a test's git
    never signs (each signed commit leaves a key file in TMPDIR) or reads the developer's settings."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', **extra}


def tmpdir(case):
    """A temporary directory removed after the test, resolved as git, getcwd and realpath name it: on Windows the
    long name, not the 8.3 one a runner's TEMP may hold (RUNNER~1)."""
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    return Path(directory.name).resolve()


PR_SET_CHILD_SUBREAPER, PR_GET_CHILD_SUBREAPER = 36, 37
SURVIVOR_GRACE = 2      # seconds a process the test's cleanup already ended gets to exit before the guard kills it


def proc_stat(pid):
    """(ppid, state, start time) of pid, or of the thread 'pid/task/tid', None once it is gone."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
    except (OSError, IndexError):
        return None
    return int(fields[1]), fields[0], fields[19]


def exited(pid):
    """Whether pid is a zombie whose threads have all exited too (a zombie leader's may still run); False where
    /proc cannot tell."""
    if (st := proc_stat(pid)) is None or st[1] != 'Z':
        return False
    with contextlib.suppress(OSError):
        for tid in os.listdir(f'/proc/{pid}/task'):
            if (task := proc_stat(f'{pid}/task/{tid}')) and task[1] not in 'ZX':
                return False
    return True


def children(pid):
    """pid's children, from every thread's list (an orphan is reparented to one thread of its subreaper)."""
    kids = []
    with contextlib.suppress(OSError):
        for tid in os.listdir(f'/proc/{pid}/task'):
            with contextlib.suppress(OSError), open(f'/proc/{pid}/task/{tid}/children') as f:
                kids += map(int, f.read().split())
    return kids


def descendants():
    """{pid: (ppid, state, start time)} of every descendant of this process, each still the child of the process
    whose list named it when read, not a later process reusing its pid or its parent's."""
    found, frontier = {}, [(os.getpid(), None)]
    while frontier:
        parent, start = frontier.pop()
        kids = {kid: st for kid in children(parent)
                if kid not in found and (st := proc_stat(kid)) and st[0] == parent}
        if start is not None and ((st := proc_stat(parent)) is None or st[2] != start):
            continue    # the parent's pid now names another process; a reparented orphan shows in the next walk
        found.update(kids)
        frontier += [(kid, st[2]) for kid, st in kids.items()]
    return found


def cmdline(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return ' '.join(x.decode(errors='replace') for x in f.read().split(b'\0') if x)
    except OSError:
        return ''


def prctl(option, arg):
    """prctl's result, None without it (not Linux)."""
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(option, arg, 0, 0, 0)
    except (OSError, AttributeError, TypeError):
        return None


def get_subreaper():
    flag = ctypes.c_int()
    return flag.value if prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag)) == 0 else None


def set_subreaper(flag):
    """Set whether orphaned descendants are reparented to this process rather than init, so the guard still sees
    a double-forked one; whether the flag now reads back as set."""
    return prctl(PR_SET_CHILD_SUBREAPER, int(flag)) == 0 and get_subreaper() == int(flag)


@contextlib.contextmanager
def pinned(pid, start):
    """A pidfd for pid while it is still the process that started at start, else None: unlike the pid, the pidfd
    never names a later process that reuses it."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        yield None
        return
    try:
        yield fd if (st := proc_stat(pid)) and st[2] == start else None
    finally:
        os.close(fd)


def reap(fd):
    with contextlib.suppress(ChildProcessError):    # not this process's child
        os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)


def reap_if_ours(pid, start):
    """Reap pid if it is still the process that started at start, has exited and is a child of this process."""
    with pinned(pid, start) as fd:
        if fd is not None:
            reap(fd)


def end(pid, start):
    """SIGKILL pid if it is still the process that started at start, wait until it has exited, and reap it if it
    is this process's."""
    with pinned(pid, start) as fd:
        if fd is None:
            return
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        select.select([fd], [], [], 10)    # readable once every thread has exited
        reap(fd)


def check_no_survivors(baseline):
    """Give every descendant of this process missing from baseline ((pid, start time) pairs) SURVIVOR_GRACE to
    exit, then kill it; reap the zombies left to this process, and fail naming the ones that still ran. Done only
    once two walks in a row find the same: a process reparented while one walk ran shows in the next."""
    named, deadline, settled = {}, time.monotonic() + SURVIVOR_GRACE, None
    for _ in range(1000):
        new = {p: st for p, st in descendants().items() if (p, st[2]) not in baseline}
        running = {p: start for p, (_, _, start) in new.items() if not exited(p)}
        for pid, (ppid, _, start) in new.items():
            if pid not in running and ppid == os.getpid():
                reap_if_ours(pid, start)
        if not running:
            if settled == new:
                break
            settled = new
            continue
        settled = None
        if time.monotonic() < deadline:
            time.sleep(0.05)
            continue
        for pid, start in running.items():
            named.setdefault(pid, cmdline(pid))
            end(pid, start)
    if named:
        raise AssertionError('killed processes that outlived their test: '
                             + '; '.join(f'{pid} {cmd}' for pid, cmd in named.items()))


def survivor_guard_off():
    """Why the survivor guard cannot run here, None when it can."""
    if not os.path.isdir('/proc/self'):
        return 'no /proc to name descendants by pid and start time'
    if not os.path.exists(f'/proc/self/task/{os.getpid()}/children'):
        return 'no /proc/<pid>/task/<tid>/children (CONFIG_PROC_CHILDREN) to find descendants by'
    if (flag := get_subreaper()) is None or not set_subreaper(flag):
        return 'no PR_SET_CHILD_SUBREAPER: a double-forked survivor would escape to init'
    if not all(hasattr(m, name) for m, name in ((os, 'pidfd_open'), (os, 'P_PIDFD'), (signal, 'pidfd_send_signal'))):
        return 'no pidfd_open, waitid(P_PIDFD) and pidfd_send_signal to hold a process across pid reuse'
    try:
        os.close(os.pidfd_open(os.getpid()))
    except OSError as e:
        return f'pidfd_open: {e.strerror}: no pidfd to hold a process across pid reuse'
    return None


SURVIVOR_GUARD_OFF = survivor_guard_off()


def start_survivor_guard(case):
    """Make every descendant that appears during case its process's, through the child-subreaper flag, and check
    for survivors after the case's other cleanups: registered first, it runs last."""
    baseline = {(pid, start) for pid, (_, _, start) in descendants().items()}
    original = get_subreaper()
    if not set_subreaper(True):
        raise AssertionError('the survivor guard could not set the child-subreaper flag')

    def check():
        try:
            check_no_survivors(baseline)
        finally:
            case._survivor_guard = False
            if not set_subreaper(original):
                raise AssertionError(f'the survivor guard could not restore the child-subreaper flag to {original}')
    case.addCleanup(check)


def guard_survivors(module):
    """Run every test of the module named `module` under the survivor guard (its setUpModule calls this); off where
    SURVIVOR_GUARD_OFF says why, which the guard's own tests report as their skip reason."""
    if SURVIVOR_GUARD_OFF:
        return
    for cls in list(vars(sys.modules[module]).values()):
        if (isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == module
                and not getattr(cls.setUp, 'survivor_guarded', False)):
            cls.setUp = guarded(cls.setUp)


def guarded(setup):
    @functools.wraps(setup)
    def setUp(case, *args, **kwargs):
        if not getattr(case, '_survivor_guard', False):    # a subclass's setUp calling its guarded base's
            case._survivor_guard = True
            start_survivor_guard(case)
        return setup(case, *args, **kwargs)
    setUp.survivor_guarded = True
    return setUp
