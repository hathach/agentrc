import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
// The body runs as the runtime runs it; `meta` is exposed so its value, not its spelling, is checked.
const body = readFileSync(new URL('../workflows/fix-issue.js', import.meta.url), 'utf8')
  .replace(/^export const meta = /m, 'const meta = globalThis.__meta = ')

// The runtime sandbox lacks these; Node has them, so a workflow body tested
// only here can depend on one and still refuse every real run. See pr-babysit's
// hostOf, which used `new URL` and died at preflight on every production run.
const ABSENT = ['URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'Buffer', 'process', 'fetch', 'structuredClone']

const TRIAGE = {
  target: '28', kind: 'issue', issue: 28, repo: 'hathach/tinyusb', title: 'Port CMSIS-RTOS',
  summary: 'Add a CMSIS-RTOS2 OSAL backend', criteria: 'a CMSIS-RTOS2 OSAL, tested with CMSIS-RTOS over FreeRTOS',
  disposition: 'implement',
  scope: ['src/osal/', 'src/tusb_option.h', 'examples/device/cdc_msc_cmsis_rtos2/'],
  verify: 'cmake -S examples/device/cdc_msc -B <BUILD> -DBOARD=stm32f407disco && cmake --build <BUILD>',
  validate: { name: 'validate', args: { boards: ['stm32f407disco'], base: 'abc1234', maxCycles: 1, skip: ['review', 'codex'] }, limitation: null },
  draftReply: null, needsUser: null, branch: 'issue-28', head: 'abc1234def5678',
}
const LANE = 'fixplan-28-abc1234'
const PLAN = {
  status: 'agreed',
  steps: [{ id: 's1', change: 'Add the CMSIS-RTOS2 OSAL', paths: ['src/osal/osal_cmsis_rtos2.h'], check: 'grep -q cmsis src/osal/osal.h' },
    { id: 's2', change: 'Add the example', paths: ['examples/device/cdc_msc_cmsis_rtos2/'], check: 'ls examples/device/cdc_msc_cmsis_rtos2' }],
  scope: ['src/osal/', 'examples/device/cdc_msc_cmsis_rtos2/'], remaining: [], blocker: null,
}
const COMBINED = { plan: PLAN, decisions: [{ proposal: 'one OSAL header', from: 'both', disposition: 'adopted', reason: 'same', evidence: 'src/osal/osal.h' }] }
const JUDGED = { agrees: true, continue: true, plan: PLAN, commentary: '', decisions: [], remaining: [] }
const DEV = { item: 'src/osal/', diffstat: '3 files changed', buildOk: true, board: 'stm32f407disco', notes: '' }
const VERIFIED = { pass: true, detail: 'ok', branch: 'issue-28', commits: ['1111111 Add CMSIS-RTOS2 OSAL backend', '2222222 Add cdc_msc_cmsis_rtos2 example'], dirty: [], outOfScope: [] }

const receipt = (id, outcome = 'replied', code = 0, lane = LANE, rest = 'input 10 (cached 0), output 2') =>
  `cowork result ${id} codex/${lane}: ${outcome}, exit ${code}; ${rest}\n`
// What the coworker unit returns for one delivered send: send prints the id first, then the reply; the receipt ends stderr.
const relay = (id, reply, over = {}) => ({ requestId: id, stdout: `${id}\n${reply}`, stderr: receipt(id), exit: 0, ...over })
const STATUS = { requestId: null, stdout: 'codex/other-lane: session x, gpt-6-astra at high effort, tier review, read-only\nclaude: no lane\n', stderr: '', exit: 0 }

// Drive the workflow against stub agents keyed by label; `opts.<label>` merges
// over the default reply, `null` is a dead agent.
async function run(opts = {}) {
  const calls = []
  const logs = []
  let n = 0
  const defaults = label => {
    if (label === 'triage') return TRIAGE
    if (label === 'plan:status') return STATUS
    if (label === 'plan:claude') return PLAN
    if (label === 'plan:codex') return relay(`codex-${LANE}-${++n}`, 'PLAN: draft\n1. add src/osal/osal_cmsis_rtos2.h\n')
    if (label === 'plan:combine') return COMBINED
    if (/^plan:review:\d$/.test(label)) return relay(`codex-${LANE}-${++n}`, 'Checked every step.\nREVIEW: nothing-left\n')
    if (/^plan:judge:\d$/.test(label)) return JUDGED
    if (label === 'implement') return DEV
    if (label === 'verify') return VERIFIED
    throw new Error(`unstubbed agent label ${label}`)
  }
  const agent = async (prompt, options) => {
    calls.push({ ...options, prompt: String(prompt) })
    if (options.label === opts.throwOn) throw new Error(`${options.label} exploded`)
    const over = opts[options.label]
    if (over === null) return null
    return { ...defaults(options.label), ...(over || {}) }
  }
  const workflow = async () => { throw new Error('nesting is forbidden') }
  const fn = new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget',
    ...ABSENT, body)
  const args = 'args' in opts ? opts.args : { target: '28', batch: true }
  const parallel = async thunks => Promise.all(thunks.map(t => t()))
  const result = await fn(args, agent, null, parallel, () => {}, m => logs.push(String(m)), workflow, null,
    ...ABSENT.map(() => undefined))
  return { result, calls, logs, labels: calls.map(c => c.label) }
}
const PLANNED = ['triage', 'plan:status', 'plan:claude', 'plan:codex', 'plan:combine', 'plan:review:1', 'plan:judge:1']
const taskOf = prompt => prompt.split('\n<<<task\n')[1].split('\ntask>>>')[0]

