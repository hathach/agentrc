# agentrc

User-wide rules are in `instructions/user.md`; edit them there, and keep
`~/.codex/AGENTS.md` a symlink to them.

- For both Claude and Codex, review and simplify changes to `cowork`
  (instructions, scripts, tests or the `lib/` modules it imports) through
  the installed cowork from the primary checkout, run from the task
  worktree in a dedicated read-only lane, after checking that the installed
  copy and the `lib/` modules it imports resolve outside the task worktree
  and are unchanged; keep them unchanged until the reply is collected. If no
  working unchanged copy and its `lib/` modules are available, report review
  and simplification as pending.
- Verify every change with `python -X utf8 -m unittest discover -s tests`,
  which also runs the workflow harnesses under node.
- `pr-review` dimensions for this repo (no firmware, so no hardware
  conformance): `correctness: logic, error and edge paths, state machines,
  resource lifetime`; `concurrency: interrupt and thread safety, shared state,
  ordering, reentrancy`; `API, compatibility and conventions: public API and
  configuration, backward compatibility, tests and docs kept in step, the
  repository's own conventions`.
- `pr-babysit` args for this repo:
  `{"pr": <num>, "reviewers": ["coderabbit", "copilot", "greptile"], "autoRun": ["coderabbit"], "coderabbitTrigger": true, "greptileTrigger": true}`;
  Greptile auto-reviews on `open` events (`.greptile/config.json`); request
  another review with `@greptileai`, which `greptileTrigger` posts once per
  pushed head Greptile has not reviewed.
