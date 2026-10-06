"""Tests for pr-babysit's sonar.py against a fake gh and a fake SonarCloud."""
import http.server
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'sonar.py'
spec = importlib.util.spec_from_file_location('pr_babysit_sonar', SCRIPT)
sonar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sonar)

HEAD = 'a' * 40
OTHER = 'b' * 40


def scanning(key, pr=7, project='o_r', login=sonar.BOT, link_pr=None):
    link = f'https://sonarcloud.io/project/issues?id={project}&issues={key}&open={key}&pullRequest={link_pr or pr}'
    return {'user': {'login': login}, 'pull_request_url': f'https://api.github.com/repos/o/r/pulls/{pr}',
            'body': f'## SonarCloud / rule\n\n<!--SONAR_ISSUE_KEY:{key}-->claim <p>See more on <a href="{link}">SonarQube Cloud</a></p>'}


class FakeSonar:
    """SonarCloud's issue search, PR list, add_comment and do_transition, in memory."""

    def __init__(self, issues, analysed=HEAD, transitions=('falsepositive', 'wontfix', 'confirm')):
        self.issues = {k: {'key': k, 'status': s, 'resolution': None, 'comments': [], 'transitions': list(transitions)}
                       for k, s in issues.items()}
        self.analysed = analysed
        self.calls = []

    def __call__(self, token, path, data=None):
        self.calls.append((path.split('?')[0], data))
        assert token == 'tok'
        if path.startswith('/api/issues/search'):
            q = dict(x.split('=', 1) for x in path.split('?', 1)[1].split('&'))
            assert q['componentKeys'] == 'o_r' and q['pullRequest'] == '7'
            return {'issues': [json.loads(json.dumps(self.issues[q['issues']]))] if q['issues'] in self.issues else []}
        if path.startswith('/api/project_pull_requests/list'):
            return {'pullRequests': [{'key': '7', 'commit': {'sha': self.analysed}}, {'key': '8', 'commit': {'sha': OTHER}}]}
        issue = self.issues[data['issue']]
        if path == '/api/issues/add_comment':
            issue['comments'].append({'markdown': data['text']})
        elif path == '/api/issues/do_transition':
            assert data['transition'] == 'falsepositive'
            issue.update(status='RESOLVED', resolution='FALSE-POSITIVE', transitions=['reopen'])
        return {}


class SonarTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.manifest = self.tmp / 'm.json'
        self.comments = {}
        self.enterContext(mock.patch.dict(os.environ, {'SONAR_TOKEN': 'tok'}))
        self.enterContext(mock.patch.object(sonar, 'attempt', self.gh))

    def gh(self, *argv, input=None):
        cid = int(argv[-1].rsplit('/', 1)[1])
        if cid not in self.comments:
            return 1, '', 'gh: Not Found (HTTP 404)'
        return 0, json.dumps(self.comments[cid]), ''

    def run_mark(self, fake, items, head=HEAD, manifest=None, receipt=None):
        self.manifest.write_text(json.dumps(manifest or {'items': [
            {'commentId': c, 'commentDigest': self.digest(c), 'how': how, 'note': note, 'digest': sonar.fnv1a(note)}
            for c, how, note in items]}))
        out, self.stderr = io.StringIO(), io.StringIO()
        argv = ['--pr', '7', '--head', head, '--manifest', str(self.manifest)] + (['--receipt', str(receipt)] if receipt else [])
        with mock.patch.object(sonar, 'sonar', fake), redirect_stdout(out), redirect_stderr(self.stderr):
            code = sonar.report(sonar.mark, argv, seal=True)
        line = json.loads(out.getvalue().splitlines()[-1])
        return code, line

    def digest(self, cid):
        return sonar.digest((self.comments.get(cid) or {}).get('body', ''))

    def outcomes(self, line):
        return {r['commentId']: (r['outcome'], r['issue']) for r in line['results']}

    def test_a_refuted_open_issue_is_commented_and_marked_false_positive(self):
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'OPEN'})
        code, line = self.run_mark(fake, [(1, 'refutation', 'Not injectable: a list argv.')])
        self.assertEqual(code, 0)
        self.assertEqual(self.outcomes(line), {1: ('marked', 'K1')})
        self.assertEqual(fake.issues['K1']['comments'], [{'markdown': 'Not injectable: a list argv.'}])
        self.assertEqual(fake.issues['K1']['resolution'], 'FALSE-POSITIVE')
        self.assertIn('seal', line)

    def test_a_refutation_is_marked_whatever_head_sonarcloud_analysed(self):
        self.comments[1] = scanning('K1')
        code, line = self.run_mark(FakeSonar({'K1': 'OPEN'}, analysed=OTHER), [(1, 'refutation', 'n')])
        self.assertEqual(self.outcomes(line), {1: ('marked', 'K1')})

    def test_a_fixed_issue_is_marked_only_once_sonarcloud_analysed_the_head(self):
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'OPEN'}, analysed=OTHER)
        _, line = self.run_mark(fake, [(1, 'fixNote', 'Fixed in X.')])
        self.assertEqual(self.outcomes(line), {1: ('waiting', 'K1')})
        self.assertEqual(fake.issues['K1']['status'], 'OPEN')
        fake.analysed = HEAD
        _, line = self.run_mark(fake, [(1, 'fixNote', 'Fixed in X.')])
        self.assertEqual(self.outcomes(line), {1: ('marked', 'K1')})

    def test_a_resolved_issue_is_left_alone(self):
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'CLOSED'})
        _, line = self.run_mark(fake, [(1, 'fixNote', 'n'), ])
        self.assertEqual(self.outcomes(line), {1: ('resolved', 'K1')})
        self.assertFalse([c for c in fake.calls if c[0] in ('/api/issues/add_comment', '/api/issues/do_transition')])

    def test_a_comment_naming_no_sonarcloud_issue_of_the_pr_is_skipped(self):
        self.comments[1] = scanning('K1', login='coderabbitai[bot]')
        self.comments[2] = {'user': {'login': sonar.BOT}, 'pull_request_url': '.../pulls/7', 'body': 'PVS-Studio V1004'}
        self.comments[3] = scanning('K3', pr=8)
        self.comments[4] = scanning('K4', link_pr=8)
        fake = FakeSonar({'K1': 'OPEN', 'K3': 'OPEN', 'K4': 'OPEN'})
        _, line = self.run_mark(fake, [(i, 'refutation', 'n') for i in (1, 2, 3, 4)])
        self.assertEqual({k: v[0] for k, v in self.outcomes(line).items()}, dict.fromkeys((1, 2, 3, 4), 'skipped'))
        self.assertEqual(fake.calls, [])

    def test_a_comment_edited_since_its_answer_is_skipped_before_sonarcloud_is_asked(self):
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'OPEN', 'K2': 'OPEN'})
        answered = {'items': [{'commentId': 1, 'commentDigest': self.digest(1), 'how': 'refutation', 'note': 'n', 'digest': sonar.fnv1a('n')}]}
        self.comments[1] = scanning('K2')
        _, line = self.run_mark(fake, [], manifest=answered)
        self.assertEqual(line['results'], [{'commentId': 1, 'issue': None, 'outcome': 'skipped', 'detail': 'edited since its answer'}])
        self.assertEqual(fake.calls, [])

    def test_an_identical_comment_is_not_added_twice(self):
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'OPEN'})
        fake.issues['K1']['comments'] = [{'markdown': 'n'}]
        self.run_mark(fake, [(1, 'refutation', 'n')])
        self.assertEqual(fake.issues['K1']['comments'], [{'markdown': 'n'}])

    def test_a_failure_is_per_item_and_named(self):
        self.comments[2] = scanning('K2')
        self.comments[3] = scanning('K3')
        fake = FakeSonar({'K2': 'OPEN', 'K3': 'OPEN'})
        fake.issues['K3']['transitions'] = ['confirm']
        _, line = self.run_mark(fake, [(1, 'refutation', 'n'), (2, 'refutation', 'n'), (3, 'refutation', 'n')])
        got = {r['commentId']: r for r in line['results']}
        self.assertEqual(got[1]['outcome'], 'failed')
        self.assertIn('HTTP 404', got[1]['detail'])
        self.assertEqual(got[2]['outcome'], 'marked')
        self.assertEqual((got[3]['outcome'], got[3]['issue']), ('failed', 'K3'))
        self.assertEqual(fake.issues['K3']['status'], 'OPEN')

    def test_a_read_back_that_is_not_false_positive_fails(self):
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'OPEN'})
        real = fake.__call__

        def ignores_transition(token, path, data=None):
            return {} if path == '/api/issues/do_transition' else real(token, path, data)
        _, line = self.run_mark(ignores_transition, [(1, 'refutation', 'n')])
        self.assertEqual(self.outcomes(line), {1: ('failed', 'K1')})

    def test_refuses_without_a_token_or_with_a_miscopied_note(self):
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'OPEN'})
        with mock.patch.dict(os.environ, {'SONAR_TOKEN': ''}):
            code, line = self.run_mark(fake, [(1, 'refutation', 'n')])
        self.assertEqual((code, line), (2, {'error': 'SONAR_TOKEN is not set'}))
        miscopied = {'items': [{'commentId': 1, 'commentDigest': self.digest(1), 'how': 'refutation', 'note': 'n', 'digest': sonar.fnv1a('m')}]}
        code, line = self.run_mark(fake, [], manifest=miscopied)
        self.assertEqual(code, 2)
        self.assertIn('does not match its digest', line['error'])
        self.assertEqual(fake.calls, [])

    def test_a_saved_receipt_is_replayed_without_github_sonarcloud_or_a_token_and_a_fresh_path_reads_again(self):
        # A relay retry reruns the script: without the receipt it finds the issue resolved by the first run (#55).
        self.comments[1] = scanning('K1')
        fake = FakeSonar({'K1': 'OPEN'})
        first = self.run_mark(fake, [(1, 'refutation', 'n')], receipt=self.tmp / 'sonar-1.json')
        self.assertEqual(self.outcomes(first[1]), {1: ('marked', 'K1')})
        calls = len(fake.calls)
        with mock.patch.object(sonar, 'attempt', side_effect=AssertionError('a replay reads GitHub')), \
                mock.patch.dict(os.environ, {'SONAR_TOKEN': ''}):
            self.assertEqual(self.run_mark(fake, [(1, 'refutation', 'n')], receipt=self.tmp / 'sonar-1.json'), first)
        self.assertEqual(len(fake.calls), calls, 'a replay asks SonarCloud nothing')
        _, again = self.run_mark(fake, [(1, 'refutation', 'n')], receipt=self.tmp / 'sonar-2.json')
        self.assertEqual(self.outcomes(again), {1: ('resolved', 'K1')})

    def test_a_receipt_that_is_not_this_requests_sealed_line_is_refused_and_nothing_is_asked(self):
        self.comments[1] = scanning('K1')
        path = self.tmp / 'sonar-1.json'
        self.run_mark(FakeSonar({'K1': 'OPEN'}), [(1, 'refutation', 'n')], receipt=path)
        saved = json.loads(path.read_text())
        tampered = {**saved, 'line': {**saved['line'], 'results': [{**saved['line']['results'][0], 'outcome': 'resolved'}]}}
        for content, why, head in [('{"line"', 'unreadable', HEAD), (json.dumps({**saved, 'line': {}}), 'malformed', HEAD),
                                   (json.dumps(saved), 'another request', OTHER), (json.dumps(tampered), 'seal', HEAD)]:
            path.write_text(content)
            fake = FakeSonar({'K1': 'OPEN'})
            code, line = self.run_mark(fake, [(1, 'refutation', 'n')], head=head, receipt=path)
            self.assertEqual(code, 2, why)
            self.assertIn(why, line['error'])
            self.assertEqual((fake.calls, fake.issues['K1']['status']), ([], 'OPEN'), why)

    def test_a_receipt_that_cannot_be_saved_still_reports_the_run(self):
        self.comments[1] = scanning('K1')
        path = self.tmp / 'sonar-1.json'
        with mock.patch.object(sonar.os, 'replace', side_effect=OSError('disk full')):
            code, line = self.run_mark(FakeSonar({'K1': 'OPEN'}), [(1, 'refutation', 'n')], receipt=path)
        self.assertEqual((code, self.outcomes(line), path.exists()), (0, {1: ('marked', 'K1')}, False))
        self.assertIn('receipt not saved: disk full', self.stderr.getvalue())

    def test_the_token_is_not_sent_on_to_a_redirect(self):
        seen = []

        class Server(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.server.name, self.headers.get('Authorization')))
                if self.server.name == 'first':
                    self.send_response(302)
                    self.send_header('Location', f'http://127.0.0.1:{other.server_port}/elsewhere')
                else:
                    self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        servers = []
        for name in ('first', 'other'):
            srv = http.server.HTTPServer(('127.0.0.1', 0), Server)
            srv.name = name
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            servers.append(srv)
        first, other = servers
        with mock.patch.object(sonar, 'HOST', f'http://127.0.0.1:{first.server_port}'):
            with self.assertRaisesRegex(sonar.Failed, 'HTTP 302'):
                sonar.sonar('tok', '/api/issues/search?x=1')
        self.assertEqual(seen, [('first', 'Basic dG9rOg==')])


if __name__ == '__main__':
    unittest.main()
