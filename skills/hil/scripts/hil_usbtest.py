#!/usr/bin/env python3
"""Linux usbtest/testusb mechanics shared by the HIL harnesses (`hil` skill). Linux only,
stdlib only.

The device lookup, driver registration and binding, the bounded testusb case run and its
parsing, and the upstream host controller check. Every project value is a parameter: the
device VID:PID, the reference id whose capability profile it inherits, the interface, the
usbfs node, the case, its testusb options and every bound. Which cases run, their options,
throughput, recovery and the report are the caller's.

Failures that make a battery meaningless exit (SystemExit with the reason), as a CLI
battery does; a case's own outcome is its result dict.

Binding uses the 5-field new_id form referencing a known usbtest id so the dynamic id
inherits its capability profile. Never register a plain "vid pid" dynamic id with usbtest:
the dynid then has driver_info == 0 and usbtest_probe() dereferences it without a NULL
check (kernel oops).

testusb reporting quirks run_case works around:
- its exit code is always 0 when the device exists: results are parsed from stdout
- a case gated off by the driver's capability profile (or an in-kernel parameter check)
  returns -EOPNOTSUPP, which testusb silently skips: a missing result line is NOTRUN.
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SYS_USB = Path('/sys/bus/usb/devices')
DRIVER = Path('/sys/bus/usb/drivers/usbtest')
PATTERN_PARAM = Path('/sys/module/usbtest/parameters/pattern')
# In the board-lock dir, shared by every run on the rig, but not a *.lock: hil_lock treats
# those as boards (status, release --all)
REGISTER_LOCK_NAME = 'usbtest-new_id.registry'
REGISTER_LOCK_TIMEOUT = 30  # a holder only checks and writes new_id, itself bounded at 15 s
HELPER_TIMEOUT = 30         # default bound for sudo helpers (dmesg/modprobe/setpci/tee)

RE_PASS = re.compile(r'test (\d+),\s*(\d+)\.(\d+) secs')
RE_FAIL = re.compile(r'test (\d+) --> (\d+) \((.*)\)')
_HEX4 = re.compile(r'[0-9a-f]{4}\Z')

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/usb_observe.py')


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


hil_load = _hil_load()
hil_load.require(__file__, FILES)
# find_device's walk and bounded reads: the process's one usb_observe (hil_load), so the strand
# memo a harness's path_stranded checks read is the one find_device fed
usb_observe = hil_load.load_file('skills/hil/scripts/usb_observe.py')

SERIAL_GRACE = 1.0   # find_device's bound on one `serial` read, tighter than usb_observe's
                     # default on purpose: a runner calls it after every case and in its
                     # startup poll, so N wedged peers cost N x this on the scan that meets them
_DEV_CACHE: dict = {}   # (vid, pid, serial lowercased) -> what find_device last resolved


def run(cmd, **kw):
    # NOT subprocess.run(timeout=): CPython's post-timeout path is an UNBOUNDED wait() that
    # never returns on a D-state child -- the hang sysfs_write's timeout exists to catch.
    timeout = kw.pop('timeout', None)
    data = kw.pop('input', None)          # subprocess.run-only kwarg; Popen takes stdin
    kw.pop('capture_output', None)        # ditto: expressed by the PIPEs below
    kw.setdefault('text', True)
    kw.setdefault('encoding', 'utf-8')
    kw.setdefault('errors', 'replace')   # strict decode would raise out of _sudo_soft
    # NO start_new_session: these helpers (dmesg, modprobe, setpci, tee) must stay in our
    # process group so the caller's outer killpg reaps them with us.
    timeout = timeout if timeout is not None else HELPER_TIMEOUT
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if data is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    try:
        out, err = proc.communicate(input=data, timeout=timeout)
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
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass          # unkillable: abandon it, the caller reports the timeout
        raise


def sudo(cmd, **kw):
    if os.geteuid() != 0:
        cmd = ['sudo', '-n'] + cmd
    r = run(cmd, **kw)
    if r.returncode != 0 and 'password is required' in (r.stderr or ''):
        sys.exit(f'sudo needs a password for: {" ".join(cmd)}\n'
                 'Run as root, or grant this user NOPASSWD sudo.')
    return r


def _sudo_soft(cmd, **kw):
    """sudo() for calls whose failure must never abort the battery: run() re-raises
    TimeoutExpired, and two of these are evaluated inside run_case's own timeout handler
    -- a raise there loses the HUNG verdict, the recovery and the caller's report."""
    try:
        return sudo(cmd, **kw)
    except (OSError, ValueError, subprocess.SubprocessError, SystemExit) as e:
        # SystemExit too: sudo() sys.exit()s on 'a password is required', unwinding out of
        # run_case's timeout handler before the HUNG verdict is recorded
        why = f'{cmd[0]}: {type(e).__name__}: {e}'
        print(why, file=sys.stderr)
        return subprocess.CompletedProcess(cmd, 1, '', why)


