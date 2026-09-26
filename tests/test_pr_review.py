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
        self.deleted = set()
        self.mutations = []
        self.reply_fails = None
        self.clock = 0

    def tick(self):
        self.clock += 1
        return f'{self.clock:06d}'

    def publish(self, rid):
        """A submitted review's comments turn public: each root opens a thread, each reply joins its root's."""
        for c in self.review_comments[rid]:
            self.inline.append({**c, 'user': {'login': ME}, 'created_at': self.tick()})
            root = c.get('in_reply_to_id')
            if root is None:
                self.threads.append({'id': f"T{c['id']}", 'isResolved': False, 'isOutdated': False,
                                     'comments': {'nodes': [{'databaseId': c['id']}]}})
            else:
                t = next(t for t in self.threads if t['comments']['nodes'][0]['databaseId'] == root)
                t['comments']['nodes'].append({'databaseId': c['id']})

    def submit(self, rid, event='COMMENT', delete=(), edit=None):
        self.review_comments[rid] = [{**c, 'body': (edit or {}).get(c['id'], c['body']), 'line': c.get('drafted_line', c['line'])}
                                     for c in self.review_comments[rid] if c['id'] not in delete]
        self.reviews[rid]['state'] = post.STATE[event]
        self.publish(rid)

    def delete(self, rid):
        self.deleted.add(rid)
        del self.reviews[rid]

    def graphql(self, argv):
        f = dict(argv[i + 1].split('=', 1) for i in range(len(argv) - 1) if argv[i] in ('-f', '-F'))
        if 'addPullRequestReviewThreadReply' in f['query']:
            self.mutations.append(f)
            rid = next(r['id'] for r in self.reviews.values() if r.get('node_id') == f['r'])
            root = next(t['comments']['nodes'][0]['databaseId'] for t in self.threads if t['id'] == f['t'])
            cid = self.next_id = self.next_id + 1
            if self.reply_fails == 'errors':
                return 0, json.dumps({'data': {'addPullRequestReviewThreadReply': None}, 'errors': [{'message': 'thread is outdated'}]})
            if self.reply_fails != 'refused':
                self.review_comments[rid].append({'id': cid, 'in_reply_to_id': root, 'path': 'src/core/a.c', 'line': None, 'body': f['b']})
            if self.reply_fails:
                return 1, '', 'HTTP 502'
            return 0, json.dumps({'data': {'addPullRequestReviewThreadReply': {'comment': {'databaseId': cid}}}})
        if 'resolveReviewThread' in f['query']:
            self.mutations.append(f)
            t = next(t for t in self.threads if t['id'] == f['t'])
            t['isResolved'] = True
            return 0, json.dumps({'data': {'resolveReviewThread': {'thread': {'isResolved': True}}}})
        return 0, json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {
            'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': self.threads}}}}})

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
            return self.graphql(argv)
        if argv[0] == 'api' and '--method' in argv:
            self.posts.append(json.loads(stdin))
            if self.post_fails is True:
                return 1, '', 'HTTP 502'
            rid = self.next_id = self.next_id + 1
            body = json.loads(stdin)
            state = post.STATE[body.get('event')]
            self.reviews[rid] = {'id': rid, 'node_id': f'PRR_{rid}', 'body': body['body'], 'state': state, 'user': {'login': ME}}
            # A pending review's comments have no line until it is submitted, as on GitHub.
            self.review_comments[rid] = [{'id': rid * 10 + i, 'path': c['path'], 'line': c['line'] if body.get('event') else None,
                                          'drafted_line': c['line'], 'body': c['body']} for i, c in enumerate(body['comments'])]
            if body.get('event'):
                self.publish(rid)
            if self.post_fails == 'lost':
                return 1, '', 'HTTP 502'
            return 0, json.dumps({'id': rid, 'node_id': f'PRR_{rid}'})
        if argv[0] == 'api':
            path = next(a for a in argv[1:] if a.startswith('repos/')).split('?')[0]
            page = lambda xs: [list(xs)]  # noqa: E731 - --paginate --slurp shape
            m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/reviews/(\d+)/comments', path)
            if m:
                return 0, json.dumps(page(self.review_comments.get(int(m.group(1)), [])))
            m = re.fullmatch(rf'repos/{REPO}/pulls/{PR}/reviews/(\d+)', path)
            if m and int(m.group(1)) in self.deleted:
                return 1, '', 'gh: Not Found (HTTP 404)'
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

    def adds(self):
        return [m for m in self.gh.mutations if 'addPullRequestReviewThreadReply' in m['query']]


