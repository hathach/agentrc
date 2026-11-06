# TinyUSB (`hathach/tinyusb`) — debugging notes

What is specific to this project when using `target-debug` (its `espressif.md` included), `rtt`,
`usb-sniffer` or `usb-kernel-debug` on it: names to inspect, what a symptom usually means, which example proves what.
For building a debug variant, taking a rig board or handling a failed `usbtest` case, follow the project's own
`CLAUDE.md` (`Build contract:`, `HIL contract:`, `usbtest battery:`), not this file; chip and probe behaviour is in
`families.md` and the technique files beside SKILL.md.

Entries carry their evidence as SKILL.md's "Project notes" says; `carried over` marks knowledge moved here with the
skills on 2026-09-17 and not re-checked since. **Before relying on a symbol, check it exists in the checkout in front of
you** (`grep -rw <name> src hw/bsp`, or `nm` the flashed ELF): one entry below was already stale when it arrived.

For firmware built away from the probe host, take the rig's exact manual flash recipe from `test/hil/hil_flash.py`:
`flash_openocd`, `flash_jlink` or `flash_esptool`; do not reconstruct the backend command elsewhere. verified 2026-09-18
e595e7950 raspberry_pi_pico, frdm_k64f, espressif_p4_function_ev: all three probe-side flashes followed those functions.

## Stack state and entry points

- Device stack, per endpoint: `p/x _usbd_dev.ep_status` — `[epnum][dir]`, dir 1 = IN, one byte of flags: `0x01` busy,
  `0x02` stalled, `0x04` claimed (`TU_EDPT_STATE_*`, `src/common/tusb_private.h`). Poke to test a hypothesis, after
  dumping: `set var _usbd_dev.ep_status[2][1] &= ~1`. verified 2026-09-18 fad6bd546 raspberry_pi_pico (cdc_msc,
  openocd + gdb): idle device reads `{0,0},{0,0},{0x5,0},{0x1,0},…`. The bitfield form `ep_status[2][1].busy` the skill
  used to give stopped existing on 2026-05-04 ("not a structure").
- Device stack, whole device: `p _usbd_dev.cfg_num` (0 = not configured), `.connected`, `.addressed`, `.suspended`,
  `.speed`. verified 2026-09-18 fad6bd546 raspberry_pi_pico: `cfg_num = 1` once enumerated.
- CDC: `p/x _cdcd_itf[0].line_state` — bit 0 DTR, bit 1 RTS; `tud_cdc_connected()` is bit 0. verified 2026-09-18
  fad6bd546 raspberry_pi_pico: 0 with no port open.
- Host stack per device: `p _usbh_devices[0]` (address, enumeration and configuration state). Symbol checked in
  fad6bd546 (`src/host/usbh.c`); carried over otherwise.
- Port driver bookkeeping is per port — read the board's `dcd_*.c`/`hcd_*.c` first. dwc2 device:
  `xfer_status[epnum][dir]` (`.total_len`, `.max_size`), so `watch -l xfer_status[2][1].total_len`. Symbol checked in
  fad6bd546 (`src/portable/synopsys/dwc2/dcd_dwc2.c`); other ports name theirs differently.
- The USB interrupt entry points are `dcd_int_handler` / `hcd_int_handler` (`src/tusb.c` dispatches); the vector itself
  is per port, e.g. `dcd_rp2040_irq`. Symbols checked in fad6bd546. Halting there stops USB service.
- `MinSizeRel` builds keep DWARF, so `p`/struct access works on rig firmware. carried over.

## Symptoms and target checks

