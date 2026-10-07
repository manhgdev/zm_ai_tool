import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const root = fileURLToPath(new URL('../frontend/src/', import.meta.url))
const nativeRequire = createRequire(import.meta.url)
const cache = new Map()
// Run the actual layout/drag code with the repo's existing TypeScript runtime.
function load(path) {
  const file = [path, `${path}.ts`, `${path}.tsx`].find(existsSync)
  if (cache.has(file)) return cache.get(file)
  const exports = {}
  cache.set(file, exports)
  const require = name => name.startsWith('@/') ? load(resolve(root, name.slice(2)))
    : name.startsWith('.') ? load(resolve(dirname(file), name)) : nativeRequire(name)
  new Function('require', 'exports', compile(readFileSync(file, 'utf8')))(require, exports)
  return exports
}
function compile(source) {
  return ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
}
const layout = load(resolve(root, 'features/editor/lib/coverLayout'))
const boxMath = load(resolve(root, 'features/editor/lib/coverBox'))
const query = load(resolve(root, 'features/editor/lib/segmentQuery'))
const ocr = load(resolve(root, 'features/editor/ocrOverlayLayout'))
const previewStyles = load(resolve(root, 'features/editor/lib/previewStyles'))
const frame = { x: 0, y: 0, w: 1920, h: 1080 }
const sourceBox = { x: 500, y: 750, w: 900, h: 80 }
const movedBox = { x: 400, y: 200, w: 900, h: 80 }
const settings = { burnSubs: true, coverHardsubs: true, subtitleFontSize: 32, captionPlacement: 'below' }
const segment = { id: 'caption', index: 1, start: 0, end: 3, source: 'source', translation: 'Caption text', layout: 'horizontal', bbox: sourceBox }

test('moving/resizing text preserves source mask before, during and after save', () => {
  for (const mode of ['horizontal', 'mid', 'label', 'vertical']) {
    const seg = { ...segment, layout: mode }
    const before = layout.resolvePreviewOverLayout(seg, settings, 1920, 1080, frame)
    const during = layout.resolvePreviewOverLayout(seg, settings, 1920, 1080, frame, movedBox)
    const saved = layout.resolvePreviewOverLayout({ ...seg, captionBox: movedBox }, settings, 1920, 1080, frame)
    assert.deepEqual(before.mask, sourceBox)
    assert.deepEqual(during.mask, before.mask)
    assert.deepEqual(saved.mask, before.mask)
    assert.deepEqual(during.cover, movedBox)
    assert.deepEqual(saved.cover, movedBox)
  }
})

test('explicit coverBox wins and mask is clipped to crop, not moved with text', () => {
  const coverBox = { x: 200, y: 600, w: 800, h: 90 }
  const crop = { x: 300, y: 0, w: 1200, h: 1080 }
  const result = layout.resolvePreviewOverLayout({ ...segment, coverBox, captionBox: movedBox }, settings, 1920, 1080, crop)
  assert.deepEqual(result.mask, { x: 300, y: 600, w: 700, h: 90 })
})

test('above/below manual captions stay draggable without creating a blur mask', () => {
  for (const captionPlacement of ['above', 'below']) {
    const config = { ...settings, coverHardsubs: false, captionPlacement }
    const seg = { ...segment, bbox: null }
    const during = layout.resolvePreviewOverLayout(seg, config, 1920, 1080, frame, movedBox)
    const saved = layout.resolvePreviewOverLayout({ ...seg, captionBox: movedBox }, config, 1920, 1080, frame)
    assert.deepEqual(during.cover, movedBox)
    assert.deepEqual(saved.cover, movedBox)
    assert.equal(during.mask, null)
    assert.equal(saved.mask, null)
    const [exported] = layout.buildExportSegments([{ ...seg, captionBox: movedBox }], config, 1920, 1080)
    assert.deepEqual(exported.captionBox, movedBox)
  }
})

function pointerWindow() {
  const listeners = new Map()
  return {
    addEventListener(name, fn) {
      if (!listeners.has(name)) listeners.set(name, new Set())
      listeners.get(name).add(fn)
    },
    removeEventListener: (name, fn) => listeners.get(name)?.delete(fn),
    emit(name, event) { for (const fn of [...(listeners.get(name) || [])]) fn({ type: name, ...event }) },
    count: () => [...listeners.values()].reduce((n, set) => n + set.size, 0),
  }
}
function pointer(overrides = {}) {
  return { button: 0, pointerId: 7, clientX: 0, clientY: 0, altKey: true,
    currentTarget: {}, preventDefault() {}, stopPropagation() { this.stopped = true }, ...overrides }
}
const dragSource = readFileSync(resolve(root, 'features/editor/useTimelineDrag.ts'), 'utf8')
function mountDrag(overrides = {}) {
  const win = pointerWindow()
  const writes = []
  const state = {}
  const deps = {
    segments: [segment], settings, sourceWidth: 1920, sourceHeight: 1080, crop: frame,
    tool: 'select', trackLocked: {}, selected: segment, selectedBox: sourceBox,
    canvasRef: { current: { getBoundingClientRect: () => ({ width: 960, height: 540 }) } },
    bboxDraftRef: { current: null },
    pushHistoryOnce: gate => { gate.current = true },
    editSegment: next => writes.push(next), onSegmentsReplace: next => writes.push(...next),
    getCachedPreviewLayout: (seg, box) => layout.resolvePreviewOverLayout(seg, settings, 1920, 1080, frame, box),
    ...overrides,
  }
  for (const key of ['SelectedOverlayId', 'SelectedOverlayIds', 'SelectedId', 'SelectedIds', 'TrackFocus', 'PropTab', 'Tool', 'DraggingBox', 'SnapGuides', 'BboxDraft']) {
    deps[`set${key}`] = value => { state[key] = value }
  }
  const exports = {}
  new Function('require', 'exports', 'window', compile(dragSource))(name => name.endsWith('/lib')
    ? { ...layout, ...boxMath, ...query } : ocr, exports, win)
  return { deps, win, writes, state, begin: exports.useTimelineDrag(deps).beginBboxDrag }
}

