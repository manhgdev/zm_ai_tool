import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { localize, useLocale } from '@/app/i18n'
import './FlowSeriesPanel.css'
import {
  type SeriesArtifact, type FlowSeriesSceneContext, type SeriesGenSettings,
  type MergeSettings,
  type FlowSeriesAccount as FlowAccount, type SeriesRun, type AutoMode,
  type Scene, type Episode, type Asset, type Series,
  VIDEO_MODELS, IMAGE_MODELS,
  SERIES_SETTINGS_KEY, SERIES_SELECTED_ID_KEY, SERIES_TAB_KEY,
  SERIES_AUTO_MODE_KEY, SERIES_AUTO_APPROVE_KEY, SERIES_COLLAPSED_EPISODES_KEY, SERIES_AI_KEY,
  SERIES_MERGE_SETTINGS_KEY, DEFAULT_MERGE_SETTINGS,
  normalizeSeries, seriesRequest as request, sceneStatusMeta,
  readSeriesSettings, toUrl, countSeriesScript,
} from '@/features/flow/flowSeries.helpers'
import { chatProviderUsable, normalizeChatProviders, type ChatProviderOption } from '@/features/chat/chatProviders'
import { useRealtimeEvents } from '@/realtime/RealtimeProvider'

export type { SeriesArtifact, FlowSeriesSceneContext }


