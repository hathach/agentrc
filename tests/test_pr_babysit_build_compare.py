"""Tests for pr-babysit's build_compare.py against fake build commands in a temp repository."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'build_compare.py'


class BuildCompareTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        self.env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
        self.git('init', '-q')
        (self.repo / 'src.c').write_text('int ok;\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def run_script(self, *argv, cwd=None):
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True)
        out = json.loads(done.stdout.splitlines()[-1])
        if 'log' in out:
            self.addCleanup(lambda: os.path.exists(out['log']) and os.remove(out['log']))
        return done.returncode, out

    def test_candidate_builds_the_checkout_as_it_stands_in_a_fresh_dir(self):
        (self.repo / 'src.c').write_text('int broken\n')
        code, out = self.run_script('candidate', '--path=.', '--command', 'grep -q "int ok;" src.c && touch <BUILD>/out')
        self.assertEqual(code, 0)
        self.assertEqual((out['side'], out['revision'], out['exit'], out['setupExit']), ('candidate', self.base, 1, None))
        self.assertIn(out['buildDir'], out['command'])
        self.assertNotIn('<BUILD>', out['command'])
        self.assertTrue(out['cleanup']['ok'])
        self.assertFalse(os.path.exists(out['buildDir']))
        self.assertIn('$ grep -q', Path(out['log']).read_text(), 'the log is kept')

    def test_the_snapshot_follows_uncommitted_and_untracked_changes(self):
        snaps = []
        for change in (lambda: None, lambda: (self.repo / 'src.c').write_text('int x;\n'),
                       lambda: (self.repo / 'new.c').write_text('a'), lambda: (self.repo / 'new.c').write_text('b')):
            change()
            snaps.append(self.run_script('candidate', '--path=.', '--command', 'true')[1]['snapshot'])
        self.assertEqual(len(set(snaps)), 4)

    def test_base_builds_in_its_own_worktree_and_leaves_the_checkout_alone(self):
        (self.repo / 'src.c').write_text('int broken\n')
        (self.repo / 'untracked.c').write_text('x')
        status = self.git('status', '--porcelain')
        code, out = self.run_script('base', '--rev', self.base, '--setup', 'touch <BUILD>/deps',
                                    '--command', 'test -f <BUILD>/deps && grep -q "int ok;" src.c')
        self.assertEqual((code, out['side'], out['revision'], out['snapshot']), (0, 'base', self.base, None))
        self.assertEqual((out['setupExit'], out['exit']), (0, 0))
        self.assertTrue(out['cleanup']['ok'])
        self.assertEqual(self.git('status', '--porcelain'), status)
        self.assertEqual(len(self.git('worktree', 'list').splitlines()), 1, 'the temporary worktree is gone')

    def test_each_side_gets_its_own_build_dir(self):
        a = self.run_script('candidate', '--path=.', '--command', 'test -z "$(ls <BUILD>)" && touch <BUILD>/x')[1]
        b = self.run_script('base', '--rev', self.base, '--command', 'test -z "$(ls <BUILD>)" && touch <BUILD>/x')[1]
        self.assertNotEqual(a['buildDir'], b['buildDir'])
        self.assertEqual((a['exit'], b['exit']), (0, 0), 'each started empty')

    def test_a_failed_baseline_setup_is_reported_and_nothing_is_built(self):
        code, out = self.run_script('base', '--rev', self.base, '--setup', 'test -d deps', '--command', 'touch built')
        self.assertEqual((code, out['setupExit'], out['exit']), (0, 1, None))
        self.assertTrue(out['cleanup']['ok'])

    def test_a_retained_build_dir_is_reported(self):
        code, out = self.run_script('candidate', '--path=.', '--command', 'mkdir <BUILD>/ro && touch <BUILD>/ro/f && chmod 555 <BUILD>/ro')
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', out['buildDir']]))
        self.addCleanup(lambda: subprocess.run(['chmod', '-R', 'u+w', out['buildDir']]))
        if os.geteuid() == 0:
            self.skipTest('root can remove a read-only directory')
        self.assertEqual((code, out['exit']), (0, 0))
        self.assertEqual(out['cleanup']['retained'], [out['buildDir']])
        self.assertFalse(out['cleanup']['ok'])

    def test_a_build_that_rewrites_the_candidate_sources_shows_in_the_snapshots(self):
        (self.repo / 'src.c').write_text('int fixed;\n')
        (self.repo / 'other.c').write_text('x')
        code, out = self.run_script('candidate', '--path=src.c', '--command', 'git checkout -- src.c')
        self.assertEqual(code, 0)
        self.assertNotEqual(out['snapshot'], out['snapshotAfter'])
        code, out = self.run_script('candidate', '--path=src.c', '--command', 'echo y > other.c && touch <BUILD>/o')
        self.assertEqual(out['snapshot'], out['snapshotAfter'], 'a path outside the candidate may change')

    def test_a_path_named_like_pathspec_magic_snapshots_only_itself(self):
        (self.repo / ':(top)*').write_text('odd')
        snap = lambda: self.run_script('candidate', '--path=:(top)*', '--command', 'true')[1]['snapshot']
        before = snap()
        (self.repo / 'src.c').write_text('int x;\n')
        (self.repo / 'new.c').write_text('a')
        self.assertEqual(snap(), before, 'no other path joins the snapshot')

    def test_values_may_start_with_a_dash(self):
        code, out = self.run_script('candidate', '--path=-weird', '--command=true')
        self.assertEqual((code, out['exit']), (0, 0), out)

    def test_a_filesystem_error_is_reported_and_cleaned_up(self):
        if os.geteuid() == 0:
            self.skipTest('root reads an unreadable file')
        (self.repo / 'secret.c').write_text('x')
        os.chmod(self.repo / 'secret.c', 0)
        self.addCleanup(os.chmod, self.repo / 'secret.c', 0o644)
        before = set(Path(tempfile.gettempdir()).glob('pr-babysit-build-*'))
        code, out = self.run_script('candidate', '--path=.', '--command', 'true')
        self.assertEqual(code, 2)
        self.assertIn('Permission denied', out['error'])
        self.assertEqual(set(Path(tempfile.gettempdir()).glob('pr-babysit-build-*')), before, 'the build dir is gone')

    def test_refusals(self):
        for argv, why in [
            (['base', '--command', 'true'], 'base needs --rev'),
            (['base', '--rev', 'HEAD', '--command', 'true'], 'base needs --rev'),
            (['candidate', '--path=.', '--rev', self.base, '--command', 'true'], 'no --rev or --setup'),
            (['candidate', '--command', 'true'], '--path names the candidate'),
            (['base', '--rev', self.base, '--path=.', '--command', 'true'], '--path names the candidate'),
            (['candidate'], 'usage'),
            (['base', '--rev', 'f' * 40, '--command', 'true'], 'git worktree add'),
        ]:
            code, out = self.run_script(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(why, out['error'])
        (self.repo / 'sub').mkdir()
        code, out = self.run_script('candidate', '--path=.', '--command', 'true', cwd=self.repo / 'sub')
        self.assertEqual(code, 2)
        self.assertIn('run from the checkout top level', out['error'])
        self.assertEqual(len(self.git('worktree', 'list').splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