class Post(PostCase):
    """--auto: the review submitted with its event."""

    def test_posts_the_saved_draft_once_with_its_marker_and_verifies_it(self):
        self.save(self.result_for(self.p))
        first = self.post('--auto')
        self.assertEqual(first['status'], 'posted')
        self.assertEqual(len(self.gh.posts), 1)
        sent = self.gh.posts[0]
        self.assertEqual((sent['commit_id'], sent['event']), (self.head, 'COMMENT'))
        self.assertTrue(sent['body'].endswith(f"<!-- agentrc-pr-review:{self.head}:{self.review()['draft']['digest']} -->"))
        self.assertEqual([(c['path'], c['line'], c['side']) for c in sent['comments']], [('src/core/a.c', 21, 'RIGHT')])
        self.assertEqual(first['review']['commentIds'], [5010])
        self.assertEqual(self.review()['findings'][0]['commentId'], 5010)
        with self.assertRaisesRegex(facts.Unusable, 'no pending draft'):
            self.post('--auto')

    def test_replies_go_only_to_comments_our_reviews_posted(self):
        self.save(self.result_for(self.p))
        self.post('--auto')
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'replies': [{'commentId': 5010, 'body': 'Fixed in abc.', 'findingId': 'pr7-f1'}]}), reason='fix')
        self.assertEqual(self.post('--auto')['status'], 'posted')
        self.save(self.result_for(self.p, draft={**draft, 'replies': [{'commentId': 42, 'body': 'Fixed.', 'findingId': 'pr7-f1'}]}), reason='again')
        out = self.post('--auto')
        self.assertEqual(out['status'], 'partial')
        self.assertIn('not a comment our reviews posted', out['replies'][0]['error'])

    def test_a_fix_note_that_fails_leaves_its_thread_to_the_next_review(self):
        self.save(self.result_for(self.p))
        self.post('--auto')
        draft = {'event': 'COMMENT', 'body': 'Fixed.', 'comments': [], 'replies': [{'commentId': 5010, 'body': 'Fixed in abc.', 'findingId': 'pr7-f1'}]}
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'fixed'}], draft=draft), reason='fix')
        failing = self.tmp / 'reply_fails.py'
        failing.write_text('import json,sys\nm=json.load(open(sys.argv[sys.argv.index("--manifest")+1]))\n'
                           'print(json.dumps({"receipts":[{"commentId":r["commentId"],"sent":True,"posted":False,'
                           '"verified":None,"error":"HTTP 502"} for r in m["replies"]]}))\n')
        with mock.patch.object(post, 'REPLY', failing):
            self.assertEqual(self.post('--auto')['status'], 'partial')
        self.assertEqual(self.review()['findings'][0]['resolveDeferred'],
                         {'commentId': 5010, 'head': self.head, 'why': 'HTTP 502', 'replied': False, 'note': 'Fixed in abc.'},
                         'drafted again word for word, so a note that did land is found')
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual([o['id'] for o in shown['open']], ['pr7-f1'])
        self.assertEqual(self.post('--auto')['status'], 'posted', 'the retry posts it')
        self.assertNotIn('resolveDeferred', self.review()['findings'][0])

    def test_a_lost_answer_is_uncertain_and_the_relaunch_recovers_by_marker_without_posting_again(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        # The POST did land on GitHub although its answer was lost.
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        rid = self.gh.next_id = self.gh.next_id + 1
        self.assertEqual(self.post('--auto')['status'], 'uncertain', 'no marker visible yet: never POST again')
        self.assertEqual(self.post('--auto')['status'], 'uncertain', 'nor on any later run')
        self.assertEqual(len(self.gh.posts), 1)
        self.gh.reviews[rid] = {'id': rid, 'body': sent['body'], 'state': 'COMMENTED', 'user': {'login': ME}}
        self.gh.review_comments[rid] = [{'id': 77, 'path': c['path'], 'line': c['line'], 'body': c['body']} for c in sent['comments']]
        out = self.post('--auto')
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
            self.post('--auto')
        self.fake_attempt = orig
        self.assertEqual(self.review()['receipts']['review']['sent'], True, 'the intent was stored before the POST')
        out = self.post('--auto')
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts)), ('posted', True, 1),
                         'the retry finds the landed review by its marker and never POSTs again')

    def test_after_a_push_an_uncertain_review_is_only_recovered_by_its_marker(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        old = self.head
        self.commit('src/core/a.c', 'int z;\n', 'z')
        self.push_pr()
        self.assertEqual(self.post('--auto')['status'], 'uncertain', 'the head moved: look for the marker, never POST')
        self.assertEqual(len(self.gh.posts), 1)
        rid = self.gh.next_id = self.gh.next_id + 1
        self.gh.reviews[rid] = {'id': rid, 'body': sent['body'], 'state': 'COMMENTED', 'user': {'login': ME}}
        self.gh.review_comments[rid] = [{'id': 78, 'path': c['path'], 'line': c['line'], 'body': c['body']} for c in sent['comments']]
        out = self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', old, '--auto'])
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts)), ('posted', True, 1))

    def test_a_marker_found_but_not_read_back_keeps_the_review_recover_only(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.post('--auto')
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        rid = self.gh.next_id = self.gh.next_id + 1
        self.gh.reviews[rid] = {'id': rid, 'body': sent['body'], 'state': 'COMMENTED', 'user': {'login': ME}}
        self.gh.readback_state = 'PENDING'
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        del self.gh.reviews[rid]
        self.assertEqual(self.post('--auto')['status'], 'uncertain', 'the marker vanished for a moment: still never POST again')
        self.assertEqual(len(self.gh.posts), 1)

    def test_a_mismatched_read_back_is_never_posted_again(self):
        self.save(self.result_for(self.p))
        self.gh.readback_state = 'APPROVED'
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.assertEqual(len(self.gh.posts), 1)

    def test_refusals_post_nothing(self):
        self.save(self.result_for(self.p, verdict={'event': 'APPROVE', 'reasons': []}, draft={**self.result_for(self.p)['draft'], 'event': 'APPROVE'}))
        with self.assertRaisesRegex(facts.Unusable, 'never approves'):
            self.post('--auto')
        self.gh.head = 'f' * 40
        with self.assertRaisesRegex(facts.Unusable, 'head moved'):
            self.post()
        self.assertEqual(self.gh.posts, [])

    def test_text_marked_long_is_never_auto_posted_and_is_named_in_a_pending_review(self):
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'comments': [{**draft['comments'][0], 'body': ' '.join(['w'] * 81)}]}))
        with self.assertRaisesRegex(facts.Unusable, r'over-length text \(src/core/a\.c:21\)'):
            self.post('--auto')
        self.assertEqual(self.gh.posts, [])
        out = self.post()
        self.assertEqual((out['status'], out['overLength']), ('drafted', ['src/core/a.c:21']))
        self.assertEqual(self.gh.posts[0]['comments'][0]['body'], ' '.join(['w'] * 81), 'the text is never cut')

    def test_auto_post_measures_what_it_would_submit_whatever_the_draft_marks(self):
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'comments': [{**draft['comments'][0], 'body': ' '.join(['w'] * 81)}]}))
        with self.assertRaisesRegex(facts.Unusable, r'over-length text \(src/core/a\.c:21\)'):
            self.post('--auto')
        self.post('--decline', '--reason', 'next')
        self.save(self.result_for(self.p, draft={**draft, 'replies': [{'commentId': 5010, 'body': ' '.join(['w'] * 61), 'findingId': 'pr7-f1'}]}), reason='note')
        with self.assertRaisesRegex(facts.Unusable, r'over-length text \(fix note on pr7-f1\)'):
            self.post('--auto')
        self.assertEqual(self.gh.posts, [])

    def test_a_long_comment_moved_into_the_body_still_stops_auto_post(self):
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'comments': [draft['comments'][0], {**draft['comments'][1], 'body': ' '.join(['w'] * 81)}]}))
        self.assertEqual([c['line'] for c in self.review()['draft']['moved']], [1], 'moved into the body, kept as the comment it was')
        with self.assertRaisesRegex(facts.Unusable, r'over-length text \(src/core/a\.c:1\)'):
            self.post('--auto')

    def test_auto_post_refuses_a_draft_saved_before_the_length_check(self):
        self.save(self.result_for(self.p))
        led = json.loads(Path(self.p['ledger']).read_text())
        del led['reviews'][-1]['draft']['moved']
        Path(self.p['ledger']).write_text(json.dumps(led))
        with self.assertRaisesRegex(facts.Unusable, 'predates the length check'):
            self.post('--auto')
        self.assertEqual(self.post()['status'], 'drafted', 'the human can still check it in a pending review')

    def test_decline_settles_a_draft_never_created(self):
        self.save(self.result_for(self.p))
        self.assertEqual(self.post('--decline', '--reason', 'not now')['status'], 'declined')
        self.assertEqual((self.review()['status'], self.gh.posts), ('declined', []))
        with self.assertRaisesRegex(facts.Unusable, 'no pending draft'):
            self.post()

    def test_a_partial_or_uncertain_draft_cannot_be_declined(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.post('--auto')
        with self.assertRaisesRegex(facts.Unusable, 'reconcile it, do not decline it'):
            self.post('--decline', '--reason', 'x')


class Draft(PostCase):
    """The default: a pending review, which the human finishes on GitHub."""

    def rid(self):
        return self.review()['receipts']['review']['reviewId']

    def show(self):
        return self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])

    def fixed(self):
        """Our review posted on the head, then a second review of it that finds pr7-f1 fixed, with a fix note."""
        self.save(self.result_for(self.p))
        self.post('--auto')
        root = self.review()['findings'][0]['commentId']
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'fixed'}],
                                  draft={'event': 'COMMENT', 'body': 'Fixed.', 'comments': [],
                                         'replies': [{'commentId': root, 'body': 'Fixed in abc.', 'findingId': 'pr7-f1'}]}), reason='fixed')
        return root

    def test_a_pending_review_without_an_event_is_created_once(self):
        self.save(self.result_for(self.p))
        self.assertEqual(self.post()['status'], 'drafted')
        self.assertNotIn('event', self.gh.posts[0])
        self.assertEqual(self.gh.reviews[self.rid()]['state'], 'PENDING')
        self.assertEqual((self.review()['status'], self.review()['publish']), ('drafted', 'draft'))
        self.assertEqual(self.post()['status'], 'drafted')
        self.assertEqual(len(self.gh.posts), 1)
        with self.assertRaisesRegex(facts.Unusable, 'published without --auto'):
            self.post('--auto')

    def test_another_pending_review_of_ours_refuses(self):
        self.save(self.result_for(self.p))
        self.gh.reviews[1] = {'id': 1, 'body': 'by hand', 'state': 'PENDING', 'user': {'login': ME}}
        with self.assertRaisesRegex(facts.Unusable, 'your pending review 1 is open'):
            self.post()
        self.assertEqual(self.gh.posts, [])

    def test_prepare_refuses_while_it_is_pending_and_records_what_the_human_submitted(self):
        self.save(self.result_for(self.p, verdict={'event': 'REQUEST_CHANGES', 'reasons': []},
                                  draft={**self.result_for(self.p)['draft'], 'event': 'REQUEST_CHANGES'}))
        self.post()
        with self.assertRaisesRegex(facts.Unusable, 'still open on the PR'):
            self.prepare()
        self.assertEqual(self.show()['last'], None, 'a pending review is not on the PR')
        self.gh.submit(self.rid(), 'COMMENT')
        out = self.prepare()
        self.assertEqual((out['synced'], out['mode']), ([{'head': self.head, 'status': 'posted', 'event': 'COMMENT'}], 'same'))
        self.assertEqual(self.review()['findings'][0]['commentId'], self.rid() * 10)
        shown = self.show()
        self.assertEqual((shown['last']['status'], [f['id'] for f in shown['open']]), ('posted', ['pr7-f1']))

    def test_a_review_created_unseen_and_submitted_keeps_the_comment_ids_it_can_tell(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'lost'
        self.assertEqual(self.post()['status'], 'uncertain')
        rid = max(self.gh.reviews)
        self.gh.submit(rid)
        self.prepare()
        self.assertEqual((self.review()['status'], self.review()['findings'][0]['commentId']), ('posted', rid * 10))

    def test_an_unseen_review_never_takes_a_changed_comment_for_its_own(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'lost'
        self.post()
        rid = max(self.gh.reviews)
        self.gh.submit(rid, edit={rid * 10: 'a comment of the human in its place'})
        self.prepare()
        f = self.review()['findings'][0]
        self.assertEqual((f['status'], f.get('commentId'), f.get('published')), ('open', None, None),
                         'an edit and a replacement look alike: the finding stands, with no thread of ours')

    def test_an_unseen_review_with_a_shared_place_never_swaps_its_findings(self):
        one = self.result_for(self.p)['findings'][0]
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'one', 'finding': 0},
                                                                  {'path': 'src/core/a.c', 'line': 21, 'body': 'two', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=[one, {**one, 'why': 'second'}], draft=draft))
        self.gh.post_fails = 'lost'
        self.post()
        rid = max(self.gh.reviews)
        self.gh.submit(rid, delete={rid * 10}, edit={rid * 10 + 1: 'two, softened'})
        self.prepare()
        self.assertEqual([(f['status'], f.get('commentId')) for f in self.review()['findings']], [('open', None), ('open', None)],
                         'which finding the edited comment is cannot be told: neither is dropped or given the other\'s thread')

    def test_an_unseen_review_with_two_identical_comments_never_swaps_them(self):
        one = self.result_for(self.p)['findings'][0]
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'same', 'finding': 0},
                                                                  {'path': 'src/core/a.c', 'line': 21, 'body': 'same', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=[one, {**one, 'why': 'second'}], draft=draft))
        self.gh.post_fails = 'lost'
        self.post()
        rid = max(self.gh.reviews)
        self.gh.submit(rid, edit={rid * 10: 'edited'})
        self.prepare()
        self.assertEqual([(f['status'], f.get('commentId')) for f in self.review()['findings']], [('open', None), ('open', None)])

    def test_two_pending_comments_alike_but_on_different_lines_are_matched_once_submitted(self):
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\nint c;\n', 'c')
        self.head = self.push_pr()
        self.p = self.prepare()
        one = self.result_for(self.p)['findings'][0]
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'same', 'finding': 0},
                                                                  {'path': 'src/core/a.c', 'line': 22, 'body': 'same', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=[one, {**one, 'line': 22}], draft=draft))
        self.post()
        rid = self.rid()
        self.gh.review_comments[rid].reverse()
        self.post()
        self.assertIsNone(self.review()['receipts']['review']['commentIds'], 'a pending read-back cannot tell them apart')
        self.gh.submit(rid)
        self.prepare()
        self.assertEqual([f['commentId'] for f in self.review()['findings']], [rid * 10, rid * 10 + 1])

    def test_an_unseen_review_never_gives_a_deleted_finding_the_comment_edited_into_its_words(self):
        one = self.result_for(self.p)['findings'][0]
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'one', 'finding': 0},
                                                                  {'path': 'src/core/a.c', 'line': 21, 'body': 'two', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=[one, {**one, 'why': 'second'}], draft=draft))
        self.gh.post_fails = 'lost'
        self.post()
        rid = max(self.gh.reviews)
        self.gh.submit(rid, delete={rid * 10}, edit={rid * 10 + 1: 'one'})
        self.prepare()
        self.assertEqual([(f['status'], f.get('commentId')) for f in self.review()['findings']], [('open', None), ('open', None)])

    def test_an_unseen_review_never_follows_bodies_swapped_on_a_shared_place(self):
        one = self.result_for(self.p)['findings'][0]
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'one', 'finding': 0},
                                                                  {'path': 'src/core/a.c', 'line': 21, 'body': 'two', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=[one, {**one, 'why': 'second'}], draft=draft))
        self.gh.post_fails = 'lost'
        self.post()
        rid = max(self.gh.reviews)
        self.gh.submit(rid, edit={rid * 10: 'two', rid * 10 + 1: 'one'})
        self.prepare()
        self.assertEqual([(f['status'], f.get('commentId')) for f in self.review()['findings']], [('open', None), ('open', None)])

    def test_a_retry_after_the_human_submitted_records_it_and_publishes_nothing(self):
        self.save(self.result_for(self.p))
        self.post()
        self.gh.submit(self.rid())
        with self.assertRaisesRegex(facts.Unusable, 'no pending draft'):
            self.post()
        self.assertEqual((self.review()['status'], len(self.gh.posts)), ('posted', 1))

    def test_a_dismissed_review_is_recorded_as_submitted(self):
        self.save(self.result_for(self.p))
        self.post()
        self.gh.submit(self.rid())
        self.gh.reviews[self.rid()]['state'] = 'DISMISSED'
        self.assertEqual(self.prepare()['synced'], [{'head': self.head, 'status': 'posted', 'event': None}])

    def test_a_review_deleted_on_github_is_declined(self):
        self.save(self.result_for(self.p))
        self.post()
        self.gh.delete(self.rid())
        self.assertEqual(self.prepare()['synced'][0]['status'], 'declined')
        self.assertEqual((self.review()['status'], self.show()['last']), ('declined', None))

    def test_a_deleted_inline_comment_drops_its_finding_and_an_edited_one_keeps_what_was_published(self):
        one = self.result_for(self.p)['findings'][0]
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'one', 'finding': 0},
                                                                  {'path': 'src/core/a.c', 'line': 21, 'body': 'two', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=[one, {**one, 'why': 'second'}], draft=draft))
        self.post()
        rid = self.rid()
        self.gh.submit(rid, delete={rid * 10}, edit={rid * 10 + 1: 'two, softened'})
        self.prepare()
        f1, f2 = self.review()['findings']
        self.assertEqual((f1['status'], f1.get('commentId')), ('dropped', None))
        self.assertEqual((f2['status'], f2['commentId'], f2['published']), ('open', rid * 10 + 1, 'two, softened'))
        self.assertEqual([f['id'] for f in self.show()['open']], [f2['id']])

    def test_a_fix_note_is_staged_and_its_thread_resolved_only_once_submitted(self):
        root = self.fixed()
        out = self.post()
        self.assertEqual([(e['kind'], e['state']) for e in out['staged']], [('fixnote', 'staged')])
        self.assertEqual(self.adds()[0]['b'], 'Fixed in abc.')
        self.assertFalse(self.gh.threads[0]['isResolved'], 'nothing resolves before the human submits')
        self.gh.submit(self.rid())
        self.prepare()
        rec = self.review()['receipts']
        self.assertEqual(rec['staged'][0]['state'], 'published')
        self.assertEqual(rec['resolved'], [{'findingId': 'pr7-f1', 'commentId': root, 'resolved': True, 'error': None}])
        self.assertTrue(self.gh.threads[0]['isResolved'])

    def test_a_push_before_the_submit_defers_the_resolve_to_the_next_review(self):
        root = self.fixed()
        self.post()
        self.gh.submit(self.rid())
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b = 0;\n', 'fix')
        self.head = self.push_pr()
        p = self.prepare()
        self.assertIn('head moved', self.review()['findings'][0]['resolveDeferred']['why'])
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual([(o['id'], o['status']) for o in self.show()['open']], [('pr7-f1', 'fixed')], 'carried for its recheck')
        self.save(self.result_for(p, findings=[{'id': 'pr7-f1', 'status': 'fixed'}],
                                  draft={'event': 'COMMENT', 'body': 'Still fixed.', 'comments': [], 'replies': [],
                                         'resolves': [{'findingId': 'pr7-f1', 'commentId': root}]}))
        self.post()
        self.gh.submit(self.rid())
        self.prepare()
        self.assertTrue(self.gh.threads[0]['isResolved'])
        self.assertNotIn('resolveDeferred', self.review()['findings'][0])
        self.assertEqual(self.show()['open'], [])

    def test_pushback_after_our_reply_defers_the_resolve(self):
        root = self.fixed()
        self.post()
        self.gh.submit(self.rid())
        self.gh.inline.append({'id': 990, 'in_reply_to_id': root, 'body': 'not fixed on RP2040', 'created_at': self.gh.tick(),
                               'user': {'login': 'contrib'}})
        self.prepare()
        self.assertEqual(self.review()['findings'][0]['resolveDeferred']['why'], 'new replies since our last one')
        self.assertFalse(self.gh.threads[0]['isResolved'])

    def test_a_fix_note_deleted_before_the_submit_leaves_its_thread_to_the_next_review_for_a_new_note(self):
        self.fixed()
        self.post()
        self.gh.submit(self.rid(), delete={self.review()['receipts']['staged'][0]['replyId']})
        self.prepare()
        self.assertEqual(self.review()['receipts']['staged'][0]['state'], 'rejected')
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual(self.review()['findings'][0]['resolveDeferred']['replied'], False)
        self.assertEqual([o['id'] for o in self.show()['open']], ['pr7-f1'], 'its open thread keeps it in the next review')

    def test_a_republished_fix_note_whose_resolve_is_deferred_is_not_drafted_again(self):
        root = self.fixed()
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['findings'][0]['resolveDeferred'] = {'commentId': root, 'head': self.head, 'why': 'the fix note was rejected', 'replied': False}
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.post()
        self.gh.submit(self.rid())
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b = 0;\n', 'push')
        self.push_pr()
        self.prepare()
        self.assertEqual(self.review()['findings'][0]['resolveDeferred']['replied'], True)

    def test_a_fix_note_edited_before_the_submit_leaves_its_thread_to_the_next_review_to_resolve(self):
        self.fixed()
        self.post()
        reply = self.review()['receipts']['staged'][0]['replyId']
        self.gh.submit(self.rid(), edit={reply: 'Fixed, thanks.'})
        self.prepare()
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual(self.review()['findings'][0]['resolveDeferred']['replied'], True)

    def test_a_reply_whose_answer_was_lost_is_found_again_never_added_twice(self):
        self.fixed()
        self.gh.reply_fails = 'lost'
        out = self.post()
        self.assertEqual((out['status'], out['staged'][0]['state']), ('partial', 'uncertain'))
        self.gh.reply_fails = None
        self.assertEqual(self.post()['status'], 'drafted', 'found by its thread and body digest')
        self.assertEqual(len(self.adds()), 1)

    def test_a_duplicated_lost_reply_is_the_answer_published(self):
        self.fixed()
        self.gh.reply_fails = 'lost'
        self.post()
        rid = self.rid()
        dup = self.gh.review_comments[rid][-1]
        self.gh.review_comments[rid].append({**dup, 'id': dup['id'] + 1000})
        self.gh.submit(rid)
        self.prepare()
        self.assertEqual((self.review()['receipts']['staged'][0]['state'], self.review()['receipts']['staged'][0]['replyId']), ('published', dup['id']))

    def test_a_fix_note_already_on_the_thread_is_not_added_again_and_its_thread_resolves_once_submitted(self):
        root = self.fixed()
        self.gh.inline.append({'id': 777, 'in_reply_to_id': root, 'body': 'Fixed in abc.', 'created_at': self.gh.tick(), 'user': {'login': ME}})
        out = self.post()
        self.assertEqual(([(e['state'], e['replyId']) for e in out['staged']], self.adds()), ([('landed', 777)], []))
        self.gh.submit(self.rid())
        self.prepare()
        self.assertTrue(self.gh.threads[0]['isResolved'])

    def test_a_found_fix_note_deleted_before_the_submit_is_not_resolved_bare(self):
        root = self.fixed()
        self.gh.inline.append({'id': 777, 'in_reply_to_id': root, 'body': 'Fixed in abc.', 'created_at': self.gh.tick(), 'user': {'login': ME}})
        self.post()
        self.gh.inline = [c for c in self.gh.inline if c['id'] != 777]
        self.gh.submit(self.rid())
        self.prepare()
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual(self.review()['findings'][0]['resolveDeferred']['replied'], False)

    def test_a_graphql_error_is_kept_in_the_receipt(self):
        self.fixed()
        self.gh.reply_fails = 'errors'
        out = self.post()
        self.assertEqual((out['staged'][0]['state'], out['staged'][0]['error']), ('uncertain', 'thread is outdated'))

    def test_a_reply_never_confirmed_is_not_added_again_and_is_lost_once_submitted(self):
        self.fixed()
        self.gh.reply_fails = 'refused'
        self.post()
        self.gh.reply_fails = None
        self.assertEqual(self.post()['status'], 'partial')
        self.assertEqual(len(self.adds()), 1)
        self.gh.submit(self.rid())
        self.prepare()
        self.assertEqual(self.review()['receipts']['staged'][0]['state'], 'lost')
        self.assertFalse(self.gh.threads[0]['isResolved'])


