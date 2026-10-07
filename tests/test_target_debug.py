"""Tests for the target-debug skill's pc_sample.py: DWT_PCSR samples read through
a fake JLinkExe on PATH (prompt-prefixed output like the real tool) are
histogrammed by function through a fake symbolizer; an incomplete capture, a
missing or broken tool, and a symbolizer answering the wrong number of lines
are explicit failures, never a partial result passed off as a profile.

probe_state.py through fake JLinkExe and openocd: the plumbing only; its hardware
behaviour is verified on a rig board."""
import importlib.util
import hashlib
import json
import struct
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts' / 'pc_sample.py'
_spec = importlib.util.spec_from_file_location('pc_sample', SCRIPT)
pc_sample = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc_sample)

# Answers each mem32 with "J-Link><addr> = <value>" (the real Commander echoes its
# prompt on the same line). FAKE_PCSR: comma-separated PCSR values in order, the
# last one repeated; FAKE_DHCSR: the two DHCSR values; FAKE_DEMCR: the DEMCR
# answer lines, comma-separated (default one, TRCENA set); FAKE_MODE=truncate stops
# after half the reads, FAKE_MODE=hang never exits.
FAKE_JLINK = '''#!/usr/bin/env python3
import os, sys, time
pcsr = os.environ.get('FAKE_PCSR', '20000100').split(',')
dhcsr = os.environ.get('FAKE_DHCSR', '00010001,00010001').split(',')
mode = os.environ.get('FAKE_MODE', '')
if mode == 'hang':
    time.sleep(60)
print('SEGGER J-Link Commander V9.66 (Compiled fake)')
n_pc = n_dh = 0
lines = sys.stdin.read().splitlines()
total = sum(1 for l in lines if l.startswith('mem32'))
done = 0
for line in lines:
    if line.startswith('mem32 E000101C'):
        v = pcsr[min(n_pc, len(pcsr) - 1)]; n_pc += 1
        print(f'J-Link>E000101C = {v}')
    elif line.startswith('mem32 E000EDF0'):
        v = dhcsr[min(n_dh, len(dhcsr) - 1)]; n_dh += 1
        print(f'J-Link>E000EDF0 = {v}')
    elif line.startswith('mem32 E000EDFC'):
        for v in os.environ.get('FAKE_DEMCR', '01000000').split(','):
            print(f'J-Link>E000EDFC = {v}')
    elif line.startswith('Sleep'):
        print('J-Link>Sleep(' + line.split()[1] + ')')
    elif line == 'qc':
        break
    done += 1
    if mode == 'truncate' and done > total // 2:
        print('Could not read memory.')
        sys.exit(0)
if mode == 'dies_after_reads':
    print('USB communication error: probe disconnected')
    sys.exit(1)
'''

# addr2line -f: two lines per address, function then file:line. FAKE_SYMS maps
# "pc=func@loc"; unknown PCs answer ??. FAKE_SYM_MODE=short drops the last line,
# =fail exits 1, =hang sleeps.
FAKE_ADDR2LINE = '''#!/usr/bin/env python3
import os, sys, time
mode = os.environ.get('FAKE_SYM_MODE', '')
if mode == 'hang':
    time.sleep(60)
if mode == 'fail':
    print('fake addr2line: bad ELF', file=sys.stderr); sys.exit(1)
syms = dict(kv.split('=') for kv in os.environ.get('FAKE_SYMS', '').split(';') if kv)
out = []
for a in sys.argv:
    if a.startswith('0x'):
        func, _, loc = syms.get(a.lower(), '??@??:0').partition('@')
        out += [func, loc]
if mode == 'short':
    out = out[:-1]
print('\\n'.join(out))
'''


LINK = ('--interface', 'swd', '--speed', '4000')


class ParseTest(unittest.TestCase):
    def test_prompt_prefixed_and_bare_lines_both_parse(self):
        text = ('J-Link>E000EDFC = 01000000\nJ-Link>E000EDF0 = 00010001\nE000101C = 08001234\n'
                'J-Link>E000101C = FFFFFFFF\nJ-Link>Sleep(5)\nJ-Link>E000EDF0 = 02030003\n')
        self.assertEqual(pc_sample.parse_reads(text),
                         ([0x08001234, 0xFFFFFFFF], [0x00010001, 0x02030003], [0x01000000]))

    def test_script_reads_demcr_then_dhcsr_around_the_samples(self):
        s = pc_sample.jlink_script(2, 5).splitlines()
        self.assertEqual(s, ['mem32 E000EDFC, 1', 'mem32 E000EDF0, 1', 'mem32 E000101C, 1', 'Sleep 5',
                             'mem32 E000101C, 1', 'Sleep 5', 'mem32 E000EDF0, 1', 'qc'])
        self.assertNotIn('Sleep 0', pc_sample.jlink_script(1, 0))


