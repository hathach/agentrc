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
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
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

    def test_record_write_failing_refuses_and_drops_the_flock(self):
        # RLIMIT_FSIZE 0 fails the record write (EFBIG); in a child, so the limit never
        # reaches this process
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

    def test_release_of_one_board_waits_for_all_of_the_holders_records(self):
        boards = [BOARD, BOARD + '-2']
        real_clear, calls = hil_lock.clear_record, []

        def slow_second_clear(fh):        # runs in the forked holder
            calls.append(fh)
            if len(calls) == 2:
                _sleep(0.5)
            real_clear(fh)

        self.patch(hil_lock, 'clear_record', slow_second_clear)
        with quiet():
            self.assertEqual(hil_lock.cmd_hold(boards, 'selftest hold'), 0)
        self.addCleanup(self.kill_quietly, hil_lock.read_record(BOARD)['pid'])
        with quiet():
            self.assertEqual(hil_lock.cmd_release([BOARD]), 0)
        for b in boards:
            self.assertIsNone(hil_lock.read_record(b))

    def test_release_waits_for_the_holder_to_drop_its_flock_after_clearing_its_record(self):
        real_clear = hil_lock.clear_record

        def clear_then_linger(fh):        # runs in the forked holder
            real_clear(fh)
            _sleep(0.5)

        self.patch(hil_lock, 'clear_record', clear_then_linger)
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD], 'selftest hold'), 0)
        self.addCleanup(self.kill_quietly, hil_lock.read_record(BOARD)['pid'])
        with quiet():
            self.assertEqual(hil_lock.cmd_release([BOARD]), 0)
        self.assertTrue(self.flock_is_free())

    def test_a_board_recorded_after_the_release_scanned_is_cleared_before_the_named_one(self):
        other = BOARD + '-2'
        real_clear = hil_lock.clear_record

        def slow_other_clear(fh):         # runs in the forked holder
            if os.readlink(f'/proc/self/fd/{fh.fileno()}').endswith(f'{other}.lock'):
                _sleep(0.5)
            real_clear(fh)

        self.patch(hil_lock, 'clear_record', slow_other_clear)
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD, other], 'selftest hold'), 0)
        self.addCleanup(self.kill_quietly, hil_lock.read_record(BOARD)['pid'])
        self.patch(hil_lock, '_boards_of', lambda pid, start: [])     # as if scanned before `other` was recorded
        with quiet():
            self.assertEqual(hil_lock.cmd_release([BOARD]), 0)
        self.assertIsNone(hil_lock.read_record(other))

    def test_a_board_only_the_scan_added_is_cleared_once_another_holder_takes_it(self):
        other = BOARD + '-2'
        self.patch(hil_lock, 'RELEASE_WAIT', 0.2)
        real = hil_lock.signal.pidfd_send_signal
        # a replacement on a board the caller named is still reported, as before the scan existed
        for named, rc in (([BOARD], 0), ([BOARD, other], 1)):
            with self.subTest(named=named), ExitStack() as stack:
                with quiet():
                    self.assertEqual(hil_lock.cmd_hold([BOARD, other], 'selftest hold'), 0)
                stack.callback(self.kill_quietly, hil_lock.read_record(BOARD)['pid'])

                def signal_then_replace(fd, sig):
                    real(fd, sig)
                    stack.callback(hil_lock.release_board_lock, self.take_when_free(other))

                with quiet() as (_, err), mock.patch.object(hil_lock.signal, 'pidfd_send_signal', signal_then_replace):
                    self.assertEqual(hil_lock.cmd_release(named), rc, err.getvalue())
                self.assertEqual(hil_lock.read_record(other)['reason'], 'replacement')
                self.assertEqual('not released' in err.getvalue(), bool(rc))

    def test_a_sigterm_while_a_board_is_taken_still_clears_its_record(self):
        other = BOARD + '-2'
        real = hil_lock.take_board_lock

        def take_then_sigterm(b, *a, **k):    # runs in the forked holder
            fh = real(b, *a, **k)
            if b == other:
                os.kill(os.getpid(), signal.SIGTERM)    # between the record write and its handle append
            return fh

        self.patch(hil_lock, 'take_board_lock', take_then_sigterm)
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD, other], 'selftest hold'), 1)
        self.wait_cleared([BOARD, other])
        self.assertIsNone(hil_lock.read_record(BOARD))
        self.assertIsNone(hil_lock.read_record(other))

    def test_a_record_its_dead_holder_left_fails_the_release(self):
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD], 'selftest hold'), 0)
        pid = hil_lock.read_record(BOARD)['pid']
        self.addCleanup(self.kill_quietly, pid)
        real = hil_lock.signal.pidfd_send_signal
        self.patch(hil_lock.signal, 'pidfd_send_signal', lambda fd, sig: real(fd, signal.SIGKILL))
        self.patch(hil_lock, 'RELEASE_WAIT', 0.2)
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
        self.assertIn(f'not released (a live holder or a leftover record): {BOARD}', err.getvalue())

    def test_a_record_left_on_a_board_the_scan_missed_fails_the_release(self):
        other = BOARD + '-2'
        real_clear = hil_lock.clear_record

        def failing_other_clear(fh):      # runs in the forked holder
            if not os.readlink(f'/proc/self/fd/{fh.fileno()}').endswith(f'{other}.lock'):
                real_clear(fh)

        self.patch(hil_lock, 'clear_record', failing_other_clear)
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD, other], 'selftest hold'), 0)
        self.addCleanup(self.kill_quietly, hil_lock.read_record(BOARD)['pid'])
        real_scan, scans = hil_lock._boards_of, []

        def first_scan_before_other(pid, start):
            scans.append(pid)
            return real_scan(pid, start) if len(scans) > 1 else []

        self.patch(hil_lock, '_boards_of', first_scan_before_other)
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
        self.assertIn(f'leftover record): {other}', err.getvalue())

    def take_when_free(self, b):
        deadline = time.monotonic() + 10
        while True:
            try:
                return hil_lock.take_board_lock(b, 'replacement')
            except hil_lock.BoardLocked:
                if time.monotonic() >= deadline:
                    raise
                _sleep(0.02)

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

        def write_record(fh, reason, **extra):
            calls.append(fh)
            if len(calls) == 2:                 # the first record is written, the second fails
                raise OSError('disk full')
            real(fh, reason, **extra)
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

    def lose_readiness(self, waits_in):
        """cmd_hold whose parent stops waiting (its select times out, the read end closes)
        before the holder, held back in `waits_in` until then, reports readiness. Returns a
        wait for the holder's exit, True once it is gone."""
        closed, holder = Path(self.dir, 'parent-closed'), Path(self.dir, 'holder-pid')
        real = getattr(hil_lock, waits_in)

        def then_wait(*a, **k):                 # runs in the forked holder
            holder.write_text(str(os.getpid()))
            got = real(*a, **k)
            deadline = time.monotonic() + 10
            while not closed.exists() and time.monotonic() < deadline:
                _sleep(0.01)
            return got

        def timed_out(rlist, *_):
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, rlist[0])              # closes the pipe's only read end
            os.close(null)
            closed.touch()
            return [], [], []
        self.patch(hil_lock, waits_in, then_wait)
        self.patch(hil_lock.select, 'select', timed_out)

        def gone():
            deadline = time.monotonic() + 10
            text = ''
            while not text and time.monotonic() < deadline:     # rewritten, so briefly empty, per board
                text = holder.read_text() if holder.exists() else ''
                _sleep(0.01)
            pid = int(text)
            self.addCleanup(self.kill_quietly, pid, signal.SIGKILL)     # a takeover holder ignores SIGTERM
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                _sleep(0.02)
            return False
        return gone

    def test_a_holder_whose_readiness_is_lost_leaves_no_owned_record(self):
        other = BOARD + '-2'
        gone = self.lose_readiness('take_board_lock')
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_hold([BOARD, other], 'selftest hold'), 1)
        self.assertIn('holder failed to acquire locks', err.getvalue())
        self.assertTrue(gone())
        self.assertIsNone(hil_lock.read_record(BOARD))
        self.assertIsNone(hil_lock.read_record(other))
        self.assertTrue(self.flock_is_free())

    def test_a_readiness_that_lands_as_the_wait_times_out_is_not_kept(self):
        """The holder's readiness is in the pipe when the parent's select gives up: the parent
        reports a failure, so neither hold nor takeover may stay."""
        real = hil_lock.select.select

        def ready_then_timed_out(rlist, *_):
            real(rlist, [], [], 10)
            return [], [], []
        self.patch(hil_lock.select, 'select', ready_then_timed_out)
        for take_over_board in (None, BOARD):
            with self.subTest(take_over=take_over_board):
                if take_over_board:
                    Takeover.blocked(self)
                with quiet() as (_, err):
                    self.assertEqual(hil_lock.cmd_hold([] if take_over_board else [BOARD], 'selftest hold',
                                                       take_over_board), 1)
                if rec := hil_lock.read_record(BOARD):
                    self.addCleanup(self.kill_quietly, rec['pid'], signal.SIGKILL)
                self.assertIn('holder failed to acquire locks', err.getvalue())
                deadline = time.monotonic() + 10
                while not self.flock_is_free() and time.monotonic() < deadline:
                    _sleep(0.02)
                self.assertTrue(self.flock_is_free())
                if take_over_board:          # an unannounced takeover stays a recovery blocker
                    self.assertEqual(hil_lock.read_record(BOARD)['role'], 'cleanup-holder')
                else:
                    self.assertIsNone(hil_lock.read_record(BOARD))

    def test_a_takeover_whose_readiness_is_lost_stays_a_recovery_blocker(self):
        Takeover.blocked(self)
        gone = self.lose_readiness('take_over')
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([], 'selftest recovery', take_over_board=BOARD), 1)
        self.assertTrue(gone())
        self.assertTrue(self.flock_is_free())
        self.assertEqual(hil_lock.read_record(BOARD)['role'], 'cleanup-holder')
        self.assertIsNotNone(hil_lock.journal_state(BOARD))
        with self.assertRaises(hil_lock.RecoveryBlocked):
            self.acquire()

    def wait_cleared(self, boards):
        deadline = time.monotonic() + 10
        while any(hil_lock.read_record(b) for b in boards) and time.monotonic() < deadline:
            _sleep(0.02)

    @staticmethod
    def kill_quietly(pid, sig=signal.SIGTERM):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


