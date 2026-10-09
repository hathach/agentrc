# agentrc

Personal agent config shared by Claude Code and Codex, versioned in git.

```
install.py     symlinks chosen parts of this checkout into ~/.claude and ~/.codex
instructions/  user.md, the user-wide instructions
CLAUDE.md      instructions for work in this repository
AGENTS.md      -> CLAUDE.md, for Codex
skills/        skills for both agents
agents/        <name>.md for both agents, plus <name>.toml for Codex
hooks/         Claude Code hooks, one folder each with a hooks.json
workflows/     saved workflows (Claude only)
statusline/    the Claude Code status line, its Codex usage fetcher and its session-PR helper
tests/         unit tests for skill scripts, hooks and the installer
```

## Install

Every entry is a symlink, so edits are live. Each flag takes every entry of
its kind; nothing is installed by default.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
~/code/agentrc/install.py install --skill --agent --workflow --claude-md --statusline
~/code/agentrc/install.py remove --skill
```

- `--skill`: into `~/.claude/skills` and `~/.codex/skills`. A skill's hook of
  the same name links into `~/.claude/hooks` and its events are registered in
  `~/.claude/settings.json` (backed up the first time).
- `--agent`: the `.md` into `~/.claude/agents` and `~/.codex/agents`, the
  `.toml`, if any, into `~/.codex/agents`.
- `--workflow`: into `~/.claude/workflows`.
- `--claude-md`: `instructions/user.md` as `~/.claude/CLAUDE.md`, and
  `~/.codex/AGENTS.md` to it.
- `--statusline`: the `statusline/` files into `~/.claude`, and `statusLine`
  in `~/.claude/settings.json`; `install` refuses if another one is set. The
  command names `$HOME/.claude`, so another `CLAUDE_CONFIG_DIR` can reuse it.

The target directories stay real, so local entries sit beside the links.
`install` refuses before touching anything if a target is a file or a
nonempty directory; `remove` never deletes one, and leaves CLAUDE.md links
that point elsewhere. Rerun after adding an entry: dead links into this repo
are pruned.

Project repos such as tinyusb call these skills by bare name and expect this
install.

## Skills

Agent collaboration and PRs:

| Skill | Use it to | Start with |
|---|---|---|
| [`cowork`](skills/cowork/SKILL.md) | hand the other agent (Codex or Claude) a task, question or review headless, in resumed lanes | `cowork.py send --lane L --task -` |
| [`herdr-peer`](skills/herdr-peer/SKILL.md) | the same with the agent in the neighbouring Herdr pane, watched live; needs `HERDR_ENV=1` | `peer.py peers`, `send --to <pane> --files none --task -`, `read --from <pane> --for <id>` |
| [`simplify-gate`](skills/simplify-gate/SKILL.md) | switch the per-repository Codex YAGNI check at Stop (below) | `/simplify-gate status\|on\|off` |
| [`pr-reply`](skills/pr-reply/SKILL.md) | post PR review replies from a manifest, read each back, resolve verified review threads | `reply.py --pr N --manifest f.json` |
| [`followup-issue`](skills/followup-issue/SKILL.md) | open a follow-up issue, or comment new evidence on one that covers the topic, read it back, never post twice | `publish.py create --repo o/r --allow-repo o/r --title t --body-file f` |
| [`pr-babysit`](skills/pr-babysit/SKILL.md) | the fact collectors the pr-babysit workflow runs before it commits a fix | `preflight.py --pr N`, `hooks.py <path>...` |
| [`pr-review`](skills/pr-review/SKILL.md) | review someone else's PR: pin it, pick rig boards, run the pr-review workflow, leave it as a pending review for you to submit (or submit it under auto-post); re-review after a push | `prepare.py --pr N` |
| [`ci-rerun`](skills/ci-rerun/SKILL.md) | read a failed CircleCI job's log, rerun its failed jobs once after an infra failure | `circleci.py log <job>`, `rerun <job>...` |
| [`headless-chief`](skills/headless-chief/SKILL.md) | launch a headless `chief` and follow its status lines while it runs, its report when it exits | `chief_run.py --out <dir> --worktree <wt> --task-file <task>` |
| [`worktree-reset`](skills/worktree-reset/SKILL.md) | recycle a worktree slot once its work merged: next numbered branch at the default branch, cowork lanes reset, ignored files kept | `worktree_reset.py [--dry-run] [--pending <item>]` |

Hardware documentation, from the Calibre library at `~/Documents/calibre-library`
(`CALIBRE_LIBRARY` overrides):

| Skill | Use it to | Start with |
|---|---|---|
| [`read-doc`](skills/read-doc/SKILL.md) | look up hardware manuals, specs and errata in Calibre instead of answering from memory | `search.py --kind reference-manual stm32h7` |
| [`download-doc`](skills/download-doc/SKILL.md) | fetch vendor datasheets, manuals and errata into the library, refresh stale revisions | `sync.py st --family STM32H7 --types datasheet,errata` (dry run; `--apply` imports) |

Board designs, from KiCad schematics in `hathach/pcb`, or EAGLE and KiCad
schematics in a directory you name:

| Skill | Use it to | Start with |
|---|---|---|
| [`read-pcb`](skills/read-pcb/SKILL.md) | answer board-wiring questions (which pin, net, part or revision) from the schematic | `pcb.py find --in <dir> <board>`, then `net <sch> <net>` |

Firmware and USB debugging on real hardware; `target-debug` maps which one
answers what, and the project supplies the build variant and board locks
through its `Build contract:` and `HIL contract:` lines:

| Skill | Use it to | Start with |
|---|---|---|
| [`target-debug`](skills/target-debug/SKILL.md) | explain what the firmware did: GDB autopsy, faults, RAM trace, PC sampling, SWO | `pc_sample.py --probe <uid> --device <dev> --interface swd --speed 4000 --elf <elf>` |
| [`esp-target-debug`](skills/esp-target-debug/SKILL.md) | the same on ESP32-S3/P4 built-in USB-Serial-JTAG (other gdb, openocd fork) | read its PHY map first |
| [`rtt`](skills/rtt/SKILL.md) | console or log over a debug probe (SEGGER RTT), live or post-mortem | `rtt.py --backend jlink --probe <serial> --device <dev> --interface swd --speed auto --seconds 20` |
| [`etm-trace`](skills/etm-trace/SKILL.md) | instruction-level trace through a J-Trace and Ozone: hot functions, coverage, history before a fault | `etm_capture.py --jdebug <ref.jdebug> --elf <elf> --probe jtrace --duration-ms 10000 --out <dir>`, then `etm_profile.py <dir> --elf <elf>` |
| [`usb-kernel-debug`](skills/usb-kernel-debug/SKILL.md) | capture Linux host URBs (usbmon); kernel dynamic debug on a host or gadget | `usbcap.py <vid:pid> 10`; `sudo usb_dyndbg.sh on\|off usbcore xhci_hcd` |
| [`usb-sniffer`](skills/usb-sniffer/SKILL.md) | what crossed D+/D- with the ataradov sniffer, where usbmon cannot see | `sniff.py capture raw.pcapng --port <port> --seconds 30` |

## Simplify gate (per repository)

`hooks/simplify-gate` snapshots the checkout and the worktrees nested in it when a prompt
arrives and when the session stops, then sends the diff to a read-only
`codex exec` YAGNI challenge at Stop: at most two rounds per user turn, one
retry on Codex failure, then the stop goes through with a notice. One review
runs at a time; edits it did not cover wait for the next turn. The challenge notes that
a peer sharing the checkout may have made part of the diff, and Claude rejects
findings on files it neither wrote nor commissioned.

`--skill` registers the hook; switch the gate on per repository with
`/simplify-gate on` in a Claude session there, or:

```sh
cd ~/code/tinyusb && ~/.claude/skills/simplify-gate/scripts/gate.py on
```

The hook costs one `git rev-parse` per checkout and starts the gate only where
`<git common dir>/simplify-gate` exists, so one marker covers a repository and
its worktrees. `/simplify-gate status` prints the state with the effective
model and effort; `on --model M --effort E` overrides the defaults at the top
of `simplify_gate.py`. Session state lives under
`~/.cache/agentrc/simplify-gate/`.

## Credit guard (per config dir)

`hooks/credit-guard` keeps a session on an account with extra usage enabled
from spending credits. At session start it asks `/api/oauth/usage` once
whether credits are enabled; if not, it stays off for that session. Otherwise
it blocks the prompt, or denies the next tool call and ends the session, once
any plan limit reaches 100%. Usage is re-read on a window that shrinks with
headroom and burn rate (`HEADROOM` in `credit_guard.py`, tuned from
`credit-guard/readings.log`); a read that fails, or a payload it cannot
parse, keeps the last reading, and blocks only when there is none. It cannot
stop a model call already running, a subagent's next turn, or a hook that
Claude Code kills on timeout, and it does not know the session's model.

`install.py` does not register it. Register it per config dir that needs it
(`~/.claude-ada` here); the recipe refuses a dir that already has it:

```sh
d=~/.claude-ada; ! grep -q credit_guard.py $d/settings.json && mkdir -p $d/hooks &&
ln -sfn ~/code/agentrc/hooks/credit-guard $d/hooks/credit-guard &&
jq --arg c "$d/hooks/credit-guard/credit_guard.py" --slurpfile h ~/code/agentrc/hooks/credit-guard/hooks.json \
  '.hooks = reduce ($h[0] | to_entries[]) as $e (.hooks // {}; .[$e.key] += [$e.value[] | .hooks |= map(.command = $c)])' \
  $d/settings.json > $d/settings.json.new && chmod --reference=$d/settings.json $d/settings.json.new &&
