import type { SrtStyle } from './srt'

export type TranscriptStyle = SrtStyle | 'original'
export type TranscriptTimedStyles = { sourceSrt: string; styles: Partial<Record<SrtStyle, string>> }
type Cue = { start: string; end: string; from: number; to: number; text: string }
type TranscriptEngine = 'whisper' | 'capcut' | 'paddleocr' | 'subtitle'
const stamp = '(\\d{2,}:([0-5]\\d):([0-5]\\d),\\d{3})'
const timing = new RegExp(`^${stamp} --> ${stamp}$`)
function millis(value: string): number {
  const [h, m, s, ms] = value.split(/[:,]/).map(Number)
  return ((h * 60 + m) * 60 + s) * 1000 + ms
}

/** Reflow only whole timed cues; never manufacture a time within a cue. */
export function formatTranscriptSrt(raw: string, style: TranscriptStyle, _engine: TranscriptEngine = 'whisper'): string {
  // "Giữ cue gốc" is deliberately byte-for-byte source SRT. CapCut already
  // returns authoritative cue timestamps; merging its short cues here makes
  // the text appear in the wrong time range. Reflow styles below may combine
  // complete cues, but never this source view.
  if (!raw.trim() || style === 'original') return raw
  const cues: Cue[] = []
  for (const block of raw.replace(/^\uFEFF/, '').trim().split(/\r?\n\s*\r?\n/)) {
    const [index, time, ...lines] = block.split(/\r?\n/)
    const match = time?.match(timing)
    // Keep partially edited/invalid input intact, rather than dropping text.
    if (!/^\d+$/.test(index) || !match || !lines.join(' ').trim()) return raw
    const start = match[1], end = match[4]
    if (millis(end) <= millis(start)) return raw
    cues.push({ start, end, from: millis(start), to: millis(end), text: lines.join(' ').trim() })
  }
  const limit = { original: Infinity, hard: 42, v916: 28, h169: 56, clause: 48, sentence: 120 }[style]
  const boundary = style === 'clause' ? /[.!?…。！？,;:，；：]["'”’)]*$/u : /[.!?…。！？]["'”’)]*$/u
  const grouped: Cue[] = []
  for (const cue of cues) {
    const previous = grouped.at(-1)
    // ponytail: without word timing we can only group existing cues. Oversized
    // cues stay intact; finer cuts require aligned word timestamps upstream.
    const gap = previous ? cue.from - previous.to : 0
    const elapsed = previous ? cue.to - previous.from : 0
    // Styled previews may combine complete source cues, but never estimate
    // an inner timestamp or move text across a pause.
    const canStyledMerge = !!previous
      && gap >= 0 && gap <= 350 && elapsed <= 6000
      && previous.text.length + cue.text.length + 1 <= limit
    if (previous && !boundary.test(previous.text) && canStyledMerge) {
      previous.text += ` ${cue.text}`
      previous.end = cue.end
      previous.to = cue.to
    } else grouped.push({ ...cue })
  }
  return grouped.map((cue, index) => `${index + 1}\n${cue.start} --> ${cue.end}\n${cue.text}`).join('\n\n')
}
