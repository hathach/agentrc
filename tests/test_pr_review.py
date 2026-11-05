"""Tests for pr-review's scripts: real git in temporary repositories, a fake GitHub behind gh."""
import contextlib
import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, make_stub, remotes_checkout, tmpdir  # noqa: E402

REPO, PR, ME = 'o/r', 7, 'reviewer'
REAL_RUN = facts.run


def sh(cwd, *argv):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True, env=git_env()).stdout.strip()


# GitHub's own refusals of a review POST, answered before it creates anything.
REFUSALS = {'refused': 'gh: API rate limit exceeded for user ID 1. (HTTP 403)',
            'invalid': 'gh: Validation Failed: {"resource":"PullRequestReview","code":"custom"} (HTTP 422)',
            'secondary': 'gh: You have exceeded a secondary rate limit. (HTTP 429)'}


def setUpModule():
    guard_survivors(__name__)


class FakeGitHub:
    def __init__(self):
        self.head = None
        self.state = 'OPEN'
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
        self.notes = []
        self.note_fails = None
        self.clock = 0
        self.maintainer_can_modify = True
        self.perms = {}  # repo: this account's permissions, or a str for gh's error

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
            if fields == 'headRefOid,state':
                return 0, json.dumps({'headRefOid': self.head, 'state': self.state})
            return 0, json.dumps({'number': PR, 'url': f'https://github.com/{REPO}/pull/{PR}', 'title': 't',
                                  'state': 'OPEN', 'author': {'login': 'contrib'}, 'headRefOid': self.head,
                                  'headRefName': 'feature', 'headRepository': {'name': 'r'},
                                  'headRepositoryOwner': {'login': 'contrib'}, 'isCrossRepository': True,
                                  'maintainerCanModify': self.maintainer_can_modify, 'baseRefName': 'main'})
        if argv[:2] == ['pr', 'checks']:
            return self.checks_code, json.dumps(self.checks) if self.checks else ''
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
        if argv[0] == 'api' and '--method' in argv and argv[argv.index('--method') + 2].endswith('/replies'):
            # A standalone reply: public at once, on its root's thread.
            root = int(argv[argv.index('--method') + 2].split('/')[-2])
            self.notes.append((root, json.loads(stdin)['body']))
            if self.note_fails is True:
                return 1, '', 'HTTP 502'
            cid = self.next_id = self.next_id + 1
            self.inline.append({'id': cid, 'in_reply_to_id': root, 'body': json.loads(stdin)['body'], 'user': {'login': ME},
                                'created_at': self.tick()})
            next(t for t in self.threads if t['comments']['nodes'][0]['databaseId'] == root)['comments']['nodes'].append({'databaseId': cid})
            if self.note_fails == 'lost':
                return 1, '', 'HTTP 502'
            return 0, json.dumps({'id': cid})
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
            if path.count('/') == 2 and '--jq' in argv and argv[argv.index('--jq') + 1] == '.permissions':
                got = self.perms.get(path[len('repos/'):])
                return (1, '', got) if isinstance(got, str) else (0, json.dumps(got))
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
        self.tmp = Path(tempfile.mkdtemp()).resolve()  # git reports the long name, not a runner's 8.3 TEMP (RUNNER~1)
        self.remote = self.tmp / 'o' / 'r.git'
        self.remote.parent.mkdir()
        sh(self.tmp, 'git', 'init', '--quiet', '--bare', '-b', 'main', str(self.remote))
        self.work = self.tmp / 'work'
        sh(self.tmp, 'git', 'clone', '--quiet', self.remote.as_posix(), str(self.work))
        for k, v in (('user.name', 't'), ('user.email', 't@t'), ('commit.gpgsign', 'false')):
            sh(self.work, 'git', 'config', k, v)
        self.env = mock.patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false'})
        self.env.start()
        self.commit('src/core/a.c', 'int a;\n' * 20, 'base')
        sh(self.work, 'git', 'push', '--quiet', 'origin', 'HEAD:main')
        self.base = sh(self.work, 'git', 'rev-parse', 'HEAD')
        self.clone = self.tmp / 'clone'
        sh(self.tmp, 'git', 'clone', '--quiet', self.remote.as_posix(), str(self.clone))
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
        patches = [mock.patch.object(m, 'run', self.fake_run) for m in (facts, harvest, prepare)]
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



class CallerGitLocation(unittest.TestCase):
    def test_a_caller_exporting_git_dir_keeps_its_repository_untouched(self):
        """A run with GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE exported, as a hook or a validator exports them, commits
        and configures only the fixture's repositories (#337)."""
        sentinel = tmpdir(self)
        sh(sentinel, 'git', 'init', '--quiet', '-b', 'main')
        sh(sentinel, 'git', 'commit', '--quiet', '--allow-empty', '-m', 'sentinel')
        state = lambda: (sh(sentinel, 'git', 'for-each-ref'), (sentinel / '.git' / 'config').read_bytes(),
                         sorted(p.name for p in (sentinel / '.git').iterdir()))
        before = state()
        env = {**os.environ, 'GIT_DIR': str(sentinel / '.git'), 'GIT_WORK_TREE': str(sentinel),
               'GIT_INDEX_FILE': str(sentinel / '.git' / 'index')}
        done = subprocess.run([sys.executable, '-m', 'unittest',
                               'test_pr_review.Ledger.test_save_refuses_a_word_off_the_scale_before_anything_is_stored'],
                              cwd=Path(__file__).parent, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(state(), before)


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

    def linked(self):
        """Make the clone a linked worktree, on main, of a primary checkout beside it."""
        primary = self.tmp / 'primary'
        os.chdir(self.tmp)
        self.clone.rename(primary)
        sh(primary, 'git', 'checkout', '--quiet', '--detach')
        sh(primary, 'git', 'worktree', 'add', '--quiet', str(self.clone), 'main')
        os.chdir(self.clone)

    def test_a_linked_checkout_gets_its_review_worktree_beside_it(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        head = self.push_pr()
        sibling = self.tmp / f'clone--pr-review-{PR}'
        self.linked()
        p = self.prepare()
        self.assertEqual(p['worktree'], sibling.as_posix())
        self.assertEqual(sh(sibling, 'git', 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.clone / '.worktrees').exists())
        token = p['lock']['token']
        with mock.patch.object(prepare, 'beside', side_effect=AssertionError('placed again')):
            q = self.prepare('--token', token)  # an existing tree is reused, under the lock of the run that prepared it
        self.assertEqual((q['worktree'], q['lock']), (sibling.as_posix(), {'state': 'held', 'token': token}))
        Path(sibling, 'src/core/a.c').write_text('local edit\n')
        with self.assertRaisesRegex(facts.Unusable, 'uncommitted changes'):
            self.prepare('--token', token)
        sh(sibling, 'git', 'checkout', '--', '.')
        sh(self.clone, 'git', 'worktree', 'add', '--quiet', '--detach', str(self.clone / '.worktrees' / f'pr-review-{PR}'))
        with self.assertRaisesRegex(facts.Unusable, 'both .* exist'):
            self.prepare('--token', token)

    def test_a_review_worktree_nests_in_a_primary_checkout_and_refuses_when_git_cannot_say(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        nested = (self.clone / '.worktrees' / f'pr-review-{PR}').as_posix()
        self.assertEqual(self.prepare()['worktree'], nested)
        sh(self.clone, 'git', 'worktree', 'remove', nested)
        real, missing = prepare.git, (self.tmp / 'missing').as_posix()
        for answer, message in ((None, f'git -C {re.escape(missing)} rev-parse .*--absolute-git-dir'),
                                (f'{self.tmp}\n', 'cannot tell where the review worktree goes'),
                                ('relative\nother\n', 'cannot tell where the review worktree goes'),
                                (f'{self.tmp}\n\n', 'cannot tell where the review worktree goes')):
            def git(*argv):
                if '--absolute-git-dir' not in argv:
                    return real(*argv)
                return real('-C', missing, *argv[2:]) if answer is None else answer
            with self.subTest(answer=answer), mock.patch.object(prepare, 'git', git), \
                    self.assertRaisesRegex(facts.Unusable, message):
                self.prepare()
        self.assertFalse(Path(nested).exists())
        self.assertFalse((self.tmp / f'clone--pr-review-{PR}').exists())
        self.linked()
        with mock.patch.object(prepare, 'beside', return_value=False):  # placed before the checkout was linked
            self.assertEqual(self.prepare()['worktree'], nested)
        self.assertEqual(self.prepare()['worktree'], nested, 'a nested tree from before is reused where it is')

    def test_herdr_on_path_or_in_the_environment_never_changes_where_a_review_worktree_goes(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        bin_dir, log = self.tmp / 'bin', self.tmp / 'herdr.log'
        bin_dir.mkdir()
        stub = make_stub(bin_dir, 'herdr', f'import sys\nopen({str(log)!r}, "a").write("called\\n")\nsys.exit(1)\n')
        os.environ.update(HERDR_ENV='1', HERDR_BIN_PATH=str(stub), PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
        p = self.prepare()
        self.assertEqual(p['worktree'], (self.clone / '.worktrees' / f'pr-review-{PR}').as_posix(), 'a primary nests')
        sh(self.clone, 'git', 'worktree', 'remove', p['worktree'])
        self.linked()
        self.assertEqual(self.prepare()['worktree'], (self.tmp / f'clone--pr-review-{PR}').as_posix(),
                         'a linked checkout gets it beside')
        self.assertFalse(log.exists(), 'herdr is never run')

    def release(self, token):
        return self.call(prepare, ['--release', '--pr', str(PR), '--repo', REPO, '--token', token])

    def reason(self, tree):
        return prepare.locks().get(os.path.realpath(tree))

    def sibling_run(self):
        """A pinned PR whose next prepare places its tree beside the checkout."""
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        self.linked()

    def test_a_sibling_review_tree_is_locked_for_its_run_and_a_nested_one_is_not(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        p = self.prepare()
        self.assertEqual(p['lock'], {'state': 'none', 'token': None})
        self.assertEqual(prepare.locks(), {})
        sh(self.clone, 'git', 'worktree', 'remove', p['worktree'])
        self.linked()
        q = self.prepare()
        token = q['lock']['token']
        self.assertEqual(q['lock']['state'], 'taken')
        self.assertRegex(token, r'^[0-9a-f]{16}$')
        self.assertEqual(self.reason(q['worktree']), f'pr-review {PR} running {token}')

    def test_another_run_holding_the_sibling_refuses_before_anything_is_checked_out(self):
        self.sibling_run()
        a = self.prepare()  # its launcher killed: nothing saved or published, the lock stays
        token = a['lock']['token']
        self.commit('src/core/a.c', 'int c;\n', 'c')
        self.push_pr()
        with self.assertRaisesRegex(facts.Unusable, f'locked by pr-review run {token} of PR {PR}: once you know that run '
                                    f'ended, release it with prepare.py --release --pr {PR} --token {token}'):
            self.prepare()
        self.assertEqual(sh(a['worktree'], 'git', 'rev-parse', 'HEAD'), a['head'])
        self.assertEqual(sh(self.clone, 'git', 'rev-parse', f'pr-review-{PR}'), a['head'])
        self.assertEqual(self.reason(a['worktree']), f'pr-review {PR} running {token}')

    def test_release_unlocks_only_the_runs_own_lock_once_and_a_stale_release_is_refused(self):
        self.sibling_run()
        a = self.prepare()
        old, tree = a['lock']['token'], Path(a['worktree']).resolve().as_posix()
        self.assertEqual(self.release(old), {'pr': PR, 'token': old, 'released': [tree]})
        self.assertIsNone(self.reason(tree))
        self.assertEqual(self.release(old)['released'], [])
        new = self.prepare()['lock']['token']
        self.assertNotEqual(new, old)
        with self.assertRaisesRegex(facts.Unusable, f'another pr-review run of PR {PR} holds .*: nothing released'):
            self.release(old)
        self.assertEqual(self.reason(tree), f'pr-review {PR} running {new}')
        self.assertEqual(self.release(new)['released'], [tree])

    def test_a_foreign_lock_is_left_by_prepare_and_release(self):
        self.sibling_run()
        p = self.prepare()
        token = p['lock']['token']
        self.release(token)
        for foreign in ('kept by hand', f'pr-review {PR + 1} running {token}'):
            sh(self.clone, 'git', 'worktree', 'lock', '--reason', foreign, p['worktree'])
            self.assertEqual(self.prepare()['lock'], {'state': 'foreign', 'token': None})
            self.assertEqual(self.release(token)['released'], [])
            self.assertEqual(self.reason(p['worktree']), foreign)
            sh(self.clone, 'git', 'worktree', 'unlock', p['worktree'])

    def test_release_refuses_a_tree_that_is_not_the_prs_review_tree_and_a_missing_or_malformed_token(self):
        other = self.tmp / 'clone--elsewhere'
        sh(self.clone, 'git', 'worktree', 'add', '--quiet', '--detach', str(other))
        sh(self.clone, 'git', 'worktree', 'lock', '--reason', f'pr-review {PR} running abc123', str(other))
        with self.assertRaisesRegex(facts.Unusable, f"carries run abc123's lock but is not a pr-review-{PR} tree"):
            self.release('abc123')
        self.assertEqual(self.reason(other), f'pr-review {PR} running abc123')
        with self.assertRaisesRegex(facts.Unusable, '--release needs --token'):
            self.call(prepare, ['--release', '--pr', str(PR), '--repo', REPO])
        with self.assertRaisesRegex(facts.Unusable, '--token must be the hex token'):
            self.release('abc 123')

    def test_a_symlinked_review_tree_is_refused_untouched(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        launcher = (sh(self.clone, 'git', 'branch', '--show-current'), sh(self.clone, 'git', 'rev-parse', 'HEAD'))
        sibling = self.tmp / f'clone--pr-review-{PR}'
        try:
            sibling.symlink_to(self.clone, target_is_directory=True)  # the launcher's HEAD, main at the base, is a pinned commit
        except OSError as e:  # Windows without Developer Mode
            self.skipTest(f'cannot create a symlink: {e}')
        with self.assertRaisesRegex(facts.Unusable, 'is a symlink'):
            self.prepare()
        self.assertEqual((sh(self.clone, 'git', 'branch', '--show-current'), sh(self.clone, 'git', 'rev-parse', 'HEAD')),
                         launcher)

    def test_an_existing_tree_that_is_not_our_linked_worktree_is_refused_untouched(self):
        self.commit('src/core/a.c', 'int b;\n', 'b')
        self.push_pr()
        launcher = (sh(self.clone, 'git', 'branch', '--show-current'), sh(self.clone, 'git', 'rev-parse', 'HEAD'))
        sibling = self.tmp / f'clone--pr-review-{PR}'
        sh(self.tmp, 'git', 'clone', '--quiet', self.remote.as_posix(), str(sibling))
        with self.assertRaisesRegex(facts.Unusable, 'not a linked worktree of this repository'):
            self.prepare()
        subprocess.run(['rm', '-rf', str(sibling)])
        (self.clone / '.worktrees' / f'pr-review-{PR}').mkdir(parents=True)
        with self.assertRaisesRegex(facts.Unusable, 'not a linked worktree of this repository'):
            self.prepare()
        self.assertEqual((sh(self.clone, 'git', 'branch', '--show-current'), sh(self.clone, 'git', 'rev-parse', 'HEAD')),
                         launcher)

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
        want = {'mergeBase': p['mergeBase'], 'scopeBase': p['scopeBase'], 'mode': 'full', 'groups': p['groups'], 'recheckFrom': None}
        self.assertEqual(ok['pins'], want)
        pinned = json.loads(Path(p['facts']).read_text())
        del pinned['recheckFrom']
        Path(p['facts']).write_text(json.dumps(pinned))
        self.assertEqual(self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head])['pins'], want,
                         "a prepare from before --recheck-from pinned no recheck")
        self.assertEqual(ok['top'], Path(os.path.realpath(p['worktree'])).as_posix())
        self.assertEqual((ok['repo'], ok['pr']), (REPO, PR))
        with self.assertRaisesRegex(facts.Unusable, 'a full SHA'):
            self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head + '\n'])
        Path('src/core/a.c').write_text('edited\n')
        with self.assertRaisesRegex(facts.Unusable, 'uncommitted changes'):
            self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head])
        sh('.', 'git', 'checkout', '--', '.')
        self.commit('src/core/a.c', 'int c;\n', 'c')
        self.push_pr()
        with self.assertRaisesRegex(facts.Unusable, 'head moved'):
            self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head])

    def test_a_non_ascii_path_is_one_utf8_line_of_the_changed_file(self):
        self.commit('src/\u00e9t\u00e9.c', 'int a;\n', 'odd')
        self.push_pr()
        p = self.prepare()
        self.assertNotIn('\\', p['worktree'] + p['ledger'] + p['facts'] + p['changedFile'])
        self.assertEqual(Path(p['changedFile']).read_bytes(), 'src/\u00e9t\u00e9.c\n'.encode('utf-8'))

    @unittest.skipIf(os.name == 'nt', 'NTFS refuses a newline in a filename')
    def test_a_filename_with_a_newline_is_one_changed_path(self):
        self.commit('src/a\nb.c', 'int a;\n', 'odd')
        self.push_pr()
        p = self.prepare()
        self.assertEqual(json.loads(Path(p['facts']).read_text())['changed'], ['src/a\nb.c'])
        self.assertIsNone(p['changedFile'], 'the selector file cannot hold that path')
        self.assertEqual(p['groups'], ['src'])

    @unittest.skipIf(os.name == 'nt', 'NTFS refuses a carriage return in a filename')
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
        # #271: skills/cowork/scripts was scanned again under skills/cowork.
        nested = ['skills/cowork/SKILL.md', 'skills/cowork/scripts/cowork.py', 'agents/coworker.md']
        self.assertEqual(prepare.groups_of(nested, 6), (['agents', 'skills/cowork'], False))


