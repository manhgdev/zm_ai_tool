import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

test('TTS model logs skip ready states and deduplicate unchanged diagnostics', () => {
  const source = readFileSync(new URL('../frontend/src/features/tts/TtsStudio.tsx', import.meta.url), 'utf8')
  const effect = source.match(/const lastModelLogRef = useRef\(''\)\s+useEffect\(\(\) => \{([\s\S]*?)\n  \}, \[vieneu, locale\]\)/)[1]
  const run = new Function('vieneu', 'locale', 'lastModelLogRef', 'localize', 'setTtsLogs', effect)
  let logs = []
  const last = { current: '' }
  const apply = status => run(status, 'en', last, (_locale, _vi, en) => en, update => { logs = update(logs) })
  apply({ ready: true })
  apply({ loadState: 'ready' })
  assert.equal(logs.length, 0)
  const loading = { installed: true, loadState: 'loading', model: 'VieNeu', device: 'CPU' }
  apply(loading)
  apply(loading)
  assert.equal(logs.length, 1)
  apply({ ...loading, loadState: 'error', error: 'full ONNX diagnostic' })
  assert.match(logs[1], /Model load error.*VieNeu.*CPU\nfull ONNX diagnostic/)
  apply({ ready: true })
  apply(loading)
  assert.equal(logs.length, 3)
})

test('TTS job IDs work on public HTTP without randomUUID', () => {
  const source = readFileSync(new URL('../frontend/src/features/tts/TtsStudio.tsx', import.meta.url), 'utf8')
  const helper = source.match(/function createTtsJobId\(\) \{([\s\S]*?)\n\}/)[1]
  const createId = new Function('crypto', helper)
  const crypto = { getRandomValues: array => globalThis.crypto.getRandomValues(array) }
  const ids = new Set(Array.from({ length: 100 }, () => createId(crypto)))
  assert.equal(ids.size, 100)
  for (const id of ids) assert.match(id, /^[a-f0-9]{12}$/)
})

test('TTS restores the saved voice only after the full catalog loads', () => {
  const app = readFileSync(new URL('../frontend/src/app/App.tsx', import.meta.url), 'utf8')
  const page = readFileSync(new URL('../frontend/src/pages/TtsPage.tsx', import.meta.url), 'utf8')
  const studio = readFileSync(new URL('../frontend/src/features/tts/TtsStudio.tsx', import.meta.url), 'utf8')
  assert.match(app, /const \[voicesLoaded, setVoicesLoaded\] = useState\(false\)/)
  assert.match(app, /setVoicesLoaded\(true\)/)
  assert.match(app, /onRefreshVoices=\{refreshVoices\}/)
  assert.match(page, /voicesLoaded: boolean/)
  assert.match(studio, /if \(!voicesLoaded\) return/)
  assert.match(studio, /disabled=\{!voicesLoaded\}/)
  assert.match(studio, /lang === 'auto' \|\| v\.id === 'system'/)
})

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')


test('transcription rewrite uses app navigation and retains the prefill', () => {
  const studio = read('features/tts/TtsStudio.tsx')
  assert.match(studio, /onRewrite=\{[^\n]+chat-prefill:v1[^\n]+onOpenChat\?\.\(\)/)
  assert.doesNotMatch(studio, /window\.location\.href\s*=\s*['"]\/chat/)
  assert.match(read('pages/TtsPage.tsx'), /onOpenChat=\{onOpenChat\}/)
  assert.match(read('app/App.tsx'), /onOpenChat=\{\(\) => navigateToMode\('chat'\)\}/)
})


test('mode synchronization preserves the current query and Chat provides a visible return path', () => {
  assert.match(read('app/App.tsx'), /appModeFromPath\(window.location.pathname\) === appMode \? window.location.search/)
  const chat = read('pages/ChatPage.tsx')
  assert.match(chat, /className="chat-back" onClick=\{onBack\}/)
  assert.match(chat, /if \(!response.ok\)/)
  assert.match(chat, /catch\(reason => setError\(errorText/)
  assert.match(chat, /Chat service was not found \(404\)/)
})

test('changing recognition engine leaves existing results and selected file intact', () => {
  const studio = read('features/tts/TtsStudio.tsx')
  const body = studio.match(/const handleTranscribeEngineChange = useCallback\(\(engine: TranscribeEngine\) => \{([\s\S]*?)\n  \}, \[\]\)/)[1]
  const changes = []
  const choose = new Function('engine', 'setTranscribeEngine', body)
  // Any attempt to call an unrelated setter or clear results fails this handler check.
  choose('whisper', engine => changes.push(engine))
  choose('capcut', engine => changes.push(engine))
  assert.deepEqual(changes, ['whisper', 'capcut'])
})
