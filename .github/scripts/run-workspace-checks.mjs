// Run every workspace check at the same time and report all failures.
//
// The unit of work is a CHECK, and not a workspace. A package that declares
// `check:*` sub-scripts gives one unit for each sub-script. A package with a
// plain `check` gives that. This is the same selection rule the old CI matrix
// used, so the set of commands is unchanged. Only the schedule is different.
//
// This is not `npm run --ws check`, because that command is serial and stops
// at the first workspace that fails. This runs every unit and fails at the
// end with the full list.
//
// The output of each unit goes to a buffer and prints on completion inside a
// group that collapses. Children that write to one stdout together interleave
// their lines, and a failure is then hard to read.
//
// This also runs on a laptop: `node .github/scripts/run-workspace-checks.mjs`.
// `--concurrency N` sets the limit. `--slice K/N` slices units. `--list` prints the units and exits.

import { execFileSync, spawn } from 'node:child_process'
import { availableParallelism } from 'node:os'

const IS_CI = Boolean(process.env.GITHUB_ACTIONS)
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm'

/** @returns {{pkg: string, script: string}[]} */
function discoverUnits() {
  const raw = execFileSync(NPM, ['query', '.workspace'], {
    encoding: 'utf-8',
    shell: process.platform === 'win32',
  })
  /** @type {{location: string, scripts?: Record<string,string>}[]} */
  const pkgs = JSON.parse(raw)
  pkgs.sort((a, b) => (a.location < b.location ? -1 : a.location > b.location ? 1 : 0))

  /** @type {{pkg: string, script: string}[]} */
  const units = []
  for (const pkg of pkgs) {
    const scripts = pkg.scripts || {}
    const subs = Object.keys(scripts).filter((s) => /^check:.+$/.test(s))
    if (subs.length > 0) {
      for (const script of subs) units.push({ pkg: pkg.location, script })
    } else if (scripts.check) {
      units.push({ pkg: pkg.location, script: 'check' })
    }
  }
  return units
}

/** @param {string[]} argv @returns {{index: number, count: number} | null} */
function parseSlice(argv) {
  let raw = null
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg === '--slice') {
      if (i + 1 >= argv.length) {
        console.error('::error::--slice requires a value in the form K/N (e.g. 1/4)')
        process.exit(1)
      }
      raw = argv[i + 1]
      break
    } else if (arg.startsWith('--slice=')) {
      raw = arg.slice('--slice='.length)
      break
    }
  }
  if (!raw) return null

  const match = /^([1-9]\d*)\/([1-9]\d*)$/.exec(raw)
  if (!match) {
    console.error(`::error::Invalid --slice format: ${raw}. Expected K/N with positive integers (e.g. 1/4)`)
    process.exit(1)
  }
  const index = Number(match[1])
  const count = Number(match[2])
  if (index > count) {
    console.error(`::error::Invalid --slice bounds: ${raw}. Must satisfy 1 <= K <= N (received ${index} > ${count})`)
    process.exit(1)
  }
  return { index, count }
}

/** @param {{pkg: string, script: string}} unit */
function runUnit(unit) {
  return new Promise((resolve) => {
    const started = Date.now()
    const child = spawn(NPM, ['run', '--prefix', unit.pkg, unit.script], {
      // Buffer, and do not inherit. Children that share one stdout
      // interleave their lines, and a failure is then hard to read.
      stdio: ['ignore', 'pipe', 'pipe'],
      shell: process.platform === 'win32',
    })
    /** @type {Buffer[]} */
    const chunks = []
    child.stdout.on('data', (c) => chunks.push(c))
    child.stderr.on('data', (c) => chunks.push(c))
    child.on('error', (err) => {
      chunks.push(Buffer.from(`failed to spawn: ${err.message}\n`))
      resolve({ unit, code: 1, output: Buffer.concat(chunks).toString('utf-8'), ms: Date.now() - started })
    })
    child.on('close', (code) => {
      resolve({
        unit,
        code: code ?? 1,
        output: Buffer.concat(chunks).toString('utf-8'),
        ms: Date.now() - started,
      })
    })
  })
}

async function main() {
  const argv = process.argv.slice(2)
  const sliceSpec = parseSlice(argv)
  const allUnits = discoverUnits()

  if (allUnits.length === 0) {
    console.error(
      '::error::No workspace package declares a check script — refusing to report green having run nothing.',
    )
    process.exit(1)
  }

  const units = sliceSpec
    ? allUnits.filter((_, idx) => idx % sliceSpec.count === sliceSpec.index - 1)
    : allUnits

  if (units.length === 0) {
    if (argv.includes('--list')) {
      return
    }
    console.log(
      `slice ${sliceSpec.index}/${sliceSpec.count}: 0 checks assigned (${allUnits.length} total discovered) — nothing to run`,
    )
    return
  }

  if (argv.includes('--list')) {
    for (const u of units) console.log(`${u.pkg} :: ${u.script}`)
    return
  }

  const flagIdx = argv.indexOf('--concurrency')
  let parsedConcurrency = NaN
  if (flagIdx !== -1 && argv[flagIdx + 1]) {
    parsedConcurrency = Number(argv[flagIdx + 1])
  } else {
    for (const arg of argv) {
      if (arg.startsWith('--concurrency=')) {
        parsedConcurrency = Number(arg.slice('--concurrency='.length))
        break
      }
    }
  }
  const concurrency = Math.max(
    1,
    !isNaN(parsedConcurrency) ? parsedConcurrency : Math.min(units.length, availableParallelism()),
  )

  console.log(`running ${units.length} checks, up to ${concurrency} at a time:`)
  for (const u of units) console.log(`  ${u.pkg} :: ${u.script}`)
  console.log('')

  const queue = [...units]
  /** @type {{unit: {pkg: string, script: string}, code: number, output: string, ms: number}[]} */
  const results = []

  async function worker() {
    for (;;) {
      const unit = queue.shift()
      if (!unit) return
      const res = await runUnit(unit)
      results.push(res)
      const label = `${res.unit.pkg} :: ${res.unit.script}`
      const secs = (res.ms / 1000).toFixed(1)
      const status = res.code === 0 ? 'PASS' : 'FAIL'
      if (IS_CI) console.log(`::group::${status} ${label} (${secs}s)`)
      else console.log(`----- ${status} ${label} (${secs}s) -----`)
      process.stdout.write(res.output.endsWith('\n') ? res.output : res.output + '\n')
      if (IS_CI) console.log('::endgroup::')
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, units.length) }, worker))

  const failed = results.filter((r) => r.code !== 0)
  console.log('\n=== summary ===')
  for (const r of [...results].sort((a, b) => b.ms - a.ms)) {
    console.log(
      `  ${r.code === 0 ? 'pass' : 'FAIL'}  ${(r.ms / 1000).toFixed(1).padStart(6)}s  ${r.unit.pkg} :: ${r.unit.script}`,
    )
  }

  if (failed.length > 0) {
    for (const r of failed) console.error(`::error::${r.unit.pkg} :: ${r.unit.script} failed`)
    console.error(`::error::${failed.length} of ${results.length} checks failed`)
    // Not process.exit(): it drops what stdout still buffers, which is the failing check's output.
    process.exitCode = 1
    return
  }
  console.log(`\nall ${results.length} checks passed`)
}

await main()
