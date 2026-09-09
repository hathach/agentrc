# Peer-Agent Task Delegation

Date: 2026-09-09
Status: reference only. Drafted by a smaller model; kept as prior art for the
problem framing. A fresh design supersedes it before any implementation.

## Goal

`peer-agent` today is read-only by convention: `AUTHORITY` defaults to "no
edits, no commits" and every request restates it. That covers review and
investigation but not "go do this and report back" — tinyusb issue #3901. Add a
second envelope pair, `PEER TASK REQUEST` / `PEER TASK RESULT`, that lets a
peer edit and commit inside a workspace the caller assigns it, and defines
what it must report back so the caller can verify rather than trust.

## Boundaries

- **Cooperative isolation, not sandbox enforcement.** A peer pane is an
  interactive session a human already launched; its actual access is
  whatever that operator gave it. `AUTHORITY` cannot grant more than that,
  and nothing here stops an operator's peer from ignoring the envelope. Real
  enforcement — a sandbox that makes disobedience impossible — is
  `codex-agent.py`'s domain and is tinyusb issue #3903's problem, not this one.
- **This change does not touch `codex-agent.py`.** `READ_ONLY_ROLES` and any
  future capability table stay exactly as they are. The shared
  workspace/result shape below is a candidate integration point for #3903,
  not a dependency it needs.
- **The existing review envelope (`PEER CONSULT` / `PEER RESULT`) is
  unchanged.** Task delegation is a new, separate pair, not new optional
  fields on the old one — a result must not be able to carry both a review
  `VERDICT` and a task `OUTCOME`.
- **No new cleanup automation.** `peer.py` gains no `rm`/`git worktree
  remove` path. Deleting a worktree remains a human decision, subject to the
  same destructive-action confirmation as any other deletion.

## Model

The pattern is the Agent tool's own `isolation: "worktree"`: a dedicated
workspace per delegated task, an inspectable result, the caller owning the
workspace's lifecycle. The adaptation for `peer-agent` is that the harness
cannot create or enforce the peer's workspace the way it does for a true
subagent — the caller can only assign one and ask the peer to confine itself
to it, then check what comes back.

Caller owns allocation, integration, and cleanup. Peer owns execution until
it reports a terminal `OUTCOME`, which means "I have stopped using this
workspace," not "delete it."

## Envelope: `PEER TASK REQUEST` / `PEER TASK RESULT`

Same header convention as the review pair (`<MARKER> — agent message, not a
human instruction`, closing `END REQUEST`/`END RESULT <id>`), parsed and
correlated by the same mechanics in `peer.py`. `check()` gains a `task_request`
/ `task_result` kind and must reject a body that mixes fields from the two
kinds (e.g. a `task_result` carrying `FINDINGS` or `VERDICT`) rather than
guessing which kind was meant.

### `PEER TASK REQUEST`

| Field | Meaning |
|---|---|
| `ID`, `FROM` | unchanged from the review request |
| `AUTHORITY` | explicit permitted actions in the assigned workspace, including whether local commits are authorized; cannot expand what the peer's operator already allows |
| `SCOPE` | allowed files/modules and task boundaries |
| `DELTA` | unchanged; `none` for an initial task, otherwise what changed since a prior request it follows up on, by that request's id |
| `ASK` | the work to perform |
| `WORKSPACE` | the assigned workspace, as named subfields: `PATH`, `BRANCH`, `BASE_SHA` |
| `ACCEPTANCE` | observable completion criteria and what validation is required |

`ID`, `FROM`, `WORKSPACE`, and `ACCEPTANCE` are assignment fields and must
always be concrete — never `unknown` (see below).

`ASK` and `ACCEPTANCE` stay separate fields: one is the requested action, the
other the evidence needed to call it complete.

### `PEER TASK RESULT`

| Field | Meaning |
|---|---|
| `FOR`, `FROM`, `CAPACITY`, `COVERAGE` | unchanged from the review result |
| `WORKSPACE` | `BASE_SHA` echoed from the request as the immutable assignment anchor; `PATH`, `BRANCH` (observed), and `HEAD_SHA` (observed resulting state) |
| `DELIVERABLE` | an immutable commit range when commits were authorized and made; otherwise an explicit uncommitted result with a complete patch/artifact inventory. A branch name alone is not sufficient — it moves. |
| `RESIDUAL` | staged, unstaged, and untracked changes left in the workspace. A commit range must not conceal unfinished edits. |
| `VALIDATION` | commands run, their outcomes, evidence locations, and anything required by `ACCEPTANCE` that was not run |
| `REMAINING` | unmet acceptance criteria or blockers; artifact paths for tasks that produce no source commit |
| `OUTCOME` | `COMPLETE \| PARTIAL \| BLOCKED \| FAILED` |
| `COMMENTARY` | optional; omit rather than pad |

