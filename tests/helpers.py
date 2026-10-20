"""Helpers the tests share: programs stubbed in Python that start by name on every OS, process liveness, a gh
stub, and temporary directories named as git and realpath name them."""
import os
import subprocess
import sys
import tempfile
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
        return True
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
