import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'captain' / 'scripts'))
import captain  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, tmpdir  # noqa: E402

DATA = ROOT / 'tests' / 'data' / 'captain'
RULE = '─' * 40
DIM = '\x1b[2m'
RESET = '\x1b[0m'


def setUpModule():
    guard_survivors(__name__)


def screen(name):
    return (DATA / f'{name}.ansi').read_text(encoding='utf-8')


class InputStateTest(unittest.TestCase):
    """Captures from live panes (2026-10-23): a human draft is the only thing that may not be typed over."""

    def test_captured_screens(self):
        for name, want in [('claude-empty', 'empty'), ('claude-draft', 'draft')]:
            with self.subTest(name):
                self.assertEqual(captain.input_state(screen(name)), want)

    def test_claude_dim_suggestion_is_empty(self):
        text = f'{RULE}\n❯ {DIM}accept all and relaunch chief{RESET}\n{RULE}\n  ? for shortcuts'
        self.assertEqual(captain.input_state(text), 'empty')

    def test_claude_labelled_top_rule(self):
        text = f'{RULE} alo68 ─\n❯ \n{RULE}\n  status\n  ⏵⏵ bypass permissions on'
        self.assertEqual(captain.input_state(text), 'empty')

    def test_claude_multiline_draft(self):
        text = f'{RULE}\n❯ \n  second line\n{RULE}\n  ? for shortcuts'
        self.assertEqual(captain.input_state(text), 'draft')

    def test_claude_without_input_box_is_unknown(self):
        for text in ['Do you want to proceed?\n❯ 1. Yes\n  2. No', f'{RULE}\n{RULE}', f'{RULE}\n  menu\n{RULE}']:
            with self.subTest(text):
                self.assertEqual(captain.input_state(text), 'unknown')

    def test_braille_draft_is_a_draft(self):
        text = screen('claude-draft').replace('my draft text', '⠓⠑⠇⠇⠕')
        self.assertEqual(captain.input_state(text), 'draft')

    def test_dialog_below_the_input_is_unknown(self):
        text = screen('claude-empty') + '\nPermission required\n❯ 1. Yes\n  2. No'
        self.assertEqual(captain.input_state(text), 'unknown')

    def test_dialog_rows_above_the_hint_are_unknown(self):
        lines = screen('claude-empty').rstrip('\n').split('\n')
        lines[-1:-1] = ['  Permission required', '  Press Enter to approve or Esc to cancel']
        self.assertEqual(captain.input_state('\n'.join(lines)), 'unknown')

    def test_indented_dialog_below_the_input_is_unknown(self):
        text = screen('claude-empty') + '\n  Permission required\n  Press Enter to approve or Esc to cancel'
        self.assertEqual(captain.input_state(text), 'unknown')


def agent(pane, kind='claude', **extra):
    return {'pane_id': pane, 'agent': kind, 'agent_status': 'idle', 'cwd': '/w', 'workspace_id': pane.split(':')[0],
            'terminal_id': f'term-{pane}', **extra}


class SessionsTest(unittest.TestCase):
    def test_excludes_self_and_labels_handles(self):
        found = captain.sessions(
            agents=[agent('w1:p1', name='me'), agent('w2:p1', name='claude-ada-x',
                                                     agent_session={'value': 'uuid-1'}), agent('w2:p2', 'codex'),
                    agent('w2:p3', 'shell')],
            workspaces=[{'workspace_id': 'w2', 'label': 'tinyusb'}], me='w1:p1')
        self.assertEqual([s['handle'] for s in found], ['tinyusb · claude-ada-x · w2:p1'])   # Claude only
        self.assertEqual([s['session'] for s in found], ['uuid-1'])

    def test_latest_herdr_change_first(self):
        found = captain.sessions(
            agents=[agent('w1:p1', state_change_seq=5), agent('w3:p1'), agent('w2:p2', state_change_seq=9),
                    agent('w2:p1', state_change_seq=9)],
            workspaces=[], me='')
        self.assertEqual([(s['pane_id'], s['seq']) for s in found], [('w2:p1', 9), ('w2:p2', 9), ('w1:p1', 5),
                                                                     ('w3:p1', 0)])

    def test_missing_or_relative_cwd_stays_unknown(self):
        for cwd in (None, '', 'rel/dir'):
            with self.subTest(cwd=cwd):
                a = agent('w2:p1')
                a['cwd'] = cwd
                if cwd is None:
                    del a['cwd']
                found = captain.sessions(agents=[a], workspaces=[], me='w1:p1')
                self.assertIsNone(found[0]['cwd'])


class UnpinnedTest(unittest.TestCase):
    def test_reports_each_missing_field(self):
        full = {'kind': 'claude', 'cwd': '/w', 'terminal_id': 't', 'pid': 1, 'started': 2, 'session': 's',
                'account': 'ada'}
        self.assertEqual(captain.unpinned(full), [])
        self.assertEqual(captain.unpinned({**full, 'cwd': None, 'account': None}), ['cwd', 'account'])


class IncarnationTest(unittest.TestCase):
    def _incarnation(self, procs):
        info = {'process_info': {'foreground_processes': procs}}
        with mock.patch.object(captain, 'herdr', return_value=info), \
             mock.patch.object(captain, 'started', return_value=777):
            return captain.incarnation('w2:p1')

    def test_single_agent_process(self):
        self.assertEqual(self._incarnation([{'name': 'claude', 'pid': 42}]), (42, 777))

    def test_none_or_several_cannot_be_pinned(self):
        self.assertIsNone(self._incarnation([{'name': 'bash', 'pid': 1}]))
        self.assertIsNone(self._incarnation([{'name': 'claude', 'pid': 1}, {'name': 'claude', 'pid': 2}]))

    def test_another_agent_kind_in_the_group_cannot_be_pinned(self):
        self.assertIsNone(self._incarnation([{'name': 'codex', 'pid': 1}, {'name': 'claude', 'pid': 2}]))
        self.assertIsNone(self._incarnation([{'name': 'codex', 'pid': 2}]))

    def test_start_time_survives_spaces_in_comm(self):
        stat = '42 (my (odd) name) S ' + ' '.join(str(n) for n in range(4, 30))
        with mock.patch('builtins.open', mock.mock_open(read_data=stat)):
            self.assertEqual(captain.started(42), 22)   # field 22 of proc(5)


class MeTest(unittest.TestCase):
    """A session finds itself from its process ancestry, not HERDR_PANE_ID, which a command need not inherit."""

    def _me(self, chain, pids):
        found = [{**pinned(f'w{n}:p1'), 'pid': pid} for n, pid in enumerate(pids)]
        with mock.patch.object(captain, 'ancestors', return_value=chain), \
             mock.patch.object(captain, 'sessions', return_value=found), \
             mock.patch.object(captain, 'pin', side_effect=lambda s: s):
            return captain.me()

    def test_the_pane_whose_agent_is_an_ancestor(self):
        self.assertEqual(self._me([500, 400, 300], [100, 300])['pane_id'], 'w1:p1')

    def test_none_or_several_refuse(self):
        for pids in ([100, 200], [300, 400]):
            with self.subTest(pids=pids), self.assertRaises(SystemExit):
                self._me([500, 400, 300], pids)

    def test_ancestors_walk_parent_pids(self):
        parents = {500: 400, 400: 300, 300: 1}
        with mock.patch.object(captain, 'stat', side_effect=lambda pid: ['S', str(parents[pid])]):
            self.assertEqual(captain.ancestors(500), [500, 400, 300])


class AccountTest(unittest.TestCase):
    def _account(self, environ):
        with mock.patch('builtins.open', mock.mock_open(read_data=environ)):
            return captain.account(42)

    def test_from_config_dir(self):
        ada = str(Path('~/.claude-ada').expanduser()).encode()
        self.assertEqual(self._account(b'HOME=/h\0CLAUDE_CONFIG_DIR=' + ada + b'\0'), 'ada')

    def test_unset_is_main(self):
        self.assertEqual(self._account(b'HOME=/h\0'), 'main')

    def test_unknown_dir_cannot_pass_for_an_account(self):
        self.assertEqual(self._account(b'CLAUDE_CONFIG_DIR=/elsewhere\0'), 'config:/elsewhere')
        self.assertEqual(self._account(b'CLAUDE_CONFIG_DIR=main\0'), 'config:main')
        self.assertEqual(self._account(b'CLAUDE_CONFIG_DIR=ada\0'), 'config:ada')

    def test_relative_dir_does_not_depend_on_the_reader_s_cwd(self):
        home = Path('~').expanduser()
        for cwd in (home, Path('/')):
            with self.subTest(cwd=cwd), mock.patch('os.getcwd', return_value=str(cwd)):
                self.assertEqual(self._account(b'CLAUDE_CONFIG_DIR=.claude-ada\0'), 'config:.claude-ada')

    def test_explicit_home_config_dir_is_not_main(self):
        """Set, Claude Code keeps its state in <dir>/.claude.json rather than ~/.claude.json: another install."""
        home = str(Path('~/.claude').expanduser())
        self.assertEqual(self._account(f'CLAUDE_CONFIG_DIR={home}\0'.encode()), f'config:{home}')


