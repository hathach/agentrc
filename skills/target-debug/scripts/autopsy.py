#!/usr/bin/env python3
"""Halted state autopsy of a Cortex-M rig board over gdb, as one JSON object: the core state
before the attach, PC, its symbol, a backtrace, ICSR, caller expressions and memory dumps
read while halted, then the core resumed and its state checked again from a separate
probe session.

  autopsy.py --hil-config <file> --board <name> [--interface swd --speed 4000] --elf <flashed.elf> \
             --out <new dir> [--expr EXPR ...] [--dump ADDR LEN FILE ...] [--timeout S]

The board's HIL config entry gives the backend and probe as for probe_state.py. The
caller holds the project's board lock for the whole run; this script takes none.

Order: a probe_state observation (the pre-attach anchor: each core's DHCSR); the gdb server
(JLinkGDBServer -singlerun, or openocd with a gdb port and no memory map, so its gdb connect
probes no flash) started here on free ports and pinned to the probe serial; one gdb batch
whose attach halts every core (both servers do; openocd's gdb-attach default is `halt 1000`,
and its gdb connect also removes every breakpoint and watchpoint); the reads; `monitor go`
(J-Link) or `monitor resume` (openocd, after polling every core) when every core was running
before the attach; detach (neither server resumes on it); the server stopped; a second
probe_state observation (the end anchor). When that finds a core not as found, it is kept
as `afterServer` and a third observation is the reported end state; for a core found
running and left halted (a gdb that timed out, a failed resume) a restoring session comes
first (openocd: poll every core, then resume each halted one; J-Link: the end anchor's own
close restarted it).

A J-Link session halts the core when it closes and restarts it unless told not to (`exec
SetRestartOnClose = 0`): the pre-attach anchor keeps the core halted for the attach, and for
a core found halted gdb sends that setting right after the attach and the end anchor keeps
it too. A gdb that dies between the attach and that command leaves the core to the
server's close, which restarts it: reported as a core that ran, which the end anchor's close
halted again. If the pre-attach anchor itself fails, the core is restarted only when its partial
output shows it was found running; otherwise its state is reported unknown. A core found
halted is not resumed, and a multi-core target found with mixed core states is refused,
since the attach halts every core and a resume moves every core.

gdb runs with may-write-memory, may-write-registers, may-call-functions and
may-insert-breakpoints off, so expressions read and never write, call or break. Each
command is its own -ex, so a failing one does not skip the resume; gdb prints errors on
stderr (gdb.err, with monitor output; reported as gdbStderr) and values on stdout (gdb.log),
so an expression with no value on stdout failed. The session counts only when gdb exits 0
(the last command is the detach) and the DHCSR, PC, ICSR and backtrace came back. No
watchpoints in this version.

Dumps are read while halted, so they are coherent with the PC; the same address rules as
probe_state.py --save apply, and an existing file is refused.

The first SIGINT or SIGTERM stops the session; the cleanup, end anchor and restore still
run (each bounded), and later signals are only recorded.

AUTOPSY_GDB, AUTOPSY_JLINK_GDBSERVER and PROBE_STATE_OPENOCD name the tools when they are
not `arm-none-eabi-gdb`, `JLinkGDBServer` and `openocd` on PATH; PROBE_STATE_JLINK_EXE
names J-Link Commander for the anchors.

Exit: 0 autopsy complete and the end state as found; 1 complete but an expression or dump
failed (each reported); 2 bad usage; 3 the probe, a tool, the server or the gdb session
failed, the run was interrupted, or the end state is not the one found (a core left
halted, or one found halted now running).
"""
import argparse
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_state  # noqa: E402
from probe_state import Capture, OpenOCD, ToolError, core_state, cpuid_state  # noqa: E402

ICSR = 0xE000ED04
BACKTRACE_FRAMES = 16
SERVER_READY = {'jlink': 'Waiting for GDB connection', 'openocd': 'for gdb connections'}
OBSERVER = ['set may-write-memory off', 'set may-write-registers off', 'set may-call-functions off',
            'set may-insert-breakpoints off', 'set pagination off', 'set confirm off', 'set width 0']
NO_RESTART = 'exec SetRestartOnClose = 0'
# Every core polled before any resume: an SMP resume fails for a core OpenOCD has not polled
# (verified 2026-10-16 raspberry_pi_pico), and the next poll then halted both.
POLL_ALL = 'foreach t [target names] {$t arp_poll}'
# A resume also restores the others, so later ones may fail. `resume` acts on the current
# target: a target instance has no resume command (target.c).
OCD_RESTORE = (f'{POLL_ALL}\n'
               'foreach t [target names] {if {[$t curstate] eq "halted"} {targets $t; catch {resume}}}')
