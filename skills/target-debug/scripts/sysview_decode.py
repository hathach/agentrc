#!/usr/bin/env python3
"""Decode single-core SystemView streams without the GUI or a debug probe.

  sysview_decode.py FILE --out DIR [--no-events]

Writes the events.txt and contexts.csv subset consumed by sysview_report.py, plus decode.json diagnostics.
The packet layout follows the pinned SEGGER_SYSVIEW.c; see tests/fixtures/sysview/README.md for sources and limits.
Exit: 0 decoded (inspect diagnostics for incomplete captures); 2 invalid input or output directory.
"""
import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SYNC = b'\0' * 10
EVENT_COLUMNS = ('sequencenum', 'timestamp', 'context', 'event', 'detail', 'timestampint',
                 'contextinint', 'contextint', 'contextoutint', 'eventint', 'eventoffset', 'eventsize', 'eventdata')
CONTEXT_COLUMNS = ('Name', 'Type', 'Core', 'Stack Information', 'Activations', 'Total Blocked Time',
                   'Total Run Time', 'Time Interrupted', 'CPU Load', 'Last Run Time', 'Min Run Time', 'Max Run Time')
ISR, TIMER, SCHEDULER, TASK, IDLE, LOST = (x << 32 for x in (1, 2, 4, 8, 16, 32))
# Implicit-size packets below 24: U32 parameters, then an optional counted string.
LAYOUT = {0: (0, False), 1: (1, False), 2: (1, False), 3: (0, False), 4: (1, False),
          5: (0, False), 6: (1, False), 7: (2, False), 8: (1, False), 9: (2, True),
          10: (0, False), 11: (0, False), 12: (1, False), 13: (2, False), 14: (0, True),
          15: (1, False), 16: (1, False), 17: (0, False), 18: (0, False), 19: (1, False),
          20: (0, False), 21: (4, False), 22: (1, True)}
NAMES = {0: 'NOP', 1: '*** Overflow ***', 2: 'ISR Enter', 3: 'ISR Exit', 4: 'Task Run',
         5: 'Task Stop', 6: 'Task Ready', 7: 'Task Block', 8: 'Task Create', 9: 'Task Info',
         10: 'Start', 11: 'Stop', 12: 'System Time (cycles)', 13: 'System Time (us)',
         14: 'System Description', 17: 'System Idle', 18: 'ISR Exit', 19: 'Timer Enter',
         20: 'Timer Exit', 21: 'Stack Info', 22: 'Module Description', 23: 'Data Sample',
         24: 'Init', 25: 'Resource Name', 26: 'Print', 27: 'Num Modules', 28: 'End Call', 29: 'Task Terminate'}
# SystemView 4.10b Description/SYSVIEW_FreeRTOS.txt; UM08027 pp. 38, 146 (see fixture claim record).
FREERTOS_STATES = {4: 'Delayed', 27: 'Suspended'}


def blocked_time_evidence(os_name):
    evidence = {'os': os_name, 'reason': 'No verified OS preemption-state rule for total blocked time.',
                'manual': 'UM08027 4.10 rev.0, sections 3.6 and 6.1.5.3, pp. 38, 146'}
    if os_name == 'FreeRTOS':
        evidence.update(source='SystemView_V410b/Description/SYSVIEW_FreeRTOS.txt',
                        sourceSha256='bed4b8dd7842b252ef4a371a6df28d7114b335da5c6ea0df1ef305e2ab5b7da0',
                        stateMask=255, taskStates={str(k): v for k, v in FREERTOS_STATES.items()},
                        preemptingTaskStates=None,
                        reason='The installed FreeRTOS description has no PreemptingTaskStates entry; its two '
                               'TaskState labels do not resolve the jl-wrapped cdc blocked-time gap.',
                        unresolvedExample={'capture': 'jl-wrapped', 'context': 'cdc', 'frequency': 168000000,
                                           'guiTicks': 859427, 'intervalTicks': 729886, 'gapTicks': 129541})
    return evidence


class DecodeError(ValueError):
    pass


class TimestampError(DecodeError):
    pass


