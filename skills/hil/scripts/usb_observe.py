#!/usr/bin/env python3
"""Bounded Linux USB observation shared by the HIL harnesses: sysfs attribute reads that give
up instead of hanging on a wedged device, the one enumeration walk, and the lookup of a
D-state process holding a usbfs node. Observation only: nothing here resets a device or takes
a lock.
"""
import glob
import os
import threading

# Roots of the scan and the stray-holder lookup; module globals so unit tests can point them at a tree
SYS_USB_DEVICES = '/sys/bus/usb/devices'
PROC_ROOT = '/proc'
SYS_DEV_CHAR = '/sys/dev/char'
UDEV_DATA = '/run/udev/data'

SYSFS_READ_GRACE = 2.0        # default bound on one attribute read; see read_sysfs

# path -> the kernfs inode the node had when its bounded read gave up. Keyed by INODE, not
# by path alone: a busport does not change when a board returns to the same physical port,
# so a path-only blacklist outlives the wedge -- a tool reflashes the board, polls that
# busport for the new inode, and the scan it polls through would never look at the device
# again. A re-enumeration destroys the kernfs node and makes
# a new one, so a CHANGED inode is the all-clear. os.stat is safe on a wedged device: it
# does not call ->show(), so it cannot block on the lock the reader is stuck behind.
_stranded: dict = {}
_strand_hits: dict = {}       # path -> how many times it has stranded, ever
_strand_lock = threading.Lock()

# Each strand costs a thread AND an fd for the life of the process -- on sysfs the open()
# SUCCEEDS and only the read blocks. A device that FLAPS while still wedged re-enumerates,
# clears the inode memo, and strands again; after this many it stays memoised whatever its
# inode says, so one sick board cannot leak without bound.
_PATH_STRAND_MAX = 4


def path_stranded(path: str) -> bool:
    """Whether THIS attribute is currently memoised as unreadable: lets a caller tell a
    device whose `serial` is held under the device lock from one that genuinely left the bus.
    """
    with _strand_lock:
        return path in _stranded


def read_sysfs(path: str, timeout: float = SYSFS_READ_GRACE) -> str | None:
    """A sysfs attribute's value, or None when it did not answer.

    BOUNDED BY DEFAULT, and it has to be. `serial` is served by usb_string_attr, which
    takes usb_lock_device_interruptible (v6.12.96 sysfs.c:141-143) -- the same lock a
    wedged usbfs ioctl holds. Every OTHER attribute the harness reads (idVendor, idProduct,
    bcdDevice, busnum, devnum, speed) is a lock-free sysfs_emit from a cached field and
    cannot block.

    The wedged device's own caller is not the only one that pays, which is why the bound
    is not opt-in: usb_scan reads `serial` on every device matching the VID to find the one
    it wants, so resolving MY device touches every peer's locked attribute, and one wedged
    device would stall every caller that scans its VID.

    A give-up reads as None, the same as unreadable: there is no third value and no
    per-attribute blindness. The memo is keyed by inode so the cost stays on the device
    that is actually wedged; path_stranded() tells a caller which device that was.
    """
    with _strand_lock:
        was = _stranded.get(path)
        stuck_for_good = _strand_hits.get(path, 0) >= _PATH_STRAND_MAX
    if was is not None:
        try:
            if os.stat(path).st_ino == was:
                return None                   # same kernfs node, still wedged
        except OSError:
            pass                              # gone: let the read below report it
        if stuck_for_good:
            return None       # flapped too many times; see _PATH_STRAND_MAX
        with _strand_lock:
            _stranded.pop(path, None)         # a different inode is the all-clear

    # BEFORE the read, not after: a node that re-enumerates DURING the grace would
    # otherwise have its brand-new HEALTHY inode recorded as the wedged one, and only a
    # second re-enumeration could ever clear it. If it cannot be stat'd there is no key to
    # memoise against, so the path is simply re-read next time -- the open fails fast.
    try:
        ino = os.stat(path).st_ino
    except OSError:
        ino = None
    out: dict = {}

    def _read():
        try:
            with open(path) as f:
                out['v'] = f.read().strip()
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    # `out` FIRST: a reader can deposit its value and still be alive for a moment
    # afterwards, and counting that as a strand blacklists a healthy attribute forever
    if t.is_alive() and 'v' not in out:
        if ino is None:
            # the pre-read stat lost a race the open then won -- the node was replaced
            # between them. Re-stat now: the reader is blocked on whatever node exists,
            # so this is the key it is stuck on. Without a key nothing is memoised and
            # every later poll starts another permanent thread and fd for this path.
            try:
                ino = os.stat(path).st_ino
            except OSError:
                pass
        if ino is not None:
            with _strand_lock:
                if path not in _stranded:         # count the PATH once, not each reader
                    _strand_hits[path] = _strand_hits.get(path, 0) + 1
                _stranded[path] = ino
        return None
    return out.get('v')


