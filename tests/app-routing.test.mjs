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
    // Ephemeral modes (live-preview / license) never stick — cold load maps to clone.
    const expected = mode === 'live-preview' || mode === 'license' ? 'clone' : mode
    assert.equal(exports.loadAppMode(), expected)
  }
})

function renderEntry(path, { desktop = false, storedMode = 'clone' } = {}) {
  const window = {
    location: new URL(path, 'http://localhost'),
    history: {
      replaceState: (_state, _unused, next) => { window.location = new URL(next, window.location) },
    },
  }
  if (desktop) window.pywebview = {}
  let rendered
  const localStorage = {
    getItem: (key) => (key === 'zm_ai_tool.appMode' ? storedMode : null),
    setItem() {},
  }
  const appModeExports = {}
  new Function('exports', 'window', 'localStorage', compile(read('app/appMode.ts')))(
    appModeExports, window, localStorage)
  const modules = {
    react: { Component: class {}, StrictMode: 'StrictMode' },
    'react/jsx-runtime': { jsx: (type, props) => ({ type, props }) },
    'react-dom/client': { createRoot: () => ({ render: tree => { rendered = tree } }) },
    './app/App': { __esModule: true, default: 'App' },
    './app/appMode': appModeExports,
    './pages/LandingPage': { __esModule: true, default: 'LandingPage' },
    './index.css': {},
  }
  new Function('require', 'exports', 'window', 'navigator', 'document', 'localStorage', compile(read('main.tsx')))(
    id => modules[id], {}, window, { userAgent: '', platform: '' },
    { getElementById: () => ({}), documentElement: { classList: { add() {} } } },
    localStorage)
  return {
    path: window.location.pathname + window.location.search + window.location.hash,
    page: rendered.props.children.props.children.type,
  }
}

test('Browser root renders landing regardless of storage', () => {
  assert.deepEqual(renderEntry('/'), { path: '/', page: 'LandingPage' })
  assert.deepEqual(renderEntry('/video-clone'), { path: '/video-clone', page: 'App' })
  assert.deepEqual(renderEntry('/text-to-speech'), { path: '/text-to-speech', page: 'App' })
})

test('Desktop root restores the last sticky tab; ?landing=1 keeps Landing', () => {
  assert.deepEqual(
    renderEntry('/', { desktop: true, storedMode: 'tts' }),
    { path: '/text-to-speech', page: 'App' },
  )
  assert.deepEqual(
    renderEntry('/?landing=1', { desktop: true, storedMode: 'tts' }),
    { path: '/?landing=1', page: 'LandingPage' },
  )
  assert.deepEqual(
    renderEntry('/?v=8.6.68', { desktop: true, storedMode: 'flow' }),
    { path: '/flow-veo?v=8.6.68', page: 'App' },
  )
})

test('Legacy /landing links keep query and anchor and force landing=1', () => {
  for (const path of ['/landing', '/landing/']) {
    assert.deepEqual(renderEntry(path + '?v=8.6.24#download'), {
      path: '/?v=8.6.24&landing=1#download',
      page: 'LandingPage',
    })
  }
})

test('Landing workspace CTAs use last-tab helper and Clone product still targets /video-clone', () => {
  const source = read('pages/LandingPage.tsx')
  assert.doesNotMatch(source, /path: '\/'/)
  assert.match(source, /path: '\/video-clone'/)
  assert.match(source, /appModePath\(loadAppMode\(\)\)/)
  assert.match(source, /checkAppUpdate/)
  assert.match(source, /Cập nhật/)
  assert.match(source, /Mở workspace/)
  assert.match(source, /path: '\/text-to-speech'/)
  assert.match(source, /\?landing=1/)
})