class SafeFiles(LockDir):
    """The shared dir is writable by others: nothing in it is opened through a link, a FIFO or
    a second name, and nothing is repaired; the target of a planted link stays untouched."""

    def plant(self, kind):
        target = Path(self.dir, 'victim')
        target.write_text('precious\n')
        path = hil_lock.lock_path(BOARD)
        if kind == 'symlink':
            os.symlink(target, path)
        elif kind == 'hardlink':
            os.link(target, path)
        elif kind == 'fifo':
            os.mkfifo(path)
        else:
            os.mkdir(path)
        return target

    def test_take_hold_and_release_refuse_an_unsafe_lock_file(self):
        for kind in ('symlink', 'hardlink', 'fifo', 'dir'):
            with self.subTest(kind=kind):
                target = self.plant(kind)
                with self.assertRaisesRegex(hil_lock.LockUnavailable, 'board lock unavailable'):
                    self.acquire()
                with quiet() as (_, err):
                    self.assertEqual(hil_lock.cmd_hold([BOARD], 'selftest hold'), 1)
                    self.assertEqual(hil_lock.cmd_release([BOARD, BOARD + '-2']), 1)
                self.assertEqual(target.read_text(), 'precious\n')
                self.assertEqual(hil_lock.record_state(BOARD)[0], 'unreadable')
                path = hil_lock.lock_path(BOARD)
                os.rmdir(path) if kind == 'dir' else os.unlink(path)
                target.unlink()

    def test_a_symlinked_lock_dir_or_an_invalid_name_is_refused(self):
        real = Path(self.dir, 'real')
        real.mkdir()
        link = Path(self.dir, 'link')
        link.symlink_to(real)
        self.patch(hil_lock, 'BOARD_LOCK_DIR', str(link))
        with self.assertRaises(hil_lock.LockUnavailable):
            self.acquire()
        self.assertEqual(os.listdir(real), [])
        self.patch(hil_lock, 'BOARD_LOCK_DIR', str(real))
        for bad in ('../escape', 'a/b', '', '.hidden'):
            with self.subTest(name=bad), self.assertRaisesRegex(hil_lock.LockUnavailable, 'invalid board lock name'):
                hil_lock.take_board_lock(bad, 'selftest')

    @unittest.skipIf(os.geteuid() == 0, 'a dir root owns passes as root-owned')
    def test_a_dir_another_user_owns_needs_the_sticky_bit(self):
        self.patch(hil_lock.os, 'geteuid', lambda: os.getuid() + 1)    # the dir is now "someone else's"
        os.chmod(self.dir, 0o777)
        with self.assertRaisesRegex(hil_lock.LockUnavailable, 'not sticky'):
            self.acquire()
        os.chmod(self.dir, 0o1777)
        self.assertIsNotNone(self.acquire())

    def test_modes_ignore_the_umask_and_leave_widening_to_the_host(self):
        sub = os.path.join(self.dir, 'locks')
        self.patch(hil_lock, 'BOARD_LOCK_DIR', sub)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        self.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(sub).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(hil_lock.lock_path(BOARD)).st_mode), 0o666)
        _, handles = hil_lock.take_over(BOARD + '-2', 'selftest recovery')
        self.addCleanup(hil_lock.end_takeover, handles)
        journal = os.path.join(sub, BOARD + '-2' + hil_lock.JOURNAL)
        self.assertEqual(stat.S_IMODE(os.stat(journal).st_mode), 0o644)   # another OS user's takeover reads it

    def test_a_create_refused_by_the_dir_names_the_host_step(self):
        os.chmod(self.dir, 0o555)
        self.addCleanup(os.chmod, self.dir, 0o755)
        if os.access(self.dir, os.W_OK):
            self.skipTest('running as root: the dir is writable anyway')
        with self.assertRaisesRegex(hil_lock.LockUnavailable, 'install -d -m 1777'):
            self.acquire()


