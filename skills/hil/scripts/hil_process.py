#!/usr/bin/env python3
"""Bounded command runner shared by the HIL harnesses (`hil` skill, Flashers). Linux only,
stdlib only, no module state: every bound and switch is a parameter.

Contract:
- `cmd` a STRING runs through the shell; a LIST is an argv that goes straight to execve.
- The child runs in its own session (start_new_session), so the terminal's SIGINT never
  reaches it and its whole process group can be killed.
- On `timeout` the group is SIGKILLed and reaped for at most `reap_grace` more seconds, then
  a CompletedProcess with returncode 124 comes back: stdout/stderr text or bytes as the call
  asked, stderr merged into stdout or split as on the success path, never None when split.
- A survivor of that reap (D state, or a root child of sudo) is abandoned with our pipe ends
  closed and kept in no registry: rc 124 proves neither that every descendant died nor that
  the board it touched is safe.
- Spawn errors (FileNotFoundError, ...) propagate. SIGINT/SIGTERM handled while the child is
  being spawned are deferred into the cleanup `try`; any interrupt kills the group, closes
  our pipe ends, never waits, and propagates.
- `run_alongside` adds no bound around `work()`; only its reap is bounded.
- `run_tracked` is run_cmd for a caller that must know the operation ended (the farm check):
  its result, or the exception it raises, carries `cleanup` and `receipt`. A supervisor it
  starts (this file, `--supervise`, in a fresh `python -I` and its own session; a fork of a
  threaded caller could inherit a held lock) is a child subreaper that runs the command
  through run_cmd's mechanics and reaps every descendant: `done` once waitpid finds no child
  left within `settle` after the exit, timeout or interrupt; `pending` when one is left (the
  supervisor stays, holding and reaping, until none is); `unknown` when it died, broke the
  pipe or did not report within timeout + reap_grace + settle + SUPERVISOR_MARGIN. The caller
  never reads /proc and never becomes a subreaper; every wait of its own is bounded, and an
  interrupt asks the supervisor to kill the group, as run_cmd does, and waits its report.
  The receipt (None for `done`) names the supervisor (pid, start, its lock's dev and inode),
  its lock, record and log in `state_dir`, and a best-effort survivor listing, never proof;
  `recheck` answers from the supervisor's lock and record. `state_dir` must be a directory of
  the caller's uid that no other user can write, in an immediate parent no other user can
  write unless sticky: run_tracked (PermissionError, before anything starts) and recheck
  (unknown) check those two only (`trusted_dir`), since a replaced lock or record would forge
  `done`. Higher ancestors are not checked; the caller must keep them trusted and unchanged
  for as long as a receipt names the directory. Files of a `done` run are removed only once
  the verdict is on the result or exception; the others stay for recheck and are the
  caller's to remove (`tidy`, once a recheck answers `done`). A missing file reads as unknown.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import fcntl
import json
import math
import os
import secrets
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any

# Post-SIGKILL reap, spent ON TOP of a timeout whenever the child has to be killed. A caller
# budgeting several bounded steps must add one of these PER STEP, or its own outer bound fires
# mid-step -- for a flasher, orphaning it on the probe.
REAP_GRACE = 10


def cmd_stdout_text(out: Any) -> str:
    if out is None:
        return ''
    if isinstance(out, bytes):
        return out.decode('utf-8', errors='ignore')
    return str(out)


def _banner_body(out: Any, err: Any) -> str:
    # split_stderr callers keep the diagnostic in stderr — a banner of stdout alone
    # would be blank exactly when something went wrong
    body = cmd_stdout_text(out)
    err_text = cmd_stdout_text(err)
    if err_text:
        body = f'{body}\n{err_text}' if body else err_text
    return body


# Shared with callers that strip them from captured output: duplicated literals let the two
# layers drift and reintroduce literal marker noise mid-row in the GitHub log.
GROUP_MARK, ENDGROUP_MARK = '::group::', '::endgroup::'


def strip_workflow_markers(line: str) -> str:
    # run_cmd only ever emits markers at line start; mid-line is not a real case.
    return line.removeprefix(GROUP_MARK).removeprefix(ENDGROUP_MARK)


def _ci_log_groups() -> bool:
    # GitHub folds ::group::/::endgroup:: only at line start of the JOB's real stdout; a
    # pool worker's capture is compacted into one row line, where they render literally.
    return bool(os.getenv('CI')) and sys.stdout is sys.__stdout__


def _print_banner(title: str, out: Any, err: Any) -> None:
    print()
    if _ci_log_groups():
        print(f'{GROUP_MARK}{title}')
        print(_banner_body(out, err))
        print(ENDGROUP_MARK)
    else:
        print(title)
        print(_banner_body(out, err))


def _kill_group(p: subprocess.Popen) -> None:
    """SIGKILL the child's process group. Never raises: ProcessLookupError is a group already
    gone, PermissionError an all-root group refusing the kill, and either escaping would skip
    the caller's bounded reap, pipe close and rc-124 return."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def _close_pipes(p: subprocess.Popen) -> None:
    """Close OUR ends of an abandoned child's pipes. Never raises."""
    for pipe in (p.stdout, p.stderr, p.stdin):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass


