#!/bin/sh
# Link this checkout into ~/.claude and ~/.codex. Idempotent; rerun after adding a skill.
set -eu
repo="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# One tab-separated "target destination" pair per link. Skills are linked one
# by one so the skill dirs stay shared with other installers (npx skills, Codex's
# own .system dir) rather than owned by this repo.
tab="$(printf '\t')"
skill_dirs() { printf '%s\n' "$HOME/.claude/skills" "$HOME/.codex/skills"; }
links() {
  printf '%s\t%s\n' "$repo/commands" "$HOME/.claude/commands" \
                    "$repo/CLAUDE.md" "$HOME/.claude/CLAUDE.md" \
                    "../.claude/CLAUDE.md" "$HOME/.codex/AGENTS.md"
  for s in "$repo"/skills/*/; do
    skill_dirs | while read -r d; do printf '%s\t%s\n' "${s%/}" "$d/$(basename "$s")"; done
  done
}

# A destination may be absent, an empty directory, a symlink we can replace,
# or one of our own skills seen through the whole-dir link an earlier version
# made. Anything else is the user's own content: refuse before touching anything.
links | while IFS="$tab" read -r _ dst; do
  [ -L "$dst" ] || [ ! -e "$dst" ] && continue
  [ -d "$dst" ] && [ -z "$(ls -A "$dst")" ] && continue
  [ "$(readlink -f "$(dirname "$dst")")" = "$repo/skills" ] && continue
  echo "install.sh: $dst exists and is not a symlink; move it aside first" >&2
  exit 1
done

# Drop that earlier whole-dir link so the dir can hold this machine's own
# skills beside the linked ones.
skill_dirs | while read -r d; do
  if [ -L "$d" ] && [ "$(readlink -f "$d")" = "$repo/skills" ]; then rm "$d"; fi
done

skill_dirs | while read -r d; do mkdir -p "$d"; done
links | while IFS="$tab" read -r src dst; do
  [ -d "$dst" ] && [ ! -L "$dst" ] && rmdir "$dst"
  ln -sfn "$src" "$dst"
done

# Prune links of ours whose skill left the repo; leave other people's links alone.
for l in ~/.claude/skills/* ~/.codex/skills/*; do
  [ -L "$l" ] || continue
  case "$(readlink "$l")" in "$repo"/skills/*) [ -e "$l" ] || rm "$l" ;; esac
done

links | while IFS="$tab" read -r _ dst; do readlink -f "$dst"; done
