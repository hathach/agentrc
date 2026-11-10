---
name: pr-babysit
description: Fact-collector scripts of the pr-babysit workflow, its publishing push, and the reader of a finished launch. Use when a pr-babysit prompt names one, to reproduce by hand what the workflow saw, or to read a launch's result.
---

# pr-babysit's scripts

Each script prints one JSON line, the last on stdout, and the workflow decides
on it. Under `AGENTRC_EMIT=<file>`, as the workflow's relay runs it, the script
also writes that line to the new `<file>` as a data module the workflow loads
with `workflow({scriptPath})`, so no model copies it (`lib/seal.py`'s `emit`);
the file is claimed before the script does anything, so a rerun meeting one
already there exits 3 at once, nothing done. `facts.py emit-dir` makes a
launch's directory for those files under the git common dir; that path is the
one value a model copies, and the files' self-naming and seals give
consistency, not freshness. An `{"error": ...}` line
with exit 2 means the facts could not be collected or contradict each other:
the workflow refuses to publish, and the caller never fills in what is
missing. Each script's docstring says what it reports.

A script that calls gh names the PR's repository with `--repo OWNER/NAME`, or
else the one the checkout's remotes name alone, and refuses a checkout whose
remotes name several with none marked `gh-resolved` (`facts.py`'s `bind_repo`).

```bash
S="${AGENTRC_DIR:-$HOME/.claude}/skills/pr-babysit/scripts"
python3 "$S/preflight.py" --pr N [--repo R] [--needs P] [--module M]  # the checkout, the PR it must stay and this interpreter; P a script the workflow calls, M a module it needs
python3 "$S/preflight.py" --recheck            # before a commit: has the checkout moved?
python3 "$S/preflight.py" --state N [--repo R]  # before a GitHub write: is the PR still OPEN?
python3 "$S/harvest.py" --pr N [--repo R] --reviewers coderabbit,greptile --auto-run coderabbit  # bot states + review comments
python3 "$S/hooks.py" [--deadline-seconds N] 'src/a.c' 'docs/b.rst'   # from the checkout's top level; killed past N s (540), cleanup 20 s more
python3 "$S/commits.py" commit [--skip-hooks <ids>] [--receipt <file> --request <r>] 'src/a.c' < msg  # commit exactly these paths, the message on stdin; the line saved to a new <file>; <ids> added to pre-commit's SKIP
python3 "$S/commits.py" receipt <file> --request <r> --parent <sha> 'src/a.c'  # what that commit printed, for this request, parent and paths only
python3 "$S/commits.py" head --parent <sha> 'src/a.c'  # audit the commit at HEAD, made on <sha> from these paths
python3 "$S/commits.py" chain <from> <to> --published <pr-head> --base <base-head> --tip <base-tip>  # audit from..to for adoption, full SHAs; preflight's prBase and prBaseTip
python3 "$S/push.py" --remote origin --branch <b> --sha <sha> --push-url <url> [--open-pr N] [--pr N] [--repo R]
python3 "$S/sonar.py" --pr N [--repo R] --head <sha> --manifest <file> [--receipt <path>]  # mark answered code-scanning comments' SonarCloud issues false positive; a saved --receipt replays
python3 "$S/coderabbit_trigger.py" --pr N [--repo R] --head <sha> [--request]  # tick CodeRabbit's Trigger review box when it skipped the head for a manual review; --request comments `@coderabbitai review` once its rate limit has reset, on such a skip no tick asked for, or when its review of the head failed
python3 "$S/greptile_trigger.py" --pr N [--repo R] --head <sha>  # comment `@greptileai` once per head, recorded under the git common dir
python3 "$S/build.py" --path 'src/a.c' --command 'make -C <BUILD>' [--setup L] [--require T]  # build the checkout as it stands
python3 "$S/scope_check.py" --path 'src/a.c' --path 'test/test_a.c'  # which paths a fix writer may edit or create
python3 "$S/state_transfer.py" <saved output>  # a stateRef's state whole with its digest
python3 "$S/facts.py" emit-dir                 # a new directory for one launch's AGENTRC_EMIT files; ones unwritten for 14 days go
python3 "$S/launch_result.py" --output <saved output> [--state-ref F:D] [--checkout DIR] [--keys]  # the caller's reading of a launch; --keys: settled failures' keys too
python3 "$S/merge.py" --pr-url <url> --base <branch> --checkout DIR --output <saved output> --state-digest D [--waive BOT] [--merge]  # chief's merge gate; inspects without --merge; --waive drops a bot from the required reviews, and a last launch's stop on that bot alone, on the owner's word, recorded as `waived`
```

`merge.py --merge` is chief-only, under `agents/chief.md`'s merge exception, and `--waive` only on the owner's word that
the merge goes without that bot; a bot left out of `autoRun` alone is never waived, and waiving both is refused. A red
CI goes with the merge only when the launch's `acceptedFailures` explain every failed check, as the very executions it
judged, and none is a required check; the receipt's `accepted` records them.

Launch the workflow from the root its scripts run from (Workflow
`scriptPath: "${AGENTRC_DIR:-$HOME/.claude}/workflows/pr-babysit.js"`, expanded
in the shell first), not by its name: a named launch loads the user scope's
saved `workflows/pr-babysit.js`, which `AGENTRC_DIR` does not change, and a
session started before an agentrc update may hold an older copy of the script
while the skills' scripts are already new. A copy whose collector output it
cannot read stops `collector-skew` (collect.py's `contract`), one calling a
script the install lacks `stale-workflow`, and one whose `facts.py` lacks
`emit-dir` stops `preflight-failed` naming both checkouts and their HEADs.

`launch_result.py` is the caller's, not the workflow's: it condenses a finished
launch, lists the `blockers` to settle before continuing, and shows as `pinned`
the arguments the state is fixed to, which a continuation passes unchanged.
A launch on a PR no longer open stops `pr-merged` or `pr-closed`, at preflight or at the CI lane's next read; nothing is left to continue. Every GitHub or SonarCloud write reads the state first (`preflight.py --state`, or `push.py --open-pr` for a push) and goes on only for an OPEN PR; an unreadable state stops `pr-state-unread`.
`preflight.py`, `harvest.py`, `scope_check.py`, `state_transfer.py`, `launch_result.py` and `commits.py` only read, except that
each writes its AGENTRC_EMIT file, `preflight.py` creates the launch's receipts directory and `commits.py commit` stages and commits exactly its paths, saving its receipt; `hooks.py` runs the repository's hooks, which may rewrite
files, and reruns failed ones alone in a temporary worktree of HEAD it then removes with its registration; its
docstring says which failures that excuses from the verdict and the commit's SKIP; `build.py`
builds the checkout in a fresh build directory; `push.py` publishes `<sha>` and reads back
where it landed, `sonar.py` comments on and resolves SonarCloud issues, and `coderabbit_trigger.py` edits
CodeRabbit's summary comment to tick its Trigger review box, or with `--request` posts one `@coderabbitai review` comment, and `greptile_trigger.py` posts one `@greptileai` comment, and `merge.py --merge` merges the PR. Run any of them by hand only with the user's authorization to publish there.

## Arguments

`ARGUMENTS.md` beside this file lists the workflow's arguments: read it
before launching the workflow or reading its argument errors.
