#!/usr/bin/env python3
"""Host-wide reservation for a root-port, hub-port or controller operation on a HIL host (`hil`
skill, Board locks): every board lock of every supplied roster, taken by one process around the
one supported mutation, after a coverage check of what the operation reaches.

  hil_reserve.py check <busport> [--scope root|hub] --roster FILE ...
  hil_reserve.py check <pciaddr> --scope controller --roster FILE ...
  hil_reserve.py run root-cycle|hub-cycle <busport> --roster FILE ... --reason TEXT
  hil_reserve.py run pci-rebind <pciaddr> --roster FILE ... --reason TEXT
  hil_reserve.py run pci-bind <pciaddr> [--driver NAME] --roster FILE ... --reason TEXT

A roster is a project's HIL roster JSON (`boards` and `boards-skip`; a parked board is still
on the hub). An entry with `probe` locks as its registered board's name, any other as its
own `name` (hil_boards.lock_name). Identities are every `uid`, `uart_uid` and `serial` string and
every value of a `uids` map (the serial each image enumerates with) in an entry outside its
`dev_attached` lists (devices on the board's own USB host port, which this host never sees, some
with serials as weak as "0"), compared in the form udev's
usb_id gives a serial (whitespace runs and unsafe characters as `_`), case-insensitively; two
roster identities that become one that way are refused. The contract's Hosts/rosters is the roster list: the check refuses what
the rosters do not explain, it never infers an inventory.

check (read-only, exit 0 covered / 1 refused / 2 usage): walks the device directories under
the busport's root port (`--scope hub`: its parent hub, which hub-cycle uses) twice within
one budget. A node's identity is its sysfs `serial`,
or, when that read does not answer (a wedged device serves it under the lock the wedge
holds), udev's record of it (`ID_SERIAL_SHORT`, written when the device was added and read
without touching it). A node is covered when its class is readable and its identity matches
a roster identity, or when it is a hub (bDeviceClass 09) whose children are all covered. An
empty hub, an unreadable class, a node without an identity, an unmatched identity, a tree
that differs between the walks, or a walk over budget refuses, naming each.

run: check (hub-cycle: under the parent hub, and the target must not be a hub), take every
lock (all-or-nothing), check again, run
`sudo -n <usb-kernel-recover>/scripts/usb_recover.sh <verb> <busport> <serial>` (the rig's
/usr/local/sbin/usb_recover when installed) with
the target's sysfs serial, or its udev one when sysfs did not answer (the script then warns
and proceeds, as its own guard says), then validate: the command exited 0, no descendant
survives (this process is a child subreaper, so orphans reparent to it), and the subtree is
back: every pre-operation node with its sysfs serial readable and matching its identity (a
udev record is admission evidence, never restoration evidence), nothing new, all of it
covered. That restoration check repeats twice a second until it holds or one budget has
passed since the command ended, every read bounded by that same deadline, since siblings
re-enumerate one after another after the target. Only a restored subtree releases the
locks cleanly, and only restoration gaps may clear by waiting: a non-zero exit, a survivor or
a signal stays a blocker, except that an exit 3 (the script refused before its first
mutation) with an unchanged subtree, no survivor and no noted signal releases and refuses.
Any other outcome is a blocker that ends with this run (agentrc#464): the BLOCKER line names
the problems, hil_lock.end_failed logs them in every board's cleanup log, which the next task
on each board reports, and every lock is released. A survivor is named, not waited for; it
reparents to init once this process exits, and nothing can recheck it, so its logged failure
stays open until an operator acknowledges it (`hil_lock.py resolve`). A reservation killed
outright leaves its records, which the next task on each board logs. Exit 0 released, 1
blocker or refused, 2 usage.

Controller (pci-rebind, pci-bind; agentrc#236): the target is a PCI function, refused unless it
is a USB controller (class 0x0c03) whose vendor, device, subsystem ids and class read, with a
driver bound for pci-rebind and none for pci-bind. The walk spans every bus it owns: the usbN
root hubs in its PCI directory, covered by the controller itself, and every device on those
buses, under the same coverage rules. An unbind/bind renumbers those buses (17 and 18 came back
as 1 and 2), so each node is keyed by its root hub's speed and its port path (`480M-2.7`), and
two root hubs of one speed, or one whose speed does not read, refuse. A dead controller may show
few devices or none, so its walk cannot reveal a missing roster: the hold is every roster's locks
plus every lock name already in the lock dir, taken under hil_lock.close_admission: while the
run lasts no new lock file is created by a taker running that hil_lock (hil_lock.AdmissionClosed,
a BoardLocked a board task waits out), so the inventory is complete for those. A taker on an
older hil_lock, a consumer pinned before it, can still create a name: the recheck under the hold
refuses one it sees, and one created after the recheck goes unheld, so every consumer on the host
must be pinned at or after this agentrc before a controller operation runs.
`sudo -n <usb_recover> <verb> <pciaddr> [driver]` (pci-bind's `--driver`, else the helper tries
xhci-pci-renesas, then xhci_hcd) is never signalled: SIGINT/SIGTERM/SIGHUP during it are
noted and ignored, since a rebind cut between its unbind and bind strands the controller, and
one still running after CONTROLLER_COMMAND_BUDGET s (a write stuck in the kernel) is left
running as a survivor while the run goes on to its outcome. Restored means the same PCI ids, a
driver bound (pci-rebind: the one bound before), a bus registered, every pre-operation node back
under its key with its sysfs serial matching, and everything now on its buses covered; a node
new since is no gap when covered, since a dead controller's fixtures return with it. When
nothing but root hubs was on its buses before (pci-bind always), its buses must also look the
same for CONTROLLER_SETTLE s, since the hub driver scans its ports after the bind returns. The output
maps each bus's number before to its number now: re-derive every busport on them. The rest
(exit 3, blockers, cleanup logs, exit codes) is as for the port operations. Prerequisite: a
rig's root-owned /usr/local/sbin/usb_recover, which wins over the checkout's, must be refreshed
(hil-farm-setup) from an agentrc with exit 3 for pci refusals; an older copy exits 1 for a
refusal before the unbind, which ends as a blocker on every board, naming that helper.

Signals: SIGINT/SIGTERM/SIGHUP before the command release the untouched reservation and
exit 130; during a port command SIGINT/SIGTERM are forwarded to its process group (a
controller command's never are, above), while SIGHUP (a lost terminal or SSH session, not a
request to abort) is noted and ignored so the mutation completes; afterwards all three are
noted and ignored, so the reconciliation completes and its outcome is logged. The command
starts with no signal blocked, so its own traps see what is forwarded. A command that cannot be
started at all releases the reservation and refuses.
"""
import argparse
import ctypes
import glob
import importlib.util
import json
import os
import re
import secrets
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/hil_lock.py', 'skills/hil/scripts/hil_process.py',
         'skills/hil/scripts/usb_observe.py', 'skills/usb-kernel-recover/scripts/usb_recover.sh',
         'skills/usb-kernel-recover/scripts/root_port_cycle.py', 'skills/hil/scripts/hil_root.py',
         'skills/hil/scripts/hil_boards.py', 'skills/hil/boards.json')


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
hil_lock = hil_load.load_file('skills/hil/scripts/hil_lock.py')
uo = hil_load.load_file('skills/hil/scripts/usb_observe.py')
hil_root = hil_load.load_file('skills/hil/scripts/hil_root.py')
hil_boards = hil_load.load_file('skills/hil/scripts/hil_boards.py')

