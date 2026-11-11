"""Helpers the tests share: programs stubbed in Python that start by name on every OS, process liveness, a gh
stub, temporary directories named as git and realpath name them, and the guard against processes that outlive
their test."""
import contextlib
import ctypes
import functools
import io
import json
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

# git exports these to a hook (GIT_INDEX_FILE relative to the committing checkout), and a caller may export them too;
# a test's git, or the code under test, in its own temporary repository must not inherit them, or it writes into the
# caller's repository. Not every GIT_*: GIT_CONFIG_* carries README's fsmonitor workaround. Here, not only in
# conftest.py, which unittest never loads: every test module that runs git imports this one.
GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_PREFIX', 'GIT_NAMESPACE')
for _name in GIT_LOCATION:
    os.environ.pop(_name, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import windows  # noqa: E402


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
    return windows.alive(pid)


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


def remotes_checkout(case, *remotes):
    """A temporary repository with these (name, url) remotes, in order, and nothing else configured."""
    path = tmpdir(case)
    subprocess.run(['git', 'init', '-q', str(path)], check=True, env=git_env())
    for name, url in remotes:
        subprocess.run(['git', '-C', str(path), 'remote', 'add', name, url], check=True, env=git_env())
    return path


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
        with open(f'/proc/{pid}/stat', errors='replace') as f:
            fields = f.read().rsplit(')', 1)[1].split()
    except (OSError, IndexError):
        return None
    return int(fields[1]), fields[0], fields[19]


CUT_NAME = 'a测试测试测试'     # prctl(PR_SET_NAME) keeps 15 bytes, so its comm ends mid-character: not UTF-8 (#380)


def cut_comm_stat(pid, ppid=1, state='S', pgrp=None, start=4243):
    """/proc/<pid>/stat as the kernel writes it for a process named CUT_NAME; starttime is field 22."""
    return (f'{pid} ('.encode() + CUT_NAME.encode()[:15]
            + f') {state} {ppid} {pgrp or pid} {" ".join(["0"] * 16)} {start} 0 0\n'.encode())


def proc_open(files):
    """An open() serving files ({path: bytes}) as /proc does, UTF-8 in text mode (strict unless errors= says
    otherwise); other paths open as usual."""
    real = open

    def fake(path, mode='r', *a, **k):
        if str(path) not in files:
            return real(path, mode, *a, **k)
        raw = io.BytesIO(files[str(path)])
        return raw if 'b' in mode else io.TextIOWrapper(raw, encoding='utf-8', errors=k.get('errors'))
    return fake


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


def load_arduino_engine(rel):
    """An agentrc Arduino engine as a library's agentrc_pin loads it: through hil_load's bootstrap, registered as
    agentrc_<stem>."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', Path(__file__).resolve().parents[1] / 'skills' /
                                                  'hil' / 'scripts' / 'hil_load.py')
    if spec.name not in sys.modules:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, spec.origin).load_file(rel)


def load_arduino_fixture():
    """A fresh copy of the fake Arduino library profile (tests/fixtures/arduino_lib/fake_profile.py)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('arduino_fake_profile', Path(__file__).resolve().parent / 'fixtures' /
                                                  'arduino_lib' / 'fake_profile.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def latest_failure(lock_module, board):
    """The latest open failed cleanup of `board` in hil_lock's cleanup log, else None."""
    failures = lock_module.open_cleanup_failures(board)
    return failures[-1] if failures else None


def failed_events(lock_module, board):
    """Every `failed` event in `board`'s cleanup log, oldest first."""
    return [e for e in lock_module.cleanup_events(board) if e['event'] == 'failed']


def taken_failure(fh):
    """The latest open failed cleanup a take found (its handle's cleanup_failures), else None."""
    return fh.cleanup_failures[-1] if fh.cleanup_failures else None


# An invocation that claims `board` in hil_lock's PendingSessions record of `kind` and starts one run_tracked `cmd` in
# its ops dir, prints {entry, stdout} and waits on stdin; argv: hil_lock.py, lock dir, board, kind, run dir, cmd.
CLAIMANT = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('agentrc_hil_lock', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.BOARD_LOCK_DIR, board, kind, run_dir, cmd = sys.argv[2:7]
ops = m._process().state_dir(run_dir, 'ops')
entry = m.claim_entry(ops, 'claimant')
m.PendingSessions(board, kind).claim(entry)
r = m._process().run_tracked(cmd, timeout=20, settle=0.3, state_dir=ops) if cmd else None
print(json.dumps({'entry': entry, 'stdout': r.stdout if r else ''}), flush=True)
sys.stdin.read()
'''
LOCK_SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'hil_lock.py'


def claimant(case, lock_dir, board, kind, run_dir, cmd=''):
    """A live CLAIMANT process and what it printed; it exits once its stdin closes (a cleanup does)."""
    p = subprocess.Popen([sys.executable, '-I', '-c', CLAIMANT, str(LOCK_SCRIPT), str(lock_dir), board, kind,
                          str(run_dir), cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    case.addCleanup(p.wait)
    case.addCleanup(p.stdin.close)
    case.addCleanup(p.stdout.close)
    return p, json.loads(p.stdout.readline())


def kill_quietly(pid, sig=signal.SIGTERM):
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)


def wait_done(recheck, receipt, wait=10):
    """Until `recheck` (hil_process's) answers done for `receipt`; AssertionError past `wait` s."""
    deadline = time.monotonic() + wait
    while recheck(receipt) != 'done':
        if time.monotonic() > deadline:
            raise AssertionError(f'{receipt.get("token")} never rechecked done')
        time.sleep(0.1)


@contextlib.contextmanager
def umask(mask):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


# a child that outlives its command: its run_tracked supervisor stays, holding its lock (`pending`); prints its pid
LEFT_CHILD = 'sleep 30 </dev/null >/dev/null 2>&1 & echo $!'


def killed_claimant(case, lock_dir, board, kind, run_dir, cmd=LEFT_CHILD):
    """A CLAIMANT SIGKILLed once its `cmd` ran, so its claim names a dead invocation while the child LEFT_CHILD left
    still runs under its supervisor: (its claim entry, the child's pid, killed by a cleanup)."""
    p, out = claimant(case, lock_dir, board, kind, run_dir, cmd)
    child = int(out['stdout'])
    case.addCleanup(kill_quietly, child, signal.SIGKILL)
    p.kill()
    p.wait()
    return out['entry'], child
