# Espressif targets — JTAG with openocd-esp32, FreeRTOS apps, the USB ports

Part of the `target-debug` skill: read it first on an ESP32 chip, before choosing probe tools. The methodology (ladder,
locks, dual-side capture, diagnosis standards) is SKILL.md's; this file is the Espressif backend: an IDF environment, a
different gdb per architecture, a different openocd (the Espressif fork), and USB ports whose wiring decides whether the
built-in debugger exists while the firmware's own USB runs. The measured chips are the S2, S3, P4 and S31; another chip
starts from its own documents. Entries carry their evidence the way `projects/` entries do; a measurement holds for the
board, firmware and tools it names.

## The chips at a glance

| Chip      | Cores            | gdb                      | JTAG route and config | Breakpoints ("Breakpoint budgets") | JTAG while the firmware's USB runs | Flash, reset, console on the measured boards | Bootloader offset |
| --------- | ---------------- | ------------------------ | --- | --- | --- | --- | --- |
| ESP32-S2  | Xtensa LX7, 1    | `xtensa-esp32s2-elf-gdb` | pads only; Kaluga: its FT2232HL, `-f interface/ftdi/esp32s2_kaluga_v1.cfg -c 'adapter usb location <loc>' -f target/esp32s2.cfg` | 2 instruction + 2 data; the third `hbreak` refused; data breakpoints untested | pads | Kaluga: UART by `by-path` for flash and console | 0x1000 |
| ESP32-S3  | Xtensa LX7, 2    | `xtensa-esp32s3-elf-gdb` | USJ `board/esp32s3-builtin.cfg` only until the app starts OTG; pads `board/esp32s3-ftdi.cfg` once the eFuses route them | 2 + 2; the third refused (Metro, with and without the end-of-stack watchpoint); data breakpoints untested | no over the USJ on the measured internal-PHY boards, yes over pads | DevKitC-1: CP2102N (UART0, EN, boot strap); Metro: images over JTAG, UART0 on a header only | 0x0 |
| ESP32-P4  | RISC-V HP 2 + LP | `riscv32-esp-elf-gdb`    | USJ `board/esp32p4-builtin.cfg -c 'adapter serial <MAC>'` | 3 triggers per HP core, breakpoints and watchpoints shared; firmware may hold one; watchpoint insertion untested | yes, USJ and HS OTG on their own PHYs | Function-EV: USJ CDC or the CP2102N on J20 for flash and reset; the 2026-10-16 runs captured the console on the USJ CDC | 0x2000 |
| ESP32-S31 | RISC-V HP 2 + LP | `riscv32-esp-elf-gdb`    | USJ `board/esp32s31-builtin.cfg -c 'adapter serial <MAC>'` | 4 triggers per HP core, all four inserted, also with the end-of-stack watchpoint build, whose attach panicked ("The end-of-stack watchpoint") | yes | CoreBoard: USJ CDC for flash, reset and console (its CP2102N receive path was unusable, 2026-10-14) | 0x2000 |

