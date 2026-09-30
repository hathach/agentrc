"""Tests for pr-review's scripts: real git in temporary repositories, a fake GitHub behind gh."""
import importlib.util
import json
import os
import re
import shutil
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


# GitHub's own refusals of a review POST, answered before it creates anything.
REFUSALS = {'refused': 'gh: API rate limit exceeded for user ID 1. (HTTP 403)',
            'invalid': 'gh: Validation Failed: {"resource":"PullRequestReview","code":"custom"} (HTTP 422)',
            'secondary': 'gh: You have exceeded a secondary rate limit. (HTTP 429)'}


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
        self.list_fails = False
        self.readback_state = None
        self.readback_body = None
        self.next_id = 500
        self.deleted = set()
        self.mutations = []
        self.reply_fails = None
        self.submits = []
        self.submit_fails = False
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
        if argv[0] == 'api' and '--method' in argv and argv[argv.index('--method') + 2].endswith('/events'):
            rid = int(argv[argv.index('--method') + 2].split('/')[-2])
            event = json.loads(stdin)['event']
            self.submits.append((rid, event))
            if self.submit_fails is True:
                return 1, '', 'HTTP 502'
            self.submit(rid, event)
            if self.submit_fails == 'lost':
                return 1, '', 'HTTP 502'
            return 0, json.dumps({'id': rid, 'state': post.STATE[event]})
        if argv[0] == 'api' and '--method' in argv:
            self.posts.append(json.loads(stdin))
            if self.post_fails is True:
                return 1, '', 'HTTP 502'
            if self.post_fails in REFUSALS:
                return 1, '', REFUSALS[self.post_fails]
            if self.post_fails == 'proxy':
                return 1, '', 'gh: Forbidden (HTTP 403)'
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
            if self.post_fails == 'created-twice-refused':
                self.reviews[rid + 1] = {**self.reviews[rid], 'id': rid + 1, 'node_id': f'PRR_{rid + 1}'}
                self.next_id += 1
            if self.post_fails in ('created-refused', 'created-twice-refused'):
                return 1, '', REFUSALS['refused']
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
                if self.readback_body:
                    r['body'] += self.readback_body
                return 0, json.dumps(r)
            if path == f'repos/{REPO}/pulls/{PR}/reviews':
                if self.list_fails and self.posts:
                    return 1, '', 'HTTP 502'
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

    def test_save_refuses_a_word_off_the_scale_before_anything_is_stored(self):
        for word in ('major', 'severe', None):
            finding = {'file': 'src/core/a.c', 'line': 21, 'why': 'w', 'severity': word, 'status': 'open'}
            with self.assertRaisesRegex(facts.Unusable, f'finding src/core/a.c:21 has severity {word!r}, not a level'):
                self.save(self.result_for(self.p, findings=[finding]))
            with self.assertRaisesRegex(facts.Unusable, 'not a level'):
                self.save(self.result_for(self.p, mode='discussion', findings=[{'id': f'pr{PR}-f1', 'severity': word or 'x', 'status': 'open'}]))
        self.assertFalse(Path(self.p['ledger']).exists() and json.loads(Path(self.p['ledger']).read_text())['reviews'])
        carried = {'id': f'pr{PR}-f1', 'file': 'src/core/a.c', 'line': 21, 'why': 'w', 'severity': None, 'status': 'open'}
        self.assertIsNone(ledger.refuse_off_scale({'findings': [carried]}), 'a carried record may predate levels')
        claim = {'commentId': 55, 'verdict': 'confirmed', 'severity': 'Major'}
        with self.assertRaisesRegex(facts.Unusable, "claim 55 has severity 'Major', not a level"):
            self.save(self.result_for(self.p, claims=[claim]))
        self.assertIsNone(ledger.refuse_off_scale({'findings': [], 'claims': [{**claim, 'verdict': 'refuted', 'severity': None}]}))

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
        pending = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--pending'])['pending']
        self.assertEqual((pending['status'], pending['digest'], [(f['id'], f['severity'], f['priority']) for f in pending['findings']]),
                         ('pending', out['draftDigest'], [(f'pr{PR}-f1', 'high', 'P1')]))
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--draft'])
        self.assertEqual(shown['status'], 'pending')
        self.assertEqual([(c['findingId'], c['severity']) for c in shown['draft']['comments']], [(f'pr{PR}-f1', 'high')],
                         'each comment names the severity its heading must match')
        self.mark_posted(self.p)
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual([f['id'] for f in shown['open']], [f'pr{PR}-f1'])
        self.assertIsNone(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--pending'])['pending'])
        self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--finding', f'pr{PR}-f1'])['finding']['line'], 21)
        led = json.loads(Path(self.p['ledger']).read_text())
        for status in ('drafted', 'uncertain'):
            led['reviews'][0]['status'] = status
            Path(self.p['ledger']).write_text(json.dumps(led))
            self.assertEqual(self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--pending'])['pending']['status'], status,
                             'a review already pending on GitHub is still not submitted')

    def test_show_names_the_defect_a_finding_shares_and_its_whole_claim(self):
        one = {'file': 'src/core/a.c', 'line': 21, 'why': 'b is never initialised ' * 20, 'severity': 'high', 'dimension': 'correctness', 'status': 'open'}
        self.save(self.result_for(self.p, findings=[{**one, 'defect': 0}, {**one, 'severity': 'medium', 'defect': 0}]))
        self.mark_posted(self.p)
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])['open']
        self.assertEqual([(f['id'], f['defect'], f['why']) for f in shown], [(f'pr{PR}-f1', 0, one['why']), (f'pr{PR}-f2', 0, one['why'])])

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
        for v in (1, 99):
            Path(self.p['ledger']).write_text(json.dumps({'v': v, 'repo': REPO, 'pr': PR, 'reviews': []}))
            with self.assertRaisesRegex(facts.Unusable, f'is version {v}, this script reads 2'):
                self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])