class InterruptHold:
    """Defer SIGINT/SIGTERM's Python handlers across Popen and into the caller's cleanup
    `try`, where release() delivers whatever arrived. Popen forks before that `try` is
    entered, and a handler that raised in between unwound with the child alive (tinyusb #4022).

    Handlers are swapped rather than masked with pthread_sigmask: CPython runs a Python
    handler in the main thread whichever thread the kernel delivered to, so masking only
    the caller's thread leaves the window open in a threaded process. Off the main thread
    no handler can run in the caller, so there is nothing to hold. Only a Python callable
    can unwind the caller; SIG_DFL, SIG_IGN and C-level handlers are left alone.
    """

    def __init__(self):
        self._held = {}
        self._pending = []
        self._released = False
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old = signal.getsignal(sig)
                if not callable(old):
                    continue
                # recorded BEFORE the swap, so a signal landing right after it is restorable
                self._held[sig] = old
                try:
                    signal.signal(sig, self._defer)
                except ValueError:
                    del self._held[sig]
                    break           # not the main thread
        except BaseException:
            # SIGTERM's own handler raised after SIGINT's was swapped: never strand a deferral
            self.release()
            raise

    def _defer(self, sig, frame):
        if self._released:      # left installed by a signal that cut release() short
            return self._held[sig](sig, frame)
        self._pending.append(sig)

    def release(self) -> None:
        self._released = True
        for sig, old in self._held.items():
            signal.signal(sig, old)
        for sig in dict.fromkeys(self._pending):
            signal.raise_signal(sig)


