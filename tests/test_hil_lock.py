"""skills/hil/scripts/hil_lock.py is the board-lock primitive every project on a HIL host
shares, so its boundary is frozen: the lock dir and file names, a hold that never truncates a
loser's record, the flock as the only authority, the holder record's fields (foreign ones kept),
cleanup holders as recovery blockers, protected holders never signalled, and fail-closed
acquisition. Every lock lives in a temp dir under a name no rig uses."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_lock requires POSIX flock and fork')

import ast
import fcntl
import importlib.util
import io
import json
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'hil_lock.py'


def load():
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_lock', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hil_lock = load()
_sleep = time.sleep   # tests patch hil_lock.time.sleep, which is this same function

BOARD = 'hil-lock-agentrc-selftest-board'

# Another process holding BOARD's flock with the given record bytes ('PID' is replaced by its
# own pid); it prints 'ready' and holds until stdin closes.
HOLDER = r'''
import fcntl, os, sys
fh = open(os.path.join(sys.argv[1], sys.argv[2] + '.lock'), 'ab+')
fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
fh.truncate(0)
fh.write(bytes.fromhex(sys.argv[3]).replace(b'PID', str(os.getpid()).encode()))
fh.flush()
print('ready', flush=True)
sys.stdin.read()
'''

CLEANUP = {'reason': 'selftest cleanup', 'role': 'cleanup-holder', 'board': 'x',
           'detail': 'park failed', 'survivors': []}


@contextmanager
def quiet():
    """Swallow a command's stdout and stderr; yields their buffers."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        yield out, err


class LockDir(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = td.name
        self.patch(hil_lock, 'BOARD_LOCK_DIR', self.dir)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('HIL_NO_BOARD_LOCK', None)

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def holder(self, record: bytes):
        p = subprocess.Popen([sys.executable, '-c', HOLDER, self.dir, BOARD, record.hex()],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.stdin.close)
        self.addCleanup(p.stdout.close)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        return p

    def record(self, **fields) -> bytes:
        return json.dumps({'pid': 'PID', 'since': '2026-10-17T00:00:00+0000', **fields}
                          ).replace('"PID"', 'PID').encode()

    def lock_bytes(self) -> bytes:
        return Path(hil_lock.lock_path(BOARD)).read_bytes()

    def lock_dir_is_a_file(self):
        path = os.path.join(self.dir, 'not-a-dir')
        open(path, 'w').close()
        self.patch(hil_lock, 'BOARD_LOCK_DIR', path)

    def acquire(self):
        fh = hil_lock.acquire_board_lock(BOARD, reason='selftest')
        if fh is not None:
            self.addCleanup(fh.close)
        return fh

    def flock_is_free(self) -> bool:
        with open(hil_lock.lock_path(BOARD), 'r+') as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True


class LoadedByPath(unittest.TestCase):
    """Adapters load the module by file path and check these values (TinyUF2's runner refuses a
    helper whose protected reasons differ)."""

    def test_the_values_adapters_check(self):
        self.assertEqual(hil_lock.PROTECTED_REASONS, {'hil_test.py', 'pool_check'})
        self.assertEqual(hil_lock.CI_REASON, 'hil_test.py')
        self.assertEqual(hil_lock.CLEANUP_ROLE, 'cleanup-holder')
        self.assertTrue(issubclass(hil_lock.LockUnavailable, RuntimeError))

    def test_imports_only_the_standard_library(self):
        tree = ast.parse(SCRIPT.read_text())
        names = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertLessEqual(names, set(sys.stdlib_module_names))

    def test_lock_dir_and_file_name_are_shared_by_every_project(self):
        self.assertEqual(hil_lock.BOARD_LOCK_DIR, '/tmp/tinyusb-hil-locks')
        self.assertEqual(hil_lock.lock_path('some-board'), '/tmp/tinyusb-hil-locks/some-board.lock')


class Record(LockDir):
    def test_a_hold_records_pid_reason_and_since(self):
        self.acquire()
        info = hil_lock.read_record(BOARD)
        self.assertEqual(set(info), {'pid', 'reason', 'since'})
        self.assertEqual((info['pid'], info['reason']), (os.getpid(), 'selftest'))
        self.assertRegex(info['since'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}$')

    def test_foreign_fields_survive_read_record_and_holder_text(self):
        self.holder(self.record(**CLEANUP))
        info = hil_lock.read_record(BOARD)
        self.assertEqual({k: info[k] for k in CLEANUP}, CLEANUP)
        self.assertIsInstance(info['pid'], int)
        text = hil_lock.holder_text(info)
        self.assertIn(hil_lock.RECOVERY_BLOCKER, text)
        self.assertIn('"detail": "park failed"', text)
        plain = {'pid': 1, 'reason': 'r', 'since': 's', 'detail': 'kept'}
        self.assertEqual(hil_lock.holder_text(plain), json.dumps(plain))
        self.assertEqual(hil_lock.holder_text(None), 'unknown holder')

    def test_a_missing_or_unparsable_record_reads_as_none(self):
        self.assertIsNone(hil_lock.read_record(BOARD))
        Path(hil_lock.lock_path(BOARD)).write_bytes(b'\xff\xfe garbage')
        self.assertIsNone(hil_lock.read_record(BOARD))


class NeverTruncates(LockDir):
    def test_a_losing_acquire_or_hold_leaves_the_winners_record(self):
        self.holder(self.record(reason='winner', detail='kept'))
        before = self.lock_bytes()
        with self.assertRaises(BlockingIOError):
            hil_lock.flock_nb(BOARD)
        with self.assertRaises(RuntimeError):
            self.acquire()
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD], 'loser'), 1)
        self.assertEqual(self.lock_bytes(), before)


