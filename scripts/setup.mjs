import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const isWin = process.platform === 'win32'
const venvDir = path.join(root, 'backend', '.venv')
const venvPython = path.join(root, 'backend', '.venv', isWin ? 'Scripts/python.exe' : 'bin/python')
const managedPythonVersion = '3.12.10'

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit', shell: false, ...options })
  if (result.error) console.error(result.error.message)
  if (result.status !== 0) process.exit(result.status ?? 1)
}

function pythonVersion(command, prefix = []) {
  const result = spawnSync(command, [...prefix, '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'], {
    encoding: 'utf8', shell: false,
  })
  const match = result.status === 0 && result.stdout.trim().match(/^(\d+)\.(\d+)$/)
  return match ? [Number(match[1]), Number(match[2])] : null
}

function compatible(version) {
  return version && version[0] === 3 && version[1] >= 10 && version[1] <= 12
}

function pythonCandidates() {
  const home = homedir()
  return isWin
    ? [
        ['py', ['-3.12']], ['py', ['-3.11']], ['py', ['-3.10']],
        ['python3.12', []], ['python3.11', []], ['python3.10', []], ['python', []],
        ...['312', '311', '310'].flatMap(version => [
          [path.join(process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'), 'Programs', 'Python', `Python${version}`, 'python.exe'), []],
          [path.join(process.env.ProgramFiles || 'C:\\Program Files', `Python${version}`, 'python.exe'), []],
        ]),
      ]
    : [
        ['python3.12', []], ['python3.11', []], ['python3.10', []], ['python3', []], ['python', []],
        ['/opt/homebrew/opt/python@3.12/bin/python3.12', []],
        ['/usr/local/opt/python@3.12/bin/python3.12', []],
      ]
}

function managedPython(installIfMissing) {
  const home = homedir()
  const executable = isWin ? 'uv.exe' : 'uv'
  const candidates = [
    'uv', path.join(path.dirname(venvPython), executable),
    path.join(home, '.local', 'bin', executable), path.join(home, '.cargo', 'bin', executable),
    path.join(process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'), 'uv', 'bin', executable),
  ]
  let uv = candidates.find(command => spawnSync(command, ['--version'], { stdio: 'ignore', shell: false }).status === 0)
  let bootstrapDir
  try {
    if (!uv && installIfMissing) {
      console.log('Chuẩn bị uv để tải Python riêng cho tool... / Preparing uv to download Python for the tool...')
      bootstrapDir = mkdtempSync(path.join(tmpdir(), 'zm-python-setup-'))
      const env = { ...process.env, UV_UNMANAGED_INSTALL: bootstrapDir }
      if (isWin) {
        run('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
          "$ErrorActionPreference = 'Stop'; irm https://astral.sh/uv/0.12.15/install.ps1 | iex"], { env })
      } else {
        const download = spawnSync('curl', ['-fLsS', 'https://astral.sh/uv/0.12.15/install.sh'], { encoding: 'utf8', shell: false })
        if (download.status !== 0) {
          console.error(download.error?.message || download.stderr)
          process.exit(download.status ?? 1)
        }
        run('sh', [], { input: download.stdout, stdio: ['pipe', 'inherit', 'inherit'], env })
      }
      uv = path.join(bootstrapDir, executable)
    }
    if (!uv) return null
    if (installIfMissing) {
      console.log(`Đang chuẩn bị Python ${managedPythonVersion}... / Preparing Python ${managedPythonVersion}...`)
      run(uv, ['python', 'install', managedPythonVersion, '--no-bin', '--no-registry'])
    }
    const result = spawnSync(uv, ['python', 'find', managedPythonVersion, '--managed-python', '--no-python-downloads'], {
      encoding: 'utf8', shell: false,
    })
    const command = result.stdout?.trim()
    return result.status === 0 && command && compatible(pythonVersion(command)) ? { command, prefix: [] } : null
  } finally {
    if (bootstrapDir) rmSync(bootstrapDir, { recursive: true, force: true })
  }
}

function findPython(installIfMissing = true) {
  for (const [command, prefix] of pythonCandidates()) {
    if (compatible(pythonVersion(command, prefix))) return { command, prefix }
  }
  const managed = managedPython(installIfMissing)
  if (managed) return managed
  if (installIfMissing && process.platform === 'darwin' && spawnSync('brew', ['--version'], { stdio: 'ignore' }).status === 0) {
    console.log('Thiếu Python tương thích. Đang cài Python 3.12 bằng Homebrew...')
    run('brew', ['install', 'python@3.12'])
    return findPython(false)
  }
  if (installIfMissing && isWin && spawnSync('winget', ['--version'], { stdio: 'ignore' }).status === 0) {
    console.log('Thiếu Python tương thích. Đang cài Python 3.12 bằng winget...')
    run('winget', ['install', '--id', 'Python.Python.3.12', '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements'])
    return findPython(false)
  }
  console.error('Không tìm thấy Python tương thích. Cài uv (https://docs.astral.sh/uv/getting-started/installation/) rồi chạy lại bun run setup hoặc npm run setup. / No compatible Python found. Install uv and rerun bun run setup or npm run setup.')
  process.exit(1)
}

if (process.argv.includes('--check-python')) {
  const python = findPython(false)
  console.log(`${python.command} ${python.prefix.join(' ')}`.trim())
  process.exit(0)
}

console.log('Cài frontend dependencies...')
run(isWin ? process.env.ComSpec || 'cmd.exe' : 'npm', isWin ? ['/d', '/s', '/c', 'npm install'] : ['install'])

if (!existsSync(venvPython) || !compatible(pythonVersion(venvPython))) {
  const python = findPython()
  console.log(`${existsSync(venvPython) ? 'Tạo lại' : 'Tạo'} backend/.venv bằng Python tương thích...`)
  run(python.command, [...python.prefix, '-m', 'venv', '--clear', venvDir])
}

console.log('Cài backend dependencies...')
run(venvPython, ['-m', 'pip', 'install', '--upgrade', 'pip'])
run(venvPython, ['-m', 'pip', 'install', '-r', path.join(root, 'backend', 'requirements.txt')])

const detectedAccel = spawnSync(venvPython, [
  '-c',
  'import sys; sys.path.insert(0, "backend"); from pipeline.core.media import detect_device; print(detect_device().get("accel", "cpu"))',
], { cwd: root, encoding: 'utf8', shell: false }).stdout.trim()
if ((process.platform === 'win32' || process.platform === 'linux') && detectedAccel === 'cuda') {
  console.log('Phát hiện NVIDIA — cài Sherpa-ONNX CUDA 12 cho tách người nói...')
  run(venvPython, [
    '-m', 'pip', 'install', '--force-reinstall',
    'sherpa-onnx==1.13.5+cuda12.cudnn9',
    '-f', 'https://k2-fsa.github.io/sherpa/onnx/cuda.html',
  ])
}

console.log('Tải model tách người nói...')
run(venvPython, [
  '-c',
  'import sys; from pathlib import Path; sys.path.insert(0, "backend"); from pipeline.asr.speaker import ensure_diarization_models; ensure_diarization_models(Path("backend/data/models/pyannote"), log=print)',
],)

console.log('\nĐã cài xong. Chạy: npm run dev:all')
