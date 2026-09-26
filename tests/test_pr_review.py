"""Tests for pr-review's scripts: real git in temporary repositories, a fake GitHub behind gh."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'pr-review' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / 'skills' / 'pr-babysit' / 'scripts'))

import facts  # noqa: E402
import harvest  # noqa: E402
import ledger  # noqa: E402
import post  # noqa: E402
import prepare  # noqa: E402
import result  # noqa: E402
import threads  # noqa: E402

REPO, PR, ME = 'o/r', 7, 'reviewer'
REAL_RUN = facts.run


def sh(cwd, *argv):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGitHub:
    def __init__(self):
        self.head = None
        self.checks = []
        self.checks_code = 0
        self.reviews = {}
        self.review_comments = {}
        self.inline = []
        self.issue = []
        self.threads = []
        self.posts = []
        self.post_fails = False
        self.readback_state = None
        self.next_id = 500

    def gh(self, argv, stdin=None):
        if argv[:2] == ['pr', 'view']:
            fields = argv[argv.index('--json') + 1]
            if fields == 'headRefOid':
                return 0, json.dumps({'headRefOid': self.head})
            return 0, json.dumps({'number': PR, 'url': f'https://github.com/{REPO}/pull/{PR}', 'title': 't',
                                  'state': 'OPEN', 'author': {'login': 'contrib'}, 'headRefOid': self.head,
                                  'headRefName': 'feature', 'headRepository': {'name': 'r'},
                                  'headRepositoryOwner': {'login': 'contrib'}, 'isCrossRepository': True,
                                  'baseRefName': 'main'})
        if argv[:2] == ['pr', 'checks']:
            return self.checks_code, json.dumps(self.checks) if self.checks else ''
        if argv[:2] == ['repo', 'view']:
            return 0, REPO + '\n'
        if argv[:2] == ['api', 'user']:
            return 0, json.dumps({'login': ME})
        if argv[:2] == ['api', 'graphql']:
            return 0, json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {
                'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': self.threads}}}}})
        if argv[0] == 'api' and '--method' in argv:
            self.posts.append(json.loads(stdin))
            if self.post_fails:
                return 1, '', 'HTTP 502'
            rid = self.next_id = self.next_id + 1
            body = json.loads(stdin)
            state = {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED', 'COMMENT': 'COMMENTED'}[body['event']]
            self.reviews[rid] = {'id': rid, 'body': body['body'], 'state': state, 'user': {'login': ME}}
            self.review_comments[rid] = [{'id': rid * 10 + i, 'path': c['path'], 'line': c['line'], 'body': c['body']}
                                         for i, c in enumerate(body['comments'])]
            return 0, json.dumps({'id': rid})
        if argv[0] == 'api':
            path = next(a for a in argv[1:] if a.startswith('repos/')).split('?')[0]
            page = lambda xs: [list(xs)]  # noqa: E731 - --paginate --slurp shape
            m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/reviews/(\d+)/comments', path)
            if m:
                return 0, json.dumps(page(self.review_comments.get(int(m.group(1)), [])))
            m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/reviews/(\d+)', path)
            if m:
                r = dict(self.reviews[int(m.group(1))])
                if self.readback_state:
                    r['state'] = self.readback_state
                return 0, json.dumps(r)
            if path == f'repos/{REPO}/pulls/{PR}/reviews':
                return 0, json.dumps(page(self.reviews.values()))
            if path == f'repos/{REPO}/pulls/{PR}/comments':
                return 0, json.dumps(page(self.inline))
            if path == f'repos/{REPO}/issues/{PR}/comments':
                return 0, json.dumps(page(self.issue))
        return 1, '', f'unexpected gh {argv}'


class Case(unittest.TestCase):
    """A bare 'remote' at .../o/r.git with main and refs/pull/7/head, and a clone of it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / 'o' / 'r.git'
        self.remote.parent.mkdir()
        sh(self.tmp, 'git', 'init', '--quiet', '--bare', '-b', 'main', str(self.remote))
        self.work = self.tmp / 'work'
        sh(self.tmp, 'git', 'clone', '--quiet', str(self.remote), str(self.work))
        for k, v in (('user.name', 't'), ('user.email', 't@t'), ('commit.gpgsign', 'false')):
            sh(self.work, 'git', 'config', k, v)
        self.env = mock.patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false'})
        self.env.start()
        self.commit('src/core/a.c', 'int a;\n' * 20, 'base')
        sh(self.work, 'git', 'push', '--quiet', 'origin', 'HEAD:main')
        self.base = sh(self.work, 'git', 'rev-parse', 'HEAD')
        self.clone = self.tmp / 'clone'
        sh(self.tmp, 'git', 'clone', '--quiet', str(self.remote), str(self.clone))
        self.gh = FakeGitHub()
        self.cwd = os.getcwd()
        os.chdir(self.clone)

    def tearDown(self):
        self.env.stop()
        os.chdir(self.cwd)
        subprocess.run(['rm', '-rf', str(self.tmp)])

    def commit(self, path, text, msg):
        f = self.work / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        sh(self.work, 'git', 'add', path)
        sh(self.work, 'git', 'commit', '--quiet', '-m', msg)
        return sh(self.work, 'git', 'rev-parse', 'HEAD')

    def push_pr(self, force=False):
        sh(self.work, 'git', 'push', '--quiet', *(['--force'] if force else []), 'origin', f'HEAD:refs/pull/{PR}/head')
        self.gh.head = sh(self.work, 'git', 'rev-parse', 'HEAD')
        return self.gh.head

    def fake_run(self, *argv, ok=(0,)):
        if argv[0] != 'gh':
            return REAL_RUN(*argv, ok=ok)
        code, out, *err = self.gh.gh(list(argv[1:]))
        if code not in ok:
            raise facts.Unusable(f"{' '.join(argv)}: {err[0] if err else ''}")
        return code, out

    def fake_attempt(self, *argv, input=None):
        code, out, *err = self.gh.gh(list(argv[1:]), stdin=input)
        return code, out, err[0] if err else ''

    def call(self, module, argv):
        patches = [mock.patch.object(m, 'run', self.fake_run) for m in (facts, harvest, ledger, prepare)]
        patches.append(mock.patch.object(post, 'attempt', self.fake_attempt))
        for p in patches:
            p.start()
        try:
            return module.collect(argv)
        finally:
            for p in patches:
                p.stop()

    def prepare(self, *extra):
        return self.call(prepare, ['--pr', str(PR), '--repo', REPO, *extra])

    def save(self, out, reason=None):
        f = self.tmp / 'out.json'
        f.write_text(json.dumps({'result': out}))
        return self.call(ledger, ['save', '--pr', str(PR), '--repo', REPO, '--output', str(f), *(['--reason', reason] if reason else [])])

    def mark_posted(self, p, status='posted'):
        led = json.loads(Path(p['ledger']).read_text())
        led['reviews'][-1]['status'] = status
        Path(p['ledger']).write_text(json.dumps(led))

    def result_for(self, p, **over):
        r = {'status': 'reviewed', 'pr': PR, 'head': p['head'], 'mergeBase': p['mergeBase'], 'mode': p['mode'],
             'verdict': {'event': 'COMMENT', 'reasons': ['one open finding']},
             'findings': [{'file': 'src/core/a.c', 'line': 21, 'why': 'b is never initialised', 'severity': 'high',
                           'dimension': 'correctness', 'status': 'open'}],
             'claims': [], 'coverage': {'dropped': [], 'unverified': [], 'unjudged': []}, 'ci': {'state': 'green'},
             'hil': None,
             'draft': {'event': 'COMMENT', 'body': 'Summary.',
                       'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'b is never initialised', 'finding': 0},
                                    {'path': 'src/core/a.c', 'line': 1, 'body': 'far from the diff', 'finding': 0}],
                       'replies': []}}
        r.update(over)
        return r


