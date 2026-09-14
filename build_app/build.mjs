import { spawnSync } from 'node:child_process'
import { createWriteStream, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const isWin = process.platform === 'win32'
const isMac = process.platform === 'darwin'
const python = path.join(root, 'backend', '.venv', isWin ? 'Scripts/python.exe' : 'bin/python')
const dataSep = isWin ? ';' : ':'
const packageJsonPath = path.join(root, 'package.json')
const workDir = path.join(root, 'build_app', '.work')
const versionFilePath = path.join(workDir, 'VERSION')
const releaseVersionFilePath = path.join(root, 'build_app', 'VERSION')
// onedir = nhanh (Windows mặc định). ONEFILE=1 để gói 1 file (chậm vì bước PKG).
const oneFile = process.env.ONEFILE === '1' || process.env.ONEFILE === 'true'
const clean = process.env.CLEAN === '1' || process.env.CLEAN === 'true'
const skipArchive = process.env.SKIP_ARCHIVE === '1' || process.env.SKIP_ARCHIVE === 'true'
const npmCommand = isWin ? process.env.ComSpec || 'cmd.exe' : 'npm'
const APP_DISPLAY_NAME = 'ZM AIO TOOL'
const APP_ARTIFACT_NAME = 'ZM_AIO_TOOL'
const APP_EXECUTABLE_NAME = APP_DISPLAY_NAME
// Useful for a local smoke build while a previous root-owned release bundle is
// still present. CI leaves it unset and uses the normal release folder.
const releaseDir = process.env.ZM_AI_TOOL_BUILD_RELEASE_DIR || path.join(root, 'build_app', 'release')

function npmArgs(...args) {
  return isWin ? ['/d', '/s', '/c', `npm ${args.join(' ')}`] : args
}

function run(command, args, extraEnv = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    stdio: 'inherit',
    shell: false,
    env: { ...process.env, ...extraEnv },
  })
  if (result.status !== 0) process.exit(result.status ?? 1)
}

function cleanFrontendDist() {
  const dist = path.join(root, 'frontend', 'dist')
  if (!existsSync(dist)) return
  try {
    // Windows Defender/WebView can hold generated assets briefly after APP closes.
    rmSync(dist, { recursive: true, force: true, maxRetries: 12, retryDelay: 250 })
  } catch (error) {
    const stale = path.join(root, 'frontend', `.dist-stale-${process.pid}-${Date.now()}`)
    try {
      renameSync(dist, stale)
      console.warn(`frontend/dist đang bị Windows giữ khóa — đã chuyển bản cũ sang ${path.basename(stale)}.`)
    } catch {
      console.error('Không thể dọn frontend/dist. Hãy đóng cửa sổ ZM AIO TOOL/Preview rồi chạy build lại.')
      console.error(error instanceof Error ? error.message : error)
      process.exit(1)
    }
  }
}

function pyOk(code) {
  const r = spawnSync(python, ['-c', code], { encoding: 'utf8', shell: false })
  return r.status === 0
}

function ensurePip(pkgs) {
  const missing = pkgs.filter((p) => {
    const mod = p === 'pywebview' ? 'webview' : p.replace(/-/g, '_')
    return !pyOk(`import ${mod}`)
  })
  if (missing.length) {
    run(python, ['-m', 'pip', 'install', '--upgrade', ...missing])
  }
}

function readPackage() {
  return JSON.parse(readFileSync(packageJsonPath, 'utf8'))
}

function parseSemver(v) {
  const m = String(v || '').trim().match(/^(\d+)\.(\d+)\.(\d+)/)
  if (!m) return { major: 1, minor: 0, patch: 0 }
  return { major: Number(m[1]), minor: Number(m[2]), patch: Number(m[3]) }
}

function formatSemver({ major, minor, patch }) {
  return `${major}.${minor}.${patch}`
}

const FF_MIN_BYTES = 2_000_000 // Chocolatey ShimGen ~400KB; Gyan/full là chục–trăm MB.

