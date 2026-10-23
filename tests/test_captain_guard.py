import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'captain' / 'scripts' / 'captain_guard.py'
spec = importlib.util.spec_from_file_location('captain_guard', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

RUN = 'python3 ~/.claude/skills/captain/scripts/captain.py'


def setUpModule():
    guard_survivors(__name__)


def call(tool, agent_type='captain', agent_id=None, **tool_input):
    payload = {'tool_name': tool, 'tool_input': tool_input, 'agent_type': agent_type}
    if agent_id:
        payload['agent_id'] = agent_id
    return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload), capture_output=True, text=True)


class HookTest(unittest.TestCase):
    """The hook as Claude Code runs it: exit 2 denies, with the reason on stderr."""

    def test_denies_captain_and_passes_everyone_else(self):
        self.assertEqual(call('Bash', command='gh pr merge 12').returncode, 2)
        self.assertEqual(call('Bash', agent_type='chief', command='gh pr merge 12').returncode, 0)
        self.assertEqual(call('Bash', agent_id='sub-1', command='gh pr merge 12').returncode, 2)
        self.assertEqual(call('Bash', agent_type=None, command='git push').returncode, 0)

    def test_allows_its_own_script(self):
        done = call('Bash', command=f'{RUN} ask w2:p1')
        self.assertEqual((done.returncode, done.stderr), (0, ''))

    def test_garbage_denies(self):
        done = subprocess.run([sys.executable, str(SCRIPT)], input='not json', capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)


class DeclarationTest(unittest.TestCase):
    def test_the_agent_declares_the_installed_guard_on_every_tool(self):
        """The hook goes wherever agents/captain.md is installed; it must run this file, on every tool."""
        agent = (Path(__file__).resolve().parents[1] / 'agents' / 'captain.md').read_text(encoding='utf-8')
        front = yaml.safe_load(agent.split('---')[1])
        [group] = front['hooks']['PreToolUse']
        [hook] = group['hooks']
        self.assertEqual(group['matcher'], '*')
        self.assertEqual(hook['type'], 'command')
        self.assertEqual(hook['command'], f'python3 {guard.SCRIPTS[0].replace("captain.py", "captain_guard.py")}')
        self.assertTrue(SCRIPT.exists())


class VerdictTest(unittest.TestCase):
    def verdict(self, tool, **tool_input):
        return guard.verdict({'tool_name': tool, 'tool_input': tool_input})

    def test_plain_script_calls_pass(self):
        home_script = (Path.home() / '.claude/skills/captain/scripts/captain.py').as_posix()
        for cmd in [f'{RUN} list', f'{RUN} list --json', f'{RUN} enroll', f'{RUN} enroll --replace',
                    f'{RUN} ask w2:p1 w3:p4', f'{RUN} collect m-0a1b --wait 1740000', f'{RUN} reports',
                    f'{RUN} cancel m-0a1b', f'{RUN} relay w2:p1 w3:p4',
                    f'{RUN} relay w2:p1 --entry f0a4846d-6b04-428a-8e4d-a4fc931e6cff', f'{RUN} withdraw a-0a1b',
                    f'{RUN} open --checkout /home/u/code/repo --account ada --name repo',
                    f'{RUN} open --checkout ~/code/repo --account main',
                    f'python3 {home_script} list']:
            with self.subTest(cmd):
                self.assertIsNone(self.verdict('Bash', command=cmd))
                self.assertIsNone(self.verdict('Monitor', command=cmd, timeout_ms=1800000))

    def test_anything_else_in_a_command_is_denied(self):
        for cmd in ['gh pr merge 12', 'git push', f'{RUN} list; git push', f'{RUN} list && gh pr view 1',
                    f'{RUN} list | sh', f'{RUN} list > /tmp/x', f'{RUN} ask "$(id)"', f'{RUN} ask `id`',
                    f'{RUN} ask w2:p1\ngit push', f'{RUN} report --for m-1 --file f.json', f'{RUN}',
                    'python3 skills/captain/scripts/captain.py list', 'python3 /tmp/captain.py list',
                    f'{RUN} ask ../etc', f'{RUN} ask ~root', 'python3 -c print(1)', f'cd /w && {RUN} list',
                    f'{RUN} ask w2:p1 --file=/etc/passwd', f'{RUN} accept --action a-1',
                    f'{RUN} outcome --action a-1 --result completed', f'{RUN} ask /etc/passwd',
                    f'{RUN} open --checkout /home/u/../../etc --account main', f'{RUN} open --name /tmp/x',
                    f'{RUN} open --checkout ~root/x --account main', None]:
            with self.subTest(cmd):
                self.assertIsNotNone(self.verdict('Bash', command=cmd))

    def test_the_skill_s_documented_commands_pass(self):
        """The captain runs what SKILL.md shows; a recipe the guard denies would strand it."""
        text = (Path(__file__).resolve().parents[1] / 'skills' / 'captain' / 'SKILL.md').read_text(encoding='utf-8')
        sample = {'<pane>...': 'w2:p1', '<msg>...': 'm-1', '<msg>': 'm-1', '<ms>': '1740000', 'MS': '1740000',
                  '<uuid>': 'f0a4846d-6b04-428a-8e4d-a4fc931e6cff', '<action>': 'a-1',
                  '<dir>': '/home/u/code/repo', '<main|ada>': 'ada', '<name>': 'repo'}
        found = re.findall(r'(python3 ~/\.claude/skills/captain/scripts/captain\.py [^`#\n]*)', text)
        self.assertGreaterEqual(len(found), 10)
        for cmd in found:
            cmd = re.sub(r'\[([^\]]*)\]', r'\1', cmd.strip())
            for k, v in sample.items():
                cmd = cmd.replace(k, v)
            with self.subTest(cmd):
                self.assertIsNone(self.verdict('Bash', command=cmd))

    def test_monitor_only_watches_a_command(self):
        self.assertIsNotNone(self.verdict('Monitor', ws={'url': 'wss://x'}))

    def test_reads_only_state_and_skill(self):
        state = os.path.join(guard.STATE, 'ledger.jsonl')
        skill = os.path.join(guard.SKILL, 'SKILL.md')
        self.assertIsNone(self.verdict('Read', file_path=state))
        self.assertIsNone(self.verdict('Read', file_path=skill))
        for path in ['/etc/passwd', os.path.join(guard.STATE, '..', 'x'), 'relative.md', guard.STATE + '-other/x',
                     None]:
            with self.subTest(path):
                self.assertIsNotNone(self.verdict('Read', file_path=path))

    def test_other_tools(self):
        self.assertIsNone(self.verdict('Skill', skill='captain'))
        self.assertIsNotNone(self.verdict('Skill', skill='pr-babysit'))
        self.assertIsNone(self.verdict('ToolSearch', query='select:Monitor'))
        self.assertIsNotNone(self.verdict('ToolSearch', query='select:WebFetch'))
        self.assertIsNone(self.verdict('AskUserQuestion', questions=[]))
        for tool in ('Edit', 'Write', 'Agent', 'Workflow', 'WebFetch', 'WebSearch', 'SendMessage', 'NotebookEdit',
                     'mcp__github__merge_pull_request', 'Artifact', None):
            with self.subTest(tool):
                self.assertIsNotNone(self.verdict(tool))


if __name__ == '__main__':
    unittest.main()
