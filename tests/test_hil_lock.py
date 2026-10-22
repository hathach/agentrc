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
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, pid_alive  # noqa: E402

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


def fork_child(body, timeout=10):
    """The exit code of a forked child running body() (0 once it returns, 1 when it raises), or
    None when it had to be killed past `timeout`."""
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            body()
            code = 0
        finally:
            os._exit(code)
    deadline = time.monotonic() + timeout
    while not (done := os.waitpid(pid, os.WNOHANG))[0]:
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return None
        time.sleep(0.05)
    return os.waitstatus_to_exitcode(done[1])


def make_receipt(test, state: Path, uid=None):
    """A run_tracked receipt in `state`, a new dir of `uid` (default ours), whose supervisor still
    holds its lock: `pending` until done()."""
    state.mkdir(mode=0o700)
    lock, record = state / 'hil_op_t.lock', state / 'hil_op_t.json'
    fh = open(lock, 'w')
    test.addCleanup(fh.close)
    fcntl.flock(fh, fcntl.LOCK_EX)
    st = os.fstat(fh.fileno())
    if uid is not None:
        os.chown(state, uid, uid)
        os.chown(lock, uid, uid)

    def done():
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'token': 't', 'lock': [st.st_dev, st.st_ino], 'cleanup': 'done'}, f)
        if uid is not None:
            os.chown(record, uid, uid)
        fh.close()
    return {'token': 't', 'supervisor': {'pid': 4242, 'start': '1', 'lock': [st.st_dev, st.st_ino]},
            'lock': str(lock), 'record': str(record), 'log': str(state / 'hil_op_t.log'), 'survivors': None}, done


def setUpModule():
    guard_survivors(__name__)


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
        self.assertEqual(hil_lock.PROTECTED_REASONS, {'hil_test.py', 'farm_check'})
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

    def test_clear_record_says_whether_the_record_is_gone(self):
        fh = self.acquire()
        self.assertTrue(hil_lock.clear_record(fh))
        self.assertEqual(hil_lock.record_state(BOARD)[0], 'empty')
        hil_lock.write_record(fh, 'selftest')
        with mock.patch.object(fh, 'truncate', side_effect=OSError(5, 'Input/output error')):
            self.assertFalse(hil_lock.clear_record(fh))
        self.assertEqual(hil_lock.read_record(BOARD)['reason'], 'selftest')

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
        real, calls = hil_lock._write_record, []

        def write_record(fh, *a):
            calls.append(fh)
            if len(calls) == 2:                 # the first record is written, the second fails
                raise OSError('disk full')
            real(fh, *a)
        self.patch(hil_lock, '_write_record', write_record)     # inherited by the forked holder
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
            # Renamed into place: gone() may read it the moment it exists.
            tmp = holder.with_name('holder-pid.tmp')
            tmp.write_text(str(os.getpid()))
            os.replace(tmp, holder)
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
                if not pid_alive(pid):
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

    def test_a_rewrite_killed_after_its_sentinel_blocks_until_a_takeover(self):
        """A rewrite that is not take_board_lock's first record keeps REWRITING: the reader cannot
        tell it from an interrupted protected rewrite, so the free board needs a recovery takeover.
        An ordinary acquisition killed the same way leaves a free board (AcquisitionSentinel)."""
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


# The hil_lock.py of helpers older than ACQUIRING, byte-identical: skills/hil/scripts/hil_lock.py at
# 9e41f6fc89375df73d453c45e4c5a97103830fcd.
PRE_154 = Path(__file__).resolve().parent / 'data' / 'hil_lock_pre154.py'


def load_pre154():
    """The hil_lock.py installed consumers on older agentrc pins run."""
    mod = types.ModuleType('agentrc_hil_lock_pre154')
    exec(compile(PRE_154.read_bytes(), str(PRE_154), 'exec'), mod.__dict__)
    return mod


