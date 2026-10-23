import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/features/studio/VideoSourcePicker.tsx', import.meta.url), 'utf8')
const ast = ts.createSourceFile('VideoSourcePicker.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const component = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'VideoSourcePicker')
const code = ts.transpile(component.getText(ast).replace('export ', ''), { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 })

function picker(props = {}, api = {}) {
  const selected = [], errors = [], clicks = [], busy = []
  let finish
  const done = new Promise(resolve => { finish = resolve })
  const context = vm.createContext({
    Error,
    React: { Fragment: 'fragment', createElement: (type, props, ...children) => ({ type, props: { ...props, children } }) },
    useLocale: () => ({ locale: 'en' }), localize: (_locale, _vi, en) => en,
    useState: value => [value, () => {}],
    useRef: () => { const index = clicks.length; clicks.push(0); return { current: { click() { clicks[index]++ } } } },
    videoAccept: '.mp4,.mov,.mkv,.webm,.avi,.m4v', studioApi: api,
  })
  vm.runInContext(code, context)
  const tree = context.VideoSourcePicker({ isDesktopApp: false, multiple: true,
    onSelect: paths => selected.push(...paths), onError: message => { if (message) errors.push(message) },
    onBusyChange: value => { busy.push(value); if (!value) finish() }, ...props })
  const elements = []
  function visit(node) {
    if (!node || typeof node !== 'object') return
    if (Array.isArray(node)) return node.forEach(visit)
    elements.push(node)
    visit(node.props.children)
  }
  visit(tree)
  return { inputs: elements.filter(node => node.type === 'input'), buttons: elements.filter(node => node.type === 'button'), selected, errors, clicks, busy, done }
}

test('Web buttons open browser pickers; folder upload filters formats and recursive selection', async () => {
  const uploaded = []
  const ui = picker({ recursive: false }, { uploadVideo: async file => { uploaded.push(file.name); return { path: '/private/' + file.name } } })
  ui.buttons[0].props.onClick()
  ui.buttons[1].props.onClick()
  assert.deepEqual(ui.clicks, [1, 1])
  assert.equal(ui.inputs[0].props.multiple, true)
  assert.equal(ui.inputs[1].props.webkitdirectory, '')
  const input = { value: 'selected', files: [
    { name: 'first.MP4', webkitRelativePath: 'movies/first.MP4' },
    { name: 'nested.mkv', webkitRelativePath: 'movies/season/nested.mkv' },
    { name: 'notes.txt', webkitRelativePath: 'movies/notes.txt' },
  ] }
  ui.inputs[1].props.onChange({ currentTarget: input })
  await ui.done
  assert.equal(input.value, '')
  assert.deepEqual(uploaded, ['first.MP4'])
  assert.deepEqual(ui.selected, ['/private/first.MP4'])
  assert.deepEqual(ui.errors, [])
  assert.deepEqual(ui.busy, [true, false])
})

test('Web uploads recurse and retain the completed selection when the next upload fails', async () => {
  const uploaded = []
  const ui = picker({}, { uploadVideo: async file => {
    uploaded.push(file.name)
    if (uploaded.length === 2) throw new Error('HTTP 413')
    return { path: '/private/' + file.name }
  } })
  const files = ['first.mp4', 'second.mkv', 'third.mov'].map(name => ({ name, webkitRelativePath: 'movies/season/' + name }))
  ui.inputs[1].props.onChange({ currentTarget: { files, value: 'selected' } })
  await ui.done
  assert.deepEqual(uploaded, ['first.mp4', 'second.mkv'])
  assert.deepEqual(ui.selected, ['/private/first.mp4'])
  assert.deepEqual(ui.errors, ['Could not add video: HTTP 413'])
  assert.deepEqual(ui.busy, [true, false])
})

test('Film selects one Web video and cancelling a picker performs no upload', async () => {
  const ui = picker({ multiple: false }, { uploadVideo: async file => ({ path: '/private/' + file.name }) })
  assert.equal(ui.inputs.length, 1)
  ui.inputs[0].props.onChange({ currentTarget: { files: [], value: '' } })
  assert.deepEqual(ui.busy, [])
  ui.inputs[0].props.onChange({ currentTarget: { files: [{ name: 'first.mp4' }, { name: 'second.mp4' }], value: '' } })
  await ui.done
  assert.deepEqual(ui.selected, ['/private/first.mp4'])
})

test('desktop reuses native pickers and displays native failures', async () => {
  const ui = picker({ isDesktopApp: true }, { pickVideos: async () => ({ paths: ['C:/video.mp4'] }) })
  ui.buttons[0].props.onClick()
  await ui.done
  assert.deepEqual(ui.selected, ['C:/video.mp4'])
  assert.deepEqual(ui.clicks, [0, 0])
  const folder = picker({ isDesktopApp: true }, { pickFolder: async () => ({ path: 'C:/movies' }) })
  folder.buttons[1].props.onClick()
  await folder.done
  assert.deepEqual(folder.selected, ['C:/movies'])
  const failed = picker({ isDesktopApp: true }, { pickVideos: async () => { throw new Error('dialog failed') } })
  failed.buttons[0].props.onClick()
  await failed.done
  assert.deepEqual(failed.errors, ['Could not add video: dialog failed'])
})
