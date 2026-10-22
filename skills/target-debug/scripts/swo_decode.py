#!/usr/bin/env python3
"""Decode a raw SWO capture into events and summaries: the ITM and DWT packet protocol of ARMv7-M
Appendix D (Armv8-M Chapter F1), as the target-debug skill's swo-dwt.md uses it.

  swo_decode.py <capture dir> [--elf FW.elf] [--jsonl OUT] [--json] [--top N]
  swo_decode.py --bin FILE --start synced|unsynced [--ts-clock-hz HZ --ts-prescale N] [...]

A capture dir is what swo_capture.py writes: swo.bin plus capture.json, which gives where the stream
starts (synced: capture began before any source was enabled, so byte 0 is a packet boundary), the
timestamp clock and prescaler, and the architecture. --bin decodes a capture made by hand; its start
and clock are then the caller's to state, never assumed.

Reports, each over the whole capture:
  INTEGRITY   always: bytes, packets, syncs, overflows, skipped and unknown bytes, segments
  EXCEPTIONS  per exception: entries, and with a timestamp clock, duration and self-time percentiles
  PC          periodic PC samples by function (--elf), sleep and prohibited samples
  DATA        DWT data trace per comparator: PC, address offset, read and write values
  TEXT        ITM stimulus per port: byte count, and the bytes as text
  GLOBAL TIMESTAMPS  complete global timestamps, first and last, and their clock against local time
  EVENTS      DWT event counter packets per counter: wraps of each 8-bit counter, Cyc periods, and with
              local timestamps the share of the span each cycle counter accounts for

An Overflow packet, a resync or an unknown header ends a segment: data was lost there, so exception
episodes never pair across it and durations are only computed inside one segment. Durations need
local timestamps and a timestamp clock; without either the counts are still exact and the
durations are withheld with the reason.

Exit 0 decoded, 1 the input held no packet at all, 2 bad usage or an unreadable input.
"""
import argparse
import bisect
import collections
import json
import os
import sys

TC_QUALITY = {0: 'sync', 1: 'ts-delayed', 2: 'data-delayed', 3: 'both-delayed'}
EXC_FN = {1: 'enter', 2: 'exit', 3: 'return'}
SYSTEM_EXCEPTIONS = {0: 'Thread', 1: 'Reset', 2: 'NMI', 3: 'HardFault', 4: 'MemManage', 5: 'BusFault',
                     6: 'UsageFault', 7: 'SecureFault', 11: 'SVCall', 12: 'DebugMonitor', 14: 'PendSV',
                     15: 'SysTick'}
EVENT_BITS = ('CPI', 'Exc', 'Sleep', 'LSU', 'Fold', 'Cyc')
SYNC_ZEROS = 5   # 47 zero bits then a one: five 0x00 bytes and 0x80 (Appendix D.2.1)


class Usage(Exception):
    pass


# ---------------------------------------------------------------------------------------------------
# packets
# ---------------------------------------------------------------------------------------------------

def find_sync(data, i):
    """(start, end) of the next synchronization packet at or after i, or None."""
    zeros = 0
    while i < len(data):
        b = data[i]
        if b == 0:
            zeros += 1
        elif b == 0x80 and zeros >= SYNC_ZEROS:
            return i - zeros, i + 1
        else:
            zeros = 0
        i += 1
    return None


def continued(data, i, limit):
    """Payload bytes from i following the C bit (bit 7) of each byte, at most limit; None if cut."""
    out = []
    while len(out) < limit:
        if i + len(out) >= len(data):
            return None
        b = data[i + len(out)]
        out.append(b)
        if not b & 0x80:
            break
    return out


def septets(payload):
    v = 0
    for n, b in enumerate(payload):
        v |= (b & 0x7F) << (7 * n)
    return v


def source_packet(h, payload):
    value = int.from_bytes(bytes(payload), 'little')
    a = h >> 3
    if not h & 0x04:
        return {'kind': 'stim', 'port': a, 'size': len(payload), 'value': value}
    if a == 0:
        return {'kind': 'evcnt', 'bits': value & 0x3F}
    if a == 1:
        return {'kind': 'exc', 'num': value & 0x1FF, 'fn': (value >> 12) & 0x3}
    if a == 2:
        if len(payload) == 4:
            return {'kind': 'pc', 'pc': value}
        if len(payload) == 1 and value in (0x00, 0xFF):
            return {'kind': 'pc', 'pc': None, 'state': 'sleep' if value == 0 else 'prohibited'}
        return None
    if 8 <= a <= 23:
        cmp = (a >> 1) & 0x3
        if a < 16:
            if a & 1:
                return {'kind': 'daddr', 'cmp': cmp, 'size': len(payload), 'value': value}
            return {'kind': 'dpc', 'cmp': cmp, 'size': len(payload), 'value': value}
        return {'kind': 'dval', 'cmp': cmp, 'access': 'write' if a & 1 else 'read', 'size': len(payload),
                'value': value}
    return None