LOCK_NAME_RE = hil_boards.LOCK_NAME_RE
UDEV_UNSAFE_RE = re.compile(r'[^A-Za-z0-9#+.:=@_-]')   # what usb_id turns into '_' after whitespace runs
IDENTITY_KEYS = ('uid', 'uart_uid', 'serial')
IMAGE_UIDS = 'uids'       # {image: the serial it enumerates with}: each value is the DUT's identity (arduino_hil)
HUB_CLASS = '09'
VERB_SCOPE = {'root-cycle': 'root', 'hub-cycle': 'hub', 'pci-rebind': 'controller', 'pci-bind': 'controller'}
SCOPE_NAME = {'root': 'root port', 'hub': 'hub', 'controller': 'controller'}
REFUSED_UNWRITTEN = 3     # usb_recover.sh, every verb run here: refused before its first mutation
SYS_PCI_DEVICES = '/sys/bus/pci/devices'
PCI_RE = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]$')   # usb_recover.sh's PCI_RE
PCI_IDENTITY = ('vendor', 'device', 'subsystem_vendor', 'subsystem_device', 'class')
USB_CONTROLLER_CLASS = '0x0c03'   # base class 0x0c serial bus, subclass 0x03 USB, as usb_recover.sh requires
ROOT_HUB_RE = re.compile(r'^usb([0-9]+)$')
CONTROLLER_COMMAND_BUDGET = 120.0   # s before a controller command is left running as a survivor
# s a controller's buses must look the same when no device was on them before: hub_activate(HUB_INIT)
# scans its ports from delayed work after the bind returns, and each connect debounces for up to 2 s
# (hub.c HUB_DEBOUNCE_TIMEOUT) before its device appears
CONTROLLER_SETTLE = 5.0
DRIVER_RE = re.compile(r'^[A-Za-z0-9_-]+$')     # usb_recover.sh's DRIVER_RE
DEFAULT_BUDGET = 60.0
PROGRESS_EVERY = 60.0
WAIT_POLL = 0.05           # s between looks at whether the command has ended
RESTORE_POLL = 0.5         # between restoration looks; a node back later than this before the deadline is missed
# the one command `run` builds; a test points this at a stub through the module, never the CLI.
# A rig's root-owned copy, pinned with the rig, wins over this checkout's, even a broken entry (hil_root.present).
INSTALLED_RECOVER = '/usr/local/sbin/usb_recover'
RECOVER_SCRIPT = INSTALLED_RECOVER if hil_root.present(INSTALLED_RECOVER) else \
    os.path.normpath(os.path.join(HERE, '..', '..', 'usb-kernel-recover', 'scripts', 'usb_recover.sh'))
PR_SET_CHILD_SUBREAPER = 36
HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
SPAWN_SIGDEF = (signal.SIGPIPE, signal.SIGXFSZ)     # what Python ignores and Popen restores in a child


class Refused(Exception):
    pass


class OverBudget(Refused):
    """A walk that ran out of its deadline; the only refusal a restoration look does not retry."""


