import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const source = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')
const compile = code => ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

test('compute mode publishes returned hardware only after a successful switch', async () => {
  const body = source('features/project/project.api.ts').match(/hardwareMode: async ([\s\S]*?)\n  },/)[1]
  const events = []
  const hw = { accel: 'cpu', gpuName: 'Apple GPU', hasGpu: true }
  let fail = false
  const switchMode = new Function('fetchJson', 'window', 'CustomEvent', 'base', `return async ${compile(body + '\n}').trim()}`)(
    async () => { if (fail) throw new Error('busy'); return hw },
    { dispatchEvent: event => events.push(event) },
    class { constructor(type, options) { this.type = type; this.detail = options.detail } }, '/api')
  assert.equal(await switchMode('cpu'), hw)
  assert.equal(events[0].type, 'zm-hardware-mode-changed')
  assert.equal(events[0].detail, hw)
  fail = true
  await assert.rejects(switchMode('gpu'), /busy/)
  assert.equal(events.length, 1)
})

test('Film CPU badge does not display physically present GPU as active', () => {
  const fn = source('pages/FilmPage.tsx').match(/function deviceBadge\(hw: HardwareInfo\) \{[\s\S]*?\n\}/)[0]
  const badge = new Function(`${compile(fn)}; return deviceBadge`)()
  assert.deepEqual(badge({ accel: 'cpu', gpuName: 'Apple GPU', label: 'CPU' }), { kind: 'CPU', name: '', gpu: false })
  assert.equal(badge({ accel: 'mps', gpuName: 'Apple GPU' }).name, 'Apple GPU')
  assert.equal(badge({ accel: 'cuda', gpuName: 'NVIDIA' }).kind, 'GPU')
})

test('download completion automatically applies update once without a second button', () => {
  const source = readFileSync(new URL('../frontend/src/features/configuration/ConfigModal.tsx', import.meta.url), 'utf8')
  assert.match(source, /state.phase === 'ready'\) \{\s*await applyUpdate\(\)/)
  assert.match(source, /if \(updateApplyStarted.current \|\| updateCancelRequested.current\) return/)
  assert.doesNotMatch(source, /updateDialog.kind === 'ready'.*<button/)
  assert.match(source, /if \(updateDialog\?\.kind !== 'downloading' && updateDialog\?\.kind !== 'cancelling'\) return/)
})

test('selecting the current compute mode does not reset the runtime', async () => {
  const header = source('shared/components/Header.tsx')
  const body = header.match(/const chooseHardware = async \(value: 'cpu' \| 'gpu'\) => \{([\s\S]*?)\n  \}/)[1]
  const choose = new Function('hardware', 'hardwareBusy', 'hardwareRef', 'setHardwareBusy', 'onHardwareChange', `return async value => {${body}}`)
  for (const [accel, current, other] of [['cpu', 'cpu', 'gpu'], ['metal', 'gpu', 'cpu']]) {
    const ref = { current: { open: true } }
    const calls = []
    const run = choose({ accel }, false, ref, () => {}, async mode => calls.push(mode))
    await run(current)
    assert.deepEqual(calls, [])
    assert.equal(ref.current.open, false)
    await run(other)
    assert.deepEqual(calls, [other])
  }
})
