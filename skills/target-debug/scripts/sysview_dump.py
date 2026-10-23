#!/usr/bin/env python3
"""Post-mortem SystemView dump: halt the target without a reset, read RTT up-buffer --channel ("SysView") and write
its bytes oldest first, for `sysview_record.py --from-raw` to decode. The contract and the preconditions this script
cannot check (post-mortem firmware, no other RTT reader, an attach that keeps RAM) are the target-debug skill's
sysview.md, "Script interface".

  sysview_dump.py --out DIR --channel N (--elf FILE | --addr ADDR)
                  (--hil-config FILE --board NAME [--interface swd|jtag --speed KHZ]
                   | --probe SN --device NAME --interface swd|jtag --speed KHZ
                   | --probe SN --cfg OPENOCD_ARGS [--vid-pid "0xVVVV 0xPPPP"])
                  [--resume]

The descriptor is read, then the ring, then the descriptor again: a changed descriptor means the core ran between
the reads and the dump is refused. J-Link Commander runs two sessions (it cannot branch on what it read), each with
SetRestartOnClose = 0, since its DLL otherwise restarts the core at session close (probe_state.py, JLink.observe);
it reads DHCSR, so that route is Cortex-M only. OpenOCD runs one session, takes the core state from the target
itself and never changes the run state on exit.

Linearization, from SEGGER_RTT_WriteWithOverwriteNoLock (SEGGER_RTT.c V8): the data is written before WrOff advances,
so WrOff is the next byte to write and the oldest one kept; ring[WrOff:] + ring[:WrOff] is oldest first, before the
first wrap too (the unwritten part is zeros, which decode as SystemView sync/NOP bytes). Post-mortem mode writes a sync
and the Init record periodically (_SendSyncInfo, SEGGER_SYSVIEW.c), which is where decoding starts. A halt inside a
write leaves a partial newest packet at the front, past which the decoder resynchronizes.

PROBE_STATE_JLINK_EXE and PROBE_STATE_OPENOCD name the tools when they are not on PATH.

Exit: 0 dumped; 2 bad usage; 3 the probe or tool failed or is missing, the read was incomplete or incoherent, the
buffer is not SystemView's, a --resume was not seen to take (the dump is written), or SIGTERM or SIGHUP stopped the
session (the tool is killed, the core's state after it unknown unless the tool reported it before the kill).
dump.json's core holds each state as far as it was established, failures included, and "unknown" otherwise.
"""
import argparse
import hashlib
import json
import os
import re
import signal
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_state as ps  # noqa: E402
from probe_state import STOP_SIGNALS, ToolError  # noqa: E402

RTT_MAGIC = b'SEGGER RTT'
AUP = 0x18          # SEGGER_RTT_CB: char acID[16]; int MaxNumUpBuffers; int MaxNumDownBuffers; aUp[]
STRIDE = 0x18       # SEGGER_RTT_BUFFER_UP: sName, pBuffer, SizeOfBuffer, WrOff, RdOff, Flags
MAX_RING = 1 << 20
NAME = b'SysView'   # SEGGER_SYSVIEW_Init_Ex's label for its channel
S_HALT = 1 << 17
S1_READS = [ps.DHCSR, ps.CPUID, ps.DHCSR]    # DHCSR, CPUID, h, DHCSR: CPUID tells the two DHCSR reads apart


def header_len(channel):
    return AUP + (channel + 1) * STRIDE


def descriptor(header, channel):
    """(sName, pBuffer, SizeOfBuffer, WrOff, RdOff, Flags) of aUp[channel], checked; ValueError otherwise."""
    if header[:len(RTT_MAGIC)] != RTT_MAGIC:
        raise ValueError('no "SEGGER RTT" control block at that address: wrong ELF or address, or RAM not '
                         'initialized')
    max_up, = struct.unpack_from('<i', header, 0x10)
    if not 0 < max_up <= 32:
        raise ValueError(f'MaxNumUpBuffers {max_up}: not a 32-bit little-endian RTT control block')
    if channel >= max_up:
        raise ValueError(f'--channel {channel}: the control block has {max_up} up-buffer(s)')
    d = struct.unpack_from('<6I', header, AUP + channel * STRIDE)
    name, buf, size, wroff, rdoff, _ = d
    if not name or not buf or not 0 < size <= MAX_RING or wroff >= size or rdoff >= size or buf + size > 1 << 32:
        raise ValueError(f'up-buffer {channel} is not a usable ring (sName 0x{name:x} pBuffer 0x{buf:x} '
                         f'SizeOfBuffer {size} WrOff {wroff} RdOff {rdoff})')
    return d


