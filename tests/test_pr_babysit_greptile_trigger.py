import importlib.util
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'skills/pr-babysit/scripts'))
spec = importlib.util.spec_from_file_location('greptile_trigger', ROOT / 'skills/pr-babysit/scripts/greptile_trigger.py')
trigger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trigger)
import facts  # noqa: E402
import harvest  # noqa: E402  the script's GET helpers, which call facts.run through harvest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

HEAD = 'a' * 40
PUSHED = '2026-10-21T10:00:00Z'
RUN = {'id': 1, 'event': 'push', 'path': '.github/workflows/ci.yml', 'created_at': PUSHED, 'head_sha': HEAD,
       'head_branch': 'fix', 'head_repository': {'full_name': 'o/r'}}


def setUpModule():
    guard_survivors(__name__)


class Rig:
    """A fake `gh`: the PR, its head's runs and timeline, its comments as GET pages (run), and each POST (attempt), which
    adds the comment it makes unless `lost` keeps it off the PR; `post_code`/`post_out` are what the POST answers."""

    def __init__(self, comments=(), runs=(RUN,), timeline=(), state='open', head=HEAD, post_code=0, post_out=None, lost=False,
                 readback=None, pause=0):
        self.comments, self.runs, self.timeline = [dict(c) for c in comments], list(runs), list(timeline)
        self.state, self.head, self.post_code, self.post_out, self.lost = state, head, post_code, post_out, lost
        self.readback, self.pause, self.posts, self.next_id = readback, pause, [], 100
        self.lock = threading.Lock()

    def attempt(self, *argv, input=None):
        assert argv[2:] == ('-X', 'POST', 'repos/{owner}/{repo}/issues/7/comments', '-f', 'body=@greptileai'), argv
        with self.lock:
            self.posts.append(argv)
            self.next_id += 1
            c = {'id': self.next_id, 'body': '@greptileai', 'created_at': '2026-10-21T10:05:00Z'}
        time.sleep(self.pause)  # a slow POST, so a second run reaches the lock meanwhile
        if not self.lost:
            self.comments.append(c)
        return self.post_code, json.dumps(c) if self.post_out is None else self.post_out, 'HTTP 502' if self.post_code else ''

    def run(self, *argv, ok=(0,)):
        path = argv[-1]
        if path.endswith('/pulls/7'):
            return 0, json.dumps({'state': self.state, 'base': {'repo': {'full_name': 'o/r'}},
                                  'head': {'sha': self.head, 'ref': 'fix', 'repo': {'full_name': 'o/r'}}})
        if '/issues/comments/' in path:
            if self.readback is not None:
                return self.readback(path)
            c = next(c for c in self.comments if path.endswith(f"/{c['id']}"))
            return 0, json.dumps(c)
        if '/actions/runs?' in path:
            return 0, json.dumps([{'workflow_runs': self.runs}])
        if '/timeline?' in path:
            return 0, json.dumps([self.timeline])
        if '/comments?' in path:
            with self.lock:
                return 0, json.dumps([self.comments])
        raise trigger.Unusable(f'unexpected {path}')


