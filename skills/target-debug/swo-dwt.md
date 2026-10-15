# SWO and DWT data trace — hardware-timed, zero code change (Cortex-M)

Part of the `target-debug` skill (its technique ladder says when to reach for these).
Cortex-M only: ARMv7-M and later for DWT trace and SWO, with the ARMv6-M
limits as written below; absent on Xtensa and RISC-V (Espressif's analog is
apptrace, `espressif.md`). Needs the SWO pin (TRACESWO) wired to the probe.

## SWO — hardware-timed trace on one pin (J-Link; verified on F407)

If SWO (TRACESWO) is wired, DWT emits packets with ZERO code change:
**exception trace** (DWT_CTRL bit16 — every IRQ enter/exit, timestamped) and
**hardware PC sampling** (bit12), better histograms than DWT_PCSR polling.
SWOViewer tools decode only ITM *stimulus* (firmware without ITM printf emits
none) — capture raw:

```bash
# JLinkExe -CommandFile:
w4 E0001000, 0x00011401      # EXCTRCENA|PCSAMPLENA|SYNCTAP|CYCCNTENA
SWOStart 4000000             # explicit speed — autodetect fails headless
Sleep 3000
SWORead                      # hex: 0x17+4B LE = PC sample, 0x0E+2B = IRQ enter/exit
```

Verified: 680 KB in 3 s (flash-range PC samples + SysTick enter/exit).
SWORead stuck at 0: check wiring (many boards route only SWDIO/SWCLK), the
trace configuration, the clocks and the capture setup before concluding.
Restore DWT_CTRL when done.

## DWT data trace — stream one variable's accesses (value + PC), zero code

The watchpoint comparators' non-halting sibling (ARMv7-M ARM Table C1-21;
absent on ARMv6-M): emit a packet on every access to a watched address
instead of halting. Verified on F407 (J-Link) and H743 (OpenOCD/ST-Link) —
both streamed a tick counter's live value plus the accessor PC (which
variable: the project notes, `projects/`):

```bash
w4 E0001020, <&variable>   # DWT_COMP0 (JLinkExe shown; OpenOCD: same via mww)
w4 E0001024, 0             # DWT_MASK0 = exact address
w4 E0001028, 0x3           # FUNCTION 0b0011: value + accessor-PC packets (0b0010: value only)
# stream: 0x47+4B = accessor PC, 0x87+4B = value, 0x70 = timestamp
```

Caveats: traces reads AND writes (no write-only encoding) — a variable the
main loop polls floods the pipe with read packets and squeezes out value
packets (seen on F407); disarm (`FUNCTION=0`) when done; costs one of the
DWT comparators.

## Enabling SWO — the chain, and the vendor part that bites

DEMCR.TRCENA → ITM (TCR/TER) → SWO/TPIU (protocol + prescaler) → pin mux.
Tools set the first three (`SWOStart` on SEGGER; `swo`/`tpiu` object
`enable` + `itm ports on` on OpenOCD) — pin mux and trace clocks are
per-family:

- STM32F4: debug pins default to trace — nothing to configure.
- STM32H7 (verified, ST-Link): DBGMCU trace clocks + **PB3 muxed to AF0 by
  hand** + native `stlink-dap.cfg` (the hla transport's tpiu path silently
  does nothing) + the cfg-provided `stm32h7x.swo` object (`stm32h7x.tpiu`
  is the parallel port — rejects uart). traceclk = c_ck 400 MHz, not HCLK:
  too-slow guesses give ratio-garbled bytes, too-fast gives silence.

```bash
openocd -f interface/stlink-dap.cfg -c 'adapter serial <uid>' -f target/stm32h7x.cfg -c init \
 -c "mww 0x5C001004 0x00700000" \
 -c 'set m [read_memory 0x58020400 32 1]; mww 0x58020400 [expr {([lindex $m 0] & ~0xC0) | 0x80}]' \
 -c 'set a [read_memory 0x58020420 32 1]; mww 0x58020420 [expr {[lindex $a 0] & ~0xF000}]' \
 -c "stm32h7x.swo configure -protocol uart -traceclk 400000000 -pin-freq 2000000 -output /tmp/swo.bin" \
 -c "stm32h7x.swo enable" -c "itm ports on" \
 -c "sleep 3000" -c "stm32h7x.swo disable" -c shutdown   # then decode /tmp/swo.bin
```
