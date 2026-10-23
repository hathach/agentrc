"""Real packet/export pairs and synthetic boundary tests for the native decoder.

The full corpus runner reports unresolved GUI parity differences; these tests do not bless those differences.
Synthetic packets exercise malformed input and records absent from the hardware corpus, not hardware coverage.
"""
import contextlib
import copy
import gzip
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, tmpdir  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import sysview_decode as decode  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bench' / 'sysview'))
import sysview_corpus as corpus  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'sysview'


def setUpModule():
    guard_survivors(__name__)


def uint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def packet(event, *args, delta=10, text=None, payload=None):
    if payload is None:
        payload = b''.join(uint(x) for x in args)
        if text is not None:
            value = text.encode('utf-8')
            size = bytes([len(value)]) if len(value) < 255 else b'\xff' + len(value).to_bytes(2, 'big')
            payload += size + value
    return uint(event) + (uint(len(payload)) if event >= 24 else b'') + payload + uint(delta)


def header(freq=1_000_000, shift=0):
    return decode.SYNC + packet(10, delta=0) + packet(24, freq, 48_000_000, 0x20000000, shift, delta=0)


def packets(data, rows):
    return [decode.packet_at(data, int(r['eventoffset']))[0] for r in rows]


def row_proofs(data, gui, native):
    return corpus.row_proofs(packets(data, gui), gui, native, corpus.normalized_rows(gui), corpus.normalized_rows(native))


def classify(data, gui, native, expected, actual, source, diffs, report_diffs, metadata=None):
    return corpus.classify(packets(data, gui), gui, native, expected, actual, source, diffs, report_diffs,
                           corpus.normalized_rows(gui), corpus.normalized_rows(native), metadata)


class Fixtures(unittest.TestCase):
    def test_hardware_packets_and_selected_normalized_rows(self):
        for name in ('baremetal', 'freertos', 'overflow', 'wrapped', 'truncated', 'timer-wrap'):
            with self.subTest(name=name):
                directory = FIXTURES / name
                data = gzip.decompress((directory / 'capture.SVDat.gz').read_bytes())
                metadata = json.loads((directory / 'fixture.json').read_text())
                self.assertEqual(hashlib.sha256(data).hexdigest(), metadata['raw_sha256'])
                native = decode.Decoder().decode(data)
                self.assertEqual(len(native.rows), metadata['event_count'])
                expected = corpus.read_rows(directory / 'events.txt')
                actual = [native.rows[int(row['sequencenum'])] for row in expected]
                for row in expected:
                    offset, size = int(row['eventoffset']), int(row['eventsize'])
                    self.assertEqual(data[offset:offset + size], bytes.fromhex(row['eventdata']))
                self.assertEqual(corpus.normalized_rows(actual), corpus.normalized_rows(expected))
                if name == 'timer-wrap':
                    self.assertGreater(native.rows[-1]['timestampint'], 2**32)
                    evidence = metadata['wrap_evidence']
                    first = next(i for i, r in enumerate(native.rows) if r['timestampint'] >= 2**32)
                    self.assertEqual(first, evidence['first_wrap_row'])
                    self.assertEqual([r['eventoffset'] for r in native.rows if r['eventint'] == 24], [12])
                    end = native.rows[first]['eventoffset'] + native.rows[first]['eventsize']
                    self.assertEqual(end, evidence['minimal_contiguous_prefix_bytes'])
                    self.assertGreater(len(gzip.compress(data[:end], mtime=0)), 350_000)
                if name == 'wrapped':
                    self.assertIn('unsynchronized head', native.diagnostics[0]['reason'])
                if name == 'truncated':
                    self.assertEqual(native.diagnostics[-1]['reason'], 'timestamp delta: truncated packet')

    def test_report_parity_on_clean_and_truncated_fixtures(self):
        for name in ('baremetal', 'freertos', 'wrapped', 'truncated'):
            with self.subTest(name=name):
                tmp = tmpdir(self)
                directory = FIXTURES / name
                raw = Path(tmp) / 'raw.SVDat'
                raw.write_bytes(gzip.decompress((directory / 'capture.SVDat.gz').read_bytes()))
                decode.decode_file(raw, tmp)
                expected = json.loads((directory / 'report.json').read_text())
                actual = corpus.json_report(tmp)
                for context in expected['contexts']:
                    context['blocked_ms'] = None
                if name == 'freertos':
                    # The final partial ISR interrupts Idle; GUI charges that time as Idle execution.
                    next(c for c in expected['contexts'] if c['name'] == 'Idle')['cpu_pct'] = 85.11
                self.assertEqual(actual, expected)
                comparison = corpus.compare(raw, directory, tmp)
                self.assertTrue(comparison['accepted'], comparison['reportDiffs'])
                if name == 'freertos':
                    self.assertEqual(comparison['classifications']['gui-inconsistent'], 1)

    def test_corpus_comparison_checks_the_pair_before_decoding(self):
        tmp = tmpdir(self)
        root = Path(tmp)
        raw, gui, out = root / 'raw', root / 'gui', root / 'out'
        gui.mkdir()
        out.mkdir()
        source = FIXTURES / 'baremetal'
        raw.write_bytes(gzip.decompress((source / 'capture.SVDat.gz').read_bytes()))
        for name in ('events.txt', 'contexts.csv'):
            (gui / name).write_bytes((source / name).read_bytes())
        result = corpus.compare(raw, gui, out)
        self.assertTrue(result['equal'], result)
        raw.write_bytes(b'wrong capture')
        with self.assertRaisesRegex(decode.DecodeError, 'does not match raw bytes'):
            corpus.compare(raw, gui, out)

    def test_manifest_hash_mismatch_is_an_error(self):
        tmp = tmpdir(self)
        (Path(tmp) / 'raw').write_bytes(b'x')
        with self.assertRaisesRegex(decode.DecodeError, 'sha256 mismatch'):
            corpus.verify(Path(tmp), {'raw': {'sha256': '0' * 64}})

    def test_corrupt_content_preserves_real_packet_boundaries_and_clock(self):
        directory = FIXTURES / 'corrupt-content'
        metadata = json.loads((directory / 'fixture.json').read_text())
        data = gzip.decompress((directory / 'capture.SVDat.gz').read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), metadata['raw_sha256'])
        self.assertEqual(data[1688:1694], bytes.fromhex('02 03 92 03 02 01'))
        result = decode.Decoder().decode(data)
        self.assertEqual(len(result.rows), metadata['event_count'])
        self.assertEqual(result.rows[-2]['eventoffset'], 3156)
        self.assertEqual(result.rows[-1]['eventoffset'], 3162)
        self.assertEqual(result.diagnostics, [{'offset': 3156, 'bytes': 6,
                                             'reason': 'malformed payload: truncated packet'}])
        self.assertFalse(result.time_lost)
        self.assertTrue(all(isinstance(r['timestampint'], int) for r in result.rows))
        expected = corpus.read_rows(directory / 'events.txt')
        actual = [result.rows[int(row['sequencenum'])] for row in expected]
        for gui, native in zip(expected, actual):
            for key in ('eventoffset', 'eventint', 'eventsize', 'timestampint', 'contextinint', 'contextint', 'contextoutint'):
                self.assertEqual(str(native[key]), gui[key], (key, gui))
            self.assertEqual(bytes.fromhex(gui['eventdata']), bytes.fromhex(native['eventdata']))
        self.assertTrue(all(r['Total Run Time'] != '' for r in result.context_rows()))