class AcquisitionSentinel(LockDir):
    """agentrc#154: take_board_lock's first record is written behind ACQUIRING, so a taker killed
    before acquisition returned leaves a free board; every other rewrite keeps REWRITING and
    stays a recovery blocker. Each transition is killed before every write, truncate, fsync,
    rename, unlink and record clear it makes (with whole and with one-byte writes), and the
    board is then read by this helper and by the pre-#154 one: only a record whose byte 0 is
    still ACQUIRING reads differently, free here and refused (fail-closed) by the old one."""

    DEAD = {'pid': 999999999, 'reason': 'selftest dead holder', 'since': '2026-10-21T00:00:00+0700',
            'detail': 'longer than the record that replaces it, so a cut leaves a splice of both'}
    PROTECTED = {'pid': 999999999, 'reason': 'selftest run', 'protected': True, 'locks': [BOARD]}

    @classmethod
    def setUpClass(cls):
        cls.old = load_pre154()

    def setUp(self):
        super().setUp()
        self.patch(self.old, 'BOARD_LOCK_DIR', self.dir)

    def prepare(self, record):
        """The lock file holds `record` (bytes, or a dict as JSON), same inode every time;
        journals and journal temp files are gone."""
        for fn in os.listdir(self.dir):
            if fn != f'{BOARD}.lock':
                os.unlink(os.path.join(self.dir, fn))
        data = record if isinstance(record, bytes) else json.dumps(record).encode()
        with open(hil_lock.lock_path(BOARD), 'ab') as f:   # created once, never replaced
            f.truncate(0)
            f.write(data)
        return os.stat(hil_lock.lock_path(BOARD)).st_ino

    @staticmethod
    def run_child(action, writer, kill_at=None, pwrite=None):
        """Exit status of a child running action(arm): 9 when it died right before its kill_at-th
        counted call after arm(), 0 when it finished, 3 when it raised, else -signal."""
        pid = os.fork()
        if pid == 0:
            try:
                signal.alarm(20)                  # a write loop that never ends fails, not hangs
                armed, calls = [], []

                def counted(real):
                    def call(*a, **k):
                        if armed:
                            calls.append(1)
                            if len(calls) == kill_at:
                                os._exit(9)
                        return real(*a, **k)
                    return call
                if pwrite is not None:
                    os.pwrite = pwrite(os.pwrite)
                for name in ('pwrite', 'write', 'ftruncate', 'fsync', 'rename', 'unlink'):
                    setattr(os, name, counted(getattr(os, name)))
                writer.clear_record = counted(writer.clear_record)
                try:
                    action(writer, lambda: armed.append(1))
                except Exception:
                    os._exit(3)
                os._exit(0)
            finally:
                os._exit(1)
        status = os.waitpid(pid, 0)[1]
        return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)

    def reads(self, mod) -> str:
        fh = mod.flock_nb(BOARD)
        try:
            return 'blocked' if mod._blocker(BOARD, fh) else 'free'
        finally:
            fh.close()

    # --- transitions: action(writer module, arm) -------------------------------------------
    @staticmethod
    def ordinary(m, arm):
        arm()
        m.take_board_lock(BOARD, 'selftest', tool='t')

    @staticmethod
    def protected(m, arm):
        arm()
        m.take_board_lock(BOARD, 'selftest', protected=True, tool='t', locks=[BOARD])

    @staticmethod
    def to_cleanup(m, arm):               # hil_reserve: drops `protected`, adds `role`
        fh = m.flock_nb(BOARD)
        arm()
        m.write_record(fh, 'selftest', tool='t', locks=[BOARD], role='cleanup-holder', detail='d')

    @staticmethod
    def takeover(m, arm):
        arm()
        m.take_over(BOARD, 'selftest recovery')

    @staticmethod
    def end_takeover(m, arm):
        _, handles = m.take_over(BOARD, 'selftest recovery')
        arm()
        m.end_takeover(handles)

    @staticmethod
    def release(protected):
        def action(m, arm):
            fh = m.take_board_lock(BOARD, 'selftest', protected=protected)
            arm()
            m.release_board_lock(fh)
        return action

    @staticmethod
    def one_byte(real):
        return lambda fd, data, offset: real(fd, data[:1], offset)

    # (name, action, starting record, reading at every interruption, reading once it finished)
    ROWS = (
        ('ordinary acquisition over a stale record', 'ordinary', DEAD, 'free', 'free'),
        ('ordinary acquisition over an empty record', 'ordinary', b'', 'free', 'free'),
        ('protected acquisition over a stale record', 'protected', DEAD, 'free', 'blocked'),
        ('protected acquisition over an empty record', 'protected', b'', 'free', 'blocked'),
        ('protected -> cleanup-holder', 'to_cleanup', PROTECTED, 'blocked', 'blocked'),
        ('recovery takeover', 'takeover', PROTECTED, 'blocked', 'blocked'),
        ('end of a recovery takeover', 'end_takeover', PROTECTED, 'blocked', 'free'),
        ('ordinary release', 'release-ordinary', b'', 'free', 'free'),
        ('protected release', 'release-protected', b'', 'blocked', 'free'),
    )
    ACQUISITIONS = ('ordinary', 'protected')

    def action(self, key):
        if key.startswith('release-'):
            return self.release(key == 'release-protected')
        return getattr(self, key)

    def matrix(self, writer, rows=ROWS):
        """[(row, writes, boundary, exit status, byte 0, this helper's reading, the old one's)];
        boundary k is a kill right before the k-th counted call, the last one the run that
        finished. Asserts the inode never changes and every interrupted run died where cut."""
        out = []
        for name, key, start, _, _ in rows:
            for writes, pwrite in (('whole', None), ('1-byte', self.one_byte)):
                k = 0
                while True:
                    k += 1
                    ino = self.prepare(start)
                    status = self.run_child(self.action(key), writer, kill_at=k, pwrite=pwrite)
                    self.assertIn(status, (0, 9), f'{name}, {writes} writes, boundary {k}')
                    self.assertEqual(os.stat(hil_lock.lock_path(BOARD)).st_ino, ino, f'{name}: inode replaced')
                    out.append((name, writes, k, status, self.lock_bytes()[:1], self.reads(hil_lock),
                                self.reads(self.old)))
                    if status == 0:
                        break
        return out

    @staticmethod
    def table(m) -> str:
        return '\n'.join(f'{r[0]:45} {r[1]:6} k={r[2]:<3} exit={r[3]} byte0={r[4]!r:5} new={r[5]:7} old={r[6]}'
                          for r in m)

    def test_only_an_interrupted_acquisition_reads_free(self):
        m = self.matrix(hil_lock)
        expected = {name: (cut, done) for name, _, _, cut, done in self.ROWS}
        acquisitions = {name for name, key, *_ in self.ROWS if key in self.ACQUISITIONS}
        for name, writes, k, status, byte0, new, old in m:
            where = f'{name}, {writes} writes, boundary {k}\n{self.table(m)}'
            self.assertEqual(new, expected[name][0 if status == 9 else 1], where)
            # ACQUIRING only after an acquisition's first write and before it returned
            self.assertEqual(byte0 == b'~', name in acquisitions and status == 9 and k > 1, where)
            self.assertEqual(old, 'blocked' if byte0 == b'~' else new, where)

    def test_a_pre154_writer_reads_the_same_to_both_helpers(self):
        rows = [r for r in self.ROWS if r[1] in self.ACQUISITIONS + ('to_cleanup',)]
        m = self.matrix(self.old, rows)
        for name, writes, k, status, byte0, new, old in m:
            where = f'{name}, {writes} writes, boundary {k}\n{self.table(m)}'
            self.assertEqual(new, old, where)
            self.assertNotEqual(byte0, b'~', where)
            if status == 9 and k > 1:      # behind REWRITING: refused by both, as before #154
                self.assertEqual(new, 'blocked', where)

    def test_a_public_rewrite_never_writes_the_acquisition_sentinel(self):
        def rewrite(m, arm):
            fh = m.flock_nb(BOARD)
            arm()
            m.write_record(fh, 'selftest', acquiring=True, first='~')
        self.prepare(self.DEAD)
        self.assertEqual(self.run_child(rewrite, hil_lock, kill_at=2), 9)
        self.assertEqual(self.lock_bytes()[:1], b'#')
        self.prepare(self.DEAD)
        self.assertEqual(self.run_child(rewrite, hil_lock), 0)
        self.assertEqual({k: v for k, v in hil_lock.read_record(BOARD).items() if k in ('acquiring', 'first')},
                         {'acquiring': True, 'first': '~'}, 'only fields of the record')

    def test_a_write_that_makes_no_progress_fails_instead_of_looping(self):
        def zero_at(n):
            def wrap(real):
                calls = []

                def pwrite(fd, data, offset):
                    calls.append(1)
                    return 0 if len(calls) == n else real(fd, data, offset)
                return pwrite
            return wrap
        for n in (1, 2, 3):                    # the sentinel, the body, byte 0
            for key, start, reading in (('ordinary', self.DEAD, 'free'), ('protected', self.DEAD, 'free'),
                                        ('to_cleanup', self.PROTECTED, 'blocked')):
                with self.subTest(write=n, transition=key):
                    self.prepare(start)
                    # 3: raised; a loop that never ends dies by its child's SIGALRM instead
                    self.assertEqual(self.run_child(self.action(key), hil_lock, pwrite=zero_at(n)), 3)
                    self.assertEqual(self.reads(hil_lock), reading)

    def test_an_interrupted_acquisition_is_free_to_take_release_and_status(self):
        def killed_mid_acquisition():
            self.prepare(b'')
            self.assertEqual(self.run_child(self.protected, hil_lock, kill_at=2), 9)
            self.assertEqual(self.lock_bytes()[:1], b'~')
        killed_mid_acquisition()
        with quiet() as (out, _):
            hil_lock.cmd_status()
            self.assertEqual(hil_lock.cmd_release([BOARD]), 0)
        self.assertEqual(out.getvalue().strip(), f'{BOARD}: acquisition in progress or interrupted '
                                                 f'(free once its taker is gone)')
        self.assertEqual(hil_lock.record_state(BOARD)[0], 'empty')
        killed_mid_acquisition()
        self.assertIsNotNone(self.acquire())
        self.assertEqual(hil_lock.read_record(BOARD)['reason'], 'selftest')

    def test_a_journal_outranks_an_acquiring_record(self):
        self.prepare(b'~' + json.dumps(self.DEAD).encode()[1:])
        Path(self.dir, BOARD + hil_lock.JOURNAL).write_text(json.dumps({'token': 't', 'locks': [BOARD]}))
        with self.assertRaisesRegex(hil_lock.RecoveryBlocked, 'takeover journal'):
            self.acquire()

    def test_two_uids_share_the_sentinel(self):
        if os.geteuid() != 0:
            self.skipTest('needs root to act as two OS users')
        shared = tempfile.mkdtemp(prefix='hil-lock-uids-', dir='/tmp')
        self.addCleanup(shutil.rmtree, shared)
        os.chmod(shared, 0o1777)
        self.patch(hil_lock, 'BOARD_LOCK_DIR', shared)
        self.patch(self.old, 'BOARD_LOCK_DIR', shared)

        def as_user(uid, action, **kw):
            def run(m, arm):
                os.setgid(uid)
                os.setuid(uid)
                action(m, arm)
            return self.run_child(run, hil_lock, **kw)
        self.assertEqual(as_user(65534, self.protected, kill_at=2), 9)
        st = os.stat(hil_lock.lock_path(BOARD))
        self.assertEqual((st.st_uid, Path(hil_lock.lock_path(BOARD)).read_bytes()[:1]), (65534, b'~'))
        self.assertEqual(self.reads(self.old), 'blocked')
        self.assertEqual(as_user(65533, self.protected), 0)          # another user takes it
        self.assertEqual(os.stat(hil_lock.lock_path(BOARD)).st_ino, st.st_ino)
        self.assertEqual(self.reads(hil_lock), 'blocked')            # its finished protected record
        self.assertEqual(as_user(65534, self.to_cleanup, kill_at=2), 9)
        self.assertEqual(self.reads(hil_lock), 'blocked')


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

    def test_takeover_predecessor_names_the_original_holder_through_a_dead_takers_journal(self):
        self.blocked()
        _, handles = hil_lock.take_over(BOARD, 'selftest recovery')
        first = self.journal(BOARD)
        hil_lock._drop_flocks(handles)               # that taker died
        token, handles = hil_lock.take_over(BOARD, 'selftest recovery again')
        self.addCleanup(hil_lock.end_takeover, handles)
        state, rec = hil_lock.takeover_predecessor(BOARD, token)
        self.assertEqual((state, rec['reason'], rec['protected']), ('record', 'selftest run', True))
        with self.assertRaisesRegex(LookupError, 'not the takeover of token'):
            hil_lock.takeover_predecessor(BOARD, first['token'])
        with self.assertRaisesRegex(LookupError, 'no takeover journal'):
            hil_lock.takeover_predecessor(self.OTHER, token)

    def test_takeover_predecessor_refuses_an_unreadable_chain(self):
        path = os.path.join(self.dir, BOARD + hil_lock.JOURNAL)
        for journal, why in (('{"tok', 'cannot be read'),
                             (json.dumps({'token': 't', 'predecessor': '', 'prior': {}}), 'earlier takeover that cannot be read'),
                             (json.dumps({'token': 't', 'predecessor': '', 'prior': 'x'}), 'earlier takeover that cannot be read'),
                             (json.dumps({'token': 't', 'prior': None}), "of token 't' kept no predecessor"),
                             (json.dumps({'token': 't', 'predecessor': '', 'prior': {'token': 'u'}}), "of token 'u' kept no predecessor"),
                             *((json.dumps({'token': 't', 'predecessor': '', 'prior': {
                                 **middle, 'prior': {'token': 'v', 'predecessor': '{"reason": "selftest run"}', 'prior': None}}}),
                                rf"of token {middle.get('token')!r} kept no predecessor")
                               for middle in ({'predecessor': ''}, {'token': 7, 'predecessor': ''}, {'token': 'u'}))):
            with self.subTest(journal=journal):
                Path(path).write_text(journal)
                with self.assertRaisesRegex(LookupError, why):
                    hil_lock.takeover_predecessor(BOARD, 't')
        Path(path).write_text(json.dumps({'token': 't', 'predecessor': '#{"pid": 1', 'prior': None}))
        self.assertEqual(hil_lock.takeover_predecessor(BOARD, 't'), ('unreadable', None))

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


