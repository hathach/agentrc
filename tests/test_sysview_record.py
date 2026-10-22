"""Tests for sysview_record.py against fake Xvfb, systemview and xdotool: the license dialog answered by its
mnemonic, an unknown dialog failing the run, the command port checked to be this SystemView's, exports taken only once
the window names the file this run loaded, a live recording's save, load-back and workload accounting, the user's
SEGGER config left untouched, and the usage checks. Stubs prove the plumbing only; no SystemView, no probe."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub, pid_alive  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('sysview_record', SCRIPTS / 'sysview_record.py')
record = importlib.util.module_from_spec(spec)
spec.loader.exec_module(record)

FAKE_XVFB = '''
import os, sys, time
fd = int(sys.argv[sys.argv.index('-displayfd') + 1])
open(os.environ['FAKE_STATE'] + '.xvfb', 'w').write(str(os.getpid()))
if not os.environ.get('FAKE_XVFB_SILENT'):
    os.write(fd, b'77\\n')
while True:
    time.sleep(1)
'''

# Windows live in FAKE_STATE (json {wid: title}); FAKE_DIALOG names a dialog shown at start, which the license
# mnemonic alt+c removes (any other stays); FAKE_TITLE overrides the title a load gives; FAKE_NO_EXPORT skips writing
# exports; FAKE_SLOW_EXPORT writes each export in two halves a second apart, the file held open between them;
# FAKE_DROP_LOADS and FAKE_DROP_STARTS ignore that many first loads and starts; a start opens the Recorder Configuration
# dialog; FAKE_EXIT_AFTER_START exits once recording; FAKE_START_TITLE is the title -start gives, empty for no main
# window. Every command received is appended to FAKE_STATE.log.
FAKE_SYSTEMVIEW = '''
import json, os, socket, sys, time
state, args = os.environ['FAKE_STATE'], sys.argv[1:]
open(state + '.pid', 'w').write(str(os.getpid()))
json.dump(args, open(state + '.args', 'w'))
def windows(w=None):
    if w is None:
        return json.load(open(state))
    json.dump(w, open(state, 'w'))
dialog = os.environ.get('FAKE_DIALOG')
windows({'1': 'SEGGER SystemView V4.10b - Sample_Multicore.SVDat', **({'2': dialog} if dialog else {})})
while '2' in windows():
    time.sleep(0.1)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', int(args[args.index('-port') + 1]))); srv.listen()
while True:
    c, _ = srv.accept()
    c.sendall(b'SEGGER SystemView V4.10b Command Server\\r\\n')
    cmd = c.recv(4096).decode().strip(); c.close()
    open(state + '.log', 'a').write(cmd + '\\n')
    verb, _, arg = cmd.partition(' ')
    if verb == '-quit':
        sys.exit(0)
    if verb in ('-load', '-start') and int(os.environ.get(f'FAKE_DROP_{verb[1:].upper()}S', 0)) >= sum(
            1 for line in open(state + '.log') if line.startswith(verb)):
        pass
    elif verb == '-load':
        name = os.path.basename(arg)
        windows({'1': os.environ.get('FAKE_TITLE', f'SEGGER SystemView V4.10b - {name} | Non-Commercial Use')})
    elif verb == '-start':
        title = os.environ.get('FAKE_START_TITLE',
                               'SEGGER SystemView V4.10b - *New Recording - app [FreeRTOS] on board')
        windows({'1': title, '3': 'Recorder Configuration | SEGGER SystemView V4.10b'} if title else {})
        if os.environ.get('FAKE_EXIT_AFTER_START'):
            sys.exit(0)
    elif verb == '-save':
        open(arg, 'wb').write(b'\\0' * 10 + b'recording')
    elif verb.startswith('-export') and not os.environ.get('FAKE_NO_EXPORT'):
        text = {'-export-contexts': 'Name,Activations\\n', '-export': 'sequencenum,event\\n'}.get(verb, 'terminal\\n')
        with open(arg, 'w') as f:
            if os.environ.get('FAKE_SLOW_EXPORT'):
                f.write(text[:5]); f.flush(); time.sleep(1)
                text = text[5:]
            f.write(text)
'''

# a key closes the dialogs it answers: the licence by its mnemonic, the others by their default button
FAKE_XDOTOOL = '''
import json, os, sys
state, a = os.environ['FAKE_STATE'], sys.argv[1:]
w = json.load(open(state))
closes = {'alt+c': ('License',), 'Return': ('Recorder Configuration', 'System Information', 'SystemView Error')}
if a[0] == 'search':
    print('\\n'.join(w))
elif a[0] == 'getwindowname':
    print(w.get(a[1], ''))
elif a[0] == 'key':
    w = {k: v for k, v in w.items() if not any(f in v for f in closes.get(a[-1], ()))}
    json.dump(w, open(state, 'w'))
'''

# unshare as usb_jail calls it: log the namespace script, then exec the command after it
FAKE_UNSHARE = '''
import os, sys
a = sys.argv[1:]
i = a.index('-ec')
open(os.environ['FAKE_STATE'] + '.unshare', 'w').write(a[i + 1])
os.execvp(a[i + 3], a[i + 3:])
'''


def setUpModule():
    guard_survivors(__name__)


@unittest.skipIf(not sys.platform.startswith('linux'), 'Linux-only recorder')
class Recorder(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir()
        for name, body in (('Xvfb', FAKE_XVFB), ('systemview', FAKE_SYSTEMVIEW), ('xdotool', FAKE_XDOTOOL),
                           ('unshare', FAKE_UNSHARE)):
            make_stub(bin_dir, name, body)
        self.sysfs = self.dir / 'sysfs'
        self.usb_device('1-1', '000000000123', 1, 5)
        self.home = self.dir / 'home'
        (self.home / '.config' / 'SEGGER').mkdir(parents=True)
        self.reg = self.home / '.config' / 'SEGGER' / 'SEGGER_REG_HKEY_CURRENT_USER.xml'
        self.reg.write_text('<keys/>')
        self.state = self.dir / 'state.json'
        self.env = {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', 'HOME': str(self.home),
                    'FAKE_STATE': str(self.state)}
        self.raw = self.dir / 'capture.SVDat'
        self.raw.write_bytes(b'\0' * 10)
        self.out = self.dir / 'out'
        self.tmp = self.dir / 'tmp'
        self.tmp.mkdir()
        for name, value in (('DISPLAY_S', 1), ('STARTUP_S', 5), ('SETTLE_S', 2), ('LOAD_S', 2), ('EXPORT_S', 3),
                            ('QUIT_S', 5), ('POLL_S', 0.05), ('LOAD_RETRY_S', 0.3)):
            patcher = mock.patch.object(record, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, name, value in ((record.tempfile, 'tempdir', str(self.tmp)), (record, 'USB_SYSFS', self.sysfs)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def usb_device(self, name, serial, bus, num):
        dev = self.sysfs / name
        dev.mkdir(parents=True)
        for f, v in (('serial', serial), ('busnum', bus), ('devnum', num)):
            (dev / f).write_text(f'{v}\n')

    def jail(self):
        return Path(str(self.state) + '.unshare').read_text()

    def run_main(self, *argv, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **env}), contextlib.redirect_stdout(out):
            rc = record.main(['--out', str(self.out), *argv])
        return rc, json.loads((self.out / 'record.json').read_text())

    def commands(self):
        return Path(str(self.state) + '.log').read_text().split('\n')[:-1]

    def assert_nothing_left(self):
        for suffix in ('.pid', '.xvfb'):
            path = Path(str(self.state) + suffix)
            if path.exists():
                self.assertFalse(pid_alive(int(path.read_text())), suffix)
        self.assertEqual(os.listdir(self.tmp), [])    # the temporary HOME is gone

    def loads(self):
        return [c for c in self.commands() if c.startswith('-load ')]

    def test_decode_answers_the_license_and_exports_the_loaded_file(self):
        rc, r = self.run_main('--from-raw', str(self.raw), FAKE_DIALOG='No Commercial-Use License | SystemView')
        self.assertEqual(rc, 0, r)
        self.assertEqual(set(r['exports']), {'contexts.csv', 'events.txt'})
        self.assertRegex(r['loadedTitle'], r' - sysview-[0-9a-f]{32}\.SVDat \|')
        self.assertEqual(r['systemviewVersion'], '4.10b')
        load, = self.loads()
        self.assertRegex(load, r'^-load .*/sysview-[0-9a-f]{32}\.SVDat$')
        self.assertEqual(self.commands()[1:], [f'-export-contexts {self.out / "contexts.csv"}',
                                               f'-export {self.out / "events.txt"}', '-quit'])
        self.assertEqual(r['registryCopied'], ['SEGGER_REG_HKEY_CURRENT_USER.xml'])
        self.assertEqual(os.listdir(self.home / '.config' / 'SEGGER'), ['SEGGER_REG_HKEY_CURRENT_USER.xml'])
        self.assert_nothing_left()

    def test_a_load_dropped_during_startup_is_sent_again(self):
        rc, r = self.run_main('--from-raw', str(self.raw), FAKE_DROP_LOADS='1')
        self.assertEqual(rc, 0, r)
        self.assertEqual(len(self.loads()), 2)
        self.assertEqual(len(set(self.loads())), 1)

    def test_a_raw_named_like_the_sample_is_not_mistaken_for_it(self):
        raw = self.dir / 'Sample_Multicore.SVDat'
        raw.write_bytes(b'\0' * 10)
        with mock.patch.object(record, 'LOAD_S', 0.6):
            rc, r = self.run_main('--from-raw', str(raw), FAKE_DROP_LOADS='99')
        self.assertEqual(rc, 3)
        self.assertIn('did not show Sample_Multicore.SVDat', r['error'])
        self.assertFalse((self.out / 'contexts.csv').exists())

    def test_an_export_still_open_is_waited_for(self):
        rc, r = self.run_main('--from-raw', str(self.raw), FAKE_SLOW_EXPORT='1')
        self.assertEqual(rc, 0, r)
        self.assertEqual((self.out / 'contexts.csv').read_text(), 'Name,Activations\n')

    def test_silent_xvfb_fails_and_cleans_up(self):
        rc, r = self.run_main('--from-raw', str(self.raw), FAKE_XVFB_SILENT='1')
        self.assertEqual(rc, 3)
        self.assertIn('Xvfb reported no display', r['error'])
        self.assert_nothing_left()

    def test_no_events_and_terminal_exports(self):
        rc, r = self.run_main('--from-raw', str(self.raw), '--no-events', '--export-terminal')
        self.assertEqual((rc, set(r['exports'])), (0, {'contexts.csv', 'terminal.csv'}))

    def test_unknown_dialog_fails_the_run(self):
        rc, r = self.run_main('--from-raw', str(self.raw), FAKE_DIALOG='Something Else')
        self.assertEqual(rc, 3)
        self.assertIn("unexpected SystemView dialog 'Something Else'", r['error'])
        self.assert_nothing_left()

    def test_sample_still_loaded_is_never_exported(self):
        with mock.patch.object(record, 'LOAD_S', 0.6):
            rc, r = self.run_main('--from-raw', str(self.raw),
                                  FAKE_TITLE='SEGGER SystemView V4.10b - Sample_Multicore')
        self.assertEqual(rc, 3)
        self.assertIn('did not show capture.SVDat', r['error'])
        self.assertFalse((self.out / 'contexts.csv').exists())

    def test_missing_export_fails(self):
        with mock.patch.object(record, 'EXPORT_S', 0.5):
            rc, r = self.run_main('--from-raw', str(self.raw), FAKE_NO_EXPORT='1')
        self.assertEqual(rc, 3)
        self.assertIn('-export-contexts wrote no complete contexts.csv', r['error'])
        self.assert_nothing_left()

    def test_port_held_by_another_process_fails(self):
        with socket.socket() as other:
            other.bind(('127.0.0.1', 0))
            other.listen()
            port = other.getsockname()[1]
            other.setblocking(False)
            with mock.patch.object(record, 'free_port', return_value=port):
                rc, r = self.run_main('--from-raw', str(self.raw))
            with self.assertRaises(BlockingIOError):
                other.accept()    # nothing was sent to the other listener, -quit included
        self.assertEqual(rc, 3)
        self.assertIn('not this SystemView', r['error'])
        self.assert_nothing_left()

    def live(self, *extra, **env):
        return self.run_main('--probe', '123', '--device', 'STM32H743ZI', '--interface', 'swd', '--speed', '4000',
                             '--addr', '0x24000000', '--duration-ms', '300', *extra, **env)

    def test_live_records_saves_and_exports_the_saved_recording(self):
        rc, r = self.live('--workload', 'sleep 30')
        self.assertEqual(rc, 0, r)
        saved = self.out / 'recording.SVDat'
        self.assertEqual(self.commands()[:3], ['-start', '-stop', f'-save {saved}'])
        self.assertRegex(self.commands()[3], r'^-load .*/sysview-[0-9a-f]{32}\.SVDat$')
        self.assertEqual(r['recordingTitle'], 'SEGGER SystemView V4.10b - *New Recording - app [FreeRTOS] on board')
        self.assertEqual(r['dialogsAnswered'].get('Recorder Configuration'), 1)
        self.assertEqual(r['workload']['ended'], 'deadline')
        self.assertEqual((r['controlBlock'], r['durationMs']), ('0x24000000', 300))

    def test_live_passes_the_recorder_arguments(self):
        self.live()
        args = json.loads(Path(str(self.state) + '.args').read_text())
        self.assertEqual(args[:12], ['-recorder', 'J-Link', '-device', 'STM32H743ZI', '-usb', '123', '-if', 'SWD',
                                     '-speed', '4000', '-rttcbaddr', '0x24000000'])
        self.assertEqual(args[12:14], ['-single', '-port'])

    def test_workload_failing_early_fails_the_run_after_saving(self):
        rc, r = self.live('--workload', 'exit 4')
        self.assertEqual(rc, 3)
        self.assertEqual(r['workload'], {'cmd': 'exit 4', 'exit': 4, 'ended': 'exited', 'stopped': True})
        self.assertTrue((self.out / 'recording.SVDat').exists())

    def test_a_failed_stop_still_stops_the_workload(self):
        pidfile = self.dir / 'workload.pid'
        rc, r = self.live('--workload', f'echo $$ > {pidfile}; sleep 30', FAKE_EXIT_AFTER_START='1')
        self.assertEqual(rc, 3)
        self.assertIn('nothing listens', r['error'])
        self.assertEqual(r['workload']['ended'], 'deadline')
        self.assertFalse(pid_alive(int(pidfile.read_text())))
        self.assert_nothing_left()

    def test_a_child_the_workload_leaves_behind_is_stopped(self):
        pidfile = self.dir / 'child.pid'
        rc, r = self.live('--workload', f'sleep 30 & echo $! > {pidfile}')
        self.assertEqual((rc, r['workload']['ended'], r['workload']['exit']), (0, 'exited', 0))
        self.assertFalse(pid_alive(int(pidfile.read_text())))

    def test_sigterm_stops_the_run_and_cleans_up(self):
        before = signal.getsignal(signal.SIGTERM)
        pidfile = self.dir / 'workload.pid'
        cancelled = threading.Event()

        def kill_once_the_workload_runs():    # a fixed delay can fire before a loaded host starts the workload
            deadline = time.monotonic() + 30
            while not cancelled.wait(0.05) and time.monotonic() < deadline:
                if pidfile.exists() and pidfile.read_text().strip():
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
        killer = threading.Thread(target=kill_once_the_workload_runs, daemon=True)
        killer.start()
        self.addCleanup(killer.join)
        self.addCleanup(cancelled.set)
        rc, r = self.run_main('--probe', '123', '--device', 'X', '--interface', 'swd', '--speed', '4000',
                              '--addr', '0x24000000', '--duration-ms', '10000',
                              '--workload', f'echo $$ > {pidfile}; sleep 30')
        self.assertEqual(rc, 3)
        self.assertIn('stopped by signal', r['error'])
        self.assertFalse(pid_alive(int(pidfile.read_text())))
        self.assert_nothing_left()
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_workload_finishing_cleanly_is_fine(self):
        rc, r = self.live('--workload', 'true')
        self.assertEqual((rc, r['workload']['ended'], r['workload']['exit']), (0, 'exited', 0))

    def test_an_error_dialog_is_answered_and_counted(self):
        rc, r = self.run_main('--from-raw', str(self.raw), FAKE_DIALOG='SystemView Error')
        self.assertEqual(rc, 0, r)
        self.assertEqual(r['dialogsAnswered'], {'SystemView Error': 1})
        self.assertEqual(r['systemviewErrors'], 1)

    def test_a_start_dropped_during_startup_is_sent_again(self):
        rc, r = self.live(FAKE_DROP_STARTS='1')
        self.assertEqual(rc, 0, r)
        self.assertEqual(self.commands().count('-start'), 2)

    def test_systemview_sees_only_the_recording_probe(self):
        self.usb_device('2-1', '000999', 2, 9)
        rc, r = self.live()
        self.assertEqual(rc, 0, r)
        jail = self.jail()
        self.assertIn('mount --bind /dev/bus/usb/001/005 ', jail)
        self.assertIn('mount -t tmpfs none /dev/bus/usb', jail)
        self.assertNotIn('002/009', jail)

    def test_a_decode_sees_no_usb_device(self):
        rc, r = self.run_main('--from-raw', str(self.raw))
        self.assertEqual(rc, 0, r)
        self.assertNotIn('--bind', self.jail())
        self.assertIn('mount -t tmpfs none /dev/bus/usb', self.jail())

    def test_a_probe_not_on_the_bus_or_ambiguous_fails(self):
        self.usb_device('9-9', '123', 9, 9)
        rc, r = self.live()
        self.assertEqual(rc, 3)
        self.assertIn('probe 123: 2 USB devices carry that serial', r['error'])
        shutil.rmtree(self.sysfs)
        self.sysfs.mkdir()
        shutil.rmtree(self.out)
        rc, r = self.live()
        self.assertEqual(rc, 3)
        self.assertIn('probe 123: 0 USB devices carry that serial', r['error'])

    def test_a_vanished_window_is_not_a_started_recording(self):
        with mock.patch.object(record, 'STARTUP_S', 1):
            rc, r = self.live(FAKE_START_TITLE='')
        self.assertEqual(rc, 3)
        self.assertIn('did not start', r['error'])
        self.assertNotIn('-stop', self.commands())
        self.assert_nothing_left()

    def survivors(self):
        real = record.stop_group
        return mock.patch.object(record, 'stop_group', lambda proc: real(proc) and False)

    def test_a_group_outliving_sigkill_fails_the_cleanup_after_all_of_it(self):
        with self.survivors():
            rc, r = self.run_main('--from-raw', str(self.raw))
        self.assertEqual(rc, 3)
        self.assertNotIn('error', r)
        self.assertRegex(r['cleanupError'], r'still running after SIGKILL: SystemView \(group \d+\), Xvfb')
        self.assert_nothing_left()

    def test_a_startup_failure_survives_a_failed_cleanup(self):
        with self.survivors():
            rc, r = self.run_main('--from-raw', str(self.raw), FAKE_XVFB_SILENT='1')
        self.assertEqual(rc, 3)
        self.assertIn('Xvfb reported no display', r['error'])
        self.assertIn('still running after SIGKILL: Xvfb', r['cleanupError'])
        self.assert_nothing_left()

    def test_a_workload_outliving_sigkill_fails_the_run(self):
        with self.survivors():
            rc, r = self.live('--workload', 'sleep 30')
        self.assertEqual(rc, 3)
        self.assertIn('outlived SIGKILL', r['error'])
        self.assertFalse(r['workload']['stopped'])


@unittest.skipIf(not sys.platform.startswith('linux'), 'Linux-only recorder')
class StopGroup(unittest.TestCase):
    def test_reports_a_member_that_outlives_sigkill(self):
        proc = mock.Mock(pid=4242)
        with mock.patch.object(record.os, 'killpg') as kill, \
                mock.patch.object(record, 'group_members', return_value=[4243]), \
                mock.patch.object(record.time, 'sleep'), mock.patch.object(record, 'GROUP_S', 0.01):
            self.assertFalse(record.stop_group(proc))
        self.assertEqual([c.args[1] for c in kill.call_args_list], [signal.SIGTERM, signal.SIGKILL])


@unittest.skipIf(sys.platform.startswith('linux'), 'the refusal is for other hosts')
class OffLinux(unittest.TestCase):
    def test_it_refuses_before_creating_out(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out'
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                record.main(['--out', str(out), '--from-raw', __file__])
            self.assertEqual(cm.exception.code, 2)
            self.assertIn('Linux-only', err.getvalue())
            self.assertFalse(out.exists())


@unittest.skipIf(not sys.platform.startswith('linux'), 'Linux-only recorder')
class Usage(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(record.shutil, 'which', return_value='/bin/tool')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_tools_refuse_before_creating_out(self):
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / 'c.SVDat'
            raw.write_bytes(b'x')
            out = Path(d) / 'out'
            with mock.patch.object(record.shutil, 'which', return_value=None):
                self.assertIn('not on PATH: systemview', self.usage('--out', str(out), '--from-raw', str(raw)))
            self.assertFalse(out.exists())

    def usage(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            record.main(list(argv))
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / 'c.SVDat'
            raw.write_bytes(b'x')
            full = Path(d) / 'full'
            full.mkdir()
            (full / 'f').write_text('x')
            live = ['--probe', '1', '--device', 'X', '--interface', 'swd', '--speed', '4000', '--addr', '0x1',
                    '--duration-ms', '100']
            cases = {
                'is for a live recording': ['--out', d + '/o', '--from-raw', str(raw), '--probe', '1'],
                'no such non-empty file': ['--out', d + '/o', '--from-raw', d + '/none'],
                'not an empty directory': ['--out', str(full), '--from-raw', str(raw)],
                'deletes /tmp/sv-*': ['--out', '/tmp/sv-x', '--from-raw', str(raw)],
                'needs --interface and --speed': ['--out', d + '/o', '--probe', '1', '--device', 'X', '--addr', '0x1',
                                                  '--duration-ms', '1'],
                'must be kHz': ['--out', d + '/o'] + live[:7] + ['auto'] + live[8:],
                'exactly one of --elf and --addr': ['--out', d + '/o'] + live[:8] + ['--duration-ms', '1'],
                'positive --duration-ms': ['--out', d + '/o'] + live[:10] + ['--duration-ms', '0'],
                'needs --probe and --device': ['--out', d + '/o', '--addr', '0x1'],
            }
            for want, argv in cases.items():
                with self.subTest(want):
                    self.assertIn(want, self.usage(*argv))

    def test_out_inside_a_tmp_sv_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(record, 'prepare_out', side_effect=AssertionError('the guard let it through')):
            raw = Path(d) / 'c.SVDat'
            raw.write_bytes(b'x')
            for out in ('/tmp/sv-x', '/tmp/sv-project/nested/out'):
                with self.subTest(out):
                    self.assertIn('deletes /tmp/sv-*', self.usage('--out', out, '--from-raw', str(raw)))

    def test_inputs_inside_a_tmp_sv_directory_are_refused(self):
        # SystemView would delete them at startup, before a decode reads --from-raw
        sv = Path(tempfile.mkdtemp(prefix='sv-', dir='/tmp'))
        self.addCleanup(shutil.rmtree, sv)
        raw = sv / 'c.SVDat'
        raw.write_bytes(b'x')
        live = ['--probe', '1', '--device', 'X', '--interface', 'swd', '--speed', '4000', '--duration-ms', '100']
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(record, 'resolve_live', return_value=0x1), \
                mock.patch.object(record, 'prepare_out', side_effect=AssertionError('the guard let it through')):
            cases = {'--from-raw': ['--from-raw', str(raw)],
                     '--elf': ['--elf', str(sv / 'fw.elf')] + live,
                     '--hil-config': ['--hil-config', str(sv / 'hil.json'), '--board', 'b'] + live}
            for flag, argv in cases.items():
                with self.subTest(flag):
                    self.assertIn(f'{flag} {sv}', self.usage('--out', d + '/o', *argv))

    def test_openocd_board_is_refused_for_live(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / 'hil.json'
            cfg.write_text(json.dumps({'boards': [{'name': 'b', 'flasher': {'name': 'openocd', 'uid': '1',
                                                                             'args': '-f x'}}]}))
            self.assertIn('over a J-Link only', self.usage('--out', d + '/o', '--hil-config', str(cfg), '--board', 'b',
                                                           '--interface', 'swd', '--speed', '4000', '--addr', '0x1',
                                                           '--duration-ms', '1'))


if __name__ == '__main__':
    unittest.main()