class Prepare(Case):
    def test_first_review_is_full_with_groups_tooling_and_a_worktree_on_its_branch(self):
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.commit('tools/gen.py', 'print(1)\n', 'tool')
        head = self.push_pr()
        p = self.prepare()
        self.assertEqual((p['mode'], p['head'], p['mergeBase'], p['scopeBase']), ('full', head, self.base, self.base))
        self.assertEqual(p['groups'], ['src/core', 'tools'])
        self.assertEqual(p['tooling'], ['tools/gen.py'])
        self.assertEqual(p['ci']['state'], 'unobserved')
        self.assertEqual(sh(p['worktree'], 'git', 'rev-parse', 'HEAD'), head)
        self.assertEqual(sh(p['worktree'], 'git', 'branch', '--show-current'), f'pr-review-{PR}')
        self.assertEqual(json.loads(Path(p['facts']).read_text())['changed'], ['src/core/a.c', 'tools/gen.py'])
        self.assertEqual(Path(p['changedFile']).read_text(), 'src/core/a.c\ntools/gen.py\n')
        self.assertTrue(p['ledger'].endswith(f'.git/agentrc/pr-review/o__r/{PR}.json'))

    def test_a_fast_forward_push_is_incremental_over_the_new_commits_only(self):
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.push_pr()
        p = self.prepare()
        self.save(self.result_for(p))
        self.commit('src/usb/b.c', 'int c;\n', 'c')
        head = self.push_pr()
        self.assertEqual(self.prepare()['mode'], 'full', 'a draft never posted is no base')
        self.mark_posted(p, 'uncertain')
        with self.assertRaisesRegex(facts.Unusable, 'may have landed unseen: reconcile it first'):
            self.prepare()
        self.mark_posted(p)
        q = self.prepare()
        self.assertEqual((q['mode'], q['scopeBase'], q['priorHead']), ('incremental', p['head'], p['head']))
        self.assertEqual(q['groups'], ['src/usb'])
        self.assertEqual(sh(q['worktree'], 'git', 'rev-parse', 'HEAD'), head)

    def test_a_force_push_or_an_unchanged_head_picks_full_or_same(self):
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.push_pr()
        p = self.prepare()
        self.save(self.result_for(p))
        with self.assertRaisesRegex(facts.Unusable, 'pending draft .* post, reconcile or decline it first'):
            self.prepare()
        self.mark_posted(p, 'partial')
        self.assertEqual(self.prepare()['mode'], 'same', 'a partial review is on the PR: its threads can be discussed')
        self.mark_posted(p)
        self.assertEqual(self.prepare()['mode'], 'same')
        sh(self.work, 'git', 'commit', '--quiet', '--amend', '-m', 'b again')
        self.push_pr(force=True)
        q = self.prepare()
        self.assertEqual((q['mode'], q['scopeBase']), ('full', self.base))
        self.assertEqual(self.prepare('--full')['mode'], 'full')

    def test_a_dirty_or_foreign_worktree_is_refused_never_reset(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        p = self.prepare()
        Path(p['worktree'], 'src/core/a.c').write_text('local edit\n')
        with self.assertRaisesRegex(facts.Unusable, 'uncommitted changes'):
            self.prepare()
        sh(p['worktree'], 'git', 'checkout', '--', '.')
        Path(p['worktree'], 'x').write_text('x')
        sh(p['worktree'], 'git', 'add', 'x')
        sh(p['worktree'], 'git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--quiet', '-m', 'local')
        with self.assertRaisesRegex(facts.Unusable, 'not a head this script pinned'):
            self.prepare()

    def test_ci_states(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        for checks, code, want in (
            ([{'name': 'a', 'state': 'SUCCESS'}], 0, 'green'),
            ([{'name': 'a', 'state': 'SUCCESS'}, {'name': 'b', 'state': 'ACTION_REQUIRED'}], 8, 'pending'),
            ([{'name': 'a', 'state': 'FAILURE'}, {'name': 'b', 'state': 'PENDING'}], 8, 'red'),
            ([], 1, 'unobserved'),
            ([{'name': 'a', 'state': 'SUCCESS'}, {'name': 'b', 'state': 'SOMETHING_NEW'}], 0, 'unknown'),
        ):
            self.gh.checks, self.gh.checks_code = checks, code
            self.assertEqual(self.prepare()['ci']['state'], want, checks)

    def test_check_refuses_a_moved_head(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        head = self.push_pr()
        p = self.prepare()
        os.chdir(p['worktree'])
        ok = self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head])
        self.assertEqual(ok['pins'], {'mergeBase': p['mergeBase'], 'scopeBase': p['scopeBase'], 'mode': 'full', 'groups': p['groups']})
        self.assertEqual(ok['top'], os.path.realpath(p['worktree']))
        Path('src/core/a.c').write_text('edited\n')
        with self.assertRaisesRegex(facts.Unusable, 'uncommitted changes'):
            self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head])
        sh('.', 'git', 'checkout', '--', '.')
        self.commit('src/core/a.c', 'int c;\n', 'c')
        self.push_pr()
        with self.assertRaisesRegex(facts.Unusable, 'head moved'):
            self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head])

    def test_a_filename_with_a_newline_is_one_changed_path(self):
        self.commit('src/a\nb.c', 'int a;\n', 'odd')
        self.push_pr()
        p = self.prepare()
        self.assertEqual(json.loads(Path(p['facts']).read_text())['changed'], ['src/a\nb.c'])
        self.assertIsNone(p['changedFile'], 'the selector file cannot hold that path')
        self.assertEqual(p['groups'], ['src'])

    def test_a_filename_with_a_carriage_return_gets_no_changed_file(self):
        self.commit('src/a\rb.c', 'int a;\n', 'odd')
        self.push_pr()
        self.assertIsNone(self.prepare()['changedFile'])

    def test_a_remote_that_is_not_the_repo_is_refused(self):
        self.push_pr()
        with self.assertRaisesRegex(facts.Unusable, 'no remote of this checkout is x/y'):
            self.call(prepare, ['--pr', str(PR), '--repo', 'x/y'])

    def test_groups_get_shallower_until_they_fit_the_cap(self):
        paths = ['src/a/x/1.c', 'src/a/y/2.c', 'src/b/z/3.c', 'README.md']
        self.assertEqual(prepare.groups_of(paths, 4), (['README.md', 'src/a/x', 'src/a/y', 'src/b/z'], False))
        self.assertEqual(prepare.groups_of(paths, 3), (['README.md', 'src/a', 'src/b'], False))
        self.assertEqual(prepare.groups_of(paths, 2), (['README.md', 'src'], False))
        self.assertEqual(prepare.groups_of(paths, 1), (['.'], True))
        self.assertEqual(prepare.groups_of(['a.c', 'b.c'], 6), (['a.c', 'b.c'], False))