class HandOver(LockDir):
    """hand_to_cleanup_holder: a runner's own flocks go to a fresh-interpreter cleanup holder whose
    release predicate (every run_tracked receipt `done`) release_token checks up front and the
    holder rechecks before clearing; a failed handoff leaves the protected record."""

    OTHER = BOARD + '-2'

    def receipt(self):
        return make_receipt(self, Path(self.dir, 'state'))

    def hand_over(self, receipts=(), boards=(BOARD,)):
        handles = {b: hil_lock.take_board_lock(b, 'selftest run', protected=True, locks=list(boards)) for b in boards}
        try:
            token = hil_lock.hand_to_cleanup_holder(handles, 'selftest run', detail='park failed', receipts=receipts,
                                                    wait=5, holder='selftest')
        finally:
            for fh in handles.values():
                fh.close()
        pid = hil_lock.read_record(boards[0])['pid']
        self.addCleanup(DetachedHolder.kill_quietly, pid, signal.SIGKILL)
        return token, pid

    def group(self):
        boards = [BOARD, self.OTHER]
        return {b: hil_lock.take_board_lock(b, 'selftest run', protected=True, locks=boards) for b in boards}

    def signals(self):
        sent = []
        real = hil_lock.signal.pidfd_send_signal
        self.patch(hil_lock.signal, 'pidfd_send_signal', lambda fd, sig: (sent.append(sig), real(fd, sig)))
        return sent

    def test_an_open_receipt_refuses_up_front_and_the_holder_ends_once_it_is_done(self):
        receipt, done = self.receipt()
        token, pid = self.hand_over([receipt], (BOARD, self.OTHER))
        rec = hil_lock.read_record(BOARD)
        self.assertEqual((rec['role'], rec['protected'], rec['receipts'], rec['locks'], rec['holder']),
                         ('cleanup-holder', True, [receipt], sorted([BOARD, self.OTHER]), 'selftest'))
        self.assertNotEqual(pid, os.getpid())
        self.assertTrue(hil_lock._holds_flock(pid, hil_lock.lock_path(self.OTHER)))
        sent = self.signals()
        with self.assertRaisesRegex(hil_lock.ReleaseRefused,
                                    rf'not signalled: holder pid {pid} of token {token} keeps {BOARD}, {self.OTHER}: '
                                    r'its operations are not proven ended: receipt t \(supervisor pid 4242\) pending'):
            hil_lock.release_token(token)
        self.assertEqual(sent, [])
        # the holder's own recheck decides: a release that slips past the up-front check leaves it holding
        self.patch(hil_lock, 'RELEASE_WAIT', 0.5)
        with mock.patch.object(hil_lock, '_release_blocker', lambda *a: ''):
            with self.assertRaisesRegex(hil_lock.ReleaseRefused, f'signalled, but holder pid {pid} left its records'):
                hil_lock.release_token(token)
        self.assertEqual((sent, hil_lock.read_record(BOARD)), ([signal.SIGUSR1], rec))
        done()
        self.patch(hil_lock, 'RELEASE_WAIT', 5)
        self.assertEqual(hil_lock.release_token(token), (pid, sorted([BOARD, self.OTHER])))
        for b in (BOARD, self.OTHER):
            self.assertIsNone(hil_lock.read_record(b))
        self.assertIsNotNone(self.acquire())

    def test_token_mismatch_pid_reuse_and_a_partial_lock_set_signal_nothing(self):
        token, pid = self.hand_over(boards=(BOARD, self.OTHER))
        sent = self.signals()
        with self.assertRaisesRegex(hil_lock.ReleaseRefused, 'no holder record carries token nope'):
            hil_lock.release_token('nope')
        recs = {b: hil_lock.read_record(b) for b in (BOARD, self.OTHER)}
        for b, rec in recs.items():
            Path(hil_lock.lock_path(b)).write_text(json.dumps({**rec, 'start': '1'}))
        with self.assertRaisesRegex(hil_lock.ReleaseRefused, rf'pid {pid} is not the recorded holder'):
            hil_lock.release_token(token)
        Path(hil_lock.lock_path(BOARD)).write_text(json.dumps(recs[BOARD]))
        Path(hil_lock.lock_path(self.OTHER)).write_text('')
        with self.assertRaisesRegex(hil_lock.ReleaseRefused, r'record lock set .* != files carrying it'):
            hil_lock.release_token(token)
        self.assertEqual(sent, [])
        Path(hil_lock.lock_path(self.OTHER)).write_text(json.dumps(recs[self.OTHER]))
        self.assertEqual(hil_lock.release_token(token), (pid, sorted([BOARD, self.OTHER])))   # no predicate

    def test_a_failed_handoff_leaves_the_protected_record(self):
        receipt, _ = self.receipt()
        fh = hil_lock.take_board_lock(BOARD, 'selftest run', protected=True)
        self.patch(hil_lock, '_holder_argv', lambda job: [sys.executable, '-c', 'pass'])
        before = sorted(os.listdir('/proc/self/fd'))
        with self.assertRaisesRegex(hil_lock.LockUnavailable, 'cleanup holder handoff failed: .*no readiness'):
            hil_lock.hand_to_cleanup_holder({BOARD: fh}, 'selftest run', detail='park failed', receipts=[receipt],
                                            wait=2)
        self.assertEqual(sorted(os.listdir('/proc/self/fd')), before)
        fh.close()
        rec = hil_lock.read_record(BOARD)
        self.assertEqual((rec['protected'], rec['detail'], rec['receipts']), (True, 'park failed', [receipt]))
        self.assertNotIn('role', rec)
        with self.assertRaises(hil_lock.RecoveryBlocked):
            self.acquire()

    def test_a_failed_multi_board_handoff_leaves_a_takeover_of_one_board_the_whole_group(self):
        fhs = self.group()
        self.patch(hil_lock, '_holder_argv', lambda job: [sys.executable, '-c', 'pass'])
        with self.assertRaisesRegex(hil_lock.LockUnavailable, 'cleanup holder handoff failed: .*no readiness'):
            hil_lock.hand_to_cleanup_holder(fhs, 'selftest run', detail='park failed', wait=2)
        for fh in fhs.values():
            fh.close()
        _, handles = hil_lock.take_over(BOARD, 'recover')
        self.addCleanup(hil_lock.end_takeover, handles)
        self.assertEqual(sorted(handles), sorted([BOARD, self.OTHER]))

    def test_a_malformed_or_disagreeing_predicate_refuses_up_front(self):
        token, pid = self.hand_over(boards=(BOARD, self.OTHER))
        sent = self.signals()
        recs = {b: hil_lock.read_record(b) for b in (BOARD, self.OTHER)}
        for receipts, why in (('x', r"its receipts 'x' are not a list"), (None, 'its receipts None are not a list')):
            for b, rec in recs.items():
                Path(hil_lock.lock_path(b)).write_text(json.dumps({**rec, 'receipts': receipts}))
            with self.assertRaisesRegex(hil_lock.ReleaseRefused, f'release predicate cannot be evaluated: {why}'):
                hil_lock.release_token(token)
        Path(hil_lock.lock_path(BOARD)).write_text(json.dumps(recs[BOARD]))
        Path(hil_lock.lock_path(self.OTHER)).write_text(json.dumps({k: v for k, v in recs[self.OTHER].items()
                                                                    if k != 'receipts'}))
        with self.assertRaisesRegex(hil_lock.ReleaseRefused, 'its records disagree on their receipts'):
            hil_lock.release_token(token)
        self.assertEqual(sent, [])

    def test_the_holder_reads_its_predicate_from_every_record_it_holds(self):
        receipt, done = self.receipt()
        done()
        fhs = [hil_lock.take_board_lock(b, 'selftest run', protected=True) for b in (BOARD, self.OTHER)]
        for fh in fhs:
            self.addCleanup(fh.close)

        def records(*receipts):
            for fh, r in zip(fhs, receipts):
                hil_lock.write_record(fh, 'selftest run', **({} if r is None else {'receipts': r}))
            return hil_lock._releasable(fhs)
        self.assertTrue(records(None, None), 'no receipts: no predicate')
        self.assertTrue(records([receipt], [receipt]))
        self.assertFalse(records([receipt], []), 'records that disagree')
        self.assertFalse(records([receipt], None), 'records that disagree')
        self.assertFalse(records('x', 'x'), 'malformed receipts')
        records(None, None)
        os.pwrite(fhs[1].fileno(), b'#', 0)
        self.assertFalse(hil_lock._releasable(fhs), 'an unreadable record')

    def test_unprotected_handles_are_refused_before_anything_changes(self):
        fhs = {BOARD: hil_lock.take_board_lock(BOARD, 'selftest run', protected=True),
               self.OTHER: hil_lock.take_board_lock(self.OTHER, 'selftest run')}
        for fh in fhs.values():
            self.addCleanup(fh.close)
        before = {b: Path(hil_lock.lock_path(b)).read_bytes() for b in fhs}
        self.patch(hil_lock, '_holder_argv', lambda job: self.fail('a holder was started'))
        with self.assertRaisesRegex(ValueError, rf'{self.OTHER} not taken protected; nothing changed'):
            hil_lock.hand_to_cleanup_holder(fhs, 'selftest run', detail='park failed')
        self.assertEqual({b: Path(hil_lock.lock_path(b)).read_bytes() for b in fhs}, before)
        with self.assertRaises(hil_lock.BoardLocked):        # still the caller's
            hil_lock.take_board_lock(self.OTHER, 'rival')

    def test_a_failed_rewrite_or_an_interrupt_leaves_every_board_blocked(self):
        self.patch(hil_lock, '_holder_argv', lambda job: [sys.executable, '-c', 'pass'])
        real = hil_lock.write_record

        def no_space(*a, **k):
            raise OSError(28, 'No space left on device')

        def interrupt_second(fh, *a, calls=[], **k):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(fh, *a, **k)
        for fault, raised in ((no_space, hil_lock.LockUnavailable), (interrupt_second, KeyboardInterrupt)):
            with self.subTest(fault=fault.__name__):
                fhs = self.group()
                with mock.patch.object(hil_lock, 'write_record', fault), self.assertRaises(raised):
                    hil_lock.hand_to_cleanup_holder(fhs, 'selftest run', detail='park failed', wait=2)
                for fh in fhs.values():
                    fh.close()
                for b in fhs:
                    with self.assertRaises(hil_lock.RecoveryBlocked):
                        hil_lock.take_board_lock(b, 'rival')
                    Path(hil_lock.lock_path(b)).write_text('')     # the test's stand-in for the recovery

    def test_a_release_that_lands_on_another_thread_ends_the_hold(self):
        def holder():
            fh = hil_lock.take_board_lock(BOARD, 'selftest run', protected=True)
            r, _ = os.pipe()
            reader = threading.Thread(target=os.read, args=(r, 1), daemon=True)   # SIGUSR1 unblocked there
            reader.start()
            wake = hil_lock.arm_release()
            signal.pthread_kill(reader.ident, signal.SIGUSR1)     # before the holder waits
            hil_lock.wait_for_release(wake, [fh])
            hil_lock.release_board_lock(fh)
        # None: it never ended; -10: SIGUSR1's default action killed it
        self.assertEqual(fork_child(holder), 0)
        self.assertEqual(hil_lock.record_state(BOARD)[0], 'empty')
        self.assertIsNotNone(self.acquire())

    def test_a_release_ends_the_hold_of_a_holder_started_with_sigusr1_blocked(self):
        def holder():
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})    # as a parent's mask leaves it
            fh = hil_lock.take_board_lock(BOARD, 'selftest run', protected=True)
            wake = hil_lock.arm_release()
            os.kill(os.getpid(), signal.SIGUSR1)
            hil_lock.wait_for_release(wake, [fh])
            hil_lock.release_board_lock(fh)
        self.assertEqual(fork_child(holder), 0, 'the release stayed pending behind the inherited mask')
        self.assertEqual(hil_lock.record_state(BOARD)[0], 'empty')


