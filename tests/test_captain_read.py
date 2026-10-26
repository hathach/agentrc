import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub, tmpdir  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'captain' / 'scripts' / 'captain_read.py'
spec = importlib.util.spec_from_file_location('captain_read', SCRIPT)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)

HOOK = '#!/bin/sh\ntouch "$MARKS/{name}"\n{tail}\n'


def setUpModule():
    guard_survivors(__name__)


class RefusalTest(unittest.TestCase):
    """The allowlist, which the guard asks too: lookups pass, anything that could write or run a program does not."""

    def test_lookups_pass(self):
        co = os.path.expanduser('~/code/repo')
        for args in [f'git -C {co} status', f'git -C {co} worktree list', f'git -C {co} branch --show-current',
                     f'git -C {co} branch -a -v', f'git -C {co} log', f'git -C {co} log -n 5 --format=dated main',
                     f'git -C {co} log --max-count=100 --stat origin/master..fix2-issue',
                     f'git -C {co} reflog show -n 3 fix2-issue', f'git -C {co} show HEAD~2 --stat',
                     f'git -C {co} diff', f'git -C {co} diff --cached --name-status',
                     f'git -C {co} diff a3c6b767e HEAD --stat', f'git -C {co} diff main...topic',
                     f'git -C {co} log v1.2.3..origin/main',
                     f'git -C {co} rev-parse --verify origin/main', f'git -C {co} rev-parse --show-toplevel',
                     f'git -C {co} merge-base --is-ancestor a b', 'git -C ~/.herdr/worktrees/tinyusb/x status',
                     'gh pr view 4165 -R hathach/tinyusb', 'gh pr view 1 -R o/r --json number,headRefOid',
                     'gh pr checks 12 -R o/r', 'gh pr diff 12 -R o/r --name-only',
                     'gh pr list -R o/r --state all --limit 30 --head fix-1 --base main', 'gh issue view 7 --repo o/r',
                     'gh issue list -R o/r -s closed -L 5', 'gh run view 123 -R o/r',
                     'gh run list -R o/r --branch main --commit a3c6b767e --limit 3', f'list {co}',
                     'list ~/.cache/chief-runs', 'list ~/.cache/chief-runs tinyusb-4165', 'list ~/.claude/projects',
                     'list ~/.claude/projects/p/memory']:
            with self.subTest(args):
                self.assertIsNone(reader.refusal(args.split()))

    def test_anything_else_is_refused(self):
        co = os.path.expanduser('~/code/repo')
        for args in ['', 'ls /', f'git -C {co}', f'git {co} status', 'git -C /etc status',
                     f'git -C {co}/../../.. status', 'git -C code/repo status', f'git -C {co} push',
                     f'git -C {co} fetch', f'git -C {co} commit', f'git -C {co} config core.pager x',
                     f'git -C {co} checkout main', f'git -C {co} reset --hard', f'git -C {co} gc',
                     f'git -C {co} stash', f'git -C {co} status --porcelain', f'git -C {co} worktree add x',
                     f'git -C {co} worktree remove x', f'git -C {co} branch new', f'git -C {co} branch -D old',
                     f'git -C {co} branch --show-current -a', f'git -C {co} log --output=/tmp/x',
                     f'git -C {co} log --format=%G?', f'git -C {co} log --format=format:%h',
                     f'git -C {co} log --pretty=fuller', f'git -C {co} log -n 101', f'git -C {co} log -n 0',
                     f'git -C {co} log --ext-diff', f'git -C {co} log --show-signature', f'git -C {co} log -p',
                     f'git -C {co} log a b', f'git -C {co} log --exec=x', f'git -C {co} log -- path',
                     f'git -C {co} log HEAD@{{1}}', f'git -C {co} log -c', f'git -C {co} show',
                     f'git -C {co} show HEAD:secret', f'git -C {co} show --output=x HEAD',
                     f'git -C {co} diff --no-index a b',
                     f'git -C {co} diff a b c', f'git -C {co} diff --output=x', f'git -C {co} diff ..x',
                     f'git -C {co} diff a....b', f'git -C {co} reflog expire', f'git -C {co} reflog delete x',
                     f'git -C {co} rev-parse HEAD', f'git -C {co} rev-parse --git-path x', f'git -C {co} merge-base a',
                     f'git -c core.pager=x -C {co} log', f'git --exec-path=/tmp -C {co} log', f'git -C {co} -c x=y log',
                     f'git -C {co} log --upload-pack=x', f'git -C {co} log -x',
                     'gh pr merge 1 -R o/r', 'gh pr comment 1 -R o/r', 'gh pr review 1 -R o/r',
                     'gh pr checkout 1 -R o/r',
                     'gh pr edit 1 -R o/r', 'gh pr close 1 -R o/r', 'gh issue create -R o/r', 'gh run rerun 1 -R o/r',
                     'gh run watch 1 -R o/r', 'gh run download 1 -R o/r', 'gh api repos/o/r', 'gh api -X POST x',
                     'gh pr view 1', 'gh pr view 1 -R o', 'gh pr view 1 -R https://x/o/r', 'gh pr view x -R o/r',
                     'gh pr view 1 2 -R o/r', 'gh pr view 1 -R o/r --web', 'gh pr view 1 -R o/r --comments',
                     'gh pr view 1 -R o/r --json comments', 'gh pr view 1 -R o/r --json number,statusCheckRollup',
                     'gh pr view 1 -R o/r --jq .x', 'gh pr view 1 -R o/r --template x', 'gh pr diff 1 -R o/r',
                     'gh pr diff 1 -R o/r --name-only --json number', 'gh pr list -R o/r --limit 31',
                     'gh pr list -R o/r --state draft', 'gh pr list -R o/r 12', 'gh pr list -R o/r --search x',
                     'gh issue list -R o/r --json body', 'gh run list -R o/r --state open',
                     'gh run list -R o/r --commit xyz', 'gh run view 1 -R o/r --log', 'gh run view 1 -R o/r --web',
                     'gh repo delete o/r', 'gh auth token', 'gh extension install x', 'gh pr view 1 -R o/r -X',
                     'gh pr view 1 -R o/r --repo a/b', f'git -C {co} log -n 2 --max-count=3', 'list /etc',
                     'list', f'list {co} a b', f'list {co} ../x', f'list {co} a/b', 'list ~/.claude/projects/x',
                     'list ~/.ssh', 'list relative',
                     'gh pr list -R o/r -L 2 --limit 3']:
            with self.subTest(args):
                self.assertIsNotNone(reader.refusal(args.split()))


