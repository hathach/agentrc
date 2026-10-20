#!/usr/bin/env python3
"""Mechanics of the ataradov usb-sniffer: a bounded capture at the right link
speed, and the DUT's wire address out of a capture. Judgment (what to tap,
what a capture shows) is in SKILL.md.

    sniff.py list                                                            # attached sniffers: serial, port, holder
    sniff.py capture OUT.pcapng --port 3-2.7 --seconds 30 --limit 3000000   # speed from sysfs
    sniff.py capture OUT.pcapng --speed hs --seconds 15 --fold --serial 4433459538463a
    sniff.py addr CAP.pcapng                                                 # SET_ADDRESS values seen on the wire

capture always takes a wall-clock bound (--limit alone never ends on an idle bus
or an unfired trigger), refuses --speed and --port together, refuses a SuperSpeed
port (the sniffer is USB 2.0 only), needs --serial when several sniffers are
attached and refuses while a usb_sniffer process holds that unit or holds none it
can name. The tool picks the first sniffer it can open, so capture runs it in a
user and mount namespace where only the selected unit's usbfs node exists. It
fails, keeping the file, when the tap showed no USB packet or the trigger never
fired: the sniffer's own records (line state, VBUS, trigger) say why. addr
refuses a capture with no SET_ADDRESS and lists every address when there are
several: the caller picks the DUT."""
import argparse
import ctypes
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

SYSFS = Path('/sys/bus/usb/devices')
USBFS = Path('/dev/bus/usb')
PROC = Path('/proc')
SPEEDS = {'1.5': 'ls', '12': 'fs', '480': 'hs'}
PIN_FAILED = 125   # _pinned's exit when it could not pin the unit; the tool never ran
UNSHARE = ['unshare', '--user', '--map-root-user', '--mount', '--propagation', 'private', '--']
USBFS_LIMIT = Path('/sys/module/usbcore/parameters/usbfs_memory_mb')   # one limit for every usbfs user (devio.c)
# MiB of usbfs a capture is budgeted: upstream 57a24e1's Linux queue (usb.c, 16 x 512*2000 B) plus the kernel's
# per-URB accounting; one capture fits the default limit of 16, two fit 32 (both seen on hardware)
TOOL_USBFS_MB = 16
RULE_USBFS_MB = 64   # the limit 88-usb-sniffer.rules raises a lower one to


class Refused(Exception):
    pass


class Unit(NamedTuple):
    serial: str
    port: str     # the sniffer's own sysfs port, not the tapped one
    dev: str      # sysfs `dev`, decimal major:minor of its usbfs node
    node: Path

    def __str__(self):
        return f'{self.serial or "(no serial)"} at {self.port}'


def serial_of(d):
    try:
        return (d / 'serial').read_text().strip()
    except OSError:   # firmware with no serial string
        return ''


def units():
    """The attached sniffers (capture port 6666:6620), by sysfs port; refuses when there are none."""
    found = []
    for d in sorted(SYSFS.iterdir()):
        try:
            if ((d / 'idVendor').read_text().strip(), (d / 'idProduct').read_text().strip()) != ('6666', '6620'):
                continue
            busnum, devnum, dev = ((d / f).read_text().strip() for f in ('busnum', 'devnum', 'dev'))
        except OSError:   # an interface, or a device gone meanwhile
            continue
        found.append(Unit(serial_of(d), d.name, dev, USBFS / f'{int(busnum):03d}' / f'{int(devnum):03d}'))
    if not found:
        raise Refused('no sniffer (6666:6620) attached; see `lsusb -d 6666:6620`')
    return found


def rdev(dev):
    major, minor = dev.split(':')
    return os.makedev(int(major), int(minor))


def select(attached, serial):
    """The unit to capture from; never a guess between several."""
    listing = '; '.join(map(str, attached))
    if serial is None:
        if len(attached) > 1:
            raise Refused(f'{len(attached)} sniffers attached, give --serial: {listing}')
        return attached[0]
    matches = [u for u in attached if u.serial == serial]
    if len(matches) != 1:
        raise Refused(f'{len(matches) or "no"} sniffers with serial {serial}: {listing}')
    return matches[0]


def port_speed(port, sysfs=SYSFS):
    """ls/fs/hs of the device at a sysfs port such as 3-2.7."""
    f = sysfs / port / 'speed'
    if not f.exists():
        raise Refused(f'no device at port {port} ({f} missing); see `lsusb -t`')
    speed = f.read_text().strip()
    if speed not in SPEEDS:
        raise Refused(f'port {port} runs at {speed} Mb/s; the sniffer captures USB 2.0 (ls/fs/hs) only')
    return SPEEDS[speed]


