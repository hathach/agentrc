#!/usr/bin/env python3
"""What the debug probe sees on a Cortex-M rig board, as one JSON object: the core's
identity (CPUID), DHCSR before and after, DEMCR, N DWT_PCSR samples, and optionally
a read-back of the flashed image compared on the host. Probe observations only: it
never says whether the application works.

  probe_state.py --hil-config <file> --board <name> [--interface swd --speed 4000] \
                 [--samples N] [--interval-ms M] [--allow-halt] [--verify FILE [--base ADDR]]

The board's HIL config entry (the target-debug SKILL.md's HIL config table) gives the
backend and probe: flasher `jlink` (args exactly `-device NAME`; --interface and
--speed are the caller's) or `openocd` (args are its -f/-c arguments; vid_pid pins
discovery). Any other flasher, a missing serial or an ambiguous board is refused.

The caller holds the project's board lock for the whole run (target-debug, Delegated
sessions); this script takes no lock and cannot check that one is held.

Nothing is written to the target. DWT_PCSR is read without halting; with
--allow-halt and no usable sample from a core that was running, a second session
halts, reads PC and resumes. --verify reads the image's address ranges back over
the probe (J-Link savebin, OpenOCD dump_image) and compares them here: a raw binary
needs --base, an ELF32 is compared per PT_LOAD segment at its physical address.

S_RESET_ST is cleared by every DHCSR read, the tool's own connect included, so
`resetSinceLastRead` before the samples covers only this session's connect; after
them it covers the sampling window.

PROBE_STATE_JLINK_EXE and PROBE_STATE_OPENOCD name the tools when they are not
`JLinkExe` (`JLink.exe` on Windows) and `openocd` on PATH.

Exit: 0 observed; 1 --verify found a difference; 2 bad usage; 3 the probe or tool
failed, the capture was incomplete, the core is not M-profile, or a halt read left the
core halted.
"""
import argparse
import collections
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pc_sample import (DEMCR, DEMCR_TRCENA, DHCSR, DWT_PCSR, HIL_PROBE_ROUTES, SENTINEL,  # noqa: E402
                       _MEM32_RE, hil_flasher, hil_jlink_device)

CPUID = 0xE000ED00
# CPUID.ARCHITECTURE [19:16]: 0xC ARMv6-M / Armv8-M Baseline, 0xF ARMv7-M / Armv8-M Mainline
M_PROFILE_ARCH = {0xC: 'armv6-m or armv8-m baseline', 0xF: 'armv7-m or armv8-m mainline'}
_JLINK_PC_RE = re.compile(r'\bPC = ([0-9A-Fa-f]{8})')
_OCD_READ_RE = re.compile(r'^R (\w+) (0x[0-9a-fA-F]+)$', re.M)
# Paths reach OpenOCD's Tcl and JLinkExe's comma-separated arguments unquoted
SAFE_PATH = re.compile(r'[A-Za-z0-9_./+-]+')


class ToolError(RuntimeError):
    pass


def dhcsr_state(v):
    bit = lambda n: bool(v >> n & 1)  # noqa: E731
    return {'raw': f'0x{v:08x}', 'halted': bit(17), 'sleeping': bit(18), 'lockup': bit(19),
            'retiredSinceLastRead': bit(24), 'resetSinceLastRead': bit(25)}


def cpuid_state(v):
    arch = v >> 16 & 0xF
    return {'raw': f'0x{v:08x}', 'implementer': f'0x{v >> 24:02x}', 'partno': f'0x{v >> 4 & 0xFFF:03x}',
            'architecture': M_PROFILE_ARCH.get(arch, f'not M-profile (0x{arch:x})')}


def pcsr_summary(pcs, demcr):
    """DWT_PCSR samples read against DEMCR: values taken while the DWT is disabled are
    UNKNOWN and are not reported as PCs; all zero is the RAZ of an unimplemented PCSR."""
    out = {'samples': len(pcs)}
    if not demcr & DEMCR_TRCENA:
        return {**out, 'status': 'dwt-disabled'}
    if all(pc == 0 for pc in pcs):
        return {**out, 'status': 'not-implemented'}
    usable = collections.Counter(pc for pc in pcs if pc not in (0, SENTINEL))
    return {**out, 'status': 'sampled' if usable else 'no-address',
            'usable': sum(usable.values()), 'sentinel': pcs.count(SENTINEL), 'zero': pcs.count(0),
            'pcs': {f'0x{pc:08x}': n for pc, n in usable.most_common()}}


