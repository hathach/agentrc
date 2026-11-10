"""skills/arduino/scripts/arduino_hil.py is the HIL harness mechanics an Arduino library's hil_test.py binds its profile
to: roster checks, build record admission (schema 2), DUT identity, flashing, the battery's lock and cleanup paths,
manual sessions, commissioning, verdicts and the report. Moved from Adafruit_TinyUSB_Arduino's hil_test.py (2989bca)
with its .claude/test/test_hil.py, run here against a fake library (tests/fixtures/arduino_lib) with agentrc's lock,
flash, process, report and USB modules and pyserial stubbed. Stubs prove the plumbing only; hardware behaviour is a
real run's."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the arduino engines are Linux only (fcntl)')

import argparse
import contextlib
import hashlib
import io
import json
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (claimant, guard_survivors, kill_quietly, killed_claimant, load_arduino_engine,  # noqa: E402
                     load_arduino_fixture, pid_alive, proc_stat, tmpdir, wait_done)

H = load_arduino_engine('skills/arduino/scripts/arduino_hil.py')
REAL = H.A   # agentrc's own modules, before a test stubs them
fake = load_arduino_fixture()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def setUpModule():
    guard_survivors(__name__)



class FakeLock:
    class BoardLocked(Exception):
        pass

    class RecoveryBlocked(Exception):
        pass

    class LockUnavailable(Exception):
        pass

    class PendingRefused(Exception):
        pass

    def __init__(self):
        self.taken, self.released, self.ended, self.logged, self.restored = [], [], [], [], []
        self.records, self.pending, self.failures, self.end_errors = {}, {}, {}, {}
        self.claims, self.running, self.settled = {}, set(), []   # claims: {board: [session]}, each its fake state
        self.judged = []
        lock = self

        class PendingSessions:
            def __init__(self, name, kind):
                self.key = (name, kind)

            def clear(self, ended):
                left = [s for s in lock.pending.get(self.key, []) if not ended(s)]
                if left:
                    raise FakeLock.PendingRefused(f'{left} not ended')
                lock.pending.pop(self.key, None)

            def claim(self, entry):   # the real record is hil_lock's, tested there
                listed = lock.claims.setdefault(self.key[0], [])
                others = list(listed)
                listed.append(entry)
                return others

            def remove(self, entry):
                listed = lock.claims.get(self.key[0], [])
                listed[:] = [s for s in listed if s['receipt'] != entry['receipt']]
                return listed
        self.PendingSessions = PendingSessions

    def claim_entry(self, ops, cmd):
        return {'receipt': {'ops': str(ops), 'pid': os.getpid(), 'start': '1'}, 'cmd': cmd}

    def claim_alive(self, session):
        return session.get('state') == 'alive'

    def invocation_state(self, session, keep=()):
        self.judged.append((session['receipt'], set(keep)))
        return session.get('state', 'ended'), session.get('receipts', [])

    logged_tokens = staticmethod(REAL.lock.logged_tokens)   # pure: hil_lock's own
    receipt_token = staticmethod(REAL.lock.receipt_token)
    open_runs = staticmethod(REAL.lock.open_runs)

    def settle(self, receipts, wait, busy=None):   # a receipt whose token is in `running` stays pending
        self.settled.append(list(receipts))
        return ['pending' if self.receipt_token(r) in self.running else 'unknown' for r in receipts], []

    def take_board_lock(self, board, reason, protected=False, **extra):
        self.taken.append((board, reason, protected, extra))
        fh = mock.Mock(name=f'fh-{board}')
        fh.cleanup_failures = self.open_cleanup_failures(board)
        return fh

    def release_board_lock(self, fh):
        self.released.append(fh)

    def end_failed(self, handles, reason, *, detail, receipts=(), retained=(), **extra):
        self.ended.append((list(handles), detail, list(receipts)))
        return {b: e for b, e in self.end_errors.items() if b in handles}

    def log_cleanup_failure(self, board, reason, *, detail, task=None, receipts=(), **extra):
        self.logged.append((board, detail, list(receipts)))
        if task is not None:
            self.failures.setdefault(board, []).append({'id': f'f{len(self.logged)}', 'task': task,
                                                        'receipts': list(receipts)})
        return {'event': 'failed', 'id': 'f2'}

    def open_cleanup_failures(self, board):
        return [f for f in self.failures.get(board, []) if (board, f.get('id')) not in self.restored]

    def resolve_restored(self, board, failures, **evidence):   # the real rule is hil_lock's, tested there
        notes = []
        for f in failures:
            if f.get('stays'):
                notes.append(f'{f["id"]} stays open')
            elif 'id' in f:
                self.restored.append((board, f['id']))
        return notes

    def failure_report(self, failures):
        out = {'previousCleanupFailure': failures[-1]} if failures else {}
        return {**out, 'openCleanupFailures': len(failures)} if len(failures) > 1 else out

    def cleanup_restored(self, board, failure_id, **evidence):
        self.restored.append((board, failure_id))

    def read_record(self, board):
        return self.records.get(board)

    def record_state(self, board):
        rec = self.records.get(board)
        return ('absent', None) if rec is None else ('unreadable', None) if rec == 'unreadable' else ('record', rec)

    def is_locked(self, board):   # as the real one: the record's holder, never the flock
        return self.holder_alive(self.records.get(board))

    def holder_alive(self, record):
        return isinstance(record, dict)


def fake_serial():
    """pyserial's surface the harness touches; each test sets Serial."""
    mod = types.ModuleType('serial')

    class SerialException(OSError):
        pass

    class SerialTimeoutException(SerialException):
        pass
    mod.SerialException, mod.SerialTimeoutException = SerialException, SerialTimeoutException
    mod.Serial = None
    return mod


def fake_tracker_init(self, run_dir):
    self.__dict__.update(receipts=['r1'], pending=[], unclean='', log=Path(run_dir) / 'hil.log',
                         state_dir=str(Path(run_dir) / 'tracked'))


class Base(unittest.TestCase):
    """The fake library bound to the harness, its roster at test/hil/ci.json, agentrc's modules stubbed."""

    def setUp(self):
        self.dir = tmpdir(self)
        self.root = fake.library(self.dir / 'lib')
        self.agentrc = fake.agentrc(self.dir / 'agentrc')
        self.BP = fake.build_profile(self.root, self.agentrc)
        self.HP = fake.hil_profile(self.BP)
        self.lock = FakeLock()
        self.A = argparse.Namespace(lock=self.lock, flash=mock.Mock(), process=mock.Mock(), report=mock.Mock(),
                                    usb=mock.Mock(), esp=mock.Mock(), root=self.agentrc['root'])
        self.A.usb.BUSPORT_RE = re.compile(r'^[0-9]+-[0-9]+(\.[0-9]+)*$')   # agentrc usb_observe's
        self.A.esp.return_value = mock.Mock(USB_LOCATION_MAX=15, USJ_VID_PID=('303a', '1001'))   # hil_openocd_esp's
        self.A.process.recheck_dir.return_value = ('done', [])
        for target, name, value in ((H, 'P', self.HP), (H.B, 'P', self.BP), (H, 'A', self.A)):
            self.patch(target, name, value)
        H.esptool_exe.cache_clear()
        self.addCleanup(H.esptool_exe.cache_clear)
        self.serial = fake_serial()
        old = sys.modules.get('serial')
        sys.modules['serial'] = self.serial
        self.addCleanup(lambda: sys.modules.__setitem__('serial', old) if old else sys.modules.pop('serial', None))
        self.roster = fake.roster(self.root / 'test/hil/ci.json')

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def board(self, name):
        return H.load_roster(self.roster)[name]

    def record(self, board='rig_jlink', **over):
        """A record for `board` whose images live under the checkout, bound to the current contract and agentrc."""
        rel = f'build/hil/{board}/echo/echo.ino.elf'
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_bytes(b'elf')
        rec = {'kind': 'arduino-hil-build', 'version': 2, 'library': fake.NAME, 'board': board, 'experimental': False,
               'fqbn': 'f', 'platform': {}, 'source': {'head': 'abc', 'dirty': False}, 'role': 'device',
               'roster': {'sha256': sha(self.roster.read_bytes())}, **H.B.contract_identity(),
               'sketches': {'echo': {'example': 'examples/Basic/echo', 'vidPid': '1234:5678', 'elf': rel, 'hex': None,
                                     'image': 'hex', 'sha256': {rel: sha(b'elf')}}}}
        rec.update(over)
        path = self.dir / 'rec.json'
        path.write_text(json.dumps(rec))
        return path

    def main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = H.main(argv)
        return code, json.loads(out.getvalue().strip().splitlines()[-1])


class Binding(Base):
    def test_bind_takes_the_build_profile_too_and_refuses_a_second(self):
        self.patch(H, 'P', None)
        self.patch(H.B, 'P', None)
        self.assertIs(H.bind(self.HP), H)
        self.assertEqual((H.P, H.B.P), (self.HP, self.BP))
        with self.assertRaisesRegex(RuntimeError, 'bound to another profile'):
            H.bind(fake.hil_profile(self.BP, REPORT_SCHEMA='other-hil'))
        self.assertIs(H.P, self.HP)
        H.P = None   # a HIL profile whose build profile is not the one arduino_build holds
        other_build = fake.build_profile(self.root, self.agentrc, LIBRARY_NAME='Other Library')
        with self.assertRaisesRegex(RuntimeError, 'arduino_build is bound to another profile'):
            H.bind(fake.hil_profile(other_build))
        self.assertEqual((H.P, H.B.P), (None, self.BP))

    def test_bind_refuses_disagreeing_profile_tables(self):
        self.patch(H, 'P', None)
        for kw, why in (({'RESTORE_TEST': {'device': 'inout', 'host': 'info'}}, "runs 'inout', not the first"),
                        ({'RESTORE_TEST': {'device': 'echo'}}, "RESTORE_TEST 'host' runs None"),
                        ({'TESTS': {'echo': 'echo', 'inout': 'inout', 'probe': 'probe'}}, 'union of ROLE_TESTS'),
                        ({'TEST_FUNCS': {'echo': lambda *a: (True, '')}}, 'TEST_FUNCS')):
            with self.subTest(kw=list(kw)), self.assertRaisesRegex(RuntimeError, why):
                H.bind(fake.hil_profile(self.BP, **kw))
            self.assertIsNone(H.P)

    def test_a_named_build_can_be_the_restoration_sketch(self):
        self.patch(H, 'P', None)
        self.patch(self.BP, 'HIL_SKETCHES', {**self.BP.HIL_SKETCHES, 'device': (
            {'echo_b': ('examples/Basic/echo', 'usb=b')}, *self.BP.HIL_SKETCHES['device'])})
        with self.assertRaisesRegex(RuntimeError, "runs 'echo', not the first HIL sketch 'echo_b'"):
            H.bind(fake.hil_profile(self.BP))
        H.bind(fake.hil_profile(self.BP, TESTS={**fake.hil_profile(self.BP).TESTS, 'echo': 'echo_b'}))
        self.assertEqual(H.restore_sketch('rig_ocd'), 'echo_b')
        seen = []
        with mock.patch.object(H, 'step', lambda name, board, image, test, *a: seen.append((image, test)) or
                               {'pass': True, 'detail': 'ok'}):
            H.restore('rig_ocd', {}, {'echo': 'echo image', 'echo_b': 'echo_b image'}, self.dir, None)
        self.assertEqual(seen, [('echo_b image', 'echo')])

    def test_budget_reserve_covers_the_restore(self):
        self.assertGreaterEqual(H.CLEANUP_RESERVE, H.FLASH_TIMEOUT + H.VERIFY_TIMEOUT + H.ENUM_TIMEOUT +
                                H.TEST_TIMEOUT)
        self.assertEqual(H.PENDING_KIND, 'arduino-hil')   # every library's older record, reported by each


class Roster(Base):
    def test_fake_roster_loads(self):
        self.assertEqual(sorted(H.load_roster(self.roster)), ['rig_esp', 'rig_host', 'rig_jlink', 'rig_ocd'])

    def test_refusals(self):
        esp_dbg = {'openocd': '-f board/esp32s3-builtin.cfg', 'tool': 'openocd-esp32', 'uid': 'M'}
        cases = (('rig_ocd', {'flasher': {'name': 'esptool', 'uid': 'x'}, 'usb_port': '1-1'}, 'ESP chip profile'),
                 ('rig_ocd', {'flasher': {'name': 'pyocd', 'uid': 'x'}}, 'flasher must be'),
                 ('rig_host', {'arduino': {'tests': ['echo']}}, 'not host tests'),
                 ('rig_ocd', {'arduino': {'tests': ['info']}}, 'not device tests'),
                 ('rig_esp', {'usb_port': None, 'no_serial': None}, 'needs usb_port'),
                 ('rig_ocd', {'no_serial': True}, 'no_serial needs usb_port'),
                 ('rig_esp', {'usb_port': '/dev/x'}, 'bus port'),
                 ('rig_ocd', {'debug': None}, None),   # absent debug: no GDB route, admitted
                 ('rig_ocd', {'debug': 'x'}, 'debug must be an object'),
                 ('rig_ocd', {'debug': {'openocd': 'x', 'tool': 'pyocd'}}, 'debug.tool is openocd'),
                 ('rig_esp', {'debug': {'openocd': '-f x', 'tool': 'openocd-esp32'}}, 'debug.uid'),
                 ('rig_esp', {'debug': {**esp_dbg, 'vid_pid': '0x303a 0x1001', 'usb_port': '11-1.2'}}, 'no vid_pid'),
                 ('rig_esp', {'debug': esp_dbg}, 'debug.usb_port'),
                 ('rig_esp', {'debug': {**esp_dbg, 'usb_port': '11-1.2.3.4.5.6.78'}}, 'no vid_pid'),   # > 16 chars
                 ('rig_esp', {'debug': {**esp_dbg, 'openocd': '-f board/esp32p4-builtin.cfg', 'usb_port': '11-1.2'}},
                  'no vid_pid'))
        for name, over, why in cases:
            path = fake.roster(self.dir / 'r.json', **{name: over})
            with self.subTest(name=name, over=over):
                if why is None:
                    H.load_roster(path)
                    continue
                with self.assertRaisesRegex(H.Refused, why):
                    H.load_roster(path)

    def test_a_host_board_needs_the_profiles_host_step(self):
        del self.HP.step_host
        with self.assertRaisesRegex(H.Refused, 'rig_host: a host board, and this harness has no host step'):
            H.load_roster(self.roster)

    def test_a_board_without_a_rig_profile_refused(self):
        data = json.loads(self.roster.read_text())
        data['boards'].append({'name': 'dev_a', 'flasher': {'name': 'jlink'}})
        self.roster.write_text(json.dumps(data))
        with self.assertRaisesRegex(H.Refused, 'no rig profile'):
            H.load_roster(self.roster)

    def test_the_profiles_board_check_runs_last(self):
        seen = []

        def check_board(name, b, path):
            seen.append(name)
            if name == 'rig_host':
                raise H.Refused(f'roster {path}: {name}: no dev_attached')
        self.HP.check_board = check_board
        with self.assertRaisesRegex(H.Refused, 'rig_host: no dev_attached'):
            H.load_roster(self.roster)
        self.assertEqual(seen, ['rig_jlink', 'rig_ocd', 'rig_host'])

    def test_selection_drops_boards_without_a_requested_test(self):
        roster = H.load_roster(self.roster)
        self.assertEqual(H.selection(roster, ['rig_ocd', 'rig_host'], ['echo']), {'rig_ocd': ['echo']})
        self.assertEqual(H.selection(roster, ['rig_host'], None), {'rig_host': ['info']})
        self.assertEqual(H.selection(roster, None, None)['rig_ocd'], ['echo', 'inout'])
        with self.assertRaisesRegex(H.Refused, 'no selected board'):
            H.selection(roster, ['rig_host'], ['echo'])
        with self.assertRaisesRegex(H.Refused, 'not in the roster'):
            H.selection(roster, ['nope'], None)


