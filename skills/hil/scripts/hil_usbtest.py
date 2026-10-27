#!/usr/bin/env python3
"""Linux usbtest/testusb mechanics shared by the HIL harnesses (`hil` skill). Linux only,
stdlib only.

The device lookup, driver loading and binding, the bounded testusb case run and its
parsing, and the upstream host controller check. Every project value is a parameter: the
device VID:PID, the interface, the usbfs node, the case, its testusb options and every
bound. Which cases run, their options, throughput, recovery and the report are the caller's.

Failures that make a battery meaningless exit (SystemExit with the reason), as a CLI
battery does; a case's own outcome is its result dict.

The device must carry an id in usbtest's static table (Gadget Zero 0525:a4a0 for a full
source/sink profile): usbtest has no new_id since 6.12.108 (upstream 00e2071f6d56), and its
GENERIC vendor/product params bind with a profile lacking ctrl_out, iso and intr.

testusb reporting quirks run_case works around:
- its exit code is always 0 when the device exists: results are parsed from stdout
- a case gated off by the driver's capability profile (or an in-kernel parameter check)
  returns -EOPNOTSUPP, which testusb silently skips: a missing result line is NOTRUN.
"""

from __future__ import annotations

import errno
import fcntl
import importlib.util
import os
import platform
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

SYS_USB = Path('/sys/bus/usb/devices')
SYS_DEV_CHAR = Path('/sys/dev/char')
DRIVER = Path('/sys/bus/usb/drivers/usbtest')
PATTERN_PARAM = Path('/sys/module/usbtest/parameters/pattern')
HELPER_TIMEOUT = 30         # default bound for the root helpers (hil_root.py verbs)
CONNECT_TIMEOUT = 15        # the usbfs open, CONNECT and close each take the device lock
# struct usbdevfs_ioctl and its requests (v6.12 include/uapi/linux/usbdevice_fs.h)
_USBDEVFS_IOCTL_ARG = struct.Struct('@iiP')
USBDEVFS_IOCTL = 0xC0005500 | _USBDEVFS_IOCTL_ARG.size << 16 | 18   # _IOWR('U', 18, ...)
USBDEVFS_CONNECT = 0x5517                                         # _IO('U', 23)
GENERIC_IOCTL_MACHINES = {'x86_64', 'i686', 'aarch64', 'armv7l', 'riscv64'}   # asm-generic/ioctl.h

RE_PASS = re.compile(r'test (\d+),\s*(\d+)\.(\d+) secs')
RE_FAIL = re.compile(r'test (\d+) --> (\d+) \((.*)\)')
_HEX4 = re.compile(r'[0-9a-f]{4}\Z')

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/usb_observe.py', 'skills/hil/scripts/hil_root.py')


