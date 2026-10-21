import contextlib
import importlib.util
import io
import json
import os
import signal
import socket
import stat
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
from helpers import end, guard_survivors, proc_stat, reap_if_ours  # noqa: E402

# openocd-esp32 stand-in: --version banner; init/shutdown sessions; a server that logs its examination and
# listening lines and answers the Tcl commands esp_session sends, with core states in FAKE_STATE.
FAKE_OPENOCD = r'''#!/usr/bin/env python3
import json, os, re, socket, sys
argv = sys.argv[1:]
if argv == ['--version']:
    sys.stderr.write(os.environ.get('FAKE_BANNER', 'Open On-Chip Debugger v0.12.0-esp32-20260703 (2026-07-03-13:49)') + '\n')
    sys.exit(0)
try:
    start = open('/proc/self/stat').read().rsplit(')', 1)[1].split()[19]
except OSError:
    start = '?'
with open(os.environ['FAKE_PIDS'], 'a') as f:
    f.write(f'{os.getpid()} {start}\n')
cs = [argv[i + 1] for i, a in enumerate(argv) if a == '-c']
log = argv[argv.index('-l') + 1]
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


@unittest.skipIf(os.name == 'nt', 'esp_session.py refuses on Windows: WindowsRefusal')
class EspSessionTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        for name, body in (('openocd', FAKE_OPENOCD), ('fake-gdb', FAKE_GDB)):
            f = self.d / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.elf = self.d / 'fw.elf'
        self.elf.write_bytes(b'\x7fELF')
        self.anchor = self.d / 'console.bin'
        self.anchor.write_bytes(b'boot\n')
        self.log = self.d / 'log.json'
        self.out = self.d / 'out'
        self.env = {**os.environ, 'PATH': f'{self.d}{os.pathsep}{os.environ["PATH"]}', 'FAKE_LOG': str(self.log),
                    'FAKE_STATE': str(self.d / 'state.json'), 'FAKE_OCD_LOG': str(self.out / 'openocd.log'),
                    'FAKE_PIDS': str(self.d / 'servers')}
        self.addCleanup(self.end_servers)
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
        directory, not by receipts, which tests rewrite, leave open or rename with their output directory."""
        for pid, start in self.server_pids():
            if str(self.d) in ' '.join(es.cmdline(pid) or ()):
                end(pid, start)
            else:
                reap_if_ours(pid, start)

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
        os.kill(receipt['pid'], signal.SIGKILL)    # the fake server, which nothing here may stop

    def test_recover_is_bounded_and_never_kills(self):
        argv = [sys.executable, str(SCRIPT), 'recover', '--out', str(self.out), *BOARD, '--timeout', '1']
        rc, out, err = self.run_cli(argv, FAKE_RECOVER_HANG='1')
        self.assertEqual(rc, 3, err)
        self.assertIn('not killed', out['error'])
        time.sleep(0.2)
        self.assertTrue(os.path.exists(f"/proc/{out['pid']}"))
        os.kill(out['pid'], signal.SIGKILL)

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

    def test_a_signal_while_a_tool_starts_still_ends_the_session(self):
        # SIGTERM lands inside Popen, after the fork: the process must be recorded, stopped and the session ended
        driver = (f'import os, signal, subprocess, sys\nsys.path.insert(0, {str(SCRIPT.parent)!r})\n'
                  'import esp_session\nreal = subprocess.Popen\nwhich = sys.argv.pop(1)\n'
                  'class Popen(real):\n'
                  '    def __init__(self, args, *a, **k):\n'
                  '        super().__init__(args, *a, **k)\n'
                  '        if (which == "server" and "-l" in args) or (which == "gdb" and os.path.basename(args[0]) == "fake-gdb"):\n'
                  '            os.kill(os.getpid(), signal.SIGTERM)\n'
                  'if which == "gdb-twice":\n'
                  '    which, base = "gdb", esp_session.Interrupted.__str__\n'
                  '    def again(self):\n'
                  '        if not getattr(again, "sent", False):\n'
                  '            again.sent = True\n'
                  '            os.kill(os.getpid(), signal.SIGTERM)    # a second signal while the first is handled\n'
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

    @unittest.skipUnless(os.path.isdir('/proc/self'), 'the identity check reads /proc')
    def test_same_process_by_argv_tail_and_start_time(self):
        me = {'pid': os.getpid(), 'argv': es.cmdline(os.getpid())[-1:], 'startTime': es.start_time(os.getpid())}
        self.assertTrue(es.same_process(me))
        self.assertFalse(es.same_process({**me, 'startTime': '1'}))
        self.assertFalse(es.same_process({**me, 'argv': ['not-this']}))
        self.assertFalse(es.same_process({k: v for k, v in me.items() if k != 'startTime'}))

    def test_same_process_refuses_without_proc(self):
        with mock.patch.object(es, 'cmdline', return_value=None):
            self.assertFalse(es.same_process({'pid': os.getpid(), 'argv': ['x'], 'startTime': '1'}))


class WindowsRefusal(unittest.TestCase):
    def test_windows_is_refused_in_one_line_before_any_probe_session(self):
        for argv in (['recover', '--out', 'out', *BOARD], ['leave', '--receipt', 'receipt.json']):
            err = io.StringIO()
            with self.subTest(mode=argv[0]), mock.patch.object(es.os, 'name', 'nt'), \
                    mock.patch.object(es.subprocess, 'Popen', side_effect=AssertionError('launched')) as popen, \
                    mock.patch.object(es, 'leave', side_effect=AssertionError('left')), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(es.main(argv), 2)
            popen.assert_not_called()
            self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
            self.assertIn('not on Windows yet', err.getvalue())


if __name__ == '__main__':
    unittest.main()
