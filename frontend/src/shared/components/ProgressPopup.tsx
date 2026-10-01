import { useEffect, useRef, useState } from 'react'
import { cn } from '@/shared/lib/cn'

import { localize, useLocale } from '@/app/i18n'
import { copyText } from '@/shared/lib/clipboard'

export type ProgressPopupProps = {
  /** Job đang chạy hoặc vừa lỗi cần hiện UI */
  active: boolean
  /** true = chỉ hiện pill góc; false = popup giữa màn */
  minimized: boolean
  title?: string
  message?: string
  /** 0–100 */
  progress: number
  error?: string | null
  /** Job còn chạy (khác lỗi đã xong) — bật đồng hồ / heartbeat */
  running?: boolean
  /** Log stream (pip output, v.v.) hiện terminal nhỏ trong popup */
  log?: string
  /** Chỉ "Chạy nền" ẩn popup, job vẫn chạy */
  onMinimize: () => void
  /** Click pill → mở lại popup */
  onRestore: () => void
  /** Huỷ job (tuỳ chọn) */
  onCancel?: () => void
  className?: string
}

function clampPct(n: number) {
  if (!Number.isFinite(n)) return 0
  return Math.max(0, Math.min(100, Math.round(n)))
}

/** Không dump cả argv ffmpeg / filter_complex vào popup. */
export function sanitizeJobMessage(raw?: string | null, limit = 220): string {
  if (!raw) return ''
  let t = String(raw).trim()
  if (!t) return ''
  const lower = t.toLowerCase()
  if (
    lower.includes('winerror 206')
    || lower.includes('filename or extension is too long')
    || (lower.includes('too long') && lower.includes('ffmpeg'))
  ) {
    return 'PATH Windows quá dài (WinError 206). App đã loại đường dẫn trùng; không cần cài lại gói AI.'
  }
  if (
    t.includes("Command '[")
    || t.includes('Command "[')
    || t.includes('-filter_complex')
    || t.includes('between(t')
    || t.includes('between(t\\')
  ) {
    const m = t.match(/exit status (-?\d+)/i) || t.match(/exit (\d+)/i)
    const code = m?.[1] ?? '?'
    if (lower.includes('ffmpeg')) {
      return `ffmpeg thất bại (exit ${code}). Xem log backend — không hiện đủ lệnh.`
    }
    return `Lệnh thất bại (exit ${code}).`
  }
  if (t.length > limit) t = `${t.slice(0, limit - 1)}…`
  return t
}

