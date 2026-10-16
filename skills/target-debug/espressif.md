# Espressif targets — JTAG with openocd-esp32, FreeRTOS apps, the USB ports

Part of the `target-debug` skill: read it first on any ESP32 chip, before
choosing probe tools. The methodology (ladder, locks, dual-side capture,
diagnosis standards) is SKILL.md's; this file is the Espressif backend: an
IDF environment, a different gdb per architecture, a different openocd (the
Espressif fork), and USB ports whose wiring decides whether the built-in
debugger exists while the firmware's own USB runs. Entries carry their
evidence the way `projects/` entries do; a measurement holds for the board,
firmware and tools it names.

## Environment, up front

```bash
. "$IDF_PATH/export.sh"      # ~/code/esp-idf; puts openocd-esp32, esptool and the gdbs on PATH
```

- openocd: the Espressif fork (`openocd-esp32`, `~/.espressif/tools/openocd-esp32/<ver>/...`);
  upstream OpenOCD lacks the `board/esp32*-builtin.cfg` files and the
  `esp_usb_jtag` driver. Record the version from its banner in every result.
- gdb: `xtensa-esp32-elf-gdb`, `xtensa-esp32s2-elf-gdb`, `xtensa-esp32s3-elf-gdb`
  for Xtensa chips, `riscv32-esp-elf-gdb` for RISC-V chips (C-series, P4,
  S31). `export.sh` may leave the RISC-V gdb off PATH: look under
  `~/.espressif/tools/riscv32-esp-elf-gdb/<version>/riscv32-esp-elf-gdb/bin/`
  (seen on ci.lan 2026-09-18, `17.1_20260402`). Record the path you used.
- esptool: flashing, `verify-flash`, `chip-id` (chip revision and MAC), eFuse
  reads through `espefuse`. The ELF is the firmware's own build output.

## What runs, and the debugger of choice

- An ESP-IDF application is a FreeRTOS app, and FreeRTOS task enumeration is
  openocd-esp32's default (`target/esp_common.cfg:26`; `set ESP_RTOS FreeRTOS`
  before the board cfg only makes it explicit): `info threads` with name,
  state and core; what each target showed is in its variant section. The ROM
  and the second-stage bootloader are bare: `set ESP_RTOS none`.
- The debugger of choice is JTAG with openocd-esp32, through the chip's
  built-in USB-Serial-JTAG (USJ) where the firmware leaves it usable, else
  through the JTAG pads with an FT2232-class probe (ESP-Prog) or the
  board's own FTDI. The USJ enumerates as `303a:1001` "USB JTAG/serial debug
  unit" with the chip MAC, upper case with colons, as iSerial; that string is
  openocd's selector: `adapter serial <MAC-with-colons>`. There is no probe
  serial; a rig config's flasher uid may name a different port (the
  project's notes say which).
- Flashing and reset go over the USJ CDC (esptool, DTR/RTS driven) or a
  board's UART bridge; which bridge line drives EN and which the boot strap
  is board wiring, recorded with each measured board below. The console is
  UART0 or the USJ CDC by sdkconfig (below).

## Attach

```bash
openocd -c 'set ESP_FLASH_SIZE 0' -c 'set ESP_RTOS FreeRTOS' \
        -f board/<chip>-builtin.cfg -c 'adapter serial <MAC-with-colons>' \
        -l <run dir>/openocd.log
# pad JTAG: -f board/esp32s3-ftdi.cfg -c 'adapter serial <FTDI serial>', or, for
#   an FTDI without an EEPROM serial, the pair with the selector between them:
#   -f interface/ftdi/<board>.cfg -c 'adapter usb location <bus>-<port[.port...]>' -f target/<chip>.cfg
<arch>-gdb -ex 'target extended-remote :3333' <elf>
(gdb) monitor halt / info threads / thbreak <app symbol> / continue / bt
(gdb) delete / monitor resume / detach / quit
# then, on telnet :4444: shutdown
```

Rules, each from a measured run (2026-10-15/16, htpc, openocd-esp32
v0.12.0-esp32-20260703, ESP-IDF v6.1 builds):