class PostCase(Case):
    def setUp(self):
        super().setUp()
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.head = self.push_pr()
        self.p = self.prepare()

    def post(self, *extra):
        return self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.head, *extra])

    def review(self):
        return json.loads(Path(self.p['ledger']).read_text())['reviews'][-1]

    def adds(self):
        return [m for m in self.gh.mutations if 'addPullRequestReviewThreadReply' in m['query']]

    def crash_at(self, part):
        """The next call whose argv names `part` dies after it is sent, as a killed run would."""
        orig = self.fake_attempt
        def crash(*argv, input=None):
            got = orig(*argv, input=input)
            if any(part in a for a in argv):
                self.fake_attempt = orig
                raise KeyboardInterrupt
            return got
        self.fake_attempt = crash

    def crash_on_head_view(self, n):
        """The run dies on its n-th read of the PR head, as a killed run would."""
        orig, views = self.fake_run, []
        def crash(*argv, ok=(0,)):
            views.extend(a for a in argv if a == 'headRefOid')
            if len(views) == n:
                self.fake_run = orig
                raise KeyboardInterrupt
            return orig(*argv, ok=ok)
        self.fake_run = crash


class Post(PostCase):
    """--auto: the pending review submitted with its event."""

    def test_posts_the_saved_draft_once_with_its_marker_and_verifies_it(self):
        self.save(self.result_for(self.p))
        first = self.post('--auto')
        self.assertEqual(first['status'], 'posted')
        self.assertEqual(len(self.gh.posts), 1)
        sent = self.gh.posts[0]
        self.assertEqual((sent['commit_id'], 'event' in sent), (self.head, False), 'created pending, as a draft is')
        self.assertEqual(self.gh.submits, [(first['review']['reviewId'], 'COMMENT')], 'then submitted once, with its event')
        self.assertEqual(first['review']['submitIntent'], 'COMMENT')
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
        self.assertEqual(out['status'], 'posted', 'a note with no thread of ours is skipped, not a reason to hold the review')
        self.assertIn('not a comment our reviews posted', out['staged'][0]['error'])

    def test_a_fix_note_that_fails_leaves_its_thread_to_the_next_review(self):
        self.save(self.result_for(self.p))
        self.post('--auto')
        draft = {'event': 'COMMENT', 'body': 'Fixed.', 'comments': [], 'replies': [{'commentId': 5010, 'body': 'Fixed in abc.', 'findingId': 'pr7-f1'}]}
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'fixed'}], draft=draft), reason='fix')
        self.gh.reply_fails = 'lost'
        out = self.post('--auto')
        self.assertEqual((out['status'], out['handedOver']), ('drafted', 'a fix note was not confirmed on the pending review'))
        self.assertEqual(len(self.gh.submits), 1, 'the review is left pending for the human, never submitted')
        self.assertEqual(self.review()['publish'], 'draft')
        with self.assertRaisesRegex(facts.Unusable, 'still open on the PR'):
            self.prepare()
        self.gh.submit(self.review()['receipts']['review']['reviewId'])
        self.prepare()
        self.assertEqual(self.review()['receipts']['staged'][0]['state'], 'published', 'the note that did land is found')
        self.assertTrue(self.gh.threads[0]['isResolved'])

    def test_a_run_that_dies_in_the_submit_is_handed_to_the_human_never_submitted_again(self):
        self.save(self.result_for(self.p))
        self.gh.submit_fails = True
        self.crash_at('/events')
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto')
        self.assertEqual(self.review()['receipts']['review']['submitIntent'], 'COMMENT', 'the intent was stored before the submit')
        out = self.post('--auto')
        self.assertEqual((out['handedOver'], self.review()['publish']), ("the submit's outcome is unknown", 'draft'))
        self.assertEqual(len(self.gh.submits), 1)

    def test_a_review_handed_to_the_human_exits_1(self):
        for out, code in (({'status': 'drafted', 'handedOver': 'x'}, 1), ({'status': 'drafted'}, 0), ({'status': 'failed'}, 1)):
            with mock.patch.object(post, 'collect', return_value=out), mock.patch('sys.stdout'):
                self.assertEqual(post.main([]), code)

    def test_a_submit_whose_answer_is_lost_is_settled_by_its_read_back(self):
        self.save(self.result_for(self.p))
        self.gh.submit_fails = 'lost'
        self.assertEqual(self.post('--auto')['status'], 'posted', 'it went through: the read-back shows it submitted')
        self.save(self.result_for(self.p), reason='again')
        self.gh.submit_fails = True
        out = self.post('--auto')
        self.assertEqual((out['status'], out['handedOver']), ('drafted', "the submit's outcome is unknown"))
        self.assertEqual(self.call(post, ['--pr', str(PR), '--repo', REPO, '--sync'])['status'], 'synced')
        self.assertEqual(len(self.gh.submits), 2, 'never sent again')

    def test_a_push_after_the_pending_review_was_made_hands_it_to_the_human(self):
        self.save(self.result_for(self.p))
        self.post('--auto')
        draft = {'event': 'COMMENT', 'body': 'Fixed.', 'comments': [], 'replies': [{'commentId': 5010, 'body': 'Fixed in abc.', 'findingId': 'pr7-f1'}]}
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'fixed'}], draft=draft), reason='fix')
        old, submits = self.head, list(self.gh.submits)
        self.crash_on_head_view(2)  # dies on the head check after the pending review was made
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto')
        self.assertEqual(self.review()['status'], 'drafted')
        with self.assertRaisesRegex(facts.Unusable, 'still open on the PR'):
            self.prepare()
        self.commit('src/core/a.c', 'int z;\n', 'z')
        self.push_pr()
        out = self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', old, '--auto'])
        self.assertEqual((out['status'], out['handedOver'], self.gh.submits), ('drafted', post.EARLIER, submits))
        self.assertEqual(self.adds(), [], 'no fix note is added to a review an earlier run left')

    def test_an_auto_review_an_earlier_run_left_pending_is_handed_to_the_human_and_settled_once_submitted(self):
        self.save(self.result_for(self.p))
        self.crash_on_head_view(2)  # dies on the head check after the pending review was made
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto')
        synced = lambda: self.call(post, ['--pr', str(PR), '--repo', REPO, '--sync'])['reviews'][0]['status']
        self.assertEqual(synced(), 'drafted', 'still pending: left for post.py --auto')
        out = self.post('--auto')
        self.assertEqual((out['handedOver'], self.review()['publish']), (post.EARLIER, 'draft'))
        self.gh.submit(self.review()['receipts']['review']['reviewId'])
        self.assertEqual(synced(), 'posted')
        self.assertEqual(self.gh.submits, [])

    def test_a_post_github_refused_created_nothing_and_a_later_run_posts_it_once(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'refused'
        for _ in range(2):
            out = self.post()
            self.assertEqual((out['status'], out['review']['sent']), ('failed', False))
            self.assertIn('HTTP 403', out['review']['error'])
            self.assertEqual(self.review()['status'], 'pending', 'nothing reached GitHub: the draft is still only ours')
        self.gh.post_fails = False
        self.assertEqual(self.post()['status'], 'drafted')
        self.assertEqual((len(self.gh.posts), len(self.gh.reviews)), (3, 1))

    def test_a_validation_failure_or_a_secondary_limit_created_nothing_either(self):
        self.save(self.result_for(self.p))
        for refusal in ('invalid', 'secondary'):
            self.gh.post_fails = refusal
            out = self.post()
            self.assertEqual((out['status'], out['review']['sent'], self.review()['status']), ('failed', False, 'pending'), refusal)
        self.gh.post_fails = False
        self.assertEqual((self.post()['status'], len(self.gh.posts), len(self.gh.reviews)), ('drafted', 3, 1))

    def test_a_refusal_whose_marker_cannot_be_read_back_stays_sent(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails, self.gh.list_fails = 'refused', True
        with self.assertRaisesRegex(facts.Unusable, 'HTTP 502'):
            self.post()
        self.assertEqual((self.review()['status'], self.review()['receipts']['review']['sent']), ('uncertain', True))
        self.gh.post_fails = self.gh.list_fails = False
        self.assertEqual(self.post()['status'], 'uncertain', 'no marker proves it absent: never POST again')
        self.assertEqual(len(self.gh.posts), 1)

    def test_a_refusal_with_two_reviews_carrying_its_marker_is_reconciled_by_hand(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'created-twice-refused'
        self.assertEqual((self.post()['status'], self.review()['receipts']['review']['sent']), ('uncertain', True))
        self.gh.post_fails = False
        out = self.post()
        self.assertEqual(out['status'], 'uncertain')
        self.assertIn('2 reviews carry this draft', out['review']['error'])
        self.assertEqual(len(self.gh.posts), 1)

    def test_a_4xx_after_the_review_was_created_is_recovered_by_its_marker(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'created-refused'
        out = self.post()
        self.assertEqual((out['status'], out['review']['sent'], out['review']['recovered']), ('drafted', True, True))
        self.assertEqual((len(self.gh.posts), len(self.gh.reviews)), (1, 1))

    def test_any_other_4xx_stays_sent_and_is_only_recovered_by_its_marker(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'proxy'
        out = self.post()
        self.assertEqual((out['status'], out['review']['sent']), ('uncertain', True))
        self.gh.post_fails = False
        self.assertEqual(self.post()['status'], 'uncertain', 'it may still show up: never POST again')
        self.assertEqual(len(self.gh.posts), 1)

    def test_an_auto_post_github_refused_is_failed_not_handed_over(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'refused'
        out = self.post('--auto')
        self.assertEqual((out['status'], out['review']['sent'], self.review()['status']), ('failed', False, 'pending'))
        self.gh.post_fails = False
        self.assertEqual((self.post('--auto')['status'], len(self.gh.reviews)), ('posted', 1))

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
        self.gh.reviews[rid] = {'id': rid, 'node_id': f'PRR_{rid}', 'body': sent['body'], 'state': 'PENDING', 'user': {'login': ME}}
        self.gh.review_comments[rid] = [{'id': 77, 'path': c['path'], 'line': None, 'drafted_line': c['line'], 'body': c['body']}
                                        for c in sent['comments']]
        out = self.post('--auto')
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts)), ('drafted', True, 1))
        self.assertEqual((out['handedOver'], self.gh.submits), (post.EARLIER, []))

    def test_a_run_that_dies_in_the_post_only_recovers(self):
        self.save(self.result_for(self.p))
        self.crash_at('POST')
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto')
        self.assertEqual(self.review()['receipts']['review']['sent'], True, 'the intent was stored before the POST')
        out = self.post('--auto')
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts), self.gh.submits), ('drafted', True, 1, []),
                         'the retry finds the landed review by its marker, never POSTs again, and leaves it to the human')
        self.assertEqual(out['handedOver'], post.EARLIER)

    def test_a_run_that_dies_in_the_post_leaves_it_uncertain_across_a_push(self):
        self.save(self.result_for(self.p))
        self.crash_at('POST')
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto')
        self.assertEqual(self.review()['status'], 'uncertain', 'it may be on GitHub from its send intent on')
        old = self.head
        self.commit('src/core/a.c', 'int z;\n', 'z')
        self.push_pr()
        with self.assertRaises(facts.Unusable):
            self.prepare()
        out = self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', old, '--auto'])
        self.assertEqual((out['status'], out['review']['recovered'], out['handedOver'], len(self.gh.posts), self.gh.submits),
                         ('drafted', True, 'the PR head moved before the submit', 1, []))

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
        self.gh.reviews[rid] = {'id': rid, 'node_id': f'PRR_{rid}', 'body': sent['body'], 'state': 'PENDING', 'user': {'login': ME}}
        self.gh.review_comments[rid] = [{'id': 78, 'path': c['path'], 'line': None, 'drafted_line': c['line'], 'body': c['body']}
                                        for c in sent['comments']]
        out = self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', old, '--auto'])
        self.assertEqual((out['status'], out['review']['recovered'], out['handedOver'], len(self.gh.posts), self.gh.submits),
                         ('drafted', True, 'the PR head moved before the submit', 1, []), 'found, but a review of an old head is the human\'s')

    def test_a_marker_found_but_not_read_back_keeps_the_review_recover_only(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = True
        self.post('--auto')
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        rid = self.gh.next_id = self.gh.next_id + 1
        self.gh.reviews[rid] = {'id': rid, 'body': sent['body'], 'state': 'PENDING', 'user': {'login': ME}}
        self.gh.readback_body = ' (edited)'
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.assertEqual(self.post('--auto')['status'], 'uncertain', 'still never POST again')
        self.assertEqual(len(self.gh.posts), 1)

    def test_a_mismatched_read_back_is_never_posted_again(self):
        self.save(self.result_for(self.p))
        self.gh.readback_body = ' (edited)'
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.assertEqual(len(self.gh.posts), 1)

    def test_an_unverified_auto_review_the_human_submitted_is_settled_by_sync(self):
        self.save(self.result_for(self.p))
        self.gh.post_fails = 'lost'
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.gh.post_fails = False
        self.gh.submit(max(self.gh.reviews))
        self.prepare()
        self.assertEqual((self.review()['status'], self.gh.submits, len(self.gh.posts)), ('posted', [], 1))

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
        """ledger.py show over a threads.py snapshot of the PR as it stands, as the workflow runs it."""
        snap = self.tmp / 'threads.json'
        self.call(threads, ['--pr', str(PR), '--repo', REPO, '--out', str(snap)])
        return self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap)])

    def due(self):
        return {o['id']: o['resolveDue'] for o in self.show()['open'] if o['resolveDue']}

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
        self.assertEqual(rec['resolved'], [{'findingId': 'pr7-f1', 'commentId': root, 'state': 'resolved', 'error': None}])
        self.assertTrue(self.gh.threads[0]['isResolved'])
        self.assertEqual((self.due(), self.show()['heldThreads']), ({}, []), 'resolved: nothing due')

    def test_a_push_before_the_submit_defers_the_resolve_to_the_next_review(self):
        root = self.fixed()
        self.post()
        self.gh.submit(self.rid())
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b = 0;\n', 'fix')
        self.head = self.push_pr()
        p = self.prepare()
        self.assertIn('head moved', self.review()['receipts']['resolved'][0]['error'])
        self.assertEqual(self.review()['receipts']['resolved'][0]['state'], 'deferred')
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual([(o['id'], o['status']) for o in self.show()['open']], [('pr7-f1', 'fixed')], 'carried for its recheck')
        self.assertEqual(self.due(), {'pr7-f1': {'replied': True}})
        self.save(self.result_for(p, findings=[{'id': 'pr7-f1', 'status': 'fixed'}],
                                  draft={'event': 'COMMENT', 'body': 'Still fixed.', 'comments': [], 'replies': [],
                                         'resolves': [{'findingId': 'pr7-f1', 'commentId': root}]}))
        self.post()
        self.gh.submit(self.rid())
        self.prepare()
        self.assertTrue(self.gh.threads[0]['isResolved'])
        self.assertEqual(self.show()['open'], [])

    def test_a_due_resolve_whose_reply_was_deleted_since_is_not_resolved(self):
        root = self.fixed()
        self.post()
        self.gh.submit(self.rid())
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b = 0;\n', 'fix')
        self.head = self.push_pr()
        p = self.prepare()
        self.save(self.result_for(p, findings=[{'id': 'pr7-f1', 'status': 'fixed'}],
                                  draft={'event': 'COMMENT', 'body': 'Still fixed.', 'comments': [], 'replies': [],
                                         'resolves': [{'findingId': 'pr7-f1', 'commentId': root}]}))
        self.post()
        gone = {c['id'] for c in self.gh.inline if c.get('in_reply_to_id') == root}
        self.gh.inline = [c for c in self.gh.inline if c['id'] not in gone]
        nodes = self.gh.threads[0]['comments']['nodes']
        nodes[:] = [n for n in nodes if n['databaseId'] not in gone]
        self.gh.submit(self.rid())
        self.prepare()
        self.assertFalse(self.gh.threads[0]['isResolved'], 'no reply of ours is on the thread any more')
        self.assertEqual(self.review()['receipts']['resolved'][-1]['error'], 'no published reply of ours on the thread')
        self.assertEqual(self.due(), {'pr7-f1': {'replied': False}}, 'the next review drafts the note again')

    def test_pushback_after_our_reply_defers_the_resolve(self):
        root = self.fixed()
        self.post()
        self.gh.submit(self.rid())
        self.gh.inline.append({'id': 990, 'in_reply_to_id': root, 'body': 'not fixed on RP2040', 'created_at': self.gh.tick(),
                               'user': {'login': 'contrib'}})
        self.prepare()
        self.assertEqual(self.review()['receipts']['resolved'][0]['error'], 'new replies since our last one')
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual(self.due(), {'pr7-f1': {'replied': True}}, 'due, for the next recheck to judge the reply first')

    def test_a_fix_note_deleted_before_the_submit_leaves_its_thread_to_the_next_review_for_a_new_note(self):
        self.fixed()
        self.post()
        self.gh.submit(self.rid(), delete={self.review()['receipts']['staged'][0]['replyId']})
        self.prepare()
        self.assertEqual(self.review()['receipts']['staged'][0]['state'], 'rejected')
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual(self.due(), {'pr7-f1': {'replied': False}}, 'its open thread keeps it in the next review, for a new note')

    def test_a_thread_reopened_after_our_resolve_is_held_never_resolved_again(self):
        self.fixed()
        self.post()
        self.gh.submit(self.rid())
        self.prepare()
        self.gh.threads[0]['isResolved'] = False
        shown = self.show()
        self.assertEqual((shown['open'], [h['why'] for h in shown['heldThreads']]), ([], ['reopened after it was resolved']))

    def test_a_run_that_dies_in_a_resolve_leaves_its_intent_held(self):
        self.fixed()
        self.post()
        orig = self.gh.graphql
        def crash(argv):
            got = orig(argv)
            if any('resolveReviewThread' in a for a in argv):
                raise KeyboardInterrupt
            return got
        self.gh.graphql = crash
        self.gh.submit(self.rid())
        with self.assertRaises(KeyboardInterrupt):
            self.prepare()
        self.gh.graphql = orig
        self.assertEqual(self.review()['receipts']['resolved'][0]['state'], 'sent', 'stored before the mutation')

    def test_a_resolve_not_confirmed_is_held_never_tried_again(self):
        refused = (0, json.dumps({'data': {'resolveReviewThread': None}, 'errors': [{'message': 'no'}]}))
        for answer in ((1, '', 'HTTP 502'), (0, json.dumps({'data': {}})), refused):
            with self.subTest(answer=answer):
                self.setUp()
                self.fixed()
                self.post()
                orig = self.gh.graphql
                self.gh.graphql = lambda argv: answer if any('resolveReviewThread' in a for a in argv) else orig(argv)  # noqa: B023
                self.gh.submit(self.rid())
                self.prepare()
                self.gh.graphql = orig
                self.assertEqual(self.review()['receipts']['resolved'][0]['state'], 'sent')
                shown = self.show()
                self.assertEqual((shown['open'], [h['why'] for h in shown['heldThreads']]), ([], ['resolve unconfirmed']))

    def test_a_fix_note_edited_before_the_submit_leaves_its_thread_to_the_next_review_to_resolve(self):
        self.fixed()
        self.post()
        reply = self.review()['receipts']['staged'][0]['replyId']
        self.gh.submit(self.rid(), edit={reply: 'Fixed, thanks.'})
        self.prepare()
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual(self.due(), {'pr7-f1': {'replied': True}}, 'the edited note is on the thread: only the resolve is due')

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

    def pub(self):
        """The newest publication: an answer publication once one is made."""
        return list(ledger.publications(self.led()))[-1][0]

    def rid(self):
        return self.pub()['receipts']['review']['reviewId']

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

    def test_a_discussion_merges_into_the_review_and_keeps_its_answers_on_it(self):
        out = self.discuss('withdrawn', {'body': 'Agreed, withdrawing.', 'resolve': True})
        self.assertEqual((out['mode'], out['answers']), ('discussion', 1))
        reviews = self.led()['reviews']
        self.assertEqual(len(reviews), 1, 'answers are no review of their own')
        self.assertEqual([(a['status'], a['answers']) for a in reviews[0]['answerPublications']],
                         [('pending', [['pr7-f1', ledger.digest('Agreed, withdrawing.')]])])
        f = self.finding()
        self.assertEqual((f['status'], f['disputes'][0]['answer']['status']), ('open', 'withdrawn'),
                         'a concession still to publish leaves its finding standing')
        self.assertEqual(f['disputes'][0]['answer']['digest'], ledger.digest('Agreed, withdrawing.'))
        self.assertEqual(self.disputes(self.snapshot((950, 'contrib', 'intentional', False))), [])
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual(shown['last']['mode'], 'full')
        self.assertEqual(shown['answers'], [{'findingId': 'pr7-f1', 'commentId': self.root, 'state': 'withdrawn', 'resolve': True,
                                             'body': 'Agreed, withdrawing.', 'reason': 'r', 'url': 'https://x/r0',
                                             'replies': [{'author': 'contrib', 'excerpt': 'intentional'}]}])
        self.snapshot((950, 'contrib', 'the opposite claim', False))
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])
        self.assertEqual(shown['answers'][0]['replies'], [{'author': 'contrib', 'excerpt': None}], 'an edited reply is not the one judged')
        draft = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--draft'])
        self.assertTrue(draft['draft']['body'].startswith('Summary.'), 'the review is the draft shown')
        with self.assertRaisesRegex(facts.Unusable, 'a pending draft'):
            self.save(self.result_for(self.p, mode='discussion', findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': []}]))

    def test_an_answer_publication_carries_only_its_own_answers(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        led = self.led()
        led['reviews'][0]['answerPublications'][0]['answers'] = [['pr7-f1', 'another digest']]
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.comment(950, 'contrib', 'intentional')
        self.assertEqual(self.post()['staged'], [], 'an answer it was not made for is not staged')

    def test_an_answers_pending_review_left_open_stops_the_next_prepare(self):
        self.concede()
        with self.assertRaisesRegex(facts.Unusable, 'still open on the PR'):
            self.prepare()

    def test_answers_wait_for_the_review_they_answer_to_reach_the_pr(self):
        rec = self.dispute('upheld', {'body': 'Still stands.', 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'comments': [], 'replies': []}), reason='again')
        self.gh.post_fails = True
        self.post('--auto')
        self.gh.post_fails = False
        with self.assertRaisesRegex(facts.Unusable, 'not on the PR yet'):
            self.post()

    def test_a_published_answer_settles_the_pushback_before_it(self):
        self.discuss('upheld', {'body': 'Still stands.', 'resolve': False})
        led = self.led()
        led['reviews'][0]['answerPublications'][-1]['receipts']['staged'] = [{'state': 'published', 'replyId': 960}]
        Path(self.p['ledger']).write_text(json.dumps(led))
        got = self.disputes(self.snapshot((950, 'contrib', 'intentional', False), (960, ME, 'Still stands.', False),
                                          (970, 'contrib', 'still intentional', False)))
        self.assertEqual([r['id'] for r in got[0]['replies']], [970])

    def test_an_answer_goes_into_a_pending_review_and_its_thread_resolves_once_submitted(self):
        reply = self.concede()
        sent = self.gh.posts[-1]
        self.assertEqual(sent, {'commit_id': self.head, 'comments': [],
                                'body': f"<!-- agentrc-pr-review:{self.head}:{self.pub()['draft']['digest']} -->"})
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

    def test_auto_keeps_one_answer_publication_across_a_crash_and_publishes_it_once_its_review_is_submitted(self):
        rec = self.dispute('upheld', {'body': 'Still stands.', 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'comments': [], 'replies': []}), reason='again')
        self.crash_at('POST')
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto')
        out = self.post('--auto')
        self.assertEqual((out['status'], out['review']['recovered'], 'answers' in out), ('drafted', True, False), 'handed over; no answers yet')
        self.gh.submit(self.led()['reviews'][1]['receipts']['review']['reviewId'])
        out = self.post()
        self.assertEqual(out['status'], 'drafted')
        self.assertEqual(len(self.led()['reviews'][1]['answerPublications']), 1)
        self.assertEqual([p.get('event') for p in self.gh.posts], [None, None, None])
        self.assertEqual([e for _, e in self.gh.submits], ['COMMENT'], 'the answers are never submitted')
        self.assertEqual(self.led()['reviews'][1]['findings'][0]['status'], 'upheld')
        self.post()
        self.assertEqual(len(self.gh.posts), 3, 'a rerun finds the answers\' pending review, never a second one')


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


class ResolveDue(unittest.TestCase):
    """ledger.resolve_due over reviews on the PR and a snapshot with our thread open."""
    SNAP = {'threads': [{'threadId': 'T', 'resolved': False, 'commentIds': [9, 10]}]}

    def due(self, *reviews):
        led = {'reviews': [{'status': 'posted', 'receipts': rec, 'findings': [{'id': 'f1', 'commentId': 9, 'status': st}]}
                           for st, rec in reviews]}
        return ledger.resolve_due(led, self.SNAP)

    def test_what_closed_a_finding_before_it_stood_again_no_longer_counts(self):
        closed = {'staged': [{'kind': 'fixnote', 'findingId': 'f1', 'commentId': 9, 'replyId': 10, 'state': 'published', 'resolve': True}],
                  'resolved': [{'findingId': 'f1', 'state': 'resolved'}]}
        self.assertEqual(self.due(('fixed', closed), ('upheld', {}), ('fixed', {})), ({'f1': {'replied': False}}, []))

    def test_a_settled_finding_our_published_reply_closes_is_due_though_not_fixed(self):
        conceded = {'staged': [{'kind': 'answer', 'findingId': 'f1', 'commentId': 9, 'replyId': 10, 'state': 'published', 'resolve': True}],
                    'resolved': [{'findingId': 'f1', 'state': 'deferred'}]}
        self.assertEqual(self.due(('withdrawn', conceded)), ({'f1': {'replied': True}}, []))
        conceded['staged'][0]['replyId'] = 11
        self.assertEqual(self.due(('withdrawn', conceded)), ({}, []), 'our reply was deleted since: nothing closes it')
        self.assertEqual(self.due(('withdrawn', {})), ({}, []), 'no reply of ours: never resolved bare')


class Result(unittest.TestCase):
    def test_counts_never_bodies(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'result': {'status': 'reviewed', 'pr': PR, 'head': 'a' * 40, 'mode': 'full',
                                  'verdict': {'event': 'REQUEST_CHANGES', 'reasons': ['r']},
                                  'findings': [{'status': 'open', 'severity': 'high'}, {'status': 'covered', 'severity': 'low'}],
                                  'claims': [{'verdict': 'refuted'}], 'coverage': {'dropped': [1], 'unverified': [], 'unjudged': []},
                                  'ci': {'state': 'green'}, 'hil': None, 'heldThreads': [{'findingId': 'pr7-f1', 'why': 'resolve unconfirmed'}],
                                  'draft': {'body': 'secret words', 'comments': [{}], 'replies': []}}}, f)
        out = result.collect(['--output', f.name])
        self.assertEqual((out['event'], out['findings'], out['openBySeverity'], out['claims'], out['coverage']['dropped']),
                         ('REQUEST_CHANGES', {'open': 1, 'covered': 1}, {'P1 high': 1}, {'refuted': 1}, 1))
        self.assertNotIn('secret words', json.dumps(out))
        self.assertEqual(out['heldThreads'], [{'findingId': 'pr7-f1', 'why': 'resolve unconfirmed'}])
        Path(f.name).write_text('')
        self.assertEqual(result.collect(['--output', f.name])['status'], 'no-result')