class Ledger(Case):
    def setUp(self):
        super().setUp()
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.push_pr()
        self.p = self.prepare()

    def test_save_numbers_findings_anchors_comments_and_fixes_the_digest(self):
        out = self.save(self.result_for(self.p))
        self.assertEqual((out['inline'], out['moved'], out['findings']), (1, 1, 1))
        led = json.loads(Path(self.p['ledger']).read_text())
        rev = led['reviews'][0]
        self.assertEqual(rev['status'], 'pending')
        self.assertEqual(rev['findings'][0]['id'], f'pr{PR}-f1')
        self.assertEqual(rev['draft']['comments'][0]['findingId'], f'pr{PR}-f1')
        self.assertIn('**src/core/a.c:1**: far from the diff', rev['draft']['body'])
        self.assertEqual(rev['draft']['digest'], out['draftDigest'])
        self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])['open'], [], 'a pending draft carries nothing')
        self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--draft'])['status'], 'pending')
        self.mark_posted(self.p)
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual([f['id'] for f in shown['open']], [f'pr{PR}-f1'])
        self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--finding', f'pr{PR}-f1'])['finding']['line'], 21)

    def test_a_second_draft_for_the_head_needs_the_first_settled_and_a_reason(self):
        self.save(self.result_for(self.p))
        with self.assertRaisesRegex(facts.Unusable, 'pending draft'):
            self.save(self.result_for(self.p))
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][0]['status'] = 'posted'
        Path(self.p['ledger']).write_text(json.dumps(led))
        with self.assertRaisesRegex(facts.Unusable, 'needs --reason'):
            self.save(self.result_for(self.p))
        self.assertTrue(self.save(self.result_for(self.p), reason='the human asked')['saved'])

    def test_a_draft_event_that_is_not_the_verdict_or_an_unsettled_head_is_refused(self):
        with self.assertRaisesRegex(facts.Unusable, "draft's event REQUEST_CHANGES is not the verdict's COMMENT"):
            self.save(self.result_for(self.p, draft={**self.result_for(self.p)['draft'], 'event': 'REQUEST_CHANGES'}))
        self.save(self.result_for(self.p))
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][0]['status'] = 'uncertain'
        Path(self.p['ledger']).write_text(json.dumps(led))
        with self.assertRaisesRegex(facts.Unusable, 'uncertain draft'):
            self.save(self.result_for(self.p), reason='again')

    def test_a_declined_review_carries_nothing(self):
        self.save(self.result_for(self.p))
        self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.p['head'], '--decline', '--reason', 'no'])
        self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])['open'], [])
        self.assertEqual(self.prepare()['mode'], 'full', 'a declined head is not a reviewed head')

    def test_an_absolute_comment_path_is_refused(self):
        r = self.result_for(self.p)
        r['draft']['comments'][0]['path'] = '/abs/src/core/a.c'
        with self.assertRaisesRegex(facts.Unusable, 'absolute path'):
            self.save(r)

    def test_carried_findings_keep_their_id(self):
        self.save(self.result_for(self.p))
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][0]['status'] = 'posted'
        Path(self.p['ledger']).write_text(json.dumps(led))
        carried = {**led['reviews'][0]['findings'][0], 'status': 'fixed'}
        new = {'file': 'src/core/a.c', 'line': 21, 'why': 'new', 'severity': 'low', 'dimension': 'style', 'status': 'open'}
        self.save(self.result_for(self.p, findings=[carried, new]), reason='test')
        ids = [f['id'] for f in json.loads(Path(self.p['ledger']).read_text())['reviews'][1]['findings']]
        self.assertEqual(ids, [f'pr{PR}-f1', f'pr{PR}-f2'])

    def test_a_launch_that_did_not_review_or_a_foreign_ledger_is_refused(self):
        with self.assertRaisesRegex(facts.Unusable, 'nothing to save'):
            self.save({'status': 'blocked', 'reason': 'head-moved'})
        Path(self.p['ledger']).parent.mkdir(parents=True, exist_ok=True)
        Path(self.p['ledger']).write_text(json.dumps({'v': 99, 'repo': REPO, 'pr': PR, 'reviews': []}))
        with self.assertRaisesRegex(facts.Unusable, 'version 99'):
            self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])


