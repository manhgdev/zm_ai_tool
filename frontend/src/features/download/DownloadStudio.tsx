import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  DownloadFormat,
  DownloadJob,
  DownloadOpts,
  DownloadQuality,
  JobFilter,
} from './download.types'
import { downloadApi } from './download.api'
import { BackTitle } from '@/shared/components/BackTitle'
import { localize, useLocale } from '@/app/i18n'
import { OutputFolderField } from '@/shared/components/OutputFolderField'
import { copyText } from '@/shared/lib/clipboard'
import { toast } from 'sonner'
import { useRealtimeEvents } from '@/realtime/RealtimeProvider'
import './DownloadStudio.css'

const ACTIVE = new Set(['queued', 'running'])
const LS_OPTS = 'vc.download.opts.v1'
const LS_PATH = 'vc.download.savePath.v1'
const LS_TA_H = 'vc.download.textareaH.v1'
const LS_JOBS = 'vc.download.jobs.v1'
const LS_LINKS = 'vc.download.links.v1'
const TA_H_MIN = 100
const TA_H_MAX = 480
const TA_H_DEFAULT = 132

function loadTextareaH(): number {
  try {
    const n = Number(localStorage.getItem(LS_TA_H))
    if (Number.isFinite(n) && n >= TA_H_MIN && n <= TA_H_MAX) return Math.round(n)
  } catch {
    /* ignore */
  }
  return TA_H_DEFAULT
}

function loadJobsCache(): DownloadJob[] {
  try {
    const raw = localStorage.getItem(LS_JOBS)
    if (!raw) return []
    const arr = JSON.parse(raw) as unknown
    if (!Array.isArray(arr)) return []
    return arr
      .filter((j): j is DownloadJob => !!j && typeof j === 'object' && typeof (j as DownloadJob).id === 'string')
      .slice(0, 100)
      .map((j) => {
        // F5: job đang chạy không resume → hiện gián đoạn
        if (j.status === 'queued' || j.status === 'running') {
          return {
            ...j,
            status: 'error' as const,
            progress: 0,
            message: j.message || 'Gián đoạn — F5 / reload',
          }
        }
        return j
      })
  } catch {
    return []
  }
}

function saveJobsCache(jobs: DownloadJob[]) {
  try {
    const slim = jobs.slice(0, 100).map((j) => ({
      id: j.id,
      url: j.url,
      title: j.title,
      quality: j.quality,
      format: j.format,
      status: j.status,
      progress: j.progress,
      message: j.message,
      outputPath: j.outputPath,
      downloadUrl: j.downloadUrl,
      createdAt: j.createdAt,
      log: (j.log || []).slice(-20),
    }))
    localStorage.setItem(LS_JOBS, JSON.stringify(slim))
  } catch {
    /* quota */
  }
}

const DEFAULT_OPTS: DownloadOpts = {
  format: 'mp4',
  writeSubs: true,
  writeInfoJson: false,
  writeThumbnail: false,
  mergeAv: true, // YouTube DASH cần ghép video+audio
  preferFreeFormats: false,
  folderBySource: false,
}

function loadOpts(): DownloadOpts {
  try {
    const raw = localStorage.getItem(LS_OPTS)
    if (!raw) return { ...DEFAULT_OPTS }
    return { ...DEFAULT_OPTS, ...JSON.parse(raw) }
  } catch {
    return { ...DEFAULT_OPTS }
  }
}

function parseUrls(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const line of text.split(/\r?\n/)) {
    const u = line.trim()
    if (!u || u.startsWith('#')) continue
    if (!/^https?:\/\//i.test(u)) continue
    if (seen.has(u)) continue
    seen.add(u)
    out.push(u)
  }
  return out
}

function statusLabel(s: DownloadJob['status']): string {
  switch (s) {
    case 'queued':
      return 'Chờ'
    case 'running':
      return 'Đang xử lý'
    case 'done':
      return 'Hoàn thành'
    case 'error':
      return 'Lỗi'
    default:
      return s
  }
}