class Recheck(Case):
    """prepare --recheck-from R: the auto-fix recheck scans only what the repair changed, a base merge's imports left out."""

    LINES = [f'int v{i};\n' for i in range(20)]

    def text(self, **edits):
        return ''.join(edits.get(f'l{i}', line) for i, line in enumerate(self.LINES))

    def setUp(self):
        super().setUp()
        self.commit('src/core/a.c', self.text(), 'lines')
        sh(self.work, 'git', 'push', '--quiet', 'origin', 'HEAD:main')
        self.base = sh(self.work, 'git', 'rev-parse', 'HEAD')
        self.commit('src/core/a.c', self.text(l5='int pr;\n'), 'pr')
        self.reviewed = self.push_pr()
        p = self.prepare()
        self.save(self.result_for(p))
        led = json.loads(Path(p['ledger']).read_text())
        led['reviews'][-1].update(status='posted', receipts={'review': {'submitted': True}})
        Path(p['ledger']).write_text(json.dumps(led))

    def on_main(self, path, text, msg):
        """A commit on the base branch, None text deleting path, the PR's own branch checked out again after it."""
        sh(self.work, 'git', 'fetch', '--quiet', 'origin')
        sh(self.work, 'git', 'checkout', '--quiet', '--detach', 'origin/main')
        if text is None:
            sh(self.work, 'git', 'rm', '--quiet', path)
            sh(self.work, 'git', 'commit', '--quiet', '-m', msg)
        else:
            self.commit(path, text, msg)
        sh(self.work, 'git', 'push', '--quiet', 'origin', 'HEAD:main')
        sh(self.work, 'git', 'checkout', '--quiet', 'main')

    def merge_main(self, *strategy):
        sh(self.work, 'git', 'fetch', '--quiet', 'origin')
        return subprocess.run(['git', 'merge', '--quiet', '--no-edit', *strategy, 'origin/main'], cwd=self.work,
                              capture_output=True, text=True, env=git_env()).returncode

    def recheck(self):
        return self.prepare('--recheck-from', self.reviewed)

    def scan(self, p):
        return sh(self.clone, 'git', 'diff', p['scopeBase'], p['head'])

    def test_a_repair_without_a_base_merge_is_incremental_from_the_reviewed_head(self):
        self.commit('src/fix/f.c', 'int f;\n', 'repair')
        self.push_pr()
        p = self.recheck()
        self.assertEqual((p['mode'], p['scopeBase'], p['priorHead'], p['recheckFrom']),
                         ('incremental', self.reviewed, self.reviewed, self.reviewed))
        self.assertEqual(p['groups'], ['src/fix'])

    def test_an_unmoved_head_is_the_same_head_and_an_unmerged_base_is_no_merge(self):
        self.assertEqual(self.recheck()['mode'], 'same')
        self.commit('src/fix/f.c', 'int f;\n', 'repair')
        self.push_pr()
        self.on_main('src/usb/m.c', 'int main_only;\n', 'main, not merged')
        p = self.recheck()
        self.assertEqual((p['mode'], p['scopeBase'], p['mergeBase']), ('incremental', self.reviewed, self.base))

    def test_a_base_merge_leaves_its_imports_out_and_keeps_the_repair_and_a_later_fix(self):
        self.commit('src/fix/f.c', 'int f;\n', 'repair')
        self.on_main('src/usb/m.c', 'int main_only;\n', 'main: new file')
        self.on_main('src/core/a.c', self.text(l15='int main_far;\n'), 'main: same file, far from the PR')
        self.assertEqual(self.merge_main(), 0)
        self.commit('src/ci/c.c', 'int ci;\n', 'pr-babysit fix')
        head = self.push_pr()
        before = (sh(self.clone, 'git', 'rev-parse', 'HEAD'), sh(self.clone, 'git', 'status', '--porcelain'))
        p = self.recheck()
        self.assertEqual((p['mode'], p['priorHead'], p['recheckFrom']), ('incremental', self.reviewed, self.reviewed))
        self.assertNotIn(p['scopeBase'], (self.reviewed, p['mergeBase']))
        self.assertEqual(sh(self.clone, 'git', 'diff', '--name-only', p['scopeBase'], head).split(), ['src/ci/c.c', 'src/fix/f.c'])
        self.assertEqual(p['groups'], ['src/ci', 'src/fix'])
        self.assertEqual(p['conflicts'], [])
        self.assertEqual(sh(self.clone, 'git', 'log', '--format=%s', f"{p['scopeBase']}..{head}").splitlines(),
                         ['pr-babysit fix', "Merge remote-tracking branch 'origin/main'", 'repair'])
        self.assertEqual(sh(self.clone, 'git', 'rev-parse', f'refs/pr-review-scope/{PR}/{head}'), p['scopeBase'])
        self.assertEqual(sh(p['worktree'], 'git', 'cat-file', '-t', p['scopeBase']), 'commit')
        self.assertEqual((sh(self.clone, 'git', 'rev-parse', 'HEAD'), sh(self.clone, 'git', 'status', '--porcelain')), before)
        self.assertEqual(self.recheck()['scopeBase'], p['scopeBase'], 'the same inputs give the same base')
        os.chdir(p['worktree'])
        try:
            pins = self.call(prepare, ['--check', '--pr', str(PR), '--repo', REPO, '--expected-head', head])['pins']
        finally:
            os.chdir(self.clone)
        self.assertEqual((pins['recheckFrom'], pins['scopeBase']), (self.reviewed, p['scopeBase']))
        q = self.prepare()
        self.assertEqual((q['mode'], q['recheckFrom']), ('full', None), 'an ordinary prepare after a base merge is unchanged')

    def test_two_base_merges_leave_both_imports_out(self):
        self.commit('src/fix/f.c', 'int f;\n', 'repair')
        self.on_main('src/usb/m.c', 'int one;\n', 'main 1')
        self.assertEqual(self.merge_main(), 0)
        self.commit('src/fix/g.c', 'int g;\n', 'repair 2')
        self.on_main('src/usb/n.c', 'int two;\n', 'main 2')
        self.assertEqual(self.merge_main(), 0)
        self.push_pr()
        p = self.recheck()
        self.assertEqual(sh(self.clone, 'git', 'diff', '--name-only', p['scopeBase'], p['head']).split(),
                         ['src/fix/f.c', 'src/fix/g.c'])

    def test_a_content_conflict_puts_its_resolution_in_scope_and_not_the_base_side_elsewhere(self):
        self.on_main('src/core/a.c', self.text(l5='int main5;\n', l15='int main_far;\n'), 'main: the same line')
        self.assertNotEqual(self.merge_main(), 0)
        (self.work / 'src/core/a.c').write_text(self.text(l5='int resolved;\n', l15='int main_far;\n'))
        sh(self.work, 'git', 'commit', '--quiet', '-am', 'merge main, resolved')
        self.push_pr()
        p = self.recheck()
        self.assertEqual((p['mode'], p['conflicts'], p['groups']), ('incremental', ['src/core/a.c'], ['src/core']))
        diff = self.scan(p)
        self.assertIn('+int resolved;', diff)
        self.assertNotIn('main_far', diff)

    def test_another_conflict_kind_is_refused_never_widened(self):
        self.on_main('src/core/a.c', None, 'main: delete a.c')
        self.assertNotEqual(self.merge_main(), 0)
        sh(self.work, 'git', 'add', 'src/core/a.c')
        sh(self.work, 'git', 'commit', '--quiet', '-m', 'merge main, kept a.c')
        self.push_pr()
        with self.assertRaisesRegex(facts.Unusable, r'CONFLICT \(modify/delete\).*src/core/a\.c'):
            self.recheck()

    def test_a_recheck_s_suppressed_nits_are_saved_with_its_review_and_never_stand(self):
        self.commit('src/fix/f.c', 'int f;\n', 'repair')
        self.push_pr()
        p = self.recheck()
        nit = {'file': 'src/fix/f.c', 'line': 1, 'dimension': 'correctness', 'severity': 'nit', 'why': 'naming', 'reason': 'recheck-nit'}
        self.save(self.result_for(p, recheckFrom=self.reviewed, suppressed=[nit]))
        rev = json.loads(Path(p['ledger']).read_text())['reviews'][-1]
        self.assertEqual((rev['recheckFrom'], rev['suppressed']), (self.reviewed, [nit]))
        self.assertNotIn('naming', [f['why'] for f in rev['findings']])

    def test_a_clean_merge_alone_leaves_nothing_to_scan(self):
        self.on_main('src/usb/m.c', 'int main_only;\n', 'main')
        self.assertEqual(self.merge_main(), 0)
        self.push_pr()
        p = self.recheck()
        self.assertEqual((p['mode'], p['groups'], p['scoped']), ('incremental', [], 0))

    def test_a_merge_that_drops_the_base_side_shows_it_as_scope(self):
        """Exact scope is claimed only for a default merge: what `-s ours` discarded reads as the repair's change."""
        self.on_main('src/usb/m.c', 'int main_only;\n', 'main')
        self.assertEqual(self.merge_main('-s', 'ours'), 0)
        self.push_pr()
        self.assertEqual(self.recheck()['groups'], ['src/usb'])

    def test_a_rewritten_history_is_a_full_recheck(self):
        sh(self.work, 'git', 'commit', '--quiet', '--amend', '-m', 'pr again')
        self.push_pr(force=True)
        p = self.recheck()
        self.assertEqual((p['mode'], p['scopeBase'], p['priorHead'], p['recheckFrom']),
                         ('full', p['mergeBase'], self.reviewed, self.reviewed))

    def test_the_reviewed_head_must_be_our_last_review_submitted_on_it(self):
        self.commit('src/fix/f.c', 'int f;\n', 'repair')
        head = self.push_pr()
        with self.assertRaisesRegex(facts.Unusable, 'not one submitted on'):
            self.prepare('--recheck-from', head)
        ledger_file = Path(self.recheck()['ledger'])
        led = json.loads(ledger_file.read_text())
        led['reviews'][-1]['receipts'] = {}
        ledger_file.write_text(json.dumps(led))
        with self.assertRaisesRegex(facts.Unusable, 'not one submitted on'):
            self.recheck()
        for extra in (['--full'], ['--auto-fix'], ['--check', '--expected-head', head], ['--release', '--token', 'ab']):
            with self.assertRaisesRegex(facts.Unusable, '--recheck-from'):
                self.prepare('--recheck-from', self.reviewed, *extra)
        with self.assertRaisesRegex(facts.Unusable, 'full SHA'):
            self.prepare('--recheck-from', self.reviewed[:12])