class Reader:
    def __init__(self, data, pos=0, end=None):
        self.data, self.pos = data, pos
        self.end = len(data) if end is None else end

    def take(self, size):
        if size > self.end - self.pos:
            raise DecodeError('truncated packet')
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def uint(self):
        value = 0
        for shift in range(0, 35, 7):
            b = self.take(1)[0]
            if shift == 28 and b > 15:
                raise DecodeError('varint exceeds 32 bits')
            value |= (b & 127) << shift
            if b < 128:
                return value
        raise DecodeError('unterminated varint')

    def string(self):
        size = self.take(1)[0]
        if size == 255:
            size = int.from_bytes(self.take(2), 'big')
        return self.take(size).decode('utf-8', errors='replace')


@dataclass
class Packet:
    event: int
    args: list
    text: str
    delta: int
    offset: int
    data: bytes
    gap: bool = False
    problem: str = ''


def packet_at(data, offset):
    r = Reader(data, offset)
    event = r.uint()
    args, text, problem = [], '', ''
    if event < 24:
        if event == 23:
            args.append(r.uint())
            r.take(4)  # SEGGER_SYSVIEW_SampleData: IEEE754 float, not a varint.
        else:
            count, has_text = LAYOUT[event]
            args = [r.uint() for _ in range(count)]
            if has_text:
                text = r.string()
    else:
        size = r.uint()
        payload = Reader(r.take(size))
        try:
            if event in (25, 26):
                if event == 25:
                    args.append(payload.uint())
                text = payload.string()
            elif event == 31:
                args.append(payload.uint())
                if args[0] == 1:
                    args.append(payload.uint())
                    text = payload.string()
                elif args[0] not in (0, 2, 3, 4, 5):
                    payload.pos = payload.end  # Other extensions carry typed binary data.
            elif event not in (24, 27, 28, 29) and event < 32:
                payload.pos = payload.end
            if event >= 32:
                payload.pos = payload.end  # User/module payloads are opaque and need not contain varints.
            while payload.pos < payload.end:
                args.append(payload.uint())
            required = {24: 4, 27: 1, 28: 1, 29: 1}.get(event, 0)
            if len(args) < required:
                raise DecodeError(f'event {event}: missing parameters')
            if event == 24 and (len(args) != 4 or args[3] > 31):
                raise DecodeError('invalid Init packet')
            if event == 31 and args[0] in (0, 1, 2, 3, 4, 5):
                required = {0: 2, 1: 2, 2: 5, 3: 4, 4: 5, 5: 3}[args[0]]
                if len(args) != required:
                    raise DecodeError(f'extension {args[0]}: invalid parameter count')
        except DecodeError as e:
            problem = str(e)
            args, text = [], ''
    try:
        delta = r.uint()
    except DecodeError as e:
        raise TimestampError('timestamp delta: ' + str(e)) from e
    return Packet(event, args, text, delta, offset, data[offset:r.pos], problem=problem), r.pos


def find_sync(data, start):
    while True:
        offset = data.find(SYNC, start)
        if offset < 0:
            return -1, -1
        after = offset + len(SYNC)
        while after < len(data) and data[after] == 0:
            after += 1
        try:
            packet, _ = packet_at(data, after)
            if packet.event == 10:  # _SendSyncInfo and SEGGER_SYSVIEW_Start both emit Start next.
                return offset, after
        except DecodeError:
            pass
        start = after


def stream(data, diagnostics):
    """Only synchronize on the target's ten-zero delimiter; never guess a packet boundary in a wrapped head."""
    offset = 0
    if data.startswith(b';'):
        while data[offset:offset + 1] == b';':
            end = data.find(b'\n', offset)
            if end < 0:
                raise DecodeError('unterminated SVDat header')
            line = data[offset:end]
            if b'Offset Core' in line and b'Offset Core0 ' not in line:
                raise DecodeError('multicore recordings are not supported')
            offset = end + 1
    sync, after = find_sync(data, offset)
    if sync < 0:
        raise DecodeError('no SystemView synchronization packet (ten zero bytes)')
    if sync != offset:
        diagnostics.append({'offset': offset, 'bytes': sync - offset, 'reason': 'discarded unsynchronized head'})
    offset = after
    gap = False
    while offset < len(data):
        if data.startswith(SYNC, offset):
            offset += len(SYNC)
            continue
        if data[offset] == 0:
            diagnostics.append({'offset': offset, 'bytes': 1, 'reason': 'unexpected padding byte'})
            offset += 1
            continue
        start = offset
        try:
            packet, offset = packet_at(data, offset)
        except DecodeError as e:
            sync, after = find_sync(data, start + 1)
            end = len(data) if sync < 0 else sync
            diagnostic = {'offset': start, 'bytes': end - start, 'reason': str(e)}
            if sync >= 0 or isinstance(e, TimestampError):
                diagnostic['timingLost'] = True
            diagnostics.append(diagnostic)
            if sync < 0:
                return
            offset, gap = after, True
            continue
        packet.gap, gap = gap, False
        yield packet