function IconFile() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6M8 13h8M8 17h5" />
    </svg>
  )
}

function IconTrash() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />
    </svg>
  )
}

function IconDownload() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden>
      <path d="M12 3v12M7 11l5 5 5-5M5 21h14" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function IconBulb() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5V15h8v-1.5A6 6 0 0 0 12 3z"
        stroke="#3B82F6"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      <path d="M10 15h4" stroke="#3B82F6" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

function EmptyArt() {
  return (
    <div className="dl-empty-art" aria-hidden>
      <svg width="148" height="128" viewBox="0 0 148 128" fill="none">
        <ellipse cx="74" cy="110" rx="40" ry="7" fill="#E0ECFF" opacity="0.85" />
        <path
          d="M34 52h30l9 9h42a9 9 0 0 1 9 9v30a9 9 0 0 1-9 9H34a9 9 0 0 1-9-9V61a9 9 0 0 1 9-9z"
          fill="#E8F1FF"
          stroke="#3B82F6"
          strokeWidth="2.4"
        />
        <path d="M25 66h98" stroke="#93C5FD" strokeWidth="1.5" />
        <circle cx="74" cy="84" r="17" fill="#3B82F6" />
        <path d="M68 76v16l13-8-13-8z" fill="#fff" />
        <rect x="92" y="28" width="24" height="18" rx="4" fill="#DBEAFE" stroke="#60A5FA" strokeWidth="1.4" />
        <circle cx="104" cy="37" r="4" fill="#3B82F6" opacity="0.9" />
        <rect x="114" y="22" width="16" height="20" rx="3" fill="#DBEAFE" stroke="#60A5FA" strokeWidth="1.4" />
        <path d="M118 29h8M118 34h6" stroke="#3B82F6" strokeWidth="1.3" strokeLinecap="round" />
        <circle cx="44" cy="34" r="10" fill="#FEF3C7" stroke="#FBBF24" strokeWidth="1.2" />
        <path d="M41 34h6M44 31v6" stroke="#F59E0B" strokeWidth="1.4" strokeLinecap="round" />
        <path d="M48 22l3-5 3 5" stroke="#FBBF24" strokeWidth="1.5" strokeLinecap="round" />
        <path d="M122 52l3-5 3 5" stroke="#FBBF24" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    </div>
  )
}

type Props = {
  onBack: () => void
  onUseInClone?: (projectId: string, meta: { videoUrl: string; duration: number; segments?: unknown[]; settings?: Record<string, unknown> }) => void
}

