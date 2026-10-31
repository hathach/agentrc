---
name: worktree-remove
description: Remove a linked worktree once its task is done, with its clean <name>--* sibling worktrees, after checking for pending follow-ups; branches whose commits are pushed or a PR head go too. Use when the user asks to remove, delete, clean up or finish a worktree.
---

# Removing a worktree

`scripts/worktree_remove.py` checks the worktree, then removes it with
`git worktree remove` and runs `scripts/worktree_children.py`'s cleanup, which
removes the clean siblings and deletes the branches. Their docstrings hold
every check and exit code. This is the only way a worktree goes: a terminal
app's own Remove (herdr-app's worktree menu) skips the gate and the siblings.

```bash
python3 <skill dir>/scripts/worktree_remove.py [PATH] --dry-run [--pending "<item>"]...
```

- Pass each follow-up of this task, including a headless chief report's, that
  is neither done nor filed as an issue under the repository's follow-up
  convention as `--pending`.
- Run `--dry-run` first and show the human its report. Run without it only on
  their grant to remove this worktree.
- Exit 4: show the list and ask; on their yes rerun with `--proceed <token>`
  as printed; headless, report needs-user. A yes allows the removal, not
  filing issues.
- Run it from outside the worktree and its siblings, naming it as PATH (it
  refuses a working directory inside them, a kept sibling included); it prints
  the outcome and logs it beside the report. It refuses while a herdr-core
  terminal works in the worktree or a sibling, naming each with its
  herdr-app space: ask the human to close those, or to run it from another
  session when it is yours. On Linux it also refuses while any other process
  of yours works in them; elsewhere such a process loses its directory with
  the removal (on Windows holds it open, failing the removal part way).
  Without `hc` installed terminals go unchecked, and without `/proc` other
  processes, as the report says.
- Exit 6: the worktree is gone but the cleanup kept a sibling or branch, or
  stopped because something appeared at its path again; report the reasons
  and the retry command the log names.
- Exit 3 or 5: report the message; never work around a refusal, such as with
  `git worktree remove` or a terminal app's Remove. A chief
  session found in the worktree, yours included, means a chief cannot remove
  its own worktree: ask the human to run this from a plain session.