def identity(serial):
    """A serial in the form udev's usb_id records it (systemd udev-util.c udev_replace_whitespace
    and udev_replace_chars), lower-cased, so a sysfs serial and its udev record compare equal."""
    return UDEV_UNSAFE_RE.sub('_', re.sub(r'\s+', '_', serial.strip())).lower()


# --- rosters -------------------------------------------------------------------------------
def _identities(obj, out):
    """The raw identity strings of an entry, stripped and lower-cased."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in IDENTITY_KEYS and isinstance(v, str) and v.strip():
                out.add(v.strip().lower())
            elif k == IMAGE_UIDS and isinstance(v, dict):
                out.update(s.strip().lower() for s in v.values() if isinstance(s, str) and s.strip())
            elif k != 'dev_attached':
                _identities(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _identities(v, out)


def load_rosters(paths):
    """[roster file] -> {'locks': sorted lock names, 'identities': {identity: {locks}}}.
    Each entry is resolved through agentrc's board registry (hil_boards), so its probe serial is an
    identity too, and a registered board's lock is the registry's. A DUT uid, any image's `uids`
    serial included, under two lock names is a conflict; any other identity may serve several."""
    locks, ids, duts, raw_of = set(), {}, {}, {}
    for path in paths:
        try:
            with open(path) as f:
                roster = json.load(f)
        except (OSError, ValueError) as e:
            raise Refused(f'roster {path}: {e}')
        entries = [e for key in ('boards', 'boards-skip') for e in roster.get(key, [])] if isinstance(roster, dict) else []
        if not entries:
            raise Refused(f'roster {path}: no boards')
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get('name'), str) or not e['name']:
                raise Refused(f'roster {path}: an entry without a name')
            try:
                lock = hil_boards.lock_name(e)
                if not LOCK_NAME_RE.match(lock):
                    raise Refused(f'roster {path}: {e["name"]}: lock name {lock!r} is not a plain file name')
                e = hil_boards.resolve(e, roster=path)
            except ValueError as err:
                raise Refused(f'roster {path}: {err}')
            locks.add(lock)
            dut = set()
            _identities({'uid': e['uid'], IMAGE_UIDS: e.get(IMAGE_UIDS)}, dut)
            for uid in sorted(dut):
                if duts.setdefault(uid, lock) != lock:
                    raise Refused(f'uid {uid} names two boards: {duts[uid]} and {lock}')
            found = set()
            _identities(e, found)
            for raw in found:
                i = identity(raw)
                if raw_of.setdefault(i, raw) != raw:
                    raise Refused(f'identities {raw_of[i]!r} and {raw!r} are one and the same to udev')
                ids.setdefault(i, set()).add(lock)
    return {'locks': sorted(locks), 'identities': ids}


# --- coverage ------------------------------------------------------------------------------
def _bounded(path, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise OverBudget('coverage check over budget: subtree uninspectable')
    return uo.read_sysfs(path, min(uo.SYSFS_READ_GRACE, left))


def top(busport, scope):
    """What an operation on the busport can reach: its root port for `root`, its parent hub
    (the hub and everything under it) for `hub`."""
    if scope == 'root':
        return busport.split('.')[0]
    if '.' not in busport:
        raise Refused(f'{busport} is on a root port: no hub above it to scope to')
    return busport.rsplit('.', 1)[0]


def _read_node(d, name, deadline, cached):
    """One device directory's {serial, udev, identity, cls, ino}; None when it vanished mid-walk
    (the second walk reports the difference)."""
    try:
        ino = os.stat(d + '/').st_ino
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused(f'{name}: cannot inspect ({e.strerror})')
    cls = _bounded(os.path.join(d, 'bDeviceClass'), deadline)
    serial = _bounded(os.path.join(d, 'serial'), deadline)
    udev = None
    if serial is None and cached:
        bus, dev = (_bounded(os.path.join(d, a), deadline) for a in ('busnum', 'devnum'))   # lock-free
        if bus and dev and bus.isdigit() and dev.isdigit():
            udev = uo.udev_serial(int(bus), int(dev))
    ident = identity(serial) if serial else identity(udev) if udev else None
    return {'serial': serial, 'udev': udev, 'identity': ident or None, 'cls': cls, 'ino': ino}


def walk(busport, identities, deadline, cached=True, scope='root'):
    """The subtree `top(busport, scope)` heads: {name: {serial, udev, identity, cls, ino,
    covered, why}}. `serial` is the sysfs read; `udev` its record, consulted only when sysfs
    did not answer and `cached` allows it (admission, never restoration). Raises Refused on
    a budget overrun; the target itself must be present."""
    root = top(busport, scope)
    nodes = {}
    for d in sorted(glob.glob(os.path.join(uo.SYS_USB_DEVICES, f'{root}*'))):
        name = os.path.basename(d)
        if ':' in name or not (name == root or name.startswith(root + '.')):
            continue
        if (n := _read_node(d, name, deadline, cached)) is not None:
            nodes[name] = n
    if time.monotonic() > deadline:
        raise OverBudget('coverage check over budget: subtree uninspectable')
    if busport not in nodes:
        raise Refused(f'no device at {busport}')
    return _judge(nodes, identities)


def _judge(nodes, identities):
    """Sets each port node's `covered` (the locks or 'hub' explaining it, None when nothing
    does) and `why`; a node already judged (a root hub) is left as it is."""
    for name in sorted(nodes, key=lambda n: -n.count('.')):   # leaves first, so a hub sees its children judged
        n = nodes[name]
        if 'covered' in n:
            continue
        sn = n['identity']
        children = [c for c in nodes if c.startswith(name + '.') and c.count('.') == name.count('.') + 1]
        if n['cls'] is None:
            n['covered'], n['why'] = None, 'class unreadable'
        elif sn and sn in identities:
            n['covered'], n['why'] = sorted(identities[sn]), ''
        elif n['cls'] == HUB_CLASS and children and all(nodes[c]['covered'] for c in children):
            n['covered'], n['why'] = ['hub'], ''
        elif n['cls'] == HUB_CLASS and not children:
            n['covered'], n['why'] = None, 'empty hub, nothing identifies it'
        elif n['cls'] == HUB_CLASS:
            n['covered'], n['why'] = None, 'hub with an uncovered child'
        elif sn is None:
            n['covered'], n['why'] = None, 'no identity: serial unreadable or absent, none recorded by udev'
        else:
            n['covered'], n['why'] = None, f'serial {n["serial"] or n["udev"]} matches no roster identity'
    return nodes


def shown(n):
    return n['serial'] or (f'{n["udev"]} (udev)' if n['udev'] else '-')


def snapshot(nodes):
    return {name: (n['identity'], n['ino']) for name, n in nodes.items()}


def gaps(nodes):
    return [f'{name}: {n["why"]}' for name, n in sorted(nodes.items()) if not n['covered']]


def check(busport, rosters, budget, scope='root'):
    """Two walks within one budget; returns the nodes or raises Refused naming every gap."""
    deadline = time.monotonic() + budget
    first = walk(busport, rosters['identities'], deadline, scope=scope)
    second = walk(busport, rosters['identities'], deadline, scope=scope)
    if snapshot(first) != snapshot(second):
        raise Refused('the subtree changed during the check')
    bad = gaps(second)
    if bad:
        raise Refused(f'uncovered under {SCOPE_NAME[scope]} {top(busport, scope)}:\n  ' + '\n  '.join(bad))
    return second


def describe(rosters, nodes):
    lines = [f'locks ({len(rosters["locks"])}): ' + ' '.join(rosters['locks'])]
    for name, n in sorted(nodes.items()):
        lines.append(f'  {name}: serial {shown(n)} -> {",".join(n["covered"])}')
    return '\n'.join(lines)


def describe_controller(addr, state, rosters, nodes):
    locks = host_locks(rosters)
    extra = sorted(set(locks) - set(rosters['locks']))
    ids = state['ids']
    lines = [f'controller {addr}: {ids["vendor"]}:{ids["device"]} subsystem {ids["subsystem_vendor"]}:'
             f'{ids["subsystem_device"]} class {ids["class"]}, driver {state["driver"] or "none"}',
             f'locks ({len(locks)}): ' + ' '.join(locks)]
    if extra:
        lines.append('  of them in no roster given: ' + ' '.join(extra))
    for name, n in sorted(nodes.items()):
        lines.append(f'  {name} ({n["key"]}): serial {shown(n)} -> {",".join(n["covered"])}')
    return '\n'.join(lines)


# --- processes -----------------------------------------------------------------------------
def survivors(pgid):
    """Live descendants after the command was reaped: our remaining children (orphans
    reparent to a subreaper) plus anything still in the command's process group."""
    me, group = str(os.getpid()), str(pgid)
    found = []
    for p in os.listdir(uo.PROC_ROOT):
        st = uo._proc_stat(p) if p.isdigit() else None
        if st and st[1] not in ('Z', 'X') and (st[3] == me or st[4] == group):
            found.append({'pid': int(p), 'start': st[2]})
    # confirmed once more: a blocker must not rest on a process that was already leaving
    return sorted(h['pid'] for h in found if uo.proc_holds(h))


