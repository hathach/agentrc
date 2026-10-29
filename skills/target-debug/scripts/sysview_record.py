#!/usr/bin/env python3
"""Record SEGGER SystemView on Linux over a J-Link, or decode a raw capture natively on any platform,
and export the analysis for sysview_report.py. The contract (arguments, files in --out, exit codes) is the target-debug
skill's sysview.md, "Script interface".

  sysview_record.py --out DIR (--hil-config FILE --board NAME | --probe SN --device NAME) --interface swd|jtag
                    --speed KHZ (--elf FILE | --addr ADDR) --duration-ms N [--workload CMD]
                    [--no-events] [--export-terminal]
  sysview_record.py --out DIR --from-raw FILE [--no-events] [--gui [--export-terminal]]

SystemView runs under an Xvfb of its own (-displayfd picks a free display) with a temporary HOME, so its ini and
recent-file state never touch the user's; the SEGGER registry file (license key) and JLinkDLL.ini are copied in. It
listens on a port this script picks (-single -port, UM08027 3.14), and every command first checks that the listener is
that SystemView: a refused port is never sent anything. Only dialogs named in DIALOGS are answered; any other one fails
the run with a screenshot.

SystemView's J-Link DLL opens every J-Link it can reach at startup, whichever one -usb names: on a rig that touches
other boards' probes while their own sessions use them (a licence notice per on-board probe, a "Communication timed
out" seen while CI flashed). So SystemView runs in a user and mount namespace whose /dev/bus/usb holds only the
recording probe's node, and none when decoding.

A fresh profile auto-loads SEGGER's sample recording, and a load sent while that is still in progress is dropped
(SystemView 4.10b), so this script waits for the window title to settle, loads a copy of the file under a name unique
to the run, repeats the load until the title names that copy, and only then exports. An export is complete once
SystemView has closed the file and its size holds.

The license notice ("Continue under SFL": non-commercial or educational use) and the J-Link terms of use are
accepted, as the operator has directed for every SEGGER notice. SystemView deletes /tmp/sv-* at startup, so the
namespace also gives it a private /tmp holding only the X server's socket directory and, under NS_TMP, its HOME and
--out: the host's /tmp is out of its reach, and the commands it is sent name those namespace paths.

Exit: 0 recorded and exported; 2 bad usage or a GUI request off Linux; 3 decoding, a tool, the probe, the workload or
an export failed, or a signal stopped the run (record.json says which).
"""
import argparse
import contextlib
import datetime
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_state as ps  # noqa: E402
from sysview_dump import control_block, prepare_out  # noqa: E402

TOOLS = ('systemview', 'Xvfb', 'xdotool', 'unshare')
USB_SYSFS, USB_DEV = Path('/sys/bus/usb/devices'), Path('/dev/bus/usb')
X11_DIR, NS_TMP = Path('/tmp/.X11-unix'), Path('/tmp/.sysview_record')
REGISTRY = ('SEGGER_REG_HKEY_CURRENT_USER.xml', 'JLinkDLL.ini')
BANNER = re.compile(rb'SEGGER SystemView V(\S+) Command Server')
# title fragment -> keys that answer it: mnemonics or the default button, never screen positions
DIALOGS = {
    'No Commercial-Use License': 'alt+c',    # "Continue under SFL"
    'System Information': 'Return',          # OK; the sample's summary
    'Recorder Configuration': 'Return',      # OK, then Finish: both pages prefilled from the -recorder arguments
    'Terms of use': 'alt+a',                 # J-Link DLL, one per on-board probe it connects to: Accept
    'SystemView Error': 'Return',            # e.g. "errors while loading from file" (-161): counted in record.json
}
RECORDING = 'New Recording'     # the main title while SystemView records (V4.10b)
# whole titles of windows left alone: "Recorder starting...", a progress box whose one button is Cancel
PROGRESS = ('systemview',)
DISPLAY_S, STARTUP_S, SETTLE_S, LOAD_S, LOAD_RETRY_S, EXPORT_S, QUIT_S, GROUP_S = 10, 60, 30, 120, 15, 300, 20, 5
POLL_S = 0.5


class Failure(Exception):
    pass


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def all_pids():
    return [int(p) for p in os.listdir('/proc') if p.isdigit()]


