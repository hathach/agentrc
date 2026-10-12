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

HEAD = 'a' * 40
BOX = '> - [ ] <!-- {"checkboxId":"e9bb8d72-00e8-4f67-9cb2-caf3b22574fe"} --> 🔍 Trigger review'
BOT = 'coderabbitai[bot]'
SKIP = {'context': 'CodeRabbit', 'state': 'success', 'updated_at': '2026-10-12T11:41:10Z',
        'description': 'Review skipped: manual review required for this OSS repository'}


class Rig:
    """A fake `gh api`: the PR, the head's statuses and the PR's comments as GET pages (run), recording each PATCH (attempt)."""

    def __init__(self, statuses=(SKIP,), comments=None, head=HEAD, readback=None):
        self.head, self.statuses, self.patches, self.readback = head, list(statuses), [], readback
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
        if '/comments?' in path:
            return 0, json.dumps([self.comments])
        raise trigger.Unusable(f'unexpected {path}')


class Trigger(unittest.TestCase):
    def run_with(self, rig, argv=('--pr', '7', '--head', HEAD)):
        out = io.StringIO()
        with mock.patch.object(trigger, 'attempt', rig.attempt), mock.patch.object(harvest, 'run', rig.run), redirect_stdout(out):
            code = trigger.report(trigger.collect, list(argv), seal=True)
        return code, json.loads(out.getvalue())

    def test_an_unticked_box_on_a_skipped_head_is_ticked_and_read_back(self):
        rig = Rig()
        code, out = self.run_with(rig)
        self.assertEqual((code, out['outcome'], out['detail']), (0, 'ticked', 'comment 5'))
        self.assertRegex(out['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(rig.patches, [f'summary\n{BOX.replace("[ ]", "[x]", 1)}\nend'])

    def test_a_ticked_box_is_left_alone(self):
        rig = Rig(comments=[{'id': 5, 'user': {'login': BOT}, 'body': BOX.replace('[ ]', '[x]', 1)}])
        self.assertEqual(self.run_with(rig)[1], {'outcome': 'already', 'detail': 'comment 5', 'at': mock.ANY, 'seal': mock.ANY})
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
                self.assertEqual(self.run_with(rig)[1]['outcome'], 'notSkipped')
                self.assertEqual(rig.patches, [])

    def test_failures(self):
        other = {'id': 6, 'user': {'login': 'someone'}, 'body': BOX}
        cases = {'no box': Rig(comments=[other]),
                 'two boxes': Rig(comments=[{'id': 5, 'user': {'login': BOT}, 'body': BOX}] * 2),
                 'read-back unticked': Rig(readback=BOX)}
        for what, rig in cases.items():
            with self.subTest(what):
                code, out = self.run_with(rig)
                self.assertEqual((code, out['outcome']), (0, 'failed'))

    def test_an_unreadable_github_answer_is_an_error_line(self):
        rig = Rig()
        rig.run = lambda *argv, ok=(0,): (0, 'not json')
        code, out = self.run_with(rig)
        self.assertEqual(code, 2)
        self.assertIn('not JSON', out['error'])
        self.assertEqual(rig.patches, [])

    def test_a_short_sha_is_refused(self):
        code, out = self.run_with(Rig(), ('--pr', '7', '--head', 'abc'))
        self.assertEqual(code, 2)
        self.assertIn('full SHA', out['error'])


if __name__ == '__main__':
    unittest.main()