def _subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise Refused('cannot become a child subreaper: ' + os.strerror(ctypes.get_errno()))


# --- reservation ---------------------------------------------------------------------------
class Reservation:
    """The locks of one run, its records, and the signal policy per phase. `forward` False
    keeps every signal from the command: a controller rebind cut between its unbind and bind
    would strand the controller."""

    def __init__(self, locks, reason, forward=True, admission=None):
        self.locks, self.reason, self.forward, self.admission = locks, reason, forward, admission
        self.token = secrets.token_hex(8)
        self._own = {'tool': 'hil_reserve', 'token': self.token, 'locks': list(locks)}
        self.handles = []
        self.phase = 'pre'          # pre -> spawn -> command -> post
        self.pgid = None
        self.noted = []
        self.deferred = []

    def install_signals(self):
        for s in HANDLED_SIGNALS:
            signal.signal(s, self._on_signal)

    def _on_signal(self, signum, _):
        if self.phase == 'spawn':        # the group is not known yet: run() replays it
            self.deferred.append(signum)
            return
        name = signal.Signals(signum).name
        if self.phase == 'pre':          # nothing mutated yet: give the locks back and stop
            self.release()
            os._exit(130)
        if self.phase == 'command' and self.forward and signum != signal.SIGHUP:
            try:
                os.killpg(self.pgid, signum)
                self.noted.append(f'{name} forwarded')
            except PermissionError:
                self.noted.append(f'{name} could not be delivered (EPERM)')
            except ProcessLookupError:
                pass
            return
        self.noted.append(f'{name} ignored during {self.phase}')

    def acquire(self):
        try:
            for lock in self.locks:
                self.handles.append(hil_lock.take_board_lock(lock, self.reason, protected=True, **self._own))
        except (hil_lock.BoardLocked, hil_lock.LockUnavailable, ValueError) as e:
            self.release()
            raise Refused(f'{lock}: {e}')

    def release(self):
        for fh in self.handles:
            try:
                hil_lock.release_board_lock(fh)
            except OSError:
                pass
        self.handles = []
        self._open_admission()

    def _open_admission(self):
        """Last, once every lock is given back: new lock names are admitted again."""
        if self.admission is not None:
            try:
                self.admission.close()
            except OSError:
                pass
            self.admission = None

    def run(self, argv, bound=None):
        """argv in its own process group; returns its exit code, or None once it outran
        `bound` seconds (it is left running, never signalled). A command that cannot be
        started mutated nothing: the reservation is released and Refused raised."""
        # a signal that lands while the child is being spawned waits until its group is known:
        # any thread may take it, but its handler runs here and the 'spawn' phase defers it
        # (glibc blocks every signal around posix_spawn's clone, so the spawn is never
        # interrupted); the child itself starts unblocked, so its traps see a forwarded signal
        self.phase = 'spawn'
        try:
            try:
                pid = os.posix_spawnp(argv[0], argv, os.environ, setpgroup=0, setsigmask=(),
                                      setsigdef=SPAWN_SIGDEF)
            except OSError as e:
                self.phase = 'pre'
                self.release()
                raise Refused(f'cannot start {argv[0]}: {e}')
            self.pgid = pid
            self.phase = 'command'
        finally:
            deferred, self.deferred = self.deferred, []
            for signum in deferred:
                self._on_signal(signum, None)
        start = time.monotonic()
        next_progress = start + PROGRESS_EVERY
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if bound is not None and time.monotonic() - start >= bound:
                self.phase = 'post'
                return None
            if time.monotonic() >= next_progress:
                next_progress += PROGRESS_EVERY
                try:
                    print(f'hil_reserve: {os.path.basename(argv[2])} still running', flush=True)
                except (OSError, ValueError):
                    pass    # a lost reader never abandons a running mutation
            time.sleep(WAIT_POLL)
        self.phase = 'post'
        return os.waitstatus_to_exitcode(status)    # a signal death stays negative, as Popen's

    def end_blocked(self, problems, receipts=()) -> int:
        """A blocker: named on both streams, logged on every board (hil_lock.end_failed, with
        `receipts`), and every lock released; returns the exit status, 1. A log or output failure
        is reported where possible and never keeps a lock."""
        detail = '; '.join(problems)
        errors = hil_lock.end_failed(dict(zip(self.locks, self.handles)), self.reason, detail=detail,
                                     receipts=receipts, tool='hil_reserve', token=self.token)
        self.handles = []
        self._open_admission()
        unlogged = '; '.join(f'{b}: {", ".join(e)}' for b, e in sorted(errors.items()))
        for stream in (sys.stdout, sys.stderr):
            try:
                print(f'hil_reserve: BLOCKER token {self.token} pid {os.getpid()}: {detail}; restoration not '
                      f'established; released {len(self.locks)} lock(s), the blocker logged in each board\'s '
                      f'cleanup log' + (f' (not logged: {unlogged})' if unlogged else ''), file=stream, flush=True)
            except (OSError, ValueError):
                pass
        return 1

