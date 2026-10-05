#!/usr/bin/env python3
"""What a chief session spent, by Workflow run, stage and model, from its transcripts.

    run_cost.py (--session-id ID | --journal PATH) [--full FILE]

--session-id finds ~/.claude/projects/*/ID.jsonl; --journal is any Workflow journal of
the session (<session>/subagents/workflows/<run>/journal.jsonl), which an interactive
chief already holds. Prints a Markdown table: one row per stage and model of each
Workflow run in start order, then chief's own turns and the agents it ran itself, then
the total. A stage is an agent's label up to its first `#`, or up to `:` before a path
(`ci:collect#2.1` -> `ci:collect`, `fix:src/a.c` -> `fix`). A row's agents are the
transcripts that used its model, and the total counts each transcript once. Its span
sums their first-to-last records, idle time included; it is `-` when one of them also
used another model, since that time cannot be split between the two.

The total is claude's own: the session's last cost-state record. Each model's cost
there is allocated over its rows by RATES, so a row's dollars are a share of claude's
figure (generic weights for a model with no rate). A model the record lists and no
transcript shows keeps its cost on an `unallocated` row; a model it does not list has
`-`. With no record at all, every row is priced at RATES (`-` for a model with no
rate). A note under the table says the figures cover the whole session, and when there
was no record.

Two breakout tables follow, each summing the rows above with its share of the total:
by part (each Workflow run, named by its saved workflow; chief's own turns; each role
chief ran itself; any unallocated cost) and by model. A sum is marked `(partial)` when one
of its rows has no dollars.

A time table ends it: each Workflow run's wall, the busy time of its stages by kind
(setup: state:load and preflight; CI: the ci: stages; other: the rest) and the gap
before it; the note under the table defines them.

With --full, that output goes to FILE, and stdout gets the report's brief form, pointing
to FILE: a Launches table, one row per Workflow run from its record beside the
transcripts (outcome, what it did, its wall with the CI lane's busy time, its tokens and
dollars), chief's own turns, its units and the total, with any run its record shows
refused before an agent ran listed under it and a State line from the last pr-babysit
launch's record (its expected head, which may precede chief's later commits, non-zero
finding and CI counts, cycles used); then a Usage table, one row per stage or chief role summed
over runs, the rows under FOLD dollars folded into one, its total split by model.
"""
import argparse
import functools
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / '.claude' / 'projects'
PARTS = ('in', 'out', 'read', 'w5m', 'w1h')
# $ per million tokens for PARTS, by model id prefix; each fits claude's cost-state
# records exactly (2026-09). GENERIC is only a weighting, for a model not listed.
RATES = {'claude-opus-5-5': (4, 20, 0.2, 5, 8), 'claude-sonnet-5': (2, 10, 0.2, 2.5, 4),
         'claude-haiku-4-5': (1, 5, 0.1, 1.25, 2)}
GENERIC = (1, 5, 0.1, 1.25, 2)


def rate_of(model):
    """The rate of a listed model id, bare or with a suffix: `-<date>` or `[1m]`."""
    return next((v for k, v in RATES.items() if model == k or model.startswith((k + '-', k + '['))), None)


def priced_at(rate, tokens):
    return sum(r * tokens[k] for r, k in zip(rate, PARTS)) / 1e6


class Failed(Exception):
    pass


def session_dir(session_id=None, journal=None):
    if session_id:
        found = sorted(PROJECTS.glob(f'*/{session_id}.jsonl'))
        if len(found) != 1:
            raise Failed(f'{len(found)} transcripts named {session_id}.jsonl under {PROJECTS}')
        return found[0].with_suffix('')
    path = Path(journal).resolve()
    if path.name != 'journal.jsonl' or len(path.parents) < 4 or path.parents[1].name != 'workflows' \
            or path.parents[2].name != 'subagents':
        raise Failed(f'{journal} is not <session>/subagents/workflows/<run>/journal.jsonl')
    if not path.is_file():
        raise Failed(f'no journal {journal}')
    return path.parents[3]


