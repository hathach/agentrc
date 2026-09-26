"""Tests for ci-rerun's collect.py against a fake gh."""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
                    {'headRefOid': head, 'baseRefName': 'master', 'baseRefOid': BASE}).encode())
            answer = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
            if isinstance(answer, tuple):
                return mock.Mock(returncode=answer[0], stdout=b'', stderr=answer[1].encode())
            return mock.Mock(returncode=0, stdout=json.dumps(answer).encode())
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
        self.assertEqual((rc, r['status'], r['pending']), (0, 'red', 0))
        self.assertEqual(sorted(r), ['checks', 'head', 'pending', 'status'], 'only what the workflow reads, and no null error')
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
        self.assertEqual((r['status'], self.clock[0]), ('red', 90))

    def test_stops_waiting_when_the_budget_is_spent(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]]
        rc, r = self.main('--wait-seconds', '70')
        self.assertEqual((rc, r['status'], r['pending'], r['checks']), (0, 'running', 1, []), 'pending checks are counted, not listed')
        self.assertEqual(self.calls.count(['pr', 'checks']), 3)

    def test_no_wait_budget_reports_what_stands(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]]
        rc, r = self.main()
        self.assertEqual((r['status'], self.clock[0], self.calls.count(['pr', 'checks'])), ('running', 0, 1))

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
        self.jobs = {'3': {'name': 'hil (x.json)', 'workflow_name': 'Build', 'run_id': 7, 'run_attempt': 2, 'head_sha': HEAD},
                     '40': {'name': 'hil (x.json)', 'workflow_name': 'Build', 'run_id': 70, 'head_sha': BASE}}
        self.logs = {'3': log(*STEP), '40': log(*STEP[:9], 'Total failed: 1', *STEP[11:])}
        self.base_runs = [{'databaseId': 71, 'headSha': 'd' * 40, 'status': 'in_progress'},
                          {'databaseId': 72, 'headSha': 'e' * 40, 'status': 'completed'},
                          {'databaseId': 70, 'headSha': BASE, 'status': 'completed'}]
        self.run_jobs = {'72': [{'name': 'hil (x.json)', 'conclusion': 'skipped', 'databaseId': 41}],
                         '70': [{'name': 'hil (x.json)', 'conclusion': 'failure', 'databaseId': 40}]}
        self.changed = ['src/host/msc.c', 'test/hil/tiny usb.json']
        self.calls = []

        def run(argv, **kw):
            self.calls.append(argv[1:])
            ok = lambda out: mock.Mock(returncode=0, stdout=out if isinstance(out, bytes) else (out if isinstance(out, str) else json.dumps(out)).encode(), stderr=b'')
            if argv[1:3] == ['pr', 'view']:
                return ok({'headRefOid': HEAD, 'baseRefName': 'master', 'baseRefOid': BASE})
            if argv[1:3] == ['pr', 'checks']:
                return ok(self.checks)
            if argv[1:3] == ['api', '--paginate']:
                if self.changed is None:
                    return mock.Mock(returncode=1, stdout=b'', stderr=b'HTTP 500')
                return ok([[{'filename': f, 'patch': '@@'} for f in self.changed]])
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

    def main(self, *links, extra=()):
        out = io.StringIO()
        argv = ['failures', '--repo', 'o/r', '--pr', '5', '--head', HEAD, *extra]
        for link in links:
            argv += ['--check', link]
        with redirect_stdout(out):
            rc = collect.main(argv)
        return rc, json.loads(out.getvalue())

    def entries(self, *links):
        rc, r = self.main(*links)
        self.assertEqual((rc, sorted(r)), (0, ['detail', 'head']), 'the evidence is in the detail file')
        return json.loads(Path(r['detail']).read_text())['checks']

    def test_actions_evidence_is_the_failed_steps_diagnostics_with_the_base_runs_shared_lines(self):
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['provider'], c['name'], c['error']), ('actions', 'hil (x.json)', None))
        self.assertNotIn('complete', c, 'the judge decides that')
        self.assertEqual((c['runId'], c['runAttempt']), (7, 2), 'a re-run shows in its attempt, so the judge re-runs once')
        self.assertEqual(c['firstError'], 'pico  host/msc  ...  Failed: /home/runner/work/r/r/src/host/msc.c timeout in 9.4s')
        self.assertEqual(c['files'], ['src/host/msc.c'])
        self.assertEqual({k: c['base'][k] for k in ('sha', 'runId', 'jobId', 'conclusion')},
                         {'sha': BASE, 'runId': 70, 'jobId': 40, 'conclusion': 'failure'})
        detail = c
        self.assertEqual(detail['diagnostics'], [STEP[10]], 'the cell rows are in cells, not listed twice')
        self.assertEqual(detail['signature'], 'pico  host/msc  ...  Failed: src/host/msc.c timeout')
        self.assertEqual(detail['base']['shared'], [], 'cells carry the comparison of rows')
        saved = Path(detail['log']).read_text()
        self.assertTrue(saved.startswith('##[group]Run build\n'), 'the whole job log, for the judge to search')
        self.assertNotIn('\x1b', saved)
        self.assertNotIn('\x00', saved)
        self.assertNotIn('2026-09-24T', saved)

    def test_every_failed_step_is_read_not_only_the_first(self):
        self.logs['3'] = log('##[group]Run lint', '##[endgroup]', 'lint.py:3: error: bad', '##[error]Process completed with exit code 1.',
                             *STEP)
        detail = self.entries(JOB.format(3))[0]
        self.assertEqual(detail['firstError'], 'lint.py:3: error: bad')
        self.assertEqual(detail['diagnostics'], ['lint.py:3: error: bad', STEP[10]])

    def test_an_action_step_error_after_a_failed_shell_step_is_read_too(self):
        self.logs['3'] = log(*STEP[:12], '##[group]Run actions/upload-artifact@v7', '##[endgroup]',
                             '##[error]No files were found with the provided path: report.json')
        detail = self.entries(JOB.format(3))[0]
        self.assertEqual(detail['diagnostics'], [STEP[10], 'No files were found with the provided path: report.json'])

    def test_each_call_writes_its_own_detail_file(self):
        first, second = self.main(JOB.format(3))[1]['detail'], self.main(RTD.format(9))[1]['detail']
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(Path(first).read_text())['checks'][0]['provider'], 'actions')

    def test_the_base_run_is_the_newest_completed_one_in_which_the_job_ran(self):
        self.main(JOB.format(3))
        self.assertEqual([c[2] for c in self.calls if c[:2] == ['run', 'view']], ['72', '70'])

    def test_a_base_run_that_passed_carries_no_lines(self):
        self.run_jobs['70'][0]['conclusion'] = 'success'
        base = self.entries(JOB.format(3))[0]['base']
        self.assertEqual(sorted(base), ['conclusion', 'jobId', 'log', 'runId', 'sha'], 'its log only, to compare HIL cells')

    def test_hil_cells_are_the_terminal_failed_rows_each_placed_against_the_base_row(self):
        c = self.entries(JOB.format(3))[0]
        self.assertEqual([x['baseLineNo'] for x in c['cells']], [9, None], 'where the base log shows the cell')
        self.assertEqual([(x['cell'], x['signature'], x['lineNo'], x['files'], x['onBase'], x['baseLine'], 'prior' in x)
                          for x in c['cells']],
                         [('pico host/msc', 'pico host/msc: Failed: src/host/msc.c timeout', 9, ['src/host/msc.c'],
                           'same-failure', STEP[8], False),
                          ('rp2 device/hid', 'rp2 device/hid: Failed: not enumerated', 10, [], 'not-run', None, False)])
        self.assertNotIn('cellsError', c)

    def test_a_cell_reads_other_failure_passed_or_other_on_base(self):
        self.logs['40'] = log('pico  host/msc  ...  Failed: stall  I (24) boot: compile time 07:16  in 1.0s', 'rp2  device/hid  ...  OK in 1.0s')
        self.assertEqual([(x['onBase'], x['baseLine']) for x in self.entries(JOB.format(3))[0]['cells']],
                         [('other-failure', 'pico  host/msc  ...  Failed: stall  I (24) boot: compile time 07:16  in 1.0s'),
                          ('passed', 'rp2  device/hid  ...  OK in 1.0s')])
        self.logs['40'] = log('pico  host/msc  ...  Skipped: no board', 'rp2  device/hid  ...  Failed: not enumerated  I (24) boot: compile time 07:16  in 5.0s')
        self.assertEqual([x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']], ['other', 'same-failure'])

    def test_without_a_comparable_base_run_a_cell_is_not_placed(self):
        self.run_jobs['70'][0]['conclusion'] = 'skipped'
        self.assertEqual({x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']}, {None})

    def test_a_retried_cell_counts_by_its_last_row_and_flash_failures_lose_the_command(self):
        self.logs['3'] = log('pico  host/msc  ...  Failed: timeout in 9.4s', 'pico  host/msc  ...  OK in 3.0s',
                             'rp2  device/hid  ...  Flash Failed: probe lost  COMMAND FAILED: openocd -f x.cfg',
                             'rp2  device/cdc  ...  Skipped: no board', '##[error]Process completed with exit code 1.')
        self.assertEqual([(x['cell'], x['signature']) for x in self.entries(JOB.format(3))[0]['cells']],
                         [('rp2 device/hid', 'rp2 device/hid: Flash Failed: probe lost')])

    def test_a_log_without_hil_rows_has_no_cells_and_leaves_a_passed_base_unread(self):
        self.logs['3'] = log('##[group]Run make', 'src/a.c:1: error: x', '##[error]Process completed with exit code 2.')
        self.run_jobs['70'][0]['conclusion'] = 'success'
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['cells'], 'log' in c['base']), ([], False))

    def test_a_profiled_row_is_a_cell_and_an_unparsed_runner_step_says_so(self):
        self.logs['3'] = log(*STEP[:8], '1790335968.123 pico  host/msc  ...  Failed: timeout in 9.4s', *STEP[10:])
        self.assertEqual([x['cell'] for x in self.entries(JOB.format(3))[0]['cells']], ['pico host/msc'], 'HIL_PROFILE=1 prefixes epoch seconds')
        self.logs['3'] = log(*STEP[:7], 'Traceback (most recent call last):', 'RuntimeError: no boards', *STEP[11:])
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['cells'], c['cellsError']), ([], 'a failed hil_test.py step printed no result row this reads; read its log'))

    def test_changed_paths_are_read_for_any_readable_check_and_their_failure_is_named(self):
        rc, r = self.main('https://greptile.com/')
        self.assertNotIn('changed', json.loads(Path(r['detail']).read_text()), 'no check could be read')
        rc, r = self.main(RTD.format(9))
        self.assertEqual(json.loads(Path(r['detail']).read_text())['changed'], self.changed, 'a readable check, even one naming no file')
        self.changed = None
        rc, r = self.main(JOB.format(3))
        got = json.loads(Path(r['detail']).read_text())
        self.assertEqual((rc, got['changed']), (0, None), 'the evidence survives')
        self.assertIn('could not be read', got['changedError'])
        self.changed = [f'f{i}.c' for i in range(3000)]
        rc, r = self.main(JOB.format(3))
        got = json.loads(Path(r['detail']).read_text())
        self.assertEqual(got['changed'], None)
        self.assertIn('may be incomplete', got['changedError'])

    def test_a_head_that_moves_while_paths_are_read_is_refused(self):
        views = iter([HEAD, HEAD, 'f' * 40])  # inventory's two reads, then the one after the paths
        real = collect.subprocess.run
        def moving(argv, **kw):
            if argv[1:3] == ['pr', 'view']:
                return mock.Mock(returncode=0, stdout=json.dumps({'headRefOid': next(views), 'baseRefName': 'master', 'baseRefOid': BASE}).encode(), stderr=b'')
            return real(argv, **kw)
        with mock.patch.object(collect.subprocess, 'run', moving), redirect_stdout(io.StringIO()) as out:
            rc = collect.main(['failures', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(3)])
        self.assertIn('moved', out.getvalue())
        self.assertNotEqual(rc, 0)

    def test_the_detail_lists_the_prs_changed_paths(self):
        rc, r = self.main(JOB.format(3))
        self.assertEqual(json.loads(Path(r['detail']).read_text())['changed'], ['src/host/msc.c', 'test/hil/tiny usb.json'], 'a path with a space stays one path')

    def remember_on(self, head, failures):
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps([{'link': JOB.format(3), 'bucket': 'fail', 'failures': failures}]))):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(collect.main(['remember', '--repo', 'o/r', '--pr', '5', '--head', head]), 0)

    def test_a_prior_verdict_shows_beside_the_cell_it_matches_by_check_cell_and_signature(self):
        judged = {'check': 'hil (x.json)', 'cell': 'pico host/msc', 'signature': 'pico host/msc: Failed: src/host/msc.c timeout',
                  'verdict': 'rig-side', 'firstError': 'probe'}
        self.remember_on(OTHER, [judged, {**judged, 'cell': 'rp2 device/hid', 'signature': 'something else'}])
        rc, r = self.main(JOB.format(3), extra=('--prior-head', OTHER))
        cells = json.loads(Path(r['detail']).read_text())['checks'][0]['cells']
        self.assertEqual([x.get('prior') for x in cells], [[{'head': OTHER, 'verdict': 'rig-side', 'firstError': 'probe'}], None])
        self.assertNotIn('prior', self.entries(JOB.format(3))[0]['cells'][0], 'only when the caller names the head')
        self.remember_on(OTHER, [judged, {**judged, 'verdict': 'real'}])
        rc, r = self.main(JOB.format(3), extra=('--prior-head', OTHER))
        self.assertEqual([v['verdict'] for v in json.loads(Path(r['detail']).read_text())['checks'][0]['cells'][0]['prior']],
                         ['rig-side', 'real'], 'both, for the judge to weigh: ambiguous')

    def test_prior_head_is_a_full_sha_other_than_the_head_and_only_for_failures(self):
        for extra in (['--prior-head', HEAD], ['--prior-head', 'b' * 7]):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                self.main(JOB.format(3), extra=extra)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            collect.main(['inventory', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--prior-head', OTHER])

    def test_read_the_docs_evidence_is_the_failed_commands_last_line(self):
        c = self.entries(RTD.format(9))[0]
        self.assertEqual((c['provider'], c['firstError'], c['base'], c['error']),
                         ('readthedocs', 'fatal: reference is not a tree: abc', None, None))

    def test_a_link_without_a_run_is_an_entry_error_not_a_guess(self):
        c = self.entries('https://greptile.com/')[0]
        self.assertEqual((c['provider'], c['error']), ('other', 'no reader for this check: its link names no run'))

    def test_a_job_from_another_head_is_refused(self):
        self.jobs['3']['head_sha'] = OTHER
        c = self.entries(JOB.format(3))[0]
        self.assertIn(f'ran on {OTHER}', c['error'])

    def test_a_check_no_longer_failing_is_stale_and_nothing_is_read(self):
        self.checks[0]['bucket'] = 'pass'
        rc, r = self.main(JOB.format(3), RTD.format(9))
        self.assertEqual(rc, 1)
        self.assertIn(f'stale snapshot: no longer failing checks of the head: {JOB.format(3)}', r['error'])
        self.assertFalse([c for c in self.calls if c[0] == 'api'])

    def test_jobs_of_one_workflow_share_one_base_run_lookup(self):
        self.checks.append({'name': 'build (x)', 'workflow': 'Build', 'bucket': 'fail', 'link': JOB.format(4)})
        self.jobs['4'] = {**self.jobs['3'], 'name': 'build (x)'}
        self.logs['4'] = self.logs['3']
        self.run_jobs['70'].append({'name': 'build (x)', 'conclusion': 'success', 'databaseId': 42})
        self.logs['42'] = log('pico  host/msc  ...  OK in 1.0s')
        self.entries(JOB.format(3), JOB.format(4))
        self.assertEqual(sum(c[:2] == ['run', 'list'] for c in self.calls), 1)
        self.assertEqual([c[2] for c in self.calls if c[:2] == ['run', 'view']], ['72', '70'], 'each run is read once')

    def test_a_log_that_is_not_utf8_is_still_read(self):
        self.logs['3'] = log(*STEP).encode().replace(b'timeout', b'time\xffout')
        c = self.entries(JOB.format(3))[0]
        self.assertEqual(c['error'], None)
        self.assertIn('time\ufffdout', c['firstError'])

    def test_a_workflow_named_like_a_cache_key_keeps_its_own_base_runs(self):
        for key in ('token', 'runs', 'rtd-token'):
            self.jobs['3']['workflow_name'] = key
            self.assertEqual(self.entries(RTD.format(9), JOB.format(3))[1]['base']['runId'], 70, key)

    def test_the_read_the_docs_token_is_read_once_and_only_when_needed(self):
        with mock.patch.object(collect.rtd, 'token', side_effect=lambda: 'k') as token:
            self.entries(JOB.format(3))
            self.assertEqual(token.call_count, 0)
            self.checks.append({'name': 'docs2', 'workflow': '', 'bucket': 'fail', 'link': RTD.format(10)})
            self.entries(RTD.format(9), RTD.format(10))
            self.assertEqual(token.call_count, 1)

    def test_failures_needs_a_check(self):
        with self.assertRaises(SystemExit), mock.patch('sys.stderr', io.StringIO()):
            collect.main(['failures', '--repo', 'o/r', '--pr', '5', '--head', HEAD])


class VerdictsTest(unittest.TestCase):
    ENTRY = {'link': JOB.format(3), 'bucket': 'fail', 'failures': [
        {'check': 'hil', 'workflow': 'Build', 'job': 'hil', 'cell': 'pico → rp2040', 'signature': 'a "quoted"\ttab\x01ctl\x7f',
         'runId': 7, 'complete': True, 'firstError': 'line\nbreak 😀 Љ', 'files': ['src/a.c'], 'verdict': 'rig-side'}]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(collect.tempfile, 'gettempdir', lambda: self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, command, *argv, stdin=''):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch('sys.stdin', io.StringIO(stdin)):
            rc = collect.main([command, '--repo', 'o/r', '--pr', '5', '--head', HEAD, *argv])
        return rc, json.loads(out.getvalue())

    def test_recall_returns_what_was_remembered_unchanged_and_skips_unknown_links(self):
        self.assertEqual(self.main('remember', stdin=json.dumps([self.ENTRY])), (0, {'head': HEAD}))
        rc, r = self.main('recall', '--check', JOB.format(3), '--check', JOB.format(4))
        self.assertEqual((rc, r['verdicts']), (0, [self.ENTRY]))

    def test_a_later_verdict_for_a_link_replaces_the_earlier(self):
        self.main('remember', stdin=json.dumps([self.ENTRY]))
        other = {**self.ENTRY, 'link': JOB.format(4)}
        self.main('remember', stdin=json.dumps([{**self.ENTRY, 'bucket': 'cancel'}, other]))
        verdicts = self.main('recall', '--check', JOB.format(3), '--check', JOB.format(4))[1]['verdicts']
        self.assertEqual([(v['link'], v['bucket']) for v in verdicts], [(JOB.format(3), 'cancel'), (JOB.format(4), 'fail')])

    def test_nothing_remembered_recalls_nothing(self):
        self.assertEqual(self.main('recall', '--check', JOB.format(3)), (0, {'head': HEAD, 'verdicts': []}))

    def test_remember_refuses_what_is_not_a_list_of_verdicts(self):
        for text in ('not json', json.dumps({'link': 'x'}), json.dumps([{'link': 'x', 'bucket': 'fail'}])):
            rc, r = self.main('remember', stdin=text)
            self.assertEqual(rc, 1, text)
            self.assertIn('verdicts on stdin', r['error'])


if __name__ == '__main__':
    unittest.main()