@dataclass
class Context:
    name: str
    kind: str = ''
    stack: str = ''
    activations: int = 0
    total: int = 0
    interrupted: int = 0
    runs: list = field(default_factory=list)


@dataclass
class Frame:
    context: int
    start: int
    rows: list = field(default_factory=list)
    interrupted: int = 0


class Decoder:
    def __init__(self):
        self.rows, self.diagnostics = [], []
        self.contexts = {0: Context('Idle'), SCHEDULER: Context('Scheduler'), IDLE: Context('Idle'),
                         LOST: Context('*** OVERFLOW ***')}
        self.freq = self.shift = self.clock = 0
        self.stream_freq = 0
        self.time_lost = False
        self.timing_losses = []
        self.timed_events = None
        self.timed_clock = 0
        self.have_init = False
        self.os_name = ''
        self.current = Frame(0, 0)
        self.parents = []
        self.ready, self.calls, self.markers, self.heaps = {}, {}, {}, {}
        self.task_order, self.isr_order = [], []
        self.activated_ready = set()
        self.scheduler_pending = False

    def context(self, key):
        if key not in self.contexts:
            ident = key & 0xffffffff
            kind = key >> 32
            name = f'ISR {ident}' if kind == 1 else f'Timer 0x{ident:X}' if kind == 2 else f'Task 0x{ident:X}'
            self.contexts[key] = Context(name, f'#{ident}' if kind == 1 else '')
            (self.isr_order if kind in (1, 2) else self.task_order).append(key)
        return self.contexts[key]

    def duration(self, ticks):
        if not self.freq or self.time_lost:
            return ''
        if ticks * 1000 >= self.freq:
            us = ticks * 1_000_000 // self.freq
            return f'{us // 1000}.{us % 1000:03d} ms'
        ns = (2 * ticks * 1_000_000_000 + self.freq) // (2 * self.freq)
        return f'{ns // 1000}.{ns % 1000:03d} us'

    def end_frame(self, ts):
        f = self.current
        elapsed = ts - f.start
        if not self.time_lost and f.context not in (0, LOST) and elapsed >= 0:
            ctx = self.context(f.context)
            ctx.total += elapsed
            ctx.interrupted += f.interrupted
            exclusive = elapsed - f.interrupted
            if exclusive > 0:
                ctx.runs.append(exclusive)
            for row in f.rows:
                duration = self.duration(elapsed)
                if duration:
                    prefix = 'Idle for' if f.context == IDLE else 'Runs for'
                    row['detail'] = f'{prefix} {duration}'
        return elapsed

    def begin_frame(self, key, ts, row=None):
        if not self.time_lost and key not in self.activated_ready:
            self.context(key).activations += 1
        self.activated_ready.discard(key)
        self.current = Frame(key, ts, [row] if row is not None else [])

    def invalidate_heaps(self):
        if not self.heaps:
            return
        self.heaps.clear()
        for row in self.rows:
            if row['event'] in ('Allocate Memory', 'Free Memory'):
                row['detail'] = ''

    def lose(self):
        self.current = Frame(LOST, self.clock)
        self.parents.clear()
        self.ready.clear()
        for key in self.activated_ready:
            self.context(key).activations -= 1
        self.activated_ready.clear()
        self.scheduler_pending = False
        self.calls.clear()
        self.markers.clear()
        self.invalidate_heaps()

    def lose_time(self, offset, reason):
        if not self.time_lost:
            self.finish_partial(self.timed_clock)
            self.timed_events = len(self.rows)
            self.lose()
            self.time_lost = True
        self.timing_losses.append({'row': len(self.rows), 'offset': offset, 'reason': reason})

    def decode(self, data):
        first = True
        for p in stream(data, self.diagnostics):
            self.clock += 0 if first else p.delta
            first = False
            if p.gap:
                gap = self.diagnostics[-1]
                self.lose_time(gap['offset'], gap['reason'])
            if p.problem:
                self.calls.clear()
                self.markers.clear()
                self.invalidate_heaps()
                self.diagnostics.append({'offset': p.offset, 'bytes': len(p.data),
                                         'reason': 'malformed payload: ' + p.problem})
            event, args = p.event, p.args
            ts = int(self.rows[-1]['timestampint']) + 1 if event == 1 and self.rows else self.clock
            if event == 24 and not p.problem:
                freq, cpu, base, shift = args
                if self.stream_freq and self.stream_freq != freq:
                    self.lose_time(p.offset, 'timestamp frequency changed')
                    self.diagnostics.append({'offset': p.offset, 'reason': 'timestamp frequency changed'})
                if not self.time_lost:
                    self.freq = freq
                self.stream_freq = freq
                self.shift = shift
                self.have_init = True
            if not self.time_lost:
                self.timed_clock = self.clock
            row = {'sequencenum': len(self.rows), 'timestamp': '', 'context': '',
                   'event': NAMES.get(event, f'Function #{event}' if event >= 512 else f'Event #{event}'),
                   'detail': '', 'timestampint': ts, 'contextinint': self.current.context,
                   'contextint': self.current.context, 'contextoutint': self.current.context,
                   'eventint': event, 'eventoffset': p.offset, 'eventsize': len(p.data),
                   'eventdata': p.data.hex(' ').upper() + ' '}
            self.rows.append(row)
            if p.problem:
                row['detail'] = 'Malformed payload: ' + p.problem
                continue
            if event == 1:
                self.lose()
                row.update(contextinint=LOST, contextint=LOST, contextoutint=LOST,
                           detail=f'{args[0]} Events lost.')
            elif event in (2, 19):
                key = (ISR if event == 2 else TIMER) | args[0]
                self.parents.append(self.current)
                self.begin_frame(key, ts, row)
                row.update(contextint=key, contextoutint=key)
            elif event in (3, 18, 20):
                if self.current.context >> 32 in (1, 2):
                    elapsed = self.end_frame(ts)
                    parent = self.parents.pop() if self.parents else Frame(LOST, ts)
                    if event != 20:
                        parent.interrupted += max(0, elapsed)
                    self.current = parent
                if event == 18:
                    self.scheduler_pending = True
                if self.scheduler_pending and not self.parents:
                    if self.current.context != SCHEDULER:
                        self.end_frame(ts)
                        self.begin_frame(SCHEDULER, ts)
                    self.scheduler_pending = False
                row['contextoutint'] = self.current.context
                row['detail'] = f'Returns to {self.context(self.current.context).name}'
            elif event in (4, 17):
                self.scheduler_pending = False
                key = (TASK | args[0]) if event == 4 else IDLE
                if self.current.context != key:
                    self.end_frame(ts)
                    self.parents.clear()
                    self.begin_frame(key, ts, row)
                else:
                    self.current.rows.append(row)
                row.update(contextint=key, contextoutint=key)
                if key in self.ready:
                    ready = self.ready.pop(key)
                    duration = self.duration(ts - ready['timestampint'])
                    if duration:
                        ready['detail'] += f', runs after {duration}'
            elif event == 5:
                self.end_frame(ts)
                self.begin_frame(SCHEDULER, ts)
                row['contextoutint'] = SCHEDULER
            elif event == 6:
                key = TASK | args[0]
                row['detail'] = self.context(key).name
                running = self.parents[0].context if self.parents else self.current.context
                if not self.time_lost and key != running and key not in self.ready:
                    self.ready[key] = row
                    self.context(key).activations += 1
                    self.activated_ready.add(key)
            elif event == 7:
                key = TASK | args[0]
                row['detail'] = (FREERTOS_STATES.get(args[1] & 255, f'Reason={args[1]}')
                                 if self.os_name == 'FreeRTOS' else f'Reason={args[1]}')
                if self.current.context != key:
                    row['detail'] = f'{self.context(key).name}, {row["detail"]}'
                self.ready.pop(key, None)
                self.activated_ready.discard(key)
                if self.current.context == key:
                    self.end_frame(ts)
                    self.begin_frame(SCHEDULER, ts)
                    row['contextoutint'] = SCHEDULER
            elif event == 8:
                ctx = self.context(TASK | args[0])
                row['detail'] = f'{ctx.name} (0x{args[0]:08X})'
            elif event == 9:
                ctx = self.context(TASK | args[0])
                ctx.name, ctx.kind = p.text, f'@{args[1]}'
                row['detail'] = f'{p.text} (0x{args[0]:08X}): Priority={args[1]}'
            elif event == 11:
                self.scheduler_pending = False
                self.finish_partial(ts)
                self.current, self.parents = Frame(0, ts), []
                row['contextoutint'] = 0
            elif event == 12:
                row['detail'] = f'{args[0]} cycles'
            elif event == 13:
                row['detail'] = f'{args[0] | args[1] << 32} us'
            elif event == 14:
                row['detail'] = p.text
                os_name = re.search(r'(?:^|,)O=([^,]+)', p.text)
                if os_name:
                    self.os_name = os_name[1]
                for ident, name in re.findall(r'I#(\d+)=([^,]+)', p.text):
                    self.context(ISR | int(ident)).name = name
            elif event in (15, 16):
                ident = args[0]
                row['event'] = f'{"Start" if event == 15 else "Stop"} Marker 0x{ident:08X}'
                if event == 15:
                    self.markers[ident] = ts
                elif ident in self.markers:
                    duration = self.duration(ts - self.markers.pop(ident))
                    if duration:
                        row['detail'] = f'Ran for {duration}'
            elif event == 21:
                ident, base, size, usage = args
                ctx = self.context(TASK | ident)
                ctx.stack = f'{size} @ 0x{base:08X}'
                row['detail'] = f'{ctx.name} (0x{ident:08X}): {ctx.stack}'
                if usage:
                    row['detail'] += f', {usage} Bytes used'
            elif event == 24:
                row['detail'] = f'Cycle Freq.: {freq}, CPU Freq.: {cpu}, ID Base: 0x{base:08X}, ID Shift: {shift}'
            elif event == 25:
                row['detail'] = f'0x{args[0]:08X}: {p.text}'
            elif event == 26:
                row['detail'] = p.text
            elif event == 27:
                if args[0]:
                    raise DecodeError('registered module descriptions are not supported; use --gui')
                row['detail'] = f'Registered Modules: {args[0]}'
            elif event == 28:
                ident = args[0]
                row['event'] = f'Function #{ident}' if ident >= 512 or (ident >= 32 and not self.os_name) else f'Event #{ident}'
                row['detail'] = 'Returns'
                key = (ident, self.current.context)
                if self.calls.get(key):
                    start = self.calls[key].pop()
                    duration = self.duration(ts - start)
                    if duration:
                        row['detail'] += f' after {duration}'
            elif event == 31:
                self.extension(row, args, p.text)
            elif event >= 512 or (event >= 32 and not self.os_name):
                row['event'] = f'Function #{event}'
                self.calls.setdefault((event, self.current.context), []).append(ts)
        if not self.rows:
            raise DecodeError('no complete events after synchronization')
        for diagnostic in self.diagnostics:
            if diagnostic.get('timingLost') and not any(x['offset'] == diagnostic['offset'] for x in self.timing_losses):
                self.lose_time(diagnostic['offset'], diagnostic['reason'])
        self.finish_partial(self.clock)
        if self.timed_events is None:
            self.timed_events = len(self.rows)
        # Init has no epoch; cycles wrap, and pfGetTime is sampled separately from the packet timestamp.
        for i, row in enumerate(self.rows):
            if i >= self.timed_events:
                row['timestampint'] = ''
            row['context'] = self.context(row['contextint']).name
            if self.os_name and row['event'].startswith('Function #'):
                ident = int(row['event'].split('#')[1])
                if ident < 512:
                    row['event'] = f'Event #{ident}'
            if self.freq and row['timestampint'] != '':
                row['timestamp'] = f'{row["timestampint"] / self.freq:.9f}'
            if row['eventint'] == 8:
                p, _ = packet_at(data, row['eventoffset'])
                row['detail'] = f'{self.context(TASK | p.args[0]).name} (0x{p.args[0]:08X})'
            for key in ('contextinint', 'contextint', 'contextoutint'):
                row[key] = f'0x{row[key]:016X}'
        return self

    def finish_partial(self, ts):
        if self.time_lost:
            return
        for frame in [*self.parents, self.current]:
            if frame.context not in (0, LOST):
                ctx = self.context(frame.context)
                ctx.total += ts - frame.start
                ctx.interrupted += frame.interrupted

    def extension(self, row, args, text):
        kind = args[0]
        if kind in (0, 1):
            row['event'] = f'Marker 0x{args[1]:X}' if kind == 0 else 'Marker Name'
            row['detail'] = text
        elif kind == 2:
            _, heap, base, size, metadata = args
            if not self.have_init:
                return
            self.heaps[heap] = {'size': size << self.shift, 'metadata': metadata,
                                'blocks': {}, 'allocations': 0, 'frees': 0}
            row['event'] = 'Define Heap'
        elif kind in (3, 4, 5):
            heap, address = args[1:3]
            row['event'] = 'Free Memory' if kind == 5 else 'Allocate Memory'
            h = self.heaps.get(heap)
            if h is None:
                return
            if kind == 5:
                if address not in h['blocks']:
                    self.invalidate_heaps()
                    return
                del h['blocks'][address]
                h['frees'] += 1
            else:
                if address in h['blocks']:
                    self.invalidate_heaps()
                    return
                h['blocks'][address] = (args[3] << self.shift) + h['metadata']
                h['allocations'] += 1
            used = sum(h['blocks'].values())
            if used > h['size'] or not h['size']:
                self.invalidate_heaps()
                return
            row['detail'] = (f"{used} used, {h['size'] - used} free, {used / h['size'] * 100:.2f}% full -- "
                             f"{h['allocations']} allocations, {h['frees']} frees, difference "
                             f"{h['allocations'] - h['frees']}")

    def context_rows(self):
        rows = []
        span = self.timed_clock
        for key in [*self.isr_order, SCHEDULER, *self.task_order, IDLE]:
            c = self.contexts[key]
            timed = self.freq and self.timed_events
            def seconds(ticks, unit='s'):
                if not timed:
                    return ''
                ns = (2 * ticks * 1_000_000_000 + self.freq) // (2 * self.freq)
                scale, precision = (1_000_000_000, 9) if unit == 's' else (1_000_000, 6)
                return f'{ns // scale}.{ns % scale:0{precision}d} {unit}'
            rows.append({'Name': c.name, 'Type': c.kind, 'Core': 'Core 0', 'Stack Information': c.stack,
                         'Activations': c.activations, 'Total Blocked Time': '',
                         'Total Run Time': seconds(c.total), 'Time Interrupted': seconds(c.interrupted),
                         'CPU Load': f'{(c.total - c.interrupted) / span * 100:.2f} %' if timed and span else '',
                         'Last Run Time': seconds(c.runs[-1] if c.runs else 0),
                         'Min Run Time': seconds(min(c.runs) if c.runs else 0, 'ms'),
                         'Max Run Time': seconds(max(c.runs) if c.runs else 0, 'ms')})
        return rows


