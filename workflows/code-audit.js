export const meta = {
  name: 'code-audit',
  description: 'Review directories across dimensions with one code-verifier per (dir x dimension), then adversarially verify every finding with finding-verifier; returns { confirmed, dropped, unverified } so a clean result is distinguishable from lost coverage',
  whenToUse: 'Auditing directories for a bug class (dimensions: [question]) or across several review dimensions; the dimension text carries any reference-document policy',
  phases: [
    { title: 'Scan', detail: 'code-verifier per (dir x dimension)' },
    { title: 'Verify', detail: 'adversarial refutation per finding' },
  ],
}

// args: { dirs: string[], dimensions: string[], diff?: { base, head }, root?: string, batch?: boolean }, dirs and
// dimensions nonempty; no defaults, since `dimensions: []` would review nothing
// and look like a clean pass. `diff` (two full SHAs, the checkout at head) narrows
// every unit to what base..head introduces or breaks, as a PR review needs; the
// caller checks that the checkout is clean and at head before launching. `root`, the
// checkout's absolute top, turns a scanner's absolute path under it into a relative one. `batch`
// verifies close findings on one file in one dispatch once every scan is in (see Batching); it stays off
// by default until it passes bench/finding-verify's adoption gate.
const list = (v, name) => {
  if (!Array.isArray(v) || v.length === 0 || !v.every(s => typeof s === 'string' && s.trim())) {
    throw new Error(`args.${name} must be a nonempty array of nonblank strings; args is { dirs: string[], dimensions: string[] }`)
  }
  return v.map(s => s.trim())
}
if (!args || typeof args !== 'object') throw new Error('args must be { dirs: string[], dimensions: string[] }')
const dirs = list(args.dirs, 'dirs')
const dims = list(args.dimensions, 'dimensions')
const SHA = /^[0-9a-f]{40}$/
const isSha = v => typeof v === 'string' && SHA.test(v)
if (args.diff != null && !(isSha(args.diff.base) && isSha(args.diff.head))) {
  throw new Error('args.diff must be { base, head }, two full 40-hex SHAs')
}
// Every `\` is a separator, on POSIX too (this runtime cannot tell the host): a Windows path, which a scanner may
// report with either slash and drive case, compares in one form, and `\x`, `\\srv\share` or `C:x` is absolute.
const DRIVE = /^[A-Za-z]:/
const slashed = p => p.replace(/\\/g, '/').replace(/^[a-z](?=:)/, c => c.toUpperCase())
if (args.root != null && !(typeof args.root === 'string' && /^(?:\/.|[A-Za-z]:[\\/])/.test(args.root))) throw new Error('args.root must be an absolute path')
if (args.batch != null && typeof args.batch !== 'boolean') throw new Error('args.batch must be a boolean')
const root = args.root == null ? null : slashed(args.root).replace(/\/+$/, '')
// A drive path's case is not its identity: the checkout's real path and a scanner's spelling may differ in it.
const under = p => root && (DRIVE.test(root) ? p.slice(0, root.length + 1).toLowerCase() === `${root}/`.toLowerCase() : p.startsWith(`${root}/`))
// A verifier reads the path it is given, and a finding's consumers need it repository-relative. Strings only:
// root is the checkout's real path, and a scanner reports the path it read.
const placed = (f) => {
  const ff = slashed(f.file)
  const file = under(ff) ? ff.slice(root.length + 1) : ff.replace(/^(?:\.\/)+/, '')
  const pathError = !file ? 'no file'
    : file.startsWith('/') || DRIVE.test(file) ? `absolute path outside the checkout${root ? ` ${root}` : ' (no root given)'}`
    : file.split('/').includes('..') ? 'path escapes the checkout' : null
  return pathError ? { ...f, pathError } : { ...f, file }
}
// Pinned SHAs, not HEAD: a checkout that moves under the run must not change what was reviewed.
const shellQuote = s => `'${s.replace(/'/g, `'\\''`)}'`
// A literal "." pathspec matches nothing, so the root group takes the whole diff.
const pathspec = dir => dir === '.' ? '' : ` -- ${shellQuote(`:(literal,top)${dir}`)}`
const scopeOf = dir => args.diff
  ? ` The checkout is at ${args.diff.head}. Judge only what \`git diff ${args.diff.base} ${args.diff.head}${pathspec(dir)}\` introduces or breaks, reading the surrounding code for context.`
  : ''