class Packets(unittest.TestCase):
    def test_sample_data_float_bits_are_varints_and_preserve_following_packets(self):
        for bits, size in ((0, 1), (0x3f800000, 5)):
            with self.subTest(bits=bits):
                self.assertEqual(len(uint(bits)), size)
                sample = packet(23, 7, bits, delta=123)
                parsed, end = decode.packet_at(sample + packet(2, 15) + packet(3), 0)
                self.assertEqual((parsed.args, parsed.delta, end), ([7, bits], 123, len(sample)))
                result = decode.Decoder().decode(header() + sample + packet(2, 15) + packet(3))
                self.assertEqual([r['eventint'] for r in result.rows], [10, 24, 23, 2, 3])
                self.assertEqual(result.rows[-2]['detail'], 'Runs for 10.000 us')
                self.assertFalse(result.diagnostics)

    def test_tail_payload_and_delta_cuts_do_not_lose_time(self):
        prefix = header() + packet(4, 1) + packet(2, 15) + packet(3) + packet(7, 1, 4)
        root = tmpdir(self)
        raw = root / 'raw'
        raw.write_bytes(prefix)
        decode.decode_file(raw, root)
        before = corpus.json_report(root)
        for tail in (packet(28, 512)[:-1], packet(28, 512)[:-2], packet(28, 512)[:-1] + b'\xff' * 6):
            with self.subTest(tail=tail.hex()):
                raw.write_bytes(prefix + tail)
                metadata = decode.decode_file(raw, root)
                self.assertFalse(metadata['timingLost'])
                self.assertEqual(metadata['timingLosses'], [])
                self.assertTrue(metadata['diagnostics'])
                self.assertEqual(corpus.json_report(root), before)

    def test_loss_marker_retains_the_boundary_event_detail(self):
        result = decode.Decoder().decode(header() + packet(512) + packet(24, 2_000_000, 0, 0, 0))
        self.assertEqual(result.rows[-1]['detail'],
                         'Cycle Freq.: 2000000, CPU Freq.: 0, ID Base: 0x00000000, ID Shift: 0 *** TIMING LOST ***')

    def test_varint_bounds_and_truncation(self):
        self.assertEqual(decode.Reader(b'\xff\xff\xff\xff\x0f').uint(), 2**32 - 1)
        for data in (b'\x80', b'\xff\xff\xff\xff\x10', b'\x80' * 6):
            with self.subTest(data=data), self.assertRaises(decode.DecodeError):
                decode.Reader(data).uint()

    def test_counted_strings_use_big_endian_extended_length(self):
        value = 'x' * 300
        row = decode.Decoder().decode(header() + packet(14, text=value)).rows[-1]
        self.assertEqual(row['detail'], value)

    def test_unknown_sized_packets_do_not_assume_varint_payloads(self):
        data = header() + packet(777, payload=b'\xff' * 8) + packet(28, 777)
        result = decode.Decoder().decode(data)
        self.assertEqual(len(result.rows), 4)
        self.assertEqual(result.rows[-1]['detail'], 'Returns after 10.000 us')
        self.assertFalse(result.diagnostics)

    def test_malformed_payload_does_not_consume_the_next_frame(self):
        malformed = packet(28, payload=b'\xe1\x01\xb1', delta=2)
        data = header() + malformed + packet(2, 15)
        result = decode.Decoder().decode(data)
        self.assertEqual([r['eventint'] for r in result.rows], [10, 24, 28, 2])
        self.assertEqual(result.rows[-1]['eventoffset'], len(header()) + len(malformed))
        self.assertFalse(result.time_lost)
        self.assertEqual(result.rows[2]['event'], 'End Call')
        self.assertEqual([r['timestampint'] for r in result.rows], [0, 0, 2, 12])
        self.assertEqual(decode.packet_at(data, len(header()))[0].args, [])

    def test_empty_extension_is_a_payload_error(self):
        result = decode.Decoder().decode(header() + packet(31) + packet(512))
        self.assertEqual(result.rows[-2]['event'], 'Event #31')
        self.assertIn('Malformed payload:', result.rows[-2]['detail'])
        self.assertEqual(result.rows[-1]['eventint'], 512)

    def test_a_bounded_bad_init_does_not_hide_a_later_valid_frequency(self):
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(decode.SYNC + packet(10) + packet(24, payload=b'\x80')
                        + packet(24, 1_000_000, 48_000_000, 0, 0) + packet(512) + packet(28, 512))
        metadata = decode.decode_file(raw, tmp)
        self.assertFalse(metadata['timingLost'])
        self.assertEqual(corpus.json_report(tmp)['functions'][0]['max_us'], 10.0)

    def test_padding_at_a_known_boundary_is_not_a_timestamped_event(self):
        result = decode.Decoder().decode(header() + b'\0' + packet(512, delta=37))
        self.assertEqual(result.rows[-1]['timestampint'], 37)
        self.assertFalse(result.time_lost)
        self.assertEqual(result.diagnostics[0]['reason'], 'unexpected padding byte')

    def test_late_os_description_resolves_earlier_low_function_ids(self):
        data = header() + packet(100) + packet(28, 100)
        self.assertEqual(decode.Decoder().decode(data).rows[2]['event'], 'Function #100')
        result = decode.Decoder().decode(data + packet(14, text='O=FreeRTOS'))
        self.assertEqual([r['event'] for r in result.rows[2:4]], ['Event #100', 'Event #100'])

    def test_resync_does_not_invent_missing_time(self):
        data = header() + packet(512) + b'\xff' * 6 + header() + packet(4, 1) + packet(7, 1, 4)
        result = decode.Decoder().decode(data)
        self.assertTrue(result.time_lost)
        self.assertEqual([r['timestampint'] for r in result.rows[:3]], [0, 0, 10])
        self.assertTrue(all(r['timestampint'] == '' for r in result.rows[3:]))
        self.assertEqual(result.contexts[decode.TASK | 1].activations, 0)
        self.assertIn('32 bits', result.diagnostics[0]['reason'])

    def test_resync_discards_a_head_without_decoding_its_accidental_events(self):
        data = b'\x01\x02\x03' + header() + packet(512)
        result = decode.Decoder().decode(data)
        self.assertEqual([r['eventint'] for r in result.rows], [10, 24, 512])
        self.assertEqual(result.diagnostics[0]['bytes'], 3)

    def test_zeros_in_a_wrapped_payload_are_not_a_sync_without_start(self):
        data = b'junk' + decode.SYNC + packet(512) + header() + packet(4, 1)
        result = decode.Decoder().decode(data)
        self.assertEqual([r['eventint'] for r in result.rows], [10, 24, 4])

    def test_registered_modules_are_explicitly_unsupported(self):
        with self.assertRaisesRegex(decode.DecodeError, 'module descriptions'):
            decode.Decoder().decode(header() + packet(27, 1))

    def test_no_init_withholds_time_metrics(self):
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(decode.SYNC + packet(10) + packet(512) + packet(28, 512))
        decode.decode_file(raw, tmp)
        result = corpus.json_report(tmp)
        self.assertIsNone(result['functions'])
        self.assertIsNone(result['live_window_s'])
        self.assertEqual(result['overflow'], 0)

    def test_empty_no_sync_and_incomplete_init_fail(self):
        for data in (b'', b'noise', decode.SYNC, decode.SYNC + b'\x18\x05\x01'):
            with self.subTest(data=data), self.assertRaises(decode.DecodeError):
                decode.Decoder().decode(data)

    def test_counter_delta_is_added_without_a_host_32_bit_wrap(self):
        data = header() + packet(512, delta=2**32 - 5) + packet(28, 512, delta=20)
        result = decode.Decoder().decode(data)
        self.assertEqual(result.rows[-1]['timestampint'], 2**32 + 15)
        self.assertEqual(result.rows[-1]['detail'], 'Returns after 20.000 us')

    def test_frequency_change_invalidates_the_time_base(self):
        data = header() + packet(512) + packet(24, 2_000_000, 48_000_000, 0x20000000, 0)
        result = decode.Decoder().decode(data)
        self.assertTrue(result.time_lost)
        self.assertEqual([r['timestampint'] for r in result.rows], [0, 0, 10, ''])
        self.assertEqual(result.freq, 1_000_000)

    def test_timed_prefix_metrics_survive_missing_delta_resync_and_frequency_change(self):
        prefix = header() + packet(4, 1) + packet(512) + packet(28, 512) + packet(7, 1, 4)
        for loss in (packet(512)[:-1] + b'\xff' * 6 + header(),
                     packet(24, 2_000_000, 48_000_000, 0, 0)):
            with self.subTest(loss=loss.hex()):
                tmp = tmpdir(self)
                raw = Path(tmp) / 'raw'
                raw.write_bytes(prefix)
                decode.decode_file(raw, tmp)
                before = corpus.json_report(tmp)
                raw.write_bytes(prefix + loss + packet(4, 2) + packet(512) + packet(28, 512))
                metadata = decode.decode_file(raw, tmp)
                after = corpus.json_report(tmp)
                (Path(tmp) / 'decode.json').unlink()
                self.assertEqual(corpus.json_report(tmp), after)
                self.assertEqual(metadata['timedEvents'], 6)
                self.assertEqual(len(metadata['timingLosses']), 1)
                self.assertEqual(after['functions'], before['functions'])
                self.assertEqual(after['live_window_s'], before['live_window_s'])
                self.assertEqual(after['overflow'], before['overflow'] + 1)
                task = next(c for c in after['contexts'] if c['name'] == 'Task 0x1')
                self.assertEqual(task, next(c for c in before['contexts'] if c['name'] == 'Task 0x1'))
                self.assertNotIn('Task 0x2', [c['name'] for c in after['contexts']])
                contexts = corpus.read_rows(Path(tmp) / 'contexts.csv')
                self.assertEqual(next(c for c in contexts if c['Name'] == 'Task 0x2')['Activations'], '0')
                self.assertIn('first 6 events', after['warnings'][0])
                for flags in (['--window', '0:0.00004'], ['--workload-event', '512']):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        corpus.report.main([str(tmp), '--json', *flags])
                    self.assertIsNotNone(json.loads(output.getvalue())['contexts_workload'])

    def test_system_time_records_do_not_reanchor_a_lost_packet_clock(self):
        data = header() + packet(512) + b'\xff' * 6 + header() + packet(12, 123) + packet(13, 500, 0)
        result = decode.Decoder().decode(data)
        self.assertEqual(result.timed_events, 3)
        self.assertTrue(all(r['timestampint'] == '' for r in result.rows[3:]))

    def test_native_untimed_suffix_annotations_cannot_extend_prefix_metrics(self):
        root = tmpdir(self)
        raw = root / 'raw'
        prefix = (header() + packet(4, 1, delta=0) + packet(513, delta=20) + packet(28, 513, delta=10)
                  + packet(512, delta=70))
        raw.write_bytes(prefix)
        decode.decode_file(raw, root)
        expected = corpus.json_report(root)
        raw.write_bytes(prefix + b'\xff' * 6 + header() + packet(28, 512) + packet(2, 15)
                        + packet(6, 2) + packet(16, 1) + packet(514) + packet(28, 514))
        metadata = decode.decode_file(raw, root)
        rows = corpus.read_rows(root / 'events.txt')
        annotations = {'28': 'Returns after 200.000 us', '2': 'Runs for 50.000 us',
                       '6': 'Task 0x2, runs after 25.000 us', '16': 'Ran for 75.000 us'}
        for row in rows[metadata['timedEvents']:]:
            self.assertEqual(row['timestampint'], '')
            if row['eventint'] in annotations:
                row['detail'] = annotations[row['eventint']]
        decode.write_csv(root / 'events.txt', decode.EVENT_COLUMNS, rows)
        (root / 'decode.json').unlink()
        actual = corpus.json_report(root)
        for key in ('functions', 'isr', 'ready_run', 'markers', 'live_window_s', 'contexts'):
            self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(actual['live_window_s'], .0001)
        self.assertEqual(actual['overflow'], 1)
        self.assertEqual(metadata['timedEvents'], 6)

    def test_unmarked_timestamp_damage_does_not_trust_native_full_capture_aggregates(self):
        root = tmpdir(self)
        raw = root / 'raw'
        raw.write_bytes(header() + packet(4, 1, delta=0) + packet(512, delta=100) + packet(513)
                        + packet(28, 512, delta=200) + packet(2, 15) + packet(3, delta=50))
        decode.decode_file(raw, root)
        rows = corpus.read_rows(root / 'events.txt')
        self.assertEqual(rows[-3]['detail'], 'Returns after 210.000 us')
        self.assertEqual(rows[-2]['detail'], 'Runs for 50.000 us')
        rows[4]['timestampint'] = 'unreadable'
        decode.write_csv(root / 'events.txt', decode.EVENT_COLUMNS, rows)
        actual = corpus.json_report(root)
        for key in ('functions', 'isr', 'live_window_s'):
            self.assertIsNone(actual[key], key)
        self.assertTrue(all(context[key] is None for context in actual['contexts']
                            for key in corpus.report.CONTEXT_TIMES))

    def test_post_mortem_head_trim_and_untimed_suffix_compose(self):
        data = (header() + packet(4, 1, delta=0) + packet(17, delta=0) + packet(512) + packet(28, 512)
                + b'\xff' * 6 + header() + packet(512))
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(data)
        decode.decode_file(raw, tmp)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            corpus.report.main([str(tmp), '--json', '--post-mortem'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['live_window_s'], .00001)
        self.assertEqual(result['overflow'], 1)
        self.assertEqual(result['functions'][0]['n'], 1)

    def test_malformed_return_preserves_other_timing_but_drops_ambiguous_calls(self):
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(header() + packet(512) + packet(28, payload=b'\x80') + packet(28, 512)
                        + packet(513) + packet(28, 513))
        metadata = decode.decode_file(raw, tmp)
        result = corpus.json_report(tmp)
        (Path(tmp) / 'decode.json').unlink()
        self.assertEqual(corpus.json_report(tmp), result)
        self.assertFalse(metadata['timingLost'])
        self.assertEqual(result['live_window_s'], .00005)
        self.assertEqual([f['id'] for f in result['functions']], [513])
        self.assertEqual(result['dropped_pairs'], 2)

    def test_truncated_tail_keeps_complete_packets(self):
        result = decode.Decoder().decode(header() + packet(512) + packet(28, 512)[:-1])
        self.assertEqual([r['eventint'] for r in result.rows], [10, 24, 512])
        self.assertEqual(result.diagnostics[-1]['reason'], 'timestamp delta: truncated packet')

    def test_multicore_gui_header_is_refused(self):
        with self.assertRaisesRegex(decode.DecodeError, 'multicore'):
            decode.Decoder().decode(b'; Offset Core0 0\n; Offset Core1 5\n' + header())


class Analysis(unittest.TestCase):
    def test_partial_isrs_interrupt_ancestors_at_eof_and_stop_without_samples(self):
        for nested in (False, True):
            for stop in (False, True):
                with self.subTest(nested=nested, stop=stop):
                    data = header() + packet(4, 1, delta=0) + packet(2, 15, delta=100)
                    if nested:
                        data += packet(2, 83, delta=50)
                    data += packet(11 if stop else 512, delta=50 if nested else 100)
                    result = decode.Decoder().decode(data)
                    task = result.contexts[decode.TASK | 1]
                    self.assertEqual((task.total, task.interrupted, task.runs), (200, 100, []))
                    self.assertEqual(next(c for c in result.context_rows() if c['Name'] == 'Task 0x1')['CPU Load'], '50.00 %')
                    outer = result.contexts[decode.ISR | 15]
                    self.assertEqual((outer.total, outer.interrupted, outer.runs), (100, 50 if nested else 0, []))
                    self.assertTrue(all(not r['detail'] for r in result.rows if r['eventint'] == 2))

    def test_timer_propagates_only_isr_interruption_to_its_parent(self):
        for completed in (True, False):
            with self.subTest(completed=completed):
                data = (header() + packet(4, 1, delta=0) + packet(19, 0x200, delta=100)
                        + packet(2, 15, delta=100) + packet(3, delta=100))
                if completed:
                    data += packet(20, delta=100) + packet(7, 1, 4, delta=100)
                else:
                    data += packet(512, delta=200)
                result = decode.Decoder().decode(data)
                task = result.contexts[decode.TASK | 1]
                self.assertEqual((task.total, task.interrupted), (500, 100))
                self.assertEqual(task.runs, [400] if completed else [])

    def test_no_events_skips_formatting_but_preserves_accounting_and_diagnostics(self):
        data = (header() + packet(8, 1) + packet(4, 1) + packet(19, 2) + packet(2, 15)
                + packet(3) + packet(20) + packet(7, 1, 4) + b'\xff' * 6 + header())
        root = tmpdir(self)
        raw = root / 'raw'
        raw.write_bytes(data)
        full = decode.decode_file(raw, root)
        contexts = (root / 'contexts.csv').read_bytes()
        (root / 'events.txt').unlink()
        with mock.patch.object(decode.Decoder, 'format_rows', side_effect=AssertionError('formatting')):
            compact = decode.decode_file(raw, root, no_events=True)
        self.assertEqual(full, compact)
        self.assertEqual((root / 'contexts.csv').read_bytes(), contexts)
        self.assertFalse((root / 'events.txt').exists())

    def test_freertos_states_are_masked_and_blocked_time_is_explicitly_undefined(self):
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(header() + packet(14, text='N=test,O=FreeRTOS') + packet(4, 1)
                        + packet(7, 1, 0x104) + packet(4, 1) + packet(7, 1, 27))
        metadata = decode.decode_file(raw, tmp)
        evidence = metadata['withheldMetrics']['blockedTime']
        self.assertIn('FreeRTOS has no verified preemption-state rule', evidence)
        rows = corpus.read_rows(Path(tmp) / 'events.txt')
        self.assertEqual([r['detail'] for r in rows if r['eventint'] == '7'], ['Delayed', 'Suspended'])
        self.assertTrue(all(c['blocked_ms'] is None for c in corpus.json_report(tmp)['contexts']))

    def test_nested_isrs_resume_the_interrupted_context(self):
        data = header() + packet(4, 1) + packet(2, 15) + packet(2, 83) + packet(3) + packet(3) + packet(7, 1, 4)
        result = decode.Decoder().decode(data)
        rows = result.rows
        self.assertEqual(rows[3]['detail'], 'Runs for 30.000 us')
        self.assertEqual(rows[4]['detail'], 'Runs for 10.000 us')
        self.assertEqual(rows[5]['detail'], 'Returns to ISR 15')
        self.assertEqual(rows[6]['contextoutint'], '0x0000000800000001')
        self.assertEqual(result.contexts[decode.TASK | 1].interrupted, 30)

    def test_blocking_another_task_keeps_the_running_task(self):
        rows = decode.Decoder().decode(header() + packet(4, 1) + packet(7, 2, 4)).rows
        self.assertEqual(rows[-1]['contextint'], rows[-1]['contextoutint'])
        self.assertIn('Task 0x2', rows[-1]['detail'])

    def test_initial_scheduler_is_not_charged_to_the_first_task(self):
        result = decode.Decoder().decode(header() + packet(18) + packet(4, 1, delta=100))
        self.assertTrue(all(r['Total Blocked Time'] == '' for r in result.context_rows()))
        self.assertEqual(result.contexts[decode.SCHEDULER].total, 100)

    def test_overflow_discards_a_provisional_ready_activation(self):
        result = decode.Decoder().decode(header() + packet(6, 1) + packet(1, 2) + packet(4, 1))
        self.assertEqual(result.contexts[decode.TASK | 1].activations, 1)

    def test_nested_scheduler_request_waits_for_outer_isr_exit(self):
        data = header() + packet(4, 1) + packet(2, 15) + packet(2, 83) + packet(18) + packet(3)
        rows = decode.Decoder().decode(data).rows
        self.assertEqual(rows[-2]['contextoutint'], f'0x{decode.ISR | 15:016X}')
        self.assertEqual(rows[-1]['contextoutint'], f'0x{decode.SCHEDULER:016X}')

    def test_incomplete_isr_has_no_duration_annotation(self):
        result = decode.Decoder().decode(header() + packet(2, 15) + packet(512))
        self.assertEqual(result.rows[2]['detail'], '')
        self.assertEqual(result.contexts[decode.ISR | 15].total, 10)
        self.assertEqual(result.contexts[decode.ISR | 15].runs, [])

    def test_overflow_discards_open_spans_and_keeps_real_clock_delta(self):
        data = (header() + packet(512) + packet(15, 3) + packet(2, 15) + packet(1, 20, delta=1000)
                + packet(1, 30, delta=2000) + packet(28, 512) + packet(16, 3))
        result = decode.Decoder().decode(data)
        self.assertEqual([r['timestampint'] for r in result.rows[5:8]], [31, 32, 3040])
        self.assertEqual(result.rows[4]['detail'], '')
        self.assertEqual(result.rows[7]['detail'], 'Returns')
        self.assertEqual(result.rows[8]['detail'], '')

    def test_timer_returns_keep_task_runtime_and_do_not_count_as_isr_interruption(self):
        data = header() + packet(4, 1) + packet(19, 0x200) + packet(20) + packet(7, 1, 4)
        result = decode.Decoder().decode(data)
        self.assertEqual(result.rows[3]['context'], 'Timer 0x200')
        task = result.contexts[decode.TASK | 1]
        self.assertEqual((task.total, task.interrupted, task.runs), (30, 0, [30]))

    def test_stop_ends_the_context_without_completing_an_isr_sample(self):
        result = decode.Decoder().decode(header() + packet(2, 15) + packet(11) + packet(10))
        self.assertEqual(result.rows[3]['contextoutint'], '0x0000000000000000')
        self.assertEqual(result.rows[4]['contextint'], '0x0000000000000000')
        self.assertEqual(result.rows[2]['detail'], '')
        self.assertEqual(result.contexts[decode.ISR | 15].total, 10)

    def test_markers_stack_and_heap_totals(self):
        data = (header() + packet(9, 1, 2, text='worker') + packet(21, 1, 0x20001000, 1024, 96)
                + packet(15, 3) + packet(16, 3, delta=250) + packet(31, 2, 100, 101, 1000, 8)
                + packet(31, 3, 100, 200, 32) + packet(31, 4, 100, 300, 64, 9) + packet(31, 5, 100, 200))
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(data)
        decode.decode_file(raw, tmp)
        result = corpus.json_report(tmp)
        self.assertEqual(result['stack'], [{'name': 'worker', 'bytes_used': 96}])
        self.assertEqual(result['heap'], {'allocs': 2, 'frees': 1, 'net_bytes': 72})
        self.assertEqual(result['markers'][0]['max_us'], 250.0)

    def test_heap_totals_are_not_reused_after_loss_or_unmatched_free(self):
        for ending in (packet(1, 3), packet(31, 5, 100, 201)):
            data = header() + packet(31, 2, 100, 101, 1000, 0) + packet(31, 3, 100, 200, 32) + ending
            result = decode.Decoder().decode(data)
            self.assertEqual(next(r['detail'] for r in result.rows if r['event'] == 'Allocate Memory'), '')


class CorpusProofs(unittest.TestCase):
    @staticmethod
    def rows(data):
        return [{k: str(v) for k, v in r.items()} for r in decode.Decoder().decode(data).rows]

    def test_boundary_artifact_requires_exact_position_value_and_packet_semantics(self):
        data = header() + packet(4, 1) + packet(512) * 999
        native = self.rows(data)
        for index, value, accepted in ((999, corpus.LOST, True), (998, corpus.LOST, False),
                                        (999, f'0x{decode.IDLE:016X}', False)):
            with self.subTest(index=index, value=value):
                gui = copy.deepcopy(native)
                gui[index]['contextoutint'] = value
                _, proofs = row_proofs(data, gui, native)
                self.assertEqual((index, 'contextoutint') in proofs, accepted)
        gui = copy.deepcopy(native)
        gui[999]['contextoutint'] = corpus.LOST
        native[999]['contextoutint'] = f'0x{decode.IDLE:016X}'
        _, proofs = row_proofs(data, gui, native)
        self.assertNotIn((999, 'contextoutint'), proofs)

    def test_isr_entry_at_boundary_proves_its_context_even_before_real_loss(self):
        data = header() + packet(512) * 997 + packet(2, 15) + packet(1, 5)
        native = self.rows(data)
        gui = copy.deepcopy(native)
        gui[999]['contextoutint'] = corpus.LOST
        _, proofs = row_proofs(data, gui, native)
        self.assertEqual(proofs[999, 'contextoutint']['classification'], 'gui-export-artifact')

    def test_loss_annotation_needs_an_exact_interval_not_just_a_loss_somewhere(self):
        data = header() + packet(2, 15) + packet(1, 5)
        native = self.rows(data)
        for annotation, accepted in (('Runs for 1.000 us', True), ('Runs for 2.000 us', False)):
            gui = copy.deepcopy(native)
            gui[2]['detail'] = annotation
            _, proofs = row_proofs(data, gui, native)
            self.assertEqual((2, 'detail') in proofs, accepted)
        clean = header() + packet(2, 15) + packet(3)
        gui = self.rows(clean)
        native = copy.deepcopy(gui)
        native[2]['detail'] = ''
        self.assertNotIn((2, 'detail'), row_proofs(clean, gui, native)[1])

    def test_active_ready_proof_requires_a_contradictory_latency(self):
        data = header() + packet(4, 1) + packet(2, 15) + packet(6, 1) + packet(18) + packet(4, 1)
        native = self.rows(data)
        for latency, accepted in (('20.000 us', False), ('5.000 s', True)):
            gui = copy.deepcopy(native)
            gui[4]['detail'] = 'Task 0x1, runs after ' + latency
            proof = row_proofs(data, gui, native)[1].get((4, 'detail'))
            self.assertEqual(proof is not None, accepted)
            if proof:
                self.assertEqual(proof['actualTicksToNextRun'], 20)

    def test_long_packet_artifact_requires_exact_512_byte_prefix(self):
        data = header() + packet(512, payload=b'x' * 600)
        native = self.rows(data)
        gui = copy.deepcopy(native)
        gui[-1]['eventdata'] = bytes.fromhex(gui[-1]['eventdata'])[:512].hex(' ').upper() + ' '
        gui[-1]['eventsize'] = '512'
        _, proofs = row_proofs(data, gui, native)
        self.assertEqual(proofs[2, 'eventsize']['framedBytes'], 605)
        gui[-1]['eventdata'] = '00 ' * 512
        self.assertFalse(row_proofs(data, gui, native)[1])

    def test_report_classification_requires_closed_accounting(self):
        data = header() + packet(2, 15) + packet(1, 5)
        native = self.rows(data)
        gui = copy.deepcopy(native)
        gui[2]['detail'] = 'Runs for 1.000 us'
        tmp = tmpdir(self)
        root = Path(tmp)
        decode.write_csv(root / 'events.txt', decode.EVENT_COLUMNS, gui)
        decode.write_csv(root / 'contexts.csv', decode.CONTEXT_COLUMNS, decode.Decoder().decode(data).context_rows())
        expected = corpus.json_report(root)
        actual = corpus.report_with_rows(root, native)
        actual['overflow'] += 1
        diffs = list(corpus.differences(corpus.normalized_rows(gui), corpus.normalized_rows(native), 'events'))
        report_diffs = list(corpus.differences(expected, actual, 'report'))
        classify(data, gui, native, expected, actual, root, diffs, report_diffs)
        by_path = {d['path']: d for d in report_diffs}
        self.assertEqual(by_path['report.isr.length']['classification'], 'loss-withheld')
        self.assertEqual(by_path['report.overflow']['classification'], 'unexplained')

    def test_context_statistics_need_independent_interval_equality(self):
        data = header() + packet(2, 15) + packet(3, delta=100) + packet(512)
        rows = self.rows(data)
        proof = corpus.context_accounting(packets(data, rows), rows)[0]['ISR 15']
        self.assertEqual(proof['runtimeTicks'], 100)
        self.assertEqual(proof['activationRows'], [2])
        self.assertEqual(proof['metrics']['total_ms'], 0.1)
        damaged = data + packet(1, 3)
        self.assertEqual(corpus.context_accounting(packets(damaged, self.rows(damaged)), self.rows(damaged))[0], {})
        duplicate = (header() + packet(9, 1, 1, text='worker') + packet(9, 2, 1, text='worker')
                     + packet(4, 1) + packet(4, 2) + packet(17))
        self.assertEqual(corpus.context_accounting(packets(duplicate, self.rows(duplicate)), self.rows(duplicate)), ({}, {}))

    def test_cpu_loss_proof_accounts_for_every_removed_interruption(self):
        data = (header() + packet(4, 1) + packet(2, 15) + packet(3, delta=20) + packet(1, 3)
                + packet(4, 1) + packet(7, 1, 4, delta=40) + packet(17))
        rows = self.rows(data)
        context = decode.Decoder().decode(data).context_rows()
        task = next(r for r in context if r['Name'] == 'Task 0x1')
        task['Time Interrupted'] = '0.020000 ms'
        tmp = tmpdir(self)
        root = Path(tmp)
        decode.write_csv(root / 'contexts.csv', decode.CONTEXT_COLUMNS, context)
        evidence = corpus.context_accounting(packets(data, rows), rows, root)[1]['Task 0x1']
        self.assertEqual(evidence['runtimeTicks'], 40)
        self.assertEqual(evidence['guiInterruptedTicks'], 20)
        self.assertEqual(evidence['retainedInterruptedTicks'], 0)
        self.assertEqual(evidence['removedSpans'], [{'startRow': 3, 'endRow': 4, 'ticks': 20,
                                                    'taskStartRow': 2, 'overflowRow': 5}])
        task['Time Interrupted'] = '0.021000 ms'
        decode.write_csv(root / 'contexts.csv', decode.CONTEXT_COLUMNS, context)
        self.assertEqual(corpus.context_accounting(packets(data, rows), rows, root)[1], {})

    def test_display_rounding_requires_an_exact_midpoint(self):
        data = header(freq=16_000_000) + packet(512) + packet(28, 512, delta=313)
        native = self.rows(data)
        self.assertEqual(native[-1]['detail'], 'Returns after 19.563 us')
        gui = copy.deepcopy(native)
        gui[-1]['detail'] = 'Returns after 19.562 us'
        proof = row_proofs(data, gui, native)[1][3, 'duration_us']
        self.assertEqual(proof['classification'], 'gui-export-artifact')
        self.assertEqual(proof['ticks'], 313)
        gui[-1]['detail'] = 'Returns after 19.561 us'
        self.assertFalse(row_proofs(data, gui, native)[1])

    def test_annotation_units_share_exact_rounding_rules(self):
        for detail, ticks in (('Runs for 19.563 us', 313), ('Runs for 1.000 ms', 16001),
                              ('Runs for 5.000 s', 80_000_001)):
            with self.subTest(detail=detail):
                self.assertTrue(corpus.annotation_matches(detail, ticks, 16_000_000))
                self.assertFalse(corpus.annotation_matches(detail, ticks + 16_000_000, 16_000_000))

    def test_csv_reader_rejects_missing_columns_and_truncated_rows(self):
        root = tmpdir(self)
        path = root / 'events.txt'
        path.write_text('event,detail\nStart,\n')
        with self.assertRaisesRegex(corpus.report.Usage, 'not a SystemView events export'):
            corpus.read_rows(path)
        path.write_text(','.join(decode.EVENT_COLUMNS) + '\n0,0,Idle\n')
        with self.assertRaisesRegex(corpus.report.Usage, 'line 2 is truncated'):
            corpus.read_rows(path)

    def test_cli_fails_on_unexplained_differences_and_accepts_proven_ones(self):
        tmp = tmpdir(self)
        root = Path(tmp)
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({'files': {'raw': {'sha256': 'hash'}}, 'captures': [{'name': 'case', 'raw': 'raw', 'gui': 'gui'}]}))
        for accepted in (True, False):
            result = {'rawRowsVerified': 1, 'nativeRows': 1, 'rowDiffCount': 1, 'equal': False,
                      'reportDiffs': [], 'fixedCount': 0, 'accepted': accepted,
                      'classifications': {key: int(key == ('gui-export-artifact' if accepted else 'unexplained'))
                                          for key in corpus.CLASSES}}
            with mock.patch.object(corpus, 'verify'), mock.patch.object(corpus, 'compare', return_value=result), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = corpus.main(['--root', str(root), '--manifest', str(manifest), '--out', str(root / str(accepted))])
            self.assertEqual(status, int(not accepted))

    def test_gui_warning_is_independent_of_other_differences(self):
        source = FIXTURES / 'baremetal'
        tmp = tmpdir(self)
        root = Path(tmp)
        raw, gui = root / 'raw', root / 'gui'
        gui.mkdir()
        raw.write_bytes(gzip.decompress((source / 'capture.SVDat.gz').read_bytes()))
        for name in ('events.txt', 'contexts.csv'):
            (gui / name).write_bytes((source / name).read_bytes())
        (gui / 'record.json').write_text(json.dumps({'systemviewErrors': 1}))
        out = root / 'out'
        out.mkdir()
        result = corpus.compare(raw, gui, out)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['classifications']['gui-warning'], 1)
        rows = corpus.read_rows(gui / 'events.txt')
        rows[2]['context'] = 'wrong context'
        decode.write_csv(gui / 'events.txt', decode.EVENT_COLUMNS, rows)
        result = corpus.compare(raw, gui, out)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['classifications']['gui-warning'], 1)

    def test_blocked_time_exception_requires_matching_os_evidence_and_a_null_value(self):
        source = FIXTURES / 'freertos'
        data = gzip.decompress((source / 'capture.SVDat.gz').read_bytes())
        gui = corpus.read_rows(source / 'events.txt')
        expected = corpus.json_report(source)
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(data)
        metadata = decode.decode_file(raw, tmp)
        native = corpus.read_rows(Path(tmp) / 'events.txt')
        actual = corpus.json_report(tmp)
        for valid in (True, False):
            meta = copy.deepcopy(metadata)
            if not valid:
                meta['withheldMetrics']['blockedTime'] = 'other'
            diffs = [d for d in corpus.differences(expected, actual, 'report') if d['path'].endswith('.blocked_ms')]
            classify(data, gui, native, expected, actual, source, [], diffs, meta)
            self.assertTrue(diffs)
            self.assertTrue(all(d['classification'] == ('withheld-undefined' if valid else 'unexplained')
                                for d in diffs))
        index = next(i for i, c in enumerate(expected['contexts']) if c['blocked_ms'] is not None)
        actual['contexts'][index]['blocked_ms'] = 1000.0
        diffs = list(corpus.differences(expected, actual, 'report'))
        classify(data, gui, native, expected, actual, source, [], diffs, metadata)
        self.assertEqual(diffs[0]['classification'], 'unexplained')


