"""Windows runtime helpers the skills, hooks and statusline share: a program lookup that never takes the working
directory, a bounded process-tree kill, and os.replace retried while another process holds the target open.

A script reaches it through its resolved path, as chief_guard.py reaches shadows.py:
`sys.path.insert(0, str(Path(__file__).resolve().parents[N] / 'lib'))`, then `import windows`.
rtt.py, and pc_sample.py with the probe_state.py it loads, are copied around alone and keep their own copies."""
import contextlib
import os
import subprocess
import time
from pathlib import PureWindowsPath

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)   # taskkill from a process without a console opens none


def absolute(directory):
    """Has a drive and a root: '.', 'bin' or '\\bin' name a directory of the working directory or its drive."""
    path = PureWindowsPath(directory)
    return bool(path.drive and path.root)


def on_path(name, exts, path=None):
    """The program as Windows' PATH alone finds it: name plus the first of exts in each directory of path (a list;
    PATH's entries by default), else None. shutil.which and CreateProcess try the working directory, perhaps a
    contributor's checkout, first, as a relative entry would, so only absolute directories count; a quoted entry is
    the directory it quotes."""
    for directory in os.environ.get('PATH', '').split(os.pathsep) if path is None else path:
        directory = directory.strip('"')
        if not absolute(directory):
            continue
        for ext in exts:
            program = os.path.join(directory, name + ext)
            if os.path.isfile(program):
                return program
    return None


def kill_tree(proc, timeout):
    """Kill the Popen proc and, on Windows, its descendants, which Popen.kill (TerminateProcess) leaves running,
    then wait up to timeout s for proc; its exit code, or None if it still runs. Windows runs taskkill /T /F from
    SystemRoot's System32, never a taskkill.exe the working directory holds, bounded by timeout s; a SystemRoot that
    is not absolute, or a taskkill that fails or hangs, still reaches proc.kill(). A proc that already exited is
    left alone: its pid could name another process's tree. Draining proc's pipes stays the caller's."""
    if os.name == 'nt' and proc.poll() is None:
        system_root = os.environ.get('SystemRoot', '')
        if absolute(system_root):
            taskkill = os.path.join(system_root, 'System32', 'taskkill.exe')
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run([taskkill, '/T', '/F', '/PID', str(proc.pid)], capture_output=True, timeout=timeout,
                               creationflags=NO_WINDOW)
    proc.kill()
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        return None


def replace(src, dst, tries=50):
    """os.replace; on Windows retried 20 ms apart, tries times in all, while another process (a reader, an indexer)
    has dst open, which refuses it there. The last PermissionError escapes with dst untouched."""
    for _ in range(tries - 1 if os.name == 'nt' else 0):
        try:
            return os.replace(src, dst)
        except PermissionError:
            time.sleep(0.02)
    return os.replace(src, dst)
