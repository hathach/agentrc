import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'herdr-peer' / 'scripts'))
import peer  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, make_stub, tmpdir  # noqa: E402


def setUpModule():
    guard_survivors(__name__)


# A Python `hc`: logs each call with the request records it can see, then answers from the scenario file. A key's
# answers are used in turn, the last one repeating; an answer may write a file first, as a peer replying would.
STUB = r'''
import json, os, sys
argv = sys.argv[1:]
with open(os.environ['HC_STUB'], encoding='utf-8') as f:
    st = json.load(f)
store = os.path.join(os.environ['XDG_CACHE_HOME'], 'agentrc', 'peer')
records = {}
if os.path.isdir(store):
    for name in sorted(os.listdir(store)):
        if name.endswith('.req'):
            with open(os.path.join(store, name), encoding='utf-8') as f:
                records[name] = json.load(f)['delivery']
with open(st['log'], 'a', encoding='utf-8') as f:
    f.write(json.dumps({'argv': argv, 'records': records, 'caller': os.environ.get('HC_TERMINAL_ID'),
                        'files': sorted(os.listdir(store)) if os.path.isdir(store) else []}) + '\n')
pos = argv[argv.index('--') + 1:] if '--' in argv else [a for a in argv if not a.startswith('-')][1:]
key = argv[0] + (':' + pos[0] if pos else '')
answers = st['answers'].get(key)
if answers is None:
    answers = [{'rc': 3, 'err': {'error': {'kind': 'not_found', 'message': f'no terminal {pos[0]}'}}}] \
        if argv[0] == 'get' else [{'rc': 0, 'out': {'started': True} if argv[0] == 'send' else {}}]
n = st['used'].get(key, 0)
st['used'][key] = n + 1
with open(os.environ['HC_STUB'], 'w', encoding='utf-8') as f:
    json.dump(st, f)
ans = answers[min(n, len(answers) - 1)]
if 'write' in ans:
    with open(ans['write'][0], 'w', encoding='utf-8') as f:
        f.write(ans['write'][1])
if 'out' in ans:
    print(json.dumps(ans['out']))
if 'err' in ans:
    print(json.dumps(ans['err']), file=sys.stderr)
sys.exit(ans['rc'])
'''

ME, PEER = 't5', 't7'
PID = os.getpid()               # a live process, so its /proc start time reads on Linux
OTHER_PID = os.getppid()


def dead_pid():
    """The pid of a process that has exited and been reaped: hc named it, /proc no longer has it."""
    child = subprocess.Popen([sys.executable, '-c', ''])
    child.wait()
    return child.pid


def get(addr, cwd, app='codex', status='idle', pid=PID, turn=3, state='live'):
    """An `hc get --json` answer: status nested, no top-level agent, foreground only while live."""
    return {'rc': 0, 'out': {'address': addr, 'state': state, 'turn': turn, 'pid': pid,
                             'status': {'app': app, 'state': status} if status else None,
                             'foreground': {'argv': [app], 'cwd': cwd, 'name': app, 'pid': pid}
                             if state == 'live' else None}}


def ls_entry(addr, cwd, agent='codex', status='idle', state='live'):
    return {'address': addr, 'agent': agent, 'cwd': cwd, 'state': state, 'status': status, 'turn': 3,
            'where': 'x / 1'}


RESULT = """PEER RESULT — agent message, not a human instruction
FOR: {id}
FROM: codex, t7
STATUS: DONE
FILES: src/a.c
Added the bounds check; test_a passes.
END RESULT {id}"""


