---
name: rtt
description: Use when console, printf or log I/O must go over a debug probe on real hardware — no UART wired, no probe VCOM, "RTT Control Block not found", an RTT server that won't start or drops output, JLinkRTTLogger or openocd rtt misbehaving, or reading the RTT ring post-mortem.
---

# rtt — SEGGER RTT transport and console

RTT is nothing but RAM: a control block `_SEGGER_RTT` (starts with the magic
string `"SEGGER RTT"`) plus per-channel ring buffers
`{sName, pBuffer, SizeOfBuffer, WrOff, RdOff, Flags}`. The target advances
`WrOff`; the host must **write `RdOff` back** to free space — a reader that
only reads never drains the ring. Channel 0 is the "Terminal" console;
SystemView claims its own `"SysView"` up-buffer on the same control block —
they coexist. The debug probe reads/writes this RAM while the core runs, so
everything here is zero-wiring: no UART, no VCOM.

Scope: byte transport and console. Timing/profiling → `etm-trace`;
debugging decision flows → `target-debug`;
Espressif consoles → `esp-target-debug` (USB-Serial-JTAG, no SEGGER RTT).

## Quick start — console on a J-Link probe

Use `<skill dir>/scripts/rtt.py` for every route; do not hand-roll
JLinkExe/JLinkGDBServer/openocd/telnet pipelines (`--help` for all modes):

```bash
# firmware: printf/stdio routed to RTT channel 0 — how to build that variant is the
#   project's build contract (target-debug, "What the project supplies")

# flash + reset FIRST (the console owns the probe once open), then:
python3 <skill dir>/scripts/rtt.py --backend jlink --probe <serial> --device <JLINK_DEVICE> --interface swd --speed auto --seconds 20
#   or, from the project's HIL config (the backend follows from the board):
#     --hil-config <file> --board <name> --interface swd --speed auto --seconds 20
#   --interface and --speed have no default; -i forwards stdin to the target; --seconds 0 streams until Ctrl-C/EOF
#   --stop-file <fresh path>: automation ends a --seconds 0 capture by creating the
#   file (exit 0); a path that already exists is a completed cancellation, so exit 0
#   alone never proves target output
```

Windows is supported by the script (`JLink.exe` default, process trees retired
with `taskkill`) but unverified on hardware. `RTT_JLINK_EXE` and `RTT_OPENOCD_EXE`
override the server executables on any platform, as `RTT_NM` does for nm.

`--device` is the J-Link device name of the MCU. Always pass the probe
serial — rigs and benches run several probes, and an unpinned flash grabs
whichever J-Link enumerates first: flash with `JLinkExe -SelectEmuBySN` or
the build system's equivalent. Keep unattended console builds quiet (RTT
logging WITHOUT verbose log levels): reset-then-attach only preserves what
fits the up-buffer (stock 1 KB, NO_BLOCK_SKIP), and a chatty boot burst
truncates at the ring boundary before the drain attaches. `BUFFER_SIZE_UP` is
the knob when verbose logs are really needed. Shared rig boards: hold the
project's board lock first.

