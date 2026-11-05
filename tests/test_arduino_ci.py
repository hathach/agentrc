"""skills/arduino/scripts/arduino_ci.py: an Arduino library's HIL contract in CI. The hosted side sets a board up
through ci-arduino's own functions and packs its clean record's tree; the rig side admits each packed tree, runs the
battery per board with lock waits, one retry and a deadline, writes summary.json and prunes old finished runs. Run
against the fake library (tests/fixtures/arduino_lib) in a temporary git checkout, the battery and ci-arduino stubbed:
stubs prove the plumbing only; the workflow is verified by a real CI run."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the arduino engines are Linux only (fcntl)')

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, load_arduino_engine, load_arduino_fixture, git_env, tmpdir  # noqa: E402

C = load_arduino_engine('skills/arduino/scripts/arduino_ci.py')
R = C.R
fake = load_arduino_fixture()
BOARD = 'rig_ocd'
ROSTER = 'test/hil/ci.json'
HEAD_RUN = '77'


def git(root, *a):
    return subprocess.run(['git', '-C', str(root), *a], check=True, capture_output=True, text=True,
                          env=git_env()).stdout.strip()


def setUpModule():
    guard_survivors(__name__)


class Checkout(unittest.TestCase):
    """The fake library as a clean git checkout with its roster and a clean build record of BOARD at HEAD."""

    def setUp(self):
        self.tmp = tmpdir(self)
        self.root = fake.library(self.tmp / 'lib')
        fake.roster(self.root / ROSTER)
        (self.root / '.gitignore').write_text('/build/\n')
        git(self.root, 'init', '-q')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'init')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        self.agentrc = fake.agentrc(self.tmp / 'agentrc')
        self.BP = fake.build_profile(self.root, self.agentrc)
        self.HP = fake.hil_profile(self.BP)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target, name, value in ((C, 'P', self.HP), (R, 'P', self.HP), (R.H, 'P', self.HP), (R.B, 'P', self.BP)):
            stack.enter_context(mock.patch.object(target, name, value))
        self.record_path = self.record()

    def record(self, board=BOARD):
        elf = self.root / f'build/hil/{board}/echo/echo.ino.elf'
        elf.parent.mkdir(parents=True, exist_ok=True)
        elf.write_bytes(b'elf')
        rec = {'kind': 'arduino-hil-build', 'version': 2, 'library': fake.NAME, 'board': board, 'experimental': False,
               'source': {'head': self.head}, **R.B.contract_identity(),
               'sketches': {'echo': {'sha256': {elf.relative_to(self.root).as_posix(): hashlib.sha256(b'elf')
                                                .hexdigest()}}}}
        path = self.root / f'build/hil/{board}.build.json'
        path.write_text(json.dumps(rec))
        return path

    def main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = C.main(list(argv))
        lines = out.getvalue().strip().splitlines()
        return rc, json.loads(lines[-1]) if lines and lines[-1].startswith('{') else None

    def pack(self, board=BOARD, out=None, run_id=HEAD_RUN):
        out = out or self.tmp / 'hosted'
        rc, res = self.main('pack', '--board', board, '--record', str(self.record_path), '--out', str(out),
                            '--run-id', run_id, '--attempt', '1', '--roster', str(self.root / ROSTER))
        self.assertEqual(rc, 0, res)
        return out


class Pack(Checkout):
    def test_packs_the_tree_hil_remote_would_stage_with_provenance(self):
        out = self.pack()
        tree = out / BOARD
        manifest = json.loads((tree / R.MANIFEST).read_text())
        self.assertEqual(manifest['head'], self.head)
        pinned = [*self.BP.CONTRACT_FILES, ROSTER, f'build/hil/{BOARD}.build.json',
                  f'build/hil/{BOARD}/echo/echo.ino.elf', *(f'agentrc/{f}' for f in self.agentrc['files'])]
        self.assertEqual(sorted(manifest['files']), sorted(pinned))
        for f in pinned:
            self.assertEqual(manifest['files'][f], hashlib.sha256((tree / f).read_bytes()).hexdigest(), f)
        self.assertEqual(json.loads((tree / C.PROVENANCE).read_text()),
                         {'board': BOARD, 'head': self.head, 'runId': HEAD_RUN, 'attempt': 1})
        self.assertEqual(sorted(p.name for p in out.iterdir()), [BOARD])   # no temporary dir left

    def test_refuses_a_dirty_tree_and_a_second_pack(self):
        out = self.pack()
        rc, res = self.main('pack', '--board', BOARD, '--record', str(self.record_path), '--out', str(out),
                            '--run-id', HEAD_RUN, '--attempt', '1', '--roster', str(self.root / ROSTER))
        self.assertEqual(rc, 2)
        self.assertIn('exists', res['error'])
        (self.root / 'src').mkdir(exist_ok=True)
        (self.root / 'library.properties').write_text('changed\n')
        rc, res = self.main('pack', '--board', BOARD, '--record', str(self.record_path), '--out',
                            str(self.tmp / 'other'), '--run-id', HEAD_RUN, '--attempt', '1', '--roster',
                            str(self.root / ROSTER))
        self.assertEqual(rc, 2)
        self.assertIn('not clean', res['error'])
        self.assertFalse((self.tmp / 'other' / BOARD).exists())


class Admit(Checkout):
    def setUp(self):
        super().setUp()
        self.tree = self.pack() / BOARD

    def admit(self, head=None, run_id=HEAD_RUN, board=BOARD):
        return C.admit_tree(self.tree, board, head or self.head, run_id, ROSTER)

    def assertRefused(self, fragment, **kw):
        with self.assertRaises(C.Refused) as cm:
            self.admit(**kw)
        self.assertIn(fragment, str(cm.exception))

    def test_admits_this_runs_tree(self):
        self.assertIsNone(self.admit())   # admitted: no refusal

    def test_refuses_another_run_head_or_board(self):
        self.assertRefused('runId', run_id='78')
        self.assertRefused('head', head='b' * 40)
        self.assertRefused('board', board='rig_jlink')

    def test_refuses_links_stray_missing_and_changed_files(self):
        (self.tree / 'stray.py').write_text('import os\n')
        self.assertRefused('unpinned')
        (self.tree / 'stray.py').unlink()
        (self.tree / ROSTER).write_text('{}')
        self.assertRefused('changed')
        fake.roster(self.tree / ROSTER)
        elf = self.tree / f'build/hil/{BOARD}/echo/echo.ino.elf'
        elf.unlink()
        self.assertRefused('missing')
        elf.symlink_to('/etc/passwd')
        self.assertRefused('links')

    def test_refuses_manifest_paths_outside_the_tree(self):
        m = json.loads((self.tree / R.MANIFEST).read_text())
        m['files']['../escape'] = '0' * 64
        (self.tree / R.MANIFEST).write_text(json.dumps(m))
        self.assertRefused('outside the tree')

    def test_refuses_a_tree_whose_manifest_pins_no_roster(self):
        m = json.loads((self.tree / R.MANIFEST).read_text())
        del m['files'][ROSTER]
        (self.tree / R.MANIFEST).write_text(json.dumps(m))
        (self.tree / ROSTER).unlink()
        self.assertRefused('pins no')

    def test_refuses_a_roster_other_than_the_checkouts(self):
        fake.roster(self.root / ROSTER, rig_ocd={'uid': 'OTHER'})
        self.assertRefused('not this checkout')


def row(verdict, locked=False, tests=None, holder=None):
    return {'verdict': verdict, 'locked': locked, 'recoveryBlocker': False, 'detail': f'{verdict} detail',
            'tests': tests or [{'test': 'echo', 'pass': verdict == 'pass', 'detail': 'x'}],
            'cleanup': {'pass': holder is None, 'state': 'restored', 'holder': holder},
            'firmware': {'fqbn': 'vend:arch:rig_ocd', 'platform': {'id': 'vend:arch', 'version': '1.0'}}}


class Loop(unittest.TestCase):
    """run_board()'s attempts against a scripted battery."""

    def setUp(self):
        self.out = tmpdir(self)
        self.sleeps = []
        p = mock.patch.object(C.time, 'sleep', lambda s: self.sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)

    def run_board(self, rows, deadline=3600, lock_wait=600, stop=None, clock_left=None, each=None):
        rows = list(rows)
        calls = []

        def battery(tree, board, run_dir, roster):
            calls.append(run_dir.name)
            if each:
                each()
            r = rows.pop(0)
            return (0 if r and r['verdict'] == 'pass' else 1), r
        clock = C.Clock(deadline)
        if clock_left is not None:
            clock.left = lambda: clock_left(len(calls))
        with mock.patch.object(C, 'battery', battery):
            res = C.run_board(BOARD, Path('tree'), self.out, ROSTER, clock, lock_wait, stop or {'signal': False})
        return res, calls

    def test_pass_first_time(self):
        res, calls = self.run_board([row('pass')])
        self.assertEqual((res['verdict'], res['flaky'], calls), ('pass', False, ['run-1']))
        self.assertEqual(res['firmware']['platform']['version'], '1.0')

    def test_a_fail_is_retried_once_and_a_passing_retry_is_flaky(self):
        res, calls = self.run_board([row('fail'), row('pass')])
        self.assertEqual((res['verdict'], res['flaky'], calls), ('pass', True, ['run-1', 'run-2']))
        res, calls = self.run_board([row('fail'), row('fail')])
        self.assertEqual((res['verdict'], res['flaky'], len(calls)), ('fail', False, 2))

    def test_blocker_and_not_run_are_never_retried(self):
        for verdict in ('blocker', 'not-run'):
            res, calls = self.run_board([row(verdict)])
            self.assertEqual((res['verdict'], calls), (verdict, ['run-1']))

    def test_no_report_is_a_blocker(self):
        res, _ = self.run_board([None])
        self.assertEqual(res['verdict'], 'blocker')
        self.assertIn('no readable report', res['detail'])

    def test_a_locked_board_is_admitted_again_until_free(self):
        res, calls = self.run_board([row('not-run', locked=True), row('not-run', locked=True), row('pass')])
        self.assertEqual((res['verdict'], len(calls)), ('pass', 3))
        self.assertTrue(self.sleeps)

    def test_a_board_locked_past_the_lock_wait_is_not_run(self):
        t = [0.0]
        with mock.patch.object(C.time, 'monotonic', lambda: t[0]):
            res, calls = self.run_board([row('not-run', locked=True)] * 3, lock_wait=600,
                                        each=lambda: t.__setitem__(0, t[0] + 400))
        self.assertEqual((res['verdict'], len(calls)), ('not-run', 2))   # the wait starts with the first locked attempt
        self.assertIn('lock wait', res['detail'])

    def test_deadline_stops_a_start_and_denies_a_retry(self):
        res, calls = self.run_board([row('pass')], clock_left=lambda n: 0)
        self.assertEqual((res['verdict'], calls), ('not-run', []))
        self.assertIn('deadline', res['detail'])
        res, calls = self.run_board([row('fail')], clock_left=lambda n: 10 if n < 1 else 0)
        self.assertEqual((res['verdict'], res['retry'], calls), ('fail', 'retry not run: deadline', ['run-1']))

    def test_deadline_during_a_lock_wait_is_not_run(self):
        res, calls = self.run_board([row('not-run', locked=True)] * 3, clock_left=lambda n: 10 if n < 2 else 0)
        self.assertEqual((res['verdict'], len(calls)), ('not-run', 2))
        self.assertIn('deadline', res['detail'])

    def test_a_fail_stands_when_its_retry_is_locked_out(self):
        res, calls = self.run_board([row('fail'), row('not-run', locked=True)], clock_left=lambda n: 10 if n < 2 else 0)
        self.assertEqual((res['verdict'], res['tests'][0]['pass']), ('fail', False))
        self.assertIn('retry not run: deadline', res['retry'])
        t = [0.0]
        with mock.patch.object(C.time, 'monotonic', lambda: t[0]):
            res, calls = self.run_board([row('fail')] + [row('not-run', locked=True)] * 3, lock_wait=600,
                                        each=lambda: t.__setitem__(0, t[0] + 400))
        self.assertEqual(res['verdict'], 'fail')
        self.assertIn('lock wait', res['retry'])
        res, _ = self.run_board([row('fail'), row('not-run')])   # the retry refused, say the probe gone
        self.assertEqual((res['verdict'], res['retry']), ('fail', 'retry not run: not-run detail'))

    def test_the_lock_wait_is_checked_before_readmission_and_counts_an_admitted_wait(self):
        t = [100.0]
        with mock.patch.object(C.time, 'monotonic', lambda: t[0]):
            res, calls = self.run_board([row('not-run', locked=True), row('pass')], lock_wait=30,
                                        each=lambda: t.__setitem__(0, t[0] + 33))
        self.assertEqual((res['verdict'], len(calls)), ('not-run', 1))
        with mock.patch.object(C.time, 'monotonic', lambda: t[0]):
            res, calls = self.run_board([row('not-run', locked=True), row('pass')], lock_wait=600,
                                        each=lambda: t.__setitem__(0, t[0] + 10))
        self.assertEqual((res['verdict'], res['waitedS']), ('pass', 10))   # until the admitted attempt's start

    def test_the_lock_wait_ends_at_admission_and_never_denies_a_later_retry(self):
        t = [100.0]
        steps = iter([31, 700, 10])   # locked 31 s, a 700 s fail, the retry

        def each():
            t[0] += next(steps)
        with mock.patch.object(C.time, 'monotonic', lambda: t[0]):
            res, calls = self.run_board([row('not-run', locked=True), row('fail'), row('pass')], lock_wait=600,
                                        each=each)
        self.assertEqual((res['verdict'], res['flaky'], len(calls), res['waitedS']), ('pass', True, 3, 31))

    def test_a_signal_starts_no_attempt(self):
        res, calls = self.run_board([row('pass')], stop={'signal': True})
        self.assertEqual((res['verdict'], calls), ('not-run', []))