def run_alongside(argv: list, work, timeout: int, *,
                  reap_grace: float = REAP_GRACE) -> subprocess.CompletedProcess:
    """Run `argv` alongside `work()`, which runs in THIS thread, then reap it -- bounded.

    The read-while-we-write shape run_cmd cannot express: the caller needs the child
    RUNNING while it does something else. Everything else about the contract is run_cmd's
    -- own session, killpg, bounded reap, our pipe ends closed, rc 124 on the kill.

    A PROCESS, not a thread: an abandoned thread keeps the fd, and usblp_open returns
    -EBUSY while usblp->used (v6.12.96 usblp.c), so every later open in this long-lived
    worker would read as a wedged device. A killed process takes its fd with it.

    stdout is captured as BYTES and kept CLEAN -- a caller byte-compares it against the
    payload it sent, so a single stderr byte (a PYTHONWARNINGS chirp, a sitecustomize
    print, a .pth deprecation from a venv) would read as USB data corruption. stderr gets
    its own pipe; communicate() drains both, so the split cannot deadlock.
    `work` runs even if the child dies immediately -- the caller's own asserts decide.
    """
    hold = InterruptHold()
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    except BaseException:
        hold.release()
        raise

    reaped = False

    def _reap() -> subprocess.CompletedProcess:
        nonlocal reaped
        try:
            out, err = p.communicate(timeout=timeout)
            reaped = True
            return subprocess.CompletedProcess(argv, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            _kill_group(p)
            try:
                out, err = p.communicate(timeout=reap_grace)
            except subprocess.TimeoutExpired:
                # Outlasted SIGKILL: uninterruptible, still holding whatever it opened.
                # Abandoned.
                out, err = b'', b''
                _close_pipes(p)     # our own fds must not leak either
            reaped = True
            return subprocess.CompletedProcess(argv, 124, out, err)

    try:
        hold.release()  # a signal deferred across the fork raises here: killed, not reaped
        try:
            work()
        except Exception:
            # Reap first so the child never outlives us, then let the caller's error through.
            # A `return` inside a `finally` would SWALLOW it -- an assert in `work` would
            # vanish and the caller would compare data it never finished sending. An
            # interrupt is not reaped: it takes the kill-and-close path below, never waiting.
            _reap()
            raise
        return _reap()
    except BaseException:
        # Interrupted before a reap completed: as run_cmd's interrupt path, kill and close,
        # never wait. Not keyed on returncode: communicate() reaps the leader on
        # KeyboardInterrupt, its group living on. The pgid stays ours while any member lives.
        if not reaped:
            _kill_group(p)
            _close_pipes(p)
        raise


def _cmd_label(cmd) -> str:
    """A one-line name for a banner. An argv whose payload is a `python3 -c` program would
    otherwise dump the whole body into the CI log, where run_cmd's banners are already the
    noisiest thing in a failing row."""
    if isinstance(cmd, str):
        return cmd
    parts = [a if len(a) <= 60 else f'<{len(a)}-char program>' for a in map(str, cmd)]
    return ' '.join(parts)


def run_cmd(cmd: str | list, cwd: str | None = None, *, timeout: float,
            binary: bool = False, split_stderr: bool = False, quiet: bool = False,
            reap_grace: float = REAP_GRACE, verbose: bool = False) -> subprocess.CompletedProcess:
    """Bounded subprocess: own session, killpg on expiry, rc 124 when it had to be killed.

    `cmd` is a shell STRING or an argv LIST. argv exists for a program that cannot survive
    a trip through the shell -- a multi-line `python3 -c` body -- which is how a harness
    runs a library call that no in-process bound can contain. A daemon thread cannot bound
    a C call that holds the GIL, so for those the child process IS the bound.
    """
    return _run(cmd, cwd, timeout, binary, split_stderr, quiet, reap_grace, verbose)


def _run(cmd, cwd, timeout, binary, split_stderr, quiet, reap_grace, verbose,
         track: dict | None = None) -> subprocess.CompletedProcess:
    """run_cmd; `track` receives the Popen as 'popen' before any deferred interrupt can unwind,
    and 'timed_out' True when the timeout killed the group."""
    if timeout is None:
        raise ValueError('run_cmd needs an explicit timeout')
    # binary: raw bytes (text mode's errors='replace' mangles non-UTF-8 file content).
    # split_stderr: keep stderr out of stdout, for callers that parse stdout. quiet: no
    # COMMAND FAILED banner, for retry loops that report failures themselves (timeouts
    # still print: a killed child is always noteworthy).
    popen_kwargs = {
        'cwd': cwd,
        # a list goes straight to execve; only a string needs a shell to parse it
        'shell': isinstance(cmd, str),
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE if split_stderr else subprocess.STDOUT,
    }
    if not binary:
        popen_kwargs.update({'text': True, 'encoding': 'utf-8', 'errors': 'replace'})
    # C-level setsid, same process-group semantics as preexec_fn=os.setsid but safe when
    # called from threads
    popen_kwargs['start_new_session'] = True

    hold = InterruptHold()
    try:
        p = subprocess.Popen(cmd, **popen_kwargs)
    except BaseException:
        hold.release()
        raise
    timed_out = False
    try:
        if track is not None:
            track['popen'] = p
        hold.release()      # a signal deferred across the fork raises here, cleanup armed
        out, err = p.communicate(timeout=timeout)
        r = subprocess.CompletedProcess(args=cmd, returncode=p.returncode, stdout=out, stderr=err)
    except subprocess.TimeoutExpired as ex:
        timed_out = True
        if track is not None:
            track['timed_out'] = True
        _kill_group(p)
        try:
            out, err = p.communicate(timeout=reap_grace)
        except subprocess.TimeoutExpired:
            # Something in the group outlived SIGKILL: D state (truly unkillable), or
            # root-owned because sudo FORKS rather than execs, so the wrapper dies and its
            # root child does not. Abandon it; the harness never
            # sudo-kills its way out. Our ends of its pipes must not leak, though: a pool
            # worker lives for the whole run, so every wedged command would cost it two fds.
            out, err = None, None
            _close_pipes(p)
        except BaseException:
            # interrupted mid-reap, already killed: the BaseException handler below is a sibling
            _close_pipes(p)
            raise
        # prefer the post-kill buffers (supersets of the exception's), falling back to ex.*
        # when the child was unkillable. TimeoutExpired carries BYTES even for a text-mode
        # Popen, so the fallbacks must be decoded or a text-mode caller gets bytes exactly
        # when the child wedged in D state.
        def _typed(v):
            if not binary and isinstance(v, bytes):
                return v.decode('utf-8', errors='replace')
            return v

        timeout_out = _typed(out or ex.stdout) or (b'' if binary else '')
        # ...and never None: with split_stderr the SUCCESS path always yields a str/bytes,
        # so a caller that does `r.stderr.strip()` works everywhere except the timeout --
        # the one path it was written for. Without split_stderr stderr stays None, as on
        # the success path (it was merged into stdout).
        timeout_err = _typed(err if err is not None else ex.stderr)
        if split_stderr and timeout_err is None:
            timeout_err = b'' if binary else ''
        r = subprocess.CompletedProcess(args=cmd, returncode=124, stdout=timeout_out, stderr=timeout_err)
    except BaseException:
        # BaseException, not Exception (as in CPython's own subprocess.run):
        # KeyboardInterrupt is the case that matters, and start_new_session put the child in
        # its OWN group, so it never got the terminal's SIGINT -- without this, Ctrl-C
        # leaves the flasher or testusb holding the probe and its usbfs node. Kill and
        # close, never wait: this path must not add a hang of its own.
        _kill_group(p)
        _close_pipes(p)
        raise
    _announce(cmd, r, timeout, timed_out, quiet, verbose)
    return r


def _announce(cmd, r: subprocess.CompletedProcess, timeout, timed_out: bool, quiet: bool, verbose: bool) -> None:
    """A finished command's banner: a timeout always, a failure unless `quiet`, else its output
    when `verbose`. `timed_out` is the kill's own evidence: rc 124 may be the command's."""
    if timed_out:
        _print_banner(f'COMMAND TIMEOUT ({timeout}s): {_cmd_label(cmd)}', r.stdout, r.stderr)
    elif r.returncode != 0 and not quiet:
        _print_banner(f'COMMAND FAILED: {_cmd_label(cmd)}', r.stdout, r.stderr)
    elif verbose:
        print(cmd)
        print(cmd_stdout_text(r.stdout))


# --- run_tracked: completion proven by an operation-owned supervisor -------------------------
PR_SET_CHILD_SUBREAPER = 36
WAIT_ALL = 0x40000000       # __WALL (uapi linux/wait.h): a clone child is a child too
SETTLE_POLL = 0.05
SURVEY_BOUND = 1.0          # the supervisor's best-effort survivor listing, names only
# On top of timeout + reap_grace + settle: the supervisor's interpreter start, survey and report.
SUPERVISOR_MARGIN = 5.0


def _encode(v):
    if v is None:
        return None
    return {'b64': base64.b64encode(v).decode('ascii')} if isinstance(v, bytes) else {'text': v}


def _decode(v):
    if v is None:
        return None
    return base64.b64decode(v['b64']) if 'b64' in v else v['text']


def _error(e: BaseException) -> dict:
    return {'error': {'type': type(e).__name__, 'message': str(e), 'errno': getattr(e, 'errno', None),
                      'strerror': getattr(e, 'strerror', None), 'filename': getattr(e, 'filename', None)}}


def _rebuilt(err: dict) -> Exception:
    if isinstance(err.get('errno'), int):
        return OSError(err['errno'], err.get('strerror'), err.get('filename'))   # errno picks the subclass
    return RuntimeError(f"{err.get('type')}: {err.get('message')}")


def _send(fd: int, msg: dict) -> None:
    data = (json.dumps(msg) + '\n').encode()
    try:
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass        # the caller is gone; the record still says how this ends


def _write_record(path: str, rec: dict) -> None:
    tmp = f'{path}.tmp'
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        json.dump(rec, f)
    os.replace(tmp, path)


def trusted_dir(path: str) -> str:
    """`path` resolved, refused (PermissionError) unless it is a directory of ours that nobody
    else can write, in an immediate parent of ours or root's that nobody else can write unless
    it is sticky. Only those two are checked: keeping every higher ancestor trusted and
    unchanged for as long as a receipt names the directory is the caller's precondition."""
    real = os.path.realpath(path)
    st, parent = os.stat(real), os.stat(os.path.dirname(real))
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise PermissionError(f'{real}: want a directory of uid {os.geteuid()} that nobody else can write')
    if parent.st_uid not in (os.geteuid(), 0) or parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise PermissionError(f'{real}: another user can replace it through its parent')
    return real


def _become_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f'cannot become a child subreaper: {os.strerror(err)}')