def fd_links(pid):
    try:
        names = os.listdir(f'/proc/{pid}/fd')
    except OSError:
        return []
    out = []
    for fd in names:
        try:
            out.append(os.readlink(f'/proc/{pid}/fd/{fd}'))
        except OSError:
            pass
    return out


def listening_sockets(port):
    """The fd link names ("socket:[inode]") of TCP sockets listening on port, from /proc."""
    out = set()
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if f[3] == '0A' and int(f[1].rsplit(':', 1)[1], 16) == port:
                out.add(f'socket:[{f[9]}]')
    return out


def group_members(pgid):
    """Live (not zombie) processes in group pgid: an orphan's zombie waits on whoever reaps it, which under a
    subreaper may be late, and kill(-pgid, 0) still counts it."""
    out = []
    for pid in all_pids():
        try:
            stat = Path(f'/proc/{pid}/stat').read_text()
        except OSError:
            continue
        fields = stat[stat.rindex(')') + 2:].split()    # after "pid (comm) ": state, ppid, pgrp, ...
        if int(fields[2]) == pgid and fields[0] != 'Z':
            out.append(pid)
    return out


def group_gone(proc, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc.poll()
        if not group_members(proc.pid):
            return True
        time.sleep(0.05)
    return False


@contextlib.contextmanager
def held_stops():
    """Stop signals held while a cleanup runs and delivered once it is done: one raised inside it would leave the
    groups it had not stopped yet running, each in a session of its own. A cleanup that fails keeps its own error."""
    held = []
    previous = {sig: signal.signal(sig, lambda signum, _frame: held.append(signum)) for sig in ps.STOP_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if held:
        signal.raise_signal(held[0])


def stop_group(proc):
    """SIGTERM, then SIGKILL, to the process group proc leads (started with start_new_session), until every member
    is gone; the leader may already have exited while a child it started runs on. False if one outlived SIGKILL."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        if group_gone(proc, GROUP_S):
            break
    proc.poll()
    return not group_members(proc.pid)


def usb_node(serial):
    """The /dev/bus/usb node of the one USB device whose serial is this probe's, leading zeros aside (J-Link
    serials are zero-padded in sysfs); Failure on none or several."""
    hits = []
    for dev in USB_SYSFS.iterdir():
        try:
            if ps.jlink_serial_match(serial, (dev / 'serial').read_text().strip()):
                bus, num = (int((dev / f).read_text()) for f in ('busnum', 'devnum'))
                hits.append(USB_DEV / f'{bus:03d}' / f'{num:03d}')
        except (OSError, ValueError):
            continue
    if len(hits) != 1:
        raise Failure(f'probe {serial}: {len(hits)} USB devices carry that serial ({", ".join(map(str, hits))})')
    return hits[0]


def usb_jail(cmd, keep, scratch, dirs):
    """cmd in a user and mount namespace whose /dev/bus/usb holds only the device nodes in keep, at their own paths,
    and whose /tmp is a private tmpfs holding X11_DIR and each host directory of dirs ({name: path}) at
    NS_TMP/name. Each node is bind-mounted aside before the empty tmpfs covers /dev/bus/usb, then back in place; the
    new /tmp is built on a tmpfs staged in scratch while the host's paths are still visible, then moved over /tmp.
    unshare execs sh, which execs cmd, so the pid stays cmd's."""
    q = shlex.quote
    steps = []
    for i, node in enumerate(keep):
        aside = scratch / f'usb{i}'
        aside.touch()
        steps.append(f'mount --bind {q(str(node))} {q(str(aside))}')
    steps.append(f'mount -t tmpfs none {q(str(USB_DEV))}')
    for i, node in enumerate(keep):
        steps += [f'mkdir -p {q(str(node.parent))}', f'touch {q(str(node))}',
                  f'mount --bind {q(str(scratch / f"usb{i}"))} {q(str(node))}']
    stage = scratch / 'tmp'
    stage.mkdir()
    steps.append(f'mount -t tmpfs none {q(str(stage))}')
    binds = {NS_TMP / name: Path(path) for name, path in dirs.items()}
    if X11_DIR.is_dir():
        binds[X11_DIR] = X11_DIR
    for seen, src in binds.items():
        dest = stage / seen.relative_to('/tmp')
        steps += [f'mkdir -p {q(str(dest))}', f'mount --bind {q(str(src))} {q(str(dest))}']
    steps.append(f'mount --move {q(str(stage))} /tmp')
    return ['unshare', '--user', '--map-root-user', '--mount', 'sh', '-ec', '; '.join(steps + ['exec "$@"']), 'sh',
            *cmd]


class SystemView:
    """One SystemView under its own Xvfb, HOME and port. close() leaves nothing of it running, whatever state
    construction or a run stopped in."""

    def __init__(self, out, args, usb_nodes=()):
        self.out, self.port = out, free_port()
        self.xvfb = self.sv = self.log = None
        self.version, self.owned, self.copied, self.answered = None, False, [], {}
        self.home = Path(tempfile.mkdtemp(prefix='sysview_record.'))
        try:
            segger = self.home / '.config' / 'SEGGER'
            segger.mkdir(parents=True)
            for name in REGISTRY:
                src = Path.home() / '.config' / 'SEGGER' / name
                if src.is_file():
                    shutil.copy2(src, segger / name)
                    self.copied.append(name)
            self.display = self.start_xvfb()
            self.ns = {'home': self.home, 'out': out}
            self.env = {**os.environ, 'HOME': str(self.ns_path(self.home)),
                        'XDG_CONFIG_HOME': str(self.ns_path(self.home / '.config')), 'DISPLAY': self.display}
            self.log = open(out / 'systemview.log', 'wb')
            cmd = usb_jail(['systemview', *args, '-single', '-port', str(self.port)], usb_nodes, self.home, self.ns)
            self.sv = subprocess.Popen(cmd, env=self.env,
                                       stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        except BaseException as e:
            try:
                self.close()
            except (Failure, OSError, subprocess.SubprocessError) as cleanup:
                e.cleanup_error = str(cleanup)
            raise

    def ns_path(self, path):
        """path, inside HOME or --out, as SystemView sees it in its namespace."""
        for name, base in self.ns.items():
            if path == base or base in path.parents:
                return NS_TMP / name / path.relative_to(base)
        raise ValueError(f'{path} is outside the directories SystemView sees')

    def start_xvfb(self):
        read, write = os.pipe()
        try:
            self.xvfb = subprocess.Popen(['Xvfb', '-displayfd', str(write), '-screen', '0', '1600x1000x24',
                                          '-nolisten', 'tcp'], pass_fds=(write,), stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
            os.close(write)
            write = None
            number, deadline = b'', time.monotonic() + DISPLAY_S
            while not number.endswith(b'\n'):
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([read], [], [], left)[0]:
                    break
                chunk = os.read(read, 16)
                if not chunk:
                    break
                number += chunk
        finally:
            os.close(read)
            if write is not None:
                os.close(write)
        if not number.strip().isdigit():
            raise Failure(f'Xvfb reported no display within {DISPLAY_S} s')
        return f':{number.strip().decode()}'

    def xdotool(self, *args):
        return subprocess.run(['xdotool', *args], env=self.env, capture_output=True, text=True, timeout=10)

    def windows(self):
        return [(wid, self.xdotool('getwindowname', wid).stdout.strip())
                for wid in self.xdotool('search', '--onlyvisible', '--name', '.').stdout.split()]

    def poll(self):
        """Answer the known dialogs and return the main window's title; Failure naming any other window."""
        main = ''
        for wid, title in self.windows():
            frag = next((f for f in DIALOGS if f in title), None)
            if frag:
                self.answered[frag] = self.answered.get(frag, 0) + 1
                keys = DIALOGS[frag]
                self.xdotool('windowfocus', '--sync', wid)
                self.xdotool('key', '--clearmodifiers', keys)
            elif title.startswith('SEGGER SystemView'):
                main = title
            elif title not in PROGRESS:
                raise Failure(f'unexpected SystemView dialog {title!r}')
        return main

    def check_owner(self):
        sockets = listening_sockets(self.port)
        self.owned = bool(sockets & set(fd_links(self.sv.pid)))
        if not self.owned:
            if not sockets:
                raise Failure(f'nothing listens on port {self.port}')
            pids = sorted(pid for pid in all_pids() if sockets & set(fd_links(pid)))
            raise Failure(f'port {self.port} is held by pid(s) {pids}, not this SystemView')

    def wait_ready(self):
        """Until this SystemView listens on the port, answering dialogs meanwhile."""
        deadline = time.monotonic() + STARTUP_S
        while time.monotonic() < deadline:
            if self.sv.poll() is not None:
                raise Failure(f'SystemView exited ({self.sv.returncode}) during startup')
            self.poll()
            if listening_sockets(self.port):
                self.check_owner()
                return
            time.sleep(POLL_S)
        raise Failure(f'SystemView did not open its command port {self.port} in {STARTUP_S} s')

    def send(self, command):
        self.check_owner()
        with socket.create_connection(('127.0.0.1', self.port), timeout=10) as s:
            s.settimeout(10)
            banner = s.recv(256)
            m = BANNER.search(banner)
            if not m:
                raise Failure(f'port {self.port} did not answer as the SystemView command server: {banner[:80]!r}')
            self.version = m.group(1).decode()
            s.sendall(command.encode() + b'\n')

    def settle(self):
        """The main title once it holds for 4 polls (SystemView's startup load has finished)."""
        deadline, last, still = time.monotonic() + SETTLE_S, None, 0
        while time.monotonic() < deadline:
            title = self.poll()
            still, last = (still + 1 if title == last else 0), title
            if still >= 4:
                return title
            time.sleep(POLL_S)
        raise Failure(f'the SystemView window did not settle in {SETTLE_S} s (title {self.poll()!r})')

    def load(self, path):
        """Load a copy of path named for this run only; the main title once it names that copy."""
        self.settle()
        copy = self.home / f'sysview-{uuid.uuid4().hex}.SVDat'
        shutil.copyfile(path, copy)
        shown = re.compile(r' - ' + re.escape(copy.name) + r'(?: - | \||$)')
        deadline, resend = time.monotonic() + LOAD_S, 0
        while time.monotonic() < deadline:
            if time.monotonic() >= resend:
                self.send(f'-load {self.ns_path(copy)}')
                resend = time.monotonic() + LOAD_RETRY_S
            title = self.poll()
            if shown.search(title):
                return title
            time.sleep(POLL_S)
        raise Failure(f'SystemView did not show {path.name} within {LOAD_S} s (title {self.poll()!r})')

    def write(self, command, path):
        """Run a command that writes path, which does not exist yet; its size once SystemView has closed it and
        the size holds."""
        seen = self.ns_path(path)
        self.send(f'{command} {seen}')
        deadline, last, still = time.monotonic() + EXPORT_S, -1, 0
        while time.monotonic() < deadline:
            self.poll()
            size = path.stat().st_size if path.exists() else -1
            done = size > 0 and size == last and str(seen) not in fd_links(self.sv.pid)
            still = still + 1 if done else 0
            if still >= 2:
                return size
            last = size
            time.sleep(POLL_S)
        raise Failure(f'{command} wrote no complete {path.name} within {EXPORT_S} s')

    def screenshot(self):
        if self.xvfb and shutil.which('import'):
            try:
                subprocess.run(['import', '-window', 'root', str(self.out / 'debug.png')], env=self.env,
                               capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass

    def close(self):
        """-quit when the port is still this SystemView's, then its process group and Xvfb's; the J-Link wedges
        when SystemView dies inside a transaction, hence the -quit first. Failure naming a group that outlived
        SIGKILL, once everything else is cleaned up."""
        left = []
        with held_stops():
            try:
                if self.sv:
                    if self.owned and self.sv.poll() is None:
                        try:
                            self.send('-quit')
                            self.sv.wait(QUIT_S)
                        except (OSError, Failure, subprocess.TimeoutExpired):
                            pass
                    if not stop_group(self.sv):
                        left.append(f'SystemView (group {self.sv.pid})')
            finally:
                try:
                    if self.xvfb and not stop_group(self.xvfb):
                        left.append(f'Xvfb (group {self.xvfb.pid})')
                finally:
                    if self.log:
                        self.log.close()
                    shutil.rmtree(self.home, ignore_errors=True)
            if left:
                raise Failure(f"still running after SIGKILL: {', '.join(left)}")


def exports(out, a):
    rows = [('-export-contexts', out / 'contexts.csv')]
    if not a.no_events:
        rows.append(('-export', out / 'events.txt'))
    if a.export_terminal:
        rows.append(('-export-terminal', out / 'terminal.csv'))
    return rows


def decode(sv, out, a, record, path):
    record['loadedTitle'] = sv.load(path)
    for cmd, dest in exports(out, a):
        record['exports'][dest.name] = sv.write(cmd, dest)


def start_recording(sv):
    """-start, repeated until SystemView asks for its recorder configuration (one sent while the startup sample
    loads is dropped), then until the title shows a recording. The workload and the recording window start only
    after it."""
    sv.settle()
    deadline, resend = time.monotonic() + STARTUP_S, 0
    while time.monotonic() < deadline:
        if 'Recorder Configuration' not in sv.answered and time.monotonic() >= resend:
            sv.send('-start')
            resend = time.monotonic() + LOAD_RETRY_S
        title = sv.poll()
        if RECORDING in title:
            return title
        time.sleep(POLL_S)
    raise Failure(f'the recording did not start within {STARTUP_S} s (title {sv.poll()!r})')


def record_live(sv, out, a, record):
    record['recordingTitle'] = start_recording(sv)
    record['recordingStart'] = now()
    workload, failure = None, None
    try:
        if a.workload:
            with open(out / 'workload.log', 'wb') as log:
                workload = subprocess.Popen(a.workload, shell=True, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
        deadline = time.monotonic() + a.duration_ms / 1000
        while time.monotonic() < deadline:
            sv.poll()
            time.sleep(min(POLL_S, max(0, deadline - time.monotonic())))
        sv.send('-stop')
        record['recordingEnd'] = now()
    finally:
        if workload:
            with held_stops():
                ended = 'exited' if workload.poll() is not None else 'deadline'
                stopped = stop_group(workload)
                record['workload'].update(exit=workload.returncode, ended=ended, stopped=stopped)
            if not stopped:
                failure = Failure(f'the workload (group {workload.pid}) outlived SIGKILL')
            elif ended == 'exited' and workload.returncode != 0:
                failure = Failure(f'the workload exited {workload.returncode} before the recording ended')
    saved = out / 'recording.SVDat'
    record['exports'][saved.name] = sv.write('-save', saved)
    decode(sv, out, a, record, saved)
    if failure:
        raise failure


def resolve_live(a, p):
    """The control block address for a live recording, after checking its arguments, or p.error()."""
    if a.hil_config or a.board:
        if not (a.hil_config and a.board):
            p.error('--hil-config and --board go together')
        try:
            flasher = ps.hil_flasher(a.hil_config, a.board)
            if flasher['name'] != 'jlink':
                raise ValueError(f"--board {a.board} is flashed over {flasher['name']!r}; SystemView records live "
                                 f"over a J-Link only (capture raw RTT with rtt.py, then --from-raw)")
            ps.hil_merge(a, a.board, probe=flasher.get('uid'), device=ps.hil_jlink_device(a.board, flasher))
        except ValueError as e:
            p.error(str(e))
    if not (a.probe and a.device):
        p.error('a live recording needs --probe and --device (or --hil-config with --board)')
    if not (a.interface and a.speed):
        p.error('a live recording needs --interface and --speed')
    if not re.fullmatch(r'[1-9][0-9]*', a.speed):
        p.error(f'--speed must be kHz, got {a.speed!r}')
    if a.duration_ms is None or a.duration_ms <= 0:
        p.error('a live recording needs a positive --duration-ms')
    return control_block(a, p)


def on_signal(signum, _frame):
    raise Failure(f'stopped by signal {signum}')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', required=True, help='new or empty output directory')
    p.add_argument('--from-raw', metavar='FILE', help='decode this raw capture instead of recording')
    p.add_argument('--gui', action='store_true', help='use the Linux SystemView GUI to decode --from-raw')
    p.add_argument('--hil-config', metavar='FILE', help="the project's HIL config json (a jlink entry)")
    p.add_argument('--board', metavar='NAME', help='board name in --hil-config')
    p.add_argument('--probe', help='J-Link serial')
    p.add_argument('--device', help='J-Link device name')
    p.add_argument('--interface', choices=['swd', 'jtag'], help='target interface, no default')
    p.add_argument('--speed', help='interface speed in kHz, no default')
    p.add_argument('--elf', help='the flashed ELF: _SEGGER_RTT from its symbol table')
    p.add_argument('--addr', help='the RTT control block address instead of --elf')
    p.add_argument('--duration-ms', type=int, help='how long to record')
    p.add_argument('--workload', metavar='CMD', help='shell command run for the recording, stopped at its end')
    p.add_argument('--no-events', action='store_true', help='skip events.txt (large: megabytes per second)')
    p.add_argument('--export-terminal', action='store_true', help='also export terminal.csv')
    from sysview_decode import DecodeError, decode_file

    a = p.parse_args(argv)
    native = bool(a.from_raw and not a.gui)
    if a.gui and not a.from_raw:
        p.error('--gui is for --from-raw; live recording already uses the GUI')
    if a.from_raw:
        live = [f for f in ('hil_config', 'board', 'probe', 'device', 'interface', 'speed', 'elf', 'addr',
                            'duration_ms', 'workload') if getattr(a, f) is not None]
        if live:
            p.error(f"--from-raw decodes a file: --{live[0].replace('_', '-')} is for a live recording")
        raw = Path(a.from_raw).resolve()
        if not raw.is_file() or not raw.stat().st_size:
            p.error(f'--from-raw {a.from_raw}: no such non-empty file')
        if not a.gui:
            if a.export_terminal:
                p.error('--export-terminal requires --gui; native decoding does not export the terminal channel')
    if not native and sys.platform != 'linux':
        p.error(f'Linux-only (Xvfb, /proc, POSIX process groups); this is {sys.platform}')
    if not a.from_raw:
        cb = resolve_live(a, p)
    missing = [t for t in TOOLS if not shutil.which(t)] if not native else []
    if missing:
        p.error(f"not on PATH: {', '.join(missing)} (SystemView's deb, xvfb, xdotool, util-linux)")
    out = Path(a.out).resolve()
    prepare_out(p, str(out))

    record = {'started': now(), 'argv': [shlex.quote(x) for x in (argv if argv is not None else sys.argv[1:])],
              'mode': 'decode' if a.from_raw else 'live', 'exports': {}}
    if native:
        record['decoder'] = 'native'
    if a.from_raw:
        sv_args = []
    else:
        record.update(probe=a.probe, device=a.device, interface=a.interface, speed=a.speed,
                      controlBlock=f'0x{cb:08x}', durationMs=a.duration_ms)
        if a.workload:
            record['workload'] = {'cmd': a.workload}
        sv_args = ['-recorder', 'J-Link', '-device', a.device, '-usb', a.probe, '-if', a.interface.upper(),
                   '-speed', a.speed, '-rttcbaddr', f'0x{cb:08X}']
    previous = {sig: signal.signal(sig, on_signal) for sig in ps.STOP_SIGNALS}
    rc, sv = 0, None
    try:
        try:
            if native:
                record['decode'] = decode_file(raw, out, a.no_events)
                for name in ('contexts.csv', 'events.txt'):
                    if (out / name).is_file():
                        record['exports'][name] = (out / name).stat().st_size
            else:
                sv = SystemView(out, sv_args, () if a.from_raw else (usb_node(a.probe),))
                record.update(display=sv.display, port=sv.port, registryCopied=sv.copied)
                sv.wait_ready()
                if a.from_raw:
                    decode(sv, out, a, record, raw)
                else:
                    record_live(sv, out, a, record)
        except (Failure, OSError, DecodeError, subprocess.SubprocessError, KeyboardInterrupt) as e:
            record['error'] = str(e) or type(e).__name__
            if hasattr(e, 'cleanup_error'):
                record['cleanupError'] = e.cleanup_error
            rc = 3
            if sv:
                sv.screenshot()
        finally:
            if sv:
                record['systemviewVersion'] = sv.version
                record['dialogsAnswered'] = sv.answered
                record['systemviewErrors'] = sv.answered.get('SystemView Error', 0)
                try:
                    sv.close()
                except (Failure, OSError, subprocess.SubprocessError) as e:
                    record['cleanupError'] = str(e)
                    rc = 3
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        record['ended'] = now()
        (out / 'record.json').write_text(json.dumps(record, indent=1), encoding='utf-8')
    print(json.dumps(record))
    return rc


if __name__ == '__main__':
    sys.exit(main())
