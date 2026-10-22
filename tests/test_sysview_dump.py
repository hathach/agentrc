"""Tests for sysview_dump.py: descriptor checks, the WrOff linearization, the J-Link two-session dump through a fake
J-Link Commander holding a memory model (halt kept between sessions, resume only for a core found running, a
descriptor that moves between the reads refused, no reset ever issued), the OpenOCD session through a stand-in
backend, and the CLI's usage checks. Stubs prove the plumbing only; no probe."""
import contextlib
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402
from test_target_debug import BSS, OBJ, elf32, fake_tools  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'target-debug' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('sysview_dump', SCRIPTS / 'sysview_dump.py')
dump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dump)

CB, NAME_AT, RING_AT = 0x2000_0000, 0x0800_1000, 0x2000_1000
CPUID_M4 = 0x410F_C241


def control_block(size, wroff, rdoff=0, max_up=3):
    head = b'SEGGER RTT'.ljust(16, b'\0') + struct.pack('<ii', max_up, 3)
    return head + struct.pack('<6I', 0, 0, 0, 0, 0, 0) + struct.pack('<6I', NAME_AT, RING_AT, size, wroff, rdoff, 0)


# Memory model in FAKE_MEM (json: {"cpuid": v, "halted": bool, "bytes": {addr: hex}, "advance": addr or null}); each
# session's script is appended to FAKE_LOG. DHCSR reads the run state, which h and g change and a session without
# SetRestartOnClose = 0 ends running, as the J-Link DLL does. "advance" bumps the 32-bit word there after the first
# session, as a core that ran between the sessions would. FAKE_NOHALT ignores h, FAKE_NOGO ignores g, FAKE_SHORT
# truncates every savebin of more than 128 bytes.
FAKE_JLINK = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
sn = args[args.index('-SelectEmuBySN') + 1]
mem = json.load(open(os.environ['FAKE_MEM']))
keep_halted = False
blobs = {int(k, 0): bytes.fromhex(v) for k, v in mem['bytes'].items()}
script = sys.stdin.read()
log = os.environ['FAKE_LOG']
sessions = json.load(open(log)) if os.path.exists(log) else []
sessions.append(script)
json.dump(sessions, open(log, 'w'))
print(f'Connecting to J-Link via USB...O.K.\nS/N: {sn}\nVTref=3.300V')
def read(addr, n):
    for base, blob in blobs.items():
        if base <= addr and addr + n <= base + len(blob):
            return blob[addr - base:addr - base + n]
    return b''
for line in script.splitlines():
    if line == 'exec SetRestartOnClose = 0':
        keep_halted = True
    elif line == 'h' and not os.environ.get('FAKE_NOHALT'):
        mem['halted'] = True
    elif line == 'g' and not os.environ.get('FAKE_NOGO'):
        mem['halted'] = False
    m = re.match(r'mem32 ([0-9A-F]+), 1', line)
    if m:
        v = mem['cpuid'] if m.group(1) == 'E000ED00' else (0x00030003 if mem['halted'] else 0x00010001)
        print(f'{m.group(1)} = {v:08X}')
    m = re.match(r'savebin (\S+), 0x([0-9A-F]+), 0x([0-9A-F]+)', line)
    if m:
        data = read(int(m.group(2), 16), int(m.group(3), 16))
        if os.environ.get('FAKE_SHORT') and len(data) > 128:
            data = data[:128]
        open(m.group(1), 'wb').write(data)
if not keep_halted:
    mem['halted'] = False
if mem.get('advance') and len(sessions) == 1:
    at = int(mem['advance'], 0)
    for base, blob in list(blobs.items()):
        if base <= at < base + len(blob):
            off = at - base
            v = int.from_bytes(blob[off:off + 4], 'little') + 4
            blobs[base] = blob[:off] + v.to_bytes(4, 'little') + blob[off + 4:]
    mem['bytes'] = {hex(k): v.hex() for k, v in blobs.items()}
