"""Tests for bench/finding-verify: extraction over synthetic journals and transcripts, sampling, adjudication."""
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'bench' / 'finding-verify' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract, sample, adjudicate, score = load('extract'), load('sample'), load('adjudicate'), load('score')

BASE, HEAD = 'a' * 40, 'b' * 40
FINDING = {'file': 'src/x.c', 'line': 3, 'snippet': 'x = 1;', 'why': 'x is never read', 'severity': 'low', 'confidence': 'high'}
REAL = {'real': True, 'reason': 'holds', 'severity': 'low', 'impact': None, 'severityReason': 's', 'confidence': 'high'}
OPUS = 'claude-opus-5-5'


def prompt(head=HEAD, finding=FINDING, directory='src', dimension='correctness: logic'):
    return (f'Adversarially verify ONE review finding about {directory}.\nDimension: {dimension}\nFinding: {json.dumps(finding)}\n'
            f"Read the cited code. The checkout is at {head}. Judge only what `git diff {BASE} {head} -- ':(literal,top){directory}'` introduces.")


def setUpModule():
    guard_survivors(__name__)


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
        self.assertNotIn(b'\r', out.read_bytes(), 'one LF-terminated line a case on every OS')
        return [json.loads(l) for l in out.read_text(encoding='utf-8').splitlines()], printed.getvalue()

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

    def test_a_windows_finding_path_names_its_worktree_as_spelled(self):
        top = 'E:\\mcu\\tinyusb\\.worktrees\\pr-review-12'
        inside = {**FINDING, 'file': 'e:\\mcu\\tinyusb\\.worktrees\\pr-review-12\\src\\class\\x.c'}
        self.journal('E--mcu-tinyusb--worktrees-pr-review-12', [
            ('verify:d0x0:0', REAL, prompt(finding=inside, directory='src/class'), OPUS),
            ('verify:d0x0:1', REAL, prompt(finding={**FINDING, 'file': 'D:/elsewhere/x.c'}, directory='src/class'), OPUS),
        ], check={'head': HEAD, 'top': top})
        cases, printed = self.run_it()
        self.assertEqual([(c['repo'], c['pr'], c['path'], c['worktree']) for c in cases],
                         [('hathach/tinyusb', 12, 'src/class/x.c', 'e:\\mcu\\tinyusb\\.worktrees\\pr-review-12')])
        self.assertIn('1 verdict(s), absolute path outside the session worktree', printed)

    def test_every_backslash_is_a_separator_and_a_unc_rooted_or_drive_relative_path_is_absolute(self):
        top = 'E:\\mcu\\tinyusb\\.worktrees\\pr-review-12'
        at = lambda file, why='w': prompt(finding={**FINDING, 'file': file, 'why': why}, directory='src/class')
        self.journal('E--mcu-tinyusb--worktrees-pr-review-12', [
            ('verify:d0x0:0', REAL, at('src\\class\\x.c'), OPUS),
            ('verify:d0x0:1', REAL, at('\\\\srv\\share\\x.c'), OPUS),
            ('verify:d0x0:2', REAL, at('\\x.c'), OPUS),
            ('verify:d0x0:3', REAL, at('C:x.c'), OPUS),
        ], check={'head': HEAD, 'top': top})
        unc = '\\\\srv\\share\\tinyusb\\.worktrees\\pr-review-13'
        self.journal('--srv-share-tinyusb--worktrees-pr-review-13', [
            ('verify:d0x0:0', REAL, at(unc + '\\src\\y.c', 'unc'), OPUS),
        ], check={'head': HEAD, 'top': unc})
        cases, printed = self.run_it()
        self.assertEqual(sorted((c['pr'], c['path'], c['worktree']) for c in cases), [(12, 'src/class/x.c', None), (13, 'src/y.c', unc)])
        self.assertIn('3 verdict(s), absolute path outside the session worktree', printed)

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

    def test_a_check_that_names_its_run_is_placed_by_it_not_by_path(self):
        top = '/home/u/code/tinyusb/.worktrees/pr-review-12'
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-12', [('verify:d0x0:0', REAL, prompt(), OPUS)],
                     check={'head': HEAD, 'top': top, 'repo': 'acme/widget', 'pr': 9})
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-13', [('verify:d0x0:0', REAL, prompt(dimension='other'), OPUS)],
                     check={'head': HEAD, 'top': top, 'repo': 'acme/widget', 'pr': '9'})
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-14', [('verify:d0x0:0', REAL, prompt(dimension='third'), OPUS)],
                     check={'head': HEAD, 'top': top, 'repo': 'acme/widget', 'pr': 0})
        absolute = {**FINDING, 'file': '/home/u/code/tinyusb/.worktrees/pr-review-15/src/x.c'}
        self.journal('-home-u-code-tinyusb--worktrees-pr-review-15', [('verify:d0x0:0', REAL, prompt(finding=absolute), OPUS)],
                     check={'head': HEAD, 'top': top, 'repo': 'acme/widget', 'pr': 15})
        cases, printed = self.run_it()
        self.assertEqual([(c['repo'], c['pr']) for c in cases], [('acme/widget', 9)])
        self.assertIn('2 verdict(s), malformed run identity', printed)
        self.assertIn('1 verdict(s), absolute path in a run that names itself', printed)

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
        src = (ROOT / 'workflows' / 'code-audit.js').read_text(encoding='utf-8')
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
        src = (ROOT / 'workflows' / 'code-audit.js').read_text(encoding='utf-8')
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
            self.assertNotIn(b'\r', out.read_bytes(), 'one LF-terminated line a case on every OS')
            return rc, err.getvalue(), [json.loads(l) for l in out.read_text(encoding='utf-8').splitlines()]

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


FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, re, sys, uuid
if '--version' in sys.argv:
    print('0.0.0 (fake)')
    sys.exit(0)
prompt = sys.stdin.buffer.read().decode('utf-8')
schema = json.loads(sys.argv[sys.argv.index('--json-schema') + 1])
# a file per run: concurrent appends to one file interleave on Windows
os.makedirs(os.environ['FAKE_LOG'], exist_ok=True)
with open(os.path.join(os.environ['FAKE_LOG'], f'{uuid.uuid4().hex}.json'), 'x') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': prompt}))
def verdict(why):
    if 'garble me' in why:
        return {'real': 'yes', 'reason': 'not the schema'}
    if 'refute me' in why:
        return {'real': False, 'reason': 'src/x.c:1 shows otherwise', 'severity': None, 'impact': None, 'severityReason': None, 'confidence': None}
    return {'real': True, 'reason': 'src/x.c:3 holds', 'severity': 'low', 'confidence': 'high', 'severityReason': 's',
            'impact': {'consequence': 'c', 'path': 'p', 'variants': 'v', 'recovery': 'r'}}
whys = [json.loads(f)['why'] for f in re.findall(r'^Finding: (.+)$', prompt, re.M)]
if any('dirty me' in w for w in whys):
    open('stray.txt', 'w').close()
if any('slow' in w for w in whys):
    import time
    time.sleep(30)
if 'verdicts' in schema['properties']:
    ids = re.findall(r'^\[(\S+)\] about ', prompt, re.M)
    out = {'verdicts': [{'id': i, **verdict(w)} for i, w in zip(ids, whys)]}
else:
    out = verdict(whys[0])
# as the CLI does under --agent: the reply is the result text, with no structured_output
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.3,
                  'modelUsage': {'claude-opus-5-5': {}}, 'result': json.dumps(out)}))
