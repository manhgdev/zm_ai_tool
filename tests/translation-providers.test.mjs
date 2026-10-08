import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')

test('translation providers lock missing keys and refresh on configuration changes', async () => {
  const source = read('features/project/useTranslationProviders.ts')
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const cloud = ['openai', 'gemini', 'deepseek', 'openrouter', 'grok', 'groq', 'nvidia', 'mistral']
  let state = []
  let effect
  let cleanup
  let config = { cloud: { openrouter: { apiKeySet: true }, gemini: { apiKeySet: false } } }
  const events = new EventTarget()
  const modules = {
    react: { useState: () => [state, value => { state = value }], useEffect: fn => { effect = fn }, useCallback: fn => fn },
    '@/app/appSettings': { AI_REVIEW_TRANSLATORS: ['ollama', ...cloud] },
    '@/app/i18n': { useLocale: () => ({ locale: 'en' }), localize: (_locale, _vi, en) => en },
    './project.api': { api: { getConfig: async () => config } },
  }
  const exports = {}
  new Function('require', 'exports', 'window', js)(id => modules[id], exports, events)
  let result = exports.useTranslationProviders()
  for (const id of cloud) assert.equal(result.isAvailable(id), false, id)
  for (const id of ['google', 'mymemory', 'tiktok', 'capcut', 'ollama']) assert.equal(result.isAvailable(id), true, id)
  cleanup = effect()
  await new Promise(resolve => setImmediate(resolve))
  result = exports.useTranslationProviders()
  assert.equal(result.isAvailable('openrouter'), true)
  assert.equal(result.isAvailable('gemini'), false)
  assert.match(result.providerLabel('gemini', 'Gemini'), /API key not configured/)
  assert.equal(result.providerLabel('openrouter', 'OpenRouter'), 'OpenRouter')
  config = { cloud: { openrouter: { apiKeySet: false }, gemini: { apiKeySet: true } } }
  events.dispatchEvent(new Event('zm-cloud-config-changed'))
  await new Promise(resolve => setImmediate(resolve))
  result = exports.useTranslationProviders()
  assert.equal(result.isAvailable('openrouter'), false)
  assert.equal(result.isAvailable('gemini'), true)
  cleanup()
})

test('all translator pickers disable unconfigured providers', () => {
  for (const path of [
    'features/project/ProjectSidebar.tsx',
    'features/editor/EditorProjectPanel.tsx',
    'features/editor/EditorPropertiesPanel.tsx',
    'features/studio/CloneBatchSettingsPanel.tsx',
    'features/tts/TtsTranscribePanel.tsx',
    'pages/SrtExportPage.tsx',
  ]) {
    assert.match(read(path), /<option key=\{id\} value=\{id\} disabled=\{!isAvailable\(id\)\}/, path)
  }
  assert.match(read('features/project/project.api.ts'), /dispatchEvent\(new Event\('zm-cloud-config-changed'\)\)/)
  assert.match(read('features/tts/TtsTranscribePanel.tsx'), /!isAvailable\(translation.translator\)/)
  assert.match(read('features/tts/TtsTranscribePanel.tsx'), /api\/chat\/models\?provider=/)
})

test('AI review enforces keys even in whole-script mode and preserves Ollama', () => {
  const source = read('features/project/TranslationReview.tsx')
  const js = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
  let configured = []
  let effects = []
  const options = ['ollama', 'openrouter', 'gemini'].map(id => ({ id, label: id }))
  const jsx = (type, props) => ({ type, props })
  const modules = {
    'react/jsx-runtime': { jsx, jsxs: jsx },
    react: { useState: initial => [initial, () => {}], useEffect: fn => { effects.push(fn) } },
    '@/app/i18n': { useLocale: () => ({ locale: 'en' }), localize: (_locale, _vi, en) => en },
    '@/app/appSettings': { canReviewTranslatedDraft: () => true, translationReviewOptions: () => options },
    './useTranslationProviders': { useTranslationProviders: () => ({
      configuredProviders: configured,
      isAvailable: id => id === 'ollama' || configured.includes(id),
      providerLabel: (_id, label) => label,
    }) },
  }
  const exports = {}
  new Function('require', 'exports', js)(id => modules[id] || {}, exports)
  function nodes(tree) {
    if (!tree || typeof tree !== 'object') return []
    if (Array.isArray(tree)) return tree.flatMap(nodes)
    return [tree, ...nodes(tree.props?.children)]
  }
  function render(wholeScript, provider) {
    effects = []
    return nodes(exports.TranslationReviewSettings({
      settings: { translator: 'google', translationReviewMode: 'manual', translationReviewTranslator: provider },
      onChange: () => {}, wholeScript,
    }))
  }
  for (const wholeScript of [true, false]) {
    configured = ['gemini']
    const tree = render(wholeScript, 'openrouter')
    const picker = tree.find(node => node.type === 'select' && node.props['aria-label'])
    assert.equal(picker.props.value, '') // stale selection must not bypass the key check
    const choices = nodes(picker).filter(node => node.type === 'option')
    assert.equal(choices.find(node => node.props.value === 'openrouter').props.disabled, true)
    assert.equal(choices.find(node => node.props.value === 'gemini').props.disabled, false)
    assert.equal(choices.find(node => node.props.value === 'ollama').props.disabled, false)
    // No configured selected provider: the model effect must return before any API call.
    for (const effect of effects) effect()
    assert.equal(tree.filter(node => node.type === 'select').length, wholeScript ? 1 : 2)
  }
  configured = ['openrouter']
  assert.equal(render(true, 'openrouter').find(node => node.props?.['aria-label']).props.value, 'openrouter')
  configured = []
  assert.equal(render(true, 'ollama').find(node => node.props?.['aria-label']).props.value, 'ollama')
})