export default function DownloadStudio({ onBack, onUseInClone }: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [text, setText] = useState(() => {
    try {
      return localStorage.getItem(LS_LINKS) || ''
    } catch {
      return ''
    }
  })
  const [useBusyId, setUseBusyId] = useState<string | null>(null)
  const [quality, setQuality] = useState<DownloadQuality>('1080')
  const [opts, setOpts] = useState<DownloadOpts>(loadOpts)
  const [jobs, setJobs] = useState<DownloadJob[]>(loadJobsCache)
  const [filter, setFilter] = useState<JobFilter>('all')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [logOpen, setLogOpen] = useState(false)
  const [savePath, setSavePath] = useState(() => {
    try {
      return localStorage.getItem(LS_PATH) || ''
    } catch {
      return ''
    }
  })
  const [pathBusy, setPathBusy] = useState(false)
  const [pathMsg, setPathMsg] = useState('')
  const [isDesktopApp, setIsDesktopApp] = useState(false)
  const [taH, setTaH] = useState(loadTextareaH)
  const fileRef = useRef<HTMLInputElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const pollRef = useRef<number | null>(null)
  const onRealtime = useCallback((event: { type: string; payload: unknown; entityId: string }) => {
    if (event.type === 'snapshot') {
      const snapshot = event.payload as { jobs?: DownloadJob[] } | null
      if (Array.isArray(snapshot?.jobs)) setJobs(snapshot.jobs)
      return
    }
    if (event.type === 'download.job.deleted') {
      setJobs(current => current.filter(job => job.id !== event.entityId))
      return
    }
    const job = event.payload as DownloadJob | null
    if (job?.id) setJobs(current => [...current.filter(item => item.id !== job.id), job].sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt))))
  }, [])
  const realtimeStatus = useRealtimeEvents('download', onRealtime)

  function persistTextareaH() {
    const el = taRef.current
    if (!el) return
    const h = Math.round(el.offsetHeight)
    if (!Number.isFinite(h) || h < TA_H_MIN || h > TA_H_MAX) return
    setTaH(h)
    try {
      localStorage.setItem(LS_TA_H, String(h))
    } catch {
      /* ignore */
    }
  }

  const urlCount = useMemo(() => parseUrls(text).length, [text])

  useEffect(() => {
    try {
      localStorage.setItem(LS_OPTS, JSON.stringify(opts))
    } catch {
      /* ignore */
    }
  }, [opts])

  useEffect(() => {
    try {
      if (savePath.trim()) localStorage.setItem(LS_PATH, savePath.trim())
      else localStorage.removeItem(LS_PATH)
    } catch {
      /* ignore */
    }
  }, [savePath])

  useEffect(() => {
    try {
      localStorage.setItem(LS_LINKS, text)
    } catch {
      /* ignore */
    }
  }, [text])

  useEffect(() => {
    saveJobsCache(jobs)
  }, [jobs])

  const refresh = useCallback(async () => {
    try {
      const list = await downloadApi.list()
      setJobs(list)
      saveJobsCache(list)
    } catch {
      /* giữ cache localStorage khi API fail */
    }
  }, [])

  useEffect(() => {
    void refresh()
    void fetch('/api/config').then(async (response) => {
      if (!response.ok) return
      const config = await response.json() as { desktop?: boolean }
      setIsDesktopApp(Boolean(config.desktop))
    }).catch(() => undefined)
    void downloadApi
      .root()
      .then((r) => {
        const p = r.path || r.display || r.relative
        if (!p) return
        setSavePath((prev) => {
          if (prev.trim()) return prev
          try {
            localStorage.setItem(LS_PATH, p)
          } catch {
            /* ignore */
          }
          return p
        })
      })
      .catch(() => {})
  }, [refresh])

  async function applySavePath(next?: string, syncState = true) {
    const path = (next ?? savePath).trim()
    if (!path) {
      setPathMsg('Nhập đường dẫn thư mục trên máy chạy backend.')
      return
    }
    setPathBusy(true)
    setPathMsg('')
    setError('')
    try {
      localStorage.setItem(LS_PATH, path)
      const r = await downloadApi.setRoot(path)
      const p = r.path || r.display || path
      if (syncState) setSavePath(p)
      localStorage.setItem(LS_PATH, p)
      setPathMsg('Đã lưu thư mục.')
    } catch (err) {
      setPathMsg(err instanceof Error ? err.message : 'Không lưu được thư mục')
    } finally {
      setPathBusy(false)
    }
  }

  async function onPickFolder() {
    try {
      const picked = await downloadApi.pickFolder()
      if (picked.path) {
        await applySavePath(picked.path, false)
        return picked.path
      }
      return undefined // user cancelled the native picker
    } catch {
      // Browser/remote backend fallback below.
    }
    // Fallback only when a native desktop picker is unavailable.
    try {
      await downloadApi.revealRoot()
    } catch {
      /* ignore */
    }
    const next = window.prompt('Dán đường dẫn thư mục lưu trên máy server:', savePath || '')
    if (next != null && next.trim()) {
      const path = next.trim()
      await applySavePath(path, false)
      return path
    }
  }

  useEffect(() => {
    const need = jobs.some((j) => ACTIVE.has(j.status))
    if (!need || realtimeStatus === 'connected') {
      if (pollRef.current != null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
      return
    }
    if (pollRef.current != null) return
    pollRef.current = window.setInterval(() => {
      void refresh()
    }, 10000)
    return () => {
      if (pollRef.current != null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [jobs, refresh, realtimeStatus])

  function patchOpt<K extends keyof DownloadOpts>(key: K, val: DownloadOpts[K]) {
    setOpts((o) => ({ ...o, [key]: val }))
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    const urls = parseUrls(text)
    if (!urls.length) {
      setError('Dán ít nhất một link http(s) — mỗi dòng một URL.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const q = opts.format === 'mp3' ? ('audio' as DownloadQuality) : quality
      const created = await downloadApi.start({ urls, quality: q, ...opts })
      setJobs((prev) => {
        const ids = new Set(created.map((j) => j.id))
        return [...created, ...prev.filter((j) => !ids.has(j.id))]
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Không gửi được job tải.')
    } finally {
      setBusy(false)
    }
  }

  async function onCancel(id: string) {
    try {
      await downloadApi.cancel(id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Hủy thất bại')
    }
  }

  async function onRemove(id: string) {
    try {
      await downloadApi.remove(id)
      // làm sạch hẳn job khỏi UI + file (API đã xóa disk)
      setJobs((prev) => prev.filter((j) => j.id !== id))
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Xóa thất bại')
    }
  }

  async function onUse(id: string) {
    if (!onUseInClone) {
      setError('Không gắn được Clone Video — reload app.')
      return
    }
    setUseBusyId(id)
    setError('')
    try {
      const res = await downloadApi.toProject(id)
      onUseInClone(res.projectId, {
        videoUrl: res.videoUrl,
        duration: res.duration || 0,
        segments: res.segments,
        settings: res.settings,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Không mở được trong Clone Video')
    } finally {
      setUseBusyId(null)
    }
  }

  async function onClearDone() {
    try {
      await downloadApi.clearDone()
      setJobs((prev) => prev.filter((j) => j.status !== 'done' && j.status !== 'error'))
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Xóa thất bại')
    }
  }

  function onImportTxt(file: File | null) {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      const t = String(reader.result || '')
      setText((prev) => (prev.trim() ? `${prev.trim()}\n${t}` : t))
    }
    reader.readAsText(file)
  }

  const counts = useMemo(() => {
    const all = jobs.length
    const active = jobs.filter((j) => ACTIVE.has(j.status)).length
    const done = jobs.filter((j) => j.status === 'done').length
    const err = jobs.filter((j) => j.status === 'error').length
    return { all, active, done, err }
  }, [jobs])

  const filtered = useMemo(() => {
    if (filter === 'all') return jobs
    if (filter === 'active') return jobs.filter((j) => ACTIVE.has(j.status))
    if (filter === 'done') return jobs.filter((j) => j.status === 'done')
    return jobs.filter((j) => j.status === 'error')
  }, [jobs, filter])

  const logLines = useMemo(() => {
    const lines: string[] = []
    for (const j of jobs.slice(0, 12)) {
      lines.push(`[${j.id.slice(0, 6)}] ${j.title || j.url}`)
      for (const L of j.log || []) lines.push(`  ${L}`)
    }
    return lines
  }, [jobs])

  const detailLog = logLines.join('\n')

  async function copyDetailLog() {
    if (!detailLog) return
    try {
      await copyText(detailLog, t('Đã sao chép log chi tiết.', 'Detailed log copied.'))
    } catch {
      setError(t('Không thể sao chép log', 'Could not copy log'))
    }
  }

  async function clearDetailLog() {
    try {
      await downloadApi.clearLogs()
      setJobs((previous) => previous.map((job) => ({ ...job, log: [] })))
      setLogOpen(true)
      toast.success(t('Đã xóa log.', 'Logs cleared.'))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('Không thể xóa log', 'Could not clear logs'))
    }
  }

  function downloadOutput(url: string) {
    const link = document.createElement('a')
    link.href = url
    link.download = ''
    link.hidden = true
    document.body.appendChild(link)
    link.click()
    link.remove()
  }

  return (
    <div className="dl-studio">
      <div className="dl-shell">
        <div className="dl-head">
          <BackTitle onBack={onBack}>Download Video</BackTitle>
          <p>
            Dán link YouTube, TikTok, Douyin, Bilibili…
          </p>
        </div>

        <div className="dl-grid">
          <div className="dl-col">
            <section className="dl-panel">
              <div className="dl-panel-h">
                <h2>
                  <span className="dl-num">1</span>
                  Nguồn
                </h2>
                <span className="dl-badge">{urlCount} link</span>
              </div>
              <p className="dl-sublabel">Dán link (mỗi dòng một link)</p>
              <textarea
                ref={taRef}
                className="dl-textarea"
                value={text}
                onChange={(e) => setText(e.target.value)}
                onMouseUp={persistTextareaH}
                onTouchEnd={persistTextareaH}
                onBlur={persistTextareaH}
                rows={7}
                style={{ height: taH }}
                placeholder={
                  'https://www.youtube.com/watch?v=…\nhttps://www.tiktok.com/@…/video/…\n# dòng trống hoặc # để bỏ qua'
                }
                spellCheck={false}
              />
              <div className="dl-toolbar">
                <input
                  ref={fileRef}
                  type="file"
                  accept=".txt,text/plain"
                  hidden
                  onChange={(e) => {
                    onImportTxt(e.target.files?.[0] ?? null)
                    e.target.value = ''
                  }}
                />
                <button type="button" className="dl-btn outline" onClick={() => fileRef.current?.click()}>
                  <IconFile />
                  Nhập từ file .txt
                </button>
                <button
                  type="button"
                  className="dl-btn danger-ghost"
                  disabled={!text}
                  onClick={() => setText('')}
                >
                  <IconTrash />
                  Xóa tất cả
                </button>
              </div>
            </section>

            <section className="dl-panel">
              <div className="dl-panel-h">
                <h2>
                  <span className="dl-num">2</span>
                  Tùy chọn tải
                </h2>
              </div>

              <div className="dl-fields-2">
                <label className="dl-field">
                  <span>Chất lượng video</span>
                  <select
                    value={quality}
                    disabled={opts.format === 'mp3'}
                    onChange={(e) => setQuality(e.target.value as DownloadQuality)}
                  >
                    <option value="best">Tốt nhất</option>
                    <option value="2160">4K (2160p)</option>
                    <option value="1440">2K (1440p)</option>
                    <option value="1080">1080p (HD)</option>
                    <option value="720">720p</option>
                    <option value="480">480p</option>
                  </select>
                </label>
                <label className="dl-field">
                  <span>Định dạng</span>
                  <select
                    value={opts.format}
                    onChange={(e) => patchOpt('format', e.target.value as DownloadFormat)}
                  >
                    <option value="mp4">MP4</option>
                    <option value="mkv">MKV</option>
                    <option value="webm">WEBM</option>
                    <option value="mp3">MP3 (audio)</option>
                  </select>
                </label>
              </div>

              <OutputFolderField isDesktopApp={isDesktopApp} value={savePath} onChange={(value) => { setSavePath(value); setPathMsg(''); localStorage.setItem(LS_PATH, value) }} onChoose={isDesktopApp ? onPickFolder : undefined} defaultPath={t('Ví dụ: kenh-a hoặc video-01.mp4', 'Example: channel-a or video-01.mp4')} appFolder="download-video" disabled={pathBusy} />
              {pathMsg && <span className="dl-path-msg">{pathMsg}</span>}

              <div className="dl-checks">
                <label className="dl-check" title="Tải .srt + nhúng phụ đề (nếu có)">
                  <input
                    type="checkbox"
                    checked={opts.writeSubs}
                    onChange={(e) => patchOpt('writeSubs', e.target.checked)}
                  />
                  Tải phụ đề (nếu có)
                </label>
                <label className="dl-check" title="Ghi .info.json, .description, embed metadata">
                  <input
                    type="checkbox"
                    checked={opts.writeInfoJson}
                    onChange={(e) => patchOpt('writeInfoJson', e.target.checked)}
                  />
                  Ghi metadata
                </label>
                <label className="dl-check" title="Tải thumbnail .jpg (+ embed nếu được)">
                  <input
                    type="checkbox"
                    checked={opts.writeThumbnail}
                    onChange={(e) => patchOpt('writeThumbnail', e.target.checked)}
                  />
                  Thumbnail
                </label>
                <label
                  className="dl-check"
                  title="Bật: DASH bv+ba merge (YouTube). Tắt: ưu tiên 1 file progressive"
                >
                  <input
                    type="checkbox"
                    checked={opts.mergeAv}
                    onChange={(e) => patchOpt('mergeAv', e.target.checked)}
                  />
                  Ghép video + audio
                </label>
                <label className="dl-check" title="Chọn format nhỏ hơn / sort theo bitrate">
                  <input
                    type="checkbox"
                    checked={opts.preferFreeFormats}
                    onChange={(e) => patchOpt('preferFreeFormats', e.target.checked)}
                  />
                  Tối ưu dung lượng
                </label>
                <label className="dl-check" title="downloads/youtube.com/… thay vì downloads/{id}">
                  <input
                    type="checkbox"
                    checked={opts.folderBySource}
                    onChange={(e) => patchOpt('folderBySource', e.target.checked)}
                  />
                  Tạo folder theo nguồn
                </label>
              </div>

              <form className="dl-form-actions" onSubmit={onSubmit}>
                <button type="submit" className="dl-btn primary block" disabled={busy || urlCount === 0}>
                  <IconDownload />
                  {busy ? 'Đang gửi…' : 'Tải video'}
                </button>
                <button
                  type="button"
                  className="dl-btn outline block"
                  disabled={!text && jobs.length === 0}
                  onClick={() => {
                    setText('')
                    void onClearDone()
                  }}
                >
                  <IconTrash />
                  Xóa danh sách
                </button>
              </form>
              {error && <p className="dl-error">{error}</p>}
            </section>
          </div>

          <div className="dl-col">
            <section className="dl-panel dl-panel-results">
              <div className="dl-panel-h">
                <h2>
                  <span className="dl-num">3</span>
                  Kết quả
                </h2>
                <div className="dl-results-tools">
                  <span className="dl-badge">
                    {counts.done}/{counts.all || 0}
                  </span>
                  <button
                    type="button"
                    className="dl-btn danger-ghost sm"
                    disabled={!counts.done && !counts.err}
                    onClick={() => void onClearDone()}
                  >
                    <IconTrash />
                    Làm sạch
                  </button>
                </div>
              </div>

              <div className="dl-tabs" role="tablist">
                {(
                  [
                    ['all', `Tất cả (${counts.all})`],
                    ['active', `Đang xử lý (${counts.active})`],
                    ['done', `Hoàn thành (${counts.done})`],
                    ['error', `Lỗi (${counts.err})`],
                  ] as const
                ).map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    role="tab"
                    className={filter === id ? 'active' : undefined}
                    aria-selected={filter === id}
                    onClick={() => setFilter(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>

              <div className="dl-table-wrap">
                <div className="dl-table-head" aria-hidden>
                  <span>#</span>
                  <span>Link</span>
                  <span>Trạng thái</span>
                  <span>Tiến độ</span>
                  <span>Thông tin</span>
                  <span>Thao tác</span>
                </div>

                {filtered.length === 0 ? (
                  <div className="dl-empty">
                    <EmptyArt />
                    <strong>Chưa có job nào</strong>
                    <p>Dán link bên trái và bấm &quot;Tải video&quot; để bắt đầu.</p>
                  </div>
                ) : (
                  <ul className="dl-jobs">
                    {filtered.map((j, i) => (
                      <li key={j.id} className={`dl-job-row is-${j.status}`}>
                        <span className="dl-c-num">{i + 1}</span>
                        <span className="dl-c-link" title={j.url}>
                          <strong>{j.title || j.url}</strong>
                          {j.title ? <em>{j.url}</em> : null}
                        </span>
                        <span className={`dl-st dl-st-${j.status}`}>{statusLabel(j.status)}</span>
                        <span className="dl-c-prog">
                          {ACTIVE.has(j.status) ? (
                            <>
                              <span className="dl-bar">
                                <i style={{ width: `${Math.max(3, j.progress)}%` }} />
                              </span>
                              <small>{Math.round(j.progress)}%</small>
                            </>
                          ) : j.status === 'done' ? (
                            '100%'
                          ) : (
                            '—'
                          )}
                        </span>
                        <span className="dl-c-info" title={j.message || ''}>
                          {j.message || `${j.quality}${j.format ? ` · ${j.format}` : ''}`}
                        </span>
                        <span className="dl-c-act">
                          {j.status === 'done' && (
                            <>
                              {!isDesktopApp && <button type="button" className="dl-result-action" title={t('Tải file theo tên đầu ra đã chọn', 'Download using the selected output name')} onClick={() => downloadOutput(j.downloadUrl || downloadApi.fileUrl(j.id))}>{t('Tải xuống', 'Download')}</button>}
                              <button
                                type="button"
                                className="dl-result-action"
                                title={t('Dùng video này trong tab Clone Video', 'Use this video in Clone Video')}
                                disabled={useBusyId === j.id}
                                onClick={() => void onUse(j.id)}
                              >
                                {useBusyId === j.id ? '…' : 'Sử dụng'}
                              </button>
                              {isDesktopApp && <button
                                type="button"
                                className="dl-result-action"
                                title={t('Mở thư mục chứa file trên máy tính', 'Open the output folder on this computer')}
                                onClick={() => {
                                  void downloadApi.openFile(j.id).catch((err) => {
                                    setError(
                                      err instanceof Error ? err.message : t('Không mở được file', 'Could not open file'),
                                    )
                                  })
                                }}
                              >
                                {t('Mở thư mục', 'Open folder')}
                              </button>}
                            </>
                          )}
                          {ACTIVE.has(j.status) && (
                            <button
                              type="button"
                              className="dl-result-action danger"
                              onClick={() => void onCancel(j.id)}
                            >
                              {t('Hủy', 'Cancel')}
                            </button>
                          )}
                          {(j.status === 'done' || j.status === 'error') && (
                            <button
                              type="button"
                              className="dl-result-action danger"
                              title={t('Xóa job và file trên ổ đĩa', 'Delete the job and file from disk')}
                              onClick={() => void onRemove(j.id)}
                            >
                              {t('Xóa', 'Delete')}
                            </button>
                          )}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <details
                className="dl-log"
                open={logOpen}
                onToggle={(e) => setLogOpen((e.target as HTMLDetailsElement).open)}
              >
                <summary>
                  <span>{t('Log chi tiết', 'Detailed log')}</span>
                  <span className="dl-log-actions" onClick={(event) => event.preventDefault()}>
                    <button type="button" onClick={() => void copyDetailLog()} disabled={!detailLog}>
                      {t('Sao chép', 'Copy')}
                    </button>
                    <button type="button" className="danger" onClick={() => void clearDetailLog()} disabled={!detailLog}>
                      {t('Xóa', 'Clear')}
                    </button>
                  </span>
                </summary>
                <pre>{detailLog || t('Chưa có log.', 'No logs yet.')}</pre>
              </details>
            </section>
          </div>
        </div>

        <p className="dl-tip">
          <IconBulb />
          <span>
            <strong>Mẹo:</strong> Bạn có thể dán link kênh, playlist hoặc nhiều link.
            <br />
            Hệ thống sẽ tự động phân tích và thêm vào danh sách.
          </span>
        </p>
      </div>
    </div>
  )
}