def records(path):
    for line in path.open(encoding='utf-8'):
        try:
            yield json.loads(line)
        except ValueError:
            continue   # a transcript being written can end mid-line


@functools.lru_cache(maxsize=None)
def usage_of(path):
    """{model: {turns, peak, tokens: Counter of PARTS}}, first and last timestamp, of one transcript."""
    last, t0, t1 = {}, None, None
    for e in records(path):
        ts = e.get('timestamp')
        if ts:
            t0, t1 = t0 or ts, ts
        msg = e.get('message') or {}
        if e.get('type') == 'assistant' and isinstance(msg, dict) and msg.get('usage'):
            last[msg.get('id')] = (msg.get('model', '?'), msg['usage'])   # streamed lines repeat a message; its last holds the final usage
    by_model = {}
    for model, u in last.values():
        split = u.get('cache_creation') or {}
        part = {'in': u.get('input_tokens', 0), 'out': u.get('output_tokens', 0), 'read': u.get('cache_read_input_tokens', 0),
                'w1h': split.get('ephemeral_1h_input_tokens', 0)}
        part['w5m'] = u.get('cache_creation_input_tokens', 0) - part['w1h']
        m = by_model.setdefault(model, {'turns': 0, 'peak': 0, 'tokens': Counter()})
        m['turns'] += 1
        m['peak'] = max(m['peak'], part['in'] + part['read'] + part['w5m'] + part['w1h'])
        m['tokens'].update(part)
    return by_model, t0, t1


def cost_state(transcript):
    """The last cost-state's {model: $}, or None."""
    state = None
    for e in records(transcript):
        if e.get('type') == 'cost-state':
            state = {k: v.get('costUSD') for k, v in (e.get('modelUsage') or {}).items()}
    return state


def stage_of(label):
    head = label.split('#', 1)[0]
    return head.split(':', 1)[0] if re.search(r':.*[/.]', head) else head


def seconds(t0, t1):
    if not (t0 and t1):
        return 0.0
    parse = lambda t: datetime.fromisoformat(t.replace('Z', '+00:00'))
    return (parse(t1) - parse(t0)).total_seconds()


def meta_of(meta_path):
    try:
        return json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}


def transcript_of(meta_path):
    return meta_path.with_name(meta_path.name.replace('.meta.json', '.jsonl'))


def row(group, stage, model, **given):
    return {'group': group, 'stage': stage, 'model': model, 'paths': set(), 'turns': 0, 'peak': 0,
            'tokens': Counter(), 'secs': 0.0, 'cost': None, **given}


def rows_for(group, agents):
    """Rows of one group from [(stage, transcript)], each row a dict."""
    acc = {}
    for stage, path in agents:
        by_model, t0, t1 = usage_of(path)
        for model, m in by_model.items():
            r = acc.setdefault((stage, model), row(group, stage, model))
            r['paths'].add(path)
            r['secs'] = seconds(t0, t1) + r['secs'] if len(by_model) == 1 and r['secs'] is not None else None
            r['turns'] += m['turns']
            r['peak'] = max(r['peak'], m['peak'])
            r['tokens'].update(m['tokens'])
    return sorted(acc.values(), key=lambda r: -priced_at(rate_of(r['model']) or GENERIC, r['tokens']))


def runs_of(session):
    """[(start, run name, [(stage, transcript)])] of the session's Workflow runs, in start order."""
    runs = []
    for run in (session / 'subagents' / 'workflows').glob('wf_*'):
        agents = [(stage_of(meta_of(m).get('description') or '?'), transcript_of(m)) for m in run.glob('agent-*.meta.json')]
        agents = [(s, p) for s, p in agents if p.exists()]
        start = min((usage_of(p)[1] or '~' for _, p in agents), default='~')
        runs.append((start, run.name, agents))
    return sorted(runs)