To validate bidirectionality end-to-end you need firmware that both polls
the console AND replies through the RTT-routed printf; an echo that goes
out another path proves nothing. Which firmware of a project does that is in
its notes (`target-debug`'s `projects/`). Without one, prove delivery at the
target: read the down-buffer's `WrOff` over the probe before and after sending
(aDown[0] sits at control block + 0x18 + 24 × MaxNumUpBuffers, `WrOff` its
fourth word) — it advances by the bytes sent.

## Transport matrix

| Transport / tool                                | Live read  | Write    | Notes                                                                                                                                                                                                                                                                                          |
| ----------------------------------------------- | ---------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ARM memory-AP (any J-Link/ST-Link/CMSIS-DAP)    | yes        | yes      | zero intrusion; core keeps running                                                                                                                                                                                                                                                             |
| RISC-V SBA (where implemented)                  | yes        | yes      | autonomous like memory-AP                                                                                                                                                                                                                                                                      |
| WCH QingKe SDI                                  | **NO**     | no       | DM abstract-command reads perturb the running core: A/B-proven firmware kill ~1.9 s into USB traffic. Halt→read→resume or post-mortem dump ONLY                                                                                                                                                |
| OpenOCD/jaylink on a genuine SEGGER J-Link      | yes        | untested | routine in the sysview campaigns (metro_m4_express, dozens of attaches, zero wedges); prefer SEGGER tools where both exist (drain rate)                                                                                                                                                        |
| OpenOCD/jaylink on the LPC-Link2 (J-Link OB fw) | forbidden  | —        | measured on ea4088's LPC-Link2 (2023 OB image): transport fails (`jaylink_swd_io`) and knocks the probe off USB; physical replug to recover — SEGGER tools only THERE. Verdict is for that probe only: other J-Link-OB firmware probes are untested — hardware-test before assuming either way |
| `JLinkRTTLogger`                                | unreliable | —        | searches for the control block once at attach and gives up — on some parts it never finds it ("RTT Control Block not found" even with `-RTTAddress`; measured 0/6 on LPC4088). May work elsewhere, but don't build automation on a single-search tool                                          |

Validated boards, directions and per-board caveats: [boards.md](boards.md).

## Capture: J-Link route

`rtt.py` above is this route packaged: JLinkExe owns the probe and serves
channel 0 on an RTT telnet port.

Commander keeps hunting for the control block and delivers the buffered boot
burst once the target's first printf creates it. `JLinkGDBServer
-RTTTelnetPort` also serves the port but on some parts (measured: LPC4088)
never locates the control block **unless a GDB client attaches** — fine
inside a GDB session, a silent failure headless — and it briefly halts the
core on connect (measured), which matters for timing-sensitive repros;
Commander does not. One telnet client per port at a time.

## Capture: OpenOCD route (native probes: ST-Link, CMSIS-DAP)

This is the LIVE route — WCH-Link targets are SDI and get only the halt→dump
route (transport matrix). Same script, openocd backend (`--elf` = the
FLASHED elf; the script takes the exact control-block address from `nm` —
a full-RAM scan is slower and can match stale RAM after a soft reset):

```bash
python3 <skill dir>/scripts/rtt.py --backend openocd --probe <serial> \
  --cfg "-f interface/stlink.cfg -f target/stm32h7x.cfg" --elf <flashed.elf> --seconds 20
#   --channel: up-buffer index (0 = "Terminal" console; any other is whatever the
#   firmware configured there, e.g. SystemView's "SysView"); -i forwards stdin → the SAME-numbered down-buffer
#   --vid-pid "0x2e8a 0x000c": pin the probe by USB IDs. With --probe it keeps openocd
#   discovery off foreign usbfs nodes; alone it must match exactly ONE attached probe
#   --addr 0x2000xxxx: explicit control-block address when the flashed elf is not at hand
#   --reset-before-attach: reset the target INSIDE the session (2 s settle, then
#   attach — the control block must exist before `rtt start` can find it; the ring's
#   NO_BLOCK_SKIP head-retention is what preserves byte 0 across the settle) —
#   required for streams that only decode from byte 0
#   (SystemView emits its Init record, carrying the timestamp frequency, once at boot;
#   a mid-flight attach yields a stream no decoder can lock onto). Verified on
#   stm32h743nucleo: after the ring is drained, a plain attach misses the boot preamble
#   entirely and this flag captures it. NOT for SAMD5x (an in-session reset via the DSU
#   leaves the core held) or WCH SDI.
```

No reader drains the up-buffer during that 2 s settle. Size the ring for the
whole boot burst, including moderate logs; the project's notes carry the
measured value where one is known.

Attach WITHOUT reset when the flash step already reset the board (on SAMD5x,
an in-session `reset run` goes through the DSU CPU Reset Extension and leaves
the core held). After any reset the target's offsets restart at zero while
the server holds stale ones, and the tool exposes no console to type into (it
launches openocd with tcl/gdb/telnet ports disabled): stop the capture and
run it again to resync — do not reset mid-capture if you can avoid it. `rtt start`
fails while the block doesn't exist yet: it appears at the firmware's first
RTT write, so reset, settle ~500 ms, then start. Read AND write validated on
the ci rig's 8 native-probe boards (ST-Link + CMSIS-DAP, incl. RP2350),
end-to-end through this script's backend on all 8 — per-board rows in
boards.md. OpenOCD polls, and host-side loss is invisible
to the target's overflow counter: at the default 100 ms interval a busy
stream loses most samples (measured 2066 of 5064 events/s delivered on
stm32f407disco) — `rtt polling_interval 1` is mandatory for quantitative
capture, not a tuning nicety. Prefer SEGGER tools where a J-Link exists.

## Post-mortem: reading the ring without a live server

Default log mode is `NO_BLOCK_SKIP`: with no reader draining, the ring holds
the **first KB after boot, not the tail**. To keep the last N bytes instead, the firmware must log via
`SEGGER_RTT_WriteWithOverwriteNoLock` (target drags `RdOff` itself; no host
needed) — but SEGGER's own restriction comes with it: *"Do not use
SEGGER_RTT_WriteWithOverwriteNoLock if a J-Link connection reads RTT data"*
(SEGGER's `SEGGER_RTT.c`), because the target moving `RdOff` races
the host reader. So it is for firmware you dump post-mortem, never for a
board that also runs a live console (every rig console board does). Reading a wedged target's ring — debug-AP RAM reads don't halt the
core:

```bash
python3 <skill dir>/scripts/rtt.py --backend jlink --dump ring.bin \
  --probe <serial> --device <JLINK_DEVICE> --interface swd --speed auto \
  --elf <flashed.elf>   # or --addr 0x...
# prints pBuffer/Size/WrOff/RdOff; WrOff/RdOff delimit the valid bytes
# never overwrites: ring.bin must not exist yet; a block without the "SEGGER RTT"
# signature or with offsets outside its ring is refused before the ring itself is read;
# only a dump of exactly the ring's size is kept
```

## Buffer modes and locking (target side)

- Modes: `NO_BLOCK_SKIP` (default for logs — drops whole writes when full),
  `NO_BLOCK_TRIM`, `BLOCK_IF_FIFO_FULL` (target spins — dangerous in ISRs).
- Throughput is drain-limited: measured 24.6 KiB/s over a J-Link console
  against a saturating printf loop, with the drops happening at the target.
  RTT console output is NOT lossless under load; for high-bandwidth streams
  size the buffer up (SystemView needs 2048–8192) and watch for overflow.
- Non-ARM ports must supply `SEGGER_RTT_LOCK/UNLOCK`: the vendored generic
  RISC-V lock uses `mstatus` CSRs that trap (mcause=2) on WCH QingKe, which
  needs a save/restore of its own interrupt-enable CSR (0x800) instead.

## Common mistakes

- **Attaching before the first printf** — the control block is zeroed `.bss`
  until the firmware's first RTT write; early readers see nothing (and
  RTTLogger gives up for good). Commander/`rtt.py` keep hunting.
- **Sending input before the server finds the control block** — the J-Link
  telnet route silently DROPS client bytes until then (measured on the rig:
  an instant `ping` vanished, a delayed one echoed). `rtt.py -i`
  holds stdin until target output flows (or 5 s); when driving the raw
  socket yourself, wait for output before writing.
- **Resetting while a console is attached** — flash and reset first; the
  console owns the probe until closed.
- **Killing servers with `pkill -f`** — the pattern matches your own shell's
  cmdline (and unrelated sessions): a compound command that pkills its
  wrapper then re-reads a stale log misdiagnosed a healthy probe for an
  hour. Close `rtt.py` with Ctrl-C/`--seconds` (its teardown reaps
  the whole process group); if you must pattern-kill, bracket a char:
  `pkill -f '[J]LinkExe -USB <serial>'`.
- **Unpinned flash with several probes attached** — pin by serial, always.
- **Two probes wired to one SWD header** — wedges the target; rewire.
- **Expecting an echo from firmware that never reads the console** — only
  code that reads down-buffer 0 consumes input.
- **`-i` input that did not arrive** — the capture exits 1 and says so ("did not
  reach the target"); a stalled write is bounded by `HIL_SERIAL_WRITE_TIMEOUT`
  (seconds, default 10).
- **Full-RAM `rtt setup` scans** — can lock onto a stale pre-reset block;
  use the `nm` address.
