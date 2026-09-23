# agentrc

Personal agent config shared by Claude Code and Codex, versioned in git.

```
install.py   symlinks chosen parts of this checkout into ~/.claude and ~/.codex
CLAUDE.md    user-wide instructions, also ~/.codex/AGENTS.md
skills/      skills for both agents
agents/      <name>.md for both agents, plus <name>.toml for Codex
hooks/       Claude Code hooks, one folder each with a hooks.json
workflows/   saved workflows (Claude only)
tests/       unit tests for skill scripts, hooks and the installer
```

## Install

Every entry is a symlink, so edits are live. Each flag takes every entry of
its kind; nothing is installed by default.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
~/code/agentrc/install.py install --skill --agent --workflow --claude-md
~/code/agentrc/install.py remove --skill
```

- `--skill`: into `~/.claude/skills` and `~/.codex/skills`. A skill's hook of
  the same name links into `~/.claude/hooks` and its events are registered in
  `~/.claude/settings.json` (backed up the first time).
- `--agent`: the `.md` into `~/.claude/agents` and `~/.codex/agents`, the
  `.toml`, if any, into `~/.codex/agents`.
- `--workflow`: into `~/.claude/workflows`.
- `--claude-md`: `~/.claude/CLAUDE.md`, and `~/.codex/AGENTS.md` to it.

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
| [`ci-rerun`](skills/ci-rerun/SKILL.md) | read a failed CircleCI job's log, rerun its failed jobs once after an infra failure | `circleci.py log <job>`, `rerun <job>...` |
| [`headless-chief`](skills/headless-chief/SKILL.md) | launch a headless `chief` and follow its status lines while it runs, its report when it exits | `chief_run.py --out <dir> --worktree <wt> --task-file <task>` |

Hardware documentation, from the Calibre library at `~/Documents/calibre-library`
(`CALIBRE_LIBRARY` overrides):

| Skill | Use it to | Start with |
|---|---|---|
| [`read-doc`](skills/read-doc/SKILL.md) | look up hardware manuals, specs and errata in Calibre instead of answering from memory | `search.py --kind reference-manual stm32h7` |
| [`download-doc`](skills/download-doc/SKILL.md) | fetch vendor datasheets, manuals and errata into the library, refresh stale revisions | `sync.py st --family STM32H7 --types datasheet,errata` (dry run; `--apply` imports) |

Board designs, from EAGLE and KiCad schematics in `adafruit/MBAdafruitBoards`,
`hathach/pcb` or a directory you name:

| Skill | Use it to | Start with |
|---|---|---|
| [`read-pcb`](skills/read-pcb/SKILL.md) | answer board-wiring questions (which pin, net, part or revision) from the schematic | `pcb.py find feather rp2040`, then `net <sch> NEOPIX` |

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

## Chief session

`agents/chief.md` is a dispatch-only main session with no file or shell tools:
every read, edit, build and review goes to the repository's agents, skills and
workflows. Its direct Codex exchanges go through `agents/coworker.md`, the
`cowork.py` transport. `workflows/code-audit.js` is its saved review: one `code-verifier`
per directory x dimension, then `finding-verifier` on every finding
(`args: { dirs, dimensions }`, both required). Start it inside the task
worktree:

```sh
~/code/agentrc/install.py install --agent --workflow --skill
git worktree add .worktrees/<branch> -b <branch> <base> && cd .worktrees/<branch> && claude --agent chief
```

Headless, launch it through the [`headless-chief`](skills/headless-chief/SKILL.md)
skill, which runs `claude -p --agent chief` and gives the caller chief's status
lines while it runs and its report when it exits.

For headless PR publishing, follow `agents/chief.md`'s Authorization exception
before launching. Include the named PR, head repository and branch, expected
HEAD, worktree, task scope, and the verbatim authorization exchange in the
task. Leave the checkout to chief until it exits. Each new chief invocation
requires a fresh exchange.

## Tests

```sh
python -X utf8 -m unittest discover -s tests
```

Needs PyYAML; the pre-commit hook runs the same command (`pre-commit install`).