def usb_scan(vid_pid=None, serial=None, vid=None, timeout=SYSFS_READ_GRACE) -> list:
    """Enumerated USB devices matching the filters: [{busport, dir, vid, pid, serial}].

    Three rules, one implementation for every caller:

    * Root hubs excluded (glob `*-*`): no DUT is one, and scans including them measured
      seconds slower.
    * idVendor/idProduct first: lock-free `sysfs_emit` from udev->descriptor
      (sysfs.c:688-705), so they rule out nearly every device for free.
    * `serial` LAST and BOUNDED: it is the only attribute here served under the device
      lock, so it is the only one that can block. Filtering on the lock-free pair first
      keeps most devices out of it, but a scan for ONE board still reads the serial of
      every peer that shares its VID -- so the bound is what stops one wedged DUT from
      stalling every caller (see read_sysfs).

    A device whose serial is missing or unreadable is not returned.
    """
    out = []
    for d in glob.glob(os.path.join(SYS_USB_DEVICES, '*-*')):
        # `in`, not endswith: an interface is '<busport>:<cfg>.<ifnum>' (2-4:1.0), which
        # CONTAINS the colon rather than ending with it. Screening them out here is worth
        # real time: interfaces are most of the matches.
        if ':' in os.path.basename(d):
            continue
        try:
            with open(os.path.join(d, 'idVendor')) as f:
                dev_vid = f.read().strip()
            with open(os.path.join(d, 'idProduct')) as f:
                dev_pid = f.read().strip()
        except OSError:
            continue          # vanished mid-walk, or not a device dir: a fact, not unknown
        if vid_pid is not None and (dev_vid, dev_pid) != tuple(vid_pid):
            continue          # ruled out for free, without touching the locked attribute
        if vid is not None and dev_vid != vid:
            continue          # same, for callers that know the VID but not the PID
        sn = read_sysfs(os.path.join(d, 'serial'), timeout)
        if sn is None:
            continue          # serial missing or unreadable (wedged, stranded, OSError)
        if serial is not None and sn.lower() != serial.lower():
            continue
        out.append({'busport': os.path.basename(d), 'dir': d,
                    'vid': dev_vid, 'pid': dev_pid, 'serial': sn})
    return out


def _proc_stat(pid) -> tuple | None:
    """(comm, state, starttime) from /proc/<pid>/stat, or None when the process is gone."""
    try:
        with open(f'{PROC_ROOT}/{pid}/stat') as f:
            text = f.read()
    except (OSError, ValueError):
        return None
    # comm is parenthesised and may itself contain ') ', so split at the LAST one
    head, sep, tail = text.rpartition(')')
    fields = tail.split()
    if not sep or len(fields) < 20:
        return None
    return head.partition('(')[2], fields[0], fields[19]   # fields 2, 3 and 22 of proc(5)


def dstate_holders(comm: str) -> list:
    """Processes named `comm` in uninterruptible sleep, with the usbfs node each was handed
    by `-D`: [{'pid', 'start', 'node'}]. Holders without a `-D` node are left out.

    cmdline is world-readable, so a holder running as root under sudo is found too, unless
    /proc is mounted hidepid. Its read is bounded like a sysfs attribute (read_sysfs): it
    takes the target's mmap lock, which a D-state process can hold.
    """
    try:
        pids = [p for p in os.listdir(PROC_ROOT) if p.isdigit()]
    except OSError:
        return []
    out = []
    for pid in pids:
        st = _proc_stat(pid)
        if not st or st[0] != comm or st[1] != 'D':
            continue
        argv = (read_sysfs(f'{PROC_ROOT}/{pid}/cmdline') or '').split('\0')
        nodes = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == '-D']
        if nodes and nodes[-1]:
            out.append({'pid': int(pid), 'start': st[2], 'node': nodes[-1]})
    return out


def proc_holds(holder: dict) -> bool:
    """Whether a dstate_holders() entry is still a live process. A zombie has closed its
    files, and a changed starttime is a recycled pid."""
    st = _proc_stat(holder['pid'])
    return bool(st) and st[2] == holder['start'] and st[1] not in ('Z', 'X')


def usb_node_serial(node: str, timeout: float = SYSFS_READ_GRACE) -> str | None:
    """The serial of the device behind usbfs node /dev/bus/usb/BBB/DDD, as udev recorded it
    when the device was added, else the bounded sysfs read. None when unidentified.

    udev first because the device asked about is usually one a D-state usbfs ioctl holds:
    its `serial` is served under that device lock (read_sysfs), so the read would only give
    up after `timeout` and strand a reader. The node's devt names both records without
    touching the device: usbcore's MKDEV(189, (busnum-1)*128 + devnum-1).

    The udev value is usb_id's ID_SERIAL_SHORT, not the raw sysfs `serial`: whitespace and
    unsafe characters become '_', so it can differ from usb_scan's. A serial with bytes above
    0x7f, control characters or ',' gets none and takes the sysfs read, which equals usb_scan's
    but can time out on a wedged device.
    """
    bus, _, dev = node.removeprefix('/dev/bus/usb/').partition('/')
    if not (bus.isdigit() and dev.isdigit()):
        return None
    devt = f'189:{(int(bus) - 1) * 128 + int(dev) - 1}'
    try:
        with open(os.path.join(UDEV_DATA, f'c{devt}')) as f:
            for line in f:
                if line.startswith('E:ID_SERIAL_SHORT='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return read_sysfs(os.path.join(SYS_DEV_CHAR, devt, 'serial'), timeout)