class ProtectedGroup(LockDir):
    """agentrc#197: a protected taker declares its whole group on its first record, so a takeover
    from any member, the one whose hand-over rewrite failed or was cut short included, takes the
    group; an older helper's record, which declares none, adds every board naming it."""

    A, B = BOARD, BOARD + '-2'

    def group(self):
        boards = [self.A, self.B]
        return {b: hil_lock.take_board_lock(b, 'selftest run', protected=True, locks=boards) for b in boards}

    def recover(self, board) -> list:
        _, handles = hil_lock.take_over(board, 'selftest recovery')
        hil_lock.end_takeover(handles)
        return sorted(handles)

    def no_holder(self):
        self.patch(hil_lock, '_holder_argv', lambda job: [sys.executable, '-c', 'pass'])

    def test_the_group_is_declared_on_the_first_record_and_a_malformed_one_touches_nothing(self):
        fh = hil_lock.take_board_lock(self.A, 'selftest run', protected=True)
        self.addCleanup(fh.close)
        self.assertEqual(hil_lock.read_record(self.A)['locks'], [self.A])
        for locks in ([self.A], [self.B, self.B], [self.B, '../x'], [self.B, 1], self.B, None):
            with self.subTest(locks=locks), self.assertRaises(ValueError):
                hil_lock.take_board_lock(self.B, 'selftest run', protected=True, locks=locks)
        self.assertFalse(os.path.exists(hil_lock.lock_path(self.B)))

    def test_recovery_from_the_member_whose_rewrite_failed_takes_the_group(self):
        fhs = self.group()
        real = hil_lock.write_record

        def fail_on_b(fh, *a, **k):
            if fh is fhs[self.B]:
                raise OSError(28, 'No space left on device')
            return real(fh, *a, **k)
        self.no_holder()
        with mock.patch.object(hil_lock, 'write_record', fail_on_b):
            with self.assertRaisesRegex(hil_lock.LockUnavailable, rf'not written on {self.B}: \[Errno 28\]'):
                hil_lock.hand_to_cleanup_holder(fhs, 'selftest run', detail='park failed', wait=2)
        for fh in fhs.values():
            fh.close()
        self.assertEqual(hil_lock.read_record(self.A)['detail'], 'park failed')
        self.assertNotIn('detail', hil_lock.read_record(self.B))      # its prior record
        with self.assertRaises(hil_lock.RecoveryBlocked):
            hil_lock.take_board_lock(self.B, 'rival')
        self.assertEqual(self.recover(self.B), [self.A, self.B])

    def test_recovery_from_the_member_whose_rewrite_was_cut_short_takes_the_group(self):
        def hand_over_and_die():
            fhs = self.group()
            real, b = hil_lock._pwrite_all, fhs[self.B].fileno()

            def die(fd, data, offset):
                if fd == b and offset == 1:          # past its REWRITING byte
                    os.pwrite(fd, data[:5], offset)
                    os._exit(9)
                return real(fd, data, offset)
            hil_lock._pwrite_all = die
            hil_lock.hand_to_cleanup_holder(fhs, 'selftest run', detail='park failed', wait=2)
        self.assertEqual(fork_child(hand_over_and_die), 9)
        self.assertEqual(hil_lock.record_state(self.B)[0], 'unreadable')
        self.assertEqual(hil_lock.read_record(self.A)['locks'], [self.A, self.B])
        self.assertEqual(self.recover(self.B), [self.A, self.B])

    def test_an_older_helpers_record_without_a_group(self):
        old = load_pre154()
        self.patch(old, 'BOARD_LOCK_DIR', self.dir)
        old.take_board_lock(self.B, 'selftest run', protected=True).close()
        self.assertNotIn('locks', hil_lock.read_record(self.B))
        with self.assertRaises(hil_lock.RecoveryBlocked):
            hil_lock.take_board_lock(self.B, 'rival')
        self.assertEqual(self.recover(self.B), [self.B], 'alone: that board, as before')
        # an older pin's hand-over that rewrote A naming both and failed on B
        fhs = {b: old.take_board_lock(b, 'selftest run', protected=True) for b in (self.A, self.B)}
        old.write_record(fhs[self.A], 'selftest run', protected=True, detail='park failed', locks=[self.A, self.B])
        for fh in fhs.values():
            fh.close()
        self.assertEqual(self.recover(self.B), [self.A, self.B])

    def test_a_group_present_but_not_a_list_of_names_refuses_the_takeover(self):
        for locks in (self.B, False, 0, '', {}, [], None, [1]):
            with self.subTest(locks=locks):
                Path(hil_lock.lock_path(self.A)).write_text(json.dumps(
                    {'pid': 999999999, 'reason': 'selftest run', 'protected': True, 'locks': locks}))
                with self.assertRaisesRegex(hil_lock.LockUnavailable, 'is not a list of names'):
                    hil_lock.take_over(self.A, 'selftest recovery')
                self.assertEqual(hil_lock.journal_names(), [])

    def test_a_malformed_group_still_names_its_readable_members_and_refuses(self):
        Path(hil_lock.lock_path(self.A)).write_text('{}')                  # declares nothing
        held = {'pid': 999999999, 'reason': 'selftest run', 'protected': True}
        for where in ('record', 'journal'):
            with self.subTest(where=where):
                journal = Path(self.dir, self.B + hil_lock.JOURNAL)
                if where == 'record':
                    Path(hil_lock.lock_path(self.B)).write_text(json.dumps({**held, 'locks': [self.A, 1]}))
                else:
                    Path(hil_lock.lock_path(self.B)).write_text(json.dumps(held))
                    journal.write_text(json.dumps({'token': 'selftest', 'locks': [self.A, 1]}))
                before = Path(hil_lock.lock_path(self.B)).read_bytes()
                with self.assertRaisesRegex(hil_lock.LockUnavailable, f'{self.B}: lock set .* is not a list of names'):
                    hil_lock.take_over(self.A, 'selftest recovery')
                self.assertEqual(Path(hil_lock.lock_path(self.B)).read_bytes(), before)
                self.assertEqual(hil_lock.read_record(self.A), {})
                self.assertEqual(hil_lock.journal_names(), [self.B] if where == 'journal' else [])

    def test_a_member_whose_chain_is_partly_unreadable_still_names_the_group_and_refuses(self):
        Path(hil_lock.lock_path(self.A)).write_bytes(b'#{"pid": 9')        # cut short: declares nothing
        Path(hil_lock.lock_path(self.B)).write_text(json.dumps(
            {'pid': 999999999, 'reason': 'selftest run', 'protected': True, 'locks': [self.A, self.B]}))
        Path(self.dir, self.B + hil_lock.JOURNAL).write_text('{"locks": [')
        with self.assertRaisesRegex(hil_lock.LockUnavailable, f'{self.B}{hil_lock.JOURNAL} cannot be read'):
            hil_lock.take_over(self.A, 'selftest recovery')
        self.assertEqual(hil_lock.journal_names(), [self.B])

    def test_a_partial_acquisition_rolls_back_and_a_failed_rollback_blocks_the_whole_group(self):
        boards = [self.A, self.B]
        rival = hil_lock.take_board_lock(self.B, 'rival')

        def acquire():
            taken = []
            try:
                for b in boards:
                    taken.append(hil_lock.take_board_lock(b, 'selftest run', protected=True, locks=boards))
            except hil_lock.BoardLocked:
                for fh in taken:
                    hil_lock.release_board_lock(fh)
                raise
        with self.assertRaises(hil_lock.BoardLocked):
            acquire()
        hil_lock.release_board_lock(hil_lock.take_board_lock(self.A, 'next'))     # free again
        with mock.patch.object(hil_lock, 'clear_record', lambda fh: False), self.assertRaises(hil_lock.BoardLocked):
            acquire()
        self.assertEqual(hil_lock.read_record(self.A)['locks'], boards)
        with self.assertRaises(hil_lock.RecoveryBlocked):
            hil_lock.take_board_lock(self.A, 'next')
        with self.assertRaises(hil_lock.BoardLocked):        # the rival's board: nothing modified
            hil_lock.take_over(self.A, 'selftest recovery')
        self.assertEqual(hil_lock.journal_names(), [])
        hil_lock.release_board_lock(rival)
        self.assertEqual(self.recover(self.A), boards)

    def test_a_handoff_of_boards_other_than_the_declared_group_changes_nothing(self):
        fhs = self.group()
        for fh in fhs.values():
            self.addCleanup(fh.close)
        before = {b: Path(hil_lock.lock_path(b)).read_bytes() for b in fhs}
        self.patch(hil_lock, '_holder_argv', lambda job: self.fail('a holder was started'))
        with self.assertRaisesRegex(ValueError, rf'not the handed boards \[.{self.A}.\] .*nothing changed'):
            hil_lock.hand_to_cleanup_holder({self.A: fhs[self.A]}, 'selftest run', detail='park failed')
        self.assertEqual({b: Path(hil_lock.lock_path(b)).read_bytes() for b in fhs}, before)