json.dump(mem, open(os.environ['FAKE_MEM'], 'w'))
'''


def setUpModule():
    guard_survivors(__name__)


class Descriptor(unittest.TestCase):
    def test_reads_the_channel(self):
        self.assertEqual(dump.descriptor(control_block(64, 10, 11), 1), (NAME_AT, RING_AT, 64, 10, 11, 0))

    def test_refuses_what_is_not_a_ring(self):
        cases = {'no "SEGGER RTT"': b'NOT RTT'.ljust(0x48, b'\0'),
                 'MaxNumUpBuffers': b'SEGGER RTT'.ljust(16, b'\0') + struct.pack('<ii', 0x7fff, 3) + bytes(48),
                 'has 1 up-buffer': control_block(64, 0, max_up=1),
                 'not a usable ring': control_block(64, 64),
                 'not a usable ring ': control_block(0, 0)}
        for want, header in cases.items():
            with self.subTest(want), self.assertRaisesRegex(ValueError, want.strip()):
                dump.descriptor(header, 1)

    def test_big_endian_layout_refused(self):
        header = b'SEGGER RTT'.ljust(16, b'\0') + struct.pack('>ii', 3, 3) + bytes(48)
        with self.assertRaisesRegex(ValueError, 'little-endian'):
            dump.descriptor(header, 1)


class JLinkDump(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        env = fake_tools(self, self.dir, (('JLinkExe', FAKE_JLINK),))
        # the whole environment, cleared into place: an inherited PROBE_STATE_* override would start a real tool
        self.env = {**env, 'FAKE_MEM': str(self.dir / 'mem.json'), 'FAKE_LOG': str(self.dir / 'log.json')}
        self.out = self.dir / 'out'

    def target(self, ring, wroff, halted=False, cpuid=CPUID_M4, name=b'SysView\0', advance=False):
        mem = {'cpuid': cpuid, 'halted': halted,
               'bytes': {hex(CB): control_block(len(ring), wroff).hex(), hex(RING_AT): ring.hex(),
                         hex(NAME_AT): name.ljust(16, b'\0').hex()},
               'advance': hex(CB + dump.AUP + dump.STRIDE + 12) if advance else None}
        (self.dir / 'mem.json').write_text(json.dumps(mem))

    def run_dump(self, *extra, **env):
        argv = ['--out', str(self.out), '--channel', '1', '--addr', hex(CB), '--probe', '123', '--device', 'X',
                '--interface', 'swd', '--speed', '4000', *extra]
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {**self.env, **env}, clear=True), contextlib.redirect_stdout(out):
            rc = dump.main(argv)
        return rc, json.loads(out.getvalue())

    def sessions(self):
        return json.loads((self.dir / 'log.json').read_text())

    def test_wrapped_ring_is_written_oldest_first(self):
        ring = bytes(range(64))
        self.target(ring, wroff=40)
        rc, r = self.run_dump()
        self.assertEqual(rc, 0, r)
        self.assertEqual((self.out / 'capture.SVDat').read_bytes(), ring[40:] + ring[:40])
        self.assertEqual((self.out / 'ring.bin').read_bytes(), ring)
        self.assertEqual(r['core'], {'before': 'running', 'after': 'halted'})
        self.assertEqual(json.loads((self.out / 'dump.json').read_text())['files']['capture.SVDat']['bytes'], 64)

    def test_unwrapped_ring_puts_the_unwritten_zeros_first(self):
        ring = b'\x0a\x0b\x0c' + bytes(61)
        self.target(ring, wroff=3)
        self.assertEqual(self.run_dump()[0], 0)
        self.assertEqual((self.out / 'capture.SVDat').read_bytes(), bytes(61) + b'\x0a\x0b\x0c')

    def test_halt_is_kept_between_and_after_the_sessions(self):
        self.target(bytes(64), wroff=0)
        self.run_dump()
        s1, s2 = self.sessions()
        for s in (s1, s2):
            self.assertIn('exec SetRestartOnClose = 0', s)
            self.assertNotRegex(s, r'(?m)^(g|r|reset|rx.*)$')
        self.assertLess(s1.index(f'mem32 {dump.ps.DHCSR:X}'), s1.index('\nh\n'))

    def test_without_resume_the_core_stays_halted_after_the_dump(self):
        self.target(bytes(64), wroff=0)
        self.run_dump()
        self.assertTrue(json.loads((self.dir / 'mem.json').read_text())['halted'])

    def test_a_core_that_does_not_halt_is_refused(self):
        self.target(bytes(64), wroff=0)
        rc, r = self.run_dump(FAKE_NOHALT='1')
        self.assertEqual(rc, 3)
        self.assertIn('did not halt', r['error'])

    def test_a_resume_that_fails_is_reported(self):
        self.target(bytes(64), wroff=0)
        rc, r = self.run_dump('--resume', FAKE_NOGO='1')
        self.assertEqual(rc, 3)
        self.assertEqual(r['core'], {'before': 'running', 'after': 'unknown'})
        self.assertIn('not seen running', r['error'])
        self.assertTrue((self.out / 'capture.SVDat').is_file())

    def test_resume_only_a_core_found_running(self):
        self.target(bytes(64), wroff=0)
        rc, r = self.run_dump('--resume')
        self.assertEqual(r['core'], {'before': 'running', 'after': 'running'})
        s2 = self.sessions()[1]
        self.assertRegex(s2, r'(?m)^g$')
        self.assertNotIn('SetRestartOnClose', s2)

    def test_core_found_halted_is_not_resumed(self):
        self.target(bytes(64), wroff=0, halted=True)
        rc, r = self.run_dump('--resume')
        self.assertEqual(r['core'], {'before': 'halted', 'after': 'halted'})
        self.assertNotRegex(self.sessions()[1], r'(?m)^g$')

    def test_descriptor_moved_between_reads_is_refused(self):
        self.target(bytes(64), wroff=0, advance=True)
        rc, r = self.run_dump()
        self.assertEqual(rc, 3)
        self.assertIn('the core ran', r['error'])
        self.assertFalse((self.out / 'capture.SVDat').exists())

    def test_other_buffer_name_is_refused(self):
        self.target(bytes(64), wroff=0, name=b'Terminal\0')
        rc, r = self.run_dump()
        self.assertEqual(rc, 3)
        self.assertIn("not SystemView's", r['error'])

    def test_an_unreadable_buffer_name_is_refused(self):
        self.target(bytes(64), wroff=0)
        mem = json.loads((self.dir / 'mem.json').read_text())
        del mem['bytes'][hex(NAME_AT)]
        (self.dir / 'mem.json').write_text(json.dumps(mem))
        rc, r = self.run_dump()
        self.assertEqual(rc, 3)
        self.assertIn('buffer name: read 0 of 16', r['error'])

    def test_short_ring_read_is_refused(self):
        self.target(bytes(256), wroff=0)
        rc, r = self.run_dump(FAKE_SHORT='1')
        self.assertEqual(rc, 3)
        self.assertIn('read 128 of 256', r['error'])

    def test_not_m_profile_is_refused(self):
        self.target(bytes(64), wroff=0, cpuid=0x4100_0000)
        rc, r = self.run_dump()
        self.assertEqual(rc, 3)
        self.assertIn('M-profile', r['error'])

    def test_elf_gives_the_control_block(self):
        elf = self.dir / 'fw.elf'
        elf.write_bytes(elf32([(0x0800_0000, b'abcd')], [('_SEGGER_RTT', CB, 0x48, OBJ, BSS)]))
        self.target(bytes(range(64)), wroff=8)
        argv = ['--out', str(self.out), '--channel', '1', '--elf', str(elf), '--probe', '123', '--device', 'X',
                '--interface', 'swd', '--speed', '4000']
        with unittest.mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dump.main(argv), 0)


try:
    import tkinter
    _TCL = tkinter.Tcl
except ImportError:
    _TCL = None

# OpenOCD's commands as Tcl stubs over a memory model: read_memory and dump_image read ::mem (a dict of base -> byte
# list), read_memory answering in hex as OpenOCD does ("0x%" PRIx64, target.c); halt and resume change ::state
# unless ::nohalt; every call is logged in ::calls and echo collects ::out.
OPENOCD_STUBS = """
set calls {}; set out {}
proc init {} {}
proc shutdown {} {}
proc echo {s} {lappend ::out $s}
proc target {cmd} {return tgt}
proc tgt {cmd} {lappend ::calls "tgt $cmd"; if {$cmd eq "curstate"} {return $::state}}
proc halt {ms} {lappend ::calls halt; if {!$::nohalt} {set ::state halted}}
proc resume {} {lappend ::calls resume; set ::state running}
proc bytes_at {addr n} {
  dict for {base data} $::mem {
    if {$addr >= $base && $addr + $n <= $base + [llength $data]} {return [lrange $data [expr {$addr - $base}] [expr {$addr - $base + $n - 1}]]}
  }
  error "read of unmapped memory $addr+$n"
}
proc read_memory {addr width n} {
  lappend ::calls "read_memory [format 0x%x $addr] $width $n"
  set b [bytes_at $addr [expr {$n * $width / 8}]]
  set out {}
  if {$width == 8} {
    foreach a $b {lappend out [format 0x%x $a]}
    return $out
  }
  for {set i 0} {$i < [llength $b]} {incr i 4} {
    lassign [lrange $b $i [expr {$i + 3}]] a c d e
    lappend out [format 0x%x [expr {$a | ($c << 8) | ($d << 16) | ($e << 24)}]]
  }
  return $out
}
proc dump_image {path addr n} {
  lappend ::calls "dump_image [file tail $path] [format 0x%x $addr] $n"
  set f [open $path wb]; puts -nonewline $f [binary format c* [bytes_at $addr $n]]; close $f
}
"""


class TclOpenOCD:
    """probe_state.OpenOCD's batch(), run in a Tcl interpreter over OPENOCD_STUBS: the generated body executes."""

    def __init__(self, memory, state='running', nohalt=False):
        self.tcl = _TCL()
        self.tcl.eval(OPENOCD_STUBS)
        mem = ' '.join(f'{base} {{{" ".join(str(b) for b in data)}}}' for base, data in memory.items())
        self.tcl.eval(f'set mem [dict create {mem}]; set state {state}; set nohalt {int(nohalt)}')

    def batch(self, body, timeout):
        self.tcl.eval(dump.ps.OpenOCD.script(body))
        text = self.tcl.eval('join $out "\\n"')
        err = [line for line in text.splitlines() if line.startswith('ERROR ')]
        if err:
            raise dump.ToolError(f'openocd: {err[0][6:]}')
        return text, []

    def calls(self):
        return self.tcl.eval('join $calls "\\n"').splitlines()


@unittest.skipIf(_TCL is None, 'needs a Tcl interpreter (tkinter)')
class OpenOCDDump(unittest.TestCase):
    def memory(self, size=64, wroff=5, name=b'SysView', **cb):
        return {CB: control_block(size, wroff, **cb), RING_AT: bytes(range(size)), NAME_AT: name.ljust(16, b'\0')}

    def run_session(self, fake, resume=False):
        with tempfile.TemporaryDirectory() as tmp:
            return dump.openocd_dump(fake, CB, 1, resume, tmp, 30)

    def test_halts_dumps_and_resumes_only_a_running_core(self):
        for state, resume, after in (('running', False, 'halted'), ('running', True, 'running'),
                                     ('halted', True, 'halted')):
            with self.subTest(state=state, resume=resume):
                fake = TclOpenOCD(self.memory(), state)
                header, d, again, before, got = self.run_session(fake, resume)
                self.assertEqual((before, got, d), (state, after, again))
                self.assertEqual(('halt' in fake.calls()), state == 'running')
                self.assertFalse(any('reset' in c for c in fake.calls()))

    def test_ring_is_read_between_two_reads_of_the_descriptor(self):
        fake = TclOpenOCD(self.memory())
        self.run_session(fake)
        dumps = [c.split()[1] for c in fake.calls() if c.startswith('dump_image')]
        self.assertEqual(dumps, ['hdr1.bin', 'ring.bin', 'hdr2.bin', 'name.bin'])

    def test_no_pointer_is_followed_before_the_descriptor_is_checked(self):
        cases = {'no SEGGER RTT control block': {CB: b'NOT RTT'.ljust(0x48, b'\0')},
                 'MaxNumUpBuffers': self.memory(max_up=1),
                 'not a usable ring': self.memory(wroff=64)}
        for want, memory in cases.items():
            with self.subTest(want):
                fake = TclOpenOCD(memory)
                with self.assertRaisesRegex(dump.ToolError, want):
                    self.run_session(fake)
                self.assertFalse([c for c in fake.calls() if c.startswith('dump_image')])

    def test_a_core_that_does_not_halt_is_refused(self):
        fake = TclOpenOCD(self.memory(), nohalt=True)
        with self.assertRaisesRegex(dump.ToolError, 'did not halt'):
            self.run_session(fake)
        self.assertFalse([c for c in fake.calls() if c.startswith(('read_memory', 'dump_image'))])


class Usage(unittest.TestCase):
    def usage(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            dump.main(list(argv))
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as d:
            full = Path(d) / 'full'
            full.mkdir()
            (full / 'x').write_text('x')
            base = ['--out', str(Path(d) / 'o'), '--channel', '1']
            jl = ['--probe', '1', '--device', 'X', '--interface', 'swd', '--speed', '4000']
            cases = {
                'exactly one of --elf and --addr': base + jl,
                'exactly one of --elf and --addr ': base + ['--elf', 'a', '--addr', '0x1'] + jl,
                '--channel must be': ['--out', 'o', '--channel', '40', '--addr', '0x1'] + jl,
                'needs --device, --interface and --speed': base + ['--addr', '0x1', '--probe', '1', '--device', 'X'],
                'J-Link flags': base + ['--addr', '0x1', '--probe', '1', '--cfg', '-f x', '--speed', '4000'],
                '--vid-pid is for an OpenOCD probe': base + ['--addr', '0x1', '--vid-pid', '0x1 0x2'] + jl,
                'not an empty directory': ['--out', str(full), '--channel', '1', '--addr', '0x1'] + jl,
                'go together': base + ['--addr', '0x1', '--board', 'b'],
            }
            for want, argv in cases.items():
                with self.subTest(want):
                    self.assertIn(want.strip(), self.usage(*argv))


if __name__ == '__main__':
    unittest.main()
