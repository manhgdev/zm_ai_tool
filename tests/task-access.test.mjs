import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/features/license/taskAccess.ts', import.meta.url), 'utf8')
const code = ts.transpile(source.replace("import { licenseApi } from './license.api'", ''), {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
})
function harness(valid = false) {
  const events = []
  const exports = {}
  vm.runInNewContext(code, { exports, Request, URL, Event, window: {
    location: new URL('http://localhost:5173/tts'), dispatchEvent: event => events.push(event.type),
  }, licenseApi: { status: async () => ({ valid }) } })
  return { ...exports, events }
}

test('only result-producing requests are tasks; navigation, settings and polling are not', () => {
  const { isTaskRequest } = harness()
  for (const path of [
    '/api/tts/studio/synthesize', '/api/tts/studio/transcribe?lang=vi', '/api/tts/studio/clone',
    '/api/projects/p/run', '/api/projects/p/dub', '/api/projects/p/export',
    '/api/projects/p/review-script', '/api/projects/p/segments/s/preview-tts',
    '/api/projects/p/segments/retranscribe-range', '/api/review/generate', '/api/queue',
    '/api/flow/jobs', '/api/flow/jobs/j/retry', '/api/flow/series/draft',
    '/api/flow/series/s/episodes/e/scenes/c/generate', '/api/flow/series/s/run',
    '/api/download/jobs', '/api/cleaner/jobs', '/api/media-compose/jobs',
    '/api/srt-export/jobs', '/api/drawing/jobs/batch', '/api/drawing/jobs/start',
    '/api/automation/jobs',
    '/api/automation/suggest-topics', '/api/automation/jobs/j/resume',
  ]) assert.equal(isTaskRequest(path, { method: 'POST' }), true, path)
  for (const path of [
    '/api/tts/studio/history', '/api/flow/accounts', '/api/chat/models',
    '/api/chat/conversations', '/api/chat/conversations/c/messages',
    '/api/chat/conversations/c/messages/m/retry',
    '/api/license/status', '/api/resources', '/api/system/pick-videos', '/api/upload',
    '/api/projects/p/settings', '/api/flow/assets', '/api/chat/accounts/a/login/p/poll',
    '/api/flow/jobs/j/cancel', '/api/system/update/check', '/api/system/update/install',
  ]) {
    assert.equal(isTaskRequest(path), false, path)
    assert.equal(isTaskRequest(path, { method: 'POST' }), false, path)
  }
  assert.equal(isTaskRequest('/api/flow/jobs'), false)
  assert.equal(isTaskRequest('/api/queue/j/action', { method: 'POST', body: '{"op":"retry"}' }), true)
  assert.equal(isTaskRequest('/api/queue/action', { method: 'POST', body: '{"op":"pause_all"}' }), false)
})

test('only ACCESS_REQUIRED on a task opens activation and preserves the response body', async () => {
  const { watchTaskAccess, events } = harness()
  const watched = watchTaskAccess(async () => Response.json({ detail: 'ACCESS_REQUIRED' }, { status: 403 }))
  await watched('/api/flow/jobs')
  await watched('/api/projects/p/settings', { method: 'POST' })
  await watched('/api/system/pick-videos', { method: 'POST' })
  assert.deepEqual(events, [])
  const response = await watched('/api/flow/jobs', { method: 'POST' })
  assert.deepEqual(events, ['zm-task-access-required'])
  assert.deepEqual(await response.json(), { detail: 'ACCESS_REQUIRED' })
  await watched('https://example.com/api/flow/jobs', { method: 'POST' })
  assert.equal(events.length, 1)
  await watchTaskAccess(async () => Response.json({ detail: 'OTHER_ERROR' }, { status: 403 }))('/api/flow/jobs', { method: 'POST' })
  await watchTaskAccess(async () => Response.json({ ok: true }))('/api/flow/jobs', { method: 'POST' })
  assert.equal(events.length, 1)
})

test('resource preparation requests activation only when an actual task starts without access', async () => {
  const blocked = harness()
  await assert.rejects(blocked.requireTaskAccess(), /ACCESS_REQUIRED/)
  assert.deepEqual(blocked.events, ['zm-task-access-required'])
  const active = harness(true)
  await active.requireTaskAccess()
  assert.deepEqual(active.events, [])
})
