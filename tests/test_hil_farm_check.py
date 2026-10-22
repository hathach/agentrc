"""skills/hil/scripts/hil_farm_check.py is the farm check engine a project's launcher runs with
its profile. A stub profile drives the engine through the real hil_flash provider with a faked
tracked runner (plumbing only), and process-integration tests run the engine in its own
interpreter with a fake flasher on PATH, the real run_tracked and real locks in a temp dir:
handled interruption, abrupt death, a surviving child. No hardware."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the farm check needs POSIX flock, fork and sessions')

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts'


def load(name):
    """By file path, as a project launcher loads it."""
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eng = load('hil_farm_check')
hf = load('hil_flash')
lk = eng.hil_lock
core = eng.report_core
RECHECK = eng.hil_process.recheck   # before Engine stubs it: hil_lock's release predicate is the same instance

BOARD = {'name': 'b', 'flasher': {'name': 'stlink', 'uid': 'P', 'args': ''}}
RESET = 'STM32_Programmer_CLI --connect port=swd sn=P --rst --go'


def patch(test, obj, name, value):
    p = mock.patch.object(obj, name, value)
    p.start()
    test.addCleanup(p.stop)


def setUpModule():
    guard_survivors(__name__)


class StubProfile:
    """A profile whose answers each test sets; its reset goes through hil_flash with ctx.run."""

    def __init__(self, d: Path):
        self.d = d
        self.__file__ = str(d / 'stub_profile.py')
        Path(self.__file__).write_text('# stub profile\n')
        self.boards = [dict(BOARD)]
        self.candidates = ['1-1']
        self.records = []           # the board's lock record as each reset saw it

    def roster(self, host, config, args):
        if config is None and host != 'rig':
            raise LookupError(f'no roster for host {host}')
        path = config or str(self.d / 'roster.json')
        Path(path).write_text(json.dumps({'boards': self.boards}))
        return {'path': path, 'boards': self.boards, 'named_only': [dict(BOARD, name='parked')]}

    def lock_name(self, board):
        return board['name']

    def probe_name(self, board):
        return board['flasher']['name']

    def probe_candidates(self, board):
        return list(self.candidates)

    def reset(self, ctx, board):
        self.records.append(lk.read_record(board['name']))
        return hf.reset_stlink(board['flasher'], timeout=5, run=ctx.run)


class Engine(unittest.TestCase):
    """The engine with a stub profile, real locks in a temp dir and a faked tracked runner."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.d = Path(td.name)
        self.locks = self.d / 'locks'
        self.state = self.d / 'state'
        self.state.mkdir(mode=0o700)
        patch(self, lk, 'BOARD_LOCK_DIR', str(self.locks))
        patch(self, eng, 'say', lambda msg: None)
        patch(self, eng, 'RECHECK_WAIT', 0)
        patch(self, eng.hil_process, 'run_tracked', self.tracked)
        patch(self, eng.hil_process, 'recheck', lambda receipt: self.rechecks.get(receipt['token'], 'unknown'))
        self.p = StubProfile(self.d)
        self.ran = []
        self.outcomes = {}          # command -> (rc, cleanup, stdout) or an exception
        self.rechecks = {}
        self.args = types.SimpleNamespace(verbose=False)

    def tracked(self, cmd, cwd=None, *, timeout, settle, state_dir, **kw):
        self.assertEqual(state_dir, str(self.state))
        name = shlex.join(cmd) if isinstance(cmd, list) else cmd
        self.ran.append(name)
        out = self.outcomes.get(name, (0, 'done', 'ok'))
        if isinstance(out, BaseException):
            out.cleanup, out.receipt = 'done', None
            raise out
        rc, cleanup, stdout = out
        r = subprocess.CompletedProcess(cmd, rc, stdout, None)
        r.cleanup, r.receipt = cleanup, None if cleanup == 'done' else {'token': name, 'lock': '/nonexistent'}
        return r

    def check(self, board=None):
        return eng.check_board_safe(self.p, board or dict(BOARD), self.args, str(self.state))

    def take(self, board='b'):
        fh = lk.take_board_lock(board, 'test')
        lk.release_board_lock(fh)

    def hold(self, reason='hil_test.py', **extra):
        fh = lk.take_board_lock('b', reason, **extra)
        self.addCleanup(fh.close)
        return fh

    def stale(self, **record):
        """A record left on a free flock, as a holder that died leaves it."""
        fh = lk.flock_nb('b')
        lk.write_record(fh, record.pop('reason', 'farm_check'), **record)
        fh.close()

    def doc(self, *rows):
        return eng.farm_document('rig', 'r.json', list(rows), {}, 0)

    # --- the reset ------------------------------------------------------------------------------
    def test_a_free_board_is_reset_under_the_protected_reason_and_released(self):
        row = self.check()
        rec = self.p.records[0]
        self.assertEqual((rec['reason'], rec['protected']), ('farm_check', True))
        self.assertIn('farm_check', lk.PROTECTED_REASONS)
        self.assertEqual((row['status'], row['probe'], row['location'], self.ran), ('ok', '✅ stlink', '1-1', [RESET]))
        self.assertEqual((row['cleanup']['result'], row['cleanup']['released']), ('released', True))
        self.assertEqual(self.doc(row)['rows'][0]['coverage'], 'reset-attempted')
        self.assertEqual(lk.record_state('b')[0], 'empty')
        self.take()

    def test_a_failed_reset_fails_and_still_releases(self):
        self.outcomes[RESET] = (1, 'done', 'Info: x\nError: No STM32 target found!\n')
        row = self.check()
        self.assertEqual((row['status'], row['note'][-1]), ('failed', 'reset rc=1: Error: No STM32 target found!'))
        self.assertEqual(row['operations'][0]['attempts'][0]['returncode'], 1)
        self.assertEqual(row['cleanup']['result'], 'released')
        self.take()

    def test_a_raised_reset_keeps_its_operation_on_the_row(self):
        self.outcomes[RESET] = OSError(2, 'No such file or directory', 'STM32_Programmer_CLI')
        row = self.check()
        self.assertEqual(row['status'], 'failed')
        self.assertTrue(row['note'][-1].startswith('reset: FileNotFoundError'), row['note'])
        self.assertEqual(row['operations'][0]['attempts'][0]['kind'], 'error')
        self.assertEqual(row['cleanup']['result'], 'released')

    def test_a_reset_whose_transcript_shows_no_target_fails_with_its_operation(self):
        # rc 0 without the target's answer: the profile raises, carrying the operation
        def reset(ctx, board):
            op = hf.reset_stlink(board['flasher'], timeout=5, run=ctx.run)
            e = RuntimeError('no target answered')
            e.operation = op
            raise e
        self.p.reset = reset
        row = self.check()
        self.assertEqual((row['status'], row['note'][-1]), ('failed', "reset: RuntimeError('no target answered')"))
        self.assertEqual(len(row['operations']), 1)
        self.take()

    def test_a_reset_that_raised_before_running_anything_fails_and_releases(self):
        def boom(ctx, board):
            raise RuntimeError('no serial device for the probe')
        self.p.reset = boom
        row = self.check()
        self.assertEqual((row['status'], row['operations'], row['cleanup']['result']), ('failed', [], 'released'))
        self.take()

    def test_a_probe_without_a_reset_only_mode_is_unsupported(self):
        self.p.reset = lambda ctx, board: None
        row = self.check()
        doc = self.doc(row)
        self.assertEqual((row['status'], row['note'], self.ran), ('unsupported', ['the probe has no reset-only mode'], []))
        self.assertEqual((doc['rows'][0]['coverage'], eng.exit_status(doc)), ('unsupported', 0))
        self.take()

    def test_a_malformed_reset_result_fails(self):
        def ran_then_none(ctx, board):
            hf.reset_stlink(board['flasher'], timeout=5, run=ctx.run)
        for reset in (lambda ctx, board: 'done', ran_then_none):
            self.p.reset = reset
            row = self.check()
            self.assertEqual(row['status'], 'failed')
            self.assertIn('ValueError', row['note'][-1])
        self.take()

    def test_a_board_without_a_name_is_its_own_error_row(self):
        row = self.check({'flasher': BOARD['flasher']})
        self.assertEqual((row['name'], row['status']), ('?', 'failed'))
        self.assertIn('KeyError', row['note'][0])
        self.assertEqual(self.doc(row)['rows'][0]['coverage'], 'reset-not-attempted')

    # --- probe matching -------------------------------------------------------------------------
    def test_an_ambiguous_probe_lists_its_locations_and_picks_none(self):
        self.p.candidates = ['1-1', '1-2']
        row = self.check()
        self.assertEqual((row['status'], row['probe'], row['location']), ('failed', '⚠ stlink', '1-1, 1-2'))
        self.assertIn('ambiguous', row['note'][0])
        self.assertEqual((self.ran, self.p.records), ([], []))
        self.assertFalse(self.locks.exists())
        self.assertEqual(self.doc(row)['rows'][0]['coverage'], 'reset-not-attempted')

    def test_a_missing_probe_fails_before_the_lock(self):
        self.p.candidates = []
        row = self.check()
        self.assertEqual((row['status'], row['probe'], row['location'], self.ran), ('failed', '❌ stlink', '–', []))
        self.assertFalse(self.locks.exists())

    def test_a_probe_name_that_is_not_a_string_is_the_boards_own_error(self):
        self.p.probe_name = lambda board: None
        row = self.check()
        self.assertEqual((row['status'], self.ran), ('failed', []))
        self.assertIn('ValueError', row['note'][0])

    # --- processes not proven ended -------------------------------------------------------------
    def test_a_reset_not_proven_ended_keeps_the_protected_record(self):
        self.outcomes[RESET] = (0, 'unknown', 'ok')
        row = self.check()
        self.assertEqual((row['status'], row['check_status'], row['cleanup']['result'], row['cleanup']['blocker']),
                         ('blocked', 'ok', 'unresolved', 'stale-record'))
        self.assertEqual(row['cleanup']['receipts'], [{'token': RESET, 'lock': '/nonexistent'}])
        self.assertEqual(self.doc(row)['rows'][0]['coverage'], 'blocked-after')
        rec = lk.read_record('b')
        self.assertEqual((rec['reason'], rec['protected']), ('farm_check', True))
        with self.assertRaises(lk.RecoveryBlocked):
            self.take()

    def test_a_reset_proven_ended_on_recheck_releases(self):
        self.outcomes[RESET] = (0, 'pending', 'ok')
        self.rechecks[RESET] = 'done'
        self.assertEqual(self.check()['status'], 'ok')
        self.take()

    def test_a_command_not_proven_ended_stops_the_next_one(self):
        second = 'STM32_Programmer_CLI --connect port=swd sn=Q --rst --go'

        def reset(ctx, board):
            hf.reset_stlink(board['flasher'], timeout=5, run=ctx.run)
            return hf.reset_stlink(dict(board['flasher'], uid='Q'), timeout=5, run=ctx.run)
        self.p.reset = reset
        self.outcomes[RESET] = (0, 'unknown', 'ok')
        row = self.check()
        self.assertEqual(self.ran, [RESET])
        self.assertNotIn(second, self.ran)
        self.assertEqual((row['status'], row['check_status']), ('blocked', 'failed'))
        self.assertTrue(any('Outstanding' in n for n in row['note']), row['note'])

    # --- incoming locks -------------------------------------------------------------------------
    def test_a_held_board_is_locked_never_waited_on_and_keeps_its_probe(self):
        self.hold()
        t = time.monotonic()
        row = self.check()
        self.assertLess(time.monotonic() - t, 2)
        self.assertEqual((row['status'], row['probe'], row['location'], self.ran), ('locked', '✅ stlink', '1-1', []))
        self.assertIn('hil_test.py', row['note'][-1])
        self.assertEqual(self.doc(row)['rows'][0]['coverage'], 'skipped-locked')

    def test_no_bypass(self):
        self.hold()
        with mock.patch.dict(os.environ, {'HIL_NO_BOARD_LOCK': '1'}):
            self.assertEqual(self.check()['status'], 'locked')
        self.assertEqual(self.ran, [])

    def test_a_stale_protected_record_is_blocked_before_the_reset(self):
        self.stale(protected=True)
        row = self.check()
        self.assertEqual((row['status'], self.ran), ('blocked', []))
        doc = self.doc(row)
        self.assertEqual((doc['rows'][0]['coverage'], eng.exit_status(doc)), ('blocked-before', 4))

    def test_a_live_cleanup_holder_is_blocked_not_locked(self):
        self.hold('hil_reserve', role=lk.CLEANUP_ROLE)
        self.assertEqual(self.check()['status'], 'blocked')

    def test_an_unusable_lock_is_an_error_not_a_holder(self):
        self.locks.mkdir()
        os.mkdir(self.locks / 'b.lock')
        row = self.check()
        self.assertEqual((row['status'], self.ran), ('failed', []))
        self.assertIn('unavailable', row['note'][-1])

    # --- the profile's seams on ctx -------------------------------------------------------------
    def test_a_run_is_labelled_whatever_its_argv_holds(self):
        def tracked(cmd, cwd=None, **kw):
            r = subprocess.CompletedProcess(cmd, 0, '', None)
            r.cleanup, r.receipt = 'done', None
            return r
        patch(self, eng.hil_process, 'run_tracked', tracked)
        ctx = eng.Context(self.args, str(self.state))
        ctx.run([Path('/opt/reset'), '-v'], timeout=5)
        self.assertEqual(ctx.ledger, [{'cmd': '/opt/reset -v', 'cleanup': 'done', 'receipt': None}])

    def test_the_recheck_waits_only_while_a_receipt_can_still_answer(self):
        patch(self, eng, 'RECHECK_WAIT', 5)
        ctx = eng.Context(self.args, str(self.state))
        ctx.ledger.append({'cmd': 'lost', 'cleanup': 'unknown', 'receipt': None})
        t = time.monotonic()
        self.assertEqual(ctx.open_runs(), [{'cmd': 'lost', 'cleanup': 'unknown', 'receipt': None}],
                         'it stays unresolved')
        self.assertLess(time.monotonic() - t, 1, 'no receipt left to recheck, nothing to wait for')

    def test_one_recheck_wait_per_reservation_and_time_never_clears_a_run(self):
        clock, answers = [100.0], []
        patch(self, eng, 'time', types.SimpleNamespace(monotonic=lambda: clock[0],
                                                       sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
        patch(self, eng, 'RECHECK_WAIT', 5)
        patch(self, eng.hil_process, 'recheck', lambda r: answers.pop(0) if answers else 'pending')
        patch(self, eng.hil_process, 'tidy', lambda r: None)
        ctx = eng.Context(self.args, str(self.state))
        ctx.ledger.append({'cmd': 'reset', 'cleanup': 'pending', 'receipt': 'r1'})
        ctx.open_runs()
        self.assertEqual(ctx.recheck_by, 105.0, 'the first open run starts the one wait')
        self.assertAlmostEqual(clock[0], 105.0, delta=0.15)
        waited = clock[0]
        self.assertEqual(ctx.ledger[0]['cleanup'], 'pending', 'time alone never clears it')
        ctx.open_runs()
        self.assertEqual(clock[0], waited, 'a later call rechecks once, without waiting')
        answers.append('done')
        self.assertEqual(ctx.open_runs(), [])
        self.assertEqual((ctx.ledger[0]['cleanup'], ctx.ledger[0]['receipt']), ('done', None), 'a late answer still counts')

    # --- handing a survivor's board to a cleanup holder -----------------------------------------
    def handoff(self, argv=None, popen=None):
        """A reset that leaves a survivor (`pending`) with the holder's start replaced."""
        self.outcomes[RESET] = (0, 'pending', 'ok')
        self.rechecks[RESET] = 'pending'
        patch(self, eng, 'HOLDER_READY', 1)
        if argv is not None:
            patch(self, lk, '_holder_argv', lambda job: argv(job))
        if popen is not None:
            patch(self, eng.subprocess, 'Popen', popen)
        t = time.monotonic()
        row = self.check()
        self.assertLess(time.monotonic() - t, 8, 'every wait of the handoff is bounded')
        doc = self.doc(row)
        self.assertEqual((row['status'], row['coverage'], row['check_status'], eng.exit_status(doc)),
                         ('blocked', 'blocked-after', 'ok', 4))
        self.assertEqual(row['cleanup']['receipts'], [{'token': RESET, 'lock': '/nonexistent'}])
        with self.assertRaises(lk.BoardLocked):   # RecoveryBlocked, or the holder's live flock
            self.take()
        return row

    def assert_failed_handoff(self, row):
        self.assertEqual(row['cleanup']['blocker'], 'stale-record')
        self.assertIn('cleanup holder handoff failed', row['cleanup']['evidence'][-1])
        rec = lk.read_record('b')
        self.assertTrue(rec['protected'])

    def test_a_holder_that_cannot_start_leaves_the_protected_record(self):
        def popen(*a, **kw):
            raise OSError(24, 'Too many open files')
        row = self.handoff(popen=popen)
        self.assert_failed_handoff(row)
        self.assertEqual(lk.read_record('b')['reason'], 'farm_check')
        self.assertNotIn('role', lk.read_record('b'))

    def test_a_stopped_holder_is_not_waited_on(self):
        self.assert_failed_handoff(self.handoff(lambda job: ['sh', '-c', 'kill -STOP $$']))

    def test_a_holder_that_registers_late_is_ended(self):
        real = lk._holder_argv

        def late(job):      # the delay in the holder's own process, so ending it leaves no survivor
            return [sys.executable, '-c', 'import os, sys, time; time.sleep(3); os.execv(sys.argv[1], sys.argv[1:])',
                    *real(job)]
        self.assert_failed_handoff(self.handoff(late))
        self.assertNotIn('role', lk.read_record('b'))

    def test_a_holder_that_dies_after_its_readiness_stays_a_blocker(self):
        fake = ('import json, os, sys\n'
                'job = json.loads(sys.argv[1])\n'
                'start = open(f"/proc/{os.getpid()}/stat").read().rpartition(")")[2].split()[19]\n'
                'os.write(job["hello_fd"], (json.dumps({"pid": os.getpid(), "start": start}) + "\\n").encode())\n'
                'os.read(job["go_fd"], 1)\n')
        row = self.handoff(lambda job: [sys.executable, '-c', fake, json.dumps(job)])
        self.assert_failed_handoff(row)
        self.assertTrue(lk.is_cleanup_holder(lk.read_record('b')), 'a record naming the dead holder')

    def test_pipes_that_cannot_be_made_keep_the_blocked_row_and_leak_nothing(self):
        real = os.pipe
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                calls = []

                def pipe():
                    calls.append(1)
                    if len(calls) == fail_at:
                        raise OSError(24, 'Too many open files')
                    return real()
                before = sorted(os.listdir('/proc/self/fd'))
                with mock.patch.object(eng.os, 'pipe', pipe):
                    row = self.handoff()
                self.assert_failed_handoff(row)
                self.assertIn('Too many open files', row['cleanup']['evidence'][-1])
                self.assertEqual(sorted(os.listdir('/proc/self/fd')), before)
                lk.release_board_lock(lk.flock_nb('b'))   # the test's own stand-in for the recovery
                self.ran.clear()

    def test_an_interrupt_as_the_holder_starts_leaves_no_untracked_holder(self):
        spawned = []
        real = subprocess.Popen

        def popen(*a, **kw):
            p = real(*a, **kw)
            spawned.append(p)
            signal.raise_signal(signal.SIGINT)    # lands before Popen returns to the engine
            return p
        row = self.handoff(lambda job: ['sh', '-c', 'kill -STOP $$'], popen=popen)
        self.assertTrue(row['interrupted'])
        self.assert_failed_handoff(row)
        self.assertIsNotNone(spawned[0].poll(), 'the stopped holder is ended, not left holding the flock')
        self.assertNotIn('role', lk.read_record('b'))

    def test_an_interrupt_during_finalization_keeps_the_blocked_row_and_stops_the_farm(self):
        self.outcomes[RESET] = (0, 'unknown', 'ok')
        self.p.boards = [dict(BOARD), dict(BOARD, name='c')]
        real = lk.write_record

        def write_record(fh, reason, **extra):
            if 'detail' in extra:      # the blocker record, not the take's own
                signal.raise_signal(signal.SIGINT)
            return real(fh, reason, **extra)
        patch(self, eng.hil_lock, 'write_record', write_record)
        rows, aborted = eng.check_farm(self.p, self.p.boards, self.args, state_dir=str(self.state))
        doc = eng.farm_document('rig', 'r.json', rows, {}, 0)
        self.assertEqual((aborted, [r['name'] for r in rows], eng.exit_status(doc, aborted)), (True, ['b'], 4))
        self.assertEqual((rows[0]['status'], rows[0]['cleanup']['blocker'], rows[0]['interrupted']),
                         ('blocked', 'stale-record', True))
        self.assertEqual(lk.read_record('b')['detail'][:30], 'farm check cleanup unresolved:')
        with self.assertRaises(lk.RecoveryBlocked):   # the engine's descriptor is closed
            self.take()

    def interrupt_before_finalization(self):
        real, calls = eng._end_reservation, []

        def end(*a):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt     # before its own interrupt hold is in place
            return real(*a)
        patch(self, eng, '_end_reservation', end)
        return calls

    def test_a_reset_that_exits_the_process_still_finalizes(self):
        def reset(ctx, board):
            hf.reset_stlink(board['flasher'], timeout=5, run=ctx.run)
            raise SystemExit(3)
        self.p.reset = reset
        with self.assertRaises(SystemExit):
            self.check()
        self.take()

    def test_an_interrupt_before_finalization_finalizes_from_the_same_ledger(self):
        calls = self.interrupt_before_finalization()
        row = self.check()
        self.assertEqual(len(calls), 2)
        self.assertEqual((row['status'], row['interrupted'], row['cleanup']['result']), ('ok', True, 'released'))
        self.take()

    def test_an_interrupt_before_finalization_keeps_a_pending_resets_receipt(self):
        self.outcomes[RESET] = (0, 'pending', '')
        self.rechecks[RESET] = 'pending'
        patch(self, eng, 'HOLDER_READY', 2)
        self.interrupt_before_finalization()
        row = self.check()
        receipt = {'token': RESET, 'lock': '/nonexistent'}
        rec = lk.read_record('b')
        if isinstance(rec, dict) and isinstance(rec.get('pid'), int) and rec['pid'] != os.getpid():
            self.addCleanup(Process.kill, rec['pid'])
        self.assertEqual((row['status'], row['interrupted'], row['cleanup']['receipts'], row['cleanup']['blocker']),
                         ('blocked', True, [receipt], 'cleanup-holder'))
        self.assertEqual((rec['receipts'], rec['token']), ([receipt], row['cleanup']['token']))
        with self.assertRaises(lk.BoardLocked):
            self.take()

    def test_a_record_that_cannot_be_cleared_keeps_the_board_blocked(self):
        real = lk.take_board_lock

        def take(*a, **kw):
            fh = real(*a, **kw)
            fh.truncate = mock.Mock(side_effect=OSError(5, 'Input/output error'))
            return fh
        patch(self, eng.hil_lock, 'take_board_lock', take)
        row = self.check()
        doc = self.doc(row)
        self.assertEqual((row['status'], row['check_status'], row['cleanup']['result'], row['cleanup']['released'],
                          eng.exit_status(doc)), ('blocked', 'ok', 'release-failed', False, 4))
        self.assertIn('the record could not be cleared', row['cleanup']['evidence'])
        patch(self, eng.hil_lock, 'take_board_lock', real)
        with self.assertRaises(lk.RecoveryBlocked):
            self.take()

    def test_a_handed_over_holder_keeps_the_board(self):
        row = self.handoff()
        rec = lk.read_record('b')
        self.addCleanup(Process.kill, rec['pid'])
        self.assertEqual((row['cleanup']['blocker'], row['cleanup']['token']), ('cleanup-holder', rec['token']))
        self.assertTrue(lk.is_cleanup_holder(rec))
        self.assertTrue(lk._holds_flock(rec['pid'], lk.lock_path('b')))
        self.assertTrue(rec['receipts'])         # its release predicate
        patch(self, eng.hil_process, 'recheck', RECHECK)
        signals = []
        with mock.patch.object(lk.signal, 'pidfd_send_signal', lambda fd, sig: signals.append(sig)):
            # its receipt cannot be rechecked: unknown, refused up front naming it and the holder
            with self.assertRaisesRegex(lk.ReleaseRefused, rf"not signalled: holder pid {rec['pid']} .* "
                                        r'not proven ended: receipt STM32_Programmer_CLI .* unknown'):
                lk.release_token(rec['token'])
        self.assertEqual(signals, [])

    # --- interruption ---------------------------------------------------------------------------
    def test_an_interrupted_reset_proven_ended_releases_and_stops_the_farm(self):
        self.outcomes[RESET] = KeyboardInterrupt()
        self.p.boards = [dict(BOARD), dict(BOARD, name='c')]
        rows, aborted = eng.check_farm(self.p, self.p.boards, self.args, state_dir=str(self.state))
        self.assertTrue(aborted)
        self.assertEqual([r['name'] for r in rows], ['b'])
        self.assertEqual((rows[0]['status'], rows[0]['cleanup']['result']), ('failed', 'released'))
        self.assertEqual(rows[0]['operations'][0]['attempts'][0]['kind'], 'interrupted')
        self.take()


def row(name, status, **kw):
    return eng.new_row(name, probe='✅ stlink', location='1-1', status=status, **kw)


class Document(unittest.TestCase):
    def test_coverage_says_what_each_status_covers(self):
        entered = {'result': 'released'}
        rows = [row('a', 'ok', cleanup=entered), row('b', 'locked'), row('c', 'failed'),
                row('c2', 'failed', cleanup=entered), row('u', 'unsupported', cleanup=entered), row('d', 'blocked'),
                row('e', 'blocked', check_status='ok', cleanup={'result': 'unresolved'})]
        doc = eng.farm_document('ci', 'tinyusb.json', rows, {'path': 'p', 'digest': 'd'}, 1.23)
        self.assertEqual([r['coverage'] for r in doc['rows']],
                         ['reset-attempted', 'skipped-locked', 'reset-not-attempted', 'reset-attempted', 'unsupported',
                          'blocked-before', 'blocked-after'])
        self.assertEqual(doc['counts'], {'ok': 1, 'failed': 2, 'unsupported': 1, 'locked': 1, 'blocked': 2})
        self.assertEqual((doc['schema_version'], doc['check'], doc['host'], doc['config'], doc['elapsed_s']),
                         (2, 'farm-check', 'ci', 'tinyusb.json', 1.2))

    def test_exit_status(self):
        def code(*statuses, aborted=False):
            return eng.exit_status(eng.farm_document('h', 'c', [row(s, s) for s in statuses], {}, 0), aborted)
        self.assertEqual(code('ok', 'locked', 'unsupported'), 0)
        self.assertEqual(code(), 0)
        self.assertEqual(code('ok', 'failed'), 1)
        self.assertEqual(code('ok', aborted=True), 1)
        self.assertEqual(code('failed', 'blocked'), 4)

    def test_the_table_is_a_rendering_of_the_document(self):
        doc = eng.farm_document('ci', 'x.json', [row('a', 'locked', note=['held']), row('b', 'blocked'),
                                                 row('c', 'unsupported')], {}, 0)
        lines = eng.render_table(doc).splitlines()
        self.assertEqual([c.strip() for c in lines[1].strip('|').split('|')],
                         ['Board', 'Probe', 'Location', 'Reset/Status', 'Note'])
        self.assertEqual([c.strip() for c in lines[3].strip('|').split('|')], ['a', '✅ stlink', '1-1', '🔒 locked', 'held'])
        self.assertIn('⛔ blocked', lines[4])
        self.assertIn('➖ unsupported', lines[5])
        self.assertTrue(lines[-1].startswith('farm check: 0 ok · 0 failed · 1 unsupported · 1 locked · 1 blocked'))


class Main(unittest.TestCase):
    """main() with the stub profile; fds 1 and 2 are captured."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.d = Path(td.name)
        self.state = self.d / 'state'
        patch(self, lk, 'BOARD_LOCK_DIR', str(self.d / 'locks'))
        self.p = StubProfile(self.d)
        self.cfg = str(self.d / 'roster.json')

    def run_main(self, *argv, host='rig', check=None, **kw):
        if check:
            patch(self, eng, 'check_board_safe', check)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            sys.stdout.flush()
            sys.stderr.flush()
            saved = os.dup(1), os.dup(2)
            os.dup2(out.fileno(), 1)
            os.dup2(err.fileno(), 2)
            streams = sys.stdout, sys.stderr     # a test runner's capture objects are not fds 1 and 2
            sys.stdout, sys.stderr = (open(fd, 'w', encoding='utf-8', closefd=False) for fd in (1, 2))
            try:
                code = eng.main(self.p, list(argv), state_dir=str(self.state), host=host, **kw)
            except (SystemExit, RuntimeError) as e:
                code = e
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                sys.stdout, sys.stderr = streams
                os.write(1, b'after\n')   # must land where fd 1 pointed before main()
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
            out.seek(0)
            err.seek(0)
            return code, out.read().decode(), err.read().decode()

    def noisy_board(self, profile, board, args, state_dir):
        print('python noise')
        os.write(1, b'child noise\n')
        return row(board['name'], 'failed')

    def test_json_stdout_carries_only_the_document(self):
        for verbose in ([], ['-v']):
            code, out, err = self.run_main(self.cfg, '--json', *verbose, check=self.noisy_board)
            doc_line, after = out.splitlines()
            self.assertEqual(after, 'after')
            doc = json.loads(doc_line)
            self.assertEqual((doc['rows'][0]['name'], doc['config'], doc['check']), ('b', os.path.realpath(self.cfg), 'farm-check'))
            self.assertEqual(doc['profile'], {'path': os.path.realpath(self.p.__file__),
                                              'digest': hashlib.sha256(b'# stub profile\n').hexdigest()})
            self.assertEqual((doc['_hil']['body_schema'], doc['_hil']['termination']), ('farm-check', 'completed'))
            self.assertEqual(doc['_hil']['roster'], {'name': 'roster.json',
                                                     'sha256': hashlib.sha256(Path(self.cfg).read_bytes()).hexdigest()},
                             'portable: the operational path stays in `config`')
            self.assertEqual(code, 1)
            self.assertIn('farm check: host rig', err)
            self.assertIn('child noise', err)

    def test_the_user_bin_dirs_go_on_path_for_the_call(self):
        home = self.d / 'home'
        (home / 'bin').mkdir(parents=True)
        (home / '.local' / 'bin').mkdir(parents=True)
        seen = []

        def check(*a, **kw):
            seen.append(os.environ['PATH'])
            return row('b', 'ok')
        for path, want in ((os.pathsep.join(['/usr/bin', '/bin']),
                            [str(home / '.local/bin'), str(home / 'bin'), '/usr/bin', '/bin']),
                           (os.pathsep.join(['/usr/bin', str(home / 'bin')]),     # already there: not again
                            [str(home / '.local/bin'), '/usr/bin', str(home / 'bin')])):
            with self.subTest(path=path), mock.patch.dict(os.environ, {'PATH': path}), \
                    mock.patch.object(eng.Path, 'home', lambda: home):
                seen.clear()
                self.assertEqual(self.run_main(self.cfg, check=check)[0], 0)
                self.assertEqual(seen[0].split(os.pathsep), want)
                self.assertEqual(os.environ['PATH'], path, 'restored after the call')
        shutil.rmtree(home / 'bin')
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin'}), mock.patch.object(eng.Path, 'home', lambda: home):
            seen.clear()
            self.run_main(self.cfg, check=check)
            self.assertEqual(seen[0].split(os.pathsep), [str(home / '.local/bin'), '/usr/bin'], 'a missing dir is not added')

    def test_an_exception_restores_stdout(self):
        def boom(*a, **kw):
            raise RuntimeError('boom')
        patch(self, eng, 'check_farm', boom)
        code, out, _ = self.run_main(self.cfg, '--json')
        self.assertIsInstance(code, RuntimeError)
        self.assertEqual(out, 'after\n')

    def test_the_default_output_is_the_table_and_the_state_dir_is_private(self):
        code, out, _ = self.run_main(self.cfg, check=lambda *a, **kw: row('b', 'ok'))
        self.assertEqual(code, 0)
        self.assertIn('| Board', out)
        self.assertIn('farm check: 1 ok · 0 failed · 0 unsupported', out)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)

    def test_an_unknown_host_without_a_roster_is_refused_before_anything(self):
        code, out, err = self.run_main('--json', host='elsewhere', check=lambda *a, **kw: self.fail('checked'))
        self.assertEqual((code, out), (2, 'after\n'))
        self.assertIn('no roster for host elsewhere', err)
        self.assertEqual(self.run_main(self.cfg, host='elsewhere', check=lambda *a, **kw: row('b', 'ok'))[0], 0)

    def test_refusals_before_any_board_exit_2(self):
        code, _, err = self.run_main(self.cfg, '-b', 'nope', check=lambda *a, **kw: self.fail('checked'))
        self.assertEqual(code, 2)
        for removed in ('--bogus', '--scan-only', '--no-park', '-B'):
            self.assertEqual(self.run_main(self.cfg, removed)[0].code, 2, removed)
        self.p.reset = None
        self.assertEqual(self.run_main(self.cfg)[0], 2)

    def test_named_boards_include_named_only_ones(self):
        seen = []
        code, _, _ = self.run_main(self.cfg, '-b', 'parked', check=lambda p, b, *a, **kw: seen.append(b['name']) or row(b['name'], 'ok'))
        self.assertEqual((code, seen), (0, ['parked']))

    def test_an_interrupted_run_keeps_the_requested_selection(self):
        self.p.boards = [dict(BOARD), dict(BOARD, name='c')]

        def interrupted(profile, board, *a, **kw):
            return row(board['name'], 'failed', interrupted=True)
        code, out, _ = self.run_main(self.cfg, '--json', check=interrupted)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual((code, [r['name'] for r in doc['rows']]), (1, ['b']))
        self.assertEqual((doc['_hil']['selection'], doc['_hil']['termination']), (['b', 'c'], 'aborted'))

    def test_the_projects_lock_module_is_used_for_the_call(self):
        mine = load('hil_lock')
        mine.BOARD_LOCK_DIR = str(self.d / 'project-locks')
        engine_lock = eng.hil_lock
        seen = []

        def tracked(cmd, cwd=None, **kw):
            seen.append(sorted(os.listdir(mine.BOARD_LOCK_DIR)))
            r = subprocess.CompletedProcess(cmd, 0, 'ok', None)
            r.cleanup, r.receipt = 'done', None
            return r
        patch(self, eng.hil_process, 'run_tracked', tracked)
        code, _, _ = self.run_main(self.cfg, lock_module=mine)
        self.assertEqual((code, seen[0]), (0, ['b.lock']))
        self.assertFalse((self.d / 'locks').exists(), "the engine's own instance was not used")
        self.assertIs(eng.hil_lock, engine_lock)

    def test_a_lock_module_without_the_farm_check_reason_is_refused(self):
        api = eng.LOCK_FUNCTIONS + eng.LOCK_EXCEPTIONS + ('CLEANUP_ROLE', 'PROTECTED_REASONS')
        old = types.SimpleNamespace(**{a: getattr(lk, a) for a in api})
        old.PROTECTED_REASONS = {'hil_test.py'}
        code, _, err = self.run_main(self.cfg, lock_module=old, check=lambda *a, **kw: self.fail('checked'))
        self.assertEqual(code, 2)
        self.assertIn('farm_check', err)
        code, _, err = self.run_main(self.cfg, lock_module=types.SimpleNamespace(), check=lambda *a, **kw: self.fail('checked'))
        self.assertEqual(code, 2)
        self.assertIn('take_board_lock', err)
        # present but of the wrong type: refused before the engine runs, not a crash inside it
        for name, value in (('take_board_lock', None), ('RecoveryBlocked', None), ('RecoveryBlocked', int),
                            ('CLEANUP_ROLE', None), ('PROTECTED_REASONS', None), ('PROTECTED_REASONS', 'farm_check')):
            with self.subTest(name=name, value=value):
                bad = types.SimpleNamespace(**{a: getattr(lk, a) for a in api})
                setattr(bad, name, value)
                with mock.patch.object(eng, '_main', side_effect=AssertionError('entered _main')):
                    code, _, err = self.run_main(self.cfg, lock_module=bad)
                self.assertEqual(code, 2)
                self.assertIn(name if name != 'PROTECTED_REASONS' else 'farm_check', err)

    def test_a_report_is_persisted_only_on_request_in_its_own_namespace(self):
        self.run_main(self.cfg, check=lambda *a, **kw: row('b', 'ok'))
        rd = self.d / 'reports'
        self.assertFalse(rd.exists())
        # a harness report already there: never overwritten, joined or archived by a farm check
        rd.mkdir()
        harness = b'{"rows": [], "scope": "", "caveat": ""}\n'
        (rd / 'hil_report.json').write_bytes(harness)
        (rd / 'hil_report.md').write_bytes(b'| harness |\n')
        for _ in range(2):
            code, out, _ = self.run_main(self.cfg, '--report-dir', str(rd), check=lambda *a, **kw: row('b', 'ok'))
            self.assertEqual(code, 0)
        self.assertEqual(((rd / 'hil_report.json').read_bytes(), (rd / 'hil_report.md').read_bytes()),
                         (harness, b'| harness |\n'))
        self.assertFalse((rd / 'hil_report.attempts').exists())
        reading = core.read_document(rd / 'farm_check.json', eng.FARM_SCHEMA)
        self.assertEqual((reading.outcome, reading.body['rows'][0]['status']), ('valid', 'ok'))
        self.assertIsNone(reading.meta['previous_attempt'])
        self.assertEqual(len(list((rd / 'farm_check.attempts').iterdir())), 1)
        self.assertIn('| Board', (rd / 'farm_check.md').read_text())
        with self.assertRaises(ValueError):
            core.apply_run_rules(eng.FARM_SCHEMA, ['b'], [], 'completed', '')

    def test_a_check_that_cannot_publish_withdraws_the_previous_result(self):
        rd = self.d / 'reports'
        code, _, _ = self.run_main(self.cfg, '--report-dir', str(rd), check=lambda *a, **kw: row('b', 'ok'))
        self.assertEqual(code, 0)
        real = core.os.replace

        def failing(times):
            left = [times]

            def replace(src, dst, *a, **kw):
                if str(dst).endswith('farm_check.json') and left[0]:
                    left[0] -= 1
                    raise OSError(28, 'no space')
                return real(src, dst, *a, **kw)
            return replace
        with mock.patch.object(core.os, 'replace', failing(1)):
            code, _, err = self.run_main(self.cfg, '--report-dir', str(rd), check=lambda *a, **kw: row('b', 'failed'))
        self.assertEqual(code, 1)
        self.assertIn('report not persisted', err)
        self.assertIn('report withdrawn', err)
        reading = core.read_document(rd / 'farm_check.json', eng.FARM_SCHEMA)
        self.assertEqual(reading.outcome, 'withdrawn', 'the earlier ok check never reads as this one')
        self.assertIn(core.NOTICE, (rd / 'farm_check.md').read_text())
        # a withdrawal that cannot be written either: the predecessor it archived goes
        code, _, _ = self.run_main(self.cfg, '--report-dir', str(rd), check=lambda *a, **kw: row('b', 'ok'))
        self.assertEqual(code, 0)
        with mock.patch.object(core.os, 'replace', failing(2)):
            code, _, err = self.run_main(self.cfg, '--report-dir', str(rd), check=lambda *a, **kw: row('b', 'failed'))
        self.assertEqual(code, 1)
        self.assertIn('report removed', err)
        self.assertFalse((rd / 'farm_check.json').exists())
        self.assertFalse((rd / 'farm_check.md').exists())


# --- process integration ------------------------------------------------------------------------
LAUNCHER = """
import importlib.util, sys
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
eng = load('agentrc_hil_farm_check', sys.argv[1])
eng.hil_lock.BOARD_LOCK_DIR = sys.argv[2]
eng.RECHECK_WAIT = 1
eng.SETTLE = 0.3
prof = load('farm_profile', sys.argv[3])
sys.exit(eng.main(prof, sys.argv[5:], state_dir=sys.argv[4], host='rig'))
"""

PROFILE = """
import importlib.util, os
spec = importlib.util.spec_from_file_location('agentrc_hil_flash', os.environ['HIL_FLASH'])
hf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hf)
D = os.environ['FARM_DIR']
BOARD = {'name': 'farmb', 'flasher': {'name': 'stlink', 'uid': 'P', 'args': ''}}

def roster(host, config, args):
    return {'path': os.path.join(D, 'roster.json'), 'boards': [BOARD], 'named_only': []}

def lock_name(board):
    return board['name']

def probe_name(board):
    return board['flasher']['name']

def probe_candidates(board):
    return ['1-1']

def reset(ctx, board):
    return hf.reset_stlink(board['flasher'], timeout=60, run=ctx.run)
"""

STM32_CLI = """#!/bin/sh
case "$FARM_MODE" in
  slow) echo $$ > "$FARM_DIR/resetting"; exec sleep 60 ;;
  survivor) setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > "$FARM_DIR/survivor" ;;
