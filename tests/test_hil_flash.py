"""skills/hil/scripts/hil_flash.py builds and runs each flasher backend's commands from a
flasher dict, moved from tinyusb's test/hil/hil_flash.py (c5099be4f). The golden test replays
every flasher of that commit's tinyusb.json and hfp.json (copies in tests/data/hil_flash),
plus a pyOCD entry, and requires exactly what tinyusb's module produced
(tests/data/hil_flash/gen_golden.py): commands, J-Link scripts, cwd, timeouts, retries,
output and the result handed back. Only the pyOCD launcher path and its --project-dir
argument are normalised, plus deliberate deltas since, each applied explicitly: J-Link
command files are unique per invocation, reused by its crash reruns and removed after it
(jlink_command_file_delta), the J-Link flash script has no reset before `loadfile`
(jlink_flash_script_delta), esptool runs with NO_COLOR=1 (esptool_no_color_delta), and a
flash or reset returns an Operation carrying the runner's results as its attempts, whose last
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
    if isinstance(cmd, str) and '-CommandFile ' in cmd:
        head, tail = cmd.rsplit('-CommandFile ', 1)
        path, = shlex.split(tail)
        if (Path(path).parent == Path.cwd() and re.fullmatch(r'hil_jlink_\w+\.jlink', Path(path).name)
                and tail == shlex.quote(path)):
            return head + COMMAND_FILE
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
    """esptool's command gains a NO_COLOR=1 prefix: output only, its arguments unchanged."""
    if case['flasher']['name'].lower() != 'esptool' or case['op'] == 'rescue':
        return observed

    def rewrite(call):
        assert call['cmd'].startswith('esptool '), call['cmd']
        return {**call, 'cmd': f"NO_COLOR=1 {call['cmd']}"}
    return map_calls(observed, rewrite)


def deltas(case, observed):
    return esptool_no_color_delta(case, jlink_flash_script_delta(case, jlink_command_file_delta(case, observed)))


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
    PREFIX = 'JLinkExe -USB S1 -device x -if swd -JTAGConf -1,-1 -speed auto -NoGui 1 -ExitOnError 1 -CommandFile '

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
            self.assertTrue(cmd.startswith(self.PREFIX), cmd)
            path, = shlex.split(cmd[len(self.PREFIX):])
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

    def test_the_path_is_shell_quoted(self):
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
            text = Path(shlex.split(cmd.rsplit('-CommandFile ', 1)[1])[0]).read_text() if '-CommandFile' in cmd else ''
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
    """run_jlink_commands/run_stlink_commands and the SYSRESETREQ operations built on them:
    the caller's connection profile, a unique command file in `workdir`, one raw attempt."""
    UF2 = {'name': 'jlink', 'uid': '000725000000', 'args': '-device MIMXRT1011DAE5A'}
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
        for fn, kw in ((hf.run_jlink_commands, self.PROFILE), (hf.run_stlink_commands, {'connect_mode': 'HOTPLUG'})):
            with self.assertRaises(KeyboardInterrupt) as cm:
                fn(self.UF2, ['h'], timeout=5, run=run, workdir=d, **kw)
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


class StlinkLm4flashEsptool(unittest.TestCase):
    def test_an_explicit_stlink_verify_false_is_no_verify(self):
        # the golden rosters set stlink verify only to true or leave it out
        _, calls, _, _ = capture(hf.flash_stlink, {'name': 'stlink', 'uid': 'S1', 'verify': False}, 'fw.elf',
                                 timeout=7)
        self.assertEqual(calls, [('STM32_Programmer_CLI --connect port=swd sn=S1 --write fw.elf --go', {'timeout': 7})])

    def test_esptool_and_lm4flash_never_reset_alone(self):
        self.assertFalse(hasattr(hf, 'reset_esptool'))
        self.assertFalse(hasattr(hf, 'reset_lm4flash'))

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

    def test_timeout_is_explicit_everywhere(self):
        for fn, args in ((hf.flash_stlink, ({}, 'fw')), (hf.reset_stlink, ({},)), (hf.flash_openocd, ({}, 'fw')),
                         (hf.reset_openocd, ({},)), (hf.flash_lm4flash, ({}, 'fw')),
                         (hf.flash_jlink, ({}, 'fw')), (hf.reset_jlink, ({},)), (hf.rescue_openocd, ({},))):
            with self.subTest(fn=fn.__name__), self.assertRaises(TypeError):
                fn(*args)


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
        self.assertIn('-c "adapter usb vid_pid 0x2e8a 0x000c 0x2e8a 0x000d"', cmd)

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
                             (hf.rescue_openocd, (f, hf.DAP_WEDGED[0]))):
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
        self.assertEqual(launches, [])

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
    return hf.hil_process.run_tracked(cmd, cwd, timeout=timeout, settle=1, state_dir=STATE, reap_grace=2,
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
