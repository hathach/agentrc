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
const IMP = { consequence: 'wrong data', path: 'every OUT transfer', variants: 'all', recovery: 'reset' }
// A finding as code-audit returns it: the verifier's level with the facts behind it.
const finding = (why, severity = 'high', line = 10, impact = IMP) => ({ file: 'src/core/a.c', line, snippet: 'x', why, severity, confidence: 'high',
  impact, severityReason: `graded: ${why}`, verdict: { real: true, reason: `holds: ${why}` } })
const confirmedAs = (severity, over = {}) => ({ verdict: 'confirmed', reason: 'yes', severity, impact: IMP, severityReason: 'graded', confidence: 'high', ...over })
const REFUTED = { verdict: 'refuted', reason: 'no', severity: null, impact: null, severityReason: null, confidence: null }

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
    if (options.label === 'group') return { groups: [] }
    if (options.label === 'beyond') return { beyond: [] }
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
// A list's [minItems, maxItems, id enum], as the runtime is asked to hold an answer to.
const sized = (schema, key, id) => { const l = schema.properties[key]; return [l.minItems, l.maxItems, l.items.properties[id].enum] }
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
  const at = (l) => calls.findIndex(c => c.label === l)
  assert.ok(at('threads') < at('ledger'), 'show reads the snapshot threads.py wrote')
  assert.match(calls[at('ledger')].prompt, /ledger\.py show .*--threads '\/tmp\/ledger\/7\/threads-b{40}\.json'/)
  assert.deepEqual(result.heldThreads, [])
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
  const variant = await run(BASE, { audit: audited(finding('EP0 reserve missed', 'high', 10, { ...IMP, variants: 'STM32F4 OTG_FS only' })) })
  assert.equal(variant.result.verdict.event, 'REQUEST_CHANGES', 'a high on one supported variant blocks like any high')
  const graded = variant.result.findings[0]
  assert.deepEqual([graded.confidence, graded.impact.variants, graded.severityReason], ['high', 'STM32F4 OTG_FS only', 'graded: EP0 reserve missed'], 'the grading travels with the finding')
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
  assert.ok(summary.logs.includes('summary left out: the check found a claim no finding states'))
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
  // Each list names exactly the ids asked, so the runtime has the agent correct a short answer; no ids, no enum.
  assert.deepEqual(sized(fixed.calls.find(c => c.label === 'write').options.schema, 'comments', 'finding'), [2, 2, [0, 1]])
  assert.deepEqual(sized(ask.options.schema, 'comments', 'finding'), [1, 1, [0]])
  assert.deepEqual(sized(ask.options.schema, 'answers', 'finding'), [0, 0, undefined])
  const prose = await run(BASE, { audit: audited(finding('a')), write: { summary: 'Prose, not bullets.', comments: [{ finding: 0, body: '**high**: a' }] }, shorten: null })
  const summaryOnly = prose.calls.find(c => c.label === 'shorten').options.schema
  assert.deepEqual([sized(summaryOnly, 'comments', 'finding'), sized(summaryOnly, 'answers', 'finding')], [[0, 0, undefined], [0, 0, undefined]])
  assert.doesNotMatch(ask.prompt, /"body":"\*\*high\*\*: b"/, 'only the text over its limit is sent')
  assert.deepEqual(fixed.result.draft.comments.map(c => c.body), ['**high**: a', '**high**: b'])
  assert.ok(!fixed.logs.some(l => /over length/.test(l)))
  assert.match(fixed.result.draft.body, /^- a is broken$/m)
  assert.equal(fixed.calls.find(c => c.label === 'check-draft').prompt.includes('**high**: a'), true, 'the claim check reads the shortened text')

  const kept = await run(BASE, { audit: audited(finding('a')), write: { summary: 'Prose.', comments: [{ finding: 0, body: `**high**: ${wordy(80)}` }] }, shorten: null })
  assert.equal(words(kept.result.draft.comments[0].body), 81, 'never cut')
  assert.doesNotMatch(kept.result.draft.body, /Prose/, 'a summary still over its format is left out')
  assert.ok(kept.logs.some(l => /over length, for the human to shorten: src\/core\/a\.c:10$/.test(l)))
  assert.ok(kept.logs.includes('summary left out: still over its format after shortening'))
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
  const judge = (p) => /overrun/.test(p) ? confirmedAs('nit') : REFUTED
  const { result, labels, calls } = await run(BASE, { audit: audited(finding('buffer overrun', 'nit')), claims, judge, group: { groups: [{ findings: [0], claims: ['55#1'] }] } })
  assert.equal(labels.filter(l => l.startsWith('judge:')).length, 2)
  assert.ok(calls.filter(c => c.label.startsWith('judge:')).every(c => c.options.agentType === 'finding-verifier'))
  assert.deepEqual(result.claims.map(c => c.verdict), ['confirmed', 'refuted'])
  assert.deepEqual(['status', 'defect'].map(k => result.findings.find(f => f.why === 'buffer overrun')[k]), ['open', 0], 'still ours to recheck')
  assert.deepEqual(result.claims.map(c => c.defect), [0, undefined])
  assert.deepEqual(result.draft.comments, [], 'a covered finding is not posted again')
  assert.match(result.draft.body, /@coderabbitai\[bot\] on `src\/core\/a\.c:10`: overrun/)
  assert.equal(result.verdict.event, 'APPROVE', 'a nit confirmed claim counted once is minor')
  assert.match(calls.find(c => c.label === 'judge:0').prompt, /First find comment 55 in \/tmp\/ledger\/7\/threads-b{40}\.json/)
  const mis = await run(BASE, { claims, judge: () => ({ verdict: 'misattributed', severity: 'high', reason: 'not said' }) })
  assert.deepEqual(mis.result.claims, [])
  assert.equal(mis.result.verdict.event, 'APPROVE', 'a misattributed claim neither blocks nor is attributed')
  const blocking = await run(BASE, { claims, judge: () => confirmedAs('high') })
  assert.equal(blocking.result.verdict.event, 'REQUEST_CHANGES')
  const lost = await run(BASE, { claims, judge: () => null })
  assert.equal(lost.result.coverage.unjudged.length, 2)
  assert.equal(lost.result.verdict.event, 'COMMENT')
})