def read_file(path, size, what):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        data = b''
    if len(data) != size:
        raise ToolError(f'{what}: read {len(data)} of {size} bytes')
    return data


def halted(dhcsr):
    return bool(dhcsr & S_HALT)


def note_before(core, text):
    """core['before'] from the first session's output, complete or not, once the DHCSR read before the halt and an
    M-profile CPUID are in it."""
    reads = ps.JLink._in_order(text, S1_READS[:2])
    if len(reads) != 2:
        return
    before, cpuid = reads
    try:
        ps.check_m_profile(cpuid)
    except ToolError:
        return
    core['before'] = 'halted' if halted(before) else 'running'


def jlink_dump(backend, cb, channel, resume, tmp, timeout, core):
    """(header, descriptor, descriptor read again) over two Commander sessions, each DHCSR read placed to prove the
    state it claims; core's before and after are set as each is established, so a failure still reports them."""
    hlen, dhcsr = header_len(channel), f'mem32 {ps.DHCSR:X}, 1'
    core.update(before='unknown', after='unknown')
    s1 = ['exec SetRestartOnClose = 0', dhcsr, f'mem32 {ps.CPUID:X}, 1', 'h', dhcsr,
          f'savebin {tmp}/hdr1.bin, 0x{cb:08X}, 0x{hlen:X}', 'qc']
    try:
        text = ps.run(backend.cmd, timeout, '\n'.join(s1) + '\n')
    except ToolError as e:
        try:
            backend._identify(getattr(e, 'text', ''))    # another probe's reads say nothing about this core
        except ToolError as wrong:
            raise ToolError(f'{e}; {wrong}') from e
        note_before(core, getattr(e, 'text', ''))
        raise
    backend._identify(text)
    note_before(core, text)
    reads = ps.JLink._in_order(text, S1_READS)
    if len(reads) != 3:
        raise ToolError(f'incomplete first session: {len(reads)}/3 reads; last output:\n{ps.tail(text)}')
    before, cpuid, after = reads
    ps.check_m_profile(cpuid)
    running = not halted(before)
    if not halted(after):
        raise ToolError(f'the core did not halt (DHCSR 0x{after:08x})')
    core['after'] = 'halted'
    header = read_file(f'{tmp}/hdr1.bin', hlen, 'control block')
    d = descriptor(header, channel)
    go = resume and running
    core['after'] = 'unknown'    # until the second session's reads say otherwise
    s2 = ([] if go else ['exec SetRestartOnClose = 0']) + [
        'h', dhcsr, f'savebin {tmp}/ring.bin, 0x{d[1]:08X}, 0x{d[2]:X}',
        f'savebin {tmp}/hdr2.bin, 0x{cb:08X}, 0x{hlen:X}', f'savebin {tmp}/name.bin, 0x{d[0]:08X}, 0x10', dhcsr] + (
        ['g', dhcsr] if go else []) + ['qc']
    text = ps.run(backend.cmd, timeout + d[2] / 10000, '\n'.join(s2) + '\n')
    states = [int(v, 16) for a, v in ps._MEM32_RE.findall(text) if int(a, 16) == ps.DHCSR]
    if len(states) < 2 or not halted(states[0]) or not halted(states[1]):
        raise ToolError(f'the core was not halted throughout the reads (DHCSR {[hex(v) for v in states]})')
    core['after'] = 'halted'
    if go:
        # a g not seen to take leaves the core to the DLL's restart at close: its final state is not known
        core['after'] = 'running' if len(states) > 2 and not halted(states[2]) else 'unknown'
    again = descriptor(read_file(f'{tmp}/hdr2.bin', hlen, 'control block, second read'), channel)
    return header, d, again


OPENOCD_TCL = '''set t [target current]
$t arp_poll
set was [$t curstate]
echo "STATE $was"
if {[catch {
  if {$was ne "halted"} {halt 1000}
  if {[$t curstate] ne "halted"} {error "the core did not halt: [$t curstate]"}
  if {[binary format c* [read_memory $cb 8 10]] ne "SEGGER RTT"} {error "no SEGGER RTT control block at $cb"}
  set up [read_memory [expr {$cb + 0x10}] 32 1]
  if {$up <= $ch || $up > 32} {error "MaxNumUpBuffers $up: no up-buffer $ch"}
  lassign [read_memory [expr {$cb + 0x18 + $ch * 0x18}] 32 6] name buf size wr rd
  if {!$name || !$buf || !$size || $size > $maxring || $wr >= $size || $rd >= $size || $buf + $size > 0x100000000} {
    error "up-buffer $ch is not a usable ring: $name $buf $size $wr $rd"
  }
  dump_image $tmp/hdr1.bin $cb $hlen
  dump_image $tmp/ring.bin $buf $size
  dump_image $tmp/hdr2.bin $cb $hlen
  dump_image $tmp/name.bin $name 16
  if {$resume && $was eq "running"} {resume}
} err]} {
  catch {echo "AFTER [$t curstate]"}
  error $err
}
echo "AFTER [$t curstate]"'''


