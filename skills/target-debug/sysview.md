# SystemView — scheduling-level timing over RTT

Part of the `target-debug` skill (its technique ladder says when to reach for this; its Delegated sessions, Rig
discipline and Warnings apply). Live recording and GUI export require Linux; native offline decoding uses only
Python on Linux, macOS or Windows.

SystemView answers where CPU time goes: per-task and per-ISR load, ISR durations, ready→run latency, the timing of
spans and functions the firmware marks, task stack high-water, heap totals, and, from a post-mortem build, what ran
right before a hang. It does not answer logic or state bugs; those are the ladder's other rungs.

Facts below carry their evidence as `projects/` entries do (SKILL.md, "Project notes"). `carried over` marks what
moved here on 2026-10-22 from tinyusb's experimental `sysview` skill (PR #3880 at 49e49f810, SystemView 4.10b) and has
not been re-checked since.

## Firmware side

The recorder is in the firmware: SEGGER's target sources, a `SysView` up-buffer on the RTT control block, and a
timestamp source. How to build that variant (levels, buffer size, post-mortem mode, which up-buffer index `SysView`
gets) is the project's, through its `Build contract:`; never guess the channel index, read it from the build or from
the control block.

- **Timestamp.** A Cortex-M with a DWT cycle counter uses it. ARMv6-M, RISC-V, and a Cortex-M whose DWT lacks
  CYCCNT need a timer the port supplies (`SEGGER_SYSVIEW_X_GetTimestamp`). A DWT without CYCCNT (`DWT_CTRL.NOCYCCNT`
  set) builds and links, then reads every duration as zero: read `DWT_CTRL` before trusting a new part
  (`families.md`, MAX3266x).
- **Check the rate before quoting a duration**: drive host-timed 1.000 s gaps and read the median gap back from the
  trace. Confirm the traffic landed first: an event the firmware fires periodically on its own makes an idle median
  read as a multiple of the gap (the project notes give known periods). carried over.
- **Buffer.** An undersized `SysView` buffer loses events in bursts and the load figures undercount; at the same size,
  loss also depends on the probe's drain rate and its USB path (`rtt`, transport matrix). Size it by measurement:
  the report's `overflow` says when it is too small.
- **RTOS hooks.** Per-task tables (load, ready→run, stack) need the RTOS's SystemView trace hooks compiled in (for
  FreeRTOS, in its `FreeRTOSConfig.h`). A build without them still links and records ISRs and user events, and leaves
  the scheduling tables empty; task names and stack use appear only if the integration sends task info itself.
- **Coverage.** Only what the integration wraps is recorded: an ISR or function it does not wrap records nothing, and
  an integration that folds nested ISR entries into one span hides the nesting. The project notes say what theirs
  covers.
- **Never register a `SEGGER_SYSVIEW_MODULE`**: SystemView 4.10b on Linux greys out Save Recording and Export Data
  once any module is registered. Give user events a fixed id base and name them host-side (`--event-map`). carried
  over (five configurations).
- **`SEGGER_SYSVIEW_LOCK` masks interrupts** around every record, so recorder calls in an interrupt-latency-sensitive
  path add to its latency. Keep a periodic call site cheap.
- **A record in a USB ISR can be bus time.** On a controller that NAKs until the ISR clears its transfer flag, a
  traced build's throughput and transfer gaps can differ from the untraced firmware's: compare them only between builds
  with the same instrumentation, and measure throughput untraced. The project notes name such controllers.
- **The recorder adds stack**: task stacks sized for an uninstrumented build can overflow.
- **Named-object ids** are offsets from `SEGGER_SYSVIEW_SetRAMBase()`'s base: a base above the part's SRAM gives
  garbage task and mutex names. Set it to the SRAM origin from the linker script.
- Restore pristine firmware before releasing the board, and revert marker or printf touches to the source.

## Routes

| Route            | Probe                        | Commands                                                      | Gives                             |
| ---------------- | ---------------------------- | ------------------------------------------------------------- | --------------------------------- |
| Live recorder    | J-Link or J-Trace            | `sysview_record.py`                                           | the stream at SEGGER's drain rate |
| RTT streaming    | any probe with OpenOCD RTT   | `rtt.py --channel N --reset-before-attach`, then `--from-raw` | the same stream, drained slower   |
| Post-mortem dump | J-Link (Cortex-M) or OpenOCD | `sysview_dump.py`, then `sysview_record.py --from-raw`        | the last ring-full only           |

