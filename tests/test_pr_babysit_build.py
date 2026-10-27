"""Tests for pr-babysit's build.py against fake build commands in a temp repository."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'build.py'
spec = importlib.util.spec_from_file_location('pr_babysit_build', SCRIPT)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def setUpModule():
    guard_survivors(__name__)


class BuildTest(unittest.TestCase):
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
        code, out = self.run_script('--path=.', '--command', 'grep -q "int ok;" src.c && touch <BUILD>/out')
        self.assertEqual(code, 0)
        self.assertEqual((out['revision'], out['exit']), (self.base, 1))
        self.assertIn(out['buildDir'], out['command'])
        self.assertNotIn('\\', out['buildDir'], 'a Windows dir is spelled C:/..., which bash reads unquoted')
        self.assertNotIn('<BUILD>', out['command'])
        self.assertEqual(out['retained'], [])
        self.assertFalse(os.path.exists(out['buildDir']))
        self.assertIn('$ grep -q', Path(out['log']).read_text(), 'the log is kept')
        self.assertNotIn('\\', out['log'])

    def test_the_snapshot_follows_uncommitted_and_untracked_changes(self):
        snaps = []
        for change in (lambda: None, lambda: (self.repo / 'src.c').write_text('int x;\n'),
                       lambda: (self.repo / 'new.c').write_text('a'), lambda: (self.repo / 'new.c').write_text('b')):
            change()
            snaps.append(self.run_script('--path=.', '--command', 'true')[1]['snapshot'])
        self.assertEqual(len(set(snaps)), 4)

    @unittest.skipIf(os.name == 'nt', 'Windows has no directory permission bits to keep a removal out')
    def test_a_retained_build_dir_is_reported(self):
        code, out = self.run_script('--path=.', '--command', 'mkdir <BUILD>/ro && touch <BUILD>/ro/f && chmod 555 <BUILD>/ro')
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', out['buildDir']]))
        self.addCleanup(lambda: subprocess.run(['chmod', '-R', 'u+w', out['buildDir']]))
        if os.geteuid() == 0:
            self.skipTest('root can remove a read-only directory')
        self.assertEqual((code, out['exit']), (0, 0))
        self.assertEqual(out['retained'], [out['buildDir']])

    def test_a_build_that_rewrites_the_candidate_sources_shows_in_the_snapshots(self):
        (self.repo / 'src.c').write_text('int fixed;\n')
        (self.repo / 'other.c').write_text('x')
        code, out = self.run_script('--path=src.c', '--command', 'git checkout -- src.c')
        self.assertEqual(code, 0)
        self.assertNotEqual(out['snapshot'], out['snapshotAfter'])
        code, out = self.run_script('--path=src.c', '--command', 'echo y > other.c && touch <BUILD>/o')
        self.assertEqual(out['snapshot'], out['snapshotAfter'], 'a path outside the candidate may change')

    @unittest.skipIf(os.name == 'nt', 'NTFS forbids : and * in a file name')
    def test_a_path_named_like_pathspec_magic_snapshots_only_itself(self):
        (self.repo / ':(top)*').write_text('odd')
        snap = lambda: self.run_script('--path=:(top)*', '--command', 'true')[1]['snapshot']
        before = snap()
        (self.repo / 'src.c').write_text('int x;\n')
        (self.repo / 'new.c').write_text('a')
        self.assertEqual(snap(), before, 'no other path joins the snapshot')

    def test_values_may_start_with_a_dash(self):
        code, out = self.run_script('--path=-weird', '--command=true')
        self.assertEqual((code, out['exit']), (0, 0), out)

    @unittest.skipIf(os.name == 'nt', 'chmod cannot make a file unreadable on Windows')
    def test_a_filesystem_error_is_reported_and_cleaned_up(self):
        if os.geteuid() == 0:
            self.skipTest('root reads an unreadable file')
        (self.repo / 'secret.c').write_text('x')
        os.chmod(self.repo / 'secret.c', 0)
        self.addCleanup(os.chmod, self.repo / 'secret.c', 0o644)
        before = set(Path(tempfile.gettempdir()).glob('pr-babysit-build-*'))
        code, out = self.run_script('--path=.', '--command', 'true')
        self.assertEqual(code, 2)
        self.assertIn('Permission denied', out['error'])
        self.assertEqual(set(Path(tempfile.gettempdir()).glob('pr-babysit-build-*')), before, 'the build dir is gone')

    def toolchain(self):
        """An export script that puts a fake idf.py on PATH, as ESP-IDF's does."""
        tools = Path(self.repo.parent / 'idf')
        (tools / 'bin').mkdir(parents=True)
        (tools / 'bin' / 'idf.py').write_text('#!/bin/sh\necho idf "$@"\n')
        os.chmod(tools / 'bin' / 'idf.py', 0o755)
        (tools / 'export.sh').write_text('export PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bin:$PATH"\n')
        return tools / 'export.sh'

    def test_setup_reaches_the_build_and_its_tools_are_checked_after_it(self):
        export = self.toolchain()
        code, out = self.run_script('--path=.', f'--setup=. "{export}"', '--require=idf.py', '--command', 'idf.py build')
        self.assertEqual((code, out['stage'], out['exit']), (0, 'build', 0), out)
        self.assertEqual((out['setup'], out['requires']), (f'. "{export}"', ['idf.py']))
        self.assertIn('idf build', Path(out['log']).read_text())

    def test_a_missing_tool_or_failed_setup_never_reaches_the_build(self):
        export = self.toolchain()
        for argv, stage, code in (
            (['--require=idf.py'], 'prerequisite', 127),
            ([f'--setup=. "{export}"', '--require=idf.py', '--require=no-such-tool'], 'prerequisite', 127),
            (['--setup=false'], 'setup', 1),
            (['--setup=exit 0'], 'setup', 0),
            (['--setup=set -e\nfalse\necho setup continued'], 'setup', 1),
        ):
            _, out = self.run_script('--path=.', *argv, '--command', 'touch built')
            self.assertEqual((out['stage'], out['exit']), (stage, code), argv)
            self.assertFalse((self.repo / 'built').exists(), argv)
        _, out = self.run_script('--path=.', '--require=no-such-tool', '--command', 'true')
        self.assertIn('missing tool: no-such-tool', Path(out['log']).read_text())
        # The limit: a tool only the command's wrapper runs, named in no --require, is found missing by the build itself.
        _, out = self.run_script('--path=.', '--command', 'sh -c no-such-tool')
        self.assertEqual((out['stage'], out['exit']), ('build', 127))

    def test_a_build_that_fails_or_kills_its_shell_is_a_build_failure(self):
        for command, exit in (('exit 3', 3), ('kill -9 $$', -9)):
            code, out = self.run_script('--path=.', '--command', command)
            if os.name == 'nt' and exit < 0:  # Windows reports no signal, only a failing status
                self.assertNotEqual(out['exit'], 0, command)
                exit = out['exit']
            self.assertEqual((code, out['stage'], out['exit'], out['setup'], out['requires']), (0, 'build', exit, '', []), command)

    def test_no_bash_on_path_is_its_error(self):
        # the lookup itself is windows.bash()'s, tested in test_windows_runtime
        with mock.patch.object(build.windows, 'bash', return_value=None), self.assertRaisesRegex(build.Unusable, 'no bash.exe on PATH'):
            build.bash()

    @unittest.skipUnless(os.name == 'nt', "POSIX runs bash by name; the checkout's bash.bat is a Windows hijack")
    def test_a_checkouts_bash_bat_or_a_system32_first_path_never_runs(self):
        (self.repo / 'bash.bat').write_text('@echo hijacked> hijacked\n')
        env = {k: v for k, v in self.env.items() if k.upper() not in ('PATH', 'NODEFAULTCURRENTDIRECTORYINEXEPATH')}
        system32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
        self.env = {**env, 'PATH': os.pathsep.join([system32, os.environ['PATH']])}  # where `where bash` finds WSL's first
        code, out = self.run_script('--path=.', '--command', 'true')
        self.assertEqual((code, out.get('stage'), out.get('exit')), (0, 'build', 0), out)
        self.assertFalse((self.repo / 'hijacked').exists())
        empty = self.repo.parent / 'empty'
        empty.mkdir()
        self.env = {**env, 'PATH': str(empty)}
        code, out = self.run_script('--path=.', '--command', 'true')
        self.assertEqual(code, 2)
        self.assertIn('no bash.exe on PATH', out['error'])
        self.assertFalse((self.repo / 'hijacked').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows temp dirs sit under the user name, which may hold a blank')
    def test_a_temp_dir_a_blank_would_split_is_refused_before_anything_is_made(self):
        spaced = self.repo.parent / 'John Smith'
        spaced.mkdir()
        self.env.update(TMP=str(spaced), TEMP=str(spaced))
        code, out = self.run_script('--path=.', '--command', 'touch <BUILD>/out')
        self.assertEqual(code, 2, out)
        self.assertIn('point TMP and TEMP at a plain path', out['error'])
        self.assertEqual(list(spaced.iterdir()), [])

    def test_refusals(self):
        for argv, why in [
            (['--command', 'true'], 'usage'),
            (['--path=.'], 'usage'),
            (['--path=.', '--rev', self.base, '--command', 'true'], 'usage'),
        ]:
            code, out = self.run_script(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(why, out['error'])
        (self.repo / 'sub').mkdir()
        code, out = self.run_script('--path=.', '--command', 'true', cwd=self.repo / 'sub')
        self.assertEqual(code, 2)
        self.assertIn('run from the checkout top level', out['error'])


if __name__ == '__main__':
    unittest.main()
