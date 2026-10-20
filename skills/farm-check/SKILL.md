---
name: farm-check
description: HIL farm health - is each board's probe on the bus, does a light image flash, does the board come back and return to its final state. Use for a farm (pool) check or a board/probe health scan on a HIL rig, after rig maintenance, a reboot or re-cabling, or before a HIL campaign, in a repository with a `HIL contract:` line. Never a test verdict.
---

# farm-check — HIL farm health

Read the `hil` skill and the file the repository's `HIL contract:` line names; the
contract names this project's farm-check launcher, its rosters and its image cache. Run
that launcher from the project's checkout: it loads the engine
(`scripts/hil_farm_check.py` of the `hil` skill) from the agentrc the project pins,
together with the project's profile. Never run an installed engine against a project
whose pin is older, or the reverse.

## When

Run the full check by default. Use `--scan-only` (probe presence: no locks, no
flashing, no fetch) only when asked for a quick look, or when `hil_lock.py status`
shows runner holders right now; a suspicion that CI might run is no reason, since the
full check is lock-safe. Say which mode ran and why.

The check takes each board's lock itself (`farm_check`, protected). A held board is
reported `locked` and skipped: never waited on, released or bypassed. A recovery
blocker is reported `blocked` and left to the contract's Recovery. Never kill a running
check: one killed outright leaves its board blocked for a recovery takeover. An
interrupt (SIGINT) ends it after the current board's cleanup.

## Reading the result

- Status: `ok`, `flash-failed` (firmware not delivered, or shown not delivered),
  `failed` (it ran but did not verify), `locked`, `blocked`.
- `coverage`: `probe-only` (scan), `skipped-locked`, `full-attempted`,
  `blocked-before` (refused before any mutation) or `blocked-after` (this check's
  cleanup is unresolved; `check_status` keeps what the check itself found).
- `verification` is the light image, `cleanup` the final state; read them apart. A
  `blocked-after` row names its blocker: a cleanup holder's `token`, which the
  contract's Recovery releases with `hil_lock.py release --token` once the board is
  restored (it refuses unless every process the check started is proven ended; past that
  the Recovery ends the holder itself), or a protected record, which needs a recovery
  takeover.
- Exit 0 every exercised row ok, 1 a failed row or an aborted check, 2 refused before
  any board, 4 any blocked row. `locked` and `--scan-only` rows are unverified, not
  healthy: read the document, never `$?` alone.

## Never a test verdict

An `ok` row says this probe, this image (its digest and provenance are in the row) and
this board's return and restoration worked now. It validates no firmware and no PR. A
persisted report (`--report-dir`: `farm_check.json`/`.md`, its own attempt records) is
farm-health evidence and never joins or replaces a harness report.

## Reporting

Paste the tool's table whole with its footer, never a digest like "27/27 healthy".
Below it only what the table cannot show: the mode when not full, boards not covered
(locked, with their holders), and each blocked board with its token or record and that
its recovery is a separate dispatch.
