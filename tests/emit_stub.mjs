// The AGENTRC_EMIT transport as both workflow harnesses stub it: a relay command's file, its claim, the data module
// lib/seal.py's emit writes there, a launch's emit directory and workflow({scriptPath})'s load. Each harness keeps its
// own receipts, journal, resume state and claim hook.
import { randomBytes } from 'node:crypto'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
// Node gives a workflow body globals the runtime sandbox does not, so a test run here is more forgiving than
// production: `new URL` cost pr-babysit every run, refusing each one at preflight, while the suite stayed green.
// Shadowing them as parameters makes the body fail here the way it fails there.
export const ABSENT = ['URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'Buffer', 'process', 'fetch', 'structuredClone']

// The file a relay command hands its script as AGENTRC_EMIT.
export const emitOf = (prompt) => String(prompt).match(/AGENTRC_EMIT='([^']+)' /)?.[1]
// Python's json.dumps with ensure_ascii, compact: every UTF-16 unit past 0x7e as \uXXXX.
const asciiJson = (v) => JSON.stringify(v).replace(/[\u007f-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
// The module lib/seal.py's emit writes to `file`, byte for byte.
export const moduleOf = (file, line) => `export const meta = { name: 'emit', description: 'a script line' }\nreturn ${asciiJson({ emit: file, line })}\n`
// The sentinel facts.py emit-dir writes in its directory (EMIT_SENTINEL), an emitted module of its own path.
export const SENTINEL = 'emit-dir.js'
// lib/seal.py's claim of `file`: undefined once created empty, else the refusal the relay answers, nothing run.
export const claimOf = (file) => {
  try {
    writeFileSync(file, '', { flag: 'wx' })
  } catch (e) {
    return ['ENOENT', 'ENOTDIR'].includes(e.code) ? { exit: 4, error: `AGENTRC_EMIT ${file}: ${e.code}: nothing was run` } : { exit: 3 }
  }
}

// facts.py emit-dir's directory for one run: `given` (an earlier run's, kept), else a new one made on first use under
// `root`, the directory mkdtemp made, its sentinel written, which cleanup() removes unless `keep` (`always`: a run that
// throws keeps nothing). answer() is emit-dir's line: the directory and the nonce its sentinel holds.
export function emitDirs (prefix, given, keep) {
  let root = null
  let made = null
  return {
    dir () {
      if (given) return given
      if (!made) {
        root = mkdtempSync(join(tmpdir(), prefix)).replace(/\\/g, '/')
        made = `${root}/agentrc-emit/r1`
        mkdirSync(made, { recursive: true })
        writeFileSync(`${made}/${SENTINEL}`, moduleOf(`${made}/${SENTINEL}`, { dir: made, nonce: randomBytes(8).toString('hex') }))
      }
      return made
    },
    answer () {
      const dir = this.dir()
      return { dir, nonce: JSON.parse(readFileSync(`${dir}/${SENTINEL}`, 'utf8').match(/^return (.*)$/m)[1]).line.nonce }
    },
    get made () { return made },
    get root () { return root },
    cleanup (always = false) { if (root && (always || !keep)) rmSync(root, { recursive: true, force: true }) },
  }
}

// workflow({scriptPath}) as the runtime runs it: the file's body, its return value the result; an unreadable file
// throws, as does one with no `export const meta` (an emit file claimed and left empty: Claude Code 2.1.322), and a
// data module nests nothing.
export function loadModule (path, a, { pipeline = null, parallel = null, log = () => {} } = {}) {
  const raw = readFileSync(path, 'utf8')
  if (!/^export const meta = /m.test(raw)) throw new Error(`${path}: \`export const meta = { name, description, ... }\` required`)
  const text = raw.replace(/^export const meta = /m, 'const meta = ')
  const nested = () => { throw new Error('a child workflow nests nothing') }
  return new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget', ...ABSENT, text)(
    a, nested, pipeline, parallel, () => {}, log, nested, null, ...ABSENT.map(() => undefined))
}