class Prune(unittest.TestCase):
    def setUp(self):
        self.state = tmpdir(self)

    def run_dir(self, name, summary, age_days=40):
        d = self.state / name
        d.mkdir()
        if summary is not None:
            (d / C.SUMMARY).write_text(json.dumps(summary))
        old = time.time() - age_days * 86400
        os.utime(d, (old, old))
        return d

    def test_removes_only_old_runs_that_finished_clean(self):
        clean = {'kind': C.SUMMARY_KIND, 'termination': 'completed',
                 'boards': [{'verdict': 'pass', 'cleanup': {'holder': None}}, {'verdict': 'not-run', 'cleanup': None}]}
        self.run_dir('1-1', clean)
        self.run_dir('2-1', clean, age_days=1)
        self.run_dir('3-1', {**clean, 'boards': [{'verdict': 'blocker', 'cleanup': {'holder': {'token': 't'}}}]})
        self.run_dir('4-1', None)
        self.run_dir('5-1', {**clean, 'termination': 'aborted'})
        self.run_dir('notarun', clean)
        linked = self.run_dir('9-1', None)
        (linked / C.SUMMARY).symlink_to(self.run_dir('outside', clean) / C.SUMMARY)
        target = self.run_dir('6-1', clean)
        (self.state / '7-1').symlink_to(target)
        keep = self.run_dir('8-1', clean)
        removed = C.prune(self.state, 30, keep)
        self.assertEqual(removed, ['1-1', '6-1'])
        self.assertEqual(sorted(p.name for p in self.state.iterdir()),
                         ['2-1', '3-1', '4-1', '5-1', '7-1', '8-1', '9-1', 'notarun', 'outside'])

    def test_a_dir_another_run_removed_is_skipped(self):
        clean = {'kind': C.SUMMARY_KIND, 'termination': 'completed', 'boards': []}
        gone, raced = self.run_dir('1-1', clean), self.run_dir('2-1', clean)
        real_rmtree = shutil.rmtree

        def loads(text):    # another run removes 1-1 between this run's summary read and its stat
            if gone.exists():
                real_rmtree(gone)
            return clean

        def rmtree_raced(path):     # and empties 2-1 under this run's rmtree
            real_rmtree(path)
            raise FileNotFoundError(path)
        with mock.patch.object(C.json, 'loads', side_effect=loads), \
                mock.patch.object(C.shutil, 'rmtree', side_effect=rmtree_raced):
            removed = C.prune(self.state, 30, self.state / '9-1')
        self.assertEqual((removed, raced.exists(), gone.exists()), ([], False, False))

    def test_a_dir_it_cannot_remove_is_logged_and_skipped(self):
        clean = {'kind': C.SUMMARY_KIND, 'termination': 'completed', 'boards': []}
        stuck, other = self.run_dir('1-1', clean), self.run_dir('2-1', clean)
        real_rmtree = shutil.rmtree

        def rmtree(path):
            if Path(path) == stuck:
                raise PermissionError(13, 'Permission denied', str(path))
            real_rmtree(path)
        with mock.patch.object(C.shutil, 'rmtree', side_effect=rmtree), \
                mock.patch.object(C.sys, 'stderr', io.StringIO()) as err:
            removed = C.prune(self.state, 30, self.state / '9-1')
        self.assertEqual((removed, stuck.exists(), other.exists()), (['2-1'], True, False))
        self.assertIn(f'prune: {stuck}: ', err.getvalue())
        self.assertIn('Permission denied', err.getvalue())


