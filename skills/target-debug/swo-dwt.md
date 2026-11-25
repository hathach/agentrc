# SWO and DWT trace — hardware-timed, zero code change (Cortex-M)

Part of the `target-debug` skill (its technique ladder says when to reach for these; its Delegated sessions, Rig
discipline and Warnings apply). Cortex-M only: ARMv7-M and Armv8-M Mainline for DWT trace and SWO (no ITM without the
Main Extension, Armv8-M ARM book 6539 p1708), absent on ARMv6-M; absent on Xtensa and RISC-V (Espressif's analog is
apptrace, `espressif.md`). Needs the SWO pin (TRACESWO) wired to the probe.

With SWO wired, the core's DWT and ITM emit packets while it runs: **exception trace** (every interrupt entry, exit and
return, with local timestamps: ISR order, nesting and duration), **hardware PC sampling** (a histogram without polling
`DWT_PCSR`), **data trace** (every access to a watched address: value, accessor PC or address offset) and the
firmware's own **ITM stimulus** writes. Two scripts beside this file do it, `swo_capture.py` and `swo_decode.py`; their
docstrings are the full contract. They import `probe_state.py` beside them, which loads the repository's `lib/`: on
the rig host run them from an agentrc checkout, such as a project's pinned `lib/agentrc`, not from copied files.

## Capture

```bash
S=<skill dir>/scripts
# what the core, its DWT and its trace unit support; writes nothing
python3 $S/swo_capture.py --hil-config <roster.json> --board <name> [--interface swd --speed 4000] --caps

python3 $S/swo_capture.py --hil-config <roster.json> --board <name> --interface swd --speed 4000 \
  --traceclk-hz 168000000 --swo-hz 1500000 --clock-source "RCC readback: HCLK 168 MHz" --protocol uart \
  --duration-ms 4000 --setup stm32f4 --exc --timestamps --core-hz 168000000 --itm-ports 1,2 \
  --watch addr=my_var,access=w,emit=val --elf fw.elf --out /tmp/swo-run
python3 $S/swo_decode.py /tmp/swo-run --elf fw.elf          # --jsonl events.jsonl, --json, --top N
```

- **Connection** comes from the project's HIL roster (`--hil-config/--board`), as `pc_sample.py` reads it, or from
  explicit flags: a J-Link by `--probe --device --interface --speed`, an OpenOCD probe by `--probe --cfg`. The roster
  file is the one the HIL contract's Hosts/rosters names for the host; pass it even on the rig host. `--speed` is the
  SWD clock in kHz (4000 on every validated board). A roster entry flashed over `stlink` has no OpenOCD
  configuration: pass `--cfg`. An OpenOCD connect also needs `--accept-attach-writes` (Ownership and restore). The
  board is held for the whole session by the hold command of the HIL contract's Reservation, released by you at the
  end (Delegated sessions); the script takes no lock.
- **`--elf`** is the running firmware's ELF, optional: its read-only sections are compared with the target before any
  write (a mismatch is refused), which makes `--watch` symbols trustworthy, names functions in the decode and lets
  `--restore` prove the firmware is unchanged. Without it a capture still runs and restores itself; only a run killed
  outright (kill -9) then has to be restored by hand from the original values in `journal.jsonl`. Know what runs: the
  validated captures used firmware built and flashed in the same session (`swo-boards.md` names it per board); a
  board's resident image is unknown unless its build is.
- **Clocks have a provenance**: `--traceclk-hz` and `--core-hz` come from the firmware's clock configuration (its BSP,
  or the clock registers read over a probe connection that does not halt the core; J-Link Commander halts it), never
  from a board table alone. A known periodic exception (TinyUSB BSPs run SysTick at 1 kHz) checks the timestamp clock
  afterwards: a consistency check, not an independent calibration.
- **`--take-itm` or `--observe-itm`**: `--caps` shows the ITM and DWT state as found without refusing. A capture of
  your own sources (`--exc`, `--pc-sample`, `--watch`, ports) on a target where that state is not zero needs
  `--take-itm`, which reconfigures for the capture and restores afterwards; `--observe-itm` only records what the
  firmware already emits, unchanged. Leftovers from an earlier probe session are common (J-Link leaves ITM_TCR
  0x10009).
