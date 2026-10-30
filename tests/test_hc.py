import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import hc  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub, tmpdir  # noqa: E402

LINUX = sys.platform.startswith('linux')


def setUpModule():
    guard_survivors(__name__)


# A Python `hc` answering every call with the scenario in HC_STUB: stdout, stderr (JSON when not a string), exit
# status, and a delay, overridden per call by its `by` entry for 'ls' or 'get:t5'; it logs its argv.
STUB = r'''
import json, os, sys, time
with open(os.environ['HC_STUB'], encoding='utf-8') as f:
    st = json.load(f)
argv = sys.argv[1:]
st = {**st, **st.get('by', {}).get(argv[0] + (':' + argv[argv.index('--') + 1] if '--' in argv else ''), {})}
with open(st['log'], 'a', encoding='utf-8') as f:
    f.write(json.dumps({'argv': argv, 'id': os.environ.get('HC_TERMINAL_ID'),
                        'socket': os.environ.get('HC_TERMINAL_SOCKET')}) + '\n')
time.sleep(st.get('sleep', 0))
for key, stream in (('out', sys.stdout), ('err', sys.stderr)):
    if key in st:
        stream.write(st[key] if isinstance(st[key], str) else json.dumps(st[key]))
sys.exit(st.get('rc', 0))
'''


def err(kind, message='m'):
    return {'error': {'kind': kind, 'message': message}}


