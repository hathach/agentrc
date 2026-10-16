"""Tests for the target-debug skill's pc_sample.py: DWT_PCSR samples read through
a fake JLinkExe on PATH (prompt-prefixed output like the real tool) are
histogrammed by function through a fake symbolizer; an incomplete capture, a
missing or broken tool, and a symbolizer answering the wrong number of lines
are explicit failures, never a partial result passed off as a profile.

probe_state.py through fake JLinkExe and openocd: the plumbing only; its hardware
behaviour is verified on a rig board."""
import contextlib
import importlib.util
import io
import hashlib
import json
import struct
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from unittest import mock
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
# the read-back sees; FAKE_LOG records argv and the script, one JSON line per session;
# FAKE_MODE=truncate stops answering after the CPUID read, =fail exits 1, =short dumps
# one byte less than asked; FAKE_HALT_DHCSR answers DHCSR in a J-Link halt-read
# session; FAKE_REPLACE names a file the read-back overwrites; FAKE_SN is the serial
# the J-Link fake reports (default: the one selected); FAKE_CORES / FAKE_CORES_AFTER are
# the OpenOCD fake's other cores as `name=DHCSR,...` (default none), FAKE_CURRENT its
# current target.
FAKE_COMMON = """
import json, os, sys
regs = {k: os.environ.get('FAKE_' + k, d).split(',') for k, d in
        (('CPUID', '410fc241'), ('DHCSR', '01010001'), ('DEMCR', '01000000'), ('PCSR', '08001000'), ('PC', '08004444'),
         ('FPCTRL', '00000260'), ('DWTCTRL', '40000000'))}
def log(script):
    with open(os.environ['FAKE_LOG'], 'a') as f:
        f.write(json.dumps({'argv': sys.argv[1:], 'script': script}) + '\\n')
seen = {}
mode = os.environ.get('FAKE_MODE', '')
def value(reg):
    state = os.environ.get('FAKE_STATE')
    if reg == 'DHCSR' and state and os.path.exists(state):
        return int(open(state).read(), 16)
    n = seen.get(reg, 0); seen[reg] = n + 1
    vals = regs[reg]
    return int(vals[min(n, len(vals) - 1)], 16)
def dump(path, addr, size):
    mem = open(os.environ['FAKE_MEM'], 'rb').read() if os.environ.get('FAKE_MEM') else b''
    off = addr - int(os.environ.get('FAKE_MEM_BASE', '0'), 16)
    if mode == 'short':
        size -= 1
    open(path, 'wb').write(mem[off:off + size])
    if os.environ.get('FAKE_REPLACE'):
        open(os.environ['FAKE_REPLACE'], 'wb').write(b'replaced while the probe read back')
"""

FAKE_JLINK_STATE = '#!/usr/bin/env python3' + FAKE_COMMON + """
import re
script = sys.stdin.read()
log(script)
if '\\nh\\n' in script and os.environ.get('FAKE_HALT_DHCSR'):
    regs['DHCSR'] = os.environ['FAKE_HALT_DHCSR'].split(',')
names = {'E000ED00': 'CPUID', 'E000EDF0': 'DHCSR', 'E000EDFC': 'DEMCR', 'E000101C': 'PCSR', 'E0002000': 'FPCTRL',
         'E0001000': 'DWTCTRL'}
print('SEGGER J-Link Commander V9.78 (Compiled fake)')
sn = os.environ.get('FAKE_SN', sys.argv[sys.argv.index('-SelectEmuBySN') + 1].lstrip('0'))
print(f'S/N: {sn}')
print('VTref=3.300V')
if os.environ.get('FAKE_JLINK_SLEEP'):
    import time; time.sleep(float(os.environ['FAKE_JLINK_SLEEP']))
once = os.environ.get('FAKE_READS_MAX_ONCE')
cut = os.environ.get('FAKE_STATE', '/nonexistent') + '.cut'
reads_max = int(once) if once and not os.path.exists(cut) else None
if reads_max is not None:
    open(cut, 'w').write('')
for line in script.splitlines():
    m = re.match(r'mem32 ([0-9A-F]{8}), 1', line)
    if m:
        if reads_max is not None and reads_max == 0:
            break
        print(f'J-Link>{m.group(1)} = {value(names[m.group(1)]):08X}')
        reads_max = None if reads_max is None else reads_max - 1
        if mode == 'truncate':
            break
    m = re.match(r'savebin (\\S+), 0x([0-9A-F]+), 0x([0-9A-F]+)', line)
    if m:
        dump(m.group(1), int(m.group(2), 16), int(m.group(3), 16))
    if line == 'h':
        print(f'PC = {value("PC"):08X}, CycleCnt = 00000000')
if os.environ.get('FAKE_STATE'):    # the DLL halts at close, then restarts unless told not to
    open(os.environ['FAKE_STATE'], 'w').write('00030003' if 'exec SetRestartOnClose = 0' in script else '01010001')
sys.exit(1 if mode == 'fail' else 0)
"""

FAKE_OPENOCD_STATE = '#!/usr/bin/env python3' + FAKE_COMMON + """
import re, time
gdb_port = next((a.split()[-1] for a in sys.argv if a.startswith('gdb port ')), None)
if gdb_port:
    log('server')
    print(f'Info : Listening on port {gdb_port} for gdb connections', file=sys.stderr, flush=True)
    while not os.environ.get('FAKE_SERVER_STAY') and not os.path.exists(os.environ.get('FAKE_STOP', '/nonexistent')):
        time.sleep(0.05)
    while os.environ.get('FAKE_SERVER_STAY'):
        time.sleep(0.05)
    sys.exit(0)
script = sys.argv[-1]
log(script)
print('Info : CMSIS-DAP: Serial# = E6614C311B597D32', file=sys.stderr)
if 'not touched:' in script:    # pc_sample's recovery
    print(f'R DHCSR 0x{value("DHCSR"):08x}', file=sys.stderr)
    open(os.environ['FAKE_STATE'], 'w').write('01010001')
    print(f'R DHCSR 0x{value("DHCSR"):08x}', file=sys.stderr)
    sys.exit(0)
others = {'B': os.environ.get('FAKE_CORES', '')}
others['A'] = os.environ.get('FAKE_CORES_AFTER', others['B'])
for line in script.splitlines():
    if line == 'echo "C [target current]"':
        print('C ' + os.environ.get('FAKE_CURRENT', 'max32665.cpu'), file=sys.stderr)
    m = re.search(r'format "T([BA]) ', line)
    if m:
        for kv in filter(None, others[m.group(1)].split(',')):
            name, v = kv.split('=')
            print(f'T{m.group(1)} {name} 0x{int(v, 16):08x}', file=sys.stderr)
    m = re.match(r'\\s*(?:rd |echo \\[format "R )(\\w+) ', line)
    if m:
        print(f'R {m.group(1)} 0x{value(m.group(1)):08x}', file=sys.stderr)
        if mode == 'truncate':
            break
    m = re.match(r'dump_image \\{(\\S+)\\} 0x([0-9a-f]+) 0x([0-9a-f]+)', line)
    if m:
        dump(m.group(1), int(m.group(2), 16), int(m.group(3), 16))
m = re.search(r'for \\{set i 0\\} \\{\\$i < (\\d+)\\}', script)
if m and 'get_reg pc' in script:    # the line loop above printed both R DHCSR lines
    if os.environ.get('FAKE_HALT_STALL'):
        if os.environ['FAKE_HALT_STALL'] != 'early':
            print('GUARDED', file=sys.stderr, flush=True)
            open(os.environ['FAKE_STATE'], 'w').write('00030003')
        time.sleep(60)
    if os.environ.get('FAKE_HALT_FAIL'):
        print('ERROR resume 0 failed: the core is left halted (DHCSR 0x00030003)', file=sys.stderr)
        sys.exit(0)
    pcs = os.environ.get('FAKE_HALT_PCS', '08004444').split(',')
    for i in range(int(m.group(1))):
        print(f'H 0x{int(pcs[i % len(pcs)], 16):08x}', file=sys.stderr)
    sys.exit(0)
if 'arp_poll' in script:
    if os.environ.get('FAKE_RESTORE_FAIL'):
        print('ERROR resume failed', file=sys.stderr)
    else:
        open(os.environ['FAKE_STATE'], 'w').write('01000001')
if mode == 'tclerror':
    print('ERROR read_memory: failed to read memory', file=sys.stderr)
sys.exit(1 if mode == 'fail' else 0)
"""


