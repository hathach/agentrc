"""skills/hil/scripts/hil_openocd_esp.py, the openocd_esp flasher: roster admission, the verified
openocd-esp32 build and its scripts, the USJ's bus-port from a temp sysfs and udev tree, and the
order of the selectors in the server's argv. Fake tools only: no openocd or board is touched."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_openocd_esp is Linux only')

import hashlib
import importlib.util
import json
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'hil' / 'scripts'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oe = load('agentrc_hil_openocd_esp', SCRIPTS / 'hil_openocd_esp.py')
hf = load('agentrc_hil_flash', SCRIPTS / 'hil_flash.py')

SERIAL = '30:ED:A0:E0:C9:02'
BOARD = {'name': 'openocd_esp', 'uid': SERIAL, 'args': '-f board/esp32p4-builtin.cfg'}
SECURITY = {'flashEncryption': 'off', 'secureBoot': 'off', 'source': 'esptool transcript /w/security.txt'}
BANNER = f'Open On-Chip Debugger {oe.VERIFIED_OPENOCD} (2026-07-03-13:49)'


# openocd-esp32 stand-in: the banner, then a server that logs its listening line and answers the
# token query and the wrapped Tcl commands. FAKE_SCRIPT maps a command, its first word, or
# `<first word>#<n>` for the n-th such command, to "ok:<answer>", "err:<message>", "hang" (never
# answers), "die" (exits without answering) or "gate:<file>" (logs a `gated` event, then runs the
# command once <file> exists). Otherwise program_esp, flash list and flash
# read_bank work on FAKE_FLASH, a FAKE_BANK_SIZE image: program_esp erases whole sectors, writes,
# and verifies only the 4-byte-aligned prefix as verify_bank_hash does; FAKE_CORRUPT=tail corrupts
# an unaligned segment's last byte, =after_verify its first byte after the verify. FAKE_EVENTS gets
# its argv, every command and every signal; it never exits on SIGTERM/SIGINT/SIGHUP, so a test
# sees a signal that would have killed openocd.
FAKE_OPENOCD = r'''#!/usr/bin/env python3
import json, os, re, signal, socket, sys, time
argv = sys.argv[1:]
if argv == ['--version']:
    sys.stderr.write(os.environ['FAKE_BANNER'] + '\n')
    sys.exit(0)
def event(**kw):
    with open(os.environ['FAKE_EVENTS'], 'a') as f:
        f.write(json.dumps(kw) + '\n')
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, lambda n, _f: event(signal=n))
event(pid=os.getpid(), argv=argv,
      fds=[os.readlink(f'/proc/self/fd/{fd}') for fd in os.listdir('/proc/self/fd') if os.path.exists(f'/proc/self/fd/{fd}')])
cs = [argv[i + 1] for i, a in enumerate(argv) if a == '-c']
search = [argv[i + 1] for i, a in enumerate(argv) if a == '-s']
def find(name):
    # configuration.c find_file: absolute or relative to the cwd first, then every -s dir
    for path in [name] + [os.path.join(d, name) for d in search]:
        if os.path.isfile(path):
            return os.path.abspath(path)
def resolve(name, out):
    path = find(name)
    out.append(path)
    if path:
        for inner in re.findall(r'source \[find (\S+)\]', open(path).read()):
            resolve(inner, out)
resolved = []
for i, a in enumerate(argv):
    if a == '-f':
        resolve(argv[i + 1], resolved)
event(cwd=os.getcwd(), cwdEntries=sorted(os.listdir('.')), resolved=resolved)
log = argv[argv.index('-l') + 1]
def say(line):
    with open(log, 'a') as f:
        f.write(line + '\n')
if os.environ.get('FAKE_EXIT_AT_START'):
    sys.exit(int(os.environ['FAKE_EXIT_AT_START']))
token = os.environ.get('FAKE_OTHER_TOKEN') or next(c.split()[2] for c in cs if c.startswith('set ESPS_SESSION '))
port = next(int(c.split()[2]) for c in cs if c.startswith('tcl port '))
script = json.loads(os.environ.get('FAKE_SCRIPT', '{}'))
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', port)); srv.listen(4)
if os.environ.get('FAKE_NO_LISTEN'):
    time.sleep(120)
say(f'Info : Listening on port {port} for tcl connections')
sent = {}
WRAPPED = re.compile(r'^set hil_r \[catch \{(.*)\} hil_m\]; set hil_done (\d+); if \{\$hil_r\} \{set hil_m "HIL-ERR \$hil_m"\} else \{set hil_m "HIL-OK \$hil_m"\}$', re.S)
DONE_QUERY = 'if {[info exists hil_done]} {set hil_done} else {list}'
done, held = 0, []
FLASH, BANK = os.environ.get('FAKE_FLASH'), int(os.environ.get('FAKE_BANK_SIZE', '0'), 0)
def image():
    with open(FLASH, 'rb') as f:
        return bytearray(f.read())
def store(img):
    with open(FLASH, 'wb') as f:
        f.write(img)
def program(path, addr):
    with open(path, 'rb') as f:
        data = f.read()
    img, n = image(), len(data)
    end = -(-(addr + n) // 4096) * 4096
    img[addr:end] = b'\xff' * (end - addr)
    img[addr:addr + n] = data
    if os.environ.get('FAKE_CORRUPT') == 'tail' and n % 4:
        img[addr + n - 1] ^= 0xff
    store(img)
    say('** Programming Finished in 1 ms **')
    if img[addr:addr + (n & ~3)] != data[:n & ~3]:
        say('** Verify Failed **')
        return 'HIL-ERR ** Verify Failed **'
    say('** Verify OK **')
    if os.environ.get('FAKE_CORRUPT') == 'after_verify':
        img[addr] ^= 1
        store(img)
    return 'HIL-OK '
def model(cmd):
    if m := re.fullmatch(r'program_esp \{(\S+)\} (0x[0-9a-f]+) verify no_skip_loaded', cmd):
        return program(m[1], int(m[2], 16))
    if cmd == 'flash list':
        return ('HIL-OK {\n    name       {esp32p4.hp.cpu0.flash}\n    driver     esp32p4\n    base       0x00000000\n'
                f'    size       0x{BANK:x}\n    bus_width  0\n    chip_width 0\n    target     esp32p4.hp.cpu0\n}}\n'
                '{\n    name       {esp32p4.hp.cpu0.irom}\n    driver     esp32p4\n    base       0x40000000\n'
                '    size       0x0\n}\n')
    if m := re.fullmatch(r'flash read_bank 0 \{(\S+)\} (0x[0-9a-f]+) (\d+)', cmd):
        off, n = int(m[2], 16), int(m[3])
        with open(m[1], 'wb') as f:
            f.write(image()[off:off + n + int(os.environ.get('FAKE_READ_DELTA', '0'))])
        return 'HIL-OK '
    say(f'Info : {cmd} done')
    return 'HIL-OK '
def run(cmd):
    first = cmd.split()[0] if cmd.split() else ''
    sent[first] = sent.get(first, 0) + 1
    action = script.get(cmd, script.get(f'{first}#{sent[first]}', script.get(first)))
    if action is None:
        return model(cmd)
    if action == 'hang':
        time.sleep(120)
    if action == 'die':
        os._exit(3)
    if action == 'defer':
        return None
    kind, _, text = action.partition(':')
    if kind == 'slow':
        time.sleep(float(text))
        return model(cmd)
    if kind == 'gate':
        event(gated=cmd)
        while not os.path.exists(text):
            time.sleep(0.01)
        return model(cmd)
    if kind == 'err':
        say(f'Error: {text}')
        return f'HIL-ERR {text}'
    say(f'Info : {cmd} done')
    return f'HIL-OK {text}'
def reply(conn, text):
    try:
        conn.sendall(text.encode() + b'\x1a')
    except OSError:
        pass                        # the client gave up; the command has run all the same
def serve(conn):
    global done
    buf = b''
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return
        buf += chunk
        while b'\x1a' in buf:
            raw, buf = buf.split(b'\x1a', 1)
            cmd = raw.decode()
            if 'ESPS_SESSION' in cmd:
                event(tcl='token?')
                reply(conn, token)
                continue
            if cmd == DONE_QUERY:
                event(tcl='done?')
                reply(conn, str(done) if done else '')
                continue
            if cmd == 'shutdown':
                event(tcl='shutdown')
                reply(conn, '')
                if os.environ.get('FAKE_IGNORE_SHUTDOWN'):
                    continue
                conn.close()
                say('shutdown command invoked')
                sys.exit(int(os.environ.get('FAKE_SHUTDOWN_RC', '0')))
            m = WRAPPED.match(cmd)
            event(tcl=m[1] if m else cmd)
            answer = run(m[1]) if m else 'unwrapped'
            if answer is None:
                held.append(conn)   # its frame stays unread while the next connection is served
                return
            if m:
                done = int(m[2])
            reply(conn, answer)
while True:
    conn, _ = srv.accept()
    serve(conn)
'''


def executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def fake_install(root, banner=BANNER, chips=('esp32p4', 'esp32c3'), board_body=None, tool=None):
    """<root>/bin/openocd printing `banner` (or `tool`), and <root>/share/openocd/scripts as an
    install has it."""
    (root / 'bin').mkdir(parents=True, exist_ok=True)
    executable(root / 'bin' / 'openocd', tool or f'#!/bin/sh\necho "{banner}" >&2\n')
    scripts = root / 'share' / 'openocd' / 'scripts'
    for sub in ('board', 'target', 'interface'):
        (scripts / sub).mkdir(parents=True, exist_ok=True)
    (scripts / 'target' / 'esp_common.cfg').write_text('proc program_esp {filename args} {\n}\n')
    (scripts / 'interface' / 'esp_usb_jtag.cfg').write_text('adapter driver esp_usb_jtag\n')
    for chip in chips:
        (scripts / 'target' / f'{chip}.cfg').write_text('source [find target/esp_common.cfg]\n')
        (scripts / 'board' / f'{chip}-builtin.cfg').write_text(
            board_body or f'# example\n\nsource [find interface/esp_usb_jtag.cfg]\nsource [find target/{chip}.cfg]\n')
    (scripts / 'target' / 'esp32.cfg').write_text('')    # a chip without a USJ
    return root / 'bin' / 'openocd', scripts


class FakeBus:
    """A temp /sys/bus/usb/devices and /run/udev/data, swapped into the module's usb_observe."""

    def __init__(self, test):
        td = tempfile.TemporaryDirectory()
        test.addCleanup(td.cleanup)
        self.sys, self.udev = Path(td.name) / 'sys', Path(td.name) / 'udev'
        self.sys.mkdir()
        self.udev.mkdir()
        for name, path in (('SYS_USB_DEVICES', self.sys), ('UDEV_DATA', self.udev)):
            p = mock.patch.object(oe.usb_observe, name, str(path))
            p.start()
            test.addCleanup(p.stop)

    def add(self, busport, serial=SERIAL, *, vid_pid=oe.USJ_VID_PID, bus=5, dev=14, udev=True, sysfs=True):
        d = self.sys / busport
        d.mkdir()
        for name, value in (('idVendor', vid_pid[0]), ('idProduct', vid_pid[1]), ('busnum', bus), ('devnum', dev)):
            (d / name).write_text(f'{value}\n')
        if serial is not None and sysfs:
            (d / 'serial').write_text(f'{serial}\n')
        if serial is not None and udev:
            (self.udev / f'c189:{(bus - 1) * 128 + dev - 1}').write_text(f'E:ID_SERIAL_SHORT={serial}\n')
        return d


