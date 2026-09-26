import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = readFileSync(new URL('../workflows/pr-review.js', import.meta.url), 'utf8')
  .replace(/^export const meta = /m, 'const meta = globalThis.__meta = ')
const ABSENT = ['URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'Buffer', 'process', 'fetch', 'structuredClone']

const H = 'b'.repeat(40), MB = 'a'.repeat(40), OLD = 'c'.repeat(40)
const BASE = {
  pr: 7, repo: 'o/r', head: H, mergeBase: MB, scopeBase: MB, mode: 'full', groups: ['src/core'],
  factsDir: '/tmp/ledger/7', hil: { choice: 'boards', boards: [{ board: 'b1', verdict: 'pass', regression: 'none', testedHead: 'b'.repeat(40), report: '/r/b1.json' }] },
  hardwareRelevant: true, autoPost: false,
}
const finding = (why, severity = 'high', line = 10) => ({ file: 'src/core/a.c', line, snippet: 'x', why, severity, confidence: 'high', verdict: { real: true, reason: `holds: ${why}` } })

// Stub agents keyed by label; `stubs.<label>` is the value, or a function of the prompt.
async function run(args, stubs = {}) {
  const calls = []
  const logs = []
  const pick = (label, prompt) => {
    const key = Object.keys(stubs).find(k => k === label) ?? Object.keys(stubs).find(k => label.startsWith(k + ':'))
    const v = key === undefined ? undefined : stubs[key]
    return typeof v === 'function' ? v(prompt, label) : v
  }
  const defaults = {
    check: { ok: true, head: H, top: '/w', ci: { state: 'green', counts: {} }, pins: { mergeBase: args.mergeBase, scopeBase: args.scopeBase, mode: args.mode, groups: args.groups } },
    threads: { file: `${BASE.factsDir}/threads-${H}.json`, count: 0 },
    ledger: { reviews: 0, open: [] },
    disputes: { disputes: [] },
    claims: { claims: [] },
  }
  const agent = async (prompt, options) => {
    calls.push({ label: options.label, prompt: String(prompt), options })
    const v = pick(options.label, prompt)
    if (v !== undefined) return v
    if (options.label in defaults) return defaults[options.label]
    if (options.label === 'write') {
      const fs = JSON.parse(/Findings: (\[.*\])$/s.exec(prompt)[1])
      return { summary: '- Overall fine.', comments: fs.map(f => ({ finding: f.finding, body: `**${f.severity}**: ${f.why}` })) }
    }
    if (options.label === 'check-draft') return { bad: [], summaryBad: false }
    if (options.label === 'covered') return { matches: [] }
    throw new Error(`unstubbed ${options.label}`)
  }
  const audits = []
  const workflow = async (name, a) => {
    assert.equal(name, 'code-audit')
    audits.push(a)
    const v = stubs.audit
    return v === undefined ? { confirmed: [], dropped: [], unverified: [] } : (typeof v === 'function' ? v(a) : v)
  }
  const parallel = thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)))
  const fn = new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget', ...ABSENT, body)
  const result = await fn(args, agent, null, parallel, () => {}, m => logs.push(String(m)), workflow, null, ...ABSENT.map(() => undefined))
  return { result, calls, logs, audits, labels: calls.map(c => c.label) }
}

const pinned = (a) => ({ ok: true, head: H, top: '/w', ci: { state: 'green' }, pins: { mergeBase: a.mergeBase, scopeBase: a.scopeBase, mode: a.mode, groups: a.groups } })
const board = (verdict, regression, testedHead = H) => ({ board: 'b1', verdict, regression, testedHead, report: '/r/b1.json' })
const words = (s) => s.split(/\s+/).filter(Boolean).length
const audited = (...fs) => ({ confirmed: [{ dir: 'src/core', dim: 'correctness: x', findings: fs }], dropped: [], unverified: [] })

test('meta names the workflow and its phases', async () => {
  await run(BASE)
  assert.equal(globalThis.__meta.name, 'pr-review')
  assert.deepEqual(globalThis.__meta.phases.map(p => p.title), ['Check', 'Context', 'Review', 'Judge', 'Draft'])
})

