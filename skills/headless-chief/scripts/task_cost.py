#!/usr/bin/env python3
"""What each headless-chief task has spent, summed over its launches from the run ledger (run_ledger.py, #490).

    task_cost.py [--task KEY] [--json]

A task is the --task KEY its launches passed chief_run.py; a launch with no key is a task of its own, `unkeyed:
<out>`. A launch is unfinished while its start has no end (running, or its launcher killed); its cost is known,
partial (some agents unpriced: a lower bound) or unknown. A total is a plain sum only when every launch finished with
a known cost and the whole ledger could be read: otherwise it shows what is known and what is not, never a sum that
reads as complete. A line that cannot be attributed (unreadable, another version, an end with no start, a conflicting
duplicate) is a diagnostic and makes the whole query incomplete, --task included: it may hide a launch of that task.
--json prints {v, ledger, complete, tasks: [{task, unkeyed, launches, unfinished, partial, unknown, knownUsd,
totalUsd, wall, runs}], diagnostics}; totalUsd is null unless that total is whole. Exit 2 when the ledger cannot be
read or its lock is not taken in time.
"""
import argparse
import json
import math
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_ledger  # noqa: E402

RUN_ID = re.compile(r'[0-9a-f]{32}')
USD = re.compile(r'\d+\.\d{2}')
COST_STATES = ('known', 'partial', 'unknown')


def shaped(rec):
    """Why a ledger record is not one this reads, else None: every field the roll-up uses present and well typed."""
    if not isinstance(rec, dict) or rec.get('v') != 1:
        return 'not a version 1 record'
    if rec.get('type') not in ('start', 'end') or not isinstance(rec.get('run'), str) or not RUN_ID.fullmatch(rec['run']):
        return 'no start or end with a run id'
    if not isinstance(rec.get('at'), str):
        return 'no time'
    if rec['type'] == 'start':
        if 'task' not in rec or not (rec['task'] is None or isinstance(rec['task'], str)) or \
                not all(isinstance(rec.get(k), str) for k in ('out', 'worktree')):
            return 'a start without task, out or worktree'
        return None
    wall = rec.get('wall')
    if type(rec.get('exit')) is not int or not isinstance(rec.get('reason'), str) or 'session' not in rec or \
            type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0:
        return 'an end without exit, reason, session or a finite wall'
    cost = rec.get('cost')
    if not isinstance(cost, dict) or set(cost) != {'status', 'usd', 'reason'} or cost['status'] not in COST_STATES:
        return 'an end whose cost is not known, partial or unknown'
    usd, why = cost['usd'], cost['reason']
    priced = isinstance(usd, str) and USD.fullmatch(usd)
    if not ((cost['status'] == 'known' and priced and (why is None or isinstance(why, str))) or
            (cost['status'] == 'partial' and priced and isinstance(why, str)) or
            (cost['status'] == 'unknown' and usd is None and isinstance(why, str))):
        return f'an end whose {cost["status"]} cost has no amount or reason it needs'
    return None


def fold(lines):
    """({run: {'start', 'end'}}, diagnostics, complete) from the ledger's byte lines."""
    runs, diagnostics, complete, disputed = {}, [], True, set()
    for n, raw in enumerate(lines, 1):
        try:
            rec = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as e:
            rec, why = None, f'unreadable: {e}'
        else:
            why = shaped(rec)
        if why:
            diagnostics.append({'line': n, 'problem': why})
            complete = False
            continue
        had = runs.setdefault(rec['run'], {}).get(rec['type'])
        if had is None:
            runs[rec['run']][rec['type']] = rec
        elif had == rec:
            diagnostics.append({'line': n, 'run': rec['run'], 'problem': f'the same {rec["type"]} again'})
        else:   # neither is established: the run counts for nothing
            diagnostics.append({'line': n, 'run': rec['run'], 'problem': f'a second, different {rec["type"]}'})
            complete = False
            disputed.add(rec['run'])
    for run, got in runs.items():
        if 'start' not in got:
            diagnostics.append({'run': run, 'problem': 'an end with no start'})
            complete = False
    return {r: got for r, got in runs.items() if 'start' in got and r not in disputed}, diagnostics, complete


