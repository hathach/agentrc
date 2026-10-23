# SystemView decoder references

These are real capture slices, not generated packet streams. `fixture.json` gives the original corpus path, byte
interval, uncompressed SHA-256, GUI row count and retained row indices. `capture.SVDat.gz` is lossless gzip with a
fixed mtime. The small cases retain every GUI row. Overflow retains initialization and the loss/recovery tail;
timer-wrap retains initialization and the boundary tail, with the complete raw prefix needed to reconstruct time.
Checked-in event CSVs use LF endings and omit the GUI's trailing space after the last hex byte.
Where present, `contexts.csv` and `report.json` cover the complete slice, not just the selected event rows. Report
JSON was produced from the GUI exports without `record.json`, so GUI dialog warnings do not affect fixture comparisons.

| Case            | Origin                        | Coverage                                                     |
| --------------- | ----------------------------- | ------------------------------------------------------------ |
| baremetal       | STM32F407 OpenOCD RTT          | ISR and user function pairs, no OS                            |
| freertos        | STM32H743 OpenOCD RTT          | Task creation, ready/run/block, scheduler, interrupted idle    |
| overflow        | STM32F407 GUI live recording   | A genuine overflow and recovery; incomplete spans             |
| wrapped         | CH32V307 post-mortem ring      | Mid-packet head, repeated sync/Init, user functions            |
| truncated       | STM32F407 post-mortem tail     | Final packet missing its timestamp byte                       |
| timer-wrap      | STM32H743 OpenOCD RTT          | Accumulated time crosses 2^32 ticks; also contains real losses |
| corrupt-content | MAX32666 OpenOCD RTT           | Valid framing with corrupt payload content; TinyUSB #4294     |

The corrupt-content fixture retains the original bytes through offset 3168 and selected GUI rows. Offset 1688
contains `02 03 92 03 02 01`; these bytes still form legal packets. At offset 3156, `1C 03 E1 01 B1 02` has a
three-byte payload ending in an incomplete varint. The next packet starts at 3162. Native decoding records that
payload error, withholds its arguments, and keeps the next boundary and its valid timestamp delta. No GUI aggregate
is supplied for this prefix; tests compare packet boundaries, identities, timestamps and payload diagnostics.
The full runner treats this known corrupt-wire capture as a robustness case, with informational GUI differences.

The timer-wrap fixture cannot be trimmed below 50 KB while preserving a contiguous real capture with Init and the
same time origin. Its only Init is at byte 12. The first wrap-crossing row is 218401, offset 1634449, timestamp
4295596724. The minimum prefix ends at 1634452 and compresses to 357416 bytes, just 16 bytes smaller than the retained
fixture. Removing intermediate packets would remove their timestamp deltas. `fixture.json` records these bounds;
the test independently finds the first crossing and verifies the only Init and minimum prefix.

The corpus has no early-crash/no-Init capture. Unit tests construct that case, malformed varints, unknown payloads,
resynchronization, markers, heap/stack records and nested interrupts; they do not establish hardware coverage for
those cases. The new decoder has no board-facing operations. Linux GUI exports ran on ci.lan; macOS/Windows native
routing is tested with platform mocks, not on native OS installations.

## Full corpus

The durable working copy is outside the repository:

```text
/home/hathach/.cache/agentrc-sysview-followups-2026-10-23/corpus/
/home/hathach/.cache/agentrc-sysview-followups-2026-10-23/corpus-manifest.json
```

`corpus.json` is the checked-in copy of that manifest. It includes SHA-256 and sizes for the original captures,
GUI exports, campaign reports and dump metadata. Capture provenance is the ci.lan campaign on 2026-10-22/23 and
TinyUSB's `SYSVIEW=4` cdc_msc recordings. The original `record.json` and `dump.json` files retain the commands and
capture metadata. The H743 export, initially absent, was generated on ci.lan with SystemView 4.10b on 2026-10-23.
The `trimmed/` directory in the working copy retains the full GUI exports of the fixtures, including rows omitted
from the repository. Fixture GUI exports used the same version; `wrapped` reuses the byte-verified original export
because a fresh GUI process crashed during startup. These recordings are not hosted at a public download URL.
The MAX32666 pair was copied read-only from
`ci.lan:~/sysview-followups/max-out/run1/{max32666fthr-capture.SVDat,max32666fthr-decoded/}` on 2026-10-23.

