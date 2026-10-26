---
name: target-debug
description: "Use when firmware misbehaves on real hardware and its own state must explain it — a wedge, STALL, endless NAK, lost data, a stuck endpoint, HardFault, stuck ISR or spinning core. Evidence over a debug probe: logs, GDB autopsy, RAM trace, PC-sampling, SWO. Also ETM trace and profiling with a SEGGER J-Trace and Ozone (hot functions, coverage, what ran before a hang; \"No trace clock present\"); SEGGER SystemView per-task and per-ISR load, latency and the scheduling timeline; Espressif chips over the built-in USB-Serial-JTAG (303a:1001) or pad JTAG with openocd-esp32, \"could not find or open device\", or the debug port becoming the firmware's USB device."
---

# target-debug — target-side capture & debugging over a debug probe

The **target** is the MCU running the code under test. The discipline here holds for any firmware on a debug probe, and
each technique names the architecture it exists on; the worked cases are USB stacks (device role, host role, or both),
where the link peer is not always a Linux PC: an MCU host may face another board or a Linux gadget (e.g. a Raspberry
Pi).

## Which skill answers what

| Skill                     | Answers | Needs |
| ------------------------- | --- | --- |
| **`target-debug`**        | **what the target did** (driver state, faults, where the core spins, which instructions ran) | a debug probe on the target; a J-Trace for instruction trace |
| `rtt`                     | the target's log/console stream, and a post-mortem ring dump | a probe, firmware that logs over SEGGER RTT |
| `usb-sniffer`             | what crossed the wire (PIDs, handshakes, resets) | the hardware tap cabled in; role-agnostic |
| SystemView (`sysview.md`) | where CPU time goes: task/ISR schedule, per-context load, switch and ready→run timing, or what ran right before a crash/hang | firmware built with SystemView's recorder; a J-Link/J-Trace for live recording, an OpenOCD probe for RTT streaming, either for a post-mortem dump |
| `usb-kernel-debug`        | what a Linux host exchanged (usbmon URBs) and why its kernel acted (dynamic debug) | Linux on either end: PC host or gadget peer |

**Espressif targets** (ESP32-S2/S3/P4/S31 and the C series): read `espressif.md` first; it names the tools, the JTAG
route per chip and the console.

## What the project supplies

These skills know probes, not projects. Three things come from the project, and none of them is ever guessed:

- **How to build a debug variant** (logging over RTT, trace init, where the ELF lands, the J-Link device name or OpenOCD
  config of a board): the project's `Build contract:` line in its `CLAUDE.md` names the skill or document that says. No
  contract: use the ELF and device/config the caller gave, and ask for what is missing.
- **How to take a rig board**: the project's `HIL contract:` line names its rig skill, which owns lock and release and
  says which HIL config json describes the host you are on. No contract: follow the procedure the caller gave; on a
  shared rig with none, ask before touching a board.
- **What its symbols and symptoms mean**: kept here, in `projects/`, for the projects this skill has been used on — see
  "Project notes" at the end.

A HIL config json maps board → probe (`--hil-config <file> --board <name>`). `rtt.py` and `swo_capture.py` turn a
board's `flasher` entry into their flags, and a flag given as well must agree:

| `flasher` field | Means | Becomes |
| --------------- | --- | --- |
| `name`          | how the rig flashes: `jlink`, `openocd`, `stlink` are probe routes; `esptool`, `lm4flash` are refused: they name no debug probe (Espressif debug transport and selector: `espressif.md`) | the backend (`stlink` → openocd, and then `--cfg` is yours to give) |
| `uid`           | the probe's serial | `--probe` |
| `args`          | jlink: exactly `-device <name>`; openocd: its `-f`/`-c` arguments; stlink: refused | `--device`, or `--cfg` whole |
| `vid_pid`       | the probe model's USB IDs, openocd only | `--vid-pid` |

A J-Link entry supplies neither interface nor speed. On the J-Link route `--interface` and `--speed` stay explicit; on
the OpenOCD route they live inside `--cfg` and the two flags are refused. `pc_sample.py` and `sysview_record.py`
(`jlink` entries only) merge a `jlink` entry the same way; `autopsy.py`, `probe_state.py` and `sysview_dump.py` take
`jlink` and `openocd` entries from the entry alone, as `pc_sample.py` takes an `openocd` one. `etm_capture.py` does not
read it — a J-Trace is one probe moved between boards, named with `--probe`.

