# agentrc

Personal agent config shared by Claude Code and Codex: user-wide instructions,
skills and commands, versioned in git.

```
install.py        symlink chosen parts of this checkout into ~/.claude and ~/.codex
CLAUDE.md         user-wide instructions (~/.codex/AGENTS.md symlinks here too)
skills/           ~/.claude/skills and ~/.codex/skills
agents/           <name>.md for Claude, plus <name>.toml for Codex, into ~/.claude/agents and ~/.codex/agents
hooks/            Claude Code hooks, one folder each with a hooks.json; switched on per repository (see below)
tests/            unit tests for skill scripts, hooks and the installer
.claude-plugin/   plugin and marketplace manifests
```

## Install by symlink (preferred)

Skills are invoked by bare name and edits are live with no reinstall step.
Nothing is installed by default: name what you want, or `all` per category.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
~/code/agentrc/install.py install --skill all --agent all --hook all --claude-md
~/code/agentrc/install.py install --skill read-doc --skill cowork   # cherry-pick
~/code/agentrc/install.py remove --hook simplify-gate
```

`--claude-md` links `~/.claude/CLAUDE.md` and points `~/.codex/AGENTS.md` at
it. Skills link one by one into `~/.claude/skills` and `~/.codex/skills`;
agents link their `.md` into `~/.claude/agents` and `~/.codex/agents` and
their `.toml` into `~/.codex/agents`, where Codex discovers it; hooks link
into `~/.claude/hooks` and register the events from their `hooks.json` in
`~/.claude/settings.json` (first-time backup kept beside it, idempotent).
Those directories stay real directories, so a machine can keep its own
skills, or ones added with `npx skills add`, beside the linked ones. The
installer refuses before touching anything if a destination holds something
that is not a link; `remove` unlinks whatever the named link points to but
never deletes a real file or directory, and removes the CLAUDE.md links only
when they point into this checkout. Rerun after adding a skill, agent or
hook: dead links into this repo are pruned, other people's links stay.

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

`hooks/simplify-gate/simplify_gate.py` snapshots the checkout, every worktree of it, when a
prompt arrives and again when the session stops, and sends the diff to a
read-only `codex exec` YAGNI challenge: at most two rounds per user turn, one
retry on Codex failure, then it lets the stop through with a notice. One
review runs at a time; edits a round did not cover, or made while one was
running, stay queued for the next turn. A peer sharing the checkout
may have made some of the diff; the challenge says so, and Claude rejects
findings on files it neither wrote nor commissioned. Codex never edits.

Install the hooks once per machine, then switch the gate on per repository:

```sh
~/code/agentrc/install.py install --hook simplify-gate     # remove undoes it
cd ~/code/tinyusb && /simplify-gate on                     # or: skills/simplify-gate/scripts/gate.py on
```

Each registered entry runs the linked `simplify-gate` launcher, which costs
one `git rev-parse` in every checkout and
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