@unittest.skipIf(os.name == 'nt', 'J-Link and addr2line stubs require POSIX executable semantics')
class CliTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        d = Path(self._dir.name)
        for name, body in (('JLinkExe', FAKE_JLINK), ('fake-addr2line', FAKE_ADDR2LINE)):
            f = d / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.elf = d / 'fw.elf'
        self.elf.write_bytes(b'\x7fELF')
        self.env = {**os.environ, 'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}'}

    def run_cli(self, *args, **env):
        e = {**self.env, **env}
        return subprocess.run([sys.executable, str(SCRIPT), '--probe', '000', '--device', 'FAKE', *LINK,
                               '--elf', str(self.elf), '--addr2line', 'fake-addr2line', *args],
                              capture_output=True, text=True, timeout=30, env=e, cwd='/')

    def test_histogram_symbols_and_counts(self):
        r = self.run_cli('--samples', '6', '--raw', str(self.elf.parent / 'raw.txt'),
                         FAKE_PCSR='08001000,08001000,08002000,FFFFFFFF,00000000,08003000',
                         FAKE_DHCSR='00010001,00010001',
                         FAKE_SYMS='0x08001000=spin_here@main.c:42;0x08002000=tud_task@usbd.c:7')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('4 usable of 6 samples', r.stdout)
        self.assertIn('0x08001000  main.c:42', r.stdout)
        self.assertIn('0x08002000  usbd.c:7', r.stdout)
        self.assertRegex(r.stdout, r'\n     1  25.0%  \?\?\n +1    0x08003000  \n')
        self.assertIn('sentinel 0xFFFFFFFF (halted or WFI): 1', r.stdout)
        self.assertIn('no PCSR (0): 1', r.stdout)
        self.assertIn('unresolved symbols: 1', r.stdout)
        self.assertIn('DHCSR before: 0x00010001 (running)', r.stdout)
        self.assertIn('DHCSR after: 0x00010001 (running)', r.stdout)
        self.assertEqual((self.elf.parent / 'raw.txt').read_text().splitlines(),
                         ['08001000', '08001000', '08002000', 'ffffffff', '00000000', '08003000'])
        # the top entry is the spin function and comes first, with its share and site
        lines = r.stdout.splitlines()
        self.assertTrue(lines[1].startswith('     2  50.0%  spin_here'), lines[1])
        self.assertIn('0x08001000  main.c:42', lines[2])

    def test_histogram_aggregates_pcs_by_function(self):
        # four sites in busy() outrank two samples at one idle() site
        r = self.run_cli('--samples', '6', '--top', '1',
                         FAKE_PCSR='08000010,08000014,08000018,0800001c,08000100,08000100',
                         FAKE_SYMS='0x08000010=busy@b.c:1;0x08000014=busy@b.c:2;0x08000018=busy@b.c:3;'
                                   '0x0800001c=busy@b.c:4;0x08000100=idle@i.c:9')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[1].startswith('     4  66.7%  busy'), lines[1])
        self.assertNotIn('idle', r.stdout)

    def test_demangled_overloads_stay_distinct(self):
        r = self.run_cli('--samples', '3', FAKE_PCSR='08000010,08000010,08000020',
                         FAKE_SYMS='0x08000010=work(int, int)@w.cpp:1;0x08000020=work(int, float)@w.cpp:9')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[1].startswith('     2  66.7%  work(int, int)'), lines[1])
        self.assertTrue(lines[3].startswith('     1  33.3%  work(int, float)'), lines[3])

    def test_only_sentinels_is_a_failure_with_the_dhcsr_context(self):
        r = self.run_cli('--samples', '3', FAKE_PCSR='FFFFFFFF', FAKE_DHCSR='00010001,02030003')
        self.assertEqual(r.returncode, 1)
        self.assertIn('0 usable of 3 samples', r.stdout)
        self.assertIn('DHCSR after: 0x02030003 (halted, reset since last read)', r.stdout)
        self.assertIn('no usable sample', r.stderr)

    def test_interval_emits_sleeps(self):
        r = self.run_cli('--samples', '2', '--interval-ms', '7', FAKE_SYMS='0x20000100=loop@main.c:1')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_trace_disabled_refuses_the_histogram(self):
        raw = self.elf.parent / 'raw.txt'
        r = self.run_cli('--samples', '3', '--raw', str(raw), FAKE_DEMCR='000F0001', FAKE_PCSR='08001000,08002000',
                         FAKE_SYM_MODE='fail')
        self.assertEqual(r.returncode, 1)
        self.assertIn('DEMCR.TRCENA (DWTENA on ARMv6-M) is 0 (DEMCR=0x000f0001): DWT_PCSR samples are not valid '
                      'while trace is disabled', r.stderr)
        self.assertNotIn('addr2line', r.stderr)
        self.assertEqual(r.stdout, '')
        self.assertFalse(raw.exists())

    def test_other_demcr_bits_do_not_matter(self):
        r = self.run_cli('--samples', '1', FAKE_DEMCR='010F07F1', FAKE_SYMS='0x20000100=loop@main.c:1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 usable of 1 samples', r.stdout)

    def test_a_demcr_read_missing_twice_or_malformed_is_no_capture(self):
        raw = self.elf.parent / 'raw.txt'
        for demcr, n in (('', 0), ('zz', 0), ('0100000', 0), ('01000000,01000000', 2)):
            r = self.run_cli('--samples', '2', '--raw', str(raw), FAKE_DEMCR=demcr, FAKE_SYM_MODE='fail')
            self.assertEqual(r.returncode, 1, demcr)
            self.assertIn(f'incomplete capture: 2/2 samples, 2/2 DHCSR reads, {n}/1 DEMCR reads', r.stderr, demcr)
            self.assertFalse(raw.exists(), demcr)

    def test_incomplete_capture_is_an_explicit_failure(self):
        r = self.run_cli('--samples', '10', FAKE_MODE='truncate')
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stderr, r'incomplete capture: \d+/10 samples, 1/2 DHCSR reads')
        self.assertIn('Could not read memory', r.stderr)

    def test_a_commander_that_fails_after_answering_is_no_capture(self):
        r = self.run_cli('--samples', '3', FAKE_MODE='dies_after_reads', FAKE_SYMS='0x20000100=loop@main.c:1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('JLinkExe exited 1', r.stderr)
        self.assertIn('USB communication error', r.stderr)
        self.assertNotIn('usable of', r.stdout)

    def test_blank_selectors_never_reach_the_commander(self):
        for flag in ('--probe', '--device'):
            r = subprocess.run([sys.executable, str(SCRIPT), '--probe', '000', '--device', 'FAKE', *LINK,
                                '--elf', str(self.elf), f'{flag}=  '], capture_output=True, text=True,
                               timeout=30, env={**self.env, 'FAKE_MODE': 'hang'})
            self.assertEqual(r.returncode, 2, flag)
            self.assertIn('must not be empty', r.stderr)

    def test_missing_or_hanging_jlink(self):
        r = self.run_cli(PATH='/nonexistent')
        self.assertEqual(r.returncode, 1)
        self.assertIn('JLinkExe not found - install J-Link Commander or set PC_SAMPLE_JLINK_EXE', r.stderr)
        r = self.run_cli('--samples', '1', '--timeout', '1', FAKE_MODE='hang')
        self.assertEqual(r.returncode, 1)
        self.assertIn('JLinkExe did not finish within 1 s', r.stderr)

    def test_symbolizer_failures_are_bounded_and_explicit(self):
        for mode, want in (('fail', 'fake-addr2line failed: fake addr2line: bad ELF'),
                           ('short', 'returned 1 lines for 1 addresses'),
                           ('hang', 'fake-addr2line did not finish within 1 s')):
            r = self.run_cli('--samples', '1', '--timeout', '1', FAKE_SYM_MODE=mode)
            self.assertEqual(r.returncode, 1, mode)
            self.assertIn(want, r.stderr, mode)
        r = self.run_cli('--samples', '1', '--addr2line', 'no-such-addr2line')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no-such-addr2line not on PATH', r.stderr)

    def test_the_link_and_the_commander_come_from_the_caller(self):
        base = [sys.executable, str(SCRIPT), '--probe', '000', '--device', 'X', '--elf', str(self.elf)]
        for given in ((), ('--interface', 'swd'), ('--speed', '4000')):
            r = subprocess.run([*base, *given], capture_output=True, text=True, timeout=30, env=self.env)
            self.assertEqual(r.returncode, 2, given)
        for bad in ('0', 'fast', '4000kHz'):
            r = subprocess.run([*base, '--interface', 'swd', f'--speed={bad}'], capture_output=True, text=True,
                               timeout=30, env=self.env)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn('--speed must be kHz', r.stderr)
        d = Path(self._dir.name)
        argv = d / 'argv'
        other = d / 'commander'
        other.write_text(f'#!{sys.executable}\nimport sys\nopen({str(argv)!r}, "w").write(" ".join(sys.argv[1:]))\n')
        other.chmod(0o755)
        r = subprocess.run([*base, '--interface', 'jtag', '--speed', 'adaptive', '--samples', '1'],
                           capture_output=True, text=True, timeout=30,
                           env={**self.env, 'PC_SAMPLE_JLINK_EXE': str(other)})
        self.assertIn('incomplete capture: 0/1 samples', r.stderr)      # the override ran, and said nothing
        self.assertIn('-if jtag -speed adaptive', argv.read_text())

    def test_raw_never_overwrites(self):
        raw = Path(self._dir.name) / 'pcs.txt'
        raw.write_text('earlier run')
        r = self.run_cli('--samples', '1', '--raw', str(raw))
        self.assertEqual(r.returncode, 2)
        self.assertIn('already exists', r.stderr)
        self.assertEqual(raw.read_text(), 'earlier run')

    def test_argument_refusals(self):
        for args in (('--samples', '0'), ('--interval-ms', '-1'), ('--top', '0'),
                     ('--timeout', '0'), ('--timeout', '-1'), ('--timeout', 'nan'), ('--timeout', 'inf')):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 2, args)
        r = subprocess.run([sys.executable, str(SCRIPT), '--probe', '000', '--device', 'X', *LINK,
                            '--elf', '/nonexistent.elf'], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn('ELF not found', r.stderr)
        r = subprocess.run([sys.executable, str(SCRIPT), '--help'], capture_output=True, text=True, timeout=30, cwd='/')
        self.assertEqual(r.returncode, 0)
        self.assertIn('--interval-ms', r.stdout)



PROBE_STATE = SCRIPT.parent / 'probe_state.py'
_spec = importlib.util.spec_from_file_location('probe_state', PROBE_STATE)
probe_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_state)

