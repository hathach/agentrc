---
name: repo-audit
description: Audit repository growth since the last completed audit - pin the range, collect change, usage and test-overlap evidence, decide what to cut, script or redesign, and mark the reviewed end. Use for a periodic maintenance audit ("audit the repo", "what grew since the last audit"), not for a PR or bug review (pr-review, code-audit).
---

# Repository audit

An audit runs from the previous audit's end to a new reviewed end. The scripts produce evidence; deciding what is
real in practice, what to cut, script or redesign stays with the lead and the owner. Their docstrings own their
contracts and exit codes.

```bash
A="${AGENTRC_DIR:-$HOME/.claude}/skills/repo-audit/scripts"
RUN=~/.local/state/agentrc/repo-audit/<repo>/<date>     # private: transcripts are the owner's data
```

## Start

1. `python3 "$A/audit.py" start --repo . --head origin/main --since <base commit time> --until <now> --out $RUN`
   finds the base from the last `audit/*` tag (or take `--base SHA`, the first time) and prints the growth.
2. `python3 "$A/usage.py" --run $RUN --claude-root ~/.claude/projects [--claude-root <other config>/projects]
   --codex-root ~/.codex/sessions --out $RUN/usage.json` counts invocations, subcommands, flags, skill loads, agents,
   workflows and doc reads. Zero is a lead, never proof: consumer repositories reach engines through their own
   wrappers and CI, which no transcript shows; search their contracts and workflows before calling anything unused.
3. Coverage with one context per test, beside (never instead of) the build command's run:

   ```bash
   printf '[run]\ndynamic_context = test_function\nbranch = True\ndata_file = %s/.coverage\n' "$RUN" \
     > "$RUN/coverage.rc"
   python -m coverage run --rcfile="$RUN/coverage.rc" -m pytest -p no:cacheprovider -q tests
   python -m pytest --collect-only -q tests > "$RUN/tests.txt"
   printf '{"coverage": "%s", "revision": "%s", "command": "pytest tests, serial, in-process only"}\n' \
     "$(python -m coverage --version | head -1)" "$(git rev-parse HEAD)" > "$RUN/provenance.json"
   python3 "$A/coverage_overlap.py" --repo . --data "$RUN/.coverage" --tests "$RUN/tests.txt" \
     --out "$RUN/overlap.json"
   ```

4. Brief the co-plan (the user rules' `co-plan`) with: the owner's ask, base and growth, usage evidence and its
   limits, in-flight work (open PRs, unmerged worktrees, open issues), the owner's constraints, what is wanted.

## Judging evidence

- Keep a capability with one verified incident or one supported consumer. Keep its protection, not its
  implementation: a simpler replacement covering the same trigger retires the old code, tests and prose together.
- A cut needs positive evidence (callers and persistent state checked, retained protection named, a validation
  path) and the owner's approval of its behavioural consequence. Unknown use is unknown; a project's own docs naming
  a feature are not demand. Weigh recent features by time since they landed.
- Coverage overlap only ranks what to read. Remove a test for a lost meaning (its mechanism is gone, it pins prose
  outside the repository's prompt-test policy, or another test checks the same invariant, setup and outcome), and
  name the test that keeps each still-relevant incident covered.
- Prompt text that is mechanics an agent re-derives each run becomes a script; a script that only wraps one tool's
  command line becomes a recipe line.
- Each deferred defect keeps a concrete trigger, consequence and evidence; a reviewer's unconfirmed hypothesis gets
  a disposition in the handoff, not an issue.

## End

Stage reviews do not replace the final one: review the whole change since the base, the audit's own changes and
everything else that landed in the range, as the user rules' completion review. Then:

```bash
python3 "$A/audit.py" finish --run $RUN --end <reviewed commit>   # $RUN/tag-message.txt: fill its last 3 lines
git tag -a audit/<date> <reviewed commit> -F $RUN/tag-message.txt
```

Tag the commit the review covered, not a newer tip. Pushing the tag is a publishing action: it needs the owner's
grant. Never move or replace an `audit/*` tag.
