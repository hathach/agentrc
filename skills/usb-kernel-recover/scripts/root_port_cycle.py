#!/usr/bin/env python3
"""One hub-class request to one root port, sent over that root hub's own usbfs node and
nothing else (usb_recover.sh root-cycle, agentrc#149); the caller supervises every run.

  root_port_cycle.py check <bus> <port>          validate; print the identity token
  root_port_cycle.py off <bus> <port> <token>    ClearPortFeature(PORT_POWER)
  root_port_cycle.py on <bus> <port> <token>     SetPortFeature(PORT_POWER)

Exit 0 done, 2 usage, 3 refused before any switching request (check never switches), 1 the
off/on request failed after it was issued (it may have reached the hub).

Identity: the token is the root hub's dev_t and sysfs directory inode. off and on refuse unless
the root hub still carries it, before and after the open: a controller re-added under the same
bus number gets the same dev_t but a new kobject, so a new inode.

Locks: the open (devio.c:1051) and every usbfs ioctl (devio.c:2608) take the root hub's device
lock uninterruptibly; only the transfer waits killably with TIMEOUT_MS (devio.c:1151). A root
hub lock already held, e.g. by hub_wq disconnecting a child (hub.c:5896, 2334), leaves a run in
D state, hence the supervision. Sources: Linux v6.12.107, USB 2.0, USB 3.2, uhubctl v2.6.0.

Run by path by usb_recover.sh (ROOT_PORT_CYCLE), and listed in skills/hil/scripts/hil_reserve.py's
FILES: a rename or CLI change needs both updated.
"""
import ctypes
import fcntl
import os
import platform
import re
import stat
import sys

SYSFS = '/sys'
DEV = '/dev'
TIMEOUT_MS = 5000
# the asm-generic _IOC encoding; alpha, mips, powerpc and sparc encode ioctl numbers differently
GENERIC_IOCTL_MACHINES = {'x86_64', 'i686', 'aarch64', 'armv7l', 'riscv64'}
# USB 2.0 Tables 11-15..11-17, USB 3.2 Table 10-9; the requests uhubctl sends (uhubctl.c:413-421, 634-638).
# usbfs passes a port-recipient request without an interface check (check_ctrlrecip, devio.c:878-934).
RT_PORT, RT_HUB_IN = 0x23, 0xA0
CLEAR_FEATURE, SET_FEATURE, GET_DESCRIPTOR = 1, 3, 6
PORT_POWER = 8
DT_HUB, DT_SS_HUB = 0x29, 0x2A
SS_HUB_SIZE = 12          # USB 3.2 Table 10-5: fixed
USB2_HUB_REQUEST = 15     # sizeof(struct usb_hub_descriptor): what a root hub returns (hcd.c:647-648)
LPSM = {0: 'ganged power switching', 1: 'per-port', 2: 'no power switching', 3: 'no power switching'}
NUM_RE = re.compile(r'[1-9][0-9]*')


class Refused(Exception):
    pass


class CtrlTransfer(ctypes.Structure):
    """struct usbdevfs_ctrltransfer (uapi usbdevice_fs.h:40-48), padded by the C rules."""
    _fields_ = [('bRequestType', ctypes.c_uint8), ('bRequest', ctypes.c_uint8), ('wValue', ctypes.c_uint16),
                ('wIndex', ctypes.c_uint16), ('wLength', ctypes.c_uint16), ('timeout', ctypes.c_uint32),
                ('data', ctypes.c_void_p)]


def usbdevfs_control():
    """USBDEVFS_CONTROL, _IOWR('U', 0, struct usbdevfs_ctrltransfer) (usbdevice_fs.h:187)."""
    if platform.system() != 'Linux' or platform.machine() not in GENERIC_IOCTL_MACHINES:
        raise Refused(f'unsupported ioctl ABI ({platform.system()} {platform.machine()})')
    return (3 << 30) | (ctypes.sizeof(CtrlTransfer) << 16) | (ord('U') << 8)


def attr(d, name):
    try:
        with open(f'{d}/{name}') as f:
            return f.read().strip()
    except OSError as e:
        raise Refused(f'cannot read {d}/{name}: {e.strerror}')


def number(d, name):
    v = attr(d, name)
    if not re.fullmatch(r'[0-9]+', v):
        raise Refused(f'{d}/{name} is {v!r}, not a number')
    return int(v)


def root_hub(bus, port):
    """(node, dev_t, inode, superspeed) of usb<bus>, from lock-free sysfs reads only (plain
    sysfs_emit: sysfs.c:200-250, 706-721; `dev`, drivers/base/core.c:2986)."""
    d = f'{SYSFS}/bus/usb/devices/usb{bus}'
    try:
        ino = os.stat(d + '/').st_ino           # the slash: the device directory, not the symlink
    except OSError as e:
        raise Refused(f'no root hub usb{bus} ({e.strerror})')
    if attr(d, 'devpath') != '0' or attr(d, 'bDeviceClass') != '09' or number(d, 'busnum') != bus:
        raise Refused(f'usb{bus} is not the root hub of bus {bus}')
    if port > number(d, 'maxchild'):
        raise Refused(f'usb{bus} has no port {port}')
    if not os.path.isdir(f'{d}/{bus}-0:1.0/usb{bus}-port{port}'):
        raise Refused(f'usb{bus} has no port directory usb{bus}-port{port}')
    m = re.fullmatch(r'([0-9]+):([0-9]+)', attr(d, 'dev'))
    if not m:
        raise Refused(f'usb{bus} has no device number')
    node = f'{DEV}/bus/usb/{bus:03d}/{number(d, "devnum"):03d}'
    # hub_is_superspeed (hub.h:153)
    return node, os.makedev(int(m[1]), int(m[2])), ino, attr(d, 'bDeviceProtocol') == '03'