Retrieve from that maintained cache (or copy it from the dev host using the same absolute path), preserving the
relative paths; do not substitute a later recording with the same board name. To reproduce:

```bash
python skills/target-debug/scripts/sysview_corpus.py \
  --root /home/hathach/.cache/agentrc-sysview-followups-2026-10-23/corpus \
  --manifest tests/fixtures/sysview/corpus.json --out /tmp/sysview-corpus-diffs
```

`--out` must be new or empty. The runner verifies the manifest and every GUI event's `eventoffset`, `eventsize` and
`eventdata` against the raw file before decoding. It writes one JSON diff per capture and `summary.json`, prints one
summary per capture, and exits 1 on any unexplained difference or failed robustness check. A mismatched pair or
unavailable/changed file is an error (2), never a pass. Captures without a GUI export are listed explicitly and cannot
contribute differential coverage. Repeat exports of the F407 post-mortem capture are checked separately.
An optional `--baseline-decoder FILE.py` runs a previous decoder and reports `fixedCount` and `fixedDifferences`:
previously differing field paths that now compare equal. This does not count newly decoded rows as fixed fields.

Normalization excludes display timestamps and sequence numbers, whose integer timestamp and packet offset are
already checked. It retains context identities, packet bytes and the built-in event details the report consumes.
OS-specific API rendering for event IDs 32–511 is excluded. User call/return details normalize to call or
return plus duration, with unannotated GUI returns timed from matching timestamps as `sysview_report.py` does.
Duration annotations use the GUI's nanosecond half-up rounding below 1 ms and truncated microseconds above it.
All original differences remain in the per-capture JSON with a classification, reason and evidence. `equal` means
strict equality; `accepted` means no unexplained differences and passing robustness checks when applicable.
A report-level justification requires an exact result after recomputing the GUI report with proven row corrections
or independently accounting for the intervals.

- `gui-export-artifact`: the exact outgoing context `0x0000002000000000` at zero-based row `N*1000-1`, when the raw
  packet and adjacent/context identities prove another outgoing context. Return text may repeat that proven
  artifact. Separately, exported packet-byte fields capped at exactly 512 bytes must equal the raw frame prefix.
  Opposite display rounding is accepted only at an exact half-nanosecond midpoint derived from packet deltas.
- `loss-withheld`: an execution annotation ends at overflow/capture end, or a ready interval crosses an overflow,
  with the exact endpoint ticks matching the GUI annotation. Unreadable deltas and frequency changes are verified
  from raw framing; the runner recomputes reports with those clock-loss boundaries. Bounded malformed payloads
  with readable deltas preserve timing. For CPU load, it sums completed ISR intervals in
  enclosing task runs later discarded at overflow, verifies the GUI runtime/interruption totals, removes exactly
  those intervals, and requires the resulting CPU percentage to equal native output.
- `gui-inconsistent`: an already active task's GUI ready latency contradicts the time to its next Task Run, or its
  context statistics disagree with independent interval/activation sums from its own context identities. The JSON
  includes row intervals, activation rows, frequency and the exact recomputed value. No aggregate is excused merely
  for being odd; ambiguous duplicate context names disable aggregate proofs.
- `gui-warning`: removing GUI error-dialog warnings, after any proven loss accounting, gives exactly the native
  warnings. Other row or report differences do not prevent this classification.
- `withheld-undefined`: native blocked time is null and `decode.json` has the verified source/OS evidence for
  withholding it. The runner checks the OS named in the raw stream and includes observed Task Stop Ready reasons.
