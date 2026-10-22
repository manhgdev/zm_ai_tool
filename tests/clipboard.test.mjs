import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/shared/lib/clipboard.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source.replace(/^import .*$/gm, '').replace(/^export /gm, ''), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

test('clipboard handles native API, modal fallback, failure cleanup and focus restoration', async () => {
  for (const mode of ['native', 'unavailable', 'denied', 'failed']) {
    for (const modal of [false, true]) {
      let focused, copied = '', removed = false, parent
      const previous = { closest: () => modal ? dialog : null, focus() { focused = this } }
      const textarea = {
        style: {}, focus() { focused = this }, select() {},
        setSelectionRange(start, end) { assert.equal(start, 0); assert.equal(end, this.value.length) },
        remove() { removed = true },
      }
      const body = { appendChild(node) { parent = this; assert.equal(node, textarea) } }
      const dialog = { appendChild: body.appendChild }
      const navigator = mode === 'unavailable' ? {} : { clipboard: { async writeText(value) {
        if (mode !== 'native') throw new Error('Denied')
        copied = value
      } } }
      const document = {
        activeElement: previous, body, querySelector: () => modal ? dialog : null,
        createElement: () => textarea,
        execCommand(command) {
          assert.equal(command, 'copy')
          assert.equal(parent, modal ? dialog : body)
          assert.equal(focused, textarea)
          copied = textarea.value
          return mode !== 'failed'
        },
      }
      const copy = vm.runInNewContext(`${code}; copyText`, {
        document, navigator, loadLocale: () => 'en', localize: (_locale, _vi, en) => en,
      })
      if (mode === 'failed') await assert.rejects(() => copy('ZMAI-TEST-ONLY', { notify: false }))
      else { await copy('ZMAI-TEST-ONLY', { notify: false }); assert.equal(copied, 'ZMAI-TEST-ONLY') }
      if (mode !== 'native') { assert.ok(removed); assert.equal(focused, previous) }
    }
  }
})