def _hil_load():
    """hil_load.py from beside this file, the process's one `agentrc_hil_load`."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, 'hil_load.py'))
    if spec.name not in sys.modules:
        if not os.path.isfile(spec.origin):
            raise ImportError(f'{spec.origin} is missing')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, spec.origin)


if __name__ != '__main__':      # the CONNECT child loads nothing but this file
    hil_load = _hil_load()
    hil_load.require(__file__, FILES)
    # find_device's walk and bounded reads: the process's one usb_observe (hil_load), so the
    # strand memo a harness's path_stranded checks read is the one find_device fed
    usb_observe = hil_load.load_file('skills/hil/scripts/usb_observe.py')
    # every root operation: a rig's sudo grant covers its installed copy and nothing else
    hil_root = hil_load.load_file('skills/hil/scripts/hil_root.py')

SERIAL_GRACE = 1.0   # find_device's bound on one `serial` read, tighter than usb_observe's
                     # default on purpose: a runner calls it after every case and in its
                     # startup poll, so N wedged peers cost N x this on the scan that meets them
_DEV_CACHE: dict = {}   # (vid, pid, serial lowercased) -> what find_device last resolved


def run(cmd, **kw):
    # NOT subprocess.run(timeout=): CPython's post-timeout path is an UNBOUNDED wait() that
    # never returns on a D-state child -- a CONNECT or modprobe stuck on a device lock.
    timeout = kw.pop('timeout', None)
    kw.pop('capture_output', None)        # subprocess.run-only kwarg: expressed by the PIPEs below
    kw.setdefault('text', True)
    kw.setdefault('encoding', 'utf-8')
    kw.setdefault('errors', 'replace')   # strict decode would raise out of _root_soft
    # NO start_new_session: the root helpers must stay in our
    # process group so the caller's outer killpg reaps them with us.
    timeout = timeout if timeout is not None else HELPER_TIMEOUT
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        # Under sudo our child is only the wrapper; the root grandchild survives this and
        # is left for the caller's report to name. Close our pipe ends so an abandoned
        # child costs no fds.
        try:
            proc.kill()          # same group as us: never killpg, that would kill us too
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass          # unkillable: abandon it, the caller reports the timeout
        raise


def root(verb, *args, **kw):
    """One hil_root.py verb (hil_root.argv picks the copy and the sudo)."""
    cmd = hil_root.argv(verb, *args)
    r = run(cmd, **kw)
    if r.returncode != 0 and 'password is required' in (r.stderr or ''):
        sys.exit(f'sudo needs a password for: {" ".join(cmd)}\n'
                 'Run as root, or grant this user NOPASSWD sudo on it.')
    return r


def _root_soft(verb, *args, **kw):
    """root() for calls whose failure must never abort the battery: run() re-raises
    TimeoutExpired, and two of these are evaluated inside run_case's own timeout handler
    -- a raise there loses the HUNG verdict, the recovery and the caller's report."""
    try:
        return root(verb, *args, **kw)
    except (OSError, ValueError, subprocess.SubprocessError, SystemExit) as e:
        # SystemExit too: root() sys.exit()s on 'a password is required', unwinding out of
        # run_case's timeout handler before the HUNG verdict is recorded
        why = f'{verb}: {type(e).__name__}: {e}'
        print(why, file=sys.stderr)
        return subprocess.CompletedProcess([verb, *args], 1, '', why)


def sysfs_write(verb, *args):
    """One hil_root.py write verb, bounded at 15 s."""
    what = ' '.join(('hil_root', verb, *args))
    try:
        r = root(verb, *args, timeout=15)
    except subprocess.TimeoutExpired:
        sys.exit(f'{what} blocked >15s. Check dmesg before any recovery '
                 '(usb-kernel-recover skill).')
    if r.returncode != 0:
        sys.exit(f'{what} failed: {r.stderr.strip()}')


def dmesg_tail():
    r = _root_soft('dmesg')           # hil_root keeps only the lines naming usbtest
    return '\n'.join(r.stdout.splitlines()[-8:])


def node_writable(node):
    """The usbfs node opens for writing: root, or the project's udev rule."""
    return os.geteuid() == 0 or os.access(node, os.W_OK)


# speed_show's values (v6.12 drivers/usb/core/sysfs.c:145-177) less 'unknown', which names no speed
_SPEEDS = ('1.5', '12', '480', '5000', '10000', '20000')


def _describe(dev, serial):
    """find_device's result for usb_observe entry `dev`, whose `serial` the caller read and
    matched; None when it left the bus mid-walk. A present device whose node, speed or tier
    attribute is missing or malformed is a named exit, never a guess."""
    bad = [k for k in ('busnum', 'devnum') if dev[k] is None]
    if dev['speed'] not in _SPEEDS:
        bad.append('speed')
    bcd = dev['bcdDevice']
    if bcd is None or not _HEX4.match(bcd):
        bad.append('bcdDevice')
    if bad:
        if not os.path.isdir(dev['dir']):
            return None
        sys.exit(f"{dev['busport']} ({dev['vid']}:{dev['pid']} serial {serial}) has no usable "
                 f"{', '.join(bad)}: refusing to guess its node, speed or tier")
    return {'sysname': dev['busport'], 'serial': serial,
            'node': '/dev/bus/usb/%03d/%03d' % (dev['busnum'], dev['devnum']),
            'speed': dev['speed'], 'tier': int(bcd[-2:], 16)}


def _identities(devs):
    return {(d['busport'], d['ino']) for d in devs}


