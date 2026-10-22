import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { accountStorage } from '../frontend/src/app/appSettings.ts'

test('web account UI caches stay separate and never consume desktop caches', () => {
  const values = new Map([['zm_ai_tool.session', 'desktop-project']])
  const native = {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    clear: () => values.clear(),
  }
  const a = accountStorage(native, 'user-a')
  const b = accountStorage(native, 'user-b')
  assert.equal(a.getItem('zm_ai_tool.session'), null)
  a.setItem('zm_ai_tool.session', 'project-a')
  b.setItem('zm_ai_tool.session', 'project-b')
  assert.equal(a.getItem('zm_ai_tool.session'), 'project-a')
  assert.equal(b.getItem('zm_ai_tool.session'), 'project-b')
  assert.equal(native.getItem('zm_ai_tool.session'), 'desktop-project')
  assert.equal(a.length, 1)
  assert.equal(a.key(0), 'zm_ai_tool.session')
  a.clear()
  assert.equal(a.length, 0)
  assert.equal(b.getItem('zm_ai_tool.session'), 'project-b')
})

test('dev/start share one Web runner, proxy auth, reject busy ports and stop both servers', { timeout: 45_000 }, async t => {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const python = path.join(root, 'backend/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
  if (!existsSync(python)) return t.skip('Requires the backend environment from setup')
  const scripts = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8')).scripts
  assert.equal(scripts.dev, scripts.start)
  assert.equal(scripts.dev, 'node scripts/dev.mjs')
  const listeners = [createServer(), createServer()]
  for (const server of listeners) server.listen(0, '127.0.0.1')
  await Promise.all(listeners.map(server => once(server, 'listening')))
  const [apiPort, webPort] = listeners.map(server => server.address().port)
  await Promise.all(listeners.map(server => new Promise(resolve => server.close(resolve))))
  const data = mkdtempSync(path.join(tmpdir(), 'zm-web-test-'))
  const env = { ...process.env, ZM_AI_TOOL_HOST: '127.0.0.1', ZM_AI_TOOL_PORT: String(apiPort),
    ZM_AI_TOOL_WEB_ORIGIN: `http://localhost:${webPort}`, ZM_AI_TOOL_WEB_DATA: data,
    ZM_AI_TOOL_GOOGLE_CLIENT_ID: '', ZM_AI_TOOL_GOOGLE_CLIENT_SECRET: '', ZM_AI_TOOL_DESKTOP: '1' }
  const child = spawn(process.execPath, ['scripts/dev.mjs'], { cwd: root, env, stdio: 'pipe' })
  const finished = once(child, 'exit')
  let output = ''
  child.stdout.on('data', chunk => { output += chunk })
  child.stderr.on('data', chunk => { output += chunk })
  const get = (port, route) => fetch(`http://127.0.0.1:${port}${route}`, { signal: AbortSignal.timeout(1000) })
  try {
    let response
    const deadline = Date.now() + 30_000
    while (Date.now() < deadline && child.exitCode === null) {
      try {
        response = await get(webPort, '/api/auth/session')
        if (response.ok) break
      } catch { /* Wait for API and Vite. */ }
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    assert.ok(response?.ok, output)
    assert.deepEqual(await response.json(), { mode: 'web', configured: false, user: null })
    assert.equal((await (await get(apiPort, '/api/health')).json()).mode, 'web')
    assert.equal((await get(webPort, '/api/config')).status, 401)
    assert.match(await (await get(webPort, '/login')).text(), /@vite\/client/)
    const duplicate = spawn(process.execPath, ['scripts/dev.mjs'], { cwd: root, env, stdio: 'ignore' })
    assert.equal((await once(duplicate, 'exit'))[0], 1)
    assert.equal((await get(webPort, '/api/health')).status, 200)
  } finally {
    child.kill('SIGTERM')
    await finished
    rmSync(data, { recursive: true, force: true })
  }
  for (const port of [apiPort, webPort]) await assert.rejects(() => get(port, '/api/health'))
})
