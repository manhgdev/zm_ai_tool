import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { explainFlowError, explainFlowEvent } from '../frontend/src/features/flow/flow.explain.ts'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')
const parse = path => ts.createSourceFile(path, read(path), ts.ScriptTarget.Latest, true)
test('Flow automatic headed fallback has Vietnamese and English event labels', () => {
  const label = explainFlowEvent('browser_headed_fallback')
  assert.match(label.titleVi, /Chrome.*ngoài màn hình/)
  assert.match(label.titleEn, /Chrome.*off screen/)
  for (const event of ['project_browser_retry', 'project_create_control_missing']) {
    const label = explainFlowEvent(event)
    assert.notEqual(label.titleVi, event)
    assert.notEqual(label.titleVi, label.titleEn)
    assert.ok(label.titleEn.trim())
  }
})
function visit(node, check) {
  check(node)
  ts.forEachChild(node, child => visit(child, check))
}
function literal(node) {
  return node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) ? node.text : undefined
}

// Validate translation data, independent of quotes, whitespace, JSX layout or CSS.
for (const paths of [
  ['app/App.tsx'],
  ['app/WebSession.tsx'],
  ['pages/AdminPage.tsx'],
  ['features/license/LicensePage.tsx'],
  ['pages/AutomationPage.tsx'],
  ['shared/components/BackgroundTasks.tsx'],
  ['features/tts/TtsStudio.tsx', 'features/tts/TtsTranscribePanel.tsx'],
  ['features/configuration/ConfigModal.tsx', 'features/configuration/setupLabels.ts', 'features/configuration/SystemInfoPanel.tsx'],
  ['pages/FlowPage.tsx', 'pages/FlowSeriesPanel.tsx'],
  ['pages/VideoCleanerPage.tsx'],
  ['pages/RendersPage.tsx'],
  ['pages/LandingPage.tsx'],
  ['features/project/TranslationReview.tsx'],
  ['features/project/ScriptTranslationReview.tsx', 'features/studio/CloneBatchSettingsPanel.tsx', 'features/project/ProjectSidebar.tsx', 'features/editor/EditorProjectPanel.tsx', 'features/editor/EditorPropertiesPanel.tsx'],
  ['features/project/useTranslationProviders.ts'],
  ['features/studio/ReviewSettingsPanel.tsx'],
]) {
  test(`inline Vietnamese/English pairs are complete: ${paths.join(', ')}`, () => {
    for (const path of paths) {
      let pairs = 0
      visit(parse(path), node => {
        if (!ts.isCallExpression(node)) return
        const name = node.expression.getText()
        const args = node.arguments
        const offset = name === 'localize' ? 1 : 0
        if (!['t', 'localize', 'text'].includes(name) || args.length < offset + 2) return
        const vi = literal(args[offset])
        if (vi === undefined || !vi.trim()) return
        const en = literal(args[offset + 1])
        if (en === undefined) return // Dynamic expressions are checked by TypeScript.
        pairs++
        assert.ok(en.trim(), `${path}: missing English for ${vi}`)
      })
      assert.ok(pairs > 0, `${path}: no bilingual labels checked`)
    }
  })
}

test('OpenCode model presets and setup guidance are bilingual', () => {
  let presets
  visit(parse('features/configuration/configModal.helpers.ts'), node => {
    if (ts.isPropertyAssignment(node) && node.name.getText() === 'opencode' && ts.isArrayLiteralExpression(node.initializer)) presets = node.initializer
  })
  assert.ok(presets?.elements.length)
  for (const model of presets.elements) {
    const values = Object.fromEntries(model.properties.map(prop => [prop.name.getText(), literal(prop.initializer)]))
    assert.ok(values.id && values.labelVi && values.labelEn)
    assert.notEqual(values.labelVi, values.labelEn)
  }
  const pairs = new Map()
  visit(parse('features/configuration/ConfigModal.tsx'), node => {
    if (ts.isCallExpression(node) && node.expression.getText() === 't') pairs.set(literal(node.arguments[0]), literal(node.arguments[1]))
  })
  assert.equal(pairs.get('Lấy API key tại '), 'Get an API key from ')
  assert.ok(pairs.get('Danh sách chỉ gồm model dùng Chat Completions. Quyền dùng model free phụ thuộc chính sách OpenCode và tài khoản của bạn.'))
})

