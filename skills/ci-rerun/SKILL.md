---
name: ci-rerun
description: Re-run failed CircleCI jobs or Read the Docs builds, or read why they failed, from the job number or build URL a GitHub check carries. Use when a PR's CircleCI or Read the Docs check failed for an infrastructure reason (a lost clone, a dropped SSH handshake, a runner timeout, a failed checkout) and should run again.
---

# CircleCI from a GitHub check

A CircleCI check on a PR links to `https://circleci.com/gh/<owner>/<repo>/<job-number>`,
and that number is all `gh pr checks` gives. `scripts/circleci.py` turns it
into the job's workflow and acts on that.

```bash
C=~/.claude/skills/ci-rerun/scripts/circleci.py
python3 $C log 391824                 # the failed steps' last 150 lines, to classify the failure
python3 $C rerun 391824 391728 ...    # each job's workflow, once, from its failed jobs
```

`rerun` prints one JSON line, `{"reruns": [{"workflow", "jobs", "newWorkflow"}], "errors": [...]}`,
and exits 1 when any workflow could not be re-run. Several failed jobs of one
workflow are one re-run; the new workflow id is what a watcher records. The
re-run goes through the installed `circleci` CLI and its token in
`~/.circleci/cli.yml`; the job and log lookups are public.

# Read the Docs from a GitHub check

A failed `docs/readthedocs.org:<project>` status links to its build,
`https://app.readthedocs.org/projects/<project>/builds/<id>/`, whose web log is
often behind a rate limit. `scripts/rtd.py` works from that URL through the
API.

```bash
R=~/.claude/skills/ci-rerun/scripts/rtd.py
python3 $R log <build-url>            # state, the failure reason RTD recorded, failed commands' tails
python3 $R rerun <build-url>...       # a new build of each build's version
```

`rerun` prints `{"reruns": [{"build", "version", "newBuild"}], "errors": [...]}`
and exits 1 when any build could not be re-run; builds of one version share one
new build. The token is `RTD_TOKEN`, from the environment or a login shell; without it
the script exits 2.

# A PR's CI for a judge

`scripts/collect.py` is `pr-babysit`'s CI lane without a model: `inventory`
waits a bounded time and lists a head's failed and cancelled checks with a
count of pending ones, and `failures` saves each failing check's log and
diagnostics, with the base branch's run of the same job, for the
`pr-ci-watcher` judge; `remember` and `recall` keep the judge's verdicts beside
that evidence, so a relaunch reuses them. Its docstring is the contract.

```bash
K=~/.claude/skills/ci-rerun/scripts/collect.py
python3 $K inventory --repo <owner/name> --pr <N> --head <sha> --wait-seconds 540
python3 $K failures --repo <owner/name> --pr <N> --head <sha> --check <link>...
```

# Judgment

- **Classify first.** Read the log; re-run only a failure that is the
  infrastructure's, not the code's. A re-run that fails the same way is a real
  failure.
- **Once.** One re-run per workflow or version per failure; a second attempt is a
  human's call.
- **Re-running is not publishing.** It changes nothing on the branch or the
  PR and needs no grant.
