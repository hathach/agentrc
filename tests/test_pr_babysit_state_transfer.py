"""Tests for pr-babysit's state_transfer.py."""
import base64
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

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


def whole(run, file, *flags):
    """The envelope of a bare call, with every other chunk fetched PER_CALL at a time."""
    env = run(file, *flags)[1]
    for k in range(transfer.PER_CALL, count(env), transfer.PER_CALL):
        ids = ','.join(str(i) for i in range(k, min(k + transfer.PER_CALL, count(env))))
        env['chunks'] += run(file, *flags, '--chunks', ids)[1]['chunks']
    return env


def decode(envelope):
    return b''.join(base64.b64decode(c['data']) for c in sorted(envelope['chunks'], key=lambda c: c['i']))


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

    def test_pad_shifts_every_chunk_by_one_byte_so_its_text_changes(self):
        # #147: relays re-copied the same 512-byte encoding of text ending "the ." wrong every time.
        _, plain = self.run_script(self.file, '--chunks', '1,2')
        code, padded = self.run_script(self.file, '--chunks', '1,2', '--pad')
        self.assertEqual(code, 0)
        self.assertEqual((plain['pad'], padded['pad']), (False, True))
        for p, q in zip(plain['chunks'], padded['chunks']):
            raw = base64.b64decode(q['data'], validate=True)
            self.assertEqual(raw[:1], transfer.PAD)
            self.assertEqual(raw[1:], base64.b64decode(p['data']))
            self.assertEqual(q['sum'], transfer.fnv1a(q['data']))

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

    def raw_file(self, payload, name='ledger.json'):
        path = self.dir / name
        path.write_bytes(payload)
        return str(path)

    def test_raw_chunks_rebuild_the_file_bytes_beyond_the_state_limit(self):
        answer = {'open': [{'id': f'pr7-f{n}', 'why': f'{REASON} \\u00e9 {n}' + 'x' * 900} for n in range(80)]}
        payload = json.dumps(answer, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        self.assertGreater(len(payload), transfer.MAX, 'the ledger answer outgrows the state limit')
        file = self.raw_file(payload)
        self.assertEqual(self.run_script(file, '--raw')[0], 0)
        env = whole(self.run_script, file, '--raw')
        self.assertEqual((env['digest'], env['length']), (transfer.fnv1a(payload.decode('ascii')), len(payload)))
        self.assertEqual(decode(env), payload)

    def test_raw_takes_ascii_bytes_up_to_its_limit_and_refuses_more_or_other_bytes(self):
        at = b'{"p":"' + b'a' * (transfer.MAX_RAW - 8) + b'"}'
        self.assertEqual(len(at), transfer.MAX_RAW)
        self.assertEqual(self.run_script(self.raw_file(at), '--raw', '--chunks', '0')[0], 0)
        self.assertEqual(self.run_script(self.raw_file(b'[1, NaN'), '--raw')[0], 0, 'parsing is the reader\'s')
        for payload, message in [(at + b' ', f'(max {transfer.MAX_RAW})'), ('{"a":"é"}'.encode(), 'is not ASCII')]:
            code, out = self.run_script(self.raw_file(payload, 'bad.json'), '--raw')
            self.assertEqual(code, 2, payload[:20])
            self.assertIn(message, out['error'])

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_the_sum_matches_the_workflows_fnv1a(self):
        source = (ROOT / 'workflows' / 'pr-babysit.js').read_text(encoding='utf-8')
        fn = re.search(r'^function fnv1a \(text\) \{.*?^\}', source, re.S | re.M).group(0)
        samples = ['', 'eyJhIjoxfQ==', 'A' * 684, '+/=0123456789abcdefXYZ']
        out = subprocess.run(['node', '-e', f'{fn}\nconsole.log(JSON.stringify({json.dumps(samples)}.map(fnv1a)))'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [transfer.fnv1a(s) for s in samples])


    def test_pr_reviews_loader_helpers_are_pr_babysits_and_its_limit_this_scripts(self):
        babysit, review = ((ROOT / 'workflows' / f).read_text(encoding='utf-8') for f in ('pr-babysit.js', 'pr-review.js'))
        for name in ('fromBase64', 'fnv1a'):
            fn = re.compile(rf'^function {name} \(.*?^\}}', re.S | re.M)
            self.assertEqual(fn.search(review).group(0), fn.search(babysit).group(0), name)
        limit = re.search(r'^const LEDGER_MAX = ([0-9 *]+) //', review, re.M).group(1)
        self.assertEqual(eval(limit), transfer.MAX_RAW)  # noqa: S307  digits and * only

    @unittest.skipUnless(shutil.which('node'), 'needs Node.js')
    def test_the_workflow_measures_a_state_as_this_script_does(self):
        source = (ROOT / 'workflows' / 'pr-babysit.js').read_text(encoding='utf-8')
        fns = '\n'.join(re.search(rf'^const {name} = .*$', source, re.M).group(0) for name in ('STATE_MAX', 'stateBytes'))
        samples = [STATE, {'s': '\x7f\x00\u2028😀'}]
        out = subprocess.run(['node', '-e', f'{fns}\nconsole.log(JSON.stringify([STATE_MAX, {json.dumps(samples)}.map(stateBytes)]))'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [transfer.MAX, [len(json.dumps(s, ensure_ascii=True, separators=(',', ':'))) for s in samples]])

if __name__ == '__main__':
    unittest.main()