class AutoFix(Case):
    """prepare --auto-fix: where repairs would be pushed, and the review branch bound to it."""

    BRANCH = f'pr-review-{PR}'

    def setUp(self):
        super().setUp()
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.head = self.push_pr()
        self.fetched = []

    def fake_fetch(self, remote, branch, at=None):
        self.fetched.append((remote, branch))
        sh(self.clone, 'git', 'update-ref', f'refs/remotes/{remote}/{branch}', at or self.gh.head)

    def auto_fix(self, at=None):
        with mock.patch.object(prepare, 'fetch_head_branch', lambda r, b: self.fake_fetch(r, b, at)):
            return self.prepare('--auto-fix')

    def config(self, key):
        return sh(self.clone, 'git', 'config', '--default', '', '--get', key)

    def test_without_it_prepare_reports_no_repair_and_binds_nothing(self):
        sh(self.clone, 'git', 'remote', 'add', 'contrib', 'git@github.com:contrib/r.git')
        p = self.prepare()
        self.assertNotIn('repair', p)
        self.assertEqual(self.config(f'branch.{self.BRANCH}.remote'), '')

    def test_a_fork_allowing_maintainer_edits_binds_its_remote_as_babysit_preflight_reads_it(self):
        import preflight
        sh(self.clone, 'git', 'remote', 'add', 'contrib', 'git@github.com:contrib/r.git')
        self.gh.perms = {'contrib/r': {'push': False, 'pull': True}, REPO: {'push': True}}
        p = self.auto_fix()
        self.assertEqual(p['repair'], {'state': 'available', 'why': f'the PR allows edits from maintainers, and push access to {REPO}',
                                       'remote': 'contrib', 'addedRemote': False})
        self.assertEqual((p['headRepo'], p['headBranch'], p['fork']), ('contrib/r', 'feature', True))
        self.assertEqual(self.fetched, [('contrib', 'feature')])
        with contextlib.chdir(p['worktree']):
            remote, urls = preflight.push_urls(self.BRANCH)
            self.assertEqual((remote, urls, preflight.upstream_branch(self.BRANCH, remote)),
                             ('contrib', ['git@github.com:contrib/r.git'], 'feature'))
            self.assertEqual(preflight.bad_push_url(f'https://github.com/{REPO}/pull/{PR}', 'github.com/contrib/r', urls), '')
        self.assertEqual(self.auto_fix()['repair']['remote'], 'contrib', 'a second prepare finds its own binding')

    def test_push_access_to_the_head_repository_is_enough(self):
        sh(self.clone, 'git', 'remote', 'add', 'contrib', 'https://github.com/contrib/r')
        self.gh.perms = {'contrib/r': {'push': True}}
        self.gh.maintainer_can_modify = False
        r = self.auto_fix()['repair']
        self.assertEqual((r['state'], r['why'], r['remote']), ('available', 'push access to contrib/r', 'contrib'))

    def test_no_push_access_anywhere_is_unavailable_and_binds_nothing(self):
        sh(self.clone, 'git', 'remote', 'add', 'contrib', 'git@github.com:contrib/r.git')
        self.gh.perms = {'contrib/r': {'push': False}, REPO: {'push': False, 'triage': True}}
        r = self.auto_fix()['repair']
        self.assertEqual((r['state'], r['why']), ('unavailable', f'no push access to contrib/r or {REPO}'))
        self.assertNotIn('remote', r)
        self.assertEqual((self.fetched, self.config(f'branch.{self.BRANCH}.remote')), ([], ''))

    def test_a_fork_refusing_maintainer_edits_is_unavailable(self):
        self.gh.perms = {'contrib/r': {'push': False}, REPO: {'admin': True}}
        self.gh.maintainer_can_modify = False
        r = self.auto_fix()['repair']
        self.assertEqual((r['state'], r['why']), ('unavailable', 'no push access to contrib/r, and the PR does not allow edits from maintainers'))

    def test_permissions_gh_cannot_read_are_unknown_not_unavailable(self):
        self.gh.perms = {'contrib/r': 'gh: Not Found (HTTP 404)', REPO: {'push': False}}
        r = self.auto_fix()['repair']
        self.assertEqual(r['state'], 'unknown')
        self.assertIn('HTTP 404', r['why'])
        for shown in (None, {}, {'pull': True}, {'push': 'no'}):
            self.gh.perms = {'contrib/r': shown, REPO: {'push': False}}
            self.assertEqual(self.auto_fix()['repair']['state'], 'unknown', f'{shown} is not a refusal')

    def test_with_no_remote_for_the_head_one_is_added_like_the_base_and_gh_default_pinned(self):
        sh(self.clone, 'git', 'config', f'url.{self.remote.as_posix()}.insteadOf', f'git@github.com:{REPO}.git')
        sh(self.clone, 'git', 'remote', 'set-url', 'origin', f'git@github.com:{REPO}.git')
        self.gh.perms = {'contrib/r': {'push': True}}
        r = self.auto_fix()['repair']
        self.assertEqual((r['remote'], r['addedRemote']), ('pr-head-contrib', True))
        self.assertEqual(sh(self.clone, 'git', 'remote', 'get-url', 'pr-head-contrib'), 'git@github.com:contrib/r.git')
        self.assertEqual(self.config('remote.origin.gh-resolved'), 'base')
        self.assertEqual(facts.gh_repo.checkout_repo(str(self.clone)), REPO)

    def test_an_added_remote_that_leaves_the_checkout_reading_another_repository_is_removed_so_a_rerun_checks_again(self):
        sh(self.clone, 'git', 'config', f'url.{self.remote.as_posix()}.insteadOf', f'git@github.com:{REPO}.git')
        sh(self.clone, 'git', 'remote', 'set-url', 'origin', f'git@github.com:{REPO}.git')
        sh(self.clone, 'git', 'remote', 'add', 'mirror', 'https://github.com/someone/else')
        sh(self.clone, 'git', 'config', 'remote.mirror.gh-resolved', 'base')
        self.gh.perms = {'contrib/r': {'push': True}}
        for _ in range(2):
            with self.assertRaisesRegex(facts.Unusable, f'no longer reads as {REPO}'):
                self.auto_fix()
            self.assertNotIn('pr-head-contrib', sh(self.clone, 'git', 'remote').split())

    def test_a_remote_pushing_elsewhere_is_not_the_head_remote(self):
        sh(self.clone, 'git', 'remote', 'add', 'contrib', 'git@github.com:contrib/r.git')
        sh(self.clone, 'git', 'remote', 'set-url', '--push', 'contrib', 'git@github.com:someone/else.git')
        self.gh.perms = {'contrib/r': {'push': True}}
        with self.assertRaisesRegex(facts.Unusable, 'not a github.com one to spell it like'):
            self.auto_fix()

    def test_two_remotes_naming_the_head_repository_refuse_unless_one_is_tracked(self):
        for name in ('a', 'b'):
            sh(self.clone, 'git', 'remote', 'add', name, 'git@github.com:contrib/r.git')
        self.gh.perms = {'contrib/r': {'push': True}}
        with self.assertRaisesRegex(facts.Unusable, 'remotes a, b all name contrib/r'):
            self.auto_fix()
        sh(self.clone, 'git', 'config', f'branch.{self.BRANCH}.remote', 'b')
        sh(self.clone, 'git', 'config', f'branch.{self.BRANCH}.merge', 'refs/heads/feature')
        self.assertEqual(self.auto_fix()['repair']['remote'], 'b')

    def test_another_upstream_already_set_is_refused_not_replaced(self):
        sh(self.clone, 'git', 'remote', 'add', 'contrib', 'git@github.com:contrib/r.git')
        self.prepare()
        sh(self.clone, 'git', 'config', f'branch.{self.BRANCH}.remote', 'origin')
        sh(self.clone, 'git', 'config', f'branch.{self.BRANCH}.merge', 'refs/heads/main')
        self.gh.perms = {'contrib/r': {'push': True}}
        with self.assertRaisesRegex(facts.Unusable, 'already tracks origin refs/heads/main'):
            self.auto_fix()
        self.assertEqual(self.config(f'branch.{self.BRANCH}.remote'), 'origin')

    def test_a_remote_whose_fetch_refspec_skips_the_head_branch_refuses_and_binds_nothing(self):
        sh(self.clone, 'git', 'remote', 'add', '-t', 'main', 'contrib', 'git@github.com:contrib/r.git')
        self.gh.perms = {'contrib/r': {'push': True}}
        with self.assertRaisesRegex(facts.Unusable, "fetch refspec does not map refs/heads/feature"):
            self.auto_fix()
        self.assertEqual((self.config(f'branch.{self.BRANCH}.remote'), self.config(f'branch.{self.BRANCH}.merge')), ('', ''))

    def test_a_head_branch_not_at_the_pinned_head_refuses_and_binds_nothing(self):
        sh(self.clone, 'git', 'remote', 'add', 'contrib', 'git@github.com:contrib/r.git')
        self.gh.perms = {'contrib/r': {'push': True}}
        with self.assertRaisesRegex(facts.Unusable, 'not the PR head'):
            self.auto_fix(at=self.base)
        self.assertEqual(self.config(f'branch.{self.BRANCH}.remote'), '')

    def test_it_is_refused_with_check_or_release(self):
        for extra in (['--check', '--expected-head', self.head], ['--release', '--token', 'ab']):
            with self.assertRaisesRegex(facts.Unusable, 'is for a prepare'):
                self.prepare('--auto-fix', *extra)


