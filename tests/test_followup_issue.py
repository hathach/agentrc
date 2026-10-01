"""Tests for followup-issue's publish.py against a fake GitHub behind the gh calls."""
import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'followup-issue' / 'scripts' / 'publish.py'
spec = importlib.util.spec_from_file_location('followup_publish', SCRIPT)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)

REPO, ME = 'o/r', 'hathach'


class FakeGitHub:
    """Issues, their comments and labels of one repository, and a log of every POST."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.labels = {'bug', 'followup', 'good first issue'}
        self.next_id = 100
        self.posts = []
        self.down = set()      # paths answering an error
        self.lose = set()      # POST paths whose response is lost after the write lands
        self.drop = set()      # POST paths whose response is lost and whose write never lands
        self.tamper = None     # a function altering what a read-back returns

    def add_issue(self, number, title, body='b', login='someone', state='open', pr=False):
        self.issues[number] = {'number': number, 'title': title, 'body': body, 'user': {'login': login},
                               'state': state, 'labels': [], 'html_url': f'https://github.com/{REPO}/issues/{number}',
                               **({'pull_request': {}} if pr else {})}
        self.comments.setdefault(number, [])

    def add_comment(self, number, body, login='someone'):
        self.next_id += 1
        c = {'id': self.next_id, 'body': body, 'user': {'login': login},
             'issue_url': f'https://api.github.com/repos/{REPO}/issues/{number}',
             'html_url': f'https://github.com/{REPO}/issues/{number}#issuecomment-{self.next_id}'}
        self.comments[number].append(c)
        return c

    def gh(self, args, stdin=None):
        method, path = args[2], args[3]
        body = json.loads(stdin) if stdin else None
        bare = path.split('?')[0]
        if method == 'POST' and bare in self.drop:
            self.posts.append(('dropped', body))
            return 1, '', 'gh: connection reset'
        if bare in self.down:
            return 1, '', f'gh: Server Error (HTTP 502)\n{path}'
        try:
            out = self.rest(method, bare, body, '--paginate' in args)
        except KeyError:
            return 1, '', f'gh: Not Found (HTTP 404)\n{path}'
        if method == 'POST' and bare in self.lose:
            return 1, '', 'gh: connection reset'
        return 0, json.dumps(out), ''

    def rest(self, method, path, body, paginate):
        def page(xs):
            if not paginate:
                return xs
            xs = list(xs)  # two per page, so a listing spans pages
            return [xs[i:i + 2] for i in range(0, len(xs), 2)] or [[]]

        if path == 'user':
            return {'login': ME}
        if m := re.fullmatch(rf'repos/{REPO}/labels/(.+)', path):
            name = m.group(1).replace('%20', ' ')
            if name not in self.labels:
                raise KeyError(name)
            return {'name': name}
        if path == f'repos/{REPO}/issues' and method == 'GET':
            return page(i for i in self.issues.values() if i['state'] == 'open')
        if path == f'repos/{REPO}/issues' and method == 'POST':
            self.posts.append(('issue', body))
            n = max(self.issues, default=0) + 1
            self.add_issue(n, body['title'], body['body'], ME)
            self.issues[n]['labels'] = [{'name': x} for x in body.get('labels', [])]
            return self.issues[n]
        if m := re.fullmatch(rf'repos/{REPO}/issues/(\d+)', path):
            i = dict(self.issues[int(m.group(1))])
            return self.tamper(i) if self.tamper else i
        if (m := re.fullmatch(rf'repos/{REPO}/issues/(\d+)/comments', path)) and method == 'GET':
            return page(self.comments[int(m.group(1))])
        if (m := re.fullmatch(rf'repos/{REPO}/issues/(\d+)/comments', path)) and method == 'POST':
            self.posts.append(('comment', body))
            return self.add_comment(int(m.group(1)), body['body'], ME)
        if m := re.fullmatch(rf'repos/{REPO}/issues/comments/(\d+)', path):
            c = next(c for cs in self.comments.values() for c in cs if c['id'] == int(m.group(1)))
            return self.tamper(dict(c)) if self.tamper else c
        raise KeyError(path)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(publish, 'gh', self.fake.gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def body(self, text='Evidence: run 42 failed.\n\nOriginating PR: https://github.com/o/r/pull/7\n'):
        f = Path(self.tmp.name) / f'body{len(list(Path(self.tmp.name).iterdir()))}.md'
        f.write_text(text)
        return str(f)

    def run_script(self, *argv, allow=(REPO,)):
        out = io.StringIO()
        with redirect_stdout(out):
            code = publish.main([*argv, '--repo', REPO, *[a for r in allow for a in ('--allow-repo', r)]])
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def create(self, title='pr-babysit: drop the stale lane', *extra, **kw):
        return self.run_script('create', '--title', title, '--body-file', kw.pop('body', None) or self.body(), *extra, **kw)

    def test_creates_labels_and_verifies_an_issue(self):
        code, r = self.create('x: y', '--label', 'followup')
        self.assertEqual((code, r['outcome'], r['posted'], r['issue']), (0, 'verified', True, 1))
        self.assertEqual(self.fake.posts[0][1]['labels'], ['followup'])

    def test_a_repository_outside_the_allowlist_is_refused_before_any_call(self):
        code, r = self.create(allow=('hathach/agentrc',))
        self.assertEqual((code, r['outcome']), (2, 'refused'))
        self.assertEqual(self.fake.posts, [])

    def test_attribution_or_a_session_link_is_refused_before_any_call(self):
        calls = []
        counting = mock.patch.object(publish, 'gh', lambda *a, **k: calls.append(a) or self.fake.gh(*a, **k))
        self.fake.add_issue(5, 'X: Y')
        with counting:
            for text in ('Details.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n',
                         'See https://claude.ai/code/session_01ABC for the run.\n',
                         'Fix.\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n',
                         'Thread: https://chatgpt.com/c/abc123\n', 'Shared: https://chatgpt.com/share/abc123\n',
                         'Task: https://chatgpt.com/codex/tasks/t1\n', 'Notes.\n\nGenerated by Codex助手\n',
                         'Notes.\n\nClaude-Session: abc\n'):
                code, r = self.create(body=self.body(text))
                self.assertEqual((code, r['outcome']), (2, 'refused'), text)
                self.assertIn(text.strip().splitlines()[-1].strip(), r['error'], 'the refusal names the line')
                code, r = self.run_script('comment', '--issue', '5', '--body-file', self.body(text))
                self.assertEqual((code, r['outcome']), (2, 'refused'), text)
            code, r = self.create('Generated by Claude')
            self.assertEqual((code, r['outcome']), (2, 'refused'), 'the title is read too')
        self.assertEqual(calls, [])

    def test_prose_about_attribution_and_unicode_lookalikes_are_not_refused(self):
        for n, text in enumerate(('Fix.\n\nSigned-off-by: Ha Thach <thach@tinyusb.org>\n', 'Generated by GPTimer.\n',
                                  'Drop the Generated-with footer from PR bodies.\n', 'ſession-url: x\n')):
            code, r = self.create(f'x: case {n}', body=self.body(text))
            self.assertEqual((code, r['outcome']), (0, 'verified'), text)

    def test_a_missing_label_is_refused(self):
        code, r = self.create('x', '--label', 'nope')
        self.assertEqual((code, r['outcome']), (2, 'refused'))
        self.assertIn("'nope'", r['error'])
        self.assertEqual(self.fake.posts, [])

    def test_a_title_match_by_someone_else_is_a_collision_not_a_reuse(self):
        self.fake.add_issue(5, 'X: Y', 'other problem')
        self.fake.add_issue(6, 'x: y', 'pr', pr=True)
        code, r = self.create('x: y')
        self.assertEqual((code, r['outcome']), (1, 'collision'))
        self.assertEqual([c['issue'] for c in r['candidates']], [5])
        self.assertEqual(self.fake.posts, [])

    def test_our_own_earlier_issue_is_found_again_not_duplicated(self):
        body = self.body()
        self.create('x', body=body)
        code, r = self.create('x', body=body)
        self.assertEqual((code, r['outcome'], r['posted'], r['issue']), (0, 'verified', False, 1))
        self.assertEqual(len(self.fake.posts), 1)

    def test_a_label_name_with_spaces_is_found(self):
        code, r = self.create('x', '--label', 'good first issue')
        self.assertEqual((code, r['outcome']), (0, 'verified'))

    def test_a_collision_on_a_later_listing_page_is_seen(self):
        for n in range(1, 6):
            self.fake.add_issue(n, f'other {n}')
        code, r = self.create('other 5')
        self.assertEqual((code, r['outcome'], r['candidates'][0]['issue']), (1, 'collision', 5))

    def test_our_earlier_issue_without_the_label_is_a_mismatch_not_verified(self):
        body = self.body()
        self.create('x', body=body)
        code, r = self.create('x', '--label', 'followup', body=body)
        self.assertEqual((code, r['outcome'], r['posted']), (1, 'mismatch', False))
        self.assertIn('labels', r['error'])
        self.assertEqual(len(self.fake.posts), 1)

    def test_a_read_back_closed_issue_is_a_mismatch(self):
        self.fake.tamper = lambda i: {**i, 'state': 'closed'}
        code, r = self.create('x')
        self.assertEqual((code, r['outcome']), (1, 'mismatch'))
        self.assertIn('state', r['error'])

    def test_a_failed_listing_creates_nothing(self):
        self.fake.down.add(f'repos/{REPO}/issues')
        code, r = self.create()
        self.assertEqual((code, r['outcome']), (2, 'refused'))
        self.assertEqual(self.fake.posts, [])

    def test_a_lost_create_response_is_recovered_by_author_and_body(self):
        self.fake.lose.add(f'repos/{REPO}/issues')
        code, r = self.create('x')
        self.assertEqual((code, r['outcome'], r['issue']), (0, 'verified', 1))
        self.assertEqual(len(self.fake.posts), 1)

    def test_a_lost_create_response_with_no_trace_is_uncertain(self):
        self.fake.drop.add(f'repos/{REPO}/issues')
        code, r = self.create('x')
        self.assertEqual((code, r['outcome']), (1, 'uncertain'))
        self.assertNotIn('posted', r)

    def test_a_read_back_that_differs_is_a_mismatch(self):
        self.fake.tamper = lambda i: {**i, 'body': 'changed'}
        code, r = self.create('x')
        self.assertEqual((code, r['outcome'], r['issue']), (1, 'mismatch', 1))
        self.assertIn('body', r['error'])

    def test_a_comment_goes_only_on_an_open_issue(self):
        self.fake.add_issue(4, 'closed', state='closed')
        self.fake.add_issue(9, 'a pr', pr=True)
        for n in ('4', '9', '99'):
            code, r = self.run_script('comment', '--issue', n, '--body-file', self.body())
            self.assertEqual((code, r['outcome']), (2, 'refused'), n)
        self.assertEqual(self.fake.posts, [])

    def test_several_of_our_matching_posts_are_a_mismatch_not_a_pick(self):
        self.fake.add_issue(3, 'covers it')
        self.fake.add_comment(3, 'Same.\n', ME)
        self.fake.add_comment(3, 'Same.\n', ME)
        code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body('Same.\n'))
        self.assertEqual((code, r['outcome']), (1, 'mismatch'))
        self.assertEqual(self.fake.posts, [])

    def test_comments_once_and_verifies(self):
        self.fake.add_issue(3, 'covers it')
        body = self.body('New evidence: run 43.\n')
        code, r = self.run_script('comment', '--issue', '3', '--body-file', body)
        self.assertEqual((code, r['outcome'], r['posted'], r['issue']), (0, 'verified', True, 3))
        code, r = self.run_script('comment', '--issue', '3', '--body-file', body)
        self.assertEqual((code, r['outcome'], r['posted']), (0, 'verified', False))
        self.assertEqual(len(self.fake.posts), 1)

    def test_a_lost_comment_response_is_recovered(self):
        self.fake.add_issue(3, 'covers it')
        self.fake.lose.add(f'repos/{REPO}/issues/3/comments')
        code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body('More.\n'))
        self.assertEqual((code, r['outcome']), (0, 'verified'))
        self.assertEqual(len(self.fake.posts), 1)

    def test_usage_errors(self):
        for argv in (['create'], ['create', '--title', 't', '--body-file', 'f', '--issue', '99'],
                     ['comment', '--issue', '3'], ['comment', '--issue', '3', '--body-file', 'f', '--label', 'bug']):
            with self.assertRaises(SystemExit) as e, redirect_stdout(io.StringIO()), \
                    mock.patch('sys.stderr', io.StringIO()):
                publish.main([*argv, '--repo', REPO, '--allow-repo', REPO])
            self.assertEqual(e.exception.code, 2, argv)


if __name__ == '__main__':
    unittest.main()