class PostCase(Case):
    def setUp(self):
        super().setUp()
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.head = self.push_pr()
        self.p = self.prepare()
        stub = self.tmp / 'reply_stub.py'
        stub.write_text('import json,sys\n'
                        'if "--inspect" in sys.argv:\n'
                        '    c,r=sys.argv[sys.argv.index("--inspect")+1].split(":")\n'
                        '    print(json.dumps({"inspected":[{"commentId":int(c),"replyId":int(r),"bodyDigest":"b","originalDigest":"o","error":None}]})); sys.exit()\n'
                        'if "--reuse" in sys.argv:\n'
                        '    m=json.load(open(sys.argv[sys.argv.index("--reuse")+1]))\n'
                        '    print(json.dumps({"receipts":[{"commentId":r["commentId"],"verified":True,"resolved":True} for r in m["reuses"]]})); sys.exit()\n'
                        'm=json.load(open(sys.argv[sys.argv.index("--manifest")+1]))\n'
                        'print(json.dumps({"receipts":[{"commentId":r["commentId"],"sent":True,"posted":True,'
                        '"verified":True,"resolved":True} for r in m["replies"]]}))\n')
        self.reply = mock.patch.object(post, 'REPLY', stub)
        self.reply.start()

    def tearDown(self):
        self.reply.stop()
        super().tearDown()

    def post(self, *extra):
        return self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.head, *extra])

    def review(self):
        return json.loads(Path(self.p['ledger']).read_text())['reviews'][-1]


