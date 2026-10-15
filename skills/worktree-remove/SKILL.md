---
name: worktree-remove
description: Remove a herdr worktree once its task is done, with its clean <name>--* sibling worktrees, after checking for pending follow-ups; branches whose commits are pushed or a PR head go too. Use when the user asks to remove, delete, clean up or finish a worktree.
---

# Removing a worktree

`scripts/worktree_remove.py` checks and asks herdr to remove the worktree;
agentrc's `worktree-children` herdr plugin then removes the siblings and
deletes the branches. Their docstrings hold every check and exit code.

```bash
python3 ~/.claude/skills/worktree-remove/scripts/worktree_remove.py [PATH] --dry-run [--pending "<item>"]...
```

- Pass each follow-up of this task, including a headless chief report's, that
  is neither done nor filed as an issue under the repository's follow-up
  convention as `--pending`.
- Run `--dry-run` first and show the human its report. Run without it only on
  their grant to remove this worktree.
- Exit 4: show the list and ask; on their yes rerun with `--proceed <token>`
  as printed; headless, report needs-user. A yes allows the removal, not
  filing issues.
- Without `--dry-run` it is the session's last action: herdr closes the
  workspace and its panes, this session's included. Tell the human before
  running it that the outcome is in the plugin log the report names.
- Exit 3 or 5: report the message; never work around a refusal, such as with
  `git worktree remove` or herdr's own menu, which skips the gate.
