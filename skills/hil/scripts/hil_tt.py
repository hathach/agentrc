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
`run(cmd, timeout=..., quiet=True)` and expected to return rc 124 when it had to kill. When
to reset is the caller's.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/hil_process.py', 'skills/hil/scripts/hil_root.py')
HELPER_TIMEOUT = 10

# Hubs (by busport, which the helper resolves again) whose helper timed out in this process.
# The usbfs ioctl waits for the HUB's device lock, which the hub driver holds while it
# enumerates any of its ports: the helper may still be waiting there, so another would only
# queue behind it.
_unconfirmed: set = set()


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


def reset_tt(busport, speed, *, run=None) -> bool:
    """Reset the device's TT if it has one of its own. False when that could not be
    confirmed: this hub's helper timed out, now or earlier in this process."""
    hub = busport.rpartition('.')[0]
    if hub in _unconfirmed:     # before discovery: a hub mid-reconfiguration may not resolve now
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
        _unconfirmed.add(hub)
        print(f'Reset_TT {node} port {port}: helper timed out, possible hub-lock contention',
              file=sys.stderr)
        return False
    if r.returncode != 0:
        out = _process().cmd_stdout_text(r.stdout)
        print(f'Reset_TT {node} port {port} failed: {out.strip()}', file=sys.stderr)
    return True
