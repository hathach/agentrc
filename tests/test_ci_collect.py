"""Tests for ci-rerun's collect.py against a fake gh."""
import importlib.util
import io
import json
import os
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
_facts_spec = importlib.util.spec_from_file_location(
    'pr_babysit_facts', Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'facts.py')
FACTS = importlib.util.module_from_spec(_facts_spec)
_facts_spec.loader.exec_module(FACTS)

HEAD, OTHER, BASE = 'a' * 40, 'b' * 40, 'c' * 40
JOB = 'https://github.com/o/r/actions/runs/7/job/{}'
RTD = 'https://app.readthedocs.org/projects/p/builds/{}/'
CIRCLE = 'https://circleci.com/gh/o/r/{}'



def unsealed(text):
    """The printed line without its seal, which must be facts.py's over the rest; an error line has none."""
    line = json.loads(text)
    if 'error' in line:
        assert 'seal' not in line, line
        return line
    rest = {k: v for k, v in line.items() if k != 'seal'}
    assert line['seal'] == FACTS.sealed(rest)['seal'], line
    return rest

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
        return rc, unsealed(out.getvalue())

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
        return rc, unsealed(out.getvalue())

    def entries(self, *links):
        rc, r = self.main(*links)
        self.assertEqual((rc, sorted(r), r['gates']), (0, ['bases', 'detail', 'gates', 'head'], []), 'the evidence is in the detail file')
        checks = json.loads(Path(r['detail']).read_text())['checks']
        self.assertEqual(r['bases'], [{'link': c['link'], 'base': collect.base_token(c.get('base'))} for c in checks], 'each check\'s base job')
        return checks

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

    def cell_priors(self):
        return [x.get('prior') for x in self.entries(JOB.format(3))[0]['cells']]

    def test_a_prior_verdict_shows_beside_the_cell_it_matches_by_workflow_check_cell_and_signature(self):
        judged = {'workflow': 'Build', 'check': 'hil (x.json)', 'cell': 'pico host/msc', 'signature': 'pico host/msc: Failed: src/host/msc.c timeout',
                  'verdict': 'rig-side', 'firstError': 'probe'}
        self.assertEqual(self.cell_priors(), [None, None], 'nothing stored yet')
        self.remember_on(OTHER, [judged, {**judged, 'cell': 'rp2 device/hid', 'signature': 'something else'}])
        self.remember_on(HEAD, [{**judged, 'verdict': 'real'}])
        self.assertEqual(self.cell_priors(), [[{'head': OTHER, 'verdict': 'rig-side', 'firstError': 'probe'}], None], 'another head\'s, never its own')
        self.remember_on(OTHER, [{**judged, 'workflow': 'Nightly'}])
        self.assertEqual(self.cell_priors()[0], None, 'another workflow\'s job of the same name')
        self.remember_on(OTHER, [judged, {**judged, 'verdict': 'real'}])
        self.assertEqual([v['verdict'] for v in self.cell_priors()[0]], ['rig-side', 'real'], 'both, for the judge to weigh: ambiguous')
        self.remember_on(OTHER, [{**judged, 'workflow': ''}])
        self.jobs['3']['workflow_name'] = ''
        self.assertEqual(self.cell_priors()[0], None, 'no workflow name on either side matches nothing')

    def test_each_cell_takes_the_newest_other_head_that_judged_it(self):
        first, second = ({'workflow': 'Build', 'check': 'hil (x.json)', 'cell': c['cell'], 'signature': c['signature'], 'verdict': 'rig-side', 'firstError': 'old'}
                         for c in self.entries(JOB.format(3))[0]['cells'])
        newer = 'c' * 40
        self.remember_on(OTHER, [first, second])
        self.remember_on(newer, [{**first, 'verdict': 'real', 'firstError': 'new'}])
        os.utime(collect.evidence_dir('o/r', 5, OTHER) / 'verdicts.json', (1, 1))
        self.assertEqual([[(p['head'], p['firstError']) for p in x] for x in self.cell_priors()], [[(newer, 'new')], [(OTHER, 'old')]],
                         'a head judged in part leaves the rest to an older one')

    def test_a_base_job_rerun_in_the_same_run_is_another_base(self):
        base = {'runId': 70, 'jobId': 40, 'conclusion': 'failure'}
        tokens = [collect.base_token(b) for b in (base, {**base, 'jobId': 41}, {**base, 'conclusion': 'success'}, None)]
        self.assertEqual(tokens, ['70:40:failure', '70:41:failure', '70:40:success', None])

    def test_bases_prints_the_tokens_failures_prints_without_reading_a_log(self):
        rc, r = self.main(JOB.format(3), RTD.format(9))
        out = io.StringIO()
        with mock.patch.object(collect, 'actions_log', side_effect=AssertionError('a log was read')), redirect_stdout(out):
            rc = collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(3), '--check', RTD.format(9)])
        self.assertEqual((rc, unsealed(out.getvalue())), (0, {'head': HEAD, 'bases': r['bases']}))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(99)]), 1)
        self.assertIn('stale snapshot', json.loads(out.getvalue())['error'])

    def test_bases_refuses_a_job_of_another_head_and_a_head_that_moved(self):
        def bases():
            out = io.StringIO()
            with redirect_stdout(out):
                rc = collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(3)])
            return rc, json.loads(out.getvalue())['error']
        self.jobs['3']['head_sha'] = OTHER
        rc, error = bases()
        self.assertEqual(rc, 1)
        self.assertIn(f'ran on {OTHER}', error)
        self.jobs['3']['head_sha'] = HEAD
        pull, seen = collect.pull, []
        def moving(repo, pr):
            seen.append(pr)
            return {**pull(repo, pr), **({'headRefOid': OTHER} if len(seen) > 2 else {})}
        with mock.patch.object(collect, 'pull', moving):
            rc, error = bases()
        self.assertEqual((rc, len(seen)), (1, 3), 'read again after the base lookups')
        self.assertIn(f'head moved to {OTHER}', error)

    def test_an_unreadable_store_of_another_head_is_named_and_left_out(self):
        judged = {'workflow': 'Build', 'check': 'hil (x.json)', 'cell': 'pico host/msc', 'signature': 'pico host/msc: Failed: src/host/msc.c timeout',
                  'verdict': 'rig-side', 'firstError': 'probe'}
        self.remember_on(OTHER, [judged])
        for garbled in ('{', '{"x": 7}'):
            (collect.evidence_dir('o/r', 5, 'c' * 40) / 'verdicts.json').write_text(garbled)
            rc, r = self.main(JOB.format(3))
            detail = json.loads(Path(r['detail']).read_text())
            self.assertEqual(rc, 0)
            self.assertEqual([e.split(':')[0] for e in detail['priorErrors']], ['c' * 40])
            self.assertEqual(detail['checks'][0]['cells'][0]['prior'][0]['head'], OTHER)
        self.assertNotIn('priorErrors', json.loads(Path(self.main(RTD.format(9))[1]['detail']).read_text()), 'no cells, no store read')
        denied = collect.evidence_dir('o/r', 5, 'c' * 40) / 'verdicts.json'
        read, stat = collect.stored_verdicts, Path.stat
        for patch in (mock.patch.object(collect, 'stored_verdicts', lambda folder: (_ for _ in ()).throw(PermissionError('denied')) if folder == denied.parent else read(folder)),
                      mock.patch.object(Path, 'stat', lambda path, **kw: (_ for _ in ()).throw(PermissionError('denied')) if path == denied else stat(path, **kw))):
            with patch:
                rc, r = self.main(JOB.format(3))
            detail = json.loads(Path(r['detail']).read_text())
            self.assertEqual((rc, detail['priorErrors'], detail['checks'][0]['cells'][0]['prior'][0]['head']), (0, [f'{"c" * 40}: denied'], OTHER))

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

    SONAR = 'https://sonarcloud.io/dashboard?id=o_r&pullRequest=5'

    def gates(self, gate, link=SONAR, token='', check=()):
        """failures --gate for a red check at `link`, SonarCloud answering `gate` (or raising it)."""
        self.checks.append({'name': 'SonarCloud', 'workflow': '', 'bucket': 'fail', 'link': link})
        sent = []

        class Opener:
            def open(self, req, timeout):
                sent.append(req)
                if isinstance(gate, Exception):
                    raise gate
                return mock.MagicMock(**{'__enter__.return_value.read.return_value': json.dumps({'projectStatus': gate}).encode()})
        with mock.patch.object(collect.rtd, 'OPENER', Opener()), mock.patch.dict(collect.os.environ, {'SONAR_TOKEN': token}):
            rc, r = self.main(*check, extra=['--gate', link])
        self.assertEqual(rc, 0, r)
        return r, sent

    def only(self, r, complete=False):
        """The one failure of the one gate, checked for its completeness and a signature equal to its text."""
        [g] = r['gates']
        [f] = g['failures']
        self.assertEqual((f['complete'], f['signature']), (complete, f['firstError']))
        return f['firstError']

    def condition(self, **over):
        return {'status': 'ERROR', 'metricKey': 'new_security_rating', 'comparator': 'GT', 'errorThreshold': '1', 'actualValue': '3', **over}

    def test_each_failing_gate_condition_is_its_own_complete_failure_outside_the_detail_file(self):
        r, sent = self.gates({'status': 'ERROR', 'conditions': [
            self.condition(status='OK', metricKey='new_coverage', comparator='LT', errorThreshold='80', actualValue='90'),
            self.condition(),
            self.condition(metricKey='new_duplicated_lines_density', errorThreshold='3', actualValue='5.2')]},
            check=(JOB.format(3),))
        lines = ['condition failed: new_security_rating 3 > 1', 'condition failed: new_duplicated_lines_density 5.2 > 3']
        self.assertEqual(r['gates'], [{'link': self.SONAR, 'failures': [{'firstError': x, 'signature': x, 'complete': True} for x in lines]}])
        self.assertEqual([c['link'] for c in json.loads(Path(r['detail']).read_text())['checks']], [JOB.format(3)], 'the judge reads only its checks')
        self.assertEqual(sent[0].full_url, 'https://sonarcloud.io/api/qualitygates/project_status?projectKey=o_r&pullRequest=5')
        self.assertIsNone(sent[0].get_header('Authorization'), 'no token, no header')

    def test_a_gate_passing_now_is_one_incomplete_failure(self):
        r, _ = self.gates({'status': 'OK', 'conditions': []})
        self.assertEqual(self.only(r), 'quality gate OK now, though its check failed: SonarCloud may have analysed again since')

    def test_an_error_gate_listing_no_condition_is_one_incomplete_failure_and_the_token_goes_to_sonarcloud(self):
        r, sent = self.gates({'status': 'ERROR', 'conditions': []}, token='t')
        self.assertEqual(sent[0].get_header('Authorization'), 'Basic dDo=')
        self.assertEqual(self.only(r), 'quality gate ERROR with no failing condition listed')

    def test_a_condition_missing_a_field_is_not_read(self):
        r, _ = self.gates({'status': 'ERROR', 'conditions': [self.condition(), self.condition(errorThreshold=None)]})
        self.assertEqual(self.only(r), 'SonarCloud gate not read: SonarCloud quality gate of o_r PR #5 lists a failing condition without its metricKey, actualValue, comparator, errorThreshold')

    def test_a_malformed_answer_is_a_gate_not_read(self):
        for gate in (None, {'status': 'ERROR', 'conditions': 'x'}, {'status': 'ERROR', 'conditions': [None]}):
            r, _ = self.gates(gate)
            self.assertRegex(self.only(r), r'^SonarCloud gate not read: SonarCloud quality gate of o_r PR #5: unexpected answer ', gate)

    def test_a_gate_not_in_error_with_failing_conditions_is_not_read(self):
        r, _ = self.gates({'conditions': [self.condition()]})
        self.assertEqual(self.only(r), 'SonarCloud gate not read: SonarCloud quality gate of o_r PR #5 is None with failing conditions listed')

    def test_a_check_run_link_names_no_gate_to_read(self):
        link = 'https://github.com/o/r/runs/8'
        r, sent = self.gates({'status': 'ERROR', 'conditions': []}, link)
        self.assertEqual(sent, [])
        self.assertEqual(r['gates'][0]['link'], link)
        self.assertEqual(self.only(r), f'SonarCloud gate not read: not a SonarCloud link naming one project and PR #5: {link}')

    def test_a_dashboard_link_for_another_pr_is_not_read(self):
        r, sent = self.gates({'status': 'ERROR', 'conditions': []}, 'https://sonarcloud.io/dashboard?id=o_r&pullRequest=6')
        self.assertEqual(sent, [])
        self.assertRegex(self.only(r), r'^SonarCloud gate not read: not a SonarCloud link naming one project and PR #5')

    def test_an_unreachable_sonarcloud_is_a_gate_not_read(self):
        r, _ = self.gates(collect.urllib.error.URLError('timed out'))
        self.assertEqual(self.only(r), 'SonarCloud gate not read: SonarCloud quality gate of o_r PR #5: <urlopen error timed out>')

    def test_gates_alone_write_an_empty_detail_file_read_no_changed_paths_and_check_the_head_again(self):
        r, _ = self.gates({'status': 'ERROR', 'conditions': []})
        self.assertEqual(json.loads(Path(r['detail']).read_text()), {'head': HEAD, 'baseRef': 'master', 'checks': []})
        self.assertFalse([c for c in self.calls if c[:2] == ['api', '--paginate']])
        self.assertEqual(sum(c[:2] == ['pr', 'view'] for c in self.calls), 3, 'inventory reads the head twice, the gates once more')

    def test_a_gate_no_longer_failing_is_stale(self):
        self.checks.append({'name': 'SonarCloud', 'workflow': '', 'bucket': 'pass', 'link': self.SONAR})
        rc, r = self.main(extra=['--gate', self.SONAR])
        self.assertEqual(rc, 1)
        self.assertIn(f'stale snapshot: no longer failing checks of the head: {self.SONAR}', r['error'])

    def test_a_sonarcloud_link_as_a_check_has_no_reader(self):
        self.checks.append({'name': 'SonarCloud', 'workflow': '', 'bucket': 'fail', 'link': self.SONAR})
        c = self.entries(self.SONAR)[0]
        self.assertEqual((c['provider'], c['error']), ('other', 'no reader for this check: its link names no run'))

    def test_gate_is_for_failures_and_takes_each_link_once(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            collect.main(['recall', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--gate', self.SONAR])
        for links, extra in (((self.SONAR,), ['--gate', self.SONAR]), ((), ['--gate', self.SONAR, '--gate', self.SONAR])):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                self.main(*links, extra=extra)

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
        self.printed = out.getvalue()
        return rc, unsealed(self.printed)

    def entry(self, n, size, link=JOB):
        return {**self.ENTRY, 'link': link.format(n), 'failures': [{**self.ENTRY['failures'][0], 'firstError': 'x' * size}]}

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

    def test_a_patch_replaces_the_stored_failures_of_its_keys_in_place_or_stores_nothing(self):
        first = self.ENTRY['failures'][0]
        entry = {**self.ENTRY, 'failures': [first, {**first, 'cell': 'b', 'verdict': 'unclassified'}, {**first, 'cell': 'c'}]}
        self.main('remember', stdin=json.dumps([entry]))
        placed = {**first, 'cell': 'b', 'verdict': 'rig-side', 'firstError': 'placed'}
        self.assertEqual(self.main('remember', stdin=json.dumps([{'link': JOB.format(3), 'bucket': 'fail', 'patch': [placed]}])), (0, {'head': HEAD}))
        recalled = self.main('recall', '--check', JOB.format(3))[1]['verdicts'][0]
        self.assertEqual(recalled, {**entry, 'failures': [first, placed, {**first, 'cell': 'c'}]})
        other = {**self.ENTRY, 'link': JOB.format(4)}
        for patch in ({'link': JOB.format(3), 'bucket': 'fail', 'patch': [{**first, 'cell': 'd'}]},
                      {'link': JOB.format(3), 'bucket': 'cancel', 'patch': [placed]},
                      {'link': JOB.format(5), 'bucket': 'fail', 'patch': [placed]},
                      {'link': JOB.format(3), 'bucket': 'fail', 'patch': [placed, placed]}):
            rc, r = self.main('remember', stdin=json.dumps([other, patch]))
            self.assertEqual(rc, 1, patch)
            self.assertIn('patch for', r['error'])
        self.assertEqual(self.main('recall', '--check', JOB.format(3), '--check', JOB.format(4))[1]['verdicts'], [recalled], 'all or nothing')

    def test_nothing_remembered_recalls_nothing(self):
        self.assertEqual(self.main('recall', '--check', JOB.format(3)), (0, {'head': HEAD, 'verdicts': [], 'left': []}))

    def joined(self, held):
        """The entry a held-back {link, starts} names, from one --offset recall per page, each line within the bound."""
        failures = []
        for start in held['starts']:
            rc, r = self.main('recall', '--check', held['link'], '--offset', str(start))
            self.assertLessEqual(len(self.printed), collect.RECALL_BYTES, 'each page line, seal and newline included')
            self.assertEqual((rc, [v['link'] for v in r['verdicts']], r['left']), (0, [held['link']], []))
            failures += r['verdicts'][0]['failures']
        return {**r['verdicts'][0], 'failures': failures}

    def test_recall_holds_back_what_does_not_fit_the_line_and_leaves_out_what_never_fits(self):
        # tinyusb#4019: one 51 KB line, a HIL job's 113 failures among them, no relay could copy.
        huge, small, other, medium = self.entry(3, 3000), self.entry(4, 300), self.entry(5, 300), self.entry(6, 900)
        self.main('remember', stdin=json.dumps([huge, small, other, medium]))
        with mock.patch.object(collect, 'RECALL_BYTES', 2000):
            _, r = self.main('recall', *[a for n in (3, 4, 5, 6) for a in ('--check', JOB.format(n))])
            self.assertLessEqual(len(self.printed), 2000, 'the printed line, seal and newline included')
            self.assertEqual((r['verdicts'], r['left']), ([small, other], [{'link': JOB.format(6), 'starts': [0]}]), 'the huge one is in neither')
            self.assertEqual(self.joined(r['left'][0]), medium, 'a held-back link fits one page')
            self.assertEqual(self.main('recall', '--check', JOB.format(3))[1], {'head': HEAD, 'verdicts': [], 'left': []}, 'nor alone')
    def test_a_verdict_too_large_for_a_line_is_held_back_then_recalled_in_pages_that_join_into_it(self):
        # tinyusb#4019: a HIL job's 113 failures, about 46 KB, judged again on every launch (#25).
        many = {**self.ENTRY, 'failures': [{**self.ENTRY['failures'][0], 'firstError': f'{n:03} ' + 'x' * 150} for n in range(40)]}
        small = self.entry(4, 10)
        self.main('remember', stdin=json.dumps([many, small]))
        with mock.patch.object(collect, 'RECALL_BYTES', 2000):
            for checks in ((3, 4), (3,)):
                _, r = self.main('recall', *[a for n in checks for a in ('--check', JOB.format(n))])
                self.assertLessEqual(len(self.printed), 2000)
                self.assertEqual((r['verdicts'], [h['link'] for h in r['left']]), ([small] if 4 in checks else [], [many['link']]))
                self.assertGreater(len(r['left'][0]['starts']), 3)
                self.assertEqual(self.joined(r['left'][0]), many)
            rc, bad = self.main('recall', '--check', JOB.format(3), '--offset', str(r['left'][0]['starts'][1] + 1))
            self.assertEqual(rc, 1)
            self.assertIn('starts no page', bad['error'])
    def test_a_verdict_with_one_failure_too_large_for_a_page_or_too_many_pages_to_list_is_not_paged(self):
        huge = {**self.ENTRY, 'failures': [{**self.ENTRY['failures'][0], 'firstError': 'x' * 3000}, self.ENTRY['failures'][0]]}
        many = {**self.ENTRY, 'link': JOB.format(5), 'failures': [{**self.ENTRY['failures'][0], 'firstError': 'x' * 700} for _ in range(1000)]}
        self.main('remember', stdin=json.dumps([huge, self.entry(4, 10), many]))
        with mock.patch.object(collect, 'RECALL_BYTES', 2000):  # two of many's failures to a page: 500 starts outgrow the line
            _, r = self.main('recall', *[a for n in (3, 4, 5) for a in ('--check', JOB.format(n))])
            self.assertEqual(([v['link'] for v in r['verdicts']], r['left']), ([JOB.format(4)], []), 'neither returned nor held back')
            for n in (3, 5):
                self.assertEqual(self.main('recall', '--check', JOB.format(n))[1]['left'], [])
                self.assertEqual(self.main('recall', '--check', JOB.format(n), '--offset', '0')[0], 1)

    def test_a_verdict_with_no_failures_held_back_is_one_empty_page(self):
        empty = {**self.ENTRY, 'link': JOB.format(4), 'failures': []}
        self.main('remember', stdin=json.dumps([self.entry(3, 1500), empty]))
        with mock.patch.object(collect, 'RECALL_BYTES', 2000):
            _, r = self.main('recall', '--check', JOB.format(3), '--check', JOB.format(4))
            self.assertIn({'link': JOB.format(4), 'starts': [0]}, r['left'])
            self.assertEqual(self.joined({'link': JOB.format(4), 'starts': [0]}), empty)
    def test_offset_is_for_recall_with_exactly_one_check(self):
        for argv in (('recall', '--check', JOB.format(3), '--check', JOB.format(4), '--offset', '0'), ('failures', '--check', JOB.format(3), '--offset', '0')):
            with self.assertRaises(SystemExit):
                with mock.patch('sys.stderr', io.StringIO()):
                    collect.main([argv[0], '--repo', 'o/r', '--pr', '5', '--head', HEAD, *argv[1:]])

    def test_a_verdict_that_fits_only_without_the_links_already_taken_is_still_returned(self):
        # Room is reserved for the links still to come, not for those already in the line.
        entries = [self.entry(n, 10) for n in (3, 4, 5, 6)] + [self.entry(7, 0)]
        self.main('remember', stdin=json.dumps(entries))
        checks = [a for n in (3, 4, 5, 6, 7) for a in ('--check', JOB.format(n))]
        with mock.patch.object(collect, 'RECALL_BYTES', 10 ** 6):
            self.main('recall', *checks)
        budget = len(self.printed)  # all five, exactly: the last one fits only with no left to list
        with mock.patch.object(collect, 'RECALL_BYTES', budget):
            _, r = self.main('recall', *checks)
        self.assertEqual((r['verdicts'], r['left']), (entries, []))

    def test_a_verdict_that_fits_alone_but_not_beside_the_other_links_is_held_back_not_dropped(self):
        big, smalls = self.entry(3, 3000), [self.entry(n, 10) for n in (4, 5, 6, 7)]
        self.main('remember', stdin=json.dumps([big, *smalls]))
        self.main('recall', '--check', JOB.format(3))
        alone = len(self.printed)
        checks = [a for n in (3, 4, 5, 6, 7) for a in ('--check', JOB.format(n))]
        with mock.patch.object(collect, 'RECALL_BYTES', alone + 50):  # less than the other four links take to list
            _, r = self.main('recall', *checks)
            self.assertEqual((r['verdicts'], r['left']), (smalls, [{'link': JOB.format(3), 'starts': [0]}]))
            self.assertEqual(self.joined(r['left'][0]), big, 'recalled alone')
    def test_links_held_back_never_push_the_line_over_its_bound(self):
        # Long links take room to list: listing them all in `left` must not break the bound.
        link = 'https://ci.example/' + 'y' * 600 + '/{}'
        entries = [self.entry(n, 10, link) for n in range(5)]
        self.main('remember', stdin=json.dumps(entries))
        with mock.patch.object(collect, 'RECALL_BYTES', 3000):
            _, r = self.main('recall', *[a for e in entries for a in ('--check', e['link'])])
            self.assertLessEqual(len(self.printed), 3000)
            self.assertTrue(r['left'], 'the fixture holds some back')
            for held in r['left']:
                self.assertEqual(self.joined(held), next(e for e in entries if e['link'] == held['link']), 'each fits one page')
    def test_remember_refuses_what_is_not_a_list_of_verdicts(self):
        for text in ('not json', json.dumps({'link': 'x'}), json.dumps([{'link': 'x', 'bucket': 'fail'}]),
                     json.dumps([{'link': 'x', 'bucket': 'fail', 'patch': [7]}]), json.dumps([{'link': 'x', 'bucket': 'fail', 'failures': [], 'patch': []}])):
            rc, r = self.main('remember', stdin=text)
            self.assertEqual(rc, 1, text)
            self.assertIn('verdicts on stdin', r['error'])


if __name__ == '__main__':
    unittest.main()
