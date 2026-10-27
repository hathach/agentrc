---
name: pr-babysit
description: Fact-collector scripts of the pr-babysit workflow, its publishing push, and the reader of a finished launch. Use when a pr-babysit prompt names one, to reproduce by hand what the workflow saw, or to read a launch's result.
---

# pr-babysit's scripts

Each script prints one JSON line, the last on stdout; the workflow's agent
returns that line unchanged and the workflow decides. An `{"error": ...}` line
with exit 2 means the facts could not be collected or contradict each other:
the workflow refuses to publish, and the caller never fills in what is
missing. Each script's docstring says what it reports.

A script that calls gh names the PR's repository with `--repo OWNER/NAME`, or
else the one the checkout's remotes name alone, and refuses a checkout whose
remotes name several with none marked `gh-resolved` (`facts.py`'s `bind_repo`).

```bash
S=~/.claude/skills/pr-babysit/scripts
python3 $S/preflight.py --pr N [--repo R] [--needs P] [--module M]  # the checkout, the PR it must stay and this interpreter; P a script the workflow calls, M a module it needs
python3 $S/preflight.py --recheck            # before a commit: has the checkout moved?
python3 $S/preflight.py --state N [--repo R]  # before a GitHub write: is the PR still OPEN?
python3 $S/harvest.py --pr N [--repo R] --reviewers coderabbit,greptile --auto-run coderabbit  # bot states + review comments
python3 $S/hooks.py [--deadline-seconds N] 'src/a.c' 'docs/b.rst'   # from the checkout's top level; killed past N s (540)
python3 $S/commits.py commit 'src/a.c' < msg    # commit exactly these paths, the message on stdin
python3 $S/commits.py head --parent <sha> 'src/a.c'  # audit the commit at HEAD, made on <sha> from these paths
python3 $S/commits.py chain <from> <to> --published <pr-head> --base <base-head> --tip <base-tip>  # audit from..to for adoption, full SHAs; preflight's prBase and prBaseTip
python3 $S/push.py --remote origin --branch <b> --sha <sha> --push-url <url> [--open-pr N] [--pr N] [--repo R]
python3 $S/sonar.py --pr N [--repo R] --head <sha> --manifest <file> [--receipt <path>]  # mark answered code-scanning comments' SonarCloud issues false positive; a saved --receipt replays
python3 $S/coderabbit_trigger.py --pr N [--repo R] --head <sha> [--request]  # tick CodeRabbit's Trigger review box when it skipped the head for a manual review; --request comments `@coderabbitai review` once its rate limit has reset, or on such a skip no tick asked for
python3 $S/greptile_trigger.py --pr N [--repo R] --head <sha>  # comment `@greptileai` once per head, recorded under the git common dir
python3 $S/build.py --path 'src/a.c' --command 'make -C <BUILD>' [--setup L] [--require T]  # build the checkout as it stands
python3 $S/scope_check.py --path 'src/a.c' --path 'test/test_a.c'  # which paths a fix writer may edit or create
python3 $S/state_transfer.py <saved output> [--chunks I,J] [--pad]  # a stateRef's state as checksummed base64 chunks; --pad shifts their text
python3 $S/launch_result.py --output <saved output> [--state-ref F:D] [--checkout DIR] [--keys]  # the caller's reading of a launch; --keys: settled failures' keys too
python3 $S/merge.py --pr-url <url> --base <branch> --checkout DIR --output <saved output> --state-digest D [--merge]  # chief's merge gate; inspects without --merge
```

`merge.py --merge` is chief-only, under `agents/chief.md`'s merge exception.

After an agentrc update, launch the workflow from the installed file (Workflow
`scriptPath: ~/.claude/workflows/pr-babysit.js`), not by its name in a session
started before the update: that session may hold an older copy of the script
while the skills' scripts are already new. A copy whose collector output it
cannot read stops `collector-skew` (collect.py's `contract`), one calling a
script the install lacks `stale-workflow`.

`launch_result.py` is the caller's, not the workflow's: it condenses a finished
launch, lists the `blockers` to settle before continuing, and shows as `pinned`
the arguments the state is fixed to, which a continuation passes unchanged.
A launch on a PR no longer open stops `pr-merged` or `pr-closed`, at preflight or at the CI lane's next read; nothing is left to continue. Every GitHub or SonarCloud write reads the state first (`preflight.py --state`, or `push.py --open-pr` for a push) and goes on only for an OPEN PR; an unreadable state stops `pr-state-unread`.
`preflight.py`, `harvest.py`, `scope_check.py`, `state_transfer.py`, `launch_result.py` and `commits.py` only read, except that
`preflight.py` creates the launch's receipts directory and `commits.py commit` stages and commits exactly its paths; `hooks.py` runs the repository's
hooks, which may rewrite files; `build.py` builds the checkout in a fresh build directory; `push.py` publishes `<sha>` and reads back
where it landed, `sonar.py` comments on and resolves SonarCloud issues, and `coderabbit_trigger.py` edits
CodeRabbit's summary comment to tick its Trigger review box, or with `--request` posts one `@coderabbitai review` comment, and `greptile_trigger.py` posts one `@greptileai` comment, and `merge.py --merge` merges the PR. Run any of them by hand only with the user's authorization to publish there.

## Arguments

`ARGUMENTS.md` beside this file lists the workflow's arguments: read it
before launching the workflow or reading its argument errors.
