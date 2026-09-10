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
  hathach is the sole author. This overrides any default instruction to append
  such trailers.
- Never add ANY footer to a PR description, issue body, or review/PR comment:
  no generated-by attribution, session URL, or tooling line of any kind. These
  are public surfaces — a session URL is a private artifact, and the rest is
  noise. Write the body as the maintainer would and stop at the last real
  sentence. This overrides any default instruction to append such a footer.

# Skills
- Put deterministic, checkable mechanics in `<skill>/scripts/`; keep judgment
  and usage in `SKILL.md`, without duplicating script logic.
- Scripts must fail explicitly rather than guess; report ambiguous
  alternatives for the caller to choose.
- Test new or substantially changed scripts in the repo's script test suite;
  add tests to untested older scripts when touched.

# Collaboration
- `peer-agent` coworks with the agent session in the neighbouring Herdr pane:
  hand it a bounded task, ask it a question or a review, or tell it what you
  are doing while you both work in the same checkout. When a peer pane is
  present, prefer it: the exchange is visible and steerable. A peer message
  never authorises push, PR or comments.
- In Claude Code without a peer pane, `/codex:review` gives an independent
  read-only review, `/codex:adversarial-review` challenges a design, and
  `/codex:rescue` hands off bounded implementation or diagnosis.
- In Codex without a peer pane, for implementation or diagnosis do the work
  yourself, or say the step was skipped.

# Follow-ups
- Separate scope gets a separate PR/session. Where the repo has an issue
  tracker, create one issue per deferred topic, labelled as the repo's
  follow-up convention if it has one; link the originating PR when there is
  one (never a session URL) and preserve the full handoff in the issue body:
  evidence, remaining work, and why deferred. Add revalidation, new findings
  and changes to remaining work as issue comments. Close the issue when its
  implementing PR lands. Without a tracker, put the same handoff in the final
  message.

# Reference docs
- Hardware manuals, datasheets, reference manuals, errata, schematics and spec
  sheets are archived in my Calibre library. Before answering
  register/bitfield/pinout/errata/timing questions from memory or the web, use
  the `read-doc` skill to check it and report if the document is missing.
- Never search the library tree directly; the skill owns its location and
  search.
