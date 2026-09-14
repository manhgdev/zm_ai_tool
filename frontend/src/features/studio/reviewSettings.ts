export type BuildMode = 'fixed' | 'stretch' | 'accumulate' | 'smart'
export type ScriptStyle = 'chuan' | 'reviewer' | 'storytelling' | 'cinematic'
export type Narration = 'default' | 'mild' | 'more'
export type PausePace = 'fast' | 'balanced' | 'slow'
export type ReviewMode = 'llm' | 'cloud' | 'translate'
export type ReviewCloudProvider = 'gemini' | 'grok' | 'openai'
export type ReviewRecognitionEngine = 'whisper' | 'capcut'

export type CaptionMode = 'off' | 'cover' | 'below' | 'above'

export const DEFAULT_REVIEW_VOICE = 'cc:BV074_streaming:7102355709945188865'

export type ReviewSettings = {
  language: string
  sourceLang: string
  recognitionEngine: ReviewRecognitionEngine
  captionMode: CaptionMode
  buildMode: BuildMode
  chunkMinutes: 10 | 15 | 20
  keepSec: number
  skipSec: number
  originalAudioPct: number
  voice: string
  genre: string
  notes: string
  scriptStyle: ScriptStyle
  reviewMode: ReviewMode
  reviewModel: string
  reviewCloudModel: string
  reviewProvider: ReviewCloudProvider
  narration: Narration
  pausePace: PausePace
}

export const STYLE_TO_PIPE: Record<ScriptStyle, string> = {
  chuan: 'normal',
  reviewer: 'recap',
  storytelling: 'deep',
  cinematic: 'cinematic',
}

export const BUILD_MODES: BuildMode[] = ['fixed', 'stretch', 'accumulate', 'smart']

export const DEFAULT_REVIEW_SETTINGS: ReviewSettings = {
  language: 'vi',
  sourceLang: 'auto',
  recognitionEngine: 'whisper',
  captionMode: 'off',
  buildMode: 'accumulate',
  chunkMinutes: 15,
  keepSec: 4,
  skipSec: 10,
  originalAudioPct: 18,
  voice: DEFAULT_REVIEW_VOICE,
  genre: 'auto',
  notes: '',
  scriptStyle: 'chuan',
  reviewMode: 'llm',
  reviewModel: 'auto',
  reviewCloudModel: 'gemini-2.5-flash',
  reviewProvider: 'gemini',
  narration: 'default',
  pausePace: 'balanced',
}

export const GENRES = [
  { id: 'auto', vi: 'Auto (Tự động nhận diện)', en: 'Auto (detect genre)' },
  { id: 'anime', vi: 'Hoạt họa AI & Anime cổ trang/tu tiên', en: 'AI animation & xianxia anime' },
  { id: 'cinema', vi: 'Tác phẩm điện ảnh / Phim chiếu rạp', en: 'Theatrical / cinema' },
  { id: 'urban', vi: 'Phim ngôn tình đô thị / Tổng tài', en: 'Urban romance / CEO' },
  { id: 'period', vi: 'Cổ trang xuyên không / Trọng sinh', en: 'Period / rebirth' },
  { id: 'survival', vi: 'Thám hiểm hoang dã / Sinh tồn', en: 'Wilderness / survival' },
]

export type Pack = { id: string; name: string; hint: string; rules: string; locked?: boolean }

export const PACK_LS = 'zm_ai_tool.reviewPacks'

export const DEFAULT_PACKS: Pack[] = GENRES.map((g) => ({
  id: g.id,
  name: g.vi,
  hint: g.vi,
  locked: g.id === 'auto',
  rules: g.id === 'auto'
    ? 'Tự động phân tích video để xác định thể loại và tự động điều chỉnh giọng điệu kể chuyện, từ ngữ thuyết minh phù hợp nhất với tính chất của từng bộ phim.'
    : `Biên soạn lời kể theo thể loại: ${g.vi}.`,
}))

export function loadPacks(): Pack[] {
  try {
    const raw = localStorage.getItem(PACK_LS)
    const parsed = raw ? JSON.parse(raw) : null
    return Array.isArray(parsed) && parsed.length ? parsed : DEFAULT_PACKS
  } catch {
    return DEFAULT_PACKS
  }
}

export function savePacks(packs: Pack[]) {
  localStorage.setItem(PACK_LS, JSON.stringify(packs))
}

export const KEEP_SKIP_PRESETS: Array<{ keep: number; skip: number }> = [
  { keep: 4, skip: 10 },
  { keep: 5, skip: 15 },
  { keep: 8, skip: 22 },
  { keep: 15, skip: 30 },
]

export function resolveBuildMode(raw: Record<string, unknown> | undefined): BuildMode {
  const mode = String(raw?.buildMode || '')
  if ((BUILD_MODES as string[]).includes(mode)) return mode as BuildMode
  const cut = String(raw?.cutMode || '')
  if ((BUILD_MODES as string[]).includes(cut)) return cut as BuildMode
  return 'accumulate'
}

export function modeLabel(mode: BuildMode, t: (vi: string, en: string) => string) {
  if (mode === 'stretch') return t('Co giãn hình theo giọng', 'Stretch to voice')
  if (mode === 'accumulate') return t('Phân đoạn tích lũy', 'Cumulative segments')
  if (mode === 'smart') return t('Cắt thông minh', 'Smart cut')
  return t('Khung hình cố định', 'Fixed frames')
}
