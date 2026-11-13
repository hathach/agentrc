#!/usr/bin/env python3
"""Board-lock primitive shared by every project on a HIL host: the per-board kernel
flocks in BOARD_LOCK_DIR and their holder records (`hil` skill, Board locks), with a
hold/release/status CLI and resolve/compact for the boards' cleanup logs. A project's HIL
contract wraps it for its roster and lock names.
"""
import argparse
import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import itertools
import json
import math
import os
import re
import secrets
import select
import signal
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/hil_process.py')


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

BOARD_LOCK_DIR = '/tmp/tinyusb-hil-locks'
CI_REASON = 'hil_test.py'
# records naming these are never signalled by `release`, with or without the protected mark
PROTECTED_REASONS = {CI_REASON, 'farm_check'}
# the role of an older helper's cleanup holder, a process that kept a board past its task
CLEANUP_ROLE = 'cleanup-holder'
RECOVERY_BLOCKER = ("an older helper's cleanup holder, which never frees on its own: its project's "
                    "`hil_lock.py release --token` ends it")
CLEANUP_OWED = 'cleanupOwed'  # record mark of a taker whose task restores the board; older helpers ignore it
HOLD_KIND = 'hil_lock-hold'   # `holder` of a record this CLI's own hold process wrote
JOURNAL = '.takeover'         # <board>.takeover: an older helper's recovery takeover, live or left
CLEANUP_LOG = '.cleanup.jsonl'   # <board>.cleanup.jsonl: the board's failed cleanups and restorations
LOG_WAIT = 5.0                # s a cleanup-log reader or writer waits for its mutex
REWRITING = b'#'              # byte 0 of a record being rewritten: unreadable
ACQUIRING = b'~'              # byte 0 while take_board_lock writes its first record: a free board
UNLOGGED = b'!'               # byte 0 while end_failed writes an unlogged failure's evidence into its record
DIR_MODE = 0o755
SHARE_STEP = (f'a second OS user needs the lock dir sticky and world-writable, a one-time host step '
              f'once every project on the host runs this helper: sudo install -d -m 1777 {BOARD_LOCK_DIR}')
RELEASE_WAIT = 3.0            # s a signalled holder gets to clear its records and exit
# herdr-core (hc) ends every process whose starting environment carries a terminal's HC_SCOPE when it cleans up
# after that terminal, setsid'd ones included: the hold holder, which outlives its terminal, starts without it
HC_SCOPE = 'HC_SCOPE'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]*\Z')


class LockUnavailable(RuntimeError):
    """The lock dir, lock file or holder record cannot be used: the board is refused, never
    run unlocked. The OSError is the __cause__."""


class BoardLocked(RuntimeError):
    """Another session holds the board; the message names its holder."""


class RecoveryBlocked(BoardLocked):
    """The flock is held by an older helper's cleanup holder (its record's `role`), which never
    frees on its own, so it is never waited out; its project's `release --token` ends it."""


class ReleaseRefused(RuntimeError):
    """A token release was refused before signalling, or its delivery or wait failed."""


class LogRefused(RuntimeError):
    """A cleanup-log command (resolve, compact) refused before changing the log."""


# --- files in the shared dir ----------------------------------------------------------------
def _check_name(board) -> None:
    if not isinstance(board, str) or not _NAME.match(board):
        raise OSError(errno.EINVAL, f'invalid board lock name {board!r}')


