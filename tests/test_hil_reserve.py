"""hil_reserve.py against a fake sysfs tree, a temp lock dir and stub `sudo`/recovery
scripts: roster resolution across both projects' shapes, the coverage refusals, the
all-or-nothing hold, the one command it builds, and the cleanup-holder outcome with its
token-bound release. `run` is exercised in a forked child that shares this process's
patched module state, so the production command line stays intact and a test can patch
one function in the child before it starts. Stubs prove the plumbing; the hardware rungs
are the skill's own validation."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_reserve requires POSIX flock, fork and /proc')

import fcntl
import importlib.util
import io
import json
import signal
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'hil_reserve.py'
DATA = Path(__file__).resolve().parent / 'data' / 'hil_reserve'
TINYUSB, TINYUF2 = DATA / 'tinyusb.json', DATA / 'tinyuf2.json'

spec = importlib.util.spec_from_file_location('hil_reserve_under_test', SCRIPT)
hr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hr)

# a fake `sudo -n CMD...` that just runs CMD, so the built command line stays the real one
SUDO = '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n'
# a fake recovery script: records its argv, then does what ACTION says
RECOVER = '''#!/bin/sh
printf '%s\\n' "$@" > "$ARGV_OUT"
case "$ACTION" in
  ok) exit 0 ;;
  fail) exit 7 ;;
  cure) echo E6633861A3978538 > "$SYS_DEVICES/3-1.1/serial"; exit 0 ;;
  drop-sibling) mv "$SYS_DEVICES/3-1.3" "$SYS_DEVICES/.gone" && exit 0 ;;
  drop-target) mv "$SYS_DEVICES/3-1.1" "$SYS_DEVICES/.gone" && exit 0 ;;
  unreadable-class) rm "$SYS_DEVICES/3-1/bDeviceClass" && exit 0 ;;
  new-node) mkdir "$SYS_DEVICES/3-1.9" && echo 00 > "$SYS_DEVICES/3-1.9/bDeviceClass" && echo new > "$SYS_DEVICES/3-1.9/serial"; exit 0 ;;
  survivor) setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $! > "$ARGV_OUT.pid"; exit 0 ;;
  survivor-same-group) sleep 30 </dev/null >/dev/null 2>&1 & echo $! > "$ARGV_OUT.pid"; exit 0 ;;
  hang) sleep 30; exit 0 ;;
  probe-locks) python3 - "$LOCK_DIR" > "$ARGV_OUT.locks" <<'PY'
import fcntl, os, sys
for fn in sorted(os.listdir(sys.argv[1])):
    if fn.endswith('.lock'):
        with open(os.path.join(sys.argv[1], fn), 'r+') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); print(fn[:-5], 'free')
            except BlockingIOError:
                print(fn[:-5], 'held')
PY
    exit 0 ;;
esac
'''


class Child:
    """A forked hil_reserve.main; .stdout is its stdout+stderr."""

    def __init__(self, pid, stdout):
        self.pid, self.stdout, self.returncode = pid, stdout, None

    def poll(self):
        if self.returncode is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout=10):
        for _ in range(int(timeout * 20)):
            if self.poll() is not None:
                return self.returncode
            time.sleep(0.05)
        raise TimeoutError('child still running')

    def output(self, timeout=10):
        """Everything it printed; only for a child that exits on its own."""
        out = self.stdout.read()
        self.wait(timeout)
        return out

    def blocker(self):
        for line in self.stdout:
            if 'BLOCKER' in line:
                return line
        return ''

    def token(self):
        return token_of(self.blocker())

    def send_signal(self, sig):
        os.kill(self.pid, sig)

    def kill(self):
        if self.poll() is None:
            os.kill(self.pid, signal.SIGKILL)
            self.wait()
        self.stdout.close()


def token_of(blocker_line):
    return blocker_line.split('token ')[1].split()[0]


def main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = hr.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.sys = root / 'sys'
        self.sys.mkdir()
        self.locks = root / 'locks'
        self.udev = root / 'udev'
        self.udev.mkdir()
        self.saved = (hr.uo.SYS_USB_DEVICES, hr.uo.UDEV_DATA, hr.hil_lock.BOARD_LOCK_DIR, hr.RECOVER_SCRIPT,
                      os.environ['PATH'])
        self.addCleanup(self.restore)
        hr.uo.SYS_USB_DEVICES, hr.uo.UDEV_DATA = str(self.sys), str(self.udev)
        hr.hil_lock.BOARD_LOCK_DIR = str(self.locks)
        self.bin = root / 'bin'
        self.bin.mkdir()
        hr.RECOVER_SCRIPT = str(self.bin / 'usb_recover.sh')
        for p, text in ((self.bin / 'sudo', SUDO), (Path(hr.RECOVER_SCRIPT), RECOVER)):
            p.write_text(text)
            p.chmod(0o755)
        self.argv_out = root / 'argv'
        os.environ.update(PATH=f'{self.bin}:{self.saved[4]}', ARGV_OUT=str(self.argv_out), SYS_DEVICES=str(self.sys),
                          LOCK_DIR=str(self.locks))
        # root port 3-1: hub 3-1 with pico2's probe, the DUT, and tinyuf2's EVK
        self.dev('3-1', cls='09')
        self.dev('3-1.1', serial='E6633861A3978538')      # tinyusb pico2 flasher
        self.dev('3-1.2', serial='560AE75E1C7152C9')      # tinyusb pico2 DUT
        self.dev('3-1.3', serial='000725299165')          # tinyuf2 EVK J-Link, exclusive to tinyuf2's roster
        self.dev('3-2', serial='unrelated-keyboard')      # another root port: outside the scope

    def restore(self):
        (hr.uo.SYS_USB_DEVICES, hr.uo.UDEV_DATA, hr.hil_lock.BOARD_LOCK_DIR, hr.RECOVER_SCRIPT,
         os.environ['PATH']) = self.saved

    def dev(self, name, serial=None, cls='00', udev=None):
        """A device directory; `udev` is the ID_SERIAL_SHORT udev recorded for it. busnum 3,
        devnum from the port path, so the udev record is keyed the way usbcore does."""
        d = self.sys / name
        d.mkdir(exist_ok=True)
        if cls is not None:
            (d / 'bDeviceClass').write_text(cls + '\n')
        if serial is not None:
            (d / 'serial').write_text(serial + '\n')
        (d / 'busnum').write_text('3\n')
        (d / 'devnum').write_text(f'{self.devnum(name)}\n')
        if udev is not None:
            self.udev_record(name).write_text(f'I:1\nE:ID_SERIAL_SHORT={udev}\n')
        return d

    @staticmethod
    def devnum(name):
        return 10 + int(name.replace('-', '').replace('.', '')) % 100

    def udev_record(self, name):
        return self.udev / f'c189:{2 * 128 + self.devnum(name) - 1}'

    def rosters(self):
        return ['--roster', str(TINYUSB), '--roster', str(TINYUF2), 'tinyuf2.lock']

    def fork_main(self, *args, action='ok', patch=None):
        """hil_reserve.main in a forked child; `patch(hr)` runs in the child first."""
        r, w = os.pipe()
        os.environ['ACTION'] = action
        pid = os.fork()
        if pid == 0:
            os.close(r)
            os.dup2(w, 1)
            os.dup2(w, 2)
            os.close(w)
            sys.stdout = os.fdopen(1, 'w', buffering=1)
            sys.stderr = os.fdopen(2, 'w', buffering=1)
            if patch:
                patch(hr)
            os._exit(hr.main(list(args)))
        os.close(w)
        child = Child(pid, os.fdopen(r))
        self.addCleanup(child.kill)
        return child

    def run_run(self, action='ok', patch=None, busport='3-1.1', budget='2'):
        """`budget` bounds the restoration poll too: a blocker case waits it out."""
        return self.fork_main('run', 'root-cycle', busport, *self.rosters(), '--reason', 'test',
                              '--budget', budget, action=action, patch=patch)

    def records(self):
        if not self.locks.exists():
            return {}
        return {fn[:-5]: hr.hil_lock.read_record(fn[:-5]) for fn in os.listdir(self.locks) if fn.endswith('.lock')}

    def held(self):
        return {k for k, v in self.records().items() if v}

    def flock_free(self, lock):
        with open(self.locks / f'{lock}.lock', 'r+') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

    def wait_for(self, cond, what, timeout=10):
        for _ in range(int(timeout * 20)):
            if cond():
                return
            time.sleep(0.05)
        self.fail(f'timed out waiting for {what}')

    def assert_cleanup_holder(self, child, token):
        recs = self.records()
        self.assertEqual(set(recs), {'mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'})
        for rec in recs.values():
            self.assertEqual((rec['role'], rec['token'], rec['pid']), ('cleanup-holder', token, child.pid))
            self.assertEqual(sorted(rec['locks']), sorted(recs))
        self.assertIsNone(child.poll())


class Rosters(Fixture):
    def test_both_shapes_resolve_dedupe_and_sort(self):
        r = hr.load_rosters([(str(TINYUSB), 'name'), (str(TINYUF2), 'tinyuf2.lock')])
        # tinyusb lists raspberry_pi_pico2 and stm32h743eval twice (one parked); tinyuf2's imxrt1064_evk
        # aliases to mimxrt1064_evk, which tinyusb also names; its stm32f303disco has no alias field
        self.assertEqual(r['locks'], ['mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'])
        self.assertEqual(r['identities']['e6633861a3978538'], {'raspberry_pi_pico2'})
        self.assertEqual(r['identities']['000725299165'], {'mimxrt1064_evk'})   # tinyuf2 flasher.uid
        self.assertEqual(r['identities']['820000000000000045b46338'], {'raspberry_pi_pico2'})   # dev_attached serial
        self.assertEqual(r['identities']['uart-1064'], {'mimxrt1064_evk'})

    def test_a_probe_shared_by_two_entries_maps_to_both_locks(self):
        self.assertEqual(hr.load_rosters([(str(TINYUSB), 'name')])['identities']['shared-probe-serial'],
                         {'raspberry_pi_pico2', 'stm32h743eval'})

    def test_the_selector_falls_back_to_name_only_when_the_field_is_absent(self):
        bad = Path(self.td.name) / 'bad.json'
        bad.write_text(json.dumps({'boards': [{'name': 'ok', 'tinyuf2': {'lock': 42}}]}))
        with self.assertRaisesRegex(hr.Refused, "ok: lock name 42 is not a plain file name"):
            hr.load_rosters([(str(bad), 'tinyuf2.lock')])
        bad.write_text(json.dumps({'boards': [{'name': 'ok', 'tinyuf2': {'lock': ''}}]}))
        with self.assertRaisesRegex(hr.Refused, "not a plain file name"):
            hr.load_rosters([(str(bad), 'tinyuf2.lock')])

    def test_identities_compare_as_udev_records_a_serial(self):
        self.assertEqual(hr.identity('  AB  CD/x,y '), 'ab_cd_x_y')
        roster = Path(self.td.name) / 'r.json'
        roster.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'AB CD'}, {'name': 'b', 'uid': 'ab_cd'}]}))
        with self.assertRaisesRegex(hr.Refused, "identities 'ab cd' and 'ab_cd' are one and the same to udev"):
            hr.load_rosters([(str(roster), 'name')])
        roster.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'AB CD'}]}))
        got = hr.load_rosters([(str(roster), 'name')])
        self.assertEqual(got['identities'], {'ab_cd': {'a'}})
        self.dev('3-1.4', serial='AB CD')                    # the sysfs form matches...
        (self.sys / '3-1.5').mkdir()
        self.dev('3-1.5', udev='AB_CD')                      # ...and so does udev's
        nodes = hr.walk('3-1.4', got['identities'], time.monotonic() + 5)
        self.assertEqual((nodes['3-1.4']['covered'], nodes['3-1.5']['covered']), (['a'], ['a']))

    def test_a_dut_uid_under_two_lock_names_is_a_conflict(self):
        bad = Path(self.td.name) / 'bad.json'
        bad.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'X1'}, {'name': 'b', 'uid': 'x1'}]}))
        with self.assertRaisesRegex(hr.Refused, 'uid x1 names two boards: a and b'):
            hr.load_rosters([(str(bad), 'name')])

    def test_malformed_rosters_and_lock_names_refuse(self):
        bad = Path(self.td.name) / 'bad.json'
        for text, why in (('{', 'roster .*bad.json'), (json.dumps({'boards': []}), 'no boards'),
                          (json.dumps({'boards': [{'uid': 'u'}]}), 'entry without a name'),
                          (json.dumps({'boards': [{'name': '../etc', 'uid': 'u'}]}), 'not a plain file name')):
            bad.write_text(text)
            with self.assertRaisesRegex(hr.Refused, why):
                hr.load_rosters([(str(bad), 'name')])
        bad.write_text(json.dumps({'boards': [{'name': 'ok', 'tinyuf2': {'lock': 'a/b'}}]}))
        with self.assertRaisesRegex(hr.Refused, 'not a plain file name'):
            hr.load_rosters([(str(bad), 'tinyuf2.lock')])
        with self.assertRaisesRegex(hr.Refused, 'roster .*missing.json'):
            hr.load_rosters([(str(Path(self.td.name) / 'missing.json'), 'name')])


@unittest.skipUnless(Path('/home/hathach/code/tinyusb/test/hil/tinyusb.json').exists()
                     and Path('/home/hathach/code/adafruit/tinyuf2/test/hil/local.json').exists(),
                     'the real project rosters are not on this host')
class RealRosters(unittest.TestCase):
    """Every entry of the real rosters resolves to the lock the project's own resolver gives:
    tinyusb's `hil_lock.boards_from_config` (name, parked boards included) and tinyuf2's
    `hil_test.load_board(...)['lock']`; identities are the raw uids lower-cased."""

    @staticmethod
    def load(path):
        spec = importlib.util.spec_from_file_location(Path(path).stem + '_project', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_tinyusb_rosters_resolve_as_its_adapter_does(self):
        try:
            adapter = self.load('/home/hathach/code/tinyusb/test/hil/helper/hil_lock.py')
        except ImportError as e:
            self.skipTest(f'tinyusb adapter not loadable here: {e}')
        for fn in ('tinyusb.json', 'local.json'):
            path = Path('/home/hathach/code/tinyusb/test/hil') / fn
            got = hr.load_rosters([(str(path), 'name')])
            self.assertEqual(got['locks'], sorted(set(adapter.boards_from_config(str(path)))), fn)
            roster = json.loads(path.read_text())
            for e in roster['boards']:
                self.assertEqual(got['identities'][e['uid'].strip().lower()], {e['name']}, e['name'])

    def test_tinyuf2_rosters_resolve_as_its_runner_does(self):
        try:
            runner = self.load('/home/hathach/code/adafruit/tinyuf2/.claude/skills/hil-contract/scripts/hil_test.py')
        except (ImportError, OSError) as e:
            self.skipTest(f'tinyuf2 runner not loadable here: {e}')
        for fn in ('local.json', 'ci.json'):
            path = Path('/home/hathach/code/adafruit/tinyuf2/test/hil') / fn
            if not path.exists():
                continue
            got = hr.load_rosters([(str(path), 'tinyuf2.lock')])
            roster = json.loads(path.read_text())
            compared = 0
            for e in roster['boards']:
                try:
                    board = runner.load_board(str(path), e['name'])
                except runner.Refused:
                    continue        # the runner refuses the entry itself; nothing to agree on
                compared += 1
                self.assertIn(board['lock'], got['locks'], e['name'])
                self.assertIn(board['lock'], got['identities'][board['uid'].strip().lower()], e['name'])
                self.assertIn(board['lock'], got['identities'][board['probe'].strip().lower()], e['name'])
            self.assertGreater(compared, 0, fn)


class Check(Fixture):
    def test_a_covered_subtree_passes_and_lists_locks_and_nodes(self):
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual(rc, 0, out)
        self.assertIn('locks (4): mimxrt1064_evk raspberry_pi_pico2 stm32f303disco stm32h743eval', out)
        self.assertIn('3-1: serial - -> hub', out)
        self.assertIn('3-1.3: serial 000725299165 -> mimxrt1064_evk', out)
        self.assertNotIn('3-2', out)

    def test_the_target_busport_must_exist(self):
        rc, out = main('check', '3-1.9', *self.rosters())
        self.assertEqual((rc, 'no device at 3-1.9' in out), (1, True), out)

    def test_an_unknown_sibling_refuses_naming_it(self):
        self.dev('3-1.4', serial='stranger')
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual(rc, 1)
        self.assertIn('3-1.4: serial stranger matches no roster identity', out)
        self.assertIn('3-1: hub with an uncovered child', out)

    def test_a_missing_serial_an_unreadable_class_and_an_empty_hub_refuse(self):
        self.dev('3-1.4')                              # no serial at all
        self.dev('3-1.5', serial='x', cls=None)        # class unreadable
        self.dev('3-1.6', cls='09')                    # empty hub
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual(rc, 1)
        self.assertIn('3-1.4: no identity: serial unreadable or absent, none recorded by udev', out)
        self.assertIn('3-1.5: class unreadable', out)
        self.assertIn('3-1.6: empty hub, nothing identifies it', out)

    def test_a_known_serial_does_not_excuse_an_unreadable_class(self):
        (self.sys / '3-1.2' / 'bDeviceClass').unlink()
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual((rc, '3-1.2: class unreadable' in out), (1, True), out)

    def test_an_uninspectable_entry_refuses_rather_than_vanishing(self):
        hidden = Path(self.td.name) / 'noperm'
        hidden.mkdir()
        (self.sys / '3-1.4').symlink_to(hidden / 'dev')     # sysfs entries are symlinks; this one cannot be followed
        os.chmod(hidden, 0)
        self.addCleanup(os.chmod, hidden, 0o755)
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual((rc, '3-1.4: cannot inspect (Permission denied)' in out), (1, True), out)

    def test_interface_directories_are_ignored(self):
        (self.sys / '3-1.1:1.0').mkdir()
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual(rc, 0, out)
        self.assertNotIn(':1.0', out)

    def test_withholding_the_roster_that_owns_a_device_refuses(self):
        rc, out = main('check', '3-1.1', '--roster', str(TINYUSB))
        self.assertEqual(rc, 1)
        self.assertIn('3-1.3: serial 000725299165 matches no roster identity', out)

    def test_a_tree_that_changes_between_the_walks_refuses(self):
        real_walk, calls = hr.walk, []

        def walk(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                self.dev('3-1.4', serial='late')
            return real_walk(*a, **k)

        hr.walk = walk
        self.addCleanup(setattr, hr, 'walk', real_walk)
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual((rc, 'changed during the check' in out), (1, True), out)

    def test_a_slow_attribute_read_hits_the_budget(self):
        real = hr.uo.read_sysfs
        hr.uo.read_sysfs = lambda path, timeout=0: (time.sleep(0.05), real(path, timeout))[1]
        self.addCleanup(setattr, hr.uo, 'read_sysfs', real)
        rc, out = main('check', '3-1.1', *self.rosters(), '--budget', '0.2')
        self.assertEqual((rc, 'over budget: subtree uninspectable' in out), (1, True), out)

    def test_usage_errors(self):
        for argv in (('check', '../x', *self.rosters()), ('check', '3-1', '--roster', 'a', 'b', 'c'),
                     ('run', 'pci-rebind', '3-1', *self.rosters(), '--reason', 'r'),
                     ('run', 'root-cycle', '3-1', *self.rosters(), '--reason', 'r', '--script', 'x')):
            with self.assertRaises(SystemExit):
                main(*argv)


class Run(Fixture):
    def test_a_clean_cycle_holds_every_lock_during_the_command_and_releases(self):
        unrelated = hr.hil_lock.take_board_lock('other-board', 'bystander')
        self.addCleanup(unrelated.close)
        p = self.run_run(action='probe-locks')
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.argv_out.read_text().split(), ['root-cycle', '3-1.1', 'E6633861A3978538'])
        self.assertIn('holding mimxrt1064_evk raspberry_pi_pico2 stm32f303disco stm32h743eval', out)
        self.assertIn(f'sudo -n {hr.RECOVER_SCRIPT} root-cycle 3-1.1 E6633861A3978538', out)
        self.assertIn('released 4 lock(s); 4 node(s) back', out)
        probed = dict(line.split() for line in Path(str(self.argv_out) + '.locks').read_text().splitlines())
        self.assertEqual(probed, {'mimxrt1064_evk': 'held', 'raspberry_pi_pico2': 'held', 'stm32f303disco': 'held',
                                  'stm32h743eval': 'held', 'other-board': 'held'})
        self.assertEqual(self.held(), {'other-board'})
        self.assertEqual(self.records()['other-board']['reason'], 'bystander')
        for lock in ('mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'):
            self.assertTrue(self.flock_free(lock), lock)

    def test_a_held_board_refuses_before_any_command_and_leaves_the_rival_record(self):
        rival = hr.hil_lock.take_board_lock('stm32f303disco', 'someone else')
        self.addCleanup(rival.close)
        p = self.run_run()
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('refused: stm32f303disco: board locked: ', out)
        self.assertIn('someone else', out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), {'stm32f303disco'})
        self.assertEqual(self.records()['stm32f303disco']['reason'], 'someone else')
        self.assertTrue(self.flock_free('raspberry_pi_pico2'))

    def test_a_record_write_that_fails_halfway_refuses_and_clears_what_it_wrote(self):
        def patch(m):
            def write_record(fh, reason, **extra):
                fh.write(json.dumps({'pid': os.getpid(), 'reason': 'half'}))
                fh.flush()
                raise OSError('disk full')
            m.hil_lock.write_record = write_record
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual((p.returncode, 'board lock unavailable: holder record: disk full' in out), (1, True), out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())
        self.assertTrue(all(self.flock_free(l) for l in self.records()))

    def test_a_target_without_a_serial_refuses_before_the_hold(self):
        p = self.run_run(busport='3-1')        # the hub: covered by its children, but no serial to bind to
        out = p.output()
        self.assertEqual((p.returncode, 'has no serial, in sysfs or recorded by udev' in out), (1, True), out)
        self.assertEqual(self.held(), set())

    def test_a_subtree_that_changes_between_the_check_and_the_hold_refuses(self):
        def patch(m):
            real, calls = m.check, []

            def check(*a, **k):
                calls.append(1)
                if len(calls) == 2:
                    self.dev('3-1.3', serial='uart-1064')    # still covered, but not the device checked
                return real(*a, **k)
            m.check = check
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual((p.returncode, 'changed between the check and the hold' in out), (1, True), out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())

    def test_a_failed_command_keeps_the_locks_as_a_cleanup_holder_until_a_token_release(self):
        p = self.run_run(action='fail')
        line = p.blocker()
        self.assertIn('command exited 7', line)
        token = token_of(line)
        self.assert_cleanup_holder(p, token)
        p.send_signal(signal.SIGTERM)                   # ignored in the cleanup state
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        rc, out = main('release', '--token', 'nope')
        self.assertEqual((rc, 'no holder record carries token nope' in out), (1, True), out)
        rc, out = main('release', '--token', token)
        self.assertEqual(rc, 0, out)
        self.assertIn(f'released 4 lock(s) of holder {p.pid}', out)
        self.assertEqual(p.wait(), 1)
        self.assertEqual(self.held(), set())

    def test_a_hangup_does_not_end_a_cleanup_hold(self):
        p = self.run_run(action='fail')
        token = token_of(p.blocker())
        p.send_signal(signal.SIGHUP)                    # the terminal or SSH session that launched it went away
        time.sleep(0.3)
        self.assert_cleanup_holder(p, token)
        self.assertFalse(self.flock_free('raspberry_pi_pico2'))
        rc, out = main('release', '--token', token)
        self.assertEqual(rc, 0, out)
        self.assertEqual(p.wait(), 1)
        self.assertEqual(self.held(), set())

    def test_a_hangup_during_the_command_is_ignored_and_before_it_releases(self):
        p = self.run_run(action='hang')
        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
        time.sleep(0.2)
        p.send_signal(signal.SIGHUP)
        time.sleep(0.3)
        self.assertIsNone(p.poll())                     # the mutation runs on
        p.send_signal(signal.SIGTERM)
        line = p.blocker()
        self.assertIn('SIGHUP ignored during command', line)
        self.assertIn('SIGTERM forwarded', line)
        p.kill()                                        # frees the locks for the second run
        self.argv_out.unlink()

        def before(m):
            real, calls = m.check, []

            def check(*a, **k):
                calls.append(1)
                if len(calls) == 2:
                    os.kill(os.getpid(), signal.SIGHUP)     # under the hold, before the command
                return real(*a, **k)
            m.check = check
        q = self.run_run(patch=before)
        self.assertEqual(q.wait(), 130)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())

    def test_a_command_that_cannot_start_releases_the_locks_and_refuses(self):
        def patch(m):
            def popen(*a, **k):
                raise FileNotFoundError(2, 'No such file or directory', 'sudo')
            m.subprocess.Popen = popen
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('refused: cannot start sudo: ', out)
        self.assertEqual(self.held(), set())
        self.assertTrue(all(self.flock_free(l) for l in self.records()))

    def test_a_cleanup_record_that_cannot_be_written_still_holds(self):
        def patch(m):
            real = m.Reservation.record

            def record(self, role=None, detail=''):
                if role:
                    raise OSError('no space')
                real(self, role, detail)
            m.Reservation.record = record
        p = self.run_run(action='fail', patch=patch)
        line = p.blocker()
        self.assertIn('cleanup record could not be written: no space', line)
        self.assertIsNone(p.poll())
        self.assertFalse(self.flock_free('raspberry_pi_pico2'))

    def test_a_missing_sibling_after_a_successful_command_is_a_blocker_once_the_budget_is_spent(self):
        p = self.run_run(action='drop-sibling')
        line = p.blocker()
        self.assertIn('3-1.3 (serial 000725299165) not back: absent', line)
        self.assertIn('not restored within 2 s', line)
        self.assertNotIn('command exited', line)
        self.assert_cleanup_holder(p, token_of(line))

    def test_a_sibling_that_returns_within_the_budget_releases(self):
        p = self.run_run(action='drop-sibling')
        self.wait_for(lambda: not (self.sys / '3-1.3').exists(), 'the sibling to drop')
        time.sleep(1.4)
        (self.sys / '.gone').rename(self.sys / '3-1.3')      # back, same serial, late in the 2 s budget
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn('released 4 lock(s); 4 node(s) back', out)
        self.assertEqual(self.held(), set())

    def test_a_target_absent_for_a_moment_is_looked_at_again_until_it_returns(self):
        p = self.run_run(action='drop-target')
        self.wait_for(lambda: not (self.sys / '3-1.1').exists(), 'the target to drop')
        time.sleep(0.7)
        (self.sys / '.gone').rename(self.sys / '3-1.1')
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn('released 4 lock(s); 4 node(s) back', out)

    def test_a_restoration_look_that_outlasts_the_budget_is_a_blocker(self):
        def patch(m):
            real = m.walk

            def walk(busport, identities, deadline, cached=True):
                if not cached:
                    time.sleep(1.2)                  # the reads of the look outlast the 1 s budget
                return real(busport, identities, deadline, cached)
            m.walk = walk
        p = self.run_run(patch=patch, budget='1')
        line = p.blocker()
        self.assertIn('coverage check over budget', line)
        self.assertIn('not restored within 1 s', line)
        self.assert_cleanup_holder(p, token_of(line))

    def test_a_wedged_target_is_admitted_by_its_udev_record_and_restored_only_by_sysfs(self):
        (self.sys / '3-1.1' / 'serial').unlink()             # the wedged read gives nothing
        self.dev('3-1.1', udev='E6633861A3978538')
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual((rc, '3-1.1: serial E6633861A3978538 (udev) -> raspberry_pi_pico2' in out), (0, True), out)
        p = self.run_run(action='ok')                        # the cycle "worked" but the serial stays unreadable
        line = p.blocker()
        self.assertEqual(self.argv_out.read_text().split(), ['root-cycle', '3-1.1', 'E6633861A3978538'])
        self.assertIn('3-1.1 (serial E6633861A3978538 (udev)) not back: serial unreadable', line)
        p.kill()
        q = self.run_run(action='cure')                      # re-enumerated: sysfs answers again
        out = q.output()
        self.assertEqual(q.returncode, 0, out)
        self.assertIn('released 4 lock(s); 4 node(s) back', out)

    def test_the_command_takes_the_sysfs_serial_the_second_look_finds_over_the_udev_form(self):
        (self.sys / '3-1.1' / 'serial').unlink()
        self.dev('3-1.1', udev='e6633861a3978538')          # same identity, not the string the script compares

        def patch(m):
            real, calls = m.check, []

            def check(*a, **k):
                calls.append(1)
                if len(calls) == 2:
                    self.dev('3-1.1', serial='E6633861A3978538')    # readable again under the hold
                return real(*a, **k)
            m.check = check
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.argv_out.read_text().split(), ['root-cycle', '3-1.1', 'E6633861A3978538'])

    def test_a_readable_serial_outranks_a_stale_udev_record_and_an_unmatched_udev_one_refuses(self):
        self.dev('3-1.1', udev='stale')
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual((rc, '3-1.1: serial E6633861A3978538 -> raspberry_pi_pico2' in out), (0, True), out)
        (self.sys / '3-1.2' / 'serial').unlink()
        self.dev('3-1.2', udev='nobody')
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual((rc, '3-1.2: serial nobody matches no roster identity' in out), (1, True), out)
        self.udev_record('3-1.2').unlink()                   # no record either
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertIn('3-1.2: no identity: serial unreadable or absent, none recorded by udev', out)

    def test_an_unreadable_class_or_a_new_node_after_the_command_is_a_blocker(self):
        p = self.run_run(action='unreadable-class')
        self.assertIn('3-1: class unreadable', p.blocker())
        p.kill()                                        # frees the locks for the second run
        self.dev('3-1', cls='09')
        q = self.run_run(action='new-node', busport='3-1.2')
        self.assertIn('3-1.9: new node (serial new)', q.blocker())

    def test_a_survivor_keeps_the_locks_even_after_it_is_gone(self):
        for action in ('survivor', 'survivor-same-group'):
            with self.subTest(action=action):
                p = self.run_run(action=action)
                line = p.blocker()
                orphan = int(Path(str(self.argv_out) + '.pid').read_text())
                self.addCleanup(lambda o=orphan: os.kill(o, signal.SIGKILL) if hr.proc_start(o) else None)
                self.assertIn(f'surviving pids [{orphan}]', line)
                os.kill(orphan, signal.SIGKILL)
                time.sleep(0.3)
                self.assertIsNone(p.poll())      # still a cleanup holder: the survivor leaving frees nothing
                self.assertEqual(self.records()['raspberry_pi_pico2']['role'], 'cleanup-holder')
                p.kill()

    def test_sigterm_during_the_command_is_forwarded_and_ends_as_a_blocker(self):
        p = self.run_run(action='hang')
        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
        time.sleep(0.2)
        p.send_signal(signal.SIGTERM)
        line = p.blocker()
        self.assertIn('SIGTERM forwarded', line)
        self.assertIn('command exited -15', line)

    def test_a_signal_during_the_spawn_waits_for_the_group_and_is_forwarded(self):
        def patch(m):
            real = m.subprocess.Popen

            def popen(*a, **k):
                proc = real(*a, **k)
                os.kill(os.getpid(), signal.SIGTERM)       # lands while the spawn is still in progress
                return proc
            m.subprocess.Popen = popen
        p = self.run_run(action='hang', patch=patch)
        line = p.blocker()
        self.assertIn('SIGTERM forwarded', line)
        self.assertIn('command exited -15', line)
        self.assertEqual(self.records()['raspberry_pi_pico2']['role'], 'cleanup-holder')

    def test_a_lost_progress_reader_does_not_abandon_the_command(self):
        marker = Path(self.td.name) / 'progress-attempted'

        def patch(m):
            m.PROGRESS_EVERY = 0.1
            import builtins
            orig = builtins.print

            def pr(*a, **k):
                if a and 'still running' in str(a[0]):
                    marker.write_text('')
                    raise BrokenPipeError()
                orig(*a, **k)
            builtins.print = pr
        p = self.run_run(action='hang', patch=patch)
        self.wait_for(marker.exists, 'the failing progress print')
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        self.assertFalse(self.flock_free('raspberry_pi_pico2'))
        p.send_signal(signal.SIGTERM)
        self.assertIn('command exited -15', p.blocker())

    def test_an_undeliverable_forwarded_signal_is_reported(self):
        def patch(m):
            real = m.os.killpg

            def killpg(pgid, sig):
                real(pgid, sig)                 # the stub still ends, but the delivery reads as refused
                raise PermissionError()
            m.os.killpg = killpg
        p = self.run_run(action='hang', patch=patch)
        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
        p.send_signal(signal.SIGTERM)
        line = p.blocker()
        self.assertIn('SIGTERM could not be delivered (EPERM)', line)

    def test_a_signal_before_the_command_releases_and_one_after_is_ignored(self):
        def before(m):
            real = m.check

            def check(*a, **k):
                out = real(*a, **k)
                if self.locks.exists() and m.hil_lock.read_record('raspberry_pi_pico2'):
                    os.kill(os.getpid(), signal.SIGINT)     # between the hold and the command
                return out
            m.check = check
        p = self.run_run(patch=before)
        self.assertEqual(p.wait(), 130)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())

        def after(m):
            real = m.survivors

            def survivors(pgid):
                os.kill(os.getpid(), signal.SIGTERM)       # during reconciliation
                os.kill(os.getpid(), signal.SIGUSR1)       # not yet a cleanup holder: ignored
                return real(pgid)
            m.survivors = survivors
        q = self.run_run(action='fail', patch=after)
        line = q.blocker()
        self.assertIn('SIGTERM ignored during post', line)
        self.assertIsNone(q.poll())

    def test_release_refuses_the_wrong_holder_a_live_mutation_and_a_reused_pid(self):
        first = self.run_run(action='fail')
        tok1 = first.token()
        other = Path(self.td.name) / 'other.json'
        other.write_text(json.dumps({'boards': [{'name': 'other-board', 'uid': 'unrelated-keyboard'}]}))
        second = self.fork_main('run', 'root-cycle', '3-2', '--roster', str(other), '--reason', 't', action='fail')
        tok2 = second.token()
        self.assertNotEqual(tok1, tok2)
        rc, out = main('release', '--token', tok1)        # two valid holders: only the first goes
        self.assertEqual(rc, 0, out)
        self.assertEqual(first.wait(), 1)
        self.assertIsNone(second.poll())
        self.assertEqual(self.held(), {'other-board'})
        rec = self.records()['other-board']
        rec.pop('role')                                    # a live mutation: a token but no cleanup role
        (self.locks / 'other-board.lock').write_text(json.dumps(rec))
        rc, out = main('release', '--token', tok2)
        self.assertEqual((rc, 'not a cleanup holder' in out), (1, True), out)
        rec['role'], rec['start'] = 'cleanup-holder', '1'  # a reused pid: the start time does not match
        (self.locks / 'other-board.lock').write_text(json.dumps(rec))
        rc, out = main('release', '--token', tok2)
        self.assertEqual((rc, 'is not the recorded holder' in out), (1, True), out)
        self.assertIsNone(second.poll())

    def test_release_checks_every_record_not_just_the_first(self):
        p = self.run_run(action='fail')
        tok = p.token()
        rec = self.records()['stm32h743eval']
        rec['locks'] = ['stm32h743eval']                    # one record disagrees about the lock set
        (self.locks / 'stm32h743eval.lock').write_text(json.dumps(rec))
        rc, out = main('release', '--token', tok)
        self.assertEqual((rc, 'the record on stm32h743eval disagrees' in out), (1, True), out)
        self.assertIsNone(p.poll())

    def test_release_refuses_a_holder_that_exits_after_validation_before_delivery(self):
        p = self.run_run(action='fail')
        tok = p.token()
        real = signal.pidfd_send_signal

        def send(fd, sig):
            p.kill()                                        # gone after the identity check passed
            return real(fd, sig)                            # the pidfd names the dead process, not a reused pid
        hr.signal.pidfd_send_signal = send
        self.addCleanup(setattr, hr.signal, 'pidfd_send_signal', real)
        rc, out = main('release', '--token', tok)
        self.assertEqual((rc, f'holder pid {p.pid} could not be signalled' in out), (1, True), out)

    def test_release_refuses_without_pidfd_support(self):
        p = self.run_run(action='fail')
        tok = p.token()
        for mod, name in ((hr.os, 'pidfd_open'), (hr.signal, 'pidfd_send_signal')):
            real = getattr(mod, name)
            delattr(mod, name)
            try:
                rc, out = main('release', '--token', tok)
            finally:
                setattr(mod, name, real)
            self.assertEqual((rc, 'pidfd) is unavailable' in out), (1, True), out)
        self.assertIsNone(p.poll())

    def test_release_refuses_a_holder_that_already_exited(self):
        p = self.run_run(action='fail')
        tok = p.token()
        p.kill()
        rc, out = main('release', '--token', tok)      # the kernel dropped the flocks; the records are stale
        self.assertEqual((rc, f'holder pid {p.pid} cannot be opened' in out), (1, True), out)


if __name__ == '__main__':
    unittest.main()
