"""usb_recover.sh shield/unshield/shield-status against a fake sysfs tree: modes are
recorded before any chmod and restored from the record only onto the same inodes; an
overlapping shield is refused; a failed acquisition rolls back and keeps no record; a live
foreign owner blocks unshield while a dead one does not. Never run as root: the seams
(USB_RECOVER_SYSFS/USB_RECOVER_STATE) are ignored under root and the partial-acquisition
case chmods a root-owned procfs file expecting EPERM."""
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
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

    def run_script(self, *args, ok=True, env=None):
        env = {**os.environ, 'USB_RECOVER_SYSFS': str(self.sys), 'USB_RECOVER_STATE': str(self.state), **(env or {})}
        r = subprocess.run(['bash', str(SCRIPT), *args], env=env, capture_output=True, text=True)
        if ok:
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



UHUBCTL = '''#!/bin/sh
printf '%s\\n' "$@" > "$UHUBCTL_ARGV"
case "$UHUBCTL_MODE" in
  fail) exit 1 ;;
  reenumerate) old=$(readlink "$USB_RECOVER_SYSFS/bus/usb/devices/$TARGET"); mv "$old" "$old.gone" && cp -r "$old.gone" "$old"; exit 0 ;;
  vanish) rm -r "$(readlink "$USB_RECOVER_SYSFS/bus/usb/devices/$TARGET")"; exit 0 ;;
  *) exit 0 ;;
esac
'''
SLEEP = f'#!/bin/sh\nexec {shutil.which("sleep")} 0.05\n'   # resolved before the stub shadows it on PATH
UDEVADM = '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$UDEVADM_ARGV"\n[ -n "$UDEVADM_PATH" ] || exit 1\necho "$UDEVADM_PATH"\n'


@unittest.skipIf(os.geteuid() == 0, 'the fake-tree seams are ignored under root')
class Verbs(Tree):
    """authorized, pci-rebind, pci-bind, root-cycle and resolve against the fake tree, with
    uhubctl and udevadm stubs on PATH: argument checks, the exact calls, and the success
    criterion (root-cycle succeeds only when the target directory's inode changed)."""

    def setUp(self):
        super().setUp()
        self.stubs = Path(self.td.name) / 'bin'
        self.stubs.mkdir()
        self.argv = Path(self.td.name) / 'argv'
        # the script's settle sleeps (authorized, pci-rebind, the root-cycle poll) shrink 20x
        for name, text in (('uhubctl', UHUBCTL), ('udevadm', UDEVADM), ('sleep', SLEEP)):
            (self.stubs / name).write_text(text)
            (self.stubs / name).chmod(0o755)
        # sysfs busports are symlinks: re-enumeration replaces the target directory behind the same link
        self.targets = Path(self.td.name) / 'targets'
        self.targets.mkdir()
        (self.dev / '13-1.6').rename(self.targets / '13-1.6.gen1')
        (self.dev / '13-1.6').symlink_to(self.targets / '13-1.6.gen1')
        self.stub_env = {'PATH': f'{self.stubs}:{os.environ["PATH"]}', 'UHUBCTL_ARGV': str(self.argv),
                         'UDEVADM_ARGV': str(self.argv), 'UHUBCTL_MODE': 'ok', 'TARGET': '13-1.6'}
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

    def verb(self, *args, ok=True, **env):
        return self.run_script(*args, ok=ok, env={**self.stub_env, **env})

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

    def test_root_cycle_refuses_a_serial_mismatch_before_touching_uhubctl(self):
        out = self.verb('root-cycle', '13-1.6', 'OTHER-SN', ok=False)
        self.assertIn("has serial 'PROBE-SN', expected 'OTHER-SN'", out)
        self.assertFalse(self.argv.exists())

    def test_root_cycle_calls_uhubctl_on_the_root_port_and_succeeds_only_on_a_new_inode(self):
        out = self.verb('root-cycle', '13-1.6', 'PROBE-SN', ok=False)       # the stub does nothing: same inode
        self.assertEqual(self.argv.read_text().split(), ['-S', '-l', '13', '-p', '1', '-a', 'cycle', '-d', '5'])
        self.assertIn('did not re-enumerate after cycling bus 13 port 1', out)
        self.assertIn('target 13-1.6 is 2e8a:000c serial=PROBE-SN', out)
        out = self.verb('root-cycle', '13-1.6', UHUBCTL_MODE='reenumerate')  # a new directory: a new inode
        self.assertIn('root-cycled 13 port 1: 13-1.6 re-enumerated (devnum 7', out)
        self.assertIn('uhubctl failed to cycle bus 13 port 1', self.verb('root-cycle', '13-1.6', UHUBCTL_MODE='fail', ok=False))
        self.assertIn('did not re-enumerate', self.verb('root-cycle', '13-1.6', UHUBCTL_MODE='vanish', ok=False))

    def test_root_cycle_without_a_serial_prints_the_identity_and_a_missing_serial_is_not_a_timeout(self):
        (self.dev / '13-1.6' / 'serial').unlink()
        out = self.verb('root-cycle', '13-1.6', 'PROBE-SN', ok=False)
        self.assertIn("has serial '', expected 'PROBE-SN'", out)       # absent reads as '', never as '?'
        self.assertFalse(self.argv.exists())
        self.assertIn('no such usb device: 13-9', self.verb('root-cycle', '13-9', ok=False))

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

if __name__ == '__main__':
    unittest.main()