## Delegated sessions

When an agent dispatches this work (chief's `hw-validator` and `hw-debugger` units), the prompt is the scope and these
rules bind the unit; its role file adds only what it may edit and when it stops.

- **Scope.** Stay within the prompt's board, operations and source paths; resolve the build and access through the
  project's contracts or the ELF, device/config and procedure it supplies. The dispatch supplies hardware task scope; no
  human grant or verbatim exchange is needed for its hardware operations. Missing technical inputs or scope return
  `blocked` with what the caller must supply. Use `needs-user` for human-only action or a permission still required for
  publishing, destructive actions on data or third-party access, instead of waiting for an answer. Establish the
  physical setup a technique needs before using it. If wiring cannot be established from current evidence or the
  supplied setup information, name the missing setup and proposed experiment in `next`: for ETM, follow `etm-trace.md`'s
  per-board wiring rule.
- **Identity.** Verify worktree, branch and HEAD, then board, probe serial and host against the prompt, or the probe
  against the HIL config entry it names, before acting. Resolve the board's family and debug backend from the project's
  metadata before choosing probe tools, never from the board name or its flashing backend.
- **Lock.** Hold the board through its HIL contract's Reservation for the whole hardware phase. A refused hold waits
  within the lock-wait budget, then returns `blocked` with the holder; a recovery blocker (a cleanup holder, a
  protected record left behind or a takeover journal) returns `blocked` at once. Never kill a holder, release a hold not yours or bypass a lock unless the prompt carries the scope
  the shared `hil` page's Board locks names for that board and operation, read by path (`~/.claude/skills/hil/SKILL.md`;
  under Codex `~/.codex/skills/hil/SKILL.md`), never through a skill lookup a project's own skill could shadow; a
  refusal alone never adds it, and a CI runner is never stopped. Reuse a supplied HIL reproduction while its firmware,
  configuration, reproducer and relevant rig inputs match; otherwise resolve equivalent
  manual steps through the HIL contract, and if there are none return `blocked` naming the exact HIL reproduction
  needed, so the caller arranges it through the project's HIL role. A later manual session re-establishes firmware
  identity and state and never treats a snapshot taken after that handoff as the earlier failure state without evidence.
- **Builds.** Through the build contract into a private build directory, leaving firmware staged for HIL untouched.
  Before the first hardware change, save the restoration artifact apart from later build outputs and pin it: its
  revision, configuration and hash, recorded as `cleanup.pristine`; cleanup restores that identity, and later commits do
  not move it. When the project's HIL contract defines a cleanup state instead of a restoration firmware, record that
  state there. Tracked paths a build may rewrite are in the prompt's temporary scope, or reported before building.
- **Instrumentation.** Only the listed paths, from a clean checkout, uncommitted, each diff saved as a patch in the
  artifact directory. From a previous unit's dirty state (a recovery prompt): inspect the recorded patch and current
  changes first, restore only what is attributable to it, report ambiguous ownership untouched.
- **Observation.** The prompt's observation window is the ceiling on one reproducer attempt; the criterion states the
  required operations or minimum exposure, and a completed deterministic operation ends the attempt. Every reproducer
  invocation, including smoke checks, cleanup checks that invoke it and failed or truncated captures, is accounted for
  in `runs` and counts toward the repetition ceiling; a state read or run-state observation that does not invoke the
  reproducer costs time, not a repetition. A `runs` row aggregates identical attempts and splits where purpose,
  firmware, conditions or outcome differ. An early decisive observation may settle the claim; otherwise a shortened
  capture is partial evidence and cannot establish a pass that needs the full exposure. Anchor every state reading as
  Warnings, "A register snapshot without a validity anchor lies", says, and record whether attaching halted or reset the
  target; a post-reset snapshot is never the failure state.
