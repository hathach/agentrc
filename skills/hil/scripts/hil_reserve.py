#!/usr/bin/env python3
"""Host-wide reservation for a root-port operation on a HIL host (`hil` skill, Board locks):
every board lock of every supplied roster, taken by one process around the one supported
mutation, after a coverage check of the subtree the operation reaches.

  hil_reserve.py check <busport> --roster FILE [SELECTOR] ...
  hil_reserve.py run root-cycle <busport> --roster FILE [SELECTOR] ... --reason TEXT
  hil_reserve.py release --token TOKEN

A roster is a project's HIL roster JSON (`boards` and `boards-skip`; a parked board is still
on the hub). SELECTOR is the dotted field holding the entry's lock name, falling back to
`name` only when the entry lacks the field (tinyuf2: `tinyuf2.lock`; tinyusb: the default,
`name`). Identities are every `uid`, `uart_uid` and `serial` string in an entry, compared
in the form udev's usb_id gives a serial (whitespace runs and unsafe characters as `_`),
case-insensitively; two roster identities that become one that way are refused. The
contract's Hosts/rosters is the roster list: the check refuses what the rosters do not
explain, it never infers an inventory.

check (read-only, exit 0 covered / 1 refused / 2 usage): walks the device directories under
the busport's root port twice within one budget. A node's identity is its sysfs `serial`,
or, when that read does not answer (a wedged device serves it under the lock the wedge
holds), udev's record of it (`ID_SERIAL_SHORT`, written when the device was added and read
without touching it). A node is covered when its class is readable and its identity matches
a roster identity, or when it is a hub (bDeviceClass 09) whose children are all covered. An
empty hub, an unreadable class, a node without an identity, an unmatched identity, a tree
that differs between the walks, or a walk over budget refuses, naming each.

run: check, take every lock (all-or-nothing), check again, run
`sudo -n <usb-kernel-recover>/scripts/usb_recover.sh root-cycle <busport> <serial>` with
the target's sysfs serial, or its udev one when sysfs did not answer (the script then warns
and proceeds, as its own guard says), then validate: the command exited 0, no descendant
survives (this process is a child subreaper, so orphans reparent to it), and the subtree is
back: every pre-operation node with its sysfs serial readable and matching its identity (a
udev record is admission evidence, never restoration evidence), nothing new, all of it
covered. That restoration check repeats twice a second until it holds or one budget has
passed since the command ended, every read bounded by that same deadline, since siblings
re-enumerate one after another after the target. Only a restored subtree releases the
locks, and only restoration gaps may clear by waiting: a non-zero exit, a survivor or a
signal stays a blocker. Any other outcome
turns the records into `role: cleanup-holder` records and keeps this process alive holding
the locks, as the shared rule requires; the blocker line prints the reservation token, and
`release --token` is the contract recovery procedure's release step once restoration is
established: it verifies the token, the holder's pid and start time, the role and the lock
set on every record, then signals through a pidfd. Exit 0 released, 1 blocker (run) or
refused (release), 2 usage.

Signals: SIGINT/SIGTERM before the command release the untouched reservation and exit 130;
during the command they are forwarded to its process group; afterwards they are noted and
ignored. SIGUSR1 is ignored until the cleanup-holder state.
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
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', os.path.join(HERE, f'{name}.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hil_lock = _load('hil_lock')
uo = _load('usb_observe')

LOCK_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
UDEV_UNSAFE_RE = re.compile(r'[^A-Za-z0-9#+.:=@_-]')   # what usb_id turns into '_' after whitespace runs
BUSPORT_RE = re.compile(r'^[0-9]+-[0-9]+(\.[0-9]+)*$')
IDENTITY_KEYS = ('uid', 'uart_uid', 'serial')
HUB_CLASS = '09'
DEFAULT_BUDGET = 60.0
PROGRESS_EVERY = 60.0
RESTORE_POLL = 0.5         # between restoration looks; a node back later than this before the deadline is missed
# the one command `run` builds; a test points this at a stub through the module, never the CLI
RECOVER_SCRIPT = os.path.normpath(os.path.join(HERE, '..', '..', 'usb-kernel-recover', 'scripts', 'usb_recover.sh'))
PR_SET_CHILD_SUBREAPER = 36
_ABSENT = object()


class Refused(Exception):
    pass


class OverBudget(Refused):
    """A walk that ran out of its deadline; the only refusal a restoration look does not retry."""


def identity(serial):
    """A serial in the form udev's usb_id records it (systemd udev-util.c udev_replace_whitespace
    and udev_replace_chars), lower-cased, so a sysfs serial and its udev record compare equal."""
    return UDEV_UNSAFE_RE.sub('_', re.sub(r'\s+', '_', serial.strip())).lower()


# --- rosters -------------------------------------------------------------------------------
def _field(entry, selector):
    cur = entry
    for part in selector.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _ABSENT
        cur = cur[part]
    return cur


def _identities(obj, out):
    """The raw identity strings of an entry, stripped and lower-cased."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in IDENTITY_KEYS and isinstance(v, str) and v.strip():
                out.add(v.strip().lower())
            else:
                _identities(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _identities(v, out)


def load_rosters(specs):
    """[(file, selector)] -> {'locks': sorted lock names, 'identities': {identity: {locks}}}.
    A DUT `uid` under two lock names is a conflict; any other identity may serve several."""
    locks, ids, duts, raw_of = set(), {}, {}, {}
    for path, selector in specs:
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
            lock = _field(e, selector)
            if lock is _ABSENT:
                lock = e['name']
            if not isinstance(lock, str) or not LOCK_NAME_RE.match(lock):
                raise Refused(f'roster {path}: {e["name"]}: lock name {lock!r} is not a plain file name')
            locks.add(lock)
            uid = e.get('uid')
            if isinstance(uid, str) and uid.strip():
                u = uid.strip().lower()
                if duts.setdefault(u, lock) != lock:
                    raise Refused(f'uid {uid} names two boards: {duts[u]} and {lock}')
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


def walk(busport, identities, deadline, cached=True):
    """The subtree under the busport's root port: {name: {serial, udev, identity, cls, ino,
    covered, why}}. `serial` is the sysfs read; `udev` its record, consulted only when sysfs
    did not answer and `cached` allows it (admission, never restoration). Raises Refused on
    a budget overrun; the target itself must be present."""
    root = busport.split('.')[0]
    nodes = {}
    for d in sorted(glob.glob(os.path.join(uo.SYS_USB_DEVICES, f'{root}*'))):
        name = os.path.basename(d)
        if ':' in name or not (name == root or name.startswith(root + '.')):
            continue
        try:
            ino = os.stat(d + '/').st_ino
        except FileNotFoundError:
            continue    # vanished mid-walk: the second walk reports the difference
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
        nodes[name] = {'serial': serial, 'udev': udev, 'identity': ident or None, 'cls': cls, 'ino': ino}
    if time.monotonic() > deadline:
        raise OverBudget('coverage check over budget: subtree uninspectable')
    if busport not in nodes:
        raise Refused(f'no device at {busport}')
    for name in sorted(nodes, key=lambda n: -n.count('.')):   # leaves first, so a hub sees its children judged
        n = nodes[name]
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


def check(busport, rosters, budget):
    """Two walks within one budget; returns the nodes or raises Refused naming every gap."""
    deadline = time.monotonic() + budget
    first = walk(busport, rosters['identities'], deadline)
    second = walk(busport, rosters['identities'], deadline)
    if snapshot(first) != snapshot(second):
        raise Refused('the subtree changed during the check')
    bad = gaps(second)
    if bad:
        raise Refused('uncovered under root port %s:\n  ' % busport.split('.')[0] + '\n  '.join(bad))
    return second


def describe(rosters, nodes):
    lines = [f'locks ({len(rosters["locks"])}): ' + ' '.join(rosters['locks'])]
    for name, n in sorted(nodes.items()):
        lines.append(f'  {name}: serial {shown(n)} -> {",".join(n["covered"])}')
    return '\n'.join(lines)


# --- processes -----------------------------------------------------------------------------
def proc_start(pid):
    """The process start time (clock ticks since boot), or None when it is gone."""
    st = uo._proc_stat(pid)
    return st[2] if st else None


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
    """The locks of one run, its records, and the signal policy per phase."""

    def __init__(self, locks, reason):
        self.locks, self.reason = locks, reason
        self.token = secrets.token_hex(8)
        self.handles = []
        self.start = proc_start(os.getpid())
        self.phase = 'pre'          # pre -> command -> post -> cleanup
        self.pgid = None
        self.noted = []

    def install_signals(self):
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)     # meaningful only as a cleanup holder
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, self._on_signal)

    def _on_signal(self, signum, _):
        name = signal.Signals(signum).name
        if self.phase == 'pre':          # nothing mutated yet: give the locks back and stop
            self.release()
            os._exit(130)
        if self.phase == 'command':
            try:
                os.killpg(self.pgid, signum)
                self.noted.append(f'{name} forwarded')
            except PermissionError:
                self.noted.append(f'{name} could not be delivered (EPERM)')
            except ProcessLookupError:
                pass
            return
        self.noted.append(f'{name} ignored during {self.phase}')

    def fields(self, role=None, detail=''):
        """This reservation's own record fields, beyond the shared pid/reason/since."""
        rec = {'tool': 'hil_reserve', 'token': self.token, 'start': self.start, 'locks': self.locks}
        if role:
            rec.update(role=role, detail=detail)
        return rec

    def record(self, role=None, detail=''):
        for fh in self.handles:
            hil_lock.write_record(fh, self.reason, **self.fields(role, detail))

    def acquire(self):
        try:
            for lock in self.locks:
                self.handles.append(hil_lock.take_board_lock(lock, self.reason, **self.fields()))
        except (hil_lock.BoardLocked, hil_lock.LockUnavailable) as e:
            self.release()
            raise Refused(f'{lock}: {e}')

    def release(self):
        for fh in self.handles:
            hil_lock.clear_record(fh)
            try:
                fh.close()
            except OSError:
                pass
        self.handles = []

    def run(self, argv):
        """argv in its own process group; returns its exit code."""
        # a signal that lands while the child is being spawned waits until its group is known
        held = {signal.SIGINT, signal.SIGTERM}
        signal.pthread_sigmask(signal.SIG_BLOCK, held)
        try:
            proc = subprocess.Popen(argv, process_group=0)
            self.pgid = proc.pid
            self.phase = 'command'
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, held)
        while True:
            try:
                rc = proc.wait(timeout=PROGRESS_EVERY)
                break
            except subprocess.TimeoutExpired:
                try:
                    print(f'hil_reserve: {os.path.basename(argv[2])} still running', flush=True)
                except (OSError, ValueError):
                    pass    # a lost reader never abandons a running mutation
        self.phase = 'post'
        return rc

    def hold_as_cleanup_holder(self, problems):
        """Never returns: the records say cleanup-holder and only SIGUSR1 ends the hold.
        A record or output failure is reported where possible and never abandons the hold."""
        self.phase = 'cleanup'
        detail = '; '.join(problems)

        def bow_out(*_):
            self.release()
            os._exit(1)

        signal.signal(signal.SIGUSR1, bow_out)
        try:
            self.record(role=hil_lock.CLEANUP_ROLE, detail=detail)
        except OSError as e:
            detail += f'; cleanup record could not be written: {e}'
        for stream in (sys.stdout, sys.stderr):
            try:
                print(f'hil_reserve: BLOCKER token {self.token} pid {os.getpid()}: {detail}; restoration not '
                      f'established, holding {len(self.locks)} lock(s) until '
                      f'`hil_reserve.py release --token {self.token}`', file=stream, flush=True)
            except (OSError, ValueError):
                pass
        while True:
            signal.pause()


