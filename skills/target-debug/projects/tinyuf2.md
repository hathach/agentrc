# TinyUF2 (`adafruit/tinyuf2`) — debugging notes

What is specific to this project when using `target-debug`, `rtt` or `usb-kernel-debug` on it. For building, taking a
rig board, probe settings, cabling, commissioning or the battery, follow the project's own `CLAUDE.md`
(`Build contract:`, `HIL contract:`), not this file; chip and probe behaviour is in `families.md` and the technique
files beside SKILL.md.

Entries carry their evidence as SKILL.md's "Project notes" says. `0.35.0` is the release image
(`0.35.0 - tinyusb (2a364ca27)`) the 2026-10-12 evidence was taken with. **Before relying on a symbol or address, check
it in the checkout in front of you.**

## Release images

- A release ships no ELF, and source builds can link differently (mimxrt10xx below), so PC samples of a release stay
  unsymbolized: build that release's tag for symbols. verified 2026-10-12 mimxrt1060_evk: 50/50 samples at 0x0000xxxx,
  all unresolved against a 302bae9 ELF.

## mimxrt10xx

- 0.35.0 is ITCM-linked: its IVT boot data copies flash 0x60000000..0x6000A000 into ITCM on every boot, and it runs at
  0x0000xxxx. Current source links the boot image to OCRAM (`_fcfb_origin` 0x20208000). verified 2026-10-12
  mimxrt1060_evk: IVT parse of the release BIN, PC samples.
- The bootloader enumerates as 239a:0083 (`ports/mimxrt10xx/boards/imxrt1060_evk/board.h`). Its USB serial is the OCOTP
  CFG0..CFG3 words (0x401F4410..0x401F4440) as little-endian bytes in hex (`board_usb_get_serial`), so it can be
  predicted from a `mem32` before TinyUF2 runs. verified 2026-10-12 0.35.0 mimxrt1060_evk: both as predicted.
- Double-tap register: `SNVS->LPGPR[3]` = 0x400D410C, magic 0xF01669EF (`DBL_TAP_MAGIC`). Writing it over J-Link with
  the core halted, then SYSRESETREQ, enters DFU, and the bootloader leaves it 0. verified 2026-10-12 9045fc0
  mimxrt1060_evk: commissioning run, `magicConsumed` true. Its reset domain: `families.md`, "i.MX RT1060".
- Before a J-Link `loadbin` or `verifybin` on a board with no boot image: `families.md`, "i.MX RT1060" (block erase,
  misreads, the reset-vector message).
- **`CURRENT.UF2` reads all zeros**: `src/ghostfat.c` bounds blocks at `BOARD_FLASH_ADDR_ZERO` + flash size; mimxrt10xx
  left it 0 while the app starts at 0x6000C000, so the bound excluded the app. Fixed in 302bae9. verified 2026-10-12
  mimxrt1060_evk: battery fails at reenter readback on 9af708f and passes on 302bae9; 93ba6c1 passes and its
  define-reverted baseline e657927 fails ("block 0: bad magic").

## lpc55

- The double-tap register is the RTC's GPREG: write it once the bootloader runs, not right after a reset-halt
  (`families.md`, "LPC55", has the evidence).