class Records(LockDir):
    def test_absent_empty_unreadable_and_non_object_records_are_told_apart(self):
        self.assertEqual(hil_lock.record_state(BOARD), ('absent', None))
        path = Path(hil_lock.lock_path(BOARD))
        for data, state in (('', 'empty'), ('{bad', 'unreadable'), ('["x"]', 'unreadable'),
                            ('7', 'unreadable'), ('{"pid": 1}', 'record')):
            path.write_text(data)
            self.assertEqual(hil_lock.record_state(BOARD)[0], state, data)

    def test_release_reports_a_non_object_record_and_goes_on(self):
        later = BOARD + '-2'
        Path(hil_lock.lock_path(later)).write_text(json.dumps({'pid': 999999999, 'reason': 'gone'}))
        self.holder(b'["x"]')
        with quiet() as (_, err):
            rc = hil_lock.cmd_release([BOARD, later])
        self.assertEqual(rc, 1)
        self.assertIn('record is unreadable', err.getvalue())
        self.assertIsNone(hil_lock.read_record(later), 'the board after the bad record was not processed')


class SignalsOnlyProvenHolds(LockDir):
    """release SIGTERMs only this CLI's own hold holder, proven by its start time and by an
    open file description of its that holds the flock; everything else is named, never signalled."""

    def setUp(self):
        super().setUp()
        self.signals = []
        real = signal.pidfd_send_signal
        self.patch(hil_lock.signal, 'pidfd_send_signal', lambda fd, sig: (self.signals.append(sig), real(fd, sig)))

    def bystander(self):
        """A live process that has the lock file open without holding its flock."""
        p = subprocess.Popen([sys.executable, '-c', 'import sys; f = open(sys.argv[1]); print("ready", flush=True); sys.stdin.read()',
                              hil_lock.lock_path(BOARD)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.stdin.close)
        self.addCleanup(p.stdout.close)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        return p

    def test_a_record_naming_a_bystander_is_never_signalled(self):
        Path(hil_lock.lock_path(BOARD)).touch()
        by = self.bystander()
        rec = {'pid': by.pid, 'reason': 'selftest', 'holder': hil_lock.HOLD_KIND, 'start': hil_lock.proc_start(by.pid)}
        self.holder(json.dumps(rec).encode())
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
        self.assertIn('does not hold the flock', err.getvalue())
        self.assertEqual(self.signals, [])
        self.assertIsNone(by.poll())

    def test_a_reused_pid_or_a_foreign_taker_is_never_signalled(self):
        for fields, says in (({'holder': hil_lock.HOLD_KIND, 'start': '1'}, 'not the process that wrote'),
                             ({}, 'not a hil_lock.py hold')):
            with self.subTest(says=says):
                p = self.holder(self.record(reason='selftest', **fields))
                with quiet() as (_, err):
                    self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
                self.assertIn(says, err.getvalue())
                self.assertEqual(self.signals, [])
                p.stdin.close()
                p.wait()

    def test_a_malformed_mark_never_cancels_legacy_protection(self):
        p = self.holder(self.record(reason='hil_test.py', protected='no', holder=hil_lock.HOLD_KIND))
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
        self.assertIn('wait for it to finish', err.getvalue())
        self.assertIsNone(p.poll())

    def test_a_proven_hold_is_signalled_through_a_pidfd_and_waited_for(self):
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD], 'selftest hold'), 0)
        pid = hil_lock.read_record(BOARD)['pid']
        self.addCleanup(DetachedHolder.kill_quietly, pid)
        with quiet() as (out, _):
            self.assertEqual(hil_lock.cmd_release([BOARD]), 0)
        self.assertEqual(self.signals, [signal.SIGTERM])
        self.assertIsNone(hil_lock.read_record(BOARD))


