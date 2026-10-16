#!/usr/bin/env python3
"""One bounded openocd-esp32 session on an Espressif chip, as one JSON object plus transcripts
(the target-debug skill's espressif.md, Attach and Leave).

  esp_session.py inspect --out <new dir> <route> --rtos FreeRTOS|none --gdb <arch gdb> --elf <flashed.elf> \
                 (--anchor <console capture file> | --no-anchor <reason>) [--expr EXPR ...] [--timeout S]
  esp_session.py budget  --out <new dir> <route> --rtos FreeRTOS|none --gdb <arch gdb> --elf <flashed.elf> \
                 (--anchor <file> | --no-anchor <reason>) --at <location> [--at ...] [--timeout S]
  esp_session.py recover --out <new dir> <route> [--timeout S]
  esp_session.py leave   --receipt <dir>/receipt.json

  <route>: --board-cfg board/<chip>-builtin.cfg --adapter-serial <MAC-with-colons>
         | --board-cfg board/esp32s3-ftdi.cfg --adapter-serial <FTDI serial>
         | --interface-cfg interface/ftdi/<board>.cfg (--adapter-serial S | --usb-location L) --target-cfg target/<chip>.cfg
         [--adapter-speed kHz]

The configuration is generated in one order: the gdb, telnet and Tcl ports (free ones, picked
here, so a second openocd on the host is never the one driven), `set ESP_FLASH_SIZE 0` and
`set ESP_RTOS`, before the first -f (esp_common.cfg reads both when it is sourced; flash support
off means no flash breakpoints and no reset at the gdb attach through the memory-protection path),
then the route with its selector: after a board file, between the interface and target files.
A selector after a board file that sources an FTDI interface without a serial is untested, so
--usb-location goes with --interface-cfg only. ESP_SESSION_OPENOCD names openocd when it is not
`openocd` on PATH; its banner must name an esp32 build.

inspect: the server examines the cores (recorded from its log, then each core's state over the
Tcl port), gdb connects (which halts them; recorded from the log), reads `info threads`, a
backtrace and the caller's expressions (may-write-memory, may-write-registers, may-call-functions
and may-insert-breakpoints off), resumes and detaches. budget: gdb sets `breakpoint
always-inserted on` and inserts one `hbreak` per --at location in order, so each insertion is tried
at once; the budget is the count of distinct addresses inserted before the first refusal, or a lower
bound when none was refused. A location gdb cannot place (no such symbol) is reported unresolved,
and one at an address an earlier location already holds is reported duplicate (gdb inserts one
breakpoint per address); neither asks the hardware for a slot. A location gdb resolves to several
addresses takes a slot each and leaves no budget, as does any other gdb failure at a location (no
connection, no hardware breakpoint support): both fail the run. The firmware can hold triggers of
its own (espressif.md, "Breakpoint budgets").

Whatever gdb does, the session ends from this script over the Tcl port: every breakpoint and
watchpoint removed, each halted core resumed, each core's state read, then `shutdown`, never a
kill (a killed openocd leaves RISC-V chips in the Breakpoint boot loop: espressif.md, Leave). Each
Tcl connection must first answer the session token this script gave its openocd (`set
ESPS_SESSION`), and the commands follow on that connection, so a server that took the port
meanwhile is never driven. A shutdown that fails leaves the
server running and its receipt open: `leave --receipt` ends that server only, after checking the
process (argv and start time) and the token. Removal errors, from the end or in openocd's log,
fail the run with the `recover` hint. `recover` is the separately chosen `init; shutdown` session
(espressif.md, "After an abnormal end"), bounded by --timeout and never killed. Nothing here
resets, reflashes or kills on its own.

--anchor names the console capture already running (console_capture.py's --out file): its
size is recorded at the start and the end, so the bytes printed during the session can be read
from it and its .times file. --no-anchor states why there is none.

Output: one JSON object on stdout; <out> keeps openocd.log, gdb.log (values), gdb.err (errors
and monitor output; budget writes both into gdb.log to keep their order) and receipt.json.

Exit: 0 done and every core running at the end; 1 done, but an expression failed or a budget
location was refused or unresolved (reported); 2 bad usage; 3 openocd, gdb or the Tcl port failed (a
budget location gdb could not ask about or resolved to several addresses included; gdb exiting
non-zero or by a signal leaves no budget), the run was interrupted, a core was left halted, removal
is unchecked or failed, or the shutdown failed.
"""
import argparse
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