export default function FlowSeriesPanel({ onOpenScene, onGenerateAnchor, onOpenSrtImage, onOpenQueue, accounts = [] }: {
  onOpenScene: (context: FlowSeriesSceneContext) => void
  onGenerateAnchor: (seriesId: string, prompt: string) => Promise<string>
  onOpenSrtImage?: (mediaFolder: string) => void
  onOpenQueue?: () => void
  accounts?: FlowAccount[]
}) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [items, setItems] = useState<Series[]>([])
  const onSeriesRealtime = useCallback((event: { type: string; payload: unknown; entityId: string }) => {
    if (event.type === 'snapshot') {
      const snapshot = event.payload as { items?: Series[] } | null
      if (Array.isArray(snapshot?.items)) setItems(snapshot.items.map(normalizeSeries))
      return
    }
    if (event.type === 'series.deleted') {
      setItems(current => current.filter(item => item.id !== event.entityId))
      return
    }
    const series = event.payload as Series | null
    if (series?.id) setItems(current => [...current.filter(item => item.id !== series.id), normalizeSeries(series)])
  }, [])
  const realtimeStatus = useRealtimeEvents('series', onSeriesRealtime)
  const [selectedId, setSelectedId] = useState(() => {
    try { return localStorage.getItem(SERIES_SELECTED_ID_KEY) || '' } catch { return '' }
  })
  const [selected, setSelected] = useState<Series | null>(null)
  const [title, setTitle] = useState('')
  const [creating, setCreating] = useState(false)
  const [topic, setTopic] = useState('')
  const [numEpisodes, setNumEpisodes] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}').numEpisodes || '' } catch { return '' }
  })
  const [episodeDuration, setEpisodeDuration] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}').episodeDuration || '' } catch { return '' }
  })
  const [sceneDuration, setSceneDuration] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}').sceneDuration || '' } catch { return '' }
  })
  const [sceneContinuity, setSceneContinuity] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}').sceneContinuity === true } catch { return false }
  })
  const [extendDraft, setExtendDraft] = useState<{ text: string } | null>(null)
  const [extendDrafting, setExtendDrafting] = useState(false)
  const [extendNumEpisodes, setExtendNumEpisodes] = useState('1')
  const [extendOpen, setExtendOpen] = useState(false)
  const [draft, setDraft] = useState<{ text: string; bible: string } | null>(null)
  const [drafting, setDrafting] = useState(false)
  const [aiProviders, setAiProviders] = useState<ChatProviderOption[]>([])
  const [aiLoading, setAiLoading] = useState(true)
  const [aiConfig, setAiConfig] = useState<{ provider: string; model: string }>(() => {
    try {
      const s = JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}')
      return { provider: '', model: '', ...s }
    } catch { return { provider: '', model: '' } }
  })
  const [sceneDraft, setSceneDraft] = useState({ episodeId: '', title: '', prompt: '', timecode: '' })
  const [episodeTitle, setEpisodeTitle] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [collapsedEpisodes, setCollapsedEpisodes] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem(SERIES_COLLAPSED_EPISODES_KEY) || '[]')) } catch { return new Set() }
  })
  // Unified preview modal (reuses flow-preview-* CSS from FlowPage)
  const [seriesPreview, setSeriesPreview] = useState<{ url: string; title: string; kind: 'video'|'image'; playlist?: {url:string;title:string}[]; idx?: number } | null>(null)
  const [anchorPrompt, setAnchorPrompt] = useState('')
  const [anchorJobId, setAnchorJobId] = useState('')
  const assetInput = useRef<HTMLInputElement>(null)
  const [activeTab, setActiveTab] = useState<'episodes' | 'assets' | 'settings'>(() => {
    try {
      const saved = localStorage.getItem(SERIES_TAB_KEY)
      return saved === 'assets' || saved === 'bible' ? 'assets' : saved === 'settings' ? 'settings' : 'episodes'
    } catch {
      return 'episodes'
    }
  })
  const [generatingScene, setGeneratingScene] = useState<string>('')  // sceneId being generated
  const [seriesSettings, setSeriesSettings] = useState<SeriesGenSettings>(() => {
    const saved = readSeriesSettings()
    return {
      accountId: saved.accountId || accounts[0]?.id || '',
      model: saved.model || 'Omni 1.1 Flash',
      ratio: saved.ratio || '16:9',
      duration: saved.duration || (/omni.*flash/i.test(saved.model || 'Omni 1.1 Flash') ? 'auto' : '8'),
      resolution: /^\d{3,4}p$/i.test(String(saved.resolution || '')) ? saved.resolution : '360p',
      quality: (() => {
        const q = saved.quality || saved.resolution || '360p'
        const r = saved.resolution || '360p'
        const qn = parseInt(q) || 360
        const rn = parseInt(r) || 360
        return qn < rn ? r : q
      })(),
      concurrency: saved.concurrency || '3',
    }
  })
  const [mergeSettings, setMergeSettings] = useState<MergeSettings>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(SERIES_MERGE_SETTINGS_KEY) || '{}')
      return { ...DEFAULT_MERGE_SETTINGS, ...saved }
    } catch { return { ...DEFAULT_MERGE_SETTINGS } }
  })
  const saveMergeSettings = (patch: Partial<MergeSettings>) => {
    setMergeSettings((prev) => {
      const next = { ...prev, ...patch }
      try { localStorage.setItem(SERIES_MERGE_SETTINGS_KEY, JSON.stringify(next)) } catch {}
      return next
    })
  }

  // ── Automation run state ──
  const [activeRun, setActiveRun] = useState<SeriesRun | null>(null)
  const [autoMode, setAutoMode] = useState<AutoMode>(() => {
    try {
      const saved = localStorage.getItem(SERIES_AUTO_MODE_KEY)
      return saved === 'full' || saved === 'keyframes_only' || saved === 'videos_only' ? (saved as AutoMode) : 'full'
    } catch {
      return 'full'
    }
  })
  const [autoApprove] = useState(() => {
    try {
      const saved = localStorage.getItem(SERIES_AUTO_APPROVE_KEY)
      return saved !== null ? saved === '1' : true
    } catch {
      return true
    }
  })
  const [imageModel, setImageModel] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SERIES_SETTINGS_KEY) || '{}').imageModel || 'Nano Banana 2' } catch { return 'Nano Banana 2' }
  })
  const selectedAccount = (seriesSettings.accountId === 'random' ? accounts.find((a) => a.status === 'online' && (a.plan === 'Ultra' || a.plan === 'Pro' || a.plan === 'Plus')) : undefined) || accounts.find((account) => account.id === seriesSettings.accountId) || accounts[0]
  const videoSection = selectedAccount?.capabilityStatus === 'verified' ? selectedAccount.capabilityCatalog?.video : undefined
  const imageSection = selectedAccount?.capabilityStatus === 'verified' ? selectedAccount.capabilityCatalog?.image : undefined
  const videoModelOptions = videoSection?.models.map((item) => item.name) || [...VIDEO_MODELS]
  const imageModelOptions = imageSection?.models.map((item) => item.name) || [...IMAGE_MODELS]
  const selectedVideoCapability = videoSection?.models.find((item) => item.name === seriesSettings.model)
  const seriesRatioOptions = selectedVideoCapability?.ratios.length ? selectedVideoCapability.ratios : ['16:9', '9:16']
  const isOmniFlash = /omni.*flash/i.test(seriesSettings.model)
  const seriesDurationOptions = selectedVideoCapability?.durations.length
    ? selectedVideoCapability.durations
    : ['4', '6', '8', '10']

  const seriesResolutionOptions = (() => {
    const options = (selectedVideoCapability?.resolutions || []).filter((value) => /^\d{3,4}p$/i.test(value))
    return options.length ? options : ['360p', '720p', '1080p']
  })()

  useEffect(() => {
    if (!videoSection?.models.length) return
    setSeriesSettings((current) => {
      const selectedModel = videoSection.models.find((item) => item.name === current.model)
        || videoSection.models.find((item) => item.name === videoSection.defaultModel)
        || videoSection.models[0]
      const ratio = selectedModel.ratios.includes(current.ratio) ? current.ratio : selectedModel.defaultRatio || selectedModel.ratios[0] || current.ratio
      const _isFlash = /omni.*flash/i.test(selectedModel.name)
      const _dCh = selectedModel.durations.length ? selectedModel.durations : ['4', '6', '8', '10']
      const duration = current.duration === 'auto'
        ? (_isFlash ? 'auto' : '8')
        : (_dCh.includes(current.duration) ? current.duration : (_isFlash ? 'auto' : selectedModel.defaultDuration || '8'))
      const resolution = selectedModel.resolutions.filter((value) => /^\d{3,4}p$/i.test(value))
      // Prefer 360p for Omni Flash when offered (fast continuous Series drafts).
      const preferFast = /omni.*flash/i.test(selectedModel.name) && resolution.includes('360p')
      const nextResolution = resolution.length
        ? (resolution.includes(current.resolution)
          ? current.resolution
          : preferFast
            ? '360p'
            : selectedModel.defaultResolution && resolution.includes(selectedModel.defaultResolution)
              ? selectedModel.defaultResolution
              : resolution[0])
        : (/^[1-9]\d{0,1}k$/i.test(current.resolution) ? '' : current.resolution)
      const nextQuality = current.quality || '720p'
      if (
        selectedModel.name === current.model
        && ratio === current.ratio
        && duration === current.duration
        && nextResolution === current.resolution
        && nextQuality === current.quality
      ) return current
      return { ...current, model: selectedModel.name, ratio, duration, resolution: nextResolution, quality: nextQuality }
    })
    if (imageSection?.models.length && !imageSection.models.some((item) => item.name === imageModel)) {
      setImageModel(imageSection.defaultModel || imageSection.models[0].name)
    }
  }, [videoSection, imageSection, imageModel, seriesSettings.model])

  useEffect(() => {
    try {
      if (selectedId) localStorage.setItem(SERIES_SELECTED_ID_KEY, selectedId)
    } catch {}
  }, [selectedId])

  useEffect(() => {
    try { localStorage.setItem(SERIES_TAB_KEY, activeTab) } catch {}
  }, [activeTab])

  useEffect(() => {
    try {
      const prev = JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}')
      localStorage.setItem(SERIES_AI_KEY, JSON.stringify({ ...prev, ...aiConfig }))
    } catch {}
  }, [aiConfig])

  useEffect(() => {
    try {
      const prev = JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}')
      localStorage.setItem(SERIES_AI_KEY, JSON.stringify({ ...prev, numEpisodes, episodeDuration, sceneDuration, sceneContinuity }))
    } catch {}
  }, [numEpisodes, episodeDuration, sceneDuration, sceneContinuity])

  useEffect(() => {
    let active = true
    void fetch('/api/chat/providers').then((response) => response.ok ? response.json() : null).then((raw) => {
      if (!active) return
      const available = normalizeChatProviders(raw)
      setAiProviders(available)
      setAiConfig((current) => {
        const provider = available.find((item) => item.id === current.provider && chatProviderUsable(item)) || available.find(chatProviderUsable)
        if (!provider) return current
        const model = provider.models.some((item) => item.id === current.model) ? current.model : provider.models[0]?.id || ''
        return { provider: provider.id, model }
      })
    }).catch(() => undefined).finally(() => { if (active) setAiLoading(false) })
    return () => { active = false }
  }, [])
  const aiProvider = aiProviders.find((item) => item.id === aiConfig.provider)

  useEffect(() => {
    try { localStorage.setItem(SERIES_AUTO_MODE_KEY, autoMode) } catch {}
  }, [autoMode])

  useEffect(() => {
    try { localStorage.setItem(SERIES_AUTO_APPROVE_KEY, autoApprove ? '1' : '0') } catch {}
  }, [autoApprove])

  useEffect(() => {
    try { localStorage.setItem(SERIES_COLLAPSED_EPISODES_KEY, JSON.stringify(Array.from(collapsedEpisodes))) } catch {}
  }, [collapsedEpisodes])

  useEffect(() => {
    try {
      localStorage.setItem(SERIES_SETTINGS_KEY, JSON.stringify({ ...seriesSettings, imageModel }))
    } catch {}
  }, [seriesSettings, imageModel])

  useEffect(() => {
    if (!accounts.length) return
    const saved = readSeriesSettings()
    setSeriesSettings((prev) => {
      if (prev.accountId === 'random') return prev
      if (saved.accountId === 'random') return { ...prev, accountId: 'random' }
      if (prev.accountId && accounts.some((a) => a.id === prev.accountId)) return prev
      if (saved.accountId && accounts.some((a) => a.id === saved.accountId)) {
        return { ...prev, accountId: saved.accountId }
      }
      return { ...prev, accountId: accounts[0]?.id || '' }
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts.map((a) => a.id).join(',')])

  const saveSeriesSettings = (patch: Partial<SeriesGenSettings>) => {
    setSeriesSettings((prev) => {
      const next = { ...prev, ...patch }
      try {
        // Read imageModel fresh from localStorage to avoid stale closure
        const stored = JSON.parse(localStorage.getItem(SERIES_SETTINGS_KEY) || '{}')
        localStorage.setItem(SERIES_SETTINGS_KEY, JSON.stringify({ ...next, imageModel: stored.imageModel || imageModel }))
      } catch {}
      return next
    })
  }

  const startRun = async (episodeId?: string) => {
    if (!selected) return
    const accountId = seriesSettings.accountId || accounts[0]?.id || ''
    if (!accountId) { toast.error(t('Cần chọn tài khoản Flow.', 'A Flow account is required.')); return }
    try {
      const raw = await request<{ runId: string; status: string; total?: number; enqueued?: number }>(`/series/${selected.id}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          accountId,
          episodeId: episodeId || '',
          settings: {
            model: videoModelOptions.includes(seriesSettings.model) ? seriesSettings.model : videoModelOptions[0],
            ratio: seriesSettings.ratio,
            ...(seriesSettings.duration && seriesSettings.duration !== 'auto' ? { duration: seriesSettings.duration } : {}),
            resolution: seriesSettings.resolution || '360p',
            quality: seriesSettings.quality || seriesSettings.resolution || '360p',
            concurrency: seriesSettings.concurrency || '1',
          },
          imageModel,
          autoApprove: true,
          mode: autoMode,
          headless,
        }),
      })
      setActiveRun({ runId: raw.runId, status: raw.status, total: raw.total || 0, done: 0, currentSceneId: '', currentStep: '', errors: [] })
      toast.success(t(`Đã đẩy ${raw.enqueued || raw.total || ''} cảnh vào Hàng đợi Flow.`, `Enqueued ${raw.enqueued || raw.total || ''} scenes into Flow Queue.`))
      void refresh(selected.id)
      onOpenQueue?.()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const stopRun = async () => {
    if (!selected || !activeRun) return
    try {
      await request(`/series/${selected.id}/run/${activeRun.runId}/stop`, { method: 'POST' })
      toast.success(t('Đã dừng sau cảnh hiện tại.', 'Will stop after the current scene.'))
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const generateScene = async (episode: { id: string }, scene: Scene, artifact: SeriesArtifact) => {
    if (!selected) return
    const accountId = seriesSettings.accountId || accounts[0]?.id || ''
    if (!accountId) { toast.error(t('Cần chọn tài khoản Flow để tạo.', 'A Flow account is required.')); return }
    setGeneratingScene(scene.id)
    try {
      // Auto-approve leftover stills so video never waits on a manual click.
      if (artifact === 'video' && !scene.approvedKeyframe) {
        if (scene.keyframeOutput || scene.keyframeJobId) {
          await request(
            `/series/${selected.id}/episodes/${episode.id}/scenes/${scene.id}/approve-keyframe?job_id=${encodeURIComponent(scene.keyframeJobId || '')}&output_index=0`,
            { method: 'POST' },
          )
          await refresh(selected.id)
        } else {
          toast.error(t('Cần tạo keyframe trước khi tạo video.', 'Create a keyframe before generating video.'))
          return
        }
      }
      const isKeyframe = artifact === 'keyframe'
      await request(`/series/${selected.id}/episodes/${episode.id}/scenes/${scene.id}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          artifact,
          accountId,
          settings: {
            model: isKeyframe ? (imageModelOptions.includes(imageModel) ? imageModel : imageModelOptions[0]) : (videoModelOptions.includes(seriesSettings.model) ? seriesSettings.model : videoModelOptions[0]),
            ratio: seriesSettings.ratio,
            ...(seriesSettings.duration && seriesSettings.duration !== 'auto' ? { duration: seriesSettings.duration } : {}),
            resolution: seriesSettings.resolution || '360p',
            quality: seriesSettings.quality || seriesSettings.resolution || '360p',
            count: 1,
          },
          headless,
        }),
      })
      toast.success(isKeyframe ? t('Đã gửi job tạo keyframe.', 'Keyframe job queued.') : t('Đã gửi job tạo video.', 'Video job queued.'))
      await refresh(selected.id)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setGeneratingScene('')
    }
  }

  const sceneFailedArtifact = (scene: Scene): SeriesArtifact => {
    if (scene.videoJobId && (scene.approvedKeyframe || scene.keyframeOutput)) return 'video'
    return 'keyframe'
  }

  const sceneFailedJobId = (scene: Scene): string => {
    const artifact = sceneFailedArtifact(scene)
    return artifact === 'video' ? String(scene.videoJobId || '') : String(scene.keyframeJobId || '')
  }

  /** Mirror Flow queue: Retry reuses the failed job; Create new enqueues a fresh one. */
  const retryScene = async (episode: Episode, scene: Scene, mode: 'retry' | 'create') => {
    if (!selected) return
    const accountId = seriesSettings.accountId || accounts[0]?.id || ''
    if (!accountId) { toast.error(t('Cần chọn tài khoản Flow.', 'A Flow account is required.')); return }
    const artifact = sceneFailedArtifact(scene)
    const jobId = sceneFailedJobId(scene)
    setGeneratingScene(scene.id)
    try {
      if (mode === 'retry' && jobId) {
        const isKeyframe = artifact === 'keyframe'
        await request(`/jobs/${jobId}/retry`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            accountId,
            settings: {
              model: isKeyframe
                ? (imageModelOptions.includes(imageModel) ? imageModel : imageModelOptions[0])
                : (videoModelOptions.includes(seriesSettings.model) ? seriesSettings.model : videoModelOptions[0]),
              ratio: seriesSettings.ratio,
              ...(seriesSettings.duration && seriesSettings.duration !== 'auto' ? { duration: seriesSettings.duration } : {}),
              resolution: seriesSettings.resolution || '360p',
              quality: seriesSettings.quality || seriesSettings.resolution || '360p',
              count: 1,
            },
            headless,
          }),
        })
        toast.success(t('Đã đưa cảnh vào hàng đợi chạy lại.', 'Scene queued for retry.'))
      } else {
        await generateScene(episode, scene, artifact)
        return
      }
      await refresh(selected.id)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setGeneratingScene('')
    }
  }

  const refresh = async (selectId = selectedId) => {
    setLoading(true)
    try {
      const data = await request<{ items: Series[] }>('/series')
      const nextItems = data.items.map(normalizeSeries)
      setItems(nextItems)
      const targetId = selectId || selectedId || (typeof localStorage !== 'undefined' ? localStorage.getItem(SERIES_SELECTED_ID_KEY) || '' : '')
      const matched = nextItems.find((item) => item.id === targetId) || nextItems[0]
      const id = matched?.id || ''
      setSelectedId(id)
      if (id) {
        try { localStorage.setItem(SERIES_SELECTED_ID_KEY, id) } catch {}
        setSelected(normalizeSeries(await request<Series>(`/series/${id}`)))
      } else {
        setSelected(null)
      }
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { void refresh().catch((error) => toast.error(String(error.message || error))) }, [])

  // Poll active run status every 3s
  useEffect(() => {
    if (!activeRun || !selected) return
    if (['done', 'done_with_errors', 'failed', 'cancelled'].includes(activeRun.status)) {
      // Skip ghost runs auto-cleared after server restart (status=cancelled, total=0)
      const isGhost = activeRun.status === 'cancelled' && activeRun.total === 0 && activeRun.done === 0
      if (!isGhost) {
        void refresh(selected.id)
        if (activeRun.status === 'done') toast.success(t('Hoàn thành tự động hoá!', 'Automation complete!'))
        else if (activeRun.status === 'done_with_errors') toast.warning(t(`Hoàn thành có ${activeRun.errors.length} lỗi.`, `Done with ${activeRun.errors.length} error(s).`))
      }

      // Tự động xoá thanh trạng thái khi đã xong/huỷ
      setActiveRun(null)
      return
    }
    const timer = window.setInterval(() => {
      void request<SeriesRun>(`/series/${selected.id}/run/${activeRun.runId}`)
        .then(setActiveRun)
        .catch((err) => {
          if ((err as any).status === 404) {
            setActiveRun(null)
          }
        })
    }, 3000)
    return () => window.clearInterval(timer)
  }, [activeRun?.runId, activeRun?.status, selected?.id])

  // Refresh scene data every 6s while a run is active so keyframe/video status
  // updates live without needing F5 — ponytail: lightweight GET, stops when run ends
  useEffect(() => {
    if (!activeRun || !selected || ['done', 'done_with_errors', 'failed', 'cancelled'].includes(activeRun.status) || realtimeStatus === 'connected') return
    const t2 = window.setInterval(() => {
      void request<Series>(`/series/${selected.id}`)
        .then((fresh) => setSelected(normalizeSeries(fresh)))
        .catch(() => {/* ignore transient errors */})
    }, 6000)
    return () => window.clearInterval(t2)
  }, [activeRun?.runId, activeRun?.status, realtimeStatus, selected?.id])

  const create = async () => {
    if (!title.trim()) return
    try {
      const created = await request<Series>('/series', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) })
      setTitle(''); setCreating(false)
      await refresh(created.id)
      toast.success(t('Đã tạo Series.', 'Series created.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const draftWithAi = async () => {
    if (!topic.trim() || !aiConfig.provider) return
    setDrafting(true)
    try {
      const result = await request<{ text: string; bible: string; anchor_prompt?: string }>('/series/draft', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topic,
          provider: aiConfig.provider,
          model: aiConfig.model,
          ...(numEpisodes ? { num_episodes: Number(numEpisodes) } : {}),
          ...(episodeDuration ? { episode_duration: Number(episodeDuration) } : {}),
          ...(sceneDuration ? { scene_duration: Number(sceneDuration) } : {}),
          ...(sceneContinuity ? { scene_continuity: true } : {}),
        }),
      })
      if (result.anchor_prompt?.trim()) setAnchorPrompt(result.anchor_prompt.trim())
      setDraft(result)
    } catch (error) {
      toast.error(t(`AI không viết được series: ${error instanceof Error ? error.message : String(error)}`, `AI could not write the series: ${error instanceof Error ? error.message : String(error)}`))
    } finally { setDrafting(false) }
  }
  const importDraft = async () => {
    if (!draft?.text.trim()) return
    try {
      const result = await request<{ series: Series }>('/series/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft) })
      setDraft(null); setTopic(''); setCreating(false)
      setActiveTab('episodes')
      await refresh(result.series.id)
      toast.success(t('Đã tạo Series.', 'Series created.'))
    } catch (error) {
      const detail = (error as Error & { status?: number }).status === 422
        ? t('Kịch bản chưa đúng định dạng — kiểm tra dòng # SERIES, # TẬP và các cảnh 001_[…].', 'The script format is invalid — check the # SERIES, # TẬP and 001_[…] scene lines.')
        : error instanceof Error ? error.message : String(error)
      toast.error(detail)
    }
  }
  const draftMoreEpisodes = async () => {
    if (!selected || !aiConfig.provider) return
    setExtendDrafting(true)
    try {
      const result = await request<{ text: string }>(`/series/${selected.id}/draft-more`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: aiConfig.provider,
          model: aiConfig.model,
          num_episodes: Number(extendNumEpisodes) || 1,
          ...(episodeDuration ? { episode_duration: Number(episodeDuration) } : {}),
          ...(sceneDuration ? { scene_duration: Number(sceneDuration) } : {}),
          ...(sceneContinuity ? { scene_continuity: true } : {}),
        }),
      })
      setExtendDraft(result)
    } catch (error) {
      toast.error(t(`AI không viết được tập mới: ${error instanceof Error ? error.message : String(error)}`, `AI could not draft new episodes: ${error instanceof Error ? error.message : String(error)}`))
    } finally { setExtendDrafting(false) }
  }
  const appendEpisodes = async () => {
    if (!selected || !extendDraft?.text.trim()) return
    try {
      await request(`/series/${selected.id}/append-episodes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: extendDraft.text, bible: '' }) })
      setExtendDraft(null); setExtendOpen(false)
      await refresh(selected.id)
      toast.success(t('Đã thêm tập mới vào Series.', 'New episodes added to the series.'))
    } catch (error) {
      const detail = (error as Error & { status?: number }).status === 422
        ? t('Kịch bản chưa đúng định dạng — sửa trực tiếp trong ô văn bản.', 'The script format is invalid — edit the text directly.')
        : error instanceof Error ? error.message : String(error)
      toast.error(detail)
    }
  }
  const saveSeries = async () => {
    if (!selected) return
    setSaving(true)
    try {
      await request(`/series/${selected.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: selected.title, bible: selected.bible, description: selected.description, anchorAssets: selected.anchorAssets }) })
      await refresh(selected.id); toast.success(t('Đã lưu Series.', 'Series saved.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
    finally { setSaving(false) }
  }
  const removeSeries = async () => {
    if (!selected || !window.confirm(t(`Xóa Series "${selected.title}" cùng ảnh neo?`, `Delete "${selected.title}" and its anchor images?`))) return
    try { await request(`/series/${selected.id}`, { method: 'DELETE' }); await refresh(''); toast.success(t('Đã xóa Series.', 'Series deleted.')) } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const uploadAsset = async (file?: File) => {
    if (!selected || !file) return
    const data = new FormData(); data.append('file', file)
    try { await request(`/series/${selected.id}/assets`, { method: 'POST', body: data }); await refresh(selected.id); toast.success(t('Đã thêm ảnh neo.', 'Anchor image added.')) } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const generateAnchor = async () => {
    if (!selected || !anchorPrompt.trim()) return
    try {
      const jobId = await onGenerateAnchor(selected.id, anchorPrompt.trim())
      setAnchorJobId(jobId)
      setAnchorPrompt('')
      toast.success(t('Đã gửi job tạo ảnh neo. Ảnh hoàn thành sẽ tự thêm và khóa.', 'Anchor image job queued. The completed image will be added and locked automatically.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  useEffect(() => {
    if (!anchorJobId || !selected) return
    const timer = window.setInterval(() => {
      void request<{ status: string; error?: string }>(`/jobs/${anchorJobId}`).then((job) => {
        if (!['done', 'failed', 'cancelled'].includes(job.status)) return
        setAnchorJobId('')
        void refresh(selected.id)
        if (job.status === 'done') {
          toast.success(t('Ảnh neo đã sẵn sàng.', 'Anchor image is ready.'))
        } else {
          toast.error(job.error || t('Không thể tạo ảnh neo.', 'Could not generate the anchor image.'))
        }
      }).catch(() => undefined)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [anchorJobId, selected?.id])
  const deleteAsset = async (assetId: string) => {
    if (!selected || !window.confirm(t('Xóa ảnh neo này?', 'Delete this anchor image?'))) return
    try { await request(`/series/${selected.id}/assets/${assetId}`, { method: 'DELETE' }); await refresh(selected.id); toast.success(t('Đã xóa ảnh neo.', 'Anchor image deleted.')) } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const toggleAnchor = async (assetId: string) => {
    if (!selected) return
    const next = selected.anchorAssets.includes(assetId) ? selected.anchorAssets.filter((id) => id !== assetId) : [...selected.anchorAssets, assetId].slice(0, 3)
    setSelected({ ...selected, anchorAssets: next })
    try { await request(`/series/${selected.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: selected.title, bible: selected.bible, description: selected.description, anchorAssets: next }) }); await refresh(selected.id) } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const toggleAssetLock = async (asset: Asset) => {
    if (!selected) return
    try {
      await request(`/series/${selected.id}/assets/${asset.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ locked: !asset.locked }) })
      await refresh(selected.id)
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const addEpisode = async () => {
    if (!selected) return
    try {
      await request(`/series/${selected.id}/episodes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: episodeTitle }) })
      setEpisodeTitle('')
      await refresh(selected.id)
      toast.success(t('Đã thêm tập mới.', 'New episode added.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const addScene = async () => {
    if (!selected || !sceneDraft.episodeId || !sceneDraft.prompt.trim()) return
    try {
      await request(`/series/${selected.id}/episodes/${sceneDraft.episodeId}/scenes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sceneDraft) })
      setSceneDraft({ episodeId: sceneDraft.episodeId, title: '', prompt: '', timecode: '' })
      await refresh(selected.id)
      toast.success(t('Đã thêm cảnh mới.', 'New scene added.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const updateScene = async (episode: Episode, scene: Scene, patch: Record<string, unknown>) => {
    if (!selected) return
    try {
      await request(`/series/${selected.id}/episodes/${episode.id}/scenes/${scene.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) })
      await refresh(selected.id)
      toast.success(t('Đã lưu cảnh.', 'Scene saved.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const deleteScene = async (episode: Episode, scene: Scene) => {
    if (!selected || !window.confirm(t('Xóa cảnh này?', 'Delete this scene?'))) return
    try {
      await request(`/series/${selected.id}/episodes/${episode.id}/scenes/${scene.id}`, { method: 'DELETE' })
      await refresh(selected.id)
      toast.success(t('Đã xóa cảnh.', 'Scene deleted.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const deleteEpisode = async (episode: Episode) => {
    if (!selected || !window.confirm(t('Xóa tập này cùng toàn bộ cảnh?', 'Delete this episode and all its scenes?'))) return
    try {
      await request(`/series/${selected.id}/episodes/${episode.id}`, { method: 'DELETE' })
      await refresh(selected.id)
      toast.success(t('Đã xóa tập.', 'Episode deleted.'))
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const [mergingEpisodeId, setMergingEpisodeId] = useState('')
  const [headless, setHeadless] = useState(() => {
    try { const v = localStorage.getItem('zm-series:headless'); return v === null ? true : v === 'true' } catch { return true }
  })
  const toggleHeadless = () => {
    setHeadless((v) => {
      try { localStorage.setItem('zm-series:headless', String(!v)) } catch {}
      return !v
    })
  }
  const mergeEpisode = async (episode: Episode) => {
    if (!selected) return
    const hasVideos = episode.scenes.some((s) => s.videoOutput)
    if (!hasVideos) { toast.error(t('Ch\u01b0a c\u00f3 c\u1ea3nh video n\u00e0o \u0111\u1ec3 gh\u00e9p.', 'No completed video scenes to merge.')); return }
    setMergingEpisodeId(episode.id)
    try {
      const result = await request<{ path: string }>(`/series/${selected.id}/episodes/${episode.id}/merge`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(mergeSettings),
      })
      toast.success(t(`\u0110\u00e3 gh\u00e9p th\u00e0nh: ${result.path}`, `Merged: ${result.path}`))
      // Open SrtImagePage with tap-NN/ scene-video folder, not the merged/ output
      if (onOpenSrtImage) {
        const firstVideo = episode.scenes.find((s) => s.videoOutput)?.videoOutput ?? ''
        if (firstVideo) {
          const sep = firstVideo.includes('/') ? '/' : '\\'
          const tapFolder = firstVideo.substring(0, firstVideo.lastIndexOf(sep))
          if (tapFolder) { onOpenSrtImage(tapFolder); return }
        }
        if (result.path) {
          const sep2 = result.path.includes('/') ? '/' : '\\'
          onOpenSrtImage(result.path.substring(0, result.path.lastIndexOf(sep2)) || result.path)
        }
      }
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
    finally { setMergingEpisodeId('') }
  }
  const openEpisodeFolder = async (episode: Episode) => {
    if (!selected) return
    try {
      await request(`/series/${selected.id}/episodes/${episode.id}/open-folder`, { method: 'POST' })
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
  }
  const [mergingSeries, setMergingSeries] = useState(false)
  const mergeSeries = async () => {
    if (!selected) return
    const hasAnyVideo = selected.episodes.some((ep) => ep.scenes.some((s) => s.videoOutput))
    if (!hasAnyVideo) { toast.error(t('Ch\u01b0a c\u00f3 t\u1eadp n\u00e0o c\u00f3 video \u0111\u1ec3 gh\u00e9p.', 'No episodes have videos to merge.')); return }
    setMergingSeries(true)
    try {
      const result = await request<{ path: string }>(`/series/${selected.id}/merge`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(mergeSettings),
      })
      const fileName = result.path.split(/[\/\\]/).pop() ?? result.path
      toast.success(t(`\u0110\u00e3 gh\u00e9p series: ${fileName}`, `Series merged: ${fileName}`))
      // Reveal merged series MP4 in Finder \u2014 no SrtImagePage navigation needed
      await request(`/series/${selected.id}/merge/open-folder`, { method: 'POST' }).catch(() => null)
    } catch (error) { toast.error(error instanceof Error ? error.message : String(error)) }
    finally { setMergingSeries(false) }
  }
  const toggleEpisode = (episodeId: string) => {
    setCollapsedEpisodes((prev) => {
      const next = new Set(prev)
      if (next.has(episodeId)) next.delete(episodeId)
      else next.add(episodeId)
      return next
    })
  }

  const totalScenes = selected ? selected.episodes.reduce((n, e) => n + e.scenes.length, 0) : 0
  const doneScenes = selected ? selected.episodes.reduce((n, e) => n + e.scenes.filter((s) => s.status === 'complete').length, 0) : 0
  const videoScenes = selected ? selected.episodes.reduce((n, e) => n + e.scenes.filter((s) => s.videoOutput).length, 0) : 0

  return (
    <section className="fsp-panel">
      {/* ── Sidebar ── */}
      <aside className="fsp-sidebar">
        <header className="fsp-sidebar-head">
          <h2>{t('Series', 'Series')}</h2>
          <span className="fsp-count">{items.length}</span>
        </header>
        <button type="button" className={`fsp-btn fsp-btn-primary fsp-new-btn${creating || !selected ? ' is-active' : ''}`} onClick={() => setCreating(true)}>
          ✨ {t('Series mới', 'New Series')}
        </button>
        <nav className="fsp-series-list">
          {items.map((item) => {
            const scenes = item.episodes.reduce((n, e) => n + e.scenes.length, 0)
            return (
              <button key={item.id} type="button" className={`fsp-series-item${!creating && item.id === selectedId ? ' is-active' : ''}`} onClick={() => { setCreating(false); void refresh(item.id) }}>
                <span className="fsp-series-item-title">{item.title}</span>
                <span className="fsp-series-item-meta">{item.episodes.length} {t('tập', 'episodes')} · {scenes} {t('cảnh', 'scenes')}</span>
              </button>
            )
          })}
        </nav>
      </aside>

      {/* ── Workspace ── */}
      <div className="fsp-workspace" aria-busy={loading}>
        {loading ? (
          <div className="fsp-skeleton-wrap" role="status">
            <p className="fsp-skeleton-text">{t('Đang tải Series…', 'Loading Series…')}</p>
            <div className="fsp-skeleton fsp-skeleton-title" />
            <div className="fsp-skeleton fsp-skeleton-meta" />
            <div className="fsp-skeleton fsp-skeleton-body" />
          </div>
        ) : creating || !selected ? (
          /* ── Create: topic → AI draft → review → save ── */
          <div className="fsp-create">
            <header className="fsp-create-head">
              <div>
                <h2>✨ {t('Tạo Series bằng AI', 'Create a Series with AI')}</h2>
                <p>{t('Nhập chủ đề — AI tự chia tập, viết cảnh và Bible nhân vật. Bạn xem lại trước khi lưu.', 'Enter a topic — AI splits episodes, writes scenes and a character Bible. You review it before saving.')}</p>
              </div>
              {selected && (
                <button type="button" className="fsp-btn" onClick={() => { setCreating(false); setDraft(null) }}>
                  {t('Huỷ', 'Cancel')}
                </button>
              )}
            </header>

            <div className="fsp-create-card">
              <label className="fsp-field">
                <span className="fsp-label">{t('Chủ đề', 'Topic')}</span>
                <textarea
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  rows={3}
                  placeholder={t('Ví dụ: Tom và Jerry đi cắm trại, lạc trong rừng và kết bạn với một chú gấu nhỏ', 'Example: Tom and Jerry go camping, get lost in the forest and befriend a little bear')}
                  onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void draftWithAi() }}
                />
              </label>
              <div className="fsp-create-row">
                <label className="fsp-field">
                  <span className="fsp-label">{t('Provider AI text', 'Text AI provider')}</span>
                  <select
                    value={aiConfig.provider}
                    onChange={(e) => {
                      const next = aiProviders.find((item) => item.id === e.target.value)
                      setAiConfig({ provider: e.target.value, model: next?.models[0]?.id || '' })
                    }}
                    disabled={aiLoading && !aiProviders.length}
                    aria-busy={aiLoading}
                  >
                    {!aiProviders.some(chatProviderUsable) && <option value="">{aiLoading ? t('Đang tải provider…', 'Loading providers…') : t('Chưa có provider khả dụng', 'No available provider')}</option>}
                    {aiProviders.map((item) => (
                      <option key={item.id} value={item.id} disabled={!chatProviderUsable(item)}>
                        {item.label}{chatProviderUsable(item) ? '' : ` · ${t('chưa sẵn sàng', 'not ready')}`}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="fsp-field">
                  <span className="fsp-label">{t('Model', 'Model')}</span>
                  <select value={aiConfig.model} onChange={(e) => setAiConfig({ ...aiConfig, model: e.target.value })} disabled={!aiProvider?.models.length}>
                    {!aiProvider?.models.length && <option value="">{aiLoading ? t('Đang tải model…', 'Loading models…') : t('Chưa có model khả dụng', 'No available model')}</option>}
                    {(aiProvider?.models || []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                  </select>
                </label>
                <label className="fsp-field">
                  <span className="fsp-label">{t('Số tập', 'Episodes')}</span>
                  <input
                    type="text" inputMode="numeric" pattern="[0-9]*"
                    value={numEpisodes}
                    onChange={(e) => setNumEpisodes(e.target.value.replace(/[^0-9]/g, ''))}
                    placeholder="auto"
                    aria-label={t('Số tập', 'Episodes')}
                  />
                </label>
                <label className="fsp-field">
                  <span className="fsp-label">{t('Thời lượng tập (s)', 'Episode duration (s)')}</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    list="fsp-ep-dur-list"
                    value={episodeDuration}
                    onChange={(e) => setEpisodeDuration(e.target.value.replace(/[^0-9]/g, ''))}
                    placeholder={t('auto', 'auto')}
                    aria-label={t('Thời lượng tập', 'Episode duration')}
                  />
                  <datalist id="fsp-ep-dur-list">
                    <option value="30" />
                    <option value="60" />
                    <option value="90" />
                    <option value="120" />
                    <option value="180" />
                    <option value="300" />
                    <option value="600" />
                    <option value="900" />
                    <option value="1200" />
                  </datalist>
                </label>
                <label className="fsp-field">
                  <span className="fsp-label">{t('Thời lượng cảnh', 'Scene duration')}</span>
                  <select value={sceneDuration} onChange={(e) => setSceneDuration(e.target.value)} aria-label={t('Thời lượng cảnh', 'Scene duration')}>
                    <option value="">{t('🤖 Tự chọn', '🤖 Auto')}</option>
                    <option value="4">4s</option>
                    <option value="6">6s</option>
                    <option value="8">8s</option>
                    <option value="10">10s</option>
                  </select>
                </label>
                <label className="fsp-field fsp-field-switch" title={t('Tập sau bắt tiếp cảnh cuối tập trước', 'Each episode continues from the last scene of the previous one')}>
                  <span className="fsp-label">{t('Nối cảnh', 'Linked scenes')}</span>
                  <label className="fsp-headless-cb-row" htmlFor="series-scene-continuity">
                    <input
                      id="series-scene-continuity"
                      type="checkbox"
                      className="fsp-headless-cb"
                      checked={sceneContinuity}
                      onChange={(e) => setSceneContinuity(e.target.checked)}
                    />
                    <span>{sceneContinuity ? t('Bật', 'On') : t('Tắt', 'Off')}</span>
                  </label>
                </label>
                <button
                  type="button"
                  className="fsp-btn fsp-btn-primary fsp-create-go"
                  onClick={() => void draftWithAi()}
                  disabled={!topic.trim() || (!aiConfig.provider && !aiLoading) || drafting}
                  title={
                    !topic.trim() ? t('Nhập chủ đề trước', 'Enter a topic first')
                    : !aiConfig.provider && !aiLoading ? t('Chọn AI Provider trong Cài đặt', 'Select an AI Provider in Settings')
                    : undefined
                  }
                >
                  {drafting ? t('AI đang viết…', 'AI is writing…') : draft ? t('↻ Viết lại', '↻ Rewrite') : t('✨ Tạo series', '✨ Create series')}
                </button>
              </div>

              {!aiLoading && !aiProviders.some(chatProviderUsable) && (
                <p className="fsp-create-hint">{t('Thêm API key trong Cài đặt → AI Provider, hoặc đăng nhập ChatGPT ở tab Chat.', 'Add an API key in Settings → AI Provider, or sign in to ChatGPT in the Chat tab.')}</p>
              )}
            </div>

            {draft ? (
              <div className="fsp-create-card fsp-draft">
                {(() => {
                  const stats = countSeriesScript(draft.text)
                  return (
                    <header className="fsp-draft-head">
                      <div>
                        <strong>{stats.title || t('(chưa có tên)', '(untitled)')}</strong>
                        <span>{t(`${stats.episodes} tập · ${stats.scenes} cảnh`, `${stats.episodes} episodes · ${stats.scenes} scenes`)}</span>
                      </div>
                      <div className="fsp-draft-actions">
                        <button type="button" className="fsp-btn" onClick={() => setDraft(null)}>{t('Bỏ bản nháp', 'Discard draft')}</button>
                        <button type="button" className="fsp-btn fsp-btn-primary" onClick={() => void importDraft()} disabled={!stats.scenes}>
                          {t('Lưu thành Series', 'Save as Series')}
                        </button>
                      </div>
                    </header>
                  )
                })()}
                <label className="fsp-field">
                  <span className="fsp-label">{t('Bible — nhân vật, bối cảnh, phong cách giữ cố định', 'Bible — characters, setting and style kept fixed')}</span>
                  <textarea value={draft.bible} onChange={(e) => setDraft({ ...draft, bible: e.target.value })} rows={5} />
                </label>
                <label className="fsp-field">
                  <span className="fsp-label">{t('Kịch bản (sửa trực tiếp)', 'Script (edit directly)')}</span>
                  <textarea className="fsp-draft-script" value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} rows={16} spellCheck={false} />
                </label>
              </div>
            ) : (
              <div className="fsp-create-alt">
                <span>{t('Hoặc', 'Or')}</span>
                <button type="button" className="fsp-btn" onClick={() => setDraft({ text: '', bible: '' })}>
                  {t('Dán kịch bản TXT', 'Paste a TXT script')}
                </button>
                <div className="fsp-new-series">
                  <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t('Tên series trống', 'Blank series title')} aria-label={t('Tên Series', 'Series title')} onKeyDown={(e) => e.key === 'Enter' && void create()} />
                  <button type="button" className="fsp-btn" onClick={() => void create()} disabled={!title.trim()}>
                    + {t('Tạo trống', 'Create blank')}
                  </button>
                </div>
              </div>
            )}
            {draft && !draft.text && (
              <details className="fsp-import-guide-toggle">
                <summary>{t('Định dạng TXT', 'TXT format')}</summary>
                <pre className="fsp-import-guide">{`# SERIES: Tên series\n# BIBLE\nCharacter: …\n# TẬP 01 — Tên tập\n001_[00.00_00.00-00.00_08.00] Nội dung cảnh 1\n002_[00.00_00.08-00.00_16.00] Nội dung cảnh 2`}</pre>
              </details>
            )}
          </div>
        ) : (
          <>
            {/* ── Series header ── */}
            <header className="fsp-ws-header">
              <div className="fsp-ws-title-row">
                <input
                  className="fsp-title-input"
                  value={selected.title}
                  onChange={(e) => setSelected({ ...selected, title: e.target.value })}
                  aria-label={t('Tên Series', 'Series title')}
                />
                {totalScenes > 0 && (
                  <div className="fsp-progress-badge" title={`${doneScenes}/${totalScenes} ${t('cảnh hoàn thành', 'scenes done')}`}>
                    <div className="fsp-progress-bar" style={{ width: `${Math.round((doneScenes / totalScenes) * 100)}%` }} />
                    <span>{doneScenes}/{totalScenes} ({Math.round((doneScenes / totalScenes) * 100)}%)</span>
                  </div>
                )}
              </div>
              <div className="fsp-ws-actions">
                <button
                  type="button"
                  className="fsp-btn fsp-btn-quick-settings"
                  onClick={() => setActiveTab('settings')}
                  title={t('Mở cài đặt nhanh cho model, tỷ lệ, thời lượng và luồng chạy', 'Open quick settings for model, ratio, duration and threads')}
                >
                  ⚙ {t('Cài đặt nhanh', 'Quick settings')}
                </button>
                {totalScenes > 0 && (
                  <button
                    type="button"
                    className="fsp-btn fsp-btn-primary fsp-btn-run-all"
                    disabled={activeRun?.status === 'running'}
                    onClick={() => void startRun()}
                    title={t('Tạo toàn bộ series theo Cài đặt tạo', 'Run the whole series with the generation settings')}
                  >
                    ▶ {t('Tạo toàn bộ', 'Run entire series')}
                  </button>
                )}
                {videoScenes > 0 && (
                  <button
                    type="button"
                    className="fsp-btn fsp-btn-merge-series"
                    disabled={mergingSeries}
                    title={t('Gh\u00e9p t\u1ea5t c\u1ea3 t\u1eadp th\u00e0nh 1 video series', 'Merge all episodes into 1 series video')}
                    onClick={() => void mergeSeries()}
                  >
                    {mergingSeries ? '\u23f3' : '\ud83d\udcfd'} {t('Gh\u00e9p series', 'Merge series')}
                  </button>
                )}
                {videoScenes > 0 && (
                  <button
                    type="button"
                    className="fsp-btn fsp-btn-preview-ep"
                    onClick={() => {
                      const videos = selected.episodes.flatMap((ep) =>
                        ep.scenes
                          .filter((sc) => sc.videoOutput)
                          .map((sc) => ({ url: toUrl(sc.videoOutput, sc.videoJobId), title: `T${String(ep.index).padStart(2,'0')} C${String(sc.index).padStart(3,'0')} · ${sc.title}` }))
                      )
                      if (videos.length) {
                        setSeriesPreview({ url: videos[0].url, title: videos[0].title, kind: 'video', playlist: videos, idx: 0 })
                      } else {
                        toast.info(t('Chưa có video nào hoàn thành', 'No videos ready yet'))
                      }
                    }}
                  >
                    🎬 {t('Xem toàn bộ', 'Preview all')} ({videoScenes})
                  </button>
                )}
                <button type="button" className="fsp-btn" onClick={() => void saveSeries()} disabled={saving}>
                  {saving ? t('Đang lưu…', 'Saving…') : t('Lưu', 'Save')}
                </button>
                <button type="button" className="fsp-btn fsp-btn-danger" onClick={() => void removeSeries()}>
                  {t('Xóa', 'Delete')}
                </button>
              </div>
            </header>

            {activeRun && (
              <div className={`fsp-run-status fsp-run-${activeRun.status}`}>
                <div className="fsp-run-bar" style={{ width: activeRun.total > 0 ? `${Math.round((activeRun.done / activeRun.total) * 100)}%` : '0%' }} />
                <span className="fsp-run-text">
                  {activeRun.status === 'running'
                    ? t(`Đang chạy… ${activeRun.done}/${activeRun.total} cảnh • ${activeRun.currentStep}`, `Running… ${activeRun.done}/${activeRun.total} scenes • ${activeRun.currentStep}`)
                    : activeRun.status === 'done'
                      ? t(`✅ Hoàn thành — ${activeRun.done}/${activeRun.total} cảnh`, `✅ Done — ${activeRun.done}/${activeRun.total} scenes`)
                      : activeRun.status === 'done_with_errors'
                        ? t(`⚠ Hoàn thành có ${activeRun.errors.length} lỗi`, `⚠ Done with ${activeRun.errors.length} error(s)`)
                        : activeRun.status === 'cancelled'
                          ? t('⏹ Đã dừng', '⏹ Stopped')
                          : t('❌ Lỗi', '❌ Failed')}
                </span>
                {activeRun.status === 'running' ? (
                  <button type="button" className="fsp-btn fsp-btn-danger fsp-btn-sm" onClick={() => void stopRun()}>
                    {t('Dừng', 'Stop')}
                  </button>
                ) : (
                  <button type="button" className="fsp-btn fsp-btn-sm" onClick={() => setActiveRun(null)} aria-label={t('Đóng', 'Close')}>
                    ×
                  </button>
                )}
              </div>
            )}

            {/* ── Tab bar ── */}
            <nav className="fsp-tabs">
              {([
                ['episodes', t('Cảnh', 'Scenes')],
                ['assets', t('Nhân vật & ảnh neo', 'Characters & anchors')],
                ['settings', t('Cài đặt nhanh', 'Quick settings')],
              ] as [typeof activeTab, string][]).map(([tab, label]) => (
                <button key={tab} type="button" className={`fsp-tab${activeTab === tab ? ' is-active' : ''}`} onClick={() => setActiveTab(tab)}>
                  {label}
                </button>
              ))}
            </nav>

            {/* ── Tab: Generation settings ── */}
            {activeTab === 'settings' && (
              <div className="fsp-tab-content">
                <div className="fsp-auto-card">
                  <div className="fsp-auto-top">
                    <div className="fsp-auto-title-group">
                      <span className="fsp-auto-icon">⚡</span>
                      <span className="fsp-auto-heading">{t('Tự động hoá Series', 'Series Automation')}</span>
                    </div>
                    <div className="fsp-auto-actions-group">
                      <span
                        className="fsp-auto-check fsp-toggle is-on"
                        title={t('Ảnh keyframe được duyệt tự động khi tạo xong', 'Keyframes are auto-approved when generation finishes')}
                      >
                        <span className="fsp-toggle-dot" />
                        {t('Tự duyệt ảnh', 'Auto-approve images')}
                      </span>
                    </div>
                  </div>
                  <div className="fsp-auto-grid">
                    {accounts.length > 0 && (
                      <div className="fsp-auto-field fsp-field-wide">
                        <label>{t('Tài khoản', 'Account')}</label>
                        <select
                          value={seriesSettings.accountId}
                          onChange={(e) => saveSeriesSettings({ accountId: e.target.value })}
                          aria-label={t('Tài khoản', 'Account')}
                        >
                          <option value="random">{t('🎲 Ngẫu nhiên tài khoản', '🎲 Random account')}</option>
                          {accounts.map((acc) => (
                            <option key={acc.id} value={acc.id}>
                              {acc.label} · {t(`Gói ${acc.plan}`, `${acc.plan} plan`)}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                    <div className="fsp-auto-field fsp-field-wide">
                      <label>{t('Model video', 'Video model')}</label>
                      <select
                        value={seriesSettings.model}
                        onChange={(e) => saveSeriesSettings({ model: e.target.value })}
                        aria-label={t('Model video', 'Video model')}
                      >
                        {videoModelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
                      </select>
                    </div>
                    <div className="fsp-auto-field">
                      <label>{t('Model ảnh', 'Image model')}</label>
                      <select
                        value={imageModel}
                        onChange={(e) => {
                          setImageModel(e.target.value)
                          localStorage.setItem(SERIES_SETTINGS_KEY, JSON.stringify({ ...seriesSettings, imageModel: e.target.value }))
                        }}
                        aria-label={t('Model ảnh', 'Image model')}
                      >
                        {imageModelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
                      </select>
                    </div>
                    <div className="fsp-auto-field fsp-field-xs">
                      <label>{t('Tỷ lệ', 'Ratio')}</label>
                      <select
                        value={seriesSettings.ratio}
                        onChange={(e) => saveSeriesSettings({ ratio: e.target.value })}
                        aria-label={t('Tỷ lệ', 'Ratio')}
                      >
                        {seriesRatioOptions.map((r) => <option key={r} value={r}>{r}</option>)}
                      </select>
                    </div>
                    {seriesDurationOptions.length > 0 ? (
                    <div className="fsp-auto-field fsp-field-xs">
                      <label>{t('Thời lượng', 'Duration')}</label>
                      <select
                        value={seriesSettings.duration}
                        onChange={(e) => saveSeriesSettings({ duration: e.target.value })}
                        aria-label={t('Thời lượng', 'Duration')}
                      >
                        {isOmniFlash && (
                          <option value="auto">{t('⏱ Auto (timecode)', '⏱ Auto (timecode)')}</option>
                        )}
                        {seriesDurationOptions.map((duration) => <option key={duration} value={duration}>{duration}s</option>)}
                      </select>
                    </div>
                    ) : null}
                    {seriesResolutionOptions.length > 0 ? (
                    <div className="fsp-auto-field fsp-field-xs">
                      <label>{t('Độ phân giải', 'Resolution')}</label>
                      <select
                        value={seriesResolutionOptions.includes(seriesSettings.resolution) ? seriesSettings.resolution : seriesResolutionOptions[0]}
                        onChange={(e) => saveSeriesSettings({
                          resolution: e.target.value,
                          quality: e.target.value,
                        })}
                        aria-label={t('Độ phân giải', 'Resolution')}
                        disabled={seriesResolutionOptions.length < 2}
                      >
                        {seriesResolutionOptions.map((resolution) => (
                          <option key={resolution} value={resolution}>
                            {resolution === '360p'
                              ? t('360p · nhanh', '360p · fast')
                              : resolution}
                          </option>
                        ))}
                      </select>
                    </div>
                    ) : null}
                    <div className="fsp-auto-field fsp-field-xs">
                      <label>{t('Tải về', 'Download')}</label>
                      <select
                        value={seriesSettings.quality || seriesSettings.resolution || '360p'}
                        onChange={(e) => saveSeriesSettings({ quality: e.target.value })}
                        aria-label={t('Chất lượng tải', 'Download quality')}
                      >
                        <option value="360p">{t('360p · nhanh', '360p · fast')}</option>
                        <option value="720p">720p</option>
                        <option value="1080p">1080p</option>
                      </select>
                    </div>
                    <div className="fsp-auto-field fsp-field-xs">
                      <label>{t('Luồng', 'Threads')}</label>
                      <select
                        value={seriesSettings.concurrency || '3'}
                        onChange={(e) => saveSeriesSettings({ concurrency: e.target.value })}
                        aria-label={t('Luồng chạy song song', 'Parallel threads')}
                      >
                        {Array.from({ length: 16 }, (_, index) => String(index + 1)).map((c) => (
                          <option key={c} value={c}>{c}</option>
                        ))}
                      </select>
                    </div>
                    <div className="fsp-auto-field">
                      <label>{t('Quy trình', 'Pipeline')}</label>
                      <select
                        value={autoMode}
                        onChange={(e) => setAutoMode(e.target.value as AutoMode)}
                        aria-label={t('Quy trình', 'Pipeline')}
                      >
                        <option value="full">{t('Khung hình + Video', 'Frames + Video')}</option>
                        <option value="keyframes_only">{t('Chỉ tạo Keyframe', 'Keyframes only')}</option>
                        <option value="videos_only">{t('Chỉ tạo Video từ khung hình', 'Videos from frames only')}</option>
                      </select>
                    </div>
                    <div className="fsp-headless-field">
                      <label htmlFor="fsp-headless-cb">{t('Mở Chrome', 'Open Chrome')}</label>
                      <label className="fsp-headless-cb-row" htmlFor="fsp-headless-cb">
                        <input id="fsp-headless-cb" type="checkbox" className="fsp-headless-cb" checked={!headless} onChange={toggleHeadless} />
                        <span>{!headless ? t('Hiện', 'On') : t('Ẩn', 'Off')}</span>
                      </label>
                    </div>
                  </div>
                  <details className="fsp-merge-settings" open>
                    <summary>{t('Cài đặt ghép video', 'Merge video settings')}</summary>
                    <div className="fsp-merge-body">
                      {/* ── Main output row ── */}
                      <div className="fsp-auto-grid fsp-merge-grid">
                        <div className="fsp-auto-field fsp-field-wide">
                          <label>{t('Chất lượng xuất', 'Output quality')}</label>
                          <select value={mergeSettings.resolution} onChange={(e) => saveMergeSettings({ resolution: e.target.value })}>
                            <option value="auto">{t('Auto theo media · 1080p', 'Auto from media · 1080p')}</option>
                            <option value="1920x1080">{t('1080p ngang', '1080p landscape')}</option>
                            <option value="1080x1920">{t('1080p dọc', '1080p portrait')}</option>
                            <option value="1080x1080">{t('1080p vuông', '1080p square')}</option>
                            <option value="1280x720">{t('720p ngang', '720p landscape')}</option>
                          </select>
                        </div>
                        <div className="fsp-auto-field fsp-field-xs">
                          <label>{t('FPS xuất video', 'Output FPS')}</label>
                          <input type="number" min={1} max={120} value={mergeSettings.fps} onChange={(e) => saveMergeSettings({ fps: Number(e.target.value) || 30 })} />
                        </div>
                        <div className="fsp-auto-field fsp-field-xs">
                          <label>{t('Chất lượng nén (CRF)', 'Compression quality (CRF)')}</label>
                          <select value={mergeSettings.crf} onChange={(e) => saveMergeSettings({ crf: Number(e.target.value) || 20 })}>
                            <option value={18}>{t('Cao · file lớn', 'High · larger file')}</option>
                            <option value={20}>{t('Cân bằng', 'Balanced')}</option>
                            <option value={23}>{t('Nhanh · file nhỏ', 'Fast · smaller file')}</option>
                          </select>
                        </div>
                        <div className="fsp-auto-field fsp-field-xs">
                          <label>{t('Bộ mã hóa', 'Encoder')}</label>
                          <select value={mergeSettings.encoder} onChange={(e) => saveMergeSettings({ encoder: e.target.value as MergeSettings['encoder'] })}>
                            <option value="auto">{t('Tự động', 'Automatic')}</option>
                            <option value="gpu">GPU</option>
                            <option value="cpu">CPU</option>
                          </select>
                        </div>
                        <div className="fsp-auto-field fsp-field-xs">
                          <label>{t('Tốc độ (%)', 'Speed (%)')}</label>
                          <input type="number" min={25} max={400} value={mergeSettings.speed} onChange={(e) => saveMergeSettings({ speed: Number(e.target.value) || 100 })} />
                        </div>
                        <div className="fsp-auto-field fsp-field-xs">
                          <label>{t('Âm lượng (%)', 'Volume (%)')}</label>
                          <input type="number" min={0} max={300} value={mergeSettings.volume} onChange={(e) => saveMergeSettings({ volume: Number(e.target.value) || 100 })} />
                        </div>
                        <div className="fsp-auto-field fsp-field-xs">
                          <label>{t('Preview (giây)', 'Preview (seconds)')}</label>
                          <input type="number" min={0} max={120} value={mergeSettings.previewSeconds} onChange={(e) => saveMergeSettings({ previewSeconds: Number(e.target.value) || 0 })} />
                        </div>
                        <label className="fsp-merge-check">
                          <input type="checkbox" checked={mergeSettings.removeMetadata} onChange={(e) => saveMergeSettings({ removeMetadata: e.target.checked })} />
                          <span>{t('Xóa metadata file xuất', 'Remove output metadata')}</span>
                        </label>
                      </div>

                      {/* ── Transitions & motion ── */}
                      <details className="fsp-merge-group" open>
                        <summary>{t('Chuyển cảnh & chuyển động', 'Transitions & motion')}</summary>
                        <div className="fsp-auto-grid fsp-merge-grid">
                          <div className="fsp-auto-field">
                            <label>{t('Nền tảng khung hình', 'Frame platform')}</label>
                            <select value={mergeSettings.targetPlatform} onChange={(e) => saveMergeSettings({ targetPlatform: e.target.value })}>
                              <option value="auto">{t('Tự động', 'Automatic')}</option>
                              <option value="youtube">YouTube</option>
                              <option value="shorts">Shorts / Reels</option>
                              <option value="tiktok">TikTok</option>
                            </select>
                          </div>
                          <div className="fsp-auto-field">
                            <label>{t('Hiệu ứng chuyển cảnh', 'Transition effect')}</label>
                            <select value={mergeSettings.effect} onChange={(e) => saveMergeSettings({ effect: e.target.value })}>
                              <option value="none">{t('Tắt', 'Off')}</option>
                              <option value="random">{t('Ngẫu nhiên', 'Random')}</option>
                              <option value="fade">Fade</option>
                              <option value="dissolve">Dissolve</option>
                            </select>
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Thời lượng chuyển cảnh (giây)', 'Transition duration (seconds)')}</label>
                            <input type="number" min={0} max={5} step={0.05} value={mergeSettings.transitionDuration} onChange={(e) => saveMergeSettings({ transitionDuration: Number(e.target.value) || 0 })} />
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>Zoom</label>
                            <select value={mergeSettings.zoom} onChange={(e) => saveMergeSettings({ zoom: e.target.value })}>
                              <option value="off">{t('Tắt', 'Off')}</option>
                              <option value="random">{t('Ngẫu nhiên', 'Random')}</option>
                              <option value="zoomIn">Zoom in</option>
                              <option value="zoomOut">Zoom out</option>
                              <option value="left">{t('Trái → phải', 'Left → right')}</option>
                              <option value="right">{t('Phải → trái', 'Right → left')}</option>
                              <option value="up">{t('Dưới → trên', 'Bottom → top')}</option>
                              <option value="down">{t('Trên → dưới', 'Top → bottom')}</option>
                            </select>
                          </div>
                        </div>
                      </details>

                      {/* ── Subtitle ── */}
                      <details className="fsp-merge-group" open>
                        <summary>{t('Phụ đề SRT', 'SRT subtitles')}</summary>
                        <div className="fsp-auto-grid fsp-merge-grid">
                          <label className="fsp-merge-check fsp-merge-field-full">
                            <input type="checkbox" checked={mergeSettings.subtitleEnabled} onChange={(e) => saveMergeSettings({ subtitleEnabled: e.target.checked })} />
                            <span>{t('Chèn phụ đề SRT', 'Burn SRT subtitles')}</span>
                          </label>
                          <div className="fsp-auto-field">
                            <label>{t('Phông chữ', 'Font')}</label>
                            <select value={mergeSettings.subtitleFontFamily} onChange={(e) => saveMergeSettings({ subtitleFontFamily: e.target.value })}>
                              <option value="system">{t('Hệ thống', 'System')}</option>
                              <option value="Arial">Arial</option>
                              <option value="Roboto">Roboto</option>
                              <option value="Montserrat">Montserrat</option>
                            </select>
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Cỡ chữ', 'Font size')}</label>
                            <input type="number" min={6} max={120} value={mergeSettings.subtitleSize} onChange={(e) => saveMergeSettings({ subtitleSize: Number(e.target.value) || 8 })} />
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Lề dưới', 'Bottom margin')}</label>
                            <input type="number" min={0} max={1000} value={mergeSettings.subtitleMargin} onChange={(e) => saveMergeSettings({ subtitleMargin: Number(e.target.value) || 0 })} />
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Lệch thời gian (giây)', 'Time offset (seconds)')}</label>
                            <input type="number" min={-3600} max={3600} step={0.1} value={mergeSettings.subtitleOffset} onChange={(e) => saveMergeSettings({ subtitleOffset: Number(e.target.value) || 0 })} />
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Nền phụ đề', 'Subtitle background')}</label>
                            <select value={mergeSettings.subtitleBackground} onChange={(e) => saveMergeSettings({ subtitleBackground: e.target.value })}>
                              <option value="solid">{t('Nền đặc', 'Solid')}</option>
                              <option value="blur">{t('Mờ nền', 'Blur')}</option>
                              <option value="none">{t('Không nền', 'None')}</option>
                            </select>
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Độ mờ nền (%)', 'Background opacity (%)')}</label>
                            <input type="number" min={0} max={100} value={mergeSettings.subtitleOpacity} onChange={(e) => saveMergeSettings({ subtitleOpacity: Number(e.target.value) || 0 })} />
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Màu chữ', 'Text color')}</label>
                            <input type="color" value={mergeSettings.subtitleColor} onChange={(e) => saveMergeSettings({ subtitleColor: e.target.value })} />
                          </div>
                          <div className="fsp-auto-field fsp-field-xs">
                            <label>{t('Màu nền', 'Background color')}</label>
                            <input type="color" value={mergeSettings.subtitleBgColor} onChange={(e) => saveMergeSettings({ subtitleBgColor: e.target.value })} />
                          </div>
                        </div>
                      </details>

                      {/* ── Drawing & Delogo ── */}
                      <details className="fsp-merge-group">
                        <summary>{t('Vẽ ảnh & xóa logo gốc', 'Drawing & remove original logo')}</summary>
                        <div className="fsp-auto-grid fsp-merge-grid">
                          <label className="fsp-merge-check fsp-merge-field-full">
                            <input type="checkbox" checked={mergeSettings.drawingEnabled} onChange={(e) => saveMergeSettings({ drawingEnabled: e.target.checked })} />
                            <span>{t('Vẽ ảnh tĩnh thành video', 'Turn still images into drawing videos')}</span>
                          </label>
                          {mergeSettings.drawingEnabled && <>
                            <div className="fsp-auto-field">
                              <label>{t('Kiểu vẽ', 'Drawing style')}</label>
                              <select value={mergeSettings.drawingMode} onChange={(e) => saveMergeSettings({ drawingMode: e.target.value })}>
                                <option value="hand">{t('Tay + bút', 'Hand + pen')}</option>
                                <option value="drawing">{t('Vẽ nét', 'Strokes')}</option>
                              </select>
                            </div>
                            <div className="fsp-auto-field">
                              <label>{t('Dụng cụ', 'Tool')}</label>
                              <select value={mergeSettings.drawingTool} onChange={(e) => saveMergeSettings({ drawingTool: e.target.value })}>
                                <option value="pencil">{t('Chì', 'Pencil')}</option>
                                <option value="pen">{t('Bút', 'Pen')}</option>
                                <option value="marker">Marker</option>
                                <option value="brush">{t('Cọ', 'Brush')}</option>
                              </select>
                            </div>
                            <div className="fsp-auto-field fsp-field-xs">
                              <label>{t('Độ chi tiết (%)', 'Detail (%)')}</label>
                              <input type="number" min={10} max={100} value={mergeSettings.drawingDetail} onChange={(e) => saveMergeSettings({ drawingDetail: Number(e.target.value) || 72 })} />
                            </div>
                            <div className="fsp-auto-field fsp-field-xs">
                              <label>{t('Độ dày nét', 'Stroke thickness')}</label>
                              <input type="number" min={1} max={8} value={mergeSettings.drawingThickness} onChange={(e) => saveMergeSettings({ drawingThickness: Number(e.target.value) || 2 })} />
                            </div>
                            <div className="fsp-auto-field fsp-merge-field-full">
                              <label>{t('Đường đi nét', 'Stroke route')}</label>
                              <select value={mergeSettings.drawingStrokeOrder} onChange={(e) => saveMergeSettings({ drawingStrokeOrder: e.target.value })}>
                                <option value="natural">{t('Tự nhiên theo đối tượng', 'Natural by object')}</option>
                                <option value="outline">{t('Theo viền thật', 'True outlines')}</option>
                                <option value="region">{t('Từng vùng hoàn chỉnh', 'Complete one region')}</option>
                                <option value="reading">{t('Theo chữ · trái sang phải', 'Text · left to right')}</option>
                                <option value="center">{t('Từ tâm lan ra', 'Centre outward')}</option>
                              </select>
                            </div>
                          </>}
                          <label className="fsp-merge-check">
                            <input type="checkbox" checked={mergeSettings.delogoEnabled} onChange={(e) => saveMergeSettings({ delogoEnabled: e.target.checked })} />
                            <span>{t('Xóa logo gốc', 'Remove original logo')}</span>
                          </label>
                          {mergeSettings.delogoEnabled && <>
                            <label className="fsp-merge-check">
                              <input type="checkbox" checked={mergeSettings.delogoAuto} onChange={(e) => saveMergeSettings({ delogoAuto: e.target.checked })} />
                              <span>{t('Tự định vị logo', 'Auto position logo')}</span>
                            </label>
                            {!mergeSettings.delogoAuto && <>
                              <div className="fsp-auto-field fsp-field-xs"><label>X (%)</label><input type="number" min={0} max={100} value={mergeSettings.delogoX} onChange={(e) => saveMergeSettings({ delogoX: Number(e.target.value) || 0 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>Y (%)</label><input type="number" min={0} max={100} value={mergeSettings.delogoY} onChange={(e) => saveMergeSettings({ delogoY: Number(e.target.value) || 0 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Rộng (%)', 'Width (%)')}</label><input type="number" min={1} max={100} value={mergeSettings.delogoW} onChange={(e) => saveMergeSettings({ delogoW: Number(e.target.value) || 1 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Cao (%)', 'Height (%)')}</label><input type="number" min={1} max={100} value={mergeSettings.delogoH} onChange={(e) => saveMergeSettings({ delogoH: Number(e.target.value) || 1 })} /></div>
                            </>}
                          </>}
                        </div>
                      </details>

                      {/* ── Logo / watermark ── */}
                      <details className="fsp-merge-group">
                        <summary>{t('Logo / watermark', 'Logo / watermark')}</summary>
                        <div className="fsp-auto-grid fsp-merge-grid">
                          <label className="fsp-merge-check fsp-merge-field-full">
                            <input type="checkbox" checked={mergeSettings.logoEnabled} onChange={(e) => saveMergeSettings({ logoEnabled: e.target.checked })} />
                            <span>{t('Chèn logo vào video', 'Add logo to video')}</span>
                          </label>
                          {mergeSettings.logoEnabled && <>
                            <div className="fsp-auto-field">
                              <label>{t('Nguồn logo', 'Logo source')}</label>
                              <select value={mergeSettings.logoSource} onChange={(e) => saveMergeSettings({ logoSource: e.target.value as MergeSettings['logoSource'] })}>
                                <option value="text">{t('Chữ', 'Text')}</option>
                                <option value="icon">Icon</option>
                              </select>
                            </div>
                            {mergeSettings.logoSource === 'text'
                              ? <div className="fsp-auto-field fsp-field-wide"><label>{t('Nội dung', 'Content')}</label><input value={mergeSettings.logoText} onChange={(e) => saveMergeSettings({ logoText: e.target.value })} /></div>
                              : <div className="fsp-auto-field fsp-field-xs"><label>Icon</label><select value={mergeSettings.logoIcon} onChange={(e) => saveMergeSettings({ logoIcon: e.target.value })}><option>★</option><option>▶</option><option>●</option><option>◆</option></select></div>
                            }
                            <div className="fsp-auto-field fsp-field-xs"><label>{t('Độ mờ (%)', 'Opacity (%)')}</label><input type="number" min={5} max={100} value={mergeSettings.logoOpacity} onChange={(e) => saveMergeSettings({ logoOpacity: Number(e.target.value) || 5 })} /></div>
                            <div className="fsp-auto-field fsp-field-xs"><label>X (%)</label><input type="number" min={0} max={100} value={mergeSettings.logoX} onChange={(e) => saveMergeSettings({ logoX: Number(e.target.value) || 0 })} /></div>
                            <div className="fsp-auto-field fsp-field-xs"><label>Y (%)</label><input type="number" min={0} max={100} value={mergeSettings.logoY} onChange={(e) => saveMergeSettings({ logoY: Number(e.target.value) || 0 })} /></div>
                            <div className="fsp-auto-field fsp-field-xs"><label>{t('Chuyển động', 'Motion')}</label><select value={mergeSettings.logoMotion} onChange={(e) => saveMergeSettings({ logoMotion: e.target.value })}><option value="fixed">{t('Cố định', 'Static')}</option><option value="random">{t('Ngẫu nhiên', 'Random')}</option></select></div>
                            <div className="fsp-auto-field fsp-field-xs"><label>{t('Phạm vi', 'Scope')}</label><select value={mergeSettings.logoScope} onChange={(e) => saveMergeSettings({ logoScope: e.target.value })}><option value="full">{t('Toàn video', 'Entire video')}</option><option value="range">{t('Theo đoạn', 'Selected range')}</option></select></div>
                            {mergeSettings.logoSource === 'text' && <>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Cỡ chữ', 'Font size')}</label><input type="number" min={6} max={160} value={mergeSettings.logoFontSize} onChange={(e) => saveMergeSettings({ logoFontSize: Number(e.target.value) || 32 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Màu chữ', 'Text color')}</label><input type="color" value={mergeSettings.logoColor} onChange={(e) => saveMergeSettings({ logoColor: e.target.value })} /></div>
                            </>}
                            {mergeSettings.logoSource !== 'text' && <div className="fsp-auto-field fsp-field-xs"><label>{t('Kích thước (%)', 'Size (%)')}</label><input type="number" min={2} max={30} value={mergeSettings.logoSize} onChange={(e) => saveMergeSettings({ logoSize: Number(e.target.value) || 8 })} /></div>}
                            {mergeSettings.logoMotion === 'random' && <>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Hiện (giây)', 'Visible (seconds)')}</label><input type="number" min={0.5} step={0.1} value={mergeSettings.logoVisibleSec} onChange={(e) => saveMergeSettings({ logoVisibleSec: Number(e.target.value) || 0.5 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Ẩn (giây)', 'Hidden (seconds)')}</label><input type="number" min={0} step={0.1} value={mergeSettings.logoHiddenSec} onChange={(e) => saveMergeSettings({ logoHiddenSec: Number(e.target.value) || 0 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>Fade (s)</label><input type="number" min={0} step={0.1} value={mergeSettings.logoFadeSec} onChange={(e) => saveMergeSettings({ logoFadeSec: Number(e.target.value) || 0 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Lề an toàn (%)', 'Safe margin (%)')}</label><input type="number" min={0} max={20} value={mergeSettings.logoSafeMargin} onChange={(e) => saveMergeSettings({ logoSafeMargin: Number(e.target.value) || 0 })} /></div>
                            </>}
                            {mergeSettings.logoScope === 'range' && <>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Hiện từ (giây)', 'Show from (seconds)')}</label><input type="number" min={0} value={mergeSettings.logoStart} onChange={(e) => saveMergeSettings({ logoStart: Number(e.target.value) || 0 })} /></div>
                              <div className="fsp-auto-field fsp-field-xs"><label>{t('Đến (giây)', 'Until (seconds)')}</label><input type="number" min={0} value={mergeSettings.logoEnd} onChange={(e) => saveMergeSettings({ logoEnd: Number(e.target.value) || 0 })} /></div>
                            </>}
                          </>}
                        </div>
                      </details>
                    </div>
                  </details>
                  <p className="fsp-auto-hint">
                    {t('Series luôn tạo video từ khung hình: keyframe đã duyệt, ảnh neo hoặc khung cuối cảnh trước.', 'Series video always starts from a frame: an approved keyframe, an anchor image, or the previous scene end frame.')}
                  </p>

                </div>
              </div>
            )}

            {/* ── Tab: Anchor images ── */}
            {activeTab === 'assets' && (
              <div className="fsp-tab-content fsp-assets">

                {/* ── Bible + Description compact row ── */}
                <div className="fsp-bible-row">
                  <label className="fsp-field fsp-bible-field">
                    <span className="fsp-label">📖 {t('Series Bible', 'Series Bible')}</span>
                    <textarea
                      value={selected.bible}
                      onChange={(e) => setSelected({ ...selected, bible: e.target.value })}
                      placeholder={t('Nhân vật, skin, đạo cụ, phong cách không được thay đổi…', 'Character, skin, props, and style that must not change…')}
                      rows={5}
                    />
                  </label>
                  <label className="fsp-field fsp-desc-field">
                    <span className="fsp-label">📝 {t('Mô tả', 'Description')}</span>
                    <textarea
                      value={selected.description}
                      onChange={(e) => setSelected({ ...selected, description: e.target.value })}
                      rows={5}
                    />
                  </label>
                </div>

                {/* ── Generate anchor image ── */}
                <div className="fsp-anchor-section">
                  <div className="fsp-anchor-section-head">
                    <span className="fsp-section-title">🎨 {t('Tạo ảnh khoá nhân vật', 'Generate character lock image')}</span>
                    <span className="fsp-section-hint">{t('Ảnh neo giữ nhân vật nhất quán xuyên suốt toàn bộ tập', 'Anchor images keep characters consistent across all episodes')}</span>
                    {selected.bible.trim() && (
                      <button
                        type="button"
                        className="fsp-btn fsp-btn-sm"
                        onClick={() => setAnchorPrompt(selected.bible.trim())}
                        title={t('Sử dụng Bible làm điểm bắt đầu cho prompt ảnh', 'Use Bible as starting point for image prompt')}
                      >
                        ← {t('Lấy từ Bible', 'From Bible')}
                      </button>
                    )}
                  </div>
                  <div className="fsp-anchor-create">
                    <textarea
                      value={anchorPrompt}
                      onChange={(e) => setAnchorPrompt(e.target.value)}
                      rows={3}
                      placeholder={t('Mô tả nhân vật, trang phục, đạo cụ và phong cách cần giữ cố định…', 'Describe the character, wardrobe, props, and art style to keep consistent…')}
                    />
                    <button type="button" className="fsp-btn fsp-btn-primary" onClick={() => void generateAnchor()} disabled={!anchorPrompt.trim() || Boolean(anchorJobId)}>
                      {anchorJobId ? t('Đang tạo…', 'Generating…') : t('✨ Tạo ảnh', '✨ Generate')}
                    </button>
                  </div>
                </div>

                {/* ── Asset list ── */}
                <div className="fsp-anchor-section">
                  <div className="fsp-anchor-section-head">
                    <span className="fsp-section-title">🖼️ {t('Ảnh tham chiếu', 'Reference images')}</span>
                    <span className="fsp-section-hint">{t('Tối đa 3 ảnh neo · Ảnh khóa luôn được dùng', 'Up to 3 anchor images · Locked images are always used')}</span>
                    <button type="button" className="fsp-btn fsp-btn-secondary fsp-btn-sm" onClick={() => assetInput.current?.click()}>
                      + {t('Thêm ảnh', 'Add image')}
                    </button>
                    <input ref={assetInput} hidden type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => void uploadAsset(e.target.files?.[0])} />
                  </div>
                  {selected.assets.length === 0 ? (
                    <div className="fsp-asset-empty">
                      <span>🖼️</span>
                      <p>{t('Chưa có ảnh. Tạo ảnh khoá nhân vật phía trên hoặc thêm ảnh thủ công.', 'No images yet. Generate a character lock image above or add one manually.')}</p>
                    </div>
                  ) : (
                    <div className="fsp-asset-grid">
                      {selected.assets.map((asset) => {
                        const isAnchor = selected.anchorAssets.includes(asset.id)
                        const anchorIdx = selected.anchorAssets.indexOf(asset.id)
                        return (
                          <article key={asset.id} className={`fsp-asset-card${isAnchor ? ' is-anchor' : ''}${asset.locked ? ' is-locked' : ''}`}>
                            <div className="fsp-asset-img-wrap" onClick={() => setSeriesPreview({ url: `/api/flow/series/${selected.id}/assets/${asset.id}`, title: asset.label || asset.name, kind: "image" })}>
                              <img loading="lazy" src={`/api/flow/series/${selected.id}/assets/${asset.id}`} alt={asset.label || asset.name} />
                              {isAnchor && <span className="fsp-anchor-badge">#{anchorIdx + 1}</span>}
                              {asset.locked && <span className="fsp-lock-badge">🔒</span>}
                              <div className="fsp-asset-overlay">
                                <span>{t('Xem', 'Preview')}</span>
                              </div>
                            </div>
                            <div className="fsp-asset-info">
                              <span className="fsp-asset-name">{asset.label || asset.name}</span>
                            </div>
                            <div className="fsp-asset-actions">
                              <label className="fsp-asset-check">
                                <input type="checkbox" checked={isAnchor} onChange={() => void toggleAnchor(asset.id)} />
                                <span>{t('Neo', 'Anchor')}</span>
                              </label>
                              <button
                                type="button"
                                className={`fsp-btn fsp-btn-sm${asset.locked ? ' fsp-btn-lock-active' : ''}`}
                                onClick={() => void toggleAssetLock(asset)}
                              >
                                {asset.locked ? t('Bỏ khóa', 'Unlock') : t('Khóa', 'Lock')}
                              </button>
                              <button
                                type="button"
                                className="fsp-btn fsp-btn-sm fsp-btn-danger"
                                onClick={() => void deleteAsset(asset.id)}
                              >
                                {t('Xóa', 'Delete')}
                              </button>
                            </div>
                          </article>
                        )
                      })}
                    </div>
                  )}
                </div>

              </div>
            )}


            {/* ── Tab: Episodes & Scenes ── */}
            {activeTab === 'episodes' && (
              <div className="fsp-tab-content fsp-episodes">
                <div className="fsp-episodes-add">

                  <input
                    value={episodeTitle}
                    onChange={(e) => setEpisodeTitle(e.target.value)}
                    placeholder={t('Tên tập mới', 'New episode title')}
                    onKeyDown={(e) => e.key === 'Enter' && void addEpisode()}
                  />
                  <button type="button" className="fsp-btn fsp-btn-secondary" onClick={() => void addEpisode()}>
                    + {t('Thêm tập', 'Add episode')}
                  </button>
                  <button
                    type="button"
                    className={`fsp-btn fsp-btn-extend${extendOpen ? ' is-active' : ''}`}
                    onClick={() => { setExtendOpen(!extendOpen); setExtendDraft(null) }}
                    disabled={!aiConfig.provider}
                    title={aiConfig.provider ? undefined : t('Thêm API key trong Cài đặt → AI Provider', 'Add an API key in Settings → AI Provider')}
                  >
                    ✨ {t('Thêm tập bằng AI', 'Add episodes with AI')}
                  </button>
                </div>

                {extendOpen && (
                  <div className="fsp-extend-panel">
                    <div className="fsp-extend-row">
                      <label className="fsp-field">
                        <span className="fsp-label">{t('Số tập thêm', 'Episodes to add')}</span>
                        <input
                          type="text" inputMode="numeric" pattern="[0-9]*"
                          value={extendNumEpisodes}
                          onChange={(e) => setExtendNumEpisodes(e.target.value.replace(/[^0-9]/g, '') || '1')}
                          style={{ width: 60 }}
                        />
                      </label>
                      <span className="fsp-extend-hint">
                        {t('AI sẽ đọc Bible + cảnh cuối để viết tiếp nội dung liên tục', 'AI reads the Bible and last scene to continue the story seamlessly')}
                      </span>
                      <button
                        type="button"
                        className="fsp-btn fsp-btn-primary"
                        onClick={() => void draftMoreEpisodes()}
                        disabled={extendDrafting}
                      >
                        {extendDrafting ? t('AI đang viết…', 'AI is writing…') : extendDraft ? t('↻ Viết lại', '↻ Rewrite') : t('✨ Tạo nháp', '✨ Draft')}
                      </button>
                      <button type="button" className="fsp-btn" onClick={() => { setExtendOpen(false); setExtendDraft(null) }}>
                        {t('Đóng', 'Close')}
                      </button>
                    </div>
                    {extendDraft && (
                      <>
                        <label className="fsp-field">
                          <span className="fsp-label">{t('Kịch bản tập mới (sửa trực tiếp)', 'New episode script (edit directly)')}</span>
                          <textarea
                            className="fsp-draft-script"
                            value={extendDraft.text}
                            onChange={(e) => setExtendDraft({ text: e.target.value })}
                            rows={14}
                            spellCheck={false}
                          />
                        </label>
                        <div className="fsp-extend-actions">
                          <button type="button" className="fsp-btn" onClick={() => setExtendDraft(null)}>
                            {t('Bỏ nháp', 'Discard draft')}
                          </button>
                          <button
                            type="button"
                            className="fsp-btn fsp-btn-primary"
                            onClick={() => void appendEpisodes()}
                            disabled={!extendDraft.text.trim()}
                          >
                            {t('Thêm vào Series', 'Add to series')}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}


                {selected.episodes.length === 0 ? (
                  <div className="fsp-empty-episodes">
                    <span>🎞️</span>
                    <p>{t('Chưa có tập. Thêm tập đầu tiên để tạo các cảnh.', 'No episode yet. Add the first episode to create scenes.')}</p>
                  </div>
                ) : (
                  <div className="fsp-episode-list">
                    {selected.episodes.map((episode) => {
                      const isCollapsed = collapsedEpisodes.has(episode.id)
                      const doneCount = episode.scenes.filter((s) => s.status === 'complete').length
                      const totalCount = episode.scenes.length
                      return (
                        <article key={episode.id} className={`fsp-episode${isCollapsed ? ' is-collapsed' : ''}`}>
                          <header className="fsp-episode-head" onClick={() => toggleEpisode(episode.id)}>
                            <div className="fsp-episode-title-row">
                              <span className="fsp-episode-toggle">{isCollapsed ? '▶' : '▼'}</span>
                              <strong className="fsp-episode-label">
                                {t(`Tập ${String(episode.index).padStart(2, '0')}`, `Episode ${String(episode.index).padStart(2, '0')}`)}
                              </strong>
                              <span className="fsp-episode-name">{episode.title}</span>
                              {totalCount > 0 && (
                                <>
                                  <span className={`fsp-ep-badge${doneCount === totalCount && totalCount > 0 ? ' is-done' : ''}`}>
                                    {doneCount}/{totalCount}
                                  </span>
                                  {/* Episode progress bar */}
                                  <div className="fsp-ep-progress">
                                    <div className="fsp-ep-progress-fill" style={{ width: `${Math.round((doneCount/totalCount)*100)}%` }} />
                                  </div>
                                </>
                              )}
                            </div>
                            <div className="fsp-episode-tools" onClick={(e) => e.stopPropagation()}>
                              <button type="button" className="fsp-ep-tool-btn" title={t('Mở cài đặt nhanh', 'Open quick settings')} onClick={() => setActiveTab('settings')}>
                                ⚙
                              </button>
                              <button
                                type="button"
                                className="fsp-ep-tool-btn fsp-ep-run"
                                disabled={activeRun?.status === 'running'}
                                title={t('Tự động tạo toàn bộ tập này', 'Auto-run this episode')}
                                onClick={(e) => { e.stopPropagation(); void startRun(episode.id) }}
                              >
                                ▶ {t('Tạo tập', 'Run ep')}
                              </button>
                              {episode.scenes.some((sc) => sc.videoOutput) && (
                                <button
                                  type="button"
                                  className="fsp-ep-tool-btn fsp-ep-preview"
                                  title={t('Xem trước tập này', 'Preview this episode')}
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    const videos = episode.scenes
                                      .filter((sc) => sc.videoOutput)
                                      .map((sc) => ({ url: toUrl(sc.videoOutput, sc.videoJobId), title: `${String(sc.index).padStart(3,'0')} · ${sc.title || sc.prompt?.slice(0,30) || ''}` }))
                                    if (videos.length) {
                                      setSeriesPreview({ url: videos[0].url, title: videos[0].title, kind: 'video', playlist: videos, idx: 0 })
                                    } else {
                                      toast.info(t('Tập này chưa có video nào', 'No videos ready in this episode'))
                                    }
                                  }}
                                >
                                  🎬 {t('Xem tập', 'Preview ep')} ({episode.scenes.filter(s => s.videoOutput).length})
                                </button>
                              )}
                              {episode.scenes.some((s) => s.videoOutput) && (
                                <button type="button" className="fsp-ep-tool-btn fsp-ep-merge" title={t('Ghép thành 1 video', 'Merge into 1 video')} onClick={() => void mergeEpisode(episode)} disabled={mergingEpisodeId === episode.id}>
                                  {mergingEpisodeId === episode.id ? '⏳' : '📽'}
                                </button>
                              )}
                              {episode.scenes.some((s) => s.videoOutput) && (
                                <button type="button" className="fsp-ep-tool-btn" title={t('Mở video ghép của tập', 'Reveal this episode merged video')} onClick={() => void openEpisodeFolder(episode)}>
                                  📂
                                </button>
                              )}
                              <button type="button" className="fsp-ep-tool-btn fsp-ep-del" title={t('Xóa tập', 'Delete episode')} onClick={() => void deleteEpisode(episode)}>
                                ×
                              </button>
                            </div>
                          </header>

                          {!isCollapsed && (
                            <div className="fsp-scene-list">
                              {episode.scenes.map((scene) => {
                                const st = sceneStatusMeta(scene.status, t)
                                const thumb = scene.approvedKeyframe || scene.keyframeOutput
                                return (
                                  <div key={scene.id} className="fsp-scene">
                                    {/* Thumbnail / Video Preview */}
                                    {(thumb || scene.videoOutput) ? (
                                      <div
                                        className="fsp-scene-thumb"
                                        title={scene.videoOutput ? t('Xem video cảnh', 'Preview scene video') : t('Xem ảnh', 'Preview image')}
                                        onClick={(e) => {
                                          e.stopPropagation()
                                          if (scene.videoOutput) {
                                            setSeriesPreview({ url: toUrl(scene.videoOutput, scene.videoJobId), title: scene.title || `Cảnh ${scene.index}`, kind: 'video' })
                                          } else if (thumb) {
                                            setSeriesPreview({ url: toUrl(thumb, undefined, selected.id), title: scene.title || `Cảnh ${scene.index}`, kind: 'image' })
                                          }
                                        }}
                                      >
                                        {thumb
                                          ? <img src={toUrl(thumb, undefined, selected.id)} alt={scene.title} loading="lazy" />
                                          : <video src={toUrl(scene.videoOutput, scene.videoJobId)} preload="metadata" muted playsInline />}
                                        {scene.videoOutput && <span className="fsp-scene-has-video">▶</span>}
                                      </div>
                                    ) : (
                                      <div className="fsp-scene-thumb fsp-scene-thumb-empty">
                                        <span>{String(scene.index).padStart(3, '0')}</span>
                                      </div>
                                    )}

                                    {/* Info */}
                                    <div className="fsp-scene-info">
                                      <div className="fsp-scene-row1">
                                        <b className="fsp-scene-title">{String(scene.index).padStart(3, '0')} · {scene.title}</b>
                                        <span className={`fsp-status-badge ${st.cls}`}>{st.label}</span>
                                      </div>
                                      {scene.timecode && <code className="fsp-scene-timecode">{scene.timecode}</code>}
                                      <p className="fsp-scene-prompt">{scene.prompt}</p>
                                      {scene.error && (
                                        <div className="fsp-scene-error-row">
                                          <p className="fsp-scene-error">⚠ {scene.error}</p>
                                          <div className="fsp-scene-error-actions">
                                            <button
                                              type="button"
                                              className="fsp-btn fsp-btn-sm fsp-btn-retry"
                                              disabled={generatingScene === scene.id}
                                              onClick={() => void retryScene(episode, scene, 'retry')}
                                            >
                                              {t('Chạy lại', 'Retry')}
                                            </button>
                                            <button
                                              type="button"
                                              className="fsp-btn fsp-btn-sm fsp-btn-create"
                                              disabled={generatingScene === scene.id}
                                              onClick={() => void retryScene(episode, scene, 'create')}
                                            >
                                              {t('Tạo mới', 'Create new')}
                                            </button>
                                            <label className="fsp-chrome-inline">
                                              <input type="checkbox" className="fsp-headless-cb" checked={!headless} onChange={toggleHeadless} />
                                              {t('Mở Chrome', 'Open Chrome')}
                                            </label>
                                          </div>
                                        </div>
                                      )}

                                      <details className="fsp-scene-overrides">
                                        <summary>{t('Tuỳ chỉnh cảnh', 'Scene overrides')}</summary>
                                        <div className="fsp-scene-overrides-body">
                                          <textarea
                                            defaultValue={scene.promptOverride}
                                            onBlur={(e) => void updateScene(episode, scene, { promptOverride: e.target.value })}
                                            placeholder={t('Bổ sung hoặc điều chỉnh prompt riêng cho cảnh này', 'Add or adjust the prompt for this scene only')}
                                            rows={3}
                                          />
                                          {selected.assets.length > 0 && (
                                            <select
                                              multiple
                                              value={scene.referenceAssetIds}
                                              onChange={(e) => void updateScene(episode, scene, { referenceAssetIds: Array.from(e.currentTarget.selectedOptions).map((o) => o.value) })}
                                              aria-label={t('Ảnh tham chiếu riêng của cảnh', 'Scene-specific reference images')}
                                            >
                                              {selected.assets.map((asset) => (
                                                <option key={asset.id} value={asset.id}>{asset.label || asset.name}</option>
                                              ))}
                                            </select>
                                          )}
                                        </div>
                                      </details>


                                      {/* Actions — clean text buttons */}
                                      <div className="fsp-scene-actions">
                                        <button
                                          type="button"
                                          className={`fsp-continuity-toggle fsp-toggle${scene.continuityEnabled ? ' is-on' : ''}`}
                                          title={t('Dùng khung cuối cảnh trước để nối liền mạch', 'Use previous scene end frame for continuity')}
                                          onClick={() => void updateScene(episode, scene, { continuityEnabled: !scene.continuityEnabled })}
                                          aria-pressed={scene.continuityEnabled}
                                        >
                                          <span className="fsp-toggle-dot" />
                                          <span className="fsp-toggle-label">{t('Nối cảnh', 'Continue')}</span>
                                        </button>
                                        <button
                                          type="button"
                                          className="fsp-btn fsp-btn-sm fsp-btn-keyframe"
                                          disabled={generatingScene === scene.id}
                                          onClick={() => void generateScene(episode, scene, 'keyframe')}
                                        >
                                          {generatingScene === scene.id ? t('Đang gửi…', 'Queuing…') : t('Tạo keyframe', 'Create keyframe')}
                                        </button>
                                        <button
                                          type="button"
                                          className="fsp-btn fsp-btn-sm fsp-btn-video"
                                          disabled={
                                            generatingScene === scene.id
                                            || !(scene.approvedKeyframe || scene.keyframeOutput)
                                          }
                                          onClick={() => void generateScene(episode, scene, 'video')}
                                        >
                                          {t('Tạo video', 'Create video')}
                                        </button>
                                        <button
                                          type="button"
                                          className="fsp-btn fsp-btn-sm"
                                          title={t('Mở trong FlowPage để chỉnh thêm', 'Open in FlowPage for fine-tuning')}
                                          onClick={() => onOpenScene({ seriesId: selected.id, episodeId: episode.id, sceneId: scene.id, artifact: scene.approvedKeyframe || scene.keyframeOutput ? 'video' : 'keyframe', seriesTitle: selected.title, episodeTitle: episode.title, sceneTitle: scene.title, scenePrompt: scene.prompt })}
                                        >
                                          ↗ {t('Flow', 'Flow')}
                                        </button>
                                        {scene.videoOutput && (
                                          <button
                                            type="button"
                                            className="fsp-btn fsp-btn-sm fsp-btn-preview-scene"
                                            title={t('Xem video', 'Preview video')}
                                            onClick={() => setSeriesPreview({
                                              url: toUrl(scene.videoOutput, scene.videoJobId),
                                              title: `${String(scene.index).padStart(3, '0')} · ${scene.title || scene.prompt.slice(0, 40)}`,
                                              kind: 'video',
                                            })}
                                          >
                                            ▶ {t('Video', 'Video')}
                                          </button>
                                        )}
                                        <button
                                          type="button"
                                          className="fsp-btn fsp-btn-sm fsp-btn-danger"
                                          title={t('Xóa cảnh này', 'Delete this scene')}
                                          onClick={() => void deleteScene(episode, scene)}
                                        >
                                          ×
                                        </button>
                                      </div>
                                    </div>
                                  </div>
                                )
                              })}

                              {/* Add scene form */}
                              <div className={`fsp-add-scene${sceneDraft.episodeId === episode.id ? ' is-active' : ''}`} onClick={() => { if (sceneDraft.episodeId !== episode.id) setSceneDraft({ ...sceneDraft, episodeId: episode.id }) }}>
                                <div className="fsp-add-scene-fields">
                                  <input
                                    value={sceneDraft.episodeId === episode.id ? sceneDraft.title : ''}
                                    onChange={(e) => setSceneDraft({ ...sceneDraft, episodeId: episode.id, title: e.target.value })}
                                    placeholder={t('Tên cảnh', 'Scene title')}
                                  />
                                  <input
                                    value={sceneDraft.episodeId === episode.id ? sceneDraft.timecode : ''}
                                    onChange={(e) => setSceneDraft({ ...sceneDraft, episodeId: episode.id, timecode: e.target.value })}
                                    placeholder="00.00_00.00-00.00_08.00"
                                  />
                                </div>
                                <div className="fsp-add-scene-prompt-row">
                                  <textarea
                                    value={sceneDraft.episodeId === episode.id ? sceneDraft.prompt : ''}
                                    onChange={(e) => setSceneDraft({ ...sceneDraft, episodeId: episode.id, prompt: e.target.value })}
                                    placeholder={t('Prompt cảnh mới', 'New scene prompt')}
                                    rows={2}
                                  />
                                  <button type="button" className="fsp-btn fsp-btn-primary fsp-btn-add-scene" onClick={() => void addScene()} disabled={!sceneDraft.prompt.trim() || sceneDraft.episodeId !== episode.id}>
                                    + {t('Cảnh', 'Scene')}
                                  </button>
                                </div>
                              </div>
                            </div>
                          )}
                        </article>
                      )
                    })}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* ── Output preview modal (reusing FlowPage styles) ── */}
      {seriesPreview && (
        <div
          className="flow-preview-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setSeriesPreview(null)
          }}
        >
          <section
            className="flow-preview-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={t('Xem trước kết quả', 'Output preview')}
          >
            <header>
              <div>
                <strong>
                  {seriesPreview.playlist && seriesPreview.playlist.length > 1
                    ? `[${seriesPreview.idx! + 1}/${seriesPreview.playlist.length}] ${seriesPreview.title}`
                    : seriesPreview.title}
                </strong>
                {seriesPreview.playlist && seriesPreview.playlist.length > 1 && (
                  <small>{t('Tự động chuyển cảnh khi kết thúc video', 'Auto-plays next scene on end')}</small>
                )}
              </div>
              <button
                type="button"
                onClick={() => setSeriesPreview(null)}
                aria-label={t('Đóng xem trước', 'Close preview')}
              >
                ×
              </button>
            </header>
            <div className="flow-preview-media">
              {seriesPreview.kind === 'video' ? (
                <video
                  key={seriesPreview.url}
                  src={seriesPreview.url}
                  controls
                  autoPlay
                  onEnded={() => {
                    if (
                      seriesPreview.playlist &&
                      seriesPreview.idx !== undefined &&
                      seriesPreview.idx < seriesPreview.playlist.length - 1
                    ) {
                      const nextIdx = seriesPreview.idx + 1
                      const nextItem = seriesPreview.playlist[nextIdx]
                      setSeriesPreview({
                        ...seriesPreview,
                        url: nextItem.url,
                        title: nextItem.title,
                        idx: nextIdx,
                      })
                    }
                  }}
                />
              ) : (
                <img src={seriesPreview.url} alt={seriesPreview.title} />
              )}
            </div>
            <footer>
              {seriesPreview.playlist && seriesPreview.playlist.length > 1 && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginRight: 'auto' }}>
                  <button
                    type="button"
                    disabled={seriesPreview.idx === 0}
                    onClick={() => {
                      const prevIdx = (seriesPreview.idx ?? 0) - 1
                      const item = seriesPreview.playlist![prevIdx]
                      setSeriesPreview({
                        ...seriesPreview,
                        url: item.url,
                        title: item.title,
                        idx: prevIdx,
                      })
                    }}
                  >
                    ◀ {t('Trước', 'Prev')}
                  </button>
                  <span style={{ fontSize: '0.75rem', fontWeight: 600 }}>
                    {seriesPreview.idx! + 1} / {seriesPreview.playlist.length}
                  </span>
                  <button
                    type="button"
                    disabled={seriesPreview.idx === seriesPreview.playlist.length - 1}
                    onClick={() => {
                      const nextIdx = (seriesPreview.idx ?? 0) + 1
                      const item = seriesPreview.playlist![nextIdx]
                      setSeriesPreview({
                        ...seriesPreview,
                        url: item.url,
                        title: item.title,
                        idx: nextIdx,
                      })
                    }}
                  >
                    {t('Sau', 'Next')} ▶
                  </button>
                </div>
              )}
              <a href={seriesPreview.url} download target="_blank" rel="noreferrer">
                {t('Tải về', 'Download')}
              </a>
              <button type="button" onClick={() => setSeriesPreview(null)}>
                {t('Đóng', 'Close')}
              </button>
            </footer>
          </section>
        </div>
      )}
    </section>

  )
}
