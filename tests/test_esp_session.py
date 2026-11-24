import argparse
import contextlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts' / 'esp_session.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (SURVIVOR_GUARD_OFF, console_pids, cut_comm_stat, end, guard_survivors, make_stub,  # noqa: E402
                     pid_alive, proc_open, proc_stat, reap_if_ours, tmpdir)

# openocd-esp32 stand-in: --version banner; init/shutdown sessions; a server that logs its examination and
# listening lines and answers the Tcl commands esp_session sends, with core states in FAKE_STATE.
FAKE_OPENOCD = r'''#!/usr/bin/env python3
import json, os, re, socket, sys
argv = sys.argv[1:]
if argv == ['--version']:
    sys.stderr.write(os.environ.get('FAKE_BANNER', 'Open On-Chip Debugger v0.12.0-esp32-20260703 (2026-07-03-13:49)') + '\n')
    sys.exit(0)
sys.path.append(os.environ['FAKE_ESPS_DIR'])
from esp_session import start_time
start = start_time(os.getpid()) or '?'
with open(os.environ['FAKE_PIDS'], 'a') as f:
    f.write(f'{os.getpid()} {start}\n')
cs = [argv[i + 1] for i, a in enumerate(argv) if a == '-c']
# esp_session.tcl_word undone as Jim reads the word: a backslashed n is a newline, any other character itself
log = re.sub(r'\\(.)', lambda m: '\n' if m[1] == 'n' else m[1], argv[argv.index('-l') + 1], flags=re.S)
cores = os.environ.get('FAKE_CORES', 'chip.cpu0,chip.cpu1').split(',')
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({'tool': 'openocd', 'argv': argv}) + '\n')
def say(line):
    with open(log, 'a') as f:
        f.write(line + '\n')
for c in cores:
    say(f'Info : [{c}] Examination succeed')
if cs[-2:] == ['init', 'shutdown']:
    if os.environ.get('FAKE_RECOVER_HANG'):
        import time; time.sleep(30)
    say('shutdown command invoked')
    sys.exit(int(os.environ.get('FAKE_RECOVER_RC', '0')))
token = next((c.split()[2] for c in cs if c.startswith('set ESPS_SESSION ')), None)
token = os.environ.get('FAKE_OTHER_TOKEN', token)
gdb_port = next(int(c.split()[2]) for c in cs if c.startswith('gdb port '))
tcl_port = next(int(c.split()[2]) for c in cs if c.startswith('tcl port '))
state_path = os.environ['FAKE_STATE']
with open(state_path, 'w') as f:
    json.dump({c: 'running' for c in cores}, f)
gate = os.environ.get('FAKE_LISTEN_GATE')
if gate:
    import time
    deadline = time.monotonic() + 60
    while not os.path.exists(gate):
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.05)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', tcl_port)); srv.listen(4)
say(f'Info : Listening on port {gdb_port} for gdb connections')
if os.environ.get('FAKE_REMOVE_ERROR'):
    say('Error: [chip.cpu0] could not remove watchpoint')
ignore_shutdown = os.environ.get('FAKE_IGNORE_SHUTDOWN')
def commands(conn):
    buf = b''
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return
        buf += chunk
        while b'\x1a' in buf:
            cmd, buf = buf.split(b'\x1a', 1)
            yield cmd.decode()
nconn = 0
def serve():
    global nconn
    conn, _ = srv.accept()
    nconn += 1
    for cmd in commands(conn):
        with open(os.environ['FAKE_LOG'], 'a') as f:
            f.write(json.dumps({'tool': 'tcl', 'conn': nconn, 'cmd': cmd}) + '\n')
        if not answer(conn, cmd):
            return
    conn.close()
def answer(conn, cmd):
    with open(state_path) as f:
        st = json.load(f)
    reply = ''
    if 'ESPS_SESSION' in cmd:
        reply = token or ''
    elif 'curstate' in cmd:
        reply = ''.join(f'{c} {s}\n' for c, s in st.items())
    elif 'rbp all' in cmd:
        reply = os.environ.get('FAKE_RBP_REPLY', '')
    elif m := re.match(r'targets (\S+); catch \{resume\}', cmd):
        if not os.environ.get('FAKE_RESUME_FAIL'):
            st = {c: 'running' for c in st}
    elif cmd == 'shutdown':
        if ignore_shutdown and not os.path.exists(ignore_shutdown):
            open(ignore_shutdown, 'w').close()
            conn.sendall(b'\x1a'); conn.close(); return False
        say('shutdown command invoked')
        conn.close()
        sys.exit(0)
    with open(state_path, 'w') as f:
        json.dump(st, f)
    conn.sendall(reply.encode() + b'\x1a')
    return True
while True:
    serve()
'''

