#!/usr/bin/env python3
"""The state of an `openocd_esp` session from its receipt (`hil` skill, Flashers), shared with
hil_openocd_esp, which re-exports it. Stdlib only and importable alone: a detached lock keeper
reads a receipt without loading the provider and its dependencies.
"""

from __future__ import annotations

import json

FILES = ()   # loads nothing by path, and is loadable without hil_load.py (hil_openocd_esp)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def session_state(receipt) -> str:
    """The state of the session a receipt names (its path, or the record itself when the provider
    could not write it), reread on every call, for lock retention: 'ended' (closed after the
    reviewed end), 'unresolved' (closed without it, or its process gone), 'running' (a process
    with the receipt's pid and start time, a zombie included, or one whose /proc entry could not
    be read) or 'unreadable' (no well-formed receipt). Liveness by pid and start time only, never
    the argv: whether `end` may address the server is esp_session.same_process's (argv and start
    time), which a 'running' session need not satisfy."""
    rec = receipt
    if not isinstance(rec, dict):
        try:
            with open(receipt) as f:
                rec = json.load(f)
        except (OSError, ValueError, TypeError):
            return 'unreadable'
    try:
        closed, pid, start = rec['closed'], rec['pid'], rec['startTime']
        if not isinstance(closed, bool) or not _int(pid) or pid <= 0 or not str(start).isdigit():
            return 'unreadable'
    except (KeyError, TypeError):
        return 'unreadable'
    if closed:
        return 'ended' if rec.get('cleanup') == 'done' else 'unresolved'
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return 'unresolved'
    except OSError:
        return 'running'
    return 'running' if len(fields) > 19 and fields[19] == str(start) else 'unresolved'