test('args that would change the verdict silently are refused before any agent', async () => {
  const bad = [
    {}, { ...BASE, pr: 0 }, { ...BASE, repo: 'x' }, { ...BASE, head: 'HEAD' }, { ...BASE, mode: 'same' },
    { ...BASE, scopeBase: OLD }, { ...BASE, mode: 'incremental' }, { ...BASE, groups: [] }, { ...BASE, groups: ['../x'] },
    { ...BASE, factsDir: 'rel' }, { ...BASE, hil: undefined }, { ...BASE, hil: { choice: 'boards', boards: [] } },
    { ...BASE, hil: { choice: 'boards', boards: [{ board: 'b', verdict: 'ok', regression: 'none' }] } },
    { ...BASE, hil: { choice: 'boards', boards: [{ board: 'b', verdict: 'pass', regression: 'none' }] } },
    { ...BASE, hil: { choice: 'boards', boards: [board('pass', 'none', OLD)] } },
    { ...BASE, hardwareRelevant: undefined }, { ...BASE, autoPost: 'yes' }, { ...BASE, dimensions: [] },
  ]
  for (const a of bad) await assert.rejects(run(a), /args|must/, JSON.stringify(a))
})

test('a failed or moved-head check stops before any review work', async () => {
  for (const check of [null, { error: 'head moved: expected b' }, { ok: true, head: OLD, ci: { state: 'green' } }]) {
    const { result, labels } = await run(BASE, { check })
    assert.equal(result.status, 'blocked')
    assert.equal(result.reason, 'check-failed')
    assert.deepEqual(labels, ['check'])
  }
})

test('a clean full review approves only with green CI, full coverage and hardware covered', async () => {
  const { result, audits, calls } = await run(BASE)
  assert.equal(result.verdict.event, 'APPROVE')
  assert.deepEqual(audits[0], { dirs: ['src/core'], dimensions: audits[0].dimensions, diff: { base: MB, head: H } })
  assert.equal(audits[0].dimensions.length, 4)
  assert.ok(calls.filter(c => ['check', 'threads', 'ledger'].includes(c.label)).every(c => c.options.model === 'haiku'))
  assert.match(calls.find(c => c.label === 'check').prompt, /prepare\.py --check --pr 7 --repo o\/r --expected-head b{40}/)
  assert.doesNotMatch(result.draft.body, /agentrc|Claude|generated/i)

  const auto = await run({ ...BASE, autoPost: true })
  assert.equal(auto.result.verdict.event, 'COMMENT')
  assert.ok(auto.result.verdict.reasons.includes('approval recommended; auto-post never approves'))

  const none = await run({ ...BASE, hil: { choice: 'none' } })
  assert.equal(none.result.verdict.event, 'COMMENT')
  assert.ok(none.result.verdict.reasons.includes('no hardware run on a hardware-relevant change'))
  assert.equal((await run({ ...BASE, hil: { choice: 'none' }, hardwareRelevant: false })).result.verdict.event, 'APPROVE')
})

test('CI not green, lost coverage or a failed board cap the verdict at COMMENT', async () => {
  const pending = await run(BASE, { check: { ...pinned(BASE), ci: { state: 'pending', actionRequired: 2 } } })
  assert.equal(pending.result.verdict.event, 'COMMENT')
  assert.ok(pending.result.verdict.reasons.includes('CI pending (2 awaiting maintainer approval)'))
  const unobserved = await run(BASE, { check: { ...pinned(BASE), ci: { state: 'unobserved' } } })
  assert.ok(unobserved.result.verdict.reasons.includes('CI unobserved'))
  const dropped = await run(BASE, { audit: { confirmed: [], dropped: [{ dir: 'src/core', dim: 'x' }], unverified: [] } })
  assert.equal(dropped.result.verdict.event, 'COMMENT')
  assert.match(dropped.result.verdict.reasons.join(), /coverage incomplete: 1 scan unit/)
  const failed = await run({ ...BASE, hil: { choice: 'boards', boards: [board('fail', 'unknown')] } })
  assert.equal(failed.result.verdict.event, 'COMMENT')
})

