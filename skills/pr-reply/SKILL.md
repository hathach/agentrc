---
name: pr-reply
description: Post replies to PR review comments from a manifest, read each back, and resolve its thread only on a match. Use when a workflow or agent must answer bot or human review comments on a pull request.
---

# Replying to PR review comments

`scripts/reply.py` is the only way a reply reaches a PR. It takes a manifest
of `{commentId, body, digest}` entries and, for each: finds which of three
things on the PR the id names (an inline review comment, an issue comment, or
a review whose body carries the finding), reuses an identical reply of ours if
one is already there, otherwise posts the body once, reads the posted comment
back, and resolves the review thread only when body, thread, author and PR all
match. An issue comment and a review body have no thread: the reply is a PR
comment quoting the original's URL, and nothing is resolved. Any other reply of
ours to the comment (in its thread after it, or quoting it) blocks the post: its receipt
names that reply with `verified: false`, for the caller to reconcile. An entry
with `"secondAnswer": true`, for a comment edited after we answered it, may go
beside our inline reply; a quoting reply of ours that gives the same kind of
answer (a `Fixed in ` note, or anything else) still blocks it. Its receipts are
the evidence; a `201` from GitHub is not.

```bash
R="${AGENTRC_DIR:-$HOME/.claude}/skills/pr-reply/scripts/reply.py"
python3 "$R" --pr <N> --manifest <file.json>
# file.json: {"replies": [{"commentId": 4013179956, "body": "...", "digest": "<8 hex>"}]}
python3 "$R" --digest "$body"      # the digest of a body, for a manifest written by hand
python3 "$R" --pr <N> --inspect <commentId>:<replyId> ...   # read our replies, post nothing
python3 "$R" --pr <N> --inspect <commentId>   # our newest reply; its `original` and `originalDigest` too,
                                             # also on exit 1 "no reply of ours": how to read the comment itself
python3 "$R" --pr <N> --reuse <file.json>
# file.json: {"reuses": [{"commentId": ..., "replyId": ..., "bodyDigest": "...", "originalDigest": "..."}]}
python3 "$R" --pr <N> --edit <file.json>
# file.json: {"edits": [{"commentId": ..., "replyId": ..., "body": "...", "digest": "...", "bodyDigest": "...", "originalDigest": "..."}]}
python3 "$R" --pr <N> --manifest <file.json> --receipt <path>   # also --reuse, --edit: replay a saved run
```

Every entry carries its body's digest (`--digest` by hand); the script refuses
one that does not match, so a body copied wrong never reaches the PR. The last
stdout line is `{"receipts": [...], "seal": ...}`, one receipt per entry: read
that line, not the exit code, for what was posted. The docstring (`reply.py
--help`) is the contract for the digests, the receipt fields, the exit codes
and `--receipt` replay.

## Judgment

- **The body is the caller's.** Write the manifest with the exact text you
  were given; do not paraphrase, shorten, quote or annotate it. A reply that
  is a PR comment gets the original's URL as a quote line above the text, so
  the reader can find what it answers; the script adds that itself.
- **A pending review of ours blocks.** GitHub refuses our review replies while
  our account holds a PENDING review on the PR, so a manifest then posts
  nothing and exits 2 naming it. Report it for the human to submit or delete;
  never submit or delete it yourself.
- **Once.** Never post or edit by hand with `gh api`, never a trial or
  placeholder comment, never a delete. If a run's outcome is unknown, run
  the script again with the same manifest: it reuses what it finds and posts
  only what is missing.
- **Receipts, verbatim.** Return the JSON line unchanged to whoever asked. A
  receipt with `verified: false` and a `replyId` is a reply that exists with the
  wrong content; that is a repair, not a reason to post again. It is settled
  on that reply only when someone judged the body `--inspect` returned to
  answer every point the comment is owed now: `--reuse` with the digests from
  that inspection reads both again, posts nothing and resolves the thread.
  A reply of ours the caller's grant lets it correct is instead put right
  with `--edit`, the offered body or an evidence-backed correction of it, and
  the same inspection's digests: it reads both again, edits only that reply,
  reads it back and resolves the thread. Otherwise it stays for the caller.
- **A reply is not agreement.** A resolved thread means our answer was
  published, not that the reviewer accepted it; what the reviewer says next
  is a new comment to read, not something this script knows about.
