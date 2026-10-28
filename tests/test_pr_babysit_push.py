"""Tests for pr-babysit's push.py against real bare remotes and a fake gh."""
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

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'push.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, run_gh_as  # noqa: E402
spec = importlib.util.spec_from_file_location('pr_babysit_push', SCRIPT)
push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push)



def setUpModule():
    guard_survivors(__name__)


class PushTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, git_env(), clear=True))
        self.enterContext(mock.patch.object(push, 'PR_WAIT', 0))
        self.enterContext(run_gh_as(self.root / 'gh.py'))
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

    def publish(self, *urls, sha=None, pr=None, open_pr=None):
        argv = ['--remote', 'origin', '--branch', 'fix', '--sha', sha or self.sha, '--repo', 'o/r']
        for u in urls:
            argv += ['--push-url', u]
        if open_pr is not None:
            argv += ['--open-pr', str(open_pr)]
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
        """gh as the Python script; count() reads and bumps a call counter."""
        (self.root / 'gh.py').write_text('import sys\nfrom pathlib import Path\n'
                                         f'def count(f=Path({str(self.root / "count")!r})):\n'
                                         '    n = int(f.read_text()) if f.exists() else 0\n'
                                         '    f.write_text(str(n + 1))\n'
                                         '    return n\n'
                                         f'{script}\n')

    def test_the_pr_head_is_retried_until_github_sees_the_push(self):
        count = self.root / 'count'
        self.fake_gh(f'print({self.sha!r} if count() >= 2 else {"0" * 40!r})')
        code, out = self.publish(self.a, pr=7)
        self.assertEqual(out['prHead'], self.sha)
        self.assertEqual(count.read_text().strip(), '3')

    def test_an_unreadable_pr_head_is_null_after_every_try(self):
        count = self.root / 'count'
        self.fake_gh('count(); sys.exit(1)')
        code, out = self.publish(self.a, pr=7)
        self.assertIsNone(out['prHead'])
        self.assertEqual(count.read_text().strip(), str(push.PR_TRIES))

    def test_a_pr_no_longer_open_is_not_pushed_to(self):
        for state in ('MERGED', 'CLOSED'):
            self.fake_gh('import os\n'
                         f'open({str(self.root / "gh.log")!r}, "a").write(os.environ["GH_REPO"] + " " + " ".join(sys.argv[1:]) + "\\n"); print({state!r})')
            code, out = self.publish(self.a, open_pr=7)
            self.assertEqual((code, out['pushed'], out['heads'], out['prState']), (0, False, [], state))
            self.assertEqual(out['detail'], f'PR #7 is {state.lower()}: nothing pushed')
            self.assertEqual(self.remote_head(self.a), '')
        self.assertIn('o/r pr view 7 --json state -q .state', (self.root / 'gh.log').read_text(), "PR 7 of the --repo named")

    def test_an_open_pr_is_pushed_to(self):
        self.fake_gh("print('OPEN')")
        code, out = self.publish(self.a, open_pr=7)
        self.assertEqual((code, out['pushed'], out['prState']), (0, True, 'OPEN'))
        self.assertEqual(self.remote_head(self.a), self.sha)

    def test_an_unreadable_pr_state_is_not_pushed_to(self):
        for script in ('sys.exit(1)', 'print()'):
            self.fake_gh(script)
            code, out = self.publish(self.a, open_pr=7)
            self.assertEqual((code, out['pushed'], out['heads'], out['prState']), (0, False, [], 'UNREAD'))
            self.assertEqual(out['detail'], 'PR #7 state is unreadable: nothing pushed')
            self.assertEqual(self.remote_head(self.a), '')


if __name__ == '__main__':
    unittest.main()
