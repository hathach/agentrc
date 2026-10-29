# rtt — per-board validation matrix

Board names are TinyUSB BSP aliases (`hw/bsp/*/boards/<name>` in `hathach/tinyusb`)
and the firmware was its examples: the record of what was measured, on what. The
probe, route and device/cfg columns are what carries over to another project.

A row appears here only after the board was exercised on real hardware; a new
validation adds the row AND any caveat it surfaced. "Read" = console/log
capture reached the host; "Write" = the target demonstrably consumed console
input (an echoing firmware returned the sent bytes; which firmware can is in the
project's notes, `target-debug`'s `projects/tinyusb.md`, "Logs and RTT"). Routes match
SKILL.md's capture sections; `Device/cfg` is the J-Link `--device` string or
the openocd target cfg. Rig rows (ci.lan) were validated 2026-08-24 by a
flash→capture→`ping`-echo sweep under per-board rig locks, and
re-validated 2026-08-25 end-to-end through the skill's own CLI
(`scripts/rtt.py`, jlink + openocd backends): 20/20 read+write —
including CONCURRENTLY at 8 parallel consoles (20 boards in 39 s, mixed
routes, no port collisions or cross-board output bleed: one server per
probe on its own ephemeral port). htpc rows on the local bench (a dev PC). The two
htpc rows dated 2026-09-18 are that day's CLI runs: pico2_etm_trace boot log
read and `--dump` byte-identical to an independent memory read;
raspberry_pi_pico boot log read, input delivery shown by the down-buffer `WrOff`
advancing 5 → 8, which is delivery, not a Write. The openocd backend's `--reset-before-attach`
is decode-validated: a channel-1 SystemView capture on stm32h743nucleo
(byte-identical boot preamble to the sysview campaign's golden reference,
49765 events decoded, ISR/task timings matching to 0.1 µs, overflow 0).

| Board                    | Rig  | Probe                  | Route   | Read | Write | Device/cfg            |
| ------------------------ | ---- | ---------------------- | ------- | ---- | ----- | --------------------- |
| ea4088_quickstart        | htpc | LPC-Link2 J-Link fw    | J-Link  | yes  | yes   | `LPC4088`             |
| raspberry_pi_pico2       | htpc | J-Trace PRO            | J-Link  | yes  | —     | `rp2350_m33_0`        |
| pico2_etm_trace (2026-09-18) | htpc | J-Trace PRO        | J-Link  | yes  | —     | `RP2350_M33_0`        |
| raspberry_pi_pico (2026-09-18) | htpc | debugprobe (CMSIS-DAP) | OpenOCD | yes | — | `target/rp2040.cfg`   |
| frdm_k64f                | ci   | J-Link                 | J-Link  | yes  | yes   | `MK64FN1M0xxx12`      |
| feather_nrf52840_express | ci   | J-Link                 | J-Link  | yes  | yes   | `nrf52840_xxaa`       |
| metro_m4_express         | ci   | J-Link                 | J-Link  | yes  | yes   | `ATSAMD51J19`         |
| lpcxpresso11u37          | ci   | J-Link                 | J-Link  | yes  | yes   | `LPC11U37/401`        |
| lpcxpresso55s28          | ci   | J-Link                 | J-Link  | yes  | yes   | `LPC55S28`            |
| ra4m1_ek                 | ci   | J-Link                 | J-Link  | yes  | yes   | `R7FA4M1AB`           |
| stm32f072disco           | ci   | J-Link                 | J-Link  | yes  | yes   | `stm32f072rb`         |
| stm32f407disco           | ci   | J-Link                 | J-Link  | yes  | yes   | `stm32f407vg`         |
| stm32f723disco           | ci   | J-Link                 | J-Link  | yes  | yes   | `stm32f723ie`         |
| stm32l476disco           | ci   | J-Link                 | J-Link  | yes  | yes   | `STM32L476VG`         |
| mimxrt1064_evk           | ci   | J-Link                 | J-Link  | yes  | yes   | `MIMXRT1064xxx6A`     |
| nrf54lm20dk              | ci   | J-Link                 | J-Link  | yes  | yes   | `NRF54LM20A_M33`      |
| max32666fthr             | ci   | CMSIS-DAP              | OpenOCD | yes  | yes   | `target/max32665.cfg` |
| raspberry_pi_pico        | ci   | debugprobe (CMSIS-DAP) | OpenOCD | yes  | yes   | `target/rp2040.cfg`   |
| raspberry_pi_pico_w      | ci   | debugprobe (CMSIS-DAP) | OpenOCD | yes  | yes   | `target/rp2040.cfg`   |
| raspberry_pi_pico2       | ci   | debugprobe (CMSIS-DAP) | OpenOCD | yes  | yes   | `target/rp2350.cfg`   |
| adafruit_fruit_jam       | ci   | debugprobe (CMSIS-DAP) | OpenOCD | yes  | yes   | `target/rp2350.cfg`   |
| stm32h743nucleo          | ci   | ST-Link                | OpenOCD | yes  | yes   | `target/stm32h7x.cfg` |
| stm32g0b1nucleo          | ci   | ST-Link                | OpenOCD | yes  | yes   | `target/stm32g0x.cfg` |
| stm32u083nucleo          | ci   | ST-Link                | OpenOCD | yes  | yes   | `target/stm32u0x.cfg` |

