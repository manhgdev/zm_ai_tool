import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { applyEngineProfile, defaultSettings } from '@/app/appSettings'
import { localize, useLocale } from '@/app/i18n'
import type { ProjectSettings } from '@/features/project/project.types'
import { CloneBatchSettingsPanel } from '@/features/studio/CloneBatchSettingsPanel'
import { studioApi, type QueueJob } from '@/features/studio/studio.api'
import { AudioSlider, CaptionModePicker, ReviewLangFields, ReviewLeftPanel, ReviewRightPanel, useVoices } from '@/features/studio/ReviewSettingsPanel'
import { BackTitle } from '@/shared/components/BackTitle'
import { OutputFolderField } from '@/shared/components/OutputFolderField'
import { IconArrowRight, IconGear } from '@/shared/components/Icons'
import { MediaPreviewModal } from '@/shared/components/MediaPreviewModal'
import { DEFAULT_REVIEW_SETTINGS, STYLE_TO_PIPE, type ReviewSettings } from '@/features/studio/reviewSettings'
import './StudioPages.css'
import './FilmPage.css'

type Props = {
  onBack: () => void
  onOpenEditor?: (projectId: string) => void
  onOpenReviewProjects: () => void
}

type BatchTab = 'all' | 'clone' | 'review' | 'drawing'
type DrawingJob = { id: string; filename: string; status: 'queued' | 'processing' | 'done' | 'error' | 'cancelled'; progress: number; error?: string; options?: Partial<DrawingBatchOptions> }
type DrawingPreset = 'pencil' | 'ink' | 'whiteboard' | 'speed' | 'watercolor'
type DrawingStrokeOrder = 'natural' | 'outline' | 'region' | 'reading' | 'center' | 'horizontal' | 'vertical'
type DrawingBatchOptions = { preset: DrawingPreset; duration: number; detail: number; thickness: number; fps: 24 | 30 | 60; resolution: '720p' | '1080p' | '4k'; mode: 'drawing' | 'hand'; tool: 'pencil' | 'pen' | 'marker' | 'brush'; strokeOrder: DrawingStrokeOrder; showOriginalEnd: boolean }
const BATCH_TAB_LS = 'zm_ai_tool.batchTab'
const BATCH_CLONE_SETTINGS_LS = 'zm_ai_tool.batchCloneSettings'
const BATCH_CLONE_SETTINGS_VERSION_LS = 'zm_ai_tool.batchCloneSettingsVersion'
const BATCH_CLONE_SETTINGS_VERSION = '3'
const BATCH_REVIEW_SETTINGS_LS = 'zm_ai_tool.batchReviewSettings'
const BATCH_DRAWING_SETTINGS_LS = 'zm_ai_tool.batchDrawingSettings'
const BATCH_OUTPUT_DIR_LS = 'zm_ai_tool.batchOutputDir'

function loadBatchTab(): BatchTab {
  try {
    const saved = localStorage.getItem(BATCH_TAB_LS)
    return saved === 'all' || saved === 'review' || saved === 'drawing' ? saved : 'clone'
  } catch {
    return 'clone'
  }
}

function loadDrawingBatchSettings(): DrawingBatchOptions {
  const fallback: DrawingBatchOptions = { preset: 'pencil', duration: 10, detail: 72, thickness: 2, fps: 30, resolution: '1080p', mode: 'drawing', tool: 'pencil', strokeOrder: 'natural', showOriginalEnd: true }
  try {
    const saved = JSON.parse(localStorage.getItem(BATCH_DRAWING_SETTINGS_LS) || '{}') as Partial<DrawingBatchOptions>
    return { ...fallback, ...saved }
  } catch {
    return fallback
  }
}

function loadBatchReviewSettings(): ReviewSettings {
  try {
    const raw = localStorage.getItem(BATCH_REVIEW_SETTINGS_LS)
    const parsed = raw ? JSON.parse(raw) as Partial<ReviewSettings> : null
    return { ...DEFAULT_REVIEW_SETTINGS, ...parsed }
  } catch {
    return DEFAULT_REVIEW_SETTINGS
  }
}

/**
 * Batch jobs need a snapshot which is independent from the settings used by
 * the regular Clone Video page. The first separate Batch version intentionally
 * starts from clean Clone defaults: audio filtering is off and all pipeline
 * controls are usable before a user chooses an option.
 */
function loadBatchCloneSettings(): ProjectSettings {
  const fallback: ProjectSettings = {
    ...defaultSettings,
    // Batch should preserve the source video by default. Captions are an
    // explicit output choice, rather than an implicit burn-in on every job.
    burnSubs: false,
    engineProfiles: { ...defaultSettings.engineProfiles },
  }
  try {
    // Version 1 was copied from the regular Clone settings, which could carry
    // a "no translation" state and make unrelated Batch fields appear locked.
    if (localStorage.getItem(BATCH_CLONE_SETTINGS_VERSION_LS) !== BATCH_CLONE_SETTINGS_VERSION) {
      return fallback
    }
    const raw = localStorage.getItem(BATCH_CLONE_SETTINGS_LS)
    if (!raw) return fallback.engine === 'subtitle' ? applyEngineProfile(fallback, 'whisper') : fallback
    const saved = JSON.parse(raw) as Partial<ProjectSettings>
    const merged = {
      ...fallback,
      ...saved,
      engineProfiles: { ...fallback.engineProfiles, ...saved.engineProfiles },
    }
    return merged.engine === 'subtitle' ? applyEngineProfile(merged, 'whisper') : merged
  } catch {
    return fallback.engine === 'subtitle' ? applyEngineProfile(fallback, 'whisper') : fallback
  }
}

