"""Tests for pr-babysit's state_transfer.py, and the AGENTRC_EMIT file every facts.py report writes (lib/seal.py's emit)."""
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from seal import EMIT_REFUSED, EMIT_UNCLAIMABLE, fnv1a  # noqa: E402
SCRIPT = ROOT / 'skills' / 'pr-babysit' / 'scripts' / 'state_transfer.py'
FACTS = ROOT / 'skills' / 'pr-babysit' / 'scripts' / 'facts.py'
EMIT_STUB = ROOT / 'tests' / 'emit_stub.mjs'
spec = importlib.util.spec_from_file_location('pr_babysit_state_transfer', SCRIPT)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)

# The reason a live loader "corrected" (146 -> 144) on both attempts, plus text a
# serializer could mangle: non-ASCII, quotes, backslashes, newlines.
REASON = 'The command is at SKILL.md:146 at head (144 at eda9a913) and reads `curl -sSO "$url"`'
STATE = {
    'version': 3, 'cyclesUsed': 3, 'maxCycles': 5,
    'decisions': [[f'5302{n}#1', {'commentId': 5302000 + n, 'verdict': 'invalid', 'gone': None, 'empty': '',
                                  'reason': f'{REASON} — case {n}: \\ "é" ✓ \nnext line'}] for n in range(12)],
    'answeredWith': [[4092032489, {'how': 'refutation', 'digest': 'd1'}]],
    'digest': 'cd1574aa',
}
# The runtime's load of a data module: its meta, then the body's return value (workflow({scriptPath})).
LOAD = ("const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor\n"
        "const body = require('fs').readFileSync(process.argv[1], 'utf8').replace(/^export const meta = /m, 'const meta = ')\n"
        "new AsyncFunction(body)().then(v => process.stdout.write(JSON.stringify(v)))\n")


def load(path):
    """The value the workflow gets from workflow({scriptPath: path})."""
    done = subprocess.run(['node', '-e', LOAD, str(path)], capture_output=True, text=True, encoding='utf-8')
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def setUpModule():
    guard_survivors(__name__)


class StateTransferTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.file = self.saved({'result': {'state': STATE, 'pass': False}})

    def saved(self, content, name='w.output'):
        path = self.dir / name
        path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2, ensure_ascii=False), encoding='utf-8')
        return str(path)

    def run_script(self, *argv, emit=None):
        """(exit, the JSON line stdout ends with), or under emit (exit, stdout whole)."""
        env = {k: v for k, v in os.environ.items() if k != 'AGENTRC_EMIT'}
        if emit:
            env['AGENTRC_EMIT'] = str(emit)
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, encoding='utf-8', env=env)
        return done.returncode, done.stdout if emit and done.returncode not in (EMIT_REFUSED, EMIT_UNCLAIMABLE) else json.loads(done.stdout.splitlines()[-1])

    def test_the_line_is_the_state_whole(self):
        code, out = self.run_script(self.file)
        self.assertEqual((code, out), (0, {'digest': 'cd1574aa', 'state': STATE}))

    def test_a_pinned_checkout_dir_is_read_against_this_worktree_and_sealed_apart_from_the_state(self):
        repo = self.dir / 'repo'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=git_env())
        (repo / 'sub').mkdir()
        other = self.dir / 'other'
        subprocess.run(['git', 'init', '-q', str(other)], check=True, env=git_env())
        for pinned, cwd, same in ((str(repo), repo / 'sub', True), (str(other), repo, False), (str(self.dir / 'gone'), repo, False),
                                  (str(repo), self.dir, False)):
            state = {**STATE, 'config': {'checkoutDir': pinned}}
            done = subprocess.run([sys.executable, str(SCRIPT), self.saved({'result': {'state': state}}, 'pinned.output')], cwd=cwd,
                                  capture_output=True, text=True, encoding='utf-8', env=git_env())
            line = json.loads(done.stdout.splitlines()[-1])
            self.assertEqual((line['state'], line['checkout']), (state, transfer.sealed({'dir': pinned, 'same': same})), (pinned, cwd))
        for config in ({'checkoutDir': '.'}, {}, None):
            _, line = self.run_script(self.saved({'result': {'state': {**STATE, 'config': config}}}, 'here.output'))
            self.assertNotIn('checkout', line, config)

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_the_emitted_module_loads_as_the_line_with_its_own_path_and_stdout_only_names_it(self):
        at = self.dir / 'state.1.js'
        _, line = self.run_script(self.file)
        code, out = self.run_script(self.file, emit=at)
        self.assertEqual((code, out), (0, f'the line is in {at}\n'), 'the line stays out of a relay\'s context')
        self.assertEqual(load(at), {'emit': str(at), 'line': line}, 'nulls, empty strings and U+2028 arrive as written')

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_the_emitted_file_is_the_harness_stubs_module_byte_for_byte(self):
        state = {**STATE, 'odd': '\u2028 \x7f \U0001F600 \ud800 \t', 'none': None, '': ''}
        at = self.dir / 'state.1.js'
        self.assertEqual(self.run_script(self.saved(json.dumps({'result': {'state': state}}), 'odd.output'), emit=at)[0], 0)
        stub = ("const { moduleOf } = await import(process.argv[1])\n"
                "const { readFileSync } = await import('node:fs')\n"
                "process.stdout.write(moduleOf(process.argv[2], JSON.parse(readFileSync(0, 'utf8'))))\n")
        done = subprocess.run(['node', '--input-type=module', '-e', stub, EMIT_STUB.as_uri(), str(at)], capture_output=True,
                              input=json.dumps(load(at)['line']), text=True, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, at.read_text(encoding='utf-8'), "the harnesses' moduleOf builds what seal.emit writes")

    def test_a_bad_file_or_argument_is_an_error_line(self):
        (self.dir / 'latin1.json').write_bytes(b'"\xe9"')
        cases = [
            ((str(self.dir / 'missing'),), 'cannot read'),
            ((str(self.dir / 'latin1.json'),), 'is not UTF-8'),
            ((self.saved('{not json', 'bad.output'),), 'is not JSON'),
            ((self.saved({'result': {'pass': True}}, 'none.output'),), 'holds no result.state object'),
            ((self.saved({'result': {'state': {'version': 3}}}, 'nodigest.output'),), 'has no digest'),
            ((self.saved({'result': {'state': {'digest': 'x', 'pad': 'a' * transfer.MAX}}}, 'big.output'),), f'(max {transfer.MAX})'),
            ((), 'usage'),
            ((self.file, '--chunks', '0'), 'usage'),
            ((self.file, '--raw'), 'usage'),
        ]
        for argv, message in cases:
            code, out = self.run_script(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, out['error'], argv)

    def at_size(self, size, unit):
        """A state whose compact ASCII JSON is exactly size bytes, padded with unit (its escaped form one size)."""
        escaped = len(json.dumps(unit)) - 2
        state = {'digest': 'x', 'checkoutDir': '/repo', 'pad': ''}
        room = size - len(json.dumps(state, ensure_ascii=True, separators=(',', ':')))
        state['pad'] = unit * (room // escaped) + 'a' * (room % escaped)
        self.assertEqual(len(json.dumps(state, ensure_ascii=True, separators=(',', ':'))), size)
        return state

    def test_a_state_loads_up_to_256_kib_measured_escaped_and_its_module_fits_a_workflow_script(self):
        # #539: a stateRef state stopped at 64 KiB; the runtime refuses a Workflow script file over 524288 bytes
        # (Claude Code 2.1.322), and the emitted module is the state plus its wrapper.
        self.assertEqual(transfer.MAX, 256 * 1024)
        for what, unit in (('ASCII', 'a'), ('escape-heavy', '\u2192\U0001F600"\\\n\x7f')):
            with self.subTest(what):
                at = self.dir / f'{what}.js'
                state = self.at_size(transfer.MAX, unit)
                file = self.saved(json.dumps({'result': {'state': state}}, ensure_ascii=False), f'{what}.output')
                self.assertEqual(self.run_script(file, emit=at)[0], 0)
                self.assertEqual(load(at)['line']['state'], state)
                self.assertLess(at.stat().st_size, 524288)
                self.assertLess(at.stat().st_size, transfer.MAX + 1024, 'the wrapper adds a few hundred bytes')
                over = self.saved(json.dumps({'result': {'state': self.at_size(transfer.MAX + 1, unit)}}, ensure_ascii=False), f'{what}-over.output')
                code, out = self.run_script(over)
                self.assertEqual(code, 2)
                self.assertEqual(out['error'], f'state too large: {transfer.MAX + 1} bytes (max {transfer.MAX})')

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_an_error_line_is_emitted_too(self):
        at = self.dir / 'state.1.js'
        self.assertEqual(self.run_script(str(self.dir / 'missing'), emit=at)[0], 2)
        self.assertEqual(load(at)['line'], self.run_script(str(self.dir / 'missing'))[1])

    def test_an_existing_emit_file_is_never_overwritten_and_nothing_runs(self):
        at = self.dir / 'state.1.js'
        at.write_text('return {"emit": "stale"}\n')
        code, out = self.run_script(self.file, emit=at)
        self.assertEqual(code, 3)
        self.assertEqual(out, {'error': f'AGENTRC_EMIT {at}: File exists: nothing was run'})
        self.assertEqual(at.read_text(), 'return {"emit": "stale"}\n')
        empty = self.dir / 'empty.1.js'
        empty.write_bytes(b'')
        self.assertEqual(self.run_script(self.file, emit=empty)[0], EMIT_REFUSED, 'an earlier claim left empty is still a claim')
        self.assertEqual(empty.read_bytes(), b'')

    def test_an_emit_file_that_cannot_be_made_is_no_claim_and_nothing_runs(self):
        # #547: a missing directory, or a file where it should be, holds no earlier attempt's claim (EMIT_REFUSED).
        absent = self.dir / 'absent' / 'state.1.js'
        code, out = self.run_script(self.file, emit=absent)
        self.assertEqual((code, out), (EMIT_UNCLAIMABLE, {'error': f'AGENTRC_EMIT {absent}: No such file or directory: nothing was run'}))
        self.assertFalse((self.dir / 'absent').exists(), 'no directory is made for it')
        (self.dir / 'plain').write_text('')
        code, out = self.run_script(self.file, emit=self.dir / 'plain' / 'state.1.js')
        self.assertEqual(code, EMIT_UNCLAIMABLE, 'its parent is a file')
        self.assertTrue(out['error'].endswith(': nothing was run'), out)

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0), 'needs POSIX permissions a non-root user obeys')
    def test_a_refusal_that_cannot_tell_whether_a_claim_is_there_is_refused_as_one(self):
        # EACCES says nothing of an earlier claim: a directory it may not write, or one that hides a claim (mode 000).
        locked = self.dir / 'locked'
        locked.mkdir(mode=0o500)
        self.addCleanup(locked.chmod, 0o700)
        code, out = self.run_script(self.file, emit=locked / 'state.1.js')
        self.assertEqual((code, out), (EMIT_REFUSED, {'error': f'AGENTRC_EMIT {locked / "state.1.js"}: Permission denied: nothing was run'}))
        self.assertEqual(list(locked.iterdir()), [])
        hidden = self.dir / 'hidden'
        hidden.mkdir()
        (hidden / 'state.1.js').write_bytes(b'')
        hidden.chmod(0o000)
        self.addCleanup(hidden.chmod, 0o700)
        code, out = self.run_script(self.file, emit=hidden / 'state.1.js')
        self.assertEqual((code, out), (EMIT_REFUSED, {'error': f'AGENTRC_EMIT {hidden / "state.1.js"}: Permission denied: nothing was run'}))
        hidden.chmod(0o700)
        self.assertEqual((hidden / 'state.1.js').read_bytes(), b'', 'the earlier claim untouched')

    def test_a_run_that_dies_before_its_line_leaves_its_claim_empty(self):
        at = self.dir / 'state.1.js'
        env = {**os.environ, 'AGENTRC_EMIT': str(at)}
        done = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import facts; raise SystemExit(9)',
                               str(FACTS.parent)], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 9)
        self.assertEqual(at.read_bytes(), b'', 'claimed at import, before anything ran')

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_the_digest_matches_the_workflows_fnv1a(self):
        samples = ['', 'eyJhIjoxfQ==', 'A' * 684, '+/=0123456789abcdefXYZ', 'é ✓ 😀']
        for name in ('pr-babysit.js', 'pr-review.js'):
            source = (ROOT / 'workflows' / name).read_text(encoding='utf-8')
            fn = re.search(r'^function fnv1a \(text\) \{.*?^\}', source, re.S | re.M).group(0)
            out = subprocess.run(['node', '-e', f'{fn}\nconsole.log(JSON.stringify({json.dumps(samples)}.map(fnv1a)))'],
                                 capture_output=True, text=True, check=True).stdout
            self.assertEqual(json.loads(out), [fnv1a(s) for s in samples], name)

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_the_workflow_measures_a_state_as_this_script_does(self):
        source = (ROOT / 'workflows' / 'pr-babysit.js').read_text(encoding='utf-8')
        fns = '\n'.join(re.search(rf'^const {name} = .*$', source, re.M).group(0) for name in ('STATE_MAX', 'stateBytes'))
        samples = [STATE, {'s': '\x7f\x00 😀'}]
        out = subprocess.run(['node', '-e', f'{fns}\nconsole.log(JSON.stringify([STATE_MAX, {json.dumps(samples)}.map(stateBytes)]))'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [transfer.MAX, [len(json.dumps(s, ensure_ascii=True, separators=(',', ':'))) for s in samples]])


class EmitDirTest(unittest.TestCase):
    """facts.py emit-dir: a launch's AGENTRC_EMIT directory, fresh under the git common dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, env=git_env())

    def emit_dir(self, cwd):
        done = subprocess.run([sys.executable, str(FACTS), 'emit-dir'], cwd=cwd, capture_output=True, text=True, env=git_env())
        return done.returncode, json.loads(done.stdout.splitlines()[-1])

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_each_directory_holds_a_sentinel_naming_it_and_the_nonce_its_run_printed(self):
        code, first = self.emit_dir(self.repo)
        self.assertEqual((code, sorted(first)), (0, ['dir', 'nonce']))
        self.assertRegex(first['nonce'], r'\A[0-9a-f]{16}\Z')
        sentinel = f"{first['dir']}/emit-dir.js"
        self.assertEqual(load(sentinel), {'emit': sentinel, 'line': first}, 'an emitted line of its own path: what the run printed')
        second = self.emit_dir(self.repo)[1]
        self.assertNotEqual(second['nonce'], first['nonce'], 'a nonce per run: another run\'s directory is told apart')
        self.assertEqual(load(f"{second['dir']}/emit-dir.js"), {'emit': f"{second['dir']}/emit-dir.js", 'line': second})
        self.assertTrue((Path(second['dir']) / 'emit-dir.js').read_bytes().isascii())

    def test_each_call_makes_a_new_directory_under_the_common_dir_and_drops_only_stale_ones(self):
        code, first = self.emit_dir(self.repo)
        self.assertEqual(code, 0)
        parent = (self.repo / '.git' / 'agentrc-emit').resolve()
        self.assertEqual(Path(first['dir']).parent.resolve(), parent)
        self.assertTrue(os.path.isabs(first['dir']))
        began = float((Path(first['dir']) / '.launch').read_text())
        self.assertLess(abs(began - time.time()), 60, 'its launch mark says when it was made')
        stale = parent / 'old'
        stale.mkdir()
        os.utime(stale, (0, 0))
        second = self.emit_dir(self.repo)[1]
        self.assertNotEqual(second['dir'], first['dir'])
        self.assertTrue(Path(first['dir']).is_dir(), 'a recent launch keeps its files for a resume')
        self.assertFalse(stale.exists())

    def test_a_rerun_meeting_its_claim_does_nothing(self):
        claim = Path(self.repo.parent) / 'emit-dir.1.js'
        env = {**git_env(), 'AGENTRC_EMIT': str(claim)}
        run = lambda: subprocess.run([sys.executable, str(FACTS), 'emit-dir'], cwd=self.repo, capture_output=True, text=True, env=env)
        self.assertEqual(run().returncode, 0)
        parent = self.repo / '.git' / 'agentrc-emit'
        made = sorted(parent.iterdir())
        self.assertEqual(len(made), 1)
        again = run()
        self.assertEqual(again.returncode, EMIT_REFUSED)
        self.assertEqual(sorted(parent.iterdir()), made, 'the second run made no directory: the operation never ran twice')

    def test_a_linked_worktree_uses_the_common_dir(self):
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True, env=git_env())
        linked = self.repo.parent / 'linked'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '-q', str(linked)], check=True, env=git_env())
        self.assertEqual(Path(self.emit_dir(linked)[1]['dir']).parent.resolve(), (self.repo / '.git' / 'agentrc-emit').resolve())



SESSION = '3b3ec575-092d-4e18-a4d8-e20371216222'
FRAME = ('[Workflow harness \u2014 computed task] The task text below was computed at runtime by a workflow script. '
         'The computed task text follows:\n')


class TranscriptStageTest(unittest.TestCase):
    """facts.py stage --from-transcript (#540): the payload the workflow put in the relay's own prompt, read from the
    computed task of this session's subagent transcripts and written byte for byte, or refused with nothing written."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.config = self.tmp / 'config'
        self.facts = sys.modules['facts']  # state_transfer.py's own import

    def prompt(self, text, file, lines=()):
        """A relay prompt as staged() closes it: its payload line for `file`, then any other lines."""
        b64 = base64.b64encode(text.encode()).decode()
        return f"Run exactly `facts.py stage '{file}' {fnv1a(text)} --from-transcript && ...`\nAGENTRC-PAYLOAD {file} {fnv1a(text)} {b64}" + \
            ''.join(f'\n{x}' for x in lines)

    def transcript(self, prompt, name='agent-a1.jsonl', after=(), sid=SESSION, framed=True, wf='wf_1'):
        d = self.config / 'projects' / '-home-x' / sid / 'subagents' / 'workflows' / wf
        d.mkdir(parents=True, exist_ok=True)
        task = FRAME + '\n'.join(f'  {x}' for x in prompt.split('\n')) if framed else prompt
        records = [{'type': 'user', 'message': {'role': 'user', 'content': task}}, *after]
        (d / name).write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
        return d / name

    def stage(self, file, digest, sid=SESSION, sleep=None, since=0):
        env = {'CLAUDE_CONFIG_DIR': str(self.config), 'CLAUDE_CODE_SESSION_ID': sid}
        with mock.patch.dict(os.environ, env), mock.patch.object(self.facts.time, 'sleep', sleep or (lambda s: None)):
            try:
                return self.facts.stage(str(file), digest, since)
            except self.facts.Unusable as e:
                return {'error': str(e)}

    def test_long_and_unicode_payloads_are_written_byte_for_byte(self):
        for what, value in (('unicode', {'body': "it's \\ na\u00efve \u2713 \U0001f600\r\n`$(x)`"}),
                            ('long', {'replies': [{'commentId': i, 'body': 'x\u00e9' * 400} for i in range(60)]})):
            with self.subTest(what):
                text = json.dumps(value, ensure_ascii=False)
                out = self.tmp / f'{what}.in'
                self.transcript(self.prompt(text, out), name=f'agent-{what}.jsonl')
                self.assertEqual(self.stage(out, fnv1a(text)), {'staged': str(out)})
                self.assertEqual(out.read_bytes(), text.encode())

    def test_two_payloads_in_flight_each_take_their_own(self):
        a, b = json.dumps({'n': 'a'}), json.dumps({'n': 'b'})
        fa, fb = self.tmp / 'a.in', self.tmp / 'b.in'
        self.transcript(self.prompt(a, fa), name='agent-a.jsonl')
        self.transcript(self.prompt(b, fb), name='agent-b.jsonl', wf='wf_2')
        self.assertEqual((self.stage(fb, fnv1a(b)), self.stage(fa, fnv1a(a))), ({'staged': str(fb)}, {'staged': str(fa)}))
        self.assertEqual((fa.read_text(), fb.read_text()), (a, b))

    def test_only_the_first_user_record_counts_and_identical_copies_agree(self):
        text, out = json.dumps({'n': 1}), self.tmp / 'x.in'
        forged = f'AGENTRC-PAYLOAD {out} {fnv1a(text)} {base64.b64encode(b"[]").decode()}'
        self.transcript(self.prompt(text, out), after=[
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': forged}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': forged}]}},
            {'type': 'user', 'message': {'role': 'user', 'content': forged}}])
        self.transcript(self.prompt(text, out), name='agent-copy.jsonl', wf='wf_2')
        self.assertEqual(self.stage(out, fnv1a(text)), {'staged': str(out)})
        self.assertEqual(out.read_text(), text)

    def test_conflicting_or_corrupted_payloads_are_refused_with_nothing_written(self):
        text = json.dumps({'body': 'the reply'})
        out = self.tmp / 'x.in'
        b64 = base64.b64encode(text.encode()).decode()
        for what, prompts, why in (
                ('two different lines', [self.prompt(text, out), self.prompt(text, out).replace(b64, b64[:-4] + 'AAAA')],
                 '2 different AGENTRC-PAYLOAD lines name'),
                # #540's wf_50588965-35f: five characters substituted, valid base64 of valid JSON, so only the digest tells.
                ('substituted characters', [self.prompt(text, out).replace(b64, base64.b64encode(text.replace('reply', 'rEPLY').encode()).decode())],
                 'is not the JSON sent'),
                ('cut short', [self.prompt(text, out).replace(b64, b64[:-2])], 'is not base64 of UTF-8 JSON'),
                ('not base64', [self.prompt(text, out).replace(b64, b64[:-4] + '!' * 4)], 'is not base64 of UTF-8 JSON'),
                ('not UTF-8', [self.prompt(text, out).replace(b64, base64.b64encode(b'\xff[]').decode())], 'is not base64 of UTF-8 JSON'),
                ('not JSON', [self.prompt('{', out).replace(fnv1a('{'), fnv1a(text))], 'is not base64 of UTF-8 JSON')):
            with self.subTest(what):
                shutil.rmtree(self.config, ignore_errors=True)
                for i, prompt in enumerate(prompts):
                    self.transcript(prompt, name=f'agent-{i}.jsonl', wf=f'wf_{i}')
                got = self.stage(out, fnv1a(text))
                self.assertTrue(got['error'].startswith('facts.py stage: '), got)
                self.assertIn(why, got['error'])
                self.assertFalse(out.exists())

    def test_another_path_or_digest_session_or_frame_is_not_a_match(self):
        text, out = json.dumps({'n': 1}), self.tmp / 'x.in'
        self.transcript(self.prompt(text, self.tmp / 'other.in'))
        self.transcript(self.prompt(text, out), name='agent-other.jsonl', sid='00000000-0000-4000-8000-000000000000')
        got = self.stage(out, fnv1a(text))
        self.assertIn("no AGENTRC-PAYLOAD line for", got['error'])
        self.assertIn('read twice', got['error'])
        self.assertIn(fnv1a(json.dumps({'n': 2})), self.stage(out, fnv1a(json.dumps({'n': 2})))['error'])
        self.transcript(self.prompt(text, out), name='agent-plain.jsonl', framed=False, wf='wf_plain')
        self.assertEqual(self.stage(out, fnv1a(text)), {'staged': str(out)}, 'a subagent prompt with no harness frame reads as given')
        for sid in ('', 'not-a-session', SESSION.upper(), f'{SESSION}/..'):
            with self.subTest(sid=sid):
                self.assertIn('CLAUDE_CODE_SESSION_ID', self.stage(out, fnv1a(text), sid=sid)['error'])

    def test_a_transcript_not_yet_written_is_read_once_more(self):
        text, out = json.dumps({'n': 1}), self.tmp / 'x.in'
        waits = []

        def sleep(s):
            waits.append(s)
            self.transcript(self.prompt(text, out))
        self.assertEqual(self.stage(out, fnv1a(text), sleep=sleep), {'staged': str(out)})
        self.assertEqual(waits, [self.facts.SCAN_RETRY_S])

    def test_a_scan_bound_is_its_own_diagnostic(self):
        text, out = json.dumps({'n': 1}), self.tmp / 'x.in'
        for i in range(3):
            self.transcript(self.prompt(json.dumps({'other': i}), self.tmp / f'o{i}.in'), name=f'agent-{i}.jsonl', wf=f'wf_{i}')
        self.transcript(self.prompt(text, out), name='agent-new.jsonl', wf='wf_new')
        with mock.patch.object(self.facts, 'SCAN_FILES', 3):
            got = self.stage(out, fnv1a(text))
        self.assertIn('the transcript scan stopped at its bound (4 transcripts written since the launch began, over 3)', got['error'])
        with mock.patch.object(self.facts, 'SCAN_BYTES', 100):
            got = self.stage(out, fnv1a(text))
        self.assertIn('the transcript scan stopped at its bound (100 bytes read', got['error'])
        self.assertFalse(out.exists())
        self.assertEqual(self.stage(out, fnv1a(text)), {'staged': str(out)}, 'within its bounds it is found')

    def test_a_launch_with_no_mark_reads_the_newest_and_takes_a_match_there(self):
        text, out = json.dumps({'n': 1}), self.tmp / 'x.in'
        old = self.transcript(self.prompt(text, out), name='agent-old.jsonl', wf='wf_old')
        os.utime(old, (1e9, 1e9))
        for i in range(4):
            other = self.transcript(self.prompt(json.dumps({'other': i}), self.tmp / f'o{i}.in'), name=f'agent-{i}.jsonl', wf=f'wf_{i}')
            os.utime(other, (2e9 + i, 2e9 + i))
        with mock.patch.object(self.facts, 'SCAN_FILES', 3):
            got = self.stage(out, fnv1a(text), since=None)
            self.assertIn('the transcript scan stopped at its bound (the 3 newest of 5 transcripts read, the launch having no .launch)',
                          got['error'], 'a match past the bound is never reached')
            self.assertFalse(out.exists())
            os.utime(self.transcript(self.prompt(text, out), name='agent-new.jsonl', wf='wf_new'), (3e9, 3e9))
            self.assertEqual(self.stage(out, fnv1a(text), since=None), {'staged': str(out)}, 'the original relay is among the newest')
            self.assertEqual(out.read_text(), text)
            b64 = base64.b64encode(text.encode()).decode()
            conflict = self.transcript(self.prompt(text, out).replace(b64, b64[:-4] + 'AAAA'), name='agent-conflict.jsonl', wf='wf_conflict')
            os.utime(conflict, (3e9 + 1, 3e9 + 1))
            self.assertIn('2 different AGENTRC-PAYLOAD lines name', self.stage(out, fnv1a(text), since=None)['error'],
                          'conflicting copies among those read refuse')

    def test_only_transcripts_written_since_the_launch_began_count_toward_the_bound(self):
        text, out = json.dumps({'n': 1}), self.tmp / 'x.in'
        began = time.time() - 60
        for i in range(5):  # an earlier launch's relays, one naming this file with other JSON
            other = self.transcript(self.prompt(json.dumps({'other': i}), out if i == 0 else self.tmp / f'o{i}.in'), name=f'agent-{i}.jsonl', wf=f'wf_{i}')
            os.utime(other, (began - 3600, began - 3600))
        self.transcript(self.prompt(text, out), name='agent-new.jsonl', wf='wf_new')
        with mock.patch.object(self.facts, 'SCAN_FILES', 1):
            self.assertEqual(self.stage(out, fnv1a(text), since=began), {'staged': str(out)}, 'the older ones are not read')
        self.assertEqual(out.read_text(), text)

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_its_line_is_rewritten_on_every_run_so_a_resumed_command_goes_on(self):
        text, out = json.dumps({'n': 1}), self.tmp / 'x.in'
        line = self.tmp / 'x.stage.js'
        (self.tmp / '.launch').write_text(f'{time.time() - 60}\n')
        env = {**{k: v for k, v in os.environ.items() if k != 'AGENTRC_EMIT'}, 'CLAUDE_CONFIG_DIR': str(self.config), 'CLAUDE_CODE_SESSION_ID': SESSION}

        claim = self.tmp / 'x.js'

        def run():
            done = subprocess.run([sys.executable, str(FACTS), 'stage', str(out), fnv1a(text), '--from-transcript', '--line', str(line),
                                   '--claimed', str(claim)], capture_output=True, text=True, env=env)
            return done.returncode, load(line)['line']
        b64 = base64.b64encode(text.encode()).decode()
        bad = self.transcript(self.prompt(text, out).replace(b64, base64.b64encode(b'{"n": 2}').decode()))
        code, got = run()
        self.assertEqual(code, 2)
        self.assertEqual(got, {'error': f'facts.py stage: the payload for {out} is not the JSON sent', 'claimed': False})
        claim.write_text('')  # the script's emit file, claimed by an earlier run and left empty
        self.assertEqual(run(), (2, {**got, 'claimed': True}), "a refusal says the script's file was claimed: its outcome is unknown")
        claim.unlink()
        bad.unlink()
        self.transcript(self.prompt(text, out), name='agent-b.jsonl', wf='wf_2')
        self.assertEqual(run(), (0, {'staged': str(out), 'claimed': False}), 'the earlier line is no claim: the stage runs again')
        self.assertEqual(run(), (0, {'staged': str(out), 'claimed': False}))
        self.assertEqual(out.read_text(), text)

    def test_the_cli_takes_every_flag_and_names_the_stage_in_its_usage(self):
        line = self.tmp / 'x.stage.js'
        for argv, why in ((['x', 'y', '--from-transcript', '--line', str(line)], 'the following arguments are required: --claimed'),
                          (['x', 'y', '--line', str(line), '--claimed', 'c'], 'the following arguments are required: --from-transcript'),
                          (['x', 'y', '--from-transcript', '--line', str(line), '--claimed', 'c', '--bogus'], 'unrecognized arguments: --bogus'),
                          (['x', 'y', '--from-transcript', '--line', str(line), '--claimed', 'c'], 'does not tell when the launch began')):
            with self.subTest(why):
                (self.tmp / '.launch').write_text('not a time')
                done = subprocess.run([sys.executable, str(FACTS), 'stage', *argv], capture_output=True, text=True,
                                      env={k: v for k, v in os.environ.items() if k != 'AGENTRC_EMIT'})
                self.assertEqual(done.returncode, 2)
                self.assertIn(why, json.loads(done.stdout.splitlines()[-1])['error'])
        done = subprocess.run([sys.executable, str(FACTS), 'bogus'], capture_output=True, text=True)
        self.assertIn('stage FILE DIGEST --from-transcript --line PATH --claimed PATH', json.loads(done.stdout.splitlines()[-1])['error'])


if __name__ == '__main__':
    unittest.main()