class FlockIsTheOnlyAuthority(LockDir):
    def test_is_locked_never_flocks(self):
        self.holder(self.record(reason='live'))
        self.patch(hil_lock.fcntl, 'flock', mock.Mock(side_effect=AssertionError('is_locked flocked')))
        self.assertTrue(hil_lock.is_locked(BOARD))

    def test_a_live_pid_record_without_the_flock_does_not_refuse_an_acquire(self):
        Path(hil_lock.lock_path(BOARD)).write_text(json.dumps({'pid': os.getppid(), 'reason': 'stale'}))
        self.assertIsNotNone(self.acquire())
        self.assertEqual(hil_lock.read_record(BOARD)['reason'], 'selftest')

    def test_a_dead_or_missing_pid_is_not_locked(self):
        dead = subprocess.Popen([sys.executable, '-c', ''])
        dead.wait()
        for rec in ({'pid': dead.pid}, {'pid': 0}, {'pid': 'x'}, {}):
            with self.subTest(rec=rec):
                Path(hil_lock.lock_path(BOARD)).write_text(json.dumps(rec))
                self.assertFalse(hil_lock.is_locked(BOARD))


class FailsClosed(LockDir):
    def assert_unavailable(self, prefix='board lock unavailable: '):
        with self.assertRaises(hil_lock.LockUnavailable) as cm:
            self.acquire()
        self.assertTrue(str(cm.exception).startswith(prefix), cm.exception)
        self.assertIsInstance(cm.exception.__cause__, OSError)
        return cm.exception

    def test_lock_dir_is_a_regular_file(self):
        self.lock_dir_is_a_file()
        self.assert_unavailable()

    @unittest.skipIf(os.geteuid() == 0, 'root ignores the dir mode')
    def test_unwritable_lock_dir(self):
        os.chmod(self.dir, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.dir, stat.S_IRWXU)
        self.assert_unavailable()

    @unittest.skipIf(os.geteuid() == 0, 'root ignores the file mode')
    def test_unwritable_lock_file(self):
        Path(hil_lock.lock_path(BOARD)).write_text('')
        os.chmod(hil_lock.lock_path(BOARD), stat.S_IRUSR)
        self.assert_unavailable()

    def test_record_write_failing_in_the_buffered_flush_refuses_and_drops_the_flock(self):
        # RLIMIT_FSIZE 0 fails the record at its flush (EFBIG), and close() re-raises it;
        # in a child, so the limit never reaches this process
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
                try:
                    hil_lock.acquire_board_lock(BOARD, reason='selftest')
                    got = 'returned'
                except BaseException as e:
                    got = f'{type(e).__name__}|{type(e.__cause__).__name__}|{e}'
                with open(hil_lock.lock_path(BOARD), 'r+') as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if still held
                os.write(w, f'{got}|{hil_lock.read_record(BOARD)}'.encode())
            finally:
                os._exit(0)
        os.close(w)
        os.waitpid(pid, 0)
        with os.fdopen(r) as f:
            kind, cause, msg, record = f.read().split('|')
        self.assertEqual((kind, cause), ('LockUnavailable', 'OSError'))
        self.assertTrue(msg.startswith('board lock unavailable: holder record: '), msg)
        self.assertEqual(record, 'None')

    def test_bypass_returns_none(self):
        self.lock_dir_is_a_file()
        os.environ['HIL_NO_BOARD_LOCK'] = '1'
        self.assertIsNone(hil_lock.acquire_board_lock(BOARD))


