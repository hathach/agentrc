"""Tests for worktree-reset's worktree_reset.py on real git repos: a bare origin, a primary
checkout and a linked worktree `slot`; GitHub, chief detection and cowork are stubbed."""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'worktree-reset' / 'scripts' / 'worktree_reset.py'
spec = importlib.util.spec_from_file_location('worktree_reset', SCRIPT)
wr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wr)


def sh(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Slot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        cfg = self.tmp / 'gitconfig'
        # user config that would hide untracked files and recurse submodules: the script's
        # explicit options must win
        cfg.write_text('[user]\n name = t\n email = t@t\n[status]\n showUntrackedFiles = no\n'
                       '[submodule]\n recurse = true\n[init]\n defaultBranch = main\n')
        env = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(cfg), 'GIT_CONFIG_NOSYSTEM': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.origin, self.primary, self.slot = self.tmp / 'origin.git', self.tmp / 'primary', self.tmp / 'slot'
        sh(self.tmp, 'init', '-q', '--bare', str(self.origin))
        sh(self.tmp, 'clone', '-q', str(self.origin), str(self.primary))
        (self.primary / '.gitignore').write_text('hil_report.md\ndeps\nbuild/\nscratch\n')
        (self.primary / 'README').write_text('r\n')
        self.commit(self.primary, 'seed')
        sh(self.primary, 'push', '-q', 'origin', 'main')
        sh(self.primary, 'worktree', 'add', '-q', str(self.slot), '-b', 'slot')
        self.prs, self.threads, self.chiefs, self.cowork = [], [], [], []
        for name, fn in (('github_repo', lambda root: 'o/r'),
                         ('pulls', lambda repo, prefix: [pr for pr in self.prs if pr['ref'].startswith(prefix)]),
                         ('unresolved_threads', lambda repo, n: self.threads),
                         ('running_chiefs', lambda root: self.chiefs), ('cowork_reset', self.fake_cowork)):
            p = mock.patch.object(wr, name, fn)
            p.start()
            self.addCleanup(p.stop)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.slot)

    def fake_cowork(self, root, side):
        self.cowork.append((side, sh(root, 'branch', '--show-current')))
        return ''

    def commit(self, repo, msg, name='f', text=None):
        (repo / name).write_text(text or msg)
        sh(repo, 'add', '-A')
        sh(repo, 'commit', '-q', '-m', msg)
        return sh(repo, 'rev-parse', 'HEAD')

    def merged_ff(self):
        self.commit(self.slot, 'work')
        sh(self.slot, 'push', '-q', 'origin', 'slot:main')

    def run_reset(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = wr.main(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def track_on_base(self, path):
        sh(self.primary, 'pull', '-q')
        target = self.primary / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('tracked')
        sh(self.primary, 'add', '-f', path)
        sh(self.primary, 'commit', '-q', '-m', f'track {path}')
        sh(self.primary, 'push', '-q', 'origin', 'main')

    def branch(self):
        return sh(self.slot, 'branch', '--show-current')

    def main_sha(self):
        return sh(self.origin, 'rev-parse', 'main')


class Merge(Slot):
    def test_fast_forward_merge_resets_to_the_next_number(self):
        self.merged_ff()
        rc, out = self.run_reset()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.branch(), sh(self.slot, 'rev-parse', 'HEAD')), ('slot-2', self.main_sha()))
        self.assertNotIn('slot', sh(self.slot, 'branch', '--format=%(refname:short)').split())
        self.assertNotEqual(subprocess.run(['git', 'config', 'branch.slot-2.merge'], cwd=self.slot).returncode, 0,
                            'no upstream')
        self.assertEqual(self.cowork, [('codex', 'slot'), ('claude', 'slot')], 'lanes reset on the old HEAD')

    def test_squash_merged_pr_is_proof(self):
        head = self.commit(self.slot, 'work')
        self.commit(self.primary, 'squash of work', text='work')
        sh(self.primary, 'push', '-q', 'origin', 'main')
        self.prs = [{'number': 7, 'ref': 'slot', 'sha': head, 'merged_at': 't', 'merge_commit_sha': self.main_sha()}]
        rc, out = self.run_reset()
        self.assertEqual(rc, 0, out)
        self.assertIn('merged as PR #7', out)

    def test_unmerged_work_is_refused_untouched(self):
        head = self.commit(self.slot, 'mine')
        for prs in ([], [{'number': 7, 'ref': 'slot', 'sha': head, 'merged_at': None, 'merge_commit_sha': None}],
                    [{'number': 7, 'ref': 'slot', 'sha': 'f' * 40, 'merged_at': 't', 'merge_commit_sha': self.main_sha()}]):
            with self.subTest(prs=prs):
                self.prs = prs
                rc, out = self.run_reset()
                self.assertEqual(rc, 3, out)
                self.assertIn('mine', out)
                self.assertEqual((self.branch(), self.cowork), ('slot', []))

    def test_stale_origin_head_is_not_the_base(self):
        sh(self.primary, 'push', '-q', 'origin', 'main:old')
        self.merged_ff()
        sh(self.slot, 'fetch', '-q', 'origin')
        sh(self.slot, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/old')
        rc, out = self.run_reset()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh(self.slot, 'rev-parse', 'HEAD'), self.main_sha())


