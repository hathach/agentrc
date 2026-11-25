"""hil_reserve.py against a fake sysfs tree, a temp lock dir and stub `sudo`/recovery
scripts: roster resolution across both projects' shapes, the coverage refusals, the
all-or-nothing hold, the one command it builds, the blocker outcome, logged on every
board and every lock released (agentrc#464), and a command that leaves descendants, which
keeps every lock, admission and the host fence until its token release (agentrc#586). `run`
is exercised in a forked child that shares this process's patched module state, and so does
the keeper it forks, so the production command line stays intact and a test can patch one
function before it starts. Stubs prove the plumbing; the hardware rungs are the skill's own
validation."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('hil_reserve requires POSIX flock, fork and /proc')

import fcntl
import importlib.util
import io
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import end, exited, guard_survivors, latest_failure  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'hil_reserve.py'
DATA = Path(__file__).resolve().parent / 'data' / 'hil_reserve'
USB_RECOVER = SCRIPT.parents[2] / 'usb-kernel-recover' / 'scripts' / 'usb_recover.sh'
TINYUSB, TINYUF2 = DATA / 'tinyusb.json', DATA / 'tinyuf2.json'
BOARDS = frozenset({'mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'})

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
  hang-pid) echo $$ > "$ARGV_OUT.pid.tmp" && mv "$ARGV_OUT.pid.tmp" "$ARGV_OUT.pid"; exec sleep 30 ;;
  late-fork) ( until [ -e "$ARGV_OUT.gate" ]; do sleep 0.05; done
               setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $! > "$ARGV_OUT.late.tmp" &&
               mv "$ARGV_OUT.late.tmp" "$ARGV_OUT.late" ) \
               </dev/null >/dev/null 2>&1 & echo $! > "$ARGV_OUT.pid"; exit 0 ;;
  sudo-survivor) "$REAL_SUDO" -n /bin/sh -c 'until [ -e "$1" ]; do sleep 0.05; done' sh "$ARGV_OUT.gate" \
                   </dev/null >/dev/null 2>&1 & echo $! > "$ARGV_OUT.pid"; exit 0 ;;
  wait-done) for _ in $(seq 100); do [ -e "$ARGV_OUT.done" ] && break; sleep 0.1; done; exit 0 ;;
  signals) exec grep -E '^Sig(Blk|Ign):' /proc/$$/status > "$ARGV_OUT.sig" ;;
  trap) exec bash -c 'trap "echo TRAPPED; exit 7" TERM; trap "echo HUP TRAPPED" HUP; for _ in $(seq 50); do [ -e "$ARGV_OUT.done" ] && break; sleep 0.1; done' ;;
  refuse) exit 3 ;;
  refuse-drop) mv "$SYS_DEVICES/3-1.4.1" "$SYS_DEVICES/.gone"; exit 3 ;;
  renumber|renumber-drop)    # a rebind: bus 3 (480M) comes back as 1, bus 4 (5000M) as 2
    for d in "$SYS_DEVICES"/3-*; do mv "$d" "$SYS_DEVICES/1-${d##*/3-}"; done
    mv "$PCI_DEV/usb3" "$PCI_DEV/usb1" && mv "$PCI_DEV/usb4" "$PCI_DEV/usb2" || exit 9
    [ "$ACTION" = renumber ] || mv "$SYS_DEVICES/1-1.3" "$SYS_DEVICES/.gone"
    exit 0 ;;
  strand) rm "$PCI_DEV/driver" && rm -r "$PCI_DEV/usb3" "$PCI_DEV/usb4"; exit 1 ;;
  swap-driver) ln -sfn "$PCI_DRIVERS/xhci_hcd" "$PCI_DEV/driver"; exit 0 ;;
  bind) ln -s "$PCI_DRIVERS/xhci-pci-renesas" "$PCI_DEV/driver" && mkdir "$PCI_DEV/usb3" &&
        echo 480 > "$PCI_DEV/usb3/speed"; exit 0 ;;
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

# a fake root_port_cycle.py for the real usb_recover.sh: logs each run; ACTION <verb>=refuse|hang|
# survive (ignores USR2, the script's test KILL_SIG, so it outlives the bound)
ROOT_PORT_CYCLE = f'''#!/bin/sh
echo "$1" >> "$ARGV_OUT.rpc"
case "$ACTION" in
  "$1=refuse") exit 3 ;;
  "$1=hang") exec {shutil.which('sleep')} 30 ;;
  "$1=survive") echo $$ > "$ARGV_OUT.pid"; trap '' USR2; exec {shutil.which('sleep')} 30 >/dev/null 2>&1 ;;
esac
[ "$1" = check ] && echo 189:1536:4242
exit 0
'''
SLEEP = f'#!/bin/sh\nexec {shutil.which("sleep")} 0.05\n'   # usb_recover.sh's polls and delays, shrunk
# `run` in a fresh interpreter, which starts with sys.stdin/stdout/stderr None for each fd closed at its exec;
# exit 99 when one is not
FRESH_RUN = '''import importlib.util, sys
closed, script, sys_dir, udev, locks, recover, *argv = sys.argv[1:]
if any((sys.stdin, sys.stdout, sys.stderr)[int(fd)] is not None for fd in closed):
    sys.exit(99)
spec = importlib.util.spec_from_file_location('hil_reserve_fresh', script)
hr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hr)
hr.uo.SYS_USB_DEVICES, hr.uo.UDEV_DATA, hr.hil_lock.BOARD_LOCK_DIR, hr.RECOVER_SCRIPT = sys_dir, udev, locks, recover
hr.hil_lock.ENV_DROP = ('HIL_SELFTEST_NO_SCOPE',)
sys.exit(hr.main(argv))
'''


def setUpModule():
    guard_survivors(__name__)


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

    def through_blocker(self):
        """Everything printed up to and including the blocker line."""
        out = []
        for line in self.stdout:
            out.append(line)
            if 'BLOCKER' in line:
                break
        return ''.join(out)

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


def keeper_of(blocker_line):
    return int(blocker_line.split(' pid ', 1)[1].split(':')[0])


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
        # the keeper stays a fork of the child, which the patches reach, even under an hc terminal or a runner
        self.addCleanup(setattr, hr.hil_lock, 'ENV_DROP', hr.hil_lock.ENV_DROP)
        hr.hil_lock.ENV_DROP = ('HIL_SELFTEST_NO_SCOPE',)
        self.started = {}
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
        return ['--roster', str(TINYUSB), '--roster', str(TINYUF2)]

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

    def assert_ended_blocked(self, child, token):
        """The run ended with exit 1, every lock released, the fence cleared and its blocker logged on every board
        by its keeper."""
        self.assertEqual(child.wait(), 1)
        recs = self.records()
        self.assertEqual(set(recs), BOARDS)
        self.assertEqual(self.held(), set())
        self.assertIsNone(hr.hil_lock.fence_state())
        for lock in recs:
            self.assertTrue(self.flock_free(lock), lock)
            f = latest_failure(hr.hil_lock, lock)
            self.assertEqual((f['how'], f['tool'], f['token'], f['pid'], f['task']['token']),
                             ('ended-failed', 'hil_reserve', token, f['task']['pid'], token))
            self.assertNotEqual(f['pid'], child.pid)
            self.assertEqual(sorted(f['task']['locks']), sorted(recs))

    def assert_retained(self, child, line, phase='survivors'):
        """The run exited 1 while its keeper, named on the BLOCKER line, holds every lock as a cleanup holder, lock
        admission and the host fence, the blocker logged on every board; returns (token, keeper pid)."""
        token, keeper = token_of(line), keeper_of(line)
        self.track(keeper)
        self.assertIn(f'until `hil_lock.py release --token {token}` once its descendants ended', line)
        self.assertEqual(child.wait(), 1, line)
        self.assertIsNotNone(hr.hil_lock.proc_start(keeper), 'the keeper ended with the foreground')
        recs = self.records()
        self.assertLessEqual(BOARDS, set(recs))
        for lock, rec in recs.items():
            self.assertFalse(self.flock_free(lock), lock)
            self.assertEqual((rec['pid'], rec['role'], rec['token'], rec['cleanupOwed']),
                             (keeper, 'cleanup-holder', token, True), lock)
            f = latest_failure(hr.hil_lock, lock)
            self.assertEqual((f['how'], f['tool'], f['token'], f['pid']),
                             ('ended-failed', 'hil_reserve', token, keeper))
        with self.assertRaisesRegex(hr.hil_lock.RecoveryBlocked, 'a hil_reserve keeper holding the host'):
            hr.hil_lock.take_board_lock('raspberry_pi_pico2', 'a later task')
        with self.assertRaises(hr.hil_lock.AdmissionClosed):
            hr.hil_lock.take_board_lock('unseen_board', 'a new board')
        self.assertEqual({k: v for k, v in hr.hil_lock.fence_state().items() if k in ('token', 'pid', 'phase')},
                         {'token': token, 'pid': keeper, 'phase': phase})
        return token, keeper

    def track(self, pid):
        """pid, ended at cleanup (helpers.end) unless `kill` ended it first."""
        self.started[pid] = hr.hil_lock.proc_start(pid)
        self.addCleanup(self.kill, pid)
        return pid

    def kill(self, pid):
        end(pid, self.started[pid])

    def orphan(self, suffix='.pid'):
        return self.track(int(Path(str(self.argv_out) + suffix).read_text()))

    def gate(self):
        """The file a gated stub action waits for; touched at cleanup at the latest."""
        gate = Path(str(self.argv_out) + '.gate')
        self.addCleanup(gate.touch)
        return gate

    def wait_ended(self):
        self.wait_for(lambda: (hr.hil_lock.fence_state() or {}).get('phase') == 'ended',
                      'the keeper to mark its subtree ended')

    def assert_released(self, token, keeper):
        """release_token ends the keeper: every lock free, the fence gone, admission open again."""
        locks = sorted(self.records())
        self.assertEqual(hr.hil_lock.release_token(token), (keeper, locks))
        self.assertIsNone(hr.hil_lock.fence_state())
        self.assertEqual(self.held(), set())
        for lock in locks:
            self.assertTrue(self.flock_free(lock), lock)
        hr.hil_lock.take_board_lock('late_board', 'after the release').close()
        self.wait_gone(keeper)

    def wait_gone(self, pid):
        self.wait_for(lambda: hr.hil_lock.proc_start(pid) is None or exited(pid), f'pid {pid} to exit')
        with suppress(ChildProcessError):
            os.waitpid(pid, 0)        # an orphan the survivor guard's subreaper adopted


class Rosters(Fixture):
    def test_both_shapes_resolve_dedupe_and_sort(self):
        r = hr.load_rosters([str(TINYUSB), str(TINYUF2)])
        # tinyusb lists raspberry_pi_pico2 and stm32h743eval twice (one parked); tinyuf2's imxrt1064_evk
        # locks as its registry name, mimxrt1064_evk, which tinyusb also names; its bench stm32f303disco as its own
        self.assertEqual(r['locks'], ['mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'])
        self.assertEqual(r['identities']['e6633861a3978538'], {'raspberry_pi_pico2'})
        self.assertEqual(r['identities']['000725299165'], {'mimxrt1064_evk'})   # tinyuf2 flasher.uid
        self.assertNotIn('820000000000000045b46338', r['identities'])   # dev_attached: the DUT's own host port
        self.assertEqual(r['identities']['uart-1064'], {'mimxrt1064_evk'})

    def test_a_probe_shared_by_two_entries_maps_to_both_locks(self):
        self.assertEqual(hr.load_rosters([str(TINYUSB)])['identities']['shared-probe-serial'],
                         {'raspberry_pi_pico2', 'stm32h743eval'})

    def test_a_bench_board_locks_as_its_roster_name_whatever_a_project_field_says(self):
        roster = Path(self.td.name) / 'r.json'
        roster.write_text(json.dumps({'boards': [{'name': 'bench', 'uid': 'B', 'tinyuf2': {'lock': 'shared'},
                                                  'flasher': {'name': 'jlink', 'uid': 'pb'}}]}))
        self.assertEqual(hr.load_rosters([str(roster)])['locks'], ['bench'])

    def test_identities_compare_as_udev_records_a_serial(self):
        self.assertEqual(hr.identity('  AB  CD/x,y '), 'ab_cd_x_y')
        roster = Path(self.td.name) / 'r.json'
        roster.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'AB CD', 'flasher': {'name': 'jlink', 'uid': 'pa'}},
                                                 {'name': 'b', 'uid': 'ab_cd', 'flasher': {'name': 'jlink', 'uid': 'pb'}}]}))
        with self.assertRaisesRegex(hr.Refused, "identities 'ab cd' and 'ab_cd' are one and the same to udev"):
            hr.load_rosters([str(roster)])
        roster.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'AB CD', 'flasher': {'name': 'jlink', 'uid': 'pa'}}]}))
        got = hr.load_rosters([str(roster)])
        self.assertEqual(got['identities'], {'ab_cd': {'a'}, 'pa': {'a'}})
        self.dev('3-1.4', serial='AB CD')                    # the sysfs form matches...
        (self.sys / '3-1.5').mkdir()
        self.dev('3-1.5', udev='AB_CD')                      # ...and so does udev's
        nodes = hr.walk('3-1.4', got['identities'], time.monotonic() + 5)
        self.assertEqual((nodes['3-1.4']['covered'], nodes['3-1.5']['covered']), (['a'], ['a']))

    def test_a_dut_uid_under_two_lock_names_is_a_conflict(self):
        bad = Path(self.td.name) / 'bad.json'
        bad.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'X1', 'flasher': {'name': 'jlink', 'uid': 'pa'}}, {'name': 'b', 'uid': 'x1', 'flasher': {'name': 'jlink', 'uid': 'pb'}}]}))
        with self.assertRaisesRegex(hr.Refused, 'uid x1 names two boards: a and b'):
            hr.load_rosters([str(bad)])

    def test_every_image_uid_is_a_dut_identity_and_conflicts_across_boards(self):
        roster = Path(self.td.name) / 'r.json'
        esp = {'name': 'esp', 'uid': 'primary-serial', 'uids': {'hwcdc': 'primary-serial', 'default': 'alternate-serial'},
               'flasher': {'name': 'esptool', 'uid': 'pa'}}
        roster.write_text(json.dumps({'boards': [esp]}))
        got = hr.load_rosters([str(roster)])
        self.assertEqual(got['identities'], {'primary-serial': {'esp'}, 'alternate-serial': {'esp'}, 'pa': {'esp'}})
        self.dev('3-1.4', serial='alternate-serial')         # the other image's enumeration
        self.assertEqual(hr.walk('3-1.4', got['identities'], time.monotonic() + 5)['3-1.4']['covered'], ['esp'])
        other = {'name': 'other', 'uid': 'Alternate-Serial', 'flasher': {'name': 'jlink', 'uid': 'pb'}}
        roster.write_text(json.dumps({'boards': [esp, other]}))
        with self.assertRaisesRegex(hr.Refused, 'uid alternate-serial names two boards: esp and other'):
            hr.load_rosters([str(roster)])

    def test_a_registered_board_is_one_lock_whatever_each_roster_calls_it(self):
        probe, entry = next(iter(hr.hil_boards.registry().items()))
        a, b = Path(self.td.name) / 'a.json', Path(self.td.name) / 'b.json'
        a.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'FW-A', 'probe': probe}]}))
        b.write_text(json.dumps({'boards': [{'name': 'b', 'uid': 'FW-B', 'probe': probe}]}))
        got = hr.load_rosters([str(a), str(b)])
        lock = {entry['name']}
        self.assertEqual(got, {'locks': [entry['name']],
                               'identities': {'fw-a': lock, 'fw-b': lock, probe.lower(): lock}})

    def test_a_registered_board_locks_as_its_registry_name(self):
        probe, entry = next(iter(hr.hil_boards.registry().items()))
        roster = Path(self.td.name) / 'r.json'
        roster.write_text(json.dumps({'boards': [{'name': 'its-repo-name', 'uid': 'X', 'probe': probe}]}))
        self.assertEqual(hr.load_rosters([str(roster)])['locks'], [entry['name']])

    def test_attached_devices_are_no_identities_registry_or_roster_copy(self):
        probe = next(p for p, e in hr.hil_boards.registry().items() if 'dev_attached' in e)
        roster = Path(self.td.name) / 'r.json'
        roster.write_text(json.dumps({'boards': [{'name': 'x', 'uid': 'DUT-UID', 'probe': probe, 'tests': {
            'dev_attached': [{'vid_pid': '1a86_7523', 'serial': '0'}]}}]}))
        ids = hr.load_rosters([str(roster)])['identities']
        self.assertEqual(set(ids), {'dut-uid', probe.lower()})

    def test_each_rosters_legacy_copy_warns_naming_its_file(self):
        probe, entry = next((p, e) for p, e in hr.hil_boards.registry().items() if 'dev_attached' in e)
        a, b = Path(self.td.name) / 'a.json', Path(self.td.name) / 'b.json'
        for roster in (a, b):
            roster.write_text(json.dumps({'boards': [{'name': 'x', 'uid': 'DUT-UID', 'probe': probe,
                                                      'tests': {'dev_attached': entry['dev_attached']}}]}))
        hr.hil_boards._warned.clear()
        err = io.StringIO()
        with redirect_stderr(err):
            hr.load_rosters([str(a), str(b)])
        self.assertEqual(err.getvalue().splitlines(), [f'hil_boards: roster {r}: x: tests.dev_attached duplicates '
                                                       f"agentrc's board registry ({entry['name']}); drop it from the roster"
                                                       for r in (a, b)])

    def test_a_roster_carrying_a_registered_boards_flasher_refuses(self):
        probe, entry = next(iter(hr.hil_boards.registry().items()))
        bad = Path(self.td.name) / 'bad.json'
        bad.write_text(json.dumps({'boards': [{'name': 'a', 'uid': 'X', 'probe': probe, 'flasher': entry['flasher']}]}))
        with self.assertRaisesRegex(hr.Refused, "come from agentrc's board registry"):
            hr.load_rosters([str(bad)])

    def test_malformed_rosters_and_lock_names_refuse(self):
        bad = Path(self.td.name) / 'bad.json'
        for text, why in (('{', 'roster .*bad.json'), (json.dumps({'boards': []}), 'no boards'),
                          (json.dumps({'boards': [{'uid': 'u'}]}), 'entry without a name'),
                          (json.dumps({'boards': [{'name': '../etc', 'uid': 'u'}]}), 'not a plain file name')):
            bad.write_text(text)
            with self.assertRaisesRegex(hr.Refused, why):
                hr.load_rosters([str(bad)])
        with self.assertRaisesRegex(hr.Refused, 'roster .*missing.json'):
            hr.load_rosters([str(Path(self.td.name) / 'missing.json')])


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
            got = hr.load_rosters([str(path)])
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
            got = hr.load_rosters([str(path)])
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

    def test_an_attached_devices_serial_covers_no_host_node(self):
        self.dev('3-1.4', serial='820000000000000045B46338')   # the pico2 roster's dev_attached serial
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual(rc, 1)
        self.assertIn('3-1.4: serial 820000000000000045B46338 matches no roster identity', out)

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
        for argv in (('check', '../x', *self.rosters()), ('check', '3-1', '--roster', 'a', 'tinyuf2.lock'),
                     ('run', 'pci-rebind', '3-1', *self.rosters(), '--reason', 'r'),
                     ('run', 'root-cycle', '3-1', *self.rosters(), '--reason', 'r', '--script', 'x'),
                     ('check', '3-1', '--scope', 'port', *self.rosters())):
            with self.assertRaises(SystemExit):
                main(*argv)


class Run(Fixture):
    def test_a_clean_cycle_holds_every_lock_on_the_host_during_the_command_and_releases(self):
        hr.hil_lock.release_board_lock(hr.hil_lock.take_board_lock('other-board', 'an earlier task'))
        p = self.run_run(action='probe-locks')
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.argv_out.read_text().split(), ['root-cycle', '3-1.1', 'E6633861A3978538'])
        self.assertIn('holding mimxrt1064_evk other-board raspberry_pi_pico2 stm32f303disco stm32h743eval\n'
                      'hil_reserve: of them in no roster given: other-board', out)
        self.assertIn(f'sudo -n {hr.RECOVER_SCRIPT} root-cycle 3-1.1 E6633861A3978538', out)
        self.assertIn('released 5 lock(s); 4 node(s) back', out)
        probed = dict(line.split() for line in Path(str(self.argv_out) + '.locks').read_text().splitlines())
        self.assertEqual(probed, dict.fromkeys(BOARDS | {'other-board'}, 'held'))
        self.assertEqual(self.held(), set())
        self.assertIsNone(hr.hil_lock.fence_state())
        for lock in BOARDS | {'other-board'}:
            self.assertTrue(self.flock_free(lock), lock)
        unrelated = hr.hil_lock.take_board_lock('other-board', 'bystander')     # any held lock refuses the run
        self.addCleanup(unrelated.close)
        self.argv_out.unlink()
        p = self.run_run()
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('refused: other-board: board locked: ', out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), {'other-board'})

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
            def write_record(fh, *a):
                fh.write(json.dumps({'pid': os.getpid(), 'reason': 'half'}))
                fh.flush()
                raise OSError('disk full')
            m.hil_lock._write_record = write_record
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual((p.returncode, 'board lock unavailable: holder record: disk full' in out), (1, True), out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())
        self.assertTrue(all(self.flock_free(l) for l in self.records()))

    def test_a_root_cycle_refusal_before_its_mutation_releases_when_the_subtree_is_unchanged(self):
        p = self.run_run(action='refuse')
        out = p.output()
        self.assertEqual((p.returncode, 'root-cycle refused before writing anything' in out), (1, True), out)
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

    def test_a_failed_command_is_a_blocker_logged_on_every_board_and_released(self):
        p = self.run_run(action='fail')
        line = p.blocker()
        self.assertIn('command exited 7', line)
        self.assertIn("released 4 lock(s), the blocker logged in each board's cleanup log", line)
        self.assert_ended_blocked(p, token_of(line))
        self.assertIn('command exited 7', latest_failure(hr.hil_lock, 'raspberry_pi_pico2')['detail'])
        q = self.run_run()                              # the next task takes every board and reports the failure
        out = q.output()
        self.assertEqual(q.returncode, 0, out)
        for lock in ('mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'):
            self.assertIn(f"hil_reserve: {lock}: an earlier task's cleanup failed: ", out)
        self.assertIn('command exited 7', out.split('hil_reserve: holding')[0])
        self.assertIn('command exited 7', latest_failure(hr.hil_lock, 'raspberry_pi_pico2')['detail'])

    def test_release_is_gone(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            hr.main(['release', '--token', 't'])
        self.assertEqual(cm.exception.code, 2)

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
        self.assertEqual(p.wait(), 1)
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
        empty = Path(self.td.name) / 'empty'
        empty.mkdir()

        def patch(m):
            os.environ['PATH'] = str(empty)         # no sudo to find
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('refused: cannot start sudo: ', out)
        self.assertEqual(self.held(), set())
        self.assertTrue(all(self.flock_free(l) for l in self.records()))

    def test_a_blocker_whose_log_cannot_be_written_still_releases(self):
        def patch(m):
            def no_space(board, event):
                raise OSError(28, 'No space left on device')
            m.hil_lock._log_append = no_space
        p = self.run_run(action='fail', patch=patch)
        line = p.blocker()
        self.assertIn('not logged: mimxrt1064_evk: cleanup log not written', line)
        self.assertIn('No space left on device', line)
        self.assertEqual(p.wait(), 1)
        held = self.held()       # each record kept, its only evidence, for the next take to log found-left
        self.assertTrue(held)
        self.assertTrue(all(self.flock_free(l) for l in held))

    def test_a_missing_sibling_after_a_successful_command_is_a_blocker_once_the_budget_is_spent(self):
        p = self.run_run(action='drop-sibling')
        line = p.blocker()
        self.assertIn('3-1.3 (serial 000725299165) not back: absent', line)
        self.assertIn('not restored within 2 s', line)
        self.assertNotIn('command exited', line)
        self.assert_ended_blocked(p, token_of(line))

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

            def walk(busport, identities, deadline, cached=True, **kw):
                if not cached:
                    time.sleep(1.2)                  # the reads of the look outlast the 1 s budget
                return real(busport, identities, deadline, cached, **kw)
            m.walk = walk
        p = self.run_run(patch=patch, budget='1')
        line = p.blocker()
        self.assertIn('coverage check over budget', line)
        self.assertIn('not restored within 1 s', line)
        self.assert_ended_blocked(p, token_of(line))

    def test_a_wedged_target_is_admitted_by_its_udev_record_and_restored_only_by_sysfs(self):
        (self.sys / '3-1.1' / 'serial').unlink()             # the wedged read gives nothing
        self.dev('3-1.1', udev='E6633861A3978538')
        rc, out = main('check', '3-1.1', *self.rosters())
        self.assertEqual((rc, '3-1.1: serial E6633861A3978538 (udev) -> raspberry_pi_pico2' in out), (0, True), out)
        p = self.run_run(action='ok')                        # the cycle "worked" but the serial stays unreadable
        line = p.blocker()
        self.assertEqual(self.argv_out.read_text().split(), ['root-cycle', '3-1.1', 'E6633861A3978538'])
        self.assertIn('3-1.1 (serial E6633861A3978538 (udev)) not back: serial unreadable', line)
        self.assertEqual(p.wait(), 1)
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
        self.assertEqual(p.wait(), 1)
        self.dev('3-1', cls='09')
        q = self.run_run(action='new-node', busport='3-1.2')
        self.assertIn('3-1.9: new node (serial new)', q.blocker())

    def test_a_survivor_keeps_every_lock_admission_and_the_fence_until_its_token_release(self):
        for action in ('survivor', 'survivor-same-group'):
            with self.subTest(action=action):
                p = self.run_run(action=action, budget='0.3')
                line = p.blocker()
                orphan = self.orphan()
                token, keeper = self.assert_retained(p, line)
                self.assertIn(f'surviving pids [{orphan}]', line)
                self.assertIn(f'surviving pids [{orphan}]', latest_failure(hr.hil_lock, 'raspberry_pi_pico2')['detail'])
                os.kill(keeper, signal.SIGUSR1)          # a release request before the subtree ended is dropped
                with self.assertRaisesRegex(hr.hil_lock.ReleaseRefused,
                                            rf"not signalled: .*its command's descendants are not proven ended "
                                            rf"\(surviving pids \[{orphan}\]\)"):
                    hr.hil_lock.release_token(token)
                self.kill(orphan)
                self.wait_ended()
                self.assertTrue(all(not self.flock_free(lock) for lock in BOARDS))
                self.assert_released(token, keeper)
                self.argv_out.unlink()

    def test_a_descendant_forked_after_the_budget_keeps_the_host_until_it_ends(self):
        gate = self.gate()
        p = self.run_run(action='late-fork', budget='0.3')
        line = p.blocker()
        first = self.orphan()
        token, keeper = self.assert_retained(p, line)
        self.assertRegex(line, rf'surviving pids \[{first}[],]')         # the subshell, maybe its sleep
        gate.touch()
        self.wait_for(lambda: Path(str(self.argv_out) + '.late').exists(), 'the late fork')
        late = self.orphan('.late')
        self.wait_for(lambda: hr.hil_lock.proc_start(first) is None, 'the named survivor to end')
        with self.assertRaisesRegex(hr.hil_lock.ReleaseRefused, "its command's descendants are not proven ended"):
            hr.hil_lock.release_token(token)
        self.assertEqual(hr.hil_lock.fence_state()['phase'], 'survivors')
        self.kill(late)
        self.wait_ended()
        self.assert_released(token, keeper)

    def test_a_root_descendant_through_real_sudo_is_reaped_before_the_release(self):
        sudo = shutil.which('sudo', path=self.saved[4])
        if sudo is None or subprocess.run([sudo, '-n', 'true'], capture_output=True).returncode != 0:
            self.skipTest('no passwordless `sudo -n` here to start a root descendant with')
        os.environ['REAL_SUDO'] = sudo
        self.addCleanup(os.environ.pop, 'REAL_SUDO', None)
        gate = self.gate()
        p = self.run_run(action='sudo-survivor', budget='0.3')
        line = p.blocker()
        root = int(Path(str(self.argv_out) + '.pid').read_text())     # sudo itself, its root sh its child
        token, keeper = self.assert_retained(p, line)
        self.assertIn(f'surviving pids [{root}', line)
        gate.touch()
        self.wait_ended()                      # the root sh ends at the gate, sudo with it, reaped by the keeper
        self.assertIsNone(hr.hil_lock.proc_start(root))
        self.assert_released(token, keeper)

    def test_a_survivors_logged_failure_stays_open_after_a_later_restore(self):
        p = self.run_run(action='survivor', budget='0.3')
        line = p.blocker()
        orphan = self.orphan()
        token, keeper = self.assert_retained(p, line)
        self.kill(orphan)
        self.wait_ended()
        self.assert_released(token, keeper)
        for lock in BOARDS:
            f = latest_failure(hr.hil_lock, lock)
            [marker] = f['receipts']
            self.assertEqual((marker['unrecheckable'][-3:], marker['survivors']), (['root-cycle', '3-1.1', 'E6633861A3978538'], [orphan]))
            self.assertRegex(hr.hil_lock.resolve_restored(lock, [f], evidence='a later restore')[0],
                             f'{f["id"]} stays open')
            self.assertEqual(latest_failure(hr.hil_lock, lock)['id'], f['id'], lock)

    def test_sigterm_during_the_command_is_forwarded_and_ends_as_a_blocker(self):
        p = self.run_run(action='hang')
        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
        time.sleep(0.2)
        p.send_signal(signal.SIGTERM)
        line = p.blocker()
        self.assertIn('SIGTERM forwarded', line)
        self.assertIn('command exited -15', line)

    def test_a_signal_during_the_spawn_waits_for_the_group_and_follows_the_command_policy(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            for when in ('before', 'after'):
                with self.subTest(sig=sig.name, when=when):
                    def patch(m, sig=sig, when=when):
                        # a second thread, as a sysfs reader can be: the kernel may hand it the signal
                        threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
                        real = m.os.posix_spawnp

                        def spawn(*a, **k):
                            if when == 'before':
                                os.kill(os.getpid(), sig)      # lands just before the spawn
                            pid = real(*a, **k)
                            if when == 'after':
                                os.kill(os.getpid(), sig)      # lands before the group is recorded
                            return pid
                        m.os.posix_spawnp = spawn
                    p = self.run_run(action='hang', patch=patch)
                    if sig == signal.SIGHUP:            # noted, the command runs on until a SIGTERM
                        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
                        time.sleep(0.3)
                        self.assertIsNone(p.poll())
                        p.send_signal(signal.SIGTERM)
                    line = p.blocker()
                    if sig == signal.SIGHUP:
                        self.assertIn('SIGHUP ignored during command', line)
                    self.assertIn('SIGTERM forwarded', line)
                    self.assertIn('command exited -15', line)
                    self.assertEqual(p.wait(), 1)
                    self.assertEqual(self.held(), set())
                    self.argv_out.unlink(missing_ok=True)      # a SIGTERM may end the stub before it writes

    def test_the_command_starts_unblocked_with_default_dispositions_and_the_mask_is_restored(self):
        mask_after = Path(self.td.name) / 'mask-after'

        def patch(m):
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR2, signal.SIGHUP})   # blocked before run
            real = m.Reservation.run

            def run(res, argv):
                rc = real(res, argv)
                mask_after.write_text(' '.join(sorted(s.name for s in signal.pthread_sigmask(signal.SIG_BLOCK, []))))
                return rc
            m.Reservation.run = run
        p = self.run_run(action='signals', patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(mask_after.read_text(), 'SIGHUP SIGUSR1 SIGUSR2')     # SIGUSR1 the keeper's own
        status = dict(line.split(':\t') for line in Path(str(self.argv_out) + '.sig').read_text().splitlines())
        bit = lambda s: 1 << (s - 1)
        self.assertEqual(int(status['SigBlk'], 16), 0)
        ignored = int(status['SigIgn'], 16)
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGPIPE, signal.SIGXFSZ):
            self.assertFalse(ignored & bit(s), s.name)

    def test_a_thread_started_before_the_hold_blocks_sigusr1(self):
        sig = Path(self.td.name) / 'thread-sig'

        def patch(m):
            real = m.check

            def read_own_mask():
                with open(f'/proc/self/task/{threading.get_native_id()}/status') as f:
                    sig.write_text(next(line for line in f if line.startswith('SigBlk:')).split()[1])

            def check(*a, **kw):        # in the keeper, where a sysfs read may strand its reader thread
                t = threading.Thread(target=read_own_mask)
                t.start()
                t.join()
                return real(*a, **kw)
            m.check = check
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertTrue(int(sig.read_text(), 16) & 1 << (signal.SIGUSR1 - 1))

    def test_a_run_started_with_a_standard_fd_closed_reports_on_the_others_and_releases(self):
        for closed in ('0', '1', '2', '12'):
            for action, rc in (('ok', 0), ('fail', 1)):
                with self.subTest(closed=closed, action=action):
                    os.environ['ACTION'] = action
                    shut = ' '.join(f'{fd}<&-' if fd == '0' else f'{fd}>&-' for fd in closed)
                    p = subprocess.run(['sh', '-c', f'exec "$@" {shut}', 'sh', sys.executable, '-I', '-c', FRESH_RUN,
                                        closed, str(SCRIPT), str(self.sys), str(self.udev), str(self.locks),
                                        hr.RECOVER_SCRIPT, 'run', 'root-cycle', '3-1.1', *self.rosters(),
                                        '--reason', 'test', '--budget', '2'],
                                       capture_output=True, text=True, timeout=60)
                    self.assertEqual(p.returncode, rc, p)
                    for fd, text in (('1', p.stdout), ('2', p.stderr)):
                        if fd in closed:
                            self.assertEqual(text, '', p)
                        elif fd == '1' or rc:          # a blocker goes to both
                            self.assertIn('released 4 lock(s)', text, p)
                    self.assertEqual(self.held(), set())
                    self.assertTrue(all(self.flock_free(lock) for lock in BOARDS))

    def test_a_spool_that_keeps_growing_never_stalls_the_relay(self):
        spool = tempfile.TemporaryFile()
        self.addCleanup(spool.close)
        spool.write(b'a' * 4 * 65536)
        spool.flush()
        spool.seek(0)
        fd, real_read, reads = spool.fileno(), os.read, []
        status_r, status_w = os.pipe()
        self.addCleanup(os.close, status_r)
        os.write(status_w, b'exit 0\n')
        os.close(status_w)

        def read(f, n):
            chunk = real_read(f, n)
            if f == fd:              # a descendant that writes as much again as every read takes
                reads.append(len(chunk))
                if len(reads) > 64:
                    raise AssertionError('the relay never left the spool')
                os.pwrite(fd, b'b' * len(chunk), os.fstat(fd).st_size)
            return chunk
        out = io.StringIO()
        with mock.patch.object(hr.os, 'read', read):
            self.assertEqual(hr._relay(os.getpid(), status_r, None, ((fd, out),), [True]), 0)
        self.assertEqual(out.getvalue(), 'a' * 4 * 65536 + 'b' * 4 * 65536)

    def test_a_forwarded_sigterm_reaches_the_commands_trap_and_a_hangup_does_not(self):
        p = self.run_run(action='trap')
        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
        time.sleep(0.3)
        p.send_signal(signal.SIGHUP)                    # not forwarded: the trap must not fire
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        p.send_signal(signal.SIGTERM)
        out = p.through_blocker()
        self.assertIn('TRAPPED\n', out)
        self.assertNotIn('HUP TRAPPED', out)
        self.assertIn('SIGHUP ignored during command', out)
        self.assertIn('SIGTERM forwarded', out)
        self.assertIn('command exited 7', out)
        self.assert_ended_blocked(p, token_of(out.splitlines()[-1]))

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
            real = m.hil_process.reap_until

            def reap_until(deadline):
                os.kill(os.getpid(), signal.SIGTERM)       # during reconciliation
                return real(deadline)
            m.hil_process.reap_until = reap_until
        q = self.run_run(action='fail', patch=after)
        line = q.blocker()
        self.assertIn('SIGTERM ignored during post', line)
        self.assertEqual(q.wait(), 1)


class Keeper(Fixture):
    """The keeper owns the run and outlives its foreground (agentrc#586): a foreground killed before the go runs
    nothing, one killed later leaves the run to finish, and a keeper killed once its fence is armed leaves the
    host refusing every lock, until a reboot or, once it had marked its subtree ended, its token release."""

    def keeper_pid(self):
        self.wait_for(lambda: (self.records().get('raspberry_pi_pico2') or {}).get('pid'), 'the keeper to hold')
        return self.records()['raspberry_pi_pico2']['pid']

    def assert_fenced(self, why):
        for take in (lambda: hr.hil_lock.take_board_lock('raspberry_pi_pico2', 'a later task'),
                     lambda: hr.hil_lock.take_board_lock('late_board', 'a new board')):
            with self.assertRaisesRegex(hr.hil_lock.FenceHeld, why):
                take()
        self.assertIsNone(hr.hil_lock.read_record('late_board'))     # its file may exist: the take refused

    def test_a_foreground_killed_before_the_go_runs_nothing_and_releases(self):
        gate, seen = Path(self.td.name) / 'gate', Path(self.td.name) / 'at-ready'

        def patch(m):
            real = m.Channel.ready

            def ready(chan):
                tmp = seen.with_name(seen.name + '.tmp')    # published whole: the test parses it once it exists
                tmp.write_text(str(os.getpid()))
                os.replace(tmp, seen)
                while not gate.exists():
                    time.sleep(0.02)
                return real(chan)
            m.Channel.ready = ready
        p = self.run_run(patch=patch)
        self.wait_for(seen.exists, 'the keeper to ask for its go')
        keeper = int(seen.read_text())
        self.assertIsNotNone(hr.hil_lock.fence_state())        # armed before the go
        p.kill()
        gate.touch()
        self.wait_gone(keeper)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())
        self.assertTrue(all(self.flock_free(lock) for lock in BOARDS))
        self.assertIsNone(hr.hil_lock.fence_state())
        hr.hil_lock.take_board_lock('late_board', 'after the run').close()

    def test_a_foreground_killed_during_the_command_leaves_the_run_to_finish(self):
        p = self.run_run(action='wait-done')
        self.wait_for(self.argv_out.exists, 'the command to start')
        keeper = self.keeper_pid()
        self.assertNotEqual(keeper, p.pid)
        p.kill()
        self.assertIsNotNone(hr.hil_lock.proc_start(keeper))
        self.assertFalse(self.flock_free('raspberry_pi_pico2'))
        Path(f'{self.argv_out}.done').touch()
        self.wait_gone(keeper)
        self.assertEqual(self.held(), set())
        self.assertIsNone(hr.hil_lock.fence_state())
        self.assertEqual([latest_failure(hr.hil_lock, lock) for lock in BOARDS], [None] * 4)

    def test_a_keeper_killed_during_the_command_leaves_the_host_fenced_until_a_reboot(self):
        p = self.run_run(action='hang-pid')
        self.wait_for(lambda: Path(str(self.argv_out) + '.pid').exists(), 'the command to start')
        command = int(Path(str(self.argv_out) + '.pid').read_text())
        self.track(command)
        keeper = self.track(self.keeper_pid())
        token = self.records()['raspberry_pi_pico2']['token']
        self.kill(keeper)
        self.assertEqual(p.wait(), 1)
        self.assertTrue(all(self.flock_free(lock) for lock in BOARDS))      # the kernel dropped them
        self.assert_fenced("gone without proving its command's subtree ended: only a host reboot clears it")
        with self.assertRaisesRegex(hr.hil_lock.ReleaseRefused, 'only a host reboot clears it'):
            hr.hil_lock.release_token(token)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hr.hil_lock.cmd_release(['raspberry_pi_pico2']), 1)
        self.assertIn('host fenced by hil_reserve', err.getvalue())
        self.assertIn('raspberry_pi_pico2', self.held())                    # its record, for the next take to log

    def test_a_keeper_killed_once_its_subtree_ended_is_cleared_by_its_token(self):
        p = self.run_run(action='survivor', budget='0.3')
        line = p.blocker()
        orphan = self.orphan()
        token, keeper = self.assert_retained(p, line)
        self.kill(orphan)
        self.wait_ended()
        self.kill(keeper)
        self.assert_fenced(f"gone after its command's subtree ended: `hil_lock.py release --token {token}` clears it")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hr.hil_lock.cmd_release_token(token), 0)
        self.assertIn(f'cleared the host fence of gone keeper pid {keeper} (token {token})', out.getvalue())
        self.assertIsNone(hr.hil_lock.fence_state())
        fh = hr.hil_lock.take_board_lock('raspberry_pi_pico2', 'the next task')      # logs what the keeper left
        self.addCleanup(fh.close)
        self.assertEqual(fh.cleanup_failures[-1]['how'], 'found-left')
        self.assertEqual(fh.cleanup_failures[-1]['task']['role'], 'cleanup-holder')

    def test_a_keeper_killed_while_finishing_leaves_the_host_fenced(self):
        def patch(m):
            real = m.Reservation.unfence

            def unfence(res):
                if res.phase == 'post':          # the command's outcome, not a refusal's release
                    os.kill(os.getpid(), signal.SIGKILL)
                return real(res)
            m.Reservation.unfence = unfence
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('ended without a status', out)
        self.assertTrue(self.argv_out.exists())
        # the status pipe's EOF can reach the foreground before the dying keeper's flocks are dropped
        self.wait_gone(self.records()['raspberry_pi_pico2']['pid'])
        self.assert_fenced('only a host reboot clears it')

    def test_a_fence_not_written_runs_nothing(self):
        def patch(m):
            def arm_fence(fence):
                raise OSError(28, 'No space left on device')
            m.hil_lock.arm_fence = arm_fence
        p = self.run_run(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('refused: host fence not written: [Errno 28] No space left on device', out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())
        hr.hil_lock.take_board_lock('late_board', 'after the run').close()

    def test_a_fence_not_cleared_keeps_the_host_until_its_token_release(self):
        def patch(m):
            real, calls = m.hil_lock.clear_fence, []

            def clear_fence(token):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError(5, 'Input/output error')
                return real(token)
            m.hil_lock.clear_fence = clear_fence
        p = self.run_run(patch=patch)
        line = p.blocker()
        self.assertIn('host fence not cleared: [Errno 5] Input/output error', line)
        self.assertNotIn('surviving pids', line)
        token, keeper = token_of(line), self.track(keeper_of(line))
        self.assertEqual(p.wait(), 1)
        self.wait_ended()
        self.assertTrue(all(not self.flock_free(lock) for lock in BOARDS))
        self.assert_released(token, keeper)

    def test_a_fence_that_cannot_be_read_at_clear_keeps_the_host_until_its_token_release(self):
        def patch(m):
            real_read, real_clear, calls = m.hil_lock._read_name, m.hil_lock.clear_fence, []

            def failing_read(name, dfd=None):
                raise OSError(5, 'Input/output error')

            def clear_fence(token):
                calls.append(1)
                if len(calls) == 1:
                    m.hil_lock._read_name = failing_read
                try:
                    return real_clear(token)
                finally:
                    m.hil_lock._read_name = real_read
            m.hil_lock.clear_fence = clear_fence
        p = self.run_run(patch=patch)
        line = p.blocker()
        self.assertIn('host fence not cleared: [Errno 5] Input/output error', line)
        token, keeper = token_of(line), self.track(keeper_of(line))
        self.assertEqual(p.wait(), 1)
        self.wait_ended()
        self.assertTrue(all(not self.flock_free(lock) for lock in BOARDS))
        self.assert_released(token, keeper)

    def test_an_ended_fence_not_written_is_retried_and_refuses_release_meanwhile(self):
        failing, tried = self.gate(), Path(self.td.name) / 'tried'

        def patch(m):
            m.PUBLISH_RETRY = (0.05, 0.1)
            real = m.hil_lock.arm_fence

            def arm_fence(fence):
                if fence['phase'] == 'ended' and not failing.exists():
                    tried.touch()
                    raise OSError(5, 'Input/output error')
                return real(fence)
            m.hil_lock.arm_fence = arm_fence
        p = self.run_run(action='survivor', patch=patch, budget='0.3')
        line = p.blocker()
        orphan = self.orphan()
        token, keeper = self.assert_retained(p, line)
        self.kill(orphan)
        self.wait_for(tried.exists, 'the keeper to fail to publish `ended`')
        with self.assertRaisesRegex(hr.hil_lock.ReleaseRefused, "its command's descendants are not proven ended"):
            hr.hil_lock.release_token(token)
        self.assertTrue(all(not self.flock_free(lock) for lock in BOARDS))
        failing.touch()
        self.wait_ended()
        self.assert_released(token, keeper)

    def fail_release(self, case):
        """A retained run whose first checked release fails as `case(m, faulty, tried)` makes it, while `faulty`
        exists; returns (token, keeper, the file `case` touches once it failed, `faulty`)."""
        faulty, tried = Path(self.td.name) / 'faulty', Path(self.td.name) / 'tried'
        faulty.touch()

        def patch(m):
            m.PUBLISH_RETRY = (0.05, 0.1)
            case(m, faulty, tried)
        p = self.run_run(action='survivor', patch=patch, budget='0.3')
        line = p.blocker()
        orphan = self.orphan()
        token, keeper = self.assert_retained(p, line)
        self.kill(orphan)
        self.wait_ended()
        with mock.patch.object(hr.hil_lock, 'RELEASE_WAIT', 0.5):
            with self.assertRaisesRegex(hr.hil_lock.ReleaseRefused, 'signalled, but holder pid'):
                hr.hil_lock.release_token(token)
        self.wait_for(tried.exists, 'the release to fail')
        return token, keeper, faulty

    def assert_released_once_healed(self, token, keeper, faulty):
        self.assertTrue(all(not self.flock_free(lock) for lock in BOARDS))
        faulty.unlink()
        self.wait_for(lambda: all((r or {}).get('role') == 'cleanup-holder' for r in self.records().values())
                      and (hr.hil_lock.fence_state() or {}).get('phase') == 'ended', 'the keeper to publish again')
        self.assert_released(token, keeper)

    def test_a_release_whose_fence_unlink_is_not_durable_publishes_again(self):
        def case(m, faulty, tried):
            real = m.hil_lock._unlink_fence

            def unlink_fence():
                real()
                if faulty.exists():
                    tried.touch()
                    raise OSError(5, 'Input/output error')      # the dir fsync, after the unlink
            m.hil_lock._unlink_fence = unlink_fence
        self.assert_released_once_healed(*self.fail_release(case))

    def test_a_release_whose_record_restore_fails_once_retries_it(self):
        def case(m, faulty, tried):
            real_unlink, real_write = m.hil_lock._unlink_fence, m.hil_lock.write_record

            def unlink_fence():
                if not tried.exists():
                    tried.touch()
                    raise OSError(5, 'Input/output error')
                real_unlink()

            def write_record(fh, reason, **extra):
                if tried.exists() and faulty.exists() and os.readlink(f'/proc/self/fd/{fh.fileno()}').endswith(
                        '/stm32f303disco.lock'):
                    raise OSError(5, 'Input/output error')
                real_write(fh, reason, **extra)
            m.hil_lock._unlink_fence, m.hil_lock.write_record = unlink_fence, write_record
        token, keeper, faulty = self.fail_release(case)
        self.assertIsNone(hr.hil_lock.read_record('stm32f303disco'))
        self.assert_released_once_healed(token, keeper, faulty)

    def test_a_keeper_started_with_a_dropped_variable_is_execd_afresh_without_it(self):
        hr.hil_lock.ENV_DROP = ('HIL_SELFTEST_SCOPE',)
        seen = Path(self.td.name) / 'exec'
        wrapper = self.bin / 'python'
        wrapper.write_text(f'#!/bin/sh\nenv > {seen}.env\nprintf "%s\\0" "$@" > {seen}.argv\n'
                           f'exec {sys.executable} "$@"\n')
        wrapper.chmod(0o755)
        for k, v in (('HIL_SELFTEST_SCOPE', 'a terminal'), ('HIL_SELFTEST_MARK', 'kept')):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        # a busport no host has: the exec'd keeper walks this host's own sysfs, and refuses before any hold
        p = self.run_run(busport='99-9', patch=lambda m: setattr(m.sys, 'executable', str(wrapper)))
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('hil_reserve: refused: no device at 99-9', out)
        env = Path(f'{seen}.env').read_text().splitlines()
        self.assertIn('HIL_SELFTEST_MARK=kept', env)
        self.assertFalse([e for e in env if e.startswith('HIL_SELFTEST_SCOPE=')], env)
        argv = Path(f'{seen}.argv').read_text().split('\0')
        self.assertEqual((argv[:2], argv[3:6], json.loads(argv[6])[0]),
                         (['-I', '-c'], ['agentrc_hil_reserve', str(SCRIPT), 'keeper_main'], str(self.locks)))
        self.assertEqual(self.records(), {})


class HubScope(Fixture):
    """hub-cycle's coverage walk spans the target's parent hub, not its root port; the lock
    set stays every roster board, and a refusal before the script's first write releases."""

    def setUp(self):
        super().setUp()
        self.dev('3-1.4', cls='09', serial='hub-sn')
        self.dev('3-1.4.1', serial='stlink-f303')         # tinyuf2 stm32f303disco probe
        self.dev('3-1.5', serial='stranger')              # a sibling of the hub, outside its scope

    def run_hub(self, action='ok', busport='3-1.4.1'):
        return self.fork_main('run', 'hub-cycle', busport, *self.rosters(), '--reason', 'test',
                              '--budget', '1', action=action)

    def test_the_hub_scope_walks_only_the_parent_hub(self):
        rc, out = main('check', '3-1.4.1', *self.rosters())
        self.assertEqual((rc, 'uncovered under root port 3-1' in out, '3-1.5' in out), (1, True, True), out)
        rc, out = main('check', '3-1.4.1', '--scope', 'hub', *self.rosters())
        self.assertEqual(rc, 0, out)
        self.assertIn('3-1.4.1: serial stlink-f303 -> stm32f303disco', out)
        self.assertNotIn('3-1.5', out)
        rc, out = main('check', '3-1', '--scope', 'hub', *self.rosters())
        self.assertEqual((rc, 'on a root port' in out), (1, True), out)

    def test_hub_cycle_holds_every_roster_lock_and_releases(self):
        p = self.run_hub()
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.argv_out.read_text().split(), ['hub-cycle', '3-1.4.1', 'stlink-f303'])
        self.assertIn('holding mimxrt1064_evk raspberry_pi_pico2 stm32f303disco stm32h743eval', out)
        self.assertIn('released 4 lock(s); 2 node(s) back under hub 3-1.4', out)
        self.assertEqual(self.held(), set())

    def test_a_hub_cycle_survivor_keeps_the_host_until_its_token_release(self):
        p = self.run_hub(action='survivor')
        line = p.blocker()
        orphan = self.orphan()
        token, keeper = self.assert_retained(p, line)
        self.assertIn(f'surviving pids [{orphan}]', line)
        self.kill(orphan)
        self.wait_ended()
        self.assert_released(token, keeper)

    def test_hub_cycle_refuses_a_hub_target(self):
        shutil.rmtree(self.sys / '3-1.5')
        rc, out = main('run', 'hub-cycle', '3-1.4', *self.rosters(), '--reason', 'test')
        self.assertEqual((rc, 'is a hub: hub-cycle cycles a leaf only' in out), (1, True), out)
        self.assertFalse(self.argv_out.exists())

    def test_a_refusal_before_writing_releases_only_when_the_subtree_is_unchanged(self):
        p = self.run_hub(action='refuse')
        out = p.output()
        self.assertEqual((p.returncode, 'refused before writing anything' in out), (1, True), out)
        self.assertEqual(self.held(), set())
        p = self.run_hub(action='refuse-drop')
        line = p.blocker()
        self.assertIn('command exited 3', line)
        self.assert_ended_blocked(p, token_of(line))