function findRealToolFile(dir, filename, depth = 0) {
  if (depth > 3 || !existsSync(dir)) return ''
  try {
    const direct = path.join(dir, filename)
    if (existsSync(direct) && statSync(direct).size >= FF_MIN_BYTES) return direct
    for (const ent of readdirSync(dir, { withFileTypes: true })) {
      if (!ent.isDirectory()) continue
      const hit = findRealToolFile(path.join(dir, ent.name), filename, depth + 1)
      if (hit) return hit
    }
  } catch {
    return ''
  }
  return ''
}

function resolveMediaTool(tool, { nextTo } = {}) {
  const exe = isWin ? `${tool}.exe` : tool
  const found = spawnSync(isWin ? 'where.exe' : 'which', [tool], { encoding: 'utf8', shell: false })
  const candidates = found.status === 0
    ? [...new Set(found.stdout.split(/\r?\n/).map((s) => s.trim()).filter(Boolean))]
    : []
  // Chocolatey: ffprobe.exe nằm cạnh ffmpeg.exe trong gói `ffmpeg`, không có gói `ffprobe`.
  if (nextTo) {
    const sibling = path.join(path.dirname(nextTo), exe)
    if (existsSync(sibling)) candidates.unshift(sibling)
  }
  const resolved = []
  for (const c of candidates) {
    if (!c || !existsSync(c)) continue
    let target = c
    try {
      // Chỉ Windows: ShimGen ~400KB. Homebrew ffmpeg trên macOS cũng nhỏ (dylibs rời) — không bỏ.
      if (isWin && statSync(c).size < FF_MIN_BYTES) {
        const dirs = [
          path.resolve(path.dirname(c), '..', 'lib', tool, 'tools'),
          path.resolve(path.dirname(c), '..', 'lib', 'ffmpeg', 'tools'),
        ]
        let hit = ''
        for (const dir of dirs) {
          hit = findRealToolFile(dir, exe)
          if (hit) break
        }
        if (!hit) continue
        target = hit
      }
      if (!isWin || statSync(target).size >= FF_MIN_BYTES) resolved.push(target)
    } catch {
      continue
    }
  }
  resolved.sort((a, b) => statSync(b).size - statSync(a).size)
  return resolved[0] || ''
}

if (findRealToolFile(path.join(root, '__no_such_ffmpeg_tools__'), isWin ? 'ffmpeg.exe' : 'ffmpeg') !== '') {
  console.error('findRealToolFile false positive')
  process.exit(1)
}

if (!existsSync(python)) {
  console.error('Thiếu backend/.venv. Chạy npm run setup trước.')
  process.exit(1)
}

const pkg = readPackage()
// CI writes build_app/VERSION from the release tag. Local builds retain the
// package version so a stale release file cannot rename a developer build.
const ciReleaseVersion = process.env.CI && existsSync(releaseVersionFilePath)
  ? readFileSync(releaseVersionFilePath, "utf8").trim()
  : ''
const appVersion = formatSemver(parseSemver(ciReleaseVersion || pkg.version || '1.0.0'))
if (!existsSync(workDir)) mkdirSync(workDir, { recursive: true })
writeFileSync(versionFilePath, `${appVersion}\n`, 'utf8')
console.log(`Building ${APP_DISPLAY_NAME} v${appVersion} (${oneFile ? 'onefile' : 'onedir'}${clean ? ', clean' : ''})`)

if (
  !existsSync(path.join(root, 'node_modules', '.bin', isWin ? 'tsc.cmd' : 'tsc')) ||
  !existsSync(path.join(root, 'node_modules', 'archiver'))
) {
  console.log('Thiếu dependency frontend — đang cài đặt...')
  run(npmCommand, npmArgs('install', '--no-package-lock'))
}
cleanFrontendDist()
run(npmCommand, npmArgs('run', 'build'))

// Pre-build validation
const distIndex = path.join(root, 'frontend', 'dist', 'index.html')
if (!existsSync(distIndex)) {
  console.error('Thiếu frontend/dist/index.html — chạy npm run build trước.')
  process.exit(1)
}

const ffmpegCheck = resolveMediaTool('ffmpeg')
if (!ffmpegCheck) {
  console.warn('Cảnh báo: ffmpeg thật không tìm thấy trên PATH (bỏ qua Chocolatey shim).')
}

// Chỉ cài khi thiếu — không reinstall mỗi lần
ensurePip(['pyinstaller', 'uv', 'pywebview', 'yt-dlp'])

