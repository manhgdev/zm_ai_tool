import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import './SrtImagePage.css'
import { localize, useLocale } from '../app/i18n'
import { BackTitle } from '../shared/components/BackTitle'
import { copyText } from '../shared/lib/clipboard'
import { CAPTION_FONT_PRESETS, captionChromeStyle, captionFontCss } from '../features/editor/lib/previewStyles'
import {
  type Job, type MissingMediaInfo, type HelpKey,
  SETTINGS_KEY, JOB_KEY, HELP, cachedSettings,
} from './srtImage.types'



export default function SrtImagePage({ onBack, initialMediaFolder = '' }: { onBack: () => void; initialMediaFolder?: string }) {
  const { locale } = useLocale()
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  function helpText(key: HelpKey) {
    if (key === 'resolution') return [
      t('Chất lượng xuất', 'Output quality'),
      t('Chọn kích thước khung hình video xuất. Auto mặc định giữ tỷ lệ ảnh và xuất với cạnh ngắn 1080px.', 'Choose the output video frame size. Auto is the default: it keeps the media aspect ratio and exports with a 1080px short edge.'),
      t('Auto cho ảnh ngang là 1920×1080, ảnh dọc là 1080×1920; chọn preset khi cần khung hình cố định.', 'Auto creates 1920×1080 for landscape and 1080×1920 for portrait media; choose a preset when you need a fixed frame.'),
    ]
    if (key === 'quality') return [
      t('Chất lượng nén', 'Compression quality'),
      t('Điều khiển mức nén video. Chất lượng cao cho hình đẹp hơn nhưng render lâu và file lớn.', 'Controls video compression. High quality looks better but takes longer and creates larger files.'),
      t('Cân bằng phù hợp mặc định; chọn Nhanh khi cần thử hoặc xem trước.', 'Balanced is the default choice; use Fast for a test or preview.'),
    ]
    return HELP[key]
  }
  const cached = useRef(cachedSettings()).current
  const [tab, setTab] = useState<'project' | 'settings'>('project')
  const [helpKey, setHelpKey] = useState<HelpKey | null>(null)
  const [mediaFolder, setMediaFolder] = useState(String(cached.mediaFolder ?? ''))
  const [audioPath, setAudioPath] = useState(String(cached.audioPath ?? ''))
  const [timelinePath, setTimelinePath] = useState(String(cached.timelinePath ?? ''))
  const [timelineText, setTimelineText] = useState(String(cached.timelineText ?? ''))
  const [timelineMode, setTimelineMode] = useState<'file' | 'paste'>('file')
  const [srtPath, setSrtPath] = useState(String(cached.srtPath ?? ''))
  const [subtitleSize, setSubtitleSize] = useState(Number(cached.subtitleSize ?? 8))
  const [subtitleOffset, setSubtitleOffset] = useState(Number(cached.subtitleOffset ?? 0))
  const [subtitleFontFamily, setSubtitleFontFamily] = useState(String(cached.subtitleFontFamily ?? 'system'))
  // Migrate former defaults so existing projects also keep captions
  // clear of the bottom edge on both portrait and landscape output.
  const [subtitleMargin, setSubtitleMargin] = useState(() => {
    const saved = Number(cached.subtitleMargin ?? 34)
    return saved === 18 || saved === 28 ? 34 : saved
  })
  const [subtitleBackground, setSubtitleBackground] = useState(String(cached.subtitleBackground ?? 'solid'))
  const [subtitleColor, setSubtitleColor] = useState(String(cached.subtitleColor ?? '#ffffff'))
  const [subtitleBgColor, setSubtitleBgColor] = useState(String(cached.subtitleBgColor ?? '#000000'))
  const [subtitleOpacity, setSubtitleOpacity] = useState(Number(cached.subtitleOpacity ?? 55))
  const [resolution, setResolution] = useState(String(cached.resolution ?? 'auto'))
  const [targetPlatform, setTargetPlatform] = useState(String(cached.targetPlatform ?? 'auto'))
  const [fps, setFps] = useState(Number(cached.fps ?? 30))
  const [crf, setCrf] = useState(Number(cached.crf ?? 20))
  const [effect, setEffect] = useState(String(cached.effect ?? 'none'))
  const [transitionDuration, setTransitionDuration] = useState(Number(cached.transitionDuration ?? 0.28))
  const [zoom, setZoom] = useState(String(cached.zoom ?? 'off'))
  const [speed, setSpeed] = useState(Number(cached.speed ?? 100))
  const [volume, setVolume] = useState(Number(cached.volume ?? 100))
  const [previewSeconds, setPreviewSeconds] = useState(Number(cached.previewSeconds ?? 15))
  const [encoder, setEncoder] = useState(String(cached.encoder ?? 'auto'))
  const [removeMetadata, setRemoveMetadata] = useState(Boolean(cached.removeMetadata ?? false))
  const [drawingEnabled, setDrawingEnabled] = useState(Boolean(cached.drawingEnabled ?? false))
  const [drawingMode, setDrawingMode] = useState(String(cached.drawingMode ?? 'hand'))
  const [drawingTool, setDrawingTool] = useState(String(cached.drawingTool ?? 'pencil'))
  const [drawingDetail, setDrawingDetail] = useState(Number(cached.drawingDetail ?? 72))
  const [drawingThickness, setDrawingThickness] = useState(Number(cached.drawingThickness ?? 2))
  const [drawingStrokeOrder, setDrawingStrokeOrder] = useState(String(cached.drawingStrokeOrder ?? 'natural'))
  const [delogoEnabled, setDelogoEnabled] = useState(Boolean(cached.delogoEnabled ?? false))
  const [delogoAuto, setDelogoAuto] = useState(Boolean(cached.delogoAuto ?? true))
  const [delogoRect, setDelogoRect] = useState(
    (cached.delogoRect as { x: number; y: number; w: number; h: number } | undefined) ?? { x: 82, y: 94, w: 16, h: 4 }
  )
  const [watermarkPath, setWatermarkPath] = useState(String(cached.watermarkPath ?? ''))
  const [logoEnabled, setLogoEnabled] = useState(Boolean(cached.logoEnabled ?? false))
  const [logoSource, setLogoSource] = useState<'text' | 'image' | 'icon'>(
    cached.logoSource === 'image' || cached.logoSource === 'icon' ? cached.logoSource : 'text',
  )
  const [logoText, setLogoText] = useState(String(
    !cached.logoText || cached.logoText === 'ZM AI TOOL' ? 'ZMTOOL' : cached.logoText,
  ))
  const [logoFontSize, setLogoFontSize] = useState(Number(
    cached.logoFontSize == null || cached.logoFontSize === 42 ? 10 : cached.logoFontSize,
  ))
  const [logoColor, setLogoColor] = useState(String(cached.logoColor ?? '#ffffff'))
  const [logoIcon, setLogoIcon] = useState(String(cached.logoIcon ?? '★'))
  const [logoSize, setLogoSize] = useState(Number(cached.logoSize ?? 8))
  const [logoOpacity, setLogoOpacity] = useState(Number(cached.logoOpacity ?? 85))
  const [logoX, setLogoX] = useState(Number(cached.logoX ?? 88))
  const [logoY, setLogoY] = useState(Number(cached.logoY ?? 88))
  const [logoMotion, setLogoMotion] = useState(String(cached.logoMotion ?? 'fixed'))
  const [logoScope, setLogoScope] = useState(String(cached.logoScope ?? 'full'))
  const [logoStart, setLogoStart] = useState(Number(cached.logoStart ?? 0))
  const [logoEnd, setLogoEnd] = useState(Number(cached.logoEnd ?? 10))
  const [logoVisibleSec, setLogoVisibleSec] = useState(Number(cached.logoVisibleSec ?? 4))
  const [logoHiddenSec, setLogoHiddenSec] = useState(Number(cached.logoHiddenSec ?? 2))
  const [logoFadeSec, setLogoFadeSec] = useState(Number(cached.logoFadeSec ?? 0.5))
  const [logoSafeMargin, setLogoSafeMargin] = useState(Number(cached.logoSafeMargin ?? 4))
  const [outputName, setOutputName] = useState(String(cached.outputName ?? 'output.mp4'))
  const [outputPath, setOutputPath] = useState(String(cached.outputPath ?? ''))
  const [defaultOutputDirectory, setDefaultOutputDirectory] = useState('ZM_AI_TOOL/subtitles/image-video/')
  const [job, setJob] = useState<Job | null>(null)
  const [sending, setSending] = useState(false)
  const [missingMedia, setMissingMedia] = useState<MissingMediaInfo | null>(null)
  const [logStart, setLogStart] = useState(0)
  const settingsSnapshot = useRef('')

  useEffect(() => {
    if (!initialMediaFolder) return
    setMediaFolder(initialMediaFolder)
    setTab('project')
  }, [initialMediaFolder])

  useEffect(() => {
    void fetch('/api/system/resolve-output-folder?tab=subtitle-image', { method: 'POST' })
      .then(async (response) => response.ok ? response.json() as Promise<{ path?: string }> : null)
      .then((result) => {
        const path = String(result?.path || '').replace(/[\\/]+$/, '')
        if (path) setDefaultOutputDirectory(`${path}/`)
      })
      .catch(() => undefined)
  }, [])

  // A render owns a server-side workspace.  Restore that workspace after an
  // F5 instead of leaving the user with an empty page while FFmpeg continues.
  useEffect(() => {
    let cancelled = false
    void fetch('/api/srt-image/jobs').then(async (response) => {
      if (!response.ok) return [] as Job[]
      return await response.json() as Job[]
    }).then((jobs) => {
      if (cancelled || !jobs.length) return
      let cachedId = ''
      try { cachedId = localStorage.getItem(JOB_KEY) || '' } catch { /* unavailable storage */ }
      setJob(jobs.find((item) => item.id === cachedId) ?? jobs[0])
    }).catch(() => undefined)
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    try {
      if (job?.id) localStorage.setItem(JOB_KEY, job.id)
      else localStorage.removeItem(JOB_KEY)
    } catch { /* unavailable storage */ }
  }, [job?.id])

  useEffect(() => {
    if (!job || !['queued', 'processing', 'paused'].includes(job.status)) return
    const timer = window.setInterval(async () => {
      const response = await fetch(`/api/srt-image/jobs/${job.id}`)
      if (response.ok) setJob(await response.json())
    }, 1000)
    return () => window.clearInterval(timer)
  }, [job?.id, job?.status])

  useEffect(() => {
    if (!helpKey) return
    const close = (event: KeyboardEvent) => event.key === 'Escape' && setHelpKey(null)
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [helpKey])

  // ponytail: debounce localStorage writes — the old effect fired synchronously
  // on every keystroke/slider drag (65+ deps), blocking the main thread with
  // JSON.stringify + setItem.  500 ms covers rapid drags without losing data.
  const saveTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => {
    clearTimeout(saveTimerRef.current)
    saveTimerRef.current = setTimeout(() => {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({
        mediaFolder, audioPath, timelinePath, timelineText, srtPath, watermarkPath, outputName, outputPath,
        resolution, targetPlatform, fps, crf, effect, transitionDuration, zoom, speed, volume,
        previewSeconds, encoder, removeMetadata, delogoEnabled, delogoAuto, delogoRect,
        drawingEnabled, drawingMode, drawingTool, drawingDetail, drawingThickness,
        drawingStrokeOrder,
        subtitleSize, subtitleOffset, subtitleFontFamily,
        subtitleMargin, subtitleBackground, subtitleColor, subtitleBgColor, subtitleOpacity, logoEnabled, logoSource, logoText,
        logoIcon, logoSize, logoFontSize, logoColor, logoOpacity, logoX, logoY,
        logoMotion, logoScope, logoStart, logoEnd, logoVisibleSec, logoHiddenSec,
        logoFadeSec, logoSafeMargin,
      }))
    }, 500)
    return () => clearTimeout(saveTimerRef.current)
  }, [
    mediaFolder, audioPath, timelinePath, timelineText, srtPath, watermarkPath, outputName, outputPath,
    resolution, targetPlatform, fps, crf, effect, transitionDuration, zoom, speed, volume,
    previewSeconds, encoder, removeMetadata, delogoEnabled, delogoAuto, delogoRect,
    drawingEnabled, drawingMode, drawingTool, drawingDetail, drawingThickness,
    drawingStrokeOrder,
    subtitleSize, subtitleOffset, subtitleFontFamily,
    subtitleMargin, subtitleBackground, subtitleColor, subtitleBgColor, subtitleOpacity, logoEnabled, logoSource, logoText,
    logoIcon, logoSize, logoFontSize, logoColor, logoOpacity, logoX, logoY,
    logoMotion, logoScope, logoStart, logoEnd, logoVisibleSec, logoHiddenSec,
    logoFadeSec, logoSafeMargin,
  ])

  async function start(preview = false, allowMissingMedia = false) {
    if (!mediaFolder) return
    setSending(true)
    try {
      const form = new FormData()
      form.append('media_folder', mediaFolder)
      if (timelineMode === 'paste' && timelineText.trim()) {
        form.append('timeline', new Blob([timelineText], { type: 'text/plain;charset=utf-8' }), 'timeline.txt')
      } else if (timelinePath) {
        form.append('timeline_path', timelinePath)
      }
      if (srtPath) form.append('srt_path', srtPath)
      if (audioPath) form.append('audio_path', audioPath)
      if (watermarkPath) form.append('watermark_path', watermarkPath)
      form.append('output_name', preview ? `${outputName.replace(/\.mp4$/i, '')}-preview.mp4` : outputName)
      if (outputPath && !preview) form.append('output_path', outputPath)
      form.append('options', JSON.stringify({
        resolution, targetPlatform, fps, crf, effect, transitionDuration, zoom, speed, volume,
        allowMissingMedia,
        encoder, removeMetadata, subtitleSize, subtitleOffset, subtitleFontFamily, subtitleMargin,
        subtitleBackground, subtitleColor, subtitleBgColor, subtitleOpacity, previewSeconds: preview ? previewSeconds : 0,
        delogo: { enabled: delogoEnabled, ...delogoRect },
        drawing: { enabled: drawingEnabled, mode: drawingMode, tool: drawingTool, detail: drawingDetail, thickness: drawingThickness, strokeOrder: drawingStrokeOrder, resolution: '1080p' },
        logo: {
          enabled: logoEnabled, source: logoSource, text: logoText, icon: logoIcon,
          size: logoSize, fontSize: logoFontSize, color: logoColor, opacity: logoOpacity,
          x: logoX, y: logoY, motion: logoMotion, scope: logoScope,
          start: logoStart, end: logoEnd, visibleSec: logoVisibleSec,
          hiddenSec: logoHiddenSec, fadeSec: logoFadeSec, safeMargin: logoSafeMargin,
        },
      }))
      const response = await fetch('/api/srt-image/jobs', { method: 'POST', body: form })
      if (response.status === 409) {
        const detail = (await response.clone().json().catch(() => null))?.detail
        if (detail?.code === 'missing_media') {
          setMissingMedia({
            required: Number(detail.required || 0),
            available: Number(detail.available || 0),
            missing_count: Number(detail.missing_count || (Number(detail.required || 0) - Number(detail.available || 0))),
            available_files: Array.isArray(detail.available_files) ? detail.available_files : [],
            missing_cues: Array.isArray(detail.missing_cues) ? detail.missing_cues : [],
            preview,
          })
          return
        }
      }
      if (!response.ok) throw new Error(await response.text())
      setLogStart(0)
      setJob(await response.json())
      toast.success(preview ? t('Đang bắt đầu tạo Preview…', 'Starting preview…') : t('Đang bắt đầu render…', 'Starting render…'))
    } catch (error) {
      const msg = String(error)
      setJob({ id: '', name: outputName, status: 'error', progress: 0, error: msg })
      toast.error(msg)
    } finally {
      setSending(false)
    }
  }

  async function cancel() {
    if (!job?.id) return
    await fetch(`/api/srt-image/jobs/${job.id}/cancel`, { method: 'POST' })
    setJob({ ...job, status: 'cancelled' })
    toast.success(t('Đã gửi yêu cầu hủy render.', 'Render cancellation requested.'))
  }

  async function togglePause() {
    if (!job?.id) return
    const paused = job.status !== 'paused'
    const response = await fetch(`/api/srt-image/jobs/${job.id}/pause?paused=${paused}`, { method: 'POST' })
    if (response.ok) {
      setJob({ ...job, status: paused ? 'paused' : 'processing' })
      toast.info(paused ? t('Đã tạm dừng render.', 'Render paused.') : t('Đang tiếp tục render.', 'Render resumed.'))
    }
  }

  async function openFolder() {
    const params = new URLSearchParams()
    if (outputPath) params.set('selected_output', outputPath)
    else if (job?.id) params.set('job_id', job.id)
    const query = params.size ? `?${params}` : ''
    const response = await fetch(`/api/srt-image/open-folder${query}`, { method: 'POST' })
    if (!response.ok) {
      const err = await response.text()
      setJob(job ? { ...job, error: err } : job)
      toast.error(err)
    }
  }

  async function chooseMediaFolder() {
    try {
      const response = await fetch('/api/system/pick-media-folder', { method: 'POST' })
      if (!response.ok) throw new Error(await response.text())
      const result = await response.json()
      if (result.ok && result.path) {
        setMediaFolder(String(result.path))
        toast.success(t('Đã chọn thư mục media.', 'Media folder selected.'))
      }
    } catch (error) {
      const msg = `Không chọn được thư mục: ${String(error)}`
      setJob({ id: '', name: outputName, status: 'error', progress: 0, error: msg })
      toast.error(msg)
    }
  }

  async function chooseInputFile(kind: 'audio' | 'timeline' | 'srt' | 'watermark') {
    try {
      const response = await fetch(`/api/system/pick-srt-image-file?kind=${kind}`, { method: 'POST' })
      if (!response.ok) throw new Error(await response.text())
      const result = await response.json()
      if (!result.ok || !result.path) return
      const path = String(result.path)
      if (kind === 'audio') {
        setAudioPath(path)
        toast.success(t('Đã chọn file audio.', 'Audio file selected.'))
      }
      else if (kind === 'timeline') {
        setTimelinePath(path)
        setTimelineText('')
        setTimelineMode('file')
        toast.success(t('Đã chọn file timeline.', 'Timeline file selected.'))
      }
      else if (kind === 'srt') {
        setSrtPath(path)
        toast.success(t('Đã chọn file phụ đề SRT.', 'SRT subtitle file selected.'))
      }
      else {
        setWatermarkPath(path)
        toast.success(t('Đã chọn ảnh logo.', 'Logo image selected.'))
      }
    } catch (error) {
      const msg = `Không chọn được file: ${String(error)}`
      setJob({ id: '', name: outputName, status: 'error', progress: 0, error: msg })
      toast.error(msg)
    }
  }

  async function chooseOutput() {
    try {
      const response = await fetch(`/api/system/pick-save-video?filename=${encodeURIComponent(outputName)}`, { method: 'POST' })
      if (!response.ok) throw new Error(await response.text())
      const result = await response.json()
      if (!result.ok || !result.path) return
      const path = String(result.path)
      setOutputPath(path)
      setOutputName(path.split(/[\\/]/).pop() || 'output.mp4')
      toast.success(t('Đã chọn file xuất.', 'Output file selected.'))
    } catch (error) {
      const msg = `Không chọn được file xuất: ${String(error)}`
      setJob({ id: '', name: outputName, status: 'error', progress: 0, error: msg })
      toast.error(msg)
    }
  }

  function renameOutput(value: string) {
    const name = `${value.replace(/\.mp4$/i, '')}.mp4`
    setOutputName(name)
    if (outputPath) {
      const slash = Math.max(outputPath.lastIndexOf('\\'), outputPath.lastIndexOf('/'))
      setOutputPath(`${outputPath.slice(0, slash + 1)}${name}`)
    }
  }

  // ponytail: memoize logo prop — recreating this object literal on every render
  // defeated React.memo on SubtitleLivePreview, forcing expensive re-renders
  // even when unrelated state (outputName, audioPath, …) changed.
  const logoProp = useMemo(() => ({
    enabled: logoEnabled, source: logoSource, text: logoText, icon: logoIcon,
    imagePath: watermarkPath, fontSize: logoFontSize, size: logoSize,
    color: logoColor, opacity: logoOpacity, x: logoX, y: logoY,
    motion: logoMotion, scope: logoScope, start: logoStart, end: logoEnd,
    visibleSec: logoVisibleSec, hiddenSec: logoHiddenSec,
    fadeSec: logoFadeSec, safeMargin: logoSafeMargin,
  }), [
    logoEnabled, logoSource, logoText, logoIcon, watermarkPath, logoFontSize,
    logoSize, logoColor, logoOpacity, logoX, logoY, logoMotion, logoScope,
    logoStart, logoEnd, logoVisibleSec, logoHiddenSec, logoFadeSec, logoSafeMargin,
  ])

  const busy = sending || job?.status === 'queued' || job?.status === 'processing' || job?.status === 'paused'
  const statusText = job?.status === 'done'
    ? localize(locale, 'Hoàn thành', 'Complete')
    : job?.status === 'error'
      ? localize(locale, 'Render thất bại', 'Render failed')
      : job?.status === 'paused'
        ? localize(locale, 'Đang tạm dừng', 'Paused')
      : job?.status === 'cancelled'
        ? localize(locale, 'Đã hủy', 'Cancelled')
        : busy
          ? localize(locale, 'Đang render…', 'Rendering…')
          : localize(locale, 'Sẵn sàng render', 'Ready to render')
  const outputSlash = Math.max(outputPath.lastIndexOf('\\'), outputPath.lastIndexOf('/'))
  const outputDirectory = outputPath
    ? outputPath.slice(0, outputSlash + 1)
    : defaultOutputDirectory
  const visibleLogs = (job?.logs || []).slice(logStart)
  const logText = visibleLogs.length
    ? visibleLogs.join('\n')
    : `[${new Date().toLocaleTimeString(locale === 'en' ? 'en-US' : 'vi-VN')}] ${job?.error || statusText}`
  const logRef = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const node = logRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [logText])

  function openSettings() {
    if (tab === 'settings') return
    settingsSnapshot.current = JSON.stringify({
      resolution, fps, crf, effect, transitionDuration, zoom, speed, volume,
      previewSeconds, encoder, removeMetadata, watermarkPath, logoEnabled,
      logoSource, logoText, logoIcon, logoSize, logoFontSize, logoColor,
      logoOpacity, logoX, logoY, logoMotion, logoScope, logoStart, logoEnd,
      logoVisibleSec, logoHiddenSec, logoFadeSec, logoSafeMargin,
    })
    setTab('settings')
  }

  function cancelSettings() {
    const value = JSON.parse(settingsSnapshot.current || '{}')
    setResolution(value.resolution ?? resolution)
    setFps(value.fps ?? fps)
    setCrf(value.crf ?? crf)
    setEffect(value.effect ?? effect)
    setTransitionDuration(value.transitionDuration ?? transitionDuration)
    setZoom(value.zoom ?? zoom)
    setSpeed(value.speed ?? speed)
    setVolume(value.volume ?? volume)
    setPreviewSeconds(value.previewSeconds ?? previewSeconds)
    setEncoder(value.encoder ?? encoder)
    setRemoveMetadata(value.removeMetadata ?? removeMetadata)
    setWatermarkPath(value.watermarkPath ?? watermarkPath)
    setLogoEnabled(value.logoEnabled ?? logoEnabled)
    setLogoSource(value.logoSource ?? logoSource)
    setLogoText(value.logoText ?? logoText)
    setLogoIcon(value.logoIcon ?? logoIcon)
    setLogoSize(value.logoSize ?? logoSize)
    setLogoFontSize(value.logoFontSize ?? logoFontSize)
    setLogoColor(value.logoColor ?? logoColor)
    setLogoOpacity(value.logoOpacity ?? logoOpacity)
    setLogoX(value.logoX ?? logoX)
    setLogoY(value.logoY ?? logoY)
    setLogoMotion(value.logoMotion ?? logoMotion)
    setLogoScope(value.logoScope ?? logoScope)
    setLogoStart(value.logoStart ?? logoStart)
    setLogoEnd(value.logoEnd ?? logoEnd)
    setLogoVisibleSec(value.logoVisibleSec ?? logoVisibleSec)
    setLogoHiddenSec(value.logoHiddenSec ?? logoHiddenSec)
    setLogoFadeSec(value.logoFadeSec ?? logoFadeSec)
    setLogoSafeMargin(value.logoSafeMargin ?? logoSafeMargin)
    setTab('project')
  }

  return (
    <main className="siv-page">
      <header>
        <div>
          <BackTitle onBack={onBack}>Ghép ảnh/video SRT</BackTitle>
          <p>{t('Ghép tuần tự ảnh hoặc clip; timeline, audio và phụ đề là tùy chọn.', 'Merge images or clips sequentially; timeline, audio, and subtitles are optional.')}</p>
        </div>
      </header>

      <div className="siv-layout">
        <div className="siv-main-pane">
          <section className="siv-workspace">
        <nav className="siv-tabs" aria-label="Thiết lập Ghép ảnh SRT">
          <button className={tab === 'project' ? 'active' : ''} onClick={() => setTab('project')}>Dự án</button>
          <button className={tab === 'settings' ? 'active' : ''} onClick={openSettings}>Cài đặt</button>
        </nav>

        <div className="siv-panel">
          {tab === 'project' ? (
            <div className="siv-form">
              <div className="siv-row">
                <label>Thư mục media <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('media') }}>i</span></label>
                <div className="siv-input"><input type="text" value={mediaFolder} onChange={(e) => setMediaFolder(e.target.value)} placeholder={t('Dán hoặc nhập đường dẫn thư mục...', 'Paste or type folder path...')} spellCheck={false} /></div>
                <button onClick={chooseMediaFolder}>Chọn</button>
                <button onClick={() => { setMediaFolder(''); toast.success(t('Đã xóa thư mục media.', 'Media folder cleared.')) }} disabled={!mediaFolder}>Xóa</button>
              </div>
              <div className="siv-row">
                <label>File audio <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('audio') }}>i</span></label>
                <div className="siv-input"><input type="text" value={audioPath} onChange={(e) => setAudioPath(e.target.value)} placeholder={t('Dán hoặc nhập đường dẫn file audio...', 'Paste or type audio file path...')} spellCheck={false} /></div>
                <button onClick={() => chooseInputFile('audio')}>Chọn</button>
                <button onClick={() => { setAudioPath(''); toast.success(t('Đã xóa file audio.', 'Audio file cleared.')) }} disabled={!audioPath}>Xóa</button>
              </div>
              <div className="siv-row siv-row--timeline">
                <label>{t('File timeline', 'Timeline file')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('timeline') }}>i</span></label>
                {timelineMode === 'paste'
                  ? <textarea
                      className="siv-timeline-input"
                      aria-label={t('Dán nội dung timeline', 'Paste timeline content')}
                      value={timelineText}
                      placeholder={t('Dán nội dung timeline (ví dụ: 001_[00:00:00.00-00:00:08.50] hoặc [00:00 - 00:05] hoặc nội dung file SRT, VTT, CSV, JSON…)', 'Paste timeline content (e.g. 001_[00:00:00.00-00:00:08.50] or [00:00 - 00:05] or SRT, VTT, CSV, JSON text…)')}
                      onChange={(event) => setTimelineText(event.target.value)}
                    />
                  : <div className="siv-input"><input type="text" value={timelinePath} onChange={(e) => setTimelinePath(e.target.value)} placeholder={t('Dán hoặc nhập đường dẫn timeline...', 'Paste or type timeline file path...')} spellCheck={false} /></div>}
                <div className="siv-row-actions" role="group" aria-label={t('Nguồn timeline', 'Timeline source')}>
                  <div className="siv-source-switch">
                    <button
                      className={timelineMode === 'paste' ? 'active' : ''}
                      aria-pressed={timelineMode === 'paste'}
                      aria-label={timelineMode === 'paste' ? t('Đổi sang chọn file', 'Switch to file selection') : t('Đổi sang dán nội dung', 'Switch to pasted content')}
                      onClick={() => {
                        if (timelineMode === 'paste') setTimelineMode('file')
                        else { setTimelinePath(''); setTimelineMode('paste') }
                      }}
                    >{t('Đổi', 'Switch')}</button>
                  </div>
                  <button onClick={() => { setTimelineMode('file'); void chooseInputFile('timeline') }}>{t('Chọn', 'Choose')}</button>
                  <button onClick={() => { setTimelinePath(''); setTimelineText(''); setTimelineMode('file'); toast.success(t('Đã xóa timeline.', 'Timeline cleared.')) }} disabled={!timelinePath && !timelineText}>{t('Xóa', 'Clear')}</button>
                </div>
              </div>
              <div className="siv-row">
                <label>File xuất <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('output') }}>i</span></label>
                <div className="siv-input siv-output-path"><span title={outputDirectory}>{outputDirectory}</span><input value={outputName.replace(/\.mp4$/i, '')} onChange={(e) => renameOutput(e.target.value)} /><b>.mp4</b></div>
                <button onClick={chooseOutput}>Chọn</button>
                <button onClick={() => { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...cachedSettings(), outputName, outputPath })); toast.success(t('Đã lưu cài đặt xuất video.', 'Output settings saved.')) }}>{localize(locale, 'Lưu', 'Save')}</button>
              </div>
              <div className="siv-row">
                <label>File phụ đề <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('subtitles') }}>i</span></label>
                <div className="siv-input"><input type="text" value={srtPath} onChange={(e) => setSrtPath(e.target.value)} placeholder={t('Dán hoặc nhập đường dẫn file SRT...', 'Paste or type SRT file path...')} spellCheck={false} /></div>
                <button onClick={() => chooseInputFile('srt')}>Chọn</button>
                <button onClick={() => { setSrtPath(''); toast.success(t('Đã xóa file phụ đề SRT.', 'SRT subtitle file cleared.')) }} disabled={!srtPath}>Xóa</button>
              </div>
              {srtPath && (
                <div className="siv-subtitle-options">
                  <label>
                    <span className="siv-subtitle-title">Phông chữ <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('subtitleFontFamily') }}>i</span></span>
                    <select value={subtitleFontFamily} onChange={(e) => setSubtitleFontFamily(e.target.value)}>
                      {CAPTION_FONT_PRESETS.map((font) => <option key={font.id} value={font.id} style={{ fontFamily: font.css }}>{font.label}</option>)}
                    </select>
                  </label>
                  <label>
                    <span className="siv-subtitle-title">Màu chữ <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('subtitleColor') }}>i</span></span>
                    <input type="color" className="siv-text-color" value={subtitleColor} title="Màu chữ" onChange={e => setSubtitleColor(e.target.value)} />
                  </label>
                  <label>
                    <span className="siv-subtitle-title">Cỡ chữ <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('subtitleSize') }}>i</span></span>
                    <input type="number" min="6" max="120" value={subtitleSize} onChange={(e) => setSubtitleSize(Number(e.target.value))} />
                  </label>
                  <label>
                    <span className="siv-subtitle-title">Lệch (s) <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('subtitleOffset') }}>i</span></span>
                    <input type="number" min="-3600" max="3600" step=".1" value={subtitleOffset} onChange={(e) => setSubtitleOffset(Number(e.target.value))} />
                  </label>
                  <label>
                    <span className="siv-subtitle-title">Lề dưới <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('subtitleMargin') }}>i</span></span>
                    <input type="number" min="0" max="1000" value={subtitleMargin} onChange={(e) => setSubtitleMargin(Number(e.target.value))} />
                  </label>
                  <div className="siv-bg-label">
                    <span className="siv-subtitle-title">Nền chữ <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('subtitleBackground') }}>i</span></span>
                    <span className="siv-bg-row">
                      <div className="siv-bg-tabs">
                        {(['solid', 'box', 'none'] as const).map(id => (
                          <button key={id} type="button" className={`siv-bg-tab${subtitleBackground === id ? ' on' : ''}`}
                            onClick={() => setSubtitleBackground(id)}>{id === 'solid' ? 'Đặc' : id === 'box' ? 'Hộp' : 'Tắt'}</button>
                        ))}
                      </div>
                      {subtitleBackground !== 'none' && <>
                        <input type="color" className="siv-bg-color" value={subtitleBgColor} title="Màu nền" onChange={e => setSubtitleBgColor(e.target.value)} />
                        <input type="range" min={0} max={100} className="siv-bg-slider" value={subtitleOpacity} onChange={e => setSubtitleOpacity(Number(e.target.value))} />
                        <span className="siv-bg-pct">{subtitleOpacity}%</span>
                      </>}
                    </span>
                  </div>
                </div>
              )}
              <p className="siv-hint">{localize(
                locale,
                'Có timeline: dùng mốc thời gian trong file. Không có: ghép theo tên file, clip giữ thời lượng thật và mỗi ảnh 5 giây. SRT chỉ dùng để chèn phụ đề.',
                'With a timeline: use its timecodes. Without one: merge by filename, keep clip duration, and show each image for 5 seconds. SRT is only for captions.',
              )}</p>
            </div>
          ) : (
            <div className="siv-settings">
              <div className="siv-set-row siv-set-row--four">
                <label><span className="siv-setting-title">{t('Chất lượng xuất', 'Output quality')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('resolution') }}>i</span></span>
                  <select value={resolution} onChange={(e) => setResolution(e.target.value)}>
                    <option value="auto">{t('Auto (theo ảnh · 1080p)', 'Auto (based on media · 1080p)')}</option>
                    <option value="1920x1080">{t('1080p ngang (1920 × 1080)', '1080p landscape (1920 × 1080)')}</option>
                    <option value="1080x1920">{t('1080p dọc (1080 × 1920)', '1080p portrait (1080 × 1920)')}</option>
                    <option value="1080x1080">{t('1080p vuông (1080 × 1080)', '1080p square (1080 × 1080)')}</option>
                    <option value="1280x720">{t('720p ngang (1280 × 720)', '720p landscape (1280 × 720)')}</option>
                  </select>
                </label>
                <label><span className="siv-setting-title">FPS <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('fps') }}>i</span></span>
                  <select value={fps} onChange={(e) => setFps(Number(e.target.value))}>
                    <option>24</option><option>25</option><option>30</option><option>60</option>
                  </select>
                </label>
                <label><span className="siv-setting-title">{t('Chất lượng nén', 'Compression quality')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('quality') }}>i</span></span>
                  <select value={crf} onChange={(e) => setCrf(Number(e.target.value))}>
                    <option value="18">{t('Cao', 'High')}</option><option value="20">{t('Cân bằng', 'Balanced')}</option><option value="24">{t('Nhanh', 'Fast')}</option>
                  </select>
                </label>
                <label><span className="siv-setting-title">{t('Bộ mã hóa', 'Encoder')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('encoder') }}>i</span></span>
                  <select value={encoder} onChange={(e) => setEncoder(e.target.value)}>
                    <option value="auto">{t('Tự động', 'Automatic')}</option><option value="gpu">GPU</option><option value="cpu">CPU</option>
                  </select>
                </label>
              </div>
              <div className="siv-set-row siv-set-row--two">
                <label><span className="siv-setting-title">{t('Hiệu ứng', 'Effect')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('effect') }}>i</span></span>
                  <select value={effect} onChange={(e) => setEffect(e.target.value)}>
                    <option value="random">{t('Ngẫu nhiên', 'Random')}</option><option value="fade">Fade</option>
                    <option value="dissolve">Dissolve</option><option value="none">{t('Tắt', 'Off')}</option>
                  </select>
                </label>
                <label><span className="siv-setting-title">Zoom <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('zoom') }}>i</span></span>
                  <select value={zoom} onChange={(e) => setZoom(e.target.value)}>
                      <option value="off">{t('Tắt', 'Off')}</option><option value="random">{t('Ngẫu nhiên', 'Random')}</option>
                      <option value="zoomIn">Zoom in</option><option value="zoomOut">Zoom out</option>
                      <option value="left">Trái → phải</option><option value="right">Phải → trái</option>
                      <option value="up">Dưới → trên</option><option value="down">Trên → dưới</option>
                  </select>
                </label>
              </div>
              <div className="siv-set-row siv-set-row--three">
                <label><span className="siv-setting-title">Speed (%) <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('speed') }}>i</span></span>
                  <input type="number" min="25" max="400" value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
                </label>
                <label><span className="siv-setting-title">{t('Âm lượng', 'Volume')} (%) <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('volume') }}>i</span></span>
                  <input type="number" min="0" max="300" value={volume} onChange={(e) => setVolume(Number(e.target.value))} />
                </label>
                <label><span className="siv-setting-title">Preview (s) <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('preview') }}>i</span></span>
                  <input type="number" min="1" max="120" value={previewSeconds} onChange={(e) => setPreviewSeconds(Number(e.target.value))} />
                </label>
              </div>
              <div className="siv-set-row siv-set-row--three">
                <label><span className="siv-setting-title">{t('Xóa metadata', 'Remove metadata')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('metadata') }}>i</span></span>
                  <select value={removeMetadata ? 'on' : 'off'} onChange={(e) => setRemoveMetadata(e.target.value === 'on')}>
                    <option value="off">{t('Tắt', 'Off')}</option><option value="on">{t('Bật', 'On')}</option>
                  </select>
                </label>
                <label><span className="siv-setting-title">{t('Xóa logo gốc', 'Remove original logo')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('delogo') }}>i</span></span>
                  <select value={delogoEnabled ? 'on' : 'off'} onChange={(e) => setDelogoEnabled(e.target.value === 'on')}>
                    <option value="off">{t('Tắt', 'Off')}</option><option value="on">{t('Bật', 'On')}</option>
                  </select>
                </label>
                {delogoEnabled && <label><span className="siv-setting-title">{t('Tự định vị', 'Auto position')} <span role="button" tabIndex={0} className="siv-info" onClick={(e) => { e.stopPropagation(); setHelpKey('delogoAuto') }}>i</span></span>
                  <select value={delogoAuto ? 'on' : 'off'} onChange={(e) => setDelogoAuto(e.target.value === 'on')}>
                    <option value="on">{t('Bật', 'On')}</option><option value="off">{t('Tắt (kéo tay)', 'Off (drag manually)')}</option>
                  </select>
                </label>}
              </div>
              <div className="siv-logo siv-drawing-settings">
                <div className="siv-logo-head"><strong>{t('Vẽ ảnh tĩnh thành video', 'Turn still images into drawing videos')}</strong><label><input type="checkbox" checked={drawingEnabled} onChange={(e) => setDrawingEnabled(e.target.checked)} /> {t('Bật', 'Enable')}</label></div>
                <p className="siv-hint">{t('Mỗi ảnh tĩnh được vẽ thành clip theo đúng thời lượng timeline trước khi ghép. Video có sẵn giữ nguyên.', 'Each still image becomes a drawing clip for its timeline duration before merging. Existing videos remain unchanged.')}</p>
                {drawingEnabled && <div className="siv-set-row siv-set-row--four">
                  <label><span className="siv-setting-title">{t('Kiểu vẽ', 'Drawing style')}</span><select value={drawingMode} onChange={(e) => setDrawingMode(e.target.value)}><option value="hand">{t('Tay + bút', 'Hand + pen')}</option><option value="drawing">{t('Vẽ nét', 'Strokes')}</option></select></label>
                  <label><span className="siv-setting-title">{t('Dụng cụ', 'Tool')}</span><select value={drawingTool} onChange={(e) => setDrawingTool(e.target.value)}><option value="pencil">{t('Chì', 'Pencil')}</option><option value="pen">{t('Bút', 'Pen')}</option><option value="marker">Marker</option><option value="brush">{t('Cọ', 'Brush')}</option></select></label>
                  <label><span className="siv-setting-title">{t('Đường đi nét', 'Stroke route')}</span><select value={drawingStrokeOrder} onChange={(e) => setDrawingStrokeOrder(e.target.value)}><option value="natural">{t('Tự nhiên theo đối tượng', 'Natural by object')}</option><option value="outline">{t('Theo viền thật', 'True outlines')}</option><option value="region">{t('Từng vùng hoàn chỉnh', 'Complete one region')}</option><option value="reading">{t('Theo chữ · trái sang phải', 'Text · left to right')}</option><option value="center">{t('Từ tâm lan ra', 'Centre outward')}</option><option value="horizontal">{t('Quét ngang', 'Horizontal sweep')}</option><option value="vertical">{t('Quét dọc', 'Vertical sweep')}</option></select></label>
                  <label><span className="siv-setting-title">{t('Độ chi tiết', 'Detail')} · {drawingDetail}%</span><input type="range" min="10" max="100" value={drawingDetail} onChange={(e) => setDrawingDetail(Number(e.target.value))} /></label>
                  <label><span className="siv-setting-title">{t('Độ dày nét', 'Stroke thickness')} · {drawingThickness}px</span><input type="range" min="1" max="8" value={drawingThickness} onChange={(e) => setDrawingThickness(Number(e.target.value))} /></label>
                </div>}
              </div>
              <div className="siv-logo">
                <div className="siv-logo-head"><strong>{t('Logo / Watermark ZM AI TOOL', 'ZM AI TOOL logo / watermark')}</strong><label><input type="checkbox" checked={logoEnabled} onChange={(e) => setLogoEnabled(e.target.checked)} /> {t('Áp dụng', 'Apply')}</label></div>
                <div className="siv-logo-sources">
                  {(['text', 'image', 'icon'] as const).map((source) => <button key={source} className={logoSource === source ? 'active' : ''} onClick={() => setLogoSource(source)}>{source === 'text' ? `T  ${t('Chữ', 'Text')}` : source === 'image' ? `▧  ${t('Ảnh', 'Image')}` : '★  Icon'}</button>)}
                </div>
                {logoSource === 'text' && <><label>{t('Nội dung', 'Content')}<input value={logoText} onChange={(e) => setLogoText(e.target.value)} /></label><label>{t('Màu chữ', 'Font color')}<input type="color" value={logoColor} onChange={(e) => setLogoColor(e.target.value)} /></label></>}
                {logoSource === 'image' && <div className="siv-logo-file"><span title={watermarkPath}>{watermarkPath || t('Chưa chọn ảnh logo', 'No logo image selected')}</span><button className="siv-choose" onClick={() => chooseInputFile('watermark')}>{t('Chọn ảnh', 'Select image')}</button></div>}
                {logoSource === 'icon' && <label>Icon<select value={logoIcon} onChange={(e) => setLogoIcon(e.target.value)}><option>★</option><option>▶</option><option>●</option><option>◆</option></select></label>}
                {logoSource === 'text'
                  ? <label>{t('Cỡ chữ', 'Font size')}: {logoFontSize}px<input type="range" min="6" max="160" value={logoFontSize} onChange={(e) => setLogoFontSize(Number(e.target.value))} /></label>
                  : <label>{t('Kích thước', 'Size')}: {logoSize}%<input type="range" min="2" max="30" value={logoSize} onChange={(e) => setLogoSize(Number(e.target.value))} /></label>}
                <label>{t('Độ mờ', 'Opacity')}: {logoOpacity}%<input type="range" min="5" max="100" value={logoOpacity} onChange={(e) => setLogoOpacity(Number(e.target.value))} /></label>
                <div className="siv-logo-range"><label>X (%)<input type="number" min="0" max="100" value={logoX} onChange={(e) => setLogoX(Number(e.target.value))} /></label><label>Y (%)<input type="number" min="0" max="100" value={logoY} onChange={(e) => setLogoY(Number(e.target.value))} /></label></div>
                <label>{t('Chuyển động', 'Motion')}<select value={logoMotion} onChange={(e) => setLogoMotion(e.target.value)}><option value="fixed">{t('Cố định', 'Static')}</option><option value="random">{t('Ngẫu nhiên', 'Random')}</option></select></label>
                <label>{t('Phạm vi', 'Scope')}<select value={logoScope} onChange={(e) => setLogoScope(e.target.value)}><option value="full">{t('Toàn video', 'Entire video')}</option><option value="range">{t('Theo đoạn', 'Selected range')}</option></select></label>
                {logoMotion === 'random' && <div className="siv-logo-motion"><label>Hiện (s)<input type="number" min="0.5" step="0.1" value={logoVisibleSec} onChange={(e) => setLogoVisibleSec(Number(e.target.value))} /></label><label>Ẩn (s)<input type="number" min="0" step="0.1" value={logoHiddenSec} onChange={(e) => setLogoHiddenSec(Number(e.target.value))} /></label><label>Fade (s)<input type="number" min="0" step="0.1" value={logoFadeSec} onChange={(e) => setLogoFadeSec(Number(e.target.value))} /></label><label>Lề (%)<input type="number" min="0" max="20" value={logoSafeMargin} onChange={(e) => setLogoSafeMargin(Number(e.target.value))} /></label></div>}
                {logoScope === 'range' && <div className="siv-logo-range"><label>Hiện từ<input type="number" min="0" value={logoStart} onChange={(e) => setLogoStart(Number(e.target.value))} /></label><label>Đến<input type="number" min="0" value={logoEnd} onChange={(e) => setLogoEnd(Number(e.target.value))} /></label></div>}
              </div>
            </div>
          )}
        </div>

        {tab === 'project' ? (
          <>
            <div className="siv-progress">
              <span>Tiến độ</span>
              <progress max="100" value={job?.progress || 0} />
              <b>{Math.round(job?.progress || 0)}%</b>
            </div>
            <div className="siv-log">
              <header>
                <strong>Log chi tiết</strong>
                <div>
                  <button type="button" onClick={() => void copyText(logText)}>Copy</button>
                  <button type="button" onClick={() => { setLogStart(job?.logs?.length || 0); toast.success(t('Đã xóa log.', 'Logs cleared.')) }}>Xóa</button>
                </div>
              </header>
              <pre ref={logRef}>{logText}</pre>
            </div>
            <footer className="siv-actions">
              <button className="primary" disabled={busy || !mediaFolder} onClick={() => start(false)}>
                {sending ? 'ĐANG TẢI…' : 'RENDER'}
              </button>
              <button disabled={busy || !mediaFolder} onClick={() => start(true)}>Preview</button>
              <button disabled={!job || !['processing', 'paused'].includes(job.status)} onClick={togglePause}>
                {job?.status === 'paused' ? 'Tiếp tục' : 'Tạm dừng'}
              </button>
              <button disabled={!busy} onClick={cancel}>Hủy</button>
              <button onClick={openFolder}>Thư mục</button>
              <span>{statusText}</span>
            </footer>
          </>
        ) : (
          <footer className="siv-actions">
            <button className="primary" onClick={() => { setTab('project'); toast.success(t('Đã lưu cài đặt.', 'Settings saved.')) }}>Lưu</button>
            <button onClick={() => { cancelSettings(); toast.info(t('Đã hủy thay đổi cài đặt.', 'Settings changes cancelled.')) }}>Hủy</button>
          </footer>
        )}
          </section>
        </div>
        
        <div className="siv-preview-pane">
          <div className="siv-preview-header">
            <span>{job?.status === 'done' ? 'Video Output' : 'Preview trực tiếp'}</span>
            {job?.status === 'done' && job.id && (<>
              <button
                className="siv-btn-sm"
                onClick={() => fetch(`/api/srt-image/jobs/${job.id}/open`, { method: 'POST' })}
                title="Mở bằng app mặc định"
                style={{ marginLeft: 'auto', fontSize: '0.72rem', padding: '2px 8px' }}
              >▶ Mở video</button>
              <button
                className="siv-btn-sm"
                onClick={() => setJob(null)}
                title="Quay lại xem trước"
                style={{ fontSize: '0.72rem', padding: '2px 8px' }}
              >✕ Xem trước</button>
            </>)}
            {job?.status === 'done' && job.name && (
              <span style={{ color: 'var(--muted-foreground)', fontSize: '0.75rem' }}>{job.name}</span>
            )}
          </div>
          {job?.status === 'done' && job.id ? (
            <div className="siv-output-stage">
              <div className="siv-output-frame">
                <video
                  key={job.id}
                  src={`/api/srt-image/jobs/${job.id}/file`}
                  controls
                  autoPlay
                  preload="auto"
                />
              </div>
            </div>
          ) : (
            <SubtitleLivePreview
              fontFamily={subtitleFontFamily}
              textColor={subtitleColor}
              bgStyle={subtitleBackground}
              bgColor={subtitleBgColor}
              bgOpacity={subtitleOpacity}
              fontSize={subtitleSize}
              marginBottom={subtitleMargin}
              resolution={resolution}
              onResolutionChange={setResolution}
              platform={targetPlatform}
              onPlatformChange={setTargetPlatform}
              mediaFolder={mediaFolder}
              delogoEnabled={delogoEnabled}
              delogoAuto={delogoAuto}
              delogoRect={delogoRect}
              onDelogoRectChange={setDelogoRect}
              logo={logoProp}
            />
          )}
        </div>
      </div>

      {helpKey && (
        <div className="siv-help-backdrop" role="presentation" onMouseDown={() => setHelpKey(null)}>
          <section className={`siv-help-dialog${helpKey === 'timeline' ? ' siv-help-dialog--timeline' : ''}`} role="dialog" aria-modal="true" aria-labelledby="siv-help-title" onMouseDown={(event) => event.stopPropagation()}>
            <header>
              <div>
                <small>{t('Hướng dẫn sử dụng', 'User Guide')}</small>
                <h2 id="siv-help-title">{helpKey === 'timeline' ? t('File timeline & Các định dạng Timecode', 'Timeline File & Timecode Formats') : helpText(helpKey)[0]}</h2>
              </div>
              <button type="button" aria-label={t('Đóng hướng dẫn', 'Close guide')} onClick={(e) => { e.stopPropagation(); setHelpKey(null) }}>×</button>
            </header>
            {helpKey === 'timeline' ? (
              <div className="siv-help-scroll-content">
                <p style={{ padding: '12px 0 8px', margin: 0, lineHeight: 1.55 }}>
                  {t(
                    'File timeline xác định thời điểm và độ dài từng cảnh. Ảnh/clip trong thư mục media sẽ tự động khớp lần lượt theo thứ tự tên file (001, 002, 003…). Hỗ trợ TXT, SRT, VTT, ASS/SSA, CSV, TSV, JSON và LRC cùng nhiều dạng timecode:',
                    'The timeline specifies the start and duration of each scene. Media files are matched sequentially in alphabetical order (001, 002, 003...). Supports TXT, SRT, VTT, ASS/SSA, CSV, TSV, JSON and LRC with several timecode formats:',
                  )}
                </p>

                <div className="siv-help-timeline-formats">
                  <div className="siv-help-format-card">
                    <div className="siv-help-format-title">📝 1. {t('Dạng Text Prompt / TXT (Khuyên dùng)', 'Text Prompt / TXT Format (Recommended)')}</div>
                    <p className="siv-help-format-desc">{t('Dạng ngoặc vuông hoặc khoảng cách gạch ngang, hỗ trợ cả dấu hai chấm (:) và dấu chấm (.):', 'Square bracket or hyphen range, supports both colons (:) and dots (.):')}</p>
                    <pre className="siv-help-code-block">{`001_[00:00:00.00-00:00:08.50] Mô tả cảnh mở đầu
002_[00:00:08.50-00:00:15.00] Mô tả diễn biến tiếp
003_[00.00.15.00-00.00.22.00] Cảnh quay trên cao
[00:00 - 00:05] Cảnh 1
[00:05 - 00:12] Cảnh 2
00:00:00 - 00:00:08
00:00:08 --> 00:00:16
0 - 5.5
5.5 - 12.0`}</pre>
                  </div>

                  <div className="siv-help-format-card">
                    <div className="siv-help-format-title">💬 2. {t('File Phụ đề (.SRT / .VTT / .ASS / .SSA)', 'Subtitle Files (.SRT / .VTT / .ASS / .SSA)')}</div>
                    <p className="siv-help-format-desc">{t('Mỗi mốc timecode phụ đề là 1 cảnh video tương ứng:', 'Each subtitle timecode block corresponds to 1 video scene:')}</p>
                    <pre className="siv-help-code-block">{`1
00:00:00,000 --> 00:00:05,500
Cảnh 1

2
00:00:05,500 --> 00:00:12,000
Cảnh 2`}</pre>
                  </div>

                  <div className="siv-help-format-card">
                    <div className="siv-help-format-title">📊 3. {t('Bảng tính (.CSV / .TSV)', 'Spreadsheet Table (.CSV / .TSV)')}</div>
                    <p className="siv-help-format-desc">{t('Bảng có cột start và end (hoặc from/to, in/out):', 'Table with start and end columns (or from/to, in/out):')}</p>
                    <pre className="siv-help-code-block">{`start,end
00:00:00,00:00:05.5
00:00:05.5,00:00:12.0`}</pre>
                  </div>

                  <div className="siv-help-format-card">
                    <div className="siv-help-format-title">📦 4. {t('Cấu trúc JSON (.JSON)', 'JSON Structure (.JSON)')}</div>
                    <pre className="siv-help-code-block">{`[
  { "start": 0, "end": 5.5 },
  { "start": "00:00:05.5", "end": "00:00:12.0" }
]`}</pre>
                  </div>

                  <div className="siv-help-format-card">
                    <div className="siv-help-format-title">🎵 5. {t('Lời bài hát (.LRC)', 'Lyrics (.LRC)')}</div>
                    <pre className="siv-help-code-block">{`[00:01.50] Câu hát 1
[00:06.20] Câu hát 2`}</pre>
                  </div>
                </div>

                <div style={{ margin: '8px 0 0', padding: '10px 12px' }}>
                  <strong>{t('💡 Lưu ý quan trọng', '💡 Important Notes')}</strong>
                  <p style={{ margin: '4px 0 0', fontSize: '0.8rem' }}>
                    {t(
                      'Không bắt buộc phải có timeline. Khi không dùng timeline, APP sẽ tự động ghép tuần tự toàn bộ ảnh/video theo tên file (ảnh tĩnh mặc định 5s, clip video giữ nguyên độ dài gốc). Bạn có thể bấm [Đổi] để dán trực tiếp timeline mà không cần tạo file.',
                      'A timeline is not mandatory. Without a timeline, the APP automatically merges all media in filename order (5s default per image, original length for videos). You can click [Switch] to paste timeline text directly without saving a file.',
                    )}
                  </p>
                </div>
              </div>
            ) : (
              <>
                <p>{helpText(helpKey)[1]}</p>
                <div><strong>{t('File hoặc thiết lập cần dùng', 'Required file or setting')}</strong><p>{helpText(helpKey)[2]}</p></div>
              </>
            )}
            <button type="button" className="siv-help-close" onClick={(e) => { e.stopPropagation(); setHelpKey(null) }}>{t('Đã hiểu', 'Got it')}</button>
          </section>
        </div>
      )}
      {missingMedia && (
        <div className="siv-help-backdrop" role="presentation" onMouseDown={() => setMissingMedia(null)}>
          <section className="siv-help-dialog siv-missing-dialog" role="alertdialog" aria-modal="true" aria-labelledby="siv-missing-title" onMouseDown={(event) => event.stopPropagation()}>
            <header>
              <div>
                <small>{t('Cảnh báo thiếu media', 'Missing Media Warning')}</small>
                <h2 id="siv-missing-title">{t('Thiếu ảnh/video', 'Missing image/video')}</h2>
              </div>
              <button type="button" aria-label={t('Đóng cảnh báo', 'Close warning')} onClick={() => setMissingMedia(null)}>×</button>
            </header>
            <div className="siv-missing-scroll">
              <div className="siv-missing-summary">
                <strong>{t('Thư mục media hiện có:', 'Current media folder:')} {missingMedia.available} file</strong>
                <span> · {t('Timeline yêu cầu:', 'Timeline requires:')} {missingMedia.required} {t('cảnh', 'scenes')} ➔ <strong>{t('Thiếu:', 'Missing:')} {missingMedia.missing_count || (missingMedia.required - missingMedia.available)} {t('file', 'files')}</strong></span>
                {missingMedia.available_files && missingMedia.available_files.length > 0 && (
                  <p>
                    {t('File đã có:', 'Existing files:')} {missingMedia.available_files.slice(0, 8).join(', ')}
                    {missingMedia.available_files.length > 8 ? ` +${missingMedia.available_files.length - 8} ${t('file khác', 'more files')}` : ''}
                  </p>
                )}
              </div>

              <div className="siv-missing-list-header">
                <span>{t('Danh sách các cảnh bị thiếu ảnh/video:', 'Missing scenes & expected files:')}</span>
                <span>{missingMedia.missing_cues?.length || (missingMedia.required - missingMedia.available)} {t('cảnh thiếu', 'missing')}</span>
              </div>

              {missingMedia.missing_cues && missingMedia.missing_cues.length > 0 ? (
                <div className="siv-missing-list">
                  {missingMedia.missing_cues.map((cue) => (
                    <div key={cue.index} className="siv-missing-item">
                      <div className="siv-missing-item-top">
                        <span className="siv-missing-badge">{t(`Cảnh #${cue.index}`, `Scene #${cue.index}`)}</span>
                        <span className="siv-missing-timecode">⏱ {cue.timecode} {cue.duration ? `(${cue.duration}s)` : ''}</span>
                      </div>
                      <div className="siv-missing-expected">
                        <span>{t('File cần thêm:', 'Expected file:')}</span>
                        <b>{cue.expected_name || `${String(cue.index).padStart(3, '0')}.*`}</b>
                      </div>
                      {cue.label ? (
                        <div className="siv-missing-prompt" title={cue.label}>
                          {cue.label}
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : (
                <p style={{ margin: 0, fontSize: '0.84rem' }}>
                  {t(
                    `Thiếu ${missingMedia.required - missingMedia.available} file từ cảnh #${missingMedia.available + 1} đến cảnh #${missingMedia.required}.`,
                    `Missing ${missingMedia.required - missingMedia.available} files from scene #${missingMedia.available + 1} to scene #${missingMedia.required}.`,
                  )}
                </p>
              )}

              <p style={{ fontSize: '0.78rem', color: 'var(--muted-foreground)', margin: 0, lineHeight: 1.45 }}>
                {t(
                  '💡 Bạn có thể thêm các file còn thiếu vào thư mục media rồi bấm Render lại, hoặc bấm [Vẫn tạo] để render với số file hiện có (các cảnh thiếu sẽ tự động bị bỏ qua).',
                  '💡 You can add the missing files into your media folder and Render again, or click [Create anyway] to render with current files (missing scenes will be skipped).',
                )}
              </p>
            </div>
            <footer>
              <button type="button" onClick={() => setMissingMedia(null)}>{t('Quay lại', 'Go back')}</button>
              <button type="button" onClick={() => void openFolder()}>{t('Mở thư mục', 'Open folder')}</button>
              <button
                type="button"
                className="primary"
                onClick={() => {
                  const preview = missingMedia.preview
                  setMissingMedia(null)
                  void start(preview, true)
                }}
              >{t('Vẫn tạo', 'Create anyway')}</button>
            </footer>
          </section>
        </div>
      )}
    </main>
  )
}

// ─── Live Preview ────────────────────────────────────────────────────────────
type PreviewProps = {
  fontFamily: string
  textColor: string
  bgStyle: string
  bgColor: string
  bgOpacity: number
  fontSize: number
  marginBottom: number
  resolution: string
  onResolutionChange: (r: string) => void
  platform: string
  onPlatformChange: (platform: string) => void
  mediaFolder: string

  delogoEnabled?: boolean
  delogoAuto?: boolean
  delogoRect?: { x: number; y: number; w: number; h: number }
  onDelogoRectChange?: (r: { x: number; y: number; w: number; h: number }) => void
  logo?: {
    enabled: boolean
    source: 'text' | 'image' | 'icon'
    text: string
    icon: string
    imagePath: string
    fontSize: number
    size: number
    color: string
    opacity: number
    x: number
    y: number
    motion: string
    scope: string
    start: number
    end: number
    visibleSec: number
    hiddenSec: number
    fadeSec: number
    safeMargin: number
  }
}

const SAMPLE_LINES = ['Đây là dòng phụ đề mẫu,', 'hiển thị trực tiếp theo cài đặt.']

// Platform → resolution mapping (exact specs)
const PLATFORM_OPTIONS = [
  { id: 'auto',      label: 'Media',      res: 'auto',      w: 16, h: 9,
    icon: <svg viewBox="0 0 24 24" fill="none" width="16" height="16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10 5-3v10l-5-3z"/></svg> },
  { id: 'tiktok',    label: 'TikTok',     res: '1080x1920', w: 9,  h: 16,
    icon: <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M19.59 6.69a4.83 4.83 0 01-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 01-2.88 2.5 2.89 2.89 0 01-2.89-2.89 2.89 2.89 0 012.89-2.89c.28 0 .54.04.79.1V9.01a6.29 6.29 0 00-.79-.05 6.34 6.34 0 00-6.34 6.34 6.34 6.34 0 006.34 6.34 6.34 6.34 0 006.33-6.34V8.82a8.18 8.18 0 004.79 1.53V6.88a4.85 4.85 0 01-1.02-.19z"/></svg> },
  { id: 'yt-shorts', label: 'Shorts',      res: '1080x1920', w: 9,  h: 16,
    icon: <svg viewBox="0 0 24 24" fill="#FF0000" width="16" height="16"><path d="M10 15l5.19-3L10 9v6m11.56-7.83c.13.47.22 1.1.28 1.9.07.8.1 1.49.1 2.09L22 12c0 2.19-.16 3.8-.44 4.83-.25.9-.83 1.48-1.73 1.73-.47.13-1.33.22-2.65.28-1.3.07-2.49.1-3.59.1L12 19c-4.19 0-6.8-.16-7.83-.44-.9-.25-1.48-.83-1.73-1.73-.13-.47-.22-1.1-.28-1.9-.07-.8-.1-1.49-.1-2.09L2 12c0-2.19.16-3.8.44-4.83.25-.9.83-1.48 1.73-1.73.47-.13 1.33-.22 2.65-.28 1.3-.07 2.49-.1 3.59-.1L12 5c4.19 0 6.8.16 7.83.44.9.25 1.48.83 1.73 1.73z"/></svg> },
  { id: 'youtube',   label: 'YouTube',     res: '1920x1080', w: 16, h: 9,
    icon: <svg viewBox="0 0 24 24" fill="#FF0000" width="16" height="16"><path d="M10 15l5.19-3L10 9v6m11.56-7.83c.13.47.22 1.1.28 1.9.07.8.1 1.49.1 2.09L22 12c0 2.19-.16 3.8-.44 4.83-.25.9-.83 1.48-1.73 1.73-.47.13-1.33.22-2.65.28-1.3.07-2.49.1-3.59.1L12 19c-4.19 0-6.8-.16-7.83-.44-.9-.25-1.48-.83-1.73-1.73-.13-.47-.22-1.1-.28-1.9-.07-.8-.1-1.49-.1-2.09L2 12c0-2.19.16-3.8.44-4.83.25-.9.83-1.48 1.73-1.73.47-.13 1.33-.22 2.65-.28 1.3-.07 2.49-.1 3.59-.1L12 5c4.19 0 6.8.16 7.83.44.9.25 1.48.83 1.73 1.73z"/></svg> },
  { id: 'facebook',  label: 'Facebook',    res: '1080x1920', w: 9,  h: 16,
    icon: <svg viewBox="0 0 24 24" fill="#1877F2" width="16" height="16"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg> },
]

const SubtitleLivePreview = memo(function SubtitleLivePreview({ fontFamily, textColor, bgStyle, bgColor, bgOpacity, fontSize, marginBottom, resolution, onResolutionChange, platform, onPlatformChange, mediaFolder, delogoEnabled, delogoAuto, delogoRect, onDelogoRectChange, logo }: PreviewProps) {
  const { locale } = useLocale()
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  // Fetch kích thước thực tế của media khi resolution=auto
  const [mediaAp, setMediaAp] = useState<{ w: number; h: number } | null>(null)
  useEffect(() => {
    if (resolution !== 'auto' || !mediaFolder) { setMediaAp(null); return }
    fetch(`/api/srt-image/media-size?folder=${encodeURIComponent(mediaFolder)}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => setMediaAp(d))
      .catch(() => setMediaAp(null))
  }, [resolution, mediaFolder])

  // Derive aspect ratio from resolution setting — this is the single source of truth
  const resMap: Record<string, { w: number; h: number }> = {
    'auto':      { w: 16, h: 9 }, // fallback nếu chưa chọn thư mục
    '1920x1080': { w: 16, h: 9 },
    '1280x720':  { w: 16, h: 9 },
    '1080x1920': { w: 9,  h: 16 },
    '1080x1080': { w: 1,  h: 1 },
  }
  // Ưu tiên kích thước thực tế từ media khi đang ở chế độ auto
  const ap = (resolution === 'auto' && mediaAp) ? mediaAp : (resMap[resolution] ?? { w: 16, h: 9 })
  // Several platforms share 1080×1920. Keep the target in the project state
  // so its platform-safe subtitle placement is also sent to the renderer.
  useEffect(() => {
    const selected = PLATFORM_OPTIONS.find(p => p.id === platform)
    if (!selected || selected.res !== resolution) {
      onPlatformChange(PLATFORM_OPTIONS.find(p => p.res === resolution)?.id ?? 'auto')
    }
  }, [resolution, platform, onPlatformChange])
  const activePlatform = platform
  const frameRef = useRef<HTMLDivElement>(null)
  const [frameH, setFrameH] = useState(300)
  const [thumbIdx, setThumbIdx] = useState(0)
  const [thumbTotal, setThumbTotal] = useState(0)
  const [mediaPreviewError, setMediaPreviewError] = useState(false)
  useEffect(() => {
    setThumbIdx(0)
    setThumbTotal(0)
    setMediaPreviewError(false)
  }, [mediaFolder])
  const [logoClock, setLogoClock] = useState(0)
  useEffect(() => {
    if (!logo?.enabled || logo.motion !== 'random') { setLogoClock(0); return }
    const startedAt = performance.now()
    const timer = window.setInterval(() => setLogoClock((performance.now() - startedAt) / 1000), 100)
    return () => window.clearInterval(timer)
  }, [logo?.enabled, logo?.motion])

  // Đo chiều cao frame thực tế để tính scale đúng với ASS (PlayResY=288)
  useEffect(() => {
    if (!frameRef.current) return
    const ro = new ResizeObserver(e => setFrameH(e[0].contentRect.height))
    ro.observe(frameRef.current)
    return () => ro.disconnect()
  }, [])

  const fakeSettings = {
    captionTextColor: textColor,
    captionBgStyle: bgStyle as 'none' | 'solid' | 'box' | 'blur',
    captionBgColor: bgColor,
    captionBgOpacity: bgOpacity,
    subtitleFontFamily: fontFamily,
    captionStroke: true,
  // ponytail: cast minimal fake settings; chỉ dùng các field mà captionChromeStyle đọc
  } as Parameters<typeof captionChromeStyle>[0]

  const captionStyle = captionChromeStyle(fakeSettings)
  const fontCss = captionFontCss(fontFamily)

  // Scale khớp ASS: FontSize/MarginV đều dùng PlayResY=288 làm reference
  // MarginV đo từ đáy frame — chrome UI chỉ là overlay visual, không ảnh hưởng vị trí ASS
  const scale = frameH / 288
  // ponytail: ASS FontSize = em-square, CSS fontSize = bounding box (ascender+descender).
  // Hệ số ~0.75 để CSS khớp kết quả libass render thực tế.
  const scaledFontSize = Math.round(fontSize * scale * 0.75)
  // Facebook Reels reserves more room for its lower action and description UI.
  // The same safe area is sent to the backend as targetPlatform.
  const platformMargin = activePlatform === 'facebook' ? 16 : 0
  const scaledMarginBottom = Math.round((marginBottom + platformMargin) * scale)
  // ponytail: ASS Outline (padding hộp nền) là giá trị cố định PlayRes, không phải em-based
  // solid: Outline=1.2, box: Outline=2 — scale giống fontSize/margin
  const outlinePx = bgStyle === 'box' ? Math.round(2 * scale) : bgStyle === 'solid' ? Math.round(1.2 * scale) : 0
  const logoCycle = Math.max(0.5, Number(logo?.visibleSec || 4)) + Math.max(0, Number(logo?.hiddenSec || 0))
  const logoStep = Math.floor(logoClock / logoCycle)
  const logoRandomPositions = [[0.08, 0.10], [0.90, 0.62], [0.12, 0.60], [0.88, 0.12], [0.50, 0.68], [0.50, 0.08], [0.08, 0.68], [0.78, 0.38]]
  const logoRandomPosition = logoRandomPositions[logoStep % logoRandomPositions.length]
  const logoMargin = Math.max(0, Math.min(20, Number(logo?.safeMargin || 0)))
  const logoXPreview = logo?.motion === 'random'
    ? logoMargin + logoRandomPosition[0] * (100 - logoMargin * 2)
    : Math.max(0, Math.min(100, Number(logo?.x || 0)))
  const logoYPreview = logo?.motion === 'random'
    ? logoMargin + logoRandomPosition[1] * Math.max(0, 75 - logoMargin * 2)
    : Math.max(0, Math.min(100, Number(logo?.y || 0)))
  const logoLocalTime = logoClock % logoCycle
  const logoVisible = logo?.motion !== 'random' || logoLocalTime < Math.max(0.5, Number(logo.visibleSec || 4))
  const logoInScope = logo?.scope !== 'range' || (logoClock >= Number(logo.start || 0) && logoClock <= Number(logo.end || 0))
  const logoFade = logo?.source === 'image' ? 0 : Math.max(0, Math.min(Number(logo?.fadeSec || 0), Number(logo?.visibleSec || 4) / 2))
  const logoFadeFactor = !logoVisible ? 0 : logo?.motion !== 'random' || !logoFade ? 1 : Math.min(
    1,
    logoLocalTime / logoFade,
    (Number(logo.visibleSec) - logoLocalTime) / logoFade,
  )


  return (
    <div className="siv-live-preview-wrap">
      <div className="siv-live-aspect-toggle">
        {PLATFORM_OPTIONS.map(p => (
          <button
            key={p.id}
            type="button"
            className={p.id === activePlatform ? 'active' : ''}
            onClick={() => {
              onPlatformChange(p.id)
              onResolutionChange(p.res)
            }}
            title={`${p.label} · ${p.res}`}
          >
            {p.icon}
            <span>{p.label}</span>
          </button>
        ))}
      </div>
      <div className="siv-live-preview-stage">
        <div
          ref={frameRef}
          className="siv-live-preview-frame"
          style={{
            aspectRatio: `${ap.w}/${ap.h}`,
            ...(ap.h > ap.w
              ? { height: '100%', width: 'auto' }
              : { width: '100%' }),
          }}
        >
            <>
              {/* Media thật luôn hiển thị; delogo chỉ điều khiển vùng chọn logo. */}
              {mediaFolder ? (
                <>
                  <img
                    key={`${mediaFolder}:${thumbIdx}`}
                    src={`/api/srt-image/media-thumb?folder=${encodeURIComponent(mediaFolder)}&index=${thumbIdx}`}
                    alt=""
                    style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', opacity: mediaPreviewError ? 0 : 0.85 }}
                    onLoad={(e) => {
                      setMediaPreviewError(false)
                      // Read total count from X-Total header via fetch (img tag has no header access)
                      if (thumbTotal === 0) {
                        const url = (e.currentTarget as HTMLImageElement).src
                        fetch(url, { method: 'HEAD' }).then(r => {
                          const n = Number(r.headers.get('x-total'))
                          if (n > 0) setThumbTotal(n)
                        }).catch(() => {})
                      }
                    }}
                    onError={() => setMediaPreviewError(true)}
                  />
                  {mediaPreviewError && (
                    <div className="siv-live-preview-bg" role="status">
                      {t('Không tải được media xem trước', 'Unable to load preview media')}
                    </div>
                  )}
                  <div style={{
                    position: 'absolute', top: 6, right: 6, zIndex: 22,
                    display: 'flex', gap: 3, alignItems: 'center',
                  }}>
                    <button onClick={() => setThumbIdx(i => thumbTotal > 0 ? (i - 1 + thumbTotal) % thumbTotal : Math.max(0, i - 1))} style={{
                      width: 22, height: 22, border: 'none', borderRadius: 4,
                      background: 'rgba(0,0,0,0.55)', color: '#fff', fontSize: 13, cursor: 'pointer', lineHeight: 1,
                    }}>◀</button>
                    <span style={{ fontSize: 10, color: '#fff', textShadow: '0 1px 3px #000', fontWeight: 600 }}>{thumbIdx + 1}{thumbTotal > 0 ? `/${thumbTotal}` : ''}</span>
                    <button onClick={() => setThumbIdx(i => thumbTotal > 0 ? (i + 1) % thumbTotal : i + 1)} style={{
                      width: 22, height: 22, border: 'none', borderRadius: 4,
                      background: 'rgba(0,0,0,0.55)', color: '#fff', fontSize: 13, cursor: 'pointer', lineHeight: 1,
                    }}>▶</button>
                  </div>
                </>
              ) : (
                <div className="siv-live-preview-bg" />
              )}
              {/* Platform UI chrome overlay */}
              <PlatformChrome platform={activePlatform} />
              {logo?.enabled && logoInScope && (
                <div
                  className="siv-live-logo"
                  style={{
                    left: `${logoXPreview}%`,
                    top: `${logoYPreview}%`,
                    color: logo.color,
                    opacity: Math.max(0.05, Math.min(1, logo.opacity / 100)) * logoFadeFactor,
                    fontSize: logo.source === 'text' ? `${Math.max(6, logo.fontSize) * scale}px` : undefined,
                    width: logo.source === 'image' ? `${Math.max(2, Math.min(30, logo.size))}%` : undefined,
                  }}
                >
                  {logo.source === 'image' && logo.imagePath
                    ? <img src={`/api/srt-image/logo-preview?path=${encodeURIComponent(logo.imagePath)}`} alt="" />
                    : logo.source === 'icon' ? logo.icon : logo.text}
                </div>
              )}
              {/* Phụ đề mẫu */}
              <div
                className="siv-live-caption-wrap"
                style={{ paddingBottom: `${scaledMarginBottom}px` }}
              >
                {SAMPLE_LINES.map((line, i) => (
                  <div
                    key={i}
                    className="siv-live-caption-line"
                    style={{
                      ...captionStyle,
                      fontFamily: fontCss,
                      fontSize: `${scaledFontSize}px`,
                      lineHeight: 1.15,
                      ...(outlinePx > 0 && { padding: `${outlinePx}px` }),
                    }}
                  >
                    {line}
                  </div>
                ))}
              </div>
              {/* Delogo: vùng xóa logo */}
              {delogoEnabled && delogoRect && (
                <div
                  style={{
                    position: 'absolute', zIndex: 20,
                    left: `${delogoRect.x}%`, top: `${delogoRect.y}%`,
                    width: `${delogoRect.w}%`, height: `${delogoRect.h}%`,
                    border: '1.5px solid rgba(0,180,255,0.7)',
                    borderRadius: 3,
                    background: 'linear-gradient(135deg, rgba(0,180,255,0.12), rgba(0,120,255,0.08))',
                    boxShadow: '0 0 0 1px rgba(0,0,0,0.15), inset 0 0 8px rgba(0,180,255,0.08)',
                    pointerEvents: 'none',
                  }}
                >
                  <span style={{
                    position: 'absolute', top: -18, left: 0,
                    fontSize: 9, fontWeight: 600, lineHeight: '16px',
                    padding: '0 5px', borderRadius: '3px 3px 0 0',
                    background: 'rgba(0,180,255,0.85)', color: '#fff',
                    whiteSpace: 'nowrap',
                  }}>✕ Logo</span>
                </div>
              )}
              {/* Drag overlay — chỉ khi delogo bật VÀ tự định vị tắt */}
              {delogoEnabled && !delogoAuto && (
                <div
                  style={{ position: 'absolute', inset: 0, zIndex: 21, cursor: 'crosshair' }}
                  onMouseDown={(e) => {
                    const el = e.currentTarget
                    const rect = el.getBoundingClientRect()
                    const startX = ((e.clientX - rect.left) / rect.width) * 100
                    const startY = ((e.clientY - rect.top) / rect.height) * 100
                    const onMove = (ev: MouseEvent) => {
                      const curX = Math.max(0, Math.min(100, ((ev.clientX - rect.left) / rect.width) * 100))
                      const curY = Math.max(0, Math.min(100, ((ev.clientY - rect.top) / rect.height) * 100))
                      const nx = Math.min(startX, curX), ny = Math.min(startY, curY)
                      const nw = Math.abs(curX - startX), nh = Math.abs(curY - startY)
                      if (nw > 1 && nh > 0.5) onDelogoRectChange?.({ x: +nx.toFixed(1), y: +ny.toFixed(1), w: +nw.toFixed(1), h: +nh.toFixed(1) })
                    }
                    const onUp = () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
                    window.addEventListener('mousemove', onMove)
                    window.addEventListener('mouseup', onUp)
                  }}
                />
              )}
            </>
        </div>
      </div>
    </div>
  )
})

// ─── Platform Chrome Overlay (accurate per-platform layouts) ─────────────────
// ponytail: shared icon helpers avoid repetition across 4 platform branches
const IcoHeart   = () => <svg viewBox="0 0 24 24" width="22" height="22" fill="white"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>
const IcoComment = () => <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="white" strokeWidth="2" strokeLinejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
const IcoShare   = () => <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round"><path d="M4 12v8a2 2 0 002 2h12a2 2 0 002-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>
const IcoBookmark= () => <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
const IcoThumbUp = () => <svg viewBox="0 0 24 24" width="22" height="22" fill="white"><path d="M2 20h2c.55 0 1-.45 1-1v-9H2v10zm19.83-7.12c.11-.25.17-.52.17-.88v-2c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L13.17 1 6.59 7.59C6.22 7.95 6 8.45 6 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05-.03.15z"/></svg>
const IcoThumbDn = () => <svg viewBox="0 0 24 24" width="22" height="22" fill="white"><path d="M22 4h-2c-.55 0-1 .45-1 1v9h2V4zM2.17 11.12c-.11.25-.17.52-.17.88v2c0 1.1.9 2 2 2h6.31l-.95 4.57c0 .41.17.79.44 1.06L10.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2H7c-.83 0-1.54.5-1.84 1.22L2.17 11.12z"/></svg>
const IcoMore3Vert=()=> <svg viewBox="0 0 24 24" width="20" height="20" fill="white"><circle cx="12" cy="5" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="12" cy="19" r="1.8"/></svg>
const IcoMore3Horiz=()=><svg viewBox="0 0 24 24" width="20" height="20" fill="white"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>
const IcoMusic   = () => <svg viewBox="0 0 24 24" width="10" height="10" fill="white"><path d="M9 18V5l12-2v13M6 21a3 3 0 100-6 3 3 0 000 6zM18 19a3 3 0 100-6 3 3 0 000 6z"/></svg>
const IcoAvatar  = ({ s = 22 }: { s?: number }) => (
  <svg viewBox="0 0 24 24" width={s} height={s}>
    <circle cx="12" cy="12" r="11" fill="rgba(255,255,255,0.18)" stroke="white" strokeWidth="1.2"/>
    <circle cx="12" cy="9" r="4" fill="rgba(255,255,255,0.55)"/>
    <path d="M4 22c0-4.4 3.6-8 8-8s8 3.6 8 8" fill="rgba(255,255,255,0.55)"/>
  </svg>
)
const Bar = () => <div className="plat-statusbar"><span>9:41</span><span>▐▐ ◈</span></div>
const Act = ({ ico, lbl }: { ico: React.ReactNode; lbl?: string }) => (
  <div className="plat-action">{ico}{lbl && <small>{lbl}</small>}</div>
)

const PlatformChrome = memo(function PlatformChrome({ platform }: { platform: string }) {
  if (platform === 'tiktok') return (
    <div className="plat-chrome plat-tiktok">
      <Bar/>
      {/* Right sidebar — Avatar · Like · Comment · Bookmark · Share · Disc */}
      <div className="plat-right">
        <div className="plat-action" style={{marginBottom:4}}>
          <div className="plat-tt-avatar"><IcoAvatar s={28}/><div className="plat-tt-plus">+</div></div>
        </div>
        <Act ico={<IcoHeart/>}    lbl="2.8M"/>
        <Act ico={<IcoComment/>}  lbl="48K"/>
        <Act ico={<IcoBookmark/>} lbl="12K"/>
        <Act ico={<IcoShare/>}    lbl="Share"/>
        <div className="plat-action" style={{marginTop:4}}>
          <div className="plat-tt-disc"><div className="plat-tt-disc-inner"/></div>
        </div>
      </div>
      {/* Bottom: @user · desc · music */}
      <div className="plat-bottom">
        <div className="plat-bottom-left">
          <div className="plat-username">@your_username</div>
          <div className="plat-desc">Mô tả video của bạn #hashtag</div>
          <div className="plat-music-row"><IcoMusic/><span>Tên bài hát · Nghệ sĩ</span></div>
        </div>
        {/* TikTok logo bottom-right */}
        <svg viewBox="0 0 24 24" fill="white" width="20" height="20" style={{flexShrink:0,alignSelf:'flex-end',marginBottom:2,opacity:.9}}><path d="M19.59 6.69a4.83 4.83 0 01-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 01-2.88 2.5 2.89 2.89 0 01-2.89-2.89 2.89 2.89 0 012.89-2.89c.28 0 .54.04.79.1V9.01a6.29 6.29 0 00-.79-.05 6.34 6.34 0 00-6.34 6.34 6.34 6.34 0 006.34 6.34 6.34 6.34 0 006.33-6.34V8.82a8.18 8.18 0 004.79 1.53V6.88a4.85 4.85 0 01-1.02-.19z"/></svg>
      </div>
    </div>
  )

  if (platform === 'yt-shorts') return (
    <div className="plat-chrome plat-ytshorts">
      <Bar/>
      {/* Top nav: back · Shorts · search · more */}
      <div className="plat-yt-topnav">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        <span style={{fontWeight:700,fontSize:'0.62rem',flex:1,textAlign:'center'}}>Shorts</span>
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
        <IcoMore3Vert/>
      </div>
      {/* Right sidebar: Like · Dislike · Comment · Share · More */}
      <div className="plat-right" style={{bottom:85}}>
        <Act ico={<IcoThumbUp/>} lbl="2.8M"/>
        <Act ico={<IcoThumbDn/>} lbl="Dislike"/>
        <Act ico={<IcoComment/>} lbl="48K"/>
        <Act ico={<IcoShare/>}   lbl="Share"/>
        <Act ico={<IcoMore3Vert/>}/>
      </div>
      {/* Bottom: avatar · channel · subscribe · title */}
      <div className="plat-bottom">
        <div className="plat-bottom-left">
          <div style={{display:'flex',alignItems:'center',gap:5,marginBottom:3}}>
            <IcoAvatar s={20}/>
            <div className="plat-username">@channel_name</div>
            <div style={{fontSize:'0.48rem',border:'1px solid white',borderRadius:3,padding:'1px 4px',whiteSpace:'nowrap'}}>Đăng ký</div>
          </div>
          <div className="plat-desc">Tiêu đề Short · Xem thêm</div>
        </div>
      </div>
      {/* Seek bar at very bottom */}
      <div className="plat-yt-seek"><div className="plat-yt-seek-fill"/></div>
    </div>
  )

  if (platform === 'youtube') return (
    <div className="plat-chrome plat-youtube">
      <Bar/>
      {/* YouTube header bar */}
      <div className="plat-yt-header">
        <svg viewBox="0 0 24 24" fill="#FF0000" width="17" height="17"><path d="M10 15l5.19-3L10 9v6m11.56-7.83c.13.47.22 1.1.28 1.9.07.8.1 1.49.1 2.09L22 12c0 2.19-.16 3.8-.44 4.83-.25.9-.83 1.48-1.73 1.73-.47.13-1.33.22-2.65.28-1.3.07-2.49.1-3.59.1L12 19c-4.19 0-6.8-.16-7.83-.44-.9-.25-1.48-.83-1.73-1.73-.13-.47-.22-1.1-.28-1.9-.07-.8-.1-1.49-.1-2.09L2 12c0-2.19.16-3.8.44-4.83.25-.9.83-1.48 1.73-1.73.47-.13 1.33-.22 2.65-.28 1.3-.07 2.49-.1 3.59-.1L12 5c4.19 0 6.8.16 7.83.44.9.25 1.48.83 1.73 1.73z"/></svg>
        <span style={{color:'white',fontSize:'0.6rem',fontWeight:700}}>YouTube</span>
        <div style={{marginLeft:'auto',display:'flex',gap:8,alignItems:'center'}}>
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="white" strokeWidth="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 01-3.46 0"/></svg>
          <IcoAvatar s={16}/>
        </div>
      </div>
      {/* Progress + controls at bottom */}
      <div style={{position:'absolute',bottom:0,left:0,right:0}}>
        <div style={{height:'2px',background:'rgba(255,255,255,0.25)',position:'relative',margin:'0 8px'}}>
          <div style={{position:'absolute',left:0,top:0,bottom:0,width:'38%',background:'#FF0000'}}/>
          <div style={{position:'absolute',left:'38%',top:'-3px',width:'7px',height:'7px',background:'#FF0000',borderRadius:'50%',marginLeft:'-3px'}}/>
        </div>
        <div style={{display:'flex',alignItems:'center',padding:'3px 8px 5px',gap:7,fontSize:'0.5rem',color:'rgba(255,255,255,0.85)'}}>
          <svg viewBox="0 0 24 24" width="12" height="12" fill="white"><polygon points="5,3 19,12 5,21"/></svg>
          <span>1:06 / 2:32</span>
          <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="white" strokeWidth="2"><polygon points="11,5 6,9 2,9 2,15 6,15 11,19"/><path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/></svg>
          <div style={{flex:1}}/>
          <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="white" strokeWidth="2"><path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/></svg>
        </div>
      </div>
    </div>
  )

  if (platform === 'facebook') return (
    <div className="plat-chrome plat-facebook">
      <Bar/>
      {/* Right sidebar: Like · Comment · Share · More */}
      <div className="plat-right">
        <Act ico={<IcoHeart/>}      lbl="2.8M"/>
        <Act ico={<IcoComment/>}    lbl="48K"/>
        <Act ico={<IcoShare/>}      lbl="Share"/>
        <Act ico={<IcoMore3Horiz/>}/>
      </div>
      {/* Bottom: avatar · name · follow · desc · music */}
      <div className="plat-bottom">
        <div className="plat-bottom-left">
          <div style={{display:'flex',alignItems:'center',gap:5,marginBottom:3}}>
            <IcoAvatar s={22}/>
            <div className="plat-username">Your Name</div>
            <div style={{fontSize:'0.48rem',border:'1px solid white',borderRadius:3,padding:'1px 5px',whiteSpace:'nowrap'}}>+ Theo dõi</div>
          </div>
          <div className="plat-desc">Mô tả video của bạn #hashtag</div>
          <div className="plat-music-row"><IcoMusic/><span>Tên bài hát · Nghệ sĩ</span></div>
        </div>
      </div>
    </div>
  )

  return null
})
