"""Tests for ci-rerun's collect.py against a fake gh."""
import importlib.util
import io
import json
import tempfile
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


def log(*lines):
    """An Actions job log as the API returns it: timestamps, ANSI colour, a NUL per line."""
    return ''.join(f'2026-09-24T17:35:56.3154101Z \x1b[36m{line}\x1b[0m\x00\n' for line in lines)


STEP = ['##[group]Run build', 'Build Summary: 1 OK, 0 Failed', '##[endgroup]', 'built',
        '##[group]Run python3 test/hil/hil_test.py', '  python3 test/hil/hil_test.py $ARGS', '##[endgroup]',
        'pico  device/cdc  ...  OK in 3.1s',
        'pico  host/msc  ...  Failed: /home/runner/work/r/r/src/host/msc.c timeout in 9.4s',
        'rp2  device/hid  ...  Failed: not enumerated in 2.0s', 'Total failed: 2',
        '##[error]Process completed with exit code 1.', '##[group]Run upload', '##[endgroup]']


class FailuresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checks = [{'name': 'hil (x.json)', 'workflow': 'Build', 'bucket': 'fail', 'link': JOB.format(3)},
                       {'name': 'docs', 'workflow': '', 'bucket': 'fail', 'link': RTD.format(9)},
                       {'name': 'bot', 'workflow': '', 'bucket': 'fail', 'link': 'https://greptile.com/'}]
        self.jobs = {'3': {'name': 'hil (x.json)', 'workflow_name': 'Build', 'run_id': 7, 'head_sha': HEAD},
                     '40': {'name': 'hil (x.json)', 'workflow_name': 'Build', 'run_id': 70, 'head_sha': BASE}}
        self.logs = {'3': log(*STEP), '40': log(*STEP[:9], 'Total failed: 1', *STEP[11:])}
        self.base_runs = [{'databaseId': 71, 'headSha': 'd' * 40, 'status': 'in_progress'},
                          {'databaseId': 72, 'headSha': 'e' * 40, 'status': 'completed'},
                          {'databaseId': 70, 'headSha': BASE, 'status': 'completed'}]
        self.run_jobs = {'72': [{'name': 'hil (x.json)', 'conclusion': 'skipped', 'databaseId': 41}],
                         '70': [{'name': 'hil (x.json)', 'conclusion': 'failure', 'databaseId': 40}]}
        self.calls = []

        def run(argv, **kw):
            self.calls.append(argv[1:])
            ok = lambda out: mock.Mock(returncode=0, stdout=out if isinstance(out, str) else json.dumps(out), stderr='')
            if argv[1:3] == ['pr', 'view']:
                return ok({'headRefOid': HEAD, 'baseRefName': 'master', 'baseRefOid': BASE})
            if argv[1:3] == ['pr', 'checks']:
                return ok(self.checks)
            if argv[1:3] == ['run', 'list']:
                return ok(self.base_runs)
            if argv[1:3] == ['run', 'view']:
                return ok({'jobs': self.run_jobs[argv[3]]})
            path = argv[2]
            job = path.split('/jobs/')[1].split('/')[0]
            return ok(self.logs[job]) if path.endswith('/logs') else ok(self.jobs[job])
        for obj, name, fake in ((collect.subprocess, 'run', run), (collect.tempfile, 'gettempdir', lambda: self.tmp.name),
                                (collect.rtd, 'token', lambda: 'k'),
                                (collect.rtd, 'reason', lambda url, key: (9, {'error': ''}, [('error', 'Error while checking out', 'x')],
                                                                          [(128, 'git checkout --force abc', 'fetching\nfatal: reference is not a tree: abc')]))):
            patcher = mock.patch.object(obj, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, *links):
        out = io.StringIO()
        argv = ['failures', '--repo', 'o/r', '--pr', '5', '--head', HEAD]
        for link in links:
            argv += ['--check', link]
        with redirect_stdout(out):
            rc = collect.main(argv)
        return rc, json.loads(out.getvalue())

    def test_actions_evidence_is_the_failed_steps_diagnostics_with_the_base_runs_shared_lines(self):
        rc, r = self.main(JOB.format(3))
        self.assertEqual(rc, 0)
        c = r['checks'][0]
        self.assertEqual((c['provider'], c['name'], c['complete'], c['error']), ('actions', 'hil (x.json)', False, None))
        self.assertEqual(c['firstError'], 'pico  host/msc  ...  Failed: /home/runner/work/r/r/src/host/msc.c timeout in 9.4s')
        self.assertEqual(c['files'], ['src/host/msc.c'])
        self.assertEqual(c['base'], {'sha': BASE, 'runId': 70, 'jobId': 40, 'conclusion': 'failure', 'shared': 1})
        detail = json.loads(Path(r['detail']).read_text())['checks'][0]
        self.assertEqual(detail['diagnostics'], [STEP[8], STEP[9], STEP[10]])
        self.assertEqual(detail['signature'], 'pico  host/msc  ...  Failed: src/host/msc.c timeout')
        self.assertEqual(detail['base']['shared'], [STEP[8]])
        saved = Path(detail['log']).read_text()
        self.assertTrue(saved.startswith('##[group]Run build\n'), 'the whole job log, for the judge to search')
        self.assertNotIn('\x1b', saved)
        self.assertNotIn('\x00', saved)
        self.assertNotIn('2026-09-24T', saved)

    def test_every_failed_step_is_read_not_only_the_first(self):
        self.logs['3'] = log('##[group]Run lint', '##[endgroup]', 'lint.py:3: error: bad', '##[error]Process completed with exit code 1.',
                             *STEP)
        rc, r = self.main(JOB.format(3))
        detail = json.loads(Path(r['detail']).read_text())['checks'][0]
        self.assertEqual(r['checks'][0]['firstError'], 'lint.py:3: error: bad')
        self.assertEqual(detail['diagnostics'], ['lint.py:3: error: bad', STEP[8], STEP[9], STEP[10]])

    def test_an_action_step_error_after_a_failed_shell_step_is_read_too(self):
        self.logs['3'] = log(*STEP[:12], '##[group]Run actions/upload-artifact@v7', '##[endgroup]',
                             '##[error]No files were found with the provided path: report.json')
        detail = json.loads(Path(self.main(JOB.format(3))[1]['detail']).read_text())['checks'][0]
        self.assertEqual(detail['diagnostics'], [STEP[8], STEP[9], STEP[10],
                                                 'No files were found with the provided path: report.json'])

    def test_each_call_writes_its_own_detail_file(self):
        first, second = self.main(JOB.format(3))[1]['detail'], self.main(RTD.format(9))[1]['detail']
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(Path(first).read_text())['checks'][0]['provider'], 'actions')

    def test_the_base_run_is_the_newest_completed_one_in_which_the_job_ran(self):
        self.main(JOB.format(3))
        self.assertEqual([c[2] for c in self.calls if c[:2] == ['run', 'view']], ['72', '70'])

    def test_a_base_run_that_passed_carries_no_lines(self):
        self.run_jobs['70'][0]['conclusion'] = 'success'
        base = self.main(JOB.format(3))[1]['checks'][0]['base']
        self.assertEqual(base, {'sha': BASE, 'runId': 70, 'jobId': 40, 'conclusion': 'success'})

    def test_read_the_docs_evidence_is_the_failed_commands_last_line(self):
        c = self.main(RTD.format(9))[1]['checks'][0]
        self.assertEqual((c['provider'], c['firstError'], c['base'], c['error']),
                         ('readthedocs', 'fatal: reference is not a tree: abc', None, None))

    def test_a_link_without_a_run_is_an_entry_error_not_a_guess(self):
        c = self.main('https://greptile.com/')[1]['checks'][0]
        self.assertEqual((c['provider'], c['error']), ('other', 'no reader for this check: its link names no run'))

    def test_a_job_from_another_head_is_refused(self):
        self.jobs['3']['head_sha'] = OTHER
        c = self.main(JOB.format(3))[1]['checks'][0]
        self.assertIn(f'ran on {OTHER}', c['error'])

    def test_a_check_no_longer_failing_is_stale_and_nothing_is_read(self):
        self.checks[0]['bucket'] = 'pass'
        rc, r = self.main(JOB.format(3), RTD.format(9))
        self.assertEqual((rc, r['stale']), (1, [JOB.format(3)]))
        self.assertFalse([c for c in self.calls if c[0] == 'api'])

    def test_failures_needs_a_check(self):
        with self.assertRaises(SystemExit), mock.patch('sys.stderr', io.StringIO()):
            collect.main(['failures', '--repo', 'o/r', '--pr', '5', '--head', HEAD])


if __name__ == '__main__':
    unittest.main()
