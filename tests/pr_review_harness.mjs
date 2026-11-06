import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = readFileSync(new URL('../workflows/pr-review.js', import.meta.url), 'utf8')
  .replace(/^export const meta = /m, 'const meta = globalThis.__meta = ')
const ABSENT = ['URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'Buffer', 'process', 'fetch', 'structuredClone']

const H = 'b'.repeat(40), MB = 'a'.repeat(40), OLD = 'c'.repeat(40)
const wordy = (n) => 'w '.repeat(n).trim()
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

// ledger.py show --snapshot's manifest over `answer` as the snapshot's bytes, and the real state_transfer.py --raw's
// line for the file a manifest named.
const STATE_TRANSFER = fileURLToPath(new URL('../skills/pr-babysit/scripts/state_transfer.py', import.meta.url))
const snapshots = new Map()
const asciiJson = (v) => JSON.stringify(v).replace(/[\u007f-￿]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
function fnv1a (text) {
  let h = 0x811c9dc5
  for (const ch of text) h = Math.imul(h ^ ch.codePointAt(0), 0x01000193) >>> 0
  return h.toString(16).padStart(8, '0')
}
function manifestOf (answer, a) {
  const payload = asciiJson(answer)
  const file = `${a.factsDir.replace(/\\/g, '/')}/ledger-${createHash('sha256').update(payload).digest('hex')}.json`
  snapshots.set(file, payload)
  return { repo: a.repo, pr: a.pr, head: a.head, file, digest: fnv1a(payload), length: payload.length }
}
function transferOf (prompt) {
  const [, file] = /state_transfer\.py '([^']+)' --raw > \/dev\/null`/.exec(prompt)
  const dir = mkdtempSync(join(tmpdir(), 'pr-review-ledger-'))
  try {
    const at = join(dir, 'ledger.json')
    writeFileSync(at, snapshots.get(file))
    const done = spawnSync('python3', [STATE_TRANSFER, at, '--raw'], { encoding: 'utf8' })
    assert.equal(done.status, 0, done.stdout + done.stderr)
    return JSON.parse(done.stdout.trim().split('\n').at(-1))
  } finally { rmSync(dir, { recursive: true, force: true }) }
}
// The file a relay command hands its script as AGENTRC_EMIT, and the module lib/seal.py's emit writes there.
const emitOf = (prompt) => String(prompt).match(/`AGENTRC_EMIT='([^']+)' /)?.[1]
const moduleOf = (file, line) => `export const meta = { name: 'emit', description: 'a script line' }\nreturn ${JSON.stringify({ emit: file, line })}\n`

// Stub agents keyed by label; `stubs.<label>` is the value, or a function of the prompt. A relay's stub is its script's
// line, which lands in the file its command names as lib/seal.py's emit writes it, claimed before the script runs (one
// there is exit 3, nothing run), null a relay that died before it wrote; the relay answers its exit. `opts.resume` is an earlier run's journal, answered
// in order as the runtime's resume answers the unchanged prefix; `opts.emitDir` is facts.py emit-dir's directory,
// else a new one, removed unless `opts.keepEmit`.
async function run(args, stubs = {}, opts = {}) {
  const calls = []
  const logs = []
  const journal = []
  const loads = []
  let live = 0
  let emitMade = null
  const emitDir = () => {
    if (opts.emitDir) return opts.emitDir
    if (!emitMade) {
      emitMade = `${mkdtempSync(join(tmpdir(), 'pr-review-emit-')).replace(/\\/g, '/')}/agentrc-emit/r1`
      mkdirSync(emitMade, { recursive: true })
    }
    return emitMade
  }
  const pick = (label, prompt) => {
    const key = Object.keys(stubs).find(k => k === label) ?? Object.keys(stubs).find(k => label.startsWith(k + ':'))
    const v = key === undefined ? undefined : stubs[key]
    return typeof v === 'function' ? v(prompt, label) : v
  }
  const defaults = {
    check: { ok: true, head: H, top: '/w', repo: args.repo, pr: args.pr, ci: { state: 'green', counts: {} }, pins: { mergeBase: args.mergeBase, scopeBase: args.scopeBase, mode: args.mode, groups: args.groups, recheckFrom: null } },
    threads: { file: `${BASE.factsDir}/threads-${H}.json`, count: 0 },
    ledger: { reviews: 0, open: [] },
    claims: { claims: [] },
  }
  const stub = async (prompt, options) => {
    calls.push({ label: options.label, prompt: String(prompt), options })
    if (options.label === 'emit:dir') return { dir: emitDir() }
    // A relay's fresh attempt (`<label>:retry`) runs the same script.
    const base = options.label.replace(/:retry$/, '')
    // `stubs['ledger:load']` replaces state_transfer.py --raw's line, or is a function of it.
    if (base === 'ledger:load') {
      const v = stubs['ledger:load']
      return v === undefined ? transferOf(prompt) : typeof v === 'function' ? v(transferOf(prompt)) : v
    }
    const v = pick(options.label, prompt)
    // `stubs.ledger` is the answer ledger.py show --snapshot writes; `stubs.disputes` its disputes, unless that answer
    // names its own; the members ledger.py always prints default as it prints them. `stubs.ledgerAsIs` is written
    // untouched, and `stubs['ledger:manifest']` replaces the manifest.
    if (base === 'ledger:manifest') {
      if (stubs['ledger:manifest'] !== undefined) return v
      const l = stubs.ledgerAsIs !== undefined ? stubs.ledgerAsIs : v === undefined ? defaults.ledger : v
      if (!l || typeof l !== 'object') return l
      const rows = Array.isArray(l.open) ? { open: l.open.map(f => f && typeof f === 'object' ? { published: null, repair: null, ...f } : f) } : {}
      const answer = stubs.ledgerAsIs !== undefined ? l
        : { ...(l.reviews > 0 ? { heldThreads: [] } : {}), ...l, ...rows, ...('disputes' in l ? {} : { disputes: stubs.disputes?.disputes ?? [] }) }
      return manifestOf(answer, args)
    }
    if (v !== undefined) return v
    if (base in defaults) return defaults[base]
    if (options.label === 'write') {
      const fs = JSON.parse(/Findings: (\[.*\])$/s.exec(prompt)[1])
      return { summary: '- Overall fine.', comments: fs.map(f => ({ finding: f.finding, body: `**${f.severity}**: ${f.why}` })) }
    }
    if (options.label === 'check-draft') return { bad: [], firstBad: [], summaryBad: false }
    if (options.label === 'group') return { groups: [] }
    if (options.label === 'beyond') return { beyond: [] }
    throw new Error(`unstubbed ${options.label}`)
  }
  const agent = async (prompt, options) => {
    const at = journal.length
    const cached = opts.resume && live === at && opts.resume[at]
    if (cached && cached.prompt === String(prompt) && cached.label === options.label) {
      live++
      journal.push(cached)
      return structuredClone(cached.answer)
    }
    const entry = { label: options.label, prompt: String(prompt) }
    journal.push(entry)
    const file = emitOf(prompt)
    let answer = null
    try {
      if (file === undefined) answer = await stub(prompt, options)
      else if (existsSync(file)) answer = { exit: 3 }
      else {
        writeFileSync(file, '') // the claim, before the script runs anything
        const line = await stub(prompt, options)
        if (line !== null) {
          writeFileSync(file, moduleOf(file, line))
          answer = { exit: line && line.error ? 2 : 0 }
        }
      }
    } finally { entry.answer = structuredClone(answer) }
    return answer
  }
  const audits = []
  // workflow({scriptPath}) as the runtime runs it: the file's body, its return value the result; an unreadable file
  // throws. Else the one named child, code-audit.
  const workflow = async (name, a) => {
    if (name && typeof name.scriptPath === 'string') {
      const text = readFileSync(name.scriptPath, 'utf8').replace(/^export const meta = /m, 'const meta = ')
      loads.push(name.scriptPath)
      const nested = () => { throw new Error('a child workflow nests nothing') }
      return new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget', ...ABSENT, text)(
        a, nested, null, null, () => {}, () => {}, nested, null, ...ABSENT.map(() => undefined))
    }
    assert.equal(name, 'code-audit')
    audits.push(a)
    const v = stubs.audit
    return v === undefined ? { confirmed: [], dropped: [], unverified: [] } : (typeof v === 'function' ? v(a) : v)
  }
  const parallel = thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)))
  // Every value the workflow parses, so a test can see the ledger answer it rebuilt.
  const parsed = []
  const json = { stringify: JSON.stringify, parse: (t, r) => { const v = JSON.parse(t, r); parsed.push(v); return v } }
  const fn = new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget', 'JSON', ...ABSENT, body)
  try {
    const result = await fn(args, agent, null, parallel, () => {}, m => logs.push(String(m)), workflow, null, json, ...ABSENT.map(() => undefined))
    return { result, calls, logs, audits, parsed, journal, loads, emitDir: emitMade, labels: calls.map(c => c.label).filter(l => l !== 'emit:dir') }
  } finally {
    if (emitMade && !opts.keepEmit) rmSync(emitMade.split('/agentrc-emit/')[0], { recursive: true, force: true })
  }
}

const pinned = (a) => ({ ok: true, head: H, top: '/w', repo: a.repo, pr: a.pr, ci: { state: 'green' }, pins: { mergeBase: a.mergeBase, scopeBase: a.scopeBase, mode: a.mode, groups: a.groups, recheckFrom: a.recheckFrom ?? null } })
const board = (verdict, regression, testedHead = H) => ({ board: 'b1', verdict, regression, testedHead, report: '/r/b1.json' })
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

test('a Windows factsDir and checkout top are read with slashes; a POSIX path stays exactly as given', async () => {
  const file = `C:/Users/me/ledger/7/threads-${H}.json`
  for (const factsDir of ['C:\\Users\\me\\ledger\\7', 'C:/Users/me/ledger/7']) {
    const { result, calls, audits } = await run({ ...BASE, factsDir }, { threads: { file, count: 0 }, check: { ...pinned(BASE), top: 'E:/w' } })
    assert.equal(result.verdict.event, 'APPROVE', factsDir)
    assert.ok(calls.find(c => c.label === 'threads').prompt.includes(`--out '${file}'`), factsDir)
    assert.equal(audits[0].root, 'E:/w')
  }
  for (const factsDir of ['/tmp/a\\b', 'C:rel', 'C:/x/../y', 'C:\\x\\..\\y', 'C:/x\'y']) {
    await assert.rejects(run({ ...BASE, factsDir }), /factsDir must be an absolute path/, factsDir)
  }
})

// The real code-audit body, its scanner reporting `findings` for every unit and its verifier confirming each.
const AUDIT = readFileSync(new URL('../workflows/code-audit.js', import.meta.url), 'utf8').replace(/^export const meta = /m, 'const meta = ')
const codeAudit = (a, findings) => {
  const agent = async (prompt, o) => o.label.startsWith('scan:') ? { findings }
    : { real: true, reason: 'holds', severity: 'high', impact: IMP, severityReason: 'graded', confidence: 'high' }
  const pipeline = (items, ...stages) => Promise.all(items.map(async (item, i) => {
    let v = item
    for (const s of stages) v = await s(v, item, i)
    return v
  }))
  const parallel = thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)))
  return new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget', ...ABSENT, AUDIT)(
    a, agent, pipeline, parallel, () => {}, () => {}, null, null, ...ABSENT.map(() => undefined))
}

test('a backslashed checkout top reaches code-audit as check gave it, and code-audit places a finding under it', async () => {
  const top = 'E:\\w\\top'
  const scanned = { file: 'e:\\W\\Top\\src\\core\\a.c', line: 10, snippet: 'x', why: 'buffer overrun', severity: 'high', confidence: 'high', hardware: false }
  const { result, audits } = await run(BASE, { check: { ...pinned(BASE), top }, audit: a => codeAudit(a, [scanned]) })
  assert.equal(audits[0].root, top)
  assert.ok(result.draft.comments.length, JSON.stringify(result.verdict))
  assert.deepEqual([...new Set(result.draft.comments.map(c => c.path))], ['src/core/a.c'])
})

test('a failed, moved-head or unidentified check stops before any review work', async () => {
  const { repo, pr, ...unnamed } = pinned(BASE)
  for (const check of [null, { error: 'head moved: expected b' }, { ok: true, head: OLD, ci: { state: 'green' } }, unnamed, { ...pinned(BASE), repo: 'o/other' }, { ...pinned(BASE), pr: 8 }]) {
    const { result, labels } = await run(BASE, { check })
    assert.equal(result.status, 'blocked')
    assert.equal(result.reason, 'check-failed')
    assert.deepEqual(labels, check === null ? ['check', 'check:retry'] : ['check'], 'a dead relay gets one fresh attempt')
  }
})

test('a clean full review approves only with green CI, full coverage and hardware covered', async () => {
  const { result, audits, calls } = await run(BASE)
  assert.equal(result.verdict.event, 'APPROVE')
  assert.deepEqual(audits[0], { dirs: ['src/core'], dimensions: audits[0].dimensions, diff: { base: MB, head: H }, root: '/w' })
  assert.equal(audits[0].dimensions.length, 4)
  assert.ok(calls.filter(c => ['emit:dir', 'check', 'threads', 'ledger:manifest', 'ledger:load'].includes(c.label)).every(c => c.options.model === 'haiku'))
  assert.match(calls.find(c => c.label === 'check').prompt, /prepare\.py --check --pr 7 --repo o\/r --expected-head b{40}/)
  const at = (l) => calls.findIndex(c => c.label === l)
  assert.ok(at('threads') < at('ledger:manifest'), 'show reads the snapshot threads.py wrote')
  assert.match(calls[at('ledger:manifest')].prompt, /ledger\.py show .*--threads '\/tmp\/ledger\/7\/threads-b{40}\.json' --head b{40} --snapshot '\/tmp\/ledger\/7'` /)
  assert.match(calls[at('ledger:load')].prompt, /`AGENTRC_EMIT='[^']+\/ledger_load\.1\.[0-9a-f]{8}\.js' python3 .*\/skills\/pr-babysit\/scripts\/state_transfer\.py '\/tmp\/ledger\/7\/ledger-[0-9a-f]{64}\.json' --raw > \/dev\/null` /)
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
  const abs = await run(BASE, { audit: audited(finding('overrun')) })
  assert.equal(abs.audits[0].root, '/w', 'code-audit is given the checkout top, to make a scanner\'s absolute path repository-relative')
  const variant = await run(BASE, { audit: audited(finding('EP0 reserve missed', 'high', 10, { ...IMP, variants: 'STM32F4 OTG_FS only' })) })
  assert.equal(variant.result.verdict.event, 'REQUEST_CHANGES', 'a high on one supported variant blocks like any high')
  const graded = variant.result.findings[0]
  assert.deepEqual([graded.confidence, graded.impact.variants, graded.severityReason], ['high', 'STM32F4 OTG_FS only', 'graded: EP0 reserve missed'], 'the grading travels with the finding')
  const reg = await run({ ...BASE, hil: { choice: 'boards', boards: [board('fail', 'verified')] } })
  assert.equal(reg.result.verdict.event, 'REQUEST_CHANGES')
  assert.ok(reg.result.verdict.reasons.includes('verified HIL regression on b1'))
})

