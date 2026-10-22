"""skills/hil/scripts/hil_flash.py builds and runs each flasher backend's commands from a
flasher dict, moved from tinyusb's test/hil/hil_flash.py (c5099be4f). The golden test replays
every flasher of that commit's tinyusb.json and hfp.json (copies in tests/data/hil_flash),
plus a pyOCD entry, and requires exactly what tinyusb's module produced
(tests/data/hil_flash/gen_golden.py): commands, J-Link scripts, cwd, timeouts, retries,
output and the result handed back. Only the pyOCD launcher path and its --project-dir
argument are normalised, plus deliberate deltas since, each applied explicitly: J-Link
command files are unique per invocation, reused by its crash reruns and removed after it
(jlink_command_file_delta), the J-Link flash script has no reset before `loadfile`
(jlink_flash_script_delta), esptool runs with NO_COLOR=1 (esptool_no_color_delta), every
command is the pre-move shell string's words as an argv list (argv_delta), and a flash or reset returns an Operation carrying the runner's results as its attempts, whose last
one is the result compared (provider_invoke). Equality there does not establish timing, signals or pyOCD's
project-directory behaviour; test_hil_process and test_hil_pyocd do."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_flash is Linux only')

import contextlib
import importlib.util
import io
import json
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'hil' / 'scripts'
DATA = Path(__file__).resolve().parent / 'data' / 'hil_flash'


def load(name, path):
    """By file path, as a project adapter loads it."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hf = load('agentrc_hil_flash', SCRIPTS / 'hil_flash.py')
gen = load('hil_flash_gen_golden', DATA / 'gen_golden.py')


def provider_invoke(case, run, ctx):
    """invoke_op's result unwrapped: an Operation must hold exactly the runner's results, in
    order, as its attempts and carry the last one's fields; that last result is compared."""
    returned = []

    def recording(cmd, **kw):
        returned.append(run(cmd, **kw))
        return returned[-1]
    ret = invoke_op(case, recording, ctx)
    if isinstance(ret, bool):
        return ret
    assert isinstance(ret, hf.Operation), ret
    assert len(ret.attempts) == len(returned) and all(a.result is r for a, r in zip(ret.attempts, returned))
    last = returned[-1]
    assert (ret.args, ret.returncode, ret.stdout, ret.stderr) == (last.args, last.returncode, last.stdout, last.stderr)
    return last


def invoke_op(case, run, ctx):
    """What a project adapter passes for each case (tinyusb: 2 J-Link crash retries, the
    build's IDF_TARGET, flash_args and serial port)."""
    f, op, t = case['flasher'], case['op'], case['timeout']
    name = f['name'].lower()
    if op == 'rescue':
        return hf.rescue_openocd(f, case['flash_out'], timeout=t, run=run)
    if name == 'esptool':
        return hf.flash_esptool(f, chip=gen.ESP_CHIP[case['board']], port=gen.ESP_PORT.format(uid=f['uid']),
                                flash_args=gen.ESP_FLASH_ARGS.strip().replace('\n', ' '),
                                cwd=str(ctx['fw_dir']), timeout=t, run=run)
    kw = {'jlink': {'crash_retries': 2},
          'pyocd': {'project_dir': str(ctx['project_dir'])}}.get(name, {})
    args = (gen.FIRMWARE,) if op == 'flash' else ()
    return getattr(hf, f'{op}_{name}')(f, *args, timeout=t, run=run, **kw)


COMMAND_FILE = '-CommandFile {COMMAND_FILE}'


def provider_normalise(cmd, ctx):
    if isinstance(cmd, list) and cmd[1:4] == [hf.PYOCD_LAUNCHER, '--project-dir', str(ctx['project_dir'])]:
        return [cmd[0], '{LAUNCHER}', *cmd[4:]]
    if isinstance(cmd, list) and cmd[-2:-1] == ['-CommandFile']:
        path = Path(cmd[-1])
        if path.parent == Path.cwd() and re.fullmatch(r'hil_jlink_\w+\.jlink', path.name):
            return [*cmd[:-1], '{COMMAND_FILE}']
    return cmd


def map_calls(observed, rewrite):
    return {**observed, 'calls': [rewrite(call) for call in observed['calls']]}


def jlink_command_file_delta(case, observed):
    """The pre-move module named the file `<board>_<firmware name>.jlink` (flash) or
    `<board>_reset.jlink` (reset, kept in the working directory); now each invocation gets a
    unique file in the working directory, reused by its crash reruns and removed after it."""
    if case['flasher']['name'].lower() != 'jlink' or case['op'] == 'rescue':
        return observed
    old = f"{case['board']}_{Path(gen.FIRMWARE).name if case['op'] == 'flash' else 'reset'}.jlink"

    def rewrite(call):
        head, name = call['cmd'].rsplit('-CommandFile ', 1)
        assert name == old, (name, old)
        return {**call, 'cmd': head + COMMAND_FILE}
    observed = map_calls(observed, rewrite)
    assert observed['left'] == ([old] if case['op'] == 'reset' else []), observed['left']
    return {**observed, 'left': []}


PRE_MOVE_FLASH_SCRIPT = f'halt\nr\nloadfile {gen.FIRMWARE}\nr\ngo\nexit\n'
FLASH_SCRIPT = f'halt\nloadfile {gen.FIRMWARE}\nr\ngo\nexit\n'


def jlink_flash_script_delta(case, observed):
    """Option B: the leading `r` of the pre-move J-Link flash script is dropped, as both
    projects' CMake jlink targets already run it; the reset script is unchanged."""
    if case['flasher']['name'].lower() != 'jlink' or case['op'] != 'flash':
        return observed

    def rewrite(call):
        assert call['script'] == PRE_MOVE_FLASH_SCRIPT, call['script']
        return {**call, 'script': FLASH_SCRIPT}
    return map_calls(observed, rewrite)


def esptool_no_color_delta(case, observed):
    """esptool runs with NO_COLOR=1 in its environment: output only, its arguments unchanged."""
    if case['flasher']['name'].lower() != 'esptool' or case['op'] == 'rescue':
        return observed

    def rewrite(call):
        assert call['cmd'].startswith('esptool '), call['cmd']
        return {**call, 'kw': {**call['kw'], 'env': {'NO_COLOR': '1'}}}
    return map_calls(observed, rewrite)


def argv_delta(case, observed):
    """No shell runs a command: each pre-move shell string is now its words, as the shell split
    them, in an argv list; none of the rosters' args used expansion, quoting aside."""
    def rewrite(call):
        cmd = call['cmd']
        return {**call, 'cmd': shlex.split(cmd) if isinstance(cmd, str) else cmd}
    return map_calls(observed, rewrite)


def deltas(case, observed):
    return argv_delta(case, esptool_no_color_delta(
        case, jlink_flash_script_delta(case, jlink_command_file_delta(case, observed))))


def setUpModule():
    guard_survivors(__name__)


class Golden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        golden = json.loads((DATA / 'golden.json').read_text())
        cls.observed = {tuple(c['case']): c['observed'] for c in golden['cases']}
        cls.expected = {gen.key(c): deltas(c, cls.observed[gen.key(c)]) for c in gen.cases()}

    def test_the_fixture_covers_every_case(self):
        self.assertEqual(list(self.observed), [gen.key(c) for c in gen.cases()])

    def test_every_roster_flasher_kind_is_covered(self):
        names = {f['name'].lower() for *_, f in gen.flashers()}
        self.assertEqual(names, {'jlink', 'stlink', 'openocd', 'lm4flash', 'esptool', 'pyocd'})

    def test_every_jlink_flash_case_carries_the_script_delta(self):
        flash = [k for k in self.observed if k[3] == 'flash' and any(
            c['script'] == PRE_MOVE_FLASH_SCRIPT for c in self.observed[k]['calls'])]
        jlink = [gen.key(c) for c in gen.cases() if c['flasher']['name'].lower() == 'jlink' and c['op'] == 'flash']
        self.assertEqual(flash, jlink)
        self.assertTrue(jlink)
        for k in jlink:
            self.assertEqual({c['script'] for c in self.expected[k]['calls']}, {FLASH_SCRIPT})

    def test_the_provider_matches_the_pre_move_module(self):
        with mock.patch.object(hf, '_pyocd_python', return_value='/py'):
            for case in gen.cases():
                with self.subTest(case=gen.key(case)):
                    self.assertEqual(gen.record(case, hf, provider_invoke, provider_normalise),
                                     self.expected[gen.key(case)])


def capture(fn, *args, rcs=(0,), took=0, **kw):
    """Run fn with run= answering rcs in turn, each call taking `took` s on a fake clock, in a
    fresh cwd; (result, calls, cwd listing, stdout)."""
    calls, rc_iter, now = [], iter(rcs), [0.0]

    def run(cmd, **k):
        calls.append((cmd, k))
        now[0] += took
        return subprocess.CompletedProcess(cmd, next(rc_iter), '', '')
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out, \
            mock.patch.object(hf, 'time', types.SimpleNamespace(monotonic=lambda: now[0])):
        os.chdir(d)
        try:
            ret = fn(*args, run=run, **kw)
            left = sorted(os.listdir(d))
        finally:
            os.chdir(cwd)
    return ret, calls, left, out.getvalue()


JLINK = {'name': 'jlink', 'uid': 'S1', 'args': '-device x'}
ABSENT = object()


