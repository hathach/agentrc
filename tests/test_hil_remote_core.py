"""skills/hil/scripts/hil_remote_core.py is the remote HIL transport the project adapters share,
moved from tinyusb's .claude/skills/hil-contract/scripts/hil_remote.py (2c992c002) with its
transport tests (.claude/test/test_hil_remote.py, test/hil/test/test_hil_remote.py), plus the
create-only, detached attempts tinyuf2's adapter needs.

A local "rig": a fake ssh runs each remote command under sh with a clean environment and a temp
HOME, rsync (behind a logging shim) reaches its server through that ssh and the tree guard in
--rsync-path, and a fake python3 in that HOME's ~/.local/bin stands in for the harness. So the quoting, the tilde expansion, the PATH shim, the guards, the rig
helper and its detached supervisor execute for real. Windows cannot exec the #! fakes, so there
only the refusals and units run, and Windows mocks are not Windows evidence. Plumbing only: the
real transport, and a launch surviving a real ssh session's end, are proved by a run on a rig."""
import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'hil_remote_core.py'


def load():
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_remote_core', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc_ = load()
RemoteError = rc_.RemoteError

REAL_RSYNC = shutil.which('rsync')
RIG_PATH = '/usr/bin:/bin'
needs_rsync = unittest.skipUnless(REAL_RSYNC, 'rsync not installed; the fake rsync execs the real one')
needs_flock = unittest.skipUnless(shutil.which('flock', path=RIG_PATH), f'flock not on the fake rig PATH ({RIG_PATH})')
needs_rig_python = unittest.skipUnless(shutil.which('python3', path=RIG_PATH), f'no python3 on the fake rig PATH ({RIG_PATH})')
# the fake ssh, rsync and python3 are #! scripts Windows cannot exec: a run there would reach the real ones
needs_fake_rig = unittest.skipIf(os.name == 'nt', 'the fake rig is #! scripts and a POSIX shell')

# FAKE_SSH_DOWN: exit 255 without running a command containing it. FAKE_SSH_DROP: run a command
# containing it, then lose its output and exit 255, as a connection dropped before the reply.
FAKE_SSH = f'''#!{sys.executable}
import json, os, subprocess, sys
args = sys.argv[1:]
while args[:1] == ['-o']:
    args = args[2:]
# like ssh, the words after the host are one remote command line: rsync passes its server's as several
host, cmd = args[0], ' '.join(args[1:])
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({{'tool': 'ssh', 'argv': sys.argv[1:]}}) + '\\n')
down, drop = os.environ.get('FAKE_SSH_DOWN'), os.environ.get('FAKE_SSH_DROP')
if down and down in cmd:
    sys.exit(255)
prelude = os.environ.get('FAKE_FETCH_PRELUDE')
if prelude and "' fetch " in cmd:
    cmd = cmd.replace("python3 -c 'TOKEN_FILE", "python3 -c '" + open(prelude).read() + "TOKEN_FILE", 1)
canned = os.environ.get('FAKE_FETCH_TAR')
if canned and "' fetch " in cmd:
    sys.stdout.buffer.write(open(canned, 'rb').read())
    sys.exit(0)
env = {{'HOME': os.environ['FAKE_HOME'], 'PATH': {RIG_PATH!r}}}
env.update({{k: v for k, v in os.environ.items() if k.startswith('FAKE_')}})
if drop and drop in cmd:
    subprocess.run(['sh', '-c', cmd], env=env, stdin=sys.stdin, stdout=subprocess.DEVNULL)
    sys.exit(255)
sys.exit(subprocess.run(['sh', '-c', cmd], env=env, stdin=sys.stdin).returncode)
'''

FAKE_RSYNC = f'''#!{sys.executable}
import json, os, sys
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({{'tool': 'rsync', 'argv': sys.argv[1:]}}) + '\\n')
args = [*sys.argv[1:]]
os.execv({REAL_RSYNC!r}, [{REAL_RSYNC!r}, *args])
'''

# The harness on the "rig": record argv, cwd, the HIL_* env it got and whether another run could
# take the wiped tree's lock; write FAKE_WRITE's files, sleep FAKE_SLEEP, write FAKE_LATE's and
# exit with FAKE_RC.
FAKE_PYTHON = f'''#!{sys.executable}
import fcntl, json, os, sys, time
with open(os.getcwd() + '.lock', 'a') as lk:
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_held = False
    except BlockingIOError:
        lock_held = True
    # the run session's own shared hold: fd 9 open on the lock file across the exec
    try:
        own_hold = os.fstat(9).st_ino == os.fstat(lk.fileno()).st_ino
    except OSError:
        own_hold = False
with open(os.environ['FAKE_RUN'], 'w') as f:
    json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'lock_held': lock_held, 'own_hold': own_hold,
               'env': {{k: v for k, v in os.environ.items() if k.startswith('HIL_')}}}}, f)


def write(names):
    for name in names.split():
        os.makedirs(os.path.dirname(name) or '.', exist_ok=True)
        open(name, 'w').write('from the rig')


write(os.environ.get('FAKE_WRITE', ''))
time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))
write(os.environ.get('FAKE_LATE', ''))
sys.exit(int(os.environ.get('FAKE_RC', '0')))
'''


# Prepended to the rig helper: its launcher is SIGKILLed right after the fork, and the forked
# supervisor goes on only once that launcher is dead -- before any acknowledgement.
LAUNCHER_DIES_IN_HANDSHAKE = '''
import os as _os, signal as _signal, time as _time
_fork = _os.fork


def _dying_fork():
    launcher = _os.getpid()
    pid = _fork()
    if pid:
        _os.kill(launcher, _signal.SIGKILL)
    else:
        while _os.getppid() == launcher:
            _time.sleep(0.01)
    return pid


_os.fork = _dying_fork
'''


def stream(*entries):
    """A fetch stream: (name, bytes) a file, (name, None) a directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w|') as tar:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type = tarfile.DIRTYPE
                tar.addfile(ti)
            else:
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def status_entry(**outcomes):
    return (rc_.FETCH_RESULT, json.dumps(outcomes).encode())


# Prepended to the rig helper: the launcher stops right after it created its claim, until
# FAKE_RESUME exists, having announced the pause by creating FAKE_PAUSED.
LAUNCHER_PAUSES_AFTER_ITS_CLAIM = '''
import os as _os, time as _time
_open = _os.open


def _pausing_open(path, *a, **k):
    fd = _open(path, *a, **k)
    if str(path).endswith('launch.claim'):
        _os.close(_open(_os.environ['FAKE_PAUSED'], _os.O_WRONLY | _os.O_CREAT, 0o644))
        while not _os.path.exists(_os.environ['FAKE_RESUME']):
            _time.sleep(0.02)
    return fd


_os.open = _pausing_open
'''


# Prepended to the rig helper's fetch (single-quote free: it is spliced into the quoted program):
# a file named b.json is deleted just before the walk opens it, after its tree was scanned.
B_JSON_VANISHES = """
import os as _o
_open = _o.open


def _vanishing(path, flags, mode=0o777, *, dir_fd=None):
    if dir_fd is not None and path == "b.json":
        _o.unlink(path, dir_fd=dir_fd)
    return _open(path, flags, mode, dir_fd=dir_fd)


_o.open = _vanishing
"""


def fake(path, body):
    path.write_text(body)
    path.chmod(0o755)


@contextlib.contextmanager
def captured_fd2():
    """What child processes write to fd 2 meanwhile, as a str in the yielded list."""
    out = []
    with tempfile.TemporaryFile() as f:
        saved = os.dup(2)
        os.dup2(f.fileno(), 2)
        try:
            yield out
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            f.seek(0)
            out.append(f.read().decode(errors='replace'))


def setUpModule():
    guard_survivors(__name__)


class Rig(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        self.root = self.tmp / 'checkout'
        self.root.mkdir()
        self.home = self.tmp / 'home'
        (self.home / '.local/bin').mkdir(parents=True)
        fake(self.home / '.local/bin/python3', FAKE_PYTHON)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        fake(self.bin / 'ssh', FAKE_SSH)
        fake(self.bin / 'rsync', FAKE_RSYNC)
        self.remote = self.tmp / 'remote'
        self.log = self.tmp / 'log.jsonl'
        self.run_file = self.tmp / 'run.json'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('HIL_', 'FAKE_'))}
        env.update(PATH=f'{self.bin}{os.pathsep}{os.environ["PATH"]}', FAKE_LOG=str(self.log),
                   FAKE_HOME=str(self.home), FAKE_RUN=str(self.run_file))
        if os.name == 'nt':
            env['PATH'] = str(self.bin)   # only the refusals run here: no real ssh or rsync on PATH
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def setenv(self, **env):
        os.environ.update(env)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text().splitlines()]

    def ran(self):
        return json.loads(self.run_file.read_text())

    def remote_files(self, under=None):
        under = under or self.remote
        return sorted(p.relative_to(under).as_posix() for p in under.rglob('*') if p.is_file())

    def checkout(self, *rels):
        for rel in rels:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(rel)

    def sync_run(self, argv=('python3', '-u', 'run.py'), env=None, files=('run.py',), groups=(), optional=(),
                 remote_dir=None, mkdirs=('sub',)):
        """An adapter's synchronous run over the core: wipe-and-lease, stage, run, copy back.
        (exit status, copied, incomplete)."""
        self.checkout(*files)
        with rc_.remote_lease('rig', remote_dir or str(self.remote), self.root, mkdirs) as tree:
            if files and rc_.rsync(tree, '-aqR', *files, tree.spec('.') + '/', staging=True) != 0:
                rc_.fail('could not stage the harness')
            status = rc_.run_remote(tree, list(argv), env)
            if status == rc_.TREE_REPLACED:
                return status, [], []
            self.got = rc_.copy_back(tree, self.root, groups, optional)
            return (status, *self.got)


# a remote dir the guards accept, on any client OS; these tests never reach it
SAFE_DIR = '/tmp/hil-remote-test'


class Refusals(Rig):
    """Every refusal happens before any ssh or rsync: a wipe followed by an abort leaves the rig
    without the previous run's artifacts."""

    def refused(self, fn, text):
        with self.assertRaises(RemoteError) as e:
            fn()
        self.assertIn(text, str(e.exception.code))
        self.assertTrue(str(e.exception.code).startswith('error: '))
        self.assertEqual(self.calls(), [])

    def test_unsafe_remote_dirs(self):
        for bad in ('/', '~/', '~', 'rel/dir', '/tmp/x/', '/tmp/../etc', '/tmp//x', '/tmp/$(id)',
                    '/tmp/a b', '/tmp/x;rm'):
            with self.subTest(bad):
                self.refused(lambda: rc_.remote_lease('rig', bad, self.root).__enter__(), 'REMOTE_DIR')
                self.refused(lambda: rc_.create_attempt(self.root, self.tmp / 'runs', 'a1', 'rig', bad),
                             'the attempt base')
        self.assertFalse((self.tmp / 'runs').exists(), 'a refused attempt spends no run id')

    def test_unsafe_relative_paths(self):
        for bad in ('/abs', '../up', 'a/../b', 'a b', '', 'a;b', '$(id)'):
            with self.subTest(bad):
                self.refused(lambda: rc_.check_relative(bad, '-B'), '-B must be a relative path')
                self.refused(lambda: rc_.remote_lease('rig', SAFE_DIR, self.root, [bad]).__enter__(), 'a staging dir')
        self.assertEqual(rc_.check_relative('out//hil/', '-B'), 'out/hil')

    def test_the_trees_own_records_are_not_a_write_target(self):
        for bad in ('.attempt', '.attempt/launch.json', '.hil-remote-run'):
            with self.subTest(bad):
                self.refused(lambda: rc_.remote_lease('rig', SAFE_DIR, self.root, [bad]).__enter__(),
                             "the tree's own records")
                self.refused(lambda: rc_.create_attempt(self.root, self.tmp / 'runs', 'a1', 'rig', SAFE_DIR, [bad]),
                             "the tree's own records")

    def test_unusable_run_ids(self):
        for bad in ('', '.hidden', '../x', 'a/b', 'x' * 65):
            with self.subTest(bad):
                self.refused(lambda: rc_.create_attempt(self.root, self.tmp / 'runs', bad, 'rig', SAFE_DIR), 'a run id is')
                self.refused(lambda: rc_.run_paths(self.tmp / 'runs', bad), 'a run id is')

    def test_argv_and_env_a_remote_shell_would_misread(self):
        tree = rc_.Tree('rig', '/tmp/t', '/tmp/t.lock', 'tok', self.root)
        self.refused(lambda: rc_.run_command(tree, []), 'non-empty list')
        self.refused(lambda: rc_.run_command(tree, ['A=b', 'x']), 'no "="')
        self.refused(lambda: rc_.run_command(tree, ['x'], {'A B': '1'}), 'not environment variable names')
        self.refused(lambda: rc_.run_command(tree, ['x'], cwd='../up'), 'a run dir')

    def test_artifact_paths_outside_the_tree(self):
        tree = rc_.Tree('rig', '/tmp/t', '/tmp/t.lock', 'tok', self.root)
        for bad in ('../x', '/etc/passwd', 'a/../../b'):
            with self.subTest(bad):
                self.refused(lambda: rc_.copy_back(tree, self.root, [('ok.json', bad)]), 'an artifact path')
                self.refused(lambda: rc_.copy_back(tree, self.root, optional=[bad]), 'an artifact path')

    def test_no_rsync_is_refused_before_the_lease(self):
        def popen(*a, **k):
            raise AssertionError('ssh started')
        cases = [('none', None, None)]
        if os.name == 'nt':   # subprocess cannot start a .cmd by bare name, and the cwd is not PATH
            cases += [('a .cmd on PATH', 'path', 'rsync.cmd'), ('an .exe in the cwd', 'cwd', 'rsync.exe')]
        for case, where, name in cases:
            with self.subTest(case), tempfile.TemporaryDirectory() as path, tempfile.TemporaryDirectory() as cwd:
                if name:
                    (Path(path if where == 'path' else cwd) / name).write_bytes(b'')
                old = os.getcwd()
                os.chdir(cwd)
                try:
                    with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(['', path])}), \
                            mock.patch.object(rc_.subprocess, 'Popen', popen), \
                            mock.patch.object(rc_.subprocess, 'run', side_effect=AssertionError('ssh ran')):
                        for call in (lambda: rc_.remote_lease('rig', '/tmp/x', self.root).__enter__(),
                                     lambda: rc_.create_attempt(self.root, Path(cwd) / 'runs', 'a1', 'rig', '/tmp/x')):
                            with self.assertRaises(RemoteError) as e:
                                call()
                            self.assertIn('rsync is not on PATH', str(e.exception.code))
                finally:
                    os.chdir(old)


