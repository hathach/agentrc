"""Tests for hooks/headless-chief/shadow_guard.py, run as Claude Code runs it: hook JSON on stdin."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'headless-chief' / 'shadow_guard.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub  # noqa: E402


def setUpModule():
    guard_survivors(__name__)


class ShadowGuard(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.project = self.root / 'wt'
        (self.project / 'src').mkdir(parents=True)
        # GIT_* from a calling hook would point git at another repository
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CLAUDE_CONFIG_DIR', 'CLAUDE_PROJECT_DIR') and not k.startswith('GIT_')}
        self.env['HOME'] = self.env['USERPROFILE'] = str(self.root)   # Path.home() reads USERPROFILE on Windows
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)

    def installed(self, kind, name, content):
        src = self.root / 'agentrc' / kind / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(content)
        link = self.root / '.claude' / kind / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(src)

    def local(self, kind, name, content):
        path = self.project / '.claude' / kind / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def hook(self, tool='Workflow', cwd=None, raw=None, run_in=None, **env):
        event = {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'cwd': str(cwd or self.project)}
        return subprocess.run([sys.executable, str(SCRIPT)], input=raw or json.dumps(event, ensure_ascii=False),
                              env={**self.env, **env}, cwd=run_in, capture_output=True, encoding='utf-8', timeout=30)

    def assert_denied(self, r, *named):
        self.assertEqual(r.returncode, 2, r.stderr)
        for want in named:
            self.assertIn(want, r.stderr)

    def test_a_stale_workflow_denies_the_call(self):
        self.installed('workflows', 'pr-babysit.js', 'new')
        self.local('workflows', 'pr-babysit.js', 'old')
        self.assert_denied(self.hook(), '.claude/workflows/pr-babysit.js', 'merge the default branch', str(self.root / '.claude'))

    def test_a_stale_role_under_a_current_workflow_denies_the_workflow(self):
        # the workflow's own agent() dispatches never reach the hook, so its outer call carries the check
        self.installed('workflows', 'pr-babysit.js', 'same')
        self.local('workflows', 'pr-babysit.js', 'same')
        self.installed('agents', 'code-writer.md', 'new')
        self.local('agents', 'code-writer.md', 'old')
        self.assert_denied(self.hook('Workflow'), '.claude/agents/code-writer.md')

    def test_nothing_to_shadow_lets_the_call_through(self):
        self.assertEqual(self.hook().returncode, 0, 'no .claude at all')
        self.installed('workflows', 'pr-babysit.js', 'same')
        self.local('workflows', 'pr-babysit.js', 'same')
        self.local('agents', 'mine.md', 'a role agentrc does not ship')
        self.local('agents', 'hw-debugger.md', 'not installed')
        r = self.hook()
        self.assertEqual((r.returncode, r.stderr), (0, ''))

    def test_the_project_is_the_sessions_repository(self):
        self.installed('workflows', 'pr-babysit.js', 'new')
        self.local('workflows', 'pr-babysit.js', 'old')
        self.assert_denied(self.hook(cwd=self.project / 'src'), '.claude/workflows/pr-babysit.js')
        self.assert_denied(self.hook(GIT_WORK_TREE=str(self.root), GIT_DIR=str(self.project / '.git')),
                           '.claude/workflows/pr-babysit.js')
        # a Bash cd moves the payload's cwd, not the session's project
        self.assert_denied(self.hook(cwd=self.root, CLAUDE_PROJECT_DIR=str(self.project)), '.claude/workflows/pr-babysit.js')
        self.assertEqual(self.hook(cwd=self.root).returncode, 0, 'without CLAUDE_PROJECT_DIR the cwd decides')

    def test_a_non_ascii_project_is_read_as_utf_8(self):
        # hook JSON is UTF-8; read in a locale's encoding (GBK) the cwd named no directory and nothing was denied
        self.project = self.root / 'wt-ü✓'
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)
        self.installed('workflows', 'pr-babysit.js', 'new')
        self.local('workflows', 'pr-babysit.js', 'old')
        self.assert_denied(self.hook(), '.claude/workflows/pr-babysit.js')

    @unittest.skipIf(os.name == 'nt', 'chmod(0) does not make a Windows directory unreadable')
    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads any directory')
    def test_an_unreadable_directory_denies_without_merge_advice(self):
        self.installed('agents', 'code-writer.md', 'new')
        agents = self.local('agents', 'code-writer.md', 'old').parent
        agents.chmod(0)
        self.addCleanup(agents.chmod, 0o755)
        r = self.hook()
        self.assert_denied(r, 'cannot inspect')
        self.assertNotIn('merge the default branch', r.stderr)

    def test_any_error_denies(self):
        # Claude Code lets a hook that exits 1 pass
        for raw in ('not json', json.dumps({'tool_name': 'Workflow', 'cwd': 5}), json.dumps([])):
            with self.subTest(raw=raw):
                r = self.hook(raw=raw)
                self.assert_denied(r, 'shadow guard: the check failed')
                self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)

    @unittest.skipUnless(os.name == 'nt', 'NTFS names match without case')
    def test_a_stale_entry_named_in_another_case_still_shadows(self):
        self.installed('agents', 'code-writer.md', 'new')
        self.local('agents', 'Code-Writer.MD', 'old')
        self.assert_denied(self.hook(), '.claude/agents/Code-Writer.MD')

    @unittest.skipUnless(os.name == 'nt', 'CreateProcess tries the working directory before PATH')
    def test_a_git_in_the_project_is_not_the_one_run(self):
        # Claude Code runs the hook in the project, a contributor's checkout: its git.exe must not run
        make_stub(self.project, 'git', f'import pathlib, sys\npathlib.Path({str(self.root / "ran")!r}).touch()\nsys.exit(1)\n')
        self.env = {k: v for k, v in self.env.items() if k.lower() != 'nodefaultcurrentdirectoryinexepath'}   # Claude Code sets it
        self.installed('workflows', 'pr-babysit.js', 'new')
        self.local('workflows', 'pr-babysit.js', 'old')
        self.assert_denied(self.hook(run_in=self.project), '.claude/workflows/pr-babysit.js')
        self.assertFalse((self.root / 'ran').exists(), "the project's git.exe ran")

    @unittest.skipUnless(os.name == 'nt', 'shadows.on_path walks PATH on Windows only')
    def test_a_relative_path_entry_is_no_place_to_find_a_program(self):
        # a relative entry resolves against the hook's working directory, the project
        spec = importlib.util.spec_from_file_location('shadows', SCRIPT.parents[2] / 'skills' / 'headless-chief' / 'scripts' / 'shadows.py')
        shadows = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shadows)
        (self.project / 'bin').mkdir()
        for exe in (self.project / 'git.exe', self.project / 'bin' / 'git.exe'):
            exe.write_bytes(b'')
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(['.', 'bin', '\\bin', ''])}):
            self.assertIsNone(shadows.on_path('git', ('.exe',)))
        with mock.patch.dict(os.environ, {'PATH': str(self.project / 'bin')}):
            self.assertEqual(Path(shadows.on_path('git', ('.exe',))), self.project / 'bin' / 'git.exe')


if __name__ == '__main__':
    unittest.main()
