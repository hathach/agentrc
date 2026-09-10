# agentrc

Personal agent config shared by Claude Code and Codex: user-wide instructions,
skills and commands, versioned in git.

```
install.sh        symlink this checkout into ~/.claude and ~/.codex
CLAUDE.md         user-wide instructions (~/.codex/AGENTS.md symlinks here too)
skills/           ~/.claude/skills
commands/         ~/.claude/commands
hooks/            Claude Code hooks, switched on per repository (see below)
tests/            unit tests for skill scripts and hooks
.claude-plugin/   plugin and marketplace manifests
```

## Install by symlink (preferred)

Skills are invoked by bare name and edits are live with no reinstall step.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
~/code/agentrc/install.sh
```

This links `CLAUDE.md` and `commands/` into `~/.claude`, points
`~/.codex/AGENTS.md` at the same `CLAUDE.md`, and links each skill one by one
into `~/.claude/skills` and `~/.codex/skills`. Those directories stay real
directories, so a machine can keep its own skills, or ones added with `npx
skills add`, beside the linked ones; only links into this repo are pruned when a
skill is removed. Rerun it after adding a skill.

Project repos such as tinyusb reference these skills by bare name only, e.g.
`read-doc`, and expect this install to have run; without it their agents take
the "skill unavailable" branch.

## Install as a plugin

The repo is also its own marketplace, so it installs directly:

```
/plugin marketplace add hathach/agentrc
/plugin install agentrc@hathach
```

Skills are then namespaced as `agentrc:<skill>`. Do not combine this with the
symlink install on the same machine or every skill shows up twice.

## Simplify gate (per repository)

`hooks/simplify_gate.py` records the files a Claude Code session edits (main
session and its subagents alike) and, when the session stops, sends that patch
to a read-only `codex exec` YAGNI challenge: at most two rounds per user turn,
one retry on Codex failure, then it lets the stop through with a notice. Codex
never edits; Claude applies or rejects each finding.

Install the hooks once per machine, then switch the gate on per repository:

```sh
python3 ~/code/agentrc/hooks/simplify_gate.py --install    # --remove undoes it
cd ~/code/tinyusb && /simplify-gate on                     # or: skills/simplify-gate/scripts/gate.py on
```

`--install` merges five entries into `~/.claude/settings.json` (backup kept
beside it, idempotent, replaces older entries of its own). Each entry runs
`hooks/simplify-gate`, which costs one `git rev-parse` in every checkout and
starts the Python gate only where `<git common dir>/simplify-gate` exists, so
one marker covers a repository and all of its worktrees. `/simplify-gate status`
prints the state with the effective model and effort; `on --model M --effort E`
stores overrides in the marker, the defaults are the constants at the top of
`simplify_gate.py`. Per-session state lives under
`~/.cache/agentrc/simplify-gate/`.

## Tests

```sh
python3 -m unittest discover -s tests
```

Needs PyYAML. The same command runs from the pre-commit hook (`pre-commit install`).
