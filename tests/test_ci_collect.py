"""Tests for ci-rerun's collect.py against a fake gh."""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

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
UF2 = Path(__file__).resolve().parent / 'data' / 'tinyuf2_hil'  # real ESP32-S3 battery reports, paths scrubbed
S3 = 'espressif_esp32s3_devkitc_1'
GREPTILE = 'https://app.greptile.com/tinyusb/github/hathach/agentrc/-/pull/165'
# Check run 118615785789 on hathach/agentrc PR 165, as GitHub reports it.
BELOW_TITLE = 'Base review: Confidence 4/5 \u2014 below your required 5/5'
BELOW_SUMMARY = ("Greptile reviewed this pull request successfully \u2014 this check reflects your team's confidence threshold, not a review "
                 'failure. The review scored 4/5, below the 5/5 this repository requires for the check to pass.')


def setUpModule():
    guard_survivors(__name__)


def greptile_run(title=BELOW_TITLE, summary=BELOW_SUMMARY, **over):
    return {'id': 118615785789, 'name': 'Greptile Review', 'head_sha': HEAD, 'status': 'completed', 'conclusion': 'failure',
            'started_at': '2026-10-20T12:02:14Z', 'completed_at': '2026-10-20T12:06:54Z', 'details_url': GREPTILE,
            'app': {'slug': 'greptile-apps'}, 'output': {'title': title, 'summary': summary, 'text': None}, **over}


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
        self.mergeable = ['MERGEABLE']   # mergeable per `gh pr view`, last one repeats
        self.states = ['OPEN']   # state per `gh pr view`, last one repeats
        self.listings = []   # `gh pr checks` answers in order: a list of checks, or (rc, stderr)
        self.calls = []
        self.clock = [0]
        self.resources = {}   # `gh api rate_limit`'s resources
        # The base branch's required checks: classic protection, rulesets; the head's check runs and statuses.
        self.protection = {'required_status_checks': {'contexts': [], 'checks': [], 'enforcement_level': 'non_admins'}}
        self.rules = []
        self.runs = []      # check runs per read, last one repeats
        self.commit_statuses = []

        def run(argv, **kw):
            self.calls.append(argv[1:3])
            path = argv[4] if argv[1:3] == ['api', '--paginate'] else argv[2] if argv[1] == 'api' else ''
            ok = lambda out: mock.Mock(returncode=0, stdout=json.dumps(out).encode())
            if path == 'repos/o/r/branches/master':
                return ok({'name': 'master', 'protection': self.protection})
            if path.startswith('repos/o/r/rules/branches/master?'):
                return ok([self.rules])
            if path.startswith(f'repos/o/r/commits/{HEAD}/check-runs?'):
                self.calls.append(['check-runs'])
                runs = self.runs.pop(0) if self.runs and isinstance(self.runs[0], list) and len(self.runs) > 1 else \
                    (self.runs[0] if self.runs and isinstance(self.runs[0], list) else self.runs)
                return ok([{'total_count': len(runs), 'check_runs': runs}])
            if path.startswith(f'repos/o/r/commits/{HEAD}/statuses?'):
                self.calls.append(['statuses'])
                return ok([self.commit_statuses])
            if argv[1:3] == ['pr', 'view']:
                head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
                mergeable = self.mergeable.pop(0) if len(self.mergeable) > 1 else self.mergeable[0]
                state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                return mock.Mock(returncode=0, stdout=json.dumps(
                    {'headRefOid': head, 'baseRefName': 'master', 'baseRefOid': BASE, 'mergeable': mergeable, 'state': state}).encode())
            if argv[1:3] == ['api', 'rate_limit']:
                return mock.Mock(returncode=0, stdout=json.dumps({'resources': self.resources}).encode())
            if argv[1] == 'api':   # a failing Actions job's record: a first attempt, its own execution
                job = int(argv[2].split('/jobs/')[1])
                return mock.Mock(returncode=0, stdout=json.dumps({'id': job, 'run_id': 7, 'run_attempt': 1, 'head_sha': HEAD}).encode())
            answer = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
            if isinstance(answer, tuple):
                return mock.Mock(returncode=answer[0], stdout=b'', stderr=answer[1].encode())
            return mock.Mock(returncode=0, stdout=json.dumps(answer).encode())
        sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        # CircleCI's public API: job number -> workflow id (wf-<n> unless set), workflow -> statuses per read.
        self.workflow_of, self.statuses = {}, {}

        def workflow_of(repo, number):
            self.calls.append(['circleci', 'job', number])
            return self.workflow_of.get(number, f'wf-{number}')

        def workflow_status(wid):
            self.calls.append(['circleci', 'workflow', wid])
            seq = self.statuses.get(wid, ['failed'])
            got = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(got, Exception):
                raise got
            return got
        for obj, name, fake in ((collect.subprocess, 'run', run), (collect.time, 'sleep', sleep),
                                (collect.time, 'monotonic', lambda: self.clock[0]),
                                (collect.circleci, 'workflow_of', workflow_of), (collect.circleci, 'workflow_status', workflow_status)):
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
        self.assertEqual(sorted(r), ['baseRef', 'checks', 'head', 'ignored', 'mergeable', 'observedAt', 'pending', 'registered', 'requiredChecks', 'status', 'waiting'], 'only what the workflow reads, and no null error')
        self.assertEqual([(c['name'], c['bucket'], c['link']) for c in r['checks']],
                         [('hil', 'fail', JOB.format(3)), ('docs', 'fail', RTD.format(9)), ('lint', 'cancel', 'https://example.com/x')])
        self.assertEqual([c['aliases'] for c in r['checks']], [[], [], []], 'printed empty, so a relay has nothing to add (#50)')
        self.assertRegex(r['observedAt'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_registered_counts_every_check_and_waiting_fingerprints_the_pending_ones(self):
        self.heads = [HEAD]
        def read(*checks):
            self.listings = [list(checks)]
            return self.main()[1]
        a, b, done = self.check('hil', 'pending', JOB.format(3)), self.check('lint', 'pending', JOB.format(4)), self.check('x', 'pass', JOB.format(1))
        r = read(a, b, done)
        self.assertEqual((r['status'], r['registered']), ('running', 3))
        self.assertEqual(read(b, a, done)['waiting'], r['waiting'], 'order does not matter')
        self.assertEqual(read(a, b)['waiting'], r['waiting'], 'only pending checks count')
        self.assertNotEqual(read(a, self.check('lint', 'pending', JOB.format(5)), done)['waiting'], r['waiting'], 'a new attempt is movement')
        self.assertNotEqual(read(a)['waiting'], r['waiting'])
        self.assertEqual((read(done)['waiting'], read(done)['registered']), ('', 1), 'nothing to fingerprint once settled')

    def test_attempt_only_from_a_link_that_names_one_run(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'fail', 'https://github.com/o/r/actions/runs/7/job/3'),
                          self.check('docs', 'fail', RTD.format(9)), self.check('cci', 'fail', CIRCLE.format(413502)),
                          self.check('preview', 'fail', 'https://p--5.org.readthedocs.build/en/5/'),
                          self.check('bot', 'fail', 'https://greptile.com/'), self.check('rabbit', 'fail', '')]]
        self.assertEqual([c['attempt'] for c in self.main()[1]['checks']],
                         ['actions:3', 'readthedocs:9', 'circleci:413502', None, None, None])

    def test_a_failed_circleci_job_is_pending_until_its_workflow_finishes(self):
        self.heads = [HEAD]
        for status in collect.circleci.UNFINISHED:
            self.statuses = {'wf-1': [status]}
            self.listings = [[self.check('cci', 'fail', CIRCLE.format(1)), self.check('hil', 'pass', JOB.format(3))]]
            rc, r = self.main()
            self.assertEqual((rc, r['status'], r['pending'], r['checks']), (0, 'running', 1, []), status)
        self.statuses = {'wf-1': ['failed']}
        rc, r = self.main()
        self.assertEqual((r['status'], r['pending'], [c['link'] for c in r['checks']]), ('red', 0, [CIRCLE.format(1)]))

    def test_an_unfinished_workflow_holds_only_its_own_jobs(self):
        self.heads = [HEAD]
        self.workflow_of = {1: 'wf-a', 2: 'wf-a', 3: 'wf-b'}
        self.statuses = {'wf-a': ['failing'], 'wf-b': ['failed']}
        self.listings = [[self.check('c1', 'fail', CIRCLE.format(1)), self.check('c2', 'cancel', CIRCLE.format(2)),
                          self.check('c3', 'fail', CIRCLE.format(3)), self.check('hil', 'fail', JOB.format(4)),
                          self.check('docs', 'fail', RTD.format(9))]]
        rc, r = self.main()
        self.assertEqual((r['status'], r['pending']), ('running', 2))
        self.assertEqual([c['name'] for c in r['checks']], ['c3', 'hil', 'docs'], 'other workflows and providers still listed')
        self.assertEqual(sorted(c[2] for c in self.calls if c[:2] == ['circleci', 'workflow']), ['wf-a', 'wf-b'], 'one status read per workflow')

    def test_waits_for_the_workflow_reading_its_status_every_poll(self):
        self.heads = [HEAD]
        self.statuses = {'wf-1': ['running', 'failing', 'failed']}
        self.listings = [[self.check('cci', 'fail', CIRCLE.format(1))]]
        rc, r = self.main('--wait-seconds', '600')
        self.assertEqual((r['status'], self.clock[0], [c['link'] for c in r['checks']]), ('red', 60, [CIRCLE.format(1)]))
        self.assertEqual(self.calls.count(['circleci', 'workflow', 'wf-1']), 3, 'status read every poll')
        self.assertEqual(self.calls.count(['circleci', 'job', 1]), 1, 'a job\'s workflow looked up once per call')

    def test_a_circleci_status_it_cannot_read_is_an_error(self):
        self.heads = [HEAD]
        self.listings = [[self.check('cci', 'fail', CIRCLE.format(1))]]
        self.statuses = {'wf-1': [collect.circleci.Failed('workflow wf-1: unknown status None')]}
        rc, r = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('CircleCI: workflow wf-1: unknown status None', r['error'])
        self.assertNotIn('status', r)

    def test_green_when_everything_passed_or_skipped(self):
        self.heads = [HEAD]
        self.listings = [[self.check('a', 'pass', JOB.format(1)), self.check('b', 'skipping', JOB.format(2))]]
        self.assertEqual(self.main()[1]['status'], 'green')

    @staticmethod
    def run_of(i, name, status='completed', conclusion='success', app=1, start='2026-10-01T00:00:00Z'):
        return {'id': i, 'name': name, 'status': status, 'conclusion': conclusion, 'app': {'id': app}, 'started_at': start}

    def require(self, *contexts, app=None):
        self.rules = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [
            {'context': c, **({'integration_id': app} if app is not None else {})} for c in contexts]}}]

    def test_required_checks_from_protection_and_rules_fold_into_the_status(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.protection = {'required_status_checks': {'contexts': ['lint'], 'checks': [{'context': 'test', 'app_id': None}], 'enforcement_level': 'everyone'}}
        self.require('build')
        self.runs = [self.run_of(1, 'build', conclusion='failure'), self.run_of(2, 'build', start='2026-10-01T01:00:00Z'),
                     self.run_of(3, 'test', conclusion='neutral')]
        self.commit_statuses = [{'id': 9, 'context': 'lint', 'state': 'success', 'updated_at': '2026-10-01T00:00:00Z'}]
        rc, r = self.main()
        self.assertEqual((rc, r['status'], r['baseRef'], r['waiting']), (0, 'green', 'master', ''), 'a rerun that passed, a neutral run, a success status')
        self.assertEqual(r['requiredChecks'], [{'context': 'build', 'app': -1, 'state': 'completed/success', 'bucket': 'pass'},
                                               {'context': 'lint', 'app': -1, 'state': 'success', 'bucket': 'pass'},
                                               {'context': 'test', 'app': -1, 'state': 'completed/neutral', 'bucket': 'pass'}])
        self.runs = [self.run_of(5, 'lint', conclusion='failure'), *self.runs[1:]]
        self.assertEqual(self.main()[1]['status'], 'red', 'a run outranks a success status')
        self.runs = [self.run_of(5, 'lint', status='in_progress', conclusion=None), *self.runs[1:]]
        rc, r = self.main()
        self.assertEqual((r['status'], r['pending'], r['registered']), ('running', 0, 1), 'pending and registered count listed checks only')
        self.assertNotEqual(r['waiting'], '')

    def test_a_required_check_never_registered_keeps_it_running_and_the_wait_names_it(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.require('build', 'docs')
        self.runs = [self.run_of(1, 'build')]
        rc, r = self.main('--wait-seconds', '90')
        self.assertEqual((r['status'], self.clock[0]), ('running', 90), 'waited for it')
        self.assertEqual(r['requiredChecks'][1], {'context': 'docs', 'app': -1, 'state': 'not registered', 'bucket': 'missing'})
        missing = r['waiting']
        self.runs = [self.run_of(1, 'build'), self.run_of(2, 'docs', status='queued', conclusion=None)]
        self.assertNotEqual(self.main()[1]['waiting'], missing)
        queued = self.main()[1]['waiting']
        self.runs[1] = {**self.runs[1], 'id': 3}
        self.assertNotEqual(self.main()[1]['waiting'], queued, 'a new run of an unmet requirement is movement')

    def test_a_check_required_from_an_app_passes_only_on_that_app_s_run(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.require('build', app=42)
        self.runs = [self.run_of(2, 'build', app=99)]
        self.commit_statuses = [{'id': 9, 'context': 'build', 'state': 'success', 'updated_at': '2026-10-01T00:00:00Z'}]
        rc, r = self.main()
        self.assertEqual((r['status'], r['requiredChecks']), ('running', [
            {'context': 'build', 'app': 42, 'state': 'not registered by app 42 (only by app 99)', 'bucket': 'missing'}]))
        self.assertNotIn(['statuses'], self.calls, 'a status names no app, so it never stands in for one')
        self.runs[0]['app']['id'] = 42
        self.assertEqual(self.main()[1]['status'], 'green')
        self.protection = {'required_status_checks': {'contexts': ['build'], 'checks': [{'context': 'build', 'app_id': -1}], 'enforcement_level': 'everyone'}}
        self.runs[0]['app']['id'] = 99
        self.assertEqual([c['app'] for c in self.main()[1]['requiredChecks']], [42], 'the named app outranks any')

    def test_a_status_never_stands_in_for_an_app_s_check_beside_an_any_app_one(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.rules = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [
            {'context': 'a-status'}, {'context': 'build', 'integration_id': 42}]}}]
        self.commit_statuses = [{'id': i, 'context': c, 'state': 'success', 'updated_at': '2026-10-01T00:00:00Z'} for i, c in enumerate(('a-status', 'build'))]
        rc, r = self.main()
        self.assertEqual((r['status'], [c['bucket'] for c in r['requiredChecks']]), ('running', ['pass', 'missing']))

    def test_a_malformed_run_or_status_is_an_error(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.require('build')
        for name, broken in (('no app', {'app': None}), ('no name', {'name': None}), ('no status', {'status': None, 'started_at': None})):
            with self.subTest(name):
                self.runs = [{**self.run_of(1, 'build'), **broken}]
                rc, r = self.main()
                self.assertEqual(rc, 1)
                self.assertIn('lacks a name, status or app id', r['error'])
        self.runs = []
        self.commit_statuses = [{'id': 9, 'state': 'success', 'updated_at': '2026-10-01T00:00:00Z'}]
        self.assertIn('has no context', self.main()[1]['error'])

    def test_a_run_and_a_status_of_one_required_name_must_both_pass(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.require('CodeRabbit')
        self.runs = [self.run_of(1, 'CodeRabbit')]
        for state, want in (('failure', 'red'), ('pending', 'running'), ('success', 'green')):
            with self.subTest(state):
                self.commit_statuses = [{'id': 9, 'context': 'CodeRabbit', 'state': state, 'updated_at': '2026-10-01T00:00:00Z'}]
                rc, r = self.main('--ignore-check', 'CodeRabbit')
                self.assertEqual((r['status'], r['requiredChecks'][0]['state']), (want, f'completed/success; status {state}'))
        for run, state in ((self.run_of(1, 'CodeRabbit', status='queued', conclusion=None), 'failure'), (self.run_of(1, 'CodeRabbit', conclusion='failure'), 'pending')):
            with self.subTest(run=run['status'], status=state):
                self.runs = [run]
                self.commit_statuses = [{'id': 9, 'context': 'CodeRabbit', 'state': state, 'updated_at': '2026-10-01T00:00:00Z'}]
                rc, r = self.main('--ignore-check', 'CodeRabbit')
                self.assertEqual((r['status'], r['requiredChecks'][0]['bucket']), ('running', 'pending'), 'a re-run may yet pass')
                waiting = r['waiting']
                self.commit_statuses[0]['id'] = 10
                self.assertNotEqual(self.main('--ignore-check', 'CodeRabbit')[1]['waiting'], waiting, 'both sources are the wait\'s evidence')

    def test_an_ignored_check_still_counts_when_required(self):
        self.heads = [HEAD]
        self.listings = [[self.check('CodeRabbit', 'fail', ''), self.check('build', 'pass', JOB.format(1))]]
        self.require('CodeRabbit')
        self.commit_statuses = [{'id': 9, 'context': 'CodeRabbit', 'state': 'failure', 'updated_at': '2026-10-01T00:00:00Z'}]
        rc, r = self.main('--ignore-check', 'CodeRabbit')
        self.assertEqual((r['status'], r['checks'], r['ignored'], r['requiredChecks'][0]['bucket']), ('red', [], ['CodeRabbit'], 'fail'))
        self.commit_statuses[0]['state'] = 'pending'
        self.assertEqual(self.main('--ignore-check', 'CodeRabbit')[1]['status'], 'running')
        self.commit_statuses[0]['state'] = 'failure'
        self.listings = [[self.check('CodeRabbit', 'fail', '')]]
        rc, r = self.main('--ignore-check', 'CodeRabbit')
        self.assertEqual((r['status'], r['registered'], r['pending']), ('red', 0, 0), 'a failed requirement is red with nothing else listed')
        self.commit_statuses[0]['state'] = 'success'
        self.assertEqual(self.main('--ignore-check', 'CodeRabbit')[1]['status'], 'running', 'nothing listed and nothing failed: as before')

    def test_required_run_states(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.require('build')
        cases = {('waiting', None): 'running', ('requested', None): 'running', ('completed', 'skipped'): 'green',
                 ('completed', 'timed_out'): 'red', ('completed', 'action_required'): 'red', ('completed', 'stale'): 'red'}
        for (status, conclusion), want in cases.items():
            with self.subTest(status=status, conclusion=conclusion):
                self.runs = [self.run_of(1, 'build', status=status, conclusion=conclusion)]
                self.assertEqual(self.main()[1]['status'], want)
        self.runs = [self.run_of(1, 'build', status='completed', conclusion='surprise')]
        rc, r = self.main()
        self.assertEqual(rc, 1)
        self.assertIn("is completed/surprise, which collect.py does not read", r['error'])
        self.runs = []
        self.commit_statuses = [{'id': 9, 'context': 'build', 'state': 'unknown', 'updated_at': '2026-10-01T00:00:00Z'}]
        self.assertIn("is 'unknown', which collect.py does not read", self.main()[1]['error'])
        self.commit_statuses = []
        self.runs = [self.run_of(1, 'build'), self.run_of(2, 'build', status='queued', conclusion=None, start=None)]
        rc, r = self.main()
        self.assertEqual((r['status'], r['requiredChecks'][0]['bucket']), ('running', 'pending'), 'a re-run not started yet')
        self.assertIn('cannot be ordered', r['requiredChecks'][0]['state'])

    def test_required_checks_are_read_only_when_judged(self):
        self.heads = [HEAD]
        self.require('build')
        self.runs = [self.run_of(1, 'build')]
        self.listings = [[self.check('build', 'pending', JOB.format(1))]] * 2 + [[self.check('build', 'pass', JOB.format(1))]]
        rc, r = self.main('--wait-seconds', '600')
        self.assertEqual((r['status'], self.calls.count(['check-runs'])), ('green', 1), 'not while other checks still run')
        self.assertEqual(self.calls.count(['api', 'repos/o/r/branches/master']), 1, 'the requirements once per call')
        self.calls.clear()
        self.rules = []
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.main()
        self.assertNotIn(['check-runs'], self.calls, 'no requirement, no check-run read')
        self.calls.clear()
        self.listings = [[self.check('build', 'pending', JOB.format(1))]]
        rc, r = self.main('--wait-seconds', '60')
        self.assertEqual((r['status'], r['requiredChecks']), ('running', []), 'a listed check still pending: nothing judged')
        self.assertNotIn(['api', 'repos/o/r/branches/master'], self.calls)
        self.require('build')
        self.listings, self.runs = [[]], []
        rc, r = self.main()
        self.assertEqual(r['requiredChecks'][0]['bucket'], 'missing', 'nothing listed: judged, for the workflow\'s no-check rule')
        self.calls.clear()
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        self.runs = [self.run_of(1, 'build')]
        self.mergeable = ['UNKNOWN', 'MERGEABLE']
        rc, r = self.main()
        self.assertEqual((r['status'], r['requiredChecks'][0]['bucket']), ('green', 'pass'), 'judged on the final read that settled it')
        self.calls.clear()
        self.mergeable = ['CONFLICTING']
        rc, r = self.main()
        self.assertEqual((r['status'], r['requiredChecks']), ('conflicting', []))
        self.assertNotIn(['api', 'repos/o/r/branches/master'], self.calls, 'a conflicting PR judges nothing')
        self.calls.clear()
        self.mergeable = ['MERGEABLE']
        self.rules = []
        self.protection = {'required_status_checks': {'contexts': ['lint'], 'checks': [], 'enforcement_level': 'off'}}
        self.assertEqual(self.main()[1]['requiredChecks'], [], 'protection that enforces nothing requires nothing')

    def test_a_base_retargeted_while_collecting_is_an_error(self):
        self.heads = [HEAD]
        self.listings = [[self.check('build', 'pass', JOB.format(1))]]
        bases = iter(['master', 'next'])
        run = collect.subprocess.run

        def view(argv, **kw):
            got = run(argv, **kw)
            if argv[1:3] == ['pr', 'view']:
                got.stdout = json.dumps({**json.loads(got.stdout), 'baseRefName': next(bases)}).encode()
            return got
        with mock.patch.object(collect.subprocess, 'run', view):
            rc, r = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('base moved to next', r['error'])

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
        self.assertEqual((self.calls.count(['pr', 'checks']), self.calls.count(['pr', 'view'])), (3, 4), 'the PR is read before, on every poll and after')

    def test_a_stop_file_ends_the_wait_before_the_next_poll(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stop = Path(tmp.name) / 'stop'
        polls = []

        def sleep(s):
            self.clock[0] += s
            polls.append(s)
            if len(polls) == 2:
                stop.touch()
        with mock.patch.object(collect.time, 'sleep', sleep):
            rc, r = self.main('--wait-seconds', '600', '--stop-file', str(stop))
        self.assertEqual(rc, 1)
        self.assertIn(f'stopped before the next GitHub call: {stop} exists', r['error'])
        self.assertEqual((self.calls.count(['pr', 'checks']), self.calls.count(['pr', 'view'])), (2, 2),
                         'no GitHub call once the file exists')
        rc, r = self.main('--wait-seconds', '0', '--stop-file', str(stop))
        self.assertEqual(rc, 1, 'nor a first one')
        self.assertEqual(self.calls.count(['pr', 'view']), 2)

    def test_brief_lists_no_checks_only_while_running(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3)), self.check('lint', 'fail', JOB.format(4))]]
        rc, r = self.main('--brief')
        self.assertEqual((rc, r['status'], r['pending'], r['checks']), (0, 'running', 1, []))
        self.assertEqual(sorted(r), ['baseRef', 'checks', 'head', 'ignored', 'mergeable', 'observedAt', 'pending', 'registered', 'requiredChecks', 'status', 'waiting'], 'the same members as a whole line')
        self.assertNotIn(['api', 'repos/o/r/actions/jobs/4'], self.calls, 'a check nobody is shown is not resolved')
        self.assertEqual(r['waiting'], self.main()[1]['waiting'], 'the pending checks it hides still fingerprint the wait')
        self.listings = [[self.check('hil', 'pass', JOB.format(3)), self.check('lint', 'fail', JOB.format(4))]]
        rc, r = self.main('--brief')
        self.assertEqual((r['status'], [c['link'] for c in r['checks']]), ('red', [JOB.format(4)]), 'settled: the whole inventory')
        self.assertIn(['api', 'repos/o/r/actions/jobs/4'], self.calls)

    def test_brief_resolves_and_reads_the_head_again_when_mergeable_settles_after_the_listing(self):
        self.heads = [HEAD]
        self.mergeable = ['UNKNOWN', 'MERGEABLE']
        self.listings = [[self.check('lint', 'fail', JOB.format(4))]]
        rc, r = self.main('--brief')
        self.assertEqual((rc, r['status'], [c['link'] for c in r['checks']]), (0, 'red', [JOB.format(4)]))
        at = lambda call: max(i for i, c in enumerate(self.calls) if c == call)
        self.assertGreater(at(['pr', 'view']), at(['api', 'repos/o/r/actions/jobs/4']), 'the head is read again after resolving')

    def test_brief_shows_no_check_when_mergeable_turns_unknown_after_the_listing(self):
        self.heads = [HEAD]
        self.mergeable = ['MERGEABLE', 'UNKNOWN']
        self.listings = [[self.check('lint', 'fail', JOB.format(4))]]
        rc, r = self.main('--brief')
        self.assertEqual((rc, r['status'], r['mergeable'], r['checks']), (0, 'running', 'UNKNOWN', []))

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

    def test_a_conflicting_pr_is_never_green_and_waits_for_nothing(self):
        self.heads = [HEAD]
        self.mergeable = ['CONFLICTING']
        for listing in ([self.check('pre-commit', 'pass', JOB.format(1))], [self.check('hil', 'pending', JOB.format(3))],
                        [self.check('hil', 'fail', JOB.format(3))], (1, "no checks reported on the 'x' branch")):
            self.listings, self.clock[0] = [listing], 0
            rc, r = self.main('--wait-seconds', '600')
            self.assertEqual((rc, r['status'], r['mergeable'], self.clock[0]), (0, 'conflicting', 'CONFLICTING', 0), listing)
        self.assertEqual([c['name'] for c in r['checks']], [], 'no checks, none listed')
        self.listings = [[self.check('hil', 'fail', JOB.format(3))]]
        self.assertEqual([c['name'] for c in self.main()[1]['checks']], ['hil'], 'a failed check is still listed')

    def test_unknown_mergeability_is_running_until_github_decides(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'fail', JOB.format(3))]]
        self.mergeable = ['UNKNOWN']
        rc, r = self.main('--wait-seconds', '70')
        self.assertEqual((r['status'], r['mergeable'], r['pending'], self.calls.count(['pr', 'checks'])), ('running', 'UNKNOWN', 0, 3),
                         'never red, so no accepted failure can pass it; never green')
        self.clock[0] = 0
        self.calls.clear()
        self.mergeable = ['UNKNOWN', 'MERGEABLE']
        rc, r = self.main('--wait-seconds', '600')
        self.assertEqual((r['status'], r['mergeable'], self.clock[0]), ('red', 'MERGEABLE', 30), 'read again on every poll')
        self.mergeable = ['MERGEABLE', 'CONFLICTING']
        rc, r = self.main()
        self.assertEqual((r['status'], r['mergeable']), ('conflicting', 'CONFLICTING'), 'the last read decides')

    def test_a_pr_no_longer_open_is_reported_at_once_whatever_its_head(self):
        self.heads = [OTHER]
        self.mergeable = ['UNKNOWN']
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]]
        for state in ('MERGED', 'CLOSED'):
            self.states, self.clock[0] = [state], 0
            self.calls.clear()
            rc, r = self.main('--wait-seconds', '600', '--brief')
            self.assertEqual((rc, r['status'], r['head'], r['pending'], r['checks'], self.clock[0]), (0, state.lower(), OTHER, 0, [], 0), state)
            self.assertEqual(self.calls, [['pr', 'view']], 'nothing listed, nothing waited for')

    def test_a_merge_during_the_wait_ends_it(self):
        self.heads = [HEAD]
        self.listings = [[self.check('hil', 'pending', JOB.format(3))]]
        for mergeable in ('UNKNOWN', 'MERGEABLE'):
            self.mergeable, self.states, self.clock[0] = [mergeable], ['OPEN', 'OPEN', 'MERGED'], 0
            rc, r = self.main('--wait-seconds', '600')
            self.assertEqual((rc, r['status'], r['checks'], self.clock[0]), (0, 'merged', [], 60), f'read again on every poll, {mergeable}')
        self.clock[0] = 0
        self.states = ['OPEN', 'OPEN', 'OPEN', 'MERGED']
        rc, r = self.main('--wait-seconds', '70')
        self.assertEqual((rc, r['status'], r['pending'], r['checks']), (0, 'merged', 0, []), 'the read after the wait decides')

    def test_an_unexpected_state_is_an_error(self):
        self.heads = [HEAD]
        self.listings = [[]]
        for value in (None, 'DRAFT'):
            self.states = [value]
            rc, r = self.main()
            self.assertEqual(rc, 1)
            self.assertIn(f'state is {value!r}', r['error'])

    def test_an_unexpected_mergeable_is_an_error(self):
        self.heads = [HEAD]
        self.listings = [[]]
        for value in (None, 'DIRTY'):
            self.mergeable = [value]
            rc, r = self.main()
            self.assertEqual(rc, 1)
            self.assertIn(f'mergeable is {value!r}', r['error'])

    def test_a_gh_failure_is_an_error_not_a_status(self):
        self.heads = [HEAD]
        self.listings = [(1, 'HTTP 502')]
        rc, r = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('HTTP 502', r['error'])
        self.assertNotIn('status', r)
        self.assertNotIn('rateLimit', r)

    def test_a_rate_limit_is_reported_with_its_reset(self):
        self.heads = [HEAD]
        self.listings = [(1, 'GraphQL: API rate limit exceeded for user ID 249515.')]
        self.resources = {'core': {'remaining': 4000, 'reset': 1790009999}, 'graphql': {'remaining': 0, 'reset': 1790000060}}
        rc, r = self.main()
        self.assertEqual((rc, r['rateLimit']['resetAt'], r['rateLimit']['secondary']), (1, '2026-09-21T14:14:20Z', False))
        self.assertIn('API rate limit exceeded', r['error'])
        self.resources = {'core': {'remaining': 4000, 'reset': 1790009999}}
        self.listings = [(1, 'HTTP 403: You have exceeded a secondary rate limit.')]
        rc, r = self.main()
        self.assertEqual((r['rateLimit']['resetAt'], r['rateLimit']['secondary']), (None, True))

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
        self.assertTrue(all(['--repo', 'o/r'] == c.args[0][4:6] for c in spy.call_args_list if c.args[0][1] != 'api'))

    def test_an_ignored_check_is_left_out_by_its_exact_name_in_any_bucket(self):
        self.heads = [HEAD]
        real = self.check('hil', 'pass', JOB.format(1))
        for bucket in ('pending', 'fail', 'cancel', 'pass'):
            self.listings = [[self.check('CodeRabbit', bucket, ''), real]]
            rc, r = self.main('--ignore-check', 'CodeRabbit')
            self.assertEqual((rc, r['status'], r['registered'], r['pending'], r['checks'], r['ignored']),
                             (0, 'green', 1, 0, [], ['CodeRabbit']), bucket)
        for name in ('CodeRabbit Review', 'coderabbit', 'SonarCloud Code Analysis'):
            self.listings = [[self.check(name, 'fail', ''), real]]
            rc, r = self.main('--ignore-check', 'CodeRabbit')
            self.assertEqual((r['status'], [c['name'] for c in r['checks']], r['ignored']), ('red', [name], []), name)

    def test_only_ignored_checks_read_as_none_registered(self):
        self.heads = [HEAD]
        self.listings = [[self.check('CodeRabbit', 'pass', '')]]
        rc, r = self.main('--ignore-check', 'CodeRabbit', '--brief')
        self.assertEqual((rc, r['status'], r['registered'], r['pending'], r['ignored']), (0, 'running', 0, 0, ['CodeRabbit']))
        self.listings = [(1, "no checks reported on the 'x' branch")]
        none = self.main('--ignore-check', 'CodeRabbit')[1]
        self.assertEqual((none['status'], none['registered'], none['ignored']), ('running', 0, []), 'truly empty: nothing ignored')
        self.assertEqual(none['waiting'], r['waiting'], 'an ignored check is no movement')

    def test_an_ignored_check_does_not_move_the_waiting_fingerprint(self):
        self.heads = [HEAD]
        hil = self.check('hil', 'pending', JOB.format(3))
        self.listings = [[self.check('CodeRabbit', 'pending', ''), hil]]
        before = self.main('--ignore-check', 'CodeRabbit')[1]
        self.listings = [[self.check('CodeRabbit', 'pass', ''), hil]]
        after = self.main('--ignore-check', 'CodeRabbit')[1]
        self.assertEqual((after['waiting'], after['pending'], after['registered']), (before['waiting'], 1, 1))

    def test_only_ignored_checks_keep_the_pr_guards(self):
        self.heads = [HEAD]
        self.listings = [[self.check('CodeRabbit', 'pending', '')]]
        for mergeable, status in (('UNKNOWN', 'running'), ('CONFLICTING', 'conflicting')):
            self.mergeable = [mergeable]
            rc, r = self.main('--ignore-check', 'CodeRabbit')
            self.assertEqual((rc, r['status'], r['mergeable'], r['ignored']), (0, status, mergeable, ['CodeRabbit']))
        self.mergeable = ['MERGEABLE']
        self.heads = [HEAD, OTHER]
        rc, r = self.main('--ignore-check', 'CodeRabbit')
        self.assertEqual(rc, 1)
        self.assertIn(f'head moved to {OTHER}', r['error'])
        self.heads, self.states = [HEAD], ['MERGED']
        rc, r = self.main('--ignore-check', 'CodeRabbit')
        self.assertEqual((rc, r['status'], r['ignored']), (0, 'merged', []))

    def test_a_real_check_registered_during_the_wait_decides(self):
        self.heads = [HEAD]
        rabbit = self.check('CodeRabbit', 'pending', '')
        self.listings = [[rabbit], [rabbit, self.check('hil', 'pending', JOB.format(3))], [rabbit, self.check('hil', 'fail', JOB.format(3))]]
        rc, r = self.main('--ignore-check', 'CodeRabbit', '--wait-seconds', '600')
        self.assertEqual((r['status'], r['registered'], [c['name'] for c in r['checks']], r['ignored'], self.clock[0]),
                         ('red', 1, ['hil'], ['CodeRabbit'], 60))

    def test_ignore_check_is_for_inventory_with_distinct_names(self):
        for argv in (['failures', '--check', JOB.format(1), '--ignore-check', 'CodeRabbit'],
                     ['inventory', '--ignore-check', ''], ['inventory', '--ignore-check', 'CodeRabbit', '--ignore-check', 'CodeRabbit']):
            with self.assertRaises(SystemExit) as e, redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
                collect.main([*argv, '--repo', 'o/r', '--pr', '5', '--head', HEAD])
            self.assertEqual(e.exception.code, 2, argv)

    def test_a_short_head_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as e, redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            collect.main(['inventory', '--repo', 'o/r', '--pr', '5', '--head', 'abc'])
        self.assertEqual(e.exception.code, 2)