class JLink(unittest.TestCase):
    PREFIX = ['JLinkExe', '-USB', 'S1', '-device', 'x', '-if', 'swd', '-JTAGConf', '-1,-1', '-speed', 'auto',
              '-NoGui', '1', '-ExitOnError', '1', '-CommandFile']

    def in_dir(self, d=None):
        """chdir into a fresh directory (or d) for the test; returns it."""
        d = d or self.enterContext(tempfile.TemporaryDirectory())
        cwd = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, cwd)
        return d

    def reading_run(self, seen, rcs=None, then=None):
        """A runner recording (path, script, files beside it) per call."""
        rcs = iter(rcs or [0] * 9)

        def run(cmd, **kw):
            self.assertEqual(cmd[:-1], self.PREFIX)
            path = cmd[-1]
            seen.append((path, Path(path).read_text(), sorted(os.listdir(Path(path).parent))))
            if then:
                then()
            return subprocess.CompletedProcess(cmd, next(rcs), '', '')
        return run

    def test_flash_writes_a_unique_script_in_the_cwd_runs_it_and_removes_it(self):
        d, seen = self.in_dir(), []
        hf.flash_jlink(JLINK, '/fw/a.hex', timeout=7, run=self.reading_run(seen))
        (path, script, beside), = seen
        self.assertEqual(Path(path).parent, Path(d).resolve())
        self.assertRegex(Path(path).name, r'^hil_jlink_\w+\.jlink$')
        self.assertEqual(script, 'halt\nloadfile /fw/a.hex\nr\ngo\nexit\n')
        self.assertEqual(beside, [Path(path).name])
        self.assertEqual(os.listdir(d), [])

    def test_flash_and_reset_take_a_workdir_for_their_command_file(self):
        self.in_dir()
        work, seen = tempfile.mkdtemp(dir=os.environ.get('TMPDIR')), []
        self.addCleanup(os.rmdir, work)
        hf.flash_jlink(JLINK, '/fw/a.hex', timeout=7, run=self.reading_run(seen), workdir=work)
        hf.reset_jlink(JLINK, timeout=7, run=self.reading_run(seen), workdir=work)
        self.assertEqual([Path(p).parent for p, _, _ in seen], [Path(work).resolve()] * 2)
        self.assertEqual(os.listdir(work), [])

    def test_reset_writes_its_script_every_time_and_removes_it(self):
        d, seen = self.in_dir(), []
        for _ in range(2):
            hf.reset_jlink(JLINK, timeout=11, run=self.reading_run(seen))
        self.assertEqual([s for _, s, _ in seen], ['halt\nr\ngo\nexit\n'] * 2)
        self.assertEqual(os.listdir(d), [])

    def test_a_stale_command_file_is_never_run_or_touched(self):
        d, seen = self.in_dir(), []
        stale = {'b_reset.jlink': 'erase\n', 'b_a.hex.jlink': 'erase\n'}
        for name, text in stale.items():
            Path(d, name).write_text(text)
        hf.reset_jlink(JLINK, timeout=11, run=self.reading_run(seen))
        hf.flash_jlink(JLINK, '/fw/a.hex', timeout=7, run=self.reading_run(seen))
        self.assertEqual([s for _, s, _ in seen], ['halt\nr\ngo\nexit\n', 'halt\nloadfile /fw/a.hex\nr\ngo\nexit\n'])
        self.assertNotIn(Path(seen[0][0]).name, stale)
        self.assertEqual({p.name: p.read_text() for p in Path(d).iterdir()}, stale)

    def test_concurrent_invocations_in_one_cwd_get_their_own_files(self):
        d, seen, errors = self.in_dir(), [], []
        both_running = threading.Barrier(2, timeout=10)

        def invoke(fn, *args):
            try:
                fn(JLINK, *args, timeout=7, run=self.reading_run(seen, then=both_running.wait))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=invoke, args=a) for a in ((hf.flash_jlink, '/fw/a.hex'),
                                                                      (hf.reset_jlink,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({p for p, _, _ in seen}), 2)
        self.assertEqual(sorted(s for _, s, _ in seen),
                         sorted(['halt\nr\ngo\nexit\n', 'halt\nloadfile /fw/a.hex\nr\ngo\nexit\n']))
        self.assertEqual(os.listdir(d), [])

    def test_the_file_is_removed_when_the_run_raises(self):
        for exc in (RuntimeError('boom'), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                d, seen = self.in_dir(), []

                def interrupted():
                    raise exc
                for fn, args in ((hf.flash_jlink, ('/fw/a.hex',)), (hf.reset_jlink, ())):
                    with self.assertRaises(type(exc)):
                        fn(JLINK, *args, timeout=7, crash_retries=2, run=self.reading_run(seen, then=interrupted))
                self.assertEqual(len(seen), 2)
                self.assertEqual(os.listdir(d), [])

    def test_crash_reruns_reuse_the_invocations_file_which_is_gone_afterwards(self):
        d, seen = self.in_dir(), []
        for fn, args in ((hf.flash_jlink, ('/fw/a.hex',)), (hf.reset_jlink, ())):
            seen.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                ret = fn(JLINK, *args, timeout=30, crash_retries=2, run=self.reading_run(seen, rcs=[139, -6, 0]))
            self.assertEqual(ret.returncode, 0)
            self.assertEqual(seen, [seen[0]] * 3)
            self.assertEqual(seen[0][2], [Path(seen[0][0]).name])
            self.assertEqual(os.listdir(d), [])

    def test_no_attempt_starts_past_the_deadline_taken_after_the_file_write(self):
        """Fake clock: writing the command file takes `prep` s, each run `took` s."""
        self.in_dir()
        real_command_file = hf._command_file

        def timed(fn, *args, prep, took, timeout, rcs):
            now, starts = [0.0], []

            @contextlib.contextmanager
            def slow_command_file(script, **kw):
                with real_command_file(script, **kw) as made:
                    now[0] += prep
                    yield made

            def run(cmd, timeout):
                starts.append((now[0], timeout))
                now[0] += took
                return subprocess.CompletedProcess(cmd, rcs[len(starts) - 1], '', '')
            with mock.patch.object(hf, '_command_file', slow_command_file), \
                    mock.patch.object(hf, 'time', types.SimpleNamespace(monotonic=lambda: now[0])), \
                    contextlib.redirect_stdout(io.StringIO()):
                fn(JLINK, *args, timeout=timeout, crash_retries=2, run=run)
            return starts
        for fn, args in ((hf.flash_jlink, ('/fw/a.hex',)), (hf.reset_jlink, ())):
            with self.subTest(fn=fn.__name__):
                # deadline = prep + timeout = 8: every attempt ends its bound there
                starts = timed(fn, *args, prep=3, took=1, timeout=5, rcs=[139, 139, 139])
                self.assertEqual(starts, [(3, 5), (4, 4), (5, 3)])
                # the first run spends the bound: no rerun past it
                self.assertEqual(timed(fn, *args, prep=3, took=5, timeout=5, rcs=[139, 139, 139]), [(3, 5)])

    def test_the_path_is_one_literal_argv_word(self):
        base = self.enterContext(tempfile.TemporaryDirectory())
        d = Path(base, "a b'$c")
        d.mkdir()
        seen = []
        self.in_dir(d)
        hf.reset_jlink(JLINK, timeout=11, run=self.reading_run(seen))
        self.assertEqual(Path(seen[0][0]).parent, d.resolve())
        self.assertEqual(os.listdir(d), [])

    def test_no_crash_rerun_by_default(self):
        ret, calls, _, _ = capture(hf.reset_jlink, JLINK, timeout=30, rcs=[139])
        self.assertEqual((ret.returncode, len(calls)), (139, 1))

    def test_crash_reruns_share_one_deadline(self):
        def timeouts(fn, *args, **kw):
            ret, calls, _, out = capture(fn, JLINK, *args, crash_retries=2, **kw)
            return ret.returncode, [k['timeout'] for _, k in calls], out.count('rerunning')
        self.assertEqual(timeouts(hf.flash_jlink, '/fw/a.hex', timeout=30, rcs=[139, -6, 0], took=10),
                         (0, [30, 20, 10], 2))
        self.assertEqual(timeouts(hf.reset_jlink, timeout=30, rcs=[134, -11, 139], took=1),
                         (139, [30, 29, 28], 2))
        # the bound is spent: no rerun
        self.assertEqual(timeouts(hf.reset_jlink, timeout=5, rcs=[139], took=5), (139, [5], 0))

    def test_other_failures_are_not_rerun(self):
        for rc in (1, 124):
            ret, calls, _, _ = capture(hf.reset_jlink, JLINK, timeout=5, crash_retries=2, rcs=[rc])
            self.assertEqual((ret.returncode, len(calls)), (rc, 1))

    def test_the_crash_rcs_are_shell_and_direct_signals(self):
        self.assertEqual(hf.JLINK_CRASH_RCS, {139, -11, 134, -6})


class Record(unittest.TestCase):
    """A flash or reset returns an Operation: CompletedProcess fields of its last run, plus
    every attempt with its own command, command file, output and rc."""

    def runner(self, results):
        """A runner answering (rc, stdout) or an exception in turn."""
        answers = iter(results)

        def run(cmd, **kw):
            rc = next(answers)
            if isinstance(rc, BaseException):
                raise rc
            text = Path(cmd[cmd.index('-CommandFile') + 1]).read_text() if '-CommandFile' in cmd else ''
            return subprocess.CompletedProcess(cmd, rc[0], f'{rc[1]}|{text}', None)
        return run

    def test_every_crash_rerun_is_its_own_attempt(self):
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), contextlib.redirect_stdout(io.StringIO()):
            op = hf.flash_jlink(JLINK, '/fw/a.hex', timeout=30, crash_retries=2,
                                run=self.runner([(139, 'S/N: 1'), (-6, 'double free'), (0, 'O.K.')]))
        script = 'halt\nloadfile /fw/a.hex\nr\ngo\nexit\n'
        self.assertIsInstance(op, subprocess.CompletedProcess)
        self.assertEqual([(a.result.returncode, a.result.stdout, a.script) for a in op.attempts],
                         [(139, f'S/N: 1|{script}', script), (-6, f'double free|{script}', script),
                          (0, f'O.K.|{script}', script)])
        self.assertEqual([a.cmd for a in op.attempts], [op.args] * 3)
        self.assertEqual((op.returncode, op.stdout), (0, f'O.K.|{script}'))
        self.assertEqual(op.retried, op.attempts[:2])
        self.assertFalse(any(a.timed_out or a.interrupted for a in op.attempts))

    def test_a_single_run_is_one_attempt_without_a_script(self):
        f = {'name': 'x', 'uid': 'S1', 'args': '', 'vid_pid': '0x0001 0x0002'}
        for fn, args, kw in ((hf.flash_stlink, ('fw',), {}), (hf.reset_stlink, (), {}),
                             (hf.flash_openocd, ('fw',), {}), (hf.reset_openocd, (), {}),
                             (hf.flash_lm4flash, ('fw',), {}),
                             (hf.flash_esptool, (), {'chip': 'c', 'port': 'p', 'flash_args': 'a', 'cwd': '/'})):
            with self.subTest(fn=fn.__name__):
                op = fn(f, *args, timeout=7, run=self.runner([(3, 'out')]), **kw)
                self.assertEqual(len(op.attempts), 1)
                a, = op.attempts
                self.assertEqual((a.cmd, a.script, a.result.returncode, a.result.stdout), (op.args, None, 3, 'out|'))
                self.assertEqual(op.retried, ())

    def test_a_returned_rc_124_is_timed_out_and_its_cleanup_unknown(self):
        op = hf.reset_stlink({'uid': 'S1'}, timeout=7, run=self.runner([(124, 'partial')]))
        a, = op.attempts
        self.assertEqual((a.kind, a.timed_out, a.interrupted, a.errored, a.error), ('timed_out', True, False, False, None))
        self.assertEqual((op.stdout, op.cleanup, a.cleanup, op.receipts), ('partial|', 'unknown', 'unknown', ()))

    def test_without_a_reported_cleanup_every_outcome_is_unknown(self):
        for rc in (0, 1, 124, 139):
            op = hf.reset_stlink({'uid': 'S1'}, timeout=7, run=self.runner([(rc, 'x')]))
            self.assertEqual((op.attempts[0].cleanup, op.cleanup), ('unknown', 'unknown'), rc)

    def raised(self, exc, before=()):
        """The operation a reset_jlink with 2 crash retries carries out on its exception."""
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(type(exc)) as cm:
            hf.reset_jlink(JLINK, timeout=30, crash_retries=2, run=self.runner([*before, exc]))
        self.assertIs(cm.exception, exc)
        return cm.exception.operation

    def test_a_raised_run_is_classified_and_carries_the_operation_so_far(self):
        class Interrupted(Exception):
            interrupted = True
        timeout = subprocess.TimeoutExpired('JLinkExe', 30, output=b'partial', stderr=b'err')
        for exc, kind in ((timeout, 'timed_out'), (KeyboardInterrupt(), 'interrupted'), (SystemExit(1), 'interrupted'),
                          (Interrupted(), 'interrupted'), (FileNotFoundError('JLinkExe'), 'error'),
                          (OSError('E2BIG'), 'error'), (RuntimeError('boom'), 'error')):
            with self.subTest(exc=type(exc).__name__):
                op = self.raised(exc, before=[(139, 'S/N: 1')])
                first, last = op.attempts
                self.assertEqual((first.kind, first.result.stdout), ('exited', 'S/N: 1|halt\nr\ngo\nexit\n'))
                self.assertEqual((last.kind, last.error, last.script, last.result.returncode),
                                 (kind, exc, 'halt\nr\ngo\nexit\n', None))
                self.assertEqual([last.timed_out, last.interrupted, last.errored],
                                 [kind == 'timed_out', kind == 'interrupted', kind == 'error'])
                self.assertEqual((op.returncode, op.cleanup), (None, 'unknown'))
                self.assertEqual((op.stdout, op.stderr), (b'partial', b'err') if exc is timeout else (None, None))

    @staticmethod
    def answers(*items):
        """A runner returning (rc, cleanup, receipt) in turn; cleanup ABSENT sets no attribute."""
        it = iter(items)

        def run(cmd, **kw):
            rc, cleanup, receipt = next(it)
            r = subprocess.CompletedProcess(cmd, rc, '', None)
            if cleanup is not ABSENT:
                r.cleanup = cleanup
            r.receipt = receipt
            return r
        return run

    def test_a_runner_supplies_cleanup_and_receipt(self):
        op = hf.reset_stlink({'uid': 'S1'}, timeout=7, run=self.answers((0, 'pending', {'pid': 41, 'start': 7})))
        self.assertEqual((op.attempts[0].cleanup, op.attempts[0].receipt), ('pending', {'pid': 41, 'start': 7}))
        self.assertEqual((op.cleanup, op.receipts), ('pending', ({'pid': 41, 'start': 7},)))
        exc = KeyboardInterrupt()
        exc.cleanup, exc.receipt = 'pending', 'holder-9'
        op = self.raised(exc)
        self.assertEqual((op.attempts[0].cleanup, op.cleanup, op.receipts), ('pending', 'pending', ('holder-9',)))

    def test_only_an_explicit_done_is_done(self):
        for value, want in (('done', 'done'), ('unknown', 'unknown'), ('pending', 'pending'), ('', 'unknown'),
                            (False, 'unknown'), (0, 'unknown'), (None, 'unknown'), ('DONE', 'unknown'),
                            (True, 'unknown'), (ABSENT, 'unknown')):
            op = hf.reset_stlink({'uid': 'S1'}, timeout=7, run=self.answers((0, value, None)))
            self.assertEqual(op.cleanup, want, value)

    def test_the_operation_cleanup_is_the_worst_attempts(self):
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), contextlib.redirect_stdout(io.StringIO()):
            op = hf.reset_jlink(JLINK, timeout=30, crash_retries=2,
                                run=self.answers((139, 'done', None), (139, 'unknown', None), (0, 'done', None)))
        self.assertEqual(([a.cleanup for a in op.attempts], op.cleanup), (['done', 'unknown', 'done'], 'unknown'))

    def test_no_crash_rerun_over_a_pending_session(self):
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), contextlib.redirect_stdout(io.StringIO()) as out:
            op = hf.reset_jlink(JLINK, timeout=30, crash_retries=2,
                                run=self.answers((139, 'pending', 'r1'), (0, 'done', None)))
        self.assertEqual(([(a.result.returncode, a.cleanup) for a in op.attempts], op.cleanup, op.receipts),
                         ([(139, 'pending')], 'pending', ('r1',)))
        self.assertNotIn('rerunning', out.getvalue())

    def test_no_crash_rerun_over_a_session_named_by_a_receipt(self):
        for cleanup in ('unknown', 'done', ABSENT):
            with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), contextlib.redirect_stdout(io.StringIO()):
                op = hf.reset_jlink(JLINK, timeout=30, crash_retries=2,
                                    run=self.answers((139, cleanup, 'r1'), (0, 'done', None)))
            self.assertEqual(([a.result.returncode for a in op.attempts], op.receipts), ([139], ('r1',)), cleanup)

    def test_an_unknown_cleanup_still_reruns_a_crash(self):
        """The default runner reports nothing, so every attempt is unknown: tinyusb's reruns stand."""
        for cleanup in ('unknown', ABSENT):
            with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), contextlib.redirect_stdout(io.StringIO()):
                op = hf.reset_jlink(JLINK, timeout=30, crash_retries=2,
                                    run=self.answers((139, cleanup, None), (-6, cleanup, None), (0, cleanup, None)))
            self.assertEqual([a.result.returncode for a in op.attempts], [139, -6, 0], cleanup)

    def test_the_default_runner_reports_unknown(self):
        """hil_process.run_cmd for real: a tool that exits 0 leaving a descendant behind, and one
        killed on its bound; run_cmd reports no cleanup, so both are unknown."""
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / 'lm4flash'
            tool.write_text('#!/bin/sh\ncase "$*" in *slow*) sleep 30;; esac\n'
                            'sleep 30 </dev/null >/dev/null 2>&1 &\necho $!\n')
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                exited = hf.flash_lm4flash({'uid': 'S1', 'args': ''}, 'fw.bin', timeout=10)
                killed = hf.flash_lm4flash({'uid': 'S1', 'args': ''}, 'slow.bin', timeout=0.5)
        survivor = int(exited.stdout)

        def reap():
            with contextlib.suppress(ProcessLookupError):
                os.kill(survivor, 9)
        self.addCleanup(reap)
        os.kill(survivor, 0)   # the descendant outlived the tool's exit
        self.assertEqual((exited.returncode, exited.attempts[0].kind, exited.cleanup), (0, 'exited', 'unknown'))
        self.assertEqual((killed.returncode, killed.attempts[0].kind, killed.cleanup), (124, 'timed_out', 'unknown'))


