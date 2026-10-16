import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts' / 'dual_capture.py'
_spec = importlib.util.spec_from_file_location('dual_capture', SCRIPT)
dual_capture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dual_capture)


def alive(pid):
    st = dual_capture.stat(pid)
    return bool(st) and st[0] != 'Z'


def wait_until(cond, seconds=5):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


@unittest.skipIf(not sys.platform.startswith('linux'), 'reads /proc')
class DualCaptureTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        self.out = self.d / 'out'

    def side(self, name, code):
        f = self.d / f'{name}.json'
        f.write_text(json.dumps([sys.executable, '-c', code]))
        return f'{name}={f}'

    def start(self, a, b, *args):
        return subprocess.Popen([sys.executable, str(SCRIPT), '--a', a, '--b', b, '--out', str(self.out), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def run_cli(self, a, b, *args):
        proc = self.start(a, b, *args)
        out, err = proc.communicate(timeout=60)
        return proc.returncode, json.loads(out) if out.strip() else None, err

    def test_both_sides_run_and_their_output_is_kept(self):
        rc, out, err = self.run_cli(self.side('host', 'import time; print("urb"); time.sleep(0.6)'),
                                    self.side('target', 'import sys, time; time.sleep(0.2); sys.stderr.write("log")'),
                                    '--timeout', '10')
        self.assertEqual(rc, 0, err)
        host, target = out['sides']
        self.assertEqual((self.out / 'host.out').read_text(), 'urb\n')
        self.assertEqual((self.out / 'target.err').read_text(), 'log')
        self.assertEqual([host['returncode'], target['returncode']], [0, 0])
        self.assertGreater(out['bothRunning']['seconds'], 0.1)
        self.assertLessEqual(out['bothRunning']['to'], host['exitedAt'])

    def test_the_bound_stops_a_side_and_an_escaped_descendant(self):
        pidfile = self.d / 'grandchild.pid'
        code = ('import subprocess, sys, time; '
                f'p = subprocess.Popen(["setsid", "sleep", "60"]); open({str(pidfile)!r}, "w").write(str(p.pid)); '
                'print("partial", flush=True); time.sleep(60)')
        rc, out, err = self.run_cli(self.side('host', code), self.side('target', 'print("done")'),
                                    '--timeout', '1.5', '--grace', '1')
        self.assertEqual(rc, 1, err)
        host = out['sides'][0]
        self.assertTrue(host['stopped'])
        self.assertEqual((self.out / 'host.out').read_text(), 'partial\n')
        self.assertTrue(wait_until(lambda: not alive(int(pidfile.read_text()))))
        self.assertFalse(out['sides'][1]['stopped'])

    def test_a_side_that_cannot_start_stops_the_other(self):
        bad = self.d / 'bad.json'
        bad.write_text(json.dumps(['/nonexistent/tool']))
        rc, out, err = self.run_cli(self.side('host', 'import time; time.sleep(30)'), f'target={bad}',
                                    '--timeout', '20', '--grace', '1')
        self.assertEqual(rc, 3, err)
        self.assertIn('startError', out['sides'][1])
        self.assertTrue(out['sides'][0]['stopped'])

    def test_a_failing_side_fails_the_run(self):
        rc, out, err = self.run_cli(self.side('host', 'raise SystemExit(4)'), self.side('target', 'pass'),
                                    '--timeout', '10')
        self.assertEqual(rc, 1)
        self.assertEqual(out['sides'][0]['returncode'], 4)

    def test_a_signal_stops_both(self):
        proc = self.start(self.side('host', 'import time; time.sleep(30)'),
                          self.side('target', 'import time; time.sleep(30)'), '--timeout', '30', '--grace', '1')
        time.sleep(1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        out = json.loads(out)
        self.assertEqual(proc.returncode, 1, err)
        self.assertEqual(out['interrupted'], [15])
        self.assertTrue(all(s['stopped'] for s in out['sides']))

    def test_what_outlives_its_tool_is_stopped_and_fails_the_run(self):
        # seen by its side while the tool still ran, or orphaned before that and reparented here
        for linger, owner in (('0.5', 'side'), ('0', 'orphans')):
            with self.subTest(owner):
                pidfile = self.d / f'orphan-{owner}.pid'
                code = ('import subprocess, time; '
                        f'p = subprocess.Popen(["setsid", "sleep", "60"]); open({str(pidfile)!r}, "w").write(str(p.pid)); '
                        f'time.sleep({linger})')
                self.out = self.d / f'out-{owner}'
                rc, out, err = self.run_cli(self.side('host', code), self.side('target', 'pass'),
                                            '--timeout', '10', '--grace', '1')
                pid = int(pidfile.read_text())
                self.assertEqual(rc, 1, err)
                if owner == 'side':
                    self.assertTrue(out['sides'][0]['stopped'])
                    self.assertNotIn('descendantsLeft', out['sides'][0])
                else:
                    self.assertEqual(out['orphans'], {'pids': [pid], 'left': []})
                self.assertTrue(wait_until(lambda: not alive(pid)))

    def test_a_child_started_by_a_thread_is_tracked(self):
        pidfile = self.d / 'thread-child.pid'
        code = ('import subprocess, threading, time\n'
                'def run():\n'
                f'    p = subprocess.Popen(["sleep", "60"], start_new_session=True); open({str(pidfile)!r}, "w").write(str(p.pid))\n'
                'threading.Thread(target=run).start(); time.sleep(60)')
        rc, out, err = self.run_cli(self.side('host', code), self.side('target', 'pass'),
                                    '--timeout', '1', '--grace', '1')
        self.assertEqual(rc, 1, err)
        self.assertNotIn('descendantsLeft', out['sides'][0])
        self.assertTrue(wait_until(lambda: not alive(int(pidfile.read_text()))))

    def test_a_live_tool_stops_its_own_children_and_they_get_the_grace(self):
        # the leader, like rtt.py, ends its own-session child in its own way on SIGTERM; the child takes
        # 0.5 s to finish, past the leader's exit, and must not be signalled by this script meanwhile
        log = self.d / 'child.log'
        child = (f'import signal, sys, time\nlog = open({str(log)!r}, "a", buffering=1)\n'
                 'def term(*_): log.write("TERM\\n")\n'
                 'def stop(*_): time.sleep(0.5); log.write("flushed\\n"); sys.exit(0)\n'
                 'signal.signal(signal.SIGTERM, term); signal.signal(signal.SIGINT, stop)\n'
                 'log.write("up\\n")\n'
                 'while True: time.sleep(1)')
        leader = ('import signal, subprocess, sys, time\n'
                  f'p = subprocess.Popen([sys.executable, "-c", {child!r}], start_new_session=True)\n'
                  'def term(*_): p.send_signal(signal.SIGINT); sys.exit(0)\n'
                  'signal.signal(signal.SIGTERM, term)\n'
                  'while True: time.sleep(1)')
        proc = self.start(self.side('host', leader), self.side('target', 'import time; time.sleep(60)'),
                          '--timeout', '1.5', '--grace', '3')
        out, err = proc.communicate(timeout=30)
        out = json.loads(out)
        self.assertEqual(log.read_text(), 'up\nflushed\n', err)
        host = out['sides'][0]
        self.assertFalse(host['killed'])
        self.assertNotIn('descendantsLeft', host)

    def test_each_exit_time_is_its_own(self):
        rc, out, err = self.run_cli(self.side('host', 'import time; time.sleep(60)'),
                                    self.side('target', 'import signal, time; signal.signal(signal.SIGTERM, '
                                                        'signal.SIG_IGN); time.sleep(60)'),
                                    '--timeout', '1', '--grace', '1.5')
        self.assertEqual(rc, 1, err)
        host, target = out['sides']
        self.assertFalse(host['killed'])
        self.assertTrue(target['killed'])
        self.assertLess(host['exitedAt'], 1.5)
        self.assertGreater(target['exitedAt'], 2.2)
        self.assertEqual(out['bothRunning']['to'], host['exitedAt'])

    def test_a_signal_while_a_side_starts_still_stops_it(self):
        # SIGTERM lands inside Popen, after the fork: the side must be recorded and stopped, not leaked
        pidfile = self.d / 'started.pid'
        popen = ('def popen(*a, **k):\n'
                 '    p = real(*a, **k)\n'
                 f'    open({str(pidfile)!r}, "w").write(str(p.pid))\n'
                 '    os.kill(os.getpid(), signal.SIGTERM)\n'
                 '    return p\n')
        proc = self.driver(popen, '--a', self.side('host', 'import time; time.sleep(60)'),
                           '--b', self.side('target', 'pass'), '--timeout', '30', '--grace', '1')
        self.assertEqual(proc.returncode, 1, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out['interrupted'], [15])
        self.assertTrue(out['sides'][0]['stopped'])
        self.assertTrue(wait_until(lambda: not alive(int(pidfile.read_text()))))

    def driver(self, prelude, *args):
        """Run main() in a child with subprocess.Popen replaced by the popen that prelude defines."""
        driver = (f'import os, signal, subprocess, sys, time\nsys.path.insert(0, {str(SCRIPT.parent)!r})\n'
                  'import dual_capture\nreal = subprocess.Popen\n' + prelude +
                  'dual_capture.subprocess.Popen = popen\nsys.exit(dual_capture.main(sys.argv[1:]))')
        return subprocess.run([sys.executable, '-c', driver, *args, '--out', str(self.out)],
                              capture_output=True, text=True, timeout=60)

    def test_an_orphan_first_seen_in_the_grace_gets_sigterm(self):
        # the tool forks in its SIGTERM handler and exits at once: the child reparents here unseen
        log = self.d / 'orphan.log'
        leader = ('import os, signal, sys, time\n'
                  'def term(*_):\n'
                  '    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})\n'
                  '    if os.fork():\n'
                  '        os._exit(0)\n'
                  f'    log = open({str(log)!r}, "a", buffering=1)\n'
                  '    def bye(*_): log.write("TERM\\n"); os._exit(0)\n'
                  '    signal.signal(signal.SIGTERM, bye)\n'
                  '    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n'
                  '    while True: time.sleep(1)\n'
                  'signal.signal(signal.SIGTERM, term)\n'
                  'while True: time.sleep(1)')
        rc, out, err = self.run_cli(self.side('host', leader), self.side('target', 'pass'),
                                    '--timeout', '1', '--grace', '3')
        self.assertEqual(rc, 1, err)
        self.assertEqual(log.read_text(), 'TERM\n', out)
        self.assertEqual(out['orphans']['left'], [])

    def test_no_group_sigkill_once_the_leader_is_reaped(self):
        # the leader exits leaving a group member that ignores SIGTERM; its pgid is no longer ours to signal
        kills = self.d / 'kills'
        pidfile = self.d / 'member.pid'
        member = 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'
        host = ('import subprocess, sys, time; '
                f'p = subprocess.Popen([sys.executable, "-c", {member!r}]); '
                f'open({str(pidfile)!r}, "w").write(str(p.pid)); time.sleep(0.5)')
        popen = ('real_kill = os.kill\n'
                 'def kill(pid, sig):\n'
                 f'    open({str(kills)!r}, "a").write(f"{{pid}} {{sig}}\\n")\n'
                 '    real_kill(pid, sig)\n'
                 'dual_capture.os.kill = kill\n'
                 'popen = real\n')
        proc = self.driver(popen, '--a', self.side('host', host), '--b', self.side('target', 'pass'),
                           '--timeout', '10', '--grace', '0.5')
        self.assertEqual(proc.returncode, 1, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out['sides'][0]['killed'])
        self.assertTrue(wait_until(lambda: not alive(int(pidfile.read_text()))))
        sent = [tuple(map(int, line.split())) for line in kills.read_text().split('\n') if line]
        self.assertIn(int(signal.SIGKILL), [sig for _, sig in sent])
        self.assertEqual([(pid, sig) for pid, sig in sent if pid < 0], [])

    def test_a_side_that_exits_before_the_other_starts_does_not_overlap(self):
        popen = ('calls = []\n'
                 'def popen(*a, **k):\n'
                 '    if calls: time.sleep(0.2)\n'
                 '    p = real(*a, **k)\n'
                 '    calls.append(p)\n'
                 '    if len(calls) == 1: p.wait()\n'
                 '    return p\n')
        proc = self.driver(popen, '--a', self.side('host', 'pass'), '--b', self.side('target', 'pass'),
                           '--timeout', '10')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        host, target = out['sides']
        self.assertLess(host['exitedAt'], target['spawnedAt'])
        self.assertEqual(out['bothRunning']['seconds'], 0)

    def test_an_output_file_that_cannot_open_is_a_start_error(self):
        # <name>.out is longer than a file name may be
        argv = self.d / 'long.json'
        argv.write_text(json.dumps(['true']))
        rc, out, err = self.run_cli(self.side('host', 'import time; time.sleep(30)'), f'{"b" * 252}={argv}',
                                    '--timeout', '20', '--grace', '1')
        self.assertEqual(rc, 3, err)
        self.assertIn('startError', out['sides'][1])
        self.assertTrue(out['sides'][0]['stopped'])

    def test_a_reused_pid_below_a_live_process_is_tracked_again(self):
        table = {1: ('S', 's1'), 101: ('S', 'new'), 202: ('S', 's202')}
        kids = {1: {101}, 101: {202}}
        with mock.patch.object(dual_capture, 'stat', table.get), \
                mock.patch.object(dual_capture, 'children', lambda pid: kids.get(pid, set())):
            seen = {101: 'old'}
            dual_capture.track(seen, [1])
            self.assertEqual(seen, {101: 'new', 202: 's202'})

    def test_an_orphan_on_a_reused_pid_is_adopted(self):
        table = {101: ('S', 'new'), 102: ('S', 'new2')}
        side = {'proc': mock.Mock(pid=999), 'seen': {102: 'old2'}}
        with mock.patch.object(dual_capture, 'stat', table.get), \
                mock.patch.object(dual_capture, 'children', lambda pid: {101, 102} if pid == os.getpid() else set()):
            strays = {101: 'old'}
            dual_capture.adopt([side], strays)
            self.assertEqual(strays, {101: 'new', 102: 'new2'})

    def test_a_reused_pid_is_not_walked(self):
        table = {101: ('S', 'new'), 202: ('S', 's202')}
        kids = {101: {202}}
        with mock.patch.object(dual_capture, 'stat', table.get), \
                mock.patch.object(dual_capture, 'children', lambda pid: kids.get(pid, set())):
            seen = {101: 'old'}
            dual_capture.track(seen, [])
            self.assertEqual(seen, {101: 'old'})
            self.assertEqual(dual_capture.left(seen), [])

    def test_refusals(self):
        good = self.side('host', 'pass')
        notlist = self.d / 'n.json'
        notlist.write_text('"echo hi"')
        nul = self.d / 'nul.json'
        nul.write_text(json.dumps(['echo', 'a\0b']))
        for args, want in (((good, good, '--timeout', '1'), 'different names'),
                           ((good, f'x={nul}', '--timeout', '1'), 'NUL'),
                           ((good, f'x={notlist}', '--timeout', '1'), 'JSON list of strings'),
                           ((good, 'nameonly', '--timeout', '1'), '<name>=<argv.json>'),
                           ((good, f'a/b={notlist}', '--timeout', '1'),
                            'letters, digits'),
                           ((good, self.side('target', 'pass'), '--timeout', '0'), '--timeout')):
            rc, _, err = self.run_cli(*args)
            self.assertEqual(rc, 2, (args, err))
            self.assertIn(want, err)
        self.out.mkdir()
        rc, _, err = self.run_cli(good, self.side('target', 'pass'), '--timeout', '1')
        self.assertEqual(rc, 2)
        self.assertIn('exists', err)


if __name__ == '__main__':
    unittest.main()