// The one scale, defined in finding-verifier.md's Severity section.
const LEVELS = ['critical', 'high', 'medium', 'low', 'nit']
const CONFIDENCE = ['high', 'medium', 'low']
// Optional: the read-doc sources a scanner consulted (Calibre book id, PDF pages, logged lookup ids).
const DOCS = {
  type: 'array',
  items: {
    type: 'object', additionalProperties: false, required: ['book', 'pages'],
    properties: { book: { type: 'integer' }, pages: { type: 'string' }, lookups: { type: 'array', items: { type: 'string' } } },
  },
}
const FINDINGS = {
  type: 'object', additionalProperties: false,
  required: ['scope', 'dimension', 'findings'],
  properties: {
    scope: { type: 'string' }, dimension: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['file', 'line', 'snippet', 'why', 'severity', 'confidence', 'hardware'],
        properties: {
          file: { type: 'string' }, line: { type: 'integer' }, snippet: { type: 'string' },
          why: { type: 'string' }, severity: { enum: LEVELS }, confidence: { enum: CONFIDENCE },
          hardware: { type: ['boolean', 'null'] },
          docs: DOCS,
        },
      },
    },
  },
}
const IMPACT = {
  type: ['object', 'null'], additionalProperties: false,
  required: ['consequence', 'path', 'variants', 'recovery'],
  properties: { consequence: { type: 'string' }, path: { type: 'string' }, variants: { type: 'string' }, recovery: { type: 'string' } },
}
const VERDICT = {
  type: 'object', additionalProperties: false,
  required: ['real', 'reason', 'severity', 'impact', 'severityReason', 'confidence'],
  properties: {
    real: { type: 'boolean' }, reason: { type: 'string' },
    severity: { enum: [...LEVELS, null] }, impact: IMPACT,
    severityReason: { type: ['string', 'null'] }, confidence: { enum: [...CONFIDENCE, null] },
  },
}
// A confirmed finding without its level and the facts behind it is not a verified one.
const graded = v => LEVELS.includes(v.severity) && CONFIDENCE.includes(v.confidence) &&
  !!v.severityReason && !!v.impact && IMPACT.required.every(k => v.impact[k])
const gradeOf = v => ({ severity: v.severity, confidence: v.confidence, impact: v.impact, severityReason: v.severityReason })

// Owner's policy: a finding the scanner labels nit needs no Opus verification.
const verifierModel = f => f.severity === 'nit' ? { model: 'sonnet', effort: 'medium' } : {}

const GRADING = "If real, set severity, impact, severityReason and confidence by the Severity section of your role; the finding's own severity is the scanner's guess. If refuted, set them null."
const verifyPrompt = (p, f) =>
  `Adversarially verify ONE review finding about ${p.dir}.\nDimension: ${p.dim}\nFinding: ${JSON.stringify(f)}\n` +
  `Read the cited code and enough surrounding context to judge.${scopeOf(p.dir)} Try to REFUTE it; real=true only if it survives your best attempt. ` +
  GRADING
// A dead or partly graded verdict gets one more verifier; what is still short after it is unverified.
const usable = v => !!v && (!v.real || graded(v))
const verifyOne = (p, f, label) => agent(verifyPrompt(p, f), { label, phase: 'Verify', agentType: 'finding-verifier', schema: VERDICT, ...verifierModel(f) })
const verifyAgain = (p, f, label, v) => usable(v) ? v : verifyOne(p, f, `${label}.retry`).then(w => w || v)
// A finding verified alone, with its one retry: { v } or null.
const verifyChecked = (p, f, label) => verifyOne(p, f, label).then(v => verifyAgain(p, f, label, v)).then(v => v && { v })

// One scan unit's findings and their verdicts (x: { v } a verdict, null a dead verifier, { pathError }) as the unit's result.
const unitResult = (p, found, xs) => {
  const kind = x => !x || !x.v ? (x && x.pathError ? 'unplaced' : 'dead') : !x.v.real ? 'refuted' : graded(x.v) ? 'confirmed' : 'ungraded'
  const kinds = xs.map(kind)
  const count = k => kinds.filter(x => x === k).length
  const unverified = found.filter((_, k) => ['dead', 'ungraded', 'unplaced'].includes(kinds[k]))
  if (unverified.length > 0) {
    log(`${p.dir}: ${unverified.length} finding(s) unverified (${count('dead')} dead verifier(s), ${count('ungraded')} confirmed without complete grading${count('unplaced') ? `, ${count('unplaced')} with an unusable path` : ''}) — re-run if needed`)
  }
  const findings = found.flatMap((f, k) => kinds[k] === 'confirmed' ? [{ ...f, ...gradeOf(xs[k].v), verdict: { real: true, reason: xs[k].v.reason } }] : [])
  return { dir: p.dir, dim: p.dim, findings, unverified }
}

