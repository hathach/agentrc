"""Tests for pr-reply's reply.py against a fake GitHub behind the gh calls."""
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-reply' / 'scripts' / 'reply.py'
spec = importlib.util.spec_from_file_location('pr_reply', SCRIPT)
reply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reply)

REPO, PR, ME = 'o/r', 7, 'hathach'


class FakeGitHub:
    """Review and issue comments on one PR, threads over the review comments,
    and a log of every mutation the script performed."""

    def __init__(self):
        self.review = {}   # id -> comment
        self.issue = {}    # id -> comment
        self.reviews = {}  # id -> review
        self.down = set()  # collection paths answering 404, as gh reports a failed GET
        self.threads = {}  # thread node id -> {ids: [...], resolved: bool}
        self.next_id = 900
        self.mutations = []
        self.calls = []

    def review_comment(self, cid, body='bot says', login='bot', thread=None, parent=None):
        self.review[cid] = {'id': cid, 'body': body, 'user': {'login': login}, 'in_reply_to_id': parent,
                            'pull_request_url': f'https://api.github.com/repos/{REPO}/pulls/{PR}',
                            'html_url': f'https://github.com/{REPO}/pull/{PR}#discussion_r{cid}'}
        t = thread or f'T{cid}'
        self.threads.setdefault(t, {'ids': [], 'resolved': False})['ids'].append(cid)

    def add_review(self, rid, body='bot review body', login='bot'):
        self.reviews[rid] = {'id': rid, 'body': body, 'user': {'login': login},
                             'html_url': f'https://github.com/{REPO}/pull/{PR}#pullrequestreview-{rid}'}

    def issue_comment(self, cid, body='bot summary', login='bot'):
        self.issue[cid] = {'id': cid, 'body': body, 'user': {'login': login},
                           'issue_url': f'https://api.github.com/repos/{REPO}/issues/{PR}',
                           'html_url': f'https://github.com/{REPO}/pull/{PR}#issuecomment-{cid}'}

    def gh(self, args, stdin=None):
        self.calls.append(args)
        if args[:2] == ['api', 'graphql']:
            return self.graphql(args)
        if args[0] != 'api':
            return 1, '', 'unexpected gh call'
        method, path = args[2], args[3]
        body = json.loads(stdin) if stdin else None
        try:
            return 0, json.dumps(self.rest(method, path, body, '--paginate' in args)), ''
        except KeyError:
            return 1, '', f'gh: Not Found (HTTP 404)\n{path}'

    def rest(self, method, path, body, paginate):
        path = path.split('?')[0]
        if path in self.down:
            raise KeyError(path)
        pages = (lambda xs: [list(xs)]) if paginate else (lambda xs: list(xs))
        if path == 'user':
            return {'login': ME}
        m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/comments', path)
        if m and method == 'GET':
            return pages(self.review.values())
        m = re.fullmatch(rf'repos/{REPO}/issues/{PR}/comments', path)
        if m and method == 'GET':
            return pages(self.issue.values())
        if m and method == 'POST':
            self.mutations.append(('post-issue', body['body']))
            cid = self.next_id = self.next_id + 1
            self.issue_comment(cid, body['body'], ME)
            return self.issue[cid]
        m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/reviews', path)
        if m and method == 'GET':
            return pages(self.reviews.values())
        m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/comments/(\d+)/replies', path)
        if m and method == 'POST':
            parent = int(m.group(1))
            if parent not in self.review:
                raise KeyError(parent)
            self.mutations.append(('post-reply', parent, body['body']))
            cid = self.next_id = self.next_id + 1
            thread = next(t for t, v in self.threads.items() if parent in v['ids'])
            # GitHub files a reply to a reply under the thread's first comment.
            self.review_comment(cid, body['body'], ME, thread=thread, parent=self.review[parent]['in_reply_to_id'] or parent)
            return self.review[cid]
        m = re.fullmatch(rf'repos/{REPO}/(pulls|issues)/comments/(\d+)', path)
        pool = m and (self.review if m.group(1) == 'pulls' else self.issue)
        if m and method == 'GET':
            return pool[int(m.group(2))]
        if m and method == 'PATCH':
            self.mutations.append(('edit', int(m.group(2)), body['body']))
            pool[int(m.group(2))]['body'] = body['body']
            return pool[int(m.group(2))]
        m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/reviews/(\d+)', path)
        if m and method == 'GET':
            return self.reviews[int(m.group(1))]
        self.mutations.append(('unexpected', method, path))
        raise KeyError(path)

    def graphql(self, args):
        q = next(a for a in args if a.startswith('query='))
        if q.startswith('query=mutation'):
            tid = next(a for a in args if a.startswith('id=')).split('=', 1)[1]
            self.mutations.append(('resolve', tid))
            self.threads[tid]['resolved'] = True
            return 0, json.dumps({'data': {'resolveReviewThread': {'thread': {'isResolved': True}}}}), ''
        nodes = [{'id': t, 'isResolved': v['resolved'], 'comments': {'nodes': [{'databaseId': i} for i in v['ids']]}}
                 for t, v in self.threads.items()]
        return 0, json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {
            'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': nodes}}}}}), ''


