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

function resourceHarness({ installed = [], restart = false, fail = false } = {}) {
  const exports = {}, events = [], requests = [], ready = new Set(installed)
  const output = ts.transpileModule(read('features/project/project.api.ts'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  new Function('require', 'exports', 'window', 'CustomEvent', output)(
    () => ({ fetchJson: async (url, init) => { requests.push(url); return { ok: true } } }),
    exports, { dispatchEvent: event => { events.push(event.detail) } },
    class { constructor(_type, { detail }) { this.detail = detail } },
  )
  exports.api.resources = async () => ({ items: ['whisper', 'ocr', 'diarization', 'demucs', 'moss_tts_nano'].map(id => ({ id, installed: ready.has(id) })) })
  exports.api.ttsStatus = async () => ({ vieneu: { installed: ready.has('vieneu') } })
  exports.api.installResource = async (id, onStatus) => {
    assert.equal(events.at(-1).running, true, 'package screen opens before installation')
    requests.push(`install:${id}`)
    onStatus({ running: true, progress: 50, log: `Installing ${id}` })
    await new Promise(setImmediate)
    if (fail) throw new Error('DOWNLOAD_FAILED')
    ready.add(id)
    return { ok: true, needsRestart: restart }
  }
  return { ...exports, events, requests }
}

test('optional features install on first use, serially and only once', async () => {
  const h = resourceHarness()
  await Promise.all([
    h.prepareAiResources(['whisper', 'ocr', 'diarization']),
    h.prepareAiResources(['whisper', 'demucs', 'moss_tts_nano']),
  ])
  assert.deepEqual(h.requests, ['install:whisper', 'install:ocr', 'install:diarization', 'install:demucs', 'install:moss_tts_nano'])
  assert.equal(h.events.at(-1), null)
  const installed = resourceHarness({ installed: ['whisper'] })
  await installed.prepareAiResources(['whisper'])
  assert.deepEqual(installed.requests, [])
  assert.deepEqual(installed.events, [])
})

test('failed or restart-required package setup stops the feature request', async () => {
  const failed = resourceHarness({ fail: true })
  await assert.rejects(failed.api.run('project', { engine: 'whisper', burnSubs: false, coverHardsubs: false }), /DOWNLOAD_FAILED/)
  assert.deepEqual(failed.requests, ['install:whisper'])
  assert.equal(failed.events.at(-1).running, false)
  const restart = resourceHarness({ restart: true })
  const jobs = await Promise.allSettled([restart.prepareAiResources(['ocr']), restart.prepareAiResources(['whisper'])])
  assert.ok(jobs.every(job => job.status === 'rejected' && job.reason.message === 'RUNTIME_RESTART_REQUIRED'))
  assert.deepEqual(restart.requests, ['install:ocr'])
  assert.equal(restart.events.at(-1).needsRestart, true)
})

test('shared entry points prepare recognition and separation packages before starting work', async () => {
  const h = resourceHarness()
  assert.deepEqual(h.recognitionResources({ engine: 'capcut', burnSubs: false, coverHardsubs: false }), [])
  assert.deepEqual(h.recognitionResources({ engine: 'capcut', burnSubs: true, coverHardsubs: false, speakerDiarization: true }), ['ocr', 'diarization'])
  assert.deepEqual(h.audioResources({ processOriginalAudio: true, originalAudioMode: 'no_vocals' }), ['demucs'])
  await h.api.run('project', { engine: 'whisper', burnSubs: false, coverHardsubs: false })
  await h.api.prepareNoVocals('project')
  assert.deepEqual(h.requests, ['install:whisper', '/api/projects/project/run', 'install:demucs', '/api/projects/project/audio/no-vocals'])
  assert.match(read('features/studio/studio.api.ts'), /if \(startNow\) await prepareJob/)
  assert.match(read('pages/SrtExportPage.tsx'), /kind === 'media'.*recognitionEngine === 'whisper'.*prepareAiResources/)
  assert.match(read('features/tts/TtsStudio.tsx'), /v\.engine === 'moss_tts_nano'.*prepareOptionalResource/)
})

test('MOSS synthesis prepares its own package while cloud and system voices skip local setup', async () => {
  const h = resourceHarness()
  h.api.voices = async () => [
    { id: 'moss:reference', engine: 'moss_tts_nano' },
    { id: 'mac:Alex', engine: 'system' },
  ]
  await h.api.ttsStudioSynth({ voice: 'moss:reference', text: 'test' })
  await h.api.ttsStudioSynth({ voice: 'cc:cloud', text: 'test' })
  await h.api.ttsStudioSynth({ voice: 'system', text: 'test' })
  await h.api.ttsStudioSynth({ voice: 'mac:Alex', text: 'test' })
  assert.deepEqual(h.requests, ['install:moss_tts_nano', ...Array(4).fill('/api/tts/studio/synthesize')])
})

function prepareVoiceHarness({ installed = false, restart = false, fail = false, local = true, voice = 'vn:test' } = {}) {
  const source = read('features/tts/TtsStudio.tsx')
  const snippet = source.slice(source.indexOf('  async function prepareVieneuVoice'), source.indexOf('  async function onSynth'))
  const compiled = ts.transpileModule(snippet, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const events = [], states = []
  let state = null, available = installed
  const api = {
    ttsStatus: async () => { events.push('status'); return { vieneu: { installed: available } } },
    installResource: async (id, onStatus) => {
      events.push(`install:${id}`)
      assert.equal(state.running, true, 'installation screen opens before the download request')
      onStatus({ running: true, progress: 50, log: 'Installing VieNeu' })
      if (fail) throw new Error('NETWORK_TIMEOUT')
      available = true
      return { ok: true, needsRestart: restart }
    },
    voices: async () => [{ id: 'vn:first', engine: 'vieneu' }],
  }
  const prepare = new Function('usesVieneu', 'voice', 'api', 'setInstallMinimized', 'setVieneuInstall', 't',
    'setStatus', 'onRefreshVoices', 'lang', 'preferredVoiceRef', 'setVoice', `${compiled}\nreturn prepareVieneuVoice`)(
      local, voice, api, () => events.push('open'), update => { state = typeof update === 'function' ? update(state) : update; states.push(state) },
      (_vi, en) => en, () => events.push('updated'), () => events.push('voices'), 'vi', { current: '' }, value => events.push(`voice:${value}`))
  return { prepare, events, states }
}

test('first VieNeu use opens progress, installs, and prepares synthesis', async () => {
  const h = prepareVoiceHarness()
  assert.equal(await h.prepare(), 'vn:test')
  assert.deepEqual(h.events, ['status', 'open', 'install:vieneu', 'status', 'updated', 'voices'])
  assert.equal(h.states[1].progress, 50)
  assert.equal(h.states[1].log, 'Installing VieNeu')
  assert.equal(h.states.at(-1), null)
  const empty = prepareVoiceHarness({ voice: '' })
  assert.equal(await empty.prepare(), 'vn:first')
  assert.ok(empty.events.includes('voice:vn:first'))
})

test('installed VieNeu and cloud voices do not trigger package installation', async () => {
  const local = prepareVoiceHarness({ installed: true })
  assert.equal(await local.prepare(), 'vn:test')
  assert.deepEqual(local.events, ['status'])
  const cloud = prepareVoiceHarness({ local: false, voice: 'cc:test' })
  assert.equal(await cloud.prepare(), 'cc:test')
  assert.deepEqual(cloud.events, [])
})

test('VieNeu install failure or required restart stops synthesis preparation', async () => {
  const restart = prepareVoiceHarness({ restart: true })
  assert.equal(await restart.prepare(), null)
  assert.equal(restart.states.at(-1).needsRestart, true)
  const failure = prepareVoiceHarness({ fail: true })
  await assert.rejects(failure.prepare(), /NETWORK_TIMEOUT/)
  assert.equal(failure.states.at(-1).running, false)
  assert.equal(failure.states.at(-1).error, 'NETWORK_TIMEOUT')
  assert.match(read('features/tts/TtsStudio.tsx'), /const preparedVoice = await prepareVieneuVoice\(\)[\s\S]*?if \(!preparedVoice[^\n]+return[\s\S]*?await api\.ttsStudioSynth/)
})

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

test('long TTS jobs have no wall-clock deadline and still finish or cancel', async () => {
  const h = ttsWaitHarness(() => Promise.resolve({ pct: 30 }))
  const result = h.wait('job')
  await new Promise(setImmediate)
  assert.equal(h.timers.size, 0, 'local synthesis must not expire after ten minutes')
  // 420 five-second ticks represent 35 minutes of an active SSE job.
  for (let tick = 0; tick < 420; tick++) h.tick()
  assert.equal(h.waiters.size, 1)
  h.waiters.get('job')({ done: true })
  assert.deepEqual(await result, { done: true })
  assert.equal(h.intervals.size, 0)
  const cancelled = ttsWaitHarness(() => Promise.resolve({ pct: 1 }))
  const pending = cancelled.wait('job')
  cancelled.waiters.get('job')({ error: 'cancelled' })
  assert.deepEqual(await pending, { error: 'cancelled' })
  assert.equal(cancelled.intervals.size, 0)
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
