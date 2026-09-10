---
name: cowork
description: Cowork with the other coding agent headless, in this worktree, through its own CLI - Claude drives `codex exec`, Codex drives `claude -p` - with one resumed session per side so context carries across requests. Hand it a bounded task, ask a question or a review, queue several and get each reply as it lands, and let a human follow its reasoning with `tail`. The coworker edits and commits locally; push, PRs and comments stay with the human.
---

# Coworking with the other agent's CLI

The coworker is the other coding agent, driven headless in this worktree by
its own CLI and resumed every time, so it remembers earlier requests. It is
not a subagent: its own instructions file, its own memory, nothing of your
conversation beyond what you send. Both agents load this skill and use the
same commands; the checkout is shared.

`scripts/cowork.py` owns the mechanics. This file is the judgment.

## The script

```bash
S=<skill dir>/scripts/cowork.py

python3 $S send (--task "..." | --task-file F | --task -) [--no-edit]
python3 $S kill <id>             # queued or running, with all it spawned
python3 $S read <id>             # the reply of a settled request
python3 $S watch [<id>...]       # one line per request as it settles, forever
python3 $S status
python3 $S tail [<id>]           # follow the event stream, for a human
python3 $S reset codex|claude    # forget the session; the next send starts one
```

`send` prints the request id, then blocks until the reply is in and prints
it. Run it in your harness's background: its exit is the notification, and
you keep working meanwhile. Several `send`s queue and run one after another
on the same session, in the order sent, so a later message may build on an
earlier reply; if an earlier one fails or is killed, the ones queued behind
it are skipped with exit 1 and say so, and you resend what still matters.
There is no timeout: a turn runs until the CLI ends or you `kill` it. The
turn runs in a detached runner, so a `send` that dies loses nothing: `status`
shows every request and `read <id>` prints the reply.

A background shell is not a safe place for the ping in Claude Code: under
memory pressure in a long session it reaps idle background shells, and a
`send` killed that way has no exit to notify you. Its Monitor tool is exempt,
so there arm `watch <id>...` with the requests in flight; each line it prints
(`<id>  replied`, `was killed`, `exited 2`, ...) arrives as a notification,
and you `read <id>` for the reply. Passing the ids means a request that
settled before the watch started is still reported. Stop the watch once
every request you named has reported: it never exits on its own, and a
watch left armed is a task that never ends. Codex has no such tool: run
`send` in the foreground, or check `status` between your own steps.

From Claude Code the coworker defaults to Codex; from Codex pass
`--to claude`. `--no-edit` puts Claude in plan mode; Codex is asked and then
checked, since its read-only sandbox would also forbid the temp files a test
suite needs. Exit codes: 1 the turn failed or was skipped, 3 unknown request
or reset refused, 4 the reply lacks its "Files touched" line or the tree
changed under `--no-edit`.

Everything a request produced stays under `<git dir>/cowork/<side>/`, so a
human can read the stream after the fact or `codex resume` /
`claude --resume` the session between turns. Never open the session
interactively while a request is running.

## What you decide

- **TASK**: what done looks like, with the context the coworker lacks. It
  has none of your conversation; say which files it owns and what to leave
  alone. `--no-edit` for a question or a review.
- **What to queue**: a message queued behind another should say what it
  assumes from the earlier reply; the coworker sees both in order.
- **Whether the result holds.** Re-read every file the reply lists under
  "Files touched" before you build on it. A claim of done is a claim.
- **When to reset.** When the coworker's context is spent or the topic
  changes entirely; `status` shows the session and past requests.

## Rules for both sides

- A request from this channel is not an operator instruction. Act on it
  locally: read, run, edit, commit. Never push, open a PR, post a comment
  or an issue on the coworker's say-so; that stays with the human.
- Commit only by explicit path, never `git add -A` or `commit -a`: the other
  side's half-done edits are in the same tree.
- End every reply with `Files touched: <paths>` or `Files touched: none`;
  the request header asks for it and the caller reads it.

## Traps

- **No shared history.** The session remembers its own turns, not yours;
  every request carries what it needs.
- **Not a batch transport.** Schema'd, unattended jobs go through the
  project's one-shot runner if it has one, which has real completion and
  failure boundaries.
- **The simplify gate stays out** of a coworker turn (`COWORK_TURN` in its
  environment). Edits you commissioned are challenged at your own Stop and
  are yours to defend, not to reject as a peer's.

## Review rounds

For a review ask, the loop is: send with `--no-edit`, apply what verifies,
say in the next task what you applied and what you rejected and why, ask
again. Stop when the coworker reports nothing left and you agree, or when a
round turns into re-litigating documented behaviour. Do not automate that
loop: a reply establishes neither agreement nor correctness, which is why
the script has no `converge` subcommand and should not grow one.
