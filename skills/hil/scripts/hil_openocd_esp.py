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
session's token (target-debug's esp_session.py Tcl). Its pid, start time, argv, port, token
and log are written to <dir>/receipt.json, in esp_session.py's receipt format, before an
interrupt can be acted on. It is never signalled or killed. The reviewed end is `reset run`,
then `shutdown` and its exit confirmed within CLEANUP_TIMEOUT; it follows success and a
command that failed and returned. An init that failed, a command still outstanding (timed
out, interrupted, its connection lost or answered by a foreign server) or a reset run that
failed gets no reset and no shutdown: the operation's cleanup is `pending` and its receipt
names the server left running, for the caller's cleanup holder. A server that exited on its
own leaves cleanup `unknown` (`done` when it exited before init was asked, so no adapter was
opened). There is no bare init/shutdown: examining a running app built with the end-of-stack
watchpoint panics it, and only the reset run brings it back.
"""

from __future__ import annotations

import glob
import importlib.util
import os
import re
import shlex
import secrets
import shutil
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usb_observe = _load('usb_observe', os.path.join(HERE, 'usb_observe.py'))
hil_flash = _load('hil_flash', os.path.join(HERE, 'hil_flash.py'))
# Tcl token check, receipts and process identity; a consumer staging this skill stages it too
ESP_SESSION = os.path.join(HERE, '..', '..', 'target-debug', 'scripts', 'esp_session.py')
esp_session = _load('esp_session', ESP_SESSION)

# The build whose program_esp, flash read_bank, esp_usb_jtag and shutdown paths were read; another
# build is refused until its source is checked again.
VERIFIED_OPENOCD = 'v0.12.0-esp32-20260703'
OPENOCD_ENV = 'HIL_OPENOCD_ESP'
USJ_VID_PID = ('303a', '1001')       # interface/esp_usb_jtag.cfg's `adapter usb vid_pid`
# adapter.c:324-331 warns about a location of 16 characters and truncates a longer one
USB_LOCATION_MAX = 15

CLEANUP_TIMEOUT = 30                 # seconds for the reviewed end: reset run, shutdown, exit

_UID_RE = re.compile(r'[A-Za-z0-9_.:+-]+')
_PATH_RE = re.compile(r'/[A-Za-z0-9_./+-]*')    # a path that reaches a Tcl command unquoted
_CHIP = r'esp32[a-z0-9]*'
_BUSPORT_RE = re.compile(r'[0-9]+-[0-9]+(\.[0-9]+)*')


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
    if 'proc program_esp ' not in common:
        raise RuntimeError(f'{scripts}/target/esp_common.cfg defines no program_esp')
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
    if not _BUSPORT_RE.fullmatch(busport) or len(busport) > USB_LOCATION_MAX:
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


def _wrap(cmd):
    # the Tcl server answers with a command's result but not its status (tcl_server.c:233-249)
    return f'if {{[catch {{{cmd}}} hil_m]}} {{set hil_m "HIL-ERR $hil_m"}} else {{set hil_m "HIL-OK $hil_m"}}'


class _Tcl(esp_session.Tcl):
    """esp_session's token-checked Tcl with one absolute `deadline` across the connect, the token
    check and the command's answer; its own _ask bounds every send and recv by what is left."""
    deadline = None

    def _left(self, cmd):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f'Tcl port {self.port}: no answer to {cmd!r} by the deadline')
        return left

    def __call__(self, cmd):
        self.timeout = self._left(cmd)     # bounds the connect
        return super().__call__(cmd)

    def _ask(self, cmd):
        try:
            self.sock.settimeout(self._left(cmd))
            self.sock.sendall(cmd.encode() + esp_session.TCL_END)
            buf = b''
            while not buf.endswith(esp_session.TCL_END):
                self.sock.settimeout(self._left(cmd))
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise esp_session.ToolError(f'Tcl port {self.port} closed before answering {cmd!r}')
                buf += chunk
        except BaseException:
            self.close()
            raise
        return buf[:-1].decode(errors='replace')


