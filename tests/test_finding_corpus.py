"""Tests for bench/finding-verify: extraction over synthetic journals and transcripts, sampling, adjudication."""
import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'bench' / 'finding-verify' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract, sample, adjudicate = load('extract'), load('sample'), load('adjudicate')

BASE, HEAD = 'a' * 40, 'b' * 40
FINDING = {'file': 'src/x.c', 'line': 3, 'snippet': 'x = 1;', 'why': 'x is never read', 'severity': 'low', 'confidence': 'high'}
REAL = {'real': True, 'reason': 'holds', 'severity': 'low', 'impact': None, 'severityReason': 's', 'confidence': 'high'}
OPUS = 'claude-opus-5-5'


def prompt(head=HEAD, finding=FINDING, directory='src', dimension='correctness: logic'):
    return (f'Adversarially verify ONE review finding about {directory}.\nDimension: {dimension}\nFinding: {json.dumps(finding)}\n'
            f"Read the cited code. The checkout is at {head}. Judge only what `git diff {BASE} {head} -- ':(literal,top){directory}'` introduces.")


class Extract(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def journal(self, project, entries, check=None, framed=True):
        """entries: (label, result, prompt or None for no transcript, model)."""
        wf = self.root / 'projects' / project / 'session' / 'subagents' / 'workflows' / 'wf_1'
        wf.mkdir(parents=True)
        lines = [{'type': 'launched'}]
        if check:
            entries = [('check', check, None, None)] + entries
        for k, (label, result, text, model) in enumerate(entries):
            agent = f'{project}{k}'
            lines += [{'type': 'started', 'key': f'k{k}', 'label': label, 'agentId': agent},
                      {'type': 'result', 'key': f'k{k}', 'result': result}]
            if text is not None:
                body = 'Workflow harness frame. The computed task text follows:\n' + '\n'.join('  ' + l for l in text.split('\n')) if framed else text
                (wf / f'agent-{agent}.jsonl').write_text('\n'.join(map(json.dumps, [
                    {'type': 'user', 'message': {'role': 'user', 'content': body}},
                    {'type': 'assistant', 'message': {'model': model, 'content': []}}])) + '\n')
        (wf / 'journal.jsonl').write_text('\n'.join(map(json.dumps, lines)) + '\n')

    def run_it(self):
        out = self.root / 'cases.jsonl'
        printed = io.StringIO()
        with redirect_stdout(printed):
            extract.main([str(self.root / 'projects'), '--out', str(out)])
        return [json.loads(l) for l in out.read_text().splitlines()], printed.getvalue()

    def test_a_case_is_the_prompt_and_model_its_transcript_recorded(self):
        nit = {**FINDING, 'severity': 'nit'}
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-12', [
            ('verify:d0x1:0', REAL, prompt(), OPUS),
            # a nit the run still sent to Opus: the transcript, not today's routing, names the model
            ('verify:d0x1:1', {'real': False, 'reason': 'refuted'}, prompt(finding=nit, directory='docs (docs/a.md)'), OPUS),
        ], check={'head': HEAD, 'top': '/home/u/code/tinyusb/.worktrees/pr-review-12'})
        cases, _ = self.run_it()
        self.assertEqual([(c['repo'], c['pr'], c['base'], c['head']) for c in cases], [('hathach/tinyusb', 12, BASE, HEAD)] * 2)
        self.assertEqual([(c['dir'], c['finding'], c['verifier']['models'], c['verifier']['real']) for c in cases],
                         [('src', FINDING, [OPUS], True), ('docs (docs/a.md)', nit, [OPUS], False)])
        self.assertEqual(cases[0]['prompt'], prompt())
        self.assertEqual(cases[0]['dimension'], 'correctness: logic')

    def test_an_absolute_finding_path_names_its_worktree(self):
        top = '/home/u/code/tinyusb/.worktrees/pr-review-12'
        absolute = {**FINDING, 'file': f'{top}/src/class/x.c'}
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-12', [
            ('verify:d0x0:0', REAL, prompt(finding=absolute, directory='src/class'), OPUS),
            ('verify:d0x0:1', REAL, prompt(finding={**FINDING, 'file': '/elsewhere/x.c'}, directory='src/class'), OPUS),
            ('verify:d0x0:2', REAL, prompt(), OPUS),
        ], check={'head': HEAD, 'top': top})
        cases, printed = self.run_it()
        self.assertEqual([(c['path'], c['worktree']) for c in cases], [('src/class/x.c', top), ('src/x.c', None)])
        self.assertIn('1 verdict(s), absolute path outside the session worktree', printed)

    def test_the_worktree_is_the_prefix_the_project_directory_encodes(self):
        top = '/home/u/src/tinyusb/.worktrees/pr-review-12'
        absolute = {**FINDING, 'file': f'{top}/src/x.c'}
        self.journal('-home-u-src-tinyusb--worktrees-pr-review-12', [('verify:d0x0:0', REAL, prompt(finding=absolute), OPUS)],
                     check={'head': HEAD, 'top': top})
        self.journal('-home-u-src-tinyusb--worktrees-pr-review-13', [('verify:d0x0:0', REAL, prompt(finding=absolute, dimension='other'), OPUS)],
                     check={'head': HEAD})
        adjacent = {**FINDING, 'file': '/home/u/src/src/x.c'}
        self.journal('-home-u-src-tinyusb--worktrees-pr-review-14', [('verify:d0x0:0', REAL, prompt(finding=adjacent), OPUS)],
                     check={'head': HEAD})
        cases, printed = self.run_it()
        self.assertEqual([(c['path'], c['worktree']) for c in cases], [('src/x.c', top)])
        self.assertIn('2 verdict(s), absolute path outside the session worktree', printed)

    def test_a_diff_quoted_in_the_finding_is_not_the_scope(self):
        quoting = {**FINDING, 'why': f'unlike `git diff {"d" * 40} {"c" * 40}` showed'}
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-12', [('verify:d0x0:0', REAL, prompt(finding=quoting), OPUS)],
                     check={'head': HEAD, 'top': '/home/u/code/tinyusb/.worktrees/pr-review-12'})
        [case], _ = self.run_it()
        self.assertEqual((case['base'], case['finding']), (BASE, quoting))

    def test_an_unframed_prompt_and_an_old_check_without_a_worktree(self):
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-host--worktrees-pr-review-7',
                     [('verify:d1x0:0', REAL, prompt(), OPUS)], check={'head': HEAD}, framed=False)
        [case], _ = self.run_it()
        self.assertEqual((case['repo'], case['pr'], case['prompt']), ('hathach/tinyusb', 7, prompt()))

    def test_replays_dedupe_and_what_cannot_replay_is_counted(self):
        check = {'head': HEAD, 'top': '/x/agentrc/.worktrees/pr-review-4'}
        self.journal('p0', [('verify:d0x0:0', REAL, None, None)], check=check)  # a replay: no transcript of its own
        self.journal('p1', [('verify:d0x0:0', REAL, prompt(), OPUS)], check=check)
        self.journal('p2', [('verify:d0x0:0', REAL, prompt(), OPUS)], check=check)
        self.journal('p3', [('verify:d0x0:0', REAL, prompt(), OPUS)])
        self.journal('p4', [('verify:d0x0:0', REAL, prompt(head='c' * 40), OPUS)], check=check)
        self.journal('p5', [('verify:d0x0:0', REAL, 'some other task', OPUS)], check=check)
        self.journal('p6', [('verify:d0x0:0', REAL, prompt(), OPUS)], check={'head': HEAD, 'top': '/x/other/.worktrees/pr-review-4'})
        cases, printed = self.run_it()
        self.assertEqual([(c['repo'], c['pr'], c['source']['journal'].split('/projects/')[1][:2]) for c in cases], [('hathach/agentrc', 4, 'p1')])
        for reason in ('no transcript', 'no pinned head', 'prompt names another head', 'unparsable prompt', 'repository not in REPOS'):
            self.assertIn(f'1 verdict(s), {reason}', printed)

    def test_a_projects_tree_reached_twice_is_read_once(self):
        check = {'head': HEAD, 'top': '/x/agentrc/.worktrees/pr-review-4'}
        self.journal('p1', [('verify:d0x0:0', REAL, prompt(), OPUS)], check=check)
        self.journal('p2', [('verify:d0x0:0', REAL, None, None)], check=check)
        (self.root / 'alias').symlink_to(self.root / 'projects')
        out = self.root / 'cases.jsonl'
        with redirect_stdout(io.StringIO()) as printed:
            extract.main([str(self.root / 'projects'), str(self.root / 'alias'), '--out', str(out)])
        self.assertIn('1 verdict(s), no transcript', printed.getvalue())

    def test_the_prompt_patterns_follow_code_audit(self):
        src = (ROOT / 'workflows' / 'code-audit.js').read_text()
        self.assertIn('`Adversarially verify ONE review finding about ${p.dir}.\\nDimension: ${p.dim}\\nFinding: ${JSON.stringify(f)}\\n`', src)
        self.assertIn(' The checkout is at ${args.diff.head}. Judge only what \\`git diff ${args.diff.base} ${args.diff.head}', src)