def collect(session):
    rows = []
    for _, name, agents in runs_of(session):
        rows += rows_for(name, agents)
    own = [('chief', session.with_suffix('.jsonl'))]
    own += [(f'chief:{meta_of(m).get("agentType") or "?"}', transcript_of(m)) for m in (session / 'subagents').glob('agent-*.meta.json')]
    return rows + rows_for('chief', [(s, p) for s, p in own if p.exists()])


def priced(rows, state):
    """Sets each row's share of its model's recorded cost, or None."""
    if state is None:
        for r in rows:
            rate = rate_of(r['model'])
            if rate:
                r['cost'] = priced_at(rate, r['tokens'])
        return rows
    tokens = defaultdict(Counter)
    for r in rows:
        tokens[r['model']].update(r['tokens'])
    for model, total in tokens.items():
        rate = rate_of(model)
        cost = state.get(model)
        if cost is None:
            continue
        weight = lambda t: priced_at(rate or GENERIC, t)
        for r in (r for r in rows if r['model'] == model):
            r['cost'] = cost * weight(r['tokens']) / weight(total) if weight(total) else None
    # a model claude billed that no transcript on disk shows: its cost stays in the total, unallocated
    for model, cost in state.items():
        if cost is not None and model not in tokens:
            rows.append(row('-', 'unallocated: no transcript', model, cost=cost))
    return rows


def total_cell(rows):
    """The sum of rows' dollars, `(partial)` when one has none, or '-'."""
    known = [r['cost'] for r in rows if r['cost'] is not None]
    if not known:
        return '-'
    return f'{sum(known):.2f}' + ('' if len(known) == len(rows) else ' (partial)')


def table(rows):
    money = lambda r: '-' if r['cost'] is None else f'{r["cost"]:.2f}'
    span = lambda r: '-' if r['secs'] is None else f'{r["secs"]:.0f} s'
    out = ['| run | stage | model | agents | turns | peak context | output | allocated $ | span |',
           '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        out.append(f'| {r["group"]} | {r["stage"]} | {r["model"].removeprefix("claude-")} | {len(r["paths"])} | {r["turns"]} | '
                   f'{r["peak"]:,} | {r["tokens"]["out"]:,} | {money(r)} | {span(r)} |')
    out.append(f'| **total, claude\'s $** | | | {len(set().union(*(r["paths"] for r in rows)))} | {sum(r["turns"] for r in rows)} | | '
               f'{sum(r["tokens"]["out"] for r in rows):,} | **{total_cell(rows)}** | |')
    return '\n'.join(out)


def breakout(rows, session):
    """The two breakout tables: spend by part and by model, each with its share of the total."""
    def part(r):
        if r['group'] == 'chief':
            return 'chief (own turns)' if r['stage'] == 'chief' else r['stage']
        if r['group'] == '-':
            return 'unallocated'
        name = workflow_name(session, r['group'])
        return f'{r["group"]} ({name})' if name else r['group']
    total = sum(r['cost'] for r in rows if r['cost'] is not None)
    def lines(key, head):
        groups = defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        known = lambda g: sum(r['cost'] for r in g if r['cost'] is not None)
        out = [f'| {head} | $ | share |', '|---|---:|---:|']
        for name, g in sorted(groups.items(), key=lambda kv: -known(kv[1])):
            share = f'{100 * known(g) / total:.0f}%' if total and any(r['cost'] is not None for r in g) else '-'
            out.append(f'| {name} | {total_cell(g)} | {share} |')
        return out
    return '\n'.join(lines(part, 'part')) + '\n\n' + '\n'.join(lines(lambda r: r['model'].removeprefix('claude-'), 'model'))


SETUP = ('state:load', 'preflight')
FOLD = 0.60   # dollars: a Usage row below it folds into `other`


def lane_of(stage):
    return 'setup' if stage.removesuffix('.retry') in SETUP else 'ci' if stage.startswith('ci:') else 'other'


