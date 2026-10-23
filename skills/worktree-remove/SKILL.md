---
name: worktree-remove
description: Remove a linked worktree, herdr's or not, once its task is done, with its clean <name>--* sibling worktrees, after checking for pending follow-ups; branches whose commits are pushed or a PR head go too. Use when the user asks to remove, delete, clean up or finish a worktree.
---

# Removing a worktree

`scripts/worktree_remove.py` checks the worktree, then removes it: through
herdr when herdr has it open as a workspace, whose `worktree-children` plugin
then removes the siblings and deletes the branches; otherwise, herdr
installed or not, with `git worktree remove` and that plugin's cleanup run by
the script itself. Their docstrings hold every check and exit code.

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
- Open in herdr, the run without `--dry-run` is the session's last action:
  herdr closes the workspace and its panes, this session's included. Tell the
  human before running it that the outcome is in the plugin log the report
  names.
- Open in no herdr workspace, run it from outside the worktree, naming it as
  PATH (it refuses a working directory inside); it prints the outcome and logs
  it beside the report. On Windows a session started inside the worktree holds
  it open, so ask the human to run it from another session.
- Exit 3 or 5: report the message; never work around a refusal, such as with
  `git worktree remove` or herdr's own menu, which skips the gate. A chief
  session found in the worktree, yours included, means a chief cannot remove
  its own worktree: ask the human to run this from a plain session.
