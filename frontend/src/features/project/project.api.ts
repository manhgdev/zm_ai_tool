import type {
  AppConfig,
  HardwareInfo,
  JobStatus,
  ProjectSettings,
  RenderedVideo,
  Segment,
  SystemChecks,
  TextOverlay,
  ProjectMediaAsset,
} from '@/features/project/project.types'
import { fetchJson } from '@/shared/api/fetchJson'

const base = '/api'

export type InstallStatus = {
  running: boolean
  kind?: string
  ok?: boolean
  message?: string
  detail?: string
  error?: string
  needsRestart?: boolean
  log?: string
  progress?: number
  startedAt?: number
  updatedAt?: number
  stage?: 'detect_hardware' | 'fetch_manifest' | 'download' | 'verify_checksum' | 'extract' | 'probe' | 'activate' | 'rollback' | ''
  runtimePack?: string
  downloadedBytes?: number
  totalBytes?: number
  requiredDiskBytes?: number
  errorCode?: 'HARDWARE_UNSUPPORTED' | 'DRIVER_TOO_OLD' | 'DOWNLOAD_FAILED' | 'CHECKSUM_MISMATCH' | 'DISK_FULL' | 'EXTRACT_FAILED' | 'RUNTIME_PROBE_FAILED' | 'ACTIVATION_FAILED' | string
  retryable?: boolean
  diagnostics?: string
}

async function pollInstall(
  url: string,
  onStatus?: (status: InstallStatus) => void,
): Promise<{
  ok: boolean
  message: string
  detail: string
  needsRestart?: boolean
}> {
  const kick = await fetchJson<InstallStatus & { ok: boolean }>(url, { method: 'POST' }, 60_000)
  onStatus?.({ ...kick, running: kick.running !== false })
  if (kick.running === false) {
    return {
      ok: kick.ok ?? true,
      message: kick.message || 'Xong',
      detail: kick.detail || '',
      needsRestart: kick.needsRestart,
    }
  }
  let pollMs = 2000
  for (;;) {
    const st = await fetchJson<InstallStatus>(`${base}/system/install/status`, undefined, 30_000)
    onStatus?.(st)
    if (st.error) throw new Error(st.error)
    if (!st.running) {
      return {
        ok: st.ok ?? true,
        message: st.message || 'Xong',
        detail: st.detail || '',
        needsRestart: st.needsRestart,
      }
    }
    await new Promise((r) => window.setTimeout(r, pollMs))
    // Backoff: 2s → 3s → 4s → 5s cap — pip không cần feedback liên tục
    pollMs = Math.min(pollMs + 1000, 5000)
  }
}