class Run(Checkout):
    def setUp(self):
        super().setUp()
        self.hosted = self.pack()
        self.state = self.tmp / 'state'
        self.state.mkdir()

    def run_cmd(self, rows, *extra, boards=(BOARD,)):
        rows = dict(rows)

        def battery(tree, board, run_dir, roster):
            self.assertTrue((tree / C.R.MANIFEST).is_file())
            r = rows[board].pop(0)
            return 0, r
        argv = ['run', '--hosted', str(self.hosted), '--head', self.head, '--run-id', HEAD_RUN, '--attempt', '1',
                '--out', str(self.state / f'{HEAD_RUN}-1'), *extra]
        for b in boards:
            argv += ['-b', b]
        with mock.patch.object(C, 'battery', battery), mock.patch.object(C.signal, 'signal'):
            return self.main(*argv, str(self.root / ROSTER))

    def test_summary_of_a_passing_board_and_one_without_a_tree(self):
        rc, s = self.run_cmd({BOARD: [row('pass')]}, boards=(BOARD, 'rig_jlink'))
        self.assertEqual(rc, 1)
        by = {b['board']: b for b in s['boards']}
        self.assertEqual(by[BOARD]['verdict'], 'pass')
        self.assertEqual(by['rig_jlink']['verdict'], 'not-run')
        self.assertIn('no packed tree', by['rig_jlink']['detail'])
        self.assertEqual((s['kind'], s['head'], s['runId'], s['attempt'], s['termination']),
                         (C.SUMMARY_KIND, self.head, HEAD_RUN, 1, 'completed'))
        self.assertEqual(json.loads((self.state / f'{HEAD_RUN}-1' / C.SUMMARY).read_text()), s)
        self.assertTrue((self.state / f'{HEAD_RUN}-1' / BOARD / 'tree' / C.R.MANIFEST).is_file())

    def test_exit_codes(self):
        self.assertEqual(self.run_cmd({BOARD: [row('pass')]})[0], 0)

    def test_an_aborted_run_exits_2_even_when_its_boards_passed(self):
        def battery(tree, board, run_dir, roster):
            C.signal.getsignal(C.signal.SIGTERM)(C.signal.SIGTERM, None)
            return 0, row('pass')
        handlers = {}
        with mock.patch.object(C, 'battery', battery), \
                mock.patch.object(C.signal, 'signal', lambda s, h: handlers.__setitem__(s, h)), \
                mock.patch.object(C.signal, 'getsignal', lambda s: handlers[s]):
            rc, s = self.main('run', '--hosted', str(self.hosted), '--head', self.head, '--run-id', HEAD_RUN,
                              '--attempt', '1', '--out', str(self.state / f'{HEAD_RUN}-1'), '-b', BOARD,
                              str(self.root / ROSTER))
        self.assertEqual((rc, s['termination'], s['boards'][0]['verdict']), (2, 'aborted', 'blocker'))
        self.assertIn('aborted by a signal (pass', s['boards'][0]['detail'])
        self.assertEqual(s['boards'][0]['attempts'][0]['verdict'], 'pass')   # the attempt's own evidence kept

    def test_an_unreadable_tree_is_one_boards_refusal(self):
        with mock.patch.object(C, 'admit_tree', side_effect=FileNotFoundError('gone')):
            rc, s = self.run_cmd({})
        self.assertEqual((rc, s['boards'][0]['verdict']), (1, 'not-run'))
        self.assertIn('gone', s['boards'][0]['detail'])

    def test_a_blocker_exits_2(self):
        rc, s = self.run_cmd({BOARD: [row('blocker', holder={'token': 't'})]})
        self.assertEqual(rc, 2)

    def test_refuses_an_out_dir_not_named_run_attempt_and_an_unknown_board(self):
        rc, res = self.main('run', '--hosted', str(self.hosted), '--head', self.head, '--run-id', HEAD_RUN,
                            '--attempt', '1', '--out', str(self.state / 'latest'), str(self.root / ROSTER))
        self.assertEqual((rc, 'run id' in res['error']), (2, True))
        rc, res = self.run_cmd({}, boards=('nope',))
        self.assertEqual((rc, 'not in' in res['error']), (2, True))