def _survey(root: int) -> dict:
    """The descendants of `root` /proc shows now, by pid, name and state: never proof, which
    is the supervisor's wait alone. An entry that could not be read is listed as such."""
    procs, unreadable = {}, []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/stat') as f:
                text = f.read()
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(int(name))
            continue
        head, _, tail = text.rpartition(')')
        fields = tail.split()
        procs[int(name)] = (head.partition('(')[2], fields[0], int(fields[1]))
    children = {}
    for pid, (_, _, ppid) in procs.items():
        children.setdefault(ppid, []).append(pid)
    found, todo = [], [root]
    while todo:
        for pid in children.get(todo.pop(), ()):
            found.append({'pid': pid, 'comm': procs[pid][0], 'state': procs[pid][1]})
            todo.append(pid)
    return {'processes': sorted(found, key=lambda p: p['pid']), 'unreadable': sorted(unreadable)}


def _bounded_survey(root: int) -> dict | None:
    # a /proc read can block (stat takes exec_update_lock), so the listing never holds up the report
    out = []
    t = threading.Thread(target=lambda: out.append(_survey(root)), daemon=True)
    t.start()
    t.join(SURVEY_BOUND)
    return out[0] if out else None


def _supervise(job: dict) -> int:
    """The supervisor (`python -I hil_process.py --supervise JOB`, started by run_tracked): a
    child subreaper owning one operation. It runs the command through _run, reports the result
    once waitpid finds no child left (done) or `settle` has passed (pending), and in that case
    keeps reaping until none is left. Every orphan in its subtree reparents to it, and a thread
    group leader with live threads is never reaped (kernel/exit.c, delay_group_leader), so
    ECHILD is proof. Its lock is held while it lives; `done` is written to its record first."""
    fd = job['report_fd']
    phase = {'now': 'setup', 'asked': False}

    def interrupt(*_):
        phase['asked'] = True
        if phase['now'] == 'run':
            phase['now'] = 'after'
            raise KeyboardInterrupt

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupt)
    record = {'token': job['token'], 'pid': os.getpid()}
    try:
        _become_subreaper()
        lock = os.open(job['lock'], os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)       # held until this process ends
        st = os.fstat(lock)
        record['lock'] = [st.st_dev, st.st_ino]
        with open('/proc/self/stat') as f:
            record['start'] = int(f.read().rpartition(')')[2].split()[19])
        _write_record(job['record'], {**record, 'cleanup': 'pending'})
    except Exception as e:
        _send(fd, {'result': _error(e), 'cleanup': 'done'})     # nothing started
        return 1
    _send(fd, {'hello': {'pid': record['pid'], 'start': record['start'], 'lock': record['lock']}})
    track = {}
    try:
        phase['now'] = 'run'
        if phase['asked']:
            raise KeyboardInterrupt
        r = _run(job['cmd'], job['cwd'], job['timeout'], job['binary'], job['split_stderr'], True,
                 job['reap_grace'], False, track)
        phase['now'] = 'after'
        result = {'returncode': r.returncode, 'stdout': _encode(r.stdout), 'stderr': _encode(r.stderr),
                  'killed': track.get('timed_out', False)}
    except KeyboardInterrupt:
        result = {'interrupted': True}
    except Exception as e:
        result = _error(e)
    phase['now'] = 'after'
    deadline, reported = time.monotonic() + job['settle'], False
    while True:
        try:
            pid, _ = os.waitpid(-1, WAIT_ALL | (0 if reported else os.WNOHANG))
        except ChildProcessError:
            break
        if pid or reported:
            continue
        if time.monotonic() < deadline:
            time.sleep(SETTLE_POLL)
            continue
        _send(fd, {'result': result, 'cleanup': 'pending', 'survivors': _bounded_survey(os.getpid())})
        os.close(fd)
        reported = True
    _write_record(job['record'], {**record, 'cleanup': 'done'})
    if not reported:
        _send(fd, {'result': result, 'cleanup': 'done'})
    return 0


