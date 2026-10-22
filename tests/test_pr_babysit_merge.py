"""Tests for pr-babysit's merge.py: every readiness row on stubbed GitHub, harvest and CI reads, and the merge's argv.
Stubs prove the plumbing only; the first live merge is the real test."""
import copy
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('pr_babysit_merge', SCRIPTS / 'merge.py')
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

HEAD = 'a' * 40
OTHER = 'b' * 40
URL = 'https://github.com/o/r/pull/7'
FP = f'coderabbit,copilot,greptile|coderabbit,greptile|{"0" * 32}'
BASE_ARGS = ['--pr-url', URL, '--base', 'main', '--checkout', '.', '--state-digest']


def bot(name, state='reviewed', sha=HEAD, kind=None, reason='review on the head'):
    return {'bot': name, 'state': state, 'kind': kind, 'sha': sha, 'evidence': ['x'], 'reason': reason, 'retryAt': None}


def launch_output(state=None, result=None, reviews=None):
    st = {'version': 3, 'pin': {'prRepo': 'o/r', 'prBranch': 'fix', 'prUrl': URL, 'remote': 'origin', 'pushUrls': ['u']},
          'expectedHead': HEAD, 'pending': None, 'cyclesUsed': 2, 'maxCycles': 8, 'config': {'pr': 7},
          'acceptedFailures': [], 'deferrals': [], 'holds': [], 'debt': [], 'answeredWith': [], 'decisions': [], 'note': 'naïve ✓', **(state or {})}
    st['digest'] = merge.state_digest(st)
    rv = {'headSha': HEAD, 'fingerprint': FP, 'bots': [bot('coderabbit'), bot('greptile')], 'findings': [], **(reviews or {})}
    res = {'stateDigest': st['digest'], 'status': 'complete', 'pass': True, 'cycles': 2, 'head': HEAD,
           'observation': {'reviewedHead': HEAD, 'lane': 'both', 'reviews': rv,
                           'ci': {'status': 'green', 'headSha': HEAD, 'realFailures': []}, 'actions': {}, 'launchActions': []},
           'state': st, **(result or {})}
    return {'agentCount': 1, 'totalTokens': 1, 'logs': [], 'result': res}


def live(**over):
    pr = {'number': 7, 'url': URL, 'state': 'OPEN', 'isDraft': False, 'title': 'Fix it', 'headRefOid': HEAD, 'headRefName': 'fix',
          'headRepository': {'nameWithOwner': 'o/r'}, 'baseRefName': 'main', 'baseRepository': {'nameWithOwner': 'o/r'},
          'mergeStateStatus': 'CLEAN', 'mergeable': 'MERGEABLE', 'isMergeQueueEnabled': False, 'mergeCommit': None,
          'closingIssuesReferences': {'nodes': [{'number': 3, 'state': 'OPEN', 'url': 'https://github.com/o/r/issues/3'}]}}
    squash = over.pop('squashMergeAllowed', True)
    pr.update(over)
    return {'data': {'repository': {'squashMergeAllowed': squash, 'pullRequest': pr}}}


def setUpModule():
    guard_survivors(__name__)