def restoration_gaps(before, busport, rosters, deadline):
    """What keeps the subtree from counting as back: coverage from a fresh sysfs-only walk,
    every pre-operation node present with a readable serial matching its identity, and
    nothing new."""
    try:
        after = walk(busport, rosters['identities'], deadline, cached=False)
    except OverBudget:
        raise
    except Refused as e:                  # the target itself absent, or a node uninspectable: a gap, looked at again
        return [str(e)]
    problems = gaps(after)
    for name, n in sorted(before.items()):
        got = after.get(name)
        if got is None:
            problems.append(f'{name} (serial {shown(n)}) not back: absent')
        elif n['identity'] is None:
            if got['serial'] is not None:     # a hub without a serial stays without one
                problems.append(f'{name} (no serial) not back: serial {got["serial"]}')
        elif got['serial'] is None:
            problems.append(f'{name} (serial {shown(n)}) not back: serial unreadable')
        elif identity(got['serial']) != n['identity']:
            problems.append(f'{name} (serial {shown(n)}) not back: serial {got["serial"]}')
    problems += [f'{name}: new node (serial {shown(after[name])})' for name in sorted(set(after) - set(before))]
    return problems


def cmd_run(args):
    rosters = load_rosters(args.roster)
    nodes = check(args.busport, rosters, args.budget)
    if nodes[args.busport]['identity'] is None:
        raise Refused(f'{args.busport} has no serial, in sysfs or recorded by udev, to bind the command to')
    _subreaper()
    res = Reservation(rosters['locks'], args.reason)
    res.install_signals()
    res.acquire()
    try:
        again = check(args.busport, rosters, args.budget)
        if snapshot(again) != snapshot(nodes):
            raise Refused('the subtree changed between the check and the hold')
    except Refused:
        res.release()
        raise
    target = again[args.busport]      # the latest look: a raw sysfs serial over the udev form the script would refuse
    argv = ['sudo', '-n', RECOVER_SCRIPT, 'root-cycle', args.busport, target['serial'] or target['udev']]
    print('hil_reserve: holding ' + ' '.join(res.locks) + '\nhil_reserve: ' + ' '.join(argv), flush=True)
    rc = res.run(argv)
    deadline = time.monotonic() + args.budget      # one deadline for everything that follows the command
    problems = []
    if rc != 0:
        problems.append(f'command exited {rc}')
    left = survivors(res.pgid)
    if left:
        problems.append(f'surviving pids {left}')
    try:
        gaps_left = restoration_gaps(nodes, args.busport, rosters, deadline)
    except OverBudget as e:
        gaps_left = [str(e)]
    while gaps_left and (left := deadline - time.monotonic()) > 0:
        time.sleep(min(RESTORE_POLL, left))
        try:
            gaps_left = restoration_gaps(nodes, args.busport, rosters, deadline)
        except OverBudget:
            break                                   # mid-look: the last complete look stands
    if gaps_left:
        gaps_left.append(f'not restored within {args.budget:g} s')
    problems += gaps_left
    problems = res.noted + problems       # noted last: a signal can land during the reconciliation
    if problems:
        res.hold_as_cleanup_holder(problems)   # never returns
    res.release()
    print(f'hil_reserve: released {len(res.locks)} lock(s); {len(nodes)} node(s) back under root port {args.busport.split(".")[0]}')
    return 0


