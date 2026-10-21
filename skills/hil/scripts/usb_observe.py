#!/usr/bin/env python3
"""Bounded Linux USB observation shared by the HIL harnesses: sysfs attribute reads that give
up instead of hanging on a wedged device, the one enumeration walk, and the lookup of a
D-state process holding a usbfs node. Observation only: nothing here resets a device or takes
a lock.
"""
import glob
import os
import re
import threading

FILES = ()   # loads and launches nothing by path (hil_load.py)

# Roots of the scan and the stray-holder lookup; module globals so unit tests can point them at a tree
SYS_USB_DEVICES = '/sys/bus/usb/devices'
PROC_ROOT = '/proc'
SYS_DEV_CHAR = '/sys/dev/char'
UDEV_DATA = '/run/udev/data'

SYSFS_READ_GRACE = 2.0        # default bound on one attribute read; see read_sysfs

# a device's bus-port as sysfs names it (5-3.4); `$` still lets .match() accept one trailing newline
BUSPORT_RE = re.compile(r'^[0-9]+-[0-9]+(\.[0-9]+)*$')

# Both memos are keyed by the attribute's realpath: usb_scan reads `serial` through
# /sys/bus/usb/devices/<busport> and usb_node_serial through /sys/dev/char/189:N, and keyed by
# spelling one wedged device would strand once per spelling.
#
# key -> the reader given up on. Keyed by INODE too, not by path alone: a busport does not
# change when a board returns to the same physical port, so a path-only blacklist outlives
# the wedge -- a tool reflashes the board, polls that busport for the new inode, and the scan
# it polls through would never look at the device again. A re-enumeration destroys the
# kernfs node and makes a new one, so a CHANGED inode is the all-clear; so is the stranded
# reader finishing (a USBDEVFS_RESET that keeps the usb_device, a D-state holder that exits).
# os.stat is safe on a wedged device: it does not call ->show(), so it cannot block on the
# lock the reader is stuck behind.
_stranded: dict = {}
_inflight: dict = {}          # key -> the reader still within its grace; concurrent callers share it
_live: dict = {}              # key -> readers started and not yet returned
_spelling: dict = {}          # a non-canonical path -> its key, while that key has a reader or a strand
_strand_lock = threading.Lock()

# Each reader costs a thread AND an fd until the read returns -- on sysfs the open()
# SUCCEEDS and only the read blocks. A device that FLAPS while still wedged re-enumerates,
# clears the inode memo, and strands again; with this many readers outstanding no new one
# starts whatever its inode says, so one sick board cannot leak without bound.
_PATH_STRAND_MAX = 4


class _Reader:
    """One bounded read of one attribute node. `done` is set under _strand_lock, so a
    caller that gives up and a reader that finishes cannot both win."""
    __slots__ = ('ino', 'value', 'done')

    def __init__(self, ino):
        self.ino = ino
        self.value = None
        self.done = threading.Event()


def _key(path: str) -> str:
    return os.path.realpath(path)


def path_stranded(path: str) -> bool:
    """Whether THIS attribute is currently memoised as unreadable: lets a caller tell a
    device whose `serial` is held under the device lock from one that genuinely left the bus.
    """
    key = _key(path)
    with _strand_lock:
        if not os.path.lexists(path):
            # an alias that vanished while its reader is stuck no longer resolves to the key
            key = _spelling.get(path, key)
        # a capped key reads as unreadable even while its readers are still in their grace
        return key in _stranded or _live.get(key, 0) >= _PATH_STRAND_MAX