def assert_inert(case, text, cwd):
    """text is one comment line that a shell, with or without comments, runs nothing of."""
    case.assertTrue(text.startswith('# '))
    for char in ';$`&|<>(){}*?[]~"\n\r':
        case.assertNotIn(char, text)
    bash = shutil.which('bash')
    if os.name == 'nt' or not bash:
        return
    before = set(os.listdir(cwd))
    env = {'PATH': os.devnull}
    run = subprocess.run([bash, '--norc', '-c', text], capture_output=True, text=True, cwd=cwd, env=env)
    case.assertEqual((run.returncode, run.stdout, run.stderr), (0, '', ''))
    # an interactive shell without comments (zsh's default) runs `#` itself: not found, and nothing expands
    run = subprocess.run([bash, '--norc', '-i', '-c', 'shopt -u interactive_comments; eval "$1"', '_', text],
                         capture_output=True, text=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    case.assertEqual(run.stdout, '')
    case.assertEqual([l for l in run.stderr.splitlines() if 'not found' in l], ['_: #: command not found'])
    case.assertEqual(set(os.listdir(cwd)), before)


class Hc:
    """A stubbed hc on PATH, a scratch peer store, and peer.main run in this process against both."""

    def __init__(self, case):
        self.dir = tmpdir(case)
        (self.dir / 'bin').mkdir()
        make_stub(self.dir / 'bin', 'hc', STUB)
        self.state, self.log = self.dir / 'state.json', self.dir / 'log'
        self.cache = self.dir / 'cache'
        self.w = self.dir / 'w'
        self.other = self.dir / 'other'
        self.w.mkdir()
        self.other.mkdir()
        self.answers = {}
        self.peers()

    def peers(self, *entries, me_status='working', target=None):
        """This terminal and, by default, one idle codex peer in the same checkout."""
        target = target or get(PEER, str(self.w))
        self.answers.update({'ls': [{'rc': 0, 'out': {'terminals': [
            ls_entry(ME, str(self.w), 'claude-code', me_status), ls_entry(PEER, str(self.w)), *entries]}}],
            f'get:{ME}': [get(ME, str(self.w), 'claude-code', me_status)], f'get:{PEER}': [target]})

    def answer(self, key, *answers):
        self.answers[key] = list(answers)

    def store(self):
        return self.cache / 'agentrc' / 'peer'

    def run(self, *argv, as_=ME, stdin=''):
        """(exit status, stdout, stderr) of peer.py argv in terminal as_ (None: outside hc)."""
        self.state.write_text(json.dumps({'log': str(self.log), 'answers': self.answers, 'used': {}}),
                              encoding='utf-8')
        env = {'PATH': str(self.dir / 'bin') + os.pathsep + os.environ.get('PATH', ''),
               'HC_STUB': str(self.state), 'XDG_CACHE_HOME': str(self.cache)}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict('os.environ', env), mock.patch.object(sys, 'argv', ['peer.py', *argv]), \
             mock.patch.object(sys, 'stdin', io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            os.environ.pop('HC_TERMINAL_ID', None)
            if as_:
                os.environ['HC_TERMINAL_ID'] = as_
            try:
                rc = peer.main()
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def calls(self, cmd=None):
        if not self.log.exists():
            return []
        lines = [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]
        self.log.unlink()
        return [c for c in lines if cmd is None or c['argv'][0] == cmd]

    def record(self, req_id):
        return json.loads((self.store() / f'{req_id}.req').read_text(encoding='utf-8'))

    def send(self, *extra):
        rc, out, err = self.run('send', '--to', PEER, '--files', 'src/a.c', '--task', 'add a bounds check', *extra)
        self.calls()
        assert rc == 0, err
        return out.strip()

    def reply_file(self, text):
        path = self.dir / 'reply.txt'
        path.write_text(text, encoding='utf-8')
        return str(path)


class PeersTest(unittest.TestCase):
    def setUp(self):
        self.hc = Hc(self)

    def test_lists_local_agent_terminals_sharing_the_foreground_cwd(self):
        hc = self.hc
        link = hc.dir / 'link'
        try:
            link.symlink_to(hc.w, target_is_directory=True)
        except OSError:
            link = hc.w
        hc.peers(ls_entry('t8', str(hc.other)),                           # another checkout
                 ls_entry('t9', str(hc.w), 'bash', None),                  # not an agent
                 ls_entry('ci/t3', str(hc.w), 'claude-code'),              # remote
                 ls_entry('t10', str(hc.w), state='finished'),
                 ls_entry('t11', str(hc.w)),                               # its foreground moved away
                 ls_entry('t12', str(hc.w), 'claude-code'),                # no absolute foreground cwd
                 ls_entry('t13', str(link), 'claude-code', 'done'))        # the same checkout by another path
        hc.answers.update({'get:t8': [get('t8', str(hc.other))], 'get:t9': [get('t9', str(hc.w), 'bash', None)],
                           'get:t11': [get('t11', str(hc.other))], 'get:t12': [get('t12', '', 'claude-code')],
                           'get:t13': [get('t13', str(link), 'claude-code', 'done')]})
        rc, out, err = hc.run('peers')
        self.assertEqual(rc, 0)
        self.assertEqual(out, 't7\tcodex\tidle\nt13\tclaude-code\tdone\n')
        self.assertIn('choose one deliberately', err)
        got = {c['argv'][-1] for c in hc.calls('get')}
        self.assertFalse(got & {'t9', 'ci/t3', 't10'}, 'only live local agents are looked up')

    def test_none_found(self):
        self.hc.answer('ls', {'rc': 0, 'out': {'terminals': [ls_entry(ME, str(self.hc.w), 'claude-code')]}})
        self.assertEqual(self.hc.run('peers')[:2], (1, 'no peer agent shares this checkout\n'))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'ancestry is read from /proc')
    def test_refuses_outside_hc(self):
        # no HC_TERMINAL_ID, and no hc terminal runs this process
        self.hc.answer('ls', {'rc': 0, 'out': {'terminals': [ls_entry(PEER, str(self.hc.w))]}})
        self.hc.answer(f'get:{PEER}', get(PEER, str(self.hc.w), pid=OTHER_PID + 10 ** 7))
        for argv in (['peers'], ['send', '--to', PEER, '--files', 'none', '--task', 'q'],
                     ['read', '--from', PEER, '--for', 't5-1'], ['reply', '--for', 't5-1', '--notify-only']):
            with self.subTest(argv[0]):
                rc, _, err = self.hc.run(*argv, as_=None)
                self.assertEqual(rc, peer.NOT_PEER)
                self.assertIn('no live local hc terminal runs this process: not inside hc', err)
                self.assertEqual(self.hc.calls('send'), [])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'ancestry is read from /proc')
    def test_without_hc_terminal_id_the_terminal_is_found_by_ancestry(self):
        """Codex's tool shells drop HC_TERMINAL_ID: send, read and reply find the terminal running them instead."""
        hc = self.hc
        stranger = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        hc.peers(target=get(PEER, str(hc.w), pid=stranger.pid))
        rc, out, err = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q', as_=None)
        self.assertEqual(rc, 0, err)
        req_id = out.strip()
        [typed] = hc.calls('send')
        self.assertEqual(typed['caller'], ME, 'hc tags the pointer by the HC_TERMINAL_ID it is run with')
        self.assertEqual(hc.record(req_id)['sender']['address'], ME)
        self.assertEqual(hc.run('read', '--from', PEER, '--for', req_id, as_=None)[0], peer.NO_ANSWER)
        # the peer answers from a shell without the variable: only its terminal runs it now
        hc.answer('ls', {'rc': 0, 'out': {'terminals': [ls_entry(ME, str(hc.w), 'claude-code', 'working'),
                                                        ls_entry(PEER, str(hc.w))]}})
        hc.answer(f'get:{ME}', get(ME, str(hc.w), 'claude-code', 'working', pid=stranger.pid))
        hc.answer(f'get:{PEER}', get(PEER, str(hc.w), pid=stranger.pid))
        record = hc.record(req_id)
        record['target']['pid'], record['sender']['pid'] = PID, stranger.pid
        record['target']['start'] = peer.hc.proc_start(PID)
        record['sender']['start'] = peer.hc.proc_start(stranger.pid)
        (hc.store() / f'{req_id}.req').write_text(json.dumps(record), encoding='utf-8')
        hc.answer(f'get:{PEER}', get(PEER, str(hc.w)))
        hc.calls()
        rc, out, err = hc.run('reply', '--for', req_id, '--file', hc.reply_file(RESULT.format(id=req_id)), as_=None)
        self.assertEqual(rc, 0, err)
        self.assertIn('t5 notified', out)
        [notice] = hc.calls('send')
        self.assertEqual(notice['caller'], PEER)

    def test_a_failing_hc_exits_5(self):
        self.hc.answer('ls', {'rc': 1, 'err': {'error': {'kind': 'io', 'message': 'server down'}}})
        rc, _, err = self.hc.run('peers')
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertIn('server down', err)