class _Reports:
    """The supervisor's newline-delimited JSON messages, each read bounded by a deadline, from
    the pipe's read end, which it owns: close() is idempotent, the descriptor released at most
    once even when an interrupt lands right after the release (FileIO clears it before closing)."""

    def __init__(self, fd: int):
        self.pipe, self.buf, self.eof = open(fd, 'rb', buffering=0), bytearray(), False

    def close(self) -> None:
        self.pipe.close()

    def next(self, deadline: float) -> dict | None:
        """The next message; None at EOF, past `deadline` or for a malformed line."""
        poller = select.poll()
        poller.register(self.pipe, select.POLLIN)
        while b'\n' not in self.buf:
            left = deadline - time.monotonic()
            if self.eof or left <= 0 or not poller.poll(math.ceil(left * 1000)):
                return None
            chunk = self.pipe.read(65536)
            self.eof = not chunk
            self.buf += chunk
        line, _, rest = bytes(self.buf).partition(b'\n')
        self.buf = bytearray(rest)
        try:
            msg = json.loads(line)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None


def _final(reports: _Reports, receipt: dict, deadline: float, *, until_hello: bool = False) -> dict | None:
    """The supervisor's final report, or None; with `until_hello`, None once it said hello."""
    while (msg := reports.next(deadline)) is not None:
        if isinstance(msg.get('hello'), dict):
            receipt['supervisor'].update(start=msg['hello'].get('start'), lock=msg['hello'].get('lock'))
            if until_hello:
                return None
        elif 'result' in msg:
            return msg
    return None