test('a drafted comment that adds a claim, or is lost, falls back to the finding\'s own words', async () => {
  const flagged = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), 'check-draft': { bad: [1], firstBad: [] } })
  assert.deepEqual(flagged.result.draft.comments.map(c => c.body), ['**high**: a', '**high**: b'])
  assert.match(flagged.result.draft.body, /Overall fine/, 'a clean summary stays when one comment is flagged')
  const summary = await run(BASE, { audit: audited(finding('a')), 'check-draft': { bad: [], firstBad: [], summaryBad: true } })
  assert.doesNotMatch(summary.result.draft.body, /Overall fine/, 'a summary that adds a claim is dropped')
  assert.ok(summary.logs.includes('summary left out: the check found a claim no finding states'))
  const dead = await run(BASE, { audit: audited(finding('a')), write: null })
  assert.deepEqual(dead.result.draft.comments.map(c => c.body), ['**high**: a'])
  const first = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), 'check-draft': { bad: [], firstBad: [0], summaryBad: false },
    write: { summary: '', comments: [{ finding: 0, body: '**high**: a, worded' }, { finding: 1, body: '**high**: b' }] } })
  assert.deepEqual(first.result.draft.comments.map(c => [c.body, !!c.firstLineUnchecked]), [['**high**: a, worded', true], ['**high**: b', false]],
    'a first line the check flags keeps its text and holds auto-post')
})

