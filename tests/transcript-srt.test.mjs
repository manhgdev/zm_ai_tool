import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const path = new URL('../frontend/src/features/tts/lib/transcriptSrt.ts', import.meta.url)
const js = ts.transpileModule(readFileSync(path, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const { formatTranscriptSrt: format } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const stylesJs = ts.transpileModule(readFileSync(new URL('../frontend/src/features/tts/lib/srt.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const styles = await import(`data:text/javascript;base64,${Buffer.from(stylesJs).toString('base64')}`)
const cue = (i, start, end, text) => `${i}\n00:00:${start} --> 00:00:${end}\n${text}`
const input = [cue(1, '00,000', '00,380', 'hê lô Anh'), cue(2, '00,380', '01,500', 'em hôm Nay mình'), cue(3, '01,500', '02,100', 'sẽ hướng dẫn.')].join('\n\n')

test('original SRT stays byte-for-byte unchanged for Whisper and CapCut', () => {
  assert.equal(format(input, 'original'), input)
  assert.equal(format(input, 'original', 'capcut'), input)
  assert.equal(format(input, 'sentence'), cue(1, '00,000', '02,100', 'hê lô Anh em hôm Nay mình sẽ hướng dẫn.'))
  assert.notEqual(format(input, 'v916'), format(input, 'sentence'))
  for (const style of ['hard', 'v916', 'h169', 'clause', 'sentence']) {
    const text = format(input, style)
    for (const time of text.match(/\d{2}:\d{2}:\d{2},\d{3}/g)) assert.ok(input.includes(time))
    const words = s => s.split('\n').filter(l => l && !/^\d+$/.test(l) && !l.includes('-->')).join(' ')
    assert.equal(words(text), words(input))
  }
})
test('original keeps adjacent CapCut cues and pause boundaries', () => {
  const raw = [cue(1, '00,000', '00,500', 'Hello'), cue(2, '00,500', '01,500', 'there'), cue(3, '02,300', '03,000', 'next')].join('\n\n')
  assert.equal(format(raw, 'original'), raw)
  assert.equal(format(raw, 'original', 'capcut'), raw)
})
test('original keeps long CapCut cues intact without changing timing', () => {
  const raw = [
    cue(1, '00,000', '04,740', 'hê lô Anh em hôm Nay mình sẽ hướng dẫn'),
    cue(2, '04,740', '09,820', 'Anh tức giận cậu định Ra Giao ước BA ngày'),
  ].join('\n\n')
  assert.equal(format(raw, 'original', 'capcut'), raw)
})
test('punctuation, pauses and overlaps remain boundaries', () => {
  for (const [end, start, text] of [['00,300', '00,400', 'Hello.'], ['00,300', '01,000', 'Hello'], ['00,500', '00,400', 'Hello']]) {
    const raw = [cue(1, '00,000', end, text), cue(2, start, '02,000', 'world')].join('\n\n')
    assert.equal(format(raw, 'sentence'), raw)
  }
})
test('long indivisible cues and incomplete edits are never split or lost', () => {
  const raw = cue(1, '00,000', '09,820', 'long '.repeat(50).trim())
  assert.equal(format(raw, 'v916').split('\n')[1], raw.split('\n')[1])
  assert.equal(format(raw, 'v916').split('\n').slice(2).join(' '), raw.split('\n')[2])
  assert.equal(format(raw, 'v916').split('-->').length, 2)
  assert.equal(format('partial edit', 'hard'), 'partial edit')
  assert.equal(format(input.replaceAll('\n', '\r\n'), 'sentence'), format(input, 'sentence'))
})
test('preview, download and TTS share one value and TTS keeps timeline', () => {
  const panel = readFileSync(new URL('../frontend/src/features/tts/TtsTranscribePanel.tsx', import.meta.url), 'utf8')
  assert.ok(panel.includes('value={outputText}'))
  assert.ok(panel.includes("useState<TranscriptStyle>('original')"))
  assert.ok(panel.includes('new Blob([srtPreview]'))
  assert.ok(panel.includes("resultTab === 'srt' ? srtPreview : txtPreview"))
  const studio = readFileSync(new URL('../frontend/src/features/tts/TtsStudio.tsx', import.meta.url), 'utf8')
  assert.match(studio, /setSrtRaw\(content\)\s+setKeepTimeline\(true\)/)
  assert.ok(studio.includes('localStorage.setItem(TTS_SRT_LS_KEY, content)'))
})

// Same lightweight component-handler harness used by translation.review.test.mjs.
function mountPanel(overrides = {}) {
  const slots = []
  let cursor = 0
  const storage = new Map()
  const applied = []
  const hooks = {
    useState(initial) {
      const i = cursor++
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial
      return [slots[i], value => { slots[i] = value }]
    },
    useEffect: effect => effect(),
    useMemo: fn => fn(),
  }
  const source = readFileSync(new URL('../frontend/src/features/tts/TtsTranscribePanel.tsx', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
  const exports = {}
  const require = id => {
    if (id === 'react') return hooks
    if (id === 'react/jsx-runtime') return { jsx: (type, props, key) => ({ type, props, key }), jsxs: (type, props, key) => ({ type, props, key }) }
    if (id.endsWith('/i18n')) return { useLocale: () => ({ locale: 'en' }), localize: (_locale, _vi, en) => en }
    if (id.endsWith('/configModal.helpers')) return { PROVIDER_PRESET_MODELS: {} }
    if (id === './TtsIcons') return { IconUpload: () => null }
    if (id === './lib/srt') return styles
    if (id === './lib/transcriptSrt') return { formatTranscriptSrt: format }
    throw new Error(`Unexpected import: ${id}`)
  }
  new Function('require', 'exports', 'localStorage', compiled)(require, exports, {
    getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
  })
  const props = {
    file: null, lang: 'auto', engine: 'capcut', busy: false,
    resultText: 'Source text', resultSrt: input, translatedText: 'Translated text',
    translatedSrt: cue(1, '00,000', '02,100', 'Translated.'),
    translation: { enabled: true, targetLang: 'en', translator: 'openai', model: '' },
    onApplyToTts: (...args) => applied.push(args), ...overrides,
  }
  return { applied, storage, props, render: () => { cursor = 0; return nodes(exports.default(props)) } }
}
function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes)
  if (!tree || typeof tree !== 'object') return []
  return [tree, ...nodes(tree.props?.children)]
}
test('each result tab switches the visible textarea, selection and TTS content together', () => {
  const ui = mountPanel()
  for (const [label, expected, kind] of [
    ['Source SRT', format(input, 'original', 'capcut'), 'srt'], ['Source TXT', 'hê lô Anh\nem hôm Nay mình\nsẽ hướng dẫn.', 'txt'],
    ['Translated SRT', format(ui.props.translatedSrt, 'original', 'capcut'), 'srt'], ['Translated TXT', 'Translated.', 'txt'],
  ]) {
    ui.render().find(n => n.props.role === 'tab' && n.props.children === label).props.onClick()
    const tree = ui.render()
    assert.equal(tree.find(n => n.type === 'textarea').props.value, expected)
    assert.equal(tree.filter(n => n.props.role === 'tab' && n.props['aria-selected']).length, 1)
    assert.equal(tree.find(n => n.props.role === 'tab' && n.props['aria-selected']).props.children, label)
    tree.find(n => n.type === 'button' && n.props.children === 'Send to Create voice').props.onClick()
    assert.deepEqual(ui.applied.at(-1), [kind, expected])
    assert.equal(ui.storage.get('tts-result-format'), kind)
  }
})
test('legacy TXT-only result allows selecting SRT and shows an explicit empty state', () => {
  const ui = mountPanel({ resultSrt: '' })
  const tab = ui.render().find(n => n.props.role === 'tab' && n.props.children === 'Source SRT')
  assert.ok(!tab.props.disabled)
  tab.props.onClick()
  const tree = ui.render()
  const editor = tree.find(n => n.type === 'textarea')
  assert.equal(editor.props.value, '')
  assert.match(editor.props.placeholder, /No SRT/)
  assert.equal(tree.find(n => n.type === 'button' && n.props.children === 'Send to Create voice').props.disabled, true)
})

test('all engines default to original cues and switching engine never regroups existing output', () => {
  for (const engine of ['whisper', 'capcut', 'paddleocr', 'subtitle']) {
    const ui = mountPanel({ engine })
    ui.render().find(n => n.props.id === 'tts-source-srt').props.onClick()
    for (const nextEngine of [engine, 'whisper', 'capcut']) {
      ui.props.engine = nextEngine
      const tree = ui.render()
      assert.equal(tree.find(n => n.props['aria-label'] === 'SRT style').props.value, 'original')
      const editor = tree.find(n => n.type === 'textarea')
      assert.equal(editor.props.value, format(input, 'original', nextEngine))
      assert.equal(editor.props.readOnly, false)
    }
  }
})

test('returning from a styled preview to original restores every cue without modifying the source', () => {
  const ui = mountPanel()
  ui.render().find(n => n.props.id === 'tts-source-srt').props.onClick()
  ui.render().find(n => n.props['aria-label'] === 'SRT style').props.onChange({ target: { value: 'sentence' } })
  assert.equal(ui.render().find(n => n.type === 'textarea').props.value, format(input, 'sentence'))
  ui.render().find(n => n.props['aria-label'] === 'SRT style').props.onChange({ target: { value: 'original' } })
  const tree = ui.render()
  assert.equal(tree.find(n => n.type === 'textarea').props.value, format(input, 'original', 'capcut'))
  assert.equal(ui.props.resultSrt, input)
  tree.find(n => n.type === 'button' && n.props.children === 'Send to Create voice').props.onClick()
  assert.deepEqual(ui.applied.at(-1), ['srt', format(input, 'original', 'capcut')])
})

test('Whisper style changes update visible SRT lines, viewing label and TTS without changing cue time', () => {
  const text = 'Hello everyone, today we are learning how to create clear subtitles for this video. Keep all of these words intact!'
  const raw = cue(1, '00,000', '09,820', text)
  const timedStyles = { sourceSrt: raw, styles: Object.fromEntries(['hard', 'v916', 'h169', 'clause', 'sentence'].map((style, index) => [style, [cue(1, '00,000', '04,000', `${style} first part`), cue(2, '04,000', '09,820', `${style} second part`)].join('\n\n')])) }
  const ui = mountPanel({ engine: 'whisper', resultSrt: raw, timedStyles })
  ui.render().find(n => n.props.id === 'tts-source-srt').props.onClick()
  const previews = new Set()
  for (const style of ['hard', 'v916', 'h169', 'clause', 'sentence', 'original']) {
    ui.render().find(n => n.props['aria-label'] === 'SRT style').props.onChange({ target: { value: style } })
    const tree = ui.render()
    const preview = tree.find(n => n.type === 'textarea').props.value
    assert.equal(preview, style === 'original' ? raw : timedStyles.styles[style])
    assert.equal(preview.split('-->').length, style === 'original' ? 2 : 3)
    assert.match(tree.find(n => n.props.className === 'tts-result-view-label').props.children.at(-1), / · .+/)
    tree.find(n => n.type === 'button' && n.props.children === 'Send to Create voice').props.onClick()
    assert.deepEqual(ui.applied.at(-1), ['srt', preview])
    previews.add(preview)
  }
  assert.equal(previews.size, 6)
  assert.equal(ui.props.resultSrt, raw)
})

test('short Whisper cue explains a style that produces identical output', () => {
  const ui = mountPanel({ engine: 'whisper', resultSrt: cue(1, '00,000', '01,500', 'Hello.') })
  ui.render().find(n => n.props.id === 'tts-source-srt').props.onClick()
  ui.render().find(n => n.props['aria-label'] === 'SRT style').props.onChange({ target: { value: 'v916' } })
  const hints = ui.render().filter(n => n.props.className === 'tts-transcribe-hint')
  assert.match(String(hints.at(-1).props.children), /No matching word timestamps/)
})

test('original Whisper preserves edited line breaks and CRLF byte for byte', () => {
  const raw = cue(1, '00,000', '09,820', 'Hello\nworld.').replaceAll('\n', '\r\n')
  assert.equal(format(raw, 'original', 'whisper'), raw)
})
