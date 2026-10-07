import { createPortal } from 'react-dom'
import { cancelBackgroundJob } from '@/realtime/cancelBackgroundJob'
import { toast } from 'sonner'
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { useRealtimeEvents, useRealtimeStatus } from '@/realtime/RealtimeProvider'
import type { RealtimeEvent } from '@/realtime/types'
import { updateBackgroundJobs, type BackgroundJob } from '@/realtime/backgroundJobs'
import type { AppMode } from './Header'

const tools: Record<string, [AppMode, string, string]> = {
  tts: ['tts', 'Tạo giọng / Chép lời', 'Speech / Transcription'],
  flow: ['flow', 'Flow', 'Flow'],
  download: ['download', 'Tải xuống', 'Download'],
  drawing: ['drawing', 'Vẽ', 'Drawing'],
  automation: ['automation', 'Tự động hóa', 'Automation'],
  cleaner: ['cleaner', 'Xóa vật thể', 'Video cleaner'],
  'srt-image': ['srt-image', 'Ghép media', 'Media compose'],
  'srt-export': ['srt-export', 'Xuất phụ đề', 'Subtitle export'],
  project: ['clone', 'Xử lý video', 'Video processing'],
}
const POSITION_KEY = 'zm-ai-tool:background-tasks-position:v1'
const COMPACT_POSITION_KEY = 'zm-ai-tool:background-tasks-position-compact:v1'