def _not_back(label, n, got):
    """Why the node `n` (pre-operation) is not back as `got` (its fresh sysfs look), or None."""
    if got is None:
        return f'{label} (serial {shown(n)}) not back: absent'
    if n['identity'] is None:
        # a hub without a serial stays without one
        return f'{label} (no serial) not back: serial {got["serial"]}' if got['serial'] is not None else None
    if got['serial'] is None:
        return f'{label} (serial {shown(n)}) not back: serial unreadable'
    if identity(got['serial']) != n['identity']:
        return f'{label} (serial {shown(n)}) not back: serial {got["serial"]}'
    return None


def restoration_gaps(before, busport, rosters, deadline, scope='root'):
    """What keeps the subtree from counting as back: coverage from a fresh sysfs-only walk,
    every pre-operation node present with a readable serial matching its identity, and
    nothing new."""
    try:
        after = walk(busport, rosters['identities'], deadline, cached=False, scope=scope)
    except OverBudget:
        raise
    except Refused as e:                  # the target itself absent, or a node uninspectable: a gap, looked at again
        return [str(e)]
    problems = gaps(after)
    problems += [p for name, n in sorted(before.items()) if (p := _not_back(name, n, after.get(name)))]
    problems += [f'{name}: new node (serial {shown(after[name])})' for name in sorted(set(after) - set(before))]
    return problems