- **Every UF2 copy hangs**: on rev 0A silicon (REV_ID 0; `families.md`, "LPC55") 0.35.0 hangs in the rev-0 ROM
  `FLASH_Read` (API entry 0x130043a3), and its USB serial is not the device UUID at 0x9FC70 (adafruit/tinyuf2#500,
  #501). verified 2026-10-12 0.35.0 lpcxpresso55s69.

## espressif

- The bootloader enumerates as 239a:00a5 on the devkitc (`USB_PID` in its `board.h`, shared with the S2 saola wrover, a
  TODO there), its USB serial the chip's factory MAC in hex. verified 2026-10-13 2b558bd espressif_esp32s3_devkitc_1:
  sysfs and `lsusb`.
- An app enters TinyUF2 with reset hint 0x11F2 (`ports/espressif/README.md`); the second-stage bootloader
  (`bootloader_components/main/bootloader_start.c`, `APP_REQUEST_UF2_RESET_HINT`) honours it only after a software reset
  (`RESET_REASON_CORE_SW`/`CPU0_SW`) from a non-factory boot, then clears it. verified 2026-10-13 2b558bd
  espressif_esp32s3_devkitc_1: the battery's reenter and 10 DFU_DETACH cycles of its app.
- While TinyUF2 or the tested app owns the native port on the S3, USB-Serial-JTAG (303a:1001) is absent; logs and reset
  go over the UART bridge (`espressif.md`, "The USB ports per chip"). verified 2026-10-13 2b558bd
  espressif_esp32s3_devkitc_1: sysfs during the battery.
- **dmesg shows "reset full-speed USB device ... device firmware changed" and "device offline error, dev sda" at the end
  of a UF2 copy**: TinyUF2 restarts while usb-storage still talks to it. verified 2026-10-13 2b558bd
  espressif_esp32s3_devkitc_1: on successful copies only, never on DFU_DETACH, in a 10-cycle run; they do not by
  themselves show a copy succeeded.
- The four entries above are S3 measurements. `espressif_esp32s3_devkitc_1` is tinyusb's `espressif_s3_devkitc`, one
  board in two rosters whose holds do not exclude each other: hold it under both names in the shared lock store, and
  stop if both holds cannot be acquired. `ports/espressif/sdkconfig.defaults` and the blinky, dfu_runtime,
  erase_firmware and update_tinyuf2 apps set `CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK=y` (fd60fc5): on a RISC-V port
  under openocd that is the halt storm `espressif.md` describes ("The end-of-stack watchpoint").
- P4 (`espressif_p4_function_ev`, 8e75969 #532): 239a:00a5 as well (the shared-PID TODO in `board.h`), UF2 board id
  `ESP32P4-FunctionEV-v1.4`, volume `P4FEVBOOT`, button GPIO35. A software restart on the P4 left the OTG HS device
  attached, so the host kept a device that no longer answered and a UF2 copy's `mcopy` hung ~60 s on SCSI resets;
  0ed85c1 (2026-10-14) detaches OTG HS before the restart. verified 2026-10-14 htpc: 10/10 UF2 copies and 5/5 DFU_DETACH
  cycles clean after the fix.
- S31 (`espressif_esp32s31_func_core`, ba97530 #554, 2026-10-15): 239a:00a5 shared too, board id `ESP32S31-FCB1-v1.0`,
  button GPIO61, NeoPixel GPIO60. The USB serial is the 6-byte factory MAC via `esp_read_mac(ESP_MAC_EFUSE_FACTORY)`
  (`esp_efuse_mac_get_default` returns the 8-byte EUI-64 on 802.15.4-capable targets). The P4 detach-before-restart path
  covers the S31. The second-stage bootloader drives the NeoPixel through dedicated GPIO channel 0 with
  `NEOPIXEL_T0H_NS=250` (`bootloader_start.c:322-329`). source-checked at ba97530, 2026-10-16; none of it exercised on
  hardware in this skill's sessions.
- S31 commissioning, htpc 2026-10-21, tinyuf2 2346572, esptool 5.3.1 (5.2.0 cannot reach its flash over the USJ):
  chip revision v0.0, uid 30EDA0F40A94, 16 MB flash (0x46/0x4018). `get-security-info` (stub and `--no-stub` ROM) and
  `espefuse summary` read secure boot, flash encryption and secure download off; those field meanings rest on esptool
  5.3.1's espefuse map only, as the TRM's eFuse chapter (book 6558 v0.1) is still to be added. 5.3.1's RTS hard reset
  brings TinyUF2 up, not the ROM loader. `--try-reentry` decided `dfu-detach`; 10 DFU_DETACH cycles under a hold showed
  disconnect and re-enumeration only, no USB reset on DETACH. verified 2026-10-21 htpc: run-20261021-234618-912997.
