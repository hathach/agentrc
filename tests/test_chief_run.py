"""Tests for headless-chief's chief_run.py against a fake `claude` replaying stream-json.

data/chief_run/stream.jsonl is a trimmed real capture (claude 2.1.280, one subagent call):
the subagent's reply reaches the stream only inside a top-level user tool_result, and no
worker assistant event was streamed, so worker exclusion is shown on the event types
that do appear."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'headless-chief' / 'scripts' / 'chief_run.py'
CAPTURE = Path(__file__).resolve().parent / 'data' / 'chief_run' / 'stream.jsonl'

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
d = pathlib.Path(os.environ['FAKE_DIR'])
(d / 'argv.json').write_text(json.dumps(sys.argv[1:]))
(d / 'env.json').write_text(json.dumps(dict(os.environ)))
(d / 'cwd').write_text(os.getcwd())
(d / 'pid').write_text(str(os.getpid()))
(d / 'stdin.txt').write_bytes(sys.stdin.buffer.read())
for line in (d / 'stream.jsonl').read_text(encoding='utf-8').splitlines(keepends=True):
    if line.startswith('#gate '):   # hold here until the test creates the named file
        gate, deadline = d / line.split()[1], time.monotonic() + 10
        while not gate.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        continue
    sys.stdout.write(line)
    sys.stdout.flush()
sys.stderr.write('fake stderr\n')
if os.environ.get('FAKE_SIGNAL'):
    os.kill(os.getpid(), int(os.environ['FAKE_SIGNAL']))
sys.exit(int(os.environ.get('FAKE_RC', '0')))
'''


def init(sid='s-1'):
    return {'type': 'system', 'subtype': 'init', 'session_id': sid}


def text(msg_id, *blocks, parent=None):
    return {'type': 'assistant', 'parent_tool_use_id': parent,
            'message': {'id': msg_id, 'role': 'assistant', 'content': [{'type': 'text', 'text': b} for b in blocks]}}


def result(body='chief: stage · done', is_error=False, subtype='success'):
    return {'type': 'result', 'subtype': subtype, 'is_error': is_error, 'result': body}


@unittest.skipIf(os.name == 'nt', 'headless-chief requires /proc and POSIX signals')
class ChiefRun(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.fake = self.root / 'fake'
        self.fake.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'claude').write_text(FAKE)
        (self.bin / 'claude').chmod(0o755)
        self.worktree = self.root / 'wt'
        self.worktree.mkdir()
        self.task = self.root / 'task.md'
        self.task.write_text('Babysit PR 1.\n')
        self.out = self.root / 'run'
        self.env = {**os.environ, 'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}', 'FAKE_DIR': str(self.fake),
                    'CLAUDECODE': '1', 'HERDR_PANE_ID': 'p1', 'HERDR_ENV': '1'}

    def stream(self, *events):
        self.fake.joinpath('stream.jsonl').write_text(
            ''.join(e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) + '\n' for e in events),
            encoding='utf-8')

    def argv(self, *extra, worktree=None):
        return [sys.executable, str(SCRIPT), '--out', str(self.out), '--worktree', str(worktree or self.worktree),
                '--task-file', str(self.task), *extra]

    def run_it(self, *extra, worktree=None, **env):
        return subprocess.run(self.argv(*extra, worktree=worktree), env={**self.env, **env},
                              capture_output=True, text=True, timeout=30)

    def start(self, *argv, **kw):
        proc = subprocess.Popen(list(argv) or self.argv(), env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc

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
        self.assertEqual((self.out / 'stream.jsonl').read_bytes(), CAPTURE.read_bytes())
        self.assertIn(b'fake stderr', (self.out / 'stderr.log').read_bytes())

    def test_the_child_gets_the_task_the_worktree_and_a_clean_env(self):
        self.stream(init(), result())
        self.assertEqual(self.run_it('--permission-mode', 'bypassPermissions').returncode, 0)
        self.assertEqual((self.fake / 'stdin.txt').read_text(), 'Babysit PR 1.\n')
        self.assertEqual(Path((self.fake / 'cwd').read_text()).resolve(), self.worktree.resolve())
        self.assertEqual(json.loads((self.fake / 'argv.json').read_text()),
                         ['-p', '--agent', 'chief', '--output-format', 'stream-json', '--verbose',
                          '--permission-mode', 'bypassPermissions'])
        env = json.loads((self.fake / 'env.json').read_text())
        self.assertNotIn('CLAUDECODE', env)
        self.assertFalse([k for k in env if k.startswith('HERDR_')], env.keys())
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
            ('claude failed', (init(), result()), {'FAKE_RC': '3'}, 3, 'claude exited 3', True),
            ('claude killed', (init(), result()), {'FAKE_SIGNAL': str(sig)}, 128 + sig, f'claude killed by signal {sig}', True),
        ]
        for name, events, env, want, reason, report in cases:
            with self.subTest(name):
                self.stream(*events)
                r = self.run_it(**env)
                self.assertEqual(r.returncode, want, r.stderr)
                self.assertTrue(self.texts()[-1].startswith(f'launcher: exit {want} ({reason})'), self.texts())
                self.assertEqual((self.out / 'report.md').exists(), report)
                shutil.rmtree(self.out)

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

    def test_a_claude_that_cannot_start_is_recorded(self):
        empty = self.root / 'empty'
        empty.mkdir()
        r = self.run_it(PATH=str(empty))
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertTrue(self.texts()[-1].startswith('launcher: exit 127 (could not start claude: '), self.texts())
        self.assertIn(b'could not start claude', (self.out / 'stderr.log').read_bytes())

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


if __name__ == '__main__':
    unittest.main()
