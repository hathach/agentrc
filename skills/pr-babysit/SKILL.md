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
S=~/.claude/skills/pr-babysit/scripts
python3 $S/preflight.py --pr N                 # the checkout and the PR it must stay
python3 $S/hooks.py 'src/a.c' 'docs/b.rst'   # from the checkout's top level
python3 $S/commits.py head 'src/a.c'         # the commit at HEAD and its scope
python3 $S/commits.py chain <from> <to>      # every commit in from..to, full SHAs
python3 $S/push.py --remote origin --branch <b> --sha <sha> --push-url <url> [--pr N]
```

`preflight.py` pins what every later step must still be true of: the branch
and HEAD, the PR's head branch, SHA, repository and URL, the remote the branch
tracks with its push URLs, and the dirty paths.

`hooks.py` runs the repository's pre-commit hooks on the paths, once more if
the first run fails, and reports the tree status and blob snapshots around
them, with the ids of the hooks pre-commit itself said modified files. A marker
a hook prints in its own output is not read as pre-commit's.

`commits.py` reads back what commits hold: parents, paths, message, and for
`head` the scope's leftover changes and tree entries. It reads HEAD once and
refuses if HEAD moved meanwhile, so every fact is about one commit.

`push.py` is the one script that acts: it pushes exactly `<sha>` to the branch,
only while the remote still pushes to the URLs given, then reads the branch
back from each of those URLs, and with `--pr` the PR head. A read-back it could
not make is `null`, and the workflow then reports the publication unknown,
never "not pushed". Run it by hand only with the user's authorization to push.
