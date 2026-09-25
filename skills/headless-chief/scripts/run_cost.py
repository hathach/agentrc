#!/usr/bin/env python3
"""What a chief session spent, by Workflow run, stage and model, from its transcripts.

    run_cost.py (--session-id ID | --journal PATH)

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
figure. A model's figures are marked est. when its rate is unknown (generic weights
then), when the rates priced over its tokens miss claude's cost by a cent or more
(fast mode, a price change), or when its tokens in the transcripts differ from what
the record counted (a session still running, a missing transcript). A model the
record lists and no transcript shows keeps its cost on an `unallocated` row; a model it
does not list has `-`. With no record at all, every row is priced at RATES and marked est.
(`-` for a model with no rate). A note under the table says the figures cover the whole
session, and when there was no record.
"""
import argparse
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
    """The last cost-state's {model: ($, [input, output, cache read, cache write] tokens)}, or None."""
    state = None
    for e in records(transcript):
        if e.get('type') == 'cost-state':
            state = {k: (v.get('costUSD'), [v.get('inputTokens'), v.get('outputTokens'), v.get('cacheReadInputTokens'),
                                            v.get('cacheCreationInputTokens')]) for k, v in (e.get('modelUsage') or {}).items()}
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
            'tokens': Counter(), 'secs': 0.0, 'cost': None, 'est': False, **given}


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


def collect(session):
    runs = []
    for run in (session / 'subagents' / 'workflows').glob('wf_*'):
        agents = [(stage_of(meta_of(m).get('description') or '?'), transcript_of(m)) for m in run.glob('agent-*.meta.json')]
        agents = [(s, p) for s, p in agents if p.exists()]
        start = min((usage_of(p)[1] or '~' for _, p in agents), default='~')
        runs.append((start, run.name, agents))
    rows = []
    for _, name, agents in sorted(runs):
        rows += rows_for(name, agents)
    own = [('chief', session.with_suffix('.jsonl'))]
    own += [(f'chief:{meta_of(m).get("agentType") or "?"}', transcript_of(m)) for m in (session / 'subagents').glob('agent-*.meta.json')]
    return rows + rows_for('chief', [(s, p) for s, p in own if p.exists()])


def priced(rows, state):
    """Sets each row's share of its model's recorded cost, or None, and whether it is estimated."""
    if state is None:
        for r in rows:
            rate = rate_of(r['model'])
            if rate:
                r['cost'], r['est'] = priced_at(rate, r['tokens']), True
        return rows
    tokens = defaultdict(Counter)
    for r in rows:
        tokens[r['model']].update(r['tokens'])
    for model, total in tokens.items():
        rate = rate_of(model)
        cost, recorded = state.get(model, (None, None))
        if cost is None:
            continue
        weight = lambda t: priced_at(rate or GENERIC, t)
        counted = [total['in'], total['out'], total['read'], total['w5m'] + total['w1h']]
        est = rate is None or abs(weight(total) - cost) >= 0.01 or recorded != counted
        for r in (r for r in rows if r['model'] == model):
            r['cost'], r['est'] = (cost * weight(r['tokens']) / weight(total) if weight(total) else None), est
    # a model claude billed that no transcript on disk shows: its cost stays in the total, unallocated
    for model, (cost, _) in state.items():
        if cost is not None and model not in tokens:
            rows.append(row('-', 'unallocated: no transcript', model, cost=cost))
    return rows


def total_cell(rows):
    """The total as the table shows it: claude's dollars, marked est. or partial, or '-'."""
    known = [r['cost'] for r in rows if r['cost'] is not None]
    if not known:
        return '-'
    return f'{sum(known):.2f}' + (' est.' if any(r['est'] for r in rows) else '') + ('' if len(known) == len(rows) else ' (partial)')


def table(rows):
    money = lambda r: '-' if r['cost'] is None else f'{r["cost"]:.2f}{" est." if r["est"] else ""}'
    span = lambda r: '-' if r['secs'] is None else f'{r["secs"]:.0f} s'
    out = ['| run | stage | model | agents | turns | peak context | output | allocated $ | span |',
           '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        out.append(f'| {r["group"]} | {r["stage"]} | {r["model"].removeprefix("claude-")} | {len(r["paths"])} | {r["turns"]} | '
                   f'{r["peak"]:,} | {r["tokens"]["out"]:,} | {money(r)} | {span(r)} |')
    out.append(f'| **total, claude\'s $** | | | {len(set().union(*(r["paths"] for r in rows)))} | {sum(r["turns"] for r in rows)} | | '
               f'{sum(r["tokens"]["out"] for r in rows):,} | **{total_cell(rows)}** | |')
    return '\n'.join(out)


def summary(session):
    """(the Markdown table, its total cell) of a session directory."""
    transcript = session.with_suffix('.jsonl')
    if not transcript.exists():
        raise Failed(f'no transcript {transcript}')
    state = cost_state(transcript)
    rows = priced(collect(session), state)
    notes = [f'Scope: the whole session {session.name}, every Workflow run and agent in it and its own turns, not one workflow alone.']
    if state is None:
        notes.append('No cost-state record in the transcript: every $ is its tokens at RATES, an estimate.')
    return table(rows) + '\n\n' + '\n'.join(notes), total_cell(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--session-id')
    g.add_argument('--journal')
    a = p.parse_args(argv)
    try:
        print(summary(session_dir(a.session_id, a.journal))[0])
    except Failed as e:
        print(f'run_cost: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