@needs_fake_rig
@needs_rsync
@needs_flock
@needs_rig_python
class WipedTree(Rig):
    def test_what_reaches_the_rig(self):
        status, copied, incomplete = self.sync_run(files=('run.py', 'lib/a.py', 'fw/x.elf'), mkdirs=('lib', 'fw', 'out/hil'))
        self.assertEqual((status, copied, incomplete), (0, [], []))
        self.assertEqual(self.remote_files(), ['.hil-remote-run', 'fw/x.elf', 'lib/a.py', 'run.py'])
        self.assertTrue((self.remote / 'out/hil').is_dir())

    def test_the_previous_tree_is_wiped(self):
        (self.remote / 'stale').mkdir(parents=True)
        (self.remote / 'stale/x').write_text('old')
        self.assertEqual(self.sync_run()[0], 0)
        self.assertFalse((self.remote / 'stale').exists())

    def test_args_and_env_survive_transport(self):
        status, _, _ = self.sync_run(argv=('python3', '-u', 'run.py', '-t', 'host/cdc msc', '-r', '3'),
                                     env={'HIL_POOL_TIMEOUT': '60', 'HIL_ODD': "a b'c $HOME"})
        self.assertEqual(status, 0)
        run = self.ran()
        self.assertEqual(run['argv'], ['-u', 'run.py', '-t', 'host/cdc msc', '-r', '3'])
        self.assertEqual(run['env'], {'HIL_POOL_TIMEOUT': '60', 'HIL_ODD': "a b'c $HOME"})
        self.assertEqual(Path(run['cwd']), self.remote)

    def test_nothing_is_forwarded_but_the_env_given(self):
        self.setenv(HIL_LOCAL_ONLY='1')
        self.assertEqual(self.sync_run()[0], 0)
        self.assertEqual(self.ran()['env'], {})

    def test_a_run_dir_inside_the_tree(self):
        self.assertEqual(self.sync_run(argv=('python3', '../run.py'), mkdirs=('sub',), env={}, files=('run.py',))[0], 0)
        self.assertEqual(Path(self.ran()['cwd']), self.remote)
        with rc_.remote_lease('rig', str(self.remote), self.root, ['sub']) as tree:
            self.assertEqual(rc_.run_remote(tree, ['python3', 'x'], cwd='sub'), 0)
        self.assertEqual(Path(self.ran()['cwd']), self.remote / 'sub')

    def test_the_exit_code_passes_through(self):
        self.setenv(FAKE_RC='3')
        self.assertEqual(self.sync_run()[0], 3)

    def test_a_tilde_remote_dir_lands_in_home(self):
        self.assertEqual(self.sync_run(remote_dir='~/hil-run')[0], 0)
        self.assertEqual(Path(self.ran()['cwd']), self.home / 'hil-run')
        self.assertTrue((self.home / 'hil-run/run.py').is_file())
        self.assertFalse(any(p.name == '~' for p in self.tmp.rglob('~')))
        rsync_dests = [c['argv'][-1] for c in self.calls() if c['tool'] == 'rsync']
        self.assertTrue(all(d.startswith(f'rig:{self.home}/hil-run') or not d.startswith('rig:')
                            for d in rsync_dests), rsync_dests)

    def test_the_remote_gate_refuses_home(self):
        for home in ('~/', str(self.home), '/'):
            with self.subTest(home):
                r = subprocess.run(['bash', '-c', rc_.SETUP_SCRIPT, 'hil-setup', home, 'tok'],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   env={'HOME': str(self.home), 'PATH': RIG_PATH})
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('refusing', r.stderr)
                self.assertTrue((self.home / '.local/bin/python3').exists())

    def test_every_ssh_carries_the_keepalive_options(self):
        self.setenv(FAKE_WRITE='report.json')
        self.assertEqual(self.sync_run(groups=[('report.json',)])[0], 0)
        opts = list(rc_.SSH_OPTS)
        ssh = [c['argv'] for c in self.calls() if c['tool'] == 'ssh']
        rsync = [c['argv'] for c in self.calls() if c['tool'] == 'rsync']
        self.assertEqual([a[:len(opts) + 1] for a in ssh], [[*opts, 'rig']] * 4,
                         'the lease, the staging rsync server, the run, the fetch')
        self.assertEqual(sum('--server' in ' '.join(a) for a in ssh), 1)
        self.assertEqual(len(rsync), 1)
        self.assertTrue(all(a[:2] == ['-e', shlex.join(['ssh', *opts])] for a in rsync), rsync)
        # every remote rsync server and the fetch start under the tree guard: shared lock plus this run's token
        token = (self.remote / rc_.TOKEN_FILE).read_text().strip()
        self.assertTrue(all(a[2].startswith('--rsync-path=sh -c ') and 'flock -s' in a[2] and token in a[2]
                            for a in rsync), rsync)
        fetch = [a[-1] for a in ssh if "' fetch " in a[-1]]
        self.assertEqual(len(fetch), 1)
        self.assertTrue(fetch[0].startswith('{ exec 9>>') and 'flock -s' in fetch[0] and token in fetch[0], fetch)


@needs_fake_rig
@needs_flock
@needs_rig_python
class RemoteLock(Rig):
    """A second run sharing a wiped tree's dir must not rm -rf the tree the first one runs from."""

    def lock(self, path=None):
        import fcntl
        lk = open(path or f'{self.remote}.lock', 'a')
        self.addCleanup(lk.close)
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_held_lock_refuses_before_the_wipe(self):
        (self.remote / 'running').mkdir(parents=True)
        (self.remote / 'running/x').write_text('the other run')
        self.lock()
        with self.assertRaises(RemoteError) as e:
            self.sync_run()
        self.assertIn(f'another hil_remote run holds {self.remote}.lock', e.exception.code)
        self.assertTrue((self.remote / 'running/x').is_file())
        self.assertEqual([c['tool'] for c in self.calls()], ['ssh'])

    def test_a_failing_flock_is_not_reported_as_a_held_lock(self):
        rig_bin = self.tmp / 'rig-bin'
        rig_bin.mkdir()
        fake(rig_bin / 'flock', '#!/bin/sh\necho "flock: 9: No locks available" >&2\nexit 71\n')
        fake(self.bin / 'ssh', FAKE_SSH.replace(f"'PATH': {RIG_PATH!r}", f"'PATH': '{rig_bin}:{RIG_PATH}'"))
        (self.remote / 'running').mkdir(parents=True)
        with self.assertRaises(RemoteError) as e:
            self.sync_run()
        self.assertIn(f'flock on {self.remote}.lock failed (exit 71)', e.exception.code)
        self.assertNotIn('another hil_remote run holds', e.exception.code)
        self.assertTrue((self.remote / 'running').is_dir())

    @needs_rsync
    def test_the_lock_is_held_for_the_run_and_released_after(self):
        self.assertEqual(self.sync_run()[0], 0)
        self.assertTrue(self.ran()['lock_held'])
        self.assertTrue(self.ran()['own_hold'], 'the command must hold the lock itself, not only the lease')
        self.lock()

    @needs_rsync
    def test_a_failed_staging_releases_the_lock(self):
        fake(self.bin / 'rsync', FAKE_RSYNC.replace('args = [', "if 'run.py' in sys.argv:\n    sys.exit(1)\nargs = [", 1))
        # a lease that outlives its stdin by a second: the wrapper must wait it out, not exit holding the lock
        fake(self.bin / 'ssh', FAKE_SSH.replace("subprocess.run(['sh', '-c', cmd], env=env, stdin=sys.stdin).returncode",
                                                "subprocess.run(['sh', '-c', cmd.replace('cat >/dev/null', "
                                                "'cat >/dev/null; sleep 1')], env=env, stdin=sys.stdin).returncode"))
        with self.assertRaises(RemoteError) as e:
            self.sync_run()
        self.assertIn('could not stage the harness', e.exception.code)
        self.lock()

    @needs_rsync
    def test_the_lock_is_held_for_the_copy_back(self):
        seen = self.tmp / 'copy_back_lock'
        # inside the fetch session, after its guard: can a wiper take the lock now?
        probe = f'(flock -n -x {self.remote}.lock true && echo free || echo held) >{seen}; '
        fake(self.bin / 'ssh', FAKE_SSH.replace(
            "['sh', '-c', cmd]", f"['sh', '-c', cmd.replace('|| exit 200; exec python3', '|| exit 200; {probe}exec python3')]"))
        self.setenv(FAKE_WRITE='report.md report.json')
        status, copied, _ = self.sync_run(groups=[('report.md', 'report.json')])
        self.assertEqual((status, copied), (0, ['report.md', 'report.json']))
        self.assertEqual(seen.read_text().strip(), 'held')

    @needs_rsync
    def test_a_noisy_rig_shell_does_not_hide_the_marker(self):
        # a login shell greeting on stdout with no trailing newline
        # rsync's own protocol needs a clean shell, so only the lease session greets
        fake(self.bin / 'ssh', FAKE_SSH.replace("['sh', '-c', cmd]", "['sh', '-c', ('printf greeting; ' if 'hil-setup' in cmd "
                                                "else '') + cmd]"))
        self.assertEqual(self.sync_run()[0], 0)

    @needs_rsync
    def test_a_chatty_setup_stderr_does_not_deadlock(self):
        fake(self.bin / 'ssh', FAKE_SSH.replace("['sh', '-c', cmd]",
                                                "['sh', '-c', 'head -c 300000 /dev/zero | tr \\\\0 x >&2; ' + cmd]"))
        with captured_fd2():
            self.assertEqual(self.sync_run()[0], 0)

    @needs_rsync
    def test_a_tree_replaced_after_staging_is_refused(self):
        # the lease died and another run wiped and rebuilt the tree before this run session took
        # its hold: its token is gone, so it must not run in the replacement
        fake(self.bin / 'ssh', FAKE_SSH.replace(
            "['sh', '-c', cmd]",
            "['sh', '-c', ('rm -rf \"$0\" && mkdir -p \"$0\"; ' if 'run.py' in cmd and 'exec env' in cmd else '') + cmd, "
            f"{str(self.remote)!r}]"))
        with captured_fd2() as err:
            status, copied, _ = self.sync_run(groups=[('report.json',)])
        self.assertEqual(status, rc_.TREE_REPLACED)
        self.assertIn("not this run's tree any more", err[0])
        self.assertFalse(self.run_file.exists(), "the command ran in a tree that is not this run's")
        self.assertEqual([c['tool'] for c in self.calls()][-1], 'ssh', 'copy_back ran after the refusal')

    @needs_rsync
    def test_a_transfer_into_a_replaced_tree_is_refused(self):
        self.checkout('run.py')
        with rc_.remote_lease('rig', str(self.remote), self.root) as tree:
            (self.remote / rc_.TOKEN_FILE).write_text('another run\n')
            with captured_fd2():
                self.assertNotEqual(rc_.rsync(tree, '-q', self.root / 'run.py', tree.spec('.') + '/'), 0)
                self.assertEqual(rc_.copy_back(tree, self.root, optional=['run.py']), ([], []))
        self.assertFalse((self.remote / 'run.py').exists())


