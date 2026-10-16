# target-debug techniques — recipes

Part of the `target-debug` skill: the recipes its technique ladder points to. Read a section when the ladder sends you
to that technique; SKILL.md's Delegated sessions, Rig discipline and Warnings bind here unchanged.

## PC-sampling — where the core spins, without halting (Cortex-M DWT_PCSR)

`DWT_PCSR` (0xE000101C) returns the current PC on every read, target running (Cortex-M3+; implementation defined on
ARMv6-M, reads 0 if absent; 0xFFFFFFFF = core halted or WFI-asleep — `mem32 E000EDF0, 1`, DHCSR bit 17 S_HALT, tells
which):

```bash
python3 <skill dir>/scripts/pc_sample.py --probe <uid> --device <JLINK_DEVICE> --interface swd --speed 4000 --elf <flashed.elf>
#   or: --hil-config <file> --board <name> --interface swd --speed 4000 --elf <flashed.elf>
#   --samples N (300), --interval-ms M, --raw FILE; DHCSR is read before and after,
#   sentinel and no-PCSR samples are counted apart; exit 1 unless every sample came back
#   DEMCR.TRCENA 0 is exit 1, no histogram
```

TRCENA is 0 after a power-on reset, and an ARMv6-M core reset clears it too; `DWT_PCSR` is then UNKNOWN (ARMv6-M/v7-M)
or 0xFFFFFFFF (Armv8-M). The probe tools set it at connect: J-Link Commander V9.78 (verified 2026-10-16 stm32f407disco:
cleared by hand, held clear for 2 s with the core running, set again at the next connect) and OpenOCD, whose cortex_m
examine replaces DEMCR with TRCENA plus its own vector-catch bits (`cortex_m.c` in 0.12.0+dev-g0ce743125). Through them
it reads set; a tool that leaves it clear needs it set from a debugger session (read DEMCR, `w4 E000EDFC` with bit 24
added). The script itself never writes.

The histogram's top entries are the spin site; a flat histogram = core is servicing normally — and an idle loop has a
hot spot of its own: learn it from a healthy run before reading a wedged one. A core halted from a JLinkExe session is
running again once that session exits (Commander restores the run state it found; seen 2026-09-18 on RP2350), so the
sentinel means the target halted or slept by itself. Native probes (ST-Link/CMSIS-DAP) are not covered by the script
yet: `probe_state.py` (next section) samples PCSR over OpenOCD from the board's HIL entry and reports the raw PC
histogram, without symbols; `--samples` sets the count.

## Probe state — DHCSR anchor, PCSR samples, image read-back

One JSON object of what the probe sees on a Cortex-M board, with the board lock already held (the script takes none):
CPUID, DHCSR before and after, DEMCR, N `DWT_PCSR` samples, and on request the breakpoint budget, static variables read
through the ELF, raw regions saved to files, and the image's address ranges read back and compared on the host:

```bash
python3 <skill dir>/scripts/probe_state.py --hil-config <file> --board <name> [--interface swd --speed 4000] \
  [--samples N] [--interval-ms M] [--allow-halt] [--budget] \
  [--verify <flashed.elf> | --verify <fw.bin> --base <flash addr>] \
  [--elf <flashed.elf> --read <symbol> ...] [--save <addr> <len> <new file> ...]
#   exit 0 observed, 1 verify mismatch, 2 usage, 3 probe/tool failed, capture or save incomplete, wrong probe
#   connected, core not M-profile, or a halt read left the core halted
```

- The script issues no configuration write; the connect does (PC-sampling above: the tools set TRCENA, OpenOCD also
  DHCSR.C_DEBUGEN), so `demcr` is the tool's value, not the firmware's. It halts only with `--allow-halt`, when no
  sample was usable from a running core; over OpenOCD it refuses that on a multi-core target, where the SMP halt stops
  every core and the resume fails for a core OpenOCD never polled, leaving it halted (OpenOCD 0.12.0+dev-g0ce743125,
  `cortex_m_restore_smp`; RP2040 core1, verified 2026-10-16), and OpenOCD's resume error is unreliable there in both
  directions, so judge each core by its DHCSR. Reads use J-Link `savebin` and OpenOCD `dump_image`, never
  `verify_image`, whose checksum path writes its CRC loader into the target's work area first
  (`armv7m_checksum_memory`), even on a running core.
- `--budget`: `fpCtrl.numCode` is the hardware breakpoint count (FP_CTRL bits [7:4] and [14:12]; ARMv6-M's BP_CTRL
  reserves the high bits), `dwtCtrl.numComp` the watchpoint comparators, reported only while TRCENA is set since the DWT
  registers read UNKNOWN otherwise (ARMv7-M ARM C1-839, ARMv6-M ARM C1-339).
