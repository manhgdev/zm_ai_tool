/** Chạy API :8787 + Vite :5173 — một terminal, Ctrl+C dừng cả hai. */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { tmpdir } from 'node:os'
import { createHash } from 'node:crypto'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const backendDir = path.join(root, 'backend')
const frontendDir = path.join(root, 'frontend')
const isWin = process.platform === 'win32'
const PORTS = [5173, 8787]
const lockPath = path.join(
  tmpdir(),
  `zm-ai-tool-dev-${createHash('sha1').update(root).digest('hex').slice(0, 12)}.lock`,
)

function acquireDevLock() {
  try {
    writeFileSync(lockPath, String(process.pid), { flag: 'wx' })
    return
  } catch (err) {
    if (err?.code !== 'EEXIST') throw err
  }
  const oldPid = Number(readFileSync(lockPath, 'utf8').trim())
  if (!Number.isSafeInteger(oldPid) || oldPid <= 0) {
    unlinkSync(lockPath)
    writeFileSync(lockPath, String(process.pid), { flag: 'wx' })
    return
  }
  try {
    if (!isWin) {
      process.kill(oldPid, 0)
      const check = spawnSync('ps', ['-p', String(oldPid), '-o', 'command='], { encoding: 'utf8' })
      if (check.status !== 0 || !/scripts[\\/]dev\.mjs/.test(String(check.stdout || ''))) {
        unlinkSync(lockPath)
        writeFileSync(lockPath, String(process.pid), { flag: 'wx' })
        return
      }
    }
    // PID có thể đã được Windows cấp lại cho tiến trình khác sau khi runner cũ chết.
    if (isWin) {
      const check = spawnSync('powershell.exe', [
        '-NoProfile',
        '-Command',
        `$p = Get-CimInstance Win32_Process -Filter "ProcessId = ${oldPid}" -ErrorAction SilentlyContinue; if ($p -and $p.Name -match '^node(\\.exe)?$' -and $p.CommandLine -match 'scripts[\\\\/]dev\\.mjs') { 'dev-runner' }`,
      ], { encoding: 'utf8', timeout: 3_000 })
      if (check.error || check.status !== 0) {
        throw new Error(`Không xác minh được dev:all cũ (pid ${oldPid}).`)
      }
      if (!String(check.stdout || '').includes('dev-runner')) {
        unlinkSync(lockPath)
        writeFileSync(lockPath, String(process.pid), { flag: 'wx' })
        return
      }
    }
    console.log(`Đang dừng dev:all cũ (pid ${oldPid})…`)
    if (isWin) {
      // PID đã được xác minh đúng runner; /T dọn luôn Vite/Uvicorn con, không đụng terminal cha.
      const stopped = spawnSync(
        'taskkill',
        ['/PID', String(oldPid), '/T', '/F'],
        { stdio: 'ignore', timeout: 8_000 },
      )
      if (stopped.status !== 0) throw new Error(`Không dừng được dev:all cũ (pid ${oldPid}).`)
    } else {
      process.kill(oldPid, 'SIGTERM')
    }
    unlinkSync(lockPath)
    writeFileSync(lockPath, String(process.pid), { flag: 'wx' })
    console.log(`Đã dừng dev:all cũ (pid ${oldPid}).`)
  } catch (err) {
    if (err?.code !== 'ESRCH') throw err
    unlinkSync(lockPath)
    writeFileSync(lockPath, String(process.pid), { flag: 'wx' })
  }
}

function releaseDevLock() {
  try {
    if (readFileSync(lockPath, 'utf8').trim() === String(process.pid)) unlinkSync(lockPath)
  } catch {
    /* lock already removed */
  }
}

function venvPython() {
  const candidates = isWin
    ? [
        path.join(backendDir, '.venv', 'Scripts', 'python.exe'),
        path.join(root, '.venv', 'Scripts', 'python.exe'),
      ]
    : [
        path.join(backendDir, '.venv', 'bin', 'python3'),
        path.join(backendDir, '.venv', 'bin', 'python'),
        path.join(root, '.venv', 'bin', 'python3'),
      ]
  for (const p of candidates) {
    if (existsSync(p)) return p
  }
  return null
}

