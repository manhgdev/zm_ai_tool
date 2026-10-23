import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')
function harness() {
  const events = []
  const exports = {}
  vm.runInNewContext(ts.transpile(read('features/license/taskAccess.ts'), {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  }), { exports, Request, URL, Event, window: {
    location: new URL('http://localhost:5173/tts'), dispatchEvent: event => events.push(event.type),
  } })
  return { ...exports, events }
}

test('only the server ACCESS_REQUIRED response opens activation; its body stays readable', async () => {
  const { watchTaskAccess, events } = harness()
  const watched = watchTaskAccess(async () => Response.json({ detail: 'ACCESS_REQUIRED' }, { status: 403 }))
  const requests = [
    ['/api/tts/studio/synthesize', { method: 'POST', body: '{"publishOutput":true}' }],
    ['/api/media-compose/jobs', { method: 'POST', body: new FormData() }],
    ['/api/cleaner/jobs', { method: 'POST', body: new FormData() }],
    ['/api/drawing/jobs/start', { method: 'POST' }],
    ['/api/queue/start', { method: 'POST' }],
    ['/api/automation/jobs', { method: 'POST', body: new FormData() }],
    ['/api/projects/p/run', { method: 'POST' }],
    ['/api/review/generate', { method: 'POST' }],
    ['/api/projects/p/export', { method: 'POST' }],
    [new Request('http://localhost:5173/api/flow/jobs', { method: 'POST', body: '{}' })],
  ]
  for (const args of requests) {
    const response = await watched(...args)
    assert.deepEqual(await response.json(), { detail: 'ACCESS_REQUIRED' })
  }
  assert.equal(events.length, requests.length)
  assert.ok(events.every(value => value === 'zm-task-access-required'))
  await watched('https://example.com/api/flow/jobs', { method: 'POST' })
  await watched('/not-an-api', { method: 'POST' })
  for (const [status, detail] of [[200, 'ACCESS_REQUIRED'], [403, 'OTHER_ERROR'], [401, 'ACCESS_REQUIRED']]) {
    await watchTaskAccess(async () => Response.json({ detail }, { status }))('/api/flow/jobs', { method: 'POST' })
  }
  await watchTaskAccess(async () => new Response('not json', { status: 403 }))('/api/flow/jobs', { method: 'POST' })
  assert.equal(events.length, requests.length)
})

test('allowed actions never open activation or depend on a duplicated frontend route policy', async () => {
  const { watchTaskAccess, events } = harness()
  const watched = watchTaskAccess(async () => Response.json({ ok: true }))
  for (const path of ['/api/download/jobs', '/api/srt-export/jobs', '/api/config', '/api/tts/studio/transcribe',
    '/api/tts/studio/clone', '/api/projects/p/editor-run', '/api/queue/j/action', '/api/automation/jobs/j/resume']) {
    await watched(path, { method: 'POST' })
  }
  assert.deepEqual(events, [])
  assert.doesNotMatch(read('features/license/taskAccess.ts'), /isTaskRequest/)
})

test('blocked Clone and Publish preserve existing results and timeline', async () => {
  const mutations = []
  const state = new Proxy({}, { get: (_, name) => (...args) => mutations.push([name, args]) })
  const app = read('app/App.tsx')
  const hook = read('features/project/useExportFlow.ts')
  function getFunction(source, name) {
    const tree = ts.createSourceFile('input.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    let result
    const visit = node => {
      if (ts.isFunctionDeclaration(node) && node.name?.text === name) result = node.getText(tree)
      ts.forEachChild(node, visit)
    }
    visit(tree)
    return ts.transpile(result)
  }
  const reject = async () => { throw new Error('ACCESS_REQUIRED') }
  const context = vm.createContext({
    projectId: 'p', status: { running: false }, settings: {}, segments: [{ id: 'keep' }],
    api: { run: reject, export: reject }, appMode: 'clone',
    resourceInstallRef: { current: null }, toast: { error: state.toast },
    setExportUrl: state.setExportUrl, setSegments: state.setSegments, setStatus: state.setStatus,
  })
  vm.runInContext(getFunction(app, 'onTranslateAll') + getFunction(hook, 'onExport'), context)
  await context.onTranslateAll(0)
  await context.onExport()
  assert.deepEqual(mutations, [])
})

test('Batch Run uses one gated start request, while staging and existing-job actions stay distinct', () => {
  assert.match(read('pages/BatchPage.tsx'), /await studioApi\.startJobs\(readyQueueJobs\)/)
  assert.match(read('features/studio/studio.api.ts'), /startJobs:[^]*fetchJson[^]*'\/api\/queue\/start'/)
  assert.match(read('pages/DrawingPage.tsx'), /append\('start_now', 'false'\)/)
  assert.match(read('features/project/project.api.ts'), /editor \? 'editor-run' : 'run'/)
  assert.equal((read('features/tts/TtsStudio.tsx').match(/publishOutput: false/g) || []).length, 2)
  assert.match(read('features/tts/TtsStudio.tsx'), /publishOutput: true/)
})

test('unactivated configuration, voice catalog and background status remain available', () => {
  assert.doesNotMatch(read('features/configuration/ConfigModal.tsx'), /!licenseStatus\?\.valid/)
  const app = read('app/App.tsx')
  assert.doesNotMatch(app, /if \(!licenseStatus\?\.valid\) return|!licenseBlocked/)
  assert.match(app, /<RealtimeProvider enabled=\{appUsable\}/)
  assert.match(app, /setTaskAccessRequired\(true\)/)
  assert.match(app, /taskAccessRequired=\{taskAccessRequired\}/)
  assert.match(read('features/configuration/ConfigModal.tsx'), /accessRequired=\{taskAccessRequired\}/)
})
