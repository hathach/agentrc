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
  (`usb-kernel-recover`, rung 1; firmware revision not recorded).

## MAX3266x

- The Cortex-M4's DWT has no cycle counter: `DWT_CTRL` read `0x4F000000`, i.e. NUMCOMP 4 with NOTRCPKT, NOEXTTRIG,
  NOCYCCNT and NOPRFCNT all set (ARMv7-M ARM DDI 0403D, book 1244, pp. C1-880–C1-881), and CYCCNT stayed 0 with
  TRCENA set. Anything timed by CYCCNT (SystemView's default Cortex-M timestamp, a DWT-based delay) reads zero there
  without a build or link error. carried over (2026-08-11 max32666fthr, tinyusb SystemView build).

## WCH QingKe (SDI)

- What a WCH-Link attach does to the target, by part: ch32v20x and ch32v30x survive an OpenOCD attach and halt with RAM
  intact, so a halted RAM dump works; ch32v10x resets on every attach and ch582m's OpenOCD attach resets it, so neither
  keeps a RAM ring across an attach. Streaming from a running hart is out on all of them (`rtt`, transport matrix).
  carried over (2026-08-11 nanoch32v203, 2026-09-04 ch32v307v_r1_1v0 dump; OpenOCD 0.12.0+dev-02620,
  `hathach/openocd` tinyusb branch).
- That OpenOCD's init halts the hart, so a tool reading the state after `init` sees `halted` on a running ch32v30x:
  `sysview_dump.py`'s `core.before` reads `halted` there (2026-10-22 ch32v307v_r1_1v0).
