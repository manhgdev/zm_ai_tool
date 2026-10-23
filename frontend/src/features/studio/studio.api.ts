import { fetchJson } from '@/shared/api/fetchJson'
import { prepareAiResources, prepareVoiceResources, recognitionResources, audioResources, type AiResourceId } from '@/features/project/project.api'

export type QueueJob = {
  id: string
  type: 'clone' | 'review' | string
  mode?: string
  source: string
  status: string
  stage: string
  progress: number
  output?: string
  outputDir?: string
  runId?: string
  projectId?: string | null
  error?: string | null
  errorType?: string | null
  log?: string[]
  outputName?: string
  settings_snapshot?: Record<string, unknown>
  parts?: Array<{
    index: number
    start?: number
    end?: number
    sourceStart?: number
    sourceEnd?: number
    outputDuration?: number | null
    label?: string
    status?: string
    output?: string
  }>
  createdAt?: number
  updatedAt?: number
}

export type QueueSnapshot = {
  jobs: QueueJob[]
  pauseAll: boolean
  diagnostics?: Record<string, unknown>
}

export type ReviewDiagnostics = {
  ollamaModels?: string[]
  llm?: string | null
}

const json = { 'Content-Type': 'application/json' }

async function prepareJob(type: string, settings: Record<string, unknown>) {
  const resources: AiResourceId[] = type === 'review'
    ? [
        ...(settings.recognitionEngine === 'capcut' ? [] : ['whisper' as const]),
        ...(settings.subtitle !== false && ['cover', 'above', 'below'].includes(String(settings.captionMode)) ? ['ocr' as const] : []),
      ]
    : [...recognitionResources(settings), ...audioResources(settings)]
  await prepareAiResources(resources)
  await prepareVoiceResources(String(settings.voice || settings.defaultVoice || ''), String(settings.language || settings.targetLang || 'auto'))
}

export const studioApi = {
  queue: () => fetchJson<QueueSnapshot>('/api/queue', undefined, 15_000),
  enqueue: async (type: 'clone' | 'review', sources: string[], settings: Record<string, unknown>, recursive = true, startNow = true) => {
    if (startNow) await prepareJob(type, settings)
    return fetchJson<{ ok: boolean; jobs: QueueJob[] }>('/api/queue', {
      method: 'POST',
      headers: json,
      body: JSON.stringify({ type, sources, settings, recursive, start_now: startNow }),
    }, 30_000)
  },
  jobAction: async (jobId: string, op: string) => {
    if (['resume', 'retry'].includes(op)) {
      const job = (await studioApi.queue()).jobs.find(item => item.id === jobId)
      if (job) await prepareJob(job.type, job.settings_snapshot || {})
    }
    return fetchJson<QueueSnapshot>(`/api/queue/${jobId}/action`, {
      method: 'POST',
      headers: json,
      body: JSON.stringify({ op }),
    })
  },
  startJobs: async (jobs: QueueJob[]) => {
    for (const job of jobs) await prepareJob(job.type, job.settings_snapshot || {})
    return fetchJson<{ ok: boolean; ids: string[] }>('/api/queue/start', {
      method: 'POST', headers: json, body: JSON.stringify({ ids: jobs.map(job => job.id) }),
    })
  },
  revealJob: (jobId: string) => fetchJson<{ ok: boolean; path: string }>(`/api/queue/${jobId}/reveal`, { method: 'POST' }),
  updateJobSettings: (jobId: string, settings: Record<string, unknown>) =>
    fetchJson<QueueSnapshot>(`/api/queue/${jobId}/settings`, {
      method: 'PATCH',
      headers: json,
      body: JSON.stringify({ settings }),
    }),
  globalAction: async (op: string) => {
    if (op === 'resume_all' || op === 'retry_failed') {
      const statuses = op === 'resume_all' ? ['paused', 'interrupted', 'queued'] : ['failed', 'interrupted']
      for (const job of (await studioApi.queue()).jobs.filter(item => statuses.includes(item.status))) {
        await prepareJob(job.type, job.settings_snapshot || {})
      }
    }
    return fetchJson<QueueSnapshot>('/api/queue/action', {
      method: 'POST',
      headers: json,
      body: JSON.stringify({ op }),
    })
  },
  generateReview: async (body: Record<string, unknown>) => {
    await prepareJob('review', body)
    return fetchJson<{ ok: boolean; job: QueueJob | null }>('/api/review/generate', {
      method: 'POST',
      headers: json,
      body: JSON.stringify(body),
    }, 30_000)
  },
  reviewStatus: (jobId: string) => fetchJson<QueueJob>(`/api/review/status?jobId=${encodeURIComponent(jobId)}`, undefined, 10_000),
  diagnostics: () => fetchJson<ReviewDiagnostics>('/api/review/diagnostics', undefined, 15_000),
  clearReviewCache: (source: string) =>
    fetchJson<{ ok: boolean; cleared: boolean }>(
      '/api/review/clear-cache',
      { method: 'POST', headers: json, body: JSON.stringify({ source }) },
      30_000,
    ),
  pickVideos: () => fetchJson<{ ok: boolean; paths: string[] }>('/api/system/pick-videos', { method: 'POST' }, 300_000),
  pickFolder: () => fetchJson<{ ok: boolean; path: string }>('/api/system/pick-folder', { method: 'POST' }, 300_000),
  uploadVideo: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return fetchJson<{ ok: boolean; path: string }>('/api/queue/upload', { method: 'POST', body }, 600_000)
  },
  fileUrl: (jobId: string, opts?: { part?: number; download?: boolean }) => {
    const q = new URLSearchParams()
    if (opts?.part != null) q.set('part', String(opts.part))
    if (opts?.download) q.set('download', '1')
    const qs = q.toString()
    return `/api/queue/${jobId}/file${qs ? `?${qs}` : ''}`
  },
  thumbnailUrl: (jobId: string) => `/api/queue/${encodeURIComponent(jobId)}/thumbnail`,
  deletePart: (jobId: string, index: number) =>
    fetchJson<QueueSnapshot>(`/api/queue/${jobId}/parts/${index}`, { method: 'DELETE' }),
}
