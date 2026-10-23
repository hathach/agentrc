#!/usr/bin/env python3
"""Compare native SystemView decoding with a checksummed corpus of GUI exports.

  sysview_corpus.py --root DIR --manifest FILE --out DIR

Checks every packet against the raw bytes before comparing normalized event rows and sysview_report JSON.
Each capture gets a JSON diff and interval proofs in --out.
Exit: 0 no unexplained differences; 1 unexplained differences; 2 missing/changed corpus or invalid input.
"""
import argparse
import bisect
import contextlib
import copy
import csv
import hashlib
import io
import importlib.util
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import sysview_decode as decoder
import sysview_report as report


# OS-specific rendering is supplied by the GUI's OS description files; the report ignores it.
REPORT_EVENTS = {1, 2, 3, 4, 6, 7, 15, 16, 17, 18, 21, 24, 28, 31}
CLASSES = ('gui-export-artifact', 'loss-withheld', 'gui-warning', 'gui-inconsistent',
           'withheld-undefined', 'informational', 'unexplained')
ROBUSTNESS_CAPTURES = {'8f8948a12c67f10946d54bda0cde8ce5da5d1e5aa23b50dcb1c1772cc558ee1c': 'max32666-corrupt'}
GUI_WARNING = re.compile(r'SystemView showed \d+ error dialog\(s\) while recording or loading \(record.json, '
                         r'systemviewErrors\): the export may be incomplete')
LOST = f'0x{decoder.LOST:016X}'


def read_rows(path):
    with Path(path).open(encoding='utf-8', errors='replace', newline='') as f:
        return list(csv.DictReader(f))


def normalized_rows(rows):
    calls = {}
    freq = 0
    result = []
    for row in rows:
        r = {key: str(row[key]).strip() for key in ('timestampint', 'eventint', 'eventoffset', 'eventsize',
                                                  'context', 'contextinint', 'contextint', 'contextoutint')}
        r['eventdata'] = bytes.fromhex(row['eventdata']).hex()
        event, detail = row['event'], row['detail']
        eid = int(row['eventint'])
        m = report.FREQ_RE.search(detail) if eid == 24 else None
        if m:
            freq = int(m[1])
        if eid in REPORT_EVENTS or eid >= 512:
            r.update(event=event, detail=detail)
        if eid == 28 and not report.FUNC_RE.match(event):
            r.pop('event', None)
            r.pop('detail', None)
        m = report.FUNC_RE.match(event)
        if m:
            key = (int(m[1]), row['context'])
            ts = report.timestamp(row)
            if 'Returns' in detail:
                r['detail'] = 'Returns'
                start = calls[key].pop() if calls.get(key) else None
                duration = report.RETURNS_RE.search(detail)
                if duration:
                    r['duration_us'] = round(report.matched_seconds(duration) * 1e6, 3)
                elif start is not None and ts is not None and freq:
                    r['duration_us'] = round((ts - start) * 1e6 / freq, 3)
            else:
                calls.setdefault(key, []).append(ts)
                r['detail'] = ''
        if eid == 1 or '*** OVERFLOW ***' in detail:
            calls.clear()
        result.append(r)
    return result


def json_report(path):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        report.main([str(path), '--json'])
    return json.loads(output.getvalue())


def differences(expected, actual, prefix=''):
    if type(expected) is not type(actual):
        yield {'path': prefix, 'gui': expected, 'native': actual}
    elif isinstance(expected, dict):
        for key in sorted(expected.keys() | actual.keys()):
            path = f'{prefix}.{key}' if prefix else key
            if key not in expected or key not in actual:
                yield {'path': path, 'gui': expected.get(key), 'native': actual.get(key)}
            else:
                yield from differences(expected[key], actual[key], path)
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            yield {'path': f'{prefix}.length', 'gui': len(expected), 'native': len(actual)}
        for i, (left, right) in enumerate(zip(expected, actual)):
            yield from differences(left, right, f'{prefix}[{i}]')
    elif expected != actual:
        yield {'path': prefix, 'gui': expected, 'native': actual}