# gdb stand-in: the -ex commands in order; the connect halts every core and appends openocd's connect lines.
FAKE_GDB = r'''#!/usr/bin/env python3
import json, os, re, sys, time
argv = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({'tool': 'gdb', 'argv': argv}) + '\n')
exs = [argv[i + 1] for i, a in enumerate(argv) if a == '-ex']
state_path = os.environ['FAKE_STATE']
exprs = json.loads(os.environ.get('FAKE_EXPRS', '{}'))
budget = int(os.environ.get('FAKE_BUDGET', '99'))
addresses = json.loads(os.environ.get('FAKE_ADDRESSES', '{}'))
held = set()
def die():
    code = int(os.environ['FAKE_GDB_DIE'])
    sys.stdout.flush()
    if code < 0:
        os.kill(os.getpid(), -code)
    sys.exit(code)
def setall(s):
    with open(state_path) as f:
        st = json.load(f)
    with open(state_path, 'w') as f:
        json.dump({c: s for c in st}, f)
n = 0
for c in exs:
    if c.startswith('target extended-remote'):
        setall('halted')
        log = os.environ['FAKE_OCD_LOG']
        with open(log, 'a') as f:
            f.write("Info : accepting 'gdb' connection on tcp/1\nInfo : [chip.cpu0] Target halted, PC=0x1\n")
        if os.environ.get('FAKE_GDB_HANG'):
            time.sleep(60)
    elif c.startswith('echo '):
        sys.stdout.write(c[5:].replace('\\n', '\n'))
    elif c == 'info threads':
        print('* 1    Thread 1 "IDLE0" (Name: IDLE0, State: Running @CPU0) 0x1 in idle ()')
    elif c.startswith('bt '):
        print('#0  0x1 in idle ()')
    elif c.startswith('output '):
        e = c[7:]
        if e in exprs:
            sys.stdout.write(exprs[e])
        else:
            sys.stderr.write(f'No symbol "{e}" in current context.\n')
    elif c.startswith('hbreak '):
        if os.environ.get('FAKE_NO_HW_BREAK') or c[7:] == os.environ.get('FAKE_FAIL_AT'):
            print('No hardware breakpoint support in the target.')
            continue
        if c[7:] in os.environ.get('FAKE_UNRESOLVED', '').split(','):
            print(f'Function "{c[7:]}" not defined.')
            continue
        n += 1
        addr = addresses.get(c[7:], f'0x{n:x}')
        if c[7:] in os.environ.get('FAKE_MULTI', '').split(','):
            held |= {addr, addr + '+'}
            print(f'Hardware assisted breakpoint {n} at {addr}: main.c:{n}. (2 locations)')
        else:
            held.add(addr)
            print(f'Hardware assisted breakpoint {n} at {addr}: file main.c, line {n}.')
        if len(held) > budget:
            print(f'Warning:\nCannot insert hardware breakpoint {n}:Remote failure reply: 0E.')
    elif c == 'monitor resume' and not os.environ.get('FAKE_GDB_NO_RESUME'):
        setall('running')
    sys.stdout.flush()
    if c.startswith(os.environ.get('FAKE_GDB_DIE_AFTER') or '\0'):
        die()
if os.environ.get('FAKE_GDB_DIE_AFTER') == 'end':
    die()
'''

BOARD = ['--board-cfg', 'board/esp32p4-builtin.cfg', '--adapter-serial', '30:ED:A0:E0:C9:02']

def setUpModule():
    guard_survivors(__name__)


@unittest.skipIf(SURVIVOR_GUARD_OFF and os.name != 'nt', f"its fake servers are ended through the survivor guard's "
                                                         f'pidfds, off here: {SURVIVOR_GUARD_OFF}')