class Post(PostCase):
    def test_posts_the_saved_draft_once_with_its_marker_and_verifies_it(self):
        self.save(self.result_for(self.p))
        first = self.post()
        self.assertEqual(first['status'], 'posted')
        self.assertEqual(len(self.gh.posts), 1)
        sent = self.gh.posts[0]
        self.assertEqual((sent['commit_id'], sent['event']), (self.head, 'COMMENT'))
        self.assertTrue(sent['body'].endswith(f"<!-- agentrc-pr-review:{self.head}:{self.review()['draft']['digest']} -->"))
        self.assertEqual([(c['path'], c['line'], c['side']) for c in sent['comments']], [('src/core/a.c', 21, 'RIGHT')])
        self.assertEqual(first['review']['commentIds'], [5010])
        self.assertEqual(self.review()['findings'][0]['commentId'], 5010)
        with self.assertRaisesRegex(facts.Unusable, 'no pending draft'):
            self.post()

    def test_replies_go_only_to_comments_our_reviews_posted(self):
        self.save(self.result_for(self.p))
        self.post()
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'replies': [{'commentId': 5010, 'body': 'Fixed in abc.', 'findingId': 'pr7-f1'}]}), reason='fix')
        self.assertEqual(self.post()['status'], 'posted')
        self.save(self.result_for(self.p, draft={**draft, 'replies': [{'commentId': 42, 'body': 'Fixed.', 'findingId': 'pr7-f1'}]}), reason='again')
        out = self.post()
        self.assertEqual(out['status'], 'partial')
        self.assertIn('not a comment our reviews posted', out['replies'][0]['error'])

    def test_a_lost_answer_is_uncertain_and_the_relaunch_recovers_by_marker_without_posting_again(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.assertEqual(self.post()['status'], 'uncertain')
        # The POST did land on GitHub although its answer was lost.
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        rid = self.gh.next_id = self.gh.next_id + 1
        self.assertEqual(self.post()['status'], 'uncertain', 'no marker visible yet: never POST again')
        self.assertEqual(self.post()['status'], 'uncertain', 'nor on any later run')
        self.assertEqual(len(self.gh.posts), 1)
        self.gh.reviews[rid] = {'id': rid, 'body': sent['body'], 'state': 'COMMENTED', 'user': {'login': ME}}
        self.gh.review_comments[rid] = [{'id': 77, 'path': c['path'], 'line': c['line'], 'body': c['body']} for c in sent['comments']]
        out = self.post()
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts)), ('posted', True, 1))
        self.assertEqual(self.review()['findings'][0]['commentId'], 77)

    def test_a_run_that_dies_in_the_post_only_recovers(self):
        self.save(self.result_for(self.p))
        orig = self.fake_attempt
        def crash(*argv, input=None):
            orig(*argv, input=input)
            raise KeyboardInterrupt
        self.fake_attempt = crash
        with self.assertRaises(KeyboardInterrupt):
            self.post()
        self.fake_attempt = orig
        self.assertEqual(self.review()['receipts']['review']['sent'], True, 'the intent was stored before the POST')
        out = self.post()
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts)), ('posted', True, 1),
                         'the retry finds the landed review by its marker and never POSTs again')

    def test_after_a_push_an_uncertain_review_is_only_recovered_by_its_marker(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.assertEqual(self.post()['status'], 'uncertain')
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        old = self.head
        self.commit('src/core/a.c', 'int z;\n', 'z')
        self.push_pr()
        self.assertEqual(self.post()['status'], 'uncertain', 'the head moved: look for the marker, never POST')
        self.assertEqual(len(self.gh.posts), 1)
        rid = self.gh.next_id = self.gh.next_id + 1
        self.gh.reviews[rid] = {'id': rid, 'body': sent['body'], 'state': 'COMMENTED', 'user': {'login': ME}}
        self.gh.review_comments[rid] = [{'id': 78, 'path': c['path'], 'line': c['line'], 'body': c['body']} for c in sent['comments']]
        out = self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', old])
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts)), ('posted', True, 1))

    def test_a_marker_found_but_not_read_back_keeps_the_review_recover_only(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.post()
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        rid = self.gh.next_id = self.gh.next_id + 1
        self.gh.reviews[rid] = {'id': rid, 'body': sent['body'], 'state': 'COMMENTED', 'user': {'login': ME}}
        self.gh.readback_state = 'PENDING'
        self.assertEqual(self.post()['status'], 'uncertain')
        del self.gh.reviews[rid]
        self.assertEqual(self.post()['status'], 'uncertain', 'the marker vanished for a moment: still never POST again')
        self.assertEqual(len(self.gh.posts), 1)

    def test_a_mismatched_read_back_is_never_posted_again(self):
        self.save(self.result_for(self.p))
        self.gh.readback_state = 'APPROVED'
        self.assertEqual(self.post()['status'], 'uncertain')
        self.assertEqual(self.post()['status'], 'uncertain')
        self.assertEqual(len(self.gh.posts), 1)

    def test_refusals_post_nothing(self):
        self.save(self.result_for(self.p, verdict={'event': 'APPROVE', 'reasons': []}, draft={**self.result_for(self.p)['draft'], 'event': 'APPROVE'}))
        with self.assertRaisesRegex(facts.Unusable, 'never approves'):
            self.post('--auto')
        self.gh.head = 'f' * 40
        with self.assertRaisesRegex(facts.Unusable, 'head moved'):
            self.post()
        self.assertEqual(self.gh.posts, [])

    def test_event_comment_downgrades_and_decline_settles(self):
        self.save(self.result_for(self.p, verdict={'event': 'REQUEST_CHANGES', 'reasons': []}, draft={**self.result_for(self.p)['draft'], 'event': 'REQUEST_CHANGES'}))
        self.assertEqual(self.post('--event', 'COMMENT', '--review-only')['status'], 'posted')
        self.assertEqual(self.gh.posts[0]['event'], 'COMMENT')
        self.save(self.result_for(self.p), reason='again')
        self.assertEqual(self.post('--decline', '--reason', 'not now')['status'], 'declined')
        self.assertEqual(self.review()['status'], 'declined')
        with self.assertRaisesRegex(facts.Unusable, 'no pending draft'):
            self.post()

    def test_a_partial_or_uncertain_draft_cannot_be_declined(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.post()
        with self.assertRaisesRegex(facts.Unusable, 'reconcile it, do not decline it'):
            self.post('--decline', '--reason', 'x')


class Pushback(PostCase):
    """Our posted review on the head, then replies on its thread."""

    def setUp(self):
        super().setUp()
        self.save(self.result_for(self.p))
        self.post()
        self.root = self.review()['findings'][0]['commentId']

    def snapshot(self, *thread):
        """thread: (id, author, body, bot) after our root comment."""
        comments = [{'id': self.root, 'author': ME, 'bot': False, 'digest': 'r0', 'body': 'root', 'url': 'https://x/r0'}]
        comments += [{'id': i, 'author': who, 'bot': bot, 'digest': harvest.digest(body), 'body': body} for i, who, body, bot in thread]
        f = Path(self.p['facts']).parent / f'threads-{self.head}.json'
        f.write_text(json.dumps({'pr': PR, 'viewer': ME, 'comments': comments,
                                 'threads': [{'threadId': 'T', 'resolved': False, 'outdated': False, 'commentIds': [c['id'] for c in comments]}]}))
        return f

    def disputes(self, snap):
        return self.call(ledger, ['disputes', '--pr', str(PR), '--repo', REPO, '--threads', str(snap), '--head', self.head])['disputes']

    def discuss(self, state, answer=None, key=None):
        snap = self.snapshot((950, 'contrib', 'intentional', False))
        d = self.disputes(snap)[0]
        rec = {'key': key or d['key'], 'replies': d['replies'], 'judgedHead': self.head, 'threadsFile': str(snap), 'state': state, 'reason': 'r',
               **({'answer': answer} if answer else {})}
        return self.save(self.result_for(self.p, mode='discussion', findings=[{'id': 'pr7-f1', 'status': state, 'disputes': [rec]}]))

    def test_replies_after_our_last_comment_are_pushback_ours_and_bots_excluded(self):
        self.assertEqual(self.disputes(self.snapshot()), [])
        self.assertEqual(self.disputes(self.snapshot((951, 'coderabbitai[bot]', 'agree', True))), [], 'a bot reply is not pushback')
        got = self.disputes(self.snapshot((950, 'contrib', 'intentional', False), (951, 'coderabbitai[bot]', 'agree', True)))
        self.assertEqual([r['id'] for r in got[0]['replies']], [950])
        self.assertEqual((got[0]['findingId'], got[0]['rootCommentId']), ('pr7-f1', self.root))
        chat = self.disputes(self.snapshot((950, 'contrib', 'intentional', False), (960, ME, 'thanks', False)))
        self.assertEqual([r['id'] for r in chat[0]['replies']], [950], 'a comment of ours no receipt verified settles nothing')
        edited = self.disputes(self.snapshot((950, 'contrib', 'intentional, see line 3', False)))
        self.assertNotEqual(edited[0]['key'], got[0]['key'])

    def test_a_judged_key_is_not_listed_again_on_that_head_and_a_discussion_merges_into_the_review(self):
        out = self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.assertEqual((out['mode'], out['answers']), ('discussion', 1))
        self.assertEqual(len(json.loads(Path(self.p['ledger']).read_text())['reviews']), 1)
        f = self.review()['findings'][0]
        self.assertEqual(f['status'], 'withdrawn')
        self.assertEqual(f['disputes'][0]['answer']['digest'], ledger.digest('Agreed, withdrawing.'))
        self.assertEqual(self.disputes(self.snapshot((950, 'contrib', 'intentional', False))), [])
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual(shown['answers'], [{'findingId': 'pr7-f1', 'commentId': self.root, 'state': 'withdrawn', 'resolve': True,
                                             'body': 'Agreed, withdrawing.', 'reason': 'r', 'url': 'https://x/r0',
                                             'replies': [{'author': 'contrib', 'excerpt': 'intentional'}]}])
        self.snapshot((950, 'contrib', 'the opposite claim', False))
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual(shown['answers'][0]['replies'], [{'author': 'contrib', 'excerpt': None}], 'an edited reply is not the one judged')

    def test_a_verified_answer_settles_the_pushback_before_it(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['findings'][0]['disputes'][-1]['answer']['receipt'] = {'sent': True, 'verified': True, 'replyId': 960}
        Path(self.p['ledger']).write_text(json.dumps(led))
        got = self.disputes(self.snapshot((950, 'contrib', 'intentional', False), (960, ME, 'Still stands.', False),
                                          (970, 'contrib', 'still intentional', False)))
        self.assertEqual([r['id'] for r in got[0]['replies']], [970])

    def test_threads_posts_only_approved_answers_once_with_their_resolve_choice(self):
        self.discuss('upheld', {'body': 'Still stands: line 12.', 'resolve': False})
        with self.assertRaisesRegex(facts.Unusable, 'never under --auto'):
            self.post('--threads', '--approve', 'pr7-f1', '--auto')
        with self.assertRaisesRegex(facts.Unusable, 'no unposted answer for pr7-f9'):
            self.post('--threads', '--approve', 'pr7-f9')
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'}]
        sent = []
        real = post.run_reply
        with mock.patch.object(post, 'run_reply', lambda repo, pr, rs: sent.extend(rs) or real(repo, pr, rs)):
            out = self.post('--threads', '--approve', 'pr7-f1')
        self.assertEqual(out['status'], 'posted')
        self.assertEqual(sent, [{'commentId': self.root, 'body': 'Still stands: line 12.', 'resolve': False}])
        self.assertTrue(self.review()['findings'][0]['disputes'][0]['answer']['receipt']['verified'])
        with self.assertRaisesRegex(facts.Unusable, 'no unposted answer'):
            self.post('--threads', '--approve', 'pr7-f1')

    def test_answers_come_from_the_review_on_the_pr_never_a_declined_draft(self):
        self.discuss('upheld', {'body': 'Still stands: line 12.', 'resolve': False})
        self.save(self.result_for(self.p), reason='again')
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['findings'][0]['disputes'] = [{**led['reviews'][0]['findings'][0]['disputes'][-1],
                                                          'answer': {'body': 'declined text', 'resolve': False, 'digest': ledger.digest('declined text'), 'receipt': None}}]
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.head, '--decline', '--reason', 'no'])
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'}]
        sent = []
        real = post.run_reply
        with mock.patch.object(post, 'run_reply', lambda repo, pr, rs: sent.extend(rs) or real(repo, pr, rs)):
            self.post('--threads', '--approve', 'pr7-f1')
        self.assertEqual([r['body'] for r in sent], ['Still stands: line 12.'])

    def test_a_concession_is_done_only_once_its_thread_is_resolved(self):
        self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'}]
        failing = self.tmp / 'resolve_fails.py'
        failing.write_text('import json,sys\nm=json.load(open(sys.argv[sys.argv.index("--manifest")+1]))\n'
                           'print(json.dumps({"receipts":[{"commentId":r["commentId"],"sent":True,"posted":True,'
                           '"verified":True,"resolved":False,"error":"resolve failed"} for r in m["replies"]]}))\n')
        with mock.patch.object(post, 'REPLY', failing):
            out = self.post('--threads', '--approve', 'pr7-f1')
        self.assertEqual((out['status'], out['answers'][0]['error']), ('partial', 'resolve failed'))
        self.gh.inline.append({'id': 961, 'in_reply_to_id': self.root, 'body': 'Agreed, withdrawing.', 'user': {'login': ME}, 'created_at': '2'})
        calls = []
        real = post.run_reply
        with mock.patch.object(post, 'run_reply', lambda *x: calls.append(x) or real(*x)):
            self.assertEqual(self.post('--threads', '--approve', 'pr7-f1')['status'], 'posted', 'a retry reuses the reply and resolves')
        self.assertEqual(calls, [], 'reconciling never goes through the posting path')

    def test_a_visible_concession_with_newer_pushback_is_left_open(self):
        self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'},
                          {'id': 961, 'in_reply_to_id': self.root, 'body': 'Agreed, withdrawing.', 'user': {'login': ME}, 'created_at': '2'},
                          {'id': 962, 'in_reply_to_id': self.root, 'body': 'actually no', 'user': {'login': 'contrib'}, 'created_at': '3'}]
        out = self.post('--threads', '--approve', 'pr7-f1')
        self.assertEqual((out['status'], out['answers'][0]['resolved']), ('partial', False))
        self.assertIn('new replies after our answer', out['answers'][0]['error'])

    def test_an_identical_reply_of_ours_before_the_pushback_is_not_the_answer(self):
        self.discuss('upheld', {'body': 'Still stands: line 12.', 'resolve': False})
        self.gh.inline = [{'id': 940, 'in_reply_to_id': self.root, 'body': 'Still stands: line 12.', 'user': {'login': ME}, 'created_at': '0'},
                          {'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'}]
        sent = []
        with mock.patch.object(post, 'run_reply', lambda repo, pr, rs: sent.extend(rs) or []):
            out = self.post('--threads', '--approve', 'pr7-f1')
        self.assertEqual(len(sent), 1, 'the new pushback gets the answer sent')
        self.assertEqual((out['status'], out['answers'][0]['done']), ('uncertain', False), 'a missing receipt is not success')
        self.assertIn('no receipt', out['answers'][0]['error'])

    def test_an_answer_is_stored_as_sent_before_it_is_sent(self):
        self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'}]
        seen = []
        def dies(repo, pr, replies):
            seen.append(json.loads(Path(self.p['ledger']).read_text())['reviews'][-1]['findings'][0]['disputes'][-1]['answer']['receipt'])
            raise KeyboardInterrupt
        with mock.patch.object(post, 'run_reply', dies), self.assertRaises(KeyboardInterrupt):
            self.post('--threads', '--approve', 'pr7-f1')
        self.assertTrue(seen[0]['sent'])
        out = self.post('--threads', '--approve', 'pr7-f1')
        self.assertIn('sent earlier and not visible yet', out['answers'][0]['error'])

    def test_a_sent_answer_is_reconciled_only_once_it_is_visible_never_posted_again(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'}]
        lost = self.tmp / 'lost.py'
        lost.write_text('import json,sys\nm=json.load(open(sys.argv[sys.argv.index("--manifest")+1]))\n'
                        'print(json.dumps({"receipts":[{"commentId":r["commentId"],"sent":True,"posted":False,'
                        '"verified":None,"error":"HTTP 502"} for r in m["replies"]]}))\n')
        with mock.patch.object(post, 'REPLY', lost):
            self.assertEqual(self.post('--threads', '--approve', 'pr7-f1')['status'], 'uncertain')
        calls = []
        with mock.patch.object(post, 'run_reply', lambda *a: calls.append(a) or []):
            out = self.post('--threads', '--approve', 'pr7-f1')
        self.assertEqual((calls, out['answers'][0]['error']), ([], 'sent earlier and not visible yet; reconcile by hand'))
        self.gh.inline.append({'id': 961, 'in_reply_to_id': self.root, 'body': 'Still stands.', 'user': {'login': ME}, 'created_at': '2'})
        self.assertEqual(self.post('--threads', '--approve', 'pr7-f1')['status'], 'posted', 'visible now: reply.py reuses it')

    def test_a_bot_reply_after_the_judgment_leaves_the_answer_current(self):
        self.discuss('upheld', {'body': 'Still stands: line 12.', 'resolve': False})
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'},
                          {'id': 955, 'in_reply_to_id': self.root, 'body': 'agree', 'user': {'login': 'coderabbitai[bot]', 'type': 'Bot'}, 'created_at': '2'}]
        self.assertEqual(self.post('--threads', '--approve', 'pr7-f1')['status'], 'posted')

    def test_a_reply_after_the_judgment_makes_the_answer_stale(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        self.gh.inline = [{'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'user': {'login': 'contrib'}, 'created_at': '1'},
                          {'id': 951, 'in_reply_to_id': self.root, 'body': 'and see line 9', 'user': {'login': 'contrib'}, 'created_at': '2'}]
        out = self.post('--threads', '--approve', 'pr7-f1')
        self.assertEqual(out['status'], 'uncertain')
        self.assertIn('the thread changed since it was judged', out['answers'][0]['error'])
        self.gh.inline = self.gh.inline[:1]
        self.assertEqual(self.post('--threads', '--approve', 'pr7-f1')['status'], 'posted')


class Threads(Case):
    def test_every_comment_with_its_thread_and_ours_marked(self):
        self.push_pr()
        self.gh.reviews = {1: {'id': 1, 'body': 'Looks off <!-- agentrc-pr-review:x:y -->', 'user': {'login': ME, 'type': 'User'}},
                           2: {'id': 2, 'body': '', 'user': {'login': 'coderabbitai[bot]', 'type': 'Bot'}}}
        self.gh.inline = [{'id': 10, 'body': 'nit', 'user': {'login': ME, 'type': 'User'}, 'path': 'a.c', 'line': 3,
                           'pull_request_review_id': 1},
                          {'id': 11, 'body': 'bug', 'user': {'login': 'coderabbitai[bot]', 'type': 'Bot'}, 'path': 'b.c',
                           'line': None, 'original_line': 9, 'pull_request_review_id': 2}]
        self.gh.issue = [{'id': 20, 'body': 'thanks', 'user': {'login': 'contrib', 'type': 'User'}}]
        self.gh.threads = [{'id': 'T1', 'isResolved': False, 'isOutdated': False, 'comments': {'nodes': [{'databaseId': 10}]}},
                           {'id': 'T2', 'isResolved': True, 'isOutdated': True, 'comments': {'nodes': [{'databaseId': 11}]}}]
        f = self.tmp / 'threads.json'
        out = self.call(threads, ['--pr', str(PR), '--repo', REPO, '--out', str(f)])
        self.assertEqual((out['count'], out['kinds'], out['unresolvedThreads'], out['ours']),
                         (4, {'review-body': 1, 'review': 2, 'issue': 1}, 1, 2))
        got = {c['id']: c for c in json.loads(f.read_text())['comments']}
        self.assertEqual((got[11]['line'], got[11]['bot'], got[11]['threadId'], got[11]['outdated']), (9, True, 'T2', True))
        self.assertEqual(got[11]['digest'], harvest.digest('bug'))


class Result(unittest.TestCase):
    def test_counts_never_bodies(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'result': {'status': 'reviewed', 'pr': PR, 'head': 'a' * 40, 'mode': 'full',
                                  'verdict': {'event': 'REQUEST_CHANGES', 'reasons': ['r']},
                                  'findings': [{'status': 'open', 'severity': 'high'}, {'status': 'covered', 'severity': 'low'}],
                                  'claims': [{'verdict': 'refuted'}], 'coverage': {'dropped': [1], 'unverified': [], 'unjudged': []},
                                  'ci': {'state': 'green'}, 'hil': None,
                                  'draft': {'body': 'secret words', 'comments': [{}], 'replies': []}}}, f)
        out = result.collect(['--output', f.name])
        self.assertEqual((out['event'], out['findings'], out['openBySeverity'], out['claims'], out['coverage']['dropped']),
                         ('REQUEST_CHANGES', {'open': 1, 'covered': 1}, {'high': 1}, {'refuted': 1}, 1))
        self.assertNotIn('secret words', json.dumps(out))
        Path(f.name).write_text('')
        self.assertEqual(result.collect(['--output', f.name])['status'], 'no-result')



class Workflow(unittest.TestCase):
    def test_stub_harness_passes(self):
        done = subprocess.run(['node', str(ROOT / 'tests' / 'pr_review_harness.mjs')], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
