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
python3 $S/preflight.py --pr N [--needs P]   # the checkout and the PR it must stay; P a script the workflow calls
python3 $S/preflight.py --recheck            # before a commit: has the checkout moved?
python3 $S/harvest.py --pr N --reviewers coderabbit,greptile --auto-run coderabbit  # bot states + review comments
python3 $S/hooks.py 'src/a.c' 'docs/b.rst'   # from the checkout's top level
python3 $S/commits.py commit 'src/a.c' < msg    # commit exactly these paths, the message on stdin
python3 $S/commits.py head --parent <sha> 'src/a.c'  # audit the commit at HEAD, made on <sha> from these paths
python3 $S/commits.py chain <from> <to> --published <pr-head> --base <base-head>  # audit from..to for adoption, full SHAs
python3 $S/push.py --remote origin --branch <b> --sha <sha> --push-url <url> [--pr N]
python3 $S/sonar.py --pr N --head <sha> --manifest <file> [--receipt <path>]  # mark answered code-scanning comments' SonarCloud issues false positive; a saved --receipt replays
python3 $S/build.py --path 'src/a.c' --command 'make -C <BUILD>'  # build the checkout as it stands
python3 $S/state_transfer.py <saved output> [--chunks I,J]  # a stateRef's state as checksummed base64 chunks
python3 $S/launch_result.py --output <saved output> [--state-ref F:D] [--checkout DIR]  # the caller's reading of a launch
```

`launch_result.py` is the caller's, not the workflow's: it condenses a finished
launch and lists the `blockers` to settle before continuing.
`preflight.py`, `harvest.py`, `state_transfer.py`, `launch_result.py` and `commits.py` only read, except that
`commits.py commit` stages and commits exactly its paths; `hooks.py` runs the repository's
hooks, which may rewrite files; `build.py` builds the checkout in a fresh build directory; `push.py` publishes `<sha>` and reads back
where it landed, and `sonar.py` comments on and resolves SonarCloud issues. Run either by hand only with the user's authorization to publish there.

## Arguments

`ARGUMENTS.md` beside this file lists the workflow's arguments: read it
before launching the workflow or reading its argument errors.
