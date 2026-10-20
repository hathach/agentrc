"""usb_recover.sh shield/unshield/shield-status against a fake sysfs tree: modes are
recorded before any chmod and restored from the record only onto the same inodes; an
overlapping shield is refused; a failed acquisition rolls back and keeps no record; a live
foreign owner blocks unshield while a dead one does not. Never run as root: the seams
(USB_RECOVER_SYSFS/USB_RECOVER_STATE) are ignored under root and the partial-acquisition
case chmods a root-owned procfs file expecting EPERM."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('usb_recover.sh drives Linux sysfs')

import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'usb-kernel-recover' / 'scripts' / 'usb_recover.sh'
ATTRS = ['bNumInterfaces', 'bmAttributes', 'bMaxPower', 'configuration', 'bConfigurationValue',
         'product', 'manufacturer', 'serial', 'avoid_reset_quirk']
MODE = {'bConfigurationValue': 0o644, 'avoid_reset_quirk': 0o644}   # the rest are 0o444, as on ci.lan


def mode(path):
    return os.stat(path).st_mode & 0o777


class Tree(unittest.TestCase):
    """A fake sysfs tree with a root hub, a hub and two leaves, and a scratch state dir."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.sys = Path(self.td.name) / 'sys'
        self.state = Path(self.td.name) / 'state'
        self.dev = self.sys / 'bus/usb/devices'
        self.dev.mkdir(parents=True)
        self.add('usb13')
        self.add('13-1', skip=('manufacturer', 'serial'))   # hubs carry no strings, like the rig's
        self.add('13-1.6')
        self.add('13-1.7')
        self.owner = subprocess.Popen(['sleep', '300'])
        self.addCleanup(self.owner.wait)
        self.addCleanup(self.owner.kill)

    def add(self, name, skip=()):
        d = self.dev / name
        d.mkdir()
        for a in ATTRS:
            if a in skip:
                continue
            (d / a).write_text(f'{name} {a}\n')
            os.chmod(d / a, MODE.get(a, 0o444))

    def run_script(self, *args, ok=True, env=None, code=None):
        env = {**os.environ, 'USB_RECOVER_SYSFS': str(self.sys), 'USB_RECOVER_STATE': str(self.state), **(env or {})}
        r = subprocess.run(['bash', str(SCRIPT), *args], env=env, capture_output=True, text=True)
        if code is not None:
            self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        elif ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def modes(self, *names):
        return {f'{n}/{a}': mode(self.dev / n / a) for n in names for a in ATTRS if (self.dev / n / a).exists()}


