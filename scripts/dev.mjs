/** Web runner: dev uses Vite; --production serves frontend/dist through Python. */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
if (existsSync(path.join(root, '.env'))) process.loadEnvFile(path.join(root, '.env'))
const backend = path.join(root, 'backend')
const production = process.argv.includes('--production')
const host = process.env.ZM_AI_TOOL_HOST || '127.0.0.1'
const apiPort = Number(process.env.ZM_AI_TOOL_PORT || 8787)
const origin = process.env.ZM_AI_TOOL_WEB_ORIGIN || `http://localhost:${production ? apiPort : 5173}`
const originUrl = new URL(origin)
const webPort = originUrl.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(originUrl.hostname)
  ? Number(originUrl.port || 80) : 5173
const connectHost = host === '0.0.0.0' ? '127.0.0.1' : host.includes(':') ? `[${host === '::' ? '::1' : host}]` : host
const apiUrl = `http://${connectHost}:${apiPort}`
const env = { ...process.env, ZM_AI_TOOL_DESKTOP: '0', ZM_AI_TOOL_WEB_ORIGIN: origin,
  ZM_AI_TOOL_PORT: String(apiPort), ZM_AI_TOOL_API_URL: apiUrl,
  PYTHONUNBUFFERED: '1', ZM_AI_TOOL_SUPERVISED: '1' }
const python = [path.join(backend, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')].find(existsSync)
const vite = path.join(root, 'node_modules', 'vite', 'bin', 'vite.js')
const children = []
let stopping = false

function stop(code = 0) {
  if (stopping) return
  stopping = true
  process.exitCode = code
  for (const child of children) {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM')
  }
  // Allow the gateway to close private workers and their subprocesses first.
  setTimeout(() => {
    for (const child of children) {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
    }
  }, 45_000).unref()
}
process.on('SIGINT', () => stop())
process.on('SIGTERM', () => stop())

function start(label, command, args, cwd) {
  const child = spawn(command, args, { cwd, env, stdio: 'inherit', shell: false })
  children.push(child)
  child.once('error', error => {
    console.error(`[${label}] ${error.message}`)
    stop(1)
  })
  child.once('exit', (code, signal) => {
    if (!stopping) {
      console.error(`[${label}] Đã dừng / Stopped (${code ?? signal}).`)
      stop(code || 1)
    }
  })
  return child
}

async function requireFreePort(port) {
  await new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', error => reject(new Error(`Không mở được cổng / Cannot bind port ${port}: ${error.code}`)))
    server.listen({ host, port }, () => server.close(resolve))
  })
}

try {
  if (!python || (!production && !existsSync(vite))) throw new Error('Chạy npm run setup trước. / Run npm run setup first.')
  if (production && !existsSync(path.join(root, 'frontend/dist/index.html'))) {
    throw new Error('Chạy npm run build trước npm run start. / Run npm run build before npm run start.')
  }
  if (!(production ? [apiPort] : [apiPort, webPort]).every(port => Number.isInteger(port) && port > 0 && port <= 65535) || (!production && apiPort === webPort)) {
    throw new Error('Cổng API và Web phải hợp lệ và khác nhau. / API and Web ports must be valid and different.')
  }
  // Fail on occupied ports; never terminate an unrelated listener.
  await requireFreePort(apiPort)
  if (!production) await requireFreePort(webPort)
  const api = start('api', python, ['main.py'], backend)
  let ready = false
  const deadline = Date.now() + 30_000
  while (!stopping && Date.now() < deadline) {
    try {
      const response = await fetch(`${apiUrl}/api/health`, { signal: AbortSignal.timeout(800) })
      ready = response.ok && (await response.json()).mode === 'web' && api.exitCode === null
      if (ready) break
    } catch { /* Wait for this API process to start. */ }
    await delay(200)
  }
  if (!stopping) {
    if (!ready) throw new Error('API chưa sẵn sàng sau 30s. / API did not start within 30s.')
    if (!production) {
      start('web', process.execPath, [vite, '--config', path.join(root, 'frontend/vite.config.ts'),
        '--strictPort', '--host', host, '--port', String(webPort)], root)
    }
    console.log(`Web → ${production ? apiUrl : `http://localhost:${webPort}`}\nAPI → ${apiUrl}\nCtrl+C để dừng / to stop.`)
  }
} catch (error) {
  console.error(`[web] ${error.message}`)
  stop(1)
}