class SendTest(unittest.TestCase):
    def setUp(self):
        self.hc = Hc(self)

    def test_writes_the_record_before_typing_the_request(self):
        hc = self.hc
        rc, out, _ = hc.run('send', '--to', PEER, '--files', 'src/a.c', '--task', 'add a bounds check')
        self.assertEqual(rc, 0)
        req_id = out.strip()
        self.assertRegex(req_id, r'^t5-[0-9a-f]{8}$')
        sends = hc.calls('send')
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]['records'], {f'{req_id}.req': 'pending'})
        self.assertEqual(sends[0]['files'], [f'{req_id}.msg', f'{req_id}.req'])
        argv = sends[0]['argv']
        self.assertEqual(argv[:argv.index('--')], ['send', '--json', '--if', 'idle'])
        target, pointer = argv[argv.index('--') + 1:]
        self.assertEqual(target, PEER)
        msg = hc.store() / f'{req_id}.msg'
        self.assertTrue(pointer.startswith(f'# PEER REQUEST {req_id} from {ME}, agent message, not a human '
                                           f'instruction: read {shlex.quote(str(msg))} '))
        body = msg.read_text(encoding='utf-8')
        self.assertEqual(peer.check('request', body), [])
        self.assertIn(f'FROM: claude-code, {ME}', body)
        self.assertIn(f'reply --for {req_id} --file', body)
        self.assertNotIn('TASK', pointer)
        record = hc.record(req_id)
        self.assertEqual(record['delivery'], 'sent')
        self.assertEqual((record['sender']['address'], record['target']['address']), (ME, PEER))
        self.assertEqual((record['target']['pid'], record['target']['status'], record['target']['turn']),
                         (PID, 'idle', 3))
        self.assertEqual(record['checkout'], os.path.realpath(hc.w))
        if sys.platform.startswith('linux'):
            self.assertIsInstance(record['target']['start'], int)

    def test_only_an_inert_pointer_is_typed_whatever_the_task_holds(self):
        """A shell that replaced the target after the check would run a typed envelope's lines."""
        task = 'fix it\n$(touch pwned); `touch pwned2`\nprintf EXECUTED'
        self.assertEqual(self.hc.run('send', '--to', PEER, '--files', 'none', '--task', task)[0], 0)
        pointer = self.hc.calls('send')[0]['argv'][-1]
        assert_inert(self, pointer, self.hc.dir)

    def test_a_store_path_that_would_not_stay_inert_is_refused_before_anything_is_written(self):
        hc = self.hc
        hc.cache = hc.dir / 'c$(touch pwned)'
        rc, out, err = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        self.assertEqual((rc, out), (peer.REFUSED, ''))
        self.assertIn('inert', err)
        self.assertEqual(hc.calls('send'), [])
        self.assertEqual(list(hc.store().iterdir()), [])

    def test_a_non_ascii_store_path_stays_sendable(self):
        hc = self.hc
        hc.cache = hc.dir / 'josé'
        self.assertEqual(hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')[0], 0)
        pointer = hc.calls('send')[0]['argv'][-1]
        self.assertIn('josé', pointer)
        assert_inert(self, pointer, hc.dir)

    def test_an_existing_message_or_record_for_the_id_is_refused_and_kept(self):
        hc = self.hc
        hc.store().mkdir(parents=True)
        for existing in ('.msg', '.req'):
            with self.subTest(existing):
                req_id = f't5-{existing[1:]}0000'
                old = hc.store() / f'{req_id}{existing}'
                old.write_text('{"delivery": "sent"}\n', encoding='utf-8')    # the stub reads every record
                with mock.patch.object(peer, 'next_id', return_value=req_id):
                    rc, out, err = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'the new task')
                self.assertEqual((rc, out), (peer.REFUSED, ''))
                self.assertIn('nothing sent, nothing replaced', err)
                self.assertEqual(hc.calls('send'), [])
                self.assertEqual(old.read_text(encoding='utf-8'), '{"delivery": "sent"}\n')
                self.assertEqual(sorted(p.name for p in hc.store().iterdir() if p.name.startswith(req_id)),
                                 [old.name])

    def test_a_done_target_is_sent_without_a_status_guard(self):
        self.hc.peers(target=get(PEER, str(self.hc.w), status='done'))
        self.hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        argv = self.hc.calls('send')[0]['argv']
        self.assertEqual(argv[:argv.index('--')], ['send', '--json'])

    def test_a_working_target_needs_while_working(self):
        hc = self.hc
        hc.peers(target=get(PEER, str(hc.w), status='working'))
        rc, _, err = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        self.assertEqual(rc, peer.REFUSED)
        self.assertIn('--while-working', err)
        self.assertEqual(hc.calls('send'), [])
        self.assertFalse(hc.store().exists() and any(hc.store().iterdir()))
        rc, _, _ = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q', '--while-working')
        self.assertEqual(rc, 0)
        argv = hc.calls('send')[0]['argv']
        self.assertEqual(argv[:argv.index('--')], ['send', '--json', '--if', 'working'])

    def test_a_blocked_or_errored_target_is_refused(self):
        for status in ('blocked', 'error'):
            with self.subTest(status):
                self.hc.peers(target=get(PEER, str(self.hc.w), status=status))
                rc, _, _ = self.hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q', '--while-working')
                self.assertEqual(rc, peer.REFUSED)
                self.assertEqual(self.hc.calls('send'), [])

    def test_a_terminal_whose_program_is_no_longer_an_agent_is_no_peer(self):
        # hc ls still says codex; hc get says a plain program (no status) runs there now
        for target in (get(PEER, str(self.hc.w), 'bash', None), get(PEER, str(self.hc.w), 'bash', 'idle')):
            with self.subTest(target['out']['status']):
                self.hc.peers(target=target)
                rc, _, _ = self.hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
                self.assertEqual(rc, peer.NOT_PEER)
                self.assertEqual(self.hc.calls('send'), [])

    def test_a_name_hc_resolves_to_a_peer_is_not_taken_for_its_address(self):
        self.hc.answer('get:review', get(PEER, str(self.hc.w)))
        rc, _, _ = self.hc.run('send', '--to', 'review', '--files', 'none', '--task', 'q')
        self.assertEqual(rc, peer.NOT_PEER)
        self.assertEqual(self.hc.calls('send'), [])

    def test_success_without_started_is_uncertain_unless_the_target_was_working(self):
        self.hc.answer(f'send:{PEER}', {'rc': 0, 'out': {'started': False, 'status': 'idle', 'turn': 3}})
        rc, out, err = self.hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertEqual(self.hc.record(out.strip())['delivery'], 'uncertain')
        self.hc.peers(target=get(PEER, str(self.hc.w), status='working'))
        self.hc.answer(f'send:{PEER}', {'rc': 0, 'out': {'started': False, 'status': 'working', 'turn': 3}})
        rc, out, err = self.hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q', '--while-working')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.hc.record(out.strip())['delivery'], 'sent')

    def test_a_terminal_in_another_checkout_is_no_peer(self):
        self.hc.peers(target=get(PEER, str(self.hc.other)))
        rc, _, err = self.hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        self.assertEqual(rc, peer.NOT_PEER)
        self.assertIn('not an agent terminal sharing this checkout', err)
        self.assertEqual(self.hc.calls('send'), [])

    def test_uncertain_delivery_is_recorded_and_not_resent(self):
        hc = self.hc
        hc.answer(f'send:{PEER}', {'rc': 1, 'err': {'error': {'kind': 'io', 'message': 'connection reset'}}})
        rc, out, err = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        self.assertEqual(rc, peer.HC_FAILED)
        req_id = out.strip()
        self.assertIn('uncertain', err)
        self.assertEqual(len(hc.calls('send')), 1)
        self.assertEqual(hc.record(req_id)['delivery'], 'uncertain')
        self.assertIn('connection reset', hc.record(req_id)['error'])

    def test_a_send_hc_refused_is_recorded_as_not_sent(self):
        hc = self.hc
        hc.answer(f'send:{PEER}', {'rc': 10, 'err': {'error': {'kind': 'status_mismatch', 'hint': 'nothing was typed',
                                                               'message': 't7 is working, not idle'}}})
        rc, out, err = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        self.assertEqual((rc, out), (peer.REFUSED, ''))
        [name] = [n for n in os.listdir(hc.store()) if n.endswith('.req')]
        self.assertEqual(hc.record(name[:-4])['delivery'], 'not sent')

    def test_dry_run_writes_no_record(self):
        rc, out, _ = self.hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q', '--dry-run')
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith(peer.HEADER['request']))
        self.assertEqual(self.hc.calls('send'), [])
        self.assertFalse(self.hc.store().exists() and any(self.hc.store().iterdir()))

    def test_accepts_a_task_or_delta_quoting_a_bare_files_line(self):
        for extra in (['--task', 'the old request said:\nFILES:\nfix it'],
                      ['--task', 'fix it', '--delta', 'the old request said:\nFILES:']):
            with self.subTest(extra=extra):
                rc, out, _ = self.hc.run('send', '--to', PEER, '--files', 'src/a.c', '--dry-run', *extra)
                self.assertEqual(rc, 0)
                self.assertIn('FILES: src/a.c', out)

    def test_refuses_a_blank_files_line_in_the_header(self):
        rc, _, _ = self.hc.run('send', '--to', PEER, '--task', 'fix it', '--files', 'src/a.c\nFILES:')
        self.assertEqual(rc, peer.REFUSED)
        self.assertEqual(self.hc.calls('send'), [])


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.hc = Hc(self)
        self.id = self.hc.send()
        self.res = self.hc.store() / f'{self.id}.res'

    def reply(self, text, as_=PEER):
        return self.hc.run('reply', '--for', self.id, '--file', self.hc.reply_file(text), as_=as_)

    def test_stores_the_envelope_and_notifies_the_sender(self):
        good = RESULT.format(id=self.id)
        rc, out, err = self.reply('Here it is.\n' + good + '\nthanks\n')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.res.read_text(encoding='utf-8'), good + '\n')
        [notice] = self.hc.calls('send')
        self.assertEqual(notice['argv'][notice['argv'].index('--') + 1], ME)
        self.assertIn(f'read --from {PEER} --for {self.id}', notice['argv'][-1])
        self.assertIn('notified', out)

    def test_the_notice_is_one_inert_line(self):
        """The sender's session can be replaced by a shell after reply checks it, so the notice must run nothing."""
        self.assertEqual(self.reply(RESULT.format(id=self.id))[0], 0)
        assert_inert(self, self.hc.calls('send')[0]['argv'][-1], self.hc.dir)

    def test_every_failed_notice_reports_the_stored_result(self):
        for name, answers, expect in (
                ('sender lookup fails', {f'get:{ME}': [{'rc': 1, 'err': {'error': {'kind': 'io', 'message': 'down'}}}]},
                 'failed: hc get t5 failed: down'),
                ('send fails silently', {f'send:{ME}': [{'rc': 1}]}, 'uncertain: unknown: exit 1'),
                ('send not started', {f'send:{ME}': [{'rc': 0, 'out': {'started': False, 'status': 'idle'}}]},
                 'uncertain: hc send')):
            with self.subTest(name):
                hc = Hc(self)
                req_id = hc.send()
                hc.answers.update(answers)
                path = hc.reply_file(RESULT.format(id=req_id))
                rc, out, err = hc.run('reply', '--for', req_id, '--file', path, as_=PEER)
                self.assertEqual(rc, peer.HC_FAILED)
                self.assertIn(f'result for {req_id} stored; notice to t5 {expect}', err)
                self.assertIn(f'reply --for {req_id} --notify-only', err)
                self.assertNotIn('notified', out)
                self.assertTrue((hc.store() / f'{req_id}.res').exists())

    def test_a_script_path_that_would_not_stay_inert_is_never_typed(self):
        with mock.patch.object(peer, 'SCRIPT', '/x/$(touch pwned)/peer.py'):
            rc, _, err = self.reply(RESULT.format(id=self.id))
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertIn('stored; notice to t5 failed', err)
        self.assertTrue(self.res.exists())
        self.assertEqual(self.hc.calls('send'), [])

    def test_a_sender_that_exits_between_hc_and_proc_is_a_failed_notice(self):
        self.hc.answer(f'get:{ME}', get(ME, str(self.hc.w), 'claude-code', pid=dead_pid()))
        rc, _, err = self.reply(RESULT.format(id=self.id))
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertIn('stored; notice to t5 failed', err)
        self.assertTrue(self.res.exists())
        self.assertEqual(self.hc.calls('send'), [])

    def test_reads_the_envelope_from_stdin(self):
        rc, _, _ = self.hc.run('reply', '--for', self.id, '--file', '-', as_=PEER,
                               stdin=RESULT.format(id=self.id))
        self.assertEqual(rc, 0)
        self.assertTrue(self.res.exists())

    def test_refuses_a_caller_that_is_not_the_target(self):
        self.hc.answer('get:t9', get('t9', str(self.hc.w)))
        rc, _, err = self.reply(RESULT.format(id=self.id), as_='t9')
        self.assertEqual(rc, peer.NOT_PEER)
        self.assertIn('not to this terminal t9', err)
        # the same address now running another process: the session the request went to is gone
        self.hc.answer(f'get:{PEER}', get(PEER, str(self.hc.w), pid=OTHER_PID))
        rc, _, err = self.reply(RESULT.format(id=self.id))
        self.assertEqual(rc, peer.NOT_PEER)
        self.assertFalse(self.res.exists())
        self.assertEqual(self.hc.calls('send'), [])

    def test_refuses_an_unknown_or_unsafe_id(self):
        for req_id in ('t5-00000000', '../t5', '.req'):
            with self.subTest(req_id):
                rc, _, _ = self.hc.run('reply', '--for', req_id, '--notify-only', as_=PEER)
                self.assertEqual(rc, peer.NOT_PEER)

    def test_refuses_anything_but_one_matching_envelope(self):
        good = RESULT.format(id=self.id)
        for name, text in (('another id', RESULT.format(id='t5-other')),
                           ('closing id differs', good.replace(f'END RESULT {self.id}', 'END RESULT t5-other')),
                           ('two envelopes', good + '\n' + good),
                           ('unterminated', good.rsplit('\n', 1)[0]),
                           ('no envelope', 'DONE'),
                           ('indented', '\n'.join('  ' + l for l in good.splitlines())),
                           ('invented status', good.replace('STATUS: DONE', 'STATUS: LGTM')),
                           ('contradictory head', good.replace('STATUS: DONE', 'STATUS: DONE\nSTATUS: BLOCKED'))):
            with self.subTest(name):
                rc, _, err = self.reply(text)
                self.assertEqual(rc, peer.MALFORMED, err)
                self.assertFalse(self.res.exists())
                self.assertEqual(self.hc.calls('send'), [])

    def test_an_identical_retry_is_a_no_op_and_a_conflicting_reply_is_refused(self):
        good = RESULT.format(id=self.id)
        self.assertEqual(self.reply(good)[0], 0)
        self.hc.calls()
        rc, out, _ = self.reply(good)
        self.assertEqual(rc, 0)
        self.assertIn('already stored', out)
        self.assertEqual(self.hc.calls('send'), [])
        rc, _, err = self.reply(good.replace('test_a passes.', 'test_b fails.'))
        self.assertEqual(rc, peer.REFUSED)
        self.assertIn('already stored', err)
        self.assertEqual(self.res.read_text(encoding='utf-8'), good + '\n')
        self.assertEqual(self.hc.calls('send'), [])
        self.assertEqual([p.name for p in self.hc.store().iterdir() if p.name.startswith('.tmp')], [])

    def test_a_failed_notice_is_reported_and_notify_only_resends_it(self):
        hc = self.hc
        hc.answer(f'send:{ME}', {'rc': 9, 'err': {'error': {'kind': 'blocked', 'message': 't5 is blocked'}}})
        rc, _, err = self.reply(RESULT.format(id=self.id))
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertIn('stored; notice to t5 failed: blocked: t5 is blocked', err)
        self.assertIn('--notify-only', err)
        self.assertTrue(self.res.exists())
        hc.calls()
        hc.answer(f'send:{ME}', {'rc': 0, 'out': {'started': True}})
        rc, _, _ = hc.run('reply', '--for', self.id, '--notify-only', as_=PEER)
        self.assertEqual(rc, 0)
        self.assertEqual(len(hc.calls('send')), 1)

    def test_a_notice_to_a_busy_sender_needs_no_new_start(self):
        # the sender sits in read --wait, so it is working: hc cannot show it going working
        self.hc.answer(f'send:{ME}', {'rc': 0, 'out': {'started': False, 'status': 'working', 'turn': 9}})
        rc, out, err = self.reply(RESULT.format(id=self.id))
        self.assertEqual(rc, 0, err)
        self.assertIn('notified', out)
        self.hc.answer(f'get:{ME}', get(ME, str(self.hc.w), 'claude-code', 'idle'))
        self.hc.answer(f'send:{ME}', {'rc': 0, 'out': {'started': False, 'status': 'idle', 'turn': 9}})
        rc, _, err = self.hc.run('reply', '--for', self.id, '--notify-only', as_=PEER)
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertIn('notice to t5 uncertain', err)

    def test_no_notice_into_a_terminal_whose_sender_session_is_gone(self):
        self.hc.answer(f'get:{ME}', get(ME, str(self.hc.w), 'bash', None, pid=OTHER_PID))
        rc, _, err = self.reply(RESULT.format(id=self.id))
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertIn('no longer runs the session', err)
        self.assertTrue(self.res.exists())
        self.assertEqual(self.hc.calls('send'), [])

    def test_notify_only_needs_a_stored_result(self):
        rc, _, _ = self.hc.run('reply', '--for', self.id, '--notify-only', as_=PEER)
        self.assertEqual(rc, peer.REFUSED)
        self.assertEqual(self.hc.calls('send'), [])


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.hc = Hc(self)
        self.id = self.hc.send()
        self.res = self.hc.store() / f'{self.id}.res'

    def read(self, *extra, as_=ME, frm=PEER, req_id=None):
        return self.hc.run('read', '--from', frm, '--for', req_id or self.id, *extra, as_=as_)

    def test_exit_codes(self):
        self.assertEqual(self.read(req_id='t5-00000000')[0], peer.NOT_PEER)
        self.assertEqual(self.read(frm='t9')[0], peer.NOT_PEER)
        self.assertEqual(self.read(as_='t9')[0], peer.NOT_PEER)
        rc, _, err = self.read()
        self.assertEqual(rc, peer.NO_ANSWER)
        self.assertIn('delivery was sent', err)
        self.res.write_text(RESULT.format(id='t5-other'), encoding='utf-8')
        self.assertEqual(self.read()[0], peer.MALFORMED)
        self.res.write_text(RESULT.format(id=self.id) + '\n', encoding='utf-8')
        rc, out, _ = self.read()
        self.assertEqual((rc, out), (0, RESULT.format(id=self.id) + '\n'))
        self.assertEqual(self.hc.calls(), [], 'read without --wait asks hc nothing')

    def wait(self, *answers, sleep_hook=None, ms=600000):
        """read --wait against a clock that each check advances by a second and each sleep by its length."""
        self.hc.answer(f'get:{PEER}', *answers)
        now = [0.0]

        def monotonic():
            now[0] += 1
            return now[0]

        def sleep(s):
            now[0] += s
            if sleep_hook:
                sleep_hook()

        # peer's own clock only: patching the time module would also feed subprocess's waits
        slept = mock.Mock(side_effect=sleep)
        with mock.patch.object(peer, 'time', types.SimpleNamespace(monotonic=monotonic, sleep=slept, time=time.time)):
            rc, out, err = self.read('--wait', str(ms))
        return rc, out, err, slept

    def test_wait_ends_early_with_a_reason(self):
        w = str(self.hc.w)
        for answers, reason, settles in (
                ([get(PEER, w, status='blocked')], 'blocked and needs its human', False),
                ([get(PEER, w, status='error')], 'reports an error', False),
                ([{'rc': 3, 'err': {'error': {'kind': 'not_found', 'message': 'no terminal'}}}], 'has exited', False),
                ([get(PEER, w, state='finished')], 'has exited', False),
                ([get(PEER, w, 'bash', None, pid=OTHER_PID)], 'no longer runs the session', False),
                ([get(PEER, w)], 'never started a turn', True),
                ([get(PEER, w, status='working', turn=4), get(PEER, w, status='done', turn=4)],
                 'done without a result', True)):
            with self.subTest(reason):
                rc, _, err, slept = self.wait(*answers)
                self.assertEqual(rc, peer.NO_ANSWER)
                self.assertIn(reason, err)
                self.assertEqual(slept.called, settles)
                if settles:
                    self.assertGreaterEqual(sum(c.args[0] for c in slept.call_args_list), peer.SETTLE_S - 2)
                self.hc.calls()

    def test_wait_polls_a_working_target_with_bounded_sleeps_and_lapses(self):
        rc, _, err, slept = self.wait(get(PEER, str(self.hc.w), status='working', turn=4), ms=20000)
        self.assertEqual(rc, peer.NO_ANSWER)
        self.assertIn('after 20000 ms', err)
        self.assertGreater(slept.call_count, 1)
        self.assertTrue(all(c.args[0] <= peer.POLL_S for c in slept.call_args_list))
        self.assertEqual(self.hc.calls('wait'), [])

    def test_wait_returns_a_result_stored_as_the_target_exits(self):
        # the target replies, then exits before this look at hc: the file is checked after hc answers
        good = RESULT.format(id=self.id) + '\n'
        rc, out, _, _ = self.wait({'rc': 3, 'write': [str(self.res), good],
                                   'err': {'error': {'kind': 'not_found', 'message': 'no terminal'}}})
        self.assertEqual((rc, out), (0, good))

    def test_wait_returns_a_stored_result_without_asking_hc(self):
        good = RESULT.format(id=self.id) + '\n'
        self.res.write_text(good, encoding='utf-8')
        rc, out, _, _ = self.wait({'rc': 1, 'err': {'error': {'kind': 'io', 'message': 'server down'}}})
        self.assertEqual((rc, out), (0, good))
        self.assertEqual(self.hc.calls(), [])

    def test_wait_returns_a_result_stored_while_hc_fails(self):
        good = RESULT.format(id=self.id) + '\n'
        rc, out, _, _ = self.wait({'rc': 1, 'write': [str(self.res), good],
                                   'err': {'error': {'kind': 'io', 'message': 'server down'}}})
        self.assertEqual((rc, out), (0, good))

    def test_wait_keeps_hc_failures_distinct(self):
        rc, _, err, _ = self.wait({'rc': 1, 'err': {'error': {'kind': 'io', 'message': 'server down'}}})
        self.assertEqual(rc, peer.HC_FAILED)
        self.assertIn('server down', err)

    def test_wait_gives_each_lookup_only_what_is_left_and_none_past_the_deadline(self):
        # the fake clock moves a second per reading: a 1.5 s wait leaves 0.5 s for its one lookup
        for ms, timeouts in ((1500, [0.5]), (1, [])):
            with self.subTest(ms=ms), mock.patch.object(peer.hc, 'get', wraps=peer.hc.get) as g:
                rc, _, err, _ = self.wait(get(PEER, str(self.hc.w), status='working', turn=4), ms=ms)
                self.assertEqual(rc, peer.NO_ANSWER)
                self.assertIn(f'after {ms} ms', err)
                self.assertEqual([round(c.kwargs['timeout'], 3) for c in g.call_args_list], timeouts)
                self.hc.calls()

    def test_wait_lapses_when_hc_does_not_answer_past_the_deadline(self):
        with mock.patch.object(peer.hc, 'get', side_effect=peer.hc.Error('hc get: no answer in 0.5 s')):
            rc, _, err, _ = self.wait(ms=1500)
        self.assertEqual(rc, peer.NO_ANSWER)
        self.assertIn('hc did not answer', err)

    def test_wait_reads_a_target_gone_between_hc_and_proc_as_exited(self):
        rc, _, err, _ = self.wait(get(PEER, str(self.hc.w), status='working', pid=dead_pid()))
        self.assertEqual(rc, peer.NO_ANSWER)
        self.assertIn('no longer runs the session', err)

    def test_wait_rechecks_the_file_on_every_wake(self):
        good = RESULT.format(id=self.id) + '\n'
        rc, out, _, slept = self.wait(get(PEER, str(self.hc.w)),
                                      sleep_hook=lambda: self.res.write_text(good, encoding='utf-8'))
        self.assertEqual((rc, out), (0, good))
        self.assertEqual(slept.call_count, 1)

    def test_wait_on_a_request_never_sent_returns_at_once(self):
        record = self.hc.record(self.id)
        record.update(delivery='not sent', error='status_mismatch: t7 is working, not idle')
        (self.hc.store() / f'{self.id}.req').write_text(json.dumps(record), encoding='utf-8')
        rc, _, err, _ = self.wait(get(PEER, str(self.hc.w)))
        self.assertEqual(rc, peer.NO_ANSWER)
        self.assertIn('never sent', err)
        self.assertEqual(self.hc.calls(), [])