const iconIco = path.join(root, 'build_app', 'app.ico')
const iconIcns = path.join(root, 'build_app', 'app.icns')
if (isMac && !existsSync(iconIcns)) {
  console.error(`Thiếu icon macOS bắt buộc: ${iconIcns}`)
  process.exit(1)
}

const args = [
  '-m', 'PyInstaller',
  '--noconfirm',
  ...(clean ? ['--clean'] : []),
  ...(oneFile ? ['--onefile'] : ['--onedir']),
  // Portable root stays stable: updater replaces only EXE + app/, never user state.
  ...(isWin && !oneFile ? ['--contents-directory', 'app'] : []),
  '--name', APP_EXECUTABLE_NAME,
  '--distpath', releaseDir,
  '--workpath', path.join(root, 'build_app', '.work'),
  '--specpath', path.join(root, 'build_app'),
  '--paths', path.join(root, 'backend'),
  // ── Cross-platform data ────────────────────────────────────────────────────
  '--add-data', `${path.join(root, 'frontend', 'dist')}${dataSep}dist`,
  '--add-data', `${path.join(root, 'backend', 'pipeline')}${dataSep}pipeline`,
  '--add-data', `${path.join(root, 'backend', 'pipeline', 'drawing', 'assets', 'scripts', 'stream_render.py')}${dataSep}references/whiteboard-stream-animation/scripts`,
  '--add-data', `${path.join(root, 'backend', 'pipeline', 'drawing', 'assets', 'images', 'drawing-hand.png')}${dataSep}references/whiteboard-stream-animation/assets`,
  '--add-data', `${path.join(root, 'backend', 'resources', 'voice-ref')}${dataSep}resources/voice-ref`,
  '--add-data', `${path.join(root, 'previews', 'v1.0-base-vietnam-2D-image.txt')}${dataSep}previews`,
  '--add-data', `${path.join(root, 'previews', 'v1.0-base-english-2D-image.txt')}${dataSep}previews`,
  '--add-data', `${versionFilePath}${dataSep}.`,
  '--collect-all', 'webview',
  '--collect-all', 'yt_dlp',
  '--collect-all', 'playwright',
  // Hidden imports: stdlib + third-party hay bị PyInstaller miss
  '--hidden-import', 'timeit',
  '--hidden-import', 'pickletools',
  '--hidden-import', 'filecmp',
  '--hidden-import', 'multiprocessing.synchronize',
  '--hidden-import', 'multiprocessing.pool',
  '--hidden-import', 'email.mime.text',
  '--hidden-import', 'email.mime.multipart',
  '--hidden-import', 'email.mime.base',
  '--hidden-import', 'email.encoders',
  '--hidden-import', 'httpx',
  '--hidden-import', 'setuptools',
  '--hidden-import', 'playwright',
  '--hidden-import', 'playwright.async_api',
  '--hidden-import', 'playwright.sync_api',
]

// Các gói AI được cài vào .venv-runtime trong thư mục Portable ở lần mở đầu tiên.
// Exclude thêm dev-only packages để giảm kích thước bundle.
for (const mod of [
  'faster_whisper', 'ctranslate2', 'tokenizers', 'huggingface_hub',
  'rapidocr_onnxruntime', 'onnxruntime', 'cv2', 'PIL', 'numpy',
  'torch', 'torchaudio', 'transformers', 'datasets', 'accelerate',
  'pandas', 'scipy', 'sklearn', 'tensorflow', 'soundfile', 'librosa',
  'pytest',
  'lxml', 'pyarrow', 'matplotlib', 'sympy', 'numba', 'llvmlite',
  'vieneu', 'sea_g2p', 'soxr',
  'webview.platforms.android', 'pycparser.lextab', 'pycparser.yacctab',
  'IPython', 'ipykernel', 'notebook', 'jupyterlab',
  'tzdata',  // zoneinfo hook requests this; Windows uses OS timezone data natively
  'secretstorage',  // Linux-only keyring backend pulled by yt_dlp on non-Linux
]) args.push('--exclude-module', mod)

