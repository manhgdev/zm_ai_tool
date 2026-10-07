import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

test('every background topic sends cancellation to its backend with the exact job id', async () => {
  const source = readFileSync(new URL('../frontend/src/realtime/cancelBackgroundJob.ts', import.meta.url), 'utf8')
  const exports = {}
  const calls = []
  let fail = false
  new Function('require', 'exports', ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(() => ({
    fetchJson: async (...args) => { calls.push(args); if (fail) throw new Error('offline'); return { ok: true } },
  }), exports)
  const routes = { tts: 'tts/studio/jobs', flow: 'flow/jobs', download: 'download/jobs', drawing: 'drawing/jobs', automation: 'automation/jobs', cleaner: 'cleaner/jobs', 'srt-image': 'media-compose/jobs', 'srt-export': 'srt-export/jobs', project: 'projects' }
  for (const [topic, route] of Object.entries(routes)) {
    await exports.cancelBackgroundJob({ topic, id: 'job/123' })
    assert.deepEqual(calls.at(-1), [`/api/${route}/job%2F123/cancel`, { method: 'POST' }])
  }
  fail = true
  await assert.rejects(exports.cancelBackgroundJob({ topic: 'flow', id: 'x' }), /offline/)
  assert.throws(() => exports.cancelBackgroundJob({ topic: 'unknown', id: 'x' }), /Unsupported/)
})
