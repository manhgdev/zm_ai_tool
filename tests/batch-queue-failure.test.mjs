import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/pages/BatchPage.tsx', import.meta.url), 'utf8')
const ast = ts.createSourceFile('BatchPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
function method(name) {
  let found
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node.getText(ast)
    ts.forEachChild(node, visit)
  }
  visit(ast)
  return ts.transpile(found)
}
test('failed drawing refresh or deletion preserves the displayed jobs and preview', async () => {
  const jobs = [{ id: 'keep-me' }]
  let displayed = jobs
  let preview = jobs[0]
  let successes = 0
  const context = vm.createContext({
    studioApi: { queue: async () => ({ jobs: [] }) },
    fetchJson: async () => { throw new Error('ACCESS_REQUIRED') },
    fetch: async () => ({ ok: false, text: async () => 'ACCESS_REQUIRED' }),
    setJobs() {}, setPauseAll() {}, setError() {},
    setDrawingJobs: value => { displayed = value },
    setDrawingPreview: value => { preview = value },
    toast: { error() {}, success() { successes++ } }, t: value => value,
  })
  vm.runInContext(method('refresh') + method('deleteDrawingJob'), context)
  await context.refresh()
  assert.equal(displayed, jobs)
  assert.equal(await context.deleteDrawingJob('keep-me'), false)
  assert.equal(displayed, jobs)
  assert.equal(preview, jobs[0])
  assert.equal(successes, 0)
})