// ── Icon ──────────────────────────────────────────────────────────────────────
if (isWin && existsSync(iconIco)) {
  args.push('--icon', iconIco)
  args.push('--add-data', `${iconIco}${dataSep}.`)  // dùng trong webview tray
} else if (isMac && existsSync(iconIcns)) {
  args.push('--icon', iconIcns)
}

// ── Windowed (ẩn terminal) ────────────────────────────────────────────────────
if (isWin || isMac) args.push('--windowed')

// ── WINDOWS-SPECIFIC ─────────────────────────────────────────────────────────
if (isWin) {
  // pythonnet / clr_loader — Windows COM interop (không có trên macOS/Linux)
  const sitePackages = spawnSync(python, ['-c', 'import site; print(site.getsitepackages()[-1])'], {
    encoding: 'utf8', shell: false,
  }).stdout.trim()
  args.push(
    '--additional-hooks-dir', path.join(sitePackages, 'pythonnet', '_pyinstaller'),
    '--collect-all', 'pythonnet',
    '--collect-all', 'clr_loader',
    '--hidden-import', 'clr',
  )

  // python3.dll (stable ABI) — cần cho cv2.pyd trong .venv-runtime.
  // PyInstaller chỉ bundle python312.dll; cv2.pyd link với python3.dll.
  // _internal/ đã được bootloader đăng ký add_dll_directory → cv2 tìm thấy.
  // Trên macOS, dylib được resolve qua @rpath → không cần bước này.
  const whereResult = spawnSync('where.exe', ['python3.dll'], { encoding: 'utf8', shell: false })
  let py3dll = whereResult.status === 0 ? whereResult.stdout.trim().split(/\r?\n/)[0].trim() : ''
  if (!py3dll || !existsSync(py3dll)) {
    const basePy = spawnSync(python, [
      '-c', 'import sys, os; exe=getattr(sys,"_base_executable",sys.executable); print(os.path.join(os.path.dirname(exe),"python3.dll"))'
    ], { encoding: 'utf8', shell: false }).stdout.trim()
    if (basePy && existsSync(basePy)) py3dll = basePy
  }
  if (py3dll && existsSync(py3dll)) {
    args.push('--add-binary', `${py3dll}${dataSep}.`)
    console.log(`[win] Bundling python3.dll: ${py3dll}`)
  } else {
    console.warn('[win] Cảnh báo: không tìm thấy python3.dll — cv2.pyd có thể fail trong APP.')
  }
}

// ── MACOS-SPECIFIC ───────────────────────────────────────────────────────────
if (isMac) {
  // Bundle identifier cho .app (macOS yêu cầu reverse-DNS)
  args.push('--osx-bundle-identifier', 'com.zmaio.tool')
  // Universal2: build cho cả Apple Silicon + Intel (nếu cross-compile được)
  // Bỏ comment dòng dưới nếu build trên máy hỗ trợ universal2:
  // args.push('--target-arch', 'universal2')
}

// ── Cross-platform: uv + ffmpeg/ffprobe ──────────────────────────────────────
const uv = path.join(path.dirname(python), isWin ? 'uv.exe' : 'uv')
if (!existsSync(uv)) {
  console.error(`Không tìm thấy uv: ${uv}`)
  process.exit(1)
}
args.push('--add-binary', `${uv}${dataSep}.`)
args.push('--upx-exclude', isWin ? 'ffmpeg.exe' : 'ffmpeg')
args.push('--upx-exclude', isWin ? 'ffprobe.exe' : 'ffprobe')

let bundledFfmpeg = ''
for (const tool of ['ffmpeg', 'ffprobe']) {
  const binary = resolveMediaTool(tool, { nextTo: bundledFfmpeg })
  if (binary && tool === 'ffmpeg') bundledFfmpeg = binary
  if (binary) {
    console.log(`[bundle] ${tool}: ${binary} (${(statSync(binary).size / 1024 / 1024).toFixed(1)} MB)`)
    args.push('--add-binary', `${binary}${dataSep}.`)
  } else {
    console.error(`Không tìm thấy ${tool} thật trên PATH (bỏ qua Chocolatey ShimGen ~400KB).`)
    process.exit(1)
  }
}