const pairs = dirs.flatMap((dir, i) => dims.map((dim, j) => ({ dir, dim, id: `d${i}x${j}` })))
log(`${pairs.length} scan units (${dirs.length} dirs x ${dims.length} dimensions)`)
const scanOf = p => agent(
  `Review ${p.dir} for exactly one dimension: ${p.dim}. Read the sources yourself.${scopeOf(p.dir)} Report each \`file\` relative to the repository root. ` +
  'Set each finding\'s `hardware` true when its conclusion depends on how hardware or a protocol behaves, false when the code alone decides it, null when unsure. ' +
  'Coverage-first: report everything, a verifier filters.',
  { label: `scan:${p.id}`, phase: 'Scan', agentType: 'code-verifier', schema: FINDINGS },
)
const placedOf = scan => !scan ? null : scan.findings.map(placed)
const emptyUnit = (p, found) => ({ dir: p.dir, dim: p.dim, ...(found ? {} : { dropped: true }), findings: [], unverified: [] })

// Batching: findings on one file within SPAN lines of the previous one, at most BATCH of them and PROMPT_CAP characters
// of finding text, verified in one dispatch on one model. A finding whose conclusion may rest on hardware or a document
// stays alone: the scanner's own flag, its cited documents, its text or its dimension says so, and unsure counts.
const SPAN = 15
const BATCH = 4
const PROMPT_CAP = 8000
const HARDWARE = /datasheet|errat|reference manual|user manual|\bregister|W1C|write-1-to-clear|silicon|read-doc|\b(?:UM|RM|AN|DS)\d{4,}/i
const HARDWARE_DIMENSION = /hardware|datasheet|errat|register|protocol|reference manual/i
const batchable = (f, dim) => f.hardware === false && !(f.docs && f.docs.length) && !HARDWARE.test(`${f.why}\n${f.snippet}`) && !HARDWARE_DIMENSION.test(dim)
const batchPrompt = (members) =>
  `Adversarially verify EACH of these ${members.length} review findings on one file. Judge each on its own evidence: ` +
  'one finding\'s verdict is no evidence for another\'s, and two findings on one line may differ.\n\n' +
  members.map(x => `[${x.id}] about ${x.p.dir}.\nDimension: ${x.p.dim}\nFinding: ${JSON.stringify(x.f)}${scopeOf(x.p.dir) && `\n${scopeOf(x.p.dir).trim()}`}`).join('\n\n') +
  '\n\nRead the cited code and enough surrounding context to judge each. Try to REFUTE each; real=true only if it survives your best attempt. ' +
  `${GRADING} Return verdicts with exactly one entry per id above.`
const batchSchema = (ids) => ({
  type: 'object', additionalProperties: false, required: ['verdicts'],
  properties: { verdicts: { type: 'array', items: { ...VERDICT, required: ['id', ...VERDICT.required], properties: { id: { enum: ids }, ...VERDICT.properties } } } },
})
const batchesOf = (items) => {
  const by = new Map()
  for (const x of items) {
    const key = JSON.stringify([x.f.file, verifierModel(x.f)])
    by.set(key, [...(by.get(key) || []), x])
  }
  const out = []
  for (const group of by.values()) {
    group.sort((a, b) => a.f.line - b.f.line)
    let cur = []
    const size = xs => xs.reduce((n, x) => n + JSON.stringify(x.f).length, 0)
    for (const x of group) {
      if (cur.length && (x.f.line - cur[cur.length - 1].f.line > SPAN || cur.length === BATCH || size([...cur, x]) > PROMPT_CAP)) { out.push(cur); cur = [] }
      cur.push(x)
    }
    out.push(cur)
  }
  return out
}
// One verdict per id or none trusted: a dead, unknown, duplicate or missing entry sends every member to its own
// verifier once; a confirmed entry without its grading sends that member alone.
const verifyBatch = async (members, b) => {
  const ids = members.map(x => x.id)
  const got = await agent(batchPrompt(members), { label: `verify:b${b}`, phase: 'Verify', agentType: 'finding-verifier', schema: batchSchema(ids), ...verifierModel(members[0].f) })
  const entries = got && Array.isArray(got.verdicts) ? got.verdicts : null
  const whole = entries && entries.length === ids.length && new Set(entries.map(e => e.id)).size === ids.length && entries.every(e => ids.includes(e.id))
  if (!whole) log(`verify:b${b}: ${got ? 'not one verdict per finding' : 'dead'} — ${ids.length} finding(s) verified one by one`)
  return parallel(members.map(x => () => {
    const e = whole ? entries.find(e => e.id === x.id) : null
    const { id, ...v } = e || {}
    return e && usable(v) ? { v } : verifyOne(x.p, x.f, `verify:${x.id}`).then(w => w ? { v: w } : (e ? { v } : null))
  }))
}

