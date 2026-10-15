# TinyUSB (`hathach/tinyusb`) — debugging notes

What is specific to this project when using `target-debug` (its `espressif.md`
included), `rtt`, `etm-trace`, `usb-sniffer` or `usb-kernel-debug` on it: names to inspect,
what a symptom usually means, which example proves what. For building a debug
variant, taking a rig board or handling a failed `usbtest` case, follow the
project's own `CLAUDE.md` (`Build contract:`, `HIL contract:`,
`usbtest battery:`), not this file; chip and probe behaviour is in
`families.md` and the technique files beside SKILL.md.

Entries carry their evidence as SKILL.md's "Project notes" says; `carried over`
marks knowledge moved here with the skills on 2026-09-17 and not re-checked
since. **Before relying on a symbol, check it exists in the checkout in front
of you** (`grep -rw <name> src hw/bsp`, or `nm` the flashed ELF): one entry
below was already stale when it arrived.

For firmware built away from the probe host, take the rig's exact manual flash
recipe from `test/hil/hil_flash.py`: `flash_openocd`, `flash_jlink` or
`flash_esptool`; do not reconstruct the backend command elsewhere. verified
2026-09-18 e595e7950 raspberry_pi_pico, frdm_k64f,
espressif_p4_function_ev: all three probe-side flashes followed those functions.

## Stack state and entry points

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

## Symptoms and target checks

Reading the host side (usbmon's limits, URB status codes) is `usb-kernel-debug`'s;
these are the tinyusb names its symptoms point to, hypotheses with the check
that discriminates, not verdicts. carried over; callback names and the
`usbd.c` EP0 stall paths checked in fad6bd546.

| Host-side symptom | Target-side check |
|---|---|
| Enumeration stalls | The request after the last good control transfer (often CONFIG, a string, or the first class request) is what `tud_descriptor_*` / the control callback mishandled. |
| Control STALL (`-32`) | A `tud_*_control_xfer_cb` returning `false`, or `usbd.c` rejecting the SETUP first: an unsupported standard request or recipient, an interface or endpoint no driver owns, a `tud_descriptor_*_cb` returning NULL. Follow the full SETUP through `process_setup_received` to where it returns `false`. |
| Bulk / interrupt: no completions | `_usbd_dev.ep_status` and the port's transfer bookkeeping (dwc2: `xfer_status`). |
| CDC read (`dd`/cat) hangs, IN endpoint idle | With DTR never seen, `tud_cdc_connected()` is false and the app stops sourcing TX; typical right after a bulk **write** phase. Confirm with `_cdcd_itf[0].line_state` bit 0. |
| Wrong descriptors | `bLength` / `wTotalLength` against `tud_descriptor_configuration_cb`. |

- Example devices enumerate as `cafe:40xx`; the low PID bits encode the classes,
  so `cafe:` on a rig matches several boards — select by bus or by full PID.
  verified 2026-09-18: two `cafe:4007` devices on one bus, `usbcap.py cafe:4007`
  accepted.
- Past cases, as pointers to the technique rather than to the fix (carried
  over): PC-sampling found the rusb2 FRDY wedge (the histogram's top entry was
  the spin); the RAM ring-buffer cracked the musb babble (ISR ordering no print
  could show); the ch32v307 Heisenbug changed behaviour under logging *and*
  under the debugger, the case for moving down in intrusiveness.

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
- **CI-flashed rig firmware had logging off**: a capture showed the J-Link
  banner and nothing else; build a logging variant first. observed 2026-09-16
  ea4088 (ci.lan).
- Keep unattended console builds at level 2. In a reset-then-attach capture
  with a 2 s undrained settle even level 2 overflowed the stock 1 KB up-buffer before SET_CONFIGURATION; `BUFFER_SIZE_UP=8192` preserved the complete boot and
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
- A `LOGGER=rtt` build traps on WCH QingKe (ch32v): the vendored generic lock
  does (`rtt`, "Buffer modes and locking"), so tinyusb needs its own
  `SEGGER_RTT_LOCK/UNLOCK` there. carried over. History, as of 2026-09-17: a
  working port existed only on the unmerged branch
  `claude/add-systemview-debug` — `hw/bsp/ch583/sysview_rtt_lock_wch.h`
  (brace-scoped save/restore of CSR 0x800) and a shared
  `hw/bsp/sysview_rtt_conf_wch.h` that the ch32v20x/ch32v30x `family.cmake`
  force-include to win the include-guard race against the vendored conf. Not on
  master; check whether it has landed before citing either path.

## Profiling and trace

- A tick counter to watch without halting (DWT data trace): `system_ticks`, read by
  `tusb_time_millis_api`. carried over (F407 over J-Link, H743 over ST-Link);
  symbols checked in fad6bd546.
- On rp2040/rp2350 the stock examples spend almost all their time in
  `get_bootsel_button` (the BOOTSEL button read runs from RAM with flash access
  off, and `board_button_read` is called every main-loop pass): a histogram
  dominated by it is the idle loop, not a spin. On RP2040 `get_bootsel_button`
  is also no place to stop for a flash read (`families.md`, "RP2040 flash
  verification": use `--symbol tud_task_ext`). verified 2026-09-18 fad6bd546 pico2_etm_trace:
  98.7 % of 300 PC samples, 97.8 % of traced instructions.
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
- pico2_etm_trace sets `TRACE_ETM` in its own `board.cmake`: no build flag. A
  bare `-DSYS_CLK_KHZ=` only sets a CMake cache var and is silently ignored;
  the definitions that take effect are the build contract's. carried over. Its reference project:
  `hw/bsp/rp2040/boards/pico2_etm_trace/ozone/rp2350.jdebug`, checked in fad6bd546.

## Espressif integration

- Resolve the board's family from `hw/bsp/*/boards/<board>` before selecting the
  debug backend; `ls -d hw/bsp/*/boards/<board>` must identify one board
  directory. For an Espressif board, read `espressif.md` first; SKILL.md
  still supplies the methodology. carried over from TinyUSB's `target-debugger`
  agent.
- The HIL config's `esptool` uid is whatever port the rig flashes through:
  the CP2102N **flasher** serial on the S3 and P4 rig boards, never the
  USB-Serial-JTAG device there; openocd's `adapter serial` is the chip MAC
  with colons, read from the device. carried over (S3/P4 rig boards).
- Break in app code for symbolized frames: `tbreak tud_task_ext`; a good backtrace
  reads `tud_task_ext` ← `tud_task` ← `usb_device_task` ← `vPortTaskWrapper`. verified
  2026-09-16 espressif_p4_function_ev (ci.lan) and 2026-10-15
  espressif_p4_function_ev v1.0 (htpc, ea1d867ce); symbols checked in fad6bd546.
- On espressif_s3_devkitm, tinyusb's non-USB debug window
  (`espressif.md`, "ESP32-S3") is `board_test`: `usb_new_phy` is
  absent from its ELF when `CFG_TUD/TUH_ENABLED` are 0. carried over.
