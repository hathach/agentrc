"""Tests for hooks/credit-guard/credit_guard.py with the usage fetch stubbed."""
import contextlib
import datetime
import errno
import io
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time as _time
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'credit-guard' / 'credit_guard.py'
spec = importlib.util.spec_from_file_location('credit_guard', SCRIPT)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

SID = '691dbea2-d051-4452-beb0-308e1444690e'
T0 = 1_800_000_000.0


def usage(enabled=True, resets=None, **percent):
    """A payload shaped like the live one; percent by limit kind, resets_at by kind as epoch seconds."""
    def lim(k, p):
        at = (resets or {}).get(k)
        return {'kind': k, 'percent': p, 'scope': None,
                **({'resets_at': datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()} if at else {})}
    return {'limits': [lim(k, p) for k, p in (percent or {'session': 10}).items()],
            'extra_usage': {'is_enabled': enabled}, 'spend': {'enabled': enabled}}


class FakeTime:
    """cg.time with a clock that sleep() advances; on_sleep(fake) runs after each sleep."""
    def __init__(self, now):
        self.now, self.slept, self.on_sleep = now, [], None

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        assert secs >= 0, secs
        self.slept.append(secs)
        self.now += secs
        if self.on_sleep:
            self.on_sleep(self)

    localtime, strftime = staticmethod(_time.localtime), staticmethod(_time.strftime)


def ev(name, sid=SID):
    return {'hook_event_name': name, 'session_id': sid}


def setUpModule():
    guard_survivors(__name__)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'credit-guard'
        env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.replies = []
        self.calls = 0
        patch = mock.patch.object(cg, 'fetch', self.fake_fetch)
        patch.start()
        self.addCleanup(patch.stop)

    def fake_fetch(self):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def open_session(self, *replies, now=T0):
        self.replies += replies
        self.assertIsNone(cg.handle(ev('SessionStart'), now))

    def assertBlocks(self, out, event='PreToolUse'):
        self.assertIsNotNone(out)
        if event == 'UserPromptSubmit':
            self.assertEqual(out['decision'], 'block')
        else:
            self.assertIs(out['continue'], False)
            self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')


