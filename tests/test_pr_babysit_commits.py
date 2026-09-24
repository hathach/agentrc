"""Tests for pr-babysit's commits.py against a real temp repository."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'commits.py'
spec = importlib.util.spec_from_file_location('pr_babysit_commits', SCRIPT)
commits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commits)

ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@t'}


class CommitsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git('init', '-q')
        self.write('a.c', 'a\n')
        self.base = self.commit('base', 'a.c')

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, env=ENV, check=True,
                              capture_output=True, text=True).stdout

    def write(self, path, text):
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(text)

    def commit(self, message, *paths):
        self.git('add', '-A', '--', *paths)
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD').strip()

    def run_script(self, *argv, cwd=None):
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=cwd or self.repo, env=ENV,
                              capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout.splitlines()[-1])

    def test_head_reads_the_commit_and_the_scope_unquoted(self):
        odd = ['src/space name.c', 'src/quote"name.c', '-x.c']
        for p in odd:
            self.write(p, 'x\n')
        self.write('a.c', 'changed\n')
        self.write('left.c', 'left\n')
        sha = self.commit('fix: the thing\n\nWhy it matters.', *odd)
        self.write('src/space name.c', 'edited after\n')
        code, out = self.run_script('head', *odd, 'a.c')
        self.assertEqual(code, 0)
        self.assertEqual(out['sha'], sha)
        self.assertEqual(out['parents'], [self.base])
        self.assertEqual(sorted(out['paths']), sorted(odd))
        self.assertEqual(out['message'], 'fix: the thing\n\nWhy it matters.\n\n')
        self.assertEqual(sorted(out['leftover']), [' M a.c', ' M src/space name.c'])
        self.assertEqual(len(out['entries']), 4)
        blob = self.git('rev-parse', f'{sha}:src/quote"name.c').strip()
        self.assertIn(f'100644 blob {blob}\tsrc/quote"name.c', out['entries'])

    def test_head_reports_a_deletion_by_its_absence_and_every_parent_of_a_merge(self):
        self.git('rm', '-q', 'a.c')
        self.git('commit', '-q', '-m', 'drop')
        code, out = self.run_script('head', 'a.c')
        self.assertEqual((code, out['paths'], out['entries']), (0, ['a.c'], []))
        self.git('checkout', '-q', '-b', 'side', self.base)
        self.write('b.c', 'b\n')
        side = self.commit('side', 'b.c')
        self.git('checkout', '-q', '-')
        self.git('merge', '-q', '--no-edit', 'side')
        code, out = self.run_script('head', 'b.c')
        self.assertEqual(len(out['parents']), 2)
        self.assertIn(side, out['parents'])

    def test_head_moving_while_read_is_an_error(self):
        real = commits.git
        heads = iter([self.base + '\n', '2' * 40 + '\n'])
        with mock.patch.object(commits, 'git', side_effect=lambda *a: next(heads) if a == ('rev-parse', 'HEAD') else real(*a)):
            cwd = os.getcwd()
            os.chdir(self.repo)
            try:
                with self.assertRaisesRegex(commits.Unusable, 'HEAD moved'):
                    commits.head(['a.c'])
            finally:
                os.chdir(cwd)

    def test_chain_lists_each_commit_oldest_first(self):
        self.write('b.c', 'b\n')
        one = self.commit('one', 'b.c')
        self.write('c\nd.c', 'c\n')
        two = self.commit('two', 'c\nd.c')
        code, out = self.run_script('chain', self.base, two)
        self.assertEqual(code, 0)
        self.assertEqual([c['sha'] for c in out['commits']], [one, two])
        self.assertEqual([c['parents'] for c in out['commits']], [[self.base], [one]])
        self.assertEqual([c['paths'] for c in out['commits']], [['b.c'], ['c\nd.c']], 'a name holding a newline stays whole')
        self.assertEqual(out['commits'][1]['message'], 'two\n\n')

    def test_a_name_that_is_not_utf8_is_an_error_not_a_lookalike(self):
        self.write('bad\ufffd.c', 'owned\n')
        (self.repo / b'bad\xff.c'.decode('utf-8', 'surrogateescape')).write_text('stray\n')
        self.commit('both', '.')
        code, out = self.run_script('head', 'bad\ufffd.c')
        self.assertEqual(code, 2)
        self.assertIn('not UTF-8', out['error'])

    def test_errors(self):
        for argv, want in ((['chain', 'HEAD~1', 'HEAD'], 'not a full SHA'), (['chain', self.base], 'usage'),
                           (['head'], 'usage'), (['frob'], 'usage'),
                           (['chain', self.base, 'f' * 40], 'git rev-list')):
            code, out = self.run_script(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(want, out['error'], argv)


if __name__ == '__main__':
    unittest.main()
