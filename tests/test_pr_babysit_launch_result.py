import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('launch_result', ROOT / 'skills/pr-babysit/scripts/launch_result.py')
launch_result = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch_result)

HEAD = 'a' * 40
NEXT = 'b' * 40


def failure(cell, verdict='rig-side', state='rigSide', complete=True):
    return {'check': 'hil-tinyusb (tinyusb.json)', 'workflow': 'Build', 'job': 'hil-tinyusb', 'cell': cell,
            'signature': f'{cell} ... Failed', 'key': cell.encode().hex().ljust(16, '0')[:16], 'complete': complete, 'verdict': verdict,
            'state': state, 'firstError': f'{cell} failed ' + 'x' * 400}


def output(**over):
    actions = {'reviewPush': {'pass': True, 'committed': True, 'sha': NEXT, 'detail': f'{HEAD[:9]}..{NEXT[:9]}'},
               'fixNotePosts': {'pass': True, 'detail': '', 'receipts': [{'commentId': 7, 'kind': 'review-body', 'replyId': 8, 'sent': True,
                                              'posted': True, 'verified': True, 'resolved': None, 'error': None}]}}
    result = {
        'stateDigest': '0123abcd', 'pass': False, 'status': 'paused', 'reason': 'yielded', 'cycles': 2, 'head': NEXT,
        'rollup': {'cycles': [2], 'findings': {'total': 1, 'fixed': 1}, 'ci': {'total': 2}, 'reran': 0, 'pushed': [NEXT[:8]], 'replies': 1},
        'pending': [{'bot': 'greptile', 'state': 'absent', 'reason': 'no check run'}],
        'observation': {'reviewedHead': HEAD, 'lane': 'both',
                        'reviews': {'fingerprint': f'coderabbit|coderabbit|{"f" * 32}', 'bots': [{'bot': 'coderabbit', 'state': 'reviewed', 'sha': HEAD}],
                                    'findings': [{'findingId': '7#1', 'commentId': 7, 'commentDigest': 'c0ffee00', 'source': 'coderabbit', 'verdict': 'valid',
                                                  'file': 'a.c', 'line': 3}]},
                        'ci': {'status': 'red', 'headSha': HEAD, 'infraRerun': [],
                               'realFailures': [failure('f072 cdc'), failure('pico host', verdict='real', state='real')]},
                        'actions': actions},
        'state': {'expectedHead': NEXT},
    }
    result.update(over.pop('result', {}))
    return {'agentCount': 3, 'totalTokens': 1000,
            'logs': ['preflight: ok', 'cycle 2: rig-side CI failure (not fixing): f072', 'cycle 2 summary ' + 'y' * 500],
            'workflowProgress': [
                {'type': 'workflow_agent', 'label': 'ci:collect#2.1', 'model': 'claude-haiku-4-5', 'state': 'done', 'startedAt': 2, 'tokens': 100, 'durationMs': 4000},
                {'type': 'workflow_agent', 'label': 'reviews#2', 'model': 'claude-opus-5-5', 'state': 'done', 'startedAt': 1, 'tokens': 500, 'durationMs': 9000},
                {'type': 'workflow_agent', 'label': 'ci:collect#2.f', 'model': 'claude-haiku-4-5', 'state': 'failed', 'startedAt': 3, 'tokens': 50, 'durationMs': 1000}],
            'result': result, **over}


def setUpModule():
    guard_survivors(__name__)


class LaunchResultTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.states = self.dir / 'states'
        patch = mock.patch.object(launch_result, 'STATES', self.states)
        patch.start()
        self.addCleanup(patch.stop)

    def run_it(self, data, *argv):
        path = self.dir / 'w.output'
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')
        out = io.StringIO()
        with redirect_stdout(out):
            rc = launch_result.report(launch_result.collect, ['--output', str(path), *argv])
        return rc, json.loads(out.getvalue())

    def repo(self):
        repo = self.dir / 'repo'
        subprocess.run(['git', 'init', '-q', '-b', 'pr', str(repo)], check=True, env=git_env())
        subprocess.run(['git', '-C', str(repo), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True, env=git_env())
        (repo / 'left.txt').write_text('x')
        return str(repo)

    def test_the_state_is_kept_outside_the_task_directory_and_loads_after_the_output_is_gone(self):
        """#340: a host reset wipes the Workflow task output; the stateRef names a copy that outlives it."""
        config = {'pr': 7, 'repo': 'o/r', 'reviewers': ['coderabbit']}
        saved = output(result={'state': {'expectedHead': NEXT, 'config': config, 'digest': '0123abcd'}})
        rc, s = self.run_it(saved)
        kept = Path(s['stateRef']['outputFile'])
        self.assertEqual((rc, kept.parent), (0, self.states))
        self.assertTrue(kept.name.startswith('0123abcd-'))
        self.assertEqual(kept.read_bytes(), (self.dir / 'w.output').read_bytes())
        self.assertEqual(self.run_it(saved)[1]['stateRef']['outputFile'], kept.as_posix(), 'the same bytes reuse the copy')
        rc, after = self.run_it('', '--state-ref', f'{kept.as_posix()}:0123abcd')  # the task output wiped, a launch that threw
        self.assertEqual((rc, after['stateRef']['outputFile'], after['pinned']), (0, kept.as_posix(), config))
        done = subprocess.run([sys.executable, str(ROOT / 'skills/pr-babysit/scripts/state_transfer.py'), str(kept)],
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, json.loads(done.stdout.splitlines()[-1])['digest']), (0, '0123abcd'), done.stdout)
        self.assertEqual(len(list(self.states.iterdir())), 1, 'no temporary file left behind')

    def test_a_state_digest_that_is_not_8_hex_digits_is_never_a_file_name(self):
        rc, s = self.run_it(output(result={'stateDigest': '../escaped'}))
        self.assertEqual((rc, s['stateRef']['outputFile']), (0, (self.dir / 'w.output').resolve().as_posix()))
        self.assertFalse(self.states.exists())
        self.assertFalse((self.dir / 'escaped').exists())
        self.assertTrue(any("'../escaped' is not 8 hex digits" in n for n in s['notes']), s['notes'])

    def test_a_state_that_cannot_be_kept_names_the_output_and_says_so(self):
        self.states.parent.mkdir(exist_ok=True)
        self.states.write_text('a file where the directory goes')
        rc, s = self.run_it(output())
        self.assertEqual((rc, s['stateRef']['outputFile']), (0, (self.dir / 'w.output').resolve().as_posix()))
        self.assertTrue(any(n.startswith('the state is not kept outside the Workflow task directory') for n in s['notes']), s['notes'])

    def test_a_launch_is_condensed(self):
        rc, s = self.run_it(output())
        self.assertEqual(rc, 0)
        self.assertEqual(s['result']['status'], 'paused')
        self.assertEqual(s['result']['rollup']['pushed'], [NEXT[:8]])
        [kept] = self.states.iterdir()
        self.assertEqual(s['stateRef'], {'outputFile': kept.as_posix(), 'digest': '0123abcd'},
                         'with slashes: the workflow puts it in a shell command')
        self.assertEqual(s['blockers'], [], 'a push past the reviewed head is not a disagreement')
        self.assertEqual((s['observation']['fingerprint'], s['observation']['bots'][0]['sha']), (f'coderabbit|coderabbit|{"f" * 32}', HEAD),
                         'the review evidence a merge compares, and the full SHA a bot reviewed')
        self.assertEqual(s['observation']['findings'], [{'id': '7#1', 'digest': 'c0ffee00', 'source': 'coderabbit', 'verdict': 'valid', 'at': 'a.c:3'}])
        ci = s['observation']['ci']
        self.assertEqual(ci['verdicts'], {'rig-side': 1, 'real': 1})
        self.assertEqual(ci['settledCells'], {'hil-tinyusb (tinyusb.json)': ['f072 cdc']}, 'a settled failure by its cell, to investigate')
        pico = failure('pico host', verdict='real', state='real')
        self.assertEqual(ci['attention'], [{'check': pico['check'], 'cell': 'pico host', 'state': 'real', 'verdict': 'real', 'key': pico['key'],
                                           'complete': True, 'files': None, 'firstError': launch_result.cut(pico['firstError'])}],
                         'an unsettled failure by the fields a caller acts on, with the key that accepts it')
        self.assertTrue(ci['attention'][0]['firstError'].endswith('…'))
        rc, s = self.run_it(output(), '--keys')
        self.assertEqual(s['observation']['ci']['settledCells'], {'hil-tinyusb (tinyusb.json)': [{'cell': 'f072 cdc', 'key': failure('f072 cdc')['key']}]},
                         'with --keys, the key a caller passes back to accept it')
        self.assertEqual(s['receipts']['replies'], [{'batch': 'fixNotePosts', 'findingVerdicts': ['valid'], 'commentId': 7, 'kind': 'review-body', 'replyId': 8, 'sent': True,
                                                     'posted': True, 'verified': True, 'resolved': None, 'error': None}], 'receipts verbatim')
        self.assertEqual(s['receipts']['pushes'][0]['committed'], True)
        self.assertEqual(s['result']['pending'][0]['bot'], 'greptile', 'result fields other than history/observation/state stay verbatim')
        self.assertNotIn('state', s['result'])
        self.assertEqual(s['launch'], {'agents': 3, 'tokens': 1000, 'secs': 9}, 'first start to last end')
        self.assertEqual(s['notes'], [])
        self.assertEqual(s['logs'][-1], '(1 "not fixing" CI failure lines, one per failure listed under ci)')
        self.assertTrue(s['logs'][1].endswith('whole line in the output\'s logs)'))

    def test_the_findings_table_quotes_every_row_with_its_outcome(self):
        sha = 'c' * 40
        rows = [
            {'lane': 'review', 'source': 'greptile', 'at': 'a.c:3', 'claim': 'pipe | back\\slash\nnewline ' + 'x' * 200, 'verdict': 'valid', 'state': 'valid',
             'repair': {'outcome': 'pushed', 'sha': sha}, 'outcome': 'fixed + pushed, since already fixed', 'reply': {'how': 'fixNotePosts', 'replyId': 9, 'settled': True}},
            {'lane': 'review', 'source': 'coderabbit', 'at': 'b.c:1', 'claim': 'old body', 'verdict': 'invalid', 'state': 'refuted', 'repair': None,
             'outcome': 'comment edited since; refuted', 'reply': {'how': 'refutedPosts', 'replyId': None, 'settled': False, 'error': 'offered, no receipt shows it posted'}},
            {'lane': 'ci', 'source': 'ci:build', 'cell': 'f072 cdc', 'firstError': 'boom', 'verdict': 'real', 'state': 'greptileGate', 'repair': None,
             'outcome': 'left red: a Greptile confidence gate, not a CI-lane fix'},
        ]
        table = launch_result.findings_table({'rows': rows, 'observed': {'reviews': True, 'ci': True}}).split('\n')
        self.assertEqual(table[0], 'reviews: 2 row(s); CI: 1 row(s)')
        self.assertEqual(len(table), 3 + 1 + len(rows))
        first = ' '.join(table[4].split())
        self.assertIn('pipe \\| back\\\\slash newline', first, 'escaped, on one line')
        self.assertIn('…', first, 'a long claim is cut')
        self.assertIn(f'fixed + pushed, since already fixed | {sha} | fix note 9 settled', first, 'the workflow\'s outcome text and the full SHA')
        self.assertIn('refutation not settled: offered, no receipt shows it posted', table[5])
        self.assertIn('| f072 cdc |', table[6])
        for row in table[2:]:
            self.assertEqual(row.count(' | '), 6, row)
        self.assertEqual(launch_result.md_cell(None), '-')
        self.assertEqual(launch_result.md_cell('y' * 200, 10), 'y' * 10 + '…')
        self.assertEqual(launch_result.findings_table({'rows': [], 'observed': {'reviews': True, 'ci': False}}), 'reviews: no findings reported; CI not observed')
        self.assertEqual(launch_result.findings_table({'cycles': [1]}), 'findings unavailable (older output)')
        rc, s = self.run_it(output())
        self.assertEqual(s['findingsTable'], 'findings unavailable (older output)', 'the fixture predates rows')

    def test_a_failure_is_settled_by_the_state_the_workflow_gave_it_not_its_verdict(self):
        gate = {**failure('gate', verdict='rig-side'), 'check': 'SonarCloud Code Analysis', 'workflow': '', 'cell': None, 'state': 'sonarGate'}
        o = output()
        o['result']['observation']['ci']['realFailures'] = [gate, failure('f072 cdc')]
        rc, s = self.run_it(o)
        ci = s['observation']['ci']
        self.assertEqual(ci['verdicts'], {'sonarGate': 1, 'rig-side': 1})
        self.assertEqual(list(ci['settledCells']), ['hil-tinyusb (tinyusb.json)'])
        self.assertEqual([f['check'] for f in ci['attention']], ['SonarCloud Code Analysis'], 'the gate needs attention')
        greptile = {**gate, 'check': 'Greptile Review', 'verdict': 'real', 'state': 'greptileGate'}
        o['result']['observation']['ci']['realFailures'] = [gate, greptile, failure('f072 cdc')]
        rc, s = self.run_it(o)
        ci = s['observation']['ci']
        self.assertEqual(ci['verdicts'], {'sonarGate': 1, 'greptileGate': 1, 'rig-side': 1}, 'each gate counted apart, whatever its verdict')
        self.assertEqual([f['check'] for f in ci['attention']], ['SonarCloud Code Analysis', 'Greptile Review'])

    def test_a_launch_that_threw_keeps_the_state_it_was_given(self):
        rc, s = self.run_it('', '--state-ref', '/t/w1.output:4841042f', '--checkout', self.repo())
        self.assertEqual(rc, 0)
        self.assertIsNone(s['result'])
        self.assertEqual(s['stateRef'], {'outputFile': '/t/w1.output', 'digest': '4841042f'})
        self.assertIn('threw before returning', s['blockers'][0])
        self.assertIn("the checkout is dirty: ['?? left.txt']", s['blockers'], 'even without a result')
        self.assertIn('no new state', s['notes'][0])

    def test_pinned_is_the_state_s_config_or_the_state_ref_s_when_the_launch_threw(self):
        config = {'pr': 7, 'reviewers': ['coderabbit'], 'autoRun': ['coderabbit'], 'checkoutDir': '.', 'ciWait': 30,
                  'protected': '^(skills\\/cowork\\/)', 'generated': None}
        rc, s = self.run_it(output(result={'state': {'expectedHead': NEXT, 'config': config}}))
        self.assertEqual(s['pinned'], config)
        rc, s = self.run_it(output(result={'state': None}))
        self.assertIsNone(s['pinned'], 'no state, nothing pinned')
        saved = self.dir / 'w1.output'
        saved.write_text(json.dumps(output(result={'stateDigest': '4841042f', 'state': {'expectedHead': NEXT, 'config': config}})), encoding='utf-8')
        rc, s = self.run_it('', '--state-ref', f'{saved}:4841042f')
        self.assertEqual((s['stateRef']['digest'], s['pinned']), ('4841042f', config))
        for ref, why in ((f'{saved}:0badf00d', 'holds no state of digest 0badf00d'), ('/t/missing.output:4841042f', 'pinned arguments are unread: cannot read')):
            rc, s = self.run_it('', '--state-ref', ref)
            self.assertIsNone(s['pinned'], ref)
            self.assertTrue(any(why in n for n in s['notes']), s['notes'])

    def test_a_spent_cycle_budget_is_a_blocker(self):
        rc, s = self.run_it(output(result={'state': {'expectedHead': NEXT, 'cyclesUsed': 5, 'maxCycles': 5}}))
        self.assertEqual(s['budget'], {'cyclesUsed': 5, 'maxCycles': 5})
        self.assertIn("cycle budget spent: 5 of 5; a relaunch needs a larger maxCycles, the user's decision", s['blockers'])
        rc, s = self.run_it(output(result={'state': {'expectedHead': NEXT, 'cyclesUsed': 4, 'maxCycles': 5}}))
        self.assertEqual((s['budget'], s['blockers']), ({'cyclesUsed': 4, 'maxCycles': 5}, []))
        rc, s = self.run_it(output(result={'pass': True, 'status': 'complete', 'state': {'expectedHead': NEXT, 'cyclesUsed': 5, 'maxCycles': 5}}))
        self.assertEqual(s['blockers'], [], 'a launch that passed on its last cycle needs no relaunch')

    def test_a_state_too_large_to_load_is_a_blocker(self):
        rc, s = self.run_it(output(result={'pass': True, 'status': 'complete', 'stateTooLarge': 70000,
                                           'state': {'expectedHead': NEXT, 'cyclesUsed': 1, 'maxCycles': 5}}))
        self.assertEqual(s['blockers'], ['the returned state is 70000 bytes, over the 65536 a launch loads: no launch can continue '
                                         'from it; keep the output file and report it, never reset or reconstruct the state'])

    def test_a_launch_stopped_at_load_is_a_blocker_and_keeps_the_state_it_was_given(self):
        stopped = {'agentCount': 1, 'totalTokens': 30000, 'logs': [], 'workflowProgress': [], 'result': {
            'pass': False, 'status': 'blocked', 'reason': 'state-transfer-failed', 'detail': 'state digest mismatch'}}
        rc, s = self.run_it(stopped, '--state-ref', '/t/w1.output:4841042f')
        self.assertEqual((s['stateRef'], s['budget']), ({'outputFile': '/t/w1.output', 'digest': '4841042f'}, None))
        self.assertTrue(any(b.startswith('the state did not load') for b in s['blockers']), s['blockers'])

    def test_a_refused_launch_is_a_blocker_that_forbids_starting_over(self):
        for reason, says in (('state-transfer-failed', 'never reset or reconstruct the state'),
                             ('state-mismatch', "starting over is the user's decision"),
                             ('adopt-head-mismatch', 'never answer it with a reset or a fabricated state'),
                             ('adopt-pending', 'never answer it with a reset or a fabricated state'),
                             ('adopt-audit-failed', 'never answer it with a reset or a fabricated state'),
                             ('adopt-base-missing', 'retry the same adoption with the same stateRef and adoptHead'),
                             ('rebase-refused', 'the re-pin was refused'),
                             ('stale-head', "only on the user's word"),
                             ('wrong-head', 'relaunch with adoptHead naming HEAD and the same stateRef, if any'),
                             ('deferral-refused', 'it needs a new decision'),
                             ('pr-conflicting', 'relaunch with adoptHead naming that merge'),
                             ('stale-workflow', 'relaunch from a fresh session'),
                             ('pr-state-unread', 'relaunch once `gh pr view` answers')):
            with self.subTest(reason):
                refused = {'agentCount': 1, 'totalTokens': 10, 'logs': [], 'workflowProgress': [],
                           'result': {'pass': False, 'status': 'blocked', 'reason': reason}}
                rc, s = self.run_it(refused, '--state-ref', '/t/w1.output:4841042f')
                self.assertEqual(len(s['blockers']), 1, s['blockers'])
                self.assertIn(says, s['blockers'][0])

    def test_an_unread_pr_state_after_a_lost_receipt_keeps_the_publication_uncertain(self):
        data = output()
        data['result'].update(status='blocked', reason='pr-state-unread')
        data['result']['observation']['actions']['adoption'] = {'from': HEAD, 'to': NEXT, 'publication': 'unknown'}
        _, s = self.run_it(data)
        self.assertIn('uncertain publication: adoption aaaaaaaa..bbbbbbbb: unknown', s['blockers'])
        self.assertTrue(any('relaunch once `gh pr view` answers' in b for b in s['blockers']), s['blockers'])
        self.assertFalse([b for b in s['blockers'] if 'nothing was written' in b], s['blockers'])

    def test_a_merged_or_closed_pr_is_terminal_with_no_relaunch_blocker(self):
        for reason, says in (('pr-merged', 'the PR is merged; nothing to continue'), ('pr-closed', 'the PR is closed; nothing to continue')):
            with self.subTest(reason):
                ended = {'agentCount': 1, 'totalTokens': 10, 'logs': [], 'workflowProgress': [],
                         'result': {'stateDigest': '0123abcd', 'pass': False, 'status': 'blocked', 'reason': reason, 'cycles': 4,
                                    'state': {'expectedHead': HEAD, 'cyclesUsed': 4, 'maxCycles': 4}}}
                rc, s = self.run_it(ended)
                self.assertEqual((rc, s['blockers']), (0, []), 'a spent budget does not block a launch with nothing to continue')
                self.assertIn(says, s['notes'])

    def test_every_stop_reason_of_the_workflow_is_refused_or_handled_elsewhere(self):
        handled_elsewhere = {  # blocked by receipts, budget, heads or CI fields, or left to chief's judgment
            'adopt-needs-push', 'adopt-push-failed', 'adopt-push-unknown', 'budget-exhausted', 'build-unverifiable',
            'cycle-threw', 'deferred-replies-unresolved', 'dirty-start', 'dry-run',
            'duplicate-finding-ids', 'fix-verification-failed', 'held-awaiting-caller', 'preflight-died', 'preflight-failed', 'push-failed',
            'review-challenger-died', 'review-report-unusable', 'review-validator-died', 'reviews-pending',
            'wrong-branch', 'wrong-remote', 'yielded'}
        source = (ROOT / 'workflows' / 'pr-babysit.js').read_text(encoding='utf-8')
        reasons = {m.group(2) for m in re.finditer(r"""pass: false\b[^{}]*?\breason: (['"])(.+?)\1""", source)}
        reasons |= {m.group(2) for m in re.finditer(r"""\bstop\([\w.]+, (['"])(.+?)\1""", source)}
        reasons |= {m.group(2) for m in re.finditer(r"""\bstop: (['"])(.+?)\1""", source)}  # the GATES table
        sets = (handled_elsewhere, set(launch_result.REFUSED), set(launch_result.ENDED), set(launch_result.STALLED))
        self.assertEqual(sum(map(len, sets)), len(set().union(*sets)), 'each reason is handled one way')
        self.assertEqual(reasons, set().union(*sets),
                         'a stop reason was added or renamed: give it a REFUSED response or name it here')

    def test_a_stop_another_launch_would_repeat_says_what_must_change(self):
        def stopped(reason, **extra):
            return {'agentCount': 1, 'totalTokens': 10, 'logs': [], 'workflowProgress': [],
                    'result': {'stateDigest': '0123abcd', 'pass': False, 'status': 'blocked', 'reason': reason, 'cycles': 2,
                               'state': {'expectedHead': HEAD, 'cyclesUsed': 2, 'maxCycles': 16}, **extra}}
        for reason, says in launch_result.STALLED.items():
            with self.subTest(reason):
                self.assertEqual(self.run_it(stopped(reason))[1]['blockers'], [says])
        held = [{'findingId': '7#1', 'commentId': 7, 'reason': 'out of scope per https://x/1 (y): defer it by findingId, or drop the topic'}]
        self.assertEqual(self.run_it(stopped('held-awaiting-caller', held=held))[1]['blockers'],
                         ['finding 7#1 on comment 7 is held for the caller, and another launch cannot lift it: '
                          'out of scope per https://x/1 (y): defer it by findingId, or drop the topic'])
        for reason in ('yielded', 'pr-merged'):
            self.assertEqual(self.run_it(stopped(reason))[1]['blockers'], [], reason)

    def test_an_unreviewed_coderabbit_says_whether_a_relaunch_asks_from_the_runs_record(self):
        # #243: chief relaunched for an ask the run had already spent on the head.
        def stopped(ask):
            return {'agentCount': 1, 'totalTokens': 10, 'logs': [], 'workflowProgress': [],
                    'result': {'stateDigest': '0123abcd', 'pass': False, 'status': 'blocked', 'reason': 'coderabbit-unreviewed', 'cycles': 2,
                               'state': {'expectedHead': HEAD, 'cyclesUsed': 2, 'maxCycles': 16}, 'coderabbitAsk': ask}}
        for ask, says in (
                ({'relaunchAsks': None, 'asked': [{'ask': 'request', 'at': '2026-10-23T01:10:00Z'}]},
                 'already used its one ask on the head (an `@coderabbitai review` comment at 2026-10-23T01:10:00Z), so a relaunch will not ask'),
                ({'relaunchAsks': None, 'asked': [{'ask': 'tick', 'at': None}]}, '(a Trigger review tick that failed), so a relaunch will not ask'),
                ({'relaunchAsks': None, 'asked': []}, "nothing in CodeRabbit's state is ours to ask, so a relaunch will not ask"),
                ({'relaunchAsks': 'tick', 'asked': []}, 'a relaunch with coderabbitTrigger ticks it once'),
                ({'relaunchAsks': 'request', 'asked': [], 'retryAt': '2026-10-23T02:07:00Z'},
                 'rate-limited until 2026-10-23T02:07:00Z and this run never asked on the head: a relaunch with coderabbitTrigger after that'),
                ({'relaunchAsks': 'request', 'asked': [{'ask': 'tick', 'at': '2026-10-23T01:00:00Z'},
                                                        {'ask': 'request', 'at': '2026-10-23T02:08:00Z', 'requests': 2}], 'nextAt': '2026-10-23T03:08:00Z'},
                 'a launch with coderabbitTrigger waits it out, commenting `@coderabbitai review` once per reset window, '
                 'next after 2026-10-23T03:08:00Z (2 request attempts on this head so far)'),
                ({'relaunchAsks': 'request', 'asked': [{'ask': 'request', 'at': None, 'requests': 1}], 'nextAt': '2026-10-23T03:08:00Z'},
                 'next after 2026-10-23T03:08:00Z (1 request attempt on this head so far)'),
                ({'relaunchAsks': 'request', 'asked': [], 'nextAt': '2026-10-23T02:08:00Z'}, 'next after 2026-10-23T02:08:00Z'),
                ({'relaunchAsks': 'request', 'asked': [], 'nextAt': None}, 'it is rate-limited, and a launch with coderabbitTrigger waits it out, '
                 'commenting `@coderabbitai review` once per reset window, next once a launch dates the limit'),
                ({'relaunchAsks': 'request', 'asked': [{'ask': 'tick', 'at': None}], 'retryAt': None},
                 'no tick asked for its review: a relaunch with coderabbitTrigger comments `@coderabbitai review` once')):
            with self.subTest(ask):
                blockers = self.run_it(stopped(ask))[1]['blockers']
                self.assertEqual(len(blockers), 1, blockers)
                self.assertIn(says, blockers[0])
                self.assertEqual('`@coderabbitai review` on the PR or accepts the head' in blockers[0], ask['relaunchAsks'] is None, blockers[0])
        # #272: another stop with CodeRabbit unreviewed notes whether a relaunch asks; its own blocker is unchanged.
        for reason in ('reviews-pending', 'budget-exhausted', 'yielded'):
            for ask, says in (({'relaunchAsks': None, 'asked': [{'ask': 'tick', 'at': '2026-10-23T01:00:00Z'}]},
                               'CodeRabbit has not reviewed the head; this run already used its one ask on the head (a Trigger review tick '
                               'at 2026-10-23T01:00:00Z), so a relaunch will not ask CodeRabbit on this head'),
                              ({'relaunchAsks': 'tick', 'asked': []}, 'CodeRabbit has not reviewed the head; it skipped the head for a manual review '
                               'and this run never ticked its box on it: a relaunch with coderabbitTrigger ticks it once')):
                with self.subTest(reason=reason, ask=ask):
                    data = stopped(ask)
                    data['result']['reason'] = reason
                    s = self.run_it(data)[1]
                    self.assertEqual(s['notes'], [says])
                    self.assertFalse([b for b in s['blockers'] if 'CodeRabbit' in b], s['blockers'])
        # An output without the record: a failed tick leaves nothing on the PR, so its ask cannot be read there.
        blockers = self.run_it(stopped(None))[1]['blockers']
        self.assertEqual(len(blockers), 1, blockers)
        self.assertIn('does not record its asks, and a failed tick leaves nothing on the PR, so a relaunch may not ask again: '
                      'the user comments `@coderabbitai review`', blockers[0])

    def test_every_reviewer_keeps_its_state_kind_and_evidence(self):
        # #85: a Copilot quota notice left no finding, so its record is the only trace.
        data = output()
        data['result']['observation']['reviews']['bots'] = [
            {'bot': 'coderabbit', 'state': 'settled', 'kind': 'limited', 'sha': HEAD, 'reason': 'Review rate limited',
             'evidence': ['comment 5'], 'retryAt': '2026-10-13T10:55:19Z'},
            {'bot': 'copilot', 'state': 'settled', 'kind': 'limited', 'sha': HEAD, 'reason': 'the requesting user reached the Copilot quota',
             'evidence': ['review 3313 on aaaaaaaa'], 'retryAt': None}]
        bots = self.run_it(data)[1]['observation']['bots']
        self.assertEqual(bots[0], {'bot': 'coderabbit', 'state': 'settled', 'kind': 'limited', 'sha': HEAD, 'reason': 'Review rate limited',
                                   'evidence': ['comment 5'], 'retryAt': '2026-10-13T10:55:19Z'})
        self.assertEqual((bots[1]['bot'], bots[1]['kind'], bots[1]['evidence']), ('copilot', 'limited', ['review 3313 on aaaaaaaa']))

    def test_a_pass_names_the_reviewers_that_did_not_review_the_head(self):
        # #191/#193: a pass is no review; whoever merges on a bot's review reads which did not
        unreviewed = [{'bot': 'greptile', 'state': 'settled', 'kind': 'limited', 'sha': HEAD, 'evidence': ['review 51 on aaaaaaaa'],
                       'reason': 'paused notice on the head: Greptile did not review it', 'retryAt': None}]
        s = self.run_it(output(result={'pass': True, 'status': 'complete', 'reason': None, 'unreviewed': unreviewed}))[1]
        self.assertEqual(s['result']['unreviewed'], unreviewed)
        self.assertIn('passed with reviewer(s) that did not review the head: greptile settled/limited '
                      '(paused notice on the head: Greptile did not review it)', s['notes'])
        self.assertFalse([n for n in self.run_it(output(result={'pass': True, 'status': 'complete', 'reason': None}))[1]['notes'] if 'did not review' in n])

    def test_a_reply_receipt_names_the_verdicts_it_answered(self):
        data = output()
        obs = data['result']['observation']
        obs['reviews']['findings'] += [{'findingId': '41#1', 'commentId': 41, 'verdict': 'stale'},
                                       {'findingId': '41#2', 'commentId': 41, 'verdict': 'invalid'},
                                       {'findingId': '41#3', 'commentId': 410, 'verdict': 'valid'}]
        obs['actions']['refutedPosts'] = {'pass': True, 'detail': '', 'receipts': [{'commentId': 41, 'kind': 'review', 'posted': True}]}
        _, s = self.run_it(json.dumps(data))
        self.assertEqual([(r['batch'], r['commentId'], r['findingVerdicts']) for r in s['receipts']['replies']],
                         [('refutedPosts', 41, ['invalid', 'stale']), ('fixNotePosts', 7, ['valid'])],
                         'a refutation of a stale finding says so; the join is on commentId, not the findingId prefix')

    def test_a_multi_cycle_launch_keeps_every_cycle_s_receipts(self):
        data = output()
        obs = data['result']['observation']
        early = {'refutedPosts': {'pass': True, 'detail': '', 'receipts': [{'commentId': 501, 'kind': 'review', 'posted': True}]}}
        obs['launchActions'] = [{'cycle': 1, **early}, {'cycle': 2, **obs['actions']}]
        _, s = self.run_it(data)
        self.assertEqual([(r['batch'], r['commentId']) for r in s['receipts']['replies']], [('refutedPosts', 501), ('fixNotePosts', 7)])
        self.assertEqual([p['lane'] for p in s['receipts']['pushes']], ['reviewPush'])
        self.assertEqual(s['receipts']['botAsks'], [])

    def test_bot_asks_are_receipts_of_every_cycle_verbatim(self):
        # #272: the bot asks a launch posted were only in its logs.
        data = output()
        obs = data['result']['observation']
        tick = {'bot': 'coderabbit', 'kind': 'tick', 'outcome': 'ticked', 'commentId': 5, 'at': '2026-10-23T01:00:00Z', 'sha': HEAD}
        ask = {'bot': 'greptile', 'kind': 'request', 'outcome': 'requested', 'commentId': 7, 'at': '2026-10-23T01:02:00Z', 'sha': HEAD}
        obs['launchActions'] = [{'cycle': 1, 'botAsks': [tick]}, {'cycle': 2, 'botAsks': None}, {'cycle': 3, **obs['actions'], 'botAsks': [ask]}]
        _, s = self.run_it(data)
        self.assertEqual(s['receipts']['botAsks'], [tick, ask])
        self.assertEqual(s['blockers'], [])

    def test_each_receipt_keeps_its_own_cycle_s_verdicts(self):
        data = output()
        obs = data['result']['observation']
        early = {'findings': [{'commentId': 501, 'verdict': 'invalid'}],
                 'refutedPosts': {'pass': True, 'detail': '', 'receipts': [{'commentId': 501, 'posted': True}]}}
        obs['launchActions'] = [{'cycle': 1, **early}, {'cycle': 2, **obs['actions'], 'findings': []}]
        _, s = self.run_it(data)
        self.assertEqual(s['receipts']['replies'][0]['findingVerdicts'], ['invalid'])

    def test_a_retried_reply_batch_supersedes_the_failed_one(self):
        data = output()
        obs = data['result']['observation']
        failed = {'refutedPosts': {'pass': False, 'detail': 'unsettled: 1', 'receipts': [{'commentId': 1, 'posted': False}]}}
        retried = {'refutedPosts': {'pass': True, 'detail': '', 'receipts': [{'commentId': 1, 'posted': True, 'replyId': 501}]}}
        obs['launchActions'] = [{'cycle': 1, **failed}, {'cycle': 2, **retried}]
        _, s = self.run_it(data)
        self.assertFalse([b for b in s['blockers'] if 'reply batch' in b], s['blockers'])
        self.assertEqual([r['posted'] for r in s['receipts']['replies']], [False, True])
        obs['launchActions'].reverse()
        _, s = self.run_it(data)
        self.assertTrue([b for b in s['blockers'] if 'refutedPosts' in b], s['blockers'])

    def test_what_chief_must_settle_is_a_blocker(self):
        data = output()
        obs = data['result']['observation']
        obs['actions']['reviewPush'] = {'pass': False, 'committed': None, 'sha': None, 'detail': 'push died' + ' z' * 200}
        obs['actions']['refutedPosts'] = {'pass': False, 'detail': 'unsettled: 9, 10', 'receipts': [
            {'commentId': 9, 'kind': 'review', 'sent': True, 'posted': True, 'verified': False, 'error': None}]}
        obs['actions']['deferralPosts'] = {'pass': False, 'detail': 'nothing publishable', 'receipts': []}
        obs['actions']['adoption'] = {'from': HEAD, 'to': NEXT, 'publication': 'unknown'}
        obs['actions']['error'] = 'gh died'
        obs['ci']['realFailures'].append(failure('l476 msc', complete=False))
        data['result']['state']['expectedHead'] = HEAD
        _, s = self.run_it(data)
        self.assertEqual(s['blockers'], [
            'uncertain publication: reviewPush: ' + ('push died' + ' z' * 200)[:launch_result.CUT] + '…',
            'reply batch did not pass: refutedPosts: unsettled: 9, 10',
            'reply batch did not pass: deferralPosts: nothing publishable',
            'uncertain publication: adoption aaaaaaaa..bbbbbbbb: unknown',
            'action error: gh died',
            'CI evidence incomplete for hil-tinyusb (tinyusb.json) / l476 msc',
            f"heads disagree: {{'result': '{NEXT}', 'state': '{HEAD}'}}"])
        self.assertEqual(s['receipts']['pushes'][0]['detail'], 'push died' + ' z' * 200, 'a push receipt is never cut')
        self.assertEqual(s['receipts']['replies'][0], {'batch': 'refutedPosts', 'findingVerdicts': [], 'commentId': 9, 'kind': 'review', 'sent': True,
                                                       'posted': True, 'verified': False, 'error': None})

    def test_a_handed_off_comment_and_a_settlement_that_did_not_settle_are_blockers(self):
        data = output()
        data['result']['handoffs'] = [
            {'commentId': 5, 'commentDigest': 'd5', 'why': 'edited after its finding ids were carried'},
            {'commentId': 7, 'why': 'no reply posted: over length', 'draft': 'a long draft'},
            {'commentId': 8, 'why': 'edited after we answered it', 'rejudge': {'reason': 'the rewrite dropped dma_drain()'}}]
        data['result']['settlements'] = [
            {'commentId': 3, 'outcome': 'settled', 'replyId': 30},
            {'commentId': 5, 'outcome': 'refused', 'why': 'a point of the comment is held'},
            {'commentId': 6, 'outcome': 'not processed', 'why': 'no review harvest in this launch'}]
        _, s = self.run_it(data)
        self.assertEqual([b for b in s['blockers'] if b.startswith('comment ')], [
            "comment 5 is the caller's to answer (chief's reply takeover rule), then to settle through replySettlements: "
            'edited after its finding ids were carried; settlement refused: a point of the comment is held',
            "comment 7 is the caller's to answer (chief's reply takeover rule), then to settle through replySettlements: "
            'no reply posted: over length; draft: a long draft',
            "comment 8 is the caller's to answer (chief's reply takeover rule), then to settle through replySettlements: "
            "edited after we answered it; rejudge requested: the rewrite dropped dma_drain()",
            'comment 6: settlement not processed: no review harvest in this launch'])
        self.assertEqual(s['result']['handoffs'][0]['commentDigest'], 'd5', 'the whole handoff stays in the reading')

    def test_an_unmarked_sonarcloud_issue_is_a_blocker_even_on_a_green_launch(self):
        data = output(result={'pass': True, 'status': 'complete', 'sonarUnmarked': [
            {'commentId': 3, 'how': 'refutation', 'last': {'commentId': 3, 'issue': 'K3', 'outcome': 'failed', 'detail': 'HTTP 403'}},
            {'commentId': 4, 'how': 'fixNote', 'last': None}]})
        _, s = self.run_it(data)
        self.assertIn('SonarCloud issue of comment 3 not marked false positive: failed: HTTP 403', s['blockers'])
        self.assertIn('SonarCloud issue of comment 4 not marked false positive: not asked', s['blockers'])
        self.assertEqual(len(s['result']['sonarUnmarked']), 2)

    def test_the_checkout_is_compared_with_the_state(self):
        _, s = self.run_it(output(), '--checkout', self.repo())
        self.assertEqual(s['checkout']['branch'], 'pr')
        self.assertEqual(s['checkout']['dirty'], ['?? left.txt'])
        self.assertTrue(any(b.startswith('heads disagree') and "'checkout'" in b for b in s['blockers']))
        self.assertIn("the checkout is dirty: ['?? left.txt']", s['blockers'])

    def test_a_held_finding_says_why(self):
        data = output()
        data['result']['observation']['reviews']['findings'].append(
            {'findingId': '9#1', 'commentId': 9, 'commentDigest': 'd9', 'source': 'greptile', 'verdict': 'valid', 'file': 'b.c', 'line': 2,
             'hold': 'out of scope per https://github.com/o/r/issues/1 (gap): defer it by findingId, or drop the topic'})
        _, s = self.run_it(data)
        self.assertNotIn('held', s['observation']['findings'][0])
        self.assertEqual(s['observation']['findings'][1]['held'], data['result']['observation']['reviews']['findings'][1]['hold'])

    def test_dirty_ide_metadata_is_a_note_as_in_the_workflow_preflight(self):
        tree = {'branch': 'pr', 'head': NEXT, 'dirty': [' M .idea/misc.xml', '?? .idea/inspectionProfiles/', ' M sub/.idea/x.xml']}
        s = launch_result.summarize(output(), '/t/w.output', tree=tree)
        self.assertEqual(s['blockers'], [])
        self.assertIn('ignoring 3 dirty .idea/ path(s) (IDE metadata)', s['notes'])
        self.assertEqual(s['checkout']['dirty'], tree['dirty'], 'the checkout is reported raw')
        for other in ('?? .idea-like/a', ' M src/.idea.c', 'R  .idea/a -> src/b.c', 'R  src/b.c -> sub/.idea/a', '?? "a\\tb.c"'):
            with self.subTest(other):
                s = launch_result.summarize(output(), '/t/w.output', tree={**tree, 'dirty': [' M .idea/misc.xml', other]})
                self.assertEqual(s['blockers'], [f'the checkout is dirty: {[other]}'])

    def test_any_stop_reason_keeps_its_own_fields(self):
        pending = {'lane': 'adopt', 'sha': NEXT, 'parent': HEAD, 'stage': 'push-unknown'}
        data = output(result={'reason': 'adopt-pending', 'pending': pending, 'detail': 'x', 'acceptedFailures': [{'cell': 'c'}],
                              'deferrals': [{'issue': 'https://example/1'}]})
        rc, s = self.run_it(data)
        self.assertEqual(rc, 0)
        for k, v in (('pending', pending), ('detail', 'x'), ('acceptedFailures', [{'cell': 'c'}]), ('deferrals', [{'issue': 'https://example/1'}])):
            self.assertEqual(s['result'][k], v)

    def test_log_lines_another_field_carries_are_dropped_and_repeats_folded(self):
        table = '| Bot | Finding |\n| --- | --- |\n| greptile | a.c:3 claim |'
        lines = ['preflight: ok',
                 'cycle 2: accepted failure 00aa matches no failure on this head',
                 'cycle 2 summary — CI red, reviews: coderabbit working (Review in progress)\n' + table,
                 'cycle 2: reply 8 to comment 7 exists with the wrong content (different (body)) — handed to the caller, not another reply',
                 'cycle 2: no reply posted to comment 9 (over length:\n200 chars) — handed to the caller, who checks the thread for an earlier attempt',
                 "cycle 2: comment 3 settled on the caller's reply 30",
                 'cycle 2: unclassified CI failure (not fixing): hil — x', 'cycle 2: SonarCloud gate CI failure (not fixing): SonarCloud — y',
                 'cycle 2: accepted failure 11bb matches no failure on this head',
                 'cycle 2: accepted failure 00aa matches no failure on this head',
                 'accepted failure not renewed by this launch, no longer accepted: key 22cc',
                 'cycle 1: reply 8 to comment 7 exists with the wrong content (x) — needs a human repair, not another reply',
                 "cycle 2: comment 3 not settled on the caller's reply 30 — dry run"]
        result = {'handoffs': [{'commentId': 7, 'replyId': 8, 'why': 'reply 8: different'}, {'commentId': 9, 'replyId': None, 'why': 'x'}],
                  'settlements': [{'commentId': 3, 'outcome': 'settled', 'replyId': 30}]}
        self.assertEqual(launch_result.logs(lines, result), [
            'preflight: ok',
            'cycle 2: accepted failure {00aa, 11bb} matches no failure on this head (3 lines)',
            "cycle 2 summary — CI red, reviews: coderabbit working (Review in progress) … (3 more lines in the output's logs)",
            'accepted failure not renewed by this launch, no longer accepted: key 22cc',
            'cycle 1: reply 8 to comment 7 exists with the wrong content (x) — needs a human repair, not another reply',
            "cycle 2: comment 3 not settled on the caller's reply 30 — dry run",
            '(2 "not fixing" CI failure lines, one per failure listed under ci)'],
            'handoffs and settlements carry the dropped lines; an older wording or a refused settlement stays')
        self.assertEqual(launch_result.logs(lines[3:6]), lines[3:6], 'without its record a line is the only copy, so it stays')

    def test_help_prints_the_usage(self):
        done = subprocess.run(['python3', str(ROOT / 'skills/pr-babysit/scripts/launch_result.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn('launch_result.py --output FILE [--state-ref FILE:DIGEST]', done.stdout)

    def test_unusable_inputs_are_refused(self):
        for argv, why in ((['--state-ref', 'nodigest'], 'FILE:DIGEST'), (['--accepted-out', 'x'], 'unrecognized arguments')):
            rc, s = self.run_it(output(), *argv)
            self.assertEqual(rc, 2)
            self.assertIn(why, s['error'])
        (self.dir / 'bad').write_bytes(b'\xff\xfe{')
        for data, why in (('{not json', 'is not JSON'), (None, 'cannot read --output'), ('[]', 'not a Workflow output object')):
            rc, s = self.run_it(data)
            self.assertEqual(rc, 2)
            self.assertIn(why, s['error'])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = launch_result.report(launch_result.collect, ['--output', str(self.dir / 'bad')])
        self.assertEqual((rc, 'cannot read --output' in json.loads(out.getvalue())['error']), (2, True), 'not UTF-8')


if __name__ == '__main__':
    unittest.main()