args.push(path.join(root, 'build_app', 'launcher.py'))
const buildHome = path.join(root, 'build_app', '.build-home')
run(python, args, {
  ZM_AI_TOOL_HOME: buildHome,
  ZM_AI_TOOL_DATA: path.join(buildHome, 'data'),
  ZM_AI_TOOL_PUBLIC_DATA: path.join(buildHome, 'public_data'),
})

const verName = `${APP_ARTIFACT_NAME}_v${appVersion}`
let output
let packageTarget = ''
if (oneFile || isMac) {
  const built = path.join(releaseDir, isWin ? `${APP_EXECUTABLE_NAME}.exe` : isMac ? `${APP_EXECUTABLE_NAME}.app` : APP_EXECUTABLE_NAME)
  output = path.join(releaseDir, isWin ? `${verName}.exe` : isMac ? `${verName}.app` : verName)
  if (existsSync(output)) rmSync(output, { recursive: true, force: true })
  if (existsSync(built)) renameSync(built, output)
  packageTarget = output
} else {
  const builtDir = path.join(releaseDir, APP_EXECUTABLE_NAME)
  const outDir = path.join(releaseDir, verName)
  if (existsSync(outDir)) rmSync(outDir, { recursive: true, force: true })
  if (existsSync(builtDir)) renameSync(builtDir, outDir)
  packageTarget = outDir
  output = path.join(outDir, isWin ? `${APP_EXECUTABLE_NAME}.exe` : APP_EXECUTABLE_NAME)
}

if (isMac && packageTarget) {
  const info = path.join(packageTarget, 'Contents', 'Info.plist')
  for (const [key, value] of [
    ['CFBundleDisplayName', APP_DISPLAY_NAME],
    ['CFBundleName', APP_DISPLAY_NAME],
    ['CFBundleShortVersionString', appVersion],
    ['CFBundleVersion', appVersion],
  ]) {
    let result = spawnSync('/usr/libexec/PlistBuddy', ['-c', `Set :${key} ${value}`, info], { encoding: 'utf8' })
    if (result.status !== 0) {
      result = spawnSync('/usr/libexec/PlistBuddy', ['-c', `Add :${key} string ${value}`, info], { encoding: 'utf8' })
    }
    if (result.status !== 0) {
      console.error(`Không thể đặt ${key} cho bundle macOS: ${result.stderr || result.stdout}`)
      process.exit(1)
    }
  }
}

if (packageTarget && !skipArchive) {
  const { default: archiver } = await import('archiver')
  const platform = isWin ? 'windows' : isMac ? 'macos' : 'linux'
  const archivePath = path.join(
    releaseDir,
    isWin
      ? `${verName}-windows-x64-Portable.zip`
      : `${verName}-${platform}-${process.arch}.zip`
  )
  if (existsSync(archivePath)) rmSync(archivePath, { force: true })
  await new Promise((resolve, reject) => {
    const output = createWriteStream(archivePath)
    const archive = archiver('zip', { zlib: { level: 6 } })  // level 6: ~85% của 9 nhưng 3x nhanh
    output.on('close', resolve)
    output.on('error', reject)
    archive.on('error', reject)
    archive.pipe(output)
    if (statSync(packageTarget).isDirectory()) archive.directory(packageTarget, (oneFile || isMac) ? path.basename(packageTarget) : false)
    else archive.file(packageTarget, { name: path.basename(packageTarget) })
    archive.finalize()
  })
  console.log(`Bản ZIP Portable: ${archivePath}`)
}

if (isWin && !oneFile && process.env.BUILD_INSTALLER !== '0') {
  try {
    const { buildInstaller } = await import('./build_installer.mjs')
    buildInstaller(appVersion)
  } catch (err) {
    console.warn(`[build] Không thể tự động chạy Inno Setup: ${err?.message || err}`)
  }
}

console.log(`\nBuild hoàn tất: ${output}`)
console.log(`Version: v${appVersion}`)
if (isWin && !oneFile) {
  console.log(`- Bản Portable (không cài đặt): release/${verName}/ hoặc ${verName}-windows-x64-Portable.zip`)
  console.log(`- Bản Cài đặt (Inno Setup): release/${verName}-windows-x64-Setup.exe (hoặc chạy: npm run build:installer)`)
} else if (!oneFile) {
  console.log(`Chạy cả thư mục release/${verName}/ (không copy riêng .exe).`)
}
