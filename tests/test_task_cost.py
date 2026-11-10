"""Tests for headless-chief's run ledger (run_ledger.py) and its per-task roll-up (task_cost.py), #490."""
import io
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'headless-chief' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_ledger  # noqa: E402
import task_cost  # noqa: E402
from helpers import guard_survivors  # noqa: E402


def setUpModule():
    guard_survivors(__name__)


def run_id(n):
    return f'{n:032x}'


def start(n, task='k', out=None, at='2026-11-10T10:00:00Z'):
    return {'v': 1, 'type': 'start', 'run': run_id(n), 'task': task, 'out': out or f'/tmp/run{n}', 'worktree': '/wt', 'at': at}


def end(n, usd='1.00', status='known', exit=0, wall=60.0):
    cost = {'status': status, 'usd': None if status == 'unknown' else usd, 'reason': None if status == 'known' else 'why'}
    return {'v': 1, 'type': 'end', 'run': run_id(n), 'at': '2026-11-10T11:00:00Z', 'exit': exit, 'reason': 'result ok',
            'session': 's', 'wall': wall, 'cost': cost}


def append_many(ledger, n, tag):
    for i in range(n):
        run_ledger.append(start(tag * 1000 + i), Path(ledger))


class Ledger(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.ledger = Path(td.name) / 'state' / 'runs.jsonl'

    def test_records_round_trip_as_lines_split_on_newline_alone(self):
        rec = start(1, out='/tmp/a b')
        run_ledger.append(rec, self.ledger)
        run_ledger.append(end(1), self.ledger)
        lines = run_ledger.read(self.ledger)
        self.assertEqual([json.loads(x) for x in lines], [rec, end(1)], 'U+2028 inside a string is no line break')

    def test_a_missing_ledger_reads_empty_and_a_torn_line_stays_its_own(self):
        self.assertEqual(run_ledger.read(self.ledger), [])
        self.ledger.write_bytes(b'{"v": 1, "type": "sta')   # a writer killed mid-line
        run_ledger.append(start(2), self.ledger)
        lines = run_ledger.read(self.ledger)
        self.assertEqual(lines[0], b'{"v": 1, "type": "sta')
        self.assertEqual(json.loads(lines[1]), start(2))

    def test_concurrent_writers_leave_whole_lines(self):
        procs = [multiprocessing.Process(target=append_many, args=(str(self.ledger), 50, t)) for t in (1, 2, 3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        lines = run_ledger.read(self.ledger)
        self.assertEqual(len(lines), 150)
        self.assertEqual(len({json.loads(x)['run'] for x in lines}), 150)

    def test_a_held_lock_is_busy_for_writers_and_readers_after_its_wait(self):
        run_ledger.append(start(1), self.ledger)
        held, go = threading.Event(), threading.Event()

        def hold():
            with run_ledger.locked(self.ledger):
                held.set()
                go.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(go.set)
        held.wait(10)
        for call in (lambda: run_ledger.append(start(2), self.ledger), lambda: run_ledger.read(self.ledger)):
            began = time.monotonic()
            with mock.patch.object(run_ledger, 'LOCK_WAIT', 0.2), self.assertRaises(run_ledger.Busy):
                call()
            self.assertLess(time.monotonic() - began, 2, 'bounded, never a hang')
        go.set()
        t.join(10)
        self.assertEqual([json.loads(x) for x in run_ledger.read(self.ledger)], [start(1)], 'the refused write wrote nothing')

    def test_a_read_waits_for_a_write_in_progress(self):
        got = []
        with run_ledger.locked(self.ledger):
            t = threading.Thread(target=lambda: got.append(run_ledger.read(self.ledger)))
            t.start()
            time.sleep(0.2)
            self.ledger.write_bytes(json.dumps(start(3)).encode() + b'\n')
        t.join(10)
        self.assertEqual([json.loads(x) for x in got[0]], [start(3)], 'the reader saw the ledger after the writer')


class TaskCost(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.ledger = Path(td.name) / 'runs.jsonl'
        env = mock.patch.dict(os.environ, {'AGENTRC_CHIEF_RUNS': str(self.ledger)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, *records):
        self.ledger.write_bytes(b''.join((r if isinstance(r, bytes) else json.dumps(r).encode()) + b'\n' for r in records))

    def query(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = task_cost.main([*argv, '--json'])
        return code, json.loads(out.getvalue()) if code == 0 else err.getvalue()

    def text(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            task_cost.main(list(argv))
        return out.getvalue()

    def test_relaunches_of_a_task_sum_across_outs_and_worktrees_and_tasks_stay_apart(self):
        self.write(start(1), end(1, '1.25'), {**start(2), 'worktree': '/other'}, end(2, '2.50'), start(3, task='j'), end(3, '9.00'))
        code, r = self.query()
        self.assertEqual(code, 0)
        self.assertTrue(r['complete'])
        self.assertEqual([(t['task'], t['launches'], t['totalUsd'], t['knownUsd']) for t in r['tasks']],
                         [('k', 2, '3.75', '3.75'), ('j', 1, '9.00', '9.00')])
        self.assertIn('| k | 2 | 2.0 m | $3.75 |', self.text())

    def test_unknown_partial_and_unfinished_never_read_as_a_whole_sum(self):
        self.write(start(1), end(1, '1.00'), start(2), end(2, status='unknown'), start(3), end(3, '0.50', status='partial'), start(4))
        _, r = self.query()
        [t] = r['tasks']
        self.assertEqual((t['knownUsd'], t['totalUsd'], t['unknown'], t['partial'], t['unfinished']), ('1.50', None, 1, 1, 1))
        self.assertIn('$1.50 known; 1 unfinished; 1 partial; 1 unknown', self.text())
        self.write(start(5), end(5, status='unknown'))
        _, r = self.query()
        self.assertEqual((r['tasks'][0]['knownUsd'], r['tasks'][0]['totalUsd']), (None, None))
        self.assertIn('| unknown; 1 unknown |', self.text(), 'never $0.00')

    def test_an_explicit_zero_is_a_known_cost(self):
        self.write(start(1), end(1, '0.00'))
        self.assertEqual(self.query()[1]['tasks'][0]['totalUsd'], '0.00')

    def test_a_keyless_launch_is_its_own_task(self):
        self.write(start(1, task=None, out='/tmp/a'), end(1), start(2, task=None, out='/tmp/a'), end(2))
        _, r = self.query()
        self.assertEqual([(t['task'], t['unkeyed'], t['launches']) for t in r['tasks']],
                         [('unkeyed: /tmp/a', True, 1), ('unkeyed: /tmp/a', True, 1)], 'one out path reused is two launches')
        self.write(start(1, task='unkeyed: /tmp/a'), end(1))
        self.assertFalse(self.query()[1]['tasks'][0]['unkeyed'], 'a key spelled like one is still a key')

    def test_unattributable_lines_make_the_query_incomplete_even_filtered(self):
        for bad in (b'not json', b'\xff\xfe', json.dumps({**start(9), 'v': 2}).encode(), json.dumps(end(8)).encode()):
            with self.subTest(bad):
                self.write(start(1), end(1), bad)
                code, r = self.query('--task', 'k')
                self.assertEqual(code, 0)
                self.assertFalse(r['complete'])
                self.assertIsNone(r['tasks'][0]['totalUsd'])
                self.assertEqual(r['tasks'][0]['knownUsd'], '1.00')
                self.assertTrue(r['diagnostics'])
                self.assertIn('Incomplete', self.text('--task', 'k'))

    def test_duplicates_are_deduplicated_or_flagged_never_a_silent_winner(self):
        self.write(start(1), end(1), end(1))
        _, r = self.query()
        self.assertTrue(r['complete'])
        self.assertEqual(r['diagnostics'], [{'line': 3, 'run': run_id(1), 'problem': 'the same end again'}])
        self.write(start(1), end(1, '1.00'), end(1, '2.00'))
        _, r = self.query()
        self.assertFalse(r['complete'])
        self.assertEqual(r['tasks'], [], 'neither amount wins')

    def test_records_missing_what_the_roll_up_reads_are_diagnostics_never_a_crash(self):
        cost = end(2, status='unknown')['cost']
        for name, bad in (('start without task', {k: v for k, v in start(2).items() if k != 'task'}),
                          ('cost without usd', {**end(1), 'cost': {k: v for k, v in cost.items() if k != 'usd'}}),
                          ('cost without reason', {**end(1), 'cost': {k: v for k, v in cost.items() if k != 'reason'}}),
                          ('unknown with no reason', {**end(1), 'cost': {**cost, 'reason': None}}),
                          ('partial with no reason', {**end(1), 'cost': {'status': 'partial', 'usd': '1.00', 'reason': None}}),
                          ('end without session', {k: v for k, v in end(1).items() if k != 'session'}),
                          ('NaN wall', {**end(1), 'wall': float('nan')}),
                          ('infinite wall', {**end(1), 'wall': float('inf')}),
                          ('huge wall', {**end(1), 'wall': 10 ** 400})):
            with self.subTest(name):
                self.write(start(1), bad, start(3), end(3, '2.00'))
                code, r = self.query()
                self.assertEqual(code, 0)
                self.assertFalse(r['complete'])
                self.assertTrue(all(t['totalUsd'] is None for t in r['tasks']))
                self.assertIn('Incomplete', self.text('--task', 'k'))

    def test_a_disputed_run_counts_for_nothing_whatever_its_records_order(self):
        for name, records in (('ends 9 then 1', (start(1), end(1, '9.00'), end(1, '1.00'))),
                              ('ends 1 then 9', (start(1), end(1, '1.00'), end(1, '9.00'))),
                              ('starts of two tasks', (start(1), start(1, task='j'), end(1, '9.00'))),
                              ('a later copy restores nothing', (start(1), end(1, '9.00'), end(1, '1.00'), end(1, '9.00')))):
            with self.subTest(name):
                self.write(*records, start(2), end(2, '2.00'))
                _, r = self.query()
                self.assertFalse(r['complete'])
                self.assertEqual([(t['task'], t['launches'], t['knownUsd'], t['totalUsd']) for t in r['tasks']], [('k', 1, '2.00', None)])

    def test_a_missing_ledger_is_empty_and_an_unreadable_one_an_error(self):
        code, r = self.query()
        self.assertEqual((code, r['tasks'], r['complete']), (0, [], True))
        self.assertIn('No launch recorded.', self.text())
        self.ledger.mkdir()
        code, err = self.query()
        self.assertEqual(code, 2)
        self.assertIn('cannot be read', err)

    def test_a_task_filter_lists_each_launch_and_orders_by_start(self):
        self.write(start(2, at='2026-11-10T12:00:00Z'), end(2), start(1, at='2026-11-10T09:00:00Z'), start(3, task='j'), end(3))
        _, r = self.query('--task', 'k')
        self.assertEqual([x['run'] for x in r['tasks'][0]['runs']], [run_id(1), run_id(2)])
        self.assertEqual(len(r['tasks']), 1)
        out = self.text('--task', 'k')
        self.assertIn('| ↳ 2026-11-10T09:00:00Z exit None | /tmp/run1 | - | unfinished |', out)


if __name__ == '__main__':
    unittest.main()