def packets(data, synced):
    """Packets of the stream in order, each a dict with 'off' and 'kind'. A stream that does not start
    synced is read from its first synchronization packet; an unknown header or a source packet with an
    undefined discriminator is reported as 'unknown' and decoding resumes at the next synchronization
    packet ('resync'), since the size of what follows cannot be known."""
    i = 0
    if not synced:
        found = find_sync(data, 0)
        if found is None:
            yield {'off': 0, 'kind': 'unsynced', 'bytes': len(data)}
            return
        if found[0]:
            yield {'off': 0, 'kind': 'unsynced', 'bytes': found[0]}
        yield {'off': found[0], 'kind': 'sync'}
        i = found[1]
    page = 0
    while i < len(data):
        off, h = i, data[i]
        ss = h & 0x3
        pkt = None
        size = 1
        if h == 0x00:
            found = find_sync(data, i)
            if found is not None and found[0] == i:
                pkt, size, page = {'kind': 'sync'}, found[1] - i, 0
        elif ss:
            n = {1: 1, 2: 2, 3: 4}[ss]
            if i + 1 + n > len(data):
                yield {'off': off, 'kind': 'truncated', 'bytes': len(data) - i}
                return
            pkt = source_packet(h, data[i + 1:i + 1 + n])
            if pkt and pkt['kind'] == 'stim':
                pkt['port'] += 32 * page
            size = 1 + n
        elif h == 0x70:
            pkt = {'kind': 'overflow'}
        elif h & 0x8F == 0:
            pkt = {'kind': 'lts', 'delta': h >> 4, 'tc': 0}
        elif h & 0xCF == 0xC0:
            payload = continued(data, i + 1, 4)
            if payload is None:
                yield {'off': off, 'kind': 'truncated', 'bytes': len(data) - i}
                return
            pkt, size = {'kind': 'lts', 'delta': septets(payload), 'tc': (h >> 4) & 0x3}, 1 + len(payload)
        elif h in (0x94, 0xB4):
            payload = continued(data, i + 1, 4 if h == 0x94 else 6)
            if payload is None:
                yield {'off': off, 'kind': 'truncated', 'bytes': len(data) - i}
                return
            pkt = {'kind': 'gts1' if h == 0x94 else 'gts2', 'value': septets(payload), 'bytes': len(payload)}
            if h == 0x94 and len(payload) == 4:
                pkt['value'] = septets(payload[:3]) | (payload[3] & 0x1F) << 21
                pkt['wrap'], pkt['clkch'] = bool(payload[3] & 0x40), bool(payload[3] & 0x20)
            size = 1 + len(payload)
        elif h & 0x0B == 0x08:
            payload = continued(data, i + 1, 4) if h & 0x80 else []
            if payload is None:
                yield {'off': off, 'kind': 'truncated', 'bytes': len(data) - i}
                return
            ex = (h >> 4) & 0x7
            pkt, size = {'kind': 'ext', 'sh': (h >> 2) & 1, 'ex': ex, 'payload': septets(payload)}, 1 + len(payload)
            if not h & 0x80 and not pkt['sh']:
                page = ex
        if pkt is None:
            found = find_sync(data, i + 1)
            yield {'off': off, 'kind': 'unknown', 'header': h, 'bytes': (found[0] if found else len(data)) - off}
            if found is None:
                return
            yield {'off': found[0], 'kind': 'resync'}
            page, i = 0, found[1]
            continue
        pkt['off'] = off
        yield pkt
        i += size


# ---------------------------------------------------------------------------------------------------
# events: timestamps and segments
# ---------------------------------------------------------------------------------------------------