def case(i, line, real=True, pr=1, file='src/x.c', why='w', models=(OPUS,)):
    """A case whose confirmed verdict, without its grading, is tagged ungraded."""
    return {'id': f'c{i}', 'repo': 'o/r', 'pr': pr, 'head': HEAD, 'dimension': 'correctness', 'path': file,
            'finding': {'file': file, 'line': line, 'why': why}, 'verifier': {'real': real, 'reason': 'r', 'models': list(models)},
            'source': {'journal': '/home/u/.claude/projects/p/s/j.jsonl', 'label': 'verify:d0x0:0'}}


def tagged(cases):
    return {c['id']: sample.tags(c) for c in cases}


class Sample(unittest.TestCase):
    def test_clusters_split_on_span_and_size(self):
        cs = [case(i, line) for i, line in enumerate((1, 5, 9, 13, 17, 60))] + [case(9, 2, file='src/y.c')]
        got = sorted(sorted(c['id'] for c in cl) for cl in sample.clusters(cs, tagged(cs), span=15, size=4))
        self.assertEqual(got, [['c0', 'c1', 'c2', 'c3'], ['c4'], ['c5'], ['c9']])

    def test_hardware_claims_cluster_apart_and_duplicates_count_their_own_lines(self):
        cs = [case(0, 1), case(1, 1), case(2, 2), case(3, 3, why='the errata says so')]
        got = sorted(sorted(c['id'] for c in cl) for cl in sample.clusters(cs, tagged(cs), span=15, size=4))
        self.assertEqual(got, [['c0', 'c1', 'c2'], ['c3']])
        cl = [case(0, 1), case(1, 1), case(2, 2), case(3, 3)]
        self.assertEqual(sample.counts(cl, tagged(cl))['duplicate'], 2)

    def test_the_grading_rule_follows_code_audit(self):
        src = (ROOT / 'workflows' / 'code-audit.js').read_text()
        for const, want in (('LEVELS', sample.LEVELS), ('CONFIDENCE', sample.CONFIDENCE)):
            got = re.search(rf"^const {const} = \[([^\]]*)\]", src, re.M).group(1)
            self.assertEqual(tuple(re.findall(r"'(\w+)'", got)), want, const)
        got = re.search(r"^const IMPACT = \{.*?required: \[([^\]]*)\]", src, re.M | re.S).group(1)
        self.assertEqual(tuple(re.findall(r"'(\w+)'", got)), sample.IMPACT)

    def test_a_partial_grade_is_ungraded(self):
        full = {'real': True, 'severity': 'low', 'confidence': 'high', 'severityReason': 's',
                'impact': {'consequence': 'c', 'path': 'p', 'variants': 'v', 'recovery': 'r'}, 'models': [OPUS]}
        self.assertTrue(sample.graded(full))
        self.assertIn('ungraded', sample.tags(case(0, 1)))
        self.assertNotIn('ungraded', sample.tags({**case(0, 1), 'verifier': full}))
        self.assertIn('ungraded', sample.tags({**case(0, 1), 'verifier': {**full, 'impact': None}}))

    def draw(self, cases, total):
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td) / 'cases.jsonl', Path(td) / 'sample.jsonl'
            src.write_text('\n'.join(map(json.dumps, cases)) + '\n')
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                rc = sample.main(['--cases', str(src), '--out', str(out), '--seed', '7', '--total', str(total)])
            return rc, err.getvalue(), [json.loads(l) for l in out.read_text().splitlines()]

    def test_every_pr_is_sampled_and_the_total_is_filled(self):
        cs = [case(i, (i % 4) // 2 * 5 + 100 * (i // 4), real=i % 2 == 0 or i < 4, pr=i // 20, why='errata' if i % 8 == 0 else 'w',
                   models=('claude-sonnet-5-5',) if i % 10 == 0 else (OPUS,)) for i in range(60)]
        rc, err, got = self.draw(cs, 20)
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(len(got), 20)
        self.assertEqual({c['pr'] for c in got}, {0, 1, 2})
        rc, err, _ = self.draw(cs, 200)
        self.assertEqual(rc, 1)
        self.assertIn('of 200 cases', err)

    def test_tags(self):
        self.assertEqual(sample.tags(case(0, 1, real=False, why='the errata says W1C')), {'refuted', 'hardware'})
        self.assertEqual(sample.tags(case(0, 1, models=('claude-sonnet-5-5',))), {'confirmed', 'sonnet', 'ungraded'})

    def test_a_short_quota_fails_and_a_seed_reproduces(self):
        cs = [case(i, 100 * i, real=i % 2 == 0, pr=i % 3) for i in range(40)]
        rc, err, first = self.draw(cs, 20)
        # singletons only and no hardware claim: those quotas cannot hold
        self.assertEqual(rc, 1)
        self.assertIn('hardware', err)
        self.assertEqual(self.draw(cs, 20)[2], first)



