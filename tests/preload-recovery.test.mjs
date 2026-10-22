import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

test('asset preload recovery reloads once and never loops when storage fails', () => {
  const exports = {}
  const saved = new Map()
  let reloads = 0
  const sessionStorage = { getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value) }
  vm.runInNewContext(ts.transpile(readFileSync(new URL('../frontend/src/app/preloadRecovery.ts', import.meta.url), 'utf8'), {
    module: ts.ModuleKind.CommonJS,
  }), { exports, sessionStorage, window: { location: { reload: () => reloads++ } } })
  const first = new Event('vite:preloadError', { cancelable: true })
  exports.recoverPreloadError(first)
  assert.equal(first.defaultPrevented, true)
  const second = new Event('vite:preloadError', { cancelable: true })
  exports.recoverPreloadError(second)
  assert.equal(second.defaultPrevented, false)
  assert.equal(reloads, 1)
  saved.clear()
  sessionStorage.setItem = () => { throw new Error('blocked') }
  exports.recoverPreloadError(second)
  assert.equal(reloads, 1)
})
