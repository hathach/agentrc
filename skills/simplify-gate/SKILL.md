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
marker. The hooks run at UserPromptSubmit and Stop, so a change takes effect at the next prompt or Stop, no
session restart; switched on mid-turn in a session with no saved baseline, the gate takes its baseline at that Stop
and says the turn's earlier edits are not reviewed.

Status shows `on`/`off`, then the model and effort with their source: `(repo)`
from the marker, `(default)` from `hooks/simplify-gate/simplify_gate.py`. The
hooks are registered in `~/.claude/settings.json` when the agentrc checkout's `install.py install --skill` installs
the skill, or in `<dir>/.claude/settings.local.json` for `--project <dir>`; if status says on but no challenge ever
runs, check that first.

`prune` works on the user's gate cache (`$XDG_CACHE_HOME/agentrc/simplify-gate`),
not a repository, and is a dry run until `--apply`. Sessions share one copy of
each cached blob, hard-linked from the `.blobs` store; prune relinks older
private copies to it after checking their content, drops the logs and
unreferenced blobs of settled sessions (no queued batch, finding or error) idle
over `--days`, and drops store copies no session links. A settled session whose
last turn closed also loses its checkout snapshot, and with it the blobs only
that snapshot held; its next prompt takes a new one. One whose turn was
interrupted keeps its snapshot, the baseline its edits are reviewed against,
and is listed. It never removes a session's directory or lock files, nor a blob
its state still references, skips sessions a hook holds, and only reports
unresolved sessions idle over `--report-days` and state it cannot read.
