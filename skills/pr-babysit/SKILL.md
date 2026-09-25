---
name: pr-babysit
description: Fact collectors the pr-babysit workflow runs in a PR checkout, the push it publishes with, and its caller's reader of a finished launch. Use when a pr-babysit prompt names one of these scripts, to reproduce by hand what the workflow saw, or to read a launch's result; the workflow, not the script, decides whether to publish.
---

# pr-babysit's scripts

Each script prints one JSON line, the last on stdout; the workflow's agent
returns that line unchanged and the workflow decides. An `{"error": ...}` line
with exit 2 means the facts could not be collected or contradict each other:
the workflow refuses to publish, and the caller never fills in what is
missing. Each script's docstring says what it reports.

```bash
S=~/.claude/skills/pr-babysit/scripts
python3 $S/preflight.py --pr N               # the checkout and the PR it must stay
python3 $S/preflight.py --recheck            # before a commit: has the checkout moved?
python3 $S/harvest.py --pr N --reviewers coderabbit,greptile --auto-run coderabbit  # bot states + review comments
python3 $S/hooks.py 'src/a.c' 'docs/b.rst'   # from the checkout's top level
python3 $S/commits.py head 'src/a.c'         # the commit at HEAD and its scope
python3 $S/commits.py chain <from> <to>      # every commit in from..to, full SHAs
python3 $S/push.py --remote origin --branch <b> --sha <sha> --push-url <url> [--pr N]
python3 $S/build_compare.py candidate --path 'src/a.c' --command 'make -C <BUILD>'
python3 $S/build_compare.py base --rev <sha> [--setup CMD] --command 'make -C <BUILD>'
python3 $S/state_transfer.py <saved output> [--chunks I,J]  # a stateRef's state as checksummed base64 chunks
python3 $S/launch_result.py --output <saved output> [--state-ref F:D] [--checkout DIR]  # the caller's reading of a launch
```

`launch_result.py` is the caller's, not the workflow's: it condenses a finished
launch and lists the `blockers` to settle before continuing.
`preflight.py`, `harvest.py`, `commits.py`, `state_transfer.py` and `launch_result.py` only read; `hooks.py` runs the repository's
hooks, which may rewrite files; `build_compare.py` builds the checkout, or the
given revision in a temporary worktree it removes, in a fresh build directory; `push.py` publishes `<sha>` and reads back
where it landed. Run it by hand only with the user's authorization to push.
