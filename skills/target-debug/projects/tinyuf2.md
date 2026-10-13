# TinyUF2 (`adafruit/tinyuf2`) — debugging notes

What is specific to this project when using `target-debug`, `rtt` or
`usb-kernel-debug` on it. For building, taking a rig board, probe settings,
cabling, commissioning or the battery, follow the project's own `CLAUDE.md`
(`Build contract:`, `HIL contract:`), not this file; chip and probe behaviour
is in `families.md` and the technique files beside SKILL.md.

Entries carry their evidence as SKILL.md's "Project notes" says. `0.35.0` is
the release image (`0.35.0 - tinyusb (2a364ca27)`) the 2026-10-12 evidence was
taken with. **Before relying on a symbol or address, check it in the checkout
in front of you.**

## Release images

- A release ships no ELF, and source builds can link differently (mimxrt10xx
  below), so PC samples of a release stay unsymbolized: build that release's
  tag for symbols. verified 2026-10-12 mimxrt1060_evk: 50/50 samples at
  0x0000xxxx, all unresolved against a 302bae9 ELF.

## mimxrt10xx

- 0.35.0 is ITCM-linked: its IVT boot data copies flash 0x60000000..0x6000A000
  into ITCM on every boot, and it runs at 0x0000xxxx. Current source links the
  boot image to OCRAM (`_fcfb_origin` 0x20208000). verified 2026-10-12
  mimxrt1060_evk: IVT parse of the release BIN, PC samples.
- The bootloader enumerates as 239a:0083
  (`ports/mimxrt10xx/boards/imxrt1060_evk/board.h`). Its USB serial is the
  OCOTP CFG0..CFG3 words (0x401F4410..0x401F4440) as little-endian bytes in
  hex (`board_usb_get_serial`), so it can be predicted from a `mem32` before
  TinyUF2 runs. verified 2026-10-12 0.35.0 mimxrt1060_evk: both as predicted.
- Double-tap register: `SNVS->LPGPR[3]` = 0x400D410C, magic 0xF01669EF
  (`DBL_TAP_MAGIC`). Writing it over J-Link with the core halted, then
  SYSRESETREQ, enters DFU, and the bootloader leaves it 0. verified 2026-10-12
  9045fc0 mimxrt1060_evk: commissioning run, `magicConsumed` true. Its reset
  domain: `families.md`, "i.MX RT1060".
- Before a J-Link `loadbin` or `verifybin` on a board with no boot image:
  `families.md`, "i.MX RT1060" (block erase, misreads, the reset-vector
  message).
- **`CURRENT.UF2` reads all zeros**: `src/ghostfat.c` bounds blocks at
  `BOARD_FLASH_ADDR_ZERO` + flash size; mimxrt10xx left it 0 while the app
  starts at 0x6000C000, so the bound excluded the app. Fixed in 302bae9.
  verified 2026-10-12 mimxrt1060_evk: battery fails at reenter readback on
  9af708f and passes on 302bae9; 93ba6c1 passes and its define-reverted
  baseline e657927 fails ("block 0: bad magic").

## lpc55

- The double-tap register is the RTC's GPREG: write it once the bootloader
  runs, not right after a reset-halt (`families.md`, "LPC55", has the
  evidence).
- **Every UF2 copy hangs**: on rev 0A silicon (REV_ID 0; `families.md`,
  "LPC55") 0.35.0 hangs in the rev-0 ROM `FLASH_Read` (API entry 0x130043a3),
  and its USB serial is not the device UUID at 0x9FC70 (adafruit/tinyuf2#500,
  #501). verified 2026-10-12 0.35.0 lpcxpresso55s69.

## espressif

843fef8 below is TinyUF2's unmerged `hil-esp32s3` branch; its bootloader
source is master 9d4b382's.

- The bootloader enumerates as 239a:00a5 on the devkitc (`USB_PID` in its
  `board.h`, shared with the S2 saola wrover, a TODO there), its USB serial the
  chip's factory MAC in hex. verified 2026-10-13 843fef8
  espressif_esp32s3_devkitc_1: sysfs and `lsusb`.
- An app enters TinyUF2 with reset hint 0x11F2 (`ports/espressif/README.md`);
  the second-stage bootloader (`bootloader_components/main/bootloader_start.c`,
  `APP_REQUEST_UF2_RESET_HINT`) honours it only after a software reset
  (`RESET_REASON_CORE_SW`/`CPU0_SW`) from a non-factory boot, then clears it.
  verified 2026-10-13 843fef8 espressif_esp32s3_devkitc_1: the battery's
  reenter and 10 DFU_DETACH cycles of its app.
- While TinyUF2 or the tested app owns the native port, USB-Serial-JTAG
  (303a:1001) is absent; logs and reset go over the UART bridge
  (`esp-target-debug`, "The PHY map").
  verified 2026-10-13 843fef8 espressif_esp32s3_devkitc_1: sysfs during the
  battery.
- **dmesg shows "reset full-speed USB device ... device firmware changed" and
  "device offline error, dev sda" at the end of a UF2 copy**: TinyUF2 restarts
  while usb-storage still talks to it. verified 2026-10-13 843fef8
  espressif_esp32s3_devkitc_1: on successful copies only, never on
  DFU_DETACH, in a 10-cycle run; they do not by themselves show a copy
  succeeded.
