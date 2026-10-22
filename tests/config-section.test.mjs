import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')
const source = read('features/configuration/configModal.helpers.ts')
const code = ts.transpileModule(source.replace(/^import .*$/gm, '').replace(/^export /gm, ''), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText
const load = localStorage => vm.runInNewContext(`${code}; ({ loadConfigSection, persistConfigSection })`, { localStorage })

test('all configuration sections survive a fresh instance through localStorage', () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
  for (const section of ['setup', 'cloud', 'tts', 'output', 'cleanup', 'license', 'logs', 'computer']) {
    load(storage).persistConfigSection(section)
    assert.equal(values.get('zm_ai_tool.configSection'), section)
    assert.equal(load(storage).loadConfigSection(), section)
  }
})

test('missing, corrupt or unavailable storage safely defaults to Cloud AI', () => {
  for (const saved of [null, '', 'unknown', 'Computer', '{"section":"logs"}']) {
    assert.equal(load({ getItem: () => saved }).loadConfigSection(), 'cloud')
  }
  const blocked = load({ getItem() { throw new Error('blocked') }, setItem() { throw new Error('quota') } })
  assert.equal(blocked.loadConfigSection(), 'cloud')
  assert.doesNotThrow(() => blocked.persistConfigSection('computer'))
})

test('configuration navigation shares the persisted state without resetting on reopen or access refresh', () => {
  const app = read('app/App.tsx')
  const modal = read('features/configuration/ConfigModal.tsx')
  assert.ok(app.includes("loadSetupGate() ? loadConfigSection() : 'setup'"))
  assert.ok(app.includes('persistConfigSection(configSection)'))
  assert.ok(app.includes('section={configSection}'))
  assert.ok(app.includes('onSectionChange={setConfigSection}'))
  assert.match(app, /onOpenConfig=\{\(\) => \{\s*setConfigOpen\(true\)/)
  assert.doesNotMatch(modal, /initialSection|setSection\(initialSection\)/)
  assert.ok(modal.includes('onSectionChange: setSection'))
  for (const section of ['setup', 'cloud', 'tts', 'output', 'cleanup', 'license', 'logs', 'computer']) {
    assert.ok(modal.includes(`onClick={() => setSection('${section}')}`))
  }
})