class Contention(LockDir):
    def test_another_process_holds_it(self):
        self.holder(self.record(reason='hil_test.py'))
        with self.assertRaisesRegex(hil_lock.BoardLocked, r'^board locked: .*hil_test\.py') as cm:
            self.acquire()
        self.assertNotIsInstance(cm.exception, hil_lock.LockUnavailable)

    def test_an_unparsable_record_is_an_unknown_holder(self):
        self.holder(b'{"pid": PID, "rea')
        with self.assertRaisesRegex(RuntimeError, '^board locked: unknown holder$'):
            self.acquire()


class CleanupHolder(LockDir):
    def setUp(self):
        super().setUp()
        self.holder(self.record(**CLEANUP))

    def test_acquire_names_a_recovery_blocker(self):
        with self.assertRaisesRegex(RuntimeError, '^board locked: ') as cm:
            self.acquire()
        self.assertIn(hil_lock.RECOVERY_BLOCKER, str(cm.exception))

    def test_hold_names_a_recovery_blocker(self):
        with quiet() as (_, err):
            rc = hil_lock.cmd_hold([BOARD], 'selftest hold')
        self.assertEqual(rc, 1)
        self.assertIn(hil_lock.RECOVERY_BLOCKER, err.getvalue())

    def test_status_names_a_recovery_blocker(self):
        with quiet() as (out, _):
            hil_lock.cmd_status()
        self.assertIn(hil_lock.RECOVERY_BLOCKER, out.getvalue())


class Release(LockDir):
    def setUp(self):
        super().setUp()
        self.patch(hil_lock.time, 'sleep', lambda s: None)

    def test_never_signals_a_protected_or_cleanup_holder(self):
        table = [(CLEANUP, hil_lock.RECOVERY_BLOCKER)]
        table += [({'reason': r}, 'wait for it to finish') for r in sorted(hil_lock.PROTECTED_REASONS)]
        real_kill = os.kill
        for fields, says in table:
            with self.subTest(reason=fields['reason']):
                proc = self.holder(self.record(**fields))
                before = self.lock_bytes()
                kill = mock.Mock(wraps=real_kill)
                with quiet() as (_, err), mock.patch.object(hil_lock.os, 'kill', kill):
                    rc = hil_lock.cmd_release([BOARD])
                self.assertEqual(rc, 1)
                self.assertIn(says, err.getvalue())
                self.assertEqual([c for c in kill.call_args_list if c.args[1] != 0], [])
                self.assertEqual(self.lock_bytes(), before)
                proc.stdin.close()
                proc.wait()

    def test_stale_record_with_a_dead_pid_is_cleared_never_unlinked(self):
        dead = subprocess.Popen([sys.executable, '-c', ''])
        dead.wait()
        with open(hil_lock.lock_path(BOARD), 'w') as f:
            json.dump({'pid': dead.pid, 'reason': 'gone'}, f)
        ino = os.stat(hil_lock.lock_path(BOARD)).st_ino
        with quiet():
            rc = hil_lock.cmd_release([BOARD])
        self.assertEqual(rc, 0)
        self.assertEqual(os.stat(hil_lock.lock_path(BOARD)).st_ino, ino, 'release must never unlink')
        self.assertIsNone(hil_lock.read_record(BOARD))