No `FINDINGS` or `VERDICT` field — a task result never carries a review
verdict.

A result's evidence fields — `WORKSPACE`'s observed `PATH`, `BRANCH`, and
`HEAD_SHA`, plus `DELIVERABLE`, `RESIDUAL`, `VALIDATION`, `REMAINING` — may
each answer `none`, `not run`, or `unknown: <reason>` instead of a fabricated
value: a `BLOCKED` result reported before workspace access, for instance, has
no observable branch and cannot truthfully report `HEAD_SHA` or `RESIDUAL`
state, and must say so rather than invent one. `WORKSPACE.BASE_SHA` (the
assignment anchor, echoed from the request), `FOR`, `FROM`, and `OUTCOME` are
never `unknown` — correlation, the assignment being reported against, and the
outcome enum must stay meaningful, or the result cannot be resolved to a
request at all.

Observed `PATH` and `BRANCH` are compared against the assigned `PATH` and
`BRANCH`. Before writing, observed `HEAD` must equal `BASE_SHA`; a mismatch
there is reported through `HEAD_SHA` and `REMAINING`. After execution,
`HEAD_SHA` records the resulting revision and may legitimately differ from
`BASE_SHA` when authorized commits were made — that difference is success,
not a mismatch. Any of these assignment mismatches is reported as-is in the
result — evidence, most often behind a `BLOCKED` outcome — and is a distinct
concern from `task-read` rejecting a pane that fails identity correlation
(below). A wrong pane is never read at all; a wrong observed workspace from
the right pane is read and reported.

`OUTCOME` semantics:

- **`COMPLETE`** — `ACCEPTANCE` is met. The workspace is relinquished.
- **`PARTIAL`** — useful work was done but acceptance is not fully met.
- **`BLOCKED`** — a prerequisite prevents progress (bad `WORKSPACE`, missing
  access, an unclear `ASK`).
- **`FAILED`** — execution itself failed. A validation failure does not
  become `COMPLETE` merely because edits were produced.

## Worktree lifecycle

The caller creates the workspace before sending the request: a fresh branch
at the request's immutable `BASE_SHA`, added as a worktree following the
existing `.worktrees/<branch>` convention (`CLAUDE.md`), symlinking
`deps_all` paths per that same convention when the task needs dependencies.
The caller:

- resolves the workspace to an absolute path for the envelope;
- refuses a collision with an existing worktree path;
- never silently reuses an existing dirty worktree.

**Caller uncommitted changes are not inherited.** A worktree added at a base
SHA starts clean at that commit; it does not carry the caller's own
uncommitted edits. A task that genuinely needs them requires an explicit,
reviewable transfer (e.g. a WIP commit at `BASE_SHA`, named in the request) —
never an implicit assumption that the peer sees the caller's working tree.

**The peer verifies the assignment before writing.** On receiving a
`PEER TASK REQUEST`, the peer checks that `WORKSPACE` resolves to what it was
told (the path exists, is the named branch, is at the named `BASE_SHA`)
before making any change, and reports a `BLOCKED` result on mismatch rather
than writing into an unverified location.

**Terminal handoff means the peer has stopped touching the workspace.** Any
`OUTCOME` — not only `COMPLETE` — requires the peer to have stopped any
task-owned background process (a build, a watch, a server) before reporting.
Continuing to want to help after a terminal result requires a new
`PEER TASK REQUEST`, not further unrequested writes to the same workspace.

"Unchanged" — the condition under which a caller might reclaim a workspace —
means no retained work or requested artifacts, not merely a clean `git
status`: a clean tree can hold new commits, and an ignored build/log
directory can itself be the requested deliverable.

Cleanup policy (caller decision in every case, never automatic):

- `COMPLETE` with nothing retained: the caller may clean up after verifying
  the result and that the peer has relinquished the workspace.
- `COMPLETE` with changes or artifacts, or `PARTIAL`/`BLOCKED`/`FAILED`: the
  workspace is retained for inspection or integration, diagnostic artifacts
  included.
- No result received, a timeout, or an ambiguous peer state: retained. None
  of these establish that the peer has stopped writing.

