import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { localize, useLocale } from '@/app/i18n'
import './FlowSeriesPanel.css'
import {
  type SeriesArtifact, type FlowSeriesSceneContext, type SeriesGenSettings,
  type FlowSeriesAccount as FlowAccount, type SeriesRun, type AutoMode,
  type Scene, type Episode, type Asset, type Series,
  VIDEO_MODELS, IMAGE_MODELS,
  SERIES_SETTINGS_KEY, SERIES_SELECTED_ID_KEY, SERIES_TAB_KEY,
  SERIES_AUTO_MODE_KEY, SERIES_AUTO_APPROVE_KEY, SERIES_COLLAPSED_EPISODES_KEY, SERIES_AI_KEY,
  normalizeSeries, seriesRequest as request, sceneStatusMeta,
  readSeriesSettings, toUrl, countSeriesScript,
} from '@/features/flow/flowSeries.helpers'
import { chatProviderUsable, normalizeChatProviders, type ChatProviderOption } from '@/features/chat/chatProviders'

export type { SeriesArtifact, FlowSeriesSceneContext }


export default function FlowSeriesPanel({ onOpenScene, onGenerateAnchor, accounts = [] }: {
  onOpenScene: (context: FlowSeriesSceneContext) => void
  onGenerateAnchor: (seriesId: string, prompt: string) => Promise<string>
  accounts?: FlowAccount[]
}) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [items, setItems] = useState<Series[]>([])
  const [selectedId, setSelectedId] = useState(() => {
    try { return localStorage.getItem(SERIES_SELECTED_ID_KEY) || '' } catch { return '' }
  })
  const [selected, setSelected] = useState<Series | null>(null)
  const [title, setTitle] = useState('')
  const [creating, setCreating] = useState(false)
  const [topic, setTopic] = useState('')
  const [draft, setDraft] = useState<{ text: string; bible: string } | null>(null)
  const [drafting, setDrafting] = useState(false)
  const [aiProviders, setAiProviders] = useState<ChatProviderOption[]>([])
  const [aiLoading, setAiLoading] = useState(true)
  const [aiConfig, setAiConfig] = useState<{ provider: string; model: string }>(() => {
    try { return { provider: '', model: '', ...JSON.parse(localStorage.getItem(SERIES_AI_KEY) || '{}') } } catch { return { provider: '', model: '' } }
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
      duration: saved.duration || '4',
      resolution: /^\d{3,4}p$/i.test(String(saved.resolution || '')) ? saved.resolution : '360p',
      quality: saved.quality || '360p',
      concurrency: saved.concurrency || '3',
    }
  })

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
  const selectedAccount = accounts.find((account) => account.id === seriesSettings.accountId) || accounts[0]
  const videoSection = selectedAccount?.capabilityStatus === 'verified' ? selectedAccount.capabilityCatalog?.video : undefined
  const imageSection = selectedAccount?.capabilityStatus === 'verified' ? selectedAccount.capabilityCatalog?.image : undefined
  const videoModelOptions = videoSection?.models.map((item) => item.name) || [...VIDEO_MODELS]
  const imageModelOptions = imageSection?.models.map((item) => item.name) || [...IMAGE_MODELS]
  const selectedVideoCapability = videoSection?.models.find((item) => item.name === seriesSettings.model)
  const seriesRatioOptions = selectedVideoCapability?.ratios.length ? selectedVideoCapability.ratios : ['16:9', '9:16']
  const seriesDurationOptions = selectedVideoCapability?.durations.length
    ? selectedVideoCapability.durations
    : (/omni.*flash/i.test(seriesSettings.model) ? ['4', '6', '8', '10'] : [])

  const seriesResolutionOptions = (selectedVideoCapability?.resolutions || [])
    .filter((value) => /^\d{3,4}p$/i.test(value))

  useEffect(() => {
    if (!videoSection?.models.length) return
    setSeriesSettings((current) => {
      const selectedModel = videoSection.models.find((item) => item.name === current.model)
        || videoSection.models.find((item) => item.name === videoSection.defaultModel)
        || videoSection.models[0]
      const ratio = selectedModel.ratios.includes(current.ratio) ? current.ratio : selectedModel.defaultRatio || selectedModel.ratios[0] || current.ratio
      const duration = selectedModel.durations.length
        ? (selectedModel.durations.includes(current.duration) ? current.duration : selectedModel.defaultDuration || selectedModel.durations[0] || current.duration)
        : ''
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
      const nextQuality = preferFast && (!current.quality || current.quality === '720p')
        ? '360p'
        : (current.quality || '720p')
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
    try { localStorage.setItem(SERIES_AI_KEY, JSON.stringify(aiConfig)) } catch {}
  }, [aiConfig])

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
            duration: seriesSettings.duration,
            resolution: seriesSettings.resolution || '360p',
            quality: seriesSettings.quality || (/360p/i.test(seriesSettings.resolution) ? '360p' : '720p'),
            concurrency: seriesSettings.concurrency || '1',
          },
          imageModel,
          autoApprove: true,
          mode: autoMode,
        }),
      })
      setActiveRun({ runId: raw.runId, status: raw.status, total: raw.total || 0, done: 0, currentSceneId: '', currentStep: '', errors: [] })
      toast.success(t(`Đã đẩy ${raw.enqueued || raw.total || ''} cảnh vào Hàng đợi Flow.`, `Enqueued ${raw.enqueued || raw.total || ''} scenes into Flow Queue.`))
      void refresh(selected.id)
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
            duration: seriesSettings.duration,
            resolution: seriesSettings.resolution || '360p',
            quality: seriesSettings.quality || (/360p/i.test(seriesSettings.resolution) ? '360p' : '720p'),
            count: 1,
          },
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
              duration: seriesSettings.duration,
              resolution: seriesSettings.resolution || '360p',
              quality: seriesSettings.quality || (/360p/i.test(seriesSettings.resolution) ? '360p' : '720p'),
              count: 1,
            },
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
    if (!activeRun || !selected || ['done', 'done_with_errors', 'failed', 'cancelled'].includes(activeRun.status)) return
    const t2 = window.setInterval(() => {
      void request<Series>(`/series/${selected.id}`)
        .then((fresh) => setSelected(normalizeSeries(fresh)))
        .catch(() => {/* ignore transient errors */})
    }, 6000)
    return () => window.clearInterval(t2)
  }, [activeRun?.runId, activeRun?.status, selected?.id])

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
      const result = await request<{ text: string; bible: string }>('/series/draft', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, provider: aiConfig.provider, model: aiConfig.model }),
      })
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
      void request<{ status: string }>(`/jobs/${anchorJobId}`).then((job) => {
        if (!['done', 'failed', 'cancelled'].includes(job.status)) return
        setAnchorJobId('')
        void refresh(selected.id)
        toast[job.status === 'done' ? 'success' : 'error'](job.status === 'done'
          ? t('Ảnh neo đã sẵn sàng.', 'Anchor image is ready.')
          : t('Không thể tạo ảnh neo.', 'Could not generate the anchor image.'))
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
                <button type="button" className="fsp-btn fsp-btn-primary fsp-create-go" onClick={() => void draftWithAi()} disabled={!topic.trim() || !aiConfig.provider || drafting}>
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
                ['settings', t('Cài đặt tạo', 'Generation settings')],
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
                        value={seriesDurationOptions.includes(seriesSettings.duration) ? seriesSettings.duration : seriesDurationOptions[0]}
                        onChange={(e) => saveSeriesSettings({ duration: e.target.value })}
                        aria-label={t('Thời lượng', 'Duration')}
                        disabled={seriesDurationOptions.length < 2}
                      >
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
                          quality: /360p/i.test(e.target.value) ? '360p' : (seriesSettings.quality || '720p'),
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
                        value={seriesSettings.quality || '360p'}
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
                        <option value="full">{t('Keyframe + Video', 'Keyframe + Video')}</option>
                        <option value="keyframes_only">{t('Chỉ tạo Keyframe', 'Keyframes only')}</option>
                        <option value="videos_only">{t('Chỉ tạo Video', 'Videos only')}</option>
                      </select>
                    </div>
                  </div>

                </div>
              </div>
            )}

            {/* ── Tab: Anchor images ── */}
            {activeTab === 'assets' && (
              <div className="fsp-tab-content fsp-assets">
                <div className="fsp-bible">
                  <label className="fsp-field">
                    <span className="fsp-label">{t('Series Bible', 'Series Bible')}</span>
                    <textarea
                      value={selected.bible}
                      onChange={(e) => setSelected({ ...selected, bible: e.target.value })}
                      placeholder={t('Nhân vật, skin, đạo cụ, phong cách không được thay đổi…', 'Character, skin, props, and style that must not change…')}
                      rows={6}
                    />
                  </label>
                  <label className="fsp-field">
                    <span className="fsp-label">{t('Mô tả', 'Description')}</span>
                    <textarea
                      value={selected.description}
                      onChange={(e) => setSelected({ ...selected, description: e.target.value })}
                      rows={3}
                    />
                  </label>
                </div>
                <div className="fsp-assets-head">
                  <p className="fsp-assets-hint">{t('Tối đa 3 ảnh theo thứ tự: nhân vật → đạo cụ/bối cảnh → bổ sung. Ảnh khóa luôn được dùng.', 'Up to 3 images in order: character → prop/background → extra. Locked images are always used.')}</p>
                  <p className="fsp-assets-hint">{t('Phim xuyên suốt: khóa ảnh nhân vật (vd. Tom, Jerry) và bật Nối cảnh. Cảnh đầu tạo keyframe từ ảnh khóa; mỗi video sau bắt đầu từ khung cuối của video trước (Khung hình — Veo và Omni Flash).', 'Film continuity: lock character images (e.g. Tom, Jerry) and keep Continue on. The first scene gets a keyframe from the locked images; every later video starts from the previous video\'s last frame (Frames — Veo and Omni Flash).')}</p>
                  <button type="button" className="fsp-btn fsp-btn-secondary" onClick={() => assetInput.current?.click()}>
                    + {t('Thêm ảnh', 'Add image')}
                  </button>
                  <input ref={assetInput} hidden type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => void uploadAsset(e.target.files?.[0])} />
                </div>
                <div className="fsp-anchor-create">
                  <textarea value={anchorPrompt} onChange={(e) => setAnchorPrompt(e.target.value)} rows={3} placeholder={t('Prompt tạo ảnh neo: mô tả nhân vật, trang phục, đạo cụ và phong cách cần giữ cố định…', 'Anchor image prompt: describe the character, wardrobe, props, and style to keep consistent…')} />
                  <button type="button" className="fsp-btn fsp-btn-primary" onClick={() => void generateAnchor()} disabled={!anchorPrompt.trim() || Boolean(anchorJobId)}>
                    {anchorJobId ? t('Đang tạo ảnh neo…', 'Generating anchor image…') : t('Tạo ảnh neo', 'Generate anchor image')}
                  </button>
                </div>
                {selected.assets.length === 0 ? (
                  <div className="fsp-asset-empty">
                    <span>🖼️</span>
                    <p>{t('Chưa có ảnh neo. Bạn vẫn có thể tạo keyframe bằng prompt.', 'No anchor image yet. You can still create a keyframe from the prompt.')}</p>
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
                </div>

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
                                    {thumb ? (
                                      <div
                                        className="fsp-scene-thumb"
                                        title={scene.videoOutput ? t('Xem video cảnh', 'Preview scene video') : t('Xem ảnh', 'Preview image')}
                                        onClick={(e) => {
                                          e.stopPropagation()
                                          if (scene.videoOutput) {
                                            setSeriesPreview({ url: toUrl(scene.videoOutput, scene.videoJobId), title: scene.title || `Cảnh ${scene.index}`, kind: 'video' })
                                          } else {
                                            setSeriesPreview({ url: toUrl(thumb), title: scene.title || `Cảnh ${scene.index}`, kind: 'image' })
                                          }
                                        }}
                                      >
                                        <img src={toUrl(thumb)} alt={scene.title} loading="lazy" />
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
