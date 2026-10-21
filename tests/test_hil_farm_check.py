"""skills/hil/scripts/hil_farm_check.py is the farm check engine a project's launcher runs with
its profile, moved from tinyusb's test/hil/helper/hil_pool_check.py (2c992c002) with the engine
cases of test_hil_pool_check.py. A stub profile drives the engine through the real hil_flash
provider with a faked tracked runner (plumbing only), and process-integration tests run the
engine in its own interpreter with a fake flasher on PATH, the real run_tracked and real
locks in a temp dir: handled interruption, abrupt death, a surviving child. No hardware."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the farm check needs POSIX flock, fork and sessions')

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

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

BOARD = {'name': 'b', 'uid': 'U', 'flasher': {'name': 'lm4flash', 'uid': 'P', 'args': ''}}
LOCAL = {'kind': 'local', 'receipt': 'build-receipt.json'}


def patch(test, obj, name, value):
    p = mock.patch.object(obj, name, value)
    p.start()
    test.addCleanup(p.stop)


class StubProfile:
    """A profile whose answers each test sets; its flashes go through hil_flash with ctx.run."""

    LEAVE_RUNNING = False

    def __init__(self, d: Path):
        self.d = d
        self.__file__ = str(d / 'stub_profile.py')
        Path(self.__file__).write_text('# stub profile\n')
        for name in ('light.bin', 'park.bin', 'verify.bin'):
            (d / name).write_bytes(name.encode())
        self.boards = [dict(BOARD)]
        self.candidates = ['1-1']
        self.provenance = dict(LOCAL)
        self.refused = None
        self.observed = {'status': 'ok', 'device': '✅ cafe:4001', 'notes': [], 'unverified': [], 'evidence': ['uid back']}
        self.final = {'verified': True, 'evidence': ['parked'], 'notes': []}
        self.final_extra = []
        self.calls = []
        self.records = []           # the board's lock record as each flash saw it

    def roster(self, host, config, args):
        if config is None and host != 'rig':
            raise LookupError(f'no roster for host {host}')
        path = config or str(self.d / 'roster.json')
        Path(path).write_text(json.dumps({'boards': self.boards}))
        return {'path': path, 'boards': self.boards, 'named_only': [dict(BOARD, name='parked')]}

    def lock_name(self, board):
        return board['name']

    def probe_candidates(self, board):
        return list(self.candidates)

    def scan_device(self, board):
        self.calls.append('scan')
        return {'device': '⚫ off bus (parked?)', 'notes': ['scanned']}

    def prepare(self, boards, args, say):
        self.calls.append('prepare')
        return {'prepared': True}

    def image(self, name):
        return {'path': str(self.d / name), 'variant': 'b', 'label': name.split('.')[0], 'provenance': self.provenance}

    def plan(self, board, args, prepared, leave_running):
        self.calls.append(('plan', prepared, leave_running))
        if self.refused:
            return {'refused': self.refused}
        return {'image': self.image('light.bin'),
                'final': {'postcondition': 'parked in board_test', 'image': self.image('park.bin')},
                'notes': ['variant: b']}

    def flash(self, ctx, board, plan):
        self.calls.append('flash')
        self.records.append(lk.read_record(board['name']))
        return hf.flash_lm4flash(board['flasher'], plan['image']['path'], timeout=5, run=ctx.run)

    def observe(self, ctx, board, plan, before):
        self.calls.append('observe')
        return dict(self.observed)

    def final_state(self, ctx, board, plan, observation):
        self.calls.append('final')
        if isinstance(self.final, BaseException):
            raise self.final
        ops = [hf.flash_lm4flash(board['flasher'], plan['final']['image']['path'], timeout=5, run=ctx.run)]
        for extra in self.final_extra:     # a second command of the same final state
            ops.append(hf.flash_lm4flash(board['flasher'], str(self.d / extra), timeout=5, run=ctx.run))
        return dict(self.final, operations=ops)


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
        self.outcomes = {}          # image name -> (rc, cleanup, stdout) or an exception
        self.rechecks = {}
        self.args = types.SimpleNamespace(scan_only=False, no_park=False, verbose=False)

    def tracked(self, cmd, cwd=None, *, timeout, settle, state_dir, **kw):
        self.assertEqual(state_dir, str(self.state))
        name = cmd.rsplit('/', 1)[-1]
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

    # --- moved from test_hil_pool_check: check_board up to and around the flash ----------------
    def test_a_free_board_is_taken_under_the_protected_reason_and_released(self):
        row = self.check()
        rec = self.p.records[0]
        self.assertEqual((rec['reason'], rec['protected']), ('farm_check', True))
        self.assertIn('farm_check', lk.PROTECTED_REASONS)
        self.assertEqual((row['status'], self.ran), ('ok', ['light.bin', 'park.bin']))
        self.assertEqual(lk.record_state('b')[0], 'empty')
        self.take()

    def test_a_device_that_does_not_come_back_fails_and_is_still_restored(self):
        self.p.observed = dict(self.p.observed, status='failed', device='❌ not enumerated')
        row = self.check()
        self.assertEqual((row['status'], row['device'], row['cleanup']['result']), ('failed', '❌ not enumerated', 'verified'))
        self.assertEqual(row['verification'], {'result': 'unverified', 'reasons': ['failed']})
        self.take()

    def test_a_failed_flash_is_flash_failed_and_still_runs_the_final_state(self):
        self.outcomes['light.bin'] = (1, 'done', 'Info: x\nError: no target\n')
        row = self.check()
        self.assertEqual((row['status'], row['note'][-1]), ('flash-failed', 'flash: Error: no target'))
        self.assertEqual(self.ran, ['light.bin', 'park.bin'])
        self.assertNotIn('observe', self.p.calls)
        self.assertEqual(row['operations'][0]['attempts'][0]['returncode'], 1)

    def test_a_raised_flash_keeps_its_operation_on_the_row(self):
        self.outcomes['light.bin'] = OSError(2, 'No such file or directory', 'lm4flash')
        row = self.check()
        self.assertEqual(row['status'], 'flash-failed')
        self.assertTrue(row['note'][-1].startswith('flash: FileNotFoundError'), row['note'])
        self.assertEqual(row['operations'][0]['attempts'][0]['kind'], 'error')
        self.assertEqual(row['cleanup']['result'], 'verified')

    def test_a_flash_that_raised_before_running_anything_still_gets_its_final_state(self):
        # it may have changed the board in process (a serial write, a library call)
        def boom(ctx, board, plan):
            raise RuntimeError('no serial device for the probe')
        self.p.flash = boom
        row = self.check()
        self.assertEqual((row['status'], row['cleanup']['result'], self.ran), ('flash-failed', 'verified', ['park.bin']))
        self.take()

    def test_only_an_observation_before_any_mutation_leaves_the_board_untouched(self):
        def boom(ctx, board, plan):
            raise RuntimeError('serial port gone')
        self.p.observe_before = boom
        row = self.check()
        self.assertEqual((row['status'], row['cleanup']['result'], self.ran), ('failed', 'untouched', []))
        self.assertNotIn('flash', self.p.calls)
        self.take()

    def test_the_final_states_operations_are_kept_on_the_row(self):
        row = self.check()
        self.assertEqual([(o['step'], o['attempts'][0]['cmd'].rsplit('/', 1)[-1]) for o in row['operations']],
                         [('flash', 'light.bin'), ('final', 'park.bin')])

    def test_a_failed_final_state_keeps_its_operation(self):
        self.outcomes['park.bin'] = OSError(5, 'Input/output error')
        row = self.check()
        self.assertEqual((row['status'], row['cleanup']['result']), ('blocked', 'error'))
        self.assertEqual([o['step'] for o in row['operations']], ['flash', 'final'])
        self.assertEqual(row['operations'][1]['attempts'][0]['kind'], 'error')

    def test_a_missing_final_image_fails_before_the_lock(self):
        (self.d / 'park.bin').unlink()
        row = self.check()
        self.assertEqual((row['status'], self.ran), ('flash-failed', []))
        self.assertIn('image missing', row['note'][-1])
        self.assertFalse(self.locks.exists())

    def test_a_plan_refusal_touches_nothing(self):
        # tinyusb: no light image, an RTT host board, a board only its runner can hold (openocd_esp)
        self.p.refused = {'status': 'flash-failed', 'notes': ['only hil_test.py keeps an openocd_esp session']}
        row = self.check()
        self.assertEqual((row['status'], row['note'], self.ran), ('flash-failed', self.p.refused['notes'], []))
        self.assertFalse(self.locks.exists())

    def test_a_malformed_plan_is_the_boards_own_error_row(self):
        self.p.plan = lambda *a: {'image': {'path': 'x'}}
        row = self.check()
        self.assertEqual((row['status'], row['device'], self.ran), ('failed', '❌ error', []))
        self.assertIn('ValueError', row['note'][0])

    def test_a_board_without_a_name_is_its_own_error_row(self):
        row = self.check({'uid': 'U'})
        self.assertEqual((row['name'], row['status']), ('?', 'failed'))
        self.assertIn('KeyError', row['note'][0])

    # --- probe matching, images, provenance ---------------------------------------------------
    def test_an_ambiguous_probe_reports_its_candidates_and_picks_none(self):
        self.p.candidates = ['1-1', '1-2']
        for scan in (False, True):
            self.args.scan_only = scan
            row = self.check()
            self.assertEqual((row['status'], row['probe'], row['probe_busport']), ('flash-failed', '⚠ ambiguous', None))
            self.assertIn('1-1, 1-2', row['note'][0])
        self.assertEqual(self.ran, [])

    def test_a_missing_probe_is_flash_failed(self):
        self.p.candidates = []
        row = self.check()
        self.assertEqual((row['status'], row['probe'], self.ran), ('flash-failed', '❌ missing', []))

    def test_the_image_carries_its_digest_and_local_provenance(self):
        row = self.check()
        want = hashlib.sha256(b'light.bin').hexdigest()
        self.assertEqual(row['image'], {'variant': 'b', 'label': 'light', 'path': str(self.d / 'light.bin'),
                                        'digest': want, 'provenance': LOCAL})
        self.assertEqual(row['cleanup']['image']['digest'], hashlib.sha256(b'park.bin').hexdigest())

    def test_missing_provenance_is_explicit_and_none_is_refused(self):
        self.p.provenance = {'kind': 'missing', 'reason': 'built from a dirty tree'}
        self.assertEqual(self.check()['image']['provenance']['kind'], 'missing')
        for bad in (None, {'kind': 'ci', 'repo': 'o/r'}, {'kind': 'guess'}):
            self.p.provenance = bad
            row = self.check()
            self.assertEqual(row['device'], '❌ error', bad)
        self.assertEqual(self.ran.count('light.bin'), 1)

    def test_scan_only_takes_no_lock_and_flashes_nothing(self):
        self.args.scan_only = True
        row = self.check()
        self.assertEqual((row['status'], row['device'], row['note']), ('ok', '⚫ off bus (parked?)', ['scanned']))
        self.assertEqual((self.ran, self.p.calls), ([], ['scan']))
        self.assertFalse(self.locks.exists())

    # --- verification and cleanup ---------------------------------------------------------------
    def test_verification_and_cleanup_are_reported_apart(self):
        self.p.final = {'verified': False, 'evidence': ['device still enumerated'], 'notes': ['park unverified']}
        row = self.check()
        self.assertEqual((row['status'], row['check_status']), ('blocked', 'ok'))
        self.assertEqual(row['verification'], {'result': 'verified', 'reasons': []})
        self.assertEqual((row['cleanup']['result'], row['cleanup']['evidence']), ('unverified', ['device still enumerated']))
        self.p.final = {'verified': True, 'evidence': ['parked'], 'notes': []}
        self.p.observed = dict(self.p.observed, unverified=['cafe:4001 before the flash too: new image unverified'])
        lk.release_board_lock(lk.flock_nb('b'))   # the test's own stand-in for the recovery
        row = self.check()
        self.assertEqual((row['status'], row['verification']['result'], row['cleanup']['result']),
                         ('ok', 'unverified', 'verified'))

    def test_a_failed_cleanup_keeps_the_protected_record_as_a_blocker(self):
        self.p.final = RuntimeError('park flash failed')
        row = self.check()
        self.assertEqual((row['status'], row['cleanup']['result'], row['cleanup']['blocker']),
                         ('blocked', 'error', 'stale-record'))
        rec = lk.read_record('b')
        self.assertEqual((rec['reason'], rec['protected']), ('farm_check', True))
        with self.assertRaises(lk.RecoveryBlocked):
            self.take()

    def test_a_flash_not_proven_ended_skips_the_final_state(self):
        self.outcomes['light.bin'] = (0, 'unknown', 'ok')
        row = self.check()
        self.assertEqual(self.ran, ['light.bin'])
        self.assertNotIn('final', self.p.calls)
        self.assertEqual((row['status'], row['cleanup']['result'], row['cleanup']['blocker']),
                         ('blocked', 'not-run', 'stale-record'))
        self.assertEqual(row['cleanup']['receipts'], [{'token': 'light.bin', 'lock': '/nonexistent'}])

    def test_a_flash_not_proven_ended_runs_no_observation(self):
        self.outcomes['light.bin'] = (0, 'unknown', 'ok')

        def observe(ctx, board, plan, before):
            self.p.calls.append('observe')
            hf.reset_stlink({'uid': 'P'}, timeout=5, run=ctx.run)
            return dict(self.p.observed)
        self.p.observe = observe
        row = self.check()
        self.assertEqual(self.ran, ['light.bin'])
        self.assertNotIn('observe', self.p.calls)
        self.assertEqual((row['status'], row['verification']['reasons']), ('blocked', ['flash not proven ended']))
        self.assertTrue(any(n.startswith('observation not run') for n in row['note']), row['note'])

    def test_an_observation_not_proven_ended_enters_no_flash(self):
        def observe_before(ctx, board, plan):
            hf.reset_stlink({'uid': 'P'}, timeout=5, run=ctx.run)
        self.p.observe_before = observe_before
        self.outcomes['STM32_Programmer_CLI --connect port=swd sn=P --rst --go'] = (0, 'unknown', '')
        row = self.check()
        self.assertNotIn('flash', self.p.calls)
        self.assertEqual(len(self.ran), 1)
        self.assertEqual((row['status'], row['cleanup']['result']), ('blocked', 'not-run'))
        self.assertTrue(any(n.startswith('flash not run') for n in row['note']), row['note'])

    def test_a_command_not_proven_ended_stops_the_next_one_in_the_final_state(self):
        self.p.final_extra = ['verify.bin']
        self.outcomes['park.bin'] = (0, 'unknown', 'ok')
        row = self.check()
        self.assertEqual(self.ran, ['light.bin', 'park.bin'])
        self.assertEqual((row['status'], row['cleanup']['result']), ('blocked', 'error'))
        self.assertIn('Outstanding', row['cleanup']['evidence'][0])
        self.assertEqual(row['cleanup']['receipts'], [{'token': 'park.bin', 'lock': '/nonexistent'}])

    def test_a_flash_proven_ended_on_recheck_lets_the_final_state_run(self):
        self.outcomes['light.bin'] = (0, 'pending', 'ok')
        self.rechecks['light.bin'] = 'done'
        row = self.check()
        self.assertEqual((row['status'], self.ran), ('ok', ['light.bin', 'park.bin']))
        self.take()

    # --- incoming locks -------------------------------------------------------------------------
    def test_a_held_board_is_locked_never_waited_on(self):
        self.hold()
        t = time.monotonic()
        row = self.check()
        self.assertLess(time.monotonic() - t, 2)
        self.assertEqual((row['status'], row['flash'], self.ran), ('locked', '🔒 locked', []))
        self.assertIn('hil_test.py', row['note'][-1])

    def test_no_bypass(self):
        self.hold()
        with mock.patch.dict(os.environ, {'HIL_NO_BOARD_LOCK': '1'}):
            self.assertEqual(self.check()['status'], 'locked')
        self.assertEqual(self.ran, [])

    def test_a_stale_protected_record_is_blocked_before_any_mutation(self):
        self.stale(protected=True)
        row = self.check()
        self.assertEqual((row['status'], row['flash'], self.ran), ('blocked', '⛔ blocked', []))
        doc = eng.farm_document('rig', 'r.json', 'full', [row], {}, 0)
        self.assertEqual((doc['rows'][0]['coverage'], eng.exit_status(doc)), ('blocked-before', 4))

    def test_a_live_cleanup_holder_is_blocked_not_locked(self):
        self.hold('hil_reserve', role=lk.CLEANUP_ROLE)
        self.assertEqual(self.check()['status'], 'blocked')

    def test_the_legacy_reason_stays_protected_and_its_stale_record_does_not_block(self):
        self.assertTrue(lk.is_protected({'reason': 'pool_check'}))
        fh = self.hold('pool_check')
        self.assertEqual(self.check()['status'], 'locked')
        fh.close()   # an old pool check that died: unmarked, its record is no blocker
        self.assertEqual(self.check()['status'], 'ok')

    def test_an_unusable_lock_is_an_error_not_a_holder(self):
        self.locks.mkdir()
        os.mkdir(self.locks / 'b.lock')
        row = self.check()
        self.assertEqual((row['status'], row['flash']), ('failed', '❌ lock'))
        self.assertIn('unavailable', row['note'][-1])

    # --- the profile's seams on ctx -------------------------------------------------------------
    def test_a_run_is_labelled_whatever_its_argv_holds(self):
        def tracked(cmd, cwd=None, **kw):
            r = subprocess.CompletedProcess(cmd, 0, '', None)
            r.cleanup, r.receipt = 'done', None
            return r
        patch(self, eng.hil_process, 'run_tracked', tracked)
        ctx = eng.Context(dict(BOARD), self.args, str(self.state), eng.new_row('b'))
        ctx.run([Path('/opt/flash'), '-v'], timeout=5)
        self.assertEqual(ctx.ledger, [{'cmd': '/opt/flash -v', 'cleanup': 'done', 'receipt': None}])

    def test_settle_waits_only_while_a_receipt_can_still_answer(self):
        patch(self, eng, 'RECHECK_WAIT', 5)
        ledger = [{'cmd': 'lost', 'cleanup': 'unknown', 'receipt': None}]
        t = time.monotonic()
        eng._settle(types.SimpleNamespace(ledger=ledger, recheck_by=None))
        self.assertLess(time.monotonic() - t, 1, 'no receipt left to recheck, nothing to wait for')
        self.assertEqual(ledger, [{'cmd': 'lost', 'cleanup': 'unknown', 'receipt': None}], 'it stays unresolved')

    def test_one_recheck_wait_per_reservation_and_time_never_clears_a_run(self):
        clock, answers = [100.0], []
        patch(self, eng, 'time', types.SimpleNamespace(monotonic=lambda: clock[0],
                                                       sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
        patch(self, eng, 'RECHECK_WAIT', 5)
        patch(self, eng.hil_process, 'recheck', lambda r: answers.pop(0) if answers else 'pending')
        patch(self, eng.hil_process, 'tidy', lambda r: None)
        ctx = types.SimpleNamespace(ledger=[{'cmd': 'flash', 'cleanup': 'pending', 'receipt': 'r1'}], recheck_by=None)
        eng._settle(ctx)
        self.assertEqual(ctx.recheck_by, 105.0, 'the first open run starts the one wait')
        self.assertAlmostEqual(clock[0], 105.0, delta=0.15)
        waited = clock[0]
        self.assertEqual(ctx.ledger[0]['cleanup'], 'pending', 'time alone never clears it')
        eng._settle(ctx)
        self.assertEqual(clock[0], waited, 'a later call rechecks once, without waiting')
        answers.append('done')
        eng._settle(ctx)
        self.assertEqual((ctx.ledger[0]['cleanup'], ctx.ledger[0]['receipt']), ('done', None), 'a late answer still counts')

    def test_settle_holds_in_process_board_io_until_every_run_is_proven_ended(self):
        reset = 'STM32_Programmer_CLI --connect port=swd sn=P --rst --go'
        patch(self, eng, '_cleanup_holder', lambda *a: (None, 'no holder in this test'))
        for cleanup in ('pending', 'unknown', 'done'):
            with self.subTest(cleanup=cleanup):
                io = []

                def observe(ctx, board, plan, before):
                    ctx.record(hf.reset_stlink({'uid': 'P'}, timeout=5, run=ctx.run))
                    ctx.settle()
                    io.append('uart opened')
                    return dict(self.p.observed)
                self.p.observe = observe
                self.outcomes[reset] = (0, cleanup, '')
                self.rechecks[reset] = cleanup
                row = self.check()
                self.assertEqual([o['step'] for o in row['operations']][:2], ['flash', 'observe'])
                if cleanup == 'done':
                    self.assertEqual((io, row['status']), (['uart opened'], 'ok'))
                    continue
                self.assertEqual((io, row['status'], row['cleanup']['result']), ([], 'blocked', 'not-run'))
                self.assertTrue(any('Outstanding' in n for n in row['note']), row['note'])
                lk.release_board_lock(lk.flock_nb('b'))   # the test's own stand-in for the recovery

    def test_a_recorded_operation_survives_a_later_failure_and_is_kept_once(self):
        for failure in (RuntimeError('serial port gone'), KeyboardInterrupt(), None):
            with self.subTest(failure=type(failure).__name__):
                def final_state(ctx, board, plan, observation):
                    op = hf.flash_lm4flash(board['flasher'], plan['final']['image']['path'], timeout=5, run=ctx.run)
                    ctx.record(op)
                    if failure is not None:
                        raise failure
                    return {'verified': True, 'evidence': [], 'notes': [], 'operations': [op]}
                self.p.final_state = final_state
                row = self.check()
                self.assertEqual([o['step'] for o in row['operations']], ['flash', 'final'])
                self.assertEqual(row['cleanup']['result'], {'RuntimeError': 'error', 'KeyboardInterrupt': 'interrupted',
                                                            'NoneType': 'verified'}[type(failure).__name__])
                if failure is not None:
                    lk.release_board_lock(lk.flock_nb('b'))

    def test_flash_error_line_is_reachable_from_ctx(self):
        seen = []

        def flash(ctx, board, plan):
            seen.append(ctx.flash_error_line('Info: connecting\nError: no target\nbye\n'))
            return hf.flash_lm4flash(board['flasher'], plan['image']['path'], timeout=5, run=ctx.run)
        self.p.flash = flash
        self.check()
        self.assertEqual(seen, ['Error: no target'])

    def test_extra_plan_keys_are_the_profiles_own_data(self):
        plan, seen = self.p.plan, []

        def with_extras(*a):
            out = plan(*a)
            out['kind'] = 'host'
            out['image']['sidecars'] = ['config.env']
            return out
        self.p.plan = with_extras

        def flash(ctx, board, plan):
            seen.append((plan['kind'], plan['image']['sidecars']))
            return hf.flash_lm4flash(board['flasher'], plan['image']['path'], timeout=5, run=ctx.run)
        self.p.flash = flash
        self.assertEqual(self.check()['status'], 'ok')
        self.assertEqual(seen, [('host', ['config.env'])])

    # --- handing a survivor's board to a cleanup holder -----------------------------------------
    def handoff(self, argv=None, popen=None):
        """A flash that leaves a survivor (`pending`) with the holder's start replaced."""
        self.outcomes['light.bin'] = (0, 'pending', 'ok')
        self.rechecks['light.bin'] = 'pending'
        patch(self, eng, 'HOLDER_READY', 1)
        if argv is not None:
            patch(self, eng, '_holder_argv', lambda job: argv(job))
        if popen is not None:
            patch(self, eng.subprocess, 'Popen', popen)
        t = time.monotonic()
        row = self.check()
        self.assertLess(time.monotonic() - t, 8, 'every wait of the handoff is bounded')
        doc = eng.farm_document('rig', 'r.json', 'full', [row], {}, 0)
        self.assertEqual((row['status'], row['coverage'], row['check_status'], eng.exit_status(doc)),
                         ('blocked', 'blocked-after', 'failed', 4))
        self.assertEqual(row['cleanup']['receipts'], [{'token': 'light.bin', 'lock': '/nonexistent'}])
        self.assertNotIn('final', self.p.calls)
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
        def late(job):
            return ['sh', '-c', 'sleep 3; exec "$@"', 'sh', *eng._holder_argv(job)]
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
        self.p.final = {'verified': False, 'evidence': ['still enumerated'], 'notes': []}
        self.p.boards = [dict(BOARD), dict(BOARD, name='c')]
        real = lk.write_record

        def write_record(fh, reason, **extra):
            if 'detail' in extra:      # the blocker record, not the take's own
                signal.raise_signal(signal.SIGINT)
            return real(fh, reason, **extra)
        patch(self, eng.hil_lock, 'write_record', write_record)
        rows, aborted = eng.check_farm(self.p, self.p.boards, self.args, state_dir=str(self.state))
        doc = eng.farm_document('rig', 'r.json', 'full', rows, {}, 0)
        self.assertEqual((aborted, [r['name'] for r in rows], eng.exit_status(doc, aborted)), (True, ['b'], 4))
        self.assertEqual((rows[0]['status'], rows[0]['cleanup']['blocker'], rows[0]['interrupted']),
                         ('blocked', 'stale-record', True))
        self.assertEqual(lk.read_record('b')['detail'][:30], 'farm check cleanup unresolved:')
        with self.assertRaises(lk.RecoveryBlocked):   # the engine's descriptor is closed
            self.take()

    def interrupt_before_finalization(self):
        real, calls = eng._end_reservation, []

        def end(*a):
            calls.append(a[-1]['result'])
            if len(calls) == 1:
                raise KeyboardInterrupt     # before its own interrupt hold is in place
            return real(*a)
        patch(self, eng, '_end_reservation', end)
        return calls

    def test_an_interrupt_before_finalization_finalizes_from_the_same_ledger(self):
        calls = self.interrupt_before_finalization()
        row = self.check()
        self.assertEqual(calls, ['verified', 'interrupted'])
        self.assertEqual((row['status'], row['interrupted'], row['cleanup']['blocker']), ('blocked', True, 'stale-record'))
        with self.assertRaises(lk.RecoveryBlocked):
            self.take()

    def test_an_interrupt_before_finalization_keeps_a_pending_observations_receipt(self):
        def observe_before(ctx, board, plan):    # its run survives; it returns no operation
            ctx.run('observe-probe', timeout=5)
        self.p.observe_before = observe_before
        self.outcomes['observe-probe'] = (0, 'pending', '')
        self.rechecks['observe-probe'] = 'pending'
        patch(self, eng, 'HOLDER_READY', 2)
        self.interrupt_before_finalization()
        row = self.check()
        receipt = {'token': 'observe-probe', 'lock': '/nonexistent'}
        rec = lk.read_record('b')
        if isinstance(rec, dict) and isinstance(rec.get('pid'), int) and rec['pid'] != os.getpid():
            self.addCleanup(Process.kill, rec['pid'])
        self.assertNotIn('flash', self.p.calls)
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
        doc = eng.farm_document('rig', 'r.json', 'full', [row], {}, 0)
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
        # its receipt cannot be rechecked: unknown, never released
        with mock.patch.object(lk, 'RELEASE_WAIT', 0.3), self.assertRaisesRegex(lk.ReleaseRefused, 'left its records'):
            lk.release_token(rec['token'])

    # --- interruption ---------------------------------------------------------------------------
    def test_an_interrupted_flash_still_restores_and_stops_the_farm(self):
        self.outcomes['light.bin'] = KeyboardInterrupt()
        self.p.boards = [dict(BOARD), dict(BOARD, name='c')]
        rows, aborted = eng.check_farm(self.p, self.p.boards, self.args, state_dir=str(self.state))
        self.assertTrue(aborted)
        self.assertEqual([r['name'] for r in rows], ['b'])
        self.assertEqual((rows[0]['status'], rows[0]['cleanup']['result'], self.ran),
                         ('failed', 'verified', ['light.bin', 'park.bin']))
        self.assertEqual(rows[0]['operations'][0]['attempts'][0]['kind'], 'interrupted')
        self.take()