class StubbedHc(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        (self.dir / 'bin').mkdir()
        make_stub(self.dir / 'bin', 'hc', STUB)
        self.state, self.log = self.dir / 'state.json', self.dir / 'log'
        env = mock.patch.dict('os.environ', {'PATH': str(self.dir / 'bin') + os.pathsep + os.environ['PATH'],
                                             'HC_STUB': str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.answer()

    def answer(self, **scenario):
        self.state.write_text(json.dumps({'log': str(self.log), **scenario}), encoding='utf-8')

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def argv(self):
        return [c['argv'] for c in self.calls()]


class RunTest(StubbedHc):
    def test_parses_the_answer_and_the_error(self):
        self.answer(out={'a': 1})
        self.assertEqual(hc.run('ls', '--json', timeout=5)[:3], (0, {'a': 1}, None))
        self.answer(rc=10, err=err('status_mismatch', 't7 is working, not idle'))
        self.assertEqual(hc.run('send', timeout=5), (10, None, 'status_mismatch', 't7 is working, not idle'))

    def test_a_malformed_error_keeps_an_unknown_kind(self):
        for stderr in ({'error': ['bad']}, {'error': 'bad'}, ['bad'], {'error': {'kind': ['not_found'], 'message': 3}}):
            with self.subTest(stderr):
                self.answer(rc=3, err=stderr)
                rc, _, kind, msg = hc.run('get', timeout=5)
                self.assertEqual((rc, kind), (3, None))
                self.assertTrue(msg)
                with self.assertRaises(hc.Error):
                    hc.get('t7', timeout=5)
                with self.assertRaises(hc.Error):
                    hc.call('ls', '--json', timeout=5)
                self.assertEqual(hc.send('t7', 'x', timeout=5)[0], hc.UNCERTAIN)

    def test_a_deadline_and_a_missing_hc_are_failures_with_their_own_kind(self):
        self.answer(sleep=10)
        self.assertEqual(hc.run('ls', timeout=0.5)[::2], (None, 'timeout'))
        with mock.patch.dict('os.environ', {'PATH': str(self.dir)}):
            self.assertEqual(hc.run('ls', timeout=5)[::2], (None, 'spawn_failed'))

    def test_call_refuses_anything_but_a_json_object(self):
        for scenario in ({'out': 'not json'}, {'out': [1]}, {'rc': 1, 'err': err('io', 'down')}, {}):
            with self.subTest(scenario):
                self.answer(**scenario)
                with self.assertRaises(hc.Error):
                    hc.call('ls', '--json', timeout=5)


class GetTest(StubbedHc):
    def test_none_only_for_exit_3_with_kind_not_found(self):
        self.answer(rc=3, err=err('not_found'))
        self.assertIsNone(hc.get('t7', timeout=5))
        self.assertEqual(self.argv()[-1], ['get', '--json', '--', 't7'])
        for scenario in ({'rc': 3, 'err': err('machine_unknown')}, {'rc': 3, 'err': 'garbled'},
                         {'rc': 1, 'err': err('not_found')}, {'out': 'garbled'}):
            with self.subTest(scenario):
                self.answer(**scenario)
                with self.assertRaises(hc.Error):
                    hc.get('t7', timeout=5)

    def test_terminals_needs_a_list(self):
        self.answer(out={'terminals': [{'address': 't7'}]})
        self.assertEqual(hc.terminals(timeout=5), [{'address': 't7'}])
        for out in ({}, {'terminals': {}}, {'terminals': ['t7']}):
            with self.subTest(out):
                self.answer(out=out)
                with self.assertRaises(hc.Error):
                    hc.terminals(timeout=5)


class SendTest(StubbedHc):
    def test_outcomes(self):
        for scenario, outcome in (
                ({'out': {'started': True, 'status': 'idle', 'turn': 3}}, hc.SENT),
                ({'out': {'started': None}}, hc.UNCERTAIN),          # a program that reports no status
                ({'out': {'started': False}}, hc.UNCERTAIN),
                ({'out': 'garbled'}, hc.UNCERTAIN),
                ({'rc': 10, 'err': err('status_mismatch')}, hc.NOT_TYPED_OUTCOME),
                ({'rc': 9, 'err': err('blocked')}, hc.NOT_TYPED_OUTCOME),
                ({'rc': 3, 'err': err('not_found')}, hc.NOT_TYPED_OUTCOME),
                ({'rc': 11, 'err': err('finished')}, hc.NOT_TYPED_OUTCOME),
                ({'rc': 1, 'err': err('unavailable')}, hc.UNCERTAIN),
                ({'rc': 1}, hc.UNCERTAIN),
                ({'sleep': 10}, hc.UNCERTAIN)):
            with self.subTest(scenario):
                self.answer(**scenario)
                self.assertEqual(hc.send('t7', 'text', timeout=0.5 if 'sleep' in scenario else 5)[0], outcome)

    def test_a_target_already_working_cannot_show_a_new_start(self):
        for out, outcome in (({'started': False, 'status': 'working', 'turn': 4}, hc.SENT),
                             ({'started': None, 'turn': 4}, hc.SENT),
                             ({'started': False, 'status': 'idle', 'turn': 4}, hc.UNCERTAIN),  # it had stopped
                             ('garbled', hc.UNCERTAIN)):
            with self.subTest(out):
                self.answer(out=out)
                self.assertEqual(hc.send('t7', 'x', timeout=5, was_working=True)[0], outcome)
                self.assertEqual(hc.send('t7', 'x', timeout=5)[0], hc.UNCERTAIN)
        self.answer(rc=10, err=err('status_mismatch'))
        self.assertEqual(hc.send('t7', 'x', timeout=5, was_working=True)[0], hc.NOT_TYPED_OUTCOME)

    def test_caller_is_passed_as_hc_terminal_id_and_the_rest_of_the_environment_kept(self):
        """hc tags what it pastes into an agent by the sender's HC_TERMINAL_ID; a shell that dropped it sends
        untagged, as a person would."""
        self.answer(out={'started': True})
        with mock.patch.dict('os.environ', {'HC_TERMINAL_SOCKET': '/run/s.sock'}):
            os.environ.pop('HC_TERMINAL_ID', None)
            hc.send('t7', 'x', timeout=5, caller='t5')
            hc.send('t7', 'x', timeout=5)
            os.environ['HC_TERMINAL_ID'] = 't9'
            hc.send('t7', 'x', timeout=5, caller='t5')
        self.assertEqual([(c['id'], c['socket']) for c in self.calls()],
                         [('t5', '/run/s.sock'), (None, '/run/s.sock'), ('t5', '/run/s.sock')])

    def test_options_precede_the_terminal_and_text(self):
        self.answer(out={'started': True})
        hc.send('t7', '--if idle', timeout=5, only_if='working')
        self.assertEqual(self.argv()[-1], ['send', '--json', '--if', 'working', '--', 't7', '--if idle'])

    def test_hc_that_cannot_start_typed_nothing(self):
        with mock.patch.dict('os.environ', {'PATH': str(self.dir)}):
            self.assertEqual(hc.send('t7', 'x', timeout=5)[0], hc.NOT_TYPED_OUTCOME)


class ReadTest(StubbedHc):
    def test_returns_the_text_as_is(self):
        self.answer(out='❯ \x1b[2mnot json\n')
        self.assertEqual(hc.read('t7', timeout=5, lines=50), '❯ \x1b[2mnot json\n')
        self.assertEqual(self.argv()[-1], ['read', '--lines', '50', '--', 't7'])
        self.answer(rc=3, err=err('not_found'))
        with self.assertRaises(hc.Error):
            hc.read('t7', timeout=5)


class ProcessTest(unittest.TestCase):
    def test_is_local(self):
        self.assertEqual([hc.is_local(a) for a in ('t7', 'ci/t3', '', None)], [True, False, False, False])

    @unittest.skipUnless(LINUX, '/proc is Linux-only')
    def test_start_time_is_an_int_and_an_exited_process_is_gone(self):
        with open(f'/proc/{os.getpid()}/stat') as f:
            self.assertEqual(hc.proc_start(os.getpid()), int(f.read().rsplit(')', 1)[1].split()[19]))
        child = subprocess.Popen([sys.executable, '-c', ''])
        child.wait()
        with self.assertRaises(hc.Gone):
            hc.proc_start(child.pid)
        with mock.patch('builtins.open', side_effect=PermissionError(13, 'denied')), \
             self.assertRaisesRegex(hc.Error, 'cannot inspect'):
            hc.proc_start(os.getpid())

    def test_off_linux_there_is_no_start_time(self):
        with mock.patch.object(hc.sys, 'platform', 'darwin'):
            self.assertIsNone(hc.proc_start(os.getpid()))
            self.assertEqual(hc.ancestors(os.getpid()), [os.getpid()])

    @unittest.skipUnless(LINUX, '/proc is Linux-only')
    def test_ancestors_run_up_from_this_process(self):
        chain = hc.ancestors(os.getpid())
        self.assertEqual(chain[:2], [os.getpid(), os.getppid()])

    def test_foreground(self):
        live = {'state': 'live', 'foreground': {'pid': os.getpid(), 'cwd': '/w'}}
        self.assertEqual(hc.foreground(live)['pid'], os.getpid())
        self.assertEqual(hc.foreground(live)['cwd'], '/w')
        for info in (None, {**live, 'state': 'finished'}, {'state': 'live', 'foreground': None},
                     {'state': 'live', 'foreground': {'pid': 'x'}}):
            self.assertIsNone(hc.foreground(info))


class OwnTerminalTest(StubbedHc):
    def record(self, pid):
        return {'address': 't5', 'state': 'live', 'foreground': {'pid': pid, 'cwd': '/w'}}

    def stranger(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child.pid

    def terminal(self, address, pid, state='live'):
        return {'out': {'address': address, 'state': state, 'foreground': {'pid': pid, 'cwd': '/w'}}}

    def without_hint(self, *terminals):
        """own_terminal with HC_TERMINAL_ID unset (or empty) and hc listing terminals, each (address, pid, state)."""
        by = {'ls': {'out': {'terminals': [{'address': a, 'state': st} for a, _, st in terminals]}}}
        by.update({f'get:{a}': self.terminal(a, pid, st) for a, pid, st in terminals})
        self.answer(by=by)
        with mock.patch.dict('os.environ', {'HC_TERMINAL_ID': ''}):
            return hc.own_terminal(timeout=5)

    def test_a_remote_hint_is_refused(self):
        with mock.patch.dict('os.environ', {'HC_TERMINAL_ID': 'ci/t5'}), self.assertRaises(hc.Unverified):
            hc.own_terminal(timeout=5)

    @unittest.skipUnless(LINUX, 'ancestry is read from /proc')
    def test_without_the_hint_the_one_terminal_running_this_process_is_found(self):
        other = self.stranger()
        address, _, fg = self.without_hint(('t2', other, 'live'), ('t5', os.getppid(), 'live'),
                                           ('ci/t3', os.getpid(), 'live'), ('t9', os.getpid(), 'finished'))
        self.assertEqual((address, fg['pid']), ('t5', os.getppid()))
        looked_up = {a[-1] for a in self.argv() if a[0] == 'get'}
        self.assertEqual(looked_up, {'t2', 't5'}, 'remote and finished terminals are not looked up')

    @unittest.skipUnless(LINUX, 'ancestry is read from /proc')
    def test_without_the_hint_none_or_several_is_a_refusal(self):
        with self.assertRaisesRegex(hc.Unverified, 'no live local hc terminal runs this process'):
            self.without_hint(('t2', self.stranger(), 'live'))
        with self.assertRaisesRegex(hc.Unverified, r'2 hc terminals \(t5, t6\) run this process'):
            self.without_hint(('t5', os.getppid(), 'live'), ('t6', os.getpid(), 'live'))

    def test_off_linux_without_the_hint_is_a_refusal(self):
        with mock.patch.dict('os.environ'), mock.patch.object(hc.sys, 'platform', 'darwin'):
            os.environ.pop('HC_TERMINAL_ID', None)
            with self.assertRaisesRegex(hc.Unverified, 'HC_TERMINAL_ID is unset'):
                hc.own_terminal(timeout=5)
        self.assertFalse(self.log.exists(), 'no hc call off Linux without the hint')

    def test_the_hint_is_verified_by_ancestry(self):
        with mock.patch.dict('os.environ', {'HC_TERMINAL_ID': 't5'}):
            self.answer(out=self.record(os.getppid()))
            address, info, fg = hc.own_terminal(timeout=5)
            self.assertEqual((address, fg['pid']), ('t5', os.getppid()))
            self.answer(rc=3, err=err('not_found'))
            with self.assertRaises(hc.Unverified):
                hc.own_terminal(timeout=5)
            if not LINUX:
                return
            stranger = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            self.addCleanup(stranger.wait)
            self.addCleanup(stranger.kill)
            self.answer(out=self.record(stranger.pid))
            with self.assertRaisesRegex(hc.Unverified, 'does not run this one'):
                hc.own_terminal(timeout=5)

    def test_off_linux_the_hint_is_taken_as_given(self):
        with mock.patch.dict('os.environ', {'HC_TERMINAL_ID': 't5'}), \
             mock.patch.object(hc.sys, 'platform', 'darwin'):
            self.answer(out=self.record(1))
            self.assertEqual(hc.own_terminal(timeout=5)[0], 't5')


if __name__ == '__main__':
    unittest.main()
