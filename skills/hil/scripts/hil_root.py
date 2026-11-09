#!/usr/bin/python3 -I
"""The HIL harnesses' root operations, one fixed verb each (`hil` skill). Linux only, stdlib
only, and it imports nothing beside it.

A rig may grant its CI user sudo on an installed copy of this file (INSTALLED), so it takes
no path, command or environment from its caller (only SUDO_UID, which sudo sets itself): each verb
checks its arguments against the one sysfs file, device or program it touches. Recovery's
usb_recover is a separate grant.

  hil_root.py pattern 0|1                usbtest's pattern parameter
  hil_root.py modprobe                   modprobe usbtest
  hil_root.py dmesg                      the kernel log lines naming usbtest
  hil_root.py reset-tt BUSPORT           Reset_TT on the multi-TT hub port BUSPORT names
  hil_root.py netns BUSPORT PID          move the USB network interface of BUSPORT's device, a
                                         board (NETNS_VIDS), into PID's network namespace, one
                                         owned by a user namespace a user other than root created,
                                         the one SUDO_UID names when set; prints its ifindex, the
                                         same in that namespace, and its name as checked

Exit 2 refuses a request; otherwise the operation's own status, its error on stderr.
Callers build the argv with `argv()`.
"""

from __future__ import annotations

import fcntl
import os
import re
import socket
import stat
import struct
import subprocess
import sys
from pathlib import Path

FILES = ()                                  # it loads and launches nothing by path
INSTALLED = '/usr/local/sbin/hil_root'      # a rig's root-owned copy, the CI user's sudo grant
SYS_USB = Path('/sys/bus/usb/devices')
PROC = Path('/proc')
PATTERN_PARAM = Path('/sys/module/usbtest/parameters/pattern')
DEV_USB = '/dev/bus/usb'
DMESG, MODPROBE = '/usr/bin/dmesg', '/usr/sbin/modprobe'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}   # fixed, not the caller's: modprobe takes options from MODPROBE_OPTIONS
USB_MAJOR = 189                               # usb_device char major (v6.12 include/linux/usb.h)

_BUSPORT = re.compile(r'[1-9][0-9]*-[1-9][0-9]*(\.[1-9][0-9]*)*\Z')
_CTRLTRANSFER = struct.Struct('@BBHHHIP')                   # struct usbdevfs_ctrltransfer
USBDEVFS_CONTROL = 0xC0005500 | _CTRLTRANSFER.size << 16   # _IOWR('U', 0, ...)
RESET_TT = 9

# A board's USB network function, never a host NIC: a board's vendor id and one of these class
# drivers. The id is the boundary because the caller cannot change a host NIC's (a host cdc_ether
# or cdc_ncm adapter, a tethered phone), and a board claiming one moves only itself. A MAC is not:
# hosts clone or randomize locally administered ones. 0xcafe: the TinyUSB examples' test VID.
NETNS_VIDS = ('cafe',)
NET_DRIVERS = ('cdc_ether', 'cdc_ncm', 'cdc_eem', 'rndis_host')
NS_GET_USERNS = 0xb701                  # _IO(0xb7, 0x1), v6.12 include/uapi/linux/nsfs.h
NS_GET_OWNER_UID = 0xb704               # _IO(0xb7, 0x4): a user namespace's creator uid (uid_t)
RTM_NEWLINK, NLMSG_ERROR = 16, 2                # v6.12 include/uapi/linux/rtnetlink.h, netlink.h
IFLA_NET_NS_FD = 28                             # v6.12 include/uapi/linux/if_link.h
IFLA_NEW_IFINDEX = 49                           # if_link.h; a move honours it since v5.13
NLM_F_REQUEST_ACK = 0x1 | 0x4
_NLMSGHDR = struct.Struct('=IHHII')                         # len, type, flags, seq, pid
_IFINFOMSG = struct.Struct('=BxHiII')                       # family, type, index, flags, change


class Refused(Exception):
    pass