def token(rdev, ino):
    return f'{os.major(rdev)}:{os.minor(rdev)}:{ino}'


def open_node(node, rdev):
    """The root hub's node, opened for writing only when it is that character device."""
    try:
        st = os.lstat(node)
    except OSError as e:
        raise Refused(f'cannot inspect {node} ({e.strerror})')
    if not stat.S_ISCHR(st.st_mode) or st.st_rdev != rdev:
        raise Refused(f'{node} is not the root hub\'s device node')
    try:
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NOCTTY)  # ioctl needs write (devio.c:2605)
    except OSError as e:
        raise Refused(f'cannot open {node} ({e.strerror})')
    if os.fstat(fd).st_rdev != rdev:
        os.close(fd)
        raise Refused(f'{node} changed while it was opened')
    return fd


def control(fd, ioctl, request_type, request, value, index, buf=None):
    if buf is None:
        ct = CtrlTransfer(request_type, request, value, index, 0, TIMEOUT_MS, None)
    else:
        ct = CtrlTransfer(request_type, request, value, index, len(buf), TIMEOUT_MS, ctypes.cast(buf, ctypes.c_void_p))
    return fcntl.ioctl(fd, ioctl, ct, True)


def check_descriptor(fd, ioctl, superspeed, bus, port):
    """Refuse unless the hub descriptor is well formed and declares per-port power switching
    (requested as get_hub_descriptor does, hub.c:412-443)."""
    dtype, size = (DT_SS_HUB, SS_HUB_SIZE) if superspeed else (DT_HUB, USB2_HUB_REQUEST)
    buf = (ctypes.c_uint8 * size)()
    try:
        n = control(fd, ioctl, RT_HUB_IN, GET_DESCRIPTOR, dtype << 8, 0, buf)
    except OSError as e:
        raise Refused(f'usb{bus} hub descriptor request failed ({e.strerror})')
    d = bytes(buf[:n])
    # a root hub returns the whole buffer, so bDescLength, not n, says where the descriptor ends
    if (n < 7 or d[1] != dtype or port > d[2] or d[0] not in descriptor_lengths(superspeed, d[2])
            or n < d[0]):
        raise Refused(f'usb{bus} returned a malformed hub descriptor ({d.hex()})')
    mode = LPSM[d[3] & 0x3]
    if mode != 'per-port':
        raise Refused(f'usb{bus} declares {mode}, not per-port: refusing')


def descriptor_lengths(superspeed, ports):
    """Valid bDescLength values. USB 2.0 Table 11-13: 7 fixed bytes, DeviceRemovable (bit 0
    reserved, one bit per port) and PortPwrCtrlMask (one bit per port, padded to a byte);
    Linux root hubs size the mask like DeviceRemovable (xhci-hub.c:293-294)."""
    if superspeed:
        return {SS_HUB_SIZE}
    removable = ports // 8 + 1
    return {7 + removable + (ports + 7) // 8, 7 + 2 * removable}


def run(verb, bus, port, expect=None):
    ioctl = usbdevfs_control()                  # an unsupported ABI refuses before anything is opened
    node, rdev, ino, superspeed = root_hub(bus, port)
    if expect is not None and token(rdev, ino) != expect:
        raise Refused(f'usb{bus} is not the root hub check saw (token {expect}, now {token(rdev, ino)})')
    fd = open_node(node, rdev)
    try:
        if root_hub(bus, port)[1:3] != (rdev, ino):
            raise Refused(f'usb{bus} was replaced while {node} was opened')
        if verb == 'check':
            check_descriptor(fd, ioctl, superspeed, bus, port)
            print(token(rdev, ino))
            print(f'root_port_cycle: usb{bus} port {port} switches per port; node {node}', file=sys.stderr)
            return 0
        request, name = (CLEAR_FEATURE, 'off') if verb == 'off' else (SET_FEATURE, 'on')
        try:
            control(fd, ioctl, RT_PORT, request, PORT_POWER, port)
        except OSError as e:
            print(f'root_port_cycle: power-{name} request to usb{bus} port {port} failed ({e.strerror})',
                  file=sys.stderr)
            return 1
        print(f'root_port_cycle: power-{name} request to usb{bus} port {port} done ({node})')
        return 0
    finally:
        os.close(fd)


def main(argv):
    verbs = {'check': 2, 'off': 3, 'on': 3}
    if not argv or verbs.get(argv[0]) != len(argv) - 1 or not all(NUM_RE.fullmatch(a) for a in argv[1:3]):
        print(__doc__.split('\n\n')[1], file=sys.stderr)
        return 2
    try:
        return run(argv[0], int(argv[1]), int(argv[2]), *argv[3:])
    except Refused as e:
        print(f'root_port_cycle: {argv[0]} refused: {e}; no switching request sent', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