class Refusals(Slot):
    def assertRefused(self, text):
        rc, out = self.run_reset()
        self.assertEqual(rc, 3, out)
        self.assertIn(text, out)
        self.assertEqual((self.branch(), self.cowork), ('slot', []))

    def test_untracked_file_despite_user_config(self):
        self.merged_ff()
        (self.slot / 'new').write_text('x')
        self.assertRefused('not clean')

    def test_primary_checkout(self):
        os.chdir(self.primary)
        rc, out = self.run_reset()
        self.assertEqual(rc, 3)
        self.assertIn('primary checkout', out)

    def test_detached_head(self):
        sh(self.slot, 'switch', '-q', '--detach')
        rc, out = self.run_reset()
        self.assertEqual(rc, 3)
        self.assertIn('detached', out)

    def test_running_chief(self):
        self.merged_ff()
        self.chiefs = [4242]
        self.assertRefused('pid 4242')

    def test_ignored_file_the_base_now_tracks(self):
        self.merged_ff()
        (self.slot / 'hil_report.md').write_text('mine')
        self.track_on_base('hil_report.md')
        self.assertRefused('hil_report.md')
        self.assertEqual((self.slot / 'hil_report.md').read_text(), 'mine')

    def swap(self, here, tracked):
        self.merged_ff()
        (self.slot / here).parent.mkdir(parents=True, exist_ok=True)
        (self.slot / here).write_text('mine')
        self.track_on_base(tracked)
        self.assertRefused(tracked)

    def test_ignored_file_where_the_base_has_a_directory(self):
        self.swap('scratch', 'scratch/new')

    def test_ignored_directory_where_the_base_has_a_file(self):
        self.swap('build/out', 'build')

    def test_ignored_file_below_a_path_the_base_has_as_a_file(self):
        self.swap('tools/deps', 'tools')    # `deps` ignores tools/deps while tools/ is not ignored


class Naming(Slot):
    def test_highest_suffix_from_every_source_plus_one(self):
        self.merged_ff()
        sh(self.slot, 'branch', 'slot-3')
        sh(self.primary, 'push', '-q', 'origin', 'main:slot-4')
        self.prs = [{'number': n, 'ref': ref, 'sha': 'a', 'merged_at': None, 'merge_commit_sha': None}
                    for n, ref in ((1, 'slot-5'), (2, 'slotx-9'), (3, 'slot-a'), (4, 'other-12'))]
        rc, out = self.run_reset()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.branch(), 'slot-6')