class World:
    """What GitHub, harvest.py and collect.py answer, and the commands merge.py ran."""

    def __init__(self):
        self.views = [live()]
        self.protection = {'required_status_checks': {'contexts': [], 'checks': []}}
        self.rules = []
        self.runs = []
        self.statuses = []
        self.fresh = {'headSha': HEAD, 'fingerprint': FP, 'bots': [bot('coderabbit'), bot('copilot', 'absent', None), bot('greptile')]}
        self.inventory = {'head': HEAD, 'status': 'green', 'mergeable': 'MERGEABLE', 'pending': 0, 'registered': 12, 'ignored': [],
                          'waiting': '', 'checks': [], 'observedAt': 'now'}
        self.merged = (0, '', '')
        self.ran = []
        self.harvested = []

    def gh_json(self, *argv):
        if argv[:2] == ('api', 'graphql'):
            got = self.views[0] if len(self.views) == 1 else self.views.pop(0)
            if isinstance(got, Exception):
                raise got
            return got
        path = argv[-1]
        if path == 'repos/o/r/branches/main':
            return {'protection': self.protection}
        if path.startswith('repos/o/r/rules/branches/main'):
            return [self.rules]
        if path.startswith(f'repos/o/r/commits/{HEAD}/check-runs'):
            return [{'check_runs': self.runs}]
        if path.startswith(f'repos/o/r/commits/{HEAD}/statuses'):
            return [self.statuses]
        raise AssertionError(f'unexpected gh {argv}')

    def collect(self, argv):
        self.harvested.append(argv)
        return copy.deepcopy(self.fresh)

    def attempt(self, *argv):
        self.ran.append(list(argv))
        if argv[1].endswith('collect.py'):
            return 0, json.dumps(self.inventory) + '\n', ''
        if argv[:3] == ('gh', 'pr', 'merge'):
            return self.merged
        raise AssertionError(f'unexpected command {argv}')

    @property
    def merges(self):
        return [c for c in self.ran if c[:3] == ['gh', 'pr', 'merge']]


class MergeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.world = World()
        for target, name, value in ((merge.harvest, 'gh_json', self.world.gh_json), (merge.harvest, 'collect', self.world.collect),
                                    (merge, 'attempt', self.world.attempt), (merge, 'READBACK_WAIT', 0),
                                    (merge.launch_result, 'checkout', lambda path: {'branch': 'fix', 'head': self.tree_head, 'dirty': []})):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tree_head = HEAD

    def run_it(self, output=None, *extra, digest=None):
        output = output or launch_output()
        path = self.dir / 'w.output'
        path.write_text(json.dumps(output))
        argv = [*BASE_ARGS, digest or output['result']['stateDigest'], '--output', str(path), *extra]
        return merge.collect(argv)

    def assertBlocked(self, got, pattern):
        self.assertEqual(got['outcome'], 'not-ready', got)
        self.assertTrue(any(re.search(pattern, b) for b in got['blockers']), got['blockers'])
        self.assertEqual(self.world.merges, [], 'nothing merged')

    def test_a_ready_pr_is_only_inspected_without_merge(self):
        got = self.run_it()
        self.assertEqual((got['outcome'], got['blockers'], got['admin'], got['checkedHead']), ('ready', [], False, HEAD), got)
        self.assertEqual(self.world.merges, [])
        self.assertEqual(self.world.harvested, [['--pr', '7', '--reviewers', 'coderabbit,copilot,greptile', '--auto-run', 'coderabbit,greptile', '--repo', 'o/r']],
                         "the launch's own harvest, against the PR's repository")
        self.assertEqual(self.world.ran[0][2:], ['inventory', '--repo', 'o/r', '--pr', '7', '--head', HEAD], 'no ignored check')

    def test_a_merge_runs_the_exact_command_and_reads_it_back(self):
        self.world.views = [live(), live(), live(state='MERGED', mergeCommit={'oid': OTHER})]
        got = self.run_it(None, '--merge')
        self.assertEqual(self.world.merges, [['gh', 'pr', 'merge', '7', '-R', 'o/r', '--squash', '--match-head-commit', HEAD,
                                              '--subject', 'Fix it (#7)', '--body', '']])
        self.assertEqual((got['outcome'], got['mergeCommit'], got['mergedHead'], got['admin']), ('merged', OTHER, HEAD, False), got)
        self.assertEqual(got['closingIssues'], [{'number': 3, 'state': 'OPEN', 'url': 'https://github.com/o/r/issues/3'}])

    def test_admin_only_when_github_reports_blocked(self):
        blocked = live(mergeStateStatus='BLOCKED')
        self.world.views = [blocked, blocked, live(state='MERGED', mergeCommit={'oid': OTHER})]
        got = self.run_it(None, '--merge')
        self.assertEqual(self.world.merges[0][-1], '--admin')
        self.assertEqual((got['outcome'], got['admin']), ('merged', True))
        self.assertEqual(got['requirements'], {'mergeStateStatus': 'BLOCKED', 'mergeable': 'MERGEABLE', 'reviewDecision': None, 'requiredChecks': []},
                         'the evidence behind the bypass')
        self.world.__init__()
        self.world.views = [live(mergeStateStatus='UNKNOWN', mergeable='UNKNOWN'), live(), live(mergeStateStatus='UNKNOWN'), live()]
        self.assertEqual(self.run_it()['outcome'], 'ready', 'read again while GitHub computes it')
        for state in ('BEHIND', 'DIRTY', 'UNSTABLE', 'UNKNOWN', 'HAS_HOOKS'):
            with self.subTest(state):
                self.world.__init__()
                self.world.views = [live(mergeStateStatus=state)]
                self.assertBlocked(self.run_it(None, '--merge'), f'merge state {state}')

    def test_a_blocked_pr_with_a_required_check_missing_is_not_merged_by_admin(self):
        self.world.views = [live(mergeStateStatus='BLOCKED')]
        self.world.rules = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'build'}]}}]
        self.assertBlocked(self.run_it(None, '--merge'), "required check 'build' on the head: not registered")

    def test_required_checks_from_protection_and_rules(self):
        self.world.protection = {'required_status_checks': {'contexts': ['lint'], 'checks': [{'context': 'test'}]}}
        self.world.rules = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'build'}]}}]
        self.world.runs = [{'id': 1, 'name': 'build', 'status': 'completed', 'conclusion': 'failure', 'started_at': '2026-10-01T00:00:00Z'},
                           {'id': 2, 'name': 'build', 'status': 'completed', 'conclusion': 'success', 'started_at': '2026-10-01T01:00:00Z'},
                           {'id': 3, 'name': 'test', 'status': 'completed', 'conclusion': 'neutral', 'started_at': '2026-10-01T00:00:00Z'}]
        self.world.statuses = [{'context': 'lint', 'state': 'success', 'updated_at': '2026-10-01T00:00:00Z'}]
        got = self.run_it()
        self.assertEqual(got['outcome'], 'ready', 'a rerun that passed, a neutral run and a success status pass')
        self.assertEqual(got['requirements']['requiredChecks'], [{'context': 'build', 'app': None, 'state': 'completed/success'},
                                                                 {'context': 'lint', 'app': None, 'state': 'success'},
                                                                 {'context': 'test', 'app': None, 'state': 'completed/neutral'}])
        self.world.runs[2]['status'] = 'in_progress'
        self.assertBlocked(self.run_it(), "required check 'test' on the head: in_progress")
        self.world.runs[2]['status'] = 'completed'
        self.world.runs.append({**self.world.runs[2], 'id': 4, 'conclusion': 'failure'})
        self.assertBlocked(self.run_it(), "required check 'test' on the head: its runs cannot be ordered")
        self.world.runs = [{'id': 5, 'name': 'lint', 'status': 'completed', 'conclusion': 'failure', 'started_at': '2026-10-01T00:00:00Z'}]
        self.assertBlocked(self.run_it(), "required check 'lint' on the head: completed/failure")  # a run outranks a success status

    def test_a_check_required_from_an_app_passes_only_on_that_app_s_run(self):
        self.world.rules = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'build', 'integration_id': 42}]}}]
        self.world.runs = [{'id': 2, 'name': 'build', 'status': 'completed', 'conclusion': 'success', 'app': {'id': 99}, 'started_at': '2026-10-01T00:00:00Z'}]
        self.world.statuses = [{'context': 'build', 'state': 'success', 'updated_at': '2026-10-01T00:00:00Z'}]
        self.world.views = [live(mergeStateStatus='BLOCKED')]
        self.assertBlocked(self.run_it(None, '--merge'), "required check 'build' on the head: not registered by app 42")
        self.world.runs[0]['app']['id'] = 42
        self.assertEqual(self.run_it()['outcome'], 'ready')
        self.world.protection = {'required_status_checks': {'contexts': ['build'], 'checks': [{'context': 'build', 'app_id': -1}]}}
        self.world.runs[0]['app']['id'] = 99
        self.assertBlocked(self.run_it(), 'not registered by app 42', )

    def test_squash_must_be_allowed_and_no_queue(self):
        cases = {
            'repository': (lambda w: setattr(w, 'views', [live(squashMergeAllowed=False)]), 'does not allow squash'),
            'rules': (lambda w: setattr(w, 'rules', [{'type': 'pull_request', 'parameters': {'allowed_merge_methods': ['merge']}}]), 'rules do not allow a squash'),
            'queue on': (lambda w: setattr(w, 'views', [live(isMergeQueueEnabled=True)]), 'merge queue True'),
            'queue unread': (lambda w: setattr(w, 'views', [live(isMergeQueueEnabled=None)]), 'merge queue None'),
            'queue rule': (lambda w: setattr(w, 'rules', [{'type': 'merge_queue', 'parameters': {}}]), 'require a merge queue'),
        }
        for name, (change, pattern) in cases.items():
            with self.subTest(name):
                self.world.__init__()
                change(self.world)
                self.assertBlocked(self.run_it(None, '--merge'), pattern)

    def test_the_pr_must_be_the_bound_one(self):
        cases = {
            'another number in the grant': (lambda: self.run_it(), 'https://github.com/o/r/pull/8', 'pinned to'),
            'a PR sharing the head repo, branch and SHA': (lambda: self.run_it(launch_output({'pin': {'prRepo': 'o/r', 'prBranch': 'fix', 'prUrl': 'https://github.com/o/r/pull/9', 'remote': 'origin', 'pushUrls': ['u']}})), URL, 'pinned to'),
        }
        for name, (call, url, pattern) in cases.items():
            with self.subTest(name), mock.patch.object(sys.modules[__name__], 'BASE_ARGS', ['--pr-url', url, *BASE_ARGS[2:]]):
                got = call()
                self.assertBlocked(got, pattern)
        self.world.views = [live(baseRefName='dev')]
        self.assertBlocked(self.run_it(), "its base is 'dev', not 'main'")
        self.world.views = [live(headRepository={'nameWithOwner': 'fork/r'})]
        self.assertBlocked(self.run_it(), "its head is fork/r:fix, the state's o/r:fix")
        self.world.views = [live(url='https://github.com/o/r/pull/70')]
        self.assertBlocked(self.run_it(), 'not the one the grant names')
        with self.assertRaisesRegex(merge.Unusable, '--pr-url must be'):
            merge.collect(['--pr-url', 'o/r#7', *BASE_ARGS[2:], 'x', '--output', 'y'])

    def test_one_head_everywhere(self):
        self.world.views = [live(headRefOid=OTHER)]
        got = self.run_it()
        self.assertBlocked(got, 'rejoins through adoptHead')
        self.assertEqual((self.world.harvested, self.world.ran), ([], []), 'nothing more is read for a head that moved')
        self.assertIn('not inspected', got['detail'])
        self.world.views = [live()]
        self.tree_head = OTHER
        self.assertBlocked(self.run_it(), 'heads disagree')
        self.tree_head = HEAD
        self.assertBlocked(self.run_it(launch_output(result={'observation': {**launch_output()['result']['observation'], 'reviewedHead': OTHER}})), 'heads disagree')
        self.world.views = [live(), live(headRefOid=OTHER)]
        self.assertBlocked(self.run_it(), 'changed during the checks')

    def test_a_pr_retargeted_during_the_checks_is_not_merged(self):
        for name, after, pattern in (('base', live(baseRefName='release', mergeStateStatus='BLOCKED'), "its base is 'release'"),
                                     ('draft', live(isDraft=True), 'a draft'), ('queue', live(isMergeQueueEnabled=True), 'merge queue True'),
                                     ('head branch', live(headRefName='other'), 'its head is o/r:other'),
                                     ('newly blocked', live(mergeStateStatus='BLOCKED'), 'merge state BLOCKED')):
            with self.subTest(name):
                self.world.__init__()
                self.world.views = [live(), after]
                self.assertBlocked(self.run_it(None, '--merge'), pattern)

    def test_a_pr_merged_or_closed_already(self):
        self.world.views = [live(state='MERGED', mergeCommit={'oid': OTHER})]
        got = self.run_it(None, '--merge')
        self.assertEqual((got['outcome'], got['mergeCommit']), ('observed-merged', OTHER), 'not claimed as this run\'s')
        self.world.views = [live(state='CLOSED')]
        self.assertEqual(self.run_it(None, '--merge')['outcome'], 'closed')
        self.world.views = [live(isDraft=True)]
        self.assertBlocked(self.run_it(None, '--merge'), 'a draft')

    def test_the_launch_must_be_a_complete_both_pass(self):
        cases = {
            'blocked': ({'status': 'blocked', 'pass': False, 'reason': 'greptile-unreviewed'}, 'stopped greptile-unreviewed'),
            'paused': ({'status': 'paused', 'pass': False}, 'status paused'),
            'a ci lane': ({'observation': {**launch_output()['result']['observation'], 'lane': 'ci'}}, 'lane ci'),
            'a handoff': ({'handoffs': [{'commentId': 5, 'why': 'edited after we answered it'}]}, "comment 5 is the caller's"),
            'an unsettled settlement': ({'settlements': [{'commentId': 6, 'outcome': 'failed', 'why': 'gone'}]}, 'comment 6: settlement failed'),
        }
        for name, (result, pattern) in cases.items():
            with self.subTest(name):
                self.assertBlocked(self.run_it(launch_output(result=result), '--merge'), pattern)

    def test_exceptions_and_obligations_in_the_state_block(self):
        cases = {'acceptedFailures': ['k'], 'deferrals': [['1#1', {}]], 'holds': [['1#1', {}]], 'debt': [[1, {}]], 'reanswer': [1],
                 'pending': {'sha': OTHER, 'stage': 'push-failed'}}
        for key, value in cases.items():
            with self.subTest(key):
                self.assertBlocked(self.run_it(launch_output({key: value}), '--merge'), 'launch: ')

    def test_a_state_missing_a_key_it_always_carries_is_not_read_as_clean(self):
        out = launch_output()
        del out['result']['state']['holds']
        out['result']['state']['digest'] = out['result']['stateDigest'] = merge.state_digest(out['result']['state'])
        self.assertBlocked(self.run_it(out), 'its state has no holds')

    def test_the_state_must_be_the_one_named_and_intact(self):
        self.assertBlocked(self.run_it(None, digest='deadbeef'), 'not --state-digest deadbeef')
        out = launch_output()
        out['result']['state']['expectedHead'] = HEAD  # unchanged, then a field the digest covers is edited
        out['result']['state']['cyclesUsed'] = 1
        self.assertBlocked(self.run_it(out), 'does not match its own digest')

    def test_an_output_without_a_fingerprint_cannot_authorize(self):
        got = self.run_it(launch_output(reviews={'fingerprint': None}), '--merge')
        self.assertBlocked(got, 'no review fingerprint')
        self.assertEqual(self.world.harvested, [], 'nothing to compare a fresh harvest with')
        self.assertBlocked(self.run_it(launch_output(reviews={'fingerprint': f'coderabbit,greptile|coderabbit|{"0" * 32}'})), 'did not settle greptile')

    def test_unreadable_facts_exit_2(self):
        path = self.dir / 'bad.output'
        path.write_text('{not json')
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(merge.report(merge.collect, [*BASE_ARGS, 'x', '--output', str(path)]), 2)
        self.assertIn('is not JSON', out.getvalue())
        path.write_text('')
        with self.assertRaisesRegex(merge.Unusable, 'launch threw'):
            merge.collect([*BASE_ARGS, 'x', '--output', str(path)])
        self.world.views = [{'data': {'repository': None}}]
        with self.assertRaisesRegex(merge.Unusable, 'shape merge.py does not read'):
            self.run_it()

    def test_both_bots_must_have_reviewed_the_head(self):
        for name in ('coderabbit', 'greptile'):
            for record in (bot(name, 'absent', None), bot(name, 'settled', HEAD, 'skipped'), bot(name, 'settled', None, 'limited'),
                           bot(name, 'unknown', None), bot(name, 'working'), bot(name, 'reviewed', OTHER)):
                with self.subTest(name=name, record=(record['state'], record['kind'], record['sha'])):
                    self.world.__init__()
                    self.world.fresh['bots'] = [record if b['bot'] == name else b for b in self.world.fresh['bots']]
                    self.assertBlocked(self.run_it(None, '--merge'), f'^{name}-unreviewed: ')
        self.world.__init__()
        self.world.fresh['bots'][1] = bot('copilot', 'settled', None, 'limited')
        self.assertEqual(self.run_it()['outcome'], 'ready', 'Copilot is not a required reviewer')

    def test_review_evidence_changed_since_the_launch(self):
        self.world.fresh['fingerprint'] = FP[:-1] + '1'
        self.assertBlocked(self.run_it(None, '--merge'), '^fingerprint: .*relaunch a `both` launch')

    def test_ci_must_be_green_with_nothing_ignored(self):
        cases = {
            'Greptile below its threshold': ({'status': 'red', 'checks': [{'name': 'Greptile Review'}]}, 'failed: Greptile Review'),
            'pending': ({'status': 'running', 'pending': 1}, '1 pending'),
            'nothing registered': ({'registered': 0}, '0 registered'),
            'conflicting': ({'status': 'conflicting', 'mergeable': 'CONFLICTING'}, 'status conflicting'),
            'an error': ({'error': 'gh: HTTP 502'}, '^ci: gh: HTTP 502'),
        }
        for name, (over, pattern) in cases.items():
            with self.subTest(name):
                self.world.__init__()
                self.world.inventory.update(over)
                self.assertBlocked(self.run_it(None, '--merge'), pattern)

    def test_an_ambiguous_merge_is_read_back(self):
        cases = {
            'refused, still open': ((1, '', 'GraphQL: Head branch was modified'), [live()], 'not-ready'),
            'error but merged': ((1, '', 'timeout'), [live(state='MERGED', mergeCommit={'oid': OTHER})], 'observed-merged'),
            'accepted, unreadable': ((0, '', ''), [merge.Unusable('gh down')], 'uncertain'),
            'accepted, still open': ((0, '', ''), [live()], 'uncertain'),
            'someone else merged first': ((0, '! Pull request o/r#7 was already merged', ''), [live(state='MERGED', mergeCommit={'oid': OTHER})], 'observed-merged'),
        }
        for name, (answer, after, outcome) in cases.items():
            with self.subTest(name):
                self.world.__init__()
                self.world.views = [live(), live(), *after]
                self.world.merged = answer
                got = self.run_it(None, '--merge')
                self.assertEqual(got['outcome'], outcome, got)
                self.assertEqual(len(self.world.merges), 1, 'one request, never retried blind')
                if outcome == 'not-ready':
                    self.assertIn('Head branch was modified', got['blockers'][0])

    def test_the_state_digest_matches_the_workflow_s(self):
        """The workflow's sealOf keeps nulls and sorts keys as JavaScript does; tests/pr_babysit_harness.mjs checks a live state."""
        st = {'b': None, 'a': [1, {'ü': 'x', 'z': None}], 'digest': 'ignored'}
        self.assertEqual(merge.state_digest(st), merge.fnv1a('{"a":[1,{"z":null,"ü":"x"}],"b":null}'))


if __name__ == '__main__':
    unittest.main()
