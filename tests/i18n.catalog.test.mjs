import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('technical status is localized without translating provider names', () => {
  const labels = readFileSync(new URL('../frontend/src/features/configuration/setupLabels.ts', import.meta.url), 'utf8')
  const modal = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  assert.match(labels, /case 'installed_auto'/)
  assert.match(labels, /Đã cài · tự động', 'Installed · automatic'/)
  assert.match(labels, /item.detailValue/)
  assert.match(modal, /setupDetail\(locale, it\)/)
})

test('setup cards use stable IDs instead of backend display strings', () => {
  const source = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  assert.match(source, /setupLabel\(locale, it.id, 'name'\)/)
  assert.match(source, /setupLabel\(locale, it.id, 'hint'\)/)
  assert.doesNotMatch(source, /it.installLabel \|\|/)
  assert.doesNotMatch(source, /systemCheckText/)
  assert.match(source, /Chi tiết kỹ thuật', 'Technical details'/)
})

test('download completion automatically applies update once without a second button', () => {
  const source = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  assert.match(source, /state.phase === 'ready'\) \{\s*await applyUpdate\(\)/)
  assert.match(source, /if \(updateApplyStarted.current\) return/)
  assert.doesNotMatch(source, /updateDialog.kind === 'ready'.*<button/)
  assert.match(source, /if \(updateDialog\?\.kind !== 'downloading'\) return/)
})

test('installation progress has a single detailed surface, without dependency dumps', () => {
  const modal = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  const status = readFileSync(new URL('../frontend/src/features/configuration/runtimeStatus.ts', import.meta.url), 'utf8')
  assert.doesNotMatch(modal, /setMsg\(runtimeStatusText/)
  assert.match(modal, /msg && !installing && !installPopupError && section === 'setup'/)
  assert.match(modal, /msg && section !== 'logs' && section !== 'setup'/)
  assert.doesNotMatch(status, /profile, status.currentPackage/)
})

test('first-run setup exposes the shared locale selector and waits for saved locale', () => {
  const modal = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  const app = readFileSync(new URL('../frontend/src/app/App.tsx', import.meta.url), 'utf8')
  assert.match(modal, /locale, setLocale.*useLocale/)
  assert.match(modal, /value=\{locale\} onChange=.*setLocale/)
  assert.match(app, /open=\{configModalOpen && localeReady\}/)
  assert.match(modal, /t\('Kiểm tra cập nhật', 'Check for updates'\)/)
  assert.match(modal, /t\('Cấu hình', 'Settings'\)/)
  assert.match(modal, /t\('Cài gói AI', 'Install AI packages'\)/)
})

test('Windows Setup Vietnamese uses complete message sections', () => {
  const source = readFileSync(new URL('../build_app/languages/Vietnamese.isl', import.meta.url), 'utf8')
  const [messages, custom] = source.split('[Messages]')[1].split('[CustomMessages]')
  for (const key of ['CreateDesktopIcon', 'AdditionalIcons', 'LaunchProgram']) {
    assert.match(custom, new RegExp(`^${key}=.+`, 'm'))
    assert.doesNotMatch(messages, new RegExp(`^${key}=`, 'm'))
  }
  assert.match(messages, /^InstallingLabel=.+/m)
  assert.match(messages, /^CannotInstallToNetworkDrive=.+/m)
  assert.doesNotMatch(messages, /^(CannotInstallTo|InstallingDesc|PreviousInstallDetected|UninstallDataNotice)=/m)
})

test('runtime setup stages and errors have Vietnamese and English labels', () => {
  const source = readFileSync(new URL('../frontend/src/features/configuration/runtimeStatus.ts', import.meta.url), 'utf8')
  for (const key of ['detect_hardware', 'prepare_python', 'download_packages', 'install_packages', 'probe', 'activate', 'rollback',
    'HARDWARE_UNSUPPORTED', 'DRIVER_TOO_OLD', 'DOWNLOAD_FAILED', 'PYTHON_PREPARE_FAILED',
    'DEPENDENCY_INSTALL_FAILED', 'RUNTIME_PROBE_FAILED', 'DISK_FULL', 'ACTIVATION_FAILED', 'RUNTIME_BUSY']) {
    assert.match(source, new RegExp(`${key}: \\[\\s*'[^']+',\\s*'[^']+'\\s*\\]`), key)
  }
  const component = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  assert.match(component, /runtimeStatusText\(status, localeRef.current\)/)
  assert.match(component, /runtimeErrorText\(status, localeRef.current\)/)
  assert.doesNotMatch(source, /return status.message \|\|/)
  assert.doesNotMatch(source, /: status.error \|\|/)
  assert.doesNotMatch(component, /checks\?\.summary \|\|/)
})

test('static English catalog entries are non-empty', () => {
  const catalog = JSON.parse(readFileSync(new URL('../frontend/src/app/ui.en.json', import.meta.url), 'utf8'))
  for (const [key, value] of Object.entries(catalog)) {
    assert.equal(typeof value, 'string', key)
    assert.ok(value.trim(), key)
  }
})