test('caption drag does not switch to mask tool; cancel cleans up and ignores other pointers', () => {
  const ui = mountDrag()
  ui.begin(pointer(), 'move')
  assert.equal(ui.state.PropTab, 'caption')
  assert.equal(ui.state.Tool, undefined)
  ui.win.emit('pointermove', pointer({ pointerId: 99, clientY: 100 }))
  ui.win.emit('pointerup', pointer({ pointerId: 99 }))
  assert.equal(ui.deps.bboxDraftRef.current, null)
  ui.win.emit('pointermove', pointer({ clientY: -100 }))
  ui.win.emit('pointercancel', pointer({ clientY: 0 }))
  assert.equal(ui.win.count(), 0)
  assert.equal(ui.state.DraggingBox, false)
  assert.equal(ui.writes.length, 1)
  assert.deepEqual(ui.writes[0].bbox, sourceBox)
  assert.equal(ui.writes[0].captionBox.y, 550)
  ui.win.emit('pointermove', pointer({ clientY: 300 }))
  assert.equal(ui.writes.length, 1)
})

test('caption drag/resize without OCR never persists a fabricated bbox, including apply all', () => {
  const seg = { ...segment, bbox: null }
  for (const applyCaptionToAll of [false, true]) {
    const ui = mountDrag({ selected: seg, segments: [seg], applyCaptionToAll })
    ui.begin(pointer(), 's')
    ui.win.emit('pointermove', pointer({ clientY: 20 }))
    ui.win.emit('pointerup', pointer({ clientY: 20 }))
    assert.equal(ui.writes.length, 1)
    assert.equal(ui.writes[0].bbox, null)
    assert.equal(ui.writes[0].coverBox, null)
    assert.equal(ui.writes[0].captionBox.h, 120)
  }
})

test('locked caption and secondary button cannot start a drag', () => {
  for (const overrides of [{ timelineEditLocked: true }, { trackLocked: { caption: true } }]) {
    const ui = mountDrag(overrides)
    ui.begin(pointer(), 'move')
    assert.equal(ui.win.count(), 0)
  }
  const ui = mountDrag()
  ui.begin(pointer({ button: 2 }), 'move')
  assert.equal(ui.win.count(), 0)
})

const editorSource = readFileSync(resolve(root, 'features/editor/LivePreviewEditor.tsx'), 'utf8')
function editorFunction(name, scope) {
  const tree = ts.createSourceFile('editor.tsx', editorSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  let found
  const visit = node => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node
    ts.forEachChild(node, visit)
  }
  visit(tree)
  assert.ok(found, name)
  return new Function(...Object.keys(scope), `${compile(found.getText(tree))}; return ${name}`)(...Object.values(scope))
}

test('text/effect move and resize paint live once, stop bubbling, and save once on release', () => {
  for (const name of ['beginOverlayDrag', 'beginOverlayResize']) {
    const win = pointerWindow()
    const overlay = { id: 'text', x: 100, y: 100, w: 300, h: 100, kind: 'text' }
    const element = { style: {} }
    const writes = []
    const scope = {
      window: win, requestAnimationFrame: fn => { fn(); return 1 }, cancelAnimationFrame() {},
      isWatermarkOverlay: () => false, trackLocked: {}, timelineEditLocked: false, tool: 'select',
      canvasRef: { current: { getBoundingClientRect: () => ({ width: 960, height: 540 }) } },
      crop: frame, sourceWidth: 1920, sourceHeight: 1080,
      overlayElementRefs: { current: new Map([[overlay.id, element]]) }, overlayDragDraftRef: { current: null },
      sourceToDisplayStyle: previewStyles.sourceToDisplayStyle, pushHistory() {}, logoDraft: null,
      onOverlayChange: next => writes.push(next),
    }
    for (const key of ['ActiveBboxId', 'SelectedId', 'SelectedIds', 'SelectedOverlayId', 'TrackFocus', 'PropTab', 'Tool', 'LogoDraft']) scope[`set${key}`] = () => {}
    const event = pointer()
    editorFunction(name, scope)(event, overlay, 'se')
    assert.equal(event.stopped, true)
    // React clears currentTarget outside the handler; cleanup must keep its own reference.
    event.currentTarget = null
    win.emit('pointermove', pointer({ clientX: 50, clientY: 50 }))
    assert.ok(element.style.left && element.style.top)
    assert.equal(writes.length, 0)
    win.emit('pointerup', pointer({ clientX: 50, clientY: 50 }))
    assert.equal(writes.length, 1)
    assert.equal(win.count(), 0)
    assert.equal(name === 'beginOverlayDrag' ? writes[0].y : writes[0].h, 200)
  }
})

test('selection chrome never paints a mask; text/logo have imperative drag refs', () => {
  const chrome = editorSource.slice(editorSource.indexOf('{/* Bbox caption:'), editorSource.indexOf('{/* Phụ đề dịch'))
  assert.doesNotMatch(chrome, /coverMaskPreviewStyle|data-cover-mask-preview/)
  assert.match(editorSource, /activeAutoBlurBand && trackFocus !== 'fx'/)
  assert.equal((editorSource.match(/overlayElementRefs.current.set\(overlay.id, element\)/g) || []).length, 3)
})
