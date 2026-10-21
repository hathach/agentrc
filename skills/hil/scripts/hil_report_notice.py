#!/usr/bin/env python3
"""The HIL report's unavailable notice and its writer lock (`hil` skill, Reporting), shared
with hil_report_core. Stdlib only and importable alone: a consumer whose report core failed
to load still writes the notice the core's prepare_pair writes, under the same lock.

write_unavailable(report_dir, stem, why) replaces `<stem>.md` atomically with the notice,
any earlier Markdown kept below it as labelled legacy text (an earlier notice's own legacy
text kept as is, never nested), while holding `.<stem>.lock` (lock_path). It never waits for
that lock: another holder raises WriterHeld and nothing is written; any other failure (an
unwritable dir) raises OSError.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: a consumer's copy-back location
    fcntl = None
    import msvcrt

NOTICE = '**HIL report unavailable'


class WriterHeld(Exception):
    """Another writer holds `lock`; nothing was written. Deliberately not an OSError, so a
    caller's fallback for an unwritable dir never touches a report another writer owns."""

    def __init__(self, lock: Path):
        super().__init__(f'another writer holds {lock}')
        self.lock = lock


def lock_path(report_dir, stem: str) -> Path:
    return Path(report_dir) / f'.{stem}.lock'


def write_unavailable(report_dir, stem: str, why: str) -> Path:
    """`<stem>.md` in `report_dir` made the notice `why` (one line, whitespace collapsed), under
    the writer lock; returns its path. Raises WriterHeld or OSError."""
    report_dir = Path(report_dir)
    fd = _acquire(report_dir, stem)
    try:
        md = report_dir / f'{stem}.md'
        _write_notice(md, f'{NOTICE}: {" ".join(why.split())}.**\n')
        return md
    finally:
        _release(fd)


def _acquire(report_dir: Path, stem: str) -> int:
    """The writer lock's fd, taken without waiting: WriterHeld when it is not, OSError when the
    dir or the lock file cannot be made. _release gives it back."""
    report_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_path(report_dir, stem)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as e:
        os.close(fd)
        raise WriterHeld(lock) from e
    return fd


def _release(fd: int) -> None:
    try:
        if not fcntl:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _legacy_section(text: str) -> str:
    return ('\n_Legacy text: a previous attempt\'s rendered output whose JSON could not be read. '
            'Its rows are not in this report\'s JSON, no verdict is read from them and no later '
            'run merges them._\n\n' + text)


def _compose(head: str, old: bytes | None) -> bytes:
    """The notice `head` (one line) over the Markdown `old` it replaces."""
    md = head
    text = old.decode('utf-8', errors='replace') if old is not None else ''
    if text.startswith(NOTICE):   # an earlier notice: its legacy text is labelled already
        md += text.split('\n', 1)[1] if '\n' in text else ''
    elif text.strip():
        md += _legacy_section(text)
    return md.encode('utf-8', errors='backslashreplace')


def _write_notice(md: Path, head: str) -> None:
    """write_unavailable under the writer lock its caller holds: the notice composed over the
    current `md`, replaced only when it differs."""
    old = _read_bytes(md)
    data = _compose(head, old)
    if old != data:
        _replace_one(md, data)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _stage(path: Path, data: bytes) -> Path:
    """`data` in a fresh temp file beside `path`, synced; the umask applies, as to the file it replaces."""
    tmp = path.with_name(f'.{path.name}.{secrets.token_hex(6)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _replace_one(path: Path, data: bytes) -> None:
    tmp = _stage(path, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