# --- controller ----------------------------------------------------------------------------
def controller_state(addr, deadline):
    """The PCI function's {ids, driver}: `ids` its PCI_IDENTITY attributes, `driver` the bound
    driver's name or None. Refused when it is absent, unreadable or not a USB controller."""
    d = os.path.join(SYS_PCI_DEVICES, addr)
    if not os.path.isdir(d):
        raise Refused(f'no such pci device: {addr}')
    ids = {a: _bounded(os.path.join(d, a), deadline) for a in PCI_IDENTITY}
    if None in ids.values():
        raise Refused(f'{addr}: ' + ', '.join(a for a, v in ids.items() if v is None) + ' unreadable')
    if not ids['class'].startswith(USB_CONTROLLER_CLASS):
        raise Refused(f'{addr} is not a USB controller (class {ids["class"]})')
    try:
        driver = os.path.basename(os.readlink(os.path.join(d, 'driver')))
    except FileNotFoundError:
        driver = None
    except OSError as e:
        raise Refused(f'{addr}: driver unreadable ({e.strerror})')
    return {'ids': ids, 'driver': driver}


def controller_walk(addr, identities, deadline, cached=True):
    """Every bus the controller owns, as walk's nodes under their sysfs names, each with a
    `key` that survives a renumbering: its root hub's speed and its port path (`480M-2.7`
    for 17-2.7 under a 480 Mbps usb17). Root hubs are nodes too, covered by the controller."""
    d = os.path.join(SYS_PCI_DEVICES, addr)
    try:
        entries = sorted(os.listdir(d))
    except FileNotFoundError:
        raise Refused(f'no such pci device: {addr}') from None
    except OSError as e:
        raise Refused(f'{addr}: cannot inspect ({e.strerror})')
    nodes, speeds = {}, {}
    for root in entries:
        if not (m := ROOT_HUB_RE.match(root)):
            continue
        try:
            ino = os.stat(os.path.join(d, root) + '/').st_ino
        except FileNotFoundError:
            continue
        except OSError as e:
            raise Refused(f'{root}: cannot inspect ({e.strerror})')
        speed = _bounded(os.path.join(d, root, 'speed'), deadline)
        if not speed:
            raise Refused(f'{root}: speed unreadable, so its bus cannot be told apart across a renumbering')
        if speed in speeds:
            raise Refused(f'{speeds[speed]} and {root} are both {speed} Mbps root hubs of {addr}: '
                          'their buses cannot be told apart across a renumbering')
        speeds[speed] = root
        nodes[root] = {'serial': None, 'udev': None, 'identity': None, 'cls': HUB_CLASS, 'ino': ino,
                       'key': f'{speed}M', 'covered': ['controller'], 'why': ''}
        bus = m.group(1)
        for dev in sorted(glob.glob(os.path.join(uo.SYS_USB_DEVICES, f'{bus}-*'))):
            name = os.path.basename(dev)
            if not uo.BUSPORT_RE.match(name) or name.split('-')[0] != bus:
                continue
            if (n := _read_node(dev, name, deadline, cached)) is not None:
                nodes[name] = dict(n, key=f'{speed}M-{name.split("-", 1)[1]}')
    if time.monotonic() > deadline:
        raise OverBudget('coverage check over budget: controller uninspectable')
    return _judge(nodes, identities)


def check_controller(addr, rosters, budget, verb=None):
    """The controller's state and two walks of its buses within one budget; returns (state,
    nodes) or raises Refused naming every gap. A verb also needs the driver state it acts on."""
    deadline = time.monotonic() + budget
    state = controller_state(addr, deadline)
    if verb == 'pci-rebind' and state['driver'] is None:
        raise Refused(f'no driver bound to {addr}: pci-bind attaches one')
    if verb == 'pci-bind' and state['driver'] is not None:
        raise Refused(f'{addr} already has a driver bound ({state["driver"]})')
    first = controller_walk(addr, rosters['identities'], deadline)
    second = controller_walk(addr, rosters['identities'], deadline)
    if snapshot(first) != snapshot(second) or controller_state(addr, deadline) != state:
        raise Refused('the controller changed during the check')
    bad = gaps(second)
    if bad:
        raise Refused(f'uncovered on controller {addr}:\n  ' + '\n  '.join(bad))
    return state, second


def host_locks(rosters):
    """Every roster's locks and every lock name already on the host: a dead controller hides
    the boards it reaches, so no topology can show a roster missing from the list."""
    try:
        return sorted(set(rosters['locks']) | set(hil_lock.lock_names()))
    except OSError as e:
        raise Refused(f'{hil_lock.BOARD_LOCK_DIR}: {e}')


def bus_map(before, after):
    """'17 -> 1 (480M), 18 -> 2 (5000M)': each root hub's bus before the operation and now,
    `-` for none before, `gone` for none now."""
    def buses(nodes):
        return {n['key']: name[len('usb'):] for name, n in nodes.items() if ROOT_HUB_RE.match(name)}
    was, now = buses(before), buses(after)
    return ', '.join(f'{was.get(k, "-")} -> {now.get(k, "gone")} ({k})'
                     for k in sorted(set(was) | set(now), key=lambda k: (len(k), k)))   # 480M before 5000M


