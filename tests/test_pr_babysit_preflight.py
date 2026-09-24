"""Tests for pr-babysit's preflight.py against a real repository and a fake gh."""
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

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'preflight.py'
spec = importlib.util.spec_from_file_location('pr_babysit_preflight', SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
VIEW = {'headRefName': 'fix', 'headRefOid': 'f' * 40, 'headRepositoryOwner': {'id': 'x', 'login': 'someone'},
        'headRepository': {'id': 'y', 'name': 'tinyusb'}, 'url': 'https://github.com/hathach/tinyusb/pull/7'}


class PreflightTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, ENV))
        bare = str(self.root / 'origin.git')
        subprocess.run(['git', 'init', '-q', '--bare', bare], check=True)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        self.git('init', '-q', '-b', 'fix')
        self.git('remote', 'add', 'origin', bare)
        self.git('commit', '-q', '--allow-empty', '-m', 'one')
        self.git('push', '-q', '-u', 'origin', 'fix')
        self.fake_gh(json.dumps(VIEW))

    def git(self, *argv):
        return subprocess.run(['git', *argv], check=True, capture_output=True, text=True).stdout

    def fake_gh(self, answer, code=0):
        (self.root / 'answer').write_text(answer)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / 'gh').write_text(f'#!/bin/sh\ncat {self.root / "answer"}\nexit {code}\n')
        (bin_dir / 'gh').chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {'PATH': f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}))

    def pin(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = preflight.report(preflight.collect, list(argv) or ['--pr', '7'])
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_pins_the_checkout_and_the_pr_in_the_flat_shape(self):
        self.git('remote', 'set-url', '--push', 'origin', 'git@github.com:someone/tinyusb.git')
        (self.root / 'repo' / 'junk.o').write_text('x')
        code, out = self.pin()
        self.assertEqual(code, 0)
        self.assertEqual(out, {
            'branch': 'fix', 'prBranch': 'fix', 'prHead': 'f' * 40, 'prRepo': 'someone/tinyusb',
            'prUrl': VIEW['url'], 'remote': 'origin', 'pushUrls': ['git@github.com:someone/tinyusb.git'],
            'head': self.git('rev-parse', 'HEAD').strip(), 'dirty': ['?? junk.o']})

    def test_a_branch_tracking_nothing_has_no_remote_and_no_push_url(self):
        self.git('branch', '--unset-upstream')
        code, out = self.pin()
        self.assertEqual((code, out['remote'], out['pushUrls']), (0, '', []))
        self.git('config', 'branch.fix.remote', 'origin')
        code, out = self.pin()
        self.assertEqual((code, out['remote'], out['pushUrls']), (0, '', []), 'a remote without a merge ref tracks nothing')

    def test_a_remote_named_with_a_slash_is_named_whole(self):
        self.git('remote', 'rename', 'origin', 'up/stream')
        code, out = self.pin()
        self.assertEqual(out['remote'], 'up/stream')
        self.assertEqual(len(out['pushUrls']), 1)

    def test_gh_failures_and_odd_answers_are_errors(self):
        for answer, code, want in (('', 1, 'gh pr view'), ('not json', 0, 'unexpected answer'),
                                   (json.dumps({**VIEW, 'headRepository': None}), 0, 'unexpected answer')):
            self.fake_gh(answer, code)
            status, out = self.pin()
            self.assertEqual(status, 2, answer)
            self.assertIn(want, out['error'], answer)

    def test_usage(self):
        self.assertIn('usage', self.pin('--pr', 'x')[1]['error'])


if __name__ == '__main__':
    unittest.main()
