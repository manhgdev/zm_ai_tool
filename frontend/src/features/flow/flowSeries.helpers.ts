// Types và helpers cho FlowSeriesPanel
import type { FlowCapabilityCatalog } from './flow.types'

export type SeriesArtifact = 'keyframe' | 'video'
export type FlowSeriesSceneContext = {
  seriesId: string
  episodeId: string
  sceneId: string
  artifact: SeriesArtifact
  seriesTitle: string
  episodeTitle: string
  sceneTitle: string
  scenePrompt: string
}
export type SeriesGenSettings = {
  accountId: string
  model: string
  ratio: string
  duration: string
  resolution: string
  quality?: string
  concurrency?: string
}

export type MergeSettings = {
  // ── Output ──
  resolution: string       // 'auto' | '1920x1080' | '1080x1920' | '1280x720' | '1080x1080'
  targetPlatform: string   // 'auto' | 'youtube' | 'shorts' | 'tiktok'
  fps: number
  crf: number
  encoder: 'auto' | 'gpu' | 'cpu'
  speed: number            // 100 = 1×
  volume: number           // 100 = original
  previewSeconds: number
  removeMetadata: boolean
  // ── Transitions & motion ──
  effect: string           // 'none' | 'fade' | 'dissolve' | 'random'
  transitionDuration: number
  zoom: string             // 'off' | 'zoomIn' | 'zoomOut' | 'random' | 'left' | 'right' | 'up' | 'down'
  // ── Subtitle ──
  subtitleEnabled: boolean
  subtitleFontFamily: string
  subtitleSize: number
  subtitleOffset: number
  subtitleMargin: number
  subtitleBackground: string  // 'solid' | 'blur' | 'none'
  subtitleColor: string
  subtitleBgColor: string
  subtitleOpacity: number
  // ── Drawing ──
  drawingEnabled: boolean
  drawingMode: string
  drawingTool: string
  drawingHandId: string
  drawingDetail: number
  drawingThickness: number
  drawingStrokeOrder: string
  // ── Delogo ──
  delogoEnabled: boolean
  delogoAuto: boolean
  delogoX: number
  delogoY: number
  delogoW: number
  delogoH: number
  // ── Logo / watermark ──
  logoEnabled: boolean
  logoSource: 'text' | 'image' | 'icon'
  logoText: string
  logoIcon: string
  logoFontSize: number
  logoColor: string
  logoSize: number
  logoOpacity: number
  logoX: number
  logoY: number
  logoMotion: string   // 'fixed' | 'random'
  logoScope: string    // 'full' | 'range'
  logoStart: number
  logoEnd: number
  logoVisibleSec: number
  logoHiddenSec: number
  logoFadeSec: number
  logoSafeMargin: number
}

export const SERIES_MERGE_SETTINGS_KEY = 'zm-flow-series:merge-settings:v1'
export const DEFAULT_MERGE_SETTINGS: MergeSettings = {
  resolution: 'auto', targetPlatform: 'auto', fps: 30, crf: 20, encoder: 'auto',
  speed: 100, volume: 100, previewSeconds: 0, removeMetadata: false,
  effect: 'none', transitionDuration: 0.28, zoom: 'off',
  subtitleEnabled: false, subtitleFontFamily: 'system', subtitleSize: 8, subtitleOffset: 0,
  subtitleMargin: 34, subtitleBackground: 'solid', subtitleColor: '#ffffff',
  subtitleBgColor: '#000000', subtitleOpacity: 55,
  drawingEnabled: false, drawingMode: 'hand', drawingTool: 'pen', drawingHandId: 'pen',
  drawingDetail: 72, drawingThickness: 2, drawingStrokeOrder: 'natural',
  delogoEnabled: false, delogoAuto: true, delogoX: 80, delogoY: 82, delogoW: 18, delogoH: 12,
  logoEnabled: false, logoSource: 'text', logoText: 'ZM AI TOOL', logoIcon: '★',
  logoFontSize: 32, logoColor: '#ffffff', logoSize: 8, logoOpacity: 85,
  logoX: 88, logoY: 88, logoMotion: 'fixed', logoScope: 'full',
  logoStart: 0, logoEnd: 10, logoVisibleSec: 4, logoHiddenSec: 2, logoFadeSec: 0.5, logoSafeMargin: 4,
}
export type FlowSeriesAccount = {
  id: string; label: string; status: string; plan?: 'Ultra' | 'Pro' | 'Plus' | 'Free'
  capabilityCatalog?: FlowCapabilityCatalog | null
  capabilityStatus?: 'verified' | 'stale' | 'unknown'
}

export type SeriesRun = {
  runId: string; status: string; total: number; done: number
  currentSceneId: string; currentStep: string
  errors: { sceneId: string; error: string }[]
}
export type AutoMode = 'full' | 'keyframes_only' | 'videos_only'

export type Scene = {
  id: string; index: number; title: string; prompt: string; timecode: string; status: string
  continuityEnabled: boolean; referenceAssetIds: string[]; promptOverride: string
  approvedKeyframe: string; keyframeJobId: string; keyframeOutput: string
  videoJobId: string; videoOutput: string; endFrame: string; error: string
}
export type Episode = { id: string; index: number; title: string; state: string; scenes: Scene[] }
export type Asset = { id: string; name: string; label: string; locked: boolean }
export type Series = { id: string; title: string; description: string; bible: string; anchorAssets: string[]; assets: Asset[]; episodes: Episode[] }