@unittest.skipIf(os.geteuid() == 0, 'the fake-tree seams are ignored under root')
class Shield(Tree):
    def test_shield_records_then_zeroes_leaf_parent_and_root_and_unshield_restores(self):
        before = self.modes('usb13', '13-1', '13-1.6', '13-1.7')
        out = self.run_script('shield', '13-1.6', str(self.owner.pid))
        self.assertIn('shielded 13-1.6: 25 attribute(s) on 13-1.6,13-1,usb13', out)
        rec = (self.state / '13-1.6').read_text()
        self.assertIn(f'owner {self.owner.pid} ', rec)
        self.assertIn('attr 13-1/bConfigurationValue ', rec)
        self.assertIn('absent 13-1/serial', rec)
        after = self.modes('usb13', '13-1', '13-1.6', '13-1.7')
        for k, m in after.items():
            self.assertEqual(m, before[k] if k.startswith('13-1.7/') else 0, k)   # the sibling is untouched
        # a recorded 644 comes back as 644, which copying from a 444 sibling attribute never could
        out = self.run_script('unshield', '13-1.6', str(self.owner.pid))
        self.assertIn('restored 25 attribute(s), 0 gone', out)
        self.assertEqual(self.modes('usb13', '13-1', '13-1.6', '13-1.7'), before)
        self.assertFalse((self.state / '13-1.6').exists())

    def test_a_shield_on_a_root_port_device_covers_leaf_and_root_only(self):
        out = self.run_script('shield', '13-1', str(self.owner.pid))
        self.assertIn('on 13-1,usb13 ', out)
        self.assertEqual(mode(self.dev / '13-1.6/serial'), 0o444)

    def test_overlapping_shields_are_refused_and_touch_nothing(self):
        self.run_script('shield', '13-1.6', str(self.owner.pid))
        before = self.modes('13-1.7')
        out = self.run_script('shield', '13-1.7', str(self.owner.pid), ok=False)
        self.assertIn('already covered by the shield on 13-1.6', out)
        self.assertEqual(self.modes('13-1.7'), before)
        self.assertFalse((self.state / '13-1.7').exists())

    def test_an_attribute_that_is_not_a_regular_file_stops_the_shield_before_any_change(self):
        (self.dev / 'usb13/product').unlink()
        os.symlink('/proc/version', self.dev / 'usb13/product')
        before = self.modes('13-1', '13-1.6')
        out = self.run_script('shield', '13-1.6', str(self.owner.pid), ok=False)
        self.assertIn('cannot inspect usb13/product (not a regular file); nothing changed', out)
        self.assertEqual(self.modes('13-1', '13-1.6'), before)
        self.assertEqual([p.name for p in self.state.glob('*')], ['.lock'])

    def test_a_failed_acquisition_rolls_back_and_keeps_no_record(self):
        # the root hub is a REAL one: its attributes stat fine but refuse chmod to a
        # non-root caller (EPERM), after the leaf and parent were already zeroed
        real = Path('/sys/bus/usb/devices/usb1')
        if not (real / 'serial').exists():
            self.skipTest('no real root hub to borrow')
        shutil.rmtree(self.dev / 'usb13')
        os.symlink(real, self.dev / 'usb13')
        real_modes = {a: mode(real / a) for a in ATTRS if (real / a).exists()}
        before = self.modes('13-1', '13-1.6')
        out = self.run_script('shield', '13-1.6', str(self.owner.pid), ok=False)
        self.assertIn('chmod 000 usb13/', out)
        self.assertIn('rolled back 16 attribute(s), no record kept', out)
        self.assertEqual(self.modes('13-1', '13-1.6'), before)
        self.assertEqual({a: mode(real / a) for a in real_modes}, real_modes)
        self.assertEqual([p.name for p in self.state.glob('*')], ['.lock'])

    def test_concurrent_shields_on_a_shared_hub_admit_exactly_one(self):
        env = dict(os.environ, USB_RECOVER_SYSFS=str(self.sys), USB_RECOVER_STATE=str(self.state))
        procs = [subprocess.Popen(['bash', str(SCRIPT), 'shield', bp, str(self.owner.pid)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                 for bp in ('13-1.6', '13-1.7')]
        outs = [(p.wait(), p.communicate()[0]) for p in procs]
        codes = sorted(rc for rc, _ in outs)
        self.assertEqual(codes, [0, 1], outs)
        loser = next(o for rc, o in outs if rc)
        self.assertIn('already covered by the shield on', loser)
        winner, = [bp for bp in ('13-1.6', '13-1.7') if (self.state / bp).exists()]
        other = '13-1.7' if winner == '13-1.6' else '13-1.6'
        self.assertEqual(mode(self.dev / other / 'serial'), 0o444)
        self.assertEqual(mode(self.dev / 'usb13/serial'), 0)

    def test_an_attribute_that_cannot_be_inspected_keeps_the_record(self):
        self.run_script('shield', '13-1.6', str(self.owner.pid))
        os.chmod(self.dev / 'usb13', 0)           # EACCES on every root-hub attribute, not ENOENT
        self.addCleanup(os.chmod, self.dev / 'usb13', 0o755)
        out = self.run_script('unshield', '13-1.6', str(self.owner.pid), ok=False)
        self.assertIn('could not restore usb13/', out)
        self.assertIn('record', out)
        self.assertIn('kept (restored 16, gone 0)', out)
        self.assertTrue((self.state / '13-1.6').exists())
        self.assertIn('9 uninspectable', self.run_script('shield-status'))
        os.chmod(self.dev / 'usb13', 0o755)
        out = self.run_script('unshield', '13-1.6', str(self.owner.pid))
        self.assertIn('restored 25 attribute(s)', out)     # the 16 again (same mode) plus the 9
        self.assertEqual(mode(self.dev / 'usb13/bConfigurationValue'), 0o644)

    def test_unshield_skips_objects_that_re_enumerated_or_vanished(self):
        self.run_script('shield', '13-1.6', str(self.owner.pid))
        # the leaf re-enumerated: same path, new inodes, kernel-fresh modes. The old objects are
        # moved aside, not deleted: a filesystem that reuses freed inode numbers (CI's does) would
        # otherwise hand the new leaf the old inodes and the script would rightly restore them.
        gone = Path(self.td.name) / 'gone'
        gone.mkdir()
        (self.dev / '13-1.6').rename(gone / '13-1.6')
        self.add('13-1.6')
        # the parent hub vanished entirely
        (self.dev / '13-1').rename(gone / '13-1')
        out = self.run_script('unshield', '13-1.6', str(self.owner.pid))
        self.assertIn('restored 9 attribute(s), 16 gone', out)
        self.assertEqual(mode(self.dev / 'usb13/bConfigurationValue'), 0o644)
        self.assertEqual(mode(self.dev / '13-1.6/serial'), 0o444)      # the new object's own mode, untouched
        self.assertFalse((self.state / '13-1.6').exists())

    def test_a_live_foreign_owner_blocks_unshield_and_a_dead_one_does_not(self):
        self.run_script('shield', '13-1.6', str(self.owner.pid))
        out = self.run_script('unshield', '13-1.6', str(os.getpid()), ok=False)
        self.assertIn(f'shielded by live pid {self.owner.pid}', out)
        out = self.run_script('unshield', '13-1.6', ok=False)
        self.assertIn('live pid', out)
        self.assertEqual(mode(self.dev / 'usb13/serial'), 0)
        self.assertIn(f'owner pid {self.owner.pid} alive; 25 attribute(s) still shielded', self.run_script('shield-status'))
        self.owner.send_signal(signal.SIGKILL)
        self.owner.wait()
        self.assertIn(f'owner pid {self.owner.pid} dead', self.run_script('shield-status', '13-1.6'))
        out = self.run_script('unshield', '13-1.6')       # stale record: no pid needed
        self.assertIn('restored 25', out)
        self.assertIn('no shields recorded', self.run_script('shield-status'))

    def test_a_reused_pid_is_not_the_owner(self):
        self.run_script('shield', '13-1.6', str(self.owner.pid))
        rec = self.state / '13-1.6'
        lines = rec.read_text().splitlines()
        pid, start, boot = lines[0].split()[1:]
        lines[0] = f'owner {pid} {int(start) - 1} {boot}'     # same pid, different start time
        rec.write_text('\n'.join(lines) + '\n')
        self.assertIn('dead', self.run_script('shield-status', '13-1.6'))

    def test_a_failed_rollback_keeps_the_record_for_a_later_unshield(self):
        real = Path('/sys/bus/usb/devices/usb1')
        if not (real / 'serial').exists():
            self.skipTest('no real root hub to borrow')
        shutil.rmtree(self.dev / 'usb13')
        os.symlink(real, self.dev / 'usb13')            # acquisition fails at the root hub (EPERM)...
        env = {'USB_RECOVER_TEST_FAIL_CHMOD': str(self.dev / '13-1/bMaxPower')}   # ...and one rollback chmod fails
        out = self.run_script('shield', '13-1.6', str(self.owner.pid), ok=False, env=env)
        self.assertIn('rollback incomplete: 13-1/bMaxPower (chmod failed: injected)', out)
        self.assertIn('KEPT for unshield', out)
        self.assertTrue((self.state / '13-1.6').exists())
        self.assertEqual(mode(self.dev / '13-1/bMaxPower'), 0)
        self.assertIn('1 attribute(s) still shielded, 24 recorded but not shielded', self.run_script('shield-status'))
        out = self.run_script('unshield', '13-1.6', str(self.owner.pid))
        self.assertIn('restored 25 attribute(s), 0 gone', out)
        self.assertEqual(mode(self.dev / '13-1/bMaxPower'), 0o444)
        self.assertFalse((self.state / '13-1.6').exists())

    def counted(self, *args, ok=True):
        """run_script with python3 behind a counter: (output, interpreter starts)."""
        bindir = Path(self.td.name) / 'bin'
        log = Path(self.td.name) / 'py.log'
        if not bindir.exists():
            bindir.mkdir()
            real = shutil.which('python3')
            (bindir / 'python3').write_text(f'#!/bin/sh\necho x >> {log}\nexec {real} "$@"\n')
            os.chmod(bindir / 'python3', 0o755)
        log.write_text('')
        out = self.run_script(*args, ok=ok, env={'PATH': f'{bindir}:{os.environ["PATH"]}'})
        return out, len(log.read_text().split())

    def test_each_verb_starts_one_interpreter_for_every_attribute(self):
        """One python3 per attribute was 52 starts to shield one leaf and 25 to unshield it."""
        self.assertEqual(self.counted('shield', '13-1.6', str(self.owner.pid))[1], 1)
        self.assertEqual(self.counted('shield', '13-1.7', str(self.owner.pid), ok=False)[1], 0)   # overlap: refused in bash
        saved = Path(self.td.name) / 'saved'
        (self.state / '13-1.6').rename(saved)           # out of the way of the overlap check
        self.counted('shield', '13-1.7', str(self.owner.pid))
        saved.rename(self.state / '13-1.6')
        out, n = self.counted('shield-status')
        self.assertEqual(n, 1, 'two records, one interpreter')
        self.assertIn('13-1.6: owner pid', out)
        self.assertIn('13-1.7: owner pid', out)
        self.assertEqual(self.counted('unshield', '13-1.6', str(self.owner.pid))[1], 1)

    def test_a_failed_shield_rolls_back_in_the_same_interpreter(self):
        real = Path('/sys/bus/usb/devices/usb1')
        if not (real / 'serial').exists():
            self.skipTest('no real root hub to borrow')
        shutil.rmtree(self.dev / 'usb13')
        os.symlink(real, self.dev / 'usb13')            # acquisition fails at the root hub (EPERM)
        out, n = self.counted('shield', '13-1.6', str(self.owner.pid), ok=False)
        self.assertIn('rolled back', out)
        self.assertEqual(n, 1)

    def test_the_other_actions_still_dispatch(self):
        out = self.run_script('resolve', '/dev/null', ok=False)
        self.assertIn('could not find parent USB device', out)
        self.assertNotIn('command not found', out)

    def test_arguments_are_checked(self):
        self.assertIn('bad usb path', self.run_script('shield', '../etc', str(self.owner.pid), ok=False))
        self.assertIn('is not a pid', self.run_script('shield', '13-1.6', ok=False))
        self.assertIn('not a running process', self.run_script('shield', '13-1.6', '4194300', ok=False))
        self.assertIn('no such usb object', self.run_script('shield', '13-2', str(self.owner.pid), ok=False))
        self.assertIn('no shield record', self.run_script('unshield', '13-1.6', ok=False))

    def test_usage_lists_the_full_path_and_every_verb_but_no_record_format(self):
        out = self.run_script(ok=False)
        self.assertIn('sudo <skill dir>/scripts/usb_recover.sh <verb>', out)
        for verb in ('authorized', 'root-cycle', 'pci-rebind', 'pci-bind', 'resolve', 'shield ',
                     'unshield', 'shield-status'):
            self.assertIn(f'#   {verb}', out)
        self.assertNotIn('owner <pid>', out)



SLEEP = f'#!/bin/sh\nexec {shutil.which("sleep")} 0.05\n'   # resolved before the stub shadows it on PATH
UDEVADM = '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$UDEVADM_ARGV"\n[ -n "$UDEVADM_PATH" ] || exit 1\necho "$UDEVADM_PATH"\n'


class VerbTree(Tree):
    """The fake tree with udevadm and sleep stubs on PATH."""

    def setUp(self):
        super().setUp()
        self.stubs = Path(self.td.name) / 'bin'
        self.stubs.mkdir()
        self.argv = Path(self.td.name) / 'argv'
        # the script's settle sleeps (authorized, pci-rebind, the root-cycle poll) shrink 20x
        for name, text in (('udevadm', UDEVADM), ('sleep', SLEEP)):
            (self.stubs / name).write_text(text)
            (self.stubs / name).chmod(0o755)
        # sysfs busports are symlinks: re-enumeration replaces the target directory behind the same link
        self.targets = Path(self.td.name) / 'targets'
        self.targets.mkdir()
        (self.dev / '13-1.6').rename(self.targets / '13-1.6.gen1')
        (self.dev / '13-1.6').symlink_to(self.targets / '13-1.6.gen1')
        self.stub_env = {'PATH': f'{self.stubs}:{os.environ["PATH"]}', 'UDEVADM_ARGV': str(self.argv),
                         'TARGET': '13-1.6'}
        for d in ('13-1.6', '13-1.7'):
            os.chmod(self.dev / d / 'serial', 0o644)
            (self.dev / d / 'authorized').write_text('1\n')
            (self.dev / d / 'idVendor').write_text('2e8a\n')
            (self.dev / d / 'idProduct').write_text('000c\n')
            (self.dev / d / 'devnum').write_text('7\n')
        (self.dev / '13-1.6' / 'serial').write_text('PROBE-SN\n')
        pci = self.sys / 'bus/pci'
        for addr, cls, drv in (('0000:05:00.0', '0x0c0330', 'xhci_hcd'), ('0000:06:00.0', '0x0c0330', None),
                               ('0000:07:00.0', '0x020000', 'e1000e')):
            d = pci / 'devices' / addr
            d.mkdir(parents=True)
            (d / 'class').write_text(cls + '\n')
            if drv:
                (pci / 'drivers' / drv).mkdir(parents=True, exist_ok=True)
                (d / 'driver').symlink_to(pci / 'drivers' / drv)
        for drv in ('xhci_hcd', 'xhci-pci-renesas'):
            (pci / 'drivers' / drv).mkdir(parents=True, exist_ok=True)
            for f in ('bind', 'unbind'):
                (pci / 'drivers' / drv / f).write_text('')

    def verb(self, *args, ok=True, code=None, **env):
        return self.run_script(*args, ok=ok, env={**self.stub_env, **env}, code=code)

    def fifo(self, path):
        """Replace the attribute with a FIFO held open here, so every write to it is kept in order."""
        path.unlink()
        os.mkfifo(path)
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        return fd

    def writes(self, fd):
        try:
            return os.read(fd, 4096).decode()
        except BlockingIOError:
            return ''


@unittest.skipIf(os.geteuid() == 0, 'the fake-tree seams are ignored under root')
class Verbs(VerbTree):
    """authorized, pci-rebind, pci-bind and resolve against the fake tree, with a udevadm stub
    on PATH: argument checks and the exact calls."""

    def test_authorized_writes_zero_then_one(self):
        auth = self.fifo(self.dev / '13-1.6' / 'authorized')
        out = self.verb('authorized', '13-1.6')
        self.assertIn('re-authorized 13-1.6', out)
        self.assertEqual(self.writes(auth), '0\n1\n')
        self.assertIn('bad usb path', self.verb('authorized', '../x', ok=False))
        self.assertIn('no such usb device: 13-9', self.verb('authorized', '13-9', ok=False))
        (self.dev / '13-1.6' / 'authorized').unlink()
        self.assertIn('no such usb device: 13-1.6', self.verb('authorized', '13-1.6', ok=False))

    def test_pci_rebind_unbinds_then_binds_the_usb_controller_only(self):
        unbind = self.fifo(self.sys / 'bus/pci/drivers/xhci_hcd/unbind')
        bind = self.fifo(self.sys / 'bus/pci/drivers/xhci_hcd/bind')
        seen = []
        import threading
        stop = threading.Event()

        def watch():      # which of the two receives its write first
            while not stop.is_set():
                for name, fd in (('unbind', unbind), ('bind', bind)):
                    got = self.writes(fd)
                    if got:
                        seen.append((name, got))
                time.sleep(0.01)
        th = threading.Thread(target=watch, daemon=True)
        th.start()
        out = self.verb('pci-rebind', '0000:05:00.0')
        stop.set()
        th.join()
        for name, fd in (('unbind', unbind), ('bind', bind)):     # whatever landed after the last poll
            got = self.writes(fd)
            if got:
                seen.append((name, got))
        self.assertIn('rebound pci 0000:05:00.0 (xhci_hcd)', out)
        self.assertEqual(seen, [('unbind', '0000:05:00.0\n'), ('bind', '0000:05:00.0\n')])
        self.assertIn('is not a USB controller (class 0x020000)', self.verb('pci-rebind', '0000:07:00.0', ok=False))
        self.assertIn('no driver bound to 0000:06:00.0', self.verb('pci-rebind', '0000:06:00.0', ok=False))
        self.assertIn('bad pci addr', self.verb('pci-rebind', '05:00.0', ok=False))
        self.assertIn('no such pci device: 0000:09:00.0', self.verb('pci-rebind', '0000:09:00.0', ok=False))

    def test_pci_bind_refuses_a_bound_controller_and_tries_the_xhci_drivers_in_order(self):
        self.assertIn('already has a driver bound', self.verb('pci-bind', '0000:05:00.0', ok=False))
        out = self.verb('pci-bind', '0000:06:00.0')
        self.assertIn('bound pci 0000:06:00.0 (xhci-pci-renesas)', out)       # the first candidate that binds
        self.assertEqual((self.sys / 'bus/pci/drivers/xhci-pci-renesas/bind').read_text(), '0000:06:00.0\n')
        renesas = self.sys / 'bus/pci/drivers/xhci-pci-renesas/bind'
        renesas.unlink()
        renesas.mkdir()                                                        # its write fails: fall through
        out = self.verb('pci-bind', '0000:06:00.0')
        self.assertIn('bound pci 0000:06:00.0 (xhci_hcd)', out)
        out = self.verb('pci-bind', '0000:06:00.0', 'xhci_hcd')
        self.assertIn('bound pci 0000:06:00.0 (xhci_hcd)', out)
        self.assertIn('no such pci driver: nope', self.verb('pci-bind', '0000:06:00.0', 'nope', ok=False))
        self.assertIn('bad driver name', self.verb('pci-bind', '0000:06:00.0', '../x', ok=False))
        (self.sys / 'bus/pci/drivers/xhci_hcd/bind').rename(self.sys / 'bus/pci/drivers/xhci_hcd/bind.off')
        self.assertIn('could not bind 0000:06:00.0 with a known xHCI driver',
                      self.verb('pci-bind', '0000:06:00.0', ok=False))

    def test_resolve_walks_up_to_the_usb_device(self):
        for a in ('busnum', 'devnum', 'authorized'):
            (self.dev / '13-1.6' / a).write_text('1\n')
        iface = self.dev / '13-1.6' / '13-1.6:1.0' / 'tty' / 'ttyACM3'
        iface.mkdir(parents=True)
        node = Path(self.td.name) / 'ttyACM3'
        node.write_text('')
        out = self.verb('resolve', str(node), UDEVADM_PATH='/bus/usb/devices/13-1.6/13-1.6:1.0/tty/ttyACM3')
        self.assertEqual(out.strip(), '13-1.6')
        self.assertEqual(self.argv.read_text().split(), ['info', '-q', 'path', '-n', str(node)])
        self.assertIn('could not find parent USB device', self.verb('resolve', str(node), UDEVADM_PATH='/class/tty', ok=False))
        self.assertIn('udevadm failed', self.verb('resolve', str(node), UDEVADM_PATH='', ok=False))
        self.assertIn('no such device node', self.verb('resolve', str(node) + '.missing', ok=False))

    def test_shield_status_distinguishes_no_state_from_state_it_cannot_inspect(self):
        self.assertIn('no shields recorded', self.run_script('shield-status'))        # no dir at all
        self.state.mkdir()
        os.chmod(self.state, 0)
        self.addCleanup(os.chmod, self.state, 0o700)
        out = self.run_script('shield-status', ok=False)
        self.assertIn(f'cannot inspect {self.state} (', out)
        self.assertNotIn('no shields', out)
        os.chmod(self.state, 0o700)
        rec = self.state / '13-1.6'
        rec.write_text('owner 1 1 x\n')
        os.chmod(self.state, 0o400)                                                 # listable, not searchable
        out = self.run_script('shield-status', ok=False)
        self.assertIn('no search permission', out)
        self.assertNotIn('no shields', out)
        os.chmod(self.state, 0o700)
        os.chmod(rec, 0)                                                            # a record it cannot read
        self.addCleanup(os.chmod, rec, 0o600)
        out = self.run_script('shield-status', ok=False)
        self.assertIn(f'cannot inspect {rec} (run with sudo)', out)
        parent = Path(self.td.name) / 'closed'
        parent.mkdir()
        os.chmod(parent, 0)                                                         # an inaccessible ancestor
        self.addCleanup(os.chmod, parent, 0o700)
        out = self.run_script('shield-status', ok=False, env={'USB_RECOVER_STATE': str(parent / 'state')})
        self.assertIn('cannot inspect', out)
        self.assertNotIn('no shields', out)


UHUBCTL_CALLED = '#!/bin/sh\necho "$@" >> "$UHUBCTL_CALLS"\n'   # neither cycle may reach it
# the poll's sleep re-enumerates the target once the port came back on (disable reads 0);
# OFF_HANG stretches the off delay (`sleep 2`) so a test can interrupt it
REENUM_SLEEP = f'''#!/bin/sh
[ -n "$OFF_HANG" ] && [ "$1" = 2 ] && exec {shutil.which("sleep")} 30
if [ -n "$REENUM" ] && [ "$(wc -l < "$WRITES" 2>/dev/null)" = 2 ] && [ ! -e "$REENUM.done" ]; then
  old=$(readlink "$USB_RECOVER_SYSFS/bus/usb/devices/$TARGET"); mv "$old" "$old.gone" && cp -r "$old.gone" "$old"
  touch "$REENUM.done"
fi
exec {shutil.which("sleep")} 0.05
'''
# `sh -c WRITE sh FILE VALUE`: hangs the write of $HANG_VALUE, and logs every write
SH = f'''#!/bin/sh
[ "$1" = -c ] && [ -n "$WRITES" ] && echo "$5" >> "$WRITES"
[ "$1" = -c ] && [ -n "$HANG_VALUE" ] && [ "$5" = "$HANG_VALUE" ] && exec {shutil.which("sleep")} 1000
exec {shutil.which("sh")} "$@"
'''


@unittest.skipIf(os.geteuid() == 0, 'the fake-tree seams are ignored under root')
class HubCycle(VerbTree):
    """hub-cycle writes the hub port's `disable` 1 then 0 for a leaf, never calls uhubctl,
    refuses everything else before any write (exit 3), restores power after a failed or
    interrupted cycle, and succeeds only when the target re-enumerated."""

    def setUp(self):
        super().setUp()
        (self.dev / '13-1' / 'bDeviceClass').write_text('09\n')
        (self.targets / '13-1.6.gen1' / 'bDeviceClass').write_text('00\n')
        port = self.dev / '13-1' / '13-1:1.0' / '13-1-port6'
        port.mkdir(parents=True)
        self.disable = port / 'disable'
        self.disable.write_text('0\n')
        self.write_log = Path(self.td.name) / 'writes'
        self.uhubctl_calls = Path(self.td.name) / 'uhubctl-calls'
        for name, text in (('uhubctl', UHUBCTL_CALLED), ('sleep', REENUM_SLEEP), ('sh', SH)):
            (self.stubs / name).write_text(text)
            (self.stubs / name).chmod(0o755)
        self.stub_env.update(UHUBCTL_CALLS=str(self.uhubctl_calls), DISABLE=str(self.disable),
                             WRITES=str(self.write_log), USB_RECOVER_PORT_WRITE_TIMEOUT='1')

    def cycle(self, *args, ok=True, code=None, **env):
        e = {**os.environ, 'USB_RECOVER_SYSFS': str(self.sys), 'USB_RECOVER_STATE': str(self.state),
             **self.stub_env, **env}
        r = subprocess.run(['bash', str(SCRIPT), 'hub-cycle', *args], env=e, capture_output=True, text=True)
        if code is not None:
            self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        elif ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def written(self):
        return self.write_log.read_text().split() if self.write_log.exists() else []

    def test_a_leaf_is_cycled_off_then_on_and_must_re_enumerate(self):
        out = self.cycle('13-1.6', 'PROBE-SN', REENUM=str(Path(self.td.name) / 'reenum'))
        self.assertIn('hub-cycled 13-1 port 6: 13-1.6 re-enumerated', out)
        self.assertEqual(self.written(), ['1', '0'])
        self.assertEqual(self.disable.read_text().strip(), '0')
        out = self.cycle('13-1.6', code=1)                       # no re-enumeration: a failure
        self.assertIn('did not re-enumerate after cycling 13-1 port 6', out)
        self.assertFalse(self.uhubctl_calls.exists())

    def test_everything_else_is_refused_before_any_write(self):
        cases = [(('13-1',), 'on a root port'),
                 (('13-1.6', 'OTHER-SN'), "expected 'OTHER-SN'"),
                 (('13-1.9',), 'no such usb device')]
        serial = self.targets / '13-1.6.gen1' / 'serial'
        serial.unlink()
        self.assertIn('has no readable serial', self.cycle('13-1.6', code=3))
        serial.write_text('PROBE-SN\n')
        (self.targets / '13-1.6.gen1' / 'bDeviceClass').unlink()
        self.assertIn("cannot read 13-1.6's class", self.cycle('13-1.6', code=3))
        (self.targets / '13-1.6.gen1' / 'bDeviceClass').write_text('00\n')
        for args, says in cases:
            with self.subTest(says=says):
                self.assertIn(says, self.cycle(*args, code=3))
                self.assertEqual(self.written(), [])
        (self.targets / '13-1.6.gen1' / 'bDeviceClass').write_text('09\n')
        self.assertIn('is a hub; only a leaf', self.cycle('13-1.6', code=3))
        (self.targets / '13-1.6.gen1' / 'bDeviceClass').write_text('00\n')
        (self.dev / '13-1' / 'bDeviceClass').write_text('00\n')
        self.assertIn('13-1 is not a hub', self.cycle('13-1.6', code=3))
        (self.dev / '13-1' / 'bDeviceClass').write_text('09\n')
        self.disable.unlink()
        self.assertIn('no port control file', self.cycle('13-1.6', code=3))
        self.assertEqual(self.written(), [])
        self.assertFalse(self.uhubctl_calls.exists())

    def test_a_wedged_leaf_is_left_to_root_cycle(self):
        serial = self.targets / '13-1.6.gen1' / 'serial'
        serial.unlink()
        os.mkfifo(serial)                                         # a read that never answers
        self.assertIn('did not answer (wedged)', self.cycle('13-1.6', code=3))
        self.assertEqual(self.written(), [])
        self.assertFalse(self.uhubctl_calls.exists())

    def test_a_hung_off_write_still_powers_the_port_back_on(self):
        out = self.cycle('13-1.6', code=1, HANG_VALUE='1')
        self.assertIn('failed (off 124, on 0); power-on restored', out)
        self.assertEqual(self.written(), ['1', '0'])

    def test_a_timeout_that_is_not_a_positive_integer_is_never_evaluated(self):
        marker = Path(self.td.name) / 'evaluated'
        out = self.cycle('13-1.6', REENUM=str(Path(self.td.name) / 'reenum'),
                         USB_RECOVER_PORT_WRITE_TIMEOUT=f'PPID[$(touch {marker})]')
        self.assertIn('re-enumerated', out)
        self.assertEqual(self.written(), ['1', '0'])
        self.assertFalse(marker.exists())

    def test_a_failed_on_write_is_reported_not_restored(self):
        out = self.cycle('13-1.6', code=1, HANG_VALUE='0')
        self.assertIn('power-on NOT restored', out)

    def test_a_group_signal_during_the_off_delay_still_powers_the_port_back_on(self):
        """hil_reserve forwards SIGTERM to the command's process group, sleep included."""
        e = {**os.environ, 'USB_RECOVER_SYSFS': str(self.sys), 'USB_RECOVER_STATE': str(self.state),
             **self.stub_env, 'OFF_HANG': '1'}
        p = subprocess.Popen(['bash', str(SCRIPT), 'hub-cycle', '13-1.6'], env=e, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        for _ in range(500):
            if self.written() == ['1']:
                break
            time.sleep(0.01)
        time.sleep(0.2)                                           # into the off delay
        os.killpg(p.pid, signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('interrupted); power-on restored', out)
        self.assertEqual(self.written(), ['1', '0'])
        self.assertEqual(self.disable.read_text().strip(), '0')

    def test_an_interrupted_cycle_powers_the_port_back_on_and_fails(self):
        e = {**os.environ, 'USB_RECOVER_SYSFS': str(self.sys), 'USB_RECOVER_STATE': str(self.state),
             **self.stub_env, 'HANG_VALUE': '1'}
        p = subprocess.Popen(['bash', str(SCRIPT), 'hub-cycle', '13-1.6'], env=e, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        for _ in range(200):
            if self.written():
                break
            time.sleep(0.01)
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 1, out)
        self.assertIn('interrupted); power-on restored', out)
        self.assertEqual(self.written(), ['1', '0'])


# root_port_cycle.py: logs each run; RPC_<VERB> = ok (default) | fail | refuse | hang (killed at the
# bound) | survive (ignores USR2, the tests' KILL_SIG, so it outlives the "SIGKILL") | slow (0.5 s,
# then ok); hang and slow touch $RPC_CALLS.hang on entry; a clean `on` re-enumerates the target
RPC = f'''#!/bin/sh
echo "$*" >> "$RPC_CALLS"
eval "mode=\\${{RPC_$1:-ok}}"
case "$mode" in
  fail) exit 1 ;;
  refuse) echo "root_port_cycle: $1 refused: stub" >&2; exit 3 ;;
  hang) touch "$RPC_CALLS.hang"; exec {shutil.which("sleep")} 30 ;;
  survive) echo $$ > "$RPC_CALLS.pid"; trap '' USR2; exec {shutil.which("sleep")} 30 >/dev/null 2>&1 ;;
  slow) touch "$RPC_CALLS.hang"; {shutil.which("sleep")} 0.5 ;;
esac
[ "$1" = check ] && echo 189:1536:4242
if [ "$1" = on ] && [ -n "$REENUM" ]; then
  old=$(readlink "$USB_RECOVER_SYSFS/bus/usb/devices/$TARGET"); mv "$old" "$old.gone" && cp -r "$old.gone" "$old"
fi
exit 0
'''
# OFF_HANG stretches the off delay (`sleep 5`) so a test can interrupt it
ROOT_SLEEP = f'''#!/bin/sh
[ -n "$OFF_HANG" ] && [ "$1" = 5 ] && touch "$RPC_CALLS.hang" && exec {shutil.which("sleep")} 30
exec {shutil.which("sleep")} 0.05
'''


@unittest.skipIf(os.geteuid() == 0, 'the fake-tree seams are ignored under root')
class RootCycle(VerbTree):
    """root-cycle runs root_port_cycle.py check, off, on (each supervised), never uhubctl or a
    `disable` write; refuses before the power-off with exit 3 and fails with 1 from it on; restores
    power after a failed or interrupted off unless a run survived; and succeeds only when the
    target re-enumerated."""

    def setUp(self):
        super().setUp()
        self.calls = Path(self.td.name) / 'rpc-calls'
        self.uhubctl_calls = Path(self.td.name) / 'uhubctl-calls'
        for name, text in (('root_port_cycle.py', RPC), ('uhubctl', UHUBCTL_CALLED), ('sleep', ROOT_SLEEP)):
            (self.stubs / name).write_text(text)
            (self.stubs / name).chmod(0o755)
        self.stub_env.update(USB_RECOVER_ROOT_PORT_CYCLE=str(self.stubs / 'root_port_cycle.py'),
                             RPC_CALLS=str(self.calls), UHUBCTL_CALLS=str(self.uhubctl_calls),
                             USB_RECOVER_PORT_WRITE_TIMEOUT='1', USB_RECOVER_TEST_KILL_SIG='USR2')

    def cycle(self, *args, code, **env):
        return self.verb('root-cycle', *args, code=code, **env)

    def ran(self):
        return [line.split()[0] for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def start(self, **env):
        e = {**os.environ, 'USB_RECOVER_SYSFS': str(self.sys), 'USB_RECOVER_STATE': str(self.state),
             **self.stub_env, **env}
        p = subprocess.Popen(['bash', str(SCRIPT), 'root-cycle', '13-1.6'], env=e, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and os.killpg(p.pid, signal.SIGKILL))
        return p

    def signal_on_hang(self, p, group=True):
        """SIGTERM once a stub entered its hang: to the process group, or to the shell only."""
        hang = Path(str(self.calls) + '.hang')
        for _ in range(500):
            if hang.exists():
                break
            time.sleep(0.01)
        hang.unlink()
        if group:
            os.killpg(p.pid, signal.SIGTERM)
        else:
            p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        return p.returncode, out

    def reap_survivor(self):
        pid = int(Path(str(self.calls) + '.pid').read_text())
        self.addCleanup(lambda: os.path.exists(f'/proc/{pid}') and os.kill(pid, signal.SIGKILL))
        return pid

    def test_check_off_on_go_to_the_root_port_and_success_needs_a_new_inode(self):
        out = self.cycle('13-1.6', 'PROBE-SN', code=0, REENUM='1')
        self.assertIn('root-cycled 13 port 1: 13-1.6 re-enumerated (devnum 7', out)
        self.assertIn('target 13-1.6 is 2e8a:000c serial=PROBE-SN', out)
        self.assertEqual(self.calls.read_text().splitlines(),
                         ['check 13 1', 'off 13 1 189:1536:4242', 'on 13 1 189:1536:4242'])
        self.assertFalse(self.uhubctl_calls.exists())
        self.calls.unlink()
        out = self.cycle('13-1.6', code=1)                         # nothing re-enumerated: same inode
        self.assertIn('did not re-enumerate after cycling bus 13 port 1', out)
        self.assertEqual(self.ran(), ['check', 'off', 'on'])

    def test_refusals_before_the_power_off_are_exit_3(self):
        self.assertIn("has serial 'PROBE-SN', expected 'OTHER-SN'", self.cycle('13-1.6', 'OTHER-SN', code=3))
        self.assertIn('no such usb device: 13-9', self.cycle('13-9', code=3))
        self.assertIn('bad usb path', self.cycle('../x', code=3))
        self.assertEqual(self.ran(), [])
        out = self.cycle('13-1.6', code=3, RPC_check='refuse')
        self.assertIn('check refused: stub', out)
        self.assertIn('root-cycle: bus 13 port 1 refused (above); nothing switched', out)
        self.assertEqual(self.ran(), ['check'])
        self.assertFalse(self.uhubctl_calls.exists())

    def test_a_missing_serial_is_a_mismatch_not_a_timeout(self):
        (self.dev / '13-1.6' / 'serial').unlink()
        self.assertIn("has serial '', expected 'PROBE-SN'", self.cycle('13-1.6', 'PROBE-SN', code=3))
        self.assertEqual(self.ran(), [])

    def test_a_check_that_fails_hangs_or_survives_ends_before_the_power_off(self):
        for mode, says in (('fail', 'check of bus 13 port 1 failed (1); nothing switched'),
                           ('hang', 'check of bus 13 port 1 failed (124); nothing switched'),
                           ('survive', 'check of bus 13 port 1 failed (125): pid ')):
            with self.subTest(mode=mode):
                self.calls.unlink(missing_ok=True)
                out = self.cycle('13-1.6', code=1, RPC_check=mode)
                self.assertIn(says, out)
                self.assertEqual(self.ran(), ['check'])
                if mode == 'survive':
                    self.assertIn(f'pid {self.reap_survivor()} survived; nothing switched, no further request', out)

    def test_a_failed_or_hung_off_still_powers_the_port_back_on_and_fails(self):
        for mode, code in (('fail', 1), ('hang', 124), ('refuse', 3)):
            with self.subTest(mode=mode):
                self.calls.unlink(missing_ok=True)
                out = self.cycle('13-1.6', code=1, RPC_off=mode, REENUM='1')
                self.assertIn(f'failed (off {code}, on 0); power-on restored', out)
                self.assertEqual(self.ran(), ['check', 'off', 'on'])

    def test_a_failed_or_refused_on_is_reported_not_restored_and_never_exit_3(self):
        for mode, code in (('fail', 1), ('refuse', 3)):
            with self.subTest(mode=mode):
                out = self.cycle('13-1.6', code=1, RPC_on=mode)
                self.assertIn(f'failed (off 0, on {code}); power-on NOT restored ({code})', out)

    def test_a_surviving_off_sends_no_on(self):
        out = self.cycle('13-1.6', code=1, RPC_off='survive')
        pid = self.reap_survivor()
        self.assertIn(f'failed (off 125, on 0); power-on not attempted: pid {pid} survived', out)
        self.assertEqual(self.ran(), ['check', 'off'])

    def test_a_surviving_on_is_reported(self):
        out = self.cycle('13-1.6', code=1, RPC_on='survive')
        pid = self.reap_survivor()
        self.assertIn(f'(off 0, on 125); power-on NOT restored (125): pid {pid} survived', out)

    def test_a_group_signal_during_the_check_aborts_without_the_power_off(self):
        rc, out = self.signal_on_hang(self.start(RPC_check='hang'))
        self.assertEqual(rc, 1, out)
        self.assertIn('interrupted before the power-off; nothing switched', out)
        self.assertEqual(self.ran(), ['check'])

    def test_a_signal_to_the_shell_during_a_check_that_succeeds_aborts_without_the_power_off(self):
        """The check completes and prints its token; the checks before the off still stop it."""
        p = self.start(RPC_check='slow', USB_RECOVER_PORT_WRITE_TIMEOUT='10')   # well inside the bound
        rc, out = self.signal_on_hang(p, group=False)
        self.assertNotIn('failed (', out)
        self.assertEqual(rc, 1, out)
        self.assertIn('interrupted before the power-off; nothing switched', out)
        self.assertEqual(self.ran(), ['check'])

    def test_a_signal_after_the_check_passed_but_before_the_off_still_switches_nothing(self):
        """An exported `echo` overrides the builtin and signals the shell from its `cycling` line,
        which runs after the check's verdict and before the off request."""
        echo = '() { [[ "$*" == "root-cycle: cycling "* ]] && kill -TERM $$; builtin echo "$@"; }'
        out = self.cycle('13-1.6', code=1, **{'BASH_FUNC_echo%%': echo})
        self.assertIn('interrupted before the power-off; nothing switched', out)
        self.assertNotIn('failed (', out)
        self.assertEqual(self.ran(), ['check'])

    def test_a_group_signal_during_the_off_or_its_delay_still_powers_the_port_back_on_once(self):
        for env, says in (({'RPC_off': 'hang'}, 'failed (off 143, on 0, interrupted)'),
                          ({'OFF_HANG': '1'}, 'failed (off 0, on 0, interrupted)')):
            with self.subTest(env=env):
                self.calls.unlink(missing_ok=True)
                rc, out = self.signal_on_hang(self.start(**env))
                self.assertEqual(rc, 1, out)
                self.assertIn(f'{says}; power-on restored', out)
                self.assertEqual(self.ran(), ['check', 'off', 'on'])


if __name__ == '__main__':
    unittest.main()