def _read(key: str, r: _Reader, path: str) -> None:
    try:
        with open(path) as f:
            value = f.read().strip()
    except (OSError, ValueError):
        value = None
    with _strand_lock:
        r.value = value
        r.done.set()
        # only its own entries: a newer reader of a re-enumerated node owns the key now
        if _inflight.get(key) is r:
            del _inflight[key]
        if _stranded.get(key) is r:
            del _stranded[key]
        _live[key] -= 1
        if not _live[key]:
            del _live[key]
            if key not in _stranded:
                for spelling in [s for s, k in _spelling.items() if k == key]:
                    del _spelling[spelling]


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
    key = _key(path)
    # BEFORE the read, not after: a node that re-enumerates DURING the grace would
    # otherwise have its brand-new HEALTHY inode recorded as the wedged one, and only a
    # second re-enumeration could ever clear it.
    try:
        ino = os.stat(path).st_ino
    except OSError:
        ino = None
    with _strand_lock:
        if path != key:
            _spelling[path] = key
        stuck = _stranded.get(key)
        if stuck is not None and ino == stuck.ino:
            return None                       # same kernfs node, still wedged
        r = _inflight.get(key)
        # share the reader in its grace unless both inodes are known and differ: a reader
        # whose stat failed may be on this very node
        mine = r is None or (ino is not None and r.ino is not None and r.ino != ino)
        if mine and _live.get(key, 0) >= _PATH_STRAND_MAX:
            return None                       # flapped too many times; the strand stays shown
        if stuck is not None:
            del _stranded[key]                # a different inode is the all-clear
        if mine:
            r = _Reader(ino)
            _inflight[key] = r
            _live[key] = _live.get(key, 0) + 1
    if mine:
        threading.Thread(target=_read, args=(key, r, path), daemon=True).start()
    if r.done.wait(timeout):
        return r.value
    if r.ino is None:
        # the pre-read stat lost a race the open then won -- the node was replaced
        # between them. Re-stat now: the reader is blocked on whatever node exists,
        # so this is the key it is stuck on. Without a key nothing is memoised and
        # every later poll starts another thread and fd for this path.
        try:
            ino = os.stat(path).st_ino
        except OSError:
            ino = None
    with _strand_lock:
        if r.done.is_set():
            return r.value                    # finished between the wait and the lock
        if r.ino is None:
            r.ino = ino
        if _inflight.get(key) is r:
            del _inflight[key]
            if r.ino is not None:
                _stranded[key] = r
    return None


def _free_attr(d: str, name: str) -> str | None:
    """A lock-free attribute's value, or None when absent, unreadable or empty."""
    try:
        with open(os.path.join(d, name)) as f:
            return f.read().strip() or None
    except (OSError, ValueError):
        return None


def _int_or_none(text: str | None) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def enumeration_id(d: str) -> int | None:
    """Device dir `d`'s inode, or None when it is gone. kernfs numbers every node it creates
    afresh for the boot (v6.12 fs/kernfs/dir.c:630-640, the inode from it at inode.c:251),
    so a re-enumeration at the same busport changes it even when the bus hands out the same
    devnum again (hub.c choose_devnum wraps at 128). os.stat never calls ->show(), so it
    cannot block on a wedged device."""
    try:
        return os.stat(d).st_ino
    except OSError:
        return None


def usb_devices(vid_pid=None, vid=None) -> list:
    """usb_scan's walk without its one locked read: [{busport, dir, ino, vid, pid, busnum,
    devnum, speed, bcdDevice}] for every enumerated device matching the filters, `serial`
    never read, so it cannot block. `ino` is the enumeration_id the attributes were read
    under; busnum and devnum are ints. Any of the last four that is absent or malformed is
    None, and never drops the device."""
    out = []
    for d in glob.glob(os.path.join(SYS_USB_DEVICES, '*-*')):
        # `in`, not endswith: an interface is '<busport>:<cfg>.<ifnum>' (2-4:1.0), which
        # CONTAINS the colon rather than ending with it. Screening them out here is worth
        # real time: interfaces are most of the matches.
        if ':' in os.path.basename(d):
            continue
        ino = enumeration_id(d)       # before the attributes, so a later change shows
        if ino is None:
            continue
        dev_vid, dev_pid = _free_attr(d, 'idVendor'), _free_attr(d, 'idProduct')
        if dev_vid is None or dev_pid is None:
            continue          # vanished mid-walk, or not a device dir: a fact, not unknown
        if vid_pid is not None and (dev_vid, dev_pid) != tuple(vid_pid):
            continue          # ruled out for free, without touching the locked attribute
        if vid is not None and dev_vid != vid:
            continue          # same, for callers that know the VID but not the PID
        # lock-free sysfs_emit too (v6.12 sysfs.c:145-217 speed/busnum/devnum, :705 bcdDevice)
        out.append({'busport': os.path.basename(d), 'dir': d, 'ino': ino, 'vid': dev_vid, 'pid': dev_pid,
                    'busnum': _int_or_none(_free_attr(d, 'busnum')),
                    'devnum': _int_or_none(_free_attr(d, 'devnum')),
                    'speed': _free_attr(d, 'speed'), 'bcdDevice': _free_attr(d, 'bcdDevice')})
    return out


