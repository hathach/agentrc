# agentrc

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Build](https://github.com/hathach/agentrc/actions/workflows/tests.yml/badge.svg)](https://github.com/hathach/agentrc/actions/workflows/tests.yml)
[![Greptile: The War on Bugs](https://www.greptile.com/badge.svg)](https://www.greptile.com/?utm_source=oss_badge&utm_medium=readme&utm_campaign=greptile_for_open_source)
![CodeRabbit Pull Request Reviews](https://img.shields.io/coderabbit/prs/github/hathach/agentrc?utm_source=oss&utm_medium=github&utm_campaign=hathach%2Fagentrc&labelColor=171717&color=FF570A&link=https%3A%2F%2Fcoderabbit.ai&label=CodeRabbit+Reviews)

Personal agent config shared by Claude Code and Codex, versioned in git. It runs
on Linux and Windows, both tested in CI; [Windows](#windows) lists its setup and
the Linux-only parts. macOS is not supported.

```
install.py     symlinks chosen parts of this checkout into ~/.claude, ~/.codex and ~/.local/bin
instructions/  user.md, the user-wide instructions
CLAUDE.md      instructions for work in this repository
AGENTS.md      -> CLAUDE.md, for Codex
skills/        skills for both agents
agents/        <name>.md for both agents, plus <name>.toml for Codex
hooks/         Claude Code hooks, one folder each with a hooks.json
workflows/     saved workflows (Claude only)
bin/           commands for the human, such as lsusbt
statusline/    the Claude Code status line, its Codex usage fetcher and its session-PR helper
plugins/       herdr plugins, one folder each with a herdr-plugin.toml
lib/           helpers the scripts share through their resolved path: windows.py (Windows runtime), greptile.py
tests/         unit tests for skill scripts, hooks and the installer
```

## Install

Every entry is a symlink or, for herdr plugins, a herdr link, so edits are
live. Each flag takes every entry of its kind; nothing is installed by default.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
~/code/agentrc/install.py install --skill --agent --workflow --bin --claude-md --statusline --herdr-plugin
~/code/agentrc/install.py remove --skill
```

- `--skill`: into `~/.claude/skills` and `~/.codex/skills`. A skill's hook of
  the same name links into `~/.claude/hooks` and its events are registered in
  `~/.claude/settings.json` (backed up the first time).
- `--agent`: the `.md` into `~/.claude/agents` and `~/.codex/agents`, the
  `.toml`, if any, into `~/.codex/agents`.
- `--workflow`: into `~/.claude/workflows`.
- `--bin`: each `bin/` command into `~/.local/bin`, which must be on `PATH`.
  `lsusbt` is `lsusb -t` with device names, serials and the HIL board each
  serial belongs to; its header lists the options.
- `--claude-md`: `instructions/user.md` as `~/.claude/CLAUDE.md`, and
  `~/.codex/AGENTS.md` to it.
- `--statusline`: the `statusline/` files into `~/.claude`, and `statusLine`
  in `~/.claude/settings.json`; `install` refuses if another one is set. The
  command names `$HOME/.claude`, so another `CLAUDE_CONFIG_DIR` can reuse it.
- `--herdr-plugin`: each `plugins/*` folder through `herdr plugin link --enabled`.
  `install` refuses if herdr holds the id from elsewhere; `remove` unlinks only
  plugins linked from this checkout, which needs a running herdr server. herdr
  steps run first, so a refusal of herdr's (such as `min_herdr_version`) changes
  nothing.
  `worktree-children` removes a herdr worktree's `<name>--*` sibling worktrees
  when herdr removes it, keeping and reporting any that hold work, and deletes
  the removed worktrees' branches whose commits are on their remote or a PR
  head (its docstring lists the checks); `worktree_children.py cleanup <parent>`
  retries, with the `--branch` arguments its log names.

The target directories stay real, so local entries sit beside the links.
`install` refuses before touching anything if a target is a file or a
nonempty directory; `remove` never deletes one, and leaves CLAUDE.md links
that point elsewhere. Rerun after adding an entry: dead links into this repo
are pruned.

Project repos such as tinyusb call these skills by bare name and expect this
install.

### Windows

Claude Code on Windows runs the Bash tool, hooks and the status line through
Git Bash (or MSYS2), and the scripts run on native Windows from there. They read
and write UTF-8 themselves, so no `-X utf8` or `PYTHONUTF8` is needed. Bash's
`PATH` needs:

- a `python3` that is a native CPython 3.12+ (python.org's install manager or
  MSYS2's mingw build), not the WindowsApps alias and not MSYS2's
  `/usr/bin/python3`, for the workflows' script calls and the simplify gate's
  wrapper; PyYAML in it for the tests, and for `pr-babysit`'s `harvest.py`, which
  `pr-review` also imports, to date a head by its pull_request runs (without it
  they go unread and the harvest says so). `install.py` registers a Python hook as
  `'<the python that ran install.py>' '<hook>.py'`, since a hook that cannot
  start lets the tool call through; rerun it after replacing that Python, and
  name the interpreter the same way in the credit guard's recipe below.
- `bash`, `cygpath` and `git` from Git for Windows or MSYS2: from Python a bare
  `bash` is System32's WSL launcher, so scripts look it up on `PATH`.
  `chief_guard.py` maps `/tmp`-style paths through `cygpath` and denies them
  without it; it also denies `cmd`, PowerShell and `wsl` outright.
- poppler's `pdfinfo` and `pdftotext` for `read-doc` (MSYS2
  `mingw-w64-x86_64-poppler`); `pdftotext` is taken from beside `pdfinfo`, so
  Git's xpdf earlier on `PATH` does not matter.

Claude Code sets `NoDefaultCurrentDirectoryInExePath`; set it too when running
the scripts from another shell, or Windows runs a `git.exe` or `gh.exe` from the
current directory, a PR checkout for many of them.

`pr-babysit`'s build check puts a temp directory into the build command
unquoted, so it refuses a temp dir with a blank or shell syntax in it, as a
profile path such as `C:\Users\First Last` gives; point `TMP` and `TEMP` at a
plain path such as `C:\tmp` there.

`install.py` creates symlinks, which needs Developer Mode or an elevated shell.
Not on Windows yet; [#148](https://github.com/hathach/agentrc/issues/148) has
the skipped tests and notes for a port:

- `hil`'s rig-host scripts (`hil_lock.py`, `hil_reserve.py`, `usb_observe.py`,
  `hil_process.py`, `hil_farm_check.py`, `hil_tt.py`, `hil_usbtest.py`) and
  `usb-kernel-recover`'s `usb_recover.sh`: POSIX `flock` and `fork`, `/proc`
  and sysfs.
- `hil`'s `hil_report_notice.py` locks the report writer with `msvcrt` on Windows,
  but its tests and `hil_report_core.py`'s use `fcntl` and skip there.
- `lsusbt`: sysfs.
- `usb-kernel-debug`: kernel dynamic debug and usbmon, which have no Windows
  counterpart to port to.
- the capture scripts of `usb-sniffer` (`sniff.py`) and `target-debug`
  (`etm_capture.py`, `dual_capture.py`): usbfs, `/proc` and POSIX process
  control.
- `target-debug`'s `sysview_record.py`, which refuses there: it drives SystemView
  under Xvfb and tracks its processes through `/proc` and process groups.
- `target-debug`'s `esp_session.py`, which refuses there: it detaches openocd
  as a POSIX session and identifies it through `/proc` (#130).
- `install.py --herdr-plugin` refuses on Windows; `worktree-remove` has no
  Windows test.

The hardware-debug scripts run on Windows but are unverified on a board there
(#130), and their tests skip the cases that drive POSIX stub executables or
ptys. Other skips are for `/proc`, permission bits or file names NTFS refuses.

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
| [`ci-rerun`](skills/ci-rerun/SKILL.md) | read a failed CircleCI job's or Read the Docs build's log, rerun it once after an infra failure | `circleci.py log <job>`, `rerun <job>...`, `rtd.py log <url>` |
| [`headless-chief`](skills/headless-chief/SKILL.md) | launch a headless `chief` and follow its status lines while it runs, its report when it exits | `chief_run.py --out <dir> --worktree <wt> --task-file <task>` |
| [`worktree-remove`](skills/worktree-remove/SKILL.md) | remove a herdr worktree when its task is done: check it, gate on pending follow-ups and PR state, then herdr removes it and the `worktree-children` plugin its siblings and branches | `worktree_remove.py [PATH] --dry-run [--pending <item>]`, `--proceed <token>` |

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

Firmware footprint; the project supplies the launcher, its builds and its CI
baseline through the `Code size` section of its `Build contract:`:

| Skill | Use it to | Start with |
|---|---|---|
| [`code-size`](skills/code-size/SKILL.md) | measure flash/RAM per board, file, section or symbol, and diff it against a base ref or CI's stored snapshots | `<launcher> diff -b <board>` |

Firmware and USB debugging on real hardware; `target-debug` maps which one
answers what, and the project supplies the build variant and board locks
through its `Build contract:` and `HIL contract:` lines:

| Skill | Use it to | Start with |
|---|---|---|
| [`hil`](skills/hil/SKILL.md) | run a project's HIL tests and judge their results: contract sections, board locks, verdicts, retries; a farm check (each board's probe on the bus and resetting it), never a test verdict | the project's `HIL contract:` page |
| [`target-debug`](skills/target-debug/SKILL.md) | explain what the firmware did: GDB autopsy, faults, RAM trace, PC sampling, SWO, ETM instruction trace (J-Trace + Ozone), SEGGER SystemView scheduling profiles; Espressif chips over USB-Serial-JTAG or pad JTAG with openocd-esp32 (read its `espressif.md` first) | `pc_sample.py --probe <uid> --device <dev> --interface swd --speed 4000 --elf <elf>` |
| [`rtt`](skills/rtt/SKILL.md) | console or log over a debug probe (SEGGER RTT), live or post-mortem | `rtt.py --backend jlink --probe <serial> --device <dev> --interface swd --speed auto --seconds 20` |
| [`usb-kernel-debug`](skills/usb-kernel-debug/SKILL.md) | capture Linux host URBs (usbmon); kernel dynamic debug on a host or gadget | `usbcap.py <vid:pid> 10`; `sudo usb_dyndbg.sh on\|off usbcore xhci_hcd` |
| [`usb-sniffer`](skills/usb-sniffer/SKILL.md) | what crossed D+/D- with the ataradov sniffer, where usbmon cannot see | `sniff.py capture raw.pcapng --port <port> --seconds 30` |

## Simplify gate (per repository)

`hooks/simplify-gate` snapshots the checkout, the worktrees nested in it and its `<checkout>--*` sibling worktrees
(linked worktrees of any repository; the user rules reserve that prefix for them) when a prompt
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
into its worktree, a guard against mistakes, not isolation: it judges only the command
families it knows, and a program outside them passes by design, even one that runs its
arguments as a command. Its direct Codex exchanges go through `agents/coworker.md`, the
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

From a herdr worktree the task worktree is its sibling `<checkout>--<branch>`
instead (`instructions/user.md`, herdr worktrees).

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
the human's request to launch chief to babysit a PR, their yes to the offer, or
their request to open a new PR under the user-wide rule, is the grant, with no
further question. Include that message verbatim (with the offer it answered
when it is a yes, and any later restriction), the PR resolved from it
(repository, PR URL, head repository and branch, expected HEAD, worktree) and
the task scope, and, when a pr-babysit launch continues an
earlier chief's run, that run's last reported `stateRef`. The task scope names
the review scope: the whole PR, or takeover-only (earlier PR commits are context,
the takeover HEAD is the task base) for a task limited to the repairs it
commissions; an offer to launch names that choice when the human has not, and an
unspecified review scope defaults to the whole PR; ask only when the PR,
repository or worktree is ambiguous. A relaunch for the same PR task copies
the grant verbatim from the earlier launch task, without asking again, until
the task is done, the PR changes, or the human narrows or withdraws it; a
later restriction rides along verbatim. A merge is granted only by the
human's own words asking to merge that PR, under chief's merge exception: the
task then also names the base branch and carries `Merge grant: yes — "<the
words>"`, else `Merge grant: none`, a line a relaunch copies too. A PR review
submits under its own PR review exception, as `skills/pr-review/SKILL.md` asks
it; its pending reviews need no exchange, under chief's standing exception.
Leave the checkout to chief until it exits.

## Tests

```sh
python -X utf8 -m unittest discover -s tests
```

Needs PyYAML. The pre-commit hook (`pre-commit install`) runs the same tests in parallel under
pytest-xdist, `python -X utf8 -m pytest -n 8 --dist worksteal tests`, which keeps it well inside
pr-babysit's 540 s hook deadline; CI runs them that way too, sharded across jobs
(`.github/workflows/tests.yml`), and `--shard I/K` (`tests/conftest.py`) runs one part of it.
Where Git for Windows has `core.fsmonitor=true` (an installer option), each
temporary test repository leaves a `git fsmonitor--daemon` behind; run the suite
with `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false`.
