import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('Flow top navigation includes Series and Accounts above utility views', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8')
  const start = source.indexOf('<div className="flow-tabs"')
  const end = source.indexOf('{utilityView === "accounts" && (', start)
  assert.ok(start >= 0 && end > start)
  const tabs = source.slice(start, end)
  assert.deepEqual([...tabs.matchAll(/\["(createImage|createVideo|series|queue|history|accounts|logs)", Icon/g)].map(match => match[1]), ['createImage', 'createVideo', 'series', 'queue', 'history', 'accounts', 'logs'])
  assert.ok(tabs.includes('t("Tạo ảnh", "Create image")'))
  assert.ok(tabs.includes('t("Tạo video", "Create video")'))
  assert.match(tabs, /\["series", IconBook, t\("Series", "Series"\)\]/)
  assert.match(tabs, /\["accounts", IconGear, t\("Tài khoản", "Accounts"\)\]/)
  assert.ok(tabs.includes('createKind === "image" ? "createImage" : "createVideo"'))
  assert.ok(tabs.includes('activateRail(id)'))
})

test('local cleaner explains conservative matching and localizes its stable errors', () => {
  const source = readFileSync(new URL('../frontend/src/pages/VideoCleanerPage.tsx', import.meta.url), 'utf8')
  for (const code of ['CLEANER_LOGO_NOT_DETECTED', 'CLEANER_IMAGE_DEPTH_UNSUPPORTED', 'CLEANER_INVALID_LOGO_MASK']) {
    assert.match(source, new RegExp(`error === '${code}'\\) return t\\('[^']+', '[^']+'\\)`))
  }
  assert.match(source, /cleanerError\(job.error\)/)
  assert.match(source, /Khử lớp phủ sao bán trong suốt/)
  assert.match(source, /Recover translucent sparkle overlays/)
  assert.doesNotMatch(source, /Using local sparkle mask|fallback still creates a result file/)
})

test('Series AI create view is bilingual and uses backend provider labels only as brand names', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowSeriesPanel.tsx', import.meta.url), 'utf8')
  for (const [vi, en] of [['Tạo Series bằng AI', 'Create a Series with AI'], ['Chủ đề', 'Topic'], ['Lưu thành Series', 'Save as Series'], ['Cài đặt nhanh', 'Quick settings'], ['Nhân vật & ảnh neo', 'Characters & anchors']]) {
    assert.ok(source.includes(`t('${vi}', '${en}')`), `${vi} / ${en}`)
  }
  assert.match(source, /chưa sẵn sàng', 'not ready'/)
})

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
  assert.match(source, /if \(updateApplyStarted.current \|\| updateCancelRequested.current\) return/)
  assert.doesNotMatch(source, /updateDialog.kind === 'ready'.*<button/)
  assert.match(source, /if \(updateDialog\?\.kind !== 'downloading' && updateDialog\?\.kind !== 'cancelling'\) return/)
})

test('OpenVoice controls and errors have Vietnamese and English labels', () => {
  const source = readFileSync(new URL('../frontend/src/features/tts/TtsStudio.tsx', import.meta.url), 'utf8')
  const messages = readFileSync(new URL('../frontend/src/app/i18n.tsx', import.meta.url), 'utf8')
  const voiceFilter = source.slice(source.indexOf('const engineVoices = useMemo'), source.indexOf('const voiceFilterTags'))
  assert.match(voiceFilter, /v\.language\?\.split/)
  assert.doesNotMatch(voiceFilter, /mode === 'reference'/)
  assert.match(source, /t\('Cài OpenVoice', 'Install OpenVoice'\)/)
  assert.match(source, /t\('Cài đặt TTS', 'TTS settings'\)/)
  assert.match(source, /t\('Clone giọng đa ngôn ngữ', 'Multilingual voice cloning'\)/)
  assert.match(source, /t\('Tạo cloud xong trả thẳng audio, không qua OpenVoice\.', 'Cloud synthesis returns the audio directly without OpenVoice\.'/)
  assert.match(source, /t\('Chỉ khi chọn giọng clone: cloud tạo audio nguồn, OpenVoice chuyển sang giọng clone đã chọn\.'/)
  const sidebar = source.split('</aside>')[0]
  assert.match(sidebar, /go\('settings'\)/)
  assert.doesNotMatch(sidebar, /tts-openvoice-card/)
  const routes = readFileSync(new URL('../frontend/src/features/tts/lib/ttsStudioHelpers.tsx', import.meta.url), 'utf8')
  assert.match(routes, /TTS_URL_SECTIONS = new Set\(\[[\s\S]*?'settings'/)
  assert.match(source, /t\('Xác nhận ngôn ngữ nội dung', 'Confirm input language'\)/)
  for (const key of ['TTS_LANGUAGE_REQUIRED', 'TTS_SOURCE_UNAVAILABLE', 'OPENVOICE_NOT_INSTALLED', 'OPENVOICE_WORKER_FAILED', 'OPENVOICE_TIMEOUT']) {
    assert.match(messages, new RegExp(`${key}: \\[\\s*'[^']+',\\s*'[^']+'\\s*\\]`))
  }
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
  for (const key of ['detect_hardware', 'prepare_python', 'resolve', 'download', 'install', 'download_packages', 'install_packages', 'probe', 'activate', 'rollback',
    'NETWORK_TIMEOUT', 'DNS_FAILED', 'PROXY_AUTH_FAILED', 'TLS_CERTIFICATE_FAILED', 'HTTP_UNAUTHORIZED',
    'HTTP_RATE_LIMITED', 'HTTP_FORBIDDEN', 'HTTP_NOT_FOUND', 'INDEX_UNAVAILABLE', 'DOWNLOAD_INTERRUPTED', 'DEPENDENCY_RESOLUTION_FAILED', 'CHECKSUM_MISMATCH', 'FILE_ACCESS_DENIED',
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

test('Flow random account options and suspension badges are bilingual', () => {
  const flowPage = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8')
  assert.match(flowPage, /t\("🎲 Ngẫu nhiên tài khoản", "🎲 Random account"\)/)
  assert.match(flowPage, /t\("Tạm cách ly", "Suspended"\)/)
  assert.match(flowPage, /t\("Bỏ cách ly", "Unblock"\)/)
  assert.match(flowPage, /t\("Tài khoản tạo ảnh", "Image account"\)/)
  assert.match(flowPage, /t\("Tài khoản tạo video", "Video account"\)/)
  const seriesPanel = readFileSync(new URL('../frontend/src/pages/FlowSeriesPanel.tsx', import.meta.url), 'utf8')
  assert.match(seriesPanel, /t\('🎲 Ngẫu nhiên tài khoản', '🎲 Random account'\)/)
})

test('static English catalog entries are non-empty', () => {
  const catalog = JSON.parse(readFileSync(new URL('../frontend/src/app/ui.en.json', import.meta.url), 'utf8'))
  for (const [key, value] of Object.entries(catalog)) {
    assert.equal(typeof value, 'string', key)
    assert.ok(value.trim(), key)
  }
})
