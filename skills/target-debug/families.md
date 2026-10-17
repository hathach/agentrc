# Chip families — constraints and scoped observations

Part of the `target-debug` skill: what a chip or family does to a debug session whatever firmware runs on it. A
project's symbols and symptoms stay in `projects/`. Entries carry their evidence the way `projects/` entries do
(SKILL.md, "Project notes"), and a measurement holds for the board, firmware and tools it names, not for the whole
family.

## RP2040 flash verification

- Before any RP2040 flash read or `verify_image`, stop at a hardware breakpoint in flash-resident code of the matching
  ELF and confirm the PC is there; a stop in RAM-resident code running with flash access off does not qualify. There
  every flash read returns `0x00`; `verify_image` reports false mismatches at both `0x10000000` and `0x13000000`.
  `scripts/rp2040_verify.py` (beside SKILL.md) does it on OpenOCD: breakpoint, PC check, verification through the
  uncached XIP alias, then it removes its breakpoint, resumes and reads DHCSR; `--help` gives its outcomes and exit
  codes:

  ```bash
  python3 <skill dir>/scripts/rp2040_verify.py --probe <serial> --interface-cfg interface/cmsis-dap.cfg \
    --speed 5000 --elf <flashed.elf> --symbol <flash-resident function the firmware keeps reaching>
  ```

  verified 2026-09-18 81a25eb5d raspberry_pi_pico (htpc, CMSIS-DAP, tinyusb `cdc_msc`, idle in RAM-resident
  `get_bootsel_button`): `--symbol tud_task_ext` verified 31,136 bytes of the matching ELF, exit 0;
  `--symbol board_init` (never reached again) gave `not-at-breakpoint pc=0x20000230`, exit 1; the pre-fix ELF gave
  `mismatch` with diffs from `0x13004112`, exit 1. DHCSR read `0x01000001` each time, the CDC echo still worked and
  dmesg logged no new enumeration. An earlier run of the script's first version did coincide with one disconnect and
  re-enumeration (14:46:42), and the next session logged "external reset detected"; cause not established, so the script
  now fails on a detected reset. OpenOCD's default SMP pair failed `resume` with "core1 not halted" and left core 0
  halted; the script sets `USE_CORE 0`.
- Observed 2026-09-18 bdb6b90fc raspberry_pi_pico (htpc, CMSIS-DAP): a bare `halt` that stopped in `get_bootsel_button`,
  then `verify_image` (all zeroes), then `reset` left the board failing enumeration ("device not accepting address")
  until a `program` reflash recovered it. Mechanism not established.

## RP2350

- Code running from flash (XIP) paid roughly 330 core cycles per taken branch in a tight loop: a 1000-iteration empty
  `volatile` loop took 2.2 ms at 150 MHz. Size calibration loops by measurement. verified 2026-09-18 fad6bd546
  pico2_etm_trace: DWT CYCCNT 329,423 cycles, `time_us_32` 2198 µs.

## i.MX RT1060

- Silicon: DIGPROG (0x400D8260) reads 0x006C0000 on RT1060_A, which the reference manual calls "silicon revision 1.0";
  registers do not tell A from B, the marking does (6A on the measured board). verified 2026-10-12 mimxrt1060_evk
  (MIMXRT1060-EVK, not -EVKB; schematic SCH-31357 rev A2 from its label, the library has rev A3, book 1673), RM rev 4
  p2551.
- Every J-Link reset while the app vector is invalid prints "Could not find valid reset vector." then "Target halted on
  flash header read."; the reset worked. verified 2026-10-12 mimxrt1060_evk (TinyUF2 0.35.0), in two J-Link installs.
- After an image `loadbin` at 0x60000000, treat the rest of its 64 KiB block as erased; a sentinel at 0x60010000, in the
  next block, survived. What triggers the erase is not established: 16-byte sentinel `loadbin`s in that block kept their
  sampled neighbours. verified 2026-10-12 mimxrt1060_evk at sampled points only: TinyUF2 0.35.0 by hand (TinyUF2's HIL
  contract Limits give the points), and TinyUF2 dcc241e's install image (ending at 0x6000C200) left 0x6000C200 and
  0x6000FF00 reading 0xFF in the same session, before any reset, and 0x60010000 unchanged.
- With no boot image (a zeroed page at 0x60000000), new J-Link sessions misread flash: zeros, and at 0x60010000 the
  bytes written at 0x60000200, so a plain `verifybin` fails. A `loadbin` first, which prints "Skipped. Contents already
  match" when nothing changed, makes later reads and `verifybin` correct. The cause is not established. verified
  2026-10-12 mimxrt1060_evk (TinyUF2 dcc241e), repeatable.