def _verdict(final: dict | None, receipt: dict) -> tuple:
    if final is not None and final.get('cleanup') == 'done':
        return 'done', None
    if final is not None and final.get('cleanup') == 'pending':
        return 'pending', {**receipt, 'survivors': final.get('survivors')}
    return 'unknown', receipt


def _attach(target, cleanup: str, receipt: dict | None):
    target.cleanup, target.receipt = cleanup, receipt
    return target


def tidy(receipt) -> None:
    """Remove the files of a run whose receipt is `done`: nothing is left to recheck. Best
    effort: a file already gone, or a receipt without a usable path, is skipped."""
    for key in ('lock', 'record', 'log'):
        with contextlib.suppress(OSError, TypeError, KeyError):
            os.unlink(receipt[key])


def recheck(receipt: dict) -> str:
    """'done', 'pending' or 'unknown' for a run_tracked receipt, from its supervisor's own
    state: its lock, the file it created, is held while it lives, and its record, a file of
    ours, says `done` only once nothing is left. Anything unverifiable is unknown."""
    try:
        trusted_dir(os.path.dirname(receipt['lock']))
        fd = os.open(receipt['lock'], os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return 'unknown'
    try:
        st = os.fstat(fd)
        lock_id = [st.st_dev, st.st_ino]
        if (receipt.get('supervisor') or {}).get('lock') not in (None, lock_id):
            return 'unknown'
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'pending'
        try:
            with os.fdopen(os.open(receipt['record'], os.O_RDONLY | os.O_NOFOLLOW)) as f:
                rst = os.fstat(f.fileno())
                rec = json.load(f)
        except (OSError, ValueError):
            return 'unknown'
        ours = stat.S_ISREG(rst.st_mode) and rst.st_uid == os.geteuid() and not rst.st_mode & 0o022
        done = ours and isinstance(rec, dict) and rec.get('token') == receipt['token'] \
            and rec.get('lock') == lock_id and rec.get('cleanup') == 'done'
        return 'done' if done else 'unknown'
    finally:
        os.close(fd)


def run_tracked(cmd: str | list, cwd: str | None = None, *, timeout: float, settle: float, state_dir: str,
                binary: bool = False, split_stderr: bool = False, quiet: bool = False,
                reap_grace: float = REAP_GRACE, verbose: bool = False) -> subprocess.CompletedProcess:
    """run_cmd through an operation-owned supervisor, plus completion evidence (module contract)."""
    if timeout is None or settle is None or state_dir is None:
        raise ValueError('run_tracked needs an explicit timeout, settle and state_dir')
    try:
        # the job crosses to the supervisor as JSON: path-like arguments as run_cmd's Popen takes them
        argv = cmd if isinstance(cmd, str) else [os.fspath(a) for a in cmd]
        cwd = None if cwd is None else os.fspath(cwd)
        base = os.path.join(trusted_dir(state_dir), f'hil_op_{secrets.token_hex(8)}')
        log = open(f'{base}.log', 'xb')
    except BaseException as e:
        raise _attach(e, 'done', None)
    receipt = {'token': os.path.basename(base)[len('hil_op_'):], 'supervisor': None, 'lock': f'{base}.lock',
               'record': f'{base}.json', 'log': f'{base}.log', 'survivors': None}
    rfd, wfd = os.pipe()
    reports = _Reports(rfd)
    job = {'cmd': argv, 'cwd': cwd, 'timeout': timeout, 'settle': settle, 'reap_grace': reap_grace,
           'binary': binary, 'split_stderr': split_stderr, 'report_fd': wfd, 'token': receipt['token'],
           'lock': receipt['lock'], 'record': receipt['record']}
    hold = InterruptHold()
    try:
        # its own session, as run_cmd's child: the terminal's SIGINT reaches it only through us
        sup = subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), '--supervise', json.dumps(job)],
                               stdout=log, stderr=log, pass_fds=(wfd,), start_new_session=True)
    except BaseException as e:
        reports.close()
        os.close(wfd)
        log.close()
        _attach(e, 'done', None)        # no supervisor, so no operation either
        hold.release()
        raise
    os.close(wfd)
    log.close()
    receipt['supervisor'] = {'pid': sup.pid, 'start': None, 'lock': None}
    known = ('unknown', receipt)
    try:
        r, killed = _follow(sup, hold, reports, receipt, cmd, timeout, settle, reap_grace)
        known = r.cleanup, r.receipt
        if r.cleanup == 'done':
            tidy(receipt)
        _announce(cmd, r, timeout, killed, quiet, verbose)
        reports.close()
        return r
    except BaseException as e:
        # whatever escapes carries the latest evidence before any file goes, the pipe's closing included
        if not hasattr(e, 'cleanup'):
            _attach(e, *known)
        try:
            if e.cleanup == 'done':
                tidy(receipt)
            reports.close()
        except BaseException as again:
            raise _attach(again, e.cleanup, e.receipt)
        raise