class Pushback(PostCase):
    """Our review posted on the head, then replies on its thread."""

    def setUp(self):
        super().setUp()
        self.save(self.result_for(self.p))
        self.post('--auto')
        self.root = self.review()['findings'][0]['commentId']

    def led(self):
        return json.loads(Path(self.p['ledger']).read_text())

    def finding(self):
        return self.led()['reviews'][0]['findings'][0]

    def comment(self, cid, who, body, bot=False):
        self.gh.inline.append({'id': cid, 'in_reply_to_id': self.root, 'body': body, 'created_at': self.gh.tick(),
                               'user': {'login': who, **({'type': 'Bot'} if bot else {})}})
        self.gh.threads[0]['comments']['nodes'].append({'databaseId': cid})

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

    def dispute(self, state, answer=None):
        snap = self.snapshot((950, 'contrib', 'intentional', False))
        d = self.disputes(snap)[0]
        return {'key': d['key'], 'replies': d['replies'], 'judgedHead': self.head, 'threadsFile': str(snap), 'state': state,
                'reason': 'r', **({'answer': answer} if answer else {})}

    def discuss(self, state, answer=None):
        rec = self.dispute(state, answer)
        return self.save(self.result_for(self.p, mode='discussion', findings=[{'id': 'pr7-f1', 'status': state, 'disputes': [rec]}]))

    def concede(self):
        """A concession on the contributor's reply, staged in a pending review; returns our reply's id."""
        self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.comment(950, 'contrib', 'intentional')
        out = self.post()
        self.assertEqual([(e['kind'], e['state']) for e in out['staged']], [('answer', 'staged')])
        return out['staged'][0]['replyId']

    def rid(self):
        return self.led()['reviews'][-1]['receipts']['review']['reviewId']

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

    def test_a_long_answer_is_named_in_its_pending_review(self):
        self.discuss('upheld', {'body': ' '.join(['w'] * 61), 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        out = self.post()
        self.assertEqual((out['status'], out['overLength']), ('drafted', ['answer on pr7-f1']))

    def test_a_discussion_merges_into_the_review_and_appends_an_answer_record(self):
        out = self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.assertEqual((out['mode'], out['answers']), ('discussion', 1))
        reviews = self.led()['reviews']
        self.assertEqual([(r.get('mode'), r['status'], len(r['findings'])) for r in reviews[1:]], [('discussion', 'pending', 0)])
        f = self.finding()
        self.assertEqual((f['status'], f['disputes'][0]['answer']['status']), ('open', 'withdrawn'),
                         'a concession still to publish leaves its finding standing')
        self.assertEqual(f['disputes'][0]['answer']['digest'], ledger.digest('Agreed, withdrawing.'))
        self.assertEqual(self.disputes(self.snapshot((950, 'contrib', 'intentional', False))), [])
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual(shown['last']['mode'], 'full', 'an answer record is not the review whose findings stand')
        self.assertEqual(shown['answers'], [{'findingId': 'pr7-f1', 'commentId': self.root, 'state': 'withdrawn', 'resolve': True,
                                             'body': 'Agreed, withdrawing.', 'reason': 'r', 'url': 'https://x/r0',
                                             'replies': [{'author': 'contrib', 'excerpt': 'intentional'}]}])
        self.snapshot((950, 'contrib', 'the opposite claim', False))
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual(shown['answers'][0]['replies'], [{'author': 'contrib', 'excerpt': None}], 'an edited reply is not the one judged')
        draft = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--draft'])
        self.assertTrue(draft['draft']['body'].startswith('Summary.'), 'an answer record is not the draft shown')
        with self.assertRaisesRegex(facts.Unusable, 'a pending draft'):
            self.save(self.result_for(self.p, mode='discussion', findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': []}]))

    def test_a_published_answer_settles_the_pushback_before_it(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        led = self.led()
        led['reviews'][-1]['receipts']['staged'] = [{'state': 'published', 'replyId': 960}]
        Path(self.p['ledger']).write_text(json.dumps(led))
        got = self.disputes(self.snapshot((950, 'contrib', 'intentional', False), (960, ME, 'Still stands.', False),
                                          (970, 'contrib', 'still intentional', False)))
        self.assertEqual([r['id'] for r in got[0]['replies']], [970])

    def test_an_answer_goes_into_a_pending_review_and_its_thread_resolves_once_submitted(self):
        reply = self.concede()
        sent = self.gh.posts[-1]
        self.assertEqual(sent, {'commit_id': self.head, 'comments': [],
                                'body': f"<!-- agentrc-pr-review:{self.head}:{self.led()['reviews'][-1]['draft']['digest']} -->"})
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.gh.submit(self.rid())
        self.prepare()
        f = self.finding()
        self.assertEqual((f['status'], f['disputes'][-1]['answer']['outcome']), ('withdrawn', 'published'))
        self.assertTrue(self.gh.threads[0]['isResolved'])
        self.assertIn(reply, ledger.answered_ids(self.led()))
        self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])['answers'], [])

    def test_an_edited_concession_reopens_its_finding_answers_the_pushback_and_is_never_resolved(self):
        reply = self.concede()
        self.gh.submit(self.rid(), edit={reply: 'Fair point, but see line 3.'})
        self.prepare()
        f = self.finding()
        a = f['disputes'][-1]['answer']
        self.assertEqual((f['status'], a['outcome'], a['published']), ('open', 'edited', 'Fair point, but see line 3.'))
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertIn(reply, ledger.answered_ids(self.led()))

    def test_a_concession_deleted_before_the_submit_or_with_its_review_reopens_its_finding(self):
        reply = self.concede()
        self.gh.submit(self.rid(), delete={reply})
        self.prepare()
        self.assertEqual((self.finding()['status'], self.finding()['disputes'][-1]['answer']['outcome']), ('open', 'rejected'))
        self.assertFalse(self.gh.threads[0]['isResolved'])

    def test_a_concession_whose_pending_review_is_deleted_reopens_its_finding(self):
        self.concede()
        self.gh.delete(self.rid())
        self.prepare()
        self.assertEqual((self.finding()['status'], self.finding()['disputes'][-1]['answer']['outcome']), ('open', 'rejected'))

    def test_auto_withdraws_a_conceded_finding_only_once_the_answer_is_published(self):
        rec = self.dispute('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'withdrawn', 'disputes': [rec]}],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'comments': [], 'replies': []}), reason='again')
        out = self.post('--auto')
        self.assertEqual(out['answers']['status'], 'drafted')
        auto = lambda: self.led()['reviews'][1]['findings'][0]  # noqa: E731
        self.assertEqual(auto()['status'], 'open', 'submitted without the concession: the finding still stands')
        self.gh.submit(self.rid())
        self.prepare()
        self.assertEqual(auto()['status'], 'withdrawn')
        self.assertTrue(self.gh.threads[0]['isResolved'])

    def test_a_declined_answer_record_reopens_its_concession(self):
        self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.post('--decline', '--reason', 'no')
        self.assertEqual(self.finding()['status'], 'open')

    def test_a_reply_after_the_judgment_keeps_the_answer_out_until_rejudged(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.comment(951, 'contrib', 'and see line 9')
        out = self.post()
        self.assertEqual((out['staged'][0]['state'], out['staged'][0]['error']), ('skipped', 'the thread changed since it was judged; review again'))
        self.assertEqual(self.adds(), [])
        self.gh.submit(self.rid())
        self.prepare()
        self.assertEqual((self.finding()['status'], self.finding()['disputes'][-1]['answer']['outcome']), ('upheld', 'skipped'))
        self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])['answers'], [], 'settled, not awaiting submission')

    def test_an_answer_judged_on_an_earlier_head_is_never_staged_on_a_later_one(self):
        self.discuss('withdrawn', {'body': 'Old-head answer.', 'resolve': True})
        self.comment(950, 'contrib', 'intentional')
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b = 0;\n', 'push')
        self.head = self.push_pr()
        p = self.prepare()
        self.save(self.result_for(p, findings=[{'id': 'pr7-f1', 'status': 'open'}],
                                  draft={'event': 'COMMENT', 'body': 'New head.', 'comments': [], 'replies': []}))
        self.assertEqual((self.post()['staged'], self.adds()), ([], []))

    def test_a_bot_reply_after_the_judgment_leaves_the_answer_current(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.comment(955, 'coderabbitai[bot]', 'agree', bot=True)
        self.assertEqual(self.post()['staged'][0]['state'], 'staged')

    def test_auto_posts_despite_a_long_answer_and_names_it_in_the_answers_pending_review(self):
        rec = self.dispute('upheld', {'body': ' '.join(['w'] * 61), 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'comments': [], 'replies': []}), reason='again')
        out = self.post('--auto')
        self.assertEqual((out['status'], out['answers']['overLength']), ('posted', ['answer on pr7-f1']))

    def test_auto_hands_its_answers_to_one_pending_review_even_across_a_crash(self):
        rec = self.dispute('upheld', {'body': 'Still stands.', 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'comments': [], 'replies': []}), reason='again')
        orig = self.fake_attempt
        def crash(*argv, input=None):
            orig(*argv, input=input)
            raise KeyboardInterrupt
        self.fake_attempt = crash
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto')
        self.fake_attempt = orig
        out = self.post('--auto')
        self.assertEqual((out['status'], out['review']['recovered'], out['answers']['status']), ('posted', True, 'drafted'))
        self.assertEqual(len([r for r in self.led()['reviews'] if r.get('origin')]), 1)
        self.assertEqual([p.get('event') for p in self.gh.posts], ['COMMENT', 'COMMENT', None], 'the answers are never submitted')
        self.assertEqual(self.led()['reviews'][1]['findings'][0]['status'], 'upheld')
        with self.assertRaisesRegex(facts.Unusable, 'no pending draft'):
            self.post('--auto')


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