- **Host.** Leave the host as found: never clear kernel logs (`dmesg -C`) or other shared history, and restore every
  setting you change (dynamic debug, module parameters). Anchor kernel-log reads on raw kernel timestamps
  (`dmesg --time-format raw`) and take the clock mapping from a kernel event you caused at a known wall time: line
  counts fail once the ring wraps, `-T` and ISO stamps need that mapping checked, and `/proc/uptime` is not it (`-T` and
  `/proc/uptime` were both ~30 s off on htpc, 2026-10-16). A host with `dmesg_restrict` refuses an unprivileged
  `dmesg -w` ("Operation not permitted"): read it with sudo.
- **Budget.** Stop starting experiments when the remaining time cannot cover the experiment plus cleanup; never cut a
  restoration short for the deadline, report the overrun.
- **Cleanup, before releasing the lock.** Stop owned capture and debug clients before opening the restoration flasher or
  running the contract's cleanup, dispose of the source as your role says, establish the pinned restoration image on the
  board (retain the programmed image when verification with the backend's procedure establishes that it matches the
  pinned artifact and nothing since changed its contents or made them uncertain, citing that verification; otherwise
  program the pinned artifact and verify its programmed contents, the artifact hash recorded apart from that result) or,
  when `cleanup.pristine` records the HIL contract's cleanup state, run the contract's cleanup instead, verified only by
  the run-state observation below; remove owned breakpoints and watchpoints (Espressif: `espressif.md` Attach, "Leave":
  `delete`, `monitor resume` before `detach`, `shutdown`), restore the firmware's expected run state and record an
  observation that establishes it (a verified flash alone does not; for a USB device, the expected function, such as an
  application exchange; a claim that needs re-enumeration records the enumeration events, and a device number alone
  establishes neither, since the host may reuse it across a reset), close any flasher you opened, restore host settings,
  release the lock. `restorationFlashes` counts actual programming attempts, a contract cleanup's write included. Run
  state not established is a failed cleanup. Do only the cleanup your own actions or the explicitly assigned recovery
  state call for, establishing ownership before restoring a predecessor's state. Blocked before touching hardware with
  no recovery state assigned, the untouched actions are `n-a`, nothing is flashed and another holder's lock stays. If
  restoration cannot complete, keep the lock where you can, report the exact remaining source, firmware, process and
  lock state, and do not call the board ready.
- **Return.** The tested firmware identity, decisive observations, artifact locations, cleanup state and budget used;
  sampling done by hand on a native probe comes back with its command, and the limits name only what was actually
  unavailable.

## Rig discipline — lock first, always

The board lock rules are Delegated sessions' Lock, for a manual session too: held for the WHOLE session (instrument,
build, flash, capture, GDB). Rigs and benches run many identical probes:

- Select the probe by serial: J-Link `-SelectEmuBySN <uid>`, its GDB server `-select usb=<uid>`, OpenOCD
  `-c 'adapter serial <uid>'`; a probe without one by `adapter usb location` (`espressif.md`, Attach).
- Run on the host that owns the probe.
- One probe serves one client: quit JLinkExe before starting JLinkGDBServer on the same probe.

## Pick the least intrusive technique that can answer the question

Observation can mask the bug: one can change behavior under logging *and* under the debugger. If the bug disappears when
instrumented, that IS a finding (timing-sensitive): move down in intrusiveness, not up.

The Core column says where a technique exists; a row that names a core family does not exist elsewhere, and
`espressif.md`'s "Technique mapping" gives the Xtensa and RISC-V substitutes.