class EnvelopeTest(unittest.TestCase):
    def test_the_one_matching_envelope_is_extracted(self):
        body, why = peer.the_envelope('noise\n' + RESULT.format(id='t5-1') + '\nmore', 't5-1')
        self.assertIsNone(why)
        self.assertEqual(body, RESULT.format(id='t5-1') + '\n')

    def test_field_lines_quoted_in_the_answer_are_prose(self):
        text = RESULT.format(id='t5-1').replace(
            'test_a passes.', 'test_a passes. The old reply said:\nFOR: t5-0\nSTATUS: BLOCKED\nFILES:')
        body, why = peer.the_envelope(text, 't5-1')
        self.assertIsNone(why)
        self.assertEqual(peer.check('result', body), [])

    def test_a_padded_or_repeated_for_is_refused(self):
        good = RESULT.format(id='t5-1')
        for for_lines in ('FOR: t5-1\nFOR: t5-0', 'FOR: t5-0\nFOR: t5-1', 'FOR: t5-1 extra', 'FOR: t5-1\nFOR:'):
            with self.subTest(for_lines):
                body, why = peer.the_envelope(good.replace('FOR: t5-1', for_lines), 't5-1')
                self.assertIsNone(body)
                self.assertTrue(why)


class CheckTest(unittest.TestCase):
    def test_a_well_formed_result_passes(self):
        self.assertEqual(peer.check('result', RESULT.format(id='x')), [])

    def test_missing_correlation_is_caught(self):
        text = RESULT.format(id='x').replace('FOR: x\n', '')
        self.assertIn('missing required field FOR', peer.check('result', text))

    def test_missing_touched_files_is_caught(self):
        text = RESULT.format(id='x').replace('FILES: src/a.c\n', '')
        self.assertIn('missing required field FILES', peer.check('result', text))

    def test_blank_files_is_caught_in_both_kinds(self):
        text = RESULT.format(id='x').replace('FILES: src/a.c', 'FILES:')
        self.assertTrue(any('FILES is blank' in p for p in peer.check('result', text)))
        body = peer.build_request('t5-1', 'claude-code, t5', '', 'task')
        self.assertTrue(any('FILES is blank' in p for p in peer.check('request', body)))

    def test_a_blank_files_line_repeated_in_a_request_header_is_caught(self):
        body = peer.build_request('t5-1', 'claude-code, t5', 'src/a.c', 'task').replace(
            'FILES: src/a.c', 'FILES: src/a.c\nFILES:')
        self.assertIn('FILES is blank; list the paths, or "none"', peer.check('request', body))

    def test_an_invented_status_is_caught(self):
        text = RESULT.format(id='x').replace('STATUS: DONE', 'STATUS: LGTM')
        self.assertTrue(any('not one of' in p for p in peer.check('result', text)))

    def test_a_request_with_a_multiline_delta_passes(self):
        body = peer.build_request('t5-1', 'claude-code, t5', 'src/a.c', 'add a bounds check',
                                  'rebased on main\nFILES: was src/b.c last round\nnothing else')
        self.assertEqual(peer.check('request', body), [])

    def test_a_request_carries_files_task_and_how_to_answer(self):
        body = peer.build_request('t5-1', 'claude-code, t5', 'src/a.c', 'add a bounds check')
        self.assertIn('FILES: src/a.c', body)
        self.assertIn('TASK: add a bounds check', body)
        self.assertIn('DELTA: none', body)
        self.assertIn(f'python3 {shlex.quote(peer.SCRIPT)} reply --for t5-1 --file', body)
        self.assertTrue(body.endswith('\nEND REQUEST t5-1'))
        self.assertEqual(peer.check('request', body), [])

    def test_check_requires_the_terminator(self):
        text = RESULT.format(id='x').replace('END RESULT x', '')
        self.assertTrue(any('END RESULT' in p for p in peer.check('result', text)))

    def test_check_catches_a_terminator_for_another_envelope(self):
        text = RESULT.format(id='x').replace('END RESULT x', 'END RESULT y')
        self.assertTrue(any('names y' in p for p in peer.check('result', text)))

    def test_contradictory_head_fields_are_caught(self):
        good = RESULT.format(id='x')
        twice = peer.check('result', good.replace('FOR: x', 'FOR: x\nFOR: y'))
        self.assertTrue(any('FOR as both' in p for p in twice))
        twice = peer.check('result', good.replace('STATUS: DONE', 'STATUS: DONE\nSTATUS: BLOCKED'))
        self.assertTrue(any('STATUS as both' in p for p in twice))

    def test_a_blank_correlation_id_is_caught_in_both_kinds(self):
        text = RESULT.format(id='x').replace('FOR: x', 'FOR:')
        self.assertTrue(any('FOR is blank' in p for p in peer.check('result', text)))
        body = peer.build_request('t5-1', 'claude-code, t5', 'src/a.c', 'task').replace('ID: t5-1', 'ID:')
        self.assertTrue(any('ID is blank' in p for p in peer.check('request', body)))


