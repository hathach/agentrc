"""Tests for lib/check_runs.py, the run ordering collect.py and harvest.py share."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_runs  # noqa: E402
from helpers import guard_survivors  # noqa: E402


def setUpModule():
    guard_survivors(__name__)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(i, start, conclusion='success', summary=None, title=None):
    return {'id': i, 'status': 'completed', 'conclusion': conclusion, 'started_at': start,
            'output': {'title': title, 'summary': summary}}


class LatestRunTest(unittest.TestCase):
    def test_the_newest_run_or_why_none_can_be_told(self):
        one, undated = run(1, '2026-10-01T00:00:00Z'), run(1, None)
        later = run(2, '2026-10-01T01:00:00+00:00', 'failure')
        cases = [([one], (one, '')), ([undated], (undated, '')),
                 ([undated, run(2, '2026-10-01T00:00:00Z')], (None, 'check run 1 has no start time to order it by')),
                 ([one, later], (later, '')), ([one, run(2, '2026-10-01T00:00:00Z')], (one, '')),
                 ([one, run(2, '2026-10-01T00:00:00Z', 'failure')], (None, '1, 2 share 2026-10-01T00:00:00Z and read differently')),
                 ([run(1, '2026-10-01T00:00:00Z', summary='a'), run(2, '2026-10-01T00:00:00Z', summary='b')],
                  (None, '1, 2 share 2026-10-01T00:00:00Z and read differently'))]
        for runs, want in cases:
            with self.subTest(runs=runs):
                self.assertEqual(check_runs.latest_run(runs), want)

    def test_a_title_tells_tied_runs_apart_only_when_asked(self):
        a, b = run(1, '2026-10-01T00:00:00Z', title='a'), run(2, '2026-10-01T00:00:00Z', title='b')
        self.assertEqual(check_runs.latest_run([a, b]), (a, ''))
        self.assertEqual(check_runs.latest_run([a, b], title=True), (None, '1, 2 share 2026-10-01T00:00:00Z and read differently'))

    def test_newest_skips_undated_items(self):
        items = [{'id': 1, 'at': None}, {'id': 2, 'at': '2026-10-01T00:00:00Z'}, {'id': 3, 'at': '2026-09-30T00:00:00Z'}]
        self.assertEqual(check_runs.newest(items, 'at')['id'], 2)
        self.assertIsNone(check_runs.newest(items[:1], 'at'))
        self.assertIsNone(check_runs.stamp(None))

    def test_both_scripts_read_the_shared_copy(self):
        harvest = load('pr_babysit_harvest', 'skills/pr-babysit/scripts/harvest.py')
        collect = load('ci_collect_shared', 'skills/ci-rerun/scripts/collect.py')
        for name in ('latest_run', 'newest', 'newest_reading', 'stamp'):
            self.assertIs(getattr(harvest, name), getattr(check_runs, name), name)
        self.assertIs(collect.latest_run, check_runs.latest_run)


if __name__ == '__main__':
    unittest.main()
