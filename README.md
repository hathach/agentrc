# agentrc

Personal agent config shared by Claude Code and Codex: user-wide instructions,
skills, commands and hooks, versioned in git and installed into `~/.claude` by
symlink.

Layout mirrors a Claude Code plugin, so adding `.claude-plugin/plugin.json`
later would turn it into one without moving files. It is deliberately not
installed as a plugin: personal skills are invoked by bare name instead of
`plugin:skill`, and edits are live with no reinstall step.

```
CLAUDE.md   user-wide instructions (~/.codex/AGENTS.md symlinks here too)
skills/     ~/.claude/skills
commands/   ~/.claude/commands
hooks/      ~/.claude/hooks
```

## Install on a new machine

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
