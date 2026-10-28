"""Tests for pr-babysit's scope_check.py against a temp repository."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'scope_check.py'
STAR = [] if os.name == 'nt' else ['src/*.c']  # NTFS forbids * in a file name


def setUpModule():
    guard_survivors(__name__)


class ScopeCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        self.env = git_env()  # pre-commit exports GIT_* a test's git in its temp repo must not inherit
        files = {'src/a.c': 'int a;\n', 'src/keep me.c': '', **dict.fromkeys(STAR, ''), 'test/unit/test_a.c': '', '.gitignore': '*.generated.c\nbuild/\n!build/again.c\n',
                 'build/keep.c': ''}
        for path, text in files.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text)
        self.git('init', '-q')
        self.git('add', '-f', '--', *files)
        self.git('commit', '-qm', 'base')
        (self.repo / 'src' / 'untracked.c').write_text('')
        (self.repo / 'docs').mkdir()
        os.symlink('test', self.repo / 'tests')
        os.symlink(self.repo.parent, self.repo / 'up')

    def git(self, *argv):
        subprocess.run(['git', *argv], cwd=self.repo, env=self.env, check=True, capture_output=True)

    def check(self, *paths, cwd=None):
        done = subprocess.run([sys.executable, str(SCRIPT), *(f'--path={p}' for p in paths)], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout.splitlines()[-1])

    def test_tracked_files_and_files_to_create_are_accepted(self):
        code, out = self.check('src/a.c', 'src/keep me.c', *STAR, 'test/unit/test_b.c', 'test/device/test_hid.c')
        self.assertEqual(code, 0)
        self.assertEqual(out['accepted'], [{'path': 'src/a.c', 'kind': 'existing'}, {'path': 'src/keep me.c', 'kind': 'existing'},
                                           *({'path': p, 'kind': 'existing'} for p in STAR), {'path': 'test/unit/test_b.c', 'kind': 'new'},
                                           {'path': 'test/device/test_hid.c', 'kind': 'new'}])
        self.assertEqual(out['refused'], [])
        self.assertIn('seal', out)

    def test_a_pathspec_pattern_names_only_itself(self):
        _, out = self.check('src/[a].c', 'src/:(glob)*.c')
        magic = ['src/:(glob)*.c'] if os.name != 'nt' else []  # a : is no plain path on Windows
        self.assertEqual([r['path'] for r in out['accepted']], ['src/[a].c', *magic])
        self.assertEqual({r['kind'] for r in out['accepted']}, {'new'}, 'neither matched src/a.c as a pattern')

    def test_what_a_writer_must_not_be_given_is_refused_with_its_reason(self):
        _, out = self.check('/etc/passwd', '../x.c', 'src/../src/a.c', './src/a.c', 'src//a.c', '', 'src/untracked.c', 'src',
                            'docs/new.md', 'tests/test_a.c', 'up/repo/src/a.c', 'src/a.c/x.c', 'src/x.generated.c', 'build/new.c', 'build/again.c')
        self.assertEqual(out['accepted'], [])
        self.assertEqual([(r['path'], r['why']) for r in out['refused']], [
            ('/etc/passwd', 'not a plain repository-relative path'),
            ('../x.c', 'not a plain repository-relative path'),
            ('src/../src/a.c', 'not a plain repository-relative path'),
            ('./src/a.c', 'not a plain repository-relative path'),
            ('src//a.c', 'not a plain repository-relative path'),
            ('', 'not a plain repository-relative path'),
            ('src/untracked.c', 'exists but is not tracked'),
            ('src', 'not a regular file'),
            ('docs/new.md', 'docs holds no tracked file'),
            ('tests/test_a.c', 'reached through a symbolic link'),
            ('up/repo/src/a.c', 'reached through a symbolic link'),
            ('src/a.c/x.c', 'src/a.c is not a directory'),
            ('src/x.generated.c', 'ignored'),
            ('build/new.c', 'ignored'),
            ('build/again.c', 'ignored'),
        ])

    @unittest.skipUnless(os.name == 'nt', 'a backslash or a colon is a plain file name character on POSIX')
    def test_a_windows_spelling_is_refused(self):
        top = (self.repo / 'src' / 'new.c').as_posix()
        paths = [top, top.replace('/', '\\'), 'src\\new.c', 'src/a.c:stream', 'C:src/new.c', 'src/:(glob)*.c']
        _, out = self.check(*paths)
        self.assertEqual(out['accepted'], [])
        self.assertEqual([(r['path'], r['why']) for r in out['refused']], [(p, 'not a plain repository-relative path') for p in paths])

    @unittest.skipUnless(os.name == 'nt', 'POSIX checks a symlink out as one, refused as reached through a symbolic link')
    def test_a_symlink_checked_out_as_a_file_is_refused(self):
        self.git('config', 'core.symlinks', 'false')
        (self.repo / 'src' / 'link.c').write_text('a.c')
        blob = subprocess.run(['git', 'hash-object', '-w', 'src/link.c'], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()
        self.git('update-index', '--add', '--cacheinfo', f'120000,{blob},src/link.c')
        self.git('commit', '-qm', 'link')
        code, out = self.check('src/link.c', 'src/a.c')
        self.assertEqual(code, 0)
        self.assertEqual(out['accepted'], [{'path': 'src/a.c', 'kind': 'existing'}])
        self.assertEqual(out['refused'], [{'path': 'src/link.c', 'why': 'not a regular file in the index'}])

    def test_it_runs_only_from_the_checkout_top(self):
        code, out = self.check('a.c', cwd=self.repo / 'src')
        self.assertEqual(code, 2)
        self.assertIn('run from the checkout top level', out['error'])


if __name__ == '__main__':
    unittest.main()