PENDING_AS = r'''
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location('agentrc_hil_lock', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.BOARD_LOCK_DIR, lock, uid = sys.argv[2], sys.argv[3], int(sys.argv[4])
os.setgroups([])
os.setresgid(uid, uid, uid)
os.setresuid(uid, uid, uid)
out = {}
for name, f in (('read', lambda: m.PendingSessions(lock, 'selftest').read()),
                ('add', lambda: m.PendingSessions(lock, 'selftest').add([{'receipt': 'theirs'}])),
                ('clear', lambda: m.PendingSessions(lock, 'selftest').clear(lambda e: True)),
                ('own', lambda: m.PendingSessions(lock + '-theirs', 'selftest').add([{'receipt': 'theirs'}]))):
    try:
        out[name] = ['ok', f()]
    except Exception as e:
        out[name] = [type(e).__name__, str(e)]
print(json.dumps(out))
'''


class Pending(LockDir):
    """PendingSessions: a board's pending-session record beside its lock, read, extended and
    cleared under its own mutex, replaced atomically; it reads TinyUF2's existing marks
    (<lock>.tinyuf2-openocd-pending.json) unchanged. Whether a session ended stays the caller's."""

    KIND = 'tinyuf2-openocd'

    def setUp(self):
        super().setUp()
        self.mark = hil_lock.PendingSessions(BOARD, self.KIND)

    def file(self, suffix=''):
        return Path(self.dir, f'{BOARD}.{self.KIND}-pending.json{suffix}')

    @staticmethod
    def sessions(*names):
        return [{'cmd': 'openocd', 'receipt': f'/run/{n}/receipt.json', 'pid': 1, 'start': 2} for n in names]

    def test_reads_and_writes_tinyuf2_marks_unchanged(self):
        old, new = self.sessions('a'), self.sessions('b')
        with open(self.file('.mutex'), 'w'):            # TinyUF2 made the mutex first
            pass
        self.file().write_text(json.dumps({'sessions': old}, indent=1))     # its write_mark
        logged = {'mark': str(self.file()), 'sessions': old, 'notEnded': [], 'note': 'n', 'hold': {}, 'at': 'x'}
        self.file('.cleared.jsonl').write_text(json.dumps(logged) + '\nnot json\n')
        self.assertEqual((os.fspath(self.mark), str(self.mark)), (str(self.file()), str(self.file())))
        self.assertEqual(self.mark.read(), old)
        self.assertEqual(self.mark.cleared(), [logged])
        self.assertEqual(self.mark.add(new + old), old + new)
        self.assertEqual(self.file().read_text(), json.dumps({'sessions': old + new}, indent=1))
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(p.name for p in (
            self.file(), self.file('.mutex'), self.file('.cleared.jsonl'))))
        for name in (f'{BOARD}.lock', f'{BOARD}.takeover'):
            self.assertNotIn(name, os.listdir(self.dir))
        self.assertEqual((hil_lock.lock_names(), hil_lock.journal_names()), ([], []))

    def test_modes_ignore_the_umask(self):
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        self.mark.add(self.sessions('a'))
        self.mark.clear(lambda e: False, note='reviewed')
        self.mark.add(self.sessions('b'))
        modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in Path(self.dir).iterdir()}
        self.assertEqual(modes, {self.file().name: 0o644, self.file('.mutex').name: 0o666,
                                 self.file('.cleared.jsonl').name: 0o666})

    def test_an_unreadable_record_is_kept_and_refuses_until_a_reviewed_clear(self):
        for data in (b'{', b'[]', b'{"sessions": "x"}', b'{"sessions": [1]}', b'{"sessions": [{"pid": 1}]}',
                     b'\xff'):
            with self.subTest(data=data):
                self.file().write_bytes(data)
                self.assertIsNone(self.mark.read())
                with self.assertRaisesRegex(hil_lock.PendingRefused, 'unreadable: kept'):
                    self.mark.add(self.sessions('a'))
                with self.assertRaisesRegex(hil_lock.PendingRefused, 'unreadable: a session may still be pending'):
                    self.mark.clear(lambda e: True)
                self.assertEqual(self.file().read_bytes(), data)
                cleared = self.mark.clear(lambda e: True, note='reviewed', hold={'pid': 7})
                self.assertFalse(self.file().exists())
                self.assertEqual((cleared['sessions'], cleared['unreadable'], cleared['hold']),
                                 (None, data.decode(errors='replace'), {'pid': 7}))
                self.assertEqual(self.mark.cleared()[-1]['unreadable'], data.decode(errors='replace'))

    def test_a_clear_is_refused_while_a_session_is_not_ended(self):
        a, b = self.sessions('a', 'b')
        self.mark.add([a, b])
        before = self.file().read_bytes()

        def only_a(e):
            if e is not a and e['receipt'] == b['receipt']:
                raise RuntimeError('receipt unreadable')
            return e['receipt'] == a['receipt']
        for ended in (only_a, lambda e: 'yes'):
            with self.subTest(ended=ended):
                with self.assertRaisesRegex(hil_lock.PendingRefused, 'not ended'):
                    self.mark.clear(ended)
                self.assertEqual(self.file().read_bytes(), before)
        self.assertFalse(self.file('.cleared.jsonl').exists())
        self.assertEqual(self.mark.clear(lambda e: True), {'mark': str(self.file()), 'sessions': [a, b],
                                                           'notEnded': []})
        self.assertFalse(self.file().exists())
        self.assertEqual(self.mark.clear(lambda e: False), {'mark': str(self.file()), 'sessions': [],
                                                            'notEnded': []})
        self.mark.add([a, b])
        cleared = self.mark.clear(only_a, note='ended by hand', hold={'pid': 7})
        self.assertEqual((cleared['notEnded'], cleared['note']), ([b['receipt']], 'ended by hand'))
        logged = self.mark.cleared()
        self.assertEqual(len(logged), 1)
        self.assertEqual({k: v for k, v in logged[0].items() if k != 'at'}, cleared)
        self.assertFalse(self.file().exists())

    def test_a_failed_append_leaves_no_torn_entry_and_loses_none(self):
        a = self.sessions('a')[0]
        self.mark.add([a])
        self.mark.clear(lambda e: False, note='first')
        self.mark.add([a])
        real, calls = os.write, []

        def short_then_full(fd, data):
            calls.append(1)
            if len(calls) == 1:
                return real(fd, data[:10])
            raise OSError(28, 'No space left on device')
        with mock.patch.object(hil_lock.os, 'write', short_then_full), self.assertRaises(OSError):
            self.mark.clear(lambda e: False, note='second')
        self.assertTrue(self.file().exists())
        self.assertFalse(self.file('.cleared.jsonl').read_bytes().endswith(b'\n'))
        self.assertEqual([e['note'] for e in self.mark.cleared()], ['first'])
        self.mark.clear(lambda e: False, note='retry')
        self.assertFalse(self.file().exists())
        self.assertEqual([e['note'] for e in self.mark.cleared()], ['first', 'retry'])
        self.assertEqual(len(self.file('.cleared.jsonl').read_bytes().splitlines()), 2)

    def test_the_log_is_durable_in_its_dir_before_the_record_goes(self):
        self.mark.add(self.sessions('a'))
        events, fsync, unlink = [], os.fsync, os.unlink

        def logged_fsync(fd):
            events.append('dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return fsync(fd)

        def logged_unlink(name, *a, **k):
            events.append(f'unlink {name}')
            return unlink(name, *a, **k)
        with mock.patch.object(hil_lock.os, 'fsync', logged_fsync), \
                mock.patch.object(hil_lock.os, 'unlink', logged_unlink):
            self.mark.clear(lambda e: False, note='reviewed')
        self.assertEqual(events, ['file', 'dir', f'unlink {self.file().name}'])

    def test_a_malformed_session_is_refused_before_anything_is_touched(self):
        for sessions in ([{'pid': 1}], [{'receipt': 3}], ['x']):
            with self.subTest(sessions=sessions), self.assertRaises(ValueError):
                self.mark.add(sessions)
        self.assertEqual(os.listdir(self.dir), [])
        with self.assertRaises(OSError):
            hil_lock.PendingSessions('../x', self.KIND)

    def test_concurrent_updates_lose_nothing(self):
        def writer(n):
            def body():
                for i in range(10):
                    self.mark.add(self.sessions(f'{n}-{i}'))
            return body
        pids = []
        for n in range(8):
            pid = os.fork()
            if pid == 0:
                code = 1
                try:
                    writer(n)()
                    code = 0
                finally:
                    os._exit(code)
            pids.append(pid)
        self.assertEqual([os.waitstatus_to_exitcode(os.waitpid(p, 0)[1]) for p in pids], [0] * 8)
        got = [e['receipt'] for e in self.mark.read()]
        self.assertEqual(sorted(got), sorted(f'/run/{n}-{i}/receipt.json' for n in range(8) for i in range(10)))
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith('.')], [])

    def interrupted(self, action):
        """For each k, a child running action() killed right before its k-th call that can persist
        or remove anything; yields after each, until one completes."""
        names = ('open', 'fchmod', 'pwrite', 'write', 'fsync', 'close', 'rename', 'unlink')
        for k in range(1, 60):
            pid = os.fork()
            if pid == 0:
                try:
                    calls = []

                    def counted(real):
                        def call(*a, **kw):
                            calls.append(1)
                            if len(calls) == k:
                                os._exit(9)
                            return real(*a, **kw)
                        return call
                    for name in names:
                        setattr(os, name, counted(getattr(os, name)))
                    action()
                    os._exit(0)
                finally:
                    os._exit(1)
            code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
            self.assertIn(code, (0, 9))
            yield code
            if code == 0:
                return
        self.fail('never completed')

    def reset(self, *sessions):
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        self.mark.add(sessions)

    def test_an_interrupted_add_leaves_the_old_record_or_the_new_one(self):
        a, b = self.sessions('a', 'b')
        seen = set()
        self.reset(a)
        for code in self.interrupted(lambda: self.mark.add([b])):
            got = self.mark.read()
            self.assertIn(got, ([a], [a, b]))
            seen.add(len(got))
            self.assertEqual(self.mark.add([b]), [a, b])     # the next writer is never stuck
            self.reset(a)
        self.assertEqual(seen, {1, 2})

    def test_an_interrupted_reviewed_clear_never_removes_the_record_before_its_note_is_logged(self):
        a = self.sessions('a')[0]
        seen = set()
        self.reset(a)
        for code in self.interrupted(lambda: self.mark.clear(lambda e: False, note='reviewed')):
            logged = [e for e in self.mark.cleared() if e.get('note') == 'reviewed']
            present = self.file().exists()
            self.assertTrue(present or logged, 'removed without its note')
            seen.add((present, bool(logged)))
            if present:
                self.assertEqual(self.mark.read(), [a])
            self.reset(a)
        self.assertIn((True, False), seen)
        self.assertIn((False, True), seen)

    def test_another_os_user_reads_the_record_and_can_neither_replace_nor_remove_it(self):
        if os.geteuid() == 0:
            argv = [sys.executable]
        elif shutil.which('sudo') and subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0:
            argv = ['sudo', '-n', sys.executable]
        else:
            self.skipTest('needs root, or sudo -n, to act as another OS user')
        shared = tempfile.mkdtemp(prefix='hil-lock-pending-', dir='/tmp')
        root = argv[:-1]                    # [] as root, else sudo -n
        self.addCleanup(subprocess.run, [*root, 'rm', '-rf', shared])
        os.chmod(shared, 0o1777)
        subprocess.run([*root, 'chown', '0:0', shared], check=True)    # as the host step makes it: root's
        self.patch(hil_lock, 'BOARD_LOCK_DIR', shared)
        ours = hil_lock.PendingSessions(BOARD, 'selftest')
        ours.add([{'receipt': 'ours'}])
        before = Path(ours.path).read_bytes()
        r = subprocess.run([*argv, '-I', '-c', PENDING_AS, str(SCRIPT), shared, BOARD, '65534'],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['read'], ['ok', [{'receipt': 'ours'}]])
        self.assertEqual((out['add'][0], out['clear'][0]), ('PermissionError', 'PermissionError'))
        self.assertEqual(out['own'], ['ok', [{'receipt': 'theirs'}]])
        self.assertEqual(Path(ours.path).read_bytes(), before)
        theirs = hil_lock.PendingSessions(BOARD + '-theirs', 'selftest')
        self.assertEqual(theirs.read(), [{'receipt': 'theirs'}])
        if os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                theirs.add([{'receipt': 'ours'}])
            with self.assertRaises(PermissionError):
                theirs.clear(lambda e: True)
            self.assertEqual(theirs.read(), [{'receipt': 'theirs'}])
        self.assertEqual([f for f in os.listdir(shared) if f.startswith('.')], [])


