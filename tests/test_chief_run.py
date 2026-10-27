"""Tests for headless-chief's chief_run.py against a fake `claude` replaying stream-json.

data/chief_run/stream.jsonl is a trimmed real capture (claude 2.1.280, one subagent call):
the subagent's reply reaches the stream only inside a top-level user tool_result, and no
worker assistant event was streamed, so worker exclusion is shown on the event types
that do appear."""
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'headless-chief' / 'scripts' / 'chief_run.py'
CAPTURE = Path(__file__).resolve().parent / 'data' / 'chief_run' / 'stream.jsonl'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub, pid_alive  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
d = pathlib.Path(os.environ['FAKE_DIR'])
(d / 'argv.json').write_text(json.dumps(sys.argv[1:]))
(d / 'env.json').write_text(json.dumps(dict(os.environ)))
(d / 'cwd').write_text(os.getcwd())
(d / 'pid').write_text(str(os.getpid()))
(d / 'stdin.txt').write_bytes(sys.stdin.buffer.read())
for line in (d / 'stream.jsonl').read_bytes().splitlines(keepends=True):
    if line.startswith(b'#gate '):   # hold here until the test creates the named file
        gate, deadline = d / line.split()[1].decode(), time.monotonic() + 10
        while not gate.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        continue
    sys.stdout.buffer.write(line)   # bytes: Windows text mode would turn each newline into CRLF
    sys.stdout.buffer.flush()
sys.stderr.write('fake stderr\n')
if os.environ.get('FAKE_SIGNAL'):
    os.kill(os.getpid(), int(os.environ['FAKE_SIGNAL']))
rc = int(os.environ.get('FAKE_RC', '0'))
if rc > 0x7FFFFFFF:   # an NTSTATUS, as Windows ends a crashed process
    import ctypes
    ctypes.windll.kernel32.ExitProcess(ctypes.c_uint(rc))