def _follow(sup, hold: InterruptHold, reports: _Reports, receipt: dict, cmd, timeout, settle, reap_grace) -> tuple:
    """(result, killed by the timeout) from the supervisor's report, the result carrying the
    evidence; an exception carries it instead. It removes no file: that waits until the caller
    can show the verdict, or an interrupt in between would leave `unknown` with no evidence."""
    try:
        hold.release()      # a signal deferred across the spawn raises here, forwarded below
        final = _final(reports, receipt, time.monotonic() + timeout + reap_grace + settle + SUPERVISOR_MARGIN)
    except BaseException as e:
        try:
            deadline = time.monotonic() + settle + SUPERVISOR_MARGIN
            final = None
            if receipt['supervisor']['start'] is None:
                # before its hello the supervisor may not handle SIGINT yet, and would die of it
                final = _final(reports, receipt, deadline, until_hello=True)
            if final is None:
                # as run_cmd's interrupt path: the supervisor kills the group, then still reports
                with contextlib.suppress(OSError):
                    os.kill(sup.pid, signal.SIGINT)
                final = _final(reports, receipt, deadline)
        except BaseException as again:
            _attach(e, 'unknown', receipt)
            raise _attach(again, 'unknown', receipt)
        raise _attach(e, *_verdict(final, receipt))
    cleanup, rec = _verdict(final, receipt)
    res = final.get('result') if final else None
    try:
        if 'error' in res:
            exc = _rebuilt(res['error'])
        elif res.get('interrupted'):
            exc = RuntimeError('the supervisor was interrupted')
            exc.interrupted = True
        else:
            r = subprocess.CompletedProcess(cmd, res['returncode'], _decode(res['stdout']), _decode(res['stderr']))
            return _attach(r, cleanup, rec), bool(res.get('killed'))
    except (TypeError, KeyError, ValueError, AttributeError):
        exc = RuntimeError(f'the supervisor reported no usable result: {res!r}')
    raise _attach(exc, cleanup, rec)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--supervise':
        sys.exit('usage: hil_process.py --supervise JOB  (started by run_tracked)')
    sys.exit(_supervise(json.loads(sys.argv[2])))
