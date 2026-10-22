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
each other on a shared board. `<lock>` is the contract's lock name for the
board, which may differ from its roster name. The helper that takes it, and its
holder record, are the contract's Reservation: a wrapper of this skill's
`scripts/hil_lock.py` for the project's roster and lock names, and the only
entry point; the contract says which agentrc commit it loads, and from where.
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
  lock-wait budget. A recovery blocker never frees on its own and is never
  waited on, for every project: a cleanup holder (`role: cleanup-holder`), live
  or dead; a record marked `protected: true` left on a free flock, its run
  having died before its cleanup; or a takeover journal (`<lock>.takeover`).
  The contract's Recovery ends it, under a recovery dispatch naming the board:
  `hil_lock.py hold --take-over <lock>` holds the blocker's whole lock set as a
  cleanup holder, journalling each record it replaces, and once the recovery
  succeeded `hil_lock.py release --token <token>` ends that live holder. A
  dead holder is never released by token; it is taken over. A takeover that
  finds a journal owned by another OS user in a sticky lock dir refuses up
  front, changing nothing, and names that uid: run it and its `release --token`
  as that user or root.
- A taker whose interrupted run needs recovery takes with `protected=True` and
  ends with `release_board_lock`, or hands its held locks to a cleanup holder
  with `hand_to_cleanup_holder`. A protected take declares the run's whole
  group, `locks=` every board it takes (default the board alone), on the
  board's first record, before any hardware access, so a takeover from any
  member takes the group; a taker failing partway ends what it took, and a
  record it could not clear stays a blocker naming the group. The hand-over
  refuses, changing nothing, unless the handed boards are exactly that group,
  and loads `scripts/hil_process.py` (in the lock helper's `FILES`, below). A
  record that declares no group (an older helper's, or one cut short) is
  taken over with every board whose record names it. Given the receipts of runs
  not proven ended, the handed-over holder clears only once each rechecks `done`, and
  `release --token` refuses up front naming the open receipt, rechecked as the
  holder's OS user when root releases it. A cleanup holder of its own waits in
  `wait_for_release` and then ends its hold itself. `takeover_predecessor`
  reads the record a takeover replaced. `release` never signals a protected record,
  a cleanup holder or a runner reason (`hil_test.py`, `farm_check`), and
  signals only `hil_lock.py hold`'s own holder, proven by its start time and
  its flock. A record from a helper older than the mark
  carries none and is
  taken like any other: protection across projects holds once every project
  on the host runs this helper.
- A session a consumer cannot prove ended outlives its run in the board's
  pending-session record, `hil_lock.PendingSessions(<lock>, <kind>)`
  (`<lock>.<kind>-pending.json` in the lock dir), so every later invocation,
  whatever its run dir or project, refuses while it lists one or cannot be
  read; an unreadable record is kept. `add` lists sessions, each a dict with a
  `receipt`; `clear(ended)` removes the record only once the consumer's
  `ended` holds for every session, otherwise refusing with their receipts;
  `clear(ended, note=..., **evidence)`, only after a separately reviewed
  recovery, removes it whatever it holds, once note, evidence and content are
  logged (`cleared()`). It needs no board lock; another OS user can read it
  but not replace or remove it. What a receipt means and how its session is
  recovered stay the consumer's; the class docstring has the mechanics.
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
  and keeps the locks as a cleanup holder until the contract's
  recovery procedure releases them by token when anything but a clean,
  fully re-enumerated outcome follows. A controller operation has no
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
  `jlink_serial_match` and `jlink_serial_candidates` decide whether a J-Link's
  zero-padded serial is a roster uid.

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

The check takes each free board's lock itself (`farm_check`, protected). A held
board is reported `locked`, its probe still shown, and skipped: never waited on,
released or bypassed. A recovery blocker is reported `blocked` and left to the
contract's Recovery. Never kill a running check: one killed outright leaves its
board blocked for a recovery takeover. An interrupt (SIGINT) ends it after the
current board's cleanup.

- Statuses: `ok` (the probe reset the board), `failed` (no probe, an ambiguous
  one, or a failed reset), `unsupported` (the probe has no reset-only mode),
  `locked`, `blocked`. `ok` says the probe reached and reset the board now, not
  that its firmware, USB or anything else works.
- A `blocked` row's cleanup names its blocker: a cleanup holder's `token`, which
  Recovery releases with `hil_lock.py release --token` (refused until every
  process the check started is proven ended), or a protected record, which needs
  a recovery takeover.
- Exit 0 no failed row, 1 a failed row or an aborted check, 2 refused before any
  board, 4 any blocked row. `locked` and `unsupported` rows are unverified, not
  healthy: read the table, never `$?` alone.
- Never a test verdict: it validates no firmware and no PR. A persisted report
  (`--report-dir`: `farm_check.json`/`.md`, its own attempt records) is
  farm-health evidence and never joins or replaces a harness report.

Paste the table whole with its footer, never a digest like "27/27 healthy".
Below it only what the table cannot show: held boards with their holders, and
each blocked board with its token or record and that its recovery is a separate
dispatch.

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
  unknown command or output shape fails). `esptool_problem(op)` does the same
  for `flash_esptool` against the offsets its last attempt wrote (each written
  once, verified, then the hard reset), `esptool_runtime()` the esptool the flash and its helper sessions run (one
  package, its version), and `esptool_port(uid)` the one tty for a probe
  serial, refusing an ambiguous or missing one.
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
  `retains_lock=True`, declaring it keeps the board lock through a session
  left pending, and never under a CI job (`RUNNER_TRACKING_ID` or
  `GITHUB_ACTIONS` set; `end` stays available); anything else is refused before any access, and a
  consumer calls `refusal(retains_lock=True)` before its own preparatory
  sessions. Each operation is one session, bounded by its `timeout`, that is
  never killed: it ends with `reset run` and a confirmed shutdown, or leaves
  cleanup `pending` with the session's receipt for the caller's cleanup
  holder, and no retry, restore or test may overlap it.
  `session_state(receipt)` is a session's state for lock retention, by pid
  and start time alone. The module and function docstrings have the
  mechanics, `usj_tty` and `usj_node` included.
- `scripts/hil_openocd_esp.py end --receipt <receipt> [--timeout S]` ends such
  a pending session with `reset run` and a confirmed shutdown, within one
  deadline and never signalling it. It refuses on any doubt: the session (or
  another `end`) still holds the receipt's lock, the process is not the
  receipt's, no pidfd is available, init failed or is unproven (that board
  needs a separately reviewed recovery), or the last command sent is not known
  to have run. Exit 0 closes the receipt with the results and keeps it, and a
  rerun on it exits 0 again; anything else (refusal, failure, timeout,
  interrupt) exits 1 with the receipt open and the board held (2: bad usage or
  an unreadable receipt). Releasing the board's cleanup holder (`hil_lock.py
  release --token TOKEN`) after exit 0 stays the caller's step.
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

A project's harness loads these from the agentrc it pins, as it loads the lock
helper; each script's docstring is its contract. They carry mechanics only:
rosters, the boards and cases a run selects, images, retries, report fields and
recovery stay the contract's.

| Script | Gives |
|---|---|
| `hil_load.py` | the one path loader: one module per file per process, a second path under one name refused |
| `hil_process.py` | `run_cmd`, a bounded command; `run_tracked` and `recheck`, a command whose end is proven |
| `hil_farm_check.py` | the farm check engine a project's launcher drives with its profile (Farm check above) |
| `hil_report_notice.py` | the unavailable notice and the report writer lock, loadable when the core is not (`write_unavailable`) |
| `hil_report_core.py` | the report document: `_hil` metadata and roster identity, attempt archive, withdrawal and publish recovery, adopting a fetched report, the Markdown view, run and sequence verdicts |
| `hil_remote_core.py` | trees and attempts on a rig: staging, launch, status, fetch and copy-back |
| `hil_tt.py` | Reset_TT of the hub TT a full- or low-speed device sits behind |
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
  plus its `FILES`, never a restated list; `hil_report_notice.py` loads alone.
- The farm check engine releases a board it reserved (`protected=True`) only
  once every process it started there is proven ended: `run_tracked`'s
  `done`, or a `recheck` of its receipt that answers `done`. Anything else
  keeps a cleanup holder or the protected record for the contract's
  Recovery. `run_tracked` only supplies the evidence; any other
  caller enforces its own reservation policy.
- `run_tracked`'s `state_dir` is the caller's: a directory of its uid that no
  other user can write, in a parent of its uid or root's that no other user
  can write unless sticky. Only those two are checked: the caller keeps every
  higher ancestor trusted and unchanged while any receipt names the directory.
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
  or the run was inconclusive (held after a failed cleanup, wedged, aborted, no
  report).
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
