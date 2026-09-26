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
  concurrency?: string
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
        resolution: /^\d{3,4}p$/i.test(String(flow.resolution || '')) ? String(flow.resolution) : '', concurrency: flow.concurrency || '8',
      }
    }
  } catch {}
  return {} as SeriesGenSettings
}

/** Return a browser-playable URL for a scene video or image output path. */
export function toUrl(absPath: string, jobId?: string): string {
  if (!absPath) return ''
  const jobMatch = absPath.match(/__([a-f0-9]{12})__/)
  if (jobMatch) return `/api/flow/jobs/${jobMatch[1]}/outputs/0`
  const marker = '/public/'
  const idx = absPath.indexOf(marker)
  if (idx >= 0) return '/data/' + absPath.slice(idx + marker.length)
  if (jobId) return `/api/flow/jobs/${jobId}/outputs/0`
  return absPath
}