# Shared by both fakes: FAKE_<REG> answers each read of that register, a comma list
# consumed in order with the last value repeated; FAKE_MEM/FAKE_MEM_BASE is the flash
# the read-back sees; FAKE_LOG records argv and the script; FAKE_MODE=truncate stops
# answering after the CPUID read, =fail exits 1; FAKE_HALT_DHCSR answers DHCSR in a
# J-Link halt-read session; FAKE_REPLACE names a file the read-back overwrites.
FAKE_COMMON = """
import json, os, sys
regs = {k: os.environ.get('FAKE_' + k, d).split(',') for k, d in
        (('CPUID', '410fc241'), ('DHCSR', '01010001'), ('DEMCR', '01000000'), ('PCSR', '08001000'), ('PC', '08004444'))}
seen = {}
def value(reg):
    n = seen.get(reg, 0); seen[reg] = n + 1
    vals = regs[reg]
    return int(vals[min(n, len(vals) - 1)], 16)
def dump(path, addr, size):
    mem = open(os.environ['FAKE_MEM'], 'rb').read() if os.environ.get('FAKE_MEM') else b''
    off = addr - int(os.environ.get('FAKE_MEM_BASE', '0'), 16)
    open(path, 'wb').write(mem[off:off + size])
    if os.environ.get('FAKE_REPLACE'):
        open(os.environ['FAKE_REPLACE'], 'wb').write(b'replaced while the probe read back')
mode = os.environ.get('FAKE_MODE', '')
"""