class Guard(Base):
    def test_credits_disabled_turns_the_guard_off_for_the_session(self):
        self.open_session(usage(enabled=False))
        for name in ('UserPromptSubmit', 'PreToolUse'):
            self.assertIsNone(cg.handle(ev(name), T0 + 1))
        self.assertEqual(self.calls, 1, 'one check at open, none after')

    def test_only_both_flags_false_turn_it_off(self):
        mixed = usage(enabled=False)
        mixed['spend']['enabled'] = True
        for payload in (mixed, {'limits': []}):
            with self.subTest(payload=payload):
                self.assertFalse(cg.credits_off(payload))

    def test_an_armed_session_reuses_the_opening_reading_and_blocks_at_100(self):
        self.open_session(usage(session=40))
        self.assertIsNone(cg.handle(ev('UserPromptSubmit'), T0 + 1))
        self.assertEqual(self.calls, 1, 'the first prompt reuses the opening fetch')
        self.replies.append(usage(session=100))
        self.assertBlocks(cg.handle(ev('PreToolUse'), T0 + 10_000))
        self.replies.append(usage(session=100))
        self.assertBlocks(cg.handle(ev('UserPromptSubmit'), T0 + 20_000), 'UserPromptSubmit')

    def test_any_limit_at_100_blocks(self):
        self.open_session(usage(session=3, weekly_all=100))
        out = cg.handle(ev('PreToolUse'), T0 + 1)
        self.assertBlocks(out)
        self.assertIn('weekly_all', out['stopReason'])

    def test_limits_are_named_by_kind_and_model_scope(self):
        data = {'limits': [{'kind': 'session', 'percent': 5, 'scope': None},
                           {'kind': 'weekly_scoped', 'percent': 7,
                            'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}}]}
        self.assertEqual(cg.limits(data), {'session': 5, 'weekly_scoped:Fable': 7})

    def test_a_limit_without_a_usable_percent_rejects_the_payload(self):
        for bad in (None, True, float('nan'), float('inf'), '50'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cg.limits({'limits': [{'kind': 'session', 'percent': 5}, {'kind': 'weekly_all', 'percent': bad}]})
        with self.assertRaises(ValueError):
            cg.limits({'five_hour': {'utilization': 12.0}})

    def test_a_fresh_reading_is_reused_until_its_refresh_is_due(self):
        self.open_session(usage(session=10))  # 90% left: trusted 600 s, refresh due at 570 s
        for t in (1, 100, 500):
            self.assertIsNone(cg.handle(ev('PreToolUse'), T0 + t))
        self.assertEqual(self.calls, 1)
        self.replies.append(usage(session=10))
        cg.handle(ev('PreToolUse'), T0 + 575)
        self.assertEqual(self.calls, 2)

    def test_the_trust_window_shrinks_with_headroom_and_burn_rate(self):
        self.assertEqual(cg.trust_window(T0, {'s': 10}, None), 600)
        self.assertEqual(cg.trust_window(T0, {'s': 70}, None), 300)
        self.assertEqual(cg.trust_window(T0, {'s': 85}, None), 120)
        self.assertEqual(cg.trust_window(T0, {'s': 92}, None), 60)
        self.assertEqual(cg.trust_window(T0, {'s': 97}, None), cg.FLOOR)
        # 10 -> 30% in 600 s: 70% left at 2%/min is 35 min to 100%, a quarter of it is 525 s
        self.assertAlmostEqual(cg.trust_window(T0 + 600, {'s': 30}, {'at': T0, 'used': {'s': 10}}), 525)
        self.assertEqual(cg.trust_window(T0 + 60, {'s': 40}, {'at': T0, 'used': {'s': 10}}), cg.FLOOR)
        # a window reset (usage fell) is no burn
        self.assertEqual(cg.trust_window(T0 + 60, {'s': 5}, {'at': T0, 'used': {'s': 90}}), 600)

    def test_a_failed_refresh_keeps_the_last_reading_and_retries_after_floor(self):
        self.open_session(usage(session=10))
        self.replies.append(OSError('429'))
        self.assertIsNone(cg.handle(ev('PreToolUse'), T0 + 580))
        self.assertIsNone(cg.handle(ev('PreToolUse'), T0 + 609), 'past the 600 s trust, before the retry')
        self.assertEqual(self.calls, 2, 'hooks behind a failed refresh do not fetch again')
        self.replies.append({'limits': []})  # an unparseable payload is a failed refresh too
        self.assertIsNone(cg.handle(ev('PreToolUse'), T0 + 611))
        self.assertEqual(self.calls, 3)
        self.replies.append(usage(session=100))
        self.assertBlocks(cg.handle(ev('PreToolUse'), T0 + 3640))

    def test_a_failed_refresh_of_a_reading_at_100_still_blocks(self):
        self.open_session(usage(session=100))
        self.replies.append(OSError('timed out'))
        self.assertBlocks(cg.handle(ev('PreToolUse'), T0 + 3600))
        self.assertEqual(self.calls, 2)

    def test_a_fresh_reading_that_cannot_be_stored_blocks(self):
        self.open_session(usage(session=10))
        (self.state / 'readings.log').unlink()
        (self.state / 'readings.log').mkdir()
        self.replies.append(usage(session=100))
        self.assertBlocks(cg.handle(ev('PreToolUse'), T0 + 600))

    def test_an_opening_does_not_overwrite_a_newer_reading(self):
        self.open_session(usage(session=100), now=T0 + 50)
        self.open_session(usage(session=80), now=T0)  # its fetch started earlier, finished later
        self.assertEqual(cg.load(self.state)['used'], {'session': 100})

    def test_any_usage_failure_while_armed_blocks(self):
        for bad in (OSError('offline'), ValueError('usage error body'), {'limits': []}):
            with self.subTest(bad=bad):
                (self.state / 'usage.json').unlink(missing_ok=True)
                self.replies.append(bad)
                self.assertBlocks(cg.handle(ev('UserPromptSubmit'), T0 + 1), 'UserPromptSubmit')

    def test_a_malformed_cached_reading_is_not_trusted(self):
        self.state.mkdir()
        for rec in ({'at': T0, 'used': {}, 'until': T0 + 600, 'retry_at': T0 + 570},
                    {'at': T0, 'used': {'session': None}, 'until': T0 + 600, 'retry_at': T0 + 570},
                    {'used': {'session': 5}}, [], 'x'):
            with self.subTest(rec=rec):
                (self.state / 'usage.json').write_text(json.dumps(rec))
                self.replies.append(usage(session=100))
                self.assertBlocks(cg.handle(ev('PreToolUse'), T0 + 1))

    def test_a_busy_lock_blocks_before_the_hook_timeout(self):
        self.state.mkdir()
        with mock.patch.object(cg, 'LOCK_WAIT', 0.2), cg.locked(self.state):  # held through its own handle
            out = cg.handle(ev('PreToolUse'), T0)
        self.assertBlocks(out)
        self.assertIn('lock busy', out['stopReason'])
        self.assertEqual(self.calls, 0)

    @unittest.skipUnless(os.name == 'nt', 'msvcrt locks are Windows only')
    def test_a_lock_failure_other_than_contention_is_not_waited_out(self):
        self.state.mkdir()
        failure = OSError(errno.EBADF, 'bad file descriptor')
        with mock.patch.object(cg.msvcrt, 'locking', side_effect=failure), self.assertRaises(OSError) as raised, \
                cg.locked(self.state):
            pass
        self.assertEqual(raised.exception.errno, errno.EBADF, 'not a TimeoutError after LOCK_WAIT')

    @unittest.skipUnless(os.name == 'nt', 'msvcrt locks are Windows only')
    def test_the_lock_is_released_before_its_handle_closes(self):
        self.state.mkdir()
        with mock.patch.object(cg.msvcrt, 'locking', wraps=cg.msvcrt.locking) as locking:
            with cg.locked(self.state):
                pass
        self.assertEqual([c.args[1] for c in locking.call_args_list], [cg.msvcrt.LK_NBLCK, cg.msvcrt.LK_UNLCK])
        with cg.locked(self.state):  # and at once free for the next hook
            pass

    def test_the_readings_log_stamps_a_full_time_one_line_per_reading(self):
        self.open_session(usage(session=10))
        raw = (self.state / 'readings.log').read_bytes()
        self.assertNotIn(b'\r', raw)
        self.assertRegex(json.loads(raw)['time'], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$')

    def test_an_unfinished_or_missing_check_leaves_the_session_armed(self):
        self.replies.append(OSError('offline'))
        with self.assertRaises(OSError):
            cg.handle(ev('SessionStart'), T0)
        self.assertTrue(cg.armed(ev('X')) and cg.armed(ev('X', sid='never-opened')) and cg.armed(ev('X', sid='../x')))
        # a resume whose check fails does not keep an earlier off
        self.open_session(usage(enabled=False))
        self.assertFalse(cg.armed(ev('X')))
        self.replies.append(OSError('offline'))
        with self.assertRaises(OSError):
            cg.handle(ev('SessionStart'), T0 + 5)
        self.assertTrue(cg.armed(ev('X')))

    def test_the_readings_log_rotates(self):
        with mock.patch.object(cg, 'LOG_MAX', 100):
            for t in range(3):
                self.open_session(usage(session=10), now=T0 + t)
        self.assertTrue((self.state / 'readings.log.1').exists())
        self.assertLessEqual((self.state / 'readings.log').stat().st_size, 400)

    def test_the_script_blocks_when_it_fails_and_stays_silent_at_session_start(self):
        env = {**os.environ, 'CLAUDE_CONFIG_DIR': self.tmp.name}  # no .credentials.json: every fetch fails
        def run(stdin):
            r = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True,
                               text=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout) if r.stdout.strip() else None
        self.assertIsNone(run(json.dumps(ev('SessionStart'))))
        self.assertBlocks(run(json.dumps(ev('PreToolUse'))))
        self.assertBlocks(run(json.dumps(ev('UserPromptSubmit'))), 'UserPromptSubmit')
        self.assertIs(run('not json')['continue'], False)

    def test_the_script_blocks_on_a_malformed_timeout_and_lists_waiters(self):
        env = {**os.environ, 'CLAUDE_CONFIG_DIR': self.tmp.name}
        self.open_session(usage(enabled=False))  # guard off: only the argument check can block
        for args in (['--timeout'], ['--timeout', 'x'], ['--wait', '5'], ['--timeout', '5', 'x']):
            with self.subTest(args=args):
                r = subprocess.run([sys.executable, str(SCRIPT), *args], input=json.dumps(ev('PreToolUse')),
                                   capture_output=True, text=True, env=env, timeout=30)
                self.assertIs(json.loads(r.stdout)['continue'], False)
        r = subprocess.run([sys.executable, str(SCRIPT), 'status'], capture_output=True, text=True, env=env,
                           timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ''), r.stderr)

    def test_the_script_reads_its_event_as_utf8(self):
        env = {**os.environ, 'CLAUDE_CONFIG_DIR': self.tmp.name}
        env.pop('PYTHONUTF8', None)
        self.open_session(usage(enabled=False))  # guard off: a correctly read prompt gets no reply
        event = {**ev('UserPromptSubmit'), 'prompt': 'café ✔ ő'}
        r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(event, ensure_ascii=False).encode(),
                           capture_output=True, env=env, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, b''), r.stderr)


class Wait(Base):
    """A full limit waits in a hook registered with --timeout."""
    TIMEOUT = cg.MAX_WAIT + cg.WAIT_MARGIN

    def setUp(self):
        super().setUp()
        self.clock = FakeTime(T0 + 1)
        patch = mock.patch.object(cg, 'time', self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.seen_waiters = []
        self.clock.on_sleep = lambda c: self.seen_waiters.append(cg.waiters())

    def handle(self, event, now, timeout=TIMEOUT):
        return cg.handle(event, now, timeout=timeout)

    def test_a_full_limit_waits_for_its_reset_then_lets_the_call_through(self):
        self.open_session(usage(session=100, resets={'session': T0 + 3600}))
        self.replies.append(usage(session=0))
        out = self.handle({**ev('PreToolUse'), 'agent_id': 'a1'}, T0 + 1)  # as a subagent's hook
        self.assertNotIn('continue', out)
        self.assertIn('waited 1h00m for session to reset', out['systemMessage'])
        self.assertEqual(self.calls, 2, 'one read at the reset, none while sleeping')
        self.assertAlmostEqual(self.clock.now, T0 + 3600 + cg.RESET_SLACK)
        self.assertLessEqual(max(self.clock.slept), cg.SLICE)
        rec = self.seen_waiters[0][0]
        self.assertEqual((rec['event'], rec['agent_id'], rec['limits']), ('PreToolUse', 'a1', ['session']))
        self.assertEqual(rec['next_check'], T0 + 3600 + cg.RESET_SLACK, 'the next usage read, not the heartbeat')
        self.assertEqual(cg.waiters(), [], 'the record goes with the wait')

    def test_a_limit_still_full_after_its_reset_is_polled_with_backoff(self):
        self.open_session(usage(session=100, resets={'session': T0 + 100}))
        self.replies += [usage(session=100), OSError('429'), usage(session=100, weekly_all=100), usage(session=3)]
        out = self.handle(ev('UserPromptSubmit'), T0 + 1)
        self.assertIn('systemMessage', out)
        self.assertEqual(self.clock.slept[-3:], [30, 60, 120])

    def test_a_reset_beyond_the_budget_stops_at_once_naming_it(self):
        self.open_session(usage(weekly_all=100, resets={'weekly_all': T0 + cg.MAX_WAIT + 3600}))
        out = self.handle(ev('PreToolUse'), T0 + 1)
        self.assertBlocks(out)
        self.assertIn('resets ', out['stopReason'])
        self.assertIn("beyond this hook's wait", out['stopReason'])
        self.assertEqual(self.clock.slept, [])

    def test_a_limit_that_stays_full_stops_when_the_budget_runs_out(self):
        self.open_session(usage(session=100))  # no reset known: poll
        self.clock.on_sleep = lambda c: self.replies.append(usage(session=100))
        out = self.handle(ev('PreToolUse'), T0 + 1)
        self.assertBlocks(out)
        self.assertIn('still full after waiting', out['stopReason'])
        self.assertAlmostEqual(sum(self.clock.slept), cg.MAX_WAIT)

    def test_a_failed_refresh_never_clears_the_wait(self):
        self.open_session(usage(session=100, resets={'session': T0 + 100}))
        def later_reader(c):  # another session stored 99% while this hook slept; trusted only until T0 + 130
            if len(c.slept) == 1:
                cg.store(self.state, {'session': 99}, {}, T0 + 100, None)
        self.clock.on_sleep = later_reader
        self.replies += [OSError('offline')] * 3 + [usage(session=2)]
        out = self.handle(ev('PreToolUse'), T0 + 1)
        self.assertIn('systemMessage', out)
        self.assertEqual(self.calls, 5, 'the stale 99% did not end the wait; the fresh 2% did')

    def test_a_trusted_reading_another_session_stored_clears_the_wait(self):
        self.open_session(usage(session=100, resets={'session': T0 + 100}))
        self.clock.on_sleep = lambda c: cg.store(self.state, {'session': 1}, {}, c.now - 5, None)
        self.assertIn('systemMessage', self.handle(ev('PreToolUse'), T0 + 1))
        self.assertEqual(self.calls, 1)

    def test_a_refused_usage_read_before_waiting_stops_at_once(self):
        import urllib.error
        for code in (401, 403):
            with self.subTest(code=code):
                self.open_session(usage(session=100, resets={'session': T0 + 100}))
                self.replies.append(urllib.error.HTTPError(cg.URL, code, 'refused', {}, None))
                out = self.handle(ev('PreToolUse'), T0 + 10_000)
                self.assertBlocks(out)
                self.assertIn(f'HTTP {code}', out['stopReason'])
                self.assertEqual(self.clock.slept, [])

    def test_a_refused_usage_read_while_waiting_stops(self):
        import urllib.error
        self.open_session(usage(session=100, resets={'session': T0 + 100}))
        self.replies.append(urllib.error.HTTPError(cg.URL, 401, 'Unauthorized', {}, None))
        out = self.handle(ev('PreToolUse'), T0 + 1)
        self.assertBlocks(out)
        self.assertIn('HTTP 401', out['stopReason'])

    def test_a_hook_registered_without_a_timeout_stops_as_before(self):
        self.open_session(usage(session=100, resets={'session': T0 + 100}))
        out = self.handle(ev('PreToolUse'), T0 + 1, timeout=None)
        self.assertBlocks(out)
        self.assertIn(f'(session, resets {cg.local_time(T0 + 100)})', out['stopReason'])
        self.assertEqual(self.clock.slept, [])

    def test_the_budget_is_the_registered_timeout_less_the_margin(self):
        self.assertEqual(cg.wait_budget(self.TIMEOUT), cg.MAX_WAIT)
        self.assertEqual(cg.wait_budget(10 * cg.MAX_WAIT), cg.MAX_WAIT)
        self.assertEqual(cg.wait_budget(900), 900 - cg.WAIT_MARGIN)
        self.assertEqual([cg.wait_budget(t) for t in (None, 15, 0)], [0, 0, 0])

    @unittest.skipUnless(shutil.which('jq'), 'the recipe is jq')
    def test_the_readme_recipe_registers_each_hook_with_its_own_timeout(self):
        reg = json.loads((SCRIPT.parent / 'hooks.json').read_text())
        self.assertEqual([reg[n][0]['hooks'][0]['timeout'] for n in cg.WAIT_EVENTS], [self.TIMEOUT] * 2)
        readme = (SCRIPT.parents[2] / 'README.md').read_text(encoding='utf-8')
        filt = re.search(r"\n  '(\.hooks = reduce .*?)' \\\n", readme, re.S).group(1)
        settings = Path(self.tmp.name) / 'settings.json'
        settings.write_text(json.dumps({'model': 'x', 'hooks': {
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/old/credit_guard.py',
                                                          'timeout': 15}, {'type': 'command', 'command': 'keep.py'}]},
                           {'hooks': [{'type': 'prompt', 'prompt': 'p'}]}],
            'SessionStart': [{'hooks': [{'type': 'command', 'command': '/old/credit_guard.py', 'timeout': 15}]}]}}))
        for _ in range(2):
            out = subprocess.run(['jq', '--arg', 'c', '/new/credit_guard.py', '--slurpfile', 'h',
                                  str(SCRIPT.parent / 'hooks.json'), filt, str(settings)],
                                 capture_output=True, text=True, check=True).stdout
            settings.write_text(out)
        got = json.loads(out)
        self.assertEqual(got['model'], 'x')
        guards = [(n, h) for n, groups in got['hooks'].items() for g in groups for h in g['hooks']
                  if 'credit_guard.py' in h.get('command', '')]
        self.assertEqual(sorted(n for n, _ in guards), ['PreToolUse', 'SessionStart', 'UserPromptSubmit'])
        for _, h in guards:
            self.assertEqual(h['command'], f'/new/credit_guard.py --timeout {h["timeout"]}')
        self.assertEqual(got['hooks']['PreToolUse'][:2], [
            {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'keep.py'}]},
            {'hooks': [{'type': 'prompt', 'prompt': 'p'}]}])

    def test_resets_are_kept_and_an_older_reading_without_them_loads(self):
        self.open_session(usage(session=5, weekly_all=7, resets={'session': T0 + 60}))
        self.assertEqual(cg.load(self.state)['resets'], {'session': T0 + 60, 'weekly_all': None})
        rec = json.loads((self.state / 'usage.json').read_text())
        del rec['resets']
        (self.state / 'usage.json').write_text(json.dumps(rec))
        self.assertEqual(cg.load(self.state)['resets'], {})
        self.assertIsNone(cg.reset_at(cg.load(self.state), ['session']))

    def test_failed_refreshes_back_off_to_a_slice_and_a_success_resets_them(self):
        self.open_session(usage(session=10))
        rec = json.loads((self.state / 'usage.json').read_text())
        self.assertEqual(cg.load(self.state)['fails'], 0, 'a reading without the count has none')
        steps, t = [], T0 + 600
        for _ in range(6):
            self.replies.append(OSError('429'))
            cg.reading(t)
            last = cg.load(self.state)
            steps.append(round(last['retry_at'] - t))
            t = last['retry_at']
        self.assertEqual(steps, [30, 60, 120, 240, 300, 300])
        self.assertEqual(cg.load(self.state)['until'], rec['until'], 'a failed refresh never extends the trust')
        self.replies.append(usage(session=10))
        cg.reading(t)
        self.assertEqual(cg.load(self.state)['fails'], 0)

    def test_a_full_reading_with_known_resets_is_reused_until_the_last_of_them(self):
        self.open_session(usage(session=100, weekly_all=100, resets={'session': T0 + 600, 'weekly_all': T0 + 900}))
        self.assertEqual(cg.load(self.state)['retry_at'], T0 + 900 + cg.RESET_SLACK)
        for t in (T0 + 1, T0 + 700, T0 + 950):
            self.assertBlocks(cg.handle(ev('PreToolUse'), t))  # no budget here: blocks, but fetches nothing
        self.assertEqual(self.calls, 1)
        self.open_session(usage(session=100))  # no reset known: the plain FLOOR window
        self.assertEqual(cg.load(self.state)['retry_at'], T0 + cg.FLOOR)
        self.open_session(usage(session=100, weekly_all=3, resets={'session': T0 + 600}))  # a full limit only counts
        self.assertEqual(cg.load(self.state)['retry_at'], T0 + 600 + cg.RESET_SLACK)

    def test_a_failed_read_at_the_reset_keeps_waiting(self):
        self.open_session(usage(session=100, resets={'session': T0 + 100}))
        self.replies += [OSError('offline'), usage(session=0)]
        self.assertIn('systemMessage', self.handle(ev('PreToolUse'), T0 + 1))
        self.assertEqual(self.calls, 3)

    def test_waiters_filter_by_tag_and_reading_them_creates_nothing(self):
        self.assertEqual(cg.waiters(), [])
        self.assertFalse(self.state.exists(), 'a reader made the state dir')
        d = self.state / 'waits'
        d.mkdir(parents=True)
        cg.write_atomic(d / '1.json', {'seen': self.clock.now, 'since': T0, 'tag': 'a'})
        cg.write_atomic(d / '2.json', {'seen': self.clock.now, 'since': T0, 'tag': 'b'})
        (d / '3.json').write_text('[]')  # not a record
        self.assertEqual([w['tag'] for w in cg.waiters('b')], ['b'])
        self.assertEqual(len(cg.waiters()), 2)

    def test_a_dead_waiters_record_is_ignored(self):
        d = self.state / 'waits'
        d.mkdir(parents=True)
        cg.write_atomic(d / '1.json', {'seen': self.clock.now - 3 * cg.SLICE, 'since': T0})
        cg.write_atomic(d / '2.json', {'seen': self.clock.now, 'since': T0 - 7200, 'pid': 2, 'event': 'PreToolUse',
                                       'limits': ['session'], 'next_check': self.clock.now + 60})
        self.assertEqual([w['pid'] for w in cg.waiters()], [2])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cg.status()
        self.assertRegex(out.getvalue(), r'pid 2 .*waiting 2h00m for session, next check \d{4}-')


if __name__ == '__main__':
    unittest.main()