def _confirm(cached, peers, serial, timeout):
    """The cached device re-described after a bounded read of its own serial, or None when a
    full scan must decide: the device gone or re-enumerated, its serial another board's, or a
    peer the last scan could not read now answering (it may answer `serial` too)."""
    dev = next(d for d in peers if (d['busport'], d['ino']) == cached['target'])
    sn = usb_observe.read_sysfs(os.path.join(dev['dir'], 'serial'), timeout)
    if sn is None or sn.lower() != serial.lower() \
            or usb_observe.enumeration_id(dev['dir']) != dev['ino']:
        return None
    if any(usb_observe.read_sysfs(os.path.join(d, 'serial'), timeout) is not None
           for d in cached['unread']):
        return None
    return _describe(dev, sn)


def _hex_id(value):
    """A vid or pid as sysfs writes it, from an int (0xcafe) or a hex string ('CAFE')."""
    return f'{value:04x}' if isinstance(value, int) else value.lower()


def find_device(vid, pid, serial=None, timeout=SERIAL_GRACE):
    """The enumerated `vid`:`pid` device answering `serial` (any, when None): {sysname, serial,
    node, speed, tier}, {'ambiguous': [sysnames]} when several answer `serial`, or None. Several
    devices without a `serial` to choose is a named exit. `vid` and `pid` are ints or hex strings.

    A full scan (usb_observe.usb_scan) pays a bounded `serial` read on every vid:pid peer, and
    a runner calls this after every case, so a device found by `serial` is cached: the next
    call reads only its own serial, the one attribute that tells identical boards apart -- a
    busport can name a different board after a renumber (controller reset, reboot). The cache
    holds only while the lock-free picture of vid:pid devices is unchanged (none added, gone or
    re-enumerated: each changes an enumeration_id) and every peer the scan could not read
    still cannot be read, so a second device answering `serial` always reaches the full scan.
    A wedged peer costs one bounded read per call, at once after the first (read_sysfs
    memoises it).
    """
    vid, pid = _hex_id(vid), _hex_id(pid)
    key = (vid, pid, serial.lower()) if serial else None
    cached = _DEV_CACHE.pop(key, None)
    if cached:
        peers = usb_observe.usb_devices(vid_pid=(vid, pid))
        if _identities(peers) == cached['picture']:
            hit = _confirm(cached, peers, serial, timeout)
            if hit:
                _DEV_CACHE[key] = cached
                return hit
    scanned = usb_observe.usb_scan(vid_pid=(vid, pid), timeout=timeout)
    matches = [d for d in scanned if serial is None or d['serial'].lower() == serial.lower()]
    if len(matches) > 1:
        if serial:
            # Dual-port parts (nanoch32v203, ch32v307) briefly enumerate BOTH ports with
            # one serial around a variant reflash, and picking one could bind the stale
            # port -- report ambiguity so the caller retries until it drops.
            return {'ambiguous': sorted(d['busport'] for d in matches)}
        sys.exit(f'multiple {vid}:{pid} devices found, use --serial: '
                 + ', '.join(d['serial'] for d in matches))
    hit = _describe(matches[0], matches[0]['serial']) if matches else None
    if hit and serial:
        # the picture is walked after the scan, and cached only if it still holds every device
        # the scan read: one observation, so nothing the scan never saw can hide behind it
        peers = usb_observe.usb_devices(vid_pid=(vid, pid))
        read, picture = _identities(scanned), _identities(peers)
        if read <= picture:
            _DEV_CACHE[key] = {'target': (hit['sysname'], matches[0]['ino']),
                               'picture': picture,
                               'unread': [d['dir'] for d in peers
                                          if (d['busport'], d['ino']) not in read]}
    return hit


def check_host_compat(node):
    """Refuse to run when the DUT's upstream host controller is known-incompatible:
    the MosChip MCS9990 (9710:9990) outright (buggy FRINDEX silicon: EHCI never
    schedules int-OUT URBs and mangles unlinked reads - verified A/B 2026-07-09). A Renesas
    uPD720201/02 needs firmware >= 2.0.2.6, or its command ring dies under unlink stress: a rig
    setup prerequisite its owner checks after each reboot, not checked here."""
    for attempt in range(3):
        try:
            root = SYS_USB / f"usb{int(node.split('/')[-2])}"
            pci = (root / '..').resolve()
            vid_did = ((pci / 'vendor').read_text().strip(), (pci / 'device').read_text().strip())
            break
        except (OSError, ValueError):
            # transient sysfs error (racing a re-enumeration): retry so a blip does not
            # silently pass an incompatible host, then fail open but say so
            if attempt == 2:
                print('warning: cannot probe the upstream host controller; '
                      'skipping the host compatibility check', file=sys.stderr)
                return
            time.sleep(1)
    if vid_did == ('0x9710', '0x9990'):
        sys.exit(f'REFUSING to run: DUT is behind a MosChip MCS9990 ({pci.name}), which is '
                 'incompatible with usbtest: broken FRINDEX silicon - int-OUT URBs are never '
                 'placed in the EHCI periodic schedule and unlinked reads complete as short '
                 'transfers (EREMOTEIO). Move the DUT to an xHCI port.')


