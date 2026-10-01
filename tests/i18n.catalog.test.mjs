import test from 'node:test'
test('Live Preview AI settings include bilingual model groups and credential gating', () => {
  const source = readFileSync(new URL('../frontend/src/features/project/TranslationReview.tsx', import.meta.url), 'utf8')
  assert.ok(source.includes("t('Model AI', 'AI model')"))
  assert.ok(source.includes("t('Model free dùng được', 'Free models available')"))
  assert.ok(source.includes("t('Model cần credit', 'Models requiring credit')"))
  assert.ok(source.includes('value.apiKeySet'))
  assert.ok(source.includes('!providers.length'))
})
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('Flow queue bulk folding is bilingual, covers both kinds and persists', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8')
  assert.ok(source.includes('t("Mở rộng tất cả", "Expand all")'))
  assert.ok(source.includes('t("Thu gọn tất cả", "Collapse all")'))
  assert.match(source, /disabled=\{!queueGroups.length\} onClick=\{toggleAllFoldersCollapsed\}/)
  const body = source.match(/const toggleAllFoldersCollapsed = \(\) => \{([\s\S]*?)\n  \};/)[1]
  const groups = [{ kind: 'video', outputDir: 'same' }, { kind: 'image', outputDir: 'same' }]
  let state = { 'video-same': true, unrelated: true }
  let saved
  const toggle = new Function('queueGroups', 'setCollapsedFolders', 'sessionStorage', 'COLLAPSED_FOLDERS_KEY', body)
  const run = () => toggle(groups, update => { state = update(state) }, { setItem: (_key, value) => { saved = JSON.parse(value) } }, 'test')
  run()
  assert.deepEqual(state, { 'video-same': true, 'image-same': true, unrelated: true })
  assert.deepEqual(saved, state)
  run()
  assert.deepEqual(state, { 'video-same': false, 'image-same': false, unrelated: true })
  assert.deepEqual(saved, state)
})

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

test('stable TTS routing removes language confirmation and OpenVoice controls', () => {
  const source = readFileSync(new URL('../frontend/src/features/tts/TtsStudio.tsx', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../frontend/src/features/project/project.api.ts', import.meta.url), 'utf8')
  const routes = readFileSync(new URL('../frontend/src/features/tts/lib/ttsStudioHelpers.tsx', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /OpenVoice V2|Xác nhận ngôn ngữ nội dung|cloneInputLanguage|languageRequest/)
  assert.doesNotMatch(api, /ttsDetectLanguage|ttsOpenVoiceInstall|tts\/language|tts\/openvoice\/install/)
  assert.doesNotMatch(routes, /'settings'/)
  assert.match(source, /preferredVoiceRef\.current = ''\s*\n\s*setVoice\(''\)/)
  assert.match(source, /TTS_TRANSCRIBE_RESULT_LS_KEY/)
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

test('Clone and Live Preview share compact bilingual translation review controls', () => {
  const sidebar = readFileSync(new URL('../frontend/src/features/project/ProjectSidebar.tsx', import.meta.url), 'utf8')
  const batch = readFileSync(new URL('../frontend/src/features/studio/CloneBatchSettingsPanel.tsx', import.meta.url), 'utf8')
  const card = readFileSync(new URL('../frontend/src/features/project/SegmentCard.tsx', import.meta.url), 'utf8')
  const projectPanel = readFileSync(new URL('../frontend/src/features/editor/EditorProjectPanel.tsx', import.meta.url), 'utf8')
  const properties = readFileSync(new URL('../frontend/src/features/editor/EditorPropertiesPanel.tsx', import.meta.url), 'utf8')
  const shared = readFileSync(new URL('../frontend/src/features/project/TranslationReview.tsx', import.meta.url), 'utf8')
  for (const source of [batch, projectPanel]) assert.match(source, /<TranslationReviewSettings settings=\{settings\}/)
  assert.match(sidebar, /<Field label=\{t\('Công cụ AI', 'AI provider'\)\}/)
  assert.match(sidebar, /<Field label=\{t\('Model AI', 'AI model'\)\}/)
  assert.doesNotMatch(sidebar, /<TranslationReviewSettings/)
  for (const source of [card, properties]) assert.match(source, /<TranslationReviewField projectId=\{projectId\}/)
  assert.match(shared, /t\('AI chỉnh bản dịch', 'AI translation review'\)/)
  assert.match(shared, /t\('Tự động sau khi dịch', 'Automatic after translation'\)/)
  assert.match(shared, /t\('Thủ công', 'Manual'\)/)
  assert.match(shared, /t\('AI chỉnh bản dịch', 'Review translation with AI'\)/)
  assert.match(shared, /t\('AI sửa câu này, giữ nguyên ý', 'Polish this translation with AI while preserving meaning'\)/)
  assert.match(shared, /t\('Nguồn AI dùng để sửa bản dịch', 'AI source used for translation review'\)/)
  assert.doesNotMatch(sidebar, /translation-review-card|translation-review-title/)
  const list = readFileSync(new URL('../frontend/src/features/project/SegmentList.tsx', import.meta.url), 'utf8')
  assert.doesNotMatch(list, /translation-review-hint/)
})