def _open_dir(create=False) -> int:
    """BOARD_LOCK_DIR as an fd every file operation goes through. Refuses a symlink, a
    non-directory, or a dir another non-root user owns without the sticky bit."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    made = False
    if create:
        try:
            os.mkdir(BOARD_LOCK_DIR, DIR_MODE)
            made = True
        except FileExistsError:
            pass
    fd = os.open(BOARD_LOCK_DIR, flags)
    try:
        st = os.fstat(fd)
        if made and st.st_uid == os.geteuid():
            os.fchmod(fd, DIR_MODE)        # umask-proof; widening it is the host step (SHARE_STEP)
        if st.st_uid not in (0, os.geteuid()) and not st.st_mode & stat.S_ISVTX:
            raise OSError(errno.EPERM, f'{BOARD_LOCK_DIR} is owned by uid {st.st_uid} and not sticky')
    except OSError:
        os.close(fd)
        raise
    return fd


def _open_file(dfd, name, flags, create=False) -> int:
    """A regular, single-link file in the lock dir, never through a link; O_NONBLOCK so a
    planted FIFO cannot hang the open. Existing first: fs.protected_regular refuses O_CREAT
    on another user's file in a sticky dir even when it exists."""
    base = flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(name, base, dir_fd=dfd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            fd = os.open(name, base | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=dfd)
            try:
                os.fchmod(fd, 0o666)
            except OSError:
                os.close(fd)
                raise
        except FileExistsError:
            fd = os.open(name, base, dir_fd=dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise OSError(errno.EINVAL, f'{name}: not a regular single-link file')
    return fd


def _read_file(board, suffix):
    """The bytes of <board><suffix>, or None when it (or the dir) is absent; OSError when it
    is unsafe or unreadable."""
    _check_name(board)
    try:
        dfd = _open_dir()
    except FileNotFoundError:
        return None
    try:
        fd = _open_file(dfd, board + suffix, os.O_RDONLY)
    except FileNotFoundError:
        return None
    finally:
        os.close(dfd)
    with os.fdopen(fd, 'rb') as f:
        return f.read()


def _replace_file(dfd, name, data: bytes, mode=0o644, owner=None) -> None:
    """Replace `name` in the lock dir `dfd` with `data`, atomically and durably: a crash leaves
    the old file or the new one, and the rename itself is fsynced through the dir. `mode` (0644,
    so another OS user reads it) whatever the umask, and `owner`, (uid, gid), when given; the temp
    file is 0600 until then. In a sticky dir only its owner or root replaces it. A failed replace
    removes its temp file; one a crash leaves is hidden and never read."""
    tmp = f'.{name}.{os.getpid()}.{secrets.token_hex(4)}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        try:
            if owner is not None:
                os.fchown(fd, *owner)
            os.fchmod(fd, mode)
            _pwrite_all(fd, data, 0)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dfd)
        raise
    os.fsync(dfd)


def _parse(data):
    """'absent', 'empty', 'unreadable' or 'record', with the record's dict."""
    if data is None:
        return 'absent', None
    if not data.strip():
        return 'empty', None
    try:
        rec = json.loads(data)
    except ValueError:
        return 'unreadable', None
    return ('record', rec) if isinstance(rec, dict) else ('unreadable', None)


def _names(suffix, skip_hidden=False) -> list:
    if not os.path.isdir(BOARD_LOCK_DIR):
        return []
    return sorted(fn[:-len(suffix)] for fn in os.listdir(BOARD_LOCK_DIR)
                  if fn.endswith(suffix) and not (skip_hidden and fn.startswith('.')))


def lock_names() -> list:
    """The boards with a lock file, held or not."""
    return _names('.lock')


def journal_names() -> list:
    """The boards carrying a takeover journal."""
    return _names(JOURNAL, skip_hidden=True)


def lock_path(board: str) -> str:
    return os.path.join(BOARD_LOCK_DIR, f'{board}.lock')


def flock_nb(board: str):
    """Open-or-create the lock file WITHOUT truncating (a losing racer must not wipe the
    winner's record) and take LOCK_EX|LOCK_NB. Returns the open handle; raises OSError when
    the file is unsafe or the flock is held elsewhere (handle already closed)."""
    _check_name(board)
    dfd = _open_dir(create=True)
    try:
        fd = _open_file(dfd, f'{board}.lock', os.O_RDWR, create=True)
    finally:
        os.close(dfd)
    fh = os.fdopen(fd, 'r+')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise
    return fh


# --- holder records -------------------------------------------------------------------------
def _pwrite_all(fd, data, offset) -> None:
    """os.pwrite until every byte is written; a write that makes no progress raises."""
    while data:
        n = os.pwrite(fd, data, offset)
        if n <= 0:
            raise OSError(errno.EIO, f'write made no progress at offset {offset}')
        data, offset = data[n:], offset + n


def _write_all(fd, data) -> None:
    """os.write until every byte is written, for an O_APPEND fd; no progress raises."""
    while data:
        n = os.write(fd, data)
        if n <= 0:
            raise OSError(errno.EIO, 'write made no progress')
        data = data[n:]


RECORD_OWN = ('pid', 'since')   # what every record write stamps itself


def _own_fields(extra, *marks) -> None:
    """ValueError when `extra` names a field the write stamps itself (RECORD_OWN, `marks`): it
    would replace the holder's pid or start, or fake or cancel a mark."""
    if clash := sorted(set(RECORD_OWN + marks) & set(extra)):
        raise ValueError(f'record fields {clash} are reserved: the lock writes them itself')


def write_record(fh, reason: str, **extra) -> None:
    """Holder record; the flock itself is already held. Raises OSError on a write failure: a
    hold whose record is missing is invisible to status/release, so every caller refuses it.
    `extra` adds a tool's own fields (hil_reserve: token, start, locks, role). Overwritten in
    place, inode kept, behind REWRITING, a non-JSON first byte that is restored last: an
    interrupted rewrite leaves the old record or an unreadable one, never an empty record or a
    valid splice of new and old bytes that a cleanup-owing record would vanish as."""
    _write_record(fh, REWRITING, reason, extra)


def _write_record(fh, first: bytes, reason: str, extra: dict) -> None:
    """write_record behind `first`: ACQUIRING only for take_board_lock's first record
    (agentrc#154), whose taker never returned when it is left, so a free flock under it is a
    free board; a helper older than ACQUIRING reads it as unreadable and refuses, fail-closed
    as for REWRITING."""
    data = json.dumps({'pid': os.getpid(), 'reason': reason,
                       'since': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **extra}).encode()
    _overwrite(fh.fileno(), first, data)


def _overwrite(fd, first: bytes, data: bytes) -> None:
    """The file's bytes made `data` behind the non-JSON byte `first`, data's own first byte last."""
    _pwrite_all(fd, first, 0)
    _pwrite_all(fd, data[1:], 1)
    os.ftruncate(fd, len(data))
    _pwrite_all(fd, data[:1], 0)


def clear_record(fh) -> bool:
    """Clear our record before dropping the flock so records stay truthful; False when the
    truncate failed and the record is still there."""
    try:
        fh.truncate(0)
    except OSError:
        return False
    return True


def _restore_record(fh, data: bytes) -> bool:
    """Put back the record `data` a failed rewrite may have cut short, behind REWRITING: an
    interrupted restore reads as unreadable (logged), never as a valid splice of the new record.
    One whose first write fails leaves what the rewrite left. False when it failed."""
    try:
        _overwrite(fh.fileno(), REWRITING, data)
    except OSError:
        return False
    return True


def release_board_lock(fh) -> None:
    """A taker's exit: clear the record, then drop the flock. It says nothing about the board's
    state: a task whose cleanup failed ends with end_failed, one that restored a board recorded
    as failed says so with cleanup_restored."""
    clear_record(fh)
    fh.close()


def _record(board: str):
    """(bytes, state, record) of the board's holder record; bytes None when absent or unsafe."""
    try:
        data = _read_file(board, '.lock')
    except OSError:
        return None, 'unreadable', None
    return (data, *_parse(data))


def record_state(board: str):
    """(state, record) of the board's holder record: 'absent', 'empty', 'unreadable' (an
    unsafe file, bad JSON, or JSON that is not an object) or 'record' with its dict."""
    return _record(board)[1:]


def _acquiring(data) -> bool:
    """A record take_board_lock was still writing: on a free flock, its taker never returned."""
    return data is not None and data[:1] == ACQUIRING


def read_record(board: str):
    return record_state(board)[1]


def journal_state(board: str):
    """None without a journal, else its dict ({} when it cannot be read)."""
    try:
        state, rec = _parse(_read_file(board, JOURNAL))
    except OSError:
        return {}
    return None if state == 'absent' else rec or {}


def is_cleanup_holder(info) -> bool:
    return isinstance(info, dict) and info.get('role') == CLEANUP_ROLE


def is_protected(info) -> bool:
    """`release` never signals it; a malformed mark never cancels the legacy names."""
    return isinstance(info, dict) and (info.get('protected') is True or info.get(CLEANUP_OWED) is True
                                       or info.get('reason') in PROTECTED_REASONS)


def _owes_cleanup(info) -> bool:
    """A record that, left behind on a free flock, means its task ended without its cleanup: this
    helper's mark, an older helper's `protected` one, or an older helper's cleanup holder."""
    return is_cleanup_holder(info) or (isinstance(info, dict) and (info.get('protected') is True
                                                                   or info.get(CLEANUP_OWED) is True))


def holder_text(info) -> str:
    """A holder record for a message; an older helper's cleanup holder is named as one."""
    if not isinstance(info, dict):
        return 'unknown holder'
    text = json.dumps(info)
    return f'{text} ({RECOVERY_BLOCKER})' if is_cleanup_holder(info) else text


def _held_refusal(board) -> BoardLocked:
    """Why a flock another process holds refuses `board`: RecoveryBlocked for an older helper's
    cleanup holder that is alive as recorded, else BoardLocked (contention: a taker still logging
    a dead holder's record holds the flock under it)."""
    rec = read_record(board)
    why = f'board locked: {holder_text(rec)}'
    live = is_cleanup_holder(rec) and _alive(rec) and rec.get('start') in (None, proc_start(rec['pid']))
    return RecoveryBlocked(why) if live else BoardLocked(why)


def _handle_record(fh) -> bytes:
    fd = fh.fileno()
    return os.pread(fd, os.fstat(fd).st_size, 0)


def _leftover(board, fh, data=None):
    """What a task that ended without its cleanup left on the board whose flock we now hold, as a
    failure event's fields, or None: a record owing cleanup, one that cannot be read (cut short
    mid-write, it may have owed one), or an older helper's takeover journal not yet logged (its
    `journalDigest`; one this user cannot unlink stays, logged once). `data`: the record's bytes
    when the caller already read them under this flock."""
    found = {}
    raw, kept = _read_journal(board)
    if kept:
        found['journalKept'] = True
    if raw is not None:
        state, journal = _parse(raw)
        found['journal'] = journal if state == 'record' else {'unreadable': raw[:2000].decode(errors='backslashreplace')}
        found['journalDigest'] = hashlib.sha256(raw).hexdigest()
    if data is None:
        data = _handle_record(fh)
    state, rec = _parse(data)
    if state == 'record' and _owes_cleanup(rec):
        found['task'] = rec
    elif state == 'unreadable' and not _acquiring(data):
        found['task'] = {'unreadable': data[:2000].decode(errors='backslashreplace')}
        if data[:1] == UNLOGGED:     # an unlogged failure's evidence cut short: it stays open (resolve_restored)
            found['task']['receipts'] = [{'unrecheckable': 'an unlogged failure whose evidence was not kept'}]
    if 'task' not in found and 'journalDigest' in found and _logged_journal(board, found['journalDigest']):
        return None
    if not found:
        return None
    task = found.get('task') or {}
    return {'how': 'found-left', 'reason': task.get('reason'), 'pid': task.get('pid'),
            'detail': task.get('detail') or 'the task ended without its cleanup', **found}


def _read_journal(board):
    """(bytes, kept) of the board's takeover journal, (None, False) when absent; one that cannot be
    read reads as its error's text, kept (its bytes unknown: logged, never unlinked)."""
    try:
        return _read_file(board, JOURNAL), False
    except OSError as e:
        return f'cannot be read: {e}'.encode(), True


def _logged_journal(board, digest) -> bool:
    try:
        return any(e.get('journalDigest') == digest for e in cleanup_events(board))
    except OSError:
        return False


def _unlink_journal(board, left) -> None:
    """Once `left` is logged: its journal goes when this user may unlink it (in the sticky dir,
    only its owner or root)."""
    if 'journalDigest' not in left or left.get('journalKept'):
        return
    with contextlib.suppress(OSError):
        dfd = _open_dir()
        try:
            os.unlink(board + JOURNAL, dir_fd=dfd)
        finally:
            os.close(dfd)


def _unavailable(e) -> LockUnavailable:
    hint = f'; {SHARE_STEP}' if isinstance(e, PermissionError) else ''
    return LockUnavailable(f'board lock unavailable: {e}{hint}')


def _valid_group(locks, board) -> bool:
    """A group as its taker declares it: unique valid lock names, `board` among them."""
    return (isinstance(locks, (list, tuple)) and all(isinstance(b, str) and _NAME.match(b) for b in locks)
            and board in locks and len(set(locks)) == len(locks))


def take_board_lock(board: str, reason: str, *, protected=False, **extra):
    """Take the board's flock and record ourselves as its holder; returns the open handle
    (keep it referenced; release_board_lock or end_failed ends it). Raises BoardLocked when
    another process holds the flock (RecoveryBlocked when that is an older helper's cleanup
    holder), and LockUnavailable when the lock cannot be taken or recorded (fail-closed). A
    free flock is always taken: what a task that ended without its cleanup left there
    (_leftover) is appended to the board's cleanup log first, never a refusal.

    The handle's `cleanup_failures` are the board's open failed cleanups (open_cleanup_failures),
    for the caller's report before any hardware access, the leftover found now last with
    `noteError` when its append failed (failure_report renders them).

    `protected` marks the record `cleanupOwed`: a task that restores the board, so `release`
    never signals it and a record it leaves is logged as a failed cleanup. It also declares
    the task's group on that first record: `locks`, the names of every board the task takes
    (unique, this one among them; default this board alone). A malformed group, or an `extra`
    field the take writes itself (RECORD_OWN, and `cleanupOwed` when protected), raises
    ValueError before anything is touched."""
    _own_fields(extra, *((CLEANUP_OWED,) if protected else ()))
    if protected:
        try:
            _check_name(board)
        except OSError as e:
            raise _unavailable(e) from e
        locks = extra.get('locks', [board])
        if not _valid_group(locks, board):
            raise ValueError(f'protected group {locks!r:.200} must be unique valid lock names, {board} among them')
        extra = {CLEANUP_OWED: True, **extra, 'locks': list(locks)}
    try:
        fh = flock_nb(board)
    except BlockingIOError:
        raise _held_refusal(board) from None
    except OSError as e:
        raise _unavailable(e) from e
    found, note_error = b'', None      # note_error stays None until the leftover's log attempt returned
    try:
        try:
            found = _handle_record(fh)
            left = _leftover(board, fh, data=found)
        except OSError as e:
            raise LockUnavailable(f'board lock unavailable: holder record: {e}') from e
        note_error = _log_leftover(board, left)
        # announce ourselves so the other side's conflict message is truthful
        try:
            _write_record(fh, ACQUIRING, reason, extra)
        except OSError as e:
            why = _abandon(fh, found, note_error)
            fh = None
            raise LockUnavailable(f'board lock unavailable: holder record: {e}{why}') from e
        fh.cleanup_failures = open_cleanup_failures(board) + ([dict(left, noteError=note_error)] if note_error else [])
    except BaseException:
        # an interrupt included: a caller keeping the exception must not keep the flock (agentrc#477)
        if fh is not None:
            _abandon(fh, found, note_error)
        raise
    return fh


def _abandon(fh, found, note_error) -> str:
    """Undo a take that will not return, then drop its flock: before the leftover's log attempt
    returned (`note_error` None) the record is left as found, for the next taker to log; once it is
    logged ours is cleared; else the leftover record `found` is put back, its only evidence when no
    journal holds it. Returns why the leftover was not logged, or ''."""
    why = ''
    try:
        if note_error == '':
            clear_record(fh)
        elif note_error:
            ok = _restore_record(fh, found)
            why = f'; leftover {"kept for the next taker" if ok else "record not restored"}, {note_error}'
    finally:
        try:
            fh.close()   # even a failing close lets the fd (and flock) go
        except OSError:
            pass
    return why


def _log_leftover(board, left) -> str:
    """Append what `left` (_leftover's) found to the board's cleanup log, then unlink a journal
    it archived; '' when done or nothing was left, else why it was not logged (kept unlinked)."""
    if not left:
        return ''
    try:
        _log_failure(board, left)
    except (OSError, TimeoutError) as e:
        return f'cleanup log not written: {e}'
    _unlink_journal(board, left)
    return ''


# --- cleanup log ----------------------------------------------------------------------------
# <board>.cleanup.jsonl beside the lock file: one JSON event per line, `failed` (a task's cleanup
# did not end proven, or a leftover was found), `restored` (a later task restored the board after
# the failure it names) or `acknowledged` (an operator's `resolve` of the failure it names, no
# evidence of a restoration). 0666 and never unlinked, so every runner's uid appends to it in a
# sticky dir; only `compact`, as its owner or root, replaces it whole. Reads, appends and that
# replace are serialised by <log>.mutex, not the board flock, and a torn last line is cut before
# the next append.
CLOSING_EVENTS = ('restored', 'acknowledged')


def _log_name(board) -> str:
    _check_name(board)
    return board + CLEANUP_LOG


@contextlib.contextmanager
def _log_locked(board, create=True):
    """The log's mutex held, waited for at most LOG_WAIT (TimeoutError past it); yields the lock
    dir's fd. A reader (create=False) creates nothing: FileNotFoundError without the dir or the
    mutex, which every append creates before its log, so there is nothing to read yet."""
    dfd = _open_dir(create=create)
    try:
        fd = _open_file(dfd, _log_name(board) + '.mutex', os.O_RDWR, create=create)
        try:
            deadline = time.monotonic() + LOG_WAIT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'{_log_name(board)}.mutex held past {LOG_WAIT}s') from None
                    time.sleep(0.05)
            yield dfd
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def _log_append(board, event: dict, check=None) -> dict:
    """Append `event`, stamped, durably; OSError or TimeoutError when it cannot be. check(events), given
    the log's events under the same mutex hold, raises to refuse the append."""
    event = {'v': 1, **event, 'board': board, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'uid': os.geteuid()}
    line = (json.dumps(event) + '\n').encode()
    with _log_locked(board) as dfd:
        fd = _open_file(dfd, _log_name(board), os.O_RDWR | os.O_APPEND, create=True)
        try:
            size = _whole_lines(fd)
            os.ftruncate(fd, size)     # an earlier append's torn line goes
            if check is not None:
                check(_events(_pread_all(fd, size)))
            _write_all(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(dfd)
    return event


def _log_failure(board, fields: dict) -> dict:
    return _log_append(board, {'event': 'failed', 'id': secrets.token_hex(8), **fields})


def cleanup_events(board) -> list:
    """Every whole event in the board's cleanup log, oldest first, read under its mutex (an
    append may cut a torn tail first); OSError (TimeoutError among them) when it cannot be read."""
    try:
        with _log_locked(board, create=False):
            data = _read_file(board, CLEANUP_LOG) or b''
    except FileNotFoundError:
        data = b''
    return _events(data)


def _events(data) -> list:
    events = []
    for line in data[:data.rfind(b'\n') + 1].splitlines():
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
    return events


def _pread_all(fd, size) -> bytes:
    data = b''
    while len(data) < size:
        chunk = os.pread(fd, size - len(data), len(data))
        if not chunk:
            raise OSError(errno.EIO, f'read made no progress at offset {len(data)}')
        data += chunk
    return data


def _closed_ids(events) -> set:
    return {e['failure'] for e in events if e.get('event') in CLOSING_EVENTS and isinstance(e.get('failure'), str)}


def open_cleanup_failures(board) -> list:
    """The board's `failed` events no `restored` or `acknowledged` event names, oldest first: each
    stays open until a task proves its own evidence resolved, or an operator acknowledges it,
    whatever failed after it. `[{'unavailable': why}]` when the log cannot be read."""
    try:
        events = cleanup_events(board)
    except OSError as e:
        return [{'unavailable': f'cleanup log unreadable: {e}'}]
    closed = _closed_ids(events)
    # a failure whose id is not a string can never be named closed: it stays open
    return [e for e in events if e.get('event') == 'failed'
            and not (isinstance(e.get('id'), str) and e['id'] in closed)]


def _guarded(step, what, note, interrupted, default=None):
    """step(), or `default` once note('<what>: <error>') took its failure; an interrupt it raised
    is kept in `interrupted` for the caller to raise once its cleanup is done."""
    try:
        return step()
    except BaseException as e:
        if not isinstance(e, Exception):
            interrupted.append(e)
        note(f'{what}: {e!r:.200}')
        return default


def end_failed(handles, reason, *, detail, receipts=(), retained=(), **extra) -> dict:
    """End a task whose cleanup did not end proven: on every board of `handles` ({board: handle}),
    append a `failed` event (the holder record, `detail`, the run_tracked `receipts` and the
    `retained` sessions not proven ended, `extra`), clear the record once that event is logged
    and drop the flock, going on whatever fails. A record whose event was not logged is kept, its
    only evidence, for the next take to log found-left, rewritten first with the failure's `detail`,
    `receipts` and `retained` (_keep_unlogged) so that resolve_restored rechecks them; a rewrite cut
    short reads as an unrecheckable receipt, and only one whose first byte cannot be written leaves
    the record without them. If the log is still unwritable at that take, it reports the failure
    only in its cleanup_failures. Returns {board: [error text]} for the boards where a step failed."""
    return _end_failed_each(handles, reason, detail, receipts, retained, extra, {})


def _end_failed_each(handles, reason, detail, receipts, retained, extra, ended) -> dict:
    """end_failed, setting ended[board] to {'logged', 'cleared', 'evidence'}: whether its failed event
    was appended, its record cleared and `receipts` and `retained` collected (an event is logged
    with what one that failed yielded and an unrecheckable marker in its receipts)."""
    errors, interrupted = {}, []

    def guard(boards, what, step, default=None):
        """step(), its failure noted on `boards`; an interrupt is kept until every board is released."""
        def note(text):
            for b in boards:
                errors.setdefault(b, []).append(text)
        return _guarded(step, what, note, interrupted, default)
    # a signal between the guarded steps waits until every board is released (None: the guards alone)
    hold = guard((), 'interrupt hold', lambda: _process().InterruptHold())

    def collect(items, got):
        for item in items:
            got.append(item)      # kept as yielded: an iterable that raises later loses none of them
        return True
    given = {'receipts': receipts, 'retained': retained}
    receipts, retained = [], []
    lost = [k for k, got in (('receipts', receipts), ('retained', retained))
            if guard(handles, f'cleanup log not written: {k}', lambda k=k, got=got: collect(given[k], got)) is None]
    # an operation in the lost part may still run: the marker keeps the logged failure open (resolve_restored)
    receipts += [{'unrecheckable': f'its {k} were not all collected'} for k in lost]
    evidence = not lost
    for b, fh in handles.items():
        ended[b] = {'logged': False, 'cleared': False, 'evidence': evidence}
        ended[b]['logged'] = guard([b], 'cleanup log not written', lambda: _log_ended(
            b, fh, reason, detail, receipts, retained, extra)) is not None
        if ended[b]['logged']:
            cleared = guard([b], 'record not cleared', lambda: clear_record(fh))
            if cleared is False:
                errors.setdefault(b, []).append('record not cleared')
            ended[b]['cleared'] = cleared is True
        else:
            guard([b], 'evidence not kept in the record', lambda: _keep_unlogged(
                fh, reason, detail, receipts, retained))
        guard([b], 'close', fh.close)
    if hold is not None:
        guard((), 'deferred interrupt', hold.release)
    if interrupted:
        raise interrupted[0]
    return errors


def _log_ended(board, fh, reason, detail, receipts, retained, extra) -> dict:
    try:
        state, rec = _parse(_handle_record(fh))
    except OSError:
        state, rec = 'unreadable', None
    return log_cleanup_failure(board, reason, detail=detail, task=rec if state == 'record' else None, receipts=receipts,
                        retained=retained, **extra)


def _keep_unlogged(fh, reason, detail, receipts, retained) -> None:
    """Rewrite the record of a failure end_failed could not log with its `detail`, `receipts` and
    `retained` (a value JSON cannot hold as its repr; evidence it cannot hold at all as an
    unrecheckable receipt), marked cleanupOwed, behind UNLOGGED. OSError when a write fails."""
    try:
        state, rec = _parse(_handle_record(fh))
    except OSError:
        state, rec = 'unreadable', None
    fields = {**(rec if state == 'record' else {'pid': os.getpid(), 'reason': reason}), CLEANUP_OWED: True}
    try:
        data = json.dumps({**fields, 'detail': detail, 'receipts': receipts, 'retained': retained}, default=repr)
    except (TypeError, ValueError) as e:
        data = json.dumps({**fields, 'detail': f'{detail!r:.2000}',
                           'receipts': [{'unrecheckable': f'its evidence cannot be recorded: {e!r:.200}'}]})
    _overwrite(fh.fileno(), UNLOGGED, data.encode())


def log_cleanup_failure(board, reason, *, detail, task=None, receipts=(), retained=(), **extra) -> dict:
    """Append a `failed` event for a task whose cleanup did not end proven (how: ended-failed),
    for a caller that does not own the board's flock, as a session command under a detached
    hold: end_failed is the flock owner's. Returns the event; OSError (TimeoutError among
    them), TypeError or ValueError when it cannot be appended."""
    return _log_failure(board, {'how': 'ended-failed', 'reason': reason, 'pid': os.getpid(), 'detail': detail,
                                'task': task, 'receipts': list(receipts), 'retained': list(retained), **extra})


def cleanup_restored(board, failure_id, **evidence) -> dict:
    """Append a `restored` event for the failure `failure_id` (resolve_restored decides which);
    every other failure stays open. OSError or TimeoutError when it cannot be appended."""
    return _log_append(board, {'event': 'restored', 'failure': failure_id, 'pid': os.getpid(), **evidence})


def _evidence(failure, task, key):
    """The failure's own `key`, or its legacy task record's when its own is absent or empty."""
    own = failure.get(key)
    return own if own not in (None, []) else task.get(key, [])


def _journal_records(journal):
    """The holder records an older helper's takeover journal kept as `predecessor`s down its
    `prior` chain, or None when a link cannot be read; an empty or acquiring one kept none."""
    recs = []
    while True:
        if not isinstance(journal, dict) or not isinstance(journal.get('predecessor'), str):
            return None
        data = journal['predecessor'].encode()
        state, rec = _parse(data)
        if state == 'record':
            recs.append(rec)
        elif state == 'unreadable' and not _acquiring(data):
            return None
        journal = journal.get('prior')
        if journal is None:
            return recs


def resolve_restored(board, failures, **evidence) -> list:
    """After the caller restored `board`: mark restored (cleanup_restored, with `evidence`) each of
    `failures` (its take's `cleanup_failures`, or open_cleanup_failures under its hold) whose own
    evidence is resolved: every receipt, its task record's (an older helper's, or one end_failed kept
    unlogged) and those its takeover journal kept included, rechecks `done` as the caller, and
    every session they retained reads ended (_release_blocker). Returns why each other one stays open or its restoration was not logged;
    a failure without an id is skipped."""
    notes = []
    for f in failures:
        fid = f.get('id')
        if not isinstance(fid, str):
            continue
        found = _failure_evidence(f)
        if found is None:
            notes.append(f'{fid} stays open: its takeover journal cannot be read')
            continue
        receipts, retained, _ = found
        if not all(isinstance(x, list) for x in receipts + retained):
            notes.append(f'{fid} stays open: its evidence is malformed')
        elif why := _release_blocker(sum(receipts, []), retained=sum(retained, [])):
            notes.append(f'{fid} stays open: {why}')
        else:
            try:
                cleanup_restored(board, fid, **evidence)
            except OSError as e:
                notes.append(f'{fid} restoration not logged: {e}')
    return notes


def _failure_evidence(f):
    """([receipts], [retained], records) of the logged failure `f`, lists as logged: its own (or its
    legacy task record's), then those of each of the records its takeover journal kept, read once;
    None when that journal cannot be read."""
    task = f.get('task') if isinstance(f.get('task'), dict) else {}
    held = _journal_records(f['journal']) if 'journal' in f else []
    if held is None:
        return None
    return (*([_evidence(f, task, k)] + [r.get(k, []) for r in held] for k in ('receipts', 'retained')), held)


def acknowledge_failure(board, failure_id, reason) -> dict:
    """An operator's resolution of the board's open failure `failure_id` (`hil_lock.py resolve`):
    an `acknowledged` event with `reason`, which closes it as `restored` does but is never evidence
    the board was restored; automated callers use resolve_restored. LogRefused, nothing logged, for
    a blank reason, a failure the log does not hold or one already closed, while its evidence is
    known still live (_live_evidence, rechecked as the caller, so run it as the user whose runs
    they are, and a holder its takeover journal kept still running), and when it cannot be read
    whole (a takeover journal with a link that cannot be read, evidence that is not a list) or
    hil_process, which rechecks it, cannot be loaded. A run whose receipt cannot be rechecked is
    the operator's to judge. OSError or TimeoutError when the log cannot be read or written."""
    reason = reason.strip() if isinstance(reason, str) else ''
    if not reason:
        raise LogRefused('a reason is required')
    if not isinstance(failure_id, str):
        raise LogRefused(f'not a failure id: {failure_id!r:.80}')
    try:
        _process()      # a recheck without it proves nothing: refused, never read as not live
    except Exception as e:
        raise LogRefused(f'its evidence cannot be rechecked: {e!r:.200}') from e

    def still_open(events):
        if failure_id in _closed_ids(events):
            how = next(e for e in events if e.get('event') in CLOSING_EVENTS and e.get('failure') == failure_id)
            raise LogRefused(f'{failure_id} is already closed ({how["event"]} {how.get("at")})')
        failed = [e for e in events if e.get('event') == 'failed' and e.get('id') == failure_id]
        if not failed:
            raise LogRefused(f'no failure {failure_id} in {_log_name(board)}')
        return failed
    live = []
    for f in still_open(cleanup_events(board)):
        found = _failure_evidence(f)
        if found is None:
            raise LogRefused(f'{failure_id} stays open: its takeover journal cannot be read, so neither can all of '
                             f'its evidence')
        receipts, retained, held = found
        if not all(isinstance(x, list) for x in receipts + retained):
            raise LogRefused(f'{failure_id} stays open: its evidence is malformed')
        live += _live_evidence(sum(receipts, []), sum(retained, []))
        live += [f'takeover journal holder pid {pid} running' for pid in _live_survivors({'survivors': held})]
    if live:
        raise LogRefused(f'{failure_id} stays open: its evidence is still live: {"; ".join(live)}')
    return _log_append(board, {'event': 'acknowledged', 'failure': failure_id, 'reason': reason,
                               'pid': os.getpid()}, check=still_open)


def _live_evidence(receipts, retained) -> list:
    """What of a failure's evidence is known still live, as text: a receipt rechecking pending (as
    the caller), a survivor it names alive (with its recorded start time, when one is), a retained
    session not closed whose process lives on with its pid and start time. Evidence that cannot be
    rechecked is not known live."""
    live = [f'{_receipt_text(r)} pending' for r, state in zip(receipts, _rechecks(receipts)) if state == 'pending']
    live += [f'{_receipt_text(r)}: pids {pids} alive' for r in receipts if (pids := _live_survivors(r))]
    return live + [f'retained session {s!r:.120} running' for s in retained if _session_running(s)]


def _live_survivors(receipt) -> list:
    """The pids among the receipt's `survivors` still alive: a list of pids or {pid, start} (hil_reserve's), or
    hil_process's survey {processes: [{pid, ...}], unreadable: [pid]}, of which only `processes` were its
    descendants: `unreadable` lists every /proc entry it could not read, its own or not."""
    survivors = receipt.get('survivors') if isinstance(receipt, dict) else None
    if isinstance(survivors, dict):
        survivors = survivors.get('processes')
    live = []
    for s in survivors if isinstance(survivors, list) else []:
        pid, start = (s.get('pid'), s.get('start')) if isinstance(s, dict) else (s, None)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and (now := proc_start(pid)) is not None \
                and (start is None or str(start) == now):
            live.append(pid)
    return live


def _session_running(receipt) -> bool:
    """Whether the retained session `receipt` (its path, or the record kept in its place) is not
    closed and a process with its pid and start time lives (hil_openocd_esp_receipt's `running`)."""
    rec = _read_session_receipt(receipt)[0] if isinstance(receipt, str) else receipt
    if not isinstance(rec, dict) or rec.get('closed') is not False:
        return False
    pid = rec.get('pid')
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and str(rec.get('startTime')).isdigit() and proc_start(pid) == str(rec['startTime']))


