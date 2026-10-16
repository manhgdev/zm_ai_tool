import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { explainFlowError } from '../frontend/src/features/flow/flow.explain.ts'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')
const parse = path => ts.createSourceFile(path, read(path), ts.ScriptTarget.Latest, true)
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
  ['shared/components/BackgroundTasks.tsx'],
  ['features/tts/TtsStudio.tsx', 'features/tts/TtsTranscribePanel.tsx'],
  ['features/configuration/ConfigModal.tsx', 'features/configuration/setupLabels.ts'],
  ['pages/FlowPage.tsx', 'pages/FlowSeriesPanel.tsx'],
  ['pages/VideoCleanerPage.tsx'],
  ['pages/RendersPage.tsx'],
  ['pages/LandingPage.tsx'],
  ['features/project/TranslationReview.tsx'],
  ['features/project/ScriptTranslationReview.tsx', 'features/studio/CloneBatchSettingsPanel.tsx', 'features/project/ProjectSidebar.tsx', 'features/editor/EditorProjectPanel.tsx', 'features/editor/EditorPropertiesPanel.tsx'],
  ['features/project/useTranslationProviders.ts'],
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

test('landing usage guide has bilingual navigation and all four topics', () => {
  const pairs = new Map()
  visit(parse('pages/LandingPage.tsx'), node => {
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
  const error = explainFlowError('FLOW_COOKIE_EXPIRED')
  for (const field of ['title', 'summary', 'action']) {
    assert.ok(error[`${field}Vi`]?.trim())
    assert.ok(error[`${field}En`]?.trim())
    assert.notEqual(error[`${field}Vi`], error[`${field}En`])
  }
  assert.doesNotMatch(error.titleVi, /hết hạn/)
  assert.doesNotMatch(error.titleEn, /expired/i)
  assert.doesNotMatch(error.actionVi, /vừa mở/)
  assert.doesNotMatch(error.actionEn, /that opened/)
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