class Admission(Base):
    def admit(self, rec, head='abc', status='', **kw):
        with mock.patch.object(H, 'git_identity', return_value=(head, status)):
            return H.admit(rec, 'rig_jlink', self.roster, self.dir / 'run', **kw)

    def test_good_record_copies_images(self):
        _, images = self.admit(self.record())
        self.assertEqual(Path(images['echo']['elf']).read_bytes(), b'elf')
        self.assertTrue(images['echo']['elf'].startswith(str(self.dir / 'run')))
        self.assertEqual((images['echo']['vidPid'], images['echo']['hex']), ('1234:5678', None))
        self.assertEqual(images['echo']['fqbn'], json.loads(self.record().read_text())['fqbn'])   # no per-sketch one

    def test_two_named_builds_of_one_source_are_admitted_by_name(self):
        rec = self.record()
        data = json.loads(rec.read_text())
        rel = 'build/hil/rig_jlink/echo_b/echo.ino.elf'
        (self.root / rel).parent.mkdir(parents=True)
        (self.root / rel).write_bytes(b'elf b')
        data['sketches']['echo_b'] = {**data['sketches']['echo'], 'example': 'echo_b', 'source': 'examples/Basic/echo',
                                      'fqbn': 'f:usb=b', 'elf': rel, 'sha256': {rel: sha(b'elf b')}}
        rec.write_text(json.dumps(data))
        _, images = self.admit(rec)
        self.assertEqual({k: (Path(v['elf']).read_bytes(), Path(v['elf']).parent.name, v['fqbn'])
                          for k, v in images.items()},
                         {'echo': (b'elf', 'echo', 'f'), 'echo_b': (b'elf b', 'echo_b', 'f:usb=b')})

    def test_admission_sees_the_build_sides_worktree_status(self):
        # a core's dirty submodule hidden by an ignore setting: the build side reports it, admission refuses on it
        with mock.patch.object(H.B, 'worktree_status', return_value='~~ libraries/USB/nested\n'):
            self.assertEqual(H.git_identity()[1], '~~ libraries/USB/nested')
            with mock.patch.object(H, 'git_identity', return_value=('abc', H.B.worktree_status().strip())), \
                    self.assertRaisesRegex(H.Refused, 'changes since the record'):
                H.admit(self.record(), 'rig_jlink', self.roster, self.dir / 'run')

    def test_a_sketchs_own_fqbn_is_carried(self):
        rec = self.record()
        data = json.loads(rec.read_text())
        data['sketches']['echo']['fqbn'] = 'vend:arch:rig_jlink:usb=other'
        rec.write_text(json.dumps(data))
        _, images = self.admit(rec)
        self.assertEqual(images['echo']['fqbn'], 'vend:arch:rig_jlink:usb=other')

    def test_refusals(self):
        rec = self.record()
        (self.root / 'build/hil/rig_jlink/echo/echo.ino.elf').write_bytes(b'tampered')
        with self.assertRaisesRegex(H.Refused, 'changed since the record'):
            self.admit(rec)
        for over, why in (({'board': 'rig_ocd'}, 'not rig_jlink'),
                          ({'experimental': True}, 'experimental'),
                          ({'source': {'head': 'old'}}, 'built at old, the checkout is at abc: rebuild'),
                          ({'roster': {'sha256': '0'}}, 'changed since the record was built'),
                          ({'role': 'host'}, 'a host record, rig_jlink is a device board'),
                          ({'kind': 'other'}, 'not a version 2'),
                          ({'version': 1}, 'not a version 2 arduino-hil-build record: rebuild it with the build '
                                           'contract\'s --record'),
                          ({'library': 'Other Library'}, 'it is a Other Library record, this harness is Fake USB '
                                                         'Library\'s')):
            with self.subTest(over=over), self.assertRaisesRegex(H.Refused, why):
                self.admit(self.record(**over))

    def test_contract_file_drift_refused(self):
        rec = self.record()
        (self.root / 'contract/check.py').write_text('# edited\n')
        with self.assertRaisesRegex(H.Refused, r"the contract files \['contract/check.py'\] differ from the ones it "
                                               r"was built with: rebuild it"):
            self.admit(rec)
        (self.root / 'contract/check.py').write_text('# the build profile\n')
        self.admit(rec)
        (self.root / 'contract/pin.json').unlink()   # a file gone counts as differing too
        with self.assertRaisesRegex(H.Refused, 'contract/pin.json'):
            self.admit(rec)

    def test_agentrc_drift_refused(self):
        rec = self.record()
        lock = self.agentrc['root'] / 'skills/hil/scripts/hil_lock.py'
        loaded = lock.read_text()
        lock.write_text('# another agentrc\n')
        with self.assertRaisesRegex(H.Refused, f'built with other agentrc files \\(agentrc {"a" * 12}\\) than this '
                                               f'harness loads \\(agentrc {"a" * 12}\\): rebuild it'):
            self.admit(rec)
        with self.assertRaisesRegex(H.Refused, r"agentrc changed since this harness loaded it: "
                                               r"\['skills/hil/scripts/hil_lock.py'\]; rerun"):
            self.admit(rec)
        lock.write_text(loaded)
        self.agentrc['commit'] = 'b' * 40   # another commit with the same closure: the files decide
        self.admit(rec)

    def test_image_must_match_the_flasher(self):
        rec = self.record()
        data = json.loads(rec.read_text())
        data['sketches']['echo']['image'] = 'elf'
        rec.write_text(json.dumps(data))
        with self.assertRaisesRegex(H.Refused, 'a jlink board flashes the hex'):
            self.admit(rec)

    def test_dirty_checkout_refused(self):
        with self.assertRaisesRegex(H.Refused, 'changes since'):
            self.admit(self.record(), status=' M src/fake.h')

    def test_experimental_allowed_for_flash(self):
        self.admit(self.record(experimental=True), status=' M src/fake.h', experimental_ok=True)   # dirty tree
        with self.assertRaisesRegex(H.Refused, 'rebuild'):
            self.admit(self.record(experimental=True, source={'head': 'other'}), experimental_ok=True)

    def test_staged_head_must_match_manifest(self):
        rec = self.record()
        with self.assertRaisesRegex(H.Refused, 'the staged tree is at zzz'):
            self.admit(rec, manifest={'head': 'zzz'})
        self.admit(rec, head=None, manifest={'head': 'abc'})

    def test_staged_manifest_digest_mismatch(self):
        m = self.dir / 'manifest.json'
        m.write_text(json.dumps({'head': 'abc', 'files': {'src/fake.h': sha(b'y')}}))
        with self.assertRaisesRegex(H.Refused, 'differ from the manifest'):
            H.verify_staged(m)
        m.write_text(json.dumps({'head': 'abc', 'files': {'src/fake.h': sha((self.root / 'src/fake.h').read_bytes())}}))
        self.assertEqual(H.verify_staged(m)['head'], 'abc')

    def test_staged_paths_outside_the_tree_or_through_links_are_refused_before_hashing(self):
        """agentrc#472: the check every staged run (hil_remote's, CI's) goes through, not only CI admission."""
        m = self.dir / 'manifest.json'
        good = {'src/fake.h': sha((self.root / 'src/fake.h').read_bytes())}
        outside = self.dir / 'outside'
        outside.mkdir()
        (outside / 'fake.h').write_bytes((self.root / 'src/fake.h').read_bytes())
        (self.root / 'parent-link').symlink_to(outside, target_is_directory=True)
        (self.root / 'file-link.h').symlink_to(self.root / 'src/fake.h')
        digest = good['src/fake.h']
        cases = {'/etc/passwd': 'outside', '../escape': 'outside', 'src/../../escape': 'outside',
                 'C:/x': 'outside', 'C:x': 'outside', '\\\\host\\share\\x': 'outside', '..\\escape': 'outside',
                 '': 'outside', 'parent-link/fake.h': 'through links', 'file-link.h': 'through links'}
        hashed = mock.Mock(side_effect=AssertionError('hashed before the path check'))
        for rel, why in cases.items():
            with self.subTest(rel=rel), mock.patch.object(H.B, 'sha256', hashed):
                m.write_text(json.dumps({'head': 'abc', 'files': {**good, rel: digest}}))
                with self.assertRaisesRegex(H.Refused, why):
                    H.verify_staged(m)
        for files in ([], {'src/fake.h': 7}, None):
            with self.subTest(files=files):
                m.write_text(json.dumps({'head': 'abc', 'files': files}))
                with self.assertRaisesRegex(H.Refused, 'malformed|no JSON object'):
                    H.verify_staged(m)
        m.write_text(json.dumps({'head': 'abc', 'files': good}))
        self.assertEqual(H.verify_staged(m)['files'], good)

    def test_record_problems_without_a_head_skip_the_head_check(self):
        rec = json.loads(self.record(source={'head': 'old'}).read_text())
        self.assertEqual(H.record_problems(rec, 'rig_jlink', None, False, 'HEAD'), [])


class Identity(Base):
    def device(self, busport, ino, serial):
        d = self.dir / f'dev{ino}'
        d.mkdir()
        if serial:   # an iSerialNumber naming an empty string: the kernel then makes no `serial`
            (d / 'serial').write_text(serial + '\n')
        return {'busport': busport, 'ino': ino, 'dir': str(d)}

    def test_no_serial_board_is_the_serial_less_device_at_its_port(self):
        esp = self.board('rig_esp')
        app, rom = self.device('11-1.3', 5, ''), self.device('11-1.3', 7, '84:F7:03:C0:84:E4')   # the ROM USJ's MAC
        gone = {'busport': '11-1.3', 'ino': 8, 'dir': str(self.dir / 'gone')}
        self.A.usb.usb_devices.return_value = [app, rom, gone, self.device('11-1.1', 6, '')]
        self.A.usb.enumeration_id = lambda d: int(Path(d).name[3:]) if Path(d).exists() else None   # as recorded
        self.assertEqual(H.dut_devices('303a:1001', esp), [app])
        self.assertEqual(self.A.usb.usb_devices.call_args[1], {'vid_pid': ('303a', '1001')})
        self.A.usb.usb_scan.assert_not_called()
        self.assertEqual(H.enumeration_of('303a:1001', esp), 5)
        self.A.usb.enumeration_id = lambda d: 99   # re-enumerated since the record was taken
        self.assertEqual(H.dut_devices('303a:1001', esp), [])
        self.assertEqual(H.dut_name(esp), 'at 11-1.3 with no serial')

    def test_serial_board_is_its_serial_and_its_usb_port_when_set(self):
        devs = [{'busport': '21-1.3', 'ino': 1, 'serial': 'OUID'}, {'busport': '21-1.4', 'ino': 2, 'serial': 'OUID'}]
        self.A.usb.usb_scan.return_value = devs
        ocd = self.board('rig_ocd')
        self.assertEqual(H.dut_devices('239a:cafe', ocd), devs)
        self.assertEqual(self.A.usb.usb_scan.call_args[1], {'vid_pid': ('239a', 'cafe'), 'serial': 'OUID'})
        self.assertIsNone(H.enumeration_of('239a:cafe', ocd))   # ambiguous: no snapshot
        self.assertEqual(H.dut_name(ocd), 'with serial OUID')
        ocd = {**ocd, 'usb_port': '21-1.4'}
        self.assertEqual(H.dut_devices('239a:cafe', ocd), [devs[1]])
        self.assertEqual(H.enumeration_of('239a:cafe', ocd), 2)
        self.assertEqual(H.dut_name(ocd), 'with serial OUID at 21-1.4')
        self.A.usb.usb_devices.assert_not_called()

    def test_wait_enumerated_takes_a_new_enumeration_stable_for_a_second(self):
        now = [0.0]

        def tick():
            now[0] += 0.25
            return now[0]
        seq = iter([[{'ino': 1}], [{'ino': 2}], [{'ino': 2}, {'ino': 3}]] + [[{'ino': 3}]] * 20)
        with mock.patch.object(H, 'dut_devices', lambda *a: next(seq)), mock.patch.object(H.time, 'sleep'), \
                mock.patch.object(H.time, 'monotonic', tick):
            self.assertEqual(H.wait_enumerated('1:2', {}, 1), {'ino': 3})
        with mock.patch.object(H, 'dut_devices', lambda *a: [{'ino': 1}]), mock.patch.object(H.time, 'sleep'), \
                mock.patch.object(H.time, 'monotonic', tick):
            self.assertIsNone(H.wait_enumerated('1:2', {}, 1))   # only the old enumeration

    def test_usb_serial_jtag_is_checked_at_its_roster_port(self):
        sysfs = self.dir / 'sys'
        usj = sysfs / '11-1.2'
        usj.mkdir(parents=True)
        (usj / 'idVendor').write_text('303a\n')
        (usj / 'idProduct').write_text('1001\n')
        self.A.usb.SYS_USB_DEVICES = str(sysfs)
        self.A.usb.enumeration_id = lambda d: 7 if Path(d).exists() else None
        dbg = {'uid': '60:55:F9', 'usb_port': '11-1.2'}
        self.A.usb.read_sysfs = lambda p: '60:55:F9'
        self.assertEqual(H.usj_at(dbg), '11-1.2')
        for serial, port in (('84:F7:03', '11-1.2'), (None, '11-1.2'), ('60:55:F9', '11-1.3')):
            self.A.usb.read_sysfs = lambda p, s=serial: s
            with self.subTest(serial=serial, port=port), self.assertRaisesRegex(RuntimeError, 'no USB-Serial/JTAG'):
                H.usj_at({**dbg, 'usb_port': port})
        ids = iter([7, 8])   # re-enumerated during the check
        self.A.usb.enumeration_id = lambda d: next(ids)
        self.A.usb.read_sysfs = lambda p: '60:55:F9'
        with self.assertRaisesRegex(RuntimeError, 'no USB-Serial/JTAG'):
            H.usj_at(dbg)

    def test_probe_presence(self):
        usb = self.A.usb
        usb.jlink_serial_candidates = lambda uid, serials: [k for k, v in serials.items() if v.lstrip('0') == uid]
        usb.usb_scan.return_value = [{'busport': '11-2.4', 'serial': '000000123456'}]
        self.assertEqual(H.probe_present({'name': 'jlink', 'uid': '123456'}), '')
        self.assertEqual(usb.usb_scan.call_args[1], {'vid': '1366'})
        usb.usb_scan.return_value = []
        self.assertIn('not on this host', H.probe_present({'name': 'jlink', 'uid': '123456'}))
        usb.usb_scan.return_value = [{'busport': '21-1.4', 'serial': 'E66'}, {'busport': '21-1.5', 'serial': 'E66'}]
        self.assertIn('ambiguous', H.probe_present(self.board('rig_ocd')['flasher']))
        self.assertEqual(usb.usb_scan.call_args[1], {'vid_pid': ('2e8a', '000c'), 'serial': 'E66'})