export const VIDEO_MODELS = ['Veo 3.1 - Lite', 'Veo 3.1 - Fast', 'Veo 3.1 - Quality', 'Omni 1.1 Flash'] as const
export const IMAGE_MODELS = ['Nano Banana Pro', 'Nano Banana 2', 'Nano Banana 2 Lite'] as const
export const SERIES_SETTINGS_KEY = 'zm-flow-series:settings:v1'
export const SERIES_SELECTED_ID_KEY = 'zm-flow-series:selected-id:v1'
export const SERIES_TAB_KEY = 'zm-flow-series:active-tab:v1'
export const SERIES_AUTO_MODE_KEY = 'zm-flow-series:auto-mode:v1'
export const SERIES_AUTO_APPROVE_KEY = 'zm-flow-series:auto-approve:v1'
export const SERIES_COLLAPSED_EPISODES_KEY = 'zm-flow-series:collapsed-episodes:v1'
export const SERIES_AI_KEY = 'zm-flow-series:ai-text:v1'

/** Episode/scene counts of a Series TXT, using the same line rules as the backend importer. */
export function countSeriesScript(text: string) {
  const lines = text.split('\n').map((line) => line.trim())
  return {
    title: lines.map((line) => line.match(/^#\s*SERIES\s*:\s*(.+)$/i)?.[1]).find(Boolean) || '',
    episodes: lines.filter((line) => /^#\s*TẬP\s*\d+/i.test(line)).length,
    scenes: lines.filter((line) => /^\d{1,3}_\[[^\]]+\]\s*\S/.test(line)).length,
  }
}

export function normalizeSeries(raw: Partial<Series>): Series {
  return {
    id: String(raw.id || ''), title: String(raw.title || ''), description: String(raw.description || ''), bible: String(raw.bible || ''),
    anchorAssets: Array.isArray(raw.anchorAssets) ? raw.anchorAssets : [],
    assets: Array.isArray(raw.assets) ? raw.assets : [],
    episodes: Array.isArray(raw.episodes)
      ? raw.episodes.map((ep) => ({ ...ep, scenes: Array.isArray(ep.scenes) ? ep.scenes : [] }))
      : [],
  }
}

export async function seriesRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/flow${path}`, options)
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    const err = new Error(typeof detail?.detail === 'string' ? detail.detail : response.statusText)
    ;(err as Error & { status: number }).status = response.status
    throw err
  }
  return response.json() as Promise<T>
}

export function sceneStatusMeta(status: string, t: (vi: string, en: string) => string) {
  const map: Record<string, { label: string; cls: string }> = {
    draft: { label: t('Nháp', 'Draft'), cls: 'status-draft' },
    ready_video: { label: t('Sẵn sàng tạo video', 'Ready for video'), cls: 'status-ready' },
    generating_keyframe: { label: t('Đang tạo ảnh', 'Generating…'), cls: 'status-generating' },
    awaiting_keyframe: { label: t('Chờ duyệt ảnh', 'Awaiting approval'), cls: 'status-awaiting' },
    generating_video: { label: t('Đang tạo video', 'Generating video…'), cls: 'status-generating' },
    complete: { label: t('Hoàn thành', 'Completed'), cls: 'status-complete' },
    error: { label: t('Lỗi', 'Error'), cls: 'status-error' },
  }
  return map[status] || { label: status, cls: '' }
}

export function readSeriesSettings(): SeriesGenSettings {
  try {
    const raw = localStorage.getItem(SERIES_SETTINGS_KEY)
    if (raw) {
      const saved = JSON.parse(raw)
      return {
        ...saved,
        model: saved.model === 'Veo 3.1 - Lite [Lower Priority]' ? 'Veo 3.1 - Fast' : saved.model || 'Veo 3.1 - Fast',
      }
    }
    const flowRaw = localStorage.getItem('zm-flow-veo:settings:v1')
    if (flowRaw) {
      const flow = JSON.parse(flowRaw)
      return {
        accountId: '', model: flow.model === 'Veo 3.1 - Lite [Lower Priority]' ? 'Veo 3.1 - Fast' : flow.model || 'Veo 3.1 - Fast',
        ratio: flow.ratio || '16:9', duration: flow.duration || '8',
        resolution: /^\d{3,4}p$/i.test(String(flow.resolution || '')) ? String(flow.resolution) : '', concurrency: flow.concurrency || '3',
      }
    }
  } catch {}
  return {} as SeriesGenSettings
}

/** Return a browser-playable URL for a scene video or image output path. */
export function toUrl(absPath: string, jobId?: string, seriesId?: string): string {
  if (!absPath) return ''
  const jobMatch = absPath.match(/__([a-f0-9]{12})__/)
  if (jobMatch) return `/api/flow/jobs/${jobMatch[1]}/outputs/0`
  if (seriesId) {
    const normalized = absPath.replaceAll('\\', '/')
    const marker = `/series/${seriesId}/assets/`
    const index = normalized.indexOf(marker)
    if (index >= 0) {
      const filename = normalized.slice(index + marker.length).split('/')[0]
      if (filename) return `/api/flow/series/${encodeURIComponent(seriesId)}/media/${encodeURIComponent(filename)}`
    }
  }
  const marker = '/public/'
  const idx = absPath.indexOf(marker)
  if (idx >= 0) return '/data/' + absPath.slice(idx + marker.length)
  if (jobId) return `/api/flow/jobs/${jobId}/outputs/0`
  return absPath
}