class Adjudicate(unittest.TestCase):
    SAMPLE = [{**case(i, 10 * i), 'base': BASE, 'dir': 'src', 'cluster': i // 2, 'finding': {'file': 'src/x.c', 'line': i, 'why': f'claim {i}', 'snippet': 's'}}
              for i in range(4)]

    def files(self, td, **answers):
        out = {}
        for name, rows in answers.items():
            path = Path(td) / f'{name}.json'
            path.write_text(json.dumps([{'id': i, 'label': l, 'evidence': 'src/x.c:1 x', 'needs': 'none'} for i, l in rows]))
            out[name] = f'{name}={path}'
        return out

    def test_evidence_must_be_text(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'n.json'
            path.write_text(json.dumps([{'id': 'c0', 'label': 'true', 'evidence': None, 'needs': 'none'}]))
            with self.assertRaises(SystemExit) as e:
                adjudicate.answers(f'n={path}', {'c0'})
            self.assertIn('no evidence', str(e.exception))

    CHECKOUTS = {'o/r': '/src/r'}

    def test_the_brief_carries_the_cited_documents(self):
        cited = [{**self.SAMPLE[0], 'finding': {**self.SAMPLE[0]['finding'], 'docs': [{'book': 12, 'pages': '3-4'}]}}]
        self.assertIn('"book": 12', adjudicate.brief(cited, {0}, self.CHECKOUTS))

    def test_the_brief_names_the_checkout_it_was_given(self):
        self.assertIn('repo /src/r ', adjudicate.brief(self.SAMPLE, {0}, self.CHECKOUTS))
        with self.assertRaises(SystemExit) as e:
            adjudicate.brief(self.SAMPLE, {0}, {})
        self.assertIn('no --checkout for o/r', str(e.exception))

    def test_the_brief_hides_the_verdict(self):
        text = adjudicate.brief(self.SAMPLE, {1}, self.CHECKOUTS)
        self.assertIn('c2', text)
        self.assertNotIn('c0', text)
        self.assertNotIn('verifier', text.split('Return only')[1])
        self.assertNotIn("'real'", text)

    def test_merge_settles_agreement_and_leaves_splits_to_the_lead(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.files(td, codex=[('c0', 'true'), ('c1', 'false'), ('c2', 'unclear'), ('c3', 'true')],
                           opus=[('c0', 'true'), ('c1', 'true'), ('c2', 'unclear'), ('c3', 'false')],
                           lead=[('c1', 'false'), ('c2', 'unclear')])
            got = {c['id']: c['truth'] for c in adjudicate.merge(self.SAMPLE, f['codex'], f['opus'], f['lead'])}
        self.assertEqual({k: (t['status'], t['label']) for k, t in got.items()},
                         {'c0': ('agreed', 'true'), 'c1': ('lead', 'false'), 'c2': ('disputed', None), 'c3': ('disputed', None)})
        self.assertEqual(got['c1']['answers']['codex']['label'], 'false')
        self.assertEqual(got['c2']['answers']['lead']['label'], 'unclear')

    def test_merge_drops_the_local_journal_path(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.files(td, a=[(c['id'], 'true') for c in self.SAMPLE], b=[(c['id'], 'true') for c in self.SAMPLE])
            got = adjudicate.merge(self.SAMPLE, f['a'], f['b'], None)
        self.assertEqual({json.dumps(c['source']) for c in got}, {'{"label": "verify:d0x0:0"}'})

    def test_merge_refuses_malformed_or_mismatched_answers(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.files(td, a=[('c0', 'true')], b=[('c1', 'true')], bad=[('c0', 'maybe')], twice=[('c0', 'true'), ('c0', 'false')],
                           ghost=[('zz', 'true')])
            for x, y, want in (('a', 'b', 'different cases'), ('a', 'bad', 'label not one of'), ('a', 'twice', 'answered twice'),
                               ('a', 'ghost', 'unknown id'), ('a', 'a', 'must be distinct')):
                with self.assertRaises(SystemExit) as e:
                    adjudicate.merge(self.SAMPLE, f[x], f[y], None)
                self.assertIn(want, str(e.exception))


if __name__ == '__main__':
    unittest.main()