test('text of any length is drafted as written: nothing measures, shortens or holds it', async () => {
  const long = `**high**: ${wordy(200)}\n- ${'x'.repeat(700)}\n- 2\n- 3\n- 4`
  const summary = `- ${wordy(60)}`
  const r = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), write: { summary, comments: [{ finding: 0, body: long }, { finding: 1, body: '**high**: b' }] } })
  // Each list names exactly the ids asked, so the runtime has the agent correct a short answer.
  assert.deepEqual(sized(r.calls.find(c => c.label === 'write').options.schema, 'comments', 'finding'), [2, 2, [0, 1]])
  assert.deepEqual(r.result.draft.comments.map(c => [c.body, c.firstLineUnchecked]), [[long, undefined], ['**high**: b', undefined]],
    'four bullets and a 700-character line, kept whole')
  assert.ok(r.result.draft.body.startsWith(`${summary}\n`), 'a long one-bullet summary is kept')
  assert.equal(r.labels.some(l => l.startsWith('shorten')), false)
  assert.ok(!r.logs.some(l => /over length|shorten/.test(l)))
  const write = r.calls.find(c => c.label === 'write').prompt
  assert.match(write, /only the bullets it needs/)
  assert.match(write, /as short as it allows/)
  assert.doesNotMatch(write, /\d+ words|characters|at most 3/, 'the writer is given no length')
  const prose = await run(BASE, { audit: audited(finding('a')), write: { summary: 'Prose, not bullets.', comments: [{ finding: 0, body: '**high**: a' }] } })
  assert.doesNotMatch(prose.result.draft.body, /Prose/, 'a summary off its format is left out')
  assert.ok(prose.logs.includes('summary left out: off its format'))
  const bare = { summary: '- ok', comments: [{ finding: 0, body: 'No heading here.' }, { finding: 1, body: '**high**: ' }] }
  const off = await run(BASE, { audit: audited(finding('a'), finding('b', 'high', 20)), write: bare })
  assert.deepEqual(off.result.draft.comments.map(c => [c.body, c.firstLineUnchecked]), [['**high**: a', true], ['**high**: b', true]],
    'no heading, or no problem after it: the finding\'s own words')
  const wrong = await run(BASE, { audit: audited(finding('a')), write: { summary: '- ok', comments: [{ finding: 0, body: '**low**: a' }] } })
  assert.deepEqual(wrong.result.draft.comments.map(c => c.body), ['**high**: a'], 'a heading must name its finding\'s severity')
  const plus = await run(BASE, { audit: audited(finding('a')), write: { summary: '+ one', comments: [{ finding: 0, body: '**high**: a' }] } })
  assert.match(plus.result.draft.body, /^\+ one$/m, 'a `+` bullet summary is in format')
})

test('open thread claims are judged; a confirmed one covers our same finding and counts once', async () => {
  const claims = { claims: [{ commentId: 55, threadId: 'T', author: 'coderabbitai[bot]', bot: true, path: 'src/core/a.c', line: 10, url: 'https://x/c55', claim: 'overrun' },
    { commentId: 56, threadId: 'U', author: 'maint', bot: false, path: null, line: null, claim: 'rename it' }] }
  const judge = (p) => /overrun/.test(p) ? confirmedAs('nit') : REFUTED
  const { result, labels, calls } = await run(BASE, { audit: audited(finding('buffer overrun', 'nit')), claims, judge, group: { groups: [{ findings: [0], claims: ['55#1'] }] } })
  assert.equal(labels.filter(l => l.startsWith('judge:')).length, 2)
  assert.ok(calls.filter(c => c.label.startsWith('judge:')).every(c => c.options.agentType === 'finding-verifier'))
  assert.deepEqual(result.claims.map(c => c.verdict), ['confirmed', 'refuted'])
  assert.deepEqual(['status', 'defect'].map(k => result.findings.find(f => f.why === 'buffer overrun')[k]), ['open', 0], 'still ours to recheck')
  assert.deepEqual(result.claims.map(c => c.defect), [0, undefined])
  assert.deepEqual(result.draft.comments, [], 'a covered finding is not posted again')
  assert.match(result.draft.body, /^Confirmed from other reviewers:\n- @coderabbitai\[bot\] \[comment\]\(https:\/\/x\/c55\), point 1$/m, 'by comment and point, never restating the claim')
  const apart = await run(BASE, { audit: audited(finding('other', 'medium', 20)), claims, judge: (p) => /overrun/.test(p) ? confirmedAs('high') : REFUTED })
  for (const label of ['write', 'check-draft']) {
    assert.match(apart.calls.find(c => c.label === label).prompt, /Already on the PR: \[\{"severity":"high","file":"src\/core\/a\.c","line":10,"author":"coderabbitai\[bot\]"\}\]/,
      `${label} weighs the confirmed claims by grade and place`)
  }
  assert.match(apart.calls.find(c => c.label === 'check-draft').prompt, /"file":"src\/core\/a\.c","line":20,"severity":"medium"/)
  assert.doesNotMatch(result.draft.body, /overrun/)
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
  const { result } = await run(BASE, { audit: two, group: { groups: [{ findings: [1, 0], claims: [] }] } })
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
  assert.match(result.draft.body, /^- @coderabbitai\[bot\] on `src\/core\/a\.c:10`, point 1; our grade \*\*high\*\*$/m, 'no url: its place names it')
  const tie = await run(BASE, { audit: audited(finding('buffer overrun', 'low')), claims, judge, group: { groups: [{ findings: [0], claims: ['55#1'] }] } })
  assert.doesNotMatch(tie.result.draft.body, /our grade/, 'only a stronger grade is named')
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
  assert.deepEqual([result.verdict.reasons, result.findings.map(f => [f.defect, 'beside' in f, f.muted])], [['1 blocking finding(s) open'], [[0, false, false], [0, false, false]]],
    'a member posted beside its group is not muted')
  assert.deepEqual(result.findings.map(f => f.lead), [true, false], 'a member posted beside its group is not its lead: its comment states only itself')
  assert.ok(logs.some(l => /beside its group, src\/core\/a\.c:10: the removed reference comment/.test(l)))
  const flagged = await run(BASE, { audit: pair, group, write: written, 'check-draft': { bad: [1], firstBad: [], summaryBad: false }, beyond: { beyond: [{ finding: 0, issue: 'x' }] } })
  assert.deepEqual([flagged.result.draft.comments[1].body, flagged.result.draft.comments[1].firstLineUnchecked], ['**nit**: rf_tv zeroed, and the reference comment removed', true],
    'the fallback is the finding\'s own words, never its verification, and holds auto-post as a body line')
  const claims = { claims: [{ commentId: 55, author: 'a[bot]', bot: true, path: 'src/core/a.c', line: 10, claim: 'change latches lost' }] }
  const narrow = await run(BASE, { audit: audited(finding('change latches and a SETUP lost', 'medium')), claims, judge: () => confirmedAs('medium'),
    group: { groups: [{ findings: [0], claims: ['55#1'] }] }, beyond: { beyond: [{ finding: 0, issue: 'the lost SETUP' }] } })
  assert.match(narrow.calls.find(c => c.label === 'beyond').prompt, /"printed":\["change latches lost"\]/)
  assert.deepEqual([narrow.result.draft.comments.length, narrow.result.findings[0].defect], [1, 0], 'a claim stating less no longer hides our finding')
  assert.match(narrow.result.draft.comments[0].body, /SETUP/)
  const behind = await run(BASE, { audit: audited(finding('change latches lost', 'medium')), claims, judge: () => confirmedAs('medium'),
    group: { groups: [{ findings: [0], claims: ['55#1'] }] } })
  assert.deepEqual([behind.result.draft.comments.length, behind.result.findings.map(f => [f.muted, f.lead])], [0, [[true, true]]], 'a claim stating it mutes our finding')
  const none = await run(BASE, { audit: pair })
  assert.ok(!none.labels.includes('beyond'), 'nothing muted, nothing to ask')
  assert.deepEqual(none.result.findings.map(f => [f.muted, f.lead]), [[false, false], [false, false]], 'written on every finding, grouped or not')
})

