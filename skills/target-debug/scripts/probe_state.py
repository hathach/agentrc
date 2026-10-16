#!/usr/bin/env python3
"""What the debug probe sees on a Cortex-M rig board, as one JSON object: the core's
identity (CPUID), DHCSR before and after, DEMCR, N DWT_PCSR samples, and on request
the breakpoint budget registers, static variables read through the ELF, raw memory
regions saved to files, and a read-back of the flashed image compared on the host.
Probe observations only: it never says whether the application works.

  probe_state.py --hil-config <file> --board <name> [--interface swd --speed 4000] \
                 [--samples N] [--interval-ms M] [--allow-halt] [--budget] \
                 [--verify FILE [--base ADDR]] [--elf FILE --read SYMBOL ...] \
                 [--save ADDR LEN FILE ...]

The board's HIL config entry (the target-debug SKILL.md's HIL config table) gives the
backend and probe: flasher `jlink` (args exactly `-device NAME`; --interface and
--speed are the caller's) or `openocd` (args are its -f/-c arguments; vid_pid pins
discovery). Any other flasher, a missing serial or an ambiguous board is refused.

The caller holds the project's board lock for the whole run (target-debug, Delegated
sessions); this script takes no lock and cannot check that one is held. The
connection's own identity (J-Link serial and VTref, CMSIS-DAP serial, ST-Link target
voltage) is reported when the tool prints it, and other probe clients seen in the
process list are reported as a possible conflict, which proves nothing about who
holds the probe.

The script issues no configuration write, but connecting has side effects: OpenOCD's
cortex_m examine sets DHCSR.C_DEBUGEN when clear and replaces DEMCR with TRCENA plus its
own vector-catch bits (cortex_m.c in 0.12.0+dev-g0ce743125), and J-Link Commander V9.78
sets TRCENA at connect. Through them the DEMCR read is the tool's value and the
pre-connect state of those bits is not observable. DWT_PCSR is read without halting, but a
J-Link session halts the core as it closes and restarts it (DFSR.HALTED is set after each
session, V9.78), so a core found halted is running afterwards. With --allow-halt and no
usable sample from a core that was running, a second session halts, reads PC and resumes;
over OpenOCD that is refused on a multi-core target, whose SMP halt stops every core and
whose resume fails for a core OpenOCD never polled.

--budget reads FP_CTRL (BP_CTRL on ARMv6-M) and DWT_CTRL: NUM_CODE is the hardware
breakpoint count, NUMCOMP the watchpoint comparator count. NUMCOMP is reported only
while TRCENA is set; the DWT registers read UNKNOWN otherwise.

--read SYMBOL (repeatable, with --elf) reads a static variable at the address and size
the ELF's symbol table gives it: data objects only, one definition, non-zero size, in
ordinary memory (Code, SRAM or RAM of the M-profile address map; peripheral and system
space refused). Widths 1, 2, 4 and 8 are decoded little-endian; every read keeps its
bytes. The reads are not a snapshot: a running core changes memory between them, and
DHCSR before and after only says whether it ran.

--save ADDR LEN FILE copies a region to a new file (an existing one is refused),
with its length checked and hashed; an incomplete copy is kept and reported. A region
read on a running core is not coherent, and a saved region proves nothing about what
restores the board. The same ordinary-memory rule applies, and it is architectural only:
a vendor device mapped inside Code, SRAM or RAM is the caller's to avoid. On RP2040 the
whole XIP region (0x10000000-0x1FFFFFFF: flash and its aliases, the XIP controller and
SSI; RP2040 datasheet 2.2.2) is refused for --read and --save: flash reads need the
flash-resident halt of `rp2040_verify.py`, which this script does not establish, and the
controller registers have read side effects (XIP STREAM_FIFO).

--verify reads the image's address ranges back over the probe (J-Link savebin, OpenOCD
dump_image) and compares them here: a raw binary needs --base, an ELF32 is compared per
PT_LOAD segment at its physical address.

S_RESET_ST is cleared by every DHCSR read, the tool's own connect included, so
`resetSinceLastRead` before the samples covers only this session's connect; after
them it covers the sampling window.

PROBE_STATE_JLINK_EXE and PROBE_STATE_OPENOCD name the tools when they are not
`JLinkExe` (`JLink.exe` on Windows) and `openocd` on PATH.

Exit: 0 observed; 1 --verify found a difference; 2 bad usage; 3 the probe or tool
failed, the capture or a save was incomplete, the connected probe is not the one
asked for, the core is not M-profile, or a halt read left the core halted.
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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pc_sample import (DEMCR, DEMCR_TRCENA, DHCSR, DWT_PCSR, HIL_PROBE_ROUTES, SENTINEL, SPEED_RE,  # noqa: E402
                       ToolError, _MEM32_RE, hil_flasher, hil_jlink_device)

CPUID = 0xE000ED00
# FP_CTRL (ARMv7-M C1-906, Armv8-M D1.2.109) and BP_CTRL (ARMv6-M C1-352) share the address
FP_CTRL = 0xE0002000
DWT_CTRL = 0xE0001000
# CPUID.ARCHITECTURE [19:16]: 0xC ARMv6-M / Armv8-M Baseline, 0xF ARMv7-M / Armv8-M Mainline
M_PROFILE_ARCH = {0xC: 'armv6-m or armv8-m baseline', 0xF: 'armv7-m or armv8-m mainline'}
# M-profile system address map (ARMv7-M B3-705, ARMv6-M B3-259, Armv8-M B7-265): the Normal-memory
# regions; a vendor can still map a device inside them
ORDINARY_MEMORY = ((0x00000000, 0x1FFFFFFF, 'Code'), (0x20000000, 0x3FFFFFFF, 'SRAM'), (0x60000000, 0x9FFFFFFF, 'RAM'))
RP2040_XIP_REGION = (0x10000000, 0x1FFFFFFF)  # RP2040 datasheet 2.2.2, Table 15
MAX_READ = 64 * 1024
_JLINK_PC_RE = re.compile(r'\bPC = ([0-9A-Fa-f]{8})')
_JLINK_SN_RE = re.compile(r'\bS/N: *(\d+)')
_JLINK_VTREF_RE = re.compile(r'\bVTref=([0-9.]+)V')
_OCD_READ_RE = re.compile(r'^R (\w+) (0x[0-9a-fA-F]+)$', re.M)
_OCD_CURRENT_RE = re.compile(r'^C (\S+)$', re.M)
_OCD_CORE_RE = re.compile(r'^T([BA]) (\S+) (0x[0-9a-fA-F]+)$', re.M)
_OCD_SERIAL_RE = re.compile(r'Serial# = (\S+)')
_OCD_VOLTAGE_RE = re.compile(r'Target voltage: ([0-9.]+)')
_PROBE_CLIENT_RE = re.compile(r'JLink|openocd|pyocd', re.I)
# Paths reach OpenOCD's Tcl and JLinkExe's comma-separated arguments unquoted
SAFE_PATH = re.compile(r'[A-Za-z0-9_./+-]+')
ET_EXEC = 2
STT_OBJECT, STT_FUNC = 1, 2
SHN_UNDEF, SHN_ABS, SHN_COMMON = 0, 0xFFF1, 0xFFF2
SHT_PROGBITS, SHT_NOBITS = 1, 8
SHF_ALLOC, SHF_EXECINSTR = 0x2, 0x4


def dhcsr_state(v):
    bit = lambda n: bool(v >> n & 1)  # noqa: E731
    return {'raw': f'0x{v:08x}', 'halted': bit(17), 'sleeping': bit(18), 'lockup': bit(19),
            'retiredSinceLastRead': bit(24), 'resetSinceLastRead': bit(25)}


def core_state(v):
    return {'raw': f'0x{v:08x}', 'halted': bool(v >> 17 & 1), 'sleeping': bool(v >> 18 & 1)}


def cpuid_state(v):
    arch = v >> 16 & 0xF
    return {'raw': f'0x{v:08x}', 'implementer': f'0x{v >> 24:02x}', 'partno': f'0x{v >> 4 & 0xFFF:03x}',
            'architecture': M_PROFILE_ARCH.get(arch, f'not M-profile (0x{arch:x})')}


def budget_state(fp_ctrl, dwt_ctrl, demcr):
    """NUM_CODE from FP_CTRL bits [7:4] and [14:12] (ARMv7-M and Armv8-M; ARMv6-M's BP_CTRL
    reserves [14:12]), NUMCOMP from DWT_CTRL [31:28], UNKNOWN while TRCENA is 0."""
    out = {'fpCtrl': {'raw': f'0x{fp_ctrl:08x}', 'numCode': (fp_ctrl >> 12 & 0x7) << 4 | fp_ctrl >> 4 & 0xF,
                      'enabled': bool(fp_ctrl & 1)}}
    if demcr & DEMCR_TRCENA:
        out['dwtCtrl'] = {'raw': f'0x{dwt_ctrl:08x}', 'numComp': dwt_ctrl >> 28}
    else:
        out['dwtCtrl'] = {'status': 'dwt-disabled'}
    return out


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


def elf_check(path, data):
    if data[:4] != b'\x7fELF':
        return False
    if len(data) < 52:
        raise ValueError(f'{path}: {len(data)} bytes, shorter than an ELF32 header')
    if data[4] != 1 or data[5] != 1:
        raise ValueError(f'{path}: only little-endian ELF32 is supported')
    return True


def image_segments(path, base):
    """([(address, bytes)], sha256) from one read of the image: a raw binary at --base,
    or an ELF32's PT_LOAD segments with file contents at their physical (load)
    addresses. A malformed or truncated ELF is refused, never compared in part."""
    with open(path, 'rb') as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    if not elf_check(f'--verify {path}', data):
        if base is None:
            raise ValueError(f'--verify {path} is a raw binary: give --base, the address it was flashed at')
        if not data:
            raise ValueError(f'--verify {path} is empty')
        return [(base, data)], digest
    if base is not None:
        raise ValueError('--base is for a raw binary; an ELF carries its own load addresses')
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


def elf_symbols(path):
    """([(name, value, size, type, shndx)], [(sh_type, sh_flags)]) from the ELF32's .symtab and its
    section headers; a relocatable or malformed file is refused."""
    with open(path, 'rb') as f:
        data = f.read()
    if not elf_check(f'--elf {path}', data):
        raise ValueError(f'--elf {path} is not an ELF file')
    e_type, = struct.unpack_from('<H', data, 16)
    if e_type != ET_EXEC:
        raise ValueError(f'--elf {path}: not a linked executable (e_type {e_type}); its symbols carry no addresses')
    shoff, = struct.unpack_from('<I', data, 32)
    shentsize, shnum = struct.unpack_from('<HH', data, 46)
    if not shnum or shentsize < 40 or shoff + shnum * shentsize > len(data):
        raise ValueError(f'--elf {path}: no usable section header table')
    sections = [struct.unpack_from('<10I', data, shoff + i * shentsize) for i in range(shnum)]
    symtabs = [s for s in sections if s[1] == 2]
    if len(symtabs) != 1:
        raise ValueError(f'--elf {path}: {len(symtabs)} symbol tables (a stripped ELF has none)')
    _, _, _, _, off, size, link, _, _, entsize = symtabs[0]
    if entsize != 16 or off + size > len(data) or link >= shnum:
        raise ValueError(f'--elf {path}: malformed symbol table')
    str_off, str_size = sections[link][4], sections[link][5]
    if str_off + str_size > len(data):
        raise ValueError(f'--elf {path}: string table ends past the end of the file')
    strtab = data[str_off:str_off + str_size]
    out = []
    for i in range(size // 16):
        st_name, st_value, st_size, st_info, _other, st_shndx = struct.unpack_from('<IIIBBH', data, off + i * 16)
        if st_name >= len(strtab):
            raise ValueError(f'--elf {path}: symbol {i} names past the string table')
        name = strtab[st_name:strtab.index(b'\0', st_name)].decode('utf-8', 'replace')
        out.append((name, st_value, st_size, st_info & 0xF, st_shndx))
    return out, [(sh[1], sh[2]) for sh in sections]


def memory_class(addr, size):
    """The ordinary-memory region name a range lies in, or None (B3-705's map)."""
    for lo, hi, name in ORDINARY_MEMORY:
        if lo <= addr and addr + size - 1 <= hi:
            return name
    return None


def check_region(what, addr, size, rp2040):
    """The architectural guard: outside Code, SRAM and RAM a read is never ordinary. Inside them
    a vendor may still map a device, which this cannot see beyond RP2040's XIP region; --read
    adds the ELF's section rule, --save relies on the caller's chip memory map."""
    if size <= 0:
        raise ValueError(f'{what}: the length must be positive')
    if addr < 0 or addr + size - 1 > 0xFFFFFFFF:
        raise ValueError(f'{what}: 0x{addr:x}+{size} is outside the 32-bit address space')
    if memory_class(addr, size) is None:
        raise ValueError(f'{what}: 0x{addr:08x}+{size} is not within Code, SRAM or RAM of the M-profile address map; '
                         f'peripheral and system space can have read side effects and is not read here')
    lo, hi = RP2040_XIP_REGION
    if rp2040 and addr <= hi and addr + size - 1 >= lo:
        raise ValueError(f'{what}: 0x{addr:08x}+{size} is in the RP2040 XIP region (flash, the XIP controller, SSI): '
                         f'flash reads need the flash-resident halt of rp2040_verify.py (families.md, "RP2040 flash '
                         f'verification") and the controller registers have read side effects')


def resolve_reads(elf, names, rp2040):
    """[(name, address, size)] for --read symbols: one definition per name, a sized data object
    in a section the linker allocated for data (a variable the image placed in memory, not a
    register the ELF only names), inside ordinary memory."""
    symbols, sections = elf_symbols(elf)
    out = []
    for name in names:
        found = [s for s in symbols if s[0] == name]
        if not found:
            raise ValueError(f'--read {name}: no symbol of that name in {elf}')
        defined = [s for s in found if s[4] not in (SHN_UNDEF, SHN_COMMON)]
        if not defined:
            raise ValueError(f'--read {name}: only undefined references in {elf}')
        if len(defined) > 1:
            places = ', '.join(f'0x{s[1]:08x}+{s[2]}' for s in defined)
            raise ValueError(f'--read {name}: {len(defined)} definitions in {elf} ({places}); ambiguous')
        _, value, size, kind, shndx = defined[0]
        if kind != STT_OBJECT:
            what = 'a function' if kind == STT_FUNC else f'not a data object (type {kind})'
            raise ValueError(f'--read {name}: {what}; --read takes variables only')
        if shndx == SHN_ABS:
            raise ValueError(f'--read {name}: an absolute symbol, not a variable in memory')
        if shndx >= len(sections):
            raise ValueError(f'--read {name}: section index {shndx} is outside {elf}')
        sh_type, sh_flags = sections[shndx]
        if sh_type not in (SHT_PROGBITS, SHT_NOBITS) or not sh_flags & SHF_ALLOC or sh_flags & SHF_EXECINSTR:
            raise ValueError(f'--read {name}: not in a data section the image loads (section type {sh_type}, '
                             f'flags 0x{sh_flags:x}); --read takes variables only')
        if not size:
            raise ValueError(f'--read {name}: zero size in {elf}; its width is unknown')
        if size > MAX_READ:
            raise ValueError(f'--read {name}: {size} bytes; --read is for scalars and small objects, use --save')
        check_region(f'--read {name}', value, size, rp2040)
        out.append((name, value, size))
    return out


def decode_read(name, addr, data):
    out = {'symbol': name, 'address': f'0x{addr:08x}', 'size': len(data), 'bytes': data.hex()}
    if len(data) in (1, 2, 4, 8):
        v = int.from_bytes(data, 'little')
        out['value'] = v
        out['hex'] = f'0x{v:0{2 * len(data)}x}'
    return out


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


_running = []    # the tool run() waits on, for a signal handler that stops it rather than raising


def stop_running():
    """Kill the tool run() is waiting on; run() then fails with its output (safe in a signal handler)."""
    for proc in list(_running):
        try:
            proc.kill()
        except OSError:
            pass


def run(cmd, timeout, stdin=None):
    """The tool's stdout and stderr as one text. Its output goes to a file, so a session that times out
    or is interrupted (any exception, a signal handler's included) still carries what it printed, as
    the exception's .text; the tool is killed then. The timeout covers feeding stdin too."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                                stdout=out, stderr=subprocess.STDOUT)
        _running.append(proc)
        caught = None
        try:
            proc.communicate(None if stdin is None else stdin.encode(), timeout=timeout)
        except BaseException as e:
            caught = e
        while True:
            # a signal handler's exception during this cleanup is kept and the cleanup retried, so the tool
            # is never left running and its output never lost
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                if proc.stdin:
                    proc.stdin.close()
                out.seek(0)
                text = out.read().decode(errors='replace')
                break
            except BaseException as e:
                caught = e
        _running.remove(proc)
    if isinstance(caught, subprocess.TimeoutExpired):
        err = ToolError(f'{os.path.basename(cmd[0])} did not finish within {timeout:.0f} s')
        err.text = text
        raise err from caught
    if caught is not None:
        caught.text = text
        raise caught
    if proc.returncode != 0:
        err = ToolError(f'{os.path.basename(cmd[0])} exited {proc.returncode}; last output:\n{tail(text)}')
        err.text = text
        raise err
    return text


def tail(text):
    return '\n'.join(text.strip().splitlines()[-6:])


def other_probe_clients():
    """Processes whose command line names a probe tool, ours excluded: a possible conflict,
    never proof of exclusivity. Linux only; elsewhere None."""
    if not os.path.isdir('/proc'):
        return None
    out = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
        except OSError:
            continue
        if cmd and _PROBE_CLIENT_RE.search(cmd.split(' ', 1)[0]):
            out.append({'pid': int(pid), 'command': cmd[:200]})
    return out


class Capture:
    """One observation batch, read back in a fixed order: CPUID, DHCSR, DEMCR, [FP_CTRL,
    DWT_CTRL], N PCSR, DHCSR, [the dumps, DHCSR]."""

    def __init__(self, samples, interval_ms, budget, dumps):
        self.samples, self.interval_ms, self.budget, self.dumps = samples, interval_ms, budget, dumps

    def want(self):
        return [n for kind, n in self.steps() if kind == 'read']

    def steps(self):
        """('read', NAME) | ('sleep', ms) | ('dump', (path, addr, size))."""
        s = [('read', 'CPUID'), ('read', 'DHCSR'), ('read', 'DEMCR')]
        if self.budget:
            s += [('read', 'FPCTRL'), ('read', 'DWTCTRL')]
        for _ in range(self.samples):
            s.append(('read', 'PCSR'))
            if self.interval_ms:
                s.append(('sleep', self.interval_ms))
        s.append(('read', 'DHCSR'))
        if self.dumps:
            s += [('dump', d) for d in self.dumps] + [('read', 'DHCSR')]
        return s

    def unpack(self, values):
        it = iter(values)
        out = {'cpuid': next(it), 'dhcsr0': next(it), 'demcr': next(it)}
        if self.budget:
            out['fpctrl'], out['dwtctrl'] = next(it), next(it)
        out['pcs'] = [next(it) for _ in range(self.samples)]
        out['dhcsr1'] = next(it)
        if self.dumps:
            out['dhcsr2'] = next(it)
        return out


class JLink:
    REG = {'CPUID': CPUID, 'DHCSR': DHCSR, 'DEMCR': DEMCR, 'FPCTRL': FP_CTRL, 'DWTCTRL': DWT_CTRL, 'PCSR': DWT_PCSR}

    def __init__(self, probe, device, interface, speed):
        self.probe = probe
        self.cmd = [tool('PROBE_STATE_JLINK_EXE', 'JLinkExe', 'JLink.exe'), '-device', device,
                    '-SelectEmuBySN', probe, '-if', interface, '-speed', speed, '-autoconnect', '1', '-nogui', '1']
        self.connection = {}

    def _identify(self, text):
        sn, vt = _JLINK_SN_RE.search(text), _JLINK_VTREF_RE.search(text)
        if sn:
            self.connection['serial'] = sn.group(1)
            if sn.group(1).lstrip('0') != self.probe.lstrip('0'):
                raise ToolError(f'connected to J-Link S/N {sn.group(1)}, not the requested {self.probe}')
        if vt:
            self.connection['vtref'] = float(vt.group(1))

    def observe(self, cap, timeout, keep_halted=False):
        """The DLL halts the core when the session closes and restarts it unless keep_halted (V9.78,
        stm32f407disco: DFSR.HALTED set after every session)."""
        forms = {'read': lambda n: f'mem32 {self.REG[n]:X}, 1', 'sleep': lambda ms: f'Sleep {ms}',
                 'dump': lambda d: f'savebin {d[0]}, 0x{d[1]:08X}, 0x{d[2]:X}'}
        lines = ['exec SetRestartOnClose = 0'] if keep_halted else []
        lines += [forms[kind](arg) for kind, arg in cap.steps()]
        want = [self.REG[n] for n in cap.want()]
        try:
            text = run(self.cmd, timeout, '\n'.join(lines + ['qc']) + '\n')
        except ToolError as err:
            self._identify(err.text)    # another probe's reads say nothing about this core
            err.partial = self._in_order(err.text, want)
            raise
        self._identify(text)
        reads = [(int(a, 16), int(v, 16)) for a, v in _MEM32_RE.findall(text)]
        if [a for a, _ in reads] != want:
            err = ToolError(f'incomplete capture: {len(reads)}/{len(want)} reads; last output:\n{tail(text)}')
            err.partial = self._in_order(text, want)
            raise err
        return [v for _, v in reads]

    @staticmethod
    def _in_order(text, want):
        """The values read in the requested order, up to the first gap."""
        out = []
        for (addr, v), w in zip(_MEM32_RE.findall(text), want):
            if int(addr, 16) != w:
                break
            out.append(int(v, 16))
        return out

    def halt_read(self, timeout):
        """Halt, read PC, resume. Commander has no conditionals, so `g` also resumes a
        core that halted by itself since the first session: dhcsrBefore says so."""
        text = run(self.cmd, timeout, f'mem32 {DHCSR:X}, 1\nh\ng\nmem32 {DHCSR:X}, 1\nqc\n')
        self._identify(text)
        reads = [int(v, 16) for a, v in _MEM32_RE.findall(text) if int(a, 16) == DHCSR]
        pc = _JLINK_PC_RE.search(text)
        if len(reads) != 2 or not pc:
            raise ToolError(f'incomplete halt read; last output:\n{tail(text)}')
        return reads[0], int(pc.group(1), 16), reads[1], True


class OpenOCD:
    REG = JLink.REG

    def __init__(self, probe, cfg, vid_pid):
        self.exe, self.probe, self.cfg, self.vid_pid = tool('PROBE_STATE_OPENOCD', 'openocd'), probe, cfg, vid_pid
        self.cmd = self.command()
        self.connection = {}
        self.current, self.others, self.reset_detected = None, {}, False

    def command(self, gdb_port=None):
        # Without the memory map gdb connect probes no flash: the RP2040 probe runs ROM code that
        # exits XIP and never re-enters it, and the resumed core faulted (rp2xxx.c in 0ce743125).
        gdb = ['gdb_port disabled'] if gdb_port is None else [f'gdb port {gdb_port}', '-c', 'gdb memory_map disable']
        cmd = [self.exe, '-c', 'tcl_port disabled', '-c', *gdb, '-c', 'telnet_port disabled']
        if self.vid_pid:
            cmd += ['-c', f'adapter usb vid_pid {self.vid_pid}']
        # a board's own args may disable the gdb port too
        return cmd + ['-c', f'adapter serial {self.probe}',
                      *[x for arg in shlex.split(self.cfg) for x in (gdb if arg == 'gdb_port disabled' else [arg])]]

    # OpenOCD's curstate stays "unknown" until a poll, so the other cores are judged by their own DHCSR;
    # the current core's state is dhcsrBefore/dhcsrAfter, read first so its sticky bits stay this session's
    @staticmethod
    def cores(tag):
        return ('foreach t [target names] {if {$t ne [target current]} '
                f'{{echo [format "T{tag} %s 0x%08x" $t [$t read_memory 0x{DHCSR:08x} 32 1]]}}}}')

    @staticmethod
    def script(body):
        return ('init\nproc rd {name addr} {echo [format "R %s 0x%08x" $name [read_memory $addr 32 1]]}\n'
                f'if {{[catch {{\n{body}\n}} err]}} {{echo "ERROR [string map {{"\\n" " | "}} $err]"}}\nshutdown')

    # A failed halt or PC read still resumes a core found running, then reads DHCSR, before the
    # error reaches the outer catch. A multi-core target is refused: an SMP halt stops every core
    # and the resume cannot restore one OpenOCD never polled halted, leaving it stopped
    # (cortex_m_smp_halt_all, cortex_m_restore_smp in 0.12.0+dev-g0ce743125; RP2040 core1, 2026-10-16).
    HALT_READ = (f'set d [read_memory 0x{DHCSR:08x} 32 1]\necho [format "R DHCSR 0x%08x" $d]\n'
                 'if {[llength [target names]] > 1} {\n'
                 '  error "refused: [llength [target names]] cores ([target names]); an SMP halt stops them all '
                 'and OpenOCD cannot resume a core it has not polled"\n'
                 '}\n'
                 'set was [expr {$d & 0x20000 ? "halted" : "running"}]\n'
                 'set rc [catch {\n'
                 '  if {$was eq "running"} {halt}\n'
                 '  echo [format "R PC 0x%08x" [dict get [get_reg pc] pc]]\n'
                 '} perr]\n'
                 'set rrc [catch {if {$was eq "running"} {resume}} rerr]\n'
                 f'set after [read_memory 0x{DHCSR:08x} 32 1]\n'
                 'echo [format "R DHCSR 0x%08x" $after]\n'
                 'set msg {}\n'
                 'if {$rc} {append msg "halt/pc read: [expr {$perr eq {} ? {failed without a message} : $perr}]; "}\n'
                 'if {$rrc} {append msg "resume: [expr {$rerr eq {} ? {failed without a message} : $rerr}]; "}\n'
                 'if {$msg ne {}} {error [format "%sfound %s, DHCSR after 0x%08x" $msg $was $after]}')

    def batch(self, body, timeout):
        text = run(self.cmd + ['-c', self.script(body)], timeout)
        err = re.search(r'^ERROR (.*)$', text, re.M)
        if err:
            failure = ToolError(f'openocd: {err.group(1)}')
            failure.text = text    # what the session printed, for callers that act on how far it got
            raise failure
        serial, volt = _OCD_SERIAL_RE.search(text), _OCD_VOLTAGE_RE.search(text)
        if serial:
            self.connection['serial'] = serial.group(1)
        if volt:
            self.connection['targetVoltage'] = float(volt.group(1))
        return text, [(n, int(v, 16)) for n, v in _OCD_READ_RE.findall(text)]

    def observe(self, cap, timeout, keep_halted=False):
        """keep_halted is J-Link's: an openocd exit never changes the run state."""
        forms = {'read': lambda n: f'rd {n} 0x{self.REG[n]:08x}',
                 'sleep': lambda ms: f'sleep {ms}',
                 'dump': lambda d: f'dump_image {{{d[0]}}} 0x{d[1]:08x} 0x{d[2]:x}'}
        steps = [forms[kind](arg) for kind, arg in cap.steps()]
        # the other cores after the current one's first DHCSR read (steps[1]), and again at the end
        body = ['echo "C [target current]"'] + steps[:2] + [self.cores('B')] + steps[2:] + [self.cores('A')]
        text, reads = self.batch('\n'.join(body), timeout)
        if [n for n, _ in reads] != cap.want():
            raise ToolError(f'incomplete capture: {len(reads)}/{len(cap.want())} reads; last output:\n{tail(text)}')
        current = _OCD_CURRENT_RE.search(text)
        listed = _OCD_CORE_RE.findall(text)
        before = {n: int(v, 16) for tag, n, v in listed if tag == 'B'}
        after = {n: int(v, 16) for tag, n, v in listed if tag == 'A'}
        if not current or set(before) != set(after) or current.group(1) in before:
            raise ToolError(f'incomplete core listing; last output:\n{tail(text)}')
        self.current, self.others = current.group(1), {n: (before[n], after[n]) for n in before}
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
            if not SPEED_RE.fullmatch(a.speed):
                raise ValueError(f'--speed must be kHz, "auto" or "adaptive", got {a.speed!r}')
            backend = JLink(flasher['uid'], device, a.interface, a.speed)
            return route, {'probe': flasher['uid'], 'device': device}, backend
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


def parse_int(p, flag, text):
    try:
        return int(text, 0)
    except ValueError:
        p.error(f'{flag} {text!r} is not a number')


def add_board_args(p):
    p.add_argument('--hil-config', required=True, metavar='FILE', help="the project's HIL config json")
    p.add_argument('--board', required=True, metavar='NAME', help='board name in --hil-config')
    p.add_argument('--interface', choices=['swd', 'jtag'], help='J-Link boards: target interface, no default')
    p.add_argument('--speed', help='J-Link boards: interface speed in kHz, "auto" or "adaptive"; no default')


def is_rp2040(ident):
    return 'rp2040' in (ident.get('device') or ident.get('cfg')).lower()


def parse_regions(p, flag, triples, rp2040, reserved=()):
    """[(absolute path, addr, size)] from ADDR LEN FILE triples, or p.error(); whether a file
    exists is the caller's check."""
    regions = []
    for addr_s, len_s, path in triples:
        addr, size = parse_int(p, flag, addr_s), parse_int(p, flag, len_s)
        path = os.path.abspath(path)
        try:
            check_region(f'{flag} {path}', addr, size, rp2040)
        except ValueError as e:
            p.error(str(e))
        if not SAFE_PATH.fullmatch(path):
            p.error(f'{flag} {path}: the path has characters the probe tools would misread '
                    f'(letters, digits, _ . / + - only)')
        if path in [r[0] for r in regions] or path in reserved:
            p.error(f'{flag} {path}: named twice, or a file this script writes itself')
        regions.append((path, addr, size))
    return regions


def check_m_profile(cpuid):
    if cpuid >> 16 & 0xF not in M_PROFILE_ARCH:
        raise ToolError(f'the target does not identify as an M-profile core (CPUID 0x{cpuid:08x}): '
                        f'DHCSR and DWT reads are not interpreted')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_board_args(p)
    p.add_argument('--samples', type=int, default=16, help='DWT_PCSR reads (default 16)')
    p.add_argument('--interval-ms', type=int, default=0, help='pause between reads, ms (default 0)')
    p.add_argument('--allow-halt', action='store_true',
                   help='without a usable PCSR sample from a running core, halt, read PC and resume')
    p.add_argument('--budget', action='store_true', help='read FP_CTRL and DWT_CTRL: breakpoint and watchpoint counts')
    p.add_argument('--verify', metavar='FILE', help='compare the target memory with this ELF or raw binary')
    p.add_argument('--base', metavar='ADDR', help='flash address of a raw --verify binary, e.g. 0x08000000')
    p.add_argument('--elf', metavar='FILE', help='the flashed ELF whose symbol table resolves --read')
    p.add_argument('--read', metavar='SYMBOL', action='append', default=[],
                   help='read this variable at the address and size --elf gives it (repeatable)')
    p.add_argument('--save', metavar=('ADDR', 'LEN', 'FILE'), nargs=3, action='append', default=[],
                   help='copy LEN bytes from ADDR to a new FILE (repeatable)')
    p.add_argument('--timeout', type=float, help='bound on each tool run in seconds (default: scaled to the work)')
    a = p.parse_args(argv)
    if a.samples <= 0 or a.interval_ms < 0:
        p.error('--samples must be positive, --interval-ms non-negative')
    if a.timeout is not None and not (math.isfinite(a.timeout) and a.timeout > 0):
        p.error('--timeout must be a finite positive number of seconds')
    if a.base is not None and a.verify is None:
        p.error('--base goes with --verify')
    if a.read and not a.elf:
        p.error('--read needs --elf, the flashed ELF that places the symbol')
    if a.elf and not a.read:
        p.error('--elf without --read does nothing; --verify takes the image itself')
    base = None if a.base is None else parse_int(p, '--base', a.base)
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
    rp2040 = is_rp2040(ident)
    reads = []
    try:
        if a.read:
            reads = resolve_reads(a.elf, a.read, rp2040)
    except OSError as e:
        p.error(f'--elf {a.elf}: {e}')
    except ValueError as e:
        p.error(str(e))
    saves = parse_regions(p, '--save', a.save, rp2040)
    created = []
    for path, _, _ in saves:
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            created.append(path)
        except OSError as e:
            for done in created:
                os.unlink(done)
            p.error(f'--save {path}: exists; a save never overwrites' if isinstance(e, FileExistsError)
                    else f'--save {path}: {e}')
    result = {'board': a.board, 'backend': route, **ident}
    clients = other_probe_clients()
    if clients:
        result['otherProbeClients'] = clients
    nbytes = sum(len(d) for _, d in segments) + sum(s for _, _, s in reads) + sum(s for _, _, s in saves)
    timeout = a.timeout or 30 + a.samples * (a.interval_ms + 50) / 1000 + nbytes / 10000

    def fail(msg):
        result['error'] = msg
        if saves:
            result['saves'] = [save_record(path, addr, size) for path, addr, size in saves]
        print(json.dumps(result))
        return 3

    with tempfile.TemporaryDirectory(prefix='probe_state.') as tmp:
        if not SAFE_PATH.fullmatch(tmp):
            return fail(f'temporary directory {tmp!r} has unsafe characters')
        dumps = [(os.path.join(tmp, f'seg{i}.bin'), addr, len(d)) for i, (addr, d) in enumerate(segments)]
        dumps += [(os.path.join(tmp, f'read{i}.bin'), addr, size) for i, (_, addr, size) in enumerate(reads)]
        dumps += saves
        try:
            cap = Capture(a.samples, a.interval_ms, a.budget, dumps)
            t0 = time.monotonic()
            vals = cap.unpack(backend.observe(cap, timeout))
            result['sessionMs'] = round((time.monotonic() - t0) * 1000)
            readback = []
            for path, _, _ in dumps[:len(segments) + len(reads)]:
                with open(path, 'rb') as f:
                    readback.append(f.read())
        except (ToolError, OSError) as e:
            return fail(str(e))
    if backend.connection:
        result['connection'] = backend.connection
    result['cpuid'] = cpuid_state(vals['cpuid'])
    try:
        check_m_profile(vals['cpuid'])
    except ToolError as e:
        return fail(str(e))
    demcr = vals['demcr']
    result.update(dhcsrBefore=dhcsr_state(vals['dhcsr0']), demcr={'raw': f'0x{demcr:08x}',
                                                                  'dwtEnabled': bool(demcr & DEMCR_TRCENA)})
    if a.budget:
        result['budget'] = budget_state(vals['fpctrl'], vals['dwtctrl'], demcr)
    result.update(pcsr=pcsr_summary(vals['pcs'], demcr), dhcsrAfter=dhcsr_state(vals['dhcsr1']))
    if isinstance(backend, OpenOCD):
        result['resetDetected'] = backend.reset_detected
        result['cores'] = {backend.current: {'current': True, 'before': core_state(vals['dhcsr0']),
                                             'after': core_state(vals.get('dhcsr2', vals['dhcsr1']))},
                           **{n: {'current': False, 'before': core_state(b), 'after': core_state(a)}
                              for n, (b, a) in backend.others.items()}}
    rc = 0
    if dumps:
        result['dhcsrAfterReads'] = dhcsr_state(vals['dhcsr2'])
    if segments:
        rows = []
        for (addr, want), got in zip(segments, readback):
            if len(got) != len(want):
                return fail(f'read back {len(got)} of {len(want)} bytes at 0x{addr:08x}')
            diff = compare(want, got)
            rows.append({'address': f'0x{addr:08x}', 'size': len(want), 'match': diff is None,
                         'firstDiff': None if diff is None else f'0x{addr + diff:08x}'})
        match = all(r['match'] for r in rows)
        result['verify'] = {'file': a.verify, 'sha256': digest, 'result': 'match' if match else 'mismatch',
                            'segments': rows}
        rc = 0 if match else 1
    if reads:
        rows = []
        for (name, addr, size), got in zip(reads, readback[len(segments):]):
            if len(got) != size:
                return fail(f'--read {name}: read back {len(got)} of {size} bytes at 0x{addr:08x}')
            rows.append(decode_read(name, addr, got))
        result['reads'] = {'elf': a.elf, 'snapshot': False, 'values': rows}
    if saves:
        result['saves'] = [save_record(path, addr, size) for path, addr, size in saves]
        if not all(s['complete'] for s in result['saves']):
            return fail('a save is incomplete (kept as written)')
    if a.allow_halt:
        running = not (result['dhcsrBefore']['halted'] or result['dhcsrAfter']['halted'])
        if result['pcsr']['status'] == 'sampled' or not running:
            result['haltRead'] = {'performed': False, 'reason': 'PCSR sampled' if running else 'core halted'}
        else:
            try:
                before, pc, after, resumed = backend.halt_read(timeout)
            except ToolError as e:
                return fail(str(e))
            result['haltRead'] = {'performed': True, 'pc': f'0x{pc:08x}', 'resumed': resumed,
                                  'dhcsrBefore': dhcsr_state(before), 'dhcsrAfter': dhcsr_state(after)}
            if dhcsr_state(after)['halted'] and not dhcsr_state(before)['halted']:
                return fail('the halt read left a core it found running halted')
    print(json.dumps(result))
    return rc


def save_record(path, addr, size):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        return {'file': path, 'address': f'0x{addr:08x}', 'size': size, 'complete': False, 'error': str(e)}
    return {'file': path, 'address': f'0x{addr:08x}', 'size': size, 'bytes': len(data),
            'complete': len(data) == size, 'sha256': hashlib.sha256(data).hexdigest()}


if __name__ == '__main__':
    sys.exit(main())
