import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react'
import { toast } from 'sonner'
import { localize, useLocale } from '@/app/i18n'
import { BackTitle } from '@/shared/components/BackTitle'
import { OutputFolderField } from '@/shared/components/OutputFolderField'
import { IconBatch, IconCam, IconGear, IconVideo } from '@/shared/components/Icons'
import { useRemountState } from '@/shared/lib/useRemountState'
import './DrawingPage.css'

type PreviewTab = 'preview' | 'line-map' | 'stroke-path'
type Tool = 'pencil' | 'pen' | 'marker' | 'brush'
type Preset = 'pencil' | 'ink' | 'whiteboard' | 'speed' | 'watercolor'
type StrokeOrder = 'natural' | 'outline' | 'region' | 'reading' | 'center' | 'horizontal' | 'vertical'
type DrawingJob = {
  id: string; filename?: string; status: 'queued' | 'processing' | 'done' | 'error' | 'cancelled'
  progress: number; step: string; error?: string
}
type RenderJob = { id: string; status: 'queued' | 'processing' | 'paused' | 'done' | 'error' | 'cancelled'; progress: number; error?: string }
type TimelineScene = { index: number; text: string; start: number; end: number }

type DrawingSettings = {
  preset: Preset; mode: 'drawing' | 'hand'; tool: Tool; duration: number
  detail: number; thickness: number; fps: 24 | 30 | 60
  resolution: '720p' | '1080p' | '4k'; strokeOrder: StrokeOrder; showOriginalEnd: boolean
}

const DRAWING_SETTINGS_KEY = 'zm-tool:drawing-settings:v1'
const DRAWING_JOBS_KEY = 'zm-tool:drawing-job-ids:v1'
const DRAWING_OUTPUT_DIR_KEY = 'zm-tool:drawing-output-dir:v1'
const DEFAULT_SETTINGS: DrawingSettings = {
  preset: 'pencil', mode: 'drawing', tool: 'pencil', duration: 10, detail: 72,
  thickness: 2, fps: 30, resolution: '1080p', strokeOrder: 'natural', showOriginalEnd: true,
}

function cachedSettings(): DrawingSettings {
  try {
    const raw = window.localStorage.getItem(DRAWING_SETTINGS_KEY)
    const stored = raw ? JSON.parse(raw) as Partial<DrawingSettings> : {}
    const preset = Object.hasOwn(PRESETS, stored.preset ?? '') ? stored.preset as Preset : DEFAULT_SETTINGS.preset
    return {
      preset,
      mode: stored.mode === 'hand' ? 'hand' : stored.mode === 'drawing' ? 'drawing' : DEFAULT_SETTINGS.mode,
      tool: ['pencil', 'pen', 'marker', 'brush'].includes(stored.tool ?? '') ? stored.tool as Tool : DEFAULT_SETTINGS.tool,
      duration: Number.isFinite(stored.duration) ? Math.max(2, Math.min(60, Number(stored.duration))) : DEFAULT_SETTINGS.duration,
      detail: Number.isFinite(stored.detail) ? Math.max(10, Math.min(100, Number(stored.detail))) : DEFAULT_SETTINGS.detail,
      thickness: Number.isFinite(stored.thickness) ? Math.max(1, Math.min(8, Number(stored.thickness))) : DEFAULT_SETTINGS.thickness,
      fps: [24, 30, 60].includes(stored.fps ?? 0) ? stored.fps as 24 | 30 | 60 : DEFAULT_SETTINGS.fps,
      resolution: ['720p', '1080p', '4k'].includes(stored.resolution ?? '') ? stored.resolution as DrawingSettings['resolution'] : DEFAULT_SETTINGS.resolution,
      strokeOrder: ['natural', 'outline', 'region', 'reading', 'center', 'horizontal', 'vertical'].includes(stored.strokeOrder ?? '') ? stored.strokeOrder as StrokeOrder : DEFAULT_SETTINGS.strokeOrder,
      showOriginalEnd: typeof stored.showOriginalEnd === 'boolean' ? stored.showOriginalEnd : DEFAULT_SETTINGS.showOriginalEnd,
    }
  } catch {
    return DEFAULT_SETTINGS
  }
}

function cachedJobIds(): string[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(DRAWING_JOBS_KEY) || '[]')
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string').slice(0, 100) : []
  } catch {
    return []
  }
}

const PRESETS: Record<Preset, { duration: number; detail: number; thickness: number; tool: Tool; mode: 'drawing' | 'hand'; strokeOrder: StrokeOrder; showOriginalEnd: boolean }> = {
  pencil: { duration: 10, detail: 72, thickness: 2, tool: 'pencil', mode: 'drawing', strokeOrder: 'natural', showOriginalEnd: true },
  ink: { duration: 8, detail: 84, thickness: 2, tool: 'pen', mode: 'drawing', strokeOrder: 'outline', showOriginalEnd: false },
  whiteboard: { duration: 12, detail: 55, thickness: 3, tool: 'marker', mode: 'hand', strokeOrder: 'region', showOriginalEnd: false },
  speed: { duration: 5, detail: 68, thickness: 2, tool: 'pencil', mode: 'hand', strokeOrder: 'horizontal', showOriginalEnd: true },
  watercolor: { duration: 14, detail: 60, thickness: 4, tool: 'brush', mode: 'hand', strokeOrder: 'center', showOriginalEnd: true },
}

function artifact(job: DrawingJob | null, kind: 'input' | 'lineMap' | 'strokePath' | 'output') {
  return job ? `/api/drawing/jobs/${job.id}/${kind}?t=${job.progress}` : ''
}