- `--read <symbol>` with `--elf` (a linked executable): the symbol table places it offline; exactly one definition, a
  sized data object in a section the image loads as data, inside Code, SRAM or RAM (peripheral and system space refused:
  a read there can have side effects), widths 1, 2, 4 and 8 decoded, every read keeps its bytes. `reads.snapshot` is
  `false` on purpose: a running core changes memory between reads, and DHCSR before and after only says whether it ran.
- `--save <addr> <len> <new file>`: a raw region copy with its hash, refusing an existing file; an incomplete copy is
  kept and reported `complete: false`. It proves nothing about what restores the board, and a region read on a running
  core is not coherent. The address rule is architectural: a vendor device mapped inside Code, SRAM or RAM is yours to
  avoid. On RP2040 the whole XIP region (0x10000000-0x1FFFFFFF: flash and its aliases, the XIP controller, SSI; RP2040
  datasheet §2.2.2) is refused for `--read` and `--save`, since the controller registers have read side effects (XIP
  `STREAM_FIFO`); flash verification there is `rp2040_verify.py`'s.
- `connection` is the probe the tool actually connected to (J-Link serial and VTref, CMSIS-DAP serial, ST-Link target
  voltage); a J-Link whose serial is not the requested one ends the run. `otherProbeClients` lists probe tools in the
  process list as a possible conflict, not as proof that the probe is free, and never replaces the lock. OpenOCD boards
  report each core's DHCSR before and after as `cores`, judged from the register since OpenOCD's own state stays
  `unknown` until it polls; reading another core's DHCSR clears its sticky bits.
- `pcsr.status`: `sampled`; `no-address` (only 0xFFFFFFFF or 0: halted, address unavailable, or non-invasive debug not
  permitted); `not-implemented` (all 0: RAZ, a core built without PCSR); `dwt-disabled` (DEMCR bit 24 clear: the values
  are UNKNOWN, Armv8-M returns 0xFFFFFFFF). On ARMv6-M (M0, M0+, M1) PCSR is implementation defined (ARMv6-M ARM book
  1243 §C1.7.2 p344); RP2040's DWT has it.
- Every DHCSR read clears S_RESET_ST, the tool's own connect included: the `dhcsrBefore` bit covers only this session's
  connect, `dhcsrAfter` the sampling window.
- Before calling an RP2040 flash read-back mismatch real: `families.md`, "RP2040 flash verification".
- A single opcode read (`mdh <addr> 1`) at an address where two builds differ supports a narrow identity claim between
  them, not a verified image.

## RAM ring-buffer trace

The zero-print instrument: a small event ring in the driver under suspicion, dumped over GDB after the failure. One
non-reentrant writer on one core (the ISR) — no locking; on a dual-core chip (ESP32-S3, P4, S31) a second core writing
the same ring needs its own ring:

```c
typedef struct { uint16_t ev; uint16_t a; uint32_t b; } dbg_ev_t;
#define DBG_N 512                          // power of two
static volatile dbg_ev_t dbg_ring[DBG_N];  // volatile REQUIRED: -Os dead-store-
static volatile uint32_t dbg_wr;           // eliminates a write-only static array
static inline void DBG_EV(uint16_t ev, uint16_t a, uint32_t b) {
  uint32_t i = dbg_wr++;
  dbg_ring[i & (DBG_N - 1)] = (dbg_ev_t){ ev, a, b };
}
// call sites: DBG_EV(__LINE__, ep_addr, count);  — __LINE__ as event id
```

After building, `nm` the ELF for `dbg_ring`/`dbg_wr` — if they're missing the compiler deleted your instrument and the
run will "reproduce" with an empty ring.

Order is the index; if durations matter add a `uint32_t t` field from the core's cycle counter (Cortex-M: `DWT->CYCCNT`,
enable once with `CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk; DWT->CTRL |= 1;`; RISC-V: `mcycle`; Xtensa: `CCOUNT`).
Let the failure happen, halt, then:

```gdb
p dbg_wr                          # total events; oldest slot = dbg_wr & (DBG_N-1) once wrapped
p dbg_ring
dump binary memory /tmp/ring.bin &dbg_ring[0] &dbg_ring[512]
```

## Log capture

