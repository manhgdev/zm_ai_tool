import React, { useState, useEffect } from 'react'
import { api } from '@/features/project/project.api'
import { createPortal } from 'react-dom'
import type { ProjectSettings } from '../project/project.types'
import { CoverPickerModal } from './CoverPickerModal'
import { localize, useLocale } from '@/app/i18n'
import { OutputFolderField } from '@/shared/components/OutputFolderField'

export interface ExportModalOptions {
  renderName: string
  exportResolution: string
  exportVideo: boolean
  exportVideoFormat: string
  exportAudio: boolean
  exportAudioFormat: string
  exportSrt: boolean
  exportSrtFormat: string
  exportGif: boolean
  exportGifRes: string
  exportOutputDir: string
  coverDataUrl?: string
}

interface ExportModalProps {
  isOpen: boolean
  onClose: () => void
  onConfirmExport: (options: ExportModalOptions) => void
  projectTitle?: string
  settings: ProjectSettings
  videoCoverUrl?: string | null
  videoPosterUrl?: string | null
  durationSec?: number
}

function formatDurationText(sec: number): string {
  if (!sec || sec <= 0) return '0 giây'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  if (m > 0) {
    return s > 0 ? `${m} phút ${s} giây` : `${m} phút`
  }
  return `${s} giây`
}

function estimateFileSizeMB(
  durationSec: number,
  resolution: string,
  bitrate: string,
  exportVideo: boolean,
  exportAudio: boolean
): number {
  const dur = durationSec > 0 ? durationSec : 60
  if (!exportVideo && exportAudio) {
    return Math.max(1, Math.round((dur / 60) * 1.5))
  }
  let mbPerSec = 1.0 // 1080p default ~ 60MB/min
  if (resolution === '2160' || resolution === '4k') mbPerSec = 3.2
  else if (resolution === '1440' || resolution === '2k') mbPerSec = 2.0
  else if (resolution === '1080') mbPerSec = 1.0
  else if (resolution === '720') mbPerSec = 0.5
  else if (resolution === '480') mbPerSec = 0.3
  else if (resolution === '360' || resolution === '240' || resolution === '144') mbPerSec = 0.18
  else if (resolution === 'original') mbPerSec = 1.1

  let mult = 1.0
  if (bitrate === 'high') mult = 1.4
  else if (bitrate === 'low') mult = 0.65

  return Math.max(1, Math.round(dur * mbPerSec * mult))
}

