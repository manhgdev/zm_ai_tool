import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

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
  for (const key of ['detect_hardware', 'prepare_python', 'install_packages', 'probe', 'activate', 'rollback',
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