class RechecksAs(LockDir):
    """_rechecks_as's child, here under our own ids: signalled and reaped only through its pidfd,
    its fds never leaked, and the whole recheck bounded by RELEASE_WAIT."""

    def setUp(self):
        super().setUp()
        self.ids = (os.geteuid(), os.getegid(), os.getgroups())
        if os.geteuid() != 0:
            self.patch(os, 'setgroups', lambda groups: None)    # needs CAP_SETGID even for our own groups
        self.receipt, self.done = make_receipt(self, Path(self.dir, 'state'))
        self.process = hil_lock._process()
        self.kill = os.kill
        self.patch(os, 'kill', lambda *a: self.fail('signalled by a bare pid'))

    def test_the_child_rechecks_and_is_reaped_through_its_pidfd(self):
        self.assertEqual(hil_lock._rechecks_as(self.ids, [self.receipt]), ['pending'])
        self.done()
        self.assertEqual(hil_lock._rechecks_as(self.ids, [self.receipt]), ['done'])

    def test_an_ignored_sigchld_reaps_it_first(self):
        real = self.process._Reports

        class Slow(real):
            def next(self, deadline):
                msg = super().next(deadline)
                _sleep(0.3)               # the kernel reaps the exited child meanwhile
                return msg
        self.patch(self.process, '_Reports', Slow)
        old = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGCHLD, old)
        self.done()
        self.assertEqual(hil_lock._rechecks_as(self.ids, [self.receipt]), ['done'])

    def test_a_fork_failure_leaks_no_descriptor(self):
        def fork():
            raise BlockingIOError(11, 'Resource temporarily unavailable')
        self.patch(os, 'fork', fork)
        before = sorted(os.listdir('/proc/self/fd'))
        states = hil_lock._rechecks_as(self.ids, [self.receipt])
        self.assertEqual(sorted(os.listdir('/proc/self/fd')), before)
        self.assertRegex(states[0], r'^unknown \(not rechecked as uid \d+: BlockingIOError\(11')

    def test_an_interrupt_after_a_close_closes_nothing_twice_and_still_reaps(self):
        pipes, pids, me = [], [], os.getpid()
        real_pipe, real_close, real_pidfd_open = os.pipe, os.close, os.pidfd_open
        self.patch(os, 'pipe', lambda: (pipes.append(real_pipe()), pipes[-1])[1])
        self.patch(os, 'pidfd_open', lambda pid, *a: (pids.append(pid), real_pidfd_open(pid, *a))[1])

        def close(fd):
            real_close(fd)
            if os.getpid() == me and pipes and fd == pipes[0][1] and not hasattr(close, 'fired'):
                close.fired = True        # right after the report writer is closed
                raise KeyboardInterrupt
        self.patch(os, 'close', close)
        before = sorted(os.listdir('/proc/self/fd'))
        with self.assertRaises(KeyboardInterrupt):
            hil_lock._rechecks_as(self.ids, [self.receipt])
        self.assertEqual(sorted(os.listdir('/proc/self/fd')), before)
        with self.assertRaises(ChildProcessError):      # reaped through its pidfd
            os.waitpid(pids[0], os.WNOHANG)

    def test_a_child_that_never_exits_is_abandoned_within_the_bound(self):
        pids, real = [], os.pidfd_open
        self.patch(os, 'pidfd_open', lambda pid, *a: (pids.append(pid), real(pid, *a))[1])
        self.patch(hil_lock, '_rechecks', lambda receipts: _sleep(60))           # in the child
        self.patch(hil_lock.signal, 'pidfd_send_signal', lambda fd, sig: None)   # as a D-state wait ignores SIGKILL
        self.patch(hil_lock, 'RELEASE_WAIT', 0.5)
        t = time.monotonic()
        states = hil_lock._rechecks_as(self.ids, [self.receipt])
        self.assertLess(time.monotonic() - t, 2)
        self.assertEqual(states, [f'unknown (the recheck child as uid {os.geteuid()} did not exit within 0.5s; '
                                  f'abandoned)'])
        self.assertEqual(len(pids), 1)
        self.kill(pids[0], signal.SIGKILL)
        os.waitpid(pids[0], 0)


