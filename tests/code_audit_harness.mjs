import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = readFileSync(new URL('../workflows/code-audit.js', import.meta.url), 'utf8')
  .replace(/^export const meta = /m, 'const meta = globalThis.__meta = ')

// The runtime sandbox lacks these; Node has them, so a body tested only here can
// depend on one and still refuse every real run (see pr-babysit's hostOf).
const ABSENT = ['URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'Buffer', 'process', 'fetch', 'structuredClone']

const finding = (line, why) =>
  ({ file: 'src/a.c', line, snippet: 'x = y;', why, severity: 'high', confidence: 'medium' })

// Drive the workflow against stub agents keyed by label prefix. `scans` maps
// "<dir>|<dim>" to a findings array (null = dead scanner); `verdicts` maps a
// finding's `why` to real (null = dead verifier). `reverse` makes later
// verifiers of a batch resolve before earlier ones; `finished` records the
// order in which verifiers actually returned.
async function run(args, { scans = {}, verdicts = {}, reverse = false } = {}) {
  const calls = []
  const logs = []
  const finished = []
  const agent = async (prompt, options) => {
    calls.push({ label: options.label, agentType: options.agentType, prompt: String(prompt), schema: options.schema })
    if (options.label.startsWith('scan:')) {
      const m = /^Review (.+?) for exactly one dimension: (.+?)\. Read the sources yourself/.exec(prompt)
      assert.ok(m, prompt)
      const key = `${m[1]}|${m[2]}`
      assert.ok(key in scans, `unstubbed scan ${key}`)
      const fs = scans[key]
      return fs === null ? null : { scope: m[1], dimension: m[2], findings: fs }
    }
    if (options.label.startsWith('verify:')) {
      const f = JSON.parse(/^Finding: (.+)$/m.exec(prompt)[1])
      assert.ok(f.why in verdicts, `unstubbed verdict ${f.why}`)
      const real = verdicts[f.why]
      if (reverse) await new Promise(r => setTimeout(r, 20 - 5 * Number(options.label.split(':')[2])))
      finished.push(f.why)
      return real === null ? null : { real, reason: real ? 'holds' : 'refuted' }
    }
    throw new Error(`unexpected label ${options.label}`)
  }
  // The runtime's contract: parallel() runs every thunk concurrently and a
  // failing one resolves to null at its own index; pipeline() advances each
  // item through the stages independently, with no barrier between stages.
  const parallel = thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)))
  const pipeline = (items, ...stages) => Promise.all(items.map(async (item, i) => {
    let v = item
    for (const s of stages) v = await s(v, item, i)
    return v
  }))
  const workflow = async () => { throw new Error('nesting is forbidden') }
  const fn = new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget',
    ...ABSENT, body)
  const result = await fn(args, agent, pipeline, parallel, () => {}, m => logs.push(String(m)), workflow, null,
    ...ABSENT.map(() => undefined))
  return { result, calls, logs, finished, labels: calls.map(c => c.label) }
}

const ONE = { dirs: ['src/portable/x'], dimensions: ['correctness'] }

test('meta names the workflow and two phases', async () => {
  await run(ONE, { scans: { 'src/portable/x|correctness': [] } })
  assert.equal(globalThis.__meta.name, 'code-audit')
  assert.deepEqual(globalThis.__meta.phases.map(p => p.title), ['Scan', 'Verify'])
})

test('invalid args are rejected before any agent runs', async () => {
  for (const args of [
    undefined, null, '{"dirs":["a"],"dimensions":["b"]}', {}, { dirs: ['a'] }, { dimensions: ['b'] },
    { dirs: [], dimensions: ['b'] }, { dirs: ['a'], dimensions: [] }, { dirs: 'a', dimensions: ['b'] },
    { dirs: ['a', ''], dimensions: ['b'] }, { dirs: ['a'], dimensions: ['  '] }, { dirs: ['a'], dimensions: [1] },
  ]) {
    await assert.rejects(run(args), /args/, JSON.stringify(args))
  }
})

test('every dir x dimension pair is scanned exactly once with a unique label, identical dir suffixes included', async () => {
  const args = { dirs: ['src/a/x', 'src/b/x'], dimensions: ['correctness', 'style'] }
  const scans = { 'src/a/x|correctness': [], 'src/a/x|style': [], 'src/b/x|correctness': [], 'src/b/x|style': [] }
  const { result, calls, labels, logs } = await run(args, { scans })
  assert.equal(calls.length, 4)
  assert.equal(new Set(labels).size, 4)
  assert.ok(calls.every(c => c.agentType === 'code-verifier'))
  assert.ok(calls.every(c => c.schema.required.includes('findings')))
  const seen = calls.map(c => /^Review (.+?) for exactly one dimension: (.+?)\./.exec(c.prompt).slice(1, 3).join('|')).sort()
  assert.deepEqual(seen, Object.keys(scans).sort())
  assert.match(calls[0].prompt, /Coverage-first: report everything, a verifier filters\.$/)
  assert.deepEqual(result, { confirmed: [], dropped: [], unverified: [] })
  assert.equal(logs[0], '4 scan units (2 dirs x 2 dimensions)')
})