- `informational`: GUI diffs for an explicitly hash-listed `mode: "robustness"` capture. This mode checks every
  packet offset and complete packet bytes, every malformed-payload and framing diagnostic, withheld arguments,
  absence of resynchronization, reconstructed timestamps, clock-loss boundaries, loss reporting, and report timing
  against the valid prefix. Missing diagnostics or packets fail even when GUI differences are informational.
- `unexplained`: every other difference, including a report difference whose proposed accounting does not close.

## Boundaries and remaining limits

Task blocked time is deliberately withheld. For jl-wrapped cdc, the previous interval sum was 729886 ticks:
5787 in ISR interruptions, 693604 in scheduler windows, 29454 while preempted until resumption, and 1041 from scheduler
entry to the Task Block naming cdc at row 5251.
The GUI total corresponds to 859427 ticks at 168 MHz, leaving 129541 ticks unaccounted for. Summing ready-to-run
latencies or assigning all loss windows to the preceding task does not close the clean or lossy totals.

The installed `/opt/SEGGER/SystemView_V410b/Description/SYSVIEW_FreeRTOS.txt`, read over SSH without modification,
has SHA-256 `bed4b8dd7842b252ef4a371a6df28d7114b335da5c6ea0df1ef305e2ab5b7da0`. It defines mask 0xFF,
state 4 = Delayed, state 27 = Suspended, and no `PreemptingTaskStates`. All Task Stop Ready reasons are already 4:
243 in jl-wrapped, 2929 in f407-live, 1839 in metro-live, 4509 in f407-oocd, and 10359 in h743-oocd. The table therefore
adds no interval rule that closes the gap. Native rendering uses only those labels and mask; no vendor file is
copied into the repository. The speculative blocked-time accumulator has been removed.

The runner accounts for oc-wrapped's remaining runtime/activation differences. Its GUI rows contain 255 SysTick
activations and positive runtime, while `contexts.csv` says 252 and zero. The independent sums reproduce the native
statistics, with every contributing interval retained in the diff JSON. F407 OpenOCD's CPU-load difference also
closes: 157 ISR spans in task runs later lost at overflow contribute 307148 ticks; removing them changes 0.34% to
0.35%. The complete GUI interruption total is 532028 ticks and the retained total is 224880 ticks at 168 MHz.

MAX32666 retains all 16626 packet offsets and diagnoses 60 malformed bounded payloads, 23 padding bytes and its
truncated tail. Every complete packet's delta parses, so all complete rows retain timing; only dependent spans are
withheld. A report warning names the malformed-payload count. A high unsigned delta is not treated as a negative
delta without evidence: the wire format permits that value. Context identities after corrupt transitions remain
different from the GUI's, and are informational only for this hash-listed capture.

The small truncated fixture ends inside a timestamp delta. Its valid-prefix timestamps/metrics are unchanged, with
one additional timing loss and warning. The full cut2 capture ends inside a payload; no complete row loses timing.
Wrapped captures keep timing from their first synchronization point. No current packet re-anchors a lost exact
recording clock: Init has frequencies only, cycles wrap, and `RecordSystime` samples OS/cycle time before the packet
timestamp. The decoder preserves the measured prefix rather than guessing the sampling offset or missing wraps.

Tests require exact report values after the documented blocked-time/loss treatment and selected normalized rows in
each fixture category. Full-corpus acceptance is recorded separately by the runner. Early-crash/no-Init hardware
coverage remains missing; native macOS/Windows execution remains unrun.

## Source and claim record

Packet implementation: TinyUSB's pinned `lib/SystemView`, commit `92ca7a810c5765ba64911919acd511c61b6b083f`,
`SYSVIEW/SEGGER_SYSVIEW.c`, `.h`, `_Int.h`, and `_ConfDefaults.h`. The header exposes compatibility version 3.32;
its comment explicitly says that this is not the target-source release number. Encoding evidence is
`ENCODE_U32`, `_EncodeStr`, `_SendPacket`, `_SendPacket_Ex`, `_TrySendOverflowPacket`, `_SendSyncInfo`,
`SEGGER_SYSVIEW_RecordSystime`, the task/ISR/timer recorders, and heap extension recorders. The decoder does not
change target code, register access, synchronization, timestamps or capture transport.

