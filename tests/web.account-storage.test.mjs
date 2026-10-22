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

test('PM2 production uses npm start with one instance and no desktop mode', () => {
  const { apps } = JSON.parse(readFileSync(new URL('../pm2.start.json', import.meta.url), 'utf8'))
  assert.equal(apps.length, 1)
  const app = apps[0]
  assert.equal(app.name, 'zm_ai_tool')
  assert.equal(app.cwd, '.')
  assert.equal(app.script, 'npm')
  assert.equal(app.args, 'run start')
  assert.equal(app.interpreter, 'none')
  assert.equal(app.exec_mode, 'fork')
  assert.equal(app.instances, 1)
  assert.equal(app.watch, false)
  assert.equal(app.env.ZM_AI_TOOL_DESKTOP, '0')
  assert.equal(app.env.ZM_AI_TOOL_HOST, '127.0.0.1')
  assert.ok(app.kill_timeout > 45000) // Let the runner finish its shutdown grace period.
})

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

for (const mode of ['dev', 'dev-busy', 'production']) test(`${mode}: origin, auth, busy ports and shutdown remain safe`, { timeout: 45_000 }, async t => {
  const production = mode === 'production'
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const python = path.join(root, 'backend/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
  if (!existsSync(python)) return t.skip('Requires the backend environment from setup')
  const scripts = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8')).scripts
  assert.equal(scripts.dev, 'node scripts/dev.mjs')
  assert.equal(scripts.start, 'node scripts/dev.mjs --production')
  if (production && !existsSync(path.join(root, 'frontend/dist/index.html'))) return t.skip('Run npm run build first')
  const listeners = [createServer(), createServer()]
  for (const server of listeners) server.listen(0, '127.0.0.1')
  await Promise.all(listeners.map(server => once(server, 'listening')))
  const [apiPort, webPort] = listeners.map(server => server.address().port)
  await Promise.all(listeners.map(server => new Promise(resolve => server.close(resolve))))
  const occupied = createServer()
  if (mode === 'dev-busy') {
    try {
      occupied.listen(5173, '127.0.0.1')
      await once(occupied, 'listening')
    } catch (error) {
      if (error.code !== 'EADDRINUSE') throw error // An existing listener already covers this case.
    }
  }
  const data = mkdtempSync(path.join(tmpdir(), 'zm-web-test-'))
  const env = { ...process.env, ZM_AI_TOOL_HOST: '127.0.0.1', ZM_AI_TOOL_PORT: String(apiPort),
    ZM_AI_TOOL_WEB_ORIGIN: mode === 'dev-busy' ? 'http://localhost:18787' : 'https://zmaitool.com', ZM_AI_TOOL_WEB_DATA: data,
    ZM_AI_TOOL_GOOGLE_CLIENT_ID: 'test-client', ZM_AI_TOOL_GOOGLE_CLIENT_SECRET: 'test-secret',
    ZM_AI_TOOL_OWNER_EMAILS: '', ZM_AI_TOOL_DESKTOP: '1' }
  const args = ['scripts/dev.mjs', ...(production ? ['--production'] : [])]
  const child = spawn(process.execPath, args, { cwd: root, env, stdio: 'pipe' })
  const finished = once(child, 'exit')
  let output = ''
  child.stdout.on('data', chunk => { output += chunk })
  child.stderr.on('data', chunk => { output += chunk })
  const get = (port, route) => fetch(`http://127.0.0.1:${port}${route}`, { signal: AbortSignal.timeout(1000) })
  let pagePort = production ? apiPort : null
  let actualOrigin
  try {
    let response
    const deadline = Date.now() + 30_000
    while (Date.now() < deadline && child.exitCode === null) {
      try {
        actualOrigin = output.match(/Web → (\S+)/)?.[1]
        if (!production && actualOrigin) pagePort = Number(new URL(actualOrigin).port)
        if (!pagePort || !actualOrigin) throw new Error('Waiting for runner URL')
        response = await get(pagePort, '/api/auth/session')
        if (response.ok) break
      } catch { /* Wait for API and Vite. */ }
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    assert.ok(response?.ok, output)
    assert.deepEqual(await response.json(), { mode: 'web', configured: true, user: null })
    assert.equal(actualOrigin, production ? 'https://zmaitool.com' : `http://localhost:${pagePort}`)
    if (mode === 'dev-busy') assert.ok(pagePort > 5173 && pagePort < 18787, output)
    if (!production) assert.ok(pagePort >= 5173 && pagePort < 18787, output)
    const login = await fetch(`http://127.0.0.1:${pagePort}/api/auth/google`, { redirect: 'manual' })
    assert.equal(login.status, 303)
    assert.equal(new URL(login.headers.get('location')).searchParams.get('redirect_uri'), `${actualOrigin}/api/auth/google/callback`)
    const logout = origin => fetch(`http://127.0.0.1:${pagePort}/api/auth/logout`, { method: 'POST', headers: { Origin: origin } })
    assert.equal((await logout(actualOrigin)).status, 200)
    assert.equal((await logout('https://other.example')).status, 403)
    assert.equal((await (await get(apiPort, '/api/health')).json()).mode, 'web')
    assert.equal((await get(pagePort, '/api/config')).status, 401)
    const html = await (await get(pagePort, '/login')).text()
    if (production) {
      assert.doesNotMatch(html, /@vite\/client/)
      const asset = html.match(/src="(\/assets\/[^" ]+\.js)"/)[1]
      assert.equal((await get(pagePort, asset)).status, 200)
      await assert.rejects(() => get(webPort, '/'))
    } else assert.match(html, /@vite\/client/)
    const duplicate = spawn(process.execPath, args, { cwd: root, env, stdio: 'ignore' })
    assert.equal((await once(duplicate, 'exit'))[0], 1)
    assert.equal((await get(pagePort, '/api/health')).status, 200)
  } finally {
    child.kill('SIGTERM')
    await finished
    if (occupied.listening) await new Promise(resolve => occupied.close(resolve))
    rmSync(data, { recursive: true, force: true })
  }
  for (const port of new Set([apiPort, pagePort].filter(Boolean))) await assert.rejects(() => get(port, '/api/health'))
})
