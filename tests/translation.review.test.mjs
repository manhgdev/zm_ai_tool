import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/features/project/TranslationReview.tsx', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText

// Exercise the real component's event handlers with only React hooks and HTTP mocked.
function mount() {
  const slots = []
  let cursor = 0
  let effects = []
  const hooks = {
    useState(initial) {
      const i = cursor++
      if (!(i in slots)) slots[i] = initial
      return [slots[i], (value) => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }]
    },
    useRef(initial) {
      const i = cursor++
      return slots[i] ??= { current: initial }
    },
    useEffect(effect, deps) {
      const i = cursor++
      if (slots[i]?.deps.every((value, index) => value === deps[index])) return
      effects.push(() => { slots[i]?.cleanup?.(); slots[i] = { deps, cleanup: effect() } })
    },
  }
  const requests = []
  const api = { reviewTranslation(...args) { return new Promise((resolve, reject) => requests.push({ args, resolve, reject })) } }
  const exports = {}
  const require = (id) => {
    if (id === 'react') return hooks
    if (id === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }
    if (id.endsWith('/i18n')) return { useLocale: () => ({ locale: 'en' }), localize: (_locale, _vi, en) => en, localizePipelineMessage: (_locale, message) => message }
    if (id.endsWith('/appSettings')) return { translationReviewOptions: () => [{ id: 'ollama', label: 'Ollama' }], canReviewTranslatedDraft: (provider) => ['google', 'mymemory', 'tiktok', 'capcut'].includes(provider) }
    if (id.endsWith('/Icons')) return { IconWand: () => null }
    if (id === './project.api') return { api }
    throw new Error(`Unexpected import: ${id}`)
  }
  new Function('require', 'exports', compiled)(require, exports)
  const applied = []
  let props = {
    projectId: 'p1', segment: { id: 's1', source: 'Hello', translation: 'Xin chao' },
    settings: { translator: 'google', targetLang: 'vi', translationReviewMode: 'manual', translationReviewTranslator: 'ollama', ollamaMode: 'local', ollamaModel: 'qwen:7b', ollamaLocalTier: 'quality' },
    onApply: (translation) => applied.push(translation), children: null,
  }
  return {
    requests, applied,
    render(patch = {}) {
      props = { ...props, ...patch }
      cursor = 0
      let tree = exports.TranslationReviewField(props)
      if (effects.length) {
        const queue = effects; effects = []; queue.forEach((effect) => effect())
        cursor = 0; tree = exports.TranslationReviewField(props)
      }
      return tree
    },
  }
}

function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes)
  if (!tree || typeof tree !== 'object') return []
  return [tree, ...nodes(tree.props?.children)]
}
const button = (tree, label) => nodes(tree).find((node) => node.type === 'button' && (node.props['aria-label'] === label || node.props.children === label))
const settle = () => new Promise((resolve) => setImmediate(resolve))

test('AI review sends current model settings and only applies after confirmation', async () => {
  const ui = mount()
  button(ui.render(), 'Review translation with AI').props.onClick()
  assert.equal(button(ui.render(), 'Review translation with AI').props.disabled, true)
  assert.deepEqual(ui.requests[0].args.slice(0, 3), ['p1', 's1', {
    text: 'Hello', translation: 'Xin chao', targetLang: 'vi', translator: 'ollama',
    ollamaMode: 'local', ollamaModel: 'qwen:7b', ollamaLocalTier: 'quality',
  }])
  ui.requests[0].resolve({ translation: 'Xin chào' }); await settle()
  assert.deepEqual(ui.applied, [])
  button(ui.render(), 'Apply').props.onClick()
  assert.deepEqual(ui.applied, ['Xin chào'])
  assert.equal(button(ui.render(), 'Apply'), undefined)
})

test('switching segment or editing draft aborts and discards late AI responses', async () => {
  for (const next of [{ id: 's2', source: 'Bye', translation: 'Tam biet' }, { id: 's1', source: 'Hello', translation: 'Bản người dùng sửa' }]) {
    const ui = mount()
    button(ui.render(), 'Review translation with AI').props.onClick()
    ui.render({ segment: next })
    assert.equal(ui.requests[0].args[3].aborted, true)
    ui.requests[0].resolve({ translation: 'Stale text' }); await settle()
    assert.equal(button(ui.render(), 'Apply'), undefined)
    assert.deepEqual(ui.applied, [])
  }
})

test('AI suggestions can be dismissed; project jobs block applying suggestions', async () => {
  const ui = mount()
  button(ui.render(), 'Review translation with AI').props.onClick()
  ui.requests[0].resolve({ translation: 'Xin chào' }); await settle()
  button(ui.render({ disabled: true }), 'Apply').props.onClick()
  assert.deepEqual(ui.applied, [])
  button(ui.render({ disabled: false }), 'Dismiss').props.onClick()
  assert.equal(button(ui.render(), 'Apply'), undefined)
  assert.deepEqual(ui.applied, [])
})
