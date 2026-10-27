# cowork reference

Details `SKILL.md` leaves out. `scripts/cowork.py --help` and its docstring
are the source of truth.

## Flags SKILL.md leaves out

`--to codex|claude` picks the coworker, by default the other CLI: Codex from
Claude Code, Claude from Codex. `--read-only` asserts a read-only lane,
refused on `main` or a worktree lane. `COWORK_READ_ONLY`, which the
`coworker` agent's hook exports, forces it on `send` and `reset` and keeps
`kill` to read-only lanes.
`--model M` and `--effort E` set a Claude lane's pair; `--tier` is for Codex
lanes. Either on the wrong side is refused.

## Delivery

`send` prints the request id, then blocks until the reply is in and prints
it; `--detach` returns after the id. Delivery by `send` or `read` prints the
reply, or a failed turn's diagnostic, on stdout; then on stderr any other
diagnostic and last the receipt, `cowork result <id> <side>/<lane>:
<outcome>, exit <N>[; paths reported: <paths>]; <usage>`; then removes the
request's files, so a reader killed or cut off before that lets a later one
print it all again. On Windows a file still open in another process cannot
be removed: the delivery says so and repeats the receipt, which stays the
last line. A lock left open keeps the whole request, which a later `read`
delivers again; any other file left goes with the lane's next send or its
`reset`, which is refused (exit 3) while a request's file stays open.
Outcomes: `replied` (0); `no-footer`, `empty-reply`, `tree-changed`,
`unverified` (4); `failed`, `killed`, `died` (1). A writer's reply owes a
"Files touched" line; a `--no-edit` request, or one to a read-only lane,
owes it only when it changed a file. Paths a successful reply's line names,
other than none, reach the receipt as `paths reported:`, what the coworker
says it touched; they leave the verdict alone. A refusal
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

## Windows

Run it with a native Windows CPython (`python` on a stock install, or
MinGW's); a Cygwin or MSYS-runtime one is refused. The CLI and `git` run
only as PATH alone finds them, never from the working directory, the
checkout, or a relative PATH entry: `git.exe`, and the CLI's `.exe` before
its `.cmd` or `.bat`. A batch-file CLI runs through `cmd.exe`, which would
reparse its arguments, so a turn whose arguments or checkout path hold one
of `%^&|<>!"` fails (exit 1) before the CLI runs: install the native `.exe`
or move the checkout.

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
no worktree lane. When herdr lists your checkout as a linked worktree open in
a workspace, a new or recreated tree is the sibling
`<checkout>--cowork-<side>-<lane>` instead, which herdr's removal of the
checkout leaves to the worktree-children plugin; herdr on PATH is asked
whatever `HERDR_ENV` says. A send that needs a new tree is refused (exit 3)
when herdr cannot answer or answers with a malformed entry, or `HERDR_ENV` is
set without herdr on PATH. A lane uses whichever place holds its tree, so
herdr is not asked again while it exists; a tree at both places, or one that
is not a linked worktree of this repository on the lane's branch (a symlink,
your checkout, another repository's tree), is refused before any change. While
a sibling lane has a request outstanding, running or undelivered (a killed one
too), cowork holds `git worktree lock` on its tree, which the plugin keeps;
delivering the last one, or `reset`, unlocks it, and a lock someone else took
is never touched. `reset` removes a worktree lane's tree and branch, refused
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