test('application key copy feedback is bilingual', () => {
  const pairs = new Map()
  visit(parse('pages/AdminPage.tsx'), node => {
    if (ts.isCallExpression(node) && node.expression.getText() === 't') pairs.set(literal(node.arguments[0]), literal(node.arguments[1]))
  })
  for (const vi of ['Sao chép key', 'Nháy đúp để sao chép key', 'Đang sao chép…', 'Đã sao chép key.']) {
    assert.ok(pairs.get(vi)?.trim(), vi)
    assert.notEqual(pairs.get(vi), vi)
  }
  const admin = read('pages/AdminPage.tsx')
  assert.ok(admin.includes("import { toast } from 'sonner'"))
  assert.ok(admin.includes("toast.success(t('Đã lưu thay đổi.', 'Changes saved.'))"))
  assert.ok(admin.includes('onDoubleClick={() => void copyListedKey(item)}'))
  assert.ok(!admin.includes('admin-success') && !admin.includes('admin-error'))
  assert.ok(!admin.includes('notify: false'))
  assert.ok(!admin.includes('Key cũ chỉ lưu mã băm.'))
  const frame = read('app/WebSession.tsx')
  assert.ok(frame.includes('<Toaster position="bottom-right" theme={theme} richColors closeButton'))
  for (const pair of ["localize(locale, 'Thông báo', 'Notifications')", "localize(locale, 'Đóng thông báo', 'Close notification')"]) assert.ok(frame.includes(pair), pair)
})

test('admin errors retain API codes and explain origin and account failures in both languages', async () => {
  let request, errors, explain, messages
  visit(parse('pages/AdminPage.tsx'), node => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'request') request = node.getText()
  })
  visit(parse('app/WebSession.tsx'), node => {
    if (ts.isVariableDeclaration(node) && node.name.getText() === 'webErrors') errors = node.getText()
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'webErrorText') explain = node.getText().replace(/^export /, '')
  })
  visit(parse('app/i18n.tsx'), node => {
    if (ts.isVariableDeclaration(node) && node.name.getText() === 'MESSAGES') messages = node.getText()
  })
  assert.ok(request && errors && explain && messages)
  const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const catalog = new Function(`${compile(`const ${messages}`)}; return MESSAGES`)()
  const errorText = new Function('localize', 'translate', `${compile(`const ${errors}; ${explain}`)}; return webErrorText`)(
    (locale, vi, en) => locale === 'vi' ? vi : en,
    (locale, key) => catalog[key][locale],
  )
  for (const [code, key] of [
    ['WEB_ORIGIN_REJECTED', 'error.web.origin'],
    ['WEB_ACCOUNT_CHANGED', 'error.web.accountChanged'],
    ['WEB_LOGIN_REQUIRED', 'error.web.loginRequired'],
  ]) for (const locale of ['vi', 'en']) {
    for (const body of [{ detail: code }, { code, detail: catalog[key][locale] }]) {
      const call = new Function('fetch', `${compile(request)}; return request`)(async () => ({ ok: false, json: async () => body }))
      await assert.rejects(() => call('/api/admin/keys', 'POST', {}), error => {
        assert.equal(error.message, code)
        assert.equal(errorText(error.message, locale), catalog[key][locale])
        return true
      })
    }
  }
  assert.equal(errorText('INVALID_FIELDS', 'vi'), 'Thông tin chưa hợp lệ.')
  assert.equal(errorText('INVALID_FIELDS', 'en'), 'Invalid details.')
})