test('one defect found by several dimensions is one comment and one blocker; each finding keeps its record', async () => {
  const two = audited(finding('overrun in the copy'), finding('the copy overruns', 'medium'))
  two.confirmed.push({ dir: 'src/core', dim: 'hardware: y', findings: [finding('other defect', 'high', 20)] })
  const { result, calls } = await run(BASE, { audit: two, group: { groups: [{ findings: [1, 0], claims: [] }] } })
  assert.match(calls.find(c => c.label === 'group').prompt, /whole substance is that defect/)
  assert.deepEqual(result.findings.map(f => [f.why, f.status, f.defect]),
    [['overrun in the copy', 'open', 0], ['the copy overruns', 'open', 0], ['other defect', 'open', undefined]], 'both keep their record under one defect')
  assert.deepEqual(result.draft.comments.map(c => c.finding), [0, 2])
  assert.deepEqual(result.verdict.reasons, ['2 blocking finding(s) open'])
  const apart = await run(BASE, { audit: two })
  assert.equal(apart.result.draft.comments.length, 3, 'without a group every finding is its own comment')
})

test('a thread claim of a group counts once at the strongest grade of all members, and the body names ours', async () => {
  const claims = { claims: [{ commentId: 55, author: 'coderabbitai[bot]', bot: true, path: 'src/core/a.c', line: 10, claim: 'overrun' },
    { commentId: 55, author: 'coderabbitai[bot]', bot: true, path: 'src/core/a.c', line: 12, claim: 'rename' },
    { commentId: 57, author: 'greptile-apps[bot]', bot: true, path: 'src/core/a.c', line: 10, claim: 'overrun too' }] }
  const judge = (p) => /rename/.test(p) ? confirmedAs('nit') : confirmedAs('low')
  const { result } = await run(BASE, { audit: audited(finding('buffer overrun', 'high'), finding('overrun', 'medium')), claims, judge,
    group: { groups: [{ findings: [1, 0], claims: ['55#1', '57#1'] }] } })
  assert.deepEqual(result.findings.map(f => [f.status, f.defect]), [['open', 0], ['open', 0]], 'the findings stay one defect if the thread goes')
  assert.deepEqual(result.claims.map(c => [c.claimId, c.defect]), [['55#1', 0], ['55#2', undefined], ['57#1', 0]])
  assert.deepEqual(result.verdict.reasons, ['1 blocking finding(s) open'], 'the low claims are the high finding: one blocker')
  assert.match(result.draft.body, /overrun \(our review grades it \*\*high\*\*, `src\/core\/a\.c:10`\)/)
  const tie = await run(BASE, { audit: audited(finding('buffer overrun', 'low')), claims, judge, group: { groups: [{ findings: [0], claims: ['55#1'] }] } })
  assert.doesNotMatch(tie.result.draft.body, /our review grades it/, 'only a stronger grade is named')
})

