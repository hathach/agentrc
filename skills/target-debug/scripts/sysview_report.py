#!/usr/bin/env python3
"""Profiling numbers from a SystemView export directory (sysview_record.py's --out): per-context CPU load and run
times from contexts.csv; from events.txt, loss counts, ISR duration and ready->run latency percentiles, user-event
(function) and marker timing, task stack high-water, heap totals and per-context busy share over a workload window.

  sysview_report.py DIR [--json] [--event-map FILE] [--post-mortem]
                    [--window T0:T1 | --workload-marker ID | --workload-event ID]

The contract (fields, null rules, anchors) is the target-debug skill's sysview.md, "Script interface". A metric the
export cannot support is null, never 0 or []: everything from events.txt when there is none, and everything
time-derived when the export has no usable time base (no Init record carrying the timestamp frequency, or timestamps
that go backwards).

Exit: 0 reported; 2 bad usage or an unreadable export.
"""
import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

# SystemView's text forms in its event export (SystemView 4.10b, Linux)
TIME_RE = re.compile(r'([\d. ]+)\s*(s|ms|us)$')
RUNS_RE = re.compile(r'Runs for (\S+) (us|ms)')
READY_RE = re.compile(r'runs after (\S+) (us|ms)')
RETURNS_RE = re.compile(r'Returns after (\S+) (us|ms)')
RAN_RE = re.compile(r'Ran for (\S+) (us|ms)')
FREQ_RE = re.compile(r'Cycle Freq\.: (\d+)')
FUNC_RE = re.compile(r'^Function #(\d+)$')
MARK_RE = re.compile(r'^(Start|Stop) Marker 0x([0-9A-Fa-f]+)$')
# periodic SEGGER_SYSVIEW_SendStackInfo with a usage figure: "<task> (0x<id>): <size> @ 0x<base>, <used> Bytes used";
# the report sent at task creation has no usage and is not a high-water reading
STACK_RE = re.compile(r'^(.+) \(0x[0-9A-Fa-f]+\): \d+ @ 0x[0-9A-Fa-f]+, (\d+) Bytes used$')
# SystemView's running totals on "Allocate Memory" and "Free Memory"
HEAP_RE = re.compile(r'(\d+) used, \d+ free, [\d.]+% full -- (\d+) allocations, (\d+) frees, difference')
UNIT = {'s': 1.0, 'ms': 1e-3, 'us': 1e-6}


class Usage(Exception):
    pass


def seconds(text):
    """'0.008 165 075 s' or '0.034 725 ms' -> seconds; None for an empty or unreadable cell."""
    m = TIME_RE.match(text.strip())
    try:
        return float(m.group(1).replace(' ', '')) * UNIT[m.group(2)] if m else None
    except ValueError:
        return None


def matched_seconds(m):
    """A duration SystemView annotated an event with; Usage when its number is unreadable."""
    try:
        v = float(m.group(1))
    except ValueError:
        v = math.nan
    if not math.isfinite(v) or v < 0:
        raise Usage(f'unreadable duration in an event detail: {m.group(0)!r}')
    return v * UNIT[m.group(2)]


