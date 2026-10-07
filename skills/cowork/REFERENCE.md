# cowork reference

Details `SKILL.md` leaves out. `scripts/cowork.py --help` and its docstring
are the source of truth.

## Flags SKILL.md leaves out

`--to codex|claude` picks the coworker, by default the other CLI: Codex from
Claude Code, Claude from Codex. `--read-only` asserts a read-only lane.
`--model M` and `--effort E` set a Claude lane's pair; `--tier` is for Codex
lanes. Either on the wrong side is refused.

## Delivery

`send` prints the request id, then blocks until the reply is in and prints
it; `--detach` returns after the id. Delivery by `send` or `read` prints the
reply, or a failed turn's diagnostic, on stdout; then on stderr any other
diagnostic and last the receipt, `cowork result <id> <side>/<lane>:
<outcome>, exit <N>[; paths reported: <paths>]; <usage>`; then removes the
request's files, so a reader killed or cut off before that lets a later one
print it all again. Outcomes: `replied` (0); `no-footer`, `empty-reply`,
`tree-changed`, `unverified` (4); `failed`, `killed`, `died` (1). A refusal
(exit 3) prints no receipt. A Codex turn's usage comes from its rollout
under `$CODEX_HOME/sessions` (`~/.codex/sessions` when unset), the turn
matched by its request header, else from the stream's session totals,
labelled `session input`; `usage unavailable` when neither reads. Plain `read <id>` refuses a request still running;
`read --wait` blocks until its runner and everything it spawned let go.
`read --out FILE` writes what the delivery would print, reply, diagnostics
and receipt, to FILE in one step (a temp file beside it, renamed over it;
the caller removes it), then prints on stderr only `cowork result <id>
<side>/<lane>: <outcome>, exit <N>; written to <FILE>`, with FILE made
absolute, and exits as without it. A FILE that cannot be written is a
refusal: a `could not write` line, exit 3, and the request kept for a read
with a writable FILE. A refusal writes nothing. Exit
3 with "no request" means the request is absent here: delivered already,
never sent, or removed by `reset`, which also drops undelivered requests
with the lane's session. The coworker's own store keeps the whole session,
prompts included, where `codex resume` / `claude --resume` find it.

`status` lists each lane holding a request, running or undelivered, with
its requests and their state, then one line counting the idle lanes;
`status --all` lists every lane with a session instead.

## Exit codes

1 the turn failed, or never ran because the tree could not be checked
before it; 3 unknown or delivered request, a `--out` FILE that cannot be
written, the lane busy, a lane of another
kind than the flags assert, a pinned or kindless lane, a dirty worktree
lane, or reset refused; 4 an empty reply, a writer's reply without its
"Files touched" line, or under `--no-edit` the tree changed or could not be
checked after the turn, with git's diagnostic. A `--no-edit` request, or
one to a read-only lane, owes the line only when it changed a file. Paths
a successful reply's line names, other than none, reach the receipt as `paths
reported:`, what the coworker says it touched; they leave the verdict
alone.

## `--no-edit`

Claude runs in plan mode; Codex is asked, since its read-only sandbox would
also forbid the temp files a test suite needs. Both are then checked: the
branch HEAD names, its commit, the index and the worktree as git would stage
it, before and after the turn. The
check sees what git sees, not effects outside the checkout, and an edit you
make during a read-only review changes the snapshot too.

## Lanes

A worktree lane lives at `.worktrees/cowork-<side>-<lane>` on branch
`cowork/<host branch>/<side>-<lane>`, created at your HEAD (its `base`); the
request header names the tree, branch and that commit. A detached host gets
no worktree lane. `reset` removes a worktree lane's tree and branch, refused
while the tree is dirty or the branch has commits your HEAD lacks: merge the
lane branch, or integrate it by hand and retire the worktree (`git worktree
remove`, then `git branch -D`). A lane that has a session but no record of
its kind is refused until `reset`. Lane names are `[a-z0-9-]`, up to 40.

## Models

| Tier | Model and effort |
|---|---|
| `review` | `gpt-6-astra` at `high` |
| `expert` | `gpt-6-astra` at `xhigh` |
| `default` | the host preset: `astra` (`gpt-6-astra` at `high`) or `sol` (`gpt-6.1-sol` at `high`, also when unset) |

`cowork.py default <preset>` saves the preset in
`$XDG_CONFIG_HOME/agentrc/cowork-default` (`~/.config/...` when unset), for
this host only. A new Codex lane
starts on `default`; a send without `--tier` keeps the lane's tier, resolved
at each send, so a flip reaches every `default` lane on its next send while
a request already running keeps its pair. A Codex lane saved with a model but
no tier shows as `pinned` in `status --all` and is refused until a send names its
`--tier`. A Claude lane keeps its `--model` and `--effort`,
else `opus` at `high`. The request header tells the coworker its pair.
