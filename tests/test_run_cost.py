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
    """model=(dollars, [input, output, cache read, cache write]) as claude records them."""
    keys = ('inputTokens', 'outputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens')
    return {'type': 'cost-state', 'modelUsage': {m: {'costUSD': c, **dict(zip(keys, n))} for m, (c, n) in models.items()}}


# the fixture's tokens per model, and their dollars at RATES, as a record counting every turn holds them
HAIKU = [2000, 18000, 0, 200000]      # $0.342
OPUS = [0, 45000, 2200000, 500000]    # $5.34


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
        return {(c[0], c[1], c[2]): c for c in ([x.strip() for x in line.strip('|').split('|')] for line in text.splitlines()[2:-1])}

    def test_stages_and_models_share_claudes_own_cost(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': (0.342, HAIKU), 'claude-opus-5-5': (5.34, OPUS)})])
        rc, out, _ = self.main('--session-id', SID)
        self.assertEqual(rc, 0)
        rows = self.rows(out)
        collect = rows[('wf_aaa', 'ci:collect', 'haiku-4-5')]
        self.assertEqual(collect[3:8], ['2', '2', '101,000', '18,000', '0.34'], 'the last line of a streamed message counts, once')
        self.assertIn(('wf_aaa', 'fix', 'opus-5-5'), rows, 'a path label is staged by its prefix')
        self.assertIn(('chief', 'chief:Explore', 'opus-5-5'), rows)
        self.assertIn(('chief', 'chief', 'opus-5-5'), rows)
        self.assertRegex(out.splitlines()[-1], r'\*\*5\.68\*\*', 'the rows add up to claude\'s total, exact where the rates price it')
        # Opus reads cost 0.05x input: the read-heavy Explore row gets less than flat 0.1x weights would give it
        self.assertEqual(rows[('chief', 'chief:Explore', 'opus-5-5')][7], '0.60')

    def test_a_cost_the_rates_do_not_price_or_an_unknown_model_is_estimated(self):
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': (0.342, HAIKU), 'claude-opus-5-5': (4.00, OPUS)})])
        rows = self.rows(self.main('--session-id', SID)[1])
        self.assertEqual(rows[('wf_aaa', 'ci:collect', 'haiku-4-5')][7], '0.34')
        self.assertTrue(rows[('wf_aaa', 'fix', 'opus-5-5')][7].endswith(' est.'), 'fast mode or a price change')
        with mock.patch.dict(run_cost.RATES, clear=True):
            write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': (0.342, HAIKU), 'claude-opus-5-5': (5.34, OPUS)})])
            self.assertTrue(self.rows(self.main('--session-id', SID)[1])[('wf_aaa', 'ci:collect', 'haiku-4-5')][7].endswith(' est.'))

    def test_tokens_the_record_did_not_count_mark_that_models_dollars_estimated(self):
        # the record predates chief's turn: its Opus count lacks that turn's 5,000 output and 300,000 written
        write(self.session.with_suffix('.jsonl'), [cost_state(**{'claude-haiku-4-5': (0.342, HAIKU), 'claude-opus-5-5': (2.84, [0, 40000, 2200000, 200000])})] + self.chief)
        out = self.main('--journal', str(self.journal))[1]
        rows = self.rows(out)
        self.assertEqual(rows[('wf_aaa', 'ci:collect', 'haiku-4-5')][7], '0.34', 'a model the record counted in full is exact')
        self.assertTrue(rows[('wf_aaa', 'fix', 'opus-5-5')][7].endswith(' est.'))
        self.assertIn('**3.18 est.**', out.splitlines()[-1])
        # a transcript missing from disk is the same mismatch the other way
        (self.session / 'subagents' / 'agent-b1.jsonl').unlink()
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': (0.342, HAIKU), 'claude-opus-5-5': (5.34, OPUS)})])
        self.assertTrue(self.rows(self.main('--session-id', SID)[1])[('wf_aaa', 'fix', 'opus-5-5')][7].endswith(' est.'))

    def test_no_cost_record_or_an_unlisted_model_leaves_dollars_out(self):
        write(self.session.with_suffix('.jsonl'), self.chief)
        out = self.main('--session-id', SID)[1]
        self.assertTrue(out.splitlines()[-1].endswith('| **-** | |'))
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-opus-5-5': (5.34, OPUS)})])
        out = self.main('--session-id', SID)[1]
        self.assertEqual(self.rows(out)[('wf_aaa', 'ci:collect', 'haiku-4-5')][7], '-')
        self.assertIn('5.34 (partial)', out.splitlines()[-1])

    def test_a_billed_model_with_no_transcript_keeps_its_cost_unallocated(self):
        for aid in ('a1', 'a2'):
            (self.journal.parent / f'agent-{aid}.jsonl').unlink()
        write(self.session.with_suffix('.jsonl'), self.chief + [cost_state(**{'claude-haiku-4-5': (0.342, HAIKU), 'claude-opus-5-5': (5.34, OPUS)})])
        out = self.main('--session-id', SID)[1]
        self.assertEqual(self.rows(out)[('-', 'unallocated: no transcript', 'haiku-4-5')][7], '0.34')
        self.assertIn('**5.68**', out.splitlines()[-1], 'the total stays claude\'s')

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
        self.assertEqual(out.splitlines()[-1].split('|')[4].strip(), '5', 'the total counts each transcript once')

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
