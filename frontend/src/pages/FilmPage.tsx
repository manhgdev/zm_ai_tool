import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { localize, useLocale } from '@/app/i18n'
import { api } from '@/features/project/project.api'
import type { HardwareInfo } from '@/features/project/project.types'
import { studioApi, type QueueJob } from '@/features/studio/studio.api'
import { AudioSlider, CaptionModePicker, ReviewLangFields, ReviewLeftPanel, ReviewRightPanel, Stepper, useVoices } from '@/features/studio/ReviewSettingsPanel'
import { DEFAULT_REVIEW_SETTINGS, STYLE_TO_PIPE, modeLabel, resolveBuildMode, type ReviewSettings } from '@/features/studio/reviewSettings'
import { BackTitle } from '@/shared/components/BackTitle'
import { OutputFolderField } from '@/shared/components/OutputFolderField'
import { copyText } from '@/shared/lib/clipboard'
import './FilmPage.css'

type Props = { onBack: () => void; onOpenEditor?: (projectId: string) => void }
type View = 'list' | 'create'

type Draft = ReviewSettings & {
  source: string
  outputDir: string
  seriesOn: boolean
  seriesTitle: string
  seriesEpisode: number
}

const DRAFT_LS = 'zm_ai_tool.reviewDraft'

const DEFAULT_DRAFT: Draft = {
  ...DEFAULT_REVIEW_SETTINGS,
  source: '',
  outputDir: '',
  seriesOn: false,
  seriesTitle: '',
  seriesEpisode: 1,
}

function loadDraft(): Draft {
  try {
    const raw = localStorage.getItem(DRAFT_LS)
    if (!raw) return DEFAULT_DRAFT
    const parsed = JSON.parse(raw) as Partial<Draft> & { cutMode?: string }
    const buildMode = resolveBuildMode(parsed as Record<string, unknown>)
    delete parsed.cutMode
    return { ...DEFAULT_DRAFT, ...parsed, buildMode }
  } catch {
    return DEFAULT_DRAFT
  }
}

function jobTitle(job: QueueJob) {
  const snap = job.settings_snapshot || {}
  const named = String(snap.seriesTitle || '').trim()
  if (named) return named
  const src = job.source || ''
  if (src.startsWith('http')) return src
  return src.split(/[/\\]/).pop() || job.id
}

const PROGRESS_HEARTBEAT = /__VC_PROGRESS__\|/

function jobLog(job: QueueJob, t: (vi: string, en: string) => string) {
  const rows = Array.isArray(job.log) ? job.log.filter(Boolean) : []
  if (!rows.length && job.stage) {
    rows.push(t(`[Hệ thống] ${job.stage} — ${Math.round((job.progress || 0) * 100)}%`, `[System] ${job.stage} — ${Math.round((job.progress || 0) * 100)}%`))
  }
  if (job.error && !rows.some((line) => line.includes(job.error || ''))) {
    rows.push(job.error)
  }
  // Old backend workers can still emit coarse whole-pipeline heartbeats. They
  // are not task progress and duplicate the precise logs below, so hide them.
  return rows.filter((line) => !PROGRESS_HEARTBEAT.test(line))
}

function statusLabel(status: string, t: (vi: string, en: string) => string) {
  if (status === 'running') return t('Đang chạy', 'Running')
  if (status === 'queued') return t('Đang chờ', 'Queued')
  if (status === 'paused') return t('Đã dừng', 'Paused')
  if (status === 'interrupted') return t('Gián đoạn', 'Interrupted')
  if (status === 'done') return t('Hoàn thành', 'Done')
  if (status === 'failed' || status === 'cancelled') return t('Đã huỷ / Lỗi', 'Cancelled / Error')
  return status
}

function partStatusLabel(status: string | undefined, t: (vi: string, en: string) => string) {
  if (status === 'running') return t('Đang chạy', 'Running')
  if (status === 'queued') return t('Đang chờ', 'Queued')
  if (status === 'paused') return t('Đã dừng', 'Paused')
  if (status === 'pending') return t('Đang chờ', 'Pending')
  if (status === 'failed') return t('Lỗi', 'Failed')
  return t('Đã huỷ', 'Cancelled')
}

