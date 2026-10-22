import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/features/license/license.api.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source.replace(/^import .*$/gm, '').replace(/^export /gm, ''), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

test('activation and deactivation wait for stale status polls, including failed polls', async () => {
  for (const action of ['activate', 'deactivate']) for (const fail of [false, true]) {
    const calls = []
    let resolve, reject
    const previous = new Promise((a, b) => { resolve = a; reject = b })
    const api = vm.runInNewContext(`${code}; licenseApi`, { fetchJson: (url, options) => {
      calls.push({ url, options })
      return url.includes('/status') ? previous : Promise.resolve({ configured: action === 'activate' })
    } })
    const poll = api.status().catch(() => {})
    const changed = api[action]('ZMAI-TEST')
    assert.equal(calls.length, 1)
    if (fail) reject(new Error('network'))
    else resolve({ configured: true })
    await poll
    assert.equal((await changed).configured, action === 'activate')
    assert.equal(calls[1].url, `/api/license/${action}`)
    assert.equal(calls[1].options.method, 'POST')
  }
})