test('a blocking finding or a verified regression requests changes; minor ones only comment', async () => {
  const high = await run(BASE, { audit: audited(finding('buffer overrun')) })
  assert.equal(high.result.verdict.event, 'REQUEST_CHANGES')
  assert.deepEqual(high.result.draft.comments, [{ path: 'src/core/a.c', line: 10, finding: 0, body: '**high**: buffer overrun' }])
  assert.equal(high.result.findings[0].why, 'buffer overrun', 'a comment indexes the result findings')
  const low = await run(BASE, { audit: audited(finding('naming', 'low')) })
  assert.equal(low.result.verdict.event, 'COMMENT', 'only a nit leaves approval open')
  const nit = await run(BASE, { audit: audited(finding('naming', 'nit')) })
  assert.equal(nit.result.verdict.event, 'APPROVE')
  const medium = await run(BASE, { audit: audited(finding('leak on error path', 'medium')) })
  assert.equal(medium.result.verdict.event, 'COMMENT')
  const abs = await run(BASE, { audit: audited({ ...finding('overrun'), file: '/w/src/core/a.c' }) })
  assert.equal(abs.result.draft.comments[0].path, 'src/core/a.c', 'an absolute path under the checkout is made repository-relative')
  const major = await run(BASE, { audit: audited(finding('use after free', 'Major')) })
  assert.equal(major.result.verdict.event, 'REQUEST_CHANGES', "code-verifier's major blocks, whatever its case")
  assert.equal(major.result.findings[0].severity, 'high', 'stored on the one scale')
  const old = await run(BASE, { audit: audited(finding('overrun', 'blocker')) })
  assert.equal(old.result.verdict.event, 'REQUEST_CHANGES', 'a historical blocker still blocks')
  const minor = await run(BASE, { audit: audited(finding('naming', 'minor')) })
  assert.equal(minor.result.verdict.event, 'COMMENT', "code-verifier's minor is above a nit")
  const reg = await run({ ...BASE, hil: { choice: 'boards', boards: [board('fail', 'verified')] } })
  assert.equal(reg.result.verdict.event, 'REQUEST_CHANGES')
  assert.ok(reg.result.verdict.reasons.includes('verified HIL regression on b1'))
})

test('a drafted comment that adds a claim, or is lost, falls back to the finding\'s own words', async () => {
  const flagged = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), 'check-draft': { bad: [1] } })
  assert.deepEqual(flagged.result.draft.comments.map(c => c.body), ['**high**: a', '**high**: b'])
  assert.match(flagged.result.draft.body, /Overall fine/, 'a clean summary stays when one comment is flagged')
  const summary = await run(BASE, { audit: audited(finding('a')), 'check-draft': { bad: [], summaryBad: true } })
  assert.doesNotMatch(summary.result.draft.body, /Overall fine/, 'a summary that adds a claim is dropped')
  const dead = await run(BASE, { audit: audited(finding('a')), write: null })
  assert.deepEqual(dead.result.draft.comments.map(c => c.body), ['**high**: a'])
})