function reviewErrorLabel(error: string, t: (vi: string, en: string) => string) {
  if (error.includes('REVIEW_LLM_REQUIRED')) {
    return t(
      'Review Phim cần ít nhất một model Ollama để viết kịch bản. Hãy cài hoặc chọn model trong Cấu hình rồi chạy lại.',
      'Movie Review requires at least one Ollama model to write the script. Install or select one in Settings, then retry.',
    )
  }
  if (error.includes('REVIEW_LLM_MODEL_UNAVAILABLE')) {
    return t(
      'Model Ollama đã chọn không còn khả dụng. Hãy chọn lại model hoặc bật Ollama.',
      'The selected Ollama model is unavailable. Choose another model or start Ollama.',
    )
  }
  if (error.includes('REVIEW_CLOUD_KEY_REQUIRED')) {
    return t(
      'Chưa có API key cho Cloud AI đã chọn. Mở Cấu hình → Cloud, lưu key rồi chạy lại.',
      'The selected Cloud AI has no API key. Open Settings → Cloud, save its key, then retry.',
    )
  }
  if (error.includes('REVIEW_CLOUD_GEMINI_HTTP_403')) {
    return t(
      'Gemini từ chối request. Kiểm tra lại API key, project sở hữu key và quyền dùng Gemini API trong Cấu hình → Cloud.',
      'Gemini rejected the request. Check the API key, its project, and Gemini API access in Settings → Cloud.',
    )
  }
  if (error.includes('REVIEW_CLOUD_GEMINI_API_KEY_MISSING')) {
    return t(
      'Gemini chưa có API key. Mở Cấu hình → Cloud → Gemini, lưu key rồi chạy lại.',
      'Gemini has no API key. Open Settings → Cloud → Gemini, save a key, then retry.',
    )
  }
  if (error.includes('REVIEW_CLOUD_GEMINI_AUTH_FAILED') || error.includes('REVIEW_CLOUD_GEMINI_ACCESS_DENIED')) {
    return t(
      'Gemini từ chối API key hoặc key chưa có quyền dùng Gemini API. Kiểm tra key và project trong Cấu hình → Cloud.',
      'Gemini rejected the API key or it lacks Gemini API access. Check the key and project in Settings → Cloud.',
    )
  }
  if (error.includes('REVIEW_CLOUD_GEMINI_RATE_LIMITED_OR_QUOTA')) {
    return t(
      'Gemini đang hết quota hoặc bị giới hạn tốc độ. Chờ quota khả dụng rồi chạy lại.',
      'Gemini is out of quota or rate-limited. Wait for quota to become available, then retry.',
    )
  }
  if (error.includes('REVIEW_CLOUD_GEMINI_MODEL_OR_REQUEST_INVALID')) {
    return t(
      'Model AI phân tích Review của Gemini không hợp lệ. Mở Cấu hình → Cloud → Gemini và dùng model Gemini API, ví dụ gemini-2.5-flash.',
      'The Gemini Review analysis model is invalid. Open Settings → Cloud → Gemini and use a Gemini API model, such as gemini-2.5-flash.',
    )
  }
  if (error.includes('REVIEW_CLOUD_GEMINI_UNAVAILABLE')) {
    return t(
      'Gemini đang tạm không phản hồi. Hệ thống đã thử lại tự động; kiểm tra mạng, Base URL và API key trong Cấu hình → Cloud rồi chạy lại.',
      'Gemini is temporarily unreachable. The app retried automatically; check the network, base URL, and API key in Settings → Cloud, then retry.',
    )
  }
  if (error.includes('REVIEW_CLOUD_REQUEST_FAILED')) {
    return t(
      'Cloud AI từ chối hoặc không nhận được request. Kiểm tra API key, Base URL và Model AI Review.',
      'Cloud AI rejected or did not receive the request. Check the API key, base URL, and Review AI model.',
    )
  }
  if (error.includes('REVIEW_LLM_EVIDENCE_INVALID')) {
    return t(
      'AI không tạo được kịch bản có dữ kiện phim đáng tin cậy. Hãy thử lại với model Ollama khác.',
      'AI could not produce a script grounded in reliable movie evidence. Retry with another Ollama model.',
    )
  }
  if (error.includes('REVIEW_TRANSLATION_EMPTY')) {
    return t(
      'Không thể dịch đủ phụ đề nguồn. Hãy kiểm tra ngôn ngữ nguồn hoặc dùng chế độ AI recap.',
      'Not enough source subtitles could be translated. Check the source language or use AI recap mode.',
    )
  }
  return error
}

function fmtDate(job: QueueJob, locale: string) {
  if (!job.createdAt) return job.id
  return new Date(job.createdAt * 1000).toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US')
}

function relTime(ts: number | undefined, t: (vi: string, en: string) => string) {
  if (!ts) return ''
  const sec = Math.max(0, Date.now() / 1000 - ts)
  if (sec < 60) return t('Vừa xong', 'Just now')
  if (sec < 3600) return `${Math.floor(sec / 60)} ${t('phút trước', 'min ago')}`
  if (sec < 86400) return `${Math.floor(sec / 3600)} ${t('giờ trước', 'hours ago')}`
  return `${Math.floor(sec / 86400)} ${t('ngày trước', 'days ago')}`
}

function downloadHref(jobId: string, part?: number) {
  return studioApi.fileUrl(jobId, { part, download: true })
}

