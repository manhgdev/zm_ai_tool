import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import ts from 'typescript'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

test('initial transient and retained pages have real, bounded grid boxes', () => {
  const source = readFileSync(new URL('../frontend/src/shared/components/RetainedPages.tsx', import.meta.url), 'utf8')
  const exports = {}
  vm.runInNewContext(ts.transpile(source, {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }), { exports, require: createRequire(import.meta.url) })
  for (const active of ['clone', 'tts']) {
    const html = renderToStaticMarkup(React.createElement(exports.default, {
      active, transient: ['clone'], children: page => React.createElement('main', null, page),
    }))
    assert.match(html, /grid-area:2 \/ 1/)
    assert.match(html, /grid-template-rows:minmax\(0, 1fr\)/)
    assert.match(html, new RegExp(`<main>${active}</main>`))
    assert.doesNotMatch(html, /display:contents/)
  }
})
