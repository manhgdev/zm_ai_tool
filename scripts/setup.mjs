import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const isWin = process.platform === 'win32'
const venvDir = path.join(root, 'backend', '.venv')
const venvPython = path.join(root, 'backend', '.venv', isWin ? 'Scripts/python.exe' : 'bin/python')

function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit', shell: false })
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
  return isWin
    ? [
        ['py', ['-3.12']], ['py', ['-3.11']], ['py', ['-3.10']],
        ['python3.12', []], ['python3.11', []], ['python3.10', []], ['python', []],
      ]
    : [
        ['python3.12', []], ['python3.11', []], ['python3.10', []], ['python3', []], ['python', []],
      ]
}

function findPython(installIfMissing = true) {
  for (const [command, prefix] of pythonCandidates()) {
    if (compatible(pythonVersion(command, prefix))) return { command, prefix }
  }
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
  console.error('Không tìm thấy Python 3.10–3.12. Hãy cài Python 3.12 rồi chạy lại npm run setup.')
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