test('a dead or malformed answer on muted findings posts them all and leaves coverage unproven', async () => {
  const nits = audited(finding('a', 'nit'), finding('a too', 'nit'))
  const group = { groups: [{ findings: [0, 1], claims: [] }] }
  const clean = await run(BASE, { audit: nits, group })
  assert.deepEqual([clean.result.draft.comments.length, clean.result.verdict.event, clean.result.findings.map(f => f.muted)], [1, 'APPROVE', [false, true]])
  for (const beyond of [null, { beyond: 'x' }, { beyond: [{ finding: 9, issue: 'x' }] }, { beyond: [null] }, { beyond: [{ finding: 0, issue: ' ' }] },
    { beyond: [{ finding: 0 }] }, { beyond: [{ finding: 0, issue: 'x' }, { finding: 0, issue: 'y' }] }]) {
    const { result } = await run(BASE, { audit: nits, group, beyond })
    assert.deepEqual([result.draft.comments.length, result.coverage.unjudged, result.verdict.event, result.findings.map(f => f.defect)],
      [2, [{ kind: 'beyond' }], 'COMMENT', [0, 0]], JSON.stringify(beyond))
    assert.deepEqual(result.findings.map(f => f.muted), [false, false], 'every member posted, none muted')
  }
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const row = (id, severity, commentId) => ({ id, status: 'open', file: 'src/core/a.c', line: 10, severity, confidence: 'high', impact: IMP, severityReason: 'g', commentId, why: `w ${id}` })
  const standing = await run(inc, { ledger: { reviews: 1, last: { head: OLD }, open: [row('pr7-f1', 'high', 901), row('pr7-f2', 'low', 902)] }, check: pinned(inc),
    recheck: (p) => ({ state: 'open', reason: /pr7-f2/.test(p) ? 'still, and the reference comment is gone' : 'still' }), group, beyond: { beyond: [{ finding: 0, issue: 'more' }] } })
  assert.match(standing.calls.find(c => c.label === 'beyond').prompt, /"verified":"still, and the reference comment is gone"/)
  assert.deepEqual(standing.result.draft.comments, [], 'its own comment states the member beside its group')
  assert.doesNotMatch(standing.result.draft.body, /pr7-f2|reference comment is gone/, 'no recheck text reaches the body')
  assert.doesNotMatch(standing.result.draft.body, /more/, 'the issue named is never printed')
  const lead = await run(inc, { ledger: { reviews: 1, last: { head: OLD }, open: [row('pr7-f1', 'high', null), row('pr7-f2', 'low', 902)] }, check: pinned(inc),
    recheck: { state: 'open', reason: 'still' }, group })
  assert.deepEqual(lead.result.draft.comments.map(c => [c.finding, c.body]), [[0, '**high**: w pr7-f1']], 'a group mate\'s comment never states the lead: it gets its own')
  assert.doesNotMatch(lead.result.draft.body, /w pr7-f2/, 'its own comment states the muted member')
  assert.deepEqual(lead.result.findings.map(f => [f.id, f.muted, f.lead]), [['pr7-f1', false, true], ['pr7-f2', true, false]], 'a lead given its own comment is not muted')
})

test('a threads line that is not of the file asked for, the script\'s error, or a relay dead twice blocks the launch', async () => {
  const file = `${BASE.factsDir}/threads-${H}.json`
  // #150's string-wrapped copy, now only as a script could print it: no line of that file.
  const wrapped = await run(BASE, { threads: { file: JSON.stringify({ file, count: 3 }) } })
  assert.deepEqual([wrapped.result.status, wrapped.result.reason, wrapped.result.detail], ['blocked', 'threads-failed', 'wrong file'])
  const failed = await run(BASE, { threads: { file: '', error: 'gh: HTTP 502' } })
  assert.deepEqual([failed.result.reason, failed.result.detail], ['threads-failed', 'gh: HTTP 502'])
  const dead = await run(BASE, { threads: (prompt, label) => { throw new Error(`${label} exploded`) } })
  assert.deepEqual([dead.result.reason, dead.result.detail], ['threads-failed', 'the threads relay died'])
  assert.deepEqual(dead.logs.filter(l => / errored — /.test(l)), ['threads errored — threads exploded', 'threads:retry errored — threads:retry exploded'])
  for (const r of [wrapped, failed]) assert.deepEqual(r.labels.filter(l => l.startsWith('threads')), ['threads'])
  const revived = await run(BASE, { threads: (prompt, label) => label === 'threads' ? null : { file, count: 3 } })
  assert.deepEqual([revived.result.status, revived.labels.filter(l => l.startsWith('threads'))], ['reviewed', ['threads', 'threads:retry']], 'a dead relay gets one fresh attempt')
})