class RootCycleScript(Fixture):
    """The real usb_recover.sh root-cycle under `run`, its root_port_cycle.py stubbed through the
    script's non-root seams: a refusal before the power-off (exit 3) releases, while a surviving
    run, or a hung one ended at the bound, is a blocker logged on every board, even with power
    restored."""

    def setUp(self):
        super().setUp()
        root = Path(self.td.name)
        (root / 'sysroot/bus/usb').mkdir(parents=True)
        (root / 'sysroot/bus/usb/devices').symlink_to(self.sys)
        stub = self.bin / 'root_port_cycle.py'
        for path, text in ((stub, ROOT_PORT_CYCLE), (self.bin / 'sleep', SLEEP)):
            path.write_text(text)
            path.chmod(0o755)
        hr.RECOVER_SCRIPT = str(USB_RECOVER)
        env = {'USB_RECOVER_SYSFS': str(root / 'sysroot'), 'USB_RECOVER_ROOT_PORT_CYCLE': str(stub),
               'USB_RECOVER_PORT_WRITE_TIMEOUT': '1', 'USB_RECOVER_TEST_KILL_SIG': 'USR2'}
        for k in env:
            self.addCleanup(os.environ.pop, k, None)
        os.environ.update(env)

    def runs(self):
        rpc = Path(str(self.argv_out) + '.rpc')
        return rpc.read_text().split() if rpc.exists() else []

    def test_a_refused_check_releases(self):
        p = self.run_run(action='check=refuse')
        out = p.output()
        self.assertEqual((p.returncode, 'root-cycle refused before writing anything' in out), (1, True), out)
        self.assertEqual(self.runs(), ['check'])
        self.assertEqual(self.held(), set())

    def test_a_surviving_off_keeps_the_host_until_its_token_release(self):
        p = self.run_run(action='off=survive', budget='0.3')
        line = p.blocker()
        orphan = self.orphan()
        self.assertIn('command exited 1', line)
        self.assertIn(f'surviving pids [{orphan}]', line)
        self.assertEqual(self.runs(), ['check', 'off'])          # no power-on after a survivor
        token, keeper = self.assert_retained(p, line)
        self.kill(orphan)
        self.wait_ended()
        self.assert_released(token, keeper)

    def test_a_sigterm_during_a_hung_off_powers_on_and_is_a_blocker_on_every_board(self):
        # the forwarded SIGTERM ends the off request and fires the script's trap, well before the bound
        p = self.run_run(action='off=hang')
        self.wait_for(lambda: 'off' in self.runs(), 'the power-off run')
        p.send_signal(signal.SIGTERM)
        out = p.through_blocker()
        line = out.splitlines()[-1]
        self.assertIn('failed (off 143, on 0, interrupted); power-on restored', out)
        self.assertIn('SIGTERM forwarded', line)
        self.assertIn('command exited 1', line)
        self.assertEqual(self.runs(), ['check', 'off', 'on'])
        self.assert_ended_blocked(p, token_of(line))