Probe serials live in the rig's config (in TinyUSB: `test/hil/*.json`) —
always pass them (`--probe` / `adapter serial`).

## Caveats

- **ea4088_quickstart**: probe has no VCOM and the BSP has no UART — RTT is
  the ONLY console; measured there: 6/6 JLinkExe attaches, boot burst
  delivered, 24.6 KiB/s drain; in TinyUSB the HIL suite runs its
  device_info-class tests over this console (the cdc/msc-fixture host tests
  don't speak RTT yet).
  NEVER point OpenOCD at this J-Link-firmware probe (jaylink knocks it off
  USB; physical replug). JLinkGDBServer never finds the CB headless on this
  part; JLinkRTTLogger 0/6.
- **raspberry_pi_pico2 (htpc, J-Trace)**: pin the probe by serial — that
  bench runs two J-Links, the flash step included. Never set a custom JLinkScript for RP2350 over J-Link. Write path
  untested there only because the flashed example doesn't poll the console
  (the ci row's debugprobe sweep validated RP2350 writes).
- **ST-Link rows**: flashed by `STM32_Programmer_CLI`; RTT capture is a
  separate openocd session (`interface/stlink.cfg` + the target cfg above),
  attach without reset.

## Excluded (recorded so absence is never read as "works")

- `espressif_s3_devkitm`, `espressif_p4_function_ev` — no SEGGER RTT path in
  the tested TinyUSB builds; their console is UART0 on the CP2102N bridge,
  with the USB-Serial-JTAG CDC as secondary where the chip keeps it
  (`target-debug`'s `espressif.md`, "Console").
- `ek_tm4c123gxl` — flashed by `lm4flash`; no debug-probe path configured on
  the rig.
- `nanoch32v203`, `ch32v103r_r1_1v0`, `ch32v307v_r1_1v0`, `ch582m_evt` — in
  TinyUSB a `LOGGER=rtt` build traps on WCH QingKe (the vendored generic RISC-V
  `SEGGER_RTT_LOCK` reads `mstatus` CSRs → mcause=2) unless it also sets
  `SYSVIEW=1..4`, whose force-included `sysview_rtt_conf.h` brings TinyUSB's
  own lock on ch32v20x, ch32v30x and ch583; ch32v10x refuses `SYSVIEW`,
  so there every `LOGGER=rtt` build traps (`target-debug`'s
  `projects/tinyusb.md`). SDI permits no live streaming anyway
  (transport matrix).