OBSERVER = ['set pagination off', 'set confirm off', 'set width 0', 'set may-write-memory off',
            'set may-write-registers off', 'set may-call-functions off']
TCL_END = b'\x1a'
_MARK_RE = re.compile(r'^ESPS (\S+)-BEGIN *\n(.*?)^ESPS \1-END *$', re.M | re.S)
_EXAMINE_RE = re.compile(r'^.*(Examination succeed|Examined |was reset|Reset cause).*$', re.M)
_CONNECT_RE = re.compile(r"^.*(accepting 'gdb'|New GDB Connection|Target halted|Reset cause|was reset).*$", re.M)


class ToolError(RuntimeError):
    pass


class Interrupted(Exception):
    pass


def free_ports(n):
    # kept identical with rtt.py's; the session token covers a port taken between this and openocd's bind
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(('127.0.0.1', 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def tool(env, name):
    exe = shutil.which(os.environ.get(env) or name)
    if not exe:
        raise ToolError(f'{os.environ.get(env) or name} not found - install it or set {env}')
    return exe


def route_args(a):
    if a.board_cfg:
        r = ['-f', a.board_cfg, '-c', f'adapter serial {a.adapter_serial}']
    else:
        sel = (f'adapter serial {a.adapter_serial}' if a.adapter_serial else f'adapter usb location {a.usb_location}')
        r = ['-f', a.interface_cfg, '-c', sel, '-f', a.target_cfg]
    return r + (['-c', f'adapter speed {a.adapter_speed}'] if a.adapter_speed else [])


def server_command(exe, a, ports, log, token=None):
    """The one place the configuration order is decided."""
    if ports:
        cmd = [exe, '-c', f'gdb port {ports[0]}', '-c', f'telnet port {ports[1]}', '-c', f'tcl port {ports[2]}',
               '-c', f'set ESPS_SESSION {token}']
    else:
        cmd = [exe, '-c', 'gdb port disabled', '-c', 'telnet port disabled', '-c', 'tcl port disabled']
    cmd += ['-c', 'set ESP_FLASH_SIZE 0']
    if a.rtos:
        cmd += ['-c', f'set ESP_RTOS {a.rtos}']
    return cmd + route_args(a) + ['-l', log]


class Tcl:
    """The port picked here could have been taken by another server meanwhile: a connection must answer
    this session's token before anything else is sent, and every later command goes down that same
    connection. A connection that fails is dropped; the next command opens and checks a new one."""

    def __init__(self, port, token, timeout=10):
        self.port, self.token, self.timeout, self.sock = port, token, timeout, None

    def _ask(self, cmd):
        try:
            self.sock.sendall(cmd.encode() + TCL_END)
            buf = b''
            while not buf.endswith(TCL_END):
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ToolError(f'Tcl port {self.port} closed before answering {cmd!r}')
                buf += chunk
        except BaseException:
            self.close()
            raise
        return buf[:-1].decode(errors='replace')

    def __call__(self, cmd):
        if self.sock is None:
            self.sock = socket.create_connection(('127.0.0.1', self.port), timeout=self.timeout)
            got = self._ask('if {[info exists ESPS_SESSION]} {set ESPS_SESSION}')
            if got != self.token:
                self.close()
                raise ToolError(f'Tcl port {self.port} answers for another server (session {got!r}): nothing sent')
        return self._ask(cmd)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def states(self):
        out = self('set r {}; foreach t [target names] {append r "$t [$t curstate]\n"}; set r')
        return dict(line.rsplit(' ', 1) for line in out.splitlines() if line.strip())

    def end(self):
        """Remove every breakpoint and watchpoint, resume each halted core, read the states, shut down."""
        rec = {'removed': self('set e {}; foreach t [target names] {targets $t; if {[catch {rbp all} m]} '
                               '{lappend e "$t rbp: $m"}; if {[catch {rwp all} m]} {lappend e "$t rwp: $m"}}; set e')}
        before = self.states()
        for t, state in before.items():
            if state == 'halted':
                self(f'targets {t}; catch {{resume}}')
        rec.update(beforeResume=before, cores=self.states())
        try:
            self('shutdown')
        except (OSError, ToolError):
            pass    # the server closes the connection as it shuts down
        self.close()
        return rec


def read(path):
    with open(path, errors='replace') as f:
        return f.read()


RECOVER_HINT = 'run `recover` before an esptool reset (espressif.md, "After an abnormal end")'


def end_errors(result, log):
    """The verdict on an ended session: cores left not running, and removal errors from the end's
    rbp/rwp or openocd's log; a log that cannot be read is no evidence of a clean removal."""
    errors, end = [], result.get('end') or {}
    halted = [t for t, s in end.get('cores', {}).items() if s != 'running']
    if halted:
        errors.append(f"left not running: {', '.join(halted)}")
    try:
        bad = [ln for ln in read(log).splitlines() if re.search(r'could not remove (break|watch)point', ln, re.I)]
    except OSError as e:
        bad = [f'openocd log unreadable, removal unchecked: {e}']
    if end.get('removed', '').strip():
        bad.append(end['removed'].strip())
    if bad:
        result['removeErrors'] = bad
        errors.append(f'breakpoint/watchpoint removal errors: {RECOVER_HINT}')
    return errors


def start_time(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def wait_for(proc, path, pattern, deadline):
    while time.monotonic() < deadline:
        if re.search(pattern, read(path), re.M):
            return
        if proc is not None and proc.poll() is not None:
            raise ToolError(f'openocd exited {proc.returncode}; see {path}')
        time.sleep(0.1)
    raise ToolError(f'openocd did not print {pattern!r} in time; see {path}')


def wait_exit(pid, proc, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                return True
        elif not os.path.exists(f'/proc/{pid}'):
            return True
        time.sleep(0.1)
    return False


def block(name, cmd, newline=''):
    return [f'echo ESPS {name}-BEGIN \\n', cmd, f'echo {newline}ESPS {name}-END \\n']


def gdb_commands(mode, port, exprs, locations):
    cmds = OBSERVER + (['set may-insert-breakpoints off'] if mode == 'inspect' else
                       ['set breakpoint always-inserted on'])
    cmds += [f'target extended-remote :{port}']
    if mode == 'inspect':
        cmds += block('THREADS', 'info threads') + block('BT', 'bt 16')
        for i, e in enumerate(exprs):
            cmds += block(f'EXPR{i}', f'output {e}', newline='\\n')
    else:
        for i, loc in enumerate(locations):
            cmds += block(f'AT{i}', f'hbreak {loc}')
        cmds += ['delete']
    return cmds + ['monitor resume', 'detach']


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        proc.send_signal(sig)
        try:
            proc.wait(5)
            return
        except subprocess.TimeoutExpired:
            continue


def size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def write_receipt(path, rec):
    with open(path, 'w') as f:
        json.dump(rec, f, indent=1)


def cmdline(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return [x.decode(errors='replace') for x in f.read().split(b'\0') if x]
    except OSError:
        return None


def leave(receipt_path):
    with open(receipt_path) as f:
        rec = json.load(f)
    result = {'mode': 'leave', 'receipt': receipt_path, 'pid': rec.get('pid')}
    if rec.get('closed'):
        result['error'] = 'the receipt is closed: that server already shut down'
        return result, 2
    running = cmdline(rec['pid'])
    # a script stand-in runs under its interpreter, which /proc lists first
    if running is None or running[-len(rec['argv']):] != rec['argv'] or start_time(rec['pid']) != rec.get('startTime'):
        result['error'] = (f"process {rec['pid']} is not the openocd this receipt names (gone, or another "
                           f'process): nothing sent')
        return result, 2
    try:
        result['end'] = Tcl(rec['ports'][2], rec['token']).end()
    except (OSError, ToolError) as e:
        result['error'] = f'Tcl port {rec["ports"][2]}: {e}'
        return result, 3
    if not wait_exit(rec['pid'], None, 10):
        result['error'] = 'openocd still runs after shutdown'
        return result, 3
    write_receipt(receipt_path, {**rec, 'closed': True})
    errors = end_errors(result, rec.get('log', ''))
    if errors:
        result['error'] = '; '.join(errors)
        return result, 3
    return result, 0


def parse_inspect(result, blocks, exprs, gdb_err):
    result['threads'] = blocks.get('THREADS', '').splitlines() or None
    result['backtrace'] = blocks.get('BT', '').splitlines() or None
    result['exprs'] = []
    for i, e in enumerate(exprs):
        out = blocks.get(f'EXPR{i}')
        result['exprs'].append({'expr': e, 'output': out.strip('\n')} if out and out.strip() else
                               {'expr': e, 'error': 'not reached' if out is None else 'no value; its error is in gdbStderr'})
    err_lines = [ln for ln in read(gdb_err).splitlines() if ln.strip()] if os.path.exists(gdb_err) else []
    if err_lines:
        result['gdbStderr'] = err_lines
    return any('error' in r for r in result['exprs'])


def parse_budget(result, blocks, locations):
    """Each location in order: inserted, refused (the budget is the count inserted before it),
    unresolved (gdb found no such location and asked the hardware nothing), or duplicate (an address
    already held: gdb inserts one breakpoint per address, and -batch prints no "also set at" note).
    gdb failing another way (no connection, no hardware breakpoint support) anywhere, or a location
    not reached or resolved to several addresses before the first refusal, leaves no budget and is
    returned as the problem."""
    rows, budget, inserted, problem, held = [], None, 0, None, set()
    for i, loc in enumerate(locations):
        out = blocks.get(f'AT{i}')
        lines = out.strip().splitlines() if out else []
        placed = re.search(r'^Hardware assisted breakpoint \d+ at (0x[0-9a-fA-F]+)', out or '', re.M)
        if out is None:
            row = {'at': loc, 'result': 'not reached'}
        elif placed and re.search(r'\(\d+ locations\)', out):
            row = {'at': loc, 'result': 'several addresses', 'output': lines}
        elif placed and re.search(r'Cannot insert|Could not insert|Remote failure', out):
            row = {'at': loc, 'result': 'refused', 'output': lines}
        elif placed:
            row = {'at': loc, 'result': 'duplicate' if placed[1] in held else 'inserted', 'address': placed[1]}
            held.add(placed[1])
        elif re.search(r'not defined|No symbol|No source file named|No line \d+|malformed linespec', out):
            row = {'at': loc, 'result': 'unresolved', 'output': lines}
        else:
            row = {'at': loc, 'result': 'failed', 'output': lines}
        rows.append(row)
        if problem is not None:
            continue
        if row['result'] == 'failed' or row['result'] == 'not reached' and budget is None:
            problem = f'gdb did not ask the hardware for {loc!r}'
        elif row['result'] == 'several addresses' and budget is None:
            problem = f'{loc!r} resolved to several addresses, a slot each: give one address per --at'
        elif budget is None:
            if row['result'] == 'inserted':
                inserted += 1
            elif row['result'] == 'refused':
                budget = {'exactly': inserted}
    if problem is not None:
        budget = None
    elif budget is None:
        budget = {'atLeast': inserted}
    result['locations'], result['budget'] = rows, budget
    return any(r['result'] not in ('inserted', 'duplicate') for r in rows), problem


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('mode', choices=['inspect', 'budget', 'recover', 'leave'])
    p.add_argument('--out', metavar='DIR', help='new directory for the transcripts and the receipt')
    p.add_argument('--receipt', metavar='FILE', help='leave: the receipt.json an earlier run wrote')
    p.add_argument('--board-cfg', metavar='CFG')
    p.add_argument('--interface-cfg', metavar='CFG')
    p.add_argument('--target-cfg', metavar='CFG')
    p.add_argument('--adapter-serial', metavar='S', help='the USJ MAC with colons, or the FTDI EEPROM serial')
    p.add_argument('--usb-location', metavar='L', help='bus-port[.port...] of an FTDI without a serial')
    p.add_argument('--adapter-speed', metavar='KHZ', help='only when the cfg files do not set it')
    p.add_argument('--rtos', choices=['FreeRTOS', 'none'], help='ESP_RTOS: none for the ROM and the bootloader')
    p.add_argument('--gdb', metavar='PATH', help="the chip architecture's gdb (espressif.md, the chips table)")
    p.add_argument('--elf', metavar='FILE', help='the ELF that is flashed')
    p.add_argument('--anchor', metavar='FILE', help="the running console capture's --out file")
    p.add_argument('--no-anchor', metavar='REASON', help='why there is no console capture')
    p.add_argument('--expr', action='append', default=[], metavar='EXPR', help='inspect: read-only gdb expression')
    p.add_argument('--at', action='append', default=[], metavar='LOCATION', help='budget: hbreak location, in order')
    p.add_argument('--timeout', type=float, default=60,
                   help='bound on the gdb session, or on recover, in seconds (default 60)')
    a = p.parse_args(argv)
    if a.mode == 'leave':
        if not a.receipt or a.out:
            p.error('leave takes --receipt only')
        try:
            result, rc = leave(a.receipt)
        except (OSError, ValueError, KeyError) as e:
            p.error(f'--receipt {a.receipt}: {e}')
        print(json.dumps(result))
        return rc
    if not a.out:
        p.error(f'{a.mode} needs --out')
    if bool(a.board_cfg) == bool(a.interface_cfg or a.target_cfg):
        p.error('give --board-cfg, or --interface-cfg with --target-cfg')
    if a.interface_cfg and not a.target_cfg or a.target_cfg and not a.interface_cfg:
        p.error('--interface-cfg and --target-cfg go together')
    if bool(a.adapter_serial) == bool(a.usb_location):
        p.error('give exactly one selector: --adapter-serial or --usb-location')
    if a.board_cfg and a.usb_location:
        p.error('--usb-location goes with --interface-cfg (a selector after a board file is untested)')
    if a.adapter_speed and not re.fullmatch(r'[1-9][0-9]*', a.adapter_speed):
        p.error('--adapter-speed is kHz')
    for v in (a.board_cfg, a.interface_cfg, a.target_cfg, a.adapter_serial, a.usb_location):
        if v and not re.fullmatch(r'[A-Za-z0-9_./:+-]+', v):
            p.error(f'{v!r}: letters, digits and _ . / : + - only (it reaches an openocd command)')
    if not (math.isfinite(a.timeout) and a.timeout > 0):
        p.error('--timeout must be a finite positive number of seconds')
    session = a.mode in ('inspect', 'budget')
    if session:
        if not (a.rtos and a.gdb and a.elf):
            p.error(f'{a.mode} needs --rtos, --gdb and --elf')
        if bool(a.anchor) == bool(a.no_anchor):
            p.error('give --anchor <console capture file> or --no-anchor <reason>')
        if a.anchor and not os.path.isfile(a.anchor):
            p.error(f'--anchor {a.anchor}: no such file (start console_capture.py first)')
        if not os.path.isfile(a.elf):
            p.error(f'--elf {a.elf}: no such file')
        for e in a.expr + a.at:
            if not e.strip() or '\n' in e:
                p.error(f'{e!r}: one non-empty line')
        if a.mode == 'budget' and (not a.at or a.expr):
            p.error('budget takes --at locations and no --expr')
        if a.mode == 'inspect' and a.at:
            p.error('--at is for budget')
    elif a.rtos or a.gdb or a.elf or a.anchor or a.no_anchor or a.expr or a.at:
        p.error('recover takes the route only')
    try:
        exe = tool('ESP_SESSION_OPENOCD', 'openocd')
        gdb = shutil.which(a.gdb) if session else None
        if session and not gdb:
            raise ToolError(f'--gdb {a.gdb}: not found')
        banner = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=10)
        if '-esp32-' not in banner.stdout + banner.stderr:
            raise ToolError(f'{exe} is not openocd-esp32 (its banner names no esp32 build)')
        os.mkdir(a.out)
    except FileExistsError:
        p.error(f'--out {a.out}: exists; give a new directory')
    except (OSError, ToolError, subprocess.TimeoutExpired) as e:
        print(json.dumps({'mode': a.mode, 'error': str(e)}))
        return 3
    version = (banner.stdout + banner.stderr).strip().splitlines()[0]
    log = os.path.join(a.out, 'openocd.log')
    result = {'mode': a.mode, 'openocd': version, 'out': a.out}
    if a.mode == 'recover':
        cmd = server_command(exe, a, None, log) + ['-c', 'init', '-c', 'shutdown']
        result['server'] = cmd
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            proc.wait(a.timeout)
        except subprocess.TimeoutExpired:
            # never killed: a killed openocd is what recover exists to clean up after
            result.update(pid=proc.pid, error=f'init; shutdown still runs after {a.timeout:.0f} s (pid {proc.pid}, '
                                              f'not killed); see {log}')
            print(json.dumps(result))
            return 3
        result['exit'] = proc.returncode
        errors = [f'openocd exited {proc.returncode}; see {log}'] if proc.returncode else []
        try:
            result['examination'] = [m.group(0) for m in _EXAMINE_RE.finditer(read(log))]
        except OSError as e:
            errors.append(f'openocd log unreadable: {e}')
        errors += end_errors(result, log)
        if errors:
            result['error'] = '; '.join(errors)
        print(json.dumps(result))
        return 3 if errors else 0
    ports, token = free_ports(3), secrets.token_hex(8)
    tcl = Tcl(ports[2], token)
    cmd = server_command(exe, a, ports, log, token)
    receipt = os.path.join(a.out, 'receipt.json')
    result.update(server=cmd, ports=ports, receipt=receipt,
                  anchor={'file': a.anchor, 'sizeAtStart': size_of(a.anchor)} if a.anchor else
                  {'none': a.no_anchor})
    signals, armed = [], True

    def on_signal(signum, _frame):
        nonlocal armed
        signals.append(signum)
        if armed:
            armed = False
            raise Interrupted(f'interrupted by signal {signum}')

    server = gdb_proc = None
    gdb_log, gdb_err = os.path.join(a.out, 'gdb.log'), os.path.join(a.out, 'gdb.err')
    errors = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, on_signal)
        open(log, 'w').close()
        server = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        receipt_rec = {'pid': server.pid, 'startTime': start_time(server.pid), 'argv': cmd, 'ports': ports,
                       'token': token, 'log': os.path.abspath(log), 'closed': False,
                       'startedAt': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
        write_receipt(receipt, receipt_rec)
        wait_for(server, log, rf'Listening on port {ports[0]} for gdb connections', time.monotonic() + 30)
        text = read(log)
        result['examination'] = {'log': [m.group(0) for m in _EXAMINE_RE.finditer(text)], 'cores': tcl.states()}
        mark = len(text)
        argv = [gdb, '-nx', '-batch', a.elf]
        for c in gdb_commands(a.mode, ports[0], a.expr, a.at):
            argv += ['-ex', c]
        with open(gdb_log, 'w') as out, open(gdb_err, 'w') as err:
            gdb_proc = subprocess.Popen(argv, stdout=out, stderr=out if a.mode == 'budget' else err,
                                        stdin=subprocess.DEVNULL)
            try:
                gdb_proc.wait(a.timeout)
            except subprocess.TimeoutExpired:
                errors.append(f'gdb did not finish within {a.timeout:.0f} s; see {gdb_log}')
            else:
                # -batch exits non-zero only when its last command (detach) failed
                if gdb_proc.returncode < 0:
                    errors.append(f'gdb was killed by signal {-gdb_proc.returncode}; see {gdb_log}')
                elif gdb_proc.returncode:
                    errors.append(f'gdb exited {gdb_proc.returncode} (detach failed); see {gdb_log}')
        result['connect'] = [m.group(0) for m in _CONNECT_RE.finditer(read(log)[mark:])]
    except (ToolError, OSError, Interrupted) as e:
        errors.append(str(e))
    finally:
        armed = False
        stop(gdb_proc)
    if gdb_proc is not None:
        result['gdbExit'] = gdb_proc.returncode
    try:
        if server is not None and server.poll() is None:
            result['end'] = tcl.end()
            if wait_exit(server.pid, server, 10):
                write_receipt(receipt, {**receipt_rec, 'closed': True})
            else:
                errors.append(f'openocd still runs after shutdown; end it with: esp_session.py leave --receipt {receipt}')
        elif server is not None:
            errors.append(f'openocd exited {server.returncode} during the session; see {log}')
            write_receipt(receipt, {**receipt_rec, 'closed': True})
    except (OSError, ToolError) as e:
        errors.append(f'ending the session over the Tcl port: {e}; the server is left running: '
                      f'esp_session.py leave --receipt {receipt}')
    gdb_text = read(gdb_log) if os.path.exists(gdb_log) else ''
    blocks = dict(_MARK_RE.findall(gdb_text))
    if a.mode == 'inspect':
        failed = parse_inspect(result, blocks, a.expr, gdb_err)
        if result['backtrace'] is None and not errors:
            errors.append(f'gdb session incomplete (no backtrace); see {gdb_log} and {gdb_err}')
    else:
        failed, problem = parse_budget(result, blocks, a.at)
        if result.get('gdbExit') != 0:
            result['budget'] = None    # a gdb that timed out, was interrupted or failed its detach
        if problem and not errors:
            errors.append(f'{problem}; see {gdb_log}')
    if a.anchor:
        result['anchor']['sizeAtEnd'] = size_of(a.anchor)
    errors += end_errors(result, log)
    if signals:
        errors.append(f"interrupted by signal {', '.join(map(str, signals))}")
    if errors:
        result['error'] = '; '.join(dict.fromkeys(errors))
    print(json.dumps(result))
    return 3 if errors else (1 if failed else 0)

if __name__ == '__main__':
    sys.exit(main())
