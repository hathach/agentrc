# pr-babysit arguments

The `args` object `workflows/pr-babysit.js` takes (installed as `~/.claude/workflows/pr-babysit.js`).

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
  never committed or published; an adopted chain may carry one only in commits
  the PR already has, or in a merge from the base that takes either parent's
  version of it.
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
  findings left to an existing issue, answered with it, on the caller's word
  that the issue covers the finding; without `issueUrl`,
  left as is by the PR owner's decision, answered with its reason. Kept in
  the state while the comment body stands. `{ issueUrl, reason }` alone is an
  out-of-scope topic, passed on every launch: a valid finding no deferral
  names is held from the fixer, unanswered, unless judged outside every
  topic's issue; the caller defers a held one by id.
- `replySettlements`: `[{ commentId, commentDigest, replyId, bodyDigest, headSha }]`:
  the caller's verified reply on a comment the result handed off, judged to
  answer it whole at `headSha`, `bodyDigest` as `reply.py`'s receipt or
  inspection gives it. Under `autoPush`, a cycle that harvests
  reviews settles it when `headSha` is the expected HEAD after any adoption,
  no point of the comment is held, and `reply.py --reuse` returns a settling
  receipt: both digests read back and, in a review thread, the thread
  resolved. The result's `settlements` say which settled and why the rest did
  not. An entry's `sonar: { how: 'refutation' | 'fixNote', note }`, `note` the
  reply body `bodyDigest` digests, makes a settled code-scanning comment owe
  its SonarCloud marking under `markSonar`, unless our earlier answer still
  owes one. Per launch.
- `yieldAfterCycle`: run one cycle and return the state for the next launch.
- `lane`: `both` (default), `ci` or `reviews`; a single lane needs
  `yieldAfterCycle` and never declares the PR done.
- `state` or `stateRef: { outputFile, digest }`: a previous launch's state, or
  the saved output holding it and its `stateDigest`, loaded as checksummed chunks.
- `adoptHead`: full SHA of audited commits on top of the state's
  `expectedHead` (a caller's repair, or a push made outside the workflow):
  the chain is audited, published under `autoPush`, and the run continues
  from it; per launch. A merge from the PR's base branch is one link, its
  paths those it leaves unlike both parents; it is from the base when the
  branch's live tip or the base GitHub recorded holds its second parent. That
  record is stale until the head is pushed, so a merge newer than it needs the
  live tip in the checkout (fetch the base branch), or the audit stops with
  `fetch it`. A PR that conflicts with its base
  stops `pr-conflicting`; the merge that resolves it rejoins by `adoptHead`.
- `rebasedHead`: full SHA of the PR head after a history rewrite (rebase,
  force-push) the user authorized: with the checkout at it and no unpublished
  candidate, the state is re-pinned to it unaudited, keeping cycles used,
  answers, decisions, holds and deferrals; passed only on the user's word,
  never with `adoptHead`; per launch.
