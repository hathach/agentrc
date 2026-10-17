#!/usr/bin/env node
// Replay the corpus through code-audit's own verify stage, per finding or batched, on real verifiers.
//
//   replay.mjs --corpus corpus.jsonl --mode single|batch --out DIR --worktrees DIR
//              --checkout OWNER/REPO=PATH ... [--jobs 4] [--only ID,...]
//
// For each pinned head, workflows/code-audit.js runs as written, its scan units answered with the corpus
// findings of that dir and dimension (each file repository-relative, as #65 makes them) and every verify
// dispatch sent to `claude -p --agent finding-verifier` in a worktree of that head, with the model and effort
// code-audit asks for, or the role's own. Truth labels never reach a verifier.
//
// Before anything is spent: DIR must not exist; every head and base must resolve in its checkout; each head
// gets a clean detached worktree under --worktrees (created once, reused); no two cases of one scan unit may
// share a file, line and claim, which would make their verdicts indistinguishable. DIR then gets
// calls.jsonl (every dispatch: its prompt, schema, case ids, model, effort, cost, duration and structured
// reply), verdicts.jsonl (each case's final kind, confirmed, refuted or unverified, with the dispatch whose
// verdict it rests on) and env.json (digests of what the verifiers ran under, before and after; score.py
// refuses a pair whose environments differ). Heads replay concurrently under the one --jobs limit. A worktree
// found changed after its head is replayed fails the run: no dispatch starts after a failure, running ones are
// terminated, and every head settles, its calls recorded, before the run exits. REPLAY_CLAUDE, a JSON array, is
// the command run in place of `claude` (the tests' stand-in).
import { spawn, execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync, appendFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const AGENTRC = resolve(HERE, '..', '..')
const WORKFLOW = join(AGENTRC, 'workflows', 'code-audit.js')
const HARDWARE = /datasheet|errat|reference manual|user manual|\bregister|W1C|write-1-to-clear|silicon|read-doc|\b(?:UM|RM|AN|DS)\d{4,}/i
const ROLE = { model: 'opus', effort: 'high' } // agents/finding-verifier.md's own, which a dispatch without overrides gets

const fail = (msg) => { console.error(`replay.mjs: ${msg}`); process.exit(2) }
const USAGE = 'usage: --corpus FILE --mode single|batch --out DIR --worktrees DIR --checkout OWNER/REPO=PATH ...'
let opt
try {
  opt = parseArgs({ options: { corpus: { type: 'string' }, mode: { type: 'string' }, out: { type: 'string' }, worktrees: { type: 'string' },
    checkout: { type: 'string', multiple: true, default: [] }, jobs: { type: 'string', default: '4' }, only: { type: 'string' } } }).values
} catch (e) { fail(`${e.message}\n${USAGE}`) }
const mode = opt.mode
if (!opt.corpus || !['single', 'batch'].includes(mode) || !opt.out || !opt.worktrees) fail(USAGE)
const corpusFile = resolve(opt.corpus), out = resolve(opt.out), worktrees = resolve(opt.worktrees)
const checkouts = Object.fromEntries(opt.checkout.map(c => {
  const i = c.indexOf('=')
  if (i < 1) fail(`--checkout ${c}: OWNER/REPO=PATH`)
  return [c.slice(0, i), resolve(c.slice(i + 1))]
}))
const jobs = Number(opt.jobs)
if (!Number.isInteger(jobs) || jobs < 1) fail(`--jobs must be a positive integer, not ${opt.jobs}`)
const only = opt.only ? new Set(opt.only.split(',')) : null
if (existsSync(out)) fail(`${out} exists: a run writes a new directory, so no two experiments share one`)
let CLAUDE = ['claude']
if (process.env.REPLAY_CLAUDE !== undefined) {  // set but empty is a mistake, not a request for the real claude
  try { CLAUDE = JSON.parse(process.env.REPLAY_CLAUDE) } catch { CLAUDE = null }
  if (!Array.isArray(CLAUDE) || !CLAUDE.length || !CLAUDE.every(a => typeof a === 'string' && a)) fail('REPLAY_CLAUDE must be a JSON array of nonempty strings')
}

const corpusText = readFileSync(corpusFile, 'utf8')
const cases = corpusText.split('\n').filter(Boolean).map(JSON.parse).filter(c => !only || only.has(c.id))
if (only && cases.length !== only.size) fail(`--only names ids not in the corpus: ${[...only].filter(i => !cases.some(c => c.id === i)).join(', ')}`)
const missing = [...new Set(cases.map(c => c.repo))].filter(r => !checkouts[r])
if (missing.length) fail(`no --checkout for ${missing.join(', ')}`)
const twins = new Map()
for (const c of cases) {
  const key = JSON.stringify([c.repo, c.head, c.dir, c.dimension, c.path, c.finding.line, c.finding.why])
  if (twins.has(key)) fail(`cases ${twins.get(key)} and ${c.id} share a scan unit, file, line and claim: their verdicts cannot be told apart`)
  twins.set(key, c.id)
}

// Batch routing from evidence independent of the sampling regex: an adjudicator that needed a document or a board
// marks it hardware, only all of them needing nothing and a clean regex mark it not, anything else is unsure.
const routing = (c) => {
  const needs = Object.values(c.truth.answers).map(a => a.needs)
  if (HARDWARE.test(`${c.finding.why}\n${c.finding.snippet || ''}`) || needs.some(n => n === 'document' || n === 'hardware')) return true
  return needs.length && needs.every(n => n === 'none') ? false : null
}

const git = (cwd, ...a) => execFileSync('git', ['-C', cwd, ...a], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim()
const resolves = (cwd, rev) => { try { git(cwd, 'cat-file', '-e', `${rev}^{commit}`); return true } catch { return false } }
const groups = new Map()
for (const c of cases) {
  const key = JSON.stringify([c.repo, c.head, c.base])
  groups.set(key, [...(groups.get(key) || []), c])
}
// Every commit and worktree is ready before the first paid call.
const trees = new Map()
for (const key of groups.keys()) {
  const [repo, head, base] = JSON.parse(key)
  for (const rev of [head, base].filter(Boolean)) {
    if (resolves(checkouts[repo], rev)) continue
    try { git(checkouts[repo], 'fetch', '--quiet', 'origin', rev) } catch { /* reported below */ }
    if (!resolves(checkouts[repo], rev)) fail(`${repo} ${rev}: not in ${checkouts[repo]}, and fetching it failed`)
  }
  const dir = join(worktrees, `${repo.replace('/', '-')}-${head.slice(0, 12)}`)
  if (!existsSync(dir)) { mkdirSync(worktrees, { recursive: true }); git(checkouts[repo], 'worktree', 'add', '--quiet', '--detach', dir, head) }
  trees.set(key, realpathSync(dir))
}
const clean = (dir, head) => git(dir, 'rev-parse', 'HEAD') === head && git(dir, 'status', '--porcelain') === ''
for (const [key, dir] of trees) if (!clean(dir, JSON.parse(key)[1])) fail(`${dir} is not a clean checkout of ${JSON.parse(key)[1]}`)

const sha256 = (data) => createHash('sha256').update(data).digest('hex')
const fileDigest = (p) => existsSync(p) ? sha256(readFileSync(realpathSync(p))) : null
// Names are `/`-separated on every host, so env.json compares across them.
const treeDigest = (root) => {
  if (!existsSync(root)) return null
  const h = createHash('sha256')
  const walk = (dir) => {
    for (const name of readdirSync(dir).sort()) {
      if (name === '__pycache__' || name === '.git') continue
      const p = join(dir, name)
      const st = statSync(p)
      if (st.isDirectory()) walk(p)
      else h.update(`${relative(root, p).split(sep).join('/')}\0${st.size}\0${st.size < 1 << 20 ? sha256(readFileSync(p)) : ''}\n`)
    }
  }
  walk(realpathSync(root))
  return h.digest('hex')
}
const env = () => ({
  workflow: fileDigest(WORKFLOW), replay: fileDigest(fileURLToPath(import.meta.url)), corpus: sha256(corpusText),
  cases: cases.map(c => c.id).sort(),
  claude: execFileSync(CLAUDE[0], [...CLAUDE.slice(1), '--version'], { encoding: 'utf8' }).trim(),
  userInstructions: fileDigest(join(homedir(), '.claude', 'CLAUDE.md')),
  role: fileDigest(join(homedir(), '.claude', 'agents', 'finding-verifier.md')),
  settings: fileDigest(join(homedir(), '.claude', 'settings.json')),
  skills: treeDigest(join(homedir(), '.claude', 'skills')),
  heads: Object.fromEntries([...groups.keys()].map(key => {
    const [repo, head] = JSON.parse(key)
    const obj = (p) => { try { return git(checkouts[repo], 'rev-parse', `${head}:${p}`) } catch { return null } }
    return [`${repo}@${head}`, { instructions: obj('CLAUDE.md'), dotClaude: obj('.claude') }]
  })),
})

let aborted = null
const children = new Set()
let running = 0
const waiting = []
const slot = () => running < jobs ? (running++, Promise.resolve()) : new Promise(r => waiting.push(r))
const release = () => { const next = waiting.shift(); if (next) next(); else running-- }
const claude = async (cwd, prompt, schema, model, effort) => {
  await slot()
  const started = Date.now()
  try {
    if (aborted) throw new Error(`not dispatched: ${aborted.message}`)
    return await new Promise((done) => {
      const p = spawn(CLAUDE[0], [...CLAUDE.slice(1), '-p', '--agent', 'finding-verifier', '--model', model, '--effort', effort, '--output-format', 'json',
        '--json-schema', JSON.stringify(schema), '--permission-mode', 'bypassPermissions', '--max-budget-usd', '10'], { cwd, stdio: ['pipe', 'pipe', 'pipe'] })
      children.add(p)
      let stdout = '', stderr = ''
      p.stdout.on('data', d => { stdout += d })
      p.stderr.on('data', d => { stderr += d })
      p.on('close', (code) => {
        children.delete(p)
        let r = null
        try { r = JSON.parse(stdout) } catch { /* reported as the error */ }
        done({ code, durationMs: Date.now() - started, result: r, stderr: r ? '' : (stderr || stdout).slice(-2000) })
      })
      p.stdin.end(prompt)
    })
  } finally { release() }
}

// The subset of JSON Schema code-audit's verdicts use: type, enum, required, properties, items, additionalProperties.
const conforms = (v, sch) => {
  const types = [].concat(sch.type || [])
  const typeOf = x => x === null ? 'null' : Array.isArray(x) ? 'array' : Number.isInteger(x) ? 'integer' : typeof x
  if (types.length && !types.some(t => t === typeOf(v) || (t === 'number' && typeof v === 'number'))) return false
  if (sch.enum && !sch.enum.includes(v)) return false
  if (typeOf(v) === 'array') return !sch.items || v.every(x => conforms(x, sch.items))
  if (typeOf(v) !== 'object') return true
  const props = sch.properties || {}
  return (sch.required || []).every(k => k in v) && Object.entries(v).every(([k, x]) => k in props ? conforms(x, props[k]) : sch.additionalProperties !== false)
}
// With --agent the CLI returns the reply as the result text rather than structured_output: parse it and hold it to the schema.
const replyOf = (r, schema) => {
  if (!r || r.is_error) return null
  let v = r.structured_output
  if (v === undefined) { try { v = JSON.parse(String(r.result).trim()) } catch { return null } }
  return conforms(v, schema) ? v : null
}

const body = readFileSync(WORKFLOW, 'utf8').replace(/^export const meta = /m, 'const meta = ')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
// usable and whole below are an independent copy of code-audit's decoding, not an import: a replay agreeing with
// the workflow by sharing its code would check nothing.
const usable = (v) => !!v && (v.real === false || (v.real === true && !!v.severity && !!v.confidence && !!v.severityReason && !!v.impact &&
  ['consequence', 'path', 'variants', 'recovery'].every(k => v.impact[k])))

mkdirSync(out, { recursive: true })
const envBefore = env()
const callsFile = join(out, 'calls.jsonl')
const abort = (msg) => { throw new Error(msg) }
const replayHead = async (key, group) => {
  const [, head, base] = JSON.parse(key)
  const cwd = trees.get(key)
  const units = new Map() // scan unit id -> its cases, in the order the stub returned their findings
  const attempts = new Map(group.map(c => [c.id, []])) // case id -> [{ label, verdict }] in the order they came back
  const dirs = [...new Set(group.map(c => c.dir))]
  const dims = [...new Set(group.map(c => c.dimension))]
  const agent = async (prompt, o) => {
    if (o.label.startsWith('scan:')) {
      // scan:d<i>x<j> is dirs[i] by dims[j], as code-audit numbers its units
      const [, i, j] = /^scan:d(\d+)x(\d+)$/.exec(o.label)
      const dir = dirs[Number(i)], dim = dims[Number(j)]
      const mine = group.filter(c => c.dir === dir && c.dimension === dim).sort((a, b) => a.finding.line - b.finding.line || (a.id < b.id ? -1 : 1))
      units.set(o.label.slice('scan:'.length), mine)
      return { scope: dir, dimension: dim, findings: mine.map(c => ({ ...c.finding, file: c.path, hardware: routing(c) })) }
    }
    const ids = o.label.startsWith('verify:b')
      ? o.schema.properties.verdicts.items.properties.id.enum
      : [o.label.slice('verify:'.length).replace(/\.retry$/, '')]
    const caseIds = ids.map(id => { const [u, k] = id.split(':'); return units.get(u)[Number(k)].id })
    const model = o.model || ROLE.model, effort = o.effort || ROLE.effort
    const r = await claude(cwd, prompt, o.schema, model, effort)
    const structured = replyOf(r.result, o.schema)
    appendFileSync(callsFile, JSON.stringify({ mode, label: o.label, head, caseIds, model, effort, exit: r.code, durationMs: r.durationMs,
      cost: r.result ? r.result.total_cost_usd ?? null : null, models: r.result ? Object.keys(r.result.modelUsage || {}) : [],
      prompt, schema: o.schema, structured, error: structured ? null : r.result ? `${r.result.subtype}: ${String(r.result.result).slice(0, 2000)}` : r.stderr }) + '\n')
    // A batch reply code-audit does not trust whole (one entry per id) contributes no verdict, as there.
    const entries = structured && Array.isArray(structured.verdicts) ? structured.verdicts : null
    const whole = entries && entries.length === ids.length && new Set(entries.map(e => e.id)).size === ids.length && entries.every(e => ids.includes(e.id))
    caseIds.forEach((id, k) => {
      const v = !structured ? null : o.label.startsWith('verify:b') ? (whole ? entries.find(e => e.id === ids[k]) : null) : structured
      attempts.get(id).push({ label: o.label, verdict: v })
    })
    return structured
  }
  const parallel = thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)))
  const pipeline = (items, ...stages) => Promise.all(items.map(async (item, i) => {
    let v = item
    for (const s of stages) { try { v = await s(v, item, i) } catch { return null } }
    return v
  }))
  const args = { dirs, dimensions: dims, ...(base ? { diff: { base, head } } : {}), root: cwd, batch: mode === 'batch' }
  const fn = new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget', body)
  const result = await fn(args, agent, pipeline, parallel, () => {}, m => console.log(`${head.slice(0, 8)} ${m}`),
    async () => { throw new Error('nesting') }, null)
  writeFileSync(join(out, `result-${head.slice(0, 12)}.json`), JSON.stringify(result, null, 1))
  if (!clean(cwd, head)) abort(`${cwd} changed while ${head} was replayed`)
  if (result.dropped.length) abort(`${head}: scan units dropped ${JSON.stringify(result.dropped)}`)
  // code-audit reports confirmed and unverified findings by content: place each on its one case or fail.
  const kindOf = new Map()
  const place = (u, f, kind) => {
    const c = group.filter(x => x.dir === u.dir && x.dimension === u.dim && x.path === f.file && x.finding.line === f.line && x.finding.why === f.why)
    if (c.length !== 1 || kindOf.has(c[0].id)) abort(`${head}: ${kind} ${f.file}:${f.line} matches ${c.length} case(s)`)
    kindOf.set(c[0].id, kind)
  }
  for (const u of result.confirmed) for (const f of u.findings) place(u, f, 'confirmed')
  for (const u of result.unverified) for (const f of u.findings) place(u, f, 'unverified')
  return group.map(c => {
    const tried = attempts.get(c.id)
    if (!tried.length) abort(`${c.id}: never sent to a verifier`)
    const kind = kindOf.get(c.id) ?? 'refuted'
    // The verdict a report rests on is the last usable one, as code-audit keeps; a kind it contradicts is a replay fault.
    const kept = [...tried].reverse().find(t => usable(t.verdict)) || null
    const agrees = kind === 'unverified' ? !kept : kept && kept.verdict.real === (kind === 'confirmed')
    if (!agrees) abort(`${c.id}: code-audit reports it ${kind}, its verdicts say ${JSON.stringify(kept && kept.verdict)}`)
    return { id: c.id, mode, kind, from: kept ? kept.label : null, verdict: kept ? kept.verdict : null, attempts: tried.map(t => t.label),
      routing: { hardware: routing(c), source: 'adjudicators+regex' } }
  })
}
const settled = await Promise.allSettled([...groups].map(([key, group]) => replayHead(key, group).catch((e) => {
  if (!aborted) { aborted = e; for (const p of children) p.kill('SIGTERM') }
  throw e
})))
if (aborted) fail(aborted.message)
const verdicts = settled.flatMap(r => r.value)
writeFileSync(join(out, 'verdicts.jsonl'), verdicts.map(v => JSON.stringify(v)).join('\n') + '\n')
writeFileSync(join(out, 'env.json'), JSON.stringify({ before: envBefore, after: env() }, null, 1))
console.log(`${verdicts.length} case(s) -> ${out}`)
