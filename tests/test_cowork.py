import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'cowork' / 'scripts' / 'cowork.py'
SKILL = ROOT / 'skills' / 'cowork' / 'SKILL.md'
sys.path.insert(0, str(SCRIPT.parent))
import cowork  # noqa: E402

# Stand-ins for the real CLIs: record argv, stdin, cwd and environment, then emit an
# event stream. With FAKE_GATE set they hold until that file exists, so a test can
# keep a turn busy exactly as long as its assertions need.
FAKE_PREAMBLE = '''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(json.dumps({'argv': args, 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),
    'env': {k: v for k, v in os.environ.items() if k.startswith(('COWORK', 'HERDR', 'CLAUDECODE'))}}) + '\\n')
while os.environ.get('FAKE_GATE') and not os.path.exists(os.environ['FAKE_GATE']):
    time.sleep(0.02)
'''
FAKE_CODEX = FAKE_PREAMBLE + '''print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-42'}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'hi'}}))
if os.environ.get('FAKE_EXIT', '0') != '0':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'usage limit reached'}}))
    sys.stderr.buffer.write(os.environ.get('FAKE_STDERR', '').encode('latin-1'))
    sys.exit(int(os.environ['FAKE_EXIT']))
open(args[args.index('-o') + 1], 'w').write(os.environ.get('FAKE_REPLY', 'codex reply\\nFiles touched: none\\n'))
'''
FAKE_CLAUDE = FAKE_PREAMBLE + '''if os.environ.get('FAKE_EXIT', '0') != '0':
    sys.exit(int(os.environ['FAKE_EXIT']))
print(json.dumps({'type': 'assistant', 'text': 'thinking'}))
print(os.environ.get('FAKE_RESULT', json.dumps({'type': 'result', 'result': 'claude reply\\nFiles touched: a.c'})))
'''


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def until(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('timed out waiting')
        time.sleep(0.02)


class CoworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / 'repo'
        self.root.mkdir()
        sh(self.root, 'git', 'init', '-q', '-b', 'main')
        self.bin = base / 'bin'
        self.bin.mkdir()
        for name, body in (('codex', FAKE_CODEX), ('claude', FAKE_CLAUDE)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = base / 'calls.jsonl'
        self.gate = base / 'gate'
        clean = {k: v for k, v in os.environ.items() if not k.startswith(('FAKE_', 'HERDR_', 'COWORK'))}
        self.env = mock.patch.dict('os.environ', {
            **clean, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'FAKE_LOG': str(self.log), 'CLAUDECODE': '1'}, clear=True)
        self.env.start()
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.background = []

    def tearDown(self):
        self.gate.touch()  # release anything still gated
        for proc in self.background:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
        for box in Path(self.tmp.name).glob('repo/.git/cowork/*'):  # runners hang off init, not off us
            for request in cowork.requests(box):
                subprocess.run(['pkill', '-9', '-f', f'cowork.py _run \\S+ {request} '], stderr=subprocess.DEVNULL)
                pid = cowork.lock_lines((box / f'{request}.lock').read_text())[0]
                if pid and cowork.holds(int(pid), box / f'{request}.lock'):
                    os.kill(int(pid), 9)
        os.chdir(self.cwd)
        self.env.stop()
        self.tmp.cleanup()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def codex_does(self, snippet):
        """A fake codex that runs `snippet` (Python) before replying."""
        (self.bin / 'codex').write_text(FAKE_CODEX.replace("while os.environ.get('FAKE_GATE')",
                                                           f"{snippet}\nwhile os.environ.get('FAKE_GATE')", 1))

    def run_cli(self, *argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             mock.patch('sys.stdin', io.StringIO(stdin)):
            try:
                code = cowork.main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def send(self, *argv, stdin='', **env):
        """A blocking send, as the harness would run it in the background."""
        with mock.patch.dict('os.environ', env):
            code, out, err = self.run_cli('send', *argv, stdin=stdin)
        request, _, reply = out.partition('\n')
        return code, request, reply, err

    def send_gated(self, *argv, **env):
        """A send whose coworker holds until `self.gate` exists; returns (process, id)."""
        proc = subprocess.Popen([sys.executable, str(SCRIPT), 'send', *argv], cwd=self.root,
                                stdout=subprocess.PIPE, text=True, env={**os.environ, 'FAKE_GATE': str(self.gate), **env})
        self.background.append(proc)
        return proc, proc.stdout.readline().strip()

    def box(self, side='codex'):
        return self.root / '.git' / 'cowork' / side

    def started(self, request):
        until(lambda: cowork.lock_lines((self.box() / f'{request}.lock').read_text())[0] != '')

    def reaped(self, *argv, **env):
        """A gated send whose process is killed before it can deliver, as the
        harness reaper would; returns the request id, still undelivered."""
        proc, request = self.send_gated(*argv, **env)
        self.started(request)
        proc.kill()
        proc.wait()
        return request

    def settled(self, request):
        self.gate.touch()
        until(lambda: not cowork.held(self.box() / f'{request}.lock'))

    def leftovers(self, request):
        return sorted(p.suffix for p in self.box().glob(f'{request}.*'))

    # --- sessions -------------------------------------------------------------

    def test_first_send_starts_a_codex_thread_and_the_next_resumes_it(self):
        code, request, reply, _ = self.send('--task', 'do a thing')
        self.assertEqual(code, 0)
        self.assertTrue(request.startswith('codex-'))
        self.assertEqual(reply, 'codex reply\nFiles touched: none\n')
        self.assertEqual((self.box() / 'session').read_text(), 'thread-42\n')
        self.send('--task', 'another')
        first, second = self.calls()
        self.assertEqual(first['argv'][:2], ['exec', '--json'])
        self.assertEqual(second['argv'][:3], ['exec', 'resume', 'thread-42'])
        self.assertIn('-o', second['argv'])

    def test_claude_gets_a_chosen_session_id_and_is_resumed_by_it(self):
        code, _, reply, _ = self.send('--to', 'claude', '--task', 'q', CLAUDECODE='')
        self.assertEqual(code, 0)
        self.assertEqual(reply, 'claude reply\nFiles touched: a.c\n')
        session = (self.box('claude') / 'session').read_text().strip()
        self.assertEqual(len(session), 36)
        self.send('--to', 'claude', '--task', 'q2', CLAUDECODE='')
        first, second = self.calls()
        self.assertEqual(first['argv'][-2:], ['--session-id', session])
        self.assertEqual(second['argv'][-2:], ['--resume', session])
        self.assertIn('stream-json', first['argv'])

    def test_the_coworker_defaults_to_codex_only_inside_claude_code(self):
        code, _, _, _ = self.send('--task', 'q', CLAUDECODE='')
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), [])

    def test_a_failed_first_claude_turn_binds_no_session(self):
        code, _, _, _ = self.send('--to', 'claude', '--task', 'q', CLAUDECODE='', FAKE_EXIT='1')
        self.assertEqual(code, 1)
        self.assertFalse((self.box('claude') / 'session').exists())
        code, _, _, _ = self.send('--to', 'claude', '--task', 'q', CLAUDECODE='')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls()[1]['argv'][-2], '--session-id', 'a fresh id, not a resume of nothing')

    def test_reset_forgets_the_session_and_removes_undelivered_requests(self):
        self.send('--task', 'a')
        undelivered = self.reaped('--task', 'b')
        self.settled(undelivered)
        code, out, _ = self.run_cli('reset', 'codex')
        self.assertEqual(code, 0)
        self.assertIn('1 undelivered request(s) removed', out)
        self.assertEqual(sorted(p.name for p in self.box().iterdir()), ['lock'])
        self.gate.unlink()
        self.send('--task', 'c')
        self.assertNotIn('resume', self.calls()[-1]['argv'])

    # --- the prompt and the coworker's environment ------------------------------

    def test_bootstrap_only_on_the_first_turn_and_a_header_every_turn(self):
        self.send('--task', 'first task')
        self.send('--no-edit', '--task', 'second task')
        first, second = self.calls()
        self.assertIn('coworker on the cowork channel', first['stdin'])
        self.assertNotIn('coworker on the cowork channel', second['stdin'])
        for call in (first, second):
            self.assertRegex(call['stdin'], r'cowork request codex-\S+ from claude')
            self.assertIn('Files touched', call['stdin'])
            self.assertEqual(call['env']['COWORK_TURN'], call['stdin'].split('cowork request ')[1].split()[0],
                             'COWORK_TURN names the request so the gate stays out')
        self.assertTrue(first['stdin'].endswith('---\nfirst task'))
        self.assertIn('Scope: edit and commit', first['stdin'])
        self.assertIn('Scope: do not edit anything', second['stdin'])

    def test_no_edit_is_plan_mode_for_claude_and_checked_afterwards_for_codex(self):
        self.send('--to', 'claude', '--no-edit', '--task', 'review', CLAUDECODE='')
        claude = self.calls()[-1]
        self.assertEqual(claude['argv'][claude['argv'].index('--permission-mode') + 1], 'plan')
        self.codex_does("open('stray', 'w').close()")
        code, _, reply, err = self.send('--no-edit', '--task', 'review')
        self.assertEqual(code, cowork.MALFORMED)
        self.assertIn('tree changed during a --no-edit turn', err)
        self.assertEqual(reply, 'codex reply\nFiles touched: none\n', 'the reply is still shown')
        (self.root / 'stray').unlink()
        code, _, _, _ = self.send('--task', 'edit')
        self.assertEqual(code, 0, 'an edit turn may change the tree')

    def test_no_edit_sees_every_kind_of_change(self):
        sh(self.root, 'git', 'config', 'user.email', 't@t')
        sh(self.root, 'git', 'config', 'user.name', 't')
        (self.root / 'dirty.txt').write_text('v1')
        sh(self.root, 'git', 'add', 'dirty.txt')
        sh(self.root, 'git', 'commit', '-qm', 'base')
        (self.root / 'newdir').mkdir()
        (self.root / 'newdir' / 'a').write_text('a')
        (self.root / 'café.txt').write_text('v1')
        (self.root / 'p').write_text('abc')
        (self.root / 'q').write_text('def')
        (self.root / 'link').symlink_to('missing-a')
        (self.root / 'dirty.txt').write_text('staged')
        sh(self.root, 'git', 'add', 'dirty.txt')
        (self.root / 'dirty.txt').write_text('v1')  # index and worktree differ before the turn
        cases = {
            'a clean-to-clean empty commit': "import subprocess; subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'x'])",
            'a file inside an untracked directory': "open('newdir/a', 'w').write('b')",
            'a path git would quote': "open('café.txt', 'w').write('v2')",
            'a staged blob under identical worktree bytes': "import subprocess; open('dirty.txt', 'w').write('other'); subprocess.run(['git', 'add', 'dirty.txt']); open('dirty.txt', 'w').write('v1')",
            'bytes moved across a file boundary': "open('p', 'w').write('ab'); open('q', 'w').write('cdef')",
            'a dangling symlink retargeted': "os.unlink('link'); os.symlink('missing-b', 'link')",
            'a tracked file that .gitignore also matches': "open('gen', 'w').write('v2')",
            'a staged file, same size and mtime': "st = os.stat('gen2'); open('gen2', 'w').write('v2'); os.utime('gen2', ns=(st.st_atime_ns, st.st_mtime_ns))",
            'a conflict stage under an unchanged worktree': "import subprocess; blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], input=b'other', capture_output=True).stdout.decode().strip(); subprocess.run(['git', 'update-index', '--index-info'], input=f'100644 {blob} 2\\tclash\\n'.encode())",
        }
        (self.root / '.gitignore').write_text('gen\n')
        (self.root / 'gen').write_text('v1')
        sh(self.root, 'git', 'add', '-f', 'gen')
        sh(self.root, 'git', 'commit', '-qm', 'tracked but ignored')
        (self.root / '.gitignore').write_text('gen*\n')
        (self.root / 'clash').write_text('base')
        sh(self.root, 'git', 'add', 'clash')
        sh(self.root, 'git', 'commit', '-qm', 'clash base')
        sh(self.root, 'git', 'checkout', '-qb', 'other')
        (self.root / 'clash').write_text('theirs')
        sh(self.root, 'git', 'commit', '-qam', 'theirs')
        sh(self.root, 'git', 'checkout', '-q', 'main')
        (self.root / 'clash').write_text('ours')
        sh(self.root, 'git', 'commit', '-qam', 'ours')
        for name, sabotage in cases.items():
            if name.startswith('a staged'):  # staged now, so no later commit sweeps it in; ignored, uncommitted
                (self.root / 'gen2').write_text('v1')
                sh(self.root, 'git', 'add', '-f', 'gen2')
            if name.startswith('a conflict'):  # last: git refuses commits while clash is unmerged
                subprocess.run(['git', 'merge', 'other'], cwd=self.root, capture_output=True)
            with self.subTest(name):
                self.codex_does(sabotage)
                code, _, _, err = self.send('--no-edit', '--task', 'review')
                self.assertEqual(code, cowork.MALFORMED)
                self.assertIn('tree changed', err)

    def test_the_coworker_runs_at_the_root_without_the_drivers_pane_identity(self):
        sub = self.root / 'sub'
        sub.mkdir()
        os.chdir(sub)
        self.send('--task', 'x', HERDR_ENV='1', HERDR_PANE_ID='w1:p1')
        call = self.calls()[0]
        self.assertEqual(call['cwd'], str(self.root))
        self.assertEqual(set(call['env']), {'COWORK_TURN'}, 'no HERDR_* and no CLAUDECODE reach the coworker')

    def test_task_sources(self):
        task = self.root / 'task.md'
        task.write_text('from file')
        code, _, _, _ = self.send('--task-file', str(task))
        self.assertEqual(code, 0)
        code, _, _, _ = self.send('--task', '-', stdin='from stdin')
        self.assertEqual(code, 0)
        self.assertTrue(self.calls()[0]['stdin'].endswith('from file'))
        self.assertTrue(self.calls()[1]['stdin'].endswith('from stdin'))
        code, _, _, _ = self.send('--task', 'task.md')
        self.assertEqual(code, 0, 'a literal is a literal, even one that names a file')
        self.assertTrue(self.calls()[2]['stdin'].endswith('task.md'))

    def test_an_empty_task_never_reaches_the_coworker(self):
        task = self.root / 'empty.md'
        task.write_text('  \n')
        for argv in (('--task', ''), ('--task', '-'), ('--task-file', str(task))):
            code, _, _, err = self.send(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn('resolved to nothing', err)
        self.assertEqual(self.calls(), [])

    # --- outcomes -----------------------------------------------------------------

    def test_a_failing_codex_turn_reports_its_jsonl_error_and_stderr(self):
        code, request, reply, _ = self.send('--task', 'x', FAKE_EXIT='2', FAKE_STDERR='bad \xff bytes')
        self.assertEqual(code, 1)
        self.assertIn('exited 2', reply)
        self.assertIn('usage limit reached', reply, 'the cause lives only in the event stream')
        self.assertIn('bad � bytes', reply, 'undecodable stderr does not crash the report')
        self.assertEqual(self.leftovers(request), [], 'delivered, so nothing stays')

    def test_codex_exiting_clean_without_a_reply_is_a_failure(self):
        (self.bin / 'codex').write_text(FAKE_CODEX.replace("open(args[args.index('-o') + 1], 'w')", "open(os.devnull, 'w')"))
        code, _, reply, _ = self.send('--task', 'x')
        self.assertEqual(code, 1)
        self.assertIn('without writing its last message', reply)
        self.assertTrue((self.box() / 'session').exists(), 'the thread exists and resumes')

    def test_a_claude_error_result_is_a_failure_not_an_empty_reply(self):
        error = json.dumps({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True, 'errors': ['turn limit']})
        code, _, reply, _ = self.send('--to', 'claude', '--task', 'q', CLAUDECODE='', FAKE_RESULT=error)
        self.assertEqual(code, 1)
        self.assertIn('turn limit', reply)
        code, _, reply, _ = self.send('--to', 'claude', '--task', 'q', CLAUDECODE='', FAKE_RESULT='not json at all')
        self.assertEqual(code, 1)
        self.assertIn('without a usable result', reply)

    def test_a_reply_without_the_files_line_is_flagged(self):
        code, _, reply, err = self.send('--task', 'x', FAKE_REPLY='did it\n')
        self.assertEqual(code, cowork.MALFORMED)
        self.assertEqual(reply, 'did it\n', 'the reply is still shown')
        self.assertIn('no "Files touched" line', err)

    def test_a_missing_cli_is_reported_not_raised(self):
        (self.bin / 'codex').unlink()
        code, _, reply, _ = self.send('--task', 'x', PATH=f'{self.bin}:{os.path.dirname(shutil.which("git"))}')
        self.assertEqual(code, 1)
        self.assertIn('failed', reply)
        self.assertIn('No such file', reply)

    def test_publish_is_all_or_nothing_and_the_first_verdict_wins(self):
        target = self.root / 'x.exit'
        self.assertTrue(cowork.publish(target, '0\n'))
        self.assertFalse(cowork.publish(target, 'died\n'))
        self.assertEqual(target.read_text(), '0\n')
        self.assertFalse(list(self.root.glob('*.tmp')))

    # --- the queue ------------------------------------------------------------------

    def test_queued_sends_run_in_order_on_the_same_session(self):
        first, first_id = self.send_gated('--task', 'one')
        second, second_id = self.send_gated('--task', 'two')
        third, third_id = self.send_gated('--task', 'three')
        self.started(first_id)
        _, out, _ = self.run_cli('status')
        self.assertIn(f'{first_id}  running', out)
        self.assertIn(f'{third_id}  queued', out)
        self.gate.touch()
        for proc in (first, second, third):
            self.assertEqual(proc.wait(timeout=30), 0)
            self.assertIn('Files touched: none', proc.stdout.read())
        tasks = [c['stdin'].rsplit('\n', 1)[-1] for c in self.calls()]
        self.assertEqual(tasks, ['one', 'two', 'three'])
        self.assertNotIn('resume', self.calls()[0]['argv'])
        self.assertEqual(self.calls()[1]['argv'][:3], ['exec', 'resume', 'thread-42'])
        self.assertEqual(self.calls()[2]['argv'][:3], ['exec', 'resume', 'thread-42'])
        self.assertIn('coworker on the cowork channel', self.calls()[0]['stdin'])
        self.assertNotIn('coworker on the cowork channel', self.calls()[1]['stdin'], 'bootstrap decided at run time')

    def test_a_failed_turn_skips_what_was_queued_behind_it(self):
        first, first_id = self.send_gated('--task', 'one', FAKE_EXIT='2')
        second, second_id = self.send_gated('--task', 'two')
        self.gate.touch()
        self.assertEqual(first.wait(timeout=30), 1)
        self.assertEqual(second.wait(timeout=30), 1)
        out = second.stdout.read()
        self.assertIn('was skipped', out)
        self.assertIn(f'queued behind {first_id}, which ended 2', out)
        self.assertEqual(len(self.calls()), 1, 'the second never reached the coworker')
        code, _, _, _ = self.send('--task', 'three')
        self.assertEqual(code, 0, 'a request sent after the failure is not behind it')

    def test_a_corpse_in_the_box_neither_blocks_nor_poisons(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-00000000-000000-000000.task').write_text('p')
        (box / 'codex-00000000-000000-000000.lock').touch()  # nobody holds it
        code, _, _, _ = self.send('--task', 'after a corpse')
        self.assertEqual(code, 0)
        _, out, _ = self.run_cli('status')
        self.assertIn('codex-00000000-000000-000000  died', out)

    def test_a_runner_that_dies_while_queued_skips_what_was_behind_it(self):
        first, first_id = self.send_gated('--task', 'slow')
        second, second_id = self.send_gated('--task', 'doomed')
        third, third_id = self.send_gated('--task', 'behind doomed')
        self.started(first_id)
        runner = int(subprocess.run(['pgrep', '-f', f'_run codex {second_id}'], capture_output=True, text=True).stdout.split()[0])
        os.kill(runner, 9)  # the runner dies while still queued; nothing holds its lock
        self.gate.touch()
        self.assertEqual(first.wait(timeout=30), 0)
        self.assertEqual(third.wait(timeout=30), 1)
        self.assertIn(f'queued behind {second_id}, which ended died', third.stdout.read())
        self.assertEqual(second.wait(timeout=10), 1, 'the send behind the dead runner reports it')

    def test_reset_refuses_while_requests_are_pending(self):
        proc, request = self.send_gated('--task', 'slow')
        code, _, err = self.run_cli('reset', 'codex')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn(request, err)
        self.gate.touch()
        proc.wait(timeout=30)
        code, _, _ = self.run_cli('reset', 'codex')
        self.assertEqual(code, 0)

    # --- kills --------------------------------------------------------------------------

    def test_kill_stops_a_running_request_and_what_was_behind_it_is_skipped(self):
        first, first_id = self.send_gated('--task', 'slow')
        second, second_id = self.send_gated('--task', 'next')
        self.started(first_id)
        code, out, _ = self.run_cli('kill', first_id)
        self.assertEqual(out.strip(), f'{first_id}: killed')
        self.assertEqual(first.wait(timeout=10), 1)
        self.assertIn('was killed', first.stdout.read())
        self.assertFalse(cowork.held(self.box() / f'{first_id}.lock'), 'nothing of the turn survives')
        self.assertEqual(second.wait(timeout=30), 1)
        self.assertIn('which ended killed', second.stdout.read())

    def test_kill_a_queued_request_before_it_starts(self):
        first, first_id = self.send_gated('--task', 'slow')
        second, second_id = self.send_gated('--task', 'never')
        self.started(first_id)
        code, out, _ = self.run_cli('kill', second_id)
        self.assertEqual(out.strip(), f'{second_id}: killed')
        self.assertEqual(second.wait(timeout=10), 1)
        self.gate.touch()
        self.assertEqual(first.wait(timeout=30), 0, 'the running one is untouched')
        self.assertEqual(len(self.calls()), 1)

    def test_kill_takes_tools_that_left_the_process_group(self):
        self.codex_does("import subprocess; open('tool.pid', 'w').write(str(subprocess.Popen(['sleep', '60'], start_new_session=True).pid))")
        proc, request = self.send_gated('--task', 'x')
        until(lambda: (self.root / 'tool.pid').exists())
        self.run_cli('kill', request)
        proc.wait(timeout=10)
        with self.assertRaises(ProcessLookupError, msg='a tool in its own session died with the tree'):
            os.kill(int((self.root / 'tool.pid').read_text()), 0)

    def test_kill_freezes_the_tree_so_nothing_spawned_meanwhile_escapes(self):
        self.codex_does('import subprocess; subprocess.Popen(["sh", "-c", "while :; do setsid sh -c \\"sleep 1; touch late\\" & sleep 0.05; done"])')
        proc, request = self.send_gated('--task', 'x')
        self.started(request)
        self.run_cli('kill', request)
        proc.wait(timeout=10)
        (self.root / 'late').unlink(missing_ok=True)  # anything that landed before the kill
        time.sleep(1.2)
        self.assertFalse((self.root / 'late').exists())

    def test_a_request_stays_live_while_its_lock_is_held_even_with_a_verdict(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        (box / 'codex-x.exit').write_text('killed\n')
        with (box / 'codex-x.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # a kill was published; the teardown is not done
            self.assertEqual(cowork.live(box), ['codex-x'])
            code, _, _ = self.run_cli('read', 'codex-x')
            self.assertEqual(code, cowork.BUSY, 'a verdict does not make teardown complete')
            code, _, _ = self.run_cli('reset', 'codex')
            self.assertEqual(code, cowork.BUSY)
        self.assertEqual(cowork.live(box), [])

    def test_kill_signals_only_a_pid_that_holds_the_request_lock(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        stranger = subprocess.Popen(['sleep', '30'])  # a reused pid: a process that never had the lock
        with (box / 'codex-x.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # a runner still holds it, but its CLI is gone
            lock.write(f'{stranger.pid}\n')
            lock.flush()
            with mock.patch.object(cowork, 'kill_tree') as kill:
                code, out, _ = self.run_cli('kill', 'codex-x')
            self.assertEqual(out.strip(), 'codex-x: killed')
            kill.assert_not_called()
        stranger.kill()
        stranger.wait()
        (box / 'codex-y.task').write_text('p')
        with (box / 'codex-y.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            orphan = subprocess.Popen(['sleep', '30'], pass_fds=(lock.fileno(),))
            lock.write(f'{orphan.pid}\n')
        try:
            self.assertTrue(cowork.holds(orphan.pid, box / 'codex-y.lock'))
            code, out, _ = self.run_cli('kill', 'codex-y')
            self.assertEqual(orphan.wait(timeout=5), -9, 'the pid that holds the lock is ours to kill')
        finally:
            if orphan.poll() is None:
                orphan.kill()
                orphan.wait()

    def test_kill_on_a_finished_request_signals_nothing(self):
        request = self.reaped('--task', 'done')
        self.settled(request)
        (self.box() / f'{request}.lock').write_text(f'{os.getpid()}\n0\n')  # a reused pid: ours
        with mock.patch.object(cowork, 'kill_tree') as kill:
            code, out, _ = self.run_cli('kill', request)
        self.assertEqual(out.strip(), f'{request}: 0')
        kill.assert_not_called()

    def test_a_kill_that_wins_during_conclude_is_the_verdict_the_successor_reads(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        (box / 'codex-x.jsonl').touch()
        (box / 'codex-x.err').touch()
        with (box / 'codex-x.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)

            def kill_meanwhile(*args, **kwargs):
                cowork.publish(box / 'codex-x.exit', 'killed\n')
                return '0'
            with mock.patch.object(cowork, 'conclude', kill_meanwhile), \
                 contextlib.redirect_stderr(io.StringIO()):
                cowork.run('codex', box, self.root, 'codex-x', None, None, False, lock.fileno())
            self.assertEqual(cowork.lock_lines((box / 'codex-x.lock').read_text())[1], 'killed')
            self.assertEqual((box / 'codex-x.exit').read_text(), 'killed\n')

    def test_a_successor_reading_the_verdict_does_not_look_like_a_live_runner(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.lock').write_text('4242\n0\n')  # the runner wrote its verdict and is exiting
        (box / 'codex-x.exit').write_text('0\n')
        (box / 'codex-x.reply').write_text('done\nFiles touched: none\n')
        (box / 'codex-x.err').touch()
        with (box / 'codex-x.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # something the CLI spawned still has the runner's lock
            self.assertTrue(cowork.held(box / 'codex-x.lock'), 'a verdict line is not release')
            code, _, _ = self.run_cli('read', 'codex-x')
            self.assertEqual(code, cowork.BUSY)
        with (box / 'codex-x.lock').open() as reader:
            fcntl.flock(reader, fcntl.LOCK_SH)
            self.assertFalse(cowork.held(box / 'codex-x.lock'))
            code, out, _ = self.run_cli('read', 'codex-x')
            self.assertEqual((code, out), (0, 'done\nFiles touched: none\n'))

    def test_kill_of_a_delivered_request_leaves_no_orphan_verdict(self):
        _, request, _, _ = self.send('--task', 'done')
        code, _, err = self.run_cli('kill', request)
        self.assertEqual(code, cowork.BUSY)
        self.assertEqual(self.leftovers(request), [])

    # --- the runner outlives the caller ---------------------------------------------

    def test_a_killed_send_loses_nothing(self):
        proc, request = self.send_gated('--task', 'x')
        self.started(request)
        runner = int(subprocess.run(['pgrep', '-f', f'_run codex {request}'], capture_output=True, text=True).stdout.split()[0])
        with open(f'/proc/{runner}/stat') as stat:
            self.assertNotEqual(int(stat.read().rsplit(')', 1)[1].split()[1]), proc.pid, 'the runner is not a child of send')
        proc.kill()
        proc.wait()
        self.settled(request)
        self.assertEqual((self.box() / f'{request}.exit').read_text(), '0\n')
        self.assertEqual((self.box() / f'{request}.reply').read_text(), 'codex reply\nFiles touched: none\n')
        self.assertEqual((self.box() / 'session').read_text(), 'thread-42\n', 'the thread was still bound')
        _, out, _ = self.run_cli('status')
        self.assertIn(f'{request}  0', out, 'undelivered, so still listed')
        code, out, _ = self.run_cli('read', request)
        self.assertEqual((code, out), (0, 'codex reply\nFiles touched: none\n'))
        self.assertEqual(self.leftovers(request), [])

    def test_a_dead_runner_with_a_live_coworker_is_busy_until_killed(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        with (box / 'codex-x.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            orphan = subprocess.Popen(['sleep', '30'], pass_fds=(lock.fileno(),))
            lock.write(f'{orphan.pid}\n')
        try:
            _, out, _ = self.run_cli('status')
            self.assertIn('codex-x  running', out)
            code, _, err = self.run_cli('reset', 'codex')
            self.assertEqual(code, cowork.BUSY)
            code, out, _ = self.run_cli('kill', 'codex-x')
            self.assertEqual(out.strip(), 'codex-x: killed')
            self.assertEqual(orphan.wait(timeout=5), -9, 'killed, not waited for')
        finally:
            if orphan.poll() is None:
                orphan.kill()
                orphan.wait()
        code, _, _ = self.run_cli('reset', 'codex')
        self.assertEqual(code, 0)

    def test_read_delivers_once_what_a_dead_send_left_and_refuses_a_live_request(self):
        proc, request = self.send_gated('--task', 'x')
        self.started(request)
        code, _, err = self.run_cli('read', request)
        self.assertEqual(code, cowork.BUSY)
        self.assertIn(f'{request} is running', err)
        proc.kill()
        proc.wait()
        self.settled(request)
        code, out, _ = self.run_cli('read', request)
        self.assertEqual((code, out), (0, 'codex reply\nFiles touched: none\n'))
        self.assertEqual(self.leftovers(request), [])
        code, _, err = self.run_cli('read', request)
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('delivered already', err)

    def test_send_delivers_and_leaves_nothing_of_the_request(self):
        _, request, _, _ = self.send('--task', 'x')
        self.assertEqual(self.leftovers(request), [])
        self.assertEqual(sorted(p.name for p in self.box().iterdir()), ['lock', 'session'])
        code, _, err = self.run_cli('read', request)
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('delivered already', err)

    def test_read_reports_a_reaped_send_exactly_as_send_would_have(self):
        for env, want in (({}, (0, 'codex reply\nFiles touched: none\n', '')),
                          ({'FAKE_REPLY': 'missing footer\n'}, (cowork.MALFORMED, 'missing footer\n', 'no "Files touched" line')),
                          ({'FAKE_EXIT': '2'}, (cowork.FAILED, 'exited 2', ''))):
            with self.subTest(env=env):
                request = self.reaped('--task', 'x', **env)
                self.settled(request)
                code, out, err = self.run_cli('read', request)
                self.assertEqual(code, want[0])
                self.assertIn(want[1], out)
                self.assertIn(want[2], err)
                self.gate.unlink()

    def test_a_runner_killed_outright_is_delivered_as_died(self):
        self.box().mkdir(parents=True)
        (self.box() / 'codex-x.task').write_text('p')
        (self.box() / 'codex-x.lock').touch()  # the runner never wrote a verdict; nobody holds the lock
        code, out, _ = self.run_cli('read', 'codex-x')
        self.assertEqual(code, cowork.FAILED)
        self.assertIn('codex-x died without recording a verdict', out)
        self.assertEqual(self.leftovers('codex-x'), [])

    def test_watch_reports_each_undelivered_request_once_and_replays_only_what_was_asked(self):
        earlier = self.reaped('--task', 'before')
        self.settled(earlier)
        self.gate.unlink()
        unasked = self.reaped('--task', 'not replayed', FAKE_REPLY='no footer\n')
        self.settled(unasked)
        self.gate.unlink()
        watch = subprocess.Popen([sys.executable, str(SCRIPT), 'watch', earlier], cwd=self.root,
                                 stdout=subprocess.PIPE, text=True)
        self.background.append(watch)
        self.assertEqual(watch.stdout.readline(), f'{earlier}   replied\n')
        first, first_id = self.send_gated('--task', 'one')
        second, second_id = self.send_gated('--task', 'two', FAKE_EXIT='2')
        third, third_id = self.send_gated('--task', 'three')
        self.started(first_id)
        for proc in (first, second, third):  # reaped before delivery, so the watch is the only ping
            proc.kill()
            proc.wait()
        self.gate.touch()
        lines = [watch.stdout.readline() for _ in range(3)]
        self.assertEqual(lines, [f'{first_id}   replied\n', f'{second_id}   exited 2\n',
                                 f'{third_id}   was skipped\n'])
        self.assertIsNone(watch.poll(), 'a watch outlives the requests it reported')
        watch.kill()
        watch.wait()
        self.assertNotIn(unasked, watch.stdout.read())
        for request in (first_id, second_id, third_id):
            self.run_cli('read', request)
        self.assertEqual(self.leftovers(first_id), [])
        code, _, _ = self.run_cli('watch', 'codex-nope')
        self.assertEqual(code, cowork.BUSY)

    def test_tail_follows_until_the_turn_settles_and_refuses_a_delivered_request(self):
        proc, request = self.send_gated('--task', 'x')
        self.started(request)
        tail = subprocess.Popen([sys.executable, str(SCRIPT), 'tail', request], cwd=self.root, stdout=subprocess.PIPE, text=True)
        self.background.append(tail)
        time.sleep(0.5)
        self.assertIsNone(tail.poll(), 'follows while the turn runs')
        self.gate.touch()
        self.assertEqual(tail.wait(timeout=10), 0, 'exits by itself once the lock is released')
        out = tail.stdout.read()
        self.assertIn('thread.started', out)
        self.assertIn('agent_message', out)
        proc.wait(timeout=30)
        code, _, err = self.run_cli('tail', request)
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('delivered already', err)
        self.assertIn('~/.codex/sessions', err)

    def test_tail_exits_quietly_when_its_reader_leaves(self):
        proc, request = self.send_gated('--task', 'x')
        self.started(request)
        tail = subprocess.Popen([sys.executable, str(SCRIPT), 'tail', request], cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.background.append(tail)
        tail.stdout.close()  # like `tail | head`
        self.gate.touch()
        self.assertEqual(tail.wait(timeout=10), 0)
        self.assertEqual(tail.stderr.read(), '')
        proc.wait(timeout=30)

    def test_the_jsonl_exists_the_instant_the_id_is_printed(self):
        proc, request = self.send_gated('--task', 'x')
        self.assertTrue((self.box() / f'{request}.jsonl').exists())
        self.gate.touch()
        proc.wait(timeout=30)

    def test_unknown_request(self):
        code, _, _ = self.run_cli('kill', 'codex-nope')
        self.assertEqual(code, cowork.BUSY)


class Frontmatter(unittest.TestCase):
    def test_declares_name_and_description(self):
        fm = yaml.safe_load(SKILL.read_text().split('---')[1])
        self.assertEqual(fm['name'], 'cowork')
        self.assertTrue(fm['description'].strip())

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == '__main__':
    unittest.main()
