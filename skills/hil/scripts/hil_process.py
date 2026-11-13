#!/usr/bin/env python3
"""Bounded command runner shared by the HIL harnesses (`hil` skill, Flashers). Linux only,
stdlib only, no module state: every bound and switch is a parameter.

Contract:
- `cmd` a STRING runs through the shell; a LIST is an argv that goes straight to execve.
  `env`, a mapping of names to strings, is added to the inherited environment.
- The child runs in its own session (start_new_session), so the terminal's SIGINT never
  reaches it and its whole process group can be killed.
- On `timeout` the group is SIGTERMed, given TERM_GRACE s (at most half of `reap_grace`) to
  exit, then SIGKILLed and reaped for the rest of `reap_grace`, then
  a CompletedProcess with returncode 124 comes back: stdout/stderr text or bytes as the call
  asked, stderr merged into stdout or split as on the success path, never None when split.
- A survivor of that reap (D state, or a root child of sudo) is abandoned with our pipe ends
  closed and kept in no registry: rc 124 proves neither that every descendant died nor that
  the board it touched is safe.
- Spawn errors (FileNotFoundError, ...) propagate. SIGINT/SIGTERM handled while the child is
  being spawned are deferred into the cleanup `try`; any interrupt SIGTERMs the group, waits
  for it at most TERM_GRACE s (half of `reap_grace`), SIGKILLs it, closes our pipe ends without
  reaping, and propagates; a second interrupt in that wait cuts it short, and one during the
  timeout's stop kills at once, or once that stop's SIGKILL is sent, only closes our pipe ends.
  Either wait reads what the group writes to our pipes and ends early once the leader exited and
  /proc shows no other live member of its group; the leader stays unreaped until the SIGKILL
  (_stop_interrupted has the one case it cannot keep the pgid: an interrupt CPython's
  communicate() reaped behind).
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
  for as long as a receipt names the directory; `state_dir` creates one in the caller's run dir
  whatever the umask. Files of a `done` run are removed only once
  the verdict is on the result or exception; the others stay for recheck and are the
  caller's to remove (`tidy`, once a recheck answers `done`). A missing file reads as unknown,
  unless the lock is held: a supervisor still starting, pending. A supervisor whose hello
  cannot reach its caller runs nothing: its record says `done` and it exits.
  `recheck_dir` rechecks and tidies every operation in a state dir from its files alone; one of
  another uid, whose files it may not trust, is pending while /proc shows a supervisor of an
  operation there (its command line) or may hide one (hidepid, an unreadable command line),
  else unknown.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import errno
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

FILES = ()   # launches only itself by path (hil_load.py)

# Post-SIGKILL reap, spent ON TOP of a timeout whenever the child has to be killed. A caller
# budgeting several bounded steps must add one of these PER STEP, or its own outer bound fires
# mid-step -- for a flasher, orphaning it on the probe.
REAP_GRACE = 10
# s of a reap_grace a group gets to exit on SIGTERM before SIGKILL: an openocd SIGKILLed on a J-Link never
# unregisters from the probe, which refuses new connections once the leaked registrations add up (agentrc#286)
TERM_GRACE = 3


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


def _kill_group(p: subprocess.Popen, sig: int = signal.SIGKILL) -> None:
    """Send `sig` to the child's process group. Never raises: ProcessLookupError is a group already
    gone, PermissionError an all-root group refusing the kill, and either escaping would skip
    the caller's bounded reap, pipe close and rc-124 return."""
    try:
        os.killpg(p.pid, sig)
    except OSError:
        pass


def _term_wait(reap_grace: float) -> float:
    return min(TERM_GRACE, reap_grace / 2)


GROUP_POLL = 0.05


def _stat_fields(path: str) -> list | None:
    """The fields of a /proc stat file after the comm; None when it is gone. OSError when unreadable."""
    try:
        with open(path, errors='replace') as f:
            return f.read().rpartition(')')[2].split()
    except FileNotFoundError:
        return None


