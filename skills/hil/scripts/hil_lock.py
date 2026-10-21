#!/usr/bin/env python3
"""Board-lock primitive shared by every project on a HIL host: the per-board kernel
flocks in BOARD_LOCK_DIR and their holder records (`hil` skill, Board locks), with a
hold/release/status CLI. A project's HIL contract wraps it for its roster and lock names.
"""
import argparse
import errno
import fcntl
import json
import os
import re
import secrets
import select
import signal
import stat
import sys
import time


BOARD_LOCK_DIR = '/tmp/tinyusb-hil-locks'
CI_REASON = 'hil_test.py'
# records naming these are never signalled by `release`, with or without the protected mark
PROTECTED_REASONS = {CI_REASON, 'pool_check', 'farm_check'}
# a holder record role that never frees on its own: a recovery blocker (`hil` Board locks)
CLEANUP_ROLE = 'cleanup-holder'
RECOVERY_BLOCKER = "recovery blocker: release it only through the project's recovery procedure"
HOLD_KIND = 'hil_lock-hold'   # `holder` of a record this CLI's own hold process wrote
JOURNAL = '.takeover'         # <board>.takeover: an unfinished or live recovery takeover
REWRITING = b'#'              # byte 0 of a record being rewritten: unreadable, so a blocker
ACQUIRING = b'~'              # byte 0 while take_board_lock writes its first record: a free board
DIR_MODE = 0o755
SHARE_STEP = (f'a second OS user needs the lock dir sticky and world-writable, a one-time host step '
              f'once every project on the host runs this helper: sudo install -d -m 1777 {BOARD_LOCK_DIR}')
RELEASE_WAIT = 3.0            # s a signalled holder gets to clear its records and exit
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]*\Z')


class LockUnavailable(RuntimeError):
    """The lock dir, lock file or holder record cannot be used: the board is refused, never
    run unlocked. The OSError is the __cause__."""


class BoardLocked(RuntimeError):
    """Another session holds the board; the message names its holder."""


class RecoveryBlocked(BoardLocked):
    """The flock is free but the board carries an unresolved recovery: a cleanup holder or a
    protected run that died, or a takeover journal. Only a recovery takeover clears it."""


class ReleaseRefused(RuntimeError):
    """A token release was refused before signalling, or its delivery or wait failed."""


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
            raise OSError(errno.EIO, f'holder record write made no progress at offset {offset}')
        data, offset = data[n:], offset + n


def write_record(fh, reason: str, *, acquiring=False, **extra) -> None:
    """Holder record; the flock itself is already held. Raises OSError on a write failure: a
    hold whose record is missing is invisible to status/release, so every caller refuses it.
    `extra` adds a tool's own fields (hil_reserve: token, start, locks, role). Overwritten in
    place, inode kept, behind a non-JSON first byte that is restored last: an interrupted
    rewrite leaves the old record or an unreadable one, never an empty record or a valid
    splice of new and old bytes that a blocker would vanish as. That byte is REWRITING, or
    ACQUIRING for take_board_lock's first record only (agentrc#154): its taker never returned,
    so a free flock under it is a free board; a helper older than ACQUIRING reads it as
    unreadable and refuses, fail-closed as for REWRITING."""
    data = json.dumps({'pid': os.getpid(), 'reason': reason,
                       'since': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **extra}).encode()
    fd = fh.fileno()
    _pwrite_all(fd, ACQUIRING if acquiring else REWRITING, 0)
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


def release_board_lock(fh) -> None:
    """A taker's clean exit: clear the record, then drop the flock. A protected taker that
    only closes the handle leaves its record as a recovery blocker."""
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
    """None without a journal, else its dict ({} when it cannot be read: still a blocker)."""
    try:
        state, rec = _parse(_read_file(board, JOURNAL))
    except OSError:
        return {}
    return None if state == 'absent' else rec or {}


