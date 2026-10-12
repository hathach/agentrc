# TinyUF2 (`adafruit/tinyuf2`) — debugging notes

What is specific to this project when using `target-debug`, `rtt` or
`usb-kernel-debug` on it. For building, taking a rig board, commissioning or the
battery, follow the project's own `CLAUDE.md` (`Build contract:`, `HIL
contract:`), not this file.

Every entry says how it is known: `verified <date> <revision> <board>: <how>`.
`0.35.0` is the release image (`0.35.0 - tinyusb (2a364ca27)`) the 2026-10-12
commissioning evidence was taken with. **Before relying on a symbol or address,
check it in the checkout in front of you.**

## Probe tools

- `rtt.py` and `pc_sample.py` resolve the probe serial and J-Link device from
  `--hil-config test/hil/local.json --board <name>`, the roster name, not its
  `tinyuf2.lock`. Pass `--interface swd --speed 4000`, the HIL runner's fixed
  J-Link settings; the roster carries neither.
  verified 2026-10-12 302bae9 mimxrt1060_evk: both resolved 000726129165 /
  MIMXRT1062xxx6A; without the two flags `rtt.py` exits 2.
- A release ships no ELF, and source builds link differently from 0.35.0 (below),
  so PC samples of a release stay unsymbolized: build that release's tag for
  symbols. verified 2026-10-12 mimxrt1060_evk: 50/50 samples at 0x0000xxxx, all
  unresolved against a 302bae9 ELF.

## imxrt1060_evk (rig lock `mimxrt1060_evk`)

- MIMXRT1060-EVK (not -EVKB), schematic SCH-31357 rev A2 from its label; the
  library has rev A3 (book 1673).
- The bootloader enumerates on USB OTG1, 239a:0083. Its serial is the OCOTP
  CFG0..CFG3 words (0x401F4410..0x401F4440) as little-endian bytes in hex
  (`board_usb_get_serial`), so it can be predicted from a `mem32` before
  TinyUF2 runs. verified 2026-10-12 0.35.0: predicted 4629F865D2291713E000005002004200,
  enumerated the same.
- Silicon: DIGPROG (0x400D8260) reads 0x006C0000 on RT1060_A, which the reference
  manual calls "silicon revision 1.0"; registers do not tell A from B, the
  marking does (6A here). verified 2026-10-12, RM rev 4 p2551.
- 0.35.0 is ITCM-linked: its IVT boot data copies flash 0x60000000..0x6000A000
  into ITCM on every boot, and it runs at 0x0000xxxx. Current source links the
  boot image to OCRAM (`_fcfb_origin` 0x20208000). verified 2026-10-12: IVT
  parse of the release BIN, PC samples.
- Double-tap register: `SNVS->LPGPR[3]` = 0x400D410C (RM rev 4 §20.6.1.17 p1258;
  low-power domain, reset only by LP POR, p1235), magic 0xF01669EF
  (`DBL_TAP_MAGIC`). Writing it over J-Link with the core halted, then
  SYSRESETREQ, enters DFU, and the bootloader leaves it 0. verified 2026-10-12
  9045fc0: commissioning run, `magicConsumed` true; that it survives SYSRESETREQ is
  bench evidence, the RM does not name that reset.
- `CURRENT.UF2` reads all zeros before 302bae9: `src/ghostfat.c` bounds blocks at
  `BOARD_FLASH_ADDR_ZERO + flash size`, and mimxrt10xx left it 0 while the app
  starts at 0x6000C000. verified 2026-10-12: battery fails at reenter readback on
  9af708f and passes on 302bae9; 93ba6c1 passes and its define-reverted baseline
  e657927 fails ("block 0: bad magic").
- Every J-Link reset while the app vector is invalid prints "Could not find
  valid reset vector." then "Target halted on flash header read."; the reset
  worked. verified 2026-10-12 0.35.0, in two J-Link installs.
- After an image `loadbin` at 0x60000000, treat the rest of its 64 KiB block as
  erased; a sentinel at 0x60010000, in the next block, survived. What triggers the erase is not established:
  16-byte sentinel `loadbin`s in that block kept their sampled neighbours.
  verified 2026-10-12 at sampled points only: 0.35.0 by hand (the HIL
  contract's Limits give them), and dcc241e's install image (ending at
  0x6000C200) left 0x6000C200 and 0x6000FF00 reading 0xFF in the same session,
  before any reset, and 0x60010000 unchanged.
- With no boot image (the HIL cleanup's zero page at 0x60000000), new J-Link
  sessions misread flash: zeros, and at 0x60010000 the bytes written at
  0x60000200, so a plain `verifybin` fails. A `loadbin` first, which prints
  "Skipped. Contents already match" when nothing changed, makes later reads and
  `verifybin` correct; the HIL cleanup re-verifies that way. The cause is not
  established. verified 2026-10-12 dcc241e, repeatable.
- In a hand-written JLinkExe command file, `-ExitOnError 0` still exits at a
  failed `verifybin` and leaves the core halted: resume it from a new session.
  verified 2026-10-12.
- DTCM at 0x20000000 is enabled while TinyUF2 runs; 0x20000FFC (the battery
  fixture's first stack word) took a write and read it back under a halted core.
  verified 2026-10-12 0.35.0 in DFU. The split is the fuse default unless
  IOMUXC_GPR16 selects GPR17 (RM rev 4 p362-363, p1308).

## lpcxpresso55s69

- TinyUF2 uses USB0 full speed (`BOARD_TUD_RHPORT` 0), the P10 connector;
  tinyusb examples use P9. verified 2026-10-12 0.35.0: enumerated only once
  re-cabled to P10.
- Rev 0A silicon (`SYSCON->DIEID` 0x000426B0, REV_ID 0, ROM 2.2): 0.35.0 hangs in
  the rev-0 ROM `FLASH_Read` (API entry 0x130043a3) on any UF2 copy, and its USB serial
  is not the device UUID at 0x9FC70 (adafruit/tinyuf2#500, #501). verified
  2026-10-12 0.35.0.
- The RTC that holds the double-tap register (GPREG) is not usable right after a
  reset-halt: its register clock (AHBCLKCTRL0 bit 23) resets off, and after
  power-up CTRL.SWRESET holds every RTC register at reset (UM11126 rev 2.1 p54,
  p570). There a J-Link `mem32` of GPREG returned "Could not read memory", and a
  write followed by `r` left the reset failing. Write it once the bootloader runs.
  verified 2026-10-12 0.35.0; which prerequisite the bench hit is not established.

## Host

- `JLinkExe` on htpc can crash in `libjlinkarm` at the probe connect, before the
  target connect (SIGSEGV; once a glibc double-free abort); the cause is unknown.
  The HIL runner retries such a session once (`jlinkRetried`). verified
  2026-10-12 mimxrt1060_evk: 2 of 94 runner sessions.
- Keep the probe off the hub of a device that hangs mid-write: the xHCI keeps
  retrying its enumeration, and a J-Link there stalled for ~80 s. verified
  2026-10-12 lpcxpresso55s69 (that the hub is the cause is a hypothesis).