class ControllerFixture(Fixture):
    """A bound controller at ADDR owning bus 3 (480M) and bus 4 (5000M), a driverless one at IDLE."""
    ADDR, IDLE = '0000:05:00.0', '0000:06:00.0'
    BOARDS = frozenset({'mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'})

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.sys / '3-2')                   # the keyboard: on this controller, in no roster
        root = Path(self.td.name)
        self.pci, self.drivers = root / 'pci', root / 'drivers'
        for name, path in (('SYS_PCI_DEVICES', self.pci), ('SYS_PCI_DRIVERS', self.drivers)):
            self.addCleanup(setattr, hr, name, getattr(hr, name))
            setattr(hr, name, str(path))
        for drv in ('xhci-pci-renesas', 'xhci_hcd'):
            (self.drivers / drv).mkdir(parents=True)
        self.controller(self.ADDR, 'xhci-pci-renesas', {'usb3': '480', 'usb4': '5000'})
        self.controller(self.IDLE, None, {})
        for k in ('PCI_DEV', 'PCI_DRIVERS'):
            self.addCleanup(os.environ.pop, k, None)
        os.environ['PCI_DRIVERS'] = str(self.drivers)

    def controller(self, addr, driver, hubs, cls='0x0c0330'):
        d = self.pci / addr
        d.mkdir(parents=True)
        for attr, v in (('vendor', '0x1912'), ('device', '0x0014'), ('subsystem_vendor', '0x1912'),
                        ('subsystem_device', '0x0014'), ('class', cls)):
            (d / attr).write_text(v + '\n')
        if driver:
            (d / 'driver').symlink_to(self.drivers / driver)
        for hub, speed in hubs.items():
            (d / hub).mkdir()
            (d / hub / 'speed').write_text(speed + '\n')
        return d

    def run_ctl(self, action='ok', verb='pci-rebind', addr=ADDR, patch=None, budget='1'):
        os.environ['PCI_DEV'] = str(self.pci / addr)
        return self.fork_main('run', verb, addr, *self.rosters(), '--reason', 'test', '--budget', budget,
                              action=action, patch=patch)

    def assert_blocked_on_every_lock(self, child, line):
        self.assertEqual(child.wait(), 1, line)
        self.assertEqual(set(self.records()), self.BOARDS)
        self.assertEqual(self.held(), set())
        for lock in self.BOARDS:
            self.assertTrue(self.flock_free(lock), lock)
            f = latest_failure(hr.hil_lock, lock)
            self.assertEqual((f['how'], f['tool'], f['token']), ('ended-failed', 'hil_reserve', token_of(line)))