export const ExportModal: React.FC<ExportModalProps> = ({
  isOpen,
  onClose,
  onConfirmExport,
  projectTitle = '',
  settings,
  videoCoverUrl,
  videoPosterUrl,
  durationSec = 0,
}) => {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const defaultName = (projectTitle || '0720').replace(/\.[^/.]+$/, '')
  const [renderName, setRenderName] = useState(defaultName)

  // Video Section
  const [exportVideo, setExportVideo] = useState(true)
  const [videoSectionOpen, setVideoSectionOpen] = useState(true)
  const [exportRes, setExportRes] = useState<ProjectSettings['exportResolution']>(
    settings.exportResolution || '1080'
  )
  const [bitrate, setBitrate] = useState<'recommended' | 'high' | 'low'>('recommended')
  const [codec, setCodec] = useState<'h264' | 'h265' | 'av1'>('h264')
  const [videoFormat, setVideoFormat] = useState<'mp4' | 'mov'>('mp4')
  const [fps, setFps] = useState<string>('30fps')

  // Audio Section
  const [exportAudio, setExportAudio] = useState(false)
  const [audioSectionOpen, setAudioSectionOpen] = useState(false)
  const [audioFormat, setAudioFormat] = useState<'mp3' | 'wav' | 'aac'>('mp3')

  // GIF Section
  const [exportGif, setExportGif] = useState(false)
  const [gifSectionOpen, setGifSectionOpen] = useState(false)
  const [gifRes, setGifRes] = useState<'240' | '360' | '480'>('240')

  // Subtitle / Captions Section
  const [exportSrt, setExportSrt] = useState(false)
  const [srtSectionOpen, setSrtSectionOpen] = useState(false)
  const [srtFormat, setSrtFormat] = useState<'srt' | 'txt' | 'vtt'>('srt')

  // Copyright check
  const [copyrightCheck, setCopyrightCheck] = useState(false)

  // Thư mục xuất — lưu localStorage để nhớ qua các lần mở modal
  const [exportDir, setExportDir] = useState(
    () => localStorage.getItem('exportOutputDir') || ''
  )
  const [isDesktopApp, setIsDesktopApp] = useState(false)

  useEffect(() => {
    void api.getConfig().then(config => setIsDesktopApp(Boolean(config.desktop))).catch(() => undefined)
  }, [])

  async function pickFolder() {
    try {
      const res = await fetch('/api/system/pick-folder', { method: 'POST' })
      const data = await res.json() as { ok: boolean; path: string }
      if (data.ok && data.path) {
        return data.path
      }
    } catch {
      // user cancel hoặc backend lỗi — ignore
    }
    return undefined
  }

  // ponytail: capture frame video qua canvas — <img> không render được video URL
  const [coverDataUrl, setCoverDataUrl] = useState<string | null>(null)
  const [isCoverPickerOpen, setIsCoverPickerOpen] = useState(false)
  // The cover workspace must follow the source frame, not a fixed portrait
  // card.  Start at 16:9 so it is also correct before metadata is available.
  const [coverAspect, setCoverAspect] = useState(16 / 9)

  useEffect(() => {
    if (isOpen) {
      setRenderName((projectTitle || '0720').replace(/\.[^/.]+$/, ''))
      setExportRes(settings.exportResolution || '1080')
      // mặc định: chỉ tích Video; reset mỗi lần mở modal
      setExportVideo(settings.exportVideo ?? true)
      setExportAudio(settings.exportAudio ?? false)
      setExportGif(settings.exportGif ?? false)
      setExportSrt(settings.exportSrt ?? false)
    }
  }, [isOpen, projectTitle, settings.exportResolution])

  useEffect(() => {
    if (!isOpen || !videoCoverUrl) {
      setCoverDataUrl(null)
      setCoverAspect(16 / 9)
      return
    }
    const vid = document.createElement('video')
    vid.crossOrigin = 'anonymous'
    vid.muted = true
    vid.playsInline = true
    // Electron/WebKit needs decoded data, not metadata, before canvas capture.
    vid.preload = 'auto'
    const capture = () => {
      try {
        const canvas = document.createElement('canvas')
        canvas.width = vid.videoWidth || 320
        canvas.height = vid.videoHeight || 180
        canvas.getContext('2d')?.drawImage(vid, 0, 0, canvas.width, canvas.height)
        setCoverDataUrl(canvas.toDataURL('image/jpeg', 0.85))
      } catch {
        setCoverDataUrl(null)
      }
    }
    vid.addEventListener('seeked', capture, { once: true })
    vid.addEventListener('loadedmetadata', () => {
      if (vid.videoWidth > 0 && vid.videoHeight > 0) {
        setCoverAspect(vid.videoWidth / vid.videoHeight)
      }
      vid.currentTime = Math.min(0.5, (vid.duration || 1) * 0.1)
    }, { once: true })
    vid.addEventListener('error', () => setCoverDataUrl(null), { once: true })
    vid.src = videoCoverUrl
    vid.load()
    return () => { vid.src = '' }
  }, [isOpen, videoCoverUrl])

  if (!isOpen) return null

  const estMb = estimateFileSizeMB(durationSec, exportRes, bitrate, exportVideo, exportAudio)
  // Keep horizontal video wide and let portrait video grow only up to the
  // previous 360px workspace height.
  const coverFrameWidth = Math.round(Math.min(240, Math.max(120, 360 * coverAspect)))

  const handleExport = () => {
    onConfirmExport({
      renderName: renderName.trim() || defaultName,
      exportResolution: exportRes,
      exportVideo,
      exportVideoFormat: videoFormat,
      exportAudio,
      exportAudioFormat: audioFormat,
      exportSrt,
      exportSrtFormat: srtFormat,
      exportGif,
      exportGifRes: gifRes,
      exportOutputDir: exportDir,
      coverDataUrl: coverDataUrl || undefined,
    })
    onClose()
  }

  const modalContent = (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/75 backdrop-blur-sm animate-fade-in p-4">
      <div
        className="relative flex flex-col w-[760px] max-w-[95vw] h-[610px] max-h-[94vh] rounded-xl border shadow-2xl overflow-hidden font-sans select-none"
        style={{
          backgroundColor: 'var(--card, #18191c)',
          color: 'var(--foreground, #e4e4e7)',
          borderColor: 'var(--border, #27272a)',
        }}
      >
        {/* ── Header Title Bar ── */}
        <div
          className="flex items-center justify-between px-5 py-3 border-b"
          style={{
            backgroundColor: 'var(--surface, #131417)',
            borderColor: 'var(--border, #27272a)',
          }}
        >
          <h2 className="text-sm font-semibold tracking-wide" style={{ color: 'var(--foreground, #ffffff)' }}>
            Xuất-{renderName || '0720'}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-md transition-colors opacity-70 hover:opacity-100"
            style={{ color: 'var(--muted-foreground, #a1a1aa)' }}
            title="Đóng"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* ── Body: Two Columns ── */}
        <div className="flex flex-1 min-h-0 p-5 gap-6 overflow-hidden">
          {/* Left Column: Video Cover Box */}
          <div className="flex flex-col items-center shrink-0 w-[240px]">
            <div
              className="relative rounded-lg overflow-hidden border shadow-inner flex items-center justify-center group"
              style={{
                width: `${coverFrameWidth}px`,
                aspectRatio: String(coverAspect),
                backgroundColor: 'var(--preview-workspace-bg, #000000)',
                borderColor: 'var(--border, #27272a)',
              }}
            >
              {coverDataUrl ? (
                <img src={coverDataUrl} alt="Video Cover" className="w-full h-full object-contain bg-black" />
              ) : videoCoverUrl ? (
                <video
                  src={videoCoverUrl}
                  poster={videoPosterUrl || undefined}
                  className="w-full h-full object-contain bg-black"
                  muted
                  playsInline
                  preload="auto"
                />
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 opacity-50">
                  <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <rect x="2" y="2" width="20" height="20" rx="2.5" />
                    <path d="M7 2v20M17 2v20M2 12h20M2 7h5M17 7h5M2 17h5M17 7h5" />
                  </svg>
                  <span className="text-xs">Khung hình xem trước</span>
                </div>
              )}

              {/* Top-Left Cover Badge */}
              <div className="absolute top-2.5 left-2.5 flex items-center gap-1.5">
                <button
                  type="button"
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium backdrop-blur-md border transition-all shadow cursor-pointer"
                  style={{
                    backgroundColor: 'rgba(0, 0, 0, 0.65)',
                    color: '#ffffff',
                    borderColor: 'rgba(255, 255, 255, 0.15)',
                  }}
                  onClick={() => setIsCoverPickerOpen(true)}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M12 20h9" />
                    <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
                  </svg>
                  {coverDataUrl ? 'Đổi ảnh bìa' : 'Sửa ảnh bìa'}
                </button>
                {coverDataUrl && (
                  <button
                    type="button"
                    title="Đặt lại về frame tự động"
                    className="flex items-center justify-center w-6 h-6 rounded-md backdrop-blur-md border transition-all shadow cursor-pointer"
                    style={{
                      backgroundColor: 'rgba(0, 0, 0, 0.65)',
                      color: '#a1a1aa',
                      borderColor: 'rgba(255, 255, 255, 0.15)',
                    }}
                    onClick={() => setCoverDataUrl(null)}
                  >
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                      <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                      <path d="M3 3v5h5" />
                    </svg>
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* Right Column: Scrollable Settings Form */}
          <div className="flex-1 flex flex-col min-h-0 overflow-y-auto pr-1 space-y-4 text-xs custom-scrollbar">
            {/* Xuất dòng thời gian */}
            <div className="flex items-center justify-between py-1">
              <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Xuất dòng thời gian</span>
              <span className="font-normal" style={{ color: 'var(--foreground, #ffffff)' }}>
                {durationSec > 0 ? `Dòng thời gian 01 (${formatDurationText(durationSec)})` : 'Dòng thời gian 01'}
              </span>
            </div>

            {/* Tên */}
            <div className="space-y-1">
              <label className="block font-medium" style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>
                Tên
              </label>
              <input
                type="text"
                value={renderName}
                onChange={(e) => setRenderName(e.target.value)}
                className="w-full rounded-md px-3 py-1.5 outline-none transition-colors"
                style={{
                  backgroundColor: 'var(--input, #24252a)',
                  color: 'var(--foreground, #ffffff)',
                  borderColor: 'var(--border, #383940)',
                  borderWidth: '1px',
                }}
                placeholder="Nhập tên video xuất…"
              />
            </div>

            <OutputFolderField isDesktopApp={isDesktopApp} value={exportDir} onChange={(value) => { setExportDir(value); localStorage.setItem('exportOutputDir', value) }} onChoose={isDesktopApp ? pickFolder : undefined} defaultPath={t('Ví dụ: video-da-xuat.mp4', 'Example: exported-video.mp4')} appFolder="export" label={t('Xuất sang', 'Export to')} />

            {/* ── 1. Group Video ── */}

            <div className="pt-3 border-t" style={{ borderColor: 'var(--border, #27272a)' }}>
              <div
                className="flex items-center justify-between py-1 cursor-pointer select-none font-medium hover:opacity-90 transition-colors"
                onClick={() => setVideoSectionOpen(!videoSectionOpen)}
              >
                <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={exportVideo}
                    onChange={(e) => setExportVideo(e.target.checked)}
                    className="accent-[#00c4cc] w-4 h-4 rounded cursor-pointer"
                  />
                  <span className="text-sm font-medium" style={{ color: 'var(--foreground, #ffffff)' }}>
                    Video
                  </span>
                </div>
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  className={`transition-transform duration-200 ${videoSectionOpen ? '' : '-rotate-90'}`}
                >
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </div>

              {videoSectionOpen && (
                <div className={`pl-6 pt-3 space-y-3 ${!exportVideo ? 'opacity-40 pointer-events-none' : ''}`}>

                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Độ phân giải</span>
                    <select
                      value={exportRes}
                      onChange={(e) => setExportRes(e.target.value as ProjectSettings['exportResolution'])}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="1080">1080P (Full HD)</option>
                      <option value="2160">2160P (4K)</option>
                      <option value="1440">1440P (2K)</option>
                      <option value="720">720P (HD)</option>
                      <option value="480">480P</option>
                      <option value="360">360P</option>
                      <option value="original">Gốc (Original)</option>
                    </select>
                  </div>

                  {/* Tốc độ bit */}
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Tốc độ bit</span>
                    <select
                      value={bitrate}
                      onChange={(e) => setBitrate(e.target.value as any)}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="recommended">Được đề xuất</option>
                      <option value="high">Cao</option>
                      <option value="low">Thấp</option>
                    </select>
                  </div>

                  {/* Bộ mã hóa và giải mã */}
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Bộ mã hóa và giải mã</span>
                    <select
                      value={codec}
                      onChange={(e) => setCodec(e.target.value as any)}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="h264">H.264</option>
                      <option value="h265">HEVC / H.265</option>
                      <option value="av1">AV1</option>
                    </select>
                  </div>

                  {/* Định dạng */}
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Định dạng</span>
                    <select
                      value={videoFormat}
                      onChange={(e) => setVideoFormat(e.target.value as any)}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="mp4">mp4</option>
                      <option value="mov">mov</option>
                    </select>
                  </div>

                  {/* Tỷ lệ khung hình */}
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Tỷ lệ khung hình</span>
                    <select
                      value={fps}
                      onChange={(e) => setFps(e.target.value)}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="30fps">30fps</option>
                      <option value="60fps">60fps</option>
                      <option value="24fps">24fps</option>
                      <option value="25fps">25fps</option>
                      <option value="50fps">50fps</option>
                    </select>
                  </div>

                  {/* Không gian màu */}
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Không gian màu</span>
                    <span className="w-48 px-2.5 py-1" style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>
                      Rec. 709 SDR
                    </span>
                  </div>
                </div>
              )}
            </div>

            {/* ── 2. Group Âm thanh (Audio) ── */}
            <div className="pt-3 border-t" style={{ borderColor: 'var(--border, #27272a)' }}>
              <div
                className="flex items-center justify-between py-1 cursor-pointer select-none font-medium hover:opacity-90 transition-colors"
                onClick={() => setAudioSectionOpen(!audioSectionOpen)}
              >
                <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={exportAudio}
                    onChange={(e) => {
                      setExportAudio(e.target.checked)
                      if (e.target.checked) setAudioSectionOpen(true)
                    }}
                    className="accent-[#00c4cc] w-4 h-4 rounded cursor-pointer"
                  />
                  <span className="text-sm font-medium" style={{ color: 'var(--foreground, #ffffff)' }}>
                    Âm thanh
                  </span>
                </div>
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  className={`transition-transform duration-200 ${audioSectionOpen ? '' : '-rotate-90'}`}
                >
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </div>

              {audioSectionOpen && (
                <div className={`pl-6 pt-3 space-y-3 ${!exportAudio ? 'opacity-40 pointer-events-none' : ''}`}>
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Định dạng</span>
                    <select
                      value={audioFormat}
                      onChange={(e) => setAudioFormat(e.target.value as any)}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="mp3">MP3</option>
                      <option value="wav">WAV</option>
                      <option value="aac">AAC</option>
                    </select>
                  </div>
                </div>
              )}
            </div>

            {/* ── 3. Group Xuất GIF ── */}
            <div className="pt-3 border-t" style={{ borderColor: 'var(--border, #27272a)' }}>
              <div
                className="flex items-center justify-between py-1 cursor-pointer select-none font-medium hover:opacity-90 transition-colors"
                onClick={() => setGifSectionOpen(!gifSectionOpen)}
              >
                <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={exportGif}
                    onChange={(e) => {
                      setExportGif(e.target.checked)
                      if (e.target.checked) setGifSectionOpen(true)
                    }}
                    className="accent-[#00c4cc] w-4 h-4 rounded cursor-pointer"
                  />
                  <span className="text-sm font-medium" style={{ color: 'var(--foreground, #ffffff)' }}>
                    Xuất GIF
                  </span>
                </div>
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  className={`transition-transform duration-200 ${gifSectionOpen ? '' : '-rotate-90'}`}
                >
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </div>

              {gifSectionOpen && (
                <div className={`pl-6 pt-3 space-y-3 ${!exportGif ? 'opacity-40 pointer-events-none' : ''}`}>
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Độ phân giải</span>
                    <select
                      value={gifRes}
                      onChange={(e) => setGifRes(e.target.value as any)}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="240">240P</option>
                      <option value="360">360P</option>
                      <option value="480">480P</option>
                    </select>
                  </div>
                </div>
              )}
            </div>

            {/* ── 4. Group Chú thích (Subtitles / SRT) ── */}
            <div className="pt-3 border-t" style={{ borderColor: 'var(--border, #27272a)' }}>
              <div
                className="flex items-center justify-between py-1 cursor-pointer select-none font-medium hover:opacity-90 transition-colors"
                onClick={() => setSrtSectionOpen(!srtSectionOpen)}
              >
                  <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={exportSrt}
                      onChange={(e) => {
                        setExportSrt(e.target.checked)
                        if (e.target.checked) setSrtSectionOpen(true)
                      }}
                      className="accent-[#00c4cc] w-4 h-4 rounded cursor-pointer"
                    />
                    <span className="text-sm font-medium" style={{ color: 'var(--foreground, #ffffff)' }}>
                      Chú thích
                    </span>
                  </div>
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  className={`transition-transform duration-200 ${srtSectionOpen ? '' : '-rotate-90'}`}
                >
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </div>

              {srtSectionOpen && (
                <div className={`pl-6 pt-3 space-y-3 ${!exportSrt ? 'opacity-40 pointer-events-none' : ''}`}>
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Định dạng</span>
                    <select
                      value={srtFormat}
                      onChange={(e) => setSrtFormat(e.target.value as any)}
                      className="w-48 rounded-md px-2.5 py-1 text-xs outline-none"
                      style={{
                        backgroundColor: 'var(--input, #24252a)',
                        color: 'var(--foreground, #ffffff)',
                        borderColor: 'var(--border, #383940)',
                        borderWidth: '1px',
                      }}
                    >
                      <option value="srt">SRT</option>
                      <option value="txt">TXT</option>
                      <option value="vtt">VTT</option>
                    </select>
                  </div>

                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>Mã hóa ký tự</span>
                    <span className="w-48 px-2.5 py-1">Unicode / UTF-8</span>
                  </div>
                </div>
              )}
            </div>

            {/* ── 5. Kiểm tra bản quyền? ── */}
            <div
              className="pt-3 border-t flex items-center justify-between py-1"
              style={{ borderColor: 'var(--border, #27272a)' }}
            >
              <div className="flex items-center gap-1.5">
                <span>Kiểm tra bản quyền?</span>
                <span className="opacity-50 cursor-pointer" title="Tự động kiểm tra bản quyền âm thanh">
                  ❓
                </span>
              </div>
              <button
                type="button"
                onClick={() => setCopyrightCheck(!copyrightCheck)}
                className={`w-9 h-5 rounded-full transition-colors relative flex items-center px-0.5 ${
                  copyrightCheck ? 'bg-[#00c4cc]' : 'bg-[#383940]'
                }`}
              >
                <div
                  className={`w-4 h-4 rounded-full bg-white transition-transform ${
                    copyrightCheck ? 'translate-x-4' : 'translate-x-0'
                  }`}
                />
              </button>
            </div>
          </div>
        </div>

        {/* ── Footer ── */}
        <div
          className="flex items-center justify-between px-5 py-3 border-t"
          style={{
            backgroundColor: 'var(--surface, #131417)',
            borderColor: 'var(--border, #27272a)',
          }}
        >
          {/* Left Meta Info */}
          <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--muted-foreground, #a1a1aa)' }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="2" y="2" width="20" height="20" rx="2.5" />
              <path d="M7 2v20M17 2v20M2 12h20M2 7h5M17 7h5M2 17h5M17 17h5" />
            </svg>
            <span>
              Khoảng thời gian:{' '}
              <strong className="font-normal" style={{ color: 'var(--foreground, #ffffff)' }}>
                {formatDurationText(durationSec)}
              </strong>
            </span>
            <span className="opacity-40">|</span>
            <span>
              Kích thước:{' '}
              <strong className="font-normal" style={{ color: 'var(--foreground, #ffffff)' }}>
                khoảng ~{estMb} MB
              </strong>
            </span>
          </div>

          {/* Right Action Buttons */}
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleExport}
              className="px-6 py-1.5 rounded-md bg-[#00c4cc] hover:bg-[#00b2b9] text-black font-semibold text-xs shadow-md transition-colors cursor-pointer"
            >
              Xuất
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-6 py-1.5 rounded-md transition-colors cursor-pointer font-medium text-xs"
              style={{
                backgroundColor: 'var(--muted, #2d2e34)',
                color: 'var(--foreground, #e4e4e7)',
              }}
            >
              Hủy
            </button>
          </div>
        </div>
      </div>
    </div>
  )

  return (
    <>
      {createPortal(modalContent, document.body)}
      <CoverPickerModal
        isOpen={isCoverPickerOpen}
        onClose={() => setIsCoverPickerOpen(false)}
        onConfirm={(dataUrl) => setCoverDataUrl(dataUrl)}
        videoUrl={videoCoverUrl}
      />
    </>
  )
}
