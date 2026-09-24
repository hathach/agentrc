---
name: pr-babysit
description: Fact collectors the pr-babysit workflow runs in a PR checkout before it commits a fix. Use when a pr-babysit prompt names one of these scripts, or to reproduce by hand what the workflow saw; the workflow, not the script, decides whether to publish.
---

# pr-babysit's scripts

Each script collects facts and prints them as one JSON line, the last on
stdout; the workflow's agent returns that line unchanged and the workflow
decides. An `{"error": ...}` line with exit 2 means the facts could not be
collected or contradict each other: the workflow refuses to publish, and the
caller never fills in what is missing.

```bash
H=~/.claude/skills/pr-babysit/scripts/hooks.py
python3 $H 'src/a.c' 'docs/b.rst'   # from the checkout's top level
```

`hooks.py` runs the repository's pre-commit hooks on the paths, once more if
the first run fails, and reports the tree status and blob snapshots around
them, with the ids of the hooks pre-commit itself said modified files. A marker
a hook prints in its own output is not read as pre-commit's.