class Esp(Base):
    def image(self, segments=2):
        d = self.dir / 'img'
        d.mkdir(exist_ok=True)
        (d / 'flash_args').write_text('--flash-mode dio\n0x0 b.bin\n0x10000 a.bin\n')
        return {'dir': str(d), 'chip': 'esp32s3', 'segments': [[0, 'b.bin'], [0x10000, 'a.bin']][:segments],
                'elf': str(d / 'a.elf'), 'vidPid': '303a:1001'}

    def test_flash_runs_flash_args_and_counts_verified_images(self):
        f = self.A.flash
        f.esptool_input_problem.return_value, f.esptool_problem.return_value = '', ''
        f.esptool_port.return_value, f.esptool_runtime.return_value = '/dev/ttyUSB1', {'exe': '/opt/esptool'}
        f.flash_esptool.return_value = mock.Mock(stdout='Hash of data verified.\nHash of data verified.\n', stderr='')
        tracker = mock.Mock()
        flasher = self.board('rig_esp')['flasher']
        self.assertEqual(H.flash(flasher, self.image(), self.dir, tracker), '')
        kw = f.flash_esptool.call_args[1]
        self.assertEqual((kw['chip'], kw['port'], kw['flash_args'], kw['esptool']),
                         ('esp32s3', '/dev/ttyUSB1', '--flash-mode dio 0x0 b.bin 0x10000 a.bin', '/opt/esptool'))
        f.flash_esptool.return_value.stdout = 'Hash of data verified.\n'
        self.assertEqual(H.flash(flasher, self.image(), self.dir, tracker), 'esptool verified 1 of 2 images')
        f.esptool_problem.return_value = "exit 0, but the output says 'Chip was NOT reset'"
        self.assertIn('NOT reset', H.flash(flasher, self.image(), self.dir, tracker))
        f.esptool_input_problem.return_value = 'flash_args names b.bin, which is missing'
        f.flash_esptool.reset_mock()
        self.assertIn('esptool inputs', H.flash(flasher, self.image(), self.dir, tracker))
        f.flash_esptool.assert_not_called()
        f.esptool_input_problem.return_value, f.esptool_port.side_effect = '', RuntimeError('want one link')
        self.assertIn('want one link', H.flash(flasher, self.image(), self.dir, tracker))

    def test_probe_presence_and_reset_through_the_bridge(self):
        flasher = {'name': 'esptool', 'uid': '3ea6'}
        self.A.flash.esptool_port.side_effect = RuntimeError('found none')
        self.assertEqual(H.probe_present(flasher), 'found none')
        self.A.flash.esptool_port.side_effect, self.A.flash.esptool_port.return_value = None, '/dev/ttyUSB1'
        self.assertEqual(H.probe_present(flasher), '')
        self.A.flash.esptool_runtime.return_value, self.A.flash.esptool_problem.return_value = {'exe': 'e'}, ''
        self.assertEqual(H.reset(flasher, mock.Mock()), '')
        self.assertEqual(self.A.flash.reset_esptool.call_args[1]['port'], '/dev/ttyUSB1')
        H.reset(flasher, mock.Mock())
        self.assertEqual(self.A.flash.esptool_runtime.call_count, 1)   # identity once per invocation
        self.assertEqual(self.A.flash.esptool_port.call_count, 4)      # the tty afresh each time

    def test_gdb_runs_espressif_openocd_on_the_usb_serial_jtag(self):
        which = mock.patch.object(H.shutil, 'which', return_value='/usr/local/bin/xtensa-esp32s3-elf-gdb')
        which.start()
        self.addCleanup(which.stop)
        esp = self.A.esp()
        esp.admit.return_value = ('esp32s3', ['board/esp32s3-builtin.cfg'])
        esp.find_openocd.return_value = {'exe': '/o/bin/openocd', 'scripts': '/o/scripts'}
        self.A.esp = lambda: esp
        board = self.board('rig_esp')
        with mock.patch.object(H, 'usj_at', return_value='11-1.2'):
            pipe = shlex.split(H.gdb_argv('rig_esp', board, 'fw.elf', [])[4].removeprefix('target extended-remote | '))
        esp.usj_location.assert_not_called()   # it refuses while a serial-less DUT is on the bus
        self.assertEqual(esp.admit.call_args[0][0], {'name': 'openocd_esp', 'uid': '60:55:F9',
                                                     'args': '-f board/esp32s3-builtin.cfg'})
        self.assertEqual(esp.find_openocd.call_args[0][0], 'openocd-esp32')
        esp.check_cfgs.assert_called_once_with('esp32s3', ['board/esp32s3-builtin.cfg'], '/o/scripts')
        flash_size, board_cfg = pipe.index('set ESP_FLASH_SIZE 0'), pipe.index('/o/scripts/board/esp32s3-builtin.cfg')
        self.assertEqual(pipe[:3], ['/o/bin/openocd', '-s', '/o/scripts'])
        self.assertLess(flash_size, board_cfg)   # read when the target cfg is sourced
        self.assertEqual(pipe[board_cfg + 1:board_cfg + 5], ['-c', 'adapter usb location 11-1.2',
                                                             '-c', 'adapter serial 60:55:F9'])
        self.assertFalse(any('vid_pid' in w for w in pipe))
        self.assertIn('tcl port disabled', pipe)
        self.assertIn('foreach t [target names] { $t configure -event gdb-detach { resume } }', pipe)
        esp.admit.return_value = ('esp32p4', ['board/esp32p4-builtin.cfg'])
        with self.assertRaisesRegex(ValueError, 'names esp32p4'):
            H.gdb_argv('rig_esp', board, 'fw.elf', [])

    def test_gdb_on_an_espressif_route_is_the_chips(self):
        esp = self.A.esp()
        esp.find_openocd.return_value = {'exe': '/o/bin/openocd', 'scripts': '/o/scripts'}
        self.A.esp = lambda: esp
        board = self.board('rig_esp')
        for chip, gdb in (('esp32s3', 'xtensa-esp32s3-elf-gdb'), ('esp32', 'xtensa-esp32-elf-gdb'),
                          ('esp32p4', 'riscv32-esp-elf-gdb'), ('esp32c6', 'riscv32-esp-elf-gdb')):
            esp.admit.return_value = (chip, [f'board/{chip}-builtin.cfg'])
            with self.subTest(chip), mock.patch.dict(H.B.P.BOARDS['rig_esp'], chip=chip), \
                    mock.patch.object(H, 'usj_at', return_value='11-1.2'), \
                    mock.patch.object(H.shutil, 'which', return_value=f'/usr/local/bin/{gdb}'):
                self.assertEqual(H.gdb_argv('rig_esp', board, 'fw.elf', [])[0], gdb)
        with mock.patch.object(H, 'usj_at', return_value='11-1.2'), mock.patch.object(H.shutil, 'which',
                                                                                      return_value=None), \
                self.assertRaisesRegex(RuntimeError, 'xtensa-esp32s3-elf-gdb, the esp32s3 gdb, is not on PATH'):
            esp.admit.return_value = ('esp32s3', ['board/esp32s3-builtin.cfg'])
            H.gdb_argv('rig_esp', board, 'fw.elf', [])
        esp.admit.return_value = ('esp32c2', ['board/esp32c2-builtin.cfg'])
        with mock.patch.dict(H.B.P.BOARDS['rig_esp'], chip='esp32c2'), \
                mock.patch.object(H, 'usj_at', return_value='11-1.2'), \
                self.assertRaisesRegex(RuntimeError, 'no Espressif gdb listed for esp32c2'):
            H.gdb_argv('rig_esp', board, 'fw.elf', [])

    def test_flash_names_what_the_flasher_wrote(self):
        image = {'elf': '/r/echo.ino.elf', 'hex': '/r/echo.ino.hex', 'segments': [[0, 'a'], [1, 'b']]}
        self.assertEqual([H.written({'name': n}, image) for n in ('openocd', 'jlink', 'esptool')],
                         ['echo.ino.elf', 'echo.ino.hex', '2 flash_args images'])
        dev = {'busport': '9-1.4', 'devnum': 95, 'ino': 3, 'after': 1.9}
        with mock.patch.object(H, 'flash_and_enumerate', return_value=('', 'n/a', dev)):
            row = H.flash_only('rig_jlink', self.board('rig_jlink'), {**image, 'vidPid': '1234:5678'}, self.dir, None)
        self.assertEqual((row['detail'], row['device']), ('flashed echo.ino.hex; enumerated as 1234:5678 at 9-1.4',
                                                          {'busport': '9-1.4', 'devnum': 95, 'after': 1.9}))
        with mock.patch.object(H, 'flash_and_enumerate', return_value=('', 'n/a', None)):
            row = H.flash_only('rig_jlink', self.board('rig_jlink'), {**image, 'vidPid': '1234:5678'}, self.dir, None)
        self.assertEqual(row['pass'], True)
        self.assertIn('the image may not run USB', row['detail'])
        with mock.patch.object(H, 'flash', return_value=''):
            row = H.flash_only('rig_host', self.board('rig_host'), image, self.dir, None)
        self.assertEqual(row, {'pass': True, 'detail': 'flashed echo.ino.elf (a host board: read its console)'})

    def test_admit_carries_the_flash_inputs(self):
        b = self.root / 'build/hil/rig_esp/echo'
        b.mkdir(parents=True)
        files = {'echo.ino.elf': b'elf', 'flash_args': b'--x y\n0x0 a.bin\n', 'a.bin': b'app'}
        for n, data in files.items():
            (b / n).write_bytes(data)
        rel = 'build/hil/rig_esp/echo/'
        path = self.record(board='rig_esp', chip='esp32s3', sketches={'echo': {
            'image': 'esptool', 'elf': rel + 'echo.ino.elf', 'hex': None, 'vidPid': '303a:1001',
            'segments': [[0, 'a.bin']], 'sha256': {rel + n: sha(d) for n, d in files.items()}}})
        with mock.patch.object(H, 'git_identity', return_value=('abc', '')):
            _, images = H.admit(path, 'rig_esp', self.roster, self.dir / 'run')
        img = images['echo']
        self.assertEqual((img['chip'], img['segments']), ('esp32s3', [[0, 'a.bin']]))
        self.assertEqual(sorted(p.name for p in Path(img['dir']).iterdir()), sorted(files))


class Console(Base):
    def capture(self, board_name, reset, fail=False, pending=()):
        board = self.board(board_name)
        calls, reads = [], []

        class Port:
            def __init__(self, tty, baud, timeout):
                calls.append(('open', tty, baud))

            def __setattr__(self, name, value):
                calls.append((name, value))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                calls.append('close')

            def reset_input_buffer(self):
                pass

            def read(self, n):
                reads.append(n)
                if fail and len(reads) > 1:
                    raise fail
                return b'boot\n' if len(calls) < 6 else b''
        self.serial.Serial = Port
        now = [0.0]

        def tick():
            now[0] += 1
            return now[0]
        args = argparse.Namespace(board=board_name, reset=reset, baud=115200, seconds=10)
        with mock.patch.object(H, 'console_tty', return_value='/dev/ttyX'), \
                mock.patch.object(H, 'reset', lambda *a: calls.append('reset') or ''), \
                mock.patch.object(H.time, 'monotonic', tick), mock.patch.object(H.time, 'sleep'):
            row = H.op_console(args, board, self.dir, mock.Mock(pending=list(pending)), None)
        return row, calls

    def test_probe_reset_happens_with_the_console_open(self):
        row, calls = self.capture('rig_host', True)
        self.assertEqual(calls[:2], [('open', '/dev/ttyX', 115200), 'reset'])
        self.assertTrue(row['pass'])
        self.assertIn('boot', (self.dir / 'console.log').read_text())

    def test_detach_family_console_reset_detaches_first(self):
        detached = []
        with mock.patch.object(H, 'detach', lambda *a: detached.append(a[2]) or 'detached'):
            _, calls = self.capture('rig_ocd', True)
        self.assertEqual((detached, calls[:2]), ([None], [('open', '/dev/ttyX', 115200), 'reset']))
        with mock.patch.object(H, 'detach', return_value='still enumerated 5s after the detach'):
            row, calls = self.capture('rig_ocd', True)
        self.assertEqual((row['pass'], 'reset' in calls), (False, False))
        self.assertIn('detach: still enumerated', row['detail'])

    def test_esptool_reset_pulses_en_with_the_bridge_open(self):
        row, calls = self.capture('rig_esp', True)
        self.assertEqual(calls[:4], [('open', '/dev/ttyX', 115200), ('dtr', False), ('rts', True), ('rts', False)])
        self.assertNotIn('reset', calls)
        times = [json.loads(x) for x in (self.dir / 'console.times').read_text().splitlines()]
        self.assertTrue(times[0]['reset'])
        self.assertEqual([t['len'] for t in times[1:]], [5] * (len(times) - 1))
        _, calls = self.capture('rig_esp', False)
        self.assertFalse([c for c in calls if c[0] in ('dtr', 'rts')])

    def test_serial_error_keeps_the_capture_and_fails(self):
        for error in (self.serial.SerialException('device disconnected'), OSError(5, 'device disconnected')):
            row, _ = self.capture('rig_host', False, fail=error)
            self.assertFalse(row['pass'])
            self.assertIn('device disconnected', row['detail'])
            self.assertIn('boot', (self.dir / 'console.log').read_text())

    def test_interrupt_keeps_the_capture(self):
        row, _ = self.capture('rig_host', False, fail=KeyboardInterrupt())
        self.assertEqual((row['pass'], row['detail']), (False, '/dev/ttyX: interrupted'))
        self.assertEqual((self.dir / 'console.log').read_text(), 'boot\n')
        (self.dir / 'console.log').unlink()
        with self.assertRaises(KeyboardInterrupt):   # a pending child: _manual records it
            self.capture('rig_host', False, fail=KeyboardInterrupt(), pending=[{'receipt': 'r'}])
        self.assertEqual((self.dir / 'console.log').read_text(), 'boot\n')

    def test_console_route(self):
        self.A.usb.usb_scan.return_value = [{'busport': '21-2.3', 'dir': '/sys/x'}]
        with mock.patch.object(H, 'node', return_value='/dev/ttyACM19') as node:
            self.assertEqual(H.console_tty(self.board('rig_host'), 5), '/dev/ttyACM19')
        self.assertEqual(self.A.usb.usb_scan.call_args[1], {'vid_pid': ('2e8a', '000c'), 'serial': 'H66'})
        self.assertEqual(node.call_args[0][1], '*:*/tty/tty*')
        self.A.usb.usb_scan.return_value = []
        with self.assertRaisesRegex(RuntimeError, 'want one on the bus, found 0'):
            H.console_tty(self.board('rig_host'), 5)
        self.A.flash.esptool_port.return_value = '/dev/ttyUSB1'
        self.assertEqual(H.console_tty(self.board('rig_esp'), 5), '/dev/ttyUSB1')
        with self.assertRaisesRegex(RuntimeError, 'no console route'):
            H.console_tty(self.board('rig_jlink'), 5)

    def test_check_is_a_session_command(self):
        with mock.patch.object(H, 'manual', return_value=0) as manual, mock.patch.object(H.signal, 'signal'):
            H.main(['check', '-b', 'rig_ocd', '--under-hold', 'x', '--build-record', 'r.json', 'ci.json'])
        self.assertEqual((manual.call_args[0][0].cmd, manual.call_args[0][0].test), ('check', None))

    def test_bounds(self):
        for argv, why in ((['console', '-b', 'b', '--under-hold', 'x', '--seconds', '500', 'r.json'], '--seconds'),
                          (['console', '-b', 'b', '--under-hold', 'x', '--baud', '-1', 'r.json'], '--baud must be '
                                                                                                 'positive'),
                          (['exec', '-b', 'b', '--under-hold', 'r', '--timeout', '601', 'ci.json', 'true'],
                           '--timeout')):
            code, out = self.main(argv)
            self.assertEqual(code, 2)
            self.assertIn(why, out['error'])


