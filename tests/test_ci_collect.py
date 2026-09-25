"""Tests for ci-rerun's collect.py against a fake gh."""
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    'ci_collect', Path(__file__).resolve().parents[1] / 'skills' / 'ci-rerun' / 'scripts' / 'collect.py')
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

HEAD, OTHER, BASE = 'a' * 40, 'b' * 40, 'c' * 40
JOB = 'https://github.com/o/r/actions/runs/7/job/{}'
RTD = 'https://app.readthedocs.org/projects/p/builds/{}/'
CIRCLE = 'https://circleci.com/gh/o/r/{}'


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.heads = []      # headRefOid per `gh pr view`, last one repeats
        self.listings = []   # `gh pr checks` answers in order: a list of checks, or (rc, stderr)
        self.calls = []
        self.clock = [0]

        def run(argv, **kw):
            self.calls.append(argv[1:3])
            if argv[1:3] == ['pr', 'view']:
                head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
                return mock.Mock(returncode=0, stdout=json.dumps(
                    {'headRefOid': head, 'baseRefName': 'master', 'baseRefOid': BASE}))
            answer = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
            if isinstance(answer, tuple):
                return mock.Mock(returncode=answer[0], stdout='', stderr=answer[1])
            return mock.Mock(returncode=0, stdout=json.dumps(answer))
        sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        for obj, name, fake in ((collect.subprocess, 'run', run), (collect.time, 'sleep', sleep),
                                (collect.time, 'monotonic', lambda: self.clock[0])):
            patcher = mock.patch.object(obj, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = collect.main(['inventory', '--repo', 'o/r', '--pr', '5', '--head', HEAD, *extra])
        return rc, json.loads(out.getvalue())

    @staticmethod
    def check(name, bucket, link):
        return {'name': name, 'workflow': 'Build', 'bucket': bucket, 'link': link}

    def test_red_lists_only_what_did_not_pass(self):
        self.heads = [HEAD]
        self.listings = [[self.check('a', 'pass', JOB.format(1)), self.check('b', 'skipping', JOB.format(2)),
                          self.check('hil', 'fail', JOB.format(3)), self.check('docs', 'fail', RTD.format(9)),
                          self.check('lint', 'cancel', 'https://example.com/x')]]
        rc, r = self.main()
        self.assertEqual((rc, r['status'], r['baseSha'], r['error']), (0, 'red', BASE, None))
        self.assertEqual(r['counts'], {'pass': 1, 'skipping': 1, 'fail': 2, 'cancel': 1})
        self.assertEqual([(c['name'], c['bucket'], c['link']) for c in r['checks']],
                         [('hil', 'fail', JOB.format(3)), ('docs', 'fail', RTD.format(9)), ('lint', 'cancel', 'https://example.com/x')])

    def test_attempt_only_from_a_link_that_names_one_run(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'fail', 'https://github.com/o/r/actions/runs/7/job/3'),
                          self.check('docs', 'fail', RTD.format(9)), self.check('cci', 'fail', CIRCLE.format(413502)),
                          self.check('preview', 'fail', 'https://p--5.org.readthedocs.build/en/5/'),
                          self.check('bot', 'fail', 'https://greptile.com/'), self.check('rabbit', 'fail', '')]]
        self.assertEqual([c['attempt'] for c in self.main()[1]['checks']],
                         ['actions:3', 'readthedocs:9', 'circleci:413502', None, None, None])

    def test_green_when_everything_passed_or_skipped(self):
        self.heads = [HEAD]
        self.listings = [[self.check('a', 'pass', JOB.format(1)), self.check('b', 'skipping', JOB.format(2))]]
        self.assertEqual(self.main()[1]['status'], 'green')

    def test_waits_while_pending_then_reports_the_settled_state(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]] * 3 + [[self.check('hil', 'fail', JOB.format(3))]]
        rc, r = self.main('--wait-seconds', '600')
        self.assertEqual((r['status'], r['waited']), ('red', 90))

    def test_stops_waiting_when_the_budget_is_spent(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]]
        rc, r = self.main('--wait-seconds', '70')
        self.assertEqual((rc, r['status'], r['waited']), (0, 'running', 60))
        self.assertEqual(self.calls.count(['pr', 'checks']), 3)

    def test_no_wait_budget_reports_what_stands(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]]
        rc, r = self.main()
        self.assertEqual((r['status'], r['waited'], self.calls.count(['pr', 'checks'])), ('running', 0, 1))

    def test_no_checks_registered_yet_is_running_not_green(self):
        self.heads = [HEAD]
        self.listings = [(1, "no checks reported on the 'x' branch")]
        rc, r = self.main()
        self.assertEqual((rc, r['status'], r['checks']), (0, 'running', []))

    def test_a_gh_failure_is_an_error_not_a_status(self):
        self.heads = [HEAD]
        self.listings = [(1, 'HTTP 502')]
        rc, r = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('HTTP 502', r['error'])
        self.assertNotIn('status', r)

    def test_a_head_other_than_expected_is_refused_before_listing(self):
        self.heads = [OTHER]
        rc, r = self.main()
        self.assertEqual(rc, 1)
        self.assertIn(f'head is {OTHER}', r['error'])
        self.assertNotIn(['pr', 'checks'], self.calls)

    def test_a_push_during_collection_is_refused(self):
        self.heads = [HEAD, OTHER]
        self.listings = [[self.check('hil', 'fail', JOB.format(3))]]
        rc, r = self.main()
        self.assertEqual(rc, 1)
        self.assertIn(f'head moved to {OTHER}', r['error'])

    def test_the_repo_is_passed_through_to_gh(self):
        self.heads = [HEAD]
        self.listings = [[]]
        with mock.patch.object(collect.subprocess, 'run', wraps=collect.subprocess.run) as spy:
            self.main()
        self.assertTrue(all(['--repo', 'o/r'] == c.args[0][4:6] for c in spy.call_args_list))

    def test_a_short_head_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as e, redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            collect.main(['inventory', '--repo', 'o/r', '--pr', '5', '--head', 'abc'])
        self.assertEqual(e.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