export const api = {
  health: () =>
    fetchJson<{ ok: boolean; app?: string }>(`${base}/health`, undefined, 3_000),

  renders: () => fetchJson<{ items: RenderedVideo[]; canReveal: boolean }>(`${base}/renders`, undefined, 30_000),

  revealRender: (renderId: string) =>
    fetchJson<{ ok: boolean; path: string }>(`${base}/renders/${renderId}/reveal`, { method: 'POST' }),

  renameRender: (renderId: string, name: string) =>
    fetchJson<{ renderId: string; name: string }>(`${base}/renders/${renderId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),

  deleteRender: (renderId: string) =>
    fetchJson<{ ok: boolean }>(`${base}/renders/${renderId}`, { method: 'DELETE' }),

  deleteRendersBatch: (renderIds: string[] = [], all = false, mediaType = 'all') =>
    fetchJson<{ ok: boolean; deletedCount: number; deletedIds?: string[] }>(`${base}/renders/delete-batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ renderIds, all, mediaType }),
    }),

  hardware: () => fetchJson<HardwareInfo>(`${base}/hardware`, undefined, 40_000),
  hardwareUsage: () => fetchJson<import('./project.types').HardwareUsage>(`${base}/hardware/usage`, undefined, 5_000),

  systemChecks: (refresh = false, _deep = false) =>
    fetchJson<SystemChecks>(
      `${base}/system/checks${refresh ? '?refresh=1' : ''}`,
      undefined,
      40_000,
    ),

  resources: () => fetchJson<{ items: import('./project.types').AiResource[] }>(`${base}/resources`, undefined, 20_000),

  installResource: (resourceId: string, onStatus?: (status: InstallStatus) => void) =>
    pollInstall(`${base}/resources/${encodeURIComponent(resourceId)}/install`, onStatus),

  runOcrTranslate: (projectId: string, settings: ProjectSettings) => fetchJson<{ ok: boolean }>(`${base}/projects/${projectId}/ocr-translate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) }, 30_000),

  mediaTimeline: (projectId: string) => fetchJson<{ items: Array<{ id: string; assetId: string; name: string; kind: 'video' | 'audio' | 'image'; start: number; end: number }> }>(`${base}/projects/${projectId}/media-timeline`, undefined, 10_000),
  replaceMediaTimeline: (projectId: string, items: Array<{ id: string; assetId: string; name: string; kind: 'video' | 'audio' | 'image'; start: number; end: number }>) => fetchJson<{ items: typeof items }>(`${base}/projects/${projectId}/media-timeline`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(items) }, 15_000),

  ollamaSignin: () =>
    fetchJson<{ ok: boolean; message: string }>(
      `${base}/system/ollama/signin`,
      { method: 'POST' },
      15_000,
    ),

  getSetupGate: () =>
    fetchJson<{ passed: boolean }>(`${base}/system/setup-gate`, undefined, 5_000),

  passSetupGate: () =>
    fetchJson<{ passed: boolean }>(`${base}/system/setup-gate`, { method: 'POST' }, 5_000),

  installStatus: () =>
    fetchJson<InstallStatus>(`${base}/system/install/status`, undefined, 30_000),

  rollbackAiRuntime: () =>
    fetchJson<{ ok: boolean; message: string; needsRestart?: boolean }>(
      `${base}/system/install/rollback`,
      { method: 'POST' },
      30_000,
    ),

  installAiRuntime: (onStatus?: (status: InstallStatus) => void) => pollInstall(`${base}/system/install/ai_runtime`, onStatus),

  installOcrCuda: (onStatus?: (status: InstallStatus) => void) => pollInstall(`${base}/system/install/ocr_cuda`, onStatus),

  installDemucsCuda: (onStatus?: (status: InstallStatus) => void) => pollInstall(`${base}/system/install/demucs_cuda`, onStatus),

  installNvm: (onStatus?: (status: InstallStatus) => void) => pollInstall(`${base}/system/install/nvm`, onStatus),

  restartApp: () =>
    fetchJson<{ ok: boolean; message: string }>(
      `${base}/system/restart`,
      { method: 'POST' },
      15_000,
    ),

  checkAppUpdate: () =>
    fetchJson<{
      desktop: boolean
      supported?: boolean
      currentVersion: string
      latestVersion?: string
      releaseAvailable?: boolean
      updateAvailable: boolean
      assetAvailable?: boolean
      assetName?: string
      releaseUrl?: string
      notes?: string
    }>(`${base}/system/update/check`, undefined, 20_000),

  installAppUpdate: () =>
    fetchJson<{ ok: boolean; running: boolean; message: string }>(
      `${base}/system/update/install`,
      { method: 'POST' },
      20_000,
    ),

  getAppUpdateStatus: () =>
    fetchJson<{
      desktop: boolean
      running: boolean
      phase: 'idle' | 'checking' | 'downloading' | 'ready' | 'applying' | 'complete' | 'error'
      progress: number
      message: string
      error: string
      latestVersion: string
      assetName: string
    }>(`${base}/system/update/status`, undefined, 20_000),

  applyAppUpdate: () =>
    fetchJson<{ ok: boolean; message: string }>(
      `${base}/system/update/apply`,
      { method: 'POST' },
      20_000,
    ),

  /** Log app (job lỗi, crash) — tab Cấu hình → Log */
  getAppLogs: (tail = 800) =>
    fetchJson<{ path: string; text: string; lines: number; desktop?: boolean }>(
      `${base}/system/logs?tail=${Math.max(50, Math.min(5000, tail))}`,
      undefined,
      15_000,
    ),

  clearAppLogs: () =>
    fetchJson<{ ok: boolean; path?: string; error?: string }>(
      `${base}/system/logs`,
      { method: 'DELETE' },
      10_000,
    ),

  getConfig: () => fetchJson<AppConfig>(`${base}/config`, undefined, 20_000),

  getLocalePreference: () =>
    fetchJson<{ locale: 'vi' | 'en' | null }>(`${base}/ui-preferences`, undefined, 5_000),

  saveLocalePreference: (locale: 'vi' | 'en') =>
    fetchJson<{ locale: 'vi' | 'en' }>(`${base}/ui-preferences`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ locale }),
    }, 5_000),

  saveOutputRoot: (outputRoot: string) =>
    fetchJson<{ outputRoot: string | null }>(`${base}/ui-preferences`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outputRoot }),
    }, 5_000),

  saveConfig: (body: {
    cloud?: Record<
      string,
      { apiKey?: string; apiKeys?: string; keys?: string[]; baseUrl?: string; model?: string }
    >
    tts?: {
      elevenlabs?: { apiKeys?: string; keys?: string[] }
    }
  }) =>
    fetchJson<AppConfig>(`${base}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  voices: (lang = 'vi') =>
    fetchJson<{
      id: string
      name: string
      previewUrl?: string
      type?: string
      engine?: string
      description?: string
      gender?: string
      language?: string
      accent?: string
      age?: string
      style?: string
      category?: string
    }[]>(
      `${base}/voices?lang=${encodeURIComponent(lang)}`,
      undefined,
      15_000,
    ),

  ttsStatus: () =>
    fetchJson<Record<string, {
      id?: string
      name?: string
      local?: boolean
      installed?: boolean
      ready?: boolean
      loaded?: boolean
      loadState?: string
      device?: string
      model?: string
      version?: string
      message?: string
      presetCount?: number
      installHint?: string
      cloneRequiresPytorch?: boolean
    }>>(`${base}/tts/status`, undefined, 15_000),

  ttsStudioSynth: (body: {
    jobId?: string
    text?: string
    srtText?: string
    voice: string
    lang?: string
    speed?: number
    volume?: number
    pitch?: number
    style?: string
    matchDuration?: string
    keepTimeline?: boolean
    autoSplit?: boolean
    gapMs?: number
    title?: string
    outputDir?: string
    outputFormat?: 'wav48' | 'wav16' | 'mp3'
    publishOutput?: boolean
  }) =>
    fetchJson<{
      id: string
      duration: number
      audioUrl: string
      mp3Url?: string
      srtUrl?: string
      zipUrl?: string
      meta: Record<string, unknown>
      cached?: boolean
      publishedDir?: string
    }>(
      `${base}/tts/studio/synthesize`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
      10 * 60_000,
    ),

  ttsStudioHistory: () =>
    fetchJson<Array<Record<string, unknown>>>(
      `${base}/tts/studio/history`,
      undefined,
      12_000,
    ),

  ttsStudioJobProgress: (jobId: string) =>
    fetchJson<{
      current: number
      total: number
      pct: number
      message: string
      running: boolean
      done: boolean
      error?: string
      resultJobId?: string
    }>(`${base}/tts/studio/jobs/${encodeURIComponent(jobId)}/progress`, undefined, 3000),

  ttsStudioDelete: (jobId: string) =>
    fetchJson<{ ok: boolean }>(`${base}/tts/studio/jobs/${jobId}`, {
      method: 'DELETE',
    }),

  ttsStudioCancel: (jobId: string) =>
    fetchJson<{ ok: boolean; cancelled?: boolean }>(
      `${base}/tts/studio/jobs/${jobId}/cancel`,
      { method: 'POST' },
      5000,
    ),

  ttsStudioClone: async (name: string, file: File, transcript = '', tags: string[] = []) => {
    const fd = new FormData()
    fd.append('file', file)
    const q = new URLSearchParams({ name, transcript, tags: JSON.stringify(tags) })
    return fetchJson<{ id: string; name: string; tags: string[] }>(
      `${base}/tts/studio/clone?${q.toString()}`,
      { method: 'POST', body: fd },
      180_000,
    )
  },

  ttsStudioCloneRename: (voiceId: string, name: string) => {
    const id = voiceId.replace(/^vn:clone:/, '')
    return fetchJson<{ id: string; name: string }>(
      `${base}/tts/studio/clone/${encodeURIComponent(id)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      },
    )
  },

  ttsStudioCloneDelete: (voiceId: string) => {
    const id = voiceId.replace(/^vn:clone:/, '')
    return fetchJson<{ ok: boolean }>(
      `${base}/tts/studio/clone/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    )
  },

  /** Đổi metadata / chuyển engine (zmAI ↔ clone) */
  ttsStudioVoicePatch: (
    voiceId: string,
    body: {
      name?: string
      tags?: string[]
      language?: string
      favorite?: boolean
      engine?: 'zmai' | 'clone'
    },
  ) =>
    fetchJson<{
      id: string
      name: string
      tags: string[]
      language?: string
      favorite?: boolean
      engine?: string
      type?: string
    }>(
      `${base}/tts/studio/voices/${encodeURIComponent(voiceId)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    ),

  ttsStudioVoiceReplaceAudio: (voiceId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetchJson<{ ok: boolean; id: string; name: string }>(
      `${base}/tts/studio/voices/${encodeURIComponent(voiceId)}/audio`,
      { method: 'PUT', body: fd },
      180_000,
    )
  },

  ttsStudioVoicesBulkMove: (voiceIds: string[], target: 'zmai' | 'clone') =>
    fetchJson<{
      target: 'zmai' | 'clone'
      successes: Array<{
        voiceId: string
        voice: { id: string; name: string; engine?: string; type?: string }
      }>
      failures: Array<{ voiceId: string; error: string; errorType: string }>
    }>(
      `${base}/tts/studio/voices/bulk-move`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voiceIds, target }),
      },
    ),

  ttsStudioVoiceDelete: (voiceId: string) =>
    fetchJson<{ ok: boolean }>(
      `${base}/tts/studio/voices/${encodeURIComponent(voiceId)}`,
      { method: 'DELETE' },
    ),

  upload: async (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetchJson<{
      projectId: string
      videoUrl: string
      duration: number
      cached?: boolean
      segments?: Segment[]
      settings?: Partial<ProjectSettings>
    }>(`${base}/upload`, { method: 'POST', body: fd }, 120_000)
  },

  mediaAssets: (projectId: string) =>
    fetchJson<{ items: ProjectMediaAsset[] }>(`${base}/projects/${projectId}/assets`, undefined, 30_000),

  uploadMediaAsset: async (projectId: string, file: File) => {
    const body = new FormData()
    body.append('file', file)
    return fetchJson<{ item: ProjectMediaAsset }>(`${base}/projects/${projectId}/assets`, { method: 'POST', body }, 120_000)
  },

  deleteMediaAsset: (projectId: string, assetId: string) =>
    fetchJson<{ ok: boolean }>(`${base}/projects/${projectId}/assets/${encodeURIComponent(assetId)}`, { method: 'DELETE' }, 30_000),

  applyMediaSrt: (projectId: string, assetId: string) =>
    fetchJson<{ segments: Segment[]; settings: ProjectSettings }>(`${base}/projects/${projectId}/assets/${encodeURIComponent(assetId)}/apply-srt`, { method: 'POST' }, 30_000),

  saveSettings: (projectId: string, settings: ProjectSettings) =>
    fetchJson<{ ok: boolean }>(`${base}/projects/${projectId}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    }),

  subtitles: (projectId: string) =>
    fetchJson<{ items: { name: string; label: string }[]; active: string }>(
      `${base}/projects/${projectId}/subtitles`, undefined, 10_000),

  uploadSubtitle: (projectId: string, file: File) => {
    const body = new FormData()
    body.append('file', file)
    return fetchJson<{ name: string; items: { name: string; label: string }[] }>(
      `${base}/projects/${projectId}/subtitles`, { method: 'POST', body }, 30_000)
  },

  applySubtitle: (projectId: string, name: string) =>
    fetchJson<{ segments: Segment[]; settings: ProjectSettings }>(
      `${base}/projects/${projectId}/subtitles/${encodeURIComponent(name)}/apply`,
      { method: 'POST' },
      30_000,
    ),

  status: (projectId: string) =>
    fetchJson<JobStatus>(`${base}/projects/${projectId}/status`, undefined, 6000),

  segments: (projectId: string) =>
    fetchJson<Segment[]>(`${base}/projects/${projectId}/segments`, undefined, 10_000),

  updateSegment: (projectId: string, seg: Segment) =>
    fetchJson<Segment>(`${base}/projects/${projectId}/segments/${seg.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(seg),
    }),

  replaceSegments: (projectId: string, segments: Segment[]) =>
    fetchJson<Segment[]>(`${base}/projects/${projectId}/segments`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(segments),
    }),

  retranscribeRange: (projectId: string, start: number, end: number, sourceLang = 'auto') =>
    fetch(`${base}/projects/${projectId}/segments/retranscribe-range`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start, end, sourceLang, engine: 'whisper' }),
      signal: AbortSignal.timeout(10 * 60_000),
    }).then((r) => {
      if (!r.ok && r.status !== 202) throw new Error(`HTTP ${r.status}`)
      return null  // 202 — caller polls /status
    }),

  /** CapCut Alt+G — compound clip (giữ children + mix TTS). */
  createCompound: (projectId: string, segmentIds: string[]) =>
    fetchJson<{
      ok: boolean
      mode: string
      compoundId: string
      mergedId: string
      start: number
      end: number
      childCount: number
      audioFile?: string
      audioUrl?: string
      audioDuration?: number
      segments: Segment[]
    }>(`${base}/projects/${projectId}/segments/compound`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segmentIds }),
    }, 120_000),

  /** Tháo compound (restore children). */
  uncompound: (projectId: string, segId: string) =>
    fetchJson<{ ok: boolean; segments: Segment[]; restored: number }>(
      `${base}/projects/${projectId}/segments/${encodeURIComponent(segId)}/uncompound`,
      { method: 'POST' },
      60_000,
    ),

  overlays: (projectId: string) =>
    fetchJson<TextOverlay[]>(`${base}/projects/${projectId}/overlays`, undefined, 10_000),

  createOverlay: (projectId: string, overlay: TextOverlay) =>
    fetchJson<TextOverlay>(`${base}/projects/${projectId}/overlays`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(overlay),
    }),

  replaceOverlays: (projectId: string, overlays: TextOverlay[]) =>
    fetchJson<TextOverlay[]>(`${base}/projects/${projectId}/overlays`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(overlays),
    }),

  updateOverlay: (projectId: string, overlay: TextOverlay) =>
    fetchJson<TextOverlay>(`${base}/projects/${projectId}/overlays/${overlay.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(overlay),
    }),

  deleteOverlay: (projectId: string, overlayId: string) =>
    fetchJson<{ ok: boolean }>(`${base}/projects/${projectId}/overlays/${overlayId}`, {
      method: 'DELETE',
    }),

  uploadLogoAsset: async (projectId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetchJson<{ url: string; width: number; height: number }>(
      `${base}/projects/${projectId}/logo-asset`,
      { method: 'POST', body: fd },
      30_000,
    )
  },

  run: (projectId: string, settings: ProjectSettings) =>
    fetchJson<{ ok: boolean; jobId?: string }>(`${base}/projects/${projectId}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    }),

  dub: (projectId: string, settings: ProjectSettings & { forceTts?: boolean }) =>
    fetchJson<{ ok: boolean }>(`${base}/projects/${projectId}/dub`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    }),

  cancel: (projectId: string) =>
    fetchJson<{ ok: boolean; ignored?: boolean }>(
      `${base}/projects/${projectId}/cancel`,
      { method: 'POST' },
      5000,
    ),

  /** Chỉ khi user bấm «Xóa cache» + xác nhận — không gọi tự động. parts rỗng = all. */
  clearProjectCache: (projectId: string, parts?: string[]) =>
    fetchJson<{
      ok: boolean
      partial?: boolean
      deleted?: string[]
      errors?: string[]
      message?: string
      parts?: string[]
      clearedSegments?: boolean
      clearedCovers?: boolean
      clearedTts?: boolean
      clearedFrontend?: boolean
    }>(
      `${base}/cache/project/${projectId}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parts: parts?.length ? parts : null }),
      },
      120_000,
    ),

  /** Đóng popup lỗi — clear meta.status.error (F5 không hiện lại). */
  dismissStatus: (projectId: string) =>
    fetchJson<{ ok: boolean; ignored?: boolean }>(
      `${base}/projects/${projectId}/status/dismiss`,
      { method: 'POST' },
      5000,
    ),

  export: (projectId: string, settings: ProjectSettings, segments: Segment[] | undefined, exportEndSec: number | undefined, exportStartSec: number | undefined, renderName: string, coverDataUrl?: string) =>
    fetchJson<{ ok: boolean; url: string; path?: string; exports?: string }>(
      `${base}/projects/${projectId}/export`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...settings, renderName, ...(segments ? { segments } : {}), ...(exportEndSec && exportEndSec > 0 ? { exportEndSec } : {}), ...(exportStartSec && exportStartSec > 0 ? { exportStartSec } : {}), ...(coverDataUrl ? { coverDataUrl } : {}) }),
      },
    ),

  revealOutput: (projectId: string) =>
    fetchJson<{ ok: boolean; path: string }>(
      `${base}/projects/${projectId}/reveal-output`,
      { method: 'POST' },
    ),

  /** Cache hit nhanh — không hiện 1% nếu đã có stem. */
  noVocalsStatus: (projectId: string) =>
    fetchJson<{
      ready: boolean
      cached: boolean
      running: boolean
      progress: number
      message: string
      audioUrl: string | null
      file: string | null
    }>(`${base}/projects/${projectId}/audio/no-vocals/status`, undefined, 4000),

  /** Demucs xóa lời — lần đầu (cài torch) có thể rất lâu; tái dùng cache. */
  prepareNoVocals: (projectId: string) =>
    fetchJson<{ audioUrl: string; file: string; cached?: boolean }>(
      `${base}/projects/${projectId}/audio/no-vocals`,
      { method: 'POST' },
      900_000,
    ),

  /** Tiến độ tách stem (poll khi đang prepareNoVocals). */
  noVocalsProgress: (projectId: string) =>
    fetchJson<{
      progress: number
      message: string
      running: boolean
      ready?: boolean
      audioUrl?: string | null
      file?: string | null
    }>(`${base}/projects/${projectId}/audio/no-vocals/progress`, undefined, 4000),

  /** URL tải WAV: original | no_vocals | vocals (theo chế độ đã xử lý). */
  projectAudioDownloadUrl: (
    projectId: string,
    kind: 'original' | 'no_vocals' | 'vocals' = 'original',
  ) => `${base}/projects/${projectId}/audio/download?kind=${encodeURIComponent(kind)}&t=${Date.now()}`,

  /** Bake tốc độ preview toàn bộ + remap timeline. */
  rebakeSpeed: (
    projectId: string,
    speed: number,
    opts?: { skipRemap?: boolean; speedRevision?: number; signal?: AbortSignal },
  ) =>
    fetchJson<{
      ok?: boolean
      ignored?: boolean
      reason?: string
      speedRevision?: number
      bakedSpeed: number
      bakedPreferVideo: boolean
      hasBakedSpeed?: boolean
      workClipSec: number
      duration: number
      /** t_new = t_old * timeScale — scale media clips Video/Âm gốc */
      timeScale?: number
      prevBakedSpeed?: number
      segments: Segment[]
      overlays?: TextOverlay[]
      videoUrl: string
    }>(
      `${base}/projects/${projectId}/rebake-speed`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          speed,
          skipRemap: Boolean(opts?.skipRemap),
          speedRevision: opts?.speedRevision ?? null,
        }),
        signal: opts?.signal,
      },
      600_000,
    ),

  previewTts: (
    projectId: string,
    segId: string,
    body: { text: string; voice: string; lang: string; speed?: number },
  ) =>
    fetchJson<{ audioUrl: string; duration: number }>(
      `${base}/projects/${projectId}/segments/${segId}/preview-tts`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
      60_000,
    ),

  retranslate: (
    projectId: string,
    segId: string,
    body?: {
      text?: string
      sourceLang?: string
      targetLang?: string
      translator?: string
    },
  ) =>
    fetchJson<{ translation: string; segment: Segment }>(
      `${base}/projects/${projectId}/segments/${segId}/retranslate`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      },
      60_000,
    ),
}