- **Backends**, chosen by measurement: a J-Link runs `JLinkGDBServerCLExe -nohalt`, streaming from its raw SWO port
  while registers go over GDB's remote protocol (JLinkExe's `SWORead` returns 256 bytes per call: no sustained
  capture); OpenOCD runs as a server, its trace unit object writing to a TCP port while registers go through its Tcl
  port (a `sleep` inside one OpenOCD command chain loses bytes). The core runs throughout.
- **Clocks are never defaulted.** `--traceclk-hz` is the trace unit's input clock, `--swo-hz` the pin rate; their
  divisor must be exact, or `--allow-approx-swo` records the rate actually used. A J-Link makes only `base / n` rates
  (`SWO GetSpeedInfo`; the on-board J-Link of an STM32F4 Discovery: 4.5 MHz / n) and silently rounds others, so both
  sides must divide; the script refuses otherwise and reads the prescaler back after the probe tool programmed it.
  `--core-hz` is the processor clock that counts local timestamps (ITM_TCR.SWOENA stays 0). Say where each clock came
  from in `--clock-source`; the decode prints it next to every duration.
- **`--setup <entry>`** applies and checks a family's own requirements from `scripts/swo_boards.json` (DBGMCU trace
  enables, the trace funnel, the TRACESWO pin's mux), with their evidence in `swo-boards.md`. A requirement that
  does not hold is refused, never fixed behind the firmware's back: a pin the firmware remapped stays its. A
  register narrower than a word gives its `bits` (accessed at that width) and any write `key`. A part whose TPIU
  stalls the bus while its trace clock is stopped names its J-Link `jlink_device` prefix and marks the write that
  starts the clock `gates_trace_unit`: the J-Link route then never touches that TPIU while the clock is stopped and
  refuses a capture without the entry (RA6M5: one such read wedged the debug port until power-off, `swo-boards.md`).
  Only that J-Link route knows the part: on such a part run `--caps` and captures over a J-Link with its own
  `-device` name; OpenOCD, or a generic `-device` such as `Cortex-M33`, reads the TPIU unguarded.
