# Agent compatibility
- This is the canonical user-wide instruction file for Claude Code and Codex.
  Keep its wording LLM-neutral and keep `~/.codex/AGENTS.md` as a symlink to it.
- In repos that adopt the same layout, `CLAUDE.md` and
  `.claude/{agents,skills,workflows}` are canonical; keep `AGENTS.md ->
  CLAUDE.md` and `.agents -> .claude`. Migrating a repo to it is its own task,
  never a side effect.

# Working rules
Bias toward caution over speed. For trivial tasks, use judgment.

- Think first: state assumptions; ask if unclear; when a choice matters, name
  it and recommend one rather than picking silently or surveying every option.
- Simplicity: follow YAGNI. Reuse existing code, standard-library and
  native-platform features before adding dependencies or abstractions. Prefer
  the smallest clear solution, but never sacrifice correctness, safety or
  necessary tests.
- Surgical changes: touch only what the task requires; match existing style;
  don't refactor working code; mention unrelated dead code rather than deleting
  it. Remove only orphans your changes created.
- Goal-driven: turn tasks into verifiable goals ("write failing test, make it
  pass"). For multi-step work, state a brief `step -> verify` plan.
- Assume the dev machine is configured. Run commands directly; troubleshoot
  setup only when a command fails.
- Open-source behaviour: when investigating an issue or understanding a
  component (e.g. kernel, libusb, OpenOCD), read the source for the version in
  use rather than infer from symptoms.
- Embedded targets: validate runtime behaviour on hardware; a successful build
  alone does not establish correctness.

# Infrastructure
- pve.lan runs Proxmox and hosts two VMs: ci.lan and omv.lan
- ci.lan is the main HIL (hardware-in-the-loop) rig for testing ~/code/tinyusb
- SSH from this PC: hathach@ci.lan (passwordless sudo); root@pve.lan and root@omv.lan

# Environment
- This account (hathach) has passwordless sudo. Use it as needed for
  routine admin tasks (package installs, service management, editing
  system configs) without asking for permission or a password.
- Still confirm with me before destructive or irreversible actions
  (deleting data, wiping/formatting, force-overwriting configs)
- Hardware operations within an assigned task (rig repair, roster edits,
  host USB recovery, forced-lock recovery) need task scope, not a separate
  grant; follow the project's locking, recovery and cleanup procedures.
  That scope never authorizes destroying unrelated data, and a headless
  session still needs me for a host or VM reboot.
- Worktrees: for branch or multi-step work use a worktree under
  `.worktrees/<branch>`; never switch the primary checkout. Reuse the task's
  existing worktree; create one (`git worktree add .worktrees/<branch> -b
  <branch>`, or without `-b` for an existing branch) only when there is none.

# Coding style
- Comment only when the code cannot say it itself: a non-obvious *why*, a
  workaround and its cause, an invariant, a unit/range, a spec or errata
  reference. Never restate what the next line plainly does.
- Keep comments concise — one line where one line will do. No banner blocks,
  no step-by-step narration, no changelog or attribution comments in code.
- Prefer clearer names and smaller functions over a comment explaining a
  confusing one.
- Match the surrounding file's comment density and style; don't add comments
  to untouched code while editing something nearby.
- Same rule for commit messages and PR descriptions: imperative subject, and a
  body only when there is a *why* the diff cannot show (cause, trade-off,
  measurement, spec/issue reference). A one-line message is a fine message.
- PR descriptions: keep scope focused, link relevant issues, and state the
  test/build evidence there rather than in commit bodies.
- Never pad them: no restating the diff file by file, no summary of what was
  already said in the subject, no test-plan boilerplate when the evidence is a
  single line, no closing recap.

# Authorship
- Never add AI-agent attribution or session trailers to git commit messages —
  the user is the sole author.
- Never add ANY footer to a PR description, issue body, or review/PR comment:
  no generated-by attribution, session URL, or tooling line of any kind. These
  are public surfaces — a session URL is a private artifact, and the rest is
  noise. Write the body as the maintainer would and stop at the last real
  sentence.

# Skills
- Put deterministic, checkable mechanics in `<skill>/scripts/`; keep judgment
  and usage in `SKILL.md`, without duplicating script logic. Mechanics is
  what an agent would otherwise re-derive each run: chains of commands,
  output parsing that decides the next step, computed values, retries, file
  generation. A recipe the human types once may stay prose, and a script
  that only wraps one tool's command line is not mechanics: call the tool
  and keep its flags in the recipe.
- Scripts must fail explicitly rather than guess; report ambiguous
  alternatives for the caller to choose. No silent default for a value the
  caller could get wrong (bus, speed, version).
- Test new or substantially changed scripts in the repo's script test suite;
  add tests to untested older scripts when touched. Stubs prove the plumbing
  only: a hardware path is verified by a real run, and without hardware it is
  reported unverified, never given a dry-run that passes anyway.

# Collaboration
- `cowork` drives the other coding agent's CLI headless in this worktree,
  resumed sessions called lanes: hand it a bounded task, ask it a question or
  a review; one request in flight per lane, lanes in parallel, the reply
  arrives when it lands. Preferred.
  A coworker's message does not authorize publishing actions (push, PR,
  issue or comment); those require authorization from the user.