def row(name, status, **kw):
    return eng.new_row(name, probe='✅ 1-1', probe_busport='1-1', status=status, **kw)


class Document(unittest.TestCase):
    def test_coverage_says_what_each_status_covers(self):
        rows = [row('a', 'ok'), row('b', 'locked'), row('c', 'failed'), row('d', 'blocked'),
                row('e', 'blocked', check_status='ok')]
        doc = eng.farm_document('ci', 'tinyusb.json', 'full', rows, {'path': 'p', 'digest': 'd'}, 1.23)
        self.assertEqual([r['coverage'] for r in doc['rows']],
                         ['full-attempted', 'skipped-locked', 'full-attempted', 'blocked-before', 'blocked-after'])
        self.assertEqual(doc['counts'], {'ok': 1, 'flash-failed': 0, 'failed': 1, 'locked': 1, 'blocked': 2})
        self.assertEqual((doc['schema_version'], doc['check'], doc['host'], doc['config'], doc['mode'], doc['elapsed_s']),
                         (1, 'farm-check', 'ci', 'tinyusb.json', 'full', 1.2))

    def test_every_scan_only_row_is_probe_only(self):
        doc = eng.farm_document('ci', 'x.json', 'scan-only', [row('a', 'ok'), row('b', 'flash-failed')], {}, 0)
        self.assertEqual({r['coverage'] for r in doc['rows']}, {'probe-only'})

    def test_exit_status(self):
        def code(*statuses, aborted=False):
            return eng.exit_status(eng.farm_document('h', 'c', 'full', [row(s, s) for s in statuses], {}, 0), aborted)
        self.assertEqual(code('ok', 'locked'), 0)
        self.assertEqual(code(), 0)
        self.assertEqual(code('ok', 'flash-failed'), 1)
        self.assertEqual(code('ok', 'failed'), 1)
        self.assertEqual(code('ok', aborted=True), 1)
        self.assertEqual(code('failed', 'blocked'), 4)

    def test_the_table_is_a_rendering_of_the_document(self):
        doc = eng.farm_document('ci', 'x.json', 'full', [row('a', 'locked', note=['held']), row('b', 'blocked')], {}, 0)
        lines = eng.render_table(doc).splitlines()
        self.assertTrue(lines[1].startswith('| Board'))
        self.assertIn('🔒 locked', lines[3])
        self.assertIn('held', lines[3])
        self.assertIn('⛔ blocked', lines[4])
        self.assertTrue(lines[-1].startswith('farm check: 0 ok · 0 flash-failed · 0 failed · 1 locked · 1 blocked'))


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

    def noisy_board(self, profile, board, args, state_dir, prepared=None):
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

    def test_the_default_output_is_the_table(self):
        code, out, _ = self.run_main(self.cfg, check=lambda *a, **kw: row('b', 'ok'))
        self.assertEqual(code, 0)
        self.assertIn('| Board', out)
        self.assertIn('farm check: 1 ok · 0 flash-failed', out)

    def test_only_a_full_check_prepares(self):
        self.run_main(self.cfg, '--scan-only', check=lambda *a, **kw: row('b', 'ok'))
        self.assertEqual(self.p.calls, [])
        self.assertFalse(self.state.exists(), 'a scan-only check needs no state dir')
        self.run_main(self.cfg, check=lambda *a, **kw: row('b', 'ok'))
        self.assertEqual(self.p.calls, ['prepare'])
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)

    def test_an_unknown_host_without_a_roster_is_refused_before_anything(self):
        code, out, err = self.run_main('--json', host='elsewhere', check=lambda *a, **kw: self.fail('checked'))
        self.assertEqual((code, out, self.p.calls), (2, 'after\n', []))
        self.assertIn('no roster for host elsewhere', err)
        self.assertEqual(self.run_main(self.cfg, host='elsewhere', check=lambda *a, **kw: row('b', 'ok'))[0], 0)

    def test_refusals_before_any_board_exit_2(self):
        cases = {'unknown board': ['-b', 'nope'], 'no-park unsupported': ['--no-park']}
        for name, argv in cases.items():
            code, _, err = self.run_main(self.cfg, *argv, check=lambda *a, **kw: self.fail('checked'))
            self.assertEqual(code, 2, name)
        self.assertEqual(self.run_main('--bogus')[0].code, 2)
        self.p.observe = None
        self.assertEqual(self.run_main(self.cfg)[0], 2)

    def test_no_park_runs_when_the_profile_verifies_leave_running(self):
        self.p.LEAVE_RUNNING = True
        seen = []
        self.run_main(self.cfg, '--no-park', check=lambda p, b, args, *a, **kw: seen.append(args.no_park) or row('b', 'ok'))
        self.assertEqual(seen, [True])

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
        old.PROTECTED_REASONS = {'hil_test.py', 'pool_check'}
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
import importlib.util, json, os
spec = importlib.util.spec_from_file_location('agentrc_hil_flash', os.environ['HIL_FLASH'])
hf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hf)
D = os.environ['FARM_DIR']
BOARD = {'name': 'farmb', 'flasher': {'name': 'lm4flash', 'uid': 'P', 'args': ''}}