function shQuote(arg) {
  const s = String(arg)
  if (!/[ \t"&<>|^()]/.test(s)) return s
  return `"${s.replace(/"/g, '""')}"`
}

function shellCmd(cmd, args) {
  return [cmd, ...args].map(shQuote).join(' ')
}

function hasCmd(cmd, args = ['--version']) {
  if (isWin && (cmd === 'npm' || cmd === 'bun')) {
    return spawnSync(shellCmd(cmd, args), { stdio: 'ignore', shell: true }).status === 0
  }
  return spawnSync(cmd, args, { stdio: 'ignore', shell: false }).status === 0
}

/** Chỉ kill PID đang LISTEN cổng — không quét lan. */
function freePorts(ports, failedPids = new Set()) {
  if (!isWin) return true
  let success = true
  for (const port of ports) {
    const out = spawnSync('cmd', ['/c', `netstat -ano | findstr :${port}`], {
      encoding: 'utf8',
    })
    const text = `${out.stdout || ''}${out.stderr || ''}`
    const pids = new Set()
    for (const line of text.split(/\r?\n/)) {
      if (!/LISTENING/i.test(line)) continue
      const m = line.trim().match(/(\d+)\s*$/)
      if (m) pids.add(m[1])
    }
    for (const pid of pids) {
      // Không /T — tránh giết nhầm parent shell / terminal VS Code
      const stopped = spawnSync('taskkill', ['/pid', pid, '/F'], {
        encoding: 'utf8',
        timeout: 8_000,
      })
      // Windows đôi khi xóa process cha nhưng worker Python vẫn giữ socket kế thừa;
      // netstat lúc đó tiếp tục trả PID cha đã chết.
      const orphanStopped = stopped.status === 0 ? null : spawnSync('powershell.exe', [
        '-NoProfile',
        '-Command',
        `$children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = ${pid}" -ErrorAction SilentlyContinue); if ($children.Count) { $children | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; exit 0 }; exit 1`,
      ], { encoding: 'utf8', timeout: 8_000 })
      if (stopped.status === 0 || orphanStopped?.status === 0) {
        console.log(`Đã giải phóng cổng ${port} (pid ${pid})`)
      } else {
        success = false
        if (!failedPids.has(pid)) {
          failedPids.add(pid)
          const detail = String(stopped.stderr || stopped.stdout || '').trim()
          console.error(`Không thể dừng tiến trình giữ cổng ${port} (pid ${pid})${detail ? `: ${detail}` : '.'}`)
        }
      }
    }
  }
  return success
}

function portsFree(ports) {
  if (!isWin) return true
  return ports.every((port) => {
    const out = spawnSync('cmd', ['/c', `netstat -ano | findstr :${port}`], { encoding: 'utf8' })
    return !/LISTENING/i.test(`${out.stdout || ''}${out.stderr || ''}`)
  })
}

async function freePortsAndWait(ports) {
  if (!isWin) return true
  const failedPids = new Set()
  for (let attempt = 0; attempt < 25; attempt += 1) {
    if (portsFree(ports)) return true
    freePorts(ports, failedPids)
    if (portsFree(ports)) return true
    await new Promise((resolve) => setTimeout(resolve, 120))
  }
  return false
}

const py = venvPython() ?? (hasCmd('python') ? 'python' : hasCmd('python3') ? 'python3' : null)
if (!py) {
  console.error('Không tìm thấy python — cài Python 3 rồi thử lại.')
  process.exitCode = 1
  // Không process.exit — để shell còn mở
  console.error('Giữ terminal. Sửa Python rồi chạy lại: npm start')
  process.stdin.resume()
} else {
  acquireDevLock()
  process.on('exit', releaseDevLock)

  const viteBin = path.join(root, 'node_modules', 'vite', 'bin', 'vite.js')
  const webCmd = process.execPath
  // ponytail: chạy thẳng Vite, tránh cmd/npm wrapper còn sống sau khi terminal dừng.
  const webArgs = [viteBin, '--config', path.join(frontendDir, 'vite.config.ts'), '--strictPort']

  /** @type {Map<string, import('node:child_process').ChildProcess>} */
  const children = new Map()
  let shuttingDown = false
  const API_READY_URL = 'http://127.0.0.1:8787/api/health'

  async function apiIsReady() {
    try {
      const res = await fetch(API_READY_URL, { signal: AbortSignal.timeout(800) })
      return res.ok
    } catch {
      return false
    }
  }

  function spawnOne(label, cmd, args, cwd) {
    const opts = {
      cwd,
      stdio: 'inherit',
      env: label === 'api'
        ? {
            ...process.env,
            ZM_AI_TOOL_SUPERVISED: '1',
            PYTHONOPTIMIZE: '1',
            PYTHONUNBUFFERED: '1',
            OLLAMA_NUM_PARALLEL: '2',  // cho phép 2 inference đồng thời — M5 Pro 48GB RAM đủ
          }
        : process.env,
      // Windows: tách khỏi job object của terminal khi có thể
      detached: false,
    }
    const child = spawn(cmd, args, { ...opts, shell: false })

    children.set(label, child)

    child.on('error', (err) => {
      console.error(`[${label}] spawn error:`, err.message)
    })

    child.on('exit', async (code, signal) => {
      // Một child cũ có thể thoát sau khi child mới đã được tạo.
      if (children.get(label) === child) children.delete(label)
      if (shuttingDown) return
      if (signal === 'SIGTERM' || signal === 'SIGINT') return

      // Uvicorn/Vite có thể tách launcher trên Windows. Không restart từ exit code
      // của launcher vì dễ giết server con đang giữ cổng và tạo WinError 10048.
      if (label === 'api' && await apiIsReady()) {
        console.log('[api] launcher đã thoát nhưng API vẫn sẵn sàng.')
      } else if (label === 'web' && !portsFree([5173])) {
        console.log('[web] launcher đã thoát nhưng Vite vẫn đang chạy trên 5173.')
      } else {
        console.error(`[${label}] đã dừng (mã ${code ?? 'signal'}); chạy lại npm run dev:all nếu cần.`)
      }
    })

    return child
  }

  function shutdown() {
    if (shuttingDown) return
    shuttingDown = true
    console.log('\nĐang dừng API + Vite…')
    for (const [, c] of children) {
      if (!c.pid) continue
      try {
        if (isWin) {
          // Chỉ process con, không /T để tránh kill terminal host
          spawnSync('taskkill', ['/pid', String(c.pid), '/F'], { stdio: 'ignore' })
        } else if (!c.killed) {
          c.kill('SIGTERM')
        }
      } catch {
        /* ignore */
      }
    }
    // Dọn process listener thật sự (Vite/Uvicorn con), kể cả khi launcher đã thoát trước.
    freePorts(PORTS)
    // Thoát 0 — VS Code không báo "terminated with exit code 5"
    setTimeout(() => process.exit(0), 200)
  }

  process.on('SIGINT', shutdown)
  process.on('SIGTERM', shutdown)
  // Uncaught trong script dev không được sập im lặng
  process.on('uncaughtException', (err) => {
    console.error('[dev] uncaughtException:', err)
  })
  process.on('unhandledRejection', (err) => {
    console.error('[dev] unhandledRejection:', err)
  })

  if (!await freePortsAndWait(PORTS)) {
    console.error('Không thể khởi động: cổng 5173 hoặc 8787 vẫn đang bị tiến trình khác sử dụng.')
    releaseDevLock()
    process.exit(1)
  }

  console.log(`API  → http://127.0.0.1:8787  (${py} uvicorn)`)
  console.log('Web  → http://127.0.0.1:5173  (Vite)')
  console.log('Ctrl+C để dừng API + Vite.\n')

  // Reload làm chậm boot (2 process) + hay miss worker trên Windows.
  // Bật lại: set ZM_AI_TOOL_RELOAD=1
  const wantReload = /^(1|true|yes)$/i.test(String(process.env.ZM_AI_TOOL_RELOAD || ''))
  const apiArgs = wantReload
    ? [
        '-m',
        'uvicorn',
        'main:app',
        '--reload',
        '--reload-dir', path.join(backendDir, 'api'),
        '--reload-dir', path.join(backendDir, 'pipeline'),
        '--host',
        '127.0.0.1',
        '--port',
        '8787',
      ]
    : ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8787']

  spawnOne('api', py, apiArgs, backendDir)
  if (!wantReload) {
    console.log('API reload tắt (nhanh hơn). ZM_AI_TOOL_RELOAD=1 để bật.\n')
  }

  // /api/health — không import torch / engines (tránh đợi warm)
  const API_WAIT_MS = 30_000
  const API_POLL_MS = 200

  async function waitForApi() {
    const t0 = Date.now()
    const deadline = t0 + API_WAIT_MS
    let warned = false
    while (Date.now() < deadline) {
      if (shuttingDown) return false
      try {
        if (await apiIsReady()) {
          console.log(`API sẵn sàng sau ${((Date.now() - t0) / 1000).toFixed(1)}s`)
          return true
        }
      } catch {
        if (!warned) {
          console.log('Đang chờ API sẵn sàng…')
          warned = true
        }
      }
      await new Promise((r) => setTimeout(r, API_POLL_MS))
    }
    console.error('API chưa sẵn sàng sau 30s — vẫn mở Vite (proxy có thể lỗi tạm).')
    return false
  }

  void waitForApi().then(async (ok) => {
    if (shuttingDown) return
    if (ok) console.log('Mở Vite.\n')
    // Vite phải dùng đúng 5173; không để instance cũ ép nó nhảy sang 5174.
    if (!await freePortsAndWait([5173])) {
      console.error('Không giải phóng được cổng 5173; dừng Vite để tránh chạy nhầm cổng.')
      return
    }
    spawnOne('web', webCmd, webArgs, root)
  })

  // Giữ event loop sống dù cả hai child die tạm thời (đang restart)
  setInterval(() => {}, 60_000)
}
