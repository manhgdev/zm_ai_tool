/** TTS Studio UI preferences — localStorage, not project/backend settings. */

export const TTS_SETTINGS_KEY = 'zm-ai-tool:tts-settings:v1'
export const DEFAULT_CAPCUT_VOICE = 'cc:BV074_streaming:7102355709945188865'

export type TtsEngine = 'all' | 'zmai' | 'vieneu' | 'clone' | 'capcut' | 'eleven' | 'system'
export type TtsOutputFormat = 'wav48' | 'wav16' | 'mp3'

export type TtsSettings = {
  lang: string
  engine: TtsEngine
  voice: string
  style: string
  speed: number
  volume: number
  pitch: number
  matchSrt: boolean
  keepTimeline: boolean
  normalize: boolean
  gapOn: boolean
  gapMs: number
  trimSilence: boolean
  autoSplit: boolean
  playbackVolume: number
  outputFormat: TtsOutputFormat
}

export const defaultTtsSettings: TtsSettings = {
  lang: 'auto',
  engine: 'all',
  voice: DEFAULT_CAPCUT_VOICE,
  style: 'tu_nhien',
  speed: 1,
  volume: 1,
  pitch: 0,
  matchSrt: true,
  keepTimeline: true,
  normalize: true,
  gapOn: false,
  gapMs: 300,
  trimSilence: true,
  autoSplit: true,
  playbackVolume: 1,
  outputFormat: 'wav48',
}

const ENGINES = new Set<TtsEngine>(['all', 'zmai', 'vieneu', 'clone', 'capcut', 'eleven', 'system'])
const STYLES = new Set(['tu_nhien', 'tin_tuc', 'doc_truyen'])
const OUTPUTS = new Set<TtsOutputFormat>(['wav48', 'wav16', 'mp3'])
const LANGS = new Set([
  'auto', 'vi', 'en', 'zh', 'ja', 'ko', 'th', 'id', 'es', 'fr', 'de', 'pt',
])

function clamp(n: number, lo: number, hi: number, fallback: number): number {
  if (typeof n !== 'number' || Number.isNaN(n) || !Number.isFinite(n)) return fallback
  return Math.min(hi, Math.max(lo, n))
}

function asBool(v: unknown, fallback: boolean): boolean {
  return typeof v === 'boolean' ? v : fallback
}

function asStr(v: unknown, fallback: string): string {
  return typeof v === 'string' ? v : fallback
}

/** Parse raw JSON (object or string). Corrupt / partial → defaults for missing fields. */
export function parseTtsSettings(raw: unknown): TtsSettings {
  let obj: Record<string, unknown> = {}
  if (typeof raw === 'string') {
    try {
      const parsed: unknown = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        obj = parsed as Record<string, unknown>
      }
    } catch {
      return { ...defaultTtsSettings }
    }
  } else if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    obj = raw as Record<string, unknown>
  } else {
    return { ...defaultTtsSettings }
  }

  const d = defaultTtsSettings
  const engine = asStr(obj.engine, d.engine)
  const style = asStr(obj.style, d.style)
  const outputFormat = asStr(obj.outputFormat, d.outputFormat)
  const lang = asStr(obj.lang, d.lang)
  const voice = asStr(obj.voice, d.voice)

  return {
    lang: LANGS.has(lang) ? lang : d.lang,
    engine: ENGINES.has(engine as TtsEngine) ? (engine as TtsEngine) : d.engine,
    voice: voice.length <= 200 ? voice : d.voice,
    style: STYLES.has(style) ? style : d.style,
    speed: clamp(Number(obj.speed), 0.5, 2, d.speed),
    volume: clamp(Number(obj.volume), 0.5, 2, d.volume),
    pitch: clamp(Math.round(Number(obj.pitch)), -12, 12, d.pitch),
    matchSrt: asBool(obj.matchSrt, d.matchSrt),
    keepTimeline: asBool(obj.keepTimeline, d.keepTimeline),
    normalize: asBool(obj.normalize, d.normalize),
    gapOn: asBool(obj.gapOn, d.gapOn),
    gapMs: clamp(Math.round(Number(obj.gapMs)), 50, 2000, d.gapMs),
    trimSilence: asBool(obj.trimSilence, d.trimSilence),
    autoSplit: asBool(obj.autoSplit, d.autoSplit),
    playbackVolume: clamp(Number(obj.playbackVolume), 0, 1, d.playbackVolume),
    outputFormat: OUTPUTS.has(outputFormat as TtsOutputFormat)
      ? (outputFormat as TtsOutputFormat)
      : d.outputFormat,
  }
}