def takeover_predecessor(board: str, token: str):
    """(state, record), as record_state gives them, of what the takeover holding `board` under
    `token` replaced: the oldest journal's predecessor, so a takeover of a dead taker names the
    original holder. LookupError says why it cannot be told: no journal, `token` not at the
    head of the chain, or a chain element that cannot be read."""
    _check_name(board)
    journal = journal_state(board)
    if journal is None:
        raise LookupError(f'{board} has no takeover journal')
    if not journal or journal.get('token') != token:
        raise LookupError(f'{board}{JOURNAL} is not the takeover of token {token}'
                          f'{"" if journal else ": it cannot be read"}')
    while True:
        if not isinstance(journal.get('token'), str) or not isinstance(journal.get('predecessor'), str):
            raise LookupError(f'{board}{JOURNAL}: the takeover of token {journal.get("token")!r} kept no predecessor')
        prior = journal.get('prior')
        if prior is None:
            return _parse(journal['predecessor'].encode())
        if not isinstance(prior, dict) or not prior:
            raise LookupError(f'{board}{JOURNAL} names an earlier takeover that cannot be read')
        journal = prior


def is_cleanup_holder(info) -> bool:
    return isinstance(info, dict) and info.get('role') == CLEANUP_ROLE


def is_protected(info) -> bool:
    """`release` never signals it; a malformed mark never cancels the legacy names."""
    return isinstance(info, dict) and (info.get('protected') is True or info.get('reason') in PROTECTED_REASONS)


def is_recovery_blocker(info) -> bool:
    """A record that, left behind on a free flock, means its cleanup never happened (so does
    one that cannot be read: see _blocker)."""
    return is_cleanup_holder(info) or (isinstance(info, dict) and info.get('protected') is True)


def holder_text(info) -> str:
    """A holder record for a message; a cleanup holder is named a recovery blocker."""
    if not isinstance(info, dict):
        return 'unknown holder'
    text = json.dumps(info)
    return f'{text} ({RECOVERY_BLOCKER})' if info.get('role') == CLEANUP_ROLE else text


def _handle_record(fh) -> bytes:
    fd = fh.fileno()
    return os.pread(fd, os.fstat(fd).st_size, 0)


def _blocker(board, fh) -> str:
    """Why a board whose flock we now hold must not be taken, or ''."""
    journal = journal_state(board)
    if journal is not None:
        return f'takeover journal {json.dumps(journal)}'
    data = _handle_record(fh)
    if _acquiring(data):
        return ''
    state, rec = _parse(data)
    if state == 'record' and is_recovery_blocker(rec):
        return f'stale record {json.dumps(rec)}'
    if state == 'unreadable':      # a record cut short mid-write may have been a blocker
        return f'unreadable record {data[:200]!r}'
    return ''


def _unavailable(e) -> LockUnavailable:
    hint = f'; {SHARE_STEP}' if isinstance(e, PermissionError) else ''
    return LockUnavailable(f'board lock unavailable: {e}{hint}')


def take_board_lock(board: str, reason: str, *, protected=False, **extra):
    """Take the board's flock and record ourselves as its holder; returns the open handle
    (keep it referenced; release_board_lock ends it). Raises BoardLocked when another session
    holds the board, RecoveryBlocked when its flock is free but a recovery is unresolved,
    and LockUnavailable when the lock cannot be taken or recorded (fail-closed).

    `protected` marks the record: `release` never signals it, and a record left on a free
    flock is a recovery blocker, so its taker must end with release_board_lock."""
    try:
        fh = flock_nb(board)
    except BlockingIOError:
        raise BoardLocked(f'board locked: {holder_text(read_record(board))}') from None
    except OSError as e:
        raise _unavailable(e) from e
    try:
        why = _blocker(board, fh)
    except OSError as e:
        fh.close()
        raise LockUnavailable(f'board lock unavailable: holder record: {e}') from e
    if why:
        fh.close()
        raise RecoveryBlocked(f'board {board}: {why} ({RECOVERY_BLOCKER}: '
                              f'`hil_lock.py hold --take-over {board}` under a recovery dispatch)')
    if protected:
        extra = {'protected': True, **extra}
    # announce ourselves so the other side's conflict message is truthful
    try:
        write_record(fh, reason, acquiring=True, **extra)
    except OSError as e:
        clear_record(fh)
        try:
            fh.close()   # even a failing close lets the fd (and flock) go
        except OSError:
            pass
        raise LockUnavailable(f'board lock unavailable: holder record: {e}') from e
    return fh


