import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test as nodeTest } from 'node:test'

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
// The body runs as the runtime runs it; `meta` is exposed so its value, not its spelling, is checked.
const body = readFileSync(new URL('../workflows/pr-babysit.js', import.meta.url), 'utf8')
  .replace(/^export const meta = /m, 'const meta = globalThis.__meta = ')

// Node gives the workflow body globals the runtime sandbox does not, so a test
// run here is more forgiving than production: `new URL` cost this workflow every
// run, refusing each one at preflight, while the suite stayed green. Shadowing
// them as parameters makes the body fail here the way it fails there.
const ABSENT = ['URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'Buffer', 'process', 'fetch', 'structuredClone']

const GREEN = { status: 'green', infraRerun: [], realFailures: [] }
const finding = (over = {}) => {
  const f = {
    source: 'coderabbit', commentId: 1, file: 'src/a.c', line: 1,
    claim: 'bad', verdict: 'valid', reason: '', fixHint: 'fix it', ...over,
  }
  return { findingId: `${f.commentId}#${f.line}`, commentDigest: `d${f.commentId}`, ...f } // overridable
}
const invalidFinding = (over = {}) => finding({ verdict: 'invalid', ...over })
const oneValid = { findings: [finding()], replies: [], bots: 'reviewed' }
const SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
// The validator's clock: each dispatch observes one minute (or opts.minutesPerCycle)
// later than the previous one, starting here.
const T0 = Date.parse('2026-09-16T10:00:00Z')
const at = (minutes) => new Date(T0 + minutes * 60000).toISOString()
// A bot record as the validator reports one; `sha: 'head'` is filled with the
// stub checkout's current head by run().
const bot = (name, over = {}) => ({ bot: name, state: 'reviewed', kind: null, sha: 'head', evidence: ['stub'], reason: '', ...over })
// reply.py's checksum, so a receipt can be built for a body the workflow chose
const fnv1a = (text) => {
  let h = 0x811c9dc5
  for (const ch of text) h = Math.imul(h ^ ch.codePointAt(0), 0x01000193) >>> 0
  return h.toString(16).padStart(8, '0')
}
// pr-babysit's state seal, so a hand-built state is one a launch could have returned
const canonical = (v) => Array.isArray(v) ? `[${v.map(canonical).join(',')}]`
  : v && typeof v === 'object' ? `{${Object.keys(v).sort().map(k => `${JSON.stringify(k)}:${canonical(v[k])}`).join(',')}}`
  : JSON.stringify(v)
const seal = ({ digest, ...st }) => ({ ...st, digest: fnv1a(canonical(JSON.parse(JSON.stringify(st)))) })
// A copy without the null members, which is also what facts.py's seal is taken over.
const bare = (v) => Array.isArray(v) ? v.map(bare)
  : v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).filter(([, x]) => x !== null).map(([k, x]) => [k, bare(x)])) : v
const sealLine = (facts) => ({ ...facts, seal: fnv1a(canonical(bare(facts))) })
// The JSON a prompt ends with after `key: `, or its trailing list on a line of its own.
const payloadOf = (prompt, key) => JSON.parse(String(prompt).match(new RegExp(`${key}: (\\{.*\\})$`))[1])
const trailingList = (prompt) => JSON.parse(String(prompt).slice(String(prompt).indexOf('\n[') + 1))
// The workflow's failure identity.
const failureKey = (f) => JSON.stringify([f.workflow, f.job, f.cell, f.signature])
// The 16-hex key a caller accepts a failure by: 64-bit FNV-1a of its identity.
const keyOf = (f) => {
  let h = 0xcbf29ce484222325n
  for (const ch of failureKey(f)) h = ((h ^ BigInt(ch.codePointAt(0))) * 0x100000001b3n) & 0xffffffffffffffffn
  return h.toString(16).padStart(16, '0')
}
// The checks a CI judge prompt asks about.
const askedOf = (prompt) => JSON.parse(String(prompt).match(/each needing exactly one entry in your reply: (\[.*\])\./)[1])
const manifestOf = (calls, label) => payloadOf(calls.find(c => c.label === label).prompt, 'Manifest').replies
// The nth commit a run makes. Each is distinct, as a real commit is, because the
// audit rejects one whose SHA equals its parent.
const shaFor = (n) => (SHA.slice(0, 38) + String(n).padStart(2, '0')).toLowerCase()
const HEAD = '0f1e2d3c4b5a69788796a5b4c3d2e1f0deadbee5'
// Somebody else's commit: a plausible HEAD that is not one this run made.
const FOREIGN = 'c0ffee11223344556677889900aabbccddeeff01'
// What the preflight pins, and what the pre-publish recheck must still find.
const PIN = {
  branch: 'claude/foo', prBranch: 'claude/foo',
  prHead: HEAD, prRepo: 'hathach/tinyusb', prUrl: 'https://github.com/hathach/tinyusb/pull/3888',
  remote: 'origin', upstreamBranch: 'claude/foo',
  pushUrls: ['git@github.com:hathach/tinyusb.git'], head: HEAD, dirty: [],
  pr: 3888, badPushUrl: '',
}
// What the pre-publish recheck must still find: HEAD exactly where the run left it.
const RECHECK = { branch: 'claude/foo', pushUrls: ['git@github.com:hathach/tinyusb.git'], head: HEAD, staged: [], status: [] }
const ADOPT = '1111111111111111111111111111111111111111'
const ADOPT_MID = '2222222222222222222222222222222222222222'
const STATE_PIN = {
  prRepo: PIN.prRepo, prBranch: PIN.prBranch, prUrl: PIN.prUrl,
  remote: PIN.remote, pushUrls: PIN.pushUrls,
}
const chainAudit = (commits = [ADOPT], paths = ['src/adopted.c'], over = {}) => ({
  from: HEAD, to: commits[commits.length - 1], commits, paths, refusal: '', ...over,
})
const adoptionState = ({
  maxCycles = 4, cyclesUsed = 1, reviewers = ['coderabbit'], autoRun = reviewers,
  protected: protectedPattern = null, pin = STATE_PIN, ...over
} = {}) => seal({
  version: 3, pin: structuredClone(pin), expectedHead: HEAD, reviewClock: null, pending: null,
  config: {
    pr: 3888, reviewers, autoRun, checkoutDir: '.', ciWait: 30,
    protected: protectedPattern === null ? null : new RegExp(protectedPattern).source, generated: null,
  },
  build: null, cyclesUsed, maxCycles, answeredWith: [], deferrals: [], acceptedFailures: [], decisions: [], holds: [],
  ciCache: { reruns: [], entries: [] }, debt: [],
  last: cyclesUsed ? { cycle: cyclesUsed, head: HEAD } : null, ...over,
})
const adoptionArgs = (state, over = {}) => ({
  reviewers: state.config.reviewers, autoRun: state.config.autoRun,
  maxCycles: state.maxCycles, ciWait: state.config.ciWait,
  protected: state.config.protected, generated: state.config.generated, build: state.build,
  yieldAfterCycle: true, state, adoptHead: ADOPT, ...over,
})

// The runtime validates a stub's reply against its schema; the harness does the
// same for the CI stub, the one whose shape changed, so a fixture in the old
// shape fails here as a stale watcher would there.
const conforms = (schema, value, at) => {
  if (schema.enum && !schema.enum.includes(value)) throw new Error(`${at}: ${JSON.stringify(value)} not in ${schema.enum}`)
  if (schema.type === 'object') {
    for (const k of schema.required || []) if (!(k in value)) throw new Error(`${at}: missing ${k}`)
    for (const k of Object.keys(value)) {
      if (!(k in (schema.properties || {}))) { if (schema.additionalProperties === false) throw new Error(`${at}: unexpected ${k}`); continue }
      conforms(schema.properties[k], value[k], `${at}.${k}`)
    }
  } else if (schema.type === 'array') {
    if (value.length < (schema.minItems ?? 0) || value.length > (schema.maxItems ?? Infinity)) throw new Error(`${at}: ${value.length} items`)
    value.forEach((v, i) => conforms(schema.items, v, `${at}[${i}]`))
  }
  return value
}

// state_transfer.py's envelope for a saved output holding `state`, all chunks or `chunks` ("1,3").
const STATE_TRANSFER = new URL('../skills/pr-babysit/scripts/state_transfer.py', import.meta.url).pathname
const transferOf = (state, chunks) => {
  const dir = mkdtempSync(join(tmpdir(), 'pr-babysit-state-'))
  try {
    const file = join(dir, 'w.output')
    writeFileSync(file, JSON.stringify({ result: { state } }))
    const done = spawnSync('python3', [STATE_TRANSFER, file, ...(chunks ? ['--chunks', chunks] : [])], { encoding: 'utf8' })
    return JSON.parse(done.stdout.trim().split('\n').at(-1))
  } finally { rmSync(dir, { recursive: true, force: true }) }
}

// The paths a prompt's command hands a script after `prefix`, each quoted as
// shq quotes it (a `'` inside a path is `'\\''`).
const SHQ = /'[^']*'(?:\\''[^']*')*/g
const quotedAfter = (prefix) => (prompt) => {
  const m = String(prompt).match(new RegExp(`${prefix.source} ((?:${SHQ.source} ?)+)`))
  return m ? [...m[1].matchAll(SHQ)].map(t => t[0].slice(1, -1).replaceAll(`'\\''`, `'`)) : []
}
const pathLine = quotedAfter(/commits\.py commit/)
const auditScope = quotedAfter(/commits\.py head --parent [0-9a-f]{40}/)
// A deterministic 40-hex blob id per path, shared by the hook snapshot and the audit's ls-tree.
const blobOf = (f) => [...f].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 0xffffffff, 7).toString(16).padStart(40, '0')
// The failing checks collect.py lists for a CI fixture: a failure with no
// workflow is a SonarCloud-style check whose link names no run (its own `link`
// when the fixture gives one); every other one is an Actions job.
const failedChecks = (ci) => (ci.realFailures || []).map((rf, i) => {
  if (rf.workflow === '') return { name: rf.check, workflow: '', bucket: ci.bucket ?? 'fail', link: rf.link ?? `https://sonarcloud.io/dashboard?id=o_r&pullRequest=${i + 1}`, attempt: null }
  const link = rf.link ?? `https://github.com/o/r/actions/runs/1/job/${i + 1}`
  return { name: rf.check, workflow: 'ci', bucket: ci.bucket ?? 'fail', link, attempt: `actions:${link.split('/').at(-1)}`, ...(rf.aliases ? { aliases: rf.aliases } : {}) }
})
// push.py's receipt: what the pinned push URL holds after the push, and the PR
// head when the workflow asked for it.
const receiptOf = (head, prHead, detail, pushed = true) => ({
  pushed, detail, heads: PIN.pushUrls.map(url => ({ url, head })), ...(prHead === undefined ? {} : { prHead }),
})
const hookQuoted = quotedAfter(/hooks\.py/)
// reply.py's printed receipts leave out a null resolved or error.
const printed = (receipts) => receipts.map(r => Object.fromEntries(Object.entries(r).filter(([k, v]) => v !== null || !['resolved', 'error'].includes(k))))
const lsTreeOf = (paths) => paths.map(f => `100644 blob ${blobOf(f)}\t${f}`)

// Drive the workflow against stub agents. Every cycle gets the same `reviews`
// and `ci` answer; `fix`/`push` patch (or null out) those replies.
async function run(opts = {}) {
  const logs = []
  const calls = opts.trace ?? [] // shared so a case that expects a throw can still see what ran
  const napPoints = [] // logs.length when a backoff started, to prove ordering
  const reviews = opts.reviews ?? { findings: [], replies: [], bots: 'reviewed' }
  const ci = opts.ci ?? GREEN
  const store = opts.store ?? new Map()
  const patch = (base, over) => over === null ? null : { ...structuredClone(base), ...over }
  // The stub checkout's HEAD: the PR head until this run pushes, then the SHA it
  // pushed — the same rule the workflow's own expectedHead follows, so a second
  // cycle rechecks against what the first one actually left behind.
  let head = (opts.preflight && opts.preflight.head) || HEAD
  let prHead = (opts.preflight && opts.preflight.prHead) || HEAD
  let made = SHA
  let commits = 0 // so each stub commit gets its own SHA, as a real one would
  let staged = [] // what the committer put in it, read back by the audit agent
  let validatorCalls = 0
  let brokenWriter = false // a writer reported a failed build since the last candidate build

  const agent = async (prompt, options) => {
    const label = options.label
    calls.push({
      label, prompt: String(prompt), agentType: options.agentType,
      phase: options.phase, schema: options.schema, model: options.model,
    })
    if (opts.throwOn && (typeof opts.throwOn === 'function' ? opts.throwOn(label) : label.startsWith(opts.throwOn))) throw new Error(`${label} exploded`)
    if (label.startsWith('state:load#')) {
      // The real state_transfer.py on a saved output holding opts.load, then
      // opts.copy(envelope, label) for what the loader agent hands back.
      if (opts.load instanceof Error) throw opts.load
      const envelope = transferOf(opts.load, (String(prompt).match(/ --chunks ([\d,]+)\n/) || [])[1])
      const answer = opts.copy ? await opts.copy(envelope, label) : envelope
      return answer === null ? null : conforms(options.schema, answer, label)
    }
    if (label === 'preflight' || label === 'preflight.retry') {
      return patch({ ...PIN, head, prHead }, opts.preflight)
    }
    if (label === 'adopt:audit' || label === 'adopt:audit.retry') {
      const fallback = chainAudit([opts.args?.adoptHead], undefined, { from: opts.args?.state?.expectedHead ?? HEAD })
      const answer = typeof opts.adoptAudit === 'function' ? await opts.adoptAudit(label)
        : opts.adoptAudit === undefined ? fallback : opts.adoptAudit
      if (answer instanceof Error) throw answer
      return answer === null ? null : conforms(options.schema, structuredClone(answer), label)
    }
    if (label === 'adopt:push' || label === 'adopt:push.retry') {
      // push.py's receipt: by default the push landed on every pinned URL and the PR.
      const to = opts.args.adoptHead
      const answer = typeof opts.adoptPush === 'function' ? await opts.adoptPush(label)
        : opts.adoptPush === undefined ? receiptOf(to, to, 'pushed adopted head') : opts.adoptPush
      if (answer instanceof Error) throw answer
      if (answer === null) return null
      const push = conforms(options.schema, bare(structuredClone(answer)), label)
      if (push.pushed) prHead = to
      return push
    }
    if (label.startsWith('recheck#')) {
      const over = typeof opts.recheck === 'function' ? opts.recheck(label) : opts.recheck
      return patch({ ...RECHECK, head }, over)
    }
    if (label.startsWith('ci:collect#')) {
      // A fixture names the report the CI lane should compose; collect.py's
      // answers follow from it (failedChecks); a running fixture also counts one pending check.
      const failed = failedChecks(ci)
      // recall and remember use opts.store, collect.py's verdict store: a Map by
      // head and link that a relaunch shares with the launch before it.
      const text = String(prompt)
      const key = (link) => `${head} ${link}`
      const links = [...text.matchAll(/--check '([^']*)'/g)].map(m => m[1])
      // opts.base: each --check's base job, 'b1' by default, or a function of the call's label and the link.
      const baseOf = (link) => typeof opts.base === 'function' ? opts.base(label, link) : opts.base === undefined ? 'b1' : opts.base
      let answer
      if (/ recall /.test(text)) {
        // opts.held: the page starts of each link collect.py holds back, its verdict too large for the line.
        const offset = text.match(/ --offset (\d+) /)?.[1]
        if (offset !== undefined) {
          const v = store.get(key(links[0])), o = Number(offset)
          answer = { head, verdicts: [{ ...structuredClone(v), failures: v.failures.slice(o, opts.held.get(v.link).find(s => s > o)) }], left: [], error: null }
        } else {
          const known = links.filter(l => store.has(key(l)))
          answer = { head, verdicts: known.filter(l => !opts.held?.has(l)).map(l => structuredClone(store.get(key(l)))), left: known.filter(l => opts.held?.has(l)).map(link => ({ link, starts: opts.held.get(link) })), error: null }
        }
        if (opts.recall) answer = opts.recall(answer, text)
      } else if (/ remember /.test(text)) {
        // A patch replaces the stored failures of its keys; the stub refuses no patch.
        for (const v of trailingList(text)) {
          const old = store.get(key(v.link))
          store.set(key(v.link), v.patch ? { ...old, failures: old.failures.map(f => v.patch.find(p => failureKey(p) === failureKey(f)) ?? f) } : v)
        }
        answer = { head, error: null }
        if (opts.remember) answer = opts.remember(answer, store)
      } else if (/collect\.py bases /.test(text)) {
        answer = { head, bases: links.map(link => ({ link, base: baseOf(link) })), error: null }
      } else if (/ inventory /.test(text)) {
        answer = { head: ci.headSha ?? head, status: ci.status, pending: ci.status === 'running' ? 1 : 0, checks: failed, error: null }
      } else {
        if (opts.evidence) await opts.evidence(calls)
        // A --gate's answer is its fixture's gate, by default one complete failure per fixture.
        const gates = [...text.matchAll(/--gate '([^']*)'/g)].map(m => {
          const rf = ci.realFailures[failed.findIndex(c => c.link === m[1])]
          return { link: m[1], failures: [{ firstError: rf.firstError, signature: rf.firstError, complete: true }], ...rf.gate }
        })
        answer = { head: ci.headSha ?? head, detail: '/tmp/ci-collect/failures-1.json', gates, bases: links.map(link => ({ link, base: baseOf(link) })), error: null }
        if (opts.gates) answer = opts.gates(answer)
      }
      return answer === null ? null : conforms(options.schema, bare(answer), label)
    }
    if (label.startsWith('ci:judge#')) {
      // The rest of each failure is the plain case: one run, every failure of a job listed.
      const links = askedOf(prompt).map(x => x.link)
      const failures = structuredClone(ci.realFailures)
        .map(rf => ({ workflow: 'ci', job: rf.check, cell: null, signature: rf.firstError, runId: 1, complete: true, ...rf }))
      const failed = failedChecks(ci)
      const judged = { checks: links.map(link => ({ link, failures: [failures[failed.findIndex(c => c.link === link)]] })), infraRerun: ci.infraRerun }
      return conforms(options.schema, opts.judge ? opts.judge(judged) : judged, label)
    }
    if (label.startsWith('reviews#')) {
      if (reviews instanceof Error) throw reviews
      const r = structuredClone(opts.reviewsPerCycle ? opts.reviewsPerCycle() : reviews)
      // A finding that relates to no earlier verdict, unless a case says it does.
      if (Array.isArray(r.findings)) r.findings = r.findings.map(f => ({ related: null, changeReason: null, ...f }))
      // `bots: 'reviewed'` / `'pending'` name every auto-running bot in that
      // state; explicit records are filled the same way, one field at a time.
      // The auto-running bots are the ones the workflow's prompt names.
      const autoRun = (String(prompt).match(/of those, (.+?) auto-run on every push/)?.[1].split(', ')) ?? []
      const bots = r.bots === 'reviewed' || r.bots === undefined ? autoRun.map(b => bot(b))
        : r.bots === 'pending' ? autoRun.map(b => bot(b, { state: 'absent', sha: null, evidence: [], reason: 'nothing on head' }))
          : r.bots
      const observedAt = at((opts.clockOffset ?? 0) + (validatorCalls++) * (opts.minutesPerCycle ?? 1))
      return {
        headSha: head, observedAt, headEventAt: null, headEventEvidence: 'stub', ...r,
        bots: bots.map(b => ({ ...b, sha: b.sha === 'head' ? head : b.sha })),
      }
    }
    if (label === 'scope:verify') {
      // git ls-files echoes the paths that exist; the prompt single-quotes each
      // one, so reading them back out of it is also the proof that it did.
      const quoted = [...String(prompt).matchAll(/'([^']*)'/g)].map(m => m[1])
      return { files: opts.lsFiles ? opts.lsFiles(quoted) : quoted }
    }
    if (label.startsWith('scope:')) return { files: opts.scope ?? [] }
    if (label.startsWith('fix:')) {
      // opts.fix overrides every writer's result; a function shapes it per label
      // (and may delay, to finish writers out of order); null is a dead writer.
      const over = typeof opts.fix === 'function' ? await opts.fix(label) : opts.fix
      if (over === null) return null
      if (over && over.buildOk === false) brokenWriter = true
      return {
        item: label.slice(4), diffstat: `stat:${label.slice(4)}`,
        buildOk: true, board: '', notes: '', ...over,
      }
    }
    if (label.startsWith('replies#') || label.startsWith('resolve#') || label.startsWith('defer#')) {
      if (opts.posting === null || (typeof opts.posting === 'function' && opts.posting(label) === null)) return null // a dead posting agent
      // The script's receipts, one per manifest entry, echoing each body's
      // digest: posted, read back and resolved unless a case says the batch
      // failed (dropDoneIds), a reply landed with the wrong body (wrongBody),
      // one of ours was already there in other words (alreadyThere),
      // its read-back was unavailable (unreadable), the POST's response was
      // lost (lost), the agent invented an id (strayDoneIds), the id is on
      // none of the PR's id spaces (noTarget), it names a review body
      // (reviewBody) or the receipts are reshaped (receipts).
      const entries = JSON.parse(String(prompt).match(/Manifest: (\{.*\})$/)[1]).replies
      const failed = opts.dropDoneIds && opts.dropDoneIds(label)
      const receipt = ({ commentId, digest }) => failed
        ? { commentId, kind: 'review', replyId: null, digest, sent: false, posted: false, verified: false, resolved: null, error: 'posting failed' }
        : opts.noTarget && opts.noTarget(commentId)
          ? { commentId, kind: 'none', replyId: null, digest, sent: false, posted: false, verified: false, resolved: null, error: `comment ${commentId} is not on PR #7` }
        : opts.reviewBody && opts.reviewBody(commentId)
          ? { commentId, kind: 'review-body', replyId: 500 + commentId, digest, sent: true, posted: true, verified: true, resolved: null, error: null }
        : opts.lost && opts.lost(commentId)
          ? { commentId, kind: 'review', replyId: null, digest, sent: true, posted: false, verified: false, resolved: null, error: 'connection reset' }
          : opts.wrongBody && opts.wrongBody(commentId)
            ? { commentId, kind: 'review', replyId: 500 + commentId, digest, sent: true, posted: true, verified: false, resolved: null, error: 'read-back mismatch on body' }
          : opts.alreadyThere && opts.alreadyThere(commentId)
            ? { commentId, kind: 'review', replyId: 500 + commentId, digest, sent: false, posted: false, verified: false, resolved: null, error: `reply ${500 + commentId} of ours is already on this comment in other words; reconcile by hand` }
            : opts.unreadable && opts.unreadable(commentId)
              ? { commentId, kind: 'review', replyId: 500 + commentId, digest, sent: true, posted: true, verified: null, resolved: null, error: 'read-back unavailable: HTTP 502' }
              : { commentId, kind: 'review', replyId: 500 + commentId, digest, sent: true, posted: true, verified: true, resolved: true, error: null }
      const receipts = [...entries.map(receipt), ...(opts.strayDoneIds || []).map(id => receipt({ commentId: id, digest: 'deadbeef' }))]
      return { receipts: printed(opts.receipts ? opts.receipts(receipts, label) : receipts) }
    }
    if (label.startsWith('hooks#')) {
      if (opts.hooks === null) return null // a dead hook agent
      // The tree as the hooks find it: exactly the owned paths, modified. A case
      // that wants a hook to regenerate something overrides `after`.
      const owned = hookQuoted(prompt)
      const status = owned.map(f => ` M ${f}`)
      const snap = owned.map(f => `644 ${blobOf(f)} ${f}`)
      const base = { ran: true, passed: true, modifiedBy: [], before: status, after: status, snapshotBefore: snap, snapshotAfter: snap }
      return { ...base, ...(typeof opts.hooks === 'function' ? opts.hooks(base) : opts.hooks) }
    }
    if (label.startsWith('commit#')) {
      if (opts.commit === null) return null // a dead commit agent
      // What the committer staged, remembered so the read-back agent can report
      // it. A distinct SHA per commit, as a real one is: the audit rejects a
      // commit whose SHA equals its parent, so reusing one would fail in cycle 2.
      staged = pathLine(prompt)
      made = shaFor(++commits)
      return { committed: true, detail: 'committed', ...opts.commit }
    }
    if (label.startsWith('audit#')) {
      if (opts.audit === null) return null // a dead read-back agent
      // ls-tree of the commit: what the stub committed is what the tree held.
      const entries = staged.map(f => `100644 blob ${blobOf(f)}\t${f}`)
      // Before any commit, HEAD is where the run started.
      const parent = prompt.match(/head --parent ([0-9a-f]{40})/)[1]
      return { parent, scope: auditScope(prompt), sha: commits ? made : head, entries, refusal: '', ...opts.audit }
    }
    if (label.startsWith('push#')) {
      // push.py's receipt; the workflow supplies committed and sha.
      if (opts.push === null) return receiptOf(head, undefined, 'push rejected', false)
      const push = conforms(options.schema, bare({ ...receiptOf(made, undefined, 'pushed to claude/foo'), ...opts.push }), label)
      if (push.pushed) head = made // the pushed commit is where the checkout now sits
      return push
    }
    if (label.startsWith('challenge#')) {
      assert.equal(options.agentType, 'finding-verifier')
      // Fixtures say upheld true/false for justified/valid; unknown is spelled out.
      const tri = (c) => c && Array.isArray(c.verdicts)
        ? { verdicts: c.verdicts.map(({ upheld, ...v }) => upheld === undefined ? v : { ...v, verdict: upheld ? 'justified' : 'valid' }) } : c
      if (opts.challengePerCycle) return tri(opts.challengePerCycle())
      if (!('challenge' in opts)) {
        // default: uphold every submitted dismissal; leave a reversal to valid unsettled
        const submitted = JSON.parse(String(prompt).match(/Findings: (\[.*\])\.$/s)[1])
        return { verdicts: submitted.map(x => x.verdict === 'valid' ? { id: x.id, verdict: 'unknown', reason: 'cannot tell' } : { id: x.id, verdict: 'justified', reason: 'stands' }) }
      }
      if (typeof opts.challenge === 'function') {
        const submitted = JSON.parse(String(prompt).match(/Findings: (\[.*\])\.$/s)[1])
        return conforms(options.schema, { verdicts: submitted.map(x => ({ id: x.id, ...opts.challenge(x) })) }, label)
      }
      return opts.challenge === null ? null : tri(structuredClone(opts.challenge))
    }
    if (label.startsWith('check:')) {
      assert.equal(options.agentType, 'finding-verifier')
      // opts.verify is every group's verdict, a function of the label for one per
      // group, or null for a dead verifier.
      if ('verify' in opts && opts.verify === null) return null
      if (typeof opts.verify === 'function') return opts.verify(label)
      return structuredClone(opts.verify ?? { addresses: true, reason: 'verified' })
    }
    if (label.startsWith('issue#')) {
      assert.equal(options.agentType, 'finding-verifier')
      if (opts.covers === null) return null
      // opts.covers(findingId) is whether its issue covers it; true by default.
      const ids = trailingList(prompt).map(x => x.findingId)
      return conforms(options.schema, { verdicts: ids.map(findingId => ({
        findingId, covers: opts.covers ? opts.covers(findingId) : true, reason: 'stub issue read',
      })) }, label)
    }
    if (label.startsWith('inspect#')) {
      // reply.py --inspect: our reply as it stands on each pair, the comment's
      // digest being the harness finding's; opts.inspect reshapes one per pair.
      // A bare COMMENT is our newest reply to it: none unless opts.byHand(commentId) gives { replyId, body }.
      const targets = String(prompt).match(/--inspect ([\d: ]+)`/)[1].trim().split(' ').map(t => t.split(':').map(Number))
      const body = 'answered already, in other words'
      const got = targets.map(([commentId, replyId]) => {
        if (replyId !== undefined) {
          return { commentId, replyId, kind: 'review', body, bodyDigest: fnv1a(body), originalDigest: `d${commentId}`, error: null,
            ...(opts.inspect ? opts.inspect(commentId) : {}) }
        }
        const hand = opts.byHand && opts.byHand(commentId)
        return hand
          ? { commentId, kind: 'review', bodyDigest: fnv1a(hand.body), original: `comment ${commentId}`, originalDigest: `d${commentId}`, error: null, ...hand }
          : { commentId, replyId: null, kind: 'review', body: null, bodyDigest: null, originalDigest: `d${commentId}`, error: `no reply of ours on comment ${commentId}` }
      })
      return conforms(options.schema, bare({ inspected: got }), label)
    }
    if (label.startsWith('judge#')) {
      // The verifier on replies answered by hand: opts.judge(commentId) is its answers, false by default.
      const ids = trailingList(prompt).map(x => x.commentId)
      return conforms(options.schema, { verdicts: ids.map(commentId => ({
        commentId, answers: opts.judge ? opts.judge(commentId) : false, reason: 'stub judgement',
      })) }, label)
    }
    if (label.startsWith('reuse#')) {
      if (opts.reuse === null) return null
      const { reuses } = payloadOf(prompt, 'Reuses')
      return conforms(options.schema, { receipts: printed(reuses.map(u => ({
        commentId: u.commentId, kind: 'review', replyId: u.replyId, digest: u.bodyDigest,
        sent: false, posted: false, verified: true, resolved: true, error: null,
        ...(opts.reuse ? opts.reuse(u.commentId) : {}),
      }))) }, label)
    }
    if (label.startsWith('sonar#')) {
      // sonar.py's sealed line: every item marked unless opts.sonar(items, label) answers otherwise.
      const items = payloadOf(prompt, 'Manifest').items
      const answer = typeof opts.sonar === 'function' ? opts.sonar(items, label)
        : { results: items.map(x => ({ commentId: x.commentId, issue: `K${x.commentId}`, outcome: 'marked', detail: 'false positive' })) }
      if (answer === null) return null
      return conforms(options.schema, answer.error ? answer : sealLine(answer), label)
    }
    if (label.startsWith('build:resolve#')) {
      // The repository's build for the batch, resolved when the caller named none.
      const plan = typeof opts.buildPlan === 'function' ? opts.buildPlan(label) : opts.buildPlan
      if (plan === null) return null
      return conforms(options.schema, { command: 'make -C <BUILD> all', contract: ['AGENTS.md'], reason: 'stub contract', error: null, ...plan }, label)
    }
    if (label.startsWith('build#')) {
      // build.py's receipt: the build fails when a writer's did; opts.candidate reshapes it, null is a dead agent.
      const over = opts.candidate
      const exit = brokenWriter ? 1 : 0
      brokenWriter = false
      if (over === null) return null
      // What the prompt asked build.py for, each value shell-quoted as --flag='value'.
      const asked = (flag) => [...String(prompt).matchAll(new RegExp(`--${flag}='((?:[^']|'\\\\'')*)'`, 'g'))].map(m => m[1].replaceAll("'\\''", "'"))
      return conforms(options.schema, bare({
        revision: head, snapshot: 'snap', snapshotAfter: 'snap',
        command: asked('command')[0].replaceAll('<BUILD>', '/tmp/b'), buildDir: '/tmp/b', exit,
        log: '/tmp/candidate.log', retained: [],
        ...(typeof over === 'function' ? over(label, String(prompt)) : over),
      }), label)
    }
    if (label.startsWith('compat#')) {
      assert.equal(options.agentType, 'finding-verifier')
      // opts.compat is every check's verdict, a function of the label for one per
      // check, or null for a dead verifier.
      const over = typeof opts.compat === 'function' ? opts.compat(label) : opts.compat
      if (over === null) return null
      return conforms(options.schema, structuredClone(over ?? { compatible: true, evidence: 'no consumer relies on it' }), label)
    }
    throw new Error(`unstubbed agent label ${label}`)
  }
  // Match the host: a thrown thunk or stage settles its slot to null, in place,
  // and the rest of the batch still lands.
  const settle = (ps) => Promise.allSettled(ps).then(rs => rs.map(r => r.status === 'fulfilled' ? r.value : null))
  const pipeline = (items, first, second) =>
    settle(items.map(async item => second(await first(item), item)))
  const parallel = (thunks) => settle(thunks.map(fn => fn()))
  const workflow = async () => { throw new Error('pr-babysit cannot nest a workflow') }

  // nap()'s real delay is minutes; fire it immediately and record where in the
  // log stream it happened.
  const realTimeout = globalThis.setTimeout
  globalThis.setTimeout = (fn) => { napPoints.push(logs.length); realTimeout(fn, 0); return 0 }
  try {
    const fn = new AsyncFunction(
      'args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget',
      ...ABSENT, body)
    const result = await fn(
      opts.rawArgs ?? { pr: 3888, maxCycles: 1, autoPush: true, reviewers: ['coderabbit'], ...opts.args },
      // Each stub answers as its script before facts.py seals the line; the seal goes on
      // here (an error line's relay fills it with ''), then opts.garble is the relay's copy.
      async (prompt, options) => {
        const sealing = !!options.schema?.properties?.seal
        const schema = sealing ? { ...options.schema, required: options.schema.required.filter(k => k !== 'seal') } : options.schema
        const line = await agent(prompt, { ...options, schema })
        const sealed = !line || !sealing || 'seal' in line ? line : line.error ? { ...line, seal: '' } : sealLine(line)
        return opts.garble ? opts.garble(options.label, sealed) : sealed
      }, pipeline, parallel, () => {}, (m) => logs.push(String(m)), workflow, null,
      ...ABSENT.map(() => undefined))
    return { result, logs, labels: calls.map(c => c.label), calls, napPoints }
  } finally {
    globalThis.setTimeout = realTimeout
  }
}

const summaries = (logs) => logs.filter(l => l.startsWith('cycle ') && l.includes(' summary '))
// Split on the padded delimiter, not on a bare pipe: an escaped `\|` inside a
// cell must stay part of that cell.
const rowsOf = (summary) => summary.split('\n').slice(3)
  .map(l => l.replace(/^\| /, '').replace(/ \|$/, '').split(' | ').map(c => c.trim()))
// GFM's own row rule: a backslash escapes the next character, so only an
// unescaped pipe splits cells. Applying it is the only way to prove a claim
// carrying `\|` renders inside one cell instead of spilling into extra columns.
const gfmCells = (row) => {
  const cells = ['']
  for (let i = 0; i < row.length; i++) {
    if (row[i] === '\\') cells[cells.length - 1] += row[++i] ?? ''
    else if (row[i] === '|') cells.push('')
    else cells[cells.length - 1] += row[i]
  }
  return cells.slice(1, -1).map(c => c.trim()) // the framing pipes leave an empty cell at each end
}

// node:test is free to run a file's tests concurrently, and run() swaps the
// global setTimeout for the length of one workflow run — two live runs would
// restore each other's timer and lose the nap ordering asserted below. Chaining
// each test onto the previous one keeps exactly one run() in flight.
let queue = Promise.resolve()
const test = (name, fn) => nodeTest(name, () => (queue = queue.then(fn, fn)))

test('meta names the three phases the workflow dispatches into', async () => {
  const { calls } = await run({ reviews: oneValid })
  assert.equal(globalThis.__meta.name, 'pr-babysit')
  assert.deepEqual(globalThis.__meta.phases.map(p => p.title), ['Triage', 'Fix', 'Push'])
  const titles = new Set(globalThis.__meta.phases.map(p => p.title))
  for (const c of calls) assert.ok(titles.has(c.phase), `${c.label} ran in phase ${c.phase}`)
})

test('args validation', async () => {
  await assert.rejects(run({ args: { pr: undefined } }), /args must be/)
  await assert.rejects(run({ rawArgs: '{"pr": 3888, "state": {"version": 3' }), /args is not valid JSON \(.+\); pass an object/)
  await assert.rejects(run({ rawArgs: '{"pr": 0}' }), /args must be/, 'valid JSON still gets the shape check')
  await assert.rejects(run({ args: { pr: 0 } }), /args must be/)
  await assert.rejects(run({ args: { pr: -3 } }), /positive integer/)
  await assert.rejects(run({ args: { pr: 'abc' } }), /positive integer/)
  await assert.rejects(run({ args: { maxCycles: 0 } }), /maxCycles must be/)
  await assert.rejects(run({ args: { ciWait: 0 } }), /ciWait must be a positive integer/)
  await assert.rejects(run({ args: { ciWait: 1.5 } }), /ciWait must be a positive integer/)
  await assert.rejects(run({ args: { lane: 'review' } }), /lane must be 'both', 'ci' or 'reviews'/)
  await assert.rejects(run({ args: { lane: 'ci' } }), /needs yieldAfterCycle/)
})

test('an unknown reviewer or a malformed protected pattern throws before any agent runs', async () => {
  for (const [args, expected] of [
    [{ reviewers: ['coderabbit', 'gpt'] }, /unknown reviewer\(s\) \["gpt"\]/],
    [{ reviewers: ['codex'] }, /unknown reviewer\(s\) \["codex"\]/],
    [{ reviewers: 'coderabbit' }, /reviewers must be an array of copilot, coderabbit, greptile, code-scanning; \[\] runs no review lane/],
    [{ reviewers: [4] }, /unknown reviewer/],
    [{ reviewers: ['coderabbit'], autoRun: ['greptile'] }, /autoRun must be a subset of reviewers \["coderabbit"\]/],
    [{ reviewers: ['coderabbit'], autoRun: 'coderabbit' }, /autoRun must be a subset/],
    [{ reviewers: ['coderabbit', 'code-scanning'], autoRun: ['coderabbit', ' Code-Scanning'] }, /autoRun must be a subset of reviewers \["coderabbit","code-scanning"\] without copilot, code-scanning/],
    [{ reviewers: ['copilot'], autoRun: ['copilot'] }, /autoRun must be a subset of reviewers \["copilot"\] without copilot, code-scanning/],
    [{ protected: '^test/hil/(' }, /protected is not a valid regex/],
    [{ protected: '   ' }, /non-empty regex string/],
    [{ protected: 7 }, /non-empty regex string/],
    [{ generated: '^hw/bsp/(' }, /generated is not a valid regex/],
    [{ generated: '' }, /generated must be a non-empty regex string/],
  ]) {
    const trace = []
    await assert.rejects(run({ args, trace }), expected, JSON.stringify(args))
    assert.deepEqual(trace, [], 'nothing may be dispatched before the args are checked')
  }
  // Names are normalized, and an empty roster is a legal value, not a typo.
  const named = await run({ args: { reviewers: ['  CodeRabbit ', 'CopIlot'] } })
  assert.equal(named.result.pass, true)
  const none = await run({ args: { reviewers: [] } })
  assert.equal(none.result.pass, true)
})

test('omitted reviewers default to coderabbit and greptile auto-running, and code-scanning harvested only', async () => {
  for (const reviewers of [undefined, null]) {
    const { calls, result } = await run({ args: { reviewers } })
    assert.equal(result.pass, true)
    const prompt = calls.find(c => c.label.startsWith('reviews#')).prompt
    assert.match(prompt, /harvest on this PR are coderabbit, greptile, code-scanning, and no others; of those, coderabbit, greptile auto-run on every push/)
    assert.deepEqual(result.state.config.autoRun, ['coderabbit', 'greptile'])
  }
})

test('code-scanning is never waited for, and its findings are fixed like any bot\'s', async () => {
  const alone = await run({ args: { reviewers: ['code-scanning'] } })
  assert.match(alone.calls.find(c => c.label.startsWith('reviews#')).prompt, /harvest on this PR are code-scanning, and no others; none of them auto-run: report no bot records/)
  assert.equal(alone.result.pass, true, JSON.stringify(alone.result.reason))
  const { calls, logs } = await run({
    args: { reviewers: ['code-scanning'] },
    reviews: { findings: [finding({ source: 'code-scanning', claim: 'PVS-Studio: a part of conditional expression is always false' })], replies: [], bots: 'reviewed' },
  })
  assert.match(calls.find(c => c.label.startsWith('fix:')).prompt, /\[code-scanning\] PVS-Studio: a part of conditional expression/)
  assert.match(rowsOf(summaries(logs)[0])[0][3], /^fixed \+ pushed/)
})

test('the requested reviewers, normalized, are the ones the validator is asked for', async () => {
  const { calls } = await run({ args: { reviewers: ['  CodeRabbit ', 'CopIlot'] } })
  const reviews = calls.find(c => c.label.startsWith('reviews#'))
  assert.match(reviews.prompt, /the reviewers to harvest on this PR are coderabbit, copilot, and no others/)
  assert.doesNotMatch(reviews.prompt, /greptile/i, 'an unrequested bot must not be harvested')
})

test('the auto-running reviewers are named apart from the harvest list', async () => {
  // Harvest-only Copilot must never become a settlement requirement.
  const split = await run({ args: { reviewers: ['greptile', 'coderabbit', 'copilot'], autoRun: [' CodeRabbit '] } })
  const prompt = split.calls.find(c => c.label.startsWith('reviews#')).prompt
  assert.match(prompt, /harvest on this PR are greptile, coderabbit, copilot, and no others; of those, coderabbit auto-run on every push: report one record for each and no other/)
  // Default: everybody harvested; copilot, harvest-only, is not waited for.
  const same = await run({ args: { reviewers: ['greptile', 'coderabbit', 'copilot'] } })
  assert.match(same.calls.find(c => c.label.startsWith('reviews#')).prompt, /of those, greptile, coderabbit auto-run/)
  const nobody = await run({ args: { reviewers: ['copilot'], autoRun: [] } })
  assert.match(nobody.calls.find(c => c.label.startsWith('reviews#')).prompt, /none of them auto-run: report no bot records/)
})

test('greptile is harvested and waited for, or harvested only', async () => {
  const waited = await run({ args: { reviewers: [' Greptile '] } })
  assert.match(waited.calls.find(c => c.label.startsWith('reviews#')).prompt, /harvest on this PR are greptile, and no others; of those, greptile auto-run on every push/)
  assert.deepEqual(waited.result.state.config.autoRun, ['greptile'])
  const harvested = await run({ args: { reviewers: ['greptile'], autoRun: [] } })
  assert.match(harvested.calls.find(c => c.label.startsWith('reviews#')).prompt, /harvest on this PR are greptile, and no others; none of them auto-run/)
  assert.equal(harvested.result.pass, true, JSON.stringify(harvested.result.reason))
})

test('the usual launch, copilot and coderabbit, waits for coderabbit only', async () => {
  const { calls, result } = await run({ args: { reviewers: ['copilot', 'coderabbit'] } })
  const reviews = calls.find(c => c.label.startsWith('reviews#'))
  assert.match(reviews.prompt, /the reviewers to harvest on this PR are copilot, coderabbit, and no others/)
  assert.deepEqual(result.state.config.reviewers, ['copilot', 'coderabbit'])
  assert.deepEqual(result.state.config.autoRun, ['coderabbit'])
  assert.equal(result.pass, true, JSON.stringify(result.reason))
})

test('reviewers: [] runs no review lane at all and still completes', async () => {
  const { result, labels, logs } = await run({
    args: { reviewers: [] },
    // Would be harvested if the lane ran; the stub is never reached.
    reviews: oneValid,
  })
  assert.equal(labels.some(l => l.startsWith('reviews#')), false, 'nobody to harvest, so nobody is asked')
  assert.equal(labels.some(l => l.startsWith('fix:')), false)
  assert.ok(logs.some(l => l === 'cycle 1: no reviewers requested — CI lane only'))
  assert.equal(result.pass, true, `a CI-only run must still reach a verdict (got ${result.reason})`)
  assert.deepEqual(result.history[0].reviews, { findings: [], replies: [], bots: [] })
})

test('the preflight pins the checkout without touching it', async () => {
  const { calls, logs } = await run()
  const pre = calls[0]
  assert.equal(pre.label, 'preflight')
  assert.match(pre.prompt, /Editing and committing nothing/)
  assert.ok(pre.prompt.includes('preflight.py --pr 3888`'), pre.prompt)
  assert.deepEqual(pre.schema.required.slice().sort(),
    ['badPushUrl', 'branch', 'dirty', 'head', 'pr', 'prBranch', 'prHead', 'prRepo', 'prUrl', 'pushUrls', 'remote', 'upstreamBranch'])
  assert.ok(logs.some(l =>
    l === 'preflight: hathach/tinyusb claude/foo@0f1e2d3 tracking origin, clean'))
})

test('a dirty start refuses before any writer runs', async () => {
  const { result, labels, logs } = await run({
    reviews: oneValid, preflight: { dirty: [' M src/a.c', '?? junk.o'] },
  })
  assert.equal(result.pass, false)
  assert.equal(result.reason, 'dirty-start')
  assert.deepEqual(result.dirty, [' M src/a.c', '?? junk.o'])
  assert.equal(result.cycles, 0)
  assert.deepEqual(labels, ['preflight'], 'a pre-existing edit could be swept into the PR')
  assert.ok(logs.some(l => /the checkout is dirty — 2 path\(s\)/.test(l)))
})

test('.idea/ drift is the one dirt a start tolerates, and it never enters a commit', async () => {
  const idea = [' M .idea/misc.xml', '?? .idea/workspace.xml', 'M  .idea/vcs.xml', ' M sub/.idea/x.xml']
  const tolerated = await run({ reviews: oneValid, preflight: { dirty: idea } })
  assert.notEqual(tolerated.result.reason, 'dirty-start')
  assert.ok(tolerated.labels.some(l => l.startsWith('fix:')), 'the run went on to its writers')
  assert.ok(tolerated.logs.some(l => /ignoring 4 dirty \.idea\/ path\(s\)/.test(l)))
  // anything else still blocks, and the report names only what blocked
  const { result, labels } = await run({ reviews: oneValid, preflight: { dirty: [...idea, ' M src/a.c'] } })
  assert.equal(result.reason, 'dirty-start')
  assert.deepEqual(result.dirty, [' M src/a.c'])
  assert.ok(!labels.some(l => l.startsWith('fix:')))
  // a look-alike is not IDE metadata
  const alike = await run({ reviews: oneValid, preflight: { dirty: [' M idea/x', ' M .ideas/x', ' M src/.idea.c'] } })
  assert.equal(alike.result.reason, 'dirty-start')
  // a finding on an .idea/ path gets no writer, like a protected one
  const scoped = await run({ reviews: { findings: [finding({ file: '.idea/misc.xml' })], replies: [], bots: 'reviewed' } })
  assert.equal(scoped.labels.some(l => l.startsWith('fix:')), false)
  assert.ok(scoped.logs.some(l => /\.idea\/misc\.xml is IDE metadata — dropped from scope/.test(l)))
  // .idea/ drift around the hooks neither blocks the publish nor rides into the commit
  const drifting = await run({ ...publishing,
    hooks: b => ({ before: [...b.before, ' M .idea/misc.xml'], after: [...b.after, ' M .idea/misc.xml', '?? .idea/workspace.xml'] }) })
  assert.equal((drifting.result.history[0].reviewPush || {}).pass, true, JSON.stringify(drifting.result.history[0].reviewPushFailed))
  const commit = drifting.calls.find(c => c.label === 'commit#1-review')
  assert.deepEqual(pathLine(commit.prompt), ['src/a.c'])
})

test('a local branch tracking the PR head branch is accepted and pushes to the PR branch', async () => {
  const { result, calls } = await run({
    reviews: oneValid,
    preflight: { branch: 'worktree/x', upstreamBranch: 'claude/foo' },
    recheck: { branch: 'worktree/x' },
  })
  assert.notEqual(result.reason, 'wrong-branch')
  assert.ok(calls.find(c => c.label === 'push#1-review').prompt.includes("--branch 'claude/foo' --sha"))
})

test('a checkout on the wrong branch refuses before any writer runs', async () => {
  const { result, labels, logs } = await run({
    reviews: oneValid, preflight: { branch: 'main', prBranch: 'claude/foo', upstreamBranch: 'main' },
  })
  assert.equal(result.reason, 'wrong-branch')
  assert.equal(result.branch, 'main')
  assert.equal(result.expected, 'claude/foo')
  assert.deepEqual(labels, ['preflight'])
  assert.ok(logs.some(l => /checked out main, but PR #3888 heads claude\/foo/.test(l)))
})

test('the right branch name at the wrong commit refuses', async () => {
  // A branch name is not an identity: the same name can be stale or ahead, and
  // its commits would become the baseline every later audit trusts.
  const { result, labels, logs } = await run({ reviews: oneValid, preflight: { head: FOREIGN } })
  assert.equal(result.reason, 'wrong-head')
  assert.equal(result.head, FOREIGN)
  assert.equal(result.expected, HEAD)
  assert.deepEqual(labels, ['preflight'])
  assert.ok(logs.some(l => /HEAD is c0ffee1, but PR #3888 heads 0f1e2d3/.test(l)))
})

test('a push URL preflight finds outside the PR head repository refuses', async () => {
  const bad = 'git@evil.example:hathach/tinyusb.git'
  const { result, labels, logs } = await run({ reviews: oneValid, preflight: { pushUrls: [bad], badPushUrl: bad } })
  assert.equal(result.reason, 'wrong-remote')
  assert.equal(result.remoteUrl, bad)
  assert.equal(result.expected, 'github.com/hathach/tinyusb')
  assert.deepEqual(labels, ['preflight'])
  assert.ok(logs.some(l => /not PR #3888's head repository/.test(l)), logs.join('\n'))
})

test('a pin of another PR stops the run', async () => {
  const { result, labels } = await run({ reviews: oneValid, preflight: { pr: 3887 } })
  assert.equal(result.reason, 'preflight-failed')
  assert.equal(result.detail, 'the pin is of PR #3887')
  assert.deepEqual(labels, ['preflight'])
})

test('a preflight the script could not pin stops the run with its error', async () => {
  const { result, labels } = await run({ reviews: oneValid, preflight: { error: 'gh pr view 3888: unexpected answer' } })
  assert.equal(result.reason, 'preflight-failed')
  assert.equal(result.detail, 'gh pr view 3888: unexpected answer')
  assert.equal(result.cycles, 0)
  assert.deepEqual(labels, ['preflight'])
})

test('a dead preflight stops the run with nothing else dispatched', async () => {
  for (const opts of [{ preflight: null }, { throwOn: 'preflight' }]) {
    const { result, labels } = await run({ reviews: oneValid, ...opts })
    assert.equal(result.pass, false)
    assert.equal(result.reason, 'preflight-died', JSON.stringify(opts))
    assert.equal(result.cycles, 0)
    assert.deepEqual(result.history, [])
    assert.deepEqual(labels, ['preflight', 'preflight.retry'], 'one fresh agent, then nothing else')
  }
})

test('a relayed copy that does not match its seal gets one fresh agent; a second bad copy is a dead relay', async () => {
  // #3986: the preflight relay cut adoptHead to 35 characters and the launch was lost.
  const cut = (field, only) => (l, a) => a && (only === undefined || l === only) ? { ...a, [field]: a[field].slice(0, 35) } : a
  const adopting = await run({ args: adoptionArgs(adoptionState()), preflight: { head: ADOPT, prHead: HEAD }, garble: cut('head', 'preflight') })
  assert.deepEqual(adopting.labels.slice(0, 3), ['preflight', 'preflight.retry', 'adopt:audit'])
  assert.notEqual(adopting.result.reason, 'adopt-head-mismatch')

  const plain = await run({ garble: cut('head', 'preflight') })
  assert.equal(plain.result.pass, true)
  assert.deepEqual(plain.labels.slice(0, 2), ['preflight', 'preflight.retry'])
  assert.deepEqual(plain.calls.slice(0, 2).map(c => c.model), ['haiku', 'sonnet'], 'the fresh agent copies on Sonnet')

  const twice = await run({ garble: (l, a) => l.startsWith('preflight') ? cut('head')(l, a) : a })
  assert.equal(twice.result.reason, 'preflight-died', 'a second bad copy is a dead relay')
  assert.deepEqual(twice.labels, ['preflight', 'preflight.retry'])

  // What the script itself printed is its answer, however odd: the seal matches it.
  const printed = await run({ preflight: { head: HEAD.slice(0, 35) } })
  assert.equal(printed.result.reason, 'wrong-head')
  assert.deepEqual(printed.labels, ['preflight'])

  for (const [name, field] of [['recheck', 'head'], ['audit', 'sha']]) {
    const { labels, result } = await run({ reviews: oneValid, garble: (l, a) => l.startsWith(`${name}#`) && !l.endsWith('.retry') ? cut(field)(l, a) : a })
    assert.ok(labels.some(l => l.startsWith(`${name}#`) && l.endsWith('.retry')), name)
    assert.ok(labels.some(l => l.startsWith('push#')), `${name}: the push went ahead`)
    assert.notEqual(result.reason, 'push-failed', name)
  }
})

test("facts.py's seal is the one the workflow checks, on text JSON escapes", async () => {
  const odd = 'claude/naïve-✓ "q" \\ \t\u0001 \ud800 é'
  const line = { ...PIN, branch: odd, prBranch: odd, error: null }
  const done = spawnSync('python3', ['-c', 'import json,sys; sys.path.insert(0, sys.argv[1]); from facts import sealed; print(json.dumps(sealed(json.loads(sys.stdin.read()))))',
    join(STATE_TRANSFER, '..')], { input: JSON.stringify(line), encoding: 'utf8' })
  assert.equal(done.status, 0, done.stderr)
  const printed = JSON.parse(done.stdout)
  const kept = await run({ preflight: printed })
  assert.equal(kept.labels.includes('preflight.retry'), false, 'the seal matched: no retry')
  assert.equal(kept.result.pass, true)
  // Keys JavaScript and a code-point sort order differently.
  const keyed = spawnSync('python3', ['-c', 'import json,sys; sys.path.insert(0, sys.argv[1]); from facts import sealed; print(sealed(json.loads(sys.stdin.read()))["seal"])',
    join(STATE_TRANSFER, '..')], { input: JSON.stringify({ '\u{1F600}': 1, '\ue000': 2, '\ud800': 3, a: null }), encoding: 'utf8' })
  assert.equal(keyed.stdout.trim(), sealLine({ '\u{1F600}': 1, '\ue000': 2, '\ud800': 3 }).seal, keyed.stderr)
  const copied = await run({ preflight: printed, garble: (l, a) => l === 'preflight' ? { ...a, branch: a.branch.replace('é', 'e'), prBranch: a.prBranch.replace('é', 'e') } : a })
  assert.deepEqual(copied.labels.slice(0, 2), ['preflight', 'preflight.retry'])
})

test('a clean green PR passes and still logs a summary', async () => {
  const { result, logs } = await run()
  assert.equal(result.pass, true)
  assert.deepEqual(summaries(logs).length, 1)
  assert.match(summaries(logs)[0], /^cycle 1 summary — CI green, reviews: coderabbit reviewed 0f1e2d3\n\(no bot findings/)
  assert.equal(result.history[0].summary, summaries(logs)[0])
})

test('the summary tables every verdict, fix and pushed SHA', async () => {
  const { result, logs } = await run({
    reviews: {
      findings: [
        finding({ commentId: 3, file: 'src/c.c', line: 3, claim: 'refuted | with a pipe', verdict: 'invalid' }),
        finding({ commentId: 1, file: 'src/a.c', line: 1, claim: 'real bug' }),
        finding({ commentId: 2, file: 'src/b.c', line: 2, claim: 'already gone', verdict: 'stale' }),
      ],
      // the validator drafts a reply for every invalid AND stale finding
      replies: [{ commentId: 3, body: 'refuted because…' }, { commentId: 2, body: 'already fixed in…' }],
      bots: 'reviewed',
    },
  })
  const rows = rowsOf(summaries(logs)[0])
  assert.deepEqual(rows.map(r => r[2]), ['valid', 'stale', 'invalid'], 'valid first, then stale, then invalid')
  assert.match(rows[0][3], /^fixed \+ pushed/)
  assert.equal(rows[0][4], shaFor(1).slice(0, 8))
  assert.match(rows[1][3], /already fixed, replied/)
  assert.match(rows[2][3], /refuted, replied/)
  assert.deepEqual([rows[1][4], rows[2][4]], ['-', '-'], 'only fixed findings carry a commit')
  assert.match(rows[2][1], /refuted \\\| with a pipe/, 'a pipe in a claim is escaped, not table-breaking')
  assert.equal(result.history[0].reviewPush.sha, shaFor(1))
  assert.deepEqual(result.rollup, {
    cycles: [1], findings: { total: 3, fixed: 1, open: 0, refuted: 1, stale: 1, deferred: 0, held: 0 },
    ci: { total: 0, fixed: 0, open: 0, accepted: 0, sonarGate: 0, rigSide: 0, unclassified: 0 }, ciChecks: { judged: 0, partial: 0, reused: 0, unchanged: 0 },
    reran: 0, pushed: [shaFor(1).slice(0, 8)], replies: 3,
  })
})

test('a launch rollup counts its own cycles, each finding and CI failure once', async () => {
  const red = redWith(RIG, PVS, UNPLACED).ci
  const first = await run({ args: { ...YIELD, acceptedFailures: [byKey()] }, reviews: WAITING, ci: red })
  assert.deepEqual(first.result.rollup.ci, { total: 3, fixed: 0, open: 0, accepted: 1, sonarGate: 0, rigSide: 1, unclassified: 1 })
  const dry = await run({ args: { autoPush: false, maxCycles: 3 }, reviews: { findings: [finding()], replies: [], bots: 'reviewed' } })
  assert.deepEqual(dry.result.rollup.findings, { total: 1, fixed: 0, open: 1, refuted: 0, stale: 0, deferred: 0, held: 0 }, 'a dry run fixes nothing')
  const twice = await run({ args: { autoPush: true, maxCycles: 2 }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual([twice.result.rollup.cycles, twice.result.rollup.ci.total], [[1, 2], 1])
  assert.deepEqual(twice.result.rollup.ciChecks, { judged: 1, partial: 0, reused: 1, unchanged: 0 }, 'per cycle, summed')
  const again = await run({ args: { ...YIELD, state: first.result.state, acceptedFailures: [byKey()] }, reviews: WAITING, ci: red })
  assert.deepEqual(again.result.rollup.cycles, [2], 'the carried cycle is the previous launch\'s')
  let cycle = 0
  const fixedThenStale = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => ({ findings: [finding({ verdict: ++cycle === 1 ? 'valid' : 'stale' })], replies: [], bots: 'reviewed' }),
  })
  assert.deepEqual([fixedThenStale.result.rollup.cycles, fixedThenStale.result.rollup.findings.fixed, fixedThenStale.result.rollup.findings.stale], [[1, 2], 1, 0],
    'a fix this launch pushed is not recounted as stale')
  // fixed in cycle 1, then called invalid: a reversal the challenger cannot settle is held
  cycle = 0
  const reopened = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => ++cycle === 1 ? { findings: [finding()], replies: [], bots: 'reviewed' }
      : { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    challenge: () => ({ verdict: 'unknown', reason: 'cannot tell' }),
  })
  assert.ok(reopened.logs.some(l => /held — contradicts the earlier valid verdict on 1#1/.test(l)), reopened.logs.join('\n'))
  assert.deepEqual([reopened.result.rollup.cycles, reopened.result.rollup.findings.fixed, reopened.result.rollup.findings.held], [[1, 2], 0, 1],
    'a fixed finding the last cycle holds again is held, not fixed')
})

test('a claim already containing a backslash-pipe stays one cell', async () => {
  const { logs } = await run({
    reviews: { findings: [finding({ claim: 'the regex \\| splits the row' })], replies: [], bots: 'reviewed' },
  })
  const cells = gfmCells(summaries(logs)[0].split('\n')[3])
  assert.equal(cells.length, 5, 'the row keeps exactly its five columns')
  assert.equal(cells[1], 'src/a.c:1 the regex \\| splits the row', 'and renders the backslash and pipe literally')
})

test('a batch whose build fails is not published', async () => {
  const { result, logs, labels, calls } = await run({
    reviews: oneValid, fix: { buildOk: false, notes: 'uncovered: src/class/bth/bth_device.c' },
  })
  assert.equal(result.pass, false)
  assert.equal(result.reason, 'fix-verification-failed')
  assert.equal(labels.some(l => l.startsWith('push#')), false, 'the publisher is not dispatched')
  assert.ok(labels.includes('check:src/a.c'), 'the issue check runs whatever the writer\'s own build said')
  assert.deepEqual(labels.filter(l => /^build[:#]/.test(l)), ['build:resolve#1-review', 'build#1-review'])
  assert.match(calls.find(c => c.label === 'build#1-review').prompt, /build\.py --path='src\/a\.c' --command='make -C <BUILD> all'`/)
  assert.match(rowsOf(summaries(logs)[0])[0][3], /unverified: the build failed \(exit 1\); log \/tmp\/cand/)
  assert.equal(labels.some(l => l.startsWith('compat#')), false, 'no compatibility check on a broken batch')
})

test('the batch build is resolved from the contract when the caller named none, and is the caller\'s otherwise', async () => {
  const resolved = await run({ reviews: twoValid })
  const resolve = resolved.calls.find(c => c.label === 'build:resolve#1-review')
  assert.match(resolve.prompt, /for a change to src\/a\.c, src\/b\.c\./)
  assert.match(resolved.calls.find(c => c.label === 'build#1-review').prompt, /build\.py --path='src\/a\.c' --path='src\/b\.c' --command='make -C <BUILD> all'`/)
  assert.equal(resolved.result.history[0].reviewPush.pass, true)
  const given = await run({ reviews: oneValid, args: { build: "make BOARD='x y' <BUILD>" } })
  assert.equal(given.labels.some(l => l.startsWith('build:resolve#')), false)
  assert.match(given.calls.find(c => c.label === 'build#1-review').prompt, /--command='make BOARD='\\''x y'\\'' <BUILD>'`/)
  assert.equal(given.result.history[0].reviewPush.pass, true, 'the receipt echoes the quoted command back')
})

test('a build plan resolved for one path set is reused by a later cycle, and a new set is resolved', async () => {
  let cycle = 0
  const { labels } = await run({
    args: { autoPush: true, maxCycles: 3 },
    reviewsPerCycle: () => {
      cycle++
      return { findings: [finding({ commentId: cycle, line: cycle, file: cycle < 3 ? 'src/a.c' : 'src/b.c' })], replies: [], bots: 'reviewed' }
    },
  })
  assert.deepEqual(labels.filter(l => l.startsWith('build:resolve#')), ['build:resolve#1-review', 'build:resolve#3-review'])
  assert.ok(labels.includes('build#2-review'), 'the reused plan still builds')
  cycle = 0
  const edited = await run({
    args: { autoPush: true, maxCycles: 2 }, buildPlan: { contract: ['src/a.c'] },
    reviewsPerCycle: () => ({ findings: [finding({ commentId: ++cycle, line: cycle, file: 'src/a.c' })], replies: [], bots: 'reviewed' }),
  })
  assert.deepEqual(edited.labels.filter(l => l.startsWith('build:resolve#')), ['build:resolve#1-review', 'build:resolve#2-review'],
    'a plan read from a file the fixes change is resolved again')
})

test('a lost commit receipt is settled by a HEAD that moved, and stays unknown otherwise', async () => {
  // An error proves no outcome, since the relay may not have seen git run; HEAD does.
  const commit = { error: 'the message on stdin is not UTF-8', committed: false, detail: '' }
  const still = await run({ reviews: oneValid, commit, audit: { sha: HEAD } })
  assert.equal(still.result.reason, 'push-failed')
  // An unmoved HEAD proves nothing: the relay may report an error while git still runs.
  assert.equal(still.result.history[0].reviewPushFailed.committed, null)
  assert.equal(still.result.history[0].reviewPushFailed.detail, 'the message on stdin is not UTF-8; HEAD is still 0f1e2d3, but the committer may not have finished')
  const moved = await run({ reviews: oneValid, commit })
  assert.ok(moved.labels.some(l => l.startsWith('push#')), 'the commit HEAD moved to is audited and pushed')
  assert.equal(moved.result.history[0].reviewPushFailed, undefined)
  assert.ok(moved.logs.some(l => l.endsWith(`the message on stdin is not UTF-8, but HEAD moved to ${shaFor(1).slice(0, 7)}; auditing it as this cycle's commit`)), moved.logs.join('\n'))
  const unread = await run({ reviews: oneValid, commit, audit: null })
  assert.equal(unread.result.history[0].reviewPushFailed.committed, null)
  assert.equal(unread.result.history[0].reviewPushFailed.detail, 'the message on stdin is not UTF-8; the audit agent died too')
})

test('the committer gets the claims without their hints, and the message on stdin', async () => {
  const { calls } = await run({ reviews: { findings: [finding({ fixHint: 'Prompt for AI agents: rewrite it all' })], replies: [], bots: 'reviewed' }, args: { autoPush: true } })
  const commit = calls.find(c => c.label === 'commit#1-review').prompt
  assert.doesNotMatch(commit, /hint:|rewrite it all/)
  assert.match(commit, /The fixes: \["src\/a\.c:\d+ /)
  assert.match(commit, /commits\.py commit 'src\/a\.c' < <that file>; rm -f <that file>`/)
})

test('an unresolved build contract, or a build that did not run, blocks the batch', async () => {
  for (const [over, why] of [
    [{ buildPlan: { error: 'no build docs' } }, /build contract not resolved: no build docs/],
    [{ buildPlan: null }, /build contract not resolved: resolver died/],
    [{ candidate: null }, /the build did not count: agent died/],
    [{ candidate: { error: 'usage: ...' } }, /the build did not count: usage/],
    [{ candidate: { exit: 2 } }, /the build failed \(exit 2\)/],
  ]) {
    const { result, labels, logs } = await run({ reviews: oneValid, ...over })
    assert.equal(result.reason, 'fix-verification-failed', JSON.stringify(over))
    assert.equal(labels.some(l => /^(compat#|push#)/.test(l)), false)
    assert.ok(logs.some(l => why.test(l)), `${why}\n${logs.join('\n')}`)
  }
})

test('a build receipt counts only as the run that was asked for', async () => {
  for (const [candidate, why] of [
    [{ revision: 'f'.repeat(40) }, /the receipt is for fffffff, not /],
    [{ command: 'make other' }, /the receipt is for another command/],
    [{ snapshotAfter: 'other' }, /the build changed src\/a\.c, which were verified before it/],
  ]) {
    const { result, logs } = await run({ reviews: oneValid, candidate })
    assert.equal(result.reason, 'fix-verification-failed', JSON.stringify(candidate))
    assert.ok(logs.some(l => why.test(l)), `${why}\n${logs.join('\n')}`)
  }
})

test('a batch no build applies to is published with the reason logged', async () => {
  const { result, labels, logs } = await run({ reviews: oneValid, buildPlan: { command: null, reason: 'docs only' } })
  assert.equal(result.history[0].reviewPush.pass, true)
  assert.equal(labels.some(l => l.startsWith('build#')), false)
  assert.ok(logs.some(l => /build#1-review: no build applies — docs only/.test(l)))
})

test('writers whose own builds passed still do not publish a combined candidate that fails', async () => {
  const { result, labels } = await run({ reviews: twoValid, candidate: { exit: 2 } })
  assert.equal(result.reason, 'fix-verification-failed')
  assert.ok(labels.includes('build#1-review'))
  assert.equal(labels.some(l => l.startsWith('push#')), false)
  const rejected = await run({ reviews: oneValid, candidate: { exit: 1 }, verify: { addresses: false, reason: 'misses the point' } })
  assert.equal(rejected.result.reason, 'fix-verification-failed')
  assert.equal(rejected.labels.some(l => /^build[:#]/.test(l)), false, 'nothing is built for a batch its checks reject')
})

test('a build directory left behind is logged', async () => {
  const { logs } = await run({ reviews: oneValid, candidate: { retained: ['/tmp/b'] } })
  assert.ok(logs.some(l => /build#1-review: cleanup left \/tmp\/b/.test(l)))
})

test('a resumed launch may change the build, keeping the debt and the budget', async () => {
  const first = await run({
    args: { build: 'make a', maxCycles: 3, yieldAfterCycle: true },
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' }, dropDoneIds: () => true,
  })
  assert.equal(first.result.state.build, 'make a')
  assert.equal('build' in first.result.state.config, false)
  const { result, logs } = await run({
    args: { build: 'make b', maxCycles: 3, yieldAfterCycle: true, state: first.result.state },
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' }, dropDoneIds: () => true,
  })
  assert.ok(logs.some(l => l === 'build changed since the last launch: "make a" → "make b"'))
  assert.equal(result.state.cyclesUsed, 2)
  assert.ok(result.state.debt.find(([id]) => id === 1))
})

test('a resumed launch may raise the cycle ceiling, keeping the cycles used', async () => {
  const reviews = { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' }
  const first = await run({ args: { maxCycles: 1, yieldAfterCycle: true }, reviews, dropDoneIds: () => true })
  const spent = await run({ args: { maxCycles: undefined, yieldAfterCycle: true, state: first.result.state }, reviews, dropDoneIds: () => true })
  assert.equal(spent.result.reason, 'budget-exhausted', 'an omitted ceiling is the state\'s, not the default')
  const { result, logs } = await run({ args: { maxCycles: 3, yieldAfterCycle: true, state: first.result.state }, reviews, dropDoneIds: () => true })
  assert.ok(logs.includes('cycle ceiling changed since the last launch: 1 → 3, 1 used'))
  assert.equal(result.state.cyclesUsed, 2)
  assert.equal(result.state.maxCycles, 3)
})

test('a dead code-writer withholds the fix', async () => {
  const { result, logs, labels } = await run({ reviews: oneValid, fix: null })
  assert.equal(result.reason, 'fix-verification-failed')
  assert.equal(labels.some(l => l.startsWith('push#')), false)
  assert.ok(logs.some(l => /lost to dead workers/.test(l)))
  assert.match(rowsOf(summaries(logs)[0])[0][3], /withheld/)
})

test('a fix the verifier rejects is reported unverified, not pushed', async () => {
  const { result, logs, labels, calls } = await run({
    reviews: oneValid, verify: { addresses: false, reason: 'does not address the claim' },
  })
  assert.equal(result.reason, 'fix-verification-failed')
  assert.equal(labels.some(l => l.startsWith('push#')), false)
  assert.match(rowsOf(summaries(logs)[0])[0][3], /unverified: does not address the claim/)
  assert.equal(calls.find(c => c.label.startsWith('check:')).agentType, 'finding-verifier')
})

// Two valid findings in two files: two writers, two verifiers.
const twoValid = { findings: [finding(), finding({ commentId: 2, file: 'src/b.c', line: 2 })], replies: [], bots: 'reviewed' }
const outcomes = (logs) => rowsOf(summaries(logs)[0]).map(r => r[3])

test('a dead or thrown verifier leaves only its own group unverified', async () => {
  for (const opts of [{ verify: (label) => label === 'check:src/a.c' ? null : { addresses: true, reason: 'ok' } }, { throwOn: 'check:src/a.c' }]) {
    const { result, logs, labels } = await run({ reviews: twoValid, ...opts })
    assert.equal(result.reason, 'fix-verification-failed')
    assert.equal(labels.some(l => l.startsWith('push#')), false)
    assert.match(outcomes(logs)[0], /unverified: verifier died/)
    assert.match(outcomes(logs)[1], /^fixed/)
  }
})

test('a thrown writer is a dead one: its group is withheld and the survivor is still verified', async () => {
  const { result, calls, logs } = await run({
    reviews: twoValid,
    fix: async (label) => { if (label === 'fix:src/a.c') throw new Error('writer exploded'); return {} },
  })
  assert.notEqual(result.reason, 'cycle-threw')
  assert.ok(logs.some(l => /1 fix group\(s\) lost to dead workers/.test(l)))
  assert.deepEqual(calls.filter(c => c.label.startsWith('check:')).map(c => c.label), ['check:src/b.c'])
  assert.match(outcomes(logs)[0], /withheld/)
  assert.match(outcomes(logs)[1], /^fixed/)
})

test('every live writer is verified, and one dead group blocks the batch before it is built', async () => {
  const { result, calls, logs, labels } = await run({
    reviews: { findings: [...twoValid.findings, finding({ commentId: 3, file: 'src/c.c', line: 3 })], replies: [], bots: 'reviewed' },
    fix: (label) => label === 'fix:src/a.c' ? null : label === 'fix:src/b.c' ? { buildOk: false, notes: 'boom' } : {},
  })
  assert.deepEqual(calls.filter(c => c.label.startsWith('check:')).map(c => c.label).sort(), ['check:src/b.c', 'check:src/c.c'])
  assert.equal(result.reason, 'fix-verification-failed')
  assert.equal(labels.some(l => /^(build[:#]|push#)/.test(l)), false)
  assert.match(outcomes(logs)[0], /withheld/)
})

test('a batch that breaks, or may break, code relying on it is not published', async () => {
  for (const [compat, why] of [
    [{ compatible: false, evidence: 'test/hil/mtp_raw.py expects 0x2001' }, /unverified: breaks code that relies on it: test\/hil/],
    [{ compatible: null, evidence: 'no search ran' }, /unverified: compatibility not established: no search ran/],
    [null, /unverified: compatibility verifier died/],
  ]) {
    const { result, logs, labels } = await run({ reviews: twoValid, compat })
    assert.equal(result.reason, 'fix-verification-failed')
    assert.equal(labels.some(l => /^(recheck|commit|push)#/.test(l)), false, 'nothing is committed or pushed')
    for (const o of outcomes(logs)) assert.match(o, why, 'every fix in the batch carries the reason')
  }
  const { result } = await run({ reviews: twoValid, throwOn: 'compat#' })
  assert.equal(result.reason, 'fix-verification-failed', 'a thrown verifier is a dead one')
})

test('one compatibility check per batch sees every change and the writers\' notes, before anything is committed', async () => {
  const { result, calls, labels } = await run({
    reviews: twoValid, fix: (label) => ({ notes: `changed the status code in ${label.slice(4)}` }),
  })
  assert.equal(result.history[0].reviewPush.sha, shaFor(1), 'the checked batch is published')
  const compat = calls.filter(c => c.label.startsWith('compat#'))
  assert.deepEqual(compat.map(c => c.label), ['compat#1-review'])
  assert.match(compat[0].prompt, /src\/a\.c, src\/b\.c/)
  assert.match(compat[0].prompt, /changed the status code in src\/a\.c[\s\S]*changed the status code in src\/b\.c/)
  assert.ok(labels.indexOf('compat#1-review') < labels.indexOf('recheck#1-review'))
})

test('consumers inside the batch\'s own files are searched too', async () => {
  const { calls } = await run({ reviews: oneValid })
  const prompt = calls.find(c => c.label.startsWith('compat#')).prompt
  assert.match(prompt, /search the whole repository, those paths included/)
})

test('the CI lane gets its own compatibility check', async () => {
  const { calls } = await run({
    args: { lane: 'ci', yieldAfterCycle: true, maxCycles: 3 },
    ci: { status: 'red', infraRerun: [], realFailures: [{ check: 'build / arm', firstError: 'boom', files: ['src/a.c'], verdict: 'real' }] },
  })
  assert.deepEqual(calls.filter(c => c.label.startsWith('compat#')).map(c => c.label), ['compat#1-ci'])
})

test('a batch already failing its own checks pays for no compatibility check', async () => {
  const { labels } = await run({ reviews: oneValid, verify: { addresses: false, reason: 'no' } })
  assert.equal(labels.some(l => l.startsWith('compat#')), false)
})

test('the writer keeps outside expectations and reports the change they would need', async () => {
  const { calls } = await run({ reviews: oneValid })
  assert.match(calls.find(c => c.label.startsWith('fix:')).prompt,
    /never edit it or its assertion to fit; report the change it would need as out of scope/)
})

test('groups the overlap merge joins get one writer and one verifier, and every finding keeps its row', async () => {
  // Two CI failures with different scope keys that both name src/a.c: two groups
  // from groupWork, one after the merge.
  const { result, calls, logs } = await run({
    args: { lane: 'ci', yieldAfterCycle: true, maxCycles: 3 },
    ci: { status: 'red', infraRerun: [], realFailures: [
      { check: 'build / arm', firstError: 'boom in a', files: ['src/a.c'], verdict: 'real' },
      { check: 'build / riscv', firstError: 'boom in the bsp', files: ['hw/bsp/x/board.c', 'src/a.c'], verdict: 'real' },
    ] },
  })
  assert.equal(calls.filter(c => c.label.startsWith('fix:')).length, 1)
  const check = calls.filter(c => c.label.startsWith('check:'))
  assert.equal(check.length, 1)
  assert.match(check[0].prompt, /boom in a[\s\S]*boom in the bsp/, 'both issues in the one verification')
  assert.equal(result.history[0].ciFixes.length, 1)
  assert.equal(result.history[0].ciFixes[0].ids.length, 2, 'both original ids ride on the one fix')
  assert.deepEqual(outcomes(logs).map(o => /^fixed/.test(o)), [true, true])
})

test('a dry run leaves the fix uncommitted', async () => {
  const { result, logs, labels } = await run({ reviews: oneValid, args: { autoPush: false } })
  assert.equal(result.dryRun, true)
  assert.equal(labels.some(l => l.startsWith('push#') || l.startsWith('replies#')), false)
  assert.match(rowsOf(summaries(logs)[0])[0][3], /fixed, uncommitted/)
})

test('a dry run dispatches no publisher and no posting agent', async () => {
  // The harness default is autoPush: true — the workflow's own default is the
  // dry run, and this is what that authorization withholds.
  const { result, labels } = await run({
    args: { autoPush: false },
    reviews: {
      findings: [finding({ commentId: 1 }), invalidFinding({ commentId: 2, line: 4 })],
      replies: [{ commentId: 2, body: 'no' }], bots: 'reviewed',
    },
  })
  assert.equal(result.dryRun, true)
  for (const l of labels) {
    assert.ok(!/^(push#|recheck#|replies#|resolve#)/.test(l), `${l} ran in a dry run`)
  }
})

test('a change to a protected path is dropped from scope, and a group needing only it is left red', async () => {
  const { result, logs, labels } = await run({
    args: { protected: '^test/hil/[^/]+\\.json$' },
    reviews: { findings: [finding({ file: 'test/hil/tinyusb.json' })], replies: [], bots: 'reviewed' },
  })
  assert.equal(result.reason, 'fix-verification-failed')
  assert.equal(labels.some(l => l.startsWith('fix:')), false, 'no fixer may be dispatched for a protected path')
  assert.ok(logs.some(l => /test\/hil\/tinyusb\.json is protected — dropped from scope/.test(l)))
  assert.ok(logs.some(l => /only a protected path would address it — leaving red for the user/.test(l)))
  assert.match(rowsOf(summaries(logs)[0])[0][3], /withheld/)
})

test('the publisher stages exactly the owned paths, never a protected one', async () => {
  const { result, calls } = await run({
    args: { protected: 'rig\\.json$' },
    reviews: {
      findings: [finding({ commentId: 1, file: 'hw/bsp/stm32f4/family.c' }),
        finding({ commentId: 2, file: 'hw/bsp/stm32f4/rig.json' })],
      replies: [], bots: 'reviewed',
    },
  })
  const fix = calls.find(c => c.label.startsWith('fix:'))
  assert.ok(fix.prompt.includes('never modify a path matching rig\\.json$'))
  // Staging is the commit agent's turn now; the push agent only ships a made SHA.
  const commit = calls.find(c => c.label === 'commit#1-review')
  assert.ok(commit)
  assert.deepEqual(pathLine(commit.prompt), ['hw/bsp/stm32f4/family.c'], 'commits.py commits exactly these; a protected path must never reach the index')
  assert.match(commit.prompt, /no trailer or line crediting an agent, model, tool or session/)
  assert.match(commit.prompt, /Do not push\. Change nothing else/)
  assert.match(commit.prompt, /On branch claude\/foo/)
  const push = calls.find(c => c.label === 'push#1-review')
  // The exact refspec is the point: pushing the branch would publish whatever
  // HEAD became after the audit, not the commit that was audited.
  assert.ok(push.prompt.includes(`push.py --remote 'origin' --branch 'claude/foo' --sha ${shaFor(1)} --push-url 'git@github.com:hathach/tinyusb.git'\``), push.prompt)
  assert.match(push.prompt, /Committing, amending and forcing nothing/)
  // The stage cannot commit and already knows the SHA, so it is asked for neither.
  assert.deepEqual(push.schema.required, ['pushed', 'detail', 'heads'])
  assert.equal(result.history[0].reviewPush.sha, shaFor(1))
})

test('the fixer is told to stage nothing, and how to verify', async () => {
  const { calls } = await run({ reviews: oneValid })
  const fix = calls.find(c => c.label.startsWith('fix:'))
  assert.equal(fix.agentType, 'code-writer')
  assert.match(fix.prompt, /Do not push, create a PR, or post an issue or PR comment\./)
  assert.match(fix.prompt, /Do not stage or commit: leave your changes in the working tree for this workflow to publish\./)
  assert.match(fix.prompt, /Verify with the repository's build contract, resolved for your scope; do not invent a command\./)
  assert.doesNotMatch(fix.prompt, /cmake|get_deps|BOARD=/, 'no repository-specific recipe is baked in')
  // Read the expected keys from code-writer's own output contract. A list
  // hardcoded here pins whatever the schema happens to say, which is how `board`
  // came to be rejected: the role always returns it, and this schema forbade it.
  const roleKeys = [...readFileSync(new URL('../agents/code-writer.md', import.meta.url), 'utf8')
    .match(/^\{"item".*\}$/m)[0].matchAll(/"(\w+)":/g)].map(m => m[1]).sort()
  assert.deepEqual(fix.schema.required.slice().sort(), roleKeys)
  const built = await run({ reviews: oneValid, args: { build: '  make check  ' } })
  const fix2 = built.calls.find(c => c.label.startsWith('fix:'))
  assert.match(fix2.prompt, /Verify with: make check \(a `<BUILD>` placeholder becomes a fresh `mktemp -d`\)\./)
  assert.doesNotMatch(fix2.prompt, /build contract/)
})

test('the CI lane waits out its budget in slices, 30 minutes by default', async () => {
  const running = { status: 'running', infraRerun: [], realFailures: [] }
  const slices = (calls) => calls.filter(c => c.label.startsWith('ci:collect#1.'))
    .map(c => Number(c.prompt.match(/collect\.py inventory --wait-seconds (\d+) --repo 'hathach\/tinyusb' --pr 3888 --head [0-9a-f]{40}`/)[1]))
  const { calls } = await run({ ci: running, args: { maxCycles: 1 } })
  assert.equal(slices(calls).reduce((a, b) => a + b), 30 * 60)
  assert.equal(slices(calls)[0], 180, 'short while the review lane may still push')
  assert.equal(calls.find(c => c.label === 'ci:collect#1.1').model, 'haiku')
  const long = await run({ ci: running, args: { ciWait: 90, maxCycles: 1 } })
  assert.equal(slices(long.calls).reduce((a, b) => a + b), 90 * 60)
  const ciOnly = await run({ ci: running, args: { lane: 'ci', yieldAfterCycle: true, maxCycles: 1 } })
  assert.equal(slices(ciOnly.calls)[0], 540, 'no review lane to wait for')
  const green = await run()
  assert.deepEqual(green.labels.filter(l => l.startsWith('ci:')), ['ci:collect#1.1'], 'a settled inventory needs no judge')
})

test('a path whose name has a leading or trailing space is rejected, not trimmed', async () => {
  // Git allows both; trimming would quietly name a different file.
  const { calls } = await run({
    ci: {
      status: 'red', infraRerun: [],
      realFailures: [{ check: 'build / arm', firstError: 'the log named no files', files: [], verdict: 'real' }],
    },
    scope: [' src/lead.c', 'src/trail.c ', 'src/keep me.c'],
  })
  const ls = calls.find(c => c.label === 'scope:verify')
  assert.match(ls.prompt, /git -c core\.quotePath=false ls-files -- 'src\/keep me\.c'\n/, 'an interior space is still a legal path')
  assert.equal(ls.prompt.includes('lead.c'), false)
  assert.equal(ls.prompt.includes('trail.c'), false)
})

test('the scoper offers every candidate to git, and keeps only the paths it knows', async () => {
  // `git ls-files` is what decides a path is real; canon only normalises spelling.
  // So the invented path has to be one the stub withholds: if the workflow stopped
  // intersecting candidates with the ls-files output, `src/invented.c` would reach
  // the fixer and this would fail.
  const { calls } = await run({
    ci: {
      status: 'red', infraRerun: [],
      realFailures: [{ check: 'build / arm', firstError: 'the log named no files', files: [], verdict: 'real' }],
    },
    scope: ['src/my file (v2).c', 'src/./plus+@~[1].c', 'src/nope/../plus+@~[1].c', 'src/invented.c'],
    lsFiles: (offered) => offered.filter(f => f !== 'src/invented.c'),
  })
  const ls = calls.find(c => c.label === 'scope:verify')
  // Offered: both spellings of plus+@~[1].c collapsed to one, and the invented path too.
  assert.match(ls.prompt, /git -c core\.quotePath=false ls-files -- 'src\/my file \(v2\)\.c' 'src\/plus\+@~\[1\]\.c' 'src\/invented\.c'\n/)
  const fix = calls.find(c => c.label.startsWith('fix:'))
  assert.match(fix.prompt, /Scope: src\/my file \(v2\)\.c, src\/plus\+@~\[1\]\.c/)
  assert.equal(fix.prompt.includes('invented'), false, 'a path git did not confirm never reaches the fixer')
})

test('two matrix legs of one check name keep separate fixes', async () => {
  const { logs } = await run({
    ci: {
      status: 'red', infraRerun: [],
      realFailures: [
        { check: 'build / arm', firstError: 'error in stm32f4', files: ['hw/bsp/stm32f4/family.c'], verdict: 'real' },
        { check: 'build / arm', firstError: 'error in nrf', files: ['hw/bsp/nrf/family.c'], verdict: 'real' },
      ],
    },
  })
  const rows = rowsOf(summaries(logs)[0])
  assert.match(rows[0][3], /stat:hw\/bsp\/stm32f4/)
  assert.match(rows[1][3], /stat:hw\/bsp\/nrf/, 'the second leg must not inherit the first fix')
})

test('a rig-side CI failure is left red, with no fix and no commit', async () => {
  const { result, logs, labels } = await run({
    reviews: { findings: [], replies: [], bots: 'reviewed' },
    ci: {
      status: 'red', infraRerun: [],
      realFailures: [{ check: 'hil / pico', firstError: 'board did not enumerate', files: [], verdict: 'rig-side' }],
    },
  })
  assert.equal(result.reason, 'ci-red-rig-side')
  assert.equal(labels.some(l => l.startsWith('fix:')), false)
  const row = rowsOf(summaries(logs)[0])[0]
  assert.deepEqual([row[2], row[3], row[4]], ['rig-side', 'left red for the rig', '-'])
})

const UNPLACED = { check: 'PVS-Studio (raspberry_pi_pico)', firstError: 'Analysis finished, then exit 2 after "Your license will expire in 28 days"; no diagnostic; no other run of this job in the last day', files: [], verdict: 'unclassified' }

test('an unclassified CI failure is left red with its evidence, no fix, and an honest stop', async () => {
  const { result, logs, labels } = await run({
    reviews: { findings: [], replies: [], bots: 'reviewed' },
    ci: { status: 'red', infraRerun: [], realFailures: [UNPLACED] },
  })
  assert.equal(result.reason, 'ci-red-unclassified')
  assert.equal(labels.some(l => l.startsWith('fix:') || l.startsWith('scope:')), false)
  const row = rowsOf(summaries(logs)[0])[0]
  assert.deepEqual([row[2], row[3], row[4]], ['unclassified', 'left red: not placed by its evidence', '-'])
  assert.match(row[1], /license will expire/)
  assert.ok(logs.some(l => /could not place — no justified fix; investigate/.test(l)))
})

test('beside a real CI failure only the real one is fixed; the unclassified row gets no commit', async () => {
  const { result, logs, labels } = await run({
    reviews: { findings: [], replies: [], bots: 'reviewed' },
    ci: { status: 'red', infraRerun: [], realFailures: [
      { check: 'build / arm', firstError: 'error in src/a.c', files: ['src/a.c'], verdict: 'real' },
      { ...UNPLACED, files: ['hw/bsp/family_support.cmake'] }, // a file named without a diagnostic is still not a scope
    ] },
  })
  assert.deepEqual(labels.filter(l => l.startsWith('fix:')), ['fix:src/a.c'])
  assert.ok(labels.includes('push#1-ci'), 'the real fix is published')
  const rows = rowsOf(summaries(logs)[0])
  assert.match(rows[0][3], /^fixed/)
  assert.notEqual(rows[0][4], '-')
  assert.deepEqual([rows[1][2], rows[1][3], rows[1][4]], ['unclassified', 'left red: not placed by its evidence', '-'])
  assert.notEqual(result.pass, true)
})

test('the judge places every failing check once, or the lane re-arms', async () => {
  const two = { status: 'red', infraRerun: [], realFailures: [UNPLACED, { ...UNPLACED, check: 'build / b' }] }
  const judge = await run({ ci: two, args: { maxCycles: 1 } })
  assert.match(judge.calls.find(c => c.label === 'ci:collect#1.f').prompt,
    / failures --check 'https:\/\/github\.com\/o\/r\/actions\/runs\/1\/job\/1' --check 'https:\/\/github\.com\/o\/r\/actions\/runs\/1\/job\/2' --repo /)
  assert.match(judge.calls.find(c => c.label === 'ci:judge#1').prompt, /evidence for them is in \/tmp\/ci-collect\/failures-1\.json\./)
  assert.doesNotMatch(judge.calls.find(c => c.label === 'ci:judge#1').prompt, /Already re-run/)
  const asked = judge.calls.find(c => c.label === 'ci:judge#1').schema.properties.checks
  assert.deepEqual([asked.minItems, asked.maxItems, asked.items.properties.link.enum],
    [2, 2, ['https://github.com/o/r/actions/runs/1/job/1', 'https://github.com/o/r/actions/runs/1/job/2']])
  assert.throws(() => conforms(asked, [{ link: 'https://github.com/o/r/actions/runs/1/job/1', failures: [] }], 'c'), /1 items/)
  // The schema lets no short answer through; one link twice still counts two.
  const { result, logs, labels } = await run({ ci: two, judge: j => ({ ...j, checks: [j.checks[0], j.checks[0]] }), args: { maxCycles: 1 } })
  assert.ok(logs.some(l => /CI judge answered .* — re-arming/.test(l)), logs.join('\n'))
  assert.ok(logs.some(l => /CI lane gave no report — re-arming/.test(l)))
  assert.equal(labels.some(l => l.startsWith('fix:')), false)
  assert.notEqual(result.pass, true)
})

test('an unclassified failure stops at once, reviews settled or not, and the debt survives a resumed launch', async () => {
  const pending = await run({
    args: { autoPush: true, maxCycles: 3 },
    reviews: { findings: [], replies: [], bots: 'pending' },
    ci: { status: 'red', infraRerun: [], realFailures: [UNPLACED] },
  })
  assert.equal(pending.result.reason, 'ci-red-unclassified', 'not reviews-pending: another cycle meets the same exit')
  assert.equal(pending.result.cycles, 1)
  const rig = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviews: { findings: [], replies: [], bots: 'pending' },
    ci: { status: 'red', infraRerun: [], realFailures: [{ check: 'hil / pico', firstError: 'board did not enumerate', files: [], verdict: 'rig-side' }] },
  })
  assert.notEqual(rig.result.reason, 'ci-red-rig-side', 'a rig-side failure alone still waits for the reviews')
  const stop = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true },
    reviews: owing, challenge: upheld,
    ci: { status: 'red', infraRerun: [], realFailures: [UNPLACED] },
  })
  const resumed = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: stop.result.state },
    reviews: { findings: [], replies: [], bots: 'reviewed' },
    ci: { status: 'red', infraRerun: [], realFailures: [UNPLACED] },
  })
  assert.deepEqual(resumed.result.state.debt.map(([id]) => id), [5], 'the owed reply is still owed after the stop and resume')
})

test('a CI reply in the old rigSide shape is a dead watcher, never a fix', async () => {
  // The runtime's schema check refuses the reply and the workflow reads that as a
  // watcher that died: the cycle re-arms, nothing is fixed on the stale shape.
  for (const [failure, why] of [
    [{ check: 'x', firstError: 'y', files: ['src/a.c'], rigSide: false }, /missing verdict/], // the first violation named; rigSide is the second
    [{ check: 'x', firstError: 'y', files: ['src/a.c'], verdict: 'real', rigSide: false }, /unexpected rigSide/],
    [{ check: 'x', firstError: 'y', files: ['src/a.c'], verdict: 'maybe' }, /not in real,rig-side,unclassified/],
    [{ check: 'x', firstError: 'y', files: ['src/a.c'] }, /missing verdict/],
  ]) {
    const { result, logs, labels } = await run({ ci: { status: 'red', infraRerun: [], realFailures: [failure] } })
    assert.ok(logs.some(l => /CI judge errored/.test(l) && why.test(l)), `${JSON.stringify(failure)} refused`)
    assert.equal(labels.some(l => l.startsWith('fix:')), false)
    assert.notEqual(result.pass, true)
  }
})

test('a check the judge re-ran is settling until its re-run registers, and never re-run twice', async () => {
  const two = { status: 'red', infraRerun: ['35171132943'], realFailures: [UNPLACED, { ...UNPLACED, check: 'build / b' }] }
  const rerunFirst = j => ({ ...j, checks: [{ ...j.checks[0], failures: [] }, ...j.checks.slice(1)] })
  // a newer base run, so the unclassified check is judged again in cycle 2
  const { calls, logs } = await run({ ci: two, judge: rerunFirst, args: { maxCycles: 2 }, base: (label) => label.startsWith('ci:collect#2') ? 'b2' : 'b1' })
  assert.doesNotMatch(calls.find(c => c.label === 'ci:judge#1').prompt, /Already re-run/)
  const second = calls.find(c => c.label === 'ci:judge#2').prompt
  assert.match(second, /\nAlready re-run on this head: \["ci \/ PVS-Studio \(raspberry_pi_pico\)"\]\./)
  assert.doesNotMatch(second, /job\/1"/, 'the old run of the re-run check is not judged again')
  assert.match(calls.find(c => c.label === 'ci:collect#2.f').prompt, / failures --check 'https:\/\/github\.com\/o\/r\/actions\/runs\/1\/job\/2' --repo /)
  assert.ok(logs.some(l => /CI still settling/.test(l)))
  const alone = await run({ ci: { ...two, realFailures: [UNPLACED] }, judge: j => ({ ...j, checks: [{ ...j.checks[0], failures: [] }] }), args: { maxCycles: 2 } })
  assert.equal(alone.labels.includes('ci:judge#2'), false, 'nothing left to judge while the re-run registers')
  const bare = await run({ ci: { ...two, infraRerun: [], realFailures: [UNPLACED] }, judge: j => ({ ...j, checks: [{ ...j.checks[0], failures: [] }] }), args: { maxCycles: 1 } })
  assert.ok(bare.logs.some(l => /re-ran 1 check\(s\) without a receipt/.test(l)))
  assert.equal(bare.result.history[0].ci.status, 'running', 'still settling, never an unactionable red')
  assert.notEqual(bare.result.reason, 'unactionable')
  // a push moves the head: the same check fails there for the first time
  const pushed = await run({ ci: two, judge: rerunFirst, reviews: oneValid, args: { autoPush: true, maxCycles: 2 } })
  const onNew = pushed.calls.filter(c => c.label.startsWith('ci:judge#')).at(-1)
  assert.doesNotMatch(onNew.prompt, /Already re-run/, onNew.label)
  assert.match(onNew.prompt, /job\/1"/, 'and it is judged, not settling')
})

test('an inventory whose status or head contradicts its checks is never read as green', async () => {
  for (const ci of [{ status: 'green', infraRerun: [], realFailures: [UNPLACED] }, { ...GREEN, headSha: 'f'.repeat(40) }]) {
    const { result, labels, logs } = await run({ ci, args: { maxCycles: 1 } })
    assert.ok(logs.some(l => /CI inventory is inconsistent/.test(l)), logs.join('\n'))
    assert.equal(labels.some(l => l.startsWith('ci:judge#')), false)
    assert.notEqual(result.pass, true)
  }
})

test('a review push that lands while the evidence is read supersedes the judge', async () => {
  const { labels, logs, result } = await run({
    reviews: oneValid, args: { autoPush: true, maxCycles: 1 },
    ci: { status: 'red', infraRerun: [], realFailures: [UNPLACED] },
    evidence: async (calls) => {
      for (let i = 0; i < 1000 && !calls.some(c => c.label.startsWith('push#')); i++) await new Promise(r => setTimeout(r, 0))
      for (let i = 0; i < 20; i++) await new Promise(r => setTimeout(r, 0))
    },
  })
  assert.ok(labels.includes('ci:collect#1.f'))
  assert.equal(labels.includes('ci:judge#1'), false)
  assert.ok(logs.some(l => /review-lane push superseded the CI run/.test(l)), logs.join('\n'))
  assert.deepEqual(result.rollup.ciChecks, { judged: 0, partial: 0, reused: 0, unchanged: 0 }, 'a check the judge never saw is not counted judged')
})

// CI verdicts: digests carried in the state, verdicts in collect.py's store

const RIG = { check: 'hil / pico', firstError: 'board did not enumerate', files: [], verdict: 'rig-side' }
const WAITING = { findings: [], replies: [], bots: 'pending' }
const YIELD = { autoPush: true, maxCycles: 3, yieldAfterCycle: true }
const ciLabels = (labels) => labels.filter(l => l.startsWith('ci:'))
const job = (n) => `https://github.com/o/r/actions/runs/1/job/${n}`

test('a same-head relaunch recalls the judged verdicts from the store: no judge', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(ciLabels(first.labels), ['ci:collect#1.1', 'ci:collect#1.f', 'ci:judge#1', 'ci:collect#1.w'])
  assert.deepEqual(Object.keys(first.result.state.ciCache.entries[0]).sort(), ['bucket', 'digest', 'head', 'link'], 'the state keeps digests only')
  const again = await run({ store, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(ciLabels(again.labels), ['ci:collect#2.1', 'ci:collect#2.r1'])
  assert.match(again.calls.find(c => c.label === 'ci:collect#2.r1').prompt, / recall --check 'https:\/\/github\.com\/o\/r\/actions\/runs\/1\/job\/1' --repo /)
  assert.equal(again.result.history.at(-1).ci.realFailures[0].firstError, 'board did not enumerate')
  assert.ok(again.logs.some(l => /CI verdicts reused for 1 check\(s\)/.test(l)))
  assert.deepEqual(again.result.state.ciCache.entries, first.result.state.ciCache.entries, 'a recalled verdict stays remembered')
  const st = first.result.state
  const older = seal({ ...st, ciCache: { ...st.ciCache, notesDigest: fnv1a('') } })
  const noted = await run({ store, args: { ...YIELD, state: older, ciNotes: 'the pico probe is flaky' }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(ciLabels(noted.labels), ['ci:collect#2.1', 'ci:collect#2.r1'], 'new ciNotes leave a stored verdict standing, in a state saved with notesDigest too')
})

test('the evidence of every head is read without a prior head: collect.py finds the other heads\' verdicts itself', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci })
  assert.doesNotMatch(first.calls.find(c => c.label === 'ci:collect#1.f').prompt, /--prior-head/)
  const st = first.result.state
  assert.deepEqual(Object.keys(st.ciCache).sort(), ['entries', 'reruns'])
  const older = await run({ store, args: { ...YIELD, state: seal({ ...st, ciCache: { ...st.ciCache, judgedHead: 'b'.repeat(40) } }) }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.equal(older.result.pass, false, 'an older state\'s judgedHead still loads')
  assert.deepEqual(Object.keys(older.result.state.ciCache).sort(), ['entries', 'reruns'])
})

test('the same head in a later cycle is not judged again, and an unclassified verdict is once its base job changed', async () => {
  const within = await run({ args: { autoPush: true, maxCycles: 2 }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(ciLabels(within.labels), ['ci:collect#1.1', 'ci:collect#1.f', 'ci:judge#1', 'ci:collect#1.w', 'ci:collect#2.1'])
  // an unclassified failure stops the launch at once; a relaunch after its base job changed reads it again
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG, UNPLACED).ci })
  assert.equal(first.result.reason, 'ci-red-unclassified')
  assert.deepEqual(trailingList(first.calls.find(c => c.label === 'ci:collect#1.w').prompt).map(v => v.link), ['https://github.com/o/r/actions/runs/1/job/1', 'https://github.com/o/r/actions/runs/1/job/2'])
  const { calls } = await run({ store, base: 'b2', args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG, UNPLACED).ci })
  const second = calls.find(c => c.label === 'ci:collect#2.f').prompt
  assert.doesNotMatch(second, /job\/1'/, 'the rig-side verdict is reused')
  assert.match(second, /job\/2'/, 'the unclassified one is read again')
})

const cellFailure = (cell, verdict, over = {}) => ({ check: 'hil / pico', workflow: 'ci', job: 'hil / pico', cell, signature: `${cell}: Failed`, runId: 1, complete: true, firstError: `${cell} failed`, files: [], verdict, ...over })
const MIXED = [cellFailure('pico a', 'rig-side'), cellFailure('pico b', 'unclassified'), cellFailure('pico c', 'rig-side')]
const judgeWith = (failures) => (j) => ({ ...j, checks: j.checks.map(c => ({ ...c, failures: structuredClone(failures) })) })
const judgePrompt = (calls, cycle) => calls.find(c => c.label === `ci:judge#${cycle}`).prompt

test('a stored check with an unclassified failure is judged again for that failure only, and merged in place', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })
  assert.equal(first.result.reason, 'ci-red-unclassified')
  assert.equal(trailingList(first.calls.find(c => c.label === 'ci:collect#1.w').prompt)[0].failures.length, 3, 'a mixed check is stored')
  const placed = { ...MIXED[1], verdict: 'rig-side', firstError: 'placed by the notes' }
  const again = await run({ store, args: { ...YIELD, state: first.result.state, ciNotes: 'pico b is the probe' }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith([placed]) })
  const [asked] = askedOf(judgePrompt(again.calls, 2))
  assert.deepEqual(asked.judgeOnly, [{ job: 'hil / pico', cell: 'pico b', signature: 'pico b: Failed' }])
  assert.deepEqual(asked.retained.map(r => [r.cell, r.verdict]), [['pico a', 'rig-side'], ['pico c', 'rig-side']])
  const report = again.result.history.at(-1).ci.realFailures
  assert.deepEqual(report.map(f => [f.cell, f.verdict, f.firstError]), [['pico a', 'rig-side', 'pico a failed'], ['pico b', 'rig-side', 'placed by the notes'], ['pico c', 'rig-side', 'pico c failed']])
  assert.notEqual(again.result.reason, 'ci-red-unclassified')
  assert.deepEqual(trailingList(again.calls.find(c => c.label === 'ci:collect#2.w').prompt), [{ link: 'https://github.com/o/r/actions/runs/1/job/1', bucket: 'fail', patch: [placed] }], 'stored as a patch of what changed')
  const settled = await run({ store, args: { ...YIELD, state: again.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.equal(settled.labels.includes('ci:judge#3'), false, 'then reused whole')
})

test('an unclassified failure is judged again only once ciNotes or its check\'s base run changed', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })
  assert.equal(first.result.state.ciCache.entries[0].placedWith, `${fnv1a('')}:b1`)
  const same = await run({ store, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(ciLabels(same.labels), ['ci:collect#2.1', 'ci:collect#2.r1', 'ci:collect#2.b'], 'its base job looked up, no evidence read, no judge')
  assert.ok(same.logs.some(l => /unclassified CI failures left as judged for hil \/ pico — ciNotes and base job unchanged/.test(l)))
  assert.deepEqual(same.result.history.at(-1).ci.realFailures.map(f => [f.cell, f.verdict]), MIXED.map(f => [f.cell, f.verdict]))
  assert.equal(same.result.reason, 'ci-red-unclassified')
  for (const over of [{ args: { ciNotes: 'pico b is the probe' } }, { base: 'b2' }, { base: null }]) {
    const again = await run({ store, ...over, args: { ...YIELD, state: first.result.state, ...over.args }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith([MIXED[1]]) })
    assert.ok(again.labels.includes('ci:judge#2'), JSON.stringify(over))
    assert.equal(again.labels.includes('ci:collect#2.b'), !over.args, 'new ciNotes need no base job lookup')
    assert.equal(again.result.state.ciCache.entries[0].placedWith, `${fnv1a(over.args?.ciNotes ?? '')}:${over.base === undefined ? 'b1' : over.base}`, 'and records what it was judged with')
  }
  const placed = await run({ store, args: { ...YIELD, state: first.result.state, ciNotes: 'n' }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith([{ ...MIXED[1], verdict: 'rig-side' }]) })
  assert.equal(placed.result.state.ciCache.entries[0].placedWith, undefined, 'nothing unclassified, nothing to record')
  await assert.rejects(run({ args: { ...YIELD, state: seal({ ...first.result.state, ciCache: { ...first.result.state.ciCache, entries: [{ ...first.result.state.ciCache.entries[0], placedWith: 7 }] } }) } }), /not a pr-babysit state/)
  // an unchanged check beside a newly failing one: only the new one is judged, and both are reported
  const fresh = new Map()
  const mixed = (await run({ store: fresh, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })).result.state
  const beside = await run({ store: fresh, args: { ...YIELD, state: mixed }, reviews: WAITING, ci: redWith(RIG, RIG).ci })
  assert.deepEqual(askedOf(judgePrompt(beside.calls, 2)).map(c => c.link), ['https://github.com/o/r/actions/runs/1/job/2'])
  assert.deepEqual(beside.result.history.at(-1).ci.realFailures.map(f => f.cell), [...MIXED.map(f => f.cell), null])
  // of two such checks, only the one whose base job changed is judged again
  const two = new Map()
  const both = (await run({ store: two, args: YIELD, reviews: WAITING, ci: redWith(RIG, RIG).ci, judge: judgeWith(MIXED) })).result.state
  const moved = await run({ store: two, base: (label, link) => link.endsWith('job/2') ? 'b2' : 'b1', args: { ...YIELD, state: both }, reviews: WAITING, ci: redWith(RIG, RIG).ci, judge: judgeWith([MIXED[1]]) })
  assert.deepEqual(askedOf(judgePrompt(moved.calls, 2)).map(c => c.link), ['https://github.com/o/r/actions/runs/1/job/2'])
  // a read that was incomplete is judged again, the log may be readable now
  const unread = [MIXED[0], { ...MIXED[1], complete: false }]
  const kept = new Map()
  const st = (await run({ store: kept, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(unread) })).result.state
  const reread = await run({ store: kept, args: { ...YIELD, state: st }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(unread) })
  assert.ok(reread.labels.includes('ci:collect#2.r1'), 'recalled, then judged')
  assert.ok(reread.labels.includes('ci:judge#2'))
})

test('a launch rollup counts the CI checks judged whole, judged in part, reused and left as judged', async () => {
  const store = new Map()
  const counts = (r) => r.result.rollup.ciChecks
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })
  assert.deepEqual(counts(first), { judged: 1, partial: 0, reused: 0, unchanged: 0 })
  const same = await run({ store, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(counts(same), { judged: 0, partial: 0, reused: 0, unchanged: 1 })
  const moved = await run({ store, base: 'b2', args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith([{ ...MIXED[1], verdict: 'rig-side' }]) })
  assert.deepEqual(counts(moved), { judged: 0, partial: 1, reused: 0, unchanged: 0 })
  const placed = await run({ store, args: { ...YIELD, state: moved.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(counts(placed), { judged: 0, partial: 0, reused: 1, unchanged: 0 })
})

test('a partly judged check answered with every failure is taken whole, and with anything else is judged whole again', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })
  const whole = [cellFailure('pico a', 'real'), cellFailure('pico b', 'rig-side'), cellFailure('pico c', 'rig-side'), cellFailure('pico d', 'rig-side')]
  const listed = await run({ store, base: 'b2', args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(whole) })
  assert.deepEqual(listed.result.history.at(-1).ci.realFailures.map(f => [f.cell, f.verdict]), whole.map(f => [f.cell, f.verdict]))
  assert.deepEqual(listed.result.rollup.ciChecks, { judged: 1, partial: 0, reused: 0, unchanged: 0 }, 'asked in part, answered whole')
  for (const answer of [[cellFailure('pico d', 'rig-side')], [MIXED[1], MIXED[1]], [cellFailure('pico a', 'rig-side'), cellFailure('pico b', 'rig-side')],
    [{ ...MIXED[1], verdict: 'rig-side', complete: false }], [...MIXED, MIXED[0]]]) {
    const store = new Map()
    const st = (await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })).result.state
    // a second check, new on this launch, is judged in the same call
    const odd = await run({ store, base: 'b2', args: { ...YIELD, state: st }, reviews: WAITING, ci: redWith(RIG, RIG).ci,
      judge: (j) => ({ ...j, checks: j.checks.map((c, i) => i === 0 ? { ...c, failures: structuredClone(answer) } : c) }) })
    assert.ok(odd.logs.some(l => /answered hil \/ pico with neither its judgeOnly failures nor every failure/.test(l)), JSON.stringify(answer.map(f => f.cell)))
    assert.equal(odd.result.history.at(-1).ci, null, 'the cycle re-arms')
    assert.deepEqual(odd.result.state.ciCache.entries.map(e => e.link), ['https://github.com/o/r/actions/runs/1/job/2'], 'the other check\'s verdict is kept')
    assert.deepEqual(odd.result.rollup.ciChecks, { judged: 1, partial: 0, reused: 0, unchanged: 0 }, 'and counted, though the cycle re-arms')
    const next = await run({ store, args: { ...YIELD, state: odd.result.state }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })
    assert.equal(askedOf(judgePrompt(next.calls, 3))[0].judgeOnly, undefined, 'judged whole next')
  }
})

test('a partly judged check answered badly never passes on its stored failures, even all accepted', async () => {
  const store = new Map()
  const acceptedFailures = MIXED.map(f => ({ key: keyOf(f), reason: 'rig', scope: 'this PR' }))
  const st = (await run({ store, args: { ...YIELD, acceptedFailures }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })).result.state
  const odd = await run({ store, base: 'b2', args: { ...YIELD, acceptedFailures, state: st }, reviews: { findings: [], replies: [] }, ci: redWith(RIG).ci, judge: judgeWith([cellFailure('pico d', 'real')]) })
  assert.equal(odd.result.pass, false)
})

test('a partial judgment needs a complete enumeration of distinct failures, and a re-run still settles the check', async () => {
  for (const snapshot of [[MIXED[0], { ...MIXED[1], complete: false }], [MIXED[0], MIXED[0], MIXED[1]]]) {
    const store = new Map()
    const st = (await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(snapshot) })).result.state
    const again = await run({ store, base: 'b2', args: { ...YIELD, state: st }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(snapshot) })
    assert.equal(askedOf(judgePrompt(again.calls, 2))[0].judgeOnly, undefined)
  }
  const store = new Map()
  const st = (await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith(MIXED) })).result.state
  const rerun = await run({ store, base: 'b2', args: { ...YIELD, state: st }, reviews: WAITING, ci: redWith(RIG).ci, judge: judgeWith([]) })
  assert.equal(rerun.result.history.at(-1).ci.status, 'running')
  assert.deepEqual(rerun.result.state.ciCache.reruns.map(r => r.sure), [true])
  assert.deepEqual(rerun.result.state.ciCache.entries, [], 'the re-run check\'s snapshot is dropped')
  assert.deepEqual(rerun.result.rollup.ciChecks, { judged: 1, partial: 0, reused: 0, unchanged: 0 }, 'a re-run is a judgment, not a merge')
})

test('an accepted failure resumed from the cache still passes', async () => {
  const store = new Map()
  const first = await run({ ...redWith(PVS), store, args: { ...YIELD, acceptedFailures: [byKey()] }, reviews: WAITING })
  const done = await run({ ...redWith(PVS), store, args: { ...YIELD, acceptedFailures: [byKey()], state: first.result.state } })
  assert.equal(done.labels.includes('ci:judge#2'), false)
  assert.equal(done.result.pass, true, JSON.stringify(done.result.reason))
  assert.equal(done.result.acceptedFailures.length, 1)
})

test('a judge lost after it may have re-run is never followed by a re-run of those checks', async () => {
  const { calls, logs } = await run({ throwOn: 'ci:judge#1', args: { autoPush: true, maxCycles: 2 }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.ok(logs.some(l => /CI judge errored/.test(l)))
  assert.match(calls.find(c => c.label === 'ci:collect#2.f').prompt, /job\/1'/, 'not settling: no re-run is known to exist')
  assert.match(calls.find(c => c.label === 'ci:judge#2').prompt, /\nPossibly re-run by a judge that was lost on this head: \["ci \/ hil \/ pico"\]/)
})

test('a check keeps one re-run record per head, and a known re-run replaces a possible one', async () => {
  const lost = await run({ throwOn: 'ci:judge#', args: { autoPush: true, maxCycles: 3 }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.equal(lost.labels.filter(l => l.startsWith('ci:judge#')).length, 3)
  assert.deepEqual(lost.result.state.ciCache.reruns.map(r => [r.check, r.sure]), [['hil / pico', false]])
  const rerun = j => ({ ...j, checks: j.checks.map(c => ({ ...c, failures: [] })) })
  const known = await run({ throwOn: 'ci:judge#1', judge: rerun, args: { autoPush: true, maxCycles: 2 }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(known.result.state.ciCache.reruns.map(r => [r.check, r.sure]), [['hil / pico', true]])
})

test('a re-run carries across a launch: its old run is still settling', async () => {
  const rerun = j => ({ ...j, checks: j.checks.map(c => ({ ...c, failures: [] })) })
  const first = await run({ args: YIELD, reviews: WAITING, ci: { status: 'red', infraRerun: ['7'], realFailures: [RIG] }, judge: rerun })
  assert.deepEqual(first.result.state.ciCache.reruns.map(r => [r.check, r.sure]), [['hil / pico', true]])
  const again = await run({ args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
  assert.deepEqual(ciLabels(again.labels), ['ci:collect#2.1'])
  assert.equal(again.result.history.at(-1).ci.status, 'running')
})

test('a re-run recorded under a copy\'s link still settles the execution it copied', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci })
  const st = first.result.state
  const job = (n) => `https://github.com/o/r/actions/runs/1/job/${n}`
  // an older launch re-ran it under J2, a copy; the listing now shows J3, a copy of J2, resolved to J1
  const old = seal({ ...st, ciCache: { ...st.ciCache, reruns: [{ head: st.ciCache.entries[0].head, link: job(2), workflow: 'ci', check: 'hil / pico', sure: true }] } })
  const copied = await run({ store, args: { ...YIELD, state: old }, reviews: WAITING, ci: redWith({ ...RIG, link: job(1), aliases: [job(3), job(2)] }).ci })
  assert.deepEqual(ciLabels(copied.labels), ['ci:collect#2.1'], 'not reused as red: still settling')
  assert.equal(copied.result.history.at(-1).ci.status, 'running')
  const executed = await run({ store, args: { ...YIELD, state: old }, reviews: WAITING, ci: redWith({ ...RIG, link: job(4) }).ci })
  assert.ok(executed.labels.includes('ci:judge#2'), 'a new execution is no longer settling')
})

test('verdicts of any size cost the state a digest each', async () => {
  const many = Array.from({ length: 12 }, (_, i) => ({ ...RIG, check: `hil / b${i}`, firstError: `${i} →\x7f`.padEnd(5000, 'y') }))
  const store = new Map()
  const full = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(...many).ci })
  assert.equal(full.result.state.ciCache.entries.length, 12)
  assert.ok(JSON.stringify(full.result.state.ciCache).length < 3 * 1024)
  const again = await run({ store, args: { ...YIELD, state: full.result.state }, reviews: WAITING, ci: redWith(...many).ci })
  assert.deepEqual(ciLabels(again.labels), ['ci:collect#2.1', 'ci:collect#2.r1', 'ci:collect#2.r2', 'ci:collect#2.r3'], 'five links to a recall')
  assert.equal(again.result.history.at(-1).ci.realFailures.length, 12)
})

test('a collector that dies gets one fresh agent; a second death leaves the cycle without CI', async () => {
  const revived = await run({ reviews: WAITING, ci: redWith(RIG).ci, throwOn: (l) => l === 'ci:collect#1.1' })
  assert.deepEqual(ciLabels(revived.labels).slice(0, 3), ['ci:collect#1.1', 'ci:collect#1.1.retry', 'ci:collect#1.f'])
  assert.match(revived.calls.find(c => c.label === 'ci:collect#1.1.retry').prompt, / inventory --wait-seconds 0 /, 'the slice was waited once')
  assert.equal(revived.logs.some(l => /CI inventory failed/.test(l)), false)
  const dead = await run({ reviews: WAITING, ci: redWith(RIG).ci, throwOn: 'ci:collect#1.1' })
  assert.deepEqual(ciLabels(dead.labels).slice(0, 2), ['ci:collect#1.1', 'ci:collect#1.1.retry'])
  assert.ok(dead.logs.some(l => /cycle 1: CI inventory failed — the collector died/.test(l)), dead.logs.join('\n'))
  assert.equal(dead.labels.includes('ci:judge#1'), false)
  const cut = await run({ reviews: WAITING, ci: redWith(RIG).ci, garble: (l, a) => l === 'ci:collect#1.1' ? { ...a, head: a.head.slice(0, 35) } : a })
  assert.deepEqual(ciLabels(cut.labels).slice(0, 3), ['ci:collect#1.1', 'ci:collect#1.1.retry', 'ci:collect#1.f'], 'a cut head is asked once more')
  // Both copies turn red to green with the head intact: nothing downstream would notice.
  const green = await run({ reviews: WAITING, ci: redWith(RIG).ci, garble: (l, a) => l.startsWith('ci:collect#1.1') ? { ...a, status: 'green', checks: [] } : a })
  assert.ok(green.logs.some(l => /cycle 1: CI inventory failed — the collector died/.test(l)), green.logs.join('\n'))
})

test('a verdict the store lost or garbled is reused by its launch, then judged again', async () => {
  const garble = (answer, store) => { for (const [k, v] of store) store.set(k, { ...v, bucket: 'cancel' }); return answer }
  const lose = (answer, store) => { store.clear(); return answer }
  const cases = [[garble, null, []], [lose, null, []], [(a, store) => lose(null, store), /the collector died/, ['ci:collect#1.w.retry']], [(a, store) => lose({ ...a, error: 'disk full' }, store), /disk full/, []]]
  for (const [remember, why, retried] of cases) {
    const store = new Map()
    const first = await run({ store, remember, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci })
    assert.equal(first.logs.some(l => /1 CI verdict\(s\) not stored — .*judged again by a later launch/.test(l) && why.test(l)), !!why, first.logs.join('\n'))
    const within = await run({ store: new Map(), remember, args: { autoPush: true, maxCycles: 2 }, reviews: WAITING, ci: redWith(RIG).ci })
    assert.deepEqual(ciLabels(within.labels), ['ci:collect#1.1', 'ci:collect#1.f', 'ci:judge#1', 'ci:collect#1.w', ...retried, 'ci:collect#2.1'], 'its own launch reuses it')
    const again = await run({ store, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
    assert.deepEqual(ciLabels(again.labels), ['ci:collect#2.1', 'ci:collect#2.r1', 'ci:collect#2.f', 'ci:judge#2', 'ci:collect#2.w'])
  }
})

test('a recalled verdict that is missing or fails its digest is judged again', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci })
  const tampered = (answer) => ({ ...answer, verdicts: answer.verdicts.map(v => ({ ...v, failures: v.failures.map(f => ({ ...f, verdict: 'real' })) })) })
  for (const [over, why] of [[{ recall: tampered }, /recalled with another digest/], [{ store: new Map() }, /not in the store/]]) {
    const again = await run({ store, ...over, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
    assert.ok(again.logs.some(l => why.test(l)), again.logs.join('\n'))
    assert.ok(again.labels.includes('ci:judge#2'))
    assert.equal(again.result.history.at(-1).ci.realFailures[0].verdict, 'rig-side')
  }
  for (const recall of [() => null, (a) => ({ ...a, error: 'gone', verdicts: [] })]) {
    const again = await run({ store, recall, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(RIG).ci })
    assert.ok(again.logs.some(l => /CI verdicts not recalled/.test(l)), again.logs.join('\n'))
    assert.ok(again.labels.includes('ci:judge#2'))
  }
})

test('a recall copy that fails its seal costs its own batch a fresh relay, and a re-judge only if that fails too', async () => {
  // #3988: one 28 KB recall line lost every failure's cell, then a firstError; the seal voided all 40 verdicts.
  const six = Array.from({ length: 6 }, (_, i) => ({ ...RIG, check: `hil / b${i}`, cell: `b${i}` }))
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(...six).ci })
  const dropCell = (a) => ({ ...a, verdicts: a.verdicts.map(v => ({ ...v, failures: v.failures.map(({ cell, ...f }) => f) })) })
  const again = (only) => run({ store, garble: (l, a) => only(l) ? dropCell(a) : a, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(...six).ci })
  const once = await again(l => l === 'ci:collect#2.r1')
  assert.deepEqual(ciLabels(once.labels), ['ci:collect#2.1', 'ci:collect#2.r1', 'ci:collect#2.r2', 'ci:collect#2.r1.retry'])
  assert.equal(once.calls.find(c => c.label === 'ci:collect#2.r1.retry').model, 'sonnet')
  assert.equal(once.result.history.at(-1).ci.realFailures.length, 6)
  const twice = await again(l => l.startsWith('ci:collect#2.r1'))
  assert.ok(twice.logs.some(l => /CI verdicts not recalled for hil \/ b0, .*hil \/ b4 — the collector died/.test(l)), twice.logs.join('\n'))
  assert.ok(twice.logs.some(l => /CI verdicts reused for 1 check/.test(l)), 'the other batch is recalled')
  const judged = twice.calls.find(c => c.label === 'ci:judge#2').prompt
  assert.ok(['b0', 'b1', 'b2', 'b3', 'b4'].every(b => judged.includes(`hil / ${b}`)) && !judged.includes('hil / b5'), 'only the lost batch is judged again')
})

test('a link the recall held back for room comes back in its own recall, and one too large to relay is judged again', async () => {
  // tinyusb#4019: one recall line of 51 KB, a HIL job's 113 failures in it, that no relay could copy.
  const four = Array.from({ length: 4 }, (_, i) => ({ ...RIG, check: `hil / b${i}`, cell: `b${i}` }))
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(...four).ci })
  // b0 has a failure too large for any line; b2 and b3 do not fit beside b1, and each comes back as one page.
  const held = new Map([[job(3), [0]], [job(4), [0]]])
  const dropped = (lost) => (a, text) => lost(({ ...a, verdicts: a.verdicts.filter(v => v.link !== job(1)) }), text)
  const again = (lost) => run({ store, held, recall: dropped(lost), args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: redWith(...four).ci })
  const judgedOf = (r) => ['b0', 'b1', 'b2', 'b3'].filter(b => r.calls.find(c => c.label === 'ci:judge#2').prompt.includes(`hil / ${b}`))

  const ok = await again(a => a)
  assert.deepEqual(ciLabels(ok.labels), ['ci:collect#2.1', 'ci:collect#2.r1', 'ci:collect#2.r1.2.p1', 'ci:collect#2.r1.3.p1', 'ci:collect#2.f', 'ci:judge#2', 'ci:collect#2.w'])
  assert.match(ok.calls.find(c => c.label === 'ci:collect#2.r1.2.p1').prompt, new RegExp(` recall --check '${job(3)}' --offset 0 --repo `))
  assert.match(ok.calls.find(c => c.label === 'ci:collect#2.r1.3.p1').prompt, new RegExp(` recall --check '${job(4)}' --offset 0 --repo `))
  assert.ok(ok.logs.includes('cycle 2: CI verdict for hil / b0 not returned by the recall (too large to relay, or not in the store) — judged again'), ok.logs.join('\n'))
  assert.ok(ok.logs.some(l => /CI verdicts reused for 3 check/.test(l)), ok.logs.join('\n'))
  assert.deepEqual(judgedOf(ok), ['b0'])

  const b3 = (text) => text.includes(`--check '${job(4)}' --offset`)
  for (const [lost, why] of [[(a, text) => b3(text) ? { ...a, error: 'gone', verdicts: [] } : a, 'gone'], [(a, text) => b3(text) ? { ...a, verdicts: [] } : a, 'its recall did not return it']]) {
    const failed = await again(lost)
    assert.ok(failed.logs.includes(`cycle 2: CI verdict not recalled for hil / b3 — page 1 of 1: ${why}`), failed.logs.join('\n'))
    assert.ok(failed.logs.some(l => /CI verdicts reused for 2 check/.test(l)), 'the first recall and the other held-back link stand')
    assert.deepEqual(judgedOf(failed), ['b0', 'b3'])
  }
})

test('a verdict too large for one line is recalled in pages that join into it, and a lost or changed page is judged again', async () => {
  // #25: tinyusb#4019's 113-failure HIL verdict was judged again on every launch.
  const two = [{ ...RIG, check: 'hil / b0', cell: 'b0' }, { ...RIG, check: 'hil / b1', cell: 'b1' }]
  const many = (judged) => ({ ...judged, checks: judged.checks.map(c => c.link !== job(1) ? c
    : { ...c, failures: [0, 1, 2].map(n => ({ ...c.failures[0], firstError: `board ${n} did not enumerate`, signature: `board ${n}` })) }) })
  const held = new Map([[job(1), [0, 2]]])
  // Beside another link or alone, the big one is held back with its page starts.
  for (const ci of [redWith(...two).ci, redWith(two[0]).ci]) {
    const store = new Map()
    const first = await run({ store, judge: many, args: YIELD, reviews: WAITING, ci })
    const again = (recall) => run({ store, held, recall, judge: many, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci })
    const ok = await again()
    assert.deepEqual(ciLabels(ok.labels), ['ci:collect#2.1', 'ci:collect#2.r1', 'ci:collect#2.r1.2.p1', 'ci:collect#2.r1.2.p2'])
    assert.match(ok.calls.find(c => c.label === 'ci:collect#2.r1.2.p2').prompt, new RegExp(` recall --check '${job(1)}' --offset 2 --repo `))
    assert.ok(ok.logs.some(l => new RegExp(`CI verdicts reused for ${ci.realFailures.length} check`).test(l)), ok.logs.join('\n'))
    assert.deepEqual(ok.result.history.at(-1).ci.realFailures.filter(f => f.check === 'hil / b0').map(f => f.firstError),
      ['board 0 did not enumerate', 'board 1 did not enumerate', 'board 2 did not enumerate'])

    const second = (text) => / --offset 2 /.test(text)
    for (const [recall, why] of [
      [(a, text) => second(text) ? null : a, 'CI verdict not recalled for hil / b0 — page 2 of 2: the collector died'],
      [(a, text) => second(text) ? { ...a, verdicts: [] } : a, 'CI verdict not recalled for hil / b0 — page 2 of 2: its recall did not return it'],
      [(a, text) => second(text) ? { ...a, verdicts: a.verdicts.map(v => ({ ...v, failures: [] })) } : a, 'CI verdict for hil / b0 recalled with another digest — judged again'],
    ]) {
      const failed = await again(recall)
      assert.ok(failed.logs.includes(`cycle 2: ${why}`), failed.logs.join('\n'))
      assert.ok(failed.calls.find(c => c.label === 'ci:judge#2').prompt.includes('hil / b0'))
    }
  }
})

test('a cached verdict is reused only while its check keeps the same conclusion', async () => {
  const store = new Map()
  const first = await run({ store, args: YIELD, reviews: WAITING, ci: redWith(RIG).ci })
  assert.equal(first.result.state.ciCache.entries[0].bucket, 'fail')
  const cancelled = await run({ store, args: { ...YIELD, state: first.result.state }, reviews: WAITING, ci: { ...redWith(RIG).ci, bucket: 'cancel' } })
  assert.equal(cancelled.labels.includes('ci:collect#2.r1'), false, 'nothing to recall for another conclusion')
  assert.ok(cancelled.labels.includes('ci:judge#2'), 'a conclusion updated under the same link is judged again')
  assert.deepEqual(cancelled.result.state.ciCache.entries.map(e => e.bucket), ['cancel'], 'and its new verdict replaces the old')
})

test('a state whose CI cache is malformed is refused', async () => {
  const first = await run({ args: YIELD, reviews: WAITING, ci: redWith(RIG).ci })
  const { digest, ...st } = first.result.state
  for (const ciCache of [{ ...st.ciCache, entries: [{ head: HEAD, link: 'x', bucket: 'fail', digest: 7 }] },
    { ...st.ciCache, reruns: [{ head: HEAD, link: 'x' }] }, { entries: [] },
    { ...st.ciCache, entries: st.ciCache.entries.map(({ bucket, ...e }) => e) },
    { ...st.ciCache, entries: st.ciCache.entries.map(({ digest, ...e }) => e) }]) {
    await assert.rejects(run({ args: { ...YIELD, state: seal({ ...st, ciCache }) } }), /not a pr-babysit state/)
  }
})

test('a state missing a field the writer always saves is refused', async () => {
  const { digest, ...st } = (await run({ args: YIELD })).result.state
  for (const key of ['deferrals', 'acceptedFailures', 'decisions', 'holds', 'ciCache', 'build']) {
    const { [key]: _, ...partial } = st
    await assert.rejects(run({ args: { ...YIELD, state: seal(partial) } }), /state is not a pr-babysit state of version 3/, key)
  }
})

test('ciNotes reach the judge prompt verbatim, and only when given', async () => {
  const red = { status: 'red', infraRerun: [], realFailures: [UNPLACED] }
  const noted = await run({ ci: red, args: { maxCycles: 1, ciNotes: 'PVS-Studio exit 2 is the license expiry warning: rig-side, see run 35171132943' } })
  assert.match(noted.calls.find(c => c.label === 'ci:judge#1').prompt, /caller established[\s\S]*license expiry warning: rig-side, see run 35171132943/)
  const bare = await run({ ci: red, args: { maxCycles: 1 } })
  assert.doesNotMatch(bare.calls.find(c => c.label === 'ci:judge#1').prompt, /caller established/)
})

test('the CI contract names the three verdicts and nothing else', async () => {
  const { calls } = await run({ ci: { status: 'red', infraRerun: [], realFailures: [UNPLACED] }, args: { maxCycles: 1 } })
  const judge = calls.find(c => c.label === 'ci:judge#1')
  assert.equal(judge.agentType, 'pr-ci-watcher')
  const item = judge.schema.properties.checks.items.properties.failures.items
  assert.deepEqual(item.required, ['check', 'workflow', 'job', 'cell', 'signature', 'runId', 'complete', 'firstError', 'files', 'verdict'])
  assert.deepEqual(judge.schema.required, ['checks', 'infraRerun'])
  assert.deepEqual(item.properties.verdict.enum, ['real', 'rig-side', 'unclassified'])
  assert.deepEqual(item.properties.runId.type, ['integer', 'null'], 'a check whose link names no run has no run id')
  assert.equal(item.additionalProperties, false)
})

// caller-accepted CI failures

const PVS = { check: 'pvs / analyze', workflow: 'static', job: 'pvs', cell: null, signature: 'license expires in 12 days', firstError: 'exit 2 after Analysis finished', files: [], verdict: 'rig-side' }
// The retired long form, refused: a caller copies the key the result shows.
const longForm = (over = {}) => ({ workflow: 'static', job: 'pvs', cell: null, signature: 'license expires in 12 days', reason: 'PVS license renewal pending', scope: 'until the license is renewed', ...over })
const redWith = (...failures) => ({ ci: { status: 'red', infraRerun: [], realFailures: failures } })

// fnv1a64 of JSON.stringify(['static', 'pvs', null, 'license expires in 12 days']), computed outside the workflow.
const PVS_KEY = '8f5a706886f8c791'
assert.equal(keyOf(longForm()), PVS_KEY)
const byKey = (over = {}) => ({ key: PVS_KEY, reason: 'PVS license renewal pending', scope: 'until the license is renewed', ...over })

test('an accepted failure is named by its key, and a wrong key accepts nothing', async () => {
  const { result, logs } = await run({ ...redWith(PVS), args: { acceptedFailures: [byKey()] } })
  assert.equal(result.pass, true, JSON.stringify(result.reason))
  assert.equal(result.acceptedFailures[0].key, PVS_KEY)
  assert.equal(result.observation.ci.realFailures[0].key, PVS_KEY, 'the observed report carries the key a caller accepts it by')
  assert.deepEqual(result.state.acceptedFailures, [{ key: PVS_KEY }], 'the state keeps the key alone')
  const wrong = await run({ ...redWith(PVS), args: { acceptedFailures: [byKey({ key: '0'.repeat(16) })] } })
  assert.notEqual(wrong.result.pass, true)
  assert.ok(wrong.logs.some(l => /accepted failure 0{16} matches no failure on this head/.test(l)), wrong.logs.join('\n'))
  const twice = await run({ ...redWith(PVS, { ...PVS }), args: { acceptedFailures: [byKey()] } })
  assert.notEqual(twice.result.pass, true, 'a key that names two observed failures accepts neither')
  assert.ok(twice.logs.some(l => /listed 2 times; one acceptance covers one/.test(l)))
})

test('acceptedFailures are checked for shape before anything runs', async () => {
  for (const acceptedFailures of [[byKey({ key: 'abc' })], [byKey({ key: PVS_KEY.toUpperCase() })], [byKey({ cell: null })], [byKey({ reason: ' ' })], [byKey({ scope: ' ' })], [byKey(), byKey()], [longForm()], [{ ...longForm(), key: PVS_KEY }]]) {
    const trace = []
    await assert.rejects(run({ args: { acceptedFailures }, trace }), /acceptedFailures must be/, JSON.stringify(acceptedFailures))
    assert.deepEqual(trace, [])
  }
})

test('a run red only from accepted failures passes, listing them, and is never called green', async () => {
  const { result, logs, labels } = await run({ ...redWith(PVS), args: { acceptedFailures: [byKey()] } })
  assert.equal(result.pass, true, JSON.stringify(result.reason))
  assert.deepEqual(result.acceptedFailures, [{ check: 'pvs / analyze', workflow: 'static', job: 'pvs', cell: null, signature: 'license expires in 12 days', key: PVS_KEY, verdict: 'rig-side', reason: 'PVS license renewal pending', scope: 'until the license is renewed' }])
  assert.match(summaries(logs)[0], /^cycle 1 summary — CI red, accepted failures only/)
  assert.match(rowsOf(summaries(logs)[0])[0][3], /^accepted, not fixed: PVS license renewal pending/)
  assert.equal(rowsOf(summaries(logs)[0])[0][2], 'rig-side', 'the watcher\'s classification is kept')
  assert.ok(logs.some(l => /CI red only from 1 accepted failure\(s\)/.test(l)))
  assert.ok(!logs.some(l => /PR is green/.test(l)))
  assert.equal(labels.some(l => l.startsWith('fix:')), false)
  assert.deepEqual(result.state.acceptedFailures, [{ key: PVS_KEY }], 'the state keeps the key alone')
})

test('a green run is called green and lists no accepted failures', async () => {
  const { result, logs } = await run({ reviews: { findings: [], replies: [], bots: 'reviewed' } })
  assert.equal(result.pass, true, JSON.stringify(result.reason))
  assert.equal('acceptedFailures' in result, false)
  assert.ok(logs.some(l => /PR is green with no unresolved valid findings/.test(l)), logs.join('\n'))
})

test('accepted failures with replies still owed are not called green', async () => {
  const { logs } = await run({ ...redWith(PVS), args: { acceptedFailures: [byKey()], maxCycles: 2 }, dropDoneIds: () => true,
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' } })
  assert.ok(logs.some(l => /CI red only from accepted failures but 1 comment\(s\) still owed an answer/.test(l)), logs.join('\n'))
  assert.ok(!logs.some(l => /PR green/.test(l)))
})

test('pending checks keep an accepted-only report from passing', async () => {
  const { result } = await run({ ci: { status: 'running', infraRerun: [], realFailures: [PVS] }, args: { acceptedFailures: [byKey()], maxCycles: 1 } })
  assert.notEqual(result.pass, true)
  const rerun = await run({ ci: { status: 'red', infraRerun: ['build / flaky'], realFailures: [PVS] }, args: { acceptedFailures: [byKey()], maxCycles: 1 } })
  assert.notEqual(rerun.result.pass, true)
  assert.match(summaries(rerun.logs)[0], /^cycle 1 summary — CI red,/)
  assert.doesNotMatch(summaries(rerun.logs)[0], /accepted failures only/, 'an infra re-run still settling is not accepted-only')
})

test('an accepted failure the watcher calls real is still not fixed', async () => {
  const real = { ...PVS, verdict: 'real', files: ['src/a.c'] }
  const { result, labels } = await run({ ...redWith(real), args: { acceptedFailures: [byKey()] } })
  assert.equal(labels.some(l => l.startsWith('fix:')), false)
  assert.equal(result.pass, true)
})

test('only the exact failure is accepted: another diagnostic, another cell, another workflow or a second failure is not', async () => {
  for (const [failures, label] of [
    [[{ ...PVS, signature: 'license expired' }], 'a new diagnostic on the accepted cell'],
    [[{ ...PVS, cell: 'arm' }], 'a cell where null was accepted'],
    [[{ ...PVS, workflow: 'ci' }], 'the same job in another workflow'],
    [[PVS, { ...PVS, signature: 'V501 identical sub-expressions', firstError: 'V501', verdict: 'unclassified' }], 'a second failure in the accepted cell'],
  ]) {
    const { result } = await run({ ...redWith(...failures), args: { acceptedFailures: [byKey()] } })
    assert.notEqual(result.pass, true, label)
  }
})

test('a CI report for another head is not fixed, accepted or counted green', async () => {
  for (const ci of [
    { status: 'red', infraRerun: [], realFailures: [{ check: 'build / arm', firstError: 'boom', files: ['src/a.c'], verdict: 'real' }] },
    { ...redWith(PVS).ci },
    GREEN,
  ]) {
    const { result, labels, logs } = await run({ ci: { ...ci, headSha: 'f'.repeat(40) }, args: { acceptedFailures: [byKey()], maxCycles: 1 } })
    assert.notEqual(result.pass, true)
    assert.equal(labels.some(l => /^(fix:|commit#|push#)/.test(l)), false)
    assert.ok(logs.some(l => /CI inventory is inconsistent — head fffffff for /.test(l)), logs.join('\n'))
  }
})

test('an acceptance needs a complete listing of the job and covers one failure', async () => {
  for (const [failures, why] of [
    [[{ ...PVS, complete: false }], /not accepted — the watcher did not list every failure of its job/],
    [[PVS, { ...PVS, check: 'pvs / analyze (2)' }], /not accepted — the same failure is listed 2 times; one acceptance covers one/],
  ]) {
    const { result, logs } = await run({ ...redWith(...failures), args: { acceptedFailures: [byKey()] } })
    assert.notEqual(result.pass, true)
    assert.ok(logs.some(l => why.test(l)), logs.join('\n'))
  }
})

test('an acceptance not renewed on a resumed launch no longer applies, and says so', async () => {
  const first = await run({ ...redWith(PVS), args: { acceptedFailures: [byKey()], maxCycles: 3, yieldAfterCycle: true } })
  const { result, logs } = await run({ ...redWith(PVS), args: { maxCycles: 3, state: first.result.state } })
  assert.ok(logs.some(l => new RegExp(`accepted failure not renewed by this launch, no longer accepted: key ${PVS_KEY}$`).test(l)))
  assert.notEqual(result.pass, true)
  assert.deepEqual(result.state.acceptedFailures, [])
})

test('a mislabeled commit SHA is not shown as a commit', async () => {
  // The SHA in the table is the committer's, validated rather than trusted.
  const { logs } = await run({ reviews: oneValid, audit: { sha: 'committed 1234567 insertions' } })
  assert.equal(rowsOf(summaries(logs)[0])[0][4], '-')
  // And the pusher cannot rename it: the table reports the commit that was audited.
  const relabeled = await run({ reviews: oneValid, push: { detail: 'pushed deadbeefdeadbeefdeadbeefdeadbeefdeadbeef' } })
  assert.equal(rowsOf(summaries(relabeled.logs)[0])[0][4], shaFor(1).slice(0, 8))
})

test('a dead review validator still settles the CI lane', async () => {
  const { result, logs, labels } = await run({ reviews: new Error('validator exploded') })
  assert.equal(result.reason, 'review-validator-died')
  assert.equal(result.history[0].error, 'pr-review-validator died')
  assert.deepEqual(labels.slice(0, 2), ['preflight', 'ci:collect#1.1'], 'the CI lane was launched')
  assert.equal(result.history[0].ci.status, 'green', 'and awaited, so no agent outlives the workflow')
  assert.equal(summaries(logs).length, 1)
})

test('a failed push stops the loop after a summary', async () => {
  const { result, logs } = await run({ reviews: oneValid, push: null })
  assert.equal(result.reason, 'push-failed')
  assert.equal(summaries(logs).length, 1)
  const row = rowsOf(summaries(logs)[0])[0]
  assert.match(row[3], /fixed \+ committed a1b2c3d, NOT PUSHED: push rejected/,
    'the fix is committed locally — the row must say so, and say the push failed')
  assert.equal(row[4], '-', 'a failed push carries no commit SHA')
  assert.equal(result.history[0].reviewPushFailed.detail, 'push rejected')
  const dry = await run({ reviews: oneValid, args: { autoPush: false } })
  assert.notEqual(row[3], rowsOf(summaries(dry.logs)[0])[0][3], 'and reads differently from a dry run')
})

test('a partial publication answers no comment and claims no push', async () => {
  // The commit exists but nothing is on the remote, so a fix note pointing at it
  // would send the reviewer to a commit they cannot see.
  const { result, labels } = await run({ reviews: oneValid, push: null })
  const entry = result.history[0]
  assert.equal(result.reason, 'push-failed')
  assert.deepEqual(entry.reviewPushFailed,
    { pass: false, committed: true, detail: 'push rejected', sha: shaFor(1) },
    'the commit exists, so its SHA is what the human recovers from')
  assert.equal(entry.reviewPush, undefined, 'a partial publication is not a push')
  assert.equal(labels.some(l => l.startsWith('resolve#')), false, 'no fix note may go out')
  assert.equal(entry.fixNotePosts, undefined)
})

test('a commit that never landed is not reported as committed', async () => {
  const { result, logs, labels } = await run({
    reviews: oneValid,
    commit: { committed: false, detail: 'pre-commit hook rejected' },
  })
  assert.equal(result.reason, 'push-failed')
  assert.equal(labels.includes('push#1-review'), false, 'there is nothing to push')
  const row = rowsOf(summaries(logs)[0])[0]
  assert.match(row[3], /fixed, COMMIT FAILED: pre-commit hook rejected/,
    'nothing landed in git — the row must not send the reader after a nonexistent commit')
  assert.doesNotMatch(row[3], /NOT PUSHED|\+ committed/, 'and must not claim a commit to recover')
  assert.equal(row[4], '-')
})

test('the publisher rechecks the checkout and refuses to publish onto a moved one', async () => {
  const url = 'git@github.com:hathach/tinyusb.git'
  for (const [recheck, detail] of [
    [{ branch: 'main' }, 'checkout moved: branch is main, not claude/foo'],
    // pushurl is what `git push <remote>` follows, so it is what the recheck watches.
    [{ pushUrls: ['git@github.com:fork/tinyusb.git'] },
      'checkout moved: origin now pushes to git@github.com:fork/tinyusb.git'],
    [{ pushUrls: [] }, 'checkout moved: origin now pushes to (nowhere)'],
    [{ head: FOREIGN }, 'checkout moved: HEAD is c0ffee1, not the 0f1e2d3 this run left'],
    [{ staged: ['src/other.c', 'src/more.c'] }, 'checkout moved: 2 path(s) already staged by somebody else'],
    [null, 'recheck agent died'],
    [{ error: 'git rev-parse HEAD: fatal: not a git repository' }, 'recheck could not read the checkout: git rev-parse HEAD: fatal: not a git repository'],
  ]) {
    const { result, logs, labels, calls } = await run({ reviews: oneValid, recheck })
    assert.equal(result.reason, 'push-failed', detail)
    assert.deepEqual(result.history[0].reviewPushFailed, { pass: false, committed: false, detail, sha: '' })
    assert.ok(labels.includes('recheck#1-review'))
    assert.equal(labels.includes('commit#1-review'), false, 'nothing may be staged on a moved checkout')
    assert.equal(labels.includes('push#1-review'), false, 'and nothing may be published from it')
    assert.match(rowsOf(summaries(logs)[0])[0][3], /fixed, COMMIT FAILED/, detail)
    const re = calls.find(c => c.label === 'recheck#1-review')
    assert.match(re.prompt, /Editing and committing nothing/)
    assert.ok(re.prompt.includes('preflight.py --recheck`'), re.prompt)
    assert.deepEqual(re.schema.required.slice().sort(), ['branch', 'head', 'pushUrls', 'staged', 'status'])
  }
})

test('a replacement commit that keeps the count is still a moved checkout', async () => {
  // The false accept the SHA comparison replaced: cycle 1 pushes one commit, and
  // somebody amends it. One commit before, one after — a count sees nothing.
  let cycle = 0
  const { result, logs, labels } = await run({
    args: { autoPush: true, maxCycles: 2 },
    recheck: (label) => label === 'recheck#2-review' ? { head: FOREIGN } : undefined,
    reviewsPerCycle: () => {
      cycle++
      return { findings: [finding({ commentId: cycle, line: cycle })], replies: [], bots: 'reviewed' }
    },
  })
  assert.equal(result.reason, 'push-failed')
  assert.equal(result.history[1].reviewPushFailed.detail,
    'checkout moved: HEAD is c0ffee1, not the a1b2c3d this run left')
  assert.ok(labels.includes('commit#1-review'), 'cycle 1 published normally')
  assert.equal(labels.includes('commit#2-review'), false, 'cycle 2 must not build on a commit it did not make')
  assert.ok(logs.some(l => /push#2-review: refusing to publish — HEAD is c0ffee1/.test(l)))
})

test('a HEAD reset behind the pinned head refuses', async () => {
  // Cycle 1's commit is gone: HEAD is back at the PR head this run started from.
  // A count would see FEWER commits than we made and never fire at all.
  let cycle = 0
  const { result, labels } = await run({
    args: { autoPush: true, maxCycles: 2 },
    recheck: (label) => label === 'recheck#2-review' ? { head: HEAD } : undefined,
    reviewsPerCycle: () => {
      cycle++
      return { findings: [finding({ commentId: cycle, line: cycle })], replies: [], bots: 'reviewed' }
    },
  })
  assert.equal(result.reason, 'push-failed')
  assert.equal(result.history[1].reviewPushFailed.detail,
    'checkout moved: HEAD is 0f1e2d3, not the a1b2c3d this run left')
  assert.equal(labels.includes('commit#2-review'), false)
})

test('a path somebody else staged refuses before the commit agent runs', async () => {
  // `git add` would fold their staged change into our commit, and the audit that
  // follows reads the commit, not the index it came from.
  const { result, labels, logs } = await run({
    reviews: oneValid, recheck: { staged: ['src/theirs.c'] },
  })
  assert.equal(result.reason, 'push-failed')
  assert.equal(result.history[0].reviewPushFailed.detail,
    'checkout moved: 1 path(s) already staged by somebody else')
  assert.deepEqual(labels.filter(l => /^(recheck|commit|push)#/.test(l)), ['recheck#1-review'])
  assert.ok(logs.some(l => /refusing to publish — 1 path\(s\) already staged/.test(l)))
})

test('staged IDE metadata does not refuse the push; anything else staged beside it does', async () => {
  const ide = await run({ reviews: oneValid, recheck: { staged: ['.idea/cmake.xml'] } })
  assert.equal(ide.result.history[0].reviewPushFailed, undefined, ide.logs.join('\n'))
  assert.ok(ide.labels.includes('push#1-review'))
  const mixed = await run({ reviews: oneValid, recheck: { staged: ['.idea/cmake.xml', 'src/theirs.c'] } })
  assert.equal(mixed.result.history[0].reviewPushFailed.detail, 'checkout moved: 1 path(s) already staged by somebody else')
})

test('the commit is read back by an agent that did not write it', async () => {
  const { calls } = await run({ reviews: oneValid })
  const audit = calls.find(c => c.label === 'audit#1-review')
  // A committer reporting on its own commit is the one witness not to rely on.
  assert.match(audit.prompt, /Editing and committing nothing/)
  assert.deepEqual(audit.schema.required, ['parent', 'scope', 'sha', 'entries', 'refusal'])
  assert.ok(audit.prompt.includes(`commits.py head --parent ${HEAD} 'src/a.c'\``), audit.prompt)
  const commit = calls.find(c => c.label === 'commit#1-review')
  assert.deepEqual(commit.schema.required, ['committed', 'detail'],
    'the committer is not asked what its own commit contains')
  assert.ok(calls.indexOf(commit) < calls.indexOf(audit))
  const dead = await run({ reviews: oneValid, audit: null })
  assert.equal(dead.result.history[0].reviewPushFailed.committed, true)
  assert.match(dead.result.history[0].reviewPushFailed.detail, /audit agent died after the commit landed/)
  assert.equal(dead.labels.includes('push#1-review'), false, 'an unread commit must not leave the machine')
})

test('a commit the audit script refuses is committed but never pushed', async () => {
  const { result, logs, labels } = await run({ reviews: oneValid, audit: { refusal: 'commit sits on c0ffee1, not 0f1e2d3' } })
  assert.equal(result.reason, 'push-failed')
  assert.deepEqual(result.history[0].reviewPushFailed, {
    pass: false, committed: true, sha: shaFor(1),
    detail: 'commit failed audit: commit sits on c0ffee1, not 0f1e2d3',
  })
  assert.equal(labels.includes('push#1-review'), false, 'an unaudited commit must not leave the machine')
  assert.ok(logs.some(l => /committed but NOT pushed — commit sits on c0ffee1/.test(l)))
  assert.match(rowsOf(summaries(logs)[0])[0][3], /fixed \+ committed [0-9a-f]{7}, NOT PUSHED: commit failed audit/)
})

test('an audit of another parent or scope is not this commit\'s audit', async () => {
  for (const audit of [{ parent: FOREIGN }, { scope: ['src/a.c', 'src/z.c'] }]) {
    const { result, labels } = await run({ reviews: oneValid, audit })
    assert.match(result.history[0].reviewPushFailed.detail, /^commit failed audit: the audit read .* not this commit$/, JSON.stringify(audit))
    assert.equal(labels.includes('push#1-review'), false)
  }
})

test('a reply poster that throws leaves the debt owed, not the cycle dead', async () => {
  // The comments it posts are public and it records what went out AFTER it
  // returns, so a rejection there must not take the cycle with it: the run has
  // to end saying the replies are still owed, not that something exploded.
  const { result, logs } = await run({ reviews: oneValid, throwOn: 'resolve#' })
  assert.equal(result.pass, false)
  assert.equal(result.reason, 'deferred-replies-unresolved', 'the debt is what is unresolved')
  assert.equal(result.history.length, 1, 'the verdict keeps the history it was built from')
  assert.deepEqual(result.history[0].fixNotePosts, { pass: false, detail: 'agent died', receipts: [], replied: [] },
    'the receipt exists even though the poster died')
  assert.ok(logs.some(l => /resolve#1 errored — resolve#1 exploded/.test(l)), logs.join('\n'))
  assert.equal(summaries(logs).length, 1, 'the scoreboard survives an agent-failure cycle')
  const row = rowsOf(summaries(logs)[0])[0]
  assert.match(row[3], /fixed \+ pushed/, 'the push did land before the throw — the row must say so')
  assert.equal(row[4], SHA.slice(0, 8))
})

test('a publisher agent that throws is a failed push, not a crash', async () => {
  // Each publisher turn is guarded, so a rejection there becomes a push verdict
  // the summary can report rather than an unexplained dead cycle.
  for (const [throwOn, detail, committed, row] of [
    ['recheck#', 'recheck agent died', false, /fixed, COMMIT FAILED/],
    ['commit#', 'commit agent died; HEAD is still 0f1e2d3, but the committer may not have finished', null, /fixed, COMMIT OUTCOME UNKNOWN: commit agent died/], // an unmoved HEAD proves nothing while git may still run
    // It may have pushed before it died: the outcome is unknown, not a failure.
    ['push#', 'push agent died after the commit landed', true, /fixed \+ committed [0-9a-f]{7}, PUBLICATION UNKNOWN/],
  ]) {
    const { result, logs } = await run({ reviews: oneValid, throwOn })
    assert.equal(result.reason, 'push-failed', throwOn)
    assert.equal(result.history[0].reviewPushFailed.detail, detail)
    assert.equal(result.history[0].reviewPushFailed.committed, committed)
    assert.match(rowsOf(summaries(logs)[0])[0][3], row, throwOn)
    if (throwOn === 'push#') assert.equal(result.state.pending.stage, 'publication-unknown')
  }
})

test('a publisher receipt whose copy does not match its seal is no answer, never a definite outcome', async () => {
  // A push receipt with a cut head would otherwise read as "not pushed" when the push may have landed.
  const cutHead = (a) => ({ ...a, heads: a.heads.map(h => ({ ...h, head: h.head.slice(0, 35) })) })
  for (const [label, garble, detail, committed] of [
    ['hooks#', (a) => ({ ...a, passed: !a.passed }), 'hook agent died', false],
    ['push#', cutHead, 'push agent died after the commit landed', true],
  ]) {
    const { result, logs } = await run({ reviews: oneValid, garble: (l, a) => l.startsWith(label) ? garble(a) : a })
    assert.equal(result.history[0].reviewPushFailed.detail, detail, label)
    assert.equal(result.history[0].reviewPushFailed.committed, committed, label)
    assert.ok(logs.some(l => l.startsWith(label) && l.endsWith('the relayed copy does not match its seal')), label)
    if (label === 'push#') assert.equal(result.state.pending.stage, 'publication-unknown')
  }
  // A push relay gets one fresh agent: push.py reads back a push that landed.
  const pushed = await run({ reviews: oneValid, garble: (l, a) => l === 'push#1-review' ? cutHead(a) : a })
  assert.ok(pushed.labels.includes('push#1-review.retry'))
  assert.equal(pushed.calls.find(c => c.label === 'push#1-review.retry').model, 'sonnet')
  assert.equal(pushed.result.history[0].reviewPushFailed, undefined)
  // A retry that finds the branch elsewhere cannot prove the lost first attempt missed.
  const rejected = ({ seal, ...a }) => sealLine({ ...a, pushed: false, detail: ' ! [rejected] non-fast-forward', heads: a.heads.map(h => ({ ...h, head: FOREIGN })) })
  const moved = await run({ reviews: oneValid, garble: (l, a) => l === 'push#1-review' ? null : l === 'push#1-review.retry' ? rejected(a) : a })
  assert.equal(moved.result.state.pending.stage, 'publication-unknown')
  assert.match(moved.result.history[0].reviewPushFailed.detail, /non-fast-forward, after an earlier attempt lost its receipt$/)
  // A commit receipt is settled by the read-back instead: the commit that landed is audited and pushed.
  const commit = await run({ reviews: oneValid, garble: (l, a) => l.startsWith('commit#') ? { ...a, detail: `${a.detail}.` } : a })
  assert.ok(commit.logs.some(l => l.startsWith('commit#') && l.endsWith('the relayed copy does not match its seal')))
  assert.ok(commit.labels.some(l => l.startsWith('push#')), 'the push went ahead')
  assert.equal(commit.result.history[0].reviewPushFailed, undefined)
})

test("a relay that fills in error: '' on a sealed line still matches its seal", async () => {
  // Live on tinyusb #3988: the commit relay added error: '' and the landed commit went unpushed.
  const { result, labels, logs } = await run({ reviews: oneValid, garble: (l, a) => a && 'seal' in a && !a.error ? { ...a, error: '' } : a })
  assert.equal(logs.some(l => l.endsWith('does not match its seal')), false)
  assert.ok(labels.some(l => l.startsWith('push#')), 'the push went ahead')
  assert.equal(result.history[0].reviewPushFailed, undefined)
})

test('the pending-bot backoff is taken after the cycle summary', async () => {
  const { result, logs, napPoints } = await run({
    args: { maxCycles: 2 },
    reviews: { findings: [], replies: [], bots: 'pending' },
  })
  assert.equal(result.reason, 'reviews-pending')
  assert.equal(summaries(logs).length, 2, 'every cycle reports')
  assert.equal(napPoints.length, 1, 'no backoff after the last cycle')
  const firstSummaryAt = logs.findIndex(l => l.startsWith('cycle 1 summary'))
  assert.ok(napPoints[0] > firstSummaryAt, 'cycle 1 reported before the wait, not after it')
})

test('reviews go to the validator role directly, and no lane leaves the roles', async () => {
  // The `workflow` binding throws, so a run that reaches its verdict is itself
  // the proof that nothing nested a workflow; these are the only roles it may
  // dispatch to (the mechanical lanes carry a model, not an agentType).
  const ROLES = ['pr-ci-watcher', 'pr-review-validator', 'finding-verifier', 'code-writer']
  const wide = await run({
    reviews: {
      findings: [finding({ commentId: 1 }), invalidFinding({ commentId: 2, line: 4 })],
      replies: [{ commentId: 2, body: 'no' }], bots: 'reviewed',
    },
  })
  const scoped = await run({
    ci: {
      status: 'red', infraRerun: [],
      realFailures: [{ check: 'build / arm', firstError: 'no files in the log', files: [], verdict: 'real' }],
    },
    scope: ['src/a.c'],
  })
  const calls = [...wide.calls, ...scoped.calls]
  assert.equal(calls.find(c => c.label.startsWith('reviews#')).agentType, 'pr-review-validator')
  assert.equal(calls.find(c => c.label.startsWith('ci:judge#')).agentType, 'pr-ci-watcher')
  for (const c of calls) {
    if (c.agentType !== undefined) assert.ok(ROLES.includes(c.agentType), `${c.label} dispatched to ${c.agentType}`)
  }
  assert.ok(calls.some(c => c.label.startsWith('fix:')) && calls.some(c => c.label.startsWith('scope:')))
  // Dispatch only proves what the exercised branches did: a dormant workflow()
  // call would never be reached here, and runCycle would absorb the stub's throw
  // as 'cycle-threw' if it were. So refuse the spelling too, over the whole source.
  assert.doesNotMatch(body, /\bworkflow\s*\(/, 'this workflow must never nest another workflow')
  assert.doesNotMatch(body, /codex-agent/, 'the review lane is a Claude role, not a codex agent')
})

test('the harvest contract requires a findingId on every finding', async () => {
  const { calls } = await run()
  const items = calls.find(c => c.label.startsWith('reviews#')).schema.properties.findings.items
  assert.ok(items.required.includes('findingId'), 'REVIEWS no longer requires findingId')
  assert.ok(items.required.includes('commentDigest'), 'nor a digest to catch an edited comment')
  assert.equal(items.additionalProperties, false)
})

test('the cycle records what each posting lane reported', async () => {
  const { result, calls } = await run({
    reviews: {
      findings: [finding({ commentId: 1 }), invalidFinding({ commentId: 2, line: 4 })],
      replies: [{ commentId: 2, body: 'no' }], bots: 'reviewed',
    },
  })
  const entry = result.history[0]
  assert.deepEqual(entry.refutedPosts, { pass: true, detail: 'posted and read back', receipts: [receiptFor(2, 'no')], replied: [2] })
  assert.equal(entry.fixNotePosts.pass, true)
  assert.equal(entry.fixNotePosts.receipts[0].digest, fnv1a(manifestOf(calls, 'resolve#1')[0].body))
  const dead = await run({ reviews: oneValid, posting: null })
  assert.deepEqual(dead.result.history[0].fixNotePosts, { pass: false, detail: 'agent died', receipts: [], replied: [] })
})

test('every dismissal is challenged before it is posted', async () => {
  // The challenge protects the act of publicly refuting a reviewer. It is a
  // second Claude role; an independent model is the chief session's coworker lane.
  const { calls } = await run({
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
  })
  const ch = calls.find(c => c.label.startsWith('challenge#'))
  assert.ok(ch, 'a validated refutation must still be challenged')
  assert.equal(ch.agentType, 'finding-verifier')
})

test('an upheld refutation still replies and resolves', async () => {
  const { calls } = await run({
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    args: { autoPush: true },
  })
  const posted = calls.find(c => c.label.startsWith('replies#'))
  assert.ok(posted, 'expected the refutation to be posted')
  assert.match(posted.prompt, /"commentId":1/)
})

test('sibling refutations on one comment are merged into its single reply', async () => {
  // Posting resolves the thread, so only one reply per comment goes out - but
  // that reply retires every dismissal on the comment, so dropping a sibling
  // draft would retire a refutation the reviewer never saw.
  const { calls, result } = await run({
    reviews: {
      findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
      replies: [{ commentId: 7, body: 'the first point misreads the guard' },
        { commentId: 7, body: 'the second point is about dead code' }],
      bots: 'reviewed',
    },
    args: { autoPush: true, maxCycles: 1 },
  })
  const posted = calls.filter(c => c.label.startsWith('replies#'))
  assert.equal(posted.length, 1, 'one posting agent, one reply per thread')
  assert.match(posted[0].prompt, /the first point misreads the guard/)
  assert.match(posted[0].prompt, /the second point is about dead code/)
  assert.equal(result.pass, true, `both dismissals must be settled (got ${result.reason})`)
})

test('an overturned finding is fixed, replied to, and carries the challenger reason', async () => {
  const { calls } = await run({
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: false, reason: 'the NAK path is real' }] },
    args: { autoPush: true },
  })
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false, 'no refutation is posted')
  const fix = calls.find(c => c.label.startsWith('fix:'))
  assert.match(fix.prompt, /Challenger evidence: the NAK path is real\nOriginal fix hint \(advisory\): fix it/)
  // every finding on the comment was overturned, so the fix note is NOT withheld
  const resolve = calls.find(c => c.label.startsWith('resolve#'))
  assert.ok(resolve, 'expected the fix note to be posted')
  assert.match(resolve.prompt, /"commentId":1/)
})

test('an overturned finding with no hint carries the evidence alone', async () => {
  const { calls } = await run({
    reviews: { findings: [invalidFinding({ fixHint: '' })], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: false, reason: 'real' }] },
    args: { autoPush: true },
  })
  const fix = calls.find(c => c.label.startsWith('fix:'))
  assert.match(fix.prompt, /hint: Challenger evidence: real$/)
  assert.doesNotMatch(fix.prompt, /Original fix hint|undefined/)
})

test("a bot's fix prompt reaches the fixer as a hint and the verifier judges without it", async () => {
  const hint = 'In usbd.c around line 40, replace the early return\nwith a STALL of the endpoint.'
  const { calls } = await run({
    reviews: { findings: [finding({ source: 'coderabbit', fixHint: hint })], replies: [], bots: 'reviewed' },
    args: { autoPush: true },
  })
  const fix = calls.find(c => c.label.startsWith('fix:'))
  assert.ok(fix.prompt.includes(`[coderabbit] bad — hint: ${hint}`), 'the hint reaches the fixer verbatim')
  const check = calls.find(c => c.label.startsWith('check:'))
  assert.ok(check.prompt.includes(hint), 'the verifier sees the issue as the fixer did')
  assert.match(check.prompt, /independently of its hint/)
})

test('a mixed comment defers its reply and blocks the green exit', async () => {
  const { calls, result } = await run({
    reviews: {
      findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
      replies: [{ commentId: 7, body: 'both wrong' }],
      bots: 'reviewed',
    },
    challenge: { verdicts: [
      { id: 0, upheld: false, reason: 'real' },
      { id: 1, upheld: true, reason: 'stands' },
    ] },
    args: { autoPush: true, maxCycles: 1 },
  })
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false)
  const resolve = calls.find(c => c.label.startsWith('resolve#'))
  if (resolve) assert.doesNotMatch(resolve.prompt, /"commentId":7/)
  assert.equal(result.pass, false)
  assert.equal(result.reason, 'deferred-replies-unresolved')
  assert.deepEqual(result.deferred, [7])
})

test('no fix note is dispatched when the push answers no comment', async () => {
  // The mixed comment defers its note, so there is nothing to post. Dispatching
  // anyway risks throwing after the fix is already pushed, which would report
  // the cycle as a crash instead of re-arming for the deferred reply.
  const { calls, result } = await run({
    reviews: {
      findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
      replies: [{ commentId: 7, body: 'both wrong' }],
      bots: 'reviewed',
    },
    challenge: { verdicts: [
      { id: 0, upheld: false, reason: 'real' },
      { id: 1, upheld: true, reason: 'stands' },
    ] },
    throwOn: 'resolve#',
    args: { autoPush: true, maxCycles: 1 },
  })
  assert.equal(calls.some(c => c.label.startsWith('resolve#')), false, 'nothing to answer, nothing to dispatch')
  assert.equal(result.reason, 'deferred-replies-unresolved', `the push must not read as a crash (got ${result.reason})`)
})

test('two valid findings on one comment share one fix note', async () => {
  // One note per thread, as postReplyRecipe posts it - and it has to name both
  // findings, because paying the comment settles both; by place, as a claim of
  // any length would put it over the reply limit.
  let cycle = 0
  const { calls, result } = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? { findings: [finding({ commentId: 1, claim: 'the first leak' }),
          finding({ commentId: 1, line: 9, claim: 'the second leak '.repeat(30) })], replies: [], bots: 'reviewed' }
        : { findings: [], replies: [], bots: 'reviewed' }
    },
  })
  const resolve = calls.filter(c => c.label.startsWith('resolve#'))
  assert.equal(resolve.length, 1)
  assert.equal([...resolve[0].prompt.matchAll(/"commentId":1\b/g)].length, 1, 'one note for the thread')
  assert.match(resolve[0].prompt, /Fixed in [0-9a-f]{40}\.\\n\\n- src\/a\.c:1\\n- src\/a\.c:9"/, 'both findings named in the one note')
  assert.doesNotMatch(resolve[0].prompt, /leak/)
  assert.equal(result.pass, true, `the comment must be settled (got ${result.reason})`)
})

test('a dry run that runs out of cycles still reads as a dry run', async () => {
  // Bots never settle, so the green exit that reports dryRun is never reached
  // and the loop expires with the debt it was never allowed to post.
  const { result } = await run({
    args: { autoPush: false, maxCycles: 2 },
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'pending' },
  })
  assert.equal(result.reason, 'deferred-replies-unresolved')
  assert.deepEqual(result.deferred, [1])
  assert.equal(result.dryRun, true, 'unposted-by-design debt must not read as a failed reply workflow')
})

test('a broken challenge response fails the cycle', async () => {
  for (const challenge of [
    null,
    { verdicts: [] },
    { verdicts: [{ id: 9, upheld: true, reason: 'x' }] },
    { verdicts: [{ id: 0, upheld: true, reason: 'x' }, { id: 0, upheld: false, reason: 'y' }] },
  ]) {
    const { result } = await run({
      reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
      challenge, args: { autoPush: true, maxCycles: 1 },
    })
    assert.equal(result.reason, 'review-challenger-died')
  }
})

test('ids a challenger left unjudged go to one fresh challenger, alone', async () => {
  const reviews = {
    findings: [invalidFinding(), invalidFinding({ commentId: 2 })],
    replies: [{ commentId: 1, body: 'no' }, { commentId: 2, body: 'no' }], bots: 'reviewed',
  }
  const answers = (...rounds) => { let n = 0; return () => { const ids = rounds[n++]; return ids && { verdicts: ids.map(id => ({ id, upheld: true, reason: 'stands' })) } } }
  const healed = await run({ reviews, challengePerCycle: answers([0], [1]), args: { autoPush: true, maxCycles: 1 } })
  assert.notEqual(healed.result.reason, 'review-challenger-died')
  assert.ok(healed.logs.includes('cycle 1: the challenger judged 1 of 2 findings — a fresh one takes the rest'), healed.logs.join('\n'))
  const retry = healed.calls.find(c => c.label === 'challenge#1.retry')
  assert.deepEqual(JSON.parse(retry.prompt.match(/Findings: (\[.*\])\.$/s)[1]).map(x => x.id), [1])
  assert.deepEqual(manifestOf(healed.calls, healed.labels.find(l => l.startsWith('replies#'))).map(r => r.commentId), [1, 2])

  const revived = await run({ reviews, challengePerCycle: answers(null, [0, 1]), args: { autoPush: true, maxCycles: 1 } })
  assert.notEqual(revived.result.reason, 'review-challenger-died', 'a dead challenger gets one fresh one for every id')
  assert.deepEqual(JSON.parse(revived.calls.find(c => c.label === 'challenge#1.retry').prompt.match(/Findings: (\[.*\])\.$/s)[1]).map(x => x.id), [0, 1])

  // Each schema names exactly the ids asked, so the runtime has that agent correct a short answer.
  const [firstAsk, retryAsk] = ['challenge#1', 'challenge#1.retry'].map(l => healed.calls.find(c => c.label === l).schema.properties.verdicts)
  assert.deepEqual([firstAsk.minItems, firstAsk.maxItems, firstAsk.items.properties.id.enum], [2, 2, [0, 1]])
  assert.deepEqual([retryAsk.minItems, retryAsk.maxItems, retryAsk.items.properties.id.enum], [1, 1, [1]])
  assert.throws(() => conforms(retryAsk, [{ id: 0, verdict: 'justified', reason: 'x' }], 'v'), /not in 1/)
  assert.throws(() => conforms(firstAsk, [{ id: 0, verdict: 'justified', reason: 'x' }], 'v'), /1 items/)

  const still = await run({ reviews, challengePerCycle: answers([0], [0]), args: { autoPush: true, maxCycles: 1 } })
  assert.equal(still.result.reason, 'review-challenger-died', 'the fresh one must judge exactly the ids left')
  assert.equal(still.labels.some(l => l.startsWith('replies#')), false)
})

test('the summary marks an overturned finding', async () => {
  const { logs } = await run({
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: false, reason: 'real' }] },
    args: { autoPush: true, maxCycles: 1 },
  })
  // Named in the order the decision was made: the validator refutes, the
  // challenger overturns that dismissal.
  assert.ok(logs.some(l => l.includes('refuted, then overturned')),
    'cycle table must show the overturn in the order the roles acted')
})

test('a deferred obligation is cleared only by a posted reply', async () => {
  // Cycle 1 defers comment 7; only a later cycle's posted refutation clears it.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 4 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? { findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
          replies: [{ commentId: 7, body: 'both wrong' }], bots: 'reviewed' }
        : { findings: [finding({ commentId: 7, verdict: 'stale' }), invalidFinding({ commentId: 7, line: 9 })],
          replies: [{ commentId: 7, body: 'still wrong' }], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, upheld: false, reason: 'real' }, { id: 1, upheld: true, reason: 'stands' }] }
      : { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }] },
  })
  assert.equal(result.pass, true, `the posted reply must discharge the deferral (got ${result.reason})`)
})

test('an orphan reply is never posted unchallenged', async () => {
  // REVIEWS does not tie replies to findings, so a validator can emit a reply
  // for a commentId that has no non-valid finding. Nothing challenges it, and
  // posting it publicly refutes a reviewer on no one's authority.
  const { calls } = await run({
    reviews: {
      findings: [finding({ commentId: 1, verdict: 'valid' })],
      replies: [{ commentId: 99, body: 'you are wrong' }],
      bots: 'reviewed',
    },
    args: { autoPush: true, maxCycles: 1 },
  })
  const posted = calls.find(c => c.label.startsWith('replies#'))
  if (posted) assert.doesNotMatch(posted.prompt, /"commentId":99/, 'orphan reply was posted')
})

test('a stray doneId cannot discharge an unrelated deferral', async () => {
  // The posting agent returns receipts. Trusting an id that was never in
  // the payload lets one clear an obligation nobody answered.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 2 },
    strayDoneIds: [7],
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? { findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
          replies: [{ commentId: 7, body: 'both wrong' }], bots: 'reviewed' }
        : { findings: [], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => ({ verdicts: [
      { id: 0, upheld: false, reason: 'real' }, { id: 1, upheld: true, reason: 'stands' }] }),
  })
  assert.equal(result.reason, 'deferred-replies-unresolved',
    'a stray doneId discharged comment 7 without any reply being posted')
})

test('a refutation with no drafted reply is not silently dropped', async () => {
  // REVIEWS lets a validator report a non-valid finding and draft no reply for
  // it. Nothing else accounts for that answer, so the cycle could go green with
  // the reviewer's thread untouched.
  const { result } = await run({
    reviews: { findings: [invalidFinding({ commentId: 5 })], replies: [], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    args: { autoPush: true, maxCycles: 1 },
  })
  assert.equal(result.pass, false, 'an unanswered refutation must not pass')
  assert.deepEqual(result.deferred, [5])
})

test('a comment mixing valid and refuted findings is not resolved early', async () => {
  // Posting the refutation resolves the whole thread, hiding the valid finding
  // until its fix lands - and burying it for good if the fixer then fails.
  const { calls, result } = await run({
    reviews: {
      findings: [finding({ commentId: 3, verdict: 'valid' }), invalidFinding({ commentId: 3, line: 9 })],
      replies: [{ commentId: 3, body: 'the second one is wrong' }],
      bots: 'reviewed',
    },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    args: { autoPush: true, maxCycles: 1 },
  })
  const posted = calls.find(c => c.label.startsWith('replies#'))
  if (posted) assert.doesNotMatch(posted.prompt, /"commentId":3/, 'thread resolved before the fix')
  assert.equal(result.pass, false)
  assert.deepEqual(result.deferred, [3])
})

test('a fix note does not discharge a deferred refutation', async () => {
  // The fix note says "fixed in commit X"; it is not the refutation that was
  // owed. Letting its receipt clear the deferral answers the thread with the
  // wrong content and lets the next green cycle pass.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? { findings: [finding({ commentId: 4, verdict: 'valid' }), invalidFinding({ commentId: 4, line: 9 })],
          replies: [{ commentId: 4, body: 'the second is wrong' }], bots: 'reviewed' }
        : { findings: [finding({ commentId: 4, verdict: 'valid' })], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => ({ verdicts: [{ id: 0, upheld: true, reason: 'stands' }] }),
  })
  assert.equal(result.reason, 'deferred-replies-unresolved',
    'a fix note discharged a refutation it never made')
})

test('a comment is never replied to twice in one cycle', async () => {
  const { calls } = await run({
    reviews: {
      findings: [invalidFinding({ commentId: 8 })],
      replies: [{ commentId: 8, body: 'wrong' }, { commentId: 8, body: 'also wrong' }],
      bots: 'reviewed',
    },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    args: { autoPush: true, maxCycles: 1 },
  })
  const posted = calls.find(c => c.label.startsWith('replies#'))
  assert.ok(posted)
  assert.equal([...posted.prompt.matchAll(/"commentId":8/g)].length, 1, 'duplicate reply posted')
})

test('a deferred refutation can still be posted after a fix note', async () => {
  // The fix note records the comment in answeredWith. If that also blocks the
  // reply stage, the refutation its debt still owes can never go out and the
  // deferral is permanent.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 4 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) {
        return { findings: [finding({ commentId: 4, verdict: 'valid' }), invalidFinding({ commentId: 4, line: 9 })],
          replies: [{ commentId: 4, body: 'the second is wrong' }], bots: 'reviewed' }
      }
      if (cycle === 2) return { findings: [finding({ commentId: 4, verdict: 'valid' })], replies: [], bots: 'reviewed' }
      return { findings: [invalidFinding({ commentId: 4, line: 9 })],
        replies: [{ commentId: 4, body: 'still wrong' }], bots: 'reviewed' }
    },
    challengePerCycle: () => ({ verdicts: [{ id: 0, upheld: true, reason: 'stands' }] }),
  })
  assert.equal(result.pass, true, `the owed refutation must be postable (got ${result.reason})`)
})

test('an already-answered comment is not deferred for a missing draft', async () => {
  // An entry in answeredWith means the thread was answered and resolved.
  // Re-opening its debt because this harvest drafted no reply creates an
  // obligation nothing can discharge.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 3 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? { findings: [invalidFinding({ commentId: 7 })],
          replies: [{ commentId: 7, body: 'wrong' }], bots: 'reviewed' }
        : { findings: [invalidFinding({ commentId: 7 })], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => ({ verdicts: [{ id: 0, upheld: true, reason: 'stands' }] }),
  })
  assert.equal(result.pass, true, `an answered comment must not be re-deferred (got ${result.reason})`)
})

test('an obligation dies with the finding that created it', async () => {
  // Cycle 2 overturns the refuted half of cycle 1's mixed comment: only a fix note is owed.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 4 },
    reviewsPerCycle: () => {
      cycle++
      return { findings: [finding({ commentId: 6, verdict: 'valid' }),
        invalidFinding({ commentId: 6, line: 9 })],
      replies: [{ commentId: 6, body: 'the second is wrong' }], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] }
      : { verdicts: [{ id: 0, upheld: false, reason: 'actually real' }] },
  })
  assert.notEqual(result.reason, 'deferred-replies-unresolved',
    'the deferral outlived the refuted finding that created it')
  assert.equal(result.deferred, undefined)
})

test('overturning one refutation does not excuse a vanished sibling', async () => {
  // Of comment 7's two refutations one is dropped and one overturned: the dropped one is still owed.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 4 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) {
        return { findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
          replies: [], bots: 'reviewed' }
      }
      if (cycle === 2) return { findings: [invalidFinding({ commentId: 7, line: 9 })], replies: [], bots: 'reviewed' }
      return { findings: [], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }] }
      : { verdicts: [{ id: 0, upheld: false, reason: 'real' }] },
  })
  assert.equal(result.reason, 'deferred-replies-unresolved',
    'one overturn discharged an unrelated unanswered dismissal')
  assert.deepEqual(result.deferred, [7])
})

test('a retried fix note discharges its own carried obligation', async () => {
  // The first attempt posts nothing, so the fix note is owed into the next
  // cycle. Only that same answer can clear it - no refutation was ever owed.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 4 },
    dropDoneIds: (label) => cycle === 1 && label.startsWith('resolve#'),
    reviewsPerCycle: () => {
      cycle++
      return cycle <= 2 ? structuredClone(oneValid) : { findings: [], replies: [], bots: 'reviewed' }
    },
  })
  assert.equal(result.pass, true, `the retried fix note must clear its deferral (got ${result.reason})`)
})

test('a stale re-report of a fixed finding owes nothing', async () => {
  // The thread was answered and resolved by the fix note. Reporting the same
  // finding stale afterwards is a consequence of our own fix, not a dismissal
  // owed to the reviewer - and no reply is ever drafted for it.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 3 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? structuredClone(oneValid)
        : { findings: [finding({ verdict: 'stale' })], replies: [], bots: 'reviewed' }
    },
  })
  assert.equal(result.pass, true, `a stale re-report reopened a settled comment (got ${result.reason})`)
})

test('an edit to an answered comment owes an answer again', async () => {
  // The fix note answered and resolved comment 1. The reviewer then edits the
  // body: the point now being made is not the one we answered, so it accrues a
  // dismissal the run must not pass without.
  let cycle = 0
  const { result, logs } = await run({
    args: { autoPush: true, maxCycles: 3 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? structuredClone(oneValid)
        : { findings: [invalidFinding({ commentDigest: 'edited' })], replies: [], bots: 'reviewed' }
    },
  })
  assert.ok(logs.some(l => l.includes('comment 1 was edited after we answered it')), 'the edit must be reported')
  assert.notEqual(result.pass, true, 'an edit to an answered comment went unnoticed')
  assert.deepEqual(result.deferred, [1])
})

test('the answer owed after an edit may go beside ours, in this launch or a later one', async () => {
  // Without secondAnswer reply.py holds on any reply of ours; the edit is what allows the second.
  const edited = { findings: [invalidFinding({ commentDigest: 'edited' })], replies: [{ commentId: 1, body: 'Not so: line 3.' }], bots: 'reviewed' }
  let cycle = 0
  const same = await run({ args: { autoPush: true, maxCycles: 3 }, reviewsPerCycle: () => ++cycle === 1 ? structuredClone(oneValid) : structuredClone(edited) })
  assert.equal(manifestOf(same.calls, 'resolve#1')[0].secondAnswer, undefined, 'a first answer is no second')
  assert.equal(manifestOf(same.calls, 'replies#2')[0].secondAnswer, true)
  assert.equal(same.result.state.reanswer, undefined, 'the answer clears the marker')

  const first = await run({ reviews: oneValid, args: { ...YIELD } })
  const at = { head: first.result.state.expectedHead, prHead: first.result.state.expectedHead }
  const seen = await run({ reviews: { ...edited, replies: [] }, preflight: at, args: { ...YIELD, state: first.result.state } })
  assert.deepEqual(seen.result.state.reanswer, [1])
  const later = await run({ reviews: edited, preflight: at, args: { ...YIELD, state: seen.result.state } })
  assert.equal(manifestOf(later.calls, 'replies#3')[0].secondAnswer, true)
})

test('a fix note reads as deferred only while it still owes a dismissal', async () => {
  // Nothing is outstanding here: the fix note closed the thread, so calling it
  // deferred names a next cycle that has nothing to do.
  let cycle = 0
  const paid = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? structuredClone(oneValid)
        : { findings: [finding({ verdict: 'stale' })], replies: [], bots: 'reviewed' }
    },
  })
  assert.match(rowsOf(summaries(paid.logs)[1])[0][3], /already fixed, answered by fix note/)

  // Comment 4's refuted half is never replied to, so the fix note that answered
  // its valid half leaves that dismissal owed - and deferred is the right word.
  let mixed = 0
  const owing = await run({
    args: { autoPush: true, maxCycles: 3 },
    reviewsPerCycle: () => {
      mixed++
      if (mixed === 1) {
        return { findings: [finding({ commentId: 4, verdict: 'valid' }), invalidFinding({ commentId: 4, line: 9 })],
          replies: [], bots: 'reviewed' }
      }
      if (mixed === 2) return { findings: [finding({ commentId: 4, verdict: 'valid' })], replies: [], bots: 'reviewed' }
      return { findings: [invalidFinding({ commentId: 4, line: 9 })], replies: [], bots: 'reviewed' }
    },
  })
  assert.match(rowsOf(summaries(owing.logs)[2])[0][3], /refuted, deferred to next cycle/)
})

test('a dry run reports refutations it would post rather than deferring them', async () => {
  // Without autoPush nothing is posted, so an obligation would spin the loop to
  // exhaustion and call it unresolved. The review lane already says dryRun.
  const { calls, result } = await run({
    args: { autoPush: false, maxCycles: 3 },
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
  })
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false, 'a dry run must post nothing')
  assert.equal(result.dryRun, true)
  assert.equal(result.reason, undefined)
})

test('a dropped dismissal survives a later harvest that reports fewer', async () => {
  // Comment 7 owes two refutations; later harvests report one, then overturn and fix it.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 5 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) {
        return { findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
          replies: [], bots: 'reviewed' }
      }
      if (cycle <= 3) return { findings: [invalidFinding({ commentId: 7, line: 9 })], replies: [], bots: 'reviewed' }
      return { findings: [], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 3
      ? { verdicts: [{ id: 0, upheld: false, reason: 'real' }] }
      : { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }]
        .slice(0, cycle === 1 ? 2 : 1) },
  })
  assert.equal(result.reason, 'deferred-replies-unresolved',
    'a shrinking harvest discharged a dismissal nobody answered')
  assert.deepEqual(result.deferred, [7])
})

test('a stale reply after a failed fix note is a repair, not a second answer', async () => {
  // The fix note may be on the thread even though its receipt said otherwise;
  // the "already fixed" reply the stale re-report drafts would be a second
  // answer of another kind. The comment goes to a human with both facts.
  let cycle = 0
  const { result, calls } = await run({
    args: { autoPush: true, maxCycles: 4 },
    dropDoneIds: (label) => cycle === 1 && label.startsWith('resolve#'),
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) return structuredClone(oneValid)
      if (cycle === 2) return { findings: [finding({ verdict: 'stale' })], replies: [{ commentId: 1, body: 'already fixed' }], bots: 'reviewed' }
      return { findings: [], replies: [], bots: 'reviewed' }
    },
  })
  assert.equal(result.pass, false)
  assert.deepEqual(result.deferred, [1])
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false, 'the stale reply went out over a possible fix note')
  assert.deepEqual(result.state.debt.find(([id]) => id === 1)[1].repair, { replyId: null, error: 'offered fixNote is stale (now owes a refutation)' })
})

test('an answered comment is not replied to again for a stale re-report', async () => {
  // The fix note already resolved the thread. A drafted "already fixed" reply
  // for the same finding is a second answer to a closed thread.
  let cycle = 0
  const { calls, logs } = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? structuredClone(oneValid)
        : { findings: [finding({ verdict: 'stale' })], replies: [{ commentId: 1, body: 'already fixed' }], bots: 'reviewed' }
    },
  })
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false,
    'a resolved thread was answered twice')
  assert.ok(logs.includes('cycle 2: drafted reply/replies withheld — already answered: 1'), 'the log says why')
})

test('a stale finding never answered gets the dismissal reply; a refuted one is not answered again', async () => {
  let cycle = 0
  const { calls, logs } = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => {
      cycle++
      const findings = [invalidFinding({ commentId: 1 }), ...(cycle === 2 ? [finding({ commentId: 2, line: 2, verdict: 'stale' })] : [])]
      const replies = findings.map(f => ({ commentId: f.commentId, body: `about ${f.commentId}` }))
      return { findings, replies: cycle === 2 ? [...replies, { commentId: 99, body: 'about 99' }] : replies, bots: cycle === 1 ? 'pending' : 'reviewed' }
    },
  })
  assert.deepEqual(calls.filter(c => c.label.startsWith('replies#')).map(c => c.label), ['replies#1', 'replies#2'])
  assert.deepEqual(manifestOf(calls, 'replies#2'), [{ commentId: 2, body: 'about 2', digest: fnv1a('about 2') }])
  assert.ok(logs.includes('cycle 2: drafted reply/replies withheld — already answered: 1, no finding in this harvest: 1'))
})

const receiptFor = (commentId, body) =>
  ({ commentId, kind: 'review', replyId: 500 + commentId, digest: fnv1a(body), sent: true, posted: true, verified: true, resolved: true, error: null })

test('a reply is published by the script from a workflow-built manifest', async () => {
  // The poster gets a manifest and the script path; it composes no body and
  // runs no gh call of its own. The fix note's text is the workflow's.
  const { calls } = await run({
    reviews: {
      findings: [finding({ commentId: 1, file: 'src/a.c', line: 3, claim: 'off by one' }), invalidFinding({ commentId: 2, line: 4 })],
      replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed',
    },
  })
  for (const label of ['replies#1', 'resolve#1']) {
    const c = calls.find(x => x.label === label)
    assert.ok(c, `${label} ran`)
    assert.match(c.prompt, /skills\/pr-reply\/scripts\/reply\.py --pr 3888 --manifest/, `${label} runs the script`)
    assert.doesNotMatch(c.prompt, /gh api/, `${label} types no gh call`)
    assert.equal(c.schema.properties.receipts.items.additionalProperties, false, `${label} takes receipts only`)
  }
  const notes = manifestOf(calls, 'resolve#1')
  assert.equal(notes.length, 1)
  assert.equal(notes[0].commentId, 1)
  assert.match(notes[0].body, /^Fixed in [0-9a-f]{40}\.\n\n- src\/a\.c:3$/, 'the note names the pushed SHA and the finding\'s place')
  assert.equal(notes[0].digest, fnv1a(notes[0].body))
  const replies = manifestOf(calls, 'replies#1')
  assert.deepEqual(replies, [{ commentId: 2, body: 'not so', digest: fnv1a('not so') }])
})

test('a reply that landed with the wrong body is a repair for a human, never reposted or edited', async () => {
  const wrong = {
    args: { autoPush: true, maxCycles: 2 },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  }
  for (const [name, bad, error] of [['read-back', { wrongBody: (id) => id === 2 }, 'read-back mismatch on body'],
    ['other body', { receipts: (rs, l) => l === 'replies#1' ? rs.map(r => ({ ...r, digest: fnv1a('deliberately') })) : rs }, 'receipt for a different body']]) {
    const { result, logs, labels } = await run({ ...wrong, ...bad })
    assert.equal(result.history[0].refutedPosts.pass, false, name)
    assert.ok(logs.some(l => /reply 502 to comment 2 exists with the wrong content .* needs a human repair, not another reply/.test(l)), name)
    assert.deepEqual(labels.filter(l => /^(replies|inspect|reuse)#/.test(l)), ['replies#1', 'inspect#2', 'inspect#2-by-hand'], name)
    assert.ok(logs.some(l => /reply 502 to comment 2 still needs repair — the reply there is not the offered body: a human answers it/.test(l)), name)
    assert.notEqual(result.pass, true, name)
    assert.deepEqual(result.handoffs, [{ commentId: 2, why: `reply 502: ${error}; the reply there is not the offered body` }], name)
  }
})

test('a reply that reads back as the offered body settles on it with no verdict and no edit', async () => {
  // #18: the reply to a follow-up went out word for word, and read-back failed only on its parent.
  const wrong = {
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    inspect: () => ({ body: 'not so', bodyDigest: fnv1a('not so') }),
  }
  const parent = (rs, l) => l === 'replies#1' ? rs.map(r => ({ ...r, verified: false, resolved: null, error: 'read-back mismatch on thread' })) : rs
  const { result, labels, calls } = await run({ ...wrong, args: { autoPush: true, maxCycles: 2 }, receipts: parent })
  assert.deepEqual(labels.filter(l => /^(replies|inspect|reconcile|edit|reuse)#/.test(l)), ['replies#1', 'inspect#2', 'reuse#2'])
  assert.deepEqual(payloadOf(calls.find(c => c.label === 'reuse#2').prompt, 'Reuses').reuses,
    [{ commentId: 2, replyId: 502, bodyDigest: fnv1a('not so'), originalDigest: 'd2' }])
  assert.equal(result.pass, true, result.reason)
})

test('a repair obligation survives a restart and still blocks a repost', async () => {
  const first = await run({
    args: { autoPush: true, maxCycles: 2, yieldAfterCycle: true },
    wrongBody: (id) => id === 2,
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  const second = await run({
    args: { autoPush: true, maxCycles: 2, state: first.result.state },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(debtOf(first.result, 2).repair.replyId, 502)
  assert.match(debtOf(first.result, 2).repair.error, /^read-back mismatch on body/)
  assert.equal(second.calls.some(c => c.label.startsWith('replies#')), false, 'reposted over a reply that needs repair')
  assert.ok(second.result.handoffs.some(h => h.commentId === 2), 'the restored repair is still handed off')
})

// Comment 2 was answered by a reply (502) whose body reply.py will not post over.
const HELD = { replyId: 502, error: 'reply 502 of ours is already on this comment in other words; reconcile by hand' }
// reply.py --inspect finding the offered body word for word.
const asOffered = (body = 'not so') => ({ inspect: () => ({ body, bodyDigest: fnv1a(body) }) })
const heldForRepair = (over = {}) => ({
  args: { autoPush: true, maxCycles: 2 },
  alreadyThere: (id) => id === 2,
  reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
  ...over,
})

test('a reply already there in other words keeps the repair for a human, with no verifier', async () => {
  const { result, calls, labels, logs } = await run(heldForRepair())
  const inspect = calls.find(c => c.label === 'inspect#2')
  assert.match(inspect.prompt, /reply\.py --pr \d+ --inspect 2:502`/)
  assert.equal(labels.some(l => l.startsWith('reuse#')), false, 'no settling on another body')
  assert.deepEqual(result.state.debt.find(([id]) => id === 2)[1].repair, { ...HELD, error: `${HELD.error}; the reply there is not the offered body` })
  assert.ok(logs.some(l => /comment 2 still needs repair — the reply there is not the offered body/.test(l)))
  assert.equal(calls.filter(c => c.label.startsWith('replies#')).length, 1, 'nothing reposted')
})

test('a reply or comment that changed, or a reuse that is not verified, keeps the repair', async () => {
  for (const over of [
    { inspect: () => ({ originalDigest: 'd9' }) },
    { inspect: () => ({ body: null, bodyDigest: null, error: 'reply 502 is not ours on comment 2: mismatch on author' }) },
    { reuse: () => ({ verified: false, error: 'comment 2 was edited since the inspection' }) },
    { reuse: () => ({ verified: null, error: 'HTTP 502' }) },
    { reuse: () => ({ resolved: null, error: 'resolve failed' }) },
    { reuse: () => ({ posted: true }) },
    { reuse: null },
  ]) {
    const { result, logs } = await run(heldForRepair({ answers: () => true, ...over }))
    const { replyId, error } = result.state.debt.find(([id]) => id === 2)[1].repair
    assert.ok(replyId === HELD.replyId && error.startsWith(HELD.error), JSON.stringify(over))
    assert.ok(logs.some(l => /reply 502 to comment 2 still needs repair/.test(l)), JSON.stringify(over))
  }
})

test('a reply already there is not judged while a carried point is missing from the harvest', async () => {
  // Comment 2 owes two dismissals; this harvest reports only one of them.
  const both = [invalidFinding({ commentId: 2, line: 4 }), invalidFinding({ commentId: 2, line: 5 })]
  const first = await run(heldForRepair({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true },
    reviews: { findings: both, replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }] },
  }))
  assert.equal(first.result.state.debt.find(([id]) => id === 2)[1].dismissals.length, 2)
  const { result, labels } = await run({
    ...heldForRepair({ answers: () => true }),
    args: { autoPush: true, maxCycles: 3, state: first.result.state },
  })
  assert.equal(labels.some(l => /^(inspect|reconcile|reuse)#/.test(l)), false, 'nothing is judged on half the points')
  assert.ok(result.state.debt.find(([id]) => id === 2)[1].repair)
  assert.notEqual(result.pass, true)
})

test('a dry run inspects a reply already there, and settles nothing', async () => {
  const first = await run(heldForRepair({ args: { autoPush: true, maxCycles: 2, yieldAfterCycle: true } }))
  const { result, labels, logs } = await run({
    ...heldForRepair(asOffered()),
    args: { autoPush: false, maxCycles: 2, state: first.result.state },
  })
  assert.ok(labels.includes('inspect#2'))
  assert.equal(labels.some(l => l.startsWith('reuse#')), false)
  assert.ok(logs.some(l => /comment\(s\) 2 would settle on the replies already there \(dry run\)/.test(l)))
  assert.ok(result.state.debt.find(([id]) => id === 2)[1].repair)
})

test('a settled reuse survives a resumed launch and nothing is posted twice', async () => {
  const first = await run(heldForRepair({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true } }))
  assert.equal(first.result.handoffs, undefined, 'a repair exact reuse can still settle is not a human\'s yet')
  const second = await run({ ...heldForRepair(asOffered()), args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: first.result.state } })
  assert.ok(second.labels.includes('reuse#2'))
  assert.equal(second.result.handoffs, undefined)
  const third = await run({ ...heldForRepair(asOffered()), args: { autoPush: true, maxCycles: 3, state: second.result.state } })
  assert.equal(third.labels.some(l => /^(inspect|reuse|replies)#/.test(l)), false, 'an answered comment owes nothing')
})

// A launch that harvests `reviews` at `state`, resumed at its pushed head.
const resumeAt = (state, over) => run({ ...over, preflight: { head: state.expectedHead, prHead: state.expectedHead }, args: { ...over.args, state } })
const debtOf = (result, id) => result.state.debt.find(([c]) => c === id)[1]

test('a reply already there is not reused while the harvest leaves out a point it owes', async () => {
  const stale = (line) => finding({ commentId: 2, line, verdict: 'stale' })
  const first = await run(heldForRepair({
    args: { autoPush: true, maxCycles: 5, yieldAfterCycle: true },
    reviews: { findings: [finding({ commentId: 2, line: 4 }), finding({ commentId: 2, line: 5 })], replies: [], bots: 'reviewed' },
  }))
  assert.deepEqual([debtOf(first.result, 2).notes, !!debtOf(first.result, 2).repair], [['2#4', '2#5'], true])
  const offered = asOffered(debtOf(first.result, 2).attempt.body)
  const partial = await resumeAt(first.result.state, { args: { autoPush: true, maxCycles: 5, yieldAfterCycle: true }, ...offered,
    reviews: { findings: [stale(4)], replies: [], bots: 'reviewed' } })
  assert.equal(partial.labels.some(l => /^(inspect|reuse)#/.test(l)), false, 'a reused reply would settle one point of two')
  assert.notEqual(partial.result.pass, true)
  assert.deepEqual(debtOf(partial.result, 2).notes, ['2#4', '2#5'])
  const whole = await resumeAt(partial.result.state, { args: { autoPush: true, maxCycles: 5 }, ...offered,
    reviews: { findings: [stale(4), stale(5)], replies: [], bots: 'reviewed' } })
  assert.equal(whole.labels.some(l => /^(inspect|reuse)#\d+$/.test(l)), false, 'a fix note offered is not reused as the refutation now owed')
  assert.match(whole.result.handoffs.find(h => h.commentId === 2).why, /^reply 502: .*; it offered a \w[\w ]*, the comment now owes a refutation$/)
})

test('a partial harvest preserves a mixed fix and deferral reply for later exact reuse', async () => {
  const args = { autoPush: true, maxCycles: 5, yieldAfterCycle: true,
    deferrals: [deferral({ findingId: '2#5', commentDigest: 'd2' })] }
  const reviews = { findings: [finding({ commentId: 2, line: 4 }), finding({ commentId: 2, line: 5 })],
    replies: [], bots: 'reviewed' }
  const first = await run(heldForRepair({ args, reviews }))
  const attempt = { ...debtOf(first.result, 2).attempt }
  assert.equal(attempt.how, 'fixNote')
  assert.equal(debtOf(first.result, 2).repair.replyId, 502)
  const offered = asOffered(attempt.body)

  const partial = await resumeAt(first.result.state, { args, ...offered,
    reviews: { ...reviews, findings: [reviews.findings[1]] } })
  assert.deepEqual(debtOf(partial.result, 2).attempt, attempt)
  assert.deepEqual(debtOf(partial.result, 2).notes, ['2#4', '2#5'])
  assert.equal(partial.result.handoffs, undefined)
  assert.equal(partial.labels.some(l => /^(inspect|reuse)#/.test(l)), false)

  const whole = await resumeAt(partial.result.state, { args, ...offered, reviews })
  assert.ok(whole.labels.includes('inspect#3'))
  assert.ok(whole.labels.includes('reuse#3'))
  assert.ok(whole.logs.some(l => /cycle 3: comment 2 settled on reply 502, already there/.test(l)))
  assert.equal(whole.result.state.debt.some(([id]) => id === 2), false)
  assert.equal(whole.result.state.answeredWith.find(([id]) => id === 2)[1].how, 'fixNote')
  assert.equal(whole.result.handoffs, undefined)
})

// Comment 1 with two deferred points, left owed by a dry run.
const twoDeferred = { findings: [finding(), finding({ line: 2 })], replies: [], bots: 'reviewed' }
const deferredTwo = () => run({ reviews: twoDeferred, args: { autoPush: false, maxCycles: 5, deferrals: [deferral(), deferral({ findingId: '1#2' })] } })

test('no deferral reply goes out while the harvest leaves out a point it would settle', async () => {
  const dry = await deferredTwo()
  const covers = dry.calls.find(c => c.label === 'issue#1').schema.properties.verdicts
  assert.deepEqual([covers.minItems, covers.maxItems, covers.items.properties.findingId.enum], [2, 2, ['1#1', '1#2']])
  assert.deepEqual(debtOf(dry.result, 1).notes, ['1#1', '1#2'])
  const { labels, result, calls } = await run({ reviews: { ...twoDeferred, findings: [finding()] }, args: { autoPush: true, maxCycles: 5, state: dry.result.state } })
  assert.equal(labels.some(l => l.startsWith('defer#')), false, 'a deferral reply naming one point of two')
  assert.notEqual(result.pass, true)
  assert.match(calls.find(c => c.label === 'reviews#2').prompt,
    /report every finding on each as its body stands now, those listed by findingId among them, so they can be reconciled: \[\{"commentId":1,"findingIds":\["1#1","1#2"\]\}\]/)
})

test('no refutation goes out while the harvest leaves out a dismissal it would settle', async () => {
  let cycle = 0
  const { labels, result } = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => ++cycle === 1
      ? { findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })], replies: [], bots: 'reviewed' }
      : { findings: [invalidFinding({ commentId: 7, line: 9 })], replies: [{ commentId: 7, body: 'wrong' }], bots: 'reviewed' },
    challengePerCycle: () => ({ verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }].slice(0, cycle === 1 ? 2 : 1) }),
  })
  assert.equal(labels.some(l => l.startsWith('replies#')), false, 'a refutation of one point of two')
  assert.deepEqual(result.deferred, [7])
})

test('no fix note goes out while the harvest leaves out a note it would settle', async () => {
  let cycle = 0
  const { labels } = await run({
    args: { autoPush: true, maxCycles: 2 }, dropDoneIds: (label) => label === 'resolve#1',
    reviewsPerCycle: () => ++cycle === 1 ? { findings: [finding(), finding({ line: 2 })], replies: [], bots: 'reviewed' } : oneValid,
  })
  assert.ok(labels.includes('resolve#1') && labels.includes('fix:src/a.c'))
  assert.equal(labels.includes('resolve#2'), false, 'a fix note naming one point of two')
})

test('after an edit, the points reported against the new body must all be shown', async () => {
  const edited = (f) => ({ ...f, commentDigest: 'd2' })
  const both = { findings: [edited(finding()), edited(finding({ line: 2 }))], replies: [], bots: 'reviewed' }
  const dry = await deferredTwo()
  const seen = await run({ reviews: both, args: { autoPush: false, maxCycles: 5, state: dry.result.state,
    deferrals: [deferral({ commentDigest: 'd2' }), deferral({ findingId: '1#2', commentDigest: 'd2' })] } })
  assert.equal(debtOf(seen.result, 1).edited, true)
  const { labels, result } = await run({ reviews: { ...both, findings: [edited(finding())] }, args: { autoPush: true, maxCycles: 5, state: seen.result.state } })
  assert.equal(labels.some(l => l.startsWith('defer#')), false, 'a deferral reply naming one point of the edited body\'s two')
  assert.notEqual(result.pass, true)
})

test('an edited comment answered with a fix note still counts the points reported since the edit', async () => {
  // B is refuted at d1 and owed; the comment is edited (d2) and A is fixed and
  // noted there; B is then reported at d2. A refutation drafted from a harvest
  // with only stale A must not settle B.
  let cycle = 0
  const at2 = (f) => ({ ...f, commentDigest: 'd2' })
  const { labels, result, calls } = await run({
    args: { autoPush: true, maxCycles: 4 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) return { findings: [invalidFinding({ commentId: 7, line: 9 })], replies: [], bots: 'reviewed' }
      if (cycle === 2) return { findings: [at2(finding({ commentId: 7 }))], replies: [], bots: 'reviewed' }
      if (cycle === 3) return { findings: [at2(invalidFinding({ commentId: 7, line: 9 }))], replies: [], bots: 'reviewed' }
      return { findings: [at2(finding({ commentId: 7, verdict: 'stale' }))], replies: [{ commentId: 7, body: 'A is fixed' }], bots: 'reviewed' }
    },
  })
  assert.equal(labels.some(l => l.startsWith('resolve#')), false, 'a fix note on an edited comment')
  assert.equal(labels.some(l => l.startsWith('replies#')), false, 'a reply about A settling B')
  assert.notEqual(result.pass, true)
  assert.match(calls.find(c => c.label === 'reviews#3').prompt, /\{"commentId":7,"findingIds":\[\]\}/, 'a pre-edit id is not named as the edited body\'s')
})

test('a held id from before an edit is not named as the edited body\'s', async () => {
  let cycle = 0
  const { calls, result } = await run({
    args: { maxCycles: 3 },
    reviewsPerCycle: () => ++cycle === 1
      ? { findings: [invalidFinding(), invalidFinding({ line: 2 })], replies: [], bots: 'reviewed' }
      : { findings: [{ ...invalidFinding(), commentDigest: 'd2' }], replies: [], bots: 'reviewed' },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, verdict: 'unknown', reason: 'cannot tell' }] }
      : { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.deepEqual(result.state.holds.map(([id]) => id), ['1#2'], 'the hold itself is kept')
  assert.equal(debtOf(result, 1).edited, true)
  assert.equal(result.cycles, 2, 'no later cycle can answer a comment handed to a human')
  assert.equal(calls.some(c => c.label === 'reviews#3'), false)
})

// caller-approved deferrals

const ISSUE = 'https://github.com/hathach/tinyusb/issues/4000'
const deferral = (over = {}) => ({ findingId: '1#1', commentDigest: 'd1', issueUrl: ISSUE, reason: 'broken on master too; own PR', ...over })

test('deferrals are checked for shape before anything runs', async () => {
  for (const deferrals of ['1#1', [{ ...deferral(), findingId: '1' }], [deferral({ issueUrl: 'https://github.com/o/r/pull/3' })],
    [deferral({ reason: ' ' })], [deferral({ commentDigest: '' })], [deferral(), deferral()]]) {
    const trace = []
    await assert.rejects(run({ args: { deferrals }, trace }), /deferrals must be/, JSON.stringify(deferrals))
    assert.deepEqual(trace, [])
  }
})

test('a deferred finding is not fixed, is answered with its issue, and the run passes listing it', async () => {
  const { result, calls, labels, logs } = await run({ reviews: oneValid, args: { deferrals: [deferral()] } })
  assert.equal(labels.some(l => l.startsWith('fix:')), false, 'no writer for a deferred finding')
  const issue = calls.find(c => c.label === 'issue#1')
  assert.ok(issue.prompt.includes(ISSUE) && issue.prompt.includes('src/a.c:1: bad'))
  const [posted] = manifestOf(calls, 'defer#1')
  assert.equal(posted.body, `- src/a.c:1: Real, and out of this PR's scope: broken on master too; own PR. Tracked in ${ISSUE}.`)
  assert.equal(result.pass, true, JSON.stringify(result.reason))
  assert.deepEqual(result.deferrals, [{ findingId: '1#1', issueUrl: ISSUE }])
  assert.deepEqual(result.state.deferrals, [['1#1', { digest: 'd1', issueUrl: ISSUE, reason: 'broken on master too; own PR' }]])
  assert.deepEqual(result.state.answeredWith.find(([id]) => id === 1)[1], { how: 'deferral', digest: 'd1' })
  assert.match(rowsOf(summaries(logs)[0])[0][3], /^deferred, answered with the issue: https:\/\/github\.com\/hathach\/tinyusb\/issues\/4000$/)
})

test('a deferral reply that is not verified leaves the comment owed', async () => {
  const { result } = await run({ reviews: oneValid, args: { deferrals: [deferral()], maxCycles: 1 }, dropDoneIds: () => true })
  assert.notEqual(result.pass, true)
  assert.ok(result.state.debt.find(([id]) => id === 1), 'still owed an answer')
})

test('a deferral naming no current valid finding, or its issue not covering it, stops the run', async () => {
  for (const [over, why] of [
    [{ args: { deferrals: [deferral({ findingId: '9#9' })] } }, /9#9: no such finding in this harvest/],
    [{ args: { deferrals: [deferral({ commentDigest: 'd0' })] } }, /1#1: its comment changed since the decision/],
    [{ args: { deferrals: [deferral()] }, reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' } }, /1#1: the finding is invalid, not valid/],
    [{ args: { deferrals: [deferral()] }, covers: () => false }, /does not cover it/],
    [{ args: { deferrals: [deferral()] }, covers: () => null }, /could not be read/],
    [{ args: { deferrals: [deferral()] }, covers: null }, /its issue was not checked/],
  ]) {
    const { result, labels } = await run({ reviews: oneValid, ...over })
    assert.equal(result.reason, 'deferral-refused')
    assert.match(result.detail, why)
    assert.equal(labels.some(l => /^(fix:|defer#|replies#|resolve#)/.test(l)), false, 'nothing fixed or posted')
  }
})

test('a deferral whose reason alone breaks the reply limit is refused before anything runs', async () => {
  await assert.rejects(run({ reviews: oneValid, args: { deferrals: [deferral({ reason: 'w '.repeat(55).trim() })] } }),
    /deferral reason too long for its reply \(60 words, a line 300 characters with the issue URL\): 1#1/)
  await assert.rejects(run({ reviews: oneValid, args: { deferrals: [deferral(), deferral({ findingId: '1#2', reason: 'x'.repeat(250) })] } }),
    /: 1#2$/, 'a long line is refused too, and only the offending finding is named')
  const { calls } = await run({ reviews: oneValid, args: { deferrals: [deferral({ reason: 'w '.repeat(40).trim() })], maxCycles: 1 } })
  assert.ok(calls.length > 0, 'a reason within the limit runs')
})

test('a deferral point quotes no claim, and is refused only when its location takes it over the limit', async () => {
  const wordyClaim = { findings: [finding({ claim: 'w '.repeat(80) + 'x'.repeat(300) })], replies: [], bots: 'reviewed' }
  const posted = await run({ reviews: wordyClaim, args: { deferrals: [deferral()], autoPush: true, maxCycles: 2 } })
  assert.equal(manifestOf(posted.calls, 'defer#1')[0].body, `- src/a.c:1: Real, and out of this PR's scope: broken on master too; own PR. Tracked in ${ISSUE}.`)
  const { result, calls } = await run({ reviews: oneValid, args: { deferrals: [deferral({ reason: 'r '.repeat(50).trim() })], maxCycles: 2 } })
  assert.equal(result.reason, 'deferral-refused')
  assert.match(result.detail, /^1#1: its reply point would exceed 60 words or a line 300 characters; pass a shorter reason$/)
  assert.equal(calls.some(c => c.label.startsWith('defer#')), false, 'nothing is posted')
})

test('a deferral applied once holds on a resumed launch without being passed again, until the comment is edited', async () => {
  const first = await run({ reviews: oneValid, args: { deferrals: [deferral()], maxCycles: 3, yieldAfterCycle: true, autoPush: false } })
  const again = await run({ reviews: oneValid, args: { maxCycles: 3, state: first.result.state } })
  assert.notEqual(again.result.reason, 'budget-exhausted')
  assert.equal(again.labels.some(l => /^(issue#|fix:)/.test(l)), false, 'no second issue read, still no writer')
  const edited = await run({
    reviews: { findings: [finding({ commentDigest: 'd1-edited' })], replies: [], bots: 'reviewed' },
    args: { maxCycles: 3, state: first.result.state },
  })
  assert.equal(edited.result.reason, 'deferral-refused')
  assert.match(edited.result.detail, /1#1: its comment was edited since it was deferred; decide again/)
})

test('a deferred point rides in the one reply its mixed comment gets', async () => {
  const deferredPoint = finding({ findingId: '1#1', claim: 'deferred point' })
  const fixedPoint = finding({ findingId: '1#2', line: 2, claim: 'in scope' })
  const withFix = await run({
    reviews: { findings: [deferredPoint, fixedPoint], replies: [], bots: 'reviewed' },
    args: { deferrals: [deferral()] },
  })
  assert.equal(withFix.calls.filter(c => c.label.startsWith('fix:')).length, 1)
  assert.doesNotMatch(withFix.calls.find(c => c.label.startsWith('fix:')).prompt, /deferred point/)
  const [note] = manifestOf(withFix.calls, 'resolve#1')
  assert.match(note.body, /^Fixed in [0-9a-f]{40}\.\n\n- src\/a\.c:2\n\n- src\/a\.c:1: Real, and out of this PR's scope/)
  assert.equal(withFix.labels.some(l => l.startsWith('defer#')), false, 'one reply per comment')
  const refutedPoint = invalidFinding({ findingId: '1#2', line: 2, claim: 'wrong' })
  const withRefutation = await run({
    reviews: { findings: [deferredPoint, refutedPoint], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { deferrals: [deferral()] },
  })
  const [reply] = manifestOf(withRefutation.calls, 'replies#1')
  assert.match(reply.body, /^not so\n\n- src\/a\.c:1: Real, and out of this PR's scope/)
  assert.equal(withRefutation.labels.some(l => l.startsWith('defer#')), false)
})

test('a deferral reply waits while a sibling dismissal is still owed', async () => {
  // Cycle 1: deferred, valid and refuted points on one comment wait on each other.
  const points = [finding({ findingId: '1#1' }), finding({ findingId: '1#2', line: 2 }), invalidFinding({ findingId: '1#3', line: 3 })]
  const first = await run({
    reviews: { findings: points, replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    args: { deferrals: [deferral()], maxCycles: 3, yieldAfterCycle: true, autoPush: false },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.deepEqual(first.result.state.debt.find(([id]) => id === 1)[1].dismissals, ['1#3'])
  // Cycle 2 reports only the deferred point: answering it would close the thread over 1#3.
  const second = await run({ reviews: oneValid, args: { maxCycles: 3, state: first.result.state } })
  assert.equal(second.labels.some(l => l.startsWith('defer#')), false)
  assert.ok(second.result.state.debt.find(([id]) => id === 1))
})

test('a comment edited while a deferral note is owed goes to a human, even with the deferral renewed', async () => {
  const first = await run({ reviews: oneValid, args: { deferrals: [deferral()], maxCycles: 3, yieldAfterCycle: true, autoPush: false } })
  const edited = { findings: [finding({ commentDigest: 'd1-edited' })], replies: [], bots: 'reviewed' }
  const { result, labels } = await run({ reviews: edited, args: { deferrals: [deferral({ commentDigest: 'd1-edited' })], maxCycles: 3, state: first.result.state } })
  assert.equal(labels.some(l => l.startsWith('defer#')), false)
  assert.notEqual(result.pass, true)
  assert.deepEqual(result.handoffs, [{ commentId: 1, why: 'edited after its finding ids were carried' }])
})

test('a deferral already answered cannot be changed to another issue or reason', async () => {
  const first = await run({ reviews: oneValid, args: { deferrals: [deferral()], maxCycles: 3, yieldAfterCycle: true } })
  assert.ok(first.result.state.answeredWith.find(([id]) => id === 1))
  for (const changed of [deferral({ issueUrl: 'https://github.com/hathach/tinyusb/issues/4001' }), deferral({ reason: 'another reason' })]) {
    const { result, labels } = await run({ reviews: oneValid, args: { deferrals: [changed], maxCycles: 3, state: first.result.state } })
    assert.equal(result.reason, 'deferral-refused')
    assert.match(result.detail, /already answered as tracked in https:\/\/github\.com\/hathach\/tinyusb\/issues\/4000/)
    assert.equal(labels.some(l => l.startsWith('issue#')), false)
  }
})

test('a dry run applies a deferral but posts nothing', async () => {
  const { result, labels } = await run({ reviews: oneValid, args: { deferrals: [deferral()], autoPush: false } })
  assert.equal(result.dryRun, true)
  assert.equal(labels.some(l => /^(defer#|fix:)/.test(l)), false)
  assert.deepEqual(result.state.deferrals.map(([id]) => id), ['1#1'])
})

// decision continuity

// A launch refutes comment 1 and posts it; the resumed launch harvests `then`.
const refutedThen = async (then, over = {}) => {
  const first = await run({
    reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { maxCycles: 3, yieldAfterCycle: true },
  })
  return run({ ...over, reviews: then, args: { maxCycles: 3, state: first.result.state } })
}
// A challenger that confirms every finding called valid and upholds every dismissal.
const confirming = (reason = 'real now') => (x) => x.verdict === 'valid' ? { verdict: 'valid', reason } : { verdict: 'justified', reason: 'stands' }

test('each verdict is kept in the state and handed to the validator on a resumed launch', async () => {
  const first = await run({ reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' }, args: { maxCycles: 3, yieldAfterCycle: true } })
  const [[id, d]] = first.result.state.decisions
  assert.equal(id, '1#1')
  assert.deepEqual(d, { commentId: 1, digest: 'd1', reviewedSha: HEAD, file: 'src/a.c', line: 1, claim: 'bad', verdict: 'invalid', reason: 'the caller checks it' })
  const second = await run({ reviews: oneValid, args: { maxCycles: 3, state: first.result.state } })
  const prompt = second.calls.find(c => c.label.startsWith('reviews#')).prompt
  assert.match(prompt, /Earlier verdicts on this PR \(set related and changeReason against them per your procedure\): \[\{"findingId":"1#1","commentId":1,.*"verdict":"invalid","reason":"the caller checks it"\}\]/)
})

test('a reversal the challenger cannot settle is held: not fixed, not answered, still owed', async () => {
  for (const [then, earlier] of [
    [{ findings: [finding()], replies: [], bots: 'reviewed' }, 'invalid'],
    [{ findings: [finding({ findingId: '5#1', commentId: 5, file: 'src/b.c', related: '1#1' })], replies: [], bots: 'reviewed' }, 'invalid'],
  ]) {
    const { result, labels, logs } = await refutedThen(then)
    assert.equal(labels.some(l => l.startsWith('fix:')), false)
    assert.notEqual(result.pass, true)
    assert.ok(logs.some(l => new RegExp(`held — contradicts the earlier ${earlier} verdict on 1#1: the challenger could not settle it: cannot tell`).test(l)), logs.join('\n'))
    assert.match(rowsOf(summaries(logs)[0])[0][3], /^held: contradicts the earlier/)
  }
})

test('a hold survives a harvest that omits the finding, within a launch and across a resume, until reconciled', async () => {
  const flipped = { findings: [finding()], replies: [], bots: 'reviewed' }
  const empty = { findings: [], replies: [], bots: 'reviewed' }
  const first = await run({
    reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { maxCycles: 5, yieldAfterCycle: true },
  })
  let cycle = 0
  const within = await run({ reviewsPerCycle: () => ++cycle === 1 ? flipped : empty, args: { maxCycles: 5, state: first.result.state } })
  assert.notEqual(within.result.pass, true, 'omitting the held finding settles nothing')
  assert.deepEqual(within.result.state.holds.map(([id]) => id), ['1#1'])
  assert.match(within.calls.filter(c => c.label.startsWith('reviews#')).at(-1).prompt, /so they can be reconciled: \[\{"commentId":1,"findingIds":\["1#1"\]\}\]/)
  const held = await run({ reviews: flipped, args: { maxCycles: 5, yieldAfterCycle: true, state: first.result.state } })
  const resumed = await run({ reviews: empty, args: { maxCycles: 5, state: held.result.state } })
  assert.notEqual(resumed.result.pass, true, 'nor across a resume')
  const explained = await run({ reviews: { findings: [finding({ changeReason: 'new evidence: the ISR path skips the check' })], replies: [], bots: 'reviewed' }, challenge: confirming(), args: { maxCycles: 5, yieldAfterCycle: true, state: held.result.state } })
  assert.deepEqual(explained.result.state.holds, [])
  assert.ok(explained.logs.some(l => /1#1 reconciled/.test(l)))
})

test('a comment with a held finding is not answered while a later harvest omits it', async () => {
  let cycle = 0
  const { result, labels } = await run({
    args: { maxCycles: 3 },
    reviewsPerCycle: () => ++cycle === 1
      ? { findings: [invalidFinding(), invalidFinding({ line: 2 })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' }
      : { findings: [invalidFinding({ line: 2 })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, verdict: 'unknown', reason: 'cannot tell' }, { id: 1, upheld: true, reason: 'stands' }] }
      : { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.ok(cycle >= 2, 'the omitting harvest ran')
  assert.equal(labels.some(l => l.startsWith('replies#')), false, 'the held point keeps the thread open')
  assert.deepEqual(result.state.answeredWith, [])
  assert.deepEqual(result.state.holds.map(([id]) => id), ['1#1'])
  assert.notEqual(result.pass, true)
})

test('a held finding stays held against its earlier decision when a later harvest drops `related`', async () => {
  const first = await run({
    reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { maxCycles: 3, yieldAfterCycle: true },
  })
  const related = finding({ findingId: '5#1', commentId: 5, file: 'src/b.c', related: '1#1' })
  const held = await run({ reviews: { findings: [related], replies: [], bots: 'reviewed' }, args: { maxCycles: 3, yieldAfterCycle: true, state: first.result.state } })
  assert.deepEqual(held.result.state.holds, [['5#1', { commentId: 5, reason: 'contradicts the earlier invalid verdict on 1#1: the challenger could not settle it: cannot tell', against: '1#1' }]])
  const dropped = await run({ reviews: { findings: [{ ...related, related: null }], replies: [], bots: 'reviewed' }, args: { maxCycles: 3, state: held.result.state } })
  assert.equal(dropped.labels.some(l => l.startsWith('fix:')), false)
  assert.ok(!dropped.logs.some(l => /5#1 reconciled/.test(l)))
  assert.ok(dropped.logs.some(l => /5#1 held — contradicts the earlier invalid verdict on 1#1/.test(l)))
})

test('a held finding keeps its earlier decision through a new uncertainty and a different `related`', async () => {
  const first = await run({
    reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { maxCycles: 5, yieldAfterCycle: true },
  })
  const related = finding({ findingId: '5#1', commentId: 5, file: 'src/b.c', related: '1#1' })
  const held = await run({ reviews: { findings: [related], replies: [], bots: 'reviewed' }, args: { maxCycles: 5, yieldAfterCycle: true, state: first.result.state } })
  const unsure = await run({
    reviews: { findings: [{ ...related, verdict: 'invalid', related: null }], replies: [{ commentId: 5, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, verdict: 'unknown', reason: 'cannot tell' }] },
    args: { maxCycles: 5, yieldAfterCycle: true, state: held.result.state },
  })
  assert.equal(unsure.result.state.holds[0][1].against, '1#1', 'a new uncertainty keeps the earlier reference')
  for (const [state, rel] of [[unsure.result.state, null], [held.result.state, '5#1']]) {
    const again = await run({ reviews: { findings: [{ ...related, related: rel }], replies: [], bots: 'reviewed' }, args: { maxCycles: 5, state } })
    assert.equal(again.labels.some(l => l.startsWith('fix:')), false, `related ${rel}`)
    assert.ok(again.logs.some(l => /5#1 held — contradicts the earlier invalid verdict on 1#1/.test(l)), `related ${rel}`)
  }
})

test('a hold on an answered comment is named in every exit that lists what is owed', async () => {
  const first = await run({
    reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { maxCycles: 5, yieldAfterCycle: true },
  })
  const flipped = { findings: [finding()], replies: [], bots: 'reviewed' }
  const yielded = await run({ reviews: flipped, args: { maxCycles: 5, yieldAfterCycle: true, state: first.result.state } })
  assert.deepEqual(yielded.result.state.debt, [], 'comment 1 is answered, so it accrues no debt')
  assert.deepEqual(yielded.result.deferred, [1])
  const spent = await run({ reviews: flipped, args: { maxCycles: 5, state: yielded.result.state } })
  assert.equal(spent.result.reason, 'deferred-replies-unresolved')
  assert.deepEqual(spent.result.deferred, [1])
})

test('a finding related to another comment keeps its own debt, and settling one does not settle the other', async () => {
  const { result } = await refutedThen({ findings: [
    invalidFinding({ reason: 'the caller checks it' }),
    finding({ findingId: '5#1', commentId: 5, file: 'src/b.c', related: '1#1', changeReason: 'new evidence: b.c has no caller check' }),
  ], replies: [], bots: 'reviewed' }, { challenge: confirming() })
  assert.deepEqual(result.state.answeredWith.map(([id]) => id), [1, 5], 'comment 1 stays answered by its refutation, comment 5 gets its own note')
  const first = await run({
    reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { maxCycles: 3, yieldAfterCycle: true },
  })
  const unsettled = await run({
    reviews: { findings: [finding({ findingId: '5#1', commentId: 5, file: 'src/b.c', related: '1#1', changeReason: 'new evidence' })], replies: [], bots: 'reviewed' },
    args: { maxCycles: 3, yieldAfterCycle: true, state: first.result.state }, posting: () => null, challenge: confirming(),
  })
  assert.ok(unsettled.result.state.debt.find(([id]) => id === 5), 'comment 5 owes its note')
  assert.deepEqual(unsettled.result.state.answeredWith.map(([id]) => id), [1], 'comment 1 answered, comment 5 not')
})

test('a valid finding now dismissed goes to the challenger with the earlier verdict, which settles the reversal', async () => {
  const run2 = (challenge) => {
    let cycle = 0
    return run({
      args: { autoPush: true, maxCycles: 2 }, ...(challenge ? { challenge } : {}),
      reviewsPerCycle: () => ++cycle === 1 ? oneValid
        : { findings: [invalidFinding({ changeReason: 'the fix landed differently' })], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    })
  }
  const confirmed = await run2()
  const ch = confirmed.calls.find(c => c.label === 'challenge#2')
  assert.match(ch.prompt, /"earlier":\{"verdict":"valid","reason":"","reviewedSha":"[0-9a-f]{40}"\},"changeReason":"the fix landed differently"/)
  assert.match(ch.prompt, /also says the earlier one no longer holds, and your reason must say why/)
  assert.ok(!confirmed.logs.some(l => /1#1 held/.test(l)), 'a justified reversal is not held')
  const unsure = await run2({ verdicts: [{ id: 0, verdict: 'unknown', reason: 'cannot tell' }] })
  assert.ok(unsure.logs.some(l => /1#1 held — contradicts the earlier valid verdict on 1#1: the challenger could not settle it: cannot tell/.test(l)))
  assert.equal(unsure.result.state.holds[0][1].against, '1#1', 'the hold names the decision it contradicts')
  assert.equal(unsure.labels.filter(l => l.startsWith('replies#')).length, 0)
  let cycle = 0
  const landed = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => ++cycle === 1 ? oneValid : { findings: [finding({ verdict: 'stale' })], replies: [], bots: 'reviewed' },
  })
  assert.ok(!landed.logs.some(l => /held/.test(l)), 'valid to stale is a fix landing')
})

test('a challenger overturning a posted dismissal is shown the earlier verdict', async () => {
  const again = { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' }
  const { calls, labels, result } = await refutedThen(again, { challenge: { verdicts: [{ id: 0, verdict: 'valid', reason: 'the ISR path skips the check' }] } })
  assert.match(calls.find(c => c.label.startsWith('challenge#')).prompt, /"earlier":\{"verdict":"invalid","reason":"the caller checks it"/)
  assert.ok(labels.includes('fix:src/a.c'), 'an overturn with evidence, against the earlier verdict it saw, is acted on')
  assert.deepEqual(result.corrections.map(c => c.now), ['the challenger: the ISR path skips the check'], 'the reversal the overturn made is reported')
})

test('a challenger verdict with no evidence, or a verdict list with a stray id, settles nothing', async () => {
  const flipped = { findings: [finding({ changeReason: 'new evidence' })], replies: [], bots: 'reviewed' }
  const blank = await refutedThen(flipped, { challenge: () => ({ verdict: 'valid', reason: ' ' }) })
  assert.equal(blank.labels.some(l => l.startsWith('fix:')), false)
  assert.ok(blank.logs.some(l => /1#1 held — .*the challenger gave no evidence/.test(l)))
  const stray = await refutedThen(flipped, { challenge: { verdicts: [{ id: 0, verdict: 'valid', reason: 'real' }, { id: 99, verdict: 'valid', reason: 'real' }] } })
  assert.equal(stray.labels.some(l => l.startsWith('fix:')), false)
  assert.equal(stray.result.reason, 'review-challenger-died')
  assert.ok(stray.logs.some(l => /1#1 held — .*the reversal was not checked/.test(l)))
  const silent = await run({ reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, verdict: 'justified', reason: '' }] }, args: { maxCycles: 1 } })
  assert.equal(silent.labels.some(l => l.startsWith('replies#')), false, 'a dismissal nobody gave evidence for is not posted')
  assert.ok(silent.logs.some(l => /1#1 held — the challenger gave no evidence/.test(l)))
})

test('a dismissal the validator turns valid is checked by the challenger, and its reason alone settles nothing', async () => {
  const flipped = { findings: [finding({ changeReason: 'new evidence: the ISR path skips the check' })], replies: [], bots: 'reviewed' }
  const upheld = await refutedThen(flipped, { challenge: () => ({ verdict: 'justified', reason: 'the caller still checks it' }) })
  assert.equal(upheld.labels.some(l => l.startsWith('fix:')), false)
  assert.ok(upheld.logs.some(l => /1#1 held — contradicts the earlier invalid verdict on 1#1: the challenger upheld the earlier dismissal: the caller still checks it/.test(l)), upheld.logs.join('\n'))
  const ch = upheld.calls.find(c => c.label.startsWith('challenge#'))
  assert.match(ch.prompt, /"verdict":"valid","reason":"","earlier":\{"verdict":"invalid","reason":"the caller checks it","reviewedSha":"[0-9a-f]{40}"\},"changeReason":"new evidence: the ISR path skips the check"/)
  const confirmed = await refutedThen(flipped, { challenge: () => ({ verdict: 'valid', reason: 'the ISR path skips it' }) })
  assert.ok(confirmed.labels.includes('fix:src/a.c'))
  assert.equal(confirmed.result.corrections[0].now, 'the challenger: the ISR path skips it')
  const unexplained = await refutedThen({ findings: [finding()], replies: [], bots: 'reviewed' }, { challenge: () => ({ verdict: 'valid', reason: 'the ISR path skips it' }) })
  assert.ok(unexplained.labels.includes('fix:src/a.c'), 'the challenger settles a reversal the validator left unexplained')
})

test('an unchecked reversal is held before the run stops, and a resume that omits it is still blocked', async () => {
  const first = await run({
    reviews: { findings: [invalidFinding({ reason: 'the caller checks it' })], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    args: { maxCycles: 5, yieldAfterCycle: true },
  })
  const dead = await run({ reviews: { findings: [finding({ changeReason: 'new evidence' })], replies: [], bots: 'reviewed' }, challenge: null, args: { maxCycles: 5, state: first.result.state } })
  assert.equal(dead.result.reason, 'review-challenger-died')
  assert.deepEqual(dead.result.state.holds, [['1#1', { commentId: 1, reason: 'contradicts the earlier invalid verdict on 1#1: the reversal was not checked', against: '1#1' }]])
  assert.equal(dead.result.state.decisions[0][1].verdict, 'invalid', 'the earlier decision is not updated')
  const omitted = await run({ reviews: { findings: [], replies: [], bots: 'reviewed' }, args: { maxCycles: 5, state: dead.result.state } })
  assert.notEqual(omitted.result.pass, true)
  assert.deepEqual(omitted.result.state.holds.map(([id]) => id), ['1#1'])
})

test('an explained flip on a posted refutation is fixed and reported as a correction, with no reply', async () => {
  const { result, labels, logs } = await refutedThen({ findings: [finding({ changeReason: 'earlier error: the caller does not check it on the ISR path' })], replies: [], bots: 'reviewed' },
    { challenge: confirming('the caller does not check it on the ISR path') })
  assert.ok(labels.includes('fix:src/a.c'))
  assert.equal(labels.some(l => l.startsWith('resolve#')), false, 'no correction reply')
  assert.deepEqual(result.corrections, [{
    findingId: '1#1', earlier: { findingId: '1#1', verdict: 'invalid', reason: 'the caller checks it' },
    now: 'the challenger: the caller does not check it on the ISR path',
  }])
  assert.ok(logs.some(l => /1#1 was refuted in a posted reply and is valid now .* reported, no correction posted/.test(l)))
})

test('a challenger that cannot settle a dismissal neither posts it nor fixes the finding', async () => {
  const { result, labels, logs } = await run({
    args: { maxCycles: 1 },
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, verdict: 'unknown', reason: 'the ISR path is not visible from here' }] },
  })
  assert.equal(labels.some(l => /^(replies#|fix:)/.test(l)), false)
  assert.ok(logs.some(l => /1#1 held — the challenger could not settle it: the ISR path/.test(l)))
  assert.deepEqual(result.state.debt.find(([id]) => id === 1)[1].dismissals, ['1#1'])
  assert.equal(result.state.decisions.length, 0, 'a held verdict is not recorded as a decision')
})

test('the challenger is asked for three verdicts', async () => {
  const { calls } = await run({ reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' } })
  const ch = calls.find(c => c.label === 'challenge#1')
  assert.match(ch.prompt, /'justified' when it really is invalid or already fixed/)
  assert.match(ch.prompt, /'unknown' when you cannot establish either/)
  assert.deepEqual(ch.schema.properties.verdicts.items.properties.verdict.enum, ['justified', 'valid', 'unknown'])
})

test('a receipt for a different body settles nothing', async () => {
  // The posting agent transcribed the manifest wrong, or answered with some
  // other reply's receipt: the digest does not match the body the workflow
  // handed out, so the comment stays owed.
  const { result, logs } = await run({
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    receipts: (rs) => rs.map(r => ({ ...r, digest: fnv1a('@/tmp/body.txt') })),
  })
  assert.equal(result.pass, false)
  assert.deepEqual(result.deferred, [2])
  assert.ok(logs.some(l => /receipt for comment 2 is for a different body/.test(l)), logs.join('\n'))
  assert.equal(result.rollup.replies, 0, 'an untrusted receipt is no reply')
})

test('two receipts for one comment, or success without a reply id, settle nothing', async () => {
  const twice = await run({
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    receipts: (rs) => [{ ...rs[0], verified: false, error: 'read-back mismatch on body' }, rs[0]],
  })
  assert.deepEqual(twice.result.deferred, [2], 'a mismatch followed by a success is contradictory')
  assert.equal(twice.result.rollup.replies, 0)
  assert.deepEqual(twice.result.state.debt.find(([id]) => id === 2)[1].repair, { replyId: 502, error: 'contradictory receipts' },
    'the reply the receipts name is kept so nothing is posted over it')
  const noId = await run({
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    receipts: (rs) => rs.map(r => ({ ...r, kind: 'issue', replyId: null, resolved: null })),
  })
  assert.deepEqual(noId.result.deferred, [2], 'verified with no reply id is impossible')
})

test('a comment on none of the PR\'s id spaces owes nothing', async () => {
  // Every space searched, nothing found: no reply can ever pay it, so the
  // debt is dropped, not carried through every later cycle. No reply exists,
  // so answeredWith stays empty and the summary says so.
  let cycle = 0
  const validOnce = () => (++cycle === 1 ? oneValid : { findings: [], replies: [], bots: 'reviewed' })
  const { result } = await run({ args: { autoPush: true, maxCycles: 2 }, reviewsPerCycle: validOnce, noTarget: (id) => id === 1 })
  assert.equal(result.pass, true, `nothing is owed (got ${result.reason})`)
  assert.deepEqual(result.state.debt, [])
  assert.deepEqual(result.state.answeredWith, [])
  assert.equal(result.history[0].fixNotePosts.detail, 'not on the PR, nothing owed: 1')
  const refuted = await run({
    args: { autoPush: true },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    noTarget: (id) => id === 2,
  })
  assert.equal(refuted.result.pass, true, 'a refutation with no target owes nothing either')
  assert.deepEqual(refuted.result.state.debt, [])
  assert.match(rowsOf(summaries(refuted.logs)[0])[0].join('|'), /refuted, no reply: comment is not on the PR/)
})

test('only an explicit none receipt retires a debt', async () => {
  // A lookup that failed (kind null) or a receipt for another body proves
  // nothing about whether the comment exists; the debt stays.
  for (const [name, reshape] of [
    ['lookup failed', (r) => ({ ...r, kind: null, sent: false, replyId: null, verified: false, error: 'HTTP 502' })],
    ['wrong digest', (r) => ({ ...r, kind: 'none', sent: false, replyId: null, verified: false, digest: 'deadbeef', error: 'not on PR' })],
    ['two receipts', (r) => [r, r].map(x => ({ ...x, kind: 'none', sent: false, replyId: null, verified: false }))],
  ]) {
    const { result } = await run({
      args: { autoPush: true },
      reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
      challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
      receipts: (rs) => rs.flatMap(reshape),
    })
    assert.deepEqual(result.deferred, [2], name)
    assert.ok(result.state.debt.some(([id]) => id === 2), `${name}: the debt is kept`)
  }
})

test('a none receipt that also names a reply is contradictory, not a retirement', async () => {
  // The script's absence shape is exact: kind none with no POST and no reply.
  // A receipt that says none and still carries a reply id can only be a
  // transcription error, so the debt is kept and the reply it names is the repair.
  for (const [name, reshape] of [
    ['mismatch', (r) => ({ ...r, kind: 'none', verified: false, resolved: null, error: 'read-back mismatch on body' })],
    ['success-shaped', (r) => ({ ...r, kind: 'none' })],
    ['sent, no reply', (r) => ({ ...r, kind: 'none', replyId: null, posted: false, verified: false, resolved: null })],
  ]) {
    const { result } = await run({
      args: { autoPush: true },
      reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
      challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
      receipts: (rs) => rs.map(reshape),
    })
    assert.deepEqual(result.deferred, [2], name)
    assert.deepEqual(result.state.answeredWith, [], `${name}: nothing was paid`)
    const [, d] = result.state.debt.find(([id]) => id === 2)
    assert.deepEqual(d.repair, name === 'sent, no reply' ? undefined : { replyId: 502, error: 'contradictory receipt' }, name)
  }
})

test('a refutation paid before a later worker threw stays paid in the failed cycle', async () => {
  // The refutation is posted before the fix is published. When a later step
  // then throws (an agent's throw settles to null, so it takes a malformed
  // recheck, whose staged list is not a list), the cycle is reported as thrown,
  // but the ledger already moved: the comment is answered and its receipt is in
  // the cycle's history.
  const { result } = await run({
    args: { autoPush: true },
    reviews: { findings: [finding({ commentId: 1 }), invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    recheck: { staged: null },
  })
  assert.equal(result.reason, 'cycle-threw')
  assert.equal(result.history[0].refutedPosts.pass, true)
  assert.deepEqual(result.state.answeredWith.map(([id, a]) => [id, a.how]), [[2, 'refutation']])
  assert.equal(result.state.debt.some(([id]) => id === 2), false)
})

test('a retired comment harvested again owes again', async () => {
  // Retirement is per cycle: the id was on no space this cycle. A later harvest
  // that reports it anew, without a draft, opens a fresh obligation, and the
  // summary calls it pending rather than retired.
  let cycle = 0
  const { result, logs } = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => ({
      findings: [invalidFinding({ commentId: 2, line: 4 })],
      replies: ++cycle === 1 ? [{ commentId: 2, body: 'not so' }] : [], bots: cycle > 1 ? 'reviewed' : 'pending',
    }),
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    noTarget: (id) => cycle === 1 && id === 2,
  })
  assert.equal(result.reason, 'deferred-replies-unresolved')
  assert.deepEqual(result.deferred, [2])
  assert.match(rowsOf(summaries(logs)[0])[0].join('|'), /no reply: comment is not on the PR/)
  assert.match(rowsOf(summaries(logs)[1])[0].join('|'), /refuted, reply pending/)
})

test('a verified review-body receipt pays like an issue comment', async () => {
  let cycle = 0
  const validOnce = () => (++cycle === 1 ? oneValid : { findings: [], replies: [], bots: 'reviewed' })
  const { result } = await run({ args: { autoPush: true, maxCycles: 2 }, reviewsPerCycle: validOnce, reviewBody: (id) => id === 1 })
  assert.equal(result.pass, true, result.reason)
  assert.deepEqual(result.state.answeredWith.map(([id, a]) => [id, a.how]), [[1, 'fixNote']])
  assert.equal(result.history[0].fixNotePosts.detail, 'posted and read back')
})

test('an unavailable read-back is retried, not repaired', async () => {
  let cycle = 0
  const { result, calls } = await run({
    args: { autoPush: true, maxCycles: 2 },
    unreadable: (id) => cycle === 1 && id === 2,
    reviewsPerCycle: () => {
      cycle++
      return { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' }
    },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(result.pass, true, `the retry settles it (got ${result.reason})`)
  assert.equal(calls.filter(c => c.label.startsWith('replies#')).length, 2)
  assert.equal(result.history[0].refutedPosts.pass, false)
  assert.equal(result.history[0].refutedPosts.receipts[0].verified, null)
})

test('a reply receipt whose copy does not match its seal gets one fresh relay on Sonnet', async () => {
  // #3988: a Haiku relay nulled replyId and kind, and two posted replies went unsettled for a cycle.
  const nulled = (a) => ({ ...a, receipts: a.receipts.map(r => ({ ...r, replyId: null, kind: null })) })
  const reviews = { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' }
  const { result, labels, calls, logs } = await run({ reviews, garble: (l, a) => l === 'replies#1' ? nulled(a) : a })
  assert.ok(logs.some(l => l === 'replies#1: the relayed copy does not match its seal'), logs.join('\n'))
  assert.deepEqual(labels.filter(l => l.startsWith('replies#')), ['replies#1', 'replies#1.retry'])
  assert.equal(calls.find(c => c.label === 'replies#1.retry').model, 'sonnet')
  assert.deepEqual(manifestOf(calls, 'replies#1.retry'), manifestOf(calls, 'replies#1'), 'the same manifest: reply.py reuses what landed')
  assert.equal(result.pass, true, result.reason)
})

test('a retry offers the body first posted, not the redraft', async () => {
  // A lost receipt or a failed resolve leaves a reply on the thread. The script
  // reuses only an identical body, so the next cycle must hand it the same text
  // even when the validator drafted new words.
  let cycle = 0
  const { result, calls, logs } = await run({
    args: { autoPush: true, maxCycles: 2 },
    posting: (label) => cycle === 1 && label.startsWith('replies#') ? null : true,
    reviewsPerCycle: () => {
      cycle++
      return { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: cycle === 1 ? 'first wording' : 'second wording' }], bots: 'reviewed' }
    },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(result.pass, true, `settled on the retry (got ${result.reason})`)
  const bodies = calls.filter(c => c.label.startsWith('replies#')).map(c => [c.label, manifestOf(calls, c.label)[0].body])
  assert.deepEqual(bodies, [['replies#1', 'first wording'], ['replies#1.retry', 'first wording'], ['replies#2', 'first wording']])
  assert.ok(logs.some(l => /comment 2 keeps the body already offered/.test(l)), logs.join('\n'))
  assert.equal(result.state.debt.length, 0, 'paid debt carries no attempt')
})

test('any failed attempt keeps the offered body: no receipt proves nothing landed', async () => {
  for (const [name, hook] of [['clean failure', 'dropDoneIds'], ['lost response', 'lost']]) {
    let cycle = 0
    const { result, calls } = await run({
      args: { autoPush: true, maxCycles: 2 },
      [hook]: () => cycle === 1,
      reviewsPerCycle: () => {
        cycle++
        return { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: cycle === 1 ? 'first wording' : 'second wording' }], bots: 'reviewed' }
      },
      challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
    })
    assert.equal(result.pass, true, name)
    assert.deepEqual(calls.filter(c => c.label.startsWith('replies#')).map(c => manifestOf(calls, c.label)[0].body),
      ['first wording', 'first wording'], name)
  }
})

test('the offered body survives a restart', async () => {
  const first = await run({
    args: { autoPush: true, maxCycles: 2, yieldAfterCycle: true },
    posting: null,
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'first wording' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.deepEqual(first.result.state.debt.find(([id]) => id === 2)[1].attempt, { body: 'first wording', how: 'refutation', digest: 'd2' })
  const second = await run({
    args: { autoPush: true, maxCycles: 2, state: first.result.state },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'second wording' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(manifestOf(second.calls, 'replies#2')[0].body, 'first wording')
  assert.equal(second.result.pass, true)
})

test('a green PR whose only debt is handed to a human stops at once instead of re-arming', async () => {
  const { result, labels } = await run({
    args: { autoPush: true, maxCycles: 4 },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'w '.repeat(61).trim() }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(result.reason, 'deferred-replies-unresolved')
  assert.equal(result.cycles, 1)
  assert.equal(labels.includes('reviews#2'), false)
  assert.equal(result.handoffs[0].commentId, 2)
})

test('the drafter is told both reply limits, and a draft within the words but over the line limit goes to a human', async () => {
  // #3988: a draft with one line over 300 characters was withheld twice and the reply stayed owed.
  const long = `Not so: ${'x'.repeat(295)}`
  const { calls, logs, result } = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: long }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.match(calls.find(c => c.label === 'reviews#1').prompt, /each point has at most 60 words and no line is over 300 characters/)
  assert.ok(logs.some(l => /no reply posted to comment 2 \(over length: .* a line 300 characters\) — a human answers it/.test(l)), logs.join('\n'))
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false)
  assert.equal(result.handoffs[0].draft, long)
})

test('a reply with a point over the length limit is never posted or cut, and goes to a human with its draft', async () => {
  const wordy = 'w '.repeat(61).trim()
  const long = await run({
    args: { autoPush: true, maxCycles: 1 },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: wordy }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(long.calls.filter(c => c.label.startsWith('replies#')).length, 0, 'nothing is posted')
  assert.ok(long.logs.some(l => /no reply posted to comment 2 \(over length: a point exceeds 60 words.*\) — a human answers it/.test(l)), long.logs.join('\n'))
  const owed = long.result.state.debt.find(([id]) => id === 2)[1]
  assert.deepEqual(owed.repair, { replyId: null, error: 'over length: a point exceeds 60 words or a line 300 characters', draft: wordy })
  assert.equal(owed.attempt, undefined, 'a withheld draft is not an offered body')
  assert.deepEqual(long.result.handoffs, [{ commentId: 2, why: `no reply posted: ${owed.repair.error}`, draft: wordy }])
  const shorter = await run({
    args: { autoPush: true, maxCycles: 2, state: long.result.state },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'short' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(shorter.calls.some(c => c.label.startsWith('replies#')), false, 'a later draft does not go out over the handoff')
  const points = await run({
    args: { autoPush: true, maxCycles: 1 },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: `${'w '.repeat(60).trim()}\n\n- ${'w '.repeat(60).trim()}` }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(manifestOf(points.calls, 'replies#1').length, 1, 'each point at its limit, bullet marker aside: posted')
})

test('a comment handed to a human settles on a reply answered by hand once a verifier finds it answers every point', async () => {
  const owing = { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'w '.repeat(61).trim() }], bots: 'reviewed' }
  const upheldOnce = { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] }
  const long = await run({ args: { autoPush: true, maxCycles: 4, yieldAfterCycle: true }, reviews: owing, challenge: upheldOnce })
  assert.equal(long.result.handoffs.length, 1)
  const byHand = { byHand: (id) => id === 2 && { replyId: 700, body: 'Not a defect: the length is checked by the caller.' } }
  const short = await run({ ...byHand, judge: () => false, args: { autoPush: true, maxCycles: 4, yieldAfterCycle: true, state: long.result.state },
    reviews: owing, challenge: upheldOnce })
  assert.ok(short.labels.includes('inspect#2-by-hand'), 'a later cycle looks for an answer by hand')
  assert.ok(short.logs.some(l => /comment 2 is still a human's — reply 700 does not answer every point: stub judgement/.test(l)), short.logs.join('\n'))
  assert.match(debtOf(short.result, 2).judged, /^[0-9a-f]{8}$/)
  assert.equal(short.result.handoffs.length, 1)
  const again = await run({ ...byHand, judge: () => true, args: { autoPush: true, maxCycles: 4, yieldAfterCycle: true, state: short.result.state },
    reviews: owing, challenge: upheldOnce })
  assert.equal(again.labels.some(l => l.startsWith('judge#')), false, 'a body judged short is not judged again until it changes')
  const settled = await run({ byHand: () => ({ replyId: 701, body: 'Refuted: see the caller check at a.c:3.' }), judge: () => true,
    args: { autoPush: true, maxCycles: 4, state: again.result.state }, reviews: owing, challenge: upheldOnce })
  assert.ok(settled.labels.some(l => /^judge#\d+$/.test(l)) && settled.labels.some(l => /^reuse#\d+$/.test(l)))
  assert.ok(settled.logs.some(l => /comment 2 settled on reply 701, answered by hand/.test(l)), settled.logs.join('\n'))
  assert.equal(settled.result.state.debt.some(([id]) => id === 2), false)
  assert.equal(settled.result.handoffs, undefined)
  assert.equal(settled.calls.some(c => c.label.startsWith('replies#')), false, 'nothing posted over the human\'s answer')
  const dry = await run({ byHand: () => ({ replyId: 701, body: 'Refuted: see the caller check at a.c:3.' }), judge: () => true,
    args: { autoPush: false, maxCycles: 4, state: again.result.state }, reviews: owing, challenge: upheldOnce })
  assert.equal(dry.labels.some(l => /^(judge|reuse)#/.test(l)), false, 'a dry run judges and settles nothing')
  assert.ok(dry.logs.some(l => /the replies on comment\(s\) 2 would be judged as answers by hand \(dry run\)/.test(l)), dry.logs.join('\n'))
  assert.equal(debtOf(dry.result, 2).judged, debtOf(again.result, 2).judged, 'nor records a judgement')
})

test('an answer by hand is judged only on a complete harvest, again when its context changes, and leaves the comment to the human', async () => {
  const two = [invalidFinding({ commentId: 2, line: 4 }), invalidFinding({ commentId: 2, line: 5 })]
  const wordy = 'w '.repeat(61).trim()
  const upheldTwo = { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }] }
  const long = await run({ args: { autoPush: true, maxCycles: 6, yieldAfterCycle: true },
    reviews: { findings: two, replies: [{ commentId: 2, body: wordy }], bots: 'reviewed' }, challenge: upheldTwo })
  const hand = { byHand: () => ({ replyId: 700, body: 'Both refuted by hand.' }) }
  const partial = await run({ ...hand, judge: () => true, args: { autoPush: true, maxCycles: 6, yieldAfterCycle: true, state: long.result.state },
    reviews: { findings: [two[0]], replies: [], bots: 'reviewed' }, challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] } })
  assert.equal(partial.labels.some(l => l.startsWith('judge#')), false, 'a point missing from this harvest: no judgement')
  assert.deepEqual(debtOf(partial.result, 2).dismissals.length, 2)
  const short = await run({ ...hand, judge: () => false, args: { autoPush: true, maxCycles: 6, yieldAfterCycle: true, state: partial.result.state },
    reviews: { findings: two, replies: [], bots: 'reviewed' }, challenge: upheldTwo })
  assert.ok(short.labels.some(l => l.startsWith('judge#')))
  const reworded = await run({ ...hand, judge: () => false, args: { autoPush: true, maxCycles: 6, yieldAfterCycle: true, state: short.result.state },
    reviews: { findings: two.map(f => ({ ...f, claim: 'reworded', reason: 'reworded since' })).reverse(), replies: [], bots: 'reviewed' }, challenge: upheldTwo })
  assert.ok(reworded.labels.some(l => /^inspect#\d+-by-hand$/.test(l)))
  assert.equal(reworded.labels.some(l => l.startsWith('judge#')), false, 'the same points worded or ordered otherwise: not judged again')
  const otherComment = await run({ ...hand, judge: () => false, args: { autoPush: true, maxCycles: 6, yieldAfterCycle: true, state: reworded.result.state },
    reviews: { findings: [two[0], { ...two[1], verdict: 'stale' }], replies: [], bots: 'reviewed' }, challenge: upheldTwo })
  assert.ok(otherComment.labels.some(l => l.startsWith('judge#')), 'a point with another disposition is judged again')
  const settled = await run({ ...hand, judge: () => true, args: { autoPush: true, maxCycles: 6, state: otherComment.result.state },
    reviews: { findings: two, replies: [], bots: 'reviewed' }, challenge: upheldTwo })
  assert.ok(settled.logs.some(l => /comment 2 settled on reply 700, answered by hand/.test(l)))
  assert.equal(settled.calls.some(c => c.label.startsWith('replies#')), false)
  assert.equal(settled.result.state.answeredWith.find(([id]) => id === 2)[1].how, 'byHand')
  const later = await run({ args: { autoPush: true, maxCycles: 7, state: settled.result.state }, reviews: { findings: two, replies: [], bots: 'reviewed' }, challenge: upheldTwo })
  assert.ok(later.logs.some(l => /2 finding\(s\) on comment\(s\) 2 left as answered by hand/.test(l)), 'a later launch leaves it to the human')
  assert.equal(later.labels.some(l => /^(judge|inspect|replies|challenge)#/.test(l)), false, later.labels.join(' '))
})

test('a fix note owed and a mixed comment can both settle by hand, and nothing is fixed or posted for them after', async () => {
  let cycle = 0
  const mixedAt = (digest) => [finding({ commentId: 2, line: 4, commentDigest: digest }), invalidFinding({ commentId: 2, line: 5, commentDigest: digest })]
  const edited = await run({
    args: { autoPush: true, maxCycles: 2 },
    reviewsPerCycle: () => { cycle++; return { findings: mixedAt(`d${cycle}`), replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' } },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(debtOf(edited.result, 2).edited, true)
  const settled = await resumeAt(edited.result.state, { byHand: () => ({ replyId: 703, body: 'Fixed one by hand, refuted the other.' }), judge: () => true,
    args: { autoPush: true, maxCycles: 4 },
    reviews: { findings: mixedAt('d2'), replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] } })
  assert.ok(settled.logs.some(l => /comment 2 settled on reply 703, answered by hand/.test(l)), settled.logs.join('\n'))
  const afterSettle = settled.labels.slice(settled.labels.findIndex(l => /^reuse#/.test(l)))
  assert.equal(afterSettle.some(l => /^(fix|commit|resolve|replies)#/.test(l)), false, afterSettle.join(' '))
  assert.equal(settled.result.handoffs, undefined)
})

test('an edited comment settles on an answer by hand to its new points', async () => {
  let cycle = 0
  const edited = await run({
    args: { autoPush: true, maxCycles: 2 },
    posting: () => cycle === 1 ? null : true,
    reviewsPerCycle: () => {
      cycle++
      return { findings: [invalidFinding({ commentId: 2, line: 4, commentDigest: `d${cycle}` })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' }
    },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  const st = edited.result.state
  assert.equal(debtOf(edited.result, 2).edited, true)
  for (const original of [null, undefined]) {
    const blind = await run({ byHand: () => ({ replyId: 702, body: 'Answered the edited point.', original }), judge: () => true,
      args: { autoPush: true, maxCycles: 4, state: st },
      reviews: { findings: [invalidFinding({ commentId: 2, line: 4, commentDigest: debtOf(edited.result, 2).digest })], replies: [], bots: 'reviewed' },
      challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] } })
    assert.equal(blind.labels.some(l => /^(judge|reuse)#/.test(l)), false, `no comment text (${original}): nothing to judge against`)
    assert.equal(blind.result.handoffs.length, 1)
  }
  const settled = await run({ byHand: () => ({ replyId: 702, body: 'Answered the edited point.' }), judge: () => true,
    args: { autoPush: true, maxCycles: 4, state: st },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4, commentDigest: debtOf(edited.result, 2).digest })], replies: [], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] } })
  assert.equal(settled.result.state.debt.some(([id]) => id === 2), false, JSON.stringify(settled.result.handoffs))
})

test('an offered answer the comment outgrew goes to a human, not a reuse or a repost', async () => {
  // The reply failed to settle, then the reviewer edited the comment: the old
  // body may be on the thread and no longer answers what is asked. Same when
  // the verdict flipped and a fix note is now owed where a refutation was
  // offered. Neither the frozen text nor a fresh one goes out.
  let cycle = 0
  const edited = await run({
    args: { autoPush: true, maxCycles: 2 },
    posting: () => cycle === 1 ? null : true,
    reviewsPerCycle: () => {
      cycle++
      return { findings: [invalidFinding({ commentId: 2, line: 4, commentDigest: `d${cycle}` })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' }
    },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(edited.result.pass, false)
  assert.deepEqual(edited.labels.filter(l => l.startsWith('replies#')), ['replies#1', 'replies#1.retry'], 'no repost under the edited comment')
  assert.equal(edited.result.state.debt.find(([id]) => id === 2)[1].edited, true)
  assert.match(rowsOf(summaries(edited.logs)[1])[0][3], /NEEDS A HUMAN: edited after its finding ids/)
  cycle = 0
  const flipped = await run({
    args: { autoPush: true, maxCycles: 2 },
    posting: () => cycle === 1 ? null : true,
    reviewsPerCycle: () => {
      cycle++
      return cycle === 1
        ? { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'no bug exists' }], bots: 'reviewed' }
        : { findings: [finding({ commentId: 2, line: 4, verdict: 'valid', changeReason: 'new evidence: the caller does reach it' })], replies: [], bots: 'reviewed' }
    },
    challenge: confirming(),
  })
  assert.equal(flipped.calls.some(c => c.label.startsWith('resolve#')), false, 'the refutation must not go out as a fix note')
  assert.equal(flipped.result.pass, false)
  assert.match(flipped.result.state.debt.find(([id]) => id === 2)[1].repair.error, /now owes a fixNote/)
})

test('a rejected receipt that names a reply still blocks a repost', async () => {
  let cycle = 0
  const { result, calls } = await run({
    args: { autoPush: true, maxCycles: 1 },
    receipts: (rs) => cycle === 1 ? rs.map(r => ({ ...r, digest: fnv1a('@/tmp/body.txt') })) : rs,
    reviewsPerCycle: () => {
      cycle++
      return { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' }
    },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(calls.filter(c => c.label.startsWith('replies#')).length, 1, 'a second reply went over the untrusted one')
  assert.deepEqual(result.state.debt.find(([id]) => id === 2)[1].repair, { replyId: 502, error: 'receipt for a different body' })
})

test('a ci launch watches CI and never runs the validator', async () => {
  // Chief saw only a check conclude: this launch spends no opus on a harvest.
  const { result, labels, logs } = await run({
    args: { lane: 'ci', yieldAfterCycle: true, maxCycles: 3 },
    ci: { status: 'red', infraRerun: [], realFailures: [{ check: 'build', firstError: 'boom', files: ['src/a.c'], verdict: 'real' }] },
  })
  assert.equal(labels.some(l => l.startsWith('reviews#') || l.startsWith('challenge#') || l.startsWith('replies#')), false)
  assert.ok(labels.includes('ci:collect#1.1') && labels.includes('ci:judge#1'))
  assert.ok(labels.some(l => l.startsWith('fix:')), 'the CI fix still runs')
  assert.equal(result.status, 'paused')
  assert.equal(result.observation.lane, 'ci')
  assert.equal(result.observation.reviews, null, 'nobody looked at reviews')
  assert.equal(result.history[0].reviews, null)
  assert.match(summaries(logs)[0], /reviews not observed this launch/)
})

test('a ci launch cannot declare the PR done, even green', async () => {
  const { result, logs } = await run({
    args: { lane: 'ci', yieldAfterCycle: true, maxCycles: 3 },
    ci: { status: 'green', infraRerun: [], realFailures: [] },
  })
  assert.equal(result.status, 'paused')
  assert.equal(result.pass, false)
  assert.ok(logs.some(l => /ci lane only — reviews not observed, no verdict this launch/.test(l)), logs.join('\n'))
})

test('a reviews launch runs no CI watcher and still fixes and pushes', async () => {
  const { result, labels, logs } = await run({
    args: { lane: 'reviews', yieldAfterCycle: true, maxCycles: 3 },
    reviews: oneValid,
  })
  assert.equal(labels.some(l => l.startsWith('ci#')), false)
  assert.ok(labels.some(l => l.startsWith('push#')), 'the review push went out')
  assert.equal(result.status, 'paused')
  assert.equal(result.observation.lane, 'reviews')
  assert.equal(result.observation.ci, null)
  assert.match(summaries(logs)[0], /CI not observed this launch/)
  assert.equal(result.state.expectedHead, shaFor(1), 'the pushed head is the next expectation')
})

test('a reviews launch with settled bots still declares nothing', async () => {
  const { result, logs } = await run({
    args: { lane: 'reviews', yieldAfterCycle: true, maxCycles: 3 },
    reviews: { findings: [], replies: [], bots: 'reviewed' },
  })
  assert.equal(result.status, 'paused')
  assert.ok(logs.some(l => /reviews lane only — CI not observed, no verdict this launch/.test(l)), logs.join('\n'))
})

test('the lane is not part of the state a launch must match', async () => {
  const first = await run({ args: { lane: 'ci', yieldAfterCycle: true, maxCycles: 3 } })
  assert.equal(first.result.state.config.lane, undefined)
  const second = await run({ args: { lane: 'reviews', yieldAfterCycle: true, maxCycles: 3, state: first.result.state }, reviews: { findings: [], replies: [], bots: 'reviewed' } })
  assert.equal(second.result.state.cyclesUsed, 2, 'the budget is shared across lanes')
  const third = await run({ args: { yieldAfterCycle: true, maxCycles: 3, state: second.result.state }, reviews: { findings: [], replies: [], bots: 'reviewed' } })
  assert.equal(third.result.status, 'complete', `only the both launch completes (got ${third.result.reason})`)
  assert.deepEqual(third.result.history.map(e => e.lane), ['reviews', 'both'], 'a launch carries only the previous launch\'s last cycle')
  assert.equal(third.result.state.last.lane, 'both')
})

test('debt and the pushed head carry across a lane switch', async () => {
  // reviews pushes and owes a refutation, ci keeps both, both pays it and completes.
  const reviews1 = { findings: [finding({ commentId: 1 }), invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' }
  const first = await run({
    args: { lane: 'reviews', yieldAfterCycle: true, maxCycles: 4 },
    reviews: reviews1, dropDoneIds: (label) => label.startsWith('replies#'),
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(first.result.state.expectedHead, shaFor(1))
  assert.deepEqual(first.result.deferred, [2])
  const second = await run({
    args: { lane: 'ci', yieldAfterCycle: true, maxCycles: 4, state: first.result.state },
    preflight: { head: shaFor(1), prHead: shaFor(1) },
  })
  assert.equal(second.result.status, 'paused')
  assert.deepEqual(second.result.deferred, [2], 'the ci launch keeps the reply owed')
  assert.equal(second.result.state.expectedHead, shaFor(1))
  const third = await run({
    args: { yieldAfterCycle: true, maxCycles: 4, state: second.result.state },
    preflight: { head: shaFor(1), prHead: shaFor(1) },
    reviews: { findings: [invalidFinding({ commentId: 2, line: 4 })], replies: [{ commentId: 2, body: 'not so' }], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] },
  })
  assert.equal(third.result.status, 'complete', `the both launch pays and completes (got ${third.result.reason})`)
  assert.equal(third.result.state.debt.length, 0)
})

test('a dry run still runs the fixers it is allowed to run', async () => {
  // Withholding the refutation must not also withhold the local fix work the
  // review lane already promises to leave uncommitted.
  const { calls, result } = await run({
    args: { autoPush: false, maxCycles: 2 },
    reviews: {
      findings: [invalidFinding({ commentId: 1 }), finding({ commentId: 2, verdict: 'valid' })],
      replies: [{ commentId: 1, body: 'no' }],
      bots: 'reviewed',
    },
  })
  assert.ok(calls.some(c => c.label.startsWith('fix:')), 'the dry run skipped the fixer')
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false, 'a dry run must post nothing')
  assert.equal(result.dryRun, true)
})

test('re-overturning a finding does not retire a different dismissal', async () => {
  // B is overturned twice; retiring by count would spend the second on A, never answered.
  let cycle = 0
  const { result } = await run({
    args: { autoPush: true, maxCycles: 5 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) {
        return { findings: [invalidFinding({ commentId: 7 }), invalidFinding({ commentId: 7, line: 9 })],
          replies: [], bots: 'reviewed' }
      }
      if (cycle <= 3) return { findings: [invalidFinding({ commentId: 7, line: 9 })], replies: [], bots: 'reviewed' }
      return { findings: [], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }] }
      : { verdicts: [{ id: 0, upheld: false, reason: 'real' }] },
  })
  assert.equal(result.reason, 'deferred-replies-unresolved',
    'a repeated overturn discharged an unrelated dismissal')
  assert.deepEqual(result.deferred, [7])
})

test('a dry run runs the CI fixer before reporting withheld replies', async () => {
  // The refutation is withheld either way; returning on it before the CI lane
  // would skip fix work the dry run is allowed to do and leave uncommitted.
  const { calls, result } = await run({
    args: { autoPush: false, maxCycles: 2 },
    reviews: { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' },
    ci: {
      status: 'red',
      infraRerun: [],
      realFailures: [{ check: 'build-arm', firstError: 'undefined reference', files: ['src/a.c'], verdict: 'real' }],
    },
  })
  assert.ok(calls.some(c => c.label.startsWith('fix:')), 'the dry run skipped the CI fixer')
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false, 'a dry run must post nothing')
  assert.equal(result.dryRun, true)
})

test('a dismissal survives the fix that moves its line', async () => {
  // A returns at a new line after B's fix: a location key would leave A's first key owed forever.
  let cycle = 0
  const A = (over) => invalidFinding({ commentId: 7, findingId: '7#1', line: 10, ...over })
  const { result } = await run({
    args: { autoPush: true, maxCycles: 5 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) return { findings: [A(), invalidFinding({ commentId: 7, findingId: '7#2', line: 20 })], replies: [], bots: 'reviewed' }
      if (cycle === 2) return { findings: [invalidFinding({ commentId: 7, findingId: '7#2', line: 20 })], replies: [], bots: 'reviewed' }
      if (cycle === 3) return { findings: [A({ line: 11 })], replies: [], bots: 'reviewed' }
      return { findings: [], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }] }
      : { verdicts: [{ id: 0, upheld: false, reason: 'real' }] },
  })
  assert.equal(result.pass, true, `a shifted line stranded a retired dismissal (got ${result.reason})`)
})

test('an edited comment stops the run instead of retiring by a reused id', async () => {
  // Editing a review comment renumbers the positions its ids are built from, so
  // 7#1 can name a different point than the one that debt belongs to.
  let cycle = 0
  const { result, logs } = await run({
    args: { autoPush: true, maxCycles: 4 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) return { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'before' })], replies: [], bots: 'reviewed' }
      if (cycle === 2) return { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'after' })], replies: [], bots: 'reviewed' }
      return { findings: [], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 1
      ? { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] }
      : { verdicts: [{ id: 0, upheld: false, reason: 'real' }] },
  })
  assert.ok(logs.some(l => l.includes('comment 7 was edited')), 'the edit must be reported')
  assert.equal(result.reason, 'deferred-replies-unresolved',
    'a reused id retired a dismissal after the comment was edited')
})

test('an edit is recorded before a refused deferral stops the run, and survives the body reverting', async () => {
  const first = await run({ reviews: oneValid, args: { deferrals: [deferral()], maxCycles: 4, yieldAfterCycle: true, autoPush: false } })
  const edited = await run({ reviews: { findings: [finding({ commentDigest: 'edited' })], replies: [], bots: 'reviewed' },
    args: { maxCycles: 4, yieldAfterCycle: true, state: first.result.state } })
  assert.equal(edited.result.reason, 'deferral-refused')
  assert.deepEqual(edited.result.handoffs, [{ commentId: 1, why: 'edited after its finding ids were carried' }])
  const reverted = await run({ reviews: oneValid, args: { maxCycles: 4, autoPush: true, state: edited.result.state } })
  assert.equal(reverted.labels.some(l => l.startsWith('defer#')), false)
  assert.notEqual(reverted.result.pass, true)
  assert.deepEqual(reverted.result.handoffs, [{ commentId: 1, why: 'edited after its finding ids were carried' }])
})

test('an older state carrying seenSinceEdit loads as an edited comment for a human', async () => {
  const st = adoptionState({ debt: [[7, { dismissals: ['7#1'], notes: [], seenSinceEdit: ['7#1'], digest: 'after' }]], last: null, cyclesUsed: 0 })
  const { result, calls } = await run({ args: { autoPush: true, maxCycles: 4, yieldAfterCycle: true, reviewers: ['coderabbit'], state: st },
    reviews: { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'after' })], replies: [{ commentId: 7, body: 'no' }], bots: 'reviewed' } })
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false)
  assert.deepEqual(result.handoffs, [{ commentId: 7, why: 'edited after its finding ids were carried' }])
  assert.deepEqual(debtOf(result, 7), { dismissals: ['7#1'], notes: [], edited: true, digest: 'after' })
})

test('an edited comment goes to a human: a later refutation does not answer it', async () => {
  // Its ids may name other points now, so neither a retirement nor a reply may rely on them.
  let cycle = 0
  const { calls, result } = await run({
    args: { autoPush: true, maxCycles: 5 },
    reviewsPerCycle: () => {
      cycle++
      if (cycle === 1) return { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'before' })], replies: [], bots: 'reviewed' }
      if (cycle === 2) return { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'after' })], replies: [], bots: 'reviewed' }
      if (cycle === 3) {
        return { findings: [finding({ commentId: 7, findingId: '7#1', commentDigest: 'after', verdict: 'stale' }),
          invalidFinding({ commentId: 7, findingId: '7#2', commentDigest: 'after' })],
        replies: [{ commentId: 7, body: 'still wrong' }], bots: 'reviewed' }
      }
      return { findings: [], replies: [], bots: 'reviewed' }
    },
    challengePerCycle: () => cycle === 2
      ? { verdicts: [{ id: 0, upheld: false, reason: 'real' }] }
      : { verdicts: [{ id: 0, upheld: true, reason: 'stands' }, { id: 1, upheld: true, reason: 'stands' }].slice(0, cycle === 3 ? 2 : 1) },
  })
  assert.equal(calls.some(c => c.label.startsWith('replies#')), false)
  assert.notEqual(result.pass, true)
  assert.deepEqual(result.handoffs, [{ commentId: 7, why: 'edited after its finding ids were carried' }])
})

test('a harvest that reuses a findingId is rejected', async () => {
  // Two dismissals under one id collapse into a single obligation, so answering
  // one would silently answer both.
  const { result } = await run({
    args: { autoPush: true, maxCycles: 1 },
    reviews: {
      findings: [invalidFinding({ commentId: 7, findingId: '7#1', line: 10 }),
        invalidFinding({ commentId: 7, findingId: '7#1', line: 20 })],
      replies: [], bots: 'reviewed',
    },
  })
  assert.equal(result.reason, 'duplicate-finding-ids')
})

test('every agent that acts on GitHub is told which checkout the PR lives in', async () => {
  // The CI watcher and both comment posters infer the repository from their
  // working directory; pointed at another checkout by checkoutDir, they were
  // acting on the launching repository's same-numbered PR.
  const { calls } = await run({
    reviews: {
      findings: [finding({ commentId: 1 }), invalidFinding({ commentId: 2, line: 4 })],
      replies: [{ commentId: 2, body: 'no' }], bots: 'reviewed',
    },
    args: { checkoutDir: '/srv/other/repo' },
  })
  for (const label of ['ci:collect#1.1', 'reviews#1', 'replies#1', 'resolve#1']) {
    const c = calls.find(c => c.label === label)
    assert.ok(c, `${label} ran in this scenario`)
    assert.match(c.prompt, /\/srv\/other\/repo/, `${label} must name the checkout`)
  }
})

// yielding launches: one cycle per launch, the ledger carried in `state`

// A debt-bearing first launch: one dismissal the validator drafted no reply for,
// so the cycle re-arms instead of passing. Yielding turns that re-arm into a pause.
const owing = { findings: [invalidFinding({ commentId: 5 })], replies: [], bots: 'reviewed' }
const upheld = { verdicts: [{ id: 0, upheld: true, reason: 'stands' }] }

test('a yielding launch runs one cycle, pauses with its state, and takes no backoff', async () => {
  const { result, labels, napPoints } = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true },
    reviews: { findings: [], replies: [], bots: 'pending' },
  })
  assert.equal(result.status, 'paused')
  assert.equal(result.reason, 'yielded')
  assert.equal(labels.filter(l => l.startsWith('reviews#')).length, 1, 'exactly one validator dispatch')
  assert.equal(napPoints.length, 0, 'the caller decides how long to wait')
  assert.equal(result.state.cyclesUsed, 1)
  assert.equal(result.state.maxCycles, 3)
  assert.equal(result.state.expectedHead, HEAD)
  assert.equal(result.observation.reviews.bots[0].state, 'absent')
  assert.equal(result.observation.ci.status, 'green')
})

test('state carries the ledger to the next launch, which re-reports what is owed', async () => {
  const first = await run({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true }, reviews: owing, challenge: upheld })
  assert.equal(first.result.status, 'paused')
  assert.deepEqual(first.result.deferred, [5])
  assert.deepEqual(first.result.state.debt, [[5, { dismissals: ['5#1'], notes: [], digest: 'd5' }]])
  const second = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: first.result.state },
    reviews: owing, challenge: upheld,
  })
  const prompt = second.calls.find(c => c.label === 'reviews#2').prompt
  assert.ok(prompt.includes('[{"commentId":5,"findingIds":["5#1"]}]'), 'the validator is told which comment still owes an answer, and for which finding')
  assert.equal(second.result.state.cyclesUsed, 2)
  assert.deepEqual(second.result.deferred, [5], 'an obligation survives the launch boundary')
})

test('a resumed launch still catches an edited comment through the carried digest', async () => {
  const first = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true },
    reviews: { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'before' })], replies: [], bots: 'reviewed' },
    challenge: upheld,
  })
  const second = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: first.result.state },
    reviews: { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'after' })], replies: [], bots: 'reviewed' },
    challenge: { verdicts: [{ id: 0, upheld: false, reason: 'real' }] },
  })
  assert.ok(second.logs.some(l => l.includes('comment 7 was edited')), 'the edit is seen across launches')
  // The same body must not read as an edit, or every resumed comment would.
  const same = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: first.result.state },
    reviews: { findings: [invalidFinding({ commentId: 7, findingId: '7#1', commentDigest: 'before' })], replies: [], bots: 'reviewed' },
    challenge: upheld,
  })
  assert.ok(!same.logs.some(l => l.includes('was edited')), 'an unchanged comment is not an edit')
  assert.deepEqual(first.result.state.debt[0][1].digest, 'before', 'the digest travels in the state')
})

test('a resumed launch refuses a checkout that is another PR, even at the expected head', async () => {
  const first = await run({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true }, reviews: owing, challenge: upheld })
  const second = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: first.result.state },
    preflight: { prRepo: 'someone/tinyusb', prUrl: 'https://github.com/someone/tinyusb/pull/3888', pushUrls: ['git@github.com:someone/tinyusb.git'] },
    reviews: owing,
  })
  assert.equal(second.result.reason, 'state-mismatch')
  assert.deepEqual(second.labels, ['preflight'])
})

test('the observation names the head the cycle reviewed, not the one it pushed', async () => {
  const { result } = await run({ reviews: oneValid, scope: ['src/a.c'], args: { autoPush: true, maxCycles: 1, yieldAfterCycle: true } })
  assert.equal(result.observation.reviewedHead, HEAD)
  assert.equal(result.state.expectedHead, shaFor(1), 'the continuation SHA is the pushed commit')
})

test('a state from a failed preflight resumes once the checkout is fixed', async () => {
  const first = await run({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true }, preflight: { dirty: ['x'] } })
  assert.equal(first.result.reason, 'dirty-start')
  const second = await run({
    args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: JSON.parse(JSON.stringify(first.result.state)) },
    reviews: { findings: [], replies: [], bots: 'pending' },
  })
  assert.equal(second.result.status, 'paused')
  assert.equal(second.result.state.cyclesUsed, 1)
})

test('the cycle budget is cumulative across launches and refuses before any agent runs', async () => {
  const first = await run({ args: { autoPush: true, maxCycles: 1, yieldAfterCycle: true }, reviews: owing, challenge: upheld })
  assert.equal(first.result.status, 'blocked', 'the last cycle of the budget does not pause')
  assert.equal(first.result.reason, 'deferred-replies-unresolved')
  const second = await run({ args: { autoPush: true, maxCycles: 1, yieldAfterCycle: true, state: first.result.state }, reviews: owing })
  assert.equal(second.result.status, 'blocked')
  assert.equal(second.result.reason, 'budget-exhausted')
  assert.deepEqual(second.labels, [], 'not even the preflight')
  assert.deepEqual(second.result.rollup.cycles, [])
})

test('a resumed launch refuses a head the previous launch did not leave', async () => {
  const first = await run({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true }, reviews: owing, challenge: upheld })
  const moved = seal({ ...first.result.state, expectedHead: FOREIGN })
  const second = await run({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: moved }, reviews: owing })
  assert.equal(second.result.reason, 'stale-head')
  assert.equal(second.result.expected, FOREIGN)
  assert.deepEqual(second.labels, ['preflight'])
})

test('state never carries autoPush, and a resumed dry run publishes nothing', async () => {
  const first = await run({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true }, reviews: owing, challenge: upheld })
  assert.ok(!JSON.stringify(first.result.state).includes('autoPush'))
  const second = await run({
    args: { autoPush: false, maxCycles: 3, yieldAfterCycle: true, state: first.result.state },
    reviews: oneValid, scope: ['src/a.c'],
  })
  assert.equal(second.result.dryRun, true)
  assert.ok(!second.labels.some(l => /^(commit|push|replies|resolve)#/.test(l)), 'the earlier grant does not carry over')
})

test('a rig-side pause keeps the reply debt', async () => {
  const { result } = await run({
    args: { autoPush: true, maxCycles: 3 },
    reviews: owing, challenge: upheld,
    ci: { status: 'red', infraRerun: [], realFailures: [{ check: 'hil / pico', firstError: 'board did not enumerate', files: [], verdict: 'rig-side' }] },
  })
  assert.equal(result.reason, 'ci-red-rig-side')
  assert.deepEqual(result.deferred, [5], 'the caller sees what is still owed when it decides to stop')
  assert.deepEqual(result.state.debt.map(([id]) => id), [5])
})

test('N cycles over N launches dispatch the validator N times, no more', async () => {
  const pending = { findings: [], replies: [], bots: 'pending' }
  const one = await run({ args: { autoPush: true, maxCycles: 2 }, reviews: pending })
  const a = await run({ args: { autoPush: true, maxCycles: 2, yieldAfterCycle: true }, reviews: pending })
  const b = await run({ args: { autoPush: true, maxCycles: 2, yieldAfterCycle: true, state: a.result.state }, reviews: pending })
  const validators = (labels) => labels.filter(l => l.startsWith('reviews#')).length
  assert.equal(validators(one.labels), 2)
  assert.equal(validators(a.labels) + validators(b.labels), 2)
  assert.equal(b.result.reason, 'reviews-pending')
  assert.equal(b.result.status, 'blocked')
})

test('a state from a run with other arguments, or of another shape, is refused', async () => {
  const first = await run({ args: { autoPush: true, maxCycles: 3, yieldAfterCycle: true }, reviews: owing, challenge: upheld })
  await assert.rejects(run({ args: { maxCycles: 3, reviewers: ['coderabbit', 'copilot'], state: first.result.state } }),
    /different arguments/)
  await assert.rejects(run({ args: { maxCycles: 3, state: { version: 0 } } }), /not a pr-babysit state/)
})

// adopting an audited local chain without resetting the carried ledger

test('adoption publishes before cycle watchers and reviews the adopted head', async () => {
  const state = adoptionState()
  const { result, labels, calls } = await run({
    args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD },
  })
  assert.deepEqual(labels.slice(0, 3), ['preflight', 'adopt:audit', 'adopt:push'])
  assert.ok(labels.indexOf('adopt:push') < labels.indexOf('ci:collect#2.1'), 'publication is confirmed before either watcher')
  assert.ok(labels.indexOf('adopt:push') < labels.indexOf('reviews#2'))
  assert.equal(labels.includes('adopt:readback'), false, 'the push receipt carries the read-back')
  assert.equal(result.history[1].cycle, 2, 'adoption occupies the next carried cycle')
  assert.equal(result.history[1].head, ADOPT)
  assert.equal(result.state.cyclesUsed, 2)
  assert.equal(result.state.expectedHead, ADOPT)
  assert.equal(result.observation.reviewedHead, ADOPT)
  assert.equal(result.history[1].adoption.publication, 'pushed')
  assert.deepEqual(result.history[1].adoption.commits, [ADOPT])
  assert.deepEqual(result.history[1].adoption.paths, ['src/adopted.c'])
  assert.deepEqual(result.observation.actions.adoption, result.history[1].adoption)
  assert.equal(typeof result.history[1].adoption.detail, 'string')
  assert.equal(JSON.stringify(result.state).includes('adoptHead'), false, 'the launch argument is not persisted')
  const audit = calls.find(c => c.label === 'adopt:audit')
  assert.equal(audit.phase, 'Triage')
  assert.deepEqual(audit.schema.required, ['from', 'to', 'commits', 'paths', 'refusal'])
  assert.ok(audit.prompt.includes(`commits.py chain ${HEAD} ${ADOPT}\``), audit.prompt)
  const push = calls.find(c => c.label === 'adopt:push')
  assert.equal(push.phase, 'Push')
  assert.ok(push.prompt.includes(`push.py --remote 'origin' --branch 'claude/foo' --sha ${ADOPT} --push-url 'git@github.com:hathach/tinyusb.git' --pr 3888\``), push.prompt)
})

test('a lost adoption push receipt gets one fresh agent, which reads the push back', async () => {
  const state = adoptionState()
  const { result, labels } = await run({
    args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD },
    adoptPush: (label) => label === 'adopt:push' ? null : receiptOf(ADOPT, ADOPT, 'Everything up-to-date'),
  })
  assert.deepEqual(labels.slice(0, 4), ['preflight', 'adopt:audit', 'adopt:push', 'adopt:push.retry'])
  assert.equal(result.history[1].adoption.publication, 'pushed')
  assert.equal(result.state.pending, null)
})

test('an already-published adopted head needs no push even in a dry-run launch', async () => {
  for (const autoPush of [true, false]) {
    const state = adoptionState()
    const { result, labels } = await run({
      args: adoptionArgs(state, { autoPush }), preflight: { head: ADOPT, prHead: ADOPT },
    })
    assert.ok(result.history[1]?.adoption, 'the adopted head must get a receipt in the next cycle')
    assert.equal(result.history[1].adoption.publication, 'already-published', String(autoPush))
    assert.equal(result.state.expectedHead, ADOPT)
    assert.equal(labels.includes('adopt:push'), false)
    assert.ok(labels.includes('ci:collect#2.1') && labels.includes('reviews#2'), 'the normal cycle still runs')
  }
})

test('an unpublished adoption without autoPush is a zero-cycle dry run', async () => {
  const state = adoptionState({
    debt: [[5, { dismissals: ['5#1'], notes: [], digest: 'd5' }]],
  })
  const before = structuredClone(state)
  const { result, labels } = await run({
    args: adoptionArgs(state, { autoPush: false }), preflight: { head: ADOPT, prHead: HEAD },
  })
  assert.equal(result.status, 'blocked')
  assert.equal(result.reason, 'adopt-needs-push')
  assert.equal(result.dryRun, true)
  assert.deepEqual(labels, ['preflight', 'adopt:audit'])
  assert.equal(result.state.cyclesUsed, before.cyclesUsed)
  assert.equal(result.state.expectedHead, HEAD)
  assert.deepEqual(result.state.last, before.last)
  assert.deepEqual(result.state.debt, before.debt)
  assert.deepEqual(state, before, 'the supplied state itself is not mutated')
})

test('adoption accepts a complete two-commit chain and canonicalizes its receipt paths', async () => {
  const state = adoptionState()
  const { result } = await run({
    args: adoptionArgs(state), preflight: { head: ADOPT, prHead: ADOPT },
    adoptAudit: chainAudit([ADOPT_MID, ADOPT], ['src/./a.c', 'src/a.c', 'src/b.c']),
  })
  assert.ok(result.history[1]?.adoption, 'the complete audited chain must be adopted')
  assert.deepEqual(result.history[1].adoption.commits, [ADOPT_MID, ADOPT])
  assert.deepEqual(result.history[1].adoption.paths, ['src/a.c', 'src/b.c'])
  assert.equal(result.history[1].adoption.publication, 'already-published')
})

test('the audit script\'s refusal, or an audit of another range, is refused', async () => {
  for (const [audit, detail] of [
    [chainAudit([ADOPT], undefined, { refusal: 'commit message carries attribution: Generated by Codex' }),
      'commit message carries attribution: Generated by Codex'],
    [chainAudit([ADOPT], undefined, { from: ADOPT_MID }), `the audit read ${ADOPT_MID}..${ADOPT}, not ${HEAD}..${ADOPT}`],
    [chainAudit([ADOPT_MID]), `the audit read ${HEAD}..${ADOPT_MID}, not ${HEAD}..${ADOPT}`],
  ]) {
    const state = adoptionState()
    const { result, labels } = await run({ args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD }, adoptAudit: audit })
    assert.equal(result.reason, 'adopt-audit-failed')
    assert.equal(result.detail, detail)
    assert.deepEqual(labels, ['preflight', 'adopt:audit'])
    assert.equal(result.state.cyclesUsed, state.cyclesUsed)
    assert.deepEqual(result.state.last, state.last)
  }
})

test('a chain the audit script cannot read back is refused with its error', async () => {
  const { result, labels } = await run({
    args: adoptionArgs(adoptionState()), preflight: { head: ADOPT, prHead: HEAD },
    adoptAudit: { error: 'git rev-list: fatal: bad revision', from: '', to: '', commits: [], paths: [], refusal: '' },
  })
  assert.equal(result.reason, 'adopt-audit-failed')
  assert.equal(result.detail, 'the chain could not be read back: git rev-list: fatal: bad revision')
  assert.deepEqual(labels, ['preflight', 'adopt:audit'])
})

test('adoptHead argument errors throw before any agent runs', async () => {
  const malformed = adoptionState()
  const equal = adoptionState()
  const unpinned = adoptionState({ pin: null })
  for (const [name, args] of [
    ['no state', { adoptHead: ADOPT }],
    ['malformed SHA', adoptionArgs(malformed, { adoptHead: 'not-a-full-sha' })],
    ['candidate equals expected head', adoptionArgs(equal, { adoptHead: HEAD })],
    ['state has no pin', adoptionArgs(unpinned)],
  ]) {
    const trace = []
    await assert.rejects(run({ args, trace }), /adoptHead|adopt head/i, name)
    assert.deepEqual(trace, [], `${name}: argument validation precedes preflight`)
  }
})

test('adoption keeps ordinary preflight refusals ahead of its own checks', async () => {
  const badPush = 'git@evil.example:hathach/tinyusb.git'
  const cases = [
    ['dirty-start', adoptionState(), { head: ADOPT, prHead: FOREIGN, dirty: [' M src/a.c'] }],
    ['wrong-branch', adoptionState(), { head: ADOPT, prHead: FOREIGN, branch: 'main', upstreamBranch: 'main' }],
    ['state-mismatch', adoptionState(), {
      head: ADOPT, prHead: FOREIGN, prRepo: 'someone/tinyusb',
      prUrl: 'https://github.com/someone/tinyusb/pull/3888', pushUrls: ['git@github.com:someone/tinyusb.git'],
    }],
    ['wrong-remote', adoptionState({ pin: { ...STATE_PIN, pushUrls: [badPush] } }), {
      head: ADOPT, prHead: FOREIGN, pushUrls: [badPush], badPushUrl: badPush,
    }],
  ]
  for (const [reason, state, preflight] of cases) {
    const { result, labels } = await run({ args: adoptionArgs(state), preflight })
    assert.equal(result.reason, reason)
    assert.deepEqual(labels, ['preflight'], `${reason}: the audit is later in preflight`)
    assert.equal(result.state.cyclesUsed, state.cyclesUsed)
    assert.deepEqual(result.state.last, state.last)
  }
  for (const preflight of [null]) {
    const state = adoptionState()
    const { result, labels } = await run({ args: adoptionArgs(state), preflight })
    assert.equal(result.reason, 'preflight-died')
    assert.deepEqual(labels, ['preflight', 'preflight.retry'])
  }
  const state = adoptionState()
  const thrown = await run({ args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD }, throwOn: 'preflight' })
  assert.equal(thrown.result.reason, 'preflight-died')
  assert.deepEqual(thrown.labels, ['preflight', 'preflight.retry'])
})

test('adoption refuses a local head other than the candidate and a remote head outside the chain', async () => {
  const mismatch = adoptionState()
  const local = await run({
    args: adoptionArgs(mismatch), preflight: { head: HEAD, prHead: HEAD },
  })
  assert.equal(local.result.reason, 'adopt-head-mismatch')
  assert.equal(local.result.head, HEAD)
  assert.equal(local.result.expected, ADOPT)
  assert.deepEqual(local.labels, ['preflight'])

  const unexpected = adoptionState()
  const remote = await run({
    args: adoptionArgs(unexpected), preflight: { head: ADOPT, prHead: FOREIGN },
  })
  assert.equal(remote.result.reason, 'wrong-head')
  assert.equal(remote.result.head, FOREIGN)
  assert.deepEqual(remote.result.expected, [HEAD, ADOPT])
  assert.deepEqual(remote.labels, ['preflight', 'adopt:audit'], 'only the audit can say which commits the chain holds')
})

test('a PR head pushed outside the workflow inside the audited chain is adopted up to the candidate', async () => {
  // #3988: 9cf0097 was pushed by hand after a launch, and the state's expectedHead no longer matched.
  const chain = chainAudit([ADOPT_MID, ADOPT], ['src/mid.c', 'src/adopted.c'])
  const pushed = await run({ args: adoptionArgs(adoptionState(), { autoPush: true }), preflight: { head: ADOPT, prHead: ADOPT_MID }, adoptAudit: chain })
  assert.deepEqual(pushed.labels.slice(0, 3), ['preflight', 'adopt:audit', 'adopt:push'])
  assert.equal(pushed.result.history[1].adoption.publication, 'pushed')
  assert.equal(pushed.result.state.expectedHead, ADOPT)
  const dry = await run({ args: adoptionArgs(adoptionState(), { autoPush: false }), preflight: { head: ADOPT, prHead: ADOPT_MID }, adoptAudit: chain })
  assert.equal(dry.result.reason, 'adopt-needs-push')
  assert.equal(dry.result.state.expectedHead, HEAD, 'a dry run adopts nothing')
  const done = await run({ args: adoptionArgs(adoptionState(), { autoPush: true }), preflight: { head: ADOPT, prHead: ADOPT }, adoptAudit: chain })
  assert.equal(done.labels.includes('adopt:push'), false)
  assert.equal(done.result.history[1].adoption.publication, 'already-published')
})

test('an audit that dies, throws, or omits required evidence is refused without a cycle', async () => {
  for (const [name, adoptAudit] of [
    ['dead', null],
    ['thrown', new Error('audit exploded')],
    ['incomplete', { from: HEAD, to: ADOPT, commits: [ADOPT], paths: ['src/a.c'] }],
  ]) {
    const state = adoptionState()
    const { result, labels } = await run({
      args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD }, adoptAudit,
    })
    assert.equal(result.reason, 'adopt-audit-failed', name)
    assert.equal(typeof result.detail, 'string')
    assert.deepEqual(labels, ['preflight', 'adopt:audit', 'adopt:audit.retry'])
    assert.equal(result.state.cyclesUsed, state.cyclesUsed)
  }
})

test('protected modifications, deletions, and either side of a rename fail adoption', async () => {
  for (const [name, paths] of [
    ['modification', ['protected/config.json']],
    ['deletion', ['protected/deleted.json']],
    ['rename from protected', ['protected/old.json', 'src/new.json']],
    ['rename to protected', ['src/old.json', 'protected/new.json']],
    ['a name with a newline, kept whole', ['protected/\nconfig.json']],
  ]) {
    const state = adoptionState({ protected: '^protected/' })
    const { result, labels } = await run({
      args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD },
      adoptAudit: chainAudit([ADOPT], paths),
    })
    assert.equal(result.reason, 'adopt-audit-failed', name)
    assert.deepEqual(labels, ['preflight', 'adopt:audit'])
  }
})

test('an uncanonicalizable adopted path fails the audit', async () => {
  for (const path of ['../outside.c', '/tmp/absolute.c', ' src/leading.c', 'src/trailing.c ']) {
    const state = adoptionState()
    const { result } = await run({
      args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD },
      adoptAudit: chainAudit([ADOPT], [path]),
    })
    assert.equal(result.reason, 'adopt-audit-failed', JSON.stringify(path))
  }
})

test('an adoption decides an unpublished candidate while the PR still heads the state\'s head', async () => {
  // Live on tinyusb #3988: a push-unknown with no SHA left no way to publish the chain on top.
  for (const pending of [
    { sha: FOREIGN, parent: HEAD, lane: 'review', stage: 'push-failed' },
    { sha: null, parent: HEAD, lane: 'review', stage: 'push-unknown' },
    { sha: ADOPT, parent: HEAD, lane: 'review', stage: 'push-failed' },
    { sha: FOREIGN, parent: HEAD, lane: 'adopt', stage: 'adopt-push-unknown' },
  ]) {
    const state = adoptionState({ pending })
    const { result, labels, logs } = await run({
      args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD },
    })
    assert.deepEqual(labels.slice(0, 3), ['preflight', 'adopt:audit', 'adopt:push'], JSON.stringify(pending))
    assert.ok(logs.some(l => l.includes(`the unpublished candidate (${pending.stage}) is left to this adoption`)), JSON.stringify(pending))
    assert.equal(result.history[1].adoption.publication, 'pushed')
    assert.equal(result.state.pending, null)
    assert.equal(result.state.expectedHead, ADOPT)
  }
})

test('an unpublished candidate blocks any other adoption once the PR has moved, or when this run\'s audit refused it', async () => {
  for (const [pending, prHead] of [
    [{ sha: FOREIGN, parent: HEAD, lane: 'review', stage: 'push-failed' }, ADOPT],
    [{ sha: null, parent: HEAD, lane: 'review', stage: 'push-unknown' }, ADOPT],
    [{ sha: FOREIGN, parent: HEAD, lane: 'adopt', stage: 'adopt-push-unknown' }, ADOPT],
    [{ sha: ADOPT, parent: HEAD, lane: 'review', stage: 'audit-blocked' }, HEAD],
  ]) {
    const state = adoptionState({ pending })
    const { result, labels } = await run({
      args: adoptionArgs(state), preflight: { head: ADOPT, prHead },
    })
    assert.equal(result.reason, 'adopt-pending', JSON.stringify(pending))
    assert.deepEqual(result.pending, pending)
    assert.deepEqual(labels, ['preflight'])
    assert.equal(result.state.cyclesUsed, state.cyclesUsed)
  }
})

test('a successful adoption push with no confirming read-back is unknown', async () => {
  for (const [name, adoptPush, detail] of [
    ['unreadable URL', receiptOf(null, ADOPT, 'pushed'), 'no read-back from git@github.com:hathach/tinyusb.git'],
    ['unreadable PR', receiptOf(ADOPT, null, 'pushed'), 'PR #3888 head unreadable after the push'],
    ['contradictory PR', receiptOf(ADOPT, FOREIGN, 'pushed'), `PR #3888 heads ${FOREIGN.slice(0, 7)} after the push, not ${ADOPT.slice(0, 7)}`],
    ['script error', { error: 'no JSON line', pushed: false, detail: '', heads: [] }, 'no JSON line'],
    ['other destination', { ...receiptOf(ADOPT, ADOPT, 'pushed'), heads: [{ url: 'git@evil.example:x.git', head: ADOPT }] }, 'the receipt names git@evil.example:x.git'],
  ]) {
    const state = adoptionState()
    const { result, labels } = await run({
      args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD }, adoptPush,
    })
    assert.equal(result.reason, 'adopt-push-unknown', name)
    assert.equal(result.detail, detail, name)
    assert.equal(result.history[1].adoption.publication, 'unknown')
    assert.equal(result.state.expectedHead, HEAD)
    assert.equal(result.state.cyclesUsed, 2, 'the publication attempt consumes its cycle')
    assert.deepEqual(result.state.pending,
      { sha: ADOPT, parent: HEAD, lane: 'adopt', stage: 'adopt-push-unknown' })
    assert.deepEqual(result.observation.actions.adoption, result.history[1].adoption)
    assert.equal(labels.some(l => l.startsWith('ci#') || l.startsWith('reviews#')), false)
  }
})

test('rejected or dead adoption pushes preserve the ledger and recover without repushing', async () => {
  for (const [name, adoptPush, publication, reason, stage] of [
    ['rejected', receiptOf(HEAD, HEAD, ' ! [rejected] non-fast-forward', false), 'failed', 'adopt-push-failed', 'adopt-push-failed'],
    ['dead', null, 'unknown', 'adopt-push-unknown', 'adopt-push-unknown'],
    ['thrown', new Error('publisher exploded'), 'unknown', 'adopt-push-unknown', 'adopt-push-unknown'],
  ]) {
    const state = adoptionState({
      debt: [[5, { dismissals: ['5#1'], notes: [], digest: 'd5' }]],
      answeredWith: [[6, { how: 'refutation', digest: 'd6' }]],
    })
    const failed = await run({
      args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD }, adoptPush,
    })
    assert.equal(failed.result.reason, reason, name)
    assert.equal(failed.result.history[1].adoption.publication, publication)
    assert.equal(failed.result.state.expectedHead, HEAD)
    assert.equal(failed.result.state.cyclesUsed, 2)
    assert.deepEqual(failed.result.state.pending, { sha: ADOPT, parent: HEAD, lane: 'adopt', stage })
    assert.deepEqual(failed.result.state.debt, state.debt)
    assert.deepEqual(failed.result.state.answeredWith, state.answeredWith)
    assert.equal(failed.labels.some(l => l.startsWith('ci#') || l.startsWith('reviews#')), false)

    const recovered = await run({
      args: adoptionArgs(failed.result.state), preflight: { head: ADOPT, prHead: ADOPT },
    })
    assert.equal(recovered.result.history.at(-1).adoption.publication, 'already-published')
    assert.equal(recovered.result.state.expectedHead, ADOPT)
    assert.equal(recovered.result.state.pending, null)
    assert.deepEqual(recovered.result.state.debt, state.debt)
    assert.deepEqual(recovered.result.state.answeredWith, state.answeredWith)
    assert.equal(recovered.labels.includes('adopt:push'), false, `${name}: recovery must not repush`)
  }
})

test('old bot timing cannot settle reviewers on a newly adopted head', async () => {
  const reviewers = ['greptile', 'coderabbit']
  const state = adoptionState({
    maxCycles: 2, reviewers, reviewClock: { sha: HEAD, eventAt: null, since: at(-100) },
  })
  const { result } = await run({
    args: adoptionArgs(state), preflight: { head: ADOPT, prHead: ADOPT }, clockOffset: 20,
    reviews: { findings: [], replies: [], bots: [bot('greptile', { state: 'absent', sha: null, evidence: [], reason: 'nothing on head' }), bot('coderabbit')] },
  })
  assert.equal(result.reason, 'reviews-pending')
  assert.deepEqual(result.state.reviewClock, { sha: ADOPT, eventAt: null, since: at(20) },
    'the first observation of Y starts Y\'s own clock')
})

test('an exhausted budget refuses adoption before preflight', async () => {
  const state = adoptionState({ maxCycles: 2, cyclesUsed: 2 })
  const { result, labels } = await run({
    args: adoptionArgs(state), preflight: { head: ADOPT, prHead: HEAD },
  })
  assert.equal(result.reason, 'budget-exhausted')
  assert.deepEqual(labels, [])
  assert.equal(result.state.cyclesUsed, 2)
  assert.deepEqual(result.state.last, state.last)
})

test('a continuation after adoption omits adoptHead and advances normally', async () => {
  const state = adoptionState()
  const adopted = await run({
    args: adoptionArgs(state), preflight: { head: ADOPT, prHead: ADOPT },
    reviews: { findings: [], replies: [], bots: 'pending' },
  })
  assert.equal(adopted.result.reason, 'yielded')
  assert.equal(adopted.result.state.expectedHead, ADOPT)
  const nextArgs = adoptionArgs(adopted.result.state)
  delete nextArgs.adoptHead
  const next = await run({
    args: nextArgs, preflight: { head: ADOPT, prHead: ADOPT },
    reviews: { findings: [], replies: [], bots: 'pending' },
  })
  assert.equal(next.result.state.cyclesUsed, 3)
  assert.equal(next.result.state.expectedHead, ADOPT)
  assert.equal(next.result.observation.actions.adoption, null)
  assert.equal(next.labels.some(l => l.startsWith('adopt:')), false)
  assert.ok(next.labels.includes('ci:collect#3.1') && next.labels.includes('reviews#3'))
})

test('every result carries a status, an observation and the state', async () => {
  for (const [opts, status] of [
    [{}, 'complete'],
    [{ preflight: { dirty: ['x'] } }, 'blocked'],
    [{ reviews: oneValid, scope: ['src/a.c'], args: { autoPush: false } }, 'blocked'],
  ]) {
    const { result } = await run(opts)
    assert.equal(result.status, status, JSON.stringify(opts))
    assert.ok('observation' in result && 'state' in result)
  }
})


// hook-regenerated paths: admitted from the hooks' own evidence, never by the committer

const gen = (base, extra = {}) => ({
  after: [...base.after, ' M docs/boards.rst'], modifiedBy: ['gen-doc'],
  snapshotAfter: [...base.snapshotAfter, `644 ${blobOf('docs/boards.rst')} docs/boards.rst`], ...extra,
})
const publishing = { reviews: oneValid, scope: ['src/a.c'], args: { autoPush: true, maxCycles: 1 } }

test('recorded hook output is committed, audited and pushed with the fix', async () => {
  const { result, logs, calls } = await run({ ...publishing, hooks: gen })
  assert.equal(result.history[0].reviewPush.pass, true, 'the widened commit is pushed')
  assert.equal(result.reason, 'maxCycles reached', 'and the cycle re-arms for the fresh CI run, as after any push')
  const commit = calls.find(c => c.label === 'commit#1-review')
  assert.deepEqual(pathLine(commit.prompt), ['src/a.c', 'docs/boards.rst'], 'the committer is handed the widened list')
  const audit = calls.find(c => c.label === 'audit#1-review')
  assert.ok(audit.prompt.includes("'docs/boards.rst'"), 'leftovers are read over the widened list too')
  assert.ok(logs.some(l => l.includes('hook output admitted into the commit: docs/boards.rst')))
  assert.match(rowsOf(summaries(logs)[0])[0][3], /fixed \+ pushed, with regenerated docs\/boards\.rst/)
  assert.deepEqual(result.history[0].reviewPush.generated, ['docs/boards.rst'])
})

test('a path changed by no hook is never admitted', async () => {
  const { result, labels } = await run({ ...publishing, hooks: b => gen(b, { modifiedBy: [] }) })
  assert.equal(result.reason, 'push-failed')
  assert.match(result.history[0].reviewPushFailed.detail, /changed outside the fix scope by no hook: docs\/boards\.rst/)
  assert.ok(!labels.some(l => l.startsWith('commit#')), 'nothing is committed')
})

test('a hook that changes an owned path stops publication', async () => {
  for (const after of [[`644 ${'f'.repeat(40)} src/a.c`], [`755 ${blobOf('src/a.c')} src/a.c`]]) {
    const { result, labels } = await run({ ...publishing, hooks: { modifiedBy: ['fmt'], snapshotAfter: after } })
    assert.match(result.history[0].reviewPushFailed.detail, /a hook changed an owned path after it was verified: src\/a\.c/)
    assert.ok(!labels.some(l => l.startsWith('commit#')), after[0])
  }
})

test('incomplete or inconsistent hook evidence admits nothing and commits nothing', async () => {
  for (const [hooks, expected] of [
    [{ snapshotBefore: [], snapshotAfter: [] }, /evidence is incomplete: no snapshot for src\/a\.c/],
    [b => gen(b, { snapshotAfter: b.snapshotAfter }), /evidence is incomplete: no snapshot for docs\/boards\.rst/],
    [{ ran: false, modifiedBy: ['gen-doc'] }, /evidence is inconsistent/],
  ]) {
    const { result, labels } = await run({ ...publishing, hooks })
    assert.match(result.history[0].reviewPushFailed.detail, expected)
    assert.ok(!labels.some(l => l.startsWith('commit#')), String(expected))
  }
})

test('a commit whose content differs from what the hooks left is never pushed', async () => {
  // The committer edited the regenerated file, or the fix, between the hooks and the commit.
  for (const path of ['docs/boards.rst', 'src/a.c']) {
    const { result, labels } = await run({
      ...publishing, hooks: gen,
      audit: { entries: [`100644 blob ${blobOf('src/a.c')}\tsrc/a.c`, `100644 blob ${blobOf('docs/boards.rst')}\tdocs/boards.rst`]
        .map(e => e.includes(`\t${path}`) ? e.replace(blobOf(path), 'e'.repeat(40)) : e) },
    })
    assert.match(result.history[0].reviewPushFailed.detail, new RegExp(`differs from what the hooks left: ${path.replace('.', '\\.')}`))
    assert.ok(!labels.some(l => l.startsWith('push#')), path)
  }
  const mode = await run({ ...publishing, audit: { entries: [`100755 blob ${blobOf('src/a.c')}\tsrc/a.c`] } })
  assert.match(mode.result.history[0].reviewPushFailed.detail, /differs from what the hooks left: src\/a\.c/)
})

test('a hook that creates a file, or fails, or finds the tree dirty outside the scope, commits nothing', async () => {
  for (const [hooks, expected] of [
    [b => ({ after: [...b.after, '?? build/log'], modifiedBy: ['gen'] }), /created or renamed file\(s\): build\/log/],
    [{ passed: false }, /hooks do not pass/],
    [b => ({ before: [...b.before, ' M other.c'], after: [...b.after, ' M other.c'] }), /outside the fix scope before the hooks ran: other\.c/],
    [null, /hook agent died/],
  ]) {
    const { result, labels } = await run({ ...publishing, hooks })
    assert.match(result.history[0].reviewPushFailed.detail, expected)
    assert.ok(!labels.some(l => l.startsWith('commit#')), String(expected))
  }
})

test('every porcelain status shape names the whole path', async () => {
  for (const line of [' M other.c', 'M  other.c', 'MM other.c', '?? other.c', ' T other.c']) {
    const { result } = await run({ ...publishing, hooks: b => ({ before: [...b.before, line], after: [...b.after, line] }) })
    assert.match(result.history[0].reviewPushFailed.detail, /outside the fix scope before the hooks ran: other\.c/, line)
  }
})

test('protected hook output is refused before the commit', async () => {
  const { result, labels } = await run({ ...publishing, args: { ...publishing.args, protected: '^docs/' }, hooks: gen })
  assert.match(result.history[0].reviewPushFailed.detail, /regenerated a protected path: docs\/boards\.rst/)
  assert.ok(!labels.some(l => l.startsWith('commit#')))
})

test('the audit scope is the fix scope widened by hook output, nothing more', async () => {
  const { calls } = await run({ ...publishing, hooks: gen })
  assert.deepEqual(auditScope(calls.find(c => c.label === 'audit#1-review').prompt), ['src/a.c', 'docs/boards.rst'])
})

test('a commit that landed but was not pushed is a pending candidate in the state, not the next head', async () => {
  const blocked = await run({ ...publishing, audit: { refusal: 'the commit carries unowned path(s): src/z.c' } })
  assert.deepEqual(blocked.result.state.pending, { sha: shaFor(1), parent: HEAD, lane: 'review', stage: 'audit-blocked' })
  assert.equal(blocked.result.state.expectedHead, HEAD)
  const rejected = await run({ ...publishing, push: null })
  assert.equal(rejected.result.state.pending.stage, 'push-failed')
  const unknown = await run({ ...publishing, audit: null, garble: (l, a) => l.startsWith('commit#') ? null : a })
  assert.deepEqual(unknown.result.state.pending, { sha: null, parent: HEAD, lane: 'review', stage: 'push-unknown' })
  const recovered = await run({ ...publishing, garble: (l, a) => l.startsWith('commit#') ? null : a })
  assert.equal(recovered.result.state.pending, null, 'the read-back found the commit, which was audited and pushed')
  assert.equal(recovered.result.state.expectedHead, shaFor(1))
  const unread = await run({ ...publishing, push: { heads: [{ url: PIN.pushUrls[0], head: null }] } })
  assert.deepEqual(unread.result.state.pending, { sha: shaFor(1), parent: HEAD, lane: 'review', stage: 'publication-unknown' })
  assert.equal(unread.result.history[0].reviewPushFailed.detail, `no read-back from ${PIN.pushUrls[0]}`)
  assert.equal(unread.labels.some(l => l.startsWith('resolve#')), false, 'no fix note for an unconfirmed push')
  assert.match(rowsOf(summaries(unread.logs)[0])[0][3], /PUBLICATION UNKNOWN: no read-ba/)
  const elsewhere = await run({ ...publishing, push: { heads: [{ url: PIN.pushUrls[0], head: FOREIGN }] } })
  assert.equal(elsewhere.result.state.pending.stage, 'push-failed')
  assert.equal(elsewhere.result.history[0].reviewPushFailed.detail, `${PIN.pushUrls[0]} heads ${FOREIGN.slice(0, 7)} after the push, not ${shaFor(1).slice(0, 7)}`)
  const pushed = await run(publishing)
  assert.equal(pushed.result.state.pending, null)
  assert.equal(pushed.result.state.expectedHead, shaFor(1))
})


test('an owned path the fix did not change, or deleted, is still complete evidence', async () => {
  // b.c is unchanged and still in ls-tree; a deleted owned path is `absent` on every side.
  const twoFiles = (b) => ({ ...publishing, reviews: { findings: [finding(), finding({ file: b, line: 2 })], replies: [], bots: 'reviewed' } })
  const quiet = await run({
    ...twoFiles('src/b.c'),
    hooks: b => ({ before: [' M src/a.c'], after: [' M src/a.c'] }),
    audit: { entries: lsTreeOf(['src/a.c', 'src/b.c']) },
  })
  assert.equal((quiet.result.history[0].reviewPush || {}).pass, true, JSON.stringify(quiet.result.history[0].reviewPushFailed))
  const deleted = await run({
    ...twoFiles('src/gone.c'),
    hooks: b => ({ before: [' M src/a.c', ' D src/gone.c'], after: [' M src/a.c', ' D src/gone.c'],
      snapshotBefore: [b.snapshotBefore[0], 'absent - src/gone.c'], snapshotAfter: [b.snapshotAfter[0], 'absent - src/gone.c'] }),
    audit: { entries: lsTreeOf(['src/a.c']) },
  })
  assert.equal(deleted.result.history[0].reviewPush.pass, true, JSON.stringify(deleted.result.history[0].reviewPushFailed))
  const resurrected = await run({
    ...twoFiles('src/gone.c'),
    hooks: b => ({ before: [' M src/a.c', ' D src/gone.c'], after: [' M src/a.c', ' D src/gone.c'],
      snapshotBefore: [b.snapshotBefore[0], 'absent - src/gone.c'], snapshotAfter: [b.snapshotAfter[0], 'absent - src/gone.c'] }),
    audit: { entries: lsTreeOf(['src/a.c', 'src/gone.c']) },
  })
  assert.match(resurrected.result.history[0].reviewPushFailed.detail, /differs from what the hooks left: src\/gone\.c/)
})

test('modes are git modes: an executable fix commits as 100755 and a symlink never matches', async () => {
  const exe = await run({
    ...publishing,
    hooks: b => ({ snapshotBefore: [`755 ${blobOf('src/a.c')} src/a.c`], snapshotAfter: [`755 ${blobOf('src/a.c')} src/a.c`] }),
    audit: { entries: [`100755 blob ${blobOf('src/a.c')}\tsrc/a.c`] },
  })
  assert.equal(exe.result.history[0].reviewPush.pass, true)
  const link = await run({ ...publishing, audit: { entries: [`120000 blob ${blobOf('src/a.c')}\tsrc/a.c`] } })
  assert.match(link.result.history[0].reviewPushFailed.detail, /differs from what the hooks left: src\/a\.c/)
})

test('a path with a space or a quote survives status, snapshot, diff-tree and ls-tree unquoted', async () => {
  for (const p of ['src/space name.c', 'src/quote"name.c', "src/it's.c"]) {
    const { result, calls } = await run({ ...publishing, reviews: { findings: [finding({ file: p })], replies: [], bots: 'reviewed' } })
    assert.equal((result.history[0].reviewPush || {}).pass, true, JSON.stringify(result.history[0].reviewPushFailed))
    assert.deepEqual(pathLine(calls.find(c => c.label === 'commit#1-review').prompt), [p], 'the path itself was committed')
    assert.deepEqual(hookQuoted(calls.find(c => c.label === 'hooks#1-review').prompt), [p], 'the hook script gets the path itself')
    const audit = calls.find(c => c.label === 'audit#1-review').prompt
    assert.deepEqual(auditScope(audit), [p], 'the audit script gets the path itself')
  }
})

test('a commit the audit script cannot read back is not pushed', async () => {
  const { result, labels } = await run({ ...publishing, audit: { error: 'HEAD moved from a to b while it was read', parent: '', scope: [], sha: '', entries: [], refusal: '' } })
  assert.match(result.history[0].reviewPushFailed.detail, /^commit failed audit: the commit could not be read back: HEAD moved/)
  assert.equal(result.history[0].reviewPushFailed.committed, true)
  assert.ok(!labels.includes('push#1-review'))
})

test('a hook script that reports no evidence stops publication with its error', async () => {
  const { result, labels, calls } = await run({ ...publishing, hooks: { error: 'pre-commit exited 0 but reported hook fmt failed', ran: false, passed: false } })
  assert.match(calls.find(c => c.label === 'hooks#1-review').prompt,
    /as error, with ran = false, passed = false, modifiedBy = \[\], before = \[\], after = \[\], snapshotBefore = \[\], snapshotAfter = \[\], seal = ''\.$/,
    'the error-case values come from the schema')
  assert.match(result.history[0].reviewPushFailed.detail, /^no hook evidence: pre-commit exited 0/)
  assert.ok(!labels.some(l => l.startsWith('commit#')), 'nothing is committed')
})

test('a file a hook created and staged is refused like an untracked one', async () => {
  const { result, labels } = await run({
    ...publishing,
    hooks: b => ({ after: [...b.after, 'A  new.c'], modifiedBy: ['gen'], snapshotAfter: [...b.snapshotAfter, `644 ${blobOf('new.c')} new.c`] }),
  })
  assert.match(result.history[0].reviewPushFailed.detail, /created or renamed file\(s\): new\.c/)
  assert.ok(!labels.some(l => l.startsWith('commit#')))
})

test('a commit whose audit died is a pending candidate with an unknown SHA', async () => {
  const { result, logs } = await run({ ...publishing, audit: null })
  assert.deepEqual(result.state.pending, { sha: null, parent: HEAD, lane: 'review', stage: 'audit-unknown' })
  assert.match(rowsOf(summaries(logs)[0])[0][3], /fixed \+ committed \(SHA unknown\), NOT PUSHED: audit agent/)
})

// the commit message: the human is the sole author

test('the committer is told the authorship rule', async () => {
  const { calls } = await run({ ...publishing })
  const commit = calls.find(c => c.label === 'commit#1-review')
  assert.match(commit.prompt, /no Co-Authored-By, Claude-Session, Generated-with or the like: the repository's human is the sole author/)
})

// build-regenerated paths: a declared tracked modification, vouched for by the hooks

const CATALOG = 'hw/bsp/family.json'
const regen = { ...publishing, args: { ...publishing.args, generated: '^hw/bsp/family\\.json$' }, recheck: { status: [' M src/a.c', ` M ${CATALOG}`] } }
// The hooks run over the fix and the catalog, so the stub's tree (built from the
// hook prompt) already holds both as plain modifications with snapshots; a case
// that wants the catalog otherwise swaps its status lines out.
const catalogAs = (b, line) => ({
  before: b.before.filter(l => !l.endsWith(CATALOG)).concat(line ? [line] : []),
  after: b.after.filter(l => !l.endsWith(CATALOG)).concat(line ? [line] : []),
})

test('a declared build-regenerated path is checked by the hooks, committed, audited and pushed with the fix', async () => {
  const { result, logs, calls } = await run(regen)
  assert.equal(result.history[0].reviewPush.pass, true)
  assert.deepEqual(hookQuoted(calls.find(c => c.label === 'hooks#1-review').prompt), ['src/a.c', CATALOG], 'the repository hooks run over the catalog too')
  assert.deepEqual(pathLine(calls.find(c => c.label === 'commit#1-review').prompt), ['src/a.c', CATALOG])
  assert.ok(calls.find(c => c.label === 'audit#1-review').prompt.includes(`'${CATALOG}'`))
  assert.ok(logs.some(l => l.includes(`build output admitted into the commit: ${CATALOG}`)))
  assert.deepEqual(result.history[0].reviewPush.generated, [CATALOG])
  assert.match(rowsOf(summaries(logs)[0])[0][3], /fixed \+ pushed, with regenerated hw\/bsp\/family\.json/)
  const both = await run({ ...regen, hooks: gen })
  assert.deepEqual(both.result.history[0].reviewPush.generated, [CATALOG, 'docs/boards.rst'])
})

test('build and hook output joining the batch is checked with it before the commit', async () => {
  for (const [opts, generated] of [[{ ...publishing, hooks: gen }, ['docs/boards.rst']], [regen, [CATALOG]]]) {
    const { result, calls, labels } = await run(opts)
    assert.equal(result.history[0].reviewPush.pass, true)
    const check = calls.find(c => c.label === 'compat#1-review-generated')
    assert.ok(check, 'the widened candidate gets its own check')
    for (const f of ['src/a.c', ...generated]) assert.ok(check.prompt.includes(f), `${f} is in the checked candidate`)
    assert.ok(labels.indexOf('compat#1-review-generated') < labels.indexOf('commit#1-review'))
    const broken = await run({ ...opts, compat: (label) => label.endsWith('-generated') ? { compatible: false, evidence: 'catalog drops a board' } : undefined })
    assert.equal(broken.result.reason, 'push-failed')
    assert.match(broken.result.history[0].reviewPushFailed.detail, /breaks code that relies on it: catalog drops a board/)
    assert.ok(!broken.labels.some(l => l.startsWith('commit#')), 'nothing is committed')
  }
  const { labels } = await run(publishing)
  assert.ok(!labels.some(l => l.endsWith('-generated')), 'no second check when nothing joined the batch')
})

test('the same modification is a stray without the declaration', async () => {
  const { result, labels } = await run({ ...regen, args: publishing.args,
    hooks: b => ({ before: [...b.before, ` M ${CATALOG}`], after: [...b.after, ` M ${CATALOG}`] }) })
  assert.match(result.history[0].reviewPushFailed.detail, /tree changed outside the fix scope before the hooks ran: hw\/bsp\/family\.json/)
  assert.ok(!labels.some(l => l.startsWith('commit#')))
})

test('only a plain unstaged modification is build output', async () => {
  for (const status of [`?? ${CATALOG}`, ` D ${CATALOG}`, `MM ${CATALOG}`]) {
    const { result, labels, calls } = await run({ ...regen, recheck: { status: [' M src/a.c', status] },
      hooks: b => ({ before: [...b.before, status], after: [...b.after, status] }) })
    assert.deepEqual(hookQuoted(calls.find(c => c.label === 'hooks#1-review').prompt), ['src/a.c'], status)
    assert.match(result.history[0].reviewPushFailed.detail, /outside the fix scope before the hooks ran: hw\/bsp\/family\.json/, status)
    assert.ok(!labels.some(l => l.startsWith('commit#')), status)
  }
  // Staged by the recheck: the staged refusal fires first, as for any path.
  const staged = await run({ ...regen, recheck: { status: [' M src/a.c', `M  ${CATALOG}`], staged: [CATALOG] } })
  assert.match(staged.result.history[0].reviewPushFailed.detail, /already staged by somebody else/)
})

test('a candidate that is no longer a plain modification when the hooks run is refused', async () => {
  for (const line of [` D ${CATALOG}`, `M  ${CATALOG}`, null]) {
    const { result, labels } = await run({ ...regen, hooks: b => catalogAs(b, line) })
    assert.match(result.history[0].reviewPushFailed.detail, /no longer a plain modification when the hooks ran: hw\/bsp\/family\.json/, String(line))
    assert.ok(!labels.some(l => l.startsWith('commit#')), String(line))
  }
})

test('a regenerated path needs passing hooks that ran, and must stay put across them', async () => {
  for (const [hooks, expected] of [
    [{ ran: false }, /regenerated path\(s\) have no hook to vouch for them: hw\/bsp\/family\.json/],
    [{ passed: false }, /hooks do not pass/],
    [b => ({ modifiedBy: ['fmt'], snapshotAfter: [b.snapshotAfter[0], `644 ${'f'.repeat(40)} ${CATALOG}`] }), /a hook changed a regenerated path after the build left it: hw\/bsp\/family\.json/],
    [b => ({ snapshotAfter: [b.snapshotAfter[0]] }), /evidence is incomplete: no snapshot for hw\/bsp\/family\.json/],
  ]) {
    const { result, labels } = await run({ ...regen, hooks })
    assert.match(result.history[0].reviewPushFailed.detail, expected, String(expected))
    assert.ok(!labels.some(l => l.startsWith('commit#')), String(expected))
  }
})

test('the audit binds a regenerated path like any other', async () => {
  const edited = await run({ ...regen, audit: { entries: [`100644 blob ${blobOf('src/a.c')}\tsrc/a.c`, `100644 blob ${'e'.repeat(40)}\t${CATALOG}`] } })
  assert.match(edited.result.history[0].reviewPushFailed.detail, /differs from what the hooks left: hw\/bsp\/family\.json/)
  assert.deepEqual(edited.result.history[0].reviewPushFailed.generated, [CATALOG])
})

test('a protected path the build regenerated is refused before the hooks run', async () => {
  const { result, labels } = await run({ ...regen, args: { ...regen.args, protected: '^hw/bsp/' } })
  assert.match(result.history[0].reviewPushFailed.detail, /the build regenerated a protected path: hw\/bsp\/family\.json/)
  assert.ok(!labels.some(l => l.startsWith('hooks#')))
})

test('a restored state records the generated pattern in its config', async () => {
  const first = await run({ ...regen, args: { ...regen.args, yieldAfterCycle: true, maxCycles: 2 } })
  assert.equal(first.result.state.config.generated, new RegExp('^hw/bsp/family\\.json$').source)
  await assert.rejects(run({ ...publishing, args: { ...publishing.args, yieldAfterCycle: true, maxCycles: 2, state: first.result.state } }), /different arguments/)
})

// reviewer state: one record per auto-running bot, settled by the workflow

const absent = (name, over = {}) => bot(name, { state: 'absent', sha: null, evidence: [], reason: 'nothing on head', ...over })
const quiet = (bots) => ({ findings: [], replies: [], bots })
const THREE = { reviewers: ['greptile', 'coderabbit', 'copilot'] }
const headLine = (logs) => summaries(logs).map(s => s.split('\n')[0])

test('the validator contract is per-bot records, not a done flag', async () => {
  const { calls } = await run({})
  const schema = calls.find(c => c.label === 'reviews#1').schema
  assert.deepEqual(schema.required, ['headSha', 'observedAt', 'headEventAt', 'headEventEvidence', 'bots', 'findings', 'replies'])
  assert.deepEqual(schema.properties.bots.items.required, ['bot', 'state', 'kind', 'sha', 'evidence', 'reason'])
  assert.deepEqual(schema.properties.bots.items.properties.state.enum, ['reviewed', 'working', 'queued', 'settled', 'absent', 'unknown'])
})

test('claude is no longer a reviewer the workflow knows', async () => {
  await assert.rejects(run({ args: { reviewers: ['coderabbit', 'claude'] } }), /unknown reviewer\(s\) \["claude"\]/)
})

test('a harvest that leaves an auto-running bot unaccounted for is refused, not read as settled', async () => {
  // The run-7 failure: a bot had no review of the head, and the run said done.
  for (const [bots, why] of [
    [[bot('greptile')], /no record for coderabbit/],
    [[bot('greptile'), bot('greptile'), bot('coderabbit')], /two records for greptile/],
    [[bot('greptile'), bot('coderabbit'), bot('copilot')], /record for copilot, which does not auto-run here/],
    [[bot('greptile', { sha: FOREIGN }), bot('coderabbit')], /greptile record names c0ffee1, not the head/],
    [[bot('greptile', { sha: 'abc' }), bot('coderabbit')], /greptile record names "abc", not the head/],
    [[bot('greptile', { sha: null }), bot('coderabbit')], /greptile reviewed with no SHA/],
    [[bot('greptile', { evidence: [] }), bot('coderabbit')], /greptile reviewed with no evidence/],
    [[bot('greptile', { state: 'settled', kind: 'limited', evidence: ['  '], reason: 'quota' }), bot('coderabbit')], /greptile settled with no evidence/],
    [[bot('greptile', { state: 'settled', kind: null, reason: 'declined' }), bot('coderabbit')], /greptile is settled with kind null/],
    [[bot('greptile', { kind: 'paused' }), bot('coderabbit')], /greptile is reviewed with kind "paused"/],
    [[bot('greptile', { state: 'settled', kind: 'skipped', sha: null, evidence: [], reason: 'declined' }), bot('coderabbit')], /greptile settled with no evidence/],
    [[absent('greptile', { reason: ' ' }), bot('coderabbit')], /greptile absent with no reason/],
  ]) {
    const { result, logs, labels } = await run({ args: THREE, reviews: quiet(bots) })
    assert.equal(result.reason, 'review-report-unusable', `${why}: got ${result.reason}`)
    assert.match(result.detail, why)
    assert.ok(logs.some(l => why.test(l) && l.startsWith('cycle 1: validator report unusable')))
    assert.equal(labels.some(l => l.startsWith('fix:') || l.startsWith('replies#')), false, 'nothing acted on')
  }
})

test('a harvest about another head, or with an unusable clock, is refused', async () => {
  for (const [over, why] of [
    [{ headSha: FOREIGN }, /validator observed head c0ffee1, expected 0f1e2d3/],
    [{ observedAt: 'yesterday' }, /observedAt "yesterday" is not a timestamp/],
    [{ headEventAt: 'NaN' }, /headEventAt "NaN" is not a timestamp/],
    [{ headEventAt: at(5) }, /headEventAt .* is after observedAt/],
  ]) {
    const { result } = await run({ reviews: { ...quiet('reviewed'), ...over } })
    assert.equal(result.reason, 'review-report-unusable', `${why}: got ${result.reason}`)
    assert.match(result.detail, why)
  }
})

test('a bot that reported it could not review settles at once, and the summary says so', async () => {
  // Greptile out of credits, CodeRabbit paused: neither will ever review this head.
  const { result, logs } = await run({
    args: THREE,
    reviews: quiet([
      bot('greptile', { state: 'settled', kind: 'limited', evidence: ['review 5107622579'], reason: 'quota limit reached' }),
      bot('coderabbit', { state: 'settled', kind: 'paused', sha: null, evidence: ['sticky 12'], reason: 'reviews paused after 5 commits' }),
    ]),
  })
  assert.equal(result.pass, true, result.reason)
  assert.equal(headLine(logs)[0],
    'cycle 1 summary — CI green, reviews: greptile settled (limited: quota limit reached) · coderabbit settled (paused: reviews paused after 5 commits)')
})

test('a silent bot blocks until the cap, then the head is declared unreviewed by it', async () => {
  // One minute between validator dispatches: cycle 3 has waited two.
  const early = await run({ args: { ...THREE, maxCycles: 3 }, reviews: quiet([absent('greptile'), bot('coderabbit')]) })
  assert.equal(early.result.reason, 'reviews-pending')
  assert.equal(early.result.head, HEAD)
  assert.deepEqual(early.result.pending, [absent('greptile', { sha: null })])
  assert.equal(headLine(early.logs)[0], 'cycle 1 summary — CI green, reviews: greptile absent (nothing on head, 10m to cap) · coderabbit reviewed 0f1e2d3')
  assert.equal(headLine(early.logs)[2], 'cycle 3 summary — CI green, reviews: greptile absent (nothing on head, 8m to cap) · coderabbit reviewed 0f1e2d3')
  assert.ok(early.logs.some(l => l === 'cycle 1: auto-review still pending (greptile absent (nothing on head, 10m to cap)) — re-arming after a wait'))
  assert.ok(early.logs.some(l => l === 'cycle 3: auto-review still pending (greptile absent (nothing on head, 8m to cap)) — cycle budget exhausted'))
  // Ten minutes between dispatches: cycle 2 observes the cap passed.
  const late = await run({ args: { ...THREE, maxCycles: 3 }, minutesPerCycle: 10, reviews: quiet([absent('greptile', { state: 'queued', reason: 'review requested' }), bot('coderabbit')]) })
  assert.equal(late.result.pass, true, late.result.reason)
  assert.equal(late.result.cycles, 2)
  assert.equal(headLine(late.logs)[1], 'cycle 2 summary — CI green, reviews: greptile queued, wait expired after 10m: head unreviewed (review requested) · coderabbit reviewed 0f1e2d3')
})

test('a bot seen working, or one that could not be read, waits past the cap', async () => {
  for (const state of ['working', 'unknown']) {
    const { result, logs } = await run({
      args: { ...THREE, maxCycles: 2 }, minutesPerCycle: 30,
      reviews: quiet([bot('greptile'), absent('coderabbit', { state, reason: state === 'working' ? 'Review in progress' : 'statuses endpoint 502' })]),
    })
    assert.equal(result.reason, 'reviews-pending', state)
    assert.equal(result.pending[0].state, state)
    assert.match(headLine(logs)[1], new RegExp(`coderabbit ${state} \\((Review in progress|statuses endpoint 502)\\)$`))
  }
})

test('the cap runs from the head event when the validator can date it, else from first sight', async () => {
  // Dated: a push fifteen minutes before the first look has already expired.
  const dated = await run({ args: THREE, reviews: { ...quiet([absent('greptile'), bot('coderabbit')]), headEventAt: at(-15) } })
  assert.equal(dated.result.pass, true, dated.result.reason)
  assert.match(headLine(dated.logs)[0], /greptile absent, wait expired after 15m/)
  // Kept: a later harvest that cannot date the event does not fall back to first sight.
  let n = 0
  const kept = await run({
    args: { ...THREE, maxCycles: 2 }, minutesPerCycle: 5,
    reviewsPerCycle: () => ({ ...quiet([absent('greptile'), bot('coderabbit')]), headEventAt: ++n === 1 ? at(-5) : null }),
  })
  assert.equal(kept.result.pass, true, kept.result.reason)
  assert.match(headLine(kept.logs)[1], /greptile absent, wait expired after 10m/)
  // Restarted: a newer event on the same SHA (reopen, ready) starts the wait over.
  n = 0
  const restarted = await run({
    args: { ...THREE, maxCycles: 2 }, minutesPerCycle: 3,
    reviewsPerCycle: () => ({ ...quiet([absent('greptile'), bot('coderabbit')]), headEventAt: ++n === 1 ? at(-8) : at(2) }),
  })
  assert.equal(restarted.result.reason, 'reviews-pending')
  assert.match(headLine(restarted.logs)[1], /greptile absent \(nothing on head, 9m to cap\)/)
})

test('the clock is keyed by head: this run\'s own push starts a new one, a resume keeps it', async () => {
  let n = 0
  const pushed = await run({
    args: { ...THREE, autoPush: true, maxCycles: 2 }, minutesPerCycle: 20,
    reviewsPerCycle: () => ++n === 1 ? { findings: [finding()], replies: [], bots: [bot('greptile'), bot('coderabbit')] }
      : quiet([absent('greptile'), bot('coderabbit')]),
  })
  assert.equal(pushed.result.reason, 'reviews-pending', 'twenty minutes since first sight of the old head do not count against the new one')
  assert.deepEqual(pushed.result.state.reviewClock, { sha: shaFor(1), eventAt: null, since: at(20) })
  const a = await run({ args: { ...THREE, autoPush: true, maxCycles: 3, yieldAfterCycle: true }, reviews: quiet([absent('greptile'), bot('coderabbit')]) })
  assert.equal(a.result.reason, 'yielded')
  assert.deepEqual(a.result.state.reviewClock, { sha: HEAD, eventAt: null, since: at(0) })
  const b = await run({ args: { ...THREE, autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: a.result.state }, clockOffset: 10, reviews: quiet([absent('greptile'), bot('coderabbit')]) })
  assert.equal(b.result.pass, true, `${b.result.reason}: the wait began in the previous launch`)
  assert.equal(b.result.state.reviewClock.since, at(0))
  await assert.rejects(run({ args: { ...THREE, maxCycles: 3, state: { ...a.result.state, reviewClock: { sha: HEAD, since: 'never' } } } }), /not a pr-babysit state/)
})

test('settled bots forgive neither a valid finding nor a reply owed, and debt outranks a silent bot', async () => {
  const fixed = await run({ args: { ...THREE, autoPush: true }, reviews: { findings: [finding()], replies: [], bots: 'reviewed' } })
  assert.ok(fixed.labels.some(l => l.startsWith('fix:')), 'the valid finding is fixed')
  const owed = await run({
    args: { ...THREE, autoPush: false, maxCycles: 1 },
    reviews: { findings: [invalidFinding({ commentId: 5 })], replies: [], bots: [absent('greptile'), bot('coderabbit')] },
  })
  assert.equal(owed.result.reason, 'deferred-replies-unresolved')
})

test('a last cycle that pushed reports the budget spent, not old-head records on the new head', async () => {
  const { result } = await run({
    args: { ...THREE, autoPush: true, maxCycles: 1 },
    reviews: { findings: [finding()], replies: [], bots: [absent('greptile', { state: 'working', reason: 'Review in progress' }), bot('coderabbit')] },
  })
  assert.equal(result.state.expectedHead, shaFor(1))
  assert.equal(result.reason, 'maxCycles reached')
  assert.equal(result.pending, undefined)
})

test('a push in a yielding launch leaves the old clock; the resume starts a new one on the pushed head', async () => {
  const a = await run({
    args: { ...THREE, autoPush: true, maxCycles: 3, yieldAfterCycle: true },
    reviews: { findings: [finding()], replies: [], bots: [absent('greptile'), bot('coderabbit')] },
  })
  assert.equal(a.result.state.expectedHead, shaFor(1))
  assert.equal(a.result.state.reviewClock.sha, HEAD)
  const b = await run({
    args: { ...THREE, autoPush: true, maxCycles: 3, yieldAfterCycle: true, state: a.result.state }, clockOffset: 20,
    preflight: { head: shaFor(1), prHead: shaFor(1) },
    reviews: quiet([absent('greptile'), bot('coderabbit')]),
  })
  assert.equal(b.result.reason, 'yielded', `${b.result.reason}: twenty minutes on the old head do not count`)
  assert.deepEqual(b.result.state.reviewClock, { sha: shaFor(1), eventAt: null, since: at(20) })
})

test('a returned state is sealed, compact and leads with its digest', async () => {
  const first = await run({ args: { yieldAfterCycle: true, maxCycles: 4 } })
  const { state } = first.result
  assert.equal(Object.keys(first.result)[0], 'stateDigest', 'the digest survives a truncated result')
  assert.equal(first.result.stateDigest, state.digest)
  assert.equal(state.version, 3)
  assert.equal('history' in state, false)
  assert.equal(state.last.cycle, 1)
  assert.deepEqual(Object.keys(state.last).filter(k => !['cycle', 'head', 'lane', 'adoption', 'reviewPushFailed', 'ciPushFailed'].includes(k)), [],
    'reports stay in the result, not the state')
  const second = await run({ args: { yieldAfterCycle: true, maxCycles: 4, state } })
  const third = await run({ args: { yieldAfterCycle: true, maxCycles: 4, state: second.result.state } })
  assert.equal(third.result.state.cyclesUsed, 3)
  assert.ok(JSON.stringify(third.result.state).length < JSON.stringify(state).length + 200, 'launches do not accumulate history')
})

test('a changed or old-format state is refused before anything runs', async () => {
  const { state } = (await run({ args: { yieldAfterCycle: true, maxCycles: 3 } })).result
  const trace = []
  await assert.rejects(run({ trace, args: { yieldAfterCycle: true, maxCycles: 3, state: { ...state, cyclesUsed: 0 } } }), /state digest mismatch/)
  await assert.rejects(run({ trace, args: { yieldAfterCycle: true, maxCycles: 3, state: JSON.stringify(state).replace('"cyclesUsed":1', '"cyclesUsed":0') } }), /state digest mismatch/)
  const { last, digest, ...rest } = state
  await assert.rejects(run({ trace, args: { yieldAfterCycle: true, maxCycles: 3, state: { ...rest, version: 2, history: [last] } } }), /not a pr-babysit state of version 3/)
  assert.deepEqual(trace, [], 'no agent ran')
})

// The state a launch saves after refuting `findings`, and a stateRef to it.
const saved = async (findings) => {
  const first = await run({
    reviews: { findings, replies: findings.map(f => ({ commentId: f.commentId, body: 'not so' })), bots: 'reviewed' },
    args: { maxCycles: 3, yieldAfterCycle: true },
  })
  const { state, stateDigest } = first.result
  return { state, stateRef: { outputFile: '/tmp/tasks/w1.output', digest: stateDigest } }
}
// A decision reason a copying model might "correct".
const REASON = 'SKILL.md:146 at head (144 at eda9a913) — é ✓ "q" \\ end'
const savedForLoad = () => saved([invalidFinding({ reason: REASON })])
const loadWith = (state, stateRef, copy) => run({ args: { yieldAfterCycle: true, maxCycles: 3, stateRef }, load: state, copy })
const asks = (calls) => calls.filter(c => c.label.startsWith('state:load#')).map(c => (c.prompt.match(/ --chunks ([\d,]+)\n/) || [null, 'all'])[1])
const flip = (data) => (data[0] === 'A' ? 'B' : 'A') + data.slice(1)
const once = (change) => (env, label) => label === 'state:load#1' ? change(structuredClone(env)) : env
// The live loader's "correction" (146 -> 144) made in a chunk together with its
// sum: the state stays well formed, so only the seal can tell.
const forged = (env) => {
  const c = env.chunks.find(c => Buffer.from(c.data, 'base64').toString('latin1').includes('SKILL.md:146'))
  if (!c) throw new Error('fixture: no chunk holds SKILL.md:146 whole')
  c.data = Buffer.from(Buffer.from(c.data, 'base64').toString('latin1').replace('SKILL.md:146', 'SKILL.md:144'), 'latin1').toString('base64')
  c.sum = fnv1a(c.data)
  return env
}

test('stateRef loads the saved state as checksummed chunks, exactly', async () => {
  const { state, stateRef } = await savedForLoad()
  assert.ok(transferOf(state).chunks.length > 1, 'the fixture spans several chunks')
  const loaded = await loadWith(state, stateRef)
  assert.deepEqual(loaded.labels.slice(0, 2), ['state:load#1', 'preflight'])
  assert.match(loaded.calls[0].prompt, /state_transfer\.py '\/tmp\/tasks\/w1\.output'\n/)
  assert.equal(loaded.result.state.cyclesUsed, 2, 'the loaded state carries the budget')
  assert.equal(loaded.result.state.decisions[0][1].reason, REASON, 'free text arrives byte for byte')
})

test('a state whose cycles are spent loads, then stops before preflight', async () => {
  const { state, stateRef } = await savedForLoad()
  const stopped = await run({ args: { yieldAfterCycle: true, maxCycles: 1, stateRef }, load: state })
  assert.equal(stopped.result.reason, 'budget-exhausted')
  assert.equal(stopped.labels.includes('preflight'), false)
  assert.ok(stopped.result.state, 'stopped by the loaded, sealed state')
  const full = seal({ ...state, maxCycles: 1 })
  const fullRef = { ...stateRef, digest: full.digest }
  const byState = await run({ args: { yieldAfterCycle: true, stateRef: fullRef }, load: full })
  assert.equal(byState.result.reason, 'budget-exhausted', 'the state\'s own ceiling when the launch names none')
  const raised = await run({ args: { yieldAfterCycle: true, maxCycles: 3, stateRef: fullRef }, load: full })
  assert.ok(raised.labels.includes('preflight'), 'a larger maxCycles loads and runs')
  const failed = await run({ args: { yieldAfterCycle: true, stateRef }, load: state, copy: () => null })
  assert.deepEqual([failed.result.reason, failed.result.stateRef], ['state-transfer-failed', stateRef], 'a failed transfer returns its stateRef')
})

test('a mis-copied, missing or duplicated chunk is asked for again alone', async () => {
  const { state, stateRef } = await savedForLoad()
  const last = transferOf(state).chunks.length - 1
  const cases = [
    [env => { env.chunks[1].data = flip(env.chunks[1].data); return env }, '1'], // sum no longer matches
    [env => { env.chunks.splice(last, 1); return env }, String(last)], // dropped
    [env => { env.chunks.push({ ...env.chunks[0], data: flip(env.chunks[0].data) }); return env }, '0'], // two copies of 0
    [env => { env.chunks[0].data = '!' + env.chunks[0].data.slice(1); env.chunks[0].sum = fnv1a(env.chunks[0].data); return env }, '0'], // not base64
    [env => { env.chunks[1].data = env.chunks[1].data.slice(4); env.chunks[1].sum = fnv1a(env.chunks[1].data); return env }, '1'], // short
    [env => { delete env.chunks[1].sum; return env }, '1'], // copied without its sum
    [env => { delete env.chunks[1].data; return env }, '1'], // copied without its data
  ]
  for (const [change, again] of cases) {
    const got = await loadWith(state, stateRef, once(change))
    assert.deepEqual(asks(got.calls), ['all', again])
    assert.equal(got.labels[2], 'preflight')
    assert.deepEqual(got.result.state.decisions, state.decisions)
  }
  // An index out of range or not asked for settles nothing and costs nothing more.
  const extra = await loadWith(state, stateRef, (env, label) => label === 'state:load#1'
    ? { ...env, chunks: [...env.chunks.slice(1), { i: 999, data: 'AAAA', sum: fnv1a('AAAA') }] }
    : { ...env, chunks: [...env.chunks, { ...transferOf(state).chunks[last] }] })
  assert.deepEqual(asks(extra.calls), ['all', '0'])
  assert.equal(extra.labels[2], 'preflight')
})

test('changed metadata, or a chunk changed with its sum, asks for the whole state again', async () => {
  const { state, stateRef } = await savedForLoad()
  // A length off by one looks consistent alone: the last chunk seems short, so it is
  // asked for again, and the true length in that reply then drops everything.
  const offByOne = await loadWith(state, stateRef, once(env => ({ ...env, length: env.length + 1 })))
  assert.deepEqual(asks(offByOne.calls), ['all', String(transferOf(state).chunks.length - 1), 'all'])
  assert.equal(offByOne.labels[3], 'preflight')
  for (const change of [
    env => ({ ...env, length: 256 * 1024 + 1 }),
    env => ({ ...env, digest: '00000000' }),
    env => ({ ...env, v: 2 }),
    forged,
  ]) {
    const got = await loadWith(state, stateRef, once(change))
    assert.deepEqual(asks(got.calls), ['all', 'all'])
    assert.equal(got.labels[2], 'preflight')
  }
  // A partial reply whose length differs from the retained chunks' drops them too.
  const partial = await loadWith(state, stateRef, (env, label) =>
    label === 'state:load#1' ? { ...env, chunks: env.chunks.slice(1) } : label === 'state:load#2' ? { ...env, length: env.length - 1 } : env)
  assert.deepEqual(asks(partial.calls), ['all', '0', 'all'])
  assert.equal(partial.labels[3], 'preflight')
})

// Nine chunks, as the live #3975 state: more than one reply may carry.
const savedLarge = async () => {
  const large = await saved([...Array(7).keys()].map(k => invalidFinding({ commentId: k + 1, reason: `r${k} `.padEnd(300, 'x') })))
  assert.equal(Math.ceil(transferOf(large.state).length / 512), 9, 'the fixture is nine chunks')
  return large
}

test('a large state arrives in two rounds, four chunks per call, and a mangled batch costs only its bad chunks', async () => {
  const { state, stateRef } = await savedLarge()
  const clean = await loadWith(state, stateRef)
  assert.deepEqual(asks(clean.calls), ['all', '4,5,6,7', '8'])
  assert.equal(clean.labels[3], 'preflight')
  assert.deepEqual(clean.result.state.decisions, state.decisions)
  // The live copy's faults: a chunk spliced, a chunk without its sum, a chunk altered with its sum kept.
  const live = await loadWith(state, stateRef, (env, label) => {
    const e = structuredClone(env)
    if (label === 'state:load#1') e.chunks[3].data = e.chunks[3].data.slice(0, 100) + e.chunks[3].data.slice(-100)
    if (label === 'state:load#2') { delete e.chunks[2].sum; e.chunks[3].data = flip(e.chunks[3].data) }
    return e
  })
  assert.deepEqual(asks(live.calls), ['all', '3,4,5,6', '7,8', '5,6'], 'every missing chunk is asked for at once, four to a call')
  assert.equal(live.labels[4], 'preflight')
  // A reply with mis-copied metadata costs its own chunks, not its siblings'.
  const sibling = await loadWith(state, stateRef, (env, label) => label === 'state:load#2' ? { ...env, digest: '00000000' } : env)
  assert.deepEqual(asks(sibling.calls), ['all', '4,5,6,7', '8', '4,5,6,7'])
  assert.equal(sibling.labels[4], 'preflight')
  // A short copied length sizes a short budget; the true length, once accepted, grows it.
  const short = await loadWith(state, stateRef, (env, label) => {
    const e = structuredClone(env)
    if (label === 'state:load#1') e.length = 4 * 512 - 10
    if (label === 'state:load#4') for (const c of e.chunks) if (c.i !== 7) c.data = flip(c.data)
    if (label === 'state:load#5') for (const c of e.chunks) if (c.i !== 8) c.data = flip(c.data)
    return e
  })
  assert.deepEqual(asks(short.calls), ['all', '3', 'all', '4,5,6,7', '8', '4,5,6'])
  assert.equal(short.labels[6], 'preflight')
  // Progress undone by every other reply stops at the round budget.
  const seesaw = await loadWith(state, stateRef, (env, label) =>
    Number(label.split('#')[1]) % 2 ? env : { ...env, length: env.length - 1 })
  assert.equal(seesaw.result.reason, 'state-transfer-failed')
  assert.equal(seesaw.labels.length, 11, 'six rounds: the first, then one or two calls each')
})

test('the chunks left after the first reply are asked for at once, in parallel calls', async () => {
  const { state, stateRef } = await savedLarge()
  const started = new Set()
  let release
  const both = new Promise(resolve => { release = resolve })
  let timer
  const serial = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('the second round ran its calls one after another')), 2000) })
  const got = await Promise.race([serial, loadWith(state, stateRef, async (env, label) => {
    if (label !== 'state:load#1') {
      started.add(label)
      if (started.size === 2) release()
      await both // each waits for the other: only concurrent calls get past
    }
    return env
  })]).finally(() => clearTimeout(timer))
  assert.deepEqual(asks(got.calls), ['all', '4,5,6,7', '8'])
  assert.equal(got.labels[3], 'preflight')
  assert.deepEqual(got.result.state.decisions, state.decisions)
})

test('a transfer that never checks out blocks before preflight with the stateRef untouched', async () => {
  const { state, stateRef } = await savedForLoad()
  const always = await loadWith(state, stateRef, (env) => forged(structuredClone(env)))
  assert.equal(always.result.reason, 'state-transfer-failed')
  assert.equal(always.result.status, 'blocked')
  assert.deepEqual(always.labels, ['state:load#1', 'state:load#2'], 'two calls without a new chunk end it; nothing past the loader runs')
  assert.deepEqual(always.result.stateRef, stateRef)
  assert.match(always.result.detail, /does not match stateRef\.digest/)
  const dead = await run({ args: { yieldAfterCycle: true, maxCycles: 3, stateRef }, load: new Error('boom') })
  assert.equal(dead.result.reason, 'state-transfer-failed')
  assert.match(dead.result.detail, /loader died/)
  const refused = await loadWith(state, stateRef, () => ({ error: 'cannot read /tmp/tasks/w1.output: No such file or directory' }))
  assert.match(refused.result.detail, /state_transfer\.py: cannot read/)
})

test('stateRef is refused when malformed or given with state', async () => {
  const { state, stateDigest } = (await run({ args: { yieldAfterCycle: true, maxCycles: 3 } })).result
  await assert.rejects(run({ args: { stateRef: { outputFile: '/x', digest: stateDigest }, state } }), /state or stateRef, not both/)
  await assert.rejects(run({ args: { stateRef: { outputFile: 'rel/x', digest: stateDigest } } }), /stateRef must be/)
  await assert.rejects(run({ args: { stateRef: { outputFile: '/x', digest: 'nothex!!' } } }), /stateRef must be/)
  for (const outputFile of ['/tmp/$(printf injected).output', '/tmp/`id`.output', "/tmp/a'b.output", '/tmp/a b.output', '/tmp/../etc/x']) {
    await assert.rejects(run({ args: { stateRef: { outputFile, digest: stateDigest } } }), /stateRef must be/, outputFile)
  }
})

// The SonarCloud notes a state still owes, from its answeredWith.
const sonarOf = (state) => state.answeredWith.filter(([, a]) => a.sonar).map(([id, a]) => [id, a.sonar])
const scanRefuted = { findings: [invalidFinding({ source: 'code-scanning', commentId: 3 })], replies: [{ commentId: 3, body: 'Not injectable: a list argv.' }], bots: 'reviewed' }

test('markSonar marks the SonarCloud issue of a refuted code-scanning comment with the reply', async () => {
  const { calls, result } = await run({ reviews: scanRefuted, args: { autoPush: true, markSonar: true, maxCycles: 1 } })
  const labels = calls.map(c => c.label)
  assert.ok(labels.indexOf('sonar#1') > labels.findIndex(l => l.startsWith('replies#')), labels.join(', '))
  const [item] = payloadOf(calls.find(c => c.label === 'sonar#1').prompt, 'Manifest').items
  assert.deepEqual(item, { commentId: 3, commentDigest: 'd3', how: 'refutation', note: 'Not injectable: a list argv.', digest: fnv1a('Not injectable: a list argv.') })
  assert.match(calls.find(c => c.label === 'sonar#1').prompt, new RegExp(`--pr 3888 --head ${HEAD} --manifest`))
  assert.deepEqual(result.history[0].sonar, [{ commentId: 3, issue: 'K3', outcome: 'marked', detail: 'false positive' }])
  assert.equal('sonarUnmarked' in result, false)
  assert.deepEqual(sonarOf(result.state), [])
})

test('without markSonar nothing goes to SonarCloud and the issue stays owed in the state', async () => {
  const { calls, result } = await run({ reviews: scanRefuted, args: { autoPush: true, maxCycles: 1 } })
  assert.equal(calls.some(c => c.label.startsWith('sonar#')), false)
  assert.deepEqual(result.state.answeredWith, [[3, { how: 'refutation', digest: 'd3', sonar: 'Not injectable: a list argv.' }]])
})

test('a repair naming no reply but an offered body stays held', async () => {
  const reviews = { findings: [invalidFinding()], replies: [{ commentId: 1, body: 'no' }], bots: 'reviewed' }
  const base = (await run({ args: { ...YIELD } })).result.state
  const offered = { dismissals: ['1#1'], notes: [], digest: 'd1', repair: { replyId: null, error: 'offered refutation is stale (comment edited)' },
    attempt: { body: 'no', how: 'refutation', digest: 'd1' } }
  const held = await run({ reviews, args: { ...YIELD, autoPush: true, state: seal({ ...base, debt: [[1, offered]] }) } })
  assert.equal(held.labels.some(l => l.startsWith('replies#')), false, 'an offered body may be on the thread: a human reconciles')
})

test('the note is the body that was posted, and an edited comment is marked with no stale answer', async () => {
  // An earlier offered body is what reply.py posts again: the SonarCloud note is that one.
  const offered = { dismissals: [], notes: [], attempt: { body: 'first wording', how: 'refutation', digest: 'd3' } }
  const state = seal({ ...(await run({ args: { ...YIELD } })).result.state, debt: [[3, offered]] })
  const { calls, result } = await run({
    reviews: { ...scanRefuted, replies: [{ commentId: 3, body: 'second wording' }] },
    args: { ...YIELD, markSonar: true, state },
  })
  assert.match(calls.find(c => c.label.startsWith('replies#')).prompt, /first wording/)
  assert.equal(payloadOf(calls.find(c => c.label.startsWith('sonar#')).prompt, 'Manifest').items[0].note, 'first wording')
  assert.deepEqual(sonarOf(result.state), [])
  // Owed before an edit, then the reviewer edits the comment: nothing is marked with the old answer.
  const owed = (await run({ reviews: scanRefuted, args: { ...YIELD } })).result.state
  const edited = await run({
    reviews: { findings: [invalidFinding({ source: 'code-scanning', commentId: 3, commentDigest: 'd3-edited' })], replies: [], bots: 'reviewed' },
    args: { ...YIELD, markSonar: true, state: owed },
    posting: null,
  })
  assert.equal(edited.calls.some(c => c.label.startsWith('sonar#')), false, edited.calls.map(c => c.label).join(', '))
  assert.deepEqual(sonarOf(edited.result.state), [])
})

test('a later launch with markSonar marks what an earlier one left owed', async () => {
  const first = await run({ reviews: scanRefuted, args: { ...YIELD } })
  const again = await run({ args: { ...YIELD, markSonar: true, state: first.result.state } })
  const sonar = again.calls.find(c => c.label.startsWith('sonar#'))
  assert.ok(sonar, again.calls.map(c => c.label).join(', '))
  assert.equal(payloadOf(sonar.prompt, 'Manifest').items[0].commentId, 3)
  assert.deepEqual(sonarOf(again.result.state), [])
})

test('markSonar needs autoPush', async () => {
  await assert.rejects(run({ reviews: scanRefuted, args: { autoPush: false, markSonar: true } }), /markSonar publishes to SonarCloud: it needs autoPush/)
})

test('a refuted comment from another reviewer owes SonarCloud nothing', async () => {
  const { calls, result } = await run({
    reviews: { findings: [invalidFinding({ commentId: 3 })], replies: [{ commentId: 3, body: 'no' }], bots: 'reviewed' },
    args: { autoPush: true, markSonar: true, maxCycles: 1 },
  })
  assert.equal(calls.some(c => c.label.startsWith('sonar#')), false)
  assert.deepEqual(sonarOf(result.state), [])
})

test('a waiting or failed issue stays owed and is listed unmarked, and a relay error stops asking for the launch', async () => {
  const two = { findings: [invalidFinding({ source: 'code-scanning', commentId: 3 }), invalidFinding({ source: 'code-scanning', commentId: 4 })],
    replies: [{ commentId: 3, body: 'a' }, { commentId: 4, body: 'b' }], bots: 'reviewed' }
  const partial = await run({
    reviews: two, args: { autoPush: true, markSonar: true, maxCycles: 1 },
    sonar: () => ({ results: [{ commentId: 3, issue: 'K3', outcome: 'failed', detail: 'HTTP 403' }, { commentId: 4, issue: 'K4', outcome: 'waiting', detail: 'not analysed' }] }),
  })
  assert.equal(partial.result.pass, true, 'the PR is green')
  assert.deepEqual(sonarOf(partial.result.state), [[3, 'a'], [4, 'b']])
  assert.deepEqual(partial.result.sonarUnmarked, [
    { commentId: 3, how: 'refutation', last: { commentId: 3, issue: 'K3', outcome: 'failed', detail: 'HTTP 403' } },
    { commentId: 4, how: 'refutation', last: { commentId: 4, issue: 'K4', outcome: 'waiting', detail: 'not analysed' } }])
  assert.ok(partial.logs.some(l => /sonar#1: comment 3, issue K3: failed \(HTTP 403\)/.test(l)), partial.logs.join('\n'))
  const dead = await run({ reviews: two, args: { autoPush: true, markSonar: true, maxCycles: 1 }, sonar: () => null })
  assert.deepEqual(sonarOf(dead.result.state).map(([id]) => id), [3, 4])
  let cycle = 0
  const down = await run({
    reviewsPerCycle: () => ++cycle === 1 ? { ...two, bots: 'pending' }
      : { findings: [invalidFinding({ source: 'code-scanning', commentId: 5 })], replies: [{ commentId: 5, body: 'c' }], bots: 'reviewed' },
    args: { autoPush: true, markSonar: true, maxCycles: 2 },
    sonar: () => ({ error: 'SONAR_TOKEN is not set', results: [] }),
  })
  assert.equal(down.calls.filter(c => c.label === 'sonar#1').length, 1, down.calls.map(c => c.label).join(', '))
  assert.equal(down.calls.some(c => c.label.startsWith('sonar#2')), false, 'nothing more is asked this launch')
  assert.ok(down.calls.some(c => c.label === 'replies#2'), 'cycle 2 answered another code-scanning comment')
  assert.deepEqual(sonarOf(down.result.state).map(([id]) => id), [3, 4, 5])
  assert.deepEqual(down.result.sonarUnmarked.map(x => x.last.outcome), ['not asked', 'not asked', 'not asked'])
})

test('a mark re-arms only for SonarCloud\'s own check, not an Actions job named for the scanner', async () => {
  const scannerJob = { status: 'red', infraRerun: [], realFailures: [{ check: 'SonarQube (stm32h743eval)', workflow: 'static_analysis', firstError: 'build failed', files: ['src/a.c'], verdict: 'real' }] }
  const { logs } = await run({ reviews: scanRefuted, ci: scannerJob, args: { autoPush: true, markSonar: true, maxCycles: 1 } })
  assert.equal(logs.some(l => /re-arming to read the SonarCloud check again/.test(l)), false, logs.join('\n'))
})

test('a fixed code-scanning finding still flagged is marked once CI settles, and a red SonarCloud check is read again, not fixed', async () => {
  let cycle = 0
  const sonarRed = { status: 'red', infraRerun: [], realFailures: [{ check: 'SonarCloud', workflow: '', firstError: 'Quality Gate failed', files: [], verdict: 'real' }] }
  const { calls, result, logs } = await run({
    reviewsPerCycle: () => ++cycle === 1
      ? { findings: [finding({ source: 'code-scanning', commentId: 5 })], replies: [], bots: 'reviewed' }
      : { findings: [], replies: [], bots: 'reviewed' },
    ci: sonarRed,
    args: { autoPush: true, markSonar: true, maxCycles: 2 },
  })
  const sonar = calls.filter(c => c.label.startsWith('sonar#'))
  assert.deepEqual(sonar.map(c => c.label), ['sonar#2'], calls.map(c => c.label).join(', '))
  const [item] = payloadOf(sonar[0].prompt, 'Manifest').items
  assert.equal(item.how, 'fixNote')
  assert.match(item.note, /^Fixed in [0-9a-f]{40}\./)
  assert.match(sonar[0].prompt, new RegExp(`--head ${shaFor(1)} `))
  assert.ok(logs.some(l => /cycle 2: 1 SonarCloud issue\(s\) marked false positive — re-arming/.test(l)), logs.join('\n'))
  assert.equal(calls.some(c => c.label.startsWith('fix:ci')), false, 'the SonarCloud check is not a CI fix')
  assert.equal(result.pass, false)
})

test('a SonarCloud gate is never a CI-lane fix: it stops the run on its own reason, rig-side failures beside it', async () => {
  const gate = { check: 'SonarCloud Code Analysis', workflow: '', firstError: 'condition failed: new_security_rating 3 > 1', files: ['tools/code_size.py'], verdict: 'real' }
  const rig = { check: 'hil / pico', workflow: 'Build', firstError: 'board did not enumerate', files: [], verdict: 'rig-side' }
  const { calls, result, logs } = await run({
    reviews: { findings: [], replies: [], bots: 'reviewed' },
    ci: { status: 'red', infraRerun: [], realFailures: [gate, rig] },
    args: { autoPush: true, maxCycles: 2 },
  })
  assert.equal(calls.some(c => c.label.startsWith('fix:')), false, 'nothing is fixed')
  assert.equal(result.reason, 'ci-red-sonar-gate')
  assert.equal(result.cycles, 1, 'another cycle would meet the same gate')
  assert.ok(logs.some(l => /CI red from the SonarCloud gate \(condition failed: new_security_rating 3 > 1\) and 1 rig-side failure\(s\) — its failing condition needs resolving/.test(l)), logs.join('\n'))
  assert.deepEqual(result.rollup.ci, { total: 2, fixed: 0, open: 0, accepted: 0, sonarGate: 1, rigSide: 1, unclassified: 0 })
  assert.ok(logs.some(l => /\| left red: a SonarCloud gate, not a CI-lane fix +\| - /.test(l)), 'the cycle table names it, with no commit')
  // An Actions job running the scanner is an ordinary CI failure.
  const job = await run({ ci: { status: 'red', infraRerun: [], realFailures: [{ ...gate, check: 'SonarQube (stm32h743eval)', workflow: 'static_analysis' }] }, args: { autoPush: false, maxCycles: 1 } })
  assert.ok(job.calls.some(c => c.label.startsWith('fix:')), 'the scanner job is fixed')
  assert.deepEqual(result.observation.ci.realFailures.map(rf => rf.state), ['sonarGate', 'rigSide'], 'each failure carries its state for the caller')
})

test('an unplaced failure beside a SonarCloud gate still stops as unclassified', async () => {
  const gate = { check: 'SonarCloud', workflow: '', firstError: 'Quality Gate failed', files: [], verdict: 'real' }
  const both = await run({ ci: { status: 'red', infraRerun: [], realFailures: [gate, UNPLACED] }, args: { autoPush: true, maxCycles: 2 } })
  assert.equal(both.result.reason, 'ci-red-unclassified')
})

const GATE = { check: 'SonarCloud Code Analysis', workflow: '', firstError: 'condition failed: new_security_rating 3 > 1', files: [] }
const gateRun = (realFailures, extra = {}) => run({ ci: { status: 'red', infraRerun: [], realFailures }, args: { autoPush: true, maxCycles: 2 }, ...extra })

test('a SonarCloud gate alone is read by collect.py and never judged', async () => {
  const { calls, labels, result, logs } = await gateRun([GATE])
  assert.deepEqual(ciLabels(labels), ['ci:collect#1.1', 'ci:collect#1.f'], 'no judge, nothing remembered')
  const f = calls.find(c => c.label === 'ci:collect#1.f').prompt
  assert.match(f, / failures --gate 'https:\/\/sonarcloud\.io\/dashboard\?id=o_r&pullRequest=1' --repo /)
  assert.doesNotMatch(f, /--check/)
  assert.equal(result.reason, 'ci-red-sonar-gate')
  assert.ok(logs.some(l => /SonarCloud gate \(condition failed: new_security_rating 3 > 1\) — its failing condition needs resolving/.test(l)), logs.join('\n'))
  assert.deepEqual(result.observation.ci.realFailures.map(({ check, workflow, job, cell, runId, complete, signature, verdict, state }) => ({ check, workflow, job, cell, runId, complete, signature, verdict, state })),
    [{ check: GATE.check, workflow: '', job: GATE.check, cell: null, runId: null, complete: true, signature: GATE.firstError, verdict: 'real', state: 'sonarGate' }])
})

test('beside a SonarCloud gate the judge gets only the other checks, and a scanner Actions job is judged', async () => {
  const actions = { check: 'build (pico)', workflow: 'Build', firstError: 'src/a.c:1: error: x', files: ['src/a.c'], verdict: 'real' }
  const { calls } = await gateRun([GATE, actions], { args: { autoPush: false, maxCycles: 1 } })
  const judge = calls.find(c => c.label === 'ci:judge#1').prompt
  assert.match(judge, /job\/2/)
  assert.doesNotMatch(judge, /sonarcloud/)
  assert.match(calls.find(c => c.label === 'ci:collect#1.f').prompt, / failures --check '[^']*job\/2' --gate '[^']*sonarcloud[^']*' --repo /)
  assert.ok(calls.some(c => c.label.startsWith('fix:')), 'the Actions failure is fixed')
  const scanner = await gateRun([{ ...GATE, check: 'SonarQube (stm32h743eval)', workflow: 'static_analysis', verdict: 'real' }], { args: { autoPush: false, maxCycles: 1 } })
  assert.ok(scanner.labels.includes('ci:judge#1'), 'a job with a workflow is judged')
})

test('each failing gate condition is its own complete failure', async () => {
  const two = [
    { firstError: 'condition failed: new_security_rating 3 > 1', signature: 'condition failed: new_security_rating 3 > 1', complete: true },
    { firstError: 'condition failed: new_coverage 40 < 80', signature: 'condition failed: new_coverage 40 < 80', complete: true },
  ]
  const { result, logs } = await gateRun([{ ...GATE, gate: { failures: two } }])
  assert.deepEqual(result.observation.ci.realFailures.map(rf => [rf.signature, rf.complete, rf.state]),
    [[two[0].signature, true, 'sonarGate'], [two[1].signature, true, 'sonarGate']])
  assert.ok(logs.some(l => /SonarCloud gate \(condition failed: new_security_rating 3 > 1; condition failed: new_coverage 40 < 80\)/.test(l)), logs.join('\n'))
})

test('a gate with no condition read stops without claiming one, and a mixed pair quotes both checks', async () => {
  const unread = { failures: [{ firstError: 'SonarCloud gate not read: x', signature: 'SonarCloud gate not read: x', complete: false }] }
  const okNow = { failures: [{ firstError: 'quality gate OK now, though its check failed', signature: 'quality gate OK now, though its check failed', complete: false }] }
  for (const gate of [unread, okNow]) {
    const { result, logs } = await gateRun([{ ...GATE, check: 'SonarCloud', link: 'https://github.com/o/r/runs/8', gate }])
    assert.equal(result.reason, 'ci-red-sonar-gate')
    assert.ok(logs.some(l => new RegExp(`SonarCloud gate \\(${gate.failures[0].firstError}\\) — no failing condition was read: inspect the gate on SonarCloud`).test(l)), logs.join('\n'))
    assert.equal(result.observation.ci.realFailures[0].complete, false)
  }
  const { logs } = await gateRun([GATE, { ...GATE, check: 'SonarCloud', link: 'https://github.com/o/r/runs/8', gate: unread }])
  assert.ok(logs.some(l => /SonarCloud gate \(condition failed: new_security_rating 3 > 1; SonarCloud gate not read: x\) — its failing condition needs resolving/.test(l)), logs.join('\n'))
})

test('gate evidence missing or doubled for a requested link re-arms', async () => {
  for (const gates of [(a) => ({ ...a, gates: [] }), (a) => ({ ...a, gates: [a.gates[0], a.gates[0]] })]) {
    const { labels, logs } = await gateRun([GATE], { gates, args: { autoPush: true, maxCycles: 1 } })
    assert.ok(logs.some(l => /SonarCloud gate evidence came back for .*, asked for \["https:\/\/sonarcloud\.io[^"]*"\] — re-arming/.test(l)), logs.join('\n'))
    assert.equal(labels.some(l => l.startsWith('ci:judge')), false)
  }
})