test('text over its limit is shortened once, then marked long for the human, never cut', async () => {
  const wordy = (n) => 'w '.repeat(n).trim()
  const long = { summary: 'Prose, not bullets.', comments: [{ finding: 0, body: wordy(81) }, { finding: 1, body: '**high**: b' }] }
  const fixed = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), write: long,
    shorten: { comments: [{ finding: 0, body: '**high**: a' }], answers: [], summary: '- a is broken' } })
  const ask = fixed.calls.find(c => c.label === 'shorten')
  assert.match(ask.prompt, /"finding":0/)
  assert.doesNotMatch(ask.prompt, /"body":"\*\*high\*\*: b"/, 'only the text over its limit is sent')
  assert.deepEqual(fixed.result.draft.comments.map(c => c.body), ['**high**: a', '**high**: b'])
  assert.ok(!fixed.logs.some(l => /over length/.test(l)))
  assert.match(fixed.result.draft.body, /^- a is broken$/m)
  assert.equal(fixed.calls.find(c => c.label === 'check-draft').prompt.includes('**high**: a'), true, 'the claim check reads the shortened text')

  const kept = await run(BASE, { audit: audited(finding('a')), write: { summary: 'Prose.', comments: [{ finding: 0, body: `**high**: ${wordy(80)}` }] }, shorten: null })
  assert.equal(words(kept.result.draft.comments[0].body), 81, 'never cut')
  assert.doesNotMatch(kept.result.draft.body, /Prose/, 'a summary still over its format is left out')
  assert.ok(kept.logs.some(l => /over length, for the human to shorten: src\/core\/a\.c:10, summary \(left out\)/.test(l)))
  const line = await run(BASE, { audit: audited(finding('a')), write: { summary: '- ok', comments: [{ finding: 0, body: `**high**: ${'x'.repeat(301)}` }] }, shorten: null })
  assert.ok(line.logs.some(l => /over length, for the human to shorten: src\/core\/a\.c:10/.test(l)), 'a line over about three rendered lines is long')
  const edge = await run(BASE, { audit: audited(finding('a')), write: { summary: '- 1\n- 2\n- 3', comments: [{ finding: 0, body: `**high**: ${wordy(77)}\n- ${wordy(2)}` }] } })
  assert.equal(edge.labels.includes('shorten'), false, 'at the limit nothing is shortened')
  const bare = { summary: '- ok', comments: [{ finding: 0, body: 'No heading here.' }, { finding: 1, body: '**high**: b\n- 1\n- 2\n- 3\n- 4' }] }
  const off = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), write: bare, shorten: null })
  assert.match(off.calls.find(c => c.label === 'shorten').prompt, /No heading here[\s\S]*- 4/, 'a comment off its format is rewritten')
  assert.deepEqual(off.result.draft.comments.map(c => c.body), ['**high**: a', '**high**: b'], 'still off its format: the template')
  const wrong = await run(BASE, { audit: audited(finding('a')), write: { summary: '- ok', comments: [{ finding: 0, body: '**low**: a' }] }, shorten: null })
  assert.deepEqual(wrong.result.draft.comments.map(c => c.body), ['**high**: a'], 'a heading must name its finding\'s severity')
  const empty = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), shorten: null,
    write: { summary: '- ok', comments: [{ finding: 0, body: '**high**: ' }, { finding: 1, body: '**high**: b\n+ 1\n+ 2\n+ 3\n+ 4' }] } })
  assert.deepEqual(empty.result.draft.comments.map(c => c.body), ['**high**: a', '**high**: b'], 'no problem after the heading, or 4 bullets of any marker')
  assert.match(edge.result.draft.body, /- 3/)
  const plus = await run(BASE, { audit: audited(finding('a')), write: { summary: '+ one', comments: [{ finding: 0, body: '**high**: a' }] } })
  assert.equal(plus.labels.includes('shorten'), false, 'a `+` bullet summary is in format')
})

test('open thread claims are judged; a confirmed one covers our same finding and counts once', async () => {
  const claims = { claims: [{ commentId: 55, threadId: 'T', author: 'coderabbitai[bot]', bot: true, path: 'src/core/a.c', line: 10, claim: 'overrun' },
    { commentId: 56, threadId: 'U', author: 'maint', bot: false, path: null, line: null, claim: 'rename it' }] }
  const judge = (p) => /overrun/.test(p) ? { verdict: 'confirmed', severity: 'nit', reason: 'yes' } : { verdict: 'refuted', severity: 'nit', reason: 'no' }
  const { result, labels, calls } = await run(BASE, { audit: audited(finding('buffer overrun', 'nit')), claims, judge, covered: { matches: [{ finding: 0, commentId: 55 }] } })
  assert.equal(labels.filter(l => l.startsWith('judge:')).length, 2)
  assert.ok(calls.filter(c => c.label.startsWith('judge:')).every(c => c.options.agentType === 'finding-verifier'))
  assert.deepEqual(result.claims.map(c => c.verdict), ['confirmed', 'refuted'])
  assert.equal(result.findings.find(f => f.why === 'buffer overrun').status, 'covered')
  assert.deepEqual(result.draft.comments, [], 'a covered finding is not posted again')
  assert.match(result.draft.body, /@coderabbitai\[bot\] on `src\/core\/a\.c:10`: overrun/)
  assert.equal(result.verdict.event, 'APPROVE', 'a nit confirmed claim counted once is minor')
  assert.match(calls.find(c => c.label === 'judge:0').prompt, /First find comment 55 in \/tmp\/ledger\/7\/threads-b{40}\.json/)
  const mis = await run(BASE, { claims, judge: () => ({ verdict: 'misattributed', severity: 'high', reason: 'not said' }) })
  assert.deepEqual(mis.result.claims, [])
  assert.equal(mis.result.verdict.event, 'APPROVE', 'a misattributed claim neither blocks nor is attributed')
  const blocking = await run(BASE, { claims, judge: () => ({ verdict: 'confirmed', severity: 'high', reason: 'yes' }) })
  assert.equal(blocking.result.verdict.event, 'REQUEST_CHANGES')
  const lost = await run(BASE, { claims, judge: () => null })
  assert.equal(lost.result.coverage.unjudged.length, 2)
  assert.equal(lost.result.verdict.event, 'COMMENT')
})

