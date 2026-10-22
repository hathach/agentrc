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
- A `LOGGER=rtt` build traps on WCH QingKe (ch32v): the vendored generic lock does (`rtt`, "Buffer modes and locking"),
  so tinyusb needs its own `SEGGER_RTT_LOCK/UNLOCK` there. carried over. History, as of 2026-09-17: a working port
  existed only on the unmerged branch `claude/add-systemview-debug` — `hw/bsp/ch583/sysview_rtt_lock_wch.h`
  (brace-scoped save/restore of CSR 0x800) and a shared `hw/bsp/sysview_rtt_conf_wch.h` that the ch32v20x/ch32v30x
  `family.cmake` force-include to win the include-guard race against the vendored conf. Not on master; check whether it
  has landed before citing either path.

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
- The HIL config's `esptool` uid is whatever port the rig flashes through: the CP2102N **flasher** serial on the S3 and
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
- Fresh-executor runs of the documented recipe, 2026-10-16 (hw-validator from an isolated config, openocd 20260703,
  tinyusb ea1d867ce). "Recipe" is attach, 1 s halt and resume, `thbreak tud_task_ext` with backtrace and thread list,
  the breakpoint budget probe and a clean end with the device answering; "load" is the host traffic during a baseline, a
  debug window holding the halt and the hit, and an after-window. The chip behaviour behind each verdict is in
  `espressif.md` (Attach, "The end-of-stack watchpoint", the variant sections):

  | Board, example                                                                   | Recipe                                                                                             | Under load |
  | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | --- |
  | P4 Function-EV v1.4 (chip v1.0), cdc_msc_freertos stock                          | refuted (below)                                                                                    | 8 KiB MSC ops 12.8 ms before the attach, ~10 s after; one run lost CDC and MSC around the attach, another's CDC probe timed out 5 s after it with no sustained traffic |
  | P4, cdc_msc_freertos, watchpoint off                                             | all verified (8 tasks, the CDC echo after `shutdown` reproduced)                                   | no echo or MSC op lost; the only gaps (1.09 s, 1.00 s) at the halt and the hit; service kept after `shutdown` |
  | P4, cdc_msc_throughput                                                           | all verified (5 tasks at connect, 3 hardware breakpoints, 2 s halt)                                | 80/80 MSC ops of 16 MiB with CDC both ways; host-to-device CDC kept through a 1.1 s halt and one hit, zero kernel messages; device-to-host CDC had stalled before the attach (below), so that direction is unverified |
  | S31 CoreBoard, cdc_msc_freertos stock                                            | refuted (below)                                                                                    | under a 100 ms CDC echo loop and the 8 KiB RAM-disk loop, the first MSC ops after the attach took 11-12 s and CDC writes timed out before the 1 s halt |
  | S31, cdc_msc_freertos, watchpoint off                                            | all verified (9 tasks)                                                                             | no echo lost across baseline and window (985/985), no kernel line; service kept after `shutdown` |
  | S31, cdc_msc_throughput                                                          | all verified (6 tasks at connect)                                                                  | 16 MiB MSC ops and 1 MiB CDC writes all completed; one 1.017 s CDC RX gap at the halt; no new kernel message |
  | Metro ESP32-S3 rev B, pad JTAG, `espressif_s3_devkitc` build of cdc_msc_freertos | all verified (8 tasks); idle, `usbd` reaches `tud_task_ext` within ~0.9 s                          | CDC echo loop and the 8 KiB RAM-disk loop: no malformed echo, I/O error, read mismatch or kernel error |
  | Metro, cdc_msc_throughput                                                        | all verified (5 threads at the hit)                                                                | 125 s window: one 1.029 s CDC RX gap at the halt, 50 CDC writes of 1 MiB and 3 direct MSC ops of 16 MiB all completed, zero kernel messages; the writes (~127 s) and the MSC ops (138 s) each ran longer than the window, and the full-speed link carried all three streams at once |
  | Kaluga S2 (FT2232HL by `adapter usb location`), cdc_msc_freertos                 | all verified (tasks usbd/IDLE/blinky/cdc/io); its end-of-stack watchpoint absent from openocd `wp` | 8 KiB RAM-disk loop and a CDC echo every ~100 ms: no echo or dd op lost, the only extended gap the halt (1.105 s) |
  | Kaluga, cdc_msc_throughput                                                       | all verified                                                                                       | 120 s with the device enumerated throughout, zero kernel messages: one 16 MiB MSC op takes 42-45 s at full speed, the 1 s halt a 1.2 s CDC RX gap |

  The two refuted rows: the P4 recovered through a kernel port power cycle in one run and needed `init; shutdown` or
  `reset run` from openocd in both, with no final whole-flash verification; the S31 stayed unavailable after `shutdown`
  and its esptool reset started the Breakpoint boot loop until `init; shutdown`, flash verified intact. Behind them:
  2630 `debug_reason=00000001` halts in a six-minute P4 session and 1247 on the S31; the P4 reset with "panic'ed
  (Breakpoint)", `rst:0xc`, on one attach in three and once logged a PMP Store access fault (contribution unresolved);
  the S31 parked in the panic handler on one attach in two (backtrace `vTaskSwitchContext` → `vPortSetStackWatchpoint` →
  `esp_cpu_set_watchpoint(wp_num=3)` → `esp_panic_handler`); the matched P4 builds inserted two hardware breakpoints
  with the option on and three without, and openocd logged "could not remove watchpoint #0" at shutdown; the S31
  inserted four either way, and `wp` listed the firmware watchpoint only after their deletion and resume; its
  non-panicking stock attach listed nine tasks. An earlier stock-build attach on the P4 (2026-10-15, before these runs)
  had been clean: no reset, `thbreak tud_task_ext` hit in `usbd` with a symbolized backtrace, eight tasks listed, CDC
  echo after `shutdown`.
- target-debug script runs, 2026-10-16 (htpc, openocd 20260703, esptool 5.5.0, the builds above): `esp_session.py
  inspect` on the P4, the S31, the Kaluga S2 (by USB location) and the Metro S3 (ESP-Prog serial) ended with every core
  running, an empty console capture and the app devices' USB numbers unchanged; with gdb hung after the connect and
  killed at `--timeout`, the Tcl side resumed both P4 cores. `esp_verify.py` matched the flashed builds on the P4, the
  S31 (USJ) and the Kaluga S2 (UART) and named the bootloader and app mismatches against other builds.
  `console_capture.py` opens with `--lines low` or `leave` reset none of the P4, the S31 and the Metro (no banner in
  6 s, USB numbers kept); a USJ-driven reset of the P4 (DTR low, RTS high) kept the whole ROM banner in the capture.
  Later the same day: `esp_verify.py` matched the Metro's cdc_msc_throughput over ESP-Prog channel B after a forced
  download boot (`espressif.md`, "ESP32-S3"; esptool 5.2.0's stub upload failed there with "Serial data stream
  stopped", 5.5.0 passed); with the end-of-stack watchpoint build (cdc_msc_freertos, option on) the
  Metro's budget stayed exactly 2 and the S31's attach panicked ("The end-of-stack watchpoint"); both boards were put
  back on their option-off images and verified.
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