class RecoveryBlockers(LockDir):
    """A record that needs recovery survives a free flock: no project takes the board and an
    ordinary release keeps it, until a recovery takeover ends it."""

    def dead_record(self, **fields):
        dead = subprocess.Popen([sys.executable, '-c', ''])
        dead.wait()
        Path(hil_lock.lock_path(BOARD)).write_text(json.dumps({'pid': dead.pid, 'reason': 'selftest', **fields}))

    def test_a_dead_protected_run_or_cleanup_holder_blocks_every_taker(self):
        for fields in ({'protected': True}, {'role': 'cleanup-holder'}):
            with self.subTest(fields=fields):
                self.dead_record(**fields)
                with self.assertRaisesRegex(hil_lock.RecoveryBlocked, hil_lock.RECOVERY_BLOCKER):
                    self.acquire()
                with quiet() as (_, err):
                    self.assertEqual(hil_lock.cmd_hold([BOARD], 'selftest hold'), 1)
                    self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
                self.assertIn('kept', err.getvalue())
                self.assertEqual(hil_lock.read_record(BOARD).get('reason'), 'selftest')
                with quiet() as (out, _):
                    hil_lock.cmd_status()
                self.assertIn('stale', out.getvalue())

    def test_an_unmarked_legacy_runner_record_is_taken_as_before(self):
        """tinyusb's runner admits after a killed run; a tinyuf2 record left by a helper older
        than the mark looks the same and is taken too (documented in the hil skill)."""
        self.dead_record(reason='hil_test.py')
        self.assertIsNotNone(self.acquire())

    def test_a_rewrite_of_a_protected_record_cut_short_anywhere_still_blocks(self):
        """A protected reservation turning cleanup holder (hil_reserve drops `protected` and
        adds `role`) dies mid-rewrite: cut at every byte (RLIMIT_FSIZE) and before every write
        call, the board stays blocked."""
        locks = [BOARD, 'x' * 40]
        fh = hil_lock.take_board_lock(BOARD, 'selftest', protected=True, tool='t', locks=locks)
        old = self.lock_bytes()
        fh.close()

        def rewrite(cut=None, kill_before=None):
            Path(hil_lock.lock_path(BOARD)).write_bytes(old)
            pid = os.fork()
            if pid == 0:
                try:
                    fh = hil_lock.flock_nb(BOARD)
                    calls = []

                    def counted(real):
                        def call(*a):
                            calls.append(1)
                            if len(calls) == kill_before:
                                os._exit(9)
                            return real(*a)
                        return call
                    for name in ('write', 'pwrite', 'ftruncate'):
                        setattr(os, name, counted(getattr(os, name)))
                    if cut is not None:
                        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                        resource.setrlimit(resource.RLIMIT_FSIZE, (cut, cut))
                    hil_lock.write_record(fh, 'selftest', tool='t', locks=locks, role='cleanup-holder', detail='d')
                    os._exit(0)
                finally:
                    os._exit(1)
            status = os.waitpid(pid, 0)[1] >> 8
            with hil_lock.flock_nb(BOARD) as fh:
                return status, hil_lock._blocker(BOARD, fh)

        open_cuts = [cut for cut in range(len(old) + 40) if not rewrite(cut=cut)[1]]
        self.assertEqual(open_cuts, [])
        kill_before = 1
        while True:
            status, why = rewrite(kill_before=kill_before)
            self.assertTrue(why, f'killed before write call {kill_before}')
            if status == 0:               # the rewrite completed: a cleanup-holder record
                break
            kill_before += 1

    def test_an_ordinary_taker_killed_after_the_sentinel_blocks_until_a_takeover(self):
        """Pins today's fail-closed outcome (agentrc#154): the reader cannot tell this from an
        interrupted protected rewrite, so the free board needs a recovery takeover."""
        pid = os.fork()
        if pid == 0:
            try:
                fh = hil_lock.flock_nb(BOARD)
                real, calls = os.pwrite, []

                def pwrite(*a):
                    calls.append(1)
                    if len(calls) == 2:
                        os._exit(9)
                    return real(*a)
                os.pwrite = pwrite
                hil_lock.write_record(fh, 'selftest')
                os._exit(0)
            finally:
                os._exit(1)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 9)
        self.assertEqual(self.lock_bytes()[:1], b'#')
        with self.assertRaisesRegex(hil_lock.RecoveryBlocked, hil_lock.RECOVERY_BLOCKER):
            self.acquire()
        with quiet() as (out, _):
            self.assertEqual(hil_lock.cmd_hold([], 'selftest recovery', take_over_board=BOARD), 0)
        token = out.getvalue().split('token ')[1].split(')')[0]
        self.addCleanup(DetachedHolder.kill_quietly, hil_lock.read_record(BOARD)['pid'])
        self.assertEqual(hil_lock.cmd_release_token(token), 0)
        self.assertIsNotNone(self.acquire())

    def test_a_protected_taker_ends_with_release_board_lock(self):
        fh = hil_lock.take_board_lock(BOARD, 'selftest', protected=True)
        self.assertIs(hil_lock.read_record(BOARD)['protected'], True)
        hil_lock.release_board_lock(fh)
        self.assertIsNotNone(self.acquire())