class HerdrTimeoutTest(unittest.TestCase):
    """A hung herdr fails like any herdr error instead of holding the ledger lock forever."""

    def hangs(self, *args, timeout=None, **kw):
        raise subprocess.TimeoutExpired(args[0], timeout)

    def test_timed_out_call_fails(self):
        with mock.patch.object(captain.subprocess, 'run', side_effect=self.hangs) as run:
            out = captain.run_herdr('agent', 'list')
            self.assertEqual(run.call_args.kwargs['timeout'], captain.HERDR_TIMEOUT_S)
            self.assertNotEqual(out.returncode, 0)
            self.assertEqual(captain.submit('w2:p1', 'hi')[0], 'uncertain')
            self.assertTrue(captain.start('n', 'w2:p1'))
            with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                captain.herdr_text('agent', 'list')

    def test_start_outwaits_herdr_s_own_limit(self):
        with mock.patch.object(captain.subprocess, 'run', side_effect=self.hangs) as run:
            captain.start('n', 'w2:p1')
        self.assertGreater(run.call_args.kwargs['timeout'], captain.START_MS / 1000)


def pinned(pane):
    return {'pane_id': pane, 'terminal_id': f'term-{pane}', 'kind': 'claude', 'cwd': '/w', 'session': f's-{pane}',
            'pid': 10, 'started': 20, 'account': 'main', 'status': 'idle', 'handle': f'ws · name · {pane}',
            'workspace_id': pane.split(':')[0], 'workspace': 'ws'}


CAPTAIN = pinned('w1:p1')
GOOD = {'task': 'fix #1', 'prs': ['o/r#12'], 'state': 'ready', 'evidence': 'tests pass', 'chief': '', 'needs': '',
        'next': 'merge'}


