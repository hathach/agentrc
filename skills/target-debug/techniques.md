# target-debug techniques — recipes

Part of the `target-debug` skill: the recipes its technique ladder points to.
Read a section when the ladder sends you to that technique; SKILL.md's
Delegated sessions, Rig discipline and Warnings bind here unchanged.

## PC-sampling — where the core spins, without halting

`DWT_PCSR` (0xE000101C) returns the current PC on every read, target running
(Cortex-M3+; optional on M0+, reads 0 if absent; 0xFFFFFFFF = core halted or
WFI-asleep — `mem32 E000EDF0, 1`, DHCSR bit 17 S_HALT, tells which):

```bash
python3 <skill dir>/scripts/pc_sample.py --probe <uid> --device <JLINK_DEVICE> --interface swd --speed 4000 --elf <flashed.elf>
#   or: --hil-config <file> --board <name> --interface swd --speed 4000 --elf <flashed.elf>
#   --samples N (300), --interval-ms M, --raw FILE; DHCSR is read before and after,
#   sentinel and no-PCSR samples are counted apart; exit 1 unless every sample came back
```

The histogram's top entries are the spin site; a flat histogram = core is
servicing normally — and an idle loop has a hot spot of its own: learn it from a
healthy run before reading a wedged one. A core halted from a JLinkExe session is
running again once that session exits (Commander restores the run state it found;
seen 2026-09-18 on RP2350), so the sentinel means the target halted or slept by
itself. Native probes (ST-Link/CMSIS-DAP) are
not covered by the script yet: repeat `mdw 0xE000101C` over OpenOCD's telnet
:4444 by hand.

## RAM ring-buffer trace

The zero-print instrument: a small event ring in the
driver under suspicion, dumped over GDB after the failure. Single-writer (ISR) — no locking:

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

After building, `nm` the ELF for `dbg_ring`/`dbg_wr` — if they're missing the
compiler deleted your instrument and the run will "reproduce" with an empty ring.

Order is the index; if durations matter add a `uint32_t t = DWT->CYCCNT` field
(enable once: `CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk; DWT->CTRL |= 1;`
RISC-V: read `mcycle`). Let the failure happen, halt, then:

```gdb
p dbg_wr                          # total events; oldest slot = dbg_wr & (DBG_N-1) once wrapped
p dbg_ring
dump binary memory /tmp/ring.bin &dbg_ring[0] &dbg_ring[512]
```

## Log capture

Build with the stack's moderate log level (the verbose one adds per-transfer
noise and much more timing skew) and route it over RTT when the probe allows
— no UART wiring (the flags are the project's build contract). Stand the channel up
per the **rtt** skill (servers per probe, transport matrix, control-block
gotchas live there):

```bash
# RTT (J-Link probe; flash + reset first — the console owns the probe):
python3 <rtt skill dir>/scripts/rtt.py --backend jlink --probe <sn> --device <JLINK_DEVICE> --interface swd --speed auto --seconds 20 > /tmp/rtt.log
# UART (board's debug serial, if wired):
stty -F /dev/ttyACM<N> 115200 raw && timeout 20s cat /dev/ttyACM<N> | tee /tmp/uart.log
```

OpenOCD RTT (native probes: ST-Link/CMSIS-DAP): rtt skill §OpenOCD — exact
CB address from `nm`, attach-only. OpenOCD polls — bursty logs can drop
lines; prefer J-Link where both exist. The drain-model warning below
applies unchanged.

A wedged RTT build holds a log tail in RAM only if a live drain was running
(the default mode drops writes once the ring fills): the drain model, the
headless-proven server and the manual ring read are the **rtt** skill's
post-mortem section. Otherwise instrument with the RAM ring above. The
exemption is a SystemView post-mortem build (the project's `sysview` skill says
how): its channel is an overwrite ring that holds the most recent events with
no drain, which answers "what ran right before this hang".

## USB: dual-side capture — the default for enumeration/transfer bugs

Capture both ends at once: usbmon plus a target channel when a Linux PC is the
host; an MCU host has no usbmon on either end, so target channel plus the wire
(`usb-sniffer`), plus `usb-kernel-debug` on a Linux gadget peer. Start both
channels, then trigger the failing test (Linux-PC-host shown;
MCU host: swap usbmon for `usb-sniffer`, + `usb-kernel-debug` on a Linux
gadget peer):

```bash
<usb-kernel-debug skill dir>/scripts/usbcap.py <bus> 30 /tmp/host.pcapng & cap=$!   # host URBs; the board's bus — a VID: selector spanning buses is refused
python3 <rtt skill dir>/scripts/rtt.py --backend jlink --probe <sn> --device <dev> --interface swd --speed auto --seconds 30 > /tmp/target.rtt & rtt=$!  # target (rtt skill; or ring dump after)
wait $cap; rc_cap=$?; wait $rtt; rc_rtt=$?   # `wait $cap && wait $rtt` would skip the rtt wait when cap failed
[ $rc_cap -eq 0 ] && [ $rc_rtt -eq 0 ]       # a bare `wait` returns 0 even when one side failed
```

RTT lines and ring events carry no wall-clock: correlate on unambiguous
anchors — bus reset, SET_ADDRESS, the first transfer on the failing EP — then
lay device events between anchors in host-URB order. Logging the SOF/frame
number on the target gives a shared clock when you need finer alignment.
When host and target evidence disagree, or the host sees nothing at all, add
the wire itself: `usb-sniffer` skill (hardware tap, PID-level).

## Manuals

- J-Link (UM08001): <https://kb.segger.com/UM08001_J-Link_/_J-Trace_User_Guide> — flash breakpoints, RTT, SWO, monitor mode, Commander.
- OpenOCD: <https://openocd.org/doc/html/index.html> — `rtt`, `bp`/`wp`, `cortex_m vector_catch`/`maskisr`, `itm`/`tpiu`.
- "Debugging with GDB" (§5.1 = break/watch/dprintf): Tenth Edition (GDB 18)
  via the `read-doc` skill, or
  `curl -sL -o /tmp/gdb.pdf https://sourceware.org/gdb/current/onlinedocs/gdb.pdf`
  (the HTML mirror blocks fetchers). Installed `arm-none-eabi-gdb`
  `help <cmd>` is authoritative here.
