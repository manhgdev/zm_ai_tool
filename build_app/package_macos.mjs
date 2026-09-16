/**
 * package_macos.mjs — codesign + pkgbuild for the .app produced by build.mjs.
 * Shared by local `npm run build:release:macos` and .github/workflows/release-macos.yml.
 *
 * App bundle: "ZM AI TOOL.app" (no version in name).
 * PKG file:   ZM_AI_TOOL_v{version}-macos-{arch}.pkg (version for GitHub Release / updater).
 */
import { spawnSync } from 'node:child_process'
import { appendFileSync, existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const releaseDir = process.env.ZM_AI_TOOL_BUILD_RELEASE_DIR || path.join(root, 'build_app', 'release')
const APP_DISPLAY_NAME = 'ZM AI TOOL'
const APP_ARTIFACT_NAME = 'ZM_AI_TOOL'

function fail(message) {
  console.error(message)
  process.exit(1)
}

function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit', shell: false })
  if (result.status !== 0) process.exit(result.status ?? 1)
}

function runCapture(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', shell: false })
  if (result.status !== 0) {
    fail((result.stderr || result.stdout || `${command} failed`).trim())
  }
  return (result.stdout || '').trim()
}

if (process.platform !== 'darwin') {
  fail('package_macos.mjs chỉ chạy trên macOS.')
}

if (!existsSync(releaseDir)) {
  fail(`Không tìm thấy thư mục release: ${releaseDir}`)
}

const app = path.join(releaseDir, `${APP_DISPLAY_NAME}.app`)
if (!existsSync(app)) {
  fail(`Không tìm thấy ${APP_DISPLAY_NAME}.app`)
}

const plist = path.join(app, 'Contents', 'Info.plist')
const iconName = runCapture('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleIconFile', plist])
if (!iconName) {
  fail('Info.plist thiếu CFBundleIconFile')
}
const iconPath = path.join(app, 'Contents', 'Resources', iconName)
if (!existsSync(iconPath)) {
  fail(`Không tìm thấy app icon: ${iconPath}`)
}

let version = runCapture('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleShortVersionString', plist])
if (!/^\d+\.\d+\.\d+/.test(version)) {
  const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'))
  version = String(pkg.version || '0.0.0').replace(/^v/, '')
}
version = version.match(/^\d+\.\d+\.\d+/)?.[0] || version

const arch = runCapture('uname', ['-m'])
const pkg = path.join(releaseDir, `${APP_ARTIFACT_NAME}_v${version}-macos-${arch}.pkg`)

run('/usr/bin/codesign', ['--force', '--deep', '--sign', '-', app])
run('pkgbuild', ['--component', app, '--install-location', '/Applications', pkg])

console.log(`PKG: ${pkg}`)

const githubOutput = process.env.GITHUB_OUTPUT
if (githubOutput) {
  appendFileSync(githubOutput, `app=${app}\npkg=${pkg}\narch=${arch}\n`)
}