@unittest.skipIf(os.name == 'nt', 'the captain needs POSIX file locks and /proc')
class LedgerCase(unittest.TestCase):
    """A state dir of its own, herdr replaced by a table of panes, and a record of what was typed where."""

    def setUp(self):
        self.state = tmpdir(self)
        self.panes = {'w1:p1': CAPTAIN, 'w2:p1': pinned('w2:p1')}
        self.screens = {}
        self.typed = []
        self.submit_result = ('delivered', '')
        self.me = 'w1:p1'
        self.ident = captain.identity(CAPTAIN)
        for target, attr, value in [
            (captain, 'STATE', str(self.state)),
            (captain, 'POLL_S', 0),
            (captain, 'find', lambda pane: dict(self.panes[pane]) if pane in self.panes else None),
            (captain, 'read_screen', lambda pane: self.screens.get(pane, screen('claude-empty'))),
            (captain, 'submit', self._submit),
            (captain, 'me', lambda: captain.identity(self.panes[self.me])),
            (captain, 'config_dir', lambda account: str(self.state / 'config')),   # never the real ~/.claude
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.enroll_captain()

    def _submit(self, pane, text):
        self.typed.append((pane, text))
        return self.submit_result

    def enroll_captain(self):
        with mock.patch('builtins.print'):
            captain.enroll(False)

    def ask(self, pane='w2:p1'):
        return captain.ask(pane, self.ident)

    def msg_of(self, line):
        return line.split()[1]

    def as_target(self, fn, *args, pane='w2:p1'):
        """fn run as the session in pane: its return code and what it printed."""
        self.me = pane
        try:
            with mock.patch('builtins.print') as out:
                code = fn(*args)
            return code, '\n'.join(str(c.args[0]) for c in out.call_args_list)
        finally:
            self.me = 'w1:p1'

    def report_as(self, pane, msg, payload=GOOD):
        f = self.state / 'r.json'
        f.write_text(json.dumps(payload), encoding='utf-8')
        return self.as_target(captain.report, msg, str(f), pane=pane)[0]

    def replace_captain(self, pane='w4:p1'):
        captain.write_json('enrolled.json', captain.identity(pinned(pane)))

    def count(self, kind):
        return [r['type'] for r in captain.records()].count(kind)


class LedgerFileTest(LedgerCase):
    def test_torn_last_line_is_corrupt_unless_lenient(self):
        captain.append({'type': 'cancel', 'msg': 'm-1'})
        with open(self.state / 'ledger.jsonl', 'ab') as f:
            f.write(b'{"type": "can')
        with self.assertRaises(captain.Corrupt):
            captain.records()
        self.assertEqual([r['msg'] for r in captain.records(lenient=True)], ['m-1'])

    def test_garbled_line_is_corrupt_even_when_lenient(self):
        (self.state / 'ledger.jsonl').write_bytes(b'not json\n')
        with self.assertRaises(captain.Corrupt):
            captain.records(lenient=True)

    def test_unknown_record_is_corrupt(self):
        (self.state / 'ledger.jsonl').write_bytes(b'{"type": "requast", "msg": "m-1"}\n')
        with self.assertRaises(captain.Corrupt):
            captain.records()
        (self.state / 'ledger.jsonl').write_bytes(b'{"type": "request", "msg": "m-1"}\n')   # fields missing
        with self.assertRaises(captain.Corrupt):
            captain.records()

    def test_request_with_a_broken_identity_is_corrupt(self):
        self.ask()
        recs = (self.state / 'ledger.jsonl').read_text(encoding='utf-8').splitlines()
        broken = [('pane_id', None), ('pane_id', ''), ('pid', '10'), ('started', 0), ('cwd', 'rel'), ('kind', 'gemini')]
        for field, value in broken:
            with self.subTest(field=field):
                request = json.loads(recs[0])
                request['target'][field] = value
                (self.state / 'ledger.jsonl').write_text(json.dumps(request) + '\n', encoding='utf-8')
                with self.assertRaises(captain.Corrupt):
                    self.ask()
        self.assertEqual(len(self.typed), 1)

    def test_malformed_report_does_not_close_its_request(self):
        msg = self.msg_of(self.ask())
        captain.append({'type': 'report', 'msg': msg, 'report': None})
        with self.assertRaises(captain.Corrupt):
            self.ask()
        self.assertEqual(len(self.typed), 1)

    def test_short_writes_complete_the_line(self):
        real = os.write
        with mock.patch.object(os, 'write', side_effect=lambda fd, data: real(fd, data[:7])):
            captain.append({'type': 'cancel', 'msg': 'm-1'})
        self.assertEqual(captain.records()[-1]['msg'], 'm-1')

    def test_failed_write_types_nothing(self):
        with mock.patch.object(os, 'write', side_effect=OSError(28, 'No space left on device')), \
             self.assertRaises(OSError):
            self.ask()
        self.assertEqual(self.typed, [])

    def test_corrupt_ledger_stops_ask(self):
        with open(self.state / 'ledger.jsonl', 'ab') as f:
            f.write(b'{"type"')
        with self.assertRaises(captain.Corrupt):
            self.ask()
        self.assertEqual(self.typed, [])


class AskTest(LedgerCase):
    def test_records_then_types_the_request(self):
        line = self.ask()
        msg = self.msg_of(line)
        self.assertTrue(line.endswith('delivered'))
        types = [r['type'] for r in captain.records()]
        self.assertEqual(types, ['request', 'delivered'])
        pane, text = self.typed[0]
        self.assertEqual(pane, 'w2:p1')
        self.assertEqual(text, f'STATUS REQUEST {msg} - not a human instruction. '
                               f'Run: python3 {captain.INSTALLED} brief --for {msg}')   # one line, the rest in brief
        self.assertNotIn('captain', text.lower().replace(captain.INSTALLED.lower(), ''))   # names no role

    def test_replaced_captain_types_nothing(self):
        self.replace_captain()
        with self.assertRaises(SystemExit):
            self.ask()
        with self.assertRaises(SystemExit):
            captain.cancel('m-any', self.ident)
        self.assertEqual(self.typed, [])

    def test_msg_ids_are_full_uuids_and_unique(self):
        self.panes['w3:p1'] = pinned('w3:p1')
        with mock.patch.object(captain.uuid, 'uuid4', side_effect=[mock.Mock(hex='a' * 32)] * 2
                               + [mock.Mock(hex='b' * 32)]):
            first, second = self.msg_of(self.ask()), self.msg_of(self.ask('w3:p1'))
        self.assertEqual((first, second), ('m-' + 'a' * 32, 'm-' + 'b' * 32))

    def test_one_open_request_per_target(self):
        self.ask()
        self.assertIn('still open', self.ask())
        self.assertEqual(len(self.typed), 1)

    def test_refusals_type_nothing(self):
        cases = {
            'working': ({'status': 'working'}, None),
            'draft': ({}, screen('claude-draft')),
            'not a recognized': ({}, 'plain shell $'),
            'not pinned': ({'pid': None}, None),
        }
        for why, (change, text) in cases.items():
            with self.subTest(why):
                self.panes['w2:p1'] = {**pinned('w2:p1'), **change}
                if text:
                    self.screens['w2:p1'] = text
                self.assertIn(why, self.ask())
                self.screens.clear()
        self.assertIn('no such agent pane', self.ask('w9:p9'))
        self.assertIn('this session', self.ask('w1:p1'))
        self.assertEqual(self.typed, [])

    def test_blocked_submission_closes_the_request(self):
        self.submit_result = ('undelivered', 'agent_blocked')
        self.ask()
        self.assertEqual(captain.open_items(captain.records()), {})

    def test_uncertain_submission_stays_open(self):
        self.submit_result = ('uncertain', 'socket closed')
        self.ask()
        self.assertEqual(len(captain.open_items(captain.records())), 1)


class ReportTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.msg = self.msg_of(self.ask())

    def test_target_reports_once(self):
        self.assertEqual(self.report_as('w2:p1', self.msg), 0)
        self.assertEqual(self.report_as('w2:p1', self.msg), 0)   # the same report again is a no-op
        self.assertEqual(self.count('report'), 1)
        with self.assertRaises(SystemExit):
            self.report_as('w2:p1', self.msg, {**GOOD, 'state': 'blocked'})

    def test_only_the_pinned_target(self):
        self.panes['w3:p1'] = pinned('w3:p1')
        with self.assertRaises(SystemExit):
            self.report_as('w3:p1', self.msg)   # holding the msg id is not enough
        for change in ({'started': 99}, {'session': 's-other'}, {'cwd': '/elsewhere'}):   # restarted, resumed, moved
            with self.subTest(change=change):
                self.panes['w2:p1'] = {**pinned('w2:p1'), **change}
                with self.assertRaises(SystemExit):
                    self.report_as('w2:p1', self.msg)

    def test_a_restart_while_waiting_for_the_lock_is_seen(self):
        real = captain.locked

        @contextmanager
        def restart_then_lock():
            self.panes['w2:p1'] = {**pinned('w2:p1'), 'started': 99}
            with real():
                yield
        with mock.patch.object(captain, 'locked', restart_then_lock), self.assertRaises(SystemExit):
            self.report_as('w2:p1', self.msg)

    def test_cancelled_or_unknown_request(self):
        captain.cancel(self.msg, self.ident)
        with self.assertRaises(SystemExit):
            self.report_as('w2:p1', self.msg)
        with self.assertRaises(SystemExit):
            self.report_as('w2:p1', 'm-nonesuch')

    def test_replaced_captain_strands_its_requests(self):
        self.replace_captain('w5:p1')
        with self.assertRaises(SystemExit):
            self.report_as('w2:p1', self.msg)

    def test_size_limit_counts_bytes(self):
        with self.assertRaises(SystemExit):
            self.report_as('w2:p1', self.msg, {**GOOD, 'task': 'é' * 4500})   # 4500 chars, 9000 bytes

    def test_report_on_stdin(self):
        def from_stdin(data):
            with mock.patch.object(sys, 'stdin', mock.Mock(buffer=io.BytesIO(data))):
                return self.as_target(captain.report, self.msg, '-')[0]
        for bad in (b'{"task":', b'\xff' * 10, json.dumps({**GOOD, 'task': 'x' * 9000}).encode()):
            with self.subTest(bad=bad[:12]), self.assertRaises(SystemExit):
                from_stdin(bad)
        self.assertEqual(from_stdin(json.dumps(GOOD).encode()), 0)
        self.assertEqual(captain.records()[-1]['report'], GOOD)

    def test_payload_validation(self):
        for bad in [{**GOOD, 'extra': 1}, {**GOOD, 'prs': ['#12']}, {**GOOD, 'state': 'great'},
                    {**GOOD, 'task': 3}, [GOOD]]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.report_as('w2:p1', self.msg, bad)
        self.assertNotIn('report', [r['type'] for r in captain.records()])


class UnchangedTest(LedgerCase):
    """The target's transcript as Claude Code writes it: its reporting turn, then whatever came after."""

    def setUp(self):
        super().setUp()
        self.transcript = self.state / 'config' / 'projects' / '-w' / 's-w2:p1.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.msg = self.msg_of(self.ask())
        self.log({'type': 'user', 'promptSource': 'typed', 'origin': {'kind': 'human'},
                  'message': {'content': captain.status_prompt(self.msg)}},
                 {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash'}]}},
                 {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'brief text'}]}},
                 {'type': 'user', 'isMeta': True, 'message': {'content': 'a reminder'}},
                 {'type': 'attachment'},
                 {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Status sent.'}]}})
        self.report_as('w2:p1', self.msg)

    def log(self, *entries, raw=b''):
        with open(self.transcript, 'ab') as f:
            f.write(b''.join(json.dumps(e).encode() + b'\n' for e in entries) + raw)

    def test_nothing_new_is_not_asked(self):
        line = self.ask()
        self.assertIn(f'not asked, nothing new since its report {self.msg}', line)
        self.assertEqual(len(self.typed), 1)
        self.assertIn('delivered', captain.ask('w2:p1', self.ident, force=True))

    def asked_after(self, change):
        """Whether ask types a new request once change is made, the transcript restored after."""
        saved = self.transcript.read_bytes()
        change()
        line = self.ask()
        if line.endswith('delivered'):
            captain.cancel(self.msg_of(line), self.ident)
        self.transcript.write_bytes(saved)
        return line.endswith('delivered')

    def test_any_new_turn_is_asked(self):
        for turn in ({'type': 'user', 'promptSource': 'typed', 'message': {'content': 'go on'}},
                     {'type': 'user', 'promptSource': 'system', 'message': {'content': '<task-notification>'}},
                     {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'summary'}]}},
                     {'type': 'user', 'message': 'an unfamiliar shape'}):
            with self.subTest(turn=turn):
                self.assertTrue(self.asked_after(lambda: self.log(turn)))

    def test_asked_when_it_cannot_be_shown(self):
        cases = {'torn line': lambda: self.log(raw=b'{"type": "user", "mess'),
                 'no transcript': lambda: self.transcript.rename(self.transcript.with_suffix('.old')),
                 'request not in it': lambda: self.transcript.write_bytes(b'{"type": "user"}\n'),
                 'only quoted': lambda: self.transcript.write_bytes(json.dumps(
                     {'type': 'user', 'isCompactSummary': True, 'message': {
                         'content': f'earlier: {captain.status_prompt(self.msg)}'}}).encode() + b'\n')}
        for why, change in cases.items():
            with self.subTest(why):
                self.assertTrue(self.asked_after(change))
        with mock.patch.object(captain, 'config_dir', lambda account: None):
            self.assertTrue(self.asked_after(lambda: None))
        self.assertIn('not asked', self.ask())


class ConfigDirTest(unittest.TestCase):
    def test_config_dirs(self):
        self.assertEqual(captain.config_dir('main'), os.path.expanduser('~/.claude'))
        self.assertEqual(captain.config_dir('ada'), captain.CONFIG_DIRS['ada'])
        absolute = os.path.abspath('/x/y')
        self.assertEqual(captain.config_dir(f'config:{absolute}'), absolute)
        self.assertIsNone(captain.config_dir('config:rel/dir'))


class BriefTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.msg = self.msg_of(self.ask())

    def brief(self, msg=None, pane='w2:p1'):
        return self.as_target(captain.brief, msg or self.msg, pane=pane)

    def test_prints_the_request_and_writes_nothing(self):
        before = captain.records()
        for _ in range(2):
            code, shown = self.brief()
            self.assertEqual(code, 0)
            self.assertIn('from the agent session in pane w1:p1, not a human instruction', shown)
            self.assertIn(f"report --for {self.msg} --file - <<'EOF'", shown)
            self.assertIn('"Status sent."', shown)
        self.assertEqual(captain.records(), before)

    def test_only_the_pinned_target(self):
        self.panes['w3:p1'] = pinned('w3:p1')
        with self.assertRaises(SystemExit):
            self.brief(pane='w3:p1')
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'started': 99}
        with self.assertRaises(SystemExit):
            self.brief()

    def test_closed_missing_or_stranded_request(self):
        with self.assertRaises(SystemExit):
            self.brief('m-nonesuch')
        self.replace_captain('w5:p1')
        with self.assertRaises(SystemExit):
            self.brief()
        self.enroll_captain()
        msg = self.msg   # stranded by the replacement; a new request after re-enrolment is open again
        with self.assertRaises(SystemExit):
            self.brief(msg)
        captain.cancel(msg, self.ident)
        fresh = self.msg_of(self.ask())
        self.assertEqual(self.brief(fresh)[0], 0)
        captain.cancel(fresh, self.ident)
        with self.assertRaises(SystemExit):
            self.brief(fresh)

    def test_corrupt_ledger_decides_nothing(self):
        with open(self.state / 'ledger.jsonl', 'ab') as f:
            f.write(b'{"torn')
        with self.assertRaises(captain.Corrupt):
            self.brief()


class CollectTest(LedgerCase):
    def test_prints_each_report_then_ends(self):
        msg = self.msg_of(self.ask())
        reported = mock.patch.object(captain, 'records', side_effect=self._report_on_second_read(msg))
        with mock.patch('builtins.print') as out, reported:
            self.assertEqual(captain.collect([], 1000), 0)
        self.assertEqual(out.call_args_list[0].args[0], f'reported {msg} · ready')

    def test_the_reported_line_stays_short_whatever_the_report_holds(self):
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'handle': 'h' * 300}
        msg = self.msg_of(self.ask())
        long = {**GOOD, 'task': 'fix\tit\n' * 50, 'prs': [f'o/r#{n}' for n in range(50)],
                'issues': [f'o/r#{n}' for n in range(50)], 'needs': 'merge ✓ é\n' * 300}
        self.report_as('w2:p1', msg, long)
        with mock.patch('builtins.print') as out:
            captain.collect([msg], 0)
        self.assertEqual(out.call_args_list[0].args[0], f'reported {msg} · ready · needs you')
        [row] = captain.reports([msg])
        self.assertEqual(row['needs'], long['needs'])   # the whole text, from reports

    def _report_on_second_read(self, msg):
        """The report lands while collect waits: its first read sees the request still open."""
        real, calls = captain.records, []

        def read(lenient=False):
            calls.append(1)
            if len(calls) == 2:
                self.report_as('w2:p1', msg)
            return real(lenient=lenient)
        return read

    def test_lapses_with_pending_listed(self):
        msg = self.msg_of(self.ask())
        with mock.patch('builtins.print') as out:
            self.assertEqual(captain.collect([msg], 0), captain.NO_ANSWER)
        self.assertEqual(out.call_args_list[-1].args[0], f'pending {msg}')

    def test_cancelled_request_is_reported_as_such(self):
        msg = self.msg_of(self.ask())
        captain.cancel(msg, self.ident)
        with mock.patch('builtins.print') as out:
            self.assertEqual(captain.collect([msg], 0), 0)
        self.assertEqual(out.call_args_list[0].args[0], f'cancelled {msg} · ws · name · w2:p1')

    def test_torn_ledger_is_not_nothing_to_wait_for(self):
        self.ask()
        with open(self.state / 'ledger.jsonl', 'ab') as f:
            f.write(b'{"type"')
        with self.assertRaises(captain.Corrupt):
            captain.collect([], 0)

    def test_unknown_msg(self):
        with self.assertRaises(SystemExit):
            captain.collect(['m-nonesuch'], 0)

    def test_reports_by_msg_keep_a_superseded_report(self):
        first = self.msg_of(self.ask())
        self.report_as('w2:p1', first, {**GOOD, 'state': 'in-progress'})
        second = self.msg_of(self.ask())
        self.report_as('w2:p1', second)
        unanswered = self.msg_of(self.ask())
        rows = captain.reports([first, unanswered, 'm-nonesuch'])
        self.assertEqual((rows[0]['msg'], rows[0]['state']), (first, 'in-progress'))
        self.assertEqual(rows[1:], [{'msg': unanswered, 'missing': 'not answered'},
                                    {'msg': 'm-nonesuch', 'missing': 'no such request'}])

    def test_reports_keeps_the_latest_per_target(self):
        first = self.msg_of(self.ask())
        self.report_as('w2:p1', first, {**GOOD, 'state': 'in-progress'})
        second = self.msg_of(self.ask())
        self.report_as('w2:p1', second)
        rows = captain.reports()
        self.assertEqual([(r['msg'], r['state']) for r in rows], [(second, 'ready')])


class EnrollTest(LedgerCase):
    def test_idempotent(self):
        first = captain.enrolled()
        self.enroll_captain()
        self.assertEqual(captain.enrolled(), first)
        self.assertEqual({k: first[k] for k in captain.IDENTITY}, self.ident)

    def test_reenrolling_after_a_replacement_strands_old_requests(self):
        msg = self.msg_of(self.ask())
        self.replace_captain()   # B took over, then went away
        self.enroll_captain()                                                     # A again: a new generation
        with self.assertRaises(SystemExit):
            self.report_as('w2:p1', msg)

    def test_live_captain_kept_unless_replaced(self):
        self.panes['w4:p1'] = pinned('w4:p1')
        captain.write_json('enrolled.json', captain.identity(self.panes['w4:p1']))
        with self.assertRaises(SystemExit):
            self.enroll_captain()
        with mock.patch('builtins.print'):
            captain.enroll(True)
        self.assertEqual(captain.enrolled()['pane_id'], 'w1:p1')

    def test_gone_captain_is_replaced(self):
        self.replace_captain()   # no longer in any pane
        self.enroll_captain()
        self.assertEqual(captain.enrolled()['pane_id'], 'w1:p1')



def concurrently(fn, n=2):
    """Run fn in n threads, raising what any raised; print is patched once around them, since per-thread patches
    restore each other's."""
    from concurrent.futures import ThreadPoolExecutor
    with mock.patch('builtins.print'), ThreadPoolExecutor(n) as pool:
        futures = [pool.submit(fn) for _ in range(n)]
        for f in futures:
            f.result()