def tool(cmd, what, **kw):
    """Run a helper; a missing one is a refusal, not a traceback."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        raise Refused(f'{cmd[0]} not found ({what}); SKILL.md, Setup')


def running():
    """pids of usb_sniffer processes already capturing."""
    r = tool(['pgrep', '-x', 'usb_sniffer'], 'looking for a leftover capture')
    if r.returncode not in (0, 1):  # 1 is "no match"; anything else is pgrep itself failing
        raise Refused(f'pgrep failed ({r.returncode}): {r.stderr.strip()}')
    return r.stdout.split()


def held(pids, proc=PROC):
    """{pid: st_rdev of every char device it holds open}, None where its fds cannot be read;
    a pid or fd gone during the look is skipped."""
    out = {}
    for pid in pids:
        try:
            fds = list((proc / pid / 'fd').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            out[pid] = None
            continue
        devs = set()
        for fd in fds:
            try:
                st = os.stat(fd)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                devs = None
                break
            if stat.S_ISCHR(st.st_mode):
                devs.add(st.st_rdev)
        out[pid] = devs
    return out


def holder(unit, attached, holds):
    """('busy', pid) when a usb_sniffer holds the unit, ('unknown', pid) when one holds no attached
    sniffer (still starting, or its fds unreadable) and could take any, else ('idle', None)."""
    ours = {rdev(u.dev) for u in attached}
    for pid, devs in holds.items():
        if devs is not None and rdev(unit.dev) in devs:
            return 'busy', pid
    for pid, devs in holds.items():
        if devs is None or not devs & ours:
            return 'unknown', pid
    return 'idle', None


def usbfs_room(others):
    """Refuse when, by the budget estimate, the kernel's usbfs buffer limit cannot hold this capture next to
    `others` running ones: the tool would die at its first transfer with LIBUSB_ERROR_NO_MEM. Other usbfs
    users count against the same limit, so a capture can still fail this way."""
    try:
        limit = int(USBFS_LIMIT.read_text())
    except (OSError, ValueError) as e:
        raise Refused(f'cannot read the usbfs buffer limit {USBFS_LIMIT}: {e}')
    need = (others + 1) * TOOL_USBFS_MB
    if limit and need > limit:
        rule = 'install the udev rule (SKILL.md, Setup), ' if limit < RULE_USBFS_MB and need <= RULE_USBFS_MB else ''
        raise Refused(f'{others} other capture(s) running; with this one usb_sniffer needs about {need} MiB of '
                      f'usbfs buffers, but usbcore.usbfs_memory_mb is {limit}: {rule}'
                      f'`echo {need} | sudo tee {USBFS_LIMIT}` for this boot, or wait')


def userns():
    """Refuse when this host cannot make the namespace the capture is pinned in."""
    r = tool([*UNSHARE, sys.executable, '-c', ''], 'pinning the unit')
    if r.returncode != 0:
        raise Refused(f'cannot create a user and mount namespace ({r.stderr.strip()}); capture pins the unit inside '
                      'one. Where AppArmor restricts them (Ubuntu), install usb-sniffer-unshare.apparmor: '
                      'SKILL.md, Setup')


def pinned(serial, port, dev, node, cmd):
    """Inside the namespace: leave only the unit's usbfs node under USBFS, then become the tool.
    Single process to the exec, so killing the pid stops setup too; the namespace, and every
    mount in it, ends with the tool."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p)

    def mount(src, target, fstype, flags):
        if libc.mount(src.encode(), str(target).encode(), fstype and fstype.encode(), flags, None) != 0:
            e = ctypes.get_errno()
            raise Refused(f'mount {src} on {target}: {os.strerror(e)}')

    node = Path(node)
    try:
        fd = os.open(node, os.O_PATH | os.O_CLOEXEC)   # the inode bound below is the one checked here
        st = os.fstat(fd)
    except OSError as e:
        raise Refused(f'sniffer at {port}: {node}: {e.strerror}')
    if not stat.S_ISCHR(st.st_mode) or st.st_rdev != rdev(dev):
        raise Refused(f'sniffer at {port}: {node} is no longer its usbfs node {dev}')
    try:
        now = (serial_of(SYSFS / port), (SYSFS / port / 'dev').read_text().strip())
    except OSError as e:
        raise Refused(f'sniffer at {port} is gone: {e}')
    if now != (serial, dev):
        raise Refused(f'sniffer at {port} changed: serial {now[0] or "(none)"}, dev {now[1]}, '
                      f'wanted {serial or "(none)"}, {dev}')
    mount('tmpfs', USBFS, 'tmpfs', 0)
    (USBFS / node.parent.name).mkdir()
    (USBFS / node.parent.name / node.name).touch()
    mount(f'/proc/self/fd/{fd}', USBFS / node.parent.name / node.name, None, 4096)   # MS_BIND
    os.close(fd)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:   # execvp's error names no file
        raise Refused(f'cannot run {cmd[0]}: {e.strerror}')