class Takeover(LockDir):
    """A recovery takeover journals each board's predecessor before replacing its record, takes
    the predecessor's whole lock set or nothing, and ends only by token release of a live taker."""

    OTHER = BOARD + '-2'

    def blocked(self, locks=None):
        for b in locks or [BOARD]:
            Path(hil_lock.lock_path(b)).write_text(json.dumps(
                {'pid': 999999999, 'reason': 'selftest run', 'protected': True, 'locks': locks or [BOARD]}))

    def journal(self, b):
        return hil_lock.journal_state(b)

    def test_takes_the_whole_set_journals_and_ends_by_token(self):
        self.blocked([BOARD, self.OTHER])
        with quiet() as (out, _):
            self.assertEqual(hil_lock.cmd_hold([], 'selftest recovery', take_over_board=BOARD), 0)
        token = out.getvalue().split('token ')[1].split(')')[0]
        rec = hil_lock.read_record(self.OTHER)
        self.addCleanup(DetachedHolder.kill_quietly, rec['pid'])
        self.assertEqual((rec['role'], rec['token'], rec['locks']), ('cleanup-holder', token, [BOARD, self.OTHER]))
        self.assertIn('selftest run', self.journal(self.OTHER)['predecessor'])
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_release([BOARD]), 1)     # a cleanup holder: never signalled
            self.assertEqual(hil_lock.cmd_hold([BOARD], 'x'), 1)
        self.assertEqual(hil_lock.cmd_release_token(token), 0)
        for b in (BOARD, self.OTHER):
            self.assertIsNone(hil_lock.read_record(b))
            self.assertIsNone(self.journal(b))
        self.assertIsNotNone(self.acquire())

    def test_a_held_member_of_the_set_modifies_nothing(self):
        self.blocked([BOARD, self.OTHER])
        before = Path(hil_lock.lock_path(BOARD)).read_bytes()
        with open(hil_lock.lock_path(self.OTHER), 'r+') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(hil_lock.BoardLocked):
                hil_lock.take_over(BOARD, 'selftest recovery')
        self.assertEqual(hil_lock.journal_names(), [])
        self.assertEqual(Path(hil_lock.lock_path(BOARD)).read_bytes(), before)

    def test_a_failed_journal_or_record_write_leaves_every_board_blocked(self):
        for name in ('_write_journal', 'write_record'):
            with self.subTest(fails=name):
                self.blocked([BOARD, self.OTHER])
                real, calls = getattr(hil_lock, name), []

                def fail_second(*a, real=real, calls=calls, **k):
                    calls.append(1)
                    if len(calls) == 2:
                        raise OSError('disk full')
                    return real(*a, **k)
                with mock.patch.object(hil_lock, name, fail_second):
                    with self.assertRaisesRegex(hil_lock.LockUnavailable, 'takeover .* failed'):
                        hil_lock.take_over(BOARD, 'selftest recovery')
                with self.assertRaises(hil_lock.RecoveryBlocked):
                    self.acquire()
                for b in (BOARD, self.OTHER):
                    os.unlink(hil_lock.lock_path(b))
                for b in hil_lock.journal_names():
                    os.unlink(os.path.join(self.dir, b + hil_lock.JOURNAL))

    def test_a_killed_taker_stays_blocked_until_a_second_takeover_recovers(self):
        self.blocked()
        pid = os.fork()
        if pid == 0:                         # dies between its journal and a complete record
            def die(fh, *a, **k):
                fh.truncate(0)
                fh.write('{"pid": ')
                fh.flush()
                os._exit(9)
            hil_lock.write_record = die
            try:
                hil_lock.take_over(BOARD, 'selftest recovery')
            finally:
                os._exit(1)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 9)
        first = self.journal(BOARD)
        self.assertEqual(first['pid'], pid)
        self.assertEqual(hil_lock.record_state(BOARD)[0], 'unreadable')
        with self.assertRaises(hil_lock.RecoveryBlocked):
            self.acquire()
        with quiet():
            self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
            self.assertEqual(hil_lock.cmd_release_token(first['token']), 1)
        self.assertIsNotNone(self.journal(BOARD))
        token, handles = hil_lock.take_over(BOARD, 'selftest recovery again')
        self.assertEqual(self.journal(BOARD)['prior']['token'], first['token'])
        hil_lock.end_takeover(handles)
        self.assertEqual(hil_lock.journal_names(), [])
        self.assertIsNotNone(self.acquire())

    def test_another_users_journal_is_refused_naming_its_owner_before_anything_changes(self):
        self.blocked([BOARD, self.OTHER])
        _, handles = hil_lock.take_over(BOARD, 'selftest recovery')
        hil_lock._drop_flocks(handles)               # that taker died
        files = [hil_lock.lock_path(b) for b in (BOARD, self.OTHER)] + \
                [os.path.join(self.dir, b + hil_lock.JOURNAL) for b in (BOARD, self.OTHER)]
        before = [Path(f).read_bytes() for f in files]
        os.chmod(self.dir, 0o1777)
        self.patch(hil_lock.os, 'geteuid', lambda: os.getuid() + 1)     # its journals are another user's

        def sticky(*a, **k):
            raise PermissionError(1, 'Operation not permitted')
        self.patch(hil_lock.os, 'rename', sticky)
        with self.assertRaisesRegex(hil_lock.LockUnavailable,
                                    rf'\(uid {os.getuid()}\).*owner or root.*nothing modified'):
            hil_lock.take_over(BOARD, 'selftest recovery again')
        self.assertEqual([Path(f).read_bytes() for f in files], before)
        with self.assertRaises(hil_lock.RecoveryBlocked):                # every flock dropped
            self.acquire()

    def test_token_release_of_a_dead_taker_is_refused(self):
        self.blocked()
        token, handles = hil_lock.take_over(BOARD, 'selftest recovery')
        for fh in handles.values():
            fh.close()                       # the taker is gone; its records name this test's pid
        rec = hil_lock.read_record(BOARD)
        rec['pid'] = 999999999
        Path(hil_lock.lock_path(BOARD)).write_text(json.dumps(rec))
        with self.assertRaisesRegex(hil_lock.ReleaseRefused, 'is gone; its boards need a recovery takeover'):
            hil_lock.release_token(token)