def stdin_of(data):
    """A stdin holding data, text sent as UTF-8."""
    return io.TextIOWrapper(io.BytesIO(data if isinstance(data, bytes) else data.encode('utf-8')), encoding='utf-8')


def log(*lines):
    """An Actions job log as the API returns it: timestamps, ANSI colour, a NUL per line."""
    return ''.join(f'2026-09-24T17:35:56.3154101Z \x1b[36m{line}\x1b[0m\x00\n' for line in lines)


STEP = ['##[group]Run build', 'Build Summary: 1 OK, 0 Failed', '##[endgroup]', 'built',
        '##[group]Run python3 test/hil/hil_test.py', '  python3 test/hil/hil_test.py $ARGS', '##[endgroup]',
        'pico  device/cdc  ...  OK in 3.1s',
        'pico  host/msc  ...  Failed: /home/runner/work/r/r/src/host/msc.c timeout in 9.4s',
        'rp2  device/hid  ...  Failed: not enumerated in 2.0s', 'Total failed: 2',
        '##[error]Process completed with exit code 1.', '##[group]Run upload', '##[endgroup]']


def runner_log(*rows, failed=True):
    """A job log whose hil_test.py step prints rows, and fails when failed."""
    return log(*STEP[4:7], *rows, *['##[error]Process completed with exit code 1.'] * failed)


class FailuresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checks = [{'name': 'hil (x.json)', 'workflow': 'Build', 'bucket': 'fail', 'link': JOB.format(3)},
                       {'name': 'docs', 'workflow': '', 'bucket': 'fail', 'link': RTD.format(9)},
                       {'name': 'bot', 'workflow': '', 'bucket': 'fail', 'link': 'https://greptile.com/'}]
        self.jobs = {'3': {'id': 3, 'name': 'hil (x.json)', 'workflow_name': 'Build', 'run_id': 7, 'run_attempt': 2, 'head_sha': HEAD},
                     '40': {'id': 40, 'name': 'hil (x.json)', 'workflow_name': 'Build', 'run_id': 70, 'head_sha': BASE}}
        self.attempts = {}   # attempt number -> the jobs of run 7 at that attempt, on one page
        self.paged = {}      # attempt number -> its pages as gh --slurp prints them, when not one
        self.logs = {'3': log(*STEP), '40': log(*STEP[:9], 'Total failed: 1', *STEP[11:])}
        self.base_runs = [{'databaseId': 71, 'headSha': 'd' * 40, 'headBranch': 'master', 'status': 'in_progress'},
                          {'databaseId': 72, 'headSha': 'e' * 40, 'headBranch': 'master', 'status': 'completed'},
                          {'databaseId': 70, 'headSha': BASE, 'headBranch': 'master', 'status': 'completed'}]
        self.commit_runs = []   # what `gh run list --commit BASE` answers; None fails it
        self.run_jobs = {'72': [{'name': 'hil (x.json)', 'conclusion': 'skipped', 'databaseId': 41}],
                         '70': [{'name': 'hil (x.json)', 'conclusion': 'failure', 'databaseId': 40}]}
        self.changed = ['src/host/msc.c', 'test/hil/tiny usb.json']
        self.check_runs = []   # the head's check runs, one page; 'limited' refuses the read
        self.workflow_id = 1000   # every run's; None answers a run record without one
        self.calls = []

        def run(argv, **kw):
            self.calls.append(argv[1:])
            ok = lambda out: mock.Mock(returncode=0, stdout=out if isinstance(out, bytes) else (out if isinstance(out, str) else json.dumps(out)).encode(), stderr=b'')
            if argv[1:3] == ['pr', 'view']:
                return ok({'headRefOid': HEAD, 'baseRefName': 'master', 'baseRefOid': BASE, 'mergeable': 'MERGEABLE', 'state': 'OPEN'})
            if argv[1:3] == ['pr', 'checks']:
                return ok(self.checks)
            if argv[1:3] == ['api', 'repos/o/r/branches/master']:
                return ok({'name': 'master', 'protection': {'enabled': False}})
            if argv[1:3] == ['api', '--paginate'] and argv[4].startswith('repos/o/r/rules/branches/master?'):
                return ok([[]])
            if argv[1:3] == ['api', '--paginate'] and '/attempts/' in argv[4]:
                n = int(argv[4].split('/attempts/')[1].split('/')[0])
                jobs = self.attempts.get(n, [])
                return ok(self.paged.get(n) or [{'total_count': len(jobs), 'jobs': jobs}])
            limited = mock.Mock(returncode=1, stdout=b'', stderr=b'HTTP 403: API rate limit exceeded for user ID 1.')
            if argv[1:3] == ['api', 'rate_limit']:
                return ok({'resources': {'core': {'remaining': 0, 'reset': 1790000060}}})
            if argv[1:3] == ['api', '--paginate'] and f'/commits/{HEAD}/check-runs?' in argv[4]:
                return limited if self.check_runs == 'limited' else ok([{'total_count': len(self.check_runs), 'check_runs': self.check_runs}])
            if argv[1:3] == ['api', '--paginate']:
                if self.changed == 'limited':
                    return limited
                if self.changed is None:
                    return mock.Mock(returncode=1, stdout=b'', stderr=b'HTTP 500')
                return ok([[{'filename': f, 'patch': '@@'} for f in self.changed]])
            if argv[1:3] == ['run', 'list'] and argv[argv.index('--workflow') + 1] != str(self.workflow_id):
                return mock.Mock(returncode=1, stdout=b'', stderr=f'could not find any workflows named {argv[argv.index("--workflow") + 1]}'.encode())
            if argv[1:3] == ['run', 'list'] and '--commit' in argv:
                if self.commit_runs == 'limited':
                    return limited
                return ok(self.commit_runs) if self.commit_runs is not None else mock.Mock(returncode=1, stdout=b'', stderr=b'HTTP 502')
            if argv[1:3] == ['run', 'list']:
                return ok(self.base_runs)
            if argv[1:3] == ['run', 'view']:
                return ok({'jobs': self.run_jobs[argv[3]]})
            path = argv[2]
            if re.fullmatch(r'repos/o/r/actions/runs/\d+', path):
                return ok({'id': int(path.rsplit('/', 1)[1]), 'workflow_id': self.workflow_id})
            job = path.split('/jobs/')[1].split('/')[0]
            if path.endswith('/logs') and self.logs[job] == 'limited':
                return limited
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
        self.assertEqual((c['runId'], c['runAttempt']), (7, 2), 'the attempt that executed it')
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

    def test_the_run_on_the_base_commit_wins_over_a_stale_branch_listing(self):
        # #52: the branch listing served a month-old run while the base commit had its own.
        self.base_runs = [{'databaseId': 60, 'headSha': 'f' * 40, 'headBranch': 'master', 'status': 'completed'}]
        self.commit_runs = [{'databaseId': 75, 'headSha': BASE, 'headBranch': 'master', 'status': 'in_progress'},
                            {'databaseId': 70, 'headSha': BASE, 'headBranch': 'master', 'status': 'completed'}]
        self.run_jobs['60'] = [{'name': 'hil (x.json)', 'conclusion': 'failure', 'databaseId': 61}]
        rc, r = self.main(JOB.format(3))
        detail = json.loads(Path(r['detail']).read_text())
        self.assertEqual((detail['baseSha'], detail['checks'][0]['base']['runId']), (BASE, 70))
        self.assertFalse([c for c in self.calls if c[:2] == ['run', 'list'] and '--branch' in c], 'no fallback')
        out = io.StringIO()
        with redirect_stdout(out):
            collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(3)])
        self.assertEqual(unsealed(out.getvalue())['bases'], r['bases'], 'bases picks the same run')

    def test_a_base_commit_run_without_the_job_or_off_the_base_branch_falls_back_to_the_branch(self):
        self.commit_runs = [{'databaseId': 73, 'headSha': BASE, 'headBranch': 'feature', 'status': 'completed'},
                            {'databaseId': 72, 'headSha': BASE, 'headBranch': 'master', 'status': 'completed'}]
        self.assertEqual(self.entries(JOB.format(3))[0]['base']['runId'], 70)
        self.assertEqual([c[2] for c in self.calls if c[:2] == ['run', 'view']], ['72', '70'], 'another branch\'s run is never read, a run listed twice is read once')

    def test_a_failed_base_lookup_is_a_base_error_not_a_fallback(self):
        self.commit_runs = None
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['error'], c['base'], [x['onBase'] for x in c['cells']], c['firstError']),
                         (None, None, [None, None], STEP[8]), 'the log, its evidence and cells stay')
        self.assertIn('HTTP 502', c['baseError'])
        self.assertFalse([c for c in self.calls if c[:2] == ['run', 'list'] and '--branch' in c], 'no stale-branch fallback (#52)')
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(3)]), 1,
                             'bases has no base to compare: a placed verdict is judged again')
        self.assertIn('HTTP 502', json.loads(out.getvalue())['error'])
        self.commit_runs, self.workflow_id = [], None
        self.assertEqual(self.entries(JOB.format(3))[0]['baseError'], 'run 7 names no workflow id')
        self.workflow_id, self.commit_runs = 1000, 'limited'
        rc, r = self.main(JOB.format(3))
        self.assertEqual((rc, r['rateLimit']['resetAt']), (1, collect.utc(1790000060)), 'a rate limit still ends the call')

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
        self.logs['40'] = runner_log('pico  host/msc  ...  Failed: stall  I (24) boot: compile time 07:16  in 1.0s', 'rp2  device/hid  ...  OK in 1.0s')
        self.assertEqual([(x['onBase'], x['baseLine']) for x in self.entries(JOB.format(3))[0]['cells']],
                         [('other-failure', 'pico  host/msc  ...  Failed: stall  I (24) boot: compile time 07:16  in 1.0s'),
                          ('passed', 'rp2  device/hid  ...  OK in 1.0s')])
        self.logs['40'] = runner_log('pico  host/msc  ...  Skipped: no board', 'rp2  device/hid  ...  Failed: not enumerated  I (24) boot: compile time 07:16  in 5.0s',
                                     failed=False)
        self.assertEqual([x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']], ['other', 'same-failure'])

    def test_without_a_comparable_base_run_a_cell_is_not_placed(self):
        self.run_jobs['70'][0]['conclusion'] = 'skipped'
        self.assertEqual({x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']}, {None})

    def test_a_retried_cell_counts_by_its_last_row_and_flash_failures_lose_the_command(self):
        self.logs['3'] = runner_log('pico  host/msc  ...  Failed: timeout in 9.4s', 'pico  host/msc  ...  OK in 3.0s',
                                    'rp2  device/hid  ...  Flash Failed: probe lost  COMMAND FAILED: openocd -f x.cfg',
                                    'rp2  device/cdc  ...  Skipped: no board')
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
        self.assertEqual((c['cells'], c['cellsError']), ([], 'a failed hil_test.py step printed no result row or report this reads; read its log'))

    def test_cells_come_only_from_a_failed_step_whose_run_script_invokes_the_runner(self):
        # tinyusb's pre-commit tests hil_test.py itself and prints a runner-shaped row; tinyuf2's names it in a traceback
        hook = ['##[group]Run pre-commit run --all-files', '##[endgroup]', '.b  device/usbtest  ...  Failed: usbtest did not run',
                '  File "/home/runner/work/r/r/test/hil/hil_test.py", line 257, in select_route', 'RuntimeError: no route',
                '##[error]Process completed with exit code 1.']
        self.logs['3'] = log(*hook)
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['cells'], 'cellsError' in c), ([], False))
        self.assertEqual(c['diagnostics'][0], hook[2], 'the hook\'s output stays its evidence')
        self.logs['3'] = log(*hook[:2], *hook[3:])
        self.assertNotIn('cellsError', self.entries(JOB.format(3))[0], 'naming the runner is not running it')
        self.logs['3'] = log(*STEP[4:7], *STEP[7:11], *hook)
        self.assertEqual([x['cell'] for x in self.entries(JOB.format(3))[0]['cells']], [], 'a runner step that passed reads no failure')
        self.logs['3'] = log('##[group]Run # one report dir per run', 'python3 test/hil/hil_test.py $ARGS', '##[endgroup]', *STEP[7:])
        self.assertEqual([x['cell'] for x in self.entries(JOB.format(3))[0]['cells']], ['pico host/msc', 'rp2 device/hid'],
                         'the runner named in the script, not on the Run line')

    def uf2_log(self, *reports):
        lines = [(UF2 / r).read_text().strip() if r.endswith('.json') else r for r in reports]
        return log(*STEP[:7], *lines, *STEP[11:])

    def test_a_tinyuf2_report_gives_a_cell_per_failed_step_placed_against_the_base_report(self):
        self.logs['3'], self.logs['40'] = self.uf2_log('fail.json'), self.uf2_log('pass.json')
        c = self.entries(JOB.format(3))[0]
        self.assertEqual([(x['cell'], x['signature'], x['lineNo'], x['line'], x['onBase'], x['baseLineNo']) for x in c['cells']],
                         [(f'{S3} app-ran', f'{S3} app-ran: Failed: the DFU device came back while the app should run', 8,
                           f'{S3}  app-ran  ...  Failed: the DFU device came back while the app should run', 'passed', 8)])
        self.assertNotIn('cellsError', c)
        self.assertFalse([d for d in c['diagnostics'] if d.startswith('{')], 'the report line is the cells, not a diagnostic')
        self.logs['40'] = self.uf2_log('fail.json')
        self.assertEqual([x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']], ['same-failure'])

    def test_a_passing_tinyuf2_report_has_no_cells(self):
        self.logs['3'] = self.uf2_log('pass.json')
        c = self.entries(JOB.format(3))[0]
        self.assertEqual(c['cells'], [])
        self.assertNotIn('cellsError', c, 'a report was read')
        self.logs['3'] = self.uf2_log('{"pass": true, "caveat": "", "results": []}')
        self.assertNotIn('cellsError', self.entries(JOB.format(3))[0], 'an empty report is still a report')

    def test_a_failed_cell_row_stands_in_for_the_report_line_as_first_error(self):
        report = json.loads((UF2 / 'fail.json').read_text())
        report['results'][0]['steps'][-1]['detail'] = 'esptool session failed: exit 2'
        self.logs['3'] = self.uf2_log(json.dumps(report))
        c = self.entries(JOB.format(3))[0]
        self.assertEqual(c['firstError'], f'{S3}  app-ran  ...  Failed: esptool session failed: exit 2')
        self.assertEqual(c['diagnostics'], [], 'neither the report nor its rows are listed')

    def test_tinyuf2_rows_cover_locks_refusals_cleanup_and_report_level_failures(self):
        passed = json.loads((UF2 / 'pass.json').read_text())['results'][0]
        failed_cleanup = {**passed, 'pass': False, 'cleanup': {'pass': False, 'state': 'bootloader invalidated', 'detail': 'readback differs'}}
        report = {'pass': False, 'caveat': 'run ended without a report; board state unknown', 'error': 'roster rejected',
                  'results': [{'board': 'a', 'ran': False, 'pass': False, 'locked': True, 'detail': 'board locked: {"pid": 9}'},
                              {'board': 'b', 'ran': False, 'pass': False, 'locked': False, 'detail': 'build record rejected'},
                              {**failed_cleanup, 'board': 'c'}, {**passed, 'board': 'd', 'pass': False, 'detail': 'odd'}]}
        failed = {cell: row[0] for cell, row in collect.report_rows(7, report).items() if row[0] != 'OK'}
        self.assertEqual(failed, {'a locked': 'Failed: board locked', 'b run': 'Failed: build record rejected',
                                  'c cleanup': 'Failed: readback differs', 'd run': 'Failed: odd',
                                  'report error': 'Failed: roster rejected',
                                  'report caveat': 'Failed: run ended without a report; board state unknown'})

    def test_a_boards_locked_and_run_cells_are_placed_against_a_base_run(self):
        locked = '{"pass": false, "caveat": "", "results": [{"board": "%s", "ran": false, "pass": false, "locked": true, "detail": "board locked"}]}' % S3
        refused = '{"pass": false, "caveat": "", "error": "no", "results": [{"board": "%s", "ran": false, "pass": false, "locked": false, "detail": "no"}]}' % S3
        self.logs['40'] = self.uf2_log('pass.json')
        for head, cell in ((locked, f'{S3} locked'), (refused, f'{S3} run')):
            self.logs['3'] = self.uf2_log(head)
            self.assertEqual([(x['cell'], x['onBase']) for x in self.entries(JOB.format(3))[0]['cells'] if x['cell'] == cell],
                             [(cell, 'passed')], 'a passing base board ran under its lock')
        self.logs['3'], self.logs['40'] = self.uf2_log(locked), self.uf2_log(refused)
        self.assertEqual([(x['cell'], x['onBase']) for x in self.entries(JOB.format(3))[0]['cells']],
                         [(f'{S3} locked', 'not-run')], 'an admission refusal never reached the lock')

    def test_passing_tinyuf2_install_restore_and_invalidate_reports_have_no_failed_cells(self):
        firmware = json.loads((UF2 / 'pass.json').read_text())['results'][0]['firmware']
        install = {'pass': True, 'caveat': '', 'survivors': [],
                   'results': [{'board': 'a', 'installed': {'path': 'tinyuf2.bin', 'sha256': '00'}}]}
        restore = {'pass': True, 'caveat': '', 'survivors': [],
                   'results': [{'board': 'a', 'firmware': firmware, 'cleanup': {'pass': True, 'state': 'blinky copied', 'evidence': {}}}]}
        invalidate = {'pass': True, 'caveat': '', 'survivors': [],
                      'results': [{'board': 'a', 'cleanup': {'pass': True, 'state': 'bootloader invalidated',
                                                             'evidence': {'zeroPage': '0x0', 'verified': True}}}]}
        for report in (install, restore, invalidate):
            self.assertEqual({row[0] for row in collect.report_rows(7, report).values()}, {'OK'})

    def test_failed_tinyuf2_manual_reports_have_a_failed_cell(self):
        survivor = [{'pid': 9, 'start': 123, 'cmd': ['JLinkExe', '-nogui', '1']}]
        install = {'pass': False, 'caveat': '', 'survivors': survivor,
                   'results': [{'board': 'a', 'installed': {'path': 'tinyuf2.bin', 'sha256': '00'}}]}
        refused = {'pass': False, 'caveat': '', 'results': [], 'error': 'a J-Link install takes --firmware and --sha256'}
        failed = lambda report: {cell: row[0] for cell, row in collect.failed_rows(collect.report_rows(7, report)).items()}
        self.assertEqual(failed(install), {'report pass': 'Failed: survivors JLinkExe'})
        restore = {'pass': False, 'caveat': '', 'survivors': survivor, 'error': 'restore left children that survived SIGKILL', 'results': []}
        self.assertEqual(failed(restore), {'report error': 'Failed: restore left children that survived SIGKILL',
                                           'report pass': 'Failed: survivors JLinkExe'}, 'the survivors are named beside the error')
        self.assertEqual(failed(refused), {'report error': 'Failed: a J-Link install takes --firmware and --sha256'})
        self.assertEqual(failed({'pass': False, 'caveat': '', 'results': []}), {'report pass': 'Failed: pass is not true'})

    def test_a_survivors_signature_names_their_commands_not_their_pids(self):
        def sig(*survivors):
            outcome = collect.report_rows(7, {'pass': False, 'caveat': '', 'results': [], 'survivors': list(survivors)})['report pass'][0]
            return collect.cell_signature('report pass', outcome)
        jlink = {'pid': 9, 'start': 123, 'cmd': ['JLinkExe', '-USB', '1']}
        esptool = {'pid': 10, 'start': 124, 'cmd': ['/usr/bin/esptool', '--port', '/dev/ttyACM0']}
        self.assertEqual(sig(jlink, esptool), 'report pass: Failed: survivors JLinkExe, esptool')
        self.assertEqual(sig(jlink, esptool), sig({**jlink, 'pid': 90, 'start': 456}, {**esptool, 'pid': 91, 'start': None}))
        self.assertEqual(sig({'cmd': 1}, {'cmd': 'JLinkExe -USB'}, {'cmd': []}, {}), 'report pass: Failed: survivors ?, ?, ?, ?')

    def test_a_survivor_is_named_by_its_container_its_helper_script_or_its_command(self):
        name = lambda cmd: collect.survivor_name({'pid': 9, 'start': 1, 'cmd': cmd})
        self.assertEqual(collect.survivor_name({'container': 'hil-flash-1'}), 'hil-flash-1')
        session, dfu = name(['/usr/bin/python3', '-B', '/w/esp_session.py']), name(['/usr/bin/python3.11', '-B', 'usb_dfu.py'])
        self.assertEqual((session, dfu), ('esp_session.py', 'usb_dfu.py'), 'hil_test.py spawns its helpers as [sys.executable, -B, script]')
        self.assertEqual(name(['/opt/SEGGER/JLinkExe', '-USB', '1']), 'JLinkExe')
        self.assertEqual(name(['/usr/bin/python3', '-m', 'esptool']), 'python3', 'only the producer\'s helper shape names its script')
        for cmd in ([None], [1], [''], ['.'], [{'x': 'a/y'}], [], 'JLinkExe', None,
                    ['python3', '-B', None], ['python3', '-B', ''], ['python3', '-B', '.'], ['python3', '-B']):
            self.assertEqual(name(cmd), '?', cmd)
        self.assertEqual(collect.survivor_name({'container': 5}), '?')
        spaced = lambda tool: collect.cell_signature('report pass', collect.report_rows(7, {'pass': False, 'caveat': '', 'results': [],
            'survivors': [{'cmd': ['/x/my  prog']}, {'cmd': [tool]}]})['report pass'][0])
        self.assertEqual(spaced('JLinkExe'), 'report pass: Failed: survivors my prog, JLinkExe')
        self.assertNotEqual(spaced('JLinkExe'), spaced('esptool'), 'a name\'s spaces never cut the signature short')

    def test_a_cleanup_survivors_signature_keeps_the_error_and_names_their_commands_not_their_pids(self):
        def sig(pid, start, detail='StepFail: children survived SIGKILL; no flash may overlap them'):
            survivors = [{'pid': pid, 'start': start, 'cmd': ['/opt/SEGGER/JLinkExe', '-USB', '1']}]
            cleanup = {'pass': False, 'state': 'unknown', 'survivors': survivors, 'detail': detail}
            report = {'pass': False, 'caveat': '', 'results': [{'board': 'a', 'pass': False, 'steps': [], 'cleanup': cleanup}]}
            return collect.cell_signature('a cleanup', collect.report_rows(7, report)['a cleanup'][0])
        self.assertEqual(sig(9, 123), 'a cleanup: Failed: StepFail: children survived SIGKILL; no flash may overlap them; survivors JLinkExe')
        self.assertEqual(sig(9, 123), sig(90, 456))
        self.assertNotEqual(sig(9, 123, 'Interrupted: interrupted by SIGTERM'), sig(9, 123, 'StepFail: JLinkExe timed out after 60s'),
                            'clean_up appends survivors after any error')
        cleanup = {'pass': False, 'state': 'unknown', 'survivors': [], 'detail': 'StepFail: readback differs'}
        report = {'pass': False, 'caveat': '', 'results': [{'board': 'a', 'pass': False, 'steps': [], 'cleanup': cleanup}]}
        self.assertEqual(collect.report_rows(7, report)['a cleanup'][0], 'Failed: StepFail: readback differs')

    def test_a_report_pass_cell_is_placed_against_the_base_reports_survivors(self):
        def report(cmd, **extra):
            return json.dumps({'pass': False, 'caveat': '', 'results': [], 'survivors': [{'pid': 9, 'start': 1, 'cmd': cmd}], **extra})
        session = ['/usr/bin/python3', '-B', 'esp_session.py']
        self.logs['3'] = self.uf2_log(report(session))
        self.logs['40'] = self.uf2_log(report(session, error='restore left children that survived SIGKILL'))
        self.assertEqual([(x['cell'], x['onBase']) for x in self.entries(JOB.format(3))[0]['cells']], [('report pass', 'same-failure')],
                         'the base failed on the same survivors, beside its error')
        self.logs['40'] = self.uf2_log(report(['/usr/bin/python3', '-B', 'usb_dfu.py'], error='restore left children that survived SIGKILL'))
        self.assertEqual([x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']], ['other-failure'])

    def test_a_cleanup_survivor_is_placed_against_the_base_by_its_error_and_name_not_its_pid(self):
        passed = json.loads((UF2 / 'pass.json').read_text())['results'][0]
        def report(error, pid):
            cleanup = {'pass': False, 'state': 'unknown', 'detail': error, 'evidence': {}, 'heldAfter': True,
                       'survivors': [{'pid': pid, 'start': pid * 10, 'cmd': ['/opt/SEGGER/JLinkExe', '-USB', '1']}]}
            return json.dumps({'pass': False, 'caveat': '', 'results': [{**passed, 'pass': False, 'cleanup': cleanup}]})
        sigterm = 'Interrupted: interrupted by SIGTERM'
        self.logs['3'], self.logs['40'] = self.uf2_log(report(sigterm, 9)), self.uf2_log(report(sigterm, 90))
        c = self.entries(JOB.format(3))[0]
        self.assertEqual([(x['cell'], x['signature'], x['onBase']) for x in c['cells']],
                         [(f'{S3} cleanup', f'{S3} cleanup: Failed: {sigterm}; survivors JLinkExe', 'same-failure')])
        self.logs['40'] = self.uf2_log(report('StepFail: JLinkExe timed out after 60s', 9))
        self.assertEqual([x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']], ['other-failure'], 'the error still signs')

    def test_a_jobs_signature_is_the_same_when_only_survivor_pids_change(self):
        def entry(pid, start):
            self.logs['3'] = self.uf2_log(json.dumps({'pass': False, 'caveat': '', 'results': [],
                                                      'survivors': [{'pid': pid, 'start': start, 'cmd': ['JLinkExe', '-USB', '1']}]}))
            return self.entries(JOB.format(3))[0]
        first, second = entry(9, 123), entry(90, 456)
        self.assertEqual((first['firstError'], first['signature']), (second['firstError'], second['signature']))
        self.assertEqual(first['firstError'], 'report  pass  ...  Failed: survivors JLinkExe')

    def test_a_malformed_survivor_command_is_named_not_a_traceback(self):
        self.logs['3'] = self.uf2_log('{"pass": false, "caveat": "", "results": [], "survivors": [{"cmd": 5}, {"cmd": {"a": 1}}, {"cmd": [null]}]}')
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['error'], [x['signature'] for x in c['cells']]), (None, ['report pass: Failed: survivors ?, ?, ?']))

    def test_a_log_holding_several_tinyuf2_reports_reads_no_cells(self):
        self.logs['3'] = self.uf2_log('fail.json', 'pass.json')
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['cells'], c['cellsError']), ([], 'the log holds 2 TinyUF2 reports (lines 8, 9): cells read none; read its log'),
                         'never the last report alone, nor table rows')
        self.assertEqual(c['error'], None)

    def test_a_base_log_holding_several_tinyuf2_reports_places_no_cell(self):
        self.logs['3'] = self.uf2_log('fail.json')
        for conclusion in ('failure', 'success'):
            self.run_jobs['70'][0]['conclusion'] = conclusion
            self.logs['40'] = self.uf2_log('fail.json', 'pass.json')
            c = self.entries(JOB.format(3))[0]
            self.assertEqual((c['error'], [x['onBase'] for x in c['cells']]), (None, [None]), conclusion)
            self.assertEqual(c['base']['cellsError'], 'the log holds 2 TinyUF2 reports (lines 8, 9): cells read none; read its log')
            self.assertNotIn('cellsError', c)

    def test_report_level_cells_are_passed_against_a_base_report_and_not_run_without_one(self):
        self.logs['3'] = self.uf2_log('{"pass": false, "caveat": "", "error": "roster rejected", "results": []}')
        self.logs['40'] = self.uf2_log('pass.json')
        self.assertEqual([(x['cell'], x['onBase'], x['baseLineNo']) for x in self.entries(JOB.format(3))[0]['cells']],
                         [('report error', 'passed', 8)])
        self.logs['3'] = self.uf2_log('{"pass": false, "caveat": "", "results": [], "survivors": [{"pid": 9, "start": 1, "cmd": ["JLinkExe"]}]}')
        self.assertEqual([(x['cell'], x['onBase']) for x in self.entries(JOB.format(3))[0]['cells']], [('report pass', 'passed')])
        self.logs['40'] = log(*STEP[:9], 'Total failed: 1', *STEP[11:])
        self.assertEqual([x['onBase'] for x in self.entries(JOB.format(3))[0]['cells']], ['not-run'], 'a tinyusb base has no report')

    def test_every_tinyuf2_report_is_found_and_other_json_is_not_one(self):
        fail, ok = (UF2 / 'fail.json').read_text().strip(), (UF2 / 'pass.json').read_text().strip()
        self.assertEqual(collect.uf2_reports([fail, '{"pass": true}', '{not json']), [(1, json.loads(fail))])
        self.assertEqual([n for n, _ in collect.uf2_reports([fail, 'plain', ok])], [1, 3])
        self.assertEqual(collect.uf2_reports(['{"results": []}', 'plain']), [])
        self.assertEqual(collect.uf2_reports(['{"pass": true, "results": []}']), [], 'hil_test.py always prints a caveat')
        for bad in ('[null]', '[{"steps": {}}]', '[{"steps": [1]}]', '[{"cleanup": "x"}]', '[], "survivors": [1]',
                    '[{"cleanup": {"survivors": [1]}}]'):
            self.assertEqual(collect.uf2_reports([fail, f'{{"pass": true, "caveat": "", "results": {bad}}}']),
                             [(1, json.loads(fail))], bad)

    def test_a_rate_limit_reading_a_check_or_the_changed_paths_fails_the_whole_call(self):
        for name, setup in (('a job log', lambda: self.logs.__setitem__('3', 'limited')), ('the changed paths', lambda: setattr(self, 'changed', 'limited'))):
            with self.subTest(name):
                self.setUp()
                setup()
                rc, r = self.main(JOB.format(3))
                self.assertEqual((rc, r['rateLimit']['resetAt'], r['rateLimit']['secondary']), (1, '2026-09-21T14:14:20Z', False))
                self.assertIn('API rate limit exceeded', r['error'])

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
                return mock.Mock(returncode=0, stdout=json.dumps({'headRefOid': next(views), 'baseRefName': 'master', 'baseRefOid': BASE, 'mergeable': 'MERGEABLE', 'state': 'OPEN'}).encode(), stderr=b'')
            return real(argv, **kw)
        with mock.patch.object(collect.subprocess, 'run', moving), redirect_stdout(io.StringIO()) as out:
            rc = collect.main(['failures', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(3)])
        self.assertIn('moved', out.getvalue())
        self.assertNotEqual(rc, 0)

    def test_a_log_is_saved_as_utf8(self):
        self.logs['3'] = log(*STEP, 'naïve ✓ 😀')
        c = self.entries(JOB.format(3))[0]
        self.assertNotIn('\\', c['log'], 'a path the judge may paste into a shell')
        self.assertIn('naïve ✓ 😀', Path(c['log']).read_text(encoding='utf-8'))
        self.assertNotIn(b'\r', Path(c['log']).read_bytes(), 'lines end in \\n on Windows too')

    def test_the_detail_lists_the_prs_changed_paths(self):
        rc, r = self.main(JOB.format(3))
        self.assertEqual(json.loads(Path(r['detail']).read_text())['changed'], ['src/host/msc.c', 'test/hil/tiny usb.json'], 'a path with a space stays one path')

    def remember_on(self, head, failures, link=JOB.format(3)):
        with mock.patch.object(sys, 'stdin', stdin_of(json.dumps([{'link': link, 'bucket': 'fail', 'failures': failures}]))):
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
        self.assertEqual(self.cell_priors(), [[{'head': OTHER, 'verdict': 'rig-side', 'firstError': 'probe'}], None], 'another head\'s, not the check read now')
        self.remember_on(OTHER, [{**judged, 'workflow': 'Nightly'}])
        self.assertEqual(self.cell_priors()[0], None, 'another workflow\'s job of the same name')
        self.remember_on(OTHER, [judged, {**judged, 'verdict': 'real'}])
        self.assertEqual([v['verdict'] for v in self.cell_priors()[0]], ['rig-side', 'real'], 'both, for the judge to weigh: ambiguous')
        self.remember_on(OTHER, [{**judged, 'workflow': ''}])
        self.checks[0]['workflow'] = ''
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

    def test_a_run_a_rerun_replaced_on_this_head_is_the_newest_prior(self):
        judged = {'workflow': 'Build', 'check': 'hil (x.json)', 'cell': 'pico host/msc', 'signature': 'pico host/msc: Failed: src/host/msc.c timeout',
                  'verdict': 'real', 'firstError': 'old head'}
        self.remember_on(OTHER, [judged])
        os.utime(collect.evidence_dir('o/r', 5, OTHER) / 'verdicts.json', (1, 1))
        placed = {**judged, 'verdict': 'rig-side', 'firstError': 'attempt 1'}
        self.remember_on(HEAD, [placed, placed], link=JOB.format(2))
        self.assertEqual(self.cell_priors()[0], [{'head': HEAD, 'verdict': 'rig-side', 'firstError': 'attempt 1'}],
                         'the replaced run on this head, newer than the other head, once')

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

    def test_a_job_cancelled_before_any_step_ran_reads_no_log_and_no_base(self):
        never = self.run_job(5, 1, None, name='hil (${{ matrix.display }})', conclusion='cancelled', steps=[], runner_id=None)
        self.checks.append({'name': never['name'], 'workflow': 'Build', 'bucket': 'cancel', 'link': JOB.format(5)})
        for runner in (None, 0):
            self.jobs['5'] = {**never, 'runner_id': runner}
            self.calls.clear()
            with mock.patch.object(collect, 'actions_log', side_effect=AssertionError('a log was read')):
                c = self.entries(JOB.format(5))[0]
                out = io.StringIO()
                with redirect_stdout(out):
                    collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(5)])
            self.assertEqual({k: c[k] for k in ('cancelledBeforeStart', 'log', 'base', 'cells', 'files', 'diagnostics', 'error')},
                             {'cancelledBeforeStart': True, 'log': None, 'base': None, 'cells': [], 'files': [], 'diagnostics': [], 'error': None})
            self.assertEqual((c['firstError'], c['signature']), (collect.NEVER_STARTED, collect.NEVER_STARTED))
            self.assertEqual(unsealed(out.getvalue())['bases'], [{'link': JOB.format(5), 'base': None}], 'the same token failures printed')
            self.assertFalse([call for call in self.calls if call[:2] == ['run', 'list']], 'no base run is looked up')
        self.logs['5'] = log(*STEP)
        for over in ({'steps': [{'name': 'Set up job'}]}, {'steps': ...}, {'runner_id': 12}, {'runner_id': ...}, {'conclusion': 'failure'}):
            self.jobs['5'] = {k: v for k, v in {**never, **over}.items() if v is not ...}
            c = self.entries(JOB.format(5))[0]
            self.assertNotIn('cancelledBeforeStart', c, over)
            self.assertTrue(c['log'], over)

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
        self.check_runs = [greptile_run(name='bot (other)', details_url='https://greptile.com/'), greptile_run(name='bot', details_url='https://greptile.com/x')]
        c = self.entries('https://greptile.com/')[0]
        self.assertEqual(c['error'], 'no reader for this check: its link names no run', 'a run needs both the name and the link')

    def test_an_app_check_run_with_no_actions_run_is_read_from_its_output(self):
        self.checks.append({'name': 'Greptile Review', 'workflow': '', 'bucket': 'fail', 'link': GREPTILE})
        self.check_runs = [greptile_run(id=1, conclusion='success', title='Base review', started_at='2026-10-20T11:00:00Z',
                                        summary='Greptile has reviewed the Pull Request.\n\n6 files reviewed, 0 comments added.'),
                           greptile_run()]
        c = self.entries(GREPTILE)[0]
        self.assertEqual({k: c[k] for k in ('provider', 'error', 'app', 'checkRun', 'conclusion', 'output', 'firstError', 'signature', 'files', 'base')},
                         {'provider': 'other', 'error': None, 'app': 'greptile-apps', 'checkRun': 118615785789, 'conclusion': 'failure',
                          'output': {'title': BELOW_TITLE, 'summary': BELOW_SUMMARY}, 'firstError': BELOW_TITLE, 'signature': BELOW_TITLE,
                          'files': [], 'base': None})
        self.assertEqual(c['diagnostics'], [BELOW_TITLE, BELOW_SUMMARY])
        self.assertEqual(Path(c['log']).read_text(encoding='utf-8').splitlines(),
                         ['check run 118615785789 by greptile-apps: completed/failure', BELOW_TITLE, BELOW_SUMMARY])
        [listed] = [x for x in self.calls if x[:2] == ['api', '--paginate'] and '/check-runs?' in x[3]]
        self.assertEqual(listed[3], f'repos/o/r/commits/{HEAD}/check-runs?check_name=Greptile+Review&filter=all&per_page=100')

    def test_app_check_runs_it_cannot_order_or_read_are_an_entry_error(self):
        self.checks.append({'name': 'Greptile Review', 'workflow': '', 'bucket': 'fail', 'link': GREPTILE})
        for runs, error in (([greptile_run(), greptile_run(id=2)], 'check runs 118615785789, 2 name this link and started together'),
                            ([greptile_run(), greptile_run(id=2, started_at=None)], 'check runs 118615785789, 2 name this link, one undated'),
                            ([greptile_run(title='', summary='')], 'check run 118615785789 reports no output title or summary')):
            with self.subTest(error):
                self.check_runs = runs
                self.assertTrue(self.entries(GREPTILE)[0]['error'].startswith(error))
        self.check_runs = 'limited'
        rc, r = self.main(GREPTILE)
        self.assertEqual(rc, 1)
        self.assertIn('rate limit', r['error'])

    T1, T2, T3 = ('01:00:00Z', '01:05:00Z'), ('02:00:00Z', '02:05:00Z'), ('03:00:00Z', '03:05:00Z')

    def run_job(self, job, attempt, times, **over):
        start, end = times or (None, None)
        return {'id': job, 'name': 'hil (x.json)', 'workflow_name': 'Build', 'head_sha': HEAD, 'run_id': 7, 'run_attempt': attempt,
                'status': 'completed', 'conclusion': 'failure', 'started_at': start, 'completed_at': end, 'runner_id': 5, **over}

    def listed_as(self, listed, earlier):
        """The failing check listed under job `listed` (the last of `earlier`'s attempts + 1), its earlier attempts given."""
        self.jobs[str(listed['id'])] = listed
        self.logs[str(listed['id'])] = self.logs['3']
        for j in (j for jobs in earlier.values() for j in jobs):
            self.jobs[str(j['id'])], self.logs[str(j['id'])] = j, self.logs['3']
        self.attempts = earlier
        self.checks[0]['link'] = JOB.format(listed['id'])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = collect.main(['inventory', '--repo', 'o/r', '--pr', '5', '--head', HEAD])
        r = unsealed(out.getvalue())
        return rc, r, (next(c for c in r['checks'] if c['workflow'] == 'Build') if rc == 0 else None)

    def executed_before(self, link):
        return self.entries(link)[0]['executedBefore']

    def test_a_job_a_rerun_copied_is_listed_as_the_job_that_executed_it(self):
        # #3956: attempt 3 re-ran another job; this one was copied from attempt 2, which executed it again after attempt 1
        self.calls.clear()
        rc, r, c = self.listed_as(self.run_job(13, 3, self.T2), {2: [self.run_job(12, 2, self.T2)], 1: [self.run_job(11, 1, self.T1)]})
        self.assertEqual((rc, c['link'], c['attempt'], c['aliases']), (0, JOB.format(12), 'actions:12', [JOB.format(13)]))
        self.assertNotIn('executedBefore', c, 'evidence for the judge, not the caller\'s inventory')
        at = lambda want: max(i for i, call in enumerate(self.calls) if want(call))
        self.assertGreater(at(lambda call: call[:2] == ['pr', 'view']), at(lambda call: '/attempts/' in ' '.join(call)), 'the head is read again after')
        entry = self.entries(JOB.format(12))[0]
        self.assertEqual((entry['runAttempt'], entry['executedBefore'], entry['log'].endswith('actions-12.log')), (2, True, True))
        rc, r = self.main(JOB.format(13))
        self.assertIn('stale snapshot', r['error'], 'the copy\'s link is not a check of its own')

    def test_a_copy_of_a_copy_resolves_to_the_first_execution_once_per_attempt(self):
        rc, r, c = self.listed_as(self.run_job(13, 3, self.T1), {2: [self.run_job(12, 2, self.T1)], 1: [self.run_job(11, 1, self.T1)]})
        self.assertEqual((c['link'], c['aliases']), (JOB.format(11), [JOB.format(13), JOB.format(12)]))
        self.assertIs(self.executed_before(JOB.format(11)), False, 'attempt 1 executed it first')
        self.calls.clear()
        self.checks.append({**self.checks[0], 'name': 'build (y)', 'link': JOB.format(23)})
        self.jobs['23'] = self.run_job(23, 3, self.T1, name='build (y)')
        self.attempts = {2: [self.run_job(12, 2, self.T1), self.run_job(22, 2, self.T1, name='build (y)')],
                         1: [self.run_job(11, 1, self.T1), self.run_job(21, 1, self.T1, name='build (y)')]}
        self.listed_as(self.jobs['13'], self.attempts)
        self.assertEqual(sorted(c[3] for c in self.calls if '/attempts/' in ' '.join(c)),
                         [f'repos/o/r/actions/runs/7/attempts/{n}/jobs?per_page=100' for n in (1, 2)], 'one read per attempt, shared')

    def test_a_job_of_another_head_is_not_resolved(self):
        rc, r, c = self.listed_as(self.run_job(13, 3, self.T2, head_sha='f' * 40), {2: [self.run_job(12, 2, self.T2)], 1: []})
        self.assertEqual((c['link'], c['aliases']), (JOB.format(13), []))

    def test_legs_a_truncated_name_shares_are_told_apart_by_their_execution(self):
        twin = self.run_job(14, 2, self.T1, runner_id=6)
        rc, r, c = self.listed_as(self.run_job(13, 3, self.T2), {2: [twin, self.run_job(12, 2, self.T2)], 1: []})
        self.assertEqual((c['link'], c['aliases']), (JOB.format(12), [JOB.format(13)]))

    def test_a_job_a_rerun_executed_again_is_its_own(self):
        rc, r, c = self.listed_as(self.run_job(13, 3, self.T3), {2: [self.run_job(12, 2, self.T2)], 1: [self.run_job(11, 1, self.T1)]})
        self.assertEqual((c['link'], c['aliases']), (JOB.format(13), []))
        self.assertIs(self.executed_before(JOB.format(13)), True)

    def test_an_execution_after_a_skipped_attempt_was_executed_before(self):
        rc, r, c = self.listed_as(self.run_job(13, 3, self.T3), {2: [], 1: [self.run_job(11, 1, self.T1)]})
        self.assertEqual(c['link'], JOB.format(13))
        self.assertIs(self.executed_before(JOB.format(13)), True)
        rc, r, c = self.listed_as(self.run_job(13, 3, self.T3), {2: [], 1: []})
        self.assertIs(self.executed_before(JOB.format(13)), False, 'first executed in a later attempt')
        self.listed_as(self.run_job(13, 3, self.T3), {2: [self.run_job(12, 2, self.T2, conclusion='skipped')], 1: []})
        self.assertIs(self.executed_before(JOB.format(13)), False, 'a skipped job has times but ran nothing')
        self.calls.clear()
        rc, r, c = self.listed_as(self.run_job(3, 1, self.T1), {})
        self.assertIs(self.executed_before(JOB.format(3)), False)
        self.assertFalse([c for c in self.calls if '/attempts/' in ' '.join(c)], 'attempt 1 has nothing before it')

    def test_what_cannot_be_told_apart_is_not_resolved(self):
        # the listed job stays itself, and whether it executed before cannot be told
        for why, listed, earlier in [
            ('two candidates', self.run_job(13, 3, self.T2), {2: [self.run_job(12, 2, self.T2), self.run_job(14, 2, self.T2)], 1: []}),
            ('no times', self.run_job(13, 3, None), {2: [self.run_job(12, 2, None)], 1: []}),
            ('another runner', self.run_job(13, 3, self.T2), {2: [self.run_job(12, 2, self.T2, runner_id=6)], 1: []}),
            ('another outcome', self.run_job(13, 3, self.T2), {2: [self.run_job(12, 2, self.T2, conclusion='cancelled')], 1: []}),
            ('never started', self.run_job(13, 3, None), {2: [], 1: []}),
            ('an earlier one never started', self.run_job(13, 3, self.T3), {2: [self.run_job(12, 2, None)], 1: []}),
        ]:
            rc, r, c = self.listed_as(listed, earlier)
            self.assertEqual((rc, c['link'], c['aliases']), (0, JOB.format(13), []), why)
            self.assertIsNone(self.executed_before(JOB.format(13)), why)
        self.listed_as(self.run_job(13, 2, self.T2), {1: [self.run_job(12, 1, self.T2)]})
        self.jobs['13'] = {**self.jobs['13'], 'id': 12}
        out = io.StringIO()
        with redirect_stdout(out):
            collect.main(['inventory', '--repo', 'o/r', '--pr', '5', '--head', HEAD])
        self.assertEqual(unsealed(out.getvalue())['checks'][0]['link'], JOB.format(13), 'a record not of the listed job is not resolved')

    def test_attempt_pages_are_read_whole_or_not_at_all(self):
        copy, executed = self.run_job(13, 2, self.T2), self.run_job(12, 1, self.T2)
        filler = [self.run_job(100 + i, 1, self.T1, name=f'other {i}') for i in range(2)]
        self.paged = {1: [{'total_count': 3, 'jobs': filler}, {'total_count': 3, 'jobs': [executed]}]}
        rc, r, c = self.listed_as(copy, {})
        self.assertEqual(c['link'], JOB.format(12), 'the match on the second page')
        self.paged = {1: [{'total_count': 4, 'jobs': filler + [executed]}]}
        rc, r, c = self.listed_as(copy, {})
        self.assertEqual(rc, 1)
        self.assertIn('run 7 attempt 1: listed 3 jobs of 4', r['error'])

    def test_bases_reads_the_job_that_executed(self):
        self.listed_as(self.run_job(13, 2, self.T2), {1: [self.run_job(12, 1, self.T2)]})
        self.calls.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(12)])
        self.assertEqual((rc, [b['link'] for b in unsealed(out.getvalue())['bases']]), (0, [JOB.format(12)]))
        self.assertFalse([c for c in self.calls if 'branches/master' in ' '.join(map(str, c))], 'bases judges no required check')

    def test_only_the_runs_of_the_asked_links_are_resolved(self):
        other = 'https://github.com/o/r/actions/runs/8/job/5'
        self.checks.append({'name': 'lint', 'workflow': 'Lint', 'bucket': 'fail', 'link': other})
        self.entries(JOB.format(3))
        self.assertIn(['api', 'repos/o/r/actions/jobs/3'], self.calls)
        self.assertNotIn(['api', 'repos/o/r/actions/jobs/5'], self.calls, 'a check of another run is not asked about')

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
        self.jobs['4'] = {**self.jobs['3'], 'id': 4, 'name': 'build (x)'}
        self.logs['4'] = self.logs['3']
        self.run_jobs['70'].append({'name': 'build (x)', 'conclusion': 'success', 'databaseId': 42})
        self.logs['42'] = log('pico  host/msc  ...  OK in 1.0s')
        self.entries(JOB.format(3), JOB.format(4))
        self.assertEqual(sum(c[:2] == ['run', 'list'] for c in self.calls), 2, 'one per listing, commit then branch')
        self.assertEqual([c[2] for c in self.calls if c[:2] == ['run', 'view']], ['72', '70'], 'each run is read once')

    def test_a_log_that_is_not_utf8_is_still_read(self):
        self.logs['3'] = log(*STEP).encode().replace(b'timeout', b'time\xffout')
        c = self.entries(JOB.format(3))[0]
        self.assertEqual(c['error'], None)
        self.assertIn('time\ufffdout', c['firstError'])

    def test_a_run_name_is_not_the_workflows_name(self):
        # a workflow's run-name: (tinyuf2's `HIL PR #<n>`) is what a job record's workflow_name carries
        for job in ('3', '40'):
            self.jobs[job]['workflow_name'] = 'HIL PR #5'
        judged = {'workflow': 'Build', 'check': 'hil (x.json)', 'cell': 'pico host/msc', 'signature': 'pico host/msc: Failed: src/host/msc.c timeout',
                  'verdict': 'rig-side', 'firstError': 'probe'}
        self.remember_on(OTHER, [judged])
        c = self.entries(JOB.format(3))[0]
        self.assertEqual((c['error'], c['workflow'], c['base']['runId'], c['cells'][0]['prior'][0]['head']), (None, 'Build', 70, OTHER),
                         'the inventory\'s workflow, which verdicts are stored under')
        self.assertEqual({x[x.index('--workflow') + 1] for x in self.calls if x[:2] == ['run', 'list']}, {'1000'}, 'base runs by workflow id')
        out = io.StringIO()
        with redirect_stdout(out):
            collect.main(['bases', '--repo', 'o/r', '--pr', '5', '--head', HEAD, '--check', JOB.format(3)])
        self.assertEqual(unsealed(out.getvalue())['bases'], [{'link': JOB.format(3), 'base': '70:40:failure'}])

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
        self.assertEqual(json.loads(Path(r['detail']).read_text()), {'head': HEAD, 'baseRef': 'master', 'baseSha': BASE, 'checks': []})
        self.assertFalse([c for c in self.calls if c[:2] == ['api', '--paginate'] and '/pulls/' in c[3]], 'no changed paths')
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

    def greptile_gate(self, *runs, bucket='fail'):
        self.checks.append({'name': 'Greptile Review', 'workflow': '', 'bucket': bucket, 'link': GREPTILE})
        self.check_runs = list(runs)
        return self.main(extra=['--gate', GREPTILE])

    def test_greptiles_below_threshold_result_is_one_complete_gate_failure(self):
        rc, r = self.greptile_gate(greptile_run())
        self.assertEqual(rc, 0, r)
        self.assertEqual(r['gates'], [{'link': GREPTILE, 'failures': [{'firstError': BELOW_TITLE, 'signature': BELOW_TITLE, 'complete': True}]}])
        self.assertEqual(json.loads(Path(r['detail']).read_text())['checks'], [], 'the judge reads nothing of it')

    def test_any_other_greptile_check_is_one_incomplete_gate_failure_saying_what_was_read(self):
        cases = ((greptile_run(app={'slug': 'greptile-lookalike'}), "check run 118615785789 is by app 'greptile-lookalike', not greptile-apps"),
                 (greptile_run(title='Base review', summary='Greptile could not finish this review.'),
                  "check run 118615785789 completed/failure is not a confidence threshold result: 'Base review'"),
                 (greptile_run(summary='Greptile reviewed this pull request.'), 'check run 118615785789 completed/failure is not a confidence threshold result'),
                 (greptile_run(conclusion='cancelled'), 'check run 118615785789 completed/cancelled is not a confidence threshold result'),
                 (greptile_run(details_url='https://app.greptile.com/x'), 'Greptile check not read: no reader for this check: its link names no run'))
        for run, why in cases:
            with self.subTest(why):
                self.checks = self.checks[:3]
                rc, r = self.greptile_gate(run)
                self.assertEqual(rc, 0, r)
                self.assertTrue(self.only(r).startswith(why), self.only(r))

    def test_a_greptile_gate_no_longer_failing_is_stale(self):
        rc, r = self.greptile_gate(greptile_run(), bucket='pass')
        self.assertEqual(rc, 1)
        self.assertIn(f'stale snapshot: no longer failing checks of the head: {GREPTILE}', r['error'])

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
        with redirect_stdout(out), mock.patch('sys.stdin', stdin_of(stdin)):
            rc = collect.main([command, '--repo', 'o/r', '--pr', '5', '--head', HEAD, *argv])
        self.printed = out.getvalue()
        return rc, unsealed(self.printed)

    def entry(self, n, size, link=JOB):
        return {**self.ENTRY, 'link': link.format(n), 'failures': [{**self.ENTRY['failures'][0], 'firstError': 'x' * size}]}

    def test_recall_returns_what_was_remembered_unchanged_and_skips_unknown_links(self):
        # Raw UTF-8 on stdin, stored as UTF-8 whatever the locale: 😀 is in no Windows code page.
        self.assertEqual(self.main('remember', stdin=json.dumps([self.ENTRY], ensure_ascii=False)), (0, {'head': HEAD}))
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
        # strict UTF-8: no BOM, no encoded surrogate (which once crashed the write, leaving its temp file)
        for text in (b'["\xff"]', b'\xef\xbb\xbf[]', b'[{"link": "\xed\xa0\x80", "bucket": "fail", "failures": []}]', 'not json', json.dumps({'link': 'x'}), json.dumps([{'link': 'x', 'bucket': 'fail'}]),
                     json.dumps([{'link': 'x', 'bucket': 'fail', 'patch': [7]}]), json.dumps([{'link': 'x', 'bucket': 'fail', 'failures': [], 'patch': []}])):
            rc, r = self.main('remember', stdin=text)
            self.assertEqual(rc, 1, text)
            self.assertIn('verdicts on stdin', r['error'])
        self.assertFalse(collect.evidence_dir('o/r', 5, HEAD).exists() and any(collect.evidence_dir('o/r', 5, HEAD).iterdir()), 'nothing written')

    def test_remember_waits_out_a_reader_holding_the_verdicts_open(self):
        self.main('remember', stdin=json.dumps([self.ENTRY]))
        held = open(collect.evidence_dir('o/r', 5, HEAD) / 'verdicts.json', 'rb')  # Windows: os.replace onto it fails
        self.addCleanup(held.close)
        threading.Timer(0.2, held.close).start()
        self.assertEqual(self.main('remember', stdin=json.dumps([{**self.ENTRY, 'link': JOB.format(4)}]))[0], 0)
        self.assertEqual(len(self.main('recall', '--check', JOB.format(3), '--check', JOB.format(4))[1]['verdicts']), 2)


if __name__ == '__main__':
    unittest.main()
