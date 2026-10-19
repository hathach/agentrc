#!/usr/bin/env python3
"""The `openocd_esp` flasher (`hil` skill, Flashers): an Espressif chip's built-in
USB-Serial/JTAG (USJ) driven by openocd-esp32. Linux only, stdlib only.

A flasher is {"name": "openocd_esp", "uid": "<USJ serial>", "args": "-f board/<chip>-builtin.cfg"},
or args "-f interface/esp_usb_jtag.cfg -f target/<chip>.cfg"; any other args, a `vid_pid` or
an openocd that is not the verified openocd-esp32 build is refused before anything runs.

Selection: the USJ's bus-port is resolved from sysfs by its serial, exactly one match, and
passed as `adapter usb location` before `adapter serial`, both before init. esp_usb_jtag_init
opens through jtag_libusb_open, which skips a device outside the location before libusb_open
but compares the serial only after opening (libusb_helper.c:272-286), so the serial alone
would open every 303a:1001 ahead of the board's in libusb order. Not convoy-safe even pinned:
esp_usb_jtag_revive_device (esp_usb_jtag.c:313-350, on a transfer error) finds the device
again through jtag_libusb_find_device, which opens every 303a:1001 to read its serial and
ignores the location (libusb_helper.c:500-540). openocd-esp32 source citations are at tag
v0.12.0-esp32-20260703.

Session: one openocd server per operation, started with `noinit` in its own session (the
terminal's SIGINT never reaches it) and driven over a Tcl port that must first answer this
session's token (target-debug's esp_session.py Tcl). A command fails when it answers an error
or when its own log lines show a target event hook that failed (`Execution of event <name>
failed:`, such as reset-assert-post during a reset): target_handle_event logs the hook's
error and does not propagate it (target.c), so the command still answers success. Its pid,
start time, argv, port, token and log are written to <dir>/receipt.json, in esp_session.py's
receipt format, before an interrupt can be acted on. It is never signalled or killed. The
reviewed end is `reset run`, then `shutdown` and its exit confirmed within CLEANUP_TIMEOUT;
it follows success and a command that failed and returned. An init that failed, a command
still outstanding (timed out, interrupted, its connection lost or answered by a foreign
server) or a reset run that failed gets no reset and no shutdown: the operation's cleanup is
`pending` and its receipt names the server left running, for the caller's cleanup holder. A
server that exited on its own leaves cleanup `unknown` (`done` when it exited before init was
asked, so no adapter was opened). There is no bare init/shutdown: examining a running app
built with the end-of-stack watchpoint panics it, and only the reset run brings it back.

The session holds an exclusive flock on <dir>/receipt.json.lock, a file never replaced, from
before the spawn until its operation is built; the openocd child never inherits it. The
receipt is replaced atomically and also records `init` (sent, ok, failed) and `submitted`,
the sequence number of the last command sent, persisted before it is sent; every command sets
the server's Tcl global `hil_done` to its number once it has run. `end` (below) ends a pending
session from its receipt:

  hil_openocd_esp.py end --receipt <dir>/receipt.json [--timeout S]
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import importlib.util
import json
import os
import re
import shlex
import secrets
import select
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usb_observe = _load('usb_observe', os.path.join(HERE, 'usb_observe.py'))
hil_process = _load('hil_process', os.path.join(HERE, 'hil_process.py'))
hil_flash = _load('hil_flash', os.path.join(HERE, 'hil_flash.py'))
# Tcl token check, receipts and process identity; a consumer staging this skill stages it too
ESP_SESSION = os.path.join(HERE, '..', '..', 'target-debug', 'scripts', 'esp_session.py')
esp_session = _load('esp_session', ESP_SESSION)

# The build whose program_esp, flash write_image/read_bank, esp_usb_jtag and shutdown paths were
# read; another build is refused until its source is checked again. The pin also covers the `esp`
# subcommands the writes use (compression, encrypt_binary, flash_stub_clock_boost,
# verify_bank_hash): C commands of this build, which `info commands` does not list.
VERIFIED_OPENOCD = 'v0.12.0-esp32-20260703'
OPENOCD_ENV = 'HIL_OPENOCD_ESP'
USJ_VID_PID = ('303a', '1001')       # interface/esp_usb_jtag.cfg's `adapter usb vid_pid`
# adapter.c:324-331 warns about a location of 16 characters and truncates a longer one
USB_LOCATION_MAX = 15

CLEANUP_TIMEOUT = 30                 # seconds for the reviewed end: reset run, shutdown, exit
# a GitHub Actions job sets these, and one set even to '' counts: the runner kills the processes
# carrying the job's tracking id at job end (actions/runner v2.337.0 src/Runner.Worker/JobExtension.cs:586-592, 880-926)
CI_JOB_ENV = ('RUNNER_TRACKING_ID', 'GITHUB_ACTIONS')

_UID_RE = re.compile(r'[A-Za-z0-9_.:+-]+')
_PATH_RE = re.compile(r'/[A-Za-z0-9_./+-]*')    # a path that reaches a Tcl command unquoted
_CHIP = r'esp32[a-z0-9]*'


def ci_refusal() -> str:
    """Why a session is refused in this process, or '': flash and reset raise it before anything
    starts, and a consumer checks it before its own preparatory sessions. `end` is not refused."""
    job = [v for v in CI_JOB_ENV if v in os.environ]
    if not job:
        return ''
    return (f'openocd_esp is refused under a CI job ({", ".join(job)} set): the runner kills the job\'s '
            f'processes at its end, and that the openocd server or a cleanup holder survives it is not established')


def _refuse_ci():
    if why := ci_refusal():
        raise RuntimeError(why)


def admit(flasher: dict) -> tuple:
    """(chip, cfg files) of an `openocd_esp` flasher, or ValueError: `args` names one board cfg
    or the USJ interface and a target cfg, nothing that selects an adapter, runs a command or
    initialises early."""
    if (flasher.get('name') or '') != 'openocd_esp':
        raise ValueError(f'not an openocd_esp flasher: {flasher.get("name")!r}')
    uid = flasher.get('uid')
    if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
        raise ValueError(f'openocd_esp uid {uid!r}: want the USJ serial (letters, digits and _ . : + -)')
    if 'vid_pid' in flasher:
        raise ValueError('openocd_esp takes no vid_pid: interface/esp_usb_jtag.cfg selects 303a:1001 and the '
                         'bus-port pins the device')
    args = shlex.split(flasher.get('args') or '')
    m = len(args) == 2 and args[0] == '-f' and re.fullmatch(rf'board/({_CHIP})-builtin\.cfg', args[1])
    if m:
        return m[1], [args[1]]
    m = (len(args) == 4 and args[:3] == ['-f', 'interface/esp_usb_jtag.cfg', '-f']
         and re.fullmatch(rf'target/({_CHIP})\.cfg', args[3]))
    if m:
        return m[1], [args[1], args[3]]
    raise ValueError(f'openocd_esp args {flasher.get("args")!r}: want "-f board/<chip>-builtin.cfg" or '
                     f'"-f interface/esp_usb_jtag.cfg -f target/<chip>.cfg" and nothing else')


def find_openocd(openocd: str | None = None) -> dict:
    """{exe, banner, scripts} of the verified openocd-esp32: `openocd`, else $HIL_OPENOCD_ESP,
    else `openocd` on PATH. Its scripts are the install's own share/openocd/scripts, passed
    with -s, so another install's cfgs are never sourced. RuntimeError otherwise."""
    name = openocd or os.environ.get(OPENOCD_ENV) or 'openocd'
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f'{name} not found: pass openocd= or set {OPENOCD_ENV}')
    exe = os.path.abspath(exe)         # the server runs in a private cwd
    try:
        r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f'{exe} --version: {e}') from None
    lines = (r.stdout + r.stderr).strip().splitlines()
    banner = lines[0] if lines else ''
    if VERIFIED_OPENOCD not in banner.split():
        raise RuntimeError(f'{exe}: banner {banner!r} is not openocd-esp32 {VERIFIED_OPENOCD}, the build this '
                           f'backend was checked against')
    scripts = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(exe)), '..', 'share', 'openocd',
                                            'scripts'))
    try:
        with open(os.path.join(scripts, 'target', 'esp_common.cfg')) as f:
            common = f.read()
    except OSError as e:
        raise RuntimeError(f'{exe}: no openocd-esp32 scripts beside it ({e})') from None
    if 'proc check_image_revision ' not in common:
        raise RuntimeError(f'{scripts}/target/esp_common.cfg defines no check_image_revision')
    return {'exe': exe, 'banner': banner, 'scripts': scripts}