class TakeoverEdges(LockDir):
    OTHER = Takeover.OTHER
    blocked = Takeover.blocked

    def test_a_lock_set_that_grows_while_it_is_taken_modifies_nothing(self):
        self.blocked()
        real = hil_lock.flock_nb

        def grow(b):
            fh = real(b)
            rec = json.loads(Path(hil_lock.lock_path(BOARD)).read_text())
            rec['locks'] = [BOARD, self.OTHER]
            Path(hil_lock.lock_path(BOARD)).write_text(json.dumps(rec))
            return fh
        self.patch(hil_lock, 'flock_nb', grow)
        with self.assertRaisesRegex(hil_lock.LockUnavailable, 'lock set changed'):
            hil_lock.take_over(BOARD, 'selftest recovery')
        self.assertEqual(hil_lock.journal_names(), [])

    def test_an_unreadable_journal_refuses_the_takeover(self):
        self.blocked()
        Path(self.dir, BOARD + hil_lock.JOURNAL).write_text('{"locks": [')
        with self.assertRaisesRegex(hil_lock.LockUnavailable, 'cannot be read'):
            hil_lock.take_over(BOARD, 'selftest recovery')
        with self.assertRaises(hil_lock.RecoveryBlocked):
            self.acquire()

    def test_token_release_proves_the_holder_holds_every_flock(self):
        Path(hil_lock.lock_path(BOARD)).touch()
        by = SignalsOnlyProvenHolds.bystander(self)
        rec = {'pid': by.pid, 'start': hil_lock.proc_start(by.pid), 'token': 'tok', 'locks': [BOARD],
               'role': 'cleanup-holder', 'reason': 'selftest'}
        self.holder(json.dumps(rec).encode())
        signals = []
        self.patch(hil_lock.signal, 'pidfd_send_signal', lambda fd, sig: signals.append(sig))
        with self.assertRaisesRegex(hil_lock.ReleaseRefused, 'does not hold the flocks'):
            hil_lock.release_token('tok')
        self.assertEqual(signals, [])