export default function BatchPage({ onBack, onOpenEditor, onOpenReviewProjects }: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [tab, setTab] = useState<BatchTab>(loadBatchTab)
  const [sources, setSources] = useState<string[]>([])
  const [outputDir, setOutputDir] = useState(() => localStorage.getItem(BATCH_OUTPUT_DIR_LS) || '')
  const [recursive, setRecursive] = useState(true)
  const [overwrite, setOverwrite] = useState('rename')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [cloneSettings, setCloneSettings] = useState<ProjectSettings>(loadBatchCloneSettings)
  const [reviewSettings, setReviewSettings] = useState<ReviewSettings>(loadBatchReviewSettings)
  const [drawingSettings, setDrawingSettings] = useState<DrawingBatchOptions>(loadDrawingBatchSettings)
  const [drawingJobs, setDrawingJobs] = useState<DrawingJob[]>([])
  const [drawingPreview, setDrawingPreview] = useState<DrawingJob | null>(null)
  const [queuePreview, setQueuePreview] = useState<QueueJob | null>(null)
  const [drawingSettingsOpen, setDrawingSettingsOpen] = useState(false)
  const [editingDrawingJobId, setEditingDrawingJobId] = useState<string | null>(null)
  const [editingQueueJob, setEditingQueueJob] = useState<QueueJob | null>(null)
  const drawingInputRef = useRef<HTMLInputElement>(null)
  const [jobs, setJobs] = useState<QueueJob[]>([])
  const [pauseAll, setPauseAll] = useState(false)
  const [error, setError] = useState('')
  const [isDesktopApp, setIsDesktopApp] = useState(false)
  const cloneVoices = useVoices(cloneSettings.targetLang === 'none' ? 'vi' : cloneSettings.targetLang)
  const voices = useVoices(reviewSettings.language)
  const tabJobs = tab === 'all' ? jobs : jobs.filter((job) => job.type === tab)
  const readyQueueJobs = tabJobs.filter((job) => job.status === 'paused')
  const readyDrawingJobs = drawingJobs.filter((job) => job.status === 'queued')
  const hasActiveJobs = jobs.some((job) => job.status === 'running' || job.status === 'queued') || drawingJobs.some((job) => job.status === 'queued' || job.status === 'processing')

  const setReview = (patch: Partial<ReviewSettings>) => setReviewSettings((cur) => ({ ...cur, ...patch }))
  const setClone = (next: ProjectSettings) => setCloneSettings(next)
  const selectTab = (next: BatchTab) => {
    setTab(next)
    setSettingsOpen(false)
    setDrawingSettingsOpen(false)
  }

  useEffect(() => {
    try { localStorage.setItem(BATCH_TAB_LS, tab) } catch { /* private mode */ }
  }, [tab])

  useEffect(() => {
    try { localStorage.setItem(BATCH_REVIEW_SETTINGS_LS, JSON.stringify(reviewSettings)) } catch {}
  }, [reviewSettings])

  useEffect(() => {
    try {
      localStorage.setItem(BATCH_CLONE_SETTINGS_LS, JSON.stringify(cloneSettings))
      localStorage.setItem(BATCH_CLONE_SETTINGS_VERSION_LS, BATCH_CLONE_SETTINGS_VERSION)
    } catch {}
  }, [cloneSettings])

  useEffect(() => {
    try { localStorage.setItem(BATCH_DRAWING_SETTINGS_LS, JSON.stringify(drawingSettings)) } catch {}
  }, [drawingSettings])

  async function refresh() {
    const snap = await studioApi.queue()
    setJobs(snap.jobs || [])
    setPauseAll(Boolean(snap.pauseAll))
    const drawing = await fetch('/api/drawing/jobs').then(async (response) => response.ok ? await response.json() as DrawingJob[] : []).catch(() => [] as DrawingJob[])
    setDrawingJobs(drawing)
  }

  useEffect(() => {
    void refresh()
    if (!hasActiveJobs) return
    const timer = window.setInterval(() => void refresh().catch(() => undefined), 3000)
    return () => window.clearInterval(timer)
  }, [hasActiveJobs])
  useEffect(() => { void fetch('/api/config').then(async (response) => response.ok && setIsDesktopApp(Boolean((await response.json() as { desktop?: boolean }).desktop))).catch(() => undefined) }, [])

  useEffect(() => {
    if (!cloneVoices.length || cloneVoices.some((voice) => voice.id === cloneSettings.defaultVoice)) return
    setClone({ ...cloneSettings, defaultVoice: cloneVoices[0].id })
  }, [cloneVoices])

  function queueSettings() {
    return tab === 'review'
      ? {
        outputDir, overwrite, naming: '{name}_review',
        style: STYLE_TO_PIPE[reviewSettings.scriptStyle],
        durationSec: reviewSettings.chunkMinutes * 60,
        buildMode: reviewSettings.buildMode,
        chunkMinutes: reviewSettings.chunkMinutes,
        keepSec: reviewSettings.keepSec,
        skipSec: reviewSettings.skipSec,
        originalAudioPct: reviewSettings.originalAudioPct,
        voice: reviewSettings.voice,
        genre: reviewSettings.genre,
        notes: reviewSettings.notes,
        reviewMode: reviewSettings.reviewMode,
        reviewModel: reviewSettings.reviewModel,
        reviewCloudModel: reviewSettings.reviewCloudModel,
        reviewProvider: reviewSettings.reviewProvider,
        narration: reviewSettings.narration,
        pausePace: reviewSettings.pausePace,
        captionMode: reviewSettings.captionMode,
        ratio: '16:9', language: reviewSettings.language, sourceLang: reviewSettings.sourceLang, recognitionEngine: reviewSettings.recognitionEngine, spoiler: 'none',
        subtitle: true, headless: true,
      }
      : {
        ...cloneSettings,
        engine: cloneSettings.engine === 'subtitle' ? 'whisper' : cloneSettings.engine,
        previewSec: 0,
        runPreviewSec: 0,
        subtitleSource: undefined,
        exportOutputDir: undefined,
        lutAssetId: '',
        hiddenLogoTexts: [],
        outputDir,
        overwrite,
        naming: '{name}_{type}',
      }
  }

  async function enqueueSources(selectedSources: string[]) {
    if (!selectedSources.length) return
    setError('')
    try {
      await studioApi.enqueue(tab === 'review' ? 'review' : 'clone', selectedSources, queueSettings(), recursive, false)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function addFiles() {
    const res = await studioApi.pickVideos()
    const selected = [...new Set(res.paths || [])]
    if (editingQueueJob) setSources(selected)
    else await enqueueSources(selected)
  }

  async function addFolder() {
    const res = await studioApi.pickFolder()
    if (!res.path) return
    if (editingQueueJob) setSources([res.path])
    else await enqueueSources([res.path])
  }

  async function addToQueue() {
    setError('')
    try {
      const settings = queueSettings()
      if (editingQueueJob) {
        await studioApi.updateJobSettings(editingQueueJob.id, { ...settings, source: editingQueueJob.source })
        setEditingQueueJob(null)
      } else await enqueueSources(sources)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function enqueueDrawingFiles(files: File[]) {
    if (!files.length) return
    setError('')
    try {
      const body = new FormData()
      files.forEach((file) => body.append('images', file))
      body.append('options', JSON.stringify(drawingSettings))
      body.append('start_now', 'false')
      const response = await fetch('/api/drawing/jobs/batch', { method: 'POST', body })
      if (!response.ok) throw new Error(await response.text())
      const result = await response.json() as { jobs: DrawingJob[] }
      setDrawingJobs((current) => [...result.jobs, ...current])
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function saveDrawingJob() {
    if (!editingDrawingJobId) return
    setError('')
    try {
      const response = await fetch(`/api/drawing/jobs/${editingDrawingJobId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(drawingSettings) })
      if (!response.ok) throw new Error(await response.text())
      const updated = await response.json() as DrawingJob
      setDrawingJobs((current) => current.map((item) => item.id === updated.id ? updated : item))
      setEditingDrawingJobId(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function runQueueJobs() {
    if (!readyQueueJobs.length) return
    setError('')
    try {
      await Promise.all(readyQueueJobs.map((job) => studioApi.jobAction(job.id, 'resume')))
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function runDrawingJobs() {
    if (!readyDrawingJobs.length) return
    setError('')
    try {
      const response = await fetch('/api/drawing/jobs/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: readyDrawingJobs.map((job) => job.id) }),
      })
      if (!response.ok) throw new Error(await response.text())
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function cancelDrawingJob(jobId: string) {
    await fetch(`/api/drawing/jobs/${jobId}/cancel`, { method: 'POST' })
    await refresh()
  }
  async function deleteDrawingJob(jobId: string) {
    setError('')
    const response = await fetch(`/api/drawing/jobs/${jobId}`, { method: 'DELETE' })
    if (!response.ok) {
      const err = await response.text()
      setError(err)
      toast.error(err)
      await refresh()
      return
    }
    setDrawingPreview((current) => current?.id === jobId ? null : current)
    setDrawingJobs((current) => current.filter((item) => item.id !== jobId))
    toast.success(t('Đã xóa job vẽ tay khỏi hàng đợi.', 'Drawing job deleted from queue.'))
  }

  async function deleteQueueJob(jobId: string) {
    try {
      await studioApi.jobAction(jobId, 'remove')
      if (editingQueueJob?.id === jobId) setEditingQueueJob(null)
      await refresh()
      toast.success(t('Đã xóa job khỏi hàng đợi.', 'Job deleted from queue.'))
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    }
  }

  async function editQueueJob(job: QueueJob) {
    if (job.status === 'queued') await studioApi.jobAction(job.id, 'pause')
    setEditingQueueJob(job)
    setSources([job.source])
    setOutputDir(String(job.settings_snapshot?.outputDir || job.outputDir || ''))
    if (job.type === 'review') setReviewSettings((current) => ({ ...current, ...(job.settings_snapshot || {}) as Partial<ReviewSettings> }))
    else setCloneSettings((current) => ({ ...current, ...(job.settings_snapshot || {}) as Partial<ProjectSettings> }))
    setSettingsOpen(true)
  }

  function editDrawingJob(job: DrawingJob) {
    if (job.status !== 'queued') return
    setDrawingSettings((current) => ({ ...current, ...(job.options || {}) }))
    setEditingDrawingJobId(job.id)
    setDrawingSettingsOpen(true)
  }

  function applyDrawingPreset(preset: DrawingPreset) {
    const next: Record<DrawingPreset, Partial<DrawingBatchOptions>> = {
      pencil: { duration: 10, detail: 72, thickness: 2, fps: 30, mode: 'drawing', tool: 'pencil', strokeOrder: 'natural', showOriginalEnd: true },
      ink: { duration: 8, detail: 84, thickness: 2, fps: 30, mode: 'drawing', tool: 'pen', strokeOrder: 'outline', showOriginalEnd: false },
      whiteboard: { duration: 12, detail: 55, thickness: 3, fps: 30, mode: 'hand', tool: 'marker', strokeOrder: 'region', showOriginalEnd: false },
      speed: { duration: 5, detail: 68, thickness: 2, fps: 60, mode: 'hand', tool: 'pencil', strokeOrder: 'horizontal', showOriginalEnd: true },
      watercolor: { duration: 14, detail: 60, thickness: 4, fps: 30, mode: 'hand', tool: 'brush', strokeOrder: 'center', showOriginalEnd: true },
    }
    setDrawingSettings((current) => ({ ...current, ...next[preset], preset }))
  }

  return (
    <div className="studio-page batch-page">
      <header>
        <div>
          <BackTitle onBack={onBack}>{t('Hàng loạt', 'Batch')}</BackTitle>
          <p>{t('Mỗi tab hiển thị hàng đợi riêng.', 'Each tab shows its own queue.')}</p>
        </div>
        {tab !== 'drawing' ? <div className="studio-actions">
          <button type="button" onClick={() => void studioApi.globalAction(pauseAll ? 'resume_all' : 'pause_all').then(refresh)}>
            {pauseAll ? t('Tiếp tục tất cả', 'Resume all') : t('Tạm dừng tất cả', 'Pause all')}
          </button>
          <button type="button" onClick={() => void studioApi.globalAction('retry_failed').then(refresh)}>{t('Thử lại lỗi', 'Retry failed')}</button>
          <button type="button" onClick={() => void studioApi.globalAction('clear_completed').then(refresh)}>{t('Xóa đã xong', 'Clear completed')}</button>
        </div> : null}
      </header>
      <div className="studio-tabs" role="tablist" aria-label={t('Loại hàng loạt', 'Batch type')}>
        <button type="button" role="tab" aria-selected={tab === 'all'} className={`batch-tab batch-tab--all${tab === 'all' ? ' active' : ''}`} onClick={() => selectTab('all')}>{t('Tất cả', 'All')}</button>
        <button type="button" role="tab" aria-selected={tab === 'clone'} className={`batch-tab batch-tab--clone${tab === 'clone' ? ' active' : ''}`} onClick={() => selectTab('clone')}>{t('Clone hàng loạt', 'Clone batch')}</button>
        <button type="button" role="tab" aria-selected={tab === 'review'} className={`batch-tab batch-tab--review${tab === 'review' ? ' active' : ''}`} onClick={() => selectTab('review')}>{t('Review hàng loạt', 'Review batch')}</button>
        <button type="button" role="tab" aria-selected={tab === 'drawing'} className={`batch-tab batch-tab--drawing${tab === 'drawing' ? ' active' : ''}`} onClick={() => selectTab('drawing')}>{t('Vẽ tay hàng loạt', 'Drawing batch')}</button>
      </div>
      {tab === 'all' ? <>
        <section className="studio-card">
          <div className="studio-card-heading">
            <div><h2>{t('Tất cả hàng đợi', 'All queues')}</h2><p className="muted">{t('Clone, Review và Vẽ tay trong một danh sách.', 'Clone, Review, and Drawing in one list.')}</p></div>
            <span className="drawing-queue-count">{jobs.length + drawingJobs.length}</span>
          </div>
          <div className="studio-queue-scroll">
          <table className="studio-table studio-queue-table studio-queue-table--all">
            <colgroup>
              <col className="studio-queue-type-column" />
              <col className="studio-queue-thumbnail-column" />
              <col className="studio-queue-source-column" />
              <col className="studio-queue-status-column" />
              <col className="studio-queue-progress-column" />
              <col className="studio-queue-actions-column" />
            </colgroup>
            <thead><tr><th>{t('Loại', 'Type')}</th><th>{t('Ảnh xem trước', 'Thumbnail')}</th><th>{t('Nguồn', 'Source')}</th><th>{t('Trạng thái', 'Status')}</th><th>{t('Tiến độ', 'Progress')}</th><th className="studio-queue-actions-heading">{t('Thao tác', 'Actions')}</th></tr></thead>
            <tbody>
              {jobs.map((job) => <tr key={job.id}><td>{job.type === 'review' ? t('Review', 'Review') : t('Clone', 'Clone')}</td><td>{job.status === 'done' ? <button type="button" className="queue-thumbnail" onClick={() => setQueuePreview(job)} aria-label={t('Xem trước video đã hoàn thành', 'Preview completed video')}><img loading="lazy" src={studioApi.thumbnailUrl(job.id)} alt="" /></button> : <div className="queue-thumbnail" aria-hidden="true"><span>▶</span></div>}</td><td title={job.source}>{job.source.split(/[/\\]/).pop()}</td><td>{job.status} · {job.stage}</td><td>{Math.round((job.progress || 0) * 100)}%</td><td className="studio-job-actions">{job.status === 'running' || job.status === 'queued' ? <><button type="button" className="batch-action batch-action--pause" onClick={() => void studioApi.jobAction(job.id, 'pause').then(refresh)}>{t('Dừng', 'Pause')}</button><button type="button" className="batch-action batch-action--danger" onClick={() => void studioApi.jobAction(job.id, 'cancel').then(refresh)}>{t('Hủy', 'Cancel')}</button></> : null}{job.status === 'paused' || job.status === 'interrupted' ? <button type="button" className="batch-action batch-action--resume" onClick={() => void studioApi.jobAction(job.id, 'resume').then(refresh)}>{t('Tiếp tục', 'Resume')}</button> : null}{job.status === 'failed' || job.status === 'cancelled' ? <button type="button" className="batch-action batch-action--retry" onClick={() => void studioApi.jobAction(job.id, 'retry').then(refresh)}>{t('Thử lại', 'Retry')}</button> : null}{job.status !== 'running' && job.status !== 'done' ? <button type="button" className="batch-action batch-action--edit" onClick={() => void editQueueJob(job)}>{t('Sửa', 'Edit')}</button> : null}{job.status === 'done' ? <><button type="button" className="batch-action batch-action--view" onClick={() => setQueuePreview(job)}>{t('Xem trước', 'Preview')}</button>{isDesktopApp ? <button type="button" className="batch-action batch-action--open" onClick={() => void studioApi.revealJob(job.id).catch((e) => setError(e instanceof Error ? e.message : String(e)))}>{t('Mở thư mục', 'Open folder')}</button> : <a className="batch-action batch-action--open" href={studioApi.fileUrl(job.id, { download: true })} download>{t('Tải xuống', 'Download')}</a>}{job.projectId && onOpenEditor ? <button type="button" className="batch-action batch-action--edit" onClick={() => onOpenEditor(job.projectId!)}>{t('Mở Editor', 'Open Editor')}</button> : null}</> : null}<button type="button" className="studio-job-delete batch-action batch-action--danger" onClick={() => void deleteQueueJob(job.id)}>{t('Xóa', 'Delete')}</button></td></tr>)}
              {drawingJobs.map((job) => <tr key={job.id}><td>{t('Vẽ tay', 'Drawing')}</td><td><button type="button" className="queue-thumbnail" onClick={() => setDrawingPreview(job)}><img loading="lazy" src={`/api/drawing/jobs/${job.id}/input`} alt={t(`Ảnh nguồn ${job.filename}`, `Source image ${job.filename}`)} /></button></td><td title={job.filename}>{job.filename}</td><td>{job.status}</td><td>{job.progress}%</td><td className="studio-job-actions"><button type="button" className="batch-action batch-action--view" onClick={() => setDrawingPreview(job)}>{t('Xem trước', 'Preview')}</button>{job.status === 'queued' ? <button type="button" className="batch-action batch-action--edit" onClick={() => editDrawingJob(job)}>{t('Sửa', 'Edit')}</button> : null}{job.status === 'done' ? <>{isDesktopApp ? <button type="button" className="batch-action batch-action--open" onClick={() => void fetch(`/api/drawing/jobs/${job.id}/reveal`, { method: 'POST' }).then(async (r) => { if (!r.ok) throw new Error(await r.text()) }).catch((e) => setError(e instanceof Error ? e.message : String(e)))}>{t('Mở thư mục', 'Open folder')}</button> : null}<a className="batch-action batch-action--open" href={`/api/drawing/jobs/${job.id}/output`} download>{t('Tải MP4', 'Download MP4')}</a></> : null}{job.status === 'queued' || job.status === 'processing' ? <button type="button" className="batch-action batch-action--danger" onClick={() => void cancelDrawingJob(job.id)}>{t('Hủy', 'Cancel')}</button> : null}<button type="button" className="studio-job-delete batch-action batch-action--danger" onClick={() => void deleteDrawingJob(job.id)}>{t('Xóa', 'Delete')}</button></td></tr>)}
            </tbody>
          </table>
          </div>
          {!jobs.length && !drawingJobs.length ? <p className="muted">{t('Chưa có job.', 'No jobs yet.')}</p> : null}
        </section>
      </> : tab === 'drawing' ? <>
        <section className="studio-card drawing-batch-setup">
          {editingDrawingJobId ? (
            <div className="drawing-batch-title">
              <div>
                <h2>{t('Sửa job Vẽ tay', 'Edit Drawing job')}</h2>
                <p>{t('Chỉnh cấu hình rồi lưu trước khi job bắt đầu.', 'Update settings and save before this job starts.')}</p>
              </div>
              <span>{t('Đang sửa', 'Editing')}</span>
            </div>
          ) : null}
          <input ref={drawingInputRef} type="file" className="drawing-visually-hidden" multiple accept="image/jpeg,image/png,image/webp,image/bmp" onChange={(event) => { const files = Array.from(event.target.files || []).filter((file) => file.type.startsWith('image/')); event.currentTarget.value = ''; void enqueueDrawingFiles(files) }} />
          <div className="drawing-batch-controls">
            <button type="button" className="drawing-upload-action" onClick={() => drawingInputRef.current?.click()}>{t('＋ Thêm ảnh', '+ Add images')}</button>
            <label><span>{t('Thời lượng', 'Duration')}</span><select value={drawingSettings.duration} onChange={(event) => setDrawingSettings((current) => ({ ...current, duration: Number(event.target.value) }))}><option value="5">5s</option><option value="10">10s</option><option value="20">20s</option><option value="30">30s</option></select></label>
            <label><span>{t('Độ phân giải', 'Resolution')}</span><select value={drawingSettings.resolution} onChange={(event) => setDrawingSettings((current) => ({ ...current, resolution: event.target.value as DrawingBatchOptions['resolution'] }))}><option value="720p">720p</option><option value="1080p">1080p</option><option value="4k">4K</option></select></label>
            <label><span>{t('Kiểu vẽ', 'Drawing style')}</span><select value={drawingSettings.mode} onChange={(event) => setDrawingSettings((current) => ({ ...current, mode: event.target.value as DrawingBatchOptions['mode'] }))}><option value="drawing">{t('Vẽ nét', 'Strokes')}</option><option value="hand">{t('Tay + bút', 'Hand + pen')}</option></select></label>
            <button type="button" className={`studio-settings-toggle${drawingSettingsOpen ? ' open' : ''}`} aria-expanded={drawingSettingsOpen} aria-controls="drawing-batch-settings" onClick={() => setDrawingSettingsOpen((open) => !open)}><IconGear size={15} />{t('Cài đặt vẽ tay', 'Drawing settings')}<IconArrowRight size={15} className="studio-settings-chevron" /></button>
            {editingDrawingJobId ? <button type="button" className="primary drawing-run-action" onClick={() => void saveDrawingJob()}>{t('Lưu chỉnh sửa', 'Save changes')}</button> : <button type="button" className="primary drawing-run-action" disabled={!readyDrawingJobs.length} onClick={() => void runDrawingJobs()}>{t(`Chạy ${readyDrawingJobs.length} job`, `Run ${readyDrawingJobs.length} jobs`)}</button>}
          </div>
          {!editingDrawingJobId ? <p className="muted">{t('Thêm ảnh rồi bấm Chạy để xử lý hàng loạt.', 'Add images, then press Run to process the batch.')}</p> : null}
          {drawingSettingsOpen ? <section id="drawing-batch-settings" className="drawing-batch-settings"><div className="drawing-batch-settings-heading"><div><h3>{t('Cài đặt áp dụng cho cả lô', 'Settings applied to the entire batch')}</h3><p>{t('Thay đổi sẽ áp dụng cho các job tạo sau khi bấm Chạy.', 'Changes apply to jobs created when you press Run.')}</p></div><button type="button" onClick={() => applyDrawingPreset('pencil')}>{t('Đặt lại', 'Reset')}</button></div><div className="drawing-batch-settings-grid"><label><span>Preset</span><select value={drawingSettings.preset} onChange={(event) => applyDrawingPreset(event.target.value as DrawingPreset)}><option value="pencil">{t('Chì chân dung', 'Portrait pencil')}</option><option value="ink">{t('Nét mực', 'Ink line art')}</option><option value="whiteboard">Whiteboard</option><option value="speed">{t('Vẽ nhanh', 'Speed drawing')}</option><option value="watercolor">{t('Lộ màu nước', 'Watercolor reveal')}</option></select></label><label><span>{t('Dụng cụ', 'Tool')}</span><select value={drawingSettings.tool} onChange={(event) => setDrawingSettings((current) => ({ ...current, tool: event.target.value as DrawingBatchOptions['tool'] }))}><option value="pencil">{t('Chì', 'Pencil')}</option><option value="pen">{t('Bút', 'Pen')}</option><option value="marker">Marker</option><option value="brush">{t('Cọ', 'Brush')}</option></select></label><label><span>{t('Đường đi nét', 'Stroke route')}</span><select value={drawingSettings.strokeOrder} onChange={(event) => setDrawingSettings((current) => ({ ...current, strokeOrder: event.target.value as DrawingStrokeOrder }))}><option value="natural">{t('Tự nhiên theo đối tượng', 'Natural by object')}</option><option value="outline">{t('Theo viền thật', 'True outlines')}</option><option value="region">{t('Từng vùng hoàn chỉnh', 'Complete one region')}</option><option value="reading">{t('Theo chữ · trái sang phải', 'Text · left to right')}</option><option value="center">{t('Từ tâm lan ra', 'Centre outward')}</option><option value="horizontal">{t('Quét ngang', 'Horizontal sweep')}</option><option value="vertical">{t('Quét dọc', 'Vertical sweep')}</option></select></label><label><span>{t('FPS', 'FPS')}</span><select value={drawingSettings.fps} onChange={(event) => setDrawingSettings((current) => ({ ...current, fps: Number(event.target.value) as DrawingBatchOptions['fps'] }))}><option value="24">24 FPS</option><option value="30">30 FPS</option><option value="60">60 FPS</option></select></label><label><span>{t('Độ chi tiết', 'Detail')} · {drawingSettings.detail}%</span><input type="range" min="10" max="100" value={drawingSettings.detail} onChange={(event) => setDrawingSettings((current) => ({ ...current, detail: Number(event.target.value) }))} /></label><label><span>{t('Độ dày nét', 'Stroke thickness')} · {drawingSettings.thickness}px</span><input type="range" min="1" max="8" value={drawingSettings.thickness} onChange={(event) => setDrawingSettings((current) => ({ ...current, thickness: Number(event.target.value) }))} /></label></div><div className="drawing-batch-checks"><label className="check"><input type="checkbox" checked={drawingSettings.showOriginalEnd} onChange={(event) => setDrawingSettings((current) => ({ ...current, showOriginalEnd: event.target.checked }))} />{t('Hiện ảnh gốc ở cuối', 'Reveal original at end')}</label></div></section> : null}
          {error ? <p className="studio-error">{error}</p> : null}
        </section>
        <section className="studio-card drawing-batch-queue">
          <div className="studio-card-heading"><div><h2>{t('Hàng đợi Vẽ tay', 'Drawing queue')}</h2><p className="muted">{t('Xem trước ảnh nguồn khi chờ, hoặc video sau khi hoàn thành.', 'Preview the source while queued, or the video after it finishes.')}</p></div><span className="drawing-queue-count">{drawingJobs.length}</span></div>
          <div className="drawing-job-list">{drawingJobs.map((item) => <article className={`drawing-job drawing-job--${item.status}`} key={item.id}><button type="button" className="drawing-job-thumb" onClick={() => setDrawingPreview(item)} aria-label={t(`Xem trước ${item.filename}`, `Preview ${item.filename}`)}><img loading="lazy" src={`/api/drawing/jobs/${item.id}/input`} alt="" /></button><div className="drawing-job-main"><strong title={item.filename}>{item.filename}</strong><div className="drawing-job-meta"><span>{item.status === 'done' ? t('Hoàn thành', 'Completed') : item.status === 'queued' ? t('Đang chờ', 'Queued') : item.status === 'processing' ? t('Đang vẽ', 'Drawing') : item.status === 'cancelled' ? t('Đã hủy', 'Cancelled') : t('Lỗi', 'Error')}</span><b>{item.progress}%</b></div><div className="drawing-job-progress"><i style={{ width: `${item.progress}%` }} /></div>{item.error ? <small>{item.error}</small> : null}</div><div className="drawing-job-actions"><button type="button" className="batch-action batch-action--view" onClick={() => setDrawingPreview(item)}>{t('Xem trước', 'Preview')}</button>{item.status === 'queued' ? <button type="button" className="batch-action batch-action--edit" onClick={() => editDrawingJob(item)}>{t('Sửa', 'Edit')}</button> : null}{item.status === 'done' ? <>{isDesktopApp ? <button type="button" className="batch-action batch-action--open" onClick={() => void fetch(`/api/drawing/jobs/${item.id}/reveal`, { method: 'POST' }).then(async (r) => { if (!r.ok) throw new Error(await r.text()) }).catch((e) => setError(e instanceof Error ? e.message : String(e)))}>{t('Mở thư mục', 'Open folder')}</button> : null}<a className="batch-action batch-action--open" href={`/api/drawing/jobs/${item.id}/output`} download>{t('Tải MP4', 'Download MP4')}</a></> : null}{item.status === 'queued' || item.status === 'processing' ? <button type="button" className="batch-action batch-action--danger" onClick={() => void cancelDrawingJob(item.id)}>{t('Hủy', 'Cancel')}</button> : null}<button type="button" className="batch-action batch-action--danger" onClick={() => void deleteDrawingJob(item.id)}>{t('Xóa', 'Delete')}</button></div></article>)}</div>
          {!drawingJobs.length ? <div className="drawing-empty-queue">{t('Chưa có job. Thêm ảnh để đưa ngay vào hàng đợi.', 'No jobs yet. Add images to place them in the queue immediately.')}</div> : null}
        </section>
      </> : <>
      <section className="studio-card">
        <div className="studio-actions">
          <button type="button" onClick={() => void addFiles()}>{t('Thêm file', 'Add files')}</button>
          <button type="button" onClick={() => void addFolder()}>{t('Thêm thư mục', 'Add folder')}</button>
          <button
            type="button"
            className={`studio-settings-toggle${settingsOpen ? ' open' : ''}`}
            aria-expanded={settingsOpen}
            aria-controls="batch-settings-panel"
            onClick={() => setSettingsOpen((open) => !open)}
          >
            <IconGear size={15} />
            {tab === 'review'
              ? t('Cài đặt Review hàng loạt', 'Review batch settings')
              : t('Cài đặt Clone hàng loạt', 'Clone batch settings')}
            <IconArrowRight size={15} className="studio-settings-chevron" />
          </button>
          <button type="button" className="primary" disabled={!readyQueueJobs.length} onClick={() => void runQueueJobs()}>{t(`Chạy ${readyQueueJobs.length} job`, `Run ${readyQueueJobs.length} jobs`)}</button>
          {editingQueueJob ? <button type="button" className="primary" onClick={() => void addToQueue()}>{t('Lưu chỉnh sửa', 'Save changes')}</button> : null}
          {editingQueueJob ? <button type="button" onClick={() => setEditingQueueJob(null)}>{t('Hủy sửa', 'Cancel edit')}</button> : null}
        </div>
        <p className="muted">{editingQueueJob ? `${outputDir || t('Xuất mặc định vào project', 'Default output is the project folder')} · ${sources.length} ${t('nguồn', 'sources')}` : t('Chọn file hoặc thư mục để tạo job ngay trong hàng đợi bên dưới, rồi bấm Chạy.', 'Choose files or a folder to create jobs in the queue below, then press Run.')}</p>
        {sources.length ? <ul className="studio-files">{sources.map((s) => <li key={s}>{s}</li>)}</ul> : null}
        {error ? <p className="studio-error">{error}</p> : null}
      </section>
      {settingsOpen ? (
        <div id="batch-settings-panel" className="studio-settings-panel">
          <section className="studio-card studio-settings-common">
            <h2>{t('Cài đặt đầu ra', 'Output settings')}</h2>
            <div className="studio-actions">
              <label className="check"><input type="checkbox" checked={recursive} onChange={(e) => setRecursive(e.target.checked)} /> {t('Quét đệ quy', 'Recursive scan')}</label>
              <select aria-label={t('Xử lý file trùng', 'Existing file handling')} value={overwrite} onChange={(e) => setOverwrite(e.target.value)}>
                <option value="rename">{t('Đổi tên nếu trùng', 'Auto rename')}</option>
                <option value="skip">{t('Bỏ qua file có sẵn', 'Skip existing')}</option>
                <option value="overwrite">{t('Ghi đè', 'Overwrite')}</option>
              </select>
            </div>
            <OutputFolderField isDesktopApp={isDesktopApp} value={outputDir} onChange={(value) => { setOutputDir(value); localStorage.setItem(BATCH_OUTPUT_DIR_LS, value) }} onChoose={isDesktopApp ? async () => (await studioApi.pickFolder()).path || undefined : undefined} defaultPath={t('Ví dụ: du-an-01', 'Example: project-01')} appFolder={tab === 'review' ? 'review' : 'clone'} label={t('Thư mục xuất', 'Output folder')} />
          </section>
          {tab === 'review' ? (
            <div className="rv-page rv-embed">
              <div className="rv-grid">
                <section className="rv-card">
                  <div className="rv-card-title">
                    <h2>{t('Cấu hình Review hàng loạt', 'Batch review setup')}</h2>
                    <button type="button" className="rv-reset" onClick={() => { setReviewSettings(DEFAULT_REVIEW_SETTINGS); toast.info(t('Đã đặt lại cấu hình.', 'Settings reset.')) }}>↻ {t('Đặt lại', 'Reset')}</button>
                  </div>
                  <p className="rv-hint">{t('Cài đặt bên dưới sẽ áp dụng cho tất cả video được thêm vào hàng đợi review hàng loạt.', 'The settings below apply to every video added to the batch review queue.')}</p>
                  <ReviewLangFields settings={reviewSettings} onChange={setReview} />
                  <ReviewLeftPanel settings={reviewSettings} onChange={setReview} />
                  <AudioSlider value={reviewSettings.originalAudioPct} onChange={(v) => setReview({ originalAudioPct: v })} />
                  <CaptionModePicker value={reviewSettings.captionMode} onChange={(v) => setReview({ captionMode: v })} />
                </section>
                <section className="rv-card">
                  <ReviewRightPanel settings={reviewSettings} onChange={setReview} voices={voices} />
                </section>
              </div>
            </div>
          ) : (
            <CloneBatchSettingsPanel settings={cloneSettings} voices={cloneVoices} onChange={setClone} />
          )}
        </div>
      ) : null}
      <section className="studio-card">
        <div className="studio-card-heading">
          <h2>{tab === 'clone' ? t('Hàng đợi Clone hàng loạt', 'Clone batch queue') : t('Hàng đợi Review hàng loạt', 'Review batch queue')}</h2>
          {tab === 'review' ? (
            <button type="button" className="studio-projects-link" onClick={onOpenReviewProjects}>
              {t('Dự án của bạn', 'Your projects')} →
            </button>
          ) : null}
        </div>
        <div className="studio-queue-scroll">
        <table className="studio-table studio-queue-table studio-queue-table--feature">
          <colgroup>
            <col className="studio-queue-thumbnail-column" />
            <col className="studio-queue-source-column" />
            <col className="studio-queue-status-column" />
            <col className="studio-queue-progress-column" />
            <col className="studio-queue-actions-column" />
          </colgroup>
          <thead>
            <tr>
              <th>{t('Ảnh xem trước', 'Thumbnail')}</th>
              <th>{t('Nguồn', 'Source')}</th>
              <th>{t('Trạng thái', 'Status')}</th>
              <th>{t('Tiến độ', 'Progress')}</th>
              <th className="studio-queue-actions-heading">{t('Thao tác', 'Actions')}</th>
            </tr>
          </thead>
          <tbody>
            {tabJobs.map((job) => (
              <tr key={job.id}>
                <td>
                  {job.status === 'done' ? (
                    <button type="button" className="queue-thumbnail" onClick={() => setQueuePreview(job)} aria-label={t('Xem trước video đã hoàn thành', 'Preview completed video')}>
                      <img loading="lazy" src={studioApi.thumbnailUrl(job.id)} alt="" />
                    </button>
                  ) : <div className="queue-thumbnail" aria-hidden="true"><span>▶</span></div>}
                </td>
                <td title={job.source}>{job.source.split(/[/\\]/).pop()}</td>
                <td>{job.status} · {job.stage}</td>
                <td>{Math.round((job.progress || 0) * 100)}%</td>
                <td className="studio-job-actions">
                  {job.status === 'running' || job.status === 'queued' ? (
                    <>
                      <button type="button" className="batch-action batch-action--pause" onClick={() => void studioApi.jobAction(job.id, 'pause').then(refresh)}>{t('Dừng', 'Pause')}</button>
                      <button type="button" className="batch-action batch-action--danger" onClick={() => void studioApi.jobAction(job.id, 'cancel').then(refresh)}>{t('Hủy', 'Cancel')}</button>
                    </>
                  ) : null}
                  {job.status === 'paused' || job.status === 'interrupted' ? (
                    <button type="button" className="batch-action batch-action--resume" onClick={() => void studioApi.jobAction(job.id, 'resume').then(refresh)}>{t('Tiếp tục', 'Resume')}</button>
                  ) : null}
                  {job.status === 'failed' || job.status === 'cancelled' ? (
                    <button type="button" className="batch-action batch-action--retry" onClick={() => void studioApi.jobAction(job.id, 'retry').then(refresh)}>{t('Thử lại', 'Retry')}</button>
                  ) : null}
                  {job.status !== 'running' && job.status !== 'done' ? (
                    <button type="button" className="batch-action batch-action--edit" onClick={() => void editQueueJob(job)}>{t('Sửa', 'Edit')}</button>
                  ) : null}
                  {job.status === 'done' ? <button type="button" className="batch-action batch-action--view" onClick={() => setQueuePreview(job)}>{t('Xem trước', 'Preview')}</button> : null}
                  {job.status === 'done' ? (isDesktopApp ? <button type="button" className="batch-action batch-action--open" onClick={() => void studioApi.revealJob(job.id).catch((e) => setError(e instanceof Error ? e.message : String(e)))}>{t('Mở thư mục', 'Open folder')}</button> : <a className="batch-action batch-action--open" href={studioApi.fileUrl(job.id, { download: true })} download>{t('Tải xuống', 'Download')}</a>) : null}
                  {job.status === 'done' && job.projectId && onOpenEditor ? (
                    <button type="button" className="batch-action batch-action--edit" onClick={() => onOpenEditor(job.projectId!)}>{t('Mở Editor', 'Open Editor')}</button>
                  ) : null}
                  <button type="button" className="studio-job-delete batch-action batch-action--danger" onClick={() => void deleteQueueJob(job.id)}>{t('Xóa', 'Delete')}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {!tabJobs.length ? <p className="muted">{t('Chưa có job.', 'No jobs yet.')}</p> : null}
      </section>
      </>}
      <MediaPreviewModal
        open={Boolean(drawingPreview)}
        onClose={() => setDrawingPreview(null)}
        item={
          drawingPreview
            ? {
                id: drawingPreview.id,
                title: drawingPreview.filename,
                subtitle:
                  drawingPreview.status === 'done'
                    ? t('Video vẽ tay đã xuất', 'Rendered drawing video')
                    : t('Ảnh nguồn của job đang chọn', 'Source image for the selected job'),
                src:
                  drawingPreview.status === 'done'
                    ? `/api/drawing/jobs/${drawingPreview.id}/output`
                    : `/api/drawing/jobs/${drawingPreview.id}/input`,
                downloadUrl:
                  drawingPreview.status === 'done'
                    ? `/api/drawing/jobs/${drawingPreview.id}/output`
                    : undefined,
                downloadFilename: `${drawingPreview.filename.replace(/\.[^.]+$/, '')}_drawn.mp4`,
                type: drawingPreview.status === 'done' ? 'video' : 'image',
              }
            : null
        }
        totalCount={drawingJobs.length}
        currentIndex={drawingPreview ? drawingJobs.findIndex((j) => j.id === drawingPreview.id) : -1}
        onPrevious={() => {
          if (!drawingPreview || drawingJobs.length <= 1) return
          const idx = drawingJobs.findIndex((j) => j.id === drawingPreview.id)
          if (idx >= 0) {
            const prev = (idx - 1 + drawingJobs.length) % drawingJobs.length
            setDrawingPreview(drawingJobs[prev])
          }
        }}
        onNext={() => {
          if (!drawingPreview || drawingJobs.length <= 1) return
          const idx = drawingJobs.findIndex((j) => j.id === drawingPreview.id)
          if (idx >= 0) {
            const next = (idx + 1) % drawingJobs.length
            setDrawingPreview(drawingJobs[next])
          }
        }}
        onReveal={
          isDesktopApp && drawingPreview && drawingPreview.status === 'done'
            ? async () => {
                await fetch(`/api/drawing/jobs/${drawingPreview.id}/reveal`, { method: 'POST' })
              }
            : undefined
        }
        onDelete={
          drawingPreview
            ? async () => {
                const targetId = drawingPreview.id
                if (drawingJobs.length > 1) {
                  const idx = drawingJobs.findIndex((j) => j.id === targetId)
                  const next = (idx + 1) % drawingJobs.length
                  setDrawingPreview(drawingJobs[next])
                } else {
                  setDrawingPreview(null)
                }
                await deleteDrawingJob(targetId)
              }
            : undefined
        }
      />

      <MediaPreviewModal
        open={Boolean(queuePreview)}
        onClose={() => setQueuePreview(null)}
        item={
          queuePreview
            ? {
                id: queuePreview.id,
                title: queuePreview.source.split(/[/\\]/).pop() || queuePreview.id,
                subtitle:
                  queuePreview.type === 'review'
                    ? t('Video Review đã xuất', 'Rendered Review video')
                    : t('Video Clone đã xuất', 'Rendered Clone video'),
                src: studioApi.fileUrl(queuePreview.id),
                downloadUrl: studioApi.fileUrl(queuePreview.id, { download: true }),
                type: 'video',
              }
            : null
        }
        totalCount={jobs.length}
        currentIndex={queuePreview ? jobs.findIndex((j) => j.id === queuePreview.id) : -1}
        onPrevious={() => {
          if (!queuePreview || jobs.length <= 1) return
          const idx = jobs.findIndex((j) => j.id === queuePreview.id)
          if (idx >= 0) {
            const prev = (idx - 1 + jobs.length) % jobs.length
            setQueuePreview(jobs[prev])
          }
        }}
        onNext={() => {
          if (!queuePreview || jobs.length <= 1) return
          const idx = jobs.findIndex((j) => j.id === queuePreview.id)
          if (idx >= 0) {
            const next = (idx + 1) % jobs.length
            setQueuePreview(jobs[next])
          }
        }}
        onReveal={
          isDesktopApp && queuePreview
            ? async () => {
                await studioApi.revealJob(queuePreview.id)
              }
            : undefined
        }
        onDelete={
          queuePreview
            ? async () => {
                const targetId = queuePreview.id
                if (jobs.length > 1) {
                  const idx = jobs.findIndex((j) => j.id === targetId)
                  const next = (idx + 1) % jobs.length
                  setQueuePreview(jobs[next])
                } else {
                  setQueuePreview(null)
                }
                await deleteQueueJob(targetId)
              }
            : undefined
        }
      />
    </div>
  )
}