def image(name):
    return {'path': os.path.join(D, name), 'variant': 'farmb', 'label': name, 'provenance': {'kind': 'local', 'receipt': 'r'}}

def roster(host, config, args):
    return {'path': os.path.join(D, 'roster.json'), 'boards': [BOARD], 'named_only': []}

def lock_name(board):
    return board['name']

def probe_candidates(board):
    return ['1-1']

def scan_device(board):
    return {'device': '-', 'notes': []}

def plan(board, args, prepared, leave_running):
    return {'image': image('light.bin'), 'final': {'postcondition': 'parked', 'image': image('park.bin')}, 'notes': []}

def flash(ctx, board, plan):
    return hf.flash_lm4flash(board['flasher'], plan['image']['path'], timeout=60, run=ctx.run)

def observe(ctx, board, plan, before):
    return {'status': 'ok', 'device': 'up', 'notes': [], 'unverified': [], 'evidence': []}

def final_state(ctx, board, plan, observation):
    op = hf.flash_lm4flash(board['flasher'], plan['final']['image']['path'], timeout=60, run=ctx.run)
    return {'verified': op.returncode == 0 and os.environ.get('FARM_PARK') != 'unverified', 'evidence': [], 'notes': []}
"""

LM4FLASH = """#!/bin/sh
case "$*" in
  *light.bin*)
    case "$FARM_MODE" in
      slow) echo $$ > "$FARM_DIR/flashing"; exec sleep 60 ;;
      survivor) setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > "$FARM_DIR/survivor" ;;
    esac ;;