class UnreadableRecords(LockDir):
    def test_a_free_board_with_an_unreadable_record_is_kept_and_the_next_one_processed(self):
        later = BOARD + '-2'
        Path(hil_lock.lock_path(BOARD)).write_bytes(b'\xff\xfe{"pid"')
        Path(hil_lock.lock_path(later)).write_text(json.dumps({'pid': 999999999, 'reason': 'gone'}))
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_release([BOARD, later]), 1)
        self.assertIn('unreadable record', err.getvalue())
        self.assertEqual(Path(hil_lock.lock_path(BOARD)).read_bytes(), b'\xff\xfe{"pid"')
        self.assertIsNone(hil_lock.read_record(later))
        with self.assertRaises(hil_lock.RecoveryBlocked):
            self.acquire()
        with quiet() as (out, _):
            hil_lock.cmd_status()
        self.assertIn(f'{BOARD}: unreadable record', out.getvalue())

    def test_a_holder_that_leaves_an_unreadable_record_fails_the_release(self):
        with quiet():
            self.assertEqual(hil_lock.cmd_hold([BOARD], 'selftest hold'), 0)
        pid = hil_lock.read_record(BOARD)['pid']
        self.addCleanup(DetachedHolder.kill_quietly, pid)

        def garble(fd, sig):                       # stands in for a holder whose cleanup went wrong
            Path(hil_lock.lock_path(BOARD)).write_bytes(b'\xff')
        self.patch(hil_lock.signal, 'pidfd_send_signal', garble)
        self.patch(hil_lock, 'RELEASE_WAIT', 0.1)
        with quiet() as (_, err):
            self.assertEqual(hil_lock.cmd_release([BOARD]), 1)
        self.assertIn('not released', err.getvalue())

    def test_a_record_left_unreadable_is_not_a_completed_release(self):
        self.patch(hil_lock.time, 'sleep', lambda s: None)
        Path(hil_lock.lock_path(BOARD)).write_bytes(b'\xff')
        self.assertEqual(hil_lock._wait_cleared([BOARD], 999999999, None, time.monotonic()), [BOARD])

    def test_a_holder_still_on_its_flock_is_not_a_completed_release(self):
        self.patch(hil_lock.time, 'sleep', lambda s: None)
        fh = hil_lock.flock_nb(BOARD)               # records cleared, flock not yet dropped
        me = (os.getpid(), hil_lock.proc_start(os.getpid()))
        self.assertEqual(hil_lock._wait_cleared([BOARD], *me, time.monotonic()), [BOARD])
        fh.close()
        self.assertEqual(hil_lock._wait_cleared([BOARD], *me, time.monotonic()), [])

    def test_an_exited_holder_not_yet_reaped_has_dropped_its_flock(self):
        self.patch(hil_lock.time, 'sleep', lambda s: None)
        pid = os.fork()
        if pid == 0:
            try:
                fh = hil_lock.flock_nb(BOARD)      # kept open: only the kernel's exit drops it
                os._exit(0 if fh else 1)
            finally:
                os._exit(1)
        self.addCleanup(os.waitpid, pid, 0)
        start = hil_lock.proc_start(pid)
        deadline = time.monotonic() + 5
        while not hil_lock._exited(pid, start) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(hil_lock._wait_cleared([BOARD], pid, start, time.monotonic()), [])

    def test_a_thread_set_that_changes_during_the_scan_is_not_an_exit(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        self.addCleanup(os.waitpid, pid, 0)
        start = hil_lock.proc_start(pid)
        deadline = time.monotonic() + 5
        while not hil_lock._exited(pid, start) and time.monotonic() < deadline:
            time.sleep(0.01)
        real, scans = os.listdir, []

        def listdir(path):                      # a worker appears between the two scans
            got = real(path)
            if path == f'/proc/{pid}/task':
                scans.append(1)
                return got + ['999999999'] if len(scans) > 1 else got
            return got
        self.patch(hil_lock.os, 'listdir', listdir)
        self.assertFalse(hil_lock._exited(pid, start))


class HolderFields(LockDir):
    def test_a_token_holder_without_its_start_or_with_a_bad_set_is_refused(self):
        self.assertEqual(set(hil_lock.holder_fields('t', [BOARD])), {'token', 'start', 'locks'})
        for locks in ([], [BOARD, BOARD], ['a/b']):
            with self.subTest(locks=locks), self.assertRaises((ValueError, OSError)):
                hil_lock.holder_fields('t', locks)
        self.patch(hil_lock, 'proc_start', lambda pid: None)
        with self.assertRaisesRegex(OSError, 'start time unreadable'):
            hil_lock.holder_fields('t', [BOARD])


class Cheap(LockDir):
    def test_release_waits_only_after_a_signal_and_status_reads_each_record_once(self):
        slept = []
        self.patch(hil_lock.time, 'sleep', slept.append)
        Path(hil_lock.lock_path(BOARD)).write_text(json.dumps({'pid': 999999999, 'reason': 'gone'}))
        with quiet():
            self.assertEqual(hil_lock.cmd_release([BOARD]), 0)
        self.assertEqual(slept, [])
        self.holder(self.record(reason='selftest'))
        reads = []
        real = hil_lock._read_file
        self.patch(hil_lock, '_read_file', lambda b, suffix: (reads.append((b, suffix)), real(b, suffix))[1])
        with quiet() as (out, _):
            hil_lock.cmd_status()
        self.assertEqual(reads.count((BOARD, '.lock')), 1)
        self.assertIn('selftest', out.getvalue())


class Cli(unittest.TestCase):
    def test_hold_takes_named_boards_only(self):
        r = subprocess.run([sys.executable, str(SCRIPT), 'hold', '--all', '--reason', 'x'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_a_token_release_takes_no_boards_and_a_takeover_one_board(self):
        for argv in (['release', '--token', 't', 'b'], ['release', '--token', 't', '--all'],
                     ['hold', 'b', '--take-over', 'c', '--reason', 'x'], ['hold', '--reason', 'x']):
            with self.subTest(argv=argv):
                r = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True)
                self.assertEqual(r.returncode, 2, r.stderr)


if __name__ == '__main__':
    unittest.main()
