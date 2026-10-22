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
const load = localStorage => vm.runInNewContext(`${code}; ({ loadConfigSection, persistConfigSection, PROVIDERS, emptyCloud, PROVIDER_PRESET_MODELS })`, { localStorage })

test('OpenCode is configured next to OpenRouter with a valid default model', () => {
  const { PROVIDERS, emptyCloud, PROVIDER_PRESET_MODELS } = load({})
  assert.equal(PROVIDERS[PROVIDERS.indexOf('openrouter') + 1], 'opencode')
  const config = emptyCloud().opencode
  assert.equal(config.label, 'OpenCode')
  assert.equal(config.baseUrl, 'https://opencode.ai/zen/v1')
  assert.equal(config.apiKeySet, false)
  assert.ok(PROVIDER_PRESET_MODELS.opencode.some(model => model.id === config.model))
})

test('shared translation and AI review pickers retain OpenCode after saving settings', () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
  const settingsCode = ts.transpileModule(read('app/appSettings.ts').replace(/^import .*$/gm, '').replace(/^export /gm, ''), {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText
  const settings = vm.runInNewContext(`${settingsCode}; ({ translatorOptions, translationReviewOptions, defaultSettings, persistSettings, loadSettings })`, { localStorage: storage })
  for (const engine of ['whisper', 'paddleocr', 'subtitle']) {
    const options = settings.translatorOptions(engine)
    assert.equal(options[options.findIndex(item => item.id === 'openrouter') + 1].id, 'opencode')
  }
  assert.ok(settings.translationReviewOptions().some(item => item.id === 'opencode' && item.label === 'OpenCode'))
  settings.persistSettings({ ...settings.defaultSettings, engine: 'whisper', translator: 'opencode', translationReviewTranslator: 'opencode' })
  assert.equal(settings.loadSettings().translator, 'opencode')
  assert.equal(settings.loadSettings().translationReviewTranslator, 'opencode')
})

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