class EspSessionTest(unittest.TestCase):
    """On Windows the stand-ins are launchers (make_stub), and a fake server is the launcher's Python child."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        for name, body in (('openocd', FAKE_OPENOCD), ('fake-gdb', FAKE_GDB)):
            make_stub(self.d, name, body.split('\n', 1)[1])
        self.elf = self.d / 'fw.elf'
        self.elf.write_bytes(b'\x7fELF')
        self.anchor = self.d / 'console.bin'
        self.anchor.write_bytes(b'boot\n')
        self.log = self.d / 'log.json'
        self.out = self.d / 'out'
        self.env = {**os.environ, 'PATH': f'{self.d}{os.pathsep}{os.environ["PATH"]}', 'FAKE_LOG': str(self.log),
                    'FAKE_STATE': str(self.d / 'state.json'), 'FAKE_OCD_LOG': str(self.out / 'openocd.log'),
                    'FAKE_PIDS': str(self.d / 'servers'), 'FAKE_ESPS_DIR': str(SCRIPT.parent)}
        self.addCleanup(self.end_servers)
        if os.name == 'nt':    # no zombies to reap
            return
        stop = threading.Event()
        reaper = threading.Thread(target=self.reap_servers, args=(stop,), daemon=True)
        reaper.start()
        self.addCleanup(reaper.join)
        self.addCleanup(stop.set)

    def server_pids(self):
        """(pid, start time) each fake server this test started wrote at its start."""
        pids = self.d / 'servers'
        lines = pids.read_text().splitlines() if pids.exists() else []
        return [(int(pid), start) for pid, start in map(str.split, lines)]

    def reap_servers(self, stop):
        """Reap an exited server at once, as init would: as a zombie of this subreaper it would still look alive
        to esp_session's exit checks."""
        while not stop.wait(0.02):
            for pid, start in self.server_pids():
                reap_if_ours(pid, start)

    def end_servers(self):
        """End the fake servers this test started, identified by pid and a command line naming this test's
        directory, not by receipts, which tests rewrite, leave open or rename with their output directory; on Windows
        by pid and creation time, its launcher exiting with it."""
        for pid, start in self.server_pids():
            if os.name == 'nt':
                if es.start_time(pid) == start:
                    with contextlib.suppress(OSError):    # it may exit meanwhile
                        os.kill(pid, signal.SIGTERM)    # TerminateProcess
                    deadline = time.monotonic() + 10
                    while es.start_time(pid) == start and time.monotonic() < deadline:
                        time.sleep(0.02)
                continue
            if str(self.d) in ' '.join(es.cmdline(pid) or ()):
                end(pid, start)
            else:
                reap_if_ours(pid, start)

    @unittest.skipIf(os.name == 'nt', 'Windows has no zombies to reap')
    def test_a_stale_registration_never_takes_another_childs_status(self):
        child = subprocess.Popen(['sh', '-c', 'exit 7'])
        deadline = time.monotonic() + 5
        while (st := proc_stat(child.pid)) and st[1] != 'Z' and time.monotonic() < deadline:
            time.sleep(0.02)
        (self.d / 'servers').write_text(f'{child.pid} 1\n')    # its pid, an earlier process's start time
        time.sleep(0.2)    # several passes of the reaper
        self.end_servers()
        self.assertEqual(child.wait(timeout=5), 7)

    def session(self, mode, *args):
        return [sys.executable, str(SCRIPT), mode, '--out', str(self.out), *BOARD, '--rtos', 'FreeRTOS',
                '--gdb', 'fake-gdb', '--elf', str(self.elf), '--anchor', str(self.anchor), *args]

    def run_cli(self, argv, **env):
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env={**self.env, **env})
        return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr

    def calls(self, tool):
        return [json.loads(line)['argv'] for line in self.log.read_text().splitlines()
                if json.loads(line)['tool'] == tool]

    def test_inspect_records_examination_connect_and_end(self):
        rc, out, err = self.run_cli(self.session('inspect', '--expr', 'x', '--expr', 'nope'),
                                    FAKE_EXPRS=json.dumps({'x': '42'}))
        self.assertEqual(rc, 1, err)    # 'nope' has no value
        self.assertEqual(out['examination']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})
        self.assertIn('Info : [chip.cpu0] Examination succeed', out['examination']['log'])
        self.assertTrue(any('Target halted' in ln for ln in out['connect']))
        self.assertEqual(out['exprs'], [{'expr': 'x', 'output': '42'},
                                        {'expr': 'nope', 'error': 'no value; its error is in gdbStderr'}])
        self.assertEqual(out['gdbStderr'], ['No symbol "nope" in current context.'])
        self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})
        self.assertEqual(out['anchor'], {'file': str(self.anchor), 'sizeAtStart': 5, 'sizeAtEnd': 5})
        self.assertTrue(json.loads((self.out / 'receipt.json').read_text())['closed'])
        gdb = self.calls('gdb')[0]
        exs = [gdb[i + 1] for i, a in enumerate(gdb) if a == '-ex']
        for setting in ('set may-write-memory off', 'set may-call-functions off', 'set may-insert-breakpoints off'):
            self.assertLess(exs.index(setting), exs.index(next(e for e in exs if e.startswith('target'))))
        self.assertEqual(exs[-2:], ['monitor resume', 'detach'])

    def test_an_out_directory_with_a_space_reaches_openocd_as_one_word(self):
        self.out = self.d / 'my out'
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_OCD_LOG=str(self.out / 'openocd.log'))
        self.assertEqual(rc, 0, err)
        word = f'{self.out.as_posix()}/openocd.log'.replace(' ', '\\ ')
        self.assertEqual(self.calls('openocd')[0][-2:], ['-l', word])
        self.assertIn('Info : [chip.cpu0] Examination succeed', out['examination']['log'])
        self.assertIn('Info : [chip.cpu0] Examination succeed', (self.out / 'openocd.log').read_text())

    def test_configuration_order(self):
        self.run_cli(self.session('inspect'))
        argv = self.calls('openocd')[0]
        first_f = argv.index('-f')
        for c in ('set ESP_FLASH_SIZE 0', 'set ESP_RTOS FreeRTOS'):
            self.assertLess(argv.index(c), first_f)
        self.assertLess(max(i for i, a in enumerate(argv) if a.startswith(('gdb port', 'tcl port'))), first_f)
        self.assertEqual(argv[first_f:first_f + 4], ['-f', 'board/esp32p4-builtin.cfg', '-c',
                                                     'adapter serial 30:ED:A0:E0:C9:02'])
        self.log.unlink()
        argv = [sys.executable, str(SCRIPT), 'recover', '--out', str(self.d / 'rec'), '--interface-cfg',
                'interface/ftdi/x.cfg', '--usb-location', '5-1.7', '--target-cfg', 'target/esp32s2.cfg',
                '--adapter-speed', '5000']
        rc, out, err = self.run_cli(argv)
        self.assertEqual(rc, 0, err)
        argv = self.calls('openocd')[0]
        i = argv.index('-f')
        self.assertEqual(argv[i:i + 7], ['-f', 'interface/ftdi/x.cfg', '-c', 'adapter usb location 5-1.7', '-f',
                                         'target/esp32s2.cfg', '-c'])
        self.assertIn('gdb port disabled', argv)
        self.assertEqual(argv[-4:], ['-c', 'init', '-c', 'shutdown'])

    def test_budget_counts_until_the_first_refusal(self):
        rc, out, err = self.run_cli(self.session('budget', '--at', 'a', '--at', 'b', '--at', 'c'), FAKE_BUDGET='2')
        self.assertEqual(rc, 1, err)
        self.assertEqual([r['result'] for r in out['locations']], ['inserted', 'inserted', 'refused'])
        self.assertEqual(out['budget'], {'exactly': 2})
        self.assertEqual(out['end']['cores']['chip.cpu1'], 'running')
        exs = self.calls('gdb')[0]
        self.assertIn('set breakpoint always-inserted on', exs)
        self.assertEqual(exs[-6:], ['-ex', 'delete', '-ex', 'monitor resume', '-ex', 'detach'])
        self.out.rename(self.d / 'out1')
        rc, out, err = self.run_cli(self.session('budget', '--at', 'a', '--at', 'b'), FAKE_BUDGET='4')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['budget'], {'atLeast': 2})

    def test_an_unresolved_location_is_no_refusal(self):
        rc, out, err = self.run_cli(self.session('budget', '--at', 'nosuch', '--at', 'b', '--at', 'c'),
                                    FAKE_BUDGET='1', FAKE_UNRESOLVED='nosuch')
        self.assertEqual(rc, 1, err)
        self.assertEqual([r['result'] for r in out['locations']], ['unresolved', 'inserted', 'refused'])
        self.assertEqual(out['budget'], {'exactly': 1})
        self.out.rename(self.d / 'out1')
        rc, out, err = self.run_cli(self.session('budget', '--at', 'nosuch'), FAKE_UNRESOLVED='nosuch')
        self.assertEqual(rc, 1, err)
        self.assertEqual(out['budget'], {'atLeast': 0})

    def test_a_shared_address_takes_one_slot(self):
        rc, out, err = self.run_cli(self.session('budget', '--at', 'a', '--at', 'alias_of_a', '--at', 'c', '--at', 'd'),
                                    FAKE_BUDGET='2', FAKE_ADDRESSES=json.dumps({'a': '0x40', 'alias_of_a': '0x40'}))
        self.assertEqual(rc, 1, err)
        self.assertEqual([r['result'] for r in out['locations']], ['inserted', 'duplicate', 'inserted', 'refused'])
        self.assertEqual(out['locations'][1]['address'], '0x40')
        self.assertEqual(out['budget'], {'exactly': 2})
        self.out.rename(self.d / 'out1')
        rc, out, err = self.run_cli(self.session('budget', '--at', 'a', '--at', 'a'), FAKE_BUDGET='4',
                                    FAKE_ADDRESSES=json.dumps({'a': '0x40'}))
        self.assertEqual(rc, 0, err)
        self.assertEqual([r['result'] for r in out['locations']], ['inserted', 'duplicate'])
        self.assertEqual(out['budget'], {'atLeast': 1})

    def test_a_location_with_several_addresses_is_no_budget(self):
        rc, out, err = self.run_cli(self.session('budget', '--at', 'a', '--at', 'inlined'), FAKE_MULTI='inlined')
        self.assertEqual(rc, 3, err)
        self.assertIsNone(out['budget'])
        self.assertEqual(out['locations'][1]['result'], 'several addresses')
        self.assertIn('several addresses', out['error'])

    def test_a_gdb_that_fails_its_detach_or_dies_is_no_result(self):
        rc, out, err = self.run_cli(self.session('budget', '--at', 'a', '--at', 'b'),
                                    FAKE_GDB_DIE_AFTER='end', FAKE_GDB_DIE='1')
        self.assertEqual(rc, 3, err)
        self.assertEqual(out['gdbExit'], 1)
        self.assertIsNone(out['budget'])
        self.assertIn('gdb exited 1', out['error'])
        if os.name == 'nt':    # no signals to die of
            return
        self.out.rename(self.d / 'out1')
        rc, out, err = self.run_cli(self.session('inspect', '--expr', 'x'), FAKE_EXPRS=json.dumps({'x': '42'}),
                                    FAKE_GDB_DIE_AFTER='end', FAKE_GDB_DIE=str(-signal.SIGSEGV))
        self.assertEqual(rc, 3, err)
        self.assertIn(f'killed by signal {signal.SIGSEGV}', out['error'])
        self.out.rename(self.d / 'out2')
        rc, out, err = self.run_cli(self.session('inspect', '--expr', 'x'), FAKE_EXPRS=json.dumps({'x': '42'}),
                                    FAKE_GDB_DIE_AFTER='echo ESPS BT-END', FAKE_GDB_DIE=str(-signal.SIGKILL))
        self.assertEqual(rc, 3, err)
        self.assertEqual(out['exprs'], [{'expr': 'x', 'error': 'not reached'}])
        self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})

    def test_removal_errors_at_the_end_fail_the_run(self):
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_RBP_REPLY='{chip.cpu0 rbp: failed}')
        self.assertEqual(rc, 3, err)
        self.assertIn('run `recover`', out['error'])
        self.assertEqual(out['removeErrors'], ['{chip.cpu0 rbp: failed}'])

    def test_each_tcl_connection_answers_the_token_before_any_command(self):
        rc, out, err = self.run_cli(self.session('inspect'))
        self.assertEqual(rc, 0, err)
        conns = {}
        for line in self.log.read_text().splitlines():
            c = json.loads(line)
            if c['tool'] == 'tcl':
                conns.setdefault(c['conn'], []).append(c['cmd'])
        self.assertEqual(len(conns), 1, conns)
        cmds = conns[1]
        self.assertIn('ESPS_SESSION', cmds[0])
        self.assertFalse(any('ESPS_SESSION' in c for c in cmds[1:]))
        self.assertEqual(cmds[-1], 'shutdown')

    def test_an_unreadable_log_leaves_removal_unchecked(self):
        flag = self.d / 'shutdown-ignored'
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_IGNORE_SHUTDOWN=str(flag))
        self.assertEqual(rc, 3, err)
        receipt = self.out / 'receipt.json'
        rec = json.loads(receipt.read_text())
        receipt.write_text(json.dumps({**rec, 'log': str(self.d / 'gone.log')}))
        rc, out, err = self.run_cli([sys.executable, str(SCRIPT), 'leave', '--receipt', str(receipt)],
                                    FAKE_IGNORE_SHUTDOWN=str(flag))
        self.assertEqual(rc, 3, (out, err))
        self.assertIn('removal unchecked', out['removeErrors'][0])

    def test_a_gdb_failure_at_a_location_is_no_budget(self):
        rc, out, err = self.run_cli(self.session('budget', '--at', 'main', '--at', 'tud_task'),
                                    FAKE_NO_HW_BREAK='1')
        self.assertEqual(rc, 3, err)
        self.assertIsNone(out['budget'])
        self.assertEqual(out['locations'][0]['result'], 'failed')
        self.assertIn('did not ask the hardware', out['error'])

    def test_a_gdb_failure_after_a_refusal_still_fails(self):
        rc, out, err = self.run_cli(self.session('budget', '--at', 'main', '--at', 'cdc_task', '--at', 'broken'),
                                    FAKE_BUDGET='1', FAKE_FAIL_AT='broken')
        self.assertEqual(rc, 3, err)
        self.assertIsNone(out['budget'])
        self.assertEqual([r['result'] for r in out['locations']], ['inserted', 'refused', 'failed'])

    def test_a_tcl_port_with_another_session_is_never_driven(self):
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_OTHER_TOKEN='intruder')
        self.assertEqual(rc, 3)
        self.assertIn('answers for another server', out['error'])
        self.assertNotIn('end', out)
        receipt = json.loads((self.out / 'receipt.json').read_text())
        self.assertFalse(receipt['closed'])
        self.end_servers()    # the fake server, which nothing here may stop

    def test_recover_is_bounded_and_never_kills(self):
        argv = [sys.executable, str(SCRIPT), 'recover', '--out', str(self.out), *BOARD, '--timeout', '1']
        rc, out, err = self.run_cli(argv, FAKE_RECOVER_HANG='1')
        self.assertEqual(rc, 3, err)
        self.assertIn('not killed', out['error'])
        time.sleep(0.2)
        self.assertTrue(pid_alive(out['pid']))
        self.end_servers()

    def test_the_tcl_side_resumes_what_gdb_left_halted(self):
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_GDB_NO_RESUME='1')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['end']['beforeResume'], {'chip.cpu0': 'halted', 'chip.cpu1': 'halted'})
        self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})

    def test_a_hung_gdb_is_killed_and_the_session_still_ends(self):
        rc, out, err = self.run_cli(self.session('inspect', '--timeout', '1'), FAKE_GDB_HANG='1')
        self.assertEqual(rc, 3)
        self.assertIn('gdb did not finish within 1 s', out['error'])
        self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})
        self.assertTrue(json.loads((self.out / 'receipt.json').read_text())['closed'])

    def test_a_core_left_halted_fails(self):
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_GDB_NO_RESUME='1', FAKE_RESUME_FAIL='1')
        self.assertEqual(rc, 3)
        self.assertIn('left not running: chip.cpu0, chip.cpu1', out['error'])

    def test_removal_errors_point_at_recover(self):
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_REMOVE_ERROR='1')
        self.assertEqual(rc, 3)
        self.assertIn('run `recover`', out['error'])

    def test_a_server_that_stays_is_left_to_leave(self):
        flag = self.d / 'ignored-once'
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_IGNORE_SHUTDOWN=str(flag))
        self.assertEqual(rc, 3)
        self.assertIn('leave --receipt', out['error'])
        receipt = self.out / 'receipt.json'
        self.assertFalse(json.loads(receipt.read_text())['closed'])
        leave = [sys.executable, str(SCRIPT), 'leave', '--receipt', str(receipt)]
        rc, out, err = self.run_cli(leave, FAKE_IGNORE_SHUTDOWN=str(flag))
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})
        self.assertTrue(json.loads(receipt.read_text())['closed'])
        rc, out, err = self.run_cli(leave)
        self.assertEqual(rc, 2)
        self.assertIn('closed', out['error'])

    def test_leave_refuses_a_process_that_is_not_its_server(self):
        receipt = self.d / 'receipt.json'
        receipt.write_text(json.dumps({'pid': os.getpid(), 'argv': ['openocd', '-c', 'x'], 'ports': [1, 2, 3],
                                       'closed': False}))
        rc, out, err = self.run_cli([sys.executable, str(SCRIPT), 'leave', '--receipt', str(receipt)])
        self.assertEqual(rc, 2)
        self.assertIn('nothing sent', out['error'])

    @unittest.skipIf(os.name == 'nt', 'Windows sends no SIGTERM; test_ctrl_break_still_ends_the_session')
    def test_a_signal_still_ends_the_session(self):
        proc = subprocess.Popen(self.session('inspect', '--timeout', '30'), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**self.env, 'FAKE_GDB_HANG': '1'})
        self.addCleanup(proc.communicate)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (self.out / 'openocd.log').exists():
            time.sleep(0.05)
        while time.monotonic() < deadline and 'accepting' not in (self.out / 'openocd.log').read_text():
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        stdout, _ = proc.communicate(timeout=60)
        out = json.loads(stdout)
        self.assertEqual(proc.returncode, 3)
        self.assertIn('interrupted by signal 15', out['error'])
        self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})

    @unittest.skipUnless(os.name == 'nt', 'Ctrl-Break, the Windows console interrupt')
    def test_ctrl_break_still_ends_the_session(self):
        if not console_pids():
            self.skipTest('this runner has no console for esp_session to share')
        # its own group: CTRL_BREAK_EVENT goes to a group, which takes gdb along but must not reach openocd
        proc = subprocess.Popen(self.session('inspect', '--timeout', '30'), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**self.env, 'FAKE_GDB_HANG': '1'},
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self.addCleanup(proc.communicate)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (self.out / 'openocd.log').exists():
            time.sleep(0.05)
        while time.monotonic() < deadline and 'accepting' not in (self.out / 'openocd.log').read_text():
            time.sleep(0.05)
        on_console = console_pids()
        self.assertIn(proc.pid, on_console)
        server = json.loads((self.out / 'receipt.json').read_text())['pid']
        self.assertFalse({server, *(pid for pid, _ in self.server_pids())} & on_console)
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        stdout, _ = proc.communicate(timeout=60)
        out = json.loads(stdout)
        self.assertEqual(proc.returncode, 3)
        self.assertIn(f'interrupted by signal {int(signal.SIGBREAK)}', out['error'])
        self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})
        self.assertTrue(json.loads((self.out / 'receipt.json').read_text())['closed'])

    def test_a_signal_while_a_tool_starts_still_ends_the_session(self):
        # SIGTERM lands inside Popen, after the fork: the process must be recorded, stopped and the session ended
        driver = (f'import os, signal, subprocess, sys\nsys.path.insert(0, {str(SCRIPT.parent)!r})\n'
                  'import esp_session\nreal = subprocess.Popen\nwhich = sys.argv.pop(1)\n'
                  'class Popen(real):\n'
                  '    def __init__(self, args, *a, **k):\n'
                  '        super().__init__(args, *a, **k)\n'
                  '        tool = os.path.splitext(os.path.basename(args[0]))[0]\n'
                  '        if (which == "server" and "-l" in args) or (which == "gdb" and tool == "fake-gdb"):\n'
                  '            signal.raise_signal(signal.SIGTERM)\n'
                  'if which == "gdb-twice":\n'
                  '    which, base = "gdb", esp_session.Interrupted.__str__\n'
                  '    def again(self):\n'
                  '        if not getattr(again, "sent", False):\n'
                  '            again.sent = True\n'
                  '            signal.raise_signal(signal.SIGTERM)    # a second signal while the first is handled\n'
                  '        return base(self)\n'
                  '    esp_session.Interrupted.__str__ = again\n'
                  'esp_session.subprocess.Popen = Popen\nsys.exit(esp_session.main(sys.argv[1:]))')
        for which in ('server', 'gdb', 'gdb-twice'):
            with self.subTest(which=which):
                out_dir = self.d / f'out-{which}'
                argv = self.session('inspect', '--timeout', '30')
                argv[argv.index(str(self.out))] = str(out_dir)
                # the server case's openocd listens only once the gate exists: still not listening when the session ends
                gate = out_dir / 'listen'
                rc, out, err = self.run_cli([sys.executable, '-c', driver, which, *argv[2:]],
                                            FAKE_OCD_LOG=str(out_dir / 'openocd.log'),
                                            **({'FAKE_LISTEN_GATE': str(gate)} if which == 'server' else {}))
                gate.touch()
                self.assertEqual(rc, 3, err)
                self.assertIn('interrupted by signal 15', out['error'])
                receipt_file = out_dir / 'receipt.json'
                if which.startswith('gdb'):
                    self.assertIn('gdbExit', out)
                    self.assertEqual(out['end']['cores'], {'chip.cpu0': 'running', 'chip.cpu1': 'running'})
                    self.assertTrue(json.loads(receipt_file.read_text())['closed'])
                    continue
                # the server, not yet listening, is recorded and left to leave --receipt rather than lost
                self.assertIn(f'leave --receipt {receipt_file}', out['error'])
                receipt = json.loads(receipt_file.read_text())
                self.assertFalse(receipt['closed'])
                deadline = time.monotonic() + 30    # leave needs the gated server listening
                while 'Listening on port' not in Path(receipt['log']).read_text() and time.monotonic() < deadline:
                    time.sleep(0.05)
                rc, out, err = self.run_cli([sys.executable, str(SCRIPT), 'leave', '--receipt', str(receipt_file)],
                                            FAKE_OCD_LOG=str(out_dir / 'openocd.log'))
                self.assertEqual(rc, 0, err)
                self.assertTrue(json.loads(receipt_file.read_text())['closed'])

    def test_refusals_start_nothing(self):
        cases = [(self.session('inspect')[:-2] + ['--no-anchor', 'x', '--anchor', str(self.anchor)], 'or --no-anchor'),
                 (self.session('inspect')[:-2], 'or --no-anchor'),
                 (self.session('inspect', '--usb-location', '5-1'), 'exactly one selector'),
                 (self.session('budget'), 'budget takes --at'),
                 (self.session('inspect', '--at', 'x'), '--at is for budget'),
                 ([sys.executable, str(SCRIPT), 'recover', '--out', str(self.out), *BOARD, '--rtos', 'none'],
                  'recover takes the route only'),
                 ([sys.executable, str(SCRIPT), 'recover', '--out', str(self.out), '--board-cfg', 'b.cfg',
                   '--usb-location', '5-1'], 'goes with --interface-cfg'),
                 ([sys.executable, str(SCRIPT), 'recover', '--out', str(self.out), '--board-cfg', 'b.cfg; x',
                   '--adapter-serial', 's'], 'letters, digits')]
        for argv, want in cases:
            rc, out, err = self.run_cli(argv)
            self.assertEqual(rc, 2, (argv, err))
            self.assertIn(want, err)
        rc, out, err = self.run_cli(self.session('inspect'), FAKE_BANNER='Open On-Chip Debugger 0.12.0')
        self.assertEqual(rc, 3)
        self.assertIn('not openocd-esp32', out['error'])
        self.assertFalse(self.log.exists() and self.calls('gdb'))


