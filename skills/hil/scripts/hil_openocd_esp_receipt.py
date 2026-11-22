#!/usr/bin/env python3
"""The state of an `openocd_esp` session from its receipt (`hil` skill, Flashers), shared with
hil_openocd_esp, which re-exports it, and what kind of receipt an operation left. Stdlib only and
importable alone: a detached lock keeper reads a receipt without loading the provider and its
dependencies.

An openocd_esp operation leaves one of two receipts when it cannot prove its process ended: an
openocd session's (its receipt.json path, or the record itself when the file could not be
written), which only the reviewed `end` ends, and a hil_process.run_tracked receipt of an esptool
child (read_security's), which ends once its recheck answers `done`. receipt_kind tells them apart
and describe names one, so no consumer restates either's keys.
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
        with open(f'/proc/{pid}/stat', errors='replace') as f:   # comm may be cut mid-UTF-8 character
            fields = f.read().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return 'unresolved'
    except OSError:
        return 'running'
    return 'running' if len(fields) > 19 and fields[19] == str(start) else 'unresolved'


TRACKED_KEYS = frozenset({'token', 'supervisor', 'lock', 'record'})   # hil_process.run_tracked's receipt


def receipt_kind(receipt) -> str | None:
    """'tracked' for a run_tracked receipt (an esptool child), 'trackedDir' for the provider's
    write-ahead entry of the run_tracked state dir a child is about to start in, 'session' for an
    openocd session's (its receipt path, or its record), 'unreceipted' for the provider's own entry
    of an operation that left no receipt, else None."""
    if isinstance(receipt, str):
        return 'session'
    if not isinstance(receipt, dict):
        return None
    if 'unreceipted' in receipt:
        return 'unreceipted'
    if isinstance(receipt.get('stateDir'), str):
        return 'trackedDir'
    if TRACKED_KEYS <= set(receipt):
        return 'tracked'
    if 'pid' in receipt and 'startTime' in receipt:
        return 'session'
    return None


def describe(receipt) -> str:
    """One line naming the process a receipt stands for, for a refusal or a cleanup log."""
    kind = receipt_kind(receipt)
    if kind == 'tracked':
        return f"esptool child, record {receipt.get('record')}"
    if kind == 'trackedDir':
        return f"esptool children in state dir {receipt['stateDir']} ({receipt.get('what')})"
    if kind == 'unreceipted':
        return f"an operation that left no receipt: {receipt.get('unreceipted')}"
    if kind == 'session' and isinstance(receipt, dict):
        return (f"openocd_esp session pid {receipt.get('pid')} start {receipt.get('startTime')}, receipt file "
                f"unwritten, log {receipt.get('log')}")
    if kind == 'session':
        return f'openocd_esp session, receipt {receipt}'
    return f'unrecognised receipt {receipt!r:.200}'