def image_segments(path, base):
    """([(address, bytes)], sha256) from one read of the image: a raw binary at --base,
    or an ELF32's PT_LOAD segments with file contents at their physical (load)
    addresses. A malformed or truncated ELF is refused, never compared in part."""
    with open(path, 'rb') as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    if data[:4] != b'\x7fELF':
        if base is None:
            raise ValueError(f'--verify {path} is a raw binary: give --base, the address it was flashed at')
        if not data:
            raise ValueError(f'--verify {path} is empty')
        return [(base, data)], digest
    if base is not None:
        raise ValueError('--base is for a raw binary; an ELF carries its own load addresses')
    if len(data) < 52:
        raise ValueError(f'--verify {path}: {len(data)} bytes, shorter than an ELF32 header')
    if data[4] != 1 or data[5] != 1:
        raise ValueError(f'--verify {path}: only little-endian ELF32 is supported')
    phoff, = struct.unpack_from('<I', data, 28)
    phentsize, phnum = struct.unpack_from('<HH', data, 42)
    if phnum and phentsize < 32:
        raise ValueError(f'--verify {path}: program header entries of {phentsize} bytes, ELF32 needs 32')
    if phoff + phnum * phentsize > len(data):
        raise ValueError(f'--verify {path}: program header table ends past the end of the file')
    segs = []
    for i in range(phnum):
        p_type, p_offset, _vaddr, p_paddr, p_filesz = struct.unpack_from('<5I', data, phoff + i * phentsize)
        if p_type != 1 or not p_filesz:
            continue
        if p_offset + p_filesz > len(data):
            raise ValueError(f'--verify {path}: segment {i} at 0x{p_paddr:08x} ends past the end of the file '
                             f'(truncated ELF?)')
        segs.append((p_paddr, data[p_offset:p_offset + p_filesz]))
    if not segs:
        raise ValueError(f'--verify {path}: no loadable segment with contents')
    return segs, digest


def compare(expected, actual):
    if actual == expected:
        return None
    n = min(len(expected), len(actual))
    return next((i for i in range(n) if expected[i] != actual[i]), n)


def tool(env, posix, windows=None):
    name = os.environ.get(env) or (windows if os.name == 'nt' and windows else posix)
    exe = shutil.which(name)
    if not exe:
        raise ToolError(f'{name} not found - install it or set {env}')
    return exe


def run(cmd, timeout, stdin=None):
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f'{os.path.basename(cmd[0])} did not finish within {timeout:.0f} s') from e
    text = r.stdout + r.stderr
    if r.returncode != 0:
        raise ToolError(f'{os.path.basename(cmd[0])} exited {r.returncode}; last output:\n{tail(text)}')
    return text


def tail(text):
    return '\n'.join(text.strip().splitlines()[-6:])


class JLink:
    def __init__(self, probe, device, interface, speed):
        self.cmd = [tool('PROBE_STATE_JLINK_EXE', 'JLinkExe', 'JLink.exe'), '-device', device,
                    '-SelectEmuBySN', probe, '-if', interface, '-speed', speed, '-autoconnect', '1', '-nogui', '1']

    def observe(self, samples, interval_ms, dumps, timeout):
        lines = [f'mem32 {a:X}, 1' for a in (CPUID, DHCSR, DEMCR)]
        for _ in range(samples):
            lines.append(f'mem32 {DWT_PCSR:X}, 1')
            if interval_ms:
                lines.append(f'Sleep {interval_ms}')
        lines.append(f'mem32 {DHCSR:X}, 1')
        if dumps:
            lines += [f'savebin {path}, 0x{addr:08X}, 0x{size:X}' for path, addr, size in dumps]
            lines.append(f'mem32 {DHCSR:X}, 1')
        text = run(self.cmd, timeout, '\n'.join(lines + ['qc']) + '\n')
        want = [CPUID, DHCSR, DEMCR] + [DWT_PCSR] * samples + [DHCSR] * (2 if dumps else 1)
        reads = [(int(a, 16), int(v, 16)) for a, v in _MEM32_RE.findall(text)]
        if [a for a, _ in reads] != want:
            raise ToolError(f'incomplete capture: {len(reads)}/{len(want)} reads; last output:\n{tail(text)}')
        return [v for _, v in reads]

    def halt_read(self, timeout):
        """Halt, read PC, resume. Commander has no conditionals, so `g` also resumes a
        core that halted by itself since the first session: dhcsrBefore says so."""
        text = run(self.cmd, timeout, f'mem32 {DHCSR:X}, 1\nh\ng\nmem32 {DHCSR:X}, 1\nqc\n')
        reads = [int(v, 16) for a, v in _MEM32_RE.findall(text) if int(a, 16) == DHCSR]
        pc = _JLINK_PC_RE.search(text)
        if len(reads) != 2 or not pc:
            raise ToolError(f'incomplete halt read; last output:\n{tail(text)}')
        return reads[0], int(pc.group(1), 16), reads[1], True


