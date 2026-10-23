import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/features/license/license.api.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source.replace(/^import .*$/gm, '').replace(/^export /gm, ''), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

test('activation, deactivation and Google sign-in wait for stale status polls, including failed polls', async () => {
  for (const action of ['activate', 'deactivate', 'login']) for (const fail of [false, true]) {
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
    assert.equal(calls[1].url, action === 'login' ? '/api/auth/desktop/start' : `/api/license/${action}`)
    assert.equal(calls[1].options.method, 'POST')
    assert.equal(calls[0].options.cache, 'no-store')
    if (action === 'activate') assert.deepEqual(JSON.parse(calls[1].options.body), { key: 'ZMAI-TEST' })
  }
})

test('APP Google sign-in leaves hardware detection to the local backend', async () => {
  const calls = []
  const api = vm.runInNewContext(`${code}; licenseApi`, { fetchJson: (url, options) => { calls.push({ url, options }); return Promise.resolve({}) } })
  await api.login()
  assert.equal(calls[0].url, '/api/auth/desktop/start')
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(calls[0].options.body, undefined)
})

test('opening access upgrades an in-flight cached poll to a forced server check', async () => {
  for (const fail of [false, true]) {
    const calls = []
    let resolve, reject
    const previous = new Promise((a, b) => { resolve = a; reject = b })
    const verified = { valid: false, state: 'blocked' }
    const api = vm.runInNewContext(`${code}; licenseApi`, { fetchJson: (url, options) => {
      calls.push({ url, options })
      return calls.length === 1 ? previous : Promise.resolve(verified)
    } })
    const poll = api.status().catch(() => {})
    const opened = api.status(true)
    const duplicate = api.status(true)
    assert.equal(calls.length, 1)
    if (fail) reject(new Error('network'))
    else resolve({ valid: true })
    await poll
    assert.equal(await opened, verified)
    assert.equal(await duplicate, verified)
    assert.equal(calls.length, 2)
    assert.equal(calls[1].url, '/api/license/status?force=true')
    assert.equal(calls[1].options.cache, 'no-store')
    await api.status(true)
    assert.equal(calls.length, 3, 'a later open must not reuse a completed check')
  }
})

test('credential changes wait for the forced check queued by an access page open', async () => {
  const calls = []
  let finishPoll, finishForced
  const pollResult = new Promise(resolve => { finishPoll = resolve })
  const forcedResult = new Promise(resolve => { finishForced = resolve })
  const api = vm.runInNewContext(`${code}; licenseApi`, { fetchJson: url => {
    calls.push(url)
    return url.includes('force=true') ? forcedResult : url.includes('/status') ? pollResult : Promise.resolve({})
  } })
  const poll = api.status()
  const opened = api.status(true)
  const changed = api.deactivate()
  finishPoll({})
  await poll
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(calls, ['/api/license/status', '/api/license/status?force=true'])
  finishForced({})
  await opened
  await changed
  assert.equal(calls[2], '/api/license/deactivate')
})

const page = ts.createSourceFile('LicensePage.tsx', readFileSync(new URL('../frontend/src/features/license/LicensePage.tsx', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
let openingEffect
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(page) === 'useEffect'
    && node.arguments[0]?.getText(page).includes('licenseApi.status(true)')) openingEffect = node
  ts.forEachChild(node, visit)
}
visit(page)

test('each access page mount verifies immediately without a callback rerender loop', async () => {
  assert.ok(openingEffect)
  assert.equal(openingEffect.arguments[1].getText(page), '[]')
  const effect = ts.transpileModule(`(${openingEffect.arguments[0].getText(page)})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText
  for (const result of [{ valid: false, state: 'blocked' }, { valid: true, transient: true }, new Error('network')]) {
    const calls = [], statuses = [], errors = [], checking = []
    let finish
    const request = new Promise((resolve, reject) => { finish = () => result instanceof Error ? reject(result) : resolve(result) })
    const context = {
      licenseApi: { status: force => { calls.push(force); return request } },
      refreshContext: { current: { locale: 'en', onStatusChange: next => statuses.push(next) } },
      setChecking: value => checking.push(value), setError: value => errors.push(value),
      webErrorText: (code, locale) => `${locale}:${code}`,
    }
    const open = vm.runInNewContext(effect, context)
    const close = open()
    assert.deepEqual(calls, [true])
    finish()
    await request.catch(() => {})
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(checking, [false])
    assert.equal(statuses.length, result instanceof Error ? 0 : 1)
    assert.equal(errors.length, result instanceof Error || result.transient ? 1 : 0)
    if (errors.length) assert.equal(errors[0], 'en:LICENSE_SERVER_UNAVAILABLE')
    close()
    const closeAgain = open()
    assert.deepEqual(calls, [true, true])
    closeAgain()
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(checking, [false], 'closed pages ignore late verification results')
  }
})
