"""The headless-chief run ledger: one JSON line per launcher start and end, kept outside every run directory so a
deleted `--out` (a session scratchpad) loses no launch (#490).

Its path is AGENTRC_CHIEF_RUNS, else `${XDG_STATE_HOME:-~/.local/state}/agentrc/headless-chief/runs.jsonl`. Writers
and readers take `<ledger>.lock` for at most LOCK_WAIT seconds. Appends are serialized, not crash-atomic: a writer
killed mid-line leaves a torn line, which the next writer ends with a newline so it stays a line of its own.
"""
import errno
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

LOCK_WAIT = 5.0


class Busy(Exception):
    """The ledger lock was not taken in time."""


def path():
    if os.environ.get('AGENTRC_CHIEF_RUNS'):
        return Path(os.environ['AGENTRC_CHIEF_RUNS'])
    state = os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state'
    return Path(state) / 'agentrc' / 'headless-chief' / 'runs.jsonl'


@contextmanager
def locked(ledger, wait=None):
    """Hold `<ledger>.lock` for the block, or raise Busy after `wait` seconds (LOCK_WAIT)."""
    wait = LOCK_WAIT if wait is None else wait
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger.with_name(ledger.name + '.lock'), 'ab') as f:
        deadline = time.monotonic() + wait
        if os.name == 'nt':
            os.lseek(f.fileno(), 1 << 30, os.SEEK_SET)   # msvcrt locks are mandatory: lock a byte past any content
        while True:
            try:
                if os.name == 'nt':
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLOCK) or time.monotonic() >= deadline:
                    raise Busy(f'{ledger}.lock not taken in {wait:g} s: {e}') from e
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                os.lseek(f.fileno(), 1 << 30, os.SEEK_SET)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def append(record, ledger=None):
    """Append one record as a JSON line; OSError or Busy when it cannot."""
    ledger = ledger or path()
    line = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
    with locked(ledger), open(ledger, 'ab+') as f:
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b'\n':
                line = b'\n' + line   # a torn line from a killed writer stays its own
        f.write(line)


def read(ledger=None):
    """The ledger's lines as bytes, split on newline alone (a JSON string may hold U+2028) and read under its lock;
    [] when there is none yet. Busy or OSError when it cannot be read. Decoding each is the reader's."""
    ledger = ledger or path()
    with locked(ledger):
        if not ledger.exists():
            return []
        return [line for line in ledger.read_bytes().split(b'\n') if line]