class RelayCase(LedgerCase):
    """The captain's own transcript in a config dir of its own, with the human's messages appended by say()."""

    def setUp(self):
        super().setUp()
        self.panes['w3:p1'] = pinned('w3:p1')
        config = self.state / 'config'
        self.transcript = config / 'projects' / '-w' / f'{CAPTAIN["session"]}.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.transcript.touch()
        env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config),
                                           'CLAUDE_CODE_SESSION_ID': CAPTAIN['session']})
        env.start()
        self.addCleanup(env.stop)
        patcher = mock.patch.object(captain, 'everyone',
                                    lambda: [dict(p) for pane, p in self.panes.items() if pane != self.me])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entries = 0
        self.words = {}

    def say(self, content, **extra):
        self.entries += 1
        uid = f'u-{self.entries}'
        self.words[uid] = captain.plain_text(content)
        entry = {'type': 'user', 'uuid': uid, 'origin': {'kind': 'human'}, 'promptSource': 'typed',
                 'message': {'role': 'user', 'content': content}, **extra}
        with open(self.transcript, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
        return uid

    def digest(self, text, panes):
        """The digest of text to the sessions in panes now."""
        return captain.digest(text, captain.pins({pane: captain.find(pane) for pane in panes}))

    def relay(self, entry, *panes, digest=None):
        """Send entry to panes with digest, by default the digest of entry's words as written to those panes."""
        panes = list(panes or ['w2:p1'])
        digest = digest or self.digest(self.words[entry] or '', panes)
        with mock.patch('builtins.print') as out:
            captain.relay(panes, entry, digest, self.ident)
        return [c.args[0] for c in out.call_args_list]

    def preview(self, *targets):
        with mock.patch('builtins.print') as out:
            captain.preview(captain.resolve(list(targets or ['w2:p1'])), self.ident)
        return '\n'.join(c.args[0] for c in out.call_args_list)

    def action_of(self, lines):
        return lines[0].split()[1]

    def relayed(self, words='merge o/r#12'):
        return self.action_of(self.relay(self.say(words)))


class RelayTest(RelayCase):
    def test_preview_shows_the_newest_message_and_sends_nothing(self):
        self.say('older')
        uid = self.say('merge o/r#12')
        shown = self.preview('w2:p1', 'w9:p9')
        self.assertIn(f'entry {uid} digest {self.digest("merge o/r#12", ["w2:p1", "w9:p9"])}\nmerge o/r#12', shown)
        self.assertIn('to w2:p1: ws · name · w2:p1', shown)
        self.assertIn('to w9:p9: no such agent pane', shown)
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_relays_the_entry_from_the_ledger_not_the_prompt(self):
        lines = self.relay(self.say([{'type': 'text', 'text': 'merge o/r#12'}]))
        action = self.action_of(lines)
        self.assertTrue(lines[0].endswith('delivered'))
        rec = captain.records()[0]
        self.assertEqual((rec['type'], rec['quote'], rec['entry']), ('relay', 'merge o/r#12', 'u-1'))
        pane, text = self.typed[0]
        self.assertEqual(pane, 'w2:p1')
        self.assertIn(f'accept --action {action}', text)
        self.assertNotIn('merge o/r#12', text)
        self.assertNotIn('captain', text.lower().replace(captain.INSTALLED.lower(), ''))   # names no role

    def test_only_the_newest_message_goes(self):
        stale = self.say('merge o/r#12')
        self.say('wait, do not merge')
        with self.assertRaises(SystemExit):
            self.relay(stale)
        self.assertEqual(self.typed, [])

    def test_a_newer_message_that_is_not_text_refuses(self):
        uid = self.say('merge o/r#12')
        self.say([{'type': 'image', 'source': {}}])
        with self.assertRaises(SystemExit):
            self.relay(uid)
        with self.assertRaises(SystemExit), mock.patch('builtins.print'):
            captain.preview(captain.resolve(['w2:p1']), self.ident)
        self.assertEqual(self.typed, [])

    def test_torn_transcript_refuses_sending(self):
        uid = self.say('merge o/r#12')
        with open(self.transcript, 'a', encoding='utf-8') as f:
            f.write('{"type": "user", "origin"')
        self.assertIn('partly written', self.preview())
        with self.assertRaises(SystemExit):
            self.relay(uid)
        self.assertEqual(self.typed, [])

    def test_text_that_is_not_the_human_typing_is_skipped(self):
        uid = self.say('merge o/r#12')
        self.say('hook feedback', isMeta=True)
        self.say('<task-notification>', origin={'kind': 'task'})
        self.say('subagent', isSidechain=True)
        self.say('pasted by a tool', promptSource='sdk')
        self.assertTrue(self.relay(uid)[0].endswith('delivered'))

    def test_another_session_cannot_relay(self):
        uid = self.say('merge o/r#12')
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 's-other'}), self.assertRaises(SystemExit):
            self.relay(uid)
        self.assertEqual(self.typed, [])

    def test_one_message_once_per_target(self):
        uid = self.say('merge o/r#12')
        action = self.action_of(self.relay(uid))
        self.as_target(captain.outcome, action, 'rejected', 'not mine')
        self.assertIn('already', self.relay(uid)[0])
        self.assertTrue(self.relay(uid, 'w3:p1')[0].endswith('delivered'))
        self.assertEqual(len(self.typed), 2)

    def test_one_open_item_per_target(self):
        self.relayed()
        self.assertIn('still open', self.ask())
        self.assertIn('still open', self.relay(self.say('and push'))[0])
        self.assertEqual(len(self.typed), 1)

    def test_refusals_type_nothing(self):
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'status': 'working'}
        self.assertIn('working', self.relay(self.say('merge o/r#12'))[0])
        self.assertEqual(self.typed, [])

    def test_concurrent_relays_of_one_message_send_once(self):
        import threading
        uid = self.say('merge o/r#12')
        start = threading.Barrier(2)

        def run():
            start.wait()
            captain.relay(['w2:p1'], uid, self.digest('merge o/r#12', ['w2:p1']), self.ident)
        concurrently(run)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.count('relay'), 1)

    def test_replay_refused_after_reenrolment_and_target_restart(self):
        uid = self.say('merge o/r#12')
        action = self.action_of(self.relay(uid))
        self.as_target(captain.outcome, action, 'rejected', '')
        captain.write_json('enrolled.json', {**captain.enrolled(), 'generation': 'new'})
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'started': 99, 'session': 's-new'}
        self.assertIn('already', self.relay(uid)[0])
        self.assertEqual(len(self.typed), 1)

    def test_one_transcript_read_for_every_target(self):
        uid = self.say('merge o/r#12')
        real = captain.human_inputs
        reads = []

        def read_then_human_writes(file):
            reads.append(1)
            found = real(file)
            self.say('stop')   # lands after the check: not seen by this send
            return found
        with mock.patch.object(captain, 'human_inputs', read_then_human_writes):
            lines = self.relay(uid, 'w2:p1', 'w3:p1')
        self.assertEqual(len(reads), 1)
        self.assertTrue(all(line.endswith('delivered') for line in lines))

    def test_context_is_the_ledger_record(self):
        self.report_as('w2:p1', self.msg_of(self.ask()))
        self.relayed()
        context = captain.records()[-2]['context']
        self.assertIn('not the human\'s words', context)
        self.assertIn('"ready"', context)
        self.assertIn('o/r#12', context)

    def test_context_ignores_an_earlier_session_in_the_pane(self):
        self.report_as('w2:p1', self.msg_of(self.ask()))
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'started': 99, 'session': 's-new'}
        self.relayed()
        self.assertIn('no status report from this session', captain.records()[-2]['context'])

    def test_delivery_outcomes(self):
        self.submit_result = ('uncertain', 'socket closed')
        uncertain = self.relayed()
        self.assertIn(uncertain, captain.open_items(captain.records()))
        self.assertEqual(self.as_target(captain.accept, uncertain)[0], 0)   # it may have landed
        self.submit_result = ('undelivered', 'agent_blocked')
        blocked = self.action_of(self.relay(self.say('merge'), 'w3:p1'))
        self.assertNotIn(blocked, captain.open_items(captain.records()))
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, blocked, pane='w3:p1')

    def test_malformed_relay_is_corrupt(self):
        self.relayed()
        rec = captain.records()[0]
        for field in ('quote', 'entry', 'context'):
            with self.subTest(field=field):
                (self.state / 'ledger.jsonl').write_text(json.dumps({**rec, field: ''}) + '\n', encoding='utf-8')
                with self.assertRaises(captain.Corrupt):
                    captain.records()
        (self.state / 'ledger.jsonl').write_text(
            json.dumps({'type': 'outcome', 'msg': 'a-1', 'result': 'done', 'note': ''}) + '\n', encoding='utf-8')
        with self.assertRaises(captain.Corrupt):
            captain.records()


class AddressedCase(RelayCase):
    """Two live sessions besides the captain, in workspaces labelled code and docs."""

    def setUp(self):
        super().setUp()
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'workspace': 'code'}
        self.panes['w3:p1'] = {**pinned('w3:p1'), 'workspace': 'docs'}

    def shown(self, words, *panes):
        """What the preview of words to panes shows to send, and its entry and digest."""
        entry = self.say(words)
        lines = self.preview(*panes).split('\n')
        head = next(i for i, line in enumerate(lines) if line.startswith('entry '))
        _, uid, _, digest = lines[head].split()
        self.assertEqual(uid, entry)
        return '\n'.join(lines[head + 1:]), entry, digest

    def refused(self, words, *panes):
        self.say(words)
        with self.assertRaises(SystemExit), mock.patch('builtins.print') as out:
            captain.preview(captain.resolve(list(panes)), self.ident)
        return '\n'.join(c.args[0] for c in out.call_args_list if c.kwargs.get('file') is sys.stderr)


