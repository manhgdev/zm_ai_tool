import {
  Suspense,
  lazy,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import { Toaster, toast } from 'sonner'
import Header, { type AppMode } from '@/shared/components/Header'
import { IconVideo } from '@/shared/components/Icons'
import ProgressPopup from '@/shared/components/ProgressPopup'
import ProjectSidebar from '@/features/project/ProjectSidebar'
import PipelineStepper from '@/features/project/PipelineStepper'
import SegmentList from '@/features/project/SegmentList'
import ConfigModal from '@/features/configuration/ConfigModal'

// Hai màn nặng nhất (editor + TTS studio) tải theo nhu cầu — bundle chính
// không còn vượt cảnh báo 600KB của vite.
const LivePreviewEditor = lazy(() => import('@/features/editor/LivePreviewEditor'))
const TtsPage = lazy(() => import('@/pages/TtsPage'))
import DownloadPage from '@/pages/DownloadPage'
import FilmPage from '@/pages/FilmPage'
import BatchPage from '@/pages/BatchPage'
import FlowPage from '@/pages/FlowPage'
import RendersPage from '@/pages/RendersPage'
import VideoCleanerPage from '@/pages/VideoCleanerPage'
import SrtImagePage from '@/pages/SrtImagePage'
import SrtExportPage from '@/pages/SrtExportPage'
import DrawingPage from '@/pages/DrawingPage'
import ChatPage from '@/pages/ChatPage'
import AutomationPage from '@/pages/AutomationPage'
import LicensePage from '@/features/license/LicensePage'
import { licenseApi, readCachedStatus, type LicenseStatus } from '@/features/license/license.api'
import { ExportSuccessModal } from '@/features/editor/ExportSuccessModal'
import { api } from '@/features/project/project.api'
import { expandSegmentsForList } from '@/features/project/expandCompound'
import type { HardwareInfo, JobStatus, ProjectSettings, Step, TextOverlay } from '@/features/project/project.types'
import { appModeFromPath, appModePath, loadAppMode, persistAppMode } from '@/app/appMode'
import { LocaleContext, LocaleTextSync, loadLocale, localize, localizePipelineMessage, persistLocale, type AppLocale } from '@/app/i18n'
import {
  applyDefaultVoice,
  asSegmentList,
  useSegmentEditing,
} from '@/features/project/useSegmentEditing'
import { useProjectMedia } from '@/features/project/useProjectMedia'
import { useDubControl } from '@/features/project/useDubControl'
import { useExportFlow } from '@/features/project/useExportFlow'
import { useJobPolling } from '@/features/project/useJobPolling'
import {
  SIDEBAR_MAX,
  SIDEBAR_W_LS,
  THEME_LS,
  SIDEBAR_MIN,
  applyEngineProfile,
  idleStatus,
  loadSetupGate,
  loadSettings,
  loadSidebarWidth,
  loadTheme,
  persistSession,
  persistSettings,
  persistSetupGate,
  snapshotEngineProfile,
  useSessionRestore,
} from '@/app/useProjectSession'
import './App.css'

function fmtDuration(sec: number) {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

const EMPTY_LICENSE: LicenseStatus = {
  valid: false,
  configured: false,
  keyMasked: '',
  remainingDay: 0,
  expiresAt: null,
  activationLimit: 0,
  message: 'Đang kiểm tra bản quyền…',
}

export default function App() {
  const [locale, setLocale] = useState<AppLocale>(loadLocale)
  const localeChangedRef = useRef(false)
  const [dark, setDark] = useState(loadTheme)
  const [appMode, setAppMode] = useState<AppMode>(loadAppMode)
  const [srtImageInitialMediaFolder, setSrtImageInitialMediaFolder] = useState('')
  const tabPrev = useRef<AppMode[]>([])
  const [hw, setHw] = useState<HardwareInfo>({ label: 'CPU', accel: 'cpu' })
  const [voices, setVoices] = useState<{ id: string; name: string; previewUrl?: string }[]>([
    { id: 'el:pNInz6obpgDQGcFmaJgB', name: 'ElevenLabs · Adam' },
    { id: 'system', name: 'Giọng hệ thống (theo ngôn ngữ đích)' },
  ])
  const [settings, setSettings] = useState(loadSettings)
  const [configOpen, setConfigOpen] = useState(false)
  const [configSection, setConfigSection] = useState<'setup' | 'cloud' | 'tts' | 'license'>(() =>
    loadSetupGate() ? 'cloud' : 'setup',
  )
  const [setupGatePassed, setSetupGatePassed] = useState(loadSetupGate)
  /** Backend checks done once — không chặn UI sau khi đã qua cổng thiết lập. */
  const [setupChecked, setSetupChecked] = useState(false)
  const [setupMissingRequired, setSetupMissingRequired] = useState(false)
  // ponytail: init từ sessionStorage cache → không flash LicensePage mỗi lần mở app
  const [licenseStatus, setLicenseStatus] = useState<LicenseStatus | null>(readCachedStatus)
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth)
  const sidebarWidthRef = useRef(sidebarWidth)
  const sidebarDrag = useRef<{ startX: number; startW: number } | null>(null)
  const [projectId, setProjectId] = useState<string | null>(null)
  const [overlays, setOverlays] = useState<TextOverlay[]>([])
  const [status, setStatus] = useState<JobStatus>(idleStatus)
  /** App desktop: file đã trên máy — chỉ Xem / Mở thư mục */
  const [isDesktopApp, setIsDesktopApp] = useState(false)
  const [progressMinimized, setProgressMinimized] = useState(false)
  const [ttsSideOpen, setTtsSideOpen] = useState(false)
  const [clearingCache, setClearingCache] = useState(false)
  /** Guards project/video switches from late restore, upload, and poll responses. */
  const projectSwitchRef = useRef(0)
  const activeProjectRef = useRef<string | null>(null)

  useEffect(() => {
    persistLocale(locale)
    document.documentElement.lang = locale
  }, [locale])

  useEffect(() => {
    api.getLocalePreference()
      .then(({ locale: savedLocale }) => {
        if (savedLocale && !localeChangedRef.current) setLocale(savedLocale)
      })
      .catch(() => {
        /* Browser/dev mode continues with localStorage or browser language. */
      })
  }, [])

  const changeLocale = (nextLocale: AppLocale) => {
    localeChangedRef.current = true
    setLocale(nextLocale)
    void api.saveLocalePreference(nextLocale).catch(() => {
      /* localStorage remains a fallback when the API is unavailable. */
    })
  }

  const {
    segments,
    setSegments,
    flushSegmentSave,
    onSegmentChange,
    onSegmentsReplace,
  } = useSegmentEditing({ projectId, defaultVoice: settings.defaultVoice })
  const {
    videoUrl,
    setVideoUrl,
    duration,
    setDuration,
    workClipSec,
    setWorkClipSec,
    workClipSecRef,
    bakedPreferVideo,
    setBakedPreferVideo,
    bakedPreferVideoRef,
    bakedSpeed,
    setBakedSpeed,
    hasBakedSpeed,
    setHasBakedSpeed,
    freshVideoUrl,
    onPreviewRebaked,
    onRestoreBakedSpeed,
  } = useProjectMedia({
    projectId,
    defaultVoice: settings.defaultVoice,
    setSegments,
    setOverlays,
  })
  const { busyAt, releaseDubLock, onDub, onCancel } = useDubControl({
    projectId,
    status,
    setStatus,
    settings,
    setSegments,
    setProgressMinimized,
    flushSegmentSave,
  })
  const {
    exportUrl,
    setExportUrl,
    exportPath,
    setExportPath,
    viewExportSrc,
    setViewExportSrc,
    exportSuccessOpen,
    setExportSuccessOpen,
    lastExportedTypes,
    pendingExportUrl,
    applyExportDone,
    onExport,
    onRevealOutput,
    onViewExport,
  } = useExportFlow({
    projectId,
    status,
    setStatus,
    settings,
    segments,
    setSegments,
    busyAt,
    setProgressMinimized,
  })
  useJobPolling({
    projectId,
    running: status.running,
    activeProjectRef,
    setStatus,
    releaseDubLock,
    defaultVoice: settings.defaultVoice,
    setSegments,
    workClipSecRef,
    setWorkClipSec,
    setDuration,
    setVideoUrl,
    freshVideoUrl,
    bakedPreferVideoRef,
    setBakedPreferVideo,
    setBakedSpeed,
    setHasBakedSpeed,
    pendingExportUrl,
    applyExportDone,
  })
  useSessionRestore({
    projectSwitchRef,
    activeProjectRef,
    setProjectId,
    settings,
    setSettings,
    setStatus,
    setSegments,
    workClipSecRef,
    setWorkClipSec,
    setDuration,
    setVideoUrl,
    freshVideoUrl,
    bakedPreferVideoRef,
    setBakedPreferVideo,
    setBakedSpeed,
    setHasBakedSpeed,
    setExportUrl,
    setExportPath,
    releaseDubLock,
    busyAt,
  })

  useEffect(() => {
    if (status.running) setProgressMinimized(false)
  }, [status.running])

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    try { localStorage.setItem(THEME_LS, dark ? 'dark' : 'light') } catch { /* ignore */ }
  }, [dark])

  // F5: giữ tab top-level (TTS / Film / …); editor preview không persist → không ép mode
  useEffect(() => {
    persistAppMode(appMode)
    // Các thao tác nội bộ (ví dụ “Dùng trong Clone” từ Download) cũng đổi URL.
    // replaceState tránh tạo một lịch sử giả; click tab vẫn dùng pushState ở dưới.
    const destination = appModePath(appMode) + (appMode === 'flow' ? window.location.search : '')
    if (window.location.pathname !== destination) {
      window.history.replaceState({ appMode }, '', destination)
    }
  }, [appMode])

  // Browser history cũng là một phần của navigation: link trực tiếp, Back/Forward
  // và các tab ở Header luôn cùng một state thay vì chỉ dựa vào localStorage.
  useEffect(() => {
    const onPopState = () => {
      const modeFromUrl = appModeFromPath(window.location.pathname)
      if (modeFromUrl) setAppMode(modeFromUrl)
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const navigateToMode = (mode: AppMode, fromBack = false) => {
    if (mode !== appMode && !fromBack) tabPrev.current.push(appMode)
    const destination = appModePath(mode) + (mode === 'flow' ? window.location.search : '')
    if (fromBack) {
      window.history.replaceState({ appMode: mode }, '', destination)
    } else if (window.location.pathname !== destination) {
      window.history.pushState({ appMode: mode }, '', destination)
    }
    setAppMode(mode)
    setTtsSideOpen(false)
  }

  const goBackTab = () => {
    const prev = tabPrev.current.pop()
    if (prev && prev !== appMode) {
      navigateToMode(prev, true)
      return
    }
    if (appMode !== 'clone') navigateToMode('clone', true)
  }

  useEffect(() => {
    api.getConfig()
      .then((c) => setIsDesktopApp(Boolean(c.desktop)))
      .catch(() => setIsDesktopApp(false))
  }, [])

  useEffect(() => {
    if (!setupGatePassed) return
    let cancelled = false
    const refresh = () => licenseApi.status()
      .then((next) => { if (!cancelled) setLicenseStatus(next) })
      .catch((error) => {
        if (!cancelled) setLicenseStatus({
          ...EMPTY_LICENSE,
          message: error instanceof Error ? error.message : 'Không thể kiểm tra key',
        })
      })
    void refresh()
    const timer = window.setInterval(refresh, 10 * 60 * 1000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [setupGatePassed])

  useEffect(() => {
    api.hardware().then(setHw).catch(() => setHw({ label: 'Local', accel: 'cpu' }))
  }, [])

  function passSetupGate() {
    persistSetupGate()
    setSetupGatePassed(true)
    setSetupMissingRequired(false)
    setConfigSection('cloud')
    setConfigOpen(false)
    void api.passSetupGate().catch(() => {
      /* file gate — localStorage vẫn giữ */
    })
    void api.voices('all').then(setVoices).catch(() => {})
  }

  // Lần đầu: Thiết lập. Lần sau: đọc gate từ disk (ổn định hơn localStorage theo port).
  useEffect(() => {
    let cancelled = false

    async function bootstrapSetup() {
      let gatePassed = loadSetupGate()

      for (let attempt = 0; attempt < 60; attempt++) {
        if (cancelled) return
        try {
          await api.health()
          break
        } catch {
          await new Promise((r) => window.setTimeout(r, 200))
        }
      }
      if (cancelled) return

      try {
        const g = await api.getSetupGate()
        if (cancelled) return
        if (g.passed) {
          gatePassed = true
          persistSetupGate()
          setSetupGatePassed(true)
        } else if (gatePassed) {
          // localStorage cũ → đồng bộ lên disk lần này
          await api.passSetupGate().catch(() => {})
          setSetupGatePassed(true)
        }
      } catch {
        /* giữ localStorage */
      }

      if (!gatePassed) {
        setConfigSection('setup')
        setConfigOpen(true)
      }

      try {
        const c = await api.systemChecks(false, false)
        if (cancelled) return
        setSetupChecked(true)
        setSetupMissingRequired(!c.ok)
        // Chỉ ép mở Thiết lập khi chưa qua cổng — lần sau vào thẳng app.
        if (!c.ok && !gatePassed && !loadSetupGate()) {
          setConfigSection('setup')
          setConfigOpen(true)
        }
      } catch {
        if (cancelled) return
        setSetupChecked(true)
        if (!gatePassed) setSetupMissingRequired(true)
      }
    }

    void bootstrapSetup()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const ac = new AbortController()
    const t = window.setTimeout(() => ac.abort(), 8000)
    fetch(`/api/voices?lang=all`, {
      signal: ac.signal,
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(await r.text())
        return r.json() as Promise<{ id: string; name: string; previewUrl?: string }[]>
      })
      .then((vs) => {
        if (!Array.isArray(vs) || !vs.length) return
        setVoices(vs)
        setSettings((s) => {
          const next = vs.some((v) => v.id === s.defaultVoice) ? s : { ...s, defaultVoice: vs[0].id }
          if (next !== s) persistSettings(next)
          return next
        })
      })
      .catch(() => {
        /* giữ preset đã seed — tránh kẹt "Đang tải giọng" */
      })
      .finally(() => window.clearTimeout(t))
    return () => {
      ac.abort()
      window.clearTimeout(t)
    }
  }, [settings.targetLang])

  async function onUpload(file: File) {
    const switchVersion = ++projectSwitchRef.current
    activeProjectRef.current = null
    persistSession(null)
    setProjectId(null)
    setVideoUrl(null)
    setDuration(0)
    setExportUrl(null)
    setExportPath(null)
    setSegments([])
    setOverlays([])
    setWorkClipSec(0)
    workClipSecRef.current = 0
    setBakedPreferVideo(false)
    bakedPreferVideoRef.current = false
    setBakedSpeed(1)
    setViewExportSrc(null)
    if (appMode === 'live-preview') setAppMode('clone')
    setStatus({ step: 'video', progress: 10, message: 'Đang tải video…', running: true })
    try {
      const res = await api.upload(file)
      if (projectSwitchRef.current !== switchVersion) return
      activeProjectRef.current = res.projectId
      setProjectId(res.projectId)
      persistSession(res.projectId)
      // bust browser + <video> cache khi đổi / mở lại project
      setVideoUrl(freshVideoUrl(res.videoUrl))
      setDuration(res.duration)
      if (res.settings && typeof res.settings === 'object') {
        setSettings((s) => {
          const next = { ...s, ...res.settings }
          persistSettings(next)
          return next
        })
      }
      if (res.segments?.length) {
        const voice =
          (res.settings && typeof res.settings === 'object' && res.settings.defaultVoice) ||
          settings.defaultVoice
        setSegments(applyDefaultVoice(asSegmentList(res.segments), voice))
        setStatus({
          step: 'translate',
          progress: 100,
          message: res.cached
            ? `Đã mở lại từ cache — ${res.segments.length} đoạn`
            : 'Video sẵn sàng',
          running: false,
        })
      } else {
        setStatus({
          step: 'video',
          progress: 100,
          message: res.cached ? 'Video đã có sẵn (cache)' : 'Video sẵn sàng',
          running: false,
        })
      }
    } catch (e) {
      if (projectSwitchRef.current !== switchVersion) return
      setStatus({
        step: 'video',
        progress: 0,
        message: e instanceof Error ? e.message : 'Tải video thất bại — kiểm tra server API',
        running: false,
        error: 'upload',
      })
    }
  }

  async function onClearCache(parts: string[]) {
    if (!projectId || clearingCache || !parts.length) return
    setClearingCache(true)
    toast.dismiss('cache')
    try {
      if (status.running) {
        try {
          await api.cancel(projectId)
        } catch {
          /* best-effort */
        }
      }
      const res = await api.clearProjectCache(projectId, parts)
      // Reset UI theo phần đã xóa — giữ video nguồn
      if (res.clearedSegments) {
        setSegments([])
        setOverlays([])
        if (appMode === 'live-preview') setAppMode('clone')
      } else {
        const dropCover = Boolean(res.clearedCovers)
        const dropTts = Boolean(res.clearedTts)
        if (dropCover || dropTts) {
          setSegments((segs) =>
            (Array.isArray(segs) ? segs : []).map((s) => ({
              ...s,
              ...(dropCover
                ? {
                    bbox: undefined,
                    captionLayout: undefined,
                    bboxInherited: undefined,
                    bboxDetected: undefined,
                  }
                : {}),
              ...(dropTts
                ? {
                    audioFile: undefined,
                    audioUrl: undefined,
                    audioDuration: undefined,
                    videoSpeed: undefined,
                  }
                : {}),
            })),
          )
        }
      }
      if (res.clearedCovers) {
        // Auto blur is OCR-derived cache. The API already clears its project
        // copy; clear this in-memory/local copy too so the current preview
        // cannot keep painting an old frozen region after cache deletion.
        setSettings((current) => {
          const { blurBandAutoRegion: _region, blurBandAutoRegionVersion: _version, ...next } = current
          persistSettings(next)
          return next
        })
      }
      if (res.clearedSegments || parts.includes('render') || parts.includes('preview')) {
        setExportUrl(null)
        setExportPath(null)
        setViewExportSrc(null)
      }
      if (parts.includes('preview') || res.clearedSegments) {
        setWorkClipSec(0)
        workClipSecRef.current = 0
        setBakedPreferVideo(false)
        bakedPreferVideoRef.current = false
        setBakedSpeed(1)
        setHasBakedSpeed(false)
      }
      if (res.clearedFrontend || parts.includes('frontend')) {
        try {
          localStorage.removeItem(`zm_ai_tool.videoClips.${projectId}`)
          localStorage.removeItem(`zm_ai_tool.bgClips.${projectId}`)
        } catch {
          /* ignore */
        }
      }
      setVideoUrl(freshVideoUrl(`/api/projects/${projectId}/video`))
      setStatus({
        step: res.clearedSegments ? 'video' : status.step,
        progress: 100,
        message: res.message || 'Đã xóa cache',
        running: false,
        error: undefined,
      })
      const message = res.message?.startsWith('Đã xóa toàn bộ')
        ? 'Đã xóa toàn bộ cache.'
        : res.message || 'Đã xóa cache đã chọn.'
      if (res.ok) toast.success(message, { id: 'cache' })
      else toast.warning('Một số cache chưa được xóa.', { id: 'cache' })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Một số cache chưa được xóa.', { id: 'cache' })
    } finally {
      setClearingCache(false)
    }
  }

  async function onTranslateAll(runWindowSec = 0, settingsOverride?: ProjectSettings) {
    if (!projectId || status.running) return
    setExportUrl(null)
    // 0 = Dịch cả video (full); >0 = ▶ Preview Ns — tách khỏi ô settings.previewSec
    const wc = Math.max(0, runWindowSec)
    workClipSecRef.current = wc
    setWorkClipSec(wc)
    // Full: xóa clip timeline local (đang kẹt độ dài preview cũ)
    if (wc <= 0 && typeof localStorage !== 'undefined') {
      try {
        localStorage.removeItem(`zm_ai_tool.videoClips.${projectId}`)
        localStorage.removeItem(`zm_ai_tool.bgClips.${projectId}`)
      } catch { /* ignore */ }
    }
    // Bust video URL ngay — tránh stream preview_Ns / full lẫn nhau
    setVideoUrl(freshVideoUrl(`/api/projects/${projectId}/video`))
    // Xóa segments UI ngay — tránh hiện bản full trong lúc chạy preview (và ngược lại)
    setSegments([])
    busyAt.current = Date.now()
    setStatus({
      step: 'asr',
      progress: 0,
      message: wc > 0 ? `Preview ${wc}s…` : 'Dịch cả video (full)…',
      running: true,
      error: undefined,
    })
    // previewSec = ô UI (giữ nguyên); runPreviewSec = cửa sổ lần này
    await api.run(projectId, {
      ...(settingsOverride ?? settings),
      runPreviewSec: wc,
    })
    setStatus((s) => ({ ...s, running: true }))
  }

  function onSettings(next: ProjectSettings) {
    const prev = settings
    // đang chạy job — đừng đổi engine (tránh xóa đoạn + nhảy về Video)
    if (status.running && next.engine !== prev.engine) return

    let out = next
    if (next.engine !== prev.engine) {
      // Lưu profile engine cũ → nạp profile engine mới (mặc định riêng nếu chưa chỉnh)
      const snapped = snapshotEngineProfile({ ...prev, engine: prev.engine })
      out = applyEngineProfile(
        { ...snapped, ...next, engine: next.engine, engineProfiles: snapped.engineProfiles },
        next.engine === 'paddleocr' || next.engine === 'subtitle' || next.engine === 'capcut'
          ? next.engine
          : 'whisper',
      )
    } else {
      out = snapshotEngineProfile(next)
    }

    setSettings(out)
    persistSettings(out)
    if (projectId) {
      void api.saveSettings(projectId, out).catch(() => {
        /* ponytail: ignore transient save */
      })
    }
    // Đổi engine → bỏ đoạn cũ; matchDuration / lọc âm theo profile riêng
    if (out.engine !== prev.engine) {
      setSegments([])
      setExportUrl(null)
      setExportPath(null)
      setStatus({
        step: 'video',
        progress: 0,
        message:
          out.engine === 'paddleocr'
            ? 'Nhận dạng chữ trên màn — chạy Dịch toàn bộ'
            : out.engine === 'subtitle'
              ? 'Dùng file phụ đề SRT — chạy Dịch toàn bộ'
              : out.engine === 'capcut'
                ? localize(locale, 'CapCut nhận dạng cloud — chạy Dịch toàn bộ', 'CapCut cloud recognition — run Full Translation')
              : 'Nhận dạng giọng nói — chạy Dịch toàn bộ rồi Lồng tiếng',
        running: false,
      })
      return
    }
    if (out.defaultVoice === prev.defaultVoice) return
    // giọng mặc định sidebar áp dụng cả list (đổi lại = đổi hết đoạn)
    setSegments((segs) => (Array.isArray(segs) ? segs : []).map((seg) => ({ ...seg, voice: out.defaultVoice })))
  }

  useEffect(() => {
    if (!projectId) {
      setOverlays([])
      return
    }
    let cancelled = false
    void api.overlays(projectId)
      .then((items) => {
        if (!cancelled && activeProjectRef.current === projectId) setOverlays(items)
      })
      .catch(() => {
        if (!cancelled && activeProjectRef.current === projectId) setOverlays([])
      })
    return () => {
      cancelled = true
    }
  }, [projectId])

  async function onOverlayChange(overlay: TextOverlay, isNew = false) {
    if (!projectId) return
    setOverlays((current) =>
      isNew ? [...current, overlay] : current.map((item) => (item.id === overlay.id ? overlay : item)),
    )
    try {
      const saved = isNew
        ? await api.createOverlay(projectId, overlay)
        : await api.updateOverlay(projectId, overlay)
      setOverlays((current) => current.map((item) => (item.id === saved.id ? saved : item)))
    } catch {
      void api.overlays(projectId).then(setOverlays)
    }
  }

  async function onOverlayDelete(overlayId: string) {
    if (!projectId) return
    setOverlays((current) => current.filter((item) => item.id !== overlayId))
    try {
      await api.deleteOverlay(projectId, overlayId)
    } catch {
      void api.overlays(projectId).then(setOverlays)
    }
  }

  async function onOverlaysReplace(next: TextOverlay[]) {
    setOverlays(next)
    if (!projectId) return
    try {
      const saved = await api.replaceOverlays(projectId, next)
      if (Array.isArray(saved)) setOverlays(saved)
    } catch {
      void api.overlays(projectId).then(setOverlays)
    }
  }

  const step: Step = status.step

  useEffect(() => {
    sidebarWidthRef.current = sidebarWidth
  }, [sidebarWidth])

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const drag = sidebarDrag.current
      if (!drag) return
      const next = Math.max(
        SIDEBAR_MIN,
        Math.min(SIDEBAR_MAX, drag.startW + (e.clientX - drag.startX)),
      )
      sidebarWidthRef.current = next
      setSidebarWidth(next)
    }
    const onUp = () => {
      if (!sidebarDrag.current) return
      sidebarDrag.current = null
      document.body.classList.remove('resizing-sidebar')
      try {
        localStorage.setItem(SIDEBAR_W_LS, String(sidebarWidthRef.current))
      } catch {
        /* ignore */
      }
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [])

  const onSidebarResizeStart = (e: ReactMouseEvent) => {
    e.preventDefault()
    sidebarDrag.current = { startX: e.clientX, startW: sidebarWidthRef.current }
    document.body.classList.add('resizing-sidebar')
  }

  const editorOpen = appMode === 'live-preview' && !!videoUrl && !!projectId

  async function editRenderedProject(id: string) {
    const switchVersion = ++projectSwitchRef.current
    const [st, segs] = await Promise.all([api.status(id), api.segments(id)])
    if (projectSwitchRef.current !== switchVersion) return
    activeProjectRef.current = id
    persistSession(id)
    setProjectId(id)
    setVideoUrl(freshVideoUrl(`/api/projects/${id}/video`))
    const wc = typeof st.workClipSec === 'number' ? Math.max(0, st.workClipSec) : 0
    workClipSecRef.current = wc
    setWorkClipSec(wc)
    const dur = Number(st.duration || 0)
    setDuration(wc > 0 ? wc : dur)
    const bs = typeof st.bakedSpeed === 'number' && st.bakedSpeed > 0 ? st.bakedSpeed : 1
    const speedOff1 = Math.abs(bs - 1) > 0.02
    const baked = Boolean(st.bakedPreferVideo) && speedOff1
    bakedPreferVideoRef.current = baked
    setBakedPreferVideo(baked)
    setBakedSpeed(bs)
    setHasBakedSpeed(Boolean((st as { hasBakedSpeed?: boolean }).hasBakedSpeed) || speedOff1)
    const extra = st as JobStatus & { settings?: Partial<ProjectSettings> }
    const mergedVoice = extra.settings?.defaultVoice || settings.defaultVoice
    setSegments(applyDefaultVoice(asSegmentList(segs), mergedVoice))
    if (extra.settings && typeof extra.settings === 'object') {
      setSettings((current) => {
        const next = { ...current, ...extra.settings }
        persistSettings(next)
        return next
      })
    }
    setStatus({
      step: st.step || 'video',
      progress: st.progress || 0,
      message: st.message || 'Đã mở lại project',
      running: Boolean(st.running),
      error: st.error,
      outputRel: st.outputRel,
      logoDetection: st.logoDetection,
    })
    if (st.running) busyAt.current = Date.now()
    setExportUrl(!st.running && st.outputRel && (st.progress || 0) >= 100 ? `/api/projects/${id}/output` : null)
    setExportPath(!st.running && st.outputRel && (st.progress || 0) >= 100 ? st.outputRel : null)
    setViewExportSrc(null)
    navigateToMode('live-preview')
  }

  // Chỉ sau Bắt đầu / đã lưu cổng — tránh Header+Modal+workspace cùng chiếm CSS grid → trắng.
  const firstRunBlocked = !setupGatePassed
  const appUsable = setupGatePassed
  // ponytail: null = đang loading → không block; chỉ block khi đã load xong và invalid
  const licenseBlocked = appUsable && licenseStatus != null && !licenseStatus.valid
  const configModalOpen = configOpen || firstRunBlocked

  return (
    <LocaleContext.Provider value={{ locale, setLocale: changeLocale }}>
    <LocaleTextSync />
    <div className={`app${licenseBlocked ? ' app-license-gate' : ''}`}>
      {!appUsable ? (
        <p className="cfg-boot-msg">
          {setupChecked
            ? setupMissingRequired
              ? 'Cài đủ thành phần bắt buộc rồi bấm Bắt đầu'
              : 'Sẵn sàng — bấm Bắt đầu'
            : 'Đang kết nối backend…'}
        </p>
      ) : null}
      {appUsable && !licenseBlocked && (
      <Header
        hardware={hw}
        dark={dark}
        mode={appMode}
        licenseStatus={licenseStatus || undefined}
        menuOpen={ttsSideOpen}
        onMenuClick={
          appMode === 'tts' ? () => setTtsSideOpen((o) => !o) : undefined
        }
        onModeChange={navigateToMode}
        onToggleTheme={() => setDark(d => !d)}
        locale={locale}
        onLocaleChange={changeLocale}
        onOpenLicense={() => {
          setConfigSection('license')
          setConfigOpen(true)
        }}
        onOpenConfig={() => {
          if (setupGatePassed) setConfigSection('cloud')
          setConfigOpen(true)
        }}
      />
      )}
      <ConfigModal
        open={configModalOpen}
        initialSection={configSection}
        forceSetup={firstRunBlocked}
        onSetupReady={passSetupGate}
        onSaved={() => {
          void api.voices('all').then(setVoices).catch(() => {})
        }}
        licenseStatus={licenseStatus || undefined}
        onLicenseStatusChange={setLicenseStatus}
        onClose={() => {
          if (firstRunBlocked) return
          setConfigOpen(false)
          // đóng config cũng refresh voices (user có thể vừa lưu key)
          void api.voices('all').then(setVoices).catch(() => {})
        }}
      />
      {licenseBlocked && (
        <LicensePage
          status={licenseStatus || EMPTY_LICENSE}
          gate
          onStatusChange={setLicenseStatus}
        />
      )}
      <Suspense fallback={<div className="page-loading" role="status">Đang tải…</div>}>
      {appUsable && !licenseBlocked ? (
      appMode === 'tts' ? (
        <TtsPage
          voices={voices}
          sideOpen={ttsSideOpen}
          onBack={goBackTab}
          onSideOpenChange={setTtsSideOpen}
          onRefreshVoices={() => {
            void api.voices('all').then(setVoices).catch(() => {})
          }}
          isDesktopApp={isDesktopApp}
        />
      ) : appMode === 'license' && !licenseBlocked ? (
        <LicensePage status={licenseStatus || EMPTY_LICENSE} onStatusChange={setLicenseStatus} />
      ) : appMode === 'download' ? (
        <DownloadPage
          onBack={goBackTab}
          onUseInClone={(pid, meta) => {
            const switchVersion = ++projectSwitchRef.current
            activeProjectRef.current = pid
            persistSession(pid)
            setProjectId(pid)
            setVideoUrl(freshVideoUrl(meta.videoUrl || `/api/projects/${pid}/video`))
            setDuration(meta.duration || 0)
            setExportUrl(null)
            setExportPath(null)
            setSegments((meta.segments || []) as never[])
            if (meta.settings) {
              const next = { ...settings, ...meta.settings } as ProjectSettings
              setSettings(next)
              persistSettings(next)
            }
            setOverlays([])
            setWorkClipSec(0)
            workClipSecRef.current = 0
            setBakedPreferVideo(false)
            bakedPreferVideoRef.current = false
            setBakedSpeed(1)
            setHasBakedSpeed(false)
            setViewExportSrc(null)
            setAppMode('clone')
            setStatus({
              step: 'video',
              progress: 100,
              message: 'Video sẵn sàng (từ Download)',
              running: false,
            })
            if (projectSwitchRef.current === switchVersion) {
              setAppMode('clone')
            }
          }}
        />
      ) : appMode === 'film' ? (
        <FilmPage onBack={goBackTab} onOpenEditor={editRenderedProject} />
      ) : appMode === 'batch' ? (
        <BatchPage
          onBack={goBackTab}
          onOpenEditor={editRenderedProject}
          onOpenReviewProjects={() => navigateToMode('film')}
        />
      ) : appMode === 'flow' ? (
        <FlowPage onBack={goBackTab} onOpenSrtImage={(mediaFolder) => {
          setSrtImageInitialMediaFolder(mediaFolder)
          navigateToMode('srt-image')
        }} />
      ) : appMode === 'chat' ? (
        <ChatPage onOpenConfig={() => { setConfigSection('cloud'); setConfigOpen(true) }} />
      ) : appMode === 'automation' ? (
        <AutomationPage />
      ) : appMode === 'cleaner' ? (
        <VideoCleanerPage onBack={goBackTab} />
      ) : appMode === 'srt-image' ? (
        <SrtImagePage onBack={goBackTab} initialMediaFolder={srtImageInitialMediaFolder} />
      ) : appMode === 'srt-export' ? (
        <SrtExportPage onBack={goBackTab} />
      ) : appMode === 'drawing' ? (
        <DrawingPage onBack={goBackTab} />
      ) : appMode === 'renders' ? (
        <RendersPage onBack={goBackTab} onEdit={editRenderedProject} />
      ) : editorOpen ? (
        <LivePreviewEditor
          key={projectId}
          videoUrl={videoUrl}
          mediaDuration={duration}
          workClipSec={workClipSec}
          bakedPreferVideo={bakedPreferVideo}
          bakedSpeed={bakedSpeed}
          hasBakedSpeed={hasBakedSpeed}
          projectId={projectId}
          segments={segments}
          settings={settings}
          logoDetection={status.logoDetection}
          voices={voices}
          busy={status.running}
          jobStep={status.step}
          jobProgress={status.progress}
          jobMessage={localizePipelineMessage(locale, status.message || '')}
          onDub={onDub}
          onRunPipeline={onTranslateAll}
          onCancel={onCancel}
          onBack={goBackTab}
          onChange={onSegmentChange}
          onSegmentsReplace={onSegmentsReplace}
          onPreviewRebaked={onPreviewRebaked}
          onRestoreBakedSpeed={onRestoreBakedSpeed}
          onExport={onExport}
          onSettings={onSettings}
          overlays={overlays}
          onOverlayChange={onOverlayChange}
          onOverlayDelete={onOverlayDelete}
          onOverlaysReplace={onOverlaysReplace}
        />
      ) : appMode === 'live-preview' ? (
        <main className="live-preview-empty" aria-labelledby="live-preview-empty-title">
          <div className="live-preview-empty-card">
            <IconVideo size={32} />
            <h1 id="live-preview-empty-title">Chưa có video để xem trước</h1>
            <p>Mở hoặc tải video ở Clone Video rồi quay lại đây để chỉnh sửa theo timeline.</p>
            <button type="button" onClick={() => navigateToMode('clone')}>Đi tới Clone Video</button>
          </div>
        </main>
      ) : (
      <div
        className="workspace"
        style={{ gridTemplateColumns: `${sidebarWidth}px 6px 1fr` }}
      >
        <ProjectSidebar
          projectId={projectId}
          videoUrl={videoUrl}
          settings={settings}
          logoDetection={status.logoDetection}
          voices={voices}
          busy={status.running}
          onSettings={onSettings}
          onSubtitleApplied={(nextSegments, nextSettings) => {
            setSegments(nextSegments)
            setSettings(nextSettings)
            persistSettings(nextSettings)
            setStatus({ step: 'asr', progress: 100, message: `Đã nạp ${nextSegments.length} câu từ phụ đề SRT`, running: false })
          }}
          onUpload={onUpload}
          onTranslateAll={() => onTranslateAll(0)}
          onPreview={(previewSec) => {
            const sec = Math.max(5, Math.min(600, previewSec || settings.previewSec || 20))
            // Chỉ ▶ Preview mới ghi ô settings.previewSec — Dịch cả không đụng
            if (sec !== settings.previewSec) {
              const next = { ...settings, previewSec: sec }
              setSettings(next)
              void api.saveSettings(projectId!, next).catch(() => {})
            }
            void onTranslateAll(sec)
          }}
          onCancel={onCancel}
          onClearCache={onClearCache}
          clearingCache={clearingCache}
        />
        <div
          className="sidebar-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Kéo đổi độ rộng menu"
          onMouseDown={onSidebarResizeStart}
        />
        <main className="main">
          <PipelineStepper
            step={step}
            onDub={onDub}
            onPreviewEditor={() => {
              // Cài Sidebar = quy tắc đầu vào: ghi server rồi mới mở xem/sửa
              if (projectId) {
                void api.saveSettings(projectId, settings).catch(() => { /* ignore */ })
              }
              navigateToMode('live-preview')
            }}
            onExport={onExport}
            canDub={
              (segments.length > 0 || expandSegmentsForList(segments).length > 0)
              && !status.running
            }
            canPreviewEditor={Boolean(projectId && videoUrl)}
            canExport={(() => {
              if (status.running) return false
              // Alt+G: shell rỗng chữ — check children đã bung
              const flat = expandSegmentsForList(segments)
              if (!flat.length && !segments.length) return false
              if (settings.targetLang === 'none') return true
              return flat.some((s) => (s.translation || '').trim())
                || segments.some((s) => (s.translation || '').trim())
            })()}
          />
          <div className="main-head">
            <div>
              <h2>Kịch bản lồng tiếng</h2>
              <p className="status-line">
                {status.running
                  ? `${localizePipelineMessage(locale, status.message || '') || (locale === 'en' ? 'Processing…' : 'Đang xử lý…')} — ${Math.round(status.progress)}% ${locale === 'en' ? '(still running; progress may pause)' : '(vẫn chạy, % có thể đứng lâu)'}`
                  : localizePipelineMessage(locale, status.message || '')}
              </p>
            </div>
            <div className="meta">
              <span className="seg-count">
                {expandSegmentsForList(segments).length || segments.length} đoạn thoại
              </span>
              {duration > 0 && <span>{fmtDuration(duration)}</span>}
            </div>
          </div>
          {exportUrl && (
            <div className="export-banner">
              <div className="export-banner-text">
                <strong>
                  {status.step === 'export'
                    ? 'Video đã xuất xong'
                    : 'Có bản xuất trước — Xuất bản lại nếu vừa dịch mới'}
                </strong>
                <code>{exportPath || `backend/public/exports/${projectId}.mp4`}</code>
              </div>
              <div className="export-banner-actions">
                <button type="button" className="export-dl" onClick={onViewExport}>
                  Xem
                </button>
                {!isDesktopApp && (
                  <a
                    className="export-dl"
                    href={`/api/projects/${projectId}/output?download=1`}
                    download={`zm-ai-tool-${projectId}.mp4`}
                  >
                    Tải xuống
                  </a>
                )}
                <button type="button" className="export-reveal" onClick={onRevealOutput}>
                  Mở thư mục
                </button>
              </div>
            </div>
          )}
          <SegmentList
            segments={segments}
            voices={voices}
            defaultVoice={settings.defaultVoice}
            targetLang={settings.targetLang}
            sourceLang={settings.sourceLang}
            translator={settings.translator}
            videoUrl={videoUrl}
            projectId={projectId}
            logoDetection={status.logoDetection}
            coverLogo={settings.coverLogo}
            hiddenLogoTexts={settings.hiddenLogoTexts}
            onCoverLogoChange={(label, covered) => {
              const old = settings.hiddenLogoTexts || []
              const isHandle = label.startsWith('@')
              const withoutLabel = old.filter((text) =>
                isHandle ? !text.startsWith('@') : text !== label,
              )
              onSettings({
                ...settings,
                // Turning one logo on also enables logo masking globally;
                // individual exclusions retain the state of every other logo.
                coverLogo: covered ? true : settings.coverLogo,
                hiddenLogoTexts: covered ? withoutLabel : [...withoutLabel, label],
              })
            }}
            onChange={onSegmentChange}
            settings={settings}
            onSettings={onSettings}
            onSegmentsReplace={onSegmentsReplace}
          />
        </main>
      </div>
      )
      ) : null}
      </Suspense>
      <Toaster position="bottom-right" theme={dark ? 'dark' : 'light'} richColors closeButton />
      <ExportSuccessModal
        isOpen={exportSuccessOpen}
        onClose={() => setExportSuccessOpen(false)}
        onOpenVideo={() => {
          void fetch(`/api/projects/${projectId}/open-output`, { method: 'POST' })
        }}
        onRevealFolder={() => {
          void onRevealOutput()
        }}
        onOpenProject={() => { setExportSuccessOpen(false); navigateToMode('live-preview') }}
        videoSrc={viewExportSrc}
        message={status.message || ''}
        exportedTypes={lastExportedTypes}
        renderName={exportPath?.split('/').pop()?.replace(/\.[^.]+$/i, '') || undefined}
      />
      <ProgressPopup
        active={
          status.running
          || Boolean(
            status.error
            && status.error !== 'cancelled'
            && status.error !== 'stale_job',
          )
        }
        minimized={progressMinimized}
        running={status.running}
        title={
          status.step === 'dub'
            ? (
                status.error && status.error !== 'cancelled'
                  ? 'Lồng tiếng thất bại'
                  : status.running
                    ? 'Đang lồng tiếng'
                    : 'Lồng tiếng'
              )
            : status.step === 'export'
              ? (status.error ? 'Xuất video thất bại' : 'Xuất video')
              : status.step === 'translate' || status.step === 'asr'
                ? (status.error ? 'Dịch / nhận dạng thất bại' : 'Dịch / nhận dạng')
                : 'Đang xử lý'
        }
        message={
          // error code ngắn ("dub") → đừng thay message; đã xử lý trong ProgressPopup
          localizePipelineMessage(locale, status.message || '')
          || (
            status.error && status.error !== 'cancelled' && status.error.length > 24
              ? localizePipelineMessage(locale, status.error)
              : status.error === 'dub'
                ? 'Lồng tiếng thất bại — xem log backend hoặc thử lại'
                : undefined
          )
        }
        progress={status.progress}
        error={
          status.error === 'dub' && !(status.message || '').trim()
            ? 'Lồng tiếng thất bại'
            : localizePipelineMessage(locale, status.error || '')
        }
        onMinimize={() => {
          // Job đang chạy: chỉ thu nhỏ (chạy nền). Lỗi xong: dismiss hẳn.
          if (status.running) {
            setProgressMinimized(true)
            return
          }
          if (status.error && status.error !== 'cancelled') {
            setStatus((s) => ({
              ...s,
              error: undefined,
              message: '',
              progress: 0,
            }))
            setProgressMinimized(false)
            if (projectId) {
              void api.dismissStatus(projectId).catch(() => {
                /* offline — UI đã clear */
              })
            }
            return
          }
          setProgressMinimized(true)
        }}
        onRestore={() => setProgressMinimized(false)}
        onCancel={status.running ? onCancel : undefined}
      />
    </div>
    </LocaleContext.Provider>
  )
}