def usb_scan(vid_pid=None, serial=None, vid=None, timeout=SYSFS_READ_GRACE) -> list:
    """Enumerated USB devices matching the filters: usb_devices' entries, each with its
    `serial` added.

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

    A device whose serial is missing or unreadable, or that re-enumerated while it was read,
    is not returned; the next scan sees the new enumeration.
    """
    out = []
    for dev in usb_devices(vid_pid, vid):
        sn = read_sysfs(os.path.join(dev['dir'], 'serial'), timeout)
        if sn is None:
            continue          # serial missing or unreadable (wedged, stranded, OSError)
        if enumeration_id(dev['dir']) != dev['ino']:
            continue          # re-enumerated across the read: this serial is not the entry's device
        if serial is not None and sn.lower() != serial.lower():
            continue
        out.append({**dev, 'serial': sn})
    return out


def jlink_serial_match(uid: str, serial: str) -> str | None:
    """'exact' when a probe's serial is the roster uid (case aside), 'padded' when only J-Link's zero
    padding of an all-digit serial separates them (681295394 is 000681295394), else None. A uid
    of zeros alone never matches by padding: a placeholder serial must not stand for a device."""
    u, s = uid.lower(), serial.lower()
    if u == s:
        return 'exact'
    stripped = u.lstrip('0')
    return 'padded' if u.isdigit() and stripped and s.isdigit() and s.lstrip('0') == stripped else None


def jlink_serial_candidates(uid: str, serials: dict) -> list:
    """The sorted keys of serials ({key: serial}) that are uid's probe, the exact matches alone
    when there are any."""
    tiers = {k: jlink_serial_match(uid, s) for k, s in serials.items()}
    best = 'exact' if 'exact' in tiers.values() else 'padded'
    return sorted(k for k, t in tiers.items() if t == best)


def _proc_stat(pid) -> tuple | None:
    """(comm, state, starttime, ppid, pgrp) from /proc/<pid>/stat, or None when the process
    is gone."""
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
    # fields 2, 3, 22, 4 and 5 of proc(5)
    return head.partition('(')[2], fields[0], fields[19], fields[1], fields[2]


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


def udev_serial(bus: int, dev: int) -> str | None:
    """usb_id's ID_SERIAL_SHORT for the device at busnum/devnum, as udev recorded it when the
    device was added, read without touching the device; None when udev has none. usb_id
    drops a serial with a byte outside 0x20-0x7f or a ',', collapses whitespace runs into
    one '_' and turns characters outside [A-Za-z0-9#+-.:=@_] into '_'. The devt names the
    record: usbcore's MKDEV(189, (busnum-1)*128 + devnum-1)."""
    try:
        with open(os.path.join(UDEV_DATA, f'c189:{(bus - 1) * 128 + dev - 1}')) as f:
            for line in f:
                if line.startswith('E:ID_SERIAL_SHORT='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return None


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
    serial = udev_serial(int(bus), int(dev))
    if serial is not None:
        return serial
    devt = f'189:{(int(bus) - 1) * 128 + int(dev) - 1}'
    return read_sysfs(os.path.join(SYS_DEV_CHAR, devt, 'serial'), timeout)
