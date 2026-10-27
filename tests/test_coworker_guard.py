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
spec = importlib.util.spec_from_file_location('coworker_guard', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
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
        self.assertEqual(out['updatedInput'], {'command': f'{guard.PREFIX}\n{command}', 'timeout': 600000,
                                               'run_in_background': True})

    def rewritten_run(self, command):
        """The command as the hook rewrites it, run through bash outside any marker; its completed process."""
        done = hook({'tool_name': 'Bash', 'tool_input': {'command': command}})
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        rewritten = json.loads(done.stdout)['hookSpecificOutput']['updatedInput']['command']
        env = {k: v for k, v in os.environ.items() if not k.startswith('COWORK_')}
        return subprocess.run(['bash', '-c', rewritten], capture_output=True, text=True, env=env)

    @unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None, 'needs a POSIX bash')
    def test_a_heredoc_task_naming_the_marker_reaches_the_script_with_it_set(self):
        script = 'import os, sys; print(repr(os.environ.get(sys.argv[1])), sys.stdin.read(), end="")'
        task = f'Review {guard.MARKER} handling in cowork.py.\n'
        ran = self.rewritten_run(f"{sys.executable} -c '{script}' {guard.MARKER} <<'END'\n{task}END")
        self.assertEqual(ran.stdout, f"'1' {task}", ran.stderr)

    @unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None, 'needs a POSIX bash')
    def test_an_unset_or_assignment_after_the_prefix_leaves_the_marker_set(self):
        show = f"{sys.executable} -c 'import os, sys; print(os.environ.get(sys.argv[1]))' {guard.MARKER}"
        for command in (f'unset {guard.MARKER}; {show}', f'{guard.MARKER}= {show}', f'{guard.MARKER}=0; {show}'):
            with self.subTest(command):
                ran = self.rewritten_run(command)
                self.assertIn('readonly variable', ran.stderr)
                self.assertIn(ran.stdout, ('1\n', ''), 'the marker kept, or the command not run')

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
        self.assertEqual(entry['onFailure'], 'block')

    def test_the_hook_exports_the_marker_cowork_reads(self):
        self.assertEqual(guard.MARKER, cowork.READ_ONLY)

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == '__main__':
    unittest.main()
