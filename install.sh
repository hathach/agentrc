#!/bin/sh
# Link this checkout into ~/.claude and ~/.codex. Idempotent; rerun after adding a skill.
set -eu
repo="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# One "target destination" pair per link. Codex scans ~/.codex/skills, not
# ~/.claude/skills, and owns the .system dir there, so each skill is linked
# individually instead of the directory.
links() {
  printf '%s\n' "$repo/skills $HOME/.claude/skills" \
                "$repo/commands $HOME/.claude/commands" \
                "$repo/CLAUDE.md $HOME/.claude/CLAUDE.md" \
                "../.claude/CLAUDE.md $HOME/.codex/AGENTS.md"
  for s in "$repo"/skills/*/; do
    printf '%s\n' "${s%/} $HOME/.codex/skills/$(basename "$s")"
  done
}

# A destination may be absent, an empty directory, or a symlink we can replace.
# Anything else is the user's own content: refuse before touching anything.
links | while read -r _ dst; do
  [ -L "$dst" ] || [ ! -e "$dst" ] && continue
  [ -d "$dst" ] && [ -z "$(ls -A "$dst")" ] && continue
  echo "install.sh: $dst exists and is not a symlink; move it aside first" >&2
  exit 1
done

mkdir -p ~/.claude ~/.codex/skills
links | while read -r src dst; do
  [ -d "$dst" ] && [ ! -L "$dst" ] && rmdir "$dst"
  ln -sfn "$src" "$dst"
done

# Prune links of ours whose skill left the repo; leave other people's links alone.
for l in ~/.codex/skills/*; do
  [ -L "$l" ] || continue
  case "$(readlink "$l")" in "$repo"/skills/*) [ -e "$l" ] || rm "$l" ;; esac
done

links | while read -r _ dst; do readlink -f "$dst"; done