class Admit(unittest.TestCase):
    def test_a_board_cfg_or_the_usj_interface_with_a_target(self):
        self.assertEqual(oe.admit(BOARD), ('esp32p4', ['board/esp32p4-builtin.cfg']))
        pair = {**BOARD, 'args': '-f interface/esp_usb_jtag.cfg  -f target/esp32c3.cfg'}
        self.assertEqual(oe.admit(pair), ('esp32c3', ['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg']))

    def test_anything_that_selects_runs_commands_or_inits_is_refused(self):
        for args in ('', '-f board/esp32p4-builtin.cfg -c init', '-c "adapter serial X" -f board/esp32p4-builtin.cfg',
                     '-f board/esp32p4-builtin.cfg -c "adapter usb location 1-1"', '-s /tmp -f board/esp32p4-builtin.cfg',
                     '-f board/esp32p4-lpcore-builtin.cfg', '-f board/../board/esp32p4-builtin.cfg',
                     '-f board/esp32p4-builtin.cfg -f target/esp32p4.cfg', '-f board/esp32s3-ftdi.cfg',
                     '-f interface/ftdi/esp32_devkitj_v1.cfg -f target/esp32.cfg',
                     '-f target/esp32p4.cfg -f interface/esp_usb_jtag.cfg', '-f board/esp32p4-builtin.cfg -d3',
                     '-f interface/esp_usb_jtag.cfg -f target/esp32p4.cfg -f target/esp32c3.cfg'):
            with self.subTest(args=args), self.assertRaises(ValueError):
                oe.admit({**BOARD, 'args': args})

    def test_a_vid_pid_another_name_or_an_odd_uid_is_refused(self):
        for f in ({**BOARD, 'vid_pid': '0x303a 0x1001'}, {**BOARD, 'name': 'openocd'}, {**BOARD, 'uid': ''},
                  {**BOARD, 'uid': 'a b'}, {**BOARD, 'uid': 'x;shutdown'}, {**BOARD, 'uid': None}):
            with self.subTest(f=f), self.assertRaises(ValueError):
                oe.admit(f)

    def test_never_convoy_safe(self):
        self.assertFalse(hf.convoy_safe(BOARD))