class Detach(Base):
    def device(self, busport, ino, serial=None):
        """A device dir; serial None: no `serial` file, as for an enumeration without iSerialNumber."""
        d = self.dir / f'dev{ino}'
        d.mkdir()
        if serial is not None:
            (d / 'serial').write_text(serial + '\n')
        return {'busport': busport, 'ino': ino, 'dir': str(d)}

    def wait(self, board, vid_pid, polls, serials):
        """detach over the usb_devices lists in `polls`, each serial read from `serials` (None: inconclusive)."""
        self.A.flash.run_openocd_commands.return_value = mock.Mock(returncode=0, stdout=b'', stderr=b'')
        self.A.usb.usb_devices.side_effect = polls
        self.A.usb.read_sysfs = lambda path: serials.get(Path(path).parent.name)
        self.A.usb.enumeration_id = lambda d: int(Path(d).name[3:])
        now = [0.0]

        def tick():
            now[0] += 1
            return now[0]
        with mock.patch.object(H.time, 'sleep'), mock.patch.object(H.time, 'monotonic', tick):
            return H.detach('rig_ocd', board, vid_pid, mock.Mock())

    def test_detach_only_where_the_flasher_targets_an_rp_chip(self):
        self.A.flash.run_openocd_commands.return_value = mock.Mock(returncode=0, stdout='', stderr='')
        self.A.usb.usb_devices.return_value = []
        board = self.board('rig_ocd')
        self.assertEqual(H.detach('rig_ocd', board, '239a:cafe', mock.Mock()), 'detached')
        self.assertEqual(self.A.flash.run_openocd_commands.call_args[0][1], ['mww 0x5011304c 0x00010000'])
        board['flasher']['args'] = board['flasher']['args'].replace('rp2040', 'rp2350')
        self.assertEqual(H.detach('rig_ocd', board, '239a:cafe', mock.Mock()), 'detached')
        self.assertEqual(self.A.flash.run_openocd_commands.call_args[0][1], ['mww 0x5011304c 0x00010000'])
        self.A.flash.run_openocd_commands.reset_mock()
        self.assertEqual(H.detach('rig_jlink', self.board('rig_jlink'), '1234:5678', mock.Mock()), '')
        self.assertEqual(H.detach('rig_host', self.board('rig_host'), None, mock.Mock()), '')   # a host board
        board['flasher']['args'] = '-f interface/cmsis-dap.cfg -f target/atsame5x.cfg'   # another chip
        self.assertEqual(H.detach('rig_ocd', board, '239a:cafe', mock.Mock()), '')
        self.A.flash.run_openocd_commands.assert_not_called()

    def test_detach_without_a_record_waits_for_the_serial(self):
        dut, hub, other = self.device('21-1.3', 1, 'OUID'), self.device('21-1', 2), self.device('21-1.4', 3, 'X')
        serials = {'dev1': 'ouid', 'dev3': 'X'}   # serial compared case aside
        self.assertEqual(self.wait(self.board('rig_ocd'), None, [[hub, dut, other], [hub, other]], serials),
                         'detached')
        self.assertEqual(self.A.usb.usb_devices.call_args, mock.call())   # any VID: no record names one
        self.A.usb.usb_scan.assert_not_called()

    def test_an_inconclusive_serial_read_is_no_detach(self):
        dut = self.device('21-1.3', 1, 'OUID')
        polls = [[dut]] * 10
        self.assertEqual(self.wait(self.board('rig_ocd'), '239a:cafe', polls, {}),
                         f'still enumerated {H.DETACH_WAIT}s after the detach')
        self.assertEqual(self.A.usb.usb_devices.call_args[1], {'vid_pid': ('239a', 'cafe')})
        self.A.usb.usb_devices.side_effect = None
        self.A.usb.enumeration_id = lambda d: 99   # read across a re-enumeration
        self.A.usb.read_sysfs = lambda path: 'X'
        self.A.usb.usb_devices.return_value = [dut]
        self.assertFalse(H.dut_absent('239a:cafe', self.board('rig_ocd')))

    def test_a_missing_serial_holds_a_scoped_wait(self):
        board = self.board('rig_ocd')
        dut = self.device('21-1.3', 1)   # its serial string not cached, or its enumeration still being added
        self.assertEqual(self.wait(board, '239a:cafe', [[dut]] * 10, {}),
                         f'still enumerated {H.DETACH_WAIT}s after the detach')
        self.assertEqual(self.wait({**board, 'usb_port': '21-1.3'}, None, [[dut]] * 10, {}),
                         f'still enumerated {H.DETACH_WAIT}s after the detach')

    def test_usb_port_scopes_the_wait(self):
        board = {**self.board('rig_ocd'), 'usb_port': '21-1.3'}
        dut, peer = self.device('21-1.3', 1, 'OUID'), self.device('21-1.4', 2, 'OUID')
        self.assertEqual(self.wait(board, '239a:cafe', [[dut, peer], [peer]], {'dev1': 'OUID'}), 'detached')
        self.assertEqual(self.wait(board, '239a:cafe', [[dut, peer]] * 10, {'dev2': 'OUID'}),
                         f'still enumerated {H.DETACH_WAIT}s after the detach')   # dut's read inconclusive

    def test_no_serial_board_waits_for_its_port_to_empty(self):
        board = {**self.board('rig_ocd'), 'usb_port': '21-1.3', 'no_serial': True}
        dut, peer = self.device('21-1.3', 1), self.device('21-1.4', 2)
        self.assertEqual(self.wait(board, None, [[dut, peer], [peer]], {}), 'detached')
        self.assertEqual(self.wait(board, None, [[dut, peer]] * 10, {}),
                         f'still enumerated {H.DETACH_WAIT}s after the detach')

    def test_a_failed_detach_is_evidence(self):
        self.A.flash.run_openocd_commands.return_value = mock.Mock(returncode=0, stdout='Error: x', stderr='')
        self.assertEqual(H.detach('rig_ocd', self.board('rig_ocd'), '239a:cafe', mock.Mock()),
                         'detach session failed (exit 0)')


class Flash(Base):
    def test_openocd_transcript_must_show_verify(self):
        op = mock.Mock(returncode=0, stdout='** Programming Finished **\n', stderr='', cleanup='done')
        self.A.flash.flash_openocd.return_value = op
        tracker = mock.Mock()
        self.assertIn('Verified OK', H.flash({'name': 'openocd', 'verify': True}, {'elf': 'x.elf'}, self.dir, tracker))
        op.stdout += '** Verified OK **\n'
        self.assertEqual(H.flash({'name': 'openocd', 'verify': True}, {'elf': 'x.elf'}, self.dir, tracker), '')
        op.returncode = 1
        self.assertIn('openocd exit 1', H.flash({'name': 'openocd'}, {'elf': 'x.elf'}, self.dir, tracker))

    def test_jlink_verifies_every_hex_run(self):
        hexfile = self.dir / 'a.hex'

        def rec(addr, data):
            r = bytes([len(data), addr >> 8, addr & 0xff, 0]) + data
            return ':' + (r + bytes([(-sum(r)) & 0xff])).hex().upper()
        hexfile.write_text('\n'.join([rec(0x4000, b'\x01\x02\x03\x04'), rec(0x5000, b'\x05\x06\x07\x08'),
                                     ':00000001FF']) + '\n')
        self.A.flash.jlink_problem.return_value = ''
        why = H.flash({'name': 'jlink', 'args': '-device ATSAMD51J19'}, {'hex': str(hexfile)}, self.dir, mock.Mock())
        self.assertEqual(why, '')
        cmds = self.A.flash.run_jlink_commands.call_args[0][1]
        self.assertEqual([c.split(',')[1] for c in cmds[:2]], ['0x4000', '0x5000'])
        self.assertEqual(cmds[2:], ['r', 'go'])
        self.assertEqual(self.A.flash.run_jlink_commands.call_args[1]['device'], 'ATSAMD51J19')
        self.assertEqual(H.flash({'name': 'jlink'}, {'hex': None}, self.dir, mock.Mock()), 'no HEX for a J-Link flash')

    def test_flash_and_enumerate_keeps_snapshot_detach_flash_wait_order(self):
        calls = []
        with mock.patch.object(H, 'enumeration_of', lambda *a: calls.append('snapshot') or 'ino1'), \
                mock.patch.object(H, 'detach', lambda *a: calls.append('detach') or 'detached'), \
                mock.patch.object(H, 'flash', lambda *a: calls.append('flash') or ''), \
                mock.patch.object(H, 'wait_enumerated', lambda v, u, before: calls.append(('wait', before)) or {}):
            self.assertEqual(H.flash_and_enumerate('b', {'uid': 'U', 'flasher': {}}, {'vidPid': '1:2'}, self.dir,
                                                   None), ('', 'detached', {}))
        self.assertEqual(calls, ['snapshot', 'detach', 'flash', ('wait', 'ino1')])
        with mock.patch.object(H, 'enumeration_of', return_value=None), mock.patch.object(H, 'detach'), \
                mock.patch.object(H, 'flash', return_value='openocd exit 1'), \
                mock.patch.object(H, 'wait_enumerated') as wait:
            self.assertIsNone(H.flash_and_enumerate('b', {'uid': 'U', 'flasher': {}}, {'vidPid': '1:2'}, self.dir,
                                                    None)[2])
        wait.assert_not_called()

    def test_tracker_marks_unclean(self):
        t = H.Tracker.__new__(H.Tracker)
        t.receipts, t.pending, t.unclean = [], [], ''
        t._note('pending', {'pid': 1}, ['openocd'])
        with self.assertRaises(H.Unclean):
            t.check()
        with self.assertRaises(H.Unclean):   # nothing more is launched
            t.run(['JLinkExe'], timeout=1)
        self.A.process.run_tracked.assert_not_called()
        self.assertEqual((t.receipts, t.pending), ([{'pid': 1}], [{'receipt': {'pid': 1}}]))
        t._note('unknown', None, ['openocd'])
        self.assertIn('unrecheckable', t.pending[-1]['receipt'])

    def test_tracker_runs_in_the_run_dirs_tracked_state_dir(self):
        self.A.process.state_dir.return_value = '/trusted/tracked'
        self.assertEqual(H.Tracker(self.dir / 'run').state_dir, '/trusted/tracked')
        self.A.process.state_dir.assert_called_once_with(str(self.dir / 'run'), 'tracked')


class BaseImage(Base):
    """A rig board's base image: admitted with its record, checked before every J-Link flash, rewritten only on a
    proven difference."""

    def setUp(self):
        super().setUp()
        H.B.write_hex([(0x1000, bytes(range(32)))], self.root / 'sd.hex')
        H.B.write_hex([(0x10001014, bytes(8))], self.root / 'uicr.hex')
        self.BP.BOARDS['rig_jlink'].update(base=('sd.hex', 'uicr.hex'), base_exclude=((0x10001000, 0x10002000),))
        self.flasher = {'name': 'jlink', 'uid': '123456', 'args': '-device X'}

    def base_record(self, **over):
        files = {f: sha((self.root / f).read_bytes()) for f in ('sd.hex', 'uicr.hex')}
        return self.record(base={'files': files, 'segments': [[0x1000, 0x1020]], **over})

    def admit(self, rec):
        with mock.patch.object(H, 'git_identity', return_value=('abc', '')):
            return H.admit(rec, 'rig_jlink', self.roster, self.dir / 'run')

    def test_admission_writes_the_runs_it_checks_and_never_the_excluded_ones(self):
        _, images = self.admit(self.base_record())
        base = images['echo']['base']
        self.assertEqual(H.B.hex_runs(base['hex']), [(0x1000, bytes(range(32)))])
        self.assertEqual([(a, Path(f).read_bytes()) for a, f in base['bins']], [(0x1000, bytes(range(32)))])
        self.assertIsNone(base['state'])

    def test_admission_refuses_a_base_that_is_not_the_records(self):
        with self.assertRaisesRegex(H.Refused, 'runs differ from the record'):
            self.admit(self.base_record(segments=[[0x1000, 0x1010]]))
        rec = self.base_record()
        H.B.write_hex([(0x1000, bytes(32))], self.root / 'sd.hex')
        with self.assertRaisesRegex(H.Refused, 'base image sd.hex is missing or changed since the record'):
            self.admit(rec)
        with self.assertRaisesRegex(H.Refused, r"its base image \[\] is not the profile's \['sd.hex', 'uicr.hex'\]"):
            self.admit(self.record())
        H.B.write_hex([(0x1000, bytes(range(32)))], self.root / 'sd.hex')
        rec = self.base_record()
        del self.BP.BOARDS['rig_jlink']['base']
        with self.assertRaisesRegex(H.Refused, r"is not the profile's \[\]: rebuild it"):
            self.admit(rec)

    def test_admission_refuses_a_base_with_nothing_to_verify(self):
        (self.root / 'sd.hex').write_text(':00000001FF\n')   # EOF only; uicr.hex's run is all excluded
        with self.assertRaisesRegex(H.Refused, 'base image: sd.hex, uicr.hex: no runs outside the excluded ranges'):
            self.admit(self.base_record(segments=[]))

    def sessions(self, *problems):
        """run_jlink_commands' scripts, each session judged by the next of `problems`."""
        self.A.flash.jlink_problem.side_effect = list(problems)
        self.A.flash.run_jlink_commands.side_effect = lambda f, cmds, **kw: mock.Mock(cmds=cmds)
        return lambda: [c[0][1] for c in self.A.flash.run_jlink_commands.call_args_list]

    def base(self):
        return {'hex': 'b.hex', 'bins': [(0x1000, 'b-1000.bin')], 'state': None}

    def test_a_matching_base_is_left_alone(self):
        base, scripts = self.base(), self.sessions('')
        self.assertEqual(H.ensure_base(self.flasher, base, self.dir, mock.Mock()), '')
        self.assertEqual((scripts(), base['state']), ([['halt', 'verifybin b-1000.bin,0x1000']], 'matched'))

    def test_a_proven_difference_rewrites_the_base_with_no_reset_or_go(self):
        base, scripts = self.base(), self.sessions('stopped', '')
        self.A.flash.jlink_verify_differs.return_value = 'verifybin b-1000.bin,0x1000'
        self.assertEqual(H.ensure_base(self.flasher, base, self.dir, mock.Mock()), '')
        self.assertEqual(scripts()[1], ['halt', 'loadfile b.hex 0 noreset', 'verifybin b-1000.bin,0x1000'])
        self.assertEqual(base['state'], 'rewritten: the run at 0x1000 differed')
        self.sessions('stopped', 'loadfile failed')
        self.assertEqual(H.ensure_base(self.flasher, self.base(), self.dir, mock.Mock()),
                         'base rewrite after the run at 0x1000 differed: loadfile failed')

    def test_a_check_that_proves_nothing_writes_nothing(self):
        base, scripts = self.base(), self.sessions('JLinkExe exit 1')
        self.A.flash.jlink_verify_differs.return_value = None
        why = H.ensure_base(self.flasher, base, self.dir, mock.Mock())
        self.assertEqual((why, len(scripts()), base['state']),
                         ('base check: JLinkExe exit 1 (nothing written)', 1, None))

    def test_admission_parses_the_bytes_it_hashed(self):
        seen = []
        real = H.B.base_runs
        with mock.patch.object(H.B, 'base_runs', lambda b, root: seen.append(root) or real(b, root=root)):
            self.admit(self.base_record())
        self.assertEqual(seen, [self.dir / 'run' / 'base' / 'files'])   # the run dir's copies, never the checkout

    def test_a_jlink_flash_checks_the_base_first_and_the_row_names_its_state(self):
        calls = []
        with mock.patch.object(H, 'ensure_base', lambda f, b, d, t: calls.append('base') or 'base check: x'):
            why = H.flash(self.flasher, {'hex': 'a.hex', 'base': {'state': None}}, self.dir, mock.Mock())
        self.assertEqual((why, calls), ('base check: x', ['base']))
        self.A.flash.flash_jlink.assert_not_called()

    def test_a_failed_check_after_a_matched_one_reports_no_stale_state(self):
        base, _ = {**self.base(), 'state': 'matched'}, self.sessions('JLinkExe exit 1')
        self.A.flash.jlink_verify_differs.return_value = None
        H.ensure_base(self.flasher, base, self.dir, mock.Mock())
        self.assertIsNone(base['state'])

    def rewritten(self, why, dev):
        """flash_and_enumerate after a base rewrite, then (why, dev)."""
        def fe(name, board, image, *a):
            image['base']['state'] = 'rewritten: the run at 0x1000 differed'
            return why, 'detached', dev
        return mock.patch.object(H, 'flash_and_enumerate', fe)

    def test_every_row_after_a_flash_names_the_base_state(self):
        image = {'vidPid': '1234:5678', 'elf': 'a.elf', 'hex': 'a.hex', 'base': self.base()}
        board, said = self.board('rig_jlink'), 'rewritten: the run at 0x1000 differed'
        self.A.usb.usb_devices.return_value = []
        for why, dev in (('J-Link flash: x', None), ('', None)):
            with self.rewritten(why, dev):
                self.assertEqual(H.step('rig_jlink', board, image, 'echo', self.dir, mock.Mock())['base'], said)
            with self.rewritten(why, dev):
                self.assertEqual(H.flash_only('rig_jlink', board, image, self.dir, None)['base'], said)
        with mock.patch.object(H, 'flash', lambda f, image, *a: image['base'].update(state='matched') or ''):
            self.assertEqual(H.flash_only('rig_host', self.board('rig_host'), image, self.dir, None)['base'],
                             'matched')
        with mock.patch.object(H, 'step', return_value={'pass': False, 'detail': 'x', 'base': said}):
            self.assertEqual(H.restore('rig_jlink', board, {'echo': image}, self.dir, None)['base'], said)


