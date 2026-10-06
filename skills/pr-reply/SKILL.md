---
name: pr-reply
description: Post replies to PR review comments from a manifest and prove they landed. Use when a workflow or agent must answer bot or human review comments on a pull request; the script posts each body once, reads it back, and resolves the thread only when the read-back matches.
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
R=~/.claude/skills/pr-reply/scripts/reply.py
python3 $R --pr <N> --manifest <file.json>
# file.json: {"replies": [{"commentId": 4013179956, "body": "...", "digest": "<8 hex>"}]}
python3 $R --digest "$body"      # the digest of a body, for a manifest written by hand
python3 $R --pr <N> --inspect <commentId>:<replyId> ...   # read our replies, post nothing
python3 $R --pr <N> --reuse <file.json>
# file.json: {"reuses": [{"commentId": ..., "replyId": ..., "bodyDigest": "...", "originalDigest": "..."}]}
python3 $R --pr <N> --edit <file.json>
# file.json: {"edits": [{"commentId": ..., "replyId": ..., "body": "...", "digest": "...", "bodyDigest": "...", "originalDigest": "..."}]}
python3 $R --pr <N> --manifest <file.json> --receipt <path>   # also --reuse, --edit: replay a saved run
```

Every entry carries the body's digest (FNV-1a, 32-bit, over code points); the
script refuses an entry whose body does not match it, so a body copied wrong
never reaches the PR. A workflow computes the digests itself; by hand, use
`--digest`. A reply's digest, in a receipt, an inspection or a `--reuse` or
`--edit` entry, is always its text's, the quote line left out, so a posting
receipt's `digest` settles that reply under `--reuse`. The original's digest
is `scripts/comment_digest.py`'s, the one pr-babysit harvests with: what
CodeRabbit rewrites at its comment's end does not change it. An inspection or
a saved receipt from before these rules may carry an older digest: inspect
again, with a fresh receipt path.

The last stdout line is `{"receipts": [...], "seal": ...}`, one receipt per manifest entry (the seal is pr-babysit's check that a relayed copy is exact):
`kind` (`review`, `issue` or `review-body`; `none` when all three were searched
and the id is on none of them, so the caller owes it nothing; `null` when a
lookup failed before that was known), `replyId`, `digest`, `sent` (a POST was issued;
with `replyId` null the response was lost and the reply may exist), `posted`
(false when an identical reply was reused), `verified` (true, false on a
mismatch, null when the read-back could not be fetched), `resolved` (true when the
thread was resolved, false when GitHub refused to resolve it, left out when
unknown: for the two kinds without a thread, or when an API error cut the
resolve short), `error` (left out when there is none). Exit 0 when every reply is verified
and every review thread resolved, 1 otherwise, 2 for a bad manifest or an
unreachable repo. `sent` and `posted` describe the run that recorded them, so a
rerun that finds its earlier post reports both false; `verified` and `resolved`
say whether the reply stands. A caller that may lose a run's output passes
`--receipt <path>`: the same path again to get that run's receipt back, a
fresh path when the replies must be read back anew.

## Judgment

- **The body is the caller's.** Write the manifest with the exact text you
  were given; do not paraphrase, shorten, quote or annotate it. A reply that
  is a PR comment gets the original's URL as a quote line above the text, so
  the reader can find what it answers; the script adds that itself.
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
  A reply your task's own recorded attempt posted may instead be put right
  with `--edit`, the offered body or an evidence-backed correction of it, and
  the same inspection's digests: it reads both again, edits only that reply,
  reads it back and resolves the thread. Otherwise it stays for the caller.
- **A reply is not agreement.** A resolved thread means our answer was
  published, not that the reviewer accepted it; what the reviewer says next
  is a new comment to read, not something this script knows about.
