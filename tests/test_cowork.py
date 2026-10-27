import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import fcntl
except ImportError:  # Windows: an open write handle is the request lock
    fcntl = None
    import msvcrt
try:
    import yaml
except ImportError:  # a minimal native Python, such as MinGW's, may ship none
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'cowork' / 'scripts' / 'cowork.py'
SKILL = ROOT / 'skills' / 'cowork' / 'SKILL.md'
sys.path.insert(0, str(SCRIPT.parent))
import cowork  # noqa: E402
sys.path.insert(0, str(ROOT / 'tests'))
from helpers import guard_survivors, make_stub, pid_alive  # noqa: E402

# Stand-ins for the real CLIs: record argv, stdin, cwd and environment, then emit an
# event stream. With FAKE_GATE set they hold until that file exists, so a test can
# keep a turn busy exactly as long as its assertions need.
FAKE_PREAMBLE = '''#!/usr/bin/env python3
import json, os, sys, time
args, prompt = sys.argv[1:], sys.stdin.buffer.read().decode('utf-8')
open(os.environ['FAKE_LOG'], 'a').write(json.dumps({'argv': args, 'stdin': prompt, 'cwd': os.getcwd(),
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
open(args[args.index('-o') + 1], 'w', encoding='utf-8', newline='').write(
    os.environ.get('FAKE_REPLY', 'codex reply\\nFiles touched: none\\n'))
if os.environ.get('FAKE_USAGE'):
    print(json.dumps({'type': 'turn.completed', 'usage': json.loads(os.environ['FAKE_USAGE'])}))
if os.environ.get('FAKE_ROLLOUT'):  # what Codex records of the turn: turn n's two calls send 1000n and 1500n
    path = os.path.join(os.environ['CODEX_HOME'], 'sessions', '2026', '10', '07', 'rollout-2026-10-07T00-00-00-thread-42.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = 1 + (open(path).read().count('"task_started"') if os.path.exists(path) else 0)
    turn, lines = f'turn-{n}', [] if n > 1 else [{'type': 'session_meta', 'payload': {'id': 'thread-42'}}]
    lines += [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}},
              {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
                  {'type': 'input_text', 'text': prompt}],
                  'internal_chat_message_metadata_passthrough': {'turn_id': turn}}}]
    for call, (sent, cached) in enumerate(((1000 * n, 0), (1500 * n, 1000 * n))):
        lines.append({'type': 'token_usage_record', 'payload': {'thread_id': 'thread-42', 'turn_id': turn,
            'usage': {'input_tokens': sent, 'cached_input_tokens': cached, 'output_tokens': 10},
            'turn_token_usage': {'input_tokens': 1000 * n + sent * call, 'cached_input_tokens': cached,
                                 'output_tokens': 10 + 10 * call}}})
    if os.environ['FAKE_ROLLOUT'] != 'incomplete':
        lines.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn}})
    open(path, 'a').write(''.join(json.dumps(line) + '\\n' for line in lines))
'''
FAKE_CLAUDE = FAKE_PREAMBLE + '''if os.environ.get('FAKE_EXIT', '0') != '0':
    sys.exit(int(os.environ['FAKE_EXIT']))
print(os.environ.get('FAKE_ASSISTANT', json.dumps({'type': 'assistant', 'text': 'thinking'})))
print(os.environ.get('FAKE_RESULT', json.dumps({'type': 'result', 'result': 'claude reply\\nFiles touched: a.c'})))
'''


THREAD = '01a08a2e-cbbb-7de2-8b6f-b2e769c5b9d8'
CODEX = {'CLAUDECODE': '', 'CODEX_THREAD_ID': THREAD}  # a send driven by a Codex session


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def receipt(request, outcome='replied', code=0, used='usage unavailable', lane='codex/main'):
    """The last line a delivery prints to stderr; the fake reports no usage unless told to."""
    return f'cowork result {request} {lane}: {outcome}, exit {code}; {used}\n'


def task_of(prompt):
    """The task as the coworker received it, between the header and the trailer."""
    return prompt.split('---\n', 1)[1].rsplit('\n---\n', 1)[0]


