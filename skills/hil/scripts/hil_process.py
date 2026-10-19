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
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
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
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                p.kill()
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
    parts = [a if len(a) <= 60 else f'<{len(a)}-char program>' for a in cmd]
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
    try:
        hold.release()      # a signal deferred across the fork raises here, cleanup armed
        out, err = p.communicate(timeout=timeout)
        r = subprocess.CompletedProcess(args=cmd, returncode=p.returncode, stdout=out, stderr=err)
    except subprocess.TimeoutExpired as ex:
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
        _print_banner(f'COMMAND TIMEOUT ({timeout}s): {_cmd_label(cmd)}', timeout_out, timeout_err)
        return subprocess.CompletedProcess(args=cmd, returncode=124, stdout=timeout_out, stderr=timeout_err)
    except BaseException:
        # BaseException, not Exception (as in CPython's own subprocess.run):
        # KeyboardInterrupt is the case that matters, and start_new_session put the child in
        # its OWN group, so it never got the terminal's SIGINT -- without this, Ctrl-C
        # leaves the flasher or testusb holding the probe and its usbfs node. Kill and
        # close, never wait: this path must not add a hang of its own.
        _kill_group(p)
        _close_pipes(p)
        raise

    if r.returncode != 0 and not quiet:
        _print_banner(f'COMMAND FAILED: {_cmd_label(cmd)}', r.stdout, r.stderr)
    elif verbose:
        print(cmd)
        print(cmd_stdout_text(r.stdout))
    return r
