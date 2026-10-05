import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import React from 'react'

test('tool navigation retains stable keyed trees without eagerly opening other tools', () => {
  const source = readFileSync(new URL('../frontend/src/shared/components/RetainedPages.tsx', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
  let opened = []
  const exports = {}
  const jsx = (type, props, key) => ({ type, props, key })
  new Function('require', 'exports', compiled)(name => name === 'react'
    ? { useState: () => [opened, next => { opened = next }] }
    : { jsx, jsxs: jsx, Fragment: React.Fragment }, exports)
  const render = active => exports.default({ active, transient: ['clone'], children: (page, visible) => ({ page, visible }) }).props.children
  const first = render('tts')[0]
  assert.deepEqual(first.map(node => node.key), ['tts'])
  const switched = render('flow')[0]
  assert.deepEqual(switched.map(node => node.key), ['tts', 'flow'])
  assert.equal(switched[0].props.hidden, true)
  assert.equal(switched[0].props.style.display, 'none')
  assert.equal(switched[0].props.children.page, 'tts')
  const back = render('tts')[0]
  assert.equal(back[0].key, first[0].key)
  assert.equal(back[0].props.hidden, false)
  assert.equal(back[1].props.hidden, true)
  const editor = render('clone')
  assert.equal(editor[0].length, 2)
  assert.ok(editor[0].every(node => node.props.hidden))
  assert.equal(editor[1].props.children.page, 'clone')
})

test('background Flow completion cannot change another tool URL', () => {
  const source = readFileSync(new URL('../frontend/src/features/flow/flow.helpers.ts', import.meta.url), 'utf8')
  const body = source.match(/export function writeFlowRoutePanel\(panel: FlowRoutePanel\) \{([\s\S]*?)\n\}/)[1]
  let writes = 0
  const win = { location: { pathname: '/text-to-speech' }, history: { pushState: () => { writes++ } } }
  new Function('window', 'panel', body)(win, 'queue')
  assert.equal(writes, 0)
})
