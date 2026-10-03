import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = path => readFileSync(new URL(`../frontend/src/${path}`, import.meta.url), 'utf8')

test('transcription rewrite uses app navigation and retains the prefill', () => {
  const studio = read('features/tts/TtsStudio.tsx')
  assert.match(studio, /onRewrite=\{[^\n]+chat-prefill:v1[^\n]+onOpenChat\?\.\(\)/)
  assert.doesNotMatch(studio, /window\.location\.href\s*=\s*['"]\/chat/)
  assert.match(read('pages/TtsPage.tsx'), /onOpenChat=\{onOpenChat\}/)
  assert.match(read('app/App.tsx'), /onOpenChat=\{\(\) => navigateToMode\('chat'\)\}/)
})

test('mode synchronization preserves the current query and Chat provides a visible return path', () => {
  assert.match(read('app/App.tsx'), /appModeFromPath\(window.location.pathname\) === appMode \? window.location.search/)
  const chat = read('pages/ChatPage.tsx')
  assert.match(chat, /className="chat-back" onClick=\{onBack\}/)
  assert.match(chat, /if \(!response.ok\)/)
  assert.match(chat, /catch\(reason => setError\(errorText/)
  assert.match(chat, /Chat service was not found \(404\)/)
  assert.doesNotMatch(read('pages/ChatPage.css'), /\.chat-toolbar\{height:0/)
})
