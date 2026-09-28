# TinyUSB (`hathach/tinyusb`) — debugging notes

What is specific to this project when using `target-debug`, `rtt`, `etm-trace`,
`esp-target-debug`, `usb-sniffer` or `usb-kernel-debug` on it: names to inspect,
what a symptom usually means, which example proves what. For building a debug
variant, taking a rig board or handling a failed `usbtest` case, follow the
project's own `CLAUDE.md` (`Build contract:`, `HIL contract:`,
`usbtest battery:`), not this file.

Every entry says how it is known: `verified <date> <revision> <board>: <how>`,
or `carried over` for knowledge moved here with the skills on 2026-09-17 and not
re-checked since. **Before relying on a symbol, check it exists in the checkout
in front of you** (`grep -rw <name> src hw/bsp`, or `nm` the flashed ELF): names
here are as of the revision given, and one entry below was already stale when it
arrived. Fix or date an entry when you find it wrong; a renamed symbol does not
undo the observation behind it.

For firmware built away from the probe host, take the rig's exact manual flash
recipe from `test/hil/hil_flash.py`: `flash_openocd`, `flash_jlink` or
`flash_esptool`; do not reconstruct the backend command elsewhere. verified
2026-09-18 e595e7950 raspberry_pi_pico, frdm_k64f,
espressif_p4_function_ev: all three probe-side flashes followed those functions.

## Inspecting state in GDB

- Device stack, per endpoint: `p/x _usbd_dev.ep_status` — `[epnum][dir]`, dir 1 =
  IN, one byte of flags: `0x01` busy, `0x02` stalled, `0x04` claimed
  (`TU_EDPT_STATE_*`, `src/common/tusb_private.h`). Poke to test a hypothesis, after
  dumping: `set var _usbd_dev.ep_status[2][1] &= ~1`.
  verified 2026-09-18 fad6bd546 raspberry_pi_pico (cdc_msc, openocd + gdb): idle
  device reads `{0,0},{0,0},{0x5,0},{0x1,0},…`. The bitfield form
  `ep_status[2][1].busy` the skill used to give stopped existing on 2026-05-04
  ("not a structure").
- Device stack, whole device: `p _usbd_dev.cfg_num` (0 = not configured),
  `.connected`, `.addressed`, `.suspended`, `.speed`.
  verified 2026-09-18 fad6bd546 raspberry_pi_pico: `cfg_num = 1` once enumerated.
- CDC: `p/x _cdcd_itf[0].line_state` — bit 0 DTR, bit 1 RTS; `tud_cdc_connected()`
  is bit 0. verified 2026-09-18 fad6bd546 raspberry_pi_pico: 0 with no port open.
- Host stack per device: `p _usbh_devices[0]` (address, enumeration and
  configuration state). Symbol checked in fad6bd546 (`src/host/usbh.c`); carried
  over otherwise.
- Port driver bookkeeping is per port — read the board's `dcd_*.c`/`hcd_*.c`
  first. dwc2 device: `xfer_status[epnum][dir]` (`.total_len`, `.max_size`), so
  `watch -l xfer_status[2][1].total_len`. Symbol checked in fad6bd546
  (`src/portable/synopsys/dwc2/dcd_dwc2.c`); other ports name theirs differently.
- The USB interrupt entry points are `dcd_int_handler` / `hcd_int_handler`
  (`src/tusb.c` dispatches); the vector itself is per port, e.g.
  `dcd_rp2040_irq`. Symbols checked in fad6bd546. Halting there stops USB service.
- `MinSizeRel` builds keep DWARF, so `p`/struct access works on rig firmware.
  carried over.
- A tick counter to watch without halting (DWT data trace): `system_ticks`, read by
  `tusb_time_millis_api`. carried over (F407 over J-Link, H743 over ST-Link);
  symbols checked in fad6bd546.

## Host-side symptom → what to check on the target

usbmon is URB-level: it cannot show data toggles or NAKs, so a device-side stall
and a toggle desync look identical (Submits on an endpoint with no Completes).
The host capture locates the failing request; the target names the cause. These
are hypotheses with the check that discriminates, not verdicts. carried over;
callback names checked in fad6bd546.