_MARK_RE = re.compile(r'^AUTOPSY (\S+) (.*)$', re.M)


class Interrupted(Exception):
    pass


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(('127.0.0.1', 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def anchor(backend, timeout, keep_halted):
    """{'cpuid', 'cores': {name: core_state}} from one probe_state observation. keep_halted leaves a
    J-Link core halted after the session, whatever it found; an openocd exit never changes the run state."""
    cap = Capture(1, 0, False, [])
    vals = cap.unpack(backend.observe(cap, timeout, keep_halted))
    probe_state.check_m_profile(vals['cpuid'])
    if isinstance(backend, OpenOCD):
        cores = {backend.current: core_state(vals['dhcsr1'])}
        cores.update({n: core_state(a) for n, (_, a) in backend.others.items()})
    else:
        cores = {'core': core_state(vals['dhcsr1'])}
    return {'cpuid': cpuid_state(vals['cpuid']), 'cores': cores}


def server_command(route, ident, backend, a, ports, exe):
    if route == 'jlink':
        return [exe, '-select', f"usb={ident['probe']}", '-device', ident['device'], '-if', a.interface,
                '-speed', a.speed, '-port', str(ports[0]), '-swoport', str(ports[1]),
                '-telnetport', str(ports[2]), '-nogui', '-singlerun']
    return backend.command(gdb_port=ports[0])


def gdb_commands(route, port, exprs, dumps, resume, multicore):
    cmds = OBSERVER + [f'target remote :{port}']
    if route == 'jlink' and not resume:
        cmds.append(f'monitor {NO_RESTART}')   # first, so a server that exits early leaves the core halted
    cmds += ['printf "AUTOPSY DHCSR 0x%08x\\n", *(unsigned *)0xE000EDF0',
                       'printf "AUTOPSY PC 0x%08x\\n", $pc',
                       'echo AUTOPSY SYMBOL-BEGIN \\n', 'info symbol $pc', 'echo AUTOPSY SYMBOL-END \\n',
                       f'printf "AUTOPSY ICSR 0x%08x\\n", *(unsigned *)0x{ICSR:08X}',
                       'echo AUTOPSY BT-BEGIN \\n', f'bt {BACKTRACE_FRAMES}', 'echo AUTOPSY BT-END \\n']
    if multicore:
        cmds += ['echo AUTOPSY THREADS-BEGIN \\n', 'info threads', 'echo AUTOPSY THREADS-END \\n']
    for i, e in enumerate(exprs):
        cmds += [f'echo AUTOPSY EXPR{i}-BEGIN \\n', f'output {e}', f'echo \\nAUTOPSY EXPR{i}-END \\n']
    for i, (path, addr, size) in enumerate(dumps):
        cmds += [f'echo AUTOPSY DUMP{i}-BEGIN \\n', f'dump binary memory {path} 0x{addr:x} 0x{addr + size:x}',
                 f'echo AUTOPSY DUMP{i}-END \\n']
    if route == 'jlink' and resume:
        cmds += ['monitor go']
    elif resume:
        cmds += [f'monitor {POLL_ALL}'] if multicore else []
        cmds += ['monitor resume']
    return cmds + ['detach']


def block(text, name):
    m = re.search(rf'^AUTOPSY {name}-BEGIN *\n(.*?)^AUTOPSY {name}-END *$', text, re.M | re.S)
    return None if m is None else m.group(1).strip('\n')


def wait_ready(proc, log_path, marker, deadline):
    while time.monotonic() < deadline:
        with open(log_path, errors='replace') as f:
            if marker in f.read():
                return
        if proc.poll() is not None:
            raise ToolError(f'gdb server exited {proc.returncode} before listening; see {log_path}')
        time.sleep(0.1)
    raise ToolError(f'gdb server did not report "{marker}" in time; see {log_path}')


def stop(proc, grace):
    """Let the process exit by itself within `grace` s, then SIGTERM (openocd ignored SIGINT), then SIGKILL."""
    if proc is None or proc.poll() is not None:
        return
    for sig, wait in ((None, grace), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if sig is not None:
            proc.send_signal(sig)
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def start_anchor_recovery(route, backend, err):
    """What a failed pre-attach anchor left, restarting a J-Link core only if it was found running."""
    if route != 'jlink':
        return ''
    partial = getattr(err, 'partial', [])
    if len(partial) < 2:    # CPUID, then the first DHCSR
        return ('; the run state before it is unknown, and a J-Link session that reached the target left the '
                'core halted: check it with probe_state.py')
    if core_state(partial[1])['halted']:
        return '; the core was found halted and stays halted'
    try:
        anchor(backend, 30, keep_halted=False)
        return '; the core was found running and a second session restarted it'
    except (ToolError, Interrupted) as e:
        return f'; the core was found running and the restarting session failed too ({e}): check it with probe_state.py'


def restore(route, backend, timeout):
    """None, or why the restoring session failed. J-Link has nothing to send: the end anchor that
    found the core halted restarted it when it closed."""
    if route == 'jlink':
        return None
    try:
        backend.batch(OCD_RESTORE, timeout)
        return None
    except (ToolError, Interrupted) as e:
        return str(e)


def end_state_error(before, after, resumed):
    """The contract: a core found running runs again, a core found halted stays halted."""
    wrong = []
    for name, b in before['cores'].items():
        a = after['cores'].get(name)
        if a is None:
            wrong.append(f'{name} missing from the end anchor')
        elif b['halted'] != a['halted']:
            wrong.append(f"{name} found {'halted' if b['halted'] else 'running'}, now "
                         f"{'halted' if a['halted'] else 'running'} (DHCSR {a['raw']})")
    if not wrong:
        return None
    hint = ' - check it with probe_state.py; resume a halted core (J-Link `g`, openocd `resume`)' if resumed else ''
    return '; '.join(wrong) + hint


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    probe_state.add_board_args(p)
    p.add_argument('--elf', required=True, metavar='FILE', help='the ELF flashed on the board, for symbols')
    p.add_argument('--out', required=True, metavar='DIR', help='new directory for the gdb and server transcripts')
    p.add_argument('--expr', metavar='EXPR', action='append', default=[],
                   help='a gdb expression to print while halted (repeatable; read-only, enforced by gdb)')
    p.add_argument('--dump', metavar=('ADDR', 'LEN', 'FILE'), nargs=3, action='append', default=[],
                   help='copy LEN bytes from ADDR to a new FILE while halted (repeatable)')
    p.add_argument('--timeout', type=float, default=60, help='bound on the gdb session in seconds (default 60)')
    a = p.parse_args(argv)
    if not (math.isfinite(a.timeout) and a.timeout > 0):
        p.error('--timeout must be a finite positive number of seconds')
    for e in a.expr:
        if not e.strip() or '\n' in e:
            p.error(f'--expr {e!r}: one non-empty line')
    try:
        with open(a.elf, 'rb') as f:
            if f.read(4) != b'\x7fELF':
                p.error(f'--elf {a.elf} is not an ELF file')
    except OSError as e:
        p.error(f'--elf {a.elf}: {e}')
    route, ident, backend = probe_state.resolve(a, p)
    transcripts = [os.path.join(os.path.abspath(a.out), n) for n in ('gdb.log', 'gdb.err', 'server.log')]
    dumps = probe_state.parse_regions(p, '--dump', a.dump, probe_state.is_rp2040(ident), transcripts)
    for path, _, _ in dumps:
        if os.path.lexists(path):
            p.error(f'--dump {path}: exists; a dump never overwrites')
    try:
        gdb = probe_state.tool('AUTOPSY_GDB', 'arm-none-eabi-gdb', 'arm-none-eabi-gdb.exe')
        server_exe = (probe_state.tool('AUTOPSY_JLINK_GDBSERVER', 'JLinkGDBServer', 'JLinkGDBServerCL.exe')
                      if route == 'jlink' else None)
        os.mkdir(a.out)
    except FileExistsError:
        p.error(f'--out {a.out}: exists; give a new directory')
    except (OSError, ToolError) as e:
        print(json.dumps({'board': a.board, 'error': str(e)}))
        return 3
    result = {'board': a.board, 'backend': route, **ident, 'elf': a.elf, 'out': a.out}
    signals, armed = [], [True]

    def on_signal(signum, _frame):
        signals.append(signum)
        if armed[0]:
            armed[0] = False
            raise Interrupted(f'interrupted by signal {signum}')

    server = gdb_proc = before = None
    server_log = os.path.join(a.out, 'server.log')
    gdb_log, gdb_err = os.path.join(a.out, 'gdb.log'), os.path.join(a.out, 'gdb.err')
    errors, text, errtext = [], '', ''
    touched = False
    # One protected block from the first probe session to the server's stop, so a signal anywhere
    # in it still reaches the cleanup, the end anchor and the restore.
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, on_signal)
        touched = True
        before = anchor(backend, 30, keep_halted=True)
        result['before'] = before
        halted = [n for n, c in before['cores'].items() if c['halted']]
        if halted and len(halted) < len(before['cores']):
            result['error'] = (f"mixed core states before the attach ({', '.join(halted)} halted): the attach halts "
                               f'every core and a resume would move every core')
            print(json.dumps(result))
            return 3
        ports = free_ports(3)
        cmd = server_command(route, ident, backend, a, ports, server_exe)
        result['server'] = cmd
        with open(server_log, 'w') as log:
            server = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        wait_ready(server, server_log, SERVER_READY[route], time.monotonic() + 20)
        argv = [gdb, '-nx', '-batch', a.elf]
        for c in gdb_commands(route, ports[0], a.expr, dumps, not halted, len(before['cores']) > 1):
            argv += ['-ex', c]
        with open(gdb_log, 'w') as out, open(gdb_err, 'w') as err:
            gdb_proc = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
            try:
                gdb_proc.wait(a.timeout)
            except subprocess.TimeoutExpired:
                errors.append(f'gdb did not finish within {a.timeout:.0f} s; see {gdb_log}')
    except (ToolError, OSError, Interrupted) as e:
        if before is None:
            armed[0] = False
            left = start_anchor_recovery(route, backend, e) if touched else '; the target was not touched'
            print(json.dumps({**result, 'error': f'pre-attach anchor: {e}{left}'}))
            return 3
        errors.append(str(e))
    finally:
        armed[0] = False
        stop(gdb_proc, 0)
        stop(server, 3 if route == 'jlink' else 0)   # JLinkGDBServer -singlerun exits by itself; openocd never
    # the cleanup policy comes from the anchor alone, whatever point a signal interrupted
    resume = not any(c['halted'] for c in before['cores'].values())
    multicore = len(before['cores']) > 1
    for path in (gdb_log, gdb_err):
        if os.path.exists(path):
            with open(path, errors='replace') as f:
                if path == gdb_log:
                    text = f.read()
                else:
                    errtext = f.read()
    marks = dict(_MARK_RE.findall(text))
    for key in ('DHCSR', 'PC', 'ICSR'):
        if key in marks and re.fullmatch(r'0x[0-9a-f]{8}', marks[key].strip()):
            result[{'DHCSR': 'attachDhcsr', 'PC': 'pc', 'ICSR': 'icsr'}[key]] = marks[key].strip()
    result['symbol'] = block(text, 'SYMBOL')
    bt = block(text, 'BT')
    result['backtrace'] = None if not bt else bt.splitlines()
    if multicore:
        threads = block(text, 'THREADS')
        result['threads'] = None if threads is None else threads.splitlines()
    exprs_failed = False
    result['exprs'] = []
    for i, e in enumerate(a.expr):
        out = block(text, f'EXPR{i}')
        if out is None:
            result['exprs'].append({'expr': e, 'error': 'not reached'})
        elif out.strip():
            result['exprs'].append({'expr': e, 'output': out})
        else:
            result['exprs'].append({'expr': e, 'error': 'no value; its error is in gdbStderr'})
        exprs_failed |= 'error' in result['exprs'][-1]
    gdb_stderr = [ln for ln in errtext.splitlines() if ln.strip()]
    if gdb_stderr:
        result['gdbStderr'] = gdb_stderr
    if dumps:
        result['dumps'] = [probe_state.save_record(*d) for d in dumps]
        exprs_failed |= not all(d['complete'] for d in result['dumps'])
    missing = [k for k, f in (('DHCSR', 'attachDhcsr'), ('PC', 'pc'), ('ICSR', 'icsr'), ('backtrace', 'backtrace'))
               if not result.get(f)]
    if gdb_proc is not None and gdb_proc.returncode is not None and not errors and (gdb_proc.returncode or missing):
        errors.append(f'gdb session incomplete (exit {gdb_proc.returncode}'
                      f"{', no ' + ', '.join(missing) if missing else ''}); see {gdb_log} and {gdb_err}")
    result['resumed'] = resume and 'attachDhcsr' in result
    after = None
    try:
        after = anchor(backend, 30, keep_halted=not resume)
        if end_state_error(before, after, resume):
            # A J-Link anchor's own close sets the state, so a second one shows what this one left.
            result['afterServer'] = after
            if resume:
                failed = restore(route, backend, 30)
                if failed:
                    errors.append(f'restoring session: {failed}')
                result['restored'] = True
            else:
                ran = [n for n, c in after['cores'].items() if not c['halted']]
                errors.append(f"{', '.join(ran)} found halted, ran after the attach (afterServer)")
            after = anchor(backend, 30, keep_halted=not resume)
    except (ToolError, Interrupted) as e:
        after = None
        errors.append(f'end anchor: {e}')
    if after is not None:
        result['after'] = after
        wrong = end_state_error(before, after, resume)
        if wrong:
            errors.append(wrong)
    if signals:
        errors.append(f"interrupted by signal {', '.join(map(str, signals))}")
    if errors:
        result['error'] = '; '.join(dict.fromkeys(errors))
        print(json.dumps(result))
        return 3
    print(json.dumps(result))
    return 1 if exprs_failed else 0


if __name__ == '__main__':
    sys.exit(main())