def sysfs_write(path, data, check=True):
    # A driver-registry write (new_id/remove_id/bind) blocks in D state while a device lock
    # is held: fail fast instead of piling up unkillable writers. A timeout alone does not
    # say which holder: a peer's in-flight testusb case holds its lock for the whole case
    # (usbdev_do_ioctl), a wedged device holds it for good.
    #
    # Verified in v6.12.96: unbind_store -> device_driver_detach ->
    # device_release_driver_internal -> __device_driver_lock (drivers/base/dd.c), which
    # takes device_lock() -- the UNINTERRUPTIBLE variant, unlike the sysfs read path -- and
    # ALSO device_lock(parent), because usb_bus_type sets need_parent_lock = true
    # (drivers/usb/core/driver.c:2048). So one such write against a wedged device blocks
    # unkillably while holding the HUB's lock: that is the mechanism by which a single
    # wedged port takes its whole bus down, and why this fails fast instead.
    try:
        r = sudo(['tee', str(path)], input=data, timeout=15)
    except subprocess.TimeoutExpired:
        sys.exit(f'write "{data}" > {path} blocked >15s: a device lock is held -- possible '
                 'device-lock contention or a wedged device. Check dmesg before any recovery '
                 '(usb-kernel-recover skill).')
    if check and r.returncode != 0:
        sys.exit(f'write "{data}" > {path} failed: {r.stderr.strip()}')
    return r.returncode == 0


def dmesg_tail():
    r = _sudo_soft(['dmesg'])
    lines = [l for l in r.stdout.splitlines() if 'usbtest' in l]
    return '\n'.join(lines[-8:])


