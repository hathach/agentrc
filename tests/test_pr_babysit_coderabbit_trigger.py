import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'skills/pr-babysit/scripts'))
spec = importlib.util.spec_from_file_location('coderabbit_trigger', ROOT / 'skills/pr-babysit/scripts/coderabbit_trigger.py')
trigger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trigger)
import harvest  # noqa: E402  the script's GET helpers, which call facts.run through harvest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

HEAD = 'a' * 40
BOX = '> - [ ] <!-- {"checkboxId":"e9bb8d72-00e8-4f67-9cb2-caf3b22574fe"} --> 🔍 Trigger review'
BOT = 'coderabbitai[bot]'
SKIP = {'context': 'CodeRabbit', 'state': 'success', 'updated_at': '2026-10-12T11:41:10Z',
        'description': 'Review skipped: manual review required for this OSS repository'}
# #324: hathach/agentrc#323's head, which CodeRabbit skipped through a check run and no status.
SKIP_RUN = {'id': 12, 'name': 'CodeRabbit', 'status': 'completed', 'conclusion': 'skipped', 'started_at': '2026-10-12T11:41:09Z',
            'completed_at': '2026-10-12T11:41:10Z', 'output': {'title': 'Review skipped', 'summary': 'Result: Skipped - Review skipped'}}
NO_AUTO = 'This repository does not receive automatic reviews because it has fewer than 10 stars.'


def setUpModule():
    guard_survivors(__name__)


class Rig:
    """A fake `gh api`: the PR, the head's statuses and check runs and the PR's comments as GET pages (run), recording each
    PATCH (attempt)."""

    def __init__(self, statuses=(SKIP,), comments=None, head=HEAD, readback=None, checks=()):
        self.head, self.statuses, self.checks, self.patches, self.readback = head, list(statuses), list(checks), [], readback
        self.comments = comments if comments is not None else [{'id': 5, 'user': {'login': BOT}, 'body': f'summary\n{BOX}\nend'}]

    def attempt(self, *argv, input=None):
        assert argv[2:4] == ('-X', 'PATCH') and argv[-2:] == ('--input', '-'), argv
        body = json.loads(input)['body']
        self.patches.append(body)
        return 0, json.dumps({'body': self.readback if self.readback is not None else body}), ''

    def run(self, *argv, ok=(0,)):
        path = argv[-1]
        if path.endswith('/pulls/7'):
            return 0, json.dumps({'head': {'sha': self.head}})
        if '/statuses?' in path:
            return 0, json.dumps([self.statuses])
        if '/check-runs?' in path:
            return 0, json.dumps([{'check_runs': self.checks}])
        if '/comments?' in path:
            return 0, json.dumps([self.comments])
        raise trigger.Unusable(f'unexpected {path}')


