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
        self.rules = []
        self.fresh = {'headSha': HEAD, 'fingerprint': FP, 'bots': [bot('coderabbit'), bot('copilot', 'absent', None), bot('greptile')]}
        self.inventory = {'head': HEAD, 'baseRef': 'main', 'status': 'green', 'mergeable': 'MERGEABLE', 'pending': 0, 'registered': 12,
                          'ignored': [], 'waiting': '', 'checks': [], 'requiredChecks': [], 'observedAt': 'now'}
        self.merged = (0, '', '')
        self.ran = []
        self.harvested = []

    def gh_json(self, *argv):
        if argv[:2] == ('api', 'graphql'):
            got = self.views[0] if len(self.views) == 1 else self.views.pop(0)
            if isinstance(got, Exception):
                raise got
            return got
        if argv[-1].startswith('repos/o/r/rules/branches/main'):
            return [self.rules]
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

    def required(self, *checks):
        self.world.inventory['requiredChecks'] = [{'context': c, 'app': app, 'state': state, 'bucket': bucket} for c, app, state, bucket in checks]
        if any(bucket != 'pass' for *_, bucket in checks):
            self.world.inventory['status'] = 'running'

    def test_a_blocked_pr_with_a_required_check_missing_is_not_merged_by_admin(self):
        self.world.views = [live(mergeStateStatus='BLOCKED')]
        self.required(('build', -1, 'not registered', 'missing'))
        self.assertBlocked(self.run_it(None, '--merge'), "required check 'build' on the head: not registered")

    def test_the_inventory_s_required_checks_are_the_receipt_s_evidence(self):
        self.required(('build', -1, 'completed/success', 'pass'), ('lint', 42, 'success', 'pass'))
        got = self.run_it()
        self.assertEqual(got['outcome'], 'ready')
        self.assertEqual(got['requirements']['requiredChecks'], [{'context': 'build', 'app': None, 'state': 'completed/success'},
                                                                 {'context': 'lint', 'app': 42, 'state': 'success'}])
        self.required(('build', 42, 'not registered by app 42 (only by app 99)', 'missing'))
        self.assertBlocked(self.run_it(), r"required check 'build' on the head: not registered by app 42 \(only by app 99\)")

    def test_an_inventory_without_readable_required_checks_or_for_another_base_blocks(self):
        GOOD = {'context': 'build', 'app': -1, 'state': 'success', 'bucket': 'pass'}
        for name, change, pattern in (('none', lambda inv: inv.pop('requiredChecks'), 'no readable requiredChecks'),
                                      ('malformed', lambda inv: inv.__setitem__('requiredChecks', [{'context': 'build'}]), 'no readable requiredChecks'),
                                      *((f'bad {k}', lambda inv, k=k, v=v: inv.__setitem__('requiredChecks', [{**GOOD, k: v}]), 'no readable requiredChecks')
                                        for k, v in (('context', None), ('state', None), ('app', '42'), ('app', True), ('bucket', 'ok'))),
                                      ('another base', lambda inv: inv.__setitem__('baseRef', 'next'), "for base 'next', not 'main'")):
            with self.subTest(name):
                self.world.__init__()
                change(self.world.inventory)
                self.assertBlocked(self.run_it(None, '--merge'), pattern)

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
            'a reviews lane': ({'observation': {**launch_output()['result']['observation'], 'lane': 'reviews'}}, 'lane reviews'),
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

    def test_only_the_owners_waiver_drops_a_bot_and_never_every_bot(self):
        # #292: Greptile left out of the launch at the owner's word; CodeRabbit reviewed the head.
        fp = f'coderabbit,copilot|coderabbit|{"0" * 32}'
        output = lambda: launch_output(reviews={'fingerprint': fp, 'bots': [bot('coderabbit')]})
        self.world.fresh = {'headSha': HEAD, 'fingerprint': fp, 'bots': [bot('coderabbit'), bot('copilot', 'absent', None)]}
        self.assertBlocked(self.run_it(output()), 'did not settle greptile')
        got = self.run_it(output(), '--waive', 'greptile')
        self.assertEqual((got['outcome'], got['blockers'], got['waived']), ('ready', [], ['greptile']), got)
        self.assertEqual(self.world.harvested[-1][2:6], ['--reviewers', 'coderabbit,copilot', '--auto-run', 'coderabbit'])
        self.world.fresh['bots'][0] = bot('coderabbit', 'settled', None, 'limited')
        self.assertBlocked(self.run_it(output(), '--waive', 'greptile', '--merge'), '^coderabbit-unreviewed: ')
        harvested = len(self.world.harvested)
        got = self.run_it(output(), '--waive', 'greptile', '--waive', 'coderabbit', '--merge')
        self.assertBlocked(got, "every review bot is waived .*: a merge needs one bot's review of the head")
        self.assertEqual((got['waived'], len(self.world.harvested)), (['coderabbit', 'greptile'], harvested))
        self.assertEqual(self.run_it()['waived'], [], 'no waiver unless named')
        with self.assertRaises(merge.Unusable):
            self.run_it(output(), '--waive', 'copilot')

    def test_a_waiver_lifts_the_launch_s_own_stop_for_that_bot_only(self):
        # #432: the last launch stopped coderabbit-unreviewed; the owner said to merge without CodeRabbit.
        def stopped(bot_name='coderabbit', bots=None, state=None, **over):
            result = {'status': 'blocked', 'pass': False, 'reason': f'{bot_name}-unreviewed', 'head': HEAD,
                      'bots': bots or [bot(bot_name, 'settled', None, 'limited')],
                      'coderabbitAsk': {'relaunchAsks': None, 'asked': [{'ask': 'tick', 'at': '2026-11-01T00:00:00Z'}]}, **over}
            return launch_output(state, result)
        self.world.fresh['bots'][0] = bot('coderabbit', 'settled', None, 'limited')
        got = self.run_it(stopped(), '--waive', 'coderabbit')
        self.assertEqual((got['outcome'], got['blockers'], got['waived']), ('ready', [], ['coderabbit']), got)
        self.world.views = [live(), live(), live(state='MERGED', mergeCommit={'oid': OTHER})]
        self.assertEqual(self.run_it(stopped(), '--waive', 'coderabbit', '--merge')['outcome'], 'merged')
        self.world.__init__()
        self.world.fresh['bots'][2] = bot('greptile', 'settled', None, 'limited')
        self.assertEqual(self.run_it(stopped('greptile'), '--waive', 'greptile')['outcome'], 'ready')
        def judged(fid, comment, verdict, digest=None, how='refutation', was=None):
            f = {'findingId': fid, 'commentId': comment, 'commentDigest': f'd{comment}', 'verdict': verdict}
            decision = [fid, {'commentId': comment, 'digest': digest or f['commentDigest'], 'verdict': was or verdict}]
            return f, decision, [comment, {'how': how, 'digest': f['commentDigest']}]

        def harvested(*rows, decisions=None, answers=None):
            obs = launch_output()['result']['observation']
            return {'observation': {**obs, 'reviews': {**obs['reviews'], 'findings': [r[0] for r in rows]}},
                    'state': {'decisions': [r[1] for r in rows] if decisions is None else decisions,
                              'answeredWith': [r[2] for r in rows] if answers is None else answers}}
        fine = harvested(judged('2#1', 2, 'invalid'), judged('3#1', 3, 'stale'), judged('4#1', 4, 'stale', how='fixNote', was='valid'))
        got = self.run_it(stopped('greptile', **fine), '--waive', 'greptile')
        self.assertEqual((got['outcome'], got['blockers']), ('ready', []), 'every dismissal decided and answered as it stands')
        observation = launch_output()['result']['observation']
        cases = {
            'no waiver': ((), stopped(), 'stopped coderabbit-unreviewed'),
            'the other bot waived': (('greptile',), stopped(), 'stopped coderabbit-unreviewed'),
            'a second bot it names unwaived': (('coderabbit',), stopped(bots=[bot('coderabbit', 'absent', None), bot('greptile', 'absent', None)]),
                                               'stopped coderabbit-unreviewed'),
            'another stop': (('coderabbit',), stopped(reason='ci-red-rig-side'), 'stopped ci-red-rig-side'),
            'paused, not blocked': (('coderabbit',), stopped(status='paused'), 'status paused'),
            'a reviews lane': (('coderabbit',), stopped(observation={**observation, 'lane': 'reviews'}), 'lane reviews'),
            'an extra blocker': (('coderabbit',), stopped(handoffs=[{'commentId': 5, 'why': 'edited after we answered it'}]), "comment 5 is the caller's"),
            'a spent budget': (('coderabbit',), stopped(state={'cyclesUsed': 8}), 'cycle budget spent'),
            'a finding still valid': (('coderabbit',), stopped(**harvested(judged('1#1', 1, 'valid', how='fixNote'))), r'finding\(s\) 1#1 of its last harvest'),
            'a finding with no decision': (('coderabbit',), stopped(**harvested(judged('1#1', 1, 'invalid'), decisions=[])), r'finding\(s\) 1#1 of'),
            'a decision on another body': (('coderabbit',), stopped(**harvested(judged('1#1', 1, 'invalid', digest='old'))), r'finding\(s\) 1#1 of'),
            'a decision with another verdict': (('coderabbit',), stopped(**harvested(judged('1#1', 1, 'invalid', was='stale'))), r'finding\(s\) 1#1 of'),
            'valid to stale without a fix note': (('coderabbit',), stopped(**harvested(judged('1#1', 1, 'stale', was='valid'))), r'finding\(s\) 1#1 of'),
            'an answer on another body': (('coderabbit',), stopped(**harvested(judged('1#1', 1, 'invalid'), answers=[[1, {'how': 'refutation', 'digest': 'old'}]])),
                                          r'finding\(s\) 1#1 of'),
            'no answer': (('coderabbit',), stopped(**harvested(judged('1#1', 1, 'invalid'), judged('2#1', 2, 'invalid'), answers=[])),
                          r'finding\(s\) 1#1, 2#1 of its last harvest not settled'),
        }
        for name, (waive, output, pattern) in cases.items():
            with self.subTest(name):
                self.world.__init__()
                self.world.fresh['bots'][0] = bot('coderabbit', 'settled', None, 'limited')
                self.assertBlocked(self.run_it(output, *(a for w in waive for a in ('--waive', w)), '--merge'), pattern)
        self.world.__init__()
        self.assertBlocked(self.run_it(stopped(state={'debt': [[1, {}]]}), '--waive', 'coderabbit'), 'still owed a reply')
        self.world.fresh['bots'][0] = bot('coderabbit', 'settled', None, 'limited')
        self.world.inventory.update(status='red', checks=[{'name': 'build'}])
        self.assertBlocked(self.run_it(stopped(), '--waive', 'coderabbit'), '^ci: status red')

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