class Ledger(Case):
    def setUp(self):
        super().setUp()
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b;\n', 'b')
        self.push_pr()
        self.p = self.prepare()

    def test_snapshot_of_an_empty_ledger_holds_nothing_standing(self):
        snap = self.tmp / 'threads.json'
        snap.write_text(json.dumps({'repo': REPO, 'pr': PR, 'comments': [], 'threads': []}))
        manifest = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap), '--head', self.p['head'],
                                      '--snapshot', str(self.tmp)])
        self.assertEqual(json.loads(Path(manifest['file']).read_bytes()), {'reviews': 0, 'last': None, 'open': [],
                                                                              'disputes': []})

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
        with mock.patch.object(ledger, 'added_lines', wraps=ledger.added_lines) as diffed:
            out = self.save(self.result_for(self.p))
        self.assertEqual((out['inline'], out['moved'], out['findings']), (1, 1, 1))
        self.assertEqual(diffed.call_count, 1, 'one diff per path, however many comments it carries')
        led = json.loads(Path(self.p['ledger']).read_text())
        rev = led['reviews'][0]
        self.assertEqual(rev['status'], 'pending')
        self.assertEqual(rev['findings'][0]['id'], f'pr{PR}-f1')
        self.assertEqual(rev['draft']['comments'][0]['findingId'], f'pr{PR}-f1')
        self.assertEqual(rev['draft']['body'], 'Summary.\n\nNot in an inline comment:\n- `src/core/a.c:1` far from the diff')
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

    def test_an_absolute_or_backslashed_comment_path_is_refused(self):
        r = self.result_for(self.p)
        r['draft']['comments'][0]['path'] = '/abs/src/core/a.c'
        with self.assertRaisesRegex(facts.Unusable, 'absolute path or a backslash'):
            self.save(r)
        led = {'pr': PR, 'reviews': []}
        for path in ('C:/abs/src/core/a.c', 'C:\\abs\\src\\core\\a.c', '\\\\srv\\share\\src\\core\\a.c',
                     '\\src\\core\\a.c', 'C:src/core/a.c', 'src\\core\\a.c'):
            r['draft']['comments'][0]['path'] = path
            with mock.patch.object(ledger.os, 'name', 'nt'), \
                    self.assertRaisesRegex(facts.Unusable, 'absolute path or a backslash', msg=path):
                ledger.save(led, r, None)

    def test_a_colon_or_backslash_is_part_of_a_posix_path(self):
        if os.name == 'nt':
            self.skipTest('Windows reads them as a drive and a separator')
        r = self.result_for(self.p)
        for path in ('a:b.c', 'src\\core\\a.c'):
            r['draft']['comments'][0]['path'] = path
            self.assertTrue(self.save(r)['saved'], path)
            Path(self.p['ledger']).unlink()

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

    def test_long_text_is_auto_posted_whole(self):
        draft = self.result_for(self.p)['draft']
        long = ' '.join(['w'] * 200) + '\n- ' + 'x' * 700
        self.save(self.result_for(self.p, draft={**draft, 'comments': [{**draft['comments'][0], 'body': long}]}))
        out = self.post('--auto')
        self.assertEqual((out['status'], len(self.gh.submits)), ('posted', 1))
        self.assertNotIn('handedOver', out)
        self.assertNotIn('overLength', out)
        self.assertEqual(self.gh.posts[0]['comments'][0]['body'], long, 'never cut')

    def test_a_held_review_is_never_submitted_by_a_retry_after_a_crash(self):
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'comments': [{'path': 'src/core/a.c', 'line': 1, 'body': '**high**: b.\n- only here: b is never initialised', 'finding': 0, 'firstLineUnchecked': True}]}))
        with mock.patch.object(post, 'publish_review', side_effect=RuntimeError('crash before the POST')):
            with self.assertRaises(RuntimeError):
                self.post('--auto')
        self.assertEqual(self.gh.posts, [])
        out = self.post('--auto')
        self.assertEqual((out['status'], len(self.gh.posts), self.gh.submits), ('drafted', 1, []))
        self.assertRegex(out['handedOver'], 'first line no check verified')

    def test_a_held_review_whose_post_response_is_lost_is_recovered_and_still_handed_over(self):
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'comments': [{'path': 'src/core/a.c', 'line': 1, 'body': '**high**: b.\n- only here: b is never initialised', 'finding': 0, 'firstLineUnchecked': True}]}))
        self.gh.post_fails = True
        self.assertEqual(self.post('--auto')['status'], 'uncertain')
        self.gh.post_fails = False
        sent = self.gh.posts[0]
        rid = self.gh.next_id = self.gh.next_id + 1
        self.gh.reviews[rid] = {'id': rid, 'node_id': f'PRR_{rid}', 'body': sent['body'], 'state': 'PENDING', 'user': {'login': ME}}
        self.gh.review_comments[rid] = [{'id': 78, 'path': c['path'], 'line': None, 'drafted_line': c['line'], 'body': c['body']}
                                        for c in sent['comments']]
        out = self.post('--auto')
        self.assertEqual((out['status'], out['review']['recovered'], len(self.gh.posts), self.gh.submits), ('drafted', True, 1, []))
        self.assertRegex(out['handedOver'], 'first line no check verified', 'recovered by its marker, still handed over')

    def test_a_body_entry_posts_only_its_first_line(self):
        long = '**low**: short problem.\n- ' + 'cause ' * 90
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 1, 'body': long, 'finding': 0}]}
        self.save(self.result_for(self.p, draft=draft))
        self.assertEqual(self.review()['draft']['moved'][0]['renderedLine'], '- `src/core/a.c:1` **low**: short problem.')
        self.assertNotIn('cause', self.review()['draft']['body'], 'only the first line is posted')

    def test_a_long_body_entry_is_auto_posted_whole(self):
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 1, 'body': '**low**: ' + 'x' * 700, 'finding': 0}]}
        self.save(self.result_for(self.p, draft=draft))
        out = self.post('--auto')
        self.assertEqual((out['status'], len(self.gh.submits)), ('posted', 1))
        self.assertIn('x' * 700, self.gh.posts[0]['body'])

    def test_a_body_entry_whose_first_line_no_check_verified_is_handed_over_in_a_pending_review(self):
        unchecked = {'body': '**high**: b.\n- only here: b is never initialised', 'finding': 0, 'firstLineUnchecked': True}
        self.assertEqual(post.first_line_unchecked({'draft': {'comments': [unchecked], 'moved': []}}), [], 'an inline comment shows whole')
        draft = {**self.result_for(self.p)['draft'], 'comments': [{'path': 'src/core/a.c', 'line': 1, **unchecked}]}
        self.save(self.result_for(self.p, draft=draft))
        out = self.post('--auto')
        self.assertRegex(out['handedOver'], r'first line no check verified \(src/core/a\.c:1\)')
        self.assertEqual((out['status'], out['firstLineUnchecked'], len(self.gh.posts), self.gh.submits), ('drafted', ['src/core/a.c:1'], 1, []))

    def test_a_long_comment_moved_into_the_body_is_auto_posted(self):
        draft = self.result_for(self.p)['draft']
        self.save(self.result_for(self.p, draft={**draft, 'comments': [draft['comments'][0], {**draft['comments'][1], 'body': ' '.join(['w'] * 81)}]}))
        self.assertEqual([c['line'] for c in self.review()['draft']['moved']], [1], 'moved into the body, kept as the comment it was')
        self.assertEqual((self.post('--auto')['status'], len(self.gh.submits)), ('posted', 1))

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

    def test_a_held_review_is_handed_over_with_its_fix_notes_staged(self):
        self.fixed()
        led = json.loads(Path(self.p['ledger']).read_text())
        draft = led['reviews'][-1]['draft']
        draft['replies'][0]['body'] = ' '.join(['w'] * 61)
        draft['moved'] = [{'path': 'src/core/a.c', 'line': 1, 'body': '**high**: b.', 'finding': 0, 'firstLineUnchecked': True,
                           'renderedLine': '- `src/core/a.c:1` **high**: b.'}]
        Path(self.p['ledger']).write_text(json.dumps(led))
        submits = list(self.gh.submits)
        out = self.post('--auto')
        self.assertRegex(out['handedOver'], r'first line no check verified \(src/core/a\.c:1\)')
        self.assertEqual(([(e['kind'], e['state']) for e in out['staged']], self.gh.submits), ([('fixnote', 'staged')], submits),
                         'the human gets the whole review, its notes included, and nothing is submitted')
        self.assertEqual(self.adds()[0]['b'], ' '.join(['w'] * 61), 'never cut')
        self.assertFalse(self.gh.threads[0]['isResolved'])

    def test_a_long_fix_note_is_auto_posted_whole(self):
        self.fixed()
        led = json.loads(Path(self.p['ledger']).read_text())
        note = ' '.join(['w'] * 100) + ' ' + 'x' * 400
        led['reviews'][-1]['draft']['replies'][0]['body'] = note
        Path(self.p['ledger']).write_text(json.dumps(led))
        submits = len(self.gh.submits)
        out = self.post('--auto')
        self.assertEqual((out['status'], len(self.gh.submits)), ('posted', submits + 1))
        self.assertNotIn('handedOver', out)
        self.assertEqual([(e['kind'], e['state']) for e in out['staged']], [('fixnote', 'published')])
        self.assertEqual(self.adds()[0]['b'], note, 'sent whole')

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
        comments = [{'kind': 'review', 'id': self.root, 'author': ME, 'bot': False, 'digest': 'r0', 'body': 'root', 'url': 'https://x/r0'}]
        comments += [{'kind': 'review', 'id': i, 'author': who, 'bot': bot, 'digest': harvest.digest(body), 'body': body}
                     for i, who, body, bot in thread]
        f = Path(self.p['facts']).parent / f'threads-{self.head}.json'
        f.write_text(json.dumps({'repo': REPO, 'pr': PR, 'viewer': ME, 'comments': comments,
                                 'threads': [{'threadId': 'T', 'resolved': False, 'outdated': False, 'commentIds': [c['id'] for c in comments]}]}))
        return f

    def disputes(self, snap):
        return self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap), '--head', self.head])['disputes']

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

    def test_show_with_a_head_carries_the_disputes_of_that_snapshot(self):
        snap = self.snapshot((950, 'contrib', 'intentional', False))
        show = ['show', '--pr', str(PR), '--repo', REPO]
        shown = self.call(ledger, [*show, '--threads', str(snap), '--head', self.head])
        self.assertEqual([(d['findingId'], [r['id'] for r in d['replies']]) for d in shown['disputes']], [('pr7-f1', [950])])
        self.assertEqual({k: v for k, v in shown.items() if k != 'disputes'}, self.call(ledger, [*show, '--threads', str(snap)]))
        with self.assertRaisesRegex(facts.Unusable, 'full SHA'):
            self.call(ledger, [*show, '--threads', str(snap), '--head', self.head[:12]])
        with self.assertRaisesRegex(facts.Unusable, 'needs --threads'):
            self.call(ledger, [*show, '--head', self.head])

    def test_snapshot_writes_the_workflows_view_to_a_content_named_file_and_prints_its_manifest(self):
        snap = self.snapshot((950, 'contrib', 'intentional', False))
        show = ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap), '--head', self.head]
        where = self.tmp / 'facts-snap'
        where.mkdir()
        manifest = self.call(ledger, [*show, '--snapshot', str(where)])
        payload = Path(manifest['file']).read_bytes()
        self.assertTrue(payload.isascii())
        plain = self.call(ledger, show)
        view = {k: v for k, v in plain.items() if k not in ('ledger', 'answers')}
        view['open'] = [{k: v for k, v in f.items() if k not in ('impact', 'severityReason')} for f in plain['open']]
        self.assertEqual(json.loads(payload), view, 'the plain answer less what the workflow never reads')
        self.assertEqual((len(view['open']), len(view['disputes'])), (1, 1))
        self.assertEqual(Path(manifest['file']).name, f"ledger-{hashlib.sha256(payload).hexdigest()}.json")
        self.assertEqual(manifest, {'repo': REPO, 'pr': PR, 'head': self.head, 'file': manifest['file'],
                                    'digest': facts.fnv1a(payload.decode()), 'length': len(payload)})
        Path(manifest['file']).write_bytes(payload + b' ')
        self.assertEqual(self.call(ledger, [*show, '--snapshot', str(where)]), manifest, 'a rerun names the same file')
        self.assertEqual(Path(manifest['file']).read_bytes(), payload, 'and writes it anew, so a damaged one is repaired')
        changed = self.snapshot((950, 'contrib', 'intentional, see line 3', False))
        later = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(changed), '--head', self.head,
                                   '--snapshot', str(where)])
        self.assertNotEqual(later['file'], manifest['file'])
        self.assertEqual(Path(manifest['file']).read_bytes(), payload, 'a later answer leaves the earlier file as it was')
        with self.assertRaisesRegex(facts.Unusable, 'is not a directory'):
            self.call(ledger, [*show, '--snapshot', str(where / 'missing')])
        for bad in (['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap)],
                    [*show, '--fixable']):
            with self.assertRaisesRegex(facts.Unusable, '--snapshot needs --threads and --head, and not --fixable'):
                self.call(ledger, [*bad, '--snapshot', str(where)])

    def test_a_finding_read_back_from_the_snapshot_saves_with_its_whole_text(self):
        path = Path(self.p['ledger'])
        led = json.loads(path.read_text())
        text = {'why': 'b is read before set — “é” \\ "q" ' * 40, 'severityReason': 'graded ✓ ' * 30,
                'impact': {'consequence': 'wrong data ' * 50, 'path': 'p', 'recovery': 'r', 'variants': 'v'}, 'confidence': 'high'}
        led['reviews'][0]['findings'][0].update(text)
        path.write_text(json.dumps(led))
        snap = self.snapshot((950, 'contrib', 'intentional', False))
        manifest = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap), '--head', self.head,
                                      '--snapshot', str(snap.parent)])
        row = json.loads(Path(manifest['file']).read_bytes())['open'][0]
        self.assertEqual((row['why'], row['confidence']), (text['why'], text['confidence']))
        self.assertFalse({'impact', 'severityReason'} & set(row))
        grade = {k: row[k] for k in ('severity', 'confidence')}  # the workflow's gradeOf(f), its absent members dropped
        self.save(self.result_for(self.p, mode='discussion', findings=[{'id': row['id'], 'status': 'upheld', **grade,
                                                                        'disputes': [self.dispute('upheld')]}]))
        self.assertEqual({k: self.finding()[k] for k in text}, text, 'a discussion merge keeps the stored grading')
        self.save(self.result_for(self.p, findings=[{'id': row['id'], 'file': row['file'], 'line': row['line'], 'status': 'open', **grade}]),
                  reason='again')
        saved = self.led()['reviews'][-1]['findings'][0]
        self.assertEqual({k: saved[k] for k in text}, text, 'number() carries the record, its grading included')
        regrade = {'severity': 'medium', 'confidence': 'low', 'severityReason': 'worse', 'impact': {**text['impact'], 'path': 'q'}}
        self.mark_posted(self.p)
        self.save(self.result_for(self.p, findings=[{'id': row['id'], 'file': row['file'], 'line': row['line'], 'status': 'open', **regrade}]),
                  reason='regraded')
        saved = self.led()['reviews'][-1]['findings'][0]
        self.assertEqual({k: saved[k] for k in regrade}, regrade, 'a regrade replaces it')

    def test_a_snapshot_of_another_repository_or_none_named_is_refused(self):
        snap = self.snapshot((950, 'contrib', 'intentional', False))
        self.assertEqual(len(self.disputes(snap)), 1)
        good = json.loads(snap.read_text())
        for wrong in ({**good, 'repo': 'other/r'}, {k: v for k, v in good.items() if k != 'repo'}, {**good, 'pr': PR + 1}):
            snap.write_text(json.dumps(wrong))
            for head in ([], ['--head', self.head]):
                with self.subTest(repo=wrong.get('repo'), pr=wrong['pr'], head=bool(head)):
                    with self.assertRaisesRegex(facts.Unusable, f'--threads is a snapshot of .*, not {REPO}#{PR}'):
                        self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap), *head])

    def test_a_long_answer_goes_into_its_pending_review_whole(self):
        self.discuss('upheld', {'body': ' '.join(['w'] * 61), 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        out = self.post()
        self.assertEqual(out['status'], 'drafted')
        self.assertNotIn('overLength', out)
        self.assertEqual(self.adds()[-1]['b'], ' '.join(['w'] * 61))

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
        staged = {'kind': 'answer', 'findingId': 'pr7-f1', 'commentId': self.root, 'resolve': False, 'state': 'published', 'replyId': 960}
        led['reviews'][0]['answerPublications'][-1]['receipts']['staged'] = [staged]
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

    def test_a_submitted_answer_on_a_closed_pr_is_recorded_and_never_resolves(self):
        self.concede()
        self.gh.submit(self.rid())
        self.gh.state = 'CLOSED'
        self.call(post, ['--pr', str(PR), '--repo', REPO, '--sync'])
        self.assertEqual(self.finding()['disputes'][-1]['answer']['outcome'], 'published', 'the receipt still settles')
        self.assertFalse(self.gh.threads[0]['isResolved'])
        self.assertEqual([(m['state'], m['error']) for m in self.pub()['receipts']['resolved']], [('deferred', 'the PR is closed')])

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

    def test_auto_posts_with_a_long_answer_and_puts_it_whole_in_the_answers_pending_review(self):
        rec = self.dispute('upheld', {'body': ' '.join(['w'] * 61), 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'comments': [], 'replies': []}), reason='again')
        out = self.post('--auto')
        self.assertEqual((out['status'], out['answers']['status']), ('posted', 'drafted'))
        self.assertNotIn('overLength', out['answers'])
        self.assertEqual(self.adds()[-1]['b'], ' '.join(['w'] * 61))

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

    def test_auto_fix_defers_the_answers_so_the_repair_may_list_its_findings_then_plain_post_publishes_them(self):
        rec = self.dispute('upheld', {'body': 'Still stands.', 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]},
                                                    {'file': 'src/core/a.c', 'line': 1, 'why': 'a shadows a global', 'severity': 'medium',
                                                     'dimension': 'correctness', 'status': 'open'}],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'replies': [],
                                         'comments': [{'path': 'src/core/a.c', 'line': 1, 'body': 'a shadows a global', 'finding': 1}]}),
                  reason='again')
        out = self.post('--auto', '--defer-answers')
        self.assertEqual((out['status'], out['answers']), ('posted', {'status': 'deferred'}))
        self.assertEqual((len(self.gh.posts), self.adds()), (2, []), 'no pending review holds the answers')
        with mock.patch.object(post, 'collect', return_value=out), mock.patch('sys.stdout'):
            self.assertEqual(post.main([]), 0)
        snap = self.tmp / 'threads.json'
        self.call(threads, ['--pr', str(PR), '--repo', REPO, '--out', str(snap)])
        got = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--fixable', '--threads', str(snap), '--head', self.head])['fixable']
        self.assertEqual(([f['id'] for f in got['fix']], [h['why'] for h in got['held']]), (['pr7-f2'], ['replied']))
        with self.assertRaisesRegex(facts.Unusable, 'a pending draft'):
            self.prepare()
        out = self.post()
        self.assertEqual((out['status'], [(e['kind'], e['state']) for e in out['staged']]), ('drafted', [('answer', 'staged')]))
        with self.assertRaisesRegex(facts.Unusable, '--defer-answers goes with --auto'):
            self.post('--defer-answers')

    def upheld_again(self, *extra, comments=()):
        """A second review of the head, pr7-f1 upheld on the contributor's pushback with an answer, plus `extra` findings
        and their `comments`."""
        rec = self.dispute('upheld', {'body': 'Still stands.', 'resolve': False})
        self.comment(950, 'contrib', 'intentional')
        self.save(self.result_for(self.p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}, *extra],
                                  draft={'event': 'COMMENT', 'body': 'Again.', 'comments': list(comments), 'replies': []}), reason='again')

    def deferred(self):
        """Our review submitted on the head by an auto-fix run, its answer to the contributor's pushback deferred."""
        self.upheld_again({'file': 'src/core/a.c', 'line': 1, 'why': 'a shadows a global', 'severity': 'medium',
                           'dimension': 'correctness', 'status': 'open'},
                          comments=[{'path': 'src/core/a.c', 'line': 1, 'body': 'a shadows a global', 'finding': 1}])
        self.assertEqual(self.post('--auto', '--defer-answers')['answers'], {'status': 'deferred'})

    def test_auto_fix_on_the_same_head_reuses_our_submitted_review_and_lets_its_deferred_answers_wait(self):
        self.deferred()
        p = self.prepare('--auto-fix')
        rid = self.review()['receipts']['review']['reviewId']
        self.assertEqual((p['mode'], p['repair']['submittedReview'], p['repair']['deferredAnswers']), ('same', rid, 1))
        self.assertNotIn('submittedWhy', p['repair'])
        with self.assertRaisesRegex(facts.Unusable, 'a pending draft'):
            self.prepare()
        not_auto, sent, two, other = (self.led() for _ in range(4))
        not_auto['reviews'][-1]['answerPublications'][0]['auto'] = False
        sent['reviews'][-1]['answerPublications'][0]['receipts'] = {'answer': {'sent': True}}
        two['reviews'][-1]['answerPublications'].append({**two['reviews'][-1]['answerPublications'][0], 'auto': False})
        other['reviews'][0].setdefault('answerPublications', []).append(other['reviews'][-1]['answerPublications'].pop())
        for name, led in (('not auto', not_auto), ('sent', sent), ('two bundles', two), ("another review's", other)):
            Path(self.p['ledger']).write_text(json.dumps(led))
            with self.subTest(name), self.assertRaisesRegex(facts.Unusable, 'a pending draft'):
                self.prepare('--auto-fix')

    def test_a_reply_after_the_deferred_judgment_keeps_its_finding_held_and_its_stale_answer_out(self):
        self.deferred()
        self.comment(951, 'contrib', 'still intentional')
        snap = self.tmp / 'threads.json'
        self.call(threads, ['--pr', str(PR), '--repo', REPO, '--out', str(snap)])
        got = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--fixable', '--threads', str(snap), '--head', self.head])['fixable']
        self.assertEqual(([f['id'] for f in got['fix']], [(h['findingId'], h['why']) for h in got['held']]), (['pr7-f2'], [('pr7-f1', 'replied')]))
        out = self.post()
        self.assertEqual([(e['findingId'], e['state']) for e in out['staged']], [('pr7-f1', 'skipped')], 'the stale answer is never sent')
        self.gh.submit(out['review']['reviewId'])
        snap = self.snapshot((950, 'contrib', 'intentional', False), (951, 'contrib', 'still intentional', False))
        self.assertEqual([d['findingId'] for d in self.disputes(snap)], ['pr7-f1'], 'the newer reply waits for its judgment')

    def test_auto_fix_reuses_a_handed_over_review_once_the_human_submitted_it(self):
        self.upheld_again()
        self.crash_at('POST')
        with self.assertRaises(KeyboardInterrupt):
            self.post('--auto', '--defer-answers')
        out = self.post('--auto', '--defer-answers')
        self.assertEqual(out['status'], 'drafted', 'handed over')
        with self.assertRaisesRegex(facts.Unusable, 'still open on the PR'):
            self.prepare('--auto-fix')
        self.gh.submit(self.review()['receipts']['review']['reviewId'])
        p = self.prepare('--auto-fix')
        self.assertEqual((p['mode'], p['repair']['submittedReview'], p['repair']['deferredAnswers']),
                         ('same', self.review()['receipts']['review']['reviewId'], 1))
        out = self.post()
        self.assertEqual([(e['kind'], e['state']) for e in out['staged']], [('answer', 'staged')], 'plain post.py still publishes them')

    def test_auto_fix_on_the_same_head_without_a_submitted_review_names_why(self):
        self.save(self.result_for(self.p, draft={'event': 'COMMENT', 'body': 'Again.', 'comments': [], 'replies': []}), reason='again')
        self.mark_posted(self.p)
        p = self.prepare('--auto-fix')
        self.assertEqual((p['mode'], p['repair']['submittedReview']), ('same', None))
        self.assertRegex(p['repair']['submittedWhy'], 'not one submitted on')
        self.assertNotIn('deferredAnswers', p['repair'])

    def test_deferred_answers_leave_the_recheck_free_and_are_judged_again_on_the_repaired_head(self):
        self.upheld_again()
        self.assertEqual(self.post('--auto', '--defer-answers')['answers'], {'status': 'deferred'})
        self.commit('src/core/c.c', 'int c;\n', 'repair')
        self.head = self.push_pr()
        p = self.prepare()
        self.assertEqual(p['mode'], 'incremental')
        self.assertEqual([d['findingId'] for d in self.disputes(self.snapshot((950, 'contrib', 'intentional', False)))], ['pr7-f1'],
                         'the pushback is judged again on the repaired head')
        rec = self.dispute('upheld', {'body': 'Still stands after the repair.', 'resolve': False})
        self.save(self.result_for(p, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}],
                                  draft={'event': 'COMMENT', 'body': 'Recheck.', 'comments': [], 'replies': []}))
        out = self.post('--auto')
        self.assertEqual((out['status'], out['answers']['status']), ('posted', 'drafted'))
        self.assertEqual([[p['head'] == self.head, p['status']] for p, r in ledger.publications(self.led()) if p is not r],
                         [[False, 'pending'], [True, 'drafted']], 'the earlier head\'s answers are never published on this one')


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