| Technique                          | Core                                                                                          | Intrusiveness                                           | Reach for it when                                                                                              |
| ---------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| PC-sampling (DWT_PCSR polled)      | Cortex-M3 and later; ARMv6-M (M0, M0+, M1) only where PCSR is implemented (`techniques.md`)   | no deliberate halt, no firmware change                  | core wedged/spinning somewhere unknown                                                                         |
| SWO exception trace / hw PC-sample | Cortex-M with the ITM: ARMv7-M, Armv8-M Mainline; SWO pin wired                               | none                                                    | ISR ordering, nesting and duration, a CPU profile, with zero code change                                       |
| DWT data trace                     | Cortex-M with the ITM: ARMv7-M, Armv8-M Mainline; SWO pin wired                               | none                                                    | stream one address's accesses: value + accessor PC                                                             |
| Vector catch                       | Cortex-M (ARMv6-M: HardFault and reset only)                                                  | none until a fault fires                                | crash-shaped wedges — autopsy AT the faulting pc                                                               |
| RAM ring-buffer                    | any; one ring per writing core                                                                | ~tens of cycles per event                               | ISR ordering/timing bugs                                                                                       |
| SystemView                         | any with an RTT path and a timestamp the firmware supplies (`sysview.md`)                     | recorder in the firmware, per event                     | scheduling-shaped bugs: throughput jitter, a task starved or preempted, an ISR too long — not logic/state bugs |
| Log lines (RTT)                    | any with a probe RTT path (J-Link, OpenOCD); none in the measured TinyUSB Espressif builds    | µs per line                                             | logic bugs that survive logging                                                                                |
| Log lines (UART)                   | any                                                                                           | up to ms per line when the write path polls a full FIFO | same, when no debug-probe RTT path                                                                             |
| ETM instruction trace              | Cortex-M with an ETM (optional on ARMv7-M, Armv8-M; none on ARMv6-M), wired to a J-Trace      | trace setup; halts; reflash+reset unless `--attach`     | exact instruction counts, coverage, ISR timing, the history before a fault/hang; confirm J-Trace wiring first  |
| dprintf / conditional breakpoint   | any                                                                                           | halt+resume per hit (~ms)                               | low-rate probes post-wedge; never ISR-rate events                                                              |
| GDB halt / breakpoints             | any; the hardware budget is per chip (`gdb.md`; `espressif.md`, Attach, "Breakpoint budgets") | stops USB service entirely                              | post-mortem state autopsy once wedged                                                                          |

Each technique's recipe, read when you reach for it (files beside this one):

- PC-sampling: `techniques.md`, "PC-sampling" — `pc_sample.py` over J-Link or OpenOCD, reading the histogram, the
  explicit halting fallback for a core without a usable PCSR.
- Probe-state snapshot (DHCSR anchor, PCSR samples, breakpoint budget, symbol reads, region saves, image read-back for
  Cleanup's verification): `techniques.md`, "Probe state" — `probe_state.py`.
- SWO exception trace, hardware PC-sampling, DWT data trace, ITM stimulus: `swo-dwt.md` — `swo_capture.py`,
  `swo_decode.py`; per-board notes in `swo-boards.md`.
- Vector catch and the fault registers: `fault-autopsy.md`.
- RAM ring-buffer: `techniques.md`, "RAM ring-buffer trace" — the ring code, the `nm` check, the GDB dump.
- SystemView: `sysview.md` — `sysview_record.py` (live J-Link recording, decoding a raw capture),
  `sysview_report.py`, `sysview_dump.py` (post-mortem ring); RTT streaming is `rtt.py --channel`.
- Log lines (RTT, UART): `techniques.md`, "Log capture" — `console_capture.py` for a UART; the RTT channel and its drain
  model are the `rtt` skill's.
- ETM instruction trace: `etm-trace.md` — `etm_capture.py`, `etm_profile.py`; per-board notes in `etm-boards.md`.
- Chip and family constraints (flash reads, reset domains, probe misreads): `families.md`; Espressif chips:
  `espressif.md`.
- dprintf, GDB halt and breakpoints: `gdb.md` — GDB servers, OpenOCD batch sessions, the halted autopsy
  (`autopsy.py`), the hardware breakpoint budget, watchpoints, FreeRTOS threads, J-Link Commander quirks.
- USB enumeration or transfer bugs: capture both sides at once by default — `techniques.md`, "USB: dual-side capture"
  (`dual_capture.py`).
- Manuals (J-Link UM08001, OpenOCD, GDB): `techniques.md`, "Manuals".

## Warnings

- **Halting via the probe does NOT disconnect the device**: a halt resets no peripheral, so the USB controller keeps its
  pull-up, the host's stuck URBs stay stuck and a wedged DUT stays wedged — recover the Linux host side (the rig's own
  procedure; its HIL contract names it). A probe reset disconnects only when the reset it issues reaches the USB
  controller or its PHY: check the part (`families.md`: DWC2, STM32F4).