class AddressingTest(AddressedCase):
    """An opening `tell <name>... [to]` naming exactly the relay's targets is not sent; any other opening `tell`
    refuses at preview, and the send sends only what the preview showed."""

    def test_the_addressing_is_stripped_and_the_preview_is_what_is_sent(self):
        text, entry, digest = self.shown('tell code to merge 318', 'w2:p1')
        self.assertEqual(text, 'merge 318')
        self.assertTrue(self.relay(entry, 'w2:p1', digest=digest)[0].endswith('delivered'))
        self.assertEqual(captain.records()[0]['quote'], 'merge 318')

    def test_every_way_of_naming_the_targets(self):
        self.report_as('w2:p1', self.msg_of(self.ask()))
        cases = [('Tell Code: merge 318', ['w2:p1'], 'merge 318'),
                 ('tell w2:p1, merge 318', ['w2:p1'], 'merge 318'),
                 ('tell w2 merge 318', ['w2:p1'], 'merge 318'),
                 ('tell pr:12 to merge it', ['w2:p1'], 'merge it'),
                 ('tell code and docs to rebase\nthen push', ['w2:p1', 'w3:p1'], 'rebase\nthen push'),
                 ('tell code, docs & w3 to rebase', ['w2:p1', 'w3:p1'], 'rebase')]
        for words, panes, want in cases:
            with self.subTest(words):
                self.assertEqual(self.shown(words, *panes)[0], want)

    def test_a_message_without_addressing_is_sent_unchanged(self):
        for words in ('merge 318', 'please tell code to merge 318', '  told code: merge'):
            with self.subTest(words):
                self.assertEqual(self.shown(words, 'w2:p1')[0], words)

    def test_only_an_addressed_message_lists_the_sessions(self):
        listed, everyone = [], captain.everyone
        with mock.patch.object(captain, 'everyone', lambda: listed.append(1) or everyone()):
            self.assertEqual(captain.relay_text('merge 318', ['w2:p1'], []), 'merge 318')
            self.assertEqual(listed, [])
            self.assertEqual(captain.relay_text('tell code to merge 318', ['w2:p1'], []), 'merge 318')
            self.assertEqual(listed, [1])

    def test_addressing_anyone_else_or_ambiguously_refuses_at_preview(self):
        cases = [('tell docs to merge 318', ['w2:p1'], 'not a target'),
                 ('tell code and docs to merge', ['w2:p1'], 'not a target'),
                 ('tell code to merge', ['w2:p1', 'w3:p1'], 'not every target'),
                 ('tell me when CI is green', ['w2:p1'], 'names no live session'),
                 ('tell w1:p1 to merge', ['w2:p1'], 'names no live session'),   # the captain itself
                 ('tell code and then merge', ['w2:p1'], 'may join another name'),
                 ('tell code', ['w2:p1'], 'nothing follows'),
                 ('tell , merge', ['w2:p1'], 'no session name follows'),
                 ('tell code, w9:p1 to merge 318', ['w2:p1'], "'w9:p1' names no live session"),
                 ('tell code, ws:missing to merge 318', ['w2:p1'], "'ws:missing' names no live session")]
        for words, panes, why in cases:
            with self.subTest(words):
                why_not = self.refused(words, *panes)
                self.assertIn(why, why_not)
                self.assertIn('ask the human to restate it', why_not)
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_a_name_for_several_sessions_refuses_even_when_all_are_targets(self):
        self.panes['w4:p1'] = {**pinned('w4:p1'), 'workspace': 'code'}
        self.report_as('w2:p1', self.msg_of(self.ask()))
        self.report_as('w4:p1', self.msg_of(self.ask('w4:p1')))
        for words in ('tell ws:code to merge 318', 'tell code to merge 318', 'tell pr:12 to merge 318'):
            with self.subTest(words):
                self.assertIn('not one session', self.refused(words, 'w2:p1', 'w4:p1'))
        self.assertEqual(self.shown('tell w2:p1 and w4 to merge 318', 'w2:p1', 'w4:p1')[0], 'merge 318')

    def test_an_unresolved_recipient_refuses_the_send_too(self):
        uid = self.say('tell code, w9:p1 to merge 318')
        for digest in (self.digest('w9:p1 to merge 318', ['w2:p1']), self.digest('merge 318', ['w2:p1'])):
            with self.subTest(digest), self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                self.relay(uid, 'w2:p1', digest=digest)
        self.assertEqual(self.typed, [])

    def test_a_rename_between_preview_and_send_is_caught_by_the_digest(self):
        text, entry, digest = self.shown('tell w2, merge to finish', 'w2:p1')
        self.assertEqual(text, 'merge to finish')
        self.panes['w2:p1'] = {**self.panes['w2:p1'], 'workspace': 'merge'}
        self.assertEqual(captain.instruction('tell w2, merge to finish', ['w2:p1'], captain.everyone(), [])[0],
                         'finish')   # the same words now parse differently
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            self.relay(entry, 'w2:p1', digest=digest)
        self.assertEqual((self.typed, [r for r in captain.records() if r['type'] == 'relay']), ([], []))

    def test_the_send_refuses_what_the_preview_did_not_show(self):
        _, entry, digest = self.shown('tell code to merge 318', 'w2:p1')
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            captain.relay(['w2:p1'], entry, None, self.ident)
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            self.relay(entry, 'w2:p1', digest=self.digest('tell code to merge 318', ['w2:p1']))
        self.panes['w2:p1'] = {**self.panes['w2:p1'], 'workspace': 'renamed'}
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            self.relay(entry, 'w2:p1', digest=digest)
        self.assertEqual((self.typed, captain.records()), ([], []))


class BroadcastTest(AddressedCase):
    """`tell all|everyone|every session to|:` is stripped only when the targets are exactly the live sessions, at
    the preview and again at the send."""

    EVERY = ('w2:p1', 'w3:p1')

    def test_tell_all_previews_and_sends_to_every_live_session(self):
        text, entry, digest = self.shown('tell all to continue', *self.EVERY)
        self.assertEqual(text, 'continue')
        lines = self.relay(entry, *self.EVERY, digest=digest)
        self.assertTrue(all(line.endswith('delivered') for line in lines), lines)
        self.assertEqual(sorted(pane for pane, _ in self.typed), list(self.EVERY))
        self.assertEqual([r['quote'] for r in captain.records() if r['type'] == 'relay'], ['continue'] * 2)

    def test_every_spelling_of_all(self):
        cases = [('tell all sessions to continue', 'continue'), ('tell all session to continue', 'continue'),
                 ('Tell ALL: continue', 'continue'), ('tell all:continue', 'continue'),
                 ('tell everyone:continue', 'continue'), ('tell everyone to continue', 'continue'),
                 ('tell every session: continue', 'continue'), ('tell all to rebase\nthen push', 'rebase\nthen push')]
        for words, want in cases:
            with self.subTest(words):
                self.assertEqual(self.shown(words, *self.EVERY)[0], want)

    def test_a_qualified_or_mixed_all_refuses_at_preview(self):
        cases = [('tell all tinyusb sessions to merge', 'not as "all'),
                 ('tell all and code to merge', 'not as "all'),
                 ('tell all, code to merge', 'not one to join'),
                 ('tell all continue', 'not as "all'),
                 ('tell every sessions to merge', 'not as "all'),
                 ('tell code and all to merge', 'not one to join'),
                 ('tell all to   ', 'nothing follows'),
                 ('tell everyone:', 'nothing follows')]
        for words, why in cases:
            with self.subTest(words):
                why_not = self.refused(words, *self.EVERY)
                self.assertIn(why, why_not)
                self.assertIn('ask the human to restate it', why_not)
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_all_joined_to_a_name_refuses(self):
        for words in ('tell code, everyone: continue', 'tell code, all to continue', 'tell code & every: go'):
            with self.subTest(words):
                self.assertIn('not one to join', self.refused(words, 'w2:p1'))
        with self.assertRaises(SystemExit), mock.patch('builtins.print') as out:
            captain.relay(['w2:p1'], self.say('tell code, everyone: continue'), 'any', self.ident)
        self.assertIn('not one to join', out.call_args.args[0])
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_all_joined_to_a_name_refuses_even_as_a_label(self):
        self.panes['w4:p1'] = {**pinned('w4:p1'), 'workspace': 'all'}
        self.assertIn('not one to join', self.refused('tell code and all to merge', 'w2:p1', 'w4:p1'))
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_all_needs_exactly_the_live_sessions(self):
        for panes, why in ((['w2:p1'], 'not targeted: w3:p1'), ([*self.EVERY, 'w9:p9'], 'not live: w9:p9')):
            with self.subTest(panes):
                why_not = self.refused('tell all to continue', *panes)
                self.assertIn(why, why_not)
                self.assertIn('preview again', why_not)
                self.assertNotIn('restate', why_not)
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_no_target_or_no_live_session_is_not_every_session(self):
        live = captain.everyone()
        for panes, sessions in (([], live), (['w2:p1'], []), ([], [])):
            with self.subTest(panes=panes, live=len(sessions)):
                text, why = captain.instruction('tell all to continue', panes, sessions, [])
                self.assertIsNone(text)
                self.assertIsInstance(why, captain.Stale)

    def test_a_session_opening_or_closing_after_the_preview_refuses_the_send(self):
        _, entry, digest = self.shown('tell all to continue', *self.EVERY)
        self.panes['w4:p1'] = pinned('w4:p1')
        for panes in (self.EVERY, (*self.EVERY, 'w4:p1')):
            with self.subTest(panes), self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                self.relay(entry, *panes, digest=digest)
        del self.panes['w4:p1'], self.panes['w3:p1']
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            self.relay(entry, *self.EVERY, digest=digest)
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_a_session_replaced_in_a_pane_after_the_preview_refuses_the_send(self):
        _, entry, digest = self.shown('tell all to continue', *self.EVERY)
        self.panes['w3:p1'] = {**self.panes['w3:p1'], 'pid': 11, 'started': 21, 'session': 's-new'}
        with self.assertRaises(SystemExit), mock.patch('builtins.print') as out:
            captain.relay(list(self.EVERY), entry, digest, self.ident)
        self.assertIn('preview again', out.call_args.args[0])
        self.assertEqual((self.typed, captain.records()), ([], []))

    def test_a_session_replaced_during_the_send_is_refused(self):
        _, entry, digest = self.shown('tell all to continue', *self.EVERY)

        def replace_w3(pane, text):
            self.panes['w3:p1'] = {**self.panes['w3:p1'], 'pid': 11, 'started': 21, 'session': 's-new'}
            return self._submit(pane, text)
        with mock.patch.object(captain, 'submit', replace_w3):
            lines = self.relay(entry, *self.EVERY, digest=digest)
        self.assertTrue(lines[0].endswith('delivered'))
        self.assertIn('not the one the preview showed', lines[1])
        self.assertEqual([pane for pane, _ in self.typed], ['w2:p1'])

    def test_a_name_that_is_also_a_session_refuses(self):
        self.panes['w3:p1'] = {**pinned('w3:p1'), 'workspace': 'all'}
        for words in ('tell all to merge', 'tell all:merge'):
            with self.subTest(words):
                self.assertIn('also names a session', self.refused(words, *self.EVERY))
        self.assertEqual(self.shown('tell ws:all to merge', 'w3:p1')[0], 'merge')

    def test_a_working_session_is_a_target_refused_at_the_send_and_retried(self):
        self.panes['w3:p1'] = {**self.panes['w3:p1'], 'status': 'working'}
        text, entry, digest = self.shown('tell all to continue', *self.EVERY)
        self.assertIn('(working)', self.preview(*self.EVERY))
        first = self.relay(entry, *self.EVERY, digest=digest)
        self.assertTrue(first[0].endswith('delivered'))
        self.assertIn('working', first[1])
        self.panes['w3:p1'] = {**self.panes['w3:p1'], 'status': 'idle'}
        again = self.relay(entry, *reversed(self.EVERY), digest=digest)   # target order does not matter
        self.assertTrue(again[0].endswith('delivered'))
        self.assertIn('already', again[1])
        self.assertEqual([pane for pane, _ in self.typed], ['w2:p1', 'w3:p1'])


