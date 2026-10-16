#!/usr/bin/env python3
"""Run two capture commands side by side under one bound, keep what each wrote, and report when
each ran (the target-debug skill's USB dual-side capture).

  dual_capture.py --a <name>=<argv.json> --b <name>=<argv.json> --timeout S --out <new dir>

Each argv file is a JSON list of strings, the command exactly as it should run (no shell); for a
host-side usbmon capture and a target channel, e.g. ["python3", "-u",
"<usb-kernel-debug>/scripts/usbcap.py", "5", "30", "host.pcapng"] and an rtt.py or serial console
command. A Python side needs -u, or a line it prints reaches its file only when it exits. Each side
starts in its own process group with its stdout and stderr in <out>/<name>.out and <name>.err, and
its working directory in <out>, so relative output paths land there.

Both sides are started, then waited for until both exit or --timeout passes. --timeout is a
backstop: give each tool its own duration and a --timeout beyond the longer one. A side still
running then gets SIGTERM to its process group, so the tool runs its own shutdown (rtt.py stops
its probe server, which sits in a session of its own); a descendant that outlives its tool, one
that left the group included, gets SIGTERM itself; whatever is left of the side after --grace
seconds gets SIGKILL. The same happens to a side whose tool exited leaving descendants, to the
other side when one cannot start, and to both on SIGINT or SIGTERM. What each side wrote is kept.
This script is the subreaper of both sides, so a descendant orphaned before it was seen
reparents here: it is stopped the same way and reported under orphans, its side unknown.

The report gives each side's spawn and exit times (seconds after the first spawn) and the window
in which both processes existed. An exit time is when this script saw the exit, within one 50 ms
poll, so it and the window's end are upper bounds. The window is when both ran, not when both were
capturing: a tool's readiness (usbmon attached, an RTT block found, a port opened) is its own
output's to show, and this script claims none.

Exit: 0 both sides exited 0 within the bound and left nothing running; 1 a side exited non-zero
or had to be stopped, or the run was interrupted (each side reported); 2 bad usage; 3 a side
could not be started.
"""
import argparse
import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import time


PR_SET_CHILD_SUBREAPER = 36


class Interrupted(Exception):
    pass


def load_side(p, flag, spec, out):
    name, sep, path = spec.partition('=')
    if not sep or not name or not path:
        p.error(f'{flag} {spec!r}: give <name>=<argv.json>')
    if not name.replace('-', '').replace('_', '').isalnum():
        p.error(f'{flag} {name!r}: the name becomes a file name; letters, digits, - and _ only')
    try:
        with open(path) as f:
            argv = json.load(f)
    except (OSError, ValueError) as e:
        p.error(f'{flag} {path}: {e}')
    if not (isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv)):
        p.error(f'{flag} {path}: a JSON list of strings, the command and its arguments')
    if any('\0' in x for x in argv):
        p.error(f'{flag} {path}: an argument holds a NUL character, which no command line can carry')
    return {'name': name, 'argv': argv, 'stdout': os.path.join(out, name + '.out'),
            'stderr': os.path.join(out, name + '.err'), 'seen': {}}


def children(pid):
    """pid's children, from every one of its threads' children lists."""
    kids = set()
    try:
        tasks = os.listdir(f'/proc/{pid}/task')
    except OSError:
        return kids
    for t in tasks:
        try:
            with open(f'/proc/{pid}/task/{t}/children') as f:
                kids.update(int(x) for x in f.read().split())
        except OSError:
            pass
    return kids


def track(seen, tops):
    """Add every process now below tops, or below one seen earlier and still the same process, to
    seen, keyed by pid with its start time so a reused pid is never taken for it."""
    todo = [*tops, *(pid for pid, start in seen.items() if (stat(pid) or (None, None))[1] == start)]
    while todo:
        for k in children(todo.pop()):
            st = stat(k)
            if st and seen.get(k) != st[1]:
                seen[k] = st[1]
                todo.append(k)


def adopt(sides, strays):
    """This process is the subreaper: a descendant orphaned before a side saw it reparents here.
    It belongs to one of the sides, which one is unknown; reap the orphans that already exited.
    Returns the strays added by this call."""
    before = dict(strays)
    leaders = {s['proc'].pid for s in sides if 'proc' in s}
    for k in children(os.getpid()) - leaders:
        st = stat(k)
        if st and st[0] == 'Z':
            try:
                os.waitpid(k, os.WNOHANG)
            except OSError:
                pass
        elif st and strays.get(k) != st[1] and not any(s['seen'].get(k) == st[1] for s in sides):
            strays[k] = st[1]
    track(strays, [])
    return {k: v for k, v in strays.items() if before.get(k) != v}