# gdb: echoes the AUTOPSY markers the way gdb would, attach writes FAKE_STATE halted and
# monitor go/resume running (what the end anchor then reads), output EXPR answers from
# FAKE_EXPRS "expr=value;..." or with gdb's refusal for an assignment, dump writes the
# bytes (one short with FAKE_GDB_SHORT), detach touches FAKE_STOP for the server. Errors go
# to stderr and the exit status is the last command's, as gdb -batch does; a FAKE_EXPRS value
# starting with ! is an error. `monitor exec SetRestartOnClose = 0` leaves FAKE_STATE.norestart
# for the J-Link server fake. FAKE_GDB_ATTACH_FAIL fails the attach, FAKE_GDB_NORESUME ignores
# the resume, FAKE_GDB_HANG never exits, FAKE_GDB_HANG_ATTACHED hangs after the attach and
# FAKE_GDB_EXIT exits with that status after the DHCSR marker.
FAKE_GDB = """#!/usr/bin/env python3
import json, os, re, sys, time
args = sys.argv[1:]
cmds = [args[i + 1] for i, a in enumerate(args) if a == '-ex']
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({'argv': args, 'script': 'gdb'}) + '\\n')
if os.environ.get('FAKE_GDB_HANG'):
    time.sleep(60)
exprs = dict(kv.split('=', 1) for kv in os.environ.get('FAKE_EXPRS', '').split(';') if kv)
attached = False
ok = True
def state(v):
    open(os.environ['FAKE_STATE'], 'w').write(v)
def error(msg):
    global ok
    sys.stdout.flush(); print(msg, file=sys.stderr, flush=True); ok = False
for c in cmds:
    ok = True
    if c.startswith('target remote'):
        if os.environ.get('FAKE_GDB_ATTACH_FAIL'):
            error(':3333: Connection timed out.'); continue
        attached = True; state('00030003')
        if os.environ.get('FAKE_GDB_HANG_ATTACHED'):
            time.sleep(60)
    elif c.startswith('set '):
        pass
    elif c.startswith('echo '):
        sys.stdout.write(c[5:].replace('\\\\n', '\\n'))
    elif not attached:
        error('The program has no registers now.')
    elif c.startswith('printf "AUTOPSY DHCSR'):
        print('AUTOPSY DHCSR 0x00030003', flush=True)
        if os.environ.get('FAKE_GDB_EXIT'):
            sys.exit(int(os.environ['FAKE_GDB_EXIT']))
    elif c.startswith('printf "AUTOPSY PC'):
        print('AUTOPSY PC 0x08001436')
    elif c.startswith('printf "AUTOPSY ICSR'):
        print('AUTOPSY ICSR 0x00000000')
    elif c == 'info symbol $pc':
        print('tud_task_ext + 6 in section .text')
    elif c.startswith('bt '):
        print('#0  0x08001436 in tud_task_ext ()'); print('#1  0x080002e0 in main ()')
    elif c == 'info threads':
        print('* 1    Thread 1 "rp2040.core0" 0x08001436 in tud_task_ext ()')
    elif c.startswith('output '):
        e = c[7:]
        v = exprs.get(e, '!No symbol "%s" in current context.' % e)
        if re.search(r'[^=!<>]=[^=]', e):
            error('Writing to memory is not allowed (addr 0x20000004, len 4)')
        elif v.startswith('!'):
            error(v[1:])
        else:
            sys.stdout.write(v)
    elif c.startswith('dump binary memory '):
        path, a, b = c.split()[3:6]
        n = int(b, 16) - int(a, 16) - (1 if os.environ.get('FAKE_GDB_SHORT') else 0)
        open(path, 'wb').write(bytes(range(256)) * (n // 256) + bytes(range(n % 256)))
    elif c in ('monitor go', 'monitor resume') and not os.environ.get('FAKE_GDB_NORESUME'):
        state('01010001')
    elif c == 'monitor exec SetRestartOnClose = 0':
        open(os.environ['FAKE_STATE'] + '.norestart', 'w').write('')
    elif c == 'detach':
        print('[Inferior 1 (Remote target) detached]')
        open(os.environ['FAKE_STOP'], 'w').write('')
    sys.stdout.flush()
sys.exit(0 if ok else 1)
"""