class FindOpenocd(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def test_the_verified_build_and_its_own_scripts(self):
        exe, scripts = fake_install(self.root)
        got = oe.find_openocd(str(exe))
        self.assertEqual(got, {'exe': str(exe), 'banner': BANNER, 'scripts': str(scripts)})

    def test_a_relative_path_is_made_absolute(self):
        exe, _ = fake_install(self.root)
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(oe.find_openocd(os.path.join('.', os.path.relpath(exe, self.root)))['exe'], str(exe))

    def test_the_env_override_comes_before_path(self):
        exe, _ = fake_install(self.root)
        with mock.patch.dict(os.environ, {oe.OPENOCD_ENV: str(exe), 'PATH': '/nonexistent'}):
            self.assertEqual(oe.find_openocd()['exe'], str(exe))

    def test_another_build_or_upstream_is_refused(self):
        for banner in ('Open On-Chip Debugger v0.12.0-esp32-20251215 (2025-12-15-10:00)',
                       'Open On-Chip Debugger 0.12.0+dev-01850-geb6f2745b (2026-01-01-00:00)', ''):
            exe, _ = fake_install(self.root, banner=banner)
            with self.subTest(banner=banner), self.assertRaisesRegex(RuntimeError, 'not openocd-esp32'):
                oe.find_openocd(str(exe))

    def test_missing_tool_or_scripts_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            oe.find_openocd(str(self.root / 'nope'))
        exe, scripts = fake_install(self.root)
        (scripts / 'target' / 'esp_common.cfg').write_text('# no procs\n')
        with self.assertRaisesRegex(RuntimeError, 'no program_esp'):
            oe.find_openocd(str(exe))
        (scripts / 'target' / 'esp_common.cfg').unlink()
        with self.assertRaisesRegex(RuntimeError, 'no openocd-esp32 scripts'):
            oe.find_openocd(str(exe))


class CheckCfgs(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def test_the_installed_usj_route_passes(self):
        _, scripts = fake_install(self.root)
        oe.check_cfgs('esp32p4', ['board/esp32p4-builtin.cfg'], str(scripts))
        oe.check_cfgs('esp32c3', ['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg'], str(scripts))

    def test_a_chip_without_a_usj_board_cfg_is_refused(self):
        _, scripts = fake_install(self.root)
        with self.assertRaisesRegex(ValueError, 'no board/esp32-builtin.cfg'):
            oe.check_cfgs('esp32', ['interface/esp_usb_jtag.cfg', 'target/esp32.cfg'], str(scripts))

    def test_a_board_cfg_that_does_more_is_refused(self):
        _, scripts = fake_install(self.root, board_body='source [find interface/esp_usb_jtag.cfg]\n'
                                                        'adapter serial X\nsource [find target/esp32p4.cfg]\n')
        with self.assertRaisesRegex(ValueError, 'not the plain USJ board cfg'):
            oe.check_cfgs('esp32p4', ['board/esp32p4-builtin.cfg'], str(scripts))

    def test_a_missing_target_cfg_is_refused(self):
        _, scripts = fake_install(self.root)
        (scripts / 'target' / 'esp32c3.cfg').unlink()
        with self.assertRaisesRegex(ValueError, 'target/esp32c3.cfg is not in'):
            oe.check_cfgs('esp32c3', ['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg'], str(scripts))


class UsjLocation(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus(self)

    def test_the_one_match_by_serial(self):
        self.bus.add('5-3.4')
        self.bus.add('5-3.1', 'AA:BB:CC:DD:EE:FF', dev=9)
        self.bus.add('5-2', SERIAL, vid_pid=('239a', '00a5'), dev=3)     # same serial, not a USJ
        (self.bus.sys / '5-3.4:1.0').mkdir()                             # an interface
        self.assertEqual(oe.usj_location(SERIAL), '5-3.4')

    def test_udevs_record_comes_before_the_locked_serial(self):
        d = self.bus.add('5-3.4', sysfs=False)
        os.mkfifo(d / 'serial')        # a read of it would block
        self.assertEqual(oe.usj_location(SERIAL), '5-3.4')

    def test_without_a_udev_record_the_bounded_sysfs_read(self):
        self.bus.add('5-3.4', udev=False)
        self.assertEqual(oe.usj_location(SERIAL), '5-3.4')

    def test_missing_is_refused_without_a_serial_only_fallback(self):
        self.bus.add('5-3.1', 'AA:BB:CC:DD:EE:FF')
        with self.assertRaisesRegex(RuntimeError, 'no 303a:1001 with that serial'):
            oe.usj_location(SERIAL)
        with self.assertRaisesRegex(RuntimeError, 'no 303a:1001'):
            oe.usj_location(SERIAL.lower())     # openocd compares exactly

    def test_ambiguous_is_refused(self):
        self.bus.add('5-3.4')
        self.bus.add('3-1', bus=3, dev=2)
        with self.assertRaisesRegex(RuntimeError, 'ambiguous, at 3-1, 5-3.4'):
            oe.usj_location(SERIAL)

    def test_a_usj_whose_serial_cannot_be_read_is_not_ruled_out(self):
        self.bus.add('5-3.4')
        self.bus.add('5-3.1', None, dev=9)
        with self.assertRaisesRegex(RuntimeError, r'cannot rule out 5-3\.1'):
            oe.usj_location(SERIAL)

    def test_a_usj_that_disappears_mid_walk_is_refused(self):
        d = self.bus.add('5-3.4')
        (d / 'devnum').unlink()
        with self.assertRaisesRegex(RuntimeError, 'disappeared during the walk'):
            oe.usj_location(SERIAL)

    def test_a_change_between_the_two_walks_is_refused(self):
        self.bus.add('5-3.4')
        walks = iter([[('5-3.4', 5, 14)], [('5-3.4', 5, 15)]])
        with mock.patch.object(oe, '_usj_walk', lambda serial: next(walks)), \
                self.assertRaisesRegex(RuntimeError, 're-enumerated during resolution'):
            oe.usj_location(SERIAL)

    def test_a_location_openocd_would_truncate_is_refused(self):
        self.bus.add('5-1.2.3.4.5.6.17')
        with self.assertRaisesRegex(RuntimeError, 'not a location openocd can match'):
            oe.usj_location(SERIAL)


class ServerArgv(unittest.TestCase):
    TOOL = {'exe': '/opt/ocd/bin/openocd', 'scripts': '/opt/ocd/share/openocd/scripts'}

    def argv(self, cfgs):
        return oe.server_argv(self.TOOL, cfgs, '5-3.4', SERIAL, tcl_port=4444, token='t0k', log='/w/openocd.log')

    def test_location_before_serial_both_after_the_interface_and_before_init(self):
        argv = self.argv(['board/esp32p4-builtin.cfg'])
        self.assertEqual(argv, [
            '/opt/ocd/bin/openocd', '-s', '/opt/ocd/share/openocd/scripts', '-c', 'gdb port disabled',
            '-c', 'telnet port disabled', '-c', 'tcl port 4444', '-c', 'set ESPS_SESSION t0k',
            '-f', '/opt/ocd/share/openocd/scripts/board/esp32p4-builtin.cfg', '-c', 'adapter usb location 5-3.4', '-c', f'adapter serial {SERIAL}',
            '-c', 'noinit', '-l', '/w/openocd.log'])

    def test_with_a_pair_the_selectors_sit_between_interface_and_target(self):
        argv = self.argv(['interface/esp_usb_jtag.cfg', 'target/esp32c3.cfg'])
        i, scripts = argv.index, self.TOOL['scripts']
        self.assertLess(i(f'{scripts}/interface/esp_usb_jtag.cfg'), i('adapter usb location 5-3.4'))
        self.assertLess(i('adapter usb location 5-3.4'), i(f'adapter serial {SERIAL}'))
        self.assertLess(i(f'adapter serial {SERIAL}'), i(f'{scripts}/target/esp32c3.cfg'))
        self.assertLess(i(f'{scripts}/target/esp32c3.cfg'), i('noinit'))
        self.assertNotIn('init', argv)


class SessionCase(unittest.TestCase):
    """A fake openocd-esp32 install, a temp sysfs with the board's USJ, and a work directory.
    Every fake server still running after a test is SIGKILLed: these are stand-ins, never openocd."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.exe, _ = fake_install(self.root, tool=FAKE_OPENOCD)
        FakeBus(self).add('5-3.4')
        self.work = self.root / 'work'
        self.work.mkdir()
        self.events_file = self.root / 'events.jsonl'
        env = mock.patch.dict(os.environ, {'FAKE_BANNER': BANNER, 'FAKE_EVENTS': str(self.events_file)})
        env.start()
        self.addCleanup(env.stop)
        cleanup = mock.patch.object(oe, 'CLEANUP_TIMEOUT', 3)
        cleanup.start()
        self.addCleanup(cleanup.stop)
        self.addCleanup(self.reap)
        # a pending session's Popen is dropped with its server running, by design
        filters = mock.patch.object(warnings, 'filters', list(warnings.filters))
        filters.start()
        self.addCleanup(filters.stop)
        warnings.filterwarnings('ignore', r'subprocess \d+ is still running', ResourceWarning)

    def reap(self):
        receipts = [json.loads(r.read_text())['pid'] for r in self.work.glob('*/receipt.json')]
        for pid in {*self.pids(), *receipts}:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def events(self):
        if not self.events_file.exists():
            return []
        return [json.loads(line) for line in self.events_file.read_text().splitlines()]

    def pids(self):
        return [e['pid'] for e in self.events() if 'pid' in e]

    def tcl(self):
        return [e['tcl'] for e in self.events() if 'tcl' in e]

    def signals(self):
        return [e['signal'] for e in self.events() if 'signal' in e]

    @staticmethod
    def alive(pid):
        try:
            with open(f'/proc/{pid}/stat') as f:
                return f.read().rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
        except OSError:
            return False

    def run_op(self, op, *, timeout=5, script=None, **env):
        env = {**env, **({'FAKE_SCRIPT': json.dumps(script)} if script else {})}
        with mock.patch.dict(os.environ, env):
            return op(timeout=timeout)

    def session(self, work=lambda s: None, **kw):
        return self.run_op(lambda timeout: oe._run(BOARD, work, workdir=str(self.work), timeout=timeout,
                                                   openocd=str(self.exe)), **kw)

    def assertPending(self, op):
        a, = op.attempts
        self.assertEqual(a.cleanup, 'pending')
        self.assertEqual(a.receipt, op.session['receipt'])
        rec = json.loads(Path(a.receipt).read_text())
        self.assertFalse(rec['closed'])
        for _ in range(50):         # the fake records its pid once it runs
            if self.pids():
                break
            time.sleep(0.1)
        self.assertEqual(self.pids(), [rec['pid']])
        self.assertTrue(self.alive(rec['pid']), 'a pending session must be left running')
        self.assertEqual(self.signals(), [], 'never signalled')

    def assertEnded(self, op):
        a, = op.attempts
        self.assertEqual(a.cleanup, 'done')
        self.assertIsNone(a.receipt)
        self.assertTrue(json.loads(Path(op.session['receipt']).read_text())['closed'])
        self.assertEqual(self.tcl()[-2:], ['reset run', 'shutdown'])
        self.assertEqual(self.signals(), [])


class Lifecycle(SessionCase):
    def test_success_is_init_the_work_then_reset_run_and_a_confirmed_shutdown(self):
        op = self.session(lambda s: s.check('esp foo'))
        self.assertEqual(self.tcl(), ['token?', 'init', 'esp foo', 'reset run', 'shutdown'])
        self.assertEqual((op.returncode, op.attempts[0].kind, op.session['exit']), (0, 'exited', 0))
        self.assertEnded(op)
        self.assertEqual(op.attempts[0].script, 'init\nesp foo\nreset run\nshutdown\n')
        self.assertEqual([s['outcome'] for s in op.steps], ['ok', 'ok', 'ok', 'sent'])
        self.assertIn('Info : esp foo done', op.steps[1]['log'])
        argv = self.events()[0]['argv']
        self.assertIn('adapter usb location 5-3.4', argv)
        self.assertEqual(op.session['location'], '5-3.4')
        self.assertEqual(Path(op.session['dir']).parent, self.work)

    def test_a_failed_command_that_returned_gets_the_reviewed_end(self):
        op = self.session(lambda s: (s.check('bad'), s.check('never')), script={'bad': 'err:nope'})
        self.assertEqual(self.tcl(), ['token?', 'init', 'bad', 'reset run', 'shutdown'])
        self.assertEqual(op.returncode, 1)
        self.assertIn('nope', op.session['failure'])
        self.assertEnded(op)

    def test_an_init_failure_gets_no_reset_and_stays_pending(self):
        op = self.session(script={'init': 'err:esp_usb_jtag: could not find or open device!'})
        self.assertEqual(self.tcl(), ['token?', 'init'])
        self.assertEqual(op.returncode, 1)
        self.assertIn('init failed', op.session['failure'])
        self.assertPending(op)

    def test_an_empty_error_answer_reports_the_logs_errors(self):
        op = self.session(script={'init': 'err:'})
        self.assertEqual(op.session['failure'], 'init failed: Error: ')

    def test_a_command_outstanding_at_the_timeout_gets_no_cleanup(self):
        t0 = time.monotonic()
        op = self.session(lambda s: s.check('slow'), timeout=2, script={'slow': 'hang'})
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(self.tcl(), ['token?', 'init', 'slow'])
        self.assertEqual((op.returncode, op.attempts[0].kind), (124, 'timed_out'))
        self.assertEqual(op.steps[-1]['outcome'], 'outstanding')
        self.assertPending(op)

    def test_a_foreign_tcl_server_is_sent_nothing(self):
        op = self.session(FAKE_OTHER_TOKEN='someone-else')
        self.assertEqual(self.tcl(), ['token?'])
        self.assertIn('another server', op.session['failure'])
        self.assertPending(op)

    def test_a_shutdown_that_leaves_the_server_running_is_pending(self):
        with mock.patch.object(oe, 'CLEANUP_TIMEOUT', 0.5):
            op = self.session(FAKE_IGNORE_SHUTDOWN='1')
        self.assertEqual(self.tcl(), ['token?', 'init', 'reset run', 'shutdown'])
        self.assertEqual(op.returncode, 1)
        self.assertIn('still runs after shutdown', op.session['failure'])
        self.assertPending(op)

    def test_a_shutdown_exit_code_fails_an_ended_session(self):
        op = self.session(FAKE_SHUTDOWN_RC='1')
        self.assertEqual((op.returncode, op.session['exit']), (1, 1))
        self.assertEnded(op)

    def test_a_failed_reset_run_is_pending_without_shutdown(self):
        op = self.session(script={'reset run': 'err:target not examined'})
        self.assertEqual(self.tcl(), ['token?', 'init', 'reset run'])
        self.assertIn("'reset run' failed", op.session['failure'])
        self.assertPending(op)

    def test_the_deadline_passing_between_commands_still_ends_the_session(self):
        op = self.session(lambda s: (time.sleep(1.5), s.check('late')), timeout=1)
        self.assertEqual(self.tcl(), ['token?', 'init', 'reset run', 'shutdown'])
        self.assertEqual(op.returncode, 124)
        self.assertEnded(op)

    def test_a_server_that_dies_mid_command_leaves_cleanup_unknown(self):
        op = self.session(lambda s: s.check('x'), script={'x': 'die'})
        a, = op.attempts
        self.assertEqual((op.returncode, a.cleanup, a.receipt), (1, 'unknown', None))
        self.assertTrue(json.loads(Path(op.session['receipt']).read_text())['closed'])

    def test_a_server_that_exits_before_init_is_done(self):
        op = self.session(FAKE_EXIT_AT_START='2')
        self.assertEqual(self.tcl(), [])
        self.assertEqual((op.returncode, op.attempts[0].cleanup, op.session['exit']), (1, 'done', 2))

    def test_a_server_that_never_listens_is_pending(self):
        op = self.session(timeout=1.5, FAKE_NO_LISTEN='1')
        self.assertEqual((op.returncode, self.tcl()), (124, []))
        self.assertPending(op)

    def test_a_parent_interrupt_leaves_the_outstanding_command_pending(self):
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGINT))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(KeyboardInterrupt) as cm:
            self.session(lambda s: s.check('slow'), timeout=30, script={'slow': 'hang'})
        op = cm.exception.operation
        self.assertEqual((op.returncode, op.attempts[0].kind), (None, 'interrupted'))
        self.assertEqual(op.steps[-1], {**op.steps[-1], 'cmd': 'slow', 'outcome': 'interrupted'})
        self.assertEqual(self.tcl(), ['token?', 'init', 'slow'])
        self.assertPending(op)

    def test_an_interrupt_during_the_spawn_is_acted_on_once_registered(self):
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        real = subprocess.Popen

        def spawn_then_interrupt(args, *a, **kw):
            p = real(args, *a, **kw)
            if '--version' not in args:
                signal.raise_signal(signal.SIGINT)     # deferred until the receipt is written
            return p
        with mock.patch.object(oe.subprocess, 'Popen', spawn_then_interrupt), \
                self.assertRaises(KeyboardInterrupt) as cm:
            self.session()
        op = cm.exception.operation
        self.assertEqual(op.attempts[0].kind, 'interrupted')
        self.assertEqual(op.steps, ())
        self.assertPending(op)
        self.assertEqual(self.tcl(), [])

    def test_cfgs_resolve_only_to_the_verified_scripts_whatever_the_cwd(self):
        planted = self.root / 'planted'
        for cfg in ('board/esp32p4-builtin.cfg', 'target/esp32p4.cfg', 'interface/esp_usb_jtag.cfg'):
            (planted / cfg).parent.mkdir(parents=True, exist_ok=True)
            (planted / cfg).write_text('init\n')
        old = os.getcwd()
        os.chdir(planted)
        self.addCleanup(os.chdir, old)
        op = self.session()
        self.assertEqual(op.returncode, 0)
        ev, = [e for e in self.events() if 'resolved' in e]
        scripts = self.exe.parent.parent / 'share' / 'openocd' / 'scripts'
        self.assertEqual(ev['resolved'], [str(scripts / 'board' / 'esp32p4-builtin.cfg'),
                                          str(scripts / 'interface' / 'esp_usb_jtag.cfg'),
                                          str(scripts / 'target' / 'esp32p4.cfg'),
                                          str(scripts / 'target' / 'esp_common.cfg')])
        self.assertEqual(ev['cwdEntries'], [])
        self.assertTrue(ev['cwd'].startswith(op.session['dir']))

    def test_an_interrupt_while_the_record_is_built_still_carries_it(self):
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        real = oe.Session.cleanup

        def interrupted(s):
            signal.raise_signal(signal.SIGINT)
            return real(s)
        with mock.patch.object(oe.Session, 'cleanup', interrupted), self.assertRaises(KeyboardInterrupt) as cm:
            self.session(script={'init': 'err:no device'})
        op = cm.exception.operation
        self.assertIn('init failed', op.session['failure'])
        self.assertPending(op)

    def test_refusals_raise_before_anything_starts(self):
        spaced = self.root / 'a b'
        spaced.mkdir()
        with self.assertRaisesRegex(ValueError, 'workdir'):
            oe._run(BOARD, lambda s: None, workdir=str(spaced), timeout=5, openocd=str(self.exe))
        with self.assertRaisesRegex(RuntimeError, 'no 303a:1001'):
            oe._run({**BOARD, 'uid': 'AA:BB:CC:DD:EE:FF'}, lambda s: None, workdir=str(self.work), timeout=5,
                    openocd=str(self.exe))
        self.assertEqual(self.events(), [])
        self.assertEqual(list(self.work.iterdir()), [])


class TclDeadline(unittest.TestCase):
    """One absolute deadline covers the token check and the command's answer: a server slow at
    each in turn may not get the whole budget twice."""

    def test_the_token_check_and_the_answer_share_one_deadline(self):
        srv = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(srv.close)

        def slow_server():
            conn, _ = srv.accept()
            with conn:
                for reply in (b'tok', b'HIL-OK done'):
                    buf = b''
                    while not buf.endswith(b'\x1a'):
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                    time.sleep(0.25)
                    conn.sendall(reply + b'\x1a')
                conn.recv(1)
        t = threading.Thread(target=slow_server, daemon=True)
        t.start()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        s = oe.Session([], td.name, srv.getsockname()[1], 'tok')
        s.rec = {'init': None, 'submitted': 0}
        s.deadline = time.monotonic() + 0.4
        with self.assertRaises(oe._Stop) as cm:
            s.ask('x')
        self.assertTrue(cm.exception.timed_out)
        self.assertEqual(s.steps[-1]['outcome'], 'outstanding')
        s.tcl.close()


class Programming(SessionCase):
    BANK = 0x10000

    def setUp(self):
        super().setUp()
        build = self.root / 'build'
        build.mkdir()
        self.files = {}
        for name, size in (('bootloader.bin', 5000), ('partition-table.bin', 1003), ('app.bin', 4096)):
            self.files[name] = build / name
            self.files[name].write_bytes(os.urandom(size))
        self.segs = [{'offset': 0x0, 'file': str(self.files['bootloader.bin']), 'size': 5000},
                     {'offset': 0x2000, 'file': str(self.files['partition-table.bin']), 'size': 1003},
                     {'offset': 0x8000, 'file': str(self.files['app.bin']), 'size': 4096}]
        self.flash = self.root / 'flash.img'
        self.flash.write_bytes(b'\x5a' * self.BANK)

    def flash_op(self, segs=None, *, bank=None, **kw):
        env = {'FAKE_FLASH': str(self.flash), 'FAKE_BANK_SIZE': hex(self.BANK if bank is None else bank), **kw.pop('env', {})}
        security = kw.pop('security', SECURITY)
        return self.run_op(lambda timeout: oe.flash_openocd_esp(BOARD, self.segs if segs is None else segs,
                                                                 security=security, workdir=str(self.work),
                                                                 timeout=timeout, openocd=str(self.exe)),
                           **env, **kw)

    def program_cmds(self, segs=None):
        return [f"program_esp {{{s['file']}}} 0x{s['offset']:x} verify no_skip_loaded" for s in segs or self.segs]

    def read_cmds(self, op):
        return [f"flash read_bank 0 {{{r['readBack']['file']}}} 0x{r['offset']:x} {r['readBack']['length']}"
                for r in op.segments]

    def test_each_segment_programmed_in_order_then_read_back_exactly(self):
        op = self.flash_op()
        self.assertEqual(self.tcl(), ['token?', 'init', 'reset init', 'flash probe 0', 'flash list',
                                      *self.program_cmds(), *self.read_cmds(op), 'reset run', 'shutdown'])
        self.assertEqual(op.returncode, 0)
        self.assertEnded(op)
        img = self.flash.read_bytes()
        for rec, seg in zip(op.segments, self.segs):
            data = Path(seg['file']).read_bytes()
            self.assertEqual(img[seg['offset']:seg['offset'] + seg['size']], data)
            self.assertTrue(rec['program']['verified'])
            self.assertEqual((rec['readBack']['match'], rec['readBack']['firstMismatch']), (True, None))
            self.assertEqual(rec['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual([r['readBack']['length'] for r in op.segments], [5000, 1004, 4096])
        self.assertEqual([r['erase'] for r in op.segments], [[0, 0x2000], [0x2000, 0x3000], [0x8000, 0x9000]])

    def test_the_callers_security_evidence_is_recorded(self):
        op = self.flash_op()
        self.assertEqual(op.session['security'], SECURITY)

    def test_no_flash_without_security_evidence(self):
        with self.assertRaises(TypeError):
            oe.flash_openocd_esp(BOARD, self.segs, workdir=str(self.work), timeout=5, openocd=str(self.exe))
        for security in (None, {}, {**SECURITY, 'flashEncryption': 'on'}, {**SECURITY, 'secureBoot': 'on'},
                         {**SECURITY, 'secureBoot': False}, {**SECURITY, 'flashEncryption': 'unknown'},
                         {**SECURITY, 'source': ''}, {k: v for k, v in SECURITY.items() if k != 'source'},
                         {**SECURITY, 'extra': 1}, 'off'):
            with self.subTest(security=security), self.assertRaisesRegex(ValueError, 'security'):
                self.flash_op(security=security)
        self.assertEqual(self.events(), [])

    def test_a_corrupt_trailing_byte_the_verify_skips_fails_the_read_back(self):
        op = self.flash_op(env={'FAKE_CORRUPT': 'tail'})
        seg = op.segments[1]
        self.assertTrue(seg['program']['verified'])
        self.assertEqual((seg['readBack']['match'], seg['readBack']['firstMismatch']), (False, 1002))
        self.assertNotIn('readBack', op.segments[2])
        self.assertEqual(op.returncode, 1)
        self.assertIn('differs at +0x3ea', op.session['failure'])
        self.assertEnded(op)

    def test_a_read_back_mismatch_after_a_good_verify_fails(self):
        op = self.flash_op(env={'FAKE_CORRUPT': 'after_verify'})
        self.assertEqual(op.segments[0]['readBack']['firstMismatch'], 0)
        self.assertEqual(op.returncode, 1)
        self.assertEnded(op)

    def test_a_short_read_back_fails_where_it_ends(self):
        op = self.flash_op(env={'FAKE_READ_DELTA': '-1'})
        self.assertEqual(op.segments[0]['readBack'], {**op.segments[0]['readBack'], 'match': False,
                                                      'firstMismatch': 4999})
        self.assertEqual(op.returncode, 1)

    def test_a_read_back_of_the_wrong_length_fails_even_when_the_payload_matches(self):
        op = self.flash_op(env={'FAKE_READ_DELTA': '1'})
        self.assertEqual(op.segments[0]['readBack'], {**op.segments[0]['readBack'], 'match': False,
                                                      'firstMismatch': 5000})
        self.assertIn('differs at +0x1388', op.session['failure'])
        self.assertEqual(op.returncode, 1)

    def test_the_first_failed_segment_stops_programming(self):
        op = self.flash_op(script={'program_esp#2': 'err:** Verify Failed **'})
        self.assertEqual(self.tcl(), ['token?', 'init', 'reset init', 'flash probe 0', 'flash list',
                                      *self.program_cmds()[:2], 'reset run', 'shutdown'])
        self.assertEqual([r.get('program', {}).get('ok') for r in op.segments], [True, False, None])
        self.assertEqual(op.segments[1]['program']['answer'], '** Verify Failed **')
        self.assertEqual(op.returncode, 1)
        self.assertEnded(op)

    def test_a_program_call_without_verify_ok_is_a_failure(self):
        op = self.flash_op(script={'program_esp#1': 'ok:'})
        self.assertFalse(op.segments[0]['program']['verified'])
        self.assertEqual(op.returncode, 1)
        self.assertEnded(op)

    def test_a_timeout_during_programming_leaves_the_session_pending(self):
        op = self.flash_op(timeout=3, script={'program_esp#2': 'hang'})
        self.assertEqual(self.tcl()[-1], self.program_cmds()[1])
        self.assertEqual((op.returncode, op.attempts[0].kind), (124, 'timed_out'))
        self.assertNotIn('reset run', self.tcl())
        self.assertPending(op)

    def test_an_interrupt_during_programming_carries_the_segments(self):
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGINT))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(KeyboardInterrupt) as cm:
            self.flash_op(timeout=30, script={'program_esp#1': 'hang'})
        op = cm.exception.operation
        self.assertEqual(len(op.segments), 3)
        self.assertEqual(op.session['security'], SECURITY)
        self.assertPending(op)

    def test_a_segment_ending_at_the_bank_end_is_accepted(self):
        self.files['app.bin'].write_bytes(os.urandom(0x1000))
        segs = [{'offset': self.BANK - 0x1000, 'file': str(self.files['app.bin']), 'size': 0x1000}]
        op = self.flash_op(segs)
        self.assertEqual(op.returncode, 0)

    def test_a_segment_whose_erase_leaves_the_bank_is_refused_before_any_write(self):
        segs = [self.segs[0], {'offset': self.BANK - 0x1000, 'file': self.segs[1]['file'], 'size': 1003}]
        op = self.flash_op(segs, bank=self.BANK - 0x1000)
        self.assertEqual(self.tcl(), ['token?', 'init', 'reset init', 'flash probe 0', 'flash list', 'reset run',
                                      'shutdown'])
        self.assertIn('leaves bank esp32p4.hp.cpu0.flash', op.session['failure'])
        self.assertEqual(op.returncode, 1)
        self.assertEnded(op)

    def test_an_unprobed_bank_is_refused_before_any_write(self):
        op = self.flash_op(bank=0)
        self.assertNotIn(self.program_cmds()[0], self.tcl())
        self.assertIn('not a probed whole-flash bank', op.session['failure'])
        self.assertEnded(op)


class Reset(SessionCase):
    def reset(self, **kw):
        return self.run_op(lambda timeout: oe.reset_openocd_esp(BOARD, workdir=str(self.work), timeout=timeout,
                                                                 openocd=str(self.exe)), **kw)

    def test_init_then_reset_run_and_a_confirmed_shutdown(self):
        op = self.reset()
        self.assertEqual(self.tcl(), ['token?', 'init', 'reset run', 'shutdown'])
        self.assertEqual(op.returncode, 0)
        self.assertFalse(hasattr(op, 'segments'))
        self.assertEnded(op)

    def test_an_init_outstanding_at_the_timeout_is_pending(self):
        op = self.reset(timeout=2, script={'init': 'hang'})
        self.assertEqual((op.returncode, self.tcl()), (124, ['token?', 'init']))
        self.assertPending(op)


class End(SessionCase):
    """`end` from a pending session's receipt, against the fake server the session left running."""

    def pending(self, script=None, release=True, **kw):
        """A session that timed out on `work`; by default `work` is gated, and once the fake is known
        to be running it, `release` lets it finish."""
        gate = self.root / 'gate'
        script = {'work': f'gate:{gate}', **(script or {})}
        op = self.session(lambda s: s.check('work'), timeout=1, script=script, **kw)
        self.assertEqual(op.attempts[0].cleanup, 'pending')
        if script['work'].startswith('gate:'):
            self.assertEqual([e['gated'] for e in self.events() if 'gated' in e], ['work'])
            if release:
                gate.touch()
        return op.session['receipt']

    def receipt(self, path):
        return json.loads(Path(path).read_text())

    def end(self, path, timeout=10):
        return oe.end_session(path, timeout=timeout)

    def after(self, n):
        return self.tcl()[n:]

    def assertKept(self, path, rc):
        self.assertEqual(rc, 1)
        self.assertFalse(self.receipt(path)['closed'])
        pid = self.receipt(path)['pid']
        self.assertTrue(self.alive(pid))
        self.assertEqual(self.signals(), [])

    def test_the_receipt_records_init_and_every_command_submitted(self):
        op = self.session(lambda s: s.check('a'))
        rec = self.receipt(op.session['receipt'])
        self.assertEqual((rec['tool'], rec['init'], rec['submitted'], rec['cleanup']), ('hil_openocd_esp', 'ok', 3, 'done'))
        self.assertEqual(sorted(p.name for p in Path(op.session['dir']).glob('.receipt.*')), [])

    def test_a_command_that_finished_after_the_timeout_is_ended_then_idempotent(self):
        path = self.pending()
        n = len(self.tcl())
        rc, result = self.end(path)
        self.assertEqual((rc, result['outcome'], result['done']), (0, 'ended', '2'))
        self.assertEqual(self.after(n), ['token?', 'done?', 'reset run', 'shutdown'])
        rec = self.receipt(path)
        self.assertEqual((rec['closed'], rec['cleanup'], rec['submitted']), (True, 'done', 3))
        self.assertTrue(rec['end']['resetRun'].startswith('HIL-OK'))
        self.assertFalse(self.alive(rec['pid']))
        self.assertEqual(self.signals(), [])
        self.assertEqual(self.end(path)[0], 0)
        self.assertEqual(self.end(path)[1]['outcome'], 'alreadyEnded')

    def test_an_interrupt_as_the_lock_is_taken_does_not_keep_it(self):
        path = self.pending()
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        real = oe._lock

        def lock_then_interrupt(receipt):
            fd = real(receipt)
            signal.raise_signal(signal.SIGINT)      # lands between the flock and the closing try
            return fd
        with mock.patch.object(oe, '_lock', lock_then_interrupt), self.assertRaises(KeyboardInterrupt):
            self.end(path)
        rc, result = self.end(path)
        self.assertEqual((rc, result['outcome']), (0, 'ended'))

    def test_a_command_still_executing_is_never_overtaken(self):
        path = self.pending(release=False)
        n = len(self.tcl())
        rc, result = self.end(path, timeout=0.5)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(self.after(n), [])
        self.assertKept(path, rc)

    def test_a_frame_left_unread_on_the_old_connection_is_never_overtaken(self):
        path = self.pending({'work': 'defer'})
        n = len(self.tcl())
        rc, result = self.end(path)
        self.assertEqual((result['outcome'], result['done']), ('refused', '1'))
        self.assertIn('#2 is not known to have run', result['error'])
        self.assertEqual(self.after(n), ['token?', 'done?'])
        self.assertKept(path, rc)

    def test_after_a_failed_or_unproven_init_the_ordinary_end_is_refused(self):
        for script, state in (({'init': 'err:no device'}, 'failed'), ({'init': 'hang'}, 'sent')):
            with self.subTest(state=state):
                op = self.session(timeout=1.5, script=script)
                path = op.session['receipt']
                self.assertEqual(self.receipt(path)['init'], state)
                n = len(self.tcl())
                rc, result = self.end(path, timeout=2)
                self.assertEqual(result['outcome'], 'refused')
                self.assertIn('separately reviewed recovery', result['error'])
                self.assertEqual(self.after(n), [])
                self.assertKept(path, rc)
                self.reap()

    def test_a_failed_reset_run_is_never_followed_by_shutdown(self):
        path = self.pending({'reset run': 'err:Target not examined yet'})
        n = len(self.tcl())
        rc, result = self.end(path)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(self.after(n), ['token?', 'done?', 'reset run'])
        self.assertKept(path, rc)

    def test_a_server_still_running_after_shutdown_keeps_the_receipt(self):
        path = self.pending(FAKE_IGNORE_SHUTDOWN='1')
        rc, result = self.end(path, timeout=1)
        self.assertIn('exit not confirmed after shutdown', result['error'])
        self.assertKept(path, rc)

    def test_another_process_or_a_foreign_server_is_sent_nothing(self):
        path = self.pending()
        rec, n = self.receipt(path), len(self.tcl())
        oe.esp_session.write_receipt(path, {**rec, 'startTime': '1'})
        rc, result = self.end(path)
        self.assertIn('not the openocd this receipt names', result['error'])
        oe.esp_session.write_receipt(path, {**rec, 'token': 'someone-else'})
        rc, result = self.end(path)
        self.assertIn('another server', result['error'])
        self.assertEqual(self.after(n), ['token?'])
        self.assertKept(path, rc)

    def test_a_server_that_is_gone_is_refused(self):
        path = self.pending()
        pid = self.receipt(path)['pid']
        os.kill(pid, signal.SIGKILL)       # a stand-in; gone as a crashed openocd would be
        for _ in range(50):
            if not self.alive(pid):
                break
            time.sleep(0.1)
        rc, result = self.end(path)
        self.assertEqual((rc, result['outcome']), (1, 'refused'))
        self.assertFalse(self.receipt(path)['closed'])

    def test_a_receipt_closed_without_the_reviewed_end_is_refused(self):
        op = self.session(lambda s: s.check('x'), script={'x': 'die'})
        rc, result = self.end(op.session['receipt'])
        self.assertEqual((rc, result['outcome']), (1, 'refused'))
        self.assertIn("cleanup 'unknown'", result['error'])

    def test_an_interrupt_propagates_and_keeps_the_receipt(self):
        path = self.pending(release=False)
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(KeyboardInterrupt):
            self.end(path, timeout=30)
        self.assertKept(path, 1)

    def test_a_live_session_owns_its_receipt_until_it_finalizes(self):
        # end must not read `submitted`, query and act while the session can still send a command
        started, go, ops = threading.Event(), threading.Event(), []

        def work(s):
            s.check('a')
            started.set()
            go.wait(20)
            s.check('b')
        t = threading.Thread(target=lambda: ops.append(self.session(work, timeout=30)))
        t.start()
        self.addCleanup(t.join, 30)
        self.addCleanup(go.set)
        self.assertTrue(started.wait(20))
        path = next(self.work.glob('*/receipt.json'))
        n = len(self.tcl())
        rc, result = self.end(str(path), timeout=1)
        self.assertEqual((rc, result['outcome']), (1, 'refused'))
        self.assertIn('still the owner', result['error'])
        self.assertEqual(self.after(n), [])
        go.set()
        t.join(30)
        self.assertEqual(ops[0].returncode, 0)

    def test_an_end_waiting_on_a_session_that_finishes_ends_nothing(self):
        started, go, results = threading.Event(), threading.Event(), []

        def work(s):
            started.set()
            go.wait(20)
        t = threading.Thread(target=lambda: self.session(work, timeout=30))
        t.start()
        self.addCleanup(t.join, 30)
        self.addCleanup(go.set)
        self.assertTrue(started.wait(20))
        path = str(next(self.work.glob('*/receipt.json')))
        e = threading.Thread(target=lambda: results.append(self.end(path, timeout=20)))
        e.start()
        time.sleep(0.5)
        go.set()
        t.join(30)
        e.join(30)
        rc, result = results[0]
        self.assertEqual((rc, result['outcome']), (0, 'alreadyEnded'))
        self.assertEqual(self.tcl().count('reset run'), 1)

    def test_two_concurrent_ends_end_the_session_once(self):
        path = self.pending()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.end(path, timeout=20))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted((rc, r['outcome']) for rc, r in results), [(0, 'alreadyEnded'), (0, 'ended')])
        self.assertEqual(self.tcl().count('reset run'), 1)
        self.assertEqual(self.tcl().count('shutdown'), 1)

    def test_the_server_never_inherits_the_receipt_lock(self):
        op = self.session()
        fds, = [e['fds'] for e in self.events() if 'fds' in e]
        self.assertNotIn(op.session['receipt'] + '.lock', fds)

    def test_without_a_pidfd_end_is_refused(self):
        path = self.pending()
        n = len(self.tcl())
        for patch in (mock.patch.object(oe.os, 'pidfd_open', None),
                      mock.patch.object(oe.os, 'pidfd_open', side_effect=OSError(38, 'Function not implemented'))):
            with self.subTest(patch=patch), patch:
                rc, result = self.end(path)
                self.assertEqual(result['outcome'], 'refused')
                self.assertIn('pidfd', result['error'])
        self.assertEqual(self.after(n), [])
        self.assertKept(path, rc)

    def test_an_uncertain_exit_is_not_an_exit(self):
        path = self.pending(FAKE_IGNORE_SHUTDOWN='1')

        class Poller:
            def register(self, fd, events):
                self.fd = fd

            def poll(self, ms):
                return [(self.fd, oe.select.POLLNVAL)]
        with mock.patch.object(oe.select, 'poll', Poller):
            rc, result = self.end(path)
        self.assertIn('exit not confirmed', result['error'])
        self.assertKept(path, rc)

    def test_the_command_line(self):
        path = self.pending()
        cli = [sys.executable, str(SCRIPTS / 'hil_openocd_esp.py'), 'end', '--receipt', path, '--timeout', '10']
        r = subprocess.run(cli, capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, json.loads(r.stdout)['outcome']), (0, 'ended'), r.stderr)
        r = subprocess.run(cli[:-4] + ['--receipt', str(self.work / 'none.json')], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 2)


