import { useEffect, useState } from 'react'

/** Input số gắn với slider — commit khi blur/Enter */
export function SliderNumber({
  value, min, max, step, label, onChange,
}: {
  value: number; min: number; max: number; step: number
  label: string; onChange: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => setDraft(String(value)), [value])

  function commit() {
    const parsed = Number(draft.replace(',', '.'))
    if (!Number.isFinite(parsed)) { setDraft(String(value)); return }
    const next = Math.min(max, Math.max(min, parsed))
    setDraft(String(next))
    onChange(next)
  }

  return (
    <input
      className="tts-slider-number"
      type="number" min={min} max={max} step={step}
      value={draft} aria-label={label}
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
    />
  )
}

export const WAVE_BARS = Array.from({ length: 180 }, (_, i) => {
  const t = i / 180
  const taper = 0.5 + Math.sin(Math.PI * Math.min(1, t * 1.15)) * 0.5
  return 3 + (Math.abs(Math.sin(t * 43)) * 12 + Math.abs(Math.cos(t * 91)) * 5) * taper
})

export const FULL_DASHBOARD = new Set(['overview', 'make'])
export const COMING_SOON = new Set(['engines', 'audio', 'match', 'advanced'])

export const SECTION_LABELS: Record<string, string> = {
  overview: 'Tổng quan', input: 'Nhập văn bản', srt: 'Nhập SRT / Phụ đề',
  make: 'Tạo giọng nói', history: 'Lịch sử tạo', voice: 'Danh sách giọng',
  clone: 'Clone giọng nói', engines: 'TTS Engines', audio: 'Cấu hình âm thanh',
  match: 'Khớp thời lượng', advanced: 'Tùy chọn nâng cao',
}

export const TTS_URL_SECTIONS = new Set([
  'overview', 'history', 'voice', 'clone', 'engines', 'audio', 'match', 'advanced',
])

export function sectionFromUrl(): string {
  if (typeof window === 'undefined') return 'overview'
  const section = new URLSearchParams(window.location.search).get('tab') || ''
  return TTS_URL_SECTIONS.has(section) ? section : 'overview'
}

export const FAVORITE_LS_KEY = 'zm-ai-tool:tts-voice-favorites'
export const OUTPUT_DIR_LS_KEY = 'zm-ai-tool:tts-output-dir.v1'
export const TTS_TEXT_LS_KEY = 'zm-ai-tool:tts-text:v1'
export const TTS_SRT_LS_KEY = 'zm-ai-tool:tts-srt:v1'
export const TTS_INPUT_MODE_LS_KEY = 'zm-ai-tool:tts-input-mode:v1'
export const TTS_ACTIVE_JOB_LS_KEY = 'zm-ai-tool:tts-active-job:v1'