FAKE_JLINK_STATE = '#!/usr/bin/env python3' + FAKE_COMMON + """
import re
script = sys.stdin.read()
open(os.environ['FAKE_LOG'], 'w').write(json.dumps({'argv': sys.argv[1:], 'script': script}))
if '\\nh\\n' in script and os.environ.get('FAKE_HALT_DHCSR'):
    regs['DHCSR'] = os.environ['FAKE_HALT_DHCSR'].split(',')
names = {'E000ED00': 'CPUID', 'E000EDF0': 'DHCSR', 'E000EDFC': 'DEMCR', 'E000101C': 'PCSR'}
print('SEGGER J-Link Commander V9.78 (Compiled fake)')
for line in script.splitlines():
    m = re.match(r'mem32 ([0-9A-F]{8}), 1', line)
    if m:
        print(f'J-Link>{m.group(1)} = {value(names[m.group(1)]):08X}')
        if mode == 'truncate':
            break
    m = re.match(r'savebin (\\S+), 0x([0-9A-F]+), 0x([0-9A-F]+)', line)
    if m:
        dump(m.group(1), int(m.group(2), 16), int(m.group(3), 16))
    if line == 'h':
        print(f'PC = {value("PC"):08X}, CycleCnt = 00000000')
sys.exit(1 if mode == 'fail' else 0)
"""

FAKE_OPENOCD_STATE = '#!/usr/bin/env python3' + FAKE_COMMON + """
import re
script = sys.argv[-1]
open(os.environ['FAKE_LOG'], 'w').write(json.dumps({'argv': sys.argv[1:], 'script': script}))
for line in script.splitlines():
    m = re.match(r'\\s*(?:rd |echo \\[format "R )(\\w+) ', line)
    if m:
        print(f'R {m.group(1)} 0x{value(m.group(1)):08x}', file=sys.stderr)
        if mode == 'truncate':
            break
    m = re.match(r'dump_image \\{(\\S+)\\} 0x([0-9a-f]+) 0x([0-9a-f]+)', line)
    if m:
        dump(m.group(1), int(m.group(2), 16), int(m.group(3), 16))
if mode == 'tclerror':
    print('ERROR read_memory: failed to read memory', file=sys.stderr)
sys.exit(1 if mode == 'fail' else 0)
"""