class Step(Base):
    def test_a_test_function_gets_the_roster_board(self):
        seen = mock.Mock(return_value=(True, 'echoed'))
        self.HP.TEST_FUNCS['echo'] = seen
        board = self.board('rig_jlink')
        dev = {'busport': '1-1', 'dir': '/x'}
        with mock.patch.object(H, 'flash_and_enumerate', return_value=('', '', dev)):
            row = H.step('rig_jlink', board, {'vidPid': '1234:5678'}, 'echo', self.dir, mock.Mock())
        self.assertEqual((row['pass'], row['device']), (True, '1-1'))
        a = seen.call_args[0]
        self.assertEqual((a[0], len(a[1]), a[4], a[5]), (dev, 8, self.dir, board))   # dev, nonce, ..., run_dir, board

    def test_host_side_failure_is_the_tests_and_an_unproven_child_is_unclean(self):
        tracker = mock.Mock()
        self.HP.TEST_FUNCS['inout'] = mock.Mock(side_effect=OSError('boom'))
        self.assertEqual(H.run_test('inout', {}, tracker, self.dir, {}), (False, 'OSError: boom'))
        tracker.check.side_effect = H.Unclean('read-block: cleanup unknown')
        with mock.patch.object(H, 'flash_and_enumerate', return_value=('', '', {'busport': '1-1', 'dir': '/x'})), \
                self.assertRaises(H.Unclean):
            H.step('rig_jlink', self.board('rig_jlink'), {'vidPid': '1234:5678'}, 'inout', self.dir, tracker)

    def test_no_new_enumeration_fails_naming_the_dut(self):
        self.A.usb.usb_devices.return_value = [{'busport': '3-1'}]
        with mock.patch.object(H, 'flash_and_enumerate', return_value=('', 'detached', None)):
            row = H.step('rig_ocd', self.board('rig_ocd'), {'vidPid': '239a:cafe'}, 'echo', self.dir, mock.Mock())
        self.assertFalse(row['pass'])
        self.assertIn("no 239a:cafe with serial OUID enumerated anew within 20s (that VID:PID at: ['3-1'])",
                      row['detail'])

    def test_a_host_board_runs_the_profiles_host_step(self):
        self.HP.step_host = mock.Mock(return_value={'test': 'info', 'pass': True})
        board = self.board('rig_host')
        self.assertEqual(H.step('rig_host', board, {'elf': 'x'}, 'info', self.dir, None)['pass'], True)
        self.HP.step_host.assert_called_once_with('rig_host', board, {'elf': 'x'}, 'info', self.dir, None)

    def test_restore_runs_the_roles_restore_test_on_its_restoration_sketch(self):
        seen = []
        images = {'echo': 'echo image', 'info': 'info image'}
        with mock.patch.object(H, 'step', lambda name, board, image, test, *a: seen.append((image, test)) or
                               {'pass': True, 'detail': 'ok'}):
            self.assertEqual(H.restore('rig_ocd', {}, images, self.dir, None),
                             {'pass': True, 'state': 'echo restored', 'evidence': 'ok'})
            H.restore('rig_host', {}, images, self.dir, None)
        self.assertEqual(seen, [('echo image', 'echo'), ('info image', 'info')])