- **Output**: `DIR/swo.bin` (raw stream), `DIR/capture.json` (configuration, clocks and their source, sources enabled,
  whether byte 0 is a packet boundary, before and after register snapshots, byte count, teardown offset, the
  capture's `errors` and the `restore` outcome apart), `DIR/journal.jsonl` and `DIR/probe.log`. A refused run leaves
  only `probe.log`, and the same `--out` can be reused.
- **`--formatter`** (J-Link, core TPIU) switches the TPIU formatter on, TPIU_FFCR.EnFCont written after the probe
  tool's start, which bypasses it, and restored with the rest: the stream is then CoreSight formatter frames
  (Architecture Specification IHI0029, book 6591, D4), and the decode takes the ITM's bytes by its trace bus ID
  (ITM_TCR.TraceBusID, 1 in a configured capture) and counts frames, syncs, lost frames and other sources' bytes,
  which it does not decode (an ETM's, say). The formatter took 21% more of the pin on stm32f407disco (126,248 bytes
  for the fixture's 104,330 ITM bytes): besides the aux byte, the frames averaged 1.7 ID bytes. The STM32H7 SWO
  unit has none (RM0433 60.5.7); OpenOCD's trace unit object owns the register and is refused.
- **`--rtt`** (J-Link only) has the same GDB server serve RTT channel 0 (`-RTTTelnetPort`, SEGGER KB book 6586) and
  logs it to `DIR/rtt.log`, the server's banner removed by the `rtt` skill's filter: the firmware's text beside its
  trace over one probe connection. The log starts with what the target's ring held when the capture connected, so it
  may begin before the trace does: match its lines to ITM markers by content, not by time. A server that closes the
  RTT socket early fails the capture, which still restores. On an OpenOCD probe use the `rtt` skill.

## Ownership and restore

Before any write the script reads ITM_TCR, ITM_TER, DWT_CTRL and the comparators. ITM or DWT trace already on belongs
to someone else, firmware, another session or a leftover, and the capture is refused (exit 2) unless the caller
chooses: `--observe-itm` keeps the ITM as found and captures what it emits, `--take-itm` reconfigures ITM and
DWT_CTRL for the capture, dropping DWT sources it was not asked for; a comparator with FUNCTION != 0 is taken only by
`--take-comparator N`. None of these bypasses the board lock. Observing needs a probe start that leaves the ITM as
it is: over a J-Link the firmware's ITM_TCR must already be the 0x1000D `SWO EnableTarget` writes (else refused), and
a trace unit already running NRZ must run at the requested rate.

Every register the script writes is journalled before the write, and before the probe tool's SWO start and stop
every register that operation may write, each read back after; each goes back to its value before the first change,
the first changed last, on success, error, SIGINT and SIGTERM, and a read-back that differs fails the run (exit 1).
At the end all sources stop at once (the capture's comparators disarmed; ITM_TCR.ITMENA and TXENA cleared together:
TXENA gates the DWT's packets, ARMv7-M ARM C1-854) and drain while the probe still listens; overflows after that point
are reported apart. A comparator is disarmed while its address and mask change, entering and leaving. A stream the
probe tool closes early fails the capture.

A killed run leaves its journal: `--restore DIR` (same host and connection, board held) checks the CPUID and the
firmware against the `--elf` segments the capture checked, then restores each register whose value the journal
explains since that register's last restore, and reports any other value untouched, including one changed during an
interrupted probe tool operation. Without an `--elf` at capture it refuses: restore by hand from the `original`
values in `journal.jsonl`. A register it cannot reach, a failed access or a trace-clock check that fails included, is
reported and left alone, and the rest is still restored (exit 1). A gated TPIU entry left so stays out of reach once
the gate register is back at its original: restore it by hand with the trace clock running. On Linux the probe server
dies with the script (`PR_SET_PDEATHSIG`).

What the probe tools do on their own, measured: J-Link's `SWO EnableTarget` sets DBGMCU_CR.TRACE_IOEN on an STM32F4,
ITM_TCR to 0x1000D and ITM_TER to its port mask (it refuses mask 0), and the TPIU's ACPR, SPPR (2) and FFCR (0x100);
`DisableTarget` clears only ITM_TCR and ITM_TER. Both are journalled; other parts' vendor registers a J-Link may write
are not known to the script. OpenOCD's connect examines the core before any command of the script runs: it sets
DEMCR.TRCENA, clears every DWT_FUNCTION and FPB comparator, rewrites ITM_TCR and ITM_TER on builds before upstream
deferred that (0.12.0+dev-g0ce743125 among them), and runs the target configuration's examine-end handler
(`cortex_m_examine`). A firmware's ITM, comparators and breakpoints are gone before a capture can see them, so an
OpenOCD connect needs `--accept-attach-writes` (`--caps` and `--restore` included) and cannot observe. A system reset
leaves DWT_CTRL and the ITM as they were (measured; only a power-on reset clears them): a capture that is not restored
outlives the firmware's next boot.

`capture.json`'s `restore` and its after-snapshots are the script's own record. A `--caps` before and after is an
independent spot-check of the ITM, DWT_CTRL and the trace unit only: it reads no comparator and no setup register
(PRCR, TSG_CNTCR), and over OpenOCD its own connect rewrites state.

## Reading the decode

- **INTEGRITY first.** An overflow, an unknown header or a packet from a source the capture never enabled ends a
  segment: data was lost there, episodes never pair across it, counts are lower bounds and the timestamp span is
  withheld (an overflow loses local timestamp time). A stream whose byte 0 was not a packet boundary is read from its
  first synchronization packet (DWT_CTRL.SYNCTAP, every 2^24 cycles).
- **Durations** come only from entry and exit packets whose local timestamps were synchronous (TC 0); a handler
  shorter than its packets' transmit time gets delayed timestamps and is counted, not timed. Self time subtracts
  nested episodes and is withheld when one of them was untimed.
- **Bandwidth is per burst, not per second.** At a 1.5 MHz pin with timestamps a byte takes 6.7 µs; an exception
  entry is about 3 bytes plus 1-4 of timestamp. Two interrupts close together, or a stimulus burst near one, overflow
  the ITM although the average rate is low: lower the sources (one capture per source), raise the pin rate, or space
  the events. A FIFOREADY-polled stimulus write never overflows but holds the ITM, so the DWT's packets behind it do.
- **PC sampling**: `--pc-sample CYCTAP:POSTPRESET` samples every (POSTPRESET+1) x 64 or x 1024 cycles; a sample is 5
  bytes, so 1:15 at 168 MHz (10.3 k/s, 51 kB/s) fits a 1.5 MHz pin and 0:0 floods any. Sleep samples count WFI time.

## DWT data trace encodings (ARMv7-M)

`--watch addr=ADDR|SYMBOL,access=r|w|rw,emit=...` programs one comparator from ARMv7-M ARM Table C1-21 (DATAVMATCH 0),
every encoding below verified through `swo_capture.py` on a Cortex-M4 (stm32f407disco):

| access | emit `val` | `pc` | `pcval` | `offset` | `offval` |
|--------|------------|------|---------|----------|----------|
| rw     | 0x2        | 0x1  | 0x3     | 0x21     | 0x22     |
| r      | 0xC        | —    | 0xE     | 0x2C     | 0x2E     |
| w      | 0xD        | —    | 0xF     | 0x2D     | 0x2F     |

Read-only and write-only comparators exist: a variable the main loop polls need not flood the pin. `mask=N` ignores
the address's low N bits; a comparator implements fewer than 31 (an STM32F407 reads back at most 15), and a larger
one fails the capture's read-back. The comparator stays disarmed after the capture.

## DWT data trace encodings (Armv8-M)

Armv8-M redesigns DWT_FUNCTIONn as MATCH[3:0], ACTION[5:4] and DATAVSIZE[11:10] (Armv8-M ARM book 6539, D1.2.64,
Tables RCNHN and RPSBJ). `--watch` takes a data address comparator for `pc` and a Data Address With Value one for
`val` and `pcval`, matching `size=1|2|4` bytes at an address aligned to it, by default the size of a 1, 2 or 4-byte
ELF symbol:

| access | emit `val` | `pc` | `pcval` |
|--------|------------|------|---------|
| rw     | 0x2C       | 0x34 | 0x3C    |
| r      | 0x2E       | 0x36 | 0x3E    |
| w      | 0x2D       | 0x35 | 0x3D    |

plus DATAVSIZE (0, 1, 2 for 1, 2, 4 bytes) at bit 10. A read-only or write-only `pc` exists here. There is no DWT_MASKn:
`mask=`, `offset` and `offval` need a linked comparator pair, which the script does not program, and are refused.
`--cycmatch` writes MATCH 0b0001 with ACTION 0b11 (0x31) on comparator 0. A comparator is used only when its
DWT_FUNCTIONn.ID names the match (data address for a watch, cycle counter for `--cycmatch`); `--caps` lists each one's.
A comparator in a linked pair, a limit or linked value comparator (MATCH 0b0011, 0b0111, 0b1011) and the one before it,
is never used or taken, and a restore leaves alone one that has joined a pair since: changing one changes what the other
matches (B14.2.5). A DWT whose DWT_CTRL.NOTRCPKT is set sends no trace packets, and every DWT source is refused there.

Reading DWT_FUNCTIONn clears its MATCHED bit on both architectures, so `--caps` and a capture's first read consume a
match the firmware or another debugger had not yet seen.

The decode reads two packets ARMv7-M does not send: a Data Trace Match packet (`match` in DATA; it also stands in for a
PC that Secure state does not let be traced, p359), and Short and Medium PC Value packets carrying PC bits [7:0] or
[15:0], whose upper bits are DWT_COMP<CMPN>'s (F1.2.3, F1.2.4): it completes them from the comparator address in
`capture.json`, and prints the bits alone for `--bin` input.

## Feature matrix

Verified means a run of `swo_capture.py` on the named board against the SWO fixture, decoded to its known rates
(`swo-boards.md`), unless the row names other firmware; the encodings table above was also checked by hand on a
Cortex-M7.

| Feature                                                   | Implemented | Verified through swo_capture.py                     |
|-----------------------------------------------------------|-------------|-----------------------------------------------------|
| NRZ SWO, J-Link GDB server                                | yes         | stm32f407disco                                      |
| NRZ SWO, OpenOCD server                                   | yes         | stm32h743nucleo                                     |
| Exception trace with local timestamps, ITM stimulus ports | yes         | both boards; unmodified `cdc_msc` on stm32f407disco |
| Hardware PC sampling                                      | yes         | both boards                                         |
| Data trace, all 13 access/emit encodings                  | yes         | stm32f407disco; `w,val` both boards                 |
| Data trace `mask`, several watches at once                | yes         | stm32f407disco                                      |
| `--observe-itm`                                           | yes         | stm32f407disco (run and refusal)                    |
| `--take-itm`, `--take-comparator`                         | yes         | both boards; comparator stm32f407disco              |
| `--setup` writes and checks (incl. PB3 TRACESWO)          | yes         | both boards; PB3 refusals stm32f407disco            |
| `--restore` after kill -9                                 | yes         | both boards                                         |
| Cleanup on error and SIGINT                               | yes         | stm32f407disco (MASK read-back error, SIGINT)       |
| DWT profiling counters, cycle-count events                | yes         | stm32f407disco                                      |
| CYCMATCH                                                  | yes         | stm32h743nucleo                                     |
| Global timestamps                                         | yes         | stm32h743nucleo (TSG behind AP2)                    |
| Armv8-M SWO, trace clock gate                             | yes         | ra6m5_ek (fixture: exc, ITM, failed gate check)     |
| Armv8-M data trace (`--watch`, `--cycmatch`)              | yes         | ra6m5_ek (`w,val`, `r,pc`, `rw,pcval`, CYCMATCH)    |
| RTT beside SWO (`--rtt`, J-Link)                          | yes         | stm32f407disco                                      |
| TPIU formatter (`--formatter`), ITM through it            | yes         | stm32f407disco                                      |
| Manchester SWO, parallel trace, CMSIS-DAP                 | no          | deferred: no farm probe captures them               |

The deferred rows need a probe the farm lacks. J-Link software captures UART (NRZ) SWO only (SEGGER KB, book 6588), and
OpenOCD's ST-LINK driver refuses any other mode; the farm's CMSIS-DAP probes are Raspberry Pi debugprobes, built with
SWO_UART and SWO_MANCHESTER 0 in every release, and OpenOCD lists no SWO on them. The Cortex-M4 TPIU itself supports
Manchester (TPIU_DEVID). A CMSIS-DAP probe with SWO support wired to a target's TRACESWO would cover both.

## Known faults

- STLINK-V3 through OpenOCD on stm32h743nucleo loses bytes: at 16 MHz near 2 KiB-multiple stream offsets (the decode
  breaks 0 to 5 bytes past one; far more often on firmware J17 than J12M3), and with exception trace and PC sampling
  together at all three rates tried (4, 8 and 16 MHz). The decode reports each as an unknown header or an unexpected
  source and resynchronizes; the counts and what was ruled out are in `swo-boards.md`.
- A periodic DWT packet due while another is output comes out after it and can lose its own Local timestamp, taking
  the next one (ARMv7-M D.2.4, p937): on stm32f407disco a Cyc event due at a TIM7 entry arrived one period late,
  sharing the following event's timestamp. `periodic_missed` counts each source's packets over its run in a segment,
  so a late packet is not counted lost; it flags packets lost without an Overflow packet.
- The 8-bit profiling counters wrap fast: `sleep`, `cpi` and `lsu` on a busy core outrun a 1.5 MHz link (about 75
  thousand overflows in 4 s on stm32f407disco); enable one at a time, or use `exc` and `fold`, which wrap rarely.
- Halting the core stops SysTick but not peripheral timers unless their DBGMCU freeze bit is set: phase relations the
  firmware set up are gone after a halt; reset before a timing capture.
- On an STM32F407 debugger reads of DWT_CYCCNT returned one value while the core ran (JLinkExe and the GDB server
  alike) although sync packets showed it counting: do not time CYCCNT from the host there.
