---
name: simplify-gate
description: Show, switch on or switch off agentrc's simplify gate (the Codex YAGNI challenge that runs when a Claude Code session stops) for the current repository and all of its worktrees. `status` prints the state with the effective model and effort, `on` takes `--model`/`--effort` overrides, bare `/simplify-gate` or `help` prints the usage. `prune` frees the gate's per-user cache, dry run unless `--apply`.
disable-model-invocation: true
---

# simplify-gate

Run from anywhere inside the checkout and show the output verbatim:

```bash
python3 <skill dir>/scripts/gate.py status
python3 <skill dir>/scripts/gate.py on [--model M] [--effort E]
python3 <skill dir>/scripts/gate.py off
python3 <skill dir>/scripts/gate.py prune [--apply] [--days 30] [--report-days 90]
python3 <skill dir>/scripts/gate.py            # usage, same as `help`
```

The state is one marker file in the repository's git common dir, so every
worktree sees the same switch and a new worktree inherits it. `on` without
flags keeps the overrides already in the marker; `off` deletes them with the
marker. A change takes effect at the next tool call, no session restart.

Status shows `on`/`off`, then the model and effort with their source: `(repo)`
from the marker, `(default)` from `hooks/simplify-gate/simplify_gate.py`. The
hooks are registered once per machine when the skill is installed
(`~/code/agentrc/install.py install --skill`); if status says on
but no challenge ever runs, check that first.

`prune` works on the user's gate cache (`$XDG_CACHE_HOME/agentrc/simplify-gate`),
not a repository, and is a dry run until `--apply`. Sessions share one copy of
each cached blob, hard-linked from the `.blobs` store; prune relinks older
private copies to it after checking their content, drops the logs and
unreferenced blobs of settled sessions (no queued batch, finding or error) idle
over `--days`, and drops store copies no session links. It never removes a
session's directory, state, lock files or referenced blobs, skips sessions a
hook holds, and only reports unresolved sessions idle over `--report-days` and
state it cannot read.
