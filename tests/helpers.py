"""Helpers the tests share: programs stubbed in Python that start by name on every OS, process liveness, a gh
stub, temporary directories named as git and realpath name them, and the guard against processes that outlive
their test."""
import contextlib
import ctypes
import functools
import os
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
        st = proc_stat(pid)    # a zombie has exited: an orphan left to the survivor guard's subreaper stays one
        return st is None or st[1] != 'Z'
    import ctypes   # os.kill(pid, 0) would terminate it on Windows
    kernel32 = ctypes.WinDLL('kernel32')
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


def tmpdir(case):
    """A temporary directory removed after the test, resolved as git, getcwd and realpath name it: on Windows the
    long name, not the 8.3 one a runner's TEMP may hold (RUNNER~1)."""
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    return Path(directory.name).resolve()


PR_SET_CHILD_SUBREAPER, PR_GET_CHILD_SUBREAPER = 36, 37
SURVIVOR_GRACE = 2      # seconds a process the test's cleanup already ended gets to exit before the guard kills it


def proc_stat(pid):
    """(ppid, state, start time) of pid, None once it is gone."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
    except (OSError, IndexError):
        return None
    return int(fields[1]), fields[0], fields[19]


def children(pid):
    """pid's children, from every thread's list (an orphan is reparented to one thread of its subreaper)."""
    kids = []
    with contextlib.suppress(OSError):
        for tid in os.listdir(f'/proc/{pid}/task'):
            with contextlib.suppress(OSError), open(f'/proc/{pid}/task/{tid}/children') as f:
                kids += map(int, f.read().split())
    return kids


def descendants():
    """{pid: (ppid, state, start time)} of every descendant of this process."""
    found, frontier = {}, [os.getpid()]
    while frontier:
        for kid in children(frontier.pop()):
            if kid not in found and (st := proc_stat(kid)):
                found[kid] = st
                frontier.append(kid)
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
    a double-forked one."""
    return prctl(PR_SET_CHILD_SUBREAPER, int(flag)) == 0


def reap_if_ours(pid, start):
    """Reap pid if it is still the process that started at start and is now a zombie of this process."""
    st = proc_stat(pid)
    if st and st[2] == start and st[1] == 'Z' and st[0] == os.getpid():
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)


def end(pid, start):
    """SIGKILL pid if it is still the process that started at start, and wait until it is gone or a zombie,
    reaped if it is this process's."""
    if (st := proc_stat(pid)) is None or st[2] != start:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while (st := proc_stat(pid)) and st[2] == start and st[1] != 'Z' and time.monotonic() < deadline:
        time.sleep(0.02)
    reap_if_ours(pid, start)


def check_no_survivors(baseline):
    """Give every descendant of this process missing from baseline ((pid, start time) pairs) SURVIVOR_GRACE to
    exit, then kill it; reap the zombies left to this process, and fail naming the ones that still ran."""
    named, deadline = {}, time.monotonic() + SURVIVOR_GRACE
    for _ in range(1000):
        table = descendants()
        left = [p for p, (ppid, state, start) in table.items() if (p, start) not in baseline
                and (state != 'Z' or ppid == os.getpid())]
        for pid in left:
            if table[pid][1] == 'Z':
                reap_if_ours(pid, table[pid][2])
        running = [p for p in left if table[p][1] != 'Z']
        if not running:
            break
        if time.monotonic() < deadline:
            time.sleep(0.05)
            continue
        for pid in running:
            named.setdefault(pid, cmdline(pid))
            end(pid, table[pid][2])
    if named:
        raise AssertionError('killed processes that outlived their test: '
                             + '; '.join(f'{pid} {cmd}' for pid, cmd in named.items()))


def survivor_guard_off():
    """Why the survivor guard cannot run here, None when it can."""
    if not os.path.isdir('/proc/self'):
        return 'no /proc to name descendants by pid and start time'
    if not os.path.exists(f'/proc/self/task/{os.getpid()}/children'):
        return 'no /proc/<pid>/task/<tid>/children (CONFIG_PROC_CHILDREN) to find descendants by'
    if get_subreaper() is None:
        return 'no PR_SET_CHILD_SUBREAPER: a double-forked survivor would escape to init'
    return None


SURVIVOR_GUARD_OFF = survivor_guard_off()


def start_survivor_guard(case):
    """Make every descendant that appears during case its process's, through the child-subreaper flag, and check
    for survivors after the case's other cleanups: registered first, it runs last."""
    baseline = {(pid, start) for pid, (_, _, start) in descendants().items()}
    original = get_subreaper()
    set_subreaper(True)

    def check():
        try:
            check_no_survivors(baseline)
        finally:
            set_subreaper(original)
            case._survivor_guard = False
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
