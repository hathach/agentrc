# pr-babysit arguments

The `args` object `workflows/pr-babysit.js` takes (installed as `"${AGENTRC_DIR:-$HOME/.claude}/workflows/pr-babysit.js"`).
A key not listed here throws before anything runs.

- `pr` (number, required).
- `repo`: `OWNER/NAME`, the repository `pr` belongs to (a fork PR's base
  repository); every script's gh call names it. Omitted, a resumed launch takes
  its state's, and a first launch the one the checkout's remotes name alone:
  the remote marked `gh-resolved` (`gh repo set-default`), else the only GitHub
  repository any remote names; with several and none marked, preflight stops
  rather than guess. A state continues only its own repository: another refuses
  the launch. Per launch.
- `python`: the interpreter every script runs under, a path or command name,
  the `stateRef` load included. Omitted, the load and preflight run `python3`,
  and every later script the absolute path that interpreter reports; nothing
  searches for another. Preflight checks it imports what the lanes need (PyYAML
  while a bot is waited on) and stops `python-unusable` naming what is missing.
  Per launch.
- `reviewers`: of `copilot`, `coderabbit`, `greptile`, `code-scanning`;
  default `['coderabbit', 'greptile', 'code-scanning']`; `[]` runs no review lane.
- `autoRun`: the reviewers that run on every push and gate done; default
  `reviewers` without `copilot` and `code-scanning`, which are only harvested.
  Every reviewer is reported with its state; only these are waited on, which is
  not a review: a pass lists each reviewer whose state was read and is not a
  review of the head in `unreviewed`. Only a trigger switch makes one required.
- `maxCycles`: cycle ceiling, default 16, counting a cycle that only waited on
  CI or bots; a resumed launch defaults to its state's.
- `autoPush` (default false, a dry run: fixes left uncommitted, nothing posted)
  and `markSonar` (needs `autoPush` and `SONAR_TOKEN`: marks the SonarCloud
  issue behind a refuted code-scanning comment, or a fixed one SonarCloud still
  flags, false positive): per launch, never restored from a state.
- `coderabbitTrigger` (boolean, default false): set in the repository's
  `CLAUDE.md` when CodeRabbit skips every push for a manual review (a
  repository below its open-source threshold). Once per head, ticks
  the "Trigger review" box of CodeRabbit's summary comment when the head was
  skipped that way, then waits for the review up to the silent-bot cap. Once
  per head too, it comments `@coderabbitai review` instead: when that tick
  failed (a summary with no box to tick, a failed edit), unless the box reads
  ticked or such a comment follows the head event, or when CodeRabbit is
  rate-limited with a reset time under an hour away, after napping until the
  reset (a limit notice has no box); then it waits the same way. A request that
  failed before its post, so posted nothing, is tried once more, one that may
  have posted is not. A tick that failed before this fallback existed, recorded
  in a state, still gets its one comment from a relaunch. The first launch with
  the switch latches the run, as `greptileTrigger` does: every later head needs
  CodeRabbit's review, a launch without the switch waits on an earlier ask and
  asks nothing, and a head left unreviewed (a failed tick and request, no review
  within the cap, a skip or limit with nothing more to ask) stops
  `coderabbit-unreviewed`, never a pass. That stop, and any other with
  CodeRabbit's last reading of the head no review, returns `coderabbitAsk`:
  the asks made on the head and the one a relaunch would still make, if any.
  Needs `autoPush` and `coderabbit` in `autoRun`; the switch itself is never
  restored from a state.
- `greptileTrigger` (boolean, default false): set in the repository's
  `CLAUDE.md` when Greptile reviews only on PR open, so a pushed head is
  reviewed only on request. Greptile is then waited on, as if in `autoRun`.
  Once per head, when Greptile has stayed absent for 2 minutes, comments
  exactly `@greptileai` (`greptile_trigger.py`), then waits for the review up
  to the silent-bot cap counted from the request. A head Greptile already
  reviewed or is scoring is never asked about. The first launch with the switch
  latches the run: its state requires Greptile's review of every later head,
  so a launch without the switch keeps waiting on Greptile, asking nothing. A
  failed, uncertain or ambiguous request, or no review within the cap, stops
  `greptile-unreviewed`, never a pass. Needs `autoPush` and `greptile` in
  `reviewers`; the switch itself is never restored from a state.
- `checkoutDir`: the PR checkout, default the session directory. Dirty `.idea/`
  paths are ignored; any other pre-existing edit refuses the start.
