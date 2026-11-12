---
name: hil
description: Shared HIL rig rules - contract sections, board locks, running, farm check, reporting, verdicts, retries. Use before a HIL run, a manual session on a rig board, judging a HIL result, or a farm check (board/probe health - is each board's probe on the bus and does it reset the board) after rig maintenance, a reboot or re-cabling, in a repository with a `HIL contract:` line.
---

# hil — running a project's HIL

## The contract

Read this page, then, completely, the file the repository's instruction file
(`CLAUDE.md`, `AGENTS.md`) names on its `HIL contract:` line. Resolve the
contract only through that line, never by a directory or skill name. A contract
provides these sections; one written before them is accepted where its content
covers the same ground:

| Section | Supplies |
|---|---|
| Hosts/rosters | the hosts with boards, how to tell which one you are on, the roster file per host, a roster entry's shape and each board's lock name |
| Selection | what a run selects (examples and tests, or the board for a fixed battery), the PR-scoped selector and how to read its output, what it reports uncovered |
| Build | the build-contract call that produces a run's firmware and the receipt or record binding the run to it |
| Execution | the exact local and remote invocations, how a board set is batched, how to wait and what marks completion |
| Reservation | the lock helper and lock names, which tools lock themselves, the manual hold, cleanup and release sequence and its cleanup state |
| Results | where a run's report is, its fields and exits, the delegated output, how a row maps onto the verdicts below, who retries |
| Recovery | the states that need recovery, what a recovery dispatch carries, its commands and who may run them |
| Limits | what the rig cannot establish, commissioning, known tool faults |

A run's firmware is built through the build contract on a clean tree at the
pinned HEAD and carries the receipt or record Build defines, or is a CI artifact
fetched as the contract says; a build alone is never hardware evidence.

A requested operation the contract does not cover is not performed: return the
blocker. Never compose a lock, flash or run command the contract does not give,
and never pick a rig or host other than its Hosts/rosters gives.

## Board locks

One kernel flock per board, `/tmp/tinyusb-hil-locks/<lock>.lock`, shared by
every project and CI runner on a host, so tools of different projects exclude
each other on a shared board. `<lock>` is a registered board's registry `name`
(Board registry), whatever a roster calls it, else the contract's lock name for
the board; `scripts/hil_boards.py`'s `lock_name` gives either. The helper that
takes it, and its holder record, are the contract's Reservation: a wrapper of
this skill's `scripts/hil_lock.py` for the project's roster and lock names, and
the only entry point; the contract says which agentrc commit it loads, and from where.
The helper creates the dir writable by its owner only; a host whose projects
run as different OS users widens it once (`sudo install -d -m 1777
/tmp/tinyusb-hil-locks`), after every project there runs a helper that opens
nothing in it through a link.

- Never stop a CI runner. Killing a holder or releasing a hold you did not take
  happens only through the contract's Recovery procedure, under a separate
  recovery dispatch explicitly naming that board, its holder and the kill or
  release; bypassing a lock (`HIL_NO_BOARD_LOCK=1`, or the contract's
  equivalent) only under task scope explicitly naming forcing that board. A
  refused hold alone adds neither.
- Never pre-hold a board for a tool that locks itself: it refuses your hold as a
  conflict.
- Report a refused hold with its holder and reason; wait only within the task's
  lock-wait budget. A lock ends with the task that took it, whatever its
  outcome: no hold outlives its task for recovery, and no manual unlock step
  follows a failed cleanup. A detached manual hold (`hil_lock.py hold`, a
  project's manual session) is the exception: it lasts until its holder
  releases it, and a failed cleanup under it is logged all the same. Its
  holder, like a remote attempt's supervisor, starts without the hc terminal's
  `HC_SCOPE`, so that terminal's cleanup does not end it. The
  flock is the only refusal to take the lock; admission to the hardware is
  the busy check's (below). A free flock is
  always taken; what a task that ended without its cleanup left there (a
  record marked `cleanupOwed`, an unreadable record, an older helper's
  takeover journal) is first appended to the board's cleanup log as a failed
  cleanup, never a refusal. An older helper's cleanup holder
  (`role: cleanup-holder`) still holds its flock and never frees on its own:
  a taker refuses it as a recovery blocker and never waits on it, and the
  contract's Recovery ends it with that project's `hil_lock.py release
  --token <token>`. `hil_lock.py hold --take-over` is gone.
- The cleanup log, `<lock>.cleanup.jsonl` in the lock dir (0666, never
  removed), keeps one JSON line per event: `failed` (its `id`; `how`,
  `ended-failed` or `found-left`; the task's record, `detail`, the `receipts`
  of children not proven ended and the provider sessions it `retained`) and
  `restored`, naming the failure a later task restored the board after. A
  failure stays open until a `restored` event names it, whatever failed after
  it (`open_cleanup_failures`). `hil_lock.py status` shows the latest open one
  and how many earlier ones are; a taker's handle carries them
  (`cleanup_failures`) for its report before any hardware access, and its run
  reports them beside its result (`failure_report`, `failure_text`).
- A taker that restores the board takes with `protected=True`, which marks its
  record `cleanupOwed` and declares its whole group, `locks=` every board it
  takes (default the board alone), on the board's first record. It ends with
  `release_board_lock` after a proven cleanup, otherwise with
  `end_failed(handles, reason, detail=..., receipts=..., retained=...)`, which
  logs the failure on every board of the group, clears each record whose
  failure it logged (one it could not log keeps its record for the next take
  to log found-left), drops each flock whatever fails and returns the errors
  per board; an interrupt is re-raised once every handle is released. A
  session command run under a hold it did not take logs with
  `log_cleanup_failure`. Only a task that restored the board marks failures
  restored, through `resolve_restored`: each one whose receipts recheck ended
  and whose retained sessions read ended, the others kept open.
  `release` never signals a protected record or a runner reason
  (`hil_test.py`, `farm_check`), and signals only `hil_lock.py hold`'s own
  holder, proven by its start time and its flock. A record from a helper older
  than the mark carries none and is taken like any other: the farm-wide rule
  holds once every project on the host pins a helper with it.
- `hil_lock.PendingSessions(<lock>, <kind>)` (`<lock>.<kind>-pending.json` in
  the lock dir) is a consumer's per-board record; its class docstring has the
  mechanics. agentrc's Arduino engine rechecks the sessions an older engine
  left in its `arduino-hil` record, reports those not proven ended and clears
  the record once every one is, never refusing on it.
- Taking the lock is no permission to touch the probe. Every consumer, before
  its first hardware operation, reports the open failures found at take and
  checks for live processes using its probe or the board's devices: a busy
  check refuses access and names the users, never kills them or recovers the
  board, and an inconclusive check refuses rather than reporting the devices
  idle. It is a snapshot: it misses a child that opens the hardware later and
  does not serialize two commands under one hold, so commands sharing a
  manual hold serialize through `hil_lock.session_mutex(<lock>)`, taken
  before the hold is validated. The shared probe check is
  `hil_flash.probe_users`, whose docstring has its coverage: a recognized
  selector naming another probe excludes that process by its intended probe,
  with no guarantee against discovery opens of other candidates, and an
  STM32_Programmer_CLI counts as using an ST-LINK (an `stlink` flasher's, or
  openocd's on `interface/stlink.cfg`) unless it selects only other ST-LINKs
  by `sn=`, its discovery opens of this one accepted. Each consumer documents what its check covers. A task taking its
  own lock does it through
  `hil_lock.board_task(<lock>, busy=<check>, verdict=<collector>)`, which
  runs the check under the lock before the body and ends the lock on the
  cleanup verdict, given by the body or, at the end, by the collector from
  the consumer's ledger under the task's own interrupt hold. A worker whose
  `__exit__` an interrupt cut short calls `finish(e)`; `report()` gives the
  lock's outcome for a report, and the docstring has the verdicts and their
  limits. A command under a hold it did not
  take stays outside it: it never releases that hold and logs a failed
  cleanup with `log_cleanup_failure`.
- The minimum, lock, cleanup log and busy check, does not detect a leftover
  child of a killed task that touches neither the probe nor the board's
  devices. CI's job-end cleanup is best effort, never proof that such a child
  ended; an earlier `.started` without a matching `.done` in a CI state dir
  can refuse a rerun, but only on paths consulting that same dir.
- A rig-wide operation, one that can reset or renumber a board you do not
  hold (a hub's power, a root-port cycle, a controller rebind), holds every
  board on the host first, across every project's roster there; a contract's
  Hosts/rosters names the other projects' rosters on a host it shares. A
  partial hold is worse than none: it reads as protection. For a root-port
  or hub-port cycle that hold is this skill's `scripts/hil_reserve.py run`,
  given every roster on the host: it refuses when a device under the root
  port, or under the hub for a hub-port cycle, is not explained by those
  rosters, holds every board of every roster in one process around the one
  command it builds (`usb-kernel-recover`'s `root-cycle` or `hub-cycle`),
  and, when anything but a clean, fully re-enumerated outcome follows,
  releases them with the blocker logged in each board's cleanup log
  (`end_failed`; a surviving process as an `unrecheckable` receipt, so that
  failure stays open) and exits 1. A controller operation has no
  inspected reservation yet; a contract refuses it. A runner that
  cycles one hub port under the board's own lock is a limitation of this rule,
  not an exemption, recorded in the contract's Limits with what else the cycle
  is known to reach.

Manual work on a rig board outside the contract's runner (`hw-validator`,
`hw-debugger`, probe tools used directly) follows `target-debug`'s Delegated
sessions for its hold and cleanup; the contract's Reservation supplies the
helper and any cleanup state.

## Running

- At most one run or manual session on a board at a time; batch, wait and judge
  completion as Execution says, never cancelling before the run's own guard or
  spending a call only to pass time. A runner that printed its report and still
  holds a board is a blocker requiring recovery, never a process to cancel.
- Read only the report this run wrote; an older report in the checkout is not
  this run's.
- A runner that watches the bus (is the board enumerated, which process holds
  its usbfs node) loads this skill's `scripts/usb_observe.py` the way it loads
  the lock helper, one module instance per process; it observes only. Its
  `usb_scan` finds a present device and leaves out one whose `serial` read is
  inconclusive (missing, unreadable, timed out, or across a re-enumeration);
  an absence check uses `usb_absent(vid_pid, serial)`, which is false while
  any VID:PID match is inconclusive. Its `jlink_serial_match` and
  `jlink_serial_candidates` decide whether a J-Link's zero-padded serial is a
  roster uid.

## Farm check

A farm check tells whether each board of the host's roster is present: exactly
one of its flash probes is on the bus and a reset through it succeeds. It
flashes nothing and checks no USB; the board runs whatever image it had. Run it
when asked for a farm or board/probe health check, after rig maintenance, a
reboot or re-cabling, or before a HIL campaign; a HIL run needs none first.

Run the launcher the contract names, from the project's checkout: it loads the
engine (`scripts/hil_farm_check.py`) from the agentrc the project pins, with the
project's profile. Never run an installed engine against a project whose pin is
older, or the reverse.

The check takes each free board as a `board_task` (`farm_check`) whose busy
check is the profile's required `busy(board)` hook; a profile without it is
refused. A held board, or one whose check names users of its probe, is reported
`locked`, its probe still shown, and skipped: never waited on, released or
bypassed. A check that cannot tell is a `failed` row, the board untouched. An
older helper's cleanup holder is reported `blocked` and left to the contract's
Recovery. Never kill a running check: one killed outright leaves its board's
cleanup unproven, which the next task on it logs as a failed cleanup. An
interrupt (SIGINT) ends it after the current board's cleanup. A row notes an
earlier task's open failed cleanup on its board.

- Statuses: `ok` (the probe reset the board), `failed` (no probe, an ambiguous
  one, an inconclusive busy check or a failed reset), `unsupported` (the probe has no reset-only mode),
  `locked`, `blocked`. `ok` says the probe reached and reset the board now, not
  that its firmware, USB or anything else works.
- A `blocked` row is a cleanup the check could not prove, a process it ran
  through `ctx.run` not proven ended or a session in its cleanup's `retained`
  (one the profile's provider left running, an openocd_esp server) not closed
  as ended, or an older helper's cleanup holder. The check releases its own
  board either way, the failure logged in the board's cleanup log with those
  receipts and sessions, which the next task on it reports. Recovery, a
  separate dispatch, ends each retained session (`hil_openocd_esp.py end
  --receipt`, exit 0).
- Exit 0 no failed row, 1 a failed row or an aborted check, 2 refused before any
  board, 4 any blocked row. `locked` and `unsupported` rows are unverified, not
  healthy: read the table, never `$?` alone.
- Never a test verdict: it validates no firmware and no PR. A persisted report
  (`--report-dir`: `farm_check.json`/`.md`, its own attempt records) is
  farm-health evidence and never joins or replaces a harness report.

Paste the table whole with its footer, never a digest like "27/27 healthy".
Below it only what the table cannot show: held boards with their holders, and
each blocked board with what its cleanup log entry or older holder names and
that its recovery is a separate dispatch.

## Board registry

Each shared-farm board is wired to one probe, so how it is flashed is a farm fact, kept once
for every project in `boards.json` beside this page and keyed by that probe's serial:
its `flasher` (without `uid`: the key is it) and optional `flasher_recover` (the
shapes below) and a `name`, the board's lock name on every host (Board locks): unique,
a plain file name, and renamed only with a migration of the locks and records that
name it. The devices wired to its USB host port are farm facts too: `dev_attached`, each
with its `vid_pid` (`vvvv:pppp`), its `serial` as the host reads it (omitted when none was
read), and what a test may use (`is_cdc`, `is_msc` with `block_size`, `block_count` and
`msc_inquiry`). A project roster names the board by `"probe": "<serial>"`, matched exactly,
and keeps its own `uid` (the serial its firmware reports, which differs per firmware),
name, tests and project fields, never the registry's.

`scripts/hil_boards.py` is the one reader, and its docstring states what it refuses:
every consumer that needs a board's flasher resolves the roster entry through it
(`resolve`, `resolve_roster`, `board` for one entry by name with an explicit parked-board
policy, or `hil_boards.py roster FILE`); the resolved
`flasher.uid` is the probe serial, and a board without `probe` (a bench board on a dev PC,
or a rig one project owns) keeps a flasher of its own. A reader that needs only names reads the roster raw; one
that needs a lock name takes `lock_name(entry, own)`, `own` being the project's lock
field. `hil_boards.py lock FILE BOARD` prints the registry name, or a bench board's
roster name, never a project lock field: a contract with one takes it through its
Reservation. A board joins the farm, or its wiring changes (a replaced thumb drive
included), by an agentrc commit to `boards.json`, then the project's agentrc pin. The
resolved entry carries the registry's `dev_attached` at its top level; a roster that still
keeps its own copy (tinyusb's `tests.dev_attached`, Arduino's `arduino.dev_attached`) keeps
it for the consumers that read it, with a warning on stderr naming whether it matches,
until each project reads the registry's.

## Flashers

A runner that flashes or resets through this skill's `scripts/hil_flash.py`
gets the backend's command and its execution only; board, image, retries and
recovery stay the contract's. Linux only.

- A flasher is a dict: `name` (`jlink`, `stlink`, `openocd`, `lm4flash`,
  `esptool`, `pyocd`), `uid` (the probe's serial), `args` (passed to the tool
  as the words a shell would split them into, never expanded), and optionally
  `vid_pid` (openocd's complete `0xVVVV 0xPPPP` pairs; pyocd requires exactly
  one) and `verify`. A malformed `vid_pid` raises `ValueError` before the tool
  runs. Every call takes an explicit `timeout`; `run=` injects the caller's
  runner, by default `scripts/hil_process.py`'s `run_cmd`. Every command is an
  argv list, never a shell string: a runner is called as `run(argv,
  timeout=...)`, and for esptool `run(argv, cwd=..., env=..., timeout=...)`,
  `env` the variables to add to the inherited environment (`run_cmd(env=)` and
  `run_tracked(env=)` take it). `flash_esptool(esptool=)` names the esptool
  executable, by default `esptool` on PATH. `reset_esptool` resets through
  the ROM loader (`chip_id` without the stub, then a hard reset); lm4flash has
  no reset-only mode. A reset that exits 0 has not necessarily reached the
  target: `reset_problem(name, op)` reads its output for the target's answer.
  A J-Link session can exit 0 with every command failed: `jlink_problem(op)`
  judges its last attempt, each command from its own output chunk against the
  command file it ran (`jlink_judgment`, from recorded transcripts; an
  unknown command or output shape fails). `jlink_verify_differs(op)` names
  the `verifybin` a session stopped at having read the target and found it
  different, the one failure that proves something about the target; any
  other failure gives None. `esptool_problem(op)` judges a
  `flash_esptool` by its last attempt's exit status and the known failures
  esptool exits 0 on (a refused reset, a skipped empty image),
  `esptool_runtime()` the esptool the flash and its helper sessions run (one
  package, the one `scripts/esptool_session.py` imports too, its version), and `esptool_port(uid)` the one tty for
  a probe serial, refusing an ambiguous or missing one.
- A flash or reset returns an `Operation`: the last run's `CompletedProcess`
  fields plus `attempts`, each run's command, command file, output and rc on
  its own, never concatenated; `retried` is every attempt but the last. An
  exception from the runner carries the operation so far as `.operation`. Only
  a J-Link crash is rerun, and only when the caller passes `crash_retries`.
- Each attempt has a `kind`: `exited`, `timed_out` (rc 124, or a raised
  `TimeoutExpired` whose partial output it keeps), `interrupted` (a
  `BaseException` that is not an `Exception`, or an exception with a true
  `interrupted` attribute) or `error` (any other exception, such as a launch
  failure), the exception itself as `error`, a `cleanup` status and a
  `receipt`. A runner reports the last two as `cleanup` (`done`, `pending`,
  `unknown`) and `receipt` attributes on its result or exception. Only an
  explicit `done` counts: a missing attribute or any other value is
  `unknown`, the default runner's included, since `run_cmd` abandons a
  survivor of its bounded reap and a tool's exit does not prove its
  descendants exited. The operation's `cleanup` is its worst attempt's
  (`pending`, then `unknown`, then `done`) and `receipts` lists every
  receipt. A J-Link crash is not rerun after an attempt reported `pending`
  or a receipt.
- `flash_jlink` and `reset_jlink` write their command file in `workdir`, the
  current directory by default.
- `run_jlink_commands` and `run_stlink_commands` run one probe session of the
  caller's commands with the caller's connection profile, from a unique
  command file in `workdir`; `sysresetreq_jlink` and `sysresetreq_stlink` write
  AIRCR SYSRESETREQ through such a session, a reset distinct from the probe's
  (`reset_jlink`, `reset_stlink`). Their results are raw: judging the
  transcript and recovering stay the caller's. `run_jlink_commands` and
  `sysresetreq_jlink` take an optional `retry_if(attempt)`: while it answers
  true the whole session reruns, at most `retries` times (default 1),
  `retry_delay` s apart, each run its own `timeout`. A rerun is refused after
  an attempt whose cleanup is not an explicit `done` or that names a receipt;
  the operation's `retry_refused` then says why.
- `run_openocd_commands` runs one `openocd_cmd_base` session: `init`, the
  caller's Tcl commands and `shutdown`, from a unique script in `workdir`;
  `sysresetreq_openocd` writes AIRCR SYSRESETREQ through it. openocd logs to
  stderr and stops at the first failing command with exit 1. Neither takes
  `retry_if`: judging, retrying and recovering stay the caller's.
- `openocd_esp` is `scripts/hil_openocd_esp.py`'s (`flash_openocd_esp`,
  `reset_openocd_esp`): an Espressif chip's built-in USB-Serial/JTAG through
  openocd-esp32 v0.12.0-esp32-20260703 only, found by `HIL_OPENOCD_ESP`, else
  `openocd` on PATH. `uid` is the USJ serial; `args` is
  `-f board/<chip>-builtin.cfg` or `-f interface/esp_usb_jtag.cfg -f
  target/<chip>.cfg` and nothing else. It is never convoy-safe. The caller
  hard-resets the chip out of its ROM loader first and needs no wait of its
  own (`BOOT_SETTLE`). A command whose log shows a failed target event hook
  fails although openocd answers success. A flash takes the caller's ordered
  segments (sector-aligned offset, raw binary file, size, optionally its
  `sha256`), verifies each and reads every one back;
  `segments_from_flash_args(build_dir, chip, timeout=)` gives them from an
  ESP-IDF build's `flash_args`, refusing a build openocd would not write as
  esptool does.
  A flash also requires `security`, the caller's evidence read from the chip
  before programming: `{"flashEncryption": "off", "secureBoot": "off",
  "source": <where it was read>}`. Without it, or with either feature not
  `off`, the flash is refused before anything starts; the provider never
  infers or defaults the chip's security state. `read_security(serial, chip=,
  workdir=, timeout=, retains_lock=True)` reads it and hard-resets the chip;
  `security_evidence(result, chip=, source=)` reads a `security` op of the
  caller's own `scripts/esptool_session.py`. Unknown or contradictory
  evidence, or a chip outside `SECURITY_CHIPS` (ESP32-P4, ESP32-S3), is
  refused.
  Flash, reset and `read_security` run only for a caller that passes
  `retains_lock=True`, declaring it holds the board lock through each
  operation and logs a session it leaves pending, and never under a CI job (`RUNNER_TRACKING_ID` or
  `GITHUB_ACTIONS` set; `end` stays available); anything else is refused before any access, and a
  consumer calls `refusal(retains_lock=True)` before its own preparatory
  sessions. Each operation is one session, bounded by its `timeout`, that is
  never killed: it ends with `reset run` and a confirmed shutdown, or leaves
  cleanup `pending` with the session's receipt, which the caller logs as
  retained in the board's cleanup log, and no retry, restore or test of that
  task may overlap it.
  `session_state(receipt)` is a session's state for lock retention, by pid
  and start time alone, from `hil_openocd_esp_receipt.py`, which loads alone
  for a lock keeper. The module and function docstrings have the
  mechanics, `usj_tty` and `usj_node` included.
- `scripts/hil_openocd_esp.py end --receipt <receipt> [--timeout S]` ends such
  a pending session with `reset run` and a confirmed shutdown, within one
  deadline and never signalling it. It refuses on any doubt: the session (or
  another `end`) still holds the receipt's lock, the process is not the
  receipt's, no pidfd is available, init failed or is unproven (that board
  needs a separately reviewed recovery), or the last command sent is not known
  to have run. Exit 0 closes the receipt with the results and keeps it, and a
  rerun on it exits 0 again; anything else (refusal, failure, timeout,
  interrupt) exits 1 with the receipt open (2: bad usage or an unreadable
  receipt). An older helper's cleanup holder waiting on the session is
  released by that helper's `hil_lock.py release --token TOKEN` after exit 0,
  the caller's step.
- rc 0 means the tool reported success, not that the target runs the image;
  the project's own checks decide that. rc 124 from the default runner means
  it killed the tool's process group on the timeout and reaped it for a
  bounded grace: a survivor is abandoned, so 124 proves neither that every
  descendant died nor that the board is in a known state. An `openocd_esp`
  timeout also returns 124; its `cleanup` says whether the session still
  ended (`done`) or a command was outstanding and the server was left running
  on purpose (`pending`). A J-Link crash rerun is no proof the target was
  untouched either.

## Shared mechanisms

A project's harness loads these from the agentrc its bootstrap selects, as it
loads the lock helper; each script's docstring is its contract. They carry mechanics only:
rosters, the boards and cases a run selects, images, retries, report fields and
recovery stay the contract's.

| Script | Gives |
|---|---|
| `hil_boards.py` | each board's flasher from the board registry by probe serial (Board registry above) |
| `hil_load.py` | the one path loader: one module per file per process, a second path under one name refused |
| `agentrc_source.py` | the identity of the agentrc a harness loads: its closure, commit and digests, the staged copy and its manifest, drift, the record comparison |
| `hil_process.py` | `run_cmd`, a bounded command; `run_tracked` and `recheck`, a command whose end is proven; `state_dir`, a directory it trusts; `recheck_dir`, every operation in one |
| `hil_farm_check.py` | the farm check engine a project's launcher drives with its profile (Farm check above) |
| `hil_report_notice.py` | the unavailable notice and the report writer lock, loadable when the core is not (`write_unavailable`) |
| `hil_openocd_esp_receipt.py` | an `openocd_esp` session's state from its receipt, loadable without the provider (`session_state`) |
| `hil_report_core.py` | the report document: `_hil` metadata and roster identity, attempt archive, withdrawal and publish recovery, adopting a fetched report, the Markdown view, run and sequence verdicts |
| `hil_remote_core.py` | trees and attempts on a rig: staging, launch, status, fetch and copy-back |
| `hil_tt.py` | Reset_TT of the hub TT a full- or low-speed device sits behind |
| `hil_root.py` | every root operation of the usbtest, TT and USB network scripts, one checked verb each (`netns` moves a board's USB network interface, by its vendor id, into the namespace of a sandbox a user other than root created, the one `SUDO_UID` names when set); a rig grants its CI user sudo on its installed copy (`/usr/local/sbin/hil_root`), and separately on `/usr/local/sbin/usb_recover` for `hil_reserve.py run` recovery |
| `hil_usbtest.py` | usbtest binding, the device under test by serial (`find_device`), and bounded testusb case runs |
| `usb_dfu.py`, `esptool_session.py` | a DFU runtime request, one esptool connection, each a child the runner can kill |

- Load agentrc files through `hil_load.py` (its docstring has the bootstrap):
  `load(name, path)` reuses the module already loaded from that file under any
  name, so the harness's names and the scripts' own `agentrc_<stem>` loads are
  one instance (one strand memo, one runner), and refuses a second checkout or
  pin under one name. A harness loader of its own is reused only when it
  registers what it loads in `sys.modules`, before the scripts load it.
- Each script exports `FILES`, a literal of the agentrc-root-relative files it
  loads or launches by path, transitively, and refuses at import naming any
  that is missing. What a harness requires and stages is each script it loads
  plus its `FILES`, never a restated list; `hil_report_notice.py` and
  `hil_openocd_esp_receipt.py` load alone.
- A project's bootstrap picks its agentrc without importing any: the staged
  copy when `<project root>/.agentrc/` exists (a staging driver writes it; any
  gap there is an error, never a fall-through), the project's pinned commit in
  CI (`GITHUB_ACTIONS` or `CI` true, a malformed value refused; only CI
  fetches it), else the installed `"${AGENTRC_DIR:-$HOME/.claude}"`. It checks
  `agentrc_source.py`'s `API` literal before running it, then binds the
  identity `frozen()` takes once per process. Records bind `portable()`,
  the commit and closure digests, and admission refuses a record that binds
  none or another (`mismatch`); a stage copies the closure with `stage()`.
- The farm check engine ends a board's `board_task` either way: `clean()` once
  every process it started there is proven ended (`run_tracked`'s `done`, or
  a `recheck` of its receipt that answers `done`), whatever the reset did,
  since a reset restores nothing; otherwise `failed()`, the open receipts
  logged in its cleanup log; a run that left no receipt is logged as an
  `unrecheckable` one, which keeps its failure open. `run_tracked` only supplies the
  evidence; any other caller enforces its own reservation policy.
- `run_tracked`'s `state_dir` is the caller's: a directory of its uid that no
  other user can write, in a parent of its uid or root's that no other user
  can write unless sticky. Only those two are checked: the caller keeps every
  higher ancestor trusted and unchanged while any receipt names the directory.
  `state_dir(run_dir, name='ops')` makes one whatever the umask: the run dir
  0755 and `run_dir/name` 0700, each created or, when it is already a real
  directory of the caller's, chmodded; a link or another user's directory is
  refused (`PermissionError`) and left as it is.
- `recheck_dir(state_dir, keep=())` owns run_tracked's file layout: it
  rebuilds a receipt for each operation with a file in the state dir, the
  supervisor's identity from its record, rechecks it, and tidies a `done` one
  unless its token is in `keep`. It returns `(verdict, open)`: `done` when every
  operation is (none included), `pending` when every open one is, else
  `unknown`. `open` lists receipts `recheck` and `tidy` take, or, for one whose
  lock, record or log is missing or whose record is unreadable or names no lock
  of its own, a diagnostic `{token, unrecheckable, files}` that `recheck`
  answers `unknown` and a human removes; an untrusted or unreadable state dir
  is `('unknown', [{unrecheckable}])`. A file missing while the lock is held
  is a supervisor still starting: a receipt naming that lock alone, `pending`.
  A supervisor whose hello cannot reach its caller runs nothing and records
  `done`: a caller killed mid-start, its files read `unknown`, leaves no tool
  to start on a board handed on.
- A report is published through `hil_report_core`: the JSON is authoritative
  and the Markdown a view `prepare_pair` regenerates from it. An attempt that
  cannot publish recovers through `recover_publish` (a Markdown repair, or a
  withdrawal of the live report), so a predecessor's result never reads as
  this run's; a report published on a rig comes home through `adopt`.
  `apply_run_rules` and `sequence_verdict` give the Verdicts and Caller rules
  below for one attempt and for a retry sequence.
- A remote attempt is created once, under its own lease, and launched once;
  it is never wiped by the core, and its retention is the project's.

## Reporting

Interactively, show the run's result in its native form (its table verbatim, or
its JSON) and comment only on what it cannot show. A delegated run
(`hil-operator`) returns `{pass, results, caveat, wedged}` plus the fields the
contract's Results adds. What the contract's report tool produced is copied
verbatim, never retyped, reworded or re-ordered; the operator authors only what
Results assigns it: `wedged` (the boards it saw stop responding), and the rows,
aggregation and caveat Results defines for runs that started none, ended without
a report, or span several invocations. Without such a definition, a run that
never started gets one row per requested board with `ran: false`,
`pass: false` and the reason in `detail`. A missing or malformed reply is a
blocker.

## Verdicts

Each requested board gets one:

- **blocker**, which takes precedence: the board's state is unsafe or unknown,
  or the run was inconclusive (a failed cleanup, logged with the board
  released; wedged, aborted, no report).
- **pass**: the requested validation ran and passed, and any cleanup the
  contract requires succeeded.
- **fail**: the validation ran and established a failure.
- **not-run**: the validation was not exercised (locked, build refused, unbuilt,
  not on this rig).

A run-level caveat denies every row a pass: aborted makes every requested board
a blocker; nothing selected leaves each board not-run unless it already holds a
blocker.

## Caller rules

- Retry a fail once, only on a run with no blocker or locked board and an empty
  caveat, unless the contract names that result final; Results says whether the
  operator or its caller retries. Keep every attempt's report; a clean retry
  never erases an earlier run-level failure. An eligible retry that fails then
  passes reads as a pass marked flaky, quoting both reports.
- `locked` is an admission refusal, not a test failure: admit again once the
  holder releases, within the budget, or report the board not covered, naming
  the holder.
- Recovery is a separate dispatch for one board, carrying what the contract's
  Recovery lists.