esac
echo reset
"""


class Process(unittest.TestCase):
    """The engine in its own interpreter: profile -> hil_flash -> ctx.run -> run_tracked, with
    a fake STM32_Programmer_CLI on PATH and real locks in a temp dir."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.d = Path(td.name)
        self.locks = self.d / 'locks'
        self.state = self.d / 'state'
        self.state.mkdir(mode=0o700)
        self.addCleanup(self.quiesce)
        patch(self, lk, 'BOARD_LOCK_DIR', str(self.locks))
        for name, text in (('STM32_Programmer_CLI', STM32_CLI), ('farm_profile.py', PROFILE), ('roster.json', '{}')):
            (self.d / name).write_text(text)
        (self.d / 'STM32_Programmer_CLI').chmod(0o755)

    def start(self, mode=''):
        env = dict(os.environ, PATH=f'{self.d}{os.pathsep}{os.environ["PATH"]}', FARM_DIR=str(self.d),
                   FARM_MODE=mode, HIL_FLASH=str(SCRIPTS / 'hil_flash.py'))
        p = subprocess.Popen([sys.executable, '-c', LAUNCHER, str(SCRIPTS / 'hil_farm_check.py'), str(self.locks),
                              str(self.d / 'farm_profile.py'), str(self.state), '--json'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                             start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def wait_for(self, name):
        path = self.d / name
        deadline = time.monotonic() + 30
        while not (path.exists() and path.read_text().strip()):
            self.assertLess(time.monotonic(), deadline, f'{name} never appeared')
            time.sleep(0.05)
        pid = int(path.read_text())
        self.addCleanup(self.kill, pid)
        return pid

    def quiesce(self):
        """Wait for every run_tracked supervisor to end: its lock is held while it lives, and it
        still writes its record once its survivor is gone."""
        deadline = time.monotonic() + 20
        for lock in self.state.glob('hil_op_*.lock'):
            with open(lock) as f:
                while True:
                    try:
                        fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            return
                        time.sleep(0.05)

    @staticmethod
    def kill(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    def finish(self, p):
        out, err = p.communicate(timeout=60)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, f'rc {p.returncode}\nstdout {out}\nstderr {err}')
        return p.returncode, json.loads(lines[0])['rows'][0], json.loads(lines[0])

    def take(self):
        fh = lk.take_board_lock('farmb', 'test')
        lk.release_board_lock(fh)

    def test_a_real_reset_is_ok_and_released(self):
        code, r, doc = self.finish(self.start())
        self.assertEqual((code, r['status'], r['location'], r['cleanup']['result'], doc['_hil']['termination']),
                         (0, 'ok', '1-1', 'released', 'completed'))
        self.take()

    def test_a_handled_interruption_ends_the_reset_and_releases(self):
        p = self.start('slow')
        self.wait_for('resetting')
        os.kill(p.pid, signal.SIGINT)
        code, r, doc = self.finish(p)
        self.assertEqual((code, r['status'], r['cleanup']['result'], doc['_hil']['termination']),
                         (1, 'failed', 'released', 'aborted'))
        self.assertEqual(r['operations'][0]['attempts'][0]['kind'], 'interrupted')
        self.take()

    def test_abrupt_death_leaves_no_report_and_a_blocker(self):
        p = self.start('slow')
        self.wait_for('resetting')
        p.kill()
        out, _ = p.communicate(timeout=30)
        self.assertEqual(out, '')
        with self.assertRaises(lk.RecoveryBlocked):
            self.take()

    def test_a_surviving_child_is_kept_by_a_live_cleanup_holder(self):
        code, r, _ = self.finish(self.start('survivor'))
        survivor = self.wait_for('survivor')
        token = r['cleanup'].get('token')
        self.addCleanup(lambda: [self.kill(p) for p in [(lk.read_record('farmb') or {}).get('pid')] if isinstance(p, int)])
        self.assertEqual((code, r['status'], r['check_status'], r['cleanup']['result'], r['cleanup']['blocker']),
                         (4, 'blocked', 'ok', 'unresolved', 'cleanup-holder'))
        listed = [x['pid'] for rc in r['cleanup']['receipts'] for x in (rc['survivors'] or {}).get('processes', ())]
        self.assertIn(survivor, listed)
        with self.assertRaises(lk.BoardLocked):
            self.take()
        self.assertTrue(lk.is_cleanup_holder(lk.read_record('farmb')))
        with self.assertRaisesRegex(lk.ReleaseRefused, 'not signalled: .* not proven ended: receipt .* pending'):
            lk.release_token(token)      # its survivor still runs
        self.kill(survivor)
        deadline = time.monotonic() + 20
        while True:
            try:
                lk.release_token(token)
                break
            except lk.ReleaseRefused:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
        self.take()

    def test_a_holder_whose_supervisor_died_is_never_released_by_token(self):
        code, r, _ = self.finish(self.start('survivor'))
        survivor = self.wait_for('survivor')
        holder = lk.read_record('farmb')['pid']
        self.addCleanup(self.kill, holder)
        self.assertEqual((code, r['cleanup']['blocker']), (4, 'cleanup-holder'))
        receipt = r['cleanup']['receipts'][0]
        self.kill(receipt['supervisor']['pid'])
        deadline = time.monotonic() + 10
        while eng.hil_process.recheck(receipt) != 'unknown':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.kill(survivor)      # gone too, but nothing proves it any more
        with self.assertRaisesRegex(lk.ReleaseRefused, 'not signalled: .* not proven ended: receipt .* unknown'):
            lk.release_token(r['cleanup']['token'])
        with self.assertRaises(lk.BoardLocked):
            self.take()


if __name__ == '__main__':
    unittest.main()