esac
echo flashed
"""


class Process(unittest.TestCase):
    """The engine in its own interpreter: profile -> hil_flash -> ctx.run -> run_tracked, with
    a fake lm4flash on PATH and real locks in a temp dir."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.d = Path(td.name)
        self.locks = self.d / 'locks'
        self.state = self.d / 'state'
        self.state.mkdir(mode=0o700)
        self.addCleanup(self.quiesce)
        patch(self, lk, 'BOARD_LOCK_DIR', str(self.locks))
        for name, text in (('lm4flash', LM4FLASH), ('farm_profile.py', PROFILE)):
            (self.d / name).write_text(text)
        (self.d / 'lm4flash').chmod(0o755)
        for name in ('light.bin', 'park.bin', 'roster.json'):
            (self.d / name).write_text(name)

    def start(self, mode='', park=''):
        env = dict(os.environ, PATH=f'{self.d}{os.pathsep}{os.environ["PATH"]}', FARM_DIR=str(self.d),
                   FARM_MODE=mode, FARM_PARK=park, HIL_FLASH=str(SCRIPTS / 'hil_flash.py'))
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

    def interrupt(self, park):
        p = self.start('slow', park)
        self.wait_for('flashing')
        os.kill(p.pid, signal.SIGINT)
        return self.finish(p)

    def test_a_handled_interruption_with_a_verified_cleanup_releases(self):
        code, r, doc = self.interrupt('')
        self.assertEqual((code, r['status'], r['cleanup']['result'], doc['_hil']['termination']),
                         (1, 'failed', 'verified', 'aborted'))
        self.assertEqual(r['operations'][0]['attempts'][0]['kind'], 'interrupted')
        self.take()

    def test_a_handled_interruption_with_an_unresolved_cleanup_keeps_the_blocker(self):
        code, r, _ = self.interrupt('unverified')
        self.assertEqual((code, r['status'], r['check_status'], r['cleanup']['blocker']),
                         (4, 'blocked', 'failed', 'stale-record'))
        with self.assertRaises(lk.RecoveryBlocked):
            self.take()

    def test_abrupt_death_leaves_no_report_and_a_blocker(self):
        p = self.start('slow')
        self.wait_for('flashing')
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
                         (4, 'blocked', 'failed', 'not-run', 'cleanup-holder'))
        self.assertEqual(r['verification']['reasons'], ['flash not proven ended'])
        listed = [x['pid'] for rc in r['cleanup']['receipts'] for x in (rc['survivors'] or {}).get('processes', ())]
        self.assertIn(survivor, listed)
        with self.assertRaises(lk.BoardLocked):
            self.take()
        self.assertTrue(lk.is_cleanup_holder(lk.read_record('farmb')))
        with mock.patch.object(lk, 'RELEASE_WAIT', 0.3), self.assertRaisesRegex(lk.ReleaseRefused, 'left its records'):
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
        with mock.patch.object(lk, 'RELEASE_WAIT', 0.3), self.assertRaisesRegex(lk.ReleaseRefused, 'left its records'):
            lk.release_token(r['cleanup']['token'])
        with self.assertRaises(lk.BoardLocked):
            self.take()


if __name__ == '__main__':
    unittest.main()