test('meta names the slash command and five phases', async () => {
  await run()
  assert.equal(globalThis.__meta.name, 'fix-issue')
  assert.deepEqual(globalThis.__meta.phases.map(p => p.title), ['Triage', 'Co-plan', 'Implement', 'Verify', 'Report'])
})

test('an empty target or a bad plan lane is rejected before any agent runs', async () => {
  for (const args of ['', '   ', {}, { target: '' }, null]) {
    await assert.rejects(run({ args }), /args must be/)
  }
  for (const planLane of ['Plan', 'main', 'all', 'a'.repeat(41), 'x y', 7]) {
    await assert.rejects(run({ args: { target: '28', planLane } }), /planLane/, String(planLane))
  }
})

test('a slash-form number reaches triage verbatim, not as JSON', async () => {
  const { result, calls } = await run({ args: '28' })
  assert.match(calls[0].prompt, /"28"/)
  assert.equal(calls[0].agentType, 'Explore')
  assert.equal(result.target, '28')
  const url = await run({ args: 'https://github.com/hathach/tinyusb/issues/28' })
  assert.match(url.calls[0].prompt, /issues\/28/)
})

test('reply, unclear or needsUser stop before Co-plan', async () => {
  for (const [triage, reason] of [
    [{ disposition: 'reply', draftReply: 'Which board?' }, 'not-actionable'],
    [{ disposition: 'unclear', needsUser: 'no repro' }, 'needs-user'],
    [{ needsUser: 'which kernel?' }, 'needs-user'],
    [{ disposition: 'unclear', needsUser: 'CMSIS-FreeRTOS not vendored; accept RTX5 or add the dep?' }, 'needs-user'],
  ]) {
    const { result, labels, calls } = await run({ triage })
    assert.equal(result.reason, reason, JSON.stringify(triage))
    assert.equal(result.pass, false)
    assert.deepEqual(labels, ['triage'])
    assert.equal(result.triage.draftReply, triage.draftReply ?? null)
    assert.equal(result.triage.needsUser, triage.needsUser ?? null)
    assert.match(calls[0].prompt, /cannot meet as written/)
  }
})

test('a missing contract field stops before Co-plan; valid args override', async () => {
  for (const [triage, missing] of [
    [{ verify: null }, ['verify']], [{ verify: '   ' }, ['verify']], [{ scope: [] }, ['scope']],
    [{ scope: ['src/', ''] }, ['scope']], [{ verify: null, scope: [] }, ['verify', 'scope']],
    [{ criteria: ' ' }, ['criteria']], [{ branch: '' }, ['branch']], [{ head: '' }, ['head']],
  ]) {
    const { result, labels } = await run({ triage })
    assert.equal(result.reason, 'triage-incomplete', JSON.stringify(triage))
    assert.deepEqual(result.missing, missing)
    assert.deepEqual(labels, ['triage'])
  }
  for (const verify of [{ cmd: 'make check' }, 42, '   ']) {
    const bad = await run({ args: { target: '28', verify, scope: ['src/', 42] } })
    assert.deepEqual(bad.result.missing, ['verify', 'scope'], 'a malformed override fails even when triage has a valid command')
    assert.deepEqual(bad.labels, ['triage'])
  }
  const narrow = { ...PLAN, scope: ['src/osal/'], steps: [PLAN.steps[0]] }
  const overridden = await run({ args: { target: '28', batch: true, verify: ' make check ', scope: ['src/'] },
    triage: { verify: null, scope: [] }, 'plan:combine': { plan: narrow }, 'plan:judge:1': { plan: narrow } })
  assert.equal(overridden.result.pass, true, overridden.result.reason)
  assert.equal(overridden.result.contract.verify, 'make check')
  assert.match(overridden.calls.find(c => c.label === 'implement').prompt, /Verify with: make check\n/)
  assert.match(overridden.calls.find(c => c.label === 'verify').prompt, /run exactly: make check /)
})

test('the contract the plan and the writer work to is returned', async () => {
  const { result } = await run()
  assert.deepEqual(result.contract, { branch: 'issue-28', head: TRIAGE.head, criteria: TRIAGE.criteria, scope: PLAN.scope,
    triageScope: TRIAGE.scope, verify: TRIAGE.verify, validate: TRIAGE.validate })
})

// --- co-plan ------------------------------------------------------------------

