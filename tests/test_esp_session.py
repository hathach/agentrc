import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts' / 'esp_session.py'

# openocd-esp32 stand-in: --version banner; init/shutdown sessions; a server that logs its examination and
# listening lines and answers the Tcl commands esp_session sends, with core states in FAKE_STATE.
FAKE_OPENOCD = r'''#!/usr/bin/env python3
import json, os, re, socket, sys
argv = sys.argv[1:]
if argv == ['--version']:
    sys.stderr.write(os.environ.get('FAKE_BANNER', 'Open On-Chip Debugger v0.12.0-esp32-20260703 (2026-07-03-13:49)') + '\n')
    sys.exit(0)
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


@unittest.skipIf(os.name == 'nt', 'openocd and gdb stubs require POSIX executable semantics')
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
                    'FAKE_STATE': str(self.d / 'state.json'), 'FAKE_OCD_LOG': str(self.out / 'openocd.log')}

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

    def test_refusals_start_nothing(self):
        cases = [(self.session('inspect')[:-2] + ['--no-anchor', 'x', '--anchor', str(self.anchor)], 'or --no-anchor'),
                 (self.session('inspect')[:-2], 'or --no-anchor'),
                 (self.session('inspect', '--usb-location', '5-1'), 'exactly one selector'),
                 (self.session('budget'), 'budget takes --at'),
                 (self.session('budget', '--at', 'main', '--at', 'main'), '--at repeated'),
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


if __name__ == '__main__':
    unittest.main()