- Loss decides which records survive, so counts, CPU shares and rare events depend on the route's drain rate, and so
  can the percentiles of a lossy capture. One firmware over both streaming routes gave the same p50s within 0.1 µs,
  with about 4.5 times the loss per second over OpenOCD (the project notes give the numbers).
- RTT streaming is headless and probe-agnostic; prefer the live recorder for counts, load or rare events, and when
  OpenOCD streaming overflows at the largest buffer the part allows.
- A target whose debug link cannot read RAM while the core runs gets the dump only: WCH QingKe over SDI (`rtt`,
  transport matrix), and only on parts whose attach keeps RAM (`families.md`, WCH QingKe). Never reset it inside a
  session.
- `--from-raw` decodes natively. Add `--gui` for the SEGGER export; the GUI host setup and licensing below apply to
  that path and to live recording.

Capture runs on the probe's host with the board taken under the project's `HIL contract:` (`hil` skill). Offline
decoding of an existing file needs neither a probe nor a board lock.
`<scripts>` is this skill's `scripts/` directory.

### Live recorder

```bash
python3 <scripts>/sysview_record.py --out <dir> --hil-config <cfg> --board <board> --interface swd --speed 4000 \
  --elf <flashed.elf> --duration-ms 8000 --workload "<command that drives the case>"
python3 <scripts>/sysview_report.py <dir> --event-map <map.json> --workload-marker <id>
```

- Quote the trace's own window (`live_window_s`), not `--duration-ms`. carried over: a 6000 ms recording covered
  12–14 s of trace under the earlier recorder; not re-measured with this one.
- Code that runs only under load leaves its rows empty in an idle capture: drive it with `--workload`. Address the
  device by a stable node (`/dev/serial/by-id/...`), give writes a timeout, and read back only from firmware that
  answers: a read-back loop against a non-echoing example spends its whole timeout per iteration. carried over.
- `recording.SVDat` opens in a desktop SystemView for the timeline.

### RTT streaming over OpenOCD

```bash
python3 <rtt scripts>/rtt.py --backend openocd --probe <serial> --cfg "<target -f/-c args>" --elf <flashed.elf> \
  --channel <SysView index> --seconds 25 --reset-before-attach > capture.SVDat   # drive the workload meanwhile
python3 <scripts>/sysview_record.py --out <dir> --from-raw capture.SVDat
```

Outside post-mortem mode SystemView sends its Init record, which carries the timestamp frequency, once at boot; a
mid-stream attach can capture bytes that decode to nothing, hence `--reset-before-attach`. Where an in-session reset
does not come back (`rtt` names SAMD5x and WCH), attach without it right after the flash's own reset: the stream joins
a boot seconds old, with a shorter clean window and a few nonsense rows before the decoder syncs. carried over.
Server, control block and drain problems are `rtt`'s.

### Post-mortem dump

Build the post-mortem variant, flash it, reproduce the failure, then dump without a reset; it captures only what ran
after that flash.

```bash
python3 <scripts>/sysview_dump.py --out <dir> --channel <SysView index> --elf <flashed.elf> \
  --hil-config <cfg> --board <board> --interface swd --speed 4000          # J-Link entry
python3 <scripts>/sysview_dump.py --out <dir> --channel <SysView index> --elf <flashed.elf> \
  --probe <serial> --cfg "<target -f/-c args>" [--vid-pid "0xVVVV 0xPPPP"]  # OpenOCD
python3 <scripts>/sysview_record.py --out <dir>-decoded --from-raw <dir>/capture.SVDat
python3 <scripts>/sysview_report.py <dir>-decoded --post-mortem
```

The ring is an overwrite buffer: at bulk USB rates a few kilobytes hold tens of milliseconds, never minutes (the
project notes give measured windows). The core stays halted unless `--resume`. On WCH recover by reflashing: a
`reset run` under SDI does not come back (`rtt`).

A J-Link Commander session that closes on the running core stops CYCCNT, SystemView's Cortex-M timestamp (observed,
`techniques.md` under DEMCR.TRCENA): the ring then holds zero time deltas back to that close and is timed only from
the dump's own connect, an untimed head that can be most of a large ring (the project notes give a measured one). For
a timed ring, reset first and close no J-Link session on the running core before the dump; to resume, use OpenOCD,
since a J-Link `--resume` is such a close. The ring still shows what ran, in order; `sysview_report.py --post-mortem`
takes durations and load from the timed tail only and says so.

## Reading results