Pins, PHYs and their citations: "The USB ports per chip". The bootloader offset is the ROM's
(`ESP_ROM_BOOTLOADER_OFFSET_FLASH` in IDF's `components/esp_rom/<chip>/esp_rom_caps.h`). Board wiring, recovery and what
is unverified: "Variants".

## Tools

```bash
. "$IDF_PATH/export.sh"      # ~/code/esp-idf; puts openocd-esp32, esptool and the gdbs on PATH
```

- openocd: the Espressif fork (`openocd-esp32`, `~/.espressif/tools/openocd-esp32/<ver>/...`); upstream OpenOCD lacks
  the `board/esp32*-builtin.cfg` files and the `esp_usb_jtag` driver. Record the version from its banner in every
  result. FreeRTOS task enumeration is its default (`target/esp_common.cfg:26`; `set ESP_RTOS FreeRTOS` before the board
  cfg only makes it explicit): `info threads` with name, state and core. The ROM and the second-stage bootloader are
  bare: `set ESP_RTOS none`.
- gdb: one per architecture (the table; `xtensa-esp32-elf-gdb` for the ESP32, `riscv32-esp-elf-gdb` for the C series
  too). `export.sh` may leave the RISC-V gdb off PATH: look under
  `~/.espressif/tools/riscv32-esp-elf-gdb/<version>/riscv32-esp-elf-gdb/bin/` (seen on ci.lan 2026-09-18,
  `17.1_20260402`). Record the path you used.
- esptool: flashing, `verify-flash`, `chip-id` (chip revision and MAC), `read-mem`/`write-mem` for registers while the
  ROM is in download mode, eFuse reads through `espefuse`; its reset route is under "Resets". The ELF is the firmware's
  own build output.
- The USJ enumerates as `303a:1001` "USB JTAG/serial debug unit" with the chip MAC, upper case with colons, as iSerial;
  that string is openocd's selector, `adapter serial <MAC-with-colons>`, and its CDC is
  `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00` (`lsusb -v -d 303a:1001` prints the iSerial).
  There is no probe serial, and a rig's flasher uid need not name the debug transport (`projects/tinyusb.md`, "Espressif
  integration").

## Attach

JTAG with openocd-esp32, through the chip's built-in USB-Serial-JTAG (USJ) where the firmware leaves it usable, else
through the JTAG pads with an FT2232-class probe (ESP-Prog) or the board's own FTDI. Rules below come from measured runs
(2026-10-15/16, htpc, openocd-esp32 v0.12.0-esp32-20260703, ESP-IDF v6.1 builds) unless they name another date.

### Before attaching

- **Anchors.** Open the console first and take the ROM-banner baseline at the open; a reset during the session then
  shows as a new ROM banner (`rst:0x..`) in that capture. Opening a port can itself reset the chip ("Console"), so for
  the autopsy of a wedged board use a port without that effect (the Metro's JP2 UART0 has no control lines) or forgo the
  baseline and say so.
  - Not anchors: the USB device number (the USJ kept its device number through a JTAG CPU reset on the P4 and the S3,
    whose USJ is unaffected by its own SRST, TRM 4890 p1249, its survival of other core resets undocumented; the
    firmware's own device can keep its number too); the reset cause openocd prints at attach, or first at the initial
    halt (Metro S3, 2026-10-16), which is the last reset; "Debug controller was reset" and "Core was reset" at
    examination (Kaluga, 2026-10-16, with no new banner and no USB disconnect); a banner delivered by a port opened
    mid-run, buffered from before the open and stamped at the open.
- Two openocds on one host clash on the default ports 3333/4444/6666: the second dies binding its Tcl port, and a client
  pointed at the defaults drives the other board. Check `ss -ltnp` and give each session `gdb port`, `telnet port` and
  `tcl port` before `-f` (P4 and S31 in parallel, 2026-10-16).
- On a RISC-V chip, read the build's sdkconfig for `CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK`: with it on, the attach
  itself panicked the app on some tries and USB service degraded or stopped on the measured P4 and S31 builds. For a
  fresh reproduction rebuild with it off first ("The end-of-stack watchpoint", recipe at its end); a wedge already on
  the board keeps its image (`gdb.md`: an autopsy never rebuilds), so attach knowing that risk and report it.

### Attach and inspect

```bash
openocd -c 'set ESP_FLASH_SIZE 0' -c 'set ESP_RTOS FreeRTOS' \
        -f board/<chip>-builtin.cfg -c 'adapter serial <MAC-with-colons>' \
        -l <run dir>/openocd.log
# pad JTAG: -f board/esp32s3-ftdi.cfg -c 'adapter serial <FTDI serial>', or, for
#   an FTDI without an EEPROM serial, the pair with the selector between them:
#   -f interface/ftdi/<board>.cfg -c 'adapter usb location <bus>-<port[.port...]>' -f target/<chip>.cfg
<arch>-gdb -ex 'target extended-remote :3333' <elf>
(gdb) monitor halt          # or inspect a halt the firmware reached itself
(gdb) info threads
(gdb) bt
(gdb) thbreak <app symbol>  # to reach app code deliberately, then
(gdb) continue
```

`esp_session.py` runs the read-only part as one bounded session and ends it the way Leave says even when gdb dies
(`--help`; runs in `projects/tinyusb.md`):

```bash
python3 <skill dir>/scripts/esp_session.py inspect --out <new dir> --board-cfg board/<chip>-builtin.cfg \
  --adapter-serial <MAC> --rtos FreeRTOS --gdb <arch gdb> --elf <flashed.elf> --anchor <console capture> [--expr ...]
#   budget: --at <location> ... instead of --expr; recover: the route only (init; shutdown);
#   a server left running after exit 3: leave --receipt <out>/receipt.json
```

- Server initialization examines the cores and leaves them running (S2, S3, P4, S31 measured 2026-10-16); a telnet
  `halt` or the gdb connect halts them. Keep the three apart in a record: examination, gdb connect and a deliberate halt
  each change state differently.
- **`set ESP_FLASH_SIZE 0` goes before the first `-f` that sources the target cfg** (`board/...`, or `target/<chip>.cfg`
  after an interface file). `target/esp_common.cfg:37-39` reads `ESP_FLASH_SIZE` into `_FLASH_SIZE` when it is sourced,
  and its gdb-attach event runs `reset halt` when `_FLASH_SIZE != 0` and memory protection is enabled (`:307-324` in
  20260703, "Memory protection is enabled. Reset target to disable it..."). Set after the board file it avoided the
  reset on one app (tinyuf2 blinky, P4) and not on another (tinyusb cdc_msc_freertos, P4); set before, openocd prints
  "ESP flash support is disabled!" and gdb connects without that path. It does not guarantee a reset-free attach ("The
  end-of-stack watchpoint"). With flash support off there are no flash breakpoints and no `program` from openocd: flash
  with esptool where the board exposes a flashing port. A board without one programs over JTAG in a separate session
  with flash support on, `program_esp <bin> <offset> verify`, `reset exit` on the last call; that command reprograms on
  a hash mismatch (`esp_common.cfg:657-663`), so it is not a verification route (the Metro S3 under an OTG app:
  "ESP32-S3").
- Scripted sessions: halt via telnet `:4444` (or the Tcl port `:6666`, commands terminated by `\x1a`, `capture` for
  output) first, then read with gdb; xtensa-gdb batch `continue`/`interrupt` is async-flaky (seen 2026-07-24
  espressif_s3_devkitm, recorded in tinyusb 5066ac7b3). Thread enumeration and attribution are checked per target and
  tool version: on 20260703 the P4 and S31 listed tasks at connect; the 20251215 behaviour is under "ESP32-P4".
- ROM-frame backtraces (`0x4004xxxx` on S3, `0x4fc0xxxx` on P4, all `??`) mean the core is in ROM: break in app code
  (`thbreak <app symbol>`; TinyUSB's is in `projects/tinyusb.md`) for symbolized frames.

### Breakpoint budgets

Count from the target's own evidence; nothing carries across chips. The probe procedure (`always-inserted`, where the
refusal shows, what a `-batch` abort leaves behind) is `gdb.md`'s; `esp_session.py budget --at <location> ...` runs it
with the cleanup outside gdb (builds without the end-of-stack watchpoint, 2026-10-16: P4 exactly 3, S31 exactly 4).

- RISC-V: openocd prints `Found N triggers` per core (P4: 3, which TRM 6567 §2.11.3.2 p161 says, and once "four"; S31:
  4), breakpoints and watchpoints share them, and firmware can hold one (the end-of-stack watchpoint held one of the
  P4's three, inferred below). The refusal is gdb "Cannot insert hardware breakpoint ...: Remote failure reply: 0E" with
  openocd "No flash at address ... Failed to get flash bank (-4)": a failed flash-breakpoint fallback, nothing written
  while flash support is off.
- Xtensa: openocd prints no count; the Debug Option allows up to two instruction and two data breakpoints (Cadence ISA
  Summary book 6577 Table 121 p257), IDF's `core-isa.h` sets two of each for ESP32, S2 and S3 (`XCHAL_NUM_IBREAK`,
  `XCHAL_NUM_DBREAK`), and the tool carries them as `xtopt ibreaknum` and `xtopt dbreaknum` in
  `target/xtensa-core-<chip>.cfg`; the third `hbreak` was refused the same way on the S2 and the S3 with flash support
  off (2026-10-16, Kaluga and Metro, tinyusb ea1d867ce), and went into flash on the S2 with it on (2026-10-15); data
  breakpoints untested there.

### Leave

```gdb
(gdb) delete
(gdb) monitor resume
(gdb) detach
(gdb) quit
# then, on telnet :4444 (or the port you gave it): shutdown
```

- **`delete` every breakpoint and watchpoint before leaving.** With flash support on, a breakpoint beyond the hardware
  budget is written into flash ("Initializing flasher algorithm for cmd_flash_bp_set"); a session that dies with one set
  leaves the app patched (Kaluga S2, 2026-10-15: the third `hbreak` went to flash, openocd was killed, the next boot
  failed the app checksum and read-back differed in 2 bytes at the breakpoint; reflashed).
- **`monitor resume` before `detach`, then confirm the cores run** (telnet `targets` shows the state; the device
  answering is the run-state observation SKILL.md's Cleanup asks for). gdb `detach` left the cores halted after an
  aborted `continue`, after a failed `-batch` budget probe under traffic (P4) and after a clean temporary-breakpoint
  stop (S31, until a script resumed them, the host resetting the device meanwhile): the rule holds for every detach.
- **Leave openocd with `shutdown`, never a kill.** On RISC-V chips IDF arms debug resources through a semihosting
  `ebreak` while `esp_cpu_dbgr_is_attached()` is true (`components/esp_hw_support/cpu.c:153`, `esp_cpu_set_watchpoint`).
  After a killed openocd the next `ebreak` has no handler: "Guru Meditation Error: Core 0 panic'ed (Breakpoint)" and a
  reboot loop until an openocd `init; shutdown` session releases it or the chip is reset by EN or power, consistent with
  the debug module's active flag surviving core resets (inferred from the boot loops, not read from the hardware; TRM
  6567 defines `ASSIST_DEBUG_CORE_0_DEBUG_MODULE_ACTIVE` as a status bit and does not say). A FreeRTOS build with the
  end-of-stack watchpoint makes that call on every context switch.
- Host side: a USJ shares the hub with other DUTs on a bench; a wedge there is recovered per device (unbind, authorize,
  port power), never by a hub or controller reset that carries the other boards.

### After an abnormal end

In this order, since an esptool reset before the first step started the boot loop (P4 and S31, 2026-10-16):

1. A fresh openocd `init; shutdown` (`esp_session.py recover`), after a killed openocd and after a `shutdown` that
   logged "could not remove watchpoint" errors. `shutdown` is meant to clear the debug module (`esp_common.cfg:340-343`
   writes DM control 0 as a pre-shutdown command), yet on both the P4 and the S31 such a shutdown was followed, at the
   next esptool reset, by the Breakpoint reboot loop until `init; shutdown` ended it; the residual state was not
   established.
2. Verify the build image through a non-writing route before reprogramming: `esp_verify.py --manifest
   <build>/flasher_args.json --chip <c> --port /dev/serial/by-id/<port> --before <reset> --after <reset>` (`--help`),
   never an objcopy of the ELF. A failure before esptool's command runs (the connect, a wrong `--chip`, the stub upload)
   leaves a chip in the ROM loader, since esptool skips `--after` then (the report's `chipState`; the P4 came back on a
   reset from the USJ's lines, 2026-10-16). Without such a port, use a forced download boot where the board has one
   (the Metro S3: "ESP32-S3"), else report verification pending, or restore and verify the pinned image under
   `SKILL.md`'s Delegated sessions, Cleanup. With flash support off the known writer, the
   flash-breakpoint fallback, is absent; that does not stand in for the verification Cleanup requires.
3. Reset or reflash as the cleanup requires ("Resets").

### The end-of-stack watchpoint (RISC-V)

`CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK=y` makes FreeRTOS re-arm a stack watchpoint on every context switch
(`components/freertos/FreeRTOS-Kernel/portable/riscv/port.c:703` → `esp_cpu_set_watchpoint`); with a debugger attached
each call is a semihosting `ebreak`, one `debug_reason=00000001` halt per switch in openocd's log. Those lines at task
rate do not by themselves establish a fault; what they cost, measured 2026-10-15/16 on the P4 Function-EV v1.4 (chip
v1.0) and espressif_s31_function_core with tinyusb cdc_msc_freertos ea1d867ce (which set the option then, as tinyuf2's
`ports/espressif` defaults do, `projects/tinyuf2.md`), openocd 20260703:

- USB service degraded or stopped while the halts ran, with and without host traffic.
- One attach in three (P4) and one in two (S31) hit the `ebreak` mid-examination and panicked: the P4 reset, the S31
  parked in its panic handler with no new banner. A PMP Store access fault was also seen on the P4; its contribution to
  that reset is unresolved. An earlier stock-build attach on the P4 (2026-10-15) had been clean (`projects/tinyusb.md`).
- The watchpoint held one of the P4's three triggers (inferred from matched builds, `projects/tinyusb.md`); the S31
  inserted four either way, with concurrent allocation unchecked, so why it shows no shortfall is open.
- Through `esp_session.py` (S31, 2026-10-16) two attaches of two panicked at the examination: cpu0 halted in the
  semihosting call of `esp_cpu_set_breakpoint` (`cpu.c:91`) after the panic handler's "Setting breakpoint at <pc> and
  returning". The session ended with both cores running, and the next attach, 8 s later, found both halted: the panic
  handler's breakpoint fired after the server left. Its budget, exactly 4, was taken in that state, so it does not
  measure coexistence with the watchpoint.
- After `shutdown`, the next esptool reset started the Breakpoint boot loop ("After an abnormal end").

The same example rebuilt with the option off, same boards, tools and recipe, passed every criterion of the recipe
(`projects/tinyusb.md`, the run records): no reset at the one examination per board, one `debug_reason=00000001` line in
the whole log (the breakpoint hit), USB service kept through the attach, a 1 s halt and the hit under MSC and CDC
traffic with no kernel message, and a clean `shutdown` with the device still answering. One clean attach per board does
not bound the panic rate. The S2 and S3 runs with the option on showed neither the attach panics nor the service loss
(the Metro's `esp_session.py budget` with it on: exactly 2, three sessions with the cores running before and after,
2026-10-16): on Xtensa the watchpoint is a register write (`cpu.c:146`), which openocd's `wp` does not list since it
shows the watchpoints openocd set. To build an IDF app without it: `idf.py reconfigure` creates the project's
`sdkconfig` (IDF 6.1 `project.cmake:104`; a project may set `SDKCONFIG` elsewhere, `projects/`); set the option to `n`
there, then `idf.py build` (a project's `sdkconfig.defaults` only seeds that file).

## Resets

### Reset domains (ESP32-S3 TRM)

ESP32-S3 TRM (book 4890 Rev 1.8) §7.1 p527-528: a **core reset** resets the digital system except the RTC domain; a
**system reset** includes RTC; a **chip reset** is power-on or brown-out (p528, which lists the Super Watchdog under
both system and chip reset). The USJ-driven reset (reset cause 0x15 "USB (UART) reset"), `RTC_CNTL_SW_SYS_RST` (0x03,
what the Arduino 1200-baud touch and openocd's `reset` produced, console `rst:0x3 (RTC_SW_SYS_RST)`) and the watchdogs'
core resets are all core resets. What survives them: `RTC_CNTL_RTC_OPTION1_REG` (0x6000812C, bit 0
`FORCE_DOWNLOAD_BOOT`, R/W, p628), the download-mode strap override (p536), and pad hold (§6.9 p481). An EN pulse read
back as reset cause 1, power-on (Metro, 2026-10-16).

### Reset routes, as measured

- esptool `--after hard-reset read-mac` through the port's DTR/RTS lines: over a USJ CDC the chip takes the USJ-driven
  reset (a core reset on the S3, above), over a CP2102N bridge the lines drive EN and the boot strap as the board wires
  them ("Variants"). After a debug session it can enter the Breakpoint boot loop ("After an abnormal end"); the P4's
  outcomes: "ESP32-P4".
- After a 1200-baud touch the ROM re-entered download mode on every core reset, openocd's included, until EN was
  pressed, and esptool's "hard reset" over that USJ port left the chip in download mode with the same USB device number,
  which cannot tell a core reset from no reset (Metro S3, Arduino sketch, 2026-10-15; consistent with
  `FORCE_DOWNLOAD_BOOT` surviving core resets, above). On the S31 (tinyuf2 session, 2026-10-15) a USJ RTS reset after
  the touch left the chip in download mode too; the exit that worked was an esptool session on the ROM's OTG CDC
  (`303a:0020`) ending in a watchdog reset. Whether the Metro's esptool-over-USJ reset failure is board- or chip-level
  is unverified.

## Console

- Selection is sdkconfig: `CONFIG_ESP_CONSOLE_UART_DEFAULT` (UART0, 115200), `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG`, and
  `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG` (boot log and `ESP_LOG*` also on the USJ CDC). Name ports by
  `/dev/serial/by-id` or `by-path`, never `ttyACM<N>`/`ttyUSB<N>`, which drift within one boot. The capture recipe is
  `techniques.md` "Log capture".
- Opening the USJ CDC, a CP2102N or an ESP-Prog channel B wired to EN can reset the chip, since Linux raises DTR and RTS
  at every open: measure it (`console_capture.py --mark 'rst:0x[0-9a-f]+'`, `techniques.md` "Log capture") rather than
  assume either way, and open before the baseline (Attach, "Before attaching"). A pyserial open of the USJ CDC with
  `dtr=rts=False` set before `open()` reset the P4 and the S31 (`rst:0x17 CHIP_USB_UART_RESET`); `console_capture.py`
  opens reset neither, nor the Metro S3 through ESP-Prog channel B (2026-10-16).
- openocd on the USJ's JTAG interface and a console on its CDC coexist: every 2026-10-16 P4 and S31 attach ran with the
  console open.
- The USJ CDC is a console only while the USJ exists (on the measured S3 boards it disappears when the firmware starts
  USB, "ESP32-S3"), and when a reset makes the USJ re-enumerate, the capture misses the beginning of the boot log. A
  USJ-driven reset of the P4 (DTR low with RTS high) did not: the open capture kept the whole ROM banner (2026-10-16).

### Console perturbation

Write-path cost, from esp-idf v6.1 source (not "ms per line"):

- UART0 without the UART driver (the usual case for a bare BSP, tinyusb's included: `projects/tinyusb.md`):
  `uart_vfs.c:186-196` spins only while the 128-byte TX FIFO has fewer than two free bytes, then writes one byte: a line
  shorter than the free FIFO costs register writes only; a full FIFO costs about 11 ms per 128 bytes at 115200.
- USJ CDC without its driver: `usb_serial_jtag_vfs.c:150-171` writes while the FIFO accepts; a byte that cannot be
  written waits until 50 ms (`TX_FLUSH_TIMEOUT_US`, :78) have passed since the last accepted byte, then it and the
  following bytes are dropped until the host drains again. Each accepted byte restarts that clock, so this bounds one
  stalled byte, not a whole write, and the write also takes the VFS write lock (:194-205). Output is lost when nothing
  reads the port.
- With the drivers (`uart_vfs_dev_use_driver`, `usb_serial_jtag_vfs_use_driver`): the UART write goes through a TX ring
  only when the driver was installed with a nonzero TX buffer (`driver/uart.h:543-547`); with a zero TX buffer the call
  blocks on the driver's semaphore until the data is in the FIFO (`uart.c:1672-1683`); the USJ driver waits up to 50 ms
  once, then drops without waiting until a byte goes through again (`tx_tried_blocking`,
  `usb_serial_jtag_vfs.c:740-757`).
- Second-stage bootloader: `ESP_LOG*` → `esp_rom_vprintf`, polled (`log_print.h:30-31` under `BOOTLOADER_BUILD`).

## The USB ports per chip

| Chip      | USJ                                                                                                          | OTG                                                                       | Pad JTAG                                                          |
| --------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| ESP32-S2  | none (TRM 6572)                                                                                              | FS only, GPIO19/20 (datasheet 6570 p21, p24)                              | GPIO39-42 (TRM 6572 p182)                                         |
| ESP32-S3  | GPIO19/20, **one internal PHY shared with OTG** (TRM 4890 §33.4.1 Table 33.4-1 p1252; datasheet 6571 p24-25) | FS, same pins by default                                                  | GPIO39-42, selected by eFuses + GPIO3 (TRM 4890 Table 8.5-1 p538) |
| ESP32-P4  | GPIO24 (D-) / GPIO25 (D+), own FS PHY (datasheet 6569 p25, p28)                                              | HS on dedicated pins 49/50, own PHY (p30, p75); FS OTG on GPIO26/27 (p28) | GPIO2-5 (datasheet 6569 p20)                                      |
| ESP32-S31 | GPIO33/34, own FS PHY (datasheet 6574 p30-31)                                                                | HS on pins 44/45 (p71)                                                    | GPIO54-57 (datasheet 6574 p24)                                    |

Whether JTAG survives the firmware's USB follows from the PHY column: the S3's shared PHY takes the USJ away once OTG
starts on the measured internal-PHY boards ("ESP32-S3"); the P4 and S31 keep both ("The chips at a glance").

## Technique mapping (vs SKILL.md's ladder)

The ladder's Core column says where a technique exists; these are the substitutes on Espressif chips.

| target-debug technique     | Espressif |
| -------------------------- | --- |
| PC-sampling without a halt | none (no DWT_PCSR); repeated telnet `halt; reg pc; resume` halts the core per sample, more intrusive than a RAM ring and untested as a histogram |
| Vector catch               | none: breakpoint the panic handler; RISC-V `mcause`/`mepc`/`mtval` are the CFSR/BFAR analogs |
| SWO / DWT data trace       | none: apptrace over JTAG is the analog (needs `CONFIG_APPTRACE` + app init; untested) |

## Variants

Each section: connection and wiring with its citations; what differed on this chip at attach, with the tool version;
board-specific recovery; what is unverified. The attach behaviour common to all four and the run-by-run evidence are
under Attach and in `projects/tinyusb.md`.

### ESP32-S2

- Xtensa LX7, single core, no USJ: JTAG is the pads GPIO39-42 only. Kaluga v1.3 (`espressif_kaluga_1`): on-board
  FT2232HL with its EEPROM footprint unpopulated (v1.2 schematic book 1657 p4, U6 NC; no v1.3 schematic in the library),
  so no serial: the selector is `adapter usb location`, placed between the interface and target files as the table shows
  (`board/esp32s2-kaluga-1.cfg` sources the same two files; the selector after it is untested), and the UART is named by
  `by-path`.
- Attach (2026-10-15/16 htpc, tinyusb cdc_msc_freertos and cdc_msc_throughput ea1d867ce, openocd 20260703,
  `xtensa-esp32s2-elf-gdb`): the OTG device stayed enumerated through examination. `info threads` listed IDLE and usbd
  only, with openocd "No symbols for FreeRTOS!", both at the same `_WindowOverflow8` frame at connect; attribution was
  right at the breakpoint hit (cdc_msc_throughput). A halt inside `tud_mount_cb` wedged the host's enumeration until a
  chip reset (`projects/tinyusb.md`).

### ESP32-S3

- Xtensa LX7 dual core. One internal PHY, USJ by default, switched to the OTG controller by firmware
  (`RTC_CNTL_RTC_USB_CONF_REG` 0x60008120, `SW_HW_USB_PHY_SEL`/`SW_USB_PHY_SEL`, TRM 4890 Reg 10.56 p626-627): the USJ
  is present in ROM download mode, the second-stage bootloader and any app that never starts the OTG controller, and
  gone once the app's USB device enumerates (same hub port flips `303a:1001` → the app's VID:PID, openocd:
  `esp_usb_jtag: could not find or open device!`). On these boards live debugging of a USB app therefore uses pad JTAG;
  a board with an external PHY can keep OTG and USJ on separate connections (TRM 4890 Table 33.4-1 p1252), not measured
  here. TRM 4890 has no core debug chapter.
- Pad JTAG source (TRM 4890 Table 8.5-1 p538): default all-zero eFuses route JTAG to the USJ; `DIS_USB_JTAG=1` with
  `DIS_PAD_JTAG=0` routes it to MTDI/MTCK/MTMS/MTDO (GPIO41/39/42/40) permanently, GPIO3 ignored, USJ CDC and USJ
  download kept (p412: `DIS_USB_SERIAL_JTAG` is the separate fuse that removes the whole device); `STRAP_JTAG_SEL=1`
  lets GPIO3 at reset choose (0 pads, 1 USJ), and GPIO3 has no internal pull (datasheet 6571 p34), so a floating pin
  gives a random source. eFuses are one-time.
- Firmware can take the pads back: the scan chain reads all zeroes while an app that drives GPIO39-42 runs and is fine
  in ROM (measured 2026-10-15 Metro S3 with an Arduino sketch whose variant maps TX/RX/SCK/MOSI to IO40/41/39/42). Check
  the firmware's pin map before blaming the probe: tinyusb's host builds with the MAX3421 shield drive two of those pads
  on the devkit BSPs (`projects/tinyusb.md`), so expect the same; not measured.
- Metro ESP32-S3 rev B (`DIS_USB_JTAG` burned 2026-10-15; from its rev B schematic through `read-pcb`): Cortex 2x5
  header X2 = pad JTAG, ESP-Prog cable pin-compatible, X2 pin 10 = EN; IO39/42 shared with the ICSP header and the
  microSD socket, IO40/41 are the D1/D0 header pins; UART0 only on header JP2, pin 1 TXD0, no USB-UART bridge. Under an
  OTG app esptool has no port to this board: images go over JTAG (`program_esp`, "Verify OK" on 2026-10-16). Its UART0
  reaches ESP-Prog channel B with EN but not IO0 (`--before default-reset` boots the app: "Wrong boot mode detected
  (0x8)"), so verification goes through a forced download boot: over JTAG, halt, set `RTC_CNTL_FORCE_DOWNLOAD_BOOT`
  (`RTC_CNTL_RTC_OPTION1_REG` 0x6000812C bit 0, ESP32-S3 TRM book 4890 p628, the 0x6000_8000 base p408; honoured on a
  CPU reset unless an eFuse disables it, p536) and `reset run`; then `esp_verify.py --before no-reset --after
  hard-reset` on channel B matched every image (esptool 5.5.0, 2026-10-16; 5.2.0's stub upload failed there with
  "Serial data stream stopped"). Its hard reset clears the bit but pulses RTS with DTR still asserted from the open, so
  EN stays high and the chip stays in the loader: pulse EN afterwards (RTS asserted, DTR deasserted; a chip reset
  clears the bit, p526).
- ESP-Prog on the Metro: `board/esp32s3-ftdi.cfg`, `adapter serial <FTDI EEPROM serial>`, JTAG PWR SEL and PROG PWR SEL
  on 3.3 V. Its JTAG header carries no reset (pin 10 NC, so X2 pin 10 EN stays unconnected; guide book 6576 p10), and
  `ESP_EN` on PROG pin 1 also gates the probe's own JTAG buffers through an inverter, with its pull-up R7 drawn NC
  (guide p14: R7 in Fig. 7, the inverter and buffers in Fig. 8): wire PROG pin 1 to the board's EN. On that FT2232
  channel B UART (`if01`) RTS asserted with DTR deasserted pulls EN low (measured; matches the guide's Fig. 7 table,
  p14); a pyserial open reset the chip once even with `dtr=rts=False` set before and after the open ("Console" for the
  opens that did not). PROG pin 5 `ESP_RXD` listens to the target's TXD (measured 2026-10-16: pin 3 captured nothing).
- Metro attach (cdc_msc_freertos and cdc_msc_throughput on OTG, 2026-10-16): the recipe passed under traffic with no DUT
  USB reset, disconnect or error logged (`projects/tinyusb.md`). The exception: after the `program_esp` session that
  reprogrammed the app (ended with `reset exit`), every attach read the scan chain as all ones, through a USB
  re-authorize of the probe, until EN was wired and pulsed (no attach between the wiring and the pulse), after which
  both cores examined with the same app running; an earlier `reset exit` with matching flash content had left the chain
  readable. Neither the trigger nor the recovery is isolated, and a floating `ESP_EN` before the wire is an untested
  cause.
- DevKitC-1 (`espressif_s3_devkitc` = tinyuf2 `espressif_esp32s3_devkitc_1`, one board under two names in two projects'
  rosters: hold it under both, `projects/tinyusb.md`): flashing and UART0 console over the CP2102N, EN and IO0 through
  the cross-coupled DTR/RTS transistor pair (schematic book 1853 p2). With sector 0 erased the ROM loops "invalid
  header: 0xffffffff" on UART0 and the USJ enumerates (2026-10-13). DevKitM (`espressif_s3_devkitm`, tinyusb rig): what
  its USJ window allowed, and how long it stayed up against a DevKitC observation, is in `projects/tinyusb.md`.

### ESP32-P4

- RISC-V dual HP core + LP core (datasheet 6569 p1). USJ on its own FS PHY (GPIO24/25) and the HS OTG on dedicated pins:
  `303a:1001` and the firmware's device enumerate together, HS at 480 Mb/s. `EFUSE_USB_PHY_SEL` swaps the two FS PHYs;
  `USB_SERIAL_JTAG_EXCHG_PINS` swaps D+/D- (datasheet 6569 p28).
- Attach (`board/esp32p4-builtin.cfg`, `riscv32-esp-elf-gdb`; Function-EV v1.4, chip v1.0, `espressif_p4_function_ev`,
  tinyusb ea1d867ce, openocd 20260703): no reset on the 2026-10-15 stock cdc_msc_freertos attach nor on any 2026-10-16
  attach of cdc_msc_throughput or of cdc_msc_freertos rebuilt without the end-of-stack watchpoint (the stock build on
  2026-10-16: "The end-of-stack watchpoint"; run records in `projects/tinyusb.md`). The gdb connect resets through the
  memory-protection path unless `ESP_FLASH_SIZE 0` precedes the board file, and that reset knocked the HS OTG device off
  the bus (descriptor reads -110, then silence) until a reset over the USJ brought it back. The esptool reset over the
  USJ returned to the app unless another process held the port (2026-10-15).
- openocd 20251215 (ci.lan, chip v0.1, 2026-09-16): the same connect reset; `info threads` right after connect printed
  one bare "Remote target" or "Too large number of threads", the tasks appeared after continuing into app code and
  halting, and thread attribution at a breakpoint was wrong on some hits; a second breakpoint armed after the first hit
  asserted at `riscv.c:2150`, preceded by "can't add write watchpoint ... resource not available", cause not
  established, so budget one breakpoint per session on that version. `set ESP_ONLYCPU 1` degrades to cpu0-only debugging
  when cpu1's examination fails (`OCD_ID = 00000000` in the log; carried over), and a breakpoint in a task scheduled on
  the other core then never hits.
- Board: Function-EV v1.4 has no USJ connector; GPIO24 (D-) and GPIO25 (D+) are header J1 pins 27/28 behind 0 Ω
  resistors (schematic book 2064 p2-3), and swapped D+/D- wiring enumerates as "new low-speed USB device" with error -71
  where the right wiring shows "new full-speed" (rig, 2026-09); HS OTG on the J15 Type-C and the J18 Type-A in parallel
  (one at a time); CP2102N on J20 driving EN and GPIO35 boot mode through the cross-coupled DTR/RTS transistor pair
  (schematic book 2064 p6). Build for the detected chip revision and investigate an image-compatibility rejection before
  reaching for esptool's `--force` (the revisions on the benches: `projects/tinyusb.md`).
- ROM USB download boot exists over the USJ and over the HS OTG controller, which flashes in full-speed mode; the FS OTG
  controller does not support it (datasheet 6569 p37). The OTG download device enumerated full-speed on the rig
  (2026-09, PID not in the datasheet).

### ESP32-S31

- RISC-V dual HP core + LP core (datasheet 6574 p1), USJ on its own FS PHY (GPIO33/34), HS OTG on pins 44/45: both
  enumerate together. `board/esp32s31-builtin.cfg` (Tcl targets `esp32s31.hp.cpu0` and `.cpu1`), `riscv32-esp-elf-gdb`.
  Flashing and console went over the USJ CDC (bootloader 0x2000); the measured board's CP2102N bridge (user guide book
  4899 p9) had an unusable receive path (2026-10-14). A 1200-baud touch left it in download mode across a USJ reset
  ("Resets").
- Attach (`espressif_s31_function_core`, openocd 20260703, 2026-10-16): no reset at examination or gdb connect on
  cdc_msc_throughput and on cdc_msc_freertos rebuilt with the end-of-stack watchpoint off; under HS MSC and CDC traffic
  the halt and one hit cost latency only, and on cdc_msc_throughput the 1 s halt found both cores in
  `esp_cpu_wait_for_intr` (task counts and the stock build's attaches: `projects/tinyusb.md`; the stock build otherwise
  behaved as "The end-of-stack watchpoint" describes). An earlier telnet-only session with `set ESP_RTOS none` (halt,
  `mdw`, `dump_image`, resume) halted both HP cores and the device kept answering, with no console capture to anchor it
  (2026-10-14).
- Chip documents: TRM book 6558 Rev 0.1 has gaps and no OTG chapter; errata 4887 lists only SPI-855 and ECDSA_DS-836.
  Its DWC2 identity as read live: `projects/tinyusb.md`.
