import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('launch_result', ROOT / 'skills/pr-babysit/scripts/launch_result.py')
launch_result = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch_result)

HEAD = 'a' * 40
NEXT = 'b' * 40


def failure(cell, verdict='rig-side', complete=True, accepted=False):
    return {'check': 'hil-tinyusb (tinyusb.json)', 'workflow': 'Build', 'job': 'hil-tinyusb', 'cell': cell,
            'signature': f'{cell} ... Failed', 'key': cell.encode().hex().ljust(16, '0')[:16], 'complete': complete, 'verdict': verdict, 'accepted': accepted,
            'firstError': f'{cell} failed ' + 'x' * 400}


def output(**over):
    actions = {'reviewPush': {'pass': True, 'committed': True, 'sha': NEXT, 'detail': f'{HEAD[:9]}..{NEXT[:9]}'},
               'fixNotePosts': {'pass': True, 'detail': '', 'receipts': [{'commentId': 7, 'kind': 'review-body', 'replyId': 8, 'sent': True,
                                              'posted': True, 'verified': True, 'resolved': None, 'error': None}]}}
    result = {
        'stateDigest': '0123abcd', 'pass': False, 'status': 'paused', 'reason': 'yielded', 'cycles': 2, 'head': NEXT,
        'rollup': {'cycles': [2], 'findings': {'total': 1, 'fixed': 1}, 'ci': {'total': 2}, 'reran': 0, 'pushed': [NEXT[:8]], 'replies': 1},
        'pending': [{'bot': 'copilot', 'state': 'absent', 'reason': 'no request'}],
        'observation': {'reviewedHead': HEAD, 'lane': 'both',
                        'reviews': {'bots': [{'bot': 'coderabbit', 'state': 'reviewed', 'sha': HEAD}],
                                    'findings': [{'findingId': '7#1', 'commentDigest': 'c0ffee00', 'source': 'coderabbit', 'verdict': 'valid',
                                                  'file': 'a.c', 'line': 3}]},
                        'ci': {'status': 'red', 'headSha': HEAD, 'infraRerun': [],
                               'realFailures': [failure('f072 cdc'), failure('pico host', verdict='real')]},
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


class LaunchResultTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_it(self, data, *argv):
        path = self.dir / 'w.output'
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(data if isinstance(data, str) else json.dumps(data))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = launch_result.report(launch_result.collect, ['--output', str(path), *argv])
        return rc, json.loads(out.getvalue())

    def repo(self):
        repo = self.dir / 'repo'
        subprocess.run(['git', 'init', '-q', '-b', 'pr', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
        (repo / 'left.txt').write_text('x')
        return str(repo)

    def test_a_launch_is_condensed(self):
        rc, s = self.run_it(output())
        self.assertEqual(rc, 0)
        self.assertEqual(s['result']['status'], 'paused')
        self.assertEqual(s['result']['rollup']['pushed'], [NEXT[:8]])
        self.assertEqual(s['stateRef'], {'outputFile': str((self.dir / 'w.output').resolve()), 'digest': '0123abcd'})
        self.assertEqual(s['blockers'], [], 'a push past the reviewed head is not a disagreement')
        self.assertEqual(s['observation']['findings'], [{'id': '7#1', 'digest': 'c0ffee00', 'source': 'coderabbit', 'verdict': 'valid', 'at': 'a.c:3'}])
        ci = s['observation']['ci']
        self.assertEqual(ci['verdicts'], {'rig-side': 1, 'real': 1})
        self.assertEqual(ci['settledCells'], {'hil-tinyusb (tinyusb.json)': [{'cell': 'f072 cdc', 'key': failure('f072 cdc')['key']}]},
                         'the key a caller passes back to accept it')
        self.assertEqual(ci['attention'], [failure('pico host', verdict='real')], 'an unsettled failure is shown in full')
        self.assertEqual(s['receipts']['replies'], [{'batch': 'fixNotePosts', 'commentId': 7, 'kind': 'review-body', 'replyId': 8, 'sent': True,
                                                     'posted': True, 'verified': True, 'resolved': None, 'error': None}], 'receipts verbatim')
        self.assertEqual(s['receipts']['pushes'][0]['committed'], True)
        self.assertEqual(s['result']['pending'][0]['bot'], 'copilot', 'result fields other than history/observation/state stay verbatim')
        self.assertNotIn('state', s['result'])
        self.assertEqual(s['launch'], {'agents': 3, 'tokens': 1000, 'secs': 9}, 'first start to last end')
        self.assertEqual(s['notes'], [])
        self.assertIn('rig-side "not fixing" lines', s['logs'][-1])
        self.assertTrue(s['logs'][1].endswith('whole line in the output\'s logs)'))

    def test_a_launch_that_threw_keeps_the_state_it_was_given(self):
        rc, s = self.run_it('', '--state-ref', '/t/w1.output:4841042f', '--checkout', self.repo())
        self.assertEqual(rc, 0)
        self.assertIsNone(s['result'])
        self.assertEqual(s['stateRef'], {'outputFile': '/t/w1.output', 'digest': '4841042f'})
        self.assertIn('threw before returning', s['blockers'][0])
        self.assertIn("the checkout is dirty: ['?? left.txt']", s['blockers'], 'even without a result')
        self.assertIn('no new state', s['notes'][0])

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
        self.assertEqual(s['receipts']['replies'][0], {'batch': 'refutedPosts', 'commentId': 9, 'kind': 'review', 'sent': True,
                                                       'posted': True, 'verified': False, 'error': None})

    def test_the_checkout_is_compared_with_the_state(self):
        _, s = self.run_it(output(), '--checkout', self.repo())
        self.assertEqual(s['checkout']['branch'], 'pr')
        self.assertEqual(s['checkout']['dirty'], ['?? left.txt'])
        self.assertTrue(any(b.startswith('heads disagree') and "'checkout'" in b for b in s['blockers']))
        self.assertIn("the checkout is dirty: ['?? left.txt']", s['blockers'])

    def test_any_stop_reason_keeps_its_own_fields(self):
        pending = {'lane': 'adopt', 'sha': NEXT, 'parent': HEAD, 'stage': 'push-unknown'}
        data = output(result={'reason': 'adopt-pending', 'pending': pending, 'detail': 'x', 'acceptedFailures': [{'cell': 'c'}],
                              'deferrals': [{'issue': 'https://example/1'}]})
        rc, s = self.run_it(data)
        self.assertEqual(rc, 0)
        for k, v in (('pending', pending), ('detail', 'x'), ('acceptedFailures', [{'cell': 'c'}]), ('deferrals', [{'issue': 'https://example/1'}])):
            self.assertEqual(s['result'][k], v)

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
