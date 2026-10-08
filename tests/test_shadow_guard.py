"""Tests for hooks/headless-chief/shadow_guard.py, run as Claude Code runs it: hook JSON on stdin."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'headless-chief' / 'shadow_guard.py'


@unittest.skipIf(os.name == 'nt', 'the hook ships for POSIX sessions')
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
        self.env['HOME'] = str(self.root)
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

    def hook(self, tool='Workflow', cwd=None, **env):
        event = {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'cwd': str(cwd or self.project)}
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(event), env={**self.env, **env},
                              capture_output=True, text=True, timeout=30)

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

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads any directory')
    def test_an_unreadable_directory_denies_without_merge_advice(self):
        self.installed('agents', 'code-writer.md', 'new')
        agents = self.local('agents', 'code-writer.md', 'old').parent
        agents.chmod(0)
        self.addCleanup(agents.chmod, 0o755)
        r = self.hook()
        self.assert_denied(r, 'cannot inspect')
        self.assertNotIn('merge the default branch', r.stderr)


if __name__ == '__main__':
    unittest.main()
