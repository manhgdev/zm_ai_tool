import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

function client(fetch) {
  const source = readFileSync(new URL('../frontend/src/shared/api/fetchJson.ts', import.meta.url), 'utf8')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const exports = {}
  new Function('exports', 'fetch', 'window', js)(exports, fetch, globalThis)
  return exports.fetchJson
}

test('concurrent default GETs share transport but return isolated objects; refresh is fresh', async () => {
  let calls = 0
  const fetchJson = client(async () => { calls++; return Response.json({ items: [1] }) })
  const [a, b] = await Promise.all([fetchJson('/api/config'), fetchJson('/api/config')])
  assert.equal(calls, 1)
  a.items.push(2)
  assert.deepEqual(b.items, [1])
  await fetchJson('/api/config')
  assert.equal(calls, 2)
})

test('failed reads are evicted and may be retried', async () => {
  let calls = 0
  const fetchJson = client(async () => { calls++; throw new Error('offline') })
  const results = await Promise.allSettled([fetchJson('/api/config'), fetchJson('/api/config')])
  assert.equal(calls, 1)
  assert.ok(results.every(item => item.status === 'rejected'))
  await assert.rejects(fetchJson('/api/config'), /offline/)
  assert.equal(calls, 2)
})

test('writes, custom headers, timeouts and cancellation scopes remain isolated', async () => {
  let calls = 0
  const fetchJson = client(async () => { calls++; return Response.json({ ok: true }) })
  await Promise.all([
    fetchJson('/api/config', { method: 'POST' }), fetchJson('/api/config', { method: 'POST' }),
    fetchJson('/api/config', { headers: { Authorization: 'a' } }),
    fetchJson('/api/config', { headers: { Authorization: 'b' } }),
    fetchJson('/api/config', { signal: new AbortController().signal }),
    fetchJson('/api/config'), fetchJson('/api/config', undefined, 20_000),
  ])
  assert.equal(calls, 7)
})

test('a write prevents subsequent reads from joining a stale pending read', async () => {
  let completeOld
  let calls = 0
  const fetchJson = client(async () => {
    calls++
    if (calls === 1) return new Promise(resolve => { completeOld = resolve })
    return Response.json({ version: calls })
  })
  const old = fetchJson('/api/config')
  await fetchJson('/api/config', { method: 'POST' })
  const fresh = await fetchJson('/api/config')
  assert.equal(fresh.version, 3)
  completeOld(Response.json({ version: 1 }))
  assert.equal((await old).version, 1)
})

test('locale selection does not persist the same value again', () => {
  const source = readFileSync(new URL('../frontend/src/app/App.tsx', import.meta.url), 'utf8')
  const body = source.match(/const changeLocale = \(nextLocale: AppLocale\) => \{([\s\S]*?)\n  \}/)[1]
  let saves = 0
  const change = new Function('nextLocale', 'locale', 'localeChangedRef', 'setLocale', 'licenseStatus', 'api', body)
  change('vi', 'vi', { current: false }, () => {}, { valid: true }, {
    saveLocalePreference: () => { saves++; return Promise.resolve() },
  })
  assert.equal(saves, 0)
})

test('cancelling an isolated request does not cancel a shared read', async () => {
  const fetchJson = client((_url, init) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => resolve(Response.json({ ok: true })), 10)
    init.signal.addEventListener('abort', () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    })
  }))
  const controller = new AbortController()
  const a = fetchJson('/api/config', { signal: controller.signal })
  const b = fetchJson('/api/config')
  controller.abort()
  await assert.rejects(a, { name: 'AbortError' })
  assert.deepEqual(await b, { ok: true })
})

test('output-root guards precede writes and are updated only after successful saves', () => {
  const source = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  assert.ok(source.includes('if (raw === savedOutputRoot.current) return'))
  assert.match(source, /await api.saveOutputRoot\(raw\)\s+savedOutputRoot.current = raw/)
  assert.match(source, /await api.saveOutputRoot\(''\)\s+savedOutputRoot.current = ''/)
})