| Host-side symptom | Target-side check |
|---|---|
| Not recognized / re-enumerates | Is `GET DESCRIPTOR (DEVICE)` answered, `bMaxPacketSize0` sane? Repeated SET_ADDRESS / resets = device too slow to respond; usbcore/xhci dynamic debug gives the host's reset reason. |
| Enumeration stalls | The request after the last good control transfer (often CONFIG, a string, or the first class request) is what `tud_descriptor_*` / the control callback mishandled. |
| Control URB completes `-32` (`-EPIPE`) | A real STALL: usually a `tud_*_control_xfer_cb` returning `false` or an unhandled `bRequest` (decode with `tshark -V`). `-71` (`-EPROTO`) is not a STALL: the device mis-/under-served the transfer, e.g. EP0 starved under bulk load. |
| Bulk / interrupt missing or short | Unexpected short (`usb.data_len < wMaxPacketSize`) suggests a FIFO/length bug; no completions: the class never writing, a halted endpoint, or a toggle desync — read the EP control register (response/toggle bits), the port's transfer bookkeeping and `_usbd_dev.ep_status`. |
| CDC read (`dd`/cat) hangs, IN endpoint idle | EP0 first: did `SET_CONTROL_LINE_STATE` (`bmRequestType==0x21`) complete `0` or fail `-71`? If it failed the device never saw DTR, `tud_cdc_connected()` is false and the app stops sourcing TX; typical right after a bulk **write** phase. Confirm with `_cdcd_itf[0].line_state` bit 0. |
| ISO / audio dropouts | Zero-length ISO frames = the device starved the endpoint; check the cadence the class feeds. |
| Wrong descriptors | `bLength` / `wTotalLength` against `tud_descriptor_configuration_cb`. |

## Logs and RTT

- The moderate log level is 2; 3 adds per-transfer lines and much more timing
  skew. With RTT logging the BSP routes `sys_read` too, so console input works.
  carried over; RTT logging builds verified 2026-09-18 fad6bd546 on
  raspberry_pi_pico (openocd route) and pico2_etm_trace (J-Link route): boot log
  from `USBD init on controller 0`.
- Up-buffer 0 is the "Terminal" console; SystemView builds add up-buffer 1
  "SysView". carried over. Stock `cdc_msc` has 3 up- and 3 down-buffers, up-buffer
  0 of 1024 B, down-buffer 0 of 16 B. verified 2026-09-18 fad6bd546
  raspberry_pi_pico: control block read over SWD.
- **CI-flashed rig firmware has logging off**: a capture shows the J-Link banner
  and nothing else. Build a logging variant first. verified 2026-09-16 ea4088
  (ci.lan).
- Keep unattended console builds at level 2, and size reset-then-attach captures
  for the 2 s undrained settle: even level 2 overflowed the stock 1 KB up-buffer
  before SET_CONFIGURATION; `BUFFER_SIZE_UP=8192` preserved the complete boot and
  enumeration. verified 2026-09-18 e595e7950 raspberry_pi_pico: 1 KB capture
  ended during the configuration descriptor, while the 8 KB run reached
  SET_CONFIGURATION, CDC open and MSC open.
- **Which example proves console INPUT**: `board_test` polls `board_getchar()`
  (RTT-aware) but echoes through `board_putchar` → `board_uart_write`, which is
  not logger-aware: on a UART-less board the echo vanishes (measured on ea4088).
  Drive a host example's menu instead (`msc_file_explorer`, `cdc_msc_hid` reply
  via printf), or patch the echo to `printf` locally. `cdc_msc` never polls the
  console: keystrokes sent to it prove nothing — read the down-buffer's `WrOff`
  over the probe instead (went 5 → 8 for 3 bytes, verified 2026-09-18 fad6bd546
  raspberry_pi_pico). carried over otherwise; `board_getchar` checked in fad6bd546.