class Robustness(unittest.TestCase):
    def test_raw_clock_uses_wire_deltas_and_detects_gaps_independently(self):
        data = header() + packet(23, 2, 0x3f800000, delta=123) + packet(512, delta=456)
        rows = CorpusProofs.rows(data)
        with mock.patch.object(decode, 'stream', side_effect=AssertionError('decoder stream used')), \
                mock.patch.object(decode, 'packet_at', side_effect=AssertionError('decoder framing used')), \
                mock.patch.object(decode.Reader, 'uint', side_effect=AssertionError('decoder varint used')):
            stamps, ends, changes, gaps = corpus.raw_clock(data, [int(r['eventoffset']) for r in rows])
        self.assertEqual(stamps, [0, 0, 123, 579])
        self.assertEqual(ends[-1], len(data))
        self.assertEqual((changes, gaps), ([], []))
        damaged = header() + packet(512) + b'\xff' * 6 + header() + packet(512)
        rows = CorpusProofs.rows(damaged)
        self.assertEqual(corpus.raw_clock(damaged, [int(r['eventoffset']) for r in rows])[3], [3])

    def test_raw_frequency_boundary_requires_blank_suffix_and_retained_detail(self):
        data = header() + packet(512) + packet(28, 512) + packet(24, 2_000_000, 0, 0, 0) + packet(513)
        root = tmpdir(self)
        raw = root / 'raw'
        raw.write_bytes(data)
        metadata = decode.decode_file(raw, root)
        rows = corpus.read_rows(root / 'events.txt')
        result = corpus.json_report(root)
        parsed = packets(data, rows)
        proof = corpus.robustness_checks(data, parsed, rows, rows, metadata, result)
        self.assertTrue(proof['passed'], proof)
        self.assertEqual(proof['timingLossRows'], [4])
        rows[-1]['timestampint'] = '999'
        self.assertFalse(corpus.robustness_checks(data, parsed, rows, rows, metadata, result)['checks']['timestamps'])

    def test_framing_diagnostics_are_checked_against_padding_and_tail_bytes(self):
        data = header() + packet(512) + b'\0' + packet(28, 512) + packet(512)[:-1]
        root = tmpdir(self)
        raw = root / 'raw'
        raw.write_bytes(data)
        metadata = decode.decode_file(raw, root)
        rows = corpus.read_rows(root / 'events.txt')
        result = corpus.json_report(root)
        parsed = packets(data, rows)
        self.assertTrue(corpus.robustness_checks(data, parsed, rows, rows, metadata, result)['passed'])
        for index in (0, 1):
            with self.subTest(index=index):
                damaged = copy.deepcopy(metadata)
                del damaged['diagnostics'][index]
                proof = corpus.robustness_checks(data, parsed, rows, rows, damaged, result)
                self.assertFalse(proof['checks']['framingDiagnostics'])

    def test_robustness_mode_requires_a_verified_manifest_checksum(self):
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(header())
        with self.assertRaisesRegex(decode.DecodeError, 'verified manifest checksum'):
            corpus.compare(raw, FIXTURES / 'baremetal', tmp, mode='robustness')
        with self.assertRaisesRegex(decode.DecodeError, 'unknown comparison mode'):
            corpus.compare(raw, FIXTURES / 'baremetal', tmp, mode='ignore')

    def test_robustness_checks_reject_missing_rows_diagnostics_arguments_and_time(self):
        data = header() + packet(512) + packet(28, payload=b'\x80') + packet(513) + packet(28, 513)
        tmp = tmpdir(self)
        raw = Path(tmp) / 'raw'
        raw.write_bytes(data)
        metadata = decode.decode_file(raw, tmp)
        rows = corpus.read_rows(Path(tmp) / 'events.txt')
        report = corpus.json_report(tmp)
        self.assertTrue(corpus.robustness_checks(data, packets(data, rows), rows, rows, metadata, report)['passed'])
        for mutate, check in (
                (lambda r, m, p: r.pop(), 'packetOffsets'),
                (lambda r, m, p: m['diagnostics'].clear(), 'malformedDiagnostics'),
                (lambda r, m, p: r[3].update(event='Function #512'), 'malformedArgumentsWithheld'),
                (lambda r, m, p: r[2].update(timestampint=''), 'timestamps'),
                (lambda r, m, p: p.update(functions=[]), 'reportTimedPrefix')):
            with self.subTest(check=check):
                actual, meta, result = copy.deepcopy((rows, metadata, report))
                mutate(actual, meta, result)
                proof = corpus.robustness_checks(data, packets(data, rows), rows, actual, meta, result)
                self.assertFalse(proof['passed'])
                self.assertFalse(proof['checks'][check])

    def test_corrupt_differences_are_informational_only_after_robustness_passes(self):
        tmp = tmpdir(self)
        root = Path(tmp)
        raw = root / 'raw'
        data = header() + packet(512) + packet(28, payload=b'\x80') + packet(513) + packet(28, 513)
        raw.write_bytes(data)
        gui, out = root / 'gui', root / 'out'
        gui.mkdir()
        out.mkdir()
        decode.decode_file(raw, gui)
        rows = corpus.read_rows(gui / 'events.txt')
        rows[3]['context'] = 'GUI corrupt context'
        decode.write_csv(gui / 'events.txt', decode.EVENT_COLUMNS, rows)
        result = corpus.compare(raw, gui, out, mode='robustness', raw_sha256=hashlib.sha256(data).hexdigest())
        self.assertTrue(result['accepted'])
        self.assertFalse(result['equal'])
        self.assertGreater(result['classifications']['informational'], 0)
        with mock.patch.object(corpus, 'robustness_checks', return_value={'passed': False}):
            self.assertFalse(corpus.compare(raw, gui, out, mode='robustness', raw_sha256=hashlib.sha256(data).hexdigest())['accepted'])


if __name__ == '__main__':
    unittest.main()
