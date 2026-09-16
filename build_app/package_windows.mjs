/**
 * package_windows.mjs — Portable.zip + Inno Setup.exe (same outputs as release-windows.yml).
 * Called from build.mjs on Windows and from GitHub Actions.
 */
import { appendFileSync, existsSync, readdirSync, statSync, createWriteStream } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { buildInstaller } from './build_installer.mjs'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const releaseDir = process.env.ZM_AI_TOOL_BUILD_RELEASE_DIR || path.join(root, 'build_app', 'release')
const APP_ARTIFACT_NAME = 'ZM_AI_TOOL'

function fail(message) {
  console.error(message)
  process.exit(1)
}

if (process.platform !== 'win32') {
  fail('package_windows.mjs chỉ chạy trên Windows.')
}

if (!existsSync(releaseDir)) {
  fail(`Không tìm thấy thư mục release: ${releaseDir}`)
}

const dirs = readdirSync(releaseDir)
  .filter((name) => name.startsWith(`${APP_ARTIFACT_NAME}_v`))
  .map((name) => path.join(releaseDir, name))
  .filter((p) => {
    try {
      return statSync(p).isDirectory() && existsSync(path.join(p, 'ZM AI TOOL.exe'))
    } catch {
      return false
    }
  })
  .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs)

const packageDir = dirs[0]
if (!packageDir) {
  fail(`Không tìm thấy thư mục ${APP_ARTIFACT_NAME}_v* với ZM AI TOOL.exe`)
}

const verName = path.basename(packageDir)
const portableZip = path.join(releaseDir, `${verName}-windows-x64-Portable.zip`)
const setupExe = path.join(releaseDir, `${verName}-windows-x64-Setup.exe`)

if (!existsSync(portableZip)) {
  console.log(`Đang tạo Portable ZIP: ${portableZip}`)
  const { default: archiver } = await import('archiver')
  await new Promise((resolve, reject) => {
    const output = createWriteStream(portableZip)
    const archive = archiver('zip', { zlib: { level: 6 } })
    output.on('close', resolve)
    output.on('error', reject)
    archive.on('error', reject)
    archive.pipe(output)
    archive.directory(packageDir, false)
    archive.finalize()
  })
}

const version = verName.replace(new RegExp(`^${APP_ARTIFACT_NAME}_v`), '')
const result = buildInstaller(version)
if (!result.ok || !existsSync(setupExe)) {
  fail(result.error || `Không tìm thấy Setup: ${setupExe}`)
}

console.log(`Portable: ${portableZip}`)
console.log(`Setup: ${setupExe}`)

const githubOutput = process.env.GITHUB_OUTPUT
if (githubOutput) {
  appendFileSync(
    githubOutput,
    `directory=${packageDir}\nzip=${portableZip}\ninstaller=${setupExe}\n`,
  )
}