def openocd_states(text):
    """{'before': STATE, 'after': AFTER} for those lines the session printed."""
    return {key: m.group(1) for key, tag in (('before', 'STATE'), ('after', 'AFTER'))
            if (m := re.search(rf'^{tag} (\S+)$', text, re.M))}


def openocd_dump(backend, cb, channel, resume, tmp, timeout, core):
    """The same over one OpenOCD session, which checks the descriptor before it follows a pointer in it and takes
    the core state from the target itself, a failed session's included."""
    head = (f'set cb 0x{cb:08x}; set ch {channel}; set hlen 0x{header_len(channel):x}; set maxring {MAX_RING}; '
            f'set resume {int(resume)}; set tmp {{{tmp}}}\n')
    core.update(before='unknown', after='unknown')
    try:
        text, _ = backend.batch(head + OPENOCD_TCL, timeout + MAX_RING / 10000)
    except ToolError as e:
        core.update(openocd_states(getattr(e, 'text', '')))
        raise
    states = openocd_states(text)
    core.update(states)
    if 'before' not in states:
        raise ToolError(f'no core state read; last output:\n{ps.tail(text)}')
    hlen = header_len(channel)
    header = read_file(f'{tmp}/hdr1.bin', hlen, 'control block')
    d = descriptor(header, channel)
    again = descriptor(read_file(f'{tmp}/hdr2.bin', hlen, 'control block, second read'), channel)
    return header, d, again


def on_signal(signum, _frame):
    # raised inside probe_state.run(), which kills the tool session before passing it on
    raise ToolError(f'stopped by signal {signum}')


def resolve_backend(a, p):
    """(route, identity, backend) from the HIL config or the explicit flags, or p.error()."""
    if a.hil_config or a.board:
        if not (a.hil_config and a.board):
            p.error('--hil-config and --board go together')
        if a.probe or a.device or a.cfg or a.vid_pid:
            p.error('--probe, --device, --cfg and --vid-pid come from --hil-config; give one or the other')
        return ps.resolve(a, p)
    if not a.probe or not a.probe.strip():
        p.error('need --probe (or --hil-config with --board)')
    if a.cfg:
        if a.device or a.interface or a.speed:
            p.error('--device, --interface and --speed are J-Link flags; OpenOCD takes its target from --cfg')
        if a.vid_pid and not re.fullmatch(r'0x[0-9a-fA-F]{1,4} 0x[0-9a-fA-F]{1,4}', a.vid_pid.strip()):
            p.error(f'--vid-pid must be "0xVVVV 0xPPPP", got {a.vid_pid!r}')
        return 'openocd', {'probe': a.probe, 'cfg': a.cfg}, ps.OpenOCD(a.probe, a.cfg, a.vid_pid and a.vid_pid.strip())
    if a.vid_pid:
        p.error('--vid-pid is for an OpenOCD probe (--cfg)')
    if not (a.device and a.interface and a.speed):
        p.error('a J-Link needs --device, --interface and --speed (or give --cfg for OpenOCD)')
    if not ps.SPEED_RE.fullmatch(a.speed):
        p.error(f'--speed must be kHz, "auto" or "adaptive", got {a.speed!r}')
    return 'jlink', {'probe': a.probe, 'device': a.device}, ps.JLink(a.probe, a.device, a.interface, a.speed)


def control_block(a, p):
    if bool(a.elf) == bool(a.addr):
        p.error('give exactly one of --elf and --addr')
    if a.addr:
        return ps.parse_int(p, '--addr', a.addr)
    try:
        (_, addr, _), = ps.resolve_reads(a.elf, ['_SEGGER_RTT'], False)
    except OSError as e:
        p.error(f'--elf {a.elf}: {e}')
    except ValueError as e:
        p.error(str(e))
    return addr