def events(pkts, sources=None, teardown=None):
    """(events, integrity): every non-protocol packet with its segment and local timestamp (ticks
    since the start of the capture, the delta sum of the Local timestamp packets that followed it), and
    the integrity counts. A segment ends at an overflow, unknown header or resync, and at a packet of
    a source not in `sources` (the enabled ones, when known). Overflows from byte `teardown` on, where the
    capture began switching its sources off, are counted apart."""
    out, pending = [], []
    t, seg = 0, 0
    gts_low = gts_high = None   # global timestamp bits [25:0] and [47:26], while known (Appendix D.2.5)
    awaiting_high, last_src = [], None
    integ = collections.Counter()
    unknown = []
    for p in pkts:
        k = p['kind']
        integ['packets'] += 1
        if k == 'lts':
            t += p['delta']
            integ['lts'] += 1
            for e in pending:
                e['t'], e['tq'] = t, TC_QUALITY[p['tc']]
            pending = []
            continue
        if k in ('sync', 'overflow', 'unknown', 'resync', 'unsynced', 'truncated', 'ext', 'gts1', 'gts2'):
            integ['teardown_overflow' if k == 'overflow' and teardown is not None and p['off'] >= teardown else k] += 1
            if k == 'unknown':
                unknown.append({'off': p['off'], 'header': f"0x{p['header']:02x}", 'bytes': p['bytes']})
                integ['unknown_bytes'] += p['bytes']
            elif k in ('unsynced', 'truncated'):
                integ[k + '_bytes'] += p['bytes']
            if k in ('overflow', 'unknown', 'resync'):
                seg, pending = seg + 1, []   # time was lost: the next timestamp is not theirs
                gts_low, gts_high, last_src, awaiting_high = None, None, None, []   # nor global time across it
            if k == 'gts1':
                # GTS1 omits high-order bytes unchanged since the last one; Wrap: bits [47:26] changed, a GTS2 follows,
                # giving them for the latest wrapping GTS1 only (ARMv7-M C1-847)
                width = 26 if p['bytes'] == 4 else 7 * p['bytes']
                gts_low = p['value'] if width == 26 else None if gts_low is None else \
                    gts_low & ~((1 << width) - 1) | p['value']
                if p.get('wrap'):
                    gts_high, awaiting_high = None, []
                # it follows the non-delayed trace packet whose time it is (C1-847): `src` indexes that packet
                e = dict(p, seg=seg, low=gts_low, src=last_src)
                if gts_low is not None and gts_high is not None:
                    e['gts'] = gts_high << 26 | gts_low
                elif gts_low is not None:
                    awaiting_high.append(e)
                out.append(e)
            elif k == 'gts2':
                gts_high = p['value']
                for e in awaiting_high:
                    e['gts'] = gts_high << 26 | e['low']
                awaiting_high = []
                out.append(dict(p, seg=seg))
            continue
        if sources is not None and k not in sources:
            # a source the capture never enabled: a corrupted stream that still framed
            integ['unexpected'] += 1
            unknown.append({'off': p['off'], 'unexpected': k})
            seg, pending = seg + 1, []
            gts_low, gts_high, last_src, awaiting_high = None, None, None, []
            continue
        e = dict(p, seg=seg, t=None)
        last_src = len(out)
        out.append(e)
        pending.append(e)
    integ['segments'] = seg + 1
    integ['untimed'] = sum(1 for e in out if e.get('t') is None and e['kind'] not in ('gts1', 'gts2'))
    return out, integ, unknown


# ---------------------------------------------------------------------------------------------------
# ELF names
# ---------------------------------------------------------------------------------------------------

def probe_state_module():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import probe_state
    return probe_state