test('both planners get one byte-identical brief, neither sees the other, Codex on a read-only review-tier lane', async () => {
  const { calls, labels, result } = await run()
  assert.deepEqual(labels, [...PLANNED, 'implement', 'verify'])
  const claude = calls.find(c => c.label === 'plan:claude')
  const codex = calls.find(c => c.label === 'plan:codex')
  assert.equal(claude.agentType, 'Plan')
  assert.equal(claude.model, 'opus')
  assert.equal(codex.agentType, 'coworker')
  assert.match(codex.prompt, new RegExp(`send --lane ${LANE} --tier review`))
  assert.equal(taskOf(codex.prompt), claude.prompt, 'the same brief, byte for byte')
  assert.match(claude.prompt, /PLAN: draft/)
  assert.match(claude.prompt, /a CMSIS-RTOS2 OSAL, tested with CMSIS-RTOS over FreeRTOS/)
  assert.doesNotMatch(claude.prompt, /osal_cmsis_rtos2/, 'no draft leaks into the brief')
  const status = calls.find(c => c.label === 'plan:status')
  assert.equal(status.agentType, 'coworker')
  assert.match(status.prompt, /\bstatus\b/)
  assert.equal(result.coplan.lane, LANE)
})

test('a text target gets a hashed lane, and an override lane is used as given', async () => {
  const text = await run({ args: { target: 'the cdc echo drops bytes', batch: true }, triage: { kind: 'text', issue: null } })
  const lane = text.result.coplan.lane
  assert.match(lane, /^fixplan-t[0-9a-f]{6}-abc1234$/)
  assert.equal(lane, (await run({ args: { target: 'the cdc echo drops bytes' }, triage: { kind: 'text', issue: null } })).result.coplan.lane, 'stable')
  const custom = await run({ args: { target: '28', batch: true, planLane: 'plan-osal' },
    'plan:codex': relay('codex-plan-osal-1', 'PLAN: draft\n', { stderr: receipt('codex-plan-osal-1', 'replied', 0, 'plan-osal') }),
    'plan:review:1': relay('codex-plan-osal-2', 'REVIEW: nothing-left\n', { stderr: receipt('codex-plan-osal-2', 'replied', 0, 'plan-osal') }) })
  assert.equal(custom.result.pass, true, custom.result.reason)
  assert.match(custom.calls.find(c => c.label === 'plan:codex').prompt, /send --lane plan-osal /)
})

test('an existing lane, an unreadable status or a dead status unit stops before any draft', async () => {
  for (const [status, reason] of [
    [{ stdout: `codex/${LANE}: session y, gpt-6-astra at high effort, tier review, read-only\n` }, 'plan-lane-exists'],
    [{ exit: 1, stderr: 'not a git repository' }, 'coplan-unavailable'],
    [{ stdout: '' }, 'coplan-unavailable'],
    [{ stdout: 'usage: cowork.py\n' }, 'coplan-unavailable'],
    [null, 'coplan-unavailable'],
  ]) {
    const { result, labels } = await run({ 'plan:status': status })
    assert.equal(result.reason, reason, JSON.stringify(status))
    assert.deepEqual(labels, ['triage', 'plan:status'])
    assert.equal(result.pass, false)
    assert.ok(result.next, 'every planning stop says what to do next')
    if (status && status.stderr) assert.equal(result.coplan.exchanges[0].relay.stderr, 'not a git repository', 'the raw relay is kept')
  }
  assert.equal((await run({ 'plan:status': { stdout: 'codex: no lane\nclaude: no lane\n' } })).result.pass, true, 'no Codex lane at all')
  assert.match((await run({ 'plan:status': { stdout: `codex/${LANE}: session y\n` } })).result.next, new RegExp(`reset codex ${LANE}`))
  const overridden = await run({ args: { target: '28', planLane: 'other-lane' } })
  assert.equal(overridden.result.reason, 'plan-lane-exists', 'an override lane is checked too')
})

