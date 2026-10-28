import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, remotes_checkout  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import gh_repo  # noqa: E402


def setUpModule():
    guard_survivors(__name__)
    unittest.enterModuleContext(mock.patch.dict(os.environ, git_env(), clear=True))


class CheckoutRepoTest(unittest.TestCase):
    def config(self, path, *argv):
        subprocess.run(['git', '-C', str(path), 'config', *argv], check=True)

    def refusal(self, path):
        with self.assertRaises(gh_repo.Unresolved) as cm:
            gh_repo.checkout_repo(path)
        return str(cm.exception)

    def test_the_one_github_repository_any_remote_names(self):
        path = remotes_checkout(self, ('local', '/srv/mirror.git'), ('origin', 'git@github.com:hathach/tinyusb.git'))
        self.assertEqual(gh_repo.checkout_repo(path), 'hathach/tinyusb')

    def test_remotes_differing_only_in_case_are_one_repository_spelled_as_the_first(self):
        path = remotes_checkout(self, ('a', 'https://github.com/Adafruit/TinyUF2'), ('b', 'ssh://git@github.com/adafruit/tinyuf2.git'))
        self.assertEqual(gh_repo.checkout_repo(path), 'Adafruit/TinyUF2')

    def test_several_repositories_none_marked_refuse(self):
        path = remotes_checkout(self, ('origin', 'https://github.com/me/fork.git'), ('upstream', 'https://github.com/o/r.git'))
        self.assertEqual(self.refusal(path), 'the remotes name 2 GitHub repositories (origin me/fork, upstream o/r) '
                                             'and none is marked gh-resolved')

    def test_no_github_remote_refuses(self):
        self.assertEqual(self.refusal(remotes_checkout(self)), 'no remote names a github.com repository')
        path = remotes_checkout(self, ('origin', 'https://gitlab.com/o/r.git'), ('bad', 'git@github.com/o/r'))
        self.assertEqual(self.refusal(path), 'no remote names a github.com repository', 'scp-like needs a colon after the host')

    def test_gh_resolved_names_the_repository_of_a_fork_of_a_fork(self):
        path = remotes_checkout(self, ('origin', 'https://github.com/me/fork.git'), ('mid', 'https://github.com/mid/fork.git'),
                                ('upstream', 'https://github.com/o/r.git'))
        self.config(path, 'remote.upstream.gh-resolved', 'base')
        self.assertEqual(gh_repo.checkout_repo(path), 'o/r')
        self.config(path, 'remote.upstream.gh-resolved', 'mid/fork')
        self.assertEqual(gh_repo.checkout_repo(path), 'mid/fork', 'a value other than base names the repository itself')

    def test_the_effective_gh_resolved_is_the_last_one_git_lists(self):
        path = remotes_checkout(self, ('origin', 'https://github.com/me/fork.git'), ('upstream', 'https://github.com/o/r.git'))
        glob = path / 'global'
        glob.write_text('[remote "upstream"]\n\tgh-resolved = me/fork\n')
        self.config(path, 'remote.upstream.gh-resolved', 'base')
        with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(glob)}):
            self.assertEqual(gh_repo.checkout_repo(path), 'o/r', 'the repository config overrides the global one')

    def test_a_marked_value_that_is_not_owner_name_refuses(self):
        path = remotes_checkout(self, ('origin', 'https://github.com/o/r.git'))
        for bad in ('bogus', 'o/r/x', '../r'):
            self.config(path, 'remote.origin.gh-resolved', bad)
            self.assertEqual(self.refusal(path), f'gh-resolved names {bad!r}, not a repository OWNER/NAME')

    def test_two_marked_remotes_naming_different_repositories_refuse(self):
        path = remotes_checkout(self, ('origin', 'https://github.com/me/fork.git'), ('upstream', 'https://github.com/o/r.git'))
        self.config(path, 'remote.origin.gh-resolved', 'base')
        self.config(path, 'remote.upstream.gh-resolved', 'base')
        self.assertEqual(self.refusal(path), 'the remotes marked gh-resolved name origin me/fork, upstream o/r')


if __name__ == '__main__':
    unittest.main()