def _group_survivors(pgid: int) -> bool:
    """Whether /proc shows a live member of group `pgid` other than its leader: a task not a zombie,
    or a zombie thread-group leader with a live thread (kernel/exit.c delay_group_leader); an
    unreadable entry counts as one."""
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name) == pgid:
            continue
        try:
            fields = _stat_fields(f'/proc/{name}/stat')
            if fields is None or int(fields[2]) != pgid:
                continue
            if fields[0] not in 'ZX':
                return True
            for tid in os.listdir(f'/proc/{name}/task'):
                t = _stat_fields(f'/proc/{name}/task/{tid}/stat')
                if t is not None and t[0] not in 'ZX':
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _drain(p: subprocess.Popen, until: float, eof: set) -> None:
    """Read what the group writes to our pipes until `until` (monotonic): a member blocked on a full
    pipe could not finish its TERM cleanup. It goes to the buffers a started communicate() returns
    it from (CPython's POSIX _fileobj2output), else nowhere. A pipe at EOF joins `eof` (its fd), left
    open for communicate() to close."""
    bufs = getattr(p, '_fileobj2output', {})
    pipes = {f.fileno(): f for f in (p.stdout, p.stderr) if f is not None and not f.closed and f.fileno() not in eof}
    poller = select.poll()
    for fd in pipes:
        poller.register(fd, select.POLLIN)
    while (left := until - time.monotonic()) > 0:
        for fd, _ in poller.poll(math.ceil(left * 1000)):
            if data := os.read(fd, 32768):
                bufs.get(pipes[fd], []).append(data)
            else:
                eof.add(fd)
                poller.unregister(fd)