class DigestTest(AddressedCase):
    """The preview's digest covers the target panes too, so a send goes only where the preview showed."""

    def test_an_unaddressed_message_goes_only_to_the_previewed_panes(self):
        _, entry, digest = self.shown('merge 318', 'w2:p1')
        with self.assertRaises(SystemExit), mock.patch('builtins.print') as out:
            captain.relay(['w2:p1', 'w3:p1'], entry, digest, self.ident)
        self.assertIn('panes', out.call_args.args[0])
        self.assertEqual(self.typed, [])
        self.assertTrue(self.relay(entry, 'w2:p1', digest=digest)[0].endswith('delivered'))

    def test_a_name_moved_to_another_pane_after_the_preview_refuses(self):
        _, entry, digest = self.shown('tell code to merge 318', 'w2:p1')
        self.panes['w2:p1'] = {**self.panes['w2:p1'], 'workspace': 'other'}
        self.panes['w3:p1'] = {**self.panes['w3:p1'], 'workspace': 'code'}
        with self.assertRaises(SystemExit), mock.patch('builtins.print') as out:
            captain.relay(['w3:p1'], entry, digest, self.ident)
        self.assertIn('not the ones the preview showed', out.call_args.args[0])
        self.assertEqual((self.typed, captain.records()), ([], []))


class SelectorTest(RelayCase):
    """Selectors resolve against the live sessions and each one's latest report, before anything is sent."""

    def setUp(self):
        super().setUp()
        self.panes['w3:p1'] = {**pinned('w3:p1'), 'workspace': 'other'}

    def reported(self, pane, **change):
        self.report_as(pane, self.msg_of(self.ask(pane)), {**GOOD, **change})

    def resolve(self, *args):
        return list(captain.resolve(list(args)))

    def refuses(self, *args):
        with self.assertRaises(SystemExit), mock.patch('sys.stderr') as err:
            captain.resolve(list(args))
        return ''.join(c.args[0] for c in err.write.call_args_list)

    def test_pr_selectors(self):
        self.reported('w2:p1')
        for arg in ('pr:12', 'pr:o:r:12', 'pr:O:R:12'):
            with self.subTest(arg):
                self.assertEqual(self.resolve(arg), ['w2:p1'])
        self.assertIn('no live session matches', self.refuses('pr:112'))
        self.assertIn('no live session matches', self.refuses('pr:p:q:12'))

    def test_a_bare_number_in_two_repositories_refuses(self):
        self.reported('w2:p1')
        self.reported('w3:p1', prs=['p/q#12'])
        why = self.refuses('pr:12')
        self.assertIn('w2:p1', why)
        self.assertIn('w3:p1', why)
        self.assertEqual(self.resolve('pr:o:r:12'), ['w2:p1'])

    def test_issue_selectors_read_only_the_issues_field(self):
        self.reported('w2:p1', issues=['o/r#7'])
        self.reported('w3:p1', task='fix issue 7')   # a legacy report, the number only in prose
        self.assertEqual(self.resolve('issue:7'), ['w2:p1'])
        self.assertIn('no live session', self.refuses('issue:12'))   # a PR number is not an issue

    def test_a_restarted_session_s_old_report_matches_nothing(self):
        self.reported('w2:p1')
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'started': 99}
        self.assertIn('no live session matches', self.refuses('pr:12'))

    def test_workspace_selectors(self):
        self.assertEqual(self.resolve('ws:w3'), ['w3:p1'])
        self.assertEqual(self.resolve('ws:other'), ['w3:p1'])
        self.panes['w4:p1'] = pinned('w4:p1')
        self.assertIn('all match', self.refuses('ws:ws'))   # two workspaces share the label
        self.panes['w3:p2'] = {**pinned('w3:p2'), 'workspace': 'other'}
        self.assertIn('all match', self.refuses('ws:w3'))   # two panes in one workspace

    def test_unsupported_selectors_refuse(self):
        for arg in ('pr:abc', 'pr:o:12', 'issue:', 'ws:'):
            with self.subTest(arg):
                self.assertIn('not a selector', self.refuses(arg))

    def test_aliases_of_one_pane_go_once(self):
        self.reported('w2:p1')
        self.assertEqual(self.resolve('w2:p1', 'pr:12'), ['w2:p1'])

    def test_preview_shows_the_pane_a_selector_names(self):
        self.reported('w2:p1')
        self.say('merge o/r#12')
        with mock.patch('builtins.print') as out:
            captain.preview(captain.resolve(['pr:12']), self.ident)
        self.assertIn('to pr:12: ws · name · w2:p1 (idle)', [c.args[0] for c in out.call_args_list])

    def test_a_relay_never_takes_a_selector(self):
        self.reported('w2:p1')
        uid = self.say('merge o/r#12')
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            self.relay(uid, 'pr:12')
        self.assertEqual([r for r in captain.records() if r['type'] == 'relay'], [])

    def test_one_bad_selector_sends_nothing(self):
        self.reported('w2:p1')
        self.typed.clear()
        self.refuses('pr:12', 'pr:99')
        self.assertEqual(self.typed, [])

    def test_issues_are_validated_like_prs(self):
        self.assertIsNone(captain.validate({**GOOD, 'issues': ['o/r#7']}))
        self.assertIsNone(captain.validate(GOOD))
        self.assertIsNotNone(captain.validate({**GOOD, 'issues': ['#7']}))
        self.assertIsNotNone(captain.validate({**GOOD, 'issues': 'o/r#7'}))


