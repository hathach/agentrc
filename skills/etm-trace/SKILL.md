---
name: etm-trace
description: Use when logs, GDB or PC-sampling cannot answer and you need instruction-level execution data from a SEGGER J-Trace and Ozone — hot functions by instruction count, ISR timing, on-target code coverage, what ran right before a fault or hang. ETM trace; "No trace clock present".
---

# etm-trace — unattended ETM instruction trace via J-Trace + Ozone

Streams full instruction (ETM) trace from a board wired to a SEGGER J-Trace,
headlessly: no GUI, scripted end to end. Produces hot-function profile, code
coverage, and optionally the raw instruction history.

Which debug skill answers what: `target-debug`'s table.

Use `target-debug`'s DWT PC-sampling for a quick statistical profile; use
this skill for exact counts, coverage, or instruction-by-instruction history.

## Requirements

- J-Trace on USB (`lsusb -d 1366:1020`) wired to the board's trace header;
  select it by J-Link USB **nickname** (e.g. `jtrace`) — never commit
  serials.
- **Physical setup is per-board and exclusive** (one J-Trace, moved between
  boards; some rigs are fly-wired): unless the user just asked for trace on
  this board or your task states it is wired, **confirm with the user** that
  the J-Trace is connected to the target before flashing or capturing.
- Linux, with Ozone found as `ozone` or `Ozone` on PATH or named by `ETM_OZONE`
  (≥ V3.38 for the automation socket) and
  `xvfb-run`; the capture script refuses to run without either.
- Firmware whose init enables the trace pins and trace clock. How to build that
  variant is the project's build contract (`target-debug`, "What the project
  supplies"); what the init is called there, its notes (`target-debug`'s
  `projects/`).
- A board with a reference Ozone project captures with `--jdebug <file>` and
  inherits its device, TIF speed, trace timing/width, core clock, hooks and
  J-Link script (where a project keeps them: its notes); any other
  target uses `--device <J-Link name>` plus `--target-if`, `--tif-speed` and
  `--trace-width`: nothing about the target is assumed. A target whose
  reference defines no `AfterTargetReset`/`AfterTargetDownload` (or has no
  reference) is refused until you pass `--cortex-m-default-hooks` (SP/PC from
  the ELF's vector table) — right for a plain Cortex-M image, wrong for one a
  ROM bootloader must start; `--attach` runs neither hook and asks for none. Verified
  boards: `boards.md` in this skill directory.

## Rig discipline

- One probe, one client: quit interactive Ozone/JLinkExe/GDB on the probe
  first. Kill only processes you started — if it's held by someone else's
  session (check `fuser /dev/bus/usb/<bus>/<dev>`), surface it and ask. The
  capture script uses automation port **19201**, never an interactive Ozone's
  19200.
- Shared rig boards: hold the project's board lock first (its `HIL contract:`
  names the skill that owns it).
- Reference projects are interactive projects — automation never opens them
  (Ozone rewrites project files); the script generates a throwaway project.