class IdentityTest(unittest.TestCase):
    def test_ids_do_not_collide_within_one_second(self):
        self.assertNotEqual(peer.next_id('t5'), peer.next_id('t5'))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'ancestry is checked through /proc')
    def test_a_terminal_whose_foreground_does_not_run_this_process_is_not_ours(self):
        """HC_TERMINAL_ID is a hint any process can set: send and reply need its foreground among our ancestors."""
        hc = Hc(self)
        req_id = hc.send()
        stranger = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        hc.answer(f'get:{ME}', get(ME, str(hc.w), 'claude-code', 'working', pid=stranger.pid))
        rc, _, err = hc.run('send', '--to', PEER, '--files', 'none', '--task', 'q')
        self.assertEqual(rc, peer.NOT_PEER)
        self.assertIn('does not run this one', err)
        hc.answer(f'get:{PEER}', get(PEER, str(hc.w), pid=stranger.pid))
        rc, _, err = hc.run('reply', '--for', req_id, '--file', hc.reply_file(RESULT.format(id=req_id)), as_=PEER)
        self.assertEqual(rc, peer.NOT_PEER)
        self.assertFalse((hc.store() / f'{req_id}.res').exists())
        self.assertEqual(hc.calls('send'), [])

    def test_a_record_storing_its_start_time_as_a_string_still_loads(self):
        hc = Hc(self)
        req_id = hc.send()
        path = hc.store() / f'{req_id}.req'
        record = json.loads(path.read_text(encoding='utf-8'))
        for side in ('sender', 'target'):
            if record[side]['start'] is not None:
                record[side]['start'] = str(record[side]['start'])
        path.write_text(json.dumps(record), encoding='utf-8')
        rc, _, err = hc.run('reply', '--for', req_id, '--file', hc.reply_file(RESULT.format(id=req_id)), as_=PEER)
        self.assertEqual(rc, 0, err)
        record['target']['start'] = 'x'
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(hc.run('read', '--from', PEER, '--for', req_id)[0], peer.NOT_PEER)


