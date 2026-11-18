#!/usr/bin/env python3
"""Reset the hub transaction translator (TT) a full-/low-speed DUT sits behind (`hil` skill).
Linux only, stdlib only.

A URB cancelled between a bulk/control start-split and its complete-split can leave that TT
buffer busy until Clear_TT_Buffer (USB 2.0 11.17.5), and Linux xHCI sends one only on an
endpoint halt (v6.12 xhci-ring.c xhci_clear_hub_tt_buffer). Once no buffer is free the TT
NAKs every start-split, so every later transfer to the port times out, re-enumeration
included. Reset_TT (11.24.2.9) frees them without resetting the port or the device.

The request is hil_root.py's `reset-tt BUSPORT` (hil_root.argv: the rig's installed copy
under sudo when it has one), which re-derives and checks the hub itself, bounded by
HELPER_TIMEOUT through `run` (default hil_process.run_cmd), called as
`run(cmd, timeout=..., quiet=True)` and expected to return rc 124 when it had to kill. A
timeout costs that call; later ones on the hub wait only while the killed helper may still run
(_unconfirmed). When to reset is the caller's.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/hil_process.py', 'skills/hil/scripts/hil_root.py')
HELPER_TIMEOUT = 10

# Hubs (by busport, which the helper resolves again) whose helper timed out in this process: the
# (pid, start time) of each process of it that outlived the kill, or, while /proc may hide them, the
# argv tail (a tuple) to look for them by again.
# usbfs open, read and ioctl wait uninterruptibly for the HUB's device lock, which the hub driver
# holds while it enumerates any of its ports (devio.c usb_lock_device), and sudo's root child is
# beyond our kill: another helper would only queue behind one still waiting there.
_unconfirmed: dict = {}
# The last /proc scan and recheck thread: one blocked read, not one per call (hil_process._bounded).
_scan: list = []
_recheck: list = []


def _hil_load():
    """hil_load.py from beside this file, the process's one `agentrc_hil_load`."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, 'hil_load.py'))
    if spec.name not in sys.modules:
        if not os.path.isfile(spec.origin):
            raise ImportError(f'{spec.origin} is missing')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, spec.origin)


hil_load = _hil_load()
hil_load.require(__file__, FILES)
hil_root = hil_load.load_file('skills/hil/scripts/hil_root.py')


def _process():
    return hil_load.load_file('skills/hil/scripts/hil_process.py')


def _survivors(tail) -> set | None:
    """(pid, start time) of each live process whose argv ends with `tail`; None when /proc may
    hide or withhold one, or does not show them all within PROC_SCAN_BOUND."""
    hp = _process()
    return hp._bounded(lambda: _listed(hp, tail), hp.PROC_SCAN_BOUND, _scan)


def _listed(hp, tail) -> set | None:
    if hp._proc_hides_others():
        return None
    tail = [os.fsencode(a) for a in tail]
    found = set()
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            # both files through one directory fd: it stays this process's, not a later one given its pid
            fd = os.open(f'/proc/{name}', os.O_RDONLY | os.O_DIRECTORY)
            try:
                with open('cmdline', 'rb', opener=_at(fd)) as f:
                    args = f.read().split(b'\0')[:-1]      # a zombie's is empty
                if args[-len(tail):] == tail and (start := _start(int(name), fd)) is not None:
                    found.add((int(name), start))
            finally:
                os.close(fd)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return None
    return found


def _at(dir_fd):
    return lambda path, flags: os.open(path, flags, dir_fd=dir_fd)


def _start(pid, dir_fd=None) -> int | None:
    """pid's start time (proc_pid_stat(5) field 22) while it runs, read through `dir_fd`, its /proc
    directory, when given; None once it has exited. OSError when unreadable."""
    try:
        if dir_fd is None:
            fields = _process()._stat_fields(f'/proc/{pid}/stat')
        else:
            with open('stat', errors='replace', opener=_at(dir_fd)) as f:
                fields = f.read().rpartition(')')[2].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None if fields is None or fields[0] in 'ZX' else int(fields[19])


def _alive(left) -> bool:
    """Whether any of `left` may still run: an unreadable one may."""
    try:
        return any(_start(pid) == start for pid, start in left)
    except OSError:
        return True


def _waiting(hub) -> bool:
    """Whether a helper of `hub` that timed out may still be waiting on it, also while /proc does
    not answer within PROC_SCAN_BOUND; forgets it once not."""
    hp = _process()
    if isinstance(left := _unconfirmed.get(hub), tuple):
        if (found := _survivors(left)) != set():
            if found:
                _unconfirmed[hub] = found
            return True
    elif left and hp._bounded(lambda: _alive(left), hp.PROC_SCAN_BOUND, _recheck) is not False:
        return True
    _unconfirmed.pop(hub, None)
    return False


def reset_tt(busport, speed, *, run=None) -> bool:
    """Reset the device's TT if it has one of its own. False when that could not be
    confirmed: this hub's helper timed out now, or an earlier one of it is still running."""
    hub = busport.rpartition('.')[0]
    if _waiting(hub):           # before discovery: a hub mid-reconfiguration may not resolve now
        return False
    if speed not in ('12', '1.5'):
        return True
    try:    # the same rule the root side applies again: high speed, a root port, a single TT -> nothing to reset
        _, port, busnum, devnum, _ = hil_root.tt_hub(busport)
    except hil_root.Refused:
        return True
    node = f'/dev/bus/usb/{busnum:03d}/{devnum:03d}'
    cmd = hil_root.argv('reset-tt', busport)
    try:
        # stderr: run_cmd prints a timeout banner, and a caller's stdout may be its JSON report
        with redirect_stdout(sys.stderr):
            r = (run or _process().run_cmd)(cmd, timeout=HELPER_TIMEOUT, quiet=True)
    except OSError as e:          # no sudo or python to spawn: an ordinary failure
        print(f'Reset_TT {node} port {port} failed: {e}', file=sys.stderr)
        return True
    if r.returncode == 124:
        # sudo's child and the helper's shebang keep the helper's path and arguments
        if (left := _survivors(cmd[-3:])) != set():
            _unconfirmed[hub] = tuple(cmd[-3:]) if left is None else left
        print(f'Reset_TT {node} port {port}: helper timed out, possible hub-lock contention',
              file=sys.stderr)
        return False
    if r.returncode != 0:
        out = _process().cmd_stdout_text(r.stdout)
        print(f'Reset_TT {node} port {port} failed: {out.strip()}', file=sys.stderr)
    return True