test('the script relays run as the relay role', async () => {
  const { calls } = await run({ ...BASE, mode: 'incremental', scopeBase: OLD }, { check: pinned({ ...BASE, mode: 'incremental', scopeBase: OLD }),
    ledger: { reviews: 1, last: { head: OLD }, open: [] } })
  const relays = calls.filter(c => c.options.agentType === 'relay')
  assert.deepEqual(relays.map(c => c.label), ['emit:dir', 'check', 'threads', 'ledger:manifest', 'ledger:load'],
    'one snapshot of the ledger and its disputes, read whole from the file its load emits')
  assert.ok(relays.slice(1).every(c => /^The working tree is the review checkout\. Run exactly `AGENTRC_EMIT='[^']+' python3 /.test(c.prompt) &&
    c.prompt.includes('answer only its exit status as exit') && c.options.schema.required[0] === 'exit'), 'each answers only its exit')
})

test('a ledger answer missing its members, or not of the review prepare pinned, blocks instead of dropping standing findings', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const standing = { id: 'pr7-f1', status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', commentId: 901 }
  const still = { state: 'open', reason: 'still' }
  const good = await run(inc, { check: pinned(inc), ledger: { reviews: 1, last: { head: OLD }, open: [standing] }, recheck: still })
  assert.equal(good.result.verdict.event, 'REQUEST_CHANGES')
  for (const ledger of [{}, { reviews: 1 }, { reviews: 1, last: { head: OLD } }, { open: [standing] }, { reviews: 0, last: null, open: [] },
    { reviews: 1, open: [standing] }, { reviews: 1, last: { head: MB }, open: [standing] }]) {
    const { result, labels } = await run(inc, { check: pinned(inc), ledger, recheck: still })
    assert.deepEqual([result.status, result.reason], ['blocked', 'ledger-failed'], JSON.stringify(ledger))
    assert.ok(!labels.some(l => l.startsWith('recheck:')), JSON.stringify(ledger))
  }
  const disc = { ...BASE, mode: 'discussion' }
  const same = { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }
  const older = await run(disc, { check: same, ledger: { reviews: 1, last: { head: OLD }, open: [standing] } })
  assert.equal(older.result.reason, 'ledger-failed', 'a discussion continues the review of this very head')
  assert.notEqual((await run(BASE, { ledger: { reviews: 0, last: null, open: [] } })).result.status, 'blocked', 'a first full review reads an empty ledger')
  assert.equal((await run(BASE, { ledger: { reviews: 2, open: [] } })).result.verdict.event, 'APPROVE', 'a full review rechecks whatever stands')
  assert.equal((await run(BASE, { ledger: { reviews: 2 } })).result.reason, 'ledger-failed')
  assert.equal((await run(BASE, { ledger: { reviews: 0, last: null, open: [], disputes: null } })).result.reason, 'ledger-failed', 'no disputes is no answer')
  const row = { ...standing, published: null, repair: { head: OLD, state: 'published', resolved: true } }
  const whole = { reviews: 1, last: { head: OLD }, open: [row], heldThreads: [], disputes: [] }
  assert.equal((await run(inc, { check: pinned(inc), ledgerAsIs: whole, recheck: still })).result.status, 'reviewed')
  const { published, ...unpublished } = row
  const { repair, ...unrepaired } = row
  for (const ledger of [{ ...whole, open: [unpublished] }, { ...whole, open: [unrepaired] }, { ...whole, open: [{ ...row, repair: { resolved: true } }] },
    (({ heldThreads, ...l }) => l)(whole)]) {
    const { result, labels } = await run(inc, { check: pinned(inc), ledgerAsIs: ledger, recheck: still })
    assert.deepEqual([result.status, result.reason], ['blocked', 'ledger-failed'], JSON.stringify(ledger))
    assert.ok(!labels.some(l => l.startsWith('recheck:')), JSON.stringify(ledger))
  }
  assert.equal((await run(BASE, { ledgerAsIs: { reviews: 0, last: null, open: [], disputes: [] } })).result.status, 'reviewed', 'an empty ledger prints no heldThreads')
})

test('a ledger answer past 64 KiB crosses whole through one emitted file, every text exact, in every mode', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  // As ledger.py's view writes a carried row: without its impact and severityReason.
  const row = (n) => ({ id: `pr7-f${n}`, status: 'open', file: 'src/core/a.c', line: 10 + n, severity: 'low', confidence: 'high', commentId: 900 + n,
    why: `${wordy(900)} “é” \\ "q" — ${n}`, published: n % 2 ? `**low**: edited by the human — ${n}` : null, repair: null })
  const open = [...Array(40).keys()].map(row)
  const disputes = [...Array(30).keys()].map(n => ({ findingId: `pr7-f${n}`, key: `k${n}`, rootCommentId: 900 + n, replies: [{ id: 5000 + n, digest: 'd', author: 'c' }] }))
  const still = { state: 'open', reason: 'still' }
  const same = { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }
  for (const [a, check, last] of [[BASE, pinned(BASE), OLD], [inc, pinned(inc), OLD], [{ ...BASE, mode: 'discussion' }, same, H]]) {
    const whole = { reviews: 1, last: { head: last }, open, heldThreads: [], disputes }
    const m = manifestOf(whole, a)
    assert.ok(m.length > 64 * 1024, `${m.length} bytes`)
    const { result, calls, parsed } = await run(a, { check, ledgerAsIs: whole, recheck: still })
    assert.equal(result.status, 'reviewed', a.mode)
    assert.deepEqual(parsed.find(v => v && Array.isArray(v.open) && v.open.length === 40), whole, `${a.mode}: the answer rebuilt exactly`)
    assert.equal(calls.filter(c => c.label.startsWith('ledger:load')).length, 1, 'one load')
    assert.equal(calls.filter(c => c.label.startsWith('recheck:')).length, a.mode === 'discussion' ? 30 : 40, a.mode)
    const carried = result.findings.filter(f => f.id)
    assert.equal(carried.length, a.mode === 'discussion' ? 30 : 40)
    assert.ok(carried.every(f => f.severity === 'low' && !('impact' in JSON.parse(JSON.stringify(f))) && !('severityReason' in JSON.parse(JSON.stringify(f)))),
      `${a.mode}: unregraded, the result leaves the grading out and save keeps the ledger's`)
  }
  const regrade = { state: 'open', reason: 'still', severity: 'medium', impact: IMP, severityReason: 'worse now', confidence: 'high' }
  const whole = { reviews: 1, last: { head: OLD }, open: open.slice(0, 1), heldThreads: [], disputes: [] }
  const { result } = await run(inc, { check: pinned(inc), ledgerAsIs: whole, recheck: regrade })
  assert.deepEqual((({ severity, impact, severityReason }) => ({ severity, impact, severityReason }))(result.findings[0]),
    { severity: 'medium', impact: IMP, severityReason: 'worse now' }, 'a regrade carries its whole grading')
})

test('a loaded ledger text off the manifest\'s digest, a load error or a dead load blocks before any review work', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const row = { id: 'pr7-f1', status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', commentId: 901, why: wordy(600), published: null, repair: null }
  const whole = { reviews: 1, last: { head: OLD }, open: [row], heldThreads: [], disputes: [] }
  const still = { state: 'open', reason: 'still' }
  const go = (load) => run(inc, { check: pinned(inc), ledgerAsIs: whole, recheck: still, 'ledger:load': load })
  assert.equal((await go(undefined)).result.status, 'reviewed')
  // The live loader's "correction": the text changed, its digest kept, or both changed together.
  const edit = (l) => l.text.replace('"high"', '"low"')
  for (const [why, load] of [['does not match the manifest digest', (l) => ({ ...l, text: edit(l) })],
    ['does not match the manifest digest', (l) => ({ text: edit(l), digest: fnv1a(edit(l)) })],
    ['state_transfer.py: cannot read x: No such file or directory', { error: 'cannot read x: No such file or directory' }],
    ['the ledger load relay died', null]]) {
    const { result, labels } = await go(load)
    assert.deepEqual([result.status, result.reason], ['blocked', 'ledger-failed'], why)
    assert.ok(result.detail.endsWith(why), result.detail)
    assert.ok(!labels.some(l => l.startsWith('recheck:')), why)
  }
})

test('a resumed review reloads the lines its relays wrote; a file there before a live run is reloaded only when it names itself', async () => {
  const first = await run(BASE, {}, { keepEmit: true })
  try {
    assert.deepEqual(first.loads.map(f => f.split('/').at(-1).split('.')[0]), ['check', 'threads', 'ledger_manifest', 'ledger_load'])
    const resumed = await run(BASE, {}, { resume: first.journal })
    assert.deepEqual(resumed.calls, [], 'no agent ran again')
    assert.deepEqual([resumed.loads, resumed.result], [first.loads, first.result])
    const again = await run(BASE, {}, { emitDir: first.emitDir })
    assert.deepEqual(again.result, first.result)
    assert.ok(again.logs.includes('check: the line an earlier attempt of this call wrote, reloaded'), again.logs.join('\n'))
    writeFileSync(first.loads[0], readFileSync(first.loads[1], 'utf8'))
    // A file there that names another: the check ran nothing (exit 3) and its outcome is unknown; its fresh attempt runs.
    const foreign = await run(BASE, {}, { emitDir: first.emitDir })
    assert.ok(foreign.logs.includes(`check: an earlier attempt of this call claimed ${first.loads[0]} and left no line of it — its outcome is unknown`), foreign.logs.join('\n'))
    assert.deepEqual([foreign.labels[0], foreign.result.status], ['check:retry', 'reviewed'])
  } finally { rmSync(first.emitDir.split('/agentrc-emit/')[0], { recursive: true, force: true }) }
})

test('a ledger manifest of another head, path or digest blocks before reading', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const row = { id: 'pr7-f1', status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', commentId: 901, why: 'w', published: null, repair: null }
  const whole = { reviews: 1, last: { head: OLD }, open: [row], heldThreads: [], disputes: [] }
  const real = manifestOf(whole, inc)
  const name = real.file.split('/').at(-1)
  for (const wrong of [{ head: OLD }, { repo: 'o/x' }, { pr: 8 }, { file: `/tmp/other/${name}` }, { file: `${inc.factsDir}/ledger-x.json` },
    { file: `${inc.factsDir}/../${name}` }, { digest: 'xyz' }, { length: 0 }, { error: 'no ledger' }]) {
    const { result, labels } = await run(inc, { check: pinned(inc), ledgerAsIs: whole, 'ledger:manifest': { ...real, ...wrong } })
    assert.deepEqual([result.status, result.reason], ['blocked', 'ledger-failed'], JSON.stringify(wrong))
    assert.ok(!labels.includes('ledger:load'), JSON.stringify(wrong))
  }
  const dead = await run(inc, { check: pinned(inc), 'ledger:manifest': null })
  assert.deepEqual([dead.result.reason, dead.result.detail], ['ledger-failed', 'the ledger relay died'])
})

test('a draft or check agent that throws takes its null path; a check relay that throws blocks', async () => {
  const thrown = (label) => () => { throw new Error(`${label} exploded`) }
  const check = await run(BASE, { check: thrown('check') })
  assert.deepEqual([check.result.reason, check.result.detail], ['check-failed', 'the check relay died'])
  const write = await run(BASE, { audit: audited(finding('a')), write: thrown('write') })
  assert.deepEqual([write.result.verdict.event, write.result.draft.comments.map(c => c.body)], ['REQUEST_CHANGES', ['**high**: a']])
  const checked = await run(BASE, { audit: audited(finding('a')), write: { summary: '- ok', comments: [{ finding: 0, body: '**high**: a, and more' }] }, 'check-draft': thrown('check-draft') })
  assert.deepEqual(checked.result.draft.comments.map(c => c.body), ['**high**: a'], 'unchecked: the finding\'s own words')
  const nits = audited(finding('a', 'nit'), finding('a too', 'nit'))
  const beyond = await run(BASE, { audit: nits, group: { groups: [{ findings: [0, 1], claims: [] }] }, beyond: thrown('beyond') })
  assert.deepEqual([beyond.result.draft.comments.length, beyond.result.coverage.unjudged], [2, [{ kind: 'beyond' }]])
  for (const [label, r] of Object.entries({ check, write, 'check-draft': checked, beyond })) {
    assert.ok(r.logs.includes(`${label} errored — ${label} exploded`), `${label} logs its error`)
  }
})

test('an invalid or dead grouping is judged again once, told why, before its findings stay ungrouped (#150)', async () => {
  const two = audited(finding('a'), finding('b'), { ...finding('c'), file: 'src/core/b.c' })
  const good = { groups: [{ findings: [0, 1], claims: [] }] }
  const thrown = new Error('group exploded')
  for (const bad of [{ groups: [{ findings: [0, 0], claims: [] }] }, null, thrown]) {
    const { result, calls } = await run(BASE, { audit: two, group: (prompt, label) => { if (label !== 'group') return good; if (bad === thrown) throw bad; return bad } })
    assert.deepEqual(result.coverage.unjudged, [], JSON.stringify(bad))
    assert.equal(result.draft.comments.length, 2, 'one comment for the grouped pair')
    assert.match(calls.find(c => c.label === 'group:retry').prompt, /answer was not usable/)
  }
  const both = await run(BASE, { audit: two, group: () => { throw thrown } })
  assert.deepEqual([both.result.coverage.unjudged, both.result.draft.comments.length], [[{ kind: 'group' }], 3])
  assert.deepEqual(both.logs.filter(l => / errored — /.test(l)), ['group errored — group exploded', 'group:retry errored — group exploded'])
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
  const again = await run(inc, { ledger: { reviews: 1, last: { head: OLD }, open: [standingRow] }, check: pinned(inc), recheck: { state: 'open', reason: 'still' }, claims: thread,
    judge: () => confirmedAs('low'), group: { groups: [{ findings: [0], claims: ['55#1'] }] } })
  assert.match(again.calls.find(c => c.label === 'group').prompt, /overrun past the buffer end/, 'standing findings are regrouped every run, whole')
  assert.deepEqual(again.result.verdict.reasons, ['1 blocking finding(s) open'], 'the high finding and its low claim are one defect, counted high')
  assert.match(again.result.draft.body, /our grade \*\*high\*\*/)
  assert.deepEqual(again.result.draft.comments, [], 'the thread states it')
  const resolved = await run(inc, { ledger: { reviews: 1, last: { head: OLD }, open: [standingRow] }, check: pinned(inc), recheck: { state: 'open', reason: 'still' } })
  assert.equal(resolved.result.verdict.event, 'REQUEST_CHANGES', 'its thread gone, the finding still stands on its own')
  assert.deepEqual(resolved.result.draft.comments.map(c => [c.finding, c.body]), [[0, '**high**: overrun past the buffer end']], 'stated by no comment, it gets one')
  assert.match(resolved.calls.find(c => c.label === 'write').prompt, /"why":"overrun past the buffer end"/)
  const listed = await run(inc, { ledger: { reviews: 1, last: { head: OLD }, open: [{ ...standingRow, inBody: true }] }, check: pinned(inc), recheck: { state: 'open', reason: 'still' } })
  assert.deepEqual([listed.result.draft.comments, listed.labels.includes('write'), listed.result.verdict.event], [[], false, 'REQUEST_CHANGES'],
    'a line of our review body the PR still shows states it: nothing is written again')
  const refound = await run(inc, { ledger: { reviews: 1, last: { head: OLD }, open: [{ ...standingRow, commentId: 900 }] }, check: pinned(inc), recheck: { state: 'open', reason: 'still' },
    audit: audited(finding('overrun again', 'medium')), group: { groups: [{ findings: [0, 1], claims: [] }] } })
  assert.deepEqual(refound.result.findings.map(f => [f.id, f.defect]), [['pr7-f1', 0], [undefined, 0]], 'a new copy of a standing finding joins its defect')
  assert.deepEqual([refound.result.draft.comments, refound.result.verdict.reasons], [[], ['1 blocking finding(s) open']])
})

test('findings linked as one defect on an earlier review are regrouped, and count once only while still one', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const row = (id, commentId) => ({ id, status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', confidence: 'high', impact: IMP, severityReason: 'g', commentId, why: `w ${id}`, defect: 0 })
  const ledger = { reviews: 1, last: { head: OLD }, open: [row('pr7-f1', 901), row('pr7-f2', null)] }
  const still = { state: 'open', reason: 'still' }
  const both = await run(inc, { ledger, check: pinned(inc), recheck: still, group: { groups: [{ findings: [0, 1], claims: [] }] } })
  assert.match(both.calls.find(c => c.label === 'group').prompt, /w pr7-f2/, 'standing findings are regrouped: a push can split them')
  assert.deepEqual([both.result.verdict.reasons, both.result.findings.map(f => f.defect)], [['1 blocking finding(s) open'], [0, 0]])
  assert.deepEqual(both.result.draft.comments, [], 'the comment on pr7-f1 states the defect')
  const apart = await run(inc, { ledger, check: pinned(inc), recheck: still })
  assert.deepEqual([apart.result.verdict.reasons, apart.result.findings.map(f => f.defect)], [['2 blocking finding(s) open'], [null, null]])
  assert.deepEqual(apart.result.draft.comments.map(c => [c.finding, c.body]), [[1, '**high**: w pr7-f2']], 'split, the one never posted gets its comment')
  const dead = await run(inc, { ledger, check: pinned(inc), recheck: still, group: null })
  assert.deepEqual([dead.result.verdict.reasons.slice(0, 1), dead.result.findings.map(f => f.defect)], [['2 blocking finding(s) open'], [null, null]],
    'a failed grouping never falls back to an earlier one')
  assert.deepEqual(dead.result.draft.comments.map(c => [c.finding, c.body]), [[1, '**high**: w pr7-f2']])
  const fixed = await run(inc, { ledger, check: pinned(inc), recheck: (p) => /pr7-f1/.test(p) ? { state: 'fixed', reason: 'gone' } : { state: 'open', reason: 'still' } })
  assert.deepEqual(fixed.result.findings.map(f => [f.status, f.defect]), [['fixed', null], ['open', null]], 'the linked one is rechecked on its own')
  assert.deepEqual(fixed.result.verdict.reasons, ['1 blocking finding(s) open'])
})

test('an incremental review rechecks earlier findings: fixed ones get a fix note, open ones keep their severity', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const ledger = { reviews: 1, last: { head: OLD }, open: [{ id: 'pr7-f1', file: 'src/core/a.c', line: 10, severity: 'high', commentId: 901 },
    { id: 'pr7-f2', file: 'src/core/a.c', line: 30, severity: 'low', commentId: 902 }] }
  const fixed = await run(inc, { ledger, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual(fixed.audits[0].diff, { base: OLD, head: H })
  assert.deepEqual(fixed.result.draft.replies.map(r => [r.commentId, r.body]), [[901, 'Fixed in bbbbbbbbbbbb.'], [902, 'Fixed in bbbbbbbbbbbb.']])
  assert.equal(fixed.result.verdict.event, 'APPROVE')
  assert.doesNotMatch(fixed.result.draft.body, /Reviewed|finding\(s\)|CI: green|Hardware/, 'no count, CI-green or hardware-not-run line')
  assert.deepEqual(fixed.result.draft.resolves, [])
  const deferred = { reviews: 1, last: { head: OLD }, open: [{ ...ledger.open[0], status: 'fixed', resolveDue: { replied: true } }, ledger.open[1]] }
  const again = await run(inc, { ledger: deferred, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual(again.result.draft.replies.map(r => r.commentId), [902], 'a due resolve with our reply on the thread needs no new note')
  assert.deepEqual(again.result.draft.resolves, [{ findingId: 'pr7-f1', commentId: 901 }])
  assert.match(again.calls.find(c => c.label === 'recheck:pr7-f1').prompt, /judge that text, not the draft/)
  const reopened = await run(inc, { ledger: deferred, recheck: () => ({ state: 'open', reason: 'back' }), check: pinned(inc) })
  assert.deepEqual(reopened.result.draft.resolves, [], 'standing again: nothing to resolve')
  const noted = { reviews: 1, last: { head: OLD }, open: [{ ...deferred.open[0], resolveDue: { replied: false } }] }
  const renote = await run(inc, { ledger: noted, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual([renote.result.draft.replies.map(r => r.commentId), renote.result.draft.resolves], [[901], []],
    'with no reply of ours there, the note is drafted; the thread resolves once it is published')
  const held = [{ findingId: 'pr7-f3', commentId: 903, why: 'resolve unconfirmed' }]
  assert.deepEqual((await run(inc, { ledger: { ...deferred, heldThreads: held }, recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })).result.heldThreads, held)
  const unjudged = await run(inc, { ledger: deferred, recheck: () => null, check: pinned(inc) })
  assert.deepEqual(unjudged.result.draft.resolves, [], 'no recheck on this head, no resolve')
  const conceded = { reviews: 1, last: { head: OLD }, open: [{ ...deferred.open[0], status: 'withdrawn' }] }
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

test('a comment the check flags falls back to its finding\'s own words, whatever their length, with no other round', async () => {
  const r = await run(BASE, { audit: audited(finding(wordy(100))), 'check-draft': { bad: [0], firstBad: [], summaryBad: false } })
  assert.deepEqual([r.result.draft.comments[0].body, r.result.draft.comments[0].firstLineUnchecked], [`**high**: ${wordy(100)}`, true])
  assert.deepEqual(r.labels.filter(l => /^(write|check-draft|shorten)/.test(l)), ['write', 'check-draft'], 'one write, one check')
  assert.ok(!r.logs.some(l => /over length/.test(l)))
})

test('a finding an auto-fix repair noted gets no second fix note, and one still standing is raised again on a new comment', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const repaired = (repair) => ({ reviews: 1, last: { head: OLD }, open: [{ id: 'pr7-f1', file: 'src/core/a.c', line: 10, severity: 'high', why: 'overrun past the buffer end', commentId: 901, repair }] })
  const done = { head: OLD, state: 'published', resolved: true }
  const fixed = await run(inc, { ledger: repaired(done), recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual([fixed.result.draft.replies, fixed.result.draft.resolves, fixed.result.draft.comments], [[], [], []])
  const unsure = await run(inc, { ledger: repaired({ ...done, state: 'uncertain', resolved: false }), recheck: () => ({ state: 'fixed', reason: 'guarded now' }), check: pinned(inc) })
  assert.deepEqual(unsure.result.draft.replies, [], 'an unconfirmed note is reconciled, never noted twice')
  const still = await run(inc, { ledger: repaired(done), recheck: () => ({ state: 'open', reason: 'still overruns' }), check: pinned(inc) })
  assert.deepEqual(still.result.draft.comments, [{ path: 'src/core/a.c', line: 10, finding: 0,
    body: '**high**: overrun past the buffer end\n- The repair in cccccccccccc did not fix this; its earlier thread was resolved.', firstLineUnchecked: true }],
    'no check verified its first line, which is all the body shows of it off the diff')
  assert.equal(still.result.findings[0].id, 'pr7-f1', 'raised again under its own id')
  const pair = repaired(done)
  pair.open.push({ id: 'pr7-f2', file: 'src/core/a.c', line: 10, severity: 'critical', why: 'overrun', commentId: 902 })
  const both = await run(inc, { ledger: pair, recheck: () => ({ state: 'open', reason: 'still' }), check: pinned(inc), group: { groups: [{ findings: [0, 1], claims: [] }] } })
  assert.deepEqual([both.result.draft.comments.map(c => c.finding), both.result.findings.map(f => [f.id, f.muted])],
    [[0], [['pr7-f1', false], ['pr7-f2', false]]], 'the muted member raised again has its own comment, so it is not muted')
  const edited = repaired(done)
  edited.open[0].published = '**high**: overrun past the end of buf'
  assert.equal((await run(inc, { ledger: edited, recheck: () => ({ state: 'open', reason: 'still' }), check: pinned(inc) })).result.draft.comments[0].body,
    '**high**: overrun past the end of buf\n- The repair in cccccccccccc did not fix this; its earlier thread was resolved.', 'restated as the human edited it')
  const regraded = { state: 'open', reason: 'still', severity: 'critical', impact: IMP, severityReason: 'graded', confidence: 'high' }
  assert.equal((await run(inc, { ledger: edited, recheck: () => regraded, check: pinned(inc) })).result.draft.comments[0].body,
    '**critical**: overrun past the end of buf\n- The repair in cccccccccccc did not fix this; its earlier thread was resolved.', 'a regrade replaces only the old grade marker')
  const relabelled = repaired(done)
  relabelled.open[0].published = '**bug**: overrun past the end of buf'
  assert.equal((await run(inc, { ledger: relabelled, recheck: () => regraded, check: pinned(inc) })).result.draft.comments[0].body,
    '**bug**: overrun past the end of buf\n- The repair in cccccccccccc did not fix this; its earlier thread was resolved.', 'a heading the human changed is left alone')
  const NOTE = '\n- The repair in cccccccccccc did not fix this; its earlier thread was resolved.'
  edited.open[0].published = `**high**: ${wordy(85)} ${'x'.repeat(300)}`
  const longEdit = await run(inc, { ledger: edited, recheck: () => ({ state: 'open', reason: 'still' }), check: pinned(inc) })
  assert.deepEqual(longEdit.result.draft.comments.map(c => c.body), [`${edited.open[0].published}${NOTE}`], 'a long human edit is kept whole, its repair note on the comment')
  assert.doesNotMatch(longEdit.result.draft.body, /Raised again/)
  assert.equal(longEdit.labels.some(l => l.startsWith('shorten')), false, 'the human\'s own words are never rewritten')
  const longWhy = repaired(done)
  longWhy.open[0].why = wordy(85)
  const unedited = await run(inc, { ledger: longWhy, recheck: () => ({ state: 'open', reason: 'still' }), check: pinned(inc) })
  assert.deepEqual(unedited.result.draft.comments.map(c => [c.body, c.firstLineUnchecked]), [[`**high**: ${wordy(85)}${NOTE}`, true]])
  assert.ok(!unedited.logs.some(l => /over length/.test(l)))
  const visible = await run(inc, { ledger: repaired({ ...done, resolved: false }), recheck: () => ({ state: 'open', reason: 'still' }), check: pinned(inc) })
  assert.deepEqual(visible.result.draft.comments, [], 'its thread is still open, so its comment still states it')
  const reopenedThread = { ...repaired(done), heldThreads: [{ findingId: 'pr7-f1', commentId: 901, why: 'reopened after it was resolved' }] }
  const restated = await run(inc, { ledger: reopenedThread, recheck: () => ({ state: 'open', reason: 'still' }), check: pinned(inc) })
  assert.deepEqual(restated.result.draft.comments, [], 'reopened since the repair resolved it, its thread states it again')
  assert.doesNotMatch(still.result.draft.body, /Raised again/, 'the note stays on the comment')
  const shifted = await run(inc, { ledger: repaired(done), recheck: () => ({ state: 'open', reason: 'still', file: 'src/core/a.c', line: 14 }), check: pinned(inc) })
  assert.deepEqual([shifted.result.draft.comments.map(c => [c.path, c.line]), shifted.result.findings[0].line], [[['src/core/a.c', 14]], 14],
    'raised again where the recheck found it on this head')
  assert.match(shifted.calls.find(c => c.label === 'recheck:pr7-f1').prompt, /`file` and `line` are where it stands/)
  for (const v of [{ state: 'fixed', reason: 'gone', line: 14 }, { state: 'open', reason: 'still', file: '../x.c', line: 14 }, { state: 'open', reason: 'still', file: '/src/core/a.c', line: 14 }, { state: 'open', reason: 'still', line: 0 }]) {
    assert.equal((await run(inc, { ledger: repaired(done), recheck: () => v, check: pinned(inc) })).result.findings[0].line, 10, JSON.stringify(v))
  }
})

test('args that differ from the pins prepare recorded for this head are blocked before review', async () => {
  for (const pins of [{ mergeBase: OLD, scopeBase: MB, mode: 'full', groups: ['src/core'] }, { mergeBase: MB, scopeBase: MB, mode: 'full', groups: ['src'] }, undefined]) {
    const { result, labels } = await run(BASE, { check: { ...pinned(BASE), pins } })
    assert.equal(result.reason, 'scope-mismatch')
    assert.deepEqual(labels, ['check'])
  }
})

const LEDGER = { reviews: 1, last: { head: OLD }, open: [{ id: 'pr7-f1', status: 'open', file: 'src/core/a.c', line: 10, severity: 'high', commentId: 901 },
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
    rootCommentId: 901, answer: { body: 'Agreed, the caller masks the IRQ first.', resolve: true } }])
  assert.deepEqual(result.draft.replies, [], 'a concession is a thread answer awaiting approval, not an auto-granted fix note')
  assert.equal(result.verdict.event, 'COMMENT', 'withdrawn drops the high finding; the medium one still caps')
  const { rootCommentId, ...rootless } = DISPUTE
  const lossy = await run(inc, { check: pinned(inc), ledger: LEDGER, disputes: { disputes: [rootless] }, recheck })
  assert.equal(lossy.result.findings.find(f => f.id === 'pr7-f1').disputes[0].rootCommentId, rootCommentId,
    'a copy that dropped the root keeps the judged thread: a comment raised again would take over the finding\'s commentId')
})

test('upheld keeps its severity and leaves the thread open; disputed only blocks approval; a dead verifier records nothing', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const base = { check: pinned(inc), ledger: { reviews: 1, last: { head: OLD }, open: [LEDGER.open[0]] }, disputes: { disputes: [DISPUTE] } }
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
  assert.match(blocker.result.draft.body, /^Blocking: `src\/core\/a\.c:10`\.\n\nDisputed, waiting for a maintainer: `src\/core\/a\.c:10`\.$/m)
  assert.doesNotMatch(blocker.result.draft.body, /Verdict/, 'the human picks the event: the body proposes none')
  assert.doesNotMatch(upheld.result.draft.body, /Disputed/, 'no dispute, no label')
  const bodyClaim = { claims: [{ commentId: 56, threadId: null, author: 'greptile[bot]', bot: true, path: null, line: null, claim: 'race' }] }
  const pathless = await run(inc, { ...base, recheck: () => ({ state: 'disputed', reason: 'x' }), claims: bodyClaim,
    judge: () => confirmedAs('high') })
  assert.match(pathless.result.draft.body, /^Blocking: @greptile\[bot\]'s comment\.\n\nDisputed[\s\S]*^- @greptile\[bot\] comment 56, point 1$/m, 'a claim with no path is named by its author')
  const dead = await run(inc, { ...base, recheck: () => null })
  assert.equal(dead.result.findings[0].disputes, undefined)
  assert.deepEqual(dead.result.coverage.unjudged, [{ kind: 'recheck', id: 'pr7-f1' }])
  const kept = await run(inc, { ...base, disputes: { disputes: [] }, ledger: { reviews: 1, last: { head: OLD }, open: [{ ...LEDGER.open[0], status: 'upheld' }] }, recheck: () => ({ state: 'open', reason: 'same' }) })
  assert.equal(kept.result.findings[0].status, 'upheld', 'no new reply cannot turn upheld back into open')
})

test('a discussion run judges only answered findings, with no audit and no claims pass', async () => {
  const disc = { ...BASE, mode: 'discussion' }
  const reviewed = { ...LEDGER, last: { head: H } }
  const { result, audits, labels } = await run(disc, { check: { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }, ledger: reviewed,
    disputes: { disputes: [DISPUTE] }, recheck: () => ({ state: 'upheld', reason: 'still' }) })
  assert.deepEqual(audits, [])
  assert.ok(!labels.includes('claims'))
  assert.deepEqual(labels.filter(l => l.startsWith('recheck:')), ['recheck:pr7-f1'])
  assert.equal(result.mode, 'discussion')
  const both = { disputes: [DISPUTE, { ...DISPUTE, findingId: 'pr7-f2', key: 'k2', rootCommentId: 902 }] }
  const two = await run(disc, { check: { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }, ledger: reviewed, disputes: both,
    recheck: () => ({ state: 'upheld', reason: 'still' }), group: { groups: [{ findings: [0, 1], claims: [] }] } })
  assert.deepEqual([two.labels.includes('group'), two.result.findings.map(f => 'defect' in f)], [false, [false, false]], 'a discussion leaves the saved groups alone')
  const idle = await run(disc, { check: { ...pinned(BASE), pins: { ...pinned(BASE).pins, mode: 'same' } }, ledger: reviewed })
  assert.equal(idle.result.status, 'nothing-new')
})

test('a thread answer of any length is kept as the recheck wrote it; the answer prompt carries the format', async () => {
  const inc = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const base = { check: pinned(inc), ledger: { reviews: 1, last: { head: OLD }, open: [LEDGER.open[0]] }, disputes: { disputes: [DISPUTE] } }
  const long = `${wordy(100)} ${'x'.repeat(400)}`
  const kept = await run(inc, { ...base, recheck: () => ({ state: 'upheld', reason: 'r', answer: long }) })
  const ask = kept.calls.find(c => c.label === 'recheck:pr7-f1').prompt
  assert.match(ask, /the evidence and nothing else, bullets for more than one point/)
  assert.doesNotMatch(ask, /\d+ words/)
  assert.deepEqual(kept.result.findings[0].disputes[0].answer, { body: long, resolve: false })
  assert.equal(kept.labels.some(l => l.startsWith('shorten')), false)
  assert.ok(!kept.logs.some(l => /over length/.test(l)))
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
  const base = { check: pinned(inc), ledger: { reviews: 1, last: { head: OLD }, open: [{ ...LEDGER.open[0], confidence: 'high', impact: IMP, severityReason: 'old' }] } }
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

// An auto-fix recheck (prepare --recheck-from R): R the reviewed head, S the synthetic base a base merge needs.
const S = 'd'.repeat(40)
const RECHECK = { ...BASE, mode: 'incremental', scopeBase: S, recheckFrom: OLD }
const STANDING = { reviews: 1, last: { head: OLD }, open: [] }

test('a recheck scans from the base prepare pinned and continues the review submitted on the reviewed head', async () => {
  for (const recheckFrom of ['x', OLD.slice(1), 7]) await assert.rejects(run({ ...RECHECK, recheckFrom }), /recheckFrom/, String(recheckFrom))
  const ok = await run(RECHECK, { check: pinned(RECHECK), ledger: STANDING, audit: audited(finding('overrun')) })
  assert.equal(ok.result.status, 'reviewed')
  assert.deepEqual(ok.audits[0].diff, { base: S, head: H })
  assert.equal(ok.result.recheckFrom, OLD)
  assert.equal((await run(RECHECK, { check: pinned(RECHECK), ledger: { ...STANDING, last: { head: S } } })).result.reason, 'ledger-failed')
  assert.equal((await run(RECHECK, { check: pinned({ ...RECHECK, recheckFrom: null }), ledger: STANDING })).result.reason, 'scope-mismatch', 'pins of no recheck')
  const plain = { ...BASE, mode: 'incremental', scopeBase: S }
  assert.equal((await run(plain, { check: pinned(RECHECK), ledger: STANDING })).result.reason, 'scope-mismatch', 'a recheck launched as a plain review')
  const legacy = { ...BASE, mode: 'incremental', scopeBase: OLD }
  assert.equal((await run(legacy, { check: pinned(legacy), ledger: STANDING })).result.status, 'reviewed', 'args from before recheckFrom')
  assert.equal((await run({ ...legacy, recheckFrom: null }, { check: pinned(legacy), ledger: STANDING })).result.status, 'reviewed')
  const full = { ...BASE, recheckFrom: OLD }
  assert.equal((await run(full, { check: pinned(full), ledger: STANDING })).result.status, 'reviewed', 'rewritten history: a full recheck')
  for (const ledger of [{ ...STANDING, last: { head: MB } }, { reviews: 0, open: [] }]) {
    assert.equal((await run(full, { check: pinned(full), ledger })).result.reason, 'ledger-failed', JSON.stringify(ledger))
  }
})

test('a recheck with nothing left to scan runs no audit and still rechecks what stands', async () => {
  await assert.rejects(run({ ...BASE, groups: [] }), /groups/)
  const empty = { ...RECHECK, groups: [] }
  const ledger = { reviews: 1, last: { head: OLD }, open: [{ id: 'pr7-f1', file: 'src/core/a.c', line: 10, severity: 'low', commentId: 901 }] }
  const { result, audits, labels } = await run(empty, { check: pinned(empty), ledger, recheck: () => ({ state: 'fixed', reason: 'gone' }) })
  assert.deepEqual([result.status, audits.length], ['reviewed', 0])
  assert.ok(labels.includes('recheck:pr7-f1'))
  assert.deepEqual(result.draft.replies.map(r => r.commentId), [901])
})

// code-audit returns the verifier's grade, the scanner's only a guess (code_audit_harness.mjs): that grade is the one read.
test('a recheck raises no new nit; a plain review and what already stands keep theirs', async () => {
  const audit = audited(finding('naming', 'nit', 10), finding('off by one', 'low', 20))
  const carriedNit = { id: 'pr7-f1', file: 'src/core/a.c', line: 30, severity: 'nit', commentId: 901 }
  const ledger = { ...STANDING, open: [carriedNit] }
  const still = () => ({ state: 'open', reason: 'still' })
  for (const a of [RECHECK, { ...BASE, recheckFrom: OLD }]) {
    const { result, logs } = await run(a, { check: pinned(a), ledger, audit, recheck: still })
    assert.deepEqual(result.findings.map(f => [f.id ?? null, f.severity]), [['pr7-f1', 'nit'], [null, 'low']], a.mode)
    assert.deepEqual(result.suppressed, [{ file: 'src/core/a.c', line: 10, dimension: 'correctness: x', severity: 'nit', why: 'naming', reason: 'recheck-nit' }])
    assert.deepEqual(result.draft.comments.map(c => [c.line, c.finding]), [[20, 1]], 'the comment indexes the low, the nit has none')
    assert.ok(logs.some(l => /1 new nit\(s\) not raised on a recheck/.test(l)))
  }
  const plainArgs = { ...BASE, mode: 'incremental', scopeBase: OLD }
  const plain = await run(plainArgs, { check: pinned(plainArgs), ledger, audit, recheck: still })
  assert.deepEqual(plain.result.findings.map(f => f.severity), ['nit', 'nit', 'low'])
  assert.deepEqual(plain.result.suppressed, [])
})
