---
name: hil
description: Shared HIL rig rules - contract sections, board locks, running, reporting, verdicts, retries. Use before a HIL run, a manual session on a rig board, or judging a HIL result, in a repository with a `HIL contract:` line.
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
holder record (pid, reason), are the contract's Reservation: a wrapper of this
skill's `scripts/hil_lock.py` for the project's roster and lock names, and the
only entry point; the contract says which agentrc commit it loads, and from
where. A runner that watches the bus (is the board enumerated, which process
holds its usbfs node) loads this skill's `scripts/usb_observe.py` the same way,
one module instance per process; it observes only, never resets or locks.

- Never stop a CI runner. Killing a holder or releasing a hold you did not take
  happens only through the contract's Recovery procedure, under a separate
  recovery dispatch explicitly naming that board, its holder and the kill or
  release; bypassing a lock (`HIL_NO_BOARD_LOCK=1`, or the contract's
  equivalent) only under task scope explicitly naming forcing that board. A
  refused hold alone adds neither.
- Never pre-hold a board for a tool that locks itself: it refuses your hold as a
  conflict.
- Report a refused hold with its holder and reason; wait only within the task's
  lock-wait budget. A holder record marked as a cleanup holder
  (`role: cleanup-holder`) never frees on its own: it is a recovery blocker for
  every project, never waited on.
- A rig-wide operation, one that can reset or renumber a board you do not
  hold (a hub's power, a root-port cycle, a controller rebind), holds every
  board on the host first, across every project's roster there; a contract's
  Hosts/rosters names the other projects' rosters on a host it shares. A
  partial hold is worse than none: it reads as protection. A runner that
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