- The HIL harness opens the same `JlinkRtt` class as a board's console (`"logger":
  "rtt"` in the HIL config). carried over.
- WCH QingKe RISC-V needs its own `SEGGER_RTT_LOCK/UNLOCK`: the vendored generic
  lock uses `mstatus` CSRs that trap (mcause=2), so a `LOGGER=rtt` build traps.
  carried over. History, as of 2026-09-17: a working port existed only on the
  unmerged branch `claude/add-systemview-debug` — `hw/bsp/ch583/sysview_rtt_lock_wch.h`
  (brace-scoped save/restore of CSR 0x800) and a shared
  `hw/bsp/sysview_rtt_conf_wch.h` that the ch32v20x/ch32v30x `family.cmake`
  force-include to win the include-guard race against the vendored conf. Not on
  master; check whether it has landed before citing either path.
- Several J-Links on one bench: an unpinned `<example>-jlink` flash takes
  whichever enumerates first; the build contract says how to pin it.

## PC sampling and profiles

- On rp2040/rp2350 the stock examples spend almost all their time in
  `get_bootsel_button` (the BOOTSEL button read runs from RAM with flash access
  off, and `board_button_read` is called every main-loop pass): a histogram
  dominated by it is the idle loop, not a spin. verified 2026-09-18 fad6bd546
  pico2_etm_trace: 98.7 % of 300 PC samples, 97.8 % of traced instructions.
- rp2350 code running from flash (XIP) pays roughly 330 core cycles per taken
  branch in a tight loop: a 1000-iteration empty `volatile` loop takes 2.2 ms at
  150 MHz. Size calibration loops by measurement. verified 2026-09-18 fad6bd546
  pico2_etm_trace: DWT CYCCNT 329,423 cycles, `time_us_32` 2198 µs.

## RP2040 flash verification

- Before any RP2040 flash read or `verify_image`, stop at a hardware
  breakpoint in flash-resident code of the matching ELF and confirm the PC is
  there; a stop in the RAM-resident `get_bootsel_button` does not qualify. In
  `get_bootsel_button` flash access is off and every read returns `0x00`;
  `verify_image` reports false mismatches at both `0x10000000` and
  `0x13000000`. `scripts/rp2040_verify.py` (beside `target-debug`'s SKILL.md)
  does it on OpenOCD: breakpoint, PC check, verification through the uncached
  XIP alias, then it removes its breakpoint, resumes and reads DHCSR; `--help`
  gives its outcomes and exit codes:

  ```bash
  python3 <skill dir>/scripts/rp2040_verify.py --probe <serial> --interface-cfg interface/cmsis-dap.cfg \
    --speed 5000 --elf <flashed.elf> --symbol tud_task_ext
  ```

  verified 2026-09-18 81a25eb5d raspberry_pi_pico (htpc, CMSIS-DAP): the
  matching ELF verified 31,136 bytes, exit 0; `--symbol board_init` (never
  reached again) gave `not-at-breakpoint pc=0x20000230`, exit 1; the pre-fix
  ELF gave `mismatch` with diffs from `0x13004112`, exit 1. DHCSR read
  `0x01000001` each time, the CDC echo still worked and dmesg logged no new
  enumeration. An earlier run of the script's first version did coincide with
  one disconnect and re-enumeration (14:46:42), and the next session logged
  "external reset detected"; cause not established, so the script now fails
  on a detected reset. OpenOCD's default SMP pair failed `resume` with "core1
  not halted" and left core 0 halted; the script sets `USE_CORE 0`.
- A single opcode read (`mdh <addr> 1`) at an address where two builds differ
  supports a narrow identity claim between them, not a verified image.
- Observed 2026-09-18 bdb6b90fc raspberry_pi_pico (htpc, CMSIS-DAP): a bare
  `halt` that stopped in `get_bootsel_button`, then `verify_image` (all
  zeroes), then `reset` left the board failing enumeration ("device not
  accepting address") until a `program` reflash recovered it. Mechanism not
  established.

## ETM trace

- The trace init is `trace_etm_init()` in the family BSP, compiled in by the ETM
  build variant; trace starts there, not at reset. symbol checked in fad6bd546
  (seven families).
- Reference Ozone projects live beside the board:
  `hw/bsp/<family>/boards/<board>/ozone/*.jdebug`; the project's board-info tool
  prints the one for a board. 15 of 15 resolve through `etm_capture.py --jdebug`.
  verified 2026-09-17 fad6bd546 (resolution only; boards.md has the captures).
- The bare-metal examples have no SysTick handler on rp2040/rp2350, so there is
  no ready calibration beat for `etm_profile.py --isr`: `dcd_rp2040_irq` is the
  USB ISR, `alarm_pool_irq_handler` the timer one, neither at a fixed rate. A
  known-rate beat has to be added for the measurement (a repeating hardware
  timer in the BSP, reverted afterwards). verified 2026-09-18 fad6bd546
  pico2_etm_trace: 154–156 µs by ETM against 157.3 µs by DWT CYCCNT.
- `etm_profile.py --isr` entries, per board, from the validated captures in
  etm-trace's boards.md (carried over): mcb1800 `USB0_IRQHandler,dcd_int_handler`;
  ra6m5_ek `tusb_int_handler,dcd_int_handler` (FSP's `usbfs_interrupt_handler`
  symbol never executes); ra8m1_ek `tusb_int_handler`.
- pico2_etm_trace sets `TRACE_ETM` in its own `board.cmake`: no build flag. Pitfalls
  at other core rates: a bare `-DSYS_CLK_KHZ=` only sets a CMake cache var and is
  silently ignored (the BSP carries no PLL table), and nothing raises the core
  regulator for 240 MHz by itself. The definitions to pass: the build contract.
  carried over (measurements: etm-trace's boards.md). Its reference project:
  `hw/bsp/rp2040/boards/pico2_etm_trace/ozone/rp2350.jdebug`, checked in fad6bd546.
- Halting pico2_etm_trace while its own USB port is on the capture host stalls
  that host's enumeration for ~30 s (`device not accepting address`, error -62);
  JLinkExe blocks at "Connecting to J-Link" meanwhile. verified 2026-09-18.

## Which technique cracked what

carried over, as pointers to the technique rather than to the fix:

- PC-sampling found the rusb2 FRDY wedge: the histogram's top entry was the spin.
- The RAM ring-buffer cracked the musb babble: ISR ordering no print could show.
- The ch32v307 Heisenbug changed behaviour under logging *and* under the
  debugger: the case for moving down in intrusiveness.

## Espressif boards

- Resolve the board's family from `hw/bsp/*/boards/<board>` before selecting the
  debug backend; `ls -d hw/bsp/*/boards/<board>` must identify one board
  directory. For an Espressif board, read `esp-target-debug` first; `target-debug`
  still supplies the methodology. carried over from TinyUSB's `target-debugger`
  agent.
- The HIL config's `esptool` uids are the CP2102N **flasher** serials (the UART
  bridge), never the USB-Serial-JTAG device: do not pass them to openocd, whose
  `adapter serial` is the chip MAC with colons. carried over.
- Break in app code for symbolized frames: `tbreak tud_task_ext`; a good backtrace
  reads `tud_task_ext` ← `usb_device_task` ← `vPortTaskWrapper`. verified
  2026-09-16 espressif_p4_function_ev (ci.lan); symbols checked in fad6bd546.
- espressif_s3_devkitm shares ONE PHY between USB-Serial-JTAG and OTG: the hub
  port flips 303a:1001 → cafe:4008 as a USB example boots and JTAG is gone.
  Debug windows there: non-USB firmware (`board_test`; `usb_new_phy` is absent
  from the ELF when `CFG_TUD/TUH_ENABLED` are 0), the bootloader, or external
  JTAG. espressif_p4_function_ev has separate pins and keeps both. carried over.
- The ELF comes from the example's own `idf.py` build directory. carried over.

## On the wire and in usbmon

- Example devices enumerate as `cafe:40xx`; the low PID bits encode the classes,
  so `cafe:` on a rig matches several boards — select by bus or by full PID.
  verified 2026-09-18: two `cafe:4007` devices on one bus, `usbcap.py cafe:4007`
  accepted; a selector whose matches span buses is refused with the list.
- The address on the wire is not the kernel's device number: an xHCI host
  assigns its own (wire address 9 while `lsusb` said Device 078). Take the DUT's
  address from `sniff.py addr`, never from `lsusb`. verified 2026-09-18
  raspberry_pi_pico on the sniffer tap.
- Halting or resetting a dwc2 device through the probe does NOT disconnect it: the
  soft-connect pull-up stays up through halt and reset, so the host's stuck URBs
  stay stuck. Recover the Linux host side (the rig's `usb-kernel-recover`
  skill). carried over.
