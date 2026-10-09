import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(new URL('../frontend/src/app/App.css', import.meta.url), 'utf8')

test('shared toast content is bounded and scrollable without truncating diagnostics', () => {
  const rule = css.match(/\[data-sonner-toaster\] \[data-sonner-toast\]\[data-styled='true'\] \[data-content\]\s*\{([^}]+)\}/)?.[1]
  assert.ok(rule, 'Style the shared Sonner content, not individual Flow messages')
  assert.match(rule, /max-height:\s*min\(320px, calc\(100dvh - 112px\)\)/)
  assert.match(rule, /overflow-y:\s*auto/)
  assert.match(rule, /overflow-wrap:\s*anywhere/)
  assert.match(rule, /scrollbar-gutter:\s*stable/)
  assert.match(rule, /overscroll-behavior:\s*contain/)
  assert.doesNotMatch(rule, /line-clamp|text-overflow:\s*ellipsis/)
  assert.match(css, /\[data-content\] > \*\s*\{\s*flex-shrink:\s*0/)
})
