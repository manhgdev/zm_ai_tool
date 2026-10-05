import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

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
