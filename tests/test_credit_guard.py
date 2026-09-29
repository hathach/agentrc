"""Tests for hooks/credit-guard/credit_guard.py with the usage fetch stubbed."""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'credit-guard' / 'credit_guard.py'
spec = importlib.util.spec_from_file_location('credit_guard', SCRIPT)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

SID = '691dbea2-d051-4452-beb0-308e1444690e'
T0 = 1_800_000_000.0


def usage(enabled=True, **percent):
    """A payload shaped like the live one; percent by limit kind."""
    return {'limits': [{'kind': k, 'percent': p, 'scope': None} for k, p in (percent or {'session': 10}).items()],
            'extra_usage': {'is_enabled': enabled}, 'spend': {'enabled': enabled}}


def ev(name, sid=SID):
    return {'hook_event_name': name, 'session_id': sid}


class Guard(unittest.TestCase):
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

    def test_a_failed_refresh_keeps_the_last_reading_and_retries_every_floor(self):
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
        with (self.state / 'usage.lock').open('w') as held, mock.patch.object(cg, 'LOCK_WAIT', 0.2):
            fcntl.flock(held, fcntl.LOCK_EX)
            out = cg.handle(ev('PreToolUse'), T0)
        self.assertBlocks(out)
        self.assertIn('lock busy', out['stopReason'])
        self.assertEqual(self.calls, 0)

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


if __name__ == '__main__':
    unittest.main()
