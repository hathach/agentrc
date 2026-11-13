"""Tests for pr-babysit's ci_recover.py: when a queued run with no job is a suspected orphan, and that its cancel and
re-run are each sent at most once per run attempt, whatever answers or relaunches (#536)."""
import base64
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'skills/pr-babysit/scripts'))
spec = importlib.util.spec_from_file_location('ci_recover', ROOT / 'skills/pr-babysit/scripts/ci_recover.py')
recover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recover)
import harvest  # noqa: E402  the script's GET helpers, which call facts.run through harvest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

HEAD = 'a' * 40
NOW = datetime(2026, 11, 11, 21, 52, tzinfo=timezone.utc)
# hathach/agentrc#533's tests run 43095438193: queued at 20:20:25Z with no job, while later runs completed.
QUEUED = '2026-11-11T20:20:25Z'
WORKFLOW = 'on: pull_request\nconcurrency:\n  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}\n'


def setUpModule():
    guard_survivors(__name__)


def a_run(id_=43095438193, **over):
    return {'id': id_, 'name': 'tests', 'path': '.github/workflows/tests.yml', 'workflow_id': 7, 'status': 'queued', 'conclusion': None,
            'run_attempt': 1, 'created_at': QUEUED, 'run_started_at': QUEUED, 'head_sha': HEAD, 'head_branch': 'captain-prune',
            'head_repository': {'full_name': 'o/r'}, 'event': 'pull_request', **over}


class GitHub:
    """A fake gh: the PR, the runs, their jobs and workflow files as GET answers (run), each POST (attempt) applied after
    `cancel_after` and `rerun_after` polls of the run, or lost (`lose`) or refused (`refuse`)."""

    def __init__(self, runs=None, workflow=WORKFLOW, newest='2026-11-11T21:45:00Z', jobs=0, cancel_after=1, rerun_after=0,
                 lose=(), refuse=(), cancel_as='cancelled'):
        self.runs = {r['id']: r for r in (runs or [a_run()])}
        self.workflow, self.newest, self.jobs = workflow, newest, jobs
        self.cancel_after, self.rerun_after, self.lose, self.refuse = cancel_after, rerun_after, set(lose), set(refuse)
        self.cancel_as = cancel_as  # the conclusion the run reads once completed after a cancel
        self.head, self.state, self.posts, self.reads = HEAD, 'OPEN', [], []
        self.after_list = None  # called once the runs on the head have been listed: a race
        self.after_cancel = None  # called once a cancel was sent: a race

    def run(self, *argv, ok=(0,)):
        path = argv[-1]
        self.reads.append(path)
        if argv[1:3] == ('pr', 'view'):
            return 0, json.dumps({'state': self.state, 'headRefOid': self.head, 'headRefName': 'captain-prune',
                                  'headRepository': {'nameWithOwner': 'o/r'}})
        if f'head_sha={HEAD}' in path:
            got = json.dumps([{'workflow_runs': list(self.runs.values())}])
            if self.after_list:
                self.after_list()
                self.after_list = None
            return 0, got
        if 'status=completed&per_page=1' in path:
            return 0, json.dumps({'workflow_runs': [{'id': 43105843911, 'created_at': self.newest}] if self.newest else []})
        if '/actions/runs?status=' in path:
            status = path.split('status=')[1].split('&')[0]
            return 0, json.dumps([{'workflow_runs': [r for r in self.others() if r['status'] == status]}])
        if '/contents/' in path:
            if self.workflow is None:
                raise harvest.Unusable(f'gh api {path}: Not Found (HTTP 404)')
            return 0, json.dumps({'content': base64.b64encode(self.workflow.encode()).decode()})
        if '/jobs?' in path:
            return 0, json.dumps({'total_count': self.jobs})
        if '/actions/runs/' in path:
            r = self.runs[int(path.rsplit('/', 1)[1])]
            pending = r.pop('_pending', None)
            if pending:
                pending[0] -= 1
                if pending[0] < 0:
                    r.update(pending[1])
                else:
                    r['_pending'] = pending
            return 0, json.dumps({k: v for k, v in r.items() if not k.startswith('_')})
        raise harvest.Unusable(f'unexpected {argv}')

    def others(self):
        return getattr(self, 'unfinished', [])

    def attempt(self, *argv, input=None):
        assert argv[:4] == ('gh', 'api', '-X', 'POST'), argv
        run_id, what = argv[4].split('/')[-2:]
        self.posts.append((int(run_id), what))
        if what in self.refuse:
            return 1, '', 'gh: Resource not accessible by integration (HTTP 403)'
        r = self.runs[int(run_id)]
        if what == 'cancel':
            r['_pending'] = [self.cancel_after, {'status': 'completed', 'conclusion': self.cancel_as}]
            if self.after_cancel:
                self.after_cancel()
        else:
            r['_pending'] = [self.rerun_after, {'status': 'queued', 'conclusion': None, 'run_attempt': r['run_attempt'] + 1,
                                                'run_started_at': '2026-11-11T21:53:00Z'}]
        if what in self.lose:
            return 1, '', 'gh: Post "https://api.github.com/...": net/http: timeout awaiting response headers'
        return 0, '', ''