test('a muted finding stating an issue its group\'s text lacks posts too, counted once with its group', async () => {
  const pair = audited(finding('rf_tv zeroed'), finding('rf_tv zeroed, and the reference comment removed', 'nit'))
  const group = { groups: [{ findings: [0, 1], claims: [] }] }
  const written = { summary: '- ok', comments: [{ finding: 0, body: '**high**: rf_tv zeroed' }, { finding: 1, body: '**nit**: the reference comment removed' }] }
  const { result, calls, logs } = await run(BASE, { audit: pair, group, write: written, beyond: { beyond: [{ finding: 0, issue: 'the removed reference comment' }] } })
  assert.match(calls.find(c => c.label === 'beyond').prompt, /"printed":\[\{[^\]]*"why":"rf_tv zeroed"\}\],"muted":\[\{"finding":0,[^\]]*"verified":"holds: rf_tv zeroed, and the reference comment removed"/)
  assert.match(calls.find(c => c.label === 'write').prompt, /"alsoState":"the removed reference comment"/)
  assert.match(calls.find(c => c.label === 'check-draft').prompt, /"alsoState":"the removed reference comment"/)
  assert.deepEqual(result.draft.comments.map(c => c.body), ['**high**: rf_tv zeroed', '**nit**: the reference comment removed'])
  assert.deepEqual([result.verdict.reasons, result.findings.map(f => [f.defect, 'beside' in f])], [['1 blocking finding(s) open'], [[0, false], [0, false]]])
  assert.ok(logs.some(l => /beside its group, src\/core\/a\.c:10: the removed reference comment/.test(l)))
  const flagged = await run(BASE, { audit: pair, group, write: written, 'check-draft': { bad: [1], summaryBad: false }, beyond: { beyond: [{ finding: 0, issue: 'x' }] } })
  assert.equal(flagged.result.draft.comments[1].body, '**nit**: rf_tv zeroed, and the reference comment removed\n\nholds: rf_tv zeroed, and the reference comment removed', 'the fallback carries its verification, never the issue named')
  const claims = { claims: [{ commentId: 55, author: 'a[bot]', bot: true, path: 'src/core/a.c', line: 10, claim: 'change latches lost' }] }
  const narrow = await run(BASE, { audit: audited(finding('change latches and a SETUP lost', 'medium')), claims, judge: () => confirmedAs('medium'),
    group: { groups: [{ findings: [0], claims: ['55#1'] }] }, beyond: { beyond: [{ finding: 0, issue: 'the lost SETUP' }] } })
  assert.match(narrow.calls.find(c => c.label === 'beyond').prompt, /"printed":\["change latches lost"\]/)
  assert.deepEqual([narrow.result.draft.comments.length, narrow.result.findings[0].defect], [1, 0], 'a claim stating less no longer hides our finding')
  assert.match(narrow.result.draft.comments[0].body, /SETUP/)
  const none = await run(BASE, { audit: pair })
  assert.ok(!none.labels.includes('beyond'), 'nothing muted, nothing to ask')
})

