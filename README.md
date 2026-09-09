# agentrc

Personal agent config shared by Claude Code and Codex: user-wide instructions,
skills and commands, versioned in git.

```
CLAUDE.md         user-wide instructions (~/.codex/AGENTS.md symlinks here too)
skills/           ~/.claude/skills
commands/         ~/.claude/commands
.claude-plugin/   plugin and marketplace manifests
```

## Install by symlink (preferred)

Skills are invoked by bare name and edits are live with no reinstall step.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
mkdir -p ~/.claude ~/.codex
for d in skills commands; do
  rmdir ~/.claude/$d 2>/dev/null
  ln -s ~/code/agentrc/$d ~/.claude/$d
done
ln -s ~/code/agentrc/CLAUDE.md ~/.claude/CLAUDE.md
ln -s ../.claude/CLAUDE.md ~/.codex/AGENTS.md
```

Verify with:

```sh
readlink -f ~/.claude/CLAUDE.md ~/.claude/skills ~/.claude/commands ~/.codex/AGENTS.md
```

## Install as a plugin

The repo is also its own marketplace, so it installs directly:

```
/plugin marketplace add hathach/agentrc
/plugin install agentrc@hathach
```

Skills are then namespaced as `agentrc:<skill>`. Do not combine this with the
symlink install on the same machine or every skill shows up twice.
