#!/usr/bin/env python3
"""Run two capture commands side by side under one bound, keep what each wrote, and report when
each ran (the target-debug skill's USB dual-side capture).

  dual_capture.py --a <name>=<argv.json> --b <name>=<argv.json> --timeout S --out <new dir>

Each argv file is a JSON list of strings, the command exactly as it should run (no shell); for a
host-side usbmon capture and a target channel, e.g. ["python3", "<usb-kernel-debug>/scripts/usbcap.py",
"5", "30", "host.pcapng"] and an rtt.py or console_capture.py command. Each side starts in its
own process group with its stdout and stderr in <out>/<name>.out and <name>.err, and its working
directory in <out>, so relative output paths land there.

Both sides are started, then waited for until both exit or --timeout passes. A side still running
then is stopped with SIGTERM to its process group and to every descendant seen while it ran
(a descendant that left the group included), then SIGKILL after --grace seconds; whatever it wrote
is kept. The same happens to both on SIGINT or SIGTERM.

The report gives each side's spawn and exit times (seconds after the first spawn) and the window
in which both processes existed. That is when both ran, not when both were capturing: a tool's
readiness (usbmon attached, an RTT block found, a port opened) is its own output's to show, and
this script claims none.

Exit: 0 both sides exited 0 within the bound; 1 a side exited non-zero, was stopped at the bound,
or the run was interrupted (each side reported); 2 bad usage; 3 a side could not be started.
"""
import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time


class Interrupted(Exception):
    pass


def load_argv(p, flag, spec):
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
    return name, argv


def descendants(pid):
    """Every process below pid, from /proc's children lists."""
    found, todo = set(), [pid]
    while todo:
        p = todo.pop()
        try:
            with open(f'/proc/{p}/task/{p}/children') as f:
                kids = [int(x) for x in f.read().split()]
        except OSError:
            continue
        for k in kids:
            if k not in found:
                found.add(k)
                todo.append(k)
    return found


def signal_side(side, sig):
    try:
        os.killpg(side['proc'].pid, sig)
    except OSError:
        pass
    for pid in side['seen']:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--a', required=True, metavar='NAME=ARGV.json', help='first side')
    p.add_argument('--b', required=True, metavar='NAME=ARGV.json', help='second side')
    p.add_argument('--timeout', required=True, type=float, help='bound on both sides, seconds')
    p.add_argument('--grace', type=float, default=5, help='seconds between SIGTERM and SIGKILL (default 5)')
    p.add_argument('--out', required=True, metavar='DIR', help='new directory for both sides\' output')
    a = p.parse_args(argv)
    if not (math.isfinite(a.timeout) and a.timeout > 0 and math.isfinite(a.grace) and a.grace >= 0):
        p.error('--timeout must be positive and --grace non-negative, both finite')
    sides = [dict(zip(('name', 'argv'), load_argv(p, f, s))) for f, s in (('--a', a.a), ('--b', a.b))]
    if sides[0]['name'] == sides[1]['name']:
        p.error('the two sides need different names')
    try:
        os.mkdir(a.out)
    except FileExistsError:
        p.error(f'--out {a.out}: exists; give a new directory')
    except OSError as e:
        p.error(f'--out {a.out}: {e}')

    signals = []

    def on_signal(signum, _frame):
        signals.append(signum)
        raise Interrupted()

    result = {'out': a.out, 'timeout': a.timeout,
              'readiness': 'not observed: spawn and exit times only; each tool shows its own'}
    t0, started = None, []
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, on_signal)
        for side in sides:
            out = open(os.path.join(a.out, side['name'] + '.out'), 'wb')
            err = open(os.path.join(a.out, side['name'] + '.err'), 'wb')
            try:
                side['proc'] = subprocess.Popen(side['argv'], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                                cwd=a.out, start_new_session=True)
            except OSError as e:
                side['startError'] = str(e)
                break
            finally:
                out.close()
                err.close()
            now = time.monotonic()
            t0 = now if t0 is None else t0
            side.update(spawned=now, seen=set())
            started.append(side)
        if len(started) == 2:
            deadline = t0 + a.timeout
            while any(s['proc'].poll() is None for s in started) and time.monotonic() < deadline:
                for s in started:
                    if s['proc'].poll() is None:
                        s['seen'] |= descendants(s['proc'].pid)
                    elif 'exited' not in s:
                        s['exited'] = time.monotonic()
                time.sleep(0.05)
    except Interrupted:
        pass
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        now = time.monotonic()
        for s in started:
            if s['proc'].poll() is not None:
                s.setdefault('exited', now)
        running = [s for s in started if s['proc'].poll() is None]
        for s in running:
            s['seen'] |= descendants(s['proc'].pid)
            s['stopped'] = True
            signal_side(s, signal.SIGTERM)
        end = time.monotonic() + a.grace
        while running and time.monotonic() < end and any(s['proc'].poll() is None for s in running):
            time.sleep(0.05)
        for s in running:
            if s['proc'].poll() is None or any(alive(pid) for pid in s['seen']):
                s['killed'] = True
                signal_side(s, signal.SIGKILL)
            try:
                s['proc'].wait(5)
            except subprocess.TimeoutExpired:
                pass
            s['exited'] = time.monotonic()
    rows = []
    for s in sides:
        row = {'name': s['name'], 'argv': s['argv'], 'stdout': os.path.join(a.out, s['name'] + '.out'),
               'stderr': os.path.join(a.out, s['name'] + '.err')}
        if 'startError' in s:
            row['startError'] = s['startError']
        if 'proc' in s:
            row.update(spawnedAt=round(s['spawned'] - t0, 3), exitedAt=round(s['exited'] - t0, 3),
                       returncode=s['proc'].returncode, stoppedAtBound=s.get('stopped', False),
                       killed=s.get('killed', False))
            for key in ('stdout', 'stderr'):
                row[key + 'Bytes'] = os.path.getsize(row[key])
            left = sorted(pid for pid in s['seen'] if alive(pid))
            if left:
                row['descendantsLeft'] = left
        rows.append(row)
    result['sides'] = rows
    if len(started) == 2:
        lo, hi = max(r['spawnedAt'] for r in rows), min(r['exitedAt'] for r in rows)
        result['bothRunning'] = {'from': lo, 'to': hi, 'seconds': round(max(0.0, hi - lo), 3)}
    if signals:
        result['interrupted'] = signals
    print(json.dumps(result))
    if any('startError' in s for s in sides):
        return 3
    ok = len(started) == 2 and not signals and all(r['returncode'] == 0 and not r['stoppedAtBound'] for r in rows)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