test('computer information report localizes identifiers and retains every section for copying', () => {
  const tree = parse('features/configuration/SystemInfoPanel.tsx')
  const pairs = new Map()
  visit(tree, node => {
    if (ts.isCallExpression(node) && node.expression.getText() === 't') pairs.set(literal(node.arguments[0]), literal(node.arguments[1]))
  })
  for (const vi of ['Thông tin máy tính', 'Sao chép toàn bộ', 'Làm mới', 'Báo cáo thông tin máy tính', 'Không có dữ liệu']) {
    assert.ok(pairs.get(vi)?.trim(), vi)
    assert.notEqual(pairs.get(vi), vi)
  }
  const fn = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'formatSystemInfo')
  const code = ts.transpileModule(fn.getText().replace('export ', ''), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const format = new Function('localize', `${code}; return formatSystemInfo`)((locale, vi, en) => locale === 'vi' ? vi : en)
  const report = { collectedAt: '2026-10-22T10:00:00Z', sections: [
    { id: 'cpu', rows: [{ id: 'physicalCores', value: 8 }, { id: 'usage', value: 0, unit: 'percent' }] },
    { id: 'memory', rows: [{ id: 'total', value: 16 * 1024 ** 3, unit: 'bytes' }] },
    { id: 'gpu', name: 0, rows: [{ id: 'model', value: 'Test GPU' }, { id: 'driver', value: null }] },
    { id: 'app', rows: [{ id: 'packaged', value: false }] },
    { id: 'pythonPackages', rows: [{ id: 'package', name: 'torch', value: '2.8.0' }] },
  ] }
  const vi = format(report, 'vi'), en = format(report, 'en')
  assert.match(vi, /Số nhân vật lý: 8/)
  assert.match(en, /Physical cores: 8/)
  assert.match(vi, /Bản đóng gói: Không/)
  assert.match(en, /Packaged build: No/)
  assert.match(vi, /Phiên bản driver: Không có dữ liệu/)
  assert.match(en, /Driver version: Unavailable/)
  for (const text of [vi, en]) for (const value of ['16 GiB', 'Test GPU', 'torch: 2.8.0', '0%', '[GPU · 0]', report.collectedAt]) assert.ok(text.includes(value), value)
  const source = read('features/configuration/SystemInfoPanel.tsx')
  assert.match(source, /copyText\(report\)/)
  assert.match(source, /value=\{report\}/)
  const modal = read('features/configuration/ConfigModal.tsx')
  assert.ok(modal.indexOf("onClick={() => setSection('computer')}") > modal.indexOf("onClick={() => setSection('logs')}"))
})