def contents(cap):
    """(USB packets, the sniffer's own records) of a capture: the records carry line
    state, VBUS and the trigger, and count as packets to every other tool."""
    r = tool(['tshark', '-r', str(cap), '-q', '-z', 'io,phs'], 'reading the capture')
    if r.returncode != 0:
        raise Refused(f'{cap} is not a readable capture: {r.stderr.strip()}')
    frames = {}
    for line in r.stdout.splitlines():
        name, _, rest = line.strip().partition(' ')
        if name in ('usbll', 'syslog') and 'frames:' in rest:
            frames[name] = int(rest.split('frames:')[1].split()[0])
    r = tool(['tshark', '-r', str(cap), '-Y', 'syslog', '-T', 'fields', '-e', 'syslog.msg'], 'reading the records')
    if r.returncode != 0:
        raise Refused(f'{cap}: tshark could not list the records: {r.stderr.strip()}')
    records = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if len(records) != frames.get('syslog', 0):
        raise Refused(f'{cap}: {frames.get("syslog", 0)} records counted but {len(records)} listed')
    return frames.get('usbll', 0), records


def terminated(signum, _frame):
    raise KeyboardInterrupt


def capture(a):
    if bool(a.speed) == bool(a.port):
        raise Refused('give exactly one of --speed and --port')
    if not 0 < a.seconds < float('inf'):
        raise Refused(f'--seconds must be positive and finite, got {a.seconds}')
    if a.limit is not None and a.limit <= 0:
        raise Refused(f'--limit must be positive, got {a.limit} (the tool counts down from it; 0 or less never stops)')
    if a.out.exists():
        raise Refused(f'{a.out} exists; a capture never overwrites, so a tool killed early cannot pass off the old file')
    for helper in ('usb_sniffer', 'editcap', 'tshark', 'unshare'):
        if shutil.which(helper) is None:
            raise Refused(f'{helper} not on PATH (SKILL.md, Setup)')
    attached = units()
    unit = select(attached, a.serial)
    holds = held(running())
    state, pid = holder(unit, attached, holds)
    if state == 'busy':
        raise Refused(f'sniffer {unit} is in use by usb_sniffer pid {pid}, maybe another session\'s capture: '
                      'wait, or take another unit; stop it only if it is your own abandoned one')
    if state == 'unknown':
        raise Refused(f'usb_sniffer pid {pid} holds no sniffer it can be told by (starting, or not ours to '
                      'inspect) and may take any unit; wait for it')
    usbfs_room(len(holds))   # idle: every holder is a capture on another unit
    speed = a.speed or port_speed(a.port)
    userns()
    cmd = ['usb_sniffer', '--capture', '--fifo', str(a.out), '--speed', speed]
    if a.fold:
        cmd.append('--fold')
    if a.limit:
        cmd += ['--limit', str(a.limit)]
    if a.trigger:
        cmd += ['--trigger', a.trigger]
    print('+', ' '.join(cmd), f'(sniffer {unit}, at most {a.seconds:g} s)', flush=True)
    launch = [*UNSHARE, sys.executable, '-I', str(Path(__file__).resolve()), '_pinned',
              unit.serial, unit.port, unit.dev, str(unit.node), *cmd]
    # a TERM must take the tool down too: left alone it captures until the disk is full.
    # Blocked across the launch, so none can land between the child existing and `proc`
    # naming it; a pending one is delivered at the unblock, inside the protected region.
    signal.signal(signal.SIGTERM, terminated)
    stop = {signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, stop)
    proc, rc, timed_out = None, None, False
    try:
        try:
            proc = subprocess.Popen(launch, preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, stop))
        except OSError as e:
            raise Refused(f'cannot run {launch[0]}: {e}')
        signal.pthread_sigmask(signal.SIG_UNBLOCK, stop)
        try:
            rc = proc.wait(timeout=a.seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, stop)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        signal.pthread_sigmask(signal.SIG_UNBLOCK, stop)   # one that waited is raised here, the tool gone
    if rc == PIN_FAILED:
        raise Refused(f'usb_sniffer never ran: pinning it to sniffer {unit} failed (reason above)')
    if not timed_out and rc != 0:
        raise Refused(f'usb_sniffer exited {rc}')
    if not a.out.exists() or a.out.stat().st_size == 0:
        raise Refused(f'{a.out} is empty: the tool wrote nothing (is the sniffer plugged in?)')
    if timed_out:
        # The tool closes its file on no signal, so the time bound leaves it cut mid-packet
        # and every reader refuses it; editcap keeps the whole packets.
        fd, whole = tempfile.mkstemp(dir=a.out.parent, prefix=a.out.name + '.', suffix='.whole')
        os.close(fd)
        fixed = tool(['editcap', str(a.out), whole], 'keeping the whole packets')
        if fixed.returncode != 0:
            os.unlink(whole)
            raise Refused(f'{a.out} is cut short and editcap could not repair it: {fixed.stderr.strip()}')
        os.replace(whole, a.out)
    usb, records = contents(a.out)
    end = f'time bound {a.seconds:g} s' if timed_out else f'--limit {a.limit}' if a.limit else 'tool exit'
    print(f'{a.out}: {usb} USB packets, {len(records)} sniffer records, '
          f'{a.out.stat().st_size // 1024} KiB, speed {speed}, sniffer {unit}, ended by {end}')
    if a.trigger and 'Starting capture' not in records:
        raise Refused(f'--trigger {a.trigger} never fired within {a.seconds:g} s ({a.out} kept: '
                      f'{"; ".join(records) or "no records"})')
    if not usb:
        raise Refused(f'no USB packets on the tap ({a.out} kept, records: {"; ".join(records[-8:]) or "none"}) - '
                      f'wrong --speed, or no device pulling up the tapped segment')