class AcceptTest(RelayCase):
    def setUp(self):
        super().setUp()
        self.action = self.relayed()

    def test_target_accepts_once_and_reads_the_message(self):
        code, shown = self.as_target(captain.accept, self.action)
        self.assertEqual(code, 0)
        self.assertIn('went to:\n\nmerge o/r#12\n', shown)
        self.assertIn(f"outcome --action {self.action} --result <completed|failed|rejected|needs-clarification> "
                      "--note-file - <<'EOF'", shown)
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action)

    def test_only_the_pinned_target(self):
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action, pane='w3:p1')
        self.panes['w2:p1'] = {**pinned('w2:p1'), 'started': 99}
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action)
        self.assertNotIn('accepted', [r['type'] for r in captain.records()])

    def test_acceptance_is_durable_before_the_message_prints(self):
        self.me = 'w2:p1'
        with mock.patch('builtins.print', side_effect=BrokenPipeError), self.assertRaises(BrokenPipeError):
            captain.accept(self.action)
        self.me = 'w1:p1'
        self.assertEqual(captain.records()[-1]['type'], 'accepted')
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action)

    def test_needs_the_sending_captain_live_and_enrolled(self):
        del self.panes['w1:p1']
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action)
        self.panes['w1:p1'] = CAPTAIN
        captain.write_json('enrolled.json', {**captain.enrolled(), 'generation': 'new'})
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action)

    def test_concurrent_accepts_admit_one(self):
        import threading
        self.me = 'w2:p1'
        results, start = [], threading.Barrier(2)

        def run():
            start.wait()
            try:
                results.append(captain.accept(self.action))
            except SystemExit as e:
                results.append(e.code)
        concurrently(run)
        self.assertEqual(sorted(results), [0, 2])
        self.assertEqual(self.count('accepted'), 1)


class OutcomeTest(RelayCase):
    def setUp(self):
        super().setUp()
        self.action = self.relayed()

    def test_completed_needs_acceptance(self):
        with self.assertRaises(SystemExit):
            self.as_target(captain.outcome, self.action, 'completed', '')
        self.as_target(captain.accept, self.action)
        self.assertEqual(self.as_target(captain.outcome, self.action, 'completed', 'merged')[0], 0)
        self.assertNotIn(self.action, captain.open_items(captain.records()))

    def test_rejected_before_acceptance_closes_it(self):
        self.as_target(captain.outcome, self.action, 'needs-clarification', 'which PR?')
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action)

    def test_once_identical_repeat_is_a_no_op(self):
        self.as_target(captain.outcome, self.action, 'rejected', 'no')
        self.assertEqual(self.as_target(captain.outcome, self.action, 'rejected', 'no')[0], 0)
        with self.assertRaises(SystemExit):
            self.as_target(captain.outcome, self.action, 'rejected', 'changed my mind')
        self.assertEqual(self.count('outcome'), 1)

    def test_only_the_pinned_target(self):
        with self.assertRaises(SystemExit):
            self.as_target(captain.outcome, self.action, 'rejected', '', pane='w3:p1')

    def test_recorded_after_the_captain_is_replaced(self):
        self.as_target(captain.accept, self.action)
        self.replace_captain()
        self.assertEqual(self.as_target(captain.outcome, self.action, 'failed', 'conflict')[0], 0)


class WithdrawTest(RelayCase):
    def setUp(self):
        super().setUp()
        self.action = self.relayed()

    def withdraw(self):
        with mock.patch('builtins.print') as out:
            captain.withdraw(self.action, self.ident)
        return out.call_args_list[0].args[0]

    def test_before_acceptance_it_cannot_be_accepted(self):
        self.assertIn('withdrawn', self.withdraw())
        with self.assertRaises(SystemExit):
            self.as_target(captain.accept, self.action)

    def test_after_acceptance_it_stays_open_until_the_outcome(self):
        self.as_target(captain.accept, self.action)
        self.assertIn('nothing stops it', self.withdraw())
        self.withdraw()
        self.assertEqual(self.count('withdraw-requested'), 1)
        self.assertIn(self.action, captain.open_items(captain.records()))
        self.as_target(captain.outcome, self.action, 'completed', 'merged before I saw it')
        self.assertNotIn(self.action, captain.open_items(captain.records()))

    def test_status_requests_are_not_withdrawn(self):
        self.as_target(captain.outcome, self.action, 'rejected', '')
        msg = self.msg_of(self.ask())
        with self.assertRaises(SystemExit), mock.patch('builtins.print'):
            captain.withdraw(msg, self.ident)

    def test_collect_prints_each_event(self):
        self.as_target(captain.accept, self.action)
        self.as_target(captain.outcome, self.action, 'completed', 'merged\nat 1234')
        with mock.patch('builtins.print') as out:
            self.assertEqual(captain.collect([self.action], 0), 0)
        self.assertEqual([c.args[0] for c in out.call_args_list],
                         [f'accepted {self.action} · ws · name · w2:p1',
                          f'completed {self.action} · ws · name · w2:p1 · merged at 1234'])


class OpenTest(LedgerCase):
    """herdr's workspace create and agent start replaced; start() puts the session it would run in the pane table."""

    def setUp(self):
        super().setUp()
        self.checkout = os.path.realpath(tmpdir(self))
        self.created = []
        self.session = {'cwd': self.checkout, 'account': 'ada'}
        self.start_error = None
        for attr, value in [('herdr', self._herdr), ('start', self._start)]:
            patcher = mock.patch.object(captain, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _herdr(self, *args):
        self.created.append(args)
        return {'root_pane': {'pane_id': 'w7:p1'}}

    def _start(self, name, pane):
        if self.session is not None:
            self.panes[pane] = {**pinned(pane), **self.session}
        return self.start_error

    def open(self, checkout=None, account='ada'):
        with mock.patch('builtins.print') as out:
            code = captain.open_session(checkout or self.checkout, account, None, self.ident)
        return code, '\n'.join(c.args[0] for c in out.call_args_list)

    def test_ready_under_the_account_named(self):
        code, line = self.open()
        self.assertEqual(code, 0)
        self.assertIn('w7:p1: ready', line)
        args = self.created[0]
        self.assertIn(f'CLAUDE_CONFIG_DIR={os.path.expanduser("~/.claude-ada")}', args)
        self.assertEqual(args[args.index('--cwd') + 1], self.checkout)
        self.assertEqual(self.typed, [])

    def test_main_leaves_the_config_dir_unset(self):
        self.session['account'] = 'main'
        self.assertEqual(self.open(account='main')[0], 0)
        self.assertFalse(any('CLAUDE_CONFIG_DIR' in str(a) for a in self.created[0]))

    def test_not_ready_is_reported_never_answered(self):
        cases = {
            'did not start': ({}, 'herdr: timed out waiting for readiness', None),
            'no Claude Code session': (None, None, None),
            'account main': ({'account': 'main'}, None, None),
            'runs in /elsewhere': ({'cwd': '/elsewhere'}, None, None),
            'not pinned': ({'pid': None}, None, None),
            'not a recognized input line': ({}, None, 'Do you trust the files in this folder?'),
        }
        for why, (session, error, text) in cases.items():
            with self.subTest(why):
                self.panes.pop('w7:p1', None)
                self.session = None if session is None else {'cwd': self.checkout, 'account': 'ada', **session}
                self.start_error = error
                self.screens = {'w7:p1': text} if text else {}
                code, line = self.open()
                self.assertEqual(code, 2)
                self.assertIn('w7:p1: not ready', line)
                self.assertIn(why, line)
        self.assertEqual(self.typed, [])

    def test_a_failure_after_creation_still_names_the_pane(self):
        with mock.patch.object(captain, 'find', side_effect=SystemExit(5)), mock.patch('builtins.print') as out, \
             self.assertRaises(SystemExit):
            captain.open_session(self.checkout, 'ada', None, self.ident)
        self.assertIn('w7:p1: opened', out.call_args_list[0].args[0])

    def test_a_replacement_waits_for_the_open_to_finish(self):
        real = captain.locked

        @contextmanager
        def replaced_then_lock():
            self.replace_captain()
            with real():
                yield
        with mock.patch.object(captain, 'locked', replaced_then_lock), self.assertRaises(SystemExit):
            self.open()
        self.assertEqual(self.created, [])

    def test_missing_checkout_creates_nothing(self):
        with self.assertRaises(SystemExit):
            self.open(os.path.join(self.checkout, 'nonesuch'))
        with self.assertRaises(SystemExit):
            self.open('relative/dir')
        self.assertEqual(self.created, [])

    def test_only_the_enrolled_captain(self):
        self.replace_captain()
        with self.assertRaises(SystemExit):
            self.open()
        self.assertEqual(self.created, [])

if __name__ == '__main__':
    unittest.main()