- `protected`: regex over repo-relative paths kept out of every fix scope (a
  review fix needing one is withheld, a CI fix's guess at one dropped) and
  never committed or published; an adopted chain may carry one only in commits
  the PR already has, in a merge from the base that takes either parent's
  version of it, or as `adoptProtected` or `adoptReviewedProtected` permits.
- `generated`: regex over repo-relative paths a fixer's build regenerates; a
  plain modification to one is admitted into the commit on the caller's word
  that the repository hooks validate it.
- `build`: verify command, `<BUILD>` for a fresh build directory, carrying any
  environment setup it needs; default the repository's build contract, its
  setup and the tools it requires resolved with it, every built target's or
  board family's included, a missing one stopping `build-unverifiable` rather
  than failing the batch; per launch. Only a tool the resolver names is checked
  before the build: one only a build wrapper invokes and the contract never
  names still fails the build as unverified.
- `ciWait`: minutes to wait on pending checks, default 30. CodeRabbit's own
  check is left out when coderabbit is in `autoRun` and the review lane runs:
  the review lane waits on it. A head with no other check registered settles
  green at the CI wait's next slice boundary after the review lane's pass
  (normally within 3 minutes, a later slice up to 9), not after the whole
  wait; that pass is a grace period for checks that register late, not proof.
- `ciNotes`: what the caller established about this PR's CI, handed to the
  judge verbatim; it never revises a placed verdict stored on the head, while
  a stored unclassified failure is judged again once the notes change.
- `acceptedFailures`: `[{ key, reason, scope }]`, `key` the 16-hex one the
  result shows beside a failure: never fixed, and a run red only from them
  passes, listing them. A key covers every failure shown with it, such as
  two HIL cells failing the same way. Per launch.
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
- `yieldOnChange`: run cycles up to `maxCycles` with both lanes, pausing (status
  `paused`, reason `yielded`) only after a cycle that pushed a repair, changed
  the handoffs, recorded a correction, or ended with no usable observation; a
  cycle that only waited (CI settling, bots pending, owed replies, a SonarCloud
  re-read) goes on in the same launch. A stop and the last cycle end the launch
  as without it. Not with `yieldAfterCycle` or a single lane.
- `lane`: `both` (default), `ci` or `reviews`; a single lane needs
  `yieldAfterCycle` and never declares the PR done.
- `state` or `stateRef: { outputFile, digest }`: a previous launch's state, or
  the saved output holding it and its `stateDigest`, loaded as checksummed chunks.
- `adoptHead`: full SHA of audited commits on top of the state's
  `expectedHead` (a caller's repair, or a push made outside the workflow):
  the chain is audited, published under `autoPush`, and the run continues
  from it; per launch. Without a pinned state (a first launch, or the state
  of one that stopped before its pin, such as `wrong-head`), the chain's base
  is the live PR head. A merge from the PR's base branch is one link, its
  paths those it leaves unlike both parents; it is from the base when the
  branch's live tip or the base GitHub recorded holds its second parent. That
  record is stale until the head is pushed, so a merge newer than it needs the
  live tip in the checkout (fetch the base branch), or the audit stops with
  `fetch it`. A PR that conflicts with its base
  stops `pr-conflicting`; the merge that resolves it rejoins by `adoptHead`.
- `adoptProtected`: exact repository-relative paths matching `protected` that a
  base merge in the `adoptHead` chain may change, such as a conflict resolved
  in a protected roster; only with `adoptHead`, per launch. A path an
  unpublished ordinary commit of the chain also touches stays refused unless
  `adoptReviewedProtected` names every unpublished commit changing it, and
  writers and hooks stay bound by `protected`.
- `adoptReviewedProtected`: `[{ sha, paths }]`, `sha` a full 40-hex commit of
  the `adoptHead` chain and `paths` the exact protected paths it changes: the
  caller's word that those changes passed the review the paths require (such
  as the peer review a repository's `CLAUDE.md` asks for). Each unpublished
  commit changing a protected path needs its own entry naming that path, a
  rename both its names, a base merge included; approving one commit approves
  no other. Only with `adoptHead`, per launch; writers and hooks stay bound by
  `protected`.
- `rebasedHead`: full SHA of the PR head after a history rewrite (rebase,
  force-push) the user authorized: with the checkout at it and no unpublished
  candidate, the state is re-pinned to it unaudited, keeping cycles used,
  answers, decisions, holds and deferrals; passed only on the user's word,
  never with `adoptHead`; per launch.
- `rejudge`: `[{ commentId, reason }]`: answered comments the user named for
  the caller to judge again, such as replies citing code a rewrite dropped,
  resolved threads included; passed only on the user's word. Each must have
  an answer of ours in the state. It is handed off, `rejudge: { reason }` on
  the handoff beside any repair or edit, and stays so across launches until a
  `replySettlements` entry settles the comment; omit it once settled, since
  passing it again asks again. Per launch.