def cmd_release(args):
    found = {}
    for lock in hil_lock.lock_names():
        rec = hil_lock.read_record(lock)
        if isinstance(rec, dict) and rec.get('token') == args.token:
            found[lock] = rec
    if not found:
        raise Refused(f'no holder record carries token {args.token}')
    first = next(iter(found.values()))
    pid, start, lockset = first.get('pid'), first.get('start'), sorted(first.get('locks') or [])
    for lock, rec in found.items():
        if rec.get('pid') != pid or rec.get('start') != start or sorted(rec.get('locks') or []) != lockset:
            raise Refused(f'token {args.token}: the record on {lock} disagrees with the others')
        if not hil_lock.is_cleanup_holder(rec):
            raise Refused(f'holder pid {pid} is not a cleanup holder on {lock}: still mutating or already clean')
    if not isinstance(pid, int) or lockset != sorted(found):
        raise Refused(f'token {args.token}: record lock set {lockset} != files carrying it {sorted(found)}')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise Refused('identity-bound signalling (pidfd) is unavailable on this platform')
    try:
        pidfd = os.pidfd_open(pid)
    except OSError as e:
        raise Refused(f'holder pid {pid} cannot be opened: {e}')
    try:
        live = proc_start(pid)
        if live is None or live != start:
            raise Refused(f'pid {pid} is not the recorded holder (start {live} != {start})')
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGUSR1)
        except OSError as e:
            raise Refused(f'holder pid {pid} could not be signalled: {e}')
    finally:
        os.close(pidfd)
    for _ in range(50):
        if all(hil_lock.read_record(lock) is None for lock in found):
            print(f'hil_reserve: released {len(found)} lock(s) of holder {pid} (token {args.token})')
            return 0
        time.sleep(0.1)
    raise Refused(f'holder {pid} signalled but its records remain')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def add_roster_args(p):
        p.add_argument('--roster', nargs='+', action='append', required=True, metavar=('FILE', 'SELECTOR'),
                       help='a roster file and, optionally, the dotted lock-name field (default name)')
        p.add_argument('--budget', type=float, default=DEFAULT_BUDGET, help='seconds for the coverage walk')

    p_check = sub.add_parser('check')
    p_check.add_argument('busport')
    add_roster_args(p_check)
    p_run = sub.add_parser('run')
    p_run.add_argument('verb', choices=['root-cycle'])
    p_run.add_argument('busport')
    p_run.add_argument('--reason', required=True)
    add_roster_args(p_run)
    p_rel = sub.add_parser('release')
    p_rel.add_argument('--token', required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'release':
            return cmd_release(args)
        if not BUSPORT_RE.match(args.busport):
            ap.error(f'bad busport: {args.busport}')
        specs = []
        for r in args.roster:
            if len(r) > 2:
                ap.error('--roster takes FILE [SELECTOR]')
            specs.append((r[0], r[1] if len(r) == 2 else 'name'))
        args.roster = specs
        if args.cmd == 'check':
            rosters = load_rosters(specs)
            print(describe(rosters, check(args.busport, rosters, args.budget)))
            return 0
        return cmd_run(args)
    except Refused as e:
        print(f'hil_reserve: refused: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
