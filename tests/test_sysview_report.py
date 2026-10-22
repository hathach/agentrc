"""Tests for sysview_report.py: call/return pairing across loss, the null rules for an export without events or
without a time base, the explicit workload anchors, the event map, and real exports (tests/data/sysview: an
stm32h743nucleo FreeRTOS slice, a trimmed re-decode of it whose task blocks another task, and an stm32f407disco
bare-metal slice with an overflow). No SystemView."""
import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts'
DATA = Path(__file__).resolve().parent / 'data' / 'sysview'
spec = importlib.util.spec_from_file_location('sysview_report', SCRIPTS / 'sysview_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

EV_HEADER = ('sequencenum,timestamp,context,event,detail,timestampint,'
             'contextinint,contextint,contextoutint,eventint,eventoffset,eventsize,eventdata\n')
CTX_HEADER = ('Name,Type,Core,Stack Information,Activations,Total Blocked Time,Total Run Time,Time Interrupted,'
              'CPU Load,Last Run Time,Min Run Time,Max Run Time\n')
CTX_ROW = 'ISR 83,#83,Core 0,,10,,0.000 100 000 s,0.000 000 ms,0.33 %,0.003 ms,0.003 571 ms,0.027 774 ms\n'
SCHED_ID = '0x0000000400000000'     # the Scheduler's context id in SystemView 4.10b exports
INIT = 'Cycle Freq.: 1000000, CPU Freq.: 48000000, ID Base: 0x20000000, ID Shift: 0'


def setUpModule():
    guard_survivors(__name__)


def ev(ts, event, detail='', context='ctx', eventint=0, ctx_out='0x0'):
    """ctx_out: the contextoutint SystemView gives a row whose event switches the running context away."""
    return f'0,0.0,"{context}","{event}","{detail}",{ts},0x0,0x0,{ctx_out},{eventint},0,0,\n'


class Case(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)

    def write(self, rows, init=True, contexts=CTX_ROW, events=True):
        (self.dir / 'contexts.csv').write_text(CTX_HEADER + contexts)
        if events:
            (self.dir / 'events.txt').write_text(EV_HEADER + (ev(0, 'Init', INIT) if init else '') + ''.join(rows))

    def run_report(self, *flags, rows=(), **kw):
        self.write(rows, **kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(report.main([str(self.dir), '--json', *flags]), 0)
        return json.loads(out.getvalue())

    def usage_error(self, *flags, rows=(), **kw):
        self.write(rows, **kw)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            report.main([str(self.dir), '--json', *flags])
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()


def calls(n, fid=512, start=1000, dur=10, step=1000, annotated=True, context='ctx'):
    rows = []
    for i in range(n):
        t = start + i * step
        rows.append(ev(t, f'Function #{fid}', context=context, eventint=fid))
        rows.append(ev(t + dur, f'Function #{fid}', f'Returns after {dur}.000 us' if annotated else 'Returns',
                       context=context, eventint=28))
    return rows


class Pairing(Case):
    def test_clean_pairs_kept(self):
        r = self.run_report(rows=calls(60))
        self.assertEqual(r['functions'], [{'id': 512, 'name': '#512', 'n': 60, 'p50_us': 10.0, 'p99_us': 10.0,
                                           'max_us': 10.0}])
        self.assertEqual(r['dropped_pairs'], 0)

    def test_return_after_overflow_dropped(self):
        rows = [ev(1000, 'Function #512'), ev(1500, '*** Overflow ***'),
                ev(135000, 'Function #512', 'Returns after 134000.000 us')]
        r = self.run_report(rows=rows)
        self.assertEqual(r['functions'], [])
        self.assertEqual(r['dropped_pairs'], 2)
        self.assertEqual(r['overflow'], 1)

    def test_nested_calls_pair_innermost_first(self):
        rows = [ev(1000, 'Function #512'), ev(1010, 'Function #512'),
                ev(1015, 'Function #512', 'Returns after 5.000 us'),
                ev(1030, 'Function #512', 'Returns after 30.000 us')]
        r = self.run_report(rows=rows)
        self.assertEqual((r['functions'][0]['n'], r['functions'][0]['p50_us'], r['functions'][0]['max_us']),
                         (2, 5.0, 30.0))
        self.assertEqual(r['dropped_pairs'], 0)

    def test_interleaved_contexts_pair_within_their_context(self):
        rows = [ev(1000, 'Function #512', context='A'), ev(1002, 'Function #512', context='B'),
                ev(1010, 'Function #512', 'Returns', context='A'), ev(1032, 'Function #512', 'Returns', context='B')]
        r = self.run_report(rows=rows)
        self.assertEqual((r['functions'][0]['p50_us'], r['functions'][0]['max_us']), (10.0, 30.0))

    def test_bare_returns_timed_from_timestamps(self):
        r = self.run_report(rows=calls(5, dur=7, annotated=False))
        self.assertEqual((r['functions'][0]['n'], r['functions'][0]['p50_us']), (5, 7.0))
        self.assertEqual(r['dropped_pairs'], 0)

    def test_call_left_open_at_the_end_counted(self):
        r = self.run_report(rows=calls(3) + [ev(9000, 'Function #512')])
        self.assertEqual(r['dropped_pairs'], 1)

    def test_lost_return_context_also_drops_open_calls(self):
        rows = [ev(100, 'Function #512'), ev(200, 'ISR Exit', 'Returns to *** OVERFLOW ***'),
                ev(10000, 'Function #512', 'Returns')]
        r = self.run_report(rows=rows)
        self.assertEqual(r['functions'], [])
        self.assertEqual(r['dropped_pairs'], 2)

    def test_unmeasurable_bare_return_is_dropped(self):
        rows = [ev(100, 'Function #512'), ev(110, 'Function #512', 'Returns')]
        self.write(rows)
        text = (self.dir / 'events.txt').read_text().replace(',100,', ',x,')
        (self.dir / 'events.txt').write_text(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report.main([str(self.dir), '--json'])
        r = json.loads(out.getvalue())
        self.assertIsNone(r['functions'])     # an unreadable timestamp withholds the time base
        self.assertEqual(r['dropped_pairs'], 1)

    def test_both_loss_forms_counted(self):
        rows = [ev(1000, '*** Overflow ***'), ev(2000, 'ISR Exit', 'Returns to *** OVERFLOW ***'),
                ev(3000, 'ISR Exit', 'Returns to *** OVERFLOW ***')]
        self.assertEqual(self.run_report(rows=rows)['overflow'], 3)

    def test_event_map_names_and_unknown_ids(self):
        (self.dir / 'map.json').write_text('{"512": "tud_task"}')
        r = self.run_report('--event-map', str(self.dir / 'map.json'), rows=calls(3) + calls(2, fid=600, start=10000))
        self.assertEqual([(f['id'], f['name']) for f in r['functions']], [(512, 'tud_task'), (600, '#600')])

    def test_bad_event_maps_refused(self):
        for text in ('[1]', '{"x": "a"}', '{"512": ""}', '{"512": 3}', 'not json'):
            with self.subTest(text=text):
                (self.dir / 'map.json').write_text(text)
                self.assertIn('--event-map', self.usage_error('--event-map', str(self.dir / 'map.json')))


class Percentiles(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertEqual(report.pct([10, 100], 50), 10)
        self.assertEqual(report.pct(list(range(1, 101)), 50), 50)
        self.assertEqual(report.pct(list(range(1, 101)), 99), 99)
        self.assertEqual(report.pct([10, 100], 99), 100)
        self.assertEqual(report.pct([10, 20, 30], 1), 10)


class Unavailable(Case):
    def test_no_events_nulls_every_event_metric(self):
        r = self.run_report(events=False)
        for key in ('isr', 'ready_run', 'functions', 'markers', 'stack', 'heap', 'overflow', 'dropped_pairs',
                    'live_window_s', 'workload_window_s', 'contexts_workload'):
            self.assertIsNone(r[key], key)
        self.assertIsNone(r['contexts'][0]['cpu_pct'])    # no Init: no time base
        self.assertEqual(r['contexts'][0]['activations'], 10)

    def test_no_init_withholds_time_derived_metrics(self):
        r = self.run_report(rows=calls(5), init=False)
        self.assertIsNone(r['live_window_s'])
        self.assertIsNone(r['functions'])
        self.assertIsNone(r['contexts'][0]['cpu_pct'])
        self.assertIsNone(r['contexts'][0]['total_ms'])
        self.assertEqual(r['overflow'], 0)      # a count needs no time base
        self.assertTrue(any('Init' in w for w in r['warnings']))

    def test_backwards_timestamps_withhold_as_no_time_base(self):
        r = self.run_report(rows=[ev(5000, 'ISR Enter', 'Runs for 3.000 us'), ev(1000, 'ISR Exit')])
        self.assertIsNone(r['live_window_s'])
        self.assertIsNone(r['isr'])
        self.assertTrue(any('backwards' in w for w in r['warnings']))

    UNTIMED = [ev(0, 'ISR Enter', 'Runs for 900.000 us', 'ISR 83'), ev(0, 'ISR Exit', context='ISR 83'),
               ev(0, 'Task Run', 'Runs for 50.000 us', 'cdc'), *calls(3, start=0, step=0, dur=0),
               ev(0, 'System Idle', 'Idle for 500.000 us', 'Idle')]

    def test_untimed_head_of_a_post_mortem_ring_is_left_out(self):
        tail = [ev(1000, 'ISR Enter', 'Runs for 3.000 us', 'ISR 83'), ev(1003, 'ISR Exit', context='ISR 83'),
                *calls(2, start=2000), ev(5000, 'System Idle', context='Idle')]
        r = self.run_report('--post-mortem', rows=self.UNTIMED + tail)
        self.assertEqual([(i['name'], i['n'], i['max_us']) for i in r['isr']], [('ISR 83', 1, 3.0)])
        self.assertEqual(r['functions'][0]['n'], 2)
        self.assertEqual(r['live_window_s'], 0.004)
        self.assertIsNone(r['contexts'][0]['cpu_pct'])
        self.assertEqual(r['contexts'][0]['activations'], 10)
        self.assertTrue(any('untimed' in w for w in r['warnings']))

    def test_a_tie_without_post_mortem_only_warns(self):
        rows = [ev(0, 'Task Run', context='cdc'), ev(0, 'ISR Enter', 'Runs for 1.000 us', 'ISR 83'),
                ev(1, 'ISR Exit', context='ISR 83'), ev(100, 'Task Run', context='cdc')]
        r = self.run_report(rows=rows)
        self.assertEqual([(i['name'], i['n']) for i in r['isr']], [('ISR 83', 1)])
        self.assertTrue(any('--post-mortem' in w for w in r['warnings']))
        self.assertIsNotNone(r['contexts'][0]['cpu_pct'])

    def test_a_timed_tail_spanning_nothing_has_no_time_base(self):
        r = self.run_report('--post-mortem', rows=[*self.UNTIMED, ev(100, 'Task Run', context='cdc')])
        self.assertIsNone(r['live_window_s'])
        self.assertIsNone(r['isr'])
        self.assertTrue(any('span nothing' in w for w in r['warnings']))

    def test_who_ran_before_the_first_switch_of_a_timed_tail_is_unknown(self):
        tail = [ev(100, 'ISR Exit', context='ISR 83'), ev(300, 'Task Run', context='cdc'),
                ev(400, 'System Idle', context='Idle'), ev(1000, 'Task Run', context='cdc')]
        r = self.run_report('--post-mortem', '--window', '0:0.0001', rows=[*self.UNTIMED, *tail])
        self.assertIsNone(r['contexts_workload'])
        self.assertTrue(any('untimed head' in w for w in r['warnings']))
        r = self.run_report('--post-mortem', '--window', '0.0002:0.0008', rows=[*self.UNTIMED, *tail])
        self.assertEqual({c['name'] for c in r['contexts_workload']}, {'cdc', 'Idle'})

    def test_a_preamble_at_the_first_timestamp_is_not_an_untimed_head(self):
        rows = [ev(0, 'System Description', 'N=x'), ev(0, 'Task Run', context='cdc'),
                ev(1000, 'ISR Enter', 'Runs for 3.000 us', 'ISR 83'), ev(1003, 'ISR Exit', context='ISR 83')]
        r = self.run_report(rows=rows)
        self.assertFalse(any('untimed' in w for w in r['warnings']))
        self.assertEqual(r['live_window_s'], 0.001003)

    def test_an_error_dialog_in_the_recording_warns(self):
        (self.dir / 'record.json').write_text(json.dumps({'systemviewErrors': 1}))
        r = self.run_report(rows=[ev(1000, 'ISR Enter', 'Runs for 3.000 us', 'ISR 83'), ev(1003, 'ISR Exit')])
        self.assertTrue(any('error dialog' in w for w in r['warnings']))
        (self.dir / 'record.json').write_text(json.dumps({'systemviewErrors': 0}))
        r = self.run_report(rows=[ev(1000, 'ISR Enter', 'Runs for 3.000 us', 'ISR 83'), ev(1003, 'ISR Exit')])
        self.assertFalse(any('error dialog' in w for w in r['warnings']))

    def test_measured_none_is_an_empty_list(self):
        r = self.run_report(rows=[ev(1000, 'ISR Enter', 'Runs for 3.000 us', 'ISR 83'), ev(1003, 'ISR Exit')])
        self.assertEqual(r['functions'], [])
        self.assertEqual(r['stack'], [])
        self.assertIsNone(r['heap'])
        self.assertEqual(r['isr'][0]['name'], 'ISR 83')

    def test_malformed_exports_are_bad_usage(self):
        cases = {
            'bad activations': (CTX_HEADER + CTX_ROW.replace(',10,', ',bad,'), None),
            'truncated context row': (CTX_HEADER + 'ISR 83,#83\n', None),
            'empty contexts': ('', None),
            'empty events': (CTX_HEADER + CTX_ROW, ''),
            'truncated event row': (CTX_HEADER + CTX_ROW, EV_HEADER + '0,0.0,"ctx","Init"\n'),
            'no contextoutint': (CTX_HEADER + CTX_ROW, EV_HEADER.replace(',contextoutint', '')),
        }
        for what, (contexts, events) in cases.items():
            with self.subTest(what):
                (self.dir / 'contexts.csv').write_text(contexts)
                ev_path = self.dir / 'events.txt'
                ev_path.unlink(missing_ok=True)
                if events is not None:
                    ev_path.write_text(events)
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    report.main([str(self.dir)])
                self.assertEqual(cm.exception.code, 2)

    def test_malformed_numbers(self):
        r = self.run_report(rows=calls(2), contexts=CTX_ROW.replace('0.000 100 000 s', '1..2 s'))
        self.assertIsNone(r['contexts'][0]['total_ms'])
        for bad in ('1..2', 'NaN', 'inf', '-3.0'):
            with self.subTest(bad):
                self.assertIn('unreadable duration', self.usage_error(rows=[
                    ev(1000, 'ISR Enter', f'Runs for {bad} us'), ev(1003, 'ISR Exit')]))

    def test_unreadable_run_time_is_null_not_zero(self):
        row = CTX_ROW.replace('0.000 100 000 s', '?')
        r = self.run_report(rows=calls(2), contexts=row)
        self.assertIsNone(r['contexts'][0]['total_ms'])
        self.assertIsNone(r['contexts'][0]['avg_us'])

    def test_cpu_pct_is_systemviews_value(self):
        self.assertEqual(self.run_report(rows=calls(2))['contexts'][0]['cpu_pct'], 0.33)

    def test_missing_contexts_is_bad_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            report.main([str(self.dir)])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('contexts.csv', err.getvalue())


def schedule():
    """Idle runs, a task runs 1000..3000 with an ISR 2000..2500 inside it, idle again to 5000."""
    return [ev(0, 'System Idle', context='Idle'), ev(1000, 'Task Run', context='cdc'),
            ev(2000, 'ISR Enter', 'Runs for 500.000 us', context='ISR 83'), ev(2500, 'ISR Exit', context='ISR 83'),
            ev(3000, 'System Idle', context='Idle'), ev(5000, 'Stop', context='Idle')]


class Workload(Case):
    def test_no_anchor_leaves_workload_null(self):
        r = self.run_report(rows=schedule())
        self.assertIsNone(r['workload_anchor'])
        self.assertIsNone(r['contexts_workload'])

    def test_window_busy_shares(self):
        r = self.run_report('--window', '0.001:0.003', rows=schedule())
        self.assertEqual((r['workload_anchor'], r['workload_window_s']), ('window', 0.002))
        busy = {c['name']: c['cpu_pct_workload'] for c in r['contexts_workload']}
        self.assertEqual(busy, {'cdc': 75.0, 'ISR 83': 25.0})

    def test_window_past_the_capture_is_bad_usage(self):
        self.assertIn('past the capture', self.usage_error('--window', '0:9', rows=schedule()))

    def test_inverted_window_is_bad_usage(self):
        self.usage_error('--window', '0.003:0.001', rows=schedule())

    def test_marker_spans_first_start_to_last_stop(self):
        rows = schedule()[:-1] + [ev(1000, 'Start Marker 0x7'), ev(1500, 'Stop Marker 0x7', 'Ran for 500.000 us'),
                                  ev(2000, 'Start Marker 0x7'), ev(3000, 'Stop Marker 0x7', 'Ran for 1000.000 us'),
                                  ev(5000, 'Stop')]
        rows.sort(key=lambda row: int(row.split(',')[5]))
        r = self.run_report('--workload-marker', '7', rows=rows)
        self.assertEqual((r['workload_anchor'], r['workload_window_s']), ('marker', 0.002))
        self.assertEqual(r['markers'][0]['n'], 2)

    def test_marker_left_open_after_a_pair_warns(self):
        rows = schedule()[:-1] + [ev(1000, 'Start Marker 0x7'), ev(2000, 'Stop Marker 0x7', 'Ran for 1000.000 us'),
                                  ev(3000, 'Start Marker 0x7'), ev(5000, 'Stop')]
        rows.sort(key=lambda row: int(row.split(',')[5]))
        r = self.run_report('--workload-marker', '7', rows=rows)
        self.assertIsNone(r['workload_anchor'])
        self.assertTrue(any('left open' in w for w in r['warnings']))

    def test_zero_span_marker_warns(self):
        rows = schedule()[:-1] + [ev(3000, 'Start Marker 0x7'), ev(3000, 'Stop Marker 0x7', 'Ran for 0.000 us'),
                                  ev(5000, 'Stop')]
        rows.sort(key=lambda row: int(row.split(',')[5]))
        self.assertIsNone(self.run_report('--workload-marker', '7', rows=rows)['workload_anchor'])

    def test_loss_inside_the_window_withholds_busy_shares(self):
        rows = schedule()[:-1] + [ev(2700, '*** Overflow ***'), ev(5000, 'Stop')]
        rows.sort(key=lambda row: int(row.split(',')[5]))
        r = self.run_report('--window', '0.001:0.003', rows=rows)
        self.assertEqual(r['workload_anchor'], 'window')
        self.assertIsNone(r['contexts_workload'])
        self.assertTrue(any('lost event' in w for w in r['warnings']))

    def test_window_between_a_loss_and_the_next_switch_is_withheld(self):
        rows = [ev(0, 'System Idle', context='Idle'), ev(100, '*** Overflow ***'),
                ev(1000, 'Task Run', context='cdc'), ev(5000, 'Stop', context='Idle')]
        for window in ('0.0002:0.0008', '0.0005:0.002'):
            with self.subTest(window=window):
                r = self.run_report('--window', window, rows=rows)
                self.assertIsNone(r['contexts_workload'])

    def test_window_after_the_next_switch_is_measured(self):
        rows = [ev(0, 'System Idle', context='Idle'), ev(100, '*** Overflow ***'),
                ev(1000, 'Task Run', context='cdc'), ev(5000, 'Stop', context='Idle')]
        r = self.run_report('--window', '0.002:0.004', rows=rows)
        self.assertEqual(r['contexts_workload'], [{'name': 'cdc', 'busy_ms': 2.0, 'cpu_pct_workload': 100.0}])

    def test_without_tasks_an_isr_after_a_loss_does_not_recover(self):
        rows = [ev(100, '*** Overflow ***'), ev(200, 'ISR Enter', 'Runs for 100.000 us', context='ISR 83'),
                ev(300, 'ISR Exit', context='ISR 83'), ev(5000, 'Stop')]
        r = self.run_report('--window', '0.0003:0.0007', rows=rows)
        self.assertIsNone(r['contexts_workload'])

    def test_loss_outside_the_window_keeps_busy_shares(self):
        rows = schedule()[:-1] + [ev(4000, '*** Overflow ***'), ev(5000, 'Stop')]
        rows.sort(key=lambda row: int(row.split(',')[5]))
        r = self.run_report('--window', '0.001:0.003', rows=rows)
        self.assertEqual({c['name'] for c in r['contexts_workload']}, {'cdc', 'ISR 83'})

    def test_incomplete_marker_warns(self):
        rows = schedule()[:-1] + [ev(4000, 'Start Marker 0x7'), ev(5000, 'Stop')]
        r = self.run_report('--workload-marker', '7', rows=rows)
        self.assertIsNone(r['workload_anchor'])
        self.assertTrue(any('marker 7' in w for w in r['warnings']))

    def test_event_anchor_spans_the_function_calls(self):
        rows = schedule()[:-1] + calls(2, fid=516, start=1000, step=1900) + [ev(5000, 'Stop')]
        rows.sort(key=lambda row: int(row.split(',')[5]))
        r = self.run_report('--workload-event', '516', rows=rows)
        self.assertEqual((r['workload_anchor'], r['workload_window_s']), ('event', 0.00191))

    def test_single_event_occurrence_warns(self):
        r = self.run_report('--workload-event', '516', rows=schedule()[:-1] + [ev(1000, 'Function #516'),
                                                                                ev(5000, 'Stop')])
        self.assertIsNone(r['workload_anchor'])

    def test_anchor_without_events_is_bad_usage(self):
        self.usage_error('--workload-event', '516', events=False)

    def test_time_after_a_block_or_an_isr_exit_to_the_scheduler_is_the_schedulers(self):
        rows = [ev(0, 'Task Run', context='cdc'), ev(1000, 'Task Block', 'Delayed', context='cdc', ctx_out=SCHED_ID),
                ev(1500, 'Task Run', context='io'), ev(2000, 'ISR Enter', 'Runs for 200.000 us', context='SysTick'),
                ev(2200, 'ISR Exit', 'Returns to Scheduler', context='SysTick'), ev(2600, 'Task Run', context='cdc'),
                ev(5000, 'Stop', context='cdc')]
        r = self.run_report('--window', '0:0.003', rows=rows)
        busy = {c['name']: c['busy_ms'] for c in r['contexts_workload']}
        self.assertEqual(busy, {'cdc': 1.4, 'Scheduler': 0.9, 'io': 0.5, 'SysTick': 0.2})

    def test_blocking_another_task_keeps_the_runner(self):
        # a real export names the running task in context and prefixes the blocked one to the detail
        rows = [ev(0, 'Task Run', context='cdc'), ev(1000, 'Task Block', 'io, Suspended', context='cdc'),
                ev(5000, 'Stop', context='cdc')]
        r = self.run_report('--window', '0:0.003', rows=rows)
        self.assertEqual([c['name'] for c in r['contexts_workload']], ['cdc'])

    def test_busy_never_exceeds_the_window(self):
        r = self.run_report('--window', '0:0.005', rows=schedule())
        self.assertLessEqual(sum(c['cpu_pct_workload'] for c in r['contexts_workload']), 100.0)


class RealExports(unittest.TestCase):
    def run_on(self, name, *flags):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(DATA / name, Path(d) / name)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(report.main([str(Path(d) / name), '--json', *flags]), 0)
        return json.loads(out.getvalue())

    def test_freertos_tasks_ready_latency_and_contexts(self):
        r = self.run_on('h743-freertos')
        self.assertEqual(r['warnings'], [])
        names = {c['name'] for c in r['contexts']}
        self.assertTrue({'cdc', 'usbd', 'Idle', 'SysTick'} <= names)
        self.assertEqual({row['name'] for row in r['ready_run']}, {'io', 'cdc'})
        self.assertEqual(r['overflow'], 0)

    def test_freertos_scheduler_after_a_task_blocks(self):
        # cdc blocks at 194.303 us; SystemView names the context Scheduler until Tmr Svc runs at 203.268 us
        r = self.run_on('h743-freertos', '--window', '0.000196:0.000200')
        self.assertEqual([c['name'] for c in r['contexts_workload']], ['Scheduler'])

    def test_freertos_task_blocking_another_keeps_running(self):
        # the h743 slice re-decoded with cdc's Task Block at 194.303 us naming blinky: cdc runs on to 203.268 us
        r = self.run_on('h743-block-other-task', '--window', '0.000196:0.000200')
        self.assertEqual([c['name'] for c in r['contexts_workload']], ['cdc'])

    def test_baremetal_overflow_and_functions(self):
        r = self.run_on('f407-baremetal')
        self.assertGreater(r['overflow'], 0)
        self.assertIn(512, [f['id'] for f in r['functions']])
        self.assertEqual(r['live_window_s'] is None, False)

    def test_text_output_runs(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            shutil.copytree(DATA / 'f407-baremetal', Path(d) / 'x')
            report.main([str(Path(d) / 'x')])
        self.assertIn('overflow:', out.getvalue())


if __name__ == '__main__':
    unittest.main()