function deviceBadge(hw: HardwareInfo) {
  const accel = (hw.accel || 'cpu').toLowerCase()
  const gpu = accel === 'cuda' || accel === 'metal' || accel === 'mps'
  const name = (hw.gpuName || hw.label || '').trim()
  const showName = name && !/^(cpu|gpu)$/i.test(name)
  return { kind: gpu ? 'GPU' : 'CPU', name: showName ? name : '', gpu }
}

function AutoLog({ className, text }: { className?: string; text: string }) {
  const ref = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const node = ref.current
    if (node) node.scrollTop = node.scrollHeight
  }, [text])
  return <pre ref={ref} className={className}>{text}</pre>
}

export default function FilmPage({ onBack, onOpenEditor }: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  // Start on the project form so a first-time user does not land on an empty
  // project index. The first queue response switches established users back
  // to their project list; later polling must never interrupt their current UI.
  const [view, setView] = useState<View>('create')
  const [filter, setFilter] = useState<'all' | 'running' | 'done' | 'failed'>('all')
  const [draft, setDraft] = useState<Draft>(loadDraft)
  const [isDesktopApp, setIsDesktopApp] = useState(false)
  const [jobs, setJobs] = useState<QueueJob[]>([])
  // Removing a running project is asynchronous: the worker must release its
  // files before the backend can delete them. Keep it out of the project UI
  // immediately, while polling continues from the unfiltered source list.
  const [deletingJobIds, setDeletingJobIds] = useState<Set<string>>(() => new Set())
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [clearingLogs, setClearingLogs] = useState(false)
  const [logOpen, setLogOpen] = useState(true)
  const [partsOpen, setPartsOpen] = useState<Record<string, boolean>>({})
  const [jobLogOpen, setJobLogOpen] = useState<Record<string, boolean>>({})
  const [preview, setPreview] = useState<{ jobId: string; part?: number; title: string } | null>(null)
  const [cacheOpen, setCacheOpen] = useState(false)
  const [clearingCache, setClearingCache] = useState(false)
  const [editingJobId, setEditingJobId] = useState<string | null>(null)
  const [hw, setHw] = useState<HardwareInfo>({ label: '', accel: 'cpu' })
  const voices = useVoices(draft.language)
  const hasActiveJobs = jobs.some((job) => job.status === 'running' || job.status === 'queued')

  const set = (patch: Partial<Draft>) => setDraft((cur) => {
    const next = { ...cur, ...patch } as Draft & { cutMode?: string }
    delete next.cutMode
    return next
  })

  useEffect(() => {
    void fetch('/api/config').then(async (response) => response.ok && setIsDesktopApp(Boolean((await response.json() as { desktop?: boolean }).desktop))).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (editingJobId) return
    try {
      localStorage.setItem(DRAFT_LS, JSON.stringify(draft))
    } catch {}
  }, [draft, editingJobId])

  const pullRef = useRef<() => void>(() => undefined)
  const initialQueueLoad = useRef(true)
  useEffect(() => {
    const pull = () => studioApi.queue().then((snap) => {
      const reviewJobs = (snap.jobs || []).filter((job) => job.type === 'review')
      setJobs(reviewJobs)
      if (initialQueueLoad.current) {
        initialQueueLoad.current = false
        setView(reviewJobs.length ? 'list' : 'create')
      }
    }).catch(() => undefined)
    pullRef.current = pull
    void pull()
    if (!hasActiveJobs) return
    const timer = window.setInterval(() => void pull(), 3000)
    const onVisible = () => { if (document.visibilityState === 'visible') void pull() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [hasActiveJobs])

  useEffect(() => {
    api.hardware().then(setHw).catch(() => undefined)
  }, [])

  useEffect(() => {
    setDeletingJobIds((current) => {
      const retained = new Set([...current].filter((id) => jobs.some((job) => job.id === id)))
      return retained.size === current.size ? current : retained
    })
  }, [jobs])

  useEffect(() => {
    if (error) {
      toast.error(error)
      setError('')
    } else if (note) {
      toast.success(note)
      setNote('')
    }
  }, [note, error])

  const displayedJobs = useMemo(
    () => jobs.filter((job) => !deletingJobIds.has(job.id)),
    [deletingJobIds, jobs],
  )

  const counts = useMemo(() => ({
    all: displayedJobs.length,
    active: displayedJobs.filter((j) => j.status === 'running' || j.status === 'queued').length,
    queued: displayedJobs.filter((j) => j.status === 'queued').length,
    done: displayedJobs.filter((j) => j.status === 'done').length,
    failed: displayedJobs.filter((j) => j.status === 'failed' || j.status === 'cancelled' || j.status === 'interrupted' || j.status === 'paused').length,
  }), [displayedJobs])

  const visible = displayedJobs.filter((j) => {
    if (filter === 'running') return j.status === 'running' || j.status === 'queued'
    if (filter === 'done') return j.status === 'done'
    if (filter === 'failed') return j.status === 'failed' || j.status === 'cancelled' || j.status === 'interrupted' || j.status === 'paused'
    return true
  })

  const active = displayedJobs.find((j) => j.status === 'running')
  const activeJobs = displayedJobs.filter((j) => j.status === 'running' || j.status === 'queued')
  const logLines = activeJobs.length
    ? activeJobs.flatMap((job) => {
      const lines = jobLog(job, t)
      return activeJobs.length === 1 ? lines : [`[${jobTitle(job)}]`, ...lines]
    })
    : [t('[Hệ thống] Khởi động Review. Sẵn sàng...', '[System] Review ready. Waiting to start...')]

  function saveDraft() {
    localStorage.setItem(DRAFT_LS, JSON.stringify(draft))
    setNote(t('Đã lưu nháp.', 'Draft saved.'))
  }

  function askClearCache() {
    const src = draft.source.trim()
    if (!src || src.startsWith('http')) {
      setError(t('Hãy chọn video trên máy để xóa cache.', 'Choose a local video to clear cache.'))
      return
    }
    setError('')
    setCacheOpen(true)
  }

  async function confirmClearCache() {
    const src = draft.source.trim()
    if (!src || src.startsWith('http')) {
      setCacheOpen(false)
      setError(t('Hãy chọn video trên máy để xóa cache.', 'Choose a local video to clear cache.'))
      return
    }
    setClearingCache(true)
    setError('')
    setNote('')
    try {
      const res = await studioApi.clearReviewCache(src)
      setNote(res.cleared ? t('Đã xóa cache', 'Cache cleared') : t('Không có cache cho video này.', 'No cache for this video.'))
      setCacheOpen(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setCacheOpen(false)
    } finally {
      setClearingCache(false)
    }
  }

  function openCreate(from?: QueueJob, edit = false) {
    if (from?.settings_snapshot) {
      const snap = from.settings_snapshot
      setDraft({ ...DEFAULT_DRAFT, ...snap, source: from.source, buildMode: resolveBuildMode(snap) } as Draft)
    }
    setEditingJobId(edit ? from?.id || null : null)
    setView('create')
    setError('')
  }

  async function pickVideo() {
    const res = await studioApi.pickVideos()
    if (res.paths?.[0]) set({ source: res.paths[0] })
  }

  async function pickOut() {
    const res = await studioApi.pickFolder()
    return res.path || undefined
  }

  async function createAndRun() {
    if (!draft.source.trim()) {
      setError(t('Hãy dán link hoặc chọn video.', 'Paste a link or choose a video.'))
      return
    }
    if (draft.buildMode === 'smart' && draft.source.startsWith('http')) {
      setError(t('Cắt thông minh cần video có sẵn trên máy. Hãy bấm Chọn video.', 'Smart cut needs a local video file. Click Choose video.'))
      return
    }
    setBusy(true)
    setError('')
    saveDraft()
    try {
      const res = await studioApi.generateReview({
        ...draft,
        cutMode: undefined,
        buildMode: draft.buildMode,
        style: STYLE_TO_PIPE[draft.scriptStyle],
        durationSec: draft.chunkMinutes * 60,
        subtitle: true,
        quality: '1080p',
        ratio: '16:9',
        spoiler: 'none',
        headless: true,
        naming: '{name}_review',
      })
      const createdJob = res.job
      if (createdJob) setJobs((current) => [createdJob, ...current.filter((job) => job.id !== createdJob.id)])
      setView('list')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function saveJobSettings() {
    if (!editingJobId) return
    setBusy(true)
    setError('')
    try {
      const snap = await studioApi.updateJobSettings(editingJobId, {
        ...draft, cutMode: undefined, buildMode: draft.buildMode,
        style: STYLE_TO_PIPE[draft.scriptStyle], durationSec: draft.chunkMinutes * 60,
        subtitle: true, quality: '1080p', ratio: '16:9', spoiler: 'none',
      })
      setJobs((snap.jobs || []).filter((j) => j.type === 'review'))
      setNote(t('Đã lưu cài đặt dự án.', 'Project settings saved.'))
      setEditingJobId(null)
      setView('list')
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setError(message.includes('Không thể đổi cài đặt khi job đang chạy')
        ? t('Không thể lưu cài đặt khi job đang chạy. Hãy dừng job trước.', 'Settings cannot be saved while the job is running. Pause it first.')
        : message)
    } finally {
      setBusy(false)
    }
  }

  function copyLog() {
    void copyText(logLines.join('\n'), t('Đã sao chép nhật ký hoạt động.', 'Activity log copied.'))
  }

  async function clearJobLogs() {
    setClearingLogs(true)
    try {
      const snapshot = await studioApi.globalAction('clear_logs')
      setJobs((snapshot.jobs || []).filter((job) => job.type === 'review'))
      setNote(t('Đã xoá nhật ký tiến trình.', 'Progress logs cleared.'))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setClearingLogs(false)
    }
  }

  async function removeJob(job: QueueJob) {
    if (!window.confirm(t(`Xoá dự án «${jobTitle(job)}»?`, `Delete project “${jobTitle(job)}”?`))) return
    const wasLastDisplayedJob = displayedJobs.length === 1
    setError('')
    setDeletingJobIds((current) => new Set(current).add(job.id))
    if (preview?.jobId === job.id) setPreview(null)
    if (wasLastDisplayedJob) setView('create')
    try {
      const snap = await studioApi.jobAction(job.id, 'remove')
      setJobs((snap.jobs || []).filter((j) => j.type === 'review'))
      const activeJob = job.status === 'running' || job.status === 'queued'
      setNote(activeJob
        ? t('Đang dừng dự án và dọn file ở nền.', 'Stopping the project and cleaning up files in the background.')
        : t('Đã xoá dự án và dọn file.', 'Project and its files were deleted.'))
    } catch (err) {
      setDeletingJobIds((current) => {
        const next = new Set(current)
        next.delete(job.id)
        return next
      })
      if (wasLastDisplayedJob) setView('list')
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function cancelJob(job: QueueJob) {
    setError('')
    try {
      const snap = await studioApi.jobAction(job.id, 'cancel')
      setJobs((snap.jobs || []).filter((row) => row.type === 'review'))
      setNote(t('Đã gửi lệnh huỷ tới backend.', 'Cancellation request sent to the backend.'))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function removePart(job: QueueJob, index: number) {
    if (!window.confirm(t(`Xoá phần ${index}?`, `Delete part ${index}?`))) return
    const snap = await studioApi.deletePart(job.id, index)
    setJobs((snap.jobs || []).filter((j) => j.type === 'review'))
    if (preview?.jobId === job.id && preview.part === index) setPreview(null)
  }

  async function rerenderJob(job: QueueJob) {
    setError('')
    try {
      const snap = await studioApi.jobAction(job.id, 'retry')
      setJobs((snap.jobs || []).filter((row) => row.type === 'review'))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const previewModal = preview ? (
    <div className="rv-modal" onClick={() => setPreview(null)}>
      <div className="rv-dialog rv-preview-box" onClick={(e) => e.stopPropagation()}>
        <div className="rv-card-title">
          <h2>{t('Xem trước', 'Preview')}</h2>
          <button type="button" className="rv-ghost" onClick={() => setPreview(null)}>×</button>
        </div>
        <video className="rv-preview-video" src={studioApi.fileUrl(preview.jobId, { part: preview.part })} controls autoPlay />
        <div className="rv-dialog-actions">
          <a className="rv-ghost" href={downloadHref(preview.jobId, preview.part)}>{t('Tải xuống', 'Download')}</a>
          <button type="button" className="rv-run" onClick={() => setPreview(null)}>{t('Đóng', 'Close')}</button>
        </div>
      </div>
    </div>
  ) : null
  if (view === 'list') {
    return (
      <div className="rv-page">
        <div className="rv-top">
          <BackTitle onBack={onBack}>{t('Dự án của bạn', 'Your projects')}<span className="rv-count">({counts.all})</span></BackTitle>
          <button type="button" className="rv-new" onClick={() => openCreate()}>+ {t('Tạo mới', 'New')}</button>
        </div>
        <div className="rv-filters">
          <button type="button" className={`rv-chip${filter === 'all' ? ' on' : ''}`} onClick={() => setFilter('all')}><i className="all" />{t('Tất cả', 'All')} {counts.all}</button>
          <button type="button" className={`rv-chip${filter === 'running' ? ' on' : ''}`} onClick={() => setFilter('running')}><i className="run" />{t('Đang hoạt động', 'Active')} {counts.active}</button>
          <button type="button" className={`rv-chip${filter === 'done' ? ' on' : ''}`} onClick={() => setFilter('done')}><i className="ok" />{t('Hoàn thành', 'Done')} {counts.done}</button>
          <button type="button" className={`rv-chip${filter === 'failed' ? ' on' : ''}`} onClick={() => setFilter('failed')}><i className="bad" />{t('Dừng / Lỗi', 'Paused / Error')} {counts.failed}</button>
        </div>
        {counts.active ? (
          <div className="rv-banner">● {t('Hàng đợi', 'Queue')}: {counts.queued} {t('dự án đang chờ', 'projects waiting')}
            {active ? <> · {t('Đang chạy', 'Running')}: {jobTitle(active)} - {fmtDate(active, locale)}</> : null}
          </div>
        ) : null}
        {!visible.length ? (
          <div className="rv-empty">{t('Chưa có dự án review. Bấm Tạo mới để bắt đầu.', 'No review projects yet. Click New to start.')}</div>
        ) : visible.map((job, idx) => {
          const pct = Math.round((job.progress || 0) * 100)
          const snap = job.settings_snapshot || {}
          const running = job.status === 'running' || job.status === 'queued'
          const parts = job.parts && job.parts.length ? job.parts : []
          const doneParts = parts.filter((p) => p.status === 'done').length
          const open = partsOpen[job.id] ?? true
          const logShown = jobLogOpen[job.id] ?? false
          const lines = jobLog(job, t)
          const mode = resolveBuildMode(snap)
          const showParts = mode === 'accumulate' && parts.length > 1
          const canFile = job.status === 'done' || parts.some((p) => p.status === 'done')
          return (
            <article key={job.id} className="rv-job">
              <div className="rv-job-top">
                <div className="rv-job-summary">
                  <span className="rv-num">#{idx + 1}</span>
                  <h3>{jobTitle(job)} - {fmtDate(job, locale)}</h3>
                  <div className="rv-meta">{relTime(job.updatedAt || job.createdAt, t)} · {fmtDate(job, locale)}</div>
                  <div className="rv-tags">
                    <span>📐 {modeLabel(mode, t)}</span>
                    <span>🌐 {t('Gốc', 'Source')}: {String(snap.sourceLang || 'auto').toUpperCase()}</span>
                    <span>🎙 {voices.find((v) => v.id === snap.voice)?.name || t('Giọng hệ thống', 'System voice')}</span>
                    <span>📝 {String(snap.scriptStyle || snap.style || '')}</span>
                  </div>
                </div>
                <span className={job.status === 'done' ? 'rv-ok' : running ? 'rv-run-badge' : 'rv-warn'}>● {statusLabel(job.status, t)}</span>
              </div>
              {running ? (
                <>
                  <div className="rv-row" style={{ justifyContent: 'space-between', margin: '8px 0 4px' }}>
                    <span className="rv-meta">{job.stage}</span>
                    <span className="rv-meta">{pct}%</span>
                  </div>
                  <div className="rv-bar"><i style={{ width: `${pct}%` }} /></div>
                </>
              ) : null}
              {showParts ? (
                <div className="rv-parts-panel">
                  <button type="button" className="rv-parts-head" onClick={() => setPartsOpen((s) => ({ ...s, [job.id]: !open }))}>
                    <span>◉ {t('Tiến độ phân đoạn', 'Segment progress')} ({doneParts}/{parts.length}) {open ? '⌃' : '⌄'}</span>
                    <span className="rv-mode-tag">{modeLabel(mode, t)}</span>
                  </button>
                  {open ? <div className="rv-parts">
                  {(parts.length ? parts : [{ index: 1, label: job.stage, status: job.status }]).map((part, i, arr) => {
                    const ready = part.status === 'done'
                    return (
                      <div key={part.index} className="rv-part">
                        <span className={`rv-tree${i === arr.length - 1 ? ' last' : ''}`} />
                        <span className="rv-part-name">🎬 {t('Phần', 'Part')} {part.index}{part.label ? `: ${part.label}` : ''}</span>
                        {ready ? (
                          <span className="rv-part-actions">
                            <span className="rv-ok">✓ {t('Xong', 'Done')}</span>
                            <a className="rv-dl" href={downloadHref(job.id, part.index)}>{t('Tải phần này', 'Download this part')}</a>
                            <button type="button" className="rv-icon-btn" title={t('Xem trước', 'Preview')} onClick={() => setPreview({ jobId: job.id, part: part.index, title: `${t('Phần', 'Part')} ${part.index}` })}>▶</button>
                            <button type="button" className="rv-icon-btn danger" title={t('Xoá', 'Delete')} onClick={() => void removePart(job, part.index)}>✕</button>
                          </span>
                        ) : (
                          <span className="rv-warn">{partStatusLabel(part.status, t)}</span>
                        )}
                      </div>
                    )
                  })}
                  {job.error ? <div className="rv-part"><span /><span>{reviewErrorLabel(job.error, t)}</span><span /></div> : null}
                  </div> : null}
                </div>
              ) : null}
              <div className="rv-job-actions">
              <div className="rv-row">
                {job.status === 'done' && job.projectId && onOpenEditor ? (
                  <button type="button" className="rv-run" onClick={() => onOpenEditor(job.projectId!)}>▶ {t('Mở Editor', 'Open Editor')}</button>
                ) : null}
                {canFile ? (
                  <>
                    <button type="button" className="rv-ghost" onClick={() => setPreview({ jobId: job.id, title: jobTitle(job) })}>▶ {t('Xem trước', 'Preview')}</button>
                    <a className="rv-ghost" href={downloadHref(job.id)}>{t('Tải xuống', 'Download')}</a>
                  </>
                ) : null}
                {running ? (
                  <>
                    <button type="button" className="rv-run" onClick={() => void studioApi.jobAction(job.id, 'pause')}>Ⅱ {t('Dừng', 'Pause')}</button>
                    <button type="button" className="rv-ghost" onClick={() => void cancelJob(job)}>{t('Hủy', 'Cancel')}</button>
                  </>
                ) : job.status === 'paused' || job.status === 'interrupted' ? (
                  <>
                    <button type="button" className="rv-run" onClick={() => { void studioApi.jobAction(job.id, 'resume').then(() => pullRef.current()) }}>▶ {t('Tiếp tục', 'Resume')}</button>
                    <button type="button" className="rv-ghost" onClick={() => void cancelJob(job)}>{t('Hủy', 'Cancel')}</button>
                  </>
                ) : job.status === 'failed' || job.status === 'cancelled' ? (
                  <button type="button" className="rv-run" onClick={() => void rerenderJob(job)}>▶ {t('Thử lại', 'Retry')}</button>
                ) : (
                  <button type="button" className="rv-run" onClick={() => void rerenderJob(job)}>▶ {t('Render lại', 'Render again')}</button>
                )}
                <button type="button" className="rv-ghost" onClick={() => openCreate(job, true)}>✎ {t('Cài đặt', 'Settings')}</button>
                <button type="button" className="rv-ghost danger" onClick={() => void removeJob(job)}>✕ {t('Xoá', 'Delete')}</button>
              </div>
              <div className="rv-log-h">
                <button type="button" className="rv-ghost" onClick={() => setJobLogOpen((s) => ({ ...s, [job.id]: !logShown }))}>
                  📄 {t('Xem nhật ký tiến trình', 'View progress log')} ({lines.length} {t('dòng', 'lines')}) {logShown ? '▲' : '▼'}
                </button>
                <button type="button" className="rv-mini" onClick={() => void copyText(lines.join('\n'), t('Đã sao chép log.', 'Log copied.'))}>📋 {t('Chép log', 'Copy log')}</button>
                {!showParts ? <span className="rv-mode-tag">{modeLabel(mode, t)}</span> : null}
              </div>
              </div>
              {logShown ? <AutoLog className="rv-job-log" text={lines.join('\n') || t('[Hệ thống] đang chạy…', '[System] running…')} /> : null}
            </article>
          )
        })}
        {previewModal}
      </div>
    )
  }

  return (
    <div className="rv-page">
      <div className="rv-top">
        <div className="rv-row">
          <BackTitle onBack={() => setView('list')}>
            {editingJobId ? t('Cài đặt dự án', 'Project settings') : t('Tạo dự án mới', 'Create new project')}
          </BackTitle>
        </div>
        <div className="rv-top-actions">
          {!editingJobId && <button type="button" className="rv-draft" onClick={saveDraft}>🖫 {t('Lưu nháp', 'Save draft')}</button>}
          <button type="button" className="rv-draft" disabled={busy || clearingCache} onClick={askClearCache}>{t('Xóa cache', 'Clear cache')}</button>
          <button type="button" className="rv-run" disabled={busy} onClick={() => void (editingJobId ? saveJobSettings() : createAndRun())}>
            {editingJobId ? t('Lưu cài đặt', 'Save settings') : `+ ${t('Tạo & Chạy', 'Create & Run')}`}
          </button>
        </div>
      </div>
      <div className="rv-grid">
        <section className="rv-card">
          <div className="rv-card-title">
            <h2>{t('Cấu hình & Tối ưu', 'Setup & optimize')}</h2>
            <button type="button" className="rv-reset" onClick={() => setDraft(DEFAULT_DRAFT)}>↻ {t('Đặt lại', 'Reset')}</button>
          </div>
          <label className="rv-field">
            <span>{t('Video gốc', 'Source video')}</span>
            <div className="rv-combo rv-combo-peach">
              <span className="rv-combo-ico">📁</span>
              <input value={draft.source} onChange={(e) => set({ source: e.target.value })} placeholder={t('Dán link video (YouTube, TikTok, FB…) hoặc chọn video…', 'Paste a video link (YouTube, TikTok, FB…) or choose a file…')} />
              <button type="button" className="rv-inbtn" onClick={() => void pickVideo()}>{t('Chọn', 'Choose')}</button>
            </div>
          </label>
          <ReviewLangFields settings={draft} onChange={set} />
          <OutputFolderField isDesktopApp={isDesktopApp} value={draft.outputDir} onChange={(outputDir) => set({ outputDir })} onChoose={isDesktopApp ? pickOut : undefined} defaultPath={t('Ví dụ: du-an-01', 'Example: project-01')} appFolder="review" label={t('Thư mục lưu video', 'Video output folder')} />

          <ReviewLeftPanel settings={draft} onChange={set} />
          <AudioSlider value={draft.originalAudioPct} onChange={(v) => set({ originalAudioPct: v })} />
          <CaptionModePicker value={draft.captionMode} onChange={(v) => set({ captionMode: v })} />
        </section>

        <section className="rv-card">
          <ReviewRightPanel
            settings={draft}
            onChange={set}
            voices={voices}
            seriesSlot={(
              <div className={`rv-series-box${draft.seriesOn ? ' on' : ''}`}>
                <label className="rv-check">
                  <span>🎞 {t('Bộ nhớ phim bộ (Trí nhớ theo tập)', 'Series memory (per episode)')}</span>
                  <span className="rv-switch">
                    <input type="checkbox" checked={draft.seriesOn} onChange={(e) => set({ seriesOn: e.target.checked })} />
                    <i />
                  </span>
                </label>
                {draft.seriesOn ? (
                  <>
                    <p className="rv-hint">{t('Ghi nhớ nhân vật & diễn biến qua từng tập – giữ liên mạch.', 'Remembers characters and plot across episodes to keep continuity.')}</p>
                    <div className="rv-series-grid">
                      <label className="rv-field">
                        <span>{t('Tên phim / Bộ phim', 'Title / series')}</span>
                        <input value={draft.seriesTitle} onChange={(e) => set({ seriesTitle: e.target.value })} placeholder={t('Nhập tên phim bộ (VD: Hậu Duệ Mặt Trời)…', 'Enter the series title (e.g. Descendants of the Sun)…')} />
                      </label>
                      <label className="rv-field">
                        <span>{t('Tập số', 'Episode')}</span>
                        <Stepper value={draft.seriesEpisode} min={1} onChange={(v) => set({ seriesEpisode: v })} />
                      </label>
                    </div>
                    <p className="rv-hint">💡 {t('Mẹo: Để trống nếu là phim lẻ. Nếu là phim bộ nhiều tập, hãy nhập tên phim để AI nhớ nhân vật cho các tập sau.', 'Tip: leave blank for a standalone film. For a multi-episode series, enter the title so AI remembers characters for later episodes.')}</p>
                  </>
                ) : null}
              </div>
            )}
          />
        </section>
      </div>
      <section className="rv-card rv-log">
        <div className="rv-log-h">
          <button type="button" className="rv-ghost" onClick={() => setLogOpen((v) => !v)}>
            <strong>📃 {t('Nhật ký hoạt động (Tiến trình đang chạy)', 'Activity log (running progress)')} {logOpen ? '▲' : '▼'}</strong>
          </button>
          <div className="rv-log-tools">
            {(() => {
              const device = deviceBadge(hw)
              const accessibleLabel = device.name
                ? t(`${device.kind} đang dùng · ${device.name}`, `${device.kind} in use · ${device.name}`)
                : t(`${device.kind} đang dùng`, `${device.kind} in use`)
              return (
                <span className={`rv-device-badge${device.gpu ? ' is-gpu' : ''}`} title={accessibleLabel} aria-label={accessibleLabel}>
                  <i aria-hidden="true" />
                  <span className="rv-device-kind">{device.kind}</span>
                  {device.name ? <span className="rv-device-name">{device.name}</span> : null}
                </span>
              )
            })()}
            <button type="button" className="rv-mini" onClick={copyLog}>{t('Sao chép', 'Copy')}</button>
            <button type="button" className="rv-mini danger" disabled={clearingLogs} onClick={() => void clearJobLogs()}>{clearingLogs ? t('Đang xoá…', 'Clearing…') : t('Xoá nhật ký', 'Clear log')}</button>
          </div>
        </div>
        {logOpen ? <AutoLog text={logLines.join('\n')} /> : null}
      </section>
      {cacheOpen ? (
        <div className="rv-modal" role="presentation" onClick={() => !clearingCache && setCacheOpen(false)}>
          <div
            className="rv-dialog rv-confirm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="rv-clear-cache-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="rv-clear-cache-title">{t('Xóa cache', 'Clear cache')}</h2>
            <p>{t('Xóa cache nhận dạng và kịch bản của video này. Video nguồn không bao giờ bị xóa.', 'Clear this video’s transcript and script cache. The source video is never deleted.')}</p>
            <p className="rv-confirm-note">{t('Lần chạy sau sẽ nhận dạng và viết kịch bản lại.', 'The next run will re-transcribe and rewrite the script.')}</p>
            <div className="rv-dialog-actions">
              <button type="button" className="rv-ghost" disabled={clearingCache} onClick={() => setCacheOpen(false)}>{t('Hủy', 'Cancel')}</button>
              <button type="button" className="rv-confirm-go" disabled={clearingCache} onClick={() => void confirmClearCache()}>
                {clearingCache ? t('Đang xóa…', 'Deleting…') : t('Xóa cache', 'Clear cache')}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
