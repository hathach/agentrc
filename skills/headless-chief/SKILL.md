---
name: headless-chief
description: Launch a headless `chief` (`claude -p --agent chief`) and follow its status lines and final report from the calling session. Use when handing a task worktree, such as a PR to babysit, to a headless chief.
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

1. **Before launching**: take the target from the assignment, formed as the
   Report rule in `agents/chief.md` forms it (`owner/repo#N` and the task),
   asking only when it is ambiguous, and name `--out` after it. A task that publishes to a PR carries
   its grant verbatim, per the README's headless recipe.
2. **Launch** in the foreground: it checks the arguments, starts the
   launcher in its own session, prints its pid and returns. A chief
   runs as long as it needs; a background command would end it at the tool's
   time limit. A refused argument exits 2 at once. The
   `launcher: exit` line in `progress.log` is the end of the run;
   `kill <launcher pid>` stops it early.
3. **Watch** with Monitor on `tail -n +1 -F <dir>/progress.log`, its
   description, on every arm, the target, such as `chief hathach/tinyusb#3988
   babysit`. Each line is
   `<seq> <HH:MM:SS> <text>`: `launcher:` lines (started, session, a parse
   warning, exit), chief's `chief: <event> · ...` lines, and `note:` lines,
   a progress note, joined onto one line, that the model returned as a `thinking` block
   instead of text; a note can carry an event whose status line never came. An `attention`
   line is chief asking for something; relay it. Every line you relay,
   delivered by Monitor or read later, names the target, so parallel chiefs
   stay apart. Monitor expires after 30
   minutes: re-arm with `tail -n +<last seq received + 1> -F`, a few lines
   earlier when unsure.
4. **On exit**, stop the Monitor and read the lines it had not delivered;
   then read `report.md`, chief's final report, or, when there is none, the
   exit line and `stderr.log`. When it can table the session's cost, the
   launcher writes `scripts/run_cost.py`'s full output, the spend by Workflow run,
   stage and model in claude's own dollars, its breakouts and a time table, to
   `cost.md`, and puts its brief form, the Launches and Usage tables, into the
   report before chief's first `##` section. The exit line ends with the total,
   or with `cost none` and why.
   Open the message that reports the exit, with or without `report.md`, with
   the target, and, unasked, relay verbatim the Launches
   table with its State line and chief's `Worth a look:` line, the Usage table
   and chief's decisions for the user; a row that dwarfs the work it did is worth
   a word.
   The exit code, the number on the `launcher: exit` line, says how the run
   ended, not whether the task passed:

   | Exit | Meaning |
   |---|---|
   | 0 | chief ended with a report |
   | 1 | no final result, an error result, or one without text |
   | 2 | bad arguments, `--out` already exists, or a chief already runs in the worktree |
   | 127 | `claude` could not start |
   | other | claude's own status; 128 + N when killed by signal N |

Leave the worktree to chief until it exits. A signal to the launcher is passed
on to chief; the launcher still writes the exit line. A relaunch needs a new
`--out`; its grant follows the README's headless recipe.

The rest of the directory is for autopsy: `stream.jsonl` (every event,
raw), `stderr.log`, and `session`, whose id names the transcript:
`ls ~/.claude/projects/*/<id>.jsonl`.