class Lock(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = tmp / 'pr-review' / '7.json'

    def test_a_second_process_waits_for_the_holder_to_leave_the_block(self):
        holder = subprocess.Popen(
            [sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import ledger; from pathlib import Path\n'
             'with ledger.locked(Path(sys.argv[2])):\n print("held", flush=True); sys.stdin.readline()\n'
             'print("released", flush=True); sys.stdin.read()',
             str(SCRIPTS), str(self.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        got, again = threading.Event(), threading.Event()

        def take():
            with ledger.locked(self.path):
                got.set()
            with ledger.locked(self.path):
                again.set()
        t = threading.Thread(target=take, daemon=True)
        t.start()
        self.assertFalse(got.wait(0.5), 'the lock was taken while another process held it')
        holder.stdin.write('\n')
        holder.stdin.flush()
        self.assertEqual(holder.stdout.readline().strip(), 'released')
        self.assertTrue(got.wait(10), 'the lock was not taken once its holder left the block')
        self.assertTrue(again.wait(10), 'the lock was not taken again in the process that released it')
        self.assertIsNone(holder.poll(), 'the holder is still alive: leaving the block released it, not exiting')
        t.join()

    @unittest.skipUnless(os.name == 'nt', 'msvcrt locking')
    def test_a_locking_error_that_is_not_contention_is_raised(self):
        raised = []

        def take():
            try:
                with ledger.locked(self.path):
                    pass
            except OSError as e:
                raised.append(e.errno)
        with mock.patch.object(facts.msvcrt, 'locking', side_effect=OSError(errno.EBADF, 'bad file descriptor')):
            t = threading.Thread(target=take, daemon=True)
            t.start()
            t.join(5)
        self.assertEqual(raised, [errno.EBADF], 'retried as if the lock were held')


class Store(unittest.TestCase):
    def test_a_reader_holding_the_ledger_open_only_delays_the_write(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / '7.json'
        path.write_text('{}\n', encoding='utf-8')
        reader = open(path, encoding='utf-8')
        self.addCleanup(reader.close)
        later = threading.Timer(0.2, reader.close)
        later.start()
        self.addCleanup(later.join)
        ledger.store(path, {'v': 1})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'v': 1})


class RepoOf(unittest.TestCase):
    def test_without_repo_the_checkouts_one_github_repository_is_named(self):
        with contextlib.chdir(remotes_checkout(self, ('origin', 'https://github.com/o/r.git'))), \
                mock.patch.dict(os.environ, git_env(), clear=True):
            self.assertEqual(ledger.repo_of(None), 'o/r')
        with contextlib.chdir(remotes_checkout(self, ('origin', 'https://github.com/me/r.git'),
                                               ('upstream', 'https://github.com/o/r.git'))), \
                mock.patch.dict(os.environ, git_env(), clear=True), \
                self.assertRaisesRegex(ledger.Unusable, 'none is marked gh-resolved: name the repository'):
            ledger.repo_of(None)


class Result(unittest.TestCase):
    def test_counts_never_bodies(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'result': {'status': 'reviewed', 'pr': PR, 'head': 'a' * 40, 'mode': 'full',
                                  'verdict': {'event': 'REQUEST_CHANGES', 'reasons': ['r']},
                                  'findings': [{'status': 'open', 'severity': 'high'}, {'status': 'covered', 'severity': 'low'}],
                                  'claims': [{'verdict': 'refuted'}], 'coverage': {'dropped': [1], 'unverified': [], 'unjudged': []},
                                  'ci': {'state': 'green'}, 'hil': None, 'heldThreads': [{'findingId': 'pr7-f1', 'why': 'resolve unconfirmed'}],
                                  'suppressed': [{'why': 'secret nit', 'severity': 'nit', 'reason': 'recheck-nit'}],
                                  'draft': {'body': 'secret words', 'comments': [{}], 'replies': []}}}, f)
        out = result.collect(['--output', f.name])
        self.assertEqual((out['event'], out['findings'], out['openBySeverity'], out['claims'], out['coverage']['dropped']),
                         ('REQUEST_CHANGES', {'open': 1, 'covered': 1}, {'P1 high': 1}, {'refuted': 1}, 1))
        self.assertNotIn('secret words', json.dumps(out))
        self.assertNotIn('secret nit', json.dumps(out))
        self.assertEqual(out['suppressedNits'], 1)
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
        role = (ROOT / 'agents' / 'finding-verifier.md').read_text(encoding='utf-8')
        self.assertEqual(tuple(re.findall(r'^\| `(\w+)` \|', role, re.M)), ledger.LEVELS)
        facts_ = re.findall(r'^- `(\w+)`: ', role.split('## Severity')[1], re.M)
        self.assertEqual(facts_, ['consequence', 'path', 'variants', 'recovery'])
        for wf in ('code-audit.js', 'pr-review.js'):
            src = (ROOT / 'workflows' / wf).read_text(encoding='utf-8')
            for const, want in (('LEVELS', ledger.LEVELS), ('CONFIDENCE', ledger.CONFIDENCE)):
                got = re.search(rf"^const {const} = \[([^\]]*)\]", src, re.M).group(1)
                self.assertEqual(tuple(re.findall(r"'(\w+)'", got)), want, f'{wf} {const}')
            got = re.search(r"^const IMPACT = \{.*?required: \[([^\]]*)\]", src, re.M | re.S).group(1)
            self.assertEqual(re.findall(r"'(\w+)'", got), facts_, f'{wf} IMPACT')


class Workflow(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_stub_harness_passes(self):
        done = subprocess.run(['node', str(ROOT / 'tests' / 'pr_review_harness.mjs')], capture_output=True, encoding='utf-8', errors='replace')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class Fixable(PostCase):
    """ledger.py show --fixable: what an auto-fix repair may take from the review we submitted."""

    def setUp(self):
        super().setUp()
        two = [{'file': 'src/core/a.c', 'line': 21, 'why': 'b is never initialised', 'severity': 'high', 'dimension': 'correctness', 'status': 'open'},
               {'file': 'src/core/a.c', 'line': 1, 'why': 'a shadows a global', 'severity': 'medium', 'dimension': 'correctness', 'status': 'open'}]
        draft = {'event': 'REQUEST_CHANGES', 'body': 'Summary.', 'replies': [],
                 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'b is never initialised', 'finding': 0},
                              {'path': 'src/core/a.c', 'line': 1, 'body': 'a shadows a global', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=two, draft=draft, verdict={'event': 'REQUEST_CHANGES', 'reasons': ['r']}))
        self.assertEqual(self.post('--auto')['status'], 'posted')
        self.root = self.review()['findings'][0]['commentId']
        self.pristine = Path(self.p['ledger']).read_text()

    def snap(self):
        snap = self.tmp / 'threads.json'
        self.call(threads, ['--pr', str(PR), '--repo', REPO, '--out', str(snap)])
        return snap, json.loads(snap.read_text())

    def show_fixable(self, snap, head=None):
        return self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--fixable', '--threads', str(snap),
                                  '--head', head or self.head])['fixable']

    def fixable(self, head=None):
        return self.show_fixable(self.snap()[0], head)

    def held(self, got):
        return [(h['findingId'], h['why']) for h in got['held']]

    def test_an_untouched_review_offers_every_standing_finding_inline_or_moved_into_the_body(self):
        got = self.fixable()
        self.assertEqual(([f['id'] for f in got['fix']], got['held'], got['autoPost']), (['pr7-f1', 'pr7-f2'], [], True))
        self.assertIsNone(self.review()['findings'][1].get('commentId'), 'the second finding stands in the review body')

    def test_a_recheck_s_suppressed_nit_is_nothing_to_repair_or_hold(self):
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['suppressed'] = [{'file': 'src/core/a.c', 'line': 5, 'dimension': 'correctness', 'severity': 'nit',
                                            'why': 'naming', 'reason': 'recheck-nit'}]
        Path(self.p['ledger']).write_text(json.dumps(led))
        got = self.fixable()
        self.assertEqual(([f['id'] for f in got['fix']], got['held'], got['autoPost']), (['pr7-f1', 'pr7-f2'], [], True))

    def test_an_edited_inline_comment_is_held_and_sends_the_recheck_to_the_human(self):
        next(c for c in self.gh.inline if c['id'] == self.root)['body'] = 'b is never initialised (edited)'
        got = self.fixable()
        self.assertEqual((self.held(got), [f['id'] for f in got['fix']], got['autoPost']), ([('pr7-f1', 'edited')], ['pr7-f2'], False))

    def test_a_deleted_inline_comment_is_held(self):
        self.gh.inline = [c for c in self.gh.inline if c['id'] != self.root]
        self.gh.threads = []
        self.assertEqual(self.held(self.fixable()), [('pr7-f1', 'deleted')])

    def test_an_edited_review_body_holds_the_finding_moved_into_it(self):
        rid = self.review()['receipts']['review']['reviewId']
        self.gh.reviews[rid]['body'] = self.gh.reviews[rid]['body'].replace('a shadows', 'a may shadow')
        got = self.fixable()
        self.assertEqual((self.held(got), [f['id'] for f in got['fix']], got['autoPost']), ([('pr7-f2', 'edited')], ['pr7-f1'], False))

    def in_body(self):
        out = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(self.snap()[0])])
        return {f['id']: f.get('inBody', False) for f in out['open']}

    def test_a_finding_is_in_the_body_only_while_the_pr_shows_that_body_as_posted(self):
        self.assertEqual(self.in_body(), {'pr7-f1': False, 'pr7-f2': True}, 'the inline one has its thread, not a body line')
        led = json.loads(Path(self.p['ledger']).read_text())
        del led['reviews'][-1]['draft']['moved'][0]['renderedLine']
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.assertEqual(self.in_body()['pr7-f2'], True, 'a draft saved before renderedLine is placed by its body too')
        rid = self.review()['receipts']['review']['reviewId']
        posted = self.gh.reviews[rid]['body']
        self.gh.reviews[rid]['body'] = posted.replace('a shadows', 'a may shadow')
        self.assertEqual(self.in_body()['pr7-f2'], False, 'an edited body no longer states it as we did')
        self.gh.reviews[rid]['body'] = posted
        del self.gh.reviews[rid]
        self.assertEqual(self.in_body()['pr7-f2'], False, 'a body the PR no longer shows states nothing')

    def test_a_contributor_reply_holds_the_finding_but_not_the_auto_recheck_and_a_bot_reply_holds_nothing(self):
        self.gh.inline.append({'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'created_at': self.gh.tick(),
                               'user': {'login': 'contrib'}})
        self.gh.threads[0]['comments']['nodes'].append({'databaseId': 950})
        got = self.fixable()
        self.assertEqual((self.held(got), got['autoPost']), ([('pr7-f1', 'replied')], True))
        self.gh.inline[-1]['user'] = {'login': 'bot[bot]', 'type': 'Bot'}
        self.assertEqual(self.fixable()['held'], [])

    def test_a_snapshot_missing_a_reply_or_the_thread_refuses_rather_than_hide_pushback(self):
        self.gh.inline.append({'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'created_at': self.gh.tick(),
                               'user': {'login': 'contrib'}})
        self.gh.threads[0]['comments']['nodes'].append({'databaseId': 950})
        for drop in ('reply', 'thread'):
            snap, got = self.snap()
            if drop == 'reply':
                got['comments'] = [c for c in got['comments'] if c['id'] != 950]
            else:
                got['threads'] = []
            snap.write_text(json.dumps(got))
            with self.assertRaisesRegex(facts.Unusable, "does not hold pr7-f1's whole thread"):
                self.show_fixable(snap)

    def test_a_disputed_finding_with_edited_text_still_sends_the_recheck_to_the_human(self):
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['findings'][0]['status'] = 'disputed'
        Path(self.p['ledger']).write_text(json.dumps(led))
        next(c for c in self.gh.inline if c['id'] == self.root)['body'] = 'edited'
        got = self.fixable()
        self.assertEqual((self.held(got), got['autoPost']), ([('pr7-f1', 'edited')], False))

    def test_an_unsettled_publication_refuses_since_the_last_review_on_the_pr_is_unknown(self):
        for status in ledger.ONLINE:
            led = json.loads(Path(self.p['ledger']).read_text())
            led['reviews'].append({**led['reviews'][-1], 'status': status, 'head': self.head})
            Path(self.p['ledger']).write_text(json.dumps(led))
            with self.assertRaisesRegex(facts.Unusable, f'a {status} publication .* is not settled'):
                self.fixable()
            led['reviews'].pop()
            led['reviews'][-1]['answerPublications'] = [{'head': self.head, 'status': status, 'answers': [], 'receipts': {}}]
            Path(self.p['ledger']).write_text(json.dumps(led))
            with self.assertRaisesRegex(facts.Unusable, 'is not settled'):
                self.fixable()
            del led['reviews'][-1]['answerPublications']
            Path(self.p['ledger']).write_text(json.dumps(led))

    def test_a_disputed_or_unplaced_finding_is_held(self):
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['findings'][0]['status'] = 'disputed'
        led['reviews'][-1]['draft']['moved'] = []
        Path(self.p['ledger']).write_text(json.dumps(led))
        got = self.fixable()
        self.assertEqual((self.held(got), got['autoPost']), ([('pr7-f1', 'disputed'), ('pr7-f2', 'unmapped')], False))

    def carried(self, found, name='c'):
        """A later review on a new head (a commit of src/core/<name>.c) carrying `found`, posted; the fixable view of it."""
        self.commit(f'src/core/{name}.c', f'int {name};\n', name)
        head = self.push_pr()
        p = self.prepare()
        self.assertEqual(p['mode'], 'incremental')
        self.save(self.result_for(p, findings=found, draft={'event': 'REQUEST_CHANGES', 'body': 'Still open.', 'comments': [], 'replies': []},
                                  verdict={'event': 'REQUEST_CHANGES', 'reasons': ['r']}))
        self.head = head
        self.assertEqual(self.post('--auto')['status'], 'posted')
        return self.fixable(head)

    def test_a_carried_finding_is_placed_by_the_review_that_published_it(self):
        got = self.carried([{'id': 'pr7-f1', 'file': 'src/core/a.c', 'line': 21, 'why': 'b is never initialised', 'severity': 'high',
                             'dimension': 'correctness', 'status': 'open'}])
        self.assertEqual(([f['id'] for f in got['fix']], got['held']), (['pr7-f1'], []))

    def group(self, change=None, muted=True):
        """The setUp review as one defect led by its first finding, its second left out as the muted member (no text
        of its own); muted=None leaves the key out, as a review saved before the flag does."""
        led = json.loads(self.pristine)
        rev = led['reviews'][-1]
        rev['draft']['moved'] = []
        for k, f in enumerate(rev['findings']):
            f.update(defect=0, lead=k == 0)
        if muted is None:
            rev['findings'][1].pop('muted', None)
        else:
            rev['findings'][1]['muted'] = muted
        if change:
            change(rev)
        Path(self.p['ledger']).write_text(json.dumps(led))
        return self.fixable()

    def outcome(self, got):
        return [f['id'] for f in got['fix']], self.held(got), got['autoPost']

    def test_a_muted_finding_is_held_grouped_and_does_not_hold_the_recheck(self):
        got = self.group()
        self.assertEqual(([f['id'] for f in got['fix']], got['held'], got['autoPost']),
                         (['pr7-f1'], [{'findingId': 'pr7-f2', 'commentId': None, 'why': 'grouped'}], True))
        self.assertEqual(self.outcome(self.group(lambda r: r['findings'].reverse())), (['pr7-f1'], [('pr7-f2', 'grouped')], True))

    def test_an_unplaced_finding_not_muted_stays_unmapped_as_one_saved_before_the_flag(self):
        for muted in (False, None):
            with self.subTest(muted=muted):
                self.assertEqual(self.outcome(self.group(muted=muted)), (['pr7-f1'], [('pr7-f2', 'unmapped')], False))

    def test_a_muted_finding_with_text_of_its_own_on_the_pr_is_judged_by_it(self):
        self.assertEqual(self.outcome(self.group(lambda r: r['draft'].update(moved=json.loads(self.pristine)['reviews'][-1]['draft']['moved']))),
                         (['pr7-f1', 'pr7-f2'], [], True))

    def test_a_contested_lead_no_longer_states_its_group(self):
        # The recheck may withdraw a disputed or replied lead and judge its muted mate without the lead's thread.
        self.assertEqual(self.outcome(self.group(lambda r: r['findings'][0].update(status='disputed'))),
                         ([], [('pr7-f1', 'disputed'), ('pr7-f2', 'unmapped')], False))
        self.gh.inline.append({'id': 950, 'in_reply_to_id': self.root, 'body': 'intentional', 'created_at': self.gh.tick(),
                               'user': {'login': 'contrib'}})
        self.gh.threads[0]['comments']['nodes'].append({'databaseId': 950})
        self.assertEqual(self.outcome(self.group()), ([], [('pr7-f1', 'replied'), ('pr7-f2', 'unmapped')], False))

    def test_a_muted_finding_whose_group_text_no_longer_stands_is_unmapped(self):
        self.assertEqual(self.outcome(self.group(lambda r: r['findings'][0].update(defect=1))), (['pr7-f1'], [('pr7-f2', 'unmapped')], False),
                         'a lead of another defect does not state it')
        # The lead's text edited or deleted on the PR, or deleted from the pending review before the human submitted it.
        next(c for c in self.gh.inline if c['id'] == self.root)['body'] = 'edited'
        self.assertEqual(self.outcome(self.group()), ([], [('pr7-f1', 'edited'), ('pr7-f2', 'unmapped')], False))
        self.gh.inline = [c for c in self.gh.inline if c['id'] != self.root]
        self.gh.threads = []
        self.assertEqual(self.outcome(self.group()), ([], [('pr7-f1', 'deleted'), ('pr7-f2', 'unmapped')], False))
        self.assertEqual(self.outcome(self.group(lambda r: r['findings'][0].update(status='dropped'))), ([], [('pr7-f2', 'unmapped')], False))

    def test_a_lead_saved_without_the_flag_or_a_mate_posted_beside_its_group_does_not_state_it(self):
        self.assertEqual(self.outcome(self.group(lambda r: r['findings'][0].pop('lead'))), (['pr7-f1'], [('pr7-f2', 'unmapped')], False))

        def beside(r):
            # The lead dropped; pr7-f1's comment, still standing, is that of a mate posted beside its group.
            r['findings'].append({**r['findings'][0], 'id': 'pr7-f3', 'commentId': None, 'status': 'dropped'})
            r['findings'][0]['lead'] = False
        self.assertEqual(self.outcome(self.group(beside)), (['pr7-f1'], [('pr7-f2', 'unmapped')], False))

    def bot_claim(self, resolved=False):
        """A bot's inline comment 900, on its own thread, claiming the group's defect; the change adding its claim."""
        self.gh.inline.append({'id': 900, 'path': 'src/core/a.c', 'line': 1, 'body': 'a shadows a global', 'created_at': self.gh.tick(),
                               'user': {'login': 'bot[bot]', 'type': 'Bot'}})
        self.gh.threads.append({'id': 'T900', 'isResolved': resolved, 'isOutdated': False, 'comments': {'nodes': [{'databaseId': 900}]}})

        def claimed(r):
            r['findings'][0]['status'] = 'dropped'
            r['claims'] = [{'commentId': 900, 'author': 'bot[bot]', 'claim': 'a shadows a global', 'verdict': 'confirmed',
                            'severity': 'medium', 'defect': 0}]
        return claimed

    def test_a_thread_claim_of_the_group_states_a_muted_finding_whose_lead_text_is_gone(self):
        self.assertEqual(self.outcome(self.group(self.bot_claim())), ([], [('pr7-f2', 'grouped')], True))

    def test_a_claim_led_group_holds_every_muted_finding_grouped_while_its_comment_is_listed(self):
        claimed = self.bot_claim()

        def led_by_claim(r):
            claimed(r)
            r['findings'][0].update(status='open', muted=True, commentId=None)
        self.assertEqual(self.outcome(self.group(led_by_claim)), ([], [('pr7-f1', 'grouped'), ('pr7-f2', 'grouped')], True))
        self.gh.inline = [c for c in self.gh.inline if c['id'] != 900]
        self.gh.threads = [t for t in self.gh.threads if t['id'] != 'T900']
        self.assertEqual(self.outcome(self.group(led_by_claim)), ([], [('pr7-f1', 'unmapped'), ('pr7-f2', 'unmapped')], False))

    def test_a_claim_on_a_resolved_thread_still_states_its_group(self):
        self.assertEqual(self.outcome(self.group(self.bot_claim(resolved=True))), ([], [('pr7-f2', 'grouped')], True))

    def test_a_claim_whose_comment_is_gone_from_the_pr_no_longer_states_its_group(self):
        claimed = self.bot_claim()
        self.gh.inline = [c for c in self.gh.inline if c['id'] != 900]
        self.gh.threads = [t for t in self.gh.threads if t['id'] != 'T900']
        self.assertEqual(self.outcome(self.group(claimed)), ([], [('pr7-f2', 'unmapped')], False))

    def test_a_carried_finding_the_latest_review_no_longer_mutes_is_unmapped(self):
        lead = {'id': 'pr7-f1', 'file': 'src/core/a.c', 'line': 21, 'why': 'b is never initialised', 'severity': 'high',
                'dimension': 'correctness', 'status': 'open', 'defect': 0, 'muted': False, 'lead': True}
        mate = {'file': 'src/core/c.c', 'line': 1, 'why': 'b is never initialised, seen from c', 'severity': 'high',
                'dimension': 'concurrency', 'status': 'open', 'defect': 0, 'muted': True, 'lead': False}
        self.assertEqual(self.outcome(self.carried([lead, mate])), (['pr7-f1'], [('pr7-f3', 'grouped')], True))
        self.assertEqual(self.outcome(self.carried([lead, {**mate, 'id': 'pr7-f3', 'defect': None, 'muted': False}], 'd')),
                         (['pr7-f1'], [('pr7-f3', 'unmapped')], False), 'the saved record keeps no stale muted')

    def test_a_carried_finding_the_human_edited_before_submitting_offers_the_published_text(self):
        # The state post.submitted leaves when the human edited the pending comment before submitting it.
        edited = 'b is initialised only on the error path'
        next(c for c in self.gh.inline if c['id'] == self.root)['body'] = edited
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['findings'][0]['published'] = edited
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.commit('src/core/c.c', 'int c;\n', 'c')
        head = self.push_pr()
        p = self.prepare()
        carried = [{'id': 'pr7-f1', 'file': 'src/core/a.c', 'line': 21, 'why': 'b is never initialised', 'severity': 'high',
                    'dimension': 'correctness', 'status': 'open'}]
        self.save(self.result_for(p, findings=carried, draft={'event': 'REQUEST_CHANGES', 'body': 'Still open.', 'comments': [], 'replies': []},
                                  verdict={'event': 'REQUEST_CHANGES', 'reasons': ['r']}))
        self.head = head
        self.assertEqual(self.post('--auto')['status'], 'posted')
        got = self.fixable(head)
        row = next(f for f in got['fix'] if f['id'] == 'pr7-f1')
        self.assertEqual((got['held'], row['why'], row.get('published')), ([], 'b is never initialised', edited))

    def test_it_refuses_a_head_that_is_not_the_last_submitted_review(self):
        with self.assertRaisesRegex(facts.Unusable, 'not one submitted on'):
            self.fixable(self.base)
        with self.assertRaisesRegex(facts.Unusable, '--fixable needs --threads and --head'):
            self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--fixable'])