def acquire_board_lock(board_name, reason=CI_REASON, **kw):
    """take_board_lock for the duration of a flash+test, or None when HIL_NO_BOARD_LOCK=1."""
    if os.environ.get('HIL_NO_BOARD_LOCK') == '1':
        return None  # user-authorized bypass — see the HIL contract
    return take_board_lock(board_name, reason, **kw)


# --- holder identity ------------------------------------------------------------------------
def proc_start(pid):
    """The process start time (clock ticks since boot, proc(5) field 22, as recorded), or
    None when it is gone."""
    try:
        with open(f'/proc/{pid}/stat') as f:
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
        with open(f'/proc/{pid}/task/{t}/stat') as f:
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


def release_token(token: str):
    """End the token's holder: its token, pid, start, role and lock set agree on every board
    that carries the token, it is alive as recorded, and SIGUSR1 reaches it through a pidfd;
    returns (pid, boards) once its records and flocks are gone. A dead holder is refused: its boards
    need a recovery takeover."""
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
        if rec.get('pid') != pid or rec.get('start') != start or rec.get('locks') != lockset:
            raise ReleaseRefused(f'not signalled: token {token}: the record on {b} disagrees with the others')
        if not is_cleanup_holder(rec):
            raise ReleaseRefused(f'not signalled: holder pid {pid} is not a cleanup holder on {b}: '
                                 f'still mutating or already clean')
    if sorted(lockset) != sorted(found):
        raise ReleaseRefused(f'not signalled: token {token}: record lock set {sorted(lockset)} '
                             f'!= files carrying it {sorted(found)}')
    live = proc_start(pid)
    if live != start:
        gone = (f'holder pid {pid} of token {token} is gone' if live is None else
                f'pid {pid} is not the recorded holder (start {live} != {start})')
        raise ReleaseRefused(f'not signalled: {gone}; its boards need a recovery takeover '
                             f'(`hil_lock.py hold --take-over {lockset[0]}`)')

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
        raise ReleaseRefused(f'signalled, but holder pid {pid} left its records or flocks on {", ".join(left)}')
    return pid, sorted(found)


# --- recovery takeover ----------------------------------------------------------------------
def _lock_set(board) -> list:
    """The board plus every board its record or journal chain names, transitively."""
    todo, seen = [board], set()
    while todo:
        b = todo.pop()
        if b in seen:
            continue
        _check_name(b)
        seen.add(b)
        names = []
        rec = read_record(b)
        if isinstance(rec, dict):
            names += rec.get('locks') or []
        journal = journal_state(b)
        if journal == {}:
            raise OSError(errno.EINVAL, f'{b}{JOURNAL} cannot be read, so the lock set it covers is unknown: '
                                        f'inspect it by hand')
        while isinstance(journal, dict) and journal:
            names += journal.get('locks') or []
            journal = journal.get('prior')
        todo += [n for n in names if isinstance(n, str)]
    return sorted(seen)


def _write_journal(dfd, board, content) -> None:
    """Replace <board>.takeover durably: a crash leaves the old journal or the new one."""
    tmp = f'.{board}{JOURNAL}.{os.getpid()}.{secrets.token_hex(4)}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dfd)
    try:
        os.fchmod(fd, 0o644)                # umask-proof: another OS user's takeover reads it
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, 'wb') as f:          # a buffered write is all or an exception
        f.write(json.dumps(content).encode())
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, board + JOURNAL, src_dir_fd=dfd, dst_dir_fd=dfd)
    os.fsync(dfd)


def _foreign_journals(boards) -> str:
    """The journals of `boards` this user cannot replace, or '': a sticky dir lets only a
    file's owner, the dir's owner or root rename over or unlink it."""
    euid = os.geteuid()
    if euid == 0:
        return ''
    dfd = _open_dir()
    try:
        st = os.fstat(dfd)
        if not st.st_mode & stat.S_ISVTX or st.st_uid == euid:
            return ''
        foreign = []
        for b in boards:
            try:
                uid = os.stat(b + JOURNAL, dir_fd=dfd, follow_symlinks=False).st_uid
            except FileNotFoundError:
                continue
            if uid != euid:
                foreign.append(f'{b}{JOURNAL} (uid {uid})')
    finally:
        os.close(dfd)
    return ', '.join(foreign)