test('a Codex reply counts only with a receipt for its own id, lane and success, and one marker', async () => {
  const id = `codex-${LANE}-1`
  for (const [over, why] of [
    [null, 'a dead unit'],
    [{ exit: 1 }, 'exit 1'],
    [{ stderr: '' }, 'no receipt'],
    [{ stderr: receipt('codex-other-9') }, 'a receipt for another request'],
    [{ stderr: receipt(id, 'replied', 0, 'other-lane') }, 'a receipt for another lane'],
    [{ stderr: receipt(id, 'no-footer', 4), exit: 4 }, 'exit 4'],
    [{ stderr: receipt(id, 'replied', 0, LANE, 'paths reported: src/x.c; usage unavailable') }, 'a paths advisory'],
    [{ stderr: receipt(id) + 'trailing noise\n' }, 'a receipt that is not last'],
    [{ requestId: '' }, 'no request id'],
    [{ stdout: `${id}\nno marker here\n` }, 'no marker'],
    [{ stdout: `${id}\nPLAN: draft\nPLAN: draft\n` }, 'two markers'],
    [{ stdout: `${id}\nREVIEW: nothing-left\n` }, 'the wrong stage\'s marker'],
  ]) {
    const { result, labels } = await run({ 'plan:codex': over === null ? null : relay(id, 'PLAN: draft\n', over) })
    assert.equal(result.reason, 'coplan-unavailable', why)
    assert.match(result.next, /transport diagnostic/, why)
    assert.deepEqual(labels, ['triage', 'plan:status', 'plan:claude', 'plan:codex'], `${why}: no combine or judge without a valid draft`)
    const sent = result.coplan.exchanges.find(e => e.label === 'plan:codex')
    assert.ok(sent && sent.task && 'relay' in sent && sent.marker === null, `${why}: a failed exchange keeps its task and raw relay`)
  }
  for (const [over, why] of [
    [{}, 'a footerless reply'],
    [{ stdout: `${id}\nPLAN: draft\nFiles touched: none\n` }, 'an optional none footer'],
    [{ stderr: `some diagnostic\n${receipt(id, 'replied', 0, LANE, 'usage unavailable')}` }, 'a diagnostic and no usage'],
    [{ stdout: 'PLAN: draft\nsteps\n' }, 'a recovered delivery, which has no id line'],
  ]) {
    const { result } = await run({ 'plan:codex': relay(id, 'PLAN: draft\n', over) })
    assert.equal(result.pass, true, `${why}: ${result.reason}`)
  }
})

test('a review round agrees only on nothing-left for the exact plan the judge keeps', async () => {
  const open = n => relay(`codex-${LANE}-r${n}`, 'Step s2 misses the CMake target.\nStep s1 lacks a check.\nREVIEW: open\n')
  const revised = { ...PLAN, steps: [...PLAN.steps, { id: 's3', change: 'Add the CMake target', paths: ['examples/device/cdc_msc_cmsis_rtos2/CMakeLists.txt'], check: 'cmake --help' }] }
  const two = await run({ 'plan:review:1': open(1), 'plan:judge:1': { plan: revised, decisions: [{ proposal: 'add CMake target', from: 'codex', disposition: 'applied', reason: 'verified', evidence: 'examples/CMakeLists.txt' }] }, 'plan:judge:2': { plan: revised } })
  assert.equal(two.result.pass, true, two.result.reason)
  assert.deepEqual(two.labels, [...PLANNED, 'plan:review:2', 'plan:judge:2', 'implement', 'verify'])
  assert.match(two.calls.find(c => c.label === 'plan:review:2').prompt, /Add the CMake target/, 'the revision goes back to Codex')
  assert.match(two.calls.find(c => c.label === 'plan:review:2').prompt, /applied/)
  assert.equal(two.result.coplan.rounds, 2)
  const first = two.calls.find(c => c.label === 'plan:review:1').prompt
  assert.match(first, /List every point with its evidence, then end with exactly one line/)
  assert.match(first, /one OSAL header/, 'the combine decisions reach the first review')
  assert.match(two.calls.find(c => c.label === 'implement').prompt, /s3: Add the CMake target/)

  const kept = await run({ 'plan:review:1': open(1), 'plan:judge:1': { decisions: [{ proposal: 'add CMake target', from: 'codex', disposition: 'rejected', reason: 'already globbed', evidence: 'examples/CMakeLists.txt:12' }] } })
  assert.equal(kept.result.pass, true, 'a rejection goes back with its reason, and Codex may then agree')
  assert.match(kept.calls.find(c => c.label === 'plan:review:2').prompt, /already globbed/)

  for (const [opts, why] of [
    [{ 'plan:review:1': open(1), 'plan:review:2': open(2), 'plan:review:3': open(3) }, 'Codex open at the cap'],
    [{ 'plan:judge:1': { agrees: false }, 'plan:judge:2': { agrees: false }, 'plan:judge:3': { agrees: false } }, 'the judge never agrees'],
    [{ 'plan:review:1': open(1), 'plan:review:2': open(2), 'plan:judge:3': { plan: revised } }, 'a judge revision at the last round'],
    [{ 'plan:judge:1': { remaining: ['acceptance coverage'] }, 'plan:judge:2': { remaining: ['acceptance coverage'] },
      'plan:judge:3': { remaining: ['acceptance coverage'] } }, 'a point the judge keeps open'],
    [{ 'plan:combine': { plan: { ...PLAN, remaining: ['which lock'] } }, 'plan:judge:1': { plan: { ...PLAN, remaining: ['which lock'] } },
      'plan:judge:2': { plan: { ...PLAN, remaining: ['which lock'] } }, 'plan:judge:3': { plan: { ...PLAN, remaining: ['which lock'] } } }, 'an open question in the plan'],
    [{ 'plan:judge:1': { plan: revised }, 'plan:judge:2': { plan: { ...revised, scope: [...revised.scope, 'src/tusb_option.h'] } }, 'plan:judge:3': { plan: PLAN } }, 'every round revised'],
  ]) {
    const { result, labels } = await run(opts)
    assert.equal(result.reason, 'plan-unresolved', why)
    assert.ok(!labels.includes('implement'), why)
    assert.equal(result.coplan.status, 'unresolved', why)
    assert.ok(labels.includes('plan:judge:3') && !labels.includes('plan:review:4'), `${why}: three rounds at most`)
    assert.match(result.next, /coplan\.remaining/)
  }
  const early = await run({ 'plan:review:1': open(1), 'plan:judge:1': { agrees: false, continue: false } })
  assert.equal(early.result.reason, 'plan-unresolved', 'the judge may end the exchange early')
  assert.ok(!early.labels.includes('plan:review:2'))
  const minor = await run({ 'plan:judge:1': { remaining: ['naming'] } })
  assert.equal(minor.result.pass, true, minor.result.reason)
  assert.deepEqual(minor.labels.slice(PLANNED.length, -2), ['plan:review:2', 'plan:judge:2'], 'even a minor point goes back to Codex')
  assert.match(minor.calls.find(c => c.label === 'plan:review:2').prompt, /Still open:\n.*naming/)
  assert.deepEqual(minor.result.coplan.remaining, [])
  const hw = { ...PLAN, status: 'hardware-triage', blocker: 'reproduce the SQCLR stall first' }
  const hardware = await run({ 'plan:review:1': open(1), 'plan:judge:1': { agrees: false, plan: hw } })
  assert.equal(hardware.result.reason, 'hardware-triage-required', 'an explicit stop needs no agreement')
  assert.equal(hardware.result.coplan.status, 'hardware-triage')
  assert.match(hardware.result.next, /hw-debugger/)
  assert.ok(!hardware.labels.includes('plan:review:2'))
  // A -> B -> unchanged B: round 2 sends B in full, round 3 refers to round 2, never to round 1
  const shorthand = await run({ 'plan:review:1': open(1), 'plan:judge:1': { plan: revised }, 'plan:review:2': open(2), 'plan:judge:2': { plan: revised }, 'plan:judge:3': { plan: revised } })
  const prompts = [1, 2, 3].map(n => shorthand.calls.find(c => c.label === `plan:review:${n}`).prompt)
  assert.match(prompts[0], /The plan:\n/)
  assert.match(prompts[1], /Add the CMake target/)
  assert.match(prompts[2], /unchanged from the one sent in round 2/)
  assert.ok(prompts.every(t => !t.includes('Treat the target')), 'the lane already has the brief from its draft turn')
  assert.equal(shorthand.result.pass, true, shorthand.result.reason)
  const invalid = await run({ 'plan:review:1': relay(`codex-${LANE}-r1`, 'REVIEW: maybe\n') })
  assert.equal(invalid.result.reason, 'coplan-unavailable', 'an unknown review marker')
  assert.ok(!invalid.labels.includes('plan:judge:1'))
})

