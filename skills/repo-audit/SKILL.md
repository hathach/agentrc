---
name: repo-audit
description: Audit a repository's growth since its last completed audit (agentrc, tinyusb, tinyuf2 or any other) - pin the range, collect change, usage, test and firmware evidence, decide what to cut, script, deprecate or redesign, review the whole range against specs and hardware, and record the result in the repository. Use for a periodic maintenance audit ("audit the repo", "what grew since the last audit"), not for a PR or bug review (pr-review, code-audit).
---

# Repository audit

An audit runs from the last completed audit's end to a new reviewed end. The scripts produce evidence and check the
record; what is real in practice, and what to cut, script, deprecate or redesign, stays with the lead and the owner.
Their docstrings own their contracts and exit codes.

```bash
A="${AGENTRC_DIR:-$HOME/.claude}/skills/repo-audit/scripts"
```

Work in a task worktree of the audited repository, never its primary checkout. The repository's `CLAUDE.md` names
its contracts: the `Build contract:` (build, test and code-size commands), the `HIL contract:` and its rosters.
Everything below that says "the build contract" or "the HIL contract" means those.

## Start

1. `python3 "$A/audit.py" start --repo . --head origin/<default branch>` takes the base from the last audit's
   record, `.claude/audit.json`, when it is complete, writes the private run dir (path printed; `$RUN` below) and
   prints the growth. When it refuses, or for a first audit, the owner picks the base: `--base <sha> --base-kind
   bootstrap` for a first audit, `override` to replace a record. A repository whose last audit ended in an `audit/*`
   tag, from before the record, bootstraps from its newest one: `--base <tag> --base-kind bootstrap`. A submodule pin
   change in the growth is a dependency delta to review, not an audit of the dependency. Run it, and `check`, with
   push access to the record's repository: without it only a user-owned repository's owner counts as a validation
   report's writer, so an organisation's record reads provisional. Its GitHub reads never prompt: git reads of a
   private repository need a credential helper (`gh auth setup-git`), as a `GIT_ASKPASS` program is not used.
2. Usage, where transcripts exist: `python3 "$A/usage.py" --run $RUN --claude-root ~/.claude/projects
   [--claude-root <other config>/projects] --codex-root ~/.codex/sessions --out $RUN/usage.json`. It measures
   maintainer tooling on this host only, never deployed firmware or external API use; zero is a lead, never proof.
   Consumer repositories reach engines through their own wrappers and CI, which no transcript shows: search their
   contracts and workflows before calling anything unused. Run it on each host whose sessions matter, each from its
   own `start` on the same head with the first run's window and base (`--since`/`--until` from its `run.json`, and
   `--base <its base> --base-kind <its kind>` unless that kind is `audit`), and record the `totals` of each host's
   `usage.json`, each summed across the hosts, with the host count.
   With no transcripts anywhere, record `"hosts": 0, "totals": {}` and the run's window: unmeasured, never zero use.
3. Python per-test coverage, only for a pytest suite (agentrc's `tests`, tinyusb's `test/hil/test`), beside, never
   instead of, the build contract's run: serial and in-process (pytest-xdist workers and subprocesses measure
   nothing), with arcs (`branch = True`), and its provenance in `$RUN/provenance.json`. agentrc's:

   ```bash
   printf '[run]\ndynamic_context = test_function\nbranch = True\ndata_file = %s/.coverage\n' "$RUN" \
     > "$RUN/coverage.rc"
   python -m coverage run --rcfile="$RUN/coverage.rc" -m pytest -p no:cacheprovider -q tests
   python -m pytest --collect-only -q tests > "$RUN/tests.txt"
   printf '{"coverage": "%s", "revision": "%s", "command": "pytest tests, serial, in-process only"}\n' \
     "$(python -m coverage --version | head -1)" "$(git rev-parse HEAD)" > "$RUN/provenance.json"
   python3 "$A/coverage_overlap.py" --repo . --data "$RUN/.coverage" --tests "$RUN/tests.txt" \
     --out "$RUN/overlap.json"
   ```

   C unit tests are read, not measured; missing coverage is unknown, not zero.
