# agentrc

User-wide rules are in `instructions/user.md`; edit them there, and keep
`~/.codex/AGENTS.md` a symlink to them.

- For both Claude and Codex, run the cowork-routed reviews of a change to
  `cowork` (its instructions, scripts, tests or the `lib/` modules it
  imports) through the installed cowork, in a read-only lane run from the
  task worktree. Before sending and again after collecting the reply, check
  that the installed copy resolves outside the task worktree and that its
  `skills/cowork` and `lib/` hold none of the task's commits and no
  uncommitted edits; if a check fails, or no such copy works, report that
  review as pending.
- Build contract: `BUILD.md`. Verify every change with its command.
- `pr-review` dimensions for this repo (no firmware, so no hardware
  conformance): `correctness: logic, error and edge paths, state machines,
  resource lifetime`; `concurrency: interrupt and thread safety, shared state,
  ordering, reentrancy`; `API, compatibility and conventions: public API and
  configuration, backward compatibility, tests and docs kept in step, the
  repository's own conventions`.
- `pr-babysit` args for this repo:
  `{"pr": <num>, "reviewers": ["coderabbit", "copilot"], "autoRun": ["coderabbit"], "coderabbitTrigger": true}`;
  Greptile is left out until 2026-12-01 (review credits used up); then add
  `"greptile"` back to `reviewers` and `"greptileTrigger": true`.
  Greptile auto-reviews on `open` events (`.greptile/config.json`); request
  another review with `@greptileai`, which `greptileTrigger` posts once per
  pushed head Greptile has not reviewed.