class OpenOCD:
    def __init__(self, probe, cfg, vid_pid):
        self.cmd = [tool('PROBE_STATE_OPENOCD', 'openocd'), '-c', 'tcl_port disabled', '-c', 'gdb_port disabled',
                    '-c', 'telnet_port disabled']
        if vid_pid:
            self.cmd += ['-c', f'adapter usb vid_pid {vid_pid}']
        self.cmd += ['-c', f'adapter serial {probe}', *shlex.split(cfg)]

    @staticmethod
    def script(body):
        return ('init\nproc rd {name addr} {echo [format "R %s 0x%08x" $name [read_memory $addr 32 1]]}\n'
                f'if {{[catch {{\n{body}\n}} err]}} {{echo "ERROR [string map {{"\\n" " | "}} $err]"}}\nshutdown')

    # A failed halt or PC read still resumes a core found running, then reads DHCSR,
    # before the error reaches the outer catch.
    HALT_READ = (f'rd DHCSR 0x{DHCSR:08x}\nset was [[target current] curstate]\n'
                 'set rc [catch {\n'
                 '  if {$was eq "running"} {halt}\n'
                 '  echo [format "R PC 0x%08x" [dict get [get_reg pc] pc]]\n'
                 '} perr]\n'
                 'set rrc [catch {if {$was eq "running"} {resume}} rerr]\n'
                 f'set after [read_memory 0x{DHCSR:08x} 32 1]\n'
                 'echo [format "R DHCSR 0x%08x" $after]\n'
                 'set msg {}\n'
                 'if {$rc} {append msg "halt/pc read: $perr; "}\n'
                 'if {$rrc} {append msg "resume: $rerr; "}\n'
                 'if {$msg ne {}} {error [format "%sfound %s, DHCSR after 0x%08x" $msg $was $after]}')

    def batch(self, body, timeout):
        text = run(self.cmd + ['-c', self.script(body)], timeout)
        err = re.search(r'^ERROR (.*)$', text, re.M)
        if err:
            raise ToolError(f'openocd: {err.group(1)}')
        return text, [(n, int(v, 16)) for n, v in _OCD_READ_RE.findall(text)]

    def observe(self, samples, interval_ms, dumps, timeout):
        body = [f'rd CPUID 0x{CPUID:08x}', f'rd DHCSR 0x{DHCSR:08x}', f'rd DEMCR 0x{DEMCR:08x}']
        for _ in range(samples):
            body.append(f'rd PCSR 0x{DWT_PCSR:08x}')
            if interval_ms:
                body.append(f'sleep {interval_ms}')
        body.append(f'rd DHCSR 0x{DHCSR:08x}')
        if dumps:
            body += [f'dump_image {{{path}}} 0x{addr:08x} 0x{size:x}' for path, addr, size in dumps]
            body.append(f'rd DHCSR 0x{DHCSR:08x}')
        text, reads = self.batch('\n'.join(body), timeout)
        want = ['CPUID', 'DHCSR', 'DEMCR'] + ['PCSR'] * samples + ['DHCSR'] * (2 if dumps else 1)
        if [n for n, _ in reads] != want:
            raise ToolError(f'incomplete capture: {len(reads)}/{len(want)} reads; last output:\n{tail(text)}')
        self.reset_detected = 'external reset detected' in text
        return [v for _, v in reads]

    def halt_read(self, timeout):
        """Halt and resume only a core OpenOCD finds running; a halted one is read as is."""
        text, reads = self.batch(self.HALT_READ, timeout)
        if [n for n, _ in reads] != ['DHCSR', 'PC', 'DHCSR']:
            raise ToolError(f'incomplete halt read; last output:\n{tail(text)}')
        before, pc, after = (v for _, v in reads)
        return before, pc, after, not dhcsr_state(before)['halted']


