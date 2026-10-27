import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, tmpdir  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'captain' / 'scripts' / 'captain_guard.py'
spec = importlib.util.spec_from_file_location('captain_guard', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
PUBLISHER = Path(__file__).resolve().parents[1] / 'skills' / 'followup-issue' / 'scripts' / 'publish.py'
spec = importlib.util.spec_from_file_location('followup_publish', PUBLISHER)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)

RUN = 'python3 ~/.claude/skills/captain/scripts/captain.py'
READ = 'python3 ~/.claude/skills/captain/scripts/captain_read.py'
PUBLISH = 'python3 ~/.claude/skills/followup-issue/scripts/publish.py'
REPO = '--repo hathach/agentrc --allow-repo hathach/agentrc'
# a command spells paths with forward slashes: the guard refuses a backslash, an escape in the Bash tool's shell
DRAFTS, MEMORY = guard.DRAFTS.replace(os.sep, '/'), guard.MEMORY.replace(os.sep, '/')


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


    def test_a_missing_reader_denies(self):
        """The reader is imported before the hook's excepthook is set; its loss must deny, not pass."""
        lone = tmpdir(self)
        (lone / 'captain_guard.py').write_bytes(SCRIPT.read_bytes())
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}, 'agent_type': 'captain'})
        done = subprocess.run([sys.executable, str(lone / 'captain_guard.py')], input=payload, capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 2, done.stderr)


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
                    f'{RUN} ask w2:p1 w3:p4', f'{RUN} ask pr:12 pr:hathach:agentrc:12 issue:7 ws:w54 ws:my-label',
                    f'{RUN} relay pr:12 w2:p1', f'{RUN} collect m-0a1b --wait 1740000', f'{RUN} reports',
                    f'{RUN} cancel m-0a1b', f'{RUN} relay w2:p1 w3:p4',
                    f'{RUN} relay w2:p1 --entry f0a4846d-6b04-428a-8e4d-a4fc931e6cff --digest 3f9a0c1d2e4b5a67',
                    f'{RUN} withdraw a-0a1b',
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
                    f'{RUN} open --checkout ~root/x --account main', 'herdr pane close w2:p1',
                    'herdr workspace close w2', 'herdr agent prompt w2:p1 /exit', 'kill 1234', 'pkill claude',
                    f'{RUN} close w2:p1', f'{RUN} kill w2:p1', None]:
            with self.subTest(cmd):
                self.assertIsNotNone(self.verdict('Bash', command=cmd))

    def test_the_skill_s_documented_commands_pass(self):
        """The captain runs what SKILL.md shows; a recipe the guard denies would strand it."""
        text = (Path(__file__).resolve().parents[1] / 'skills' / 'captain' / 'SKILL.md').read_text(encoding='utf-8')
        sample = {'<pane>...': 'w2:p1', '<msg>...': 'm-1', '<msg>': 'm-1', '<ms>': '1740000', 'MS': '1740000',
                  '<uuid>': 'f0a4846d-6b04-428a-8e4d-a4fc931e6cff', '<hex>': '3f9a0c1d2e4b5a67', '<action>': 'a-1',
                  '<target>...': 'pr:12',
                  '<dir>': '/home/u/code/repo', '<main|ada>': 'ada', '<name>': 'repo'}
        found = re.findall(r'(python3 ~/\.claude/skills/captain/scripts/captain\.py [^`#\n]*)', text)
        self.assertGreaterEqual(len(found), 10)
        for cmd in found:
            cmd = re.sub(r'\[([^\]]*)\]', r'\1', cmd.strip())
            for k, v in sample.items():
                cmd = cmd.replace(k, v)
            with self.subTest(cmd):
                self.assertIsNone(self.verdict('Bash', command=cmd))

    def test_reader_lookups_pass_and_the_reader_s_refusals_deny(self):
        for cmd in [f'{READ} git -C ~/.herdr/worktrees/tinyusb/fix2-issue reflog show -n 5',
                    f'{READ} git -C ~/code/agentrc log --format=dated origin/main..topic',
                    f'{READ} gh pr view 4165 -R hathach/tinyusb --json number,headRefOid,updatedAt']:
            with self.subTest(cmd):
                self.assertIsNone(self.verdict('Bash', command=cmd))
        for cmd in [f'{READ} git -C ~/code/agentrc push', f'{READ} gh pr merge 1 -R o/r', f'{READ} gh api x',
                    f'{READ} git -C /etc status', f"{READ} git -C ~/code/a log --format='%h'",
                    f'{READ} gh pr view 1 -R o/r | sh', f'{READ} git -C ~/code/a log; git push',
                    f'{RUN} ask a,b', 'python3 skills/captain/scripts/captain_read.py gh pr view 1 -R o/r']:
            with self.subTest(cmd):
                self.assertIsNotNone(self.verdict('Bash', command=cmd))
        self.assertIsNotNone(self.verdict('Monitor', command=f'{READ} gh pr view 1 -R o/r --web'))

    def test_the_skill_s_documented_lookups_pass(self):
        text = (Path(__file__).resolve().parents[1] / 'skills' / 'captain' / 'SKILL.md').read_text(encoding='utf-8')
        sample = {'<checkout>': '~/code/repo', '<N>': '12', '<owner>/<repo>': 'o/r', '<fields>': 'number,title',
                  '-n N': '-n 5', 'oneline|dated': 'dated', '<range>': 'main..topic', '<ref>': 'main',
                  '<directory>': '~/.cache/chief-runs', '<prefix>': 'tinyusb-4165', '<word>': 'captain',
                  '--limit N': '--limit 500'}
        found = re.findall(r'(python3 ~/\.claude/skills/captain/scripts/captain_read\.py [^`#\n]*)', text)
        self.assertGreaterEqual(len(found), 7)
        for cmd in found:
            for k, v in sample.items():
                cmd = cmd.replace(k, v)
            cmd = re.sub(r'\[([^\]]*)\]', r'\1', cmd.strip())
            with self.subTest(cmd):
                self.assertIsNone(self.verdict('Bash', command=cmd))

    def test_a_follow_up_issue_publishes_only_as_drafted_for_hathach_agentrc(self):
        title, body = f'{DRAFTS}/herdr-timeout-title.md', f'{DRAFTS}/herdr.md'
        tilde = body.replace(Path.home().as_posix(), '~', 1)
        home = (Path.home() / '.claude/skills/followup-issue/scripts/publish.py').as_posix()
        create = f'create {REPO} --title-file {title} --body-file {body} --label follow-up'
        for cmd in [f'{PUBLISH} {create}', f'python3 {home} {create}',
                    f'{PUBLISH} create --label follow-up --body-file {tilde} --title-file {title} {REPO}',
                    f'{PUBLISH} comment {REPO} --issue 313 --body-file {body}']:
            with self.subTest(cmd):
                self.assertIsNone(self.verdict('Bash', command=cmd))
                self.assertIsNotNone(self.verdict('Monitor', command=cmd, timeout_ms=1800000))
        for cmd in [f'{PUBLISH}', f'{PUBLISH} edit {REPO} --issue 1 --body-file {body}',
                    f'{PUBLISH} {create} --label bug', f'{PUBLISH} {create} --repo hathach/agentrc',
                    f'{PUBLISH} {create.replace("--label follow-up", "")}',
                    f'{PUBLISH} {create.replace("--label follow-up", "--label bug")}',
                    f'{PUBLISH} {create.replace("--repo hathach/agentrc", "--repo hathach/tinyusb")}',
                    f'{PUBLISH} {create} --allow-repo hathach/tinyusb',
                    f'{PUBLISH} {create.replace("--title-file", "--title")}',
                    f'{PUBLISH} {create.replace("--title-file", "--title-f")}',
                    f'{PUBLISH} {create.replace("--title-file " + title, "--title-file=" + title)}',
                    f'{PUBLISH} {create.replace(title, DRAFTS + "/x.txt")}',
                    f'{PUBLISH} {create.replace(title, DRAFTS + "/../x.md")}',
                    f'{PUBLISH} {create.replace(title, DRAFTS + "-other/x.md")}',
                    f'{PUBLISH} {create.replace(title, MEMORY + "/x.md")}',
                    f'{PUBLISH} {create.replace(title, "x.md")}', f'{PUBLISH} {create.replace(title, "~root/x.md")}',
                    f'{PUBLISH} {create} extra', f'{PUBLISH} comment {REPO} --issue 0 --body-file {body}',
                    f'{PUBLISH} comment {REPO} --issue x --body-file {body}',
                    f'{PUBLISH} comment {REPO} --issue 3 --body-file {body} --label follow-up',
                    f'{PUBLISH} comment {REPO} --issue 3 --issue 4 --body-file {body}',
                    f'{PUBLISH} {create}; git push', f'{PUBLISH} {create} > /tmp/x',
                    f'python3 skills/followup-issue/scripts/publish.py {create}',
                    f'python3 /tmp/publish.py {create}']:
            with self.subTest(cmd):
                self.assertIsNotNone(self.verdict('Bash', command=cmd))

    def test_every_admitted_publication_is_one_publish_py_parses(self):
        """The guard keeps its own copy of publish.py's options: each shape it admits must reach publish.py's checks,
        here its refusal of a draft that does not exist, never an argparse usage error."""
        drafts = tmpdir(self)
        missing = str(drafts / 'never-written.md')
        for action, values in guard.PUBLISH.items():
            argv = [action] + [w for name, value in values.items()
                               for w in (name, value or ('3' if name == '--issue' else missing))]
            with self.subTest(action), mock.patch.object(guard, 'DRAFTS', str(drafts)):
                self.assertIsNone(guard.publication(argv))
                out = io.StringIO()
                with redirect_stdout(out), mock.patch.object(publish, 'gh', side_effect=AssertionError('no call')):
                    code = publish.main(argv)
                receipt = json.loads(out.getvalue().splitlines()[-1])
                self.assertEqual((code, receipt['outcome']), (2, 'refused'))
                self.assertIn('cannot read', receipt['error'])

    def test_a_draft_symlinked_out_of_the_drafts_is_refused(self):
        drafts = tmpdir(self)
        (drafts / 'x.md').symlink_to(Path(os.path.expanduser('~')) / 'code' / 'x.md')
        (drafts / 'y.md').write_text('y', encoding='utf-8')
        with mock.patch.object(guard, 'DRAFTS', str(drafts)):
            ok = f'{PUBLISH} comment {REPO} --issue 3 --body-file {(drafts / "y.md").as_posix()}'
            self.assertIsNone(self.verdict('Bash', command=ok))
            self.assertIsNotNone(self.verdict('Bash', command=ok.replace('y.md', 'x.md')))
            self.assertIsNone(self.verdict('Write', file_path=str(drafts / 'y.md'), content='x'))
            self.assertIsNotNone(self.verdict('Write', file_path=str(drafts / 'x.md'), content='x'))

    def test_the_skill_s_documented_publications_pass(self):
        text = (Path(__file__).resolve().parents[1] / 'skills' / 'captain' / 'SKILL.md').read_text(encoding='utf-8')
        sample = {'<title.md>': '~/.cache/agentrc/captain/drafts/herdr-title.md',
                  '<body.md>': f'{DRAFTS}/herdr.md', '<N>': '313'}
        found = re.findall(r'(python3 ~/\.claude/skills/followup-issue/scripts/publish\.py [^`#\n]*)', text)
        self.assertEqual(len(found), 2)
        for cmd in found:
            for k, v in sample.items():
                cmd = cmd.replace(k, v)
            with self.subTest(cmd):
                self.assertIsNone(self.verdict('Bash', command=cmd.strip()))

    def test_monitor_only_watches_a_command(self):
        self.assertIsNotNone(self.verdict('Monitor', ws={'url': 'wss://x'}))

    def test_reads_checkouts_chief_runs_memory_state_and_skill(self):
        home = os.path.expanduser('~')
        for path in [os.path.join(guard.STATE, 'ledger.jsonl'), os.path.join(guard.SKILL, 'SKILL.md'),
                     f'{home}/code/agentrc/README.md', f'{home}/.herdr/worktrees/tinyusb/x/src/tusb.c',
                     f'{home}/.cache/chief-runs/tinyusb-4165-babysit-1/progress.log',
                     f'{home}/.claude/projects/-home-u-code-agentrc/memory/MEMORY.md',
                     os.path.join(guard.MEMORY, 'MEMORY.md'),
                     f'{home}/.claude/skills/followup-issue/SKILL.md']:
            with self.subTest(path):
                self.assertIsNone(self.verdict('Read', file_path=path))
        for path in ['/etc/passwd', os.path.join(guard.STATE, '..', 'x'), 'relative.md', guard.STATE + '-other/x',
                     f'{home}/.ssh/id_ed25519', f'{home}/.config/gh/hosts.yml', f'{home}/.claude/.credentials.json',
                     f'{home}/.claude/projects/-home-u-code-agentrc/abc.jsonl', f'{home}/.claude/projects/memory',
                     f'{home}/code-other/x', f'{home}/code/../.ssh/id_ed25519', None]:
            with self.subTest(path):
                self.assertIsNotNone(self.verdict('Read', file_path=path))

    def test_a_symlink_out_of_a_root_is_not_readable(self):
        root = tmpdir(self)
        (root / 'out').symlink_to('/etc')
        with mock.patch.object(guard.captain_read, 'READ_ROOTS', (str(root),)):
            self.assertIsNone(self.verdict('Read', file_path=str(root / 'in.log')))
            self.assertIsNotNone(self.verdict('Read', file_path=str(root / 'out' / 'passwd')))

    def test_writes_only_markdown_in_its_memory_and_drafts(self):
        for note in (os.path.join(guard.MEMORY, 'MEMORY.md'), os.path.join(guard.DRAFTS, 'herdr-title.md')):
            self.assertIsNone(self.verdict('Write', file_path=note, content='x'))
            self.assertIsNone(self.verdict('Edit', file_path=note, old_string='a', new_string='b'))
        for path in [os.path.join(guard.MEMORY, 'run.sh'), guard.MEMORY + '-other/x.md',
                     os.path.join(guard.DRAFTS, 'x.json'), os.path.join(guard.DRAFTS, '..', 'ledger.md'),
                     guard.DRAFTS + '-other/x.md',
                     os.path.join(guard.MEMORY, '..', 'chief', 'x.md'),
                     os.path.expanduser('~/.claude/projects/-home-u-code-agentrc/memory/MEMORY.md'),
                     os.path.expanduser('~/code/agentrc/README.md'), 'MEMORY.md', None]:
            for tool in ('Write', 'Edit'):
                with self.subTest(tool=tool, path=path):
                    self.assertIsNotNone(self.verdict(tool, file_path=path))

    def test_a_markdown_symlink_to_another_file_is_not_writable(self):
        memory = tmpdir(self)
        (memory / 'x.md').symlink_to(memory / 'x.json')
        with mock.patch.object(guard, 'MEMORY', str(memory)):
            self.assertIsNone(self.verdict('Write', file_path=str(memory / 'y.md'), content='x'))
            for tool in ('Write', 'Edit'):
                with self.subTest(tool):
                    self.assertIsNotNone(self.verdict(tool, file_path=str(memory / 'x.md')))

    def test_a_parent_step_is_refused_before_links_resolve(self):
        base = tmpdir(self)
        memory, out = base / 'memory', base / 'out'
        (memory / 'sub').mkdir(parents=True)
        out.mkdir()
        (out / 'link').symlink_to(memory / 'sub')   # resolved, link/../x.md is memory/x.md; as written, out/x.md
        with mock.patch.object(guard, 'MEMORY', str(memory)):
            self.assertIsNone(self.verdict('Write', file_path=str(memory / 'sub' / 'x.md'), content='x'))
            for path in (os.path.join(out, 'link', '..', 'x.md'), os.path.join(memory, 'sub', '..', 'x.md')):
                with self.subTest(path):
                    self.assertIsNotNone(self.verdict('Write', file_path=path, content='x'))

    def test_other_tools(self):
        self.assertIsNone(self.verdict('Skill', skill='captain'))
        self.assertIsNone(self.verdict('Skill', skill='followup-issue'))
        self.assertIsNotNone(self.verdict('Skill', skill='pr-babysit'))
        self.assertIsNone(self.verdict('ToolSearch', query='select:Monitor'))
        self.assertIsNotNone(self.verdict('ToolSearch', query='select:WebFetch'))
        self.assertIsNone(self.verdict('AskUserQuestion', questions=[]))
        for tool in ('Glob', 'Grep', 'Agent', 'Workflow', 'WebFetch', 'WebSearch', 'SendMessage', 'NotebookEdit',
                     'mcp__github__merge_pull_request', 'Artifact', None):
            with self.subTest(tool):
                self.assertIsNotNone(self.verdict(tool))


if __name__ == '__main__':
    unittest.main()
