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

function ttsWaitHarness(getProgress, status = 'connected') {
  const source = read('features/tts/TtsStudio.tsx')
  const snippet = source.slice(source.indexOf('  const waitForTtsJob = useCallback('),
                              source.indexOf('  const [duration, setDuration]'))
  const compiled = ts.transpileModule(snippet, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const intervals = new Map(), timers = new Map(), waiters = new Map()
  const connection = { current: status }
  const updates = []
  let id = 0
  const window = {
    setInterval: fn => { intervals.set(++id, fn); return id },
    clearInterval: id => intervals.delete(id),
    setTimeout: fn => { timers.set(++id, fn); return id },
    clearTimeout: id => timers.delete(id),
  }
  const wait = new Function('useCallback', 'locale', 'window', 'ttsProgressRef', 'ttsWaitersRef',
    'cancelledJobIdsRef', 'realtimeStatusRef', 'activeJobIdRef', 'api', 'setBusyProgress',
    'setBusyCustomMessage', 't', `${compiled}\nreturn waitForTtsJob`)(
      fn => fn, 'en', window, { current: new Map() }, { current: waiters },
      { current: new Set() }, connection, { current: 'job' }, { ttsStudioJobProgress: getProgress },
      value => updates.push(value), value => updates.push(value), (_vi, en) => en)
  return { wait, waiters, connection, intervals, timers, updates,
           tick: () => [...intervals.values()].forEach(fn => fn()) }
}

test('TTS uses SSE while connected and polls only during disconnection', async () => {
  let calls = 0, finishRead
  const h = ttsWaitHarness(() => {
    calls++
    return calls === 1 ? Promise.resolve({ pct: 10 }) : new Promise(resolve => { finishRead = resolve })
  })
  const result = h.wait('job')
  await new Promise(setImmediate)
  h.tick(); h.tick(); h.tick()
  assert.equal(calls, 1, 'only the initial snapshot is fetched while SSE is connected')
  h.connection.current = 'reconnecting'
  h.tick(); h.tick()
  assert.equal(calls, 2, 'a slow fallback request must not overlap with another request')
  h.connection.current = 'connected'
  h.tick()
  assert.equal(calls, 2, 'fallback stops as soon as SSE reconnects')
  h.waiters.get('job')({ done: true })
  assert.deepEqual(await result, { done: true })
  finishRead({ pct: 20, message: 'late update' })
  await new Promise(setImmediate)
  assert.deepEqual(h.updates, [10], 'late HTTP responses must not update a completed job')
  assert.equal(h.intervals.size, 0)
  assert.equal(h.timers.size, 0)
})

test('TTS snapshot recovers fast completion and fallback can finish without SSE', async () => {
  const fast = ttsWaitHarness(() => Promise.resolve({ done: true, resultJobId: 'cached' }))
  assert.equal((await fast.wait('job')).resultJobId, 'cached')
  assert.equal(fast.intervals.size, 0)
  assert.equal(fast.timers.size, 0)
  let calls = 0
  const h = ttsWaitHarness(() => Promise.resolve(++calls === 1 ? { pct: 1 } : { done: true }), 'fallback')
  const result = h.wait('job')
  await new Promise(setImmediate)
  h.tick()
  assert.deepEqual(await result, { done: true })
  assert.equal(calls, 2)
  assert.equal(h.intervals.size, 0)
  assert.equal(h.timers.size, 0)
})


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