- **`set ESP_FLASH_SIZE 0` goes before the first `-f` that sources the
  target cfg** (`board/...`, or `target/<chip>.cfg` after an interface
  file). `target/esp_common.cfg:37-39` reads `ESP_FLASH_SIZE` into
  `_FLASH_SIZE` when it is sourced, and its gdb-attach event runs `reset
  halt` when `_FLASH_SIZE != 0` and memory protection is enabled (`:307-324`
  in 20260703, "Memory protection is enabled. Reset target to disable
  it..."). Set after the board file it avoided the reset on one app (tinyuf2
  blinky, P4) and not on another (tinyusb cdc_msc_freertos, P4); set before,
  openocd prints "ESP flash support is disabled!" and gdb connects without
  going through that path. It does not guarantee a reset-free attach: the
  2026-10-16 P4 run saw a panic and reset on one of three attaches (below).
  With flash support off there are no flash breakpoints and no `program`
  from openocd: flash with esptool where the board exposes a flashing port.
  A board without one (Metro S3 under an OTG app: no USJ, and no IO0 or
  ESP_TXD-to-RXD0 wiring) programs over JTAG in a separate session with
  flash support on, `program_esp <bin> <offset> verify`, `reset exit` on
  the last call; that command reprograms on a hash mismatch
  (`esp_common.cfg:657-663`), so it is not a verification route, and after
  one such session the Metro's scan chain read all ones until a chip reset
  by EN ("ESP32-S3").