function formatSrtTime(seconds: number) {
  const ms = Math.max(0, Math.round(seconds * 1000))
  const hours = Math.floor(ms / 3_600_000)
  const minutes = Math.floor(ms % 3_600_000 / 60_000)
  const secs = Math.floor(ms % 60_000 / 1000)
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')},${String(ms % 1000).padStart(3, '0')}`
}

function scenesFromScript(value: string, secondsPerScene: number): TimelineScene[] {
  const srtBlocks = value.trim().split(/\n\s*\n/).filter(Boolean)
  const srt = srtBlocks.map((block, index) => {
    const lines = block.trim().split(/\r?\n/).filter(Boolean)
    const timing = lines.find((line) => line.includes('-->'))
    if (!timing) return null
    const [from, to] = timing.split('-->').map((part) => part.trim().replace(',', '.'))
    const time = (part: string) => {
      const bits = part.split(':').map(Number)
      return bits.length === 3 ? bits[0] * 3600 + bits[1] * 60 + bits[2] : 0
    }
    const start = time(from); const end = time(to)
    const text = lines.slice(lines.indexOf(timing) + 1).join(' ').trim()
    return text && end > start ? { index: index + 1, text, start, end } : null
  }).filter((item): item is TimelineScene => Boolean(item))
  if (srt.length) return srt
  return value.split(/\n\s*\n|\r?\n/).map((line) => line.trim()).filter(Boolean).map((text, index) => ({ index: index + 1, text, start: index * secondsPerScene, end: (index + 1) * secondsPerScene }))
}

function scenesToSrt(scenes: TimelineScene[]) {
  return scenes.map((scene) => `${scene.index}\n${formatSrtTime(scene.start)} --> ${formatSrtTime(scene.end)}\n${scene.text}`).join('\n\n')
}

