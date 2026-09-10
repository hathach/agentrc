---
name: simplify-gate
description: Show, switch on or switch off agentrc's simplify gate (the Codex YAGNI challenge that runs when a Claude Code session stops) for the current repository and all of its worktrees. `status` prints the state with the effective model and effort, `on` takes `--model`/`--effort` overrides, bare `/simplify-gate` or `help` prints the usage.
---

# simplify-gate

Run from anywhere inside the checkout and show the output verbatim:

```bash
python3 <skill dir>/scripts/gate.py status
python3 <skill dir>/scripts/gate.py on [--model M] [--effort E]
python3 <skill dir>/scripts/gate.py off
python3 <skill dir>/scripts/gate.py            # usage, same as `help`
```

The state is one marker file in the repository's git common dir, so every
worktree sees the same switch and a new worktree inherits it. `on` without
flags keeps the overrides already in the marker; `off` deletes them with the
marker. A change takes effect at the next tool call, no session restart.

Status shows `on`/`off`, then the model and effort with their source: `(repo)`
from the marker, `(default)` from `hooks/simplify_gate.py`. The hooks
themselves are installed once per machine with
`python3 ~/code/agentrc/hooks/simplify_gate.py --install`; if status says on
but no challenge ever runs, check that first.
