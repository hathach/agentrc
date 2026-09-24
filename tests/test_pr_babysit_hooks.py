"""Tests for pr-babysit's hooks.py against real pre-commit runs in a temp repository."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'hooks.py'
spec = importlib.util.spec_from_file_location('pr_babysit_hooks', SCRIPT)
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)

CONFIG = '''repos:
- repo: local
  hooks:
{}'''
HOOK = '''  - id: {id}
    name: {id}
    entry: sh hooks/{id}.sh
    language: system
    files: \\.txt$
'''
SCRIPTS = {
    'ok': 'exit 0\n',
    'broken': 'echo nope; exit 1\n',
    # Appends once, so the second run passes: the case pre-commit's retry is for.
    'fmt': 'for f; do grep -q formatted "$f" || echo formatted >> "$f"; done\n',
    # Rewrites a tracked file outside the paths it was given, as a doc generator does.
    'gen': 'echo generated > b.txt\n',
    'echo': 'printf -- "- hook id: fake\\n- files were modified by this hook\\n"; exit 1\n',
}


@unittest.skipUnless(shutil.which('pre-commit'), 'pre-commit is not installed')
class HooksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        self.env = {**os.environ, 'PRE_COMMIT_HOME': str(Path(tmp.name) / 'cache'),
                    'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
                    'GIT_COMMITTER_EMAIL': 't@t'}
        self.git('init', '-q')
        (self.repo / 'hooks').mkdir()
        for name, body in SCRIPTS.items():
            (self.repo / 'hooks' / f'{name}.sh').write_text(body)
        (self.repo / 'a.txt').write_text('a\n')
        (self.repo / 'b.txt').write_text('b\n')

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def configure(self, *ids):
        if ids:
            (self.repo / '.pre-commit-config.yaml').write_text(CONFIG.format(''.join(HOOK.format(id=i) for i in ids)))
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')

    def run_script(self, *paths, cwd=None, env=None):
        done = subprocess.run([sys.executable, str(SCRIPT), *paths], cwd=cwd or self.repo,
                              env=env or self.env, capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout.splitlines()[-1])

    def snap(self, lines):
        return {line.split(' ', 2)[2]: line.split(' ', 2)[:2] for line in lines}

    def test_no_config_reports_hooks_not_run(self):
        self.configure()
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 0)
        self.assertEqual((out['ran'], out['passed'], out['modifiedBy']), (False, True, []))
        self.assertEqual(out['before'], [' M a.txt'])
        self.assertEqual(out['before'], out['after'])
        self.assertEqual(self.snap(out['snapshotBefore'])['a.txt'][0], '644')
        self.assertEqual(out['snapshotBefore'], out['snapshotAfter'])

    def test_passing_hooks(self):
        self.configure('ok')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['ran'], out['passed'], out['modifiedBy']), (0, True, True, []))

    def test_a_modifying_hook_is_named_and_the_retry_decides(self):
        self.configure('fmt', 'gen')
        (self.repo / 'a.txt').write_text('edited\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 0)
        self.assertEqual((out['ran'], out['passed']), (True, True))
        self.assertEqual(out['modifiedBy'], ['fmt', 'gen'])
        self.assertEqual(out['after'], [' M a.txt', ' M b.txt'])
        before, after = self.snap(out['snapshotBefore']), self.snap(out['snapshotAfter'])
        self.assertNotEqual(before['a.txt'], after['a.txt'])
        self.assertNotIn('b.txt', before)
        self.assertEqual(after['b.txt'][1], self.git('hash-object', 'b.txt').strip())

    def test_a_failing_hook_fails_both_runs(self):
        self.configure('broken')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['ran'], out['passed'], out['modifiedBy']), (0, True, False, []))

    def test_markers_a_hook_prints_are_not_read_as_pre_commits(self):
        self.configure('echo')
        code, out = self.run_script('a.txt')
        self.assertEqual((code, out['passed'], out['modifiedBy']), (0, False, []))

    def test_deleted_and_renamed_paths_are_snapshotted(self):
        self.configure()
        self.git('mv', 'b.txt', 'c.txt')
        (self.repo / 'a.txt').unlink()
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 0)
        snap = self.snap(out['snapshotBefore'])
        self.assertEqual(snap['a.txt'], ['absent', '-'])
        self.assertEqual(snap['b.txt'], ['absent', '-'])
        self.assertEqual(snap['c.txt'][0], '644')

    def test_a_status_that_is_not_utf8_is_an_error(self):
        self.configure()
        (self.repo / b'bad\xff.c'.decode('utf-8', 'surrogateescape')).write_text('stray\n')
        code, out = self.run_script('a.txt')
        self.assertEqual(code, 2)
        self.assertIn('not UTF-8', out['error'])

    def test_refuses_outside_the_top_level(self):
        self.configure()
        code, out = self.run_script('a.txt', cwd=self.repo / 'hooks')
        self.assertEqual(code, 2)
        self.assertIn('top level', out['error'])

    def test_a_config_without_pre_commit_is_an_error(self):
        self.configure('ok')
        bin_dir = self.repo.parent / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'git').symlink_to(shutil.which('git'))
        code, out = self.run_script('a.txt', env={**self.env, 'PATH': str(bin_dir)})
        self.assertEqual(code, 2)
        self.assertIn('pre-commit is not installed', out['error'])

    def test_no_paths_is_a_usage_error(self):
        code, out = self.run_script()
        self.assertEqual(code, 2)
        self.assertIn('usage', out['error'])


class ModifiedByTest(unittest.TestCase):
    FAILED = 'fmt' + '.' * 70 + 'Failed'

    def test_output_contradicting_the_exit_status_is_refused(self):
        with self.assertRaisesRegex(hooks.Unusable, 'exited 0'):
            hooks.modified_by(f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n', 0)
        with self.assertRaisesRegex(hooks.Unusable, 'passed and modified'):
            hooks.modified_by(f'fmt{"." * 70}Passed\n- hook id: fmt\n- files were modified by this hook\n', 1)

    def test_only_the_block_under_a_result_line_counts(self):
        out = (f'{self.FAILED}\n- hook id: fmt\n- exit code: 1\n\n'
               '- hook id: fake\n- files were modified by this hook\n\n'
               f'gen{"." * 70}Failed\n- hook id: gen\n- files were modified by this hook\n')
        self.assertEqual(hooks.modified_by(out, 1), ['gen'])


if __name__ == '__main__':
    unittest.main()