class Symbols:
    """Function lookup by address and exception names from the ELF's vector table, read with
    probe_state.py's ELF reader (no toolchain)."""

    def __init__(self, elf):
        ps = probe_state_module()
        syms, _ = ps.elf_symbols(elf)
        funcs = sorted((v & ~1, s, n) for n, v, s, typ, shndx in syms if typ == 2 and shndx and n)
        self.starts = [f[0] for f in funcs]
        self.funcs = funcs
        self.by_addr = {}
        for start, _, name in funcs:
            self.by_addr.setdefault(start, name)
        segs, _ = ps.image_segments(elf, None)
        base, image = min(segs)
        self.vectors = {n: int.from_bytes(image[4 * n:4 * n + 4], 'little') & ~1
                        for n in range(1, min(len(image) // 4, 512))}

    def function(self, addr):
        i = bisect.bisect_right(self.starts, addr) - 1
        if i >= 0:
            start, size, name = self.funcs[i]
            if addr < start + max(size, 1):
                return name
        return f'0x{addr:08x}'

    def exception(self, num):
        handler = self.by_addr.get(self.vectors.get(num))
        return handler or exception_name(num)


def exception_name(num):
    return SYSTEM_EXCEPTIONS.get(num, f'IRQ{num - 16}' if num >= 16 else f'Exception{num}')


# ---------------------------------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------------------------------

def percentile(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))]


def stamp(e):
    """The event's timestamp when synchronous, 'delayed' when its packet or timestamp was delayed
    (TC != 00), None without one."""
    if e['t'] is None:
        return None
    return e['t'] if e['tq'] == 'sync' else 'delayed'


def exception_report(evts, clock):
    """Per exception number: entries, and durations (enter to exit) and self times (less the nested
    episodes inside) for episodes whose enter and exit both carry a synchronous timestamp in the same
    segment. Other episodes are counted as 'delayed' or 'untimed'; a self time is only computed when
    every nested episode was timed."""
    per = collections.defaultdict(lambda: {'enter': 0, 'exit': 0, 'return': 0, 'delayed': 0, 'untimed': 0,
                                           'dur': [], 'self': []})
    anomalies = collections.Counter()
    stack, seg = [], 0
    for e in evts:
        if e['kind'] != 'exc':
            continue
        if e['fn'] not in EXC_FN:
            anomalies['reserved-fn'] += 1
            continue
        if e['seg'] != seg:
            stack, seg = [], e['seg']
        fn, num = EXC_FN[e['fn']], e['num']
        per[num][fn] += 1
        if fn == 'enter':
            stack.append({'num': num, 't': stamp(e), 'nested': 0, 'nested_timed': True})
        elif fn == 'exit':
            if not stack or stack[-1]['num'] != num:
                anomalies['exit-without-enter'] += 1
                stack = []
                continue
            ep, t1 = stack.pop(), stamp(e)
            timed = isinstance(ep['t'], int) and isinstance(t1, int)
            if timed:
                dur = t1 - ep['t']
                per[num]['dur'].append(dur)
                if ep['nested_timed']:
                    per[num]['self'].append(dur - ep['nested'])
            else:
                per[num]['delayed' if 'delayed' in (ep['t'], t1) else 'untimed'] += 1
            if stack:
                if timed:
                    stack[-1]['nested'] += dur
                else:
                    stack[-1]['nested_timed'] = False
        elif num == 0 and stack:
            anomalies['thread-return-with-active-handler'] += 1
            stack = []
        elif num and (not stack or stack[-1]['num'] != num):
            anomalies['return-to-inactive'] += 1
    out = {}
    for num, r in sorted(per.items()):
        row = {k: r[k] for k in ('enter', 'exit', 'return', 'delayed', 'untimed')}
        if clock:
            scale = 1e6 / clock
            for name, vals in (('dur_us', r['dur']), ('self_us', r['self'])):
                if vals:
                    row[name] = {q: round(percentile(vals, p) * scale, 3)
                                 for q, p in (('p50', 0.5), ('p99', 0.99), ('max', 1.0))}
                    row[name]['n'] = len(vals)
        out[num] = row
    return out, dict(anomalies)


def pc_report(evts, syms, top):
    counts = collections.Counter()
    states = collections.Counter()
    for e in evts:
        if e['kind'] == 'pc':
            if e['pc'] is None:
                states[e['state']] += 1
            else:
                counts[syms.function(e['pc']) if syms else f"0x{e['pc']:08x}"] += 1
    total = sum(counts.values()) + sum(states.values())
    return {'samples': total, 'sleep': states['sleep'], 'prohibited': states['prohibited'],
            'top': counts.most_common(top)}


def data_report(evts, syms):
    out = collections.defaultdict(lambda: collections.Counter())
    pcs = collections.defaultdict(collections.Counter)
    offsets = collections.defaultdict(collections.Counter)
    last = {}
    for e in evts:
        if e['kind'] in ('dpc', 'daddr', 'dval'):
            key = e['kind'] if e['kind'] != 'dval' else e['access']
            out[e['cmp']][key] += 1
            if e['kind'] == 'dpc':
                pcs[e['cmp']][syms.function(e['value']) if syms else f"0x{e['value']:08x}"] += 1
            if e['kind'] == 'daddr':   # data address bits [15:0]
                offsets[e['cmp']][f"0x{e['value']:04x}"] += 1
            if e['kind'] == 'dval':
                last[e['cmp']] = e['value']
    return {c: dict(out[c], last_value=last.get(c), pcs=pcs[c].most_common(5), offsets=offsets[c].most_common(8))
            for c in sorted(out)}


def text_report(evts):
    ports = collections.defaultdict(bytearray)
    sizes = collections.defaultdict(collections.Counter)
    for e in evts:
        if e['kind'] == 'stim':
            ports[e['port']] += e['value'].to_bytes(e['size'], 'little')
            sizes[e['port']][e['size']] += 1
    return {p: {'bytes': len(b), 'writes': dict(sizes[p]), 'text': b.decode('latin-1')}
            for p, b in sorted(ports.items())}


def event_report(evts):
    """{counter: packets}: one packet per 8-bit wrap of CPI, Exc, Sleep, LSU and Fold (256 counts each), one per
    POSTCNT period for Cyc."""
    c = collections.Counter()
    for e in evts:
        if e['kind'] == 'evcnt':
            for n, name in enumerate(EVENT_BITS):
                if e['bits'] >> n & 1:
                    c[name] += 1
    return dict(c)


def event_shares(evts, ticks_hz, clock_hz):
    """{counter: share of the processor's cycles} for the counters that count cycles (all but Fold, which counts
    instructions, and Cyc): 256 counts per wrap received after a counter's first synchronously timestamped wrap
    in segment 0 up to its last, over the cycles between those two."""
    out = {}
    for n, name in enumerate(EVENT_BITS):
        if name not in ('CPI', 'Exc', 'Sleep', 'LSU'):
            continue
        wraps = [e for e in evts if e['kind'] == 'evcnt' and e['bits'] >> n & 1 and e['seg'] == 0]
        timed = [i for i, e in enumerate(wraps) if e.get('tq') == 'sync']
        if len(timed) > 1 and wraps[timed[-1]]['t'] > wraps[timed[0]]['t']:
            cycles = (wraps[timed[-1]]['t'] - wraps[timed[0]]['t']) / ticks_hz * clock_hz
            out[name] = round((timed[-1] - timed[0]) * 256 / cycles, 4)
    return out


def periodic_missed(evts, periods, prescale):
    """{source: packets missing}: a periodic source ('pc' samples, 'Cyc' events) whose consecutive packets, both
    synchronously timestamped in one segment, lie k periods apart lost k - 1 between them. Measured on an STM32F407:
    such packets go missing without an Overflow packet, which ARMv7-M Appendix D.2.3 would have the DWT emit."""
    out = {}
    for name, period in (periods or {}).items():
        mine = [e for e in evts if (e['kind'] == 'pc' if name == 'pc' else e['kind'] == 'evcnt' and e['bits'] >> 5 & 1)]
        missed = 0
        for a, b in zip(mine, mine[1:]):   # neighbours as received: a delayed one between breaks the comparison
            if a['seg'] == b['seg'] and a.get('tq') == b.get('tq') == 'sync':
                missed += max(0, round((b['t'] - a['t']) * prescale / period) - 1)
        out[name] = missed
    return out


def global_report(evts, ticks_hz):
    """Global timestamps: how many complete ones, the first and last, and the global clock they imply against
    the local time of the trace packets they follow (segment 0, synchronously timestamped)."""
    full = [e for e in evts if e['kind'] == 'gts1' and 'gts' in e]
    if not full:
        return None
    out = {'complete': len(full), 'first': full[0]['gts'], 'last': full[-1]['gts']}
    timed = [(e['gts'], evts[e['src']]['t']) for e in full if e['seg'] == 0 and e['src'] is not None
             and evts[e['src']].get('tq') == 'sync']
    if ticks_hz and len(timed) > 1 and timed[-1][1] > timed[0][1]:
        out['clock_hz'] = round((timed[-1][0] - timed[0][0]) * ticks_hz / (timed[-1][1] - timed[0][1]))
    return out


def summarize(data, meta, syms, top):
    evts, integ, unknown = events(packets(data, meta['synced']), meta.get('sources'), meta.get('teardown_byte'))
    clock = meta.get('ts_clock_hz')
    ticks_hz = clock / meta['ts_prescale'] if clock else None
    timing = None
    if not integ['lts']:
        timing = 'no local timestamp packets in the stream (timestamps were off)'
    elif not clock:
        timing = 'no timestamp clock given'
    exc, anomalies = exception_report(evts, ticks_hz if not timing else None)
    if integ['lts'] and meta.get('periods'):
        integ['periodic_missed'] = periodic_missed(evts, meta['periods'], meta['ts_prescale'])
    summary = {
        'integrity': dict(integ, bytes=len(data), unknown=unknown[:20]),
        'timing': timing or f"local timestamps at {clock} Hz / {meta['ts_prescale']} ({meta.get('ts_clock_source')})",
        'exceptions': {(syms.exception(n) if syms else exception_name(n)) + f' ({n})': r for n, r in exc.items()},
        'exception_anomalies': anomalies,
        'pc': pc_report(evts, syms, top),
        'data': data_report(evts, syms),
        'text': text_report(evts),
        'events': event_report(evts),
        'global_ts': global_report(evts, ticks_hz if not timing else None),
    }
    breaks = integ['overflow'] + integ['unknown'] + integ['resync'] + integ['unexpected']
    if ticks_hz and integ['lts'] and not breaks:   # a gap loses local timestamp time; up to teardown only
        stamped = [e['t'] for e in evts if e.get('t') is not None and e['seg'] == 0]
        span = (max(stamped) - min(stamped)) / ticks_hz if stamped else 0   # first to last timestamped packet
        summary['span_s'] = round(span, 6)
        summary['event_shares'] = event_shares(evts, ticks_hz, clock)
    return summary, evts


def print_text(s, out=sys.stdout):
    i = s['integrity']
    w = out.write
    w('INTEGRITY\n')
    w(f"  bytes {i['bytes']}  packets {i.get('packets', 0)}  syncs {i.get('sync', 0)}  overflows {i.get('overflow', 0)}"
      f"  segments {i['segments']}\n")
    for k in ('unsynced_bytes', 'unknown_bytes', 'unexpected', 'truncated_bytes', 'untimed'):
        if i.get(k):
            w(f'  {k.replace("_", " ")} {i[k]}\n')
    for u in i['unknown']:
        if 'unexpected' in u:
            w(f"  {u['unexpected']} packet at {u['off']} from a source the capture did not enable: corrupted stream\n")
        else:
            w(f"  unknown header {u['header']} at {u['off']}, {u['bytes']} bytes skipped to the next sync\n")
    for name, n in i.get('periodic_missed', {}).items():
        if n:
            w(f'  {n} {name} packet(s) missing between timestamped neighbours (no Overflow packet marks them)\n')
    if i.get('teardown_overflow'):
        w(f"  {i['teardown_overflow']} overflow(s) after the capture began switching its sources off\n")
    if i.get('overflow'):
        w('  WARNING: data was lost at each overflow; counts are lower bounds, episodes never span one\n')
    w(f"TIMING  {s['timing']}" + (f"  span {s['span_s']} s" if 'span_s' in s else '') + '\n')
    if s['exceptions']:
        w('EXCEPTIONS\n')
        for name, r in s['exceptions'].items():
            line = f"  {name:32} enter {r['enter']:7}  exit {r['exit']:7}"
            for key in ('dur_us', 'self_us'):
                if key in r:
                    d = r[key]
                    line += f"  {key[:-3]} us p50 {d['p50']} p99 {d['p99']} max {d['max']} (n {d['n']})"
            for key in ('delayed', 'untimed'):
                if r[key]:
                    line += f'  {r[key]} {key}'
            w(line + '\n')
        if s['exception_anomalies']:
            w(f"  anomalies {s['exception_anomalies']}\n")
    if s['pc']['samples']:
        p = s['pc']
        w(f"PC  samples {p['samples']}  sleep {p['sleep']}  prohibited {p['prohibited']}\n")
        for name, n in p['top']:
            w(f'  {n * 100 / p["samples"]:6.2f}%  {n:7}  {name}\n')
    for c, r in s['data'].items():
        w(f'DATA comparator {c}  ' + '  '.join(f'{k} {v}' for k, v in r.items() if k not in ('pcs', 'offsets'))
          + '\n')
        for name, n in r['pcs']:
            w(f'  {n:7}  {name}\n')
        if r['offsets']:
            w('  address bits [15:0]: ' + '  '.join(f'{a} x{n}' for a, n in r['offsets']) + '\n')
    for port, r in s['text'].items():
        w(f"TEXT port {port}  bytes {r['bytes']}  writes {r['writes']}\n  {r['text'][:400]!r}\n")
    g = s['global_ts']
    if g:
        w(f"GLOBAL TIMESTAMPS  {g['complete']} complete, 0x{g['first']:x} .. 0x{g['last']:x}" +
          (f", clock ~{g['clock_hz']} Hz against local time" if 'clock_hz' in g else '') + '\n')
    for name, n in s['events'].items():
        unit = 'periods' if name == 'Cyc' else f'wraps (>= {n * 256} counts)'
        share = s.get('event_shares', {}).get(name)
        w(f'EVENTS {name:6} {n} {unit}' + (f', {share:.2%} of the cycles between its timed wraps'
                                            if share is not None else '') + '\n')


def periods(features):
    """{source: cycles between its packets} for the capture's periodic sources: (POSTPRESET+1) x 64 or x 1024."""
    out = {}
    for key, name in (('pc_sample', 'pc'), ('cyc_event', 'Cyc')):
        if features.get(key):
            cyctap, postpreset = features[key]
            out[name] = (postpreset + 1) * (1024 if cyctap else 64)
    return out


def load_input(a):
    if a.capture:
        if a.bin or a.start:
            raise Usage('a capture dir carries its stream start; --bin and --start are for a raw file')
        with open(os.path.join(a.capture, 'capture.json')) as f:
            cap = json.load(f)
        path = os.path.join(a.capture, cap.get('stream', 'swo.bin'))
        ts = cap.get('timestamps') or {}
        meta = {'synced': cap['stream_start'] == 'synced', 'ts_clock_hz': ts.get('clock_hz'),
                'ts_clock_source': ts.get('clock_source'), 'ts_prescale': ts.get('prescale', 1),
                'sources': cap.get('sources'), 'teardown_byte': cap.get('teardown_byte'),
                'periods': periods(cap.get('features') or {})}
        if a.ts_clock_hz or a.ts_prescale:
            raise Usage('a capture dir carries its timestamp clock; --ts-clock-hz is for a raw file')
    else:
        if not a.bin or not a.start:
            raise Usage('give a capture dir, or --bin with --start synced|unsynced')
        if a.ts_prescale and not a.ts_clock_hz:
            raise Usage('--ts-prescale needs --ts-clock-hz')
        path = a.bin
        meta = {'synced': a.start == 'synced', 'ts_clock_hz': a.ts_clock_hz,
                'ts_clock_source': 'given with --ts-clock-hz', 'ts_prescale': a.ts_prescale or 1}
    if meta['ts_prescale'] not in (1, 4, 16, 64):
        raise Usage(f"timestamp prescale {meta['ts_prescale']} is not one of 1, 4, 16, 64 (ITM_TCR.TSPrescale)")
    with open(path, 'rb') as f:
        return f.read(), meta


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('capture', nargs='?', help='capture dir written by swo_capture.py')
    p.add_argument('--bin', help='raw SWO byte file captured by hand')
    p.add_argument('--start', choices=('synced', 'unsynced'),
                   help='with --bin: synced when byte 0 is a packet boundary (capture began before any source)')
    p.add_argument('--ts-clock-hz', type=int, help='with --bin: the local timestamp clock')
    p.add_argument('--ts-prescale', type=int, help='with --bin: ITM_TCR.TSPrescale as a divisor (1, 4, 16, 64)')
    p.add_argument('--elf', help='firmware ELF for function and handler names')
    p.add_argument('--jsonl', help='write every event as one JSON line here')
    p.add_argument('--json', action='store_true', help='print the summary as JSON')
    p.add_argument('--top', type=int, default=15, help='PC functions listed (default 15)')
    a = p.parse_args(argv)
    try:
        data, meta = load_input(a)
        syms = Symbols(a.elf) if a.elf else None
    except (Usage, ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f'swo_decode.py: {e}', file=sys.stderr)
        return 2
    summary, evts = summarize(data, meta, syms, a.top)
    if a.jsonl:
        with open(a.jsonl, 'w') as f:
            for e in evts:
                f.write(json.dumps(e) + '\n')
    if a.json:
        print(json.dumps(summary, indent=1, default=str))
    else:
        print_text(summary)
    return 0 if summary['integrity'].get('packets') else 1


if __name__ == '__main__':
    sys.exit(main())
