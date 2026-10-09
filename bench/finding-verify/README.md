# finding-verify replay corpus

Ground truth for agentrc#64: whether a finding-verifier that judges a batch of related findings stays as
correct as one dispatch per finding. Each case is one past `verify:dIxJ:k` unit of code-audit, as a
pr-review run gave it, with the pinned commits to replay it against and a truth label adjudicated
independently of the verifier.

- `extract.py`: journals and verify transcripts into cases; run it on the host that ran the reviews.
- `sample.py`: a seeded, stratified draw in clusters.
- `adjudicate.py`: the blind brief, and the merge of adjudicators' answers into `corpus.jsonl`.

Each script's docstring has the rules.

A cluster is one file of one head, hardware claims apart, across scan units and dimensions: the shared
source context #64 names. A comparison that batches per code-audit scan unit instead scores only the
labelled members of each batch. The `hardware` tag reads the claim's own text; a dispatcher that splits by
dimension needs its own rule, which `dimension` lets a comparison apply.

## corpus.jsonl

One case per line: `id`, `repo`, `pr`, `base`, `head`, `dir`, `dimension`, `finding`, `path` and `worktree`
(below), `prompt` (what the verifier was sent), `verifier` (`models` and the verdict it returned), `source`
(the run's unit label), `cluster`, `tags`, and `truth`:
`label` true or false, null while disputed; `status` agreed, lead or disputed; `answers` by adjudicator,
each with its label, evidence and what it needed beyond the code.

A label follows the verifier's own criterion, as `adjudicate.py`'s RULES give it to the adjudicators.

Every pinned commit resolved on GitHub when the corpus was drawn.

## Drawing it again

```bash
extract.py --out cases.jsonl
sample.py --cases cases.jsonl --out sample.jsonl --seed 1 --total 60
adjudicate.py brief --sample sample.jsonl --clusters 0-6 --checkout hathach/tinyusb=$HOME/code/tinyusb \
  --checkout hathach/agentrc=$HOME/code/agentrc > brief.md     # per adjudicator, per range
adjudicate.py merge --sample sample.jsonl --a codex=a1.json,a2.json --b opus=b1.json,b2.json --lead lead=l.json --out corpus.jsonl
```

## Replaying a case

- Fetch `head` (and `base`) into a scratch worktree and send `prompt` unchanged, except that a case naming a
  `worktree` has that prefix replaced with the scratch worktree; `path` is the file relative to the root.
- The prompt leans on context the case does not pin: the finding-verifier role and its Severity section,
  the verdict schema in `workflows/code-audit.js`, and the instructions loaded where the run's session
  started. A comparison fixes them at one agentrc revision and reruns per-finding and batched modes under
  it; the historical `verifier` verdicts are a reference, not that baseline.
- Dead responses and duplicated response ids are faults of a run, not of a case: the comparison harness
  injects them. Same-line findings here are distinct cases with distinct ids.

## This draw

Seed 1, total 60, from 1100 cases extracted on 2026-10-10 out of 12 pr-review runs (tinyusb #3776 to #4017,
agentrc #4): 63 cases in 22 clusters. Adjudicators: `codex` (gpt-6-astra, cowork review tier) and `opus`
(Claude Opus 5.5), each blind to the verifier and to the other; `lead` broke ties after reading both, and
its tiebreaks went through a Codex review that reopened four.

- 47 agreed, 12 settled by the lead, 4 disputed with what decides each in their answers:
  `a4b3d6de5aba` (SAM E70 dual-bank ZLP, a board run), `2b056b060c13` and `7f7d55de67fc` (LPC55 SETUP copy
  without volatile: the shipped compiler's code and the controller's write while latched),
  `7ac6c1913465` (whether tinyusb's TU_ASSERT rule exempts SDK-copied BSP code, the maintainer's call).
- Truth: 40 true, 19 false. The historical per-finding verdicts were right on 47 of 59: 8 true findings
  refuted, 4 false ones confirmed; 11 of 16 on hardware claims.