class Repair(PostCase):
    """post.py --fixed: once an auto-fix repair is pushed, a fix note and a resolve on each finding it fixed."""

    def setUp(self):
        super().setUp()
        two = [{'file': 'src/core/a.c', 'line': 21, 'why': 'b is never initialised', 'severity': 'high', 'dimension': 'correctness', 'status': 'open'},
               {'file': 'src/core/a.c', 'line': 1, 'why': 'a shadows a global', 'severity': 'medium', 'dimension': 'correctness', 'status': 'open'}]
        draft = {'event': 'COMMENT', 'body': 'Summary.', 'replies': [],
                 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': 'b is never initialised', 'finding': 0},
                              {'path': 'src/core/a.c', 'line': 1, 'body': 'a shadows a global', 'finding': 1}]}
        self.save(self.result_for(self.p, findings=two, draft=draft))
        self.assertEqual(self.post('--auto')['status'], 'posted')
        self.root = self.review()['findings'][0]['commentId']
        self.reviewed = self.head
        self.commit('src/core/a.c', 'int a;\n' * 20 + 'int b = 0;\n', 'fix')
        self.fixed_head = self.push_pr()
        self.evidence = self.tmp / 'fix-check.json'
        self.evidence.write_text('{"verdict": "verified"}')

    def row(self, fid='pr7-f1', **over):
        f = next(f for f in self.review()['findings'] if f['id'] == fid)
        return {'id': fid, 'claimDigest': ledger.digest(ledger.claim(f)), 'verdict': 'verified', 'checkedSha': self.fixed_head,
                'note': 'b is now initialised.', 'evidence': str(self.evidence), **over}

    def fixed(self, *rows, head=None):
        m = self.tmp / 'fixed.json'
        m.write_text(json.dumps({'reviewedHead': self.reviewed, 'findings': list(rows) or [self.row()]}))
        return self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', head or self.fixed_head, '--fixed', str(m)])

    def thread(self):
        return next(t for t in self.gh.threads if t['comments']['nodes'][0]['databaseId'] == self.root)

    def show(self, head=None):
        snap = self.tmp / 'threads.json'
        self.call(threads, ['--pr', str(PR), '--repo', REPO, '--out', str(snap)])
        return self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO, '--threads', str(snap), *(['--head', head] if head else [])])

    def reply(self, author='contrib'):
        self.gh.inline.append({'id': 950, 'in_reply_to_id': self.root, 'body': 'not fixed for me', 'created_at': self.gh.tick(),
                               'user': {'login': author}})
        self.thread()['comments']['nodes'].append({'databaseId': 950})

    def test_a_long_note_is_posted_whole(self):
        note = ' '.join(['word'] * 70)
        self.assertEqual(self.fixed(self.row(note=note))['status'], 'repaired')
        self.assertEqual(self.gh.notes, [(self.root, f'Fixed in {self.fixed_head[:12]}: {note}')])

    def test_a_verified_repair_gets_one_note_and_its_resolve_and_no_review(self):
        posts, submits = len(self.gh.posts), len(self.gh.submits)
        out = self.fixed()
        self.assertEqual(out['status'], 'repaired')
        self.assertEqual(self.gh.notes, [(self.root, f'Fixed in {self.fixed_head[:12]}: b is now initialised.')])
        self.assertTrue(self.thread()['isResolved'])
        self.assertEqual((len(self.gh.posts), len(self.gh.submits)), (posts, submits), 'no review is created or submitted')
        x = self.review()['repairs'][0]
        self.assertEqual((x['state'], x['resolve'], x['head'], x['evidence']), ('published', 'resolved', self.fixed_head, str(self.evidence)))
        mutations = len(self.gh.mutations)
        self.assertEqual(self.fixed()['status'], 'repaired', 'a rerun returns the receipt')
        self.assertEqual((len(self.gh.notes), len(self.gh.mutations)), (1, mutations), 'and sends nothing')
        row = next(o for o in self.show()['open'] if o['id'] == 'pr7-f1')
        self.assertEqual(row['repair'], {'head': self.fixed_head, 'state': 'published', 'resolved': True},
                         'the recheck sees the note, so it drafts none')

    def test_rows_no_fix_check_verified_on_this_head_refuse_before_anything_is_sent(self):
        for over, why in (({'verdict': 'refuted'}, "not 'verified'"), ({'checkedSha': self.reviewed}, 'not the expected head'),
                          ({'claimDigest': ''}, 'no claimDigest'), ({'evidence': str(self.tmp / 'nope')}, 'is not a file'),
                          ({'note': 'two\nlines'}, 'not one nonempty line'), ({'note': '  '}, 'not one nonempty line')):
            with self.assertRaisesRegex(facts.Unusable, why):
                self.fixed(self.row(**over))
        with self.assertRaisesRegex(facts.Unusable, 'twice'):
            self.fixed(self.row(), self.row())
        nameless = self.row()
        del nameless['id']
        for bad in (nameless, {**self.row(), 'id': ''}, {**self.row(), 'id': ['pr7-f1']}, {**self.row(), 'id': 1}):
            with self.assertRaisesRegex(facts.Unusable, 'not an object with an id', msg=bad.get('id')):
                self.fixed(self.row('pr7-f2'), bad)
        out = self.fixed(self.row(checkedSha=self.reviewed), head=self.reviewed)
        self.assertEqual([(r['state'], r['error']) for r in out['repairs']],
                         [('skipped', f'the PR head moved to {self.fixed_head}; the repair was checked on {self.reviewed}')])
        self.assertEqual(self.gh.notes, [])

    def test_a_finding_in_the_body_with_a_changed_claim_or_pushback_is_skipped_and_the_rest_resolve(self):
        self.reply()
        out = self.fixed(self.row(), self.row('pr7-f2'))
        self.assertEqual(out['status'], 'partial')
        self.assertEqual([(r['findingId'], r['state'], r['error']) for r in out['repairs']],
                         [('pr7-f1', 'skipped', 'replied'), ('pr7-f2', 'skipped', 'no thread: it stands in the review body')])
        self.gh.inline = [c for c in self.gh.inline if c['id'] != 950]
        self.thread()['comments']['nodes'].pop()
        out = self.fixed(self.row(claimDigest='0' * 16))
        self.assertEqual(out['repairs'][0]['error'], 'the claim changed since its fix check')
        self.assertEqual(self.gh.notes, [])

    def test_a_reply_landing_before_the_resolve_defers_it_and_stays_pushback_to_judge(self):
        orig = self.gh.gh
        def gh(argv, stdin=None):
            got = orig(argv, stdin)
            if argv[0] == 'api' and '--method' in argv and argv[argv.index('--method') + 2].endswith('/replies'):
                self.reply()
            return got
        self.gh.gh = gh
        out = self.fixed()
        self.gh.gh = orig
        self.assertEqual((out['repairs'][0]['state'], out['repairs'][0]['resolve'], out['repairs'][0]['error']),
                         ('published', 'deferred', 'replied'))
        self.assertFalse(self.thread()['isResolved'])
        self.assertEqual(self.fixed()['repairs'][0]['resolve'], 'deferred', 'never resolved past that reply')
        self.assertEqual(len(self.gh.notes), 1)
        self.assertEqual([d['findingId'] for d in self.show(self.fixed_head)['disputes']], ['pr7-f1'],
                         'our note does not count as an answer to the reply it was not sent against')

    def test_a_note_whose_answer_was_lost_is_found_again_and_one_never_seen_is_never_sent_twice(self):
        self.gh.note_fails = 'lost'
        self.assertEqual(self.fixed()['status'], 'repaired')
        self.assertEqual(len(self.gh.notes), 1)
        self.setUp_second_finding_thread()
        self.gh.note_fails = True
        out = self.fixed(self.row('pr7-f3'))
        self.assertEqual((out['repairs'][0]['state'], out['status']), ('uncertain', 'partial'))
        self.gh.note_fails = None
        self.assertEqual(self.fixed(self.row('pr7-f3'))['repairs'][0]['state'], 'uncertain')
        self.assertEqual(len(self.gh.notes), 2, 'an unconfirmed note is reconciled by hand, never sent again')

    def setUp_second_finding_thread(self):
        """A third finding with its own thread, added to the posted review as if it had been posted with it."""
        led = json.loads(Path(self.p['ledger']).read_text())
        rev = led['reviews'][-1]
        rev['findings'].append({**rev['findings'][0], 'id': 'pr7-f3', 'commentId': 777, 'why': 'c leaks'})
        rev['receipts']['review']['commentIds'].append(777)
        rev['draft']['comments'].append({'path': 'src/core/a.c', 'line': 21, 'body': 'c leaks', 'findingId': 'pr7-f3'})
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.gh.inline.append({'id': 777, 'body': 'c leaks', 'path': 'src/core/a.c', 'line': 21, 'user': {'login': ME},
                               'created_at': self.gh.tick(), 'pull_request_review_id': rev['receipts']['review']['reviewId']})
        self.gh.threads.append({'id': 'T777', 'isResolved': False, 'isOutdated': False, 'comments': {'nodes': [{'databaseId': 777}]}})

    def test_a_reopened_or_unconfirmed_resolve_is_held_never_resolved_again(self):
        self.fixed()
        self.thread()['isResolved'] = False
        out = self.fixed()
        self.assertEqual((out['repairs'][0]['state'], out['repairs'][0]['error']), ('held', 'reopened after it was resolved'))
        self.assertFalse(self.thread()['isResolved'])
        self.assertEqual(self.show()['heldThreads'], [{'findingId': 'pr7-f1', 'commentId': self.root, 'why': 'reopened after it was resolved'}])
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['repairs'][0]['resolve'] = 'sent'
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.assertEqual(self.fixed()['repairs'][0]['error'], 'resolve unconfirmed')

    def after_note(self, act):
        """Run act right after the fake GitHub takes a note."""
        orig = self.gh.gh
        def gh(argv, stdin=None):
            got = orig(argv, stdin)
            if argv[0] == 'api' and '--method' in argv and argv[argv.index('--method') + 2].endswith('/replies'):
                act()
            return got
        self.gh.gh = gh

    def test_a_push_after_one_note_stops_the_next_before_it_is_sent(self):
        self.setUp_second_finding_thread()
        self.after_note(lambda: setattr(self.gh, 'head', 'f' * 40))
        out = self.fixed(self.row(), self.row('pr7-f3'))
        self.assertEqual([(r['findingId'], r['resolve'], r['error']) for r in out['repairs']],
                         [('pr7-f1', 'deferred', f"the PR head moved to {'f' * 40} since the repair"),
                          ('pr7-f3', None, f"the PR head moved to {'f' * 40} since the repair")])
        self.assertEqual(len(self.gh.notes), 1, 'the second note is never sent on a moved head')

    def test_a_closed_or_merged_pr_takes_no_note_and_no_resolve(self):
        for state in ('CLOSED', 'MERGED'):
            self.gh.state = state
            out = self.fixed()
            self.assertEqual((out['status'], out['repairs'][0]['state'], out['repairs'][0]['error']),
                             ('partial', 'skipped', f'the PR is {state.lower()}'))
        self.assertEqual(self.gh.notes, [])
        self.gh.state = 'OPEN'
        self.setUp_second_finding_thread()
        self.after_note(lambda: setattr(self.gh, 'state', 'MERGED'))
        out = self.fixed(self.row(), self.row('pr7-f3'))
        self.assertEqual([(r['findingId'], r['resolve'], r['error']) for r in out['repairs']],
                         [('pr7-f1', 'deferred', 'the PR is merged'), ('pr7-f3', None, 'the PR is merged')])
        self.assertEqual(len(self.gh.notes), 1, 'no note is sent once the PR is merged')
        self.assertFalse(self.thread()['isResolved'])

    def test_a_rerun_on_a_closed_pr_reads_back_a_note_left_sent_and_sends_and_resolves_nothing(self):
        with mock.patch.object(post, 'settle_note', side_effect=RuntimeError('crash after the POST')):
            with self.assertRaises(RuntimeError):
                self.fixed()
        self.assertEqual(self.review()['repairs'][0]['state'], 'sent')
        self.gh.state = 'CLOSED'
        mutations = len(self.gh.mutations)
        out = self.fixed()
        self.assertEqual([(r['state'], r['resolve'], r['error']) for r in out['repairs']], [('published', 'deferred', 'the PR is closed')])
        self.assertEqual((len(self.gh.notes), len(self.gh.mutations)), (1, mutations), 'nothing sent, nothing resolved')
        self.assertFalse(self.thread()['isResolved'])

    def test_an_edited_repair_note_holds_its_thread_from_a_later_review_despite_an_older_answer(self):
        self.reply(author=ME)  # an older verified answer of ours on the thread
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['receipts']['staged'] = [{'kind': 'answer', 'findingId': 'pr7-f1', 'commentId': self.root, 'replyId': 950,
                                                     'state': 'published', 'resolve': False}]
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.after_note(lambda: setattr(self.gh, 'head', 'f' * 40))
        self.fixed()
        del self.gh.gh
        self.gh.head = self.fixed_head
        self.gh.inline[-1]['body'] = 'Correction: not actually fixed'
        out = self.fixed()
        self.assertEqual((out['repairs'][0]['resolve'], out['repairs'][0]['error']), ('deferred', 'our repair note was edited'))
        sh(self.clone, 'git', 'fetch', '--quiet', 'origin', f'refs/pull/{PR}/head')
        resolves = [{'findingId': 'pr7-f1', 'commentId': self.root}]
        for reason in ('recheck', 'another full review'):
            draft = {'event': 'COMMENT', 'body': reason, 'comments': [], 'replies': [], 'resolves': resolves}
            self.save(self.result_for(self.p, head=self.fixed_head, findings=[{'id': 'pr7-f1', 'status': 'fixed'}], draft=draft), reason=reason)
            self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.fixed_head, '--auto'])
            self.assertEqual([(m['state'], m['error']) for m in self.review()['receipts']['resolved']], [('deferred', 'our repair note was edited')])
        self.assertEqual(self.show()['heldThreads'], [{'findingId': 'pr7-f1', 'commentId': self.root, 'why': 'our repair note was edited'}])
        self.assertFalse(self.thread()['isResolved'])

    def test_a_note_deferred_on_one_head_resolves_on_another_only_for_the_same_claim(self):
        self.after_note(lambda: setattr(self.gh, 'head', 'f' * 40))
        self.assertEqual(self.fixed()['repairs'][0]['resolve'], 'deferred')
        del self.gh.gh  # the head stays moved; a fix check on it comes next
        out = self.fixed(self.row(checkedSha='f' * 40, claimDigest='0' * 16), head='f' * 40)
        self.assertEqual((out['repairs'][0]['resolve'], out['repairs'][0]['error']), ('deferred', 'the claim changed since its fix check'))
        self.assertFalse(self.thread()['isResolved'])
        out = self.fixed(self.row(checkedSha='f' * 40), head='f' * 40)
        self.assertEqual(out['status'], 'repaired')
        self.assertEqual(self.review()['repairs'][0]['resolveCheck'], {'head': 'f' * 40, 'evidence': str(self.evidence)})

    def test_a_root_edited_after_the_note_is_never_resolved(self):
        self.after_note(lambda: next(c for c in self.gh.inline if c['id'] == self.root).update(body='b may be uninitialised'))
        out = self.fixed()
        self.assertEqual((out['status'], out['repairs'][0]['resolve'], out['repairs'][0]['error']), ('partial', 'deferred', 'edited'))
        self.assertFalse(self.thread()['isResolved'])

    def test_an_unconfirmed_note_is_shown_to_the_recheck_and_held(self):
        self.gh.note_fails = True
        self.fixed()
        row = next(o for o in self.show()['open'] if o['id'] == 'pr7-f1')
        self.assertEqual(row['repair'], {'head': self.fixed_head, 'state': 'uncertain', 'resolved': False})
        self.assertEqual(self.show()['heldThreads'], [{'findingId': 'pr7-f1', 'commentId': self.root, 'why': 'repair note unconfirmed'}])

    def test_a_finding_raised_again_after_its_repair_takes_its_new_comment_as_root_once_published(self):
        self.fixed()
        sh(self.clone, 'git', 'fetch', '--quiet', 'origin', f'refs/pull/{PR}/head')
        draft = {'event': 'COMMENT', 'body': 'Again.', 'replies': [],
                 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': '**high**: b is never initialised\n- The repair did not fix this.', 'finding': 0}]}
        self.save(self.result_for(self.p, head=self.fixed_head, findings=[{'id': 'pr7-f1', 'status': 'open'}], draft=draft), reason='recheck')
        self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.fixed_head, '--auto'])
        f = self.review()['findings'][0]
        self.assertNotEqual(f['commentId'], self.root, 'the new comment is its root')
        row = next(o for o in self.show()['open'] if o['id'] == 'pr7-f1')
        self.assertEqual((row['repair'], row['resolveDue']), (None, None), 'the old repair stays on the old root')
        self.assertEqual(json.loads(Path(self.p['ledger']).read_text())['reviews'][-2]['repairs'][0]['commentId'], self.root)

    def recheck(self, comments, replies=()):
        """A recheck review of the repaired head, saved and submitted with its event."""
        sh(self.clone, 'git', 'fetch', '--quiet', 'origin', f'refs/pull/{PR}/head')
        draft = {'event': 'COMMENT', 'body': 'Again.', 'replies': list(replies), 'comments': comments}
        self.save(self.result_for(self.p, head=self.fixed_head, findings=[{'id': 'pr7-f1', 'status': 'open'}], draft=draft), reason='recheck')
        return self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.fixed_head, '--auto'])

    def test_a_recheck_draft_saved_before_the_repair_note_never_repeats_it(self):
        sh(self.clone, 'git', 'fetch', '--quiet', 'origin', f'refs/pull/{PR}/head')
        draft = {'event': 'COMMENT', 'body': 'Fixed.', 'comments': [], 'replies': [{'commentId': self.root, 'body': 'Fixed in abc.', 'findingId': 'pr7-f1'}]}
        self.save(self.result_for(self.p, head=self.fixed_head, findings=[{'id': 'pr7-f1', 'status': 'fixed'}], draft=draft), reason='recheck')
        self.fixed()
        self.assertEqual(self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.fixed_head, '--auto'])['status'], 'posted')
        self.assertEqual((len(self.gh.notes), self.adds()), (1, []), 'only the repair note reaches the thread')

    def upheld_raised_again(self, *flags):
        """An auto-post whose recheck upholds the pushback on a repaired finding and raises it again on a new comment."""
        self.fixed()
        self.reply()
        d = self.show(self.fixed_head)['disputes'][0]
        self.assertEqual(d['rootCommentId'], self.root)
        rec = {'key': d['key'], 'replies': d['replies'], 'judgedHead': self.fixed_head, 'threadsFile': str(self.tmp / 'threads.json'),
               'state': 'upheld', 'reason': 'r', 'rootCommentId': d['rootCommentId'],
               'answer': {'body': 'Still stands: b is read before it is set.', 'resolve': False}}
        sh(self.clone, 'git', 'fetch', '--quiet', 'origin', f'refs/pull/{PR}/head')
        draft = {'event': 'COMMENT', 'body': 'Again.', 'replies': [],
                 'comments': [{'path': 'src/core/a.c', 'line': 21, 'body': '**high**: b is never initialised\n- The repair did not fix this.', 'finding': 0}]}
        self.save(self.result_for(self.p, head=self.fixed_head, findings=[{'id': 'pr7-f1', 'status': 'upheld', 'disputes': [rec]}], draft=draft),
                  reason='recheck')
        out = self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.fixed_head, '--auto', *flags])
        self.assertNotEqual(self.review()['findings'][0]['commentId'], self.root, 'the raised-again comment is its root now')
        return out

    def test_an_upheld_finding_raised_again_answers_its_pushback_on_the_thread_judged(self):
        out = self.upheld_raised_again()
        self.assertEqual([(e['commentId'], e['state']) for e in out['answers']['staged']], [(self.root, 'staged')],
                         'the answer goes on the thread whose pushback it answers')

    def test_a_deferred_answer_on_a_finding_raised_again_lists_the_thread_judged(self):
        next(c for c in self.gh.inline if c['id'] == self.root)['html_url'] = 'https://x/root'
        self.assertEqual(self.upheld_raised_again('--defer-answers')['answers'], {'status': 'deferred'})
        shown = self.call(ledger, ['show', '--pr', str(PR), '--repo', REPO])['answers']
        self.assertEqual([(a['findingId'], a['commentId'], a['url']) for a in shown], [('pr7-f1', self.root, 'https://x/root')],
                         'the answer awaiting the human names the thread post.py stages it on')

    def test_a_raised_again_comment_keeps_the_claim_the_human_edited_and_reads_unedited(self):
        next(c for c in self.gh.inline if c['id'] == self.root)['body'] = 'b may be uninitialised'
        self.call(post, ['--pr', str(PR), '--repo', REPO, '--sync'])
        led = json.loads(Path(self.p['ledger']).read_text())
        led['reviews'][-1]['findings'][0]['published'] = 'b may be uninitialised'
        Path(self.p['ledger']).write_text(json.dumps(led))
        self.fixed(self.row())
        body = 'b may be uninitialised\n- The repair did not fix this.'
        self.recheck([{'path': 'src/core/a.c', 'line': 21, 'body': body, 'finding': 0}])
        f = self.review()['findings'][0]
        self.assertEqual(f['published'], body)
        snap, _ = Fixable.snap(self)
        self.assertIsNone(ledger.holder(json.loads(Path(self.p['ledger']).read_text()), json.loads(snap.read_text()))(f),
                          'its new root reads as published')

    def test_a_finding_raised_again_in_the_body_has_no_thread_and_keeps_its_old_repair(self):
        self.fixed()
        self.recheck([{'path': 'src/core/a.c', 'line': 1, 'body': '**high**: b is never initialised\n- The repair did not fix this.', 'finding': 0}])
        self.assertIsNone(self.review()['findings'][0]['commentId'])
        row = next(o for o in self.show()['open'] if o['id'] == 'pr7-f1')
        self.assertEqual((row['commentId'], row['repair']), (None, None))
        self.reviewed = self.fixed_head  # the recheck is now the review a repair answers
        self.assertEqual(self.fixed()['repairs'][0]['error'], 'no thread: it stands in the review body')

    def test_a_finding_raised_again_off_the_diff_holds_auto_post_for_its_dropped_lines(self):
        self.fixed()
        body = '**high**: b is uninitialised.\n- Only when mode is enabled.\n- The repair in cccccccccccc did not fix this; its earlier thread was resolved.'
        submits = list(self.gh.submits)
        out = self.recheck([{'path': 'src/core/a.c', 'line': 1, 'body': body, 'finding': 0, 'firstLineUnchecked': True}])
        self.assertRegex(out['handedOver'], r'first line no check verified \(src/core/a\.c:1\)')
        self.assertEqual((self.review()['status'], self.gh.submits), ('drafted', submits), 'pending for the human, never submitted')

    def test_a_saved_draft_never_resolves_a_repaired_thread_reopened_since(self):
        self.fixed()
        sh(self.clone, 'git', 'fetch', '--quiet', 'origin', f'refs/pull/{PR}/head')
        draft = {'event': 'COMMENT', 'body': 'Fixed.', 'comments': [], 'replies': [], 'resolves': [{'findingId': 'pr7-f1', 'commentId': self.root}]}
        self.save(self.result_for(self.p, head=self.fixed_head, findings=[{'id': 'pr7-f1', 'status': 'fixed'}], draft=draft), reason='recheck')
        self.thread()['isResolved'] = False
        resolves = len([m for m in self.gh.mutations if 'resolveReviewThread' in m['query']])
        self.assertEqual(self.call(post, ['--pr', str(PR), '--repo', REPO, '--expected-head', self.fixed_head, '--auto'])['status'], 'posted')
        self.assertEqual(len([m for m in self.gh.mutations if 'resolveReviewThread' in m['query']]), resolves)
        self.assertEqual([(m['state'], m['error']) for m in self.review()['receipts']['resolved']], [('deferred', 'reopened after it was resolved')])
        self.assertFalse(self.thread()['isResolved'])

    def test_a_pending_review_of_ours_blocks_a_new_note_only(self):
        self.gh.reviews[1] = {'id': 1, 'body': 'by hand', 'state': 'PENDING', 'user': {'login': ME}}
        self.assertEqual(self.fixed()['repairs'][0]['error'], 'a pending review of ours is open on the PR')
        self.assertEqual(self.gh.notes, [])

    def test_a_repaired_note_counts_as_replied_once_a_later_review_finds_it_fixed(self):
        orig = self.gh.gh
        def gh(argv, stdin=None):
            got = orig(argv, stdin)
            if argv[0] == 'api' and '--method' in argv and argv[argv.index('--method') + 2].endswith('/replies'):
                self.reply(author='bot[bot]')
                self.gh.inline[-1]['user']['type'] = 'Bot'
            return got
        self.gh.gh = gh
        self.assertEqual(self.fixed()['status'], 'repaired', 'a bot reply is no pushback')
        self.gh.gh = orig
        self.assertEqual(self.review()['repairs'][0]['replyId'] in ledger.answered_ids(json.loads(Path(self.p['ledger']).read_text())), True)


if __name__ == '__main__':
    unittest.main()
