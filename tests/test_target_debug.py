"""Tests for the target-debug skill's pc_sample.py: DWT_PCSR samples read through
a fake JLinkExe on PATH (prompt-prefixed output like the real tool) are
histogrammed by function through a fake symbolizer; an incomplete capture, a
missing or broken tool, and a symbolizer answering the wrong number of lines
are explicit failures, never a partial result passed off as a profile."""
import importlib.util
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
# last one repeated; FAKE_DHCSR: the two DHCSR values; FAKE_MODE=truncate stops
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
        text = ('J-Link>E000EDF0 = 00010001\nE000101C = 08001234\n'
                'J-Link>E000101C = FFFFFFFF\nJ-Link>Sleep(5)\nJ-Link>E000EDF0 = 02030003\n')
        self.assertEqual(pc_sample.parse_reads(text),
                         ([0x08001234, 0xFFFFFFFF], [0x00010001, 0x02030003]))

    def test_script_reads_dhcsr_around_the_samples(self):
        s = pc_sample.jlink_script(2, 5).splitlines()
        self.assertEqual(s, ['mem32 E000EDF0, 1', 'mem32 E000101C, 1', 'Sleep 5',
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


if __name__ == '__main__':
    unittest.main()