def _drop_flocks(handles) -> None:
    """A failed takeover's cleanup: close every handle and touch no record or journal."""
    for fh in handles.values():
        try:
            fh.close()
        except OSError:
            pass


def take_over(board: str, reason: str, detail=''):
    """Recovery takeover of `board` and every board its record or journal chain names, all
    or nothing; returns (token, {board: handle}). Each board first gets a journal holding
    its predecessor's record bytes and any earlier journal, then a cleanup-holder record:
    a crash anywhere leaves a journal, so the boards stay blocked. Only release_token of
    this live holder, after the recovery succeeded, clears records and journals.
    Run only under a recovery dispatch naming these boards, as the OS user owning any
    journal on them, or root."""
    try:
        boards = _lock_set(board)
    except OSError as e:
        raise _unavailable(e) from e
    handles = {}
    try:
        for b in boards:
            handles[b] = flock_nb(b)
    except BlockingIOError:
        _drop_flocks(handles)
        raise BoardLocked(f'board locked: {holder_text(read_record(b))}') from None
    except OSError as e:
        _drop_flocks(handles)
        raise _unavailable(e) from e
    try:
        again = _lock_set(board)
    except OSError as e:
        again = e
    if again != boards:
        _drop_flocks(handles)
        raise LockUnavailable(f'takeover of {board}: its lock set changed while it was taken '
                              f'({boards} -> {again}); nothing modified, run it again')
    try:
        foreign = _foreign_journals(boards)
    except OSError as e:
        _drop_flocks(handles)
        raise _unavailable(e) from e
    if foreign:
        _drop_flocks(handles)
        raise LockUnavailable(f'takeover of {board}: {foreign} belong to another OS user, and the sticky '
                              f'lock dir lets only their owner or root replace them; nothing modified: run '
                              f'the takeover and its `release --token` as that user or root')
    token = secrets.token_hex(8)
    try:
        fields = holder_fields(token, boards)
        dfd = _open_dir()
        try:
            for b in boards:
                _write_journal(dfd, b, {'token': token, 'pid': os.getpid(), 'start': fields['start'],
                                        'locks': boards,
                                        'predecessor': _handle_record(handles[b]).decode(errors='backslashreplace'),
                                        'prior': journal_state(b)})
        finally:
            os.close(dfd)
        for b in boards:
            write_record(handles[b], reason, holder=HOLD_KIND, role=CLEANUP_ROLE, detail=detail, **fields)
            os.fsync(handles[b].fileno())
    except (OSError, ValueError) as e:
        _drop_flocks(handles)          # the journals written so far keep every board blocked
        raise LockUnavailable(f'takeover of {", ".join(boards)} failed: {e}; '
                              f'journals left: {", ".join(journal_names()) or "none"}') from e
    return token, handles


def end_takeover(handles) -> None:
    """The takeover holder's exit after a successful recovery: records, then journals."""
    for fh in handles.values():
        clear_record(fh)
    try:
        dfd = _open_dir()
    except OSError:
        dfd = None                 # the journals stay: still blocked, never silently clean
    if dfd is not None:
        for b in handles:
            try:
                os.unlink(b + JOURNAL, dir_fd=dfd)
            except OSError:
                pass               # left behind, it keeps the board a recovery blocker
        os.close(dfd)
    for fh in handles.values():
        fh.close()


# --- operator CLI (hold/release/status) ------------------------------------
def is_locked(board: str) -> bool:
    """True if the recorded holder process is still alive.

    Deliberately never touches the flock: even a momentary probe lock would
    make a concurrent acquirer's LOCK_NB attempt fail spuriously. The flock
    taken by acquirers themselves stays the only authority."""
    return _alive(read_record(board))


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