def check_cfgs(chip: str, cfgs: list, scripts: str) -> None:
    """The route's cfgs exist in the verified scripts, the chip has a USJ board cfg there, and
    that board cfg only sources the USJ interface and the chip's target (no selector, no init)."""
    board = os.path.join(scripts, 'board', f'{chip}-builtin.cfg')
    try:
        with open(board) as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith('#')]
    except OSError:
        raise ValueError(f'{chip}: openocd-esp32 has no board/{chip}-builtin.cfg, so no built-in USB-JTAG') from None
    if lines != ['source [find interface/esp_usb_jtag.cfg]', f'source [find target/{chip}.cfg]']:
        raise ValueError(f'{board}: not the plain USJ board cfg (sources more than the interface and target)')
    for cfg in cfgs:
        if not os.path.isfile(os.path.join(scripts, cfg)):
            raise ValueError(f'{cfg} is not in {scripts}')


def _attr(d, name):
    # idVendor, idProduct, busnum and devnum are lock-free sysfs_emit reads of cached fields
    with open(os.path.join(d, name)) as f:
        return f.read().strip()


def _usj_walk(serial: str) -> list:
    found, unknown = [], []
    for d in sorted(glob.glob(os.path.join(usb_observe.SYS_USB_DEVICES, '*-*'))):
        busport = os.path.basename(d)
        if ':' in busport:
            continue                  # an interface, not a device
        try:
            if (_attr(d, 'idVendor'), _attr(d, 'idProduct')) != USJ_VID_PID:
                continue
        except OSError:
            continue                  # gone before it was identified as a USJ
        try:
            bus, dev = int(_attr(d, 'busnum')), int(_attr(d, 'devnum'))
        except (OSError, ValueError):
            raise RuntimeError(f'USJ at {busport} disappeared during the walk') from None
        # udev's record first: `serial` is served under the device lock (usb_observe.read_sysfs)
        sn = usb_observe.udev_serial(bus, dev)
        if sn is None:
            sn = usb_observe.read_sysfs(os.path.join(d, 'serial'))
        if sn is None:
            unknown.append(busport)
        elif sn == serial:            # openocd compares exactly (libusb_helper.c string_descriptor_equal)
            found.append((busport, bus, dev))
    if unknown:
        raise RuntimeError(f'USJ serial {serial}: cannot rule out {", ".join(unknown)} (serial unreadable)')
    if not found:
        raise RuntimeError(f'USJ serial {serial}: no 303a:1001 with that serial')
    if len(found) > 1:
        raise RuntimeError(f'USJ serial {serial}: ambiguous, at {", ".join(f[0] for f in found)}')
    return found