export default function DrawingPage({ onBack }: { onBack: () => void }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [saved] = useState(cachedSettings)
  const inputRef = useRef<HTMLInputElement>(null)
  const audioRef = useRef<HTMLInputElement>(null)
  const [localPreview, setLocalPreview] = useState('')
  const [previewTab, setPreviewTab] = useState<PreviewTab>('preview')
  const [preset, setPreset] = useState<Preset>(saved.preset)
  const [mode, setMode] = useState<'drawing' | 'hand'>(saved.mode)
  const [tool, setTool] = useState<Tool>(saved.tool)
  const [duration, setDuration] = useState(saved.duration)
  const [detail, setDetail] = useState(saved.detail)
  const [thickness, setThickness] = useState(saved.thickness)
  const [fps, setFps] = useState<24 | 30 | 60>(saved.fps)
  const [resolution, setResolution] = useState<'720p' | '1080p' | '4k'>(saved.resolution)
  const [strokeOrder, setStrokeOrder] = useState<StrokeOrder>(saved.strokeOrder)
  const [showOriginalEnd, setShowOriginalEnd] = useState(saved.showOriginalEnd)
  const [outputDir, setOutputDir] = useState(() => window.localStorage.getItem(DRAWING_OUTPUT_DIR_KEY) || '')
  const [isDesktopApp, setIsDesktopApp] = useState(false)
  const [job, setJob] = useRemountState<DrawingJob | null>('drawing.job', null)
  const [batchJobs, setBatchJobs] = useRemountState<DrawingJob[]>('drawing.batchJobs', [])
  const [dragging, setDragging] = useState(false)
  const [artifactError, setArtifactError] = useState<PreviewTab | null>(null)
  const [jobsRestored, setJobsRestored] = useState(false)
  const [runRequested, setRunRequested] = useState(false)

  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [settingsTab, setSettingsTab] = useState<'drawing' | 'export'>('drawing')
  const [workspacePanel, setWorkspacePanel] = useState<'project' | 'drawing'>('drawing')
  const [script, setScript] = useState('')
  const [timelineScenes, setTimelineScenes] = useState<TimelineScene[]>([])
  const [audioFile, setAudioFile] = useState<File | null>(null)
  const [renderJob, setRenderJob] = useRemountState<RenderJob | null>('drawing.renderJob', null)
  const [rendering, setRendering] = useState(false)
  const [includeCaptions, setIncludeCaptions] = useState(true)
  const [isPortrait, setIsPortrait] = useState(false)
  const [handId, setHandId] = useState<string>(() => window.localStorage.getItem('zm-tool:drawing-hand-id:v1') || 'default')
  const [hands, setHands] = useState<Array<{ id: string; nameVi: string; nameEn: string; url: string | null; builtin: boolean }>>(
    [{ id: 'default', nameVi: 'Tay phải + bút chì', nameEn: 'Right hand + pencil', url: '/api/drawing/hands/default', builtin: true }]
  )
  const [uploadingHand, setUploadingHand] = useState(false)
  const handUploadRef = useRef<HTMLInputElement>(null)
  const allJobs = useMemo(() => [job, ...batchJobs].filter((item): item is DrawingJob => Boolean(item)), [batchJobs, job])

  const isInitialMount = useRef(true)
  useEffect(() => {
    if (isInitialMount.current) {
      isInitialMount.current = false
      return
    }
    // If settings change, mark completed/errored/cancelled jobs back to queued so they can be redrawn
    setJob(current => current && current.status !== 'processing' && current.status !== 'queued' ? { ...current, status: 'queued' } : current)
    setBatchJobs(current => current.map(j => j.status !== 'processing' && j.status !== 'queued' ? { ...j, status: 'queued' } : j))
  }, [mode, tool, strokeOrder, preset, duration, detail, thickness, fps, resolution, showOriginalEnd])

  const activeJob = useMemo(() => allJobs.find(j => j.id === selectedJobId) || allJobs[0] || null, [allJobs, selectedJobId])

  // Clear selected job if it no longer exists
  useEffect(() => {
    if (selectedJobId && !allJobs.some(j => j.id === selectedJobId)) setSelectedJobId(null)
  }, [allJobs, selectedJobId])

  // A selected source is saved as a queued backend job immediately.  Queued
  // means safely cached and ready to start; only processing blocks edits.
  const active = [job, ...batchJobs].some((item) => item?.status === 'processing')
  const queuedJobs = [job, ...batchJobs].filter((item): item is DrawingJob => item?.status === 'queued')
  const hasCachedSource = Boolean(job)
  const [savingSource, setSavingSource] = useState(false)
  const visual = useMemo(() => {
    if (previewTab === 'line-map' && activeJob && artifactError !== 'line-map') return artifact(activeJob, 'lineMap')
    if (previewTab === 'stroke-path' && activeJob && artifactError !== 'stroke-path') return artifact(activeJob, 'strokePath')
    if (activeJob) return artifact(activeJob, 'input')
    return localPreview
  }, [artifactError, activeJob, localPreview, previewTab])

  useEffect(() => () => { if (localPreview) URL.revokeObjectURL(localPreview) }, [localPreview])
  useEffect(() => {
    const ids = cachedJobIds()
    if (!ids.length) { setJobsRestored(true); return }
    void fetch('/api/drawing/jobs').then(async (response) => {
      if (!response.ok) return [] as DrawingJob[]
      const all = await response.json() as DrawingJob[]
      const byId = new Map(all.map((item) => [item.id, item]))
      return ids.map((id) => byId.get(id)).filter((item): item is DrawingJob => Boolean(item))
    }).then((restored) => {
      setJob(restored[0] ?? null)
      setBatchJobs(restored.slice(1))
    }).catch(() => undefined).finally(() => setJobsRestored(true))
  }, [])
  useEffect(() => {
    const settings: DrawingSettings = { preset, mode, tool, duration, detail, thickness, fps, resolution, strokeOrder, showOriginalEnd }
    window.localStorage.setItem(DRAWING_SETTINGS_KEY, JSON.stringify(settings))
  }, [preset, mode, tool, duration, detail, thickness, fps, resolution, strokeOrder, showOriginalEnd])
  useEffect(() => { window.localStorage.setItem(DRAWING_OUTPUT_DIR_KEY, outputDir) }, [outputDir])
  useEffect(() => { void fetch('/api/config').then(async (r) => r.ok && setIsDesktopApp(Boolean((await r.json() as { desktop?: boolean }).desktop))).catch(() => undefined) }, [])
  useEffect(() => {
    void fetch('/api/drawing/hands').then(async (r) => r.ok ? (await r.json() as { items: typeof hands }).items : hands)
      .then(setHands).catch(() => undefined)
  }, [])
  useEffect(() => { window.localStorage.setItem('zm-tool:drawing-hand-id:v1', handId) }, [handId])
  useEffect(() => {
    if (!jobsRestored) return
    const ids = [job, ...batchJobs].filter((item): item is DrawingJob => Boolean(item)).map((item) => item.id)
    window.localStorage.setItem(DRAWING_JOBS_KEY, JSON.stringify(ids))
  }, [batchJobs, job, jobsRestored])
  useEffect(() => { setArtifactError(null) }, [activeJob?.id, activeJob?.progress, previewTab])
  useEffect(() => {
    const ids = [job, ...batchJobs].filter((item): item is DrawingJob => Boolean(item && (item.status === 'processing' || (runRequested && item.status === 'queued')))).map((item) => item.id)
    if (!ids.length) return
    const timer = window.setInterval(async () => {
      const results = await Promise.all(ids.map(async (id) => {
        const response = await fetch(`/api/drawing/jobs/${id}`)
        return response.ok ? await response.json() as DrawingJob : null
      }))
      const updates = new Map(results.filter((item): item is DrawingJob => Boolean(item)).map((item) => [item.id, item]))
      if (!updates.size) return
      setJob((current) => current ? updates.get(current.id) ?? current : current)
      setBatchJobs((current) => current.map((item) => updates.get(item.id) ?? item))
    }, 800)
    return () => window.clearInterval(timer)
  }, [active, batchJobs, job?.id, runRequested])
  useEffect(() => {
    if (runRequested && [job, ...batchJobs].every((item) => !item || ['done', 'error', 'cancelled'].includes(item.status))) setRunRequested(false)
  }, [batchJobs, job, runRequested])
  useEffect(() => {
    if (!renderJob?.id || !['queued', 'processing', 'paused'].includes(renderJob.status)) return
    const timer = window.setInterval(() => {
      void fetch(`/api/srt-image/jobs/${renderJob.id}`).then(async (response) => response.ok ? await response.json() as RenderJob : null).then((next) => { if (next) setRenderJob(next) }).catch(() => undefined)
    }, 900)
    return () => window.clearInterval(timer)
  }, [renderJob?.id, renderJob?.status])

  const selectFiles = async (next: FileList | File[] | null | undefined) => {
    const accepted = Array.from(next ?? []).filter((item) => item.type.startsWith('image/'))
    if (!accepted.length) return
    setArtifactError(null)
    setPreviewTab('preview')
    setLocalPreview(URL.createObjectURL(accepted[0]))
    setSavingSource(true)
    try {
      const body = new FormData()
      accepted.forEach((item) => body.append('images', item))
      body.append('options', JSON.stringify({ preset, mode, tool, duration, detail, thickness, fps, resolution, strokeOrder, showOriginalEnd, outputDir, handId }))
      body.append('start_now', 'false')
      const response = await fetch('/api/drawing/jobs/batch', { method: 'POST', body })
      if (!response.ok) throw new Error(await response.text())
      const result = await response.json() as { jobs: DrawingJob[] }
      setJob(result.jobs[0] ?? null)
      setBatchJobs(result.jobs.slice(1))
      setRunRequested(false)
      toast.success(t('Đã thêm ảnh vào hàng đợi.', 'Image added to queue.'))
    } catch (error) {
      setJob({ id: '', status: 'error', progress: 0, step: 'error', error: error instanceof Error ? error.message : String(error) })
      setBatchJobs([])
    } finally {
      setSavingSource(false)
    }
  }
  const applyPreset = (id: Preset) => {
    const value = PRESETS[id]
    setPreset(id); setDuration(value.duration); setDetail(value.detail); setThickness(value.thickness)
    setTool(value.tool); setMode(value.mode); setStrokeOrder(value.strokeOrder); setShowOriginalEnd(value.showOriginalEnd)
  }
  const generate = async () => {
    if (!queuedJobs.length || active || savingSource) return
    try {
      const options = { preset, mode, tool, duration, detail, thickness, fps, resolution, strokeOrder, showOriginalEnd, outputDir, handId }
      const updates = await Promise.all(queuedJobs.map(async (item) => {
        const response = await fetch(`/api/drawing/jobs/${item.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options),
        })
        if (!response.ok) throw new Error(await response.text())
        return await response.json() as DrawingJob
      }))
      const response = await fetch('/api/drawing/jobs/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: updates.map((item) => item.id) }),
      })
      if (!response.ok) throw new Error(await response.text())
      setJob(updates[0] ?? null)
      setBatchJobs(updates.slice(1))
      setRunRequested(true)
      toast.success(t('Đã bắt đầu tạo video vẽ tay.', 'Started drawing video generation.'))
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setJob((current) => current ? { ...current, error: message } : { id: '', status: 'error', progress: 0, step: 'error', error: message })
      toast.error(message)
    }
  }

  const removeJob = async (jobId: string) => {
    const target = allJobs.find(j => j.id === jobId)
    if (target && (target.status === 'queued' || target.status === 'processing')) {
      await fetch(`/api/drawing/jobs/${jobId}/cancel`, { method: 'POST' }).catch(() => { })
    }
    setJob(current => current?.id === jobId ? null : current)
    setBatchJobs(current => current.filter(j => j.id !== jobId))
    if (selectedJobId === jobId) setSelectedJobId(null)
  }

  const cancelJob = async (jobId: string) => {
    await fetch(`/api/drawing/jobs/${jobId}/cancel`, { method: 'POST' })
    setJob((current) => current?.id === jobId ? { ...current, status: 'cancelled' } : current)
    setBatchJobs((current) => current.map((item) => item.id === jobId ? { ...item, status: 'cancelled' } : item))
    toast.success(t('Đã hủy tạo video.', 'Generation cancelled.'))
  }
  const pickOutputDir = async () => {
    const response = await fetch('/api/system/pick-folder', { method: 'POST' })
    if (!response.ok) throw new Error(await response.text())
    const picked = await response.json() as { path?: string }
    return picked.path || undefined
  }
  const createTimeline = () => {
    const scenes = scenesFromScript(script, duration)
    if (!scenes.length) {
      toast.error(t('Nhập kịch bản hoặc SRT trước.', 'Enter a script or SRT first.'))
      return
    }
    setTimelineScenes(scenes)
    toast.success(t(`Đã tạo ${scenes.length} cảnh timeline.`, `Created ${scenes.length} timeline scenes.`))
  }
  const renderTimeline = async () => {
    if (!timelineScenes.length) { toast.error(t('Tạo timeline trước khi render.', 'Create the timeline before rendering.')); return }
    if (timelineScenes.length !== allJobs.length) { toast.error(t('Số cảnh timeline phải bằng số ảnh nguồn.', 'Timeline scene count must match source image count.')); return }
    setRendering(true)
    try {
      const form = new FormData()
      for (const [index, item] of allJobs.entries()) {
        const response = await fetch(artifact(item, 'input'))
        if (!response.ok) throw new Error(t('Không đọc được ảnh nguồn.', 'Could not read a source image.'))
        form.append('images', new File([await response.blob()], item.filename || `scene-${index + 1}.png`, { type: response.headers.get('content-type') || 'image/png' }))
      }
      const srt = scenesToSrt(timelineScenes)
      form.append('timeline', new File([srt], 'timeline.srt', { type: 'application/x-subrip' }))
      if (includeCaptions) form.append('srt', new File([srt], 'subtitles.srt', { type: 'application/x-subrip' }))
      if (audioFile) form.append('audio', audioFile)
      form.append('output_name', 'drawing-timeline.mp4')
      const folderResponse = await fetch(`/api/system/resolve-output-folder?tab=drawing&output=${encodeURIComponent(outputDir)}`, { method: 'POST' })
      if (folderResponse.ok) {
        const folder = await folderResponse.json() as { path?: string }
        if (folder.path) form.append('output_path', `${String(folder.path).replace(/[\\/]+$/, '')}/drawing-timeline.mp4`)
      }
      form.append('options', JSON.stringify({
        resolution, fps, crf: 20, effect: 'none', transitionDuration: 0, zoom: 'off', speed: 100, volume: 100, encoder: 'auto',
        subtitleSize: 8, subtitleOffset: 0, subtitleFontFamily: 'system', subtitleMargin: 34, subtitleBackground: 'solid', subtitleColor: '#ffffff', subtitleBgColor: '#000000', subtitleOpacity: 55,
        drawing: { enabled: true, mode, tool, detail, thickness, strokeOrder, resolution },
      }))
      const response = await fetch('/api/srt-image/jobs', { method: 'POST', body: form })
      if (!response.ok) throw new Error(await response.text())
      setRenderJob(await response.json() as RenderJob)
      toast.success(t('Đã bắt đầu render timeline.', 'Timeline rendering started.'))
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setRenderJob({ id: '', status: 'error', progress: 0, error: message })
      toast.error(message)
    } finally { setRendering(false) }
  }
  const statusText = activeJob?.status === 'done' ? t('Video đã sẵn sàng', 'Video ready')
    : activeJob?.status === 'error' ? t('Không thể tạo video', 'Could not create video')
      : activeJob?.status === 'cancelled' ? t('Đã hủy', 'Cancelled')
        : activeJob?.step === 'line_map' ? t('Đang tách nét từ ảnh', 'Extracting lines from image')
          : activeJob?.step === 'stroke_path' ? t('Đang sắp thứ tự nét vẽ', 'Ordering drawing strokes')
            : activeJob?.step === 'ink' ? t('Đang vẽ từng nét', 'Drawing stroke by stroke')
              : activeJob?.step === 'color' ? t('Đang tô màu', 'Adding colour')
                : activeJob?.step === 'encoding' ? t('Đang mã hóa video', 'Encoding video')
                  : savingSource ? t('Đang lưu ảnh vào hàng đợi', 'Saving image to queue')
                    : queuedJobs.length ? t('Đã lưu, sẵn sàng tạo video', 'Saved, ready to create video')
                      : active ? t('Đang tạo video vẽ tay', 'Creating drawing video') : t('Sẵn sàng render cục bộ', 'Ready for local rendering')

  const clearAll = () => { setJob(null); setBatchJobs([]); setSelectedJobId(null); setLocalPreview('') }

  const uploadHand = async (files: FileList | null) => {
    const file = files?.[0]
    if (!file) return
    setUploadingHand(true)
    try {
      const body = new FormData()
      body.append('file', file)
      const response = await fetch('/api/drawing/hands', { method: 'POST', body })
      if (!response.ok) throw new Error(await response.text())
      const created = await response.json() as { id: string; url: string }
      setHands((prev) => [...prev, { id: created.id, nameVi: file.name.replace(/\.png$/i, ''), nameEn: file.name.replace(/\.png$/i, ''), url: created.url, builtin: false }])
      setHandId(created.id)
      toast.success(t('Đã tải lên sprite thành công.', 'Sprite uploaded.'))
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setUploadingHand(false)
      if (handUploadRef.current) handUploadRef.current.value = ''
    }
  }

  const renderQueue = (compact = false) => (
    <div className={`drawing-queue-list${compact ? ' is-compact' : ''}`}>
      {allJobs.length === 0 && !localPreview && <div className="drawing-queue-empty">{t('Chưa có ảnh nào', 'No images yet')}</div>}
      {allJobs.length === 0 && localPreview && <div className="drawing-queue-item is-active"><img src={localPreview} alt="" /><div className="drawing-queue-overlay">{t('Đang chuẩn bị...', 'Preparing...')}</div></div>}
      {allJobs.map((item, index) => {
        const isActive = selectedJobId ? item.id === selectedJobId : index === 0
        const jobStatus = item.status === 'done' ? t('Xong', 'Done') : item.status === 'cancelled' ? t('Đã hủy', 'Cancelled') : item.status === 'error' ? t('Lỗi', 'Error') : item.status === 'processing' ? `${item.progress}%` : t('Chờ', 'Queued')
        return <div key={item.id} className={`drawing-queue-item ${isActive ? 'is-active' : ''}`} onClick={() => setSelectedJobId(item.id)}>
          <img src={artifact(item, 'input')} alt="" />
          <div className="drawing-queue-meta"><span className="drawing-queue-name">{item.filename || `${t('Ảnh', 'Image')} ${index + 1}`}</span><span className={`drawing-queue-status status-${item.status}`}>{jobStatus}</span></div>
          {item.status === 'processing' && <div className="drawing-queue-progress" style={{ width: `${item.progress}%` }} />}
          <button type="button" className="drawing-queue-cancel" onClick={(event) => { event.stopPropagation(); void removeJob(item.id) }} title={t('Xóa khỏi hàng đợi', 'Remove from queue')}>×</button>
        </div>
      })}
    </div>
  )

  const renderSettings = () => <section className="drawing-studio-settings">
    <div className="drawing-settings-title"><IconGear size={18} /><h2>{t('Cài đặt & xuất', 'Settings & export')}</h2></div>
    <div className="drawing-settings-tabs" role="tablist">
      <button type="button" className={settingsTab === 'drawing' ? 'is-active' : ''} onClick={() => setSettingsTab('drawing')}>{t('Vẽ tay', 'Drawing')}</button>
      <button type="button" className={settingsTab === 'export' ? 'is-active' : ''} onClick={() => setSettingsTab('export')}>{t('Xuất video', 'Export')}</button>
    </div>
    <div className="drawing-settings-scroll">
      {settingsTab === 'drawing' ? <>
        <label className="drawing-field"><span>{t('Chế độ vẽ', 'Drawing mode')}</span><select value={mode} onChange={(event) => setMode(event.target.value as 'drawing' | 'hand')}><option value="drawing">{t('Vẽ nét liên tục', 'Continuous strokes')}</option><option value="hand">{t('Tay cầm bút vẽ nét', 'Hand drawing strokes')}</option></select></label>
        <span className="drawing-label">{t('Loại dụng cụ', 'Tool')}</span>
        <div className="drawing-tools">{(['pencil', 'pen', 'marker', 'brush'] as Tool[]).map((id) => <button key={id} type="button" className={tool === id ? 'is-active' : ''} onClick={() => setTool(id)}>{({ pencil: t('Chì', 'Pencil'), pen: t('Bút', 'Pen'), marker: 'Marker', brush: t('Cọ', 'Brush') })[id]}</button>)}</div>
        <span className="drawing-label">{t('Sprite bút vẽ', 'Pen sprite')}</span>
        <div className="drawing-hand-grid">
          {hands.map((h) => (
            <button key={h.id} type="button" title={locale === 'vi' ? h.nameVi : h.nameEn}
              className={`drawing-hand-chip${handId === h.id ? ' is-active' : ''}`}
              onClick={() => setHandId(h.id)}>
              {h.url
                ? <img src={h.url} alt={locale === 'vi' ? h.nameVi : h.nameEn} loading="lazy" />
                : <span className="drawing-hand-bare">•</span>}
              <span>{locale === 'vi' ? h.nameVi : h.nameEn}</span>
            </button>
          ))}
          <button type="button" className="drawing-hand-upload" onClick={() => handUploadRef.current?.click()} disabled={uploadingHand}>
            {uploadingHand ? '...' : '+'}
            <span>{t('Tải PNG lên', 'Upload PNG')}</span>
          </button>
          <input ref={handUploadRef} type="file" accept="image/png" className="drawing-visually-hidden" onChange={(e) => void uploadHand(e.target.files)} />
        </div>
        <label className="drawing-field"><span>{t('Đường đi nét', 'Stroke route')}</span><select value={strokeOrder} onChange={(event) => setStrokeOrder(event.target.value as StrokeOrder)}><option value="natural">{t('Tự nhiên theo đối tượng', 'Natural by object')}</option><option value="outline">{t('Theo viền thật', 'True outlines')}</option><option value="region">{t('Từng vùng hoàn chỉnh', 'Complete one region')}</option><option value="reading">{t('Theo chữ · trái sang phải', 'Text · left to right')}</option><option value="center">{t('Từ tâm lan ra', 'Centre outward')}</option><option value="horizontal">{t('Quét ngang', 'Horizontal sweep')}</option><option value="vertical">{t('Quét dọc', 'Vertical sweep')}</option></select></label>
      </> : <>
        <label className="drawing-field"><span>Preset</span><select value={preset} onChange={(event) => applyPreset(event.target.value as Preset)}><option value="pencil">{t('Chì chân dung', 'Portrait pencil')}</option><option value="ink">{t('Nét mực', 'Ink line art')}</option><option value="whiteboard">Whiteboard</option><option value="speed">{t('Vẽ nhanh', 'Speed drawing')}</option><option value="watercolor">{t('Lộ màu nước', 'Watercolor reveal')}</option></select></label>
        <Range label={t('Thời lượng', 'Duration')} value={duration} min={2} max={60} suffix={t(' giây', ' sec')} onChange={setDuration} />
        <Range label={t('Độ chi tiết', 'Detail')} value={detail} min={10} max={100} suffix="%" onChange={setDetail} />
        <Range label={t('Độ dày nét', 'Stroke thickness')} value={thickness} min={1} max={8} suffix=" px" onChange={setThickness} />
        <div className="drawing-setting-row"><label className="drawing-field"><span>FPS</span><select value={fps} onChange={(event) => setFps(Number(event.target.value) as 24 | 30 | 60)}><option value="24">24</option><option value="30">30</option><option value="60">60</option></select></label><label className="drawing-field"><span>{t('Độ phân giải', 'Resolution')}</span><select value={resolution} onChange={(event) => setResolution(event.target.value as '720p' | '1080p' | '4k')}><option value="720p">720p</option><option value="1080p">1080p</option><option value="4k">4K</option></select></label></div>
        <OutputFolderField isDesktopApp={isDesktopApp} value={outputDir} onChange={(value) => { setOutputDir(value); window.localStorage.setItem(DRAWING_OUTPUT_DIR_KEY, value) }} onChoose={isDesktopApp ? () => pickOutputDir().catch(() => undefined) : undefined} defaultPath={t('Ví dụ: du-an-01', 'Example: project-01')} appFolder="drawing" />
        <label className="drawing-toggle"><input type="checkbox" checked={showOriginalEnd} onChange={(event) => setShowOriginalEnd(event.target.checked)} /><span>{t('Hiện ảnh gốc ở cuối', 'Reveal original at end')}</span></label>
      </>}
    </div>
    <div className="drawing-settings-actions">
      <button className="drawing-generate" type="button" disabled={!hasCachedSource || !queuedJobs.length || active || savingSource} onClick={() => void generate()}>{savingSource ? t('Đang lưu ảnh…', 'Saving image…') : active ? `${Math.round(job?.progress ?? 0)}% · ${t('Đang tạo', 'Creating')}` : allJobs.length > 1 ? t(`▶ Vẽ tay hàng loạt (${allJobs.length})`, `▶ Batch draw (${allJobs.length})`) : t('▶ Tạo video vẽ tay', '▶ Create drawing video')}</button>
      {(active || queuedJobs.length > 0) && <button className="drawing-cancel" type="button" onClick={() => allJobs.filter((item): item is DrawingJob => Boolean(item && (item.status === 'queued' || item.status === 'processing'))).forEach((item) => void cancelJob(item.id))}>{t('Hủy tất cả', 'Cancel all')}</button>}
      {activeJob?.status === 'done' && <a className="drawing-download" href={artifact(activeJob, 'output')} download>{t('Tải video MP4', 'Download MP4 video')}</a>}
    </div>
  </section>

  return <main className="drawing-page drawing-studio">
    <aside className="drawing-studio-rail" aria-label={t('Điều hướng Vẽ tay', 'Drawing navigation')}><nav><button id="drawing-nav-project" type="button" className={workspacePanel === 'project' ? 'is-active' : ''} onClick={() => setWorkspacePanel('project')} title={t('Dự án vẽ tay', 'Drawing project')}><IconBatch size={19} /><span>{t('Dự án', 'Project')}</span></button><button id="drawing-nav-drawing" type="button" className={workspacePanel === 'drawing' ? 'is-active' : ''} onClick={() => setWorkspacePanel('drawing')} title={t('Không gian vẽ tay', 'Drawing workspace')}><IconVideo size={19} /><span>{t('Vẽ tay', 'Drawing')}</span></button></nav></aside>
    <section className="drawing-studio-workspace">
      <header className="drawing-studio-head"><div><BackTitle onBack={onBack}>{t('Vẽ tay', 'Drawing')}</BackTitle></div><button type="button" className="drawing-head-action" onClick={() => setWorkspacePanel(workspacePanel === 'project' ? 'drawing' : 'project')}>{workspacePanel === 'project' ? <><IconVideo size={16} /> {t('Mở vẽ tay', 'Open drawing')}</> : <><IconBatch size={16} /> {t('Về dự án', 'Project')}</>}</button></header>
      <div className="drawing-studio-body">
        <section className="drawing-studio-main">
          {workspacePanel === 'project' ? <>
            <section className="drawing-project-card">
              <div className="drawing-section-heading"><span>01</span><div><h2>{t('Nội dung & tư liệu', 'Content & assets')}</h2><p>{t('Dán kịch bản hoặc SRT, rồi chọn ảnh nguồn theo đúng thứ tự cảnh.', 'Paste a script or SRT, then select source images in scene order.')}</p></div></div>
              <div className="drawing-project-inputs"><label><span>{t('Kịch bản hoặc SRT', 'Script or SRT')}</span><textarea value={script} onChange={(event) => setScript(event.target.value)} placeholder={t('Mỗi đoạn trống là một cảnh; có thể dán nguyên SRT.', 'Each paragraph is a scene; you can paste full SRT.')} /><button type="button" className="drawing-open-workspace" onClick={createTimeline}>{t('Tạo timeline', 'Create timeline')}</button></label><div className="drawing-project-options"><Range label={t('Thời lượng mỗi cảnh', 'Duration per scene')} value={duration} min={2} max={60} suffix={t(' giây', ' sec')} onChange={setDuration} /><label><span>{t('Audio lời thoại (tuỳ chọn)', 'Narration audio (optional)')}</span><button type="button" className="drawing-file-button" onClick={() => audioRef.current?.click()}>{audioFile?.name || t('Chọn audio', 'Choose audio')}</button><input ref={audioRef} className="drawing-visually-hidden" type="file" accept="audio/*" onChange={(event) => setAudioFile(event.target.files?.[0] ?? null)} /></label><label className="drawing-toggle"><input type="checkbox" checked={includeCaptions} onChange={(event) => setIncludeCaptions(event.target.checked)} /><span>{t('Nhúng phụ đề từ timeline', 'Burn captions from timeline')}</span></label></div></div>
              <button className={`drawing-dropzone${dragging ? ' is-dragging' : ''}`} type="button" onClick={() => inputRef.current?.click()} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={(event: DragEvent<HTMLButtonElement>) => { event.preventDefault(); setDragging(false); void selectFiles(event.dataTransfer.files) }}><IconCam size={20} /><strong>{t('Chọn hoặc thả ảnh vào đây', 'Choose or drop images here')}</strong><small>{t('JPG, PNG, WebP, BMP', 'JPG, PNG, WebP, BMP')}</small></button><input ref={inputRef} className="drawing-visually-hidden" type="file" multiple accept="image/jpeg,image/png,image/webp,image/bmp" onChange={(event) => void selectFiles(event.target.files)} /><div className="drawing-project-assets"><div><strong>{t('Ảnh nguồn', 'Source images')}</strong><span>{allJobs.length ? t(`${allJobs.length} ảnh đã chọn`, `${allJobs.length} selected`) : t('Chưa có ảnh', 'No images')}</span></div>{allJobs.length > 0 && <button type="button" className="drawing-text-action" onClick={clearAll}>{t('Xóa tất cả', 'Clear all')}</button>}</div>{renderQueue(true)}</section>
            <section className="drawing-project-card drawing-project-timeline"><div className="drawing-section-heading"><span>02</span><div><h2>{t('Timeline cảnh', 'Scene timeline')}</h2><p>{timelineScenes.length ? t(`${timelineScenes.length} cảnh · ${allJobs.length} ảnh nguồn`, `${timelineScenes.length} scenes · ${allJobs.length} source images`) : t('Tạo timeline từ kịch bản/SRT để kiểm tra số cảnh trước khi render.', 'Create a timeline from script/SRT to check scenes before rendering.')}</p></div></div>{timelineScenes.length > 0 && <ol className="drawing-scene-list">{timelineScenes.map((scene) => <li key={scene.index}><b>{String(scene.index).padStart(2, '0')}</b><span>{scene.text}</span><small>{scene.end - scene.start}s</small></li>)}</ol>}<div className="drawing-project-render"><button type="button" className="drawing-open-workspace" onClick={() => setWorkspacePanel('drawing')}><IconVideo size={18} />{t('Xem vẽ tay', 'Open drawing')}</button><button type="button" className="drawing-generate" disabled={rendering || !timelineScenes.length || timelineScenes.length !== allJobs.length} onClick={() => void renderTimeline()}>{rendering ? t('Đang chuẩn bị…', 'Preparing…') : renderJob?.status === 'processing' ? `${Math.round(renderJob.progress)}% · ${t('Đang render', 'Rendering')}` : t('▶ Render toàn bộ MP4', '▶ Render full MP4')}</button></div>{renderJob?.status === 'done' && (isDesktopApp ? <button type="button" className="drawing-open-workspace" onClick={() => void fetch(`/api/srt-image/open-folder?job_id=${renderJob.id}`, { method: 'POST' })}>{t('Mở thư mục', 'Open folder')}</button> : <a className="drawing-download" href={`/api/srt-image/jobs/${renderJob.id}/file`} download>{t('Tải video MP4', 'Download MP4 video')}</a>)}{renderJob?.error && <p className="drawing-error">{renderJob.error}</p>}</section>
          </> : <div className="drawing-workbench"><section className="drawing-card drawing-queue-card"><div className="drawing-queue-header"><div><h2>{t('Hàng đợi', 'Queue')}</h2>{allJobs.length > 0 && <button type="button" className="drawing-text-action" onClick={clearAll}>{t('Xóa tất cả', 'Clear all')}</button>}</div><button className={`drawing-dropzone${dragging ? ' is-dragging' : ''}`} type="button" onClick={() => inputRef.current?.click()} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={(event: DragEvent<HTMLButtonElement>) => { event.preventDefault(); setDragging(false); void selectFiles(event.dataTransfer.files) }}><b>＋</b><strong>{t('Thêm ảnh', 'Add images')}</strong></button><input ref={inputRef} className="drawing-visually-hidden" type="file" multiple accept="image/jpeg,image/png,image/webp,image/bmp" onChange={(event) => void selectFiles(event.target.files)} /></div>{renderQueue()}</section><section className={`drawing-card drawing-stage-card ${isPortrait ? 'is-portrait-layout' : ''}`}><div className="drawing-preview-tabs" role="tablist">{([{ id: 'preview', vi: 'Xem trước', en: 'Preview' }, { id: 'line-map', vi: 'Bản đồ nét', en: 'Line Map' }, { id: 'stroke-path', vi: 'Đường nét', en: 'Stroke Path' }] as const).map((item) => <button key={item.id} id={`drawing-tab-${item.id}`} type="button" role="tab" aria-selected={previewTab === item.id} className={previewTab === item.id ? 'is-active' : ''} onClick={() => setPreviewTab(item.id)}>{t(item.vi, item.en)}</button>)}</div><div className="drawing-stage-content"><div className={`drawing-canvas drawing-canvas--${previewTab}`}>{visual ? <img src={visual} alt={t('Xem trước ảnh vẽ', 'Drawing image preview')} onLoad={(event) => setIsPortrait(event.currentTarget.naturalHeight > event.currentTarget.naturalWidth * 1.7)} onError={() => { if (previewTab !== 'preview') setArtifactError(previewTab) }} /> : <div>{t('Chọn một ảnh để bắt đầu', 'Choose an image to begin')}</div>}</div>{activeJob && <div className="drawing-stage-meta"><div className="drawing-progress"><span style={{ width: `${activeJob.progress ?? 0}%` }} /></div><div className="drawing-stats"><div><span>{t('Trạng thái', 'Status')}</span><strong>{statusText}</strong></div><div><span>{t('Chế độ', 'Mode')}</span><strong>{mode === 'hand' ? t('Tay + bút', 'Hand + pen') : t('Vẽ nét', 'Stroke drawing')}</strong></div><div><span>{t('Ước tính', 'Estimate')}</span><strong>{duration}s · {fps} FPS</strong></div></div>{activeJob.status === 'done' && <video className="drawing-result" controls src={artifact(activeJob, 'output')} />}{activeJob.error && <p className="drawing-error">{activeJob.error}</p>}</div>}</div></section></div>}
        </section>
        {renderSettings()}
      </div>
      <footer className="drawing-pipeline">{t('Pipeline: Ảnh → Line/Stroke → Sắp thứ tự nét → Animation → Tay/Bút (tùy chọn) → Màu → MP4', 'Pipeline: Image → Line/Stroke → Stroke order → Animation → Hand/Pen (optional) → Color → MP4')}</footer>
    </section>
  </main>
}

function Range({ label, value, min, max, suffix, onChange }: { label: string; value: number; min: number; max: number; suffix: string; onChange: (value: number) => void }) {
  return <label className="drawing-range"><span><b>{label}</b><em>{value}{suffix}</em></span><input type="range" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} /></label>
}