def load_usbtest():
    """modprobe usbtest unless loaded: udev usually loads it on the device's modalias."""
    if not DRIVER.exists():
        r = _root_soft('modprobe')
        if r.returncode != 0 or not DRIVER.exists():
            sys.exit(f'cannot load usbtest module: {r.stderr.strip()}')


def bound_driver(interface):
    drv = SYS_USB / interface / 'driver'
    return drv.resolve().name if drv.is_symlink() else None


def ensure_usbtest_bound(interface, node):
    """Bind `interface` (sysfs name, e.g. '1-1:1.0') of the device at usbfs `node` to usbtest.

    usbtest matches the device's static id itself, so an unbound interface needs only a reprobe:
    a usbfs CONNECT (device_attach, v6.12 devio.c proc_ioctl) rather than the driver's bind
    file, since any opener with write access may issue it, no root, and its fd pins one
    enumeration. Both take the device's and then the interface's lock, so it runs in a child
    bounded at CONNECT_TIMEOUT. A node this user cannot write (testusb needs it too, so bound
    or not) and an interface held by another driver (a usbfs claim, a foreign driver) are named
    exits, never sudo or a steal."""
    if not node_writable(node):
        sys.exit(f"{node} is not writable: testusb and the usbfs CONNECT need write access to it "
                 "(the project's udev rule)")
    load_usbtest()
    held = bound_driver(interface)
    if held is None:
        if platform.system() != 'Linux' or platform.machine() not in GENERIC_IOCTL_MACHINES:
            sys.exit(f'{interface} is unbound and usbfs CONNECT has no known ioctl ABI here '
                     f'({platform.system()} {platform.machine()})')
        cmd = [sys.executable, '-I', os.path.abspath(__file__), 'connect', node, interface]
        try:
            r = run(cmd, timeout=CONNECT_TIMEOUT)
        except subprocess.TimeoutExpired:
            sys.exit(f'usbfs CONNECT on {interface} blocked >{CONNECT_TIMEOUT}s: its device lock is '
                     'held -- a peer case or a wedged device. Check dmesg before any recovery '
                     '(usb-kernel-recover skill).')
        held = bound_driver(interface)   # EBUSY may mean a concurrent probe won: recheck
        if held is None:
            why = r.stderr.strip() or f'exit {r.returncode}'
            sys.exit(f'{interface} did not bind to usbtest (CONNECT {why}):\n{dmesg_tail()}')
    if held != 'usbtest':
        sys.exit(f'{interface} is held by {held}, not usbtest: release it, then rerun')