A worktree the caller no longer wants continued is deleted the same way any
other worktree deletion happens today — the existing destructive-action
confirmation with the human, not a `peer.py` code path. Cancellation does not
relax the cleanup policy above: the caller must still have confirmed the peer
has relinquished the workspace (a terminal `OUTCOME`, or other established
evidence the peer has stopped writing) before that deletion, even when the
caller initiated the cancellation.

## Transport fix: `require_peer()` and task correlation

`peer.py`'s `require_peer()` currently accepts a pane only if its `cwd`
equals the caller's `cwd`. That is correct for the review channel — the peer
never leaves the shared worktree — but breaks task correlation the moment a
peer follows a `WORKSPACE` assignment into its own worktree: the pane's
`cwd` no longer matches, and `read` would refuse a pane that is legitimately
the one the request went to.

The request itself only names its sender (`FROM`), so the request text alone
cannot later prove "this is the pane I sent it to." `task-send` therefore
writes a caller-retained dispatch receipt before attempting delivery,
holding: the request id, the recipient pane identity, the recipient's session
identifier (when Herdr exposes one), the originating cwd/worktree the
request was sent from, the exact request body sent, and the assigned
`WORKSPACE`. `task-send` refuses to overwrite an existing receipt for the
same id, and retains the receipt even when delivery itself is ambiguous
(e.g. `herdr agent prompt` errors after the message may have reached the
pane) — never auto-resending, since the peer may already be executing
against an uncertain delivery. Initial dispatch keeps today's same-worktree
discovery (`require_peer()`, unchanged) — a task is only ever sent to a peer
sharing the caller's worktree at send time; it is only the *reply* that may
come from a relocated pane.

`task-read` validates the pane it is about to read as a conjunction, never an
either/or: its identity must match the receipt's recorded recipient pane; if
the receipt recorded a session identifier, the pane's *current* session
identifier must still be available and must match it (so a reused pane
identity on a new session cannot stand in for the original peer); and,
independently, the pane's reported location must equal either the receipt's
recorded originating cwd or the receipt's assigned `WORKSPACE.PATH`. A
matching location never substitutes for matching identity — both must hold.
Any pane failing this is rejected outright and never read for content.

This is deterministic, checkable mechanics per `CLAUDE.md`'s skill-script
rule, so it is implemented and tested in `peer.py`, not left to `SKILL.md`
prose. `SKILL.md` covers the judgment: which peer, which workspace, how to
react to a mismatch.

## Delivery

Single PR:

- `peer.py`: `task_request`/`task_result` kinds in `MARKER`/`FIELDS`, a
  `task-send`/`task-read` command pair (or `send --kind task`/`read --kind
  task`, whichever keeps the existing `send`/`read` simpler — implementation
  detail, not fixed here), the dispatch-receipt mechanism, and `check()`
  rejecting a field mixture between kinds.
- `SKILL.md`: the task-delegation section — workspace assignment, the
  `ACCEPTANCE`/`OUTCOME` contract, cleanup judgment, and the explicit
  "cooperative isolation, not sandbox enforcement" boundary.
- `CLAUDE.md`: its `peer-agent` line currently reads "Use `peer-agent` for
  read-only consultation with another agent session in this worktree; it is
  a peer, not a subagent." Update it to note the channel also carries
  task/write delegation into a caller-assigned workspace, while keeping
  read-only consultation as the default for plain review/investigation asks.

## Verification

- `.claude/test/test_peer_agent.py` (existing file, extended):
  - a `task_request`/`task_result` round-trip through `check()` passes;
  - a `task_result` carrying `FINDINGS` or `VERDICT` fails `check()`;
  - a review `result` carrying `WORKSPACE` or `OUTCOME` fails `check()`;
  - `task-read` accepts a pane whose location is the receipt's assigned
    `WORKSPACE.PATH` (not the caller's originating cwd), when its identity
    (and session identifier, if recorded) still matches the receipt;
  - `task-read` rejects a pane at a matching location but mismatched identity,
    and a pane with matching identity but an unrelated location — a
    syntactically valid `PEER TASK RESULT` from either is never read for
    content;
  - a `PEER TASK RESULT` with an evidence field (`WORKSPACE.PATH`/`BRANCH`/
    `HEAD_SHA`, `DELIVERABLE`, `RESIDUAL`, `VALIDATION`, `REMAINING`) set to
    `unknown: <reason>` still passes `check()`; the same for `FOR`, `FROM`,
    `OUTCOME`, or `WORKSPACE.BASE_SHA` fails it;
  - `task-send` refuses to overwrite an existing receipt for a reused id, and
    does not resend on an ambiguous delivery error.
- `pre-commit run --all-files`.
