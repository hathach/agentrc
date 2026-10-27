import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'skills' / 'cowork' / 'scripts' / 'coworker_guard.py'
spec = importlib.util.spec_from_file_location('cowork', SCRIPT.with_name('cowork.py'))
cowork = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cowork)


def setUpModule():
    guard_survivors(__name__)


def hook(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(SCRIPT)], input=text, capture_output=True, text=True)


class HookTest(unittest.TestCase):
    """The hook as Claude Code runs it: exit 2 denies with the reason on stderr; exit 0 with updatedInput rewrites."""

    def test_every_command_runs_with_the_marker_exported_and_its_other_fields_kept(self):
        command = "python3 ~/.claude/skills/cowork/scripts/cowork.py kill codex-main-1"
        done = hook({'tool_name': 'Bash', 'agent_type': 'coworker',
                     'tool_input': {'command': command, 'timeout': 600000, 'run_in_background': True}})
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        out = json.loads(done.stdout)['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'PreToolUse')
        self.assertEqual(out['updatedInput'], {'command': f'export {cowork.READ_ONLY}=1\n{command}', 'timeout': 600000,
                                               'run_in_background': True})

    @unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None, 'needs a POSIX bash')
    def test_the_marker_reaches_a_heredoc_command(self):
        # names no marker, which the hook would deny
        script = ('import os, sys; '
                  'print([k + "=" + v for k, v in os.environ.items() if k.startswith("COWORK_")], end=" ")')
        command = f"{sys.executable} -c '{script}; print(sys.stdin.read())' <<'END'\ntask\nEND"
        done = hook({'tool_name': 'Bash', 'tool_input': {'command': command}})
        rewritten = json.loads(done.stdout)['hookSpecificOutput']['updatedInput']['command']
        env = {k: v for k, v in os.environ.items() if not k.startswith('COWORK_')}
        ran = subprocess.run(['bash', '-c', rewritten], capture_output=True, text=True, env=env)
        self.assertEqual(ran.stdout, f"['{cowork.READ_ONLY}=1'] task\n\n", ran.stderr)

    def test_a_command_naming_the_marker_is_denied(self):
        for command in (f'unset {cowork.READ_ONLY}; python3 cowork.py reset codex impl',
                        f'{cowork.READ_ONLY}= python3 cowork.py kill codex-main-1'):
            with self.subTest(command):
                done = hook({'tool_name': 'Bash', 'tool_input': {'command': command}})
                self.assertEqual(done.returncode, 2)
                self.assertIn(f'{cowork.READ_ONLY} is this hook\'s to set', done.stderr)
                self.assertEqual(done.stdout, '')

    def test_garbage_or_a_missing_command_denies(self):
        for payload in ('not json', {'tool_name': 'Bash', 'tool_input': {}}):
            with self.subTest(payload):
                done = hook(payload)
                self.assertEqual(done.returncode, 2)
                self.assertIn('so the call is denied', done.stderr)


class DeclarationTest(unittest.TestCase):
    def test_the_coworker_agent_declares_the_installed_hook_on_bash(self):
        """The hook goes wherever agents/coworker.md is installed; Bash is the agent's only tool."""
        front = yaml.safe_load((REPO / 'agents' / 'coworker.md').read_text(encoding='utf-8').split('---')[1])
        self.assertEqual(front['tools'], 'Bash')
        [group] = front['hooks']['PreToolUse']
        [entry] = group['hooks']
        self.assertEqual(group['matcher'], 'Bash')
        self.assertEqual(entry['type'], 'command')
        self.assertEqual(entry['command'], f'python3 ~/.claude/skills/cowork/scripts/{SCRIPT.name}')

    def test_the_hook_exports_the_marker_cowork_reads(self):
        spec = importlib.util.spec_from_file_location('coworker_guard', SCRIPT)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        self.assertEqual(guard.MARKER, cowork.READ_ONLY)

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == '__main__':
    unittest.main()
