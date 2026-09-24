"""Tests for pr-babysit's push.py against real bare remotes and a fake gh."""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'push.py'
spec = importlib.util.spec_from_file_location('pr_babysit_push', SCRIPT)
push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push)

ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}


class PushTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, ENV))
        self.enterContext(mock.patch.object(push, 'PR_WAIT', 0))
        self.a, self.b = str(self.root / 'a.git'), str(self.root / 'b.git')
        for bare in (self.a, self.b):
            subprocess.run(['git', 'init', '-q', '--bare', bare], check=True)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', self.a)
        self.sha = self.commit('one')

    def git(self, *argv):
        return subprocess.run(['git', *argv], check=True, capture_output=True, text=True).stdout

    def commit(self, message):
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD').strip()

    def remote_head(self, bare):
        out = subprocess.run(['git', 'ls-remote', bare, 'refs/heads/fix'], capture_output=True, text=True).stdout
        return out.split('\t')[0] if out else ''

    def publish(self, *urls, sha=None, pr=None):
        argv = ['--remote', 'origin', '--branch', 'fix', '--sha', sha or self.sha]
        for u in urls:
            argv += ['--push-url', u]
        if pr is not None:
            argv += ['--pr', str(pr)]
        out = io.StringIO()
        with redirect_stdout(out):
            code = push.report(push.publish, argv)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_a_push_that_lands_reads_back_the_sha(self):
        code, out = self.publish(self.a)
        self.assertEqual(code, 0)
        self.assertEqual(out['pushed'], True)
        self.assertEqual(out['heads'], [{'url': self.a, 'head': self.sha}])
        self.assertNotIn('prHead', out)

    def test_a_rejected_push_reads_back_what_the_branch_holds(self):
        self.publish(self.a)
        self.git('checkout', '-q', '--orphan', 'other')
        other = self.commit('unrelated')
        code, out = self.publish(self.a, sha=other)
        self.assertEqual((code, out['pushed']), (0, False))
        self.assertIn('rejected', out['detail'])
        self.assertEqual(out['heads'], [{'url': self.a, 'head': self.sha}])

    def test_the_readback_asks_the_push_url_not_the_fetch_url(self):
        self.git('remote', 'set-url', '--push', 'origin', self.b)
        code, out = self.publish(self.b)
        self.assertEqual(out['heads'], [{'url': self.b, 'head': self.sha}])
        self.assertEqual(self.remote_head(self.a), '', 'the fetch URL never got it')

    def test_every_push_url_is_read_back(self):
        self.git('remote', 'set-url', '--push', 'origin', self.a)
        self.git('remote', 'set-url', '--add', '--push', 'origin', self.b)
        code, out = self.publish(self.a, self.b)
        self.assertEqual([h['head'] for h in out['heads']], [self.sha, self.sha])

    def test_an_unreadable_destination_is_null_not_absent(self):
        gone = str(self.root / 'gone.git')
        self.git('remote', 'set-url', '--push', 'origin', gone)
        code, out = self.publish(gone)
        self.assertEqual((code, out['pushed']), (0, False))
        self.assertEqual(out['heads'], [{'url': gone, 'head': None}])

    def test_changed_push_urls_refuse_before_pushing(self):
        code, out = self.publish(self.b)
        self.assertEqual(code, 2)
        self.assertIn(f'origin now pushes to {self.a}, not {self.b}', out['error'])
        self.assertEqual(self.remote_head(self.a), '')

    def test_argument_errors(self):
        for argv, want in ((['--remote', 'origin'], 'usage'),
                           (['--remote', 'origin', '--branch', 'fix', '--sha', 'HEAD', '--push-url', self.a], 'not a full SHA')):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(push.report(push.publish, argv), 2)
            self.assertIn(want, json.loads(out.getvalue())['error'])

    def fake_gh(self, script):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / 'gh').write_text(f'#!/bin/sh\n{script}\n')
        (bin_dir / 'gh').chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {'PATH': f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}))

    def test_the_pr_head_is_retried_until_github_sees_the_push(self):
        count = self.root / 'count'
        self.fake_gh(f'n=$(cat {count} 2>/dev/null || echo 0); echo $((n+1)) > {count}; '
                     f'[ "$n" -ge 2 ] && echo {self.sha} || echo {"0" * 40}')
        code, out = self.publish(self.a, pr=7)
        self.assertEqual(out['prHead'], self.sha)
        self.assertEqual(count.read_text().strip(), '3')

    def test_an_unreadable_pr_head_is_null_after_every_try(self):
        count = self.root / 'count'
        self.fake_gh(f'n=$(cat {count} 2>/dev/null || echo 0); echo $((n+1)) > {count}; exit 1')
        code, out = self.publish(self.a, pr=7)
        self.assertIsNone(out['prHead'])
        self.assertEqual(count.read_text().strip(), str(push.PR_TRIES))


if __name__ == '__main__':
    unittest.main()