def stat(pid):
    """(state, start time) from /proc/<pid>/stat, or None when it is gone."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
        return fields[0], fields[19]
    except (OSError, IndexError):
        return None


def note_exit(side):
    if side['proc'].poll() is not None:
        side.setdefault('exited', time.monotonic())


def left(seen):
    """The processes in seen still alive (zombies excluded)."""
    out = []
    for pid, start in seen.items():
        st = stat(pid)
        if st and st[0] != 'Z' and st[1] == start:
            out.append(pid)
    return out


def roots(side):
    """The leader, while Popen has not reaped it and its pid cannot have been reused."""
    return [side['proc'].pid] if side['proc'].poll() is None else []


def lingering(side):
    return side['proc'].poll() is None or bool(left(side['seen']))


def signal_pids(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--a', required=True, metavar='NAME=ARGV.json', help='first side')
    p.add_argument('--b', required=True, metavar='NAME=ARGV.json', help='second side')
    p.add_argument('--timeout', required=True, type=float, help='backstop bound on both sides, seconds')
    p.add_argument('--grace', type=float, default=15,
                   help='seconds from SIGTERM to SIGKILL, longer than either tool\'s own shutdown (default 15)')
    p.add_argument('--out', required=True, metavar='DIR', help='new directory for both sides\' output')
    a = p.parse_args(argv)
    if not (math.isfinite(a.timeout) and a.timeout > 0 and math.isfinite(a.grace) and a.grace >= 0):
        p.error('--timeout must be positive and --grace non-negative, both finite')
    sides = [load_side(p, f, s, a.out) for f, s in (('--a', a.a), ('--b', a.b))]
    if sides[0]['name'] == sides[1]['name']:
        p.error('the two sides need different names')
    try:
        os.mkdir(a.out)
    except FileExistsError:
        p.error(f'--out {a.out}: exists; give a new directory')
    except OSError as e:
        p.error(f'--out {a.out}: {e}')

    signals, armed = [], True

    def on_signal(signum, _frame):
        signals.append(signum)
        if armed:
            raise Interrupted()

    t0, started, running, strays = None, [], [], {}
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        print(json.dumps({'out': a.out, 'error': f'prctl(PR_SET_CHILD_SUBREAPER): errno {ctypes.get_errno()}'}))
        return 3
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, on_signal)
        for side in sides:
            armed = False    # a signal now is acted on once the side is fully recorded
            try:
                with open(side['stdout'], 'wb') as out, open(side['stderr'], 'wb') as err:
                    for s in sides:
                        if 'proc' in s:
                            note_exit(s)    # before the next spawn: a side already gone then never overlapped it
                    side['proc'] = subprocess.Popen(side['argv'], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                                    cwd=a.out, start_new_session=True)
                    side['spawned'] = time.monotonic()
                    t0 = side['spawned'] if t0 is None else t0
            except OSError as e:
                side['startError'] = str(e)
                break
            finally:
                armed = True
            if signals:
                raise Interrupted()
        started = [s for s in sides if 'proc' in s]
        if len(started) == 2:
            deadline = t0 + a.timeout
            while any(s['proc'].poll() is None for s in started) and time.monotonic() < deadline:
                for s in started:
                    track(s['seen'], roots(s))
                    note_exit(s)
                adopt(sides, strays)
                time.sleep(0.05)
    except Interrupted:
        pass
    finally:
        armed = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        started = [s for s in sides if 'proc' in s]
        for s in started:
            track(s['seen'], roots(s))
            note_exit(s)
        adopt(sides, strays)
        running = [s for s in started if lingering(s)]
        signal_pids(left(strays), signal.SIGTERM)
        for s in running:
            # SIGTERM to the leader's group only: a live tool stops what it started in its own order
            # (rtt.py's server sits in a session of its own); what outlived its leader gets it directly
            signal_pids([-pid for pid in roots(s)] or left(s['seen']), signal.SIGTERM)
        end = time.monotonic() + a.grace
        while True:
            for s in running:
                track(s['seen'], roots(s))
                note_exit(s)
            # before adopt: a process seen gone has already reparented its orphans here
            busy = any(lingering(s) for s in running)
            signal_pids(left(adopt(sides, strays)), signal.SIGTERM)
            if not (busy or left(strays)) or time.monotonic() >= end:
                break
            time.sleep(0.05)
        for s in running:
            if lingering(s):
                s['killed'] = True
                track(s['seen'], roots(s))
                # the group only while its unreaped leader holds the pgid; what it leaves is in seen or strays
                signal_pids([*(-pid for pid in roots(s)), *left(s['seen'])], signal.SIGKILL)
            try:
                s['proc'].wait(5)
            except subprocess.TimeoutExpired:
                pass
            note_exit(s)
        adopt(sides, strays)
        signal_pids(left(strays), signal.SIGKILL)
        time.sleep(0.1 if strays else 0)
        adopt(sides, strays)
    rows = []
    for s in sides:
        row = {k: s[k] for k in ('name', 'argv', 'stdout', 'stderr')}
        if 'startError' in s:
            row['startError'] = s['startError']
        if 'proc' in s:
            row.update(spawnedAt=round(s['spawned'] - t0, 3), exitedAt=round(s['exited'] - t0, 3),
                       returncode=s['proc'].returncode, stopped=s in running, killed=s.get('killed', False),
                       stdoutBytes=os.path.getsize(s['stdout']), stderrBytes=os.path.getsize(s['stderr']))
            remaining = sorted(left(s['seen']))
            if remaining:
                row['descendantsLeft'] = remaining
        rows.append(row)
    result = {'out': a.out, 'timeout': a.timeout, 'sides': rows}
    if len(started) == 2:
        lo, hi = max(r['spawnedAt'] for r in rows), min(r['exitedAt'] for r in rows)
        result['bothRunning'] = {'from': lo, 'to': hi, 'seconds': round(max(0.0, hi - lo), 3)}
    if strays:
        result['orphans'] = {'pids': sorted(strays), 'left': sorted(left(strays))}
    if signals:
        result['interrupted'] = signals
    print(json.dumps(result))
    if any('startError' in s for s in sides):
        return 3
    ok = (len(started) == 2 and not signals and not strays
          and all(r['returncode'] == 0 and not r['stopped'] for r in rows))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
