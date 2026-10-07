---
name: worktree-reset
description: Reset a git worktree slot for its next task once its branch merged (fresh numbered branch at the default branch, ignored files and chief state kept). Use when the user asks to reset, clean up or recycle a worktree.
---

# Resetting a worktree

`scripts/worktree_reset.py` does the reset; its docstring has every check, step and exit code.

```bash
python3 ~/.claude/skills/worktree-reset/scripts/worktree_reset.py [--dry-run] [--pending "<item>"]... [--proceed]
```

- Pass each follow-up of this task, including a headless chief report's, that is
  neither done nor filed as an issue under the repository's follow-up convention
  as `--pending`.
- Exit 4: show its list and ask the human; rerun with `--proceed` only on their
  yes; headless, report needs-user. A yes allows the reset, not filing issues.
- Exit 3 or 5: report the message; never work around a refusal.