def usj_location(serial: str) -> str:
    """The bus-port of the one USJ with `serial`, for `adapter usb location`; RuntimeError when
    it is missing, ambiguous, unidentifiable or changes between two walks. No serial-only
    fallback."""
    first = _usj_walk(serial)
    if _usj_walk(serial) != first:
        raise RuntimeError(f'USJ serial {serial}: moved or re-enumerated during resolution ({first[0][0]})')
    busport = first[0][0]
    if not usb_observe.BUSPORT_RE.fullmatch(busport) or len(busport) > USB_LOCATION_MAX:
        raise RuntimeError(f'USJ serial {serial}: bus-port {busport!r} is not a location openocd can match')
    return busport


def server_argv(tool: dict, cfgs: list, location: str, serial: str, *, tcl_port: int, token: str,
                log: str) -> list:
    """The one place the configuration order is decided: ports and the session token, then the
    route with `adapter usb location` and `adapter serial` after the interface cfg (inside a
    board cfg, or before the target cfg) and `noinit`, so init runs only when asked over Tcl.
    Each cfg is named by its path in the verified scripts, never relative to a cwd."""
    selectors = ['-c', f'adapter usb location {location}', '-c', f'adapter serial {serial}']
    files = [os.path.join(tool['scripts'], cfg) for cfg in cfgs]
    route = ['-f', files[0], *selectors] + [x for f in files[1:] for x in ('-f', f)]
    return [tool['exe'], '-s', tool['scripts'], '-c', 'gdb port disabled', '-c', 'telnet port disabled',
            '-c', f'tcl port {tcl_port}', '-c', f'set ESPS_SESSION {token}', *route, '-c', 'noinit', '-l', log]


class _Stop(Exception):
    """The work ends here. `end`: nothing is outstanding, so the reviewed end may follow."""

    def __init__(self, reason, *, end=False, timed_out=False):
        super().__init__(reason)
        self.end, self.timed_out = end, timed_out


def _wrap(cmd, seq):
    # the Tcl server answers with a command's result but not its status (tcl_server.c:233-249);
    # hil_done records, on the server, that command number `seq` has run
    return (f'set hil_r [catch {{{cmd}}} hil_m]; set hil_done {seq}; '
            f'if {{$hil_r}} {{set hil_m "HIL-ERR $hil_m"}} else {{set hil_m "HIL-OK $hil_m"}}')


DONE_QUERY = 'if {[info exists hil_done]} {set hil_done} else {list}'

# target_handle_event's LOG_TARGET_ERROR "[%s] Execution of event %s failed:" (pinned build)
_EVENT_FAILED_RE = re.compile(r'Error: \[(\S+)\] Execution of event (\S+) failed:')


def _event_failures(lines) -> list:
    return [f'event {m[2]} failed on {m[1]}' for ln in lines if (m := _EVENT_FAILED_RE.fullmatch(ln))]


def _log_since(log, mark) -> tuple:
    """(lines, events) of the log from byte `mark`; never raises. `events` names each failed
    event hook, or a log that could not be read, which leaves one not ruled out: either fails
    the command."""
    try:
        with open(log, 'rb') as f:
            f.seek(mark)
            lines = f.read().decode(errors='replace').splitlines()
    except OSError as e:
        return [], [f'its log could not be read, so a failed event hook is not ruled out ({e})']
    return lines, _event_failures(lines)