class DetachedHolder(LockDir):
    """cmd_hold's holder outlives the call in its own session and bows out on SIGTERM,
    clearing its records first."""

    def test_hold_survives_the_call_and_sigterm_clears_its_records(self):
        boards = [BOARD, BOARD + '-2']
        with quiet() as (out, _):
            self.assertEqual(hil_lock.cmd_hold(boards, 'selftest hold'), 0)
        info = hil_lock.read_record(BOARD)
        pid = info['pid']
        self.addCleanup(self.kill_quietly, pid)
        self.assertIn('held: ', out.getvalue())
        self.assertNotEqual(pid, os.getpid())
        self.assertNotEqual(os.getsid(pid), os.getsid(0))
        for b in boards:
            rec = hil_lock.read_record(b)
            self.assertEqual((rec['pid'], rec['reason']), (pid, 'selftest hold'))
            self.assertTrue(hil_lock.is_locked(b))

        # cmd_release's settle wait, made to wait for the holder's records to clear
        self.patch(hil_lock.time, 'sleep', lambda s: self.wait_cleared(boards))
        with quiet():
            self.assertEqual(hil_lock.cmd_release([BOARD]), 0)
        for b in boards:
            self.assertIsNone(hil_lock.read_record(b))
        # the records clear before the holder exits; the kernel drops the flock at its exit
        deadline = time.monotonic() + 10
        while not self.flock_is_free() and time.monotonic() < deadline:
            _sleep(0.02)
        self.assertTrue(self.flock_is_free())

    def test_a_repeated_name_holds_once(self):
        with quiet() as (out, _):
            self.assertEqual(hil_lock.cmd_hold([BOARD, BOARD], 'selftest hold'), 0)
        self.addCleanup(self.kill_quietly, hil_lock.read_record(BOARD)['pid'])
        self.assertEqual(out.getvalue().strip(), f'held: {BOARD}')
        self.assertTrue(hil_lock.is_locked(BOARD))

    def test_a_lost_race_leaves_no_owned_record_and_the_rivals_intact(self):
        rival = self.holder(self.record(reason='rival'))
        other = BOARD + '-2'
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_hold([other, BOARD], 'selftest hold'), 1)
        self.assertIn(f'{BOARD} locked: ', err.getvalue())
        self.assertIn('"rival"', err.getvalue())
        self.wait_cleared([other])
        self.assertIsNone(hil_lock.read_record(other))     # our own record, cleared by the losing holder
        self.assertEqual(hil_lock.read_record(BOARD)['pid'], rival.pid)

    def test_a_record_write_that_fails_in_the_holder_leaves_no_owned_record(self):
        other = BOARD + '-2'
        real, calls = hil_lock.write_record, []

        def write_record(fh, reason):
            calls.append(fh)
            if len(calls) == 2:                 # the first record is written, the second fails
                raise OSError('disk full')
            real(fh, reason)
        self.patch(hil_lock, 'write_record', write_record)     # inherited by the forked holder
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_hold([BOARD, other], 'selftest hold'), 1)
        self.assertIn('holder failed to acquire locks', err.getvalue())
        self.wait_cleared([BOARD, other])
        self.assertIsNone(hil_lock.read_record(BOARD))
        self.assertIsNone(hil_lock.read_record(other))
        deadline = time.monotonic() + 10
        while not self.flock_is_free() and time.monotonic() < deadline:
            _sleep(0.02)
        self.assertTrue(self.flock_is_free())
        with open(hil_lock.lock_path(other), 'r+') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)       # free too

    def wait_cleared(self, boards):
        deadline = time.monotonic() + 10
        while any(hil_lock.read_record(b) for b in boards) and time.monotonic() < deadline:
            _sleep(0.02)

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class Cli(unittest.TestCase):
    def test_hold_takes_named_boards_only(self):
        r = subprocess.run([sys.executable, str(SCRIPT), 'hold', '--all', '--reason', 'x'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)


if __name__ == '__main__':
    unittest.main()