def set_addresses(tshark_fields):
    """{address: [frame numbers]} from 'frame<TAB>device' lines of the SET_ADDRESS filter (Wireshark
    dissects SET_ADDRESS's wValue as usb.device_address)."""
    seen = {}
    for line in tshark_fields.splitlines():
        frame, _, value = line.partition('\t')
        if value.strip():
            seen.setdefault(int(value, 0), []).append(int(frame))
    return seen


def addr_filter(address):
    """Display filter for one device's traffic; `contains "9."` would also match 19., 29., ..."""
    return f'usbll.src matches "^{address}[.]" || usbll.dst matches "^{address}[.]"'


def addr(a):
    r = tool(['tshark', '-r', str(a.cap), '-Y', 'usb.setup.bRequest == 5 && usb.bmRequestType == 0x00',
              '-T', 'fields', '-e', 'frame.number', '-e', 'usb.device_address'], 'reading the capture')
    if r.returncode != 0:
        raise Refused(f'tshark failed: {r.stderr.strip()}')
    seen = set_addresses(r.stdout)
    if not seen:
        raise Refused('no SET_ADDRESS in the capture: it started after enumeration, or the DUT is not on the tap')
    for address, frames in sorted(seen.items()):
        print(f'{address}\tframes {", ".join(map(str, frames))}\tfilter: {addr_filter(address)}')
    if len(seen) > 1:
        print(f'{len(seen)} devices enumerated on the tap; pick the DUT by frame order', file=sys.stderr)


def list_units(_a):
    attached = units()
    holds = held(running())
    for u in attached:
        state, pid = holder(u, attached, holds)
        print(f'{u.serial or "(no serial)"}\t{u.port}\t{u.node}\t{state}{f" {pid}" if pid else ""}')


def main():
    if sys.argv[1:2] == ['_pinned']:   # internal: run by capture inside the namespace
        serial, port, dev, node, *cmd = sys.argv[2:]
        try:
            pinned(serial, port, dev, node, cmd)
        except (Refused, OSError) as e:
            print(f'sniff: {e}', file=sys.stderr)
        return PIN_FAILED
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture', help='bounded capture into a pcapng')
    c.add_argument('out', type=Path)
    c.add_argument('--speed', choices=['ls', 'fs', 'hs'], help='link speed of the tapped segment')
    c.add_argument('--port', help='sysfs port of the tapped device (3-2.7): its speed picks --speed')
    c.add_argument('--limit', type=int, help='stop after N packets (the tool exits by itself)')
    c.add_argument('--seconds', type=float, required=True,
                   help='wall-clock bound: the tool is killed after S seconds, armed or capturing')
    c.add_argument('--fold', action='store_true', help='fold empty frames')
    c.add_argument('--trigger', choices=['low', 'high', 'falling', 'rising'], help='arm on the external trigger pin')
    c.add_argument('--serial', help='the sniffer to capture with (`sniff.py list`); required when several are attached')
    sub.add_parser('list', help='attached sniffers: serial, sysfs port, usbfs node, and the usb_sniffer holding each')
    d = sub.add_parser('addr', help='SET_ADDRESS values seen in a capture')
    d.add_argument('cap', type=Path)
    a = p.parse_args()
    if not sys.platform.startswith('linux'):
        sys.exit(f'sniff.py is Linux-only (usbfs, signals); this is {sys.platform}')
    try:
        {'capture': capture, 'list': list_units, 'addr': addr}[a.cmd](a)
        return 0
    except Refused as e:
        print(f'sniff: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('sniff: interrupted; the tool was stopped, a partial capture may remain', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