STATE_BOARDS = [
    {'name': 'k64f', 'flasher': {'name': 'jlink', 'uid': '000621000000', 'args': '-device MK64FN1M0xxx12'}},
    {'name': 'max32', 'flasher': {'name': 'openocd', 'uid': 'E6614C311B597D32', 'vid_pid': '0x2e8a 0x000c',
                                  'args': '-f interface/cmsis-dap.cfg -f target/max32665.cfg'}},
    {'name': 'h743', 'flasher': {'name': 'stlink', 'uid': '004C00343137510F39383538'}},
    {'name': 'p4', 'flasher': {'name': 'esptool', 'uid': '4ea4f48f', 'args': '-b 1500000'}},
    {'name': 'no_uid', 'flasher': {'name': 'jlink', 'args': '-device X'}},
    {'name': 'no_dev', 'flasher': {'name': 'jlink', 'uid': '1'}},
    {'name': 'no_cfg', 'flasher': {'name': 'openocd', 'uid': '1'}},
    {'name': 'bad_ids', 'flasher': {'name': 'openocd', 'uid': '1', 'vid_pid': '2e8a:000c', 'args': '-f x.cfg'}},
    {'name': 'twin', 'flasher': {'name': 'jlink', 'uid': '1', 'args': '-device X'}},
    {'name': 'twin', 'flasher': {'name': 'jlink', 'uid': '2', 'args': '-device X'}},
]


def elf32(segments):
    """A minimal little-endian ELF32 with one PT_LOAD per (paddr, bytes, filesz-or-None)."""
    phoff, phentsize = 52, 32
    data_off = phoff + phentsize * len(segments)
    header = bytearray(b'\x7fELF\x01\x01\x01' + bytes(9))
    header += struct.pack('<HHIIIIIHHHHHH', 2, 40, 1, 0, phoff, 0, 0, 52, phentsize, len(segments), 0, 0, 0)
    phdrs, blobs = b'', b''
    for paddr, blob in segments:
        phdrs += struct.pack('<8I', 1, data_off + len(blobs), paddr | 0x1000_0000, paddr, len(blob), len(blob), 5, 4)
        blobs += blob
    return bytes(header) + phdrs + blobs