- **A bug that vanishes under logging is not fixed**: timing-sensitive until the ring buffer reproduces it, and the
  added code's own effect on stack, layout and ordering is the alternative to rule out.
- **A logging write path that polls a full FIFO blocks** (worst perturbation, including inside the ISR; which driver
  does what: `techniques.md` "Log capture", `espressif.md` "Console"); RTT is much cheaper but not free; verbose logging
  multiplies both.
- **`pkill -f PATTERN` matches its own caller**: the pattern is in your shell's (or ssh's) command line, so it kills the
  command running it. Bracket one character (`pkill -f '[J]LinkGDBServer'`) or kill by PID; an openocd-esp32 server is
  ended with `shutdown`, never killed (`espressif.md`, Attach).
- Flash/GDB only with the board lock held (Delegated sessions' Lock).
- **Instrumentation is temporary**: before `release`, restore the pinned restoration firmware or cleanup state as
  Delegated sessions' Cleanup says (the next CI run must not inherit a debug build) and revert the instrumentation diff.
  Handing the diff over with the diagnosis preserves the evidence; it does not count as cleanup.
- **A register snapshot without a validity anchor lies**: debug tool sessions can reset or briefly halt the DUT as a
  side effect, and a snapshot of a freshly-reset chip (e.g. NVIC ISER = 0) reads like a smoking gun. Cortex-M: read
  DHCSR (0xE000EDF0: bit 17 S_HALT, bit 25 S_RESET_ST) with every snapshot; Espressif: `espressif.md`, Attach, Anchors,
  since the USB device number survives a core reset there. Cross-check against something the device demonstrably still
  does.
- **"Flash OK" can lie** (silent no-op — old firmware keeps running). When behavior contradicts the flashed code, read
  the image back: `probe_state.py --verify <flashed.elf>` (J-Link and OpenOCD boards, `techniques.md` "Probe state"), on
  RP2040 `rp2040_verify.py` (`families.md`), since `probe_state.py` refuses flash there; on mismatch reflash before
  debugging further. Espressif: `esp_verify.py` over the build's `flasher_args.json` (`espressif.md`, "After an abnormal
  end"), never an objcopy of the ELF, since the flashed image is esptool's `elf2image` output.
- **A marginal link can fake a deterministic firmware bug** — down to failing the same test at the same iteration twice.
  "USB disconnect" in dmesg on a freshly re-cabled port (high devnum = churn) points at the plug before the code: first
  sustained bulk traffic is when a bad contact drops. Before declaring a regression, re-run the OLD build on the SAME
  link state — and if a bisect exonerates every hunk, believe it: re-test the exact failing binary.
- **`dd` creates a regular file when its output device node has vanished** (an 8 KiB file at /dev/sdb after the device
  dropped, 2026-10-16): verify the DUT identity and `test -b` before each raw write, add `conv=nocreat` so a
  disappearance before the open fails instead, and discard every result obtained from a replacement regular file. Bare
  ` sdX:` kernel lines after raw writes are partition-rescan output, not errors.

## Project notes

What is specific to the projects these skills have been used on, one file each, for these skills: their symbols, typical
issues and the behaviour of their code, releases and CI firmware; a project's build, rig and battery procedure stays in
its own contracts. Read it with `families.md`'s section for the board's chip, when it has one. Match the project by its
repository identity (`git remote get-url origin`), not by a directory name:

| Repository         | Notes                 |
| ------------------ | --------------------- |
| `hathach/tinyusb`  | `projects/tinyusb.md` |
| `adafruit/tinyuf2` | `projects/tinyuf2.md` |

Every entry there says how it is known (verified when, on which revision and board, how — or only carried over). Check a
symbol exists in the checkout in front of you before relying on it. When a session establishes something reusable —
reproduced, or read in a source — add it with the same evidence, or correct the entry it contradicts, where it holds:
only for this project, its file here; for any firmware on the chip, `families.md` (Espressif chips: `espressif.md`); for
a probe or tool, the technique file (RTT and trace per-board caveats: the `rtt` skill's `boards.md`, `etm-boards.md`).
Leave out the session's chronology and dead ends. A project with no file: work from its source, and start one when there
is something worth keeping.
