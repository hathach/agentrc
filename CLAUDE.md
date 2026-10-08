# agentrc

User-wide rules are in `instructions/user.md`; edit them there, and keep
`~/.codex/AGENTS.md` a symlink to them.

- For both Claude and Codex, changes to `cowork` (instructions, scripts or
  tests) require review and simplification through `herdr-peer`; changes to
  `herdr-peer` require both through `cowork`. Never use the skill being
  changed to consult its own peer. If both need changes, handle them
  separately so each uses the unchanged channel. If the required channel is
  unavailable, report review and simplification as pending rather than
  substituting the skill under edit.
- Verify every change with `python -X utf8 -m unittest discover -s tests`,
  which also runs the workflow harnesses under node.
