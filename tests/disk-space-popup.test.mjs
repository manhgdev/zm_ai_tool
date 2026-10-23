import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import ts from 'typescript'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { diskSpaceError } from '../frontend/src/shared/lib/diskSpace.ts'

const trace = 'File "C:\\app\\transformers.py", line 116\n'
  + "OSError: [WinError 112] There is not enough space on the disk: 'D:\\ZM AI TOOL\\cache\\hf\\modules'"

test('disk failures identify the destination and keep unknown disks unknown', () => {
  assert.equal(diskSpaceError(trace).diskDrive, 'D:')
  assert.equal(diskSpaceError(trace).diskPath, 'D:\\ZM AI TOOL\\cache\\hf\\modules')
  assert.equal(diskSpaceError(`${trace}\nInstallation failed [DISK_FULL]`).diskDrive, 'D:')
  assert.equal(diskSpaceError('DISK_FULL\nFile "C:\\app\\code.py", line 10').diskDrive, '')
  assert.equal(diskSpaceError("[Errno 28] No space left on device: '/srv/models/cache'").diskPath, '/srv/models/cache')
  assert.equal(diskSpaceError("[WinError 112] full: 'E:\\\\models\\\\hf'").diskPath, 'E:\\models\\hf')
  assert.equal(diskSpaceError('DISK_FULL').diskDrive, '')
  assert.equal(diskSpaceError('Permission denied'), null)
  assert.equal(diskSpaceError('VieNeu worker timeout'), null)
  assert.equal(diskSpaceError('No space left', { diskPath: '/mnt/Models/cache', diskDrive: '/mnt/Models' }).diskDrive, '/mnt/Models')
})

function popup(locale, props) {
  const exports = {}
  const require = createRequire(import.meta.url)
  const source = readFileSync(new URL('../frontend/src/shared/components/ProgressPopup.tsx', import.meta.url), 'utf8')
  vm.runInNewContext(ts.transpile(source, { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }), {
    exports,
    require: id => id === '@/app/i18n' ? { useLocale: () => ({ locale }), localize: (locale, vi, en) => locale === 'en' ? en : vi }
      : id === '@/shared/lib/diskSpace' ? { diskSpaceError }
      : id === '@/shared/lib/cn' ? { cn: (...values) => values.filter(Boolean).join(' ') }
      : id === '@/shared/lib/clipboard' ? { copyText: async () => {} }
      : id === '@/shared/hooks/useStickToBottomScroll' ? { useStickToBottomScroll: () => ({}) }
      : id.endsWith('.css') ? {} : require(id),
  })
  return renderToStaticMarkup(React.createElement(exports.default, {
    active: true, minimized: false, progress: 0, error: trace,
    onMinimize() {}, onRestore() {}, ...props,
  }))
}

test('a background disk failure opens a readable bilingual dialog with recovery steps', () => {
  const vi = popup('vi', { minimized: true, hideMinimized: true })
  const en = popup('en', { minimized: true, hideMinimized: true })
  assert.match(vi, /role="dialog"/)
  assert.match(vi, /Ổ D: không đủ dung lượng/)
  assert.match(vi, /Đường dẫn đang ghi:/)
  assert.match(en, /Drive D: is out of space/)
  assert.match(en, /Write location:/)
  assert.match(vi, /Cài hoặc lưu sang ổ khác/)
  assert.match(en, /Install or save to another drive/)
  assert.match(en, /Setup does not move existing data/)
  assert.doesNotMatch(en, /Tiến độ|Đường dẫn|không đủ/)
  assert.match(en, /<details><summary[^>]*>Error details<\/summary>/)
  assert.match(en, /WinError 112/)
})

test('structured preflight errors show required and available space', () => {
  const html = popup('en', { error: 'DISK_FULL', log: 'Original installer diagnostics', actions: 'Retry on CPU', diskSpace: {
    errorCode: 'DISK_FULL', diskDrive: 'E:', diskPath: 'E:\\ZM AI TOOL\\user-data\\runtime',
    freeDiskBytes: 0, requiredDiskBytes: 10 * 1024 ** 3,
  } })
  assert.match(html, /Drive E: is out of space/)
  assert.match(html, /Available:.*0\.00.*GiB/)
  assert.match(html, /Required:.*10\.00.*GiB/)
  assert.doesNotMatch(html, /animate-pulse/)
  assert.doesNotMatch(html, /Retry on CPU/)
  assert.match(html, /Original installer diagnostics/)
  assert.match(html, /Copy error/)
  const unknown = popup('en', { error: 'DISK_FULL' })
  assert.match(unknown, /write location is unavailable/)
  assert.doesNotMatch(unknown, /Drive C:/)
})

test('non-disk errors retain their existing popup and background behavior', () => {
  const html = popup('en', { error: 'Worker timeout', title: 'TTS error' })
  assert.match(html, /TTS error/)
  assert.doesNotMatch(html, /Install or save to another drive/)
  assert.equal(popup('en', { minimized: true, hideMinimized: true, error: 'Worker timeout' }), '')
})