def _usbfs_connect(node, interface):
    """ensure_usbtest_bound's child: CONNECT `interface` through `node` once the open fd is
    confirmed to be that interface's device. 0, or the errno."""
    device, _, ifc = interface.partition(':')
    try:
        fd = os.open(node, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as e:
        return e.errno
    try:
        rdev = os.fstat(fd).st_rdev
        # the fd pins one enumeration: a replaced device must not take the CONNECT
        if os.path.realpath(SYS_DEV_CHAR / f'{os.major(rdev)}:{os.minor(rdev)}') != \
                os.path.realpath(SYS_USB / device):
            return errno.ENODEV
        ifno = int(ifc.split('.')[1])
        fcntl.ioctl(fd, USBDEVFS_IOCTL, _USBDEVFS_IOCTL_ARG.pack(ifno, USBDEVFS_CONNECT, 0))
    except OSError as e:
        return e.errno
    finally:
        os.close(fd)
    return 0


def set_pattern(value):
    try:
        if PATTERN_PARAM.read_text().strip() != str(value):
            sysfs_write('pattern', str(value))
    except OSError as e:  # FileNotFoundError (no param), PermissionError (root-only), ...
        sys.exit(f'{PATTERN_PARAM} not usable ({e.strerror}): this usbtest module build may lack '
                 'the "pattern" param, or it is not readable')


def run_case(testusb, node, num, params, timeout, *, wedge_confirm):
    """Run testusb case `num` with options `params` (argv list) on usbfs `node`, bounded by
    `timeout` s. Returns the verdict fields: status PASS (secs), FAIL (detail; dmesg, stderr
    or late_cleared where they apply), NOTRUN or HUNG.

    HUNG means the killed child was not reaped within 5 s and then not within `wedge_confirm`
    s more; it carries `_proc`, the surviving child for the caller's recovery to watch. A node
    this user cannot write fails without running testusb: never under sudo, whose wrapper's
    reaping proves nothing."""
    # -A <node> confines testusb's ftw() device scan to the DUT: with -D alone it opens every
    # usbfs node, blocking on any peer's held device lock (hathach/tinyusb#4047). -A must
    # precede -D, it clears it.
    cmd = [testusb, '-A', node, '-D', node, '-t', str(num), *params]
    result = {}
    if not node_writable(node):
        # a re-enumeration after the caller's check can hand back a node only root may open
        result.update(status='FAIL', detail=f'{node} is not writable: install the project\'s udev rule')
        return result

    # NO start_new_session: testusb must stay in OUR process group so the caller's outer
    # killpg still reaps it.
    # errors='replace': testusb output is not guaranteed UTF-8, and a strict decode would
    # raise out of here and out of the caller, printing no report at all.
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding='utf-8', errors='replace')
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except OSError:
            pass
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGKILL had no effect: the child is in uninterruptible sleep on the DUT's
            # device lock or an in-kernel usbfs ioctl. A finite hold is reaped once it ends,
            # so watch the child for `wedge_confirm` before calling it a wedge
            # (hathach/tinyusb#3944).
            t0 = time.monotonic()
            try:
                p.wait(timeout=wedge_confirm)
                waited = time.monotonic() - t0
                result.update(status='FAIL', dmesg=dmesg_tail(), late_cleared=True,
                              detail=f'timeout after {timeout}s (the kill landed '
                                     f'{waited:.0f}s late; testusb reaped during the '
                                     f'confirmation watch)')
                return result
            except subprocess.TimeoutExpired:
                pass
            result.update(status='HUNG', detail=f'testusb stuck in D state after {timeout}s',
                          dmesg=dmesg_tail())
            # not JSON: the caller watches the child after its recovery step
            result['_proc'] = p
            return result
        result.update(status='FAIL', detail=f'timeout after {timeout}s', dmesg=dmesg_tail())
        return result

    m = RE_PASS.search(out)
    if m and int(m.group(1)) == num:
        result.update(status='PASS', secs=float(f'{m.group(2)}.{m.group(3)}'))
        return result

    m = RE_FAIL.search(out)
    if m and int(m.group(1)) == num:
        result.update(status='FAIL', detail=f'errno {m.group(2)} ({m.group(3)})',
                      dmesg=dmesg_tail())
        return result

    # testusb prints '<speed> speed\t<node>\t<ifnum>' once it has opened the node
    # (tools/usb/testusb.c handle_testdev). Without it testusb never reached the ioctl: an
    # open failure, a usage error. Not NOTRUN, which sends the reader to the binding profile.
    if not re.search(rf'^\S+ speed\t{re.escape(node)}\t\d+$', out, re.M):
        result.update(status='FAIL', detail=f'testusb did not run the case (rc {p.returncode})',
                      stderr=out.strip())
        return result

    # no result line: the kernel returned -EOPNOTSUPP (capability profile or
    # in-kernel parameter gate) and testusb skipped silently
    result.update(status='NOTRUN', detail='case gated off: check binding profile/pattern',
                  stderr=out.strip())
    return result


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != 'connect':
        sys.exit('usage: hil_usbtest.py connect NODE INTERFACE')
    rc = _usbfs_connect(sys.argv[2], sys.argv[3])
    if rc:
        print(errno.errorcode.get(rc, rc), file=sys.stderr)
    sys.exit(rc)
