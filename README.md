# agentrc

Personal agent config shared by Claude Code and Codex: user-wide instructions,
skills and commands, versioned in git.

```
install.sh        symlink this checkout into ~/.claude and ~/.codex
CLAUDE.md         user-wide instructions (~/.codex/AGENTS.md symlinks here too)
skills/           ~/.claude/skills
commands/         ~/.claude/commands
.claude-plugin/   plugin and marketplace manifests
```

## Install by symlink (preferred)

Skills are invoked by bare name and edits are live with no reinstall step.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
~/code/agentrc/install.sh
```

This links `CLAUDE.md`, `skills/` and `commands/` into `~/.claude`, points
`~/.codex/AGENTS.md` at the same `CLAUDE.md`, and links each skill into
`~/.codex/skills` (Codex scans that directory, not `~/.claude/skills`, and
manages `.system` inside it, so skills are linked one by one). Rerun it after
adding a skill so Codex picks it up.

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