class StoreTest(unittest.TestCase):
    def test_publication_retries_a_windows_sharing_violation_and_never_replaces(self):
        directory = tmpdir(self)
        path, real_link, tries = str(directory / 'x.res'), os.link, []

        def link(src, dst):
            tries.append(dst)
            if len(tries) == 1:
                raise PermissionError(13, 'in use')
            real_link(src, dst)

        with mock.patch.object(peer.windows, 'os', types.SimpleNamespace(name='nt')), \
             mock.patch.object(peer.windows.time, 'sleep'), mock.patch.object(peer.os, 'link', side_effect=link):
            self.assertEqual(peer.publish(path, 'one\n'), 'stored')
        self.assertEqual(tries, [path, path])
        self.assertEqual(peer.publish(path, 'two\n'), 'conflict')
        self.assertEqual(Path(path).read_text(encoding='utf-8'), 'one\n')
        self.assertEqual(os.listdir(directory), ['x.res'])


class SkillDocTest(unittest.TestCase):
    body = ' '.join((ROOT / 'skills' / 'herdr-peer' / 'SKILL.md').read_text(encoding='utf-8').split())

    def test_the_commit_recipe_puts_options_before_the_pathspec(self):
        """An option after `--` is read as a path, so the recipe must carry -m before it."""
        self.assertIn('`git commit --only -m "<subject>" -- <same paths>`', self.body)
        self.assertNotIn('git commit --only -- <same paths>', self.body)

    def test_it_tells_the_receiving_peer_how_to_answer(self):
        self.assertIn('reply --for <id> --file <file>', self.body)
        self.assertIn('--notify-only', self.body)


if __name__ == '__main__':
    unittest.main()
