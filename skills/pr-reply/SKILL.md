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
back, and resolves the review thread only when body, parent, author and PR all
match. An issue comment and a review body have no thread: the reply is a PR
comment quoting the original's URL, and nothing is resolved, so a reply of ours
quoting it that gives the same kind of answer (a `Fixed in ` note, or anything
else) in other words blocks the post: its receipt names that reply with
`verified: false`, for a human to reconcile. Its receipts are the evidence; a
`201` from GitHub is not.

```bash
R=~/.claude/skills/pr-reply/scripts/reply.py
python3 $R --pr <N> --manifest <file.json>
# file.json: {"replies": [{"commentId": 4013179956, "body": "...", "digest": "<8 hex>"}]}
python3 $R --digest "$body"      # the digest of a body, for a manifest written by hand
python3 $R --pr <N> --inspect <commentId>:<replyId> ...   # read our replies, post nothing
python3 $R --pr <N> --reuse <file.json>
# file.json: {"reuses": [{"commentId": ..., "replyId": ..., "bodyDigest": "...", "originalDigest": "..."}]}
```

Every entry carries the body's digest (FNV-1a, 32-bit, over code points); the
script refuses an entry whose body does not match it, so a body copied wrong
never reaches the PR. A workflow computes the digests itself; by hand, use
`--digest`.

The last stdout line is `{"receipts": [...]}`, one per manifest entry:
`kind` (`review`, `issue` or `review-body`; `none` when all three were searched
and the id is on none of them, so the caller owes it nothing; `null` when a
lookup failed before that was known), `replyId`, `digest`, `sent` (a POST was issued;
with `replyId` null the response was lost and the reply may exist), `posted`
(false when an identical reply was reused), `verified` (true, false on a
mismatch, null when the read-back could not be fetched), `resolved` (null for
the two kinds without a thread), `error`. Exit 0 when every reply is verified
and every review thread resolved, 1 otherwise, 2 for a bad manifest or an
unreachable repo.

## Judgment

- **The body is the caller's.** Write the manifest with the exact text you
  were given; do not paraphrase, shorten, quote or annotate it. A reply that
  is a PR comment gets the original's URL as a quote line above the text, so
  the reader can find what it answers; the script adds that itself.
- **Once.** Never post by hand with `gh api`, never a trial or placeholder
  comment, never an edit, never a delete. If a run's outcome is unknown, run
  the script again with the same manifest: it reuses what it finds and posts
  only what is missing.
- **Receipts, verbatim.** Return the JSON line unchanged to whoever asked. A
  receipt with `verified: false` and a `replyId` is a reply that exists with the
  wrong content; that is a repair, not a reason to post again. It is settled
  on that reply only when someone judged the body `--inspect` returned to
  answer every point the comment is owed now: `--reuse` with the digests from
  that inspection reads both again, posts nothing and resolves the thread.
  Otherwise it stays for a human.
- **A reply is not agreement.** A resolved thread means our answer was
  published, not that the reviewer accepted it; what the reviewer says next
  is a new comment to read, not something this script knows about.