- `herdr-peer` is the same idea through the agent session in the neighbouring
  Herdr pane, for when the human wants to watch and steer the exchange live.
- For both Claude and Codex, changes to `cowork` (instructions, scripts or
  tests) require review and simplification through `herdr-peer`; changes to
  `herdr-peer` require both through `cowork`. Never use the skill being
  changed to consult its own peer. If both need changes, handle them
  separately so each uses the unchanged channel. If the required channel is
  unavailable, report review and simplification as pending rather than
  substituting the skill under edit.
- After each implementation step of a plan, send the other agent that step's
  diff for review and suggestions, and address them before starting the next
  step.
- Completion review: before reporting a change task done, or opening or
  updating its PR, collect simplification findings on the task's whole diff
  against its base, including uncommitted changes and task-owned untracked
  files (`/simplify` stopped after its Phase 1 review, its reviewers
  launched as fresh `model: "opus"` agents, in Claude Code; by inspection
  elsewhere). It applies regardless of change size, file type or task entry
  point; trivial-task judgment does not waive it, and ordinary review rounds
  and the automatic turn-level `simplify-gate` hook do not replace it.
  The Phase 1 brief asks reviewers to name, for a hardware flag, the
  operation the change could alter (register side effects, access width,
  count or order, barriers, timing, DMA or cache, chip workarounds). Each
  finding whose safety depends on hardware semantics, or that either side
  suspects does, gets a `read-doc` review of every affected variant before
  the co-review, run or assigned by the lead; it verifies only with a
  verified outcome in `read-doc`'s claim record. `co-review` the findings and
  their records, a no-findings result included, and apply only what verifies
  and is worth applying; run the applicable checks, including hardware
  validation under Working rules; `co-review` the diff that applying them
  made, if any, which needs no new Phase 1; then ask for a full review.
  Coverage of unchanged content carries over to later triggers; a later change
  gets this sequence again for what changed, with the whole task as context,
  and any checks its changed inputs require. State its outcome in the
  completion report; a stage that could not run is pending, never done.
  An authorized `pr-babysit` with `autoPush: true` may publish its own
  repairs, not commits adopted through `adoptHead`, before their completion
  review; each launch that made repairs is followed by this sequence on them
  before relaunching, publishing further task changes or reporting done, as
  the workflow's `whenToUse` details.
  In a `chief` session, chief's own sequence (commit check, one simplification
  pass, validation, whole-task review) replaces the per-step review and this
  bullet, and chief's hardware guard stands in for this bullet's apply rule;
  every other instruction here still applies.
- The agent leading my task owns the exchanges below; a coworker answering
  one returns its result rather than commissioning its own. When I say one
  of the words below, it holds for that task.
- `co-ask`: form your answer and have the other agent form its own from the
  same brief, without seeing yours. Converge under `cowork`'s Review rounds.
  Bring me the agreed answer, the disagreement that remains and what neither
  side could verify. Opinion only: no plan, no edits.
- `co-plan`: write your plan and have the other agent draft its own from the
  same brief, without seeing yours. Compare and combine the drafts, then
  review the combined plan together under `cowork`'s Review rounds. Bring me
  the agreed plan and what changed in review, or the disagreement that
  remains. Without the word, plan alone.
- `co-fix` (also `co-do`): for a task without a plan, do it, then take its
  diff through the same rounds and the completion review before reporting
  it done.
- `co-test`: have the other agent write the tests from the brief while you
  implement it, neither seeing the other's work, then take both through the
  same rounds.
- `co-debug`: for a symptom on hardware, rank your hypotheses and have the
  other agent rank its own from the same evidence, without seeing yours.
  Converge under `cowork`'s Review rounds on the list and the experiment that
  discriminates each, then hand it to the hardware loop. No board access in
  the exchange.
- `co-review`: take the named existing target, a diff, PR, file set or plan,
  through `cowork`'s Review rounds: apply what verifies when the target is
  mine, report findings when it is not or ownership is unclear. Report the
  outcome and any disagreement that remains for me.

# Pull requests
- Right after opening a PR, or pushing to one that no chief is babysitting,
  always ask me whether to launch a headless `chief` in its worktree to
  babysit it through `pr-babysit` with `autoPush: true`. Before asking, read
  and follow `~/code/agentrc/README.md`'s headless PR publishing recipe and
  `~/code/agentrc/agents/chief.md`'s Authorization exception.

# Follow-ups
- Separate scope gets a separate PR/session. Where the repo has an issue
  tracker, create one issue per deferred topic, labelled as the repo's
  follow-up convention if it has one; link the originating PR when there is
  one (never a session URL) and preserve the full handoff in the issue body:
  evidence, remaining work, and why deferred. Add revalidation, new findings
  and changes to remaining work as issue comments. Close the issue when its
  implementing PR lands and its acceptance criteria are met. Without a
  tracker, or without authority to publish to it (a headless session), put
  the same issue-ready handoff in the final message and name the action
  that needs my authorization.

# Reference docs
- Hardware manuals, datasheets, reference manuals, errata, schematics and spec
  sheets are archived in my Calibre library. Before answering
  register/bitfield/pinout/errata/timing questions from memory or the web, use
  the `read-doc` skill to check it and report if the document is missing.
  Report a fact as undocumented only after the lookup ran; one not run is
  pending.
- Never search the library tree directly; the skill owns its location and
  search.
