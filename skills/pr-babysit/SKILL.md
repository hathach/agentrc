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
python3 $S/commits.py commit 'src/a.c' < msg    # commit exactly these paths, the message on stdin
python3 $S/commits.py head 'src/a.c'         # the commit at HEAD and its scope
python3 $S/commits.py chain <from> <to>      # audit from..to for adoption, full SHAs
python3 $S/push.py --remote origin --branch <b> --sha <sha> --push-url <url> [--pr N]
python3 $S/sonar.py --pr N --head <sha> --manifest <file>  # mark answered code-scanning comments' SonarCloud issues false positive
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

The workflow's own contract (`workflows/pr-babysit.js`).

- `pr` (number, required).
- `reviewers`: of `copilot`, `coderabbit`, `greptile`, `code-scanning`;
  default `['coderabbit', 'greptile', 'code-scanning']`; `[]` runs no review lane.
- `autoRun`: the reviewers that run on every push and gate done; default
  `reviewers` without `copilot` and `code-scanning`, which are only harvested.
- `maxCycles`: cycle ceiling, default 10; a resumed launch defaults to its state's.
- `autoPush` (default false, a dry run: fixes left uncommitted, nothing posted)
  and `markSonar` (needs `autoPush` and `SONAR_TOKEN`: marks the SonarCloud
  issue behind a refuted code-scanning comment, or a fixed one SonarCloud still
  flags, false positive): per launch, never restored from a state.
- `checkoutDir`: the PR checkout, default the session directory. Dirty `.idea/`
  paths are ignored; any other pre-existing edit refuses the start.
- `protected`: regex over repo-relative paths dropped from every fix scope and
  never committed.
- `generated`: regex over repo-relative paths a fixer's build regenerates; a
  plain modification to one is admitted into the commit on the caller's word
  that the repository hooks validate it.
- `build`: verify command, `<BUILD>` for a fresh build directory; default the
  repository's build contract; per launch.
- `ciWait`: minutes to wait on pending checks, default 30.
- `ciNotes`: what the caller established about this PR's CI, handed to the
  judge verbatim; it never revises a placed verdict stored on the head, while
  a stored unclassified failure is judged again once the notes change.
- `acceptedFailures`: `[{ key, reason, scope }]`, `key` the 16-hex one the
  result shows beside a failure: never fixed, and a run red only from them
  passes, listing them. Per launch.
- `deferrals`: `[{ findingId, commentDigest, issueUrl, reason }]`: valid
  findings left to an existing issue, answered with it; kept in the state
  while the comment body stands.
- `yieldAfterCycle`: run one cycle and return the state for the next launch.
- `lane`: `both` (default), `ci` or `reviews`; a single lane needs
  `yieldAfterCycle` and never declares the PR done.
- `state` or `stateRef: { outputFile, digest }`: a previous launch's state, or
  the saved output holding it and its `stateDigest`, loaded as checksummed chunks.
- `adoptHead`: full SHA of audited commits on top of the state's
  `expectedHead` (a caller's repair, or a push made outside the workflow):
  the chain is audited, published under `autoPush`, and the run continues
  from it; per launch.