def busy(intervals):
    """Seconds covered by the union of [(t0, t1)] ISO intervals."""
    total, end = 0.0, None
    for t0, t1 in sorted(intervals):
        if end is None or t0 > end:
            total, end = total + seconds(t0, t1), t1
        elif t1 > end:
            total, end = total + seconds(end, t1), t1
    return total


def minutes(s):
    return f'{s / 60:.1f}'


def walls_of(session):
    """([(run name, first record, last record, {kind: busy seconds})] in start order, session wall seconds, runs' union seconds)."""
    _, s0, s1 = usage_of(session.with_suffix('.jsonl'))
    runs, last = [], s0
    for _, name, agents in runs_of(session):
        spans = [(lane_of(stage), *usage_of(p)[1:]) for stage, p in agents]
        spans = [(lane, t0, t1) for lane, t0, t1 in spans if t0 and t1]
        if not spans:
            continue
        r0, r1 = min(t0 for _, t0, _ in spans), max(t1 for _, _, t1 in spans)
        runs.append((name, r0, r1, {k: busy([(t0, t1) for x, t0, t1 in spans if x == k]) for k in ('setup', 'ci', 'other')}))
        last = max(last, r1) if last else r1
    return runs, seconds(s0, max(s1 or '', last or '')), busy([(r0, r1) for _, r0, r1, _ in runs])


def timeline(session, walls):
    """The Markdown time table: each Workflow run's wall and the busy time of its stages by kind, and the gap before it."""
    out = ['| run | start UTC | wall min | setup min | CI lane min | other work min | gap before it min |',
           '|---|---|---:|---:|---:|---:|---:|']
    runs, session_wall, union = walls
    last = usage_of(session.with_suffix('.jsonl'))[1]
    for name, r0, r1, lane in runs:
        out.append(f'| {name} | {r0[11:19]} | {minutes(seconds(r0, r1))} | {minutes(lane["setup"])} | {minutes(lane["ci"])} | '
                   f'{minutes(lane["other"])} | {minutes(max(0.0, seconds(last, r0)))} |')
        last = max(last, r1) if last else r1
    out.append(f'\nSession wall {minutes(session_wall)} min, of which Workflow runs {minutes(union)} min. A run\'s wall runs from its '
               'first agent record to its last; a kind\'s time is the union of its agents\' spans, idle waits included (ci:collect waits '
               'on CI), and kinds overlap. The gap before a run is everything chief did since the previous one: its own turns, its '
               'units, writers, checks and review rounds.')
    return '\n'.join(out)


@functools.lru_cache(maxsize=None)
def record_of(session, run):
    """A run's record beside the transcripts, or {}."""
    try:
        r = json.loads((session / 'workflows' / f'{run}.json').read_text())
    except (OSError, ValueError):
        return {}
    return r if isinstance(r, dict) else {}


def workflow_name(session, run):
    """The saved workflow a run executed, or None."""
    return record_of(session, run).get('workflowName')


def cell(text):
    """Text safe in one Markdown table cell."""
    return ' '.join(str(text).replace('|', '\\|').split())


def outcome(record):
    result, error = record.get('result'), record.get('error')
    if isinstance(result, dict) and result.get('status'):
        return ': '.join(str(x) for x in (result['status'], result.get('reason')) if x)
    if error:
        return str(error).splitlines()[0].removeprefix('Error: ')
    return record.get('status') or 'no record'


def rollup_of(record):
    result = record.get('result')
    return (result.get('rollup') if isinstance(result, dict) else None) or {}


def did(record):
    """What a pr-babysit run's rollup says it changed, or '–'."""
    ro = rollup_of(record)
    fixed = sum(((ro.get(k) or {}).get('fixed') or 0) for k in ('findings', 'ci'))
    replies = ro.get('replies') or 0
    parts = ([f'fixed {fixed}'] if fixed else []) + ([f'{replies} repl{"y" if replies == 1 else "ies"}'] if replies else [])
    if ro.get('pushed'):
        parts.append('pushed ' + ', '.join(str(sha)[:7] for sha in ro['pushed']))
    return ', '.join(parts) or '–'


