/** Logic thuần: bucket engine, tên hiển thị và metadata (mô tả + tag) của giọng. */
import type { TtsEngine } from '../ttsSettings'
import { canonicalVoiceTags } from '../VoiceMetadataModal'
import type { Voice } from '../tts.types'

export function engineOf(voiceId: string) {
  if (!voiceId) return '—'
  if (voiceId.startsWith('vn:clone:')) return 'Clone'
  if (voiceId.startsWith('vn:')) return 'VieNeu Local'
  if (voiceId.startsWith('cc:')) return 'CapCut TTS'
  if (voiceId.startsWith('el:')) return 'ElevenLabs'
  if (voiceId.startsWith('win:') || voiceId === 'system' || voiceId.startsWith('espeak:')) return 'System'
  // bare id = giọng tham chiếu zmAI
  return 'zmAI'
}

/** Bucket rõ ràng — clone không lẫn VieNeu Local / zmAI. */
export function voiceEngineBucket(v: Voice): TtsEngine {
  if (v.type === 'zmAI' || v.engine === 'zmai') return 'zmai'
  if (v.type === 'clone' || v.engine === 'clone' || v.id.startsWith('vn:clone:')) return 'clone'
  if (v.engine === 'capcut' || v.id.startsWith('cc:')) return 'capcut'
  if (v.id.startsWith('el:')) return 'eleven'
  if (v.type === 'preset' || v.engine === 'vieneu' || v.id.startsWith('vn:')) return 'vieneu'
  return 'system'
}

export type VoiceTag = {
  label: string
  kind: 'source' | 'gender' | 'accent' | 'category' | 'language' | 'editable'
}

export const LANGUAGE_NAMES: Record<string, string> = {
  vi: 'Tiếng Việt',
  en: 'Tiếng Anh',
  zh: 'Tiếng Trung',
  ja: 'Tiếng Nhật',
  ko: 'Tiếng Hàn',
  th: 'Tiếng Thái',
  id: 'Tiếng Indonesia',
  es: 'Tiếng Tây Ban Nha',
  fr: 'Tiếng Pháp',
  de: 'Tiếng Đức',
  pt: 'Tiếng Bồ Đào Nha',
}

export const VOICE_TRAIT_NAMES: Record<string, string> = {
  male: '👨 Nam',
  female: '👩 Nữ',
  bac: '🏔️ Miền Bắc',
  bắc: '🏔️ Miền Bắc',
  nam: '🌴 Miền Nam',
  tu_nhien: 'Tự nhiên',
  tin_tuc: '📰 Tin tức',
  doc_truyen: 'Kể chuyện',
  doc_tho: '📜 Đọc thơ',
  quang_cao: '📢 Quảng cáo',
  tre_em: '👶 Trẻ em',
  young: '👶 Trẻ em',
  old: '👴 Người già',
  elderly: '👴 Người già',
  review: '⭐ Review',
}

export function voiceMetadata(v: Voice): { description: string; tags: VoiceTag[] } {
  const bucket = voiceEngineBucket(v)
  const source =
    bucket === 'zmai' ? 'zmAI' :
      bucket === 'vieneu' ? 'VieNeu Local' :
        bucket === 'clone' ? 'Clone' :
          bucket === 'capcut' ? 'CapCut' :
            bucket === 'eleven' ? 'ElevenLabs' : 'Hệ thống'
  const description = v.description?.trim() ||
    (bucket === 'clone' ? 'Giọng tùy chỉnh được tạo từ audio mẫu.' :
      bucket === 'zmai' ? 'Giọng tham chiếu cục bộ của zmAI.' :
        bucket === 'system' ? 'Giọng hệ thống theo ngôn ngữ đích.' :
          `Giọng từ ${source}.`)
  const tags: VoiceTag[] = [{ label: source, kind: 'source' }]
  const editableTags = canonicalVoiceTags(v.tags)
  tags.push(...editableTags.map((label) => ({ label, kind: 'editable' as const })))
  const add = (raw: string | undefined, kind: VoiceTag['kind'], map = true) => {
    const value = raw?.trim()
    if (!value) return
    const key = value.toLowerCase().replace(/\s+/g, '_')
    const label = map ? (VOICE_TRAIT_NAMES[key] || value) : value
    if (!tags.some((tag) => tag.label.toLowerCase() === label.toLowerCase())) tags.push({ label, kind })
  }
  add(v.gender, 'gender')
  add(v.accent, 'accent')
  add(v.style, 'category')
  add(v.category, 'category')
  add(v.age, 'category')
  const languageCode = v.language?.toLowerCase().split(/[-_]/)[0]
  if (languageCode) add(LANGUAGE_NAMES[languageCode] || v.language, 'language', false)
  return { description, tags }
}

export function engineLabel(engine?: string, voiceId?: string) {
  const e = (engine || '').toLowerCase()
  if (e === 'zmai' || e === 'zmai_ref' || e === 'reference') return 'zmAI'
  if (e === 'clone') return 'Clone'
  if (e === 'vieneu' || e === 'vn') {
    // meta cũ gộp hết vào vieneu — suy ra đúng bucket từ voice id
    if (voiceId?.startsWith('vn:clone:')) return 'Clone'
    if (voiceId && !voiceId.startsWith('vn:')) return 'zmAI'
    return 'VieNeu Local'
  }
  if (e === 'capcut' || e === 'cc') return 'CapCut TTS'
  if (e === 'elevenlabs' || e === 'eleven' || e === 'el') return 'ElevenLabs'
  if (e === 'system') return 'System'
  if (voiceId) return engineOf(voiceId)
  return engine || '—'
}

/** Bỏ prefix engine lặp (VieNeu · Clone · …) — kể cả đã stack nhiều lần. */
export function stripEngineNamePrefix(name: string): string {
  let s = name.trim().replace(/\s+/g, ' ')
  const re =
    /^(?:VieNeu\s*[·•.\-]\s*(?:Clone\s*[·•.\-]\s*)?|CapCut\s*[·•.\-]\s*|ElevenLabs\s*[·•.\-]\s*|macOS\s*[·•.\-]\s*)+/i
  for (let i = 0; i < 8; i++) {
    const next = s.replace(re, '').replace(/^[·•.\-\s]+/, '').trim()
    if (next === s) break
    s = next
  }
  return s
}

/** Tên hiển thị: meta.voiceName → list voices → fallback id */
export function voiceDisplayName(
  voiceId: string | undefined,
  voices: Voice[],
  voiceName?: string,
): string {
  if (voiceName?.trim()) {
    const cleaned = stripEngineNamePrefix(voiceName)
    if (cleaned) return cleaned
  }
  if (!voiceId) return '—'
  const hit = voices.find((v) => v.id === voiceId)
  if (hit?.name) {
    const cleaned = stripEngineNamePrefix(hit.name)
    if (cleaned) return cleaned
  }
  if (voiceId.startsWith('cc:')) {
    // cc:voice_type:resource_id → voice_type dễ đọc hơn rid
    const rest = voiceId.slice(3)
    const type = rest.includes(':') ? rest.slice(0, rest.lastIndexOf(':')) : rest
    return type || rest
  }
  return voiceId.replace(/^vn:clone:/, '').replace(/^vn:|^el:/, '')
}