def _await_group(p: subprocess.Popen, deadline: float) -> bool:
    """Wait until `deadline` (monotonic) for the leader to exit and /proc to show no other live
    member of its group, draining our pipes meanwhile: True once it does (best-effort: a look can
    miss a member forked during it), False at the deadline, each /proc look bounded by what is left.
    The leader is never reaped here: its zombie keeps the pgid ours for a SIGKILL that follows."""
    last, eof = [], set()
    while True:
        try:
            exited = p.returncode is not None or os.waitid(os.P_PID, p.pid,
                                                           os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            exited = True
        left = deadline - time.monotonic()
        if exited and _bounded(lambda: _group_survivors(p.pid), max(0.0, left), last) is False:
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        _drain(p, time.monotonic() + min(GROUP_POLL, left), eof)


def _stop_group(p: subprocess.Popen, reap_grace: float, stop: dict) -> tuple:
    """The timeout's stop: SIGTERM the group, wait for it _term_wait(reap_grace), SIGKILL whatever is
    left (a member that ignored the TERM) and reap the leader for the rest of reap_grace: (out,
    err). TimeoutExpired when something outlived the SIGKILL. The timed-out communicate() reaped
    nothing, so the pgid stays ours until that reap. `stop` gets 'killed' once the SIGKILL is sent:
    an interrupt after it must not signal the group again, its leader perhaps reaped by communicate()."""
    term = _term_wait(reap_grace)
    _kill_group(p, signal.SIGTERM)
    _await_group(p, time.monotonic() + term)
    _kill_group(p)
    stop['killed'] = True
    return p.communicate(timeout=reap_grace - term)


def _stop_interrupted(p: subprocess.Popen, reap_grace: float) -> None:
    """An interrupt's cleanup within _term_wait(reap_grace): SIGTERM the group and wait for it, then
    SIGKILL it, and close our pipe ends, never reaping. A second interrupt ends the wait, never the
    kill or the close. Raises nothing of its own.

    CPython's communicate() reaps a leader that exits in its SIGINT wait, after which the pgid is
    ours only while a member lives: a group /proc then shows empty is not signalled at all, its
    number free for reuse. Members ending between the last look and a signal, or one forked
    during a look and missed by it, remain a gap there."""
    deadline, empty = time.monotonic() + _term_wait(reap_grace), False
    try:
        empty = p.returncode is not None and \
            _bounded(lambda: _group_survivors(p.pid), max(0.0, deadline - time.monotonic())) is False
        if not empty:
            _kill_group(p, signal.SIGTERM)
            empty = _await_group(p, deadline)
    except BaseException:       # a second interrupt: the kill below either way
        pass
    if not empty or p.returncode is None:      # a /proc look can miss a member forked during it
        _kill_group(p)
    _close_pipes(p)


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

    deliver() lets signals through to the held handlers until `deferring` is set again: a
    plain attribute store, unlike a call's entry, is no interrupt check, so a `finally` or
    `except` that sets it first defers from its first instruction.
    """

    def __init__(self):
        self._held = {}
        self._pending = []
        self._released = False
        self.deferring = True
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
        if self._released or not self.deferring:      # released: left installed by a signal that cut release() short
            return self._held[sig](sig, frame)
        self._pending.append(sig)

    def deliver(self) -> None:
        """Stop deferring, then raise what arrived meanwhile."""
        self.deferring = False
        pending, self._pending = self._pending, []
        for sig in dict.fromkeys(pending):
            signal.raise_signal(sig)

    def rearm(self) -> None:
        """Defer again, the deferring handler put back where one installed since replaced it
        (release() restores the handlers found at construction). signal.signal runs a pending
        signal's handler before it swaps, so one already pending as this starts is not deferred."""
        self.deferring = True
        for sig in self._held:
            if signal.getsignal(sig) != self._defer:
                signal.signal(sig, self._defer)

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

    reaped = stopping = False
    stop = {}

    def _reap() -> subprocess.CompletedProcess:
        nonlocal reaped, stopping
        try:
            out, err = p.communicate(timeout=timeout)
            reaped = True
            return subprocess.CompletedProcess(argv, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            stopping = True
            try:
                out, err = _stop_group(p, reap_grace, stop)
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
            # interrupt is not reaped: it takes the interrupt's bounded stop below.
            _reap()
            raise
        return _reap()
    except BaseException:
        # Interrupted before a reap completed: as run_cmd's interrupt path. Not keyed on
        # returncode: communicate() reaps the leader on KeyboardInterrupt, its group living on.
        # The pgid stays ours while any member lives.
        if stopping and not reaped:     # interrupted in the timeout's stop: kill unless it had, never wait again
            if 'killed' not in stop:
                _kill_group(p)
            _close_pipes(p)
        elif not reaped:
            _stop_interrupted(p, reap_grace)
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
            reap_grace: float = REAP_GRACE, verbose: bool = False,
            env: dict | None = None) -> subprocess.CompletedProcess:
    """Bounded subprocess: own session, killpg on expiry, rc 124 when it had to be killed.

    `cmd` is a shell STRING or an argv LIST. argv exists for a program that cannot survive
    a trip through the shell -- a multi-line `python3 -c` body, a path with spaces -- and is
    what every hil_flash builder produces; `env` adds its variables to the inherited
    environment, so no `NAME=value` prefix needs a shell either. A daemon thread cannot bound
    a C call that holds the GIL, so for those the child process IS the bound.
    """
    return _run(cmd, cwd, timeout, binary, split_stderr, quiet, reap_grace, verbose, env=env)


def _environ(env) -> dict | None:
    """The child's whole environment: ours plus `env`, refused unless it maps names to strings."""
    if env is None:
        return None
    if not isinstance(env, dict) or not all(isinstance(k, str) and k and '=' not in k and '\0' not in k
                                            and isinstance(v, str) and '\0' not in v for k, v in env.items()):
        raise ValueError(f'env {env!r}: want a mapping of variable names to strings')
    return {**os.environ, **env}


def _run(cmd, cwd, timeout, binary, split_stderr, quiet, reap_grace, verbose,
         track: dict | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
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
        'env': _environ(env),
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
    timed_out, stop = False, {}
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
        try:
            out, err = _stop_group(p, reap_grace, stop)
        except subprocess.TimeoutExpired:
            # Something in the group outlived SIGKILL: D state (truly unkillable), or
            # root-owned because sudo FORKS rather than execs, so the wrapper dies and its
            # root child does not. Abandon it; the harness never
            # sudo-kills its way out. Our ends of its pipes must not leak, though: a pool
            # worker lives for the whole run, so every wedged command would cost it two fds.
            out, err = None, None
            _close_pipes(p)
        except BaseException:
            # interrupted mid-reap, perhaps still in its TERM wait: kill unless the stop had, never wait
            # again; the BaseException handler below is a sibling
            if 'killed' not in stop:
                _kill_group(p)
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
        # leaves the flasher or testusb holding the probe and its usbfs node. Its wait is
        # bounded and a second interrupt ends it: this path must not add a hang of its own.
        _stop_interrupted(p, reap_grace)
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
PROC_SCAN_BOUND = 5.0       # another uid's supervisors listed from /proc: past it, nothing is proven
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


def _send(fd: int, msg: dict) -> bool:
    """Whether `msg` reached the pipe; False once the caller is gone, the record still saying how this ends."""
    data = (json.dumps(msg) + '\n').encode()
    try:
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        return False
    return True


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


def _own_dir(path: str, mode: int) -> None:
    """`path`, absolute and normalized, created if missing, then set to `mode` whatever the umask; refused
    (PermissionError), and never chmodded, unless its last component is a real directory of ours. A missing ancestor
    is made the same way at 0o755; an existing one is the caller's to trust, as in trusted_dir."""
    head, tail = os.path.split(path)
    if not tail:
        raise PermissionError(f'{path}: want a directory below /')
    if not os.path.lexists(head):
        _own_dir(head, 0o755)
    parent = os.open(head, os.O_PATH | os.O_DIRECTORY)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(tail, mode, dir_fd=parent)
        try:
            # O_PATH needs no read permission on the directory itself (an umask of 0777, a mode of 0300)
            fd = os.open(tail, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise PermissionError(f'{path}: want a directory, not a link or a file') from e
            raise
    finally:
        os.close(parent)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise PermissionError(f'{path}: want a directory of uid {os.geteuid()}')
        os.chmod(f'/proc/self/fd/{fd}', mode)    # fchmod refuses an O_PATH descriptor; this names its inode
    finally:
        os.close(fd)


def state_dir(parent: str, name: str = 'ops') -> str:
    """A run_tracked state dir: `parent` (the caller's run dir) and `parent/name` created as needed and set to 0o755
    and 0o700 whatever the umask, then returned through trusted_dir. An existing one of them is secured only when it
    is a real directory of ours; a link or another user's is refused (PermissionError) and left as it is, however
    `parent` is spelled (a trailing slash, `.` or `..` are resolved lexically first). Missing ancestors of `parent`
    are created 0o755."""
    if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\0' in name:
        raise ValueError(f'state dir name {name!r}: want a single path component')
    parent = os.path.abspath(os.fspath(parent))
    _own_dir(parent, 0o755)
    path = os.path.join(parent, name)
    _own_dir(path, 0o700)
    return trusted_dir(path)


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
            with open(f'/proc/{name}/stat', errors='replace') as f:   # comm may be cut mid-UTF-8 character
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


_bounded_start = threading.Lock()


def _bounded(fn, bound: float, last: list | None = None, *, reraise: bool = False):
    # a /proc read can block (stat takes exec_update_lock, cmdline the mm's lock): fn() or None past `bound` s;
    # `last` keeps the thread: until its fn has returned no other starts and None is returned at once. fn's
    # exception is raised here with `reraise`, else it ends the thread (threading.excepthook) as None.
    # The thread's own `finished` event, not is_alive(): CPython 3.12's Thread.join, interrupted, marks it stopped
    out, finished = [], threading.Event()

    def run():
        try:
            out.append((True, fn()))
        except BaseException as e:
            out.append((False, e))
            if not reraise:
                raise
        finally:
            finished.set()
    t = threading.Thread(target=run, daemon=True)
    t.finished = finished
    hold = InterruptHold()      # an interrupt between recording t and its start would gate `last` forever
    try:
        with _bounded_start:
            if last is not None:
                if any(not x.finished.is_set() for x in last):
                    return None
                last[:] = [t]
            try:
                t.start()
            except Exception:
                finished.set()      # it never started: nothing for `last` to wait for
                raise
    finally:
        hold.release()
    finished.wait(bound)
    if not out:
        return None
    ok, value = out[0]
    if ok:
        return value
    if reraise:
        raise value
    return None


def _bounded_survey(root: int) -> dict | None:
    return _bounded(lambda: _survey(root), SURVEY_BOUND)


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
        with open('/proc/self/stat', errors='replace') as f:
            record['start'] = int(f.read().rpartition(')')[2].split()[19])
        _write_record(job['record'], {**record, 'cleanup': 'pending'})
    except Exception as e:
        _send(fd, {'result': _error(e), 'cleanup': 'done'})     # nothing started
        return 1
    if not _send(fd, {'hello': {'pid': record['pid'], 'start': record['start'], 'lock': record['lock']}}):
        # the caller died before its hello and may have been judged by its files alone: the board may be another's
        _write_record(job['record'], {**record, 'cleanup': 'done'})
        return 1
    track = {}
    try:
        phase['now'] = 'run'
        if phase['asked']:
            raise KeyboardInterrupt
        r = _run(job['cmd'], job['cwd'], job['timeout'], job['binary'], job['split_stderr'], True,
                 job['reap_grace'], False, track, job.get('env'))
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
    ours, says `done` only once nothing is left. Anything unverifiable is unknown, a receipt
    without a token or paths included (recheck_dir's `unrecheckable` entries). An entry recheck_dir
    made for a state dir of another uid is rechecked from /proc alone, or as a dir of ours by its owner
    (_foreign_state)."""
    if isinstance(receipt, dict) and 'foreign' in receipt:
        return _foreign_state(receipt)
    try:
        token = receipt['token']
        trusted_dir(os.path.dirname(receipt['lock']))
        fd = os.open(receipt['lock'], os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, KeyError, TypeError):
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
        done = ours and isinstance(rec, dict) and rec.get('token') == token \
            and rec.get('lock') == lock_id and rec.get('cleanup') == 'done'
        return 'done' if done else 'unknown'
    finally:
        os.close(fd)


_OP_PREFIX, _OP_FILES = 'hil_op_', ('lock', 'json', 'log')


def _receipt(base: str, token: str, supervisor) -> dict:
    """The receipt of the operation whose files are `base`.{lock,json,log}."""
    return {'token': token, 'supervisor': supervisor, 'lock': f'{base}.lock', 'record': f'{base}.json',
            'log': f'{base}.log', 'survivors': None}


def _held_lock(path: str) -> list | None:
    """[dev, ino] of the lock at `path` while a process holds it, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        st = os.fstat(fd)
        return [st.st_dev, st.st_ino]
    except OSError:
        pass
    finally:
        os.close(fd)
    return None


def _dir_receipt(state_dir: str, token: str, found: set) -> dict:
    """The receipt of `token`'s files, the supervisor's identity from its record, or from its held lock
    alone while a file is missing (a supervisor still starting, which recheck answers pending); else a
    diagnostic entry: `token`, `unrecheckable` (why) and the `files` there, which recheck answers unknown."""
    base = os.path.join(state_dir, f'{_OP_PREFIX}{token}')
    paths = {ext: f'{base}.{ext}' for ext in _OP_FILES}

    def unrecheckable(why: str) -> dict:
        return {'token': token, 'unrecheckable': why, 'files': [paths[ext] for ext in _OP_FILES if ext in found]}
    if missing := [ext for ext in _OP_FILES if ext not in found]:
        if 'lock' in found and (held := _held_lock(paths['lock'])):
            return _receipt(base, token, {'pid': None, 'start': None, 'lock': held})
        return unrecheckable(f'no {" or ".join(f".{ext}" for ext in missing)} file')
    try:
        with os.fdopen(os.open(paths['json'], os.O_RDONLY | os.O_NOFOLLOW)) as f:
            rec = json.load(f)
    except (OSError, ValueError) as e:
        return unrecheckable(f'record unreadable: {e}')
    if not isinstance(rec, dict) or rec.get('token') != token:
        return unrecheckable('record of another operation')
    lock = rec.get('lock')
    if not (isinstance(lock, list) and len(lock) == 2 and all(type(v) is int for v in lock)):
        return unrecheckable('record names no lock')
    return _receipt(base, token, {'pid': rec.get('pid'), 'start': rec.get('start'), 'lock': lock})


def _proc_exemptions() -> tuple[bool, set] | None:
    """(CAP_SYS_PTRACE effective, our fsgid and supplementary gids) from /proc/self/status; None when unreadable, or
    outside the init user namespace: a hidepid mount's ptrace check and gid= are against init's caps and gids."""
    try:
        if os.readlink('/proc/self/ns/user') != f'user:[{0xEFFFFFFD}]':       # PROC_USER_INIT_INO
            return None
        with open('/proc/self/status', errors='replace') as f:
            status = dict(line.split(':', 1) for line in f if ':' in line)
        return (int(status['CapEff'], 16) >> 19 & 1 == 1,                       # CAP_SYS_PTRACE
                {int(status['Gid'].split()[3]), *map(int, status['Groups'].split())})
    except (OSError, KeyError, ValueError, IndexError):
        return None


def _proc_hides_others() -> bool:
    """Whether /proc may hide another uid's processes from us: mounted hidepid other than 0 (proc(5)), unless we have
    CAP_SYS_PTRACE or, for hidepid 1 or 2, are in its gid= group, 0 when absent (fs/proc/base.c has_pid_permissions);
    or its mount unreadable, or our status for a mount that needs it (_proc_exemptions)."""
    try:
        with open('/proc/self/mountinfo', errors='replace') as f:
            lines = [line.split() for line in f]
    except OSError:
        return True
    procs = [f for f in lines if len(f) > 4 and f[4] == '/proc' and '-' in f and f[f.index('-') + 1:][:1] == ['proc']]
    if not procs:
        return True
    opts = ','.join(procs[-1][5:]).split(',')          # the mount's options and its super options
    hidepid = [o[len('hidepid='):] for o in opts if o.startswith('hidepid=')]
    if not hidepid or hidepid[-1] in ('0', 'off'):
        return False
    exempt = _proc_exemptions()
    if exempt is None:
        return True
    ptrace, gids = exempt
    if ptrace:
        return False
    if hidepid[-1] not in ('1', 'noaccess', '2', 'invisible'):
        return True                 # ptraceable, or a mode we do not know: gid= exempts no one
    gid = ([o[len('gid='):] for o in opts if o.startswith('gid=')] or ['0'])[-1]
    return not (gid.isdigit() and int(gid) in gids)


def _supervisors_in(state_dir: str) -> list | None:
    """[pid] of the first process /proc shows running a supervisor (this file's `--supervise JOB`, as run_tracked
    starts it) of an operation in `state_dir`, from command lines alone, else []; None when /proc may not show every
    process to us."""
    if _proc_hides_others():
        return None
    dirs = {os.path.abspath(state_dir), os.path.realpath(state_dir)}
    try:
        names = os.listdir('/proc')
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/cmdline', 'rb') as f:
                argv = f.read().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue                    # gone
        except OSError:
            return None
        if b'--supervise' not in argv:
            continue
        try:
            lock = json.loads(argv[argv.index(b'--supervise') + 1])['lock']
        except (IndexError, ValueError, TypeError, KeyError):
            continue
        if isinstance(lock, str) and os.path.dirname(lock) in dirs:
            return [int(name)]          # one is proof enough: read no command line that may block after it
    return []


_proc_scan: list = []       # _foreign_state's last scan thread


class _KeepAll:
    """recheck_dir's `keep` holding every token: a recheck that tidies nothing."""

    def __contains__(self, _token) -> bool:
        return True


def _foreign_state(entry: dict) -> str:
    """For the entry's state dir's owner, once trusted_dir passes, recheck_dir's verdict, tidying nothing: the files
    of other receipts logged against it stay. For any other uid, 'pending' while /proc shows a supervisor of an
    operation there, or cannot show every process within PROC_SCAN_BOUND, or an earlier scan is still blocked (one
    such thread, not one per poll); 'unknown' once it shows none: the dir's records, of another uid, prove no `done`.
    A supervisor lives until it has reaped every descendant, so /proc shows it for as long as its lock is held."""
    d = entry.get('foreign')
    try:
        trusted_dir(d)
    except (OSError, TypeError, ValueError):
        pass
    else:
        return recheck_dir(d, _KeepAll())[0]
    if not isinstance(d, str):
        return 'pending'
    return 'unknown' if _bounded(lambda: _supervisors_in(d), PROC_SCAN_BOUND, _proc_scan) == [] else 'pending'


def _foreign_entry(state_dir: str) -> dict | None:
    """recheck_dir's entry for a state dir of another uid, or one beyond a directory we cannot search: its files
    are not ours to trust, so it is rechecked from /proc (_foreign_state), `uid` its owner when known. None for
    any other."""
    try:
        st = os.stat(state_dir)
    except PermissionError:
        uid = None
    except (OSError, TypeError, ValueError):
        return None
    else:
        if not stat.S_ISDIR(st.st_mode) or st.st_uid == os.geteuid():
            return None
        uid = st.st_uid
    path = os.path.abspath(state_dir)
    return {'token': f'foreign:{path}', 'foreign': path, 'uid': uid}


def recheck_dir(state_dir: str, keep=()) -> tuple:
    """(verdict, open entries) for every run_tracked operation with a file in `state_dir`, from
    those files alone. Each is rebuilt as a receipt whose supervisor identity is its record's,
    and rechecked; one whose lock, record or log is missing, or whose record is unreadable or
    names no lock of its own, is unknown, except that a missing file with the lock held is pending,
    its receipt naming that lock alone. A `done` one is tidied unless its token is in `keep`.
    verdict: 'done' when every one is done (none found included), 'pending' when the open ones
    all are, else 'unknown'. Open entries: receipts recheck and tidy accept, or diagnostics with
    `unrecheckable` (why) and the `files` left, which recheck answers unknown. A state_dir of another
    uid, or one we cannot reach, is (its recheck, [{'token', 'foreign': its path, 'uid'}]): 'pending'
    while /proc shows a supervisor of an operation there or cannot show every process, else
    'unknown', and nothing in it is read or tidied; any other untrusted or unreadable state_dir is
    ('unknown', [{'unrecheckable': why}])."""
    try:
        real = trusted_dir(state_dir)
        names = os.listdir(real)
    except OSError as e:
        if (entry := _foreign_entry(state_dir)) is not None:
            return recheck(entry), [entry]
        return 'unknown', [{'unrecheckable': f'{state_dir}: {e}'}]
    found = {}
    for name in names:
        stem, _, ext = name.partition('.')
        if stem.startswith(_OP_PREFIX) and stem != _OP_PREFIX and ext in _OP_FILES:
            found.setdefault(stem[len(_OP_PREFIX):], set()).add(ext)
    verdicts, entries = set(), []
    for token in sorted(found):
        entry = _dir_receipt(real, token, found[token])
        verdict = 'unknown' if 'unrecheckable' in entry else recheck(entry)
        if verdict == 'done':
            if token not in keep:
                tidy(entry)
            continue
        verdicts.add(verdict)
        entries.append(entry)
    return ('done' if not verdicts else 'pending' if verdicts == {'pending'} else 'unknown'), entries


def run_tracked(cmd: str | list, cwd: str | None = None, *, timeout: float, settle: float, state_dir: str,
                binary: bool = False, split_stderr: bool = False, quiet: bool = False,
                reap_grace: float = REAP_GRACE, verbose: bool = False,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """run_cmd through an operation-owned supervisor, plus completion evidence (module contract)."""
    if timeout is None or settle is None or state_dir is None:
        raise ValueError('run_tracked needs an explicit timeout, settle and state_dir')
    try:
        _environ(env)       # refused here, before anything starts, not in the supervisor
        # the job crosses to the supervisor as JSON: path-like arguments as run_cmd's Popen takes them
        argv = cmd if isinstance(cmd, str) else [os.fspath(a) for a in cmd]
        cwd = None if cwd is None else os.fspath(cwd)
        token = secrets.token_hex(8)
        base = os.path.join(trusted_dir(state_dir), f'{_OP_PREFIX}{token}')
        log = open(f'{base}.log', 'xb')
    except BaseException as e:
        raise _attach(e, 'done', None)
    receipt = _receipt(base, token, None)
    rfd, wfd = os.pipe()
    reports = _Reports(rfd)
    job = {'cmd': argv, 'cwd': cwd, 'env': env, 'timeout': timeout, 'settle': settle, 'reap_grace': reap_grace,
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
        tidy(receipt)
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
            deadline = time.monotonic() + _term_wait(reap_grace) + settle + SUPERVISOR_MARGIN
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