- **CPU load** is a share of the whole capture, first event to last (`live_window_s`), idle included; for the share
  during the workload give an anchor and read `contexts_workload`. Bare-metal firmware has no Task Run or Idle events,
  so only ISR shares appear there and they do not sum to 100. The time from a task blocking itself, or an ISR exiting
  into the scheduler, to the next switch is `Scheduler`'s, as SystemView names it; a task that blocks or suspends
  another keeps running.
- **ISR duration** spans the wrapped handler's enter→exit, for the invocations coverage and loss let through. A span
  far longer than the handler's instruction count is a lead (waiting on the peripheral is one cause): check it on the
  timeline and with target-side evidence.
- **ready→run** is scheduling latency, the number behind throughput jitter. A p99 far above p50 points at preemption
  by a higher-priority task or a long ISR; confirm it on the timeline.
- **functions** time each marked call, CALL→RET; rank hot ones by `n × p50_us`, not by `max_us`.
- **max is not trustworthy when `overflow` is nonzero**: a lost record pairs one call's start with a later end.
  carried over (a 134 ms max on a 10 µs function). Quote p50; treat max as a lead.
- **Small-sample p99 is unstable**: check `n` and the loss counts, and collect more samples before drawing
  conclusions from it.
- **stack** comes from what the firmware reports during the capture: a firmware that reports one task per period
  shows only some tasks in a short capture. **heap** needs the firmware to report allocations; `null` means it did
  not.

## Host setup and licensing

```bash
sudo apt-get install -y xvfb xdotool imagemagick
# SystemView: segger.com/downloads/systemview (Linux .deb, installs /usr/bin/systemview); JLinkExe: segger.com/jlink
```

- SystemView deletes `/tmp/sv-*` at startup (a build tree was erased mid-session that way). `sysview_record.py`
  gives it a private `/tmp` holding only the X server's socket directory and, under `/tmp/.sysview_record`, its
  temporary HOME and `--out`, so the host's `/tmp` is out of its reach.
- Live recording and `--gui` are Linux only. `unshare --user --map-root-user --mount` must work for the operator
  (unprivileged user namespaces). SystemView's J-Link DLL opens every J-Link it can reach at startup, live or decode,
  whichever probe `-usb` names: on a rig that raised a terms-of-use notice per other board's on-board probe and a
  "Communication timed out" while CI flashed. `sysview_record.py` runs SystemView in a namespace whose `/dev/bus/usb`
  holds only the recording probe's node, none when decoding.
- The licence dialog on an unregistered install is answered "Continue under SFL", which asserts non-commercial or
  educational use on the operator's behalf; commercial use needs a SEGGER commercial licence. The target sources are
  BSD-licensed, so a built image carries no condition. A registered key stops the dialog; killing SystemView has been
  seen to drop the key from `SEGGER_REG_HKEY_CURRENT_USER.xml`. carried over (ci.lan).
- The J-Link DLL's "Terms of use" notice (V9.78, one per on-board probe: OpenSDA, LPC-Link2, ST-LINK) is accepted
  on the operator's behalf; its per-day "don't show again" registry keys did not suppress it. Recorder Configuration
  and System Information take their default button, and a SystemView Error dialog is answered and counted
  (`systemviewErrors`). Any other dialog fails the run.
- References: SystemView User Guide UM08027 (command line §3.14, post-mortem §3.15.3, target integration ch. 4),
  through `read-doc`; target sources at github.com/SEGGERMicro/SystemView.

## Script interface

The contract a project's harness may rely on. Scripts are in `scripts/` beside this file; a harness loads or runs them
by path from the agentrc checkout it pins. Nothing below is project-specific: names, workloads and build options come
from the caller.

### `sysview_record.py` — live recording, or decoding a raw capture

```text
sysview_record.py --out DIR (--hil-config FILE --board NAME | --probe SN --device NAME) --interface swd|jtag
                  --speed KHZ (--elf FILE | --addr ADDR) --duration-ms N [--workload CMD]
                  [--no-events] [--export-terminal]
sysview_record.py --out DIR --from-raw FILE [--no-events] [--gui [--export-terminal]]
```

