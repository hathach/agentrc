"""Tests for pr-babysit's preflight.py against a real repository and a fake gh."""
import importlib.util
import io
import json
import os
import subprocess
import sys
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
VIEW = {'headRefName': 'fix', 'headRefOid': 'f' * 40, 'baseRefOid': 'b' * 40, 'headRepositoryOwner': {'id': 'x', 'login': 'someone'},
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
        self.enterContext(mock.patch.object(tempfile, 'tempdir', str(self.root)))

    def git(self, *argv, check=True):
        return subprocess.run(['git', *argv], check=check, capture_output=True, text=True).stdout

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
            code = preflight.report(preflight.collect, list(argv) or ['--pr', '7'], seal=True)
        line = json.loads(out.getvalue().splitlines()[-1])
        if 'error' in line:
            return code, line
        rest = {k: v for k, v in line.items() if k != 'seal'}
        self.assertEqual(line['seal'], sys.modules['facts'].sealed(rest)['seal'])
        return code, rest

    def test_pins_the_checkout_and_the_pr_in_the_flat_shape(self):
        self.git('remote', 'set-url', '--push', 'origin', 'git@github.com:someone/tinyusb.git')
        (self.root / 'repo' / 'junk.o').write_text('x')
        code, out = self.pin()
        self.assertEqual(code, 0)
        receipts = out.pop('receipts')
        self.assertEqual((Path(receipts).parent, list(Path(receipts).iterdir())), (self.root / 'pr-babysit-receipts', []), 'a new empty directory')
        self.assertNotEqual(self.pin()[1]['receipts'], receipts, 'one per launch')
        self.assertEqual(out, {
            'branch': 'fix', 'prBranch': 'fix', 'prHead': 'f' * 40, 'prBase': 'b' * 40, 'prRepo': 'someone/tinyusb',
            'prUrl': VIEW['url'], 'remote': 'origin', 'upstreamBranch': 'fix', 'pushUrls': ['git@github.com:someone/tinyusb.git'],
            'head': self.git('rev-parse', 'HEAD').strip(), 'dirty': ['?? junk.o'],
            'pr': 7, 'badPushUrl': ''})

    def test_a_push_url_outside_the_pr_head_repository_is_named(self):
        good = 'git@github.com:someone/tinyusb.git'
        for urls, bad in (
            # A fork PR: its URL names the base repo, so a checkout pushing there is wrong.
            (['git@github.com:hathach/tinyusb.git'], 0),
            # Case and https forms of the same repository are the same repository.
            (['https://x@GitHub.com/SomeOne/tinyusb.git/', 'ssh://git@github.com:22/someone/tinyusb'], None),
            (['git@evil.example:someone/tinyusb.git'], 0),
            (['https://github.com/someone/tinyusb-backup.git'], 0),
            # Forms git accepts as remotes but GitHub is not, or not for push.
            (['github.com/someone/tinyusb'], 0), (['file://github.com/someone/tinyusb'], 0),
            (['https://github.com/someone/tinyusb/extra'], 0), (['/srv/someone/tinyusb'], 0),
            (['http://github.com/someone/tinyusb'], 0), (['git://github.com/someone/tinyusb'], 0),
            # The wrong delimiter makes git read a local path.
            (['git@github.com/someone/tinyusb'], 0),
            ([good, 'git@evil.example:someone/tinyusb.git'], 1),
            (['https://ghe.corp.example/someone/tinyusb.git'], 0),
            # A port is ASCII digits only.
            (['ssh://git@github.com:２２/someone/tinyusb'], 0),
        ):
            self.git('config', '--unset-all', 'remote.origin.pushurl', check=False)
            for u in urls:
                self.git('config', '--add', 'remote.origin.pushurl', u)
            out = self.pin()[1]
            self.assertEqual(out['pushUrls'], urls)
            self.assertEqual(out['badPushUrl'], '' if bad is None else urls[bad], urls)

    def test_an_empty_push_url_is_named_not_passed(self):
        self.git('remote', 'set-url', '--push', 'origin', '\ngit@github.com:someone/tinyusb.git')
        out = self.pin()[1]
        self.assertEqual(out['pushUrls'], ['', 'git@github.com:someone/tinyusb.git'])
        self.assertEqual(out['badPushUrl'], '(empty push URL)')

    def test_no_push_url_or_a_pr_off_github_https_is_refused(self):
        self.git('branch', '--unset-upstream')
        self.assertEqual(self.pin()[1]['badPushUrl'], '(no push URL)')
        self.git('branch', '-u', 'origin/fix')
        self.git('remote', 'set-url', '--push', 'origin', 'git@github.com:someone/tinyusb.git')
        for url in ('https://ghe.corp.example/hathach/tinyusb/pull/7', 'http://github.com/hathach/tinyusb/pull/7'):
            self.fake_gh(json.dumps({**VIEW, 'url': url}))
            self.assertEqual(self.pin()[1]['badPushUrl'], 'git@github.com:someone/tinyusb.git', url)

    def test_a_branch_tracking_nothing_has_no_remote_and_no_push_url(self):
        self.git('branch', '--unset-upstream')
        code, out = self.pin()
        self.assertEqual((code, out['remote'], out['pushUrls']), (0, '', []))
        self.git('config', 'branch.fix.remote', 'origin')
        code, out = self.pin()
        self.assertEqual((code, out['remote'], out['pushUrls']), (0, '', []), 'a remote without a merge ref tracks nothing')

    def test_upstream_branch_is_the_remote_side_name_whatever_the_local_one(self):
        self.git('branch', '-m', 'worktree/x')
        code, out = self.pin()
        self.assertEqual((code, out['branch'], out['upstreamBranch']), (0, 'worktree/x', 'fix'))
        self.git('branch', '--unset-upstream')
        self.assertEqual(self.pin()[1]['upstreamBranch'], '')

    def test_upstream_branch_follows_the_first_merge_value_as_git_does(self):
        self.git('config', '--add', 'branch.fix.merge', 'refs/heads/other')
        self.assertEqual(self.pin()[1]['upstreamBranch'], 'fix')

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

    def test_recheck_reads_the_local_facts_without_gh(self):
        self.git('remote', 'set-url', '--push', 'origin', 'git@github.com:someone/tinyusb.git')
        self.fake_gh('', 1)
        (self.repo / 'a name.c').write_text('x')
        (self.repo / 'staged.c').write_text('x')
        self.git('add', 'staged.c')
        code, out = self.pin('--recheck')
        self.assertEqual(code, 0)
        self.assertEqual(out, {'branch': 'fix', 'pushUrls': ['git@github.com:someone/tinyusb.git'],
                               'head': self.git('rev-parse', 'HEAD').strip(), 'staged': ['staged.c'],
                               'status': ['A  staged.c', '?? a name.c']})

    def test_a_script_the_workflow_needs_and_lacks_is_a_stale_definition(self):
        there = self.root / 'there.py'
        there.write_text('')
        code, out = self.pin('--pr', '7', '--needs', str(there))
        self.assertEqual((code, out['pr']), (0, 7))
        self.fake_gh('', 1)
        code, out = self.pin('--pr', '7', '--needs', str(there), '--needs', str(self.root / 'gone.py'), '--needs', str(self.root))
        self.assertEqual(code, 2)
        self.assertEqual(out['error'], f"stale workflow definition: {self.root / 'gone.py'}, {self.root} missing; "
                                       'reload the workflow (a fresh session) before relaunching')

    def test_usage(self):
        for argv in (['--pr', 'x'], ['--pr', '7', '--recheck'], ['--frob'], ['--recheck', '--needs', 'x']):
            self.assertIn('usage', self.pin(*argv)[1]['error'], argv)


if __name__ == '__main__':
    unittest.main()