test('landing usage guide has bilingual navigation and all four topics', () => {
  const pairs = new Map()
  for (const path of ['pages/LandingPage.tsx', 'app/WebSession.tsx']) visit(parse(path), node => {
    if (ts.isCallExpression(node) && node.expression.getText() === 't') {
      pairs.set(literal(node.arguments[0]), literal(node.arguments[1]))
    }
  })
  for (const vi of ['Hướng dẫn', 'Xem hướng dẫn', 'Cài đặt ứng dụng', 'Cấu hình lần đầu', 'Dịch & lồng tiếng video', 'Chỉnh sửa & xuất bản', 'Từ cài đặt, cấu hình đến chỉnh sửa và xuất video — đầy đủ các bước bên dưới.']) {
    assert.ok(pairs.get(vi)?.trim(), `Missing English guide label for ${vi}`)
    assert.notEqual(pairs.get(vi), vi)
  }
  const source = read('pages/LandingPage.tsx')
  assert.match(source, /guides\.map\(\(guide, index\) => <details className="lp-guide-card" key=\{guide.title\} open>/)
  assert.match(source, /<summary className="lp-guide-header">/)
  assert.match(source, /className="lp-guide-toggle" aria-hidden="true">\+<\/span>/)
  assert.match(read('pages/LandingPage.css'), /\.lp-guide-grid\s*\{[^}]*align-items: stretch/)
})

test('landing value proposition and creation paths have bilingual copy and real previews', () => {
  const pairs = new Map()
  visit(parse('pages/LandingPage.tsx'), node => {
    if (ts.isCallExpression(node) && node.expression.getText() === 't') {
      pairs.set(literal(node.arguments[0]), literal(node.arguments[1]))
    }
  })
  for (const vi of ['Làm video.', 'Đậm chất của bạn.', 'AI xử lý. Bạn làm chủ câu chuyện.', 'VIDEO CÓ SẴN', 'KỊCH BẢN CỦA BẠN', 'Ý TƯỞNG MỚI', 'Giao diện thật. Quyền kiểm soát thật.', 'AI làm phần nặng.', 'Bạn giữ phần sáng tạo.']) {
    assert.ok(pairs.get(vi)?.trim(), `Missing English landing copy for ${vi}`)
    assert.notEqual(pairs.get(vi), vi)
  }
  const source = read('pages/LandingPage.tsx')
  assert.match(source, /const \[preview, setPreview\] = useState\(0\)/)
  assert.match(source, /const previews = \[\s*\{[^\n]*image: '\/previews\/live-previews\.png'/)
  assert.match(source, /id="use-cases"/)
  assert.match(source, /outcomes\.map/)
  assert.match(source, /download=\{asset.name\}/)
  const css = read('pages/LandingPage.css')
  assert.match(css, /--lp-bg: #fff;/)
  assert.match(css, /\.landing\[data-theme="dark"\] \.lp-hero\s*\{/)
  assert.match(css, /\.landing\[data-theme="dark"\] \.lp-workflow-section\s*\{/)
  assert.match(css, /\.lp-preview-tabs\s*\{[^}]*background: var\(--lp-surface\)/)
  assert.match(css, /\.lp-preview-screen img\s*\{[^}]*width: 100%; height: auto;/)
  assert.match(css, /\.lp-preview-screen img\s*\{[^}]*border-radius: 0 0 3% 3%/)
  assert.doesNotMatch(css, /\.lp-preview-screen\s*\{[^}]*aspect-ratio:/)
})

test('shared chrome and runtime dictionaries have complete translations', () => {
  for (const [path, dictionaries, size] of [
    ['app/i18n.tsx', ['MESSAGES'], 2],
    ['features/configuration/runtimeStatus.ts', ['stages', 'errors'], 2],
    ['features/configuration/setupLabels.ts', ['labels'], 4],
  ]) {
    const found = new Set()
    visit(parse(path), node => {
      if (!ts.isVariableDeclaration(node) || !dictionaries.includes(node.name.getText())) return
      found.add(node.name.getText())
      let value = node.initializer
      while (ts.isAsExpression(value)) value = value.expression
      assert.ok(ts.isObjectLiteralExpression(value))
      assert.ok(value.properties.length > 0)
      for (const entry of value.properties) {
        const label = `${path}: ${entry.name.getText()}`
        const values = size === 2 && ts.isObjectLiteralExpression(entry.initializer)
          ? ['vi', 'en'].map(key => entry.initializer.properties.find(p => p.name.getText() === key)?.initializer)
          : entry.initializer.elements
        assert.equal(values.length, size, label)
        for (const item of values) assert.ok(literal(item)?.trim(), label)
      }
    })
    assert.equal(found.size, dictionaries.length, path)
  }
})

test('Flow cookie account inputs have Vietnamese and English labels', () => {
  const pairs = new Map()
  visit(parse('pages/FlowPage.tsx'), node => {
    if (ts.isCallExpression(node) && node.expression.getText() === 't') {
      pairs.set(literal(node.arguments[0]), literal(node.arguments[1]))
    }
  })
  for (const vi of ['Email Google', 'Cách kết nối', 'Dán Cookie Google Flow', 'Cookie Google Flow *', 'Lưu và kết nối', 'Mở phiên']) {
    assert.ok(pairs.get(vi)?.trim(), `Missing English label for ${vi}`)
    assert.notEqual(pairs.get(vi), vi)
  }
  assert.doesNotMatch(read('pages/FlowPage.tsx'), /Tên hiển thị|Định danh tài khoản/)
  assert.doesNotMatch(read('pages/FlowPage.tsx'), /Project ID/)
})

test('headless Flow login explains cookie and desktop recovery in both languages', () => {
  const error = explainFlowError('FLOW_LOGIN_VERIFICATION_REQUIRED')
  assert.equal(error.code, 'FLOW_LOGIN_VERIFICATION_REQUIRED')
  assert.notEqual(error.titleVi, error.titleEn)
  assert.match(error.actionVi, /đăng nhập Google bằng Chrome/)
  assert.match(error.actionEn, /complete Google sign-in/)
  for (const action of [error.actionVi, error.actionEn]) {
    assert.match(action, /desktop\/VNC/)
    assert.match(action, /[Pp]assword|mật khẩu/)
  }
})

test('Flow login redirect keeps bilingual recovery copy without claiming cookie expiry or an open window', () => {
  for (const code of ['FLOW_COOKIE_EXPIRED', 'FLOW_SESSION_EXPIRED']) {
    const error = explainFlowError(code)
    for (const field of ['title', 'summary', 'action']) {
      assert.ok(error[`${field}Vi`]?.trim())
      assert.ok(error[`${field}En`]?.trim())
      assert.notEqual(error[`${field}Vi`], error[`${field}En`])
    }
    assert.doesNotMatch(error.titleVi, /hết hạn/)
    assert.doesNotMatch(error.titleEn, /expired/i)
    assert.doesNotMatch(error.actionVi, /vừa mở|đang thử đăng nhập/)
    assert.doesNotMatch(error.actionEn, /that opened|is signing in/)
  }
})

test('Flow result timeout explains stopped collection and same-account retry in both languages', () => {
  const error = explainFlowError('FLOW_GENERATION_TIMEOUT: original result could not be collected')
  assert.equal(error.titleVi, 'Chưa lấy được kết quả đã gửi')
  assert.equal(error.titleEn, 'Submitted result not collected')
  assert.match(error.actionVi, /dừng chờ/)
  assert.match(error.actionVi, /cùng tài khoản/)
  assert.match(error.actionEn, /stopped waiting/)
  assert.match(error.actionEn, /same account/)
})

test('Flow project and copied-session errors have bilingual guidance without claiming logout', () => {
 for (const code of ['FLOW_PROJECT_NOT_FOUND', 'FLOW_COOKIE_SESSION_UNVERIFIED']) {
  const error = explainFlowError(`${code}: Could not prepare job Chrome`)
  assert.equal(error.code, code)
  for (const field of ['title', 'summary', 'action']) {
    assert.ok(error[`${field}Vi`]?.trim())
    assert.ok(error[`${field}En`]?.trim())
    assert.notEqual(error[`${field}Vi`], error[`${field}En`])
  }
  assert.match(error.actionVi, /Chạy lại/)
  assert.match(error.actionEn, /Retry/)
  assert.doesNotMatch(error.actionVi, /đăng nhập|Kết nối/)
  assert.doesNotMatch(error.actionEn, /sign.in|Reconnect/)
 }
})

test('CapCut errors produce Vietnamese and English messages while preserving diagnostics', () => {
  const tree = parse('app/i18n.tsx')
  const fn = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'localizePipelineMessage')
  const code = ts.transpileModule(fn.getText().replace('export ', ''), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const localize = (locale, vi, en) => locale === 'vi' ? vi : en
  const message = new Function('localize', `${code}; return localizePipelineMessage`)(localize)
  for (const error of ['CAPCUT_TTS_TIMEOUT', 'CAPCUT_TTS_NETWORK_ERROR|ConnectTimeout: diagnostic']) {
    const vi = message('vi', error)
    const en = message('en', error)
    assert.ok(vi && en)
    assert.notEqual(vi, en)
    assert.notEqual(en, error)
    if (error.includes('|')) {
      assert.ok(vi.includes('ConnectTimeout: diagnostic'))
      assert.ok(en.includes('ConnectTimeout: diagnostic'))
    }
  }
})

test('network waiting has Vietnamese and English labels in Flow and background tasks', () => {
  for (const path of ['pages/FlowPage.tsx', 'shared/components/BackgroundTasks.tsx', 'features/flow/flow.explain.ts']) {
    assert.match(read(path), /waiting_network/)
    assert.match(read(path), /Đang chờ mạng/)
    assert.match(read(path), /Waiting for network/)
  }
})

test('static English catalog entries are non-empty', () => {
  const catalog = JSON.parse(read('app/ui.en.json'))
  assert.ok(Object.keys(catalog).length > 0)
  for (const [key, value] of Object.entries(catalog)) {
    assert.equal(typeof value, 'string', key)
    assert.ok(value.trim(), key)
  }
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

test('non-quota Flow image rejection tile maps to FLOW_GENERATION_REJECTED and strips icon ligatures', () => {
  const error = explainFlowError(
    'FLOW_QUOTA_EXHAUSTED: warning Không thành công Rất tiếc, không tạo được hình ảnh này. Bạn chưa bị tính phí cho lượt tạo này. refresh undo delete_forever'
  )
  assert.equal(error.code, 'FLOW_GENERATION_REJECTED')
  assert.doesNotMatch(error.titleVi, /refresh|undo|delete_forever|warning Không thành công/i)
  assert.ok(error.actionVi.trim() && error.actionEn.trim())
  assert.notEqual(error.actionVi, error.actionEn)
})

test('Flow folder header actions include bilingual Zip button between Cancel and Delete', () => {
  const page = read('pages/FlowPage.tsx')
  const section = page.split('className="flow-queue-folder-actions"')[1]?.slice(0, 2600) || ''
  const cancelIdx = section.indexOf('t("Hủy", "Cancel")')
  const zipIdx = section.indexOf('t("Nén ZIP", "Zip")')
  const deleteIdx = section.indexOf('t("Xóa", "Delete")')
  assert.ok(cancelIdx >= 0 && zipIdx > cancelIdx && deleteIdx > zipIdx)
})






test('account management and APP login keep bilingual access and term controls', () => {
  const admin = read('pages/AdminPage.tsx')
  for (const pair of ["t('Quản lý tài khoản', 'Account management')", "t('Quản lý key', 'Key management')", "t('Lần kích hoạt đầu tiên', 'First activation')", "t('Hạn mức đại lý', 'Reseller allowance')", "t('Vĩnh viễn', 'Lifetime')"]) assert.ok(admin.includes(pair), pair)
  const login = read('features/license/LicensePage.tsx')
  assert.ok(login.includes("t('Đăng nhập bằng Google', 'Sign in with Google')"))
  assert.match(login, /user \? <a[^]*Thông tin tài khoản và đăng xuất/)
  assert.match(read('app/WebSession.tsx'), /<WebHeader[^]*<WebFooter/)
})

test('account page layouts keep bilingual dialogs, actions and empty states', () => {
  const admin = read('pages/AdminPage.tsx')
  for (const pair of ["t('Sửa', 'Edit')", "t('Gia hạn', 'Renew')", "t('Thu hồi phiên', 'Revoke sessions')", "t('Xóa', 'Delete')", "t('Thông tin tài khoản', 'Account details')", "t('Thông tin key', 'Key details')", "t('Quyền sử dụng', 'Access')", "t('Thử thay đổi bộ lọc hoặc thêm một mục mới.', 'Try changing the filters or adding a new item.')"]) assert.ok(admin.includes(pair), pair)
  assert.ok(admin.includes("localize(loadLocale(), 'Đóng', 'Close')"))
  assert.ok(read('app/WebSession.tsx').includes("t('Bắt đầu · Giờ Việt Nam', 'Starts · Vietnam time')"))
})

test('APP activation identifies one method and exposes a red destructive action', () => {
  const source = read('features/license/LicensePage.tsx')
  for (const pair of ["t('Phương thức kích hoạt APP', 'APP activation method')", "t('Tài khoản Google', 'Google account')", "t('Thiết bị đang kích hoạt / giới hạn', 'Active devices / limit')", "t('Kiểm tra lại quyền', 'Refresh access')", "t('Hủy kích hoạt thiết bị', 'Deactivate device')"]) assert.ok(source.includes(pair), pair)
  assert.match(source, /status\.configured \? <>/)
  assert.match(source, /disabled=\{busy \|\| waiting \|\| !key\.trim\(\) \|\| !status\.installationId\}/)
  assert.match(read('features/license/LicensePage.css'), /\.license-deactivate\.license-danger\s*\{[^}]*color: var\(--destructive\)/)
})

test('APP shows detected hardware without manual selection and keeps bilingual replacement states', () => {
  const source = read('features/license/LicensePage.tsx')
  for (const pair of ["t('Loại thiết bị', 'Device category')", "t('Mã máy', 'Machine ID')", "t('Mã máy: ', 'Machine ID: ')"]) assert.ok(source.includes(pair), pair)
  assert.doesNotMatch(source, /<select|selectedType|Xác nhận loại thiết bị này|Confirm this device category/)
  assert.match(source, /deviceTypeText\(status\.deviceType, locale\)/)
  assert.match(read('features/license/license.api.ts'), /deviceType\?: 'pc' \| 'laptop'/)
  const shared = read('app/WebSession.tsx')
  for (const pair of ["t('Điện thoại', 'Phone')", "t('Máy tính bảng / iPad', 'Tablet / iPad')", "t('Máy tính bàn', 'Desktop PC')", "t('Máy tính xách tay', 'Laptop')"]) assert.ok(shared.includes(pair), pair)
  for (const code of ['DEVICE_REPLACED', 'DEVICE_MISMATCH', 'DEVICE_REACTIVATION_REQUIRED', 'DEVICE_ID_UNAVAILABLE', 'DEVICE_TYPE_LOCKED']) assert.match(shared, new RegExp(code + ': t\\('))
  assert.ok(!read('pages/AdminPage.tsx').includes('form.max_devices'))
})

test('APP Google callback failures stop waiting with bilingual errors and refresh on return', () => {
  const source = read('features/license/LicensePage.tsx')
  assert.match(source, /waiting && status\.loginError[^]*setWaiting\(false\); setError\(webErrorText\(status\.loginError, locale\)\)/)
  assert.ok(source.includes("window.addEventListener('focus', resumed)"))
  assert.ok(source.includes("document.addEventListener('visibilitychange', resumed)"))
  assert.ok(source.includes("window.removeEventListener('focus', resumed)"))
  assert.ok(source.includes("document.removeEventListener('visibilitychange', resumed)"))
  assert.match(read('app/WebSession.tsx'), /AUTH_SERVER_UPDATE_REQUIRED: t\('[^']+', '[^']+'\)/)
})


test('unactivated APP retains its header and opens activation only for task access requests', () => {
  const app = read('app/App.tsx')
  assert.match(app, /appUsable && \(\s*<Header/)
  assert.doesNotMatch(app, /app-license-gate|status=\{licenseStatus \|\| EMPTY_LICENSE\}\s+gate/)
  for (const event of ['onClickCapture', 'onPointerDownCapture', 'onKeyDownCapture', 'onSubmitCapture', 'onDropCapture']) {
    assert.ok(!app.includes(`${event}={requireActivation}`), event)
  }
  assert.match(app, /onModeChange=\{navigateToMode\}/)
  assert.match(app, /window\.addEventListener\(TASK_ACCESS_REQUIRED, activate\)/)
  assert.match(app, /setConfigSection\('license'\)\s+setConfigOpen\(true\)/)
  assert.doesNotMatch(app, /key=\{licenseStatus\?\.valid/)
  assert.match(read('features/configuration/ConfigModal.tsx'), /t\('Kiểm tra cập nhật', 'Check for updates'\)/)
  assert.match(read('shared/components/Header.tsx'), /!licenseStatus\.valid \? t\('header\.activate'\)/)
  assert.match(read('app/i18n.tsx'), /'header.activate': \{ vi: 'Kích hoạt APP', en: 'Activate APP' \}/)
})

test('header locale switch collapses to a bilingual toggle on small screens', () => {
  const source = read('shared/components/LocaleSwitch.tsx')
  assert.ok(read('shared/components/Header.tsx').includes('compactOnSmallScreen'))
  assert.ok(source.includes("localize(locale, 'Chuyển sang tiếng Anh', 'Switch to Vietnamese')"))
  assert.ok(source.includes("onChange(locale === 'vi' ? 'en' : 'vi')"))
  assert.ok(source.includes('{locale.toUpperCase()}'))
  const css = read('shared/components/LocaleSwitch.css')
  assert.match(css, /@media \(max-width: 900px\)/)
  assert.match(css, /\.locale-switch-responsive > \.locale-switch-toggle\s*\{\s*display: inline-block/)
})
