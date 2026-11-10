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
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from seal import fnv1a  # noqa: E402
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
        return done.returncode, done.stdout if emit and done.returncode != 3 else json.loads(done.stdout.splitlines()[-1])

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
        code, _ = self.run_script(self.file, emit=self.dir / 'absent' / 'state.1.js')
        self.assertEqual(code, 3, 'no directory is made for it')
        self.assertFalse((self.dir / 'absent').exists())

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

    def test_each_call_makes_a_new_directory_under_the_common_dir_and_drops_only_stale_ones(self):
        code, first = self.emit_dir(self.repo)
        self.assertEqual(code, 0)
        parent = (self.repo / '.git' / 'agentrc-emit').resolve()
        self.assertEqual(Path(first['dir']).parent.resolve(), parent)
        self.assertTrue(os.path.isabs(first['dir']))
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
        self.assertEqual(again.returncode, 3)
        self.assertEqual(sorted(parent.iterdir()), made, 'the second run made no directory: the operation never ran twice')

    def test_a_linked_worktree_uses_the_common_dir(self):
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True, env=git_env())
        linked = self.repo.parent / 'linked'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '-q', str(linked)], check=True, env=git_env())
        self.assertEqual(Path(self.emit_dir(linked)[1]['dir']).parent.resolve(), (self.repo / '.git' / 'agentrc-emit').resolve())



class StageTest(unittest.TestCase):
    """facts.py stage: a relay command's script input, written only when it is the JSON the workflow sent."""

    def stage(self, data, digest):
        out = Path(tempfile.mkdtemp()) / 'x.in'
        self.addCleanup(shutil.rmtree, out.parent)
        done = subprocess.run([sys.executable, str(FACTS), 'stage', str(out), digest], input=data, capture_output=True)
        return done.returncode, json.loads(done.stdout.decode().splitlines()[-1]), out

    def test_the_bytes_sent_are_written_and_anything_else_refused(self):
        text = json.dumps({'body': "it's \\ naïve ✓ 😀\r\n"}, ensure_ascii=False)
        sent = base64.b64encode(text.encode())
        code, line, out = self.stage(sent, fnv1a(text))
        self.assertEqual((code, line, out.read_bytes()), (0, {'staged': str(out)}, text.encode()))
        for data, digest, why in [
            (sent, fnv1a(text + ' '), 'not the JSON sent'),
            (sent + b'\n', fnv1a(text), 'not base64'),
            (base64.b64encode(b'\xff[]'), fnv1a(text), 'not base64 of UTF-8 JSON'),
            (base64.b64encode(b'{'), fnv1a('{'), 'not base64 of UTF-8 JSON'),
        ]:
            code, line, out = self.stage(data, digest)
            self.assertEqual(code, 2, why)
            self.assertIn(why, line['error'])
            self.assertFalse(out.exists(), why)

if __name__ == '__main__':
    unittest.main()