sys.exit(rc)
'''


def init(sid='s-1'):
    return {'type': 'system', 'subtype': 'init', 'session_id': sid}


def text(msg_id, *blocks, parent=None):
    return {'type': 'assistant', 'parent_tool_use_id': parent,
            'message': {'id': msg_id, 'role': 'assistant', 'content': [{'type': 'text', 'text': b} for b in blocks]}}


def result(body='chief: stage · done', is_error=False, subtype='success'):
    return {'type': 'result', 'subtype': subtype, 'is_error': is_error, 'result': body}


def has_console(pid):
    """Windows: does the process have a console; AttachConsole asks it from a process without one."""
    probe = ('import ctypes, sys; k = ctypes.WinDLL("kernel32", use_last_error=True); '
             f'sys.exit(0 if k.AttachConsole({pid}) else ctypes.get_last_error())')
    rc = subprocess.run([sys.executable, '-c', probe], creationflags=subprocess.DETACHED_PROCESS).returncode
    assert rc in (0, 6), f'AttachConsole failed with {rc}'   # 6: ERROR_INVALID_HANDLE, the process has no console
    return rc == 0


def kill(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


def setUpModule():
    guard_survivors(__name__)


class ChiefRun(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.fake = self.root / 'fake'
        self.fake.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        make_stub(self.bin, 'claude', FAKE.split('\n', 1)[1])
        self.worktree = self.root / 'wt'
        self.worktree.mkdir()
        self.task = self.root / 'task.md'
        self.task.write_text('Babysit PR 1.\n')
        self.out = self.root / 'run'
        # HOME holds the transcripts run_cost.py reads: none, unless a test writes them
        # with no CLAUDE_CONFIG_DIR, the user scope is HOME/.claude
        self.env = {**{k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'},
                    'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}', 'FAKE_DIR': str(self.fake),
                    'CLAUDECODE': '1', 'HERDR_PANE_ID': 'p1', 'HERDR_ENV': '1', 'HOME': str(self.root),
                    'USERPROFILE': str(self.root)}   # Path.home() on Windows
        self.assertEqual(Path(shutil.which('claude', path=self.env['PATH'])).parent, self.bin, 'the fake comes before any real claude')

    def stream(self, *events):
        self.fake.joinpath('stream.jsonl').write_text(
            ''.join(e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) + '\n' for e in events),
            encoding='utf-8')

    def argv(self, *extra, worktree=None, detached=False):
        return [sys.executable, str(SCRIPT), '--out', str(self.out), '--worktree', str(worktree or self.worktree),
                '--task-file', str(self.task), *([] if detached else ['--foreground']), *extra]

    def run_it(self, *extra, worktree=None, detached=False, **env):
        r = subprocess.run(self.argv(*extra, worktree=worktree, detached=detached), env={**self.env, **env},
                           capture_output=True, text=True, timeout=30)
        return self.detach(r) if detached else r

    def start(self, *argv, **kw):
        proc = subprocess.Popen(list(argv) or self.argv(), env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.addCleanup(lambda: proc.poll() is None and (kill(proc.pid), proc.wait()))   # its tree: Windows keeps claude
        return proc

    def detach(self, r):
        """A detached launcher writes its exit line before it exits, its logs open until then (Windows will not
        remove them): its exit is waited for before the test's directory goes."""
        if 'launcher pid ' in r.stdout:
            pid = int(r.stdout.split('launcher pid ')[1].split(',')[0])
            self.addCleanup(self.wait_until, 'the launcher to exit', lambda: not pid_alive(pid))
        return r

    def wait_until(self, what, ok):
        deadline = time.monotonic() + 10
        while not ok():
            if time.monotonic() > deadline:
                self.fail(f'timed out waiting for {what}')
            time.sleep(0.02)

    def logged(self, want):
        log = self.out / 'progress.log'
        return log.exists() and want in log.read_text(encoding='utf-8')

    def progress(self):
        """[(seq, text)], the clock checked and dropped."""
        rows = []
        for line in (self.out / 'progress.log').read_text(encoding='utf-8').splitlines():
            seq, clock, rest = line.split(' ', 2)
            self.assertRegex(clock, r'^\d\d:\d\d:\d\d$')
            rows.append((int(seq), rest))
        return rows

    def texts(self):
        return [t for _, t in self.progress()]

    def chief_lines(self):
        return [t for t in self.texts() if t.startswith('chief: ')]

    def test_the_real_capture_forwards_chief_lines_only(self):
        self.fake.joinpath('stream.jsonl').write_bytes(CAPTURE.read_bytes())
        r = self.run_it('--permission-mode', 'bypassPermissions')
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.progress()
        self.assertEqual([s for s, _ in rows], list(range(1, len(rows) + 1)))
        texts = [t for _, t in rows]
        self.assertTrue(texts[0].startswith('launcher: started pid '), texts)
        self.assertEqual(texts[1], 'launcher: session 372b0a62-ee03-4b94-bd3d-fca6209d4db0')
        self.assertEqual(texts[2:4], ['chief: stage · probe', 'chief: stage · done'])
        self.assertTrue(texts[4].startswith('launcher: exit 0 (result ok) · report '), texts)
        self.assertEqual(len(texts), 5, 'the worker\'s "chief: worker line" must not be forwarded')
        self.assertEqual((self.out / 'report.md').read_text(encoding='utf-8'), 'chief: stage · done\n')
        self.assertEqual((self.out / 'session').read_text(), '372b0a62-ee03-4b94-bd3d-fca6209d4db0\n')
        for name in ('progress.log', 'report.md', 'session'):   # read line-wise by other tools: no CRLF on Windows
            self.assertNotIn(b'\r', (self.out / name).read_bytes(), name)
        self.assertEqual((self.out / 'stream.jsonl').read_bytes(), CAPTURE.read_bytes())
        self.assertIn(b'fake stderr', (self.out / 'stderr.log').read_bytes())

    def test_the_child_gets_the_task_the_worktree_and_a_clean_env(self):
        self.stream(init(), result())
        lead = {k: 'lead' for k in ('CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_MESSAGING_SOCKET',
                                    'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_BRIDGE_SESSION_ID',
                                    'CLAUDE_CODE_SESSION_ATTENDED')}
        self.assertEqual(self.run_it('--permission-mode', 'bypassPermissions', **lead).returncode, 0)
        self.assertEqual((self.fake / 'stdin.txt').read_text(), 'Babysit PR 1.\n')
        self.assertEqual(Path((self.fake / 'cwd').read_text()).resolve(), self.worktree.resolve())
        self.assertEqual(json.loads((self.fake / 'argv.json').read_text()),
                         ['-p', '--agent', 'chief', '--output-format', 'stream-json', '--verbose',
                          '--permission-mode', 'bypassPermissions'])
        env = json.loads((self.fake / 'env.json').read_text())
        self.assertNotIn('CLAUDECODE', env)
        self.assertFalse([k for k in env if k.startswith('HERDR_')], env.keys())
        self.assertFalse(lead.keys() & env.keys(), 'none of the lead session\'s identity reaches chief')
        self.assertEqual(env['CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'], '0')

    def test_only_the_first_line_of_a_message_is_a_status_line(self):
        self.stream(
            init(),
            text('m1', 'chief: stage · one\nnarration\n```\nchief: a pasted reply\n```'),
            text('m2', 'prose first\nchief: not at the start'),
            text('m3', 'narration', 'chief: a second text block'),
            text('m3', 'chief: a later event of the same message'),
            {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': 'm4', 'content': [{'type': 'thinking', 'thinking': ''}]}},
            text('m4', '\n\nchief: stage · ü → ✓'),
            text('m5', '    chief: indented', ''),
            result())
        self.assertEqual(self.run_it().returncode, 0)
        self.assertEqual(self.texts()[2:-1], [
            'chief: stage · one',
            'launcher: warning: a status line later in a message was not forwarded; see stream.jsonl',
            'chief: stage · ü → ✓'])

    def test_a_progress_note_in_thinking_is_forwarded_as_a_note(self):
        self.stream(
            init(),
            {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': 'm1', 'content': [
                {'type': 'thinking', 'thinking': 'Cycle 2/5 wrapped: pushed abc.\nSecond line.'}]}},
            {'type': 'assistant', 'parent_tool_use_id': 'toolu_1', 'message': {'id': 'w1', 'content': [
                {'type': 'thinking', 'thinking': 'a worker note'}]}},
            text('m2', 'chief: cycle · 2/5'),
            result())
        self.assertEqual(self.run_it().returncode, 0)
        self.assertEqual(self.texts()[2:-1], ['note: Cycle 2/5 wrapped: pushed abc. Second line.', 'chief: cycle · 2/5'])

    def test_the_report_keeps_its_indentation(self):
        self.stream(init(), result('    first command\n    second command\n\n'))
        self.assertEqual(self.run_it().returncode, 0)
        self.assertEqual((self.out / 'report.md').read_text(encoding='utf-8'), '    first command\n    second command\n')

    def test_worker_text_never_matches(self):
        self.stream(
            init(),
            {'type': 'user', 'parent_tool_use_id': 'toolu_1',
             'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'chief: subagent prompt'}]}},
            text('w1', 'chief: a worker assistant event', parent='toolu_1'),
            {'type': 'user', 'parent_tool_use_id': None, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': [{'type': 'text', 'text': 'chief: worker reply'}]}]}},
            {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': 'm1', 'content': [
                {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Agent', 'input': {'prompt': 'chief: in a tool input'}}]}},
            result())
        self.assertEqual(self.run_it().returncode, 0)
        self.assertEqual(self.chief_lines(), [])

    def test_a_malformed_line_warns_once_and_reading_goes_on(self):
        self.stream(init(), 'not json\n', '[1, 2]\n',
                    {'type': 'assistant', 'parent_tool_use_id': None, 'message': 'oops'},
                    {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': 'm0', 'content': [{'type': 'text', 'text': None}]}},
                    text('m1', 'chief: stage · after the noise'), result())
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        texts = self.texts()
        self.assertEqual(texts.count('launcher: warning: unusable stdout lines; see stderr.log'), 1, texts)
        self.assertIn('chief: stage · after the noise', texts)
        err = (self.out / 'stderr.log').read_bytes()
        for bad in (b'not json', b'[1, 2]', b'"oops"'):
            self.assertIn(bad, err)

    def test_exit_paths(self):
        sig = int(signal.SIGTERM)
        cases = [
            ('no result', (init(),), {}, 1, 'no result event', False),
            ('error result', (init(), result('boom', is_error=True, subtype='error_during_execution')), {}, 1,
             'result is an error (error_during_execution)', True),
            ('empty result', (init(), result('  ')), {}, 1, 'result has no text', False),
            ('prompt blocked by a hook', (init(), {**result('UserPromptSubmit operation blocked by hook:\nx'), 'num_turns': 0}),
             {}, 1, 'no model turn; see stream.jsonl', False),
            ('claude failed', (init(), result()), {'FAKE_RC': '3'}, 3, 'claude exited 3', True),
            *([('claude crashed', (init(), result()), {'FAKE_RC': str(0xC0000005)}, 1, 'claude exited 0xC0000005', True)]
              if os.name == 'nt' else []),   # an NTSTATUS is no exit code a caller could get
            ('claude killed', (init(), result()), {'FAKE_SIGNAL': str(sig)}, 128 + sig, f'claude killed by signal {sig}', True)
            if os.name != 'nt' else   # Windows ends a process with an exit code, never a signal
            ('claude killed', (init(), result()), {'FAKE_SIGNAL': str(sig)}, sig, f'claude exited {sig}', True),
        ]
        for name, events, env, want, reason, report in cases:
            with self.subTest(name):
                self.stream(*events)
                r = self.run_it(**env)
                self.assertEqual(r.returncode, want, r.stderr)
                self.assertTrue(self.texts()[-1].startswith(f'launcher: exit {want} ({reason})'), self.texts())
                self.assertEqual((self.out / 'report.md').exists(), report)
                shutil.rmtree(self.out)

    def test_a_result_that_leaves_chiefs_own_work_running_or_killed_is_incomplete(self):
        # the shapes of claude 2.1.310's task events (agentrc#289): a unit returns while its own background Bash runs
        def task(sub, tid, **kw):
            return {'type': 'system', 'subtype': sub, 'task_id': tid, **kw}

        def thinking(msg_id):
            return {'type': 'assistant', 'parent_tool_use_id': None,
                    'message': {'id': msg_id, 'content': [{'type': 'thinking', 'thinking': ''}]}}
        unit = 'a2fe9c5420ae63043'
        dispatched = (init(), text('m1', 'chief: stage · W1 dispatched'),
                      task('task_started', unit, description='Fix R1-2', task_type='local_agent', is_backgrounded=True),
                      task('task_started', 'b0w29yqzt', description='Rerun full\nsuite', task_type='local_bash',
                           is_backgrounded=True, parent_task_id=unit),
                      task('task_updated', unit, patch={'status': 'completed'}),
                      task('task_notification', unit, status='completed'),
                      {'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [
                          {'task_id': 'b0w29yqzt', 'task_type': 'local_bash', 'description': 'Rerun full\nsuite',
                           'parent_task_id': unit}]})
        killed = (task('task_updated', 'b0w29yqzt', patch={'status': 'killed'}),
                  task('task_notification', 'b0w29yqzt', status='stopped'),
                  {'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': []})
        waiting = text('m2', 'W1 is running its suite. Still waiting for its final result.')
        wait = (task('task_started', 'bwait', description='Wait', task_type='local_bash', is_backgrounded=True),)
        wait_killed = (task('task_updated', 'bwait', patch={'status': 'killed'}), task('task_notification', 'bwait', status='stopped'))
        child = f'b0w29yqzt {{}} "Rerun full suite" under {unit} "Fix R1-2"'
        cases = [
            ('result before kill', (*dispatched, waiting, result(waiting['message']['content'][0]['text']), *killed),
             child.format('stopped')),
            ('kill before result', (*dispatched, waiting, *killed, result('W1 is running.')), child.format('stopped')),
            ('a completed unit with a live child', (*dispatched, waiting, result('W1 is running.')), child.format('running')),
            # one message straddles the kill: its later block is no proof chief took the kill in
            ("a kill inside chief's last message", (init(), *wait, thinking('m2'), *wait_killed,
                                                     text('m2', 'W1 is done.'), result('W1 is done.')), 'bwait stopped "Wait"'),
        ]
        for name, events, cut in cases:
            with self.subTest(name):
                self.stream(*events)
                self.assertEqual(self.run_it().returncode, 1)
                reason = f"incomplete: the session ended on chief's own work: {cut}"
                self.assertTrue(self.texts()[-1].startswith(f'launcher: exit 1 ({reason}) · report '), self.texts())
                self.assertTrue((self.out / 'report.md').read_text(encoding='utf-8').startswith(f'**{reason}**\n\nW1 '))
                shutil.rmtree(self.out)
        settled = (task('task_updated', 'b0w29yqzt', patch={'status': 'completed'}),
                   task('task_notification', 'b0w29yqzt', status='completed'),
                   {'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': []})
        stop = {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': 'm2', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'TaskStop', 'input': {'task_id': 'bwait'}}]}}
        unit_went_on = (*dispatched[:4], *killed, task('task_progress', unit), *dispatched[4:6])
        done = (text('m3', 'chief: stage · done'), result())
        for name, events in (('normal completion', (*dispatched, *settled, *done)),
                             ('a TaskStop naming it', (init(), *wait, stop, *wait_killed, *done)),
                             ("chief's next turn after the kill", (init(), *wait, *wait_killed, init(), *done)),
                             ('its unit went on past the kill', (*unit_went_on, *done))):
            with self.subTest(name):
                self.stream(*events)
                self.assertEqual(self.run_it().returncode, 0)
                self.assertTrue(self.texts()[-1].startswith('launcher: exit 0 (result ok) · report '), self.texts())
                self.assertEqual((self.out / 'report.md').read_text(encoding='utf-8'), 'chief: stage · done\n')
                shutil.rmtree(self.out)

    def transcript(self, config, dollars=0.2):
        """Session s-1's transcripts under config/projects: chief's and one preflight agent's, on Haiku."""
        session = config / 'projects' / '-wt' / 's-1'
        run = session / 'subagents' / 'workflows' / 'wf_a'
        run.mkdir(parents=True)
        usage = {'input_tokens': 50000, 'output_tokens': 10000, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
        turn = lambda mid: json.dumps({'type': 'assistant', 'timestamp': '2026-09-25T10:00:00Z',
                                       'message': {'id': mid, 'model': 'claude-haiku-4-5', 'usage': usage}}) + '\n'
        (run / 'agent-a1.meta.json').write_text(json.dumps({'description': 'preflight'}))
        (run / 'agent-a1.jsonl').write_text(turn('m1'))
        state = {'type': 'cost-state', 'modelUsage': {'claude-haiku-4-5': {
            'costUSD': dollars, 'inputTokens': 100000, 'outputTokens': 20000, 'cacheReadInputTokens': 0, 'cacheCreationInputTokens': 0}}}
        session.with_suffix('.jsonl').write_text(turn('m2') + json.dumps(state) + '\n')

    def test_the_session_cost_is_tabled_and_ends_the_exit_line(self):
        self.transcript(self.root / '.claude')
        self.stream(init(), result())
        self.assertEqual(self.run_it().returncode, 0)
        self.assertTrue(self.texts()[-1].endswith(' · cost $0.20'), self.texts())
        table = (self.out / 'cost.md').read_text(encoding='utf-8')
        for name in ('cost.md', 'report.md'):
            self.assertNotIn(b'\r', (self.out / name).read_bytes(), name)
        self.assertIn('| wf_a | preflight | haiku-4-5 |', table)
        report = (self.out / 'report.md').read_text(encoding='utf-8')
        self.assertTrue(report.startswith('chief: stage · done\n\n## Launches\n\n| # | outcome |'), report)
        self.assertIn('## Usage by stage and agent', report)
        self.assertTrue(report.endswith(f'Per launch × stage × model and the time breakdown: {self.out / "cost.md"}\n'), report)
        self.assertNotIn('| wf_a | preflight |', report, 'the full table stays in cost.md')

    def test_the_brief_tables_go_before_chiefs_first_section(self):
        spec = importlib.util.spec_from_file_location('chief_run', SCRIPT)
        chief_run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(chief_run)
        self.assertEqual(chief_run.with_brief('chief: x\n\n# PR\n\nverdict\n\n## Final state\n- HEAD\n', 'B'),
                         'chief: x\n\n# PR\n\nverdict\n\nB\n\n## Final state\n- HEAD\n')
        self.assertEqual(chief_run.with_brief('chief: x\n', 'B'), 'chief: x\n\nB\n', 'no section: after it all')
        fenced = 'chief: x\n\n```md\n## quoted\n```\n\n## Final\n'
        self.assertEqual(chief_run.with_brief(fenced, 'B'), 'chief: x\n\n```md\n## quoted\n```\n\nB\n\n## Final\n', 'not inside a fence')
        tilde = 'chief: x\n\n~~~~\n```\n## quoted\n~~~\n~~~~\n\n## Final\n'
        self.assertEqual(chief_run.with_brief(tilde, 'B'), 'chief: x\n\n~~~~\n```\n## quoted\n~~~\n~~~~\n\nB\n\n## Final\n',
                         'only a fence of its character and length closes it')

    def test_an_ntstatus_is_named_in_hex_and_ends_the_run_with_1(self):
        spec = importlib.util.spec_from_file_location('chief_run', SCRIPT)
        chief_run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(chief_run)
        self.assertEqual(chief_run.verdict(0xC000013A, result(), 'x'), (1, 'claude exited 0xC000013A'))
        self.assertEqual(chief_run.verdict(3, result(), 'x'), (3, 'claude exited 3'))

    def test_a_cost_it_cannot_write_still_leaves_an_exit_line(self):
        spec = importlib.util.spec_from_file_location('chief_run', SCRIPT)
        chief_run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(chief_run)
        blocker = self.root / 'not-a-dir'
        blocker.write_text('')
        with mock.patch.object(chief_run.run_cost, 'report', lambda session, full_path: (full_path.write_text('| t |'), 'B', '1.00')), \
                mock.patch.object(chief_run.run_cost, 'session_dir', lambda session_id: self.root):
            self.assertRegex(chief_run.cost(blocker, 's-1', blocker / 'report.md'), r'^cost none \((NotADirectoryError|FileNotFoundError): ')

    def test_the_session_cost_is_read_from_claude_config_dir_alone(self):
        cfg = self.root / 'cfg'
        self.transcript(self.root / '.claude', 9.99)   # the same session id in the default tree
        self.stream(init(), result())
        self.assertEqual(self.run_it(CLAUDE_CONFIG_DIR=str(cfg)).returncode, 0)
        self.assertRegex(self.texts()[-1], rf' · cost none \(run_cost: 0 transcripts named s-1\.jsonl under {re.escape(str(cfg))}',
                         'no fallback to the default tree')
        self.transcript(cfg)
        for config, dollars in ((str(cfg), '0.20'), ('', '9.99')):   # empty: HOME/.claude
            shutil.rmtree(self.out)
            self.assertEqual(self.run_it(CLAUDE_CONFIG_DIR=config).returncode, 0)
            self.assertTrue(self.texts()[-1].endswith(f' · cost ${dollars}'), (config, self.texts()))

    def test_a_cost_it_cannot_read_leaves_the_run_as_it_was(self):
        self.stream(init(), result())
        self.assertEqual(self.run_it().returncode, 0)
        self.assertRegex(self.texts()[-1], r' · cost none \(run_cost: 0 transcripts named s-1\.jsonl under .*\)$')
        self.assertFalse((self.out / 'cost.md').exists())
        self.assertEqual((self.out / 'report.md').read_text(encoding='utf-8'), 'chief: stage · done\n')

    def test_an_existing_out_dir_is_refused_untouched(self):
        self.out.mkdir()
        (self.out / 'progress.log').write_text('earlier run\n')
        self.stream(init(), result())
        r = self.run_it()
        self.assertEqual(r.returncode, 2)
        self.assertIn('exists', r.stderr)
        self.assertEqual((self.out / 'progress.log').read_text(), 'earlier run\n')
        self.assertFalse((self.fake / 'argv.json').exists(), 'claude must not start')

    def test_a_status_line_is_logged_before_chief_exits(self):
        self.stream(init(), text('m1', 'chief: stage · live'), '#gate go\n', result())
        proc = self.start()
        self.wait_until('the status line', lambda: self.logged('chief: stage · live'))
        self.assertIsNone(proc.poll(), 'chief was still running')
        self.assertIn('chief: stage · live', (self.out / 'stream.jsonl').read_text(encoding='utf-8'),
                      'the raw stream is on disk while chief runs')
        (self.fake / 'go').touch()
        self.assertEqual(proc.wait(timeout=10), 0)

    @unittest.skipIf(os.name == 'nt', 'Windows delivers no SIGTERM; test_ending_claude_on_windows_finishes_the_run')
    def test_a_sigterm_to_the_launcher_stops_chief_and_finishes_the_run(self):
        self.stream(init(), text('m1', 'chief: stage · live'), '#gate never\n', result())
        proc = self.start()
        self.wait_until('the status line', lambda: self.logged('chief: stage · live'))
        pid = int((self.fake / 'pid').read_text())
        proc.send_signal(signal.SIGTERM)
        sig = int(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 128 + sig)
        self.assertFalse(Path(f'/proc/{pid}').exists(), 'chief outlived the launcher')
        self.assertTrue(self.texts()[-1].startswith(f'launcher: exit {128 + sig} (claude killed by signal {sig})'), self.texts())

    @unittest.skipUnless(os.name == 'nt', 'the Windows way to stop a run')
    def test_ending_claude_on_windows_finishes_the_run(self):
        self.stream(init(), text('m1', 'chief: stage · live'), '#gate never\n', result())
        proc = self.start()
        self.wait_until('the status line', lambda: self.logged('chief: stage · live'))
        started = next(t for t in self.texts() if t.startswith('launcher: started pid '))
        kill(int(started.split()[3].rstrip(',')))   # taskkill /T /F on the started pid, as SKILL.md says
        self.assertEqual(proc.wait(timeout=10), 1)
        self.assertFalse(pid_alive(int((self.fake / 'pid').read_text())), 'chief outlived the launcher')
        self.assertTrue(self.texts()[-1].startswith('launcher: exit 1 (claude exited 1)'), self.texts())

    def test_it_returns_at_once_and_the_launcher_runs_on_in_its_own_session(self):
        self.stream(init(), text('m1', 'chief: stage · live'), '#gate go\n', result())
        r = self.run_it(detached=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        pid = int(r.stdout.split('launcher pid ')[1].split(',')[0])
        self.addCleanup(lambda: pid_alive(pid) and kill(pid))
        self.assertTrue(pid_alive(pid), 'it returned while the launcher runs on')
        self.assertFalse(self.logged('launcher: exit'), 'it returned while chief was held at its gate')
        self.assertIn(str(self.out / 'progress.log'), r.stdout)
        self.wait_until('the status line', lambda: self.logged('chief: stage · live'))
        if hasattr(os, 'getsid'):
            self.assertNotEqual(os.getsid(pid), os.getsid(0), 'a caller ending its process group does not reach it')
        else:   # Windows: DETACHED_PROCESS stands for the session, so no console event from the caller reaches it
            control = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], creationflags=subprocess.CREATE_NO_WINDOW)
            self.addCleanup(lambda: (control.kill(), control.wait()))
            self.wait_until('the probe to see a console where there is one', lambda: has_console(control.pid))
            self.assertFalse(has_console(pid), 'the launcher has no console')
        (self.fake / 'go').touch()
        self.wait_until('the exit line', lambda: self.logged('launcher: exit 0 (result ok)'))
        self.assertEqual((self.out / 'report.md').read_text(encoding='utf-8'), 'chief: stage · done\n')
        r = self.run_it(detached=True)
        self.assertEqual((r.returncode, r.stdout), (2, ''), 'an argument it refuses is refused before it detaches')
        self.assertIn('exists', r.stderr)

    @unittest.skipIf(os.name == 'nt', 'chmod(0) leaves a Windows file readable')
    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads any file')
    def test_an_unreadable_task_is_refused_before_it_detaches(self):
        self.task.chmod(0)
        r = self.run_it(detached=True)
        self.assertEqual((r.returncode, self.out.exists()), (2, False), 'an unreadable task is refused before it detaches')
        self.assertIn('cannot be read', r.stderr)

    def test_the_task_file_may_go_once_it_returns(self):
        # a forked launcher keeps the task open; the Windows one gets it on its stdin, not by path
        self.stream(init(), result())
        self.task.write_text('Babysit PR 2: ü ✓\n' * 2000, encoding='utf-8')   # more than a pipe holds
        want = self.task.read_bytes()
        r = self.run_it(detached=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.task.unlink()
        self.wait_until('the exit line', lambda: self.logged('launcher: exit'))
        self.assertTrue(self.texts()[-1].startswith('launcher: exit 0 (result ok)'), self.texts())
        self.assertEqual((self.fake / 'stdin.txt').read_bytes(), want)

    @unittest.skipIf(os.name == 'nt', 'the Windows launcher is the --_detached run of this command')
    def test_detached_is_no_option_where_the_launcher_forks(self):
        r = self.run_it('--_detached', detached=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn('unrecognized arguments: --_detached', r.stderr)
        self.assertFalse(self.out.exists())

    @unittest.skipUnless(os.name == 'nt', 'the Windows launcher is the --_detached run of this command')
    def test_a_detached_launcher_checks_again_what_its_caller_checked(self):
        self.stream(init(), result())
        detached = lambda: subprocess.run(self.argv('--_detached', detached=True), env=self.env, input=b'task', capture_output=True,
                                          timeout=30)
        self.out.mkdir()
        (self.out / 'progress.log').write_text('earlier run\n')
        r = detached()
        self.assertEqual(r.returncode, 2)
        self.assertIn(b'is not one', r.stderr)
        self.assertEqual((self.out / 'progress.log').read_text(), 'earlier run\n', 'an earlier run is left alone')
        shutil.rmtree(self.out)
        self.out.mkdir()
        self.installed('workflows', 'pr-babysit.js', 'new')
        self.project('workflows', 'pr-babysit.js', 'old')
        self.assertEqual(detached().returncode, 2)
        self.assertRegex(self.texts()[-1], r'^launcher: exit 2 \(.*\.claude/workflows/pr-babysit\.js.*\) · report none$')
        self.assertFalse((self.fake / 'argv.json').exists(), 'claude must not start')

    def cmd_claude(self, where, marker=None):
        """An npm-style claude.cmd: cmd.exe running the fake, or only touching marker."""
        where.mkdir(exist_ok=True)
        if not marker:
            (self.fake / 'claude').write_text(FAKE, encoding='utf-8')   # make_stub keeps no source beside its launcher
        run = f'type nul > "{marker}"' if marker else f'"{sys.executable}" "{self.fake / "claude"}" %*'
        (where / 'claude.cmd').write_text(f'@echo off\r\n{run}\r\nexit /b %errorlevel%\r\n')
        return where

    @unittest.skipUnless(os.name == 'nt', 'an npm claude.cmd is cmd.exe running node')
    def test_a_launcher_that_fails_stops_the_whole_claude_tree(self):
        self.env['PATH'] = f'{self.cmd_claude(self.root / "npm")}{os.pathsep}{os.environ["PATH"]}'
        self.stream(init(), 'not json\n', '#gate never\n', result())
        failing = (f'import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import chief_run\n'
                   'line = chief_run.Progress.line\n'
                   'def broken(self, text):\n    if text.startswith("launcher: warning"): raise OSError("disk full")\n    line(self, text)\n'
                   'chief_run.Progress.line = broken\nsys.exit(chief_run.main(sys.argv[1:]))')
        r = self.detach(subprocess.run([sys.executable, '-c', failing, *self.argv(detached=True)[2:]], env=self.env, capture_output=True,
                                          text=True, timeout=30))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_until('the exit line', lambda: self.logged('launcher: exit 1 (launcher failed: OSError: disk full)'))
        pid = int((self.fake / 'pid').read_text())
        self.addCleanup(lambda: pid_alive(pid) and kill(pid))
        self.assertFalse(pid_alive(pid), "claude.cmd's node stopped with its cmd.exe")

    @unittest.skipUnless(os.name == 'nt', 'shutil.which and CreateProcess try the working directory first')
    def test_claude_is_found_on_path_alone(self):
        self.stream(init(), result())
        env = {k: v for k, v in self.env.items() if k.lower() != 'nodefaultcurrentdirectoryinexepath'}   # Claude Code sets it
        empty = self.root / 'empty'
        empty.mkdir()
        checkout = self.cmd_claude(self.root / 'checkout', self.root / 'checkout-ran')   # the caller's directory
        r = subprocess.run(self.argv(), env={**env, 'PATH': str(empty)}, cwd=checkout, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertTrue(self.texts()[-1].startswith('launcher: exit 127 (could not start claude: '), self.texts())
        self.assertFalse((self.root / 'checkout-ran').exists(), "the caller's directory is no place to find claude")
        shutil.rmtree(self.out)
        self.cmd_claude(self.bin, self.root / 'cmd-ran')   # beside the claude.exe
        r = subprocess.run(self.argv(), env=env, cwd=checkout, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.root / 'cmd-ran').exists(), '.exe before .cmd')

    @unittest.skipUnless(os.name == 'nt', 'the Windows run lock')
    def test_a_run_lock_it_cannot_take_is_a_clean_refusal(self):
        self.git_worktree()
        (self.worktree / '.git' / 'chief').write_text('')   # where the lock's directory goes
        self.stream(init(), result())
        for detached in (False, True):
            r = self.run_it(detached=detached)
            self.assertEqual((r.returncode, r.stdout), (2, ''), r.stderr)
            self.assertIn('cannot take the run lock', r.stderr)
            self.assertNotIn('Traceback', r.stderr)
            self.assertFalse(self.out.exists())

    def test_a_refusal_after_the_run_lock_releases_it(self):
        # Windows frees a closed handle's lock only after a delay: a refusal must not leave it to that
        spec = importlib.util.spec_from_file_location('chief_run', SCRIPT)
        chief_run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(chief_run)
        released = []
        with mock.patch.object(chief_run, 'lock_held', lambda worktree, keep: chief_run.HELD.append('lock')), \
                mock.patch.object(chief_run, 'release', lambda: released.append(chief_run.HELD.pop())), \
                mock.patch.object(chief_run, 'refusal', lambda worktree, fix: 'stale roles'), \
                mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            chief_run.main(self.argv()[2:])
        self.assertEqual(released, ['lock'])
        self.assertFalse(self.out.exists())

    def test_a_detached_launchers_own_output_is_appended_after_claudes(self):
        # the Windows launcher inherits stderr.log as a handle with no append mode, at offset 0
        self.out.mkdir()
        launcher = (f'import sys, argparse, pathlib; sys.path.insert(0, {str(SCRIPT.parent)!r}); import chief_run\n'
                    'out = pathlib.Path(sys.argv[1])\n'
                    'def launch(a, task):\n'
                    '    with (out / "stderr.log").open("ab") as err: err.write(b"claude stderr\\n")\n'
                    '    raise RuntimeError("boom")\n'
                    'chief_run.launch, chief_run.refusal, chief_run.lock_held = launch, lambda *a: None, lambda *a, **k: None\n'
                    'a = argparse.Namespace(out=out, worktree=pathlib.Path(sys.argv[2]))\n'
                    'chief_run.detached(a, lambda: chief_run.detached_launch(a))')
        with (self.out / 'stderr.log').open('wb') as log:
            subprocess.run([sys.executable, '-c', launcher, str(self.out), str(self.worktree)], input=b'task', stdout=log,
                           stderr=log, env=self.env, timeout=30)
        logged = (self.out / 'stderr.log').read_bytes()
        self.assertTrue(logged.startswith(b'claude stderr\n'), logged)
        self.assertIn(b'RuntimeError: boom', logged)

    def test_a_launcher_that_fails_detached_still_writes_its_exit_line(self):
        failing = (f'import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import chief_run\n'
                   'def launch(a, task):\n    chief_run.Progress(a.out / "progress.log").line("launcher: started")\n    raise RuntimeError("boom")\n'
                   'chief_run.launch = launch\nsys.exit(chief_run.main(sys.argv[1:]))')
        r = self.detach(subprocess.run([sys.executable, '-c', failing, *self.argv(detached=True)[2:]], env=self.env, capture_output=True, text=True, timeout=30))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_until('the exit line', lambda: self.logged('launcher: exit 1'))
        self.assertEqual(self.progress(), [(1, 'launcher: started'), (2, 'launcher: exit 1 (launcher failed: RuntimeError: boom) · report none')])
        self.assertIn('RuntimeError: boom', (self.out / 'stderr.log').read_text(encoding='utf-8', errors='replace'))

    def test_a_launcher_that_fails_while_chief_runs_stops_chief_before_its_exit_line(self):
        self.stream(init(), 'not json\n', '#gate never\n', result())
        failing = (f'import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import chief_run\n'
                   'line = chief_run.Progress.line\n'
                   'def broken(self, text):\n    if text.startswith("launcher: warning"): raise OSError("disk full")\n    line(self, text)\n'
                   'chief_run.Progress.line = broken\nsys.exit(chief_run.main(sys.argv[1:]))')
        r = self.detach(subprocess.run([sys.executable, '-c', failing, *self.argv(detached=True)[2:]], env=self.env, capture_output=True, text=True, timeout=30))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_until('the exit line', lambda: self.logged('launcher: exit 1 (launcher failed: OSError: disk full)'))
        self.assertFalse(pid_alive(int((self.fake / 'pid').read_text())), 'chief stopped before the exit line')

    def test_a_claude_that_cannot_start_is_recorded(self):
        empty = self.root / 'empty'
        empty.mkdir()
        r = self.run_it(PATH=str(empty))
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertTrue(self.texts()[-1].startswith('launcher: exit 127 (could not start claude: '), self.texts())
        self.assertIn(b'could not start claude', (self.out / 'stderr.log').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'reads /proc; test_a_second_launch_in_the_worktree_is_refused covers Windows')
    def test_a_worktree_with_a_running_chief_is_refused(self):
        self.stream('#gate never\n')
        self.start('sleep', '30', cwd=self.worktree)          # in the worktree, but not a chief
        other = self.start(str(self.bin / 'claude'), '-p', '--verbose', '--agent', 'chief', cwd=self.worktree)
        self.wait_until('the other chief', (self.fake / 'pid').exists)
        (self.fake / 'pid').unlink()
        r = self.run_it()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(f'a chief already runs in {self.worktree} (pid {other.pid})', r.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse((self.fake / 'pid').exists(), 'no second chief started')
        elsewhere = self.root / 'wt2'
        elsewhere.mkdir()
        self.stream(init(), result())
        r = self.run_it(worktree=elsewhere)
        self.assertEqual(r.returncode, 0, f'another worktree is not blocked: {r.stderr}')

    def git_worktree(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        subprocess.run(['git', 'init', '-q', str(self.worktree)], check=True, env=env)

    def test_a_second_launch_in_the_worktree_is_refused(self):
        self.git_worktree()
        self.stream(init(), text('m1', 'chief: stage · live'), '#gate go\n', result())
        proc = self.start()
        self.wait_until('the status line', lambda: self.logged('chief: stage · live'))
        first, self.out = self.out, self.root / 'run2'
        for detached in (False, True):
            r = self.run_it(detached=detached)
            self.assertEqual((r.returncode, r.stdout), (2, ''), r.stderr)
            self.assertIn(f'a chief already runs in {self.worktree}', r.stderr)
            self.assertFalse(self.out.exists())
        (self.fake / 'go').touch()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.out = first
        self.assertTrue(self.logged('launcher: exit 0 (result ok)'))
        self.out = self.root / 'run3'
        self.assertEqual(self.run_it().returncode, 0, 'the lock goes with its launcher')

    @unittest.skipUnless(os.name == 'nt', 'the Windows launcher takes the run lock after its caller checked it')
    def test_a_detached_launcher_that_loses_the_lock_writes_its_exit_line(self):
        self.git_worktree()
        self.stream(init(), text('m1', 'chief: stage · live'), '#gate go\n', result())
        proc = self.start()
        self.wait_until('the status line', lambda: self.logged('chief: stage · live'))
        first, self.out = self.out, self.root / 'run2'
        self.out.mkdir()   # as its caller left it
        (self.fake / 'argv.json').unlink()
        r = subprocess.run(self.argv('--_detached', detached=True), env=self.env, input=b'task', capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertRegex(self.texts()[-1], r'^launcher: exit 2 \(a chief already runs in .*run\.lock\) · report none$')
        self.assertFalse((self.fake / 'argv.json').exists(), 'no second claude started')
        (self.fake / 'go').touch()
        self.assertEqual(proc.wait(timeout=10), 0, 'the first run went on')

    def installed(self, kind, name, content, root=None, scope=None):
        """An agentrc entry linked into the user scope as install.py links it."""
        src = (root or self.root / 'agentrc') / kind / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(content)
        link = (scope or self.root / '.claude') / kind / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(src)
        return src

    def project(self, kind, name, content):
        path = self.worktree / '.claude' / kind / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def assert_refused(self, r, named=(), not_named=()):
        self.assertEqual(r.returncode, 2, r.stderr)
        for want in named:
            self.assertIn(want, r.stderr)
        for unwanted in not_named:
            self.assertNotIn(unwanted, r.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse((self.fake / 'argv.json').exists(), 'claude must not start')

    def test_a_stale_tracked_workflow_is_refused_before_launch(self):
        self.stream(init(), result())
        self.installed('workflows', 'pr-babysit.js', 'new')
        stale = self.project('workflows', 'pr-babysit.js', 'old')
        self.assert_refused(self.run_it(), ['.claude/workflows/pr-babysit.js', 'merge the default branch', str(self.root / '.claude')])
        r = self.run_it(detached=True)
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assert_refused(r)
        stale.unlink()
        self.assertEqual(self.run_it().returncode, 0)

    def test_stale_roles_are_refused_chief_included(self):
        self.stream(init(), result())
        for name in ('code-writer.md', 'chief.md'):
            self.installed('agents', name, 'new')
            self.project('agents', name, 'old')
        self.assert_refused(self.run_it(), ['.claude/agents/code-writer.md', '.claude/agents/chief.md'])

    def test_a_copy_that_shadows_nothing_installed_is_let_through(self):
        self.stream(init(), result())
        self.installed('workflows', 'pr-babysit.js', 'same')
        self.project('workflows', 'pr-babysit.js', 'same')
        mine = self.root / '.claude' / 'agents' / 'mine.md'   # the user's own role, which a project may override
        mine.parent.mkdir(parents=True)
        mine.write_text('user')
        self.project('agents', 'mine.md', 'project')
        self.project('agents', 'hw-debugger.md', 'not installed')
        self.installed('agents', 'code-writer.md', 'new').unlink()   # a dangling link
        self.project('agents', 'code-writer.md', 'old')
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_peer_installed_from_another_checkout_counts(self):
        self.stream(init(), result())
        self.installed('workflows', 'pr-babysit.js', 'new', root=self.root / 'other-agentrc')
        copy = self.project('workflows', 'pr-babysit.js', 'old')
        self.assert_refused(self.run_it(), ['.claude/workflows/pr-babysit.js'])
        copy.write_text('new')
        self.assertEqual(self.run_it().returncode, 0)

    def test_the_user_scope_follows_claude_config_dir(self):
        self.stream(init(), result())
        cfg = self.root / 'cfg'
        self.installed('workflows', 'pr-babysit.js', 'old')
        self.installed('workflows', 'pr-babysit.js', 'new', root=self.root / 'agentrc-new', scope=cfg)
        copy = self.project('workflows', 'pr-babysit.js', 'new')
        self.assertEqual(self.run_it(CLAUDE_CONFIG_DIR=str(cfg)).returncode, 0)
        shutil.rmtree(self.out)
        (self.fake / 'argv.json').unlink()
        self.assert_refused(self.run_it(), ['.claude/workflows/pr-babysit.js'])
        copy.write_text('old')
        self.assert_refused(self.run_it(CLAUDE_CONFIG_DIR=str(cfg)), [str(cfg)])

    def test_symlinked_project_entries_are_followed(self):
        self.stream(init(), result())
        self.installed('workflows', 'pr-babysit.js', 'new')
        self.installed('agents', 'code-writer.md', 'new')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'pr-babysit.js').write_text('old')
        (outside / 'code-writer.md').write_text('old')
        (self.worktree / '.claude' / 'agents').mkdir(parents=True)
        (self.worktree / '.claude' / 'workflows').symlink_to(outside)
        (self.worktree / '.claude' / 'agents' / 'code-writer.md').symlink_to(outside / 'code-writer.md')
        self.assert_refused(self.run_it(), ['.claude/workflows/pr-babysit.js', '.claude/agents/code-writer.md'])

    @unittest.skipIf(os.name == 'nt', 'chmod(0) does not make a Windows directory unreadable')
    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads any directory')
    def test_an_unreadable_project_or_installed_directory_is_refused_without_merge_advice(self):
        self.stream(init(), result())
        self.installed('agents', 'code-writer.md', 'new')
        project = self.project('agents', 'code-writer.md', 'old').parent
        for unreadable in (project, self.root / '.claude' / 'agents'):
            with self.subTest(unreadable=unreadable):
                unreadable.chmod(0)
                try:
                    self.assert_refused(self.run_it(), ['cannot inspect', str(unreadable)], ['merge the default branch'])
                finally:
                    unreadable.chmod(0o755)

    def test_a_catalogue_it_cannot_read_is_an_error_not_an_empty_one(self):
        spec = importlib.util.spec_from_file_location('shadows', SCRIPT.with_name('shadows.py'))
        shadows = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shadows)
        agentrc = self.root / 'agentrc-partial'
        (agentrc / 'agents').mkdir(parents=True)
        with mock.patch.object(shadows, 'AGENTRC', agentrc), self.assertRaises(FileNotFoundError) as caught:
            shadows.shadows(self.worktree)
        self.assertEqual(Path(caught.exception.filename), agentrc / 'workflows')


if __name__ == '__main__':
    unittest.main()