class ReplyCase(unittest.TestCase):
    """The fake GitHub and reply.py's entry point; no tests of its own."""

    def setUp(self):
        self.gh = FakeGitHub()
        patcher = mock.patch.object(reply, 'gh', self.gh.gh)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def json_file(self, obj):
        fd, path = tempfile.mkstemp(suffix='.json', dir=self.tmp)
        with os.fdopen(fd, 'w') as f:
            json.dump(obj, f)
        return path

    def main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = reply.main(['--pr', str(PR), '--repo', REPO, *args])
        lines = out.getvalue().strip().splitlines()
        return rc, json.loads(lines[-1]) if lines else None

    def receipts(self, out):
        """The printed receipts, with the nulls the script leaves out put back as pr-babysit does."""
        if not out:
            return None
        for r in out['receipts']:
            self.assertNotIn(None, (r.get('resolved', False), r.get('error', '')), 'a null resolved or error is printed')
        return [{'resolved': None, 'error': None, **r} for r in out['receipts']]

    def run_script(self, replies, raw=False):
        path = self.json_file({'replies': replies if raw else [{'digest': reply.fnv1a(r['body']), **r} if isinstance(r.get('body'), str) else r
                                                               for r in replies]})
        rc, out = self.main('--manifest', path)
        return rc, self.receipts(out) or []

    def reuse(self, *items):
        rc, out = self.main('--reuse', self.json_file({'reuses': list(items)}))
        return rc, self.receipts(out)

    def edit(self, *items):
        rc, out = self.main('--edit', self.json_file({'edits': [{'digest': reply.fnv1a(i['body']), **i} for i in items]}))
        return rc, self.receipts(out)