def controller_gaps(state, before, addr, rosters, deadline, seen):
    """What keeps the controller from counting as back: its PCI identity unchanged, a driver
    bound (pci-rebind: the same one), a bus registered, every pre-operation node back under
    its key, and everything now on its buses covered. A node new since is no gap when
    covered: a dead controller's fixtures come back with it. With no device before, the buses
    must also be unchanged for CONTROLLER_SETTLE s. `seen['after']` keeps the last complete walk."""
    try:
        now = controller_state(addr, deadline)
    except OverBudget:
        raise
    except Refused as e:
        return [str(e)]
    problems = []
    if now['ids'] != state['ids']:
        problems.append(f'{addr}: PCI identity changed: ' + ', '.join(
            f'{a} {state["ids"][a]} -> {now["ids"][a]}' for a in PCI_IDENTITY if now['ids'][a] != state['ids'][a]))
    if now['driver'] is None:
        return problems + [f'{addr}: no driver bound (stranded; pci-bind attaches one)']
    if state['driver'] is not None and now['driver'] != state['driver']:
        problems.append(f'{addr}: bound to {now["driver"]}, was {state["driver"]}')
    try:
        after = controller_walk(addr, rosters['identities'], deadline, cached=False)
    except OverBudget:
        raise
    except Refused as e:
        return problems + [str(e)]
    seen['after'] = after
    if not any(ROOT_HUB_RE.match(name) for name in after):
        problems.append(f'{addr}: no bus registered')
    problems += gaps(after)
    by_key = {n['key']: n for n in after.values()}
    problems += [p for name, n in sorted(before.items())
                 if (p := _not_back(f'{name} ({n["key"]})', n, by_key.get(n['key'])))]
    if all(ROOT_HUB_RE.match(name) for name in before):
        # nothing to wait for by name: the first root-hub-only look would pass before any device enumerated
        snap, now_t = snapshot(after), time.monotonic()
        if seen.get('settle', (None,))[0] != snap:
            seen['settle'] = (snap, now_t)
        if (steady := now_t - seen['settle'][1]) < CONTROLLER_SETTLE:
            problems.append(f'{addr}: buses unchanged for {steady:.1f} of {CONTROLLER_SETTLE:g} s')
    return problems


# --- run -----------------------------------------------------------------------------------
def _hold(res, recheck):
    """Take every lock, then `recheck()` (raising Refused) before anything is mutated, and
    show each board's open cleanup failures."""
    res.install_signals()
    res.acquire()
    try:
        recheck()
    except Refused:
        res.release()
        raise
    for lock, fh in zip(res.locks, res.handles):
        if failures := fh.cleanup_failures:
            more = f' ({len(failures) - 1} earlier still open)' if len(failures) > 1 else ''
            print(f'hil_reserve: {lock}: an earlier task\'s cleanup failed: {hil_lock.failure_text(failures[-1])}{more}',
                  flush=True)


def _outcome(res, verb, argv, rc, budget, unchanged, look, failed_note=None):
    """After the command: exit 0 with every lock released when it exited 0, left no survivor
    and `look(deadline)` (restoration gaps, retried until the budget is spent) found none;
    Refused when it refused before writing (exit 3) and `unchanged()` holds; else the blocker,
    a failed exit followed by `failed_note(rc)` when given."""
    deadline = time.monotonic() + budget      # one deadline for everything that follows the command
    problems, unproven = [], []
    if rc is None:
        problems.append(f'command still running after {CONTROLLER_COMMAND_BUDGET:g} s')
    elif rc != 0:
        problems.append(f'command exited {rc}')
        if failed_note and (note := failed_note(rc)):
            problems.append(note)
    left = survivors(res.pgid)
    if left:
        problems.append(f'surviving pids {left}')
        # a survivor has no receipt to recheck: the marker keeps its logged failure open (resolve_restored)
        unproven.append({'unrecheckable': argv, 'survivors': left})
    if rc == REFUSED_UNWRITTEN and not left:
        # the script refused before its first write: proven only if what it reaches is unchanged
        try:
            same = unchanged()
        except Refused:
            same = False
        if same and not res.noted:
            res.release()
            raise Refused(f'{verb} refused before writing anything (its message above); locks released')
    try:
        gaps_left = look(deadline)
    except OverBudget as e:
        gaps_left = [str(e)]
    while gaps_left and (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(RESTORE_POLL, remaining))
        try:
            gaps_left = look(deadline)
        except OverBudget:
            break                                   # mid-look: the last complete look stands
    if gaps_left:
        gaps_left.append(f'not restored within {budget:g} s')
    problems += gaps_left
    problems = res.noted + problems       # noted last: a signal can land during the reconciliation
    if problems:
        return res.end_blocked(problems, unproven)
    res.release()
    return 0


def cmd_run(args):
    scope, busport = VERB_SCOPE[args.verb], args.target
    if scope == 'controller':
        return cmd_run_controller(args)
    rosters = load_rosters(args.roster)
    nodes = check(busport, rosters, args.budget, scope)
    if nodes[busport]['identity'] is None:
        raise Refused(f'{busport} has no serial, in sysfs or recorded by udev, to bind the command to')
    if args.verb == 'hub-cycle' and nodes[busport]['cls'] == HUB_CLASS:
        raise Refused(f'{busport} is a hub: hub-cycle cycles a leaf only')
    _subreaper()
    res = Reservation(rosters['locks'], args.reason)
    seen = {}

    def recheck():
        seen['again'] = check(busport, rosters, args.budget, scope)
        if snapshot(seen['again']) != snapshot(nodes):
            raise Refused('the subtree changed between the check and the hold')
    _hold(res, recheck)
    target = seen['again'][busport]   # the latest look: a raw sysfs serial over the udev form the script would refuse
    argv = ['sudo', '-n', RECOVER_SCRIPT, args.verb, busport, target['serial'] or target['udev']]
    print('hil_reserve: holding ' + ' '.join(res.locks) + '\nhil_reserve: ' + ' '.join(argv), flush=True)
    rc = res.run(argv)
    status = _outcome(res, args.verb, argv, rc, args.budget,
                      lambda: snapshot(check(busport, rosters, args.budget, scope)) == snapshot(nodes),
                      lambda deadline: restoration_gaps(nodes, busport, rosters, deadline, scope))
    if status == 0:
        print(f'hil_reserve: released {len(res.locks)} lock(s); {len(nodes)} node(s) back under '
              f'{SCOPE_NAME[scope]} {top(busport, scope)}')
    return status