def load_module():
    spec = importlib.util.spec_from_file_location('agentrc_esp_session', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


es = load_module()


class TclBounds(unittest.TestCase):
    """Without a deadline every socket operation gets `timeout` (as before); with one, the connect,
    token check, send and each receive share it."""

    def serve(self, replies):
        """One connection: per request, (delay before answering, chunks of the answer)."""
        srv = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(srv.close)

        def run():
            conn, _ = srv.accept()
            with conn, contextlib.suppress(OSError):    # a client past its deadline hangs up
                for delay, chunks in replies:
                    buf = b''
                    while not buf.endswith(b'\x1a'):
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                    time.sleep(delay)
                    for c in chunks:
                        conn.sendall(c)
                        time.sleep(0.05)
                conn.recv(1)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        return srv.getsockname()[1]

    def test_without_a_deadline_each_operation_gets_the_timeout(self):
        port = self.serve([(0.25, [b'tok\x1a']), (0.25, [b'he', b'll', b'o\x1a'])])
        tcl = es.Tcl(port, 'tok', 0.4)
        self.addCleanup(tcl.close)
        self.assertEqual(tcl('x'), 'hello')

    def test_a_deadline_bounds_the_token_check_and_the_answer_together(self):
        port = self.serve([(0.25, [b'tok\x1a']), (0.25, [b'hello\x1a'])])
        tcl = es.Tcl(port, 'tok', deadline=time.monotonic() + 0.4)
        with self.assertRaises(TimeoutError):
            tcl('x')
        self.assertIsNone(tcl.sock)

    def test_a_deadline_bounds_a_reply_trickling_in_fragments(self):
        port = self.serve([(0, [b'tok\x1a']), (0, [b'a'] * 20 + [b'\x1a'])])
        tcl = es.Tcl(port, 'tok', deadline=time.monotonic() + 0.5)
        with self.assertRaises(TimeoutError):
            tcl('x')

    def test_a_fragmented_answer_within_the_deadline(self):
        port = self.serve([(0, [b'to', b'k\x1a']), (0, [b'he', b'llo', b'\x1a'])])
        tcl = es.Tcl(port, 'tok', deadline=time.monotonic() + 5)
        self.addCleanup(tcl.close)
        self.assertEqual(tcl('x'), 'hello')


class Receipts(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.d = Path(td.name)

    def test_written_atomically_also_at_a_relative_path(self):
        old = os.getcwd()
        os.chdir(self.d)
        self.addCleanup(os.chdir, old)
        es.write_receipt('receipt.json', {'pid': 1})
        es.write_receipt('receipt.json', {'pid': 2, 'closed': True})
        self.assertEqual(json.loads((self.d / 'receipt.json').read_text()), {'pid': 2, 'closed': True})
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ['receipt.json'])

    def test_a_failure_before_the_replacement_keeps_the_old_receipt(self):
        path = self.d / 'receipt.json'
        es.write_receipt(str(path), {'pid': 1})
        with mock.patch.object(es.json, 'dump', side_effect=OSError('disk full')), self.assertRaises(OSError):
            es.write_receipt(str(path), {'pid': 2})
        self.assertEqual(json.loads(path.read_text()), {'pid': 1})
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ['receipt.json'])

    def test_same_process_by_argv_and_start_time_until_it_exits(self):
        # POSIX: argv's tail from /proc; Windows: the image file, a launcher there, and the creation time
        stub = make_stub(self.d, 'stand-in', 'import time; time.sleep(30)')
        proc = subprocess.Popen([str(stub)])
        self.addCleanup(proc.wait)
        self.addCleanup(es.stop, proc)
        rec = {'pid': proc.pid, 'argv': [str(stub)], 'startTime': es.start_time(proc.pid)}
        deadline = time.monotonic() + 10
        while not es.same_process(rec) and time.monotonic() < deadline:    # POSIX: /proc names the exec'd argv
            time.sleep(0.02)
        self.assertTrue(es.same_process(rec))
        self.assertFalse(es.same_process({**rec, 'startTime': '1'}))
        self.assertFalse(es.same_process({**rec, 'argv': [sys.executable]}))
        self.assertFalse(es.same_process({k: v for k, v in rec.items() if k != 'startTime'}))
        self.assertFalse(es.wait_exit(proc.pid, None, 0.2))
        es.stop(proc)
        self.assertIsNotNone(proc.poll())
        self.assertTrue(es.wait_exit(proc.pid, None, 5))
        self.assertFalse(es.same_process(rec))
        self.assertIsNone(es.start_time(proc.pid))

    @unittest.skipIf(os.name == 'nt', 'Windows reads no /proc')
    def test_a_comm_cut_mid_character_still_gives_the_start_time(self):
        with mock.patch.object(es, 'open', proc_open({'/proc/4242/stat': cut_comm_stat(4242, start=777)}), create=True):
            self.assertEqual(es.start_time(4242), '777')

    def test_same_process_refuses_without_proc(self):
        with mock.patch.object(es, 'cmdline', return_value=None):
            self.assertFalse(es.same_process({'pid': os.getpid(), 'argv': ['x'], 'startTime': '1'}))