def cmd_hold(boards, reason, take_over_board=None):
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
            if take_over_board:
                token, held = msg[1:].split(' ', 1)
                print(f'took over: {held} (cleanup holder, token {token}); after the recovery succeeds: '
                      f'hil_lock.py release --token {token}')
            else:
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

    def report(text) -> bool:
        """False when the parent is gone or stopped waiting (EPIPE, or no ack of a readiness):
        it reports a failure."""
        try:
            os.write(w_fd, text.encode())
            return not text.startswith('1') or os.read(ack_r, 1) == b'1'
        except OSError:
            return False

    if take_over_board:
        # a takeover holder is a cleanup holder: SIGTERM never ends it, release --token does
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            token, handles = take_over(take_over_board, reason)
        except (BoardLocked, LockUnavailable) as e:
            report(f'0{e}')
            os._exit(1)
        def _recovered(*_):
            end_takeover(handles)
            os._exit(0)

        signal.signal(signal.SIGUSR1, _recovered)    # before readiness: the token works at once
        if not report(f'1{token} {", ".join(handles)}'):
            os._exit(1)        # no end_takeover: the journals and records stay recovery blockers
        os.close(w_fd)
        while True:
            signal.pause()

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
        # lost a race, a blocker, or a record failed: clear every record we wrote (the
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
    # flock was free: only a stale record remained — clear it, unless it blocks a recovery
    try:
        try:
            why = _blocker(b, fh)
        except OSError as e:
            why = f'record cannot be read: {e}'
        if why:
            print(f'ERROR: {b}: {why} — kept ({RECOVERY_BLOCKER})', file=sys.stderr)
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


def cmd_status():
    lines = []
    for b in sorted(set(lock_names()) | set(journal_names())):
        journal = journal_state(b)
        data, state, info = _record(b)
        if _alive(info):
            lines.append(f'{b}: {holder_text(info)}')
        elif state == 'record' and is_recovery_blocker(info):
            lines.append(f'{b}: stale {json.dumps(info)} ({RECOVERY_BLOCKER})')
        elif _acquiring(data):
            lines.append(f'{b}: acquisition in progress or interrupted (free once its taker is gone)')
        elif state == 'unreadable':
            lines.append(f'{b}: unreadable record ({RECOVERY_BLOCKER} unless its holder still runs)')
        if journal is not None:
            lines.append(f'{b}: takeover journal {json.dumps(journal)} ({RECOVERY_BLOCKER})')
    print('\n'.join(lines) if lines else 'no locks')
    return 0


_CLI_USAGE = """Per-board advisory locks for a HIL host, shared by every project on it.

Arbitrates board access between dev sessions and project runners, which lock
themselves. Locks are kernel flocks: the kernel releases them automatically
when the holder process dies, and holders clear their lock-file record on
release so records stay truthful (/tmp also clears on reboot). A record a
cleanup holder or a protected run leaves on a free flock, or a takeover
journal, is a recovery blocker: no one takes the board until a recovery
takeover ends it.

Usage:
  hil_lock.py hold BOARD [BOARD...] --reason TEXT
  hil_lock.py hold --take-over BOARD --reason TEXT   (recovery dispatch only)
  hil_lock.py release BOARD [BOARD...] | release --all | release --token TOKEN
  hil_lock.py status

A holder process holds ALL boards given in one `hold` call; releasing any of
them ends that holder and releases all of its boards. A takeover holder is a
cleanup holder over the blocked board's whole lock set; only `release --token`
ends it, once the recovery succeeded.
"""


def main():
    ap = argparse.ArgumentParser(description=_CLI_USAGE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p_hold = sub.add_parser('hold')
    p_hold.add_argument('boards', nargs='*')
    p_hold.add_argument('--take-over', metavar='BOARD')
    p_hold.add_argument('--reason', required=True)
    p_rel = sub.add_parser('release')
    p_rel.add_argument('boards', nargs='*')
    p_rel.add_argument('--all', action='store_true')
    p_rel.add_argument('--token')
    sub.add_parser('status')
    a = ap.parse_args()
    if a.cmd == 'hold':
        if bool(a.boards) == bool(a.take_over):
            ap.error('name boards, or one --take-over BOARD')
        sys.exit(cmd_hold(a.boards, a.reason, a.take_over))
    if a.cmd == 'release':
        if a.token:
            if a.boards or a.all:
                ap.error('--token takes no board names and no --all')
            sys.exit(cmd_release_token(a.token))
        boards = lock_names() if a.all else a.boards
        if not boards:
            ap.error('no boards given (name boards or use --all)')
        sys.exit(cmd_release(boards))
    sys.exit(cmd_status())


if __name__ == '__main__':
    main()