@needs_fake_rig
@needs_rsync
@needs_flock
@needs_rig_python
class CopyBack(Rig):
    def test_a_group_and_optional_files_come_home(self):
        self.setenv(FAKE_WRITE='report.md report.json cfg.json.failed')
        status, copied, incomplete = self.sync_run(groups=[('report.md', 'report.json')], optional=['cfg.json.failed'])
        self.assertEqual((status, copied, incomplete), (0, ['report.md', 'report.json', 'cfg.json.failed'], []))
        for n in copied:
            self.assertEqual((self.root / n).read_text(), 'from the rig', n)

    def test_an_absent_optional_file_is_quiet_and_its_stale_copy_goes(self):
        (self.root / 'cfg.json.failed').write_text('stale')
        self.setenv(FAKE_WRITE='report.md report.json')
        with captured_fd2() as err:
            _, copied, _ = self.sync_run(groups=[('report.md', 'report.json')], optional=['cfg.json.failed'])
        self.assertEqual(copied, ['report.md', 'report.json'])
        self.assertEqual(err[0], '')
        self.assertFalse((self.root / 'cfg.json.failed').exists(), 'a stale spec would re-flash boards that passed')

    def test_half_a_group_leaves_none(self):
        for n in ('report.md', 'report.json', 'cfg.json.failed'):
            (self.root / n).write_text('stale')
        self.setenv(FAKE_WRITE='report.md')
        status, copied, incomplete = self.sync_run(groups=[('report.md', 'report.json')], optional=['cfg.json.failed'])
        self.assertEqual((status, copied, incomplete), (0, [], [['report.md', 'report.json']]))
        for n in ('report.md', 'report.json', 'report.md.tmp', 'cfg.json.failed'):
            self.assertFalse((self.root / n).exists(), n)

    def test_a_directory_tree_comes_home_whole_and_without_links(self):
        self.setenv(FAKE_WRITE='bundle/a.json bundle/deep/b.log report.json')
        (self.root / 'bundle').mkdir()
        (self.root / 'bundle/stale').write_text('from the last attempt')
        fake(self.bin / 'ssh', FAKE_SSH.replace(
            "['sh', '-c', cmd]", "['sh', '-c', ('mkdir -p \"$0/bundle\"; ln -s /etc \"$0/bundle/etc\"; ' if 'exec env' in cmd else '') + cmd, "
                                 f"{str(self.remote)!r}]"))
        status, copied, incomplete = self.sync_run(groups=[('bundle/', 'report.json')])
        self.assertTrue((self.remote / 'bundle/etc').is_symlink())
        self.assertEqual((copied, incomplete), (['bundle/', 'report.json'], []))
        self.assertEqual(self.remote_files(self.root / 'bundle'), ['a.json', 'deep/b.log'])
        self.assertFalse(any(p.is_symlink() for p in (self.root / 'bundle').rglob('*')), 'a link came home')
        self.assertFalse(any(p.name.startswith('.hil-remote-fetch') for p in self.root.iterdir()))

    def test_a_tree_that_is_a_link_or_missing_is_an_incomplete_group(self):
        (self.remote.parent / 'elsewhere').mkdir()
        (self.remote.parent / 'elsewhere/secret').write_text('not the tree')
        fake(self.bin / 'ssh', FAKE_SSH.replace(
            "['sh', '-c', cmd]", "['sh', '-c', ('ln -s ../elsewhere \"$0/bundle\"; ' if 'exec env' in cmd else '') + cmd, "
                                 f"{str(self.remote)!r}]"))
        self.setenv(FAKE_WRITE='report.json')
        _, copied, incomplete = self.sync_run(groups=[('bundle/', 'report.json'), ('missing/',)])
        self.assertTrue((self.remote / 'bundle').is_symlink())
        self.assertEqual((copied, incomplete), ([], [['bundle/', 'report.json'], ['missing/']]))
        self.assertFalse((self.root / 'bundle').exists() or (self.root / 'report.json').exists())

    def outside(self):
        """A dir beside the tree with a report and a tree in it, and `escape` in the tree linking to it."""
        out = self.remote.parent / 'outside'
        (out / 'sub').mkdir(parents=True)
        for rel in ('report.json', 'opt.log', 'sub/x.log'):
            (out / rel).write_text('not the tree')
        fake(self.bin / 'ssh', FAKE_SSH.replace(
            "['sh', '-c', cmd]", "['sh', '-c', ('mkdir -p \"$0/bundle\"; ln -s ../outside \"$0/escape\"; '"
                                 "'ln -s ../../outside \"$0/bundle/link\"; ' if 'exec env' in cmd else '') + cmd, "
                                 f"{str(self.remote)!r}]"))

    def test_nothing_is_read_through_a_symlinked_parent(self):
        self.outside()
        self.setenv(FAKE_WRITE='report.json bundle/in.log')
        _, copied, incomplete = self.sync_run(groups=[('escape/report.json',), ('escape/sub/',), ('report.json',)],
                                              optional=['escape/opt.log'])
        self.assertTrue((self.remote / 'escape').is_symlink() and (self.remote / 'escape/report.json').is_file())
        self.assertEqual((copied, incomplete), (['report.json'], [['escape/report.json'], ['escape/sub/']]))
        self.assertFalse((self.root / 'escape').exists())

    def test_a_link_inside_a_fetched_tree_is_not_followed(self):
        self.outside()
        self.setenv(FAKE_WRITE='bundle/in.log')
        _, copied, incomplete = self.sync_run(groups=[('bundle/',)])
        self.assertTrue((self.remote / 'bundle/link/sub/x.log').is_file(), 'the link resolves on the rig')
        self.assertEqual((copied, incomplete), (['bundle/'], []))
        self.assertEqual(self.remote_files(self.root / 'bundle'), ['in.log'])

    def test_a_local_destination_parent_that_is_a_link_refuses_before_the_fetch(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        (self.root / 'out').symlink_to(elsewhere)
        self.setenv(FAKE_WRITE='out/report.json')
        for groups, optional in (([('out/report.json',)], ()), ((), ['out/report.json']), ([('out/run/',)], ())):
            with self.subTest(groups=groups, optional=optional), self.assertRaises(RemoteError) as e:
                self.sync_run(groups=groups, optional=optional, mkdirs=('out',))
            self.assertIn('is not a directory inside', e.exception.code)
            self.assertFalse([c for c in self.calls() if "' fetch " in c['argv'][-1]], 'fetched anyway')
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_an_optional_tree_comes_home_when_present(self):
        self.setenv(FAKE_WRITE='report.json report.attempts/a1/report.json report.attempts/a1/report.md')
        status, copied, incomplete = self.sync_run(groups=[('report.json',)], optional=['report.attempts/', 'x.failed'])
        self.assertEqual((status, copied, incomplete), (0, ['report.json', 'report.attempts/'], []))
        self.assertEqual(self.got.outcomes, {'report.json': 'ok', 'report.attempts/': 'ok', 'x.failed': 'absent'})
        self.assertEqual(self.remote_files(self.root / 'report.attempts'), ['a1/report.json', 'a1/report.md'])

    def test_an_absent_optional_tree_leaves_the_group_complete(self):
        (self.root / 'report.attempts/old').mkdir(parents=True)
        (self.root / 'report.attempts/old/report.json').write_text('a stale local archive')
        self.setenv(FAKE_WRITE='report.json')
        status, copied, incomplete = self.sync_run(groups=[('report.json',)], optional=['report.attempts/'])
        self.assertEqual((copied, incomplete), (['report.json'], []))
        self.assertEqual(self.got.outcomes['report.attempts/'], 'absent')
        self.assertFalse((self.root / 'report.attempts').exists(), 'a stale local tree is removed, never merged')

    def test_a_failed_fetch_is_not_an_absent_member(self):
        self.setenv(FAKE_WRITE='report.json report.attempts/a1/report.json')
        for case, env in (('the ssh session', {'FAKE_SSH_DOWN': "' fetch "}),
                          ('the stream', {'FAKE_FETCH_TAR': str(self.tmp / 'canned.tar')})):
            with self.subTest(case):
                (self.tmp / 'canned.tar').write_bytes(stream((rc_.FETCH_RESULT, None)))
                self.setenv(**env)
                _, copied, incomplete = self.sync_run(groups=[('report.json',)], optional=['report.attempts/'])
                self.assertEqual((copied, incomplete), ([], [['report.json']]))
                self.assertEqual(self.got.outcomes, {'report.json': 'transport', 'report.attempts/': 'transport'})
                for k in env:
                    del os.environ[k]

    def test_a_symlinked_optional_tree_is_refused(self):
        self.outside()
        fake(self.bin / 'ssh', FAKE_SSH.replace(
            "['sh', '-c', cmd]", "['sh', '-c', ('ln -s ../outside \"$0/report.attempts\"; ' if 'exec env' in cmd else '') + cmd, "
                                 f"{str(self.remote)!r}]"))
        self.setenv(FAKE_WRITE='report.json')
        _, copied, incomplete = self.sync_run(groups=[('report.json',)], optional=['report.attempts/'])
        self.assertTrue((self.remote / 'report.attempts/sub/x.log').is_file(), 'the link resolves on the rig')
        self.assertEqual((copied, incomplete, self.got.outcomes['report.attempts/']), (['report.json'], [], 'unsafe'))
        self.assertFalse((self.root / 'report.attempts').exists())

    def test_a_malformed_stream_publishes_nothing(self):
        canned = self.tmp / 'canned.tar'
        for case, data in (('a directory named report.json', stream(('report.json', None), status_entry(**{'report.json': 'ok'}))),
                           ('a directory status entry', stream((rc_.FETCH_RESULT, None))),
                           ('ok without the artifact', stream(status_entry(**{'report.json': 'ok', 'report.md': 'ok'})))):
            with self.subTest(case):
                (self.root / 'report.md').write_text('stale')
                canned.write_bytes(data)
                self.setenv(FAKE_FETCH_TAR=str(canned))
                status, copied, incomplete = self.sync_run(groups=[('report.md', 'report.json')])
                self.assertEqual((status, copied, incomplete), (0, [], [['report.md', 'report.json']]))
                self.assertFalse((self.root / 'report.json').exists() or (self.root / 'report.md').exists(),
                                 'a stale pair is removed and nothing new published')
                self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.hil-remote-fetch')], [])

    def test_overlapping_artifacts_are_refused(self):
        tree = rc_.Tree('rig', '/tmp/t', '/tmp/t.lock', 'tok', self.root)
        with self.assertRaises(RemoteError) as e:
            rc_.copy_back(tree, self.root, [('run/',)], ['run/report.json'])
        self.assertIn('is inside the tree run/', e.exception.code)

    def test_a_member_of_two_groups_or_a_group_and_optional_is_refused(self):
        tree = rc_.Tree('rig', '/tmp/t', '/tmp/t.lock', 'tok', self.root)
        for groups, optional in (([('report.json', 'report.md')], ['report.json']),
                                 ([('report.json', 'report.md'), ('report.json', 'cfg.json')], [])):
            with self.subTest(groups=groups, optional=optional), self.assertRaises(RemoteError) as e:
                rc_.copy_back(tree, self.root, groups, optional)
            self.assertIn('report.json', e.exception.code)
            self.assertIn('a group lands all or none', e.exception.code)
        self.assertEqual(self.calls(), [], 'refused before the rig')


class Units(unittest.TestCase):
    def test_snapshot_pins_the_bytes_it_copied(self):
        with tempfile.TemporaryDirectory() as td:
            root, into = Path(td) / 'checkout', Path(td) / 'snap'
            fw = root / 'cmake-build/cmake-build-a/device/cdc/cdc.elf'
            fw.parent.mkdir(parents=True)
            fw.write_text('built')
            pinned = rc_.file_digests(root, [fw])
            self.assertEqual(list(pinned), ['cmake-build/cmake-build-a/device/cdc/cdc.elf'])
            self.assertEqual(rc_.snapshot(root, [fw], into), pinned)
            fw.write_text('rebuilt after the check')
            self.assertEqual(rc_.file_digests(into, list(into.rglob('*.elf'))), pinned, 'the snapshot is what was checked')
            with self.assertRaises(RemoteError) as e:
                rc_.snapshot(root, [Path(td) / 'outside.elf'], into)
            self.assertIn('is not under', e.exception.code)

    def test_differing_names_rebuilt_and_removed_files(self):
        pinned = {'d/a/x.elf': '1', 'd/a/flash_args': '2', 'd/b/y.elf': '3', 'other/z.elf': '4'}
        staged = {'d/a/x.elf': 'rebuilt', 'd/b/y.elf': '3', 'd/c/new.elf': '5'}
        self.assertEqual(rc_.differing(pinned, staged, ['d/a/', 'd/b/']), ['d/a/flash_args', 'd/a/x.elf', 'd/c/new.elf'])
        self.assertEqual(rc_.differing(pinned, staged), ['d/a/x.elf', 'd/c/new.elf'])

    def test_windows_takes_rsync_exe_only_from_an_absolute_path_dir(self):
        # the os module as Windows' rsync_program sees it, on any host
        with tempfile.TemporaryDirectory() as path, tempfile.TemporaryDirectory() as cwd:
            for d in (path, cwd):
                (Path(d) / 'rsync.exe').write_bytes(b'')
            old = os.getcwd()
            os.chdir(cwd)
            try:
                for entries, found in ((['.', 'bin', ''], None), (['.', path], os.path.join(path, 'rsync.exe'))):
                    nt = types.SimpleNamespace(name='nt', path=os.path, pathsep=os.pathsep,
                                               environ={'PATH': os.pathsep.join(entries)})
                    with self.subTest(entries), mock.patch.object(rc_, 'os', nt):
                        self.assertEqual(rc_.rsync_program(), found)
            finally:
                os.chdir(old)

    @unittest.skipUnless(os.name == 'nt', 'rsync.exe is the Windows name')
    def test_windows_runs_the_rsync_exe_found_on_path(self):
        calls = []
        with tempfile.TemporaryDirectory() as path, tempfile.TemporaryDirectory() as root:
            (Path(path) / 'rsync.exe').write_bytes(b'')
            tree = rc_.Tree('rig', '/tmp/t', '/tmp/t.lock', 'tok', Path(root))
            with mock.patch.dict(os.environ, {'PATH': path}), \
                    mock.patch.object(rc_.subprocess, 'run', lambda argv, **kw: calls.append(argv) or mock.Mock(returncode=0)):
                rc_.rsync(tree, '-q', 'rig:/tmp/t/x', Path(root) / 'x')
        self.assertEqual(calls[0][0], os.path.join(path, 'rsync.exe'))

    def test_rsync_names_local_files_from_the_checkout_and_passes_remote_specs_as_they_are(self):
        # a one-letter ssh host must not be read as a drive letter, the checkout's own or another
        calls = []
        root = Path(tempfile.gettempdir()).resolve()
        local = root / 'hil_report.json'
        hosts = ('rig', root.drive[:1].lower() or 'e', 'r')
        with mock.patch.object(rc_, 'rsync_program', return_value='rsync'), \
                mock.patch.object(rc_.subprocess, 'run', lambda argv, **kw: calls.append((argv, kw)) or mock.Mock(returncode=0)):
            for host in hosts:
                tree = rc_.Tree(host, '/tmp/t', '/tmp/t.lock', 'tok', root)
                rc_.rsync(tree, '-q', local, f'{host}:/tmp/t/')
                rc_.rsync(tree, '-q', f'{host}:/tmp/t/hil_report.json', local)
        name = 'hil_report.json' if os.name == 'nt' else str(local)   # Windows rsync reads C: as a host
        self.assertEqual([argv[-2:] for argv, _ in calls],
                         [x for h in hosts for x in ([name, f'{h}:/tmp/t/'], [f'{h}:/tmp/t/hil_report.json', name])])
        self.assertEqual({kw['cwd'] for _, kw in calls}, {root})

    @unittest.skipUnless(os.name == 'nt', 'drive letters are Windows paths')
    def test_a_file_rsync_cannot_name_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            other = 'Q:' if root.drive.upper() != 'Q:' else 'R:'
            with self.assertRaises(RemoteError) as e:
                rc_.local_operand(root, Path(f'{other}/rig/tinyusb.json'))
        self.assertIn('is not on the drive of the checkout', e.exception.code)

    def test_the_state_the_evidence_supports(self):
        launch = {'pid': 7, 'start': '1'}
        for probe, state in (
                ({'tokenOk': False}, 'gone'),
                ({'tokenOk': True, 'launch': None, 'done': None, 'driverLock': 'missing'}, 'not-launched'),
                # claimed, its supervisor not yet (or never) recorded: the inherited driver lock tells
                ({'tokenOk': True, 'claimed': True, 'launch': None, 'done': None, 'driverLock': 'held'}, 'running'),
                ({'tokenOk': True, 'claimed': True, 'launch': None, 'done': None, 'driverLock': 'free'}, 'dead'),
                ({'tokenOk': True, 'claimed': True, 'launch': None, 'done': None, 'driverLock': 'missing'}, 'dead'),
                ({'tokenOk': True, 'launch': launch, 'done': {'exit': 0}, 'driverLock': 'held'}, 'done'),
                ({'tokenOk': True, 'launch': None, 'done': {'exit': 127}}, 'done'),
                ({'tokenOk': True, 'launch': launch, 'done': None, 'driverLock': 'held', 'driverAlive': False}, 'running'),
                ({'tokenOk': True, 'launch': launch, 'done': None, 'driverLock': 'free', 'driverAlive': False}, 'dead'),
                # the lock free while the recorded driver lives contradicts itself
                ({'tokenOk': True, 'launch': launch, 'done': None, 'driverLock': 'free', 'driverAlive': True}, 'inconclusive'),
                ({'tokenOk': True, 'launch': launch, 'done': None, 'driverLock': 'missing', 'driverAlive': False}, 'inconclusive'),
                # a launch record not yet (or never) written, an unreadable completion record
                ({'tokenOk': True, 'launch': 'unreadable', 'done': None, 'driverLock': 'free'}, 'inconclusive'),
                ({'tokenOk': True, 'launch': {'token': 't'}, 'done': None, 'driverLock': 'free'}, 'inconclusive'),
                ({'tokenOk': True, 'launch': launch, 'done': 'unreadable', 'driverLock': 'free', 'driverAlive': False}, 'inconclusive')):
            with self.subTest(probe):
                self.assertEqual(rc_.attempt_state(probe), state)

    def test_a_wait_bounds_each_status_poll_by_the_time_left(self):
        tree = rc_.Tree('rig', '/tmp/t', '/tmp/t.lock', 'tok', Path('.'))
        given = []

        def slow_rig(host, op, *args, timeout=rc_.RIG_SECS):
            given.append(timeout)
            time.sleep(timeout)   # a rig that answers no poll
            return 255, '', f'ssh to {host} did not return within {timeout:g} s'
        with mock.patch.object(rc_, '_rig', slow_rig), mock.patch.object(rc_, 'STATUS_MIN_SECS', 0.2):
            t = time.monotonic()
            code, st = rc_.wait_attempt(tree, 0.5, poll=0.05)
            took = time.monotonic() - t
        self.assertEqual((code, st['state']), (rc_.INCONCLUSIVE, 'unreachable'))
        self.assertTrue(given and all(0.2 <= g <= 0.5 for g in given), given)
        self.assertLess(took, 0.5 + 0.2 + 0.3, 'a poll past the deadline is bounded by STATUS_MIN_SECS')

    def test_the_rig_helper_is_one_program(self):
        compile(rc_.RIG_PY, 'rig', 'exec')
        self.assertTrue(rc_.RIG_PY.startswith(f'TOKEN_FILE, RECORDS, REFUSED, FETCH_RESULT, RIG_PATH = {rc_.TOKEN_FILE!r}, '
                                              f'{rc_.RECORDS!r}, {rc_.RIG_REFUSED}, {rc_.FETCH_RESULT!r}, '
                                              f'{rc_.RIG_PATH!r}\n'))


class FetchStream(unittest.TestCase):
    """read_fetch against synthetic streams: anything malformed or self-contradictory is None, a
    transport failure, before anything is published."""

    def read(self, data, members=('report.json', 'run/')):
        with tempfile.TemporaryDirectory() as into:
            return rc_.read_fetch(io.BytesIO(data), list(members), Path(into))

    def test_a_well_formed_stream(self):
        self.assertEqual(self.read(stream(('report.json', b'{}'), ('run', None), ('run/a.log', b'x'),
                                          status_entry(**{'report.json': 'ok', 'run/': 'ok'}))),
                         {'report.json': 'ok', 'run/': 'ok'})
        self.assertEqual(self.read(stream(status_entry(**{'report.json': 'absent'}))),
                         {'report.json': 'absent', 'run/': 'transport'})

    def test_malformed_or_inconsistent_streams(self):
        for case, data in (
                ('a directory named as a file member', stream(('report.json', None), status_entry(**{'report.json': 'ok'}))),
                ('a file named as a tree member', stream(('run', b'x'), status_entry(**{'run/': 'ok'}))),
                ('a directory status entry', stream((rc_.FETCH_RESULT, None))),
                ('ok without the artifact', stream(status_entry(**{'report.json': 'ok'}))),
                ('a tree ok without its root', stream(('run/a.log', b'x'), status_entry(**{'run/': 'ok'}))),
                ('an entry not asked for', stream(('other.json', b'x'), status_entry(**{'report.json': 'absent'}))),
                ('an entry leaving its root', stream(('run/../../x', b'x'), status_entry())),
                ('a member twice', stream(('report.json', b'1'), ('report.json', b'2'), status_entry(**{'report.json': 'ok'}))),
                ('two status entries', stream(status_entry(), status_entry())),
                ('absent, yet its entries arrived', stream(('run', None), ('run/a.log', b'x'), status_entry(**{'run/': 'absent'}))),
                ('an absent file that arrived', stream(('report.json', b'{}'), status_entry(**{'report.json': 'absent'}))),
                ('a null status entry before a valid one', stream((rc_.FETCH_RESULT, b'null'), status_entry())),
                ('a nested directory twice', stream(('run', None), ('run/sub', None), ('run/sub', None),
                                                    status_entry(**{'run/': 'ok'}))),
                ('no status entry', stream(('report.json', b'{}'))),
                ('a status entry that is not an object of strings', stream((rc_.FETCH_RESULT, b'["ok"]'))),
                ('a status value that is not a string', stream(status_entry(**{'report.json': 1}))),
                ('not a tar stream', b'greeting\n' + stream(status_entry())),
                ('nothing', b'')):
            with self.subTest(case):
                self.assertIsNone(self.read(data))


class Drift(unittest.TestCase):
    """Receipt validation inputs: what an adapter checks before and after staging from the live
    checkout. The receipt fields themselves stay the adapter's."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        # a pre-commit hook exports GIT_DIR/GIT_INDEX_FILE: the temp checkout's git must not act on the real one
        for k in [k for k in os.environ if k.startswith('GIT_')]:
            del os.environ[k]
        (self.root / 'run.py').write_text('harness')
        (self.root / 'roster.json').write_text('{}')
        self.git('init', '-q')
        self.commit('base')
        self.head = rc_.git(self.root, 'rev-parse', 'HEAD')
        self.pinned = {'roster.json': rc_.digest(self.root / 'roster.json')}

    def git(self, *argv):
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        '-c', 'commit.gpgsign=false', *argv], check=True)

    def commit(self, msg):
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', msg)

    def test_a_clean_checkout_at_head_has_no_drift(self):
        (self.root / 'untracked.log').write_text('not staged from the checkout')
        self.assertEqual(rc_.checkout_drift(self.root, self.head, self.pinned), [])

    def test_a_tracked_edit_a_moved_head_and_a_changed_pinned_file(self):
        (self.root / 'run.py').write_text('edited after the build')
        self.assertEqual(rc_.checkout_drift(self.root, self.head, self.pinned),
                         ['tracked files changed since HEAD: run.py'])
        self.commit('later')
        why = rc_.checkout_drift(self.root, self.head, self.pinned)
        self.assertEqual(len(why), 1)
        self.assertIn(f'it is for {self.head[:12]} but the checkout is at', why[0])
        (self.root / 'roster.json').write_text('{"note": "a variant gained a define"}')
        self.assertIn('roster.json is not the one it was built from', rc_.checkout_drift(self.root, self.head, self.pinned))

    def test_git_failure_is_a_refusal(self):
        with self.assertRaises(RemoteError) as e:
            rc_.git(self.root / 'nope', 'rev-parse', 'HEAD')
        self.assertIn('git rev-parse HEAD', e.exception.code)

    def test_a_hooks_git_environment_neither_redirects_nor_reconfigures_it(self):
        # TemporaryDirectory, not mkdtemp+rmtree: its cleanup clears git's read-only objects on Windows
        tmp = tempfile.TemporaryDirectory(dir=self.root.parent)
        self.addCleanup(tmp.cleanup)
        other = Path(tmp.name)
        subprocess.run(['git', '-C', str(other), 'init', '-q'], check=True)
        (other / 'a').write_text('a')
        subprocess.run(['git', '-C', str(other), 'add', 'a'], check=True)
        before = (other / '.git' / 'index').read_bytes()
        (self.root / 'é.txt').write_text('x')
        self.commit('non-ascii')
        head = rc_.git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'é.txt').write_text('edited')
        os.environ.update({'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(other),
                           'GIT_INDEX_FILE': str(other / '.git' / 'index'),
                           'GIT_OBJECT_DIRECTORY': str(other / '.git' / 'objects'),
                           'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.quotePath', 'GIT_CONFIG_VALUE_0': 'false'})
        self.assertEqual(rc_.checkout_drift(self.root, head),
                         ['tracked files changed since HEAD: "\\303\\251.txt"'])
        self.assertEqual((other / '.git' / 'index').read_bytes(), before)

    def test_status_never_runs_the_checkouts_fsmonitor_hook(self):
        marker = self.root.parent / f'{self.root.name}.fsmonitor'
        self.addCleanup(marker.unlink, missing_ok=True)
        self.git('config', 'core.fsmonitor', f'touch {shlex.quote(str(marker))}; false')
        self.assertEqual(rc_.checkout_drift(self.root, self.head), [])
        self.assertFalse(marker.exists())


class RunRecords(unittest.TestCase):
    """A wrapper's local started/done records and the wait on them. No rig: the body is stubbed,
    the processes are local."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / '.hil-remote'

    def run_(self, run_id, body):
        return rc_.run_recorded(self.runs, run_id, body)

    def wait(self, run_id, timeout=rc_.WAIT_SECS, start_grace=20):
        return rc_.wait_local(self.runs, run_id, timeout, poll=0.05, start_grace=start_grace)

    def started(self, run_id):
        """A started record nobody holds: its run is gone."""
        started, _ = rc_.run_paths(self.runs, run_id)
        started.parent.mkdir(exist_ok=True)
        rc_.write_json(started, {'runId': run_id, 'pid': 1, 'startedAt': 0})

    def launch(self, body, run_id):
        """A real background wrapper in its own process, whose body is `body`."""
        script = textwrap.dedent(f'''
            import importlib.util, os, signal, sys, time
            spec = importlib.util.spec_from_file_location('core', {str(SCRIPT)!r})
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            sys.exit(m.run_recorded({str(self.runs)!r}, {run_id!r}, lambda: {body}))
        ''')
        p = subprocess.Popen([sys.executable, '-c', script])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p

    def test_a_finished_run_leaves_its_exit_and_reports(self):
        self.assertEqual(self.run_('r1', lambda: (2, ['hil_report.md', 'hil_report.json'])), 2)
        self.assertEqual(self.wait('r1'), (0, {'state': 'done', 'runId': 'r1', 'exit': 2,
                                               'reports': ['hil_report.md', 'hil_report.json']}))

    def test_extra_fields_go_on_the_started_record(self):
        self.run_('r0', lambda: (0, []))
        self.assertEqual(rc_.run_recorded(self.runs, 'r0b', lambda: (0, []), {'remote': {'host': 'rig'}}), 0)
        started = json.loads(rc_.run_paths(self.runs, 'r0b')[0].read_text())
        self.assertEqual(started['remote'], {'host': 'rig'})

    def test_a_refused_run_records_its_exit_and_no_report(self):
        with self.assertRaises(SystemExit) as e:
            self.run_('r2', lambda: rc_.fail('no build under cmake-build/'))
        self.assertEqual(e.exception.code, 'error: no build under cmake-build/', 'the refusal still reaches stderr')
        self.assertEqual(self.wait('r2')[1], {'state': 'done', 'runId': 'r2', 'exit': 1, 'reports': []})

    def test_a_done_record_that_cannot_be_written_still_releases_the_run(self):
        with self.assertRaises(TypeError):
            self.run_('r9', lambda: (0, [object()]))   # reports JSON cannot hold
        self.assertEqual(self.wait('r9', timeout=1)[0], rc_.DEAD, 'a finished run never reads as running')

    def test_a_run_id_is_used_once(self):
        self.run_('r3', lambda: (0, []))
        with self.assertRaises(RemoteError) as e:
            self.run_('r3', lambda: (0, []))
        self.assertIn('was used before (.hil-remote/r3.started.json)', e.exception.code)

    def test_a_live_run_is_still_running_at_the_timeout(self):
        p = self.launch('(time.sleep(30), (0, []))[1]', 'r4')
        rc, status = self.wait('r4', 2)  # the start grace covers the launch
        self.assertEqual((rc, status['state'], status['runId'], status['pid']), (rc_.RUNNING, 'running', 'r4', p.pid))

    def test_an_earlier_runs_done_record_does_not_answer_for_this_one(self):
        self.run_('old', lambda: (0, ['hil_report.md']))
        self.launch('(time.sleep(30), (0, []))[1]', 'new')
        self.assertEqual(self.wait('new', 1)[1]['state'], 'running')

    def test_a_started_record_nobody_holds_is_dead(self):
        self.started('r5')
        self.assertEqual(self.wait('r5', 5)[0], rc_.DEAD)

    def test_a_run_that_ends_while_waited_on_is_done(self):
        """wait blocks across the start and the end: the records land after wait began polling."""
        self.launch("(time.sleep(0.5), (0, ['hil_report.md']))[1]", 'r6')
        self.assertEqual(self.wait('r6', 20), (0, {'state': 'done', 'runId': 'r6', 'exit': 0, 'reports': ['hil_report.md']}))

    def test_a_wrapper_killed_before_its_done_record_is_dead(self):
        # Windows has no SIGKILL; its SIGTERM is TerminateProcess, as abrupt
        self.launch('os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))', 'r7').wait()
        rc, status = self.wait('r7', 5)
        self.assertEqual((rc, status['state']), (rc_.DEAD, 'dead'))
        self.assertFalse(rc_.run_paths(self.runs, 'r7')[1].exists())

    @unittest.skipUnless(os.name == 'nt', 'msvcrt has no shared lock: two waiters probes contend there')
    def test_another_waiters_probe_does_not_read_as_a_live_run(self):
        self.started('r12')
        started = rc_.run_paths(self.runs, 'r12')[0]
        fd = os.open(started, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        rc_.win_lock(fd, block=False)   # the other waiter, mid-probe
        other = threading.Timer(0.05, rc_.win_unlock, (fd,))
        other.start()
        self.addCleanup(other.join)   # before the close: it seeks and unlocks fd
        # a 5 s retry budget at the usual step, so a late Timer on a loaded host still lands inside it
        with mock.patch.object(rc_, 'PROBE_TRIES', int(5 / rc_.PROBE_SECS)), \
                mock.patch.object(rc_, 'win_unlock', wraps=rc_.win_unlock) as unlock:
            self.assertFalse(rc_.run_alive(started))
        unlock.assert_called_once()   # the probe released its hold, not left it to the close

    def test_a_refused_unlink_of_the_hidden_name_keeps_the_runs_exit(self):
        # Windows refuses it while a waiter's probe holds the record open; the next sweep takes it
        real_unlink = Path.unlink

        def unlink(path, missing_ok=False):
            if path.name.startswith('.r14.started.json.'):
                raise PermissionError(13, 'The process cannot access the file')
            return real_unlink(path, missing_ok=missing_ok)
        with mock.patch.object(rc_, 'fcntl', None), mock.patch.object(rc_, 'win_lock', create=True), \
                mock.patch.object(rc_, 'win_unlock', create=True), mock.patch.object(Path, 'unlink', unlink):
            self.assertEqual(self.run_('r14', lambda: (3, [])), 3)
        self.assertEqual(self.wait('r14')[1], {'state': 'done', 'runId': 'r14', 'exit': 3, 'reports': []})

    @unittest.skipUnless(os.name == 'nt', 'only Windows defers the unlink of the hidden name')
    def test_a_killed_runs_hidden_record_name_is_swept_at_the_next_run(self):
        self.runs.mkdir()
        hidden = {}
        for run_id, pid in (('killed', 4242), ('live', 4343)):
            started = self.runs / f'{run_id}.started.json'
            started.write_text('{}')
            hidden[run_id] = self.runs / f'.{started.name}.{pid}.tmp'
            os.link(started, hidden[run_id])
        fd = os.open(hidden['live'], os.O_RDONLY)   # its run still holds it
        self.addCleanup(os.close, fd)
        writing = self.runs / '.new.started.json.4444.tmp'   # a launch that has not linked its record yet
        writing.write_text('{}')
        self.assertEqual(self.run_('r13', lambda: (0, [])), 0)
        self.assertEqual(sorted(p.name for p in self.runs.glob('.*.tmp')), sorted([hidden['live'].name, writing.name]))
        self.assertTrue((self.runs / 'killed.started.json').is_file(), 'the record itself stays')

    def test_a_wait_begun_before_the_launch_wrote_its_record_waits_for_it(self):
        timer = threading.Timer(0.1, self.run_, ['r8', lambda: (0, [])])
        timer.start()
        self.addCleanup(timer.join)
        self.assertEqual(self.wait('r8', 20)[1]['state'], 'done')

    def reading_done(self, run_id, *refusals):
        """Path.read_text with the run's done record answering `refusals` (an exception, or the text read) first."""
        done, real = rc_.run_paths(self.runs, run_id)[1], Path.read_text
        left = list(refusals)

        def read_text(path, *args, **kwargs):
            if path == done and left:
                refusal = left.pop(0)
                if isinstance(refusal, Exception):
                    raise refusal
                return refusal
            return real(path, *args, **kwargs)
        return mock.patch.object(Path, 'read_text', read_text)

    def test_a_done_record_refused_or_torn_for_a_moment_is_read_again(self):
        self.run_('r15', lambda: (0, ['hil_report.md']))
        with self.reading_done('r15', PermissionError(13, 'Permission denied'), '{"runId": "r15", "ex'):
            self.assertEqual(self.wait('r15', 5), (0, {'state': 'done', 'runId': 'r15', 'exit': 0,
                                                       'reports': ['hil_report.md']}))

    def test_a_done_record_unreadable_to_the_timeout_is_an_error(self):
        self.run_('r16', lambda: (0, []))
        begun = time.monotonic()
        with self.reading_done('r16', *[PermissionError(13, 'Permission denied')] * 1000), \
                self.assertRaises(RemoteError) as e:
            self.wait('r16', 0.3)
        self.assertIn('run r16 finished, but its done record', e.exception.code)
        self.assertIn('stayed unreadable: [Errno 13] Permission denied', e.exception.code)
        self.assertLess(time.monotonic() - begun, 2, 'the retries end with the timeout')

    def test_the_start_grace_ends_with_the_timeout(self):
        begun = time.monotonic()
        with self.assertRaises(RemoteError) as e:
            rc_.wait_local(self.runs, 'never', 0.1, poll=5, start_grace=30)
        self.assertIn(f'no run never was started from {self.root}', e.exception.code)
        self.assertLess(time.monotonic() - begun, 1, 'neither the grace nor its poll outlasts the timeout')

    def test_a_killed_runs_hidden_name_under_a_reused_pid_keeps_its_record(self):
        self.started('r13')
        started = rc_.run_paths(self.runs, 'r13')[0]
        os.link(started, started.with_name(f'.{started.name}.{os.getpid()}.tmp'))   # left by a kill on Windows
        before = started.read_bytes()
        with self.assertRaises(FileExistsError):
            rc_.publish_locked(started, {'runId': 'r13', 'pid': os.getpid(), 'startedAt': 1})
        self.assertEqual(started.read_bytes(), before)

    def test_an_unlinked_leftover_hidden_name_is_replaced(self):
        started = rc_.run_paths(self.runs, 'r14')[0]
        started.parent.mkdir(exist_ok=True)
        started.with_name(f'.{started.name}.{os.getpid()}.tmp').write_text('stale')
        os.close(rc_.publish_locked(started, {'runId': 'r14', 'pid': os.getpid(), 'startedAt': 1}))
        self.assertEqual(json.loads(started.read_text())['runId'], 'r14')

    def test_a_timeout_past_the_tool_cap_is_refused(self):
        for bad in ('571', '-1', 'nan', 'inf'):
            with self.subTest(timeout=bad), self.assertRaises(Exception) as e:
                rc_.wait_secs(bad)
            self.assertEqual(type(e.exception).__name__, 'ArgumentTypeError')
        self.assertEqual(rc_.wait_secs('570'), 570)

    def test_options_need_a_value(self):
        self.assertEqual(rc_.pop_option(['-b', 'x', '--run-id', 'r', '-v'], '--run-id'), ('r', ['-b', 'x', '-v']))
        self.assertEqual(rc_.pop_option(['--receipt=f'], '--receipt'), ('f', []))
        self.assertEqual(rc_.pop_option(['-v'], '--run-id'), (None, ['-v']))
        for argv, name in ((['-v', '--run-id'], '--run-id'), (['--receipt='], '--receipt'), (['--run-id', ''], '--run-id')):
            with self.subTest(argv), self.assertRaises(RemoteError) as e:
                rc_.pop_option(argv, name)
            self.assertIn(f'{name} needs a value', e.exception.code)


@needs_fake_rig
@needs_rsync
@needs_flock
@needs_rig_python
class Attempts(Rig):
    """Create-only attempts with a detached launch: the rig helper and its supervisor run for real
    under the fake ssh."""

    def setUp(self):
        super().setUp()
        self.runs = self.root / '.hil-remote'
        self.base = self.tmp / 'attempts'

    def create(self, run_id='a1', runs=None, dirs=('src', 'out')):
        return rc_.create_attempt(self.root, runs or self.runs, run_id, 'rig', str(self.base), dirs)

    def launch(self, tree, argv=('python3', 'run.py'), env=None):
        record = rc_.launch(tree, list(argv), env)
        self.addCleanup(self.kill, record)
        return record

    @staticmethod
    def kill(record):
        with contextlib.suppress(OSError, KeyError):
            os.killpg(record['sid'], signal.SIGKILL)

    def gone(self, pid, secs=10):
        """Wait until pid is gone or a zombie."""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            try:
                if Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                    return
            except OSError:
                return
            time.sleep(0.05)
        self.fail(f'pid {pid} still alive')

    def wait(self, tree, timeout=20, **kw):
        return rc_.wait_attempt(tree, timeout, poll=0.05, **kw)

    def test_create_makes_a_token_guarded_dir_and_a_local_record(self):
        tree = self.create()
        d = self.base / 'a1'
        self.assertEqual((tree.host, tree.dir, tree.lock), ('rig', str(d), f'{d}.lease'))
        self.assertEqual((d / rc_.TOKEN_FILE).read_text().strip(), tree.token)
        self.assertTrue((d / 'src').is_dir() and (d / 'out').is_dir() and (d / rc_.RECORDS).is_dir())
        self.assertEqual(rc_.load_attempt(self.root, self.runs, 'a1'), tree)
        self.assertEqual(rc_.attempt_status(tree)['state'], 'not-launched')

    def test_a_tilde_base_lands_in_home(self):
        tree = rc_.create_attempt(self.root, self.runs, 'a1', 'rig', '~/hil-attempts')
        self.assertEqual(tree.dir, str(self.home / 'hil-attempts/a1'))

    def test_a_reused_attempt_id_is_refused_and_the_attempt_untouched(self):
        tree = self.create()
        (self.base / 'a1/src/evidence').write_text('an unresolved attempt')
        with self.assertRaises(RemoteError) as e:
            self.create()
        self.assertIn('run id a1 was used before (.hil-remote/a1.attempt.json)', e.exception.code)
        # the same id from another checkout's records: the rig refuses
        with self.assertRaises(RemoteError) as e:
            self.create(runs=self.tmp / 'other-runs')
        self.assertIn('attempt a1 exists on the rig', e.exception.code)
        self.assertEqual((self.base / 'a1/src/evidence').read_text(), 'an unresolved attempt')
        self.assertEqual((self.base / 'a1' / rc_.TOKEN_FILE).read_text().strip(), tree.token)

    def test_a_held_lease_refuses_the_create(self):
        import fcntl
        self.base.mkdir()
        lk = open(self.base / 'a1.lease', 'a')
        self.addCleanup(lk.close)
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(RemoteError) as e:
            self.create()
        self.assertIn(f'another session holds {self.base}/a1.lease', e.exception.code)
        self.assertFalse((self.base / 'a1').exists())

    def test_an_uncreated_attempt_cannot_be_loaded(self):
        with self.assertRaises(RemoteError) as e:
            rc_.load_attempt(self.root, self.runs, 'nope')
        self.assertIn('no attempt nope was created', e.exception.code)
        self.setenv(FAKE_SSH_DOWN="' create ")
        with self.assertRaises(RemoteError):
            self.create('a2')
        with self.assertRaises(RemoteError) as e:
            rc_.load_attempt(self.root, self.runs, 'a2')
        self.assertIn('never created on its rig', e.exception.code)

    def stage(self, tree):
        src = self.tmp / 'stage'
        for rel, text in (('run.py', 'the driver'), ('bundles/repo.bundle', 'git objects'), ('manifest.json', '{}')):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text)
        rc_.stage_tree(tree, src, '.')
        return src, rc_.file_digests(src, [p for p in src.rglob('*') if p.is_file()])

    def test_staging_lands_and_verifies_on_the_rig(self):
        tree = self.create()
        src, digests = self.stage(tree)
        self.assertEqual(rc_.verify_remote(tree, digests), [])
        (self.base / 'a1/bundles/repo.bundle').write_text('changed on the rig')
        (self.base / 'a1/manifest.json').unlink()
        self.assertEqual(rc_.verify_remote(tree, digests), ['bundles/repo.bundle', 'manifest.json'])

    def test_a_link_in_the_staged_dir_is_refused_before_the_rig(self):
        tree = self.create()
        src = self.tmp / 'stage'
        src.mkdir()
        (src / 'dep').symlink_to(self.root)
        before = len(self.calls())
        with self.assertRaises(RemoteError) as e:
            rc_.stage_tree(tree, src)
        self.assertIn('symlinks', e.exception.code)
        self.assertEqual(len(self.calls()), before)

    def test_a_replaced_token_refuses_staging_launch_and_collection(self):
        tree = self.create()
        (self.base / 'a1' / rc_.TOKEN_FILE).write_text('another session\n')
        with captured_fd2(), self.assertRaises(RemoteError):
            self.stage(tree)
        self.assertFalse((self.base / 'a1/run.py').exists())
        with self.assertRaises(RemoteError) as e:
            rc_.launch(tree, ['python3', 'run.py'])
        self.assertIn("is not this attempt's tree any more", e.exception.code)
        self.assertFalse((self.base / 'a1' / rc_.RECORDS / 'launch.json').exists())
        self.assertEqual(rc_.attempt_status(tree)['state'], 'gone')
        for call in (lambda: self.wait(tree), lambda: rc_.collect(tree, self.tmp / 'got')):
            with self.assertRaises(RemoteError) as e:
                call()
            self.assertIn("not this attempt's tree any more", e.exception.code)

    def test_a_launch_runs_detached_and_wait_collects_it(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_WRITE='out/report.json out/run/step.log', FAKE_RC='2')
        record = self.launch(tree, env={'HIL_ODD': "a b'c $HOME"})
        self.assertEqual((record['argv'], record['cwd'], record['env'], record['token']),
                         (['python3', 'run.py'], '.', ['HIL_ODD'], tree.token))
        self.assertEqual(record['sid'], record['pid'], 'the supervisor leads its own session')
        dest = self.tmp / 'got'
        code, st = self.wait(tree, dest=dest, groups=[('out/report.json', 'out/run/')])
        self.assertEqual((code, st['state'], st['done']['exit']), (0, 'done', 2))
        col = st['collection']
        self.assertEqual((col['state'], col['remoteState'], col['missing']), ('complete', 'done', []))
        self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text()), col)
        self.assertEqual((dest / 'out/report.json').read_text(), 'from the rig')
        self.assertEqual((dest / 'out/run/step.log').read_text(), 'from the rig')
        self.assertTrue(all((dest / r).is_file() for r in rc_.RECORD_FILES))
        self.assertEqual(self.ran()['env'], {'HIL_ODD': "a b'c $HOME"})
        self.assertEqual(Path(self.ran()['cwd']), self.base / 'a1')
        # collected again, idempotently: the complete collection is returned, nothing fetched
        before = len(self.calls())
        self.assertEqual(rc_.collect(tree, dest, [('out/report.json', 'out/run/')]), col)
        self.assertEqual(self.wait(tree, dest=dest, groups=[('out/report.json',)])[1]['collection'], col)
        self.assertEqual([c['tool'] for c in self.calls()[before:]], ['ssh'], 'one status poll, no fetch')

    def test_the_driver_is_detached_from_the_session_that_launched_it(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_SLEEP='30')
        record = self.launch(tree)
        # the launching ssh session has ended; the driver lives on, in its own session, its stdio
        # on the attempt's files and nothing inherited from the session
        self.assertEqual(rc_.attempt_status(tree)['state'], 'running')
        records = self.base / 'a1' / rc_.RECORDS
        fds = {int(fd.name): os.readlink(fd) for fd in Path(f'/proc/{record["pid"]}/fd').iterdir()}
        self.assertEqual((fds[0], fds[1], fds[2]), (os.devnull, str(records / 'stdout.log'), str(records / 'stderr.log')))
        self.assertEqual(sorted(v for k, v in fds.items() if k > 2), [str(records / 'driver.lock')])
        self.assertEqual(os.getsid(record['childPid']), record['sid'])

    def test_a_launch_is_never_repeated_and_staging_ends_at_the_launch(self):
        tree = self.create()
        self.stage(tree)
        self.launch(tree)
        self.assertEqual(self.wait(tree)[0], 0)
        with self.assertRaises(RemoteError) as e:
            rc_.launch(tree, ['python3', 'run.py'])
        self.assertIn('was launched before', e.exception.code)
        with captured_fd2(), self.assertRaises(RemoteError):
            self.stage(tree)

    def test_a_lost_acknowledgement_finds_the_attempt_started_and_never_launches_another(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_SSH_DROP="' launch ", FAKE_SLEEP='30')
        with self.assertRaises(RemoteError) as e:
            rc_.launch(tree, ['python3', 'run.py'])
        self.assertIn('was not acknowledged (exit 255)', e.exception.code)
        self.assertIn('never launch it again', e.exception.code)
        st = rc_.attempt_status(tree)
        self.addCleanup(self.kill, st['launch'])
        self.assertEqual(st['state'], 'running')
        with self.assertRaises(RemoteError) as e:
            rc_.launch(tree, ['python3', 'run.py'])
        self.assertIn('was launched before', e.exception.code)
        self.assertEqual(rc_.attempt_status(tree)['launch']['pid'], st['launch']['pid'])

    def test_a_launcher_dead_in_the_handshake_leaves_the_attempt_to_its_supervisor(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_WRITE='out/report.json', FAKE_SLEEP='0.5')
        cmd = shlex.join(['python3', '-c', LAUNCHER_DIES_IN_HANDSHAKE + rc_.RIG_PY, 'launch', tree.dir, tree.lock,
                          tree.token, '.', '{}', '--', 'python3', 'run.py'])
        r = subprocess.run(rc_.ssh_argv('rig', cmd), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('HIL_REMOTE_LAUNCH=', r.stdout, 'the launcher acknowledged before it died')
        code, st = self.wait(tree, dest=self.tmp / 'got', groups=[('out/report.json',)])
        self.addCleanup(self.kill, st['launch'])
        self.assertEqual((code, st['state'], st['done']['exit']), (0, 'done', 0))
        self.assertEqual((st['launch']['argv'], st['launch']['token']), (['python3', 'run.py'], tree.token))
        self.assertTrue((self.base / 'a1' / rc_.RECORDS / 'done.json').is_file())
        self.assertEqual(st['collection']['state'], 'complete')
        with self.assertRaises(RemoteError) as e:
            rc_.launch(tree, ['python3', 'run.py'])
        self.assertIn('was launched before', e.exception.code)
        self.assertEqual(json.loads((self.base / 'a1' / rc_.RECORDS / 'launch.json').read_text())['pid'], st['launch']['pid'])

    def test_a_launch_inside_its_claim_section_is_never_read_as_dead(self):
        tree = self.create()
        self.stage(tree)
        paused, resume = self.tmp / 'paused', self.tmp / 'resume'
        self.setenv(FAKE_PAUSED=str(paused), FAKE_RESUME=str(resume), FAKE_WRITE='out/report.json')
        cmd = shlex.join(['python3', '-c', LAUNCHER_PAUSES_AFTER_ITS_CLAIM + rc_.RIG_PY, 'launch', tree.dir, tree.lock,
                          tree.token, '.', '{}', '--', 'python3', 'run.py'])
        launcher = subprocess.Popen(rc_.ssh_argv('rig', cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        self.addCleanup(launcher.wait)
        self.addCleanup(launcher.kill)
        self.addCleanup(resume.touch)
        deadline = time.monotonic() + 20
        while not paused.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.base / 'a1' / rc_.RECORDS / 'launch.claim').exists())
        self.assertFalse((self.base / 'a1' / rc_.RECORDS / 'driver.lock').exists(), 'paused before the driver lock')
        code, st = self.wait(tree, 0.5)   # one probe, which waits out its lease bound
        self.assertEqual((code, st['state'], st.get('leaseBusy')), (rc_.INCONCLUSIVE, 'inconclusive', True),
                         'a live launch read as dead')
        resume.touch()
        out, err = launcher.communicate(timeout=60)
        self.assertEqual(launcher.returncode, 0, err)
        self.assertIn('HIL_REMOTE_LAUNCH=', out)
        code, st = self.wait(tree, dest=self.tmp / 'got', groups=[('out/report.json',)])
        self.addCleanup(self.kill, st['launch'])
        self.assertEqual((code, st['state'], st['collection']['state']), (0, 'done', 'complete'))

    def rig_fetch(self, tree, prelude, members):
        """The rig helper's fetch run here, under prelude, its stream read as _fetch reads it."""
        into = self.tmp / 'into'
        rc_._remove(into)
        into.mkdir()
        p = subprocess.Popen(['python3', '-c', prelude + rc_.RIG_PY, 'fetch', tree.dir, tree.token, *members],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            got = rc_.read_fetch(p.stdout, list(members), into)
        finally:
            p.stdout.close()
            err = p.stderr.read().decode()
            p.stderr.close()
            p.wait()
        return got, p.returncode, err, into

    @unittest.skipUnless(sys.platform.startswith('linux'), 'RLIMIT_AS and /proc/self/status')
    def test_a_fetch_streams_a_file_far_larger_than_the_helpers_memory(self):
        tree = self.create()
        big = self.base / 'a1/out/big.bin'
        with open(big, 'wb') as f:
            f.truncate(96 << 20)   # sparse: no disk on the rig side
            f.seek((96 << 20) - 3)
            f.write(b'end')
        # 32 MiB of address space beyond what the interpreter already maps: a whole-file read cannot fit
        limit = textwrap.dedent('''
            import resource as _resource
            _vm = next(int(ln.split()[1]) for ln in open('/proc/self/status') if ln.startswith('VmSize:')) * 1024
            _resource.setrlimit(_resource.RLIMIT_AS, (_vm + (32 << 20), _vm + (32 << 20)))
        ''')
        got, code, err, into = self.rig_fetch(tree, limit, ['out/big.bin'])
        self.assertEqual((got, code), ({'out/big.bin': 'ok'}, 0), err)
        self.assertEqual((into / 'out/big.bin').stat().st_size, 96 << 20)
        with open(into / 'out/big.bin', 'rb') as f:
            f.seek(-3, 2)
            self.assertEqual(f.read(), b'end')
        # the limit binds: a whole-file read in the helper fails under it
        whole = limit + "assert len(open(%r, 'rb').read())\n" % str(big)
        r = subprocess.run(['python3', '-c', whole], capture_output=True, text=True)
        self.assertIn('MemoryError', r.stderr)

    def test_a_descendant_vanishing_mid_fetch_is_no_absent_tree(self):
        tree = self.done_attempt(write='out/run/a.json out/run/b.json')
        got, code, err, _ = self.rig_fetch(tree, B_JSON_VANISHES, ['out/run/', 'out/nothing/'])
        self.assertEqual(code, 0, err)
        self.assertTrue(got['out/run/'].startswith('vanished: '), got)
        self.assertEqual(got['out/nothing/'], 'absent')
        # through collect: incomplete, so not cached, and a retry fetches what is left
        (self.base / 'a1/out/run/b.json').write_text('again')
        prelude = self.tmp / 'prelude.py'
        prelude.write_text(B_JSON_VANISHES)
        self.setenv(FAKE_FETCH_PRELUDE=str(prelude))
        dest = self.tmp / 'got'
        col = rc_.collect(tree, dest, optional=['out/run/'])
        self.assertEqual(col['state'], 'incomplete')
        self.assertTrue(col['outcomes']['out/run/'].startswith('vanished: '))
        self.assertFalse((dest / 'out/run').exists(), 'part of a tree landed')
        del os.environ['FAKE_FETCH_PRELUDE']
        col = rc_.collect(tree, dest, optional=['out/run/'])
        self.assertEqual((col['state'], col['outcomes']['out/run/']), ('complete', 'ok'))
        self.assertEqual(self.remote_files(dest / 'out/run'), ['a.json'])

    def test_a_file_that_shrinks_while_fetched_is_reported_truncated(self):
        tree = self.create()
        (self.base / 'a1/out/run').mkdir()
        for rel in ('out/report.json', 'out/run/a.log'):
            (self.base / 'a1' / rel).write_text('x' * 100)
        # fstat reports more than the file holds, as for one cut after its size was taken
        grow = textwrap.dedent('''
            import os as _os, stat as _stat
            _fstat = _os.fstat
            def _bigger(fd):
                st = _fstat(fd)
                if not _stat.S_ISREG(st.st_mode):
                    return st
                return _os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                        st.st_size + 5000, st.st_atime, st.st_mtime, st.st_ctime))
            _os.fstat = _bigger
        ''')
        got, code, err, _ = self.rig_fetch(tree, grow, ['out/report.json', 'out/run/'])
        self.assertEqual((got, code), ({'out/report.json': 'truncated', 'out/run/': 'truncated'}, 0), err)

    def test_the_transports_names_are_never_staged(self):
        tree = self.create()
        for name in (f'{rc_.RECORDS}/done.json', rc_.TOKEN_FILE):
            with self.subTest(name):
                src = self.tmp / f'stage-{name.replace("/", "-")}'
                (src / name).parent.mkdir(parents=True, exist_ok=True)
                (src / name).write_text('{"exit": 0}')
                (src / 'run.py').write_text('the driver')
                (self.root / name).parent.mkdir(parents=True, exist_ok=True)
                (self.root / name).write_text('{"exit": 0}')
                before = len(self.calls())
                for call in (lambda: rc_.stage_tree(tree, src), lambda: rc_.stage_files(tree, [name, 'run.py'])):
                    with self.assertRaises(RemoteError) as e:
                        call()
                    self.assertIn(f'name {name.split("/")[0]}, which the transport owns', e.exception.code)
                self.assertEqual(len(self.calls()), before, 'a refusal after a transfer began')
                # a raw staging transfer leaves them out too
                self.assertEqual(rc_.rsync(tree, '-aqR', name, tree.spec('.') + '/', staging=True), 0)
        self.assertEqual((self.base / 'a1' / rc_.TOKEN_FILE).read_text().strip(), tree.token)
        self.assertFalse((self.base / 'a1' / rc_.RECORDS / 'done.json').exists())
        self.assertEqual(rc_.attempt_status(tree)['state'], 'not-launched')
        with self.assertRaises(RemoteError):
            rc_.stage_tree(tree, self.tmp / f'stage-{rc_.RECORDS}-done.json', 'src')   # at the top of any transfer

    def done_attempt(self, run_id='a1', write='out/report.json'):
        tree = self.create(run_id)
        self.setenv(FAKE_WRITE=write)
        self.launch(tree)
        self.assertEqual(self.wait(tree)[0], 0)
        return tree

    def test_a_cached_collection_answers_only_for_its_attempt_and_what_it_fetched(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        col = rc_.collect(tree, dest, [('out/report.json',)])
        self.assertEqual((col['state'], col['identity']), ('complete', {'host': 'rig', 'dir': tree.dir, 'token': tree.token}))
        for groups, optional, short in (([('out/report.json', 'out/summary.json')], (), 'out/summary.json'),
                                        ([('out/',)], (), 'out/'), ((), ['out/extra.log'], 'out/extra.log')):
            with self.subTest(short), self.assertRaises(RemoteError) as e:
                rc_.collect(tree, dest, groups, optional)
            self.assertIn(f'did not fetch {short}', e.exception.code)
        # a file never answers for a tree of the same name
        rc_.collect(tree, self.tmp / 'logs', optional=[f'{rc_.RECORDS}/stdout.log'])
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, self.tmp / 'logs', [(f'{rc_.RECORDS}/stdout.log/',)])
        self.assertIn(f'did not fetch {rc_.RECORDS}/stdout.log/', e.exception.code)
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, dest, [('out/report.json/',)])
        self.assertIn('did not fetch out/report.json/', e.exception.code)
        # optional trees, typed the same way
        logs = self.tmp / 'trees'
        trees = rc_.collect(tree, logs, optional=['out/', 'gone/'])
        self.assertEqual((trees['state'], trees['outcomes']['out/'], trees['outcomes']['gone/']), ('complete', 'ok', 'absent'))
        self.assertEqual(rc_.collect(tree, logs, optional=['out/', 'gone/']), trees)
        for optional, short in ((['out'], 'out'), (['gone'], 'gone'), (['other/'], 'other/')):
            with self.subTest(optional), self.assertRaises(RemoteError) as e:
                rc_.collect(tree, logs, optional=optional)
            self.assertIn(f'did not fetch {short}', e.exception.code)
        (self.base / 'a1/esc').symlink_to(self.tmp)
        esc = rc_.collect(tree, self.tmp / 'esc', optional=['esc/'])
        self.assertEqual((esc['state'], esc['outcomes']['esc/']), ('incomplete', 'unsafe'),
                         'an optional member lost to the fetch is not an absent one')
        other = self.done_attempt('a2')
        with self.assertRaises(RemoteError) as e:
            rc_.collect(other, dest, [('out/report.json',)])
        self.assertIn('holds the collection of another attempt', e.exception.code)
        self.assertEqual(rc_.collect(tree, dest, [('out/report.json',)]), col)
        (dest / rc_.COLLECTION).write_text('{')
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, dest)
        self.assertIn('is unreadable', e.exception.code)
        stray = self.tmp / 'stray'
        stray.mkdir()
        (stray / 'x').write_text('not a collection')
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, stray)
        self.assertIn('holds files but no collection.json', e.exception.code)
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, self.tmp / 'got2', [(rc_.COLLECTION,)])
        self.assertIn('is the collection record itself', e.exception.code)

    def test_a_cached_collection_whose_files_were_moved_out_is_refused(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        col = rc_.collect(tree, dest, [('out/report.json', f'{rc_.RECORDS}/done.json')])
        self.assertEqual(col['state'], 'complete', 'a record file may be a group member too')
        self.assertEqual(rc_.collect(tree, dest, [('out/report.json',)]), col)
        (dest / 'out/report.json').rename(self.tmp / 'adopted.json')   # an adoption moves the report out
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, dest, [('out/report.json',)])
        self.assertIn('out/report.json are gone', e.exception.code)

    def paused_collector(self, dest, seam, args, accept=None):
        """collect(a1, dest, <args>, accept=<accept>) in another interpreter, paused once `seam`
        has run (copy_back, or `accept`, the source of a predicate, on a freshly fetched tree)
        until the file `resume` exists."""
        paused = self.tmp / 'paused'
        script = textwrap.dedent(f'''
            import importlib.util, os, time
            from pathlib import Path
            spec = importlib.util.spec_from_file_location('core', {str(SCRIPT)!r})
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

            def pause():
                Path({str(paused)!r}).write_text('x')
                while not Path({str(self.tmp / 'resume')!r}).exists():
                    time.sleep(0.02)
            accept = {accept or 'None'}
            if {seam!r} == 'copy_back':
                real = m.copy_back
                def copy_back(*a, **k):
                    got = real(*a, **k)
                    pause()
                    return got
                m.copy_back = copy_back
            else:
                predicate = accept
                def accept(path):
                    missing = predicate(path)
                    if Path(path) != Path({str(dest)!r}):
                        pause()
                    return missing
            tree = m.load_attempt(Path({str(self.root)!r}), Path({str(self.runs)!r}), 'a1')
            print(m.collect(tree, Path({str(dest)!r}), {args}, accept=accept)['state'])
        ''')
        first = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(first.wait)
        self.addCleanup(first.kill)
        deadline = time.monotonic() + 20
        while not paused.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(paused.exists())
        return first

    def test_collectors_of_one_dest_are_serialised(self):
        """The incomplete collector pauses holding the dest after its fetch; meanwhile the attempt
        completes and a second collector is refused, so the partial result cannot overwrite a
        complete one published in between."""
        tree = self.create()
        self.setenv(FAKE_WRITE='out/early.log', FAKE_LATE='out/report.json', FAKE_SLEEP='1')
        self.launch(tree)
        dest = self.tmp / 'got'
        first = self.paused_collector(dest, 'copy_back', "[('out/report.json',)]")
        self.assertEqual(self.wait(tree)[0], 0)   # complete evidence is now on the rig
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, dest, [('out/report.json',)])
        self.assertIn('another collector holds', e.exception.code)
        (self.tmp / 'resume').write_text('x')
        self.assertEqual(first.communicate(timeout=30)[0].strip(), 'incomplete')
        self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text())['state'], 'incomplete')
        self.assertEqual(rc_.collect(tree, dest, [('out/report.json',)])['state'], 'complete')
        self.assertEqual(rc_.collect(tree, dest, [('out/report.json',)])['state'], 'complete')

    # a predicate over the collected tree, and its source for a collector in another interpreter
    @staticmethod
    def late(path):
        return [] if (Path(path) / 'out/late.json').is_file() else ['out/late.json']
    LATE = "lambda path: [] if (Path(path) / 'out/late.json').is_file() else ['out/late.json']"

    def ssh_ops(self, since):
        """The rig helper ops of the ssh calls logged since index `since`."""
        return [next((op for op in ('status', 'fetch') if f"' {op} " in ' '.join(c['argv'])), '?')
                for c in self.calls()[since:] if c['tool'] == 'ssh']

    def test_a_done_attempt_is_collected_on_the_status_that_saw_it_done(self):
        tree = self.done_attempt()
        before = len(self.calls())
        code, st = self.wait(tree, dest=self.tmp / 'got', groups=[('out/report.json',)])
        self.assertEqual((code, st['collection']['state']), (0, 'complete'))
        self.assertEqual(self.ssh_ops(before), ['status', 'fetch'])

    def test_a_predicate_rejects_until_the_rig_has_what_it_asks_for(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((col['state'], col['missing'], col['unaccepted']), ('incomplete', [], ['out/late.json']))
        self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text()), col)
        (self.base / 'a1/out/late.json').write_text('late')
        col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((col['state'], col['unaccepted']), ('complete', []))
        self.assertEqual((dest / 'out/late.json').read_text(), 'late')
        before = len(self.calls())
        self.assertEqual(rc_.collect(tree, dest, optional=['out/'], accept=self.late), col)
        self.assertEqual(self.ssh_ops(before), [], 'complete by the predicate: final, never fetched again')
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, self.tmp / 'odd', optional=['out/'], accept=lambda path: 'out/late.json')
        self.assertIn('returns a list of strings', e.exception.code)

    def test_a_cached_complete_collection_short_by_the_predicate_is_collected_again(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        self.assertEqual(rc_.collect(tree, dest, optional=['out/'])['state'], 'complete')
        (self.base / 'a1/out/late.json').write_text('late')
        before = len(self.calls())
        col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual(self.ssh_ops(before), ['status', 'fetch'], 'collected again in the same call')
        self.assertEqual((col['state'], col['unaccepted']), ('complete', []))
        self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text()), col)
        self.assertEqual((dest / 'out/late.json').read_text(), 'late')

    def test_a_refresh_that_is_not_complete_keeps_the_cached_collection(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        held = rc_.collect(tree, dest, optional=['out/'])

        def unchanged():
            self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text()), held)
            self.assertEqual((dest / 'out/report.json').read_text(), 'from the rig')
        self.setenv(FAKE_SSH_DOWN="' fetch ")
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertIn('out/ transport', e.exception.code)
        self.assertIn('kept as it was', e.exception.code)
        unchanged()
        del os.environ['FAKE_SSH_DOWN']
        with self.assertRaises(RemoteError) as e:     # fetched whole, still short
            rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertIn('the predicate finds short (out/late.json)', e.exception.code)
        unchanged()
        (self.base / 'a1/out/late.json').write_text('late')
        col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((col['state'], col['unaccepted']), ('complete', []))
        self.assertEqual((dest / 'out/late.json').read_text(), 'late')
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock'])

    def test_collectors_with_a_predicate_never_strand_or_remove_a_complete_collection(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        held = rc_.collect(tree, dest, optional=['out/'])
        (self.base / 'a1/out/late.json').write_text('late')
        first = self.paused_collector(dest, 'accept', "optional=['out/']", self.LATE)
        # the first has fetched and holds the dest: the complete collection stays published meanwhile
        self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text()), held)
        self.assertTrue((dest / 'out/report.json').is_file())
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertIn('another collector holds', e.exception.code)
        self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text()), held)
        (self.tmp / 'resume').write_text('x')
        self.assertEqual(first.communicate(timeout=30)[0].strip(), 'complete')
        before = len(self.calls())
        col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((col['state'], self.ssh_ops(before)), ('complete', []))
        self.assertEqual((dest / 'out/late.json').read_text(), 'late')
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock'], 'nothing stranded')

    def test_a_failed_publication_keeps_the_collection_it_would_replace(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        held = rc_.collect(tree, dest, optional=['out/'])
        (self.base / 'a1/out/late.json').write_text('late')
        real = Path.rename

        def rename(src, target):
            if Path(target) == dest and src.name.endswith('.tmp'):
                raise OSError(5, 'Input/output error')
            return real(src, target)
        with mock.patch.object(Path, 'rename', rename), self.assertRaises(OSError):
            rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual(json.loads((dest / rc_.COLLECTION).read_text()), held)
        self.assertEqual((dest / 'out/report.json').read_text(), 'from the rig')
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock'])
        # interrupted with the collection set aside: the next collector puts it back
        dest.rename(self.tmp / '.got.old')
        self.assertEqual(rc_.collect(tree, dest, optional=['out/']), held)
        # interrupted after the replacement was published: the next collector drops what was set aside
        col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((col['state'], (dest / 'out/late.json').read_text()), ('complete', 'late'))
        (self.tmp / '.got.old/out').mkdir(parents=True)
        self.assertEqual(rc_.collect(tree, dest, optional=['out/'], accept=self.late), col)
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock'])

    def test_an_interrupt_after_the_replacement_landed_keeps_it(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        rc_.collect(tree, dest, optional=['out/'])
        (self.base / 'a1/out/late.json').write_text('late')
        real = Path.rename

        def rename(src, target):
            real(src, target)
            if Path(target) == dest and src.name.endswith('.tmp'):
                raise KeyboardInterrupt
        with mock.patch.object(Path, 'rename', rename), self.assertRaises(KeyboardInterrupt):
            rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((dest / 'out/late.json').read_text(), 'late')
        col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((col['state'], col['unaccepted']), ('complete', []))
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock'])

    def test_a_replaced_collection_that_cannot_be_removed_waits_for_the_next_collector(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        rc_.collect(tree, dest, optional=['out/'])
        (self.base / 'a1/out/late.json').write_text('late')
        real = rc_._remove

        def remove(p):
            if Path(p).name.startswith('.got.drop-'):
                raise PermissionError(13, 'Permission denied')
            return real(p)
        with mock.patch.object(rc_, '_remove', remove):
            col = rc_.collect(tree, dest, optional=['out/'], accept=self.late)
            self.assertEqual((col['state'], (dest / 'out/late.json').read_text()), ('complete', 'late'))
            self.assertEqual(rc_.collect(tree, dest, optional=['out/'], accept=self.late), col)
        self.assertEqual([p.name[:10] for p in self.tmp.glob('.got.[!c]*')], ['.got.drop-'])
        self.assertEqual(rc_.collect(tree, dest, optional=['out/'], accept=self.late), col)
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock'])

    def test_debris_removal_spares_a_sibling_that_only_shares_its_prefix(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        rc_.collect(tree, dest, optional=['out/'])
        (self.tmp / '.got.drop-backup').mkdir()
        (self.tmp / '.got.drop-backup/keep').write_text('mine')
        (self.base / 'a1/out/late.json').write_text('late')
        rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual((self.tmp / '.got.drop-backup/keep').read_text(), 'mine')
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock', '.got.drop-backup'])

    def test_a_partly_removed_collection_is_never_restored(self):
        tree = self.done_attempt()
        dest = self.tmp / 'got'
        rc_.collect(tree, dest, optional=['out/'])
        (self.base / 'a1/out/late.json').write_text('late')
        real = rc_._remove
        partly = []

        def remove(p):
            if Path(p).name == '.got.old' or Path(p).name.startswith('.got.drop-'):
                (Path(p) / 'out/report.json').unlink()
                partly.append(p)
                raise OSError(5, 'Input/output error')
            return real(p)
        with mock.patch.object(rc_, '_remove', remove):
            rc_.collect(tree, dest, optional=['out/'], accept=self.late)
        self.assertEqual(len(partly), 1)
        shutil.rmtree(dest)
        (self.base / 'a1/out/report.json').write_text('fetched again')
        col = rc_.collect(tree, dest, optional=['out/'])
        self.assertEqual((col['state'], (dest / 'out/report.json').read_text()), ('complete', 'fetched again'))
        self.assertEqual(sorted(p.name for p in self.tmp.glob('.got*')), ['.got.collect.lock'])

    def test_running_partial_then_dead_never_complete_nor_relaunched(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_WRITE='out/early.log', FAKE_LATE='out/report.json', FAKE_SLEEP='30')
        record = self.launch(tree)
        code, st = self.wait(tree, 0.5)
        self.assertEqual((code, st['state']), (rc_.RUNNING, 'running'))
        dest = self.tmp / 'got'
        groups = [('out/report.json',)]
        deadline = time.monotonic() + 10
        while not (self.base / 'a1/out/early.log').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        col = rc_.collect(tree, dest, groups, ['out/early.log'])
        self.assertEqual((col['state'], col['remoteState'], col['missing']), ('incomplete', 'running', [['out/report.json']]))
        self.assertTrue((dest / 'out/early.log').is_file(), 'partial evidence is kept, labelled incomplete')
        os.killpg(record['sid'], signal.SIGKILL)
        self.gone(record['pid'])
        self.gone(record['childPid'])
        code, st = self.wait(tree)
        self.assertEqual((code, st['state'], st['driverLock'], st['driverAlive']), (rc_.DEAD, 'dead', 'free', False))
        self.assertEqual(rc_.collect(tree, dest, groups)['state'], 'incomplete')
        with self.assertRaises(RemoteError):
            rc_.launch(tree, ['python3', 'run.py'])
        self.assertFalse((self.base / 'a1' / rc_.RECORDS / 'done.json').exists(), 'nothing inferred a completion')

    def test_a_driver_killed_while_its_command_lives_is_still_running(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_SLEEP='30')
        record = self.launch(tree)
        os.kill(record['pid'], signal.SIGKILL)
        self.gone(record['pid'])
        st = rc_.attempt_status(tree)
        self.assertEqual((st['state'], st['driverAlive']), ('running', False), 'the command still holds the driver lock')

    def test_done_with_a_missing_required_artifact_is_an_incomplete_transfer(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_WRITE='out/report.json')
        self.launch(tree)
        code, st = self.wait(tree, dest=self.tmp / 'got', groups=[('out/report.json', 'out/summary.json')])
        self.assertEqual(code, 0)
        self.assertEqual((st['collection']['state'], st['collection']['missing']),
                         ('incomplete', [['out/report.json', 'out/summary.json']]))
        self.assertFalse((self.tmp / 'got/out/report.json').exists())

    def test_partial_evidence_never_replaces_a_complete_collection(self):
        tree = self.create()
        self.stage(tree)
        self.setenv(FAKE_WRITE='out/report.json')
        self.launch(tree)
        dest = self.tmp / 'got'
        col = self.wait(tree, dest=dest, groups=[('out/report.json',)])[1]['collection']
        self.assertEqual(col['state'], 'complete')
        (self.base / 'a1/out/report.json').unlink()
        self.assertEqual(rc_.collect(tree, dest, [('out/report.json',)]), col)
        self.assertEqual((dest / 'out/report.json').read_text(), 'from the rig')

    def test_a_command_that_cannot_start_completes_with_its_error(self):
        tree = self.create()
        record = self.launch(tree, ['no-such-program-here'])
        self.assertIn('No such file', record['error'])
        code, st = self.wait(tree)
        self.assertEqual((code, st['done']['exit']), (0, 127))

    def test_a_claim_nothing_holds_is_dead_and_never_relaunched(self):
        # a launcher that died between its claim and the fork
        tree = self.create()
        (self.base / 'a1' / rc_.RECORDS / 'launch.claim').write_text('')
        code, st = self.wait(tree)
        self.assertEqual((code, st['state'], st['claimed']), (rc_.DEAD, 'dead', True))
        with self.assertRaises(RemoteError) as e:
            rc_.launch(tree, ['python3', 'run.py'])
        self.assertIn('was launched before', e.exception.code)

    def test_an_unreadable_launch_record_is_inconclusive(self):
        tree = self.create()
        for name in ('launch.claim', 'launch.json'):
            (self.base / 'a1' / rc_.RECORDS / name).write_text('')
        code, st = self.wait(tree, 0.3)
        self.assertEqual((code, st['state'], st['launch']), (rc_.INCONCLUSIVE, 'inconclusive', 'unreadable'))

    def test_an_unreachable_rig_is_inconclusive_and_cannot_be_collected(self):
        tree = self.create()
        self.setenv(FAKE_SSH_DOWN="' status ")
        code, st = self.wait(tree, 0.3)
        self.assertEqual((code, st['state']), (rc_.INCONCLUSIVE, 'unreachable'))
        with self.assertRaises(RemoteError) as e:
            rc_.collect(tree, self.tmp / 'got')
        self.assertIn('cannot reach rig', e.exception.code)
        self.assertFalse((self.tmp / 'got').exists())


if __name__ == '__main__':
    unittest.main()