test('a dead or malformed answer on muted findings posts them all and leaves coverage unproven', async () => {
  const nits = audited(finding('a', 'nit'), finding('a too', 'nit'))
  const group = { groups: [{ findings: [0, 1], claims: [] }] }
  const clean = await run(BASE, { audit: nits, group })
  assert.deepEqual([clean.result.draft.comments.length, clean.result.verdict.event], [1, 'APPROVE'])
  for (const beyond of [null, { beyond: 'x' }, { beyond: [{ finding: 9, issue: 'x' }] }, { beyond: [null] }, { beyond: [{ finding: 0, issue: ' ' }] },
    { beyond: [{ finding: 0 }] }, { beyond: [{ finding: 0, issue: 'x' }, { finding: 0, issue: 'y' }] }]) {
    const { result } = await run(BASE, { audit: nits, group, beyond })
    assert.deepEqual([result.draft.comments.length, result.coverage.unjudged, result.verdict.event, result.findings.map(f => f.defect)],
      [2, [{ kind: 'beyond' }], 'COMMENT', [0, 0]], JSON.stringify(beyond))
  }
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const row = (id, severity, commentId) => ({ id, status: 'open', file: 'src/core/a.c', line: 10, severity, confidence: 'high', impact: IMP, severityReason: 'g', commentId, why: `w ${id}` })
  const standing = await run(inc, { ledger: { reviews: 1, open: [row('pr7-f1', 'high', 901), row('pr7-f2', 'low', 902)] }, check: pinned(inc),
    recheck: (p) => ({ state: 'open', reason: /pr7-f2/.test(p) ? 'still, and the reference comment is gone' : 'still' }), group, beyond: { beyond: [{ finding: 0, issue: 'more' }] } })
  assert.match(standing.calls.find(c => c.label === 'beyond').prompt, /"verified":"still, and the reference comment is gone"/)
  assert.match(standing.result.draft.body, /not stated in full by any comment:\n- \*\*low\*\* `src\/core\/a\.c:10`: w pr7-f2\n  still, and the reference comment is gone$/m, 'its own comment is not enough once its group speaks for it')
  assert.doesNotMatch(standing.result.draft.body, /more/, 'the issue named is never printed')
  const lead = await run(inc, { ledger: { reviews: 1, open: [row('pr7-f1', 'high', null), row('pr7-f2', 'low', 902)] }, check: pinned(inc),
    recheck: { state: 'open', reason: 'still' }, group })
  assert.match(lead.result.draft.body, /not stated in full by any comment:\n- \*\*high\*\* `src\/core\/a\.c:10`: w pr7-f1$/m, 'a group mate\'s comment never states the lead')
  assert.doesNotMatch(lead.result.draft.body, /w pr7-f2/, 'its own comment states the muted member')
})

test('a grouping that names an item twice, nothing, one member or two files is distrusted whole; a dead one is unjudged', async () => {
  const two = audited(finding('a'), finding('b'), { ...finding('c'), file: 'src/core/b.c' })
  for (const groups of [[{ findings: [0, 1], claims: [] }, { findings: [1, 0], claims: [] }], [{ findings: [0, 5], claims: [] }],
    [{ findings: [0], claims: [] }], [{ findings: [0, 1], claims: ['9#1'] }], [{ findings: [0, 2], claims: [] }]]) {
    const { result } = await run(BASE, { audit: two, group: { groups } })
    assert.equal(result.draft.comments.length, 3, JSON.stringify(groups))
    assert.deepEqual(result.coverage.unjudged, [{ kind: 'group' }])
  }
  const dead = await run(BASE, { audit: two, group: null })
  assert.deepEqual([dead.result.draft.comments.length, dead.result.verdict.event], [3, 'REQUEST_CHANGES'])
  const odd = await run(BASE, { audit: audited({ ...finding('a'), file: 'constructor' }, { ...finding('b'), file: 'constructor' }),
    group: { groups: [{ findings: [0, 1], claims: ['toString'] }] } })
  assert.deepEqual([odd.labels.includes('group'), odd.result.coverage.unjudged], [true, [{ kind: 'group' }]], 'a file or claim id named like an Object property')
  const alone = await run(BASE, { audit: audited(finding('a'), { ...finding('b'), file: 'src/core/b.c' }) })
  assert.ok(!alone.labels.includes('group'), 'nothing shares a file: no grouping asked')
})