def compact_cleanup_log(board) -> dict:
    """Rewrite the board's cleanup log without its closed failures (`hil_lock.py compact`): each
    `failed` event a `restored` or `acknowledged` event names, and every event naming it. Every
    other event stays as written (open failures, events of another kind, a closing event naming no
    failure in the log, a closed failure whose takeover journal is still on disk unchanged, with what
    closed it), and a torn last line goes as the next append would cut it. Under the
    log's mutex, as the log's owner or root: a new file, the log's mode, owner and group kept, is
    written and fsynced, renamed over the log and the dir fsynced, so the only copy is never
    truncated and the mutex is untouched; a failed step leaves the log as it was. Nothing is
    rewritten when nothing is closed. LogRefused, nothing changed, for a whole line that is not a
    JSON object, a log of another owner or, but as root, of a group the caller is not in; OSError
    or TimeoutError when it cannot be done.
    Returns {'dropped': events dropped, 'kept': events kept}."""
    with contextlib.ExitStack() as stack:
        try:
            dfd = stack.enter_context(_log_locked(board, create=False))
            fd = _open_file(dfd, _log_name(board), os.O_RDONLY)
        except FileNotFoundError:
            return {'dropped': 0, 'kept': 0}
        with os.fdopen(fd, 'rb') as f:
            st, data = os.fstat(f.fileno()), f.read()
        return _compact(board, dfd, st, data)