class Setup(Checkout):
    def ci(self, platforms):
        d = self.tmp / 'ci'
        d.mkdir()
        (d / 'all_platforms.py').write_text(f'ALL_PLATFORMS = {platforms!r}\n')
        (d / 'build_platform.py').write_text(
            'import json, os\nfrom all_platforms import ALL_PLATFORMS\nCALLS = []\n'
            'def install_library_deps():\n    CALLS.append(["deps"])\n'
            'def install_platform(fqbn, full_platform_name=None):\n    CALLS.append([fqbn, full_platform_name])\n')
        for m in ('build_platform', 'all_platforms'):
            sys.modules.pop(m, None)
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in ('build_platform', 'all_platforms')])
        self.addCleanup(lambda: str(d) in sys.path and sys.path.remove(str(d)))
        return d

    def test_installs_the_way_ci_arduino_does_for_the_boards_ci_platform(self):
        d = self.ci({'dev_a': ['vend:arch:dev_a', '0x1', None]})
        versions = self.tmp / 'versions.json'
        with mock.patch.object(C, 'versions', lambda platform: {'platform': platform}), \
                mock.patch.object(C.shutil, 'which', return_value=None):
            rc, res = self.main('setup', '--board', BOARD, '--ci', str(d), '--versions', str(versions))
        self.assertEqual((rc, res['platform'], res['ciPlatform']), (0, 'vend:arch', 'dev_a'))
        self.assertEqual(sys.modules['build_platform'].CALLS,
                         [['deps'], ['vend:arch', ['vend:arch:dev_a', '0x1', None]]])
        self.assertEqual(json.loads(versions.read_text()), {'platform': 'vend:arch'})

    def test_keeps_ci_arduino_and_its_arduino_cli_out_of_the_checkouts_status(self):
        d = self.ci({'dev_a': ['vend:arch:dev_a', '0x1', None]})
        ci, cli = self.root / 'ci', self.root / 'bin [x]'
        shutil.copytree(d, ci)
        cli.mkdir()
        exclude = self.root / '.git' / 'info' / 'exclude'
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text('# own')
        for _ in range(2):   # a second setup adds nothing
            with mock.patch.object(C, 'versions', lambda platform: {}), \
                    mock.patch.object(C.shutil, 'which', return_value=str(cli / 'arduino-cli')):
                self.assertEqual(self.main('setup', '--board', BOARD, '--ci', str(ci))[0], 0)
        self.assertEqual(exclude.read_text(), '# own\n/ci/\n/bin \\[x]/\n')
        out = subprocess.run(['git', 'status', '--porcelain'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(out.stdout, '')

    def test_never_excludes_the_checkout_or_a_dir_outside_it(self):
        with mock.patch.object(C.P, 'ROOT', self.root):
            self.assertEqual(C.exclude_from_checkout([self.root, self.tmp / 'elsewhere']), [])

    def test_refuses_a_board_whose_core_is_not_what_ci_arduino_installs(self):
        d = self.ci({'dev_a': ['other:arch:dev_a', '0x1', None]})
        versions = self.tmp / 'versions.json'
        with mock.patch.object(C, 'versions', lambda platform: {'platform': platform}):
            rc, res = self.main('setup', '--board', BOARD, '--ci', str(d), '--versions', str(versions))
        self.assertEqual(rc, 2)
        self.assertIn('ci-arduino installs other:arch', res['error'])
        self.assertEqual(json.loads(versions.read_text()), {'platform': None})   # written whatever the outcome

    def test_refuses_a_board_without_a_ci_platform(self):
        d = self.ci({})
        rc, res = self.main('setup', '--board', 'dev_a', '--ci', str(d))
        self.assertEqual(rc, 2)
        self.assertIn('not a rig board', res['error'])


if __name__ == '__main__':
    unittest.main()