class Controller(ControllerFixture):
    """pci-rebind and pci-bind (agentrc#236): a PCI-identified controller whose every bus is
    walked, nodes keyed across a renumbering, every lock on the host held, the command never
    signalled and bounded, and restoration judged from the PCI function and its buses."""

    def test_check_names_the_pci_identity_and_keys_every_bus_by_speed(self):
        self.dev('5-1', serial='on-another-controller')   # bus 5 is not this controller's
        rc, out = main('check', self.ADDR, '--scope', 'controller', *self.rosters())
        self.assertEqual(rc, 0, out)
        self.assertIn(f'controller {self.ADDR}: 0x1912:0x0014 subsystem 0x1912:0x0014 class 0x0c0330, '
                      'driver xhci-pci-renesas', out)
        self.assertIn('locks (4): mimxrt1064_evk raspberry_pi_pico2 stm32f303disco stm32h743eval', out)
        for line in ('usb3 (480M): serial - -> controller', 'usb4 (5000M): serial - -> controller',
                     '3-1 (480M-1): serial - -> hub', '3-1.1 (480M-1.1): serial E6633861A3978538 -> raspberry_pi_pico2'):
            self.assertIn(line, out)
        self.assertNotIn('5-1', out)

    def test_an_unexplained_device_on_any_of_its_buses_refuses(self):
        self.dev('3-2', serial='unrelated-keyboard')
        rc, out = main('check', self.ADDR, '--scope', 'controller', *self.rosters())
        self.assertEqual(rc, 1, out)
        self.assertIn(f'uncovered on controller {self.ADDR}:\n  3-2: serial unrelated-keyboard matches no roster', out)
        p = self.run_ctl()
        self.assertEqual(p.wait(), 1)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())

    def test_a_target_that_is_no_usb_controller_or_in_the_wrong_driver_state_refuses(self):
        self.controller('0000:07:00.0', 'xhci_hcd', {}, cls='0x020000')
        for argv, why in ((('run', 'pci-rebind', '0000:07:00.0'), 'is not a USB controller (class 0x020000)'),
                          (('run', 'pci-rebind', '0000:09:00.0'), 'no such pci device: 0000:09:00.0'),
                          (('run', 'pci-rebind', self.IDLE), f'no driver bound to {self.IDLE}: pci-bind attaches one'),
                          (('run', 'pci-bind', self.ADDR), f'{self.ADDR} already has a driver bound (xhci-pci-renesas)')):
            with self.subTest(argv=argv):
                rc, out = main(*argv, *self.rosters(), '--reason', 'test')
                self.assertEqual(rc, 1, out)
                self.assertIn(why, out)
        (self.pci / self.ADDR / 'subsystem_device').unlink()
        rc, out = main('check', self.ADDR, '--scope', 'controller', *self.rosters())
        self.assertIn(f'{self.ADDR}: subsystem_device unreadable', out)
        self.assertFalse(self.argv_out.exists())
        for argv in (('run', 'pci-rebind', '05:00.0'), ('run', 'pci-bind', '3-1'), ('check', '3-1', '--scope', 'controller'),
                     ('check', self.ADDR)):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                hr.main([*argv, *self.rosters(), *(('--reason', 'r') if argv[0] == 'run' else ())])

    def test_two_root_hubs_of_one_speed_or_an_unreadable_speed_refuse(self):
        (self.pci / self.ADDR / 'usb4' / 'speed').write_text('480\n')
        rc, out = main('check', self.ADDR, '--scope', 'controller', *self.rosters())
        self.assertEqual(rc, 1, out)
        self.assertIn('usb3 and usb4 are both 480 Mbps root hubs', out)
        (self.pci / self.ADDR / 'usb4' / 'speed').unlink()
        rc, out = main('check', self.ADDR, '--scope', 'controller', *self.rosters())
        self.assertIn('usb4: speed unreadable', out)

    def test_a_rebind_that_renumbers_the_buses_is_restored_and_maps_them(self):
        p = self.run_ctl(action='renumber')
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.argv_out.read_text().split(), ['pci-rebind', self.ADDR])
        self.assertIn(f'sudo -n {hr.RECOVER_SCRIPT} pci-rebind {self.ADDR}\n', out)
        self.assertIn(f'controller {self.ADDR} buses: 3 -> 1 (480M), 4 -> 2 (5000M); re-derive every busport', out)
        self.assertIn(f'released 4 lock(s); controller {self.ADDR} back, 6 node(s) on its buses', out)
        self.assertEqual(self.held(), set())
        self.assertTrue(all(self.flock_free(l) for l in self.records()))

    def test_every_lock_name_on_the_host_is_held_and_a_held_one_refuses(self):
        hr.hil_lock.release_board_lock(hr.hil_lock.take_board_lock('arduino_board', 'an earlier task'))
        p = self.run_ctl(action='probe-locks')
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn('holding arduino_board mimxrt1064_evk raspberry_pi_pico2 stm32f303disco stm32h743eval\n'
                      'hil_reserve: of them in no roster given: arduino_board', out)
        probed = dict(line.split() for line in Path(str(self.argv_out) + '.locks').read_text().splitlines())
        self.assertEqual(set(probed.values()), {'held'})
        self.assertEqual(len(probed), 5)
        self.assertEqual(self.held(), set())
        self.argv_out.unlink()
        rival = hr.hil_lock.take_board_lock('arduino_board', 'a live task')
        self.addCleanup(rival.close)
        p = self.run_ctl()
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('refused: arduino_board: board locked: ', out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), {'arduino_board'})

    def test_a_lock_created_during_the_hold_refuses(self):
        def patch(m):
            real, calls = m.host_locks, []

            def host_locks(rosters):
                calls.append(1)
                return real(rosters) + (['late_board'] if len(calls) == 2 else [])
            m.host_locks = host_locks
        p = self.run_ctl(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('lock(s) created during the hold, so not held: late_board', out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())

    def test_no_new_lock_name_is_admitted_until_the_run_ends(self):
        done = Path(f'{self.argv_out}.done')
        self.addCleanup(done.touch)                     # a failed assertion still ends the command
        p = self.run_ctl(action='wait-done')
        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
        with self.assertRaisesRegex(hr.hil_lock.AdmissionClosed, 'late_board has no lock file yet'):
            hr.hil_lock.take_board_lock('late_board', 'a task starting mid-run')
        self.assertNotIn('late_board', hr.hil_lock.lock_names())
        done.touch()
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        hr.hil_lock.take_board_lock('late_board', 'after the run').close()

    def test_a_running_reservation_refuses_another_before_any_hold(self):
        hr.hil_lock.ensure_lock_files(['mimxrt1064_evk', 'raspberry_pi_pico2', 'stm32f303disco', 'stm32h743eval'])
        adm = hr.hil_lock.close_admission()
        self.addCleanup(adm.close)
        p = self.run_ctl(patch=lambda m: setattr(m.hil_lock, 'LOG_WAIT', 0.2))
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('refused: lock admission: board lock unavailable: .admission.mutex still shared', out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual(self.held(), set())

    def test_a_controller_that_changes_between_the_check_and_the_hold_refuses(self):
        def patch(m):
            real, calls = m.check_controller, []

            def check_controller(*a, **k):
                calls.append(1)
                if len(calls) == 2:
                    (self.pci / self.ADDR / 'driver').unlink()
                    (self.pci / self.ADDR / 'driver').symlink_to(self.drivers / 'xhci_hcd')
                return real(*a, **k)
            m.check_controller = check_controller
        p = self.run_ctl(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('the controller changed between the check and the hold', out)
        self.assertFalse(self.argv_out.exists())

    def test_a_stranded_controller_is_a_blocker_logged_on_every_board(self):
        p = self.run_ctl(action='strand')
        line = p.blocker()
        self.assertIn('command exited 1', line)
        self.assertIn(f'{self.ADDR}: no driver bound (stranded; pci-bind attaches one)', line)
        self.assertIn('not restored within 1 s', line)
        self.assert_blocked_on_every_lock(p, line)

    def test_a_lost_node_a_changed_driver_or_identity_is_a_blocker(self):
        p = self.run_ctl(action='renumber-drop')
        out = p.output()
        line = next(x for x in out.splitlines() if 'BLOCKER' in x)     # on stdout and stderr
        self.assertIn('3-1.3 (480M-1.3) (serial 000725299165) not back: absent', line)
        self.assertIn(f'controller {self.ADDR} buses: 3 -> 1 (480M), 4 -> 2 (5000M)', out)
        self.assert_blocked_on_every_lock(p, line)
        for f in self.sys.glob('1-*'):
            f.rename(self.sys / f.name.replace('1-', '3-', 1))
        for a, b in (('usb1', 'usb3'), ('usb2', 'usb4')):
            (self.pci / self.ADDR / a).rename(self.pci / self.ADDR / b)
        shutil.move(self.sys / '.gone', self.sys / '3-1.3')
        self.argv_out.unlink()

        def patch(m):
            real = m.controller_state

            def controller_state(addr, deadline):
                st = real(addr, deadline)
                if self.argv_out.exists():                    # after the command: another function at that address
                    st['ids']['device'] = '0x0015'
                return st
            m.controller_state = controller_state
        p = self.run_ctl(action='swap-driver', patch=patch)
        line = p.blocker()
        self.assertIn(f'{self.ADDR}: PCI identity changed: device 0x0014 -> 0x0015', line)
        self.assertIn(f'{self.ADDR}: bound to xhci_hcd, was xhci-pci-renesas', line)
        self.assert_blocked_on_every_lock(p, line)

    def test_a_refusal_before_the_unbind_releases_when_the_controller_is_unchanged(self):
        p = self.run_ctl(action='refuse')
        out = p.output()
        self.assertEqual((p.returncode, 'pci-rebind refused before writing anything' in out), (1, True), out)
        self.assertNotIn('BLOCKER', out)
        self.assertEqual(self.held(), set())
        self.assertEqual([latest_failure(hr.hil_lock, l) for l in self.records()], [None] * 4)

    def test_a_hung_command_is_left_running_unsignalled_past_its_bound_and_keeps_the_host(self):
        def patch(m):
            m.CONTROLLER_COMMAND_BUDGET = 0.5
        p = self.run_ctl(action='hang', patch=patch)
        line = p.blocker()
        pids = [int(x) for x in line.split('surviving pids [')[1].split(']')[0].split(', ')]
        for x in pids:
            self.track(x)
        self.assertIn('command still running after 0.5 s', line)
        token, keeper = self.assert_retained(p, line)
        self.assertTrue(all(hr.hil_lock.proc_start(x) for x in pids), 'a controller command was signalled')
        f = latest_failure(hr.hil_lock, 'raspberry_pi_pico2')
        [marker] = f['receipts']
        self.assertEqual((marker['unrecheckable'][-2:], marker['survivors']), (['pci-rebind', self.ADDR], pids))
        for x in pids:
            self.kill(x)
        self.wait_ended()
        self.assert_released(token, keeper)

    def test_signals_during_the_command_are_never_forwarded(self):
        p = self.run_ctl(action='trap')
        self.wait_for(lambda: self.argv_out.exists(), 'the command to start')
        time.sleep(0.3)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            p.send_signal(sig)
            time.sleep(0.1)
        self.assertIsNone(p.poll())
        Path(f'{self.argv_out}.done').touch()           # the trap's loop ends early
        out = p.through_blocker()
        self.assertNotIn('TRAPPED', out)
        line = out.splitlines()[-1]
        for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
            self.assertIn(f'{name} ignored during command', line)
        self.assertNotIn('forwarded', line)
        self.assert_blocked_on_every_lock(p, line)

    def test_pci_bind_attaches_a_driverless_controller_and_admits_its_returning_fixtures(self):
        shutil.rmtree(self.pci / self.ADDR)                # bus 3's devices come back under the idle controller
        rc, out = main('check', self.IDLE, '--scope', 'controller', *self.rosters())
        self.assertEqual(rc, 0, out)
        self.assertIn(f'controller {self.IDLE}: 0x1912:0x0014 subsystem 0x1912:0x0014 class 0x0c0330, driver none', out)
        p = self.run_ctl(action='bind', verb='pci-bind', addr=self.IDLE,
                         patch=lambda m: setattr(m, 'CONTROLLER_SETTLE', 0.3))
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.argv_out.read_text().split(), ['pci-bind', self.IDLE])
        self.assertIn(f'controller {self.IDLE} buses: - -> 3 (480M)', out)
        self.assertIn(f'released 4 lock(s); controller {self.IDLE} back, 5 node(s) on its buses', out)
        self.assertEqual(self.held(), set())

    def test_a_pci_bind_survivor_keeps_the_host_until_its_token_release(self):
        shutil.rmtree(self.pci / self.ADDR)
        p = self.run_ctl(action='survivor', verb='pci-bind', addr=self.IDLE,
                         patch=lambda m: setattr(m, 'CONTROLLER_SETTLE', 0.3))
        line = p.blocker()
        orphan = self.orphan()
        token, keeper = self.assert_retained(p, line)
        self.assertIn(f'surviving pids [{orphan}]', line)
        self.assertIn(f'{self.IDLE}: no driver bound', line)
        self.kill(orphan)
        self.wait_ended()
        self.assert_released(token, keeper)

    def test_pci_bind_waits_for_its_buses_to_settle_so_a_late_device_is_judged(self):
        shutil.rmtree(self.pci / self.ADDR)

        def patch(m):
            real, looks = m.controller_walk, []
            m.CONTROLLER_SETTLE = 0.3

            def controller_walk(addr, identities, deadline, cached=True):
                nodes = real(addr, identities, deadline, cached)
                if not cached:                               # a restoration look
                    looks.append(1)
                    if len(looks) == 1:                      # enumerates once the first look showed the root hub
                        self.dev('3-9', serial='late-keyboard')
                return nodes
            m.controller_walk = controller_walk
        p = self.run_ctl(action='bind', verb='pci-bind', addr=self.IDLE, patch=patch)
        line = p.blocker()
        self.assertIn('3-9: serial late-keyboard matches no roster identity', line)
        self.assertIn('not restored within 1 s', line)
        self.assert_blocked_on_every_lock(p, line)

    def test_a_refused_look_restarts_the_settle_window(self):
        rosters, seen = hr.load_rosters([str(TINYUSB), str(TINYUF2)]), {}

        def look():
            return hr.controller_gaps(state, {}, self.ADDR, rosters, time.monotonic() + 5, seen)
        state = hr.controller_state(self.ADDR, time.monotonic() + 5)
        self.assertIn(f'{self.ADDR}: buses unchanged for 0.0 of 5 s', look())
        snap = seen['settle'][0]
        seen['settle'] = (snap, time.monotonic() - 10)
        self.assertEqual(look(), [])
        sub = self.pci / self.ADDR / 'subsystem_device'
        for cut, restore in ((lambda: sub.rename(f'{sub}.off'), lambda: os.rename(f'{sub}.off', sub)),
                             (lambda: (self.pci / self.ADDR / 'driver').unlink(),
                              lambda: (self.pci / self.ADDR / 'driver').symlink_to(self.drivers / 'xhci-pci-renesas'))):
            with self.subTest(cut=cut):
                seen['settle'] = (snap, time.monotonic() - 10)
                cut()
                self.assertTrue(look())
                self.assertNotIn('settle', seen)
                restore()
                self.assertIn(f'{self.ADDR}: buses unchanged for 0.0 of 5 s', look())

    def test_a_budget_too_short_to_settle_refuses_before_any_lock(self):
        shutil.rmtree(self.pci / self.ADDR)
        p = self.run_ctl(action='bind', verb='pci-bind', addr=self.IDLE, budget='5')
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn(f'refused: --budget 5 s is under 5.5 s: with no device on its buses before, {self.IDLE}', out)
        self.assertFalse(self.argv_out.exists())
        self.assertEqual((self.held(), self.records()), (set(), {}))

    def test_pci_bind_passes_its_driver_and_only_pci_bind_takes_one(self):
        shutil.rmtree(self.pci / self.ADDR)
        os.environ['PCI_DEV'] = str(self.pci / self.IDLE)
        p = self.fork_main('run', 'pci-bind', self.IDLE, *self.rosters(), '--reason', 'test', '--budget', '1',
                           '--driver', 'xhci_hcd', action='bind', patch=lambda m: setattr(m, 'CONTROLLER_SETTLE', 0.3))
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.argv_out.read_text().split(), ['pci-bind', self.IDLE, 'xhci_hcd'])
        self.assertIn(f'sudo -n {hr.RECOVER_SCRIPT} pci-bind {self.IDLE} xhci_hcd\n', out)
        self.argv_out.unlink()
        for argv, why in ((('pci-rebind', self.ADDR, '--driver', 'xhci_hcd'), '--driver is for pci-bind only'),
                          (('root-cycle', '3-1.1', '--driver', 'xhci_hcd'), '--driver is for pci-bind only'),
                          (('pci-bind', self.IDLE, '--driver', 'xhci/../x'), 'bad driver name: xhci/../x')):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as e:
                    hr.main(['run', *argv, *self.rosters(), '--reason', 'r'])
                self.assertEqual(e.exception.code, 2)
                self.assertIn(why, err.getvalue())
        self.assertFalse(self.argv_out.exists())


class ControllerScript(ControllerFixture):
    """The real usb_recover.sh pci-rebind under `run`, against the fake PCI tree through the
    script's non-root seam: its unbind then bind land, the reservation releases, and its
    refusal before the unbind (exit 3) releases too."""

    def setUp(self):
        super().setUp()
        root = Path(self.td.name)
        (root / 'sysroot/bus/pci').mkdir(parents=True)
        (root / 'sysroot/bus/pci/devices').symlink_to(self.pci)
        (root / 'sysroot/bus/pci/drivers').symlink_to(self.drivers)
        for drv in ('xhci-pci-renesas', 'xhci_hcd'):
            for f in ('bind', 'unbind'):
                (self.drivers / drv / f).write_text('')
        (self.bin / 'sleep').write_text(SLEEP)
        (self.bin / 'sleep').chmod(0o755)
        hr.RECOVER_SCRIPT = str(USB_RECOVER)
        self.addCleanup(os.environ.pop, 'USB_RECOVER_SYSFS', None)
        os.environ['USB_RECOVER_SYSFS'] = str(root / 'sysroot')

    def old_helper(self):
        """usb_recover.sh as it was before its pci refusals exited 3 (die, exit 1): the copy a rig
        keeps until hil-farm-setup refreshes it."""
        text = USB_RECOVER.read_text()
        head, sep, rest = text.partition('require_usb_controller() {')
        pci, sep2, tail = rest.partition('  root-cycle)')
        old = head + sep + pci.replace('refuse "', 'die "') + sep2 + tail
        self.assertNotEqual(old, text)
        path = self.bin / 'usb_recover_old.sh'
        path.write_text(old)
        path.chmod(0o755)
        return str(path)

    def test_an_old_helpers_refusal_fails_closed_naming_the_helper(self):
        if os.geteuid() == 0:
            self.skipTest('the fake-tree seams are ignored under root')
        hr.RECOVER_SCRIPT = self.old_helper()

        def patch(m):
            real_run, link = m.Reservation.run, self.pci / self.ADDR / 'driver'

            def run(res, argv, bound=None):
                os.rename(link, f'{link}.off')           # the helper finds no driver: refused before the unbind
                try:
                    return real_run(res, argv, bound)
                finally:
                    os.rename(f'{link}.off', link)
            m.Reservation.run = run
        p = self.run_ctl(patch=patch)
        out = p.output()
        line = next(x for x in out.splitlines() if 'BLOCKER' in x)
        self.assertIn(f'no driver bound to {self.ADDR}', out)
        self.assertIn('command exited 1', line)
        self.assertIn(f'the controller is unchanged: if {hr.RECOVER_SCRIPT} predates exit 3 for pci refusals', line)
        self.assertIn('hil-farm-setup', line)
        self.assert_blocked_on_every_lock(p, line)
        for drv in ('xhci-pci-renesas', 'xhci_hcd'):
            self.assertEqual((self.drivers / drv / 'unbind').read_text(), '')

    def test_a_pci_bind_names_the_old_helper_only_when_it_had_no_bind_file_to_write(self):
        if os.geteuid() == 0:
            self.skipTest('the fake-tree seams are ignored under root')
        settle = lambda m: setattr(m, 'CONTROLLER_SETTLE', 0.3)   # noqa: E731
        for drv in ('xhci-pci-renesas', 'xhci_hcd'):
            (self.drivers / drv / 'bind').unlink()
        p = self.run_ctl(verb='pci-bind', addr=self.IDLE, patch=settle)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn(f'no known xHCI driver is loaded to bind {self.IDLE}', out)
        self.assertIn('pci-bind refused before writing anything', out)
        self.assertEqual(self.held(), set())
        hr.RECOVER_SCRIPT = self.old_helper()
        p = self.run_ctl(verb='pci-bind', addr=self.IDLE, patch=settle)
        line = p.blocker()
        self.assertIn('command exited 1', line)
        self.assertIn(f'the controller is unchanged: if {hr.RECOVER_SCRIPT} predates exit 3', line)
        self.assert_blocked_on_every_lock(p, line)
        hr.RECOVER_SCRIPT = str(USB_RECOVER)
        (self.drivers / 'xhci_hcd' / 'bind').mkdir()            # there, but its write fails: the kernel's rejection
        os.environ['PCI_DEV'] = str(self.pci / self.IDLE)
        p = self.fork_main('run', 'pci-bind', self.IDLE, *self.rosters(), '--reason', 'test', '--budget', '1',
                           '--driver', 'xhci_hcd', patch=settle)
        line = p.blocker()
        self.assertIn('command exited 1', line)
        self.assertNotIn('predates exit 3', line)
        self.assert_blocked_on_every_lock(p, line)

    def test_the_real_script_rebinds_under_the_reservation(self):
        if os.geteuid() == 0:
            self.skipTest('the fake-tree seams are ignored under root')
        p = self.run_ctl()
        out = p.output()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn(f'rebound pci {self.ADDR} (xhci-pci-renesas)', out)
        for f in ('unbind', 'bind'):
            self.assertEqual((self.drivers / 'xhci-pci-renesas' / f).read_text(), f'{self.ADDR}\n')
        self.assertEqual(self.held(), set())

        def patch(m):
            real_run, link = m.Reservation.run, self.pci / self.ADDR / 'driver'

            def run(res, argv, bound=None):
                os.rename(link, f'{link}.off')           # the script finds no driver: refused before the unbind
                try:
                    return real_run(res, argv, bound)
                finally:
                    os.rename(f'{link}.off', link)
            m.Reservation.run = run
        p = self.run_ctl(patch=patch)
        out = p.output()
        self.assertEqual(p.returncode, 1, out)
        self.assertIn(f'no driver bound to {self.ADDR}', out)
        self.assertIn('pci-rebind refused before writing anything', out)
        self.assertEqual(self.held(), set())


class RecoverScriptChoice(unittest.TestCase):
    """A rig's installed usb_recover wins, even a broken entry (sudo then fails): falling back to a
    checkout a CI user can write would run that as root."""

    def choice(self, error):
        real = os.lstat

        def lstat(p, *a, **k):
            if p == '/usr/local/sbin/usb_recover':
                if error:
                    raise error
                return real('/')
            return real(p, *a, **k)
        sp = importlib.util.spec_from_file_location('hil_reserve_choice', SCRIPT)
        mod = importlib.util.module_from_spec(sp)
        with mock.patch.object(hr.hil_root.os, 'lstat', lstat):
            sp.loader.exec_module(mod)
        return mod.RECOVER_SCRIPT

    def test_installed_unless_nothing_is_there(self):
        for error in (None, PermissionError(13, 'denied'), NotADirectoryError(20, 'not a dir')):
            self.assertEqual(self.choice(error), '/usr/local/sbin/usb_recover', error)
        self.assertTrue(self.choice(FileNotFoundError(2, 'gone')).endswith('skills/usb-kernel-recover/scripts/usb_recover.sh'))


if __name__ == '__main__':
    unittest.main()
