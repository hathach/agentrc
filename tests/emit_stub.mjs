// The AGENTRC_EMIT transport as both workflow harnesses stub it: a relay command's file, the data module lib/seal.py's
// emit writes there, a launch's emit directory and workflow({scriptPath})'s load. Each harness keeps its own receipts,
// journal and resume state.
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
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

// facts.py emit-dir's directory for one run: `given` (an earlier run's, kept), else a new one made on first use under
// `root`, the directory mkdtemp made, which cleanup() removes unless `keep` (`always`: a run that throws keeps nothing).
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
      }
      return made
    },
    get made () { return made },
    get root () { return root },
    cleanup (always = false) { if (root && (always || !keep)) rmSync(root, { recursive: true, force: true }) },
  }
}

// workflow({scriptPath}) as the runtime runs it: the file's body, its return value the result; an unreadable file
// throws, and a data module nests nothing.
export function loadModule (path, a, { pipeline = null, parallel = null, log = () => {} } = {}) {
  const text = readFileSync(path, 'utf8').replace(/^export const meta = /m, 'const meta = ')
  const nested = () => { throw new Error('a child workflow nests nothing') }
  return new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'phase', 'log', 'workflow', 'budget', ...ABSENT, text)(
    a, nested, pipeline, parallel, () => {}, log, nested, null, ...ABSENT.map(() => undefined))
}