def prepare_out(p, path):
    if os.path.exists(path):
        if not os.path.isdir(path) or os.listdir(path):
            p.error(f'--out {path}: exists and is not an empty directory')
    else:
        os.makedirs(path)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', required=True, help='new or empty directory for the dump')
    p.add_argument('--channel', required=True, type=int, help='RTT up-buffer of SystemView ("SysView")')
    p.add_argument('--elf', help='the flashed ELF: _SEGGER_RTT from its symbol table')
    p.add_argument('--addr', help='the RTT control block address instead of --elf')
    p.add_argument('--hil-config', metavar='FILE', help="the project's HIL config json")
    p.add_argument('--board', metavar='NAME', help='board name in --hil-config')
    p.add_argument('--probe', help='J-Link serial, or the OpenOCD adapter serial')
    p.add_argument('--device', help='J-Link device name')
    p.add_argument('--interface', choices=['swd', 'jtag'], help='J-Link: target interface, no default')
    p.add_argument('--speed', help='J-Link: interface speed in kHz, "auto" or "adaptive"; no default')
    p.add_argument('--cfg', help='OpenOCD: its -f/-c arguments for the target')
    p.add_argument('--vid-pid', help='OpenOCD: the adapter USB ids, "0xVVVV 0xPPPP"')
    p.add_argument('--resume', action='store_true', help='resume the core afterwards if it was running')
    p.add_argument('--timeout', type=float, default=30, help='bound on each tool session, seconds (default 30)')
    a = p.parse_args(argv)
    if not 0 <= a.channel < 32:
        p.error('--channel must be 0..31')
    cb = control_block(a, p)
    prepare_out(p, a.out)
    result = {'controlBlock': f'0x{cb:08x}', 'channel': a.channel}

    def finish(rc):
        record = os.path.join(a.out, 'dump.json')
        with open(record + '.tmp', 'w') as f:     # renamed whole, so an interrupted write leaves no partial record
            json.dump(result, f, indent=1)
        os.replace(record + '.tmp', record)
        print(json.dumps(result))
        return rc

    def fail(msg):
        result['error'] = msg
        return finish(3)

    try:
        route, ident, backend = resolve_backend(a, p)
    except ToolError as e:    # a missing probe tool: exit 3, not bad usage
        return fail(str(e))
    result = {'backend': route, **ident, **result}
    clients = ps.other_probe_clients()
    if clients:
        result['otherProbeClients'] = clients

    with tempfile.TemporaryDirectory(prefix='sysview_dump.') as tmp:
        tmp = ps.tool_path(tmp)
        if not ps.SAFE_PATH.fullmatch(tmp):
            return fail(f'temporary directory {tmp!r} has unsafe characters: set TMPDIR')
        dump = jlink_dump if route == 'jlink' else openocd_dump
        previous = {sig: signal.signal(sig, on_signal) for sig in STOP_SIGNALS}
        error, core = None, {}
        try:
            t0 = time.monotonic()
            header, d, again = dump(backend, cb, a.channel, a.resume, tmp, a.timeout, core)
            result['sessionMs'] = round((time.monotonic() - t0) * 1000)
            ring = read_file(f'{tmp}/ring.bin', d[2], 'ring')
        except (ToolError, ValueError, OSError) as e:
            error = str(e)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        if core:
            result['core'] = core
        if error is not None:    # recorded with on_signal gone: a later stop signal acts as it did before the session
            return fail(error)
        name, buf, size, wroff, rdoff, flags = d
        result['descriptor'] = {'sName': f'0x{name:08x}', 'pBuffer': f'0x{buf:08x}', 'SizeOfBuffer': size,
                                'WrOff': wroff, 'RdOff': rdoff, 'Flags': flags}
        if again != d:
            return fail(f'the descriptor changed between the reads ({d} then {again}): the core ran, the ring is '
                        f'not one snapshot')
        try:
            label = read_file(f'{tmp}/name.bin', 16, 'buffer name').split(b'\0', 1)[0]
        except ToolError as e:
            return fail(str(e))
        if label != NAME:
            return fail(f'up-buffer {a.channel} is {label!r}, not SystemView\'s {NAME!r}')
    capture = ring[wroff:] + ring[:wroff]
    files = {'header.bin': header, 'ring.bin': ring, 'capture.SVDat': capture}
    for fname, data in files.items():
        with open(os.path.join(a.out, fname), 'wb') as f:
            f.write(data)
    result['files'] = {fname: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                       for fname, data in files.items()}
    if a.resume and core['before'] == 'running' and core['after'] != 'running':
        return fail(f'dumped, but the core was not seen running after the resume ({core["after"]})')
    return finish(0)


if __name__ == '__main__':
    sys.exit(main())