FINDINGS = (('fixed', 'fixed'), ('open', 'open'), ('refuted', 'refuted'), ('stale', 'stale'), ('deferred', 'deferred'), ('held', 'held'))
CI = (('fixed', 'fixed'), ('open', 'open'), ('accepted', 'accepted'), ('sonarGate', 'Sonar gate'), ('rigSide', 'rig-side'),
      ('unclassified', 'unclassified'))


def state_line(numbered):
    """The State line of the last pr-babysit launch in [(number, record)], or ''."""
    last = next(((i, r) for i, r in reversed(numbered) if r.get('workflowName') == 'pr-babysit'), None)
    if last is None:
        return ''
    i, rec = last
    result = rec.get('result')
    st = result.get('state') if isinstance(result, dict) else None
    if not isinstance(st, dict):
        return f'State after launch {i}: not recorded.'
    ro = rollup_of(rec)
    def words(counts, names):
        if not isinstance(counts, dict):
            return 'unavailable'
        return ' · '.join(f'{counts[k]} {w}' for k, w in names if counts.get(k)) or 'none'
    return (f'State after launch {i}: {str(st.get("expectedHead") or "?")[:7]}; findings {words(ro.get("findings"), FINDINGS)}; '
            f'CI {words(ro.get("ci"), CI)}; {st.get("cyclesUsed", "?")}/{st.get("maxCycles", "?")} cycles used.')


def kilo(n):
    return f'{n / 1000:.0f} k' if n else '–'


def paths(rows):
    return set().union(*(r['paths'] for r in rows))


def launches(session, rows, walls):
    """The brief form's Launches table, the runs refused before any agent ran, and the State line."""
    runs, session_wall, union = walls
    timed = {name: (seconds(r0, r1), lane['ci']) for name, r0, r1, lane in runs}
    names = {r['group'] for r in rows if r['group'] not in ('chief', '-')} | {p.stem for p in (session / 'workflows').glob('wf_*.json')}
    names = sorted(names, key=lambda n: (record_of(session, n).get('startTime') or 0, n))
    out = ['| # | outcome | did | time | tokens | $ |', '|---|---|---|---:|---:|---:|']
    refused, numbered = [], []
    for name in names:
        rec, mine = record_of(session, name), [r for r in rows if r['group'] == name]
        if rec.get('agentCount') == 0 and not rec.get('totalTokens') and rec.get('error') and not mine and name not in timed:
            refused.append(f'{name} ({outcome(rec)})')
            continue
        numbered.append((len(numbered) + 1, rec))
        i = len(numbered)
        wall, ci = timed.get(name, ((rec.get('durationMs') or 0) / 1000, 0.0))
        when = f'{minutes(wall)} m' + (f' ({minutes(ci)} CI)' if ci else '')
        out.append(f'| {i} | {cell(outcome(rec))} | {cell(did(rec))} | {when} | {kilo(rec.get("totalTokens") or 0)} | '
                   f'{total_cell(mine) if mine else "-"} |')
    own = [r for r in rows if r['group'] == 'chief' and r['stage'] == 'chief']
    units = [r for r in rows if r['group'] == 'chief' and r['stage'] != 'chief']
    out.append(f'| chief | own turns | | {minutes(max(0.0, session_wall - union))} m | | {total_cell(own) if own else "-"} |')
    if units:
        by_role = defaultdict(list)
        for r in units:
            by_role[r['stage']].append(r)
        role, g = max(by_role.items(), key=lambda kv: sum(r['cost'] or 0 for r in kv[1]))
        label = 'biggest known' if any(r['cost'] is None for r in units) else 'biggest'
        out.append(f'| chief units | {label}: {cell(role.removeprefix("chief:"))} ×{len(paths(g))} | | | | {total_cell(units)} |')
    out.append(f'| **total** | | | **{minutes(session_wall)} m** | | **{total_cell(rows)}** |')
    if refused:
        out.append(f'\nRefused before any agent ran: {cell("; ".join(refused))}.')
    line = state_line(numbered)
    return '\n'.join(out) + (f'\n\n{line}' if line else '')