- The default capture reflashes and resets the target (`--attach` doesn't).
- A target whose own USB port is on this host stops answering it while halted;
  the host then retries enumeration for ~30 s and a JLinkExe started meanwhile
  can hang ("JLinkExe hung") — wait it out, and never start two back to back.

## Capture and analyze

```bash
# 1. Build with the trace init enabled (the project's build contract says how)

# 2. Capture (all options + defaults: etm_capture.py --help):
python3 <skill dir>/scripts/etm_capture.py \
  --jdebug <reference.jdebug> --elf <elf> --probe jtrace --duration-ms 10000 --out <dir>
#   no reference project: --device <J-Link name> --target-if SWD --tif-speed '4 MHz' --trace-width 4
#     --cortex-m-default-hooks --elf <elf> --probe jtrace [--jlink-script <file>]

# 3. Analyze (hot functions, coverage, hottest lines):
python3 <skill dir>/scripts/etm_profile.py <dir> --elf <elf>
```

Every capture — project firmware or vendor demo — goes through
`etm_capture.py`; extend it when a board needs something new, never
hand-roll Ozone drivers.

Choosing capture flags (semantics in `--help`):
- fresh-boot profile/coverage: defaults (flash + reset + trace from startup)
- a LIVE state established first (for example flash, wait for enumeration,
  then capture): `--attach` — no reflash/reset (flashed firmware must match
  `--elf` and have trace init built in); the default would erase that state
- raw history: `--trace-csv` (~80 MB/1M instructions) — when sequence/timing
  matters, e.g. feeding `--isr`
- stream dies (overflow/unknown-packet): `--no-timestamps`, then reduce the
  core clock (`boards.md`); marginal wiring: sweep `--trace-timing`,
  isolate lines with `--trace-width`. "capture OK" requires nonzero profile
  totals — silence (no trace at all) fails with its own error
- deeper data: `--profile-lines-csv` (hottest lines), `--profile-insts-csv`
  (branch bias), `--sample "expr,.."` (data sampling), `--power` (probe-powered
  targets only), `--os-plugin` (RTOS timeline), `--trace-only` (experimental,
  see Warnings)
- targets without a reference project: `--device` with its interface, speed and
  width, plus `--jlink-script` when the firmware doesn't init the trace pins

First trace on a board — or after any rewiring — is a bring-up, not a plain
capture: follow `boards.md`, "Adding a new board" (vendor example first), and
read it before touching hardware.

Analyzer: `--isr ENTRY[,BODY..] --tick-symbol SYM --tick-hz HZ` gives ISR
min/median/avg/worst from a `--trace-csv` capture with timestamps, calibrated
against a periodic handler the firmware runs at a rate you know (`nm` the ELF
for its name: `SysTick_Handler`, pico-sdk `isr_systick`, ...; a wrong rate
scales every duration); without the pair, timing is reported unavailable.
Disassemble both entries first (`objdump -d`): an `--isr` entry that is also a
loop head splits one invocation into several episodes, and the tick handler
must be short and return to other code between beats
(fast-enumerating boards need a short no-eviction run).

Outputs in `<dir>`: `code_profile.txt` (run/fetch counts + coverage); on
request `itrace.csv`, `profile_lines.csv`, `profile_insts.csv`, `samples.csv`,
`power.csv`; `session.log` / `ozone_console.log` / `jlink.log` as evidence.

## Reading results

- **Load %** = share of instruction **fetches** — Ozone has no per-function
  time; time comes only from itrace timestamps (`--isr`, time-share table).
- ISR timing: sub-µs values are approximate (interpolated timestamps — hence
  the SysTick calibration); instruction counts are exact. Time-share ≫
  instruction-share = stalled/waiting (e.g. slave-mode FIFO at wire pace).
- "Fully covered" needs both branch directions — 100% is not expected from an
  idle run.
- itrace timestamps scale by `VAR_TRACE_CORE_CLOCK` (from the board reference;
  `--core-clock` overrides): ordering is exact, absolute times approximate.
- A ms+ "largest gap" or `Trace overflow detected` beyond the startup burst =
  lost packets — reduce the core clock or trace a quieter phase.
- One `Invalid trace timestamp` line at `Debug.Halt` is a normal decoder
  artifact.
- `itrace.csv is truncated` = the run executed code Ozone has no image of, and
  its export only reaches back to the last such address. Code the ELF says
  startup copies to RAM is read into Ozone's instruction cache for you; boot-ROM
  calls or code built at run time are not — trace a phase without them. A
  profile-only capture survives with a warning: those fetches are missing.
- `Unknown trace data packet … Trace collection stopped!` = stream dead from
  that point (the script exits non-zero): retry with `--no-timestamps`, then
  reduce the core clock.

## Timing

- Capture ≈ `--duration-ms` + 15 s overhead; add ~5 s per 1M instructions with
  `--trace-csv`. Bash timeout: duration + 120000 ms.
- Analyzer: < 5 s for a 2M-row itrace.csv.

## Warnings

- **Trace starts at the firmware's trace init**, not at reset: earlier board-init
  code shows as never-executed and Ozone logs `No trace clock present` — both
  expected. Trace-from-reset needs a SEGGER J-Link script (`.pex`) instead of
  firmware init.
- Never commit capture output (`itrace.csv` can exceed 100 MB) — keep `--out`
  in scratchpad/`/tmp`; `*.jdebug.user` files stay untracked.
- The automation socket can't evaluate symbolic constants (`EXPORT_AS_CSV`):
  the scripts send numeric/plain commands only — keep it that way when
  extending them (UM08025 §6.7).
- Ozone has no `--help`/`--version` — any such probe opens the GUI; check
  with `which ozone` only.
- **`--trace-only` is experimental**: ETM start/stop comparators are scarce
  and erratic — low-rate handler windows may silently not record, adjacent
  instructions leak in, timestamps are invalid across gaps, and the profile
  becomes share-of-traced-stream. Use only for instruction-exact inventories
  of high-rate symbols; for ISR timing use full trace + `--isr`.

## Per-board notes

Every validated board has a row (config: core clock, TRACECLK, width, timing,
physical setup, TODO) and a caveat entry in `boards.md` (same directory) —
**read a board's row and caveat before capturing on it**; new validations add
both. Timing semantics and clock columns are explained at the top of that file.

## References

- Ozone manual (UM08025, automation socket §6.7, project commands §7):
  <https://www.segger.com/downloads/jlink/UM08025_Ozone.pdf>; the installed
  Ozone ships its own revision of it under its `Doc/` directory (offline
  fallback — section numbers can differ between revisions).
- J-Link / J-Trace manual (UM08001, trace ch. 10, timing troubleshooting):
  <https://kb.segger.com/UM08001_J-Link_/_J-Trace_User_Guide>