function fmtElapsed(sec: number) {
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}m${s.toString().padStart(2, '0')}s`
}

/**
 * Popup tiến độ tái sử dụng: % + X hủy + nút chạy nền.
 * Parent giữ state `minimized`; job không phụ thuộc vào việc đóng popup.
 * % đứng lâu vẫn hiện đồng hồ + "vẫn đang chạy" — tránh tưởng UI đơ.
 */
export default function ProgressPopup({
  active,
  minimized,
  title = 'Đang xử lý',
  message,
  progress,
  error,
  running = false,
  log,
  onMinimize,
  onRestore,
  onCancel,
  className,
}: ProgressPopupProps) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [elapsed, setElapsed] = useState(0)
  const [copied, setCopied] = useState(false)
  const logRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    if (!active || !running) {
      setElapsed(0)
      return
    }
    const t0 = Date.now()
    const id = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - t0) / 1000))
    }, 1000)
    return () => window.clearInterval(id)
  }, [active, running])

  // Auto-scroll log terminal to bottom on new output
  useEffect(() => {
    if (logRef.current && log) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [log])

  if (!active) return null

  const pct = clampPct(progress)
  const failed = Boolean(error && error !== 'cancelled')
  // Ưu tiên message dài; error code ngắn (dub/export) không che message
  const errStr = error && error !== 'cancelled' ? String(error).trim() : ''
  const msgStr = (message || '').trim()
  const rawBase =
    failed
      ? (
          msgStr && errStr && msgStr !== errStr && errStr.length <= 24
            ? msgStr
            : errStr || msgStr || title
        )
      : msgStr || title
  const base = sanitizeJobMessage(rawBase)
  const compactLine =
    running && !failed
      ? locale === 'en' ? ` · ran ${fmtElapsed(elapsed)} · still processing` : ` · đã chạy ${fmtElapsed(elapsed)} · vẫn đang xử lý`
      : ''
  const line = base

  async function copyError() {
    const text = [title, base, rawBase && rawBase !== base ? rawBase : '']
      .filter(Boolean)
      .join('\n')
    try {
      await copyText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* ignore */
    }
  }

  if (minimized) {
    return (
      <button
        type="button"
        className={cn(
          'fixed bottom-4 right-4 z-[200] flex items-center gap-2 rounded-lg border border-border bg-background/95 px-3 py-2 text-left shadow-lg backdrop-blur-sm',
          'hover:bg-accent/40 transition-colors max-w-[min(360px,90vw)]',
          className,
        )}
        onClick={onRestore}
        title={t('Mở lại tiến độ', 'Restore progress')}
      >
        <span
          className={cn(
            'h-2 w-2 shrink-0 rounded-full',
            failed ? 'bg-destructive' : 'bg-primary animate-pulse',
          )}
        />
        <span className="min-w-0 flex-1 truncate text-xs text-foreground">{`${line}${compactLine}`}</span>
        <span className="shrink-0 tabular-nums text-xs font-medium text-muted-foreground">{pct}%</span>
      </button>
    )
  }

  return (
    <div
      className={cn(
        'fixed inset-0 z-[200] flex items-center justify-center bg-black/45 p-4',
        className,
      )}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="w-full max-w-sm rounded-lg border border-border bg-background shadow-xl">
        <div className="flex items-start gap-2 border-b border-border px-4 py-3">
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-foreground">{title}</h3>
            {!failed && line ? (
              <p className="mt-0.5 text-xs text-muted-foreground leading-snug break-words select-text">{line}</p>
            ) : null}
            {failed && rawBase ? (
              <pre className="mt-2 max-h-36 overflow-y-auto rounded bg-muted/60 p-2 text-[10px] leading-relaxed font-mono whitespace-pre-wrap break-all select-text">{rawBase}</pre>
            ) : null}
          </div>
          <button
            type="button"
            className="shrink-0 rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={onCancel ? t('Hủy', 'Cancel') : t('Đóng', 'Close')}
            aria-label={onCancel ? t('Hủy', 'Cancel') : t('Đóng', 'Close')}
            onClick={onCancel ?? onMinimize}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="px-4 py-4 space-y-2">
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-xs text-muted-foreground">
              {running && !failed ? (locale === 'en' ? `Ran ${fmtElapsed(elapsed)}` : `Đã chạy ${fmtElapsed(elapsed)}`) : t('Tiến độ', 'Progress')}
            </span>
            <span className="text-sm font-semibold tabular-nums text-foreground">{pct}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={cn(
                'h-full rounded-full transition-[width] duration-200 ease-out',
                failed ? 'bg-destructive' : 'bg-primary',
                running && !failed && pct > 0 && pct < 100 ? 'animate-pulse' : '',
              )}
              style={{ width: `${Math.max(pct, running && !failed ? 4 : 0)}%` }}
            />
          </div>
          {log && (
            <pre
              ref={logRef}
              className="mt-1 max-h-40 cursor-text select-text overflow-y-auto rounded bg-black/85 p-2 text-[10px] leading-relaxed text-green-400 font-mono whitespace-pre-wrap break-all"
            >{log}</pre>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          {failed ? (
            <>
              <button
                type="button"
                className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                onClick={() => void copyError()}
              >
                {copied ? t('Đã chép', 'Copied') : t('Chép lỗi', 'Copy error')}
              </button>
              <button
                type="button"
                className="rounded-md border border-border bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90"
                onClick={onCancel ?? onMinimize}
              >
                OK
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="rounded-md border border-border bg-accent/40 px-3 py-1.5 text-xs hover:bg-accent"
                onClick={onMinimize}
              >
                {t('Chạy nền', 'Run in background')}
              </button>
              {onCancel ? (
                <button
                  type="button"
                  className="rounded-md border border-destructive/50 bg-destructive/15 px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/25"
                  onClick={onCancel}
                  title={t('Dừng job — kill ffmpeg/TTS/OCR', 'Stop job — stop ffmpeg/TTS/OCR')}
                >
                  {t('Huỷ', 'Cancel')}
                </button>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