- Live recording is J-Link only (SystemView's own recorder); a `--hil-config` board must be a `jlink` entry. The RTT
  control block comes from `--elf` (ELF32 little-endian symbol table, no `nm`) or `--addr`.
- `--out` must not exist, or be an empty directory. It receives `contexts.csv`, `events.txt` (unless `--no-events`),
  and `record.json` with the inputs, decoder or tool versions and run times. Native decoding adds `decode.json` with
  packet diagnostics. The GUI path adds `systemview.log`, `terminal.csv` with `--export-terminal`, and `debug.png` on
  failure; live recording also saves `recording.SVDat`.
- `--workload CMD` runs through the shell in its own process group from the start of the recording. Stopping it when
  `--duration-ms` expires is the normal end, not a failure; it exiting non-zero before then fails the run (exit 3).
  `record.json` holds its exit status and how it ended (`exited` or `deadline`).
- `--from-raw` decodes bytes another route captured (`rtt.py --channel N`, `sysview_dump.py`), with no probe. Native
  decoding bypasses Linux, tool and namespace setup. `--gui` selects the existing SEGGER export. Native
  `--export-terminal` is refused with a message directing the caller to `--gui`.
- `record.json` `dialogsAnswered` counts each dialog answered, by title fragment, and `systemviewErrors` the
  SystemView error dialogs among them; `sysview_report.py` warns when it is nonzero.
- Exit: 0 recorded and exported; 2 bad usage or a GUI request off Linux; 3 decoding, a tool, the probe (none or several
  USB devices carrying its serial), the workload or an export failed, or a signal stopped the run (`record.json`
  says which).

### `sysview_decode.py` — native offline decoding

```text
sysview_decode.py FILE --out DIR [--no-events]
```

- The decoder accepts the pinned single-core SystemView RTT streams, chronological post-mortem rings, and the text
  header of GUI-saved `.SVDat` files. It writes the event/context columns consumed by `sysview_report.py`; OS API
  description files, ELF symbol rendering and terminal export are outside this subset. Registered modules require
  `--gui`. Unknown length-delimited events retain their packet bytes. With an OS description, IDs 32–511 are OS
  events; otherwise they are unnamed functions. IDs from 512 are user functions.
- A wrapped head is discarded up to the ten-zero synchronization record. Truncated trailing packets are reported
  in `decode.json` and excluded without counting a timing loss. Resynchronization or a frequency change ends the
  timed segment:
  earlier timestamps and metrics remain valid, later timestamps are blank. Init supplies no epoch, cycle samples
  wrap, and OS time samples are taken separately from the packet clock, so none re-anchor its exact timeline.
  Missing Init withholds report timing. Complete deltas accumulate without a host-side counter wrap.
- Malformed content inside a complete length-delimited packet is diagnosed without losing the next packet boundary.
  Its event ID remains visible, its arguments are withheld, and a readable trailing delta preserves clock continuity.
  Dependent call/marker spans and heap totals are invalidated. A large unsigned delta alone is not evidence of a
  negative delta; the target format permits the full unsigned range.
- Overflow invalidates open call, marker, ready and interrupt spans and heap state. The native decoder does not
  splice those spans across loss. Aggregate context runtime includes observed partial execution at EOF; duration
  samples include complete spans only.
- Native task blocked time is blank; use `--gui` when the GUI aggregate statistics are required.
- `decode.json` records the source, decoded event count, timestamp frequency and byte offsets/reasons for damaged
  input. The event rows retain valid-prefix timestamps and mark loss boundaries; the report needs no `decode.json`
  to retain prefix metrics, count timing loss in `overflow`, or warn about malformed payloads. A successful decode
  can have diagnostics; inspect them before using the report. Exit 0 means a usable prefix was decoded, not that the
  capture is complete; exit 2 means invalid input or output directory.
- Corpus runner and manifest: `bench/sysview/`; provenance and reproduction: `tests/fixtures/sysview/README.md`.

### `sysview_report.py` — numbers from an export directory

```text
sysview_report.py DIR [--json] [--event-map FILE] [--post-mortem]
                  [--window T0:T1 | --workload-marker ID | --workload-event ID]
```

- `--event-map` names user events: a JSON object from the absolute event id, a decimal string, to a name
  (`{"512": "tud_task"}`). An id it does not name is reported as `#<id>`.
- The workload window is the caller's choice, with no implicit anchor:
  - `--window T0:T1`: seconds from the first event; a range outside the capture is bad usage (exit 2).
  - `--workload-marker ID`: from the first Start of marker ID to the last Stop of it, one span over all its pairs.
  - `--workload-event ID`: from the first to the last occurrence of event ID.
  An anchor the capture does not complete (no Stop after a Start, fewer than two occurrences) leaves the workload
  fields `null` with a warning. With no anchor asked for they are `null` too.
- `--json` prints one object:

```text
contexts:   [{name, activations, cpu_pct, total_ms, blocked_ms, min_us, avg_us, max_us}]
isr, ready_run, markers:  [{name, n, p50_us, p99_us, max_us}] | null
functions:  [{id, name, n, p50_us, p99_us, max_us}] | null
stack:      [{name, bytes_used}] | null
heap:       {allocs, frees, net_bytes} | null
overflow, dropped_pairs: int | null
live_window_s: float | null           first event to last
workload_window_s: float | null, workload_anchor: "window" | "marker" | "event" | null
contexts_workload: [{name, busy_ms, cpu_pct_workload}] | null
warnings:   [str]
```

  A metric the capture cannot support is `null`, never `0` or `[]`: every events-derived field without `events.txt`
  (`--no-events`); every time-derived one (the contexts' load and times, the duration tables, the window figures)
  without a usable time base: no Init record (no timestamp frequency) or non-monotonic timestamps. `[]` means measured
  and none seen.
  An explicit native timing-loss boundary followed entirely by blank timestamps retains the established prefix's
  event and context timing; duration annotations in the untimed suffix are ignored. Unmarked unreadable timestamps
  withhold all time-derived metrics, including the full-capture context timings.
- `--post-mortem` declares a decoded post-mortem ring. More than one task switch or ISR entry at the first timestamp
  then marks an untimed head: the events up to the first later timestamp are left out of every events-derived field,
  the contexts' times are `null`, who ran before the first task switch after them is unknown (busy shares overlapping
  it withheld), and a timed tail that spans nothing has no time base. Without the flag such a tie only warns: a coarse
  timestamp clock can tie a live capture's switches too.
- Exit: 0 reported; 2 bad usage or an unreadable export.

### `sysview_dump.py` — post-mortem ring from a halted target

```text
sysview_dump.py --out DIR --channel N (--elf FILE | --addr ADDR)
                (--hil-config FILE --board NAME [--interface swd|jtag --speed KHZ]
                 | --probe SN --device NAME --interface swd|jtag --speed KHZ
                 | --probe SN --cfg OPENOCD_ARGS [--vid-pid "0xVVVV 0xPPPP"])
                [--resume]
```

- Preconditions the script cannot check: the firmware records in SystemView's post-mortem mode (the up-buffer is an
  overwrite ring), no other RTT reader is attached, and the route attaches without resetting the target or clearing
  RAM (UM08027 §3.15.3). A valid `SysView` descriptor alone establishes none of them.