def _compact(board, dfd, st, data) -> dict:
    name = _log_name(board)
    lines = data[:data.rfind(b'\n') + 1].splitlines(keepends=True)
    events = []
    for n, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            raise LogRefused(f'{name} line {n} is not a JSON object, not compacted: {line[:80]!r}')
        events.append(event)

    def names(e):
        """The failure id `e` names: a failed event's own, a closing event's failure."""
        key = 'id' if e.get('event') == 'failed' else 'failure' if e.get('event') in CLOSING_EVENTS else None
        return e[key] if key and isinstance(e.get(key), str) else None
    closed = _closed_ids(events) & {names(e) for e in events if e.get('event') == 'failed'}
    if closed and (raw := _read_journal(board)[0]) is not None:
        # a journal this user could not unlink is logged once by its digest (_logged_journal): kept while it stays
        digest = hashlib.sha256(raw).hexdigest()
        closed -= {names(e) for e in events if e.get('event') == 'failed' and e.get('journalDigest') == digest}
    if not closed:
        return {'dropped': 0, 'kept': len(lines)}
    euid = os.geteuid()
    if euid not in (0, st.st_uid):
        raise LogRefused(f'{name} is owned by uid {st.st_uid}: compact it as that user or root')
    if euid and st.st_gid not in (os.getegid(), *os.getgroups()):
        raise LogRefused(f'{name} has group {st.st_gid}, which uid {euid} cannot give its replacement: compact it '
                         f'as root')
    keep = [line for line, e in zip(lines, events) if names(e) not in closed]
    _replace_file(dfd, name, b''.join(keep), stat.S_IMODE(st.st_mode), (st.st_uid, st.st_gid))
    return {'dropped': len(lines) - len(keep), 'kept': len(keep)}


def acquire_board_lock(board_name, reason=CI_REASON, **kw):
    """take_board_lock for the duration of a flash+test, or None when HIL_NO_BOARD_LOCK=1."""
    if os.environ.get('HIL_NO_BOARD_LOCK') == '1':
        return None  # user-authorized bypass — see the HIL contract
    return take_board_lock(board_name, reason, **kw)


# --- board task -----------------------------------------------------------------------------
TASK_POLL = 0.25    # s between takes while a board task waits out contention


def users_text(users) -> str:
    """What a busy check named, on one line: `pid N (cmd)` for hil_flash.probe_users' entries,
    str() of any other."""
    return ', '.join(f'pid {u["pid"]} ({u.get("cmd", "")})' if isinstance(u, dict) and 'pid' in u else str(u)
                     for u in users)


class BoardBusy(BoardLocked):
    """The task's busy check named live users of the board's probe or devices: its lock, taken
    for the check, is released again without a failure event. `users` are what the check named,
    `cleanup_failures` the take's open failures, for the caller's report."""

    def __init__(self, board, users, cleanup_failures):
        super().__init__(f'board busy: {board} in use by {users_text(users):.500}')
        self.board, self.users, self.cleanup_failures = board, users, cleanup_failures


def _exc_text(exc) -> str:
    return f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__


class BoardTask:
    """One task's lifecycle on one board, as a context manager that takes nothing until entered:
    a protected take (`record_fields` added to the record; the take's own fields and marks, and
    `protected`, `locks` and `role`, refused), waiting out ordinary contention for at most `wait`
    s (a RecoveryBlocked or LockUnavailable refuses at once), then `busy()`, the consumer's
    observational check that returns the live users of its probe or devices as a list, tuple or
    set, empty when idle, and raises when inconclusive, under the lock and before the body. A
    name refuses with BoardBusy, a raising check with its exception and any other result with
    TypeError, after the lock is released with no failure event. Each take attempt runs under an
    InterruptHold until its handle and failures are kept, so an interrupt there ends as that
    refusal.

    The body ends on its verdict, given once as its last hardware action: clean() clears the
    record and releases (a record that cannot be cleared ends through end_failed),
    restored(evidence=...) runs resolve_restored on the take's failures while still holding,
    then releases, and failed(...), no verdict, or a body that raised before its verdict end
    through end_failed, the exception named in the detail. A second verdict raises and ends the
    task as failed. Finalization runs under an InterruptHold: a SIGINT or SIGTERM whose handler is
    a Python callable is replayed once the task's state is complete and then takes precedence
    over the body's exception, kept as its __context__, unless that is itself an interrupt; the
    first interrupt caught while finishing wins over later ones (a signal landing while the
    replay's own handler runs is outside that rule). Other finalization errors go to `errors` and never
    replace it. A handle that cannot be closed is kept, and a later __exit__ or finish() retries
    its close. With `allow_bypass`, HIL_NO_BOARD_LOCK=1 takes no flock (acquire_board_lock): the
    busy check still runs, a failed cleanup is logged with log_cleanup_failure and a restoration
    resolves nothing; without it the variable is ignored.

    `verdict(task, exc)`, when given, is the consumer's verdict collector: called once, at the
    end of an admitted task whose body gave no verdict, under finalization's InterruptHold and
    before its steps, with the body's exception (else an interrupt that cut an earlier attempt
    short) as `exc`, it gives the verdict from the consumer's own ledger through clean(),
    restored() or failed(), and never suppresses the exception. A collector that raises (an
    interrupt included, where no hold could be installed) or gives no verdict ends the task as
    failed, any failed verdict it gave keeping its evidence, with an unrecheckable marker in its
    receipts and never cleanly; its interrupt is raised after finalization. Hardware work stays in
    the body: the collector only reads the ledger.

    Limit: a signal CPython delivers as __exit__ or finish() is entered unwinds before it runs,
    leaving the flock held while the process lives and the protected record for the next take to
    log found-left. A worker that catches that interrupt and goes on calls finish(e) while `ended`
    is false (a task without a verdict or collector then ends as failed), or ends before it
    continues. One landing as __enter__ returns, after admission, leaves an admitted task whose
    body never ran: finish() then ends it as an empty body, its collector included.

    `cleanup_failures` (the take's open failures, kept even when admission is refused), `errors`,
    `restore_notes` (resolve_restored's), `bypassed` and `released` (an owned handle was closed)
    stay readable after the task ends, for the caller's report. A caller decides on the outcome
    attributes, never on the text of `errors`:
    * `taken`: the task owned a lock handle; false for a refusal before the take and under the
      bypass.
    * `failure_logged`: None when the end logged no failure, else whether its failed event was
      appended (end_failed, or log_cleanup_failure under the bypass).
    * `ended_cleanly`: `ended`, with the lock ended as its verdict said: an owned handle closed,
      its record cleared (clear_record, or end_failed's clear for a failed verdict) and, for a
      failed verdict, its failure logged with its receipts and retained sessions (under the bypass
      too: there one that cannot be collected leaves it unlogged). `not ended_cleanly` alone is the
      lock's own failure for the caller's exit status; the verdict's failure stays the caller's.
      A clean or restored verdict whose record was not cleared ends through end_failed and never
      cleanly; a refusal before the take ends cleanly. Advisory notes do not change it: an
      interrupt hold that could not be installed, a restoration's notes (restore_notes) or
      errors, a deferred interrupt replayed afterwards.
    describe() puts these and `errors` in one line for the caller's report, and report() all of
    them as one dict."""

    def __init__(self, board, reason=CI_REASON, *, busy, wait=0, allow_bypass=False, verdict=None,
                 **record_fields):
        if not callable(busy):
            raise TypeError(f'busy must be callable, not {busy!r:.80}')
        if verdict is not None and not callable(verdict):
            raise TypeError(f'verdict must be callable or None, not {verdict!r:.80}')
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or wait < 0:
            raise ValueError(f'wait must be a finite number of seconds >= 0, not {wait!r:.80}')
        _own_fields(record_fields, CLEANUP_OWED, 'protected', 'locks', 'role')
        self.board, self.reason = board, reason
        self._busy, self._wait, self._allow_bypass, self._fields = busy, wait, allow_bypass, record_fields
        self._collector = verdict        # None once called: a retried _finish never reruns it
        self._collector_why = None       # why the collector's verdict does not stand, once called
        self.cleanup_failures, self.errors, self.restore_notes = [], [], []
        self.bypassed = self.released = self.taken = False
        self.failure_logged = None
        self._as_verdict = False         # the end's steps did what its verdict said
        self._fh = self._verdict = None
        self._given = []                 # every verdict call, in order
        # new, entering, active; then ending (its verdict's steps), closing (its handle) and done
        self._state = 'new'

    def __enter__(self):
        if self._state != 'new':
            raise RuntimeError(f'board task {self.board} is entered once')
        self._state = 'entering'
        refusal = None
        try:
            self._take()
            if self._fh is None:
                self.bypassed = True
                self.cleanup_failures = open_cleanup_failures(self.board)
            users = self._busy()
            if not isinstance(users, (list, tuple, set, frozenset)):
                raise TypeError(f'busy() returned {users!r:.80}, not a list, tuple or set of users')
            if users:
                refusal = BoardBusy(self.board, list(users), self.cleanup_failures)
        except BaseException as e:
            refusal = e
        if refusal is None:
            self._state = 'active'
            return self
        # inline, as in __exit__: entering a helper is itself a point where a signal handler runs
        pending = None
        while True:
            try:
                self._finish(None, refused=True)
                break
            except BaseException as e:
                if self._state != 'entering':
                    raise
                pending = pending or e
        raise pending or refusal

    def _take(self):
        take = acquire_board_lock if self._allow_bypass else take_board_lock
        deadline = time.monotonic() + self._wait
        while True:
            hold = self._interrupt_hold()      # per attempt, never over the sleep: Ctrl-C ends a wait
            try:
                try:
                    self._fh = take(self.board, self.reason, protected=True, **self._fields)
                    if self._fh is not None:
                        self.taken = True
                        self.cleanup_failures = list(self._fh.cleanup_failures)
                    return
                finally:
                    if hold is not None:
                        hold.release()
            except RecoveryBlocked:
                raise
            except BoardLocked:
                pause = deadline - time.monotonic()
                if pause <= 0:
                    raise
                time.sleep(min(TASK_POLL, pause))     # then one more take, even past the deadline

    def _interrupt_hold(self):
        try:
            return _process().InterruptHold()
        except Exception as e:
            if (why := f'interrupt hold: {e!r:.200}') not in self.errors:     # once, not per take attempt
                self.errors.append(why)
            return None

    @property
    def ended(self) -> bool:
        """Whether finalization completed with no owned handle left open: false before entry, inside
        the body, and while a handle that did not close is kept for a later __exit__."""
        return self._state == 'done'

    @property
    def ended_cleanly(self) -> bool:
        return self.ended and self._as_verdict

    def describe(self) -> str:
        """One line on how the lock ended: released or not (or bypassed), the failure logged or not
        with the cleanup log's path when one was due, and `errors`."""
        if self.bypassed:
            lock = 'lock bypassed (HIL_NO_BOARD_LOCK=1)'
        elif not self.taken:
            lock = 'lock not taken'
        else:
            lock = 'lock released' if self.released else 'lock NOT released'
        parts = [f'{self.board}: {lock}']
        if self.failure_logged is not None:
            log = os.path.join(BOARD_LOCK_DIR, self.board + CLEANUP_LOG)
            parts.append(f'failure {"logged" if self.failure_logged else "NOT logged"} in {log}')
        if self.errors:
            parts.append('errors: ' + '; '.join(self.errors))
        return ', '.join(parts)

    def clean(self) -> None:
        self._decide(('clean',))

    def restored(self, *, evidence) -> None:
        self._decide(('restored', evidence))

    def failed(self, *, detail, receipts=(), retained=()) -> None:
        self._decide(('failed', detail, receipts, retained))

    def _decide(self, verdict) -> None:
        if self._state != 'active':
            raise RuntimeError(f'board task {self.board}: a verdict belongs inside its with block')
        self._given.append(verdict)
        if self._verdict is None:
            self._verdict = verdict      # one store: an interrupt leaves it given or not
            return
        failed = [v for v in self._given if v[0] == 'failed']
        detail = 'cleanup verdict misuse: ' + ' then '.join(v[0] for v in self._given)
        detail += ''.join(f'; {v[1]}' for v in failed)
        # lazy: finalization consumes each collection once, under its own guard, however often this merges
        self._verdict = ('failed', detail, itertools.chain.from_iterable(v[2] for v in failed),
                         itertools.chain.from_iterable(v[3] for v in failed))
        raise RuntimeError(f'board task {self.board}: verdict {verdict[0]} after {self._given[0][0]}; '
                           f'it ends as a failed cleanup')

    def __exit__(self, exc_type, exc, traceback) -> bool:
        # an interrupt before _finish's hold went in did nothing yet: finish again. The first
        # interrupt wins, and a body's own interrupt is first
        start, pending = self._state, None
        while True:
            try:
                self._finish(exc if exc is not None else pending, refused=start == 'entering')
                break
            except BaseException as e:
                pending = pending or e
                if self._state != start or start not in ('entering', 'active'):
                    break
        if pending is not None and (exc is None or isinstance(exc, Exception) or isinstance(pending, Exception)):
            raise pending
        return False

    def finish(self, exc=None) -> None:
        """End the task now, as its __exit__ with `exc` would: what a worker calls when an
        interrupt cut its __exit__ short, or when an interrupt left __enter__ before admission
        returned (a refusal then). It does nothing once `ended`, and ends a task never entered
        as untouched. It never raises `exc`; when `exc` is an interrupt, one caught while
        finishing is dropped for the caller to re-raise its own."""
        if self._state == 'new':
            self._state = 'done'
            return
        self.__exit__(None if exc is None else type(exc), exc, None)

    def report(self) -> dict:
        """The outcome attributes, describe() and copies of `errors` and `restore_notes`, for a
        caller's report; reading it changes nothing."""
        return {'taken': self.taken, 'released': self.released, 'bypassed': self.bypassed, 'ended': self.ended,
                'endedCleanly': self.ended_cleanly, 'failureLogged': self.failure_logged,
                'errors': list(self.errors), 'describe': self.describe(), 'restoreNotes': list(self.restore_notes)}

    def _verdict_at_end(self, exc):
        verdict = self._verdict
        if self._collector_why is not None:
            verdict = self._collector_failure(verdict)
        if verdict is None:
            return ('failed', 'the task ended without a cleanup verdict'
                    + (f': {_exc_text(exc)}' if exc is not None else ''), (), ())
        if verdict[0] == 'failed' and exc is not None:
            return ('failed', f'{verdict[1]}; then {_exc_text(exc)}', *verdict[2:])
        return verdict

    def _collector_failure(self, given):
        """The failed verdict a collector that did not finish, or gave none, ends its task on."""
        why = self._collector_why
        if given is None:
            detail, receipts, retained = f'the verdict collector {why}', (), ()
        elif given[0] == 'failed':
            detail, receipts, retained = f'{given[1]}; then the verdict collector {why}', given[2], given[3]
        else:
            detail, receipts, retained = f'the verdict collector {why} after a {given[0]} verdict', (), ()
        return ('failed', detail, itertools.chain(receipts, [{'unrecheckable': f'the verdict collector {why}'}]),
                retained)

    def _finish(self, exc, refused=False) -> None:
        if self._state == 'done':
            return
        hold = self._interrupt_hold()
        interrupted = []

        def guard(what, step, default=None):
            return _guarded(step, what, self.errors.append, interrupted, default)
        try:
            if self._state == 'active' and not refused and self._collector is not None and self._verdict is None:
                collector, self._collector, self._collector_why = self._collector, None, 'did not finish'
                if guard('verdict collector', lambda: (collector(self, exc), True)[1]):
                    self._collector_why = None if self._verdict is not None else 'gave no verdict'
                    if self._collector_why:
                        self.errors.append('verdict collector: gave no verdict')
            if self._state in ('entering', 'active'):
                self._state = 'ending'
                self._end_on(guard, refused, exc)
                if self._collector_why is not None:
                    self._as_verdict = False
                self._state = 'closing'
        finally:
            # whatever cut the steps short (no hold to defer it), an owned handle is closed here
            try:
                fh = self._fh
                if fh is not None and not fh.closed:
                    guard('close', fh.close)
                self.released = fh is not None and fh.closed
                if fh is None or fh.closed:   # a handle still open is kept for a later __exit__ to close
                    self._fh = None
                    self._state = 'done'
            finally:
                if hold is not None:
                    try:
                        hold.release()
                    except BaseException as e:     # the replay: an interrupt already kept stays first
                        interrupted.append(e)
        if interrupted:
            raise interrupted[0]

    def _end_on(self, guard, refused, exc) -> None:
        verdict = ('refused',) if refused else guard('verdict', lambda: self._verdict_at_end(exc),
                                                     ('failed', 'the task ended without a cleanup verdict', (), ()))
        kind, fh = verdict[0], self._fh
        if fh is None:
            if self.bypassed and kind == 'failed':
                self.failure_logged = guard('cleanup log not written', lambda: log_cleanup_failure(
                    self.board, self.reason, detail=verdict[1], receipts=verdict[2], retained=verdict[3])) is not None
                self._as_verdict = self.failure_logged
            else:
                if self.bypassed and kind == 'restored' and self.cleanup_failures:
                    self.restore_notes.append('nothing resolved: the board lock was bypassed (HIL_NO_BOARD_LOCK=1)')
                self._as_verdict = True
        elif kind == 'failed':
            self._as_verdict = self._end_failed(guard, *verdict[1:])
        else:
            if kind == 'restored':
                self.restore_notes.extend(guard('restoration', lambda: resolve_restored(
                    self.board, self.cleanup_failures, evidence=verdict[1]), []))
            cleared = guard('record not cleared', lambda: clear_record(fh))
            if cleared is False:
                self.errors.append('record not cleared')
            if not cleared and kind != 'refused':     # a refusal touched nothing: the next take logs the record
                self._end_failed(guard, f'record not cleared after a {kind} verdict', (), ())
            self._as_verdict = cleared is True

    def _end_failed(self, guard, detail, receipts, retained) -> bool:
        """end_failed on the task's handle; whether it logged the failure with its evidence and cleared the record."""
        ended = {}
        errors = guard('end_failed', lambda: _end_failed_each({self.board: self._fh}, self.reason, detail, receipts,
                                                              retained, {}, ended), {})
        self.errors.extend(errors.get(self.board, []))
        outcome = ended.get(self.board, {})
        self.failure_logged = outcome.get('logged', False)
        return self.failure_logged and outcome.get('cleared', False) and outcome.get('evidence', False)