class ReplyTest(ReplyCase):
    def test_review_reply_is_posted_read_back_and_resolved(self):
        self.gh.review_comment(10)
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'not so: see line 3'}])
        self.assertEqual(rc, 0)
        self.assertEqual(receipts, [{'commentId': 10, 'kind': 'review', 'replyId': 901, 'digest': reply.fnv1a('not so: see line 3'),
                                     'sent': True, 'posted': True, 'verified': True, 'resolved': True, 'error': None}])
        self.assertEqual(self.gh.mutations, [('post-reply', 10, 'not so: see line 3'), ('resolve', 'T10')])
        self.assertTrue(self.gh.threads['T10']['resolved'])

    def test_identical_existing_reply_is_reused_not_reposted(self):
        self.gh.review_comment(10)
        self.gh.review_comment(55, 'already said', ME, thread='T10', parent=10)
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'already said'}])
        self.assertEqual(rc, 0)
        self.assertEqual(receipts[0]['replyId'], 55)
        self.assertFalse(receipts[0]['posted'])
        self.assertEqual(self.gh.mutations, [('resolve', 'T10')])

    def test_read_back_mismatch_leaves_thread_open(self):
        self.gh.review_comment(10)
        real_post = self.gh.rest

        def mangling(method, path, body, paginate):
            if method == 'POST' and body:
                body = {'body': '@/tmp/body.txt'}  # what a wrong flag would have sent
            return real_post(method, path, body, paginate)
        self.gh.rest = mangling
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'the intended text'}])
        self.assertEqual(rc, 1)
        r = receipts[0]
        self.assertEqual((r['replyId'], r['posted'], r['verified'], r['resolved']), (901, True, False, None))
        self.assertIn('body', r['error'])
        self.assertFalse(self.gh.threads['T10']['resolved'])
        self.assertNotIn(('resolve', 'T10'), self.gh.mutations)

    def test_issue_comment_gets_quote_line_and_no_resolve(self):
        self.gh.issue_comment(20)
        rc, receipts = self.run_script([{'commentId': 20, 'body': 'seven points answered'}])
        self.assertEqual(rc, 0)
        r = receipts[0]
        self.assertEqual((r['kind'], r['verified'], r['resolved']), ('issue', True, None))
        self.assertEqual(self.gh.mutations, [('post-issue', f'> https://github.com/{REPO}/pull/{PR}#issuecomment-20\n\nseven points answered')])

    def test_unknown_comment_posts_nothing(self):
        rc, receipts = self.run_script([{'commentId': 99, 'body': 'x'}])
        self.assertEqual(rc, 1)
        self.assertEqual((receipts[0]['kind'], receipts[0]['sent'], receipts[0]['error']),
                         ('none', False, f'comment 99 is not on PR #{PR}'))
        self.assertEqual(self.gh.mutations, [])

    def test_failed_lookup_is_not_none(self):
        self.gh.down.add(f'repos/{REPO}/pulls/{PR}/reviews')
        rc, receipts = self.run_script([{'commentId': 99, 'body': 'x'}])
        self.assertEqual(rc, 1)
        self.assertEqual((receipts[0]['kind'], receipts[0]['sent']), (None, False))
        self.assertIn('404', receipts[0]['error'])
        self.assertEqual(self.gh.mutations, [])

    def test_review_body_reply_is_an_issue_comment_quoting_the_review(self):
        self.gh.add_review(30, 'Actionable comments posted: 1\n\nOutside the diff: x')
        body = 'Fixed in abc1234.\n\n- a.c:3: x'
        rc, receipts = self.run_script([{'commentId': 30, 'body': body}])
        self.assertEqual(rc, 0)
        r = receipts[0]
        self.assertEqual((r['kind'], r['replyId'], r['posted'], r['verified'], r['resolved']), ('review-body', 901, True, True, None))
        quoted = f'> https://github.com/{REPO}/pull/{PR}#pullrequestreview-30\n\n{body}'
        self.assertEqual(self.gh.mutations, [('post-issue', quoted)])
        rc, receipts = self.run_script([{'commentId': 30, 'body': body}])
        self.assertEqual((rc, receipts[0]['replyId'], receipts[0]['posted']), (0, 901, False), 'the retry reuses the reply')
        self.assertEqual(len(self.gh.mutations), 1)

    def test_quoted_reply_of_the_same_kind_in_other_words_is_left_to_a_human(self):
        self.gh.add_review(30)
        quote = f'> https://github.com/{REPO}/pull/{PR}#pullrequestreview-30\n\n'
        self.gh.issue_comment(60, quote + 'Not applying this: the old wording.', ME)
        rc, receipts = self.run_script([{'commentId': 30, 'body': 'Not applying this: a fresh draft.'}])
        self.assertEqual(rc, 1)
        r = receipts[0]
        self.assertEqual((r['replyId'], r['sent'], r['posted'], r['verified']), (60, False, False, False))
        self.assertIn('reconcile', r['error'])
        self.assertEqual(self.gh.mutations, [])

    def test_identical_quoted_reply_wins_over_a_reworded_one(self):
        self.gh.issue_comment(20)
        quote = f'> https://github.com/{REPO}/pull/{PR}#issuecomment-20\n\n'
        self.gh.issue_comment(60, quote + 'Not so: an older draft.', ME)
        self.gh.issue_comment(61, quote + 'Not so: see line 3.', ME)
        rc, receipts = self.run_script([{'commentId': 20, 'body': 'Not so: see line 3.'}])
        self.assertEqual((rc, receipts[0]['replyId'], receipts[0]['posted'], receipts[0]['verified']), (0, 61, False, True))

    def test_a_second_answer_posts_a_fix_note_beside_a_refutation(self):
        self.gh.add_review(30)
        quote = f'> https://github.com/{REPO}/pull/{PR}#pullrequestreview-30\n\n'
        self.gh.issue_comment(60, quote + 'Fixed in abc1234.\n\n- a.c:3: x', ME)
        rc, receipts = self.run_script([{'commentId': 30, 'body': 'Not applying the second point: y.', 'secondAnswer': True}])
        self.assertEqual((rc, receipts[0]['posted']), (0, True))
        self.gh.issue_comment(20)
        quote = f'> https://github.com/{REPO}/pull/{PR}#issuecomment-20\n\n'
        self.gh.issue_comment(61, quote + 'Not applying the first point: y.', ME)
        rc, receipts = self.run_script([{'commentId': 20, 'body': 'Fixed in def5678.', 'secondAnswer': True}])
        self.assertEqual((rc, receipts[0]['posted']), (0, True))

    def test_without_second_answer_any_reply_of_ours_holds(self):
        # A caller that lost its state: a fix note, or an inline reply in other words, is ours already.
        self.gh.add_review(30)
        self.gh.issue_comment(60, f'> https://github.com/{REPO}/pull/{PR}#pullrequestreview-30\n\nFixed in abc1234.', ME)
        self.gh.review_comment(10)
        self.gh.review_comment(55, 'Fixed in abc1234.', ME, thread='T10', parent=10)
        rc, receipts = self.run_script([{'commentId': 30, 'body': 'Not applying this: y.'}, {'commentId': 10, 'body': 'Not so: see line 3.'}])
        self.assertEqual(rc, 1)
        self.assertEqual([(r['replyId'], r['sent'], r['verified']) for r in receipts], [(60, False, False), (55, False, False)])
        self.assertTrue(all('reconcile' in r['error'] for r in receipts))
        self.assertEqual(self.gh.mutations, [])

    def test_other_quotes_and_second_inline_answers_are_not_reused(self):
        self.gh.add_review(3)
        self.gh.issue_comment(60, f'> https://github.com/{REPO}/pull/{PR}#pullrequestreview-30\n\nNot so.', ME)
        self.gh.issue_comment(61, f'> https://github.com/{REPO}/pull/{PR}#pullrequestreview-3\n\nNot so.', 'someone')
        self.gh.review_comment(10)
        self.gh.review_comment(55, 'said differently', ME, thread='T10', parent=10)
        self.gh.review_comment(56, 'said by someone', 'someone', thread='T10', parent=10)
        rc, receipts = self.run_script([{'commentId': 3, 'body': 'Not so either.'}, {'commentId': 10, 'body': 'said again', 'secondAnswer': True}])
        self.assertEqual(rc, 0)
        self.assertEqual([r['posted'] for r in receipts], [True, True])

    def test_an_id_in_two_spaces_is_refused(self):
        self.gh.review_comment(40)
        self.gh.add_review(40)
        rc, receipts = self.run_script([{'commentId': 40, 'body': 'x'}])
        self.assertEqual(rc, 1)
        self.assertEqual(receipts[0]['kind'], None)
        self.assertIn('ambiguous', receipts[0]['error'])
        self.assertEqual(self.gh.mutations, [])

    def test_one_failure_does_not_stop_the_others(self):
        self.gh.review_comment(10)
        rc, receipts = self.run_script([{'commentId': 99, 'body': 'x'}, {'commentId': 10, 'body': 'ok'}])
        self.assertEqual(rc, 1)
        self.assertEqual([r['verified'] for r in receipts], [False, True])

    def test_already_resolved_thread_is_left_alone(self):
        self.gh.review_comment(10)
        self.gh.threads['T10']['resolved'] = True
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'ok'}])
        self.assertEqual(rc, 0)
        self.assertTrue(receipts[0]['resolved'])
        self.assertEqual([m[0] for m in self.gh.mutations], ['post-reply'])

    def test_bad_manifest_is_exit_2_without_api_calls(self):
        for bad in ([], [{'commentId': '10', 'body': 'x'}], [{'commentId': 10, 'body': ' '}],
                    [{'commentId': 10, 'body': 'a'}, {'commentId': 10, 'body': 'b'}],
                    [{'commentId': 10, 'body': 'a', 'digest': None}], [{'commentId': 10, 'body': 'x', 'secondAnswer': 1}]):
            with self.subTest(bad=bad):
                rc, receipts = self.run_script(bad)
                self.assertEqual(rc, 2)
                self.assertEqual(self.gh.calls, [])

    def test_digest_mismatch_posts_nothing(self):
        self.gh.review_comment(10)
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'transcribed wrong', 'digest': reply.fnv1a('the intended text')}])
        self.assertEqual(rc, 1)
        self.assertEqual(receipts[0]['error'], 'manifest body does not match its digest')
        self.assertEqual(self.gh.mutations, [])
        self.assertEqual([a for a in self.gh.calls if a[0] == 'api' and a[2] != 'GET'], [])

    def test_matching_digest_is_echoed_in_the_receipt(self):
        self.gh.review_comment(10)
        d = reply.fnv1a('right')
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'right', 'digest': d}])
        self.assertEqual((rc, receipts[0]['digest'], receipts[0]['verified']), (0, d, True))

    def test_unavailable_read_back_is_null_not_a_mismatch(self):
        self.gh.review_comment(10)
        real = self.gh.rest

        def flaky(method, path, body, paginate):
            if method == 'GET' and re.fullmatch(rf'repos/{REPO}/pulls/comments/\d+', path):
                raise KeyError('HTTP 502')
            return real(method, path, body, paginate)
        self.gh.rest = flaky
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'ok'}])
        self.assertEqual(rc, 1)
        r = receipts[0]
        self.assertEqual((r['replyId'], r['verified'], r['resolved']), (901, None, None))
        self.assertIn('read-back unavailable', r['error'])
        self.assertFalse(self.gh.threads['T10']['resolved'])
        # the retry with the same body reuses the reply and only resolves
        self.gh.rest = real
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'ok'}])
        self.assertEqual((rc, receipts[0]['replyId'], receipts[0]['posted'], receipts[0]['resolved']), (0, 901, False, True))
        self.assertEqual([m[0] for m in self.gh.mutations], ['post-reply', 'resolve'])

    def test_graphql_strings_go_through_f_and_the_number_through_F(self):
        self.gh.review_comment(10)
        self.run_script([{'commentId': 10, 'body': 'ok'}])
        q = next(a for a in self.gh.calls if a[:2] == ['api', 'graphql'] and any(x.startswith('o=') for x in a))
        self.assertIn('-f', q[q.index('o=o') - 1])
        self.assertIn('-f', q[q.index('r=r') - 1])
        self.assertEqual(q[q.index(f'p={PR}') - 1], '-F')

    def test_documented_manifest_works_as_written(self):
        # SKILL.md's example, digest computed the way it says, no test-side augmentation
        self.gh.review_comment(10)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(reply.main(['--digest', 'hello']), 0)
        digest = out.getvalue().strip()
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'hello', 'digest': digest}], raw=True)
        self.assertEqual((rc, receipts[0]['verified']), (0, True))
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'hello'}], raw=True)
        self.assertEqual(rc, 2, 'an entry without a digest is refused')

    def test_lost_response_reports_sent_without_a_reply_id(self):
        self.gh.review_comment(10)
        real = self.gh.rest

        def lost(method, path, body, paginate):
            r = real(method, path, body, paginate)
            if method == 'POST':
                raise KeyError('connection reset')  # GitHub stored it, the answer never came
            return r
        self.gh.rest = lost
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'ok'}])
        r = receipts[0]
        self.assertEqual((rc, r['sent'], r['posted'], r['replyId'], r['verified']), (1, True, False, None, False))
        self.assertEqual(len(self.gh.review), 2, 'the reply exists on GitHub')
        # a failure before any POST says so
        rc, receipts = self.run_script([{'commentId': 99, 'body': 'x'}])
        self.assertEqual((receipts[0]['sent'], receipts[0]['replyId']), (False, None))

    def test_never_edits_or_deletes(self):
        self.gh.review_comment(10)
        self.gh.review_comment(55, 'wrong text', ME, thread='T10', parent=10)
        self.run_script([{'commentId': 10, 'body': 'right text'}])
        methods = {a[2] for a in self.gh.calls if a[0] == 'api' and len(a) > 3 and a[1] == '-X'}
        self.assertLessEqual(methods, {'GET', 'POST'})