export function loadTtsSettings(): TtsSettings {
  try {
    const raw = localStorage.getItem(TTS_SETTINGS_KEY)
    if (!raw) return { ...defaultTtsSettings }
    return parseTtsSettings(raw)
  } catch {
    return { ...defaultTtsSettings }
  }
}

export function persistTtsSettings(s: TtsSettings): void {
  try {
    localStorage.setItem(TTS_SETTINGS_KEY, JSON.stringify(parseTtsSettings(s)))
  } catch {
    /* quota / private mode */
  }
}

/** ponytail: self-check — serialize/validate falls back safely */
export function __checkTtsSettings(): void {
  const base = parseTtsSettings(null)
  if (base.engine !== 'all' || base.voice !== DEFAULT_CAPCUT_VOICE || base.speed !== 1) {
    throw new Error('defaults mismatch')
  }

  const ok = parseTtsSettings({
    lang: 'vi',
    engine: 'clone',
    voice: 'vn:clone:demo',
    style: 'tin_tuc',
    speed: 1.25,
    volume: 0.8,
    pitch: 3,
    matchSrt: false,
    keepTimeline: false,
    normalize: false,
    gapOn: true,
    gapMs: 500,
    trimSilence: false,
    autoSplit: false,
    playbackVolume: 0.4,
    outputFormat: 'mp3',
  })
  if (ok.lang !== 'vi' || ok.engine !== 'clone' || ok.voice !== 'vn:clone:demo') {
    throw new Error('valid fields not kept')
  }
  if (ok.speed !== 1.25 || ok.gapMs !== 500 || ok.outputFormat !== 'mp3') {
    throw new Error('numeric/enum fields not kept')
  }

  const bad = parseTtsSettings({
    engine: 'hacked',
    speed: 99,
    pitch: 'nope',
    gapMs: -1,
    lang: 'xx',
    style: '???',
    outputFormat: 'flac',
    voice: 'x'.repeat(300),
  })
  if (bad.engine !== 'all') throw new Error('bad engine must fallback')
  if (bad.speed !== 2) throw new Error('out-of-range speed must clamp')
  if (bad.pitch !== 0) throw new Error('bad pitch must fallback')
  if (bad.gapMs !== 50) throw new Error('out-of-range gapMs must clamp')
  if (bad.lang !== 'auto') throw new Error('bad lang must fallback')
  if (bad.style !== 'tu_nhien') throw new Error('bad style must fallback')
  if (bad.outputFormat !== 'wav48') throw new Error('bad output must fallback')
  if (bad.voice !== DEFAULT_CAPCUT_VOICE) throw new Error('oversized voice must fallback')

  const partial = parseTtsSettings({ engine: 'capcut', speed: 1.5 })
  if (partial.engine !== 'capcut' || partial.speed !== 1.5) throw new Error('partial merge failed')
  if (partial.matchSrt !== true || partial.autoSplit !== true) {
    throw new Error('missing fields must keep defaults')
  }

  const junk = parseTtsSettings('{not json')
  if (junk.engine !== 'all' || junk.voice !== DEFAULT_CAPCUT_VOICE) {
    throw new Error('corrupt JSON must fallback')
  }
}