def cmd_run_controller(args):
    addr = args.target
    rosters = load_rosters(args.roster)
    state, nodes = check_controller(addr, rosters, args.budget, args.verb)
    _subreaper()
    try:
        hil_lock.ensure_lock_files(rosters['locks'])     # before admission closes, which refuses even our creates
        admission = hil_lock.close_admission()
    except (hil_lock.BoardLocked, hil_lock.LockUnavailable, OSError) as e:
        raise Refused(f'lock admission: {e}')
    try:
        locks = host_locks(rosters)
    except Refused:
        admission.close()
        raise
    res = Reservation(locks, args.reason, forward=False, admission=admission)

    def unchanged():
        again_state, again = check_controller(addr, rosters, args.budget, args.verb)
        return again_state == state and snapshot(again) == snapshot(nodes)

    def recheck():
        if not unchanged():
            raise Refused('the controller changed between the check and the hold')
        if created := sorted(set(host_locks(rosters)) - set(locks)):     # by a helper older than ADMISSION
            raise Refused('lock(s) created during the hold, so not held: ' + ' '.join(created))
    _hold(res, recheck)
    argv = ['sudo', '-n', RECOVER_SCRIPT, args.verb, addr, *([args.driver] if args.driver else [])]
    extra = sorted(set(locks) - set(rosters['locks']))
    print('hil_reserve: holding ' + ' '.join(res.locks)
          + (f'\nhil_reserve: of them in no roster given: {" ".join(extra)}' if extra else '')
          + '\nhil_reserve: ' + ' '.join(argv), flush=True)
    rc = res.run(argv, bound=CONTROLLER_COMMAND_BUDGET)
    seen = {}

    def stale_helper(rc):
        """An exit 1 with nothing changed is what a helper older than its pci exit 3 gives for a
        refusal before the unbind: still a blocker, since nothing proves which it was."""
        try:
            same = rc == 1 and unchanged()
        except Refused:
            same = False
        return (f'the controller is unchanged: if {RECOVER_SCRIPT} predates exit 3 for pci refusals '
                f'(agentrc#236; refresh the root-owned copy with hil-farm-setup), this was a refusal '
                f'before the unbind') if same else None
    status = _outcome(res, args.verb, argv, rc, args.budget, unchanged,
                      lambda deadline: controller_gaps(state, nodes, addr, rosters, deadline, seen), stale_helper)
    if 'after' in seen:
        print(f'hil_reserve: controller {addr} buses: {bus_map(nodes, seen["after"]) or "none"}; '
              're-derive every busport on them', flush=True)
    if status == 0:
        print(f'hil_reserve: released {len(res.locks)} lock(s); controller {addr} back, '
              f'{len(seen["after"])} node(s) on its buses')
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def add_roster_args(p):
        p.add_argument('--roster', action='append', required=True, metavar='FILE', help='a roster file')
        p.add_argument('--budget', type=float, default=DEFAULT_BUDGET, help='seconds for the coverage walk')

    p_check = sub.add_parser('check')
    p_check.add_argument('target', metavar='busport|pciaddr')
    p_check.add_argument('--scope', choices=sorted(SCOPE_NAME), default='root',
                         help='what the coverage walk spans: the root port (default), the parent hub, '
                              'or every bus of the controller at a PCI address')
    add_roster_args(p_check)
    p_run = sub.add_parser('run')
    p_run.add_argument('verb', choices=sorted(VERB_SCOPE))
    p_run.add_argument('target', metavar='busport|pciaddr')
    p_run.add_argument('--reason', required=True)
    p_run.add_argument('--driver', metavar='NAME',
                       help='pci-bind only: the driver to bind (default: the helper tries xhci-pci-renesas, '
                            'then xhci_hcd)')
    add_roster_args(p_run)
    args = ap.parse_args(argv)
    scope = args.scope if args.cmd == 'check' else VERB_SCOPE[args.verb]
    try:
        if scope == 'controller' and not PCI_RE.match(args.target):
            ap.error(f'bad pci address: {args.target}')
        if scope != 'controller' and not uo.BUSPORT_RE.match(args.target):
            ap.error(f'bad busport: {args.target}')
        if getattr(args, 'driver', None) is not None:
            if args.verb != 'pci-bind':
                ap.error('--driver is for pci-bind only')
            if not DRIVER_RE.match(args.driver):
                ap.error(f'bad driver name: {args.driver}')
        if args.cmd == 'check':
            rosters = load_rosters(args.roster)
            if scope == 'controller':
                state, nodes = check_controller(args.target, rosters, args.budget)
                print(describe_controller(args.target, state, rosters, nodes))
            else:
                print(describe(rosters, check(args.target, rosters, args.budget, scope)))
            return 0
        return cmd_run(args)
    except Refused as e:
        print(f'hil_reserve: refused: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
