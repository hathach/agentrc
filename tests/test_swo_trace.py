"""Tests for the SWO scripts: swo_decode.py turns ITM/DWT packets into events and reports, keeping
segments apart at every loss; swo_capture.py checks clocks and ownership before any write, journals
every register it or the probe tool changes and restores them, also from a journal left by a killed
run. No probe, no hardware: a fake backend stands in for the target."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dec = load('swo_decode')
cap = load('swo_capture')


def setUpModule():
    guard_survivors(__name__)

# ---------------------------------------------------------------------------------------------------
# packet encoders (ARMv7-M Appendix D)
# ---------------------------------------------------------------------------------------------------

SYNC = b'\x00' * 5 + b'\x80'
OVERFLOW = b'\x70'
SS = {1: 1, 2: 2, 4: 3}


def stim(port, value, size=1):
    return bytes([port << 3 | SS[size]]) + value.to_bytes(size, 'little')


def hw(disc, value, size):
    return bytes([disc << 3 | 0x04 | SS[size]]) + value.to_bytes(size, 'little')


def exc(num, fn):
    return hw(1, num | fn << 12, 2)


def dval(cmp, value, write=True, size=4):
    return hw(16 + 2 * cmp + (1 if write else 0), value, size)


def lts(delta, tc=0):
    if tc == 0 and 0 < delta < 7:
        return bytes([delta << 4])
    out = []
    while True:
        out.append(delta & 0x7F)
        delta >>= 7
        if not delta:
            break
    return bytes([0xC0 | tc << 4] + [b | 0x80 for b in out[:-1]] + [out[-1]])


def decode(data, synced=True, **meta):
    meta = dict({'synced': synced, 'ts_clock_hz': None, 'ts_prescale': 1}, **meta)
    return dec.summarize(data, meta, None, 5)


class Packets(unittest.TestCase):
    def test_every_packet_kind_round_trips(self):
        data = (stim(1, 0x41) + stim(2, 0xA55A, 2) + stim(2, 7, 4) + exc(15, 1) + lts(3) + lts(1000, 3)
                + dval(0, 0x1234) + hw(2, 0x08000123, 4) + hw(2, 0, 1) + OVERFLOW + SYNC)
        kinds = [(p['kind'], p.get('value', p.get('delta', p.get('pc')))) for p in dec.packets(data, True)]
        self.assertEqual(kinds, [('stim', 0x41), ('stim', 0xA55A), ('stim', 7), ('exc', None), ('lts', 3),
                                 ('lts', 1000), ('dval', 0x1234), ('pc', 0x08000123), ('pc', None),
                                 ('overflow', None), ('sync', None)])

    def test_an_unsynced_stream_is_read_from_its_first_sync(self):
        pkts = list(dec.packets(b'\x12\x34' + SYNC + stim(0, 9), False))
        self.assertEqual([p['kind'] for p in pkts], ['unsynced', 'sync', 'stim'])
        self.assertEqual(pkts[0]['bytes'], 2)

    def test_an_unknown_header_skips_to_the_next_sync(self):
        data = stim(0, 1) + b'\x04\xff\xee' + SYNC + stim(0, 2)
        pkts = list(dec.packets(data, True))
        self.assertEqual([p['kind'] for p in pkts], ['stim', 'unknown', 'resync', 'stim'])
        self.assertEqual(pkts[1]['bytes'], 3)

    def test_a_cut_packet_is_truncated_not_guessed(self):
        self.assertEqual([p['kind'] for p in dec.packets(stim(0, 1) + b'\x0b\x01', True)], ['stim', 'truncated'])


class Events(unittest.TestCase):
    def test_a_source_the_capture_never_enabled_ends_a_segment(self):
        data = exc(15, 1) + hw(2, 0x08000000, 4) + exc(15, 2)
        s, evts = decode(data, sources=['exc'])
        self.assertEqual(s['integrity']['unexpected'], 1)
        self.assertEqual([e['seg'] for e in evts], [0, 1])

    def test_overflows_after_teardown_are_counted_apart(self):
        data = exc(15, 1) + OVERFLOW + exc(15, 2)
        end = len(exc(15, 1))
        s, _ = decode(data + OVERFLOW, teardown_byte=end + 1 + len(exc(15, 2)))
        self.assertEqual((s['integrity']['overflow'], s['integrity']['teardown_overflow']), (1, 1))

    def test_timestamps_attach_to_the_packets_before_them(self):
        _, evts = decode(exc(15, 1) + lts(5) + exc(15, 2) + lts(20, 1))
        self.assertEqual([(e['t'], e['tq']) for e in evts], [(5, 'sync'), (25, 'ts-delayed')])


class ExceptionReport(unittest.TestCase):
    def test_duration_and_self_time_of_a_nested_episode(self):
        data = (exc(71, 1) + lts(100, 0) + exc(61, 1) + lts(100, 0) + exc(61, 2) + lts(10, 0)
                + exc(71, 3) + lts(1, 0) + exc(71, 2) + lts(190, 0) + exc(0, 3) + lts(10, 0))
        s, _ = decode(data, ts_clock_hz=1_000_000)
        tim7, nested = s['exceptions']['IRQ55 (71)'], s['exceptions']['IRQ45 (61)']
        self.assertEqual(tim7['dur_us']['max'], 301.0)
        self.assertEqual(tim7['self_us']['max'], 291.0)
        self.assertEqual(nested['dur_us']['max'], 10.0)

    def test_delayed_timestamps_are_counted_not_timed(self):
        data = exc(15, 1) + lts(10, 0) + exc(15, 2) + lts(10, 1)
        s, _ = decode(data, ts_clock_hz=1_000_000)
        row = s['exceptions']['SysTick (15)']
        self.assertEqual((row['delayed'], 'dur_us' in row), (1, False))

    def test_an_overflow_never_pairs_an_episode_across_it(self):
        data = exc(15, 1) + lts(10) + OVERFLOW + exc(15, 2) + lts(10)
        s, _ = decode(data, ts_clock_hz=1_000_000)
        self.assertNotIn('dur_us', s['exceptions']['SysTick (15)'])
        self.assertNotIn('span_s', s)   # an overflow loses local timestamp time

    def test_a_timestamp_after_a_gap_is_never_given_to_packets_before_it(self):
        data = exc(15, 1) + lts(10) + exc(15, 2) + OVERFLOW + lts(100)
        s, evts = decode(data, ts_clock_hz=1_000_000)
        self.assertEqual([e['t'] for e in evts], [10, None])
        self.assertNotIn('dur_us', s['exceptions']['SysTick (15)'])

    def test_any_gap_withholds_the_span(self):
        s, _ = decode(exc(15, 1) + lts(10) + b'\x04\xff' + SYNC + exc(15, 2) + lts(10), ts_clock_hz=1_000_000)
        self.assertEqual(s['integrity']['unknown_bytes'], 2)
        self.assertNotIn('span_s', s)
        s, _ = decode(exc(15, 1) + lts(10) + exc(15, 2) + lts(10) + OVERFLOW + exc(15, 1) + lts(500),
                      ts_clock_hz=1_000_000, teardown_byte=10)
        self.assertEqual(s['span_s'], 1e-05)   # first to last timestamped packet, up to the teardown

    GTS2 = bytes([0xB4, 0x83, 0x80, 0x80, 0x00])   # TS[47:26] = 3

    @staticmethod
    def gts1(low, n, wrap=False):
        b = [(low >> (7 * i)) & 0x7F for i in range(n)]
        if n == 4:
            b[3] = (low >> 21) & 0x1F | (0x40 if wrap else 0)
        return bytes([0x94] + [x | 0x80 for x in b[:-1]] + [b[-1]])

    def test_compressed_global_timestamps_are_completed_and_timed_by_the_packet_they_follow(self):
        gts1 = self.gts1
        data = (exc(15, 1) + lts(100) + gts1(0x123456, 4, wrap=True) + self.GTS2 + exc(15, 2) + lts(100)
                + gts1(0x56 + 0x10, 1) + exc(15, 1) + lts(1000))
        s, evts = decode(data, ts_clock_hz=1_000_000)
        full = [e['gts'] for e in evts if e['kind'] == 'gts1']
        self.assertEqual(full, [3 << 26 | 0x123456, 3 << 26 | 0x123466])
        self.assertEqual(s['global_ts']['clock_hz'], round(0x10 * 1_000_000 / 100))   # not / 1000, the next one

    def test_global_timestamps_are_never_completed_across_a_gap_or_an_earlier_wrap(self):
        gts1 = self.gts1
        s, evts = decode(exc(15, 1) + lts(10) + gts1(0x100, 4, wrap=True) + OVERFLOW + self.GTS2, ts_clock_hz=1_000_000)
        self.assertIsNone(s['global_ts'])
        s, evts = decode(exc(15, 1) + lts(10) + gts1(0x3FFFFFF, 4, wrap=True) + exc(15, 2) + lts(10)
                         + gts1(0x1, 4, wrap=True) + self.GTS2, ts_clock_hz=1_000_000)
        self.assertEqual([e.get('gts') for e in evts if e['kind'] == 'gts1'], [None, 3 << 26 | 1])

    def test_a_periodic_packet_lost_without_an_overflow_is_counted(self):
        cyc = hw(0, 1 << 5, 1)
        data = cyc + lts(1) + cyc + lts(16384) + cyc + lts(32768) + cyc + lts(16384)
        s, _ = decode(data, ts_clock_hz=1_000_000, periods={'Cyc': 16384})
        self.assertEqual(s['integrity']['periodic_missed'], {'Cyc': 1})
        s, _ = decode(cyc + lts(1) + cyc + lts(64, 1) + cyc + lts(64), ts_clock_hz=1_000_000, periods={'Cyc': 64})
        self.assertEqual(s['integrity']['periodic_missed'], {'Cyc': 0})   # a delayed one was still received
        self.assertEqual(dec.periods({'pc_sample': [1, 15], 'cyc_event': [0, 3]}), {'pc': 16384, 'Cyc': 256})

    def test_profiling_counter_wraps_become_a_share_of_the_span(self):
        data = (hw(0, 1 << 2, 1) + lts(1000)) * 10 + hw(0, 1 << 5, 1) + lts(1000)   # 10 Sleep wraps, 1 Cyc
        s, _ = decode(data, ts_clock_hz=1_000_000)
        self.assertEqual(s['events'], {'Sleep': 10, 'Cyc': 1})
        self.assertEqual(s['event_shares'], {'Sleep': round(9 * 256 / 9000, 4)})   # between the first and last wrap
        s, _ = decode(hw(0, 1 << 2, 1) + lts(1000) + hw(0, 1 << 2, 1) + lts(256), ts_clock_hz=1_000_000)
        self.assertEqual(s['event_shares'], {'Sleep': 1.0})
        sleep = hw(0, 1 << 2, 1)
        s, _ = decode(sleep + lts(256) + sleep + lts(256, 1) + sleep + lts(256), ts_clock_hz=1_000_000)
        self.assertEqual(s['event_shares'], {'Sleep': 1.0})   # a delayed wrap between still counts

    def test_without_a_clock_counts_stay_and_durations_are_withheld(self):
        s, _ = decode(exc(15, 1) + lts(10) + exc(15, 2) + lts(10))
        self.assertEqual(s['timing'], 'no timestamp clock given')
        self.assertEqual(s['exceptions']['SysTick (15)']['enter'], 1)
        self.assertNotIn('dur_us', s['exceptions']['SysTick (15)'])


class DecodeCli(unittest.TestCase):
    def test_a_capture_dir_carries_its_start_clock_sources_and_teardown(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'swo.bin').write_bytes(b'\x01' + SYNC + stim(1, ord('h')) + stim(1, ord('i')) + lts(3))
            Path(d, 'capture.json').write_text(json.dumps({
                'stream': 'swo.bin', 'stream_start': 'unsynced', 'sources': ['stim'],
                'timestamps': {'clock_hz': 1000, 'clock_source': 'test', 'prescale': 1}}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(dec.main([d, '--json']), 0)
            s = json.loads(out.getvalue())
            self.assertEqual(s['text']['1']['text'], 'hi')
            self.assertEqual(s['integrity']['unsynced_bytes'], 1)

    def test_a_raw_file_needs_its_start_stated(self):
        with tempfile.NamedTemporaryFile(suffix='.bin') as f, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dec.main(['--bin', f.name]), 2)


# ---------------------------------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------------------------------

def args(**kw):
    a = dict(traceclk_hz=168_000_000, swo_hz=1_500_000, allow_approx_swo=False, clock_source='test',
             duration_ms=10, exc=True, pc_sample=None, cyc_event=None, profile=[], cycmatch=None, global_ts=None,
             itm_ports=None, timestamps=False, core_hz=None, ts_prescale=1, watch=[], setup=None, observe_itm=False,
             take_itm=False, take_comparator=None,
             elf=None, elf_sha256=None, image_regions=None)
    a.update(kw)
    return argparse.Namespace(**a)


class FakeTarget:
    """A Cortex-M4 behind a probe tool: memory as a dict, read-only status bits, and a probe tool that
    programs the TPIU and ITM at start the way JLinkGDBServer does. `fail` makes the stream fail after the
    probe tool configured the target: 'open' (no stream) or 'eof' (closed at once)."""
    name = 'fake'
    attach_writes = None
    start_itm_tcr = 0x1000D
    family = None
    gate = None
    VECTORS = (0x08000000, bytes.fromhex('0000022001010008'))

    def __init__(self, regs=None):
        self.mem = {cap.CPUID: 0x410FC241, cap.DEMCR: cap.DEMCR_TRCENA, cap.DWT_CTRL: 0x40000000,
                    cap.ITM_TCR: 0, cap.ITM_TER: 0, 0xE000ED08: 0x08000000, 0x08000000: 0x20020000,
                    0x08000004: 0x08000101, cap.TPIU_ACPR: 0, cap.TPIU_SPPR: 1, cap.TPIU_FFCR: 0x102,
                    cap.TPIU_CSPSR: 1}
        self.mem.update(regs or {})
        self.writes = []
        self.stream = None
        self.acpr = None   # what the probe tool programs, None: the exact prescaler
        self.fail = None
        self.fail_read = None   # an address whose next read fails once

    def identity(self):
        return {'backend': 'fake', 'probe': '1'}

    def read32(self, addr, target=None):   # a target names a register behind another access port
        if addr == self.fail_read:
            self.fail_read = None
            raise cap.ToolError(f'read 0x{addr:08x}: E31')
        v = self.mem.get((target, addr) if target else addr, 0)
        if cap.DWT_COMP + 8 <= addr <= cap.DWT_COMP + 0x38 and v & 0xF:
            v |= 1 << 24   # MATCHED
        return v

    def read_block(self, addr, n):
        words = b''.join(self.read32(a).to_bytes(4, 'little') for a in range(addr & ~3, addr + n, 4))
        return words[addr & 3:(addr & 3) + n]

    def write32(self, addr, value, target=None):
        self.writes.append((addr, value))
        self.mem[(target, addr) if target else addr] = value

    def probe_rates(self):
        return None

    def ap_target(self, ap):
        return f'ap{ap}'

    def trace_unit_regs(self):
        return [cap.TPIU_CSPSR, cap.TPIU_ACPR, cap.TPIU_SPPR, cap.TPIU_FFCR]

    def start_swo(self, traceclk, swo_hz, mask):
        self.mem[cap.TPIU_ACPR] = self.acpr if self.acpr is not None else traceclk // swo_hz - 1
        self.mem[cap.TPIU_SPPR], self.mem[cap.TPIU_FFCR] = 2, 0x100
        self.mem[cap.ITM_TCR], self.mem[cap.ITM_TER] = 0x1000D, mask or 1
        if self.fail == 'open':
            raise cap.ToolError('port 1 did not open within 10 s')
        ours, self.stream = socket.socketpair()
        self.stream.sendall(SYNC + exc(15, 1) + exc(15, 2))
        if self.fail == 'eof':
            self.stream.close()
        return ours

    def stop_swo(self, mask):
        self.mem[cap.ITM_TCR] = self.mem[cap.ITM_TER] = 0
        self.stream.close()


class FakeRA(FakeTarget):
    """An RA6M5: PRCR and TRCKCR at their widths, a PRCR write ignored without its key, and a TPIU access while
    TRCKCR.TRCKEN is 0 recorded as a stall."""
    device = 'R7FA6M5BH'
    PRCR, TRCKCR, P109PFS = 0x4001E3FE, 0x4001E03F, 0x40080864

    def __init__(self, regs=None):
        super().__init__({cap.CPUID: 0x410FD214, self.PRCR: 0, self.TRCKCR: 0x01, self.P109PFS: 0x10400,
                          **(regs or {})})
        self.gate = cap.trace_gate(self.device)
        self.stalled = []

    def stall(self, addr):
        if cap.TPIU <= addr < cap.TPIU + 0x1000 and not self.gate_open():
            self.stalled.append(addr)
            raise cap.ToolError('stalled')

    def read32(self, addr, target=None):
        self.stall(addr)
        return super().read32(addr, target)

    def write32(self, addr, value, target=None):
        self.stall(addr)
        super().write32(addr, value, target)

    def read_sized(self, addr, bits):
        return self.mem.get(addr, 0)

    def write_sized(self, addr, value, bits):
        self.writes.append((addr, value))
        if addr == self.PRCR and value >> 8 != 0xA5:
            return
        self.mem[addr] = value & 0xFF if addr == self.PRCR else value

    def gate_open(self):
        return bool(self.mem[self.TRCKCR] & 0x80)


class Rates(unittest.TestCase):
    def test_an_exact_divisor_passes(self):
        self.assertEqual(cap.check_rates(args(), None)[:2], (112, 1_500_000.0))

    def test_an_inexact_rate_is_refused_with_the_exact_ones_nearby(self):
        with self.assertRaisesRegex(cap.Refused, r'does not divide.*1500000'):
            cap.check_rates(args(swo_hz=1_510_000), None)

    def test_approx_records_the_actual_rate(self):
        prescaler, actual, notes = cap.check_rates(args(swo_hz=2_000_000, allow_approx_swo=True), None)
        self.assertEqual((prescaler, actual), (84, 2_000_000.0))
        prescaler, actual, notes = cap.check_rates(args(swo_hz=1_510_000, allow_approx_swo=True), None)
        self.assertEqual(prescaler, 111)
        self.assertIn('from the requested', notes[0])

    def test_a_jlink_rate_must_be_its_base_over_n(self):
        with self.assertRaisesRegex(cap.Refused, r'4500000 Hz / n'):
            cap.check_rates(args(swo_hz=2_000_000), (4_500_000, 1))
        self.assertEqual(cap.check_rates(args(), (4_500_000, 1))[0], 112)


class Watch(unittest.TestCase):
    def test_v7_encodings_include_read_only_and_write_only(self):
        self.assertEqual(cap.parse_watch('addr=0x20000000,access=w,emit=val', None)['function'], 0b1101)
        self.assertEqual(cap.parse_watch('addr=0x20000000,access=r,emit=val', None)['function'], 0b1100)
        self.assertEqual(cap.parse_watch('addr=0x20000000,access=rw,emit=offset', None)['function'], 0b100001)

    def test_a_pc_alone_on_one_direction_has_no_encoding(self):
        with self.assertRaisesRegex(ValueError, 'no ARMv7-M data trace encoding'):
            cap.parse_watch('addr=0x20000000,access=w,emit=pc', None)

    def test_a_symbol_needs_an_elf_and_one_data_object(self):
        with self.assertRaisesRegex(ValueError, 'needs --elf'):
            cap.parse_watch('addr=var,access=w,emit=val', None)
        syms = [('var', 0x20000010, 4, 1, 3), ('var', 0x20000020, 4, 1, 3)]
        with self.assertRaisesRegex(ValueError, '2 data objects'):
            cap.parse_watch('addr=var,access=w,emit=val', syms)
        w = cap.parse_watch('addr=var,access=w,emit=val', syms[:1])
        self.assertEqual(w['addr'], 0x20000010)

    def test_the_address_is_aligned_to_the_mask(self):
        with self.assertRaisesRegex(ValueError, 'aligned'):
            cap.parse_watch('addr=0x20000002,access=w,emit=val,mask=2', None)

    def test_ports(self):
        self.assertEqual(cap.parse_ports('0,2-3'), 0b1101)
        with self.assertRaises(ValueError):
            cap.parse_ports('32')


class Ownership(unittest.TestCase):
    def before(self, regs=None):
        b = {cap.ITM_TCR: 0, cap.ITM_TER: 0, cap.DWT_CTRL: 0x40000000}
        b.update({cap.DWT_COMP + 16 * n + 8: 0 for n in range(4)})
        b.update(regs or {})
        return b

    def test_an_itm_already_on_is_refused_without_an_override(self):
        busy = self.before({cap.ITM_TCR: 0x10009, cap.ITM_TER: 1})
        with self.assertRaisesRegex(cap.Refused, 'already on'):
            cap.plan_config(args(), {'numcomp': 4}, busy)
        tcr, ter, _, _ = cap.plan_config(args(take_itm=True, itm_ports='1'), {'numcomp': 4}, busy)
        self.assertEqual((tcr & 0xF, ter), (0b1101, 0b10))
        tcr, ter, _, _ = cap.plan_config(args(observe_itm=True), {'numcomp': 4}, busy)
        self.assertEqual((tcr, ter), (0x10009, 1))

    def test_an_itm_the_probe_tool_set_at_connect_is_not_someone_elses(self):
        busy = self.before({cap.ITM_TCR: 0x10009, cap.ITM_TER: 1})
        cap.plan_config(args(), {'numcomp': 4}, busy, itm_set_by_probe=True)

    def test_dwt_sources_already_on_are_refused(self):
        with self.assertRaisesRegex(cap.Refused, 'already on'):
            cap.plan_config(args(), {'numcomp': 4}, self.before({cap.DWT_CTRL: 0x40010001}))

    def test_taking_the_itm_drops_sources_the_capture_did_not_ask_for(self):
        busy = self.before({cap.DWT_CTRL: 0x40001001 | 1 << 10})   # PC sampling on
        _, _, steps, _ = cap.plan_config(args(take_itm=True), {'numcomp': 4}, busy)
        self.assertEqual(steps[-1] & cap.CTRL_TRACE_SOURCES, cap.CTRL_EXCTRCENA)

    def test_pc_sampling_writes_its_period_before_enabling(self):
        _, _, steps, _ = cap.plan_config(args(exc=False, pc_sample=(1, 15)), {'numcomp': 4}, self.before())
        self.assertEqual(len(steps), 2)
        self.assertFalse(steps[0] & cap.CTRL_PCSAMPLENA)
        self.assertEqual((steps[0] >> 5 & 0xF, steps[0] >> 1 & 0xF), (15, 15))   # POSTINIT, POSTPRESET
        self.assertEqual(steps[1], steps[0] | cap.CTRL_PCSAMPLENA)

    def test_cycle_events_and_profiling_counters(self):
        _, _, steps, _ = cap.plan_config(args(exc=False, cyc_event=(0, 3), profile=['sleep', 'cpi']),
                                         {'numcomp': 4}, self.before())
        self.assertFalse(steps[0] & (cap.CTRL_CYCEVTENA | cap.CTRL_PCSAMPLENA))
        self.assertEqual(steps[1], steps[0] | cap.CTRL_CYCEVTENA)
        self.assertEqual(steps[1] & (0x1F << 17), 1 << 19 | 1 << 17)
        with self.assertRaisesRegex(cap.Refused, 'NOPRFCNT'):
            cap.plan_config(args(profile=['cpi']), {'numcomp': 4, 'dwt_ctrl': cap.CTRL_NOPRFCNT}, self.before())

    def test_cycmatch_takes_comparator_0_and_watches_the_rest(self):
        w = cap.parse_watch('addr=0x20000000,access=w,emit=val', None)
        _, _, _, watches = cap.plan_config(args(cycmatch=0x1000, watch=[w]), {'numcomp': 4}, self.before())
        self.assertEqual([(x['comparator'], x['function'], x['addr']) for x in watches],
                         [(0, 0x81, 0x1000), (1, 0xD, 0x20000000)])
        with self.assertRaisesRegex(cap.Refused, 'needs comparator 0'):
            cap.plan_config(args(cycmatch=0), {'numcomp': 4}, self.before({cap.DWT_COMP + 8: 0x5}))
        with self.assertRaisesRegex(cap.Refused, 'NOCYCCNT'):
            cap.plan_config(args(cycmatch=0), {'numcomp': 4, 'dwt_ctrl': 1 << 25}, self.before())

    def test_a_comparator_in_use_is_taken_only_by_name(self):
        w = cap.parse_watch('addr=0x20000000,access=w,emit=val', None)
        used = self.before({cap.DWT_COMP + 8: 0x5})
        _, _, _, watches = cap.plan_config(args(watch=[w]), {'numcomp': 1}, self.before())
        self.assertEqual(watches[0]['comparator'], 0)
        with self.assertRaisesRegex(cap.Refused, 'free data trace comparators'):
            cap.plan_config(args(watch=[w]), {'numcomp': 1}, used)
        _, _, _, watches = cap.plan_config(args(watch=[w], take_comparator=0), {'numcomp': 1}, used)
        self.assertEqual(watches[0]['comparator'], 0)


class Capture(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        patcher = mock.patch.object(cap, 'DRAIN_S', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_capture(self, target, **kw):
        return cap.capture(args(**kw), target, self.dir)

    def journal(self):
        return cap.load_journal(os.path.join(self.dir, 'journal.jsonl'))

    def session(self, t, **header):
        image = {'regions': [[FakeTarget.VECTORS[0], 8]], 'sha256': cap.read_image(t, [[FakeTarget.VECTORS[0], 8]])}
        j = cap.Journal(os.path.join(self.dir, 'journal.jsonl'),
                        dict({'identity': t.identity(), 'host': socket.gethostname(), 'cpuid': t.mem[cap.CPUID],
                              'image': image}, **header))
        return cap.Session(t, j)

    def test_a_capture_restores_every_register_it_or_the_probe_tool_changed(self):
        t = FakeTarget()
        before = dict(t.mem)
        w = cap.parse_watch('addr=0x20000000,access=w,emit=val', None)
        record, failures = self.run_capture(t, watch=[w], itm_ports='1,2', timestamps=True, core_hz=168_000_000)
        self.assertEqual(failures, [])
        self.assertEqual(record['restore'], 'complete')
        self.assertEqual({k: t.mem.get(k, 0) for k in before}, before)
        self.assertEqual(t.mem[cap.DWT_COMP + 8], 0)
        self.assertEqual(record['stream_start'], 'unsynced')   # the probe tool turned ITM on before we read
        self.assertEqual(record['clocks']['acpr_readback'], 111)
        self.assertEqual(record['sources'], ['stim', 'exc', 'dpc', 'daddr', 'dval'])
        names = [e['name'] for e in self.journal()[1]]
        self.assertIn('TPIU_ACPR', names)   # the probe tool's own change, journalled

    def test_teardown_stops_every_source_at_once_before_the_probe_stops(self):
        t = FakeTarget()
        self.run_capture(t)
        tcr_writes = [v for a, v in t.writes if a == cap.ITM_TCR]
        quiesce = next(v for v in tcr_writes if v and not v & (cap.TCR_ITMENA | cap.TCR_TXENA))
        self.assertTrue(quiesce)

    def test_a_stream_that_never_opens_still_restores_what_the_probe_tool_set(self):
        t = FakeTarget()
        before = dict(t.mem)
        t.fail = 'open'
        record, failures = self.run_capture(t)
        self.assertRegex(failures[0], 'did not open')
        self.assertEqual({k: t.mem.get(k, 0) for k in before}, before)
        tcr = [e for e in self.journal()[1] if e['name'] == 'ITM_TCR']
        self.assertEqual((tcr[0]['kind'], tcr[0]['readback']), ('start_swo', 0x1000D))

    def test_a_stream_the_probe_tool_closes_fails_the_capture(self):
        t = FakeTarget()
        t.fail = 'eof'
        record, failures = self.run_capture(t, duration_ms=2000)
        self.assertRegex(failures[-1], 'closed the SWO stream after 12 bytes')
        self.assertEqual((record['errors'], record['restore']), (failures, 'complete'))

    def test_a_taken_comparator_is_disarmed_while_its_address_changes(self):
        f0 = cap.DWT_COMP + 8
        t = FakeTarget({cap.DWT_COMP: 0x20001000, f0: 0x5})
        w = cap.parse_watch('addr=0x20000000,access=w,emit=val', None)
        record, failures = self.run_capture(t, watch=[w], take_comparator=0)
        self.assertEqual(failures, [])
        function = 0x5
        for addr, value in t.writes:
            if addr in (cap.DWT_COMP, cap.DWT_COMP + 4):
                self.assertEqual(function & 0xF, 0, f'0x{addr:08x} written while comparator 0 armed')
            elif addr == f0:
                function = value
        self.assertEqual((t.mem[cap.DWT_COMP], t.mem[f0] & 0xFF), (0x20001000, 0x5))

    def test_the_elf_must_be_what_the_target_runs(self):
        t = FakeTarget()
        addr, image = FakeTarget.VECTORS
        record, failures = self.run_capture(t, elf='fw.elf', image_regions=[(addr, image)])
        self.assertEqual(self.journal()[0]['image']['regions'], [[addr, 8]])
        t = FakeTarget({0x08000004: 0x08000199})
        with self.assertRaisesRegex(cap.Refused, 'differ at 0x08000004'):
            cap.capture(args(elf='fw.elf', image_regions=[(addr, image)]), t, self.dir)
        self.assertEqual(t.writes, [])

    def test_the_firmware_is_identified_by_its_read_only_sections_not_segment_padding(self):
        body = b'A' * 8 + bytes(8) + b'BBBB' + b'CCCC'   # .text, padding, .rodata, then .data (written at boot)
        ph = [struct.pack('<8I', 1, 0x100, 0x08000000, 0x08000000, 0x14, 0x14, 5, 4),
              struct.pack('<8I', 1, 0x114, 0x20000000, 0x08000014, 4, 4, 6, 4)]
        sh = [bytes(40)] + [struct.pack('<10I', 0, 1, flags, addr, off, size, 0, 0, 4, 0) for flags, addr, off, size in
                            ((6, 0x08000000, 0x100, 8), (2, 0x08000010, 0x110, 4), (3, 0x20000000, 0x114, 4))]
        head = b'\x7fELF\x01\x01\x01' + bytes(9) + struct.pack('<HHIIIIIHHHHHH', 2, 40, 1, 0, 52, 0x120, 0, 52, 32, 2,
                                                                 40, 4, 0)
        elf = head + b''.join(ph)
        elf = (elf.ljust(0x100, b'\0') + body).ljust(0x120, b'\0') + b''.join(sh)
        path = os.path.join(self.dir, 'fw.elf')
        Path(path).write_bytes(elf)
        self.assertEqual(cap.image_regions(path), [(0x08000000, b'A' * 8), (0x08000010, b'BBBB')])

    def test_a_prescaler_the_probe_tool_rounded_fails_the_capture_and_restores(self):
        t = FakeTarget()
        before = dict(t.mem)
        t.acpr = 110
        record, failures = self.run_capture(t)
        self.assertRegex(failures[0], 'prescaler to 111')
        self.assertEqual({k: t.mem.get(k, 0) for k in before}, before)

    def test_a_setup_entry_is_checked_and_its_writes_journalled(self):
        t = FakeTarget({0xE0042004: 0x7, 0x40020400: 0x280})
        setup = self.setup('stm32f4')
        record, failures = self.run_capture(t, setup=setup)
        self.assertEqual(failures, [])
        self.assertIn((0xE0042004, 0x27), t.writes)
        self.assertEqual(t.mem[0xE0042004], 0x7)
        t = FakeTarget({0x5C001004: 0x3F, 0x5C004000: 0x303, 0x58020400: 0xFFFFFEBF})
        with self.assertRaisesRegex(cap.Refused, 'DBGMCU_CR is 0x0000003f, needs 0x00700000 under mask'):
            cap.capture(args(setup=self.setup('stm32h7')), t, self.dir)
        t = FakeTarget({0xE0042004: 0x7})
        with self.assertRaisesRegex(cap.Refused, r'GPIOB_MODER is 0x00000000 \(a peripheral reads 0 while its clock'):
            cap.capture(args(setup=self.setup('stm32f4')), t, self.dir)

    def setup(self, key):
        p = argparse.ArgumentParser()
        with mock.patch.object(p, 'error', side_effect=AssertionError):
            return cap.load_setup(key, p)

    def test_global_timestamps_start_the_generator_behind_its_access_port(self):
        t = FakeTarget({0x5C001004: 0x0070003F, 0x5C004000: 0x303, 0x58020400: 0xFFFFFEBD})
        record, failures = self.run_capture(t, setup=self.setup('stm32h7'), global_ts='8192', take_itm=True)
        self.assertEqual(failures, [])
        self.assertIn((0xE00E5000, 1), t.writes)
        self.assertEqual(t.mem[('ap2', 0xE00E5000)], 0)   # restored
        tcr = next(e for e in self.journal()[1] if e['name'] == 'ITM_TCR' and e['kind'] == 'write')
        self.assertEqual(tcr['intended'] >> 10 & 3, 2)
        t = FakeTarget({0x5C001004: 0x0070003F, 0x5C004000: 0x303, 0x58020400: 0xFFFFFEBD})
        os.remove(os.path.join(self.dir, 'journal.jsonl'))
        record, failures = self.run_capture(t, setup=self.setup('stm32h7'))
        self.assertNotIn(0xE00E5000, [a for a, _ in t.writes])   # only for --global-ts

    def test_ra_starts_the_trace_clock_before_any_tpiu_access_and_stops_it_last(self):
        t = FakeRA()
        record, failures = self.run_capture(t, setup=self.setup('ra6m5'))
        self.assertEqual((failures, record['restore'], t.stalled), ([], 'complete', []))
        self.assertEqual(t.writes[:2], [(FakeRA.PRCR, 0xA501), (FakeRA.TRCKCR, 0x81)])
        self.assertEqual(t.writes[-2:], [(FakeRA.TRCKCR, 0x01), (FakeRA.PRCR, 0xA500)])
        self.assertEqual((t.mem[FakeRA.TRCKCR], t.mem[FakeRA.PRCR]), (0x01, 0))
        prcr = next(e for e in self.journal()[1] if e['name'] == 'PRCR')
        self.assertEqual((prcr['bits'], prcr['key']), (16, 0xA500))

    def test_ra_without_its_setup_or_observing_is_refused_untouched(self):
        t = FakeRA()
        with self.assertRaisesRegex(cap.Refused, r'R7FA6M5BH: its TPIU stalls.*--setup ra6m5'):
            self.run_capture(t)
        t.mem.update({cap.ITM_TCR: 0x1000D, cap.ITM_TER: 1})
        with self.assertRaisesRegex(cap.Refused, 'TRCKCR stops the trace clock, so no SWO runs to observe'):
            self.run_capture(t, setup=self.setup('ra6m5'), observe_itm=True, exc=False)
        self.assertEqual((t.writes, t.stalled), ([], []))

    def test_ra_caps_leaves_a_stopped_tpiu_alone(self):
        t = FakeRA()
        self.assertRegex(cap.caps_report(t)['tpiu'], r'^not read: TRCKCR stops the trace clock.*--setup ra6m5')
        self.assertEqual(t.stalled, [])
        t.mem[FakeRA.TRCKCR] = 0x81
        t.mem.update({cap.TPIU_CIDR + 4 * i: b for i, b in enumerate((0x0D, 0x90, 0x05, 0xB1))})
        self.assertEqual(cap.caps_report(t)['tpiu']['at'], '0xe0040000')

    def test_restore_from_a_killed_run_writes_sized_registers_at_their_width_with_their_key(self):
        t = FakeRA()
        self.setup('ra6m5')
        s = self.session(t)
        s.write(FakeRA.PRCR, 0x1)
        s.write(FakeRA.TRCKCR, 0x81)
        with mock.patch.dict(cap.SIZED, clear=True):   # a new process knows the widths from the journal alone
            report, problems = cap.restore_from(t, self.dir)
        self.assertEqual((problems, t.mem[FakeRA.TRCKCR], t.mem[FakeRA.PRCR]), ([], 0x01, 0))
        self.assertEqual(t.writes[-2:], [(FakeRA.TRCKCR, 0x01), (FakeRA.PRCR, 0xA500)])

    def test_ra_recovery_unlocks_prcr_for_a_trckcr_the_capture_failed_to_restore(self):
        t = FakeRA()
        write, failed = t.write_sized, []

        def fail_once(addr, value, bits):
            if addr == FakeRA.TRCKCR and value == 0x01 and not failed:
                failed.append(addr)
                raise cap.ToolError('memU8: E31')
            write(addr, value, bits)
        t.write_sized = fail_once
        record, failures = self.run_capture(t, setup=self.setup('ra6m5'), elf=None)
        self.assertRegex(record['restore'][0], 'TRCKCR: memU8: E31')
        self.assertEqual((t.mem[FakeRA.TRCKCR], t.mem[FakeRA.PRCR]), (0x81, 0))   # PRCR relocked, TRCKCR stuck
        header = self.journal()[0]
        with open(os.path.join(self.dir, 'journal.jsonl')) as f:   # give the run an image to restore against
            lines = f.read().splitlines()
        regions = [[FakeTarget.VECTORS[0], 8]]
        header.update(image={'regions': regions, 'sha256': cap.read_image(t, regions)})
        with open(os.path.join(self.dir, 'journal.jsonl'), 'w') as f:
            f.write('\n'.join([json.dumps(header)] + lines[1:]) + '\n')
        report, problems = cap.restore_from(t, self.dir)
        self.assertEqual((problems, t.stalled), ([], []))
        self.assertEqual((t.mem[FakeRA.TRCKCR], t.mem[FakeRA.PRCR]), (0x01, 0))
        self.assertEqual(t.writes[-3:], [(FakeRA.PRCR, 0xA501), (FakeRA.TRCKCR, 0x01), (FakeRA.PRCR, 0xA500)])
        report, problems = cap.restore_from(t, self.dir)   # all settled, the trace clock stopped: no TPIU access
        self.assertEqual((problems, t.stalled), ([], []))
        self.assertIn('TPIU_ACPR: restored earlier; not read, the trace clock is stopped', report)

    def test_ra_recovery_leaves_a_prerequisite_someone_else_changed(self):
        t = FakeRA()
        self.setup('ra6m5')
        s = self.session(t)
        s.write(FakeRA.PRCR, 0x1)
        s.write(FakeRA.TRCKCR, 0x81, requires={'reg': FakeRA.PRCR, 'target': None, 'mask': 1, 'value': 1})
        t.mem[FakeRA.PRCR] = 0
        cap.Journal.restored(s.j, {'reg': FakeRA.PRCR, 'target': None}, 0)   # the capture relocked it
        t.mem[FakeRA.PRCR] = 0x2   # then firmware set PRC1
        writes = len(t.writes)
        report, problems = cap.restore_from(t, self.dir)
        self.assertRegex(problems[0], 'TRCKCR: needs PRCR, which is 0x00000002.*both left alone')
        self.assertEqual((t.writes[writes:], t.mem[FakeRA.TRCKCR]), ([], 0x81))
        t.mem[FakeRA.PRCR] = 0x2   # the same foreign prerequisite meets the capture's own restore
        writes = len(t.writes)
        failures = s.restore()
        self.assertRegex(failures[0], 'TRCKCR: needs PRCR, which is 0x00000002.*both left alone')
        self.assertEqual((t.writes[writes:], t.mem[FakeRA.PRCR], t.mem[FakeRA.TRCKCR]), ([], 0x2, 0x81))

    def test_observing_a_running_swo_keeps_its_rate(self):
        t = FakeTarget({cap.ITM_TCR: 0x1000D, cap.ITM_TER: 1, cap.TPIU_SPPR: 2, cap.TPIU_ACPR: 55})
        with self.assertRaisesRegex(cap.Refused, 'already runs NRZ at prescaler 56'):
            self.run_capture(t, observe_itm=True)
        self.assertEqual(t.writes, [])
        record, failures = self.run_capture(t, observe_itm=True, swo_hz=3_000_000)
        self.assertEqual(failures, [])
        # only the restore after the probe tool's stop cleared it
        self.assertEqual([v for a, v in t.writes if a == cap.ITM_TCR], [0x1000D])

    def test_observing_refuses_an_itm_the_probe_start_would_change(self):
        t = FakeTarget({cap.ITM_TCR: 0x1020F, cap.ITM_TER: 1})
        with self.assertRaisesRegex(cap.Refused, 'sets ITM_TCR to 0x0001000d.*0x0001020f'):
            self.run_capture(t, observe_itm=True, exc=False)
        self.assertEqual(t.writes, [])

    def test_refusals_come_before_any_write(self):
        t = FakeTarget({cap.DEMCR: 0})
        with self.assertRaisesRegex(cap.Refused, 'TRCENA'):
            self.run_capture(t)
        t = FakeTarget({cap.CPUID: 0x410CC601})
        with self.assertRaisesRegex(cap.Refused, 'Cortex-M0\\+'):
            self.run_capture(t)
        self.assertEqual(t.writes, [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'journal.jsonl')))

    def test_restore_from_a_killed_run_touches_only_what_the_journal_explains(self):
        t = FakeTarget()
        s = self.session(t)
        s.write(cap.ITM_TCR, 0x1000F)
        s.write(cap.DWT_CTRL, 0x40010401)
        s.write(cap.DWT_COMP, 0x20000000)
        t.mem[cap.DWT_COMP] = 0x20000100   # changed since by someone else
        report, problems = cap.restore_from(t, self.dir)
        self.assertEqual((t.mem[cap.ITM_TCR], t.mem[cap.DWT_CTRL]), (0, 0x40000000))
        self.assertEqual(t.mem[cap.DWT_COMP], 0x20000100)
        self.assertRegex(problems[0], 'comparator 0: DWT_COMP0 0x20000100 neither.*left alone')
        report, problems = cap.restore_from(t, self.dir)
        self.assertIn('ITM_TCR: already 0x00000000', report)

    def test_restore_after_a_restore_follows_what_changed_since(self):
        t = FakeTarget({cap.ITM_TCR: 0x10009})
        s = self.session(t)
        with s.probe_op([cap.ITM_TCR], 'start_swo'):
            t.mem[cap.ITM_TCR] = 0x1000D
        s.write(cap.ITM_TCR, 0x1000F)
        self.assertEqual(s.restore(), [])
        with s.probe_op([cap.ITM_TCR], 'stop_swo'):
            t.mem[cap.ITM_TCR] = 0   # killed before the final restore
        report, problems = cap.restore_from(t, self.dir)
        self.assertEqual((t.mem[cap.ITM_TCR], problems), (0x10009, []))
        t.mem[cap.ITM_TCR] = 0x1000F   # a value the capture wrote, but before its last restore
        cap.Journal.restored(s.j, {'reg': cap.ITM_TCR, 'target': None}, 0x10009)
        report, problems = cap.restore_from(t, self.dir)
        self.assertRegex(problems[0], 'ITM_TCR: 0x0001000f is neither')

    def test_a_failed_read_in_teardown_still_restores_the_rest(self):
        t = FakeTarget()
        before = dict(t.mem)
        read, failed = t.read32, []

        def fail_in_teardown(addr, target=None):   # once, at quiesce's first read: teardown has begun
            if cap.CLEANING.is_set() and addr == cap.ITM_TCR and not failed:
                failed.append(addr)
                raise cap.ToolError(f'read 0x{addr:08x}: E31')
            return read(addr, target)
        t.read32 = fail_in_teardown
        record, failures = self.run_capture(t, duration_ms=50)
        self.assertRegex(record['errors'][0], 'stop: read 0xe0000e80: E31')
        self.assertEqual(record['restore'], 'complete')
        self.assertEqual({k: t.mem.get(k, 0) for k in before}, before)

    def test_restore_leaves_a_comparator_someone_else_changed_whole(self):
        f0 = cap.DWT_COMP + 8
        for change in ({f0: 0x6}, {cap.DWT_COMP: 0x20000300}):
            with self.subTest(change=change):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(self.dir, 'journal.jsonl'))
                t = FakeTarget({cap.DWT_COMP: 0x20001000, f0: 0x5})
                s = self.session(t)
                s.baseline([cap.DWT_COMP, cap.DWT_COMP + 4, f0])
                s.write(f0, 0)
                s.write(cap.DWT_COMP, 0x20000000)
                s.write(f0, 0xD)
                t.mem.update(change)
                t.writes.clear()
                report, problems = cap.restore_from(t, self.dir)
                self.assertEqual([a for a, _ in t.writes if cap.comparator_of(a) == 0], [])
                self.assertRegex(problems[0], 'comparator 0: .*left alone')

    def test_a_comparator_cut_short_after_its_disarm_is_recovered_only_when_whole(self):
        f0 = cap.DWT_COMP + 8
        journal = os.path.join(self.dir, 'journal.jsonl')
        for baseline, change, rearmed in ((True, {}, True), (True, {cap.DWT_COMP: 0x20002000}, False),
                                          (False, {cap.DWT_COMP + 4: 4}, False)):
            with self.subTest(baseline=baseline, change=change):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(journal)
                t = FakeTarget({cap.DWT_COMP: 0x20001000, f0: 0x5})
                s = self.session(t)
                if baseline:
                    s.baseline([cap.DWT_COMP, cap.DWT_COMP + 4, f0])
                s.write(f0, 0)   # killed here
                t.mem.update(change)
                report, problems = cap.restore_from(t, self.dir)
                self.assertEqual(t.mem[f0] & 0xF, 0x5 if rearmed else 0)
                self.assertEqual(problems == [], rearmed)

    def test_a_restore_settles_what_it_verified(self):
        t = FakeTarget()
        s = self.session(t)
        s.write(cap.ITM_TCR, 0x1000F)
        self.assertEqual(cap.restore_from(t, self.dir)[1], [])
        t.mem[cap.ITM_TCR] = 0x1000F   # a new owner, after the restore
        report, problems = cap.restore_from(t, self.dir)
        self.assertEqual(t.mem[cap.ITM_TCR], 0x1000F)
        self.assertRegex(problems[0], 'ITM_TCR: 0x0001000f is neither')

    def test_restore_disarms_a_comparator_before_restoring_its_address(self):
        f0 = cap.DWT_COMP + 8
        t = FakeTarget({cap.DWT_COMP: 0x20001000, f0: 0x5})
        s = self.session(t)
        s.baseline([cap.DWT_COMP, cap.DWT_COMP + 4, f0])
        s.write(f0, 0)
        s.write(cap.DWT_COMP, 0x20000000)
        s.write(f0, 0xD)
        t.writes.clear()
        cap.restore_from(t, self.dir)
        self.assertEqual([(a, v & 0xFF if a == f0 else v) for a, v in t.writes],
                         [(f0, 0), (cap.DWT_COMP, 0x20001000), (f0, 0x5)])

    def test_restore_refuses_another_host_target_or_firmware(self):
        t = FakeTarget()
        self.session(t)
        t.mem[0x08000004] = 0x08000999
        with self.assertRaisesRegex(cap.Refused, 'no longer runs the firmware'):
            cap.restore_from(t, self.dir)
        os.remove(os.path.join(self.dir, 'journal.jsonl'))
        self.session(t, host='elsewhere')
        with self.assertRaisesRegex(cap.Refused, 'host elsewhere'):
            cap.restore_from(t, self.dir)
        os.remove(os.path.join(self.dir, 'journal.jsonl'))
        self.session(t, image=None)
        with self.assertRaisesRegex(cap.Refused, 'without --elf'):
            cap.restore_from(t, self.dir)

    def test_restore_returns_each_register_to_its_first_original(self):
        t = FakeTarget({cap.ITM_TCR: 0x5})
        s = cap.Session(t, cap.Journal(os.path.join(self.dir, 'j.jsonl'), {}))
        with s.probe_op([cap.ITM_TCR], 'start_swo'):
            t.mem[cap.ITM_TCR] = 0
        s.write(cap.ITM_TCR, 0x1000F)
        self.assertEqual(s.restore(), [])
        self.assertEqual(t.mem[cap.ITM_TCR], 0x5)

    def test_read_only_status_bits_do_not_fail_a_read_back(self):
        t = FakeTarget({cap.DWT_COMP + 0x18: 0x200})   # comparator 1 reads LNK1ENA
        s = cap.Session(t, cap.Journal(os.path.join(self.dir, 'j.jsonl'), {}))
        s.write(cap.DWT_COMP + 8, 0xD)   # reads back with MATCHED
        t.write32 = lambda addr, value, w=t.write32: w(addr, value | (0x200 if addr == cap.DWT_COMP + 0x18 else 0))
        s.write(cap.DWT_COMP + 0x18, 0)
        self.assertEqual(s.restore(), [])


class Transports(unittest.TestCase):
    def serve(self, replies):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)

        def run():
            for reply in replies:
                buf = b''
                while not (b'#' in buf and len(buf) >= buf.index(b'#') + 3):
                    buf += theirs.recv(4096)
                for body in reply:
                    theirs.sendall(f'+${body}#{sum(body.encode()) & 0xFF:02x}'.encode())
        threading.Thread(target=run, daemon=True).start()
        return ours

    def test_rsp_memory_and_a_multi_packet_monitor_reply(self):
        rsp = cap.Rsp(self.serve([['78563412'], ['O' + 'Base frequency: 4500000Hz, '.encode().hex(),
                                                 'O' + 'MinDiv: 1\n'.encode().hex(), 'OK']]))
        self.assertEqual(rsp.read32(0xE0001000), 0x12345678)
        self.assertEqual(rsp.monitor('SWO GetSpeedInfo'), 'Base frequency: 4500000Hz, MinDiv: 1')

    def test_a_jlink_never_touches_a_tpiu_whose_clock_is_stopped(self):
        j = cap.JLinkBackend('1', 'R7FA6M5BH', 'swd', '4000')
        j.rsp = mock.Mock()
        j.rsp.monitor.return_value = 'Reading from address 0x4001E03F (Data = 0x01)'
        with self.assertRaisesRegex(cap.ToolError, 'TRCKCR stops the trace clock'):
            j.read32(cap.TPIU_ACPR)
        j.rsp.read32.assert_not_called()
        j.rsp.monitor.assert_called_with('memU8 0x4001e03f')
        j.rsp.monitor.return_value = 'Reading from address 0x4001E03F (Data = 0x81)'
        j.rsp.read32.return_value = 5
        self.assertEqual(j.read32(cap.TPIU_ACPR), 5)
        self.assertIsNone(cap.JLinkBackend('1', 'STM32F407VG', 'swd', '4000').gate)

    def test_an_openocd_command_with_an_empty_result_succeeds(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        o = cap.OpenOCDBackend('1', '-f interface/stlink.cfg', None, None)
        o.sock, o.buf = ours, b''
        theirs.sendall(b'OK:\x1aERR:no such object\x1a')
        self.assertEqual(o._checked('x configure -pin-freq 1'), '')
        with self.assertRaisesRegex(cap.ToolError, 'no such object'):
            o._checked('y')

    def test_the_adapter_serial_goes_after_the_interface_file(self):
        o = cap.OpenOCDBackend('ABC', '-f interface/stlink-dap.cfg -f target/stm32h7x.cfg', None, None)
        self.assertEqual(o._cfg_with(['-c', 'adapter serial ABC']),
                         ['-f', 'interface/stlink-dap.cfg', '-c', 'adapter serial ABC', '-f', 'target/stm32h7x.cfg'])


class CaptureCli(unittest.TestCase):
    def error(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            cap.parse_args(argv)
        self.assertEqual(ctx.exception.code, 2)
        return err.getvalue()

    JLINK = ['--probe', '1', '--device', 'STM32F407VG', '--interface', 'swd', '--speed', '4000']

    def test_clocks_are_never_defaulted(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn('--traceclk-hz (no default)',
                          self.error(self.JLINK + ['--out', d + '/x', '--swo-hz', '1', '--exc']))

    def test_pc_sampling_and_cycle_events_are_one_at_a_time(self):
        with tempfile.TemporaryDirectory() as d:
            base = self.JLINK + ['--out', d + '/x', '--traceclk-hz', '1', '--swo-hz', '1', '--clock-source', 'x',
                                 '--protocol', 'uart', '--duration-ms', '1']
            self.assertIn('share the POSTCNT timer', self.error(base + ['--pc-sample', '0:1', '--cyc-event', '0:1']))
            self.assertIn('counters are cpi,exc,sleep,lsu,fold', self.error(base + ['--profile', 'sleep,ipc']))

    def test_a_refused_runs_probe_log_does_not_block_its_out_dir(self):
        with tempfile.TemporaryDirectory() as d:
            argv = self.JLINK + ['--out', d, '--traceclk-hz', '1', '--swo-hz', '1', '--clock-source', 'x',
                                 '--protocol', 'uart', '--duration-ms', '1', '--exc']
            open(os.path.join(d, 'probe.log'), 'w').close()
            self.assertEqual(cap.parse_args(argv)[0].out, d)
            open(os.path.join(d, 'swo.bin'), 'w').close()
            self.assertIn("holds more than a refused run's probe.log", self.error(argv))

    def test_connection_parameters_are_checked_before_any_tool_starts(self):
        self.assertIn("--speed '4 MHz': kHz", self.error(self.JLINK[:-1] + ['4 MHz', '--caps']))
        self.assertIn('complete 0xVVVV 0xPPPP pairs', self.error(['--probe', '1', '--cfg', '-f interface/stlink.cfg',
                                                                  '--vid-pid', '0x0483', '--caps',
                                                                  '--accept-attach-writes']))

    def test_caps_stands_alone(self):
        self.assertIn('capture flags need --out', self.error(self.JLINK + ['--caps', '--exc']))

    def test_a_setup_the_route_cannot_drive_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.error(self.JLINK + ['--out', d + '/x', '--traceclk-hz', '1', '--swo-hz', '1', '--clock-source',
                                           'x', '--protocol', 'uart', '--duration-ms', '1', '--exc',
                                           '--setup', 'stm32h7'])
        self.assertIn('jlink unsupported', out)

    def test_openocd_needs_its_attach_writes_accepted_and_cannot_observe(self):
        oocd = ['--probe', '1', '--cfg', '-f interface/stlink-dap.cfg', '--caps']
        self.assertIn('--accept-attach-writes connects anyway', self.error(oocd))
        a, b = cap.parse_args(oocd + ['--accept-attach-writes'])
        self.assertEqual(b.name, 'openocd')
        self.assertIn('a jlink connect writes nothing', self.error(self.JLINK + ['--caps', '--accept-attach-writes']))
        with tempfile.TemporaryDirectory() as d:
            out = self.error(oocd[:-1] + ['--accept-attach-writes', '--out', d + '/x', '--observe-itm'])
        self.assertIn("would not be the firmware's", out)

    def test_an_unknown_setup_lists_the_entries(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.error(self.JLINK + ['--out', d + '/x', '--setup', 'nope'])
        self.assertIn('stm32f4, stm32h7', out)


class HilRoster(unittest.TestCase):
    def roster(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name, 'hil.json')
        path.write_text(json.dumps({'boards': [
            {'name': 'f407', 'flasher': {'name': 'jlink', 'uid': '773', 'args': '-device STM32F407VG'}},
            {'name': 'h743', 'flasher': {'name': 'stlink', 'uid': '004C'}}]}))
        return str(path)

    def test_a_jlink_entry_fills_probe_and_device_and_a_given_flag_must_agree(self):
        hil = self.roster()
        a, b = cap.parse_args(['--hil-config', hil, '--board', 'f407', '--interface', 'swd', '--speed', '4000',
                               '--caps'])
        self.assertEqual((b.probe, b.device), ('773', 'STM32F407VG'))
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            cap.parse_args(['--hil-config', hil, '--board', 'f407', '--probe', '999', '--interface', 'swd',
                            '--speed', '4000', '--caps'])
        self.assertIn("contradicts --board f407", err.getvalue())

    def test_an_stlink_entry_needs_the_callers_openocd_configuration(self):
        hil = self.roster()
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            cap.parse_args(['--hil-config', hil, '--board', 'h743', '--caps'])
        self.assertIn('give --cfg', err.getvalue())
        a, b = cap.parse_args(['--hil-config', hil, '--board', 'h743', '--cfg', '-f interface/stlink-dap.cfg',
                               '--accept-attach-writes', '--caps'])
        self.assertEqual((b.probe, b.cfg), ('004C', '-f interface/stlink-dap.cfg'))


class BoardEntries(unittest.TestCase):
    def test_every_entry_names_its_registers_and_evidence(self):
        entries = json.loads((SCRIPTS / 'swo_boards.json').read_text())['entries']
        for key, e in entries.items():
            for w in e['writes']:
                self.assertTrue({'name', 'addr', 'mask', 'value', 'why', 'ref'} <= set(w), key)
                self.assertFalse(int(w['value'], 0) & ~int(w['mask'], 0), key)
            for w in e['watch']:
                self.assertTrue({'name', 'addr', 'mask', 'value', 'why', 'ref'} <= set(w), key)
                self.assertFalse(int(w['value'], 0) & ~int(w['mask'], 0), key)
            for w in e['writes'] + e['watch']:
                self.assertIn(w.get('bits', 32), (8, 16, 32), key)
            if 'jlink_device' in e:
                self.assertEqual(sum(1 for w in e['writes'] if w.get('gates_trace_unit')), 1, key)


if __name__ == '__main__':
    unittest.main()