class WindowsExit(unittest.TestCase):
    """Only Windows' word that the process exited counts as an exit; an unknown state never closes a receipt."""

    def kernel32(self, opened, wait=None):
        k = mock.Mock()
        k.OpenProcess.return_value = 1 if opened else None
        k.WaitForSingleObject.return_value = wait
        return mock.patch.object(es, '_kernel32', return_value=k)

    def test_the_wait_and_open_results(self):
        for opened, wait, error, want in ((True, 0, 0, True), (True, 0x102, 0, False), (True, 0xFFFFFFFF, 6, None),
                                          (False, None, 87, True), (False, None, 5, None)):
            with self.subTest(opened=opened, wait=wait, error=error), self.kernel32(opened, wait), \
                    mock.patch('ctypes.get_last_error', return_value=error, create=True):
                self.assertIs(es._windows_exited(4242), want)

    def test_each_caller_keeps_its_access_and_closes_the_handle(self):
        for opened, wait, error, identified in ((True, 0x102, 0, True), (True, 0, 0, False),
                                                (True, 0xFFFFFFFF, 6, False), (False, None, 87, False),
                                                (False, None, 5, False)):
            with self.subTest(opened=opened, wait=wait, error=error), self.kernel32(opened, wait) as kernel32, \
                    mock.patch('ctypes.get_last_error', return_value=error, create=True):
                k = kernel32.return_value
                es._windows_exited(4242)
                k.OpenProcess.assert_called_with(0x00100000, False, 4242)
                self.assertEqual(es._windows_process(4242) is not None, identified)
                k.OpenProcess.assert_called_with(0x1000 | 0x00100000, False, 4242)
                self.assertEqual(k.CloseHandle.call_count, 2 if opened else 0)

    def test_an_unknown_state_is_no_exit_and_leaves_the_receipt_open(self):
        d = tmpdir(self)
        receipt = d / 'receipt.json'
        rec = {'pid': 4242, 'startTime': '1', 'argv': ['openocd'], 'ports': [1, 2, 3], 'token': 't',
               'log': str(d / 'openocd.log'), 'closed': False}
        receipt.write_text(json.dumps(rec))
        (d / 'openocd.log').write_text('')
        tcl = mock.Mock(**{'return_value.end.return_value': {'removed': '', 'cores': {'cpu0': 'running'}}})
        with mock.patch.object(es.os, 'name', 'nt'), mock.patch.object(es, 'same_process', return_value=True), \
                mock.patch.object(es, 'Tcl', tcl), mock.patch.object(es, '_windows_exited', return_value=None), \
                mock.patch.object(es, 'SHUTDOWN_EXIT_WAIT', 0.3):
            self.assertFalse(es.wait_exit(4242, None, 0.3))
            result, rc = es.leave(str(receipt))
        self.assertEqual(rc, 3)
        self.assertIn('unconfirmed', result['error'])
        self.assertFalse(json.loads(receipt.read_text())['closed'])
        with mock.patch.object(es.os, 'name', 'nt'), mock.patch.object(es, '_windows_exited', return_value=True):
            self.assertTrue(es.wait_exit(4242, None, 0.3))