const results = !args.batch
  ? await pipeline(pairs, scanOf, (scan, p) => {
    const found = placedOf(scan)
    if (!found || !found.length) return emptyUnit(p, found)
    return parallel(found.map((f, k) => () => f.pathError ? { pathError: f.pathError } : verifyChecked(p, f, `verify:${p.id}:${k}`)))
      .then(xs => unitResult(p, found, xs))
  })
  : await (async () => {
    // Clusters cross scan units, so a batchable finding waits for every scan; any other is verified as its scan returns.
    const verdict = new Map()
    const alone = x => verifyChecked(x.p, x.f, `verify:${x.id}`).then(v => verdict.set(x.id, v))
    const itemsOf = (p, found) => (found || []).map((f, k) => ({ p, f, k, id: `${p.id}:${k}` }))
    const early = []
    const founds = await parallel(pairs.map(p => () => scanOf(p).then(scan => {
      const found = placedOf(scan)
      for (const x of itemsOf(p, found)) {
        if (x.f.pathError) verdict.set(x.id, { pathError: x.f.pathError })
        else if (!batchable(x.f, p.dim)) early.push(alone(x).catch(() => verdict.set(x.id, null)))
      }
      return found
    })))
    const open = pairs.flatMap((p, i) => itemsOf(p, founds[i])).filter(x => !x.f.pathError && batchable(x.f, x.p.dim))
    const batches = batchesOf(open).filter(b => b.length > 1)
    const batched = new Set(batches.flat().map(x => x.id))
    log(`${early.length + open.length} finding(s) to verify: ${batched.size} in ${batches.length} batch(es), ${early.length + open.length - batched.size} alone`)
    await parallel([
      ...batches.map((b, n) => () => verifyBatch(b, n).then(xs => b.forEach((x, k) => verdict.set(x.id, xs[k])))),
      ...open.filter(x => !batched.has(x.id)).map(x => () => alone(x)),
    ])
    await Promise.all(early)
    return pairs.map((p, i) => !founds[i] || !founds[i].length ? emptyUnit(p, founds[i])
      : unitResult(p, founds[i], founds[i].map((_, k) => verdict.get(`${p.id}:${k}`) || null)))
  })()

// Lost coverage travels with the findings: an empty `confirmed` only means
// "clean" when `dropped` and `unverified` are empty too.
const dropped = results.filter(r => r.dropped).map(r => ({ dir: r.dir, dim: r.dim }))
if (dropped.length > 0) log(`${dropped.length} scan unit(s) dropped (scanner died)`)
const unverified = results.filter(r => r.unverified.length > 0)
  .map(r => ({ dir: r.dir, dim: r.dim, findings: r.unverified }))
const confirmed = results.filter(r => r.findings.length > 0)
  .map(r => ({ dir: r.dir, dim: r.dim, findings: r.findings }))
// F<n> names a finding within this one report: assigned after verification, so a
// refuted claim takes no number, in unit order and then file and line.
const byPlace = (a, b) => a.file.localeCompare(b.file) || a.line - b.line
let n = 0
for (const r of results) {
  r.findings.sort(byPlace)
  r.unverified.sort(byPlace)
  for (const f of [...r.findings, ...r.unverified].sort(byPlace)) f.id = `F${++n}`
}
log(`${confirmed.length} scan units produced confirmed findings`)
return { confirmed, dropped, unverified }
