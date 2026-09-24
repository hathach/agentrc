"""Tests for pr-babysit's state_transfer.py."""
import base64
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'pr-babysit' / 'scripts' / 'state_transfer.py'
spec = importlib.util.spec_from_file_location('pr_babysit_state_transfer', SCRIPT)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)

# The reason a live loader "corrected" (146 -> 144) on both attempts, plus text a
# serializer could mangle: non-ASCII, quotes, backslashes, newlines.
REASON = 'The command is at SKILL.md:146 at head (144 at eda9a913) and reads `curl -sSO "$url"`'
STATE = {
    'version': 3, 'cyclesUsed': 3, 'maxCycles': 5,
    'decisions': [[f'5302{n}#1', {'commentId': 5302000 + n, 'verdict': 'invalid',
                                  'reason': f'{REASON} — case {n}: \\ "é" ✓\nnext line'}] for n in range(12)],
    'answeredWith': [[4092032489, {'how': 'refutation', 'digest': 'd1'}]],
    'digest': 'cd1574aa',
}


def count(envelope):
    return -(-envelope['length'] // transfer.SIZE)


def decode(envelope):
    return b''.join(base64.b64decode(c['data']) for c in sorted(envelope['chunks'], key=lambda c: c['i']))


class StateTransferTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.file = self.saved({'result': {'state': STATE, 'pass': False}})

    def saved(self, content, name='w.output'):
        path = self.dir / name
        path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
        return str(path)

    def run_script(self, *argv):
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout.splitlines()[-1])

    def test_the_chunks_rebuild_the_state_exactly(self):
        code, env = self.run_script(self.file)
        self.assertEqual(code, 0)
        self.assertEqual((env['v'], env['digest'], env['size']), (1, 'cd1574aa', transfer.SIZE))
        total = count(env)
        self.assertGreater(total, transfer.PER_CALL)
        self.assertEqual([c['i'] for c in env['chunks']], list(range(transfer.PER_CALL)), 'a bare call gives the first batch')
        rest = ','.join(str(i) for i in range(transfer.PER_CALL, total))
        env['chunks'] += self.run_script(self.file, '--chunks', rest)[1]['chunks']
        payload = decode(env)
        self.assertEqual(len(payload), env['length'])
        self.assertTrue(payload.isascii(), 'non-ASCII is escaped, so a byte is a character')
        self.assertEqual(json.loads(payload), STATE)
        self.assertIn(b'SKILL.md:146 at head', payload)
        for c in env['chunks']:
            size = transfer.SIZE if c['i'] < total - 1 else env['length'] - transfer.SIZE * (total - 1)
            self.assertEqual(len(base64.b64decode(c['data'], validate=True)), size, 'each chunk decodes alone')
            self.assertEqual(c['sum'], transfer.fnv1a(c['data']))

    def test_chunks_names_a_subset_with_the_same_metadata(self):
        _, full = self.run_script(self.file)
        code, part = self.run_script(self.file, '--chunks', '2,0')
        self.assertEqual(code, 0)
        self.assertEqual([c['i'] for c in part['chunks']], [2, 0])
        self.assertEqual({k: part[k] for k in ('v', 'digest', 'length', 'size')},
                         {k: full[k] for k in ('v', 'digest', 'length', 'size')})
        self.assertEqual(part['chunks'][0], full['chunks'][2])

    def test_a_bad_file_or_argument_is_an_error_line(self):
        total = count(self.run_script(self.file)[1])
        cases = [
            ((str(self.dir / 'missing'),), 'cannot read'),
            ((self.saved('{not json', 'bad.output'),), 'is not JSON'),
            ((self.saved({'result': {'pass': True}}, 'none.output'),), 'holds no result.state object'),
            ((self.saved({'result': {'state': {'version': 3}}}, 'nodigest.output'),), 'has no digest'),
            ((self.file, '--chunks', 'a'), 'comma-separated indexes'),
            ((self.file, '--chunks', f'{total}'), 'outside 0..'),
            ((self.file, '--chunks', '1,1'), 'a duplicate'),
            ((self.file, '--chunks', '0,1,2,3,4'), f'more than {transfer.PER_CALL}'),
            ((), 'usage'),
            ((self.file, '--all'), 'usage'),
        ]
        for argv, message in cases:
            code, out = self.run_script(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, out['error'], argv)

    def test_a_state_over_the_limit_is_refused(self):
        big = self.saved({'result': {'state': {'digest': 'x', 'pad': 'a' * transfer.MAX}}}, 'big.output')
        code, out = self.run_script(big)
        self.assertEqual(code, 2)
        self.assertIn(f'(max {transfer.MAX})', out['error'])

    def test_the_sum_matches_the_workflows_fnv1a(self):
        source = (ROOT / 'workflows' / 'pr-babysit.js').read_text()
        fn = re.search(r'^function fnv1a \(text\) \{.*?^\}', source, re.S | re.M).group(0)
        samples = ['', 'eyJhIjoxfQ==', 'A' * 684, '+/=0123456789abcdefXYZ']
        out = subprocess.run(['node', '-e', f'{fn}\nconsole.log(JSON.stringify({json.dumps(samples)}.map(fnv1a)))'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [transfer.fnv1a(s) for s in samples])


if __name__ == '__main__':
    unittest.main()
