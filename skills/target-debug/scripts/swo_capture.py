#!/usr/bin/env python3
"""Capture raw SWO from a running Cortex-M target and leave its debug state as found (the
target-debug skill's swo-dwt.md); decode the result with swo_decode.py.

  swo_capture.py CONNECTION --caps
  swo_capture.py CONNECTION --traceclk-hz HZ --swo-hz HZ --clock-source TEXT --protocol uart \
                 --duration-ms N --out DIR [--exc] [--pc-sample CYCTAP:POSTPRESET] [--itm-ports SPEC] \
                 [--timestamps --core-hz HZ [--ts-prescale N]] [--watch addr=A|SYM,access=..,emit=..] \
                 [--elf FW.elf] [--setup ENTRY] [--observe-itm | --take-itm] [--take-comparator N] \
                 [--allow-approx-swo]
  swo_capture.py CONNECTION --restore DIR

CONNECTION is --hil-config FILE --board NAME, or explicit flags: a J-Link by --probe SERIAL --device NAME
--interface swd --speed KHZ, an OpenOCD probe by --probe SERIAL --cfg "ARGS" [--vid-pid "0xVVVV 0xPPPP"]. A
board flashed over `stlink` is an OpenOCD probe whose --cfg is the caller's. OpenOCD names the trace unit
object with --swo-object when its configuration creates more than one.

Backends, chosen by measurement (swo-dwt.md): a J-Link runs JLinkGDBServerCLExe -nohalt, whose raw SWO port
streams the capture while the script reads and writes registers over GDB's remote protocol; OpenOCD runs as a
server, its trace unit object streaming to a TCP port and registers going through its Tcl port. The core keeps
running throughout. JLINK_GDBSERVER and OPENOCD name the tools when they are not on PATH.

Clocks, never defaulted: --traceclk-hz is the trace unit's clock and --swo-hz the pin rate; the divisor must be
exact (--allow-approx-swo takes the nearest and records the actual rate), and on a J-Link the rate must also be
one the probe produces (its base / n); the prescaler the probe tool programs is read back and must match.
--timestamps needs --core-hz, the clock of the ITM's local timestamp counter, recorded with --clock-source.

Ownership: before its first write the script reads ITM_TCR, ITM_TER, DWT_CTRL and the comparators. An ITM or
DWT trace source already on belongs to someone else (firmware, another session, a leftover): the capture is
refused, unless --observe-itm (keep the ITM setup as found and capture what it emits; needs a trace unit setup
and a probe start that leave it as it is) or --take-itm (reconfigure ITM and DWT_CTRL for this capture, restored
afterwards). A comparator with FUNCTION != 0 is taken only by --take-comparator N. OpenOCD's connect rewrites
that state before anything can read it: an OpenOCD probe needs --accept-attach-writes, and cannot observe. None
of these overrides a board lock: the caller holds the board through its project's HIL contract (target-debug
Delegated sessions).

Journal: DIR/journal.jsonl gets one line per register before it is written (original value and intended value)
and its read-back after; before a probe tool's start or stop, every register it may write, read back after it.
The capture restores, the first changed last, on success, error, SIGINT and SIGTERM, and fails when a read-back
differs. A run cut short leaves the journal: --restore DIR checks host, probe, CPUID and, through the read-only
segments of the --elf the capture checked against the target, the firmware; a capture without --elf cannot be
restored this way. It restores only registers whose value is one the journal explains since their last restore
(original, written by the capture or read after a probe tool operation); any other value is reported and left
alone.

DIR also gets swo.bin (the raw stream), probe.log (the probe tool's output) and capture.json (configuration, clocks
with their source, identity, before and after register snapshots, byte count, host timing, the capture's errors and
the restore's outcome). Exit 0 captured and restored, 1 a capture or restore that failed (the report says what is
left), 2 bad usage or a refusal before any write.
"""
import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PR_SET_PDEATHSIG = 1

CPUID = 0xE000ED00
DHCSR = 0xE000EDF0
DEMCR = 0xE000EDFC
DEMCR_TRCENA = 1 << 24
ITM_TER = 0xE0000E00
ITM_TPR = 0xE0000E40
ITM_TCR = 0xE0000E80
ITM_CIDR = 0xE0000FF0
DWT_CTRL = 0xE0001000
DWT_COMP = 0xE0001020   # + 16 n; MASK + 4, FUNCTION + 8
TPIU = 0xE0040000
TPIU_CSPSR = TPIU + 0x004
TPIU_ACPR = TPIU + 0x010
TPIU_SPPR = TPIU + 0x0F0
TPIU_FFCR = TPIU + 0x304
TPIU_TYPE = TPIU + 0xFC8
TPIU_CIDR = TPIU + 0xFF0
CID_PREAMBLE, CID_MASK = 0xB105000D, 0xFFFF0FFF   # component class in bits 15:12 (ITM 0xE, TPIU 0x9)

TCR_ITMENA, TCR_TSENA, TCR_SYNCENA, TCR_TXENA = 1, 1 << 1, 1 << 2, 1 << 3
TCR_TRACE_BUS_ID = 1 << 16
TS_PRESCALE = {1: 0, 4: 1, 16: 2, 64: 3}
GTSFREQ = {'128': 1, '8192': 2, 'idle': 3}   # a global timestamp per 2^7 or 2^13 clock ticks, or when idle
CTRL_CYCCNTENA = 1
CTRL_SYNCTAP_24 = 1 << 10
CTRL_SYNCTAP = 3 << 10
CTRL_CYCTAP = 1 << 9
CTRL_PCSAMPLENA = 1 << 12
CTRL_EXCTRCENA = 1 << 16
CTRL_CYCEVTENA = 1 << 22
CTRL_NOPRFCNT = 1 << 24
PROFILE_BITS = {'cpi': 1 << 17, 'exc': 1 << 18, 'sleep': 1 << 19, 'lsu': 1 << 20, 'fold': 1 << 21}   # ...EVTENA
CTRL_TRACE_SOURCES = CTRL_PCSAMPLENA | CTRL_EXCTRCENA | (0x3F << 17)   # sampling, exception trace, event counters
SPPR_NRZ = 2
DBGMCU_CR_F4 = 0xE0042004
JLINK_ITM_TCR = 0x0001000D   # what J-Link's SWO EnableTarget writes to ITM_TCR (measured)
ACPR_MAX = 1 << 13   # Cortex-M4 TPIU_ACPR PRESCALER[12:0]; the architecture allows 16 bits (ARMv7-M C1-43)

# ARMv7-M Table C1-21: (access, emit) -> FUNCTION | EMITRANGE(bit 5) for data trace with DATAVMATCH = 0
E = 1 << 5
CYCMATCH = 1 << 7
WATCH_FUNCTION_V7 = {
    ('rw', 'pc'): 0b0001, ('rw', 'offset'): 0b0001 | E, ('rw', 'val'): 0b0010, ('rw', 'offval'): 0b0010 | E,
    ('rw', 'pcval'): 0b0011,
    ('r', 'val'): 0b1100, ('r', 'offset'): 0b1100 | E, ('r', 'pcval'): 0b1110, ('r', 'offval'): 0b1110 | E,
    ('w', 'val'): 0b1101, ('w', 'offset'): 0b1101 | E, ('w', 'pcval'): 0b1111, ('w', 'offval'): 0b1111 | E,
}
ARMV7M_PARTS = {0xC23: 'Cortex-M3', 0xC24: 'Cortex-M4', 0xC27: 'Cortex-M7'}
ARMV8M_MAIN_PARTS = {0xD21: 'Cortex-M33', 0xD22: 'Cortex-M55', 0xD23: 'Cortex-M85', 0xD31: 'Cortex-M35P'}
ARM_CHINA_ARMV8M_MAIN_PARTS = {0xD24: 'Cortex-M52'}   # implementer 0x63, Arm China
NO_ITM_PARTS = {0xC20: 'Cortex-M0', 0xC60: 'Cortex-M0+', 0xC21: 'Cortex-M1', 0xD20: 'Cortex-M23'}

REG_NAMES = {DEMCR: 'DEMCR', DBGMCU_CR_F4: 'DBGMCU_CR', ITM_TER: 'ITM_TER0', ITM_TPR: 'ITM_TPR', ITM_TCR: 'ITM_TCR',
             DWT_CTRL: 'DWT_CTRL',
             TPIU_CSPSR: 'TPIU_CSPSR', TPIU_ACPR: 'TPIU_ACPR', TPIU_SPPR: 'TPIU_SPPR', TPIU_FFCR: 'TPIU_FFCR'}
for _n in range(4):
    REG_NAMES.update({DWT_COMP + 16 * _n: f'DWT_COMP{_n}', DWT_COMP + 16 * _n + 4: f'DWT_MASK{_n}',
                      DWT_COMP + 16 * _n + 8: f'DWT_FUNCTION{_n}'})

# registers a probe tool writes outside the trace registers when it starts SWO, by part: STM32F4 DBGMCU_CR.TRACE_IOEN
# (J-Link EnableTarget, measured; openocd stm32f4x.cfg's tpiu pre-enable handler)
START_SIDE_EFFECTS = {'stm32f4': [DBGMCU_CR_F4]}
DRAIN_S = 0.3
CLEANING = threading.Event()   # set once a capture's teardown began
READ_CHUNK = 1024
# read-only bits a read-back ignores: ITM_TCR.BUSY; DWT_FUNCTIONn.MATCHED and LNK1ENA (ARMv7-M Table C1-39)
VOLATILE_BITS = {ITM_TCR: 1 << 23}
VOLATILE_BITS.update({DWT_COMP + 16 * n + 8: 1 << 24 | 1 << 9 for n in range(4)})
SIZED = {}   # address -> (bits, key): a register narrower than a word, accessed at its width, `key` ORed into writes


def same(addr, a, b):
    return not (a ^ b) & ~VOLATILE_BITS.get(addr, 0)


class Refused(Exception):
    """Refused before any write: exit 2."""


class ToolError(RuntimeError):
    pass


def reg_name(addr):
    return REG_NAMES.get(addr, f'0x{addr:08x}')