# JLinkGDBServer: its close halts the core and restarts it, unless gdb sent the setting or
# FAKE_SERVER_NORESTART stands for a restart that did not happen.
FAKE_JLINK_GDBSERVER = """#!/usr/bin/env python3
import json, os, signal, sys, time
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({'argv': sys.argv[1:], 'script': 'server'}) + '\\n')
def close(*_):
    st = os.environ['FAKE_STATE']
    keep = os.path.exists(st + '.norestart') or os.environ.get('FAKE_SERVER_NORESTART')
    open(st, 'w').write('00030003' if keep else '01010001')
    sys.exit(0)
signal.signal(signal.SIGTERM, close)
time.sleep(float(os.environ.get('FAKE_SERVER_DELAY', '0')))
print('Waiting for GDB connection...', flush=True)
while not os.path.exists(os.environ['FAKE_STOP']):
    time.sleep(0.05)
close()
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
    {'name': 'pico', 'flasher': {'name': 'openocd', 'uid': 'E6614103E72C1D2F',
                                 'args': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg'}},
]


def elf32(segments, symbols=(), e_type=2):
    """A minimal little-endian ELF32 with one PT_LOAD per (paddr, bytes) and, when symbols are
    given, sections .text (1), .rodata (2), .bss (3), then .symtab, .strtab, .shstrtab, the
    symbols as (name, value, size, type, shndx)."""
    phoff, phentsize = 52, 32
    data_off = phoff + phentsize * len(segments)
    phdrs, blobs = b'', b''
    for paddr, blob in segments:
        phdrs += struct.pack('<8I', 1, data_off + len(blobs), paddr | 0x1000_0000, paddr, len(blob), len(blob), 5, 4)
        blobs += blob
    shoff, shnum, shstrndx, tail = 0, 0, 0, b''
    if symbols:
        strtab, symtab = b'\0', bytes(16)
        for name, value, size, kind, shndx in symbols:
            symtab += struct.pack('<IIIBBH', len(strtab), value, size, kind, 0, shndx)
            strtab += name.encode() + b'\0'
        names = [b'.text', b'.rodata', b'.bss', b'.symtab', b'.strtab', b'.shstrtab']
        shstrtab, name_off = b'\0', []
        for n in names:
            name_off.append(len(shstrtab))
            shstrtab += n + b'\0'
        off_sym = data_off + len(blobs)
        off_str, off_shstr = off_sym + len(symtab), off_sym + len(symtab) + len(strtab)
        shoff = off_shstr + len(shstrtab)
        shdr = lambda i, kind, flags, off, size, link, entsize: struct.pack(  # noqa: E731
            '<10I', name_off[i], kind, flags, 0, off, size, link, 0, 1, entsize)
        alloc, execinstr = 0x2, 0x4
        tail = (symtab + strtab + shstrtab + bytes(40)
                + shdr(0, 1, alloc | execinstr, 0, 0, 0, 0) + shdr(1, 1, alloc, 0, 0, 0, 0)
                + shdr(2, 8, alloc | 0x1, 0, 0, 0, 0)
                + shdr(3, 2, 0, off_sym, len(symtab), 5, 16) + shdr(4, 3, 0, off_str, len(strtab), 0, 0)
                + shdr(5, 3, 0, off_shstr, len(shstrtab), 0, 0))
        shnum, shstrndx = 7, 6
    header = bytearray(b'\x7fELF\x01\x01\x01' + bytes(9))
    header += struct.pack('<HHIIIIIHHHHHH', e_type, 40, 1, 0, phoff, shoff, 0, 52, phentsize, len(segments), 40, shnum,
                          shstrndx)
    return bytes(header) + phdrs + blobs + tail


OBJ, FUNC, TEXT, RODATA, BSS = 1, 2, 1, 2, 3   # symbol types and the section indexes elf32() lays out
SYMS = [('g_count', 0x2000_0010, 4, OBJ, BSS), ('g_flag', 0x2000_0014, 1, OBJ, BSS),
        ('g_wide', 0x2000_0018, 8, OBJ, BSS), ('g_odd', 0x2000_0020, 3, OBJ, BSS),
        ('table', 0x0800_0100, 16, OBJ, RODATA), ('main', 0x0800_0200, 40, FUNC, TEXT),
        ('code_obj', 0x0800_0300, 4, OBJ, TEXT), ('same_twice', 0x2000_0060, 4, OBJ, BSS),
        ('same_twice', 0x2000_0060, 4, FUNC, TEXT),
        ('dup', 0x2000_0030, 4, OBJ, BSS), ('dup', 0x2000_0040, 4, OBJ, BSS),
        ('empty', 0x2000_0050, 0, OBJ, BSS), ('extern_only', 0, 0, 0, 0),
        ('reg', 0x4002_0000, 4, OBJ, BSS), ('ABSVAL', 0x1234, 4, OBJ, 0xFFF1),
        ('flash_var', 0x1000_0100, 4, OBJ, RODATA)]


class ProbeStateUnitTest(unittest.TestCase):
    def test_dhcsr_bits(self):
        self.assertEqual(probe_state.dhcsr_state(0x03030003),
                         {'raw': '0x03030003', 'halted': True, 'sleeping': False, 'lockup': False,
                          'retiredSinceLastRead': True, 'resetSinceLastRead': True})
        self.assertTrue(probe_state.dhcsr_state(1 << 18)['sleeping'])
        self.assertTrue(probe_state.dhcsr_state(1 << 19)['lockup'])

    def test_openocd_gdb_server_command(self):
        with unittest.mock.patch.dict(os.environ, {'PROBE_STATE_OPENOCD': sys.executable}):
            ocd = probe_state.OpenOCD('SN1', '-f target/x.cfg -c "gdb_port disabled"', None)
        self.assertEqual(ocd.cmd, [sys.executable, '-c', 'tcl_port disabled', '-c', 'gdb_port disabled', '-c',
                                   'telnet_port disabled', '-c', 'adapter serial SN1', '-f', 'target/x.cfg', '-c',
                                   'gdb_port disabled'])
        cmd = ocd.command(gdb_port=3333)
        self.assertNotIn('gdb_port disabled', cmd)
        self.assertEqual(cmd.count('gdb port 3333'), 2)
        self.assertEqual(cmd[4:7], ['gdb port 3333', '-c', 'gdb memory_map disable'])

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

    def test_budget_fields(self):
        on = probe_state.DEMCR_TRCENA
        b = probe_state.budget_state(0x0000_1261, 0x4000_0000, on)
        self.assertEqual((b['fpCtrl']['numCode'], b['fpCtrl']['enabled'], b['dwtCtrl']['numComp']), (22, True, 4))
        self.assertEqual(probe_state.budget_state(0x40, 0x2000_0000, 0)['dwtCtrl'], {'status': 'dwt-disabled'})
        self.assertEqual(probe_state.budget_state(0x40, 0, on)['fpCtrl']['numCode'], 4)

    def test_symbol_reads_resolve_offline_and_refuse_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            elf = Path(d) / 'fw.elf'
            elf.write_bytes(elf32([(0x0800_0000, b'abcd')], SYMS))
            self.assertEqual(probe_state.resolve_reads(str(elf), ['g_count', 'table'], False),
                             [('g_count', 0x2000_0010, 4), ('table', 0x0800_0100, 16)])
            self.assertEqual(probe_state.resolve_reads(str(elf), ['flash_var'], False), [('flash_var', 0x1000_0100, 4)])
            for name, want in (('nothere', 'no symbol'), ('main', 'a function'), ('dup', 'ambiguous'),
                               ('same_twice', 'ambiguous'), ('empty', 'zero size'), ('extern_only', 'undefined'),
                               ('reg', 'not within Code'), ('ABSVAL', 'absolute'), ('code_obj', 'data section')):
                with self.assertRaisesRegex(ValueError, want, msg=name):
                    probe_state.resolve_reads(str(elf), [name], False)
            with self.assertRaisesRegex(ValueError, 'RP2040 XIP region'):
                probe_state.resolve_reads(str(elf), ['flash_var'], True)
            probe_state.check_region('x', 0x1400_001C, 4, False)    # architectural Code: allowed off RP2040
            stripped = Path(d) / 'stripped.elf'
            stripped.write_bytes(elf32([(0x0800_0000, b'abcd')]))
            with self.assertRaisesRegex(ValueError, 'section header'):
                probe_state.resolve_reads(str(stripped), ['g_count'], False)
            relocatable = Path(d) / 'obj.o'
            relocatable.write_bytes(elf32([], SYMS, e_type=1))
            with self.assertRaisesRegex(ValueError, 'not a linked executable'):
                probe_state.resolve_reads(str(relocatable), ['g_count'], False)

    def test_region_rules(self):
        probe_state.check_region('x', 0x0800_0000, 0x10000, False)
        probe_state.check_region('x', 0x6000_0000, 16, False)
        for addr, size, rp, want in ((0x4000_0000, 4, False, 'not within'), (0xE000_EDF0, 4, False, 'not within'),
                                     (0x1FFF_FFFE, 4, False, 'not within'), (0x1000_0000, 4, True, 'RP2040'),
                                     (0x1300_0000, 4, True, 'RP2040'), (0x1400_001C, 4, True, 'RP2040 XIP'),
                                     (0x1800_0000, 4, True, 'RP2040 XIP'), (0x2000_0000, 0, False, 'positive'),
                                     (0xFFFF_FFFE, 4, False, 'outside')):
            with self.assertRaisesRegex(ValueError, want, msg=hex(addr)):
                probe_state.check_region('x', addr, size, rp)

    def test_read_decoding(self):
        self.assertEqual(probe_state.decode_read('a', 0x2000_0000, b'\x34\x12')['hex'], '0x1234')
        self.assertEqual(probe_state.decode_read('a', 0x2000_0000, b'\x01\x00\x00\x00\x00\x00\x00\x00')['value'], 1)
        odd = probe_state.decode_read('a', 0x2000_0000, b'abc')
        self.assertEqual((odd['bytes'], 'value' in odd), ('616263', False))


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
proc read_memory {a w n} {return $::dhcsr}
proc target {cmd} {if {$cmd eq "names"} {return $::targets}; return tgt}
proc tgt {args} {return $::dhcsr}
proc tgt2 {args} {return $::dhcsr2}
proc halt {} {lappend ::calls halt}
proc resume {} {lappend ::calls resume}
proc get_reg {r} {
  if {$::pcfail} {error "failed to read pc"}
  return [dict create pc 0x0800abcd]
}
'''


@unittest.skipIf(_TCL is None, 'needs a Tcl interpreter (tkinter)')
class ProbeStateHaltReadTclTest(unittest.TestCase):
    def run_tcl(self, state, pcfail, other=None):
        tcl = _TCL()
        tcl.eval(OPENOCD_TCL_STUBS)
        dhcsr = {'running': '0x01010001', 'halted': '0x01030003'}
        tcl.eval(f'set dhcsr {dhcsr[state]}; set pcfail {int(pcfail)}; set targets {{tgt{" tgt2" if other else ""}}}; '
                 f'set dhcsr2 {dhcsr[other or "running"]}')
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
        self.assertEqual(out[0], 'R DHCSR 0x01030003')
        self.assertIn('found halted', out[-1])

    def test_a_multi_core_target_refuses_the_halt(self):
        for other in ('halted', 'running'):
            calls, out = self.run_tcl('running', pcfail=False, other=other)
            self.assertEqual(calls, ('shutdown',))
            self.assertIn('refused: 2 cores (tgt tgt2)', out[-1])

    def test_an_empty_resume_error_is_named(self):
        tcl = _TCL()
        tcl.eval(OPENOCD_TCL_STUBS)
        tcl.eval('set dhcsr 0x01010001; set pcfail 0; set targets {tgt}; set dhcsr2 0; proc resume {} {error ""}')
        tcl.eval(probe_state.OpenOCD.script(probe_state.OpenOCD.HALT_READ))
        out = list(tcl.splitlist(tcl.eval('set out')))
        self.assertIn('resume: failed without a message; found running', out[-1])


HALT_TCL_STUBS = '''
set calls {}; set out {}; set state running; set n 0
proc echo {s} {lappend ::out $s}
proc target {cmd} {return $::targets}
proc read_memory {a w n} {expr {$::state eq "running" ? 0x01010001 : 0x01030003}}
proc halt {ms} {lappend ::calls "halt $ms"; if {$::haltfail == $::n} {error ""}; set ::state halted}
proc resume {} {lappend ::calls resume; if {$::resumefail != $::n} {set ::state running}; incr ::n}
proc get_reg {r} {return [dict create pc [format 0x%08x [expr {0x08001000 + $::n}]]]}
proc sleep {ms} {lappend ::calls "sleep $ms"}
'''


@unittest.skipIf(_TCL is None, 'needs a Tcl interpreter (tkinter)')
class PcSampleHaltTclTest(unittest.TestCase):
    def run_tcl(self, n=3, state='running', targets='tgt', haltfail=-1, resumefail=-1, interval=0):
        tcl = _TCL()
        tcl.eval(HALT_TCL_STUBS)
        tcl.eval(f'set state {state}; set targets {{{targets}}}; set haltfail {haltfail}; set resumefail {resumefail}')
        rc = tcl.eval(f'catch {{{pc_sample.halt_tcl(n, 250, interval)}}} err')
        err = tcl.eval('set err') if rc == '1' else None
        return list(tcl.splitlist(tcl.eval('set calls'))), list(tcl.splitlist(tcl.eval('set out'))), err

    def test_each_halt_is_resumed_and_checked(self):
        calls, out, err = self.run_tcl(interval=5)
        self.assertIsNone(err)
        self.assertEqual(calls, ['halt 250', 'resume', 'sleep 5'] * 3)
        self.assertEqual(out, ['R DHCSR 0x01010001', 'GUARDED', 'H 0x08001000', 'H 0x08001001', 'H 0x08001002',
                               'R DHCSR 0x01010001'])

    def test_a_failed_halt_still_resumes_and_stops(self):
        calls, out, err = self.run_tcl(haltfail=1)
        self.assertEqual(calls, ['halt 250', 'resume', 'halt 250', 'resume'])
        self.assertEqual(err, 'halt 1: failed without a message; DHCSR after a resume 0x01010001')

    def test_a_failed_resume_stops_the_loop(self):
        calls, out, err = self.run_tcl(resumefail=0)
        self.assertEqual(calls, ['halt 250', 'resume'])
        self.assertEqual(err, 'resume 0 failed: the core is left halted (DHCSR 0x01030003)')

    def test_a_halted_or_multi_core_target_is_refused_untouched(self):
        calls, out, err = self.run_tcl(state='halted')
        self.assertEqual(calls, [])
        self.assertIn('refused: the core is halted now', err)
        calls, out, err = self.run_tcl(targets='tgt tgt2')
        self.assertEqual(calls, [])
        self.assertIn('refused: 2 cores (tgt tgt2)', err)


class FakeToolsCase(unittest.TestCase):
    """A temp dir of fake tools first on PATH, the state HIL config and a FAKE_LOG of their sessions."""
    tools = ()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.d = Path(self._dir.name)
        for name, body in self.tools:
            f = self.d / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.config = self.d / 'hil.json'
        self.config.write_text(json.dumps({'boards': STATE_BOARDS}))
        self.log = self.d / 'log.json'
        self.env = {**os.environ, 'PATH': f'{self.d}{os.pathsep}{os.environ["PATH"]}', 'FAKE_LOG': str(self.log)}

    def sessions(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@unittest.skipIf(os.name == 'nt', 'openocd and addr2line stubs require POSIX executable semantics')
class PcSampleOpenOCDTest(FakeToolsCase):
    tools = (('openocd', FAKE_OPENOCD_STATE), ('JLinkExe', FAKE_JLINK), ('fake-addr2line', FAKE_ADDR2LINE))

    def setUp(self):
        super().setUp()
        self.elf = self.d / 'fw.elf'
        self.elf.write_bytes(b'\x7fELF')

    def run_cli(self, board, *args, **env):
        return subprocess.run([sys.executable, str(SCRIPT), '--hil-config', str(self.config), '--board', board,
                               '--elf', str(self.elf), '--addr2line', 'fake-addr2line', *args],
                              capture_output=True, text=True, timeout=30, env={**self.env, **env}, cwd='/')

    def scripts(self):
        return [s['script'] for s in self.sessions()]

    def test_an_openocd_board_samples_through_probe_state(self):
        r = self.run_cli('max32', '--samples', '3', '--halt-samples', '4', FAKE_PCSR='08001000,08001000,08002000',
                         FAKE_CORES='max32665.cpu1=00040001', FAKE_SYMS='0x08001000=spin@main.c:4')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('3 usable of 3 samples', r.stdout)
        self.assertIn('spin', r.stdout)
        self.assertIn('max32665.cpu1 DHCSR before 0x00040001, after 0x00040001', r.stdout)
        self.assertEqual(len(self.scripts()), 1)    # usable samples: no halting fallback
        self.assertEqual(self.scripts()[0].count('rd PCSR'), 3)

    def test_the_halting_fallback_is_labelled_apart(self):
        r = self.run_cli('max32', '--samples', '2', '--halt-samples', '3', FAKE_PCSR='FFFFFFFF',
                         FAKE_HALT_PCS='08003000,08003000,08004000')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('0 usable of 2 samples', r.stdout)
        self.assertIn('--- halted samples', r.stdout)
        self.assertIn('3 usable of 3 halt reads', r.stdout)
        self.assertIn('halt 500', self.scripts()[1])
        r = self.run_cli('max32', '--samples', '2', '--halt-samples', '3', FAKE_PCSR='FFFFFFFF', FAKE_HALT_FAIL='1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('halting fallback: openocd: resume 0 failed', r.stderr)

    def test_a_stalled_fallback_is_killed_then_recovered(self):
        r = self.run_cli('max32', '--samples', '2', '--halt-samples', '3', '--timeout', '2', FAKE_PCSR='FFFFFFFF',
                         FAKE_HALT_STALL='1', FAKE_STATE=str(self.d / 'state'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('did not finish within 2 s', r.stderr)
        self.assertIn('the core was left halted, resumed: now running (DHCSR 0x01010001)', r.stderr)
        self.assertEqual((self.d / 'state').read_text(), '01010001')
        self.log.unlink()
        r = self.run_cli('max32', '--samples', '2', '--halt-samples', '3', '--timeout', '2', FAKE_PCSR='FFFFFFFF',
                         FAKE_HALT_STALL='early')
        self.assertEqual(r.returncode, 1)
        self.assertIn('stopped before its guards passed, so it halted nothing', r.stderr)
        self.assertEqual(len(self.scripts()), 2)    # no recovery session

    def test_a_core_that_is_not_m_profile_is_refused(self):
        r = self.run_cli('max32', '--samples', '2', '--halt-samples', '3', FAKE_PCSR='FFFFFFFF', FAKE_CPUID='00000000')
        self.assertEqual(r.returncode, 1)
        self.assertIn('does not identify as an M-profile core', r.stderr)
        self.assertEqual(len(self.scripts()), 1)

    def test_no_fallback_without_the_flag_or_for_a_halted_core(self):
        r = self.run_cli('max32', '--samples', '2', FAKE_PCSR='FFFFFFFF')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.scripts()), 1)
        self.log.unlink()
        r = self.run_cli('max32', '--samples', '2', '--halt-samples', '3', FAKE_PCSR='FFFFFFFF', FAKE_DHCSR='01030003')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.scripts()), 1)

    def test_route_flags_are_checked(self):
        r = self.run_cli('max32', *LINK)
        self.assertEqual(r.returncode, 2)
        self.assertIn('J-Link only', r.stderr)
        r = self.run_cli('max32', '--probe', '1')
        self.assertEqual(r.returncode, 2)
        self.assertIn('--probe and --device are J-Link flags', r.stderr)
        r = self.run_cli('k64f', *LINK, '--halt-samples', '2')
        self.assertEqual(r.returncode, 2)
        self.assertIn('cannot stop on a failed resume', r.stderr)
        r = self.run_cli('k64f')
        self.assertEqual(r.returncode, 2)
        self.assertIn('a J-Link needs --interface and --speed', r.stderr)
        r = self.run_cli('max32', PROBE_STATE_OPENOCD='no-such-openocd')
        self.assertEqual(r.returncode, 2)    # probe_state's ToolError is pc_sample's when run as a script
        self.assertIn('no-such-openocd not found', r.stderr)


@unittest.skipIf(os.name == 'nt', 'JLinkExe and openocd stubs require POSIX executable semantics')
class ProbeStateCliTest(FakeToolsCase):
    tools = (('JLinkExe', FAKE_JLINK_STATE), ('openocd', FAKE_OPENOCD_STATE))

    def run_cli(self, board, *args, **env):
        r = subprocess.run([sys.executable, str(PROBE_STATE), '--hil-config', str(self.config), '--board', board,
                            *args], capture_output=True, text=True, timeout=30, env={**self.env, **env}, cwd='/')
        out = json.loads(r.stdout) if r.stdout.strip() else None
        return r.returncode, out, r.stderr

    def logged(self, session=-1):
        return self.sessions()[session]

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
        self.assertNotIn('SetRestartOnClose', log['script'])

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
        self.assertEqual(out['dhcsrAfterReads']['raw'], '0x01010001')
        self.assertEqual(out['connection'], {'serial': '621000000', 'vtref': 3.3})
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


    def test_budget_registers(self):
        rc, out, err = self.run_cli('k64f', *LINK, '--budget', FAKE_FPCTRL='00001261', FAKE_DWTCTRL='40000000')
        self.assertEqual(rc, 0, err)
        self.assertEqual((out['budget']['fpCtrl']['numCode'], out['budget']['dwtCtrl']['numComp']), (22, 4))
        rc, out, err = self.run_cli('max32', '--budget', FAKE_DEMCR='00000000', FAKE_FPCTRL='00000040')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['budget'], {'fpCtrl': {'raw': '0x00000040', 'numCode': 4, 'enabled': False},
                                         'dwtCtrl': {'status': 'dwt-disabled'}})
        rc, out, err = self.run_cli('max32')
        self.assertNotIn('budget', out)
        self.assertNotIn('E0002000', self.logged()['script'].lower().replace('0x', ''))

    def test_symbol_reads_over_both_backends(self):
        mem = self.d / 'ram.bin'
        mem.write_bytes(bytes(range(256)))
        elf = self.d / 'fw.elf'
        elf.write_bytes(elf32([(0x0800_0000, b'abcd')], SYMS))
        for board, args in (('k64f', LINK), ('max32', ())):
            rc, out, err = self.run_cli(board, *args, '--elf', str(elf), '--read', 'g_count', '--read', 'g_odd',
                                        FAKE_MEM=str(mem), FAKE_MEM_BASE='20000000')
            self.assertEqual(rc, 0, (board, err))
            values = out['reads']['values']
            self.assertEqual((values[0]['symbol'], values[0]['hex'], values[0]['value']),
                             ('g_count', '0x13121110', 0x13121110))
            self.assertEqual((values[1]['bytes'], 'value' in values[1]), ('202122', False))
            self.assertFalse(out['reads']['snapshot'])
            self.assertIn('dhcsrAfterReads', out)
        rc, out, err = self.run_cli('k64f', *LINK, '--elf', str(elf), '--read', 'g_count', FAKE_MEM=str(mem),
                                    FAKE_MEM_BASE='20000000', FAKE_MODE='short')
        self.assertEqual(rc, 3)
        self.assertIn('read back 3 of 4 bytes', out['error'])

    def test_save_is_exclusive_exact_and_kept_when_short(self):
        mem = self.d / 'flash.bin'
        mem.write_bytes(bytes(range(256)) * 4)
        target = self.d / 'pristine.bin'
        rc, out, err = self.run_cli('k64f', *LINK, '--save', '0x10', '32', str(target), FAKE_MEM=str(mem))
        self.assertEqual(rc, 0, err)
        self.assertEqual(target.read_bytes(), bytes(range(16, 48)))
        self.assertEqual(out['saves'], [{'file': str(target), 'address': '0x00000010', 'size': 32, 'bytes': 32,
                                         'complete': True, 'sha256': hashlib.sha256(bytes(range(16, 48))).hexdigest()}])
        rc, out, err = self.run_cli('k64f', *LINK, '--save', '0x10', '32', str(target), FAKE_MEM=str(mem))
        self.assertEqual(rc, 2)
        self.assertIn('never overwrites', err)
        self.assertEqual(len(self.sessions()), 1)
        short = self.d / 'short.bin'
        rc, out, err = self.run_cli('max32', '--save', '0x20', '16', str(short), FAKE_MEM=str(mem),
                                    FAKE_MEM_BASE='0', FAKE_MODE='short')
        self.assertEqual(rc, 3)
        self.assertIn('incomplete', out['error'])
        self.assertEqual((out['saves'][0]['complete'], out['saves'][0]['bytes'], len(short.read_bytes())),
                         (False, 15, 15))
        failed = self.d / 'failed.bin'
        rc, out, err = self.run_cli('max32', '--save', '0x20', '16', str(failed), FAKE_MODE='fail')
        self.assertEqual(rc, 3)
        self.assertEqual(out['saves'][0]['complete'], False)
        self.assertTrue(failed.exists())

    def test_connection_identity_and_core_states(self):
        rc, out, err = self.run_cli('k64f', *LINK, FAKE_SN='123')
        self.assertEqual(rc, 3)
        self.assertIn('S/N 123, not the requested 000621000000', out['error'])
        rc, out, err = self.run_cli('max32', FAKE_CURRENT='rp2040.core0', FAKE_CORES='rp2040.core1=00040001',
                                    FAKE_CORES_AFTER='rp2040.core1=00030003', FAKE_DHCSR='01000001')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['connection'], {'serial': 'E6614C311B597D32'})
        running = {'raw': '0x01000001', 'halted': False, 'sleeping': False}
        self.assertEqual(out['cores'], {
            'rp2040.core0': {'current': True, 'before': running, 'after': running},
            'rp2040.core1': {'current': False, 'before': {'raw': '0x00040001', 'halted': False, 'sleeping': True},
                             'after': {'raw': '0x00030003', 'halted': True, 'sleeping': False}}})
        script = self.logged()['script'].splitlines()
        first_dhcsr = next(i for i, ln in enumerate(script) if ln.startswith('rd DHCSR'))
        self.assertLess(first_dhcsr, next(i for i, ln in enumerate(script) if 'format "TB' in ln))
        (self.d / 'ram.bin').write_bytes(bytes(16))
        rc, out, err = self.run_cli('max32', '--save', '0x20000000', '4', str(self.d / 'end.bin'),
                                    FAKE_MEM=str(self.d / 'ram.bin'), FAKE_MEM_BASE='20000000',
                                    FAKE_DHCSR='01000001,01000001,00030003')
        self.assertEqual(rc, 0, err)
        self.assertTrue(out['cores']['max32665.cpu']['after']['halted'])    # the reading at the capture's end
        rc, out, err = self.run_cli('max32', FAKE_CORES='a=1', FAKE_CORES_AFTER='b=1')
        self.assertEqual(rc, 3)
        self.assertIn('incomplete core listing', out['error'])

    def test_rp2040_flash_reads_are_refused_before_the_probe(self):
        elf = self.d / 'fw.elf'
        elf.write_bytes(elf32([(0x1000_0000, b'abcd')], SYMS))
        for args in (('--save', '0x10000000', '16', str(self.d / 'x.bin')),
                     ('--save', '0x1400001c', '4', str(self.d / 'x.bin')), ('--elf', str(elf), '--read', 'flash_var')):
            rc, out, err = self.run_cli('pico', *args)
            self.assertEqual(rc, 2, args)
            self.assertIn('RP2040 XIP region', err)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.d / 'x.bin').exists())
        mem = self.d / 'ram.bin'
        mem.write_bytes(bytes(64))
        rc, out, err = self.run_cli('pico', '--elf', str(elf), '--read', 'g_count', FAKE_MEM=str(mem),
                                    FAKE_MEM_BASE='20000000')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['reads']['values'][0]['value'], 0)

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
                 ('max32', ('--verify', str(bin_), '--base', 'flash'), 'not a number'),
                 ('max32', ('--samples', '0'), '--samples'), ('max32', ('--timeout', 'nan'), '--timeout'),
                 ('max32', ('--read', 'x'), '--read needs --elf'), ('max32', ('--elf', str(bin_)), '--elf without'),
                 ('max32', ('--save', '0x40000000', '4', str(self.d / 'p.bin')), 'not within'),
                 ('max32', ('--save', '0x20000000', '4', str(self.d / 'bad name.bin')), 'characters'),
                 ('max32', ('--save', '0x20000000', 'four', str(self.d / 'q.bin')), 'not a number')]
        for board, args, want in cases:
            rc, out, err = self.run_cli(board, *args)
            self.assertEqual(rc, 2, (board, args, err))
            self.assertIn(want, err, (board, args))
            self.assertFalse(self.log.exists(), (board, args))


AUTOPSY = SCRIPT.parent / 'autopsy.py'


@unittest.skipIf(os.name == 'nt', 'gdb, server and probe stubs require POSIX executable semantics')
class AutopsyCliTest(FakeToolsCase):
    tools = (('JLinkExe', FAKE_JLINK_STATE), ('openocd', FAKE_OPENOCD_STATE), ('arm-none-eabi-gdb', FAKE_GDB),
             ('JLinkGDBServer', FAKE_JLINK_GDBSERVER))

    def setUp(self):
        super().setUp()
        self.elf = self.d / 'fw.elf'
        self.elf.write_bytes(elf32([(0x0800_0000, b'abcd')], SYMS))
        self.env.update(FAKE_STATE=str(self.d / 'state'), FAKE_STOP=str(self.d / 'stop'))

    def run_cli(self, board, *args, out='out', **env):
        r = subprocess.run([sys.executable, str(AUTOPSY), '--hil-config', str(self.config), '--board', board,
                            '--elf', str(self.elf), '--out', str(self.d / out), *args],
                           capture_output=True, text=True, timeout=60, env={**self.env, **env}, cwd='/')
        return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr

    def gdb_commands(self):
        argv = next(s['argv'] for s in self.sessions() if s['script'] == 'gdb')
        return [argv[i + 1] for i, a in enumerate(argv) if a == '-ex']

    def test_jlink_autopsy_reads_resumes_and_checks_the_end_state(self):
        dump = self.d / 'ram.bin'
        rc, out, err = self.run_cli('k64f', *LINK, '--expr', 'blink_interval_ms', '--expr', 'blink_interval_ms = 5',
                                    '--dump', '0x20000000', '300', str(dump), FAKE_EXPRS='blink_interval_ms=1000')
        self.assertEqual(rc, 1, err)    # the assignment was refused
        self.assertEqual((out['attachDhcsr'], out['pc'], out['icsr']), ('0x00030003', '0x08001436', '0x00000000'))
        self.assertEqual(out['symbol'], 'tud_task_ext + 6 in section .text')
        self.assertEqual(out['backtrace'][0], '#0  0x08001436 in tud_task_ext ()')
        self.assertEqual(out['exprs'][0], {'expr': 'blink_interval_ms', 'output': '1000'})
        self.assertIn('no value', out['exprs'][1]['error'])
        self.assertIn('Writing to memory is not allowed (addr 0x20000004, len 4)', out['gdbStderr'])
        self.assertEqual(out['dumps'][0]['bytes'], 300)
        self.assertTrue(out['dumps'][0]['complete'])
        self.assertTrue(out['resumed'])
        self.assertFalse(out['before']['cores']['core']['halted'])
        self.assertFalse(out['after']['cores']['core']['halted'])
        cmds = self.gdb_commands()
        self.assertEqual(cmds[:4], ['set may-write-memory off', 'set may-write-registers off',
                                    'set may-call-functions off', 'set may-insert-breakpoints off'])
        self.assertEqual(cmds[-2:], ['monitor go', 'detach'])
        last_read = max(i for i, c in enumerate(cmds) if c.startswith(('output', 'dump')))
        self.assertLess(last_read, cmds.index('monitor go'))
        server = next(s['argv'] for s in self.sessions() if s['script'] == 'server')
        self.assertEqual(server[:4], ['-select', 'usb=000621000000', '-device', 'MK64FN1M0xxx12'])
        self.assertIn('-singlerun', server)
        self.assertTrue((self.d / 'out' / 'gdb.log').exists())
        anchors = [s['script'] for s in self.sessions() if s['argv'][0] == '-device']
        self.assertEqual([a.startswith('exec SetRestartOnClose = 0\n') for a in anchors], [True, False])

    def test_a_core_found_halted_is_not_resumed(self):
        rc, out, err = self.run_cli('k64f', *LINK, FAKE_DHCSR='01030003')
        self.assertEqual(rc, 0, err)
        self.assertFalse(out['resumed'])
        cmds = self.gdb_commands()
        attach = next(i for i, c in enumerate(cmds) if c.startswith('target remote :'))
        self.assertEqual(cmds[attach + 1], 'monitor exec SetRestartOnClose = 0')
        self.assertNotIn('monitor go', cmds)
        self.assertTrue(out['after']['cores']['core']['halted'])
        anchors = [s['script'] for s in self.sessions() if s['argv'][0] == '-device']
        self.assertEqual([a.startswith('exec SetRestartOnClose = 0\n') for a in anchors], [True, True])

    def test_a_core_left_halted_is_restored_and_observed_again(self):
        # J-Link: the end anchor that saw it halted restarted it at close; a third session confirms
        rc, out, err = self.run_cli('k64f', *LINK, FAKE_GDB_NORESUME='1', FAKE_SERVER_NORESTART='1')
        self.assertEqual(rc, 0, err)
        self.assertTrue(out['restored'])
        self.assertTrue(out['afterServer']['cores']['core']['halted'])
        self.assertFalse(out['after']['cores']['core']['halted'])
        self.assertEqual(sum(s['argv'][0] == '-device' for s in self.sessions()), 3)
        # openocd: a restoring batch polls and resumes each halted core
        rc, out, err = self.run_cli('max32', out='out2', FAKE_GDB_NORESUME='1')
        self.assertEqual(rc, 0, err)
        self.assertTrue(out['restored'])
        self.assertIn('arp_poll', self.sessions()[-2]['argv'][-1])
        rc, out, err = self.run_cli('max32', out='out3', FAKE_GDB_NORESUME='1', FAKE_RESTORE_FAIL='1')
        self.assertEqual(rc, 3)
        self.assertIn('restoring session: openocd: resume failed', out['error'])
        self.assertIn('max32665.cpu found running, now halted', out['error'])
        self.assertIn('resume a halted core', out['error'])

    def test_a_failed_start_anchor_restarts_only_a_core_found_running(self):
        rc, out, err = self.run_cli('k64f', *LINK, FAKE_READS_MAX_ONCE='1')
        self.assertEqual(rc, 3)
        self.assertIn('run state before it is unknown', out['error'])
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual((self.d / 'state').read_text(), '00030003')
        (self.d / 'state.cut').unlink()
        (self.d / 'state').unlink()
        rc, out, err = self.run_cli('k64f', *LINK, out='out2', FAKE_READS_MAX_ONCE='2')
        self.assertEqual(rc, 3)
        self.assertIn('found running and a second session restarted it', out['error'])
        self.assertEqual((self.d / 'state').read_text(), '01010001')
        (self.d / 'state.cut').unlink()
        (self.d / 'state').unlink()
        rc, out, err = self.run_cli('k64f', *LINK, out='out3', FAKE_READS_MAX_ONCE='2', FAKE_DHCSR='01030003')
        self.assertEqual(rc, 3)
        self.assertIn('found halted and stays halted', out['error'])
        self.assertEqual((self.d / 'state').read_text(), '00030003')
        self.assertFalse(any(s['script'] in ('gdb', 'server') for s in self.sessions()))

    def test_openocd_server_and_multi_core_states(self):
        rc, out, err = self.run_cli('max32', FAKE_CURRENT='rp2040.core0', FAKE_CORES='rp2040.core1=00040001')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.gdb_commands()[-3:], ['monitor foreach t [target names] {$t arp_poll}',
                                                    'monitor resume', 'detach'])
        self.assertIn('info threads', self.gdb_commands())
        self.assertIn('gdb port', ' '.join(out['server']))
        self.assertNotIn('gdb_port disabled', out['server'])
        self.assertIn('gdb memory_map disable', out['server'])
        self.assertEqual(set(out['before']['cores']), {'rp2040.core0', 'rp2040.core1'})
        rc, out, err = self.run_cli('max32', out='out2', FAKE_CURRENT='rp2040.core0',
                                    FAKE_CORES='rp2040.core1=00030003')
        self.assertEqual(rc, 3)
        self.assertIn('mixed core states', out['error'])
        self.assertFalse(any(s['script'] in ('gdb', 'server') for s in self.sessions()[-1:]))

    def test_a_server_that_stays_is_stopped_with_sigterm(self):
        rc, out, err = self.run_cli('max32', FAKE_SERVER_STAY='1')
        self.assertEqual(rc, 0, err)

    def test_failures_exit_3_and_still_check_the_end(self):
        rc, out, err = self.run_cli('k64f', *LINK, FAKE_GDB_ATTACH_FAIL='1')
        self.assertEqual(rc, 3)
        self.assertIn('gdb session incomplete (exit 1, no DHCSR, PC, ICSR, backtrace)', out['error'])
        self.assertIn('after', out)
        rc, out, err = self.run_cli('k64f', *LINK, '--timeout', '1', out='out2', FAKE_GDB_HANG='1')
        self.assertEqual(rc, 3)
        self.assertIn('did not finish within 1 s', out['error'])
        rc, out, err = self.run_cli('k64f', *LINK, '--dump', '0x20000000', '16', str(self.d / 'd.bin'), out='out3',
                                    FAKE_GDB_SHORT='1')
        self.assertEqual(rc, 1)
        self.assertFalse(out['dumps'][0]['complete'])
        rc, out, err = self.run_cli('k64f', *LINK, out='out4', FAKE_DHCSR='01030003', FAKE_GDB_EXIT='42')
        self.assertEqual(rc, 3)
        self.assertIn('gdb session incomplete (exit 42, no PC, ICSR, backtrace)', out['error'])
        rc, out, err = self.run_cli('max32', '--timeout', '1', out='out5', FAKE_GDB_HANG_ATTACHED='1')
        self.assertEqual(rc, 3)
        self.assertIn('did not finish within 1 s', out['error'])
        self.assertTrue(out['restored'])
        self.assertFalse(out['after']['cores']['max32665.cpu']['halted'])

    def test_a_core_found_halted_that_ran_is_reported(self):
        # gdb dies before it can ask the J-Link server not to restart the core at close
        rc, out, err = self.run_cli('k64f', *LINK, '--timeout', '1', FAKE_DHCSR='01030003',
                                    FAKE_GDB_HANG_ATTACHED='1')
        self.assertEqual(rc, 3)
        self.assertIn('core found halted, ran after the attach (afterServer)', out['error'])
        self.assertFalse(out['afterServer']['cores']['core']['halted'])
        self.assertTrue(out['after']['cores']['core']['halted'])
        self.assertNotIn('restored', out)

    def test_an_expression_error_is_reported_from_stderr(self):
        rc, out, err = self.run_cli('k64f', *LINK, '--expr', '1/0', '--expr', 'x',
                                    FAKE_EXPRS='1/0=!Division by zero;x=3')
        self.assertEqual(rc, 1, err)
        self.assertIn('error', out['exprs'][0])
        self.assertEqual(out['exprs'][1], {'expr': 'x', 'output': '3'})
        self.assertEqual(out['gdbStderr'], ['Division by zero'])

    def interrupt(self, board, until, *args, **env):
        proc = subprocess.Popen([sys.executable, str(AUTOPSY), '--hil-config', str(self.config), '--board', board,
                                 '--elf', str(self.elf), '--out', str(self.d / 'out'), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd='/',
                                env={**self.env, **env})
        deadline = time.monotonic() + 20
        while not (self.log.exists() and until(self.sessions())) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=60)
        return proc.returncode, json.loads(stdout), stderr

    def test_a_signal_after_the_anchor_still_reaches_the_end_anchor(self):
        rc, out, err = self.interrupt('k64f', lambda ss: any(s['script'] == 'server' for s in ss), *LINK,
                                      FAKE_SERVER_DELAY='5')
        self.assertEqual(rc, 3, err)
        self.assertIn('interrupted by signal 15', out['error'])
        self.assertFalse(out['after']['cores']['core']['halted'])
        self.assertEqual((self.d / 'state').read_text(), '01010001')

    def test_a_signal_during_the_anchor_reports_an_unknown_state(self):
        rc, out, err = self.interrupt('k64f', lambda ss: True, *LINK, FAKE_JLINK_SLEEP='5')
        self.assertEqual(rc, 3, err)
        self.assertIn('pre-attach anchor: interrupted by signal 15', out['error'])
        self.assertIn('run state before it is unknown', out['error'])

    def test_a_signal_right_after_the_anchor_keeps_the_found_state_policy(self):
        spec = importlib.util.spec_from_file_location('autopsy_inproc', AUTOPSY)
        autopsy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(autopsy)
        real = autopsy.anchor

        class Trap(dict):    # the first look at the anchor's cores is where the signal lands
            fired = False

            def items(self):
                if not Trap.fired:
                    Trap.fired = True
                    raise autopsy.Interrupted('interrupted by signal 15')
                return super().items()

        def anchor(*args, **kw):
            got = real(*args, **kw)
            if not Trap.fired:
                got['cores'] = Trap(got['cores'])
            return got

        argv = ['--hil-config', str(self.config), '--board', 'k64f', '--elf', str(self.elf), '--out',
                str(self.d / 'out'), *LINK]
        stdout = io.StringIO()
        for sig in (signal.SIGTERM, signal.SIGINT):    # main() installs its own handlers
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(autopsy, 'anchor', anchor), \
                contextlib.redirect_stdout(stdout):
            rc = autopsy.main(argv)
        out = json.loads(stdout.getvalue())
        self.assertEqual(rc, 3)
        self.assertIn('interrupted by signal 15', out['error'])
        self.assertNotIn('ran after the attach', out['error'])
        self.assertFalse(out['after']['cores']['core']['halted'])
        self.assertEqual((self.d / 'state').read_text(), '01010001')

    def test_signals_still_run_cleanup_and_the_end_anchor(self):
        proc = subprocess.Popen([sys.executable, str(AUTOPSY), '--hil-config', str(self.config), '--board', 'max32',
                                 '--elf', str(self.elf), '--out', str(self.d / 'out')],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd='/',
                                env={**self.env, 'FAKE_GDB_HANG_ATTACHED': '1'})
        deadline = time.monotonic() + 20
        while not (self.log.exists() and 'gdb' in self.log.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=60)
        out = json.loads(stdout)
        self.assertEqual(proc.returncode, 3, stderr)
        self.assertIn('interrupted by signal 15, 15', out['error'])
        self.assertTrue(out['restored'])
        self.assertFalse(out['after']['cores']['max32665.cpu']['halted'])

    def test_refusals_launch_nothing(self):
        (self.d / 'exists.bin').write_bytes(b'')
        (self.d / 'taken').mkdir()
        cases = [(('--dump', '0x40000000', '4', str(self.d / 'p.bin')), 'not within'),
                 (('--dump', '0x20000000', '4', str(self.d / 'exists.bin')), 'never overwrites'),
                 (('--dump', '0x20000000', '4', str(self.d / 'out' / 'server.log')), 'a file this script writes'),
                 (('--expr', ''), 'one non-empty line'), (('--timeout', '0'), '--timeout')]
        for args, want in cases:
            rc, out, err = self.run_cli('k64f', *LINK, *args)
            self.assertEqual(rc, 2, (args, err))
            self.assertIn(want, err)
        rc, out, err = self.run_cli('k64f', *LINK, out='taken')
        self.assertEqual(rc, 2)
        self.assertIn('give a new directory', err)
        rc, out, err = self.run_cli('pico', '--dump', '0x14000000', '4', str(self.d / 'x.bin'))
        self.assertEqual(rc, 2)
        self.assertIn('RP2040 XIP region', err)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
