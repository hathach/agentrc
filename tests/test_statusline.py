import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'statusline'
spec = importlib.util.spec_from_file_location('codex_usage', ROOT / 'statusline-codex-usage.py')
codex_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex_usage)


def window(used, mins):
    return {'usedPercent': used, 'windowDurationMins': mins, 'resetsAt': 1}


class CodexWeeklyTest(unittest.TestCase):
    def test_the_codex_bucket_wins_and_either_slot_may_hold_the_week(self):
        result = {'rateLimitsByLimitId': {
            'other': {'primary': window(90, 10080)},
            'codex': {'primary': window(40, 10080), 'secondary': None}}}
        self.assertEqual(codex_usage.weekly(result)['usedPercent'], 40)
        result = {'rateLimitsByLimitId': {'codex': {'primary': window(5, 300), 'secondary': window(60, 10080)}}}
        self.assertEqual(codex_usage.weekly(result)['usedPercent'], 60)

    def test_legacy_snapshot_near_a_week_counts_but_another_limit_does_not(self):
        self.assertEqual(codex_usage.weekly({'rateLimits': {'primary': window(7, 10000)}})['usedPercent'], 7)
        self.assertIsNone(codex_usage.weekly({'rateLimits': {'limitId': 'gpt-x', 'primary': window(7, 10080)}}))
        self.assertIsNone(codex_usage.weekly({'rateLimits': {'primary': window(7, 300)}}))


class RenderTest(unittest.TestCase):
    def test_renders_from_input_alone_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            payload = {'workspace': {'current_dir': f'{home}/code'}, 'model': {'display_name': 'Opus (1M context)'},
                       'effort': {'level': 'high'}, 'context_window': {'used_percentage': 12}}
            done = subprocess.run(['bash', str(ROOT / 'statusline.sh')], input=json.dumps(payload),
                                  capture_output=True, text=True,
                                  env={**os.environ, 'HOME': home, 'CLAUDE_CONFIG_DIR': home})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('~/code', done.stdout)
        self.assertIn('Opus', done.stdout)
        self.assertNotIn('1M context', done.stdout)
        self.assertIn('12%', done.stdout)
        self.assertIn('?', done.stdout, 'no usage cache, so the placeholders show')


if __name__ == '__main__':
    unittest.main()