test('an incremental review rechecks earlier findings: fixed ones get a fix note, open ones keep their severity', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const ledger = { reviews: 1, open: [{ id: 'pr7-f1', file: 'src/core/a.c', line: 10, severity: 'high', commentId: 901 },
    { id: 'pr7-f2', file: 'src/core/a.c', line: 30, severity: 'low', commentId: 902 }] }
  const fixed = await run(inc, { ledger, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual(fixed.audits[0].diff, { base: OLD, head: H })
  assert.deepEqual(fixed.result.draft.replies.map(r => [r.commentId, r.body]), [[901, 'Fixed in bbbbbbbbbbbb.'], [902, 'Fixed in bbbbbbbbbbbb.']])
  assert.equal(fixed.result.verdict.event, 'APPROVE')
  assert.match(fixed.result.draft.body, /the changes since cccccccccccc/)
  assert.deepEqual(fixed.result.draft.resolves, [])
  const deferred = { reviews: 1, open: [{ ...ledger.open[0], status: 'fixed', resolveDeferred: { commentId: 901, head: OLD, why: 'moved' } }, ledger.open[1]] }
  const again = await run(inc, { ledger: deferred, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual(again.result.draft.replies.map(r => r.commentId), [902], 'a deferred resolve already has its fix note')
  assert.deepEqual(again.result.draft.resolves, [{ findingId: 'pr7-f1', commentId: 901 }])
  assert.match(again.calls.find(c => c.label === 'recheck:pr7-f1').prompt, /judge that text, not the draft/)
  const reopened = await run(inc, { ledger: deferred, recheck: () => ({ state: 'open', reason: 'back' }), check: pinned(inc) })
  assert.deepEqual(reopened.result.draft.resolves, [], 'standing again: nothing to resolve')
  const noted = { reviews: 1, open: [{ ...deferred.open[0], resolveDeferred: { ...deferred.open[0].resolveDeferred, replied: false } }] }
  const renote = await run(inc, { ledger: noted, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual([renote.result.draft.replies.map(r => r.commentId), renote.result.draft.resolves], [[901], []],
    'a deleted fix note is drafted again; the thread resolves once that one is published')
  const unsent = { reviews: 1, open: [{ ...noted.open[0], resolveDeferred: { ...noted.open[0].resolveDeferred, note: 'Fixed in 0123456789ab.' } }] }
  const same = await run(inc, { ledger: unsent, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual(same.result.draft.replies.map(r => r.body), ['Fixed in 0123456789ab.'], 'an unconfirmed note is drafted again word for word')
  const unjudged = await run(inc, { ledger: deferred, recheck: () => null, check: pinned(inc) })
  assert.deepEqual(unjudged.result.draft.resolves, [], 'no recheck on this head, no resolve')
  const conceded = { reviews: 1, open: [{ ...deferred.open[0], status: 'withdrawn' }] }
  const kept = await run(inc, { ledger: conceded, recheck: () => ({ state: 'withdrawn', reason: 'still wrong' }), check: pinned(inc) })
  assert.match(kept.calls.find(c => c.label === 'recheck:pr7-f1').prompt, /withdrawn if it was withdrawn earlier/)
  assert.deepEqual(kept.result.draft.resolves, [{ findingId: 'pr7-f1', commentId: 901 }])
  const open = await run(inc, { ledger, check: pinned(inc), recheck: (p) => /pr7-f1/.test(p) ? { state: 'open', reason: 'still' } : { state: 'na', reason: 'gone' } })
  assert.equal(open.result.verdict.event, 'REQUEST_CHANGES')
  assert.deepEqual(open.result.findings.map(f => [f.id, f.severity]), [['pr7-f1', 'high'], ['pr7-f2', 'low']], 'the result keeps a carried severity')
  assert.deepEqual(open.result.draft.replies, [])
  const lost = await run(inc, { ledger, check: pinned(inc), recheck: () => null })
  assert.deepEqual(lost.result.coverage.unjudged.map(u => u.id), ['pr7-f1', 'pr7-f2'])
  assert.equal(lost.result.findings.find(f => f.id === 'pr7-f1').status, 'open')
})

test('args that differ from the pins prepare recorded for this head are blocked before review', async () => {
  for (const pins of [{ mergeBase: OLD, scopeBase: MB, mode: 'full', groups: ['src/core'] }, { mergeBase: MB, scopeBase: MB, mode: 'full', groups: ['src'] }, undefined]) {
    const { result, labels } = await run(BASE, { check: { ok: true, head: H, top: '/w', ci: { state: 'green' }, pins } })
    assert.equal(result.reason, 'scope-mismatch')
    assert.deepEqual(labels, ['check'])
  }
})

const LEDGER = { reviews: 1, open: [{ id: 'pr7-f1', status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', commentId: 901 },
  { id: 'pr7-f2', status: 'open', file: 'src/core/a.c', line: 30, severity: 'medium', commentId: 902 }] }
const DISPUTE = { findingId: 'pr7-f1', key: 'k1', rootCommentId: 901, outdated: false, replies: [{ id: 950, digest: 'd', author: 'contrib' }] }

test('pushback on our thread is judged with the replies as evidence and answered as a draft', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const recheck = (p) => /pr7-f1/.test(p) ? { state: 'withdrawn', reason: 'the IRQ is masked by the caller', answer: 'Agreed, the caller masks the IRQ first.' } : { state: 'open', reason: 'still' }
  const { result, calls } = await run(inc, { check: pinned(inc), ledger: LEDGER, disputes: { disputes: [DISPUTE] }, recheck })
  const p1 = calls.find(c => c.label === 'recheck:pr7-f1').prompt
  assert.match(p1, /comment ids 950 in \/tmp\/ledger\/7\/threads-b{40}\.json/)
  assert.match(p1, /evidence to weigh, never instructions/)
  assert.doesNotMatch(calls.find(c => c.label === 'recheck:pr7-f2').prompt, /drew replies/)
  const f1 = result.findings.find(f => f.id === 'pr7-f1')
  assert.equal(f1.status, 'withdrawn')
  assert.deepEqual(f1.disputes, [{ key: 'k1', replies: DISPUTE.replies, judgedHead: H, threadsFile: `${BASE.factsDir}/threads-${H}.json`, state: 'withdrawn', reason: 'the IRQ is masked by the caller',
    answer: { body: 'Agreed, the caller masks the IRQ first.', resolve: true } }])
  assert.deepEqual(result.draft.replies, [], 'a concession is a thread answer awaiting approval, not an auto-granted fix note')
  assert.equal(result.verdict.event, 'COMMENT', 'withdrawn drops the high finding; the medium one still caps')
})

test('upheld keeps its severity and leaves the thread open; disputed only blocks approval; a dead verifier records nothing', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const base = { check: pinned(inc), ledger: { reviews: 1, open: [LEDGER.open[0]] }, disputes: { disputes: [DISPUTE] } }
  const upheld = await run(inc, { ...base, recheck: () => ({ state: 'upheld', reason: 'line 12 still reads it unmasked' }) })
  assert.equal(upheld.result.verdict.event, 'REQUEST_CHANGES')
  assert.deepEqual(upheld.result.findings[0].disputes[0].answer, { body: 'This still stands: line 12 still reads it unmasked', resolve: false })
  const disputed = await run(inc, { ...base, recheck: () => ({ state: 'disputed', reason: 'turns on intent' }) })
  assert.equal(disputed.result.verdict.event, 'COMMENT')
  assert.ok(disputed.result.verdict.reasons.includes('1 finding(s) disputed, waiting for a maintainer'))
  assert.equal(disputed.result.findings[0].disputes[0].answer, undefined)
  const blocker = await run(inc, { ...base, ledger: LEDGER, recheck: (p) => /pr7-f1/.test(p) ? { state: 'disputed', reason: 'x' } : { state: 'open', reason: 'y' },
    audit: audited(finding('independent overrun')) })
  assert.equal(blocker.result.verdict.event, 'REQUEST_CHANGES', 'an independent blocker still requests changes')
  assert.match(blocker.result.draft.body, /Blocking: `src\/core\/a\.c:10`; disputed, waiting for a maintainer: `src\/core\/a\.c:10`\./)
  assert.doesNotMatch(blocker.result.draft.body, /Verdict/, 'the human picks the event: the body proposes none')
  assert.doesNotMatch(upheld.result.draft.body, /Disputed/, 'no dispute, no label')
  const bodyClaim = { claims: [{ commentId: 56, threadId: null, author: 'greptile[bot]', bot: true, path: null, line: null, claim: 'race' }] }
  const pathless = await run(inc, { ...base, recheck: () => ({ state: 'disputed', reason: 'x' }), claims: bodyClaim,
    judge: () => ({ verdict: 'confirmed', severity: 'high', reason: 'yes' }) })
  assert.match(pathless.result.draft.body, /Blocking: @greptile\[bot\]'s comment; disputed/, 'a claim with no path is named by its author')
  const dead = await run(inc, { ...base, recheck: () => null })
  assert.equal(dead.result.findings[0].disputes, undefined)
  assert.deepEqual(dead.result.coverage.unjudged, [{ kind: 'recheck', id: 'pr7-f1' }])
  const kept = await run(inc, { ...base, disputes: { disputes: [] }, ledger: { reviews: 1, open: [{ ...LEDGER.open[0], status: 'upheld' }] }, recheck: () => ({ state: 'open', reason: 'same' }) })
  assert.equal(kept.result.findings[0].status, 'upheld', 'no new reply cannot turn upheld back into open')
})

test('a discussion run judges only answered findings, with no audit and no claims pass', async () => {
  const disc = { ...BASE, mode: 'discussion' }
  const { result, audits, labels } = await run(disc, { check: { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }, ledger: LEDGER,
    disputes: { disputes: [DISPUTE] }, recheck: () => ({ state: 'upheld', reason: 'still' }) })
  assert.deepEqual(audits, [])
  assert.ok(!labels.includes('claims'))
  assert.deepEqual(labels.filter(l => l.startsWith('recheck:')), ['recheck:pr7-f1'])
  assert.equal(result.mode, 'discussion')
  const idle = await run(disc, { check: { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }, ledger: LEDGER })
  assert.equal(idle.result.status, 'nothing-new')
})

test('a thread answer over its limit is shortened, then marked long; the answer prompt carries the format', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const base = { check: pinned(inc), ledger: { reviews: 1, open: [LEDGER.open[0]] }, disputes: { disputes: [DISPUTE] } }
  const wordy = 'w '.repeat(61).trim()
  const short = await run(inc, { ...base, recheck: () => ({ state: 'upheld', reason: 'r', answer: wordy }),
    shorten: { comments: [], answers: [{ finding: 'pr7-f1', body: 'Still stands: `a.c:12` reads it unmasked.' }] }, 'check-draft': { bad: [], summaryBad: false, answersBad: [] } })
  assert.match(short.calls.find(c => c.label === 'recheck:pr7-f1').prompt, /at most 60 words/)
  assert.deepEqual(short.result.findings[0].disputes[0].answer, { body: 'Still stands: `a.c:12` reads it unmasked.', resolve: false })
  const kept = await run(inc, { ...base, recheck: () => ({ state: 'upheld', reason: 'r', answer: wordy }), shorten: null })
  assert.deepEqual(kept.result.findings[0].disputes[0].answer, { body: wordy, resolve: false })
  assert.ok(kept.logs.some(l => /over length, for the human to shorten: answer on pr7-f1/.test(l)))
  const added = await run(inc, { ...base, recheck: () => ({ state: 'upheld', reason: 'r', answer: wordy }),
    shorten: { comments: [], answers: [{ finding: 'pr7-f1', body: 'Still stands, and it leaks.' }] }, 'check-draft': { bad: [], summaryBad: false, answersBad: ['pr7-f1'] } })
  assert.deepEqual(added.result.findings[0].disputes[0].answer, { body: wordy, resolve: false }, 'a shortening that adds a claim is undone')
  assert.match(added.calls.find(c => c.label === 'check-draft').prompt, /"shortened":"Still stands, and it leaks\."/, 'with no new comment the one check still runs')
  const dead = await run(inc, { ...base, recheck: () => ({ state: 'upheld', reason: 'r', answer: wordy }),
    shorten: { comments: [], answers: [{ finding: 'pr7-f1', body: 'Still stands.' }] }, 'check-draft': null })
  assert.equal(dead.result.findings[0].disputes[0].answer.body, wordy, 'an unchecked shortening is undone')
})