class Severity(unittest.TestCase):
    """The one scale: no other word read, reports ordered by it, every copy of it the same."""

    def led(self, *findings):
        rev = {'status': 'posted', 'head': 'a' * 40, 'mergeBase': 'b' * 40, 'mode': 'full', 'reviewedAt': 't',
               'verdict': {'event': 'COMMENT'}, 'findings': [{'status': 'open', 'file': 'src/a.c', 'line': 1, 'why': 'w', **f} for f in findings]}
        return {'v': ledger.VERSION, 'repo': REPO, 'pr': PR, 'reviews': [rev]}

    def test_a_word_off_the_scale_is_refused(self):
        for word in ('major', 'severe'):
            with self.assertRaisesRegex(facts.Unusable, f"severity '{word}' is on no known scale"):
                ledger.show(self.led({'id': 'f1', 'severity': word}))

    def test_fixed_and_withdrawn_ids_stay_on_their_record_and_are_never_recycled(self):
        led = self.led({'id': f'pr{PR}-f1', 'status': 'fixed', 'severity': 'high'}, {'id': f'pr{PR}-f2', 'status': 'withdrawn', 'severity': 'low'},
                       {'id': f'pr{PR}-f3', 'severity': 'medium'})
        self.assertEqual([f['id'] for f in ledger.show(led)['open']], [f'pr{PR}-f3'])
        again = {'status': 'open', 'file': 'src/a.c', 'line': 1, 'why': 'w', 'severity': 'high'}
        out = ledger.number(led, {'findings': [{'id': f'pr{PR}-f3', 'status': 'open'}, again]})
        self.assertEqual([f['id'] for f in out], [f'pr{PR}-f3', f'pr{PR}-f4'], 'a closed id is never recycled')
        self.assertEqual({f['id']: f['status'] for f in led['reviews'][0]['findings']},
                         {f'pr{PR}-f1': 'fixed', f'pr{PR}-f2': 'withdrawn', f'pr{PR}-f3': 'open'})

    def test_reports_order_by_severity_then_confidence_then_place_and_ids_never_move(self):
        led = self.led({'id': 'pr7-f1', 'severity': 'low', 'confidence': 'high'}, {'id': 'pr7-f2', 'severity': 'high', 'confidence': 'low'},
                       {'id': 'pr7-f3', 'severity': 'high', 'confidence': 'high', 'line': 9}, {'id': 'pr7-f4', 'severity': 'high', 'confidence': 'high', 'line': 2},
                       {'id': 'pr7-f5', 'severity': 'nit'})
        self.assertEqual([f['id'] for f in ledger.show(led)['open']], ['pr7-f4', 'pr7-f3', 'pr7-f2', 'pr7-f1', 'pr7-f5'])
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            self.addCleanup(os.unlink, f.name)
            json.dump({'result': {'status': 'reviewed', 'pr': PR, 'head': 'a' * 40, 'findings': [
                {'status': 'open', 'severity': s} for s in ('nit', 'high', 'critical', 'high', 'medium')]}}, f)
        self.assertEqual(list(result.collect(['--output', f.name])['openBySeverity'].items()),
                         [('P0 critical', 1), ('P1 high', 2), ('P2 medium', 1), ('P4 nit', 1)])

    def test_every_copy_of_the_scale_is_the_one_in_finding_verifier(self):
        role = (ROOT / 'agents' / 'finding-verifier.md').read_text()
        self.assertEqual(tuple(re.findall(r'^\| `(\w+)` \|', role, re.M)), ledger.LEVELS)
        facts_ = re.findall(r'^- `(\w+)`: ', role.split('## Severity')[1], re.M)
        self.assertEqual(facts_, ['consequence', 'path', 'variants', 'recovery'])
        for wf in ('code-audit.js', 'pr-review.js'):
            src = (ROOT / 'workflows' / wf).read_text()
            for const, want in (('LEVELS', ledger.LEVELS), ('CONFIDENCE', ledger.CONFIDENCE)):
                got = re.search(rf"^const {const} = \[([^\]]*)\]", src, re.M).group(1)
                self.assertEqual(tuple(re.findall(r"'(\w+)'", got)), want, f'{wf} {const}')
            got = re.search(r"^const IMPACT = \{.*?required: \[([^\]]*)\]", src, re.M | re.S).group(1)
            self.assertEqual(re.findall(r"'(\w+)'", got), facts_, f'{wf} IMPACT')


class Workflow(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_stub_harness_passes(self):
        done = subprocess.run(['node', str(ROOT / 'tests' / 'pr_review_harness.mjs')], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