def until(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('timed out waiting')
        time.sleep(0.02)


# A herdr that lists the checkout as a linked worktree open in a workspace (or STUB_HERDR_ENTRY), logging each call.
HERDR_STUB = '''import json, os, sys
open(os.environ['STUB_HERDR_LOG'], 'a').write(' '.join(sys.argv[1:]) + '\\n')
if os.environ.get('STUB_HERDR_DOWN'):
    sys.exit('herdr server unreachable')
entry = json.loads(os.environ.get('STUB_HERDR_ENTRY') or '{"is_linked_worktree": true, "open_workspace_id": "w1"}')
print(json.dumps({'result': {'worktrees': [{**entry, 'path': sys.argv[-1]}]}}))
'''
SLEEP = [sys.executable, '-c', 'import time; time.sleep(30)']
KILLED = 1 if os.name == 'nt' else -9  # what taskkill /F leaves for an exit code


@contextlib.contextmanager
def holding(path, mode='w'):
    """A request lock held as a runner holds it: flocked, or on Windows open for writing."""
    with open(path, mode) as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield lock


@contextlib.contextmanager
def reading(path):
    """Another reader of the request lock: a shared flock, or on Windows a probe as held() opens it."""
    if fcntl:
        with open(path) as reader:
            fcntl.flock(reader, fcntl.LOCK_SH)
            yield
        return
    handle = cowork.kernel32.CreateFileW(str(path), cowork.GENERIC_READ, cowork.FILE_SHARE_READ | cowork.FILE_SHARE_DELETE,
                                         None, cowork.OPEN_EXISTING, 0, None)
    assert handle != cowork.INVALID_HANDLE_VALUE
    try:
        yield
    finally:
        cowork.kernel32.CloseHandle(handle)


@contextlib.contextmanager
def letting_go(holder):
    """windows.retry, with `holder` (if any) closed once a step has failed on
    it, so the retry, not timing, lets it through. Yields the failures met."""
    met, retry = [], cowork.windows.retry

    def attempt(step):
        try:
            return step()
        except PermissionError as failure:
            met.append(failure)
            if holder:
                holder.close()
            raise
    with mock.patch.object(cowork.windows, 'retry', lambda step, tries=50: retry(lambda: attempt(step), tries)):
        yield met


def spawn_holding(lock, argv):
    """A process that inherits the request lock, as the runner's CLI does."""
    if fcntl is None:
        return cowork.nt_popen(argv, msvcrt.get_osfhandle(lock.fileno()))
    return subprocess.Popen(argv, pass_fds=(lock.fileno(),))


def gone(pid):
    if os.name == 'nt':
        return cowork.nt_born(pid) is None
    return not pid_alive(pid)


def launcher(bin_dir, name, script):
    """<bin_dir>/<name>.exe running `script` with this Python: CreateProcess
    runs only an .exe by name, and a missing stand-in must never fall through
    to the real CLI on PATH."""
    make_stub(bin_dir, name, f'import runpy\nrunpy.run_path({str(script)!r}, run_name="__main__")\n')


def setUpModule():
    guard_survivors(__name__)


class CoworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()  # as git and getcwd name it: Windows' long name, not TEMP's 8.3 one
        self.root = base / 'repo'
        self.root.mkdir()
        sh(self.root, 'git', 'init', '-q', '-b', 'main')
        self.bin = base / 'bin'
        self.bin.mkdir()
        self.stubs = base / 'stubs' if os.name == 'nt' else self.bin  # what the fake CLIs run
        self.stubs.mkdir(exist_ok=True)
        for name, body in (('codex', FAKE_CODEX), ('claude', FAKE_CLAUDE)):
            self.fake(name, body)
            if os.name == 'nt':
                launcher(self.bin, name, self.stubs / name)
        self.log = base / 'calls.jsonl'
        self.gate = base / 'gate'
        clean = {k: v for k, v in os.environ.items() if not k.startswith(('FAKE_', 'HERDR_', 'COWORK', 'CODEX_', 'CLAUDE_'))}
        self.env = mock.patch.dict('os.environ', {
            **clean, 'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}', 'FAKE_LOG': str(self.log), 'CLAUDECODE': '1',
            'HOME': str(base / 'home'), 'USERPROFILE': str(base / 'home'),
            'XDG_CONFIG_HOME': str(base / 'config'), 'CODEX_HOME': str(base / 'codex'),
            'HERDR_BIN_PATH': str(base / 'no-herdr'),  # no herdr: worktree lanes nest
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
            # Git for Windows enables fsmonitor system-wide: its daemon would outlive each repo, holding the dir
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.fsmonitor', 'GIT_CONFIG_VALUE_0': 'false'}, clear=True)
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
        for box in Path(self.tmp.name).glob('repo/.git/cowork/*/*'):  # runners hang off init, not off us
            for request in cowork.requests(box):
                lock = box / f'{request}.lock'
                if os.name == 'nt':  # the runner then terminates its CLI's job, as on `kill`
                    cowork.publish(box / f'{request}.exit', 'killed\n')
                else:
                    subprocess.run(['pkill', '-9', '-f', f'cowork.py _run \\S+ \\S+ {request} '], stderr=subprocess.DEVNULL)
                pid = cowork.cli_pid(lock)
                if pid and cowork.holds(int(pid), lock):
                    if os.name == 'nt':
                        cowork.kill_tree(int(pid))
                    else:
                        os.kill(int(pid), 9)
                if os.name == 'nt':  # the runner goes with its CLI
                    with contextlib.suppress(AssertionError):
                        until(lambda: not cowork.held(lock))
        os.chdir(self.cwd)
        self.env.stop()
        for attempt in reversed(range(50 if os.name == 'nt' else 1)):  # a process letting go may still be exiting
            try:
                self.tmp.cleanup()
                break
            except OSError:
                if not attempt:
                    raise
                time.sleep(0.1)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def fake(self, name, body):
        """What the fake `name` CLI runs: the executable itself, or on Windows the script its launcher runs."""
        (self.stubs / name).write_text(body, encoding='utf-8')
        (self.stubs / name).chmod(0o755)

    def codex_does(self, snippet):
        """A fake codex that runs `snippet` (Python) before replying."""
        self.fake('codex', FAKE_CODEX.replace("while os.environ.get('FAKE_GATE')",
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
        """A send run in-process: blocking, as the harness would run it in the background, or --detach."""
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

    def box(self, side='codex', lane='main'):
        return self.root / '.git' / 'cowork' / side / lane

    def started(self, request):
        until(lambda: cowork.cli_pid(self.box() / f'{request}.lock') != '')

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

    def read_wait(self, request):
        argv = [str(SCRIPT)]
        if os.name == 'nt':  # it polls without holding the lock open: a sentinel marks that it waits
            polling = Path(self.tmp.name) / f'polling-{len(self.background)}'
            argv = ['-c', f'import pathlib, sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import cowork\n'
                          f'held = cowork.held\n'
                          f'def waiting(lock, wait=False):\n'
                          f'    wait and pathlib.Path({str(polling)!r}).touch()\n'
                          f'    return held(lock, wait)\n'
                          f'cowork.held = waiting\nsys.exit(cowork.main(sys.argv[1:]))']
        proc = subprocess.Popen([sys.executable, *argv, 'read', '--wait', request], cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.background.append(proc)
        if os.name == 'nt':
            until(lambda: proc.poll() is not None or polling.exists())
        else:
            until(lambda: proc.poll() is not None or cowork.holds(proc.pid, self.box() / f'{request}.lock'))
        self.assertIsNone(proc.poll())
        return proc

    def leftovers(self, request):
        return sorted(p.suffix for p in self.box().glob(f'{request}.*'))

    # --- sessions -------------------------------------------------------------

    def test_first_send_starts_a_codex_thread_and_the_next_resumes_it(self):
        code, request, reply, _ = self.send('--task', 'do a thing')
        self.assertEqual(code, 0)
        self.assertTrue(request.startswith('codex-'))
        self.assertEqual(reply, 'codex reply\nFiles touched: none\n')
        self.assertEqual((self.box() / 'session').read_text(), 'thread-42\ngpt-6.1-sol\nhigh\ndefault\n')
        self.send('--task', 'another')
        first, second = self.calls()
        self.assertEqual(first['argv'][:6], ['exec', '-m', 'gpt-6.1-sol', '-c', 'model_reasoning_effort=high', '--json'])
        self.assertEqual(second['argv'][:3], ['exec', 'resume', 'thread-42'])
        self.assertIn('-o', second['argv'])

    def test_claude_gets_a_chosen_session_id_and_is_resumed_by_it(self):
        code, _, reply, _ = self.send('--to', 'claude', '--task', 'q', **CODEX)
        self.assertEqual(code, 0)
        self.assertEqual(reply, 'claude reply\nFiles touched: a.c\n')
        session = cowork.session_of(self.box('claude'))
        self.assertEqual(len(session), 36)
        self.send('--to', 'claude', '--task', 'q2', **CODEX)
        first, second = self.calls()
        self.assertEqual(first['argv'][-2:], ['--session-id', session])
        self.assertEqual(second['argv'][-2:], ['--resume', session])
        self.assertIn('stream-json', first['argv'])

    def test_the_coworker_defaults_to_the_other_cli_and_needs_naming_otherwise(self):
        code, _, _, _ = self.send('--task', 'q', CLAUDECODE='')
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), [])
        code, _, reply, _ = self.send('--task', 'q', **CODEX)
        self.assertEqual((code, reply), (0, 'claude reply\nFiles touched: a.c\n'), 'Codex drives Claude without --to')

    def test_a_failed_first_claude_turn_binds_no_session(self):
        code, _, _, _ = self.send('--to', 'claude', '--task', 'q', **CODEX, FAKE_EXIT='1')
        self.assertEqual(code, 1)
        self.assertIsNone(cowork.session_of(self.box('claude')))
        code, _, _, _ = self.send('--to', 'claude', '--task', 'q', **CODEX)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls()[1]['argv'][-2], '--session-id', 'a fresh id, not a resume of nothing')

    def test_reset_forgets_the_session_and_removes_undelivered_requests(self):
        self.send('--task', 'a')
        undelivered = self.reaped('--task', 'b')
        self.settled(undelivered)
        code, out, _ = self.run_cli('reset', 'codex', 'main')
        self.assertEqual(code, 0)
        self.assertIn('1 undelivered request(s) removed', out)
        self.assertEqual(sorted(p.name for p in self.box().iterdir()), ['lock'])
        self.gate.unlink()
        self.send('--task', 'c')
        self.assertNotIn('resume', self.calls()[-1]['argv'])

    def test_a_read_only_reset_refuses_any_other_lane_under_its_lock(self):
        # The coworker relay resets only read-only lanes; a lane remade as a worktree lane between
        # a status read and the reset must not lose its tree.
        self.commit('a.txt', 'a')
        self.send('--task', 'a')
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.send('--lane', 'review', '--task', 'q')
        for lane, what in (('main', 'main'), ('impl', 'worktree')):
            code, _, err = self.run_cli('reset', 'codex', lane, '--read-only')
            self.assertEqual(code, cowork.BUSY, lane)
            self.assertIn(f'codex/{lane} is {what}, not read-only', err)
            self.assertTrue((self.box(lane=lane) / 'session').exists(), lane)
        code, _, err = self.run_cli('reset', 'codex', 'all', '--read-only')
        self.assertEqual(code, cowork.FAILED)
        self.assertIn('not all', err)
        code, out, _ = self.run_cli('reset', 'codex', 'review', '--read-only')
        self.assertEqual((code, out.strip()), (0, 'codex/review: session forgotten'))
        code, out, _ = self.run_cli('reset', 'codex', 'review', '--read-only')  # a hint followed twice
        self.assertEqual((code, out.strip()), (0, 'codex/review: no session'))

    # --- model and effort ---------------------------------------------------------

    def test_a_new_lane_starts_at_its_sides_default_whatever_drives_it(self):
        self.send('--task', 'from claude')
        argv = self.calls()[0]['argv']
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6.1-sol')
        self.assertEqual(argv[argv.index('-c') + 1], 'model_reasoning_effort=high')
        self.assertIn('answered by gpt-6.1-sol at high effort', self.calls()[0]['stdin'])
        code, _, _, err = self.send('--effort', 'max', '--task', 'max')
        self.assertEqual(code, 2)
        self.assertIn('--model/--effort are for Claude lanes', err)
        self.send('--task', 'from codex', **CODEX)
        argv = self.calls()[1]['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'opus')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')

    def test_a_claude_lanes_model_and_effort_persist_until_replaced_or_reset(self):
        for argv in (('--model', 'sonnet', '--effort', 'medium', '--task', 'a'), ('--task', 'b'),
                     ('--effort', 'low', '--task', 'c'), ('--model', 'haiku', '--task', 'd'), ('--task', 'e')):
            self.assertEqual(self.send(*argv, **CODEX)[0], 0)
        self.run_cli('reset', 'claude', 'main')
        self.send('--task', 'f', **CODEX)
        seen = [(c['argv'][c['argv'].index('--model') + 1], c['argv'][c['argv'].index('--effort') + 1]) for c in self.calls()]
        self.assertEqual(seen, [('sonnet', 'medium'), ('sonnet', 'medium'), ('sonnet', 'low'), ('haiku', 'low'),
                                ('haiku', 'low'), ('opus', 'high')])

    def test_a_field_update_keeps_the_bound_thread(self):
        self.assertEqual(self.send('--task', 'one')[0], 0)
        self.assertEqual(cowork.session_of(self.box()), 'thread-42')
        cowork.update_side(self.box(), effort='low')
        self.assertEqual(cowork.lane_fields(self.box()), {'session': 'thread-42', 'model': 'gpt-6.1-sol', 'effort': 'low',
                                                          'tier': 'default'}, 'a field-wise update')
        self.send('--task', 'two')
        self.assertEqual(self.calls()[-1]['argv'][:3], ['exec', 'resume', 'thread-42'])

    def test_a_flag_writes_back_only_its_own_field(self):
        self.send('--model', 'haiku', '--effort', 'medium', '--task', 'a', **CODEX)
        box = self.box(side='claude')
        cowork.settle_pair(box, 'claude', None, 'high', None)
        self.assertEqual((cowork.lane_fields(box)['model'], cowork.lane_fields(box)['effort']), ('haiku', 'high'))
        self.assertFalse(list(box.glob('session.*.tmp')), 'the session file is replaced, never truncated')

    # --- tiers and the Codex default -------------------------------------------

    def pair(self, call):
        argv = call['argv']
        return argv[argv.index('-m') + 1], argv[argv.index('-c') + 1].removeprefix('model_reasoning_effort=')

    def test_a_flipped_default_reaches_a_default_lane_on_its_next_send(self):
        self.send('--task', 'a')
        self.assertEqual(self.run_cli('default', 'astra')[0], 0)
        _, out, _ = self.run_cli('status', '--all')
        self.assertIn('codex/main: session thread-42, gpt-6.1-sol at high effort, tier default', out)
        self.send('--task', 'b')
        self.assertEqual(self.pair(self.calls()[1]), ('gpt-6-astra', 'high'))
        self.assertEqual(self.calls()[1]['argv'][:3], ['exec', 'resume', 'thread-42'], 'the session carries over')

    def test_review_and_expert_tiers_stay_put_and_omission_keeps_the_saved_tier(self):
        self.send('--tier', 'review', '--task', 'a')
        self.run_cli('default', 'sol')
        self.send('--task', 'b')
        self.send('--tier', 'expert', '--task', 'c')
        self.send('--tier', 'default', '--task', 'd')
        self.assertEqual([self.pair(c) for c in self.calls()], [('gpt-6-astra', 'high'), ('gpt-6-astra', 'high'),
                                                                 ('gpt-6-astra', 'xhigh'), ('gpt-6.1-sol', 'high')])

    def test_a_pinned_codex_lane_is_refused_until_a_tier_is_named(self):
        self.box().mkdir(parents=True)
        (self.box() / 'session').write_text('thread-42\ngpt-6-astra\nhigh\n')
        _, out, _ = self.run_cli('status', '--all')
        self.assertIn('codex/main: session thread-42, gpt-6-astra at high effort, pinned', out)
        code, _, _, err = self.send('--task', 'a')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('codex/main is pinned to gpt-6-astra at high effort; name its tier with --tier default|review|expert', err)
        self.assertEqual(self.calls(), [])
        self.send('--tier', 'default', '--task', 'b')
        self.assertEqual([self.pair(c) for c in self.calls()], [('gpt-6.1-sol', 'high')])
        self.assertEqual(self.calls()[0]['argv'][:3], ['exec', 'resume', 'thread-42'], 'the session carries over')

    def test_a_tier_with_a_pin_or_on_a_claude_lane_is_refused(self):
        code, _, _, err = self.send('--tier', 'review', '--model', 'gpt-6-astra', '--task', 'q')
        self.assertEqual(code, 2)
        self.assertIn('--model/--effort are for Claude lanes', err)
        code, _, _, err = self.send('--tier', 'review', '--task', 'q', **CODEX)
        self.assertEqual(code, 2)
        self.assertIn('--tier is for Codex lanes', err)
        self.assertEqual(self.calls(), [], 'nothing reached a coworker')

    def test_default_shows_and_sets_the_preset_and_a_bad_file_is_refused(self):
        code, out, _ = self.run_cli('default')
        self.assertEqual((code, out), (0, 'codex default: sol, gpt-6.1-sol at high effort (built in)\n'))
        path = Path(os.environ['XDG_CONFIG_HOME']) / 'agentrc' / 'cowork-default'
        code, out, _ = self.run_cli('default', 'astra')
        self.assertEqual((code, out, path.read_text()), (0, f'codex default: astra, gpt-6-astra at high effort ({path})\n', 'astra\n'))
        self.assertEqual(self.run_cli('default', 'bogus')[0], 2)
        path.write_text('fast\n')
        code, _, _, err = self.send('--task', 'q')
        self.assertEqual(code, 1)
        self.assertIn("expected one of astra, sol, got 'fast'", err)
        self.assertEqual(self.calls(), [], 'nothing reached the coworker')

    def test_a_bad_effort_is_refused_not_guessed(self):
        code, _, _, err = self.send('--effort', 'bogus', '--task', 'q')
        self.assertEqual(code, 2)
        self.assertIn('invalid choice', err)
        self.assertEqual(self.calls(), [], 'nothing reached the coworker')

    # --- the prompt and the coworker's environment ------------------------------

    def test_bootstrap_only_on_the_first_turn_and_a_header_every_turn(self):
        self.send('--task', 'first task')
        self.send('--no-edit', '--task', 'second task')
        first, second = self.calls()
        self.assertIn('coworker on the cowork channel', first['stdin'])
        self.assertNotIn('coworker on the cowork channel', second['stdin'])
        for rule in ('not operator instructions', 'never push, open a PR, or post a comment or an issue',
                     '`git add -- <paths>` then `git commit --only -m "<subject>" -- <same paths>`',
                     'Do not load the `cowork` skill'):
            self.assertIn(rule, ' '.join(first['stdin'].split()), 'the receiver gets its rules inline')
        for call in (first, second):
            self.assertRegex(call['stdin'], r'cowork request codex-\S+ from claude')
            self.assertIn('Files touched', call['stdin'])
            self.assertEqual(call['env']['COWORK_TURN'], call['stdin'].split('cowork request ')[1].split()[0],
                             'COWORK_TURN names the request so the gate stays out')
        self.assertIn('---\nfirst task\n---\n', first['stdin'])
        # last, so a task asking for its output only does not drop the footer
        self.assertTrue(first['stdin'].endswith('"Files touched: none", even when the task asks for nothing else.\n'))
        self.assertTrue(second['stdin'].endswith('\n---\nIf you changed any file, end your reply with a line '
                                                 '"Files touched: <paths>".\n'), 'a read-only turn owes no "none"')
        self.assertIn('Scope: edit and commit', first['stdin'])
        self.assertIn('Scope: do not edit anything', second['stdin'])

    def test_no_edit_is_plan_mode_for_claude_and_checked_afterwards_for_codex(self):
        self.send('--to', 'claude', '--no-edit', '--task', 'review', **CODEX)
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

    def test_a_tree_it_cannot_snapshot_before_the_turn_runs_nothing(self):
        code, _, _, _ = self.send('--no-edit', '--task', 'review')
        self.assertEqual(code, 0, 'an unborn HEAD and no index yet are a snapshot, not a failure')
        self.commit('a.txt', 'a')
        (self.root / '.git' / 'index').write_bytes(b'garbage')
        ran = len(self.calls())
        code, request, reply, _ = self.send('--no-edit', '--task', 'review')
        self.assertEqual(code, cowork.FAILED, 'the turn never ran')
        self.assertIn('could not verify the tree', reply)
        self.assertIn('index', reply.split('could not verify the tree', 1)[1], "git's own diagnostic is shown")
        self.assertEqual(len(self.calls()), ran, 'the coworker never ran')
        self.assertEqual(self.leftovers(request), [])

    def test_a_broken_head_is_not_taken_for_an_unborn_one(self):
        self.commit('a.txt', 'a')
        (self.root / '.git' / 'refs' / 'heads' / 'main').write_text('garbage\n')
        ran = len(self.calls())
        code, _, reply, _ = self.send('--no-edit', '--task', 'review')
        self.assertEqual(code, cowork.FAILED)
        self.assertIn('could not verify the tree', reply)
        self.assertEqual(len(self.calls()), ran)

    def test_a_detached_head_is_a_snapshot_and_a_branch_switch_is_an_edit(self):
        self.commit('a.txt', 'a')
        sh(self.root, 'git', 'checkout', '-q', '--detach')
        self.assertEqual(self.send('--no-edit', '--task', 'review')[0], 0)
        sh(self.root, 'git', 'checkout', '-q', 'main')
        self.codex_does("import subprocess; subprocess.run(['git', 'checkout', '-q', '-b', 'elsewhere'])")
        code, _, _, err = self.send('--no-edit', '--task', 'review')
        self.assertEqual(code, cowork.MALFORMED, 'same commit, same index, another branch')
        self.assertIn('tree changed during a --no-edit turn', err)

    def test_a_tree_it_cannot_snapshot_after_the_turn_keeps_the_reply(self):
        self.commit('a.txt', 'a')
        self.codex_does("open('.git/index', 'wb').write(b'garbage')")
        code, request, reply, err = self.send('--no-edit', '--task', 'review')
        self.assertEqual(code, cowork.MALFORMED)
        self.assertIn('could not verify the tree', err)
        self.assertIn('index', err.split('could not verify the tree', 1)[1])
        self.assertEqual(reply, 'codex reply\nFiles touched: none\n')
        self.assertEqual(self.leftovers(request), [])

    def test_an_index_it_cannot_copy_is_a_snapshot_failure(self):
        self.commit('a.txt', 'a')
        with mock.patch.object(cowork.shutil, 'copy', side_effect=PermissionError('denied')):
            with self.assertRaises(cowork.SnapshotError):
                cowork.tree_state(self.root)

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
        code, _, _, _ = self.send('--task', '-', stdin='from stdin')
        self.assertEqual(code, 0)
        self.assertEqual(task_of(self.calls()[0]['stdin']), 'from stdin')
        code, _, _, _ = self.send('--task', 'task.md')
        self.assertEqual(code, 0, 'a literal is a literal, even one that names a file')
        self.assertEqual(task_of(self.calls()[1]['stdin']), 'task.md')

    def test_a_non_ascii_task_and_reply_cross_the_real_stdio_as_utf8(self):
        task, reply = 'check ✔ • café — 中', 'done ✔ — café\nFiles touched: none\n'
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
        env['PYTHONUTF8'] = '0' if os.name == 'nt' else '1'  # cowork sets UTF-8 stdio on Windows; elsewhere the locale's
        done = subprocess.run([sys.executable, str(SCRIPT), 'send', '--task', '-'], cwd=self.root, input=task.encode(),
                              capture_output=True, timeout=30, env={**env, 'FAKE_REPLY': reply})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.decode().replace('\r\n', '\n').partition('\n')[2], reply)
        self.assertEqual(task_of(self.calls()[0]['stdin']), task)

    def test_an_empty_task_never_reaches_the_coworker(self):
        for argv in (('--task', ''), ('--task', '-')):
            code, _, _, err = self.send(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn('resolved to nothing', err)
        self.assertEqual(self.calls(), [])

    # --- outcomes -----------------------------------------------------------------

    def test_codex_usage_is_the_sessions_running_total_labelled_as_such(self):
        total = {'input_tokens': 77537, 'cached_input_tokens': 51328, 'output_tokens': 91, 'reasoning_output_tokens': 0}
        code, request, _, err = self.send('--task', 'x', FAKE_USAGE=json.dumps(total))
        self.assertEqual(code, 0)
        self.assertEqual(err, receipt(request, used='session input 77,537 (cached 51,328), output 91'))
        self.assertNotIn('context', err, 'the codex stream has no per-call figure')

    def rollout(self):
        return next(Path(os.environ['CODEX_HOME']).glob('sessions/*/*/*/rollout-*-thread-42.jsonl'))

    def turn_usage(self, n):
        return f'input {2500 * n:,} (cached {1000 * n:,}), output 20; input context {1500 * n:,} (last call)'

    def test_codex_usage_is_the_turns_own_from_its_rollout(self):
        total = json.dumps({'input_tokens': 9, 'cached_input_tokens': 0, 'output_tokens': 1})
        for n in (1, 2):  # the second turn reports its own figures, not the session's
            code, request, _, err = self.send('--task', 'x', FAKE_ROLLOUT='1', FAKE_USAGE=total)
            self.assertEqual((code, err), (0, receipt(request, used=self.turn_usage(n))))

    def detached_and_settled(self, *argv, **env):
        request = self.send('--detach', *argv, **env)[1]
        until(lambda: not cowork.held(self.box() / f'{request}.lock'))
        return request

    def test_codex_usage_counts_only_a_header_match_through_compaction(self):
        first = self.detached_and_settled('--task', 'x', FAKE_ROLLOUT='1')
        header = f'cowork request {first} from claude on lane main'
        self.detached_and_settled('--task', f'what did "{header}" ask?', FAKE_ROLLOUT='1')
        with self.rollout().open('a') as rollout:
            rollout.write(json.dumps({'type': 'compacted', 'payload': {'replacement_history': [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': header + '\n---\nx'}]}]}}) + '\n')
        code, _, err = self.run_cli('read', first)
        self.assertEqual((code, err), (0, receipt(first, used=self.turn_usage(1))))

    def test_codex_usage_falls_back_to_the_session_unless_the_turn_is_certain(self):
        total = json.dumps({'input_tokens': 9, 'cached_input_tokens': 0, 'output_tokens': 1})
        fallback = 'session input 9 (cached 0), output 1'
        def rewrite(old, new, count=1):
            text = self.rollout().read_text()
            self.assertIn(old, text)
            self.rollout().write_text(text.replace(old, new, count))
        def quoted(request):  # the id in a message without an envelope
            events = [json.loads(line) for line in self.rollout().read_text().splitlines()]
            for event in events:
                if event['payload'].get('role') == 'user':
                    event['payload']['content'][0]['text'] = f'explain cowork request {request} from claude'
            self.rollout().write_text(''.join(json.dumps(event) + '\n' for event in events))
        for spoil in (None,  # no rollout at all
                      lambda _: rewrite('"type": "session_meta", "payload": {"id": "thread-42"}', '"type": "session_meta", "payload": {"id": "other"}'),
                      lambda _: rewrite('"internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"}',
                                        '"internal_chat_message_metadata_passthrough": {"turn_id": "turn-9"}'),
                      lambda _: rewrite('"thread_id": "thread-42", "turn_id": "turn-1"', '"thread_id": "thread-7", "turn_id": "turn-1"', 2),
                      lambda _: rewrite('"usage": {"input_tokens": 1500,', '"usage": {"input_tokens": true,'),
                      lambda _: self.rollout().write_text(self.rollout().read_text() * 2),  # the header twice
                      quoted,
                      'incomplete'):
            with self.subTest(spoil):
                with contextlib.suppress(StopIteration):
                    self.rollout().unlink()
                rollout = 'incomplete' if spoil == 'incomplete' else ('' if spoil is None else '1')
                request = self.detached_and_settled('--task', 'x', FAKE_ROLLOUT=rollout, FAKE_USAGE=total)
                if callable(spoil):
                    spoil(request)
                code, _, err = self.run_cli('read', request)
                self.assertEqual((code, err), (0, receipt(request, used=fallback)))

    def test_missing_or_failed_usage_is_unavailable_and_changes_nothing_else(self):
        for env, want in (({}, 0), ({'FAKE_USAGE': '"garbage"'}, 0), ({'FAKE_USAGE': '{}'}, 0),
                          ({'FAKE_USAGE': '{"input_tokens": 5, "output_tokens": 1}'}, 0),
                          ({'FAKE_USAGE': '{"input_tokens": "5", "cached_input_tokens": 0, "output_tokens": 1}'}, 0),
                          ({'FAKE_USAGE': '{"input_tokens": true, "cached_input_tokens": 0, "output_tokens": 1}'}, 0),
                          ({'FAKE_USAGE': '{"input_tokens": -3, "cached_input_tokens": 0, "output_tokens": 1}'}, 0),
                          ({'FAKE_EXIT': '2'}, cowork.FAILED),
                          ({'FAKE_REPLY': 'no footer\n'}, cowork.MALFORMED)):
            with self.subTest(env):
                code, request, _, err = self.send('--task', 'x', **env)
                self.assertEqual(code, want)
                self.assertTrue(err.endswith(f'exit {want}; usage unavailable\n'), err)
                self.assertNotIn('input 0', err)
                self.assertEqual(self.leftovers(request), [])

    def test_claude_usage_is_per_request_with_the_last_calls_context(self):
        call = {'input_tokens': 8, 'cache_read_input_tokens': 27935, 'cache_creation_input_tokens': 182, 'output_tokens': 2}
        result = {'type': 'result', 'result': 'ok\nFiles touched: none',
                  'usage': {'input_tokens': 18, 'cache_read_input_tokens': 42079, 'cache_creation_input_tokens': 13973,
                            'output_tokens': 158}}
        code, request, _, err = self.send('--to', 'claude', '--task', 'q', **CODEX, FAKE_RESULT=json.dumps(result),
                                          FAKE_ASSISTANT=json.dumps({'type': 'assistant', 'message': {'usage': call}}))
        self.assertEqual(code, 0)
        self.assertEqual(err, receipt(request, lane='claude/main',
                                      used='input 56,070 (cached 42,079), output 158; context 28,125 (last call)'))
        for bad in (None, {}, {'output_tokens': 3}, {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 'x'},
                    {'input_tokens': True, 'output_tokens': 1}, {'input_tokens': 1, 'output_tokens': -1}):
            with self.subTest(bad):
                code, request, _, err = self.send('--to', 'claude', '--task', 'q', **CODEX,
                                                  FAKE_RESULT=json.dumps({**result, 'usage': bad}))
                self.assertEqual(code, 0)
                self.assertEqual(err, receipt(request, lane='claude/main'))

    def test_a_usage_report_that_raises_still_delivers_the_reply(self):
        with mock.patch.object(cowork, 'usage', side_effect=PermissionError('denied')):
            code, request, reply, err = self.send('--task', 'x')
        self.assertEqual((code, reply, err), (0, 'codex reply\nFiles touched: none\n', receipt(request)))
        self.assertEqual(self.leftovers(request), [])

    def test_a_failing_codex_turn_reports_its_jsonl_error_and_stderr(self):
        code, request, reply, _ = self.send('--task', 'x', FAKE_EXIT='2', FAKE_STDERR='bad \xff bytes')
        self.assertEqual(code, 1)
        self.assertIn('exited 2', reply)
        self.assertIn('usage limit reached', reply, 'the cause lives only in the event stream')
        self.assertIn('bad � bytes', reply, 'undecodable stderr does not crash the report')
        self.assertEqual(self.leftovers(request), [], 'delivered, so nothing stays')

    def test_codex_exiting_clean_without_a_reply_is_a_failure(self):
        self.fake('codex', FAKE_CODEX.replace("open(args[args.index('-o') + 1], 'w'", "open(os.devnull, 'w'"))
        code, _, reply, _ = self.send('--task', 'x')
        self.assertEqual(code, 1)
        self.assertIn('without writing its last message', reply)
        self.assertTrue((self.box() / 'session').exists(), 'the thread exists and resumes')

    def test_a_claude_error_result_is_a_failure_not_an_empty_reply(self):
        error = json.dumps({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True, 'errors': ['turn limit']})
        code, _, reply, _ = self.send('--to', 'claude', '--task', 'q', **CODEX, FAKE_RESULT=error)
        self.assertEqual(code, 1)
        self.assertIn('turn limit', reply)
        code, _, reply, _ = self.send('--to', 'claude', '--task', 'q', **CODEX, FAKE_RESULT='not json at all')
        self.assertEqual(code, 1)
        self.assertIn('without a usable result', reply)

    def test_a_reply_without_the_files_line_is_flagged_before_its_receipt(self):
        code, request, reply, err = self.send('--task', 'x', FAKE_REPLY='did it\n')
        self.assertEqual((code, reply), (cowork.MALFORMED, 'did it\n'), 'the reply is still shown')
        diagnostic, last = err.splitlines(keepends=True)
        self.assertIn('no "Files touched" line', diagnostic)
        self.assertEqual(last, receipt(request, 'no-footer', cowork.MALFORMED))

    def test_the_receipt_names_a_changed_or_unverified_tree(self):
        self.codex_does("open('new.c', 'w').write('x')")
        code, request, _, err = self.send('--no-edit', '--task', 'x')
        self.assertTrue(err.endswith(receipt(request, 'tree-changed', cowork.MALFORMED)), err)
        self.assertIn('the tree changed', err)
        (self.root / 'new.c').unlink()
        self.codex_does("import shutil, stat; shutil.rmtree('.git/objects', onerror=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))")
        code, request, _, err = self.send('--no-edit', '--task', 'x')
        self.assertEqual(code, cowork.MALFORMED)
        self.assertTrue(err.endswith(receipt(request, 'unverified', cowork.MALFORMED)), err)
        self.assertIn('could not verify the tree', err, "git's diagnostic is kept apart from the receipt")

    def test_the_receipt_comes_after_the_reply_on_a_merged_stream(self):
        request = self.reaped('--task', 'x')
        self.settled(request)
        done = subprocess.run([sys.executable, str(SCRIPT), 'read', request], cwd=self.root,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
        self.assertEqual(done.stdout, 'codex reply\nFiles touched: none\n' + receipt(request))

    def test_a_delivery_whose_output_fails_leaves_the_request_to_read_again(self):
        class Broken(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, 'Broken pipe')
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream):
                request = self.reaped('--task', 'x')
                self.settled(request)
                with mock.patch(f'sys.{stream}', Broken()), self.assertRaises(BrokenPipeError):
                    cowork.main(['read', request])
                self.assertIn('.reply', self.leftovers(request))
                code, out, err = self.run_cli('read', request)
                self.assertEqual((code, out, err), (0, 'codex reply\nFiles touched: none\n', receipt(request)))
                self.gate.unlink()

    def test_the_scope_sets_the_trailer_and_the_footer_rule(self):
        self.commit('a.txt', 'a')
        bare = {**CODEX, 'FAKE_RESULT': json.dumps({'type': 'result', 'result': 'done'})}
        for argv, env, outcome, advisory in (
                (('--no-edit',), {'FAKE_REPLY': 'looks fine\n'}, 'replied', ''),
                (('--lane', 'plan'), {'FAKE_REPLY': 'looks fine\n'}, 'replied', ''),
                (('--lane', 'impl', '--worktree', '--no-edit'), {'FAKE_REPLY': 'done\n'}, 'replied', ''),
                (('--lane', 'impl'), {'FAKE_REPLY': 'done\n'}, 'no-footer', ''),
                (('--no-edit',), {'FAKE_REPLY': 'x\nFiles touched: none\n'}, 'replied', ''),
                (('--no-edit',), {'FAKE_REPLY': 'x\nFiles touched: /tmp/notes.md\n'}, 'replied', 'paths reported: /tmp/notes.md; '),
                ((), {'FAKE_REPLY': 'x\nFiles touched: a.c\n'}, 'replied', 'paths reported: a.c; '),
                (('--to', 'claude', '--no-edit'), bare, 'replied', ''),
                (('--to', 'claude'), bare, 'no-footer', ''),
                (('--no-edit',), {'FAKE_REPLY': '\n'}, 'empty-reply', ''),
                ((), {'FAKE_REPLY': '\n'}, 'empty-reply', '')):
            with self.subTest(argv=argv, env=env):
                code, request, _, err = self.send(*argv, '--task', 'x', **env)
                where = ('claude' if 'claude' in argv else 'codex') + '/' + (argv[1] if argv[:1] == ('--lane',) else 'main')
                self.assertEqual(code, cowork.OUTCOMES[outcome])
                self.assertTrue(err.endswith(receipt(request, outcome, code, advisory + 'usage unavailable', where)), err)
                read_only = '--no-edit' in argv or 'plan' in argv
                self.assertEqual(self.calls()[-1]['stdin'].endswith(cowork.TRAILER[True]), read_only)
                self.assertEqual(self.leftovers(request), [])

    def test_an_outcome_from_a_runner_on_older_code_keeps_its_writer_rule(self):
        box = self.box()
        box.mkdir(parents=True)
        for status, reply, outcome in (('0', 'done\nFiles touched: none\n', 'replied'), ('0', 'done\n', 'no-footer'),
                                       ('0', '', 'empty-reply'), ('edited', 'x\n', 'tree-changed'),
                                       ('error', None, 'failed'), ('2', None, 'failed')):
            with self.subTest(status=status, reply=reply):
                (box / 'codex-x.exit').write_text(status + '\n')
                if reply is not None:
                    (box / 'codex-x.reply').write_bytes(reply.encode())
                (box / 'codex-x.lock').touch()
                code, out, err = self.run_cli('read', 'codex-x')
                self.assertTrue(err.endswith(receipt('codex-x', outcome, cowork.OUTCOMES[outcome])), err)
                if status == '2':
                    self.assertIn('codex-x exited 2', out)

    def test_a_missing_cli_is_reported_not_raised(self):
        (self.bin / ('codex.exe' if os.name == 'nt' else 'codex')).unlink()
        code, _, reply, _ = self.send('--task', 'x', PATH=f'{self.bin}{os.pathsep}{os.path.dirname(shutil.which("git"))}')
        self.assertEqual(code, 1)
        self.assertIn('failed', reply)
        self.assertIn('codex is not on PATH as codex.exe, codex.cmd, codex.bat' if os.name == 'nt' else 'No such file',
                      reply)

    @unittest.skipIf(os.name != 'nt', 'CreateProcess and shutil.which search the current directory on Windows only')
    def test_the_cli_is_found_on_path_alone_never_in_the_checkout(self):
        (self.root / 'codex.cmd').write_text('@echo hijacked> hijacked\r\n', newline='')  # a contributor's checkout
        env = {k: v for k, v in os.environ.items() if k.lower() != 'nodefaultcurrentdirectoryinexepath'}
        with mock.patch.dict('os.environ', env, clear=True):
            code, _, reply, _ = self.send('--task', 'x')
        self.assertEqual((code, reply), (0, 'codex reply\nFiles touched: none\n'))
        self.assertFalse((self.root / 'hijacked').exists())
        self.assertEqual(len(self.calls()), 1)
        first, second = Path(self.tmp.name) / 'first', Path(self.tmp.name) / 'second'
        for where, names in ((first, ('codex.cmd',)), (second, ('codex.exe', 'codex.cmd'))):
            where.mkdir()
            for name in names:
                (where / name).touch()
        for path, found in (((first, second), first / 'codex.cmd'), ((second, first), second / 'codex.exe')):
            with self.subTest(path=path), mock.patch.dict('os.environ', {'PATH': os.pathsep.join(map(str, path))}):
                self.assertEqual(cowork.nt_cli(['codex', 'exec']), [str(found), 'exec'], 'PATH order, then .exe first')

    def test_a_cli_only_a_relative_path_entry_reaches_is_not_found(self):
        (self.root / 'codex.exe').touch()  # the checkout, the working directory
        for path, cwd in (('.', self.root), (os.pathsep.join(('', self.root.name)), self.root.parent)):
            with self.subTest(path=path), mock.patch.dict('os.environ', {'PATH': path}), contextlib.chdir(cwd), \
                    self.assertRaisesRegex(FileNotFoundError, 'codex is not on PATH as codex.exe, codex.cmd, codex.bat'):
                cowork.nt_cli(['codex', 'exec'])

    @unittest.skipIf(os.name != 'nt', 'CreateProcess searches the current directory on Windows only')
    def test_git_is_found_on_path_alone_never_in_the_checkout(self):
        decoy = Path(self.tmp.name) / 'decoy'
        decoy.write_text(f'open({str(Path(self.tmp.name) / "hijacked")!r}, "w").close()\nraise SystemExit(1)\n',
                         encoding='utf-8')
        launcher(self.root, 'git', decoy)  # a contributor's checkout
        env = {k: v for k, v in os.environ.items() if k.lower() != 'nodefaultcurrentdirectoryinexepath'}
        cowork.git_program.cache_clear()
        try:
            with mock.patch.dict('os.environ', env, clear=True):
                code, _, reply, err = self.send('--no-edit', '--task', 'x')  # git_dir, the runner's and tree_state
        finally:
            cowork.git_program.cache_clear()
        self.assertEqual((code, reply), (0, 'codex reply\nFiles touched: none\n'), err)
        self.assertFalse((Path(self.tmp.name) / 'hijacked').exists())

    @unittest.skipIf(os.name != 'nt', 'taskkill is Windows')
    def test_taskkill_is_system32s_never_the_checkouts(self):
        hijacked = Path(self.tmp.name) / 'hijacked'
        decoy = Path(self.tmp.name) / 'decoy'
        decoy.write_text(f'open({str(hijacked)!r}, "w").close()\n', encoding='utf-8')
        launcher(self.root, 'taskkill', decoy)  # in the working directory, as the checkout is for `kill`
        env = {k: v for k, v in os.environ.items() if k.lower() != 'nodefaultcurrentdirectoryinexepath'}
        victim = subprocess.Popen(SLEEP)
        try:
            with mock.patch.dict('os.environ', env, clear=True):
                cowork.kill_tree(victim.pid)
            self.assertEqual(victim.wait(timeout=5), KILLED)
            self.assertFalse(hijacked.exists())
            for root, failure in (('', None), ('Windows', None), (os.environ['SystemRoot'], subprocess.TimeoutExpired('taskkill', 30)),
                                  (os.environ['SystemRoot'], FileNotFoundError(2, 'gone'))):
                with self.subTest(root=root, failure=failure), mock.patch.dict('os.environ', {'SystemRoot': root}), \
                        mock.patch.object(cowork.subprocess, 'run', side_effect=failure) as ran, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    cowork.kill_tree(victim.pid)  # reported, never raised
                self.assertIn(f'cowork: taskkill of {victim.pid} failed: ', err.getvalue())
                self.assertEqual(ran.called, failure is not None, 'a relative SystemRoot runs nothing')
        finally:
            if victim.poll() is None:
                victim.kill()
            victim.wait()

    @unittest.skipIf(os.name != 'nt', 'the stand-ins are launchers on Windows only')
    def test_a_launcher_runs_a_stand_in_under_a_non_ascii_path(self):
        where = Path(self.tmp.name) / 'stubs ✔'
        where.mkdir()
        (where / 'probe').write_text("print('ran')\n", encoding='utf-8')
        launcher(self.bin, 'probe', where / 'probe')
        self.assertEqual(subprocess.run([str(self.bin / 'probe.exe')], capture_output=True, text=True).stdout, 'ran\n')

    @unittest.skipIf(os.name != 'nt', 'job objects are Windows')
    def test_what_the_cli_spawns_first_thing_is_in_its_job(self):
        early = Path(self.tmp.name) / 'early.pid'
        self.codex_does(f'import subprocess; open({str(early)!r}, "w").write(str(subprocess.Popen({SLEEP!r}).pid))')
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        (box / 'codex-x.jsonl').touch()
        assign, nt_job, jobs, ran_first = cowork.kernel32.AssignProcessToJobObject, cowork.nt_job, [], []

        def late_assign(job, process):  # give a CLI that is not suspended the time to spawn
            deadline = time.monotonic() + 2
            while not early.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            ran_first.append(early.exists())
            return assign(job, process)

        def keep(child):
            jobs.append(nt_job(child))
            return jobs[-1]
        with holding(box / 'codex-x.lock') as lock, \
                mock.patch.object(cowork.kernel32, 'AssignProcessToJobObject', late_assign), \
                mock.patch.object(cowork, 'nt_job', keep), mock.patch.dict('os.environ', {'FAKE_GATE': str(self.gate)}), \
                contextlib.redirect_stderr(io.StringIO()):
            cowork.write_side(box, model='gpt-6-astra', effort='medium')
            runner = threading.Thread(target=cowork.run, args=('codex', box, self.root, 'codex-x', False, lock.fileno()))
            runner.start()
            try:
                until(lambda: early.exists() and early.read_text())
                pid = int(early.read_text())
                until(lambda: ran_first)
                self.assertEqual(ran_first, [False], 'the CLI ran nothing before it joined')
                cowork.kernel32.TerminateJobObject(jobs[0], 1)
                until(lambda: gone(pid), timeout=5)
            finally:
                self.gate.touch()  # a CLI the job did not take finishes
                runner.join(timeout=10)
                if early.exists() and early.read_text() and not gone(int(early.read_text())):
                    subprocess.run(['taskkill', '/F', '/PID', early.read_text()], capture_output=True)
        cowork.kernel32.CloseHandle(jobs[0])

    @unittest.skipIf(os.name != 'nt', 'cmd.exe runs batch files on Windows only')
    def test_a_batch_file_cli_runs_but_never_with_an_argument_cmd_would_reparse(self):
        (self.bin / 'claude.exe').unlink()  # an npm install: claude.cmd only
        (self.bin / 'claude.cmd').write_text(f'@"{sys.executable}" "{self.stubs / "claude"}" %*\r\n', newline='')
        code, _, reply, _ = self.send('--to', 'claude', '--task', 'q')
        self.assertEqual((code, reply), (0, 'claude reply\nFiles touched: a.c\n'))
        code, _, reply, _ = self.send('--to', 'claude', '--model', 'x&echo.>injected', '--task', 'q')
        self.assertEqual(code, 1)
        self.assertIn('is a batch file, and cmd.exe would reinterpret', reply)
        self.assertFalse((self.root / 'injected').exists())
        self.assertEqual(len(self.calls()), 1, 'refused before it ran')

    def test_a_refused_breakaway_is_noted_in_the_requests_err_and_any_other_denial_raised(self):
        denied = PermissionError(5, 'Access is denied')
        with contextlib.ExitStack() as stack:
            for flag in ('CREATE_NEW_PROCESS_GROUP', 'CREATE_BREAKAWAY_FROM_JOB'):  # absent outside Windows
                stack.enter_context(mock.patch.object(cowork.subprocess, flag, 0, create=True))
            for retried, noted in (('runner', b'refused breakaway'), (denied, b'')):  # a program or cwd denied
                err = Path(self.tmp.name) / f'{bool(noted)}.err'
                with self.subTest(retried=retried), err.open('ab') as stream, \
                        mock.patch.object(cowork, 'nt_popen', side_effect=[denied, retried]):
                    if noted:
                        self.assertEqual(cowork.nt_detach(['x'], '.', 0, stream), 'runner')
                    else:
                        self.assertRaises(PermissionError, cowork.nt_detach, ['x'], '.', 0, stream)
                if noted:
                    self.assertIn(noted, err.read_bytes())
                else:
                    self.assertEqual(err.read_bytes(), b'')

    @unittest.skipIf(os.name != 'nt', 'job objects are Windows')
    def test_a_tool_may_ask_to_break_away_from_the_clis_job(self):
        # the CLI itself, not a launcher: the fakes' launcher lets them break away from its own job
        gate = Path(self.tmp.name) / 'joined'
        away = (f"import os, subprocess, sys, time\nwhile not os.path.exists({str(gate)!r}): time.sleep(0.02)\n"
                "try:\n    subprocess.run([sys.executable, '-c', ''], creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB, "
                "check=True); print('left')\nexcept OSError as e:\n    print(repr(e))")
        cli = subprocess.Popen([sys.executable, '-c', away], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            job = cowork.nt_job(cli)
        gate.touch()
        out, _ = cli.communicate(timeout=10)
        cowork.kernel32.CloseHandle(job)
        self.assertEqual((out.strip(), err.getvalue()), ('left', ''))

    @unittest.skipIf(os.name != 'nt', 'the job object is the Windows backend')
    def test_kill_takes_a_daemon_whose_parent_already_exited(self):
        daemon = ("import subprocess, sys; open('daemon.pid', 'w').write(str(subprocess.Popen("
                  f"{SLEEP!r}, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP).pid))")
        self.codex_does(f'import subprocess; subprocess.run([sys.executable, "-c", {daemon!r}])')
        proc, request = self.send_gated('--task', 'x')
        until(lambda: (self.root / 'daemon.pid').exists() and (self.root / 'daemon.pid').read_text())
        pid = int((self.root / 'daemon.pid').read_text())
        try:
            self.run_cli('kill', request)
            proc.wait(timeout=10)
            until(lambda: gone(pid), timeout=5)  # taskkill finds no parent link to it: only the job holds it
        finally:
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)

    @unittest.skipIf(os.name != 'nt', 'msvcrt locks are Windows')
    def test_admission_retries_only_a_lock_held_by_another(self):
        box = self.box()
        box.mkdir(parents=True)
        for failure, raised in ((OSError(errno.EACCES, 'held'), False), (OSError(errno.EBADF, 'bad'), True)):
            with self.subTest(failure=failure), mock.patch.object(cowork.msvcrt, 'locking',
                                                                  side_effect=[failure, None, None]):
                with contextlib.ExitStack() as stack:
                    if raised:
                        stack.enter_context(self.assertRaises(OSError))
                    stack.enter_context(cowork.admission(box))

    @unittest.skipIf(os.name != 'nt', 'an open file blocks its removal on Windows only')
    def test_a_request_file_still_open_elsewhere_neither_loses_the_reply_nor_raises(self):
        box = self.box()
        box.mkdir(parents=True)
        for let_go in (True, False):
            with self.subTest(let_go=let_go):
                for ext, body in (('exit', b'0\n'), ('reply', b'done\nFiles touched: none\n'), ('lock', b''),
                                  ('err', b''), ('jsonl', b'')):
                    (box / f'codex-x.{ext}').write_bytes(body)
                holder = (box / 'codex-x.jsonl').open()  # what a grandchild inherited as its stdout
                try:
                    with letting_go(holder if let_go else None) as met:
                        code, out, err = self.run_cli('read', 'codex-x')
                    left = self.leftovers('codex-x')
                finally:
                    holder.close()
                self.assertEqual((code, out), (0, 'done\nFiles touched: none\n'))
                self.assertNotIn('Traceback', err)
                self.assertTrue(met, 'a removal met the open file')
                if let_go:
                    self.assertEqual((left, err), ([], receipt('codex-x')))
                else:
                    self.assertEqual(left, ['.jsonl'], 'all else is gone, the lock with it')
                    self.assertIn('delivered, but', err)
                    self.assertTrue(err.endswith(receipt('codex-x')), 'the receipt stays last')
                    (box / 'codex-x.jsonl').unlink()

    @unittest.skipIf(os.name != 'nt', 'an open file blocks its removal on Windows only')
    def test_a_lock_still_open_elsewhere_keeps_the_request_whole_and_readable(self):
        box = self.box()
        box.mkdir(parents=True)
        for ext, body in (('exit', b'0\n'), ('reply', b'done\nFiles touched: none\n'), ('lock', b''), ('err', b''),
                          ('jsonl', b'')):
            (box / f'codex-x.{ext}').write_bytes(body)
        with (box / 'codex-x.lock').open():  # a reader, say a virus scanner: not a holder of the request
            code, out, err = self.run_cli('read', 'codex-x')
            self.assertEqual((code, out), (0, 'done\nFiles touched: none\n'))
            self.assertIn('the request is kept whole', err)
            self.assertEqual(self.leftovers('codex-x'), ['.err', '.exit', '.jsonl', '.lock', '.reply'])
        code, out, err = self.run_cli('read', 'codex-x')
        self.assertEqual((code, out, err), (0, 'done\nFiles touched: none\n', receipt('codex-x')), 'not "died"')
        self.assertEqual(self.leftovers('codex-x'), [])

    def test_a_note_on_what_a_delivery_left_comes_before_its_receipt(self):
        box = self.box()
        box.mkdir(parents=True)
        for ext, body in (('exit', b'0\n'), ('reply', b'done\nFiles touched: none\n'), ('lock', b'')):
            (box / f'codex-x.{ext}').write_bytes(body)
        stuck = PermissionError(13, 'in use', str(box / 'codex-x.jsonl'))
        with mock.patch.object(cowork, 'fcntl', None), mock.patch.object(cowork, 'held', return_value=False), \
                mock.patch.object(cowork, 'admission', lambda box: contextlib.nullcontext()), \
                mock.patch.object(cowork, 'remove', side_effect=stuck), \
                mock.patch.object(cowork, 'git_program', return_value='git'):  # the Windows backend, a file held open
            code, out, err = self.run_cli('read', 'codex-x')
        self.assertEqual((code, out), (0, 'done\nFiles touched: none\n'))
        self.assertIn('delivered, but', err)
        self.assertTrue(err.endswith(receipt('codex-x')), err)

    def test_reset_with_a_file_still_open_elsewhere_is_refused_not_raised(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'session').write_text('thread-42\n')
        (box / 'codex-x.lock').touch()
        stuck = PermissionError(13, 'in use', str(box / 'codex-x.jsonl'))
        with mock.patch.object(cowork, 'remove', side_effect=stuck):
            code, _, err = self.run_cli('reset', 'codex', 'main')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn(f'could not remove {box / "codex-x.jsonl"}', err)
        code, out, _ = self.run_cli('reset', 'codex', 'main')
        self.assertEqual((code, out), (0, 'codex/main: session forgotten, 1 undelivered request(s) removed\n'))

    def test_reset_refuses_while_a_delivered_requests_leftover_is_held(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'session').write_text('thread-42\n')
        (box / 'codex-main-1.jsonl').touch()  # its lock went with the delivery
        with mock.patch.object(cowork, 'sweep', return_value=[box / 'codex-main-1.jsonl']):
            code, _, err = self.run_cli('reset', 'codex', 'main')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn(f'could not remove {box / "codex-main-1.jsonl"}', err)
        self.assertTrue((box / 'session').exists(), 'the session stays until the reset can finish')

    def test_what_a_delivery_left_goes_with_the_next_send_or_reset(self):
        box = self.box()
        box.mkdir(parents=True)
        for leftover in ('codex-main-1.jsonl', 'codex-main-1.exit.42.tmp'):  # its lock went, these stayed open
            (box / leftover).touch()
        for name in ('codex-main-2.lock', 'codex-main-2.jsonl'):  # a request still there keeps its files
            (box / name).touch()
        code, request, _, _ = self.send('--task', 'x')
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in box.glob('codex-main-*')), ['codex-main-2.jsonl', 'codex-main-2.lock'])
        (box / 'codex-main-3.err').touch()
        self.run_cli('reset', 'codex', 'main')
        self.assertEqual(list(box.glob('codex-main-*')), [])

    def test_git_output_that_is_not_utf8_is_replaced_not_raised_on_windows(self):
        (self.root / 'legacy.txt').write_bytes(b'caf\xe9\n')  # what a hook in a legacy code page prints
        blob = sh(self.root, 'git', 'hash-object', '-w', 'legacy.txt').strip()
        with mock.patch.object(cowork, 'GIT_ENCODING', 'utf-8'):
            self.assertEqual(cowork.git(self.root, 'cat-file', '-p', blob).stdout, 'caf�\n')

    @unittest.skipIf(os.name != 'nt', 'os.replace onto an open file fails on Windows only')
    def test_setting_the_default_waits_out_a_reader_of_the_preference_file(self):
        self.assertEqual(self.run_cli('default', 'astra')[0], 0)
        holder = cowork.preset_file().open()
        try:
            with letting_go(holder) as met:
                code, _, err = self.run_cli('default', 'sol')
        finally:
            holder.close()
        self.assertEqual(code, 0, err)
        self.assertTrue(met, 'the replace met the open file')
        self.assertEqual(cowork.preset_file().read_bytes(), b'sol\n')

    @unittest.skipIf(os.name != 'nt', 'stdin is read as UTF-8 on Windows; elsewhere in the locale encoding')
    def test_a_task_on_stdin_that_is_not_utf8_is_a_usage_error(self):
        done = subprocess.run([sys.executable, str(SCRIPT), 'send', '--task', '-'], cwd=self.root,
                              input=b'caf\xe9\n', capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr.decode().strip(), '--task - must be UTF-8')
        self.assertEqual(self.calls(), [])

    def test_a_cygwin_or_msys_python_is_refused(self):
        for platform in ('cygwin', 'msys'):
            with self.subTest(platform), mock.patch.object(sys, 'platform', platform):
                code, out, err = self.run_cli('default')
                self.assertEqual((code, out), (1, ''))
                self.assertIn('native Windows CPython', err)

    def test_the_windows_middle_process_is_no_command_elsewhere(self):
        with mock.patch.object(cowork, 'fcntl', object()), mock.patch.object(cowork, 'nt_detach') as detach:
            code, _, err = self.run_cli('_detach', 'codex', 'main', 'codex-x', '0', '5')
        self.assertEqual(code, 2, err)
        detach.assert_not_called()

    def test_git_output_is_utf8_on_windows_and_the_locale_encoding_elsewhere(self):
        real, seen = subprocess.run, []

        def recording(*args, **kwargs):
            if args[0][0] == cowork.git_program():
                seen.append(kwargs)
            return real(*args, **kwargs)
        with mock.patch.object(cowork.subprocess, 'run', recording):
            cowork.git_dir()
            cowork.git(self.root, 'status')
            cowork.tree_state(self.root)
        self.assertGreater(len(seen), 3)
        for kwargs in seen:  # as text=True alone decodes outside Windows: strictly, in the locale's encoding
            self.assertTrue(kwargs.get('text'))
            self.assertEqual(kwargs.get('encoding'), 'utf-8' if os.name == 'nt' else None)
            self.assertIn(kwargs.get('errors'), (None, 'replace') if os.name == 'nt' else (None,))

    def test_publish_is_all_or_nothing_and_the_first_verdict_wins(self):
        target = self.root / 'x.exit'
        self.assertTrue(cowork.publish(target, '0\n'))
        self.assertFalse(cowork.publish(target, 'died\n'))
        self.assertEqual(target.read_text(), '0\n')
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_a_published_verdict_stands_when_its_temporary_file_is_held(self):
        target = self.root / 'x.exit'
        unlink = Path.unlink
        def held(path, *args, **kwargs):
            if path.suffix == '.tmp':
                raise PermissionError(13, 'in use', str(path))
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', held), mock.patch.object(cowork.time, 'sleep'):
            self.assertTrue(cowork.publish(target, 'killed\n'))
        self.assertEqual(target.read_text(), 'killed\n')

    def test_a_verdict_is_published_after_a_reader_briefly_holds_its_temporary_file(self):
        target = self.root / 'x.exit'
        with mock.patch.object(cowork.windows.os, 'name', 'nt'), mock.patch.object(cowork.windows.time, 'sleep'), \
                mock.patch.object(cowork.os, 'rename', wraps=os.rename,
                                  side_effect=[PermissionError(13, 'in use'), mock.DEFAULT]) as move:
            self.assertTrue(cowork.publish(target, '0\n'))
        self.assertEqual((target.read_text(), move.call_count), ('0\n', 2))

    # --- one request in flight -----------------------------------------------------------

    def test_one_request_in_flight_per_side_and_the_next_resumes_the_same_session(self):
        first, first_id = self.send_gated('--task', 'one')
        self.started(first_id)
        _, out, _ = self.run_cli('status')
        self.assertIn(f'{first_id}  running', out)
        before = sorted(p.name for p in self.box().iterdir())
        code, _, _, err = self.send('--task', 'two')
        self.assertEqual(code, cowork.BUSY, 'a second send while one runs is refused')
        self.assertIn(first_id, err)
        self.assertEqual(sorted(p.name for p in self.box().iterdir()), before, 'the refused send left nothing')
        self.gate.touch()
        self.assertEqual(first.wait(timeout=30), 0)
        self.assertIn('Files touched: none', first.stdout.read())
        code, _, _, _ = self.send('--task', 'two')
        self.assertEqual(code, 0)
        tasks = [task_of(c['stdin']) for c in self.calls()]
        self.assertEqual(tasks, ['one', 'two'])
        self.assertNotIn('resume', self.calls()[0]['argv'])
        self.assertEqual(self.calls()[1]['argv'][:3], ['exec', 'resume', 'thread-42'])
        self.assertIn('coworker on the cowork channel', self.calls()[0]['stdin'])
        self.assertNotIn('coworker on the cowork channel', self.calls()[1]['stdin'], 'bootstrap decided at run time')

    def test_a_failed_turn_does_not_block_the_next_send(self):
        code, _, _, _ = self.send('--task', 'one', FAKE_EXIT='2')
        self.assertEqual(code, 1)
        code, _, _, _ = self.send('--task', 'two')
        self.assertEqual(code, 0)

    def test_a_corpse_in_the_box_neither_blocks_nor_poisons(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-00000000-000000-000000.task').write_text('p')
        (box / 'codex-00000000-000000-000000.lock').touch()  # nobody holds it
        code, _, _, _ = self.send('--task', 'after a corpse')
        self.assertEqual(code, 0)
        _, out, _ = self.run_cli('status')
        self.assertIn('codex-00000000-000000-000000  died', out)

    def test_reset_refuses_while_a_request_runs(self):
        proc, request = self.send_gated('--task', 'slow')
        code, _, err = self.run_cli('reset', 'codex', 'main')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn(request, err)
        self.gate.touch()
        proc.wait(timeout=30)
        code, _, _ = self.run_cli('reset', 'codex', 'main')
        self.assertEqual(code, 0)

    # --- kills --------------------------------------------------------------------------

    def test_kill_stops_a_running_request_and_frees_the_side(self):
        first, first_id = self.send_gated('--task', 'slow')
        self.started(first_id)
        code, out, _ = self.run_cli('kill', first_id)
        self.assertEqual(out.strip(), f'{first_id}: killed')
        self.assertEqual(first.wait(timeout=10), 1)
        self.assertIn('was killed', first.stdout.read())
        self.assertFalse(cowork.held(self.box() / f'{first_id}.lock'), 'nothing of the turn survives')
        code, _, _, _ = self.send('--task', 'next')
        self.assertEqual(code, 0)

    def test_a_kill_that_lands_before_the_runner_starts_runs_nothing(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        cowork.publish(box / 'codex-x.exit', 'killed\n')
        with holding(box / 'codex-x.lock') as lock, contextlib.redirect_stderr(io.StringIO()):
            cowork.write_side(box, model='gpt-6-astra', effort='medium')
            cowork.run('codex', box, self.root, 'codex-x', False, lock.fileno())
        self.assertEqual(self.calls(), [])
        self.assertTrue((box / 'codex-x.task').exists(), 'unread')

    def test_kill_takes_tools_that_left_the_process_group(self):
        away = ('creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP' if os.name == 'nt'
                else 'start_new_session=True')
        self.codex_does(f"import subprocess; open('tool.pid', 'w').write(str(subprocess.Popen({SLEEP!r}, {away}).pid))")
        proc, request = self.send_gated('--task', 'x')
        until(lambda: (self.root / 'tool.pid').exists() and (self.root / 'tool.pid').read_text())
        self.run_cli('kill', request)
        proc.wait(timeout=10)
        tool = int((self.root / 'tool.pid').read_text())
        if os.name == 'nt':  # TerminateProcess returns before the process is gone
            until(lambda: gone(tool), timeout=5)
        self.assertTrue(gone(tool), 'a tool in its own session died with the tree')

    def test_kill_freezes_the_tree_so_nothing_spawned_meanwhile_escapes(self):
        if os.name == 'nt':  # no sh or setsid: detached Pythons, which the runner's job still holds
            late = ("import subprocess, sys, time\nwhile True:\n    subprocess.Popen([sys.executable, '-c', "
                    "'import pathlib, time; time.sleep(1); pathlib.Path(\"late\").touch()'], "
                    "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)\n    time.sleep(0.05)")
            self.codex_does(f'import subprocess; subprocess.Popen([sys.executable, "-c", {late!r}])')
        else:
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
        with holding(box / 'codex-x.lock'):  # a kill was published; the teardown is not done
            self.assertEqual(cowork.running(box), 'codex-x')
            code, _, _ = self.run_cli('read', 'codex-x')
            self.assertEqual(code, cowork.BUSY, 'a verdict does not make teardown complete')
            code, _, _ = self.run_cli('reset', 'codex', 'main')
            self.assertEqual(code, cowork.BUSY)
        self.assertIsNone(cowork.running(box))

    def test_kill_signals_only_a_pid_that_holds_the_request_lock(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        stranger = subprocess.Popen(SLEEP)  # a reused pid: a process that never had the lock
        with holding(box / 'codex-x.lock') as lock:  # a runner still holds it, but its CLI is gone
            lock.write(cowork.lock_record(stranger.pid, None if fcntl else cowork.nt_born(stranger.pid) - 1))
            lock.flush()
            with mock.patch.object(cowork, 'kill_tree') as kill:
                code, out, _ = self.run_cli('kill', 'codex-x')
            self.assertEqual(out.strip(), 'codex-x: killed')
            kill.assert_not_called()
        stranger.kill()
        stranger.wait()
        (box / 'codex-y.task').write_text('p')
        with holding(box / 'codex-y.lock') as lock:
            orphan = spawn_holding(lock, SLEEP)
            lock.write(cowork.lock_record(orphan.pid))
        try:
            self.assertTrue(cowork.holds(orphan.pid, box / 'codex-y.lock'))
            code, out, _ = self.run_cli('kill', 'codex-y')
            self.assertEqual(orphan.wait(timeout=5), KILLED, 'the pid that holds the lock is ours to kill')
        finally:
            if orphan.poll() is None:
                orphan.kill()
                orphan.wait()

    @unittest.skipIf(fcntl is None, '/proc lists the open lock')
    def test_a_descriptor_closed_while_listed_does_not_hide_the_lock(self):
        box = self.box()
        box.mkdir(parents=True)
        lock_file = box / 'codex-x.lock'
        with holding(lock_file) as lock:
            orphan = spawn_holding(lock, SLEEP)
        self.addCleanup(lambda: (orphan.kill(), orphan.wait()))
        real = os.readlink

        def closed_since_listed(fd):  # a CLI opens and closes files all the time
            if real(fd) != str(lock_file):
                raise FileNotFoundError(fd)
            return real(fd)
        with mock.patch.object(cowork.os, 'readlink', closed_since_listed):
            self.assertTrue(cowork.holds(orphan.pid, lock_file))

    @unittest.skipIf(os.name != 'nt', 'the Windows backend records the pid\'s creation time; /proc shows the open lock')
    def test_a_pid_reused_after_the_runner_wrote_it_is_not_ours(self):
        box = self.box()
        box.mkdir(parents=True)
        lock = box / 'codex-x.lock'
        with holding(lock) as held:
            stranger = subprocess.Popen(SLEEP)
            try:
                held.write(cowork.lock_record(stranger.pid))
                held.flush()
                self.assertTrue(cowork.holds(stranger.pid, lock), 'the process the runner recorded')
                held.seek(0)
                held.truncate()
                held.write(cowork.lock_record(stranger.pid, cowork.nt_born(stranger.pid) - 1))  # its pid's earlier owner
                held.flush()
                self.assertFalse(cowork.holds(stranger.pid, lock))
                held.seek(0)
                held.truncate()
                held.write(f'{stranger.pid}\n')
                held.flush()
                self.assertFalse(cowork.holds(stranger.pid, lock), 'a pid without its creation time')
            finally:
                stranger.kill()
                stranger.wait()

    def test_kill_on_a_finished_request_signals_nothing(self):
        request = self.reaped('--task', 'done')
        self.settled(request)
        (self.box() / f'{request}.lock').write_text(f'{os.getpid()}\n')  # a reused pid: ours
        with mock.patch.object(cowork, 'kill_tree') as kill:
            code, out, _ = self.run_cli('kill', request)
        self.assertEqual(out.strip(), f'{request}: replied')
        kill.assert_not_called()

    def test_a_kill_that_wins_during_conclude_is_the_verdict(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        (box / 'codex-x.jsonl').touch()
        (box / 'codex-x.err').touch()
        with holding(box / 'codex-x.lock') as lock:

            def kill_meanwhile(*args, **kwargs):
                cowork.publish(box / 'codex-x.exit', 'killed\n')
                return '0'
            with mock.patch.object(cowork, 'conclude', kill_meanwhile), \
                 contextlib.redirect_stderr(io.StringIO()):
                cowork.write_side(box, model='gpt-6-astra', effort='medium')
                cowork.run('codex', box, self.root, 'codex-x', False, lock.fileno())
            self.assertEqual((box / 'codex-x.exit').read_text(), 'killed\n')

    def test_a_shared_reader_of_the_lock_does_not_look_like_a_live_runner(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.lock').write_text('4242\n')  # the runner is exiting
        (box / 'codex-x.exit').write_text('0\n')
        (box / 'codex-x.reply').write_bytes(b'done\nFiles touched: none\n')  # as a runner writes it
        (box / 'codex-x.err').touch()
        with holding(box / 'codex-x.lock', 'r+'):  # something the CLI spawned still has the runner's lock
            self.assertTrue(cowork.held(box / 'codex-x.lock'), 'a verdict line is not release')
            code, _, _ = self.run_cli('read', 'codex-x')
            self.assertEqual(code, cowork.BUSY)
        with reading(box / 'codex-x.lock'):
            self.assertFalse(cowork.held(box / 'codex-x.lock'))
            code, out, _ = self.run_cli('read', 'codex-x')
            self.assertEqual((code, out), (0, 'done\nFiles touched: none\n'))

    def test_a_grandchild_holding_the_lock_keeps_the_request_live_after_the_cli_exits(self):
        release = Path(self.tmp.name) / 'release'
        wait = f'import os, time\nwhile not os.path.exists({str(release)!r}): time.sleep(0.02)'
        self.codex_does(f'import subprocess; subprocess.Popen([sys.executable, "-c", {wait!r}], close_fds=False)')
        request = self.send('--detach', '--task', 'x')[1]
        lock = self.box() / f'{request}.lock'
        try:
            until(lambda: (self.box() / f'{request}.exit').exists())  # the CLI and its runner are done
            self.assertTrue(cowork.held(lock), 'what the CLI spawned still holds it')
            self.assertEqual(self.run_cli('read', request)[0], cowork.BUSY)
        finally:
            release.touch()
        until(lambda: not cowork.held(lock))
        code, out, _ = self.run_cli('read', request)
        self.assertEqual((code, out), (0, 'codex reply\nFiles touched: none\n'))

    def test_kill_of_a_delivered_request_leaves_no_orphan_verdict(self):
        _, request, _, _ = self.send('--task', 'done')
        code, _, err = self.run_cli('kill', request)
        self.assertEqual(code, cowork.BUSY)
        self.assertEqual(self.leftovers(request), [])

    # --- the runner outlives the caller ---------------------------------------------

    def test_a_killed_send_loses_nothing(self):
        proc, request = self.send_gated('--task', 'x')
        self.started(request)
        if os.name == 'nt':  # what a harness does: send and its descendants; the runner is none of them
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, check=True)
        else:
            runner = int(subprocess.run(['pgrep', '-f', f'_run codex main {request}'], capture_output=True, text=True).stdout.split()[0])
            with open(f'/proc/{runner}/stat') as stat:
                self.assertNotEqual(int(stat.read().rsplit(')', 1)[1].split()[1]), proc.pid, 'the runner is not a child of send')
        proc.kill()
        proc.wait()
        self.settled(request)
        self.assertEqual((self.box() / f'{request}.exit').read_text(), 'replied\n')
        self.assertEqual((self.box() / f'{request}.reply').read_text(), 'codex reply\nFiles touched: none\n')
        self.assertEqual(cowork.session_of(self.box()), 'thread-42', 'the thread was still bound')
        _, out, _ = self.run_cli('status')
        self.assertIn(f'{request}  replied', out, 'undelivered, so still listed')
        code, out, _ = self.run_cli('read', request)
        self.assertEqual((code, out), (0, 'codex reply\nFiles touched: none\n'))
        self.assertEqual(self.leftovers(request), [])

    def test_a_dead_runner_with_a_live_coworker_is_busy_until_killed(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.task').write_text('p')
        with holding(box / 'codex-x.lock') as lock:
            orphan = spawn_holding(lock, SLEEP)
            lock.write(cowork.lock_record(orphan.pid))
        try:
            _, out, _ = self.run_cli('status')
            self.assertIn('codex-x  running', out)
            code, _, err = self.run_cli('reset', 'codex', 'main')
            self.assertEqual(code, cowork.BUSY)
            code, out, _ = self.run_cli('kill', 'codex-x')
            self.assertEqual(out.strip(), 'codex-x: killed')
            self.assertEqual(orphan.wait(timeout=5), KILLED, 'killed, not waited for')
        finally:
            if orphan.poll() is None:
                orphan.kill()
                orphan.wait()
        code, _, _ = self.run_cli('reset', 'codex', 'main')
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
        for env, want in (({}, (0, 'codex reply\nFiles touched: none\n', 'replied, exit 0')),
                          ({'FAKE_REPLY': 'missing footer\n'}, (cowork.MALFORMED, 'missing footer\n', 'no "Files touched" line')),
                          ({'FAKE_EXIT': '2'}, (cowork.FAILED, 'exited 2', 'failed, exit 1'))):
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
        code, out, err = self.run_cli('read', 'codex-x')
        self.assertEqual((code, err), (cowork.FAILED, receipt('codex-x', 'died', cowork.FAILED)))
        self.assertIn('codex-x died without recording a verdict', out)
        self.assertEqual(self.leftovers('codex-x'), [])

    def test_send_detach_returns_the_id_at_once_and_read_wait_delivers_it(self):
        code, request, reply, err = self.send('--detach', '--task', 'x', FAKE_GATE=str(self.gate))
        self.assertEqual((code, reply, err), (0, '', ''), 'only the id is printed')
        self.assertTrue(request.startswith('codex-main-'), request)
        self.assertTrue(cowork.held(self.box() / f'{request}.lock'), 'the runner still holds the request')
        reader = self.read_wait(request)
        self.gate.touch()
        out, err = reader.communicate(timeout=10)
        self.assertEqual((reader.returncode, out, err), (0, 'codex reply\nFiles touched: none\n', receipt(request)))
        self.assertEqual(self.leftovers(request), [])

    def test_send_detach_exits_and_closes_its_output_while_the_runner_works(self):
        done = subprocess.run([sys.executable, str(SCRIPT), 'send', '--detach', '--task', 'x'], cwd=self.root,
                              capture_output=True, text=True, timeout=10, env={**os.environ, 'FAKE_GATE': str(self.gate)})
        request = done.stdout.strip()
        self.assertEqual((done.returncode, done.stderr, done.stdout), (0, '', request + '\n'))
        self.assertTrue(cowork.held(self.box() / f'{request}.lock'), 'the runner still holds the request')
        self.settled(request)

    def test_read_wait_recovers_a_dead_sender_and_binds_the_session(self):
        request = self.reaped('--task', 'x')
        reader = self.read_wait(request)
        self.assertIsNone(cowork.session_of(self.box()))
        self.gate.touch()
        out, err = reader.communicate(timeout=10)
        self.assertEqual((reader.returncode, out, err), (0, 'codex reply\nFiles touched: none\n', receipt(request)))
        self.assertEqual(cowork.session_of(self.box()), 'thread-42')
        self.assertEqual(self.leftovers(request), [])

    def test_read_wait_waits_for_lock_release_even_with_a_verdict(self):
        box = self.box()
        box.mkdir(parents=True)
        (box / 'codex-x.exit').write_text('0\n')
        (box / 'codex-x.reply').write_bytes(b'done\nFiles touched: none\n')
        with holding(box / 'codex-x.lock'):
            reader = self.read_wait('codex-x')
            self.assertTrue((box / 'codex-x.reply').exists())
        out, err = reader.communicate(timeout=10)
        self.assertEqual((reader.returncode, out, err), (0, 'done\nFiles touched: none\n', receipt('codex-x')))

    def test_read_wait_leaves_status_and_kill_responsive(self):
        request = self.reaped('--task', 'x')
        reader = self.read_wait(request)
        status = subprocess.run([sys.executable, str(SCRIPT), 'status'], capture_output=True, text=True, timeout=5)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f'{request}  running', status.stdout)
        killed = subprocess.run([sys.executable, str(SCRIPT), 'kill', request],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(killed.returncode, 0, killed.stderr)
        out, err = reader.communicate(timeout=10)
        self.assertEqual(reader.returncode, cowork.FAILED)
        self.assertIn('was killed', out)
        self.assertEqual(err, receipt(request, 'killed', cowork.FAILED))

    def test_read_wait_out_writes_the_delivery_and_prints_only_a_short_receipt(self):
        out_file = Path(self.tmp.name) / 'reply.txt'
        request = self.reaped('--task', 'x')
        reader = subprocess.Popen([sys.executable, str(SCRIPT), 'read', '--wait', '--out', str(out_file), request],
                                  cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.background.append(reader)
        self.gate.touch()
        out, err = reader.communicate(timeout=10)
        self.assertEqual((reader.returncode, out, err),
                         (0, '', f'cowork result {request} codex/main: replied, exit 0; written to {out_file}\n'))
        self.assertEqual(out_file.read_bytes().decode(), 'codex reply\nFiles touched: none\n' + receipt(request),
                         'the same bytes on every OS')
        self.assertEqual(self.leftovers(request), [])
        self.assertEqual(sorted(p.name for p in out_file.parent.glob('reply.txt*')), ['reply.txt'], 'no temp file left')

    def test_read_out_keeps_exit_codes_diagnostics_and_full_receipts_in_the_file(self):
        out_file = Path(self.tmp.name) / 'reply.txt'
        for env, code, outcome, said in (({'FAKE_REPLY': 'did it\n'}, cowork.MALFORMED, 'no-footer', 'no "Files touched" line'),
                                         ({'FAKE_EXIT': '2'}, cowork.FAILED, 'failed', 'exited 2')):
            with self.subTest(outcome):
                request = self.reaped('--task', 'x', **env)
                self.settled(request)
                got, out, err = self.run_cli('read', '--out', str(out_file), request)
                self.assertEqual((got, out, err), (code, '', f'cowork result {request} codex/main: {outcome}, exit {code}; '
                                                              f'written to {out_file}\n'))
                written = out_file.read_text()
                self.assertIn(said, written)
                self.assertTrue(written.endswith(receipt(request, outcome, code)), written)
                self.gate.unlink()

    def test_read_out_that_cannot_be_written_is_refused_and_keeps_the_request(self):
        request = self.reaped('--task', 'x')
        self.settled(request)
        missing = Path(self.tmp.name) / 'no-such-dir' / 'reply.txt'
        code, out, err = self.run_cli('read', '--out', str(missing), request)
        self.assertEqual((code, out), (cowork.BUSY, ''))
        self.assertIn(f'could not write {missing}', err)
        self.assertNotIn('cowork result', err)
        self.assertFalse(missing.parent.exists())
        out_file = Path(self.tmp.name) / 'reply.txt'
        code, out, err = self.run_cli('read', '--out', str(out_file), request)
        self.assertEqual((code, out, err), (0, '', f'cowork result {request} codex/main: replied, exit 0; written to {out_file}\n'))
        self.assertEqual(out_file.read_text(), 'codex reply\nFiles touched: none\n' + receipt(request))
        self.assertEqual(self.leftovers(request), [])

    def test_read_out_of_an_unknown_request_is_refused_and_writes_nothing(self):
        out_file = Path(self.tmp.name) / 'reply.txt'
        code, out, err = self.run_cli('read', '--wait', '--out', str(out_file), 'codex-nope')
        self.assertEqual((code, out), (cowork.BUSY, ''))
        self.assertIn('no request codex-nope', err)
        self.assertFalse(out_file.exists())

    def test_status_lists_lanes_holding_a_request_and_counts_idle_ones(self):
        self.commit('a.txt', 'a')
        self.send('--lane', 'review-a', '--task', 'x')
        self.send('--lane', 'review-b', '--task', 'x')
        undelivered = self.reaped('--task', 'x')
        self.settled(undelivered)
        _, out, _ = self.run_cli('status')
        self.assertEqual(out, f'codex/main: session thread-42, gpt-6.1-sol at high effort, tier default\n'
                              f'  {undelivered}  replied\n2 idle lanes, listed by status --all\n')
        _, out, _ = self.run_cli('status', '--all')
        self.assertIn('codex/review-a: session thread-42', out)
        self.assertIn('codex/review-b: session thread-42', out)
        self.assertIn(f'  {undelivered}  replied', out)
        self.assertIn('claude: no lane', out)
        self.assertNotIn('idle', out)
        self.run_cli('read', undelivered)
        self.assertEqual(self.run_cli('status')[1], '3 idle lanes, listed by status --all\n')

    def test_read_wait_competing_consumers_deliver_once(self):
        request = self.reaped('--task', 'x')
        readers = [self.read_wait(request), self.read_wait(request)]
        self.gate.touch()
        results = []
        for reader in readers:
            out, err = reader.communicate(timeout=10)
            results.append((reader.returncode, out, err))
        results.sort()
        self.assertEqual(results[0], (0, 'codex reply\nFiles touched: none\n', receipt(request)))
        self.assertEqual(results[1][:2], (cowork.BUSY, ''))
        self.assertIn('delivered already', results[1][2])
        self.assertEqual(self.leftovers(request), [])

    def test_a_killed_read_wait_leaves_the_request_recoverable(self):
        request = self.reaped('--task', 'x')
        reader = self.read_wait(request)
        reader.kill()
        reader.wait(timeout=5)
        self.assertTrue(cowork.held(self.box() / f'{request}.lock'))
        replacement = self.read_wait(request)
        self.gate.touch()
        out, err = replacement.communicate(timeout=10)
        self.assertEqual((replacement.returncode, out, err), (0, 'codex reply\nFiles touched: none\n', receipt(request)))
        self.assertEqual(self.leftovers(request), [])

    def test_the_jsonl_exists_the_instant_the_id_is_printed(self):
        proc, request = self.send_gated('--task', 'x')
        self.assertTrue((self.box() / f'{request}.jsonl').exists())
        self.gate.touch()
        proc.wait(timeout=30)

    # --- lanes ----------------------------------------------------------------

    def commit(self, name, text, cwd=None):
        cwd = cwd or self.root
        (cwd / name).write_text(text)
        sh(cwd, 'git', 'add', name)
        sh(cwd, 'git', 'commit', '-q', '-m', name)
        return sh(cwd, 'git', 'rev-parse', 'HEAD').strip()

    def head(self, cwd):
        return sh(cwd, 'git', 'rev-parse', 'HEAD').strip()

    def test_a_worktree_lane_works_in_its_own_tree_and_resumes(self):
        base = self.commit('a.txt', 'a')
        tree = self.root / '.worktrees' / 'cowork-codex-impl'
        code, request, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.assertEqual(code, 0, err)
        self.assertTrue(request.startswith('codex-impl-'))
        self.assertEqual(self.calls()[0]['cwd'], str(tree))
        prompt = self.calls()[0]['stdin']
        self.assertIn(f'on lane impl,', prompt)
        self.assertIn(f'Your checkout is the worktree {tree} on branch cowork/main/codex-impl, created at {base[:12]} '
                      'of the host checkout; commit there.', prompt)
        self.assertEqual(self.head(tree), base)
        code, out, _ = self.run_cli('status', '--all')
        self.assertIn(f'codex/impl: session thread-42, gpt-6.1-sol at high effort, tier default, in {tree}', out)
        self.assertNotIn('codex/main', out)
        self.send('--lane', 'impl', '--worktree', '--task', 'y')
        self.assertEqual(self.calls()[1]['argv'][:3], ['exec', 'resume', 'thread-42'])
        self.assertEqual(self.head(tree), base)

    def test_a_read_only_lane_is_this_checkout_and_every_send_is_no_edit(self):
        self.commit('a.txt', 'a')
        code, _, _, err = self.send('--lane', 'review', '--read-only', '--task', 'x')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[0]['cwd'], str(self.root))
        self.assertIn('Scope: do not edit anything', self.calls()[0]['stdin'])
        self.assertFalse((self.root / '.worktrees').exists())
        code, out, _ = self.run_cli('status', '--all')
        self.assertIn('codex/review: session thread-42, gpt-6.1-sol at high effort, tier default, read-only', out)
        self.codex_does("open('edited.txt', 'w').write('!')")
        code, _, _, err = self.send('--lane', 'review', '--task', 'y')  # no --no-edit: the lane implies it
        self.assertEqual(code, cowork.MALFORMED)
        self.assertIn('tree changed during a --no-edit turn', err)
        (self.root / 'edited.txt').unlink()
        self.send('--lane', 'impl', '--worktree', '--task', 'z')
        code, _, _, err = self.send('--lane', 'impl', '--read-only', '--task', 'w')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('codex/impl is a worktree lane; it keeps its kind until reset', err)
        code, _, _, err = self.send('--lane', 'review', '--worktree', '--task', 'u')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('codex/review is a read-only lane; it keeps its kind until reset', err)
        for flag in ('--read-only', '--worktree'):
            code, _, _, err = self.send(flag, '--task', 'v')
            self.assertEqual(code, cowork.BUSY)
            self.assertIn('main is this checkout and writable', err)
        self.assertEqual(self.send('--lane', 'x', '--read-only', '--worktree', '--task', 't')[0], 2, 'one kind or the other')

    def test_a_new_named_lane_is_read_only_unless_its_first_send_says_worktree(self):
        self.commit('a.txt', 'a')
        code, _, _, err = self.send('--lane', 'plan', '--task', 'x')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[-1]['cwd'], str(self.root))
        self.assertIn('Scope: do not edit anything', self.calls()[-1]['stdin'])
        self.assertIn('codex/plan: session thread-42, gpt-6.1-sol at high effort, tier default, read-only', self.run_cli('status', '--all')[1])
        self.assertFalse((self.root / '.worktrees').exists())
        box = self.box(lane='old')  # a worktree lane from before the default changed: no marker, a base
        box.mkdir(parents=True)
        self.send('--lane', 'old', '--worktree', '--task', 'y')
        (box / 'session').unlink()
        self.send('--lane', 'old', '--task', 'z')
        self.assertEqual(self.calls()[-1]['cwd'], str(self.root / '.worktrees' / 'cowork-codex-old'), 'it keeps its kind')

    def test_lanes_run_concurrently_with_one_request_in_flight_each(self):
        self.commit('a.txt', 'a')
        proc, first = self.send_gated('--task', 'slow')
        self.started(first)
        code, _, _, err = self.send('--lane', 'review', '--read-only', '--task', 'meanwhile')
        self.assertEqual(code, 0, err)
        code, _, _, err = self.send('--task', 'again')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn(f'codex/main is busy with {first}', err)
        self.gate.touch()
        proc.wait(timeout=30)

    def test_a_lane_with_a_session_but_no_kind_is_refused_not_converted(self):
        self.commit('a.txt', 'a')
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        (self.box(lane='impl') / 'base').unlink()
        ran = len(self.calls())
        code, _, _, err = self.send('--lane', 'impl', '--task', 'y')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('codex/impl has a session but no record of its kind', err)
        self.assertEqual(len(self.calls()), ran)
        self.assertFalse((self.box(lane='impl') / 'read-only').exists())
        self.assertIn('codex/impl: session thread-42, gpt-6.1-sol at high effort, tier default, kind unknown',
                      self.run_cli('status', '--all')[1])
        (self.root / '.worktrees' / 'cowork-codex-impl' / 'scratch.txt').write_text('left behind')
        code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.BUSY, "a kindless lane's tree is still guarded")
        self.assertIn('uncommitted changes', err)

    def test_a_tree_left_without_its_base_is_refused_not_run_in_the_host(self):
        self.commit('a.txt', 'a')
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        for name in ('base', 'session'):  # as if interrupted between making the tree and recording its base
            (self.box(lane='impl') / name).unlink()
        ran = len(self.calls())
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'y')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('has no record of its base', err)
        self.assertEqual(len(self.calls()), ran)
        code, _, err = self.run_cli('reset', 'codex', 'impl', '--read-only')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('codex/impl is kindless, not read-only', err)
        self.assertTrue((self.root / '.worktrees' / 'cowork-codex-impl').exists())

    def test_a_read_only_reset_refuses_a_kindless_lane_holding_a_session_or_a_request(self):
        self.commit('a.txt', 'a')
        self.send('--lane', 'review', '--task', 'x')
        request = self.send('--lane', 'pending', '--detach', '--task', 'y')[1]
        until(lambda: not cowork.held(self.box(lane='pending') / f'{request}.lock'))
        (self.box(lane='pending') / 'session').unlink()
        for lane, kept in (('review', 'session'), ('pending', f'{request}.lock')):
            box = self.box(lane=lane)
            (box / 'read-only').unlink()
            self.assertEqual(sorted(p.name for p in box.iterdir() if p.name in ('session', f'{request}.lock')), [kept])
            code, _, err = self.run_cli('reset', 'codex', lane, '--read-only')
            self.assertEqual(code, cowork.BUSY, lane)
            self.assertIn(f'codex/{lane} is kindless, not read-only', err)
            self.assertTrue((box / kept).exists(), lane)

    def test_the_recorded_base_is_the_commit_the_worktree_was_made_from(self):
        base = self.commit('a.txt', 'a')
        real = cowork.git
        def git(cwd, *args, check=True):
            done = real(cwd, *args, check=check)
            if args[:2] == ('worktree', 'add'):  # the host moves on between creating the tree and recording its base
                self.commit('b.txt', 'b')
            return done
        with mock.patch.object(cowork, 'git', git):
            self.assertEqual(self.send('--lane', 'impl', '--worktree', '--task', 'x')[0], 0)
        tree = self.root / '.worktrees' / 'cowork-codex-impl'
        self.assertEqual(self.head(tree), base)
        self.assertEqual((self.box(lane='impl') / 'base').read_text().strip(), base)
        self.assertIn(f'created at {base[:12]} of the host checkout', self.calls()[-1]['stdin'])

    def test_a_worktree_lane_stays_at_its_base_and_keeps_its_commits(self):
        base = self.commit('a.txt', 'a')
        tree = self.root / '.worktrees' / 'cowork-codex-impl'
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.commit('b.txt', 'b')
        self.send('--lane', 'impl', '--task', 'y')
        self.assertEqual(self.head(tree), base, 'host commits do not move the lane')
        self.assertIn(f'created at {base[:12]} of the host checkout', self.calls()[-1]['stdin'])
        self.codex_does("import subprocess; open('lane.txt', 'w').write('lane'); "
                        "subprocess.run(['git', 'add', 'lane.txt']); subprocess.run(['git', 'commit', '-q', '-m', 'lane'])")
        self.send('--lane', 'impl', '--task', 'commit something')
        self.codex_does('')
        sh(self.root, 'git', 'commit', '-q', '--amend', '-m', 'b, amended')
        code, _, _, err = self.send('--lane', 'impl', '--task', 'z')
        self.assertEqual(code, 0, err)
        self.assertEqual(sh(tree, 'git', 'log', '--format=%s').split(), ['lane', 'a.txt'], 'a rewritten host is not its concern')
        (tree / 'scratch.txt').write_text('left behind')
        code, _, _, err = self.send('--lane', 'impl', '--task', 'dirty')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('uncommitted changes', err)
        self.assertIn('scratch.txt', err)

    def test_reset_of_a_worktree_lane_needs_its_branch_merged(self):
        self.commit('a.txt', 'a')
        tree = self.root / '.worktrees' / 'cowork-codex-impl'
        self.codex_does("import subprocess; open('lane.txt', 'w').write('lane'); "
                        "subprocess.run(['git', 'add', 'lane.txt']); subprocess.run(['git', 'commit', '-q', '-m', 'lane'])")
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('commits on cowork/main/codex-impl that HEAD lacks', err)
        self.assertTrue(tree.exists())
        sh(self.root, 'git', 'merge', '-q', '--ff-only', 'cowork/main/codex-impl')
        code, out, _ = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, 0)
        self.assertIn(f'codex/impl: worktree {tree} removed, branch cowork/main/codex-impl deleted', out)
        self.assertIn('codex/impl: session forgotten', out)
        self.assertFalse(tree.exists())
        self.assertEqual(sorted(p.name for p in self.box(lane='impl').iterdir()), ['lock'], 'the admission lock stays')
        self.assertNotIn('cowork/main/codex-impl', sh(self.root, 'git', 'branch'))
        self.assertNotIn('codex/impl', self.run_cli('status', '--all')[1])
        self.codex_does('')
        self.send('--lane', 'review', '--read-only', '--task', 'x')
        self.send('--task', 'x')
        code, out, _ = self.run_cli('reset', 'codex', 'all')
        self.assertEqual(out.count('session forgotten'), 2)
        self.assertEqual(self.run_cli('status', '--all')[1], 'codex: no lane\nclaude: no lane\n')
        self.assertEqual(self.run_cli('status')[1], '0 idle lanes\n')
        code, out, _ = self.run_cli('reset', 'codex', 'nope')
        self.assertEqual((code, out.strip()), (0, 'codex/nope: no session'))
        code, _, err = self.run_cli('reset', 'codex', '../..')
        self.assertEqual(code, cowork.FAILED)
        self.assertIn('lane names are', err)
        self.send('--lane', 'review', '--read-only', '--task', 'again')  # a reset lane can be created anew, of either kind
        self.assertEqual(self.calls()[-1]['cwd'], str(self.root))

    def test_reset_refuses_a_cherry_picked_lane_and_does_not_advise_cherry_picking(self):
        self.commit('a.txt', 'a')
        self.codex_does("import subprocess; open('lane.txt', 'w').write('lane'); "
                        "subprocess.run(['git', 'add', 'lane.txt']); subprocess.run(['git', 'commit', '-q', '-m', 'lane'])")
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.commit('b.txt', 'b')
        sh(self.root, 'git', 'cherry-pick', 'cowork/main/codex-impl')
        code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('merge the lane branch into this checkout, or integrate it by hand and retire the worktree', err)
        self.assertNotIn('cherry-pick', err)

    def test_a_removed_tree_comes_back_with_the_lanes_own_commits(self):
        self.commit('a.txt', 'a')
        tree = self.root / '.worktrees' / 'cowork-codex-impl'
        self.codex_does("import subprocess; open('lane.txt', 'w').write('lane'); "
                        "subprocess.run(['git', 'add', 'lane.txt']); subprocess.run(['git', 'commit', '-q', '-m', 'lane'])")
        self.send('--lane', 'impl', '--worktree', '--task', 'commit')
        self.codex_does('')
        sh(self.root, 'git', 'worktree', 'remove', str(tree))
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'again')
        self.assertEqual(code, 0, err)
        self.assertEqual(sh(tree, 'git', 'log', '-1', '--format=%s').strip(), 'lane', 'its own commit is not the base')
        (self.box(lane='impl') / 'base').unlink()
        sh(self.root, 'git', 'worktree', 'remove', str(tree))
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'no base')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('has a session but no record of its kind', err)
        (self.box(lane='impl') / 'session').unlink()  # a branch some earlier lane left: which of its commits are its own?
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'no base')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('has no record of its base', err)

    def test_reset_refuses_a_tree_that_is_not_on_the_lanes_branch(self):
        self.commit('a.txt', 'a')
        tree = self.root / '.worktrees' / 'cowork-codex-impl'
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        sh(tree, 'git', 'checkout', '-q', '-b', 'unrelated')
        code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn("is not lane impl's worktree", err)
        self.assertIn('unrelated', sh(self.root, 'git', 'branch'))

    def test_reset_clears_a_lane_a_failed_first_send_left_half_made(self):
        box = self.box(lane='review')
        box.mkdir(parents=True)
        (box / 'read-only').touch()  # the first send died before settle_pair wrote the session
        code, out, _ = self.run_cli('reset', 'codex', 'review')
        self.assertEqual((code, out.strip()), (0, 'codex/review: no session'))
        self.assertFalse((box / 'read-only').exists())
        self.commit('a.txt', 'a')
        self.send('--lane', 'review', '--task', 'x')
        self.assertEqual(self.calls()[0]['cwd'], str(self.root), 'made anew, read-only by default')

    def test_a_stray_directory_is_not_taken_for_the_lanes_worktree(self):
        self.commit('a.txt', 'a')
        tree = self.root / '.worktrees' / 'cowork-codex-impl'
        tree.mkdir(parents=True)
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn(f"{tree} is not lane impl's worktree", err)
        self.assertEqual(self.calls(), [])

    def test_lane_names_are_restricted_and_a_detached_host_gets_no_worktree_lane(self):
        self.commit('a.txt', 'a')
        for bad in ('Review', 'a_b', 'all', ''):
            code, _, _, err = self.send('--lane', bad, '--task', 'x')
            self.assertEqual(code, cowork.FAILED, bad)
            self.assertIn('lane names are [a-z0-9-]', err)
        sh(self.root, 'git', 'checkout', '-q', '--detach')
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('detached', err)
        self.assertEqual(self.calls(), [])

    # --- herdr sibling lanes ----------------------------------------------------

    def herdr(self, down=False):
        """A stub herdr on HERDR_BIN_PATH (HERDR_STUB); `down`: it cannot answer. Returns its call log."""
        stub, log = Path(self.tmp.name).resolve() / 'herdr', Path(self.tmp.name).resolve() / 'herdr.log'
        stub.write_text(f'#!{sys.executable}\n{HERDR_STUB}')
        stub.chmod(0o755)
        os.environ.update(HERDR_BIN_PATH=str(stub), STUB_HERDR_LOG=str(log))
        os.environ.pop('STUB_HERDR_DOWN', None)
        if down:
            os.environ['STUB_HERDR_DOWN'] = '1'
        return log

    def no_herdr(self):
        os.environ['HERDR_BIN_PATH'] = str(Path(self.tmp.name) / 'no-herdr')

    def sibling(self, lane='impl'):
        return self.root.parent / f'repo--cowork-codex-{lane}'

    def lock_of(self, tree):
        """The reason git records for tree's worktree lock, None while unlocked."""
        locked = Path(sh(tree, 'git', 'rev-parse', '--path-format=absolute', '--git-dir').strip()) / 'locked'
        return locked.read_text().strip() if locked.exists() else None

    def detached(self, lane='impl'):
        """A gated --detach send on lane, started; returns its id, undelivered."""
        _, request = self.send_gated('--lane', lane, '--detach', '--task', 'slow')
        until(lambda: cowork.cli_pid(self.box(lane=lane) / f'{request}.lock') != '')
        return request

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_a_herdr_checkout_gets_its_worktree_lane_beside_it(self):
        base = self.commit('a.txt', 'a')
        log = self.herdr()
        tree = self.sibling()
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[-1]['cwd'], str(tree))
        self.assertEqual(self.head(tree), base)
        self.assertFalse((self.root / '.worktrees').exists())
        self.assertIn(f'in {tree}', self.run_cli('status', '--all')[1])
        self.assertIsNone(self.lock_of(tree), 'nothing outstanding: unlocked')
        asked = log.read_text()
        self.herdr(down=True)
        code, _, _, err = self.send('--lane', 'impl', '--task', 'y')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[-1]['cwd'], str(tree), 'the tree found beside the checkout')
        self.assertEqual(log.read_text(), asked, 'herdr is asked only for a new tree')
        code, out, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, 0, err)
        self.assertIn(f'worktree {tree} removed', out)
        self.assertFalse(tree.exists())
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'z')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn("cannot ask herdr where the lane's worktree goes", err)
        self.assertIn('unreachable', err)
        self.no_herdr()
        self.assertEqual(self.send('--lane', 'impl', '--worktree', '--task', 'z')[0], 0, 'placed anew after a reset')
        self.assertEqual(self.calls()[-1]['cwd'], str(self.root / '.worktrees' / 'cowork-codex-impl'))

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_a_new_lane_is_refused_when_herdr_cannot_place_it(self):
        self.commit('a.txt', 'a')
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'x', HERDR_ENV='1')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('HERDR_ENV is set but', err)
        self.herdr()
        with mock.patch.object(cowork, 'PLUGIN', Path(self.tmp.name) / 'missing.py'):
            code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn("cannot ask herdr where the lane's worktree goes", err)
        code, _, _, err = self.send('--lane', 'impl', '--worktree', '--task', 'x',
                                    STUB_HERDR_ENTRY=json.dumps({'is_linked_worktree': 'false', 'open_workspace_id': 'w1'}))
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('malformed entry', err, "the plugin's check: its tests list the rest")
        self.assertEqual(self.calls(), [])
        self.assertEqual(sorted(p.name for p in self.box(lane='impl').iterdir()), ['lock'])

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_a_nested_lane_stays_nested_until_its_tree_is_made_anew(self):
        self.commit('a.txt', 'a')
        nested = self.root / '.worktrees' / 'cowork-codex-impl'
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        log = self.herdr()
        self.send('--lane', 'impl', '--task', 'y')
        self.assertEqual(self.calls()[-1]['cwd'], str(nested))
        self.assertFalse(log.exists(), 'herdr is not asked about a tree that exists')
        sh(self.root, 'git', 'worktree', 'remove', str(nested))
        code, _, _, err = self.send('--lane', 'impl', '--task', 'z')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[-1]['cwd'], str(self.sibling()), 'a recreated tree is placed anew')
        self.assertEqual(sh(self.sibling(), 'git', 'branch', '--show-current').strip(), 'cowork/main/codex-impl')

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_reset_refuses_a_dangling_symlink_at_the_lane_place_and_forgets_nothing(self):
        self.commit('a.txt', 'a')
        self.herdr()
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        request = self.detached()
        self.gate.touch()
        until(lambda: not cowork.held(self.box(lane='impl') / f'{request}.lock'))
        sh(self.root, 'git', 'worktree', 'remove', '-f', '-f', str(self.sibling()))  # locked: a request is outstanding
        self.sibling().symlink_to(Path(self.tmp.name) / 'gone')
        box = self.box(lane='impl')
        before = sorted(p.name for p in box.iterdir())
        code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn("is not lane impl's worktree (a symlink", err)
        self.assertEqual(sorted(p.name for p in box.iterdir()), before)
        self.assertIn(request, cowork.requests(box))
        self.assertTrue((box / 'base').exists() and (box / 'session').exists())

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_a_lane_with_a_tree_at_both_places_is_refused(self):
        self.commit('a.txt', 'a')
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        self.sibling().mkdir()
        ran = len(self.calls())
        for argv in (('send', '--lane', 'impl', '--task', 'y'), ('reset', 'codex', 'impl')):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, cowork.BUSY)
                self.assertIn('has a tree at both', err)
        self.assertEqual(len(self.calls()), ran)
        self.assertIn(' and ', self.run_cli('status', '--all')[1], 'status still lists the lane')

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_a_sibling_lane_is_locked_while_any_request_is_outstanding(self):
        self.commit('a.txt', 'a')
        self.herdr()
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        tree, reason = self.sibling(), 'cowork codex/impl has a request outstanding'
        first = self.detached()
        self.assertEqual(self.lock_of(tree), reason, 'running')
        self.gate.touch()
        until(lambda: not cowork.held(self.box(lane='impl') / f'{first}.lock'))
        self.assertEqual(self.lock_of(tree), reason, 'undelivered')
        code, second, _, err = self.send('--lane', 'impl', '--detach', '--task', 'more')
        self.assertEqual(code, 0, err)
        until(lambda: not cowork.held(self.box(lane='impl') / f'{second}.lock'))
        self.assertEqual(self.run_cli('read', second)[0], 0)
        self.assertEqual(self.lock_of(tree), reason, 'the first is still undelivered')
        self.assertEqual(self.run_cli('read', first)[0], 0)
        self.assertIsNone(self.lock_of(tree), 'the last delivery unlocks')

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_a_killed_or_unwritten_reply_keeps_the_lock_until_delivered(self):
        self.commit('a.txt', 'a')
        self.herdr()
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        tree = self.sibling()
        request = self.detached()
        self.assertEqual(self.run_cli('kill', request)[0], 0)
        until(lambda: not cowork.held(self.box(lane='impl') / f'{request}.lock'))
        self.assertIsNotNone(self.lock_of(tree), 'a killed request is still undelivered')
        missing = Path(self.tmp.name) / 'no-such-dir' / 'reply.txt'
        self.assertEqual(self.run_cli('read', '--out', str(missing), request)[0], cowork.BUSY)
        self.assertIsNotNone(self.lock_of(tree), 'a failed delivery keeps it')
        self.assertEqual(self.run_cli('read', request)[0], cowork.FAILED)
        self.assertIsNone(self.lock_of(tree))

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_a_send_that_records_no_request_releases_only_the_lock_it_took(self):
        self.commit('a.txt', 'a')
        self.herdr()
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        tree, box = self.sibling(), self.box(lane='impl')
        request = self.detached()
        self.gate.touch()
        until(lambda: not cowork.held(box / f'{request}.lock'))
        real = Path.write_text
        def write_text(path, *args, **kwargs):
            if path.suffix == '.task':
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, 'write_text', write_text):
            self.assertRaises(OSError, self.send, '--lane', 'impl', '--task', 'y')
        self.assertIsNotNone(self.lock_of(tree), 'the undelivered request still holds it')
        self.assertEqual(cowork.requests(box), [request])
        self.assertEqual(self.run_cli('read', request)[0], 0)
        self.assertIsNone(self.lock_of(tree))
        with mock.patch.object(Path, 'write_text', write_text):
            self.assertRaises(OSError, self.send, '--lane', 'impl', '--task', 'z')
        self.assertIsNone(self.lock_of(tree), 'a lock taken for a request never recorded is released')
        self.assertEqual(cowork.requests(box), [])

    @unittest.skipIf(os.name == 'nt', 'a stub executable')
    def test_reset_unlocks_only_after_its_checks_and_never_a_foreign_lock(self):
        self.commit('a.txt', 'a')
        self.herdr()
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        tree = self.sibling()
        request = self.detached()
        self.gate.touch()
        until(lambda: not cowork.held(self.box(lane='impl') / f'{request}.lock'))
        (tree / 'scratch.txt').write_text('left behind')
        code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.BUSY)
        self.assertIn('uncommitted changes', err)
        self.assertIsNotNone(self.lock_of(tree), 'a refused reset keeps it')
        (tree / 'scratch.txt').unlink()
        real = cowork.git
        def git(cwd, *args, check=True):
            if args[:2] == ('worktree', 'remove'):
                return subprocess.CompletedProcess(args, 1, '', 'disk on fire')
            return real(cwd, *args, check=check)
        with mock.patch.object(cowork, 'git', git):
            code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.FAILED)
        self.assertIn('disk on fire', err)
        self.assertIsNotNone(self.lock_of(tree), 'a failed removal is locked again')
        code, out, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, 0, err)
        self.assertIn('1 undelivered request(s) removed', out)
        self.assertFalse(tree.exists())
        self.send('--lane', 'impl', '--worktree', '--task', 'y')
        sh(self.root, 'git', 'worktree', 'lock', '--reason', 'mine', str(tree))
        self.assertEqual(self.send('--lane', 'impl', '--task', 'z')[0], 0)
        self.assertEqual(self.lock_of(tree), 'mine', 'a lock already there is left as it is')
        code, _, err = self.run_cli('reset', 'codex', 'impl')
        self.assertEqual(code, cowork.FAILED)
        self.assertIn('locked', err)
        self.assertEqual(self.lock_of(tree), 'mine')

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'POSIX permissions, which root ignores')
    def test_an_unlock_that_fails_after_delivery_is_advisory(self):
        self.commit('a.txt', 'a')
        self.herdr()
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        tree = self.sibling()
        request = self.detached()
        self.gate.touch()
        until(lambda: not cowork.held(self.box(lane='impl') / f'{request}.lock'))
        admin = Path(sh(tree, 'git', 'rev-parse', '--path-format=absolute', '--git-dir').strip())
        admin.chmod(0o555)  # git cannot remove its `locked` file
        try:
            code, out, err = self.run_cli('read', request)
        finally:
            admin.chmod(0o755)
        self.assertEqual(code, 0, err)
        self.assertIn('codex reply', out)
        self.assertIn("delivered, but the lane's worktree lock stays", err)
        self.assertTrue(err.endswith(receipt(request, lane='codex/impl')), 'the receipt stays the last line')
        self.assertIsNotNone(self.lock_of(tree))
        self.assertIn('no request', self.run_cli('read', request)[2])

    @unittest.skipIf(os.name == 'nt', 'a symlink')
    def test_a_lane_tree_that_is_not_our_linked_worktree_is_never_touched(self):
        self.commit('a.txt', 'a')
        self.herdr()
        self.send('--lane', 'impl', '--worktree', '--task', 'x')
        tree, ran = self.sibling(), len(self.calls())
        sh(self.root, 'git', 'worktree', 'remove', str(tree))
        host = (sh(self.root, 'git', 'branch', '--show-current'), self.head(self.root))
        tree.symlink_to(self.root)
        for argv in (('send', '--lane', 'impl', '--task', 'y'), ('reset', 'codex', 'impl')):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, cowork.BUSY)
                self.assertIn("is not lane impl's worktree (a symlink", err)
        tree.unlink()
        other = Path(self.tmp.name).resolve() / 'other'
        other.mkdir()
        sh(other, 'git', 'init', '-q', '-b', 'main')
        sh(other, 'git', 'commit', '-q', '--allow-empty', '-m', 'other')
        sh(other, 'git', 'worktree', 'add', '-q', '-b', 'cowork/main/codex-impl', str(tree))  # the lane's branch name
        for argv in (('send', '--lane', 'impl', '--task', 'y'), ('reset', 'codex', 'impl')):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, cowork.BUSY)
                self.assertIn("is not lane impl's worktree (toplevel", err)
        self.assertEqual(len(self.calls()), ran)
        self.assertEqual((sh(self.root, 'git', 'branch', '--show-current'), self.head(self.root)), host)
        self.assertTrue(tree.exists())

    def test_unknown_request(self):
        code, _, _ = self.run_cli('kill', 'codex-nope')
        self.assertEqual(code, cowork.BUSY)
        code, _, err = self.run_cli('read', 'codex-nope')
        self.assertEqual(code, cowork.BUSY)
        self.assertNotIn('cowork result', err, 'a refusal is no delivery')


class Frontmatter(unittest.TestCase):
    @unittest.skipIf(yaml is None, 'needs PyYAML')
    def test_declares_name_and_description(self):
        fm = yaml.safe_load(SKILL.read_text(encoding='utf-8').split('---')[1])
        self.assertEqual(fm['name'], 'cowork')
        self.assertTrue(fm['description'].strip())

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == '__main__':
    unittest.main()