class Segments(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.f = Path(td.name) / 'app.bin'
        self.f.write_bytes(b'\x01' * 0x1001)

    def seg(self, **kw):
        return {'offset': 0x10000, 'file': str(self.f), 'size': 0x1001, **kw}

    def test_admitted_segments_keep_their_order_and_bytes(self):
        g = Path(str(self.f) + '2')
        g.write_bytes(b'\x02' * 4)
        got = oe.admit_segments([self.seg(), self.seg(offset=0x2000, file=str(g), size=4, encrypted='false')])
        self.assertEqual([(r['offset'], r['erase'], d[:1]) for r, d in got],
                         [(0x10000, [0x10000, 0x12000], b'\x01'), (0x2000, [0x2000, 0x3000], b'\x02')])

    def test_guards(self):
        for segs, why in (([], 'no segments'), (None, 'no segments'), ([self.seg(size=0)], 'positive'),
                          ([self.seg(size=0x1000)], '4097 bytes, the segment says 4096'),
                          ([self.seg(offset=0x10004)], 'multiple of 0x1000'), ([self.seg(offset=-0x1000)], 'multiple'),
                          ([self.seg(offset=True)], 'multiple'), ([self.seg(size='0x1001')], 'positive'),
                          ([self.seg(compress=True)], 'nothing but'), ([{'offset': 0, 'file': str(self.f)}], 'want'),
                          ([self.seg(encrypted=True)], 'encrypted writes'), ([self.seg(encrypted='true')], 'encrypted'),
                          ([self.seg(file=str(self.f) + 'x')], 'No such file'),
                          ([self.seg(), self.seg(offset=0x11000)], 'overlap'), ([('0', str(self.f), 1)], 'want')):
            with self.subTest(segs=segs, why=why), self.assertRaisesRegex(ValueError, why):
                oe.admit_segments(segs)

    def test_a_file_openocd_would_load_as_elf_ihex_or_srecord_is_refused(self):
        # image.c autodetect_image_type reads these by their embedded addresses, not at the offset
        for head in (b'\x7fELF\x01\x01\x01\x00\x00', b':020000040800F2\r\n', b':10010000214601360121470136007EFE09\r\n',
                     b'S00F000068656C6C6F202020202000003C\r\n', b'S1137AF00A0A0D0000000000000000000000000061\r\n'):
            self.f.write_bytes(head.ljust(0x1001, b'\x00'))
            with self.subTest(head=head), self.assertRaisesRegex(ValueError, 'would load it as'):
                oe.admit_segments([self.seg()])

    def test_raw_binaries_that_only_resemble_those_are_admitted(self):
        # an ESP image, files under 9 bytes, a record type past 05, a non-digit S type, a broken ELF magic
        for head, pad in ((b'\xe9\x03\x02\x4f', True), (b':0200000', False), (b'S113', False),
                          (b':02000006xx', True), (b'SA13000000', True), (b'\x7fEL\x00', True)):
            self.f.write_bytes(head.ljust(0x1001, b'\x00') if pad else head)
            with self.subTest(head=head):
                self.assertEqual(len(oe.admit_segments([self.seg(size=self.f.stat().st_size)])), 1)

    def test_a_path_that_would_reach_tcl_unquoted_is_refused(self):
        odd = self.f.parent / 'a b.bin'
        odd.write_bytes(b'\x00' * 4)
        with self.assertRaisesRegex(ValueError, 'reaches Tcl'):
            oe.admit_segments([{'offset': 0, 'file': str(odd), 'size': 4}])

    def test_adjacent_erase_extents_are_fine(self):
        self.assertEqual(len(oe.admit_segments([self.seg(), self.seg(offset=0x12000)])), 2)


if __name__ == '__main__':
    unittest.main()