def needs_sudo(node):
    """Device nodes are usually opened directly (udev rule); sudo only if not."""
    return not os.access(node, os.W_OK) and os.geteuid() != 0


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
    schedules int-OUT URBs and mangles unlinked reads - verified A/B 2026-07-09),
    and the Renesas uPD720201/02 unless it runs firmware >= 2.0.2.6 (see below)."""
    for attempt in range(3):
        try:
            root = SYS_USB / f"usb{int(node.split('/')[-2])}"
            drv = (root / '../driver').resolve().name
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
    if drv.startswith('xhci') and vid_did in (('0x1912', '0x0014'), ('0x1912', '0x0015')):
        # The Renesas uPD720201/uPD720202 must run firmware >= 2.0.2.6 (K2026090.mem;
        # RAM-uploaded, so it reverts to ROM on every power cycle): on ROM firmware its
        # command ring dies under unlink stress and the hub worker deadlocks holding the
        # device lock, needing a host power cycle (ch32v307 2026-07-10; ra6m5 test 24,
        # mimxrt1015 2026-07-11). Both parts expose the FW version at PCI config 0x6c.
        # Necessary, not sufficient -- batteries have killed the controller on current
        # firmware too, which per-board skips in the rig config handle.
        fw = None
        try:
            r = _sudo_soft(['setpci', '-s', pci.name, '0x6c.l'])
            if r.returncode == 0:
                fw = int(r.stdout.strip(), 16)
        except (OSError, ValueError):
            pass
        if fw is None:
            sys.exit(f'REFUSING to run: cannot read host xHCI Renesas ({pci.name}) firmware '
                     'version (setpci missing or not permitted) - usbtest requires verified '
                     'firmware >= 0x00202609 (2.0.2.6); on older firmware the command ring '
                     'dies under unlink stress. Install pciutils / fix sudo, or load the '
                     'firmware and re-check.')
        if fw < 0x00202609:
            sys.exit(f'REFUSING to run: host xHCI Renesas ({pci.name}) firmware 0x{fw:08x} '
                     '< 0x00202609 (2.0.2.6) - its command ring dies under usbtest unlink '
                     'stress. Load the latest firmware (K2026090.mem; it is RAM-uploaded and '
                     'reverts to ROM on every power cycle).')


def register_usbtest_id(vid, pid, ref, lock_dir):
    """Register `vid`:`pid` with usbtest, inheriting the capability profile of `ref`
    ('vvvv pppp'): one new_id write per module load, before any battery.

    Every new_id write runs driver_attach, which takes the device lock of every matching
    interface on the rig; a peer's testusb case holds its lock for the whole case, so a write
    beside running batteries waits for the slowest case and trips sysfs_write's bound. The
    kernel keeps no duplicate check and lists the id before driver_attach runs, so the check
    and the write share one flock in `lock_dir` (the rig's board-lock dir): two initializers
    make one write, and neither returns while the other's attach is in flight. A wrong entry
    is repaired by a module reload, never by remove_id + this."""
    ref_ids = ref.split(' ')
    # the listing is lowercase '%04x %04x': a vid/pid spelled otherwise never matches and every
    # run would append another entry; ref is held to the same spelling as an input contract
    if not all(_HEX4.match(x) for x in (vid, pid, *ref_ids)) or len(ref_ids) != 2:
        raise ValueError(f'usbtest id {vid!r}:{pid!r} ref {ref!r}: '
                         "expected lowercase 4-digit hex ids and ref 'vvvv pppp'")
    if not DRIVER.exists():
        r = _sudo_soft(['modprobe', 'usbtest'])
        if r.returncode != 0 or not DRIVER.exists():
            sys.exit(f'cannot load usbtest module: {r.stderr.strip()}')
    path = os.path.join(lock_dir, REGISTER_LOCK_NAME)
    try:
        os.makedirs(lock_dir, exist_ok=True)
        fh = open(path, 'a')
    except OSError as e:
        sys.exit(f'usbtest id registration lock {path}: {e.strerror}')
    with fh:
        deadline = time.monotonic() + REGISTER_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    sys.exit(f'usbtest id registration lock {path} held >{REGISTER_LOCK_TIMEOUT}s')
                time.sleep(0.1)
            except OSError as e:
                sys.exit(f'usbtest id registration lock {path}: {e.strerror}')
        try:
            listed = (DRIVER / 'new_id').read_text().splitlines()
        except OSError as e:
            sys.exit(f'cannot read {DRIVER / "new_id"}: {e.strerror}')
        if f'{vid} {pid}' not in listed:
            sysfs_write(DRIVER / 'new_id', f'{vid} {pid} 0 {ref}')


def bind_usbtest(interface):
    """Bind `interface` (sysfs name, e.g. '1-1:1.0') to usbtest. The id is registered
    already (register_usbtest_id), so this writes only this interface's own bind/unbind."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        drv = SYS_USB / interface / 'driver'
        if drv.is_symlink():
            if drv.resolve().name == 'usbtest':
                return
            # claimed by a foreign driver: steal the interface
            sysfs_write(drv / 'unbind', interface)
        sysfs_write(DRIVER / 'bind', interface, check=False)
        time.sleep(0.2)
    sys.exit(f'interface {interface} did not bind to usbtest')


def set_pattern(value):
    try:
        if PATTERN_PARAM.read_text().strip() != str(value):
            sysfs_write(PATTERN_PARAM, str(value))
    except OSError as e:  # FileNotFoundError (no param), PermissionError (root-only), ...
        sys.exit(f'{PATTERN_PARAM} not usable ({e.strerror}): this usbtest module build may lack '
                 'the "pattern" param, or it is not readable')


def run_case(testusb, node, num, params, timeout, *, wedge_confirm, sudo_forbidden):
    """Run testusb case `num` with options `params` (argv list) on usbfs `node`, bounded by
    `timeout` s. Returns the verdict fields: status PASS (secs), FAIL (detail; dmesg, stderr
    or late_cleared where they apply), NOTRUN or HUNG.

    HUNG means the killed child was not reaped within 5 s and then not within `wedge_confirm`
    s more; it carries `_proc`, the surviving child for the caller's recovery to watch, or
    None under sudo, whose wrapper's reaping proves nothing. `sudo_forbidden` refuses a
    sudo-wrapped testusb (a caller that must confirm a recovery)."""
    # -A <node> confines testusb's ftw() device scan to the DUT: with -D alone it opens every
    # usbfs node, blocking on any peer's held device lock (hathach/tinyusb#4047). -A must
    # precede -D, it clears it.
    cmd = [testusb, '-A', node, '-D', node, '-t', str(num), *params]
    result = {}
    if needs_sudo(node):
        if sudo_forbidden:
            # a re-enumeration after the caller's check can hand back a node only root may open
            result.update(status='FAIL', detail=f'{node} is not writable: refusing a '
                          'sudo-wrapped testusb under a recovery request')
            return result
        cmd = ['sudo', '-n'] + cmd

    # NO start_new_session: testusb must stay in OUR process group so the caller's outer
    # killpg still reaps it.
    # errors='replace': testusb output is not guaranteed UTF-8, and a strict decode would
    # raise out of here and out of the caller, printing no report at all.
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding='utf-8', errors='replace')
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Under sudo we only kill the wrapper; its root-owned testusb keeps the inherited
        # stdout pipe, so the reap below times out and the overrun is reported as HUNG.
        # Accepted rather than escalated: the rig's udev rules make the device node
        # writable, so sudo is the exception, and the harness must never sudo-kill a pid
        # it cannot prove is its own.
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
            # (hathach/tinyusb#3944). Under sudo the child is the wrapper and reaps at once,
            # which proves nothing.
            if cmd[0] != 'sudo':
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
            # not JSON: the caller watches the child after its recovery step (sudo: cannot)
            result['_proc'] = None if cmd[0] == 'sudo' else p
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

    if cmd[0] == 'sudo' and ('password is required' in out or 'a terminal is required' in out):
        result.update(status='FAIL', detail='sudo needs a password to run testusb: the device node '
                      'is not writable')
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