class WindowsPaths(unittest.TestCase):
    def test_stop_runs_taskkill_only_from_an_absolute_system_root(self):
        for root, ran in (('C:\\Windows', True), ('Windows', False), ('\\Windows', False), ('', False)):
            proc = mock.Mock(pid=4242, **{'poll.return_value': None})
            with self.subTest(root=root), mock.patch.object(es.os, 'name', 'nt'), \
                    mock.patch.dict(os.environ, {'SystemRoot': root}), mock.patch.object(es.subprocess, 'run') as run:
                es.stop(proc)
            self.assertEqual(run.called, ran)
            if ran:
                self.assertEqual(run.call_args.args[0][1:], ['/T', '/F', '/PID', '4242'])
                self.assertIn('creationflags', run.call_args.kwargs)
            proc.kill.assert_called_once_with()

    def test_the_log_reaches_openocds_tcl_line_with_forward_slashes(self):
        a = argparse.Namespace(board_cfg='board/esp32p4-builtin.cfg', adapter_serial='30:ED', adapter_speed=None,
                               rtos=None)
        with mock.patch.object(es.os, 'sep', '\\'):
            cmd = es.server_command('openocd', a, None, 'C:\\Users\\t\\out\\openocd.log')
        self.assertEqual(cmd[-2:], ['-l', 'C:/Users/t/out/openocd.log'])

    def test_the_log_is_one_literal_word_of_openocds_tcl_line(self):
        a = argparse.Namespace(board_cfg='board/esp32p4-builtin.cfg', adapter_serial='30:ED', adapter_speed=None,
                               rtos=None)
        for log, word in (('/tmp/my out/openocd.log', '/tmp/my\\ out/openocd.log'),
                          ('/tmp/$x[y]{z}"q;\\/openocd.log', '/tmp/\\$x\\[y\\]\\{z\\}\\"q\\;\\\\/openocd.log'),
                          ('/tmp/a\tb\nc/openocd.log', '/tmp/a\\\tb\\nc/openocd.log')):
            with self.subTest(log=log), mock.patch.object(es.os, 'sep', '/'):
                self.assertEqual(es.server_command('openocd', a, None, log)[-2:], ['-l', word])

    def test_a_relative_openocd_is_recorded_absolute(self):
        d = tmpdir(self)
        (d / 'tools').mkdir()
        stub = make_stub(d / 'tools', 'openocd', 'pass')
        old = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {'ESP_SESSION_OPENOCD': os.path.join('.', 'tools', stub.name)}):
            exe = es.tool('ESP_SESSION_OPENOCD', 'openocd')
        self.assertTrue(os.path.isabs(exe), exe)
        os.chdir(d / 'tools')
        self.assertTrue(os.path.samefile(exe, stub))

    @unittest.skipUnless(os.name == 'nt', 'the Windows lookup')
    def test_a_bare_tool_name_is_never_taken_from_the_working_directory(self):
        d = tmpdir(self)
        (d / 'here').mkdir()
        make_stub(d / 'here', 'openocd', 'pass')
        old = os.getcwd()
        os.chdir(d / 'here')
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(['.', 'here', str(d / 'none')])}):
            self.assertIsNone(es.which('openocd'))
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(['.', f'"{d / "here"}"'])}):
            self.assertTrue(os.path.samefile(es.which('openocd'), d / 'here' / 'openocd.exe'))


if __name__ == '__main__':
    unittest.main()
