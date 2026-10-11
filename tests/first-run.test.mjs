import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')

test('the installer locale loads before activation and an explicit UI choice wins', async () => {
  const source = read('app/App.tsx')
  const effect = source.match(/useEffect\(\(\) => \{(\s*api\.getInitialLocale\(\)[\s\S]*?)\n  \}, \[\]\)/)[1]
  const changed = { current: false }, locales = []
  let ready = false, reads = 0
  const run = new Function('api', 'localeChangedRef', 'setLocale', 'setLocaleReady', effect)
  const api = { getInitialLocale: async () => { reads++; return { locale: 'vi' } } }
  run(api, changed, value => locales.push(value), value => { ready = value })
  await new Promise(setImmediate)
  assert.equal(reads, 1)
  assert.deepEqual(locales, ['vi'])
  assert.equal(ready, true)
  changed.current = true
  run(api, changed, value => locales.push(value), () => {})
  await new Promise(setImmediate)
  assert.deepEqual(locales, ['vi'])
})

test('missing optional AI packages never enter automatic first setup', () => {
  const source = read('features/configuration/configModal.helpers.ts')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const exports = {}
  new Function('exports', compiled)(exports)
  const checks = { items: ['ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu', 'ocr_cuda', 'demucs_cuda']
    .map(install => ({ install, ok: false, required: false })) }
  assert.equal(exports.nextAutoInstall(checks), null)
})
