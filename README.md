# agentrc

Personal agent config shared by Claude Code and Codex: user-wide instructions,
skills, commands and hooks, versioned in git.

```
CLAUDE.md         user-wide instructions (~/.codex/AGENTS.md symlinks here too)
skills/           ~/.claude/skills
commands/         ~/.claude/commands
hooks/            ~/.claude/hooks, plus hooks.json for the plugin route
.claude-plugin/   plugin and marketplace manifests
```

## Install by symlink (preferred)

Skills are invoked by bare name and edits are live with no reinstall step.

```sh
git clone git@github.com:hathach/agentrc.git ~/code/agentrc
mkdir -p ~/.claude ~/.codex
for d in skills commands hooks; do
  rmdir ~/.claude/$d 2>/dev/null
  ln -s ~/code/agentrc/$d ~/.claude/$d
done
ln -s ~/code/agentrc/CLAUDE.md ~/.claude/CLAUDE.md
ln -s ../.claude/CLAUDE.md ~/.codex/AGENTS.md
```

Hooks are wired in `~/.claude/settings.json`, which is not tracked here:
Claude Code rewrites it on every setting change and it sits next to
credentials. Reference the scripts by absolute path, e.g.
`/home/hathach/.claude/hooks/herdr-agent-state.sh`, and they resolve through the
directory symlink.

Verify with:

```sh
readlink -f ~/.claude/CLAUDE.md ~/.claude/skills ~/.claude/commands ~/.claude/hooks ~/.codex/AGENTS.md
```

## Install as a plugin

The repo is also its own marketplace, so it installs directly:

```
/plugin marketplace add hathach/agentrc
/plugin install agentrc@hathach
```

Skills are then namespaced as `agentrc:<skill>` and hooks come from
`hooks/hooks.json`. Do not combine this with the symlink install on the same
machine or every hook fires twice.