class Session:
    """One managed openocd-esp32 server (module docstring). `ask` and `check` send one command
    and record it in `steps` with its outcome, answer and the log lines it produced."""

    def __init__(self, argv, out, tcl_port, token):
        self.argv, self.out = argv, out
        self.log, self.receipt = os.path.join(out, 'openocd.log'), os.path.join(out, 'receipt.json')
        self.tcl = _Tcl(tcl_port, token)
        self.proc = self.rec = self.exit = self.failure = None
        self.receipt_written = self.closed = self.init_sent = self.timed_out = False
        self.steps, self.deadline = [], None

    def start(self, deadline):
        self.deadline = deadline
        open(self.log, 'w').close()
        # find_file tries a name relative to the cwd before any -s dir (configuration.c:69-85), and
        # the command line's -s dirs come before the defaults (options.c:350-353): from an empty
        # cwd a cfg's `source [find ...]` reaches only the verified scripts
        cwd = os.path.join(self.out, 'cwd')
        os.mkdir(cwd)
        with open(os.path.join(self.out, 'openocd.out'), 'w') as out:
            hold = hil_flash.hil_process._InterruptHold()
            try:
                self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=out, cwd=cwd,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                self.rec = {'pid': self.proc.pid, 'startTime': esp_session.start_time(self.proc.pid),
                            'argv': self.argv, 'ports': [None, None, self.tcl.port], 'token': self.tcl.token,
                            'log': self.log, 'closed': False, 'startedAt': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
                esp_session.write_receipt(self.receipt, self.rec)
                self.receipt_written = True
            finally:
                hold.release()     # a signal deferred across the spawn is acted on here, once registered

    def wait_ready(self):
        # server.c:313 prints this at server_init, before the server loop answers
        try:
            esp_session.wait_for(self.proc, self.log, rf'Listening on port {self.tcl.port} for tcl connections',
                                 self.deadline)
        except esp_session.ToolError as e:
            raise _Stop(str(e), timed_out=self.proc.poll() is None) from None

    def _log_size(self):
        try:
            return os.path.getsize(self.log)
        except OSError:
            return 0

    def _log_since(self, mark):
        try:
            with open(self.log, 'rb') as f:
                f.seek(mark)
                return f.read().decode(errors='replace').splitlines()
        except OSError:
            return []

    def ask(self, cmd) -> tuple:
        """(ok, answer) of one command; _Stop when it is left outstanding or was never sent."""
        if self.deadline <= time.monotonic():
            raise _Stop(f'timed out before {cmd!r}', end=True, timed_out=True)
        if cmd == 'init':
            self.init_sent = True
        mark, step = self._log_size(), {'cmd': cmd, 'outcome': 'outstanding'}
        self.steps.append(step)
        self.tcl.deadline = self.deadline
        try:
            answer = self.tcl(_wrap(cmd))
        except TimeoutError:
            step['log'] = self._log_since(mark)
            raise _Stop(f'{cmd!r} still outstanding after the timeout', timed_out=True) from None
        except (OSError, esp_session.ToolError) as e:
            step['log'] = self._log_since(mark)
            raise _Stop(f'{cmd!r}: {e}') from None
        step['log'] = self._log_since(mark)
        ok = answer.startswith('HIL-OK')
        step['outcome'] = 'ok' if ok else 'error'
        step['answer'] = answer.removeprefix('HIL-OK' if ok else 'HIL-ERR').removeprefix(' ')
        # a failed init answers empty; its reason is only in the log
        errors = '; '.join(ln for ln in step['log'] if ln.startswith('Error'))
        return ok, step['answer'] if ok or step['answer'] else errors

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
        try:
            self.tcl.deadline = self.deadline
            self.tcl('shutdown')
        except (OSError, esp_session.ToolError):
            pass        # it answers and closes the connection as it shuts down (tcl_server.c:235-240)
        self.tcl.close()
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
        if not self.closed and self.proc is not None:
            self.exit, self.closed = self.proc.returncode, True
            if self.receipt_written:
                esp_session.write_receipt(self.receipt, {**self.rec, 'closed': True})
        orderly = bool(self.steps) and self.steps[-1]['cmd'] == 'shutdown'
        return 'done' if orderly or not self.init_sent else 'unknown'

    def operation(self, error=None, **extra):
        """hil_flash's Operation of one attempt, the session: argv, the commands sent as its
        script, the log as stdout, rc 0 (all done), 1 (failed), 124 (timed out) or None
        (raised), cleanup and receipt; plus `steps` and `session`."""
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
                      'failure': self.failure}
        return op


def _finish(s, error=None, **info):
    """s.operation(error) with SIGINT/SIGTERM held until it is built, so an interrupt arriving
    meanwhile still carries the operation and its receipt."""
    hold = hil_flash.hil_process._InterruptHold()
    op = None
    try:
        op = s.operation(error, **info)
    finally:
        try:
            hold.release()
        except BaseException as late:
            if op is not None:
                late.operation = op
            raise
    return op


def _workdir(workdir) -> str:
    path = os.path.abspath(workdir)
    if not _PATH_RE.fullmatch(path) or not os.path.isdir(path):
        raise ValueError(f'workdir {workdir!r}: want an existing directory whose path has letters, digits and '
                         f'_ . / + - only')
    return path


def _run(flasher: dict, work, *, workdir, timeout, openocd=None):
    """Admit, resolve and start a session in a new directory under `workdir`, init it, run
    `work(session)` within `timeout` seconds, then end it as the module docstring says.
    Refusals raise before anything starts; an exception after the start carries `.operation`."""
    chip, cfgs = admit(flasher)
    workdir = _workdir(workdir)
    tool = find_openocd(openocd)
    check_cfgs(chip, cfgs, tool['scripts'])
    location = usj_location(flasher['uid'])
    out = tempfile.mkdtemp(prefix='hil_openocd_esp_', dir=workdir)
    port, token = esp_session.free_ports(1)[0], secrets.token_hex(8)
    s = Session(server_argv(tool, cfgs, location, flasher['uid'], tcl_port=port, token=token,
                            log=os.path.join(out, 'openocd.log')), out, port, token)
    info = {'openocd': tool['banner'], 'scripts': tool['scripts'], 'location': location, 'serial': flasher['uid']}
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
            raise
        for step in s.steps:
            if step['outcome'] == 'outstanding':
                step['outcome'] = 'interrupted'
        e.operation = _finish(s, e, **info)
        raise
    return _finish(s, **info)