class Trigger(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def run_with(self, rig):
        out = io.StringIO()
        with mock.patch.object(trigger, 'attempt', rig.attempt), mock.patch.object(harvest, 'run', rig.run), \
                mock.patch.object(trigger, 'record_dir', lambda: self.dir), redirect_stdout(out):
            code = trigger.report(trigger.collect, ['--pr', '7', '--head', HEAD], seal=True)
        return code, json.loads(out.getvalue())

    def record(self):
        return json.loads((self.dir / f'o-r-7-{HEAD}.json').read_text())

    def test_an_unrequested_head_is_asked_once_read_back_and_recorded(self):
        rig = Rig()
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome'], out['detail'], out['at']), (0, 'requested', 'comment 101', '2026-10-21T10:05:00Z'))
        self.assertEqual(len(rig.posts), 1)
        self.assertEqual(self.record()['receipt'], {'id': 101, 'at': '2026-10-21T10:05:00Z'})
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'already')
        self.assertEqual(len(rig.posts), 1, 'a rerun after a lost line posts nothing')

    def test_a_request_after_the_head_event_is_not_repeated_one_before_it_is(self):
        after = {'id': 50, 'body': '@greptileai\n', 'created_at': '2026-10-21T10:01:00Z'}
        code, out = self.run_with(rig := Rig(comments=[after]))
        self.assertEqual((out['outcome'], out['detail'], out['at']), ('already', 'comment 50', '2026-10-21T10:01:00Z'))
        self.assertEqual(rig.posts, [])
        self.assertEqual(self.record()['receipt'], {'id': 50, 'at': '2026-10-21T10:01:00Z'}, 'the match is recorded as the receipt')
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        before = dict(after, created_at='2026-10-21T09:00:00Z')
        mention = {'id': 51, 'body': '@greptileai please review', 'created_at': '2026-10-21T10:02:00Z'}
        self.assertEqual(self.run_with(Rig(comments=[before, mention]))[1]['outcome'], 'requested',
                         "an older head's request and a comment that is not exactly the request leave the head unasked")

    def test_a_lost_post_answer_is_found_posted_and_never_posted_again(self):
        rig = Rig(post_code=1)
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome'], out['detail']), (0, 'requested', 'comment 101'))
        rig = Rig(post_out='garbled')
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'requested')
        self.assertEqual(len(rig.posts), 1)

    def test_a_post_whose_comment_cannot_be_found_is_uncertain_and_stays_so(self):
        rig = Rig(post_code=1, lost=True)
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome']), (0, 'uncertain'))
        self.assertIn('HTTP 502', out['detail'])
        self.assertIn(str(self.dir / f'o-r-7-{HEAD}.json'), out['detail'])
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'uncertain', 'the claim is reconciled, never posted again')
        self.assertEqual(len(rig.posts), 1)
        rig.comments.append({'id': 120, 'body': '@greptileai', 'created_at': '2026-10-21T10:06:00Z'})
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'already', 'the comment shows up later')
        self.assertEqual(self.record()['receipt']['id'], 120)

    def test_a_lost_read_back_is_uncertain_then_reconciled(self):
        rig = Rig(readback=lambda path: (_ for _ in ()).throw(trigger.Unusable(f'gh api {path}: HTTP 502')))
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome']), (0, 'uncertain'))
        self.assertIn('comment 101 was posted and does not read back', out['detail'])
        self.assertNotIn('receipt', self.record())
        rig.readback = None
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'already')
        self.assertEqual((len(rig.posts), self.record()['receipt']['id']), (1, 101))
        rig.readback = lambda path: (0, json.dumps({'id': 101, 'body': 'edited', 'created_at': '2026-10-21T10:05:00Z'}))
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.assertEqual(self.run_with(Rig(readback=rig.readback))[1]['outcome'], 'uncertain', 'a read-back of another body')

    def test_a_reopen_after_a_recorded_request_is_not_asked_again(self):
        rig = Rig()
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'requested')
        rig.timeline = [{'event': 'reopened', 'commit_id': None, 'created_at': '2026-10-21T11:00:00Z'},
                        {'event': 'ready_for_review', 'commit_id': None, 'created_at': '2026-10-21T11:01:00Z'}]
        self.assertEqual(self.run_with(rig)[1], {'outcome': 'already', 'detail': 'comment 101', 'at': '2026-10-21T10:05:00Z', 'seal': mock.ANY})
        self.assertEqual(len(rig.posts), 1)

    def test_a_reopen_after_an_unrecorded_request_asks_once_more(self):
        made_elsewhere = {'id': 60, 'body': '@greptileai', 'created_at': '2026-10-21T10:05:00Z'}
        rig = Rig(comments=[made_elsewhere], timeline=[{'event': 'reopened', 'commit_id': None, 'created_at': '2026-10-21T11:00:00Z'}])
        self.assertEqual(self.run_with(rig)[1]['outcome'], 'requested')
        self.assertEqual(len(rig.posts), 1)

    def test_a_default_types_pull_request_run_dates_a_later_head(self):
        # the owner asked for the previous head by hand; this head arrived later and only a pull_request run whose
        # workflow also runs on reopened dates it (agentrc's own workflows: push covers main alone)
        earlier = {'id': 61, 'body': '@greptileai', 'created_at': '2026-10-21T09:00:00Z'}
        run_ = dict(RUN, event='pull_request', created_at=PUSHED)
        rig = Rig(comments=[earlier], runs=(run_,))
        with mock.patch.object(harvest, 'pr_types', lambda *a: {'opened', 'synchronize', 'reopened'}):
            self.assertEqual(self.run_with(rig)[1]['outcome'], 'requested')
        self.assertEqual(len(rig.posts), 1)

    def test_a_request_at_the_head_event_second_is_ambiguous_and_nothing_is_posted(self):
        tied = {'id': 50, 'body': '@greptileai', 'created_at': PUSHED}
        rig = Rig(comments=[tied])
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome']), (0, 'ambiguous'))
        self.assertIn('comment 50', out['detail'])
        self.assertEqual(rig.posts, [])
        self.assertFalse((self.dir / f'o-r-7-{HEAD}.json').exists(), 'nothing claimed')
        later = dict(tied, id=51, created_at='2026-10-21T10:00:01Z')
        self.assertEqual(self.run_with(Rig(comments=[tied, later]))[1]['outcome'], 'already', 'one after it settles it')

    def test_an_unknown_head_event_time_posts_only_when_no_request_stands(self):
        standing = {'id': 50, 'body': '@greptileai', 'created_at': '2026-10-21T10:01:00Z'}
        rig = Rig(comments=[standing], runs=())
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome']), (0, 'failed'))
        self.assertIn('head event time is unknown', out['detail'])
        self.assertEqual(rig.posts, [])
        self.assertFalse((self.dir / f'o-r-7-{HEAD}.json').exists(), 'nothing claimed')
        self.assertEqual(self.run_with(Rig(runs=()))[1]['outcome'], 'requested')

    def test_nothing_is_posted_unless_the_pr_is_open_at_the_head(self):
        for what, rig in (('closed', Rig(state='closed')), ('another head', Rig(head='b' * 40))):
            with self.subTest(what):
                self.assertEqual(self.run_with(rig)[1]['outcome'], 'notOpen')
                self.assertEqual(rig.posts, [])

    def test_two_concurrent_runs_post_at_most_once(self):
        if facts.fcntl is None:
            self.skipTest('the concurrency check runs on POSIX')
        rig = Rig(pause=0.3)
        outs = []

        def one():
            outs.append(trigger.collect(['--pr', '7', '--head', HEAD]))
        with mock.patch.object(trigger, 'attempt', rig.attempt), mock.patch.object(harvest, 'run', rig.run), \
                mock.patch.object(trigger, 'record_dir', lambda: self.dir):
            threads = [threading.Thread(target=one) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(rig.posts), 1)
        self.assertEqual(sorted(o['outcome'] for o in outs), ['already', 'requested'])

    def test_a_short_sha_or_a_foreign_record_is_an_error_line(self):
        with redirect_stdout(code_out := io.StringIO()):
            rc = trigger.report(trigger.collect, ['--pr', '7', '--head', 'abc'], seal=True)
        self.assertEqual(rc, 2)
        self.assertIn('full SHA', json.loads(code_out.getvalue())['error'])
        (self.dir / f'o-r-7-{HEAD}.json').write_text('{"something": "else"}')
        rc, out = self.run_with(rig := Rig())
        self.assertEqual(rc, 2)
        self.assertIn('not one this script wrote', out['error'])
        self.assertEqual(rig.posts, [])


if __name__ == '__main__':
    unittest.main()
