#!/bin/sh
# Link this checkout into ~/.claude and ~/.codex. Idempotent; rerun after adding a skill.
set -eu
repo="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# A destination may be absent, an empty directory, or a symlink we can replace.
# Anything else is the user's own content: refuse before touching anything.
claimable() {
  [ -L "$1" ] || [ ! -e "$1" ] && return 0
  [ -d "$1" ] && [ -z "$(ls -A "$1")" ] && return 0
  echo "install.sh: $1 exists and is not a symlink; move it aside first" >&2
  return 1
}
dests="$HOME/.claude/skills $HOME/.claude/commands $HOME/.claude/CLAUDE.md $HOME/.codex/AGENTS.md"
for s in "$repo"/skills/*/; do
  dests="$dests $HOME/.codex/skills/$(basename "$s")"
done
for p in $dests; do claimable "$p"; done
for p in $dests; do [ -d "$p" ] && [ ! -L "$p" ] && rmdir "$p"; done

mkdir -p ~/.claude ~/.codex/skills
ln -sfn "$repo/skills" ~/.claude/skills
ln -sfn "$repo/commands" ~/.claude/commands
ln -sfn "$repo/CLAUDE.md" ~/.claude/CLAUDE.md
ln -sfn ../.claude/CLAUDE.md ~/.codex/AGENTS.md

# Codex scans ~/.codex/skills, not ~/.claude/skills, and owns the .system dir
# there, so link each skill individually instead of the directory.
for s in "$repo"/skills/*/; do
  ln -sfn "${s%/}" ~/.codex/skills/"$(basename "$s")"
done
# Prune links of ours whose skill left the repo; leave other people's links alone.
for l in ~/.codex/skills/*; do
  [ -L "$l" ] || continue
  case "$(readlink "$l")" in "$repo"/skills/*) [ -e "$l" ] || rm "$l" ;; esac
done

readlink -f ~/.claude/CLAUDE.md ~/.claude/skills ~/.claude/commands ~/.codex/AGENTS.md ~/.codex/skills/*
