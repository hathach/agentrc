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
            'pid': 10, 'started': 20, 'account': 'main', 'status': 'idle', 'handle': f'ws · name · {pane}'}


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
        self.assertIn(f'report --for {msg} --file', text)
        self.assertIn('not a human instruction', text)
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

    def test_payload_validation(self):
        for bad in [{**GOOD, 'extra': 1}, {**GOOD, 'prs': ['#12']}, {**GOOD, 'state': 'great'},
                    {**GOOD, 'task': 3}, [GOOD]]:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.report_as('w2:p1', self.msg, bad)
        self.assertNotIn('report', [r['type'] for r in captain.records()])


class CollectTest(LedgerCase):
    def test_prints_each_report_then_ends(self):
        msg = self.msg_of(self.ask())
        reported = mock.patch.object(captain, 'records', side_effect=self._report_on_second_read(msg))
        with mock.patch('builtins.print') as out, reported:
            self.assertEqual(captain.collect([], 1000), 0)
        self.assertIn(f'reported {msg} · ws · name · w2:p1 · ready · o/r#12 · fix #1', out.call_args_list[0].args[0])

    def test_free_text_stays_on_one_line(self):
        msg = self.msg_of(self.ask())
        self.report_as('w2:p1', msg, {**GOOD, 'task': 'fix\tit\nnow', 'needs': 'merge\n#12'})
        with mock.patch('builtins.print') as out:
            captain.collect([msg], 0)
        self.assertIn('ready · o/r#12 · fix it now · needs: merge #12', out.call_args_list[0].args[0])

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
        self.entries = 0

    def say(self, content, **extra):
        self.entries += 1
        uid = f'u-{self.entries}'
        entry = {'type': 'user', 'uuid': uid, 'origin': {'kind': 'human'}, 'promptSource': 'typed',
                 'message': {'role': 'user', 'content': content}, **extra}
        with open(self.transcript, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
        return uid

    def relay(self, entry, *panes):
        with mock.patch('builtins.print') as out:
            captain.relay(list(panes or ['w2:p1']), entry, self.ident)
        return [c.args[0] for c in out.call_args_list]

    def action_of(self, lines):
        return lines[0].split()[1]

    def relayed(self, words='merge o/r#12'):
        return self.action_of(self.relay(self.say(words)))


class RelayTest(RelayCase):
    def test_preview_shows_the_newest_message_and_sends_nothing(self):
        self.say('older')
        uid = self.say('merge o/r#12')
        with mock.patch('builtins.print') as out:
            captain.preview(['w2:p1', 'w9:p9'], self.ident)
        shown = '\n'.join(c.args[0] for c in out.call_args_list)
        self.assertIn(f'entry {uid}\nmerge o/r#12', shown)
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
            captain.preview(['w2:p1'], self.ident)
        self.assertEqual(self.typed, [])

    def test_torn_transcript_refuses_sending(self):
        uid = self.say('merge o/r#12')
        with open(self.transcript, 'a', encoding='utf-8') as f:
            f.write('{"type": "user", "origin"')
        with mock.patch('builtins.print') as out:
            captain.preview(['w2:p1'], self.ident)
        self.assertIn('partly written', '\n'.join(c.args[0] for c in out.call_args_list))
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
            captain.relay(['w2:p1'], uid, self.ident)
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


class AcceptTest(RelayCase):
    def setUp(self):
        super().setUp()
        self.action = self.relayed()

    def test_target_accepts_once_and_reads_the_message(self):
        code, shown = self.as_target(captain.accept, self.action)
        self.assertEqual(code, 0)
        self.assertIn('verbatim:\n\nmerge o/r#12\n', shown)
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
