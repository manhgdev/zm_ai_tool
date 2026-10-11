import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import React from 'react'

const source = readFileSync(new URL('../frontend/src/realtime/backgroundJobs.ts', import.meta.url), 'utf8')
const exports = {}
new Function('exports', ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(exports)
const update = exports.updateBackgroundJobs
const event = (topic, type, payload, entityId = '') => ({ topic, type, payload, entityId })

test('F5 restores all running TTS and Flow jobs from authoritative snapshots', () => {
  let rows = update([], event('tts', 'snapshot', { jobs: { a: { running: true, pct: 42 }, b: { done: true, pct: 100 } } }))
  rows = update(rows, event('flow', 'snapshot', { jobs: [{ id: 'a', status: 'processing', progress: 12 }, { id: 'b', status: 'queued' }] }))
  assert.equal(rows.length, 3)
  assert.equal(rows[0].progress, 42)
  rows = update(rows, event('tts', 'tts.job.updated', { done: true, pct: 100 }, 'a'))
  assert.equal(rows.length, 2)
  rows = update(rows, event('flow', 'snapshot', { jobs: [] }))
  assert.deepEqual(rows, [])
})

test('newer task updates are ordered first', () => {
  let rows = update([], event('flow', 'flow.job.updated', { id: 'old', status: 'processing' }, 'old'))
  rows = update(rows, { ...event('tts', 'tts.job.updated', { running: true, pct: 1 }, 'new'), timestamp: 200 })
  rows = update(rows, { ...event('flow', 'flow.job.updated', { id: 'old', status: 'processing', progress: 2 }, 'old'), timestamp: 100 })
  assert.deepEqual(rows.sort((a, b) => b.updatedAt - a.updatedAt).map(row => row.id), ['new', 'old'])
})

test('updates replace rows, ignore account events, and remove failed or deleted jobs', () => {
  const start = event('download', 'download.job.updated', { status: 'running', progress: 20 }, 'x')
  let rows = update(update([], start), start)
  assert.equal(rows.length, 1)
  assert.equal(update(rows, event('flow', 'flow.account.updated', {})), rows)
  assert.equal(update(rows, event('download', 'download.job.updated', { status: 'error' }, 'x')).length, 0)
  assert.equal(update(rows, event('download', 'download.job.deleted', {}, 'x')).length, 0)
})

test('tool navigation retains stable keyed trees without eagerly opening other tools', () => {
  const source = readFileSync(new URL('../frontend/src/shared/components/RetainedPages.tsx', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
  let opened = []
  const exports = {}
  const jsx = (type, props, key) => ({ type, props, key })
  new Function('require', 'exports', compiled)(name => name === 'react'
    ? { useState: () => [opened, next => { opened = next }] }
    : { jsx, jsxs: jsx, Fragment: React.Fragment }, exports)
  const render = active => exports.default({ active, transient: ['clone'], children: (page, visible) => ({ page, visible }) }).props.children
  const first = render('tts')[0]
  assert.deepEqual(first.map(node => node.key), ['tts'])
  const switched = render('flow')[0]
  assert.deepEqual(switched.map(node => node.key), ['tts', 'flow'])
  assert.equal(switched[0].props.hidden, true)
  assert.equal(switched[0].props.style.display, 'none')
  assert.equal(switched[0].props.children.page, 'tts')
  const back = render('tts')[0]
  assert.equal(back[0].key, first[0].key)
  assert.equal(back[0].props.hidden, false)
  assert.equal(back[1].props.hidden, true)
  const editor = render('clone')
  assert.equal(editor[0].length, 2)
  assert.ok(editor[0].every(node => node.props.hidden))
  assert.equal(editor[1].props.children.page, 'clone')
})


test('background Flow completion cannot change another tool URL', () => {
  const source = readFileSync(new URL('../frontend/src/features/flow/flow.helpers.ts', import.meta.url), 'utf8')
  const body = source.match(/export function writeFlowRoutePanel\(panel: FlowRoutePanel\) \{([\s\S]*?)\n\}/)[1]
  let writes = 0
  const win = { location: { pathname: '/text-to-speech' }, history: { pushState: () => { writes++ } } }
  new Function('window', 'panel', body)(win, 'queue')
  assert.equal(writes, 0)
})

test('realtime subscriptions stay stable on status changes and discard pre-snapshot replay', () => {
  const source = readFileSync(new URL('../frontend/src/realtime/RealtimeProvider.tsx', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source.replace('import.meta.env.VITE_API_BASE_URL', "''"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText
  let context
  let dependencies
  let stream
  const react = {
    createContext: () => ({ Provider: 'provider' }), useContext: () => context,
    useCallback: fn => fn, useMemo: fn => fn(), useRef: current => ({ current }),
    useState: value => [value, () => {}], useEffect: (_fn, deps) => { dependencies = deps },
  }
  class EventSource {
    static CLOSED = 2
    readyState = 1
    listeners = {}
    constructor() { stream = this }
    close() {}
    addEventListener(type, handler) { this.listeners[type] = handler }
  }
  const exports = {}
  new Function('require', 'exports', 'EventSource', compiled)(name => name === 'react' ? react : {
    jsx: (_type, props) => props,
  }, exports, EventSource)
  context = exports.RealtimeProvider({ children: null }).value
  const received = []
  const handler = event => received.push(event)
  context.subscribe('flow', handler)
  exports.useRealtimeEvents('flow', handler)
  const before = dependencies
  context = { ...context, status: 'connected' }
  exports.useRealtimeEvents('flow', handler)
  assert.deepEqual(dependencies, before)
  const emit = (type, id, payload) => stream.listeners[type]({ data: JSON.stringify({
    type, id: String(id), topic: 'flow', entityId: 'a', payload,
  }) })
  emit('snapshot', 10, { jobs: [{ id: 'a', status: 'done' }] })
  assert.deepEqual(received.map(event => event.type), ['snapshot'], 'snapshots are delivered once without row replay')
  emit('flow.job.updated', 8, { id: 'a', status: 'processing' })
  assert.ok(received.every(event => event.payload?.status !== 'processing'))
  emit('flow.job.updated', 11, { id: 'a', status: 'processing' })
  emit('flow.job.updated', 12, { id: 'a', status: 'done' })
  assert.deepEqual(received.reduce(update, []), [])
  emit('flow.job.updated', 11, { id: 'a', status: 'processing' })
  emit('snapshot', 9, { jobs: [{ id: 'a', status: 'processing' }] })
  assert.deepEqual(received.reduce(update, []), [], 'late progress and stale snapshots cannot resurrect completed jobs')
  emit('flow.job.updated', 13, { id: 'a', status: 'processing' })
  assert.equal(received.reduce(update, []).length, 1)
  emit('snapshot', 14, { jobs: [] })
  assert.deepEqual(received.reduce(update, []), [], 'periodic snapshots remove jobs without a delete event')
  emit('flow.job.updated', 13, { id: 'a', status: 'processing' })
  assert.deepEqual(received.reduce(update, []), [], 'pre-snapshot replay cannot restore a missing job')
})

test('periodic TTS snapshots resolve the active job and do not repeat error logs', () => {
  const source = readFileSync(new URL('../frontend/src/features/tts/TtsStudio.tsx', import.meta.url), 'utf8')
  const snippet = source.slice(source.indexOf('  const onTtsRealtime = useCallback('), source.indexOf('  const realtimeStatus = useRealtimeEvents'))
  const compiled = ts.transpileModule(snippet, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const progress = new Map(), waiters = new Map(), logs = []
  let finished
  waiters.set('active', value => { finished = value })
  const receive = new Function('useCallback', 'activeJobIdRef', 'cancelledJobIdsRef', 'ttsProgressRef',
    'ttsWaitersRef', 'setStatus', 'setBusyProgress', 'setBusyCustomMessage', 'setError', 'setTtsLogs',
    `${compiled}\nreturn onTtsRealtime`)(fn => fn, { current: 'active' }, { current: new Set() },
    { current: progress }, { current: waiters }, () => {}, () => {}, () => {}, () => {},
    update => { logs.splice(0, logs.length, ...update(logs)) })
  const snapshot = { type: 'snapshot', entityId: 'snapshot:tts', payload: { jobs: { active: { error: 'failed', pct: 50 } } } }
  receive(snapshot)
  receive(snapshot)
  assert.equal(finished.error, 'failed')
  assert.equal(progress.get('active').pct, 50)
  assert.equal(logs.length, 1)
  receive({ ...snapshot, payload: { jobs: { active: { done: true, pct: 100 } } } })
  assert.equal(finished.done, true)
  assert.equal(progress.has('snapshot:tts'), false)
})
