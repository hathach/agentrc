"""Tests for headless-chief's run_cost.py on a hand-built session tree."""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'headless-chief' / 'scripts' / 'run_cost.py'
spec = importlib.util.spec_from_file_location('run_cost', SCRIPT)
run_cost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_cost)

SID = '11111111-2222-3333-4444-555555555555'


def turn(mid, model, ts, inp=0, out=0, read=0, w5m=0, w1h=0):
    inp, out, read, w5m, w1h = (100 * n for n in (inp, out, read, w5m, w1h))   # large enough to show dollars
    usage = {'input_tokens': inp, 'output_tokens': out, 'cache_read_input_tokens': read,
             'cache_creation_input_tokens': w5m + w1h,
             'cache_creation': {'ephemeral_5m_input_tokens': w5m, 'ephemeral_1h_input_tokens': w1h}}
    return {'type': 'assistant', 'timestamp': ts, 'message': {'id': mid, 'model': model, 'usage': usage}}


def cost_state(**models):
    """model=dollars as claude records them."""
    return {'type': 'cost-state', 'modelUsage': {m: {'costUSD': c} for m, c in models.items()}}


# the fixture's dollars per model at RATES, as a record counting every turn holds them
HAIKU = 0.342
OPUS = 5.34


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


class RunCostTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.projects = Path(tmp.name)
        patcher = mock.patch.object(run_cost, 'PROJECTS', self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)
        run_cost.usage_of.cache_clear()   # each test writes its own transcripts at the same paths
        self.session = self.projects / '-home-x-repo' / SID
        run = self.session / 'subagents' / 'workflows' / 'wf_aaa'
        self.journal = run / 'journal.jsonl'
        write(self.journal, [{'type': 'launched'}])
        agents = {
            # a streamed message: its first line carries partial output, its last the final usage
            'a1': ('ci:collect#1.1', [turn('m1', 'claude-haiku-4-5', '2026-09-25T10:00:00Z', inp=10, out=1, w5m=1000),
                                      turn('m1', 'claude-haiku-4-5', '2026-09-25T10:00:05Z', inp=10, out=90, w5m=1000)]),
            'a2': ('ci:collect#1.f', [turn('m2', 'claude-haiku-4-5', '2026-09-25T10:01:00Z', inp=10, out=90, w5m=1000)]),
            'a3': ('fix:src/a.c', [turn('m3', 'claude-opus-5-5', '2026-09-25T10:02:00Z', inp=0, out=100, read=5000, w1h=2000),
                                   turn('m4', 'claude-opus-5-5', '2026-09-25T10:02:30Z', inp=0, out=100, read=7000)]),
        }
        for aid, (label, records) in agents.items():
            (run / f'agent-{aid}.meta.json').write_text(json.dumps({'description': label, 'agentType': 'x'}))
            write(run / f'agent-{aid}.jsonl', records)
        own = self.session / 'subagents'
        (own / 'agent-b1.meta.json').write_text(json.dumps({'description': 'Inspect launch 1', 'agentType': 'Explore'}))
        write(own / 'agent-b1.jsonl', [turn('m5', 'claude-opus-5-5', '2026-09-25T10:03:00Z', out=200, read=10000)])
        self.chief = [turn('m6', 'claude-opus-5-5', '2026-09-25T09:59:00Z', out=50, w1h=3000)]

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = run_cost.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def rows(self, text):
        return {(c[0], c[1], c[2]): c for c in ([x.strip() for x in line.strip('|').split('|')] for line in self.table(text)[2:-1])}

    def table(self, text, n=0):
        """The nth Markdown table of the output: the cost table, then the two breakouts."""
        blocks, lines = [], text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith('|') and (i == 0 or not lines[i - 1].startswith('|')):
                blocks.append([])
            if line.startswith('|'):
                blocks[-1].append(line)
        return blocks[n]

    def breakout(self, text, n):
        return {c[0]: c[1:] for c in ([x.strip() for x in line.strip('|').split('|')] for line in self.table(text, n)[2:])}

    def test_stages_and_models_share_claudes_own_cost(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': HAIKU, 'claude-opus-5-5': OPUS})])
        rc, out, _ = self.main('--session-id', SID)
        self.assertEqual(rc, 0)
        rows = self.rows(out)
        collect = rows[('wf_aaa', 'ci:collect', 'haiku-4-5')]
        self.assertEqual(collect[3:8], ['2', '2', '101,000', '18,000', '0.34'], 'the last line of a streamed message counts, once')
        self.assertIn(('wf_aaa', 'fix', 'opus-5-5'), rows, 'a path label is staged by its prefix')
        self.assertIn(('chief', 'chief:Explore', 'opus-5-5'), rows)
        self.assertIn(('chief', 'chief', 'opus-5-5'), rows)
        self.assertRegex(self.table(out)[-1], r'\*\*5\.68\*\*', 'the rows add up to claude\'s total, exact where the rates price it')
        # Opus reads cost 0.05x input: the read-heavy Explore row gets less than flat 0.1x weights would give it
        self.assertEqual(rows[('chief', 'chief:Explore', 'opus-5-5')][7], '0.60')

    def test_the_breakouts_sum_the_rows_by_part_and_by_model(self):
        (self.session / 'workflows').mkdir(parents=True)
        (self.session / 'workflows' / 'wf_aaa.json').write_text(json.dumps({'runId': 'wf_aaa', 'workflowName': 'pr-babysit'}))
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': HAIKU, 'claude-opus-5-5': OPUS})])
        out = self.main('--session-id', SID)[1]
        rows, parts, models = self.rows(out), self.breakout(out, 1), self.breakout(out, 2)
        self.assertEqual(set(parts), {'wf_aaa (pr-babysit)', 'chief (own turns)', 'chief:Explore'}, 'a run is named by its saved workflow')
        run = sum(float(c[7]) for k, c in rows.items() if k[0] == 'wf_aaa')
        self.assertAlmostEqual(float(parts['wf_aaa (pr-babysit)'][0]), run, delta=0.011, msg='a part sums its rows across models')
        self.assertEqual(parts['chief:Explore'], ['0.60', '11%'])
        self.assertEqual(models, {'opus-5-5': ['5.34', '94%'], 'haiku-4-5': ['0.34', '6%']})
        self.assertEqual(list(parts)[0], 'wf_aaa (pr-babysit)', 'largest first')
        # without the run's record the id alone names it; a row with no dollars makes every sum it is in partial
        (self.session / 'workflows' / 'wf_aaa.json').unlink()
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-opus-5-5': OPUS})])
        out = self.main('--session-id', SID)[1]
        parts, models = self.breakout(out, 1), self.breakout(out, 2)
        self.assertTrue(parts['wf_aaa'][0].endswith(' (partial)'), 'its Haiku rows have no dollars')
        self.assertEqual(models['haiku-4-5'], ['-', '-'])

    def test_a_model_the_rates_misprice_or_do_not_know_still_sums_to_its_recorded_dollars(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': HAIKU, 'claude-opus-5-5': 4.00})])
        out = self.main('--session-id', SID)[1]
        self.assertEqual(self.rows(out)[('wf_aaa', 'ci:collect', 'haiku-4-5')][7], '0.34')
        self.assertEqual(self.breakout(out, 2)['opus-5-5'][0], '4.00', 'fast mode or a price change: the record, split by the rates')
        with mock.patch.dict(run_cost.RATES, clear=True):
            write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': HAIKU, 'claude-opus-5-5': OPUS})])
            out = self.main('--session-id', SID)[1]
            self.assertEqual(self.rows(out)[('wf_aaa', 'ci:collect', 'haiku-4-5')][7], '0.34')
            self.assertIn('**5.68**', self.table(out)[-1], 'generic weights split the recorded dollars')

    def test_a_journal_finds_its_session_and_a_missing_transcript_leaves_its_dollars_on_the_rest(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': HAIKU, 'claude-opus-5-5': OPUS})])
        self.assertIn('**5.68**', self.table(self.main('--journal', str(self.journal))[1])[-1])
        (self.session / 'subagents' / 'agent-b1.jsonl').unlink()
        self.assertEqual(self.breakout(self.main('--session-id', SID)[1], 2)['opus-5-5'][0], '5.34')

    def test_no_cost_record_prices_every_row_at_rates(self):
        write(self.session.with_suffix('.jsonl'), self.chief)
        out = self.main('--session-id', SID)[1]
        rows = self.rows(out)
        self.assertEqual(rows[('wf_aaa', 'ci:collect', 'haiku-4-5')][7], '0.34')
        self.assertEqual(rows[('wf_aaa', 'fix', 'opus-5-5')][7], f'{run_cost.priced_at(run_cost.RATES["claude-opus-5-5"], {"out": 20000, "read": 1200000, "w1h": 200000, "in": 0, "w5m": 0}):.2f}')
        self.assertIn('**5.68**', self.table(out)[-1], 'the same dollars a record would hold')
        self.assertNotIn('est.', out)
        self.assertIn('No cost-state record in the transcript', out)
        self.assertIn(f'Scope: the whole session {SID}', out)
        with mock.patch.dict(run_cost.RATES, clear=True):
            self.assertTrue(self.table(self.main('--session-id', SID)[1])[-1].endswith('| **-** | |'), 'no rate, no dollars')

    def test_an_unlisted_model_leaves_dollars_out(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-opus-5-5': OPUS})])
        out = self.main('--session-id', SID)[1]
        self.assertEqual(self.rows(out)[('wf_aaa', 'ci:collect', 'haiku-4-5')][7], '-')
        self.assertIn('5.34 (partial)', self.table(out)[-1])

    def test_a_billed_model_with_no_transcript_keeps_its_cost_unallocated(self):
        for aid in ('a1', 'a2'):
            (self.journal.parent / f'agent-{aid}.jsonl').unlink()
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': HAIKU, 'claude-opus-5-5': OPUS})])
        out = self.main('--session-id', SID)[1]
        self.assertEqual(self.rows(out)[('-', 'unallocated: no transcript', 'haiku-4-5')][7], '0.34')
        self.assertIn('**5.68**', self.table(out)[-1], 'the total stays claude\'s')

    def test_a_rate_belongs_to_its_model_and_its_suffixed_ids_only(self):
        opus = run_cost.RATES['claude-opus-5-5']
        for model in ('claude-opus-5-5', 'claude-opus-5-5[1m]', 'claude-opus-5-5-20260901'):
            self.assertEqual(run_cost.rate_of(model), opus, model)
        for model in ('claude-opus-5-50', 'claude-sonnet-50', 'claude-opus-5'):
            self.assertIsNone(run_cost.rate_of(model), model)

    def test_runs_are_listed_in_start_order(self):
        write(self.session.with_suffix('.jsonl'), self.chief)
        early = self.session / 'subagents' / 'workflows' / 'wf_zzz'
        (early / 'agent-c1.meta.json').parent.mkdir(parents=True)
        (early / 'agent-c1.meta.json').write_text(json.dumps({'description': 'preflight'}))
        write(early / 'agent-c1.jsonl', [turn('m7', 'claude-haiku-4-5', '2026-09-25T09:00:00Z', out=5)])
        groups = [c[0] for c in self.rows(self.main('--session-id', SID)[1]).values()]
        self.assertLess(groups.index('wf_zzz'), groups.index('wf_aaa'))

    def test_a_transcript_of_two_models_is_one_agent_whose_span_neither_row_claims(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [turn('m8', 'claude-haiku-4-5', '2026-09-25T11:59:00Z', out=1)])
        out = self.main('--session-id', SID)[1]
        rows = self.rows(out)
        for model in ('opus-5-5', 'haiku-4-5'):
            self.assertEqual(rows[('chief', 'chief', model)][3::5], ['1', '-'], model)
        self.assertEqual(rows[('wf_aaa', 'ci:collect', 'haiku-4-5')][8], '5 s', 'single-model transcripts keep their spans')
        self.assertEqual(self.table(out)[-1].split('|')[4].strip(), '5', 'the total counts each transcript once')

    def test_the_timeline_splits_each_runs_wall_by_kind_and_unions_the_runs(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [turn('m9', 'claude-opus-5-5', '2026-09-25T10:10:00Z', out=1)])
        later = self.session / 'subagents' / 'workflows' / 'wf_bbb'
        for aid, label, t0, t1 in (('d1', 'state:load#1', '10:05:00', '10:06:00'), ('d2', 'reviews#1', '10:06:00', '10:08:00'),
                                   ('d3', 'challenge#1', '10:07:00', '10:08:30'), ('d4', 'ci:collect#1.1', '10:06:00', '10:09:00')):
            (later / f'agent-{aid}.meta.json').parent.mkdir(parents=True, exist_ok=True)
            (later / f'agent-{aid}.meta.json').write_text(json.dumps({'description': label}))
            write(later / f'agent-{aid}.jsonl', [turn(f'n{aid}', 'claude-haiku-4-5', f'2026-09-25T{t}Z', out=1) for t in (t0, t1)])
        out = self.main('--session-id', SID)[1]
        rows = self.breakout(out, 3)
        self.assertEqual(rows['wf_aaa'], ['10:00:00', '2.5', '0.0', '0.1', '0.5', '1.0'], 'chief ran from 09:59; ci:collect 5 s and an instant, fix 30 s')
        self.assertEqual(rows['wf_bbb'], ['10:05:00', '4.0', '1.0', '3.0', '2.5', '2.5'], 'overlapping agents of one kind count once')
        self.assertIn('Session wall 11.0 min, of which Workflow runs 6.5 min.', out)
        # a second workflow running beside wf_bbb adds only what it covers beyond it
        beside = self.session / 'subagents' / 'workflows' / 'wf_ccc'
        beside.mkdir(parents=True)
        (beside / 'agent-e1.meta.json').write_text(json.dumps({'description': 'reviews#1'}))
        write(beside / 'agent-e1.jsonl', [turn(f'e{t}', 'claude-haiku-4-5', f'2026-09-25T{t}Z', out=1) for t in ('10:06:00', '10:10:00')])
        self.assertIn('Session wall 11.0 min, of which Workflow runs 7.5 min.', self.main('--session-id', SID)[1])
        self.assertEqual(run_cost.lane_of(run_cost.stage_of('preflight.retry')), 'setup', 'a relay retry keeps its stage kind')

    def test_the_brief_form_tables_launches_and_usage_and_writes_the_full_output(self):
        records = self.session / 'workflows'
        records.mkdir(parents=True)
        rollup = {'findings': {'total': 3, 'fixed': 1, 'refuted': 2, 'open': 0}, 'ci': {'total': 4, 'fixed': 1, 'rigSide': 3},
                  'replies': 2, 'pushed': ['44563dc0abc', 'e5380505def']}
        state = {'expectedHead': '9a8b7c6d5e4f', 'cyclesUsed': 4, 'maxCycles': 10}
        (records / 'wf_aaa.json').write_text(json.dumps({'runId': 'wf_aaa', 'startTime': 2, 'totalTokens': 271025, 'agentCount': 3, 'workflowName': 'pr-babysit',
                                                         'result': {'status': 'paused', 'reason': 'yielded', 'rollup': rollup, 'state': state}}))
        (records / 'wf_bad.json').write_text(json.dumps({'runId': 'wf_bad', 'startTime': 1, 'totalTokens': 0, 'status': 'failed',
                                                         'agentCount': 0, 'error': 'Error: deferral reason too long\n    at x'}))
        # an agent ran but left no usage: a failed run, not a refusal; a pipe in its reason stays in its cell
        (records / 'wf_died.json').write_text(json.dumps({'runId': 'wf_died', 'startTime': 3, 'totalTokens': 0, 'status': 'failed',
                                                          'agentCount': 1, 'error': 'Error: how must be refutation|fixNote'}))
        write(self.session.with_suffix('.jsonl'), self.chief + [turn('m9', 'claude-opus-5-5', '2026-09-25T10:10:00Z', out=1),
                                                                 cost_state(**{'claude-haiku-4-5': HAIKU, 'claude-opus-5-5': OPUS})])
        full = self.projects / 'cost.md'
        rc, out, _ = self.main('--session-id', SID, '--full', str(full))
        self.assertEqual(rc, 0)
        self.assertEqual(full.read_text(), self.main('--session-id', SID)[1], 'the full output, unchanged, goes to the file')
        launches = self.breakout(out, 0)
        self.assertEqual(launches['1'], ['paused: yielded', 'fixed 2, 2 replies, pushed 44563dc, e538050', '2.5 m (0.1 CI)', '271 k', '2.58'], 'its rows, as the part breakout sums them')
        self.assertEqual(launches['chief'], ['own turns', '', '8.5 m', '', '2.50'], 'the session wall less the runs')
        self.assertEqual(launches['chief units'], ['biggest: Explore ×1', '', '', '', '0.60'])
        self.assertEqual(launches['**total**'], ['', '', '**11.0 m**', '', '**5.68**'])
        self.assertIn('Refused before any agent ran: wf_bad (deferral reason too long).', out)
        self.assertIn('State after launch 1: 9a8b7c6; findings 1 fixed · 2 refuted; CI 1 fixed · 3 rig-side; 4/10 cycles used.', out)
        usage = self.breakout(out, 1)
        self.assertEqual(usage['chief (own turns)'], ['opus', '–', '1', '5 k', '–', '2.50'])
        self.assertEqual(usage['fix'], ['opus', '1', '1', '20 k', '0.5 m', '2.24'])
        self.assertEqual(usage['other (2 under $0.60)'], ['', '', '3', '38 k', '', '0.94'], 'ci:collect and chief:Explore fold')
        self.assertEqual(usage['**total**'], ['opus 94% · haiku 6%', '', '5', '63 k', '', '**5.68**'])
        self.assertTrue(out.rstrip().endswith(f'Per launch × stage × model and the time breakdown: {full}'))
        self.assertIn('| 2 | how must be refutation\\|fixNote | – | 0.0 m | – | - |', out)
        # a later pr-babysit launch that failed has no state: the line says so rather than show an older launch's
        rec = json.loads((records / 'wf_died.json').read_text())
        (records / 'wf_died.json').write_text(json.dumps({**rec, 'workflowName': 'pr-babysit'}))
        self.assertIn('State after launch 2: not recorded.', self.main('--session-id', SID, '--full', str(full))[1], 'records reread per report')
        # a record claiming no agent ran, beside an agent's rows, is a normal row
        rec = json.loads((records / 'wf_aaa.json').read_text())
        (records / 'wf_aaa.json').write_text(json.dumps({**rec, 'agentCount': 0, 'totalTokens': 0, 'error': 'Error: odd'}))
        self.assertIn('| 1 | paused: yielded |', self.main('--session-id', SID, '--full', str(full))[1])
        # a run with an unpriced row says so
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-opus-5-5': OPUS})])
        out = self.main('--session-id', SID, '--full', str(full))[1]
        self.assertTrue(self.breakout(out, 0)['1'][-1].endswith(' (partial)'), out)

    def test_a_session_it_cannot_place_is_refused(self):
        rc, _, err = self.main('--session-id', 'nope')
        self.assertEqual(rc, 1)
        self.assertIn('0 transcripts named nope.jsonl', err)
        rc, _, err = self.main('--journal', str(self.projects / 'journal.jsonl'))
        self.assertEqual(rc, 1)
        self.assertIn('is not <session>/subagents/workflows/<run>/journal.jsonl', err)
        rc, _, err = self.main('--journal', str(self.journal.parent.with_name('wf_gone') / 'journal.jsonl'))
        self.assertEqual((rc, 'no journal' in err), (1, True), 'a journal that does not exist names no session')
        rc, _, err = self.main('--journal', str(self.journal))
        self.assertEqual(rc, 1, 'the session transcript itself is missing')
        self.assertIn('no transcript', err)


if __name__ == '__main__':
    unittest.main()
