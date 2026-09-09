#!/bin/sh
# Link this checkout into ~/.claude and ~/.codex. Idempotent; rerun after adding a skill.
set -eu
repo="$(cd "$(dirname "$0")" && pwd)"

mkdir -p ~/.claude ~/.codex/skills
for d in skills commands; do
  [ -L ~/.claude/$d ] || rmdir ~/.claude/$d 2>/dev/null || true
  ln -sfn "$repo/$d" ~/.claude/$d
done
ln -sfn "$repo/CLAUDE.md" ~/.claude/CLAUDE.md
ln -sfn ../.claude/CLAUDE.md ~/.codex/AGENTS.md

# Codex scans ~/.codex/skills, not ~/.claude/skills, and owns the .system dir
# there, so link each skill individually instead of the directory.
for s in "$repo"/skills/*/; do
  ln -sfn "${s%/}" ~/.codex/skills/"$(basename "$s")"
done
for l in ~/.codex/skills/*; do
  [ -L "$l" ] && [ ! -e "$l" ] && rm "$l"
done

readlink -f ~/.claude/CLAUDE.md ~/.claude/skills ~/.claude/commands ~/.codex/AGENTS.md ~/.codex/skills/*
