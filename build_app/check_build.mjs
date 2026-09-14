/**
 * check_build.mjs — Kiểm tra build output sau khi PyInstaller xong.
 * Chạy: node build_app/check_build.mjs [version]
 */
import { existsSync, readdirSync, statSync, readFileSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const isWin = process.platform === 'win32'
const isMac = process.platform === 'darwin'
const releaseDir = path.join(root, 'build_app', 'release')

const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'))
const version = process.argv[2] || pkg.version?.match(/^\d+\.\d+\.\d+/)?.[0] || '0.0.0'
const APP_ARTIFACT_NAME = 'ZM_AI_TOOL'
const APP_EXECUTABLE_NAME = 'ZM AI TOOL'
const verName = `${APP_ARTIFACT_NAME}_v${version}`
// PyInstaller on macOS emits one .app bundle rather than the Windows/Linux
// onedir folder. Keep all later checks pointed at the equivalent bundle paths.
const distDir = path.join(releaseDir, isMac ? `${verName}.app` : verName)
const executableDir = isMac ? path.join(distDir, 'Contents', 'MacOS') : distDir
const resourceDir = isMac ? path.join(distDir, 'Contents', 'Resources') : distDir
const frameworkDir = isMac ? path.join(distDir, 'Contents', 'Frameworks') : resourceDir
const exePath = path.join(executableDir, isWin ? `${APP_EXECUTABLE_NAME}.exe` : APP_EXECUTABLE_NAME)

let ok = true
function check(label, pass, detail = '') {
  const icon = pass ? '✓' : '✗'
  console.log(`  ${icon} ${label}${detail ? ': ' + detail : ''}`)
  if (!pass) ok = false
}
function size(p) {
  try {
    const s = statSync(p).size
    if (s > 1024 * 1024) return `${(s / 1024 / 1024).toFixed(1)} MB`
    return `${(s / 1024).toFixed(0)} KB`
  } catch { return '?' }
}
function dirSize(dir) {
  if (!existsSync(dir)) return '?'
  let total = 0
  function walk(d) {
    for (const f of readdirSync(d, { withFileTypes: true })) {
      const fp = path.join(d, f.name)
      if (f.isDirectory()) walk(fp)
      else try { total += statSync(fp).size } catch {}
    }
  }
  walk(dir)
  return `${(total / 1024 / 1024).toFixed(1)} MB`
}

console.log(`\nZM AI TOOL Build Check — v${version}`)
console.log(`Release: ${distDir}\n`)

// 1. Thư mục release tồn tại
check('Release dir exists', existsSync(distDir))

// 2. EXE chính
check(`${APP_EXECUTABLE_NAME}${isWin ? '.exe' : ''}`, existsSync(exePath), size(exePath))

// 3. dist/index.html (frontend build đã được pack)
const internalDir = isMac
  ? resourceDir
  : existsSync(path.join(distDir, 'app'))
    ? path.join(distDir, 'app')
    : existsSync(path.join(distDir, '_internal')) ? path.join(distDir, '_internal') : distDir

if (isWin) {
  check('portable app/ payload', existsSync(path.join(distDir, 'app')))
  check('portable package has no Setup marker', !existsSync(path.join(distDir, '.zmaio-installed')))
  check(
    'portable package excludes mutable state',
    !['data', 'output', 'tmp', '.venv-runtime', '.venv-ocr', 'resources', 'updates', 'app.log', 'last_crash.txt']
      .some((name) => existsSync(path.join(distDir, name))),
  )
}

// 3. dist/index.html (frontend build đã được pack)
const distIndex = path.join(internalDir, 'dist', 'index.html')
check('dist/index.html', existsSync(distIndex))
check(
  'bundled caption font',
  existsSync(path.join(internalDir, 'dist', 'fonts', 'NotoSans-Bold.ttf')),
)

function checkTool(label, bin, expect) {
  const exists = existsSync(bin)
  check(`${label} bundled`, exists, size(bin))
  if (!exists) return
  const bytes = statSync(bin).size
  if (isWin) check(`${label} not a shim`, bytes >= 2_000_000, size(bin))
  const r = spawnSync(bin, ['-version'], { encoding: 'utf8', timeout: 8000 })
  const out = `${r.stdout || ''}${r.stderr || ''}`
  const line = out.split(/\r?\n/).find(Boolean) || `exit ${r.status}`
  check(`${label} -version`, r.status === 0 && out.toLowerCase().includes(expect), line.slice(0, 90))
}

// 4. ffmpeg / 5. ffprobe — phải là binary thật, không phải Chocolatey ShimGen
checkTool('ffmpeg', path.join(isMac ? frameworkDir : internalDir, isWin ? 'ffmpeg.exe' : 'ffmpeg'), 'ffmpeg')
checkTool('ffprobe', path.join(isMac ? frameworkDir : internalDir, isWin ? 'ffprobe.exe' : 'ffprobe'), 'ffprobe')

// 6. uv
const uv = path.join(isMac ? frameworkDir : internalDir, isWin ? 'uv.exe' : 'uv')
check('uv bundled', existsSync(uv), size(uv))
const ytdlpReport = path.join(releaseDir, `.yt-dlp-version-${process.pid}.txt`)
const ytdlp = isWin
  ? spawnSync(exePath, ['--yt-dlp-version-check', ytdlpReport], { encoding: 'utf8', timeout: 8000 })
  : spawnSync(exePath, ['--yt-dlp-cli', '--version'], { encoding: 'utf8', timeout: 8000 })
const ytdlpOutput = isWin && existsSync(ytdlpReport)
  ? readFileSync(ytdlpReport, 'utf8').trim()
  : `${ytdlp.stdout || ''}${ytdlp.stderr || ''}`.trim()
if (existsSync(ytdlpReport)) rmSync(ytdlpReport, { force: true })
check('yt-dlp embedded CLI', ytdlp.status === 0 && /^\d{4}\.\d{1,2}\.\d{1,2}/m.test(ytdlpOutput), ytdlpOutput.split(/\r?\n/)[0] || `exit ${ytdlp.status}`)
check(
  'embedded Python runtime DLL',
  !isWin || existsSync(path.join(internalDir, 'python312.dll')),
  isWin ? size(path.join(internalDir, 'python312.dll')) : '',
)

// 7. pipeline directory (app logic)
const pipeDir = path.join(internalDir, 'pipeline')
check('pipeline/ dir', existsSync(pipeDir))

// 8. resources/voice-ref
const voiceRef = path.join(internalDir, 'resources', 'voice-ref')
check('voice-ref', existsSync(voiceRef))

// Drawing streaming renderer — source moved to pipeline/drawing/assets/, bundled to references/ dest path
const drawingReference = path.join(internalDir, 'references', 'whiteboard-stream-animation')
check(
  'drawing stream renderer',
  existsSync(path.join(drawingReference, 'scripts', 'stream_render.py')),
)
check(
  'drawing hand asset',
  existsSync(path.join(drawingReference, 'assets', 'drawing-hand.png')),
)

// 9. VERSION file
const versionFile = path.join(internalDir, 'VERSION')
check('VERSION file', existsSync(versionFile),
  existsSync(versionFile) ? readFileSync(versionFile, 'utf8').trim() : '')

// 10. ZIP archive (Portable)
const platform = isWin ? 'windows' : process.platform === 'darwin' ? 'macos' : 'linux'
const portableZip = path.join(releaseDir, `${verName}-windows-x64-Portable.zip`)
const legacyZip = path.join(releaseDir, `${verName}-${platform}-${process.arch}.zip`)
const zipPath = isWin && existsSync(portableZip) ? portableZip : legacyZip
check(isWin ? 'ZIP archive (Portable)' : 'ZIP archive', existsSync(zipPath), size(zipPath))

// 11. Inno Setup installer (Windows optional / CI)
if (isWin) {
  const setupExe = path.join(releaseDir, `${verName}-windows-x64-Setup.exe`)
  if (existsSync(setupExe)) {
    check('Inno Setup installer (.exe)', true, size(setupExe))
  }
}

// Summary
console.log(`\nTổng kích thước: ${dirSize(distDir)}`)
if (ok) {
  console.log('\n✓ Build OK\n')
  process.exit(0)
} else {
  console.error('\n✗ Build có vấn đề — xem các mục ✗ trên\n')
  process.exit(1)
}