class FollowUpTest(ReplyCase):
    """A bot's follow-up in a thread: every reply, ours included, is filed under the thread's first comment (#18)."""

    def thread(self, *ours):
        """#3988's shape: root 10, our early reply 15 to it, the follow-up 20, then any (id, body) replies of ours."""
        self.gh.review_comment(10, 'root finding')
        self.gh.review_comment(15, 'answer to the root', ME, thread='T10', parent=10)
        self.gh.review_comment(20, 'follow-up', 'bot', thread='T10', parent=10)
        for cid, body in ours:
            self.gh.review_comment(cid, body, ME, thread='T10', parent=10)

    def test_a_reply_to_a_follow_up_is_filed_under_the_root_and_verifies(self):
        self.thread()
        rc, receipts = self.run_script([{'commentId': 20, 'body': 'not so: line 3'}])
        self.assertEqual(rc, 0, receipts)
        self.assertEqual((receipts[0]['verified'], receipts[0]['resolved']), (True, True))
        self.assertEqual(self.gh.mutations, [('post-reply', 10, 'not so: line 3'), ('resolve', 'T10')],
                         'posted to the root, not over our earlier reply to it')

    def test_only_a_reply_of_ours_after_the_target_counts_as_its_answer(self):
        self.thread((25, 'not so: line 3'))
        rc, receipts = self.run_script([{'commentId': 20, 'body': 'not so: line 3'}])
        self.assertEqual((rc, receipts[0]['replyId'], receipts[0]['posted']), (0, 25, False))
        rc, receipts = self.run_script([{'commentId': 20, 'body': 'said otherwise'}])
        self.assertEqual((receipts[0]['replyId'], receipts[0]['sent']), (25, False))
        rc, receipts = self.run_script([{'commentId': 10, 'body': 'root again'}])
        self.assertEqual((receipts[0]['replyId'], receipts[0]['sent']), (15, False), 'for the root, every later reply of ours counts')

    def test_inspect_reuse_and_edit_pair_a_reply_with_the_follow_up_it_came_after(self):
        self.thread((25, 'deliberately'))
        rc, out = self.main('--inspect', '20:25', '20:15')
        self.assertEqual(rc, 1)
        good, early = out['inspected']
        self.assertEqual((good['error'], good['body']), (None, 'deliberately'))
        self.assertIn('mismatch on thread', early['error'], 'our reply from before the follow-up cannot answer it')
        pair = {'commentId': 20, 'replyId': 25, 'bodyDigest': reply.fnv1a('deliberately'), 'originalDigest': reply.comment_digest.sha12('follow-up')}
        rc, receipts = self.edit({**pair, 'body': 'precisely'})
        self.assertEqual((rc, receipts[0]['verified'], receipts[0]['resolved']), (0, True, True))
        mutations = len(self.gh.mutations)
        rc, out = self.main('--reuse', self.json_file({'reuses': [{**pair, 'bodyDigest': reply.fnv1a('precisely')}]}))
        self.assertEqual((rc, out['receipts'][0]['verified']), (0, True))
        self.assertEqual([m[0] for m in self.gh.mutations[mutations:]], [], 'the thread was already resolved: nothing written')

    def test_a_reply_in_another_thread_fails_on_parent(self):
        self.thread()
        self.gh.review_comment(30, 'elsewhere')
        self.gh.review_comment(35, 'not so', ME, thread='T30', parent=30)
        rc, out = self.main('--inspect', '20:35')
        self.assertIn('mismatch on thread', out['inspected'][0]['error'])