def annotation_matches(detail, ticks, freq):
    m = re.search(r'(?:Runs for|Idle for|runs after) (\d+\.\d{3}) (us|ms|s)$', detail)
    if not m or not freq or ticks < 0:
        return False
    scale = {'us': 1_000_000_000, 'ms': 1_000_000, 's': 1000}[m[2]]
    value = ((2 * ticks * scale + freq) // (2 * freq) if m[2] == 'us' else ticks * scale // freq)
    return m[1] == f'{value // 1000}.{value % 1000:03d}'


def row_proofs(data, gui, native):
    """Only edit the comparison copy after a packet/interval proof and an exact native-value check."""
    cleaned = copy.deepcopy(gui)
    proofs = {}
    if len(gui) != len(native) or any(int(g['eventoffset']) != int(n['eventoffset']) for g, n in zip(gui, native)):
        return cleaned, proofs
    packets = [decoder.packet_at(data, int(r['eventoffset']))[0] for r in gui]
    stamps = [int(r['timestampint']) for r in gui]
    losses = [i for i, p in enumerate(packets) if p.event == 1]
    freq = next((p.args[0] for p in packets if p.event == 24 and not p.problem), 0)
    formatter = decoder.Decoder()
    formatter.freq = freq
    names = {r['contextint']: r['context'] for r in gui}

    def justify(i, field, value, category, reason, **evidence):
        if str(native[i][field]) != str(value) or str(gui[i][field]) == str(value):
            return False
        proofs[i, field] = {'classification': category, 'reason': reason, 'row': i,
                            'offset': packets[i].offset, 'event': packets[i].event, **evidence}
        cleaned[i][field] = value
        return True

    for i, (g, n, p) in enumerate(zip(gui, native, packets)):
        passive = p.event in (2, 4, 6, 8, 9, 12, 13, 14, 15, 16, 17, 19, 21, 22, 23, 24, 25, 26, 27, 28, 31) or p.event >= 32
        if (i % 1000 == 999 and i + 1 < len(gui) and g['contextoutint'] == LOST
                and p.event != 1 and n['contextoutint'] != LOST
                and (n['contextoutint'] == gui[i + 1]['contextinint']
                     or (passive and n['contextoutint'] == g['contextint']))
                and g['contextinint'] == n['contextinint'] and g['contextint'] == n['contextint']):
            key = int(g['contextint'], 16)
            expected = (decoder.TASK | p.args[0]) if p.event == 4 else decoder.IDLE if p.event == 17 else key
            if p.event in (2, 19):
                expected = (decoder.ISR if p.event == 2 else decoder.TIMER) | p.args[0]
            if p.event == 7 and p.args and decoder.TASK | p.args[0] == key:
                expected = decoder.SCHEDULER
            if p.event in (3, 18, 20):
                # The following row independently names the resumed context; no loss packet intervenes.
                expected = int(gui[i + 1]['contextinint'], 16)
            value = f'0x{expected:016X}'
            if value != LOST and justify(i, 'contextoutint', value, 'gui-export-artifact',
                                        'Only the outgoing context becomes LOST at an N*1000-1 export row.',
                                        nextRow=i + 1, nextContextIn=gui[i + 1]['contextinint'],
                                        packet=p.data.hex()):
                if p.event in (3, 18, 20) and g['detail'] == 'Returns to *** OVERFLOW ***' and value in names:
                    justify(i, 'detail', 'Returns to ' + names[value], 'gui-export-artifact',
                            'Return text repeats the proven export-boundary outgoing-context artifact.',
                            outgoingContext=value, nextRow=i + 1)
        if (int(g['eventsize']) == 512 and len(p.data) > 512
                and bytes.fromhex(g['eventdata']) == p.data[:512] and bytes.fromhex(n['eventdata']) == p.data):
            for field, value in [('eventsize', len(p.data)), ('eventdata', p.data.hex(' ').upper() + ' ')]:
                justify(i, field, value, 'gui-export-artifact',
                        'GUI caps exported packet bytes at 512; the raw length field proves the full frame size.',
                        exportedBytes=512, framedBytes=len(p.data), frameEnd=p.offset + len(p.data))

    frames = []
    incomplete = {}
    running_roots = {}
    for i, (r, p) in enumerate(zip(cleaned, packets)):
        eid, key = p.event, int(r['contextint'], 16)
        running_roots[i] = frames[0]['key'] if frames else 0
        if eid in (1, 11):
            for frame in frames:
                for entry in frame['rows']:
                    incomplete[entry] = (i, 'overflow' if eid == 1 else 'recording stop', frame['rows'][0])
            frames = []
        elif eid in (4, 17):
            if frames and frames[-1]['key'] == key:
                frames[-1]['rows'].append(i)
            else:
                frames = [{'key': key, 'rows': [i]}]
        elif eid in (2, 19):
            frames.append({'key': key, 'rows': [i]})
        elif eid in (3, 18, 20):
            if frames and frames[-1]['key'] >> 32 in (1, 2):
                frames.pop()
            if eid == 18 and len(frames) < 2:
                frames = []
        elif eid == 7 and r['contextint'] != r['contextoutint']:
            frames = []
    for frame in frames:
        for entry in frame['rows']:
            incomplete[entry] = (len(gui) - 1, 'capture end', frame['rows'][0])
    for i, (end, boundary, start) in incomplete.items():
        if annotation_matches(gui[i]['detail'], stamps[end] - stamps[start], freq):
            justify(i, 'detail', '', 'loss-withheld', 'GUI closes an open execution span at a loss/cut boundary.',
                    startRow=start, endRow=end, boundary=boundary, ticks=stamps[end] - stamps[start])

    next_run, ready_runs = {}, {}
    for i in range(len(gui) - 1, -1, -1):
        p = packets[i]
        if p.event == 4:
            next_run[p.args[0]] = i
        elif p.event == 6 and p.args[0] in next_run:
            ready_runs[i] = next_run[p.args[0]]
    groups = defaultdict(list)
    for i, end in ready_runs.items():
        groups[end, gui[i]['detail']].append(i)
    for (end, detail), entries in groups.items():
        entries.sort()
        source = next((i for i in entries if annotation_matches(detail, stamps[end] - stamps[i], freq)
                       and bisect.bisect_right(losses, i) < bisect.bisect_left(losses, end)), None)
        for i in entries:
            p = packets[i]
            task = f'0x{decoder.TASK | p.args[0]:016X}'
            name = names.get(task)
            if not name:
                continue
            if (freq and running_roots[i] == int(task, 16)
                    and re.search(r'runs after \d+\.\d+ (us|ms|s)$', detail)
                    and not annotation_matches(detail, stamps[end] - stamps[i], freq)):
                justify(i, 'detail', name, 'gui-inconsistent',
                        'GUI ready latency disagrees with its own next Task Run while the named task is already active.',
                        task=task, nextRun=end, context=gui[i]['contextint'], rootContext=task,
                        actualTicksToNextRun=stamps[end] - stamps[i], frequency=freq)
            if source is None:
                continue
            crossing = losses[bisect.bisect_right(losses, source):bisect.bisect_left(losses, end)]
            value = name if i < crossing[-1] else name + ', runs after ' + formatter.duration(stamps[end] - stamps[i])
            justify(i, 'detail', value, 'loss-withheld',
                    'GUI ready latency starts before an overflow; later Ready rows repeat that same annotation.',
                    startRow=source, endRow=end, overflowRows=crossing, ticks=stamps[end] - stamps[source])
    calls = defaultdict(list)
    gui_normal, native_normal = normalized_rows(gui), normalized_rows(native)
    for i, g in enumerate(gui):
        if packets[i].problem or packets[i].event == 1:
            calls.clear()
        if packets[i].problem:
            continue
        function = report.FUNC_RE.match(g['event'])
        if not function:
            continue
        key = function[1], g['contextint']
        if 'Returns' not in g['detail']:
            calls[key].append(i)
        elif calls[key]:
            start = calls[key].pop()
            ticks = stamps[i] - stamps[start]
            numerator = ticks * 1_000_000_000
            left, right = gui_normal[i].get('duration_us'), native_normal[i].get('duration_us')
            if (left != right and freq and 2 * (numerator % freq) == freq
                    and ticks == sum(p.delta for p in packets[start + 1:i + 1])
                    and {left, right} == {numerator // freq / 1000, (numerator // freq + 1) / 1000}):
                proofs[i, 'duration_us'] = {'classification': 'gui-export-artifact',
                                          'reason': 'The exact packet duration is a half-nanosecond midpoint; displays choose opposite neighbors.',
                                          'row': i, 'event': packets[i].event, 'startRow': start,
                                          'ticks': ticks, 'frequency': freq}
    return cleaned, proofs


def report_with_rows(gui, rows, metadata=None):
    with tempfile.TemporaryDirectory(prefix='sysview-accounting-') as tmp:
        path = Path(tmp)
        decoder.write_csv(path / 'events.txt', rows[0].keys(), rows)
        (path / 'contexts.csv').write_bytes((Path(gui) / 'contexts.csv').read_bytes())
        record = Path(gui) / 'record.json'
        if record.is_file():
            (path / 'record.json').write_bytes(record.read_bytes())
        if metadata:
            (path / 'decode.json').write_text(json.dumps(metadata), encoding='utf-8')
        return json_report(path)


def clock_losses(data):
    diagnostics, losses = [], []
    frequency = 0
    packets = list(decoder.stream(data, diagnostics))
    for i, packet in enumerate(packets):
        if packet.gap:
            gap = next(d for d in reversed(diagnostics) if d.get('timingLost') and d['offset'] < packet.offset)
            losses.append({'row': i, 'offset': gap['offset'], 'reason': gap['reason']})
        if packet.event == 24 and not packet.problem:
            if frequency and frequency != packet.args[0]:
                losses.append({'row': i, 'offset': packet.offset, 'reason': 'timestamp frequency changed'})
            frequency = packet.args[0]
    for diagnostic in diagnostics:
        if diagnostic.get('timingLost') and not any(x['offset'] == diagnostic['offset'] for x in losses):
            losses.append({'row': len(packets), 'offset': diagnostic['offset'], 'reason': diagnostic['reason']})
    return packets, losses


def context_accounting(data, rows):
    """Independent interval sums from the GUI's context identities, for captures without loss packets."""
    packets = [decoder.packet_at(data, int(r['eventoffset']))[0] for r in rows]
    if any(p.event == 1 or p.problem for p in packets):
        return {}
    freq = next((p.args[0] for p in packets if p.event == 24), 0)
    stamps = [int(r['timestampint']) for r in rows]
    if not freq or not stamps or stamps[-1] <= stamps[0]:
        return {}
    names = {int(r['contextint'], 16): r['context'] for r in rows}
    labels = [name for key, name in names.items() if key]
    if len(labels) != len(set(labels)):
        return {}
    total, interrupted, activations = Counter(), Counter(), defaultdict(list)
    intervals, runs, pending = defaultdict(list), defaultdict(list), set()
    active = [{'key': 0, 'exclusive': 0}]
    previous = stamps[0]

    def close(frame):
        if frame['exclusive'] > 0:
            runs[frame['key']].append(frame['exclusive'])

    for i, (r, p) in enumerate(zip(rows, packets)):
        ts = stamps[i]
        if ts < previous:
            return {}
        delta = ts - previous
        for depth, frame in enumerate(active):
            key = frame['key']
            total[key] += delta
            if any(f['key'] >> 32 == 1 for f in active[depth + 1:]):
                interrupted[key] += delta
            else:
                frame['exclusive'] += delta
            if delta:
                intervals[key].append({'startRow': i - 1, 'endRow': i, 'ticks': delta})
        previous = ts
        key, out = int(r['contextint'], 16), int(r['contextoutint'], 16)
        before = active[-1]['key'] if active else 0
        if p.event == 6:
            task = decoder.TASK | p.args[0]
            root = active[0]['key'] if active else 0
            if task != root and task not in pending:
                activations[task].append(i)
                pending.add(task)
        elif p.event == 7:
            pending.discard(decoder.TASK | p.args[0])
        if p.event in (2, 19):
            activations[key].append(i)
            active.append({'key': key, 'exclusive': 0})
        elif p.event in (3, 18, 20):
            if active and active[-1]['key'] >> 32 in (1, 2):
                close(active.pop())
            if p.event == 18 and out == decoder.SCHEDULER and (not active or active[-1]['key'] != out):
                for frame in active:
                    close(frame)
                active = [{'key': out, 'exclusive': 0}]
                activations[out].append(i)
        elif p.event in (4, 17, 5, 7) and before != out:
            for frame in active:
                close(frame)
            active = [{'key': out, 'exclusive': 0}]
            if out not in pending:
                activations[out].append(i)
            pending.discard(out)
        elif p.event == 11:
            active = []
    result = {}
    for key, name in names.items():
        if key == 0:
            continue
        count = len(activations[key])
        seconds = round(total[key] / freq, 9)
        metrics = {'activations': count, 'total_ms': round(seconds * 1000, 3),
                   'avg_us': round(seconds / count * 1e6, 1) if count else None,
                   'cpu_pct': round((total[key] - interrupted[key]) / (stamps[-1] - stamps[0]) * 100, 2),
                   'min_us': round(round(min(runs[key], default=0) / freq, 9) * 1e6, 1),
                   'max_us': round(round(max(runs[key], default=0) / freq, 9) * 1e6, 1)}
        result[name] = {'metrics': metrics, 'activationRows': activations[key], 'intervals': intervals[key],
                        'runtimeTicks': total[key], 'interruptedTicks': interrupted[key],
                        'frequency': freq, 'windowTicks': stamps[-1] - stamps[0]}
    return result


def lost_interruption_accounting(data, rows, gui_dir):
    """Account for completed ISR intervals whose enclosing task execution is later lost."""
    packets = [decoder.packet_at(data, int(r['eventoffset']))[0] for r in rows]
    if any(p.problem for p in packets):
        return {}
    freq = next((p.args[0] for p in packets if p.event == 24), 0)
    stamps = [int(r['timestampint']) for r in rows]
    if any(a > b for a, b in zip(stamps, stamps[1:])):
        return {}
    window = stamps[-1] - stamps[0]
    if not freq or window <= 0:
        return {}
    runtime, interrupted, kept = Counter(), Counter(), Counter()
    discarded = defaultdict(list)
    stack, root, root_start, pending = [], 0, 0, []

    def finish(end, lost=False):
        if root >> 32 != 8:
            return
        if lost:
            for entry in pending:
                discarded[root].append({**entry, 'taskStartRow': root_start, 'overflowRow': end})
        else:
            runtime[root] += int(rows[end]['timestampint']) - int(rows[root_start]['timestampint'])
            kept[root] += sum(entry['ticks'] for entry in pending)

    for i, (r, p) in enumerate(zip(rows, packets)):
        key, out = int(r['contextint'], 16), int(r['contextoutint'], 16)
        if p.event == 1:
            finish(i, lost=True)
            stack, root, pending = [], 0, []
        elif p.event in (4, 17):
            if root != key:
                finish(i)
                root, root_start, pending = key, i, []
        elif p.event == 2:
            stack.append((i, int(r['contextinint'], 16)))
        elif p.event in (3, 18):
            if stack:
                start, parent = stack.pop()
                if parent >> 32 == 8:
                    ticks = int(r['timestampint']) - int(rows[start]['timestampint'])
                    if ticks < 0:
                        return {}
                    interrupted[parent] += ticks
                    pending.append({'startRow': start, 'endRow': i, 'ticks': ticks})
            if p.event == 18 and not stack:
                finish(i)
                root, pending = decoder.SCHEDULER, []
        elif p.event in (5, 7, 11) and key != out:
            finish(i)
            root, pending = out, []
    finish(len(rows) - 1)
    names = {int(r['contextint'], 16): r['context'] for r in rows}
    labels = [name for key, name in names.items() if key]
    if len(labels) != len(set(labels)):
        return {}
    exported = {r['Name']: r for r in read_rows(Path(gui_dir) / 'contexts.csv')}
    result = {}
    for key, spans in discarded.items():
        name = names[key]
        context = exported.get(name, {})
        gui_time = report.seconds(context.get('Total Run Time', ''))
        gui_interrupted = report.seconds(context.get('Time Interrupted', ''))
        if (gui_time is None or gui_interrupted is None
                or round(runtime[key] / freq, 9) != round(gui_time, 9)
                or round(interrupted[key] / freq, 9) != round(gui_interrupted, 9)
                or interrupted[key] - kept[key] != sum(span['ticks'] for span in spans)):
            continue
        result[name] = {'runtimeTicks': runtime[key], 'guiInterruptedTicks': interrupted[key],
                        'retainedInterruptedTicks': kept[key], 'removedSpans': spans,
                        'frequency': freq, 'windowTicks': window,
                        'guiCpu': round((runtime[key] - interrupted[key]) / window * 100, 2),
                        'accountedCpu': round((runtime[key] - kept[key]) / window * 100, 2)}
    return result


def classify(data, gui, native, gui_report, native_report, gui_dir, diffs, report_diffs, metadata=None):
    metadata = metadata or {}
    cleaned, proofs = row_proofs(data, gui, native)
    for diff in diffs:
        match = re.fullmatch(r'events\[(\d+)\]\.(\w+)', diff['path'])
        proof = proofs.get((int(match[1]), match[2])) if match else None
        diff.update(proof or {'classification': 'unexplained', 'reason': 'No packet/interval proof closes this difference.'})
    packets, losses = clock_losses(data)
    timed = losses[0]['row'] if losses else len(packets)
    timing_verified = (bool(losses) and losses == metadata.get('timingLosses')
                       and timed == metadata.get('timedEvents') and len(packets) == len(gui)
                       and all(int(g['eventoffset']) == p.offset for g, p in zip(gui, packets))
                       and all(n['timestampint'] == '' for n in native[timed:]))
    accounted = report_with_rows(gui_dir, cleaned, metadata if timing_verified else None)
    contexts = context_accounting(data, cleaned) if any(d['path'].startswith('report.contexts') for d in report_diffs) else {}
    interruptions = (lost_interruption_accounting(data, cleaned, gui_dir)
                     if any(d['path'].endswith('.cpu_pct') for d in report_diffs) else {})
    remaining = {d['path'] for d in differences(accounted, native_report, 'report')}
    event_proofs = [proof for (i, field), proof in proofs.items() if field in ('detail', 'duration_us')]
    os_names = [m[1] for p in packets if p.event == 14
                for m in [re.search(r'(?:^|,)O=([^,]+)', p.text)] if m]
    blocked_evidence = decoder.blocked_time_evidence(os_names[-1] if os_names else '')
    for diff in report_diffs:
        proof = {'classification': 'unexplained', 'reason': 'Report accounting does not close this difference.'}
        if diff['path'] not in remaining and event_proofs:
            # Context aggregates are unchanged in the accounting copy, so this cannot excuse a context statistic.
            relevant = [p for p in event_proofs if
                        (diff['path'].startswith('report.isr') and p['event'] == 2)
                        or (diff['path'].startswith('report.ready_run') and p['event'] == 6)
                        or (diff['path'] == 'report.overflow' and p['event'] in (3, 18))]
            if relevant:
                category = 'loss-withheld' if any(p['classification'] == 'loss-withheld' for p in relevant) else relevant[0]['classification']
                proof = {'classification': category, 'reason': 'Recomputing the GUI report after proven row corrections gives the exact native value.',
                         'accountedValue': diff['native'], 'rowProofs': [p['row'] for p in relevant]}
        if diff['path'] not in remaining and timing_verified:
            proof = {'classification': 'loss-withheld',
                     'reason': 'Recomputing the GUI report with byte-verified clock-loss boundaries gives the exact native value.',
                     'accountedValue': diff['native'], 'timingLosses': losses}
        warnings = [w for w in accounted['warnings'] if not GUI_WARNING.fullmatch(w)]
        dialogs = [w for w in gui_report['warnings'] if GUI_WARNING.fullmatch(w)]
        if diff['path'].startswith('report.warnings') and dialogs and warnings == native_report['warnings']:
            proof = {'classification': 'gui-warning',
                     'reason': 'Removing GUI-only error-dialog warnings after proven loss accounting gives the native warnings.',
                     'warnings': dialogs, 'timingLosses': losses if timing_verified else []}
        match = re.fullmatch(r'report.contexts\[(\d+)\]\.(\w+)', diff['path'])
        if match:
            index, metric = int(match[1]), match[2]
            name = gui_report['contexts'][index]['name']
            evidence = contexts.get(name, {})
            if metric in evidence.get('metrics', {}) and evidence['metrics'][metric] == diff['native']:
                proof = {'classification': 'gui-inconsistent',
                         'reason': 'The GUI context aggregate disagrees with intervals/activations in its own event rows.',
                         'context': name, 'accountedValue': evidence['metrics'][metric],
                         'accounting': evidence}
            evidence = interruptions.get(name, {})
            if metric == 'cpu_pct' and evidence.get('guiCpu') == diff['gui'] and evidence.get('accountedCpu') == diff['native']:
                proof = {'classification': 'loss-withheld',
                         'reason': 'GUI interruption time includes completed ISRs inside task executions later discarded at overflow.',
                         'context': name, 'accountedValue': diff['native'], 'accounting': evidence}
            if (metric == 'blocked_ms' and diff['native'] is None
                    and native_report['contexts'][index]['name'] == name
                    and metadata.get('withheldMetrics', {}).get('blockedTime') == blocked_evidence):
                proof = {'classification': 'withheld-undefined', 'reason': blocked_evidence['reason'],
                         'context': name, 'evidence': blocked_evidence,
                         'taskStopReadyReasons': dict(Counter(p.args[1] for p in packets if p.event == 7))}
        diff.update(proof)
    return proofs


def robustness_checks(data, gui, native, metadata, native_report):
    packets, losses = clock_losses(data)
    framing = []
    list(decoder.stream(data, framing))
    malformed = [{'offset': p.offset, 'bytes': len(p.data), 'reason': 'malformed payload: ' + p.problem}
                 for p in packets if p.problem]
    diagnostics = [d for d in metadata['diagnostics'] if d['reason'].startswith('malformed payload:')]
    timed = losses[0]['row'] if losses else len(packets)
    expected_time, clock = [], 0
    for i, p in enumerate(packets):
        clock += p.delta if i else 0
        ts = expected_time[-1] + 1 if p.event == 1 and i else clock
        expected_time.append(ts)
    checks = {
        'packetOffsets': [int(r['eventoffset']) for r in native] == [int(r['eventoffset']) for r in gui],
        'packetBytes': len(native) == len(packets) and all(
            int(r['eventoffset']) == p.offset and int(r['eventint']) == p.event
            and int(r['eventsize']) == len(p.data) and bytes.fromhex(r['eventdata']) == p.data
            for r, p in zip(native, packets)),
        'malformedDiagnostics': diagnostics == malformed,
        'framingDiagnostics': [d for d in metadata['diagnostics']
                               if not d['reason'].startswith('malformed payload:')
                               and d['reason'] != 'timestamp frequency changed'] == framing,
        'malformedArgumentsWithheld': all(not p.args and not p.text
            and r['event'] == decoder.NAMES.get(p.event, f'Event #{p.event}')
            and r['detail'] == 'Malformed payload: ' + p.problem for r, p in zip(native, packets) if p.problem),
        'noResynchronization': not any(p.gap for p in packets),
        'clockBoundaries': metadata.get('timingLosses') == losses and metadata.get('timedEvents') == timed,
        'timestamps': all(r['timestampint'] == (str(ts) if i < timed else '')
                          for i, (r, ts) in enumerate(zip(native, expected_time))),
    }
    # Report timing must equal a fresh report of only the byte-verified timed prefix.
    with tempfile.TemporaryDirectory(prefix='sysview-robustness-') as tmp:
        path = Path(tmp)
        prefix_end = packets[timed - 1].offset + len(packets[timed - 1].data) if timed else 0
        raw = path / 'timed.SVDat'
        raw.write_bytes(data[:prefix_end])
        decoder.decode_file(raw, path)
        prefix = json_report(path)
    timing = ('isr', 'ready_run', 'functions', 'markers', 'live_window_s', 'workload_window_s', 'contexts_workload')
    checks['reportTimedPrefix'] = all(native_report[k] == prefix[k] for k in timing)
    checks['lossCounted'] = not losses or (native_report['overflow'] >= len(losses)
                                          and any('native timing lost' in w for w in native_report['warnings']))
    return {'checks': checks, 'malformedPackets': malformed, 'timingLosses': losses,
            'timedEvents': timed, 'passed': all(checks.values())}


def compare(raw, gui, out, baseline=None, mode='parity'):
    data = Path(raw).read_bytes()
    if mode not in ('parity', 'robustness'):
        raise decoder.DecodeError(f'unknown comparison mode: {mode}')
    if mode == 'robustness' and hashlib.sha256(data).hexdigest() not in ROBUSTNESS_CAPTURES:
        raise decoder.DecodeError('robustness mode requires an explicitly allow-listed corrupt capture')
    expected = read_rows(Path(gui) / 'events.txt')
    for i, row in enumerate(expected):
        offset, size = int(row['eventoffset']), int(row['eventsize'])
        packet = bytes.fromhex(row['eventdata'])
        if size != len(packet) or data[offset:offset + size] != packet:
            raise decoder.DecodeError(f'{gui}: row {i} does not match raw bytes at offset {offset}')
    out = Path(out)
    metadata = decoder.decode_file(raw, out)
    native = read_rows(out / 'events.txt')
    diffs = list(differences(normalized_rows(expected), normalized_rows(native), 'events'))
    counts = Counter(re.sub(r'\[\d+\]', '[]', d['path']) for d in diffs)
    gui_report, native_report = json_report(gui), json_report(out)
    report_diffs = list(differences(gui_report, native_report, 'report'))
    robustness = None
    if mode == 'robustness':
        robustness = robustness_checks(data, expected, native, metadata, native_report)
        for diff in [*diffs, *report_diffs]:
            diff.update(classification='informational', reason='Allow-listed corrupt-wire capture: GUI parity is not an oracle; see robustness checks.')
    else:
        classify(data, expected, native, gui_report, native_report, gui, diffs, report_diffs, metadata)
    classes = Counter(d['classification'] for d in [*diffs, *report_diffs])
    fixed = []
    if baseline:
        with tempfile.TemporaryDirectory(prefix='sysview-baseline-') as tmp:
            baseline.decode_file(raw, tmp)
            old_rows = read_rows(Path(tmp) / 'events.txt')
            old = [*differences(normalized_rows(expected), normalized_rows(old_rows), 'events'),
                   *differences(gui_report, json_report(tmp), 'report')]
        current = {d['path'] for d in [*diffs, *report_diffs]}
        fixed = [d for d in old if d['path'] not in current]
    return {'raw': str(raw), 'gui': str(gui), 'rawRowsVerified': len(expected), 'nativeRows': len(native),
            'rowDiffCount': len(diffs), 'rowDiffFields': dict(counts), 'rowDiffs': diffs,
            'reportDiffs': report_diffs, 'equal': not diffs and not report_diffs,
            'classifications': {key: classes[key] for key in CLASSES}, 'mode': mode, 'robustness': robustness,
            'accepted': robustness['passed'] if robustness is not None else not classes['unexplained'],
            'fixedCount': len(fixed), 'fixedDifferences': fixed}


def verify(root, files):
    for relative, expected in files.items():
        path = root / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected['sha256']:
            raise decoder.DecodeError(f'{path}: sha256 mismatch: expected {expected["sha256"]}, got {digest}')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--baseline-decoder', type=Path, help='previous decoder source; count differences now fixed')
    a = p.parse_args(argv)
    try:
        manifest = json.loads(a.manifest.read_text(encoding='utf-8'))
        verify(a.root, manifest['files'])
        baseline = None
        if a.baseline_decoder:
            spec = importlib.util.spec_from_file_location('sysview_baseline', a.baseline_decoder)
            if spec is None or spec.loader is None:
                raise decoder.DecodeError('--baseline-decoder must name a Python source file')
            baseline = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = baseline
            spec.loader.exec_module(baseline)
        if a.out.exists() and (not a.out.is_dir() or any(a.out.iterdir())):
            raise decoder.DecodeError('--out must be a new or empty directory')
        a.out.mkdir(parents=True, exist_ok=True)
        results = []
        for case in manifest['captures']:
            with tempfile.TemporaryDirectory(prefix='sysview-corpus-') as tmp:
                result = compare(a.root / case['raw'], a.root / case['gui'], tmp, baseline, case.get('mode', 'parity'))
            result['name'] = case['name']
            (a.out / f'{case["name"]}.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            summary = {key: result[key] for key in ('name', 'rawRowsVerified', 'nativeRows', 'rowDiffCount', 'equal')}
            summary['reportDiffCount'] = len(result['reportDiffs'])
            summary['fixed'] = result['fixedCount']
            summary.update(result['classifications'])
            summary['accepted'] = result['accepted']
            results.append(summary)
            print(json.dumps(summary), flush=True)
        (a.out / 'summary.json').write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError, KeyError) as e:
        p.error(str(e))
    return int(any(not r['accepted'] for r in results))


if __name__ == '__main__':
    sys.exit(main())