test('mixed verdicts keep only confirmed findings, each verified once by finding-verifier, same-line findings included', async () => {
  const scans = { 'src/portable/x|correctness': [finding(10, 'real bug'), finding(20, 'false alarm'), finding(10, 'another real')] }
  const verdicts = { 'real bug': true, 'false alarm': false, 'another real': true }
  const { result, calls } = await run(ONE, { scans, verdicts })
  const verifies = calls.filter(c => c.label.startsWith('verify:'))
  assert.equal(verifies.length, 3)
  assert.equal(new Set(verifies.map(v => v.label)).size, 3)
  assert.ok(verifies.every(c => c.agentType === 'finding-verifier'))
  assert.ok(verifies.every(c => c.schema.required.includes('real')))
  assert.match(verifies[0].prompt, /^Adversarially verify ONE review finding about src\/portable\/x\.\nDimension: correctness\nFinding: \{/)
  assert.match(verifies[0].prompt, /Try to REFUTE it; real=true only if it survives your best attempt\.$/)
  assert.doesNotMatch(verifies[0].prompt, /ISR|datasheet|macros/)
  assert.deepEqual(result.dropped, [])
  assert.deepEqual(result.unverified, [])
  assert.deepEqual(result.confirmed, [{
    dir: 'src/portable/x', dim: 'correctness',
    findings: [
      { ...finding(10, 'real bug'), verdict: { real: true, reason: 'holds' } },
      { ...finding(10, 'another real'), verdict: { real: true, reason: 'holds' } },
    ],
  }])
})

test('a dead scanner is reported as dropped, a dead verifier as unverified, never as clean', async () => {
  const args = { dirs: ['src/a', 'src/b'], dimensions: ['correctness'] }
  const scans = { 'src/a|correctness': null, 'src/b|correctness': [finding(5, 'lost'), finding(6, 'kept')] }
  const { result, logs } = await run(args, { scans, verdicts: { lost: null, kept: true } })
  assert.deepEqual(result.dropped, [{ dir: 'src/a', dim: 'correctness' }])
  assert.deepEqual(result.unverified, [{ dir: 'src/b', dim: 'correctness', findings: [finding(5, 'lost')] }])
  assert.deepEqual(result.confirmed, [{ dir: 'src/b', dim: 'correctness', findings: [{ ...finding(6, 'kept'), verdict: { real: true, reason: 'holds' } }] }])
  assert.ok(logs.some(l => /1 scan unit\(s\) dropped/.test(l)), logs.join('\n'))
  assert.ok(logs.some(l => /src\/b: 1 finding\(s\) lost to dead verifiers/.test(l)), logs.join('\n'))
})

test('verdicts stay attached to their finding when verifiers finish in reverse order', async () => {
  const scans = { 'src/portable/x|correctness': [finding(1, 'first'), finding(2, 'second'), finding(3, 'third')] }
  const verdicts = { first: true, second: null, third: false }
  const { result, finished } = await run(ONE, { scans, verdicts, reverse: true })
  assert.deepEqual(finished, ['third', 'second', 'first'], 'the stub really completed them in reverse')
  assert.deepEqual(result.confirmed[0].findings.map(f => f.why), ['first'])
  assert.deepEqual(result.unverified[0].findings.map(f => f.why), ['second'])
})

test('diff pins every scan and verify unit to base..head of its own dir', async () => {
  const base = 'a'.repeat(40), head = 'b'.repeat(40)
  const scans = { 'src/portable/x|correctness': [finding(1, 'introduced')] }
  const { calls } = await run({ ...ONE, diff: { base, head } }, { scans, verdicts: { introduced: true } })
  for (const c of calls) {
    assert.ok(c.prompt.includes(`The checkout is at ${head}. Judge only what \`git diff ${base} ${head} -- src/portable/x\` introduces or breaks`.replace('-- src/portable/x', "-- ':(literal,top)src/portable/x'")), c.prompt)
  }
})

test('a diff without two full SHAs is rejected before any agent runs', async () => {
  for (const diff of [{}, { base: 'a'.repeat(40) }, { base: 'HEAD~1', head: 'b'.repeat(40) }, { base: 'a'.repeat(40), head: 'B'.repeat(40) }, { base: ['a'.repeat(40)], head: 'b'.repeat(40) }, 'main']) {
    await assert.rejects(run({ ...ONE, diff }), /args\.diff/, JSON.stringify(diff))
  }
})

test('a diff dir with spaces or quotes is one shell-quoted pathspec', async () => {
  const base = 'a'.repeat(40), head = 'b'.repeat(40)
  const { calls } = await run({ dirs: ["src/it's x"], dimensions: ['correctness'], diff: { base, head } }, { scans: { "src/it's x|correctness": [] } })
  assert.ok(calls[0].prompt.includes(`-- ':(literal,top)src/it'\\''s x'\``), calls[0].prompt)
})

test('the root group diffs the whole change, with no pathspec', async () => {
  const base = 'a'.repeat(40), head = 'b'.repeat(40)
  const { calls } = await run({ dirs: ['.'], dimensions: ['correctness'], diff: { base, head } }, { scans: { '.|correctness': [] } })
  assert.ok(calls[0].prompt.includes(`\`git diff ${base} ${head}\` introduces`), calls[0].prompt)
})

test('a dir that looks like pathspec magic stays a literal path', async () => {
  const base = 'a'.repeat(40), head = 'b'.repeat(40)
  const { calls } = await run({ dirs: [':(exclude)src'], dimensions: ['correctness'], diff: { base, head } }, { scans: { ':(exclude)src|correctness': [] } })
  assert.ok(calls[0].prompt.includes("-- ':(literal,top):(exclude)src'`"), calls[0].prompt)
})