def resolve(a, p):
    """(route, identity, backend) for the board, or p.error()."""
    try:
        flasher = hil_flasher(a.hil_config, a.board)
        if 'uid' not in flasher:
            raise ValueError(f'--board {a.board}: no flasher.uid (probe serial) in {a.hil_config}')
        route = HIL_PROBE_ROUTES[flasher['name']]
        if flasher['name'] == 'stlink':
            raise ValueError(f'--board {a.board} is flashed over stlink, whose entry carries no OpenOCD '
                             f'configuration; probe_state.py takes jlink and openocd entries')
        if route == 'jlink':
            device = hil_jlink_device(a.board, flasher)
            if device is None:
                raise ValueError(f"--board {a.board}: a jlink entry needs flasher.args '-device NAME'")
            if not (a.interface and a.speed):
                raise ValueError('a J-Link board needs --interface and --speed (its HIL entry has neither)')
            if not re.fullmatch(r'[1-9][0-9]*|auto|adaptive', a.speed):
                raise ValueError(f'--speed must be kHz, "auto" or "adaptive", got {a.speed!r}')
            return route, {'probe': flasher['uid'], 'device': device}, JLink(flasher['uid'], device, a.interface, a.speed)
        if a.interface or a.speed:
            raise ValueError('--interface and --speed are J-Link only; an OpenOCD board takes them from flasher.args')
        if 'args' not in flasher:
            raise ValueError(f'--board {a.board}: an openocd entry needs flasher.args (its -f/-c arguments)')
        vid_pid = flasher.get('vid_pid')
        if vid_pid is not None and not re.fullmatch(r'0x[0-9a-fA-F]{1,4} 0x[0-9a-fA-F]{1,4}', vid_pid.strip()):
            raise ValueError(f'--board {a.board}: flasher.vid_pid must be "0xVVVV 0xPPPP", got {vid_pid!r}')
        try:
            shlex.split(flasher['args'])
        except ValueError as e:
            raise ValueError(f"--board {a.board}: flasher.args {flasher['args']!r}: {e}")
        return route, {'probe': flasher['uid'], 'cfg': flasher['args']}, OpenOCD(flasher['uid'], flasher['args'],
                                                         vid_pid and vid_pid.strip())
    except ValueError as e:
        p.error(str(e))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--hil-config', required=True, metavar='FILE', help="the project's HIL config json")
    p.add_argument('--board', required=True, metavar='NAME', help='board name in --hil-config')
    p.add_argument('--interface', choices=['swd', 'jtag'], help='J-Link boards: target interface, no default')
    p.add_argument('--speed', help='J-Link boards: interface speed in kHz, "auto" or "adaptive"; no default')
    p.add_argument('--samples', type=int, default=16, help='DWT_PCSR reads (default 16)')
    p.add_argument('--interval-ms', type=int, default=0, help='pause between reads, ms (default 0)')
    p.add_argument('--allow-halt', action='store_true',
                   help='without a usable PCSR sample from a running core, halt, read PC and resume')
    p.add_argument('--verify', metavar='FILE', help='compare the target memory with this ELF or raw binary')
    p.add_argument('--base', metavar='ADDR', help='flash address of a raw --verify binary, e.g. 0x08000000')
    p.add_argument('--timeout', type=float, help='bound on each tool run in seconds (default: scaled to the work)')
    a = p.parse_args(argv)
    if a.samples <= 0 or a.interval_ms < 0:
        p.error('--samples must be positive, --interval-ms non-negative')
    if a.timeout is not None and not (math.isfinite(a.timeout) and a.timeout > 0):
        p.error('--timeout must be a finite positive number of seconds')
    if a.base is not None and a.verify is None:
        p.error('--base goes with --verify')
    try:
        base = None if a.base is None else int(a.base, 0)
    except ValueError:
        p.error(f'--base {a.base!r} is not an address')
    segments, digest = [], None
    if a.verify:
        try:
            segments, digest = image_segments(a.verify, base)
        except OSError as e:
            p.error(f'--verify {a.verify}: {e}')
        except ValueError as e:
            p.error(str(e))
    try:
        route, ident, backend = resolve(a, p)
    except ToolError as e:
        print(json.dumps({'board': a.board, 'error': str(e)}))
        return 3
    result = {'board': a.board, 'backend': route, **ident}
    nbytes = sum(len(d) for _, d in segments)
    timeout = a.timeout or 30 + a.samples * (a.interval_ms + 50) / 1000 + nbytes / 10000
    with tempfile.TemporaryDirectory(prefix='probe_state.') as tmp:
        if not SAFE_PATH.fullmatch(tmp):
            print(json.dumps({**result, 'error': f'temporary directory {tmp!r} has unsafe characters'}))
            return 3
        dumps = [(os.path.join(tmp, f'seg{i}.bin'), addr, len(d)) for i, (addr, d) in enumerate(segments)]
        try:
            vals = backend.observe(a.samples, a.interval_ms, dumps, timeout)
            readback = []
            for path, _, size in dumps:
                with open(path, 'rb') as f:
                    readback.append(f.read())
        except (ToolError, OSError) as e:
            print(json.dumps({**result, 'error': str(e)}))
            return 3
    cpuid, dhcsr0, demcr, pcs, dhcsr1 = vals[0], vals[1], vals[2], vals[3:3 + a.samples], vals[3 + a.samples]
    result['cpuid'] = cpuid_state(cpuid)
    if cpuid >> 16 & 0xF not in M_PROFILE_ARCH:
        result['error'] = 'the target does not identify as an M-profile core: DHCSR and DWT reads are not interpreted'
        print(json.dumps(result))
        return 3
    result.update(dhcsrBefore=dhcsr_state(dhcsr0), demcr={'raw': f'0x{demcr:08x}', 'dwtEnabled': bool(demcr & DEMCR_TRCENA)},
                  pcsr=pcsr_summary(pcs, demcr), dhcsrAfter=dhcsr_state(dhcsr1))
    if isinstance(backend, OpenOCD):
        result['resetDetected'] = backend.reset_detected
    rc = 0
    if segments:
        rows = []
        for (addr, want), got in zip(segments, readback):
            if len(got) != len(want):
                print(json.dumps({**result, 'error': f'read back {len(got)} of {len(want)} bytes at 0x{addr:08x}'}))
                return 3
            diff = compare(want, got)
            rows.append({'address': f'0x{addr:08x}', 'size': len(want), 'match': diff is None,
                         'firstDiff': None if diff is None else f'0x{addr + diff:08x}'})
        match = all(r['match'] for r in rows)
        result['verify'] = {'file': a.verify, 'sha256': digest, 'result': 'match' if match else 'mismatch',
                            'segments': rows, 'dhcsrAfter': dhcsr_state(vals[-1])}
        rc = 0 if match else 1
    if a.allow_halt:
        running = not (result['dhcsrBefore']['halted'] or result['dhcsrAfter']['halted'])
        if result['pcsr']['status'] == 'sampled' or not running:
            result['haltRead'] = {'performed': False, 'reason': 'PCSR sampled' if running else 'core halted'}
        else:
            try:
                before, pc, after, resumed = backend.halt_read(timeout)
            except ToolError as e:
                print(json.dumps({**result, 'error': str(e)}))
                return 3
            result['haltRead'] = {'performed': True, 'pc': f'0x{pc:08x}', 'resumed': resumed,
                                  'dhcsrBefore': dhcsr_state(before), 'dhcsrAfter': dhcsr_state(after)}
            if dhcsr_state(after)['halted'] and not dhcsr_state(before)['halted']:
                result['error'] = 'the halt read left a core it found running halted'
                print(json.dumps(result))
                return 3
    print(json.dumps(result))
    return rc


if __name__ == '__main__':
    sys.exit(main())