mv $d/settings.json.new $d/settings.json
```

## Chief session

`agents/chief.md` is the orchestrating main session: it reads and runs short
chores itself, and every worktree edit, long build or investigation and review goes
to the repository's agents, skills and workflows. `hooks/headless-chief/chief_guard.py`
denies chief's own direct `git`/`gh` writes, workflow-only publishers and writes
into its worktree, a guard against mistakes, not isolation. Its direct Codex exchanges go through `agents/coworker.md`, the
`cowork.py` transport. `workflows/code-audit.js` is its saved review: one `code-verifier`
per directory x dimension, then `finding-verifier` on every finding (on Sonnet for a
scanner-labelled nit)
(`args: { dirs, dimensions, diff? }`, the first two required; `diff: { base, head }`
narrows it to a change); the verifier sets each finding's level by the one
severity scale in `agents/finding-verifier.md`'s Severity section.
`workflows/pr-review.js` reviews a pinned PR head through it. Start it inside the task
worktree:

```sh
~/code/agentrc/install.py install --agent --workflow --skill
git worktree add .worktrees/<branch> -b <branch> <base> && cd .worktrees/<branch> && claude --agent chief
```

Headless, launch it through the [`headless-chief`](skills/headless-chief/SKILL.md)
skill, which runs `claude -p --agent chief` and gives the caller chief's status
lines while it runs and its report when it exits.

A branch whose `.claude/workflows` or `.claude/agents` entries differ from the
same-named agentrc ones installed would run them in their place, since project
scope wins; merge the default branch first. The launcher refuses such a
worktree, and the `headless-chief` hook (`--skill` registers it) denies every
`Workflow` and `Agent` call in it, interactive sessions included; the chief
prompt itself loads before any tool call and is not covered.

For headless PR publishing, follow `agents/chief.md`'s Authorization exception:
the human's request to launch chief to babysit a PR, or their yes to the
offer, is the grant, with no further question. Include that message verbatim
(with the offer it answered when it is a yes, and any later restriction), the
PR resolved from it (repository, PR URL, head repository and branch, expected
HEAD, worktree) and the task scope, and, when a pr-babysit launch continues an
earlier chief's run, that run's last reported `stateRef`; ask only when the PR,
repository or worktree is ambiguous. A relaunch for the same PR task copies
the grant verbatim from the earlier launch task, without asking again, until
the task is done, the PR changes, or the human narrows or withdraws it; a
later restriction rides along verbatim. A PR review submits under its own PR
review exception, as `skills/pr-review/SKILL.md` asks it; its pending reviews
need no exchange, under chief's standing exception. Leave the checkout to chief
until it exits.

## Tests

```sh
python -X utf8 -m unittest discover -s tests
```

Needs PyYAML; the pre-commit hook runs the same command (`pre-commit install`).