def sibling(name):
    sys.path.insert(0, HERE)
    return __import__(name)


def load_entries():
    with open(os.path.join(HERE, 'swo_boards.json')) as f:
        return json.load(f)['entries']


def size_reg(w):
    """Register a setup entry's register narrower than a word."""
    if 'bits' in w:
        SIZED[int(w['addr'], 0)] = (w['bits'], int(w.get('key', '0'), 0))


def trace_gate(device):
    """The setup write a J-Link device's TPIU needs before any access to it, with its entry's key, or None."""
    for key, entry in load_entries().items():
        if 'jlink_device' in entry and device.upper().startswith(entry['jlink_device']):
            gate = next(w for w in entry['writes'] if w.get('gates_trace_unit'))
            size_reg(gate)
            REG_NAMES[int(gate['addr'], 0)] = gate['name']
            return dict(gate, entry=key)
    return None


def free_ports(n):
    # the copy autopsy.py and esp_session.py keep: importing either drags lib/ modules a copied scripts/ dir lacks
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(('127.0.0.1', 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def connect_when_up(port, proc, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return socket.create_connection(('127.0.0.1', port), timeout=5)
        except OSError:
            if proc.poll() is not None:
                raise ToolError(f'the probe server exited (rc {proc.returncode}) before port {port} opened')
            if time.monotonic() > deadline:
                raise ToolError(f'port {port} did not open within {timeout} s')
            time.sleep(0.1)


def spawn(argv, log):
    """The probe server in its own session, so a terminal's Ctrl-C reaches only this script, which restores
    first; on Linux the server gets SIGTERM when this script dies without stopping it."""
    def die_with_parent():
        ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                            preexec_fn=die_with_parent if sys.platform.startswith('linux') else None)


def stop_process(proc, grace=5):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(grace)


# ---------------------------------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------------------------------

class Rsp:
    """GDB remote serial protocol, the part a register session needs."""

    def __init__(self, sock):
        self.sock, self.buf = sock, b''
        sock.settimeout(10)

    def _packet(self):
        while True:
            self.buf = self.buf.lstrip(b'+')
            if self.buf.startswith(b'$') and b'#' in self.buf and len(self.buf) >= self.buf.index(b'#') + 3:
                j = self.buf.index(b'#')
                body, self.buf = self.buf[1:j].decode('latin-1'), self.buf[j + 3:]
                self.sock.sendall(b'+')
                return body
            if self.buf and not self.buf.startswith(b'$'):
                i = self.buf.find(b'$')
                self.buf = self.buf[i:] if i >= 0 else b''
                continue
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ToolError('the GDB server closed the connection')
            self.buf += chunk

    def cmd(self, body):
        self.sock.sendall(f'${body}#{sum(body.encode()) & 0xFF:02x}'.encode())
        return self._packet()

    def monitor(self, text):
        out, reply = [], self.cmd('qRcmd,' + text.encode().hex())
        while reply != 'OK':
            if reply.startswith('E'):
                raise ToolError(f'monitor {text!r}: {reply}')
            hexpart = reply[1:] if reply.startswith('O') else reply
            try:
                out.append(bytes.fromhex(hexpart).decode('latin-1'))
            except ValueError:
                raise ToolError(f'monitor {text!r}: unexpected reply {reply!r}')
            if not reply.startswith('O'):
                break
            reply = self._packet()
        return ''.join(out).strip()

    def read_block(self, addr, n):
        reply = self.cmd(f'm{addr:x},{n:x}')
        if len(reply) != 2 * n or not re.fullmatch(r'[0-9a-fA-F]*', reply):
            raise ToolError(f'read {n} bytes at 0x{addr:08x}: {reply[:40]!r}')
        return bytes.fromhex(reply)

    def read32(self, addr):
        return int.from_bytes(self.read_block(addr, 4), 'little')

    def write32(self, addr, value):
        reply = self.cmd(f'M{addr:x},4:' + value.to_bytes(4, 'little').hex())
        if reply != 'OK':
            raise ToolError(f'write 0x{addr:08x}: {reply!r}')


class JLinkBackend:
    """JLinkGDBServerCLExe -nohalt: GDB remote protocol for registers, its raw SWO port for the stream."""
    name = 'jlink'
    attach_writes = None
    start_itm_tcr = JLINK_ITM_TCR

    def __init__(self, probe, device, interface, speed):
        self.probe, self.device, self.interface, self.speed = probe, device, interface, speed
        self.family = 'stm32f4' if device.lower().startswith('stm32f4') else None
        self.gate = trace_gate(device)
        self.proc = self.rsp = None

    def identity(self):
        return {'backend': 'jlink', 'probe': self.probe, 'device': self.device}

    def open(self, log):
        exe = os.environ.get('JLINK_GDBSERVER') or shutil.which('JLinkGDBServerCLExe')
        if not exe:
            raise ToolError('JLinkGDBServerCLExe not found (set JLINK_GDBSERVER)')
        self.gdb_port, self.swo_port, telnet = free_ports(3)
        self.proc = spawn(
            [exe, '-select', f'usb={self.probe}', '-device', self.device, '-if', self.interface, '-speed', self.speed,
             '-nohalt', '-noir', '-port', str(self.gdb_port), '-swoport', str(self.swo_port), '-telnetport',
             str(telnet), '-singlerun', '-nogui'], log)
        self.rsp = Rsp(connect_when_up(self.gdb_port, self.proc, 20))

    def read32(self, addr):
        self.check_gate(addr, 4)
        return self.rsp.read32(addr)

    def read_block(self, addr, n):
        self.check_gate(addr, n)
        return self.rsp.read_block(addr, n)

    def write32(self, addr, value):
        self.check_gate(addr, 4)
        self.rsp.write32(addr, value)

    def read_sized(self, addr, bits):
        reply = self.rsp.monitor(f'memU{bits} 0x{addr:x}')
        m = re.search(r'Data = (0x[0-9a-fA-F]+)', reply)
        if not m:
            raise ToolError(f'memU{bits} 0x{addr:08x}: {reply!r}')
        return int(m[1], 16)

    def write_sized(self, addr, value, bits):
        reply = self.rsp.monitor(f'memU{bits} 0x{addr:x} = 0x{value:x}')
        if 'Writing' not in reply:
            raise ToolError(f'memU{bits} 0x{addr:08x} = 0x{value:x}: {reply!r}')

    def gate_open(self):
        g = self.gate
        return self.read_sized(int(g['addr'], 0), g['bits']) & int(g['mask'], 0) == int(g['value'], 0)

    def check_gate(self, addr, n):
        # an access to a TPIU whose clock is stopped never completes, and wedges the AHB-AP until power-off
        if self.gate and addr < TPIU + 0x1000 and addr + n > TPIU and not self.gate_open():
            raise ToolError(f"0x{addr:08x}: {self.gate['name']} stops the trace clock, and a TPIU access would stall "
                            f"the bus (swo-boards.md)")

    def ap_target(self, ap):
        raise Refused(f'a register behind access port {ap} is out of reach of this J-Link route')

    def probe_rates(self):
        """(base Hz, minimum divider) the probe derives its SWO rates from."""
        info = self.rsp.monitor('SWO GetSpeedInfo')
        m = re.search(r'Base frequency:\s*(\d+)\s*Hz,\s*MinDiv:\s*(\d+)', info)
        if not m:
            raise ToolError(f'SWO GetSpeedInfo: unexpected answer {info!r}')
        return int(m[1]), int(m[2])

    def start_swo(self, traceclk, swo_hz, port_mask):
        # EnableTarget refuses mask 0 and writes ITM_TCR and ITM_TER itself; the capture rewrites both after it
        reply = self.rsp.monitor(f'SWO EnableTarget {traceclk} {swo_hz} 0x{port_mask or 1:x} 0')
        if 'success' not in reply:
            raise ToolError(f'SWO EnableTarget: {reply!r}')
        return connect_when_up(self.swo_port, self.proc, 10)

    def stop_swo(self, port_mask):
        reply = self.rsp.monitor(f'SWO DisableTarget 0x{port_mask or 1:x}')
        if 'success' not in reply:
            raise ToolError(f'SWO DisableTarget: {reply!r}')

    def trace_unit_regs(self):
        return [TPIU_CSPSR, TPIU_ACPR, TPIU_SPPR, TPIU_FFCR]

    def close(self):
        if self.rsp:
            with contextlib.suppress(OSError, ToolError):
                self.rsp.sock.sendall(b'$D#44')
            self.rsp.sock.close()
        if self.proc:
            stop_process(self.proc)


class OpenOCDBackend:
    """openocd as a server: its trace unit object streams to a TCP port, registers go through the Tcl port."""
    name = 'openocd'
    # cortex_m_examine (armv7m_trace_itm_config, the FPB setup, cortex_m_dwt_setup), before any command of ours
    attach_writes = ('openocd connects by examining the core, which sets DEMCR.TRCENA, clears every DWT_FUNCTION and '
                     'FPB comparator, rewrites ITM_TCR and ITM_TER (on builds before upstream deferred that, '
                     '0.12.0+dev-g0ce743125 among them) and runs the target configuration\'s examine-end handler, '
                     'all before this script can read them')
    start_itm_tcr = None
    gate = None

    def __init__(self, probe, cfg, vid_pid, swo_object):
        self.probe, self.cfg, self.vid_pid, self.swo_object = probe, cfg, vid_pid, swo_object
        self.family = 'stm32f4' if any(w.endswith('stm32f4x.cfg') for w in shlex.split(cfg)) else None
        self.proc = self.sock = None

    def identity(self):
        return {'backend': 'openocd', 'probe': self.probe, 'cfg': self.cfg, 'swo_object': self.swo_object}

    def open(self, log):
        exe = os.environ.get('OPENOCD') or shutil.which('openocd')
        if not exe:
            raise ToolError('openocd not found (set OPENOCD)')
        self.tcl_port, self.swo_port = free_ports(2)
        pre = ['-c', f'adapter serial {self.probe}']
        if self.vid_pid:
            pre = ['-c', f'adapter usb vid_pid {self.vid_pid}'] + pre   # cmsis-dap/stlink discovery pins
        self.proc = spawn(
            [exe] + self._cfg_with(pre) +
            ['-c', f'tcl port {self.tcl_port}', '-c', 'telnet port disabled', '-c', 'gdb port disabled', '-c', 'init'],
            log)
        self.sock = connect_when_up(self.tcl_port, self.proc, 30)
        self.sock.settimeout(30)
        self.buf = b''
        self._resolve_swo_object()

    def _cfg_with(self, pre):
        # the adapter serial goes after the interface file, before the target file: the first -f is the interface
        words = shlex.split(self.cfg)
        for i in range(len(words) - 1):
            if words[i] == '-f' and 'interface/' in words[i + 1]:
                return words[:i + 2] + pre + words[i + 2:]
        return pre + words

    def tcl(self, cmd):
        self.sock.sendall(cmd.encode() + b'\x1a')
        while b'\x1a' not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ToolError('openocd closed its Tcl connection')
            self.buf += chunk
        reply, self.buf = self.buf.split(b'\x1a', 1)
        return reply.decode('latin-1').strip()

    def _checked(self, cmd):
        reply = self.tcl(f'if {{[catch {{{cmd}}} r]}} {{return "ERR:$r"}} else {{return "OK:$r"}}')
        if not reply.startswith('OK:'):
            raise ToolError(f'openocd {cmd!r}: {reply[4:].strip()}')
        return reply[3:].strip()

    def _resolve_swo_object(self):
        names = self._checked('swo names').split()   # tpiu and swo objects alike
        if self.swo_object:
            if self.swo_object not in names:
                raise Refused(f'--swo-object {self.swo_object}: openocd has {names or "none"}')
        elif len(names) == 1:
            self.swo_object = names[0]
        else:
            raise Refused(f'openocd has {len(names)} trace unit objects ({", ".join(names) or "none"}): name one with '
                          f'--swo-object')
        ap = int(self._checked(f'{self.swo_object} cget -ap-num'), 0)
        self.unit_base = int(self._checked(f'{self.swo_object} cget -baseaddr'), 0)
        self.unit_target = None if ap == 0 else self._mem_ap_target(ap)

    def ap_target(self, ap):
        target = self._mem_ap_target(ap)
        if target is None:
            raise Refused(f'openocd has no mem_ap target on access port {ap}: its configuration must create one')
        return target

    def _mem_ap_target(self, ap):
        for t in self._checked('target names').split():
            with contextlib.suppress(ToolError):
                if self._checked(f'{t} cget -type') == 'mem_ap' and int(self._checked(f'{t} cget -ap-num'), 0) == ap:
                    return t
        return None

    def read32(self, addr, target=None):
        return self.read_sized(addr, 32, target)

    def read_sized(self, addr, bits, target=None):
        cmd = f'read_memory 0x{addr:x} {bits} 1'
        if target:
            cmd = f'set t [target current]; targets {target}; set v [{cmd}]; targets $t; set v'
        return int(self._checked(cmd), 0)

    def read_block(self, addr, n):
        return bytes(int(v, 0) for v in self._checked(f'read_memory 0x{addr:x} 8 {n}').split())

    def write32(self, addr, value, target=None):
        self.write_sized(addr, value, 32, target)

    def write_sized(self, addr, value, bits, target=None):
        cmd = f'write_memory 0x{addr:x} {bits} {{0x{value:x}}}'
        if target:
            cmd = f'set t [target current]; targets {target}; {cmd}; targets $t'
        self._checked(cmd)

    def probe_rates(self):
        return None

    def start_swo(self, traceclk, swo_hz, port_mask):
        self._checked(f'{self.swo_object} configure -protocol uart -traceclk {traceclk} -pin-freq {swo_hz} '
                      f'-output :{self.swo_port}')
        self._checked(f'{self.swo_object} enable')
        return connect_when_up(self.swo_port, self.proc, 10)

    def stop_swo(self, port_mask):
        self._checked(f'{self.swo_object} disable')

    def trace_unit_regs(self):
        """The unit's registers as (address, target) pairs: on a unit behind another AP (STM32H7's SWO on AP2)
        through the mem_ap target the configuration creates; none when there is no such target."""
        if self.unit_target is None and self.unit_base != TPIU:
            return []
        return [(self.unit_base + off, self.unit_target) for off in (0x004, 0x010, 0x0F0, 0x304)]

    def close(self):
        if self.sock:
            with contextlib.suppress(OSError, ToolError):
                self.tcl('shutdown')
            self.sock.close()
        if self.proc:
            stop_process(self.proc)


def unit_reg(backend, reg):
    """(address, target) of a trace unit register entry."""
    return reg if isinstance(reg, tuple) else (reg, None)


def unit_read(backend, reg):
    addr, target = unit_reg(backend, reg)
    at = (target,) if target else ()
    return backend.read_sized(addr, SIZED[addr][0], *at) if addr in SIZED else backend.read32(addr, *at)


def unit_write(backend, reg, value):
    addr, target = unit_reg(backend, reg)
    at = (target,) if target else ()
    if addr in SIZED:
        bits, key = SIZED[addr]
        return backend.write_sized(addr, value | key, bits, *at)
    return backend.write32(addr, value, *at)


def requirement(b, w, writes):
    """The setup write `w` names in `requires` (TRCKCR needs PRCR.PRC0), as a journal records it."""
    if 'requires' not in w:
        return None
    r = next(x for x in writes if x['name'] == w['requires'])
    addr, target = unit_reg(b, setup_reg(b, r))
    return {'reg': addr, 'target': target, 'mask': int(r['mask'], 0), 'value': int(r['value'], 0)}


def meet_requirement(b, e, journal, allowed, entries=None):
    """Before a restore writes `e`'s register, put back, journalled, the write it requires when a restore took it
    back: the requirement is then owed, restored in its turn. Only from a value `allowed` lists for its register
    (one the journal explains): otherwise the reason both are left alone."""
    req = e.get('requires')
    if not req:
        return None
    reg = (req['reg'], req['target']) if req['target'] else req['reg']
    now = unit_read(b, reg)
    if now & req['mask'] == req['value']:
        return None
    if not any(same(req['reg'], now, v) for v in allowed((req['reg'], req['target']))):
        return (f"{e['name']}: needs {reg_name(req['reg'])}, which is 0x{now:08x}, a value this capture did not "
                f"leave; both left alone")
    r = journal.before(reg, now, now & ~req['mask'] | req['value'], 'requirement')
    unit_write(b, reg, r['intended'])
    journal.after(r, unit_read(b, reg))
    if entries is not None:
        entries.append(r)
    return None


def in_tpiu(addr):
    return TPIU <= addr < TPIU + 0x1000


# ---------------------------------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------------------------------

class Journal:
    """One JSON line per register, written and fsynced before the write it describes."""

    def __init__(self, path, header=None):
        """A new journal with `header`, or, without one, more records for the journal at `path`."""
        self.path = path
        self.entries = []
        if header is not None:
            self._append({'journal': 1, **header})

    def _append(self, rec):
        with open(self.path, 'a') as f:
            f.write(json.dumps(rec) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def before(self, reg, original, intended, kind='write', requires=None):
        addr, target = reg if isinstance(reg, tuple) else (reg, None)
        e = {'reg': addr, 'target': target, 'name': reg_name(addr), 'original': original, 'intended': intended,
             'kind': kind}
        if requires:
            e['requires'] = requires
        if addr in SIZED:
            e['bits'], e['key'] = SIZED[addr]
        self._append({'before': e})
        self.entries.append(e)
        return e

    def after(self, e, readback):
        e['readback'] = readback
        self._append({'after': {'reg': e['reg'], 'target': e['target'], 'readback': readback}})

    def restored(self, e, value):
        self._append({'restored': {'reg': e['reg'], 'target': e['target'], 'value': value}})


def load_journal(path):
    """(header, entries, settled): settled maps a register to the number of its entries a restore covered, so
    an entry after that (the probe tool's stop, say) is still owed."""
    header, entries, settled = None, [], {}
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            if 'journal' in rec:
                header = rec
            elif 'before' in rec:
                entries.append(dict(rec['before']))
                if 'bits' in rec['before']:
                    SIZED[rec['before']['reg']] = (rec['before']['bits'], rec['before']['key'])
            elif 'after' in rec:
                for e in reversed(entries):
                    if (e['reg'], e['target']) == (rec['after']['reg'], rec['after']['target']):
                        e['readback'] = rec['after']['readback']
                        break
            elif 'restored' in rec:
                key = (rec['restored']['reg'], rec['restored']['target'])
                settled[key] = sum(1 for e in entries if (e['reg'], e['target']) == key)
    if header is None:
        raise ValueError(f'{path}: no journal header')
    return header, entries, settled


# ---------------------------------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------------------------------

def core_kind(cpuid):
    implementer, part = cpuid >> 24, (cpuid >> 4) & 0xFFF
    if implementer == 0x63 and part in ARM_CHINA_ARMV8M_MAIN_PARTS:
        return 'armv8-m', ARM_CHINA_ARMV8M_MAIN_PARTS[part]
    if implementer != 0x41:
        return 'unknown', f'implementer 0x{implementer:02x}'
    if part in ARMV7M_PARTS:
        return 'armv7-m', ARMV7M_PARTS[part]
    if part in ARMV8M_MAIN_PARTS:
        return 'armv8-m', ARMV8M_MAIN_PARTS[part]
    if part in NO_ITM_PARTS:
        return 'no-itm', NO_ITM_PARTS[part]
    return 'unknown', f'part 0x{part:03x}'


def parse_ports(spec):
    mask = 0
    for part in spec.split(','):
        m = re.fullmatch(r'(\d+)(?:-(\d+))?', part.strip())
        if not m:
            raise ValueError(f'--itm-ports {spec!r}: expected ports 0-31 like 0,1 or 0-3')
        lo, hi = int(m[1]), int(m[2] or m[1])
        if not lo <= hi <= 31:
            raise ValueError(f'--itm-ports {spec!r}: ports are 0-31')
        for n in range(lo, hi + 1):
            mask |= 1 << n
    return mask


def parse_period(flag, text):
    """(CYCTAP, POSTPRESET) from 'C:P', or None."""
    if text is None:
        return None
    m = re.fullmatch(r'([01]):(\d+)', text)
    if not m or int(m[2]) > 15:
        raise ValueError(f'{flag} {text!r}: CYCTAP 0|1 and POSTPRESET 0-15')
    return int(m[1]), int(m[2])


def parse_watch(text, symbols):
    fields = {}
    for part in text.split(','):
        k, sep, v = part.partition('=')
        if not sep or k not in ('addr', 'access', 'emit', 'mask'):
            raise ValueError(f'--watch {text!r}: expected '
                             f'addr=..,access=r|w|rw,emit=val|pc|pcval|offset|offval[,mask=N]')
        fields[k] = v.strip()
    if 'addr' not in fields or 'access' not in fields or 'emit' not in fields:
        raise ValueError(f'--watch {text!r}: addr, access and emit are required')
    key = (fields['access'], fields['emit'])
    if key not in WATCH_FUNCTION_V7:
        raise ValueError(f'--watch {text!r}: no ARMv7-M data trace encoding for access={key[0]} emit={key[1]} '
                         f'(read-only and write-only comparators cannot emit the PC alone)')
    addr = fields['addr']
    if re.fullmatch(r'0x[0-9a-fA-F]+|\d+', addr):
        value, source = int(addr, 0), 'given'
    else:
        if symbols is None:
            raise ValueError(f'--watch addr={addr}: a symbol needs --elf')
        found = [(v, s) for n, v, s, typ, shndx in symbols if n == addr and typ == 1 and shndx]
        if len(found) != 1:
            raise ValueError(f'--watch addr={addr}: {len(found)} data objects of that name in the ELF')
        value, source = found[0][0], f'ELF symbol {addr} ({found[0][1]} bytes)'
    mask = int(fields.get('mask', '0'), 0)
    if not 0 <= mask <= 31 or value & ((1 << mask) - 1):
        raise ValueError(f'--watch {text!r}: mask {mask} must be 0-31 bits and the address aligned to it')
    return {'addr': value, 'addr_source': source, 'mask': mask, 'access': key[0], 'emit': key[1],
            'function': WATCH_FUNCTION_V7[key]}


def check_rates(a, probe_rates):
    """(prescaler, actual swo Hz, notes) or Refused."""
    notes = []
    exact = a.traceclk_hz % a.swo_hz == 0
    prescaler = a.traceclk_hz // a.swo_hz if exact else max(1, round(a.traceclk_hz / a.swo_hz))
    if not 1 <= prescaler <= ACPR_MAX:
        raise Refused(f'--traceclk-hz {a.traceclk_hz} / --swo-hz {a.swo_hz} needs a prescaler of {prescaler}, '
                      f'outside 1..{ACPR_MAX}')
    actual = a.traceclk_hz / prescaler
    if not exact:
        if not a.allow_approx_swo:
            near = [a.traceclk_hz // p for p in range(max(1, prescaler - 2), prescaler + 3)
                    if a.traceclk_hz % p == 0]
            raise Refused(f'--swo-hz {a.swo_hz} does not divide --traceclk-hz {a.traceclk_hz} (exact rates nearby: '
                          f'{near or "none"}); --allow-approx-swo takes {actual:.0f} Hz')
        notes.append(f'SWO rate {actual:.0f} Hz, {abs(actual - a.swo_hz) / a.swo_hz:.2%} from the requested')
    if probe_rates:
        base, mindiv = probe_rates
        if base % a.swo_hz or base // a.swo_hz < mindiv:
            options = [base // n for n in range(mindiv, mindiv + 64)
                       if base % n == 0 and a.traceclk_hz % (base // n) == 0][:6]
            raise Refused(f'the J-Link produces SWO rates of {base} Hz / n (n >= {mindiv}); --swo-hz {a.swo_hz} is '
                          f'not one (exact on both sides: {options or "none"})')
    return prescaler, actual, notes


# ---------------------------------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------------------------------

class Stream:
    """The SWO socket to a file. `error` says why it ended early: the probe tool closed it, or a read or write
    failed, before `ending` was set."""

    def __init__(self, sock, path):
        self.sock, self.f = sock, open(path, 'wb')
        self.bytes, self.first, self.last, self.error = 0, None, None, None
        self.stop, self.ending = threading.Event(), threading.Event()
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    if not self.ending.is_set():
                        self.error = f'the probe tool closed the SWO stream after {self.bytes} bytes'
                    return
                now = time.time()
                self.first = self.first or now
                self.last = now
                self.bytes += len(chunk)
                self.f.write(chunk)
            except socket.timeout:
                continue
            except OSError as e:
                if not self.ending.is_set():
                    self.error = f'SWO stream after {self.bytes} bytes: {e}'
                return

    def close(self):
        self.stop.set()
        with contextlib.suppress(OSError):   # wakes the reader now, not at its next recv timeout
            self.sock.shutdown(socket.SHUT_RDWR)
        self.thread.join(5)
        self.f.close()
        self.sock.close()


def snapshot(backend, regs):
    out = {}
    for r in regs:
        addr, target = unit_reg(backend, r)
        with contextlib.suppress(ToolError):
            out[reg_name(addr) + (f'@{target}' if target else '')] = f'0x{unit_read(backend, r):08x}'
    return out


class Session:
    def __init__(self, backend, journal):
        self.b, self.j = backend, journal

    def write(self, reg, value, kind='write', requires=None):
        original = unit_read(self.b, reg)
        e = self.j.before(reg, original, value, kind, requires)
        unit_write(self.b, reg, value)
        rb = unit_read(self.b, reg)
        self.j.after(e, rb)
        if not same(e['reg'], rb, value):
            raise ToolError(f'{e["name"]}: wrote 0x{value:08x}, read back 0x{rb:08x}')
        return original

    def values(self, key):
        """Every value the journal explains for register `key`: its original, written or read back."""
        es = [e for e in self.j.entries if (e['reg'], e['target']) == key]
        return [v for e in es for v in (e['original'], e['intended'], e.get('readback')) if v is not None]

    def journalled(self, reg):
        addr, target = unit_reg(self.b, reg)
        return any((e['reg'], e['target']) == (addr, target) for e in self.j.entries)

    def original(self, reg):
        addr, target = unit_reg(self.b, reg)
        return next(e['original'] for e in self.j.entries if (e['reg'], e['target']) == (addr, target))

    def baseline(self, regs):
        """Journal `regs` as they are before the first write to any of them: a recovery then knows the whole group
        (a comparator's address, mask and function) whatever write it was cut short at."""
        for r in regs:
            v = unit_read(self.b, r)
            self.j.after(self.j.before(r, v, v, 'baseline'), v)

    @contextlib.contextmanager
    def probe_op(self, regs, op):
        """Journal `regs` before a probe tool operation that may write any of them (intended value unknown), and
        what each reads after it, failed or not."""
        es = [(r, self.j.before(r, unit_read(self.b, r), None, op)) for r in regs]
        try:
            yield
        finally:
            for r, e in es:
                with contextlib.suppress(ToolError, OSError):
                    self.j.after(e, unit_read(self.b, r))

    def restore(self):
        """Each journalled register back to its value before the first change: comparators first, each as a whole
        (restore_comparator), then the rest, the first changed last."""
        firsts = {}
        for e in self.j.entries:
            firsts.setdefault((e['reg'], e['target']), e)
        failures = []
        for n, originals in comparators(firsts):
            try:
                settled, _, problems = restore_comparator(self.b, n, originals)
            except (ToolError, OSError) as err:
                settled, problems = {}, [f'comparator {n}: {err}']
            for addr, value in settled.items():
                self.j.restored(firsts[(addr, None)], value)
            failures += problems
        foreign = set()   # prerequisites found holding a value the journal does not explain: left as found
        for (addr, target), e in reversed(list(firsts.items())):
            if target is None and comparator_of(addr) is not None or (addr, target) in foreign:
                continue
            reg = (addr, target) if target else addr
            try:
                if not same(addr, unit_read(self.b, reg), e['original']):
                    conflict = meet_requirement(self.b, e, self.j, self.values)
                    if conflict:
                        failures.append(conflict)
                        foreign.add((e['requires']['reg'], e['requires']['target']))
                        continue
                    unit_write(self.b, reg, e['original'])
                rb = unit_read(self.b, reg)
            except (ToolError, OSError) as err:
                failures.append(f"{e['name']}: {err}")
                continue
            if same(addr, rb, e['original']):
                self.j.restored(e, rb)
            else:
                failures.append(f"{e['name']}: restored 0x{e['original']:08x}, reads 0x{rb:08x}")
        return failures


def comparator_of(addr):
    """n when addr is DWT_COMPn, DWT_MASKn or DWT_FUNCTIONn (n 0-3), else None."""
    off = addr - DWT_COMP
    return off // 16 if 0 <= off < 64 and off % 16 in (0, 4, 8) else None


def comparators(firsts):
    """[(n, {addr: original})] of the comparators among `firsts` ({(addr, target): first journal entry})."""
    out = {}
    for (addr, target), e in firsts.items():
        if target is None and comparator_of(addr) is not None:
            out.setdefault(comparator_of(addr), {})[addr] = e['original']
    return sorted(out.items())


def restore_comparator(b, n, originals, allowed=None):
    """(settled {addr: value}, report, problems): comparator n's journalled registers (`originals`, {addr: value})
    back as found. With `allowed` ({addr: values the journal explains}) every register is checked first, and one
    value that is neither original nor explained leaves the whole comparator alone. Its function is disarmed and
    verified before its address or mask changes, and restored last."""
    base = DWT_COMP + 16 * n
    func = base + 8
    now = {r: b.read32(r) for r in (base, base + 4, func)}
    if allowed is not None:
        foreign = [r for r, v in originals.items()
                   if not same(r, now[r], v) and not any(same(r, now[r], x) for x in allowed[r])]
        if foreign:
            return {}, [], [f'comparator {n}: ' + ', '.join(f'{reg_name(r)} 0x{now[r]:08x}' for r in foreign) +
                            ' neither as found nor written by this capture; the comparator is left alone']
    if func in originals and originals[func] & 0xF and not {base, base + 4} <= originals.keys():
        return {}, [], [f'comparator {n}: its original address and mask were not journalled, so its function '
                        f'0x{originals[func]:08x} is not re-armed; FUNCTION left at 0x{now[func]:08x}']
    settled, report = {}, []
    moves = [r for r in (base, base + 4) if r in originals and not same(r, now[r], originals[r])]
    if moves and now[func] & 0xF:
        if func not in originals:
            return {}, [], [f'comparator {n}: armed by someone else (FUNCTION 0x{now[func]:08x}); its address and mask '
                            f'are left as they are']
        b.write32(func, 0)
        if b.read32(func) & 0xF:
            return {}, [], [f'comparator {n}: FUNCTION does not disarm; left as it is']
        now[func] = 0
    for r in moves + [func] * (func in originals):
        if not same(r, now[r], originals[r]):
            b.write32(r, originals[r])
            rb = b.read32(r)
            if not same(r, rb, originals[r]):
                return settled, report, [f'{reg_name(r)}: restored 0x{originals[r]:08x}, reads 0x{rb:08x}']
            report.append(f'{reg_name(r)}: 0x{now[r]:08x} -> 0x{rb:08x}')
        else:
            report.append(f'{reg_name(r)}: already 0x{now[r]:08x}')
    settled.update({r: originals[r] for r in originals})
    return settled, report, []


def ctrl_sources(a):
    """Whether the capture turns on trace sources in DWT_CTRL."""
    return bool(a.exc or a.pc_sample or a.cyc_event or a.profile)


def dwt_sources(a):
    return ctrl_sources(a) or bool(a.watch) or a.cycmatch is not None


def plan_config(a, caps, before, itm_set_by_probe=False):
    """(itm_tcr, ter, dwt_ctrl_steps, watches) this capture writes; Refused on a conflict."""
    tcr_before, ter_before, ctrl_before = before[ITM_TCR], before[ITM_TER], before[DWT_CTRL]
    itm_busy = (tcr_before & TCR_ITMENA) and ter_before and not itm_set_by_probe
    dwt_busy = ctrl_before & CTRL_TRACE_SOURCES
    if (itm_busy or dwt_busy) and not (a.observe_itm or a.take_itm):
        raise Refused(f'trace sources are already on (ITM_TCR 0x{tcr_before:08x}, ITM_TER 0x{ter_before:08x}, '
                      f'DWT_CTRL 0x{ctrl_before:08x}): firmware, another session or a leftover uses them; '
                      f'--observe-itm captures them as they are, --take-itm reconfigures them for this capture')
    if a.observe_itm and dwt_busy and ctrl_sources(a):
        raise Refused('--observe-itm keeps DWT_CTRL as found, and it already has trace sources on; --exc, '
                      '--pc-sample, --cyc-event and --profile need --take-itm')
    if a.profile and caps.get('dwt_ctrl', 0) & CTRL_NOPRFCNT:
        raise Refused('--profile: this DWT has no profiling counters (DWT_CTRL.NOPRFCNT)')
    if a.observe_itm:
        if not tcr_before & TCR_ITMENA:
            raise Refused('--observe-itm: ITM is off (ITM_TCR.ITMENA = 0), nothing to observe')
        tcr, ter = tcr_before, ter_before
        if dwt_sources(a) and not tcr & TCR_TXENA:
            raise Refused('--observe-itm: ITM_TCR.TXENA is 0, so DWT packets would not reach the stream; '
                          '--take-itm to change ITM')
        if a.global_ts:
            raise Refused('--observe-itm keeps ITM_TCR as found; --global-ts needs --take-itm')
        if a.timestamps and not tcr & TCR_TSENA:
            raise Refused('--observe-itm: the firmware runs ITM without local timestamps; --take-itm to add them')
    else:
        tcr = TCR_ITMENA | TCR_SYNCENA | TCR_TRACE_BUS_ID
        if dwt_sources(a):
            tcr |= TCR_TXENA
        if a.timestamps:
            tcr |= TCR_TSENA | TS_PRESCALE[a.ts_prescale] << 8
        if a.global_ts:
            tcr |= GTSFREQ[a.global_ts] << 10
        ter = parse_ports(a.itm_ports) if a.itm_ports else 0
    # --take-itm drops sources the capture did not ask for: their packets would read as corruption
    ctrl = (ctrl_before & ~CTRL_TRACE_SOURCES if a.take_itm else ctrl_before) | CTRL_CYCCNTENA
    if not ctrl & CTRL_SYNCTAP:
        ctrl |= CTRL_SYNCTAP_24
    steps = []
    if a.exc:
        ctrl |= CTRL_EXCTRCENA
    for name in a.profile:
        ctrl |= PROFILE_BITS[name]
    period, enable = (a.pc_sample, CTRL_PCSAMPLENA) if a.pc_sample else (a.cyc_event, CTRL_CYCEVTENA)
    if period:
        cyctap, postpreset = period
        # POSTINIT, POSTPRESET and CYCTAP while the POSTCNT timer is off, then one enable bit (ARMv7-M C1-883)
        base = ctrl & ~(CTRL_CYCTAP | 0x1FE | CTRL_PCSAMPLENA | CTRL_CYCEVTENA)
        base |= (CTRL_CYCTAP if cyctap else 0) | postpreset << 5 | postpreset << 1
        steps.append(base)
        ctrl = base | enable
    steps.append(ctrl)
    free = [n for n in range(min(caps['numcomp'], 4))
            if not before[DWT_COMP + 16 * n + 8] & 0xF or n == a.take_comparator]
    watches = []
    if a.cycmatch is not None:   # comparator 0 only (ARMv7-M C1-863): a Data trace PC packet when CYCCNT == COMP0
        if caps.get('dwt_ctrl', 0) & 1 << 25:
            raise Refused('--cycmatch: this DWT has no cycle counter (DWT_CTRL.NOCYCCNT)')
        if 0 not in free:
            raise Refused('--cycmatch needs comparator 0, which is in use: --take-comparator 0 takes it')
        free.remove(0)
        watches.append({'comparator': 0, 'addr': a.cycmatch, 'mask': 0, 'function': CYCMATCH | 0b0001,
                        'cycmatch': a.cycmatch})
    if len(free) < len(a.watch):
        raise Refused(f'{len(a.watch)} watches, {len(free)} free data trace comparators (0-3 with FUNCTION 0, 0 '
                      f'kept for --cycmatch); --take-comparator N takes one in use')
    for w, n in zip(a.watch, free):
        watches.append(dict(w, comparator=n))
    return tcr, ter, steps, watches


def read_before(b, numcomp):
    regs = [DEMCR, ITM_TCR, ITM_TER, ITM_TPR, DWT_CTRL] + [DWT_COMP + 16 * n + k for n in range(min(numcomp, 4))
                                                            for k in (0, 4, 8)]
    return {r: b.read32(r) for r in regs}


def caps_report(b):
    cpuid = b.read32(CPUID)
    kind, part = core_kind(cpuid)
    demcr = b.read32(DEMCR)
    out = {'cpuid': f'0x{cpuid:08x}', 'core': part, 'architecture': kind, 'dhcsr': f'0x{b.read32(DHCSR):08x}',
           'demcr_trcena': bool(demcr & DEMCR_TRCENA)}
    if kind == 'no-itm':
        out['itm'] = out['dwt_trace'] = 'unsupported: no ITM or DWT trace on this core'
        return out
    if not demcr & DEMCR_TRCENA:
        out['note'] = 'DEMCR.TRCENA is 0: DWT and ITM registers read UNKNOWN, so their capabilities are unknown'
        return out
    ctrl = b.read32(DWT_CTRL)
    out.update({'dwt_ctrl': f'0x{ctrl:08x}', 'numcomp': ctrl >> 28,
                'trace_packets': 'unsupported (NOTRCPKT)' if ctrl >> 27 & 1 else 'supported',
                'cyccnt': 'unsupported (NOCYCCNT)' if ctrl >> 25 & 1 else 'supported',
                'profiling_counters': 'unsupported (NOPRFCNT)' if ctrl >> 24 & 1 else 'supported'})
    out['itm'] = 'present' if is_component(b, ITM_CIDR) else 'unknown: no component ID at 0xE0000FF0'
    out['itm_tcr'], out['itm_ter'] = f'0x{b.read32(ITM_TCR):08x}', f'0x{b.read32(ITM_TER):08x}'
    if b.gate and not b.gate_open():
        out['tpiu'] = (f"not read: {b.gate['name']} stops the trace clock, and a TPIU access would stall the bus; "
                       f"--setup {b.gate['entry']} starts it for a capture")
    elif is_component(b, TPIU_CIDR):
        typ = b.read32(TPIU_TYPE)
        out['tpiu'] = {'at': f'0x{TPIU:08x}', 'nrz': bool(typ >> 11 & 1), 'manchester': bool(typ >> 10 & 1),
                       'acpr': f'0x{b.read32(TPIU_ACPR):08x}', 'sppr': f'0x{b.read32(TPIU_SPPR):08x}'}
    else:
        out['tpiu'] = 'not at 0xE0040000: the SWO unit is vendor-placed (OpenOCD names it; swo-dwt.md, board setup)'
    if isinstance(b, OpenOCDBackend):
        out['openocd_trace_unit'] = {'object': b.swo_object, 'base': f'0x{b.unit_base:08x}', 'via': b.unit_target}
    return out


def is_component(b, cidr):
    try:
        cid = sum((b.read32(cidr + 4 * i) & 0xFF) << (8 * i) for i in range(4))
    except ToolError:
        return False
    return cid & CID_MASK == CID_PREAMBLE


def check_setup(b, setup):
    for w in setup['watch']:
        mask, value = int(w['mask'], 0), int(w['value'], 0)
        now = unit_read(b, int(w['addr'], 0))
        if now & mask != value:
            off = ' (a peripheral reads 0 while its clock is off)' if now == 0 else ''
            raise Refused(f"--setup {setup['key']}: {w['name']} is 0x{now:08x}{off}, needs 0x{value:08x} under mask "
                          f"0x{mask:08x}: {w['why']} ({w['ref']})")


def image_regions(path):
    """[(address, bytes)] of the ELF's read-only sections with contents, at their load addresses: the code and
    constants that identify the firmware and do not change while it runs. Sections, not segments: a flasher programs
    sections, and the padding between them inside a segment is not what the target holds."""
    with open(path, 'rb') as f:
        data = f.read()
    if not sibling('probe_state').elf_check(f'--elf {path}', data):
        raise ValueError(f'--elf {path} is not an ELF file')
    phoff, shoff = struct.unpack_from('<II', data, 28)
    phentsize, phnum, shentsize, shnum = struct.unpack_from('<HHHH', data, 42)
    if phnum and phentsize < 32 or shnum and shentsize < 40 or phoff + phnum * phentsize > len(data) \
            or shoff + shnum * shentsize > len(data):
        raise ValueError(f'--elf {path}: malformed program or section header table')
    loads = [struct.unpack_from('<5I', data, phoff + i * phentsize) for i in range(phnum)]
    regions = []
    for i in range(shnum):
        _name, sh_type, sh_flags, _addr, sh_offset, sh_size = struct.unpack_from('<6I', data, shoff + i * shentsize)
        if sh_type != 1 or not sh_flags & 2 or sh_flags & 1 or not sh_size:   # PROGBITS, ALLOC, not WRITE
            continue
        if sh_offset + sh_size > len(data):
            raise ValueError(f'--elf {path}: section {i} ends past the end of the file (truncated ELF?)')
        seg = next((p for p in loads if p[0] == 1 and p[1] <= sh_offset and sh_offset + sh_size <= p[1] + p[4]), None)
        if seg is None:
            raise ValueError(f'--elf {path}: section {i} lies in no loadable segment')
        regions.append((seg[3] + sh_offset - seg[1], data[sh_offset:sh_offset + sh_size]))
    if not regions:
        raise ValueError(f'--elf {path}: no read-only section to identify the firmware by')
    return regions


def read_image(b, regions):
    """sha256 of the target's bytes over [(address, length)]."""
    h = hashlib.sha256()
    for addr, n in regions:
        for at in range(addr, addr + n, READ_CHUNK):
            h.update(b.read_block(at, min(READ_CHUNK, addr + n - at)))
    return h.hexdigest()


def check_image(b, path, regions):
    """The image record the journal keeps, after checking the target runs the ELF's read-only segments."""
    for addr, want in regions:
        for at in range(addr, addr + len(want), READ_CHUNK):
            chunk = want[at - addr:at - addr + READ_CHUNK]
            have = b.read_block(at, len(chunk))
            if have != chunk:
                off = next(i for i, (x, y) in enumerate(zip(chunk, have)) if x != y)
                raise Refused(f'--elf {path} is not the firmware the target runs: they differ at 0x{at + off:08x}')
    h = hashlib.sha256()
    for _, want in regions:
        h.update(want)
    return {'regions': [[addr, len(d)] for addr, d in regions], 'sha256': h.hexdigest()}


def features_on(a):
    """The capture features a setup write may be `for`."""
    return ['global-ts'] if a.global_ts else []


def setup_reg(b, w):
    """A setup write's register: its address, or (address, target) when it sits behind another access port."""
    addr = int(w['addr'], 0)
    return (addr, b.ap_target(w['ap'])) if 'ap' in w else addr


def capture(a, b, out_dir):
    CLEANING.clear()
    cap = {'version': 1, 'stream': 'swo.bin', 'probe': b.identity(), 'host_started': time.time()}
    cpuid = b.read32(CPUID)
    kind, part = core_kind(cpuid)
    if kind not in ('armv7-m', 'armv8-m'):
        raise Refused(f'CPUID 0x{cpuid:08x} ({part}): SWO trace needs an ARMv7-M or Armv8-M Mainline core with ITM')
    if (a.watch or a.cycmatch is not None) and kind != 'armv7-m':
        raise Refused(f'{part} is Armv8-M: its DWT_FUNCTION encoding is not supported by --watch or --cycmatch yet')
    if not b.read32(DEMCR) & DEMCR_TRCENA:
        raise Refused('DEMCR.TRCENA is 0: DWT and ITM read UNKNOWN; the probe tool normally sets it at connect')
    if b.gate and (not a.setup or a.setup['key'] != b.gate['entry']):
        raise Refused(f"{b.device}: its TPIU stalls the bus while {b.gate['name']} stops the trace clock: capture with "
                      f"--setup {b.gate['entry']}, which starts it")
    clock_off = b.gate and not b.gate_open()   # the TPIU is out of reach until the setup writes
    if a.setup:
        check_setup(b, a.setup)
    ctrl_now = b.read32(DWT_CTRL)
    caps = {'numcomp': ctrl_now >> 28, 'dwt_ctrl': ctrl_now}
    cap.update(cpuid=f'0x{cpuid:08x}', core=part, architecture=kind, dhcsr_before=f'0x{b.read32(DHCSR):08x}')
    before = read_before(b, caps['numcomp'])
    tcr, ter, ctrl_steps, watches = plan_config(a, caps, before, b.attach_writes is not None)
    if a.observe_itm and b.start_itm_tcr is not None and not same(ITM_TCR, before[ITM_TCR], b.start_itm_tcr):
        raise Refused(f'--observe-itm: starting SWO over a {b.name} sets ITM_TCR to 0x{b.start_itm_tcr:08x}, and the '
                      f'firmware runs 0x{before[ITM_TCR]:08x}: observing would change it; --take-itm reconfigures it '
                      f'for the capture')
    prescaler, actual_swo, notes = check_rates(a, b.probe_rates())
    unit_regs = b.trace_unit_regs()
    if isinstance(b, JLinkBackend) and not clock_off and not is_component(b, TPIU_CIDR):
        raise Refused('no TPIU at 0xE0040000: this J-Link route drives the core TPIU only (swo-dwt.md, board setup)')
    if not unit_regs:
        notes.append('the trace unit is not readable here: its prescaler was not read back')
    elif a.observe_itm and clock_off:
        raise Refused(f"--observe-itm: {b.gate['name']} stops the trace clock, so no SWO runs to observe")
    elif a.observe_itm and unit_read(b, unit_regs[2]) == SPPR_NRZ:
        found = (unit_read(b, unit_regs[1]) & 0xFFFF) + 1
        if found != prescaler:   # someone runs SWO already: observing must not change its rate
            raise Refused(f'--observe-itm: the trace unit already runs NRZ at prescaler {found} '
                          f'({a.traceclk_hz / found:.0f} Hz at this traceclk), not {prescaler}: capture at that rate '
                          f'or --take-itm')
    image = check_image(b, a.elf, a.image_regions) if a.elf else None
    if b.attach_writes:
        notes.append(b.attach_writes)
    cap['clocks'] = {'traceclk_hz': a.traceclk_hz, 'swo_hz': actual_swo, 'requested_swo_hz': a.swo_hz,
                     'prescaler': prescaler, 'source': a.clock_source}
    if tcr & TCR_TSENA:
        cap['timestamps'] = {'clock_hz': a.core_hz, 'clock_source': a.clock_source,
                             'prescale': (1, 4, 16, 64)[tcr >> 8 & 3]}
    cap.update(notes=notes, watches=watches, setup=a.setup and a.setup['key'],
               features={'exc': a.exc, 'pc_sample': a.pc_sample, 'cyc_event': a.cyc_event, 'profile': a.profile,
                         'global_ts': a.global_ts, 'cycmatch': a.cycmatch,
                         'itm_ports': f'0x{ter:08x}', 'observe_itm': a.observe_itm, 'take_itm': a.take_itm})
    sources = ['stim'] if ter else []
    if a.exc:
        sources.append('exc')
    if a.pc_sample:
        sources.append('pc')
    if a.profile or a.cyc_event:
        sources.append('evcnt')
    if watches:
        sources += ['dpc', 'daddr', 'dval']
    cap['sources'] = None if a.observe_itm else sources   # what the firmware enables is not ours to list
    cap['before'] = {reg_name(r): f'0x{v:08x}' for r, v in before.items()}
    writes = [w for w in a.setup['writes'] if w.get('for') in (None, *features_on(a))] if a.setup else []
    if a.global_ts and not any(w.get('for') == 'global-ts' for w in writes):
        notes.append('no --setup write starts a global timestamp generator: global timestamps appear only if '
                     'something else runs one')
    setup_writes = [setup_reg(b, w) for w in writes]
    setup_watch = [int(w['addr'], 0) for w in a.setup['watch']] if a.setup else []
    tracked = list(dict.fromkeys(unit_regs + setup_writes + [ITM_TCR, ITM_TER, ITM_TPR, DWT_CTRL] +
                                 START_SIDE_EFFECTS.get(b.family, [])))
    cap['trace_unit_before'] = snapshot(b, ([] if clock_off else unit_regs) + setup_writes + setup_watch)

    journal = Journal(os.path.join(out_dir, 'journal.jsonl'),
                      {'identity': b.identity(), 'host': socket.gethostname(), 'cpuid': cpuid, 'image': image,
                       'started': time.time()})
    s = Session(b, journal)
    stream = None
    failures = []
    try:
        for w, reg in zip(writes, setup_writes):
            s.write(reg, unit_read(b, reg) & ~int(w['mask'], 0) | int(w['value'], 0),
                    requires=requirement(b, w, writes))
        with s.probe_op(tracked, 'start_swo'):
            stream_sock = b.start_swo(a.traceclk_hz, a.swo_hz, ter)
        stream = Stream(stream_sock, os.path.join(out_dir, 'swo.bin'))
        # synced: no ITM source could emit before the stream was read, so its byte 0 is a packet boundary
        itm_on = b.read32(ITM_TCR) & TCR_ITMENA and b.read32(ITM_TER)
        cap['stream_start'] = 'unsynced' if (before[ITM_TCR] & TCR_ITMENA and before[ITM_TER]) or itm_on else 'synced'
        if unit_regs:
            acpr = unit_read(b, unit_regs[1]) & 0xFFFF
            cap['clocks']['acpr_readback'] = acpr
            if acpr + 1 != prescaler:
                raise ToolError(f'the probe tool set the prescaler to {acpr + 1} (SWO {a.traceclk_hz / (acpr + 1):.0f} '
                                f'Hz), not {prescaler}')
        if a.observe_itm:
            for r in (ITM_TCR, ITM_TER):
                if not same(r, b.read32(r), before[r]):
                    raise ToolError(f'starting SWO changed the observed {reg_name(r)} from 0x{before[r]:08x} to '
                                    f'0x{b.read32(r):08x}')
        else:
            s.write(ITM_TPR, 0)
            s.write(ITM_TER, ter)
            s.write(ITM_TCR, tcr)
        for w in watches:   # disarmed while its address and mask change
            base = DWT_COMP + 16 * w['comparator']
            s.baseline([base, base + 4, base + 8])
            s.write(base + 8, 0)
            s.write(base, w['addr'])
            s.write(base + 4, w['mask'])
            s.write(base + 8, w['function'])
        for value in ctrl_steps:
            s.write(DWT_CTRL, value)
        cap['configured'] = time.time()
        deadline = time.monotonic() + a.duration_ms / 1000
        while time.monotonic() < deadline and not stream.error:
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    except (ToolError, OSError) as e:
        failures.append(f'capture: {e}')
    except KeyboardInterrupt:
        failures.append('capture: interrupted')
    finally:
        # every source off and drained while the probe still listens, then every register back; each stage runs
        # whatever the one before raised, and a signal now only marks the run interrupted (main)
        CLEANING.set()
        if stream:
            cap['teardown_byte'] = stream.bytes   # bytes in flight arrive after: a lower bound
        failures += quiesce(s, a, watches)
        if stream:
            time.sleep(DRAIN_S)
            stream.ending.set()
            try:
                with s.probe_op(tracked, 'stop_swo'):
                    b.stop_swo(ter)
            except (ToolError, OSError) as e:
                failures.append(f'stop: {e}')
            try:
                stream.close()
            except OSError as e:
                failures.append(f'stream: {e}')
            if stream.error:
                failures.append(f'capture: {stream.error}')
            cap.update(bytes=stream.bytes, stream_first=stream.first, stream_last=stream.last)
        restore_failures = s.restore()
        with contextlib.suppress(ToolError, OSError):
            cap['dhcsr_after'] = f'0x{b.read32(DHCSR):08x}'
        cap['after'] = snapshot(b, list(before))
        try:
            reachable = not b.gate or b.gate_open()   # the restore stops a trace clock the setup started
        except ToolError as e:
            reachable = False
            failures.append(f"after: {b.gate['name']}: {e}")
        cap['trace_unit_after'] = snapshot(b, (unit_regs if reachable else []) + setup_writes + setup_watch)
    cap['errors'] = failures
    cap['restore'] = restore_failures or 'complete'
    return cap, failures + restore_failures


def quiesce(s, a, watches):
    """Stop what the capture made emit, first in one write the ITM's ITMENA and TXENA (stimulus and the DWT's
    packets, ARMv7-M C1-854) when the ITM is the capture's, else DWT_CTRL as found; then disarm its comparators."""
    failures = []
    if not a.observe_itm and s.journalled(ITM_TCR):
        try_write(s, ITM_TCR, lambda: s.b.read32(ITM_TCR) & ~(TCR_ITMENA | TCR_TXENA), failures)
    elif s.journalled(DWT_CTRL):
        try_write(s, DWT_CTRL, lambda: s.original(DWT_CTRL), failures)
    for w in watches:
        r = DWT_COMP + 16 * w['comparator'] + 8
        if s.journalled(r):
            try_write(s, r, lambda: 0, failures)
    return failures


def try_write(s, reg, value, failures):
    try:
        s.write(reg, value())
    except (ToolError, OSError) as e:
        failures.append(f'stop: {e}')


# ---------------------------------------------------------------------------------------------------
# restore from a journal
# ---------------------------------------------------------------------------------------------------

def restore_from(b, out_dir):
    """(report, problems): each journalled register back to its original where its value now is one the journal
    explains: written by the capture, or read back after a probe tool operation, since the last restore of it."""
    path = os.path.join(out_dir, 'journal.jsonl')
    header, entries, settled = load_journal(path)
    if header['identity'] != b.identity():
        raise Refused(f"journal was written over {header['identity']}, this connection is {b.identity()}")
    if header.get('host') != socket.gethostname():
        raise Refused(f"journal was written on host {header.get('host')}, this is {socket.gethostname()}")
    cpuid = b.read32(CPUID)
    if cpuid != header['cpuid']:
        raise Refused(f"journal CPUID 0x{header['cpuid']:08x}, target 0x{cpuid:08x}")
    image = header.get('image')
    if not image:
        raise Refused('the capture recorded no firmware image (it ran without --elf), so the target cannot be shown '
                      'to run the same firmware: restore by hand from the original values in journal.jsonl')
    if read_image(b, image['regions']) != image['sha256']:
        raise Refused('the target no longer runs the firmware the capture journalled: restore by hand from the '
                      'original values in journal.jsonl')
    first = {}
    for e in entries:
        first.setdefault((e['reg'], e['target']), e)
    journal = Journal(path)   # what this restore verifies is settled for the next one

    def explained(key):
        owed = [e for e in entries if (e['reg'], e['target']) == key][settled.get(key, 0):]
        return owed, [e['intended'] for e in owed if e['intended'] is not None] + \
            [e['readback'] for e in owed if 'readback' in e]

    report, problems = [], []
    for n, originals in comparators(first):
        done, lines, trouble = restore_comparator(b, n, originals, {r: explained((r, None))[1] for r in originals})
        for addr, value in done.items():
            journal.restored(first[(addr, None)], value)
        report += lines
        problems += trouble
    for key in reversed(list(first)):
        if key[1] is None and comparator_of(key[0]) is not None:
            continue
        reg, name, original = key if key[1] else key[0], first[key]['name'], first[key]['original']
        owed, values = explained(key)
        if b.gate and in_tpiu(key[0]) and not b.gate_open():   # never touch a TPIU whose clock is stopped
            if owed:
                problems.append(f'{name}: not read, {b.gate["name"]} stops the trace clock; left alone')
            else:
                report.append(f'{name}: restored earlier; not read, the trace clock is stopped')
            continue
        try:
            now = unit_read(b, reg)
        except ToolError as err:
            problems.append(f'{name}: {err}; left alone')
            continue
        if same(key[0], now, original):
            report.append(f'{name}: already 0x{now:08x}')
            journal.restored(first[key], now)
        elif any(same(key[0], now, v) for v in values):
            conflict = meet_requirement(b, first[key], journal, lambda k: [first[k]['original']] + explained(k)[1],
                                        entries)
            if conflict:
                problems.append(conflict)
                continue
            unit_write(b, reg, original)
            rb = unit_read(b, reg)
            if same(key[0], rb, original):
                report.append(f'{name}: 0x{now:08x} -> 0x{rb:08x}')
                journal.restored(first[key], rb)
            else:
                problems.append(f'{name}: 0x{now:08x} -> 0x{rb:08x}, not the original 0x{original:08x}')
        else:
            in_flight = [e['kind'] for e in owed if 'readback' not in e]
            why = f' (interrupted during {in_flight[-1]})' if in_flight else ''
            problems.append(f'{name}: 0x{now:08x} is neither the original 0x{original:08x} nor a value this capture '
                            f'left{why}; left alone')
    return report, problems


# ---------------------------------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------------------------------

def load_setup(key, p):
    entries = load_entries()
    if key not in entries:
        p.error(f'--setup {key}: swo_boards.json has {", ".join(sorted(entries))}')
    setup = dict(entries[key], key=key)
    for w in setup['writes'] + setup['watch']:
        REG_NAMES[int(w['addr'], 0)] = w['name']
        size_reg(w)
    return setup


def make_backend(a, p):
    pc = sibling('pc_sample')
    if bool(a.hil_config) != bool(a.board):
        p.error('--hil-config and --board go together')
    route = None
    if a.hil_config:
        try:
            flasher = pc.hil_flasher(a.hil_config, a.board)
            route = pc.HIL_PROBE_ROUTES[flasher['name']]
            if route == 'jlink':
                if a.cfg or a.vid_pid:
                    raise ValueError('--cfg and --vid-pid are OpenOCD flags; this board is a J-Link one')
                pc.hil_merge(a, a.board, probe=flasher.get('uid'), device=pc.hil_jlink_device(a.board, flasher))
            else:
                if a.device:
                    raise ValueError('--device is a J-Link flag; this board is an OpenOCD one')
                pc.hil_merge(a, a.board, probe=flasher.get('uid'), cfg=flasher.get('args'),
                             vid_pid=flasher.get('vid_pid'))
        except ValueError as e:
            p.error(str(e))
        if not a.probe:
            p.error(f'--board {a.board}: no flasher.uid (probe serial) in {a.hil_config}: give --probe')
        if route == 'jlink' and not a.device:
            p.error(f"--board {a.board}: a jlink entry needs flasher.args '-device NAME', or give --device")
        if route == 'openocd' and not a.cfg:
            p.error(f"--board {a.board} is flashed over {flasher['name']}, whose entry has no OpenOCD configuration: "
                    f"give --cfg")
    else:
        if not a.probe:
            p.error('name the probe: --hil-config with --board, or --probe with --device (J-Link) or --cfg (OpenOCD)')
        if bool(a.device) == bool(a.cfg):
            p.error('give --device (J-Link) or --cfg (OpenOCD), not both')
        route = 'jlink' if a.device else 'openocd'
    if a.setup and route in a.setup:
        if isinstance(a.setup[route], str):
            p.error(f"--setup {a.setup['key']}: {route} {a.setup[route]}")
        a.swo_object = a.swo_object or a.setup[route].get('swo_object')
    if route == 'jlink':
        if not (a.interface and a.speed):
            p.error('a J-Link needs --interface and --speed')
        if not pc.SPEED_RE.fullmatch(a.speed):
            p.error(f'--speed {a.speed!r}: kHz, auto or adaptive')
        if a.swo_object or a.vid_pid:
            p.error('--swo-object and --vid-pid are OpenOCD flags')
        return JLinkBackend(a.probe, a.device, a.interface, a.speed)
    if a.interface or a.speed:
        p.error('--interface and --speed are J-Link flags; OpenOCD takes them from --cfg')
    if a.vid_pid and not re.fullmatch(r'0x[0-9a-fA-F]{1,4} 0x[0-9a-fA-F]{1,4}( 0x[0-9a-fA-F]{1,4} 0x[0-9a-fA-F]{1,4})*',
                                      a.vid_pid.strip()):
        p.error(f'--vid-pid {a.vid_pid!r}: complete 0xVVVV 0xPPPP pairs')
    if a.restore and not a.swo_object:
        try:
            a.swo_object = load_journal(os.path.join(a.restore, 'journal.jsonl'))[0]['identity'].get('swo_object')
        except (OSError, ValueError) as e:
            p.error(f'--restore {a.restore}: {e}')
    return OpenOCDBackend(a.probe, a.cfg, a.vid_pid, a.swo_object)


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group('connection')
    g.add_argument('--hil-config', metavar='FILE')
    g.add_argument('--board', metavar='NAME')
    g.add_argument('--probe', help='probe serial')
    g.add_argument('--device', help='J-Link device name')
    g.add_argument('--interface', choices=['swd'], help='J-Link: swd (SWO shares the JTAG TDO pin)')
    g.add_argument('--speed', help='J-Link: interface speed in kHz')
    g.add_argument('--cfg', help='OpenOCD: its -f/-c arguments, without adapter serial')
    g.add_argument('--vid-pid', help='OpenOCD: "0xVVVV 0xPPPP"')
    g.add_argument('--swo-object', help='OpenOCD: the trace unit object, when its configuration creates several')
    g.add_argument('--accept-attach-writes', action='store_true',
                   help="OpenOCD: connect although its examine rewrites trace and comparator state first")
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument('--caps', action='store_true', help='report what the core and trace unit support, and stop')
    m.add_argument('--out', help='new capture dir')
    m.add_argument('--restore', metavar='DIR', help='restore what an interrupted capture in DIR left')
    c = p.add_argument_group('capture')
    c.add_argument('--traceclk-hz', type=int, help="the trace unit's clock (TRACECLKIN), no default")
    c.add_argument('--swo-hz', type=int, help='SWO pin rate, no default')
    c.add_argument('--clock-source', help='where the clocks come from (recorded), e.g. "RCC readback: HCLK 168 MHz"')
    c.add_argument('--protocol', choices=['uart'], help='pin protocol: uart (NRZ); Manchester is not supported yet')
    c.add_argument('--allow-approx-swo', action='store_true', help='take the nearest divisor and record the rate')
    c.add_argument('--duration-ms', type=int)
    c.add_argument('--exc', action='store_true', help='exception trace (DWT_CTRL.EXCTRCENA)')
    c.add_argument('--pc-sample', metavar='CYCTAP:POSTPRESET',
                   help='periodic PC samples every (POSTPRESET+1) x 64 (CYCTAP 0) or x 1024 (CYCTAP 1) cycles')
    c.add_argument('--cyc-event', metavar='CYCTAP:POSTPRESET',
                   help='a Cyc event packet every (POSTPRESET+1) x 64 or x 1024 cycles; not with --pc-sample')
    c.add_argument('--profile', default='', help='profiling counters whose 8-bit wraps emit event packets: any of '
                   'cpi,exc,sleep,lsu,fold')
    c.add_argument('--itm-ports', help='ITM stimulus ports to enable, e.g. 0,1 or 0-3')
    c.add_argument('--timestamps', action='store_true', help='local timestamps (ITM_TCR.TSENA)')
    c.add_argument('--core-hz', type=int, help='with --timestamps: the processor clock, which counts them')
    c.add_argument('--ts-prescale', type=int, default=1, choices=(1, 4, 16, 64))
    c.add_argument('--global-ts', choices=list(GTSFREQ),
                   help='global timestamps (ITM_TCR.GTSFREQ) from the part\'s timestamp generator, which its --setup '
                        'entry enables')
    c.add_argument('--watch', action='append', default=[], help='addr=ADDR|SYMBOL,access=r|w|rw,'
                   'emit=val|pc|pcval|offset|offval[,mask=BITS] (ARMv7-M, comparators 0-3)')
    c.add_argument('--cycmatch', type=lambda v: int(v, 0), metavar='CYCLES',
                   help='a PC packet each time DWT_CYCCNT equals CYCLES (comparator 0, once per 2^32 cycles)')
    c.add_argument('--elf', help='firmware ELF: symbols for --watch, and its hash in capture.json')
    c.add_argument('--setup', help='board setup entry in swo_boards.json, applied before the capture')
    o = c.add_mutually_exclusive_group()
    o.add_argument('--observe-itm', action='store_true')
    o.add_argument('--take-itm', action='store_true')
    c.add_argument('--take-comparator', type=int)
    a = p.parse_args(argv)
    if a.setup:
        if not a.out:
            p.error('--setup applies to a capture (--out)')
        a.setup = load_setup(a.setup, p)
    backend = make_backend(a, p)
    if backend.attach_writes and not a.accept_attach_writes:
        p.error(f'{backend.attach_writes}: firmware state there is lost. --accept-attach-writes connects anyway')
    if a.accept_attach_writes and not backend.attach_writes:
        p.error(f'--accept-attach-writes: a {backend.name} connect writes nothing of that kind')
    if a.observe_itm and backend.attach_writes:
        p.error(f'--observe-itm: {backend.attach_writes}, so the ITM found would not be the firmware\'s')
    if a.out:
        for flag in ('traceclk_hz', 'swo_hz', 'clock_source', 'protocol', 'duration_ms'):
            if getattr(a, flag) is None:
                p.error(f'a capture needs --{flag.replace("_", "-")} (no default)')
        if min(a.traceclk_hz, a.swo_hz, a.duration_ms) <= 0:
            p.error('--traceclk-hz, --swo-hz and --duration-ms must be positive')
        if a.timestamps != bool(a.core_hz):
            p.error('--timestamps and --core-hz go together')
        if not (dwt_sources(a) or a.itm_ports or a.observe_itm):
            p.error('nothing to capture: give --exc, --pc-sample, --cyc-event, --profile, --watch, --itm-ports or '
                    '--observe-itm')
        if a.pc_sample and a.cyc_event:
            p.error('--pc-sample and --cyc-event share the POSTCNT timer: one at a time (ARMv7-M C1-883)')
        if a.observe_itm and a.itm_ports:
            p.error('--observe-itm keeps the firmware\'s ITM ports; --itm-ports is for a configured capture')
        if os.path.exists(a.out) and set(os.listdir(a.out)) - {'probe.log'}:   # a refused run leaves only its log
            p.error(f'--out {a.out} exists and holds more than a refused run\'s probe.log')
        try:
            if a.cycmatch is not None and not 0 <= a.cycmatch <= 0xFFFFFFFF:
                raise ValueError(f'--cycmatch {a.cycmatch}: DWT_CYCCNT is 32 bits')
            a.pc_sample = parse_period('--pc-sample', a.pc_sample)
            a.cyc_event = parse_period('--cyc-event', a.cyc_event)
            a.profile = [n for n in a.profile.split(',') if n]
            unknown = sorted(set(a.profile) - set(PROFILE_BITS))
            if unknown:
                raise ValueError(f'--profile {",".join(unknown)}: counters are {",".join(PROFILE_BITS)}')
            if a.itm_ports:
                parse_ports(a.itm_ports)
            symbols = sibling('probe_state').elf_symbols(a.elf)[0] if a.elf else None
            a.watch = [parse_watch(w, symbols) for w in a.watch]
            a.image_regions = image_regions(a.elf) if a.elf else None
        except ValueError as e:
            p.error(str(e))
        a.elf_sha256 = None
        if a.elf:
            with open(a.elf, 'rb') as f:
                a.elf_sha256 = hashlib.sha256(f.read()).hexdigest()
    elif any(getattr(a, f) for f in ('traceclk_hz', 'swo_hz', 'exc', 'pc_sample', 'cyc_event', 'profile', 'watch',
                                     'itm_ports', 'timestamps')) or a.cycmatch is not None:
        p.error('capture flags need --out')
    return a, backend


def main(argv=None):
    a, b = parse_args(argv)
    log_dir = a.out or a.restore
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    log = open(os.path.join(log_dir, 'probe.log'), 'a') if log_dir else subprocess.DEVNULL
    stopping = []

    def on_signal(signum, frame):
        stopping.append(signum)
        if len(stopping) == 1 and not CLEANING.is_set():   # nothing may cut the restore short
            raise KeyboardInterrupt
    old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        b.open(log)
        if a.caps:
            print(json.dumps(caps_report(b), indent=1))
            return 0
        if a.restore:
            report, problems = restore_from(b, a.restore)
            print(json.dumps({'restored': report, 'problems': problems}, indent=1))
            return 1 if problems else 0
        cap, failures = capture(a, b, a.out)
        if a.elf:
            cap['elf'], cap['elf_sha256'] = os.path.abspath(a.elf), a.elf_sha256
        if stopping:
            cap['interrupted'] = f'signal {stopping[0]}'
        with open(os.path.join(a.out, 'capture.json'), 'w') as f:
            json.dump(cap, f, indent=1)
        print(json.dumps({k: cap.get(k) for k in ('bytes', 'clocks', 'timestamps', 'errors', 'restore', 'notes')},
                         indent=1))
        return 1 if failures or stopping else 0
    except Refused as e:
        print(f'swo_capture.py: refused: {e}', file=sys.stderr)
        return 2
    except (ToolError, OSError, ValueError) as e:
        print(f'swo_capture.py: {e}', file=sys.stderr)
        if a.out and os.path.exists(os.path.join(a.out, 'journal.jsonl')):
            print(f'swo_capture.py: the journal in {a.out} may hold writes to undo: --restore {a.out}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('swo_capture.py: interrupted before the capture started', file=sys.stderr)
        return 1
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        b.close()


if __name__ == '__main__':
    sys.exit(main())