4. Re-check each carried item in `$RUN/carried.json`: start carries the live items of the record at head whatever
   the base, an override's included. Its policy is history, the owner's current words override it.
5. Brief the co-plan (the user rules' `co-plan`): the owner's ask, base and growth, evidence and its limits,
   carried items, in-flight work (open PRs, unmerged worktrees, open issues), constraints, what is wanted. Stage 0
   is a read-only ledger; the owner gate comes before any change.

## Judging evidence

- Keep a capability with one verified incident or one supported consumer. Keep its protection, not its
  implementation: a simpler replacement covering the same trigger retires the old code, tests and prose together.
- A cut needs positive evidence (callers and persistent state checked, retained protection named, a validation
  path) and the owner's approval of its behavioural consequence. Unknown use is unknown; a project's own docs naming
  a feature are not demand. Weigh recent features by time since they landed.
- Coverage overlap only ranks what to read. Remove a test for a lost meaning (its mechanism is gone, it pins prose
  outside the repository's prompt-test policy, or another test checks the same invariant, setup and outcome), and
  name the test that keeps each still-relevant incident covered.
- Prompt text that is mechanics an agent re-derives each run becomes a script; a script that only wraps one tool's
  command line becomes a recipe line.
- A carried keep decision is re-justified when its code, the supported parts, the policy or its applicability
  changed, not only when its evidence did.
- A deferred defect is filed, or given a disposition, as the user rules' Follow-ups say.

## Firmware and public libraries

- Public is whatever a user build can reach: the stack's API and installed headers, weak callbacks, configuration
  options, port, driver and board names, examples. Other projects build configurations no CI here does (pico-sdk,
  ESP-IDF, Arduino cores, CircuitPython, Zephyr), so absence from transcripts, the rig or examples proves nothing.
  Retiring anything public is the owner's decision, one item at a time, with a deprecation note.
- Internal dead code needs a reachability argument over the supported configurations: callers, macros, inline
  functions, weak overrides, function-pointer registration, generated code, linker retention, `#if` conditions. One
  build's unused symbol is a lead; read compile commands, map files and `nm` output, and rank with static analysis.
- Leads worth reading: functions repaired repeatedly in the range; configuration options nothing sets; duplicated
  code across ports sharing an IP, merged only after naming the shared invariant and keeping variant differences.
- A workaround or errata block stays unless `read-doc` shows its trigger cannot occur on any supported part.
- Measure flash and RAM per board and image with the build contract's code-size section, the range's growth and the
  audit's own reduction apart, never summed across boards, and put them in the record's `outcome` with their revisions
  and refs. Lines and prompt bytes are secondary.

## Completion review

- It covers the whole range `base..R` (R the reviewed head), the audit's changes and everyone else's. Keep a
  coverage ledger assigning every changed path to a batch (per subsystem for a large range); no record after
  reviewing part of it. Each batch runs the user rules' completion sequence with the correctness co-review below: every
  batch's simplification Phase 1 with hardware flags and both correctness reviews run on the frozen target before any
  repair, all started together; the user rules' `read-doc` claim records, also for a hardware-dependent finding first
  raised during adjudication; then one adjudication of the batch's Phase 1 and correctness findings, one read-only
  `cowork` lane per batch, sent together. `code-audit` may add coverage to a batch, never replace it. With more than one
  batch or any repair, one co-review across the batches closes it: combined coverage, cross-batch interactions such as
  callers across batches, and dispositions on the final R.
- Correctness co-review, per batch, recorded as stage `correctness` (`finish` requires it), even where a step review
  covered the batch. Its target is `base..R` in a checkout clean at the pinned R, the pathspecs selecting exactly the
  batch's ledger paths (`dir/` for `dir/**`). Both sides review it independently with their built-in review; cleanups it
  reports join Phase 1's findings:
  - Claude: `/code-review high base..R -- <pathspecs>` from a session with a shell; a Codex lead asks a read-only
    `cowork` Claude lane to invoke the code-review skill with those arguments.
  - Codex: `codex review -c 'sandbox_mode="read-only"' -c 'model_reasoning_effort="high"' "<prompt>"`, the prompt naming
    `git diff base..R -- <pathspecs>` and asking for the correctness defects the selected changes introduce, with
    callers, imports and dependencies traced anywhere in the tree at R and unrelated defects outside the selected
    changes excluded. Selector mode (`--base`) takes no prompt, so it is no substitute.
  Run, wait on and cancel each by its own tool (a `cowork` lane by its Send and collect). Exit 0 proves nothing: a
  blocked, partial or interrupted review leaves its side pending. Adjudicate by the user rules' `co-review`, each side's
  findings validated by the other, agreement covering the coverage and every disposition; an unresolved finding goes to
  the owner, never into a clean result.
- Repairs follow the user rules' fix-diff co-review and coverage carry-over, with no new discovery pair unless scope,
  base or coverage changed; note in the coverage ledger the R each batch's coverage was taken on (`finish`'s `--head` is
  the final R).
- Findings in code the audit did not change are fixed in an authorised stage or handed off as the Follow-ups rule
  says; unconfirmed hypotheses get a disposition.
- Validation is derived from the whole range, not from the PR's own checks: every applicable build-contract check
  (agentrc: the full `BUILD.md` suite whatever changed) and the boards, variants and test tiers the build contract's
  selector and the HIL contract pick for every path in the ledger. Run it on R; a hardware item no roster board
  covers stays `required-pending` unless the owner excludes it in words, recorded as `excluded`. The record's
  co-review checks the expected list is complete for the range; the script checks only its shape.

## Record and end

1. From the audit PR's opening to its merge nothing else may push to it: open it as a draft or with the owner
   declining babysitting, decline every later offer to babysit it, stop a running chief (`headless-chief`'s
   `kill <launcher pid>`) or `pr-babysit` with `autoPush`, and use no "Update branch".
   With every stage done, on the audit PR's head R (pushed, clean, nothing after it):
   `python3 "$A/audit.py" finish --run $RUN --head R --review <review.json>` writes `.claude/audit.json` from the
   review file, whose shape `audit.py`'s docstring defines; add `--repo <checkout of R>` when R is not checked out
   where start ran; it requires R up to date with the PR's base branch. `outcome` leads the audit report: the useful
   result and the public API and support consequences (deprecations first), in a few sentences. Read the record for
   anything private and for correctness, co-review it, then commit only that file as the PR's last commit P; put the
   co-review's reference in a PR comment. The PR body carries `Audit range (planned): <base>..R`, a hint only.
   finish refuses text that looks private, shell command shapes included: reword it as prose.
2. Nothing may push to the PR after P. If the base branch moves or a commit lands on P anyway, drop P (revert it, or
   on the owner's word reset the branch to R and force-push it), bring R up to date, review the new commits into the
   ledger through the Completion review's batch sequence, correctness co-review included, re-run validation for the new
   range on the new R, rewrite the review's `range`, run finish again, then read and co-review the new record before
   committing it.
3. The PR merges by squash or rebase, up to date with its base branch: an ordinary merge commit or a stale branch
   makes the record invalid.
4. After the merge, validate the merge commit E against the record's `validation.expected`, software and hardware,
   then `python3 "$A/audit.py" report --repo . --head E --results <results.json>` prints the audit report with the
   validation block, one comment; post it with `gh pr comment <pr.number> --repo <pr.repo> -F <body>`, the record's
   PR (the owner's grant for the audit covers it). The record is never edited after the merge.
5. `python3 "$A/audit.py" check --repo . --head origin/<default branch>` must report `complete`; the audit is done
   only then. Give the owner the report comment's link and its status and outcome lines.