Build with the stack's moderate log level (the verbose one adds per-transfer noise and much more timing skew) and route
it over RTT when the probe allows — no UART wiring (the flags are the project's build contract). Stand the channel up
per the **rtt** skill (servers per probe, transport matrix, control-block gotchas live there):

```bash
# RTT (J-Link probe; flash + reset first — the console owns the probe):
python3 <rtt skill dir>/scripts/rtt.py --backend jlink --probe <sn> --device <JLINK_DEVICE> --interface swd --speed auto --seconds 20 > /tmp/rtt.log
# UART (board's debug serial, if wired; name it by /dev/serial/by-id or by-path; opening a port can reset an
#   Espressif chip through DTR/RTS: espressif.md "Console"):
timeout 20s python3 -c '
import os, sys, termios, tty
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOCTTY)
tty.setraw(fd, termios.TCSANOW)
a = termios.tcgetattr(fd); a[4] = a[5] = termios.B115200
termios.tcsetattr(fd, termios.TCSANOW, a)
while chunk := os.read(fd, 4096):
    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
' /dev/serial/by-id/<port> | tee /tmp/uart.log
```

A separate `stty` can race another client's terminal setup, and the settings are shared: the reader configures its own
fd with `TCSANOW` (the default `TCSAFLUSH` waits for a concurrent writer's output to drain and discards unread input);
coordinate later changes between clients (a capture stopped at 4095 bytes, a reader blocked behind the CDC writer:
2026-10-16, P4 and S31).

A UART write path costs what its driver does: a polled driver blocks only while the TX FIFO is full, an interrupt-driven
ring blocks only when the ring is full, and a console that drops when the host does not drain waits as long as its
driver says before dropping, then loses lines. Espressif consoles (UART0, USB-Serial-JTAG CDC): `espressif.md`
"Console".

OpenOCD RTT (native probes: ST-Link/CMSIS-DAP): rtt skill §OpenOCD — exact CB address from `nm`, attach-only. OpenOCD
polls — bursty logs can drop lines; prefer J-Link where both exist. The drain-model warning below applies unchanged.

A wedged RTT build holds a log tail in RAM only if a live drain was running (the default mode drops writes once the ring
fills): the drain model, the headless-proven server and the manual ring read are the **rtt** skill's post-mortem
section. Otherwise instrument with the RAM ring above. The exemption is a SystemView post-mortem build (the project's
`sysview` skill says how): its channel is an overwrite ring that holds the most recent events with no drain, which
answers "what ran right before this hang".

## USB: dual-side capture — the default for enumeration/transfer bugs

Capture both ends at once: usbmon plus a target channel when a Linux PC is the host; an MCU host has no usbmon on either
end, so target channel plus the wire (`usb-sniffer`), plus `usb-kernel-debug` on a Linux gadget peer. Start both
channels, then trigger the failing test (Linux-PC-host shown; MCU host: swap usbmon for `usb-sniffer`, +
`usb-kernel-debug` on a Linux gadget peer):

```bash
<usb-kernel-debug skill dir>/scripts/usbcap.py <bus> 30 /tmp/host.pcapng & cap=$!   # host URBs; the board's bus — a VID: selector spanning buses is refused
python3 <rtt skill dir>/scripts/rtt.py --backend jlink --probe <sn> --device <dev> --interface swd --speed auto --seconds 30 > /tmp/target.rtt & rtt=$!  # target (rtt skill; or ring dump after)
wait $cap; rc_cap=$?; wait $rtt; rc_rtt=$?   # `wait $cap && wait $rtt` would skip the rtt wait when cap failed
[ $rc_cap -eq 0 ] && [ $rc_rtt -eq 0 ]       # a bare `wait` returns 0 even when one side failed
```

RTT lines and ring events carry no wall-clock: correlate on unambiguous anchors — bus reset, SET_ADDRESS, the first
transfer on the failing EP — then lay device events between anchors in host-URB order. Logging the SOF/frame number on
the target gives a shared clock when you need finer alignment. When host and target evidence disagree, or the host sees
nothing at all, add the wire itself: `usb-sniffer` skill (hardware tap, PID-level).

## Manuals

- J-Link (UM08001): <https://kb.segger.com/UM08001_J-Link_/_J-Trace_User_Guide> — flash breakpoints, RTT, SWO, monitor
  mode, Commander.
- OpenOCD: <https://openocd.org/doc/html/index.html> — `rtt`, `bp`/`wp`, `cortex_m vector_catch`/`maskisr`,
  `itm`/`tpiu`.
- openocd-esp32 (Espressif's fork):
  <https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/jtag-debugging/> and the fork's
  `share/openocd/scripts/target/esp_common.cfg`.
- "Debugging with GDB" (§5.1 = break/watch/dprintf): Tenth Edition (GDB 18) via the `read-doc` skill, or
  `curl -sL -o /tmp/gdb.pdf https://sourceware.org/gdb/current/onlinedocs/gdb.pdf` (the HTML mirror blocks fetchers).
  The matching toolchain's installed gdb (`arm-none-eabi-gdb`, `riscv32-esp-elf-gdb`, `xtensa-esp32s3-elf-gdb`)
  `help <cmd>` is authoritative here.