"""


class Score(unittest.TestCase):
    def test_disputed_truth_is_right_in_no_count(self):
        truth = {'t': ('true', True), 'f': ('false', True), 'd': (None, True), 's': ('true', False)}
        corpus = [{'id': i, 'truth': {'label': label}} for i, (label, _) in truth.items()]
        env = {'before': {'corpus': 'x', 'cases': sorted(truth)}, 'after': {'corpus': 'x', 'cases': sorted(truth)}}
        calls = [{'label': 'verify:d0x0:0', 'caseIds': sorted(truth), 'cost': 1.0, 'durationMs': 1000, 'model': 'opus', 'effort': 'high',
                  'models': [OPUS]}]
        for disputed in ('confirmed', 'refuted'):
            kinds = {'t': 'confirmed', 'f': 'confirmed', 'd': disputed, 's': 'unverified'}
            records = {i: {'id': i, 'kind': kinds[i], 'routing': {'hardware': hw}} for i, (_, hw) in truth.items()}
            f = score.report(corpus, (records, calls, env), (records, calls, env), corpus_digest='x')['figures']['single']
            self.assertEqual((f['settled'], f['right'], f['wrong']), (3, 1, ['f']), disputed)
            self.assertEqual(f['byHardware'], {'True': {'cases': 3, 'right': 1}, 'False': {'cases': 1, 'right': 0}, 'None': {'cases': 0, 'right': 0}},
                             disputed)


class Replay(unittest.TestCase):
    """replay.mjs drives code-audit.js itself against a stand-in claude; score.py reads what it wrote."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update({'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
                         'FAKE_LOG': str(self.root / 'fake-log'), 'HOME': str(self.root / 'home'),
                         'USERPROFILE': str(self.root / 'home')})
        (self.root / 'home').mkdir()
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'claude').write_text(FAKE_CLAUDE)
        (bin_dir / 'claude').chmod(0o755)
        # Launched by the interpreter, so it runs where a shebang does not; a `claude` looked up on PATH is
        # this one, or on Windows a blocker that cannot start, never the real CLI.
        self.env['REPLAY_CLAUDE'] = json.dumps([sys.executable, str(bin_dir / 'claude')])
        if os.name == 'nt':
            (bin_dir / 'claude.exe').write_bytes(b'')
        self.env['PATH'] = f"{bin_dir}{os.pathsep}{self.env['PATH']}"
        self.repo = self.root / 'clone'
        (self.repo / 'src').mkdir(parents=True)
        (self.repo / 'src' / 'x.c').write_text('int x;\n')
        self.sh('git', 'init', '-q')
        self.sh('git', 'add', '-A')
        self.sh('git', 'commit', '-qm', 'base')
        self.base = self.sh('git', 'rev-parse', 'HEAD').strip()
        (self.repo / 'src' / 'x.c').write_text('int x;\nint y;\n')
        self.sh('git', 'commit', '-qam', 'head')
        self.head = self.sh('git', 'rev-parse', 'HEAD').strip()

    def sh(self, *argv):
        return subprocess.run(argv, cwd=self.repo, env=self.env, check=True, capture_output=True, text=True).stdout

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def fake_runs(self):
        return [json.loads(f.read_text(encoding='utf-8')) for f in (self.root / 'fake-log').glob('*.json')]

    def corpus(self):
        def one(i, line, why, needs='none', label='true'):
            return {'id': f'c{i}', 'repo': 'o/r', 'pr': 1, 'head': self.head, 'base': self.base, 'dir': 'src',
                    'dimension': 'correctness: logic', 'path': 'src/x.c', 'worktree': None,
                    'finding': {'file': 'src/x.c', 'line': line, 'snippet': 'int y;', 'why': why, 'severity': 'low', 'confidence': 'high'},
                    'truth': {'label': label, 'status': 'agreed', 'answers': {'a': {'label': label, 'evidence': 'src/x.c:2', 'needs': needs},
                                                                            'b': {'label': label, 'evidence': 'src/x.c:2', 'needs': 'none'}}}}
        rows = [one(0, 1, 'y is unused'), one(1, 3, 'refute me', label='false'), one(2, 2, 'clears a W1C flag', needs='hardware')]
        path = self.root / 'corpus.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return path

    def replay(self, mode, corpus, *extra, out=None, ok=True):
        out = out or str(self.root / mode)
        done = subprocess.run(['node', str(ROOT / 'bench' / 'finding-verify' / 'replay.mjs'), '--corpus', str(corpus), '--mode', mode,
                               '--out', out, '--worktrees', str(self.root / 'wt'), '--checkout', f'o/r={self.repo}', *extra],
                              env=self.env, capture_output=True, encoding='utf-8', errors='replace', cwd=self.root)
        self.assertEqual(done.returncode == 0, ok, done.stderr + done.stdout)
        return Path(self.root, out), done.stderr

    def test_both_modes_run_code_audit_and_record_every_dispatch(self):
        corpus = self.corpus()
        single, batch = self.replay('single', corpus)[0], self.replay('batch', corpus)[0]
        calls = {m: [json.loads(l) for l in (self.root / m / 'calls.jsonl').read_text(encoding='utf-8').splitlines()] for m in ('single', 'batch')}
        self.assertEqual(sorted(c['label'] for c in calls['single']), ['verify:d0x0:0', 'verify:d0x0:1', 'verify:d0x0:2'])
        self.assertEqual(sorted((c['label'], tuple(c['caseIds'])) for c in calls['batch']),
                         [('verify:b0', ('c0', 'c1')), ('verify:d0x0:1', ('c2',))], 'the hardware case stays alone')
        self.assertTrue(all(c['prompt'] and c['schema'] for c in calls['batch']), 'each dispatch keeps what it was sent')
        logged = self.fake_runs()
        self.assertTrue(all(Path(x['cwd']).resolve() == (self.root / 'wt' / f'o-r-{self.head[:12]}').resolve() for x in logged))
        self.assertTrue(all(x['argv'][:7] == ['-p', '--agent', 'finding-verifier', '--model', 'opus', '--effort', 'high'] for x in logged))
        self.assertFalse(any('agreed' in x['prompt'] or '"label"' in x['prompt'] for x in logged), 'no truth reaches a verifier')
        records = {m: {v['id']: v for v in map(json.loads, (self.root / m / 'verdicts.jsonl').read_text(encoding='utf-8').splitlines())} for m in calls}
        self.assertEqual({i: r['kind'] for i, r in records['single'].items()}, {'c0': 'confirmed', 'c1': 'refuted', 'c2': 'confirmed'})
        self.assertEqual({i: r['kind'] for i, r in records['batch'].items()}, {i: r['kind'] for i, r in records['single'].items()})
        self.assertEqual(records['batch']['c0']['from'], 'verify:b0')
        r = score.report(score.load(corpus), score.run(single), score.run(batch), corpus_digest=self.digest(corpus))
        self.assertEqual((r['batched'], r['regressions'], r['lostCoverage']), (2, [], []))
        self.assertEqual({k: v for k, v in r['gate'].items()}, {'environment': True, 'accounting': True, 'correctness': True, 'cost': True, 'evidence': None})
        self.assertFalse(r['pass'], 'unscored evidence is no pass')
        text = score.brief(score.load(corpus), score.run(single), score.run(batch))
        self.assertNotIn('single', text.split('\n## ')[1])
        letter = lambda i: 'A' if score.letters(i)['A'] == 'batch' else 'B'
        review = lambda c0, c1='preserved': [{'id': 'c0', 'verdict': c0, 'why': 'x'}, {'id': 'c1', 'verdict': c1, 'why': 'y'}]
        self.assertTrue(score.report(score.load(corpus), score.run(single), score.run(batch), review('preserved'), self.digest(corpus))['pass'])
        for verdicts, gate in ((review(f'lost-{letter("c0")}'), False), (review('lost-both'), False), (review('inconclusive'), False),
                               (review(f'lost-{"B" if letter("c0") == "A" else "A"}'), True)):
            self.assertEqual(score.report(score.load(corpus), score.run(single), score.run(batch), verdicts, self.digest(corpus))['gate']['evidence'], gate, verdicts)
        for bad in (review('preserved')[:1], review('preserved') + review('preserved')[:1], [], review('maybe')):
            with self.assertRaises(SystemExit, msg=bad):
                score.report(score.load(corpus), score.run(single), score.run(batch), bad, self.digest(corpus))

    def test_a_coverage_trade_missing_cost_or_other_environment_fails_the_gate(self):
        corpus = self.corpus()
        single, batch = score.run(self.replay('single', corpus)[0]), score.run(self.replay('batch', corpus)[0])
        traded = (dict(batch[0]), batch[1], batch[2])
        traded[0]['c0'] = {**traded[0]['c0'], 'kind': 'unverified'}
        swapped = (dict(single[0]), single[1], single[2])
        swapped[0]['c2'] = {**swapped[0]['c2'], 'kind': 'unverified'}
        r = score.report(score.load(corpus), swapped, traded, corpus_digest=self.digest(corpus))
        self.assertEqual((r['lostCoverage'], r['gate']['accounting']), (['c0'], False), 'a case gained elsewhere does not pay for c0')
        uncosted = (batch[0], [{**c, 'cost': None} for c in batch[1]], batch[2])
        self.assertFalse(score.report(score.load(corpus), single, uncosted, corpus_digest=self.digest(corpus))['gate']['cost'])
        moved = (batch[0], batch[1], {'before': {**batch[2]['before'], 'workflow': 'other'}, 'after': {**batch[2]['after'], 'workflow': 'other'}})
        r = score.report(score.load(corpus), single, moved, corpus_digest=self.digest(corpus))
        self.assertEqual((r['gate']['environment'], r['environment']), (False, "the runs differ in ['workflow']"))
        drifted = (batch[0], batch[1], {'before': batch[2]['before'], 'after': {**batch[2]['after'], 'role': 'other'}})
        self.assertFalse(score.report(score.load(corpus), single, drifted, corpus_digest=self.digest(corpus))['gate']['environment'])
        resonnet = (batch[0], [{**c, 'model': 'sonnet', 'effort': 'medium', 'models': ['claude-sonnet-5-5']} for c in batch[1]], batch[2])
        r = score.report(score.load(corpus), single, resonnet, corpus_digest=self.digest(corpus))
        self.assertEqual((r['gate']['environment'], r['environment']), (False, "cases asked of other models or efforts: ['c0', 'c1', 'c2']"))
        answered = (batch[0], [{**c, 'models': ['claude-opus-4-1']} for c in batch[1]], batch[2])
        self.assertEqual(score.report(score.load(corpus), single, answered, corpus_digest=self.digest(corpus))['environment'],
                         "cases answered by other models: ['c0', 'c1', 'c2']")
        # two revisions swapped between cases cancel out overall, never per case
        rev = lambda calls, first: [{**c, 'models': ['rev-1' if ('c2' in c['caseIds']) == first else 'rev-2']} for c in calls]
        r = score.report(score.load(corpus), (single[0], rev(single[1], True), single[2]), (batch[0], rev(batch[1], False), batch[2]),
                         corpus_digest=self.digest(corpus))
        self.assertEqual(r['environment'], "cases answered by other models: ['c0', 'c1', 'c2']")
        unnamed = (batch[0], [{**c, 'models': []} for c in batch[1]], batch[2])
        self.assertEqual(score.report(score.load(corpus), single, unnamed, corpus_digest=self.digest(corpus))['environment'],
                         'a dispatch names no model that answered it')
        relabelled = [json.loads(l) for l in corpus.read_text(encoding='utf-8').splitlines()]
        relabelled[1]['truth']['label'] = 'true'
        r = score.report(relabelled, single, batch, corpus_digest=hashlib.sha256(json.dumps(relabelled).encode()).hexdigest())
        self.assertEqual((r['gate']['environment'], r['environment']), (False, 'the corpus scored is not the one replayed'))

    def test_heads_replay_concurrently_each_in_its_own_worktree_and_units_by_their_label(self):
        rows = [json.loads(l) for l in self.corpus().read_text(encoding='utf-8').splitlines()]
        (self.repo / 'src' / 'x.c').write_text('int x;\nint y;\nint z;\n')
        self.sh('git', 'commit', '-qam', 'second head')
        head2 = self.sh('git', 'rev-parse', 'HEAD').strip()
        later = [{**rows[0], 'id': 'd0', 'head': head2, 'base': self.head},
                 {**rows[1], 'id': 'd1', 'head': head2, 'base': self.head, 'dimension': 'concurrency'}]
        corpus = self.root / 'two-heads.jsonl'
        corpus.write_text(''.join(json.dumps(r) + '\n' for r in rows + later))
        out, _ = self.replay('single', corpus, '--jobs', '2')
        records = {v['id']: v for v in map(json.loads, (out / 'verdicts.jsonl').read_text(encoding='utf-8').splitlines())}
        self.assertEqual({i: r['kind'] for i, r in records.items()},
                         {'c0': 'confirmed', 'c1': 'refuted', 'c2': 'confirmed', 'd0': 'confirmed', 'd1': 'refuted'})
        calls = [json.loads(l) for l in (out / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(sorted((c['head'][:12], c['label'], tuple(c['caseIds'])) for c in calls if c['head'] == head2),
                         [(head2[:12], 'verify:d0x0:0', ('d0',)), (head2[:12], 'verify:d0x1:0', ('d1',))], 'scan:d0x1 is the second dimension')
        logged = self.fake_runs()
        self.assertEqual({Path(x['cwd']).name for x in logged}, {f'o-r-{self.head[:12]}', f'o-r-{head2[:12]}'})

    def test_a_failing_head_stops_the_other_heads_dispatches_and_keeps_their_calls(self):
        rows = [json.loads(l) for l in self.corpus().read_text(encoding='utf-8').splitlines()]
        (self.repo / 'src' / 'x.c').write_text('int x;\nint y;\nint z;\n')
        self.sh('git', 'commit', '-qam', 'second head')
        head2 = self.sh('git', 'rev-parse', 'HEAD').strip()
        slow = {**rows[0], 'id': 's0', 'finding': {**rows[0]['finding'], 'why': 'slow'}}
        dirty = {**rows[0], 'id': 'd0', 'head': head2, 'base': self.head, 'finding': {**rows[0]['finding'], 'why': 'dirty me'}}
        corpus = self.root / 'failing.jsonl'
        corpus.write_text(json.dumps(slow) + '\n' + json.dumps(dirty) + '\n')
        started = time.monotonic()
        out, err = self.replay('single', corpus, ok=False)
        self.assertLess(time.monotonic() - started, 20, 'the slow verifier was terminated, not awaited')
        self.assertIn(f'changed while {head2} was replayed', err)
        calls = {c['label'] + '@' + c['head'][:12]: c for c in map(json.loads, (out / 'calls.jsonl').read_text(encoding='utf-8').splitlines())}
        killed = calls[f'verify:d0x0:0@{self.head[:12]}']
        self.assertEqual((killed['caseIds'], killed['structured']), (['s0'], None), 'the terminated call is still recorded')
        self.assertNotIn(f'verify:d0x0:0.retry@{self.head[:12]}', calls, 'no dispatch starts after the failure')
        self.assertFalse((out / 'verdicts.jsonl').exists())

    def test_a_reply_off_its_schema_counts_as_none(self):
        rows = [json.loads(l) for l in self.corpus().read_text(encoding='utf-8').splitlines()]
        corpus = self.root / 'garbled.jsonl'
        corpus.write_text(json.dumps({**rows[0], 'finding': {**rows[0]['finding'], 'why': 'garble me'}}) + '\n')
        out, _ = self.replay('single', corpus)
        [record] = map(json.loads, (out / 'verdicts.jsonl').read_text(encoding='utf-8').splitlines())
        self.assertEqual((record['kind'], record['attempts']), ('unverified', ['verify:d0x0:0', 'verify:d0x0:0.retry']))
        calls = [json.loads(l) for l in (out / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(all(c['structured'] is None and c['error'].startswith('success: {') for c in calls))

    def test_the_skills_digest_names_a_file_with_slashes(self):
        skill = self.root / 'home' / '.claude' / 'skills' / 'x'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_bytes(b'hi\n')
        out, _ = self.replay('single', self.corpus())
        env = json.loads((out / 'env.json').read_text(encoding='utf-8'))
        line = '\0'.join(('x/SKILL.md', '3', hashlib.sha256(b'hi\n').hexdigest())) + '\n'
        self.assertEqual(env['before']['skills'], hashlib.sha256(line.encode()).hexdigest())

    def test_nothing_is_spent_on_a_run_that_cannot_be_scored(self):
        corpus = self.corpus()
        _, err = self.replay('single', corpus, '--jobs', '0', ok=False)
        self.assertIn('--jobs must be a positive integer', err)
        (self.root / 'taken').mkdir()
        _, err = self.replay('single', corpus, out='taken', ok=False)
        self.assertIn('exists', err)
        rows = [json.loads(l) for l in corpus.read_text(encoding='utf-8').splitlines()]
        twin = self.root / 'twin.jsonl'
        twin.write_text(''.join(json.dumps(r) + '\n' for r in rows + [{**rows[0], 'id': 'c9'}]))
        _, err = self.replay('single', twin, ok=False)
        self.assertIn('cases c0 and c9 share a scan unit, file, line and claim', err)
        gone = self.root / 'gone.jsonl'
        gone.write_text(''.join(json.dumps({**r, 'base': 'f' * 40}) + '\n' for r in rows))
        _, err = self.replay('single', gone, ok=False)
        self.assertIn(f"o/r {'f' * 40}: not in", err)
        stand_in = self.env['REPLAY_CLAUDE']
        for bad in ('', 'claude', '[]', '[""]'):
            self.env['REPLAY_CLAUDE'] = bad
            _, err = self.replay('single', corpus, ok=False)
            self.assertIn('REPLAY_CLAUDE must be a JSON array of nonempty strings', err, repr(bad))
        self.env['REPLAY_CLAUDE'] = stand_in
        self.assertEqual(self.fake_runs(), [], 'no verifier was started')
        self.replay('single', Path('corpus.jsonl'), '--worktrees', 'wt2', out='relative')
        self.assertTrue((self.root / 'relative' / 'verdicts.jsonl').exists(), 'relative paths resolve against the caller')
        self.assertTrue((self.root / 'wt2' / f'o-r-{self.head[:12]}' / 'src' / 'x.c').exists(), 'not inside the checkout')
        self.assertFalse((self.repo / 'wt2').exists())