def write_csv(path, columns, rows):
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def decode_file(raw, out, no_events=False):
    result = Decoder().decode(Path(raw).read_bytes())
    out = Path(out)
    write_csv(out / 'contexts.csv', CONTEXT_COLUMNS, result.context_rows())
    if not no_events:
        write_csv(out / 'events.txt', EVENT_COLUMNS, result.rows)
    metadata = {'decoder': 'native', 'source': str(raw), 'events': len(result.rows),
                'timestampFrequency': result.freq or None, 'timingLost': result.time_lost,
                'timedEvents': result.timed_events, 'timingLosses': result.timing_losses,
                'withheldMetrics': {'blockedTime': blocked_time_evidence(result.os_name)} if result.task_order else {},
                'diagnostics': result.diagnostics}
    (out / 'decode.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    return metadata


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('file', type=Path)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--no-events', action='store_true')
    a = p.parse_args(argv)
    try:
        if a.out.exists() and (not a.out.is_dir() or any(a.out.iterdir())):
            raise DecodeError('--out must be a new or empty directory')
        a.out.mkdir(parents=True, exist_ok=True)
        print(json.dumps(decode_file(a.file, a.out, a.no_events)))
    except (OSError, DecodeError) as e:
        p.error(str(e))
    return 0


if __name__ == '__main__':
    sys.exit(main())