board_task = BoardTask


SESSION_MUTEX = '.session.mutex'   # <lock>.session.mutex: never unlinked; not '.lock', a board's suffix


class SessionBusy(RuntimeError):
    """Another command under the board's hold runs now: its session mutex is held."""


@contextlib.contextmanager
def session_mutex(lock):
    """Serialize the commands run under one manual hold of `lock`, whatever tree or OS user runs
    them: a nonblocking flock on <lock>.session.mutex in BOARD_LOCK_DIR (0666, opened as every
    shared file there is, never replaced or unlinked, so every taker locks the same inode), held
    for the with block. A command takes it before validating the caller's hold, so two commands
    cannot both pass that check. SessionBusy, naming the lock, at once when another holds it;
    LockUnavailable when the dir or file cannot be used, or hil_process cannot be loaded. It is
    no board lock: lock_names and `release --all` never list it.

    SIGINT and SIGTERM are deferred (hil_process.InterruptHold) from before its first fd opens
    until both are closed, the with block alone excepted, so a caller catching the interrupt
    keeps no flock; one deferred is raised after the closes, ahead of a close's error. The one
    gap: a handler the with block installed and left runs until the cleanup's rearm loop puts
    the deferring one back (InterruptHold.rearm, retried until it completes, cannot defer a
    signal already pending as it starts). One interrupt there is raised after the closes; one
    more on the loop's back-edge ends the loop with the fds closed undeferred, so a third after
    an fd is popped can leave that fd, and its flock, open until the process exits. That handler
    is replaced at exit by the one found at entry."""
    _check_name(lock)
    try:
        process = _process()        # before any fd: a helper that cannot load acquires nothing
    except Exception as e:
        raise LockUnavailable(f'session mutex unavailable: interrupt hold: {e!r:.200}') from e
    hold = process.InterruptHold()
    fds, raised, failed = [], None, None      # fds: the dir's, then the mutex's
    try:
        try:
            fds.append(_open_dir(create=True))
            fds.append(_open_file(fds[0], lock + SESSION_MUTEX, os.O_RDWR, create=True))
        except OSError as e:
            raise _unavailable(e) from e
        try:
            fcntl.flock(fds[1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SessionBusy(f'session busy: another command under the hold of {lock} is running '
                              f'({lock}{SESSION_MUTEX} held)') from None
        except OSError as e:
            raise _unavailable(e) from e
        hold.deliver()
        yield
    except BaseException as e:      # no call in here: no interrupt check before the finally's first store
        raised = e
        raise
    finally:
        hold.deferring = True       # first: an attribute store is no interrupt check, a call's entry is
        rearmed = None
        try:
            while True:             # a handler the with block installed defers too, before an fd goes
                try:
                    hold.rearm()
                    break
                except BaseException as e:
                    rearmed = rearmed or e      # the with block's handler ran first: the first is raised at the end
        finally:                    # the fds go even when a signal lands on the loop's back-edge
            while fds:
                try:
                    os.close(fds.pop())       # once each: a failed close frees the fd number all the same
                except Exception as e:
                    failed = failed or e
            hold.release()
        if rearmed is not None:
            raise rearmed
        if failed is not None and (raised is None or isinstance(raised, Exception)):     # an interrupt wins
            raise failed


# --- holder identity ------------------------------------------------------------------------
def proc_start(pid):
    """The process start time (clock ticks since boot, proc(5) field 22, as recorded), or
    None when it is gone."""
    try:
        with open(f'/proc/{pid}/stat', errors='replace') as f:   # comm may be cut mid-UTF-8 character
            return f.read().rpartition(')')[2].split()[19]
    except (OSError, IndexError):
        return None


def holder_fields(token: str, locks) -> dict:
    """The fields a token-released holder records on every board it holds. Raises when its
    start time cannot be read or the lock set is not a non-empty set of valid names: a record
    missing them could never be released by token."""
    locks = list(locks)
    if not locks or len(set(locks)) != len(locks):
        raise ValueError(f'lock set must be non-empty and unique: {locks}')
    for b in locks:
        _check_name(b)
    start = proc_start(os.getpid())
    if start is None:
        raise OSError(errno.ESRCH, 'own start time unreadable')
    return {'token': token, 'start': start, 'locks': locks}


def _holds_flock(pid, path) -> bool:
    """Whether an open file description of `pid` on `path`'s inode holds a flock: the
    fdinfo `lock:` line is listed per file description, so a process that merely has the
    file open, or whose flock is on another description, does not count."""
    st = os.stat(path, follow_symlinks=False)
    fd_dir = f'/proc/{pid}/fd'
    for fd in os.listdir(fd_dir):
        try:
            fst = os.stat(f'{fd_dir}/{fd}')
            if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                continue
            with open(f'/proc/{pid}/fdinfo/{fd}') as f:
                if any(line.startswith('lock:') and ' FLOCK ' in line for line in f):
                    return True
        except FileNotFoundError:
            continue
    return False


def _pidfd_signal(pid, start, sig, still_valid) -> None:
    """Deliver `sig` to the process that is `pid` started at `start`, through a pidfd so a
    recycled pid cannot receive it; `still_valid()` is re-checked right before delivery.
    Raises ReleaseRefused naming the step that failed."""
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ReleaseRefused('not signalled: identity-bound signalling (pidfd) is unavailable')
    try:
        pidfd = os.pidfd_open(pid)
    except OSError as e:
        raise ReleaseRefused(f'not signalled: holder pid {pid} cannot be opened: {e}')
    try:
        live = proc_start(pid)
        if live is None or live != start:
            raise ReleaseRefused(f'not signalled: pid {pid} is not the recorded holder (start {live} != {start})')
        try:
            valid = still_valid()
        except OSError:
            valid = False
        if not valid:
            raise ReleaseRefused(f'not signalled: the record of holder pid {pid} changed')
        try:
            signal.pidfd_send_signal(pidfd, sig)
        except OSError as e:
            raise ReleaseRefused(f'delivery failed: holder pid {pid}: {e}')
    finally:
        os.close(pidfd)


def _exited(pid, start) -> bool:
    """Whether `pid` started at `start` has only zombie threads: do_exit() drops a task's files
    before exit_notify() makes it one (kernel/exit.c v6.12 :934, :959), and its fd dir is then
    EACCES. A zombie creates no thread, so an unchanged task set around the scan has no live one."""
    def state(t):
        with open(f'/proc/{pid}/task/{t}/stat', errors='replace') as f:
            return f.read().rpartition(')')[2].split()[0]
    try:
        tasks = sorted(os.listdir(f'/proc/{pid}/task'))
        return (bool(tasks) and all(state(t) in ('Z', 'X') for t in tasks)
                and sorted(os.listdir(f'/proc/{pid}/task')) == tasks and proc_start(pid) == start)
    except (OSError, IndexError):
        return False


def _keeps_flock(pid, start, board) -> bool:
    """Whether the holder `pid` started at `start` still holds `board`'s flock: it clears its
    records before it exits and the kernel drops its flocks only then."""
    try:
        return proc_start(pid) == start and _holds_flock(pid, lock_path(board))
    except OSError:
        return not _exited(pid, start)     # otherwise unproven either way: rechecked until the deadline


def _wait_cleared(boards, pid, start, deadline) -> list:
    """The boards whose record or journal is still there at the deadline, or whose flock the
    holder `pid` started at `start` still holds."""
    while True:
        left = [b for b in boards if record_state(b)[0] not in ('absent', 'empty') or journal_state(b) is not None
                or _keeps_flock(pid, start, b)]
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.05)


def _process():
    """hil_process, loaded on first use: only a release predicate and end_failed need it."""
    return hil_load.load_file('skills/hil/scripts/hil_process.py')


def _receipt_text(r) -> str:
    if isinstance(r, dict) and 'foreign' in r:
        owner = f'uid {r["uid"]}' if isinstance(r.get('uid'), int) else 'its owner'
        return f'operations in {r["foreign"]!r:.200} (uid {os.geteuid()} cannot recheck them: rerun as {owner} or root)'
    if isinstance(r, dict) and 'unrecheckable' in r:     # a run without a receipt: its command or why
        what = r['unrecheckable']
        what = ' '.join(map(str, what)) if isinstance(what, list) else str(what)
        alive = f', surviving pids {r["survivors"]!r:.80}' if r.get('survivors') else ''
        token = f' {r["token"]!r:.40}' if 'token' in r else ''
        return f'unrecheckable run{token} ({what:.200}{alive})'
    sup = r.get('supervisor') if isinstance(r, dict) else None
    pid = sup.get('pid') if isinstance(sup, dict) else None
    return f'receipt {r.get("token")} (supervisor pid {pid})' if isinstance(r, dict) else f'receipt {r!r:.80}'


def _release_receipts(recs, key='receipts') -> list:
    """The release predicate of a cleanup holder's records `recs`: the run_tracked receipts in
    their `receipts`, every one of which must recheck `done`, or with key='retained' the
    sessions a provider left running, every one of which must read ended (retained_state);
    none when they carry no such key (an older helper's takeover or hil_reserve holder). ValueError when
    the records disagree or it is not a list."""
    named = [(key in r, r.get(key)) for r in recs]
    if any(n != named[0] for n in named):
        raise ValueError(f'its records disagree on their {key}')
    present, receipts = named[0] if named else (False, None)
    if not present:
        return []
    if not isinstance(receipts, list):
        raise ValueError(f'its {key} {receipts!r:.80} are not a list')
    return receipts


def _read_session_receipt(path) -> tuple:
    """(the JSON at `path`, '') or (None, why it cannot be read). Non-blocking: a FIFO in its place
    must not hang a release."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)) as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None, 'not a regular file'
            return json.load(f), ''
    except (OSError, ValueError) as e:
        return None, f'unreadable ({e!r:.80})'


def retained_state(receipt) -> str:
    """'ended' once the session receipt at path `receipt` (esp_session's receipt format) is
    hil_openocd_esp's, whole and closed after its reviewed end, as `hil_openocd_esp.py end` leaves
    it on exit 0 (`tool` 'hil_openocd_esp', its `pid` and `startTime`, `closed` true, `cleanup`
    'done'), else why not. A receipt kept as a record, not a path, is never reread, so never
    ended: its holder is not released by token."""
    if not isinstance(receipt, str):
        return 'not a receipt path, so never proven ended'
    rec, why = _read_session_receipt(receipt)
    if why:
        return why
    if not isinstance(rec, dict) or rec.get('tool') != 'hil_openocd_esp':
        return 'not a hil_openocd_esp session receipt'
    pid = rec.get('pid')
    # as hil_openocd_esp_receipt.session_state reads a receipt, which calls one without these unreadable
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and str(rec.get('startTime')).isdigit()
            and isinstance(rec.get('closed'), bool)):
        return 'not a whole session receipt (pid, startTime, closed)'
    if not rec['closed']:
        return 'not closed'
    return 'ended' if rec.get('cleanup') == 'done' else f'closed with cleanup {rec.get("cleanup")!r:.40}'


def _rechecks(receipts) -> list:
    states = []
    for r in receipts:
        try:
            states.append(_process().recheck(r))
        except Exception as e:       # a malformed receipt or a missing hil_process proves nothing
            states.append(f'unknown ({e!r:.80})')
    return states


def _holder_ids(pid, start):
    """(euid, egid, groups) of the holder `pid` started at `start` when we are root and it is
    not, else None: recheck trusts only files of the evaluating euid (hil_process.trusted_dir),
    so the holder's receipts are rechecked as the holder."""
    if os.geteuid() != 0:
        return None
    with open(f'/proc/{pid}/status', errors='replace') as f:   # Name: is comm, maybe cut mid-UTF-8 character
        fields = {k: v.split() for k, _, v in (line.partition(':') for line in f)}
    if proc_start(pid) != start:
        raise OSError(errno.ESRCH, f'pid {pid} is not the recorded holder')
    uid, gid = int(fields['Uid'][1]), int(fields['Gid'][1])
    return None if uid == 0 else (uid, gid, [int(g) for g in fields.get('Groups', [])])


def _rechecks_as(ids, receipts) -> list:
    """_rechecks in a child that took the holder's ids, report and reap bounded by RELEASE_WAIT.
    The child is signalled and reaped only through a pidfd opened while it still waits for the
    go byte, so it cannot have exited and its pid cannot name another process (SIGCHLD ignored,
    or another reaper). One that does not exit within the bound is abandoned, its states unknown."""
    uid, gid, groups = ids
    if not hasattr(os, 'pidfd_open') or not hasattr(os, 'P_PIDFD'):
        return [f'unknown (not rechecked as uid {uid}: no pidfd)'] * len(receipts)
    deadline = time.monotonic() + RELEASE_WAIT
    fds, reports, pidfd, msg, reaped = [], None, None, None, False
    try:
        process = _process()      # loaded before the fork: the child opens no module file as the holder
        fds.extend(os.pipe())     # report: read, write
        fds.extend(os.pipe())     # go: read, write
        child = os.fork()
        if child == 0:
            try:
                os.close(fds[0])
                os.close(fds[3])
                if os.read(fds[2], 1) == b'1':
                    os.setgroups(groups)
                    os.setresgid(gid, gid, gid)
                    os.setresuid(uid, uid, uid)
                    process._send(fds[1], {'states': _rechecks(receipts)})
            finally:
                os._exit(0)
        # failing, as an interrupt before it, leaves the child to exit through the closed go pipe, unreaped
        pidfd = os.pidfd_open(child)
        os.write(fds[3], b'1')
        for fd in fds[1:]:
            fds.remove(fd)        # first: an fd is closed at most once, whatever interrupts
            os.close(fd)
        reports = process._Reports(fds.pop())     # owns and closes it from here on
        msg = reports.next(deadline)
    except OSError as e:
        return [f'unknown (not rechecked as uid {uid}: {e!r:.80})'] * len(receipts)
    finally:
        try:
            while fds:            # a child still waiting for its go byte reads EOF and exits
                try:
                    os.close(fds.pop())
                except OSError:
                    pass
            if reports is not None:
                reports.close()
        finally:
            if pidfd is not None:
                reaped = _kill_and_reap(pidfd, deadline)
    if not reaped:
        return [f'unknown (the recheck child as uid {uid} did not exit within {RELEASE_WAIT}s; '
                f'abandoned)'] * len(receipts)
    states = msg.get('states') if msg else None
    if not isinstance(states, list) or len(states) != len(receipts):
        return [f'unknown (not rechecked as uid {uid})'] * len(receipts)
    return states


def _kill_and_reap(pidfd, deadline) -> bool:
    """SIGKILL the child behind `pidfd` and reap it by 'deadline'; closes `pidfd`. False when it
    has not exited by then (a D-state wait): it is abandoned."""
    try:
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except OSError:
            pass                  # it already exited
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if not poller.poll(max(0, math.ceil((deadline - time.monotonic()) * 1000))):
            return False
        try:
            os.waitid(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG)
        except ChildProcessError:
            pass                  # reaped already: SIGCHLD ignored
        return True
    finally:
        os.close(pidfd)


def _release_blocker(receipts, holder=None, retained=()) -> str:
    """Why the release predicate is false now, or '' when every one of `receipts` rechecks `done`
    and every one of `retained` reads ended. `holder`, (pid, start), is whose ownership context
    rechecks the receipts (_holder_ids)."""
    sessions = [f'{r!r:.120} {s}' for r, s in ((r, retained_state(r)) for r in retained) if s != 'ended']
    why = (f'its retained sessions are not proven ended (end each with `hil_openocd_esp.py end --receipt`, '
           f'exit 0): {"; ".join(sessions)}') if sessions else ''
    return '; '.join(w for w in (why, _receipts_blocker(receipts, holder)) if w)


def _receipts_blocker(receipts, holder) -> str:
    if not receipts:
        return ''
    try:
        ids = _holder_ids(*holder) if holder else None
    except (OSError, KeyError, IndexError, ValueError) as e:
        states = [f'unknown (the holder\'s ids: {e!r:.80})'] * len(receipts)
    else:
        states = _rechecks(receipts) if ids is None else _rechecks_as(ids, receipts)
    runs = open_runs(receipts, states)
    return f'its operations are not proven ended: {"; ".join(runs)}' if runs else ''


def open_runs(receipts, states) -> list:
    """Each of `receipts` whose state (its `states` entry) is not done, as text with that state."""
    return [f'{_receipt_text(r)} {s}' for r, s in zip(receipts, states) if s != 'done']


def release_token(token: str):
    """End the token's holder: its token, pid, start, role, lock set and release predicate agree
    on every board that carries the token, it is alive as recorded, its predicate holds,
    rechecked as the holder (_holder_ids), and SIGUSR1 reaches it through a pidfd; returns
    (pid, boards) once its records and flocks are gone. A dead holder is refused: its flocks
    are free, and the next task takes its boards."""
    found = {}
    for b in lock_names():
        rec = read_record(b)
        if isinstance(rec, dict) and rec.get('token') == token:
            found[b] = rec
    if not found:
        raise ReleaseRefused(f'not signalled: no holder record carries token {token}')
    first = next(iter(found.values()))
    pid, start, lockset = first.get('pid'), first.get('start'), first.get('locks')
    if not isinstance(pid, int) or pid <= 0 or start is None or not isinstance(lockset, list):
        raise ReleaseRefused(f'not signalled: token {token}: the record on {next(iter(found))} lacks pid/start/locks')
    for b, rec in found.items():
        if any(rec.get(k) != first.get(k) for k in ('pid', 'start', 'locks')):
            raise ReleaseRefused(f'not signalled: token {token}: the record on {b} disagrees with the others')
        if not is_cleanup_holder(rec):
            raise ReleaseRefused(f'not signalled: holder pid {pid} is not a cleanup holder on {b}: '
                                 f'still mutating or already clean')
    if sorted(lockset) != sorted(found):
        raise ReleaseRefused(f'not signalled: token {token}: record lock set {sorted(lockset)} '
                             f'!= files carrying it {sorted(found)}')
    try:
        receipts, retained = _release_receipts(found.values()), _release_receipts(found.values(), 'retained')
    except ValueError as e:
        raise ReleaseRefused(f'not signalled: token {token}: its release predicate cannot be evaluated: {e}')
    live = proc_start(pid)
    if live != start:
        gone = (f'holder pid {pid} of token {token} is gone' if live is None else
                f'pid {pid} is not the recorded holder (start {live} != {start})')
        raise ReleaseRefused(f'not signalled: {gone}; its flocks are free and the next task takes '
                             f'{", ".join(sorted(found))}')
    why = _release_blocker(receipts, (pid, start), retained)
    if why:
        raise ReleaseRefused(f'not signalled: holder pid {pid} of token {token} keeps '
                             f'{", ".join(sorted(found))}: {why}')

    def still_valid():
        return all(read_record(b) == found[b] and _holds_flock(pid, lock_path(b)) for b in found)

    try:
        proven = still_valid()
    except OSError as e:
        raise ReleaseRefused(f'not signalled: cannot prove holder pid {pid} holds its flocks: {e}')
    if not proven:
        raise ReleaseRefused(f'not signalled: holder pid {pid} does not hold the flocks its records name')
    _pidfd_signal(pid, start, signal.SIGUSR1, still_valid)
    left = _wait_cleared(sorted(found), pid, start, time.monotonic() + RELEASE_WAIT)
    if left:
        why = _release_blocker(receipts, (pid, start), retained)   # it held above, but the holder's own recheck decides
        raise ReleaseRefused(f'signalled, but holder pid {pid} left its records or flocks on {", ".join(left)}'
                             + (f': {why}' if why else ''))
    return pid, sorted(found)


# --- pending sessions -----------------------------------------------------------------------
PENDING = '-pending.json'           # <lock>.<kind>-pending.json, beside the board's lock file
PENDING_MUTEX = '.mutex'            # <record>.mutex: never replaced or removed; not '.lock', a board's suffix
PENDING_CLEARED = '.cleared.jsonl'  # <record>.cleared.jsonl: every clear made after a reviewed recovery


class PendingRefused(RuntimeError):
    """A pending-session record was kept as it is: it cannot be read, or lists a session its
    consumer has not proven ended."""


def _sessions_ok(sessions) -> bool:
    return isinstance(sessions, list) and all(isinstance(e, dict) and isinstance(e.get('receipt'), (str, dict))
                                              for e in sessions)


class PendingSessions(os.PathLike):
    """A board's pending-session record, <lock>.<kind>-pending.json in BOARD_LOCK_DIR: the
    sessions a consumer (`kind`, e.g. `tinyuf2-openocd`) left on the board without proving them
    ended, so a later invocation, whatever its run dir or project, refuses until they are.
    `{"sessions": [...]}`, each session a dict whose `receipt` (a path or a record) identifies
    it; what a receipt means, when its session counts as ended and what a refusal does stay the
    consumer's. Every read, write and removal holds an flock on <record>.mutex (0666, never
    removed), so the board lock need not be held; the record is replaced atomically and fsynced
    with its dir (_replace_file), 0644. A record that cannot be parsed, or lists a malformed
    session, is unreadable: kept as it is, and it refuses."""

    def __init__(self, lock, kind):
        _check_name(lock)
        _check_name(kind)
        self.lock, self.suffix = lock, f'.{kind}{PENDING}'
        self.name = lock + self.suffix

    @property
    def path(self) -> str:
        return os.path.join(BOARD_LOCK_DIR, self.name)

    def __fspath__(self):
        return self.path

    def __str__(self):
        return self.path

    @contextlib.contextmanager
    def _locked(self):
        """The mutex held; yields the lock dir's fd, created on first use."""
        dfd = _open_dir(create=True)
        try:
            fd = _open_file(dfd, self.name + PENDING_MUTEX, os.O_RDWR, create=True)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield dfd
            finally:
                os.close(fd)
        finally:
            os.close(dfd)

    def _read(self):
        try:
            data = _read_file(self.lock, self.suffix)
            rec = data is not None and json.loads(data)
        except (OSError, ValueError):
            return None
        if data is None:
            return []
        sessions = rec.get('sessions') if isinstance(rec, dict) else None
        return sessions if _sessions_ok(sessions) else None

    def read(self):
        """The sessions it lists, [] without a record; None when it is unreadable."""
        with self._locked():
            return self._read()

    def add(self, sessions) -> list:
        """List `sessions` too, each one whose receipt no listed session has, and return what the
        record lists. ValueError for a malformed session and PendingRefused for an unreadable
        record, both before anything is written; OSError when the record cannot be replaced (in
        a sticky dir, another OS user's), the previous one kept."""
        sessions = list(sessions)
        if not _sessions_ok(sessions):
            raise ValueError(f'a pending session is a dict with a receipt: {sessions!r:.200}')
        with self._locked() as dfd:
            merged = self._read()
            if merged is None:
                raise PendingRefused(f'{self.path} is unreadable: kept as it is')
            listed = len(merged)
            for e in sessions:
                if all(e['receipt'] != m['receipt'] for m in merged):
                    merged.append(e)
            if len(merged) > listed:
                _replace_file(dfd, self.name, json.dumps({'sessions': merged}, indent=1).encode())
            return merged

    def clear(self, ended, note=None, **evidence) -> dict:
        """Remove the record once `ended(session)`, the consumer's reading of its receipt, is
        True for every session it lists (one that raises is not ended); or, given the `note` of a
        separately reviewed recovery, whatever it holds, once the note, `evidence` (e.g. the
        hold that proved it) and the record's sessions, or an unreadable record's text, are
        appended to <record>.cleared.jsonl and fsynced. PendingRefused otherwise, naming the
        receipts, the record kept; OSError when it cannot be logged or removed (in a sticky dir,
        another OS user's), the record kept. Returns {mark, sessions, notEnded} plus what was
        logged."""
        with self._locked() as dfd:
            sessions = self._read()
            if sessions is None and note is None:
                raise PendingRefused(f'{self.path} is unreadable: a session may still be pending; it is cleared only '
                                     f'with the note of a separately reviewed recovery')
            left = [e['receipt'] for e in sessions or [] if not _ended(ended, e)]
            if left and note is None:
                raise PendingRefused(f'{self.path}: sessions {left} are not ended')
            cleared = {'mark': self.path, 'sessions': sessions, 'notEnded': left}
            if note is not None:
                cleared.update(note=note, **evidence)
                raw = _read_file(self.lock, self.suffix) if sessions is None else None
                if raw:
                    cleared['unreadable'] = raw.decode(errors='replace')
                line = json.dumps(dict(cleared, at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))) + '\n'
                fd = _open_file(dfd, self.name + PENDING_CLEARED, os.O_RDWR | os.O_APPEND, create=True)
                try:
                    os.ftruncate(fd, _whole_lines(fd))     # an earlier append's torn line goes
                    _write_all(fd, line.encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(dfd)                          # the log's entry durable before the record goes
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.name, dir_fd=dfd)     # a removal a crash loses only refuses again
            return cleared

    def cleared(self) -> list:
        """Every entry <record>.cleared.jsonl holds, oldest first; a line that is not a JSON
        object, or a last one without its newline (an append that failed), is skipped."""
        with self._locked():
            data = _read_file(self.lock, self.suffix + PENDING_CLEARED) or b''
        found = []
        for line in data[:data.rfind(b'\n') + 1].splitlines():
            with contextlib.suppress(ValueError):
                entry = json.loads(line)
                if isinstance(entry, dict):
                    found.append(entry)
        return found


def _whole_lines(fd) -> int:
    """The size of `fd`'s file up to and including its last newline."""
    end = os.fstat(fd).st_size
    while end > 0:
        start = max(0, end - 4096)
        chunk = os.pread(fd, end - start, start)
        if b'\n' in chunk:
            return start + chunk.rindex(b'\n') + 1
        end = start
    return 0


def _ended(ended, session) -> bool:
    try:
        return ended(session) is True
    except Exception:
        return False


# --- operator CLI (hold/release/status) ------------------------------------
PROC_LOCKS = '/proc/locks'


def flock_observed(board: str):
    """Whether a flock is held on the board's lock file, read from /proc/locks without taking it: True or False, None
    when it cannot be read (no lock file, no /proc/locks). Lines per fs/locks.c lock_get_status (v6.12): `<id>: FLOCK
    ADVISORY WRITE <pid> <major hex>:<minor hex>:<inode> 0 EOF`, a blocked request's id followed by `->`. A holder in
    another pid namespace, or a dev number the file system reports otherwise to stat, reads free: only an
    observation, the flock itself decides."""
    try:
        _check_name(board)
        st = os.stat(lock_path(board), follow_symlinks=False)
        with open(PROC_LOCKS) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    want = f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
    return any(len(t) > 5 and t[1] == 'FLOCK' and t[5] == want for t in (line.split() for line in lines))


def is_locked(board: str) -> bool:
    """True if the recorded holder process is still alive.

    Deliberately never touches the flock: even a momentary probe lock would
    make a concurrent acquirer's LOCK_NB attempt fail spuriously. The flock
    taken by acquirers themselves stays the only authority."""
    return _alive(read_record(board))


def holder_alive(record) -> bool:
    """is_locked on a record the caller already read, so one read decides both its owner and its liveness."""
    return _alive(record)


def _alive(info) -> bool:
    pid = info.get('pid') if isinstance(info, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive but owned by another user (e.g. the CI runner)
    return True


def cmd_hold(boards, reason):
    boards = list(dict.fromkeys(boards))   # a repeated name would conflict with its own flock
    # No pre-check: the holder's own LOCK_NB flock is the only authority, since a recorded
    # pid may be stale or recycled. The holder signals success through this pipe because a
    # generic is_locked() poll would be fooled by a RIVAL invocation's flock — only the
    # holder knows whether it won every board. It keeps a won hold only once the parent acks
    # it, so a readiness landing as the parent's wait times out is never left running.
    r_fd, w_fd = os.pipe()
    ack_r, ack_w = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(w_fd)
        os.close(ack_r)
        os.waitpid(pid, 0)  # reap intermediate child
        ready, _, _ = select.select([r_fd], [], [], 10)
        msg = os.read(r_fd, 4096).decode(errors='replace') if ready else ''
        os.close(r_fd)
        if msg.startswith('1'):
            try:
                os.write(ack_w, b'1')
            except OSError:
                msg = '0holder exited before its readiness was acknowledged'
        os.close(ack_w)     # unacknowledged, the holder reads EOF and bows out
        if msg.startswith('1'):
            print(f'held: {", ".join(boards)}')
            return 0
        if msg[1:]:
            print(f'ERROR: {msg[1:]}', file=sys.stderr)
        print('ERROR: holder failed to acquire locks', file=sys.stderr)
        return 1
    # intermediate child: detach, then spawn the actual holder
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    # holder (grandchild): acquire all flocks, signal the parent, sleep until ended
    os.close(r_fd)
    os.close(ack_w)
    # Keep the pipes clear of fds 0-2: invoked with stdio closed, os.pipe() can land there
    # and the dup2 loop below would clobber them.
    w_fd, ack_r = (fcntl.fcntl(fd, fcntl.F_DUPFD, 3) if fd <= 2 else fd for fd in (w_fd, ack_r))
    # Detach stdio: a `hold` whose output is captured must see EOF when the front-end
    # exits — the immortal holder must not keep that pipe open.
    devnull = os.open(os.devnull, os.O_RDWR)
    for std_fd in (0, 1, 2):
        os.dup2(devnull, std_fd)
    if devnull > 2:
        os.close(devnull)
    if _starts_scoped():
        _exec_holder(boards, reason, w_fd, ack_r)
    _holder(boards, reason, w_fd, ack_r)


def _starts_scoped() -> bool:
    """Whether os.environ or the environment this process started with (/proc/self/environ: what a fork keeps
    whatever os.environ drops, and what hc reads) names HC_SCOPE; true when that is unreadable."""
    if HC_SCOPE in os.environ:
        return True
    try:
        with open('/proc/self/environ', 'rb') as f:
            return any(e.startswith(os.fsencode(HC_SCOPE) + b'=') for e in f.read().split(b'\0'))
    except OSError:
        return True


# a fresh interpreter's holder: argv is this file, BOARD_LOCK_DIR, the reason, the two pipe fds, the boards
_HOLDER_BOOT = (
    'import importlib.util, sys\n'
    "spec = importlib.util.spec_from_file_location('agentrc_hil_lock', sys.argv[1])\n"
    'lock = importlib.util.module_from_spec(spec)\n'
    'sys.modules[spec.name] = lock\n'
    'spec.loader.exec_module(lock)\n'
    'lock.BOARD_LOCK_DIR = sys.argv[2]\n'
    'lock._holder(sys.argv[6:], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))\n')


def _exec_holder(boards, reason, w_fd, ack_r):
    """Become the holder in a fresh interpreter whose environment is os.environ without HC_SCOPE, the pipes passed
    on: only an exec sets the environment a process started with. Never returns; a failed exec is reported."""
    env = {k: v for k, v in os.environ.items() if k != HC_SCOPE}
    try:
        for fd in (w_fd, ack_r):
            os.set_inheritable(fd, True)
        os.execve(sys.executable, [sys.executable, '-I', '-c', _HOLDER_BOOT, os.path.abspath(__file__),
                                   BOARD_LOCK_DIR, reason, str(w_fd), str(ack_r), *boards], env)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.write(w_fd, f'0holder not started: {e}'.encode())
    os._exit(1)


def _holder(boards, reason, w_fd, ack_r):
    """The hold holder: take every board, report readiness on w_fd and keep the hold once ack_r acknowledges it,
    until SIGTERM. Never returns."""
    def report(text) -> bool:
        """False when the parent is gone or stopped waiting (EPIPE, or no ack of a readiness):
        it reports a failure."""
        try:
            os.write(w_fd, text.encode())
            return not text.startswith('1') or os.read(ack_r, 1) == b'1'
        except OSError:
            return False

    handles = []
    start = proc_start(os.getpid())

    def _bow_out(*_):
        # clear the records before dying so read_record/status stay truthful (the kernel
        # drops the flocks themselves on exit either way); newest first, so a board recorded
        # after a release scanned is cleared before the boards it named and waits for
        for h in reversed(handles):
            clear_record(h)
        os._exit(0)

    signal.signal(signal.SIGTERM, _bow_out)          # before readiness: a release works at once
    try:
        for b in boards:
            # held off until the record is in `handles`: _bow_out would leave it behind
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
            handles.append(take_board_lock(b, reason, holder=HOLD_KIND, start=start))
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        ready = report('1')
    except (BoardLocked, LockUnavailable) as e:
        # lost a race, an older helper's cleanup holder, or a record failed: clear every record we wrote (the
        # flocks go with the exit) so no stale owned record outlives this holder
        for h in reversed(handles):
            clear_record(h)
        if type(e) is BoardLocked:
            report(f'0{b} locked: {holder_text(read_record(b))}')
        else:
            report(f'0{b}: {e}')
        os._exit(1)  # parent reports the failure
    if not ready:
        _bow_out()   # the parent reported a failure: a hold it never announced must not stay
    os.close(w_fd)
    while True:
        signal.pause()


def _release_one(b, victims) -> int:
    """Clear a stale record, or queue a provable hold holder for SIGTERM; 1 on a refusal."""
    try:
        fh = flock_nb(b)
    except BlockingIOError:
        state, info = record_state(b)
        if state != 'record':
            print(f'ERROR: {b} is held but its record is {state}', file=sys.stderr)
            return 1
        pid, reason = info.get('pid'), info.get('reason')
        if is_cleanup_holder(info):
            why = RECOVERY_BLOCKER
        elif is_protected(info):
            why = 'wait for it to finish'
        elif info.get('holder') != HOLD_KIND or info.get('start') is None or not isinstance(pid, int):
            why = 'not a hil_lock.py hold: ask it to finish, or recover'
        elif proc_start(pid) != info['start']:
            why = 'its recorded pid is not the process that wrote the record'
        else:
            try:
                why = '' if _holds_flock(pid, lock_path(b)) else 'its recorded pid does not hold the flock'
            except OSError as e:
                why = f'cannot prove it holds the flock: {e}'
        if why:
            print(f'ERROR: {b} is held by {reason} (pid {pid}) — not signalling it; {why}', file=sys.stderr)
            return 1
        victims.setdefault((pid, info['start']), []).append((b, info))
        return 0
    except OSError as e:
        print(f'ERROR: {b}: {e}', file=sys.stderr)
        return 1
    # flock was free: only a stale record remained — log what its task left owed, then clear it
    try:
        try:
            left = _leftover(b, fh)
        except OSError as e:
            print(f'ERROR: {b}: record cannot be read: {e} — kept', file=sys.stderr)
            return 1
        if why := _log_leftover(b, left):
            print(f'ERROR: {b}: its task left its cleanup owed and the {why} — kept', file=sys.stderr)
            return 1
        clear_record(fh)
        return 0
    finally:
        fh.close()


def _boards_of(pid, start) -> list:
    """Every board whose record names the holder `pid` started at `start`."""
    return [b for b in lock_names()
            if isinstance(r := read_record(b), dict) and (r.get('pid'), r.get('start')) == (pid, start)]


def _wait_released(owned, named, deadline) -> list:
    """The boards of `owned` ({board: (pid, start) of its signalled holder}) still taken at the
    deadline: by that holder's record, alive or not, or its flock, by an unreadable record, or,
    on a board in `named`, by any live holder. A board only _boards_of added is cleared once
    another holder has it."""
    def taken(b):
        state, rec = record_state(b)
        if state == 'unreadable':
            return True
        if state == 'record' and ((rec.get('pid'), rec.get('start')) == owned[b]
                                  or (b in named and _alive(rec))):
            return True
        return _keeps_flock(*owned[b], b)
    while True:
        still = sorted(b for b in owned if taken(b))
        if not still or time.monotonic() >= deadline:
            return still
        time.sleep(0.05)


def cmd_release(boards):
    rc = 0
    victims = {}
    for b in boards:
        rc |= _release_one(b, victims)
    owned = {}
    for (pid, start), held in sorted(victims.items()):
        # SIGTERM ends the holder on every board it holds: wait for all of its records
        theirs = {b for b, _ in held} | set(_boards_of(pid, start))
        try:
            _pidfd_signal(pid, start, signal.SIGTERM,
                          lambda: all(read_record(b) == info for b, info in held))
            print(f'released holder pid {pid}')
            owned.update(dict.fromkeys(theirs, (pid, start)))
        except ReleaseRefused as e:
            print(f'ERROR: {e}', file=sys.stderr)
            rc = 1
    if owned:
        still = set(_wait_released(owned, set(boards), time.monotonic() + RELEASE_WAIT))
        # a board recorded after the first scan, whose clear failed, is found only by a second
        still.update(*(_boards_of(*holder) for holder in set(owned.values())))
        if still:
            print(f'ERROR: not released (a live holder or a leftover record): {", ".join(sorted(still))}',
                  file=sys.stderr)
            return 1
    return rc


def cmd_release_token(token):
    try:
        pid, boards = release_token(token)
    except ReleaseRefused as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    print(f'released {len(boards)} lock(s) of holder {pid} (token {token}): {", ".join(boards)}')
    return 0


def failure_text(f) -> str:
    """One logged failure (open_cleanup_failures') for a message."""
    if 'unavailable' in f:
        return f['unavailable']
    what = f.get('reason') or 'unknown task'
    return (f'last failed cleanup {f.get("at")} ({f.get("how")}, {what} pid {f.get("pid")}, id {f.get("id")}): '
            f'{f.get("detail")}' + (f'; receipts {json.dumps(f["receipts"])}' if f.get('receipts') else '')
            + (f'; retained {json.dumps(f["retained"])}' if f.get('retained') else '')
            + (f'; {f["noteError"]}' if f.get('noteError') else ''))


def failure_report(failures) -> dict:
    """A report row's keys for the open failed cleanups a task found (its take's
    `cleanup_failures`): the latest whole, and how many are open when more than one."""
    if not failures:
        return {}
    out = {'previousCleanupFailure': failures[-1]}
    if len(failures) > 1:
        out['openCleanupFailures'] = len(failures)
    return out


def cmd_status():
    lines = []
    for b in sorted(set(lock_names()) | set(journal_names()) | set(_names(CLEANUP_LOG, skip_hidden=True))):
        journal = journal_state(b)
        data, state, info = _record(b)
        if _alive(info):
            lines.append(f'{b}: {holder_text(info)}')
        elif state == 'record' and _owes_cleanup(info):
            lines.append(f'{b}: stale {json.dumps(info)} (its task ended without its cleanup; the next task takes '
                         f'the board)')
        elif _acquiring(data):
            lines.append(f'{b}: acquisition in progress or interrupted (free once its taker is gone)')
        elif state == 'unreadable':
            lines.append(f'{b}: unreadable record (the next task takes the board unless its holder still runs)')
        if journal is not None:
            lines.append(f'{b}: takeover journal of an older helper {json.dumps(journal)} (left unless its '
                         f'holder still holds the board)')
        failures = open_cleanup_failures(b)
        if failures:
            earlier = f' ({len(failures) - 1} earlier still open)' if len(failures) > 1 else ''
            lines.append(f'{b}: {failure_text(failures[-1])}{earlier}')
    print('\n'.join(lines) if lines else 'no locks')
    return 0


def cmd_resolve(board, failure_id, reason):
    try:
        acknowledge_failure(board, failure_id, reason)
    except (LogRefused, OSError) as e:
        print(f'ERROR: {board}: {e}', file=sys.stderr)
        return 1
    print(f'{board}: failure {failure_id} acknowledged (closed by an operator, not proven restored)')
    return 0


def cmd_compact(boards):
    rc = 0
    for b in dict.fromkeys(boards):
        try:
            done = compact_cleanup_log(b)
        except (LogRefused, OSError) as e:
            print(f'ERROR: {b}: {e}', file=sys.stderr)
            rc = 1
            continue
        print(f'{b}: {done["dropped"]} events of closed failures dropped, {done["kept"]} kept')
    return rc


_CLI_USAGE = """Per-board advisory locks for a HIL host, shared by every project on it.

Arbitrates board access between dev sessions and project runners, which lock
themselves. Locks are kernel flocks: the kernel releases them automatically
when the holder process dies, and holders clear their lock-file record on
release so records stay truthful (/tmp also clears on reboot). A lock ends
with the task that took it: a free flock is always taken, and what a task
that ended without its cleanup left there is logged in the board's cleanup
log (<board>.cleanup.jsonl), which `status` shows, never a refusal.

Usage:
  hil_lock.py hold BOARD [BOARD...] --reason TEXT
  hil_lock.py release BOARD [BOARD...] | release --all | release --token TOKEN
  hil_lock.py status
  hil_lock.py resolve BOARD FAILURE_ID --reason TEXT
  hil_lock.py compact BOARD [BOARD...]

A holder process holds ALL boards given in one `hold` call; releasing any of
them ends that holder and releases all of its boards. `release --token` ends
an older helper's cleanup holder, which never frees on its own, once its
release predicate (the run_tracked receipts it waits for, the provider
sessions in its `retained`) holds: until then it refuses, naming what is
still open.

A logged failure stays open until a task that restored the board proves its
evidence ended. `resolve` closes one by an operator's acknowledgement with
its reason, logged as such and never as a restoration; it refuses while the
failure's evidence is known still live (a receipt rechecking pending as the
caller, a survivor or retained session alive). `compact` rewrites a board's
log without its closed failures, every open one kept, as the log's owner or
root; a closed one whose journalDigest matches a takeover journal still on
disk stays, with its closing events.
"""
TAKE_OVER_GONE = ('--take-over is gone (agentrc#464): a free board is taken by the next task, and a live older '
                  "helper's cleanup holder ends with `release --token`")


def main():
    ap = argparse.ArgumentParser(description=_CLI_USAGE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p_hold = sub.add_parser('hold')
    p_hold.add_argument('boards', nargs='*')
    p_hold.add_argument('--take-over', metavar='BOARD', help=argparse.SUPPRESS)
    p_hold.add_argument('--reason', required=True)
    p_rel = sub.add_parser('release')
    p_rel.add_argument('boards', nargs='*')
    p_rel.add_argument('--all', action='store_true')
    p_rel.add_argument('--token')
    sub.add_parser('status')
    p_res = sub.add_parser('resolve')
    p_res.add_argument('board')
    p_res.add_argument('failure')
    p_res.add_argument('--reason', required=True)
    p_cmp = sub.add_parser('compact')
    p_cmp.add_argument('boards', nargs='+')
    a = ap.parse_args()
    if a.cmd == 'hold':
        if a.take_over:
            ap.error(TAKE_OVER_GONE)
        if not a.boards:
            ap.error('name the boards to hold')
        sys.exit(cmd_hold(a.boards, a.reason))
    if a.cmd == 'release':
        if a.token:
            if a.boards or a.all:
                ap.error('--token takes no board names and no --all')
            sys.exit(cmd_release_token(a.token))
        boards = lock_names() if a.all else a.boards
        if not boards:
            ap.error('no boards given (name boards or use --all)')
        sys.exit(cmd_release(boards))
    if a.cmd == 'resolve':
        sys.exit(cmd_resolve(a.board, a.failure, a.reason))
    if a.cmd == 'compact':
        sys.exit(cmd_compact(a.boards))
    sys.exit(cmd_status())


if __name__ == '__main__':
    main()