class Sessions(unittest.TestCase):
    """run_jlink_commands/run_stlink_commands/run_openocd_commands and the SYSRESETREQ operations
    built on them: the caller's connection profile, a unique command file in `workdir`, one raw
    attempt."""
    UF2 = {'name': 'jlink', 'uid': '000725000000', 'args': '-device MIMXRT1011DAE5A'}
    MAX32666 = {'name': 'openocd', 'uid': 'E6614103E7176A23', 'vid_pid': '0x2e8a 0x000c',
                'args': '-f interface/cmsis-dap.cfg -f target/max32665.cfg'}
    PROFILE = {'interface': 'SWD', 'speed': '4000', 'autoconnect': True, 'device': 'MIMXRT1011DAE5A'}

    def session(self, fn, *args, rc=0, workdir=None, **kw):
        """(operation, argv, command file path, its text, files beside it) of one call."""
        seen = []

        def run(argv, **k):
            path = argv[-1]
            seen.append((argv, k, path, Path(path).read_text(), sorted(os.listdir(Path(path).parent))))
            return subprocess.CompletedProcess(argv, rc, 'transcript', None)
        op = fn(*args, run=run, workdir=workdir, **kw)
        (argv, k, path, text, beside), = seen
        self.assertEqual(k, {'timeout': kw['timeout']})
        self.assertFalse(Path(path).exists())
        self.assertEqual(beside, [Path(path).name])
        self.assertEqual((op.args, op.returncode, op.stdout), (argv, rc, 'transcript'))
        self.assertEqual(len(op.attempts), 1)
        self.assertEqual(op.attempts[0].script, text)
        return op, argv, path, text

    def test_jlink_session_argv_and_script(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        _, argv, path, text = self.session(hf.run_jlink_commands, self.UF2, ['h', 'mem32 E000ED08 1', 'g'],
                                           workdir=d, timeout=120, **self.PROFILE)
        self.assertEqual(Path(path).parent, Path(d))
        self.assertRegex(Path(path).name, r'^hil_jlink_\w+\.jlink$')
        self.assertEqual(argv, ['JLinkExe', '-USB', '000725000000', '-device', 'MIMXRT1011DAE5A', '-if', 'SWD',
                                '-speed', '4000', '-autoconnect', '1', '-NoGui', '1', '-ExitOnError', '1',
                                '-CommandFile', path])
        self.assertEqual(text, 'h\nmem32 E000ED08 1\ng\nexit\n')

    def test_jlink_session_profile_is_the_callers(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        _, argv, _, _ = self.session(hf.run_jlink_commands, self.UF2, ['h'], workdir=d, timeout=5,
                                     interface='JTAG', speed=1000, autoconnect=False, device='Cortex-M4')
        self.assertEqual(argv[3:9], ['-device', 'Cortex-M4', '-if', 'JTAG', '-speed', '1000'])
        self.assertEqual(argv[9:13], ['-NoGui', '1', '-ExitOnError', '1'])
        with self.assertRaises(TypeError):
            hf.run_jlink_commands(self.UF2, ['h'], timeout=5, interface='SWD', speed=4000, autoconnect=True)

    def test_stlink_session_argv_and_script(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        _, argv, path, text = self.session(hf.run_stlink_commands, {'name': 'stlink', 'uid': '0035'},
                                           ['-halt', '-r32 0xE000ED08 1'], connect_mode='HOTPLUG', workdir=d,
                                           timeout=120)
        self.assertRegex(Path(path).name, r'^hil_stlink_\w+\.prg$')
        self.assertEqual(argv, ['STM32_Programmer_CLI', '-script', path])
        self.assertEqual(text, '-c port=SWD sn=0035 mode=HOTPLUG\n-halt\n-r32 0xE000ED08 1\n')

    def test_openocd_session_cmd_and_script(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        commands = ['halt', 'reg pc', 'mdw 0xE000ED08', 'mww 0x2008BFFC 0xf01669ef', 'reset run']
        _, cmd, path, text = self.session(hf.run_openocd_commands, self.MAX32666, commands, workdir=d, timeout=120)
        self.assertEqual(Path(path).parent, Path(d))
        self.assertRegex(Path(path).name, r'^hil_openocd_\w+\.tcl$')
        self.assertEqual(cmd, ['openocd', '-c', 'tcl_port disabled', '-c', 'gdb_port disabled',
                               '-c', 'telnet_port disabled', '-c', 'adapter serial E6614103E7176A23',
                               '-c', 'adapter usb vid_pid 0x2e8a 0x000c',
                               '-f', 'interface/cmsis-dap.cfg', '-f', 'target/max32665.cfg', '-f', path])
        self.assertEqual(text, 'init\nhalt\nreg pc\nmdw 0xE000ED08\nmww 0x2008BFFC 0xf01669ef\nreset run\nshutdown\n')

    def test_the_workdir_defaults_to_the_cwd(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        with contextlib.chdir(d):
            _, _, path, _ = self.session(hf.run_stlink_commands, {'uid': 'S'}, ['-halt'], connect_mode='UR',
                                         timeout=5)
        self.assertEqual(Path(path).parent, Path(d).resolve())

    def test_a_failed_or_crashed_session_is_one_raw_attempt(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        for rc in (1, 124, -11, 139):
            op, _, _, _ = self.session(hf.run_jlink_commands, self.UF2, ['h'], rc=rc, workdir=d, timeout=5,
                                       **self.PROFILE)
            self.assertEqual((op.returncode, op.retried, op.attempts[0].timed_out), (rc, (), rc == 124))

    def test_an_interrupted_session_removes_its_file(self):
        d = self.enterContext(tempfile.TemporaryDirectory())

        def run(argv, **k):
            raise KeyboardInterrupt
        for fn, f, kw in ((hf.run_jlink_commands, self.UF2, self.PROFILE),
                          (hf.run_stlink_commands, self.UF2, {'connect_mode': 'HOTPLUG'}),
                          (hf.run_openocd_commands, self.MAX32666, {})):
            with self.assertRaises(KeyboardInterrupt) as cm:
                fn(f, ['h'], timeout=5, run=run, workdir=d, **kw)
            self.assertTrue(cm.exception.operation.attempts[0].interrupted)
        self.assertEqual(os.listdir(d), [])

    def test_sysresetreq_is_tinyuf2s_sequence_byte_for_byte(self):
        """tinyuf2 hil_test.py at 7ab18b5: Battery.sysreset() runs Probe.run(['w4 E000ED0C 05FA0004'],
        judge=False) with its session profile; StlinkBattery.sysreset() runs StlinkProbe.run(
        ['-w32 0xE000ED0C 0x05FA0004 -nv']) after its HOTPLUG connect line."""
        d = self.enterContext(tempfile.TemporaryDirectory())
        _, argv, path, text = self.session(hf.sysresetreq_jlink, self.UF2, workdir=d, timeout=120, **self.PROFILE)
        self.assertEqual(argv, ['JLinkExe', '-USB', '000725000000', '-device', 'MIMXRT1011DAE5A', '-if', 'SWD',
                                '-speed', '4000', '-autoconnect', '1', '-NoGui', '1', '-ExitOnError', '1',
                                '-CommandFile', path])
        self.assertEqual(text.encode(), b'w4 E000ED0C 05FA0004\nexit\n')
        _, argv, path, text = self.session(hf.sysresetreq_stlink, {'name': 'stlink', 'uid': '0035'},
                                           connect_mode='HOTPLUG', workdir=d, timeout=120)
        self.assertEqual(argv, ['STM32_Programmer_CLI', '-script', path])
        self.assertEqual(text.encode(), b'-c port=SWD sn=0035 mode=HOTPLUG\n-w32 0xE000ED0C 0x05FA0004 -nv\n')

    def test_sysresetreq_openocd_writes_aircr_in_one_session(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        _, cmd, path, text = self.session(hf.sysresetreq_openocd, self.MAX32666, workdir=d, timeout=120)
        self.assertEqual(cmd, [*hf.openocd_cmd_base(self.MAX32666), '-f', path])
        self.assertEqual(text.encode(), b'init\nmww 0xE000ED0C 0x05FA0004\nshutdown\n')


class SessionRetry(unittest.TestCase):
    """run_jlink_commands' optional retry_if: the caller's judgement reruns the whole session,
    bounded, and never over an attempt whose cleanup is unresolved."""
    UF2 = Sessions.UF2
    PROFILE = {**Sessions.PROFILE, 'timeout': 9}

    def runner(self, answers, seen):
        """Each call answers (rc, cleanup, receipt) in turn, recording its command file's text."""
        it = iter(answers)

        def run(argv, **kw):
            rc, cleanup, receipt = next(it)
            seen.append((Path(argv[-1]).read_text(), kw))
            r = subprocess.CompletedProcess(argv, rc, f'rc {rc}', None)
            if cleanup is not None:
                r.cleanup = cleanup
            r.receipt = receipt
            return r
        return run

    def call(self, answers, **kw):
        seen, slept = [], []
        d = self.enterContext(tempfile.TemporaryDirectory())
        with mock.patch.object(hf, 'time', types.SimpleNamespace(monotonic=lambda: 0.0, sleep=slept.append)):
            op = hf.run_jlink_commands(self.UF2, ['h'], run=self.runner(answers, seen), workdir=d, **self.PROFILE,
                                       **kw)
        self.assertEqual(os.listdir(d), [])
        return op, seen, slept

    def test_no_retry_without_retry_if(self):
        op, seen, _ = self.call([(-11, 'done', None), (0, 'done', None)])
        self.assertEqual((len(op.attempts), op.returncode, op.retry_refused), (1, -11, None))

    def test_a_judged_failure_reruns_the_same_session_within_the_bound(self):
        judged = []

        def died_at_connect(attempt):
            judged.append(attempt.result.returncode)
            return attempt.result.returncode != 0
        op, seen, slept = self.call([(-11, 'done', None), (-11, 'done', None), (-11, 'done', None)],
                                    retry_if=died_at_connect, retries=2, retry_delay=2)
        self.assertEqual([a.result.returncode for a in op.attempts], [-11, -11, -11])
        self.assertEqual(judged, [-11, -11])
        self.assertEqual(slept, [2, 2])
        self.assertEqual([t for t, _ in seen], ['h\nexit\n'] * 3)
        self.assertEqual([k for _, k in seen], [{'timeout': 9}] * 3)
        self.assertIsNone(op.retry_refused)
        op, _, _ = self.call([(-11, 'done', None), (0, 'done', None)], retry_if=died_at_connect, retries=3)
        self.assertEqual([a.result.returncode for a in op.attempts], [-11, 0])

    def test_a_retry_over_an_unresolved_cleanup_is_refused(self):
        for cleanup, receipt in ((None, None), ('unknown', None), ('pending', None), ('done', '/r/receipt.json')):
            with self.subTest(cleanup=cleanup, receipt=receipt):
                op, seen, slept = self.call([(-11, cleanup, receipt), (0, 'done', None)],
                                            retry_if=lambda a: True, retries=2, retry_delay=1)
                self.assertEqual((len(op.attempts), len(seen), slept), (1, 1, []))
                self.assertIn(cleanup or 'unknown', op.retry_refused)
                self.assertEqual(op.returncode, -11)

    def test_the_bound_is_explicit_and_checked(self):
        for kw in ({'retries': -1}, {'retries': True}, {'retries': 1.5}, {'retry_delay': -1},
                   {'retry_delay': float('inf')}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.call([(0, 'done', None)], retry_if=lambda a: True, **kw)

    def test_a_raising_judgement_carries_the_operation(self):
        def boom(attempt):
            raise RuntimeError('judge')
        with self.assertRaises(RuntimeError) as cm:
            self.call([(-11, 'done', None)], retry_if=boom)
        self.assertEqual(len(cm.exception.operation.attempts), 1)

    def test_sysresetreq_forwards_the_retry(self):
        seen = []
        d = self.enterContext(tempfile.TemporaryDirectory())
        op = hf.sysresetreq_jlink(self.UF2, run=self.runner([(-6, 'done', None), (0, 'done', None)], seen),
                                  workdir=d, retry_if=lambda a: a.result.returncode < 0, **self.PROFILE)
        self.assertEqual([a.result.returncode for a in op.attempts], [-6, 0])


class Uid(unittest.TestCase):
    """A uid that is not one plain word is refused before any command file or spawn: every
    builder splices it into a shell line, a command file or a Tcl `-c` command."""
    BAD = ('0725;reboot', '0725 -x', '0725"', "0725'", '07\n25', '$(id)', '`id`', '', None, 725)
    PROFILE = {'interface': 'SWD', 'speed': '4000', 'autoconnect': True, 'device': 'X'}

    def builders(self, d):
        fw = {'firmware': 'fw.elf', 'timeout': 5}
        return {
            'flash_jlink': lambda f: hf.flash_jlink(f, workdir=d, **fw, run=self.run_),
            'reset_jlink': lambda f: hf.reset_jlink(f, workdir=d, timeout=5, run=self.run_),
            'run_jlink_commands': lambda f: hf.run_jlink_commands(f, ['h'], workdir=d, timeout=5, run=self.run_,
                                                                  **self.PROFILE),
            'flash_stlink': lambda f: hf.flash_stlink(f, **fw, run=self.run_),
            'reset_stlink': lambda f: hf.reset_stlink(f, timeout=5, run=self.run_),
            'run_stlink_commands': lambda f: hf.run_stlink_commands(f, ['-halt'], connect_mode='UR', workdir=d,
                                                                    timeout=5, run=self.run_),
            'flash_openocd': lambda f: hf.flash_openocd({**f, 'vid_pid': '0x1366 0x0101'}, **fw, run=self.run_),
            'reset_openocd': lambda f: hf.reset_openocd({**f, 'vid_pid': '0x1366 0x0101'}, timeout=5, run=self.run_),
            'run_openocd_commands': lambda f: hf.run_openocd_commands({**f, 'vid_pid': '0x2e8a 0x000c'}, ['halt'],
                                                                      workdir=d, timeout=5, run=self.run_),
            'sysresetreq_openocd': lambda f: hf.sysresetreq_openocd({**f, 'vid_pid': '0x2e8a 0x000c'}, workdir=d,
                                                                    timeout=5, run=self.run_),
            'flash_lm4flash': lambda f: hf.flash_lm4flash(f, **fw, run=self.run_),
            'flash_pyocd': lambda f: hf.flash_pyocd({**f, 'vid_pid': '0x0d28 0x0204'}, **fw, project_dir=d,
                                                    run=self.run_),
            'reset_pyocd': lambda f: hf.reset_pyocd({**f, 'vid_pid': '0x0d28 0x0204'}, project_dir=d, timeout=5,
                                                    run=self.run_),
        }

    def run_(self, cmd, **kw):
        self.ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '', None)

    def test_every_builder_refuses_a_uid_that_is_not_one_plain_word(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        for name, build in self.builders(d).items():
            for uid in self.BAD:
                with self.subTest(builder=name, uid=uid):
                    self.ran = []
                    with self.assertRaisesRegex(ValueError, 'want a probe serial'):
                        build({'name': name.split('_')[-1], 'uid': uid, 'args': '-f x.cfg'})
                    self.assertEqual((self.ran, os.listdir(d)), ([], []))

    def test_every_roster_serial_form_is_a_plain_word(self):
        for uid in ('000725000000', '0035', '30:ED:A0:E0:C9:02', 'E6614103E7176A23', 'a.b_c+d-e'):
            self.assertTrue(hf.UID_RE.fullmatch(uid), uid)


class StlinkLm4flashEsptool(unittest.TestCase):
    def test_an_explicit_stlink_verify_false_is_no_verify(self):
        # the golden rosters set stlink verify only to true or leave it out
        _, calls, _, _ = capture(hf.flash_stlink, {'name': 'stlink', 'uid': 'S1', 'verify': False}, 'fw.elf',
                                 timeout=7)
        self.assertEqual(calls, [(['STM32_Programmer_CLI', '--connect', 'port=swd', 'sn=S1', '--write', 'fw.elf', '--go'],
                                  {'timeout': 7})])

    def test_lm4flash_never_resets_alone(self):
        self.assertFalse(hasattr(hf, 'reset_lm4flash'))

    def test_esptool_resets_through_the_rom_loader_without_the_stub(self):
        seen = []

        def run(cmd, **kw):
            seen.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, '', None)
        hf.reset_esptool({'name': 'esptool', 'uid': 'U', 'args': '-b 1500000'}, port='/dev/x y', timeout=9, run=run)
        self.assertEqual(seen, [(['esptool', '-p', '/dev/x y', '-b', '1500000', '--no-stub', '--before=default_reset',
                                  '--after=hard_reset', 'chip_id'], {'env': {'NO_COLOR': '1'}, 'timeout': 9})])

    def test_esptool_runs_with_no_color_in_its_environment(self):
        """Through the provider runner's shell, with a colour-capable TERM: a fake esptool on PATH."""
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / 'esptool'
            tool.write_text('#!/bin/sh\necho "NO_COLOR=$NO_COLOR TERM=$TERM args: $*"\n')
            tool.chmod(0o755)
            env = {'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}', 'TERM': 'xterm-256color'}
            with mock.patch.dict(os.environ, env), mock.patch.dict(os.environ):
                os.environ.pop('NO_COLOR', None)
                r = hf.flash_esptool({'name': 'esptool', 'uid': 'U', 'args': '-b 1500000'}, chip='esp32s3',
                                     port='/dev/null', flash_args='0x0 a.bin', cwd=d, timeout=10)
                self.assertNotIn('NO_COLOR', os.environ)
        self.assertEqual((r.returncode, r.stdout),
                         (0, 'NO_COLOR=1 TERM=xterm-256color args: --chip esp32s3 -p /dev/null -b 1500000 '
                             '--before=default_reset --after=hard_reset write_flash 0x0 a.bin\n'))

    def test_esptool_inputs_are_checked_before_a_lock(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('bootloader.bin', 'app.bin'):
                (Path(d) / name).write_bytes(b'x')
            args = '--flash_mode dio --flash_freq=80m 0x0 bootloader.bin 0x10000 app.bin'
            self.assertEqual(hf.esptool_input_problem('esp32s3', args, d), '')
            for chip, flash_args, cwd, why in (
                    ('', args, d, 'chip'), ('esp32; reboot', args, d, 'chip'), (None, args, d, 'chip'),
                    ('esp32', args, str(Path(d) / 'nope'), 'not a directory'), ('esp32', args, None, 'not a directory'),
                    ('esp32', '0x0 "unterminated', d, 'flash_args'), ('esp32', '--flash_mode dio', d, 'pairs'),
                    ('esp32', '0x0 app.bin 0x1000', d, 'pairs'), ('esp32', 'zero app.bin', d, "offset 'zero'"),
                    ('esp32', '0x0 gone.bin', d, 'gone.bin, which is missing')):
                with self.subTest(chip=chip, flash_args=flash_args, cwd=cwd):
                    self.assertIn(why, hf.esptool_input_problem(chip, flash_args, cwd))

    def test_timeout_is_explicit_everywhere(self):
        for fn, args in ((hf.flash_stlink, ({}, 'fw')), (hf.reset_stlink, ({},)), (hf.flash_openocd, ({}, 'fw')),
                         (hf.reset_openocd, ({},)), (hf.flash_lm4flash, ({}, 'fw')),
                         (hf.flash_jlink, ({}, 'fw')), (hf.reset_jlink, ({},)), (hf.rescue_openocd, ({},))):
            with self.subTest(fn=fn.__name__), self.assertRaises(TypeError):
                fn(*args)


ODD = 'a b;$(touch PWNED)`id`*'   # spaces and shell metacharacters, none of which may act


JLINK_CORPUS = DATA / 'jlink'


def jlink_fixture(name):
    return (JLINK_CORPUS / name).read_text()


class JLinkJudgment(unittest.TestCase):
    """Each J-Link command judged from its own chunk of a recorded transcript (MANIFEST.json: host,
    board, device, JLinkExe version and the command file each was recorded with)."""
    FLASH = 'V9.78-imxrt1064_evk-halt-loadfile-r-go.txt'
    RESET = 'V9.78-nrf52840dk-halt-r-go.txt'

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((JLINK_CORPUS / 'MANIFEST.json').read_text())

    def commands(self, name):
        return self.manifest[name]['commands']

    def test_every_recorded_session_completes(self):
        self.assertGreaterEqual({m['version'] for m in self.manifest.values()}, {'V9.78', 'V9.84'})
        self.assertTrue(any('Skipped. Contents already match' in jlink_fixture(n) for n in self.manifest),
                        'an already-matching image is recorded too')
        for name, m in self.manifest.items():
            with self.subTest(name):
                self.assertEqual(hf.jlink_judgment(jlink_fixture(name), m['commands']),
                                 {'ok': True, 'problem': '', 'commands': mock.ANY})

    def test_a_failed_chunk_fails_whatever_the_rest_says(self):
        good = jlink_fixture(self.FLASH)
        cases = {'no probe, exit 0': ('J-Link>halt\nJ-Link connection not established yet but required for command.\n'
                                      'Connecting to J-Link ...FAILED: Cannot connect to J-Link.\n', 'echoed'),
                 'a failed reset outlived': (re.sub(r'(?s)J-Link>r\n.*?J-Link>go', 'J-Link>r\nReset delay: 0 ms\n'
                                                    'ResetTarget() start\nResetTarget failed (-1).\nJ-Link>go', good),
                                             "'r' failed"),
                 'loadfile without O.K.': (good.replace('O.K.\n', ''), 'did not complete'),
                 'a DLL error in go': (good.replace('J-Link>exit', '****** Error: CPU is not halted\nJ-Link>exit'),
                                       "'go' failed"),
                 'no exit': (good.replace('J-Link>exit\n', ''), 'echoed'),
                 'cut short': (good.replace('Script processing completed.', ''), "'exit' did not complete"),
                 'an extra echo': (good.replace('J-Link>exit', 'J-Link>go\nJ-Link>exit'), 'echoed'),
                 'an echo without its arguments': (good.replace('J-Link>go', 'J-Link>sleep\nJ-Link>go'), 'echoed'),
                 'nothing': ('', 'echoed')}
        for what, (out, why) in cases.items():
            with self.subTest(what):
                j = hf.jlink_judgment(out, self.commands(self.FLASH))
                self.assertFalse(j['ok'])
                self.assertIn(why, j['problem'])

    def test_a_failed_verify_ends_the_script_and_fails(self):
        name = 'V9.84-metro_m7_1011-h-verifybin-r-g.txt'
        out = jlink_fixture(name)
        cut = out[:out.index('Verify successful.')] + 'Verify failed @ address 0x60000400.\nERROR: Verify failed.\n' \
            'Script processing completed.\n'
        self.assertIn('echoed', hf.jlink_judgment(cut, self.commands(name))['problem'])

    def test_repeated_commands_are_judged_per_occurrence(self):
        name = 'V9.78-lpcxpresso55s69-h-verifybin-verifybin-r-g.txt'
        cmds = self.commands(name)
        self.assertEqual(sum(c.startswith('verifybin') for c in cmds), 2)
        self.assertTrue(hf.jlink_completed(jlink_fixture(name), cmds))
        once = [c for i, c in enumerate(cmds) if not (c.startswith('verifybin') and i == cmds.index(c))]
        self.assertFalse(hf.jlink_completed(jlink_fixture(name), once))

    def test_a_command_without_a_completion_rule_never_passes(self):
        out = 'J-Link>erase\nErasing done.\nJ-Link>exit\nScript processing completed.\n'
        self.assertEqual(hf.jlink_judgment(out, ['erase'])['problem'], "'erase' has no completion rule")

    def test_reset_after_target_hooks(self):
        """A device script's AfterResetTarget() closes `r` (MK64, LPC4088); nothing else may."""
        out = jlink_fixture('V9.78-frdm_k64f-halt-r-go.txt')
        self.assertTrue(hf.jlink_completed(out, ['halt', 'r', 'go']))
        self.assertFalse(hf.jlink_completed(out.replace('AfterResetTarget() end', 'Something else\nAfterResetTarget() end'),
                                            ['halt', 'r', 'go']))

    def test_the_problem_of_an_operation_is_its_last_attempt_judged_against_its_command_file(self):
        good = jlink_fixture(self.RESET)

        def op(*outs, script='halt\nr\ngo\nexit\n'):
            attempts = [hf._record(['JLinkExe'], script, subprocess.CompletedProcess([], rc, out, ''), err)
                        for rc, out, err in outs]
            return hf.Operation(attempts)
        self.assertEqual(hf.jlink_problem(op((0, good, None))), '')
        self.assertEqual(hf.jlink_problem(op((0, good, None), (-11, '', None))), 'JLinkExe exit -11')
        self.assertIn('echoed', hf.jlink_problem(op((-11, good, None), (0, '', None))), 'an earlier attempt never counts')
        self.assertEqual(hf.jlink_problem(op((1, good, None))), 'JLinkExe exit 1')
        self.assertEqual(hf.jlink_problem(op((None, good, subprocess.TimeoutExpired('JLinkExe', 5)))),
                         'JLinkExe timed out')
        self.assertIn('echoed', hf.jlink_problem(op((0, good, None), script='halt\nloadfile /x/a.hex\nr\ngo\nexit\n')))
        self.assertEqual(hf.jlink_problem(op((0, good, None), script=None)),
                         'no command file ending in exit to judge against')

    def test_the_providers_scripts_are_judged_as_they_run(self):
        """flash_jlink and reset_jlink's own command files are the recorded ones."""
        for fn, name, extra in ((hf.flash_jlink, self.FLASH, ('/x/tinyuf2.hex',)), (hf.reset_jlink, self.RESET, ())):
            with self.subTest(fn.__name__):
                out = jlink_fixture(name)
                if extra:
                    out = re.sub(r'loadfile \S+', f'loadfile {extra[0]}', out)
                with tempfile.TemporaryDirectory() as d:
                    op = fn(JLINK, *extra, timeout=5, workdir=d,
                            run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ''))
                self.assertEqual(hf.jlink_problem(op), '')


ESPTOOL_CORPUS = DATA / 'esptool'


def esptool_synthetic(offsets, *, stub=True, uncompressed=False):
    """A write_flash transcript in the shape tinyuf2's bench recorded with esptool 5.2.0 (CSI
    codes, the underscore spellings' warning); without the stub, or a stub run that found
    compression would not shrink a segment (5.3.1), each write uncompressed."""
    out = "\x1b[0;33mWarning:\x1b[0m Deprecated: Command 'write_flash' is deprecated. Use 'write-flash' instead.\n"
    for off in offsets:
        if stub and uncompressed:
            out += 'Note: Cannot compress \'x.bin\' more than the original size, will flash uncompressed.\n'
        out += (f'Writing at 0x{off:08x} [====] 100.0% \x1b[1A\x1b[2K\nWrote 9 bytes '
                f'{"(9 compressed) " if stub and not uncompressed else ""}at 0x{off:08x} in 0.1 seconds (100.0 kbit/s).\n'
                'Verifying written data...\nHash of data verified.\n')
    return out + '\nHard resetting via RTS pin...\n'


class EsptoolJudgment(unittest.TestCase):
    OFFSETS = [0x0, 0x8000, 0xe000, 0x410000]

    def test_recorded_and_documented_forms_pass(self):
        manifest = json.loads((ESPTOOL_CORPUS / 'MANIFEST.json').read_text())
        for name, m in manifest.items():
            with self.subTest(name):
                self.assertEqual(hf.esptool_flash_judgment((ESPTOOL_CORPUS / name).read_text(), m['offsets']),
                                 {'ok': True, 'problem': '', 'wrote': m['offsets']})
        for form, kw in (('stub', {}), ('--no-stub', {'stub': False}), ('uncompressed', {'uncompressed': True})):
            with self.subTest(form):
                out = esptool_synthetic(self.OFFSETS, **kw)
                self.assertTrue(hf.esptool_flash_judgment(out, self.OFFSETS)['ok'])
                self.assertTrue(hf.esptool_flash_judgment(out.replace('Wrote', '\x1b[2K\x1b[1AWrote'), self.OFFSETS)['ok'])

    def test_every_write_needs_its_own_verify_and_the_reset_follows(self):
        out = (ESPTOOL_CORPUS / '5.3.1-espressif_esp32s3_devkitm_1-stub.txt').read_text()
        first = out.index('Hash of data verified.')
        moved = out[:first] + out[first + len('Hash of data verified.'):]
        moved = moved.replace('Hard resetting', 'Hash of data verified.\nHard resetting', 1)
        last = out.rindex('Hash of data verified.')
        past_reset = out[:last] + out[last + len('Hash of data verified.'):] + 'Hash of data verified.\n'
        cases = {'a write missing': (out.replace('at 0x00008000 in', 'at 0x00009000 in'), 'not the requested'),
                 'a write twice': (out.replace('at 0x0000e000 in', 'at 0x00008000 in'), 'not the requested'),
                 'an extra write': (out + 'Wrote 1 bytes at 0x00500000 in 0.1 seconds.\n', 'not the requested'),
                 'an unknown write line': (out + 'Wrote 1 bytes at 0x500000 in 0.1 seconds.\n', 'unknown shape'),
                 'a verify missing': (out.replace('Hash of data verified.\n', '', 1), 'no verify between the write at 0x0 '),
                 'a verify moved to the last write': (moved, 'no verify between the write at 0x0 '),
                 'the last verify past the reset': (past_reset, 'no verify between the write at 0x410000 '),
                 'no hard reset': (out.replace('Hard resetting via RTS pin...', ''), 'no hard reset'),
                 'a fatal error': (out + 'A fatal error occurred: Packet content transfer stopped\n', 'fatal error'),
                 'not reset': (out.replace('Hard resetting via RTS pin...', 'Chip was NOT reset.'), 'NOT reset'),
                 'an unknown format': ('Flashed 4 regions OK\n', 'not the requested'),
                 'nothing': ('', 'not the requested')}
        for what, (text, why) in cases.items():
            with self.subTest(what):
                j = hf.esptool_flash_judgment(text, self.OFFSETS)
                self.assertFalse(j['ok'])
                self.assertIn(why, j['problem'])
        self.assertEqual(hf.esptool_flash_judgment(out, [])['problem'], 'no offsets requested')
        twice = esptool_synthetic([0, 0])
        self.assertIn('repeat', hf.esptool_flash_judgment(twice, [0, 0])['problem'])

    def test_the_problem_of_a_flash_is_its_last_attempt_judged(self):
        good = esptool_synthetic(self.OFFSETS)

        def op(*outs):
            return hf.Operation([hf._record(['esptool'], None, subprocess.CompletedProcess([], rc, out, ''), err)
                                 for rc, out, err in outs])
        self.assertEqual(hf.esptool_problem(op((0, good, None)), self.OFFSETS), '')
        self.assertEqual(hf.esptool_problem(op((2, good, None)), self.OFFSETS), 'esptool exit 2')
        self.assertEqual(hf.esptool_problem(op((None, good, subprocess.TimeoutExpired('esptool', 5))), self.OFFSETS),
                         'esptool timed out')
        self.assertIn('not the requested', hf.esptool_problem(op((0, good, None), (0, '', None)), self.OFFSETS))

    def test_the_offsets_are_the_flash_args_pairs(self):
        self.assertEqual(hf.esptool_offsets('--flash-mode dio --flash-size 8MB 0x0 b.bin 0x8000 p.bin'), [0, 0x8000])
        with self.assertRaises(ValueError):
            hf.esptool_offsets('--flash-mode dio')


class EsptoolRuntime(unittest.TestCase):
    """One esptool: the executable on PATH and its interpreter's package report one version."""

    def setUp(self):
        self.d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.d / 'pkg' / 'esptool').mkdir(parents=True)
        self.env = self.enterContext(mock.patch.dict(os.environ, {'PYTHONNOUSERSITE': '1', 'PYTHONPATH': ''}))

    def exe(self, says='9.9.9', shebang=None, package='9.9.9', name='esptool'):
        if package is not None:
            (self.d / 'pkg' / 'esptool' / '__init__.py').write_text(f'__version__ = {package!r}\n')
            os.environ['PYTHONPATH'] = str(self.d / 'pkg')
        exe = self.d / name
        exe.write_text(f'{shebang or "#!" + sys.executable}\nprint("esptool v{says}")\nprint("{says}")\n')
        exe.chmod(0o755)
        return str(exe)

    def test_one_package_and_its_version(self):
        exe = self.exe()
        self.assertEqual(hf.esptool_runtime(exe), {'exe': exe, 'python': sys.executable, 'version': '9.9.9',
                                                   'package': str(self.d / 'pkg' / 'esptool' / '__init__.py')})

    def test_the_package_is_the_one_the_script_imports(self):
        (self.d / 'esptool').mkdir()
        (self.d / 'esptool' / '__init__.py').write_text("__version__ = '9.9.9'\n")   # beside it: first on its path
        exe = self.exe(name='esptool-cli')
        self.assertEqual(hf.esptool_runtime(exe)['package'], str(self.d / 'esptool' / '__init__.py'))
        with contextlib.chdir(self.d):
            self.assertEqual(hf.esptool_runtime('./esptool-cli')['exe'], exe)

    def test_a_failed_or_malformed_import_probe_is_refused(self):
        said = subprocess.CompletedProcess([], 0, 'esptool v9.9.9\n', '')
        for what, probe in (('exit 1', subprocess.CompletedProcess([], 1, '["9.9.9", "/p"]\n', 'boom')),
                            ('no package path', subprocess.CompletedProcess([], 0, '["9.9.9", null]\n', '')),
                            ('not a pair', subprocess.CompletedProcess([], 0, '"9.9.9"\n', ''))):
            with self.subTest(what), mock.patch.object(hf.subprocess, 'run', side_effect=[probe, said]), \
                    self.assertRaisesRegex(RuntimeError, 'imports no esptool package'):
                hf.esptool_runtime(self.exe())

    def test_refusals(self):
        cases = (('a mismatched executable', {'says': '1.0'}, 'does not report 9.9.9'),
                 ('no python shebang', {'shebang': '#!/bin/sh'}, 'want an absolute python shebang'))
        for what, kw, why in cases:
            with self.subTest(what), self.assertRaisesRegex(RuntimeError, why):
                hf.esptool_runtime(self.exe(**kw))
        with self.assertRaisesRegex(RuntimeError, 'is not on PATH'):
            hf.esptool_runtime(str(self.d / 'missing'))
        unreadable = self.exe(name='esptool-unreadable')
        os.chmod(unreadable, 0o111)
        if not os.access(unreadable, os.R_OK):   # root reads it anyway
            with self.assertRaisesRegex(RuntimeError, 'Permission denied'):
                hf.esptool_runtime(unreadable)
        with mock.patch.object(hf.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 1)), \
                self.assertRaisesRegex(RuntimeError, 'timed out'):
            hf.esptool_runtime(self.exe())

    def test_no_package_for_its_interpreter(self):
        exe = self.exe(package=None)
        probe = subprocess.run([sys.executable, '-c', 'import esptool'], capture_output=True)
        if probe.returncode == 0:
            self.skipTest('this interpreter has an esptool package outside the user site')
        with self.assertRaisesRegex(RuntimeError, 'imports no esptool package'):
            hf.esptool_runtime(exe)


class EsptoolPort(unittest.TestCase):
    def setUp(self):
        self.byid = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(hf, 'SERIAL_BY_ID', str(self.byid)))

    def link(self, name, target='/dev/null'):
        (self.byid / name).symlink_to(target)

    def test_one_bridge_link_to_a_character_device(self):
        self.link('usb-Silicon_Labs_CP2102N_USB_to_UART_abc123-if00-port0')
        self.link('usb-Silicon_Labs_CP2102N_USB_to_UART_abc1234-if00-port0')   # another serial with this prefix
        self.assertEqual(hf.esptool_port('abc123'), '/dev/null')

    def test_refusals(self):
        regular = self.byid.parent / 'regular'
        regular.write_text('')
        self.addCleanup(regular.unlink)
        cases = (('none', [], 'found none'),
                 ('two interfaces', [('usb-FTDI_Dual_RS232_ab1-if00-port0', '/dev/null'),
                                     ('usb-FTDI_Dual_RS232_ab1-if01-port0', '/dev/zero')], 'found ['),
                 ('dangling', [('usb-x_ab1-if00-port0', '/nonexistent/tty')], 'No such file'),
                 ('not a device', [('usb-x_ab1-if00-port0', str(regular))], 'not a character device'))
        for what, links, why in cases:
            with self.subTest(what):
                for p in self.byid.iterdir():
                    p.unlink()
                for name, target in links:
                    self.link(name, target)
                with self.assertRaisesRegex(RuntimeError, re.escape(why)):
                    hf.esptool_port('ab1')
        for uid in ('', 'a*b', 'a[1]', None):
            with self.subTest(uid=uid), self.assertRaisesRegex(RuntimeError, 'want a probe serial'):
                hf.esptool_port(uid)

    def test_a_usj_serial_goes_to_usj_tty_only(self):
        seen = []
        fake = types.SimpleNamespace(usj_tty=lambda serial: seen.append(serial) or '/dev/ttyACM3')
        with mock.patch.object(hf.hil_load, 'load_file', lambda rel: fake if rel.endswith('hil_openocd_esp.py')
                               else self.fail(rel)):
            self.assertEqual(hf.esptool_port('30:ED:A0:E0:C9:02'), '/dev/ttyACM3')
            self.link('usb-x_ab1-if00-port0')
            self.assertEqual(hf.esptool_port('ab1'), '/dev/null')
        self.assertEqual(seen, ['30:ED:A0:E0:C9:02'])


class ResetProblem(unittest.TestCase):
    """A reset that exited 0 counts only with the target's answer in its output."""

    @staticmethod
    def op(stdout='', stderr=None):
        return subprocess.CompletedProcess([], 0, stdout, stderr)

    def test_each_backend_needs_its_contact_line(self):
        cases = [('jlink', 'Found SW-DP with ID 0x2BA01477\nCortex-M4 identified.\n', ''),
                 ('JLink', 'J-Link>go\nScript processing completed.\n', 'no line shows the target answering'),
                 ('stlink', 'Device ID   : 0x413\n', ''),
                 ('stlink', 'ST-LINK SN  : P\n', 'no line shows the target answering'),
                 ('openocd', 'Info : [rp2040.core0] Examination succeed\n', ''),
                 ('openocd', 'Info : wlink_init ok\nInfo : [riscv.cpu] Examined RISC-V core\n', ''),
                 ('openocd', 'Info : Listening on port 3333\n', 'no line shows the target answering'),
                 ('pyocd', '', ''),
                 ('esptool', 'Hard resetting via RTS pin...\n', '')]
        for name, out, why in cases:
            with self.subTest(name=name, out=out):
                got = hf.reset_problem(name, self.op(out))
                self.assertEqual(bool(got), bool(why), got)
                self.assertIn(why, got)

    def test_a_refuting_line_wins_whatever_else_answered(self):
        cases = [('jlink', 'Cortex-M4 identified.\nReset: CPU may not have been reset (DHCSR.S_RESET_ST never gets set).'),
                 ('openocd', 'Info : SWD DPIDR 0x0bc12477\nWarn : target max32665.cpu examination failed'),
                 ('openocd', 'Info : [rp2040.core0] Examination succeed\n'
                             'Error: [rp2040.core0] Execution of event reset-assert failed:\nboom'),
                 ('pyocd', 'Core 0 is not accessible after reset'),
                 ('pyocd', '0000123 E Invalid reset method: invalid [reset_cmd]'),
                 ('esptool', 'Chip was NOT reset. Setting RTS failed')]
        for name, out in cases:
            with self.subTest(name=name, out=out):
                self.assertIn('the output says', hf.reset_problem(name, self.op(stderr=out.encode())))

class LiteralArgv(unittest.TestCase):
    """Every built command is an argv list and its environment a mapping: no shell parses a path,
    a roster arg or a flash_args file name, so spaces and metacharacters arrive as written."""

    def tools(self, d, *names):
        """Fake tools on PATH printing each argument on its own line."""
        bin_dir = Path(d) / 'bin dir'
        bin_dir.mkdir()
        for name in names:
            tool = bin_dir / name
            tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            tool.chmod(0o755)
        return bin_dir

    def test_a_directory_qualified_rescue_target_keeps_its_directory(self):
        """The pre-argv rescue replaced the cfg inside the args string, so a qualified path kept its
        directory: the argv must be that baseline's words."""
        for cfg, pre, post in (('target/rp2350.cfg', [], ['-c', 'shutdown']),
                               ('target/rp2040.cfg', ['-c', 'set RESCUE 1'], [])):
            qualified = f'/opt/openocd/scripts/{cfg}'
            rescue = {'name': 'openocd', 'uid': 'S1', 'args': f'-f interface/cmsis-dap.cfg -f {qualified}'}
            baseline_args = rescue['args'].replace(cfg, hf.RESCUE_CFG[cfg][0])
            _, calls, _, _ = capture(hf.rescue_openocd, rescue, hf.DAP_WEDGED[0], timeout=7)
            with self.subTest(cfg=cfg):
                self.assertEqual(calls[0][0], [*hf.openocd_cmd_base({**rescue, 'args': ''}), *pre,
                                               *shlex.split(baseline_args), *post])
                self.assertIn(f'/opt/openocd/scripts/{hf.RESCUE_CFG[cfg][0]}', calls[0][0])

    def test_roster_args_are_split_as_words_never_expanded(self):
        _, calls, _, _ = capture(hf.flash_lm4flash, {'name': 'lm4flash', 'uid': 'S1', 'args': "-v '$HOME x' *"},
                                 ODD, timeout=7)
        self.assertEqual(calls, [(['lm4flash', '-s', 'S1', '-v', '$HOME x', '*', ODD], {'timeout': 7})])

    def test_paths_with_spaces_and_metacharacters_reach_the_tool_literally(self):
        with tempfile.TemporaryDirectory() as d:
            bin_dir = self.tools(d, 'STM32_Programmer_CLI', 'lm4flash')
            with mock.patch.dict(os.environ, {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'}):
                st = hf.flash_stlink({'name': 'stlink', 'uid': 'S1'}, ODD, timeout=10, run=quiet_run(d))
                lm = hf.flash_lm4flash({'name': 'lm4flash', 'uid': 'S1', 'args': "'-x y'"}, ODD, timeout=10,
                                       run=quiet_run(d))
            self.assertNotIn('PWNED', os.listdir(d))
        self.assertEqual(st.stdout.splitlines(), ['--connect', 'port=swd', 'sn=S1', '--write', ODD, '--go'])
        self.assertEqual(lm.stdout.splitlines(), ['-s', 'S1', '-x y', ODD])

    def test_esptool_takes_its_executable_and_files_literally_with_its_env(self):
        with tempfile.TemporaryDirectory() as d:
            exe = self.tools(d, 'my esptool') / 'my esptool'
            build = Path(d) / 'build $x'
            build.mkdir()
            with mock.patch.dict(os.environ):
                os.environ.pop('NO_COLOR', None)
                seen = []

                def run(cmd, **kw):
                    seen.append(kw['env'])
                    return quiet_run(build)(cmd, **kw)
                r = hf.flash_esptool({'name': 'esptool', 'uid': 'U', 'args': ''}, chip='esp32s3', port='/dev/x y',
                                     flash_args=f"--flash_mode dio 0x0 'boot loader.bin' 0x10000 {shlex.quote(ODD)}",
                                     cwd=str(build), timeout=10, run=run, esptool=str(exe))
            self.assertNotIn('PWNED', os.listdir(build))
        self.assertEqual(seen, [{'NO_COLOR': '1'}])
        self.assertEqual(r.args[0], str(exe))
        self.assertEqual(r.stdout.splitlines(), ['--chip', 'esp32s3', '-p', '/dev/x y', '--before=default_reset',
                                                 '--after=hard_reset', 'write_flash', '--flash_mode', 'dio', '0x0',
                                                 'boot loader.bin', '0x10000', ODD])

    def test_a_missing_esptool_executable_is_refused(self):
        for exe in ('', None, 3):
            with self.subTest(exe=exe), self.assertRaises(ValueError):
                hf.esptool_argv(exe, '--chip', 'esp32s3')


def quiet_run(cwd):
    """The provider runner in `cwd`, as a caller injects it."""
    def run(cmd, **kw):
        kw.pop('cwd', None)
        return hf.hil_process.run_cmd(cmd, str(cwd), **kw)
    return run


class DefaultRunner(unittest.TestCase):
    def test_run_defaults_to_the_provider_runner(self):
        """No run= reaches hil_process.run_cmd with the caller's bound: a fake tool on PATH."""
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / 'lm4flash'
            tool.write_text('#!/bin/sh\necho "args: $*"\n')
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}'}):
                r = hf.flash_lm4flash({'name': 'lm4flash', 'uid': 'S1', 'args': '-v'}, 'fw.bin', timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, 'args: -s S1 -v fw.bin\n'))
        self.assertIs(hf._runner(None), hf.hil_process.run_cmd)


class Openocd(unittest.TestCase):
    """The roster's optional flasher `vid_pid` field (openocd-verbatim, e.g. "0x1a86 0x8010",
    more pairs appended) pins openocd's probe discovery so it never opens foreign usbfs
    nodes. It must be emitted BEFORE the args: the rescue cfgs run `init` internally."""

    def setUp(self):
        hf._VID_PID_WARNED.clear()

    def test_vid_pid_multiple_pairs(self):
        cmd = hf.openocd_cmd_base({'uid': 'S1', 'args': '-f i.cfg', 'vid_pid': '0x2e8a 0x000c 0x2e8a 0x000d'})
        self.assertEqual(cmd[-4:], ['-c', 'adapter usb vid_pid 0x2e8a 0x000c 0x2e8a 0x000d', '-f', 'i.cfg'])

    def test_no_pin_warns_once_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(2):
                cmd = hf.openocd_cmd_base({'name': 'openocd', 'uid': 'S-warn', 'args': '-f i.cfg'})
        self.assertNotIn('vid_pid', cmd)
        self.assertEqual(err.getvalue().count('has no vid_pid pin'), 1)

    def test_a_malformed_explicit_pin_is_refused_before_openocd_runs(self):
        rp2040 = {'name': 'openocd', 'uid': 'S-bad', 'args': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg'}
        for bad in ('1a86_55d4', '0x1a86', '0x1a86 0x8010 0x2e8a', '0x2e8a\n0x000c', '', '   ', None, 4660,
                    ['0x1a86', '0x8010']):
            f = {**rp2040, 'vid_pid': bad}
            for fn, args in ((hf.flash_openocd, (f, 'fw.elf')), (hf.reset_openocd, (f,)),
                             (hf.rescue_openocd, (f, hf.DAP_WEDGED[0])), (hf.run_openocd_commands, (f, ['halt'])),
                             (hf.sysresetreq_openocd, (f,))):
                with self.subTest(vid_pid=bad, fn=fn.__name__):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err), self.assertRaises(ValueError) as cm:
                        capture(fn, *args, timeout=7)
                    self.assertIn(f"'S-bad' has a malformed vid_pid {bad!r}", str(cm.exception))
                    self.assertEqual(err.getvalue(), '')
        launches = []
        with self.assertRaises(ValueError):
            hf.flash_openocd({**rp2040, 'vid_pid': '0x1a86 0x8010 0x2e8a'}, 'fw.elf', timeout=7,
                             run=lambda *a, **k: launches.append(a))
        d = self.enterContext(tempfile.TemporaryDirectory())
        with self.assertRaises(ValueError):
            hf.run_openocd_commands({**rp2040, 'vid_pid': '0x1a86'}, ['halt'], timeout=7, workdir=d,
                                    run=lambda *a, **k: launches.append(a))
        self.assertEqual((launches, os.listdir(d)), ([], []))

    def test_openocd_over_jlink_does_not_warn(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hf.openocd_cmd_base({'name': 'openocd', 'uid': 'S-jl', 'args': '-f interface/jlink.cfg'})
        self.assertEqual(err.getvalue(), '')


class Rescue(unittest.TestCase):
    RP2040 = {'name': 'openocd', 'uid': 'S1', 'vid_pid': '0x2e8a 0x000c',
              'args': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg'}

    def test_the_result_is_the_rescue_commands_rc(self):
        for rc, want in ((0, True), (1, False), (124, False)):
            ret, _, _, _ = capture(hf.rescue_openocd, self.RP2040, hf.DAP_WEDGED[0], timeout=13, rcs=[rc])
            self.assertIs(ret, want)

    def test_no_wedge_or_no_rp_target_runs_nothing(self):
        for f, out in ((self.RP2040, 'Error: flash write failed'), (self.RP2040, ''),
                       ({**self.RP2040, 'args': '-f target/stm32f4x.cfg'}, hf.DAP_WEDGED[0]),
                       ({**self.RP2040, 'name': 'jlink'}, hf.DAP_WEDGED[0])):
            ret, calls, _, _ = capture(hf.rescue_openocd, f, out, timeout=13)
            self.assertEqual((ret, calls), (False, []))


class ConvoySafe(unittest.TestCase):
    """convoy_safe is true ONLY for flashers that reach their probe without opening a
    poisoned usbfs node (tinyusb test_hil_bounded.py ConvoySafeFlasher and
    test_change_impact.py FlasherRecoverEntry)."""

    f = staticmethod(hf.convoy_safe)

    def test_pinned_openocd_is_safe(self):
        self.assertTrue(self.f({'name': 'openocd', 'vid_pid': '0x2e8a 0x000c'}))
        self.assertTrue(self.f({'name': 'openocd', 'vid_pid': '0x2e8a 0x000c', 'args': '-f interface/cmsis-dap.cfg'}))

    def test_unpinned_openocd_is_not(self):
        self.assertFalse(self.f({'name': 'openocd'}))
        self.assertFalse(self.f({'name': 'openocd', 'vid_pid': ''}))
        self.assertFalse(self.f({'name': 'openocd', 'args': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg'}))

    def test_esptool_and_stlink_are_safe_without_a_pin(self):
        self.assertTrue(self.f({'name': 'esptool'}))
        self.assertTrue(self.f({'name': 'stlink', 'uid': 'X'}))

    def test_openocd_over_a_filtering_driver_is_safe_without_a_pin(self):
        for cfg in hf.CONVOY_SAFE_CFGS:
            self.assertTrue(self.f({'name': 'openocd', 'args': f'-f {cfg} -f target/stm32h7x.cfg'}), cfg)

    def test_enumerating_flashers_are_not(self):
        for name in ('jlink', 'lm4flash', 'dfu-util', 'openocd_wch'):
            self.assertFalse(self.f({'name': name, 'vid_pid': '0x1366 0x1024'}), name)

    def test_missing_or_odd_name_is_not_safe(self):
        for flasher in ({}, {'name': None}, {'name': ''}):
            self.assertFalse(self.f(flasher))

    def test_valid_vid_pid(self):
        for value, want in (('0x2e8a 0x000c', True), (' 0x2e8a  0x000c ', True), ('\t0x2e8a\t0x000c\t', True),
                            # a newline would split the Tcl `adapter usb vid_pid` command in two
                            ('0x2e8a\n0x000c', False), ('0x2e8a 0x000c\n0x2e8a 0x000d', False),
                            ('0x2e8a\t0x000c\n', False), ('\n0x2e8a 0x000c', False), ('0x2e8a\r0x000c', False),
                            ('0x2e8a 0x000c 0x2e8a 0x000d', True), ('0x2e8a', False),
                            ('0x2e8a 0x000c 0x2e8a', False), ('0x1234 0x5678 0x9abc', False),
                            ('0x2e8a 0x000c 0x2e8a 0x000d 0x2e8a', False), ('0x2e8a0x000c', False),
                            ('2e8a 000c', False), ('1a86_55d4', False), ('0x2e8a 0x000c,', False),
                            (None, False), ('', False), ('   ', False), (0x2e8a, False),
                            (['0x2e8a', '0x000c'], False), (b'0x2e8a 0x000c', False)):
            self.assertEqual(hf.valid_vid_pid(value), want, value)

    def test_an_incomplete_pair_is_neither_convoy_safe_nor_a_pyocd_filter(self):
        for f in ({'name': 'openocd', 'vid_pid': '0x2e8a 0x000c 0x2e8a'},
                  {'name': 'pyocd', 'uid': 'U', 'vid_pid': '0x2e8a 0x000c 0x2e8a'}):
            self.assertFalse(self.f(f), f)
        self.assertIsNone(hf.pyocd_vid_pid({'name': 'pyocd', 'uid': 'U', 'vid_pid': '0x2e8a 0x000c 0x2e8a'}))


class Pyocd(unittest.TestCase):
    FLASHER = {'name': 'pyocd', 'uid': 'GSA0CQEQ', 'vid_pid': '0x1fc9 0x0090', 'args': '-t lpc55s28'}

    def test_flash_and_reset_run_the_launcher(self):
        head = ['/py', hf.PYOCD_LAUNCHER, '--project-dir', '/proj', '0x1fc9', '0x0090']
        tail = ['-W', '-u', 'cmsisdap:GSA0CQEQ', '-t', 'lpc55s28']
        with mock.patch.object(hf, '_pyocd_python', return_value='/py'):
            for fn, fw, verb in ((hf.flash_pyocd, ('/tmp/a b/fw.elf',), 'flash'), (hf.reset_pyocd, (), 'reset')):
                _, calls, _, _ = capture(fn, self.FLASHER, *fw, project_dir='/proj', timeout=7)
                self.assertEqual(calls, [([*head, verb, *tail, *fw], {'timeout': 7})])
        self.assertTrue(Path(hf.PYOCD_LAUNCHER).is_file())

    def test_a_relative_firmware_is_made_absolute(self):
        with mock.patch.object(hf, '_pyocd_python', return_value='/py'):
            _, calls, _, _ = capture(hf.flash_pyocd, self.FLASHER, 'fw.elf', project_dir='/proj', timeout=7)
        self.assertTrue(os.path.isabs(calls[0][0][-1]))

    def test_an_unfiltered_entry_is_refused(self):
        for bad in ({'vid_pid': None}, {'vid_pid': '0x1fc9 0x0090 0x1fc9 0x0143'},
                    {'vid_pid': '1fc9:0090'}, {'uid': ''}):
            flasher = {**self.FLASHER, **bad}
            self.assertIsNone(hf.pyocd_vid_pid(flasher), bad)
            self.assertFalse(hf.convoy_safe(flasher), bad)
            with self.assertRaises(ValueError):
                hf._pyocd_argv(flasher, 'reset', project_dir='/proj')
        self.assertTrue(hf.convoy_safe(self.FLASHER))
        self.assertEqual(hf.pyocd_vid_pid(self.FLASHER), ['0x1fc9', '0x0090'])

    def test_the_interpreter_comes_from_pyocds_shebang(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / 'pyocd'
            for shebang, want in ((f'#!{sys.executable}', sys.executable),
                                  ('#!/usr/bin/env python3', None), ('#!/bin/sh', None), ('', None)):
                exe.write_text(f'{shebang}\nimport sys\n')
                exe.chmod(0o755)
                with mock.patch.dict(os.environ, {'PATH': td}):
                    if want:
                        self.assertEqual(hf._pyocd_python(), want)
                    else:
                        self.assertRaises(RuntimeError, hf._pyocd_python)
            exe.unlink()
            with mock.patch.dict(os.environ, {'PATH': td}):
                self.assertRaises(RuntimeError, hf._pyocd_python)

# A project's chain in miniature, in its own interpreter: the profile's flash step calls the
# adapter (tinyusb's board-shaped flash_*), which calls the provider with its runner, run_tracked
# through hil_flash's own hil_process instance. argv: hil_flash.py, the fake tools' dir; stdout's
# last line is JSON.
CHAIN = """
import contextlib, importlib.util, io, json, os, signal, sys, time
spec = importlib.util.spec_from_file_location('agentrc_hil_flash', sys.argv[1])
hf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hf)
os.environ['PATH'] = sys.argv[2] + os.pathsep + os.environ['PATH']
os.chdir(sys.argv[2])
STATE = os.path.join(sys.argv[2], 'state')
os.mkdir(STATE, 0o700)


def run(cmd, cwd=None, timeout=None, **kw):
    return hf.hil_process.run_tracked(cmd, cwd, timeout=timeout, settle=0.3, state_dir=STATE, reap_grace=2,
                                      quiet=True, **kw)


def adapter_flash(board, fw, timeout):
    f = board['flasher']
    if f['name'] == 'jlink':
        return hf.flash_jlink(f, fw, timeout=timeout, crash_retries=2, run=run)
    return hf.flash_lm4flash(f, fw, timeout=timeout, run=run)


def profile_flash(board, fw, timeout=10):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            op = adapter_flash(board, fw, timeout)
    except BaseException as e:
        op = e.operation
    listed = [p for r in op.receipts for p in (r['survivors'] or {}).get('processes', ())]
    for p in listed:
        os.kill(p['pid'], signal.SIGKILL)
    deadline = time.monotonic() + 10
    while any(hf.hil_process.recheck(r) == 'pending' for r in op.receipts) and time.monotonic() < deadline:
        time.sleep(0.02)
    return {'kinds': [a.kind for a in op.attempts], 'rcs': [a.result.returncode for a in op.attempts],
            'cleanups': [a.cleanup for a in op.attempts], 'cleanup': op.cleanup,
            'survivors': [sorted(p['comm'] for p in (r['survivors'] or {}).get('processes', ())) for r in op.receipts],
            'rechecked': [hf.hil_process.recheck(r) for r in op.receipts], 'leftovers': [p['pid'] for p in listed]}


def alarm(*_):
    raise KeyboardInterrupt


signal.signal(signal.SIGALRM, alarm)
lm4 = {'flasher': {'name': 'lm4flash', 'uid': 'S1', 'args': ''}}
out = {fw: profile_flash(lm4, fw) for fw in ('clean.bin', 'daemon.bin', 'escape.bin')}
out['slow.bin'] = profile_flash(lm4, 'slow.bin', timeout=0.5)
signal.setitimer(signal.ITIMER_REAL, 0.5)
out['interrupted'] = profile_flash(lm4, 'slow.bin')
for uid in ('CLEAN', 'ESCAPE'):
    out[uid] = profile_flash({'flasher': {'name': 'jlink', 'uid': uid, 'args': '-device x'}}, 'fw.hex')
print('RESULT ' + json.dumps(out))
"""

LM4FLASH = """#!/bin/sh
case "$*" in
  *daemon.bin) sleep 30 </dev/null >/dev/null 2>&1 & ;;
  *escape.bin) setsid sleep 30 </dev/null >/dev/null 2>&1 & ;;
  *slow.bin) sleep 30 ;;
esac
echo flashed
"""

# crashes on its first run in each directory state, as libjlinkarm does before target access
JLINKEXE = """#!/bin/sh
case "$*" in *ESCAPE*) setsid sleep 30 </dev/null >/dev/null 2>&1 & ;; esac
state="$PWD/jlink-$2"
[ -e "$state" ] || { : > "$state"; kill -SEGV $$; }
echo O.K.
"""


class TrackedChain(unittest.TestCase):
    """The profile -> adapter -> provider -> runner chain with run_tracked and fake tools on
    PATH, real processes throughout: the operation's cleanup is the supervisor's evidence, a
    pending one resolves by recheck once its survivor ends, and a J-Link crash is rerun only
    over a run proven ended."""

    def test_the_operation_carries_the_runners_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (('lm4flash', LM4FLASH), ('JLinkExe', JLINKEXE)):
                tool = Path(d) / name
                tool.write_text(text)
                tool.chmod(0o755)
            r = subprocess.run([sys.executable, '-c', CHAIN, str(SCRIPTS / 'hil_flash.py'), d],
                               capture_output=True, text=True, timeout=120)
        line = next((x for x in r.stdout.splitlines() if x.startswith('RESULT ')), None)
        self.assertIsNotNone(line, f'rc {r.returncode}\n{r.stdout}\n{r.stderr}')
        out = json.loads(line[len('RESULT '):])
        for case in out.values():
            for pid in case['leftovers']:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 9)

        def summary(case):
            return case['kinds'], case['cleanups'], case['cleanup'], case['survivors']
        self.assertEqual(summary(out['clean.bin']), (['exited'], ['done'], 'done', []))
        for fw in ('daemon.bin', 'escape.bin'):
            self.assertEqual(summary(out[fw]), (['exited'], ['pending'], 'pending', [['sleep']]), fw)
            self.assertEqual(out[fw]['rechecked'], ['done'], 'the survivor was killed')
        self.assertEqual(summary(out['slow.bin']), (['timed_out'], ['done'], 'done', []))
        self.assertEqual(out['slow.bin']['rcs'], [124])
        self.assertEqual(summary(out['interrupted']), (['interrupted'], ['done'], 'done', []))
        self.assertEqual(out['interrupted']['rcs'], [None])
        crash, ok = out['CLEAN']['rcs']
        self.assertIn(crash, hf.JLINK_CRASH_RCS)
        self.assertEqual((ok, out['CLEAN']['cleanups'], out['CLEAN']['cleanup']), (0, ['done', 'done'], 'done'))
        self.assertEqual(len(out['ESCAPE']['rcs']), 1, 'a crash rerun started over a run not proven ended')
        self.assertIn(out['ESCAPE']['rcs'][0], hf.JLINK_CRASH_RCS)
        self.assertEqual((out['ESCAPE']['cleanup'], out['ESCAPE']['survivors']), ('pending', [['sleep']]))


if __name__ == '__main__':
    unittest.main()