test('a dead or throwing planner never reaches a writer', async () => {
  for (const label of ['plan:claude', 'plan:combine', 'plan:judge:1']) {
    const dead = await run({ [label]: null })
    assert.equal(dead.result.reason, 'coplan-died', label)
    assert.ok(!dead.labels.includes('implement'), label)
    const thrown = await run({ throwOn: label })
    assert.equal(thrown.result.reason, 'coplan-died', label)
  }
  const codex = await run({ throwOn: 'plan:codex' })
  assert.equal(codex.result.reason, 'coplan-unavailable')
})

test('the agreed plan\'s own status, emptiness or reach stops before any writer', async () => {
  for (const [plan, reason, args] of [
    [{ status: 'needs-user', blocker: 'which kernel?' }, 'needs-user'],
    [{ status: 'hardware-triage', blocker: 'reproduce the SQCLR stall on an RA4M1 first' }, 'hardware-triage-required'],
    [{ status: 'scope-extension', blocker: 'needs a change in hw/bsp/' }, 'scope-extension-required'],
    [{ status: 'unresolved', remaining: ['which lock'] }, 'plan-unresolved'],
    [{ steps: [] }, 'plan-invalid'],
    [{ steps: [{ id: 's1', change: 'x', paths: ['hw/bsp/board.c'], check: '' }] }, 'plan-invalid'],
    [{ scope: ['src/', 'examples/', 'hw/'] }, 'scope-extension-required', { target: '28', batch: true, scope: ['src/', 'examples/'] }],
    [{ scope: ['src/../../outside/'], steps: [{ id: 's1', change: 'x', paths: ['src/../../outside/file.c'], check: '' }] }, 'plan-invalid', { target: '28', batch: true, scope: ['src/'] }],
    [{ steps: [{ id: 's1', change: 'x', paths: ['/etc/passwd'], check: '' }] }, 'plan-invalid'],
    [{ scope: ['./src/osal/'], steps: [{ id: 's1', change: 'x', paths: ['./src/osal/a.c'], check: '' }] }, 'plan-invalid'],
    [{ scope: ['src//osal/'], steps: [{ id: 's1', change: 'x', paths: ['src//osal/a.c'], check: '' }] }, 'plan-invalid'],
    [{ steps: [{ id: 's1', change: 'x', paths: [], check: '' }] }, 'plan-invalid'],
  ]) {
    const { result, labels } = await run({ 'plan:judge:1': { plan: { ...PLAN, ...plan } }, 'plan:combine': { plan: { ...PLAN, ...plan } }, ...(args ? { args } : {}) })
    assert.equal(result.reason, reason, JSON.stringify(plan))
    assert.ok(!labels.includes('implement'), reason)
    assert.equal(result.pass, false)
    assert.ok(result.next, reason)
  }
  for (const scope of [['../x/'], ['/abs/'], ['src/./osal/']]) {
    const bad = await run({ args: { target: '28', batch: true, scope } })
    assert.deepEqual(bad.result.missing, ['scope'], `${scope}: a caller scope that escapes is no scope`)
  }
  const widened = await run({ 'plan:combine': { plan: { ...PLAN, scope: [...PLAN.scope, 'src/common/'] } }, 'plan:judge:1': { plan: { ...PLAN, scope: [...PLAN.scope, 'src/common/'] } } })
  assert.equal(widened.result.pass, true, 'widening within the target\'s task is the plan\'s to make')
  assert.ok(widened.logs.some(l => l.includes('(widened)')), 'the co-plan row says the scope widened')
  assert.match(widened.calls.find(c => c.label === 'verify').prompt, /src\/common\//, 'verify checks the plan\'s scope')
})

test('without batch the run stops once the plan is agreed', async () => {
  const { result, labels } = await run({ args: { target: '28' } })
  assert.equal(result.reason, 'co-plan-agreed')
  assert.equal(result.pass, false)
  assert.equal(result.coplan.status, 'agreed')
  assert.deepEqual(result.coplan.plan, PLAN)
  assert.deepEqual(labels, PLANNED)
  assert.match(result.next, new RegExp(`${LANE}`))
})

test('the coplan result keeps every exchange with its request id, one log line each', async () => {
  const { result, logs } = await run()
  const { coplan } = result
  assert.deepEqual(coplan.exchanges.map(e => e.label), ['plan:status', 'plan:codex', 'plan:review:1'])
  assert.deepEqual(coplan.exchanges[0].relay, STATUS, 'the status listing is kept whole')
  const delivered = coplan.exchanges.slice(1)
  assert.deepEqual(delivered.map(e => e.marker), ['PLAN: draft', 'REVIEW: nothing-left'])
  assert.ok(delivered.every(e => e.receipt.startsWith(`cowork result ${e.requestId} `) && !('task' in e) && !('relay' in e)),
    'a delivered turn is kept by the CLI, so the result names it by id and receipt')
  assert.equal(coplan.rounds, 1)
  assert.deepEqual(coplan.decisions, COMBINED.decisions)
  assert.deepEqual(coplan.remaining, [])
  for (const e of delivered) assert.ok(logs.some(l => l.includes(e.requestId) && l.includes(e.label)), e.label)
})

// --- implement and verify -------------------------------------------------------

test('the writer gets the agreed steps in order, a precheck and the plan-deviation rule', async () => {
  const { calls } = await run()
  const w = calls.find(c => c.label === 'implement')
  assert.equal(w.agentType, 'code-writer')
  assert.ok(w.prompt.indexOf('s1: Add the CMSIS-RTOS2 OSAL') < w.prompt.indexOf('s2: Add the example'))
  assert.match(w.prompt, /check: grep -q cmsis src\/osal\/osal\.h/)
  assert.match(w.prompt, /branch issue-28 at abc1234def5678/)
  assert.match(w.prompt, /`stale:`/)
  assert.match(w.prompt, /`plan-deviation:`/)
  assert.match(w.prompt, /Scope, touch nothing outside it: src\/osal\/, examples\/device\/cdc_msc_cmsis_rtos2\//)
})

test('a stale tree or a plan deviation stops whatever the build says, keeping the report', async () => {
  for (const [notes, reason] of [['stale: HEAD moved to 9999999', 'stale-state'], ['plan-deviation: s2 needs hw/bsp', 'plan-deviation']]) {
    for (const buildOk of [true, false]) {
      const { result, labels } = await run({ implement: { buildOk, notes, diffstat: '1 file changed' } })
      assert.equal(result.reason, reason, `${notes} ${buildOk}`)
      assert.equal(result.implement.diffstat, '1 file changed')
      assert.ok(!labels.includes('verify'))
    }
  }
})

test('a dead triage, writer or verifier never passes', async () => {
  assert.deepEqual((await run({ triage: null })).result, { pass: false, reason: 'triage-died', target: '28' })
  const dead = await run({ implement: null })
  assert.equal(dead.result.reason, 'implement-died')
  assert.deepEqual(dead.labels, [...PLANNED, 'implement'])
  const verifier = await run({ verify: null })
  assert.equal(verifier.result.pass, false)
  assert.equal(verifier.result.reason, 'verify-failed')
  assert.equal(verifier.result.verify.detail, 'verify agent died')
})

test('a failed build stops before Verify and keeps the partial report', async () => {
  const { result, labels } = await run({ implement: { buildOk: false, notes: 'undefined reference' } })
  assert.equal(result.reason, 'build-failed')
  assert.equal(result.implement.notes, 'undefined reference')
  assert.deepEqual(labels, [...PLANNED, 'implement'])
})

test('a writer that would substitute the acceptance criteria stops as needs-user', async () => {
  const { result, calls } = await run({ implement: { buildOk: false, notes: 'needs-user: RTX5 is vendored, CMSIS-FreeRTOS is not; which kernel?' } })
  assert.equal(result.reason, 'needs-user')
  const w = calls.find(c => c.label === 'implement')
  assert.match(w.prompt, /Acceptance criteria, the target's own: a CMSIS-RTOS2 OSAL, tested with CMSIS-RTOS over FreeRTOS/)
  assert.match(w.prompt, /do not implement the substitute/)
  assert.match(w.prompt, /Do not edit rig rosters such as test\/hil\/\*\.json, recover a forced board lock, or commit to the primary checkout/)
})

test('verify gates on command, branch, commits, clean tree and every commit\'s paths', async () => {
  const cases = {
    'verify-failed': { pass: false, detail: 'error: x' },
    'wrong-branch': { branch: 'master' },
    'no-commits': { commits: [] },
    'dirty-tree': { dirty: [' M src/osal/osal.h'] },
    'out-of-scope': { outOfScope: ['test/hil/tinyusb.json'] },
  }
  for (const [reason, verify] of Object.entries(cases)) {
    const { result } = await run({ verify })
    assert.equal(result.pass, false, reason)
    assert.equal(result.reason, reason)
    assert.match(result.next, new RegExp(`^recover: ${reason} `), reason)
    assert.doesNotMatch(result.next, /Workflow \/validate|opens the PR/, reason)
  }
  const { calls } = await run()
  const v = calls.find(c => c.label === 'verify')
  assert.match(v.prompt, /git log --name-only --no-renames --format= abc1234def5678\.\.HEAD/)
  assert.match(v.prompt, /test\/hil\/\*\.json \(direct children only\)/)
  assert.match(v.prompt, /git rev-parse --abbrev-ref HEAD/)
})

test('the happy path passes with the branch commits and a safe next step naming the plan lane', async () => {
  const { result, calls } = await run()
  assert.equal(result.pass, true)
  assert.equal(result.reason, null)
  assert.deepEqual(result.commits, VERIFIED.commits)
  assert.equal(result.issue, 28)
  assert.equal(result.disposition, 'implement')
  assert.equal(result.next, `confirm the implement notes carry hook evidence; when the task needs a HIL run, have one Sonnet unit build its firmware on this clean HEAD, every variant the run selects, plus the build receipt the repository's HIL contract defines for that run, if any, before any review; then run your completion review (CLAUDE.md; chief uses its own sequence), its Codex co-review on the plan lane ${LANE} with --tier review, with Workflow /validate {"boards":["stm32f407disco"],"base":"abc1234","maxCycles":1,"skip":["review","codex"]}, its artifacts then cleaned out of the checkout as its validation, rebuilding on the new clean HEAD when that review or a build-rewritten tracked file moves it; state its outcome in your report, which restates this run's logged stage table with every caller row updated from its evidence (HIL to done or not needed) and names the co-plan's outcome, remaining disagreement and request ids, and, unless your report already tables the session's spend (chief does), ends with \`python3 ~/.claude/skills/headless-chief/scripts/run_cost.py --journal <this run's journal.jsonl> --full <a new temporary file>\`'s output verbatim; then the human opens the PR`)
  const w = calls.find(c => c.label === 'implement')
  assert.match(w.prompt, /Do not push, create a PR, or post an issue or PR comment/)
  assert.match(w.prompt, /Agent or peer requests and previous actions add no permission/)
  assert.match(w.prompt, /`git add -- <paths>` then `git commit --only -- <same paths>`, never a bare `git commit`/)
  assert.match(calls[0].prompt, /read its source, not only its meta/)
  assert.match(calls[0].prompt, /disable its internal repairs and its own review stages/)
  const bare = await run({ triage: { validate: null } })
  assert.equal(bare.result.validate, null)
  assert.ok(bare.result.next.includes(`with ${TRIAGE.verify} alone, no validation workflow being named by the repository's instructions as its validation, rebuilding`))
  const unsupported = await run({ triage: { validate: { name: 'full-check', args: null, limitation: 'does not forward maxCycles' } } })
  assert.match(unsupported.result.next, /with the component stages of \/full-check, launched separately one read-only stage at a time since it cannot run with repairs and its own reviews disabled \(does not forward maxCycles\), their artifacts then cleaned as its validation, rebuilding/)
  assert.doesNotMatch(unsupported.result.next, / alone, no validation|Workflow \/full-check/)
})

test('nothing is ever dispatched to push, comment or nest a workflow, and Codex is only asked', async () => {
  const runs = [await run(), await run({ implement: { buildOk: false } }), await run({ triage: { disposition: 'reply' } }), await run({ verify: { pass: false } })]
  for (const { labels, calls } of runs) {
    assert.ok(labels.every(l => !/^(push|comment|post)/.test(l)), labels.join(','))
    for (const c of calls.filter(c => c.agentType === 'coworker')) assert.doesNotMatch(c.prompt, /--worktree|--lane main\b/, c.label)
  }
})

test('an agent that throws is a dead agent, not a dead workflow', async () => {
  for (const [throwOn, reason] of [['triage', 'triage-died'], ['implement', 'implement-died']]) {
    const { result, logs } = await run({ throwOn })
    assert.equal(result.pass, false, throwOn)
    assert.equal(result.reason, reason, throwOn)
    assert.ok(logs.some(l => l.includes(`${throwOn} errored — ${throwOn} exploded`)), logs.join('\n'))
  }
  const { result } = await run({ throwOn: 'verify' })
  assert.equal(result.reason, 'verify-failed')
  assert.equal(result.verify.detail, 'verify agent died')
})

test('the verifier defers to the build contract for what counts as verified', async () => {
  const { calls } = await run()
  assert.match(calls.find(c => c.label === 'verify').prompt, /or the command's build-contract skill defines the outcome as verified/)
})

test('every exit logs one stage table: what ran, what never ran, and the caller\'s stages', async () => {
  const table = logs => {
    const t = logs.filter(l => l.startsWith('| stage |'))
    assert.equal(t.length, 1, 'exactly one table')
    return t[0].split('\n').slice(2).map(r => r.slice(2, -2).split(' | '))
  }
  const outcomes = rows => rows.map(r => `${r[0]}: ${r[2]}`)
  const notRun = ['completion review: not run', 'validation: not run', 'HIL: not run', 'PR: not run']

  const happy = table((await run()).logs)
  assert.deepEqual(outcomes(happy), ['triage: done', 'co-plan: agreed', 'implement: built', 'verify: pass', 'completion review: pending',
    'validation: pending', 'HIL: if needed', 'PR: pending'])
  assert.deepEqual(happy.map(r => r[1]), ['Explore', 'Plan+Codex', 'code-writer', 'haiku', 'caller', 'caller', 'caller', 'caller'])
  assert.equal(happy[0][3], `issue #28 implement; scope ${TRIAGE.scope.join(', ')}; verify: ${TRIAGE.verify}`)
  assert.equal(happy[1][3], `lane ${LANE}, 1 round(s); scope ${PLAN.scope.join(', ')}`)
  assert.equal(happy[3][3], '2 commit(s) on issue-28, abc1234def5678..1111111; ok', 'HEAD is the newest commit git log lists')
  assert.equal(happy[4][3], `co-review on ${LANE}`)
  assert.equal(happy[5][3], '/validate')

  for (const [opts, expected] of [
    [{ triage: null }, ['triage: died', 'co-plan: not run', 'implement: not run', 'verify: not run']],
    [{ triage: { disposition: 'unclear', needsUser: 'which kernel?' } }, ['triage: needs-user', 'co-plan: not run', 'implement: not run', 'verify: not run']],
    [{ triage: { disposition: 'reply', draftReply: 'Which board?' } }, ['triage: not actionable', 'co-plan: not run', 'implement: not run', 'verify: not run']],
    [{ triage: { verify: null } }, ['triage: incomplete', 'co-plan: not run', 'implement: not run', 'verify: not run']],
    [{ 'plan:status': { exit: 1 } }, ['triage: done', 'co-plan: coplan-unavailable', 'implement: not run', 'verify: not run']],
    [{ args: { target: '28' } }, ['triage: done', 'co-plan: agreed', 'implement: not run', 'verify: not run']],
    [{ implement: null }, ['triage: done', 'co-plan: agreed', 'implement: died', 'verify: not run']],
    [{ implement: { buildOk: false, notes: 'undefined reference' } }, ['triage: done', 'co-plan: agreed', 'implement: build failed', 'verify: not run']],
    [{ verify: { pass: false, detail: 'error: x' } }, ['triage: done', 'co-plan: agreed', 'implement: built', 'verify: verify-failed']],
    [{ verify: { dirty: [' M a.c'] } }, ['triage: done', 'co-plan: agreed', 'implement: built', 'verify: dirty-tree']],
  ]) {
    const rows = table((await run(opts)).logs)
    assert.deepEqual(outcomes(rows), [...expected, ...notRun], JSON.stringify(opts))
    assert.ok(rows.slice(4).every(r => r[3] === ''), 'a stage that will not run has no fact')
  }
  const needs = table((await run({ triage: { disposition: 'unclear', needsUser: 'which kernel?' } })).logs)
  assert.equal(needs[0][3], 'issue #28 unclear: which kernel?')
  assert.equal(table((await run({ implement: { buildOk: false, notes: 'x | y\nz' } })).logs)[2][3], 'x \\| y z', 'one line, pipes escaped')
  assert.equal(table((await run({ implement: { buildOk: false, notes: 'a \\| b' } })).logs)[2][3], 'a \\\\\\| b', 'a backslash is escaped before the pipe')
})