def tasks_of(runs, complete):
    """Per task, in the order of its first start."""
    by = {}
    for run, got in sorted(runs.items(), key=lambda kv: (kv[1]['start']['at'], kv[0])):
        start, end = got['start'], got.get('end')
        key = (start['task'], None) if start['task'] is not None else (f'unkeyed: {start["out"]}', run)   # its own task
        cost = end['cost'] if end else None
        by.setdefault(key, []).append({
            'run': run, 'out': start['out'], 'worktree': start['worktree'], 'startedAt': start['at'],
            'endedAt': end['at'] if end else None, 'exit': end['exit'] if end else None,
            'reason': end['reason'] if end else 'unfinished', 'wall': end['wall'] if end else None, 'cost': cost})
    out = []
    for (task, alone), launches in by.items():
        costs = [x['cost'] for x in launches if x['cost']]
        known = sum((Decimal(c['usd']) for c in costs if c['usd'] is not None), Decimal('0.00'))
        unfinished = sum(x['cost'] is None for x in launches)
        partial = sum(c['status'] == 'partial' for c in costs)
        unknown = sum(c['status'] == 'unknown' for c in costs)
        whole = complete and not (unfinished or partial or unknown)
        out.append({'task': task, 'unkeyed': alone is not None, 'launches': len(launches), 'unfinished': unfinished,
                    'partial': partial, 'unknown': unknown,
                    'knownUsd': str(known) if any(c['usd'] is not None for c in costs) else None,
                    'totalUsd': str(known) if whole else None,
                    'wall': round(sum(x['wall'] for x in launches if x['wall'] is not None), 1), 'runs': launches})
    return out


def spent(t):
    """The task's cost as text: a sum only when it is whole."""
    if t['totalUsd'] is not None:
        return f'${t["totalUsd"]}'
    gaps = [f'{t[k]} {k}' for k in ('unfinished', 'partial', 'unknown') if t[k]]
    known = f'${t["knownUsd"]} known' if t['knownUsd'] is not None else 'unknown'
    return '; '.join([known, *gaps]) if gaps else f'{known}; ledger incomplete'


def text(report, each):
    rows = ['| task | launches | wall | $ |', '|---|---:|---:|---|']
    for t in report['tasks']:
        rows.append(f'| {t["task"]} | {t["launches"]} | {t["wall"] / 60:.1f} m | {spent(t)} |')
        if each:
            for x in t['runs']:
                cost = x['cost']
                what = ('unfinished' if cost is None else f'unknown ({cost["reason"]})' if cost['usd'] is None
                        else f'${cost["usd"]}' + (' (partial)' if cost['status'] == 'partial' else ''))
                wall = '-' if x['wall'] is None else f'{x["wall"] / 60:.1f} m'
                rows.append(f'| ↳ {x["startedAt"]} exit {x["exit"]} | {x["out"]} | {wall} | {what} |')
    lines = rows if report['tasks'] else ['No launch recorded' + (' for that task.' if each else '.')]
    if not report['complete']:
        lines.append('\nIncomplete: the ledger holds lines that cannot be attributed, so a total may miss a launch.')
    lines += [f'- line {d.get("line", "-")}: {d["problem"]}' + (f' (run {d["run"]})' if d.get('run') else '')
              for d in report['diagnostics']]
    return '\n'.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--task')
    p.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    ledger = run_ledger.path()
    try:
        lines = run_ledger.read(ledger)
    except (run_ledger.Busy, OSError) as e:
        print(f'task_cost: {ledger} cannot be read: {e}', file=sys.stderr)
        return 2
    runs, diagnostics, complete = fold(lines)
    tasks = [t for t in tasks_of(runs, complete) if a.task is None or (not t['unkeyed'] and t['task'] == a.task)]
    report = {'v': 1, 'ledger': str(ledger), 'complete': complete, 'tasks': tasks, 'diagnostics': diagnostics}
    print(json.dumps(report, indent=2) if a.json else text(report, a.task is not None))
    return 0


if __name__ == '__main__':
    sys.exit(main())