class ReceiptFileTest(ReplyCase):
    """--receipt: a relay retry gets the first run's receipt back instead of a rerun's (#53)."""

    def run_saved(self, path, body='not so'):
        manifest = self.json_file({'replies': [{'commentId': 10, 'body': body, 'digest': reply.fnv1a(body)}]})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = reply.main(['--pr', str(PR), '--repo', REPO, '--manifest', manifest, '--receipt', path])
        return rc, out.getvalue(), err.getvalue()

    def test_a_saved_receipt_is_replayed_without_calling_github(self):
        self.gh.review_comment(10)
        path = os.path.join(self.tmp, 'replies#1.json')
        rc, first, _ = self.run_saved(path)
        r = json.loads(first)['receipts'][0]
        self.assertEqual((rc, r['sent'], r['posted']), (0, True, True))
        with mock.patch.object(reply, 'gh', side_effect=AssertionError('a replay calls GitHub')):
            self.assertEqual(self.run_saved(path)[:2], (0, first))
        self.assertEqual(self.gh.mutations, [('post-reply', 10, 'not so'), ('resolve', 'T10')])

    def test_a_saved_unverified_run_replays_its_exit_code_and_a_new_path_reads_again(self):
        self.gh.review_comment(10)
        real = self.gh.rest

        def flaky(method, path, body, paginate):
            if method == 'GET' and re.fullmatch(rf'repos/{REPO}/pulls/comments/\d+', path):
                raise KeyError('HTTP 502')
            return real(method, path, body, paginate)
        self.gh.rest = flaky
        path = os.path.join(self.tmp, 'replies#1.json')
        rc, first, _ = self.run_saved(path)
        self.assertEqual((rc, json.loads(first)['receipts'][0]['verified']), (1, None))
        self.gh.rest = real
        self.assertEqual(self.run_saved(path)[:2], (1, first))
        rc, again, _ = self.run_saved(os.path.join(self.tmp, 'replies#2.json'))
        r = json.loads(again)['receipts'][0]
        self.assertEqual((rc, r['replyId'], r['sent'], r['posted'], r['verified'], r['resolved']), (0, 901, False, False, True, True))

    def test_a_receipt_that_is_not_this_requests_sealed_line_is_refused_and_nothing_posts(self):
        self.gh.review_comment(10)
        path = os.path.join(self.tmp, 'replies#1.json')
        self.run_saved(path)
        saved = json.loads(Path(path).read_text())
        tampered = {**saved, 'line': {**saved['line'], 'receipts': [{**saved['line']['receipts'][0], 'sent': False}]}}
        for content, why in [('{"line"', 'unreadable'), (json.dumps({**saved, 'code': 3}), 'malformed'),
                             (json.dumps(saved), 'another request'), (json.dumps(tampered), 'seal')]:
            Path(path).write_text(content)
            rc, out, err = self.run_saved(path, body='other words' if why == 'another request' else 'not so')
            self.assertEqual((rc, out), (2, ''), why)
            self.assertIn(why, err)
        self.assertEqual(len(self.gh.mutations), 2, 'only the first run posted')

    def test_a_receipt_that_cannot_be_saved_is_exit_2_with_the_line(self):
        self.gh.review_comment(10)
        path = os.path.join(self.tmp, 'replies#1.json')
        with mock.patch.object(reply.os, 'replace', side_effect=OSError('disk full')):
            rc, out, err = self.run_saved(path)
        self.assertEqual((rc, json.loads(out)['receipts'][0]['posted'], os.path.exists(path)), (2, True, False))
        self.assertIn('receipt not saved: disk full', err)

    def test_inspect_takes_no_receipt(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            reply.main(['--pr', str(PR), '--repo', REPO, '--inspect', '10', '--receipt', os.path.join(self.tmp, 'x')])


class SealTest(ReplyCase):
    """The seal pr-babysit checks a relayed copy against, as its facts.py computes it."""

    def test_receipts_and_inspected_lines_carry_facts_seal(self):
        facts_spec = importlib.util.spec_from_file_location('facts', SCRIPT.parents[2] / 'pr-babysit' / 'scripts' / 'facts.py')
        facts = importlib.util.module_from_spec(facts_spec)
        facts_spec.loader.exec_module(facts)
        self.gh.review_comment(10)
        path = self.json_file({'replies': [{'commentId': 10, 'body': 'naïve ✓ "q"', 'digest': reply.fnv1a('naïve ✓ "q"')},
                                           {'commentId': 99, 'body': 'x', 'digest': reply.fnv1a('x')}]})
        for args in (['--manifest', path], ['--inspect', '10:901']):
            _, out = self.main(*args)
            line = {k: v for k, v in out.items() if k != 'seal'}
            self.assertEqual(out['seal'], facts.sealed(line)['seal'], args)
        self.assertEqual(reply.sealed({'receipts': [], 'error': 'x'})['seal'], facts.sealed({'receipts': [], 'error': 'x'})['seal'])


class ReconcileTest(ReplyCase):
    """--inspect and --reuse: settle on a reply of ours already there, never posting."""

    def reworded(self):
        """Comment 20 and our quoting answer 901 in other words than any manifest now holds."""
        self.gh.issue_comment(20, 'three points')
        body = f'> https://github.com/{REPO}/pull/{PR}#issuecomment-20\n\nall three answered'
        self.gh.issue_comment(901, body, ME)
        return body

    def test_inspect_reads_our_reply_and_both_digests(self):
        body = self.reworded()
        rc, out = self.main('--inspect', '20:901')
        self.assertEqual(rc, 0)
        self.assertEqual(out['inspected'], [{'commentId': 20, 'replyId': 901, 'kind': 'issue', 'body': body,
                                             'bodyDigest': reply.fnv1a(body), 'original': 'three points',
                                             'originalDigest': reply.comment_digest.sha12('three points'),
                                             'error': None}])
        self.assertEqual(reply.comment_digest.sha12('three points'),
                         __import__('hashlib').sha256(b'three points').hexdigest()[:12], 'the validator\'s digest')
        self.assertEqual(self.gh.mutations, [])

    def test_a_bare_comment_inspects_our_newest_reply_to_it(self):
        body = self.reworded()
        self.gh.issue_comment(905, 'someone else quoting', 'bot')
        later = f'> https://github.com/{REPO}/pull/{PR}#issuecomment-20\n\nanswered by hand'
        self.gh.issue_comment(906, later, ME)
        self.gh.review_comment(10, 'one point')
        self.gh.review_comment(55, 'first answer', ME, thread='T10', parent=10)
        self.gh.review_comment(57, 'newer answer', ME, thread='T10', parent=10)
        self.gh.review_comment(12)
        rc, out = self.main('--inspect', '20', '10', '12')
        self.assertEqual(rc, 1)
        self.assertEqual([(i['replyId'], i['body'], i['error']) for i in out['inspected']],
                         [(906, later, None), (57, 'newer answer', None), (None, None, 'no reply of ours on comment 12')])
        self.assertNotEqual(body, later)
        self.assertEqual(self.gh.mutations, [])

    def test_inspect_refuses_a_reply_that_is_not_ours_on_that_comment(self):
        self.reworded()
        self.gh.issue_comment(902, 'someone else', 'bot')
        self.gh.issue_comment(903, f'> https://github.com/{REPO}/pull/{PR}#issuecomment-77\n\nelsewhere', ME)
        self.gh.review_comment(10)
        self.gh.review_comment(55, 'ours, other thread', ME, parent=11)
        rc, out = self.main('--inspect', '20:902', '20:903', '10:55', '99:901')
        self.assertEqual(rc, 1)
        errors = [i['error'] for i in out['inspected']]
        self.assertIn('mismatch on author', errors[0])
        self.assertIn('mismatch on quote', errors[1])
        self.assertIn('mismatch on thread', errors[2])
        self.assertEqual(errors[3], f'comment 99 is not on PR #{PR}')
        self.assertTrue(all(i['body'] is None for i in out['inspected']))

    def test_reuse_settles_without_posting_and_resolves_an_inline_thread(self):
        self.gh.review_comment(10, 'two points')
        self.gh.review_comment(55, 'both answered', ME, thread='T10', parent=10)
        rc, receipts = self.reuse({'commentId': 10, 'replyId': 55, 'bodyDigest': reply.fnv1a('both answered'),
                                   'originalDigest': reply.comment_digest.sha12('two points')})
        self.assertEqual(rc, 0)
        self.assertEqual(receipts, [{'commentId': 10, 'kind': 'review', 'replyId': 55, 'digest': reply.fnv1a('both answered'),
                                     'sent': False, 'posted': False, 'verified': True, 'resolved': True, 'error': None}])
        self.assertEqual(self.gh.mutations, [('resolve', 'T10')])
        body = self.reworded()
        rc, receipts = self.reuse({'commentId': 20, 'replyId': 901, 'bodyDigest': reply.fnv1a(body),
                                   'originalDigest': reply.comment_digest.sha12('three points')})
        self.assertEqual((rc, receipts[0]['kind'], receipts[0]['verified'], receipts[0]['resolved']), (0, 'issue', True, None))
        self.assertEqual(self.gh.mutations, [('resolve', 'T10')], 'nothing posted, nothing more resolved')

    def test_reuse_refuses_anything_changed_since_the_inspection(self):
        body = self.reworded()
        good = {'commentId': 20, 'replyId': 901, 'bodyDigest': reply.fnv1a(body), 'originalDigest': reply.comment_digest.sha12('three points')}
        for change, expected in [
            (lambda: self.gh.issue[20].update(body='four points'), 'comment 20 was edited since the inspection'),
            (lambda: self.gh.issue[901].update(body=body + ' and more'), 'reply 901 was edited since the inspection'),
            (lambda: self.gh.issue[901]['user'].update(login='bot'), 'mismatch on author'),
        ]:
            self.setUp()
            body = self.reworded()
            change()
            rc, receipts = self.reuse(good)
            self.assertEqual((rc, receipts[0]['verified']), (1, False))
            self.assertIn(expected, receipts[0]['error'])
            self.assertEqual(self.gh.mutations, [])
        self.setUp()
        self.reworded()
        del self.gh.issue[901]
        rc, receipts = self.reuse(good)
        self.assertEqual((rc, receipts[0]['verified']), (1, None), 'an unreadable reply is unknown, not a mismatch')
        self.assertEqual(self.gh.mutations, [])

    def test_each_reuse_reads_its_comment_afresh(self):
        self.gh.review_comment(10, 'two points')
        self.gh.review_comment(55, 'both answered', ME, thread='T10', parent=10)
        body = self.reworded()
        real = self.gh.graphql

        def resolve_then_edit(args):
            out = real(args)
            if any(a.startswith('query=mutation') for a in args):
                self.gh.issue[20]['body'] = 'three points and a fourth'  # edited while comment 10 settled
            return out
        self.gh.graphql = resolve_then_edit
        rc, receipts = self.reuse(
            {'commentId': 10, 'replyId': 55, 'bodyDigest': reply.fnv1a('both answered'), 'originalDigest': reply.comment_digest.sha12('two points')},
            {'commentId': 20, 'replyId': 901, 'bodyDigest': reply.fnv1a(body), 'originalDigest': reply.comment_digest.sha12('three points')})
        self.assertEqual(rc, 1)
        self.assertEqual([r['verified'] for r in receipts], [True, False])
        self.assertEqual(receipts[1]['error'], 'comment 20 was edited since the inspection')

    def test_reuse_reads_a_review_body_afresh_too(self):
        self.gh.add_review(30, 'outside the diff: x')
        quoted = f'> https://github.com/{REPO}/pull/{PR}#pullrequestreview-30\n\nnot so'
        self.gh.issue_comment(901, quoted, ME)
        rc, receipts = self.reuse({'commentId': 30, 'replyId': 901, 'bodyDigest': reply.fnv1a(quoted),
                                   'originalDigest': reply.comment_digest.sha12('outside the diff: x')})
        self.assertEqual((rc, receipts[0]['kind'], receipts[0]['verified']), (0, 'review-body', True))

    def test_a_failed_resolve_keeps_the_reuse_verified_and_the_thread_open(self):
        self.gh.review_comment(10, 'two points')
        self.gh.review_comment(55, 'both answered', ME, thread='T10', parent=10)
        self.gh.graphql = lambda args: (1, '', 'HTTP 502')
        rc, receipts = self.reuse({'commentId': 10, 'replyId': 55, 'bodyDigest': reply.fnv1a('both answered'),
                                   'originalDigest': reply.comment_digest.sha12('two points')})
        self.assertEqual((rc, receipts[0]['verified'], receipts[0]['resolved']), (1, True, None))
        self.assertIn('502', receipts[0]['error'])

    def test_edit_puts_the_intended_body_on_our_reply_and_resolves(self):
        self.gh.review_comment(10, 'two points')
        self.gh.review_comment(55, 'deliberately', ME, thread='T10', parent=10)
        rc, receipts = self.edit({'commentId': 10, 'replyId': 55, 'body': 'precisely', 'bodyDigest': reply.fnv1a('deliberately'),
                                  'originalDigest': reply.comment_digest.sha12('two points')})
        self.assertEqual(rc, 0)
        self.assertEqual(receipts, [{'commentId': 10, 'kind': 'review', 'replyId': 55, 'digest': reply.fnv1a('precisely'),
                                     'sent': True, 'posted': True, 'verified': True, 'resolved': True, 'error': None}])
        self.assertEqual(self.gh.mutations, [('edit', 55, 'precisely'), ('resolve', 'T10')])
        body = self.reworded()
        rc, receipts = self.edit({'commentId': 20, 'replyId': 901, 'body': 'the draft', 'bodyDigest': reply.fnv1a(body),
                                  'originalDigest': reply.comment_digest.sha12('three points')})
        self.assertEqual((rc, receipts[0]['verified'], receipts[0]['resolved']), (0, True, None))
        self.assertEqual(self.gh.mutations[-1], ('edit', 901, f'> https://github.com/{REPO}/pull/{PR}#issuecomment-20\n\nthe draft'))

    def test_edit_refuses_anything_changed_since_the_inspection(self):
        body = self.reworded()
        good = {'commentId': 20, 'replyId': 901, 'body': 'the draft', 'bodyDigest': reply.fnv1a(body), 'originalDigest': reply.comment_digest.sha12('three points')}
        for change, expected in [
            (lambda: self.gh.issue[20].update(body='four points'), 'comment 20 was edited since the inspection'),
            (lambda: self.gh.issue[901].update(body=body + ' and more'), 'reply 901 was edited since the inspection'),
            (lambda: self.gh.issue[901]['user'].update(login='bot'), 'mismatch on author'),
        ]:
            self.setUp()
            self.reworded()
            change()
            rc, receipts = self.edit(good)
            self.assertEqual((rc, receipts[0]['sent'], receipts[0]['verified']), (1, False, False))
            self.assertIn(expected, receipts[0]['error'])
            self.assertEqual(self.gh.mutations, [])
        rc, out = self.main('--edit', self.json_file({'edits': [{**good, 'digest': reply.fnv1a('other')}]}))
        self.assertEqual((rc, out['receipts'][0]['error']), (1, 'edit body does not match its digest'))
        self.assertEqual(self.gh.mutations, [])

    def test_a_rerun_after_a_lost_receipt_finds_its_edit_and_makes_no_other(self):
        self.gh.review_comment(10, 'two points')
        self.gh.review_comment(55, 'deliberately', ME, thread='T10', parent=10)
        body = self.reworded()
        items = [{'commentId': 10, 'replyId': 55, 'body': 'precisely', 'bodyDigest': reply.fnv1a('deliberately'),
                  'originalDigest': reply.comment_digest.sha12('two points')},
                 {'commentId': 20, 'replyId': 901, 'body': 'the draft', 'bodyDigest': reply.fnv1a(body),
                  'originalDigest': reply.comment_digest.sha12('three points')}]
        self.edit(*items)
        edits = [m for m in self.gh.mutations if m[0] == 'edit']
        self.gh.threads['T10']['resolved'] = False
        rc, receipts = self.edit(*items)
        self.assertEqual(rc, 0)
        self.assertEqual([(r['sent'], r['posted'], r['verified']) for r in receipts], [(False, False, True)] * 2)
        self.assertEqual([m for m in self.gh.mutations if m[0] == 'edit'], edits, 'no second edit')
        self.assertTrue(self.gh.threads['T10']['resolved'])

    def test_edit_that_does_not_read_back_is_not_verified(self):
        self.gh.review_comment(10, 'two points')
        self.gh.review_comment(55, 'deliberately', ME, thread='T10', parent=10)
        real = self.gh.rest
        self.gh.rest = lambda method, path, body, paginate: real(method, path, {'body': body['body'] + ' '} if method == 'PATCH' else body, paginate)
        rc, receipts = self.edit({'commentId': 10, 'replyId': 55, 'body': 'precisely', 'bodyDigest': reply.fnv1a('deliberately'),
                                  'originalDigest': reply.comment_digest.sha12('two points')})
        self.assertEqual((rc, receipts[0]['posted'], receipts[0]['verified'], receipts[0]['resolved']), (1, True, False, None))
        self.assertFalse(self.gh.threads['T10']['resolved'])

    def test_bad_pairs_and_reuse_files_are_usage_errors(self):
        with self.assertRaises(SystemExit) as e:
            self.main('--inspect', '20-901')
        self.assertEqual(e.exception.code, 2)
        path = self.json_file({'reuses': [{'commentId': 20, 'replyId': 901, 'bodyDigest': 'x'}]})
        self.assertEqual(self.main('--reuse', path)[0], 2)
        dup = {'commentId': 20, 'replyId': 901, 'bodyDigest': 'x', 'originalDigest': 'y'}
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self.main('--reuse', self.json_file({'reuses': [dup, dup]}))[0], 2)
        self.assertIn('commentId 20 listed twice', err.getvalue())
        with self.assertRaises(SystemExit):
            self.main('--reuse', path, '--inspect', '20:901')