class Recover(unittest.TestCase):
    def setUp(self):
        self.fresh()
        self.clock = [0.0]

    def fresh(self):
        """A new record directory: another checkout's, with no record of an earlier case."""
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def go(self, gh, wait=30):
        out = io.StringIO()
        sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)  # noqa: E731
        with mock.patch.object(harvest, 'run', gh.run), mock.patch.object(recover, 'attempt', gh.attempt), \
                mock.patch.object(recover, 'record_dir', lambda: self.dir), mock.patch.object(recover, 'now', lambda: NOW), \
                mock.patch.object(recover.time, 'sleep', sleep), mock.patch.object(recover.time, 'monotonic', lambda: self.clock[0]), \
                redirect_stdout(out):
            code = recover.report(recover.collect, ['--pr', '533', '--head', HEAD, '--repo', 'o/r', '--wait-minutes', str(wait)], seal=True)
        return code, json.loads(out.getvalue())

    def records(self):
        return sorted(p.name for p in self.dir.glob('*.json'))

    def test_a_suspected_orphan_is_cancelled_and_rerun_once_its_cancel_completes(self):
        gh = GitHub(cancel_after=2)
        code, out = self.go(gh)
        self.assertEqual((code, out['outcome']), (0, 'recovered'), out)
        self.assertEqual(gh.posts, [(43095438193, 'cancel'), (43095438193, 'rerun')])
        run = out['runs'][0]
        self.assertEqual((run['runId'], run['attempt'], run['newAttempt'], run['outcome']), (43095438193, 1, 2, 'recovered'))
        self.assertEqual(run['evidence'][:2], ['queued since 2026-11-11T20:20:25Z (91 minutes), attempt 1 with no job',
                                               'run 43105843911 created 2026-11-11T21:45:00Z has completed since'])
        self.assertIn("concurrency group '${{ github.workflow }}", run['evidence'][2])
        record = json.loads(Path(run['record']).read_text())
        self.assertEqual(record['steps']['cancel']['answer'], 'sent')
        self.assertEqual((record['steps']['rerun']['answer'], record['newAttempt']), ('sent', 2))
        self.assertEqual(self.records(), ['o-r-43095438193-1.json'])

    def test_a_run_that_is_not_a_suspected_orphan_is_left_alone(self):
        older = {'id': 9, 'workflow_id': 7, 'status': 'in_progress', 'head_branch': 'captain-prune', 'head_repository': {'full_name': 'o/r'}}
        cases = {
            'jobs registered': (GitHub(jobs=2), 'its attempt 1 has 2 job(s)'),
            'recent': (GitHub(runs=[a_run(created_at='2026-11-11T21:40:00Z', run_started_at='2026-11-11T21:40:00Z')]),
                       'queued 12 minutes, under the 30-minute CI wait'),
            "a re-run's attempt still recent": (GitHub(runs=[a_run(run_attempt=2, run_started_at='2026-11-11T21:40:00Z')]), 'queued 12 minutes'),
            'no newer completed run': (GitHub(newest='2026-11-11T20:00:00Z'), 'no run in the repository created since it was queued'),
            'no completed run at all': (GitHub(newest=None), 'no run in the repository created since it was queued'),
            'in progress': (GitHub(runs=[a_run(status='in_progress')]), None),
            'concurrency-held on its PR': (GitHub(), "may be held by run 9 (in_progress)"),
            'workflow file unreadable': (GitHub(workflow=None), 'whether a concurrency group holds it cannot be told'),
            'not on the head branch': (GitHub(runs=[a_run(head_branch='main')]), "not on the PR's head branch"),
        }
        cases['concurrency-held on its PR'][0].unfinished = [older]
        for what, (gh, why) in cases.items():
            with self.subTest(what):
                code, out = self.go(gh)
                self.assertEqual((code, out['outcome'], gh.posts), (0, 'none', []))
                if why:
                    self.assertEqual(out['runs'][0]['outcome'], 'skipped')
                    self.assertIn(why, out['runs'][0]['reason'])
                else:
                    self.assertEqual(out['runs'], [])
                self.assertEqual(self.records(), [], 'nothing recorded')

    def test_only_no_group_or_the_run_id_alone_proves_no_run_can_hold_it(self):
        elsewhere = {'id': 9, 'name': 'lint', 'workflow_id': 8, 'status': 'queued', 'head_branch': 'other', 'event': 'pull_request',
                     'head_repository': {'full_name': 'o/r'}}
        group = 'concurrency:\n  group: %s\n'.__mod__
        for what, workflow, unfinished, held in (
                ('no group', 'on: [push]\n', [elsewhere], False),
                ('the run id alone', 'concurrency: ${{ github.run_id }}\n', [elsewhere], False),
                ('the run id alone, as the group', group('${{github.run_id}}'), [elsewhere], False),
                ('a per-PR group, no other unfinished run', WORKFLOW, [], False),
                ("a per-PR group, another workflow's run on another branch", WORKFLOW, [elsewhere], True),
                ('a constant group', 'on: push\nconcurrency: deploy\n', [elsewhere], True),
                ('a literal that reads like the run id', 'concurrency: github.run_id\n', [elsewhere], True),
                ('the run id beside the workflow', group('ci-${{ github.workflow }}-${{ github.run_id }}'), [elsewhere], True),
                ('a ref compared', group("${{ github.ref == 'refs/heads/main' && 'production' || 'staging' }}"), [elsewhere], True),
                # workflow ci on captain-prune and workflow ci-captain on prune both read ci-captain-prune
                ('names that concatenate alike', group('${{ github.workflow }}-${{ github.head_ref || github.ref }}'),
                 [dict(elsewhere, name='ci-captain', head_branch='prune')], True)):
            with self.subTest(what):
                self.fresh()
                gh = GitHub(workflow=workflow)
                gh.unfinished = unfinished
                out = self.go(gh)[1]
                self.assertEqual(out['outcome'], 'none' if held else 'recovered', out)
                if held:
                    self.assertEqual((out['runs'][0]['outcome'], gh.posts), ('skipped', []))
                    self.assertIn('may be held by run 9 (queued)', out['runs'][0]['reason'])

    def test_a_head_that_moves_or_a_run_that_starts_before_the_cancel_is_left_alone(self):
        gh = GitHub()
        gh.after_list = lambda: setattr(gh, 'head', 'b' * 40)
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('none', []))
        self.assertIn('not open at aaaaaaaaa', out['runs'][0]['reason'])
        gh = GitHub()
        gh.after_list = lambda: gh.runs[43095438193].update(status='in_progress')
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('none', []))
        self.assertIn('it is now in_progress', out['runs'][0]['reason'])
        out = self.go(GitHub(), wait=200)[1]
        self.assertEqual(out['outcome'], 'none', 'a longer wait')
        gh = GitHub()
        gh.state = 'CLOSED'
        self.assertEqual((self.go(gh)[1]['outcome'], gh.posts), ('none', []))

    def test_a_cancel_that_did_not_end_in_cancelled_is_never_rerun(self):
        gh = GitHub(lose={'cancel'}, cancel_as='success')
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('completed', [(43095438193, 'cancel')]), 'it ran and passed: no re-run')
        self.assertIn('completed success', out['runs'][0]['reason'])
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('completed', [(43095438193, 'cancel')]), 'a relaunch sends nothing either')

    def test_a_pr_that_moves_during_the_cancel_withholds_the_rerun(self):
        for what, race, why in (('a new head', lambda gh: setattr(gh, 'head', 'b' * 40), 'not open at aaaaaaaaa'),
                                ('closed', lambda gh: setattr(gh, 'state', 'CLOSED'), 'PR 533 is CLOSED')):
            with self.subTest(what):
                self.fresh()
                gh = GitHub()
                gh.after_cancel = lambda: race(gh)
                out = self.go(gh)[1]
                self.assertEqual((out['outcome'], gh.posts), ('withheld', [(43095438193, 'cancel')]))
                self.assertIn(why, out['runs'][0]['reason'])
                record = json.loads(Path(out['runs'][0]['record']).read_text())
                self.assertEqual(list(record['steps']), ['cancel'])

    def test_a_record_cut_short_before_its_cancel_is_checked_again(self):
        path = self.dir / 'o-r-43095438193-1.json'
        path.write_text(json.dumps({'repo': 'o/r', 'pr': 533, 'head': HEAD, 'runId': 43095438193, 'attempt': 1, 'evidence': ['e'],
                                    'steps': {}}))
        gh = GitHub(runs=[a_run(status='in_progress')])
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('none', []), 'it started since: no cancel')
        self.assertEqual((out['runs'][0]['outcome'], out['runs'][0]['record']), ('skipped', str(path)))
        self.assertIn('it is now in_progress', out['runs'][0]['reason'])
        gh = GitHub()
        gh.unfinished = [{'id': 9, 'name': 'tests', 'workflow_id': 7, 'status': 'in_progress', 'head_branch': 'captain-prune',
                          'event': 'pull_request'}]
        self.assertEqual((self.go(gh)[1]['outcome'], gh.posts), ('none', []), 'its group is held now')
        gh.unfinished = []
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('recovered', [(43095438193, 'cancel'), (43095438193, 'rerun')]))
        self.assertNotEqual(json.loads(path.read_text())['evidence'], ['e'], 'the record carries what was read before its cancel')

    def test_a_lost_cancel_answer_is_observed_and_never_sent_again(self):
        gh = GitHub(lose={'cancel'})
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('recovered', [(43095438193, 'cancel'), (43095438193, 'rerun')]), 'it took: observed')
        record = json.loads(Path(out['runs'][0]['record']).read_text())
        self.assertTrue(record['steps']['cancel']['answer'].startswith('unknown: '))
        self.fresh()
        gh = GitHub(cancel_after=100)
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('uncertain', [(43095438193, 'cancel')]), 'a cancel that never completes')
        self.assertIn('not sent again', out['runs'][0]['reason'])
        gh.cancel_after = 0
        gh.runs[43095438193]['_pending'] = [0, {'status': 'completed', 'conclusion': 'cancelled'}]
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('recovered', [(43095438193, 'cancel'), (43095438193, 'rerun')]),
                         'a relaunch resumes the record once the cancel completed, with no second cancel')

    def test_a_lost_rerun_answer_is_observed_and_never_sent_again(self):
        gh = GitHub(lose={'rerun'})
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], out['runs'][0]['newAttempt']), ('recovered', 2))
        self.fresh()
        gh = GitHub(lose={'rerun'}, rerun_after=100)
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('uncertain', [(43095438193, 'cancel'), (43095438193, 'rerun')]))
        gh.runs[43095438193].pop('_pending')
        out = self.go(gh)[1]
        self.assertEqual(gh.posts, [(43095438193, 'cancel'), (43095438193, 'rerun')], 'a relaunch sends nothing again')
        self.assertEqual(out['outcome'], 'uncertain')

    def test_a_refused_step_fails_and_is_never_sent_again(self):
        for what, refuse, posts in (('cancel', {'cancel'}, [(43095438193, 'cancel')]),
                                    ('rerun', {'rerun'}, [(43095438193, 'cancel'), (43095438193, 'rerun')])):
            with self.subTest(what):
                self.fresh()
                gh = GitHub(refuse=refuse)
                out = self.go(gh)[1]
                self.assertEqual((out['outcome'], gh.posts), ('failed', posts))
                self.assertIn('HTTP 403', out['runs'][0]['reason'])
                if what == 'cancel':
                    gh.refuse = set()
                    self.assertEqual((self.go(gh)[1]['outcome'], gh.posts), ('failed', posts), 'a relaunch sends nothing again')

    def test_a_relaunch_after_a_recovery_sends_nothing_again(self):
        gh = GitHub()
        self.assertEqual(self.go(gh)[1]['outcome'], 'recovered')
        out = self.go(gh)[1]
        self.assertEqual(out['outcome'], 'none', 'the new attempt is recent')
        self.assertEqual(gh.posts, [(43095438193, 'cancel'), (43095438193, 'rerun')])

    def test_a_record_cut_short_after_its_cancel_was_sent_is_observed(self):
        gh = GitHub()
        path = self.dir / 'o-r-43095438193-1.json'
        path.write_text(json.dumps({'repo': 'o/r', 'pr': 533, 'head': HEAD, 'runId': 43095438193, 'attempt': 1, 'evidence': ['e'],
                                    'steps': {'cancel': {'at': '2026-11-11T21:50:00Z'}}}))
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('uncertain', []))
        self.assertIn('the record holds no answer', out['runs'][0]['reason'])
        gh.runs[43095438193].update(status='completed', conclusion='cancelled')
        out = self.go(gh)[1]
        self.assertEqual((out['outcome'], gh.posts), ('recovered', [(43095438193, 'rerun')]), 'the cancel took: the re-run follows once')

    def test_a_read_failing_for_one_run_leaves_the_others_standing(self):
        gh = GitHub(runs=[a_run(), a_run(id_=5, path='.github/workflows/gone.yml')])
        answer = gh.run

        def run(*argv, ok=(0,)):
            if 'gone.yml' in argv[-1]:
                raise harvest.Unusable('gh api: HTTP 500')
            return answer(*argv, ok=ok)
        gh.run = run
        out = self.go(gh)[1]
        self.assertEqual(out['outcome'], 'recovered')
        self.assertEqual([r['outcome'] for r in out['runs']], ['recovered', 'skipped'])

    def test_usage(self):
        for argv in (['--pr', '1', '--head', 'abc', '--wait-minutes', '30'], ['--pr', '1', '--head', HEAD, '--wait-minutes', '0']):
            with self.subTest(argv):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(recover.report(recover.collect, argv), 2)
                self.assertIn('error', json.loads(out.getvalue()))


if __name__ == '__main__':
    unittest.main()