def _lock(receipt):
    """An fd holding the exclusive flock on the receipt's companion lock file, or None while
    another owner holds it. The fd is not inheritable (PEP 446), so no child keeps the lock;
    callers hold interrupts from this call into the `try` that closes it."""
    fd = os.open(receipt + '.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


def _shutdown(tcl):
    try:
        tcl('shutdown')
    except (OSError, esp_session.ToolError):
        pass        # it answers and closes the connection as it shuts down (tcl_server.c:235-240)
    tcl.close()


class Session:
    """One managed openocd-esp32 server (module docstring). `ask` and `check` send one command
    and record it in `steps` with its outcome, answer and the log lines it produced."""

    def __init__(self, argv, out, tcl_port, token):
        self.argv, self.out = argv, out
        self.log, self.receipt = os.path.join(out, 'openocd.log'), os.path.join(out, 'receipt.json')
        self.lock_fd = None
        self.tcl = esp_session.Tcl(tcl_port, token)
        self.proc = self.rec = self.exit = self.failure = self.receipt_error = None
        self.receipt_written = self.closed = self.timed_out = False
        self.steps, self.deadline, self.seq = [], None, 0

    def start(self, deadline):
        self.deadline = deadline
        open(self.log, 'w').close()
        # find_file tries a name relative to the cwd before any -s dir (configuration.c:69-85), and
        # the command line's -s dirs come before the defaults (options.c:350-353): from an empty
        # cwd a cfg's `source [find ...]` reaches only the verified scripts
        cwd = os.path.join(self.out, 'cwd')
        os.mkdir(cwd)
        with open(os.path.join(self.out, 'openocd.out'), 'w') as out:
            hold = hil_process.InterruptHold()
            try:
                self.lock_fd = _lock(self.receipt)
                if self.lock_fd is None:
                    raise RuntimeError(f'{self.receipt}.lock is held by another owner')
                self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=out, cwd=cwd,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                self.rec = {'pid': self.proc.pid, 'startTime': esp_session.start_time(self.proc.pid),
                            'argv': self.argv, 'ports': [None, None, self.tcl.port], 'token': self.tcl.token,
                            'log': self.log, 'closed': False, 'startedAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            'tool': 'hil_openocd_esp', 'init': None, 'submitted': 0}
                esp_session.write_receipt(self.receipt, self.rec)
                self.receipt_written = True
            except BaseException:
                if self.proc is None:
                    self.release()
                raise
            finally:
                hold.release()     # a signal deferred across the spawn is acted on here, once registered

    def release(self):
        """Give up the receipt: from here `end` may own it."""
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def wait_ready(self):
        # server.c:313 prints this at server_init, before the server loop answers
        try:
            esp_session.wait_for(self.proc, self.log, rf'Listening on port {self.tcl.port} for tcl connections',
                                 self.deadline)
        except esp_session.ToolError as e:
            raise _Stop(str(e), timed_out=self.proc.poll() is None) from None

    @property
    def init_sent(self):
        return self.rec is not None and self.rec['init'] is not None

    def ask(self, cmd) -> tuple:
        """(ok, answer) of one command, failed on an error answer, an event failure in its log lines
        or a log it could not read (`events`); _Stop when it is left outstanding or was never sent."""
        if self.deadline <= time.monotonic():
            raise _Stop(f'timed out before {cmd!r}', end=True, timed_out=True)
        if cmd == 'init':
            self.rec['init'] = 'sent'
        self.seq += 1
        self.rec['submitted'] = self.seq
        esp_session.write_receipt(self.receipt, self.rec)
        mark, step = esp_session.size_of(self.log) or 0, {'cmd': cmd, 'outcome': 'outstanding'}
        self.steps.append(step)
        self.tcl.deadline = self.deadline
        try:
            answer = self.tcl(_wrap(cmd, self.seq))
        except TimeoutError:
            raise _Stop(f'{cmd!r} still outstanding after the timeout', timed_out=True) from None
        except (OSError, esp_session.ToolError) as e:
            raise _Stop(f'{cmd!r}: {e}') from None
        finally:
            step['log'], events = _log_since(self.log, mark)    # never raises over an interrupt
        answered_ok = answer.startswith('HIL-OK')
        step['answer'] = answer.removeprefix('HIL-OK' if answered_ok else 'HIL-ERR').removeprefix(' ')
        ok = answered_ok and not events
        step['outcome'] = 'ok' if ok else 'error'
        if events:
            step['events'] = events
            reason = '; '.join(events)
        elif ok or step['answer']:
            reason = step['answer']
        else:
            # a failed init answers empty; its reason is only in the log
            reason = '; '.join(ln for ln in step['log'] if ln.startswith('Error'))
        if cmd == 'init':
            self.rec['init'] = 'ok' if ok else 'failed'
            esp_session.write_receipt(self.receipt, self.rec)
        return ok, reason

    def check(self, cmd) -> str:
        """The answer of a command that must succeed; its failure ends the work, then the reviewed end."""
        ok, answer = self.ask(cmd)
        if not ok:
            raise _Stop(f'{cmd!r} failed: {answer}', end=True)
        return answer

    def end(self):
        self.deadline = time.monotonic() + CLEANUP_TIMEOUT
        ok, answer = self.ask('reset run')
        if not ok:
            raise _Stop(f"'reset run' failed: {answer}")
        self.steps.append({'cmd': 'shutdown', 'outcome': 'sent'})
        self.tcl.deadline = self.deadline
        _shutdown(self.tcl)
        if not esp_session.wait_exit(self.proc.pid, self.proc, max(self.deadline - time.monotonic(), 0)):
            raise _Stop('openocd still runs after shutdown')
        if self.proc.returncode:
            raise _Stop(f'openocd exited {self.proc.returncode} after shutdown')

    def fail(self, stop):
        self.failure = self.failure or str(stop)
        self.timed_out |= stop.timed_out

    def cleanup(self) -> str:
        if self.proc is not None and self.proc.poll() is None:
            return 'pending'
        orderly = bool(self.steps) and self.steps[-1]['cmd'] == 'shutdown'
        cleanup = 'done' if orderly or not self.init_sent else 'unknown'
        if not self.closed and self.proc is not None:
            self.exit, self.closed = self.proc.returncode, True
            if self.receipt_written:
                try:
                    esp_session.write_receipt(self.receipt, {**self.rec, 'closed': True, 'cleanup': cleanup,
                                                             'exit': self.exit})
                except OSError as e:
                    self.receipt_error = e     # raised by _finish once the operation is built
        return cleanup

    def operation(self, error=None, **extra):
        """hil_flash's Operation of one attempt, the session: argv, the commands sent as its
        script, the log as stdout, rc 0 (all done), 1 (failed), 124 (timed out) or None
        (raised), cleanup and receipt; plus `steps` and `session`."""
        self.tcl.close()    # an outstanding command still completes; the receipt reconnects
        cleanup = self.cleanup()
        receipt = None if cleanup != 'pending' else self.receipt if self.receipt_written else self.rec
        rc = None if error is not None else 124 if self.timed_out else 1 if self.failure else 0
        try:
            with open(self.log, errors='replace') as f:
                text = f.read()
        except OSError:
            text = ''
        script = ''.join(f"{s['cmd']}\n" for s in self.steps)
        result = subprocess.CompletedProcess(self.argv, rc, text, None)
        op = hil_flash.Operation([hil_flash.Attempt(self.argv, script, result, error, cleanup, receipt)])
        op.steps = tuple(self.steps)
        op.session = {**extra, 'dir': self.out, 'log': self.log, 'receipt': self.receipt, 'exit': self.exit,
                      'failure': self.failure,
                      'receiptError': None if self.receipt_error is None else str(self.receipt_error)}
        return op


def _finish(s, error=None, **info):
    """s.operation(error) with SIGINT/SIGTERM held until it is built, so an interrupt arriving
    meanwhile still carries the operation and its receipt. A closing receipt that could not be
    written raises its OSError carrying the operation, unless `error` is already on its way."""
    hold = hil_process.InterruptHold()
    op = None
    try:
        op = s.operation(error, **info)
    finally:
        s.release()
        try:
            hold.release()
        except BaseException as late:
            if op is not None:
                late.operation = op
            raise
    if error is None and s.receipt_error is not None:
        s.receipt_error.operation = op
        raise s.receipt_error
    return op


def _workdir(workdir) -> str:
    path = os.path.abspath(workdir)
    if not _PATH_RE.fullmatch(path) or not os.path.isdir(path):
        raise ValueError(f'workdir {workdir!r}: want an existing directory whose path has letters, digits and '
                         f'_ . / + - only')
    return path


def _run(flasher: dict, work, *, workdir, timeout, openocd=None, record=None):
    """Admit, resolve and start a session in a new directory under `workdir`, init it, run
    `work(session)` within `timeout` seconds, then end it as the module docstring says; `record`
    joins the operation's `session`. Refusals raise before anything starts; an exception after
    the start carries `.operation`."""
    chip, cfgs = admit(flasher)
    workdir = _workdir(workdir)
    tool = find_openocd(openocd)
    check_cfgs(chip, cfgs, tool['scripts'])
    location = usj_location(flasher['uid'])
    out = tempfile.mkdtemp(prefix='hil_openocd_esp_', dir=workdir)
    port, token = esp_session.free_ports(1)[0], secrets.token_hex(8)
    s = Session(server_argv(tool, cfgs, location, flasher['uid'], tcl_port=port, token=token,
                            log=os.path.join(out, 'openocd.log')), out, port, token)
    info = {'openocd': tool['banner'], 'scripts': tool['scripts'], 'location': location, 'serial': flasher['uid'],
            **(record or {})}
    try:
        s.start(time.monotonic() + timeout)
        s.wait_ready()
        ok, answer = s.ask('init')
        if not ok:
            # init runs once even when it fails (openocd.c:115-119): nothing may follow it
            raise _Stop(f'init failed: {answer}')
        try:
            work(s)
        except _Stop as stop:
            if not stop.end:
                raise
            s.fail(stop)
        s.end()
    except _Stop as stop:
        s.fail(stop)
    except BaseException as e:
        if s.proc is None:
            s.release()
            raise
        for step in s.steps:
            if step['outcome'] == 'outstanding':
                step['outcome'] = 'interrupted'
        e.operation = _finish(s, e, **info)
        raise
    return _finish(s, **info)


# -------------------------------------------------------------
# Programming
# -------------------------------------------------------------
SECTOR = 4096           # FLASH_SECTOR_SIZE of every esp32*.c flash driver
_SEGMENT_KEYS = {'offset', 'file', 'size', 'encrypted'}


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


_HEX = frozenset(b'0123456789abcdefABCDEF')


def _loaded_as(head: bytes) -> str | None:
    """What openocd would load a file starting with `head` as, if not a raw binary: the write
    hands the file to `flash write_image erase` without a type, as program_esp does
    (esp_common.cfg:642-663), which autodetects ELF, Intel HEX and S-record (image.c:42-89,
    image_open :956-960) and places their records at their own addresses rather than at the
    segment's offset."""
    if len(head) < 9:
        return None                      # image.c:57-62: under 9 bytes is always binary
    if head[:4] == b'\x7fELF':
        return 'ELF'
    if head[0] == ord(':') and all(c in _HEX for c in head[1:7]) and head[7] == ord('0') \
            and ord('0') <= head[8] < ord('6'):
        return 'Intel HEX'
    if head[0] == ord('S') and all(c in _HEX for c in head[1:4]) and ord('0') <= head[1] < ord('9'):
        return 'S-record'
    return None


def admit_segments(segments) -> list:
    """[(record, bytes)] for the caller's ordered segments ({offset, file, size}), or ValueError.

    An offset is sector-aligned: `flash write_image erase` erases whole sectors around the
    range but pads only its end with data (core.c:516-552, 883-900), so bytes before an
    unaligned offset would be erased. Erase extents may not overlap. A length need not be a
    multiple of 4: verify_bank_hash skips the last length % 4 bytes (esp_flash.c:1726-1730),
    so the read-back covers them. `encrypted` may only be false: the writes run with `esp
    encrypt_binary no` and write plaintext; any other key is an option this backend does not apply."""
    if not isinstance(segments, (list, tuple)) or not segments:
        raise ValueError('no segments')
    admitted, extents = [], []
    for seg in segments:
        if not isinstance(seg, dict) or not {'offset', 'file', 'size'} <= set(seg) <= _SEGMENT_KEYS:
            raise ValueError(f'segment {seg!r}: want {{offset, file, size}} and nothing but `encrypted` false')
        if seg.get('encrypted', False) not in (False, 'false'):
            raise ValueError(f'segment {seg!r}: encrypted writes are refused (the writes are plaintext here)')
        offset, size = seg['offset'], seg['size']
        if not _int(offset) or offset < 0 or offset % SECTOR:
            raise ValueError(f'segment {seg!r}: the offset must be a multiple of 0x{SECTOR:x}')
        if not _int(size) or size <= 0:
            raise ValueError(f'segment {seg!r}: the size must be a positive number of bytes')
        path = os.path.abspath(str(seg['file']))
        if not _PATH_RE.fullmatch(path):
            raise ValueError(f'segment file {path!r}: letters, digits and _ . / + - only (it reaches Tcl)')
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as e:
            raise ValueError(f'segment file {path}: {e}') from None
        if len(data) != size:
            raise ValueError(f'segment file {path}: {len(data)} bytes, the segment says {size}')
        kind = _loaded_as(data[:9])
        if kind:
            raise ValueError(f'segment file {path}: openocd would load it as {kind}, at its own addresses; '
                             f'give the raw binary')
        end = -(-(offset + size) // SECTOR) * SECTOR
        extents.append((offset, end, path))
        admitted.append(({'offset': offset, 'file': path, 'size': size, 'erase': [offset, end],
                          'sha256': hashlib.sha256(data).hexdigest()}, data))
    extents.sort()
    for (_, a_end, a), (b_start, _, b) in zip(extents, extents[1:]):
        if b_start < a_end:
            raise ValueError(f'segments {a} and {b} overlap in their erased sectors')
    return admitted


_SECURITY_KEYS = {'flashEncryption', 'secureBoot', 'source'}


def admit_security(security) -> dict:
    """The caller's evidence that the chip's flash encryption and secure boot are off, read from
    the chip before programming: {flashEncryption: 'off', secureBoot: 'off', source: <where it was
    read, such as a transcript>}. Anything else is refused: this backend writes plaintext without
    `encrypt` and never decides the chip's security state itself."""
    if not isinstance(security, dict) or set(security) != _SECURITY_KEYS \
            or security['flashEncryption'] != 'off' or security['secureBoot'] != 'off' \
            or not isinstance(security['source'], str) or not security['source'].strip():
        raise ValueError(f'security evidence {security!r}: want {{flashEncryption: "off", secureBoot: "off", '
                         f'source: <where it was read>}}; programming is refused otherwise')
    return dict(security)


_BANK_RE = re.compile(r'name\s+\{([^}]*)\}\s+driver\s+(\S+)\s+base\s+(0x[0-9a-fA-F]+)\s+size\s+(0x[0-9a-fA-F]+)')


def _bank0(listing: str) -> dict:
    # `flash list` (tcl.c:1454-1475); bank 0 is the whole flash, which verify_bank_hash 0 and
    # read_bank 0 use (esp_common.cfg configure_esp_flash_bank)
    m = _BANK_RE.search(listing)
    if not m or not m[1].endswith('.flash') or int(m[3], 16) != 0 or not int(m[4], 16) \
            or int(m[4], 16) % SECTOR:
        raise _Stop(f'flash bank 0 is not a probed whole-flash bank: {listing[:200]!r}', end=True)
    return {'name': m[1], 'driver': m[2], 'size': int(m[4], 16)}


RESET_INIT_ATTEMPTS = 3
_TARGET_RE = re.compile(r'[A-Za-z0-9_.]+')     # a target name reaches Tcl unquoted


def _reset_init(s):
    """A `reset init` with no failed event hook, within RESET_INIT_ATTEMPTS; nothing is written yet."""
    for _ in range(RESET_INIT_ATTEMPTS):
        ok, answer = s.ask('reset init')
        if ok:
            return
    raise _Stop(f"'reset init' failed {RESET_INIT_ATTEMPTS} times, last: {answer}", end=True)


def _write_setup(s) -> dict:
    """program_esp's setup after its reset (esp_common.cfg:621-652) with this backend's options:
    no work-area backup, no compression, plaintext, flash clock boost on. Returns each target's
    saved -work-area-backup."""
    names = s.check('target names').split()
    if not names or not all(_TARGET_RE.fullmatch(n) for n in names):
        raise _Stop(f'target names {names!r}: want names of letters, digits, _ and .', end=True)
    saved = {}
    for tgt in names:
        saved[tgt] = s.check(f'{tgt} cget -work-area-backup')
        if saved[tgt] not in ('0', '1'):
            raise _Stop(f'{tgt} -work-area-backup is {saved[tgt]!r}, not 0 or 1', end=True)
        s.check(f'{tgt} configure -work-area-backup 0')
    for cmd in ('esp compression off', 'esp encrypt_binary no', 'esp flash_stub_clock_boost on'):
        s.check(cmd)
    return saved


def _program(s, rec):
    """program_esp's checks, write and verify of one segment (esp_common.cfg:577-579, 663-669):
    `check_image_revision`, `flash write_image erase`, `esp verify_bank_hash`, on the session's
    copy and with no reset. `program` is attached once a command has returned and holds the last
    answer and the log of every command that returned; `ok` and `verified` turn true only once
    the verify passed."""
    copy, offset = rec['copy'], f"0x{rec['offset']:x}"
    program = {'ok': False, 'answer': '', 'log': [], 'verified': False}
    for name, cmd in (('check_image_revision', f'check_image_revision {{{copy}}} 0'),
                      ('flash write_image erase', f'flash write_image erase {{{copy}}} {offset}'),
                      ('esp verify_bank_hash', f'esp verify_bank_hash 0 {{{copy}}} {offset}')):
        ok, program['answer'] = s.ask(cmd)
        program['log'] += s.steps[-1]['log']
        rec['program'] = program
        if not ok:
            raise _Stop(f"programming {rec['file']} at {offset} failed at {name!r}: {program['answer']}", end=True)
    program['ok'] = program['verified'] = True


def flash_openocd_esp(flasher: dict, segments, *, security, workdir, timeout, openocd=None):
    """Program `segments` in order, each verified, stopping at the first failure, then read every
    segment back with `flash read_bank` and compare it byte for byte with the bytes admitted. One
    managed session (module docstring) within `timeout` seconds, files in a new directory under
    `workdir`. The chip must not sit in its ROM loader (the `hil` skill's Flashers).

    `security` is the caller's evidence that flash encryption and secure boot are off
    (admit_security), required and recorded as `session['security']`.

    Nothing is written without a `reset init` whose event hooks all passed, within
    RESET_INIT_ATTEMPTS (_reset_init), nor a segment whose erased sectors leave bank 0; no reset
    runs between that reset and the last write (_write_setup, _program). Returns hil_flash's
    Operation, with `segments`: per segment its offset, file, size, erased range, sha256, `copy`
    (the session's copy of the admitted bytes, the file programmed), `program` once attempted
    (_program) and `readBack` (file, length, match, firstMismatch). Refusals raise ValueError
    or RuntimeError before anything starts."""
    _refuse_ci()
    security = admit_security(security)
    admitted = admit_segments(segments)
    records = [rec for rec, _ in admitted]

    def work(s):
        # each write step reopens its file: program a private copy, so an input replaced after
        # admission cannot write past the checked erase range
        try:
            for i, (rec, data) in enumerate(admitted):
                rec['copy'] = os.path.join(s.out, f'segment_{i}_0x{rec["offset"]:x}.bin')
                with open(rec['copy'], 'xb') as f:
                    f.write(data)
        except OSError as e:
            raise _Stop(f'copying the admitted segments: {e}', end=True) from None
        _reset_init(s)
        s.check('flash probe 0')
        bank = _bank0(s.check('flash list'))
        for rec in records:
            if rec['erase'][1] > bank['size']:
                raise _Stop(f"segment {rec['file']} at 0x{rec['offset']:x} leaves bank {bank['name']} "
                            f"(0x{bank['size']:x} bytes)", end=True)
        saved = _write_setup(s)
        for rec in records:
            _program(s, rec)
        # each target its own value: configure_esp_workarea_backups's `incr $index` never advances
        for tgt, value in saved.items():
            s.check(f'{tgt} configure -work-area-backup {value}')
        for i, (rec, data) in enumerate(admitted):
            # esp_algo_flash_read refuses an unaligned offset (esp_flash.c:986-989); the length is
            # rounded up to 4 and only the segment's own bytes compared
            length = -(-rec['size'] // 4) * 4
            path = os.path.join(s.out, f'readback_{i}_0x{rec["offset"]:x}.bin')
            rec['readBack'] = {'file': path, 'length': length, 'match': False, 'firstMismatch': None}
            s.check(f"flash read_bank 0 {{{path}}} 0x{rec['offset']:x} {length}")
            with open(path, 'rb') as f:
                got = f.read()
            diff = None
            if got[:rec['size']] != data:
                diff = next((j for j, (a, b) in enumerate(zip(got, data)) if a != b), min(len(got), rec['size']))
            elif len(got) != length:
                diff = rec['size']
            rec['readBack'].update(match=diff is None, firstMismatch=diff)
            if diff is not None:
                raise _Stop(f"read-back of {rec['file']} at 0x{rec['offset']:x} differs at +0x{diff:x}", end=True)

    try:
        op = _run(flasher, work, workdir=workdir, timeout=timeout, openocd=openocd, record={'security': security})
    except BaseException as e:
        if hasattr(e, 'operation'):
            e.operation.segments = tuple(records)
        raise
    op.segments = tuple(records)
    return op


def reset_openocd_esp(flasher: dict, *, workdir, timeout, openocd=None):
    """Reset and run the chip through a managed session: init within `timeout` seconds, then the
    reviewed end, `reset run` and a confirmed shutdown within CLEANUP_TIMEOUT. init examines the
    cores, which panics a running app built with the end-of-stack watchpoint; the reset run
    that follows reboots it. An init or a reset run that failed, a failed event hook included,
    leaves the session pending instead.
    Returns hil_flash's Operation, as flash_openocd_esp without `segments`."""
    _refuse_ci()
    return _run(flasher, lambda s: None, workdir=workdir, timeout=timeout, openocd=openocd)


# -------------------------------------------------------------
# Ending a pending session
# -------------------------------------------------------------
END_TIMEOUT = 60
_RECEIPT_KEYS = ('pid', 'startTime', 'argv', 'ports', 'token', 'log', 'closed', 'init', 'submitted')
NEEDS_RECOVERY = 'the board stays held and needs a separately reviewed recovery'


def _exited(pidfd, deadline) -> bool:
    """Whether the identified process exited by the deadline: only its pidfd turning readable
    (POLLIN at exit, pidfd_open(2)) counts; any other poll result is no evidence."""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return any(ev & select.POLLIN for _, ev in poller.poll(max(deadline - time.monotonic(), 0) * 1000))


def end_session(receipt: str, *, timeout: float = END_TIMEOUT) -> tuple:
    """(rc, result) of ending the pending session a receipt names within `timeout` seconds (the
    `hil` skill's Flashers has the caller's contract): 0 ended or already closed after the
    reviewed end, 1 refused or failed with the receipt kept. OSError or ValueError for an
    unreadable or malformed receipt; an interrupt propagates with the receipt kept.

    Invariants: the receipt's lock is held throughout and the receipt read only once it is
    taken, so the session has finalized and no other `end` runs. The process is pinned by a
    pidfd, the only accepted evidence of its exit. `hil_done` must equal `submitted` before
    anything changes the target: an answer alone is no proof, since the server reads a
    connection 256 bytes at a time (tcl_server.c:172-176) and services connections in turn
    (server.c:572-590), so an older command still unread on a dropped connection could run
    after a new connection's, and one still running holds the server, so the query times out."""
    deadline = time.monotonic() + timeout
    os.stat(receipt)
    while True:
        hold, lock = hil_process.InterruptHold(), None
        try:
            lock = _lock(receipt)
            if lock is not None:
                held, hold = hold, None
                held.release()             # an interrupt that arrived meanwhile raises inside this try
                return _end_owned(receipt, deadline)
        finally:
            if hold is not None:
                hold.release()
            if lock is not None:
                os.close(lock)
        if time.monotonic() >= deadline:
            return 1, {'mode': 'end', 'receipt': receipt, 'outcome': 'refused',
                       'error': 'the session still holds the receipt lock: it is still the owner; nothing sent'}
        time.sleep(0.05)


def _end_owned(receipt, deadline) -> tuple:
    with open(receipt) as f:
        rec = json.load(f)
    if not isinstance(rec, dict) or rec.get('tool') != 'hil_openocd_esp' or any(k not in rec for k in _RECEIPT_KEYS):
        raise ValueError('not a hil_openocd_esp session receipt')
    result = {'mode': 'end', 'receipt': receipt, 'pid': rec['pid']}

    def stop(outcome, why):
        result.update(outcome=outcome, error=why)
        return 1, result

    if rec['closed']:
        if rec.get('cleanup') == 'done':
            result['outcome'] = 'alreadyEnded'
            return 0, result
        return stop('refused', f"closed with cleanup {rec.get('cleanup')!r}, without the reviewed end: "
                               f'{NEEDS_RECOVERY}')
    if not esp_session.same_process(rec):
        return stop('refused', f"process {rec['pid']} is not the openocd this receipt names (gone or another "
                               f'process): nothing sent; {NEEDS_RECOVERY}')
    pidfd = tcl = None
    try:
        pidfd_open = getattr(os, 'pidfd_open', None)
        if pidfd_open is None:
            return stop('refused', f'no pidfd here, so the exit could not be confirmed: nothing sent; {NEEDS_RECOVERY}')
        try:
            pidfd = pidfd_open(rec['pid'])
        except ProcessLookupError:
            return stop('refused', f"process {rec['pid']} exited: nothing sent; {NEEDS_RECOVERY}")
        except OSError as e:
            return stop('refused', f'pidfd_open: {e}, so the exit could not be confirmed: nothing sent; '
                                   f'{NEEDS_RECOVERY}')
        if not esp_session.same_process(rec):        # the pidfd now names the identified process
            return stop('refused', f"process {rec['pid']} changed while it was identified: nothing sent")
        if rec['init'] != 'ok':
            return stop('refused', f"init {rec['init'] or 'never sent'}: the ordinary end (reset run, shutdown) is "
                                   f'refused after a failed or unproven init; {NEEDS_RECOVERY}')
        tcl = esp_session.Tcl(rec['ports'][2], rec['token'], deadline=deadline)
        done = tcl(DONE_QUERY)
        result['done'] = done
        if done != str(rec['submitted']):
            return stop('refused', f"command #{rec['submitted']} is not known to have run (the server has run "
                                   f"#{done or 'none'}): nothing sent; rerun later")
        rec['submitted'] += 1
        esp_session.write_receipt(receipt, rec)
        mark = esp_session.size_of(rec['log']) or 0
        answer = tcl(_wrap('reset run', rec['submitted']))
        result['resetRun'] = answer
        _, events = _log_since(rec['log'], mark)
        if events:
            result['events'] = events
        if not answer.startswith('HIL-OK') or events:
            return stop('failed', f"'reset run' failed: {'; '.join(events) or answer}; {NEEDS_RECOVERY}")
        _shutdown(tcl)
        if not _exited(pidfd, deadline):
            return stop('failed', 'openocd exit not confirmed after shutdown (still running?); rerun end')
        esp_session.write_receipt(receipt, {**rec, 'closed': True, 'cleanup': 'done',
                                'end': {'resetRun': answer, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}})
        result['outcome'] = 'ended'
        return 0, result
    except (OSError, esp_session.ToolError) as e:     # TimeoutError included
        return stop('failed', f'{e}; the receipt is kept')
    finally:
        if tcl is not None:
            tcl.close()
        if pidfd is not None:
            os.close(pidfd)


def main(argv=None):
    p = argparse.ArgumentParser(description='End a pending openocd_esp session from its receipt '
                                            '(end_session); one JSON object on stdout.')
    sub = p.add_subparsers(dest='mode', required=True)
    e = sub.add_parser('end', help='reset run and shut down the session a receipt names')
    e.add_argument('--receipt', required=True, metavar='FILE')
    e.add_argument('--timeout', type=float, default=END_TIMEOUT, metavar='S',
                   help=f'one bound on the whole end, in seconds (default {END_TIMEOUT})')
    a = p.parse_args(argv)
    if not (0 < a.timeout < float('inf')):
        p.error('--timeout must be a finite positive number of seconds')
    try:
        rc, result = end_session(a.receipt, timeout=a.timeout)
    except (OSError, ValueError) as err:
        p.error(f'--receipt {a.receipt}: {err}')
    except KeyboardInterrupt:
        rc, result = 1, {'mode': 'end', 'receipt': a.receipt, 'outcome': 'failed',
                         'error': 'interrupted; the receipt is kept'}
    print(json.dumps(result))
    return rc


if __name__ == '__main__':
    sys.exit(main())