class ProbeStateUnitTest(unittest.TestCase):
    def test_dhcsr_bits(self):
        self.assertEqual(probe_state.dhcsr_state(0x03030003),
                         {'raw': '0x03030003', 'halted': True, 'sleeping': False, 'lockup': False,
                          'retiredSinceLastRead': True, 'resetSinceLastRead': True})
        self.assertTrue(probe_state.dhcsr_state(1 << 18)['sleeping'])
        self.assertTrue(probe_state.dhcsr_state(1 << 19)['lockup'])

    def test_cpuid_architecture(self):
        self.assertEqual(probe_state.cpuid_state(0x410CC601)['architecture'], 'armv6-m or armv8-m baseline')
        self.assertEqual(probe_state.cpuid_state(0x410FC241)['partno'], '0xc24')
        self.assertTrue(probe_state.cpuid_state(0x00000000)['architecture'].startswith('not M-profile'))

    def test_pcsr_classes(self):
        on = probe_state.DEMCR_TRCENA
        self.assertEqual(probe_state.pcsr_summary([0x0800_1000] * 3, 0)['status'], 'dwt-disabled')
        self.assertEqual(probe_state.pcsr_summary([0, 0], on)['status'], 'not-implemented')
        self.assertEqual(probe_state.pcsr_summary([0xFFFFFFFF, 0], on)['status'], 'no-address')
        s = probe_state.pcsr_summary([0x10, 0x10, 0xFFFFFFFF, 0, 0x20], on)
        self.assertEqual((s['status'], s['usable'], s['sentinel'], s['zero']), ('sampled', 3, 1, 1))
        self.assertEqual(list(s['pcs'].items()), [('0x00000010', 2), ('0x00000020', 1)])

    def test_image_segments(self):
        with tempfile.TemporaryDirectory() as d:
            elf = Path(d) / 'fw.elf'
            elf.write_bytes(elf32([(0x0800_0000, b'abcd'), (0x0800_0100, b'')]))
            segs, digest = probe_state.image_segments(str(elf), None)
            self.assertEqual(segs, [(0x0800_0000, b'abcd')])
            self.assertEqual(digest, hashlib.sha256(elf.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ValueError, 'ELF carries its own load addresses'):
                probe_state.image_segments(str(elf), 0x0800_0000)
            raw = Path(d) / 'fw.bin'
            raw.write_bytes(b'xyz')
            self.assertEqual(probe_state.image_segments(str(raw), 0x100)[0], [(0x100, b'xyz')])
            with self.assertRaisesRegex(ValueError, 'give --base'):
                probe_state.image_segments(str(raw), None)

    def test_a_malformed_elf_is_refused_not_compared_in_part(self):
        good = elf32([(0x0800_0000, b'abcd')])
        cases = [(good[:-2], 'segment 0 at 0x08000000 ends past the end of the file'),
                 (good[:60], 'program header table ends past the end of the file'),
                 (good[:40], 'shorter than an ELF32 header'),
                 (good[:42] + struct.pack('<H', 16) + good[44:], 'entries of 16 bytes')]
        with tempfile.TemporaryDirectory() as d:
            elf = Path(d) / 'fw.elf'
            for data, want in cases:
                elf.write_bytes(data)
                with self.assertRaisesRegex(ValueError, want):
                    probe_state.image_segments(str(elf), None)


try:
    import tkinter
    _TCL = tkinter.Tcl
except ImportError:
    _TCL = None

# OpenOCD's commands as Tcl stubs: calls are logged, echo collects output.
OPENOCD_TCL_STUBS = '''
set calls {}; set out {}
proc init {} {}; proc shutdown {} {lappend ::calls shutdown}
proc echo {s} {lappend ::out $s}
proc read_memory {a w n} {return 0x01010001}
proc target {cmd} {return tgt}
proc tgt {cmd} {return $::state}
proc halt {} {lappend ::calls halt}
proc resume {} {lappend ::calls resume}
proc get_reg {r} {
  if {$::pcfail} {error "failed to read pc"}
  return [dict create pc 0x0800abcd]
}
'''


@unittest.skipIf(_TCL is None, 'needs a Tcl interpreter (tkinter)')
class ProbeStateHaltReadTclTest(unittest.TestCase):
    def run_tcl(self, state, pcfail):
        tcl = _TCL()
        tcl.eval(OPENOCD_TCL_STUBS)
        tcl.eval(f'set state {state}; set pcfail {int(pcfail)}')
        tcl.eval(probe_state.OpenOCD.script(probe_state.OpenOCD.HALT_READ))
        return tcl.splitlist(tcl.eval('set calls')), list(tcl.splitlist(tcl.eval('set out')))

    def test_a_failed_pc_read_still_resumes_the_core_it_halted(self):
        calls, out = self.run_tcl('running', pcfail=True)
        self.assertEqual(calls, ('halt', 'resume', 'shutdown'))
        self.assertEqual(out[-2], 'R DHCSR 0x01010001')
        self.assertTrue(out[-1].startswith('ERROR halt/pc read: failed to read pc; found running, '
                                           'DHCSR after 0x01010001'), out[-1])
        self.assertFalse(any(o.startswith('R PC') for o in out))

    def test_success_and_a_halted_core(self):
        calls, out = self.run_tcl('running', pcfail=False)
        self.assertEqual(calls, ('halt', 'resume', 'shutdown'))
        self.assertEqual(out, ['R DHCSR 0x01010001', 'R PC 0x0800abcd', 'R DHCSR 0x01010001'])
        calls, out = self.run_tcl('halted', pcfail=True)
        self.assertEqual(calls, ('shutdown',))
        self.assertIn('found halted', out[-1])


@unittest.skipIf(os.name == 'nt', 'JLinkExe and openocd stubs require POSIX executable semantics')
class ProbeStateCliTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        for name, body in (('JLinkExe', FAKE_JLINK_STATE), ('openocd', FAKE_OPENOCD_STATE)):
            f = self.d / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.config = self.d / 'hil.json'
        self.config.write_text(json.dumps({'boards': STATE_BOARDS}))
        self.log = self.d / 'log.json'
        self.env = {**os.environ, 'PATH': f'{self.d}{os.pathsep}{os.environ["PATH"]}', 'FAKE_LOG': str(self.log)}

    def run_cli(self, board, *args, **env):
        r = subprocess.run([sys.executable, str(PROBE_STATE), '--hil-config', str(self.config), '--board', board,
                            *args], capture_output=True, text=True, timeout=30, env={**self.env, **env}, cwd='/')
        out = json.loads(r.stdout) if r.stdout.strip() else None
        return r.returncode, out, r.stderr

    def logged(self):
        return json.loads(self.log.read_text())

    def test_jlink_observation(self):
        rc, out, err = self.run_cli('k64f', *LINK, '--samples', '4', '--interval-ms', '3',
                                    FAKE_PCSR='08001000,08001000,FFFFFFFF,08002000',
                                    FAKE_DHCSR='01010001,03010001')
        self.assertEqual(rc, 0, err)
        self.assertEqual((out['backend'], out['probe'], out['device']), ('jlink', '000621000000', 'MK64FN1M0xxx12'))
        self.assertEqual(out['cpuid']['architecture'], 'armv7-m or armv8-m mainline')
        self.assertFalse(out['dhcsrBefore']['halted'])
        self.assertTrue(out['dhcsrAfter']['resetSinceLastRead'])
        self.assertEqual(out['pcsr'], {'samples': 4, 'status': 'sampled', 'usable': 3, 'sentinel': 1, 'zero': 0,
                                       'pcs': {'0x08001000': 2, '0x08002000': 1}})
        self.assertNotIn('verify', out)
        log = self.logged()
        self.assertEqual(log['argv'][:4], ['-device', 'MK64FN1M0xxx12', '-SelectEmuBySN', '000621000000'])
        self.assertIn('-if swd -speed 4000', ' '.join(log['argv']))
        self.assertEqual(log['script'].count('Sleep 3'), 4)
        self.assertNotRegex(log['script'], r'(?m)^(h|g|r|w\w*|savebin.*)$')   # no halt, no write

    def test_jlink_verify_match_and_mismatch(self):
        mem = self.d / 'flash.bin'
        mem.write_bytes(bytes(range(256)) * 4)
        elf = self.d / 'fw.elf'
        elf.write_bytes(elf32([(0x0000_0010, bytes(range(16, 48))), (0x0000_0100, bytes(range(16)))]))
        rc, out, err = self.run_cli('k64f', *LINK, '--verify', str(elf), FAKE_MEM=str(mem), FAKE_MEM_BASE='0')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['verify']['result'], 'match')
        self.assertEqual([s['address'] for s in out['verify']['segments']], ['0x00000010', '0x00000100'])
        self.assertIn('savebin ', self.logged()['script'])
        self.assertEqual(out['verify']['dhcsrAfter']['raw'], '0x01010001')
        elf.write_bytes(elf32([(0x0000_0010, bytes(range(16, 40)) + b'XX' + bytes(range(42, 48)))]))
        rc, out, err = self.run_cli('k64f', *LINK, '--verify', str(elf), FAKE_MEM=str(mem), FAKE_MEM_BASE='0')
        self.assertEqual(rc, 1, err)
        self.assertEqual(out['verify']['result'], 'mismatch')
        self.assertEqual(out['verify']['segments'][0]['firstDiff'], '0x00000028')

    def test_digest_is_of_the_bytes_compared(self):
        mem = self.d / 'flash.bin'
        mem.write_bytes(b'firmware')
        raw = self.d / 'fw.bin'
        raw.write_bytes(b'firmware')
        want = hashlib.sha256(b'firmware').hexdigest()
        rc, out, err = self.run_cli('k64f', *LINK, '--verify', str(raw), '--base', '0x0', FAKE_MEM=str(mem),
                                    FAKE_REPLACE=str(raw))
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(raw.read_bytes(), b'firmware')    # the fake did replace it mid-run
        self.assertEqual(out['verify']['sha256'], want)

    def test_a_truncated_elf_never_reaches_the_probe(self):
        elf = self.d / 'fw.elf'
        elf.write_bytes(elf32([(0x0, b'abcd')])[:-2])
        rc, out, err = self.run_cli('k64f', *LINK, '--verify', str(elf))
        self.assertEqual(rc, 2)
        self.assertIn('ends past the end of the file', err)
        self.assertFalse(self.log.exists())

    def test_openocd_observation_and_raw_verify(self):
        mem = self.d / 'flash.bin'
        mem.write_bytes(b'\x00' * 16 + b'firmware')
        raw = self.d / 'fw.bin'
        raw.write_bytes(b'firmware')
        rc, out, err = self.run_cli('max32', '--samples', '2', '--verify', str(raw), '--base', '0x10000010',
                                    FAKE_PCSR='0', FAKE_MEM=str(mem), FAKE_MEM_BASE='10000000')
        self.assertEqual(rc, 0, err)
        self.assertEqual((out['backend'], out['cfg']), ('openocd', '-f interface/cmsis-dap.cfg -f target/max32665.cfg'))
        self.assertEqual(out['pcsr'], {'samples': 2, 'status': 'not-implemented'})
        self.assertEqual(out['verify']['result'], 'match')
        self.assertFalse(out['resetDetected'])
        argv = self.logged()['argv']
        self.assertIn('adapter serial E6614C311B597D32', argv)
        self.assertIn('adapter usb vid_pid 0x2e8a 0x000c', argv)
        self.assertEqual(argv[argv.index('-f'):argv.index('-f') + 4],
                         ['-f', 'interface/cmsis-dap.cfg', '-f', 'target/max32665.cfg'])
        self.assertNotRegex(self.logged()['script'], r'\b(halt|resume|reset|write_memory|mww|program)\b')

    def test_dwt_disabled_samples_are_not_reported_as_pcs(self):
        rc, out, err = self.run_cli('max32', FAKE_DEMCR='00000000', FAKE_PCSR='FFFFFFFF')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['pcsr']['status'], 'dwt-disabled')
        self.assertFalse(out['demcr']['dwtEnabled'])

    def test_halt_read_only_when_allowed_and_needed(self):
        rc, out, err = self.run_cli('max32', FAKE_PCSR='0')
        self.assertNotIn('haltRead', out)
        rc, out, err = self.run_cli('max32', '--allow-halt', FAKE_PCSR='08001000')
        self.assertEqual(out['haltRead'], {'performed': False, 'reason': 'PCSR sampled'})
        rc, out, err = self.run_cli('max32', '--allow-halt', FAKE_PCSR='0', FAKE_DHCSR='01030003')
        self.assertEqual(out['haltRead'], {'performed': False, 'reason': 'core halted'})
        rc, out, err = self.run_cli('max32', '--allow-halt', FAKE_PCSR='0', FAKE_PC='0800abcd')
        self.assertEqual(rc, 0, err)
        self.assertEqual((out['haltRead']['pc'], out['haltRead']['resumed']), ('0x0800abcd', True))
        self.assertIn('if {$was eq "running"} {resume}', self.logged()['script'])
        rc, out, err = self.run_cli('k64f', *LINK, '--allow-halt', FAKE_PCSR='FFFFFFFF', FAKE_PC='0000abc0')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['haltRead']['pc'], '0x0000abc0')
        self.assertEqual(self.logged()['script'].splitlines()[1:3], ['h', 'g'])
        rc, out, err = self.run_cli('k64f', *LINK, '--allow-halt', FAKE_PCSR='FFFFFFFF',
                                    FAKE_HALT_DHCSR='01010001,01030003')
        self.assertEqual(rc, 3)
        self.assertIn('left a core it found running halted', out['error'])

    def test_a_core_that_is_not_m_profile_is_not_interpreted(self):
        rc, out, err = self.run_cli('max32', FAKE_CPUID='00000000')
        self.assertEqual(rc, 3)
        self.assertIn('M-profile', out['error'])
        self.assertNotIn('dhcsrBefore', out)

    def test_tool_failures_exit_3(self):
        for board, args in (('k64f', LINK), ('max32', ())):
            for mode, want in (('truncate', 'incomplete capture'), ('fail', 'exited 1')):
                rc, out, err = self.run_cli(board, *args, FAKE_MODE=mode)
                self.assertEqual(rc, 3, (board, mode))
                self.assertIn(want, out['error'], (board, mode))
        rc, out, err = self.run_cli('max32', FAKE_MODE='tclerror')
        self.assertEqual(rc, 3)
        self.assertIn('read_memory: failed', out['error'])
        rc, out, err = self.run_cli('k64f', *LINK, PATH='/nonexistent')
        self.assertEqual(rc, 3)
        self.assertIn('JLinkExe not found', out['error'])

    def test_refusals_launch_nothing(self):
        bin_ = self.d / 'fw.bin'
        bin_.write_bytes(b'x')
        cases = [('p4', (), 'no debug-probe route'), ('h743', (), 'stlink'), ('no_uid', LINK, 'no flasher.uid'),
                 ('no_dev', LINK, "-device NAME"), ('no_cfg', (), 'needs flasher.args'),
                 ('bad_ids', (), '0xVVVV 0xPPPP'), ('twin', LINK, '2 entries'), ('absent', LINK, '0 entries'),
                 ('k64f', (), 'needs --interface and --speed'), ('k64f', ('--interface', 'swd', '--speed', 'fast'),
                                                                  '--speed must be kHz'),
                 ('max32', ('--speed', '4000'), 'J-Link only'), ('max32', ('--base', '0x0'), 'goes with --verify'),
                 ('max32', ('--verify', str(bin_)), 'give --base'),
                 ('max32', ('--verify', str(bin_), '--base', 'flash'), 'not an address'),
                 ('max32', ('--samples', '0'), '--samples'), ('max32', ('--timeout', 'nan'), '--timeout')]
        for board, args, want in cases:
            rc, out, err = self.run_cli(board, *args)
            self.assertEqual(rc, 2, (board, args, err))
            self.assertIn(want, err, (board, args))
            self.assertFalse(self.log.exists(), (board, args))


if __name__ == '__main__':
    unittest.main()