def pct(sorted_vals, p):
    """Nearest-rank percentile: the smallest value with at least p% of the values at or below it."""
    n = len(sorted_vals)
    return sorted_vals[max(0, -(-n * p // 100) - 1)]


def us(sec):
    return None if sec is None else round(sec * 1e6, 1)


def duration_rows(durs, name_of=str, with_id=False):
    rows = []
    for key, vals in sorted(durs.items()):
        vals.sort()
        row = {'id': key} if with_id else {}
        row.update(name=name_of(key), n=len(vals), p50_us=us(pct(vals, 50)), p99_us=us(pct(vals, 99)),
                   max_us=us(vals[-1]))
        rows.append(row)
    return rows


def load_event_map(path):
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise Usage(f'--event-map {path}: {e}')
    if not isinstance(raw, dict):
        raise Usage(f'--event-map {path}: expected a JSON object from event id to name')
    out = {}
    for key, name in raw.items():
        if not re.fullmatch(r'[0-9]+', key):
            raise Usage(f'--event-map {path}: key {key!r} is not a decimal event id')
        if not isinstance(name, str) or not name.strip():
            raise Usage(f'--event-map {path}: event {key} needs a non-empty name')
        out[int(key)] = name
    return out


CONTEXT_COLUMNS = {'Name', 'Activations', 'CPU Load', 'Total Run Time', 'Total Blocked Time', 'Min Run Time',
                   'Max Run Time'}
EVENT_COLUMNS = {'event', 'detail', 'context', 'contextint', 'contextoutint', 'timestampint', 'eventint'}
CONTEXT_TIMES = ('cpu_pct', 'total_ms', 'blocked_ms', 'min_us', 'avg_us', 'max_us')
SWITCHES = ('Task Run', 'ISR Enter', 'System Idle')
# SystemView's context for the time from a task's block (Task Stop Ready) or an ISR exit into the scheduler to the next
# switch (UM08027 6.1.1.4, 7.6.10)
SCHEDULER = 'Scheduler'


def read_csv(path, columns, what):
    """The rows of a SystemView export with every column it needs; Usage for anything else."""
    try:
        with open(path, newline='', errors='replace') as f:
            reader = csv.DictReader(f)
            fields = set(reader.fieldnames or ())
            rows = list(reader)
    except OSError as e:
        raise Usage(f'{path}: {e}')
    missing = columns - fields
    if missing:
        raise Usage(f'{path}: not a SystemView {what} export (no {", ".join(sorted(missing))} column)')
    for i, row in enumerate(rows, 2):
        if None in row.values():
            raise Usage(f'{path}: line {i} is truncated')
    return rows


def read_contexts(path):
    out = []
    for i, row in enumerate(read_csv(path, CONTEXT_COLUMNS, 'contexts'), 2):
        try:
            act = int(row['Activations'] or 0)
            cpu = row['CPU Load'].strip().rstrip('%').strip()
            cpu = float(cpu) if cpu else None
        except ValueError:
            raise Usage(f'{path}: line {i} has an unreadable Activations or CPU Load value')
        if cpu is not None and not math.isfinite(cpu):    # SystemView's load over a capture that spans no time
            cpu = None
        total = seconds(row['Total Run Time'])
        if act == 0 and not total:
            continue
        blocked = seconds(row['Total Blocked Time'])
        out.append({'name': row['Name'], 'activations': act, 'cpu_pct': cpu,
                    'total_ms': None if total is None else round(total * 1e3, 3),
                    'blocked_ms': None if blocked is None else round(blocked * 1e3, 3),
                    'min_us': us(seconds(row['Min Run Time'])),
                    'avg_us': us(total / act) if act and total is not None else None,
                    'max_us': us(seconds(row['Max Run Time']))})
    return out


def timestamp(row):
    try:
        return int(row['timestampint'])
    except ValueError:
        return None


def time_base(rows, warnings):
    """Timestamp frequency in Hz, or None when the export gives no usable one."""
    init = next((r for r in rows if r['event'] == 'Init'), None)
    m = FREQ_RE.search(init['detail']) if init else None
    if not m or not int(m.group(1)):
        warnings.append('no Init record with the timestamp frequency: time-derived metrics withheld')
        return None
    stamps = [r['ts'] for r in rows]
    if None in stamps:
        warnings.append('unreadable timestamps: time-derived metrics withheld')
        return None
    if len(stamps) < 2 or stamps[-1] <= stamps[0] or any(b < a for a, b in zip(stamps, stamps[1:])):
        warnings.append('timestamps go backwards or span nothing (counter wrap?): time-derived metrics withheld')
        return None
    return int(m.group(1))


def timed_from(rows):
    """Index of the first row past a possible untimed head, else 0. Events written with zero time deltas, as while
    the target's timestamp counter stood still (sysview.md, "Post-mortem dump"), export at the first timestamp; more
    than one context switch there marks one, though a coarse timestamp clock can tie a live capture's switches too, so
    only the caller knows which it is."""
    k = next((i for i, r in enumerate(rows) if r['ts'] != rows[0]['ts']), len(rows))
    switches = sum(r['event'] in SWITCHES for r in rows[:k])
    return k if switches > 1 else 0


class Scan:
    """One pass over events.txt."""

    def __init__(self, rows, workload_marker, workload_event, unknown_start=False):
        self.overflow = self.dropped_pairs = 0
        self.isr, self.ready, self.funcs, self.markers = {}, {}, {}, {}
        self.stack, self.heap = {}, None
        self.segments = []          # (t0, t1, context running), for busy share
        # [loss, recovery] spans where the running context is unknown: recovered at the next task switch, so never
        # (None) in a capture without one, where an ISR exit returns to a context nothing names
        self.losses = [[rows[0]['ts'], None]] if unknown_start and rows else []
        self.anchor = []            # timestamps of the workload anchor's events
        self.mark_start = self.mark_stop = None
        self.mark_open = False
        self.stamps = [r['ts'] for r in rows if r['ts'] is not None]
        open_calls = {}             # (id, context) -> [call timestamps]; LIFO so nested calls pair innermost first
        task, isrs, last = None, [], None
        for row in rows:
            event, detail, ctx, ts = row['event'], row['detail'], row['context'], row['ts']
            # a switch whose return context was lost renders as "Returns to *** OVERFLOW ***" with no Overflow row of
            # its own: most of the loss in a heavily overflowing capture
            lost = event == '*** Overflow ***' or '*** OVERFLOW ***' in detail
            if lost:
                self.overflow += 1
                if not self.losses or self.losses[-1][1] is not None:
                    self.losses.append([ts, None])
                # a call open across the gap may have lost its return; SystemView would splice it to a later one
                self.dropped_pairs += sum(map(len, open_calls.values()))
                open_calls.clear()
            elif event in ('Task Run', 'System Idle') and self.losses and self.losses[-1][1] is None and ts is not None:
                self.losses[-1][1] = ts
            if ts is not None and (lost or event in SWITCHES or event in ('ISR Exit', 'Task Block')):
                runner = isrs[-1] if isrs else task
                if runner is not None and last is not None and ts > last:
                    self.segments.append((last, ts, runner))
                if lost:
                    task, isrs = None, []    # unknown until the next task switch
                elif event == 'ISR Enter':
                    isrs.append(ctx)
                elif event == 'ISR Exit':
                    if isrs:
                        isrs.pop()
                    if detail == 'Returns to Scheduler':
                        task = SCHEDULER
                elif event == 'Task Block':
                    # the running context leaves only when the blocked task is itself; blocking or suspending another
                    # task keeps it (SystemView 4.10b exports)
                    if row['contextoutint'] != row['contextint']:
                        task = SCHEDULER
                else:
                    task = ctx
                last = ts
            mark = MARK_RE.match(event)
            func = FUNC_RE.match(event)
            if lost:
                pass
            elif event == 'ISR Enter':
                m = RUNS_RE.search(detail)
                if m:
                    self.isr.setdefault(ctx, []).append(matched_seconds(m))
            elif event == 'Task Ready':
                m = READY_RE.search(detail)
                if m:
                    self.ready.setdefault(detail.split(',')[0], []).append(matched_seconds(m))
            elif event == 'Stack Info':
                m = STACK_RE.match(detail)
                if m:
                    self.stack[m.group(1)] = int(m.group(2))
            elif event in ('Allocate Memory', 'Free Memory'):
                self.heap = HEAP_RE.search(detail) or self.heap
            elif func:
                # a call and its RecordEndCall both render as "Function #<id>"; only the return says "Returns",
                # and SystemView annotates its duration only sometimes
                fid = int(func.group(1))
                key = (fid, ctx)
                if 'Returns' not in detail:
                    open_calls.setdefault(key, []).append(ts)
                elif not open_calls.get(key):
                    self.dropped_pairs += 1
                else:
                    call_ts = open_calls[key].pop()
                    m = RETURNS_RE.search(detail)
                    if m:
                        self.funcs.setdefault(fid, []).append(matched_seconds(m))
                    elif call_ts is None or ts is None:
                        self.dropped_pairs += 1
                    else:
                        self.funcs.setdefault(fid, []).append((call_ts, ts))
                if workload_event == fid and ts is not None:
                    self.anchor.append(ts)
            elif mark:
                mid = int(mark.group(2), 16)
                if mark.group(1) == 'Stop':
                    m = RAN_RE.search(detail)
                    if m:
                        self.markers.setdefault(mid, []).append(matched_seconds(m))
                if mid == workload_marker and ts is not None:
                    if mark.group(1) == 'Start':
                        self.mark_start = ts if self.mark_start is None else self.mark_start
                        self.mark_open = True
                    elif self.mark_open:
                        self.mark_stop, self.mark_open = ts, False
            if workload_event is not None and not func and ts is not None \
                    and row['eventint'].strip() == str(workload_event):
                self.anchor.append(ts)
        self.dropped_pairs += sum(map(len, open_calls.values()))    # calls the capture ended inside
        if last is not None and self.stamps and self.stamps[-1] > last:
            runner = isrs[-1] if isrs else task
            if runner is not None:
                self.segments.append((last, self.stamps[-1], runner))

    def func_durations(self, freq):
        """Annotated durations as given; the rest from the call and return timestamps."""
        return {fid: [v if isinstance(v, float) else (v[1] - v[0]) / freq for v in vals]
                for fid, vals in self.funcs.items()}


def workload(scan, freq, args, warnings):
    """(window_s, anchor, busy rows) over the asked-for window; all None when unanchored or unusable."""
    if args.window:
        t0, t1 = args.window
        span = (scan.stamps[-1] - scan.stamps[0]) / freq
        if t1 > span:
            raise Usage(f'--window {t0:g}:{t1:g} ends past the capture, which spans {span:.3f} s')
        w0, w1, anchor = scan.stamps[0] + int(t0 * freq), scan.stamps[0] + int(t1 * freq), 'window'
    elif args.workload_marker is not None:
        if scan.mark_stop is None or scan.mark_open or scan.mark_stop <= scan.mark_start:
            warnings.append(f'marker {args.workload_marker}: no complete Start/Stop span in the capture (one left '
                            f'open, or none); no workload window')
            return None, None, None
        w0, w1, anchor = scan.mark_start, scan.mark_stop, 'marker'
    elif args.workload_event is not None:
        if len(scan.anchor) < 2 or scan.anchor[-1] <= scan.anchor[0]:
            warnings.append(f'event {args.workload_event}: fewer than two occurrences in the capture; '
                            f'no workload window')
            return None, None, None
        w0, w1, anchor = scan.anchor[0], scan.anchor[-1], 'event'
    else:
        return None, None, None
    window_s = round((w1 - w0) / freq, 6)
    lost = sum(1 for t0, t1 in scan.losses if t0 < w1 and (t1 is None or t1 > w0))
    if lost:
        warnings.append(f'{lost} span(s) after lost events or an untimed head overlap the workload window: who ran '
                        f'there is unknown, busy shares withheld')
        return window_s, anchor, None
    busy = {}
    for s0, s1, ctx in scan.segments:
        lo, hi = max(s0, w0), min(s1, w1)
        if hi > lo:
            busy[ctx] = busy.get(ctx, 0) + hi - lo
    rows = [{'name': ctx, 'busy_ms': round(t / freq * 1e3, 3), 'cpu_pct_workload': round(t / (w1 - w0) * 100, 2)}
            for ctx, t in sorted(busy.items(), key=lambda kv: -kv[1])]
    return window_s, anchor, rows


def report(args):
    out = Path(args.dir)
    warnings = []
    names = load_event_map(args.event_map) if args.event_map else {}
    ctx_path = out / 'contexts.csv'
    if not ctx_path.is_file():
        raise Usage(f'{ctx_path} not found: not a sysview_record.py output directory')
    result = {'contexts': read_contexts(ctx_path)}
    try:
        errors = json.loads((out / 'record.json').read_text()).get('systemviewErrors')
    except (OSError, ValueError, AttributeError):
        errors = None
    if errors:
        warnings.append(f'SystemView showed {errors} error dialog(s) while recording or loading (record.json, '
                        f'systemviewErrors): the export may be incomplete')
    result.update(dict.fromkeys(('isr', 'ready_run', 'functions', 'markers', 'stack', 'heap', 'overflow',
                                 'dropped_pairs', 'live_window_s', 'workload_window_s', 'workload_anchor',
                                 'contexts_workload')))
    anchored = args.window or args.workload_marker is not None or args.workload_event is not None
    ev_path = out / 'events.txt'
    freq, head = None, 0
    if not ev_path.is_file():
        if anchored:
            raise Usage('a workload window needs events.txt (recorded without --no-events)')
        warnings.append('no events.txt: event-derived and time-derived metrics withheld')
    else:
        rows = read_csv(ev_path, EVENT_COLUMNS, 'events')
        for r in rows:
            r['ts'] = timestamp(r)
        freq = time_base(rows, warnings)
        head = timed_from(rows) if freq else 0
        if head and not args.post_mortem:
            warnings.append(f'the first {head} events share the first timestamp with more than one context switch: '
                            f'if this is a decoded post-mortem ring they are untimed, pass --post-mortem')
            head = 0
        elif head:
            warnings.append(f'the first {head} events share one timestamp and are left out as untimed: '
                            f'event and time metrics cover the {len(rows) - head} timed events after them; '
                            f'contexts.csv times withheld')
            rows = rows[head:]
            if len(rows) < 2 or rows[-1]['ts'] <= rows[0]['ts']:
                warnings.append('the timed events span nothing: time-derived metrics withheld')
                freq = None
        scan = Scan(rows, args.workload_marker, args.workload_event, unknown_start=bool(head))
        heap = None
        if scan.heap:
            net, allocs, frees = map(int, scan.heap.groups())
            heap = {'allocs': allocs, 'frees': frees, 'net_bytes': net}
        result.update(stack=[{'name': k, 'bytes_used': v} for k, v in scan.stack.items()], heap=heap,
                      overflow=scan.overflow, dropped_pairs=scan.dropped_pairs)
    if freq is None or head:
        for c in result['contexts']:
            c.update(dict.fromkeys(CONTEXT_TIMES))
    if freq is None:
        if anchored and ev_path.is_file():
            warnings.append('no usable time base: no workload window')
        return {**result, 'warnings': warnings}
    window_s, anchor, busy = workload(scan, freq, args, warnings)
    result.update(isr=duration_rows(scan.isr), ready_run=duration_rows(scan.ready),
                  functions=duration_rows(scan.func_durations(freq), lambda fid: names.get(fid, f'#{fid}'), True),
                  markers=duration_rows(scan.markers, lambda mid: f'marker {mid}'),
                  live_window_s=round((scan.stamps[-1] - scan.stamps[0]) / freq, 6),
                  workload_window_s=window_s, workload_anchor=anchor, contexts_workload=busy)
    return {**result, 'warnings': warnings}


def na(v, fmt='{}'):
    return 'n/a' if v is None else fmt.format(v)


def print_text(r):
    print(f"{'context':<16} {'act':>7} {'cpu%':>7} {'total_ms':>10} {'blk_ms':>9} {'min_us':>8} {'avg_us':>8} "
          f"{'max_us':>8}")
    for c in r['contexts']:
        print(f"{c['name']:<16} {c['activations']:>7} {na(c['cpu_pct']):>7} {na(c['total_ms']):>10} "
              f"{na(c['blocked_ms']):>9} {na(c['min_us']):>8} {na(c['avg_us']):>8} {na(c['max_us']):>8}")
    print(f"\nlive_window_s: {na(r['live_window_s'])}")
    print(f"overflow: {na(r['overflow'])}" + (' -- events were lost: raise the RTT buffer or record less'
                                             if r['overflow'] else ''))
    print(f"dropped_pairs: {na(r['dropped_pairs'])}")
    if r['workload_anchor']:
        print(f"workload window ({r['workload_anchor']}): {r['workload_window_s']} s")
        for c in r['contexts_workload'] or ():
            print(f"  {c['name']:<16} {c['cpu_pct_workload']:>6.2f} %  busy {c['busy_ms']:.3f} ms")
    for label, key in (('ISR duration', 'isr'), ('ready->run', 'ready_run'), ('function', 'functions'),
                       ('marker', 'markers')):
        if r[key]:
            print(f"\n{label:<20} {'n':>7} {'p50_us':>8} {'p99_us':>8} {'max_us':>8}")
            for row in r[key]:
                print(f"{row['name']:<20} {row['n']:>7} {row['p50_us']:>8.1f} {row['p99_us']:>8.1f} "
                      f"{row['max_us']:>8.1f}")
    if r['stack']:
        print(f"\n{'stack high-water':<20} {'bytes_used':>10}")
        for row in r['stack']:
            print(f"{row['name']:<20} {row['bytes_used']:>10}")
    if r['heap']:
        print(f"\nheap: allocs={r['heap']['allocs']} frees={r['heap']['frees']} net_bytes={r['heap']['net_bytes']}")
    for w in r['warnings']:
        print(f'warning: {w}')


def window_arg(text):
    try:
        t0, t1 = (float(x) for x in text.split(':', 1))
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected T0:T1 in seconds, got {text!r}')
    if not 0 <= t0 < t1:
        raise argparse.ArgumentTypeError(f'need 0 <= T0 < T1, got {text!r}')
    return t0, t1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('dir', help='a sysview_record.py --out directory')
    p.add_argument('--json', action='store_true', help='one JSON object instead of tables')
    p.add_argument('--event-map', metavar='FILE', help='JSON object: absolute event id (decimal string) -> name')
    p.add_argument('--post-mortem', action='store_true',
                   help='a decoded post-mortem ring: leave out its untimed head')
    anchor = p.add_mutually_exclusive_group()
    anchor.add_argument('--window', type=window_arg, metavar='T0:T1', help='seconds from the first event')
    anchor.add_argument('--workload-marker', type=int, metavar='ID', help='first Start to last Stop of this marker')
    anchor.add_argument('--workload-event', type=int, metavar='ID', help='first to last occurrence of this event id')
    args = p.parse_args(argv)
    try:
        r = report(args)
    except Usage as e:
        p.error(str(e))
    if args.json:
        print(json.dumps(r, indent=1))
    else:
        print_text(r)
    return 0


if __name__ == '__main__':
    sys.exit(main())