- `SNVS->LPGPR` sits in the low-power domain, reset only by LP POR (RM rev 4 §20.6.1.17 p1258, p1235). A value written
  with the core halted survived SYSRESETREQ: bench evidence, the RM does not name that reset. verified 2026-10-12
  mimxrt1060_evk (TinyUF2 9045fc0, `LPGPR[3]`).
- DTCM at 0x20000000 was enabled while TinyUF2 ran; 0x20000FFC took a write and read it back under a halted core.
  verified 2026-10-12 mimxrt1060_evk (TinyUF2 0.35.0 in DFU). The split is the fuse default unless IOMUXC_GPR16 selects
  GPR17 (RM rev 4 p362-363, p1308).

## LPC55

- **Debug over OpenOCD, flash over pyOCD.** Upstream OpenOCD has neither an LPC55 target config nor an LPC55 flash
  driver (master source of 2026-10-16; the farm's 0.12.0+dev-02620-g0ce743125 binary lists the lpc2000, lpc288x and
  lpcspifi drivers only), so OpenOCD gets the target inline and cannot `program` or `load`: flash with pyOCD
  (`-t lpc55s28`). The inline single-core target on an LPC55S28, after `-f interface/cmsis-dap.cfg`:

  ```
  -c "transport select swd" -c "adapter speed 1000"
  -c "swd newdap lpc55s28 cpu -expected-id 0x6ba02477" -c "dap create lpc55s28.dap -chain-position lpc55s28.cpu"
  -c "target create lpc55s28.cpu cortex_m -dap lpc55s28.dap -ap-num 0"
  ```

  verified 2026-10-17 lpcxpresso55s28 (LPC-Link2, CMSIS-DAP FW 1.10): CPUID 0x410fd213, PCSR samples, 8 breakpoints
  and 4 watchpoint comparators, image read-back, halt and resume, RTT, gdb attach.
- The DPIDR read 0x6ba02477, an Arm (designer 0x23B) DPv2 debug port (ADIv5 IHI0031 B2.2.5); UM11126 rev 2.1 and the
  LPC55S2x datasheet give no value. The debug subsystem is an SWJ-DP with a CPU0 AP, a CPU1 AP (LPC55S6x only) and the
  DM-AP of the debug mailbox (UM11126 rev 2.1 §51.5.1, PDF p1120); CPU0 answered at AP 0. verified 2026-10-17
  lpcxpresso55s28. A dual-core LPC55S69 needs its own target for CPU1.
- Rev 0A silicon read `SYSCON->DIEID` 0x000426B0 (REV_ID 0) and ROM 2.2. verified 2026-10-12 lpcxpresso55s69.
- The RTC that holds GPREG is not usable right after a reset-halt: its register clock (AHBCLKCTRL0 bit 23) resets off,
  and after power-up CTRL.SWRESET holds every RTC register at reset (UM11126 rev 2.1 p54, p570). There a J-Link `mem32`
  of GPREG returned "Could not read memory", and a write followed by `r` left the reset failing. verified 2026-10-12
  lpcxpresso55s69 (TinyUF2 0.35.0); which prerequisite the bench hit is not established.

## Espressif (ESP32-S2, S3, P4, S31)

- `espressif.md` beside SKILL.md: tools, the USB ports per chip, attach rules, resets, console, and the per-chip
  measurements.

## DWC2 (Synopsys USB 2.0 OTG core)

- A read of `GRXSTSP` (offset 0x20) pops the RxFIFO status entry; `GRXSTSR` (0x1C) reads the same entry without popping
  it. Debugger memory dumps over the register block must skip 0x20 or the device loses a packet status. HS OTG Databook
  4.20a (book 2045) §7.1.9 p387, §7.1.8 p382.
- From core version 2.94a, `DCTL.SftDiscon` resets to 1 (disconnect) and a core soft reset (`GRSTCTL.CSftRst`) keeps it;
  before 2.94a it reset to 0. On such a core a hard reset of the controller presents a disconnect until firmware clears
  the bit. Whether a probe's reset reaches that hard reset is the chip's wiring. HS OTG Databook 4.20a (book 2045) p19,
  p356, p549-550.

## STM32F4

- `OTG_FS_DCTL` resets to 0 (SDIS clear), but `OTG_FS_GCCFG.PWRDWN` resets to 0, transceiver powered down (RM0090 rev 22
  p1277-1278, p1291), so a system reset drops the device until firmware sets PWRDWN. A J-Link `r; g` gave "USB
  disconnect" and re-enumeration 325 ms later, while a bare halt stayed connected. verified 2026-08-16 stm32f407disco
  (tinyusb's `usb-kernel-recover`; firmware revision not recorded).
