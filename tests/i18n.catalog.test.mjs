import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

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
  ['shared/components/BackgroundTasks.tsx'],
  ['features/tts/TtsStudio.tsx', 'features/tts/TtsTranscribePanel.tsx'],
  ['features/configuration/ConfigModal.tsx', 'features/configuration/setupLabels.ts'],
  ['pages/FlowPage.tsx', 'pages/FlowSeriesPanel.tsx'],
  ['pages/VideoCleanerPage.tsx'],
  ['pages/RendersPage.tsx'],
  ['features/project/TranslationReview.tsx'],
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