- **Leave openocd with `shutdown`, never a kill.** On RISC-V chips IDF's
  `esp_cpu_set_watchpoint` (`components/esp_hw_support/cpu.c:153`) issues a
  semihosting `ebreak` while `esp_cpu_dbgr_is_attached()` is true, and FreeRTOS
  calls it on every context switch when `CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK=y`
  (`components/freertos/FreeRTOS-Kernel/portable/riscv/port.c:703`; on in
  the measured tinyusb cdc_msc_freertos and tinyuf2 P4 builds, off in
  tinyusb cdc_msc_throughput: check the build's sdkconfig). After a killed
  openocd the next `ebreak` has no handler: "Guru Meditation Error: Core 0
  panic'ed (Breakpoint)" and a reboot loop until an openocd `init; shutdown`
  session releases it or the chip is reset by EN or power, consistent with
  the debug module's active flag surviving core resets (inferred from the
  boot loops, not read from the hardware). With openocd attached the
  same call is one `debug_reason=00000001` halt per context switch in its
  log (2630 in a six minute P4 session, 1247 on the S31): those lines at
  task rate do not by themselves establish a fault, but the firmware's USB
  service degraded or stopped while they occurred, with and without host
  traffic, and one attach in three on the P4 and one in two on the S31 hit
  the `ebreak` mid-examination and panicked: the P4 reset ("panic'ed
  (Breakpoint)", `rst:0xc`), the S31 parked in the panic handler with no
  new banner (backtrace `vTaskSwitchContext` → `vPortSetStackWatchpoint` →
  `esp_cpu_set_watchpoint(wp_num=3)` → `esp_panic_handler`). A PMP Store
  access fault was also seen on the P4 and its contribution is unresolved.
  The same example rebuilt with `CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK=n`
  (same boards, tools and recipe, 2026-10-16) verified every sub-claim on
  both chips: no reset at the one examination per board, one
  `debug_reason=00000001` line in the whole log (the breakpoint hit), USB
  service kept through the attach, a 1 s halt and the hit under MSC and CDC
  traffic with no kernel message, and a clean `shutdown` with the device
  still answering (budget: the count rule below). With the option off
  neither run reproduced the service loss or the attach panic, whose
  backtrace runs through `vPortSetStackWatchpoint`; one clean attach per
  board does not bound the panic rate by itself.
  measured 2026-10-15/16 espressif_p4_function_ev v1.0 and
  espressif_s31_function_core, tinyusb cdc_msc_freertos ea1d867ce, openocd
  20260703; run records in `projects/tinyusb.md`.
- **After a `shutdown` that logged "could not remove watchpoint" errors,
  run a fresh `init; shutdown` before any reset.** `shutdown` is meant to
  clear the debug module (`esp_common.cfg:340-343` writes DM control 0 as a
  pre-shutdown command), yet on both the P4 and the S31 such a shutdown was
  followed, at the next esptool reset, by the Breakpoint reboot loop above
  (43 and 48 banners) until `init; shutdown` ended it; the residual state
  was not established (2026-10-16).
- **`monitor resume` before `detach`, then confirm the cores run.** gdb
  `detach` left both cores halted after an aborted `continue` (P4), after a
  failed `-batch` budget probe under traffic (P4, `gdb.md`) and after a
  clean temporary-breakpoint stop (S31, ~112 s until a script resumed them,
  the host resetting the device meanwhile): the rule holds for every detach
  (2026-10-16).
- **`delete` every breakpoint and watchpoint before leaving.** With flash
  support on, a breakpoint beyond the hardware budget is written into flash
  ("Initializing flasher algorithm for cmd_flash_bp_set"); a session that
  dies with one set leaves the app patched. measured 2026-10-15 Kaluga S2:
  the third `hbreak` went to flash, openocd was killed, the next boot failed
  the app checksum and read-back differed in 2 bytes at the breakpoint;
  reflashed. After any abnormal end, verify the build image through a
  non-writing route before reprogramming: `esptool --chip <c> --port
  /dev/serial/by-id/<port> verify-flash <offset> <bin>`, offsets from the
  build's `flasher_args.json`; without such a port (Metro S3), report
  verification pending, or restore and verify the pinned image under
  `SKILL.md`'s Delegated sessions, Cleanup.
- **Count breakpoints from the target's own evidence**: openocd prints
  `Found N triggers` per RISC-V core (P4: 3), and the usable count can be
  smaller: the 2026-10-16 P4 run (cdc_msc_freertos ea1d867ce, openocd
  20260703) inserted two hardware breakpoints and had the third refused
  (gdb "Cannot insert hardware breakpoint ...: Remote failure reply: 0E",
  openocd "No flash at address ... Failed to get flash bank (-4)", nothing
  written since flash support was off); the same chip took three with
  cdc_msc_throughput, whose sdkconfig has no end-of-stack watchpoint, and
  three with cdc_msc_freertos rebuilt with that option off (2026-10-16): on
  the P4 the stack watchpoint held one trigger (inferred from the matched
  builds, not read from the hardware; openocd also logged "could not remove
  watchpoint #0"). The S31 inserted four with the watchpoint on and off
  (with it on, `wp` listed the watchpoint only after their deletion and
  resume; concurrent allocation unchecked), so why it shows no shortfall is
  open.
  In these runs the refusal read as a failed flash-breakpoint fallback, not
  as "no free triggers" (when it surfaces: `gdb.md`). Xtensa prints
  no count: it is `xtopt ibreaknum` in `target/xtensa-core-<chip>.cfg` (S2:
  2; watchpoints are `xtopt dbreaknum` in the same file, untested), and the
  refusal of the third `hbreak` reads the same as on the P4 (2026-10-16,
  Kaluga, cdc_msc_freertos ea1d867ce). Nothing carries across chips.
- **Anchors**: a reset during the session shows as a new ROM banner
  (`rst:0x..`) in the console capture started before the attach. The USB
  device number proves nothing: the USJ kept its device number through a
  JTAG CPU reset on P4 and on S3 (its controller survives a core reset), and
  so can the firmware's own device. The reset cause openocd prints at attach
  is the last reset, not evidence about the attach, and such lines can
  first appear at the initial halt instead (Metro S3, 2026-10-16): their
  appearance after examination alone does not establish a new reset,
  correlate them with the pre-attach console baseline. On the Kaluga the
  initial examination printed "Debug controller was reset" and "Core was
  reset" with no new console banner and no USB disconnect (2026-10-16):
  those lines alone do not establish an attach-induced reset. A serial
  port opened mid-run can deliver a banner buffered from before the open,
  stamped at the open: take the banner baseline at the open, before the
  attach.
- Scripted sessions: halt via telnet `:4444` (or the Tcl port `:6666`,
  commands terminated by `\x1a`, `capture` for output) first, then read
  with gdb; xtensa-gdb batch `continue`/`interrupt` is async-flaky (seen 2026-07-24
  espressif_s3_devkitm, recorded in tinyusb 5066ac7b3). On openocd 20251215
  (P4, ci.lan, 2026-09-16) `info threads` right after connect printed one
  bare "Remote target" or "Too large number of threads", the tasks appeared
  after continuing into app code and halting, and thread attribution at a
  breakpoint was wrong on some hits; on 20260703 the P4 and S31 runs listed
  tasks at connect (2026-10-16). Enumeration and attribution are checked
  per target. Two openocds on one host clash on the default ports
  3333/4444/6666: the second dies binding its Tcl port, and a client pointed
  at the defaults drives the other board; give each session `gdb port`,
  `telnet port` and `tcl port` before `-f` and check `ss -ltnp` first (P4
  and S31 in parallel, 2026-10-16).
- ROM-frame backtraces (`0x4004xxxx` on S3, `0x4fc0xxxx` on P4, all `??`)
  mean the core is in ROM: break in app code (`thbreak <app symbol>`; TinyUSB's
  is in `projects/tinyusb.md`) for symbolized frames.

## Resets: core vs chip

ESP32-S3 TRM (book 4890 Rev 1.8) §7.1 p527-528: a **core reset** resets the
digital system except the RTC domain; a **system reset** includes RTC; a
**chip reset** is power-on, EN, brown-out or super watchdog. The USJ-driven
reset (reset cause 0x15 "USB (UART) reset"), `RTC_CNTL_SW_SYS_RST` (0x03,
what the Arduino 1200-baud touch and openocd's `reset` produced, console
`rst:0x3 (RTC_SW_SYS_RST)`) and the watchdogs' core resets are all core
resets. What survives them: `RTC_CNTL_RTC_OPTION1_REG` (0x6000812C, bit 0
`FORCE_DOWNLOAD_BOOT`, R/W, p628), the download-mode strap override (p536),
and pad hold. Consequences measured 2026-10-15 (Metro S3, Arduino sketch):
after a 1200-baud touch the ROM re-entered download mode on every core
reset, openocd's included, until EN was pressed; esptool's "hard reset" over
that USJ port left the chip in download mode with the same USB device
number, which cannot tell a core reset from no reset. In the 2026-10-15 P4
session the esptool reset over the USJ returned to the app unless another
process held the port; after a debug session it can instead enter the
Breakpoint boot loop (Attach).

## Console

Selection is sdkconfig: `CONFIG_ESP_CONSOLE_UART_DEFAULT` (UART0, 115200),
`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG`, and `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG`
(boot log and `ESP_LOG*` also on the USJ CDC). Name ports by
`/dev/serial/by-id` or `by-path`, never `ttyACM<N>`/`ttyUSB<N>`, which drift
within one boot. Opening the USJ CDC or a CP2102N with DTR/RTS asserted can
reset the chip: set the control lines before opening, as the capture tool
allows, and still expect one: a pyserial open with `dtr=rts=False` set
before `open()` reset the P4 (`rst:0x17 CHIP_USB_UART_RESET`, 2026-10-16),
so open the console before the baseline, never during a session.
Write-path cost, from esp-idf v6.1 source (not "ms per line"):

- UART0 without the UART driver (TinyUSB's espressif BSP installs none):
  `uart_vfs.c:186-196` spins only while the 128-byte TX FIFO has fewer than
  two free bytes, then writes one byte: a line shorter than the free FIFO
  costs register writes only; a full FIFO costs about 11 ms per 128 bytes at
  115200.
- USJ CDC without its driver: `usb_serial_jtag_vfs.c:150-171` writes while
  the FIFO accepts; a byte that cannot be written waits until 50 ms
  (`TX_FLUSH_TIMEOUT_US`, :78) have passed since the last accepted byte, then
  it and the following bytes are dropped until the host drains again. Each
  accepted byte restarts that clock, so this bounds one stalled byte, not a
  whole write, and the write also takes the VFS write lock (:194-205).
  Output is lost when nothing reads the port.
- With the drivers (`uart_vfs_dev_use_driver`, `usb_serial_jtag_vfs_use_driver`):
  the UART write goes through a TX ring only when the driver was installed
  with a nonzero TX buffer (`driver/uart.h:543-547`); with a zero TX buffer
  the call blocks on the driver's semaphore until the data is in the FIFO
  (`uart.c:1672-1683`); the USJ driver waits up to 50 ms once, then drops
  without waiting until a byte goes through again (`tx_tried_blocking`,
  `usb_serial_jtag_vfs.c:740-757`).
- Second-stage bootloader: `ESP_LOG*` → `esp_rom_vprintf`, polled
  (`log_print.h:30-31` under `BOOTLOADER_BUILD`).

The USJ CDC is a console only while the USJ exists: on the measured S3
boards, where the OTG controller takes the shared internal PHY, it
disappears when the firmware starts USB. When a reset makes the USJ
re-enumerate, the capture misses the beginning of the boot log.

## The USB ports per chip

| Chip | USJ | OTG | JTAG while the firmware's USB runs | Pad JTAG |
|---|---|---|---|---|
| ESP32-S2 | none (TRM 6572) | FS only, GPIO19/20 (datasheet 6570 p21, p24) | pads only | GPIO39-42 (TRM 6572 p182) |
| ESP32-S3 | GPIO19/20, **one internal PHY shared with OTG** (TRM 4890 §33.4.1 Table 33.4-1 p1252; datasheet 6571 p24-25) | FS, same pins by default | **no** over USJ on the measured boards (internal PHY only): the port flips from `303a:1001` to the firmware's device; yes over pads | GPIO39-42, selected by eFuses + GPIO3 (TRM 4890 Table 8.5-1 p538) |
| ESP32-P4 | GPIO24 (D-) / GPIO25 (D+), own FS PHY (datasheet 6569 p25, p28) | HS on dedicated pins 49/50, own PHY; FS OTG on GPIO26/27 | **yes**, both enumerated at once | yes |
| ESP32-S31 | GPIO33/34, own FS PHY (datasheet 6574 p30-31) | HS on pins 44/45 (p71) | **yes** | yes |

`EFUSE_USB_PHY_SEL` swaps the P4's two FS PHYs; `USB_SERIAL_JTAG_EXCHG_PINS`
swaps D+/D- (datasheet 6569 p28). The bootloader's flash offset is the ROM's
(`ESP_ROM_BOOTLOADER_OFFSET_FLASH` in IDF's `components/esp_rom/<chip>/esp_rom_caps.h`):
S2 0x1000, S3 0x0, P4 and S31 0x2000.

## Technique mapping (vs SKILL.md's ladder)

| target-debug technique | Espressif |
|---|---|
| GDB autopsy, bp/wp | same flow via openocd-esp32 `:3333`; budgets in Attach and per variant below |
| PC-sampling without a halt | none (no DWT_PCSR); repeated telnet `halt; reg pc; resume` halts the core per sample, more intrusive than a RAM ring and untested as a histogram |
| Vector catch | none: breakpoint the panic handler; RISC-V `mcause`/`mepc`/`mtval` are the CFSR/BFAR analogs |
| SWO / DWT data trace | none: apptrace over JTAG is the analog (needs `CONFIG_APPTRACE` + app init; untested) |
| Log lines | console above; no SEGGER RTT path in the measured TinyUSB builds |
| verifybin | `esptool --chip <c> --port <p> verify-flash <offset> <bin>`, image and offset from the build's `flasher_args.json`, never an objcopy of the ELF (verified 2026-10-15 P4, S2) |

## Variants

Each section: identity and selection; PHY and controller topology, then
board wiring; where flashing, reset and console go; attach and its effects
with the tool version; budgets; recovery; what is unverified.

### ESP32-S2

- Xtensa LX7, single core, no USJ: JTAG is the pads GPIO39-42 only. Boards
  measured: Kaluga v1.3 (`espressif_kaluga_1`): on-board FT2232HL without
  EEPROM, so no serial; the measured form is the pair with the selector
  between them, `-f interface/ftdi/esp32s2_kaluga_v1.cfg -c 'adapter usb
  location <bus>-<port[.port...]>' -f target/esp32s2.cfg`
  (`board/esp32s2-kaluga-1.cfg` sources the same two files; the selector
  after it is untested), and the UART by its `by-path` name.
- Attach examines without reset and keeps the OTG device enumerated (the
  2026-10-16 runs found the core running after server init, halted only by
  telnet `halt` or the gdb connect); gdb connect resets ("Memory protection
  is enabled") unless `ESP_FLASH_SIZE 0`. Two hardware breakpoints; the
  third becomes a flash breakpoint when flash support is on and is refused
  with it off (Attach, above). Halting inside `tud_mount_cb` left the
  host's SET_CONFIGURATION at -110 and a re-authorize at -32; only a chip
  reset recovered. `info threads` listed IDLE and usbd only, with openocd
  "No symbols for FreeRTOS!", both at the same `_WindowOverflow8` frame at
  connect; attribution was right at the breakpoint hit (cdc_msc_throughput).
  measured 2026-10-15/16 htpc, tinyusb cdc_msc_freertos and
  cdc_msc_throughput ea1d867ce, openocd 20260703, `xtensa-esp32s2-elf-gdb`.
  Watchpoint budget untested.

### ESP32-S3

- Xtensa LX7 dual core. One internal PHY, USJ by default, switched to the
  OTG controller by firmware (`RTC_CNTL_RTC_USB_CONF_REG` 0x60008120,
  `SW_HW_USB_PHY_SEL`/`SW_USB_PHY_SEL`, TRM 4890 Reg 10.56 p626-627): the USJ
  is present in ROM download mode, the second-stage bootloader and any app
  that never starts the OTG controller, and gone once the app's USB device
  enumerates (same hub port flips `303a:1001` → the app's VID:PID, openocd:
  `esp_usb_jtag: could not find or open device!`). On these boards live
  debugging of a USB app therefore uses pad JTAG; a board with an external
  PHY can keep OTG and USJ on separate connections (TRM 4890 Table 33.4-1
  p1252), not measured here.
- Pad JTAG source (TRM 4890 Table 8.5-1 p538): default all-zero eFuses route
  JTAG to the USJ; `DIS_USB_JTAG=1` with `DIS_PAD_JTAG=0` routes it to
  MTDI/MTCK/MTMS/MTDO (GPIO41/39/42/40) permanently, GPIO3 ignored, USJ CDC
  and USJ download kept (p412: `DIS_USB_SERIAL_JTAG` is the separate fuse
  that removes the whole device); `STRAP_JTAG_SEL=1` lets GPIO3 at reset
  choose (0 pads, 1 USJ), and GPIO3 has no internal pull (datasheet 6571 p34),
  so a floating pin gives a random source. eFuses are one-time.
- Firmware can take the pads back: the scan chain reads all zeroes while an
  app that drives GPIO39-42 runs and is fine in ROM. measured 2026-10-15
  Metro S3 with an Arduino sketch (its variant maps TX/RX/SCK/MOSI to
  IO40/41/39/42). A tinyusb build with `CFG_TUH_MAX3421` drives SCK/MOSI
  on GPIO39/42 (`projects/tinyusb.md`), so expect the same; not measured.
- Boards measured: Metro ESP32-S3 rev B (`DIS_USB_JTAG` burned 2026-10-15,
  Cortex 2x5 header X2 = pad JTAG, ESP-Prog cable pin-compatible, X2 pin 10 =
  EN; IO39/42 shared with the ICSP header and the microSD socket, IO40/41 are
  the D1/D0 header pins; UART0 only on header JP2, pin 1 TXD0; no tinyusb
  BSP, the `espressif_s3_devkitc` build runs). ESP-Prog: `board/esp32s3-ftdi.cfg`,
  `adapter serial <FTDI EEPROM serial>`, JTAG PWR SEL and PROG PWR SEL on
  3.3 V, PROG header pin 5 `ESP_RXD` listens to the target's TXD (measured
  2026-10-16: pin 3 captured nothing), PROG pin 1 `ESP_EN` wired to the
  board's EN: RTS asserted with DTR deasserted on the same FT2232 channel
  B UART (`if01`) pulls EN low (measured; the guide does not document the
  mapping), and opening that console port reset the chip once in the
  measured run even with `dtr=rts=False` set before and after the open
  (Console). The ESP-Prog JTAG header carries no reset (its pin 10 is NC,
  so X2 pin 10 EN stays unconnected; guide book 6576 p10). With
  cdc_msc_freertos and cdc_msc_throughput on OTG, attach examines both
  cores and leaves them running; halt, resume, a `thbreak tud_task_ext`
  hit and a 1 s halt under CDC and MSC traffic kept the device enumerated
  with no DUT USB reset, disconnect or error logged (2026-10-16,
  `projects/tinyusb.md`). After the `program_esp` session that
  reprogrammed the app (ended with `reset exit`), every attach read the
  scan chain as all ones, through a USB re-authorize of the probe, until
  EN was wired and pulsed (no attach between the wiring and the pulse);
  the same `reset exit` earlier that night, with flash content matching,
  had left the chain readable, so the trigger is not established. After
  the pulse both cores could be examined with the same app running
  (2026-10-16). esptool has no port to this board (Attach): images go over
  JTAG (`program_esp`), "Verify OK" on 2026-10-16.
- DevKitM (`espressif_s3_devkitm`, tinyusb rig): attach/halt/symbol
  resolution in the no-OTG window of `board_test`; USJ dropped ~4 s after boot
  with nothing attached and stayed up once a client survived that window;
  recorded in tinyusb 5066ac7b3 (2026-07-24), original transcript unavailable,
  contradicted on a DevKitC running tinyuf2 blinky (USJ up for minutes
  unattended, 2026-10-13). DevKitC-1 (`espressif_s3_devkitc` = tinyuf2
  `espressif_esp32s3_devkitc_1`, one board under two names in two projects'
  rosters whose holds do not exclude each other: hold it under both names in
  the shared lock store, and stop if both holds cannot be acquired): with
  sector 0 erased the ROM loops "invalid header: 0xffffffff" on UART0 and
  the USJ enumerates (2026-10-13). Flashing and UART0 console over the
  CP2102N (RTS to EN, DTR to IO0).
- Breakpoint budget: not found in TRM 4890; the tool's
  `target/xtensa-core-esp32s3.cfg` says `xtopt ibreaknum 2`, and the Metro
  inserted two and had the third refused (2026-10-16).

### ESP32-P4

- RISC-V dual HP core + LP core. USJ on its own FS PHY (GPIO24/25) and the
  HS OTG on dedicated pins: `303a:1001` and the firmware's device enumerate
  together, HS at 480 Mb/s. Three triggers per HP core (openocd `Found 3
  triggers`; TRM 6567 §2.11.3.2 p161 says three, and once "four").
- Attach (`board/esp32p4-builtin.cfg`, `riscv32-esp-elf-gdb`): server
  initialization examines both cores and leaves them running; a telnet
  `halt` or the gdb connect halts them. No reset on 2026-10-15, on two
  attaches of three on 2026-10-16 with cdc_msc_freertos (Attach, above),
  and on every attach with cdc_msc_throughput the same day (no end-of-stack
  watchpoint: no semihosting halts, three hardware breakpoints, a ~1 s halt
  plus one breakpoint hit under MSC and host-to-device CDC traffic with no
  kernel message; device-to-host CDC had stalled before the attach,
  `projects/tinyusb.md`) and with cdc_msc_freertos rebuilt with the
  watchpoint off (Attach). gdb connect resets through the memory-protection
  path unless `ESP_FLASH_SIZE 0` precedes the board file (Attach, above);
  that reset knocked the HS OTG device off the bus (descriptor reads -110,
  then silence) until a reset over the USJ brought it back. With the
  ordering right: `thbreak tud_task_ext` hit in the `usbd` task, backtrace
  `tud_task_ext` ← `tud_task` ← `usb_device_task` ← `vPortTaskWrapper`, eight
  tasks listed, CDC echo after `shutdown` (not reproduced on 2026-10-16
  with the stock build, reproduced with the watchpoint off).
  measured 2026-10-15/16 htpc, Function-EV v1.4 chip v1.0
  (`espressif_p4_function_ev`), tinyusb ea1d867ce, openocd 20260703. On 20251215 (ci.lan, chip
  v0.1, 2026-09-16) the same connect reset, and a second breakpoint armed
  after the first hit asserted at `riscv.c:2150`, preceded by "can't add
  write watchpoint ... resource not available"; cause not established, so
  budget one breakpoint per session on that version. `set ESP_ONLYCPU 1`
  degrades to cpu0-only debugging when cpu1's examination fails, and a
  breakpoint in a task scheduled on the other core then never hits
  (2026-09-16).
- Board: Function-EV v1.4 has no USJ connector; GPIO24 (D-) and GPIO25 (D+)
  are header J1 pins 27/28 behind 0 Ω resistors (schematic book 2064 p2-3),
  and swapped D+/D- wiring enumerates as "new low-speed USB device" with
  error -71 where the right wiring shows "new full-speed" (rig, 2026-09),
  HS OTG on the J15 Type-C and the J18 Type-A in parallel (one at a time),
  CP2102N on J20 drives EN (RTS) and GPIO35 boot mode (DTR). Build for the
  detected chip revision (v0.1 and v1.0 units exist on the benches) and
  investigate an image-compatibility rejection before reaching for esptool's
  `--force`; the rig roster's "`--force` for v0.1" note predates esptool 5.2.0.
- ROM USB download boot exists over the USJ and over the OTG controller, the
  latter without DFU (datasheet 6569 p37); the OTG download device enumerated
  full-speed on the rig (2026-09, PID not in the datasheet). Watchpoint
  budget untested.

### ESP32-S31

- RISC-V dual HP core + LP core, USJ on its own FS PHY (GPIO33/34), HS OTG
  on pins 44/45: both enumerate together. `board/esp32s31-builtin.cfg`
  (Tcl targets `esp32s31.hp.cpu0` and `.cpu1`), `riscv32-esp-elf-gdb`.
  Attach with `set ESP_RTOS none` halted both HP cores and the device kept
  answering in a telnet-only session (halt, `mdw`, `dump_image`, resume);
  no console capture ran, so reset-anchor coverage was limited
  (2026-10-14). With `ESP_RTOS FreeRTOS` on tinyusb cdc_msc_freertos
  ea1d867ce (end-of-stack watchpoint on) it behaved like the P4 (Attach,
  above): one attach of two panicked at examination and stayed halted
  there, the other examined both cores without reset and `thbreak
  tud_task_ext` hit in `usbd` with 9 tasks listed at connect; USB
  service degraded under the semihosting halts before the explicit 1 s
  halt, `detach` left both cores halted, and the post-`shutdown` esptool
  reset boot-looped until `init; shutdown`. On cdc_msc_throughput and on
  cdc_msc_freertos rebuilt with the watchpoint off, server initialization
  left both cores running without reset, gdb connect caused no reset
  either, and under HS MSC and CDC traffic the halt and one hit cost
  latency only (no kernel message); on cdc_msc_throughput the explicit 1 s
  halt found both cores in `esp_cpu_wait_for_intr`. openocd reports `Found
  4 triggers`; all three builds inserted four hardware breakpoints with the
  fifth refused (Attach's count rule). measured 2026-10-16 htpc,
  `espressif_s31_function_core`, openocd 20260703; run records in
  `projects/tinyusb.md`. Flashing and console went over the USJ CDC
  (bootloader 0x2000); the measured board's CP2102N bridge (user guide
  book 4899 p9) had an unusable receive path (2026-10-14). After a
  1200-baud touch (`FORCE_DOWNLOAD_BOOT`) a USJ RTS reset left the chip in
  download mode; the exit that worked was an esptool session on the ROM's
  OTG CDC (`303a:0020`) ending in a watchdog reset (2026-10-15, tinyuf2
  session). DWC2 base 0x20300000, GSNPSID 0x4F54430A (4.30a).
- Chip documents: TRM book 6558 Rev 0.1 has gaps and no OTG chapter; errata
  4887 lists only SPI-855 and ECDSA_DS-836.

## Limits

- Host-side: an Espressif USJ shares the hub with other DUTs on a bench; a
  wedge there is recovered per device (unbind, authorize, port power), never
  by a hub or controller reset that carries the other boards.
- Unverified: whether the Metro's esptool-over-USJ reset failure is board-
  or chip-level; each variant's own list above.
