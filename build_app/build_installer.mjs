/**
 * build_installer.mjs — Đóng gói bộ cài Windows (.exe) bằng Inno Setup 6.
 * Chạy: node build_app/build_installer.mjs [version]
 */
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const isWin = process.platform === 'win32'

function findISCC() {
  if (!isWin) return null

  // 1. Kiểm tra PATH
  const found = spawnSync('where.exe', ['ISCC.exe'], { encoding: 'utf8', shell: false })
  if (found.status === 0) {
    const p = found.stdout.trim().split(/\r?\n/)[0]?.trim()
    if (p && existsSync(p)) return p
  }

  // 2. Các đường dẫn cài đặt thông dụng trên Windows
  const candidates = [
    'C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe',
    'C:\\Program Files\\Inno Setup 6\\ISCC.exe',
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Inno Setup 6', 'ISCC.exe'),
    path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Inno Setup 6', 'ISCC.exe'),
    path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Inno Setup 6', 'ISCC.exe'),
    'C:\\ProgramData\\chocolatey\\bin\\ISCC.exe',
  ]

  for (const c of candidates) {
    if (c && existsSync(c)) return c
  }
  return null
}

export function buildInstaller(customVersion) {
  const releaseDir = process.env.ZM_AI_TOOL_BUILD_RELEASE_DIR || path.join(root, 'build_app', 'release')
  const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'))

  const version = pkg.version
  if (!/^\d+\.\d+\.\d+$/.test(version || '')) throw new Error('Invalid package.json version')
  if (customVersion && customVersion !== version) throw new Error('Build version differs from package.json; rebuild the app')

  const verName = `ZM_AI_TOOL_v${version}`
  const sourceDir = path.join(releaseDir, verName)
  const mainExe = path.join(sourceDir, 'ZM AI TOOL.exe')

  if (!existsSync(mainExe)) {
    console.error(`\n[Inno Setup] Chưa có thư mục build: ${sourceDir}`)
    console.error('Hãy chạy `npm run build:app` trước khi tạo bộ cài đặt.')
    return { ok: false, error: 'Source directory not found' }
  }

  const iscc = findISCC()
  if (!iscc) {
    console.warn('\n[Inno Setup] Không tìm thấy ISCC.exe (Inno Setup 6).')
    console.warn('Cài đặt qua Chocolatey: choco install innosetup -y')
    console.warn('Hoặc tải từ: https://jrsoftware.org/isdl.php')
    return { ok: false, error: 'ISCC not found' }
  }

  console.log(`\n[Inno Setup] Đang biên dịch bộ cài đặt Windows cho v${version}...`)
  console.log(`Compiler: ${iscc}`)
  console.log(`Source: ${sourceDir}`)

  const issPath = path.join(root, 'build_app', 'installer.iss')
  const outputBase = `${verName}-windows-x64-Setup`

  const args = [
    `/DMyAppVersion=${version}`,
    `/DMyAppSourceDir=${sourceDir}`,
    `/DMyAppOutputDir=${releaseDir}`,
    `/DMyAppOutputBaseFilename=${outputBase}`,
    issPath,
  ]

  const result = spawnSync(iscc, args, {
    cwd: path.join(root, 'build_app'),
    stdio: 'inherit',
    shell: false,
  })

  if (result.status !== 0) {
    console.error(`\n[Inno Setup] Biên dịch thất bại với mã lỗi ${result.status}.`)
    return { ok: false, error: `ISCC exit ${result.status}` }
  }

  const setupExe = path.join(releaseDir, `${outputBase}.exe`)
  if (existsSync(setupExe)) {
    const sizeMb = (statSync(setupExe).size / 1024 / 1024).toFixed(1)
    console.log(`\n✓ Bộ cài Windows đã tạo thành công:`)
    console.log(`  File: ${setupExe} (${sizeMb} MB)`)
    return { ok: true, file: setupExe }
  }

  return { ok: false, error: 'Output setup exe not found after compilation' }
}

// Chạy trực tiếp qua CLI
const isDirectRun = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])
if (isDirectRun) {
  if (!isWin) {
    console.log('[Inno Setup] Trình biên dịch Inno Setup chỉ khả dụng trên Windows.')
    process.exit(0)
  }
  const res = buildInstaller(process.argv[2])
  if (!res.ok) process.exit(1)
}