class Battery(Base):
    def run_board(self, step=None, restore=None, admit=None, probe='', **over):
        rec = self.record(**over)
        board = self.board('rig_jlink')
        with mock.patch.object(H, 'probe_present', return_value=probe), \
                mock.patch.object(H, 'admit', admit or (lambda *a, **k: (json.loads(rec.read_text()),
                                                                         {'echo': {'sha256': {},
                                                                                   'fqbn': 'vend:arch:rig_jlink'}}))), \
                mock.patch.object(H, 'step', step or (lambda *a: {'test': 'echo', 'pass': True, 'detail': 'ok'})), \
                mock.patch.object(H, 'restore', restore or (lambda *a: {'pass': True, 'state': 'echo restored',
                                                                        'evidence': 'ok'})), \
                mock.patch.object(H.Tracker, '__init__', fake_tracker_init):
            return H.run_board('rig_jlink', board, ['echo'], rec, self.roster, self.dir / 'run', None)

    def test_pass_releases_the_protected_lock(self):
        row = self.run_board()
        self.assertTrue(row['pass'])
        self.assertEqual(H.verdict(row), 'pass')
        self.assertEqual(self.lock.taken[0][:3], ('rig_jlink', 'hil_test.py', True))
        self.assertEqual(self.lock.taken[0][3]['locks'], ['rig_jlink'])
        self.assertEqual(len(self.lock.released), 1)
        self.assertEqual(row['firmware']['head'], 'abc')
        self.assertEqual(row['firmware']['fqbns'], {'echo': 'vend:arch:rig_jlink'})
        self.assertEqual(row['firmware']['driftedHeaders'], [])

    def test_firmware_names_the_headers_a_core_copy_drifted(self):
        hdr = lambda c, m: {'path': f'/core/{c}', 'counterpart': c, 'matches': m}
        sketches = {'echo': {'foreignHeaders': [hdr('src/b.h', False), hdr('src/ok.h', True)]},
                    'info': {'foreignHeaders': [hdr('src/a.h', False), hdr('src/b.h', False)]}}
        row = self.run_board(sketches=sketches)
        self.assertEqual(row['firmware']['driftedHeaders'], ['src/a.h', 'src/b.h'])

    def test_failed_test_is_a_fail_after_cleanup(self):
        row = self.run_board(step=lambda *a: {'test': 'echo', 'pass': False, 'detail': 'no echo'})
        self.assertEqual((H.verdict(row), len(self.lock.released)), ('fail', 1))
        self.assertEqual(H.exit_code([row]), 1)

    def test_surviving_child_ends_the_lock_and_logs_its_receipt(self):
        def step(*a):
            a[-1].pending.append({'receipt': {'pid': 7}})
            raise H.Unclean('openocd: cleanup pending')
        row = self.run_board(step=step)
        self.assertEqual(H.verdict(row), 'blocker')
        self.assertEqual((row['cleanup']['state'], row['cleanup']['released']), ('pending', True))
        self.assertEqual(self.lock.ended, [(['rig_jlink'], row['cleanup']['evidence'], [{'pid': 7}])])
        self.assertEqual((self.lock.released, self.lock.pending), ([], {}))
        self.assertNotIn('logError', row['cleanup'])
        self.assertEqual(H.exit_code([row]), 4)

    def test_unrecheckable_child_is_logged_and_the_next_run_proceeds(self):
        lost = {'receipt': {'unrecheckable': ['JLinkExe'], 'at': 1.0}}

        def step(*a):
            a[-1].pending.append(lost)
            raise H.Unclean('JLinkExe: cleanup unknown')
        row = self.run_board(step=step)
        self.assertEqual((row['cleanup']['pending'], H.verdict(row)), ([lost], 'blocker'))
        self.assertEqual(self.lock.ended[0][2], [lost['receipt']])
        self.lock.failures['rig_jlink'] = [{'id': 'f1', 'receipts': [lost['receipt']], 'stays': True}]
        row = self.run_board()   # the next run is not refused: it reports the failure and runs
        self.assertEqual((H.verdict(row), row['ran'], row['previousCleanupFailure']['id']), ('pass', True, 'f1'))
        self.assertEqual((row['cleanup']['restoredNote'], self.lock.restored), ('f1 stays open', []))

    def test_a_passing_restore_marks_the_earlier_failure_restored(self):
        self.lock.failures['rig_jlink'] = [{'id': 'f1', 'receipts': [{'pid': 9}]}]
        self.A.process.recheck.return_value = 'done'
        row = self.run_board()
        self.assertEqual((H.verdict(row), self.lock.restored), ('pass', [('rig_jlink', 'f1')]))
        self.assertNotIn('restoredNote', row['cleanup'])

    def test_a_passing_restore_hands_every_open_failure_to_hil_lock(self):
        self.lock.failures['rig_jlink'] = [{'id': 'fA', 'stays': True}, {'id': 'fB'}]
        row = self.run_board()
        self.assertEqual((row['previousCleanupFailure']['id'], row['openCleanupFailures']), ('fB', 2))
        self.assertEqual(self.lock.restored, [('rig_jlink', 'fB')])
        self.assertEqual(row['cleanup']['restoredNote'], 'fA stays open')

    def test_a_failed_restore_resolves_nothing(self):
        self.lock.failures['rig_jlink'] = [{'id': 'f1'}]
        self.run_board(restore=lambda *a: {'pass': False, 'state': 'unknown', 'evidence': 'no echo'})
        self.assertEqual(self.lock.restored, [])

    def test_failed_restore_releases_the_board_and_logs_it(self):
        row = self.run_board(restore=lambda *a: {'pass': False, 'state': 'unknown', 'evidence': 'no echo'})
        self.assertEqual((H.verdict(row), row['cleanup']['released']), ('blocker', True))
        self.assertEqual(self.lock.ended, [(['rig_jlink'], 'no echo', [])])

    def test_an_unlogged_failure_is_reported(self):
        self.lock.end_errors = {'rig_jlink': ['log: disk full']}
        row = self.run_board(restore=lambda *a: {'pass': False, 'state': 'unknown', 'evidence': 'x'})
        self.assertEqual((row['cleanup']['logError'], row['cleanup']['released']), ('log: disk full', True))
        self.assertEqual(H.exit_code([row]), 4)

    def test_harness_error_is_a_blocker(self):
        def step(*a):
            raise RuntimeError('bug')
        row = self.run_board(step=step)
        self.assertEqual((H.verdict(row), row['cleanup']['state']), ('blocker', 'unknown'))

    def test_refused_record_is_not_run_and_untouched(self):
        def admit(*a, **k):
            raise H.Refused('stale')
        row = self.run_board(admit=admit)
        self.assertEqual((H.verdict(row), row['cleanup']['state'], len(self.lock.released)),
                         ('not-run', 'untouched', 1))

    def test_an_older_engines_pending_children_are_reported_not_a_refusal(self):
        self.lock.pending[('rig_jlink', H.PENDING_KIND)] = [{'receipt': {'pid': 9}}]
        self.A.process.recheck.return_value = 'pending'
        row = self.run_board()
        self.assertEqual((H.verdict(row), row['ran'], len(self.lock.released)), ('pass', True, 1))
        self.assertIn('older engine', row['previousPending'])
        self.A.process.recheck.return_value = 'done'
        row = self.run_board()
        self.assertNotIn('previousPending', row)
        self.assertEqual(self.lock.pending, {})

    def other(self, state, receipts=()):
        """Another invocation's claim on rig_jlink, judged `state` with `receipts` by the fake invocation_state."""
        listed = self.lock.claims.setdefault('rig_jlink', [])
        s = {'receipt': {'ops': '/other/ops', 'pid': 99 + len(listed), 'start': '5'}, 'cmd': 'hil_test.py',
             'state': state, 'receipts': list(receipts)}
        listed.append(s)
        return s

    def test_a_clean_run_claims_first_and_removes_its_claim_before_the_release(self):
        at_release = []
        self.lock.release_board_lock = lambda fh: at_release.append(list(self.lock.claims['rig_jlink']))
        seen = []

        def step(*a):
            seen.append([s['receipt']['pid'] for s in self.lock.claims['rig_jlink']])
            return {'test': 'echo', 'pass': True, 'detail': 'ok'}
        row = self.run_board(step=step)
        self.assertEqual((H.verdict(row), seen, at_release), ('pass', [[os.getpid()]], [[]]))
        self.assertEqual(self.A.process.recheck_dir.call_args[0][0], str(self.dir / 'run/rig_jlink/tracked'))
        self.assertNotIn('claimNote', row)

    def test_another_live_invocation_refuses_before_admission_and_before_any_judgement(self):
        dead = self.other('unresolved', [{'token': 't1'}])
        other = self.other('alive')
        row = self.run_board(admit=mock.Mock(side_effect=AssertionError('admitted')))
        self.assertEqual((row['locked'], H.verdict(row), len(self.lock.released)), (True, 'not-run', 1))
        self.assertIn('board in use: another invocation on rig_jlink is running', row['detail'])
        self.assertEqual((self.lock.claims['rig_jlink'], self.lock.logged, self.lock.settled), ([dead, other], [], []))
        self.assertEqual(self.lock.judged, [])   # judging tidies: a live claimant may log what it would tidy

    def test_the_kept_tokens_are_read_after_the_gate(self):
        self.other('ended')
        self.lock.failures['rig_jlink'] = [{'id': 'f0', 'receipts': [{'token': 't1'}]}]
        take = self.lock.take_board_lock

        def taken(*a, **k):   # a claimant alive at the take logs t2 before it ends
            fh = take(*a, **k)
            self.lock.failures['rig_jlink'].append({'id': 'f9', 'receipts': [{'token': 't2'}]})
            return fh
        self.lock.take_board_lock = taken
        resolved = []
        self.lock.resolve_restored = lambda board, failures, **ev: resolved.append([f['id'] for f in failures]) or []
        row = self.run_board()
        self.assertEqual(self.lock.judged[0][1], {'t1', 't2'})
        self.assertEqual((row['previousCleanupFailure']['id'], row['openCleanupFailures']), ('f9', 2))
        self.assertEqual(resolved, [['f0', 'f9']])   # each once

    def test_an_earlier_invocations_unproven_children_are_logged_then_settled(self):
        other = self.other('unresolved', [{'token': 't1'}])
        ended = self.other('ended')
        row = self.run_board()
        self.assertEqual((H.verdict(row), row['ran']), ('pass', True))
        self.assertEqual(self.lock.logged, [('rig_jlink', 'an earlier invocation ended with children not proven '
                                                          'ended', [{'token': 't1'}])])
        self.assertEqual(row['previousCleanupFailure']['task'], other['receipt'])
        self.assertEqual((self.lock.settled, self.lock.claims['rig_jlink']), ([[{'token': 't1'}]], []))
        self.assertNotIn(ended, self.lock.claims['rig_jlink'])

    def test_children_still_running_after_the_settle_give_the_board_up_and_keep_their_claim(self):
        other = self.other('unresolved', [{'token': 't1'}, {'token': 'u1'}])
        gone = self.other('unresolved', [{'token': 'u2'}])
        self.lock.running = {'t1'}
        row = self.run_board(admit=mock.Mock(side_effect=AssertionError('admitted')))
        self.assertEqual((row['locked'], H.verdict(row), len(self.lock.released), self.lock.ended),
                         (True, 'not-run', 1, []))
        self.assertIn(f'still run after {H.CLAIM_SETTLE}s', row['detail'])
        self.assertIn('t1', row['detail'])
        self.assertNotIn('u1', row['detail'])
        self.assertEqual(row['previousCleanupFailure']['task'], gone['receipt'])
        self.assertEqual(self.lock.claims['rig_jlink'], [other])   # judged again by the next attempt
        logged = len(self.lock.logged)
        row = self.run_board(admit=mock.Mock(side_effect=AssertionError('admitted')))
        self.assertTrue(row['locked'])
        self.assertEqual(len(self.lock.logged), logged)   # not logged again
        self.lock.running = set()
        self.assertEqual(H.verdict(self.run_board()), 'pass')
        self.assertEqual(self.lock.claims['rig_jlink'], [])

    def test_an_unknown_receipt_never_holds_the_board(self):
        self.other('unresolved', [{'unrecheckable': 'no .json file', 'token': 'x', 'files': []}])
        row = self.run_board()
        self.assertEqual((H.verdict(row), len(self.lock.logged), self.lock.claims['rig_jlink']), ('pass', 1, []))

    def test_receipts_already_logged_are_settled_not_logged_again(self):
        self.lock.failures['rig_jlink'] = [{'id': 'f0', 'receipts': [{'token': 't1'}]}]
        self.other('unresolved', [{'token': 't1'}])
        row = self.run_board()
        self.assertEqual((H.verdict(row), self.lock.logged, self.lock.settled), ('pass', [], [[{'token': 't1'}]]))
        self.assertEqual(self.lock.claims['rig_jlink'], [])

    def test_failures_and_receipts_with_malformed_ids_are_kept_never_hashed(self):
        odd = [{'id': [], 'receipts': []}, {'id': {'x': 1}, 'receipts': [{'token': ['t']}]}, {'receipts': []}]
        self.lock.failures['rig_jlink'] = [dict(f) for f in odd]
        take = self.lock.take_board_lock

        def taken(*a, **k):   # the log read after the gate names them again, and one more
            fh = take(*a, **k)
            self.lock.failures['rig_jlink'] += [dict(f) for f in odd] + [{'id': [1], 'receipts': []}]
            return fh
        self.lock.take_board_lock = taken
        self.other('unresolved', [{'token': ['t']}, {'token': {'x': 1}}, {'token': 't2'}])
        resolved = []
        self.lock.resolve_restored = lambda board, failures, **ev: resolved.append(failures) or []
        row = self.run_board()
        self.assertEqual((H.verdict(row), row['ran']), ('pass', True))
        self.assertEqual(self.lock.logged[0][2], [{'token': ['t']}, {'token': {'x': 1}}, {'token': 't2'}])
        self.assertEqual([f.get('id') for f in resolved[0]], [[], {'x': 1}, None, [1], 'f1'])

    def test_an_earlier_claim_whose_log_failed_stays_for_the_next_attempt(self):
        other = self.other('unresolved', [{'token': 't1'}])
        self.lock.log_cleanup_failure = mock.Mock(side_effect=OSError('disk full'))
        row = self.run_board()
        self.assertEqual(H.verdict(row), 'pass')
        self.assertIn('cleanup log not written: disk full', row['claimNote'])
        self.assertEqual(self.lock.claims['rig_jlink'], [other])

    def test_an_unclean_end_keeps_its_claim_for_the_next_attempt(self):
        def step(*a):
            a[-1].pending.append({'receipt': {'token': 'p1'}})
            raise H.Unclean('openocd: cleanup pending')
        self.A.process.recheck_dir.return_value = ('pending', [{'token': 'p1'}])
        row = self.run_board(step=step)
        self.assertEqual((H.verdict(row), row['claimNote']), ('blocker', 'claim kept: its operations recheck pending'))
        self.assertEqual(self.A.process.recheck_dir.call_args[0][1], {'p1'})   # its logged receipt's files stay
        self.assertEqual([s['receipt']['pid'] for s in self.lock.claims['rig_jlink']], [os.getpid()])
        self.assertEqual(len(self.lock.ended), 1)

    def test_a_claim_record_that_cannot_name_the_run_refuses_it(self):
        class Unwritable(self.lock.PendingSessions):
            def claim(self, entry):
                raise PermissionError('another user\'s record')
        self.lock.PendingSessions = Unwritable
        row = self.run_board(admit=mock.Mock(side_effect=AssertionError('admitted')))
        self.assertEqual((H.verdict(row), row['locked'], len(self.lock.released)), ('not-run', False, 1))
        self.assertIn("claim record could not name this invocation: PermissionError", row['detail'])

    def test_interrupt_keeps_the_row_and_ends_the_lock(self):
        def step(*a):
            raise KeyboardInterrupt
        row = self.run_board(step=step)
        self.assertTrue(row['interrupted'])
        self.assertEqual((row['cleanup']['released'], len(self.lock.ended)), (True, 1))

    def test_absent_probe_is_not_run_before_the_lock(self):
        row = self.run_board(probe='probe 1 is not on this host\'s bus')
        self.assertEqual((H.verdict(row), self.lock.taken), ('not-run', []))

    def test_locked_and_blocked_boards(self):
        def locked(*a, **k):
            raise FakeLock.BoardLocked('held by ci')
        self.lock.take_board_lock = locked
        row = self.run_board()
        self.assertEqual((row['locked'], H.verdict(row)), (True, 'not-run'))

        def blocked(*a, **k):
            raise FakeLock.RecoveryBlocked('cleanup holder')
        self.lock.take_board_lock = blocked
        row = self.run_board()
        self.assertEqual((row['recoveryBlocker'], H.verdict(row)), (True, 'blocker'))

    def test_budget_stops_starting_tests(self):
        with mock.patch.object(H, 'BOARD_BUDGET', H.STEP_MAX):
            row = self.run_board()
        self.assertFalse(row['ran'])
        self.assertIn('budget', row['tests'][0]['detail'])

    def test_run_gives_each_board_the_requested_tests_of_its_role(self):
        seen = {}

        def run_board(name, board, tests, *a):
            seen[name] = tests
            return {'board': name, 'ran': True, 'pass': True, 'locked': False, 'recoveryBlocker': False,
                    'tests': [], 'cleanup': {'pass': True, 'state': 'x'}, 'detail': ''}
        args = argparse.Namespace(board=['rig_ocd', 'rig_host'], test=['echo'], roster=str(self.roster),
                                  record_dir=str(self.dir), run_dir=str(self.dir / 'run'), staged=None)
        self.A.report.ReportError, self.A.report.publish.return_value = Exception, ('hil_report.json',)
        self.A.report.BodySchema = lambda **kw: argparse.Namespace(**kw)
        with mock.patch.object(H, 'run_board', run_board), \
                mock.patch.object(H.shutil, 'which', return_value='/bin/x'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(H.cmd_run(args), 0)
        self.assertEqual(seen, {'rig_ocd': ['echo']})   # the host board is not run, nor reported
        self.assertEqual(self.A.report.make_meta.call_args.kwargs['selection'], ['rig_ocd'])
        self.assertEqual(self.A.report.start_attempt.call_args[0][1].name, 'fake-usb-hil')   # the profile's schema

    def test_run_fails_unless_its_report_stands(self):
        row = {'board': 'rig_ocd', 'ran': True, 'pass': True, 'locked': False, 'recoveryBlocker': False, 'tests': [],
               'cleanup': {'pass': True, 'state': 'restored'}, 'detail': ''}
        args = argparse.Namespace(board=['rig_ocd'], test=['echo'], roster=str(self.roster), record_dir=str(self.dir),
                                  run_dir=str(self.dir / 'run'), staged=None)

        class ReportError(Exception):
            pass
        self.A.report.ReportError = ReportError
        self.A.report.publish.side_effect = ReportError('replace: disk full')
        for outcome, code in (('repaired', 0), ('withdrawn', 1), ('removed', 1), ('unresolved', 1), ('refused', 1)):
            self.A.report.recover_publish.return_value = argparse.Namespace(outcome=outcome, problems=[])
            with self.subTest(outcome=outcome), mock.patch.object(H, 'run_board', return_value=dict(row)), \
                    mock.patch.object(H.shutil, 'which', return_value='/bin/x'), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(H.cmd_run(args), code)
                got = json.loads(out.getvalue())
                self.assertEqual((got['pass'], got['exitCode'], got['reportPath'] is None),
                                 (code == 0, code, outcome != 'repaired'))
                self.assertIn(f'recovery {outcome}', got['reportError'])

    def test_run_refuses_a_missing_flasher_tool_before_hardware(self):
        args = argparse.Namespace(board=['rig_esp'], test=None, roster=str(self.roster), record_dir=str(self.dir),
                                  run_dir=None, staged=None)
        with mock.patch.object(H.shutil, 'which', return_value=None), \
                self.assertRaisesRegex(H.Refused, 'rig_esp: esptool not on PATH'):
            H.cmd_run(args)

    def test_unknown_board_refused_before_hardware(self):
        code, out = self.main(['run', '-b', 'nope', str(self.roster)])
        self.assertEqual(code, 2)
        self.assertIn('nope', out['error'])


class Manual(Base):
    def held(self, **kw):
        self.lock.records['rig_jlink'] = {'reason': 'mine', 'pid': 1}
        return argparse.Namespace(**{'board': 'rig_jlink', 'under_hold': 'mine', 'roster': str(self.roster),
                                     'staged': None, 'bundle': None, **kw})

    def test_unclean_manual_flash_logs_a_failed_cleanup(self):
        for exc in (H.Unclean('JLinkExe: cleanup pending'), RuntimeError('boom'), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                self.lock.logged.clear()
                args = self.held(cmd='flash', build_record=str(self.record(experimental=True)), sketch='echo')

                def unclean(*a):
                    a[-1].pending.append({'receipt': {'pid': 7}})
                    raise exc
                with mock.patch.object(H, 'git_identity', return_value=('abc', '')), \
                        mock.patch.object(H, 'flash_only', unclean), contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(H.Tracker, '__init__', fake_tracker_init):
                    self.assertEqual(H.manual(args), 4)
                self.assertEqual([(b, r) for b, _, r in self.lock.logged], [('rig_jlink', [{'pid': 7}])])
                self.assertEqual(self.lock.pending, {})

    def test_staged_flash_admits_against_the_manifest_and_keeps_a_result(self):
        rec = self.record()
        args = self.held(cmd='flash', build_record=str(rec), staged='manifest.json', sketch='echo')
        flashed = mock.Mock(return_value={'pass': True, 'detail': 'flashed'})
        with mock.patch.object(H, 'verify_staged', return_value={'head': 'abc', 'files': {}}), \
                mock.patch.object(H, 'git_identity', side_effect=AssertionError('a staged tree has no git')), \
                mock.patch.object(H, 'flash_only', flashed), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(H.Tracker, '__init__', fake_tracker_init):
            self.assertEqual(H.manual(args), 0)
        result = json.loads(next((self.root / 'build/hil-manual').glob('rig_jlink-flash-*/result.json')).read_text())
        self.assertEqual((result['pass'], result['record'], result['experimental']), (True, str(rec), False))

    def test_exec_with_a_surviving_child_is_unclean_not_a_pass(self):
        args = self.held(cmd='exec', argv=['sleep', '9'], timeout=5)
        self.A.process.run_tracked.return_value = mock.Mock(returncode=0, stdout='', stderr='', cleanup='pending',
                                                            receipt={'pid': 5})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(H.manual(args), 4)
        self.assertEqual(json.loads(out.getvalue())['survivors'], [{'receipt': {'pid': 5}}])
        self.assertEqual([r for _, _, r in self.lock.logged], [[{'pid': 5}]])

    def test_a_session_command_beside_a_live_invocation_is_refused(self):
        other = {'receipt': {'ops': '/other/ops', 'pid': 99, 'start': '5'}, 'cmd': 'hil_test.py', 'state': 'alive'}
        self.lock.claims['rig_jlink'] = [other]
        self.lock.records['rig_jlink'] = {'reason': 'mine', 'pid': 1}
        code, out = self.main(['exec', '-b', 'rig_jlink', '--under-hold', 'mine', str(self.roster), '--', 'true'])
        self.assertEqual(code, 2)
        self.assertIn('another invocation on rig_jlink is running', out['error'])
        self.assertEqual((self.lock.claims['rig_jlink'], self.A.process.run_tracked.called), ([other], False))
        args = self.held(cmd='restore', release=False, report=None, build_record='r.json')
        with self.assertRaisesRegex(H.Refused, 'is running'):   # a refused restore is logged as ever
            H.manual(args)
        self.assertEqual([d for _, d, _ in self.lock.logged], ['restore refused or raised: InUse: another invocation '
                                                               f'on rig_jlink is running: {[other["receipt"]]}'])

    def test_a_refused_session_command_reports_and_keeps_the_failures_its_claim_found(self):
        other = {'receipt': {'ops': '/o', 'pid': 99, 'start': '5'}, 'cmd': 'hil_test.py', 'state': 'alive'}
        self.lock.claims['rig_jlink'] = [other]
        self.lock.failures['rig_jlink'] = [{'id': 'f1', 'receipts': []}]
        self.lock.records['rig_jlink'] = {'reason': 'mine', 'pid': 1}
        code, out = self.main(['exec', '-b', 'rig_jlink', '--under-hold', 'mine', str(self.roster), '--', 'true'])
        self.assertEqual((code, out['previousCleanupFailure']['id']), (2, 'f1'))
        result = json.loads(next((self.root / 'build/hil-manual').glob('rig_jlink-exec-*/result.json')).read_text())
        self.assertEqual((result['pass'], result['previousCleanupFailure']['id']), (False, 'f1'))
        self.assertIn('is running', result['error'])
        args = self.held(cmd='restore', release=True, report=None, build_record='r.json')
        with mock.patch.object(H, 'release_hold', return_value={'exit': 0, 'output': ''}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(H.manual(args), H.RESTORE_FAILED)
        out = json.loads(out.getvalue())
        self.assertEqual(out['previousCleanupFailure']['id'], 'f1')
        self.assertIn('is running', out['error'])

    def test_a_clean_session_command_removes_its_claim(self):
        args = self.held(cmd='exec', argv=['true'], timeout=5)
        self.A.process.run_tracked.return_value = mock.Mock(returncode=0, stdout='', stderr='', cleanup='done',
                                                            receipt=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(H.manual(args), 0)
        self.assertEqual(self.lock.claims['rig_jlink'], [])
        self.A.process.run_tracked.return_value.cleanup, self.A.process.recheck_dir.return_value = 'pending', \
            ('pending', [{'token': 'p'}])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(H.manual(args), 4)
        self.assertEqual(json.loads(out.getvalue())['claimNote'], 'claim kept: its operations recheck pending')
        self.assertEqual(len(self.lock.claims['rig_jlink']), 1)

    def test_one_session_command_at_a_time(self):
        args = self.held(cmd='exec', argv=['true'], timeout=5)
        with H.one_session_command('rig_jlink'):
            with self.assertRaisesRegex(H.Refused, 'still running'):
                H.manual(args)
        with H.one_session_command('rig_jlink'):
            pass

    def test_bundle_holds_the_record_and_its_images(self):
        rec = self.record()
        bundle = self.root / 'bundles/1'
        shutil.copytree(self.root / 'build', bundle / 'build')
        (self.root / 'build/hil/rig_jlink/echo/echo.ino.elf').write_bytes(b'overwritten')
        shutil.copy2(rec, bundle / 'rec.json')
        args = self.held(build_record='rec.json', bundle='bundles/1')
        with mock.patch.object(H, 'git_identity', return_value=('abc', '')):
            _, image, _ = H.admit_sketch(args, 'echo', self.dir / 'run', None, False)
            self.assertEqual(Path(image['elf']).read_bytes(), b'elf')
            with self.assertRaisesRegex(H.Refused, 'has no sketch inout'):
                H.admit_sketch(args, 'inout', self.dir / 'run2', None, False)
            args.bundle = '../..'
            with self.assertRaisesRegex(H.Refused, 'not a directory inside'):
                H.admit_sketch(args, 'echo', self.dir / 'run', None, False)

    def test_restore_refuses_an_unrecheckable_child_of_the_report(self):
        report = self.dir / 'hil_report.json'
        report.write_text(json.dumps({'results': [{'board': 'rig_jlink', 'cleanup': {
            'receipts': [], 'pending': [{'receipt': {'unrecheckable': ['JLinkExe'], 'at': 1.0}}]}}]}))
        args = self.held(build_record=str(self.record()), report=str(report), release=False)
        with mock.patch.object(H, 'restore', return_value={'pass': True}) as restore:
            with self.assertRaisesRegex(H.Refused, 'unrecheckable'):
                H.op_restore(args, None, self.dir / 'run', None, None)
        restore.assert_not_called()

    def test_restore_release_releases_the_hold_whatever_the_restore_did(self):
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True)
        restored = {'pass': True, 'state': 'echo restored', 'evidence': 'echo'}
        failed = {'pass': False, 'state': 'unknown', 'evidence': 'no echo'}
        for restore, release_exit, code, logged in ((restored, 0, 0, 0), (failed, 0, H.RESTORE_FAILED, 1),
                                                    (restored, 1, H.RELEASE_FAILED, 0),
                                                    (H.Refused('stale'), 0, H.RESTORE_FAILED, 1)):
            with self.subTest(restore=restore, release_exit=release_exit):
                self.lock.logged.clear()
                outcome = {'side_effect': restore} if isinstance(restore, Exception) else {'return_value': restore}
                with mock.patch.object(H, 'restore', **outcome), \
                        mock.patch.object(H, 'git_identity', return_value=('abc', '')), \
                        mock.patch.object(H.Tracker, '__init__', fake_tracker_init), \
                        mock.patch.object(H, 'release_hold', return_value={'exit': release_exit}) as release, \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(H.manual(args), code)
                release.assert_called_once_with('rig_jlink')
                self.assertEqual(json.loads(out.getvalue())['pass'], code == 0)
                self.assertEqual(len(self.lock.logged), logged)

    def test_restore_release_keeps_the_final_verdict_in_result_json(self):
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True)
        with mock.patch.object(H, 'restore', return_value={'pass': True, 'state': 'echo restored', 'evidence': 'e'}), \
                mock.patch.object(H, 'git_identity', return_value=('abc', '')), \
                mock.patch.object(H.Tracker, '__init__', fake_tracker_init), \
                mock.patch.object(H, 'release_hold', return_value={'exit': 1, 'output': 'ssh dropped'}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(H.manual(args), H.RELEASE_FAILED)
        result = json.loads(next((self.root / 'build/hil-manual').glob('rig_jlink-restore-*/result.json')).read_text())
        self.assertEqual(result, json.loads(out.getvalue()))
        self.assertEqual((result['pass'], result['restored'], result['released']),
                         (False, True, {'exit': 1, 'output': 'ssh dropped'}))

    def test_restore_release_refused_before_its_run_dir_still_keeps_a_result(self):
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True,
                         staged='manifest.json')
        code, out, _ = self.release_restore(args, verify_staged=mock.Mock(side_effect=H.Refused('staged tree changed')))
        self.assertEqual(code, H.RESTORE_FAILED)
        result = json.loads(next((self.root / 'build/hil-manual').glob('rig_jlink-restore-*/result.json')).read_text())
        self.assertEqual(result, out)

    def test_restore_release_releases_before_an_interrupt_propagates(self):
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True)
        with mock.patch.object(H, 'restore', side_effect=KeyboardInterrupt), \
                mock.patch.object(H, 'git_identity', return_value=('abc', '')), \
                mock.patch.object(H.Tracker, '__init__', fake_tracker_init), \
                mock.patch.object(H, 'release_hold', return_value={'exit': 0}) as release, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            H.manual(args)
        release.assert_called_once_with('rig_jlink')

    def test_a_signal_as_the_release_begins_waits_until_it_ran_and_its_verdict_is_out(self):
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True)
        self.A.process.InterruptHold = load_arduino_engine('skills/hil/scripts/hil_process.py').InterruptHold
        prev = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, prev)
        released = []

        def release_hold(name):
            signal.raise_signal(signal.SIGINT)
            released.append(name)
            return {'exit': 0}
        with mock.patch.object(H, 'restore', return_value={'pass': True, 'state': 'echo restored', 'evidence': 'e'}), \
                mock.patch.object(H, 'git_identity', return_value=('abc', '')), \
                mock.patch.object(H.Tracker, '__init__', fake_tracker_init), \
                mock.patch.object(H, 'release_hold', release_hold), \
                contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(KeyboardInterrupt):
            H.manual(args)
        self.assertEqual(released, ['rig_jlink'])
        self.assertEqual(json.loads(out.getvalue())['released'], {'exit': 0})
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def release_restore(self, args, **patches):
        """restore --release under the given patches: (exit, JSON, release_hold mock); an interrupt propagates."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(H, 'git_identity', return_value=('abc', '')))
            stack.enter_context(mock.patch.object(H.Tracker, '__init__', patches.pop('tracker', fake_tracker_init)))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(H, name, value))
            release = stack.enter_context(mock.patch.object(H, 'release_hold', return_value={'exit': 0}))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            try:
                return H.manual(args), json.loads(out.getvalue()), release
            finally:
                release.assert_called_once_with('rig_jlink')

    def test_restore_release_releases_after_a_refused_input_or_setup_and_logs_it(self):
        def no_dir(self, run_dir):
            raise OSError('read-only file system')
        for patches, staged, why in (
                ({'verify_staged': mock.Mock(side_effect=H.Refused('staged tree changed'))}, 'manifest.json',
                 'staged tree changed'), ({'tracker': no_dir}, None, 'read-only file system')):
            with self.subTest(why):
                self.lock.logged.clear()
                args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True,
                                 staged=staged)
                code, out, _ = self.release_restore(args, **patches)
                self.assertEqual(code, H.RESTORE_FAILED)
                self.assertIn(why, out['error'])
                self.assertEqual(len(self.lock.logged), 1)
                self.assertIn(why, self.lock.logged[0][1])

    def test_an_unlogged_restore_failure_says_so(self):
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True)
        with mock.patch.object(self.lock, 'log_cleanup_failure', side_effect=OSError('disk full')):
            code, out, _ = self.release_restore(args, restore=mock.Mock(side_effect=H.Refused('stale')))
        self.assertEqual(code, H.RESTORE_FAILED)
        self.assertIn('cleanup log not written: disk full', out['error'])

    def test_restore_release_propagates_an_interrupt_with_pending_children(self):
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=True)

        def interrupted(*a):
            a[-1].pending.append({'receipt': {'pid': 7}})
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.release_restore(args, restore=interrupted)
        self.assertEqual([r for _, _, r in self.lock.logged], [[{'pid': 7}]])

    def test_a_passing_manual_restore_marks_the_earlier_failure_restored(self):
        self.lock.failures['rig_jlink'] = [{'id': 'f1', 'receipts': []}]
        args = self.held(cmd='restore', build_record=str(self.record()), report=None, release=False)
        with mock.patch.object(H, 'restore', return_value={'pass': True, 'state': 'echo restored', 'evidence': 'e'}), \
                mock.patch.object(H, 'git_identity', return_value=('abc', '')), \
                mock.patch.object(H.Tracker, '__init__', fake_tracker_init), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(H.manual(args), 0)
        self.assertEqual(self.lock.restored, [('rig_jlink', 'f1')])
        self.assertEqual(json.loads(out.getvalue())['previousCleanupFailure']['id'], 'f1')

    def test_release_ends_only_a_hold_under_its_reason_and_restores_nothing(self):
        self.lock.records['rig_jlink'] = {'reason': 'other', 'pid': 1}
        argv = ['release', '-b', 'rig_jlink', '--under-hold', 'mine']
        old = signal.getsignal(signal.SIGHUP)
        self.addCleanup(signal.signal, signal.SIGHUP, old)
        with mock.patch.object(H, 'release_hold', return_value={'exit': 0}) as release, \
                mock.patch.object(H, 'restore', side_effect=AssertionError('restored')):
            self.assertEqual(self.main(argv)[0], H.NOT_HELD)
            self.lock.records['rig_jlink'] = 'unreadable'
            self.assertEqual(self.main(argv)[0], 2)
            release.assert_not_called()
            self.held()
            self.assertEqual(self.main(argv), (0, {'pass': True, 'board': 'rig_jlink', 'released': {'exit': 0}}))
            release.assert_called_once_with('rig_jlink')
            release.return_value = {'exit': 1}
            self.assertEqual(self.main(argv)[0], 1)

    def test_restore_takes_clean_records_only(self):
        args = self.held(build_record=str(self.record(experimental=True)), report=None, release=False)
        with mock.patch.object(H, 'git_identity', return_value=('abc', '')), \
                self.assertRaisesRegex(H.Refused, 'experimental'):
            H.op_restore(args, None, self.dir / 'run', None, None)

    def test_exec_takes_the_argv_after_the_roster(self):
        a = H.parse(['exec', '-b', 'rig_jlink', '--under-hold', 'r', 'ci.json', '--', 'ls', '-l'])
        self.assertEqual((a.roster, a.argv, a.timeout, a.staged), ('ci.json', ['ls', '-l'], 60, None))

    def test_sigterm_unwinds_a_session_command_as_an_interrupt(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])

        def manual(args):   # SIG_DFL would end the harness past _manual's pending bookkeeping
            self.assertTrue(callable(signal.getsignal(signal.SIGTERM)))
            signal.raise_signal(signal.SIGTERM)
        with mock.patch.object(H, 'manual', manual), self.assertRaises(KeyboardInterrupt):
            H.main(['exec', '-b', 'rig_jlink', '--under-hold', 'r', 'ci.json', '--', 'true'])

    def test_sigterm_unwinds_run_and_commission_as_an_interrupt(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])

        def command(args):   # SIG_DFL would skip run_board's finally and cmd_commission's hand-over
            self.assertTrue(callable(signal.getsignal(signal.SIGTERM)))
            signal.raise_signal(signal.SIGTERM)
        for cmd, argv in (('cmd_run', ['run', 'ci.json']), ('cmd_commission', ['commission', '-b', 'b', 'ci.json'])):
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            with self.subTest(cmd), mock.patch.object(H, cmd, command), self.assertRaises(KeyboardInterrupt):
                H.main(argv)
            self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)

    def test_gdb_runs_openocd_on_a_pipe_pinned_to_the_probe(self):
        self.A.flash.openocd_cmd_base = lambda p: [
            'openocd', '-c', 'gdb_port disabled', '-c', f'adapter serial {p["uid"]}',
            *(['-c', f'adapter usb vid_pid {p["vid_pid"]}'] if 'vid_pid' in p else []), *shlex.split(p['args'])]
        argv = H.gdb_argv('rig_ocd', self.board('rig_ocd'), 'fw.elf', ['info symbol main', 'bt'])
        self.assertEqual(argv[:4], ['gdb-multiarch', '-batch', '-nx', '-ex'])
        self.assertEqual(argv[-5:], ['-ex', 'info symbol main', '-ex', 'bt', 'fw.elf'])
        pipe = shlex.split(argv[4].removeprefix('target extended-remote | '))
        self.assertEqual(pipe[-8:], ['-c', 'gdb port pipe', '-c', 'gdb memory_map disable',
                                     '-c', 'gdb breakpoint_override hard', '-c',
                                     'foreach t [target names] { $t configure -event gdb-detach '
                                     '{ foreach u [target names] { $u arp_poll }; resume } }'])
        self.assertIn('adapter serial E66', pipe)
        self.assertIn('adapter usb vid_pid 0x2e8a 0x000c', pipe)
        self.assertIn('target/rp2040.cfg', pipe)
        pipe = shlex.split(H.gdb_argv('rig_jlink', self.board('rig_jlink'), 'fw.elf', [])[4]
                           .removeprefix('target extended-remote | '))
        self.assertIn('interface/jlink.cfg', pipe)
        self.assertEqual(pipe[-1], 'foreach t [target names] { $t configure -event gdb-detach { resume } }')
        self.assertFalse(any('vid_pid' in w for w in pipe))
        self.assertNotIn('arp_poll', H.gdb_argv('rig_host', self.board('rig_host'), 'fw.elf', [])[4])   # rp2350

    def test_gdb_needs_a_debug_route(self):
        args = self.held(cmd='gdb', build_record='r.json', sketch='echo', ex=[], timeout=5)
        with self.assertRaisesRegex(H.Refused, 'rig_jlink has no debug.openocd'):
            H.op_gdb(args, {**self.board('rig_jlink'), 'debug': {}}, self.dir, None, None)

    def test_foreign_or_missing_hold_refused(self):
        with self.assertRaisesRegex(H.NotHeld, 'not held'):
            H.held_by('rig_jlink', 'mine')
        self.lock.records['rig_jlink'] = {'reason': 'someone else', 'pid': 1}
        with self.assertRaisesRegex(H.NotHeld, 'not held'):
            H.held_by('rig_jlink', 'mine')
        self.lock.records['rig_jlink'] = {'reason': 'mine', 'pid': 1}
        H.held_by('rig_jlink', 'mine')
        with mock.patch.object(self.lock, 'is_locked', return_value=False):   # a second read, unreadable
            H.held_by('rig_jlink', 'mine')   # the one record read decides owner and liveness

    def test_a_session_command_not_held_exits_not_held_unless_the_record_is_unreadable(self):
        argv = ['exec', '-b', 'rig_jlink', '--under-hold', 'mine', str(self.roster), '--', 'true']
        with mock.patch.object(H, 'git_identity', return_value=('abc', '')):
            self.assertEqual(self.main(argv)[0], H.NOT_HELD)   # its holder gone: a restore released it, or it died
            self.lock.records['rig_jlink'] = 'unreadable'   # the flock may still be held: unknown, not "not held"
            code, out = self.main(argv)
            self.assertEqual(code, 2)
            self.assertIn('unreadable', out['error'])
            del self.lock.records['rig_jlink']
            self.lock.pending[('rig_jlink', H.PENDING_KIND)] = [{'receipt': {'pid': 7}}]
            self.A.process.recheck.return_value = 'pending'
            self.assertEqual(self.main(argv)[0], H.NOT_HELD)   # an older engine's pending record refuses nothing

    def test_check_runs_a_device_test_without_flashing(self):
        args = argparse.Namespace(board='rig_ocd', test=None)
        image = {'vidPid': '239a:cafe'}
        self.HP.TEST_FUNCS['echo'] = lambda *a: (True, 'echoed')
        with mock.patch.object(H, 'admit_sketch', return_value=({'record': 'r'}, image, {})) as admitted, \
                mock.patch.object(H, 'dut_devices', return_value=[{'busport': '21-1.3'}]), \
                mock.patch.object(H, 'flash') as flashed:
            row = H.op_check(args, self.board('rig_ocd'), self.dir, mock.Mock(), None)
        self.assertEqual((row['pass'], row['test'], row['device']['busport']), (True, 'echo', '21-1.3'))
        self.assertEqual(admitted.call_args[0][1], 'echo')
        flashed.assert_not_called()
        args.test = 'inout'
        with mock.patch.object(H, 'admit_sketch', return_value=({}, image, {})) as admitted, \
                mock.patch.object(H, 'dut_devices', return_value=[]):
            self.assertIn('found 0', H.op_check(args, self.board('rig_ocd'), self.dir, mock.Mock(), None)['detail'])
        self.assertEqual(admitted.call_args[0][1], 'inout')
        args.board = 'rig_host'
        with self.assertRaisesRegex(H.Refused, 'host board'):
            H.op_check(args, self.board('rig_host'), self.dir, mock.Mock(), None)


class ReleaseHelper(unittest.TestCase):
    """release_hold's `hil_lock.py release` survives a signal to its caller's process group (agentrc#477): a tty
    Ctrl-C, or `timeout` without --foreground, would kill it before it signalled the holder."""

    HELPER = ('import os, pathlib, sys, time\n'
              'd = pathlib.Path(__file__).parent\n'
              '(d / "started").write_text(str(os.getpid()))\n'
              'time.sleep(1)\n'
              '(d / "released").write_text(" ".join(sys.argv[1:]))\n')
    CALLER = ('import json, signal, sys\n'
              'from pathlib import Path\n'
              'sys.path.insert(0, sys.argv[2])\n'
              'from helpers import load_arduino_engine\n'
              'H = load_arduino_engine("skills/arduino/scripts/arduino_hil.py")\n'
              'H.A.root = Path(sys.argv[1])\n'
              'signal.signal(signal.SIGINT, signal.default_int_handler)\n'
              'try:\n'
              '    H.release_hold("rig_jlink")\n'
              'except KeyboardInterrupt:\n'
              '    print("interrupted", (Path(sys.argv[1]) / H.LOCK_SCRIPT).with_name("released").exists(), flush=True)\n')

    def released_through(self, sig):
        """Signal the caller's whole group once the helper started; (caller's output, its exit, helper's argv)."""
        root = tmpdir(self)
        script = root / H.LOCK_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text(self.HELPER)
        caller = subprocess.Popen([sys.executable, '-c', self.CALLER, str(root), str(Path(__file__).resolve().parent)],
                                  stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(caller.stdout.close)
        self.addCleanup(kill_quietly, caller.pid, signal.SIGKILL)
        started, deadline = script.with_name('started'), time.monotonic() + 30
        while not started.exists() or not started.read_text():
            self.assertIsNone(caller.poll(), 'the caller exited before its helper started')
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        os.killpg(caller.pid, sig)
        out = caller.stdout.read()
        code = caller.wait(timeout=30)
        helper, deadline = int(started.read_text()), time.monotonic() + 10
        while pid_alive(helper):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        released = script.with_name('released')
        return out, code, released.read_text() if released.exists() else None

    def test_a_group_sigint_waits_for_the_release_then_interrupts(self):
        self.assertEqual(self.released_through(signal.SIGINT), ('interrupted True\n', 0, 'release rig_jlink'))

    def test_a_group_sigterm_ends_the_caller_but_not_the_release(self):
        out, code, released = self.released_through(signal.SIGTERM)
        self.assertEqual((code, released), (-signal.SIGTERM, 'release rig_jlink'))


class Commission(Base):
    def args(self, board):
        return argparse.Namespace(board=board, roster=str(self.roster), record_dir=str(self.dir))

    def commission(self, board, image, **patches):
        ino = [0]

        def bump(*a):
            ino[0] += 1
            return ''
        patches = {'admit': mock.Mock(return_value=({}, {'echo': image})), 'detach': mock.Mock(), 'flash': bump,
                   'reset': bump, **patches}
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(H, name, value))
            stack.enter_context(mock.patch.object(H.time, 'sleep'))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = H.cmd_commission(self.args(board))
        return code, json.loads(out.getvalue().splitlines()[-1]), ino

    def test_refuses_a_host_board(self):
        with self.assertRaisesRegex(H.Refused, 'host board'):
            H.cmd_commission(self.args('rig_host'))
        self.assertEqual(self.lock.taken, [])

    def test_by_port(self):
        ino = [0]
        self.A.usb.usb_devices.side_effect = lambda **k: [{'busport': '11-1.3', 'ino': ino[0], 'dir': '/x'},
                                                          {'busport': '11-1.1', 'ino': 1, 'dir': '/y'}]

        def bump(*a):
            ino[0] += 1
            return ''
        test = mock.Mock(return_value=(True, 'echoed'))
        self.HP.TEST_FUNCS['echo'] = test
        code, result, _ = self.commission('rig_esp', {'vidPid': '303a:1001'}, flash=bump, reset=bump,
                                          no_serial=mock.Mock(return_value=True))
        self.assertEqual(code, 0)
        self.assertEqual((result['port'], result['matchesRoster'], result['observed']),
                         ('11-1.3', True, [['11-1.3'], ['11-1.3']]))
        self.assertEqual(test.call_args[0][0]['busport'], '11-1.3')
        self.assertEqual(self.lock.taken[0][:3], ('rig_esp', 'hil_test.py commission', True))
        self.assertEqual(len(self.lock.released), 1)

    def serial_devices(self, ino, *extra):
        """The DUT re-enumerating at 21-1.3 with the roster serial, beside other devices of the same VID:PID."""
        def scan(vid_pid=None, serial=None):
            devs = [{'serial': 'OUID', 'busport': '21-1.3', 'ino': ino[0]}, *[dict(d, ino=ino[0]) for d in extra]]
            return [d for d in devs if serial is None or d['serial'] == serial]
        self.A.usb.usb_scan.side_effect = scan

    def test_by_serial_runs_the_profiles_restore_test(self):
        ino = [0]
        self.serial_devices(ino)
        self.HP.RESTORE_TEST = {'device': 'inout', 'host': 'info'}
        test = mock.Mock(return_value=(True, 'echoed'))
        self.HP.TEST_FUNCS['inout'] = test

        def bump(*a):
            ino[0] += 1
            return ''
        code, result, _ = self.commission('rig_ocd', {'vidPid': '239a:cafe'}, flash=bump, reset=bump)
        self.assertEqual((code, result['serial'], result['matchesRoster'], result['cleanup']), (0, 'OUID', True,
                                                                                             'echoed'))
        self.assertEqual(test.call_args[0][5], self.board('rig_ocd'))

    def test_by_serial_takes_only_the_roster_port_when_set(self):
        ino = [0]
        self.serial_devices(ino, {'serial': 'OTHER', 'busport': '21-1.4'})

        def bump(*a):
            ino[0] += 1
            return ''
        code, result, _ = self.commission('rig_ocd', {'vidPid': '239a:cafe'}, flash=bump, reset=bump)
        self.assertEqual(code, 4)   # two serials re-enumerated: ambiguous, and the board stays held
        self.assertIn('ambiguous', result['error'])
        fake.roster(self.roster, rig_ocd={'usb_port': '21-1.3'})
        code, result, _ = self.commission('rig_ocd', {'vidPid': '239a:cafe'}, flash=bump, reset=bump)
        self.assertEqual((code, result['serial'], result['observed']), (0, 'OUID', [['OUID'], ['OUID']]))

    def test_by_serial_refuses_more_than_one_device_at_the_port(self):
        ino = [0]
        self.serial_devices(ino, {'serial': 'OUID', 'busport': '21-1.3'})
        fake.roster(self.roster, rig_ocd={'usb_port': '21-1.3'})

        def bump(*a):
            ino[0] += 1
            return ''
        code, result, _ = self.commission('rig_ocd', {'vidPid': '239a:cafe'}, flash=bump, reset=bump)
        self.assertEqual(code, 4)
        self.assertIn('want one 239a:cafe with serial OUID, found 2', result['error'])
        self.assertEqual((result['results'][0]['cleanup']['released'], len(self.lock.ended)), (True, 1))

    def test_a_failed_flash_names_the_base_state(self):
        def flash(f, image, *a):
            image['base']['state'] = 'matched'
            return 'J-Link flash: x'
        code, result, _ = self.commission('rig_jlink', {'vidPid': '1234:5678', 'base': {'state': None}}, flash=flash,
                                          scan=mock.Mock(return_value=[]))
        self.assertEqual((code, result['base'], result['error']),
                         (4, 'matched', 'RuntimeError: flash: J-Link flash: x'))

    def test_unclean_commission_logs_its_children_and_keeps_a_report(self):
        def detach(*a):
            a[-1]._note('pending', {'pid': 7}, ['JLinkExe'])
            a[-1].check()
        code, result, _ = self.commission('rig_jlink', {'vidPid': '1234:5678'}, detach=detach,
                                          scan=mock.Mock(return_value=[]))
        self.assertEqual(code, 4)
        self.assertEqual((self.lock.ended[0][2], self.lock.pending), ([{'pid': 7}], {}))
        self.A.process.recheck.return_value = 'pending'
        report = next(self.root.glob('build/hil-manual/commission-rig_jlink-*/result.json'))
        self.assertTrue(json.loads(report.read_text())['results'][0]['cleanup']['released'])
        restore = argparse.Namespace(board='rig_jlink', report=str(report))
        with self.assertRaisesRegex(H.Refused, 'not proven ended'):
            H.op_restore(restore, None, self.dir / 'run', None, None)

    def test_a_live_invocation_refuses_commissioning_before_hardware(self):
        other = {'receipt': {'ops': '/o', 'pid': 99, 'start': '5'}, 'cmd': 'hil_test.py', 'state': 'alive'}
        self.lock.claims['rig_jlink'] = [other]
        flash = mock.Mock(side_effect=AssertionError('flashed'))
        self.lock.failures['rig_jlink'] = [{'id': 'f1', 'receipts': []}]
        with self.assertRaisesRegex(H.Refused, 'is running') as refused:
            self.commission('rig_jlink', {'vidPid': '1234:5678'}, flash=flash)
        self.assertEqual((len(self.lock.released), self.lock.ended, self.lock.claims['rig_jlink']), (1, [], [other]))
        self.assertEqual(H.refusal_report(refused.exception)['previousCleanupFailure']['id'], 'f1')   # main's output

    def test_an_unusable_run_dir_refuses_before_the_lock(self):
        with mock.patch.object(H, 'Tracker', side_effect=PermissionError('chmod')), \
                self.assertRaisesRegex(H.Refused, 'run dir .*chmod'):
            H.cmd_commission(self.args('rig_jlink'))
        self.assertEqual(self.lock.taken, [])

    def test_interrupt_after_the_release_is_not_a_held_board(self):
        ino = [0]

        def detach(*a):
            ino[0] += 1

        def release(fh):   # SIGTERM lands just after the lock is gone
            self.lock.released.append(fh)
            raise KeyboardInterrupt
        with mock.patch.object(self.lock, 'release_board_lock', release), self.assertRaises(KeyboardInterrupt):
            self.commission('rig_jlink', {'vidPid': '1234:5678'}, detach=detach, flash=mock.Mock(return_value=''),
                            reset=mock.Mock(return_value=''), scan=lambda *a: [{'serial': 'JUID', 'ino': ino[0]}])
        self.assertEqual((len(self.lock.released), self.lock.ended), (1, []))
        self.assertFalse(list(self.root.glob('build/hil-manual/commission-rig_jlink-*/result.json')))


class ClaimsOnTheRealLock(Base):
    """The claim against agentrc's real hil_lock and hil_process: a real earlier invocation, SIGKILLed while the child
    its run_tracked command left still runs under its supervisor, or still alive (agentrc#467)."""

    def setUp(self):
        super().setUp()
        self.locks = self.dir / 'locks'
        self.patch(REAL.lock, 'BOARD_LOCK_DIR', str(self.locks))
        self.patch(self.A, 'lock', REAL.lock)
        self.patch(self.A, 'process', REAL.process)

    def run_board(self, admit=AssertionError('admitted')):
        with mock.patch.object(H, 'probe_present', return_value=''), mock.patch.object(H, 'admit', side_effect=admit):
            return H.run_board('rig_jlink', self.board('rig_jlink'), ['echo'], self.dir / 'rec.json', self.roster,
                               self.dir / 'run', None)

    def claims(self):
        return REAL.lock.PendingSessions('rig_jlink', H.CLAIM_KIND).read()

    def assert_released(self):
        fh = REAL.lock.take_board_lock('rig_jlink', 'selftest')
        REAL.lock.release_board_lock(fh)

    @staticmethod
    def meanwhile(action):
        """PendingSessions.claim patched so that `action(record)` runs right after this process's own claim, as
        another claimant would between that claim and the judgement."""
        real = REAL.lock.PendingSessions.claim

        def claim(record, entry):
            others = real(record, entry)
            if entry['receipt']['pid'] == os.getpid():
                action(record)
            return others
        return mock.patch.object(REAL.lock.PendingSessions, 'claim', claim)

    def logs_dead(self, dead, child, logged, then=lambda: None):
        """A claimant's judgement of `dead`: its receipts logged, its claim removed, then `then()` and its child
        ended."""
        def action(record):
            receipts = REAL.lock.invocation_state(dead)[1]
            logged.append(REAL.lock.log_cleanup_failure('rig_jlink', 'live', detail='x', task=dead['receipt'],
                                                        receipts=receipts))
            record.remove(dead)
            then()
            kill_quietly(child, signal.SIGKILL)
            wait_done(REAL.process.recheck, receipts[0])
        return action

    def test_a_dead_invocations_live_child_holds_the_board_until_it_ends(self):
        dead, child = killed_claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'dead')
        self.patch(H, 'CLAIM_SETTLE', 0.5)
        row = self.run_board()
        self.assertEqual((row['locked'], H.verdict(row), row['cleanup']['state']), (True, 'not-run', 'untouched'))
        self.assertIn('still run after 0.5s', row['detail'])
        failure = row['previousCleanupFailure']
        self.assertEqual((failure['task'], len(failure['receipts'])), (dead['receipt'], 1))
        self.assertEqual(REAL.process.recheck(failure['receipts'][0]), 'pending')
        self.assertEqual(self.claims(), [dead])   # kept: the next attempt judges it again
        self.assert_released()
        row = self.run_board()   # the child still runs: refused again, nothing logged again
        self.assertEqual((row['locked'], row['previousCleanupFailure']['id']), (True, failure['id']))
        self.assertEqual((len(REAL.lock.open_cleanup_failures('rig_jlink')), self.claims()), (1, [dead]))
        kill_quietly(child, signal.SIGKILL)
        wait_done(REAL.process.recheck, failure['receipts'][0])
        row = self.run_board(admit=H.Refused('stop here'))
        self.assertEqual((row['locked'], row['detail'], self.claims()), (False, 'refused: stop here', []))
        self.assertEqual(len(os.listdir(dead['receipt']['ops'])), 3)   # kept for the logged failure's resolve
        self.assertEqual(REAL.lock.resolve_restored('rig_jlink', [failure], evidence='restored'), [])

    def test_an_unknown_receipt_is_logged_and_never_holds_the_board(self):
        dead, out = claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'dead')
        dead.kill()
        dead.wait()
        ops = Path(out['entry']['receipt']['ops'])
        (ops / 'hil_op_feed.log').write_text('')   # no lock or record: never rechecks done
        started = time.monotonic()
        row = self.run_board(admit=H.Refused('stop here'))
        self.assertLess(time.monotonic() - started, 5)   # nothing pending: no wait
        self.assertEqual((row['locked'], row['detail'], self.claims()), (False, 'refused: stop here', []))
        self.assertEqual(row['previousCleanupFailure']['receipts'][0]['unrecheckable'], 'no .lock or .json file')

    def test_a_refusal_for_a_live_claimant_never_tidies_a_receipt_it_logged(self):
        dead, child = killed_claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'dead')
        claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'live')
        logged = []
        with self.meanwhile(self.logs_dead(dead, child, logged)):
            row = self.run_board()
        self.assertIn('is running', row['detail'])
        self.assertEqual(REAL.process.recheck(logged[0]['receipts'][0]), 'done')
        self.assertEqual(REAL.lock.resolve_restored('rig_jlink', logged, evidence='restored'), [])

    def test_a_failure_another_claimant_logged_after_the_take_is_reported_and_resolved(self):
        dead, child = killed_claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'dead')
        live, _ = claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'live')
        logged, resolved, resolve_restored = [], [], REAL.lock.resolve_restored

        def exits():
            live.stdin.close()
            live.wait()

        def resolve(board, failures, **evidence):
            resolved.append([f['id'] for f in failures])
            return resolve_restored(board, failures, **evidence)
        with self.meanwhile(self.logs_dead(dead, child, logged, then=exits)), \
                mock.patch.object(REAL.lock, 'resolve_restored', resolve), \
                mock.patch.object(H, 'probe_present', return_value=''), \
                mock.patch.object(H, 'admit', return_value=({'source': {'head': 'abc'}, 'fqbn': 'f', 'platform': {},
                                                             'sketches': {}}, {'echo': {'sha256': {}, 'fqbn': 'f'}})), \
                mock.patch.object(H, 'step', return_value={'test': 'echo', 'pass': True, 'detail': 'ok'}), \
                mock.patch.object(H, 'restore', return_value={'pass': True, 'state': 'echo restored',
                                                              'evidence': 'ok'}):
            row = H.run_board('rig_jlink', self.board('rig_jlink'), ['echo'], self.dir / 'rec.json', self.roster,
                              self.dir / 'run', None)
        self.assertEqual((H.verdict(row), row['previousCleanupFailure']['id']), ('pass', logged[0]['id']))
        self.assertEqual(resolved, [[logged[0]['id']]])
        self.assertEqual(REAL.lock.open_cleanup_failures('rig_jlink'), [])   # its receipt kept, so resolved

    def test_an_exited_unreaped_claimant_is_not_alive(self):
        zombie, _ = claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'zombie')
        zombie.kill()
        deadline = time.monotonic() + 10
        while proc_stat(zombie.pid)[1] != 'Z':
            self.assertLess(time.monotonic(), deadline, 'not a zombie once SIGKILLed')
            time.sleep(0.05)
        row = self.run_board(admit=H.Refused('stop here'))
        self.assertEqual((row['locked'], row['detail'], self.claims()), (False, 'refused: stop here', []))

    def test_a_live_invocation_refuses_the_run_and_keeps_its_claim(self):
        live, out = claimant(self, self.locks, 'rig_jlink', H.CLAIM_KIND, self.dir / 'live')
        row = self.run_board()
        self.assertEqual((row['locked'], H.verdict(row)), (True, 'not-run'))
        self.assertIn('is running', row['detail'])
        self.assertEqual(self.claims(), [out['entry']])
        self.assert_released()