Calibre book **2464**, *SEGGER SystemView User Guide*, UM08027, software 4.10, revision 0, 2026-03-30:

- **Verified:** Init's timestamp frequency defines event time units; source timestamps can use fewer than 32 bits.
  Sections 4.5.1.1–4.5.1.2, pp. 75–76, lookup `6315c3f4833d`; title/revision lookup `6b778631644a`.
  `_SendPacket_Ex` masks target deltas before encoding, so the host adds transmitted deltas without wrapping them.
- **Verified:** A post-mortem ring overwrites old records and can be saved starting at its chronological tail.
  Section 3.15.3, pp. 58–59, lookups `3e3432a52af3`, `9e3f1e6a99eb`. `_SendSyncInfo` supplies the ten-zero delimiter;
  discarding bytes before it avoids guessing a boundary in a partial packet.
- **Verified:** Task stop-ready, idle, ISR return and ISR-to-scheduler have distinct meanings.
  Sections 6.1.1.4–6.1.2.2, pp. 140–141, lookup `aa202f336a23`. Native frames distinguish the interrupted context
  from the scheduler and leave the running task unchanged when another task is blocked.
- **Verified:** Heap allocation/deallocation loss invalidates the heap model. Section 3.8, p. 40, lookup
  `f8224daf5f92`. Native decoding clears prior running totals on loss or an unmatched free rather than reporting a
  stale heap total as the final state.
- **Verified:** Context statistics distinguish active, interrupted and ready-but-inactive time. Section 3.6, p. 38,
  lookups `3189cc2fa225`, `33aae70fc268`. **Pending:** exact GUI total-blocked aggregation for the FreeRTOS cases above.
  Sections 6.1.5.2–6.1.5.3, pp. 145–146, lookup `e7e7e48cf0c4`, describe OS-specific state mappings but do not
  resolve the observed aggregate discrepancy. It must not be described as verified GUI parity.
- **Verified:** The context statistics distinguish total blocked time from suspended time and itemize blocking by
  tasks, ISRs and the scheduler. Context details, pp. 44–45, lookup `6bc68cf5ccc9`. OS task-state mappings and
  `PreemptingTaskStates`, pp. 145–146, lookup `a4e7bbea6043`, do not specify enough to derive the unmatched totals.
  Lookup `ed08d75f8ddf` verifies that preemption states determine blocked-time calculation; the installed FreeRTOS
  description above supplies none. Withholding that statistic preserves the distinction without inventing a rule.
- **Verified:** `pfGetTime` supplies uptime in microseconds; without it the GUI shows recording-relative time.
  Section 6.1.3.1, p. 143, lookup `9567d72ea7b2`; `RecordSystime` sends a U32/U64 sample, section 7.6.13, p. 218,
  lookup `1e24d8651fd2`. The pinned source samples that value separately from `_SendPacket_Ex`'s timestamp.
  **Not established:** an exact packet-clock offset after loss; native decoding does not re-anchor from these samples.
- **Verified:** ISR-to-scheduler records request a scheduling transition when an interrupt causes a task switch.
  Section 6.1.2, pp. 141–142, lookup `c887eeace880`. Native decoding retains that request until the outer ISR exits;
  the MAX32666 GUI rows and synthetic nested-interrupt tests check that behavior.

A separate synthetic GUI probe on ci.lan confirmed that context IDs retain their encoded ID even with ID Shift=2,
while heap sizes expand by that shift and include allocation metadata. This probe is not a hardware corpus case.
A second probe is retained under the cache's `analysis/blocked-synthetic/` directory. Its `wake` task has 100 us
ready-to-run latency and a 300 us scheduler interval after blocking; the GUI reports 300 us total blocked time,
but 100 us minimum/maximum blocked time. Its `sched` task excludes a 30 us Task Stop/Run scheduler interval from
total blocked time. These observations rule out equating that total with summed ready-to-run or preemption time;
they do not establish a replacement aggregation rule.
