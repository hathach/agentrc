"""skills/arduino/scripts/arduino_remote.py stages an Arduino library's HIL run or session on a rig: the contract files,
the roster, the agentrc closure and HEAD's build records with their images, pinned by manifest.json, refusing before
the rig whatever the harness there would refuse. Moved from Adafruit_TinyUSB_Arduino's hil_remote.py (2989bca) with
its .claude/test/test_hil_remote.py, run here against a fake library (tests/fixtures/arduino_lib) in a temporary git
checkout, with every hil_remote_core call that reaches a rig stubbed. Stubs prove the plumbing only; the transport is
verified by a real run."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the arduino engines are Linux only (fcntl)')

import contextlib
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, load_arduino_engine, load_arduino_fixture, git_env, tmpdir  # noqa: E402

R = load_arduino_engine('skills/arduino/scripts/arduino_remote.py')
fake = load_arduino_fixture()
BOARD = 'rig_ocd'
ROSTER = 'test/hil/ci.json'


def git(root, *a):
    return subprocess.run(['git', '-C', str(root), *a], check=True, capture_output=True, text=True,
                          env=git_env()).stdout.strip()


def setUpModule():
    guard_survivors(__name__)



class Checkout(unittest.TestCase):
    """The fake library as a clean git checkout with its roster and one clean build record at HEAD."""

    def setUp(self):
        tmp = tmpdir(self)
        self.root = fake.library(tmp / 'lib')
        fake.roster(self.root / ROSTER)
        (self.root / '.gitignore').write_text('/build/\n/.hil-remote/\n')
        git(self.root, 'init', '-q')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'init')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        self.agentrc = fake.agentrc(tmp / 'agentrc')
        self.BP = fake.build_profile(self.root, self.agentrc)
        self.HP = fake.hil_profile(self.BP)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target, name, value in ((R, 'P', self.HP), (R.H, 'P', self.HP), (R.B, 'P', self.BP)):
            stack.enter_context(mock.patch.object(target, name, value))
        self.record()
        self.staged, self.calls, self.rig_exit = {}, [], {}
        tree = R.core.Tree('ci.lan', '/home/u/hil-remote/fake-usb/r1', '/home/u/hil-remote/fake-usb/r1.lease', 'tok',
                           self.root)

        def stage_tree(t, src, dest='.'):
            for p in Path(src).rglob('*'):
                if p.is_file():
                    self.staged[p.relative_to(src).as_posix()] = p.read_bytes()

        def create_attempt(root, runs, run_id, host, base, *a):   # the real one writes its record under runs first
            self.created = (run_id, host, base)
            Path(runs).mkdir(exist_ok=True)
            return tree
        for name, fn in (('create_attempt', create_attempt), ('load_attempt', lambda *a: tree),
                         ('stage_tree', stage_tree), ('verify_remote', lambda t, d, base='.': []),
                         ('launch', lambda t, argv, env=None, cwd='.': self.calls.append(argv) or {'pid': 9}),
                         ('run_remote', self.run_remote)):
            stack.enter_context(mock.patch.object(R.core, name, fn))

    def run_remote(self, tree, argv, env=None, cwd='.'):
        self.calls.append(argv)
        return self.rig_exit.get(argv[3], 0)

    def record(self, board=BOARD, **over):
        elf = self.root / f'build/hil/{board}/echo/echo.ino.elf'
        elf.parent.mkdir(parents=True, exist_ok=True)
        elf.write_bytes(b'elf')
        rec = {'kind': 'arduino-hil-build', 'version': 2, 'library': fake.NAME, 'board': board, 'experimental': False,
               'source': {'head': self.head}, **R.B.contract_identity(),
               'sketches': {'echo': {'sha256': {elf.relative_to(self.root).as_posix(): hashlib.sha256(b'elf')
                                                .hexdigest()}}}}
        rec.update(over)
        path = self.root / f'build/hil/{BOARD}.build.json'
        path.write_text(json.dumps(rec))
        return path

    def main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = R.main(list(argv))
        lines = out.getvalue().strip().splitlines()
        return rc, json.loads(lines[-1]) if lines and lines[-1].startswith('{') else None


class Run(Checkout):
    def test_stages_a_manifest_pinned_tree_and_launches_the_battery(self):
        rc, out = self.main('run', '--run-id', 'r1', '-b', BOARD, '-t', 'echo')
        self.assertEqual((rc, out['boards'], out['launch']), (0, [BOARD], {'pid': 9}))
        self.assertEqual(self.created, ('r1', 'ci.lan', '~/hil-remote/fake-usb'))
        manifest = json.loads(self.staged[R.MANIFEST])
        self.assertEqual(manifest['head'], self.head)
        self.assertEqual(manifest['agentrc'], {'commit': 'a' * 40, 'dirty': False})
        pinned = [*self.BP.CONTRACT_FILES, ROSTER, f'build/hil/{BOARD}.build.json',
                  f'build/hil/{BOARD}/echo/echo.ino.elf', *(f'agentrc/{f}' for f in self.agentrc['files'])]
        self.assertEqual(sorted(manifest['files']), sorted(pinned))
        for f in pinned:
            self.assertEqual(manifest['files'][f], hashlib.sha256(self.staged[f]).hexdigest(), f)
        self.assertEqual(self.calls[0], ['python3', '-u', self.HP.HIL, 'run', '--staged', R.MANIFEST, '--run-dir',
                                         R.RUN_DIR, '-b', BOARD, '-t', 'echo', ROSTER])

    def test_staging_refuses_agentrc_files_changed_since_load(self):
        (self.agentrc['root'] / 'skills/hil/scripts/hil_lock.py').write_text('# pulled meanwhile\n')
        with self.assertRaisesRegex(SystemExit, r"agentrc changed since this process loaded it: "
                                                r"\['skills/hil/scripts/hil_lock.py'\]; rerun"):
            R.snapshot(tmpdir(self), [], with_agentrc=True)

    def test_test_choices_are_the_profiles(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as e:
            self.main('run', '--run-id', 'r1', '-t', 'cdc_echo')
        self.assertEqual(e.exception.code, 2)
        # newer argparse quotes each choice
        self.assertIn("choose from echo, info, inout, probe", err.getvalue().replace("'", ''))
        with self.assertRaisesRegex(SystemExit, 'no selected board runs any of'):
            self.main('run', '--run-id', 'r1', '-b', 'rig_host', '-t', 'echo')

    def test_refusals_come_before_the_rig(self):
        cases = ((lambda: self.record(source={'head': 'old'}), 'built at old'),
                 (lambda: self.record(experimental=True), 'experimental'),
                 (lambda: (self.root / f'build/hil/{BOARD}.build.json').unlink(), 'build record'),
                 (lambda: (self.root / ROSTER).write_text((self.root / ROSTER).read_text() + ' '), 'not clean'),
                 (lambda: self.record(version=1), 'not a version 2 arduino-hil-build record: rebuild it'),
                 (lambda: self.record(library='Other Library'), 'it is a Other Library record, this harness is Fake '
                                                                'USB Library\'s'),
                 (lambda: self.record(contract={}), r"the contract files \['contract/check.py', 'contract/pin.json'\] "
                                                    r"differ"),
                 (lambda: (self.agentrc['root'] / 'skills/hil/scripts/hil_lock.py').write_text('# another\n'),
                  'built with other agentrc files'),
                 (lambda: self.record(board='rig_jlink'), 'it is for rig_jlink, not rig_ocd'))
        for spoil, why in cases:
            with self.subTest(why=why):
                git(self.root, 'checkout', '-q', '--', '.')
                (self.agentrc['root'] / 'skills/hil/scripts/hil_lock.py').write_text(
                    '# agentrc skills/hil/scripts/hil_lock.py\n')
                self.record()
                spoil()
                with self.assertRaisesRegex(SystemExit, why):
                    self.main('run', '--run-id', 'r2', '-b', BOARD)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.staged, {})

    def test_record_files_refuses_a_contract_file_changed_since_the_record(self):
        path = self.record()
        self.assertEqual(R.record_files(path, self.head, BOARD)[1], [f'build/hil/{BOARD}.build.json',
                                                                     f'build/hil/{BOARD}/echo/echo.ino.elf'])
        (self.root / 'contract/pin.json').write_text('{"commit": "moved"}\n')
        with self.assertRaisesRegex(SystemExit, r"contract files \['contract/pin.json'\] differ .*; rebuild with the "
                                                r"build skill's --record"):
            R.record_files(path, self.head, BOARD)
        (self.root / 'contract/pin.json').write_text('{"commit": "pinned"}\n')
        R.record_files(path, self.head, BOARD)
        (self.agentrc['root'] / fake.SELF).write_text('# a newer engine\n')
        with self.assertRaisesRegex(SystemExit, 'built with other agentrc files'):
            R.record_files(path, self.head, BOARD)

    def test_a_base_images_files_are_staged_and_pinned_with_the_record(self):
        base = self.root / 'base.hex'
        base.write_text(':00000001FF\n')
        self.BP.BOARDS[BOARD]['base'] = ('base.hex',)
        path = self.record(base={'files': {'base.hex': hashlib.sha256(base.read_bytes()).hexdigest()}})
        self.assertEqual(R.record_files(path, self.head, BOARD)[1][-1], 'base.hex')
        base.write_text(':00000001FF\n\n')
        with self.assertRaisesRegex(SystemExit, 'base.hex changed since the build record'):
            R.snapshot_records(self.root / 'stage', [R.record_files(path, self.head, BOARD)])

    def test_record_files_takes_an_experimental_one_only_unclean(self):
        path = self.record(experimental=True, patch={'path': f'build/hil/{BOARD}.build.patch', 'sha256': 'x'})
        with self.assertRaisesRegex(SystemExit, 'experimental'):
            R.record_files(path, self.head, BOARD)
        self.assertEqual(R.record_files(path, self.head, BOARD, clean=False)[1][-1], f'build/hil/{BOARD}.build.patch')
        path.write_text('{')
        with self.assertRaisesRegex(SystemExit, 'build record .*JSONDecodeError'):
            R.record_files(path, self.head, BOARD)

    def test_image_changed_after_the_record_is_refused(self):
        (self.root / f'build/hil/{BOARD}/echo/echo.ino.elf').write_bytes(b'other')
        with self.assertRaisesRegex(SystemExit, 'changed since the build record'):
            self.main('run', '--run-id', 'r3', '-b', BOARD)

    def test_staged_tree_differing_on_the_rig_is_refused(self):
        rec = f'build/hil/{BOARD}.build.json'
        with mock.patch.object(R.core, 'verify_remote', lambda t, d, base='.': [rec]), \
                self.assertRaisesRegex(SystemExit, f'differ on ci.lan: {rec}'):
            self.main('run', '--run-id', 'r5', '-b', BOARD)
        self.assertEqual(self.calls, [])

    def test_a_submodule_change_an_ignore_hides_is_refused_before_the_rig(self):
        dirty = [{'path': 'libraries/X', 'commit': 'a', 'state': ' ', 'dirty': True}]
        with mock.patch.object(R.B, 'submodules', return_value=dirty), \
                self.assertRaisesRegex(SystemExit, r'not clean.*\n~~ libraries/X'):
            self.main('run', '--run-id', 'r6', '-b', BOARD)
        with mock.patch.object(R.B, 'submodules', side_effect=[[], dirty]), \
                self.assertRaisesRegex(SystemExit, 'moved while it was copied: libraries/X has changes'):
            self.main('run', '--run-id', 'r7', '-b', BOARD)
        self.assertEqual((self.calls, self.staged), ([], {}))

    def test_unknown_board_and_foreign_roster_refused(self):
        with self.assertRaisesRegex(SystemExit, 'not in the roster'):
            self.main('run', '--run-id', 'r4', '-b', 'nope')
        with self.assertRaisesRegex(SystemExit, 'is not inside the checkout'):
            self.main('run', '--run-id', 'r4', '--roster', fake.__file__)


class Session(Checkout):
    def open(self):
        rc, out = self.main('session', 'open', '--run-id', 's1', '-b', BOARD)
        self.assertEqual((rc, out['held']), (0, True))
        return out

    def test_open_holds_the_board_under_a_reason_unique_to_the_attempt(self):
        out = self.open()
        self.assertEqual(self.calls[-1], ['python3', '-u', self.HP.HIL, 'lock', 'hold', BOARD, '--reason',
                                          out['reason']])
        self.assertRegex(out['reason'], r'^arduino-hil session /home/u/hil-remote/fake-usb/r1 [0-9a-f]{16}$')
        self.assertEqual(out['restoreRecord'], f'build/hil/{BOARD}.build.json')
        self.assertNotEqual(self.open()['reason'], out['reason'])   # the same run id again, another library's say

    def test_open_without_the_hold_says_so_and_acts_under_none(self):
        self.rig_exit['lock'] = 1
        rc, out = self.main('session', 'open', '--run-id', 's1', '-b', BOARD)
        self.assertEqual((rc, out['held']), (1, False))
        self.assertIn('the hold was not taken', out['error'])
        self.assertFalse(json.loads((self.root / '.hil-remote/s1.session.json').read_text())['held'])
        calls = len(self.calls)
        with self.assertRaisesRegex(SystemExit, 'session s1 does not hold rig_ocd'):
            R.main(['session', 'exec', 's1', 'exec', '--', 'true'])
        self.assertEqual(len(self.calls), calls)   # exec never reached the rig
        session = self.root / '.hil-remote/s1.session.json'
        self.rig_exit['release'] = 255
        rc, closed = self.main('session', 'close', 's1')
        self.assertEqual((rc, closed['closed'], closed['releaseExit']), (255, False, 255))
        self.assertNotIn('closed', json.loads(session.read_text()))
        self.rig_exit['release'] = R.H.NOT_HELD
        rc, closed = self.main('session', 'close', 's1')
        self.assertEqual((rc, closed['closed'], closed['restoreExit']), (0, True, None))
        self.assertIn('nothing was held, released or restored', closed['note'])
        self.assertEqual(self.calls[-1], ['python3', '-u', self.HP.HIL, 'release', '-b', BOARD, '--under-hold',
                                          out['reason']])
        record = json.loads(session.read_text())
        self.assertEqual((record['held'], record['closed']), (False, True))

    def test_a_session_losing_the_hold_cannot_release_the_winners(self):
        lock = R.H.A.lock
        with mock.patch.object(lock, 'BOARD_LOCK_DIR', str(tmpdir(self))):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(lock.cmd_hold([BOARD], 'arduino-hil session s1'), 0)   # another library's s1
            try:
                def rig(tree, argv, **kw):   # the rig: hold, or one session command under its reason
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        if argv[3:5] == ['lock', 'hold']:
                            return lock.cmd_hold([BOARD], argv[-1])
                        args = R.H.parse(argv[3:])
                        if args.cmd == 'release':
                            return R.H.cmd_release(args)
                        R.H.held_by(args.board, args.under_hold)
                        return lock.cmd_release([args.board])
                with mock.patch.object(R.core, 'run_remote', rig), \
                        mock.patch.object(R.H, 'release_hold', side_effect=AssertionError('released the winner')):
                    rc, opened = self.main('session', 'open', '--run-id', 's1', '-b', BOARD)
                    self.assertEqual((rc, opened['held']), (1, False))
                    with self.assertRaises(SystemExit):
                        R.main(['session', 'exec', 's1', 'restore', '--release'])
                    rc, closed = self.main('session', 'close', 's1')
                    self.assertEqual((rc, closed['releaseExit']), (0, R.H.NOT_HELD))
                self.assertTrue(lock.is_locked(BOARD))
                self.assertFalse(json.loads((self.root / '.hil-remote/s1.session.json').read_text())['held'])
            finally:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    lock.cmd_release([BOARD])

    def test_close_releases_a_hold_the_rig_took_after_open_lost_its_answer(self):
        lock = R.H.A.lock
        with mock.patch.object(lock, 'BOARD_LOCK_DIR', str(tmpdir(self))):
            try:
                def rig(tree, argv, **kw):   # the holder got its ack, then ssh dropped before `held:` reached us
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        if argv[3:5] == ['lock', 'hold']:
                            self.assertEqual(lock.cmd_hold([BOARD], argv[-1]), 0)
                            return 255
                        return R.H.cmd_release(R.H.parse(argv[3:]))

                def release(name):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        return {'exit': lock.cmd_release([name])}
                with mock.patch.object(R.core, 'run_remote', rig), mock.patch.object(R.H, 'release_hold', release):
                    rc, opened = self.main('session', 'open', '--run-id', 's1', '-b', BOARD)
                    self.assertEqual((rc, opened['held']), (1, False))
                    self.assertTrue(lock.is_locked(BOARD))
                    rc, closed = self.main('session', 'close', 's1')
                self.assertEqual((rc, closed['closed'], closed['releaseExit'], closed['restoreExit']),
                                 (0, True, 0, None))
                self.assertFalse(lock.is_locked(BOARD))
                record = json.loads((self.root / '.hil-remote/s1.session.json').read_text())
                self.assertEqual((record['held'], record['closed']), (False, True))
            finally:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    lock.cmd_release([BOARD])

    def test_close_whose_restore_lost_its_answer_closes_once_the_rig_holds_no_more(self):
        self.open()
        self.rig_exit['restore'] = 255   # the restore released the board, then ssh dropped
        rc, closed = self.main('session', 'close', 's1')
        self.assertEqual((rc, closed['closed']), (255, False))
        self.assertIn('rerun session close', closed['note'])
        self.assertTrue(json.loads((self.root / '.hil-remote/s1.session.json').read_text())['held'])
        self.rig_exit['restore'] = R.H.NOT_HELD
        rc, closed = self.main('session', 'close', 's1')
        self.assertEqual((rc, closed['closed'], closed['restoreExit']), (0, True, R.H.NOT_HELD))
        self.assertIn('the restore unconfirmed', closed['note'])
        record = json.loads((self.root / '.hil-remote/s1.session.json').read_text())
        self.assertEqual((record['held'], record['closed']), (False, True))

    def test_exec_adds_the_session_options_and_keeps_argv_after_the_roster(self):
        out = self.open()
        self.assertEqual(R.main(['session', 'exec', 's1', 'exec', '--timeout', '9', '--', 'ls', '-l']), 0)
        self.assertEqual(self.calls[-1], ['python3', '-u', self.HP.HIL, 'exec', '-b', BOARD, '--under-hold',
                                          out['reason'], '--staged', R.MANIFEST, '--timeout', '9', ROSTER,
                                          '--', 'ls', '-l'])
        R.main(['session', 'exec', 's1', 'gdb', '--build-record', 'r.json', '--sketch', 'echo', '--ex', 'bt'])
        self.assertEqual(self.calls[-1][-7:], ['--build-record', 'r.json', '--sketch', 'echo', '--ex', 'bt', ROSTER])
        for bad in (['flash', '-b', 'x'], ['flash', '--under-hold=x'], ['exec'], ['lock', 'status'], [],
                    ['flash', '--stag', 'x'], ['flash', '--stag='], ['flash', '--under', 'x'], ['flash', '--boa', 'x'],
                    ['flash', '-bX'], ['flash', '-b=X']):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                R.main(['session', 'exec', 's1', *bad])
        R.main(['session', 'exec', 's1', 'flash', '--bundle', 'b', '--build-record', 'r', '--sketch', 'echo'])
        self.assertEqual(self.calls[-1][-7:], ['--bundle', 'b', '--build-record', 'r', '--sketch', 'echo', ROSTER])
        R.main(['session', 'exec', 's1', 'console', '--baud', '9600'])
        self.assertEqual(self.calls[-1][-3:], ['--baud', '9600', ROSTER])
        with self.assertRaisesRegex(SystemExit, 'no session s9 was opened'):
            R.main(['session', 'exec', 's9', 'exec', '--', 'true'])

    def test_close_is_one_restore_that_releases(self):
        out = self.open()
        self.rig_exit['restore'] = 1
        rc, closed = self.main('session', 'close', 's1')
        self.assertEqual((rc, closed['closed']), (1, False))
        self.assertTrue(json.loads((self.root / '.hil-remote/s1.session.json').read_text())['held'])
        self.rig_exit['restore'] = 0
        rc, closed = self.main('session', 'close', 's1')
        self.assertEqual((rc, closed['closed']), (0, True))
        self.assertEqual(self.calls[-1], ['python3', '-u', self.HP.HIL, 'restore', '-b', BOARD, '--under-hold',
                                          out['reason'], '--staged', R.MANIFEST, '--bundle', R.RESTORE_BUNDLE,
                                          '--build-record', out['restoreRecord'], '--release', ROSTER])
        self.assertNotIn(['lock', 'release'], [c[3:5] for c in self.calls])

    def test_collect_exempts_only_the_records_a_session_never_writes(self):
        self.open()
        absent = {r: 'absent' for r in R.core.RECORD_FILES}
        for manual, rc in (('absent', 0), ('transport', 1)):
            with self.subTest(manual=manual), \
                    mock.patch.object(R.core, 'attempt_status', return_value={'state': 'not-launched'}), \
                    mock.patch.object(R.core, 'copy_back', return_value=R.core.CopyBack(
                        [], [], {**absent, 'build/hil-manual/': manual})):
                self.assertEqual(self.main('collect', 's1'), (rc, mock.ANY))

    def test_stage_puts_each_record_in_a_bundle_of_its_own(self):
        self.open()
        restore = f'{R.RESTORE_BUNDLE}/build/hil/{BOARD}/echo/echo.ino.elf'
        self.assertEqual(self.staged[restore], b'elf')
        self.assertIn(restore, json.loads(self.staged[R.MANIFEST])['files'])
        rec = self.record(experimental=True)
        patch = rec.with_suffix('.patch')
        patch.write_text('diff')
        data = json.loads(rec.read_text())
        data['patch'] = {'path': patch.relative_to(self.root).as_posix(),
                         'sha256': hashlib.sha256(b'diff').hexdigest()}
        rec.write_text(json.dumps(data))
        self.staged.clear()
        rc, out = self.main('session', 'stage', 's1', str(rec))
        bundle = out['bundle']
        self.assertEqual(rc, 0)
        self.assertEqual(out['use'], [f'--bundle {bundle} --build-record build/hil/{BOARD}.build.json'])
        self.assertIn(f'{bundle}/build/hil/{BOARD}.build.patch', self.staged)
        self.assertFalse([p for p in self.staged if p.startswith(R.RESTORE_BUNDLE) or p == R.MANIFEST])
        rc, out = self.main('session', 'stage', 's1', str(rec))
        self.assertNotEqual(out['bundle'], bundle)
        patch.write_text('changed')
        with self.assertRaisesRegex(SystemExit, 'changed since the build record'):
            self.main('session', 'stage', 's1', str(rec))
        self.record(board='rig_jlink')
        with self.assertRaisesRegex(SystemExit, 'not rig_ocd'):
            self.main('session', 'stage', 's1', str(rec))

    def test_stage_refuses_records_of_other_agentrc_files_than_the_session_opened_with(self):
        self.open()
        session = self.root / '.hil-remote/s1.session.json'
        self.assertEqual(json.loads(session.read_text())['agentrc'],
                         {'commit': 'a' * 40, 'files': self.agentrc['sha256']})
        rec = self.record(experimental=True)
        data = json.loads(session.read_text())
        session.write_text(json.dumps({**data, 'agentrc': {**data['agentrc'], 'commit': 'b' * 40}}))
        self.main('session', 'stage', 's1', str(rec))   # the commit is provenance: the files decide
        files = {**data['agentrc']['files'], 'skills/hil/scripts/hil_lock.py': '0' * 64}
        session.write_text(json.dumps({**data, 'agentrc': {**data['agentrc'], 'files': files}}))
        with self.assertRaisesRegex(SystemExit, 'not the ones the session opened with'):
            self.main('session', 'stage', 's1', str(rec))


if __name__ == '__main__':
    unittest.main()