def present(path) -> bool:
    """False only when nothing is at `path`: a broken, unreadable or odd entry still counts, so
    a rig's installed copy is never bypassed for a checkout."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def argv(verb, *args) -> list:
    """The command running one verb: this file as root; otherwise the installed copy under
    sudo when the host has one -- then never a checkout's, even when that entry is broken --
    else this file under sudo."""
    if os.geteuid() == 0:
        return [sys.executable, '-I', os.path.abspath(__file__), verb, *args]
    if present(INSTALLED):
        return ['sudo', '-n', INSTALLED, verb, *args]
    return ['sudo', '-n', sys.executable, '-I', os.path.abspath(__file__), verb, *args]


def _read(path):
    return Path(path).read_text().strip()


def _write(path, data):
    with open(path, 'w') as f:
        f.write(data)


def pattern(value):
    if value not in ('0', '1'):     # usbtest's pattern param: 0 zeroes, 1 mod63 (v6.12 usbtest.c)
        raise Refused('pattern takes 0 or 1')
    _write(PATTERN_PARAM, value)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', env=ENV)
    sys.stderr.write(r.stderr)
    return r


def modprobe():
    return _run([MODPROBE, 'usbtest']).returncode


def dmesg():
    r = _run([DMESG])
    sys.stdout.write(''.join(l for l in r.stdout.splitlines(True) if 'usbtest' in l))
    return r.returncode


def tt_hub(busport):
    """(hub busport, port, busnum, devnum, (idVendor, idProduct)) of the hub port whose own TT
    would serve a full-/low-speed device at `busport`: a hub running one TT per port (USB 2.0
    11.23.1), whose Reset_TT (11.24.2.9) disturbs no other port. Refused, naming why, for
    anything else. The device's speed is the caller's: a wedged TT is often why it is gone.
    Sysfs reads only: hil_tt asks it unprivileged, reset-tt again as root."""
    if not _BUSPORT.match(busport) or '.' not in busport:
        raise Refused(f'busport {busport!r}: expected BUS-PORT.PORT[...] behind a hub')
    hub, _, port = busport.rpartition('.')
    port = int(port)
    try:
        # sysfs lists only the active configuration's interfaces; the multi-TT one is protocol 02
        intf = list((SYS_USB / hub).glob(f'{hub}:*.0'))
        if len(intf) != 1 or _read(intf[0] / 'bInterfaceProtocol') != '02':
            raise Refused(f'hub {hub} is not running one TT per port')
        if port > int(_read(SYS_USB / hub / 'maxchild')):
            raise Refused(f'hub {hub} has no port {port}')
        busnum, devnum = int(_read(SYS_USB / hub / 'busnum')), int(_read(SYS_USB / hub / 'devnum'))
        ids = (int(_read(SYS_USB / hub / 'idVendor'), 16), int(_read(SYS_USB / hub / 'idProduct'), 16))
    except (OSError, ValueError) as e:
        raise Refused(f'{busport}: {getattr(e, "strerror", None) or e}')
    return hub, port, busnum, devnum, ids


def reset_tt(busport):
    """Reset_TT on the hub port tt_hub() derives itself; the node it opens must be that hub."""
    hub, port, busnum, devnum, ids = tt_hub(busport)
    node = f'{DEV_USB}/{busnum:03d}/{devnum:03d}'
    fd = os.open(node, os.O_RDWR | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        # usbdev minors: (busnum - 1) * 128 + (devnum - 1) (v6.12 drivers/usb/core/devio.c)
        if not stat.S_ISCHR(st.st_mode) or (os.major(st.st_rdev), os.minor(st.st_rdev)) != \
                (USB_MAJOR, (busnum - 1) * 128 + devnum - 1):
            raise Refused(f'{node} is not hub {hub}\'s usbfs node')
        desc = os.read(fd, 18)       # usbfs reads begin with the device descriptor
        if len(desc) < 18 or desc[4] != 9 or struct.unpack_from('<HH', desc, 8) != ids:
            raise Refused(f'{node} is not hub {hub}')
        # bmRequestType 0x23: class request to a hub port
        fcntl.ioctl(fd, USBDEVFS_CONTROL, _CTRLTRANSFER.pack(0x23, RESET_TT, 0, port, 0, 1000, 0))
    finally:
        os.close(fd)


def usb_netdev(busport):
    """(ifindex, name) of the one network interface BUSPORT's device has, refused unless the device
    is a board (NETNS_VIDS) and a USB network class driver (NET_DRIVERS) drives it. The ifindex is
    read before the checks and again after: a device re-enumerated meanwhile gets a new one, so the
    one returned is the one checked. The name is not checked again: a rename (udev's) may change it."""
    if not _BUSPORT.match(busport):
        raise Refused(f'busport {busport!r}: expected BUS-PORT[.PORT...]')
    try:
        found = list((SYS_USB / busport).glob(f'{busport}:*/net/*'))
        if len(found) != 1:
            raise Refused(f'{busport}: expected one network interface, found {sorted(p.name for p in found)}')
        net = found[0]
        ifindex = int(_read(net / 'ifindex'))
        vid = _read(SYS_USB / busport / 'idVendor')
        if vid not in NETNS_VIDS:
            raise Refused(f'{busport}: vendor id {vid} is not a board\'s ({", ".join(NETNS_VIDS)})')
        driver = os.path.basename(os.readlink(net.parent.parent / 'driver'))
        if driver not in NET_DRIVERS:
            raise Refused(f'{busport}: {net.name} is driven by {driver}, not a USB network function')
        if int(_read(net / 'ifindex')) != ifindex:
            raise Refused(f'{busport}: {net.name} changed while it was checked')
        return ifindex, net.name
    except (OSError, ValueError) as e:
        raise Refused(f'{busport}: {getattr(e, "strerror", None) or e}')


def sandbox_netns(pid):
    """An fd on PID's network namespace, refused unless a user namespace other than the initial
    one owns it and a user other than root created that one -- the one SUDO_UID names when set:
    an unprivileged process's sandbox, never the host's or a root container's. Held open, so a PID
    reused after this check cannot redirect the move."""
    if not pid.isdecimal() or int(pid) < 1:
        raise Refused(f'pid {pid!r}: expected a process id')
    if int(pid) == 1:
        raise Refused('pid 1 is the host\'s init')
    try:
        fd = os.open(PROC / pid / 'ns' / 'net', os.O_RDONLY)
    except OSError as e:
        raise Refused(f'pid {pid}: {e.strerror or e}')
    try:
        owner = fcntl.ioctl(fd, NS_GET_USERNS)
        try:
            st, init = os.fstat(owner), os.stat(PROC / '1' / 'ns' / 'user')
            uid, = struct.unpack('=I', fcntl.ioctl(owner, NS_GET_OWNER_UID, bytes(4)))
        finally:
            os.close(owner)
        if (st.st_dev, st.st_ino) == (init.st_dev, init.st_ino):
            raise Refused(f'pid {pid}: its network namespace is not owned by an unprivileged user namespace')
        if uid == 0:
            raise Refused(f'pid {pid}: its user namespace was created by root')
        caller = os.environ.get('SUDO_UID')
        if caller is not None and str(uid) != caller:
            raise Refused(f'pid {pid}: its user namespace was created by uid {uid}, not SUDO_UID {caller}')
    except BaseException:
        os.close(fd)
        raise
    return fd


def move_link(ifindex, nsfd):
    """RTM_NEWLINK moving ifindex into the namespace nsfd names, by index: a device that
    re-enumerated since has a new one, so a stale index fails rather than moving its successor.
    It keeps that index there or the move fails (EBUSY): unasked, the kernel renumbers on a clash."""
    attr = struct.pack('=HHI', 8, IFLA_NET_NS_FD, nsfd) + struct.pack('=HHi', 8, IFLA_NEW_IFINDEX, ifindex)
    body = _IFINFOMSG.pack(socket.AF_UNSPEC, 0, ifindex, 0, 0) + attr
    msg = _NLMSGHDR.pack(_NLMSGHDR.size + len(body), RTM_NEWLINK, NLM_F_REQUEST_ACK, 1, 0) + body
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as s:
        s.settimeout(5)
        s.send(msg)
        reply = s.recv(4096)
    if len(reply) < _NLMSGHDR.size + 4:
        raise OSError(f'short netlink reply ({len(reply)} bytes)')
    _, kind, _, seq, _ = _NLMSGHDR.unpack_from(reply)
    if kind != NLMSG_ERROR or seq != 1:
        raise OSError(f'unexpected netlink reply type {kind} seq {seq}')
    err, = struct.unpack_from('=i', reply, _NLMSGHDR.size)
    if err:
        raise OSError(-err, os.strerror(-err))


def netns(busport, pid):
    ifindex, name = usb_netdev(busport)
    fd = sandbox_netns(pid)
    try:
        move_link(ifindex, fd)
    finally:
        os.close(fd)
    print(ifindex, name)


VERBS = {'pattern': pattern, 'modprobe': modprobe, 'dmesg': dmesg, 'reset-tt': reset_tt, 'netns': netns}


def main(args) -> int:
    fn = VERBS.get(args[0]) if args else None
    if fn is None:
        print(f'usage: hil_root.py {"|".join(VERBS)} ARG...', file=sys.stderr)
        return 2
    if len(args) - 1 != fn.__code__.co_argcount:
        print(f'{args[0]} takes {fn.__code__.co_argcount} argument(s)', file=sys.stderr)
        return 2
    try:
        return fn(*args[1:]) or 0
    except Refused as e:
        print(f'hil_root: refused: {e}', file=sys.stderr)
        return 2
    except OSError as e:
        print(f'hil_root: {args[0]}: {e.strerror or e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