test('a claims-only group needs a path, and a claim that covered a finding counts with it only while it is confirmed again', async () => {
  const claims = { claims: [{ commentId: 55, author: 'a[bot]', bot: true, path: null, line: null, claim: 'x' },
    { commentId: 56, author: 'b[bot]', bot: true, path: null, line: null, claim: 'x too' }] }
  const both = await run(BASE, { audit: audited(finding('a'), finding('b')), claims, judge: () => confirmedAs('high'), group: { groups: [{ findings: [], claims: ['55#1', '56#1'] }] } })
  assert.deepEqual(both.result.coverage.unjudged, [{ kind: 'group' }], 'no shared path: nothing proves one defect')
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const row = { id: 'pr7-f1', status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', confidence: 'high', impact: IMP, severityReason: 'g', commentId: null, defect: 0 }
  const thread = { claims: [{ commentId: 55, author: 'a[bot]', bot: true, path: 'src/core/a.c', line: 10, claim: 'overrun' }] }
  const standingRow = { ...row, why: 'overrun past the buffer end' }
  const again = await run(inc, { ledger: { reviews: 1, open: [standingRow] }, check: pinned(inc), recheck: { state: 'open', reason: 'still' }, claims: thread,
    judge: () => confirmedAs('low'), group: { groups: [{ findings: [0], claims: ['55#1'] }] } })
  assert.match(again.calls.find(c => c.label === 'group').prompt, /overrun past the buffer end/, 'standing findings are regrouped every run, whole')
  assert.deepEqual(again.result.verdict.reasons, ['1 blocking finding(s) open'], 'the high finding and its low claim are one defect, counted high')
  assert.match(again.result.draft.body, /our review grades it \*\*high\*\*/)
  assert.doesNotMatch(again.result.draft.body, /Still standing/, 'the thread states it')
  const resolved = await run(inc, { ledger: { reviews: 1, open: [standingRow] }, check: pinned(inc), recheck: { state: 'open', reason: 'still' } })
  assert.equal(resolved.result.verdict.event, 'REQUEST_CHANGES', 'its thread gone, the finding still stands on its own')
  assert.match(resolved.result.draft.body, /Still standing from earlier reviews, not stated in full by any comment:\n- \*\*high\*\* `src\/core\/a\.c:10`: overrun past the buffer end/)
  const refound = await run(inc, { ledger: { reviews: 1, open: [{ ...standingRow, commentId: 900 }] }, check: pinned(inc), recheck: { state: 'open', reason: 'still' },
    audit: audited(finding('overrun again', 'medium')), group: { groups: [{ findings: [0, 1], claims: [] }] } })
  assert.deepEqual(refound.result.findings.map(f => [f.id, f.defect]), [['pr7-f1', 0], [undefined, 0]], 'a new copy of a standing finding joins its defect')
  assert.deepEqual([refound.result.draft.comments, refound.result.verdict.reasons], [[], ['1 blocking finding(s) open']])
})

test('findings linked as one defect on an earlier review are regrouped, and count once only while still one', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const row = (id, commentId) => ({ id, status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', confidence: 'high', impact: IMP, severityReason: 'g', commentId, why: `w ${id}`, defect: 0 })
  const ledger = { reviews: 1, open: [row('pr7-f1', 901), row('pr7-f2', null)] }
  const still = { state: 'open', reason: 'still' }
  const both = await run(inc, { ledger, check: pinned(inc), recheck: still, group: { groups: [{ findings: [0, 1], claims: [] }] } })
  assert.match(both.calls.find(c => c.label === 'group').prompt, /w pr7-f2/, 'standing findings are regrouped: a push can split them')
  assert.deepEqual([both.result.verdict.reasons, both.result.findings.map(f => f.defect)], [['1 blocking finding(s) open'], [0, 0]])
  assert.doesNotMatch(both.result.draft.body, /Still standing/, 'the comment on pr7-f1 states the defect')
  const apart = await run(inc, { ledger, check: pinned(inc), recheck: still })
  assert.deepEqual([apart.result.verdict.reasons, apart.result.findings.map(f => f.defect)], [['2 blocking finding(s) open'], [null, null]])
  assert.match(apart.result.draft.body, /Still standing from earlier reviews, not stated in full by any comment:\n- \*\*high\*\* `src\/core\/a\.c:10`: w pr7-f2$/m, 'split, the one never posted is named')
  const dead = await run(inc, { ledger, check: pinned(inc), recheck: still, group: null })
  assert.deepEqual([dead.result.verdict.reasons.slice(0, 1), dead.result.findings.map(f => f.defect)], [['2 blocking finding(s) open'], [null, null]],
    'a failed grouping never falls back to an earlier one')
  assert.match(dead.result.draft.body, /: w pr7-f2$/m)
  const fixed = await run(inc, { ledger, check: pinned(inc), recheck: (p) => /pr7-f1/.test(p) ? { state: 'fixed', reason: 'gone' } : { state: 'open', reason: 'still' } })
  assert.deepEqual(fixed.result.findings.map(f => [f.status, f.defect]), [['fixed', null], ['open', null]], 'the linked one is rechecked on its own')
  assert.deepEqual(fixed.result.verdict.reasons, ['1 blocking finding(s) open'])
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
  const deferred = { reviews: 1, open: [{ ...ledger.open[0], status: 'fixed', resolveDue: { replied: true } }, ledger.open[1]] }
  const again = await run(inc, { ledger: deferred, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual(again.result.draft.replies.map(r => r.commentId), [902], 'a due resolve with our reply on the thread needs no new note')
  assert.deepEqual(again.result.draft.resolves, [{ findingId: 'pr7-f1', commentId: 901 }])
  assert.match(again.calls.find(c => c.label === 'recheck:pr7-f1').prompt, /judge that text, not the draft/)
  const reopened = await run(inc, { ledger: deferred, recheck: () => ({ state: 'open', reason: 'back' }), check: pinned(inc) })
  assert.deepEqual(reopened.result.draft.resolves, [], 'standing again: nothing to resolve')
  const noted = { reviews: 1, open: [{ ...deferred.open[0], resolveDue: { replied: false } }] }
  const renote = await run(inc, { ledger: noted, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual([renote.result.draft.replies.map(r => r.commentId), renote.result.draft.resolves], [[901], []],
    'with no reply of ours there, the note is drafted; the thread resolves once it is published')
  const held = [{ findingId: 'pr7-f3', commentId: 903, why: 'resolve unconfirmed' }]
  assert.deepEqual((await run(inc, { ledger: { ...deferred, heldThreads: held }, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })).result.heldThreads, held)
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
    judge: () => confirmedAs('high') })
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
  const both = { disputes: [DISPUTE, { ...DISPUTE, findingId: 'pr7-f2', key: 'k2', rootCommentId: 902 }] }
  const two = await run(disc, { check: { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }, ledger: LEDGER, disputes: both,
    recheck: () => ({ state: 'upheld', reason: 'still' }), group: { groups: [{ findings: [0, 1], claims: [] }] } })
  assert.deepEqual([two.labels.includes('group'), two.result.findings.map(f => 'defect' in f)], [false, [false, false]], 'a discussion leaves the saved groups alone')
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
  assert.deepEqual(sized(short.calls.find(c => c.label === 'shorten').options.schema, 'answers', 'finding'), [1, 1, ['pr7-f1']])
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

test('a reviewer\'s label never sets our level, and a confirmed claim without its grading is unjudged, never a blocker', async () => {
  const claims = { claims: [{ commentId: 55, threadId: 'T', author: 'coderabbitai[bot]', bot: true, path: 'src/core/a.c', line: 10, claim: 'Critical: overrun' }] }
  const low = await run(BASE, { claims, judge: () => confirmedAs('low') })
  assert.equal(low.result.claims[0].severity, 'low')
  assert.equal(low.result.verdict.event, 'COMMENT')
  assert.match(low.calls.find(c => c.label === 'judge:0').prompt, /Severity section of your role.*never your level/)
  for (const k of ['severity', 'impact', 'severityReason', 'confidence']) assert.ok(low.calls.find(c => c.label === 'judge:0').options.schema.required.includes(k), k)
  const ungraded = await run(BASE, { claims, judge: () => confirmedAs('high', { impact: null }) })
  assert.deepEqual(ungraded.result.claims, [])
  assert.deepEqual(ungraded.result.coverage.unjudged, [{ kind: 'claim', commentId: 55 }])
  assert.equal(ungraded.result.verdict.event, 'COMMENT', 'lost coverage, not a blocker')
})

test('a recheck regrades only with the facts behind it; without them the old level stands and coverage is lost', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const base = { check: pinned(inc), ledger: { reviews: 1, open: [{ ...LEDGER.open[0], confidence: 'high', impact: IMP, severityReason: 'old' }] } }
  const facts = { impact: { ...IMP, recovery: 'a retry' }, severityReason: 'the host retries', confidence: 'medium' }
  const down = await run(inc, { ...base, recheck: () => ({ state: 'open', reason: 'still', severity: 'medium', ...facts }) })
  const f = down.result.findings[0]
  assert.deepEqual([f.id, f.severity, f.impact.recovery, f.severityReason, f.confidence], ['pr7-f1', 'medium', 'a retry', 'the host retries', 'medium'])
  assert.equal(down.result.verdict.event, 'COMMENT', 'the regraded level decides the verdict')
  assert.match(down.calls.find(c => c.label === 'recheck:pr7-f1').prompt, /Its level is high\. Only when new evidence changes its facts/)
  const bare = await run(inc, { ...base, recheck: () => ({ state: 'open', reason: 'still', severity: 'medium' }) })
  assert.equal(bare.result.findings[0].severity, 'high')
  assert.equal(bare.result.findings[0].severityReason, 'old', 'the old grading is kept with its level')
  assert.deepEqual(bare.result.coverage.unjudged, [{ kind: 'regrade', id: 'pr7-f1' }])
  assert.equal(bare.result.verdict.event, 'REQUEST_CHANGES', 'the standing high still blocks')
  const same = await run(inc, { ...base, recheck: () => ({ state: 'open', reason: 'still' }) })
  assert.deepEqual([same.result.findings[0].severity, same.result.coverage.unjudged], ['high', []])
  const revised = await run(inc, { ...base, recheck: () => ({ state: 'open', reason: 'still', severity: 'high', ...facts }) })
  assert.deepEqual([revised.result.findings[0].severity, revised.result.findings[0].severityReason, revised.result.coverage.unjudged], ['high', 'the host retries', []],
    'revised facts at the same level replace the old ones')
  const partial = await run(inc, { ...base, recheck: () => ({ state: 'open', reason: 'still', impact: facts.impact }) })
  assert.deepEqual([partial.result.findings[0].severityReason, partial.result.coverage.unjudged], ['old', [{ kind: 'regrade', id: 'pr7-f1' }]],
    'facts without their level are an incomplete regrade')
})
