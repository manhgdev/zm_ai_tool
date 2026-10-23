import { useEffect, useState, type ReactNode } from 'react'
import { cn } from '@/shared/lib/cn'

import { localize, useLocale } from '@/app/i18n'
import { copyText } from '@/shared/lib/clipboard'
import { diskSpaceError, type DiskSpaceInfo } from '@/shared/lib/diskSpace'
import { useStickToBottomScroll } from '@/shared/hooks/useStickToBottomScroll'
import './ProgressPopup.css'

export type ProgressPopupProps = {
  /** Job đang chạy hoặc vừa lỗi cần hiện UI */
  active: boolean
  /** true = chỉ hiện pill góc; false = popup giữa màn */
  minimized: boolean
  hideMinimized?: boolean
  title?: string
  message?: string
  /** 0–100 */
  progress: number
  error?: string | null
  diskSpace?: DiskSpaceInfo
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
  actions?: ReactNode
  details?: ReactNode
  wide?: boolean
  collapseLog?: boolean
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
  hideMinimized = false,
  title = 'Đang xử lý',
  message,
  progress,
  error,
  diskSpace,
  running = false,
  log,
  onMinimize,
  onRestore,
  onCancel,
  className,
  actions,
  details,
  wide = false,
  collapseLog = false,
}: ProgressPopupProps) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [elapsed, setElapsed] = useState(0)
  const [copied, setCopied] = useState(false)
  const [dismissedDiskError, setDismissedDiskError] = useState('')
  const disk = diskSpaceError([error, message].filter(Boolean).join('\n'), diskSpace)
  const diskKey = disk ? JSON.stringify([error, disk.diskPath, disk.diskDrive]) : ''
  const { ref: logRef, onScroll: onLogScroll, onWheel: onLogWheel, onPointerDown: onLogPointerDown, onPointerUp: onLogPointerUp } = useStickToBottomScroll(log)

  useEffect(() => {
    if (!active || !diskKey) setDismissedDiskError('')
  }, [active, diskKey])

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

  if (!active) return null

  const pct = clampPct(progress)
  const failed = Boolean(disk || error && error !== 'cancelled')
  const displayTitle = disk
    ? disk.diskDrive ? t(`Ổ ${disk.diskDrive} không đủ dung lượng`, `Drive ${disk.diskDrive} is out of space`)
      : t('Không đủ dung lượng ổ đĩa', 'Not enough disk space')
    : title
  const popupActions = disk ? undefined : actions
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
  const base = disk ? displayTitle : sanitizeJobMessage(rawBase)
  const compactLine =
    running && !failed
      ? locale === 'en' ? ` · ran ${fmtElapsed(elapsed)} · still processing` : ` · đã chạy ${fmtElapsed(elapsed)} · vẫn đang xử lý`
      : ''
  const line = base

  async function copyError() {
    const text = [displayTitle, base, disk?.diskPath, rawBase && rawBase !== base ? rawBase : '', disk ? log : '']
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

  function close() {
    setDismissedDiskError(diskKey)
    const dismiss = onCancel ?? onMinimize
    dismiss()
  }

  // A disk failure must surface even when the operation was sent to the background.
  if (minimized && (!disk || dismissedDiskError === diskKey)) {
    if (hideMinimized) return null
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
      aria-label={displayTitle}
    >
      <div className={cn('w-full rounded-lg border border-border bg-background shadow-xl', wide ? 'max-w-[720px]' : 'max-w-sm', disk && 'max-h-[calc(100dvh-2rem)] overflow-y-auto')}>
        <div className="flex items-start gap-2 border-b border-border px-4 py-3">
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-foreground">{displayTitle}</h3>
            {!failed && line ? (
              <p className="mt-0.5 text-xs text-muted-foreground leading-snug break-words select-text">{line}</p>
            ) : null}
            {disk ? (
              <div role="alert" className="mt-2 space-y-2 text-xs leading-relaxed select-text">
                <p>{t('Không thể tiếp tục ghi dữ liệu. Hãy giải phóng dung lượng rồi thử lại.', 'Cannot continue writing data. Free up space, then retry.')}</p>
                {disk.diskPath ? <p className="break-all"><strong>{t('Đường dẫn đang ghi:', 'Write location:')}</strong> {disk.diskPath}</p>
                  : <p>{t('Chưa xác định được đường dẫn đang ghi. Xem chi tiết lỗi để kiểm tra ổ đĩa.', 'The write location is unavailable. Check the error details to identify the disk.')}</p>}
                {disk.freeDiskBytes != null && <p>{t('Còn trống:', 'Available:')} {(disk.freeDiskBytes / 1073741824).toFixed(2)} GiB</p>}
                {Boolean(disk.requiredDiskBytes) && <p>{t('Cần tối thiểu:', 'Required:')} {((disk.requiredDiskBytes || 0) / 1073741824).toFixed(2)} GiB</p>}
                <details>
                  <summary className="cursor-pointer font-medium">{t('Cài hoặc lưu sang ổ khác', 'Install or save to another drive')}</summary>
                  <ul className="mt-1 list-disc space-y-2 pl-4">
                    <li>{t('File xuất: chọn Thư mục xuất trên ổ còn trống.', 'Output files: choose an Output folder on a drive with free space.')}</li>
                    <li>{t('APP Windows: đóng APP, chạy Setup và chọn thư mục cài trên ổ khác. Model, runtime và cache sẽ lưu cùng thư mục cài APP.', 'Windows app: close the app, run Setup and choose a folder on another drive. Models, runtime and cache are stored with the app installation.')}</li>
                    <li>{t('Trước khi đổi nơi cài, sao lưu thư mục user-data cũ. Bộ cài không tự chuyển dữ liệu sang thư mục mới.', 'Back up the existing user-data folder before changing the installation location. Setup does not move existing data to the new folder.')}</li>
                    <li>{t('Web: nhờ quản trị viên giải phóng hoặc chuyển dữ liệu trên máy chủ.', 'Web: ask the administrator to free space or move storage on the server.')}</li>
                  </ul>
                </details>
                <details>
                  <summary className="cursor-pointer">{t('Chi tiết lỗi', 'Error details')}</summary>
                  <pre className="mt-1 max-h-36 overflow-y-auto rounded bg-muted/60 p-2 text-[10px] font-mono whitespace-pre-wrap break-all">{[rawBase, log].filter(Boolean).join('\n')}</pre>
                </details>
              </div>
            ) : failed && rawBase ? (
              <pre className="mt-2 max-h-36 overflow-y-auto rounded bg-muted/60 p-2 text-[10px] leading-relaxed font-mono whitespace-pre-wrap break-all select-text">{rawBase}</pre>
            ) : null}
          </div>
          <button
            type="button"
            className="shrink-0 rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={onCancel ? t('Hủy', 'Cancel') : t('Đóng', 'Close')}
            aria-label={onCancel ? t('Hủy', 'Cancel') : t('Đóng', 'Close')}
            autoFocus={Boolean(disk)}
            onClick={close}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {!disk && <div className="px-4 py-4 space-y-2">
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
          {details}
          {log && collapseLog ? (
            <details className="text-xs">
              <summary>{t('Chi tiết lỗi', 'Error details')}</summary>
              <div className={cn('progress-popup-log', wide && 'progress-popup-log-wide')}>
                <pre>{log}</pre>
              </div>
            </details>
          ) : log ? (
            <div
              ref={logRef}
              onScroll={onLogScroll}
              onWheel={onLogWheel}
              onPointerDown={onLogPointerDown}
              onPointerUp={onLogPointerUp}
              onPointerCancel={onLogPointerUp}
              className={cn('progress-popup-log', wide && 'progress-popup-log-wide')}
            >
              <pre>{log}</pre>
            </div>
          ) : null}
        </div>}

        <div className={cn('flex items-center justify-end gap-2 border-t border-border px-4 py-3', popupActions && 'flex-wrap')}>
          {popupActions ?? (failed ? (
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
                onClick={close}
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
          ))}
        </div>
      </div>
    </div>
  )
}