def family(model):
    return model.removeprefix('claude-').split('-')[0]


def usage(rows):
    """The brief form's Usage table: each stage or chief role summed over runs, the small ones folded."""
    groups = defaultdict(list)
    for r in rows:
        groups['chief (own turns)' if (r['group'], r['stage']) == ('chief', 'chief') else r['stage']].append(r)
    known = lambda g: sum(r['cost'] for r in g if r['cost'] is not None)
    order = sorted(groups.items(), key=lambda kv: -known(kv[1]))
    small = {k for k, g in order if all(r['cost'] is not None for r in g) and known(g) < FOLD}
    if len(small) < 2:
        small = set()
    out = ['| stage / agent | model | launches | agents | output | busy | $ |', '|---|---|---:|---:|---:|---:|---:|']
    for name, g in order:
        if name in small:
            continue
        runs = {r['group'] for r in g}
        secs = None if name == 'chief (own turns)' or any(r['secs'] is None for r in g) else sum(r['secs'] for r in g)
        out.append(f'| {cell(name)} | {cell(", ".join(sorted({family(r["model"]) for r in g})))} | {"–" if runs & {"chief", "-"} else len(runs)} | '
                   f'{len(paths(g))} | {kilo(sum(r["tokens"]["out"] for r in g))} | {"–" if secs is None else minutes(secs) + " m"} | {total_cell(g)} |')
    if small:
        g = [r for k in small for r in groups[k]]
        out.append(f'| other ({len(small)} under ${FOLD:.2f}) | | | {len(paths(g))} | {kilo(sum(r["tokens"]["out"] for r in g))} | | {total_cell(g)} |')
    total = known(rows)
    by_model = defaultdict(float)
    for r in rows:
        by_model[family(r['model'])] += r['cost'] or 0
    split = ' · '.join(f'{cell(m)} {100 * c / total:.0f}%' for m, c in sorted(by_model.items(), key=lambda kv: -kv[1])) if total else ''
    out.append(f'| **total** | {split} | | {len(paths(rows))} | {kilo(sum(r["tokens"]["out"] for r in rows))} | | **{total_cell(rows)}** |')
    return '\n'.join(out)


def report(session, full_path=None):
    """(the brief form, or None without full_path, the full output, its total cell) of a session directory; with
    full_path, the full output is written there and the brief points to it."""
    transcript = session.with_suffix('.jsonl')
    if not transcript.exists():
        raise Failed(f'no transcript {transcript}')
    record_of.cache_clear()   # records change between calls in one process
    state = cost_state(transcript)
    rows, walls = priced(collect(session), state), walls_of(session)
    notes = [f'Scope: the whole session {session.name}, every Workflow run and agent in it and its own turns, not one workflow alone.']
    if state is None:
        notes.append('No cost-state record in the transcript: every $ is its tokens at RATES.')
    full = table(rows) + '\n\n' + breakout(rows, session) + '\n\n' + timeline(session, walls) + '\n\n' + '\n'.join(notes)
    if full_path is None:
        return None, full, total_cell(rows)
    full_path.write_text(full + '\n', encoding='utf-8')
    brief = (f'## Launches\n\n{launches(session, rows, walls)}\n\n## Usage by stage and agent\n\n{usage(rows)}\n\n'
             + ''.join(n + '\n' for n in notes[1:]) + f'Per launch × stage × model and the time breakdown: {full_path}')
    return brief, full, total_cell(rows)


def summary(session):
    """(the full Markdown output, its total cell) of a session directory."""
    return report(session)[1:]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--session-id')
    g.add_argument('--journal')
    p.add_argument('--full', type=Path, help='write the full output here and print the brief form')
    a = p.parse_args(argv)
    try:
        brief, full, _ = report(session_dir(a.session_id, a.journal), a.full)
        print(brief if a.full else full)
    except Failed as e:
        print(f'run_cost: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