DATA = Path(__file__).resolve().parent / 'data' / 'comment_digest'
RABBIT = reply.comment_digest.CODERABBIT
FOOTER = reply.comment_digest.FOOTER


class CommentDigestTest(unittest.TestCase):
    """What CodeRabbit rewrites at the end of its own comment leaves the digest alone; nothing else does."""

    cd = reply.comment_digest

    def digest(self, body, author):
        return self.cd.digests(body, author)[0]

    def aliases(self, body, author):
        return self.cd.digests(body, author)[1:]

    def live(self, cid):
        return json.loads((DATA / f'{cid}.json').read_text())

    def test_live_comments_keep_the_digest_recorded_before_their_rewrite(self):
        # tinyusb#4055 and #3862: pr-babysit recorded these before CodeRabbit added a status, or flipped its footer to reply.
        for cid, before, now in (('4154162329', 'e2bfa191d40d', 'b8a6c31d627f'), ('4135986177', '679c9f9aaf47', 'd755e5bbc27a')):
            c = self.live(cid)
            self.assertEqual(self.cd.sha12(c['body']), now)
            self.assertEqual(self.digest(c['body'], c['user']), before, cid)
            self.assertIn(now, self.aliases(c['body'], c['user']), cid)

    def test_every_supported_tail_digests_as_the_first_form(self):
        head = 'claim\n\n<!-- cr-comment:v1:abc -->\n\n'
        first = head + FOOTER.format('comment')
        statuses = ['', '\n\n✅ Addressed in commit a3f8bc8', '\n\n✅ Addressed in commits a3f8bc8 to 0123abcd',
                    '\n\n✅ Confirmed as addressed by @hathach']
        for word in ('comment', 'reply'):
            for status in statuses:
                for again in ('', f'\n\n{FOOTER.format("comment")}') if status else ('',):
                    body = head + FOOTER.format(word) + status + again
                    self.assertEqual(self.digest(body, RABBIT), self.cd.sha12(first), repr(body))
                    also = self.aliases(body, RABBIT)
                    self.assertNotIn(self.cd.sha12(first), also)
                    if body != first:
                        self.assertIn(self.cd.sha12(body), also, repr(body))
        self.assertEqual(self.aliases(first, RABBIT), [self.cd.sha12(head + FOOTER.format('reply'))])

    def test_anything_else_counts_as_it_is(self):
        footer = FOOTER.format('comment')
        for body, author in [
            ('claim\n\n' + footer + '\n', RABBIT),                                  # a byte after the structure
            ('claim\n\n' + footer + '\n\n✅ Addressed in commit xyz1234', RABBIT),   # not a SHA
            ('claim\n\n' + footer + '\n\n✅ Addressed soon', RABBIT),                # no known status
            ('```\n' + FOOTER.format('reply') + '\n```', RABBIT),                    # fenced
            ('claim\n\n' + FOOTER.format('reply'), 'greptile-apps[bot]'),           # another author
            ('<!-- greptile_summary -->\n\nsummary', 'greptile-apps[bot]'),
            (None, RABBIT),
        ]:
            self.assertEqual(self.digest(body, author), self.cd.sha12(body or ''), repr(body))
            self.assertEqual(self.aliases(body, author), [], repr(body))
        self.assertNotEqual(self.digest('claim\n\n' + footer, RABBIT), self.digest('claim edited\n\n' + footer, RABBIT))


class CanonicalReuseTest(ReplyCase):
    """--reuse reads a CodeRabbit comment under its canonical digest; one recorded before that rule needs a new inspection."""

    def test_reuse_settles_on_the_canonical_digest_only(self):
        first = 'two points\n\n' + FOOTER.format('comment')
        now = 'two points\n\n' + FOOTER.format('reply') + '\n\n✅ Addressed in commit a3f8bc8'
        sha12 = reply.comment_digest.sha12
        for recorded, verified in ((sha12(first), True), (sha12(now), False)):
            self.setUp()
            self.gh.review_comment(10, now, RABBIT)
            self.gh.review_comment(55, 'both answered', ME, thread='T10', parent=10)
            rc, receipts = self.reuse({'commentId': 10, 'replyId': 55, 'bodyDigest': reply.fnv1a('both answered'), 'originalDigest': recorded})
            self.assertEqual(receipts[0]['verified'], verified, recorded)
            self.assertEqual(self.gh.mutations, [('resolve', 'T10')] if verified else [])
        rc, out = self.main('--inspect', '10:55')
        self.assertEqual(out['inspected'][0]['originalDigest'], sha12(first))


if __name__ == '__main__':
    unittest.main()
