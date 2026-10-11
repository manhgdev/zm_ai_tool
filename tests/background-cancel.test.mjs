import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

function client(fetchJson) {
  const source = readFileSync(new URL('../frontend/src/realtime/cancelBackgroundJob.ts', import.meta.url), 'utf8')
  const exports = {}
  new Function('require', 'exports', ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(() => ({ fetchJson }), exports)
  return exports.cancelBackgroundJob
}

test('every background topic sends cancellation to its backend with the exact job id', async () => {
  const calls = []
  let fail = false
  const cancel = client(async (...args) => { calls.push(args); if (fail) throw new Error('offline'); return { ok: true } })
  const routes = { tts: 'tts/studio/jobs', flow: 'flow/jobs', download: 'download/jobs', drawing: 'drawing/jobs', automation: 'automation/jobs', cleaner: 'cleaner/jobs', 'srt-image': 'media-compose/jobs', 'srt-export': 'srt-export/jobs', project: 'projects' }
  for (const [topic, route] of Object.entries(routes)) {
    await cancel({ topic, id: 'job/123' })
    assert.deepEqual(calls.at(-1), [`/api/${route}/job%2F123/cancel`, { method: 'POST' }])
  }
  fail = true
  await assert.rejects(cancel({ topic: 'flow', id: 'x' }), /offline/)
  assert.throws(() => cancel({ topic: 'unknown', id: 'x' }), /Unsupported/)
})

test('missing Flow job is marked removed only after a fresh authoritative read', async () => {
  const calls = []
  const cancel = client(async (...args) => {
    calls.push(args)
    if (args[1].method === 'POST') throw new Error('Flow job not found')
    return { jobs: [{ id: 'other-job', status: 'processing' }] }
  })
  assert.deepEqual(await cancel({ topic: 'flow', id: 'old-job' }), { alreadyRemoved: true })
  assert.deepEqual(calls, [
    ['/api/flow/jobs/old-job/cancel', { method: 'POST' }],
    ['/api/flow/jobs', { method: 'GET' }],
  ])
})

test('existing jobs and invalid snapshots do not hide a cancellation error', async () => {
  for (const snapshot of [{ jobs: [{ id: 'x' }] }, {}, { jobs: null }, { jobs: [null] }, { jobs: [{}] }]) {
    const cancel = client(async (_url, init) => {
      if (init.method === 'POST') throw new Error('Flow job not found')
      return snapshot
    })
    await assert.rejects(cancel({ topic: 'flow', id: 'x' }), /Flow job not found/)
  }
})

test('snapshot failure leaves the missing job unresolved', async () => {
  const cancel = client(async (_url, init) => {
    throw new Error(init.method === 'POST' ? 'Flow job not found' : 'offline')
  })
  await assert.rejects(cancel({ topic: 'flow', id: 'x' }), /offline/)
})

test('other cancellation failures never trigger missing-job recovery', async () => {
  for (const [topic, message] of [['flow', 'offline'], ['flow', 'Forbidden'], ['tts', 'Flow job not found']]) {
    let calls = 0
    const cancel = client(async () => { calls++; throw new Error(message) })
    await assert.rejects(cancel({ topic, id: 'x' }), { message })
    assert.equal(calls, 1)
  }
})
