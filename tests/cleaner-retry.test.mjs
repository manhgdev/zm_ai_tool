import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

test('cleaner permits cached terminal files and blocks active jobs', () => {
  const source = readFileSync(new URL('../frontend/src/pages/VideoCleanerPage.tsx', import.meta.url), 'utf8')
  const body = source.slice(source.indexOf('  const runnableFiles ='), source.indexOf('\n\n', source.indexOf('  const runnableFiles =')))
  const code = ts.transpileModule(body + '\nreturn runnableFiles;', { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  class File {}
  const selected = [{ file: new File() }, ...['done', 'error', 'cancelled', 'queued', 'processing', 'missing'].map(jobId => ({ jobId }))]
  const jobs = selected.slice(1, -1).map(file => ({ id: file.jobId, status: file.jobId }))
  const result = new Function('selectedFiles', 'jobs', 'ACTIVE_STATES', 'File', code)(selected, jobs, new Set(['queued', 'processing']), File)
  assert.deepEqual(result, selected.slice(0, 4))
  assert.match(source, /disabled=\{submitting \|\| !runnableFiles.length\}/)
  assert.match(source, /cleanerApi.retry\(file.jobId, method, options, outputDir\)/)
})