# Run as root by CrossUserRelease: release_token of argv[3] with hil_lock argv[1] over lock dir argv[2].
RELEASE_AS_ROOT = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('agentrc_hil_lock', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.BOARD_LOCK_DIR = sys.argv[2]
try:
    print(json.dumps(['released', m.release_token(sys.argv[3])]))
except m.ReleaseRefused as e:
    print(json.dumps(['refused', str(e)]))
'''


class CrossUserRelease(LockDir):
    """Root releasing another user's receipt-backed cleanup holder rechecks the receipts as that
    holder: hil_process.recheck trusts only files of the evaluating euid. Covers this helper's
    holder and an older farm check's, whose record carries `receipts` and no other predicate
    field. As root the holder is uid 65534; otherwise root is `sudo -n`, skipped without it."""

    NOBODY = 65534

    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.holder_uid = self.NOBODY
        elif shutil.which('sudo') and subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0:
            self.holder_uid = None
        else:
            self.skipTest('needs root, or sudo -n, to release as another OS user')
        shared = tempfile.mkdtemp(prefix='hil-lock-release-', dir='/tmp')
        self.addCleanup(shutil.rmtree, shared)
        os.chmod(shared, 0o1777)              # root's _open_dir refuses another user's dir unless sticky
        self.patch(hil_lock, 'BOARD_LOCK_DIR', shared)
        self.states = Path(shared if self.holder_uid else self.dir)
        hil_lock._process()                   # loaded here: a holder dropped to 65534 may not read it

    def start_holder(self, receipts, older):
        """(token, pid) of a forked cleanup holder of BOARD as the holder uid; `older` handles
        SIGUSR1 as hil_farm_check's own `--hold` did before hand_to_cleanup_holder."""
        token = f'x1{int(older)}'
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                for fd in map(int, os.listdir('/proc/self/fd')):
                    if fd > 2 and fd != w:        # a receipt's lock among them: its flock is the supervisor's
                        with suppress(OSError):
                            os.close(fd)
                if self.holder_uid is not None:
                    os.setgroups([])
                    os.setresgid(self.holder_uid, self.holder_uid, self.holder_uid)
                    os.setresuid(self.holder_uid, self.holder_uid, self.holder_uid)
                wake = None if older else hil_lock.arm_release()
                fh = hil_lock.take_board_lock(BOARD, 'farm_check', protected=True)

                def bow_out(*_):
                    if all(hil_lock._process().recheck(r) == 'done' for r in receipts):
                        hil_lock.release_board_lock(fh)
                        os._exit(0)
                if older:
                    signal.signal(signal.SIGUSR1, bow_out)
                me = os.getpid()
                hil_lock.write_record(fh, 'farm_check', protected=True, holder='hil_farm_check',
                                      role=hil_lock.CLEANUP_ROLE, detail='d', receipts=receipts, pid=me,
                                      start=hil_lock.proc_start(me), token=token, locks=[BOARD])
                os.write(w, b'1')
                if older:
                    while True:
                        signal.pause()
                hil_lock.wait_for_release(wake, [fh])
                hil_lock.release_board_lock(fh)
                os._exit(0)
            finally:
                os._exit(1)
        os.close(w)
        def reap():
            DetachedHolder.kill_quietly(pid, signal.SIGKILL)
            with suppress(ChildProcessError):     # reaped by the test
                os.waitpid(pid, 0)
        self.addCleanup(reap)
        with os.fdopen(r, 'rb') as f:
            self.assertEqual(f.read(1), b'1')
        return token, pid

    def release_as_root(self, token) -> list:
        if os.geteuid() == 0:
            try:
                return json.loads(json.dumps(['released', hil_lock.release_token(token)]))
            except hil_lock.ReleaseRefused as e:
                return ['refused', str(e)]
        r = subprocess.run(['sudo', '-n', sys.executable, '-I', '-c', RELEASE_AS_ROOT, str(SCRIPT),
                            hil_lock.BOARD_LOCK_DIR, token], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_root_rechecks_the_receipts_as_the_holder(self):
        for older in (False, True):
            with self.subTest(holder='older farm check' if older else 'hil_lock'):
                receipt, done = make_receipt(self, self.states / f'state{int(older)}', self.holder_uid)
                token, pid = self.start_holder([receipt], older)
                verdict, why = self.release_as_root(token)
                self.assertEqual(verdict, 'refused')
                self.assertRegex(why, r'not proven ended: receipt t \(supervisor pid 4242\) pending$')
                done()
                self.assertEqual(self.release_as_root(token), ['released', [pid, [BOARD]]])
                self.assertEqual(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), 0)
                self.assertEqual(hil_lock.record_state(BOARD)[0], 'empty')


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
