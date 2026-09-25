export const meta = {
  name: 'code-audit',
  description: 'Review directories across dimensions with one code-verifier per (dir x dimension), then adversarially verify every finding with finding-verifier; returns { confirmed, dropped, unverified } so a clean result is distinguishable from lost coverage',
  whenToUse: 'Auditing directories for a bug class (dimensions: [question]) or across several review dimensions; the dimension text carries any reference-document policy',
  phases: [
    { title: 'Scan', detail: 'code-verifier per (dir x dimension)' },
    { title: 'Verify', detail: 'adversarial refutation per finding' },
  ],
}

// args: { dirs: string[], dimensions: string[], diff?: { base, head } }, dirs and
// dimensions nonempty; no defaults, since `dimensions: []` would review nothing
// and look like a clean pass. `diff` (two full SHAs, the checkout at head) narrows
// every unit to what base..head introduces or breaks, as a PR review needs; the
// caller checks that the checkout is clean and at head before launching.
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
// Pinned SHAs, not HEAD: a checkout that moves under the run must not change what was reviewed.
const shellQuote = s => `'${s.replace(/'/g, `'\\''`)}'`
// A literal "." pathspec matches nothing, so the root group takes the whole diff.
const pathspec = dir => dir === '.' ? '' : ` -- ${shellQuote(`:(literal,top)${dir}`)}`
const scopeOf = dir => args.diff
  ? ` The checkout is at ${args.diff.head}. Judge only what \`git diff ${args.diff.base} ${args.diff.head}${pathspec(dir)}\` introduces or breaks, reading the surrounding code for context.`
  : ''

const FINDINGS = {
  type: 'object', additionalProperties: false,
  required: ['scope', 'dimension', 'findings'],
  properties: {
    scope: { type: 'string' }, dimension: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['file', 'line', 'snippet', 'why', 'severity', 'confidence'],
        properties: {
          file: { type: 'string' }, line: { type: 'integer' }, snippet: { type: 'string' },
          why: { type: 'string' }, severity: { type: 'string' }, confidence: { type: 'string' },
        },
      },
    },
  },
}
const VERDICT = {
  type: 'object', additionalProperties: false,
  required: ['real', 'reason'],
  properties: { real: { type: 'boolean' }, reason: { type: 'string' } },
}

const pairs = dirs.flatMap((dir, i) => dims.map((dim, j) => ({ dir, dim, id: `d${i}x${j}` })))
log(`${pairs.length} scan units (${dirs.length} dirs x ${dims.length} dimensions)`)

const results = await pipeline(
  pairs,

  p => agent(
    `Review ${p.dir} for exactly one dimension: ${p.dim}. Read the sources yourself.${scopeOf(p.dir)} Report each \`file\` relative to the repository root. Coverage-first: report everything, a verifier filters.`,
    { label: `scan:${p.id}`, phase: 'Scan', agentType: 'code-verifier', schema: FINDINGS },
  ),

  (scan, p) => {
    if (!scan) return { dir: p.dir, dim: p.dim, dropped: true, findings: [], unverified: [] }
    if (scan.findings.length === 0) return { dir: p.dir, dim: p.dim, findings: [], unverified: [] }
    return parallel(scan.findings.map((f, k) => () =>
      agent(
        `Adversarially verify ONE review finding about ${p.dir}.\nDimension: ${p.dim}\nFinding: ${JSON.stringify(f)}\n` +
        `Read the cited code and enough surrounding context to judge.${scopeOf(p.dir)} Try to REFUTE it; real=true only if it survives your best attempt.`,
        { label: `verify:${p.id}:${k}`, phase: 'Verify', agentType: 'finding-verifier', schema: VERDICT },
      ).then(v => v && { ...f, verdict: v })
    )).then(vs => {
      const unverified = scan.findings.filter((_, k) => !vs[k])
      if (unverified.length > 0) {
        log(`${p.dir}: ${unverified.length} finding(s) lost to dead verifiers — treat as unverified, re-run if needed`)
      }
      return { dir: p.dir, dim: p.dim, findings: vs.filter(x => x && x.verdict.real), unverified }
    })
  },
)

// Lost coverage travels with the findings: an empty `confirmed` only means
// "clean" when `dropped` and `unverified` are empty too.
const dropped = results.filter(r => r.dropped).map(r => ({ dir: r.dir, dim: r.dim }))
if (dropped.length > 0) log(`${dropped.length} scan unit(s) dropped (scanner died)`)
const unverified = results.filter(r => r.unverified.length > 0)
  .map(r => ({ dir: r.dir, dim: r.dim, findings: r.unverified }))
const confirmed = results.filter(r => r.findings.length > 0)
  .map(r => ({ dir: r.dir, dim: r.dim, findings: r.findings }))
log(`${confirmed.length} scan units produced confirmed findings`)
return { confirmed, dropped, unverified }
