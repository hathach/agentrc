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
from types import SimpleNamespace
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'preflight.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, run_gh_as  # noqa: E402
spec = importlib.util.spec_from_file_location('pr_babysit_preflight', SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

VIEW = {'headRefName': 'fix', 'headRefOid': 'f' * 40, 'baseRefOid': 'b' * 40, 'baseRefName': 'master',
        'headRepositoryOwner': {'id': 'x', 'login': 'someone'},
        'headRepository': {'id': 'y', 'name': 'tinyusb'}, 'url': 'https://github.com/hathach/tinyusb/pull/7', 'state': 'OPEN'}
REF = {'ref': 'refs/heads/master', 'object': {'type': 'commit', 'sha': 'c' * 40}}


def setUpModule():
    guard_survivors(__name__)


class PreflightTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, git_env(), clear=True))
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
        self.enterContext(run_gh_as(self.root / 'gh.py'))
        self.enterContext(mock.patch.object(tempfile, 'tempdir', str(self.root)))

    def git(self, *argv, check=True):
        return subprocess.run(['git', *argv], check=check, capture_output=True, text=True).stdout

    def fake_gh(self, answer, code=0, ref=json.dumps(REF), ref_code=0):
        """`gh pr ...` answers answer, `gh api ...` ref; each call's argv is appended to gh.log, its GH_REPO to gh.repo."""
        (self.root / 'answer').write_text(answer)
        (self.root / 'ref').write_text(ref)
        (self.root / 'gh.py').write_text('import os, sys\n'
                                         f'open({str(self.root / "gh.log")!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                                         f'open({str(self.root / "gh.repo")!r}, "a").write(os.environ.get("GH_REPO", "-") + "\\n")\n'
                                         f'api = sys.argv[1:2] == ["api"]\n'
                                         f'sys.stdout.write(open({str(self.root / "ref")!r} if api else {str(self.root / "answer")!r}).read())\n'
                                         f'sys.exit({ref_code} if api else {code})\n')

    def gh_calls(self, log='gh.log'):
        log = self.root / log
        return log.read_text().splitlines() if log.exists() else []

    def pin(self, *argv, repo='hathach/tinyusb'):
        """preflight.py's line for argv (default --pr 7), --repo repo added unless argv is --recheck or repo is None."""
        argv = list(argv) or ['--pr', '7']
        argv += ['--repo', repo] if repo and '--recheck' not in argv else []
        out = io.StringIO()
        with redirect_stdout(out):
            code = preflight.report(preflight.collect, argv, seal=True)
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
        self.assertNotIn('\\', receipts, 'a Windows dir is spelled C:/...')
        self.assertEqual((Path(receipts).parent, list(Path(receipts).iterdir())), (self.root / 'pr-babysit-receipts', []), 'a new empty directory')
        self.assertNotEqual(self.pin()[1]['receipts'], receipts, 'one per launch')
        self.assertEqual(out, {
            'python': Path(sys.executable).as_posix(), 'missing': [], 'repo': 'hathach/tinyusb', 'branch': 'fix', 'prBranch': 'fix', 'prHead': 'f' * 40, 'prBase': 'b' * 40, 'prBaseBranch': 'master', 'prBaseTip': 'c' * 40,
            'prRepo': 'someone/tinyusb',
            'prUrl': VIEW['url'], 'prState': 'OPEN', 'remote': 'origin', 'upstreamBranch': 'fix', 'pushUrls': ['git@github.com:someone/tinyusb.git'],
            'head': self.git('rev-parse', 'HEAD').strip(), 'dirty': ['?? junk.o'],
            'pr': 7, 'badPushUrl': ''})
        self.assertEqual(self.gh_calls()[-1], 'api repos/{owner}/{repo}/git/ref/heads/master', 'the base repository gh resolves for the PR')
        self.assertEqual(set(self.gh_calls('gh.repo')), {'hathach/tinyusb'}, 'every gh call names the repository')

    def test_the_same_pr_number_in_two_repositories_is_read_in_the_one_named(self):
        for repo in ('adafruit/Pico-PIO-USB', 'sekigon-gonnoc/Pico-PIO-USB'):
            (self.root / f"answer-{repo.split('/')[0]}").write_text(json.dumps({**VIEW, 'url': f'https://github.com/{repo}/pull/7'}))
        (self.root / 'gh.py').write_text('import os, sys\n'
                                         'api = sys.argv[1:2] == ["api"]\n'
                                         f'name = {str(self.root)!r} + ("/ref" if api else "/answer-" + os.environ["GH_REPO"].split("/")[0])\n'
                                         'sys.stdout.write(open(name).read())\n')
        for repo in ('adafruit/Pico-PIO-USB', 'sekigon-gonnoc/Pico-PIO-USB'):
            code, out = self.pin(repo=repo)
            self.assertEqual((code, out['repo'], out['prUrl']), (0, repo, f'https://github.com/{repo}/pull/7'))

    def test_without_repo_the_checkouts_one_github_repository_is_named(self):
        self.git('remote', 'add', 'adafruit', 'https://github.com/adafruit/Pico-PIO-USB.git')
        code, out = self.pin(repo=None)
        self.assertEqual((code, out['repo']), (0, 'adafruit/Pico-PIO-USB'), 'origin is a local path, no GitHub repository')
        self.assertEqual(set(self.gh_calls('gh.repo')), {'adafruit/Pico-PIO-USB'})
        self.git('remote', 'add', 'sekigon', 'git@github.com:sekigon-gonnoc/Pico-PIO-USB.git')
        self.git('remote', 'add', 'same', 'https://github.com/ADAFRUIT/pico-pio-usb')
        before = self.gh_calls()
        code, out = self.pin(repo=None)
        self.assertEqual((code, out['error']), (2, 'the remotes name 2 GitHub repositories (adafruit adafruit/Pico-PIO-USB, same ADAFRUIT/pico-pio-usb, '
                                                    'sekigon sekigon-gonnoc/Pico-PIO-USB) and none is marked gh-resolved: '
                                                    "name the PR's repository (--repo OWNER/NAME)"))
        self.assertEqual(self.gh_calls(), before, 'nothing is read from a guessed repository')
        for marks, want in (({'sekigon': 'base'}, 'sekigon-gonnoc/Pico-PIO-USB'), ({'origin': 'adafruit/Pico-PIO-USB'}, 'adafruit/Pico-PIO-USB'),
                            ({'adafruit': 'base', 'same': 'base'}, 'adafruit/Pico-PIO-USB')):
            with self.subTest(marks):
                for remote, mark in marks.items():
                    self.git('config', f'remote.{remote}.gh-resolved', mark)
                code, out = self.pin(repo=None)
                self.assertEqual((code, out['repo']), (0, want))
                for remote in marks:
                    self.git('config', '--unset', f'remote.{remote}.gh-resolved')
        self.git('config', 'remote.adafruit.gh-resolved', 'base')
        self.git('config', 'remote.sekigon.gh-resolved', 'base')
        self.assertIn('the remotes marked gh-resolved name adafruit adafruit/Pico-PIO-USB, sekigon sekigon-gonnoc/Pico-PIO-USB:',
                      self.pin(repo=None)[1]['error'])
        self.git('config', '--unset', 'remote.sekigon.gh-resolved')
        self.git('config', 'remote.origin.gh-resolved', 'base')
        self.assertIn('origin (not a GitHub repository)', self.pin(repo=None)[1]['error'], 'base on a remote off GitHub')
        self.git('config', '--unset', 'remote.origin.gh-resolved')
        # A worktree's own value overrides the shared one, as git reads it.
        self.git('config', 'extensions.worktreeConfig', 'true')
        self.git('config', 'remote.adafruit.gh-resolved', 'sekigon-gonnoc/Pico-PIO-USB')
        self.git('config', '--worktree', 'remote.adafruit.gh-resolved', 'adafruit/Pico-PIO-USB')
        self.assertEqual(self.pin(repo=None)[1]['repo'], 'adafruit/Pico-PIO-USB')
        self.git('config', '--worktree', '--unset', 'remote.adafruit.gh-resolved')
        self.git('config', '--unset', 'remote.adafruit.gh-resolved')
        for remote in ('adafruit', 'sekigon', 'same'):
            self.git('remote', 'remove', remote)
        self.assertEqual(self.pin(repo=None)[1]['error'], "no remote names a github.com repository: name the PR's repository (--repo OWNER/NAME)")

    def test_the_base_branch_is_requested_quoted(self):
        name = 'rel/v1#x{branch}'
        self.fake_gh(json.dumps({**VIEW, 'baseRefName': name}), ref=json.dumps({**REF, 'ref': f'refs/heads/{name}'}))
        code, out = self.pin()
        self.assertEqual((code, out['prBaseBranch'], out['prBaseTip']), (0, name, 'c' * 40))
        self.assertEqual(self.gh_calls()[-1], 'api repos/{owner}/{repo}/git/ref/heads/rel/v1%23x%7Bbranch%7D')

    def test_a_base_tip_gh_cannot_vouch_for_is_an_error(self):
        for ref, code, want in (('', 1, 'gh api'), ('not json', 0, 'unexpected answer'),
                                (json.dumps({**REF, 'ref': 'refs/heads/main'}), 0, 'unexpected answer'),
                                (json.dumps([REF]), 0, 'unexpected answer'),
                                (json.dumps({**REF, 'object': {'type': 'tag', 'sha': 'c' * 40}}), 0, 'unexpected answer'),
                                (json.dumps({**REF, 'object': {'type': 'commit', 'sha': 'c' * 7}}), 0, 'unexpected answer'),
                                (json.dumps({**REF, 'object': {'type': 'commit', 'sha': 'c' * 40 + '\n'}}), 0, 'unexpected answer')):
            self.fake_gh(json.dumps(VIEW), ref=ref, ref_code=code)
            status, out = self.pin()
            self.assertEqual(status, 2, ref)
            self.assertIn(want, out['error'], ref)

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
                                   (json.dumps({**VIEW, 'headRepository': None}), 0, 'unexpected answer'),
                                   (json.dumps({**VIEW, 'state': 'DRAFT'}), 0, "state 'DRAFT'")):
            self.fake_gh(answer, code)
            status, out = self.pin()
            self.assertEqual(status, 2, answer)
            self.assertIn(want, out['error'], answer)

    def test_a_pr_no_longer_open_is_pinned_with_its_state(self):
        for state in ('MERGED', 'CLOSED'):
            self.fake_gh(json.dumps({**VIEW, 'state': state}))
            code, out = self.pin()
            self.assertEqual((code, out['prState']), (0, state))

    def test_a_pr_no_longer_open_pins_without_its_base_tip(self):
        for state in ('MERGED', 'CLOSED'):
            self.fake_gh(json.dumps({**VIEW, 'state': state}), ref='', ref_code=1)
            code, out = self.pin()
            self.assertEqual((code, out['prState'], out['prBaseTip']), (0, state, ''))
            self.assertFalse(any(c.startswith('api ') for c in self.gh_calls()), 'a deleted base branch cannot fail the pin')

    def test_a_pr_no_longer_open_whose_head_fork_is_gone_is_pinned_with_its_state(self):
        for state in ('MERGED', 'CLOSED'):
            for gone in ({'headRepository': None}, {'headRepository': None, 'headRepositoryOwner': None}):
                self.fake_gh(json.dumps({**VIEW, 'state': state, **gone}))
                code, out = self.pin()
                self.assertEqual((code, out['prState'], out['prRepo']), (0, state, ''), gone)
        self.fake_gh(json.dumps({**VIEW, 'headRepositoryOwner': None}))
        self.assertEqual(self.pin()[0], 2, 'an open PR is not pinned without its head repository')

    def test_state_reads_the_pr_state_alone(self):
        for state in ('OPEN', 'MERGED', 'CLOSED'):
            self.fake_gh(json.dumps({'state': state}))
            self.assertEqual(self.pin('--state', '7'), (0, {'state': state}))
        self.assertEqual(self.gh_calls()[-1], 'pr view 7 --json state')
        self.assertEqual(set(self.gh_calls('gh.repo')), {'hathach/tinyusb'})
        for answer, code, want in (('', 1, 'gh pr view'), ('not json', 0, 'unexpected answer'), ('{}', 0, 'unexpected answer'),
                                   (json.dumps({'state': 'DRAFT'}), 0, "state 'DRAFT'")):
            self.fake_gh(answer, code)
            status, out = self.pin('--state', '7')
            self.assertEqual(status, 2, answer)
            self.assertIn(want, out['error'], answer)
        status, out = self.pin('--state', '7', '--needs', 'x')
        self.assertEqual((status, out['error']), (2, 'usage: --needs and --module are for --pr'))

    def test_recheck_reads_the_local_facts_without_gh(self):
        self.git('remote', 'set-url', '--push', 'origin', 'git@github.com:someone/tinyusb.git')
        self.fake_gh('', 1)
        (self.repo / 'a name.c').write_text('x')
        (self.repo / 'staged.c').write_text('x')
        (self.repo / 'new').mkdir()
        (self.repo / 'new' / 'x.c').write_text('x')
        self.git('add', 'staged.c')
        code, out = self.pin('--recheck')
        self.assertEqual((code, self.gh_calls()), (0, []))
        self.assertEqual(out, {'branch': 'fix', 'pushUrls': ['git@github.com:someone/tinyusb.git'],
                               'head': self.git('rev-parse', 'HEAD').strip(), 'staged': ['staged.c'],
                               'status': ['A  staged.c', '?? a name.c', '?? new/x.c']}, 'a new directory by its files (#322)')

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

    def test_the_interpreter_names_itself_and_the_modules_it_cannot_import(self):
        code, out = self.pin('--pr', '7', '--module', 'json', '--module', 'no_such_module_x')
        self.assertEqual((code, out['python'], out['missing']),
                         (0, Path(sys.executable).as_posix(), ["no_such_module_x: ModuleNotFoundError: No module named 'no_such_module_x'"]))
        # Found but not loadable: a yaml.py that raises, as a broken install would.
        lib = self.root / 'lib'
        lib.mkdir()
        (lib / 'broken_yaml_x.py').write_text("raise ImportError('libyaml.so: cannot open shared object file')\n")
        self.enterContext(mock.patch.object(sys, 'path', [str(lib), *sys.path]))
        self.assertEqual(self.pin('--pr', '7', '--module', 'broken_yaml_x')[1]['missing'],
                         ['broken_yaml_x: ImportError: libyaml.so: cannot open shared object file'])
        # preflight's own sys: the fake gh runs under the real sys.executable.
        with mock.patch.object(preflight, 'sys', SimpleNamespace(executable='/opt/My Python/bin/python3')):
            self.assertEqual(self.pin()[1]['python'], '/opt/My Python/bin/python3', 'a path with spaces, as is')
        with mock.patch.object(preflight, 'sys', SimpleNamespace(executable='')):
            self.assertEqual(self.pin()[1]['error'], 'this interpreter does not know its own path (sys.executable is empty)')
        for argv in (['--recheck', '--module', 'yaml'], ['--pr', '7', '--module', 'os.path']):
            self.assertIn('usage', self.pin(*argv)[1]['error'], argv)

    def test_usage(self):
        for argv in (['--pr', 'x'], ['--pr', '7', '--recheck'], ['--frob'], ['--recheck', '--needs', 'x'], ['--recheck', '--repo', 'o/r']):
            self.assertIn('usage', self.pin(*argv)[1]['error'], argv)
        for repo in ('o', 'o/r/x', '_o/r', 'o/..', 'o/r x'):
            self.assertEqual(self.pin(repo=repo)[1]['error'], f'not a repository OWNER/NAME: {repo!r}')
        self.assertEqual(self.gh_calls(), [])


if __name__ == '__main__':
    unittest.main()