class Kept(Slot):
    def test_ignored_files_and_chief_state_stay(self):
        self.merged_ff()
        outside = self.tmp / 'deps-target'
        outside.mkdir()
        (self.slot / 'deps').symlink_to(outside)
        (self.slot / 'build').mkdir()
        (self.slot / 'hil_report.md').write_text('r')
        chief = Path(sh(self.slot, 'rev-parse', '--path-format=absolute', '--git-path', 'chief'))
        chief.mkdir()
        rc, out = self.run_reset()
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.slot / 'deps').is_symlink() and (self.slot / 'build').is_dir()
                        and (self.slot / 'hil_report.md').exists() and chief.is_dir())


class FollowUps(Slot):
    def test_pending_items_stop_until_the_human_agrees(self):
        self.merged_ff()
        rc, out = self.run_reset('--pending', 'rerun f723 at HEAD')
        self.assertEqual(rc, 4, out)
        self.assertIn('rerun f723 at HEAD', out)
        self.assertEqual((self.branch(), self.cowork), ('slot', []))
        rc, out = self.run_reset('--pending', 'rerun f723 at HEAD', '--proceed')
        self.assertEqual((rc, self.branch()), (0, 'slot-2'), out)

    def test_unresolved_thread_of_the_latest_merged_pr(self):
        self.merged_ff()
        self.prs = [{'number': 9, 'ref': 'slot', 'sha': 'a', 'merged_at': '2026', 'merge_commit_sha': 'b'}]
        self.threads = ['https://github.com/o/r/pull/9#discussion_r1']
        rc, out = self.run_reset()
        self.assertEqual(rc, 4, out)
        self.assertIn('discussion_r1', out)


class Apply(Slot):
    def test_dry_run_changes_nothing(self):
        self.merged_ff()
        rc, out = self.run_reset('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertIn('slot-2', out)
        self.assertEqual((self.branch(), self.cowork), ('slot', []))

    def test_cowork_refusal_is_a_partial_failure_before_the_switch(self):
        self.merged_ff()

        def refuse(root, side):
            raise wr.Refused('lane busy')
        with mock.patch.object(wr, 'cowork_reset', refuse):
            rc, out = self.run_reset()
        self.assertEqual(rc, 5, out)
        self.assertIn('lane busy', out)
        self.assertEqual(self.branch(), 'slot')

    def test_cowork_failing_part_way_keeps_what_it_did(self):
        self.merged_ff()

        def partial(root, side):
            raise wr.Refused('cowork.py reset codex all: codex/aaa: session forgotten\ncodex/zzz is busy')
        with mock.patch.object(wr, 'cowork_reset', partial):
            rc, out = self.run_reset()
        self.assertEqual(rc, 5, out)
        self.assertIn('aaa: session forgotten', out)
        self.assertIn('cowork codex reset run', out)

    def test_a_failed_command_refuses_with_its_stdout(self):
        with self.assertRaises(wr.Refused) as e:
            wr.run(['sh', '-c', 'echo did-$((1 + 1)); echo then-failed >&2; exit 1'])
        self.assertIn('did-2', str(e.exception))


class PrSearch(unittest.TestCase):
    def test_pr_search_maps_nodes_and_refuses_a_short_answer(self):
        node = {'number': 7, 'headRefName': 'slot-2', 'headRefOid': 'a', 'mergedAt': None, 'mergeCommit': None}
        for count, want in ((1, [{'number': 7, 'ref': 'slot-2', 'sha': 'a', 'merged_at': None,
                                   'merge_commit_sha': None}]), (2, None)):
            page = json.dumps({'issueCount': count, 'nodes': [node]})
            with self.subTest(count=count), mock.patch.object(wr, 'gh_lines', lambda *a: [page]):
                if want is None:
                    self.assertRaises(wr.Refused, wr.pulls, 'o/r', 'slot')
                else:
                    self.assertEqual(wr.pulls('o/r', 'slot'), want)


if __name__ == '__main__':
    unittest.main()