- The J-Link route is Cortex-M only (the core state comes from DHCSR); OpenOCD takes the target's own state, RISC-V
  included, as `init` left it: an `init` that halts the core (WCH's, `families.md`) reads `halted`, and `--resume`
  then leaves it halted.
- Halts the core without a reset, reads the RTT control block and up-buffer `--channel`, dumps the ring, reads the
  descriptor again and refuses the dump unless it is unchanged. The core stays halted unless `--resume` and it was
  running when the dump began; a resume not seen to take leaves the files written, the state after `unknown` (J-Link)
  or `halted`, and exit 3.
- 32-bit little-endian RTT layouts only; anything else is refused.
- `--out` (new or empty) receives `header.bin`, `ring.bin`, `capture.SVDat` (the ring from `WrOff`, oldest byte
  first) and `dump.json` (addresses, offsets, sizes, core state before and after).
- Exit: 0 dumped; 2 bad usage; 3 the probe or tool failed or is missing, the read was incomplete or incoherent, the
  buffer is not SystemView's, a resume was not seen to take, or SIGTERM or SIGHUP stopped the session (the tool is
  killed, the core's state after it unknown unless the tool reported it before the kill). `dump.json` is written on
  exit 3 too, with the `error` and, once a probe session began, `core`: each state as far as it was established,
  `unknown` otherwise.

### What a harness replaces

A harness passes `--elf` or `--addr`; the scripts export no helper functions. Raw OpenOCD streaming is the `rtt` skill's
`rtt.py --backend openocd --channel N`; nothing here wraps it.

### Unattended use

A CI job runs the same scripts; its comparison, base, workload and thresholds are the project's
(`projects/<name>.md`).

- Any nonzero exit fails that stage: keep its files for diagnosis and gate on none of its numbers.
- Gate only a non-null field of a capture with no loss (`overflow` 0), unless the project has validated its metric on
  lossy captures (Routes); judge max, p99 and `n` as Reading results says. Report rather than gate a capture whose
  report warns of `systemviewErrors`.
- Keep the provenance with the report: `record.json` (its fields depend on the mode and how far the run got), and the
  capture route's own record, `dump.json` for a dump.
- `sysview_record.py` shows SystemView only its own probe (Host setup), so recording or decoding may run beside other
  boards' jobs; the dump and OpenOCD routes have no such isolation, and every route still takes the board's lock.