export default function BackgroundTasks({ onOpen, project }: {
  onOpen: (mode: AppMode) => void
  project?: { id: string; running: boolean; progress: number; message?: string }
  currentMode?: AppMode
}) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [jobs, setJobs] = useState<BackgroundJob[]>([])
  const cancellingRef = useRef(new Set<string>())
  const [cancelling, setCancelling] = useState<Set<string>>(new Set())
  const [selectedJob, setSelectedJob] = useState<BackgroundJob | null>(null)
  const cancelJob = async (job: BackgroundJob) => {
    const key = `${job.topic}:${job.id}`
    if (cancellingRef.current.has(key)) return
    cancellingRef.current.add(key)
    setCancelling(new Set(cancellingRef.current))
    try {
      const result = await cancelBackgroundJob(job)
      if (result.ok === false) throw new Error(t('Backend chưa chấp nhận huỷ tác vụ.', 'The backend did not accept cancellation.'))
    } catch (error) {
      toast.error(t('Không huỷ được tác vụ', 'Could not cancel task'), { description: error instanceof Error ? error.message : String(error) })
    } finally {
      cancellingRef.current.delete(key)
      setCancelling(new Set(cancellingRef.current))
    }
  }
  const cancelButton = (job: BackgroundJob) => <button type="button"
    aria-label={t(`Huỷ tác vụ ${job.id}`, `Cancel task ${job.id}`)} title={t('Huỷ tác vụ', 'Cancel task')}
    disabled={cancelling.has(`${job.topic}:${job.id}`)} onClick={() => void cancelJob(job)}
    className="h-6 w-6 shrink-0 rounded text-base hover:bg-accent/50 disabled:opacity-50">
    {cancelling.has(`${job.topic}:${job.id}`) ? '…' : '×'}
  </button>
  const [collapsed, setCollapsed] = useState(true)
  const [position, setPosition] = useState<{ left: number; top: number } | null>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(POSITION_KEY) || 'null')
      return saved && Number.isFinite(saved.left) && Number.isFinite(saved.top) ? saved : null
    } catch { return null }
  })
  const dragRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null)
  const panelRef = useRef<HTMLElement>(null)
  const compactPositionRef = useRef<{ left: number; top: number } | null>(null)
  const mountedRef = useRef(false)
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(COMPACT_POSITION_KEY) || 'null')
      if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) compactPositionRef.current = saved
    } catch { /* private mode */ }
  }, [])
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    if (collapsed) {
      if (compactPositionRef.current) {
        const compact = compactPositionRef.current
        compactPositionRef.current = null
        setPosition(compact)
        try { localStorage.setItem(COMPACT_POSITION_KEY, JSON.stringify(compact)) } catch { /* private mode */ }
      }
      return
    }
    if (position) {
      compactPositionRef.current = position
      try { localStorage.setItem(COMPACT_POSITION_KEY, JSON.stringify(position)) } catch { /* private mode */ }
    }
  }, [collapsed])
  useLayoutEffect(() => {
    if (!position || collapsed || !panelRef.current) return
    const height = panelRef.current.getBoundingClientRect().height
    const nextTop = Math.max(8, Math.min(position.top, window.innerHeight - height - 8))
    if (nextTop !== position.top) setPosition(current => current ? { ...current, top: nextTop } : current)
  }, [collapsed, position])
  useEffect(() => {
    if (!position) return
    try { localStorage.setItem(POSITION_KEY, JSON.stringify(position)) } catch { /* private mode */ }
  }, [position])
  useEffect(() => {
    const clear = (event: Event) => {
      const topic = (event as CustomEvent<{ topic?: string }>).detail?.topic
      if (topic) setJobs(current => current.filter(job => job.topic !== topic))
      else setJobs([])
    }
    window.addEventListener('zm-ai-tool:background-tasks-clear', clear)
    return () => window.removeEventListener('zm-ai-tool:background-tasks-clear', clear)
  }, [])
  const receive = useCallback((event: RealtimeEvent) => setJobs(current => updateBackgroundJobs(current, event)), [])
  useRealtimeEvents('flow', receive)
  useRealtimeEvents('tts', receive)
  useRealtimeEvents('download', receive)
  useRealtimeEvents('drawing', receive)
  useRealtimeEvents('automation', receive)
  useRealtimeEvents('cleaner', receive)
  useRealtimeEvents('srt-image', receive)
  useRealtimeEvents('srt-export', receive)
  const connection = useRealtimeStatus()
  const rows = project?.running ? [...jobs, { id: project.id, topic: 'project', progress: project.progress, message: project.message || '', queued: false, updatedAt: Date.now() }] : jobs
  if (!rows.length) return null
  // Keep every task in the FIFO list; the viewport shows about three rows and
  // the scrollbar exposes older/newer tasks without reordering them.
  const visibleRows = rows
  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button')) return
    const rect = event.currentTarget.parentElement?.getBoundingClientRect()
    if (!rect) return
    dragRef.current = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag) return
    setPosition({
      left: Math.max(8, Math.min(window.innerWidth - 258, drag.left + event.clientX - drag.x)),
      top: Math.max(8, Math.min(window.innerHeight - 60, drag.top + event.clientY - drag.y)),
    })
  }
  const stopDrag = () => { dragRef.current = null }
  return <aside ref={panelRef} aria-label={t('Tác vụ nền', 'Background tasks')}
    className={`fixed z-[190] max-h-[calc(100vh-1rem)] w-[250px] max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-lg border border-border bg-background/95 p-1.5 shadow-lg backdrop-blur-sm ${position ? '' : 'bottom-3 right-3'}`}
    style={position ? { left: position.left, top: position.top } : undefined}>
    <div className="flex cursor-move touch-none items-center justify-between gap-2 px-1.5 py-1 text-[11px] font-semibold"
      onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={stopDrag} onPointerCancel={stopDrag}
      title={t('Kéo để di chuyển', 'Drag to move')}>
      <span>{t('Tác vụ nền', 'Background tasks')} · {rows.length}</span>
      <button type="button" aria-label={collapsed ? t('Mở rộng tác vụ nền', 'Expand background tasks') : t('Thu nhỏ tác vụ nền', 'Collapse background tasks')}
        title={collapsed ? t('Mở rộng', 'Expand') : t('Thu nhỏ', 'Collapse')}
        onClick={() => setCollapsed(value => !value)}
        className="flex h-5 w-5 items-center justify-center rounded text-sm leading-none text-muted-foreground hover:bg-accent/50 hover:text-foreground">
        {collapsed ? '+' : '−'}
      </button>
    </div>
    {connection !== 'connected' && <p role="status" className="px-2 text-xs text-muted-foreground">{t('Đang kết nối lại tiến độ…', 'Reconnecting to progress…')}</p>}
    {!collapsed && <div className="flex max-h-[132px] flex-col gap-0.5 overflow-y-auto pr-0.5">
      {visibleRows.map(job => {
        const tool = tools[job.topic]
        if (!tool) return null
        const label = t(tool[1], tool[2])
        return <div key={`${job.topic}:${job.id}`} className="flex items-center"><button type="button" onClick={() => setSelectedJob(job)}
          title={t('Mở trang tác vụ', 'Open task page')}
          className="min-w-0 flex-1 rounded-lg px-2 py-2 text-left hover:bg-accent/40">
          <span className="flex items-center justify-between gap-2 text-xs"><strong>{label} · {job.id.slice(0, 8)}</strong><span>{Math.round(job.progress)}%</span></span>
          <progress aria-label={label} max={100} value={job.progress} className="block h-1 w-full accent-primary" />
        </button>{cancelButton(job)}</div>
      })}
    </div>}
    {collapsed && (() => {
      const latest = rows[0]
      const tool = latest && tools[latest.topic]
      return tool ? <div className="flex items-center"><button type="button" onClick={() => setSelectedJob(latest)} className="min-w-0 flex-1 rounded-lg px-2 py-1 text-left hover:bg-accent/40">
        <span className="flex items-center justify-between gap-2 text-xs"><strong>{t(tool[1], tool[2])}</strong><span>{Math.round(latest.progress)}%</span></span>
        <progress aria-label={t(tool[1], tool[2])} max={100} value={latest.progress} className="mt-1 block h-1 w-full accent-primary" />
      </button>{cancelButton(latest)}</div> : null
    })()}
    {selectedJob && (() => {
      const currentJob = rows.find(row => row.topic === selectedJob.topic && row.id === selectedJob.id)
      if (!currentJob) return null
      const detailTool = tools[selectedJob.topic]
      const detailLabel = detailTool ? t(detailTool[1], detailTool[2]) : selectedJob.topic
      return createPortal(<div className="fixed inset-0 z-[210] grid place-items-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-label={detailLabel}>
        <section className="w-[min(560px,calc(100vw-2rem))] rounded-xl border border-border bg-background p-5 shadow-2xl">
          <div className="flex items-start justify-between gap-4"><div><p className="text-xs text-muted-foreground">{detailLabel}</p><h2 className="mt-1 text-lg font-semibold">{selectedJob.id}</h2></div><button type="button" aria-label={t('Đóng chi tiết tác vụ', 'Close task details')} onClick={() => setSelectedJob(null)} className="h-8 w-8 rounded text-xl text-muted-foreground hover:bg-accent/50">×</button></div>
          <div className="mt-5 flex items-center justify-between text-sm"><strong>{t('Tiến độ', 'Progress')}</strong><strong>{Math.round(currentJob.progress)}%</strong></div>
          <progress max={100} value={currentJob.progress} className="mt-2 block h-2 w-full accent-primary" />
          <p className="mt-4 min-h-12 rounded-lg bg-muted/40 p-3 text-sm text-muted-foreground">{currentJob.message || t('Đang xử lý…', 'Processing…')}</p>
          <div className="mt-5 flex justify-end gap-2"><button type="button" onClick={() => void cancelJob(selectedJob)} disabled={cancelling.has(`${selectedJob.topic}:${selectedJob.id}`)} className="rounded-lg border border-destructive/40 px-3 py-2 text-sm text-destructive hover:bg-destructive/10">{cancelling.has(`${selectedJob.topic}:${selectedJob.id}`) ? t('Đang huỷ…', 'Cancelling…') : t('Huỷ tác vụ', 'Cancel task')}</button><button type="button" onClick={() => { setSelectedJob(null); if (detailTool) onOpen(detailTool[0]) }} className="rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent/50">{t('Mở trang', 'Open page')}</button></div>
        </section>
      </div>, document.body)
    })()}
  </aside>
}