@unittest.skipIf(os.name == 'nt', 'the captain runs on Linux')
class HostileRepoTest(unittest.TestCase):
    """Repository config that would run a program on a read: each fixture fires under plain git, never under the
    reader, and the repository is left as it was."""

    def setUp(self):
        self.home = tmpdir(self)
        self.marks = self.home / 'marks'
        self.marks.mkdir()
        self.repo = self.home / 'code' / 'repo'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(HOME=str(self.home), MARKS=str(self.marks), GIT_CONFIG_NOSYSTEM='1')
        self.git('init', '-q', '-b', 'main', str(self.repo), cwd=self.home)
        for k, v in [('user.name', 't'), ('user.email', 't@t'), ('commit.gpgsign', 'false')]:
            self.git('config', k, v)
        (self.repo / '.gitattributes').write_text('* diff=spy\n', encoding='utf-8')
        (self.repo / 'a.txt').write_text('one\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'one')
        (self.repo / 'a.txt').write_text('two\n', encoding='utf-8')
        self.git('commit', '-q', '-am', 'two')
        self.signed()
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        for name, tail in [('fsmonitor', 'exit 1'), ('extdiff', 'exit 0'), ('textconv', 'cat "$1"'),
                           ('gpg', 'exit 1'), ('clean', 'cat')]:
            spy = bin_dir / name
            spy.write_text(HOOK.format(name=name, tail=tail), encoding='utf-8')
            spy.chmod(0o755)
        hooks = self.repo / '.git' / 'hooks'
        hook = hooks / 'post-index-change'
        hook.write_text(HOOK.format(name='hook', tail='exit 0'), encoding='utf-8')
        hook.chmod(0o755)
        for k, v in [('core.fsmonitor', f'{bin_dir}/fsmonitor'),
                     ('diff.external', f'{bin_dir}/extdiff'), ('diff.spy.textconv', f'{bin_dir}/textconv'),
                     ('gpg.program', f'{bin_dir}/gpg'), ('log.showSignature', 'true'),
                     ('format.pretty', 'format:%h %G?')]:
            self.git('config', k, v)
        (self.repo / 'a.txt').write_text('three\n', encoding='utf-8')   # a working-tree change for status/diff

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.repo, env=self.env, check=True, capture_output=True,
                              text=True).stdout

    def signed(self):
        """A commit carrying a PGP signature header, so a verifying read calls gpg.program."""
        head = self.git('cat-file', 'commit', 'HEAD')
        header, _, message = head.partition('\n\n')
        sig = 'gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQ==\n -----END PGP SIGNATURE-----'
        sha = subprocess.run(['git', 'hash-object', '-t', 'commit', '-w', '--stdin'], cwd=self.repo, env=self.env,
                             input=f'{header}\n{sig}\n\n{message}', capture_output=True, text=True, check=True)
        self.git('update-ref', 'refs/heads/main', sha.stdout.strip())

    def state(self):
        git_dir = self.repo / '.git'
        return {str(p.relative_to(git_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in git_dir.rglob('*') if p.is_file()}

    def read(self, *args, where=None):
        return subprocess.run([sys.executable, str(SCRIPT), 'git', '-C', str(where or self.repo), *args],
                              env=self.env, capture_output=True, text=True)

    def marks_left(self):
        found = sorted(p.name for p in self.marks.iterdir())
        for p in self.marks.iterdir():
            p.unlink()
        return found

    def test_the_fixtures_fire_under_plain_git(self):
        """Without this control the reader tests could pass on fixtures that never run."""
        self.git('-c', 'core.hooksPath=.git/hooks', 'status')
        self.git('log', '-1')
        self.git('diff', 'HEAD~1', 'HEAD')
        self.git('show', '--textconv', '--no-ext-diff', 'HEAD')
        left = set(self.marks_left())
        self.assertTrue({'fsmonitor', 'extdiff', 'textconv', 'gpg', 'hook'} <= left, left)

    def test_reads_run_none_of_them_and_change_nothing(self):
        before = self.state()
        for args in [('status',), ('log', '-n', '3', '--stat'), ('log', '--format=dated'), ('reflog', 'show'),
                     ('show', 'HEAD'), ('diff',), ('diff', 'HEAD~1', 'HEAD'), ('diff', 'HEAD~1..HEAD', '--stat'),
                     ('branch', '-a', '-v'), ('worktree', 'list'), ('rev-parse', '--verify', 'HEAD'),
                     ('merge-base', 'HEAD~1', 'HEAD')]:
            with self.subTest(args):
                done = self.read(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.marks_left(), [])
                self.assertNotIn('%G?', done.stdout)
        self.assertEqual(self.state(), before)
        self.assertIn('M a.txt', self.read('status').stdout)

    def test_a_local_clean_filter_refuses_working_tree_reads_only(self):
        (self.repo / '.gitattributes').write_text('* filter=spy\n', encoding='utf-8')
        self.git('config', 'filter.spy.clean', str(self.home / 'bin' / 'clean'))
        for args in [('status',), ('diff',), ('diff', 'HEAD')]:
            with self.subTest(args):
                done = self.read(*args)
                self.assertEqual(done.returncode, 2)
                self.assertIn('filter', done.stderr)
        self.assertEqual(self.read('diff', 'HEAD~1', 'HEAD', '--stat').returncode, 0)
        self.assertEqual(self.read('diff', '--cached').returncode, 0)
        self.assertEqual(self.marks_left(), [])

    def test_a_filter_from_global_config_refuses_working_tree_reads_where_a_file_takes_it(self):
        """git-lfs registers its filter system-wide; reading a repository that uses it runs it and writes objects."""
        self.git('config', '--global', 'filter.spy.process', str(self.home / 'bin' / 'clean'))
        self.assertEqual(self.read('status').returncode, 0)   # registered but used by no file here
        (self.repo / '.gitattributes').write_text('a.txt filter=spy\n', encoding='utf-8')
        done = self.read('status')
        self.assertEqual(done.returncode, 2)
        self.assertIn('filter', done.stderr)
        self.assertEqual(self.read('log', '-n', '1').returncode, 0)
        self.assertEqual(self.marks_left(), [])

    def test_the_filter_check_covers_the_whole_worktree_and_drivers_named_like_states(self):
        (self.repo / 'sub').mkdir()
        (self.repo / 'sub' / 'b.txt').write_text('b\n', encoding='utf-8')
        self.git('add', 'sub')
        self.git('commit', '-q', '-m', 'sub')
        clean = str(self.home / 'bin' / 'clean')
        self.git('config', 'filter.spy.clean', clean)
        (self.repo / '.gitattributes').write_text('a.txt filter=spy\n', encoding='utf-8')
        self.marks_left()   # the plain git setting up the test fires the fixtures
        done = self.read('status', where=self.repo / 'sub')
        self.assertEqual(done.returncode, 2, done.stdout)
        for name in ('unset', 'unspecified'):
            with self.subTest(name):
                self.git('config', '--unset-all', 'filter.spy.clean')
                self.git('config', f'filter.{name}.clean', clean)
                (self.repo / '.gitattributes').write_text(f'a.txt filter={name}\n', encoding='utf-8')
                self.assertEqual(self.read('status').returncode, 2)
                self.git('config', '--remove-section', f'filter.{name}')
                self.git('config', 'filter.spy.clean', clean)
                self.assertNotIn('clean', self.marks_left())

    def test_a_diff_never_enters_a_submodule(self):
        inner = self.home / 'code' / 'inner'
        self.git('init', '-q', '-b', 'main', str(inner), cwd=self.home)
        (inner / '.gitattributes').write_text('c.txt filter=spy\n', encoding='utf-8')
        (inner / 'c.txt').write_text('c\n', encoding='utf-8')
        self.git('-C', str(inner), 'add', '.')
        self.git('-C', str(inner), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'c')
        self.git('-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(inner), 'inner')
        self.git('commit', '-q', '-m', 'submodule')
        sub = self.repo / 'inner'
        self.git('-C', str(sub), 'config', 'filter.spy.clean', str(self.home / 'bin' / 'clean'))
        (sub / 'c.txt').write_text('changed\n', encoding='utf-8')
        self.git('config', 'diff.submodule', 'diff')
        (self.repo / 'a.txt').write_text('two\n', encoding='utf-8')
        self.marks_left()
        for args in [('diff',), ('diff', 'HEAD'), ('status',)]:
            with self.subTest(args):
                self.assertEqual(self.read(*args).returncode, 0)
                self.assertEqual(self.marks_left(), [])

    def test_a_worktree_or_git_dir_outside_the_roots_is_refused(self):
        outside = tmpdir(self)
        (outside / 'secret.txt').write_text('secret\n', encoding='utf-8')
        self.git('config', 'core.worktree', str(outside))
        for args in [('status',), ('diff',), ('log',)]:
            with self.subTest(args):
                done = self.read(*args)
                self.assertEqual(done.returncode, 2)
                self.assertIn('outside', done.stderr)
                self.assertNotIn('secret', done.stdout)
        self.git('config', '--unset', 'core.worktree')
        moved = outside / 'repo.git'
        (self.repo / '.git').rename(moved)
        (self.repo / '.git').write_text(f'gitdir: {moved}\n', encoding='utf-8')
        done = self.read('log')
        self.assertEqual(done.returncode, 2)
        self.assertIn('outside', done.stderr)

    def test_a_refused_call_runs_nothing(self):
        done = self.read('config', 'core.pager', 'x')
        self.assertEqual(done.returncode, 2)
        self.assertIn('not a lookup', done.stderr)


@unittest.skipIf(os.name == 'nt', 'the captain runs on Linux')
class ListTest(unittest.TestCase):
    def test_lists_newest_first_without_following_a_symlink(self):
        home = tmpdir(self)
        runs = home / '.cache' / 'chief-runs'
        (runs / 'old').mkdir(parents=True)
        os.utime(runs / 'old', (1, 1))
        (runs / 'new.log').write_text('x', encoding='utf-8')
        (runs / 'out').symlink_to('/etc')
        env = {**os.environ, 'HOME': str(home)}
        done = subprocess.run([sys.executable, str(SCRIPT), 'list', str(runs)], env=env, capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = [line.split()[2:] for line in done.stdout.splitlines()]
        self.assertEqual([r[-1] for r in rows][-1], 'old')
        self.assertIn(['l', rows[[r[-1] for r in rows].index('out')][1], 'out'], rows)
        self.assertNotIn('passwd', done.stdout)
        refused = subprocess.run([sys.executable, str(SCRIPT), 'list', str(runs / 'out')], env=env,
                                 capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        narrowed = subprocess.run([sys.executable, str(SCRIPT), 'list', str(runs), 'ol'], env=env,
                                  capture_output=True, text=True)
        self.assertEqual([line.split()[-1] for line in narrowed.stdout.splitlines()], ['old'])

    def test_a_file_or_missing_directory_fails_in_one_line(self):
        home = tmpdir(self)
        runs = home / '.cache' / 'chief-runs'
        runs.mkdir(parents=True)
        (runs / 'f.log').write_text('x', encoding='utf-8')
        env = {**os.environ, 'HOME': str(home)}
        for path in (runs / 'f.log', runs / 'missing'):
            with self.subTest(path.name):
                done = subprocess.run([sys.executable, str(SCRIPT), 'list', str(path)], env=env,
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertRegex(done.stderr, r'\Acaptain_read: [^\n]+\n\Z')

    def test_an_undecodable_byte_control_character_or_backslash_is_listed_escaped_one_row_each(self):
        home = tmpdir(self)
        runs = home / '.cache' / 'chief-runs'
        runs.mkdir(parents=True)
        for name in (b'good', b'bad\xff', b'bad\\xff', b'nl\nfake 1970-01-01 f 0 forged', b'esc\x1b[2J', b'c1\xc2\x85'):
            (runs / os.fsdecode(name)).write_text('x', encoding='utf-8')
        # a strict UTF-8 stdout, as under en_US.UTF-8; the suite's -X utf8 would hide it
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONUTF8'}
        env.update(HOME=str(home), LC_ALL='C.UTF-8', PYTHONIOENCODING='utf-8:strict')
        done = subprocess.run([sys.executable, str(SCRIPT), 'list', str(runs)], env=env, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(line.split(maxsplit=4)[4] for line in done.stdout.splitlines()),
                         [b'bad\\x5cxff', b'bad\\xff', b'c1\\xc2\\x85', b'esc\\x1b[2J',
                          b'good', b'nl\\x0afake 1970-01-01 f 0 forged'])

    def test_an_entry_removed_while_listing_is_skipped(self):
        home = tmpdir(self)
        runs = home / '.cache' / 'chief-runs'
        runs.mkdir(parents=True)
        for name in ('gone', 'kept'):
            (runs / name).write_text('x', encoding='utf-8')
        code = (f'import os, sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import captain_read\n'
                'scandir = os.scandir\n'
                'class Vanishing:\n'
                '    def __init__(self, path): self.it = scandir(path)\n'
                '    def __enter__(self): return self\n'
                '    def __exit__(self, *exc): self.it.close()\n'
                '    def __iter__(self):\n'
                '        for entry in self.it:\n'
                '            if entry.name == "gone": os.unlink(entry.path)\n'
                '            yield entry\n'
                'os.scandir = Vanishing\n'
                f'sys.exit(captain_read.main(["list", {str(runs)!r}]))')
        done = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'HOME': str(home)},
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([line.split()[-1] for line in done.stdout.splitlines()], ['kept'])


@unittest.skipIf(os.name == 'nt', 'the captain runs on Linux')
class FailureTest(unittest.TestCase):
    """A tool that is missing or a preflight that times out ends in one line and exit 1."""

    def setUp(self):
        self.home = tmpdir(self)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin)}

    def main(self, *args, tight_after_located=False):
        """With tight_after_located, located() keeps the default TIMEOUT so its answering rev-parse cannot lose a race
        on a loaded runner; the 0.5 s applies from the call after it."""
        code = (f'import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import captain_read\n'
                'located = captain_read.located\n'
                'def tight(*args):\n'
                '    found = located(*args)\n'
                '    captain_read.TIMEOUT = 0.5\n'
                '    return found\n'
                f'if {tight_after_located}: captain_read.located = tight\n'
                'else: captain_read.TIMEOUT = 0.5\n'
                f'sys.exit(captain_read.main({list(args)!r}))')
        done = subprocess.run([sys.executable, '-c', code], env=self.env, capture_output=True, text=True,
                              timeout=20)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertRegex(done.stderr, r'\Acaptain_read: [^\n]+\n\Z')
        return done.stderr

    def test_a_missing_git_or_gh(self):
        for args in (('git', '-C', str(self.home / 'code' / 'repo'), 'log'), ('gh', 'pr', 'view', '1', '-R', 'o/r')):
            with self.subTest(args[0]):
                self.assertIn(repr(args[0]), self.main(*args))

    def test_a_preflight_that_times_out(self):
        """located() times out on the first git call; filtered() on the first after rev-parse answered."""
        for name, answers, stuck in (('located', False, 'rev-parse'), ('filtered', True, 'ls-files')):
            with self.subTest(name):
                make_stub(self.bin, 'git', 'import os, sys, time\n'
                          f'if {answers} and "rev-parse" in sys.argv:\n'
                          '    repo = os.path.join(os.environ["HOME"], "code", "repo")\n'
                          '    print(repo, repo + "/.git", repo + "/.git", sep="\\n")\n'
                          'else:\n'
                          '    time.sleep(30)\n')
                why = self.main('git', '-C', str(self.home / 'code' / 'repo'), 'status', tight_after_located=answers)
                self.assertIn('timed out', why)
                self.assertIn(repr(stuck), why)


@unittest.skipIf(os.name == 'nt', 'the captain runs on Linux')
class RunTest(unittest.TestCase):
    def test_a_timeout_stops_a_child_that_holds_the_pipe(self):
        child = ('import subprocess, sys, time; '
                 'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); time.sleep(60)')
        code = (f'import os, sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import captain_read; '
                'captain_read.TIMEOUT = 0.5; '
                f'sys.exit(captain_read.run([sys.executable, "-c", {child!r}], os.environ))')
        done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 124, done.stderr)
        self.assertIn('stopped after', done.stderr)


@unittest.skipIf(os.name == 'nt', 'the captain runs on Linux')
class GhTest(unittest.TestCase):
    """gh runs once per lookup with the argv the reader built: bounded --json, fixed host, no pager or prompt."""

    def test_one_call_with_bounded_json(self):
        home = tmpdir(self)
        calls = home / 'calls.jsonl'
        bin_dir = home / 'bin'
        bin_dir.mkdir()
        make_stub(bin_dir, 'gh', 'import json, os, sys\n'
                  f'open({str(calls)!r}, "a").write(json.dumps([sys.argv[1:], os.environ.get("GH_HOST"), '
                  'os.environ.get("GH_PROMPT_DISABLED")]) + "\\n")\nprint("{}")\n')
        env = {**os.environ, 'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', 'GH_HOST': 'evil.example'}
        done = subprocess.run([sys.executable, str(SCRIPT), 'gh', 'pr', 'list', '-R', 'o/r'], env=env,
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, '{}\n'), done.stderr)
        [line] = calls.read_text(encoding='utf-8').splitlines()
        argv, host, prompt = json.loads(line)
        self.assertEqual(argv, ['pr', 'list', '-R', 'o/r', '--limit', '10', '--json', reader.GH['pr', 'list'][2]])
        self.assertEqual((host, prompt), ('github.com', '1'))


if __name__ == '__main__':
    unittest.main()