class Report(Base):
    def test_render_verdicts_and_exit(self):
        self.A.report.table = lambda headers, rows: '|'.join(headers) + '\n' + '\n'.join('|'.join(r) for r in rows)
        row = {'board': 'b', 'ran': True, 'pass': False, 'locked': False, 'recoveryBlocker': False,
               'tests': [{'test': 'echo', 'pass': False, 'detail': 'no echo'}],
               'cleanup': {'pass': True, 'state': 'echo restored'}, 'detail': ''}
        md = H.render({'pass': False, 'caveat': 'aborted', 'exitCode': 1, 'results': [row]})
        self.assertIn('b echo: no echo', md)
        self.assertIn('caveat: aborted', md)
        self.assertEqual((H.verdict(row), H.exit_code([row]), H.exit_code([])), ('fail', 1, 1))
        self.assertNotIn('bundled copy', md)
        row['firmware'] = {'driftedHeaders': ['src/a.h', 'src/b.h']}
        self.assertIn('- b: its core\'s bundled copy of the library differs from the checkout in src/a.h, src/b.h; '
                      'the result covers that mix', H.render({'pass': True, 'caveat': '', 'exitCode': 0,
                                                             'results': [row]}))

    def test_schema_is_the_profiles(self):
        self.A.report.BodySchema = lambda **kw: kw
        sch = H.schema()
        self.assertEqual((sch['name'], sch['stem']), ('fake-usb-hil', 'hil_report'))
        with self.assertRaisesRegex(ValueError, 'report body needs'):
            sch['check']({'pass': True})

    def test_requested_boards_come_from_the_recorded_selection(self):
        self.A.report.read_document.return_value = argparse.Namespace(
            outcome='valid', meta={'selection': ['rig_jlink', 'rig_ocd'], 'termination': 'aborted'},
            body={'caveat': 'aborted by a signal', 'results': []})
        self.A.report.apply_run_rules.side_effect = lambda sch, req, rows, term, cav: {
            'verdict': 'blocker', 'rows': [{'board': b, 'verdict': 'blocker', 'detail': 'missing'} for b in req]}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            H.cmd_report(argparse.Namespace(run_dir=str(self.dir), board=None))
        got = json.loads(out.getvalue())
        self.assertEqual(self.A.report.apply_run_rules.call_args[0][1], ['rig_jlink', 'rig_ocd'])
        self.assertEqual((got['pass'], got['wedged'], [r['verdict'] for r in got['results']]),
                         (False, [], ['blocker', 'blocker']))


if __name__ == '__main__':
    unittest.main()