Reading the host side (usbmon's limits, URB status codes) is `usb-kernel-debug`'s; these are the tinyusb names its
symptoms point to, hypotheses with the check that discriminates, not verdicts. carried over; callback names and the
`usbd.c` EP0 stall paths checked in fad6bd546.

| Host-side symptom                           | Target-side check |
| ------------------------------------------- | --- |
| Enumeration stalls                          | The request after the last good control transfer (often CONFIG, a string, or the first class request) is what `tud_descriptor_*` / the control callback mishandled. |
| Control STALL (`-32`)                       | A `tud_*_control_xfer_cb` returning `false`, or `usbd.c` rejecting the SETUP first: an unsupported standard request or recipient, an interface or endpoint no driver owns, a `tud_descriptor_*_cb` returning NULL. Follow the full SETUP through `process_setup_received` to where it returns `false`. |
| Bulk / interrupt: no completions            | `_usbd_dev.ep_status` and the port's transfer bookkeeping (dwc2: `xfer_status`). |
| CDC read (`dd`/cat) hangs, IN endpoint idle | With DTR never seen, `tud_cdc_connected()` is false and the app stops sourcing TX; typical right after a bulk **write** phase. Confirm with `_cdcd_itf[0].line_state` bit 0. |
| Wrong descriptors                           | `bLength` / `wTotalLength` against `tud_descriptor_configuration_cb`. |

- Example devices enumerate as `cafe:40xx`; the low PID bits encode the classes, so `cafe:` on a rig matches several
  boards — select by bus or by full PID. verified 2026-09-18: two `cafe:4007` devices on one bus, `usbcap.py cafe:4007`
  accepted.
- Past cases, as pointers to the technique rather than to the fix (carried over): PC-sampling found the rusb2 FRDY wedge
  (the histogram's top entry was the spin); the RAM ring-buffer cracked the musb babble (ISR ordering no print could
  show); the ch32v307 Heisenbug changed behaviour under logging *and* under the debugger, the case for moving down in
  intrusiveness.

## Logs and RTT

- The moderate log level is 2; 3 adds per-transfer lines and much more timing skew. With RTT logging the BSP routes
  `sys_read` too, so console input works. carried over; RTT logging builds verified 2026-09-18 fad6bd546 on
  raspberry_pi_pico (openocd route) and pico2_etm_trace (J-Link route): boot log from `USBD init on controller 0`.
- Up-buffer 0 is the "Terminal" console; SystemView builds add up-buffer 1 "SysView". carried over. Stock `cdc_msc` has
  3 up- and 3 down-buffers, up-buffer 0 of 1024 B, down-buffer 0 of 16 B. verified 2026-09-18 fad6bd546
  raspberry_pi_pico: control block read over SWD.
- **CI-flashed rig firmware had logging off**: a capture showed the J-Link banner and nothing else; build a logging
  variant first. observed 2026-09-16 ea4088 (ci.lan).
- Keep unattended console builds at level 2. In a reset-then-attach capture with a 2 s undrained settle even level 2
  overflowed the stock 1 KB up-buffer before SET_CONFIGURATION; `BUFFER_SIZE_UP=8192` preserved the complete boot and
  enumeration. verified 2026-09-18 e595e7950 raspberry_pi_pico: 1 KB capture ended during the configuration descriptor,
  while the 8 KB run reached SET_CONFIGURATION, CDC open and MSC open.
- **Which example proves console INPUT**: `board_test` polls `board_getchar()` (RTT-aware) but echoes through
  `board_putchar` → `board_uart_write`, which is not logger-aware: on a UART-less board the echo vanishes (measured on
  ea4088). Drive a host example's menu instead (`msc_file_explorer`, `cdc_msc_hid` reply via printf), or patch the echo
  to `printf` locally. `cdc_msc` never polls the console: keystrokes sent to it prove nothing — read the down-buffer's
  `WrOff` over the probe instead (went 5 → 8 for 3 bytes, verified 2026-09-18 fad6bd546 raspberry_pi_pico). carried over
  otherwise; `board_getchar` checked in fad6bd546.
- Cortex-M `SYSVIEW` and `LOGGER=rtt` builds with GCC or Clang lock RTT with PRIMASK (`hw/bsp/rtt_conf_cortexm.h`,
  force-included, from tinyusb#4356), not the vendored BASEPRI lock, which a USB IRQ at priority 0 slips past; other
  compilers and other cores keep the vendored lock (WCH: next bullet). A priority-0 ISR printing against the main loop
  corrupted 27 and 29 lines in 60 s under BASEPRI, 0 and 0 under PRIMASK; verified 2026-10-30 18400f951 stm32f407disco.
  Separate `cdc_msc` `LOGGER=rtt` runs without `SYSVIEW` (2026-10-30) peaked at 2.3 µs priority-0 entry latency under
  PRIMASK, against 0.29 µs under BASEPRI. Cost with `SYSVIEW` on the same board (2026-10-29): sampled priority-0
  interrupt entry latency up to 2.6 µs, against 0.27 µs under BASEPRI; an untraced build whose USB IRQ shares that
  priority 0, master's layout, is already late by 6 µs and more.
- A `LOGGER=rtt` build traps on WCH QingKe (ch32v): the vendored generic lock does (`rtt`, "Buffer modes and locking"),
  so tinyusb needs its own `SEGGER_RTT_LOCK/UNLOCK` there. carried over. tinyusb#4293 added one for `SYSVIEW` builds
  only, so a `LOGGER=rtt` build without `SYSVIEW` still traps, while one with `SYSVIEW=1..4` gets tinyusb's lock:
  `hw/bsp/<family>/sysview_rtt_conf.h` in ch32v20x, ch32v30x and ch583 (brace-scoped save/restore of CSR 0x800),
  force-included by its `family.cmake` to win the include-guard race against the vendored conf. All three follow the
  mask with `fence.i` (ch32v20x and ch32v30x since tinyusb#4354), checked in c3193e650.
  ch32v10x refuses `SYSVIEW`: its QingKe V3A has no `gintenr` (`rtt`), and writing CSR 0x800 hung the board, so every
  `LOGGER=rtt` build there traps. Tracked in tinyusb#4363.

## Profiling and trace

- A tick counter to watch without halting (DWT data trace): `system_ticks`, read by `tusb_time_millis_api`. carried over
  (F407 over J-Link, H743 over ST-Link); symbols checked in fad6bd546.
- On rp2040/rp2350 the stock examples spend almost all their time in `get_bootsel_button` (the BOOTSEL button read runs
  from RAM with flash access off, and `board_button_read` is called every main-loop pass): a histogram dominated by it
  is the idle loop, not a spin. On RP2040 `get_bootsel_button` is also no place to stop for a flash read (`families.md`,
  "RP2040 flash verification": use `--symbol tud_task_ext`). verified 2026-09-18 fad6bd546 pico2_etm_trace: 98.7 % of
  300 PC samples, 97.8 % of traced instructions.
- The trace init is `trace_etm_init()` in the family BSP, compiled in by the ETM build variant; trace starts there, not
  at reset. symbol checked in fad6bd546 (seven families).
- Reference Ozone projects live beside the board: `hw/bsp/<family>/boards/<board>/ozone/*.jdebug`; the project's
  board-info tool prints the one for a board. 15 of 15 resolve through `etm_capture.py --jdebug`. verified 2026-09-17
  fad6bd546 (resolution only; `etm-boards.md` has the captures).
- The bare-metal examples have no SysTick handler on rp2040/rp2350, so there is no ready calibration beat for
  `etm_profile.py --isr`: `dcd_rp2040_irq` is the USB ISR, `alarm_pool_irq_handler` the timer one, neither at a fixed
  rate. A known-rate beat has to be added for the measurement (a repeating hardware timer in the BSP, reverted
  afterwards). verified 2026-09-18 fad6bd546 pico2_etm_trace: 154–156 µs by ETM against 157.3 µs by DWT CYCCNT.
- `etm_profile.py --isr` entries, per board, from the validated captures in `etm-boards.md` (carried over):
  mcb1800 `USB0_IRQHandler,dcd_int_handler`; ra6m5_ek `tusb_int_handler,dcd_int_handler` (FSP's
  `usbfs_interrupt_handler` symbol never executes); ra8m1_ek `tusb_int_handler`.
- pico2_etm_trace sets `TRACE_ETM` in its own `board.cmake`: no build flag. A bare `-DSYS_CLK_KHZ=` only sets a CMake
  cache var and is silently ignored; the definitions that take effect are the build contract's. carried over. Its
  reference project: `hw/bsp/rp2040/boards/pico2_etm_trace/ozone/rp2350.jdebug`, checked in fad6bd546.

## SystemView

The firmware landed with tinyusb#4293 (190f1ac7a); the names, diagnostics and routes below were checked at its
follow-ups, tinyusb#4356 (892f03b9a). 190f1ac7a has `CFG_TUD_SYSVIEW`/`CFG_TUH_SYSVIEW` instead of `CFG_TUSB_SYSVIEW`, a
CMake warning instead of the FreeRTOS hook's compiler note, and the TMR timestamps on MAX32665/MAX32666 only. The
measurements dated 2026-10-22 and earlier used the experimental firmware of PR #3880 at 49e49f810. `sysview.md` has the
routes and how to read the numbers. tinyusb's CI capture takes the RTT streaming route: `hil_test.py --sysview-out`
records the raw stream on the rig before the board's final park, and `sysview_ci.py decode` decodes it off the rig.

- Build: CMake `-DSYSVIEW=1..4` (1 the USB ISR, 2 adds the `usbd`/`usbh` core, 3 the `dcd`/`hcd` API, 4 the class
  drivers), `-DSYSVIEW_POST_MORTEM=1` on top for the dump route, and `-DSYSVIEW_BUFFER_SIZE`. `make` refuses it. The
  level reaches the code as `CFG_TUSB_SYSVIEW`, one level for device and host.
  `SysView` is up-buffer 1. User events use the fixed base `TU_SV_EVENT_BASE` (512), named host-side with
  `--event-map`.
- ISR spans come from whichever entry the port uses: the `tud_int_handler`/`tuh_int_handler` macros, or the driver's
  own handler (rp2040). `ch32v20x`'s FSDEV port 0 records them too, through a C wrapper; the PIO-USB host driver
  records none. The ISR depth counter in `src/tusb.c` (`tu_sysview_isr_enter`) folds a dual-role BSP's back-to-back
  device and host handler calls into one span, and folds genuine nesting the same way: a USB IRQ preempting another
  (ch32v20x's high-priority one) records no span of its own.
- FreeRTOS task tables need the SystemView block (`SEGGER_SYSVIEW_FreeRTOS.h`) at the end of the family's
  `FreeRTOSConfig.h`; wired in `nrf`, `samd5x_e5x`, `stm32f4`, `stm32f7` and `stm32h7`. Other FreeRTOS families build
  with a compiler note and no scheduling tables (CPU load, ready→run), though `tu_sysview_stack_report` still sends
  task names and stack use at levels 2-4. tinyusb records no heap events, so `heap` is always `null`.
- Buffer: RAM-rich families default `SYSVIEW_BUFFER_SIZE` to 65536, the rest to 4096. At 4096 mimxrt1064_evk lost
  96.7% of its ISR exits at ~109 Hz; at 65536 stm32h743nucleo and raspberry_pi_pico lost nothing in a 15 s CDC burst
  over OpenOCD while feather_nrf52840_express, on an OpenOCD-over-J-Link path, lost in 73 bursts.
- RTT and SystemView share one lock: PRIMASK on Cortex-M with GCC or Clang ("Logs and RTT").
- `cdc_msc_freertos`'s 1024-byte task stacks overflowed during enumeration at level 4; the example raises them under
  `BOARD_STACK_INSTRUMENTED` (`CFG_TUSB_DEBUG || CFG_TUSB_SYSVIEW`). `SYSVIEW_RAM_BASE` defaults to `0x20000000`;
  families whose SRAM starts elsewhere (lpc11/13/15/17/18/40/43, kinetis_k, kinetis_kl) set `SYSVIEW_RAM_BASE_DEFAULT`
  in their `family.cmake`.
- WCH's USBFS/USBHS device drivers NAK the bus while the ISR holds the transfer flag (`INT_BUSY`); tinyusb
  `docs/reference/device_issues.rst` (`wch-trace-timing`) says what that does to traced timings.
- An idle enumerated CDC device emits a USB event about every 2.016 s: a rate check with no traffic reads the median
  gap as 2× the intended 1.000 s.
- ch32v307v_r1_1v0 at level 4 dropped off the bus under sustained CDC traffic. The old notes blame the recorder's
  interrupt masking in one place and, after an A/B, persistent SDI debug access in another; which applies to this
  drop is unresolved.
- Capture windows (the runs: agentrc#251, 394d2ef): a post-mortem ring of 16 KB held ~20–25 ms of CDC bulk on
  same54_xplained, one of 4096 B decoded to 0.16 s on ch32v307v_r1_1v0, and metro_m4_express's attach-only stream
  2.4–5.6 s of a 15 s workload. stm32f407disco's 64 KiB post-mortem ring, dumped after an earlier J-Link session's
  close, decoded to 12205 events with only the last 158 (6 ms) timed; dumped after a reflash, 9187 events over 243 ms,
  all timed. metro_m4_express's live J-Link window ran from boot (21.7 s for `--duration-ms 8000`; why is not
  established), and raspberry_pi_pico's timestamps are whole microseconds.
- The route comparison behind `sysview.md`'s OpenOCD loss figure (stm32f407disco `cdc_msc_freertos` level 4, ci.lan
  2026-10-22, firmware 49e49f810): live J-Link 8.4 s, ISR 83 p50 18.1 µs, SysTick 8.8, `tud_task` 23.4, overflow 131;
  OpenOCD RTT (`interface/jlink.cfg`) 17.1 s, ISR 83 p50 18.1 µs, SysTick 8.8, overflow 1136. Per-board reference
  numbers (same54_xplained, stm32h743nucleo): agentrc#251 (394d2ef).
- nanoch32v203's 838.5 Hz tick is a probe artifact, not a board clock bug (tinyusb#4296, 2026-10-23, WCH-LinkE with
  OpenOCD 0.12.0+dev-02620 `wlinke`): attaching to the running CH32V203 left it on HSI ×15 with HSE off
  (RCC_CTLR `0x03008a83`, CFGR0 `0x00b4040a`; tick 838.3–838.8 Hz) until a reset; `reset halt` then `resume` in the same
  connection restored HSE ×18 (`0x03038a83`, `0x00bd040a`) and 999.6–1000.1 Hz. Time that setup only after a reset
  inside the session; a post-mortem dump taken over such an attach carries the perturbation, so withhold its timing.
  Whether `rtt.py`'s `reset_before_attach` avoids it is not established, and the rtt skill's rule against live WCH reads
  stands.
- max32666fthr (2026-10-23, `cdc_msc` level 4, debugprobe CMSIS-DAP, OpenOCD `target/max32665.cfg`):
  - Timestamps are TMR0 at `PeripheralClock` 48 MHz; the 0.30 s + 1.0 s CDC burst cadence decodes as 1.300–1.301 s.
  - `max32665.cfg` sets no adapter speed, so OpenOCD runs SWD at 100 kHz and drains RTT at 6–7 KB/s: every capture
    stopped near 100 KB with a 64 KB ring's backlog undrained, a ~8 s window for a 15 s workload. Pass
    `-c "adapter speed 4000"` on the capture route (the 4 MHz limit is not checked: the MAX32666 datasheet is not in
    the library); loss stays nonzero even at 10 MHz.
  - Bare-metal builds leave the USB IRQ at priority 0, which a BASEPRI 0x20 RTT lock never masks: the ISR corrupted
    half-built packets (wrong function ids, negative timestamp deltas) until the PRIMASK lock (tinyusb#4294) fixed it at
    any priority; the experiments behind it are in this file at agentrc#353 (12d1443).
- max32650 and max32690 take the same TMR timestamp route (`hw/bsp/maxim/sysview_max32_tmr.h`) from tinyusb#4356
  (8bfefdc83): their user guides do not document the DWT cycle counter. Build-verified only; on hardware, still to
  check: `board_init` completing (max32690's MSDK timer init waits on clock-ready and write-done without a bound), the
  counter's progress and rate, max32690's carry between its cascaded 16-bit halves, the full rollover, and decoded
  captures under USB and FreeRTOS load.
- stm32f407disco `cdc_msc_freertos` at level 4 produces about 50k events/s under the CDC burst, more than an RTT read
  drains (tinyusb#4351, 64 KiB buffer): OpenOCD at its 2 MHz SWD clock delivered 58-67 KB/s and lost 79 % of the events,
  J-Link firmware on the on-board ST-LINK at 4 MHz 61 %; OpenOCD's 1024-byte RTT read per poll is not the limit (an
  8 KiB read cost the same per byte; measurements in this file at agentrc#353, 12d1443). Bare-metal `cdc_msc` (about
  11k events/s) loses nothing over a 15 s CDC burst with its 1 s idle gaps; 0.5 s and 0.1 s gaps, the only shorter
  ones tried, overflowed.

## Espressif integration

- Resolve the board's family from `hw/bsp/*/boards/<board>` before selecting the debug backend;
  `ls -d hw/bsp/*/boards/<board>` must identify one board directory. carried over from TinyUSB's `target-debugger`
  agent.
- Boards: `espressif_kaluga_1` (S2), `espressif_s3_devkitc` / `espressif_s3_devkitm` (S3), `espressif_p4_function_ev`
  (P4; `-DMA` variant), `espressif_s31_function_core` (S31, BSP 4536bb27b #4228; slave and DMA variants by flags); build
  and flash through the project's contracts. The Metro ESP32-S3 has no BSP: the `espressif_s3_devkitc` build runs on it
  (LED and button pins differ). That BSP puts the MAX3421 host shield's SCK and MOSI on GPIO39/42, the JTAG pads (host
  builds with `CFG_TUH_MAX3421` only, `family.c`); the `adafruit_feather_esp32s3` BSP puts them on 36/35. verified
  2026-10-15 htpc, ea1d867ce. `espressif_s3_devkitc` and tinyuf2's `espressif_esp32s3_devkitc_1` are one board in two
  rosters whose holds do not exclude each other: hold it under both names in the shared lock store, and stop if both
  holds cannot be acquired. P4 units with chip v0.1 and v1.0 both exist on the benches: build for the detected revision;
  the rig roster's "`--force` for v0.1" note predates esptool 5.2.0.
- The espressif BSP installs no UART driver, so UART0 log writes take the polled VFS path (`espressif.md`, "Console
  perturbation").
- An `esptool` board's probe serial (its `skills/hil/boards.json` key) is whatever port the rig flashes through: the CP2102N **flasher** serial on the S3 and
  P4 rig boards, never the USB-Serial-JTAG device there; openocd's `adapter serial` is the chip MAC with colons, read
  from the device. carried over (S3/P4 rig boards). On the S31 the flasher port is the USB-Serial-JTAG CDC itself, so
  its uid is the chip MAC with colons, the same string openocd takes. verified 2026-10-14 htpc
  espressif_s31_function_core.
- Break in app code for symbolized frames: `thbreak tud_task_ext`, armed once the host shows the device configured (it
  also runs the enumeration events, below); a good backtrace reads `tud_task_ext` ← `tud_task` ← `usb_device_task` ←
  `vPortTaskWrapper`. verified 2026-09-16 espressif_p4_function_ev (ci.lan) and 2026-10-15 espressif_p4_function_ev v1.0
  (htpc, ea1d867ce); symbols checked in fad6bd546.
- tinyusb's FreeRTOS examples set `CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK=y` in `sdkconfig.defaults` up to ea1d867ce
  (cdc_msc_freertos, board_test, hid_composite_freertos, audio_test_freertos, audio_4_channel_mic_freertos;
  cdc_msc_throughput does not); PR #4236, merged 2026-10-16 as d668b0abb, removes it, and a build directory from before
  keeps it until its `sdkconfig` is regenerated. On RISC-V under openocd each context switch is then a semihosting halt,
  and the rebuild recipe is in `espressif.md`, "The end-of-stack watchpoint". Here `idf.py` takes
  `-C examples/device/<example> -DBOARD=<board>` as well, `family.cmake` puts `sdkconfig` in the build directory, and an
  override file passed as `-DSDKCONFIG_DEFAULTS` loses: the example's CMakeLists appends its own defaults after it.
- Run records, 2026-10-16 (htpc, openocd 20260703, tinyusb ea1d867ce): a fresh hw-validator from an isolated config ran
  the documented recipe (attach, 1 s halt and resume, `thbreak tud_task_ext` with backtrace and thread list, the
  breakpoint budget probe, a clean end with the device answering) under host traffic on the P4 Function-EV v1.4 (chip
  v1.0), the S31 CoreBoard, the Metro ESP32-S3 rev B (pad JTAG) and the Kaluga S2 (FT2232HL by `adapter usb location`),
  each with `cdc_msc_freertos` and `cdc_msc_throughput`. Per-board results, load windows and the refuted runs' evidence:
  agentrc#101 (76d8e22). The stock `cdc_msc_freertos`, end-of-stack watchpoint on, refuted the recipe on the P4 and the
  S31 (`espressif.md`, "The end-of-stack watchpoint"); every other board and build verified. What `espressif.md` cites
  from them: the matched P4 builds inserted two hardware breakpoints with the option on and three without; the S31
  listed 9 tasks with it off (as did its non-panicking stock attach) and 6 at connect on `cdc_msc_throughput`; an
  earlier stock-build attach on the P4 (2026-10-15) had been clean (no reset, `thbreak tud_task_ext` hit in `usbd` with
  a symbolized backtrace, eight tasks, CDC echo after `shutdown`).
- Script runs, 2026-10-16 (`esp_session.py inspect`, `esp_verify.py`, `console_capture.py` on the same boards and
  builds, esptool 5.5.0): agentrc#103 (e023357). esptool 5.2.0's stub upload failed on the Metro over ESP-Prog channel B
  ("Serial data stream stopped") where 5.5.0 passed.
- cdc_msc_throughput: its CDC interface is `usb-TinyUSB_Throughput_<serial>-if00`, and its CDC filler is NUL bytes
  without a newline: a canonical-mode tty delivers nothing of it to `cat` and mimics a device-to-host stall; set raw
  mode on the reader's own fd (`techniques.md` "Log capture").
- Two cdc_msc_throughput observations outside the short-halt criterion, not yet diagnosed (P4 Function-EV v1.0,
  ea1d867ce, 2026-10-16): (1) a breakpoint-budget batch error left the target halted ~36 s under MSC traffic, the host
  reset the device meanwhile, and the resume ended in "Store access fault" at `tu_unaligned_write32(mem=0x0)` from
  `tu_hwfifo_read(hwfifo=0x50001000, dest=0x0, len=31)` ← `handle_rxflvl_irq` (dcd_dwc2.c, RX_DATA with
  `xfer->buffer == NULL` and no FIFO); the host could not enumerate it until a chip reset. (2) Device-to-host CDC
  stopped 26 s into combined CDC RX, CDC TX and MSC load before any debugger attached, MSC still serving, cleared only
  by a chip reset; a later busy/claimed endpoint snapshot (`ep_status[2] = {0x5, 0x5}`, EP 0x82 armed with 0x400 bytes)
  was taken with no reader open and is not diagnostic by itself.
- Halting inside `tud_mount_cb` on the Kaluga S2 wedged the host's enumeration: SET_CONFIGURATION at -110, a
  re-authorize at -32, only a chip reset recovered (`espressif.md`, "ESP32-S2"). `tud_task_ext` also runs the
  enumeration events, so confirm the host shows the device configured before arming a breakpoint there. verified
  2026-10-15 espressif_kaluga_1, cdc_msc_freertos ea1d867ce.
- #4229 (S31 slave-mode bulk IN truncation): the slave-mode ISR TX FIFO write and the TX FIFO flush ran unlocked against
  the task on the other core; fixed in ec7be1ed1 (#4234, 2026-10-15) with `usbd_spin_lock` around both. The autopsy used
  openocd telnet `halt`/`mdw`/`dump_image`/`resume` over the USJ; instrumentation symbols move between builds
  (`riscv32-esp-elf-nm` the flashed ELF before `mdw`). verified 2026-10-14 htpc.
- `src/portable/synopsys/dwc2/dwc2_info.py` records the S31 DWC2 values read live in 50d481aca (2026-10-14): base
  0x20300000, GSNPSID 0x4F54430A (4.30a); chip details: `espressif.md`, "ESP32-S31".
- On espressif_s3_devkitm, tinyusb's window before any app starts OTG (`espressif.md`, "ESP32-S3") is `board_test`:
  `usb_new_phy` is absent from its ELF when `CFG_TUD/TUH_ENABLED` are 0; attach, halt and symbol resolution worked in
  it. carried over. In that window the USJ dropped ~4 s after boot with nothing attached and stayed up once a client
  survived it (tinyusb 5066ac7b3, 2026-07-24; original transcript unavailable); a DevKitC running tinyuf2 blinky kept
  its USJ up for minutes unattended (2026-10-13), a differing observation on another board and build.