class Trigger(unittest.TestCase):
    def run_with(self, rig, argv=('--pr', '7', '--head', HEAD, '--repo', 'o/r')):
        out = io.StringIO()
        with mock.patch.object(trigger, 'attempt', rig.attempt), mock.patch.object(harvest, 'run', rig.run), redirect_stdout(out):
            code = trigger.report(trigger.collect, list(argv), seal=True)
        return code, json.loads(out.getvalue())

    def test_an_unticked_box_on_a_skipped_head_is_ticked_and_read_back(self):
        rig = Rig()
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome'], out['detail'], out['commentId']), (0, 'ticked', 'comment 5', 5))
        self.assertRegex(out['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(rig.patches, [f'summary\n{BOX.replace("[ ]", "[x]", 1)}\nend'])

    def test_a_ticked_box_is_left_alone(self):
        rig = Rig(comments=[{'id': 5, 'user': {'login': BOT}, 'body': BOX.replace('[ ]', '[x]', 1)}])
        self.assertEqual(self.run_with(rig)[1], {'outcome': 'already', 'detail': 'comment 5', 'commentId': 5, 'at': mock.ANY, 'seal': mock.ANY})
        self.assertEqual(rig.patches, [])

    def test_nothing_is_edited_unless_coderabbit_skipped_this_head(self):
        cases = {'not the head': Rig(head='b' * 40), 'no status': Rig(statuses=()),
                 'reviewed': Rig(statuses=(dict(SKIP, description='Review completed'),)),
                 'newer status wins': Rig(statuses=(SKIP, dict(SKIP, state='pending', description='Review in progress',
                                                              updated_at='2026-10-12T11:42:00Z'))),
                 'a tie that reads differently': Rig(statuses=(SKIP, dict(SKIP, id=2, state='pending', description='Review in progress'))),
                 'undated': Rig(statuses=(dict(SKIP, updated_at=None),))}
        for what, rig in cases.items():
            with self.subTest(what):
                out = self.run_with(rig)[1]
                self.assertEqual(out['outcome'], 'notSkipped')
                self.assertNotIn('commentId', out)
                self.assertEqual(rig.patches, [])

    def test_a_check_run_skip_is_ticked_only_when_the_summary_says_manual_review(self):
        rig = Rig(statuses=(), checks=(SKIP_RUN,))
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'ticked', 'its unticked box says it')
        self.assertEqual(len(rig.patches), 1)
        words = Rig(statuses=(), checks=(SKIP_RUN,), comments=[{'id': 5, 'user': {'login': BOT}, 'body': f'summary\n> {NO_AUTO}'}])
        code, out = self.run_with(words)
        self.assertEqual((out['outcome'], out['detail']), ('failed', '0 CodeRabbit comments hold one Trigger review box, not 1'),
                         'its words say it, and no box is left to tick')
        cases = {'another skip': Rig(statuses=(), checks=(SKIP_RUN,), comments=[{'id': 5, 'user': {'login': BOT}, 'body': 'summary'}]),
                 'a newer status': Rig(statuses=(dict(SKIP, description='Review completed', updated_at='2026-10-12T11:45:00Z'),), checks=(SKIP_RUN,)),
                 'a status that reads otherwise at its second': Rig(statuses=(dict(SKIP, state='pending', description='Review in progress'),),
                                                                    checks=(SKIP_RUN,)),
                 'a newer run in progress': Rig(statuses=(), checks=(SKIP_RUN, dict(SKIP_RUN, id=13, status='in_progress', conclusion=None,
                                                                                     started_at='2026-10-12T11:50:00Z', completed_at=None)))}
        for what, rig in cases.items():
            with self.subTest(what):
                out = self.run_with(rig)[1]
                self.assertEqual(out['outcome'], 'notSkipped', out)
                self.assertEqual(rig.patches, [])
        self.assertIn('check run skipped: Review skipped', self.run_with(cases['another skip'])[1]['detail'])

    def test_failures(self):
        other = {'id': 6, 'user': {'login': 'someone'}, 'body': BOX}
        cases = {'no box': Rig(comments=[other]),
                 'two boxes': Rig(comments=[{'id': 5, 'user': {'login': BOT}, 'body': BOX}] * 2),
                 'read-back unticked': Rig(readback=BOX)}
        for what, rig in cases.items():
            with self.subTest(what):
                code, out = self.run_with(rig)
                self.assertEqual((code, out['outcome']), (0, 'failed'))
                self.assertNotIn('commentId', out)

    def test_an_unreadable_github_answer_is_an_error_line(self):
        rig = Rig()
        rig.run = lambda *argv, ok=(0,): (0, 'not json')
        code, out = self.run_with(rig)
        self.assertEqual(code, 2)
        self.assertIn('not JSON', out['error'])
        self.assertEqual(rig.patches, [])

    def test_a_short_sha_is_refused(self):
        code, out = self.run_with(Rig(), ('--pr', '7', '--head', 'abc', '--repo', 'o/r'))
        self.assertEqual(code, 2)
        self.assertIn('full SHA', out['error'])



LIMITED = {'bot': 'coderabbit', 'state': 'settled', 'kind': 'limited', 'sha': HEAD, 'reason': 'Review rate limited',
           'evidence': ["status success 'Review rate limited' at 2026-10-13T10:04:18Z", 'comment 5'], 'retryAt': '2026-10-13T10:55:19Z'}
NOW = '2026-10-13T11:00:00Z'


class RequestRig:
    """A fake `gh api` for --request: the PR, harvest.observe's reading, the PR's comments, and each POST with its read-back."""

    def __init__(self, cr=LIMITED, observed=NOW, comments=(), state='open', head=HEAD, readback=trigger.REQUEST, post_code=0, later=None):
        self.cr, self.observed, self.comments, self.state, self.head = dict(cr), observed, list(comments), state, head
        self.later, self.pulls = later, 0  # later: the PR's (state, head) on the read just before posting
        self.readback, self.post_code, self.posts = readback, post_code, []

    def observe(self, repo, n, reviewers, auto):
        assert (repo, n, reviewers, auto) == ('o/r', 7, ['coderabbit'], ['coderabbit'])
        return {'headSha': self.head, 'observedAt': self.observed, 'bots': [self.cr], 'comments': []}

    def attempt(self, *argv, input=None):
        assert argv[2:5] == ('-X', 'POST', 'repos/{owner}/{repo}/issues/7/comments') and argv[5:] == ('-f', f'body={trigger.REQUEST}'), argv
        self.posts.append(argv)
        return self.post_code, json.dumps({'id': 99, 'body': trigger.REQUEST}), 'boom' if self.post_code else ''

    def run(self, *argv, ok=(0,)):
        path = argv[-1]
        if path == 'nameWithOwner':
            return 0, json.dumps({'nameWithOwner': 'o/r'})
        if path.endswith('/pulls/7'):
            self.pulls += 1
            state, head = self.later if self.later and self.pulls > 1 else (self.state, self.head)
            return 0, json.dumps({'state': state, 'head': {'sha': head}})
        if path.endswith('/issues/comments/99'):
            return 0, json.dumps({'id': 99, 'body': self.readback, 'created_at': '2026-10-13T11:00:01Z'})
        if '/comments?' in path:
            return 0, json.dumps([self.comments])
        raise trigger.Unusable(f'unexpected {path}')


class Request(unittest.TestCase):
    def run_with(self, rig):
        out = io.StringIO()
        with mock.patch.object(trigger, 'attempt', rig.attempt), mock.patch.object(harvest, 'run', rig.run), \
                mock.patch.object(trigger, 'observe', rig.observe), redirect_stdout(out):
            code = trigger.report(trigger.collect, ['--pr', '7', '--head', HEAD, '--repo', 'o/r', '--request'], seal=True)
        return code, json.loads(out.getvalue())

    def test_a_reset_limit_is_asked_once_and_read_back(self):
        rig = RequestRig()
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome'], out['detail'], out['commentId'], out['at']), (0, 'requested', 'comment 99', 99, '2026-10-13T11:00:01Z'))
        self.assertEqual(out['retryAt'], LIMITED['retryAt'])
        self.assertEqual(len(rig.posts), 1)

    def test_an_earlier_request_after_the_reset_is_not_repeated(self):
        mine = {'id': 50, 'body': '@coderabbitai review\n', 'created_at': '2026-10-13T10:56:00Z'}
        code, out = self.run_with(rig := RequestRig(comments=[mine]))
        self.assertEqual((out['outcome'], out['detail'], out['commentId'], out['at']), ('already', 'comment 50', 50, '2026-10-13T10:56:00Z'))
        self.assertEqual(rig.posts, [])
        before = dict(mine, created_at='2026-10-13T10:50:00Z')
        self.assertEqual(self.run_with(RequestRig(comments=[before]))[1]['outcome'], 'requested', 'one asked before the reset was rate-limited too')

    def test_nothing_is_posted_before_the_reset_or_without_one(self):
        later = dict(LIMITED, retryAt='2026-10-13T11:20:00Z')
        for what, rig, retry in (('extended after the harvest', RequestRig(cr=later), '2026-10-13T11:20:00Z'),
                                 ('no reset time', RequestRig(cr=dict(LIMITED, retryAt=None)), None)):
            with self.subTest(what):
                code, out = self.run_with(rig)
                self.assertEqual((code, out['outcome'], out['retryAt']), (0, 'notDue', retry))
                self.assertNotIn('commentId', out)
                self.assertEqual(rig.posts, [])

    def test_nothing_is_posted_unless_limited_on_the_open_head(self):
        cases = {'closed': RequestRig(state='closed'), 'another head': RequestRig(head='b' * 40),
                 'working': RequestRig(cr=dict(LIMITED, state='working', kind=None, retryAt=None)),
                 'paused': RequestRig(cr=dict(LIMITED, kind='paused', retryAt=None))}
        for what, rig in cases.items():
            with self.subTest(what):
                code, out = self.run_with(rig)
                self.assertEqual((code, out['outcome'], out['retryAt']), (0, 'notLimited', None))
                self.assertEqual(rig.posts, [])

    def test_a_pr_closed_or_pushed_during_the_reads_is_not_asked_about(self):
        for what, later in (('closed', ('closed', HEAD)), ('pushed', ('open', 'b' * 40))):
            with self.subTest(what):
                rig = RequestRig(later=later)
                code, out = self.run_with(rig)
                self.assertEqual((code, out['outcome']), (0, 'notLimited'))
                self.assertEqual(rig.posts, [])

    def test_a_failed_or_unconfirmed_post_is_failed_with_its_limit(self):
        for what, rig in (('post failed', RequestRig(post_code=1)), ('read back differs', RequestRig(readback='something else'))):
            with self.subTest(what):
                code, out = self.run_with(rig)
                self.assertEqual((code, out['outcome'], out['retryAt'], out['phase']), (0, 'failed', None, 'post'), 'it may have posted')
                self.assertNotIn('commentId', out)
                self.assertIsInstance(out['detail'], str)

    def test_a_read_that_fails_before_the_post_says_nothing_was_posted(self):
        # #276: a 503 on the pull read, which comes before the POST.
        rig = RequestRig()

        def unavailable(*argv, ok=(0,)):
            raise trigger.Unusable('gh api repos/{owner}/{repo}/pulls/7: gh: HTTP 503')
        rig.run = unavailable
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome'], out['phase'], out['detail']), (0, 'failed', 'read', 'gh api repos/{owner}/{repo}/pulls/7: gh: HTTP 503'))
        self.assertEqual(rig.posts, [])
        mine = {'id': 50, 'body': '@coderabbitai review', 'created_at': '2026-10-13T10:56:00Z'}
        self.assertEqual(self.run_with(retry := RequestRig(comments=[mine]))[1]['outcome'], 'already', 'a retry still finds a standing request first')
        self.assertEqual(retry.posts, [])
        self.assertNotIn('phase', self.run_with(RequestRig())[1], 'only a failed line has a phase')


if __name__ == '__main__':
    unittest.main()
