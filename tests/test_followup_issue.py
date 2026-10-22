"""Tests for followup-issue's publish.py against a fake GitHub behind the gh calls."""
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
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'followup-issue' / 'scripts' / 'publish.py'
spec = importlib.util.spec_from_file_location('followup_publish', SCRIPT)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)

REPO, ME = 'o/r', 'hathach'
# A gh stand-in that echoes stdin and appends UTF-8 text: a real process, so the pipe decoding is the script's own.
ECHO = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read() + ' ✓ 修复'.encode())"


def setUpModule():
    guard_survivors(__name__)


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
        self.garble = {}       # (method, path): what gh prints instead, one per call, after any write lands; None answers

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
        queue = self.garble.get((method, bare))
        garbled = queue.pop(0) if queue else None
        return 0, json.dumps(out) if garbled is None else garbled, ''

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


class GhTest(unittest.TestCase):
    def test_gh_output_is_read_as_utf8(self):
        real = subprocess.run
        with mock.patch.object(publish.subprocess, 'run', lambda argv, **kw: real([sys.executable, '-c', ECHO], **kw)):
            self.assertEqual(publish.gh(['api'], '{"title": "é"}'), (0, '{"title": "é"} ✓ 修复', ''))

    def test_a_byte_gh_prints_that_is_not_utf8_is_replaced_not_raised(self):
        real = subprocess.run
        stray = "import sys; sys.stdout.buffer.write(b'ok \\xff'); sys.stderr.buffer.write(b'err \\xfe'); sys.exit(1)"
        with mock.patch.object(publish.subprocess, 'run', lambda argv, **kw: real([sys.executable, '-c', stray], **kw)):
            self.assertEqual(publish.gh(['api']), (1, 'ok �', 'err �'))


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
        f.write_text(text, encoding='utf-8')
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

    def unusable(self, method, path, *outputs):
        self.fake.garble[(method, f'repos/{REPO}/{path}')] = list(outputs)

    def test_an_unusable_create_response_is_recovered_by_relisting(self):
        for n, bad in enumerate(('{', '', 'null', '[]', '{"html_url": "u"}', '{"number": null}'), 1):
            self.unusable('POST', 'issues', bad)
            code, r = self.create(f'x {n}')
            self.assertEqual((code, r['outcome'], r['posted'], r['issue']), (0, 'verified', True, n), bad)
            self.assertEqual(len(self.fake.posts), n, 'one POST each time')

    def test_an_unusable_answer_after_a_create_is_uncertain_not_refused(self):
        self.unusable('POST', 'issues', '{')
        self.unusable('GET', 'issues', None, '[[{"title": "x"}]]')   # the check before, then the relisting
        code, r = self.create('x')
        self.assertEqual((code, r['outcome']), (1, 'uncertain'))
        self.assertNotIn('posted', r)
        self.assertIn('relisting failed: unusable response', r['error'])
        self.unusable('GET', 'issues/2', '{"title": "y"}')
        code, r = self.create('y')
        self.assertEqual((code, r['outcome'], r['posted'], r['issue']), (1, 'uncertain', True, 2), 'read-back unusable')
        self.assertEqual(len(self.fake.posts), 2, 'one POST each time')

    def test_an_unusable_answer_before_any_post_is_refused(self):
        for method, path in (('GET', 'issues'), ('GET', 'labels/followup')):
            self.unusable(method, path, '<html>')
            code, r = self.create('x', '--label', 'followup')
            self.assertEqual((code, r['outcome']), (2, 'refused'), path)
        self.fake.garble[('GET', 'user')] = ['{"login": null}']
        code, r = self.create('x')
        self.assertEqual((code, r['outcome']), (2, 'refused'))
        self.fake.add_issue(3, 'covers it')
        for path, bad in (('issues/3', '{"state": "open"}'), ('issues/3/comments', '[[{"body": "no url"}]]')):
            self.unusable('GET', path, bad)
            code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body())
            self.assertEqual((code, r['outcome']), (2, 'refused'), path)
        self.assertEqual(self.fake.posts, [])

    def test_a_listing_that_is_not_a_list_of_pages_of_issues_is_never_read_as_empty(self):
        body = self.body()
        self.create('x', body=body)   # ours: a listing read as empty would post it again
        self.fake.add_comment(1, 'Ours.\n', ME)
        bad = ('{}', '""', '[]', '[{}]', '[""]', '[[{"number": 2, "html_url": "u", "title": "x", "user": ["bad"]}]]')
        for out in bad:
            self.unusable('GET', 'issues', out)
            code, r = self.create('x', body=body)
            self.assertEqual((code, r['outcome']), (2, 'refused'), out)
            self.unusable('GET', 'issues/1/comments', out)
            code, r = self.run_script('comment', '--issue', '1', '--body-file', self.body('Ours.\n'))
            self.assertEqual((code, r['outcome']), (2, 'refused'), out)
        self.assertEqual(len(self.fake.posts), 1, 'nothing posted again')
        self.fake.lose.update({f'repos/{REPO}/issues', f'repos/{REPO}/issues/1/comments'})
        for n, out in enumerate(bad, 1):
            self.unusable('GET', 'issues', None, out)   # the check before, then the relisting after the lost response
            code, r = self.create(f'y {n}')
            self.assertEqual((code, r['outcome']), (1, 'uncertain'), out)
            self.assertNotIn('posted', r)
            self.unusable('GET', 'issues/1/comments', None, out)
            code, r = self.run_script('comment', '--issue', '1', '--body-file', self.body(f'More {n}.\n'))
            self.assertEqual((code, r['outcome']), (1, 'uncertain'), out)
        self.assertEqual(len(self.fake.posts), 1 + 2 * len(bad), 'one POST each time')

    def test_a_read_back_whose_fields_cannot_be_read_is_uncertain(self):
        self.fake.add_issue(3, 'covers it')
        for n, bad in enumerate(({'user': ['bad']}, {'labels': [{}]}, {'labels': None}, {'labels': [{'name': ['x']}]},
                                 {'title': 5}, {'html_url': ['u']}), 1):
            self.fake.tamper = lambda i: {**i, **bad}
            code, r = self.create(f'x {n}')
            self.assertEqual((code, r['outcome'], r['posted']), (1, 'uncertain', True), bad)
            if 'labels' not in bad:   # a comment has no labels
                self.fake.tamper = lambda c: {**c, **bad} if 'issue_url' in c else c   # the comment's read-back only
                code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body(f'C {n}.\n'))
                self.assertEqual((code, r['outcome'], r['posted']), (1, 'uncertain', True), bad)

    def test_an_unusable_comment_response_goes_the_lost_response_way(self):
        self.fake.add_issue(3, 'covers it')
        self.unusable('POST', 'issues/3/comments', '{"html_url": "u"}')
        code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body('One.\n'))
        self.assertEqual((code, r['outcome'], r['posted']), (0, 'verified', True), 'recovered by relisting')
        self.unusable('POST', 'issues/3/comments', '{')
        self.unusable('GET', 'issues/3/comments', None, '[[{"body": "x"}]]')   # the check before, then the relisting
        code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body('Two.\n'))
        self.assertEqual((code, r['outcome']), (1, 'uncertain'))
        self.assertNotIn('posted', r)
        code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body('Three.\n'))
        self.assertEqual((code, r['outcome'], r['posted']), (0, 'verified', True))
        self.unusable('GET', 'issues/comments/' + str(self.fake.next_id + 1), '{')
        code, r = self.run_script('comment', '--issue', '3', '--body-file', self.body('Four.\n'))
        self.assertEqual((code, r['outcome'], r['posted']), (1, 'uncertain', True), 'read-back unusable')
        self.assertEqual(len(self.fake.posts), 4, 'one POST each time')

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
