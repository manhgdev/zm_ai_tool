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
  useRealtimeEvents('flow', receive)
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
        return <button key={`${job.topic}:${job.id}`} type="button" onClick={() => onOpen(tool[0])}
          title={t('Mở trang tác vụ', 'Open task page')}
          className="rounded-lg px-2 py-2 text-left hover:bg-accent/40">
          <span className="flex items-center justify-between gap-2 text-xs"><strong>{label} · {job.id.slice(0, 8)}</strong><span>{Math.round(job.progress)}%</span></span>
          <progress aria-label={label} max={100} value={job.progress} className="block h-1 w-full accent-primary" />
        </button>
      })}
    </div>}
    {collapsed && (() => {
      const latest = rows[0]
      const tool = latest && tools[latest.topic]
      return tool ? <button type="button" onClick={() => onOpen(tool[0])} className="w-full rounded-lg px-2 py-1 text-left hover:bg-accent/40">
        <span className="flex items-center justify-between gap-2 text-xs"><strong>{t(tool[1], tool[2])}</strong><span>{Math.round(latest.progress)}%</span></span>
        <progress aria-label={t(tool[1], tool[2])} max={100} value={latest.progress} className="mt-1 block h-1 w-full accent-primary" />
      </button> : null
    })()}
  </aside>
}
