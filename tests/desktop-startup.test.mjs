import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const read = file => readFileSync(new URL(`../${file}`, import.meta.url), 'utf8')

test('toast host cannot consume a workspace grid row when the setup gate is restored', () => {
  const tree = ts.createSourceFile('App.tsx', read('frontend/src/app/App.tsx'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const toasters = []
  const visit = node => {
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(tree) === 'Toaster') toasters.push(node)
    ts.forEachChild(node, visit)
  }
  visit(tree)
  assert.equal(toasters.length, 1)
  for (let node = toasters[0].parent; node; node = node.parent) {
    if (!ts.isJsxElement(node)) continue
    const className = node.openingElement.attributes.properties.find(attr =>
      ts.isJsxAttribute(attr) && attr.name.getText(tree) === 'className')
    assert.notEqual(className?.initializer?.text, 'app', 'Sonner renders an in-flow section; keep it outside the app grid')
  }
})

test('packaged entry restores the last tab before the desktop JS bridge is available', () => {
  const main = ts.createSourceFile('main.tsx', read('frontend/src/main.tsx'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const functions = main.statements.filter(node => ts.isFunctionDeclaration(node)
    && ['isDesktopShell', 'resolveRootEntry'].includes(node.name?.text)).map(node => node.getText(main)).join('\n')
  const code = ts.transpile(`${read('frontend/src/app/appMode.ts')}\n${functions}`, { module: ts.ModuleKind.CommonJS })
  const launcher = read('build_app/launcher.py')
  const entry = launcher.match(/page_url = f"\{base\}([^"?]+)\?v=/)?.[1]
  assert.ok(entry && entry !== '/', 'Desktop needs an explicit app entry, independent of bridge timing')
  assert.ok(launcher.match(/for _spa_path in \(([\s\S]*?)\):/)?.[1].includes(`"${entry}"`), 'Launcher must serve its entry route')
  for (const [saved, expected] of [[null, 'clone'], ['tts', 'tts'], ['flow', 'flow'], ['live-preview', 'clone']]) {
    const exports = {}
    const window = { location: { pathname: entry, search: '?v=test', hash: '' }, history: { replaceState() {} } }
    vm.runInNewContext(code, { exports, window, URLSearchParams, localStorage: { getItem: () => saved } })
    assert.equal(exports.resolveRootEntry(), 'app')
    assert.equal(exports.loadAppMode(), expected)
    window.location.pathname = '/'
    assert.equal(exports.resolveRootEntry(), 'landing', 'Ordinary browser root remains the landing page')
  }
})
