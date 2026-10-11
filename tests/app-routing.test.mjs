import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')
const compile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText

test('Clone has its own URL and explicit routes override the saved workspace', () => {
  const exports = {}
  const window = { location: { pathname: '/video-clone' } }
  new Function('exports', 'window', 'localStorage', compile(read('app/appMode.ts')))(
    exports, window, { getItem: () => 'tts' })
  assert.equal(exports.appModePath('clone'), '/video-clone')
  assert.equal(exports.appModeFromPath('/'), null)
  assert.equal(exports.appModeFromPath('/landing'), null)
  assert.equal(exports.loadAppMode(), 'clone')
  for (const [mode, path] of Object.entries(exports.APP_MODE_PATHS)) {
    assert.equal(exports.appModeFromPath(path + '/'), mode)
    window.location.pathname = path
    assert.equal(exports.loadAppMode(), mode)
  }
})

function renderEntry(path) {
  const window = { location: new URL(path, 'http://localhost'), history: {
    replaceState: (_state, _unused, next) => { window.location = new URL(next, window.location) },
  } }
  let rendered
  const modules = {
    react: { Component: class {}, StrictMode: 'StrictMode' },
    'react/jsx-runtime': { jsx: (type, props) => ({ type, props }) },
    'react-dom/client': { createRoot: () => ({ render: tree => { rendered = tree } }) },
    './app/App': { __esModule: true, default: 'App' },
    './pages/LandingPage': { __esModule: true, default: 'LandingPage' },
    './index.css': {},
  }
  new Function('require', 'exports', 'window', 'navigator', 'document', compile(read('main.tsx')))(
    id => modules[id], {}, window, { userAgent: '', platform: '' },
    { getElementById: () => ({}), documentElement: { classList: { add() {} } } })
  return { path: window.location.pathname + window.location.search + window.location.hash,
    page: rendered.props.children.props.children.type }
}

test('Root renders landing regardless of storage; old landing links keep query and anchor', () => {
  assert.deepEqual(renderEntry('/'), { path: '/', page: 'LandingPage' })
  assert.deepEqual(renderEntry('/video-clone'), { path: '/video-clone', page: 'App' })
  assert.deepEqual(renderEntry('/text-to-speech'), { path: '/text-to-speech', page: 'App' })
  for (const path of ['/landing', '/landing/']) {
    assert.deepEqual(renderEntry(path + '?v=8.6.24#download'), {
      path: '/?v=8.6.24#download', page: 'LandingPage',
    })
  }
})

test('Landing workspace links and Clone card target the explicit Clone URL', () => {
  const source = read('pages/LandingPage.tsx')
  assert.doesNotMatch(source, /\/landing|path: '\/'/)
  assert.match(source, /path: '\/video-clone'/)
  assert.equal((source.match(/href="\/video-clone"/g) || []).length, 3)
  assert.equal((source.match(/href="\/"/g) || []).length, 2)
  assert.match(source, /path: '\/text-to-speech'/)
})
