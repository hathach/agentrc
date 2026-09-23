---
name: headless-chief
description: Launch a headless `chief` (`claude -p --agent chief`) and follow it from the calling session - its `chief:` status lines arrive as a numbered progress log while it runs, its final report as a file when it exits. Use when handing a task worktree, such as a PR to babysit, to a headless chief.
---

# Headless chief

`scripts/chief_run.py` runs chief with streaming output and keeps what the
caller needs in one new directory per run. Chief begins a message that reports a change
with a status line (the rule in `agents/chief.md`); the script copies those
first lines to `progress.log`, so the caller tails a file.

```bash
R=~/.claude/skills/headless-chief/scripts/chief_run.py
python3 $R --out <new dir> --worktree <task worktree> --task-file <task.md> \
  --permission-mode bypassPermissions
```

1. **Before launching**: a task that publishes to a PR carries its
   authorization exchange verbatim, per the README's headless recipe.
2. **Launch** as a tracked background command, never `nohup ... &` (it
   outlives the tool shell untracked). Its exit notification is the end of
   the run.
3. **Watch** with Monitor on `tail -n +1 -F <dir>/progress.log`. Each line is
   `<seq> <HH:MM:SS> <text>`: `launcher:` lines (started, session, a parse
   warning, exit), chief's `chief: <event> · ...` lines, and `note:` lines,
   a progress note, joined onto one line, that the model returned as a `thinking` block
   instead of text; a note can carry an event whose status line never came. An `attention`
   line is chief asking for something; relay it. Monitor expires after 30
   minutes: re-arm with `tail -n +<last seq received + 1> -F`, a few lines
   earlier when unsure.
4. **On exit**, stop the Monitor and read the lines it had not delivered;
   then read `report.md`, chief's final report, or, when there is none, the
   exit line and `stderr.log`. The exit code says how the run ended, not
   whether the task passed:

   | Exit | Meaning |
   |---|---|
   | 0 | chief ended with a report |
   | 1 | no final result, an error result, or one without text |
   | 2 | bad arguments, `--out` already exists, or a chief already runs in the worktree |
   | 127 | `claude` could not start |
   | other | claude's own status; 128 + N when killed by signal N |

Leave the worktree to chief until it exits. A signal to the launcher is passed
on to chief; the launcher still writes the exit line. A relaunch needs a new
`--out` and, when it publishes to a PR, a fresh authorization exchange.

The rest of the directory is for autopsy: `stream.jsonl` (every event,
raw), `stderr.log`, and `session`, whose id names the transcript:
`ls ~/.claude/projects/*/<id>.jsonl`.
