"""Tests for lib/check_runs.py, the run ordering collect.py and harvest.py share, and the approval gate collect.py and
pr-review's prepare.py share."""
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


HEAD, OTHER = 'a' * 40, 'b' * 40


def workflow_run(i, event='pull_request', status='completed', conclusion='action_required', head=HEAD):
    return {'id': i, 'event': event, 'status': status, 'conclusion': conclusion, 'head_sha': head}


class GatedRunsTest(unittest.TestCase):
    def test_a_run_awaits_approval_only_as_a_completed_action_required_pull_request_run(self):
        for want, r in ((True, workflow_run(1)),
                        # hathach/tinyusb run 43103189104: a fork contributor's review comment, held the same way.
                        (False, workflow_run(2, event='pull_request_review_comment')),
                        (False, workflow_run(3, event='pull_request_target')),
                        (False, workflow_run(4, status='queued', conclusion=None)),
                        (False, workflow_run(5, status='in_progress', conclusion='action_required')),
                        (False, workflow_run(6, conclusion='failure'))):
            with self.subTest(r['id']):
                self.assertIs(check_runs.awaiting_approval(r), want)

    def test_gates_are_read_by_githubs_filter_unless_the_whole_listing_is_given(self):
        listing = [workflow_run(1), workflow_run(2, head=OTHER), workflow_run(3, event='pull_request_review_comment'), workflow_run(4, conclusion='success')]
        paths = []

        def read(path):
            paths.append(path)
            return listing
        self.assertEqual([r['id'] for r in check_runs.gated_runs('o/r', HEAD, read)], [1], 'another head\'s and other events\' left out')
        self.assertEqual(paths, [f'repos/o/r/actions/runs?head_sha={HEAD}&event=pull_request&status=action_required'])
        paths.clear()
        self.assertEqual([r['id'] for r in check_runs.gated_runs('o/r', HEAD, read, listing)], [1])
        self.assertEqual(check_runs.gated_runs('o/r', HEAD, read, []), [], 'an empty listing is still a listing')
        self.assertEqual(paths, [], 'no request beside a listing already read')

        def failing(path):
            raise RuntimeError('HTTP 404')
        with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
            check_runs.gated_runs('o/r', HEAD, failing)

    def test_prepare_and_collect_read_the_gates_through_the_shared_copy(self):
        collect = load('ci_collect_gates', 'skills/ci-rerun/scripts/collect.py')
        prepare = load('pr_review_prepare_gates', 'skills/pr-review/scripts/prepare.py')
        self.assertIs(collect.gated_runs, check_runs.gated_runs)
        self.assertIs(prepare.gated_runs, check_runs.gated_runs)


if __name__ == '__main__':
    unittest.main()
