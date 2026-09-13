import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { api } from '@/features/project/project.api'
import ProgressPopup from '@/shared/components/ProgressPopup'
import { IconHeadphones, IconHeart, IconMic, IconSpeaker } from '@/shared/components/Icons'
import { BackTitle } from '@/shared/components/BackTitle'
import { OutputFolderField } from '@/shared/components/OutputFolderField'
import { studioApi } from '@/features/studio/studio.api'
import { toast } from 'sonner'
import {
  type TtsEngine,
  type TtsOutputFormat,
  loadTtsSettings,
  persistTtsSettings,
} from './ttsSettings'
import VoiceMetadataModal, { VOICE_TAGS, type VoiceTagLabel } from './VoiceMetadataModal'
import DashPanel from './DashPanel'
import TtsHistoryPanel from './TtsHistoryPanel'
import VoiceClonePanel from './VoiceClonePanel'
import TtsInputPanel from './TtsInputPanel'
import {
  DEFAULT_DASH_LAYOUT,
  loadDashLayout,
  persistDashLayout,
  type DashId,
  type DashLayout,
} from './ttsDashboardLayout'
import type { EngineStatus, HistoryItem, Voice } from './tts.types'
import { voiceDisplayName, voiceEngineBucket, voiceMetadata } from './lib/voiceDisplay'
import { SRT_STYLE_OPTIONS, looksLikeSrt, srtPreviewLines } from './lib/srt'
import { downloadWavHref, triggerDownload as startDownload } from './lib/download'
import { HISTORY_MAX, fmtDur, previewSampleFor } from './lib/format'
import {
  IconClock,
  IconClone,
  IconDownload,
  IconFile,
  IconGear,
  IconHelp,
  IconKb,
  IconList,
  IconPause,
  IconPlay,
  IconUsers,
} from './TtsIcons'
import {
  SliderNumber, WAVE_BARS,
  FULL_DASHBOARD, COMING_SOON, SECTION_LABELS, sectionFromUrl,
  FAVORITE_LS_KEY, OUTPUT_DIR_LS_KEY, TTS_TEXT_LS_KEY,
  TTS_SRT_LS_KEY, TTS_INPUT_MODE_LS_KEY, TTS_ACTIVE_JOB_LS_KEY,
  TTS_URL_SECTIONS,
} from './lib/ttsStudioHelpers'

import './TtsStudio.css'

type Props = {
  voices: Voice[]
  onBack: () => void
  onRefreshVoices?: (lang?: string) => void
  isDesktopApp?: boolean
  /** Mobile drawer — controlled từ Header ☰ */
  sideOpen?: boolean
  onSideOpenChange?: (open: boolean) => void
}

export default function TtsStudio({
  voices,
  onBack,
  onRefreshVoices,
  isDesktopApp = false,
  sideOpen: sideOpenProp,
  onSideOpenChange,
}: Props) {
  const { locale } = useLocale()
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  const savedRef = useRef(loadTtsSettings())
  const saved = savedRef.current
  /** Preferred voice across async voice-list loads — avoids wiping restored selection. */
  const preferredVoiceRef = useRef(saved.voice)

  const [sideOpenLocal, setSideOpenLocal] = useState(false)
  const sideOpen = sideOpenProp ?? sideOpenLocal
  const setSideOpen = (open: boolean) => {
    onSideOpenChange?.(open)
    if (sideOpenProp === undefined) setSideOpenLocal(open)
  }

  const [section, setSection] = useState(sectionFromUrl)
  const [text, setText] = useState(() => {
    try {
      return localStorage.getItem(TTS_TEXT_LS_KEY) || ''
    } catch {
      return ''
    }
  })
  const [lang, setLang] = useState(saved.lang)
  const [engine, setEngine] = useState<TtsEngine>(saved.engine)
  const [voice, setVoice] = useState(saved.voice)
  const [style, setStyle] = useState(saved.style)
  const [speed, setSpeed] = useState(saved.speed)
  const [volume, setVolume] = useState(saved.volume)
  const [pitch, setPitch] = useState(saved.pitch)
  const [matchSrt, setMatchSrt] = useState(saved.matchSrt)
  const [keepTimeline, setKeepTimeline] = useState(saved.keepTimeline)
  const [normalize, setNormalize] = useState(saved.normalize)
  const [gapOn, setGapOn] = useState(saved.gapOn)
  const [gapMs, setGapMs] = useState(saved.gapMs)
  const [trimSilence, setTrimSilence] = useState(saved.trimSilence)
  const [autoSplit, setAutoSplit] = useState(saved.autoSplit)
  const [outputFormat, setOutputFormat] = useState<TtsOutputFormat>(saved.outputFormat)
  const [outputDir, setOutputDir] = useState(() => localStorage.getItem(OUTPUT_DIR_LS_KEY) || '')
  const outputDirRef = useRef(outputDir)
  useEffect(() => { outputDirRef.current = outputDir }, [outputDir])
  useEffect(() => {
    if (isDesktopApp || !/^(?:[A-Za-z]:[\\/]|[\\/])/.test(outputDir.trim())) return
    try {
      localStorage.removeItem(OUTPUT_DIR_LS_KEY)
    } catch {
      /* ignore */
    }
  }, [isDesktopApp, outputDir])
  const webOutputStem = (outputDir.trim().split(/[/\\]/).filter(Boolean).pop() || 'tts-output')
    .replace(/\.(?:wav|mp3|srt|zip)$/i, '')
    .replace(/[<>:"/\\|?*]+/g, '-')
    .trim() || 'tts-output'
  const [busy, setBusy] = useState(false)
  const [busyKind, setBusyKind] = useState<'synth' | 'clone' | null>(null)
  const [busyProgress, setBusyProgress] = useState(0)
  const [busyCustomMessage, setBusyCustomMessage] = useState('')
  const [progressMinimized, setProgressMinimized] = useState(false)
  const [error, setError] = useState('')

  const [initialActiveJob] = useState<{ id: string; duration: number; audioUrl: string; mp3Url?: string } | null>(() => {
    try {
      const raw = localStorage.getItem(TTS_ACTIVE_JOB_LS_KEY)
      if (!raw) return null
      const parsed = JSON.parse(raw) as { id?: unknown; duration?: unknown; audioUrl?: unknown; mp3Url?: unknown }
      if (parsed && typeof parsed.id === 'string' && typeof parsed.audioUrl === 'string') {
        return {
          id: String(parsed.id),
          duration: Number(parsed.duration || 0),
          audioUrl: String(parsed.audioUrl),
          mp3Url: parsed.mp3Url ? String(parsed.mp3Url) : undefined,
        }
      }
    } catch {
      /* ignore */
    }
    return null
  })

  const [audioUrl, setAudioUrl] = useState<string | null>(() => initialActiveJob?.audioUrl || null)
  const [mp3Url, setMp3Url] = useState<string | null>(() => initialActiveJob?.mp3Url || null)
  // ponytail: SRT/ZIP URLs are derived from jobId when downloading.
  const [jobId, setJobId] = useState<string | null>(() => initialActiveJob?.id || null)
  const activeJobIdRef = useRef<string | null>(null)
  const cancelledJobIdsRef = useRef(new Set<string>())
  const [duration, setDuration] = useState<number>(() => initialActiveJob?.duration || 0)
  const [playbackTime, setPlaybackTime] = useState(0)
  const [playbackDuration, setPlaybackDuration] = useState(0)
  const [playbackVolume, setPlaybackVolume] = useState(saved.playbackVolume)
  const [isPlaying, setIsPlaying] = useState(false)
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [historyPage, setHistoryPage] = useState(1)
  const [playingHistoryId, setPlayingHistoryId] = useState<string | null>(null)
  /** Menu chọn định dạng tải trong cột Hành động */
  const [downloadMenuId, setDownloadMenuId] = useState<string | null>(null)
  const [historySrtMenuId, setHistorySrtMenuId] = useState<string | null>(null)
  const [mainSrtMenuOpen, setMainSrtMenuOpen] = useState(false)
  const [status, setStatus] = useState<Record<string, EngineStatus>>({})
  const [cloneName, setCloneName] = useState('')
  const [cloneFile, setCloneFile] = useState<File | null>(null)
  const [cloneTags, setCloneTags] = useState<VoiceTagLabel[]>([])
  const [previewSample, setPreviewSample] = useState('')
  const [srtRaw, setSrtRaw] = useState(() => {
    try {
      return localStorage.getItem(TTS_SRT_LS_KEY) || ''
    } catch {
      return ''
    }
  })
  /** Dashboard « Nhập nội dung »: text | srt — quyết định synth path */
  const [inputMode, setInputMode] = useState<'text' | 'srt'>(() => {
    try {
      const mode = localStorage.getItem(TTS_INPUT_MODE_LS_KEY)
      return mode === 'srt' ? 'srt' : 'text'
    } catch {
      return 'text'
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(TTS_TEXT_LS_KEY, text)
    } catch {
      /* ignore */
    }
  }, [text])

  useEffect(() => {
    try {
      localStorage.setItem(TTS_SRT_LS_KEY, srtRaw)
    } catch {
      /* ignore */
    }
  }, [srtRaw])

  useEffect(() => {
    try {
      localStorage.setItem(TTS_INPUT_MODE_LS_KEY, inputMode)
    } catch {
      /* ignore */
    }
  }, [inputMode])
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewingVoiceId, setPreviewingVoiceId] = useState<string | null>(null)
  const [previewGeneratingVoiceId, setPreviewGeneratingVoiceId] = useState<string | null>(null)
  const [selectedVoiceIds, setSelectedVoiceIds] = useState<Set<string>>(() => new Set())
  const [bulkMoveOpen, setBulkMoveOpen] = useState(false)
  const [voiceQuery, setVoiceQuery] = useState('')
  const [voiceTag, setVoiceTag] = useState('')
  const [voiceListPage, setVoiceListPage] = useState(1)
  const [voiceListPageSize, setVoiceListPageSize] = useState(25)
  const [editingVoice, setEditingVoice] = useState<Voice | null>(null)
  const [localFavorites, setLocalFavorites] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(FAVORITE_LS_KEY)
      if (!raw) return new Set()
      const arr = JSON.parse(raw) as unknown
      return new Set(Array.isArray(arr) ? arr.map(String) : [])
    } catch {
      return new Set()
    }
  })
  const [dashLayout, setDashLayout] = useState<DashLayout>(() => loadDashLayout())
  const [dashActive, setDashActive] = useState<DashId | null>(null)
  const dashRef = useRef<HTMLDivElement>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  const voicePreviewRef = useRef<HTMLAudioElement | null>(null)
  const historyAudioRef = useRef<HTMLAudioElement | null>(null)

  function stopHistoryPlayback() {
    const player = historyAudioRef.current
    if (player) {
      player.onended = null
      player.onerror = null
      player.pause()
      player.currentTime = 0
      historyAudioRef.current = null
    }
    setPlayingHistoryId(null)
  }

  useEffect(() => () => stopHistoryPlayback(), [])

  function isVoiceFavorite(v: Voice) {
    if (v.favorite) return true
    return localFavorites.has(v.id)
  }

  function persistLocalFavorites(next: Set<string>) {
    setLocalFavorites(next)
    try {
      localStorage.setItem(FAVORITE_LS_KEY, JSON.stringify([...next]))
    } catch {
      /* ignore */
    }
  }

  async function onToggleFavorite(v: Voice) {
    const next = !isVoiceFavorite(v)
    const bucket = voiceEngineBucket(v)
    if (bucket === 'zmai' || bucket === 'clone') {
      setBusyKind('clone')
      setBusy(true)
      setError('')
      try {
        await api.ttsStudioVoicePatch(v.id, { favorite: next })
        onRefreshVoices?.(lang)
        toast.success(next ? t('Đã thêm vào yêu thích.', 'Added to favorites.') : t('Đã bỏ yêu thích.', 'Removed from favorites.'))
      } catch (e) {
        const msg = e instanceof Error ? e.message : t('Không thể lưu yêu thích', 'Could not save favorite')
        setError(msg)
        toast.error(msg)
      } finally {
        setBusy(false)
        setBusyKind(null)
      }
      return
    }
    const set = new Set(localFavorites)
    if (next) set.add(v.id)
    else set.delete(v.id)
    persistLocalFavorites(set)
    toast.success(next ? t('Đã thêm vào yêu thích.', 'Added to favorites.') : t('Đã bỏ yêu thích.', 'Removed from favorites.'))
  }

  const sortedVoices = useMemo(() => {
    const pref = [...voices]
    pref.sort((a, b) => {
      const fav = (v: Voice) => (v.favorite || localFavorites.has(v.id) ? 0 : 1)
      const score = (v: Voice) => {
        const bkt = voiceEngineBucket(v)
        if (bkt === 'zmai' || bkt === 'vieneu' || bkt === 'clone') return 0
        if (bkt === 'capcut') return 1
        if (bkt === 'eleven') return 2
        return 3
      }
      return fav(a) - fav(b) || score(a) - score(b) || a.name.localeCompare(b.name, 'vi')
    })
    return pref
  }, [voices, localFavorites])

  /** Chỉ giọng thuộc Engine đang chọn */
  const engineVoices = useMemo(
    () => engine === 'all' ? sortedVoices : sortedVoices.filter((v) => voiceEngineBucket(v) === engine),
    [sortedVoices, engine],
  )
  const voiceFilterTags: readonly string[] = VOICE_TAGS
  const activeVoiceTag = voiceFilterTags.includes(voiceTag) ? voiceTag : ''
  const visibleEngineVoices = useMemo(() => {
    const query = voiceQuery.trim().toLocaleLowerCase('vi')
    const langFilter = lang && lang !== 'auto' ? lang.split('-')[0] : ''
    return engineVoices.filter((v) => {
      const metadata = voiceMetadata(v)
      const matchesLang = !langFilter || !v.language || v.language.split('-')[0] === langFilter
      const matchesTag = !activeVoiceTag || metadata.tags.some((tag) => tag.label === activeVoiceTag)
      const matchesQuery = !query || [v.name, metadata.description, ...metadata.tags.map((tag) => tag.label)]
        .join(' ')
        .toLocaleLowerCase('vi')
        .includes(query)
      return matchesLang && matchesTag && matchesQuery
    })
  }, [activeVoiceTag, engineVoices, lang, voiceQuery])
  const voiceListPageCount = Math.max(1, Math.ceil(visibleEngineVoices.length / voiceListPageSize))
  const safeVoiceListPage = Math.min(voiceListPage, voiceListPageCount)
  const pagedEngineVoices = useMemo(() => {
    const start = (safeVoiceListPage - 1) * voiceListPageSize
    return visibleEngineVoices.slice(start, start + voiceListPageSize)
  }, [safeVoiceListPage, visibleEngineVoices, voiceListPageSize])
  const canBulkManage = engine === 'zmai' || engine === 'clone'
  const selectedVoiceCount = canBulkManage
    ? engineVoices.filter((v) => selectedVoiceIds.has(v.id)).length
    : 0
  const allEngineVoicesSelected =
    canBulkManage && engineVoices.length > 0 && selectedVoiceCount === engineVoices.length
  const bulkMoveTarget: 'zmai' | 'clone' = engine === 'zmai' ? 'clone' : 'zmai'
  const bulkMoveSourceLabel = engine === 'clone' ? 'Clone' : 'zmAI'
  const bulkMoveTargetLabel = bulkMoveTarget === 'clone' ? 'Clone' : 'zmAI'

  const cloneCount = useMemo(
    () => sortedVoices.filter((v) => voiceEngineBucket(v) === 'clone').length,
    [sortedVoices],
  )
  const selectedVoice = useMemo(() => voices.find((v) => v.id === voice), [voices, voice])
  const isVieneuVoice = selectedVoice ? voiceEngineBucket(selectedVoice) === 'vieneu' : false

  useEffect(() => {
    setSelectedVoiceIds(new Set())
    setVoiceListPage(1)
  }, [engine, lang])

  useEffect(() => {
    setVoiceListPage(1)
  }, [voiceQuery, activeVoiceTag, voiceListPageSize])

  useEffect(() => {
    // Keep restored/preferred voice until the async list arrives; only then fall back.
    if (voice && engineVoices.some((v) => v.id === voice)) {
      preferredVoiceRef.current = voice
      return
    }
    if (!engineVoices.length) {
      // App seeds eleven+system before /api/voices returns — don't wipe restored vn:/cc: ids.
      const looksSeed =
        voices.length === 0 ||
        (voices.length <= 3 &&
          voices.every((v) => {
            const b = voiceEngineBucket(v)
            return b === 'eleven' || b === 'system'
          }))
      if (!looksSeed && voice) setVoice('')
      return
    }
    const preferred = preferredVoiceRef.current
    if (preferred && engineVoices.some((v) => v.id === preferred)) {
      if (voice !== preferred) setVoice(preferred)
      return
    }
    const fallback = engineVoices[0].id
    preferredVoiceRef.current = fallback
    setVoice(fallback)
  }, [engineVoices, voice, lang, engine, voices])

  useEffect(() => {
    persistTtsSettings({
      lang,
      engine,
      voice,
      style,
      speed,
      volume,
      pitch,
      matchSrt,
      keepTimeline,
      normalize,
      gapOn,
      gapMs,
      trimSilence,
      autoSplit,
      playbackVolume,
      outputFormat,
    })
  }, [
    lang,
    engine,
    voice,
    style,
    speed,
    volume,
    pitch,
    matchSrt,
    keepTimeline,
    normalize,
    gapOn,
    gapMs,
    trimSilence,
    autoSplit,
    playbackVolume,
    outputFormat,
  ])

  useEffect(() => {
    persistDashLayout(dashLayout)
  }, [dashLayout])

  useEffect(() => {
    const onPopState = () => setSection(sectionFromUrl())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const setDashLayoutSafe = useCallback((next: DashLayout | ((prev: DashLayout) => DashLayout)) => {
    setDashLayout(next)
  }, [])

  useEffect(() => {
    onRefreshVoices?.(lang)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lang])

  useEffect(() => {
    if (!busy) {
      setBusyProgress(0)
      setBusyCustomMessage('')
    } else {
      setProgressMinimized(false)
    }
  }, [busy])

  useEffect(() => {
    setPlaybackTime(0)
    setPlaybackDuration(audioUrl ? duration : 0)
    setIsPlaying(false)
  }, [audioUrl, duration])

  useEffect(() => () => {
    voicePreviewRef.current?.pause()
    voicePreviewRef.current = null
  }, [])

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.ttsStatus())
    } catch {
      /* ignore */
    }
  }, [])

  const loadHistory = useCallback(async () => {
    try {
      const rows = await api.ttsStudioHistory()
      const formattedRows: HistoryItem[] = rows.slice(0, HISTORY_MAX).map((r) => ({
        id: String(r.id || ''),
        title: String(r.title || ''),
        voice: String(r.voice || ''),
        voiceName: r.voiceName ? String(r.voiceName) : undefined,
        engine: String(r.engine || ''),
        duration: Number(r.duration || 0),
        createdAt: String(r.createdAt || ''),
        audioUrl: String(r.audioUrl || ''),
        mp3Url: r.mp3Url ? String(r.mp3Url) : undefined,
        srtUrl: r.srtUrl ? String(r.srtUrl) : undefined,
        zipUrl: r.zipUrl ? String(r.zipUrl) : undefined,
        text: String(r.text || ''),
      }))
      setHistory(formattedRows)
      setHistoryPage(1)

      // ponytail: nếu chưa có audio preview nào (lần đầu vào), tự động khôi phục bản tạo gần nhất từ lịch sử
      setJobId((currentJobId) => {
        if (currentJobId) return currentJobId
        const latest = formattedRows[0]
        if (latest && latest.id && latest.audioUrl) {
          const t = Date.now()
          const finalAudio = `${latest.audioUrl}${latest.audioUrl.includes('?') ? '&' : '?'}t=${t}`
          const mp3 = latest.mp3Url || `/api/tts/studio/jobs/${latest.id}/audio.mp3`
          const finalMp3 = `${mp3}${mp3.includes('?') ? '&' : '?'}download=1&t=${t}`
          setAudioUrl(finalAudio)
          setMp3Url(finalMp3)
          setDuration(Number(latest.duration || 0))
          setText((curText) => curText || latest.text || '')
          return String(latest.id)
        }
        return null
      })
    } catch {
      /* ignore */
    }
  }, [])

  useEffect(() => {
    void loadStatus()
    void loadHistory()
  }, [loadStatus, loadHistory])

  const vieneu = status.vieneu

  useEffect(() => {
    if (vieneu?.loadState !== 'loading') return
    const timer = window.setInterval(() => void loadStatus(), 2000)
    return () => window.clearInterval(timer)
  }, [vieneu?.loadState, loadStatus])

  function go(id: string) {
    // input/srt gộp vào dashboard Tổng quan (không còn tab sidebar riêng)
    const next = id === 'input' || id === 'srt' || id === 'make' || !TTS_URL_SECTIONS.has(id)
      ? 'overview'
      : id
    const url = new URL(window.location.href)
    if (next === 'overview') url.searchParams.delete('tab')
    else url.searchParams.set('tab', next)
    const destination = `${url.pathname}${url.search}${url.hash}`
    if (destination !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
      window.history.pushState({ ttsSection: next }, '', destination)
    }
    setSection(next)
    setSideOpen(false)
  }

  const isFullDash = FULL_DASHBOARD.has(section)
  const showComingSoon = COMING_SOON.has(section)

  function applyJobUrls(res: {
    id: string
    duration: number
    audioUrl: string
    mp3Url?: string
  }) {
    const t = Date.now()
    const finalAudio = `${res.audioUrl}${res.audioUrl.includes('?') ? '&' : '?'}t=${t}`
    const mp3 = res.mp3Url || `/api/tts/studio/jobs/${res.id}/audio.mp3`
    const finalMp3 = `${mp3}${mp3.includes('?') ? '&' : '?'}download=1&t=${t}`
    setJobId(res.id)
    setAudioUrl(finalAudio)
    setMp3Url(finalMp3)
    setDuration(res.duration)
    try {
      localStorage.setItem(TTS_ACTIVE_JOB_LS_KEY, JSON.stringify({
        id: res.id,
        duration: res.duration,
        audioUrl: finalAudio,
        mp3Url: finalMp3,
      }))
    } catch {
      /* ignore */
    }
  }

  function triggerDownload(url: string | undefined, filename: string) {
    startDownload(url, filename)
    setDownloadMenuId(null)
    setHistorySrtMenuId(null)
    setMainSrtMenuOpen(false)
  }

  useEffect(() => {
    if (!downloadMenuId && !mainSrtMenuOpen) return
    const onDoc = (e: MouseEvent) => {
      const el = e.target as HTMLElement | null
      if (el?.closest?.('[data-dl-menu]')) return
      setDownloadMenuId(null)
      setHistorySrtMenuId(null)
      setMainSrtMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setDownloadMenuId(null)
        setHistorySrtMenuId(null)
        setMainSrtMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [downloadMenuId, mainSrtMenuOpen])

  useEffect(() => {
    if (!bulkMoveOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) setBulkMoveOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [bulkMoveOpen, busy])

  /** Phát trong player / Audio() — không mở tab mới, không nhảy giao diện. */
  function playHistoryItem(h: HistoryItem) {
    if (!h.audioUrl) return
    if (playingHistoryId === h.id) {
      stopHistoryPlayback()
      return
    }
    stopHistoryPlayback()
    audioRef.current?.pause()
    voicePreviewRef.current?.pause()
    setPreviewingVoiceId(null)
    const t = Date.now()
    const base = h.audioUrl.replace(/([?&])download=1/, '').replace(/([?&])t=\d+/, '')
    const inlineUrl = `${base}${base.includes('?') ? '&' : '?'}t=${t}`
    setJobId(h.id)
    setAudioUrl(inlineUrl)
    setDuration(h.duration || 0)
    if (h.mp3Url) setMp3Url(downloadWavHref(h.mp3Url) || null)
    try {
      const player = new Audio(inlineUrl)
      historyAudioRef.current = player
      setPlayingHistoryId(h.id)
      const reset = () => {
        if (historyAudioRef.current !== player) return
        historyAudioRef.current = null
        setPlayingHistoryId(null)
      }
      player.onended = reset
      player.onerror = reset
      void player.play().catch(reset)
    } catch {
      setPlayingHistoryId(null)
    }
  }

  async function onSynth() {
    const useSrt = inputMode === 'srt' && !!srtRaw.trim()
    if ((!useSrt && !text.trim()) || (useSrt && !srtRaw.trim()) || !voice) return
    setBusyKind('synth')
    setBusy(true)
    setBusyProgress(2)
    setBusyCustomMessage(useSrt ? 'Đang chuẩn bị tạo giọng từ SRT…' : 'Đang chuẩn bị tạo giọng nói…')
    setError('')
    const requestJobId = crypto.randomUUID().replaceAll('-', '').slice(0, 12)
    activeJobIdRef.current = requestJobId

    try {
      // Kick off — backend trả {id, running:true} ngay (không block)
      const res = await api.ttsStudioSynth({
        jobId: requestJobId,
        text: useSrt ? undefined : text.trim(),
        srtText: useSrt ? srtRaw : undefined,
        voice,
        lang,
        speed,
        volume,
        pitch,
        style,
        matchDuration: useSrt && matchSrt ? 'natural' : useSrt ? 'none' : matchSrt ? 'natural' : 'none',
        keepTimeline: useSrt ? keepTimeline : false,
        autoSplit: useSrt ? false : autoSplit,
        gapMs: useSrt ? 0 : gapOn ? gapMs : 0,
        title: (useSrt ? srtRaw : text).trim().slice(0, 48),
        outputDir: outputDir || '',
        outputFormat,
        publishOutput: true,
      })
      if (cancelledJobIdsRef.current.has(requestJobId)) return
      const jobIdFromRes = (res as { id?: string; job_id?: string }).id || (res as { id?: string; job_id?: string }).job_id || requestJobId
      let resolvedJobId = jobIdFromRes

      // Poll /progress đến khi done=true
      for (let i = 0; i < 1200; i++) {
        if (cancelledJobIdsRef.current.has(requestJobId)) return
        await new Promise((r) => window.setTimeout(r, 300))
        let p
        try {
          p = await api.ttsStudioJobProgress(jobIdFromRes)
        } catch {
          /* ignore transient errors */
          continue
        }
        if (p.error) throw new Error(p.error)
        if (p.resultJobId) resolvedJobId = p.resultJobId
        if (p.pct > 0) {
          setBusyProgress(p.pct)
          if (p.message) setBusyCustomMessage(p.message)
        }
        if (p.done) break
      }
      if (cancelledJobIdsRef.current.has(requestJobId)) return

      setBusyProgress(100)
      setBusyCustomMessage('Đã hoàn thành!')
      // applyJobUrls với fallback URL-based (file đã có sau khi done=true)
      applyJobUrls({
        id: resolvedJobId,
        duration: (res as { duration?: number }).duration || 0,
        audioUrl: (res as { audioUrl?: string }).audioUrl || `/api/tts/studio/jobs/${resolvedJobId}/audio.wav`,
        mp3Url: (res as { mp3Url?: string }).mp3Url,
      })
      const resAny = res as { publishError?: string; publishedDir?: string }
      if (resAny.publishError) {
        toast.error(t(`Lỗi xuất kết quả: ${resAny.publishError}`, `Output error: ${resAny.publishError}`))
      } else if (resAny.publishedDir) {
        toast.success(t(`Đã lưu vào: ${resAny.publishedDir}`, `Saved to: ${resAny.publishedDir}`))
      }
      // Cùng giọng + chữ + setting → server trả cache, không thêm lịch sử mới
      if (!(res as { cached?: boolean }).cached) await loadHistory()
      setTimeout(() => audioRef.current?.play().catch(() => {}), 80)
    } catch (e) {
      if (!cancelledJobIdsRef.current.has(requestJobId)) {
        setError(e instanceof Error ? e.message : 'Tạo giọng thất bại')
      }
    } finally {
      setBusyCustomMessage('')
      cancelledJobIdsRef.current.delete(requestJobId)
      if (activeJobIdRef.current === requestJobId) {
        activeJobIdRef.current = null
        setBusy(false)
        setBusyKind(null)
      }
    }
  }

  async function onPreview() {
    if (!voice) return
    let sample = previewSample.trim().slice(0, 200)
    if (!sample) {
      sample = previewSampleFor(lang).slice(0, 200)
      setPreviewSample(sample)
    }
    voicePreviewRef.current?.pause()
    setPreviewingVoiceId(null)
    setPreviewBusy(true)
    setError('')
    const requestJobId = crypto.randomUUID().replaceAll('-', '').slice(0, 12)
    try {
      const res = await api.ttsStudioSynth({
        jobId: requestJobId,
        text: sample,
        voice,
        lang,
        speed,
        volume,
        pitch,
        style,
        matchDuration: 'none',
        autoSplit: false,
        title: 'Nghe thử',
      })
      const jid = (res as { id?: string; job_id?: string }).id || requestJobId
      let resolvedJobId = jid
      for (let i = 0; i < 200; i++) {
        await new Promise((r) => window.setTimeout(r, 300))
        let p
        try {
          p = await api.ttsStudioJobProgress(jid)
        } catch { /* ignore */ }
        if (!p) continue
        if (p.error) throw new Error(p.error)
        if (p.resultJobId) resolvedJobId = p.resultJobId
        if (p.done) break
      }
      applyJobUrls({
        id: resolvedJobId,
        duration: (res as { duration?: number }).duration || 0,
        audioUrl: (res as { audioUrl?: string }).audioUrl || `/api/tts/studio/jobs/${resolvedJobId}/audio.wav`,
        mp3Url: (res as { mp3Url?: string }).mp3Url,
      })
      requestAnimationFrame(() => {
        const el = audioRef.current
        if (!el) return
        el.load()
        void el.play().catch(() => {
          /* autoplay policy — user bấm play trên controls */
        })
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Nghe thử thất bại')
    } finally {
      setPreviewBusy(false)
    }
  }


  async function onCancelJob() {
    const runningJobId = activeJobIdRef.current
    if (runningJobId) {
      cancelledJobIdsRef.current.add(runningJobId)
      activeJobIdRef.current = null
      setBusy(false)
      setBusyKind(null)
      setBusyProgress(0)
      setError('Đã hủy')
      try {
        await api.ttsStudioCancel(runningJobId)
      } catch {
        /* ignore */
      }
      return
    }
    audioRef.current?.pause()
  }

  async function onClone() {
    if (!cloneFile || !cloneName.trim()) {
      const msg = t('Chọn file audio và nhập tên giọng clone', 'Choose an audio file and enter a clone name')
      setError(msg)
      toast.error(msg)
      return
    }
    setBusyKind('clone')
    setBusy(true)
    setError('')
    try {
      const v = await api.ttsStudioClone(cloneName.trim(), cloneFile, '', cloneTags)
      setBusyProgress(100)
      setEngine('clone')
      preferredVoiceRef.current = v.id
      setVoice(v.id)
      onRefreshVoices?.(lang)
      setCloneFile(null)
      setCloneName('')
      setCloneTags([])
      toast.success(t('Đã thêm giọng clone thành công!', 'Cloned voice added successfully!'))
      go('voice') // danh sách / quản lý ở tab riêng
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('Clone thất bại', 'Clone failed')
      setError(msg)
      toast.error(msg)
    } finally {
      setBusy(false)
      setBusyKind(null)
    }
  }

  async function onSaveVoiceMetadata(
    voiceId: string,
    name: string,
    tags: VoiceTagLabel[],
    language: string,
    file: File | null,
  ) {
    setBusyKind('clone')
    setBusy(true)
    setError('')
    try {
      const v = await api.ttsStudioVoicePatch(voiceId, { name, tags, language })
      if (file) await api.ttsStudioVoiceReplaceAudio(v.id, file)
      if (voice === voiceId) {
        preferredVoiceRef.current = v.id
        setVoice(v.id)
      }
      onRefreshVoices?.(lang)
      setEditingVoice(null)
      toast.success(t('Đã lưu thông tin giọng thành công!', 'Voice metadata saved successfully!'))
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('Lưu thông tin giọng thất bại', 'Failed to save voice metadata')
      setError(msg)
      toast.error(msg)
      throw e
    } finally {
      setBusy(false)
      setBusyKind(null)
    }
  }

  async function onDeleteVoice(voiceId: string) {
    const label = voiceDisplayName(voiceId, voices)
    const isClone = voiceId.startsWith('vn:clone:')
    if (!window.confirm(isClone
      ? t(`Xóa giọng clone «${label}»?`, `Delete cloned voice “${label}”?`)
      : t(`Ẩn / xóa giọng zmAI «${label}» khỏi danh sách?`, `Hide/delete zmAI voice “${label}” from the list?`))) return
    setBusyKind('clone')
    setBusy(true)
    setError('')
    try {
      await api.ttsStudioVoiceDelete(voiceId)
      if (voice === voiceId) {
        preferredVoiceRef.current = ''
        setVoice('')
      }
      onRefreshVoices?.(lang)
      toast.success(t('Đã xóa giọng thành công!', 'Voice deleted successfully!'))
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('Xóa giọng thất bại', 'Failed to delete voice')
      setError(msg)
      toast.error(msg)
    } finally {
      setBusy(false)
      setBusyKind(null)
    }
  }

  async function onMoveVoice(voiceId: string, target: 'zmai' | 'clone') {
    setBusyKind('clone')
    setBusy(true)
    setError('')
    try {
      const v = await api.ttsStudioVoicePatch(voiceId, { engine: target })
      if (voice === voiceId) {
        preferredVoiceRef.current = v.id
        setVoice(v.id)
        setEngine(target)
      }
      onRefreshVoices?.(lang)
      toast.success(t('Đã chuyển engine giọng thành công!', 'Voice engine updated successfully!'))
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('Chuyển engine thất bại', 'Failed to change engine')
      setError(msg)
      toast.error(msg)
    } finally {
      setBusy(false)
      setBusyKind(null)
    }
  }

  function toggleBulkVoice(voiceId: string) {
    setSelectedVoiceIds((current) => {
      const next = new Set(current)
      if (next.has(voiceId)) next.delete(voiceId)
      else next.add(voiceId)
      return next
    })
  }

  function openBulkMoveModal() {
    if (!canBulkManage || busy || selectedVoiceCount === 0) return
    setBulkMoveOpen(true)
  }

  function closeBulkMoveModal() {
    if (busy) return
    setBulkMoveOpen(false)
  }

  async function confirmBulkMoveVoices() {
    if (!canBulkManage || busy) return
    const voiceIds = engineVoices.filter((v) => selectedVoiceIds.has(v.id)).map((v) => v.id)
    if (!voiceIds.length) {
      setBulkMoveOpen(false)
      return
    }
    const target = bulkMoveTarget

    setBusyKind('clone')
    setBusy(true)
    setError('')
    try {
      const result = await api.ttsStudioVoicesBulkMove(voiceIds, target)
      onRefreshVoices?.(lang)
      if (result.failures.length === 0) {
        setSelectedVoiceIds(new Set())
        setBulkMoveOpen(false)
        if (result.successes.length) {
          setEngine(target)
          preferredVoiceRef.current = result.successes[0].voice.id
          setVoice(result.successes[0].voice.id)
        }
        toast.success(t(`Đã chuyển ${result.successes.length} giọng sang ${bulkMoveTargetLabel}!`, `Moved ${result.successes.length} voices to ${bulkMoveTargetLabel}!`))
        return
      }
      // Partial: keep failed selected on source so user can retry; switch only on full success.
      setSelectedVoiceIds(new Set(result.failures.map((item) => item.voiceId)))
      setBulkMoveOpen(false)
      const details = result.failures.map((item) => `${item.voiceId}: ${item.error}`).join('; ')
      const warnMsg = `Đã chuyển ${result.successes.length}/${voiceIds.length} giọng sang ${bulkMoveTargetLabel}. ` +
        `Thất bại ${result.failures.length} (vẫn chọn): ${details}`
      setError(warnMsg)
      toast.warning(warnMsg)
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('Chuyển hàng loạt thất bại', 'Bulk move failed')
      setError(msg)
      toast.error(msg)
      setBulkMoveOpen(false)
    } finally {
      setBusy(false)
      setBusyKind(null)
    }
  }

  function playVoicePreview(voiceId: string, url: string) {
    const player = new Audio(`${url}${url.includes('?') ? '&' : '?'}t=${Date.now()}`)
    voicePreviewRef.current = player
    setPreviewingVoiceId(voiceId)
    const clear = () => {
      if (voicePreviewRef.current === player) {
        voicePreviewRef.current = null
        setPreviewingVoiceId(null)
      }
    }
    player.onended = clear
    player.onerror = () => {
      clear()
      setError('Không phát được audio mẫu của giọng này')
    }
    void player.play().catch(() => {
      clear()
      setError('Trình duyệt không cho phát audio mẫu')
    })
  }

  async function toggleVoicePreview(v: Voice) {
    const current = voicePreviewRef.current
    if (current && previewingVoiceId === v.id && !current.paused) {
      current.pause()
      setPreviewingVoiceId(null)
      return
    }
    current?.pause()
    audioRef.current?.pause()
    if (v.previewUrl) {
      playVoicePreview(v.id, v.previewUrl)
      return
    }
    setPreviewGeneratingVoiceId(v.id)
    setError('')
    try {
      const sampleLang = v.language || lang
      const previewJobId = crypto.randomUUID().replaceAll('-', '').slice(0, 12)
      const res = await api.ttsStudioSynth({
        jobId: previewJobId,
        text: previewSampleFor(sampleLang).slice(0, 200),
        voice: v.id,
        lang: sampleLang,
        speed,
        volume,
        pitch,
        style,
        matchDuration: 'none',
        autoSplit: false,
        title: t(
          `Nghe thử · ${voiceDisplayName(v.id, voices, v.name)}`,
          `Preview · ${voiceDisplayName(v.id, voices, v.name)}`,
        ).slice(0, 80),
      })
      const jid = (res as { id?: string; job_id?: string }).id || previewJobId
      let resolvedJobId = jid
      for (let i = 0; i < 200; i++) {
        await new Promise((r) => window.setTimeout(r, 300))
        let p
        try {
          p = await api.ttsStudioJobProgress(jid)
        } catch { /* ignore */ }
        if (!p) continue
        if (p.error) throw new Error(p.error)
        if (p.resultJobId) resolvedJobId = p.resultJobId
        if (p.done) break
      }
      const audioUrl = (res as { audioUrl?: string }).audioUrl || `/api/tts/studio/jobs/${resolvedJobId}/audio.wav`
      playVoicePreview(v.id, audioUrl)

    } catch (e) {
      setError(e instanceof Error ? e.message : 'Không tạo được audio nghe thử')
    } finally {
      setPreviewGeneratingVoiceId(null)
    }
  }

  /** Danh sách giọng theo engine; zmAI + clone có sửa / xóa / chuyển bucket. */
  function renderVoiceList() {
    if (!visibleEngineVoices.length) {
      return (
        <p className="tts-clone-empty">
          {engine === 'clone' && !voiceQuery ? 'Chưa có giọng clone.' : 'Không có giọng phù hợp với bộ lọc này.'}
        </p>
      )
    }
    return (
      <ul className="tts-clone-list">
        {pagedEngineVoices.map((v) => {
          const voiceBucket = voiceEngineBucket(v)
          const isClone = voiceBucket === 'clone'
          const isZmai = voiceBucket === 'zmai'
          const canManage = isClone || isZmai
          const bucket: 'zmai' | 'clone' = isClone ? 'clone' : 'zmai'
          const metadata = voiceMetadata(v)
          return (
          <li
            key={v.id}
            className={`tts-clone-item${voice === v.id ? ' is-active' : ''}`}
            aria-current={voice === v.id ? 'true' : undefined}
          >
            {canManage && (
              <input
                type="checkbox"
                className="tts-voice-check"
                checked={selectedVoiceIds.has(v.id)}
                disabled={busy}
                aria-label={`Chọn ${voiceDisplayName(v.id, voices, v.name)}`}
                onChange={() => toggleBulkVoice(v.id)}
              />
            )}
            <button
              type="button"
              className="tts-clone-pick"
              title="Chọn giọng này"
              onClick={() => {
                preferredVoiceRef.current = v.id
                setVoice(v.id)
                go('overview')
              }}
            >
              <span className="tts-voice-copy">
                <strong>{voiceDisplayName(v.id, voices, v.name)}</strong>
                <span className="tts-voice-description">{metadata.description}</span>
                <span className="tts-voice-tags" aria-label="Thông tin giọng">
                  {metadata.tags.map((tag) => (
                    <span key={`${tag.kind}:${tag.label}`} className={`tts-voice-tag ${tag.kind}`}>
                      {tag.label}
                    </span>
                  ))}
                </span>
              </span>
            </button>
            <div className="tts-clone-actions">
              <button
                type="button"
                className={`tts-btn-sm tts-btn-icon tts-btn-heart${isVoiceFavorite(v) ? ' is-fav' : ''}`}
                title={isVoiceFavorite(v) ? 'Bỏ yêu thích' : 'Thêm yêu thích'}
                aria-label={isVoiceFavorite(v) ? 'Bỏ yêu thích' : 'Thêm yêu thích'}
                aria-pressed={isVoiceFavorite(v)}
                disabled={busy}
                onClick={() => void onToggleFavorite(v)}
              >
                <IconHeart size={14} filled={isVoiceFavorite(v)} />
              </button>
              <button
                type="button"
                className="tts-btn-sm tts-btn-icon"
                title={previewingVoiceId === v.id
                  ? 'Dừng audio mẫu'
                  : v.previewUrl ? 'Phát audio mẫu gốc' : 'Tạo và phát câu nghe thử'}
                aria-label={previewingVoiceId === v.id ? 'Dừng' : 'Phát audio mẫu'}
                disabled={busy || previewGeneratingVoiceId !== null}
                onClick={() => void toggleVoicePreview(v)}
              >
                {previewGeneratingVoiceId === v.id
                  ? <span aria-hidden>…</span>
                  : previewingVoiceId === v.id ? <IconPause size={13} /> : <IconPlay size={13} />}
              </button>
              {canManage && (
                <>
                  <select
                    className="tts-voice-move"
                    value={bucket}
                    disabled={busy}
                    title="Chuyển sang engine khác"
                    aria-label="Chuyển engine"
                    onChange={(e) => {
                      const next = e.target.value as 'zmai' | 'clone'
                      if (next === bucket) return
                      void onMoveVoice(v.id, next)
                    }}
                  >
                    <option value="zmai">zmAI</option>
                    <option value="clone">Clone</option>
                  </select>
                  <button type="button" className="tts-btn-sm" disabled={busy} onClick={() => setEditingVoice(v)}>
                    Sửa
                  </button>
                  <button type="button" className="tts-btn-sm" disabled={busy} onClick={() => void onDeleteVoice(v.id)}>
                    Xóa
                  </button>
                </>
              )}
            </div>
          </li>
          )
        })}
      </ul>
    )
  }

  const busyTitle = busyKind === 'clone' ? 'Clone giọng nói' : 'Tạo giọng nói'
  const busyMessage =
    busyCustomMessage ||
    (busyKind === 'clone'
      ? 'Đang tạo giọng clone…'
      : srtRaw.trim()
        ? 'Đang tạo giọng từ SRT…'
        : isVieneuVoice
          ? 'Đang tạo giọng VieNeu (lần đầu có thể nạp model)…'
          : 'Đang tạo giọng nói…')

  const historyPanel = (
    <TtsHistoryPanel
      history={history}
      voices={voices}
      page={historyPage}
      setPage={setHistoryPage}
      downloadMenuId={downloadMenuId}
      historySrtMenuId={historySrtMenuId}
      onToggleDownloadMenu={(id) => {
        setDownloadMenuId((cur) => (cur === id ? null : id))
        setHistorySrtMenuId(null)
      }}
      onToggleSrtMenu={(id) => setHistorySrtMenuId((cur) => (cur === id ? null : id))}
      playingHistoryId={playingHistoryId}
      onPlay={playHistoryItem}
      onDelete={(h) => {
        if (playingHistoryId === h.id) stopHistoryPlayback()
        void api.ttsStudioDelete(h.id).then(() => {
          toast.success(t('Đã xóa bản thu thành công!', 'History item deleted successfully!'))
          return loadHistory()
        }).catch((e) => {
          toast.error(e instanceof Error ? e.message : t('Xóa thất bại', 'Delete failed'))
        })
      }}
      isDesktopApp={isDesktopApp}
      onReveal={(id, kind, style) => {
        setDownloadMenuId(null)
        setHistorySrtMenuId(null)
        void revealTtsOutput(kind, style, id)
      }}
      onDownload={triggerDownload}
    />
  )

  function onLoadTxt(file: File) {
    const reader = new FileReader()
    reader.onload = () => setText(String(reader.result || ''))
    reader.readAsText(file, 'utf-8')
  }

  function applySrtContent(raw: string) {
    const body = String(raw || '')
    if (!body.trim()) {
      setError('SRT rỗng')
      return
    }
    if (!looksLikeSrt(body)) {
      setError('Nội dung không giống SRT (thiếu timestamp --> ). Vẫn giữ nguyên để bạn sửa.')
    } else {
      setError('')
    }
    // Giữ nguyên file SRT (BOM/xuống dòng/timestamp) — synth_srt_job parse 1 cue = 1 TTS
    setSrtRaw(body)
    setInputMode('srt')
    setKeepTimeline(true) // mặc định giữ timeline khi vào mode SRT
    setAutoSplit(false) // SRT không tách câu CapCut
    setText(srtPreviewLines(body))
  }

  async function revealTtsOutput(kind: 'wav' | 'mp3' | 'srt' | 'zip', style = 'hard', targetJobId = jobId) {
    if (!targetJobId) return
    try {
      const response = await fetch(
        `/api/tts/studio/jobs/${encodeURIComponent(targetJobId)}/reveal/${kind}?style=${encodeURIComponent(style)}`,
        { method: 'POST' },
      )
      if (!response.ok) throw new Error(await response.text())
    } catch (e) {
      setError(e instanceof Error ? e.message : t('Không thể mở kết quả TTS', 'Could not open the TTS output'))
    }
  }

  function onLoadSrt(file: File) {
    const reader = new FileReader()
    reader.onload = () => applySrtContent(String(reader.result || ''))
    reader.readAsText(file, 'utf-8')
  }

  function switchInputMode(mode: 'text' | 'srt') {
    setInputMode(mode)
    setError('')
    if (mode === 'srt') {
      setKeepTimeline(true)
      setAutoSplit(false)
      if (!srtRaw.trim() && looksLikeSrt(text)) {
        applySrtContent(text)
      }
    }
  }

  async function onPasteClipboard() {
    try {
      const tVal = await navigator.clipboard.readText()
      if (!tVal?.trim()) {
        const msg = t('Clipboard trống', 'Clipboard is empty')
        setError(msg)
        toast.info(msg)
        return
      }
      if (looksLikeSrt(tVal) || inputMode === 'srt') {
        applySrtContent(tVal)
      } else {
        setSrtRaw('')
        setInputMode('text')
        setText(tVal)
        setError('')
      }
      toast.success(t('Đã dán nội dung từ clipboard.', 'Pasted content from clipboard.'))
    } catch {
      const msg = t('Không đọc được clipboard — cho phép quyền dán hoặc Ctrl+V vào ô', 'Could not read clipboard — allow paste permission or use Ctrl+V in the box')
      setError(msg)
      toast.error(msg)
    }
  }

  return (
    <div className={`tts-studio${sideOpen ? ' tts-studio--side-open' : ''}`}>
      {sideOpen && (
        <button
          type="button"
          className="tts-side-backdrop"
          aria-label="Đóng menu"
          onClick={() => setSideOpen(false)}
        />
      )}
      {/* ── Left sidebar (desktop cố định / mobile drawer) ── */}
      <aside className={`tts-side${sideOpen ? ' is-open' : ''}`} aria-hidden={false}>
        <div className="tts-side-head">
          <strong>Menu TTS</strong>
          <button
            type="button"
            className="tts-side-close"
            aria-label="Đóng"
            onClick={() => setSideOpen(false)}
          >
            ×
          </button>
        </div>
        <div className="tts-side-body">
          <select
            className="tts-side-select"
            value={section === 'overview' || section === 'make' || section === 'input' || section === 'srt' ? 'overview' : section}
            onChange={(e) => go(e.target.value)}
          >
            <option value="overview">Tổng quan</option>
            <option value="history">Lịch sử tạo</option>
            <option value="clone">Clone giọng nói</option>
          </select>

          <div className="tts-sec">Tạo giọng nói</div>
          <button type="button" className={`tts-nav${section === 'make' || section === 'overview' || section === 'input' || section === 'srt' ? ' active' : ''}`} onClick={() => go('overview')}>
            <IconMic size={14} /> Tạo giọng nói
          </button>
          <button type="button" className={`tts-nav${section === 'history' ? ' active' : ''}`} onClick={() => go('history')}>
            <IconClock /> Lịch sử tạo
          </button>

          <div className="tts-sec">Quản lý giọng</div>
          <button type="button" className={`tts-nav${section === 'voice' ? ' active' : ''}`} onClick={() => go('voice')}>
            <IconUsers /> Danh sách giọng
          </button>
          <button type="button" className={`tts-nav${section === 'clone' ? ' active' : ''}`} onClick={() => go('clone')}>
            <IconClone /> Clone giọng nói
            <span className="pill-new">Mới</span>
          </button>

          <div className="tts-sec">Cài đặt</div>
          <button type="button" className={`tts-nav${section === 'engines' ? ' active' : ''}`} onClick={() => go('engines')}>
            <IconGear /> TTS Engines
          </button>
          <button type="button" className={`tts-nav${section === 'audio' ? ' active' : ''}`} onClick={() => go('audio')}>
            <IconSpeaker size={14} /> Cấu hình âm thanh
          </button>
          <button type="button" className={`tts-nav${section === 'match' ? ' active' : ''}`} onClick={() => go('match')}>
            <IconClock /> Khớp thời lượng
          </button>
          <button type="button" className={`tts-nav${section === 'advanced' ? ' active' : ''}`} onClick={() => go('advanced')}>
            <IconList /> Tùy chọn nâng cao
          </button>
        </div>

        <div className="tts-engine-card">
          <div className="top">
            <h4>{vieneu?.name || 'VieNeu Local'}</h4>
            <span className="tts-pill-local">Local</span>
          </div>
          <div className="meta">
            <div className="meta-row">
              <span className="meta-lab">Trạng thái</span>
              <strong className={vieneu?.ready ? 'ok' : 'bad'}>
                {vieneu?.ready ? 'Sẵn sàng' : (vieneu?.message || 'Chưa cài').slice(0, 48)}
              </strong>
            </div>
            <div className="meta-row">
              <span className="meta-lab">Thiết bị</span>
              <span className="meta-val">{vieneu?.device || '—'}</span>
            </div>
            <div className="meta-row">
              <span className="meta-lab">Model</span>
              <span className="meta-val" title={vieneu?.model || 'VieNeu-TTS-v3-Turbo'}>
                {(vieneu?.model || 'VieNeu-TTS-v3-Turbo').replace('VieNeu-TTS-', 'v')}
              </span>
            </div>
            <div className="meta-row">
              <span className="meta-lab">Preset</span>
              <span className="meta-val">{vieneu?.presetCount ?? 0}</span>
            </div>
          </div>
          <div className="tts-ram">
            <i style={{ width: vieneu?.loaded ? '42%' : vieneu?.installed ? '18%' : '6%' }} />
          </div>
          {!vieneu?.installed && (
            <p className="tts-engine-hint">
              {vieneu?.installHint || 'pip install vieneu onnxruntime soundfile soxr sea-g2p'}
            </p>
          )}
          <button type="button" className="tts-link" onClick={() => void loadStatus()}>
            Làm mới trạng thái
          </button>
        </div>
      </aside>

      {/* ── Main ── */}
      <div className="tts-main">
        <div className="tts-page-head">
          <div>
            <BackTitle onBack={onBack}>Text to Speech (TTS)</BackTitle>
            <p>Nhập văn bản, chọn giọng và tạo giọng nói AI tự nhiên</p>
          </div>
          <div className="tts-page-actions">
            {isFullDash && (
              <button
                type="button"
                title="Khôi phục bố cục mặc định 4+2"
                onClick={() => {
                  setDashLayout(structuredClone(DEFAULT_DASH_LAYOUT))
                  setDashActive(null)
                }}
              >
                Đặt lại layout
              </button>
            )}
            <button type="button"><IconHelp size={14} /> Hướng dẫn</button>
            <button type="button"><IconKb size={14} /> Phím tắt</button>
          </div>
        </div>

        {(isFullDash || section === 'clone') && (
          <div className="tts-mobile-mode-tabs" role="tablist" aria-label="Chế độ tạo giọng">
            <button
              type="button"
              role="tab"
              aria-selected={isFullDash}
              className={isFullDash ? 'active' : undefined}
              onClick={() => go('overview')}
            >
              <IconMic size={16} /> Tạo giọng nói
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={section === 'clone'}
              className={section === 'clone' ? 'active' : undefined}
              onClick={() => go('clone')}
            >
              <IconClone /> Clone giọng nói
            </button>
          </div>
        )}

        {error && <div className="tts-error">{error}</div>}

        {showComingSoon && (
          <div className="tts-coming">
            <div className="tts-coming-card">
              <div className="tts-coming-ico">🚀</div>
              <h2>{SECTION_LABELS[section] || 'Tính năng'}</h2>
              <p>Trang này đang được phát triển.</p>
              <p className="tts-coming-soon">Sắp ra mắt…</p>
              <button type="button" className="tts-btn tts-btn-blue" onClick={() => go('overview')}>
                Về Tổng quan
              </button>
            </div>
          </div>
        )}

        {section === 'voice' && (
          <div className="tts-page-panel tts-voice-page">
            <section className="tts-card" id="tts-voice-list">
              <h3 className="tts-card-title">
                <span className="tts-step">4</span> Danh sách giọng
              </h3>
              <div className="tts-voice-toolbar">
                <label className="tts-field">
                  <span>Ngôn ngữ</span>
                  <select
                    value={lang}
                    onChange={(e) => {
                      setLang(e.target.value)
                      preferredVoiceRef.current = ''
                      setVoice('')
                    }}
                  >
                    <option value="auto">Tự động</option>
                    <option value="vi">Tiếng Việt</option>
                    <option value="en">Tiếng Anh</option>
                    <option value="zh">Tiếng Trung</option>
                    <option value="ja">Tiếng Nhật</option>
                    <option value="ko">Tiếng Hàn</option>
                    <option value="th">Tiếng Thái</option>
                    <option value="id">Tiếng Indonesia</option>
                    <option value="es">Tiếng Tây Ban Nha</option>
                    <option value="fr">Tiếng Pháp</option>
                    <option value="de">Tiếng Đức</option>
                    <option value="pt">Tiếng Bồ Đào Nha</option>
                  </select>
                </label>
                <label className="tts-field">
                  <span>Engine</span>
                  <select
                    value={engine}
                    onChange={(e) => {
                      setEngine(e.target.value as typeof engine)
                      preferredVoiceRef.current = ''
                      setVoice('')
                    }}
                  >
                    <option value="all">{t('Tất cả', 'All')}</option>
                    <option value="zmai">zmAI</option>
                    <option value="vieneu">VieNeu Local</option>
                    <option value="clone">Clone{cloneCount > 0 ? ` (${cloneCount})` : ''}</option>
                    <option value="capcut">CapCut TTS</option>
                    <option value="eleven">ElevenLabs</option>
                    <option value="system">System</option>
                  </select>
                </label>
                <label className="tts-voice-search">
                  <span className="tts-sr-only">Tìm giọng</span>
                  <svg aria-hidden="true" viewBox="0 0 24 24">
                    <circle cx="11" cy="11" r="7" />
                    <path d="m20 20-4-4" />
                  </svg>
                  <input
                    type="search"
                    value={voiceQuery}
                    onChange={(e) => setVoiceQuery(e.target.value)}
                    placeholder="Tìm kiếm giọng nói…"
                    aria-label="Tìm giọng theo tên, mô tả hoặc tag"
                  />
                </label>
                <label className="tts-field tts-voice-page-size">
                  <span>{t('Mỗi trang', 'Per page')}</span>
                  <select
                    value={voiceListPageSize}
                    aria-label={t('Số giọng mỗi trang', 'Voices per page')}
                    onChange={(e) => setVoiceListPageSize(Number(e.target.value))}
                  >
                    <option value={20}>20</option>
                    <option value={25}>25</option>
                    <option value={50}>50</option>
                    <option value={100}>100</option>
                  </select>
                </label>
              </div>
              <div className="tts-voice-filter">
                  <strong>Lọc theo tag:</strong>
                  <div className="tts-voice-filter-chips" role="group" aria-label="Lọc danh sách theo tag">
                    {voiceFilterTags.map((tag) => (
                      <button
                        key={tag}
                        type="button"
                        className={`tts-voice-filter-chip${activeVoiceTag === tag ? ' is-active' : ''}`}
                        aria-pressed={activeVoiceTag === tag}
                        onClick={() => setVoiceTag(activeVoiceTag === tag ? '' : tag)}
                      >
                        {tag}
                      </button>
                    ))}
                  </div>
                </div>
              <p style={{ margin: '0 0 10px', fontSize: '0.78rem', color: 'var(--tts-muted)' }}>
                {visibleEngineVoices.length} giọng phù hợp
                {engine === 'zmai' || engine === 'clone'
                  ? ' · Có thể Sửa / Xóa / chuyển zmAI ↔ Clone'
                  : ''}
              </p>
              {canBulkManage && (
                <div className="tts-voice-bulk">
                  <label>
                    <input
                      type="checkbox"
                      checked={allEngineVoicesSelected}
                      disabled={busy || !engineVoices.length}
                      onChange={(e) => {
                        setSelectedVoiceIds(
                          e.target.checked ? new Set(engineVoices.map((v) => v.id)) : new Set(),
                        )
                      }}
                    />
                    Chọn tất cả
                  </label>
                  <span>{selectedVoiceCount} đã chọn</span>
                  <button
                    type="button"
                    className="tts-btn-sm"
                    disabled={busy || selectedVoiceCount === 0}
                    onClick={openBulkMoveModal}
                  >
                    Chuyển sang {engine === 'zmai' ? 'Clone' : 'zmAI'}
                  </button>
                </div>
              )}
              {renderVoiceList()}
              {visibleEngineVoices.length > voiceListPageSize && (
                <nav className="tts-voice-pagination" aria-label={t('Phân trang danh sách giọng', 'Voice list pagination')}>
                  <span className="tts-pager-info">
                    {t(
                      `${(safeVoiceListPage - 1) * voiceListPageSize + 1}–${Math.min(safeVoiceListPage * voiceListPageSize, visibleEngineVoices.length)} / ${visibleEngineVoices.length} giọng`,
                      `${(safeVoiceListPage - 1) * voiceListPageSize + 1}–${Math.min(safeVoiceListPage * voiceListPageSize, visibleEngineVoices.length)} of ${visibleEngineVoices.length} voices`,
                    )}
                  </span>
                  <div className="tts-pager-btns">
                    <button
                      type="button"
                      className="tts-btn tts-btn-ghost"
                      disabled={safeVoiceListPage === 1}
                      onClick={() => setVoiceListPage((page) => Math.max(1, page - 1))}
                    >
                      {t('Trước', 'Previous')}
                    </button>
                    <span className="tts-pager-page" aria-current="page">
                      {t(`Trang ${safeVoiceListPage}/${voiceListPageCount}`, `Page ${safeVoiceListPage}/${voiceListPageCount}`)}
                    </span>
                    <button
                      type="button"
                      className="tts-btn tts-btn-ghost"
                      disabled={safeVoiceListPage === voiceListPageCount}
                      onClick={() => setVoiceListPage((page) => Math.min(voiceListPageCount, page + 1))}
                    >
                      {t('Sau', 'Next')}
                    </button>
                  </div>
                </nav>
              )}
              <div style={{ marginTop: 16, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button type="button" className="tts-btn tts-btn-blue" onClick={() => go('clone')}>
                  Clone giọng mới
                </button>
                <button type="button" className="tts-btn tts-btn-ghost" onClick={() => onRefreshVoices?.(lang)}>
                  Làm mới
                </button>
              </div>
            </section>
          </div>
        )}

        {section === 'clone' && (
          <div className="tts-page-panel" style={{ maxWidth: 520 }}>
            <VoiceClonePanel
              variant="page"
            cloneName={cloneName}
            cloneFile={cloneFile}
            cloneTags={cloneTags}
            cloneCount={cloneCount}
            busy={busy}
            onNameChange={setCloneName}
            onFileChange={setCloneFile}
            onTagsChange={setCloneTags}
            onSubmit={() => void onClone()}
            onOpenVoiceList={() => go('voice')}
            />
          </div>
        )}

        {section === 'history' && (
          <div className="tts-page-panel tts-history-page">
            <section className="tts-card tts-history-card" id="tts-history">
              <h3 className="tts-card-title"><span className="tts-step">7</span> Lịch sử tạo giọng</h3>
              {historyPanel}
            </section>
          </div>
        )}

        {isFullDash && (
        <>
        {/* Full dashboard — Tổng quan / Tạo giọng nói */}
        <div className="tts-dash" ref={dashRef}>
          <DashPanel
            id="input"
            item={dashLayout.input}
            active={dashActive === 'input'}
            gridRef={dashRef}
            onActive={setDashActive}
            onChange={setDashLayoutSafe}
          >
          <TtsInputPanel
            inputMode={inputMode}
            text={text}
            srtRaw={srtRaw}
            keepTimeline={keepTimeline}
            autoSplit={autoSplit}
            setAutoSplit={setAutoSplit}
            onSwitchMode={switchInputMode}
            onPickTxt={(f) => {
              setInputMode('text')
              setSrtRaw('')
              onLoadTxt(f)
            }}
            onPickSrt={onLoadSrt}
            onTextChange={(v) => {
              setText(v)
              if (srtRaw) setSrtRaw('')
            }}
            onSrtChange={(v) => {
              setSrtRaw(v)
              setText(srtPreviewLines(v))
            }}
            onClearText={() => setText('')}
            onClearSrt={() => {
              setSrtRaw('')
              setText('')
            }}
            onPasteClipboard={() => void onPasteClipboard()}
          />
          </DashPanel>

          <DashPanel
            id="voice"
            item={dashLayout.voice}
            active={dashActive === 'voice'}
            gridRef={dashRef}
            onActive={setDashActive}
            onChange={setDashLayoutSafe}
          >
          <section className="tts-card" id="tts-voice">
            <h3 className="tts-card-title"><span className="tts-step">2</span> Cài đặt giọng nói</h3>
            <div className="tts-inline" style={{ gap: 8, marginBottom: 8 }}>
              <label className="tts-field" style={{ flex: 1, marginBottom: 0 }}>
                <span>Ngôn ngữ</span>
                <select
                  value={lang}
                  onChange={(e) => {
                    setLang(e.target.value)
                    preferredVoiceRef.current = ''
                    setVoice('')
                  }}
                >
                  <option value="auto">Tự động</option>
                  <option value="vi">Tiếng Việt</option>
                  <option value="en">Tiếng Anh</option>
                  <option value="zh">Tiếng Trung</option>
                  <option value="ja">Tiếng Nhật</option>
                  <option value="ko">Tiếng Hàn</option>
                  <option value="th">Tiếng Thái</option>
                  <option value="id">Tiếng Indonesia</option>
                  <option value="es">Tiếng Tây Ban Nha</option>
                  <option value="fr">Tiếng Pháp</option>
                  <option value="de">Tiếng Đức</option>
                  <option value="pt">Tiếng Bồ Đào Nha</option>
                </select>
              </label>
              <label className="tts-field" style={{ flex: 1, marginBottom: 0 }}>
                <span>Engine</span>
                <div className="tts-inline">
                  <select
                    value={engine}
                    onChange={(e) => {
                      const eng = e.target.value as typeof engine
                      setEngine(eng)
                      preferredVoiceRef.current = ''
                      setVoice('') // effect chọn giọng đầu của engine
                    }}
                  >
                    <option value="all">{t('Tất cả', 'All')}</option>
                    <option value="zmai">zmAI</option>
                    <option value="vieneu">VieNeu Local</option>
                    <option value="clone">
                      Clone{cloneCount > 0 ? ` (${cloneCount})` : ''}
                    </option>
                    <option value="capcut">CapCut TTS</option>
                    <option value="eleven">ElevenLabs</option>
                    <option value="system">System</option>
                  </select>
                  {(engine === 'zmai' || engine === 'vieneu' || engine === 'clone' || engine === 'system') && (
                    <span className="tts-pill-local">Local</span>
                  )}
                </div>
              </label>
            </div>
            <label className="tts-field">
              <span>{t(`Giọng nói (${engineVoices.length})`, `Voices (${engineVoices.length})`)}</span>
              <div className="tts-inline">
                <select
                  value={voice}
                  onChange={(e) => {
                    preferredVoiceRef.current = e.target.value
                    setVoice(e.target.value)
                  }}
                >
                  {engineVoices.length === 0 && (
                    <option value="">
                      {engine === 'clone'
                        ? '— Chưa có giọng clone —'
                        : '— Không có giọng engine này —'}
                    </option>
                  )}
                  {engineVoices.map((v) => (
                    <option key={v.id} value={v.id}>{v.name}</option>
                  ))}
                </select>
                {selectedVoice?.previewUrl && (
                  <button
                    type="button"
                    className="tts-btn-sm tts-btn-icon"
                    onClick={() => toggleVoicePreview(selectedVoice)}
                    title={previewingVoiceId === selectedVoice.id ? 'Dừng audio mẫu' : 'Phát audio mẫu gốc (không tạo TTS)'}
                    aria-label={previewingVoiceId === selectedVoice.id ? 'Dừng' : 'Phát audio mẫu'}
                  >
                    {previewingVoiceId === selectedVoice.id ? <IconPause size={13} /> : <IconPlay size={13} />}
                  </button>
                )}
                <button
                  type="button"
                  className="tts-btn-sm tts-btn-list"
                  onClick={() => go('voice')}
                  title="Mở danh sách và chọn giọng"
                >
                  <IconUsers size={13} /> Danh sách
                </button>
              </div>
              {selectedVoice && (() => {
                const metadata = voiceMetadata(selectedVoice)
                return (
                  <div className="tts-voice-selected-meta">
                    <span>{metadata.description}</span>
                    <span className="tts-voice-tags" aria-label="Thông tin giọng đang chọn">
                      {metadata.tags.map((tag) => (
                        <span key={`${tag.kind}:${tag.label}`} className={`tts-voice-tag ${tag.kind}`}>
                          {tag.label}
                        </span>
                      ))}
                    </span>
                  </div>
                )
              })()}
            </label>
            <label className="tts-field">
              <span>Nghe thử giọng</span>
              <div className="tts-listen-row">
                <input
                  type="text"
                  value={previewSample}
                  onChange={(e) => setPreviewSample(e.target.value)}
                  placeholder={previewSampleFor(lang)}
                  title={t(`Trống → tự dùng: ${previewSampleFor(lang)}`, `Empty → use: ${previewSampleFor(lang)}`)}
                />
                <button
                  type="button"
                  className="tts-btn tts-btn-ghost"
                  disabled={busy || previewBusy || !voice}
                  onClick={() => void onPreview()}
                  title="Tổng hợp TTS từ câu trong ô (không phải audio mẫu gốc)"
                >
                  <IconHeadphones size={14} /> {previewBusy ? 'Đang tạo…' : 'Nghe thử'}
                </button>
              </div>
            </label>
            <div className="tts-slider-row">
              <div className="lab"><span>Tốc độ (Speed)</span></div>
              <div className="tts-slider-control"><div className="tts-slider-range"><input type="range" min={0.5} max={2} step={0.05} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} /><div className="tts-slider-marks tts-slider-marks-speed"><span>0.5x</span><span>1.0x</span><span>2.0x</span></div></div><SliderNumber value={Number(speed.toFixed(2))} min={0.5} max={2} step={0.05} label="Nhập tốc độ" onChange={setSpeed} /></div>
            </div>
            <div className="tts-slider-row">
              <div className="lab"><span>Âm lượng (Volume)</span></div>
              <div className="tts-slider-control"><div className="tts-slider-range"><input type="range" min={0.5} max={2} step={0.05} value={volume} onChange={(e) => setVolume(Number(e.target.value))} /><div className="tts-slider-marks"><span>50%</span><span>100%</span><span>150%</span><span>200%</span></div></div><SliderNumber value={Math.round(volume * 100)} min={50} max={200} step={5} label="Nhập âm lượng phần trăm" onChange={(value) => setVolume(value / 100)} /></div>
            </div>
            <div className="tts-slider-row">
              <div className="lab"><span>Cao độ (Pitch)</span></div>
              <div className="tts-slider-control"><div className="tts-slider-range"><input type="range" min={-12} max={12} step={1} value={pitch} onChange={(e) => setPitch(Number(e.target.value))} /><div className="tts-slider-marks"><span>-12</span><span>0</span><span>+12</span></div></div><SliderNumber value={pitch} min={-12} max={12} step={1} label="Nhập cao độ" onChange={setPitch} /></div>
            </div>
            {isVieneuVoice && (
              <label className="tts-field">
                <span>Phong cách (VieNeu)</span>
                <select value={style} onChange={(e) => setStyle(e.target.value)}>
                  <option value="tu_nhien">Tự nhiên</option>
                  <option value="tin_tuc">Tin tức</option>
                  <option value="doc_truyen">Đọc truyện</option>
                </select>
              </label>
            )}
          </section>
          </DashPanel>

          <DashPanel
            id="advanced"
            item={dashLayout.advanced}
            active={dashActive === 'advanced'}
            gridRef={dashRef}
            onActive={setDashActive}
            onChange={setDashLayoutSafe}
          >
          <section className="tts-card" id="tts-advanced">
            <h3 className="tts-card-title"><span className="tts-step">3</span> Tùy chọn nâng cao</h3>
            <label className="tts-check">
              <input type="checkbox" checked={matchSrt} onChange={(e) => setMatchSrt(e.target.checked)} />
              <span>
                Khớp thời lượng (khi nhập SRT)
                <small>Tự động điều chỉnh tốc độ để khớp thời gian phụ đề</small>
              </span>
            </label>
            <label className="tts-check">
              <input type="checkbox" checked={keepTimeline} onChange={(e) => setKeepTimeline(e.target.checked)} />
              <span>
                Giữ nguyên timeline SRT
                <small>
                  {t(
                    'Bật: audio và file xuất giữ đúng start/end của từng cue SRT. Tắt: nối tuần tự, timestamp SRT theo audio.',
                    'On: audio and exported files keep each SRT cue’s exact start/end. Off: cues are joined sequentially and SRT timestamps follow the audio.',
                  )}
                </small>
              </span>
            </label>
            <label className="tts-check">
              <input type="checkbox" checked={normalize} onChange={(e) => setNormalize(e.target.checked)} />
              <span>
                Chuẩn hóa âm lượng
                <small>Giữ âm lượng đồng đều giữa các câu</small>
              </span>
            </label>
            <label className={`tts-check${gapOn ? '' : ' tts-check-disabled'}`}>
              <input type="checkbox" checked={gapOn} onChange={(e) => setGapOn(e.target.checked)} />
              <span>
                Thêm khoảng nghỉ giữa câu
                <small>
                  {gapOn ? (
                    <input
                      type="number"
                      min={50}
                      max={2000}
                      value={gapMs}
                      onChange={(e) => setGapMs(Number(e.target.value))}
                      style={{ width: 72, marginLeft: 4, border: '1px solid var(--tts-line)', borderRadius: 6, padding: '2px 6px' }}
                    />
                  ) : null}
                  {' '}ms — {t('khoảng nghỉ ngắn giữa các câu', 'a short pause between sentences')}
                </small>
              </span>
            </label>
            <label className="tts-check">
              <input type="checkbox" checked={trimSilence} onChange={(e) => setTrimSilence(e.target.checked)} />
              <span>
                Loại bỏ khoảng lặng thừa
                <small>Tự động cắt khoảng lặng ở đầu và cuối</small>
              </span>
            </label>
            <label className="tts-field" style={{ marginTop: 4 }}>
              <span>Định dạng xuất audio</span>
              <select
                value={outputFormat}
                onChange={(e) => setOutputFormat(e.target.value as TtsOutputFormat)}
              >
                <option value="wav48">WAV (48kHz, 16bit)</option>
                <option value="wav16">WAV (16kHz)</option>
                <option value="mp3">MP3</option>
              </select>
            </label>
            <div className="tts-output-folder">
              <OutputFolderField
                isDesktopApp={isDesktopApp}
                value={outputDir}
                onChange={(value) => setOutputDir(value)}
                onSave={() => {
                  try { localStorage.setItem(OUTPUT_DIR_LS_KEY, outputDirRef.current) } catch { /* ignore */ }
                }}
                onChoose={isDesktopApp ? async () => {
                  const result = await studioApi.pickFolder()
                  return result.path || undefined
                } : undefined}
                defaultPath={t('Ví dụ: du-an-01 hoặc giong-doc.mp3', 'Example: project-01 or narration.mp3')}
                appFolder="text-to-speech"
                label={t('Thư mục đầu ra', 'Output folder')}
                disabled={busy}
              />

            </div>
          </section>
          </DashPanel>

          <DashPanel
            id="clone"
            item={dashLayout.clone}
            active={dashActive === 'clone'}
            gridRef={dashRef}
            onActive={setDashActive}
            onChange={setDashLayoutSafe}
          >
          <VoiceClonePanel
            variant="dash"
          cloneName={cloneName}
          cloneFile={cloneFile}
          cloneTags={cloneTags}
          cloneCount={cloneCount}
          busy={busy}
          onNameChange={setCloneName}
          onFileChange={setCloneFile}
          onTagsChange={setCloneTags}
          onSubmit={() => void onClone()}
          onOpenVoiceList={() => go('voice')}
          />
          </DashPanel>

<DashPanel
            id="preview"
            item={dashLayout.preview}
            active={dashActive === 'preview'}
            gridRef={dashRef}
            onActive={setDashActive}
            onChange={setDashLayoutSafe}
          >
          <section className="tts-card" id="tts-make">
            <h3 className="tts-card-title"><span className="tts-step">5</span> Xem trước & Tạo giọng nói</h3>
            <div className="tts-preview-body">
              <button
                type="button"
                className="tts-main-play"
                disabled={!audioUrl}
                aria-label={isPlaying ? 'Tạm dừng' : 'Phát'}
                title={isPlaying ? 'Tạm dừng' : 'Phát'}
                onClick={() => {
                  const a = audioRef.current
                  if (!a) return
                  if (a.paused) void a.play()
                  else a.pause()
                }}
              >
                {isPlaying ? <IconPause size={17} /> : <IconPlay size={20} />}
              </button>
              <div className="tts-player-main">
                <div className="tts-wave-box">
                  <button
                    type="button"
                    className={`tts-wave-vis${isPlaying ? ' is-playing' : ''}`}
                    disabled={!audioUrl}
                    aria-label={isPlaying ? 'Tạm dừng' : 'Phát audio'}
                    title={isPlaying ? 'Tạm dừng' : 'Phát audio'}
                    onClick={() => {
                      const a = audioRef.current
                      if (!a) return
                      if (a.paused) void a.play()
                      else a.pause()
                    }}
                  >
                    {WAVE_BARS.map((h, i) => (
                      <i
                        key={i}
                        className={
                          audioUrl &&
                          i / WAVE_BARS.length <=
                            playbackTime / Math.max(playbackDuration, duration, 0.01)
                            ? 'played'
                            : audioUrl
                              ? 'ready'
                              : undefined
                        }
                        style={{ height: `${h}px` }}
                      />
                    ))}
                  </button>
                </div>
                {audioUrl ? (
                  <>
                    <audio
                      ref={audioRef}
                      key={audioUrl}
                      className="tts-audio"
                      src={audioUrl}
                      preload="auto"
                      onLoadedMetadata={(e) => {
                        const d = e.currentTarget.duration
                        setPlaybackDuration(Number.isFinite(d) ? d : duration)
                        e.currentTarget.volume = playbackVolume
                      }}
                      onTimeUpdate={(e) => setPlaybackTime(e.currentTarget.currentTime)}
                      onPlay={() => setIsPlaying(true)}
                      onPause={() => setIsPlaying(false)}
                      onEnded={() => setIsPlaying(false)}
                    />
                    <div className="tts-player-controls">
                      <span className="tts-player-time">
                        {fmtDur(playbackTime)} / {fmtDur(playbackDuration || duration)}
                      </span>
                      <input
                        className="tts-seek"
                        type="range"
                        min={0}
                        max={Math.max(playbackDuration || duration, 0.01)}
                        step={0.01}
                        value={Math.min(playbackTime, playbackDuration || duration || 0)}
                        aria-label="Vị trí phát"
                        style={{
                          background: `linear-gradient(to right, var(--tts-blue) ${
                            (playbackTime / Math.max(playbackDuration || duration, 0.01)) * 100
                          }%, var(--tts-line) 0%)`,
                        }}
                        onChange={(e) => {
                          const next = Number(e.target.value)
                          setPlaybackTime(next)
                          if (audioRef.current) audioRef.current.currentTime = next
                        }}
                      />
                      <IconSpeaker size={14} />
                      <input
                        className="tts-player-volume"
                        type="range"
                        min={0}
                        max={1}
                        step={0.01}
                        value={playbackVolume}
                        aria-label="Âm lượng phát"
                        style={{
                          background: `linear-gradient(to right, var(--tts-blue) ${
                            playbackVolume * 100
                          }%, var(--tts-line) 0%)`,
                        }}
                        onChange={(e) => {
                          const next = Number(e.target.value)
                          setPlaybackVolume(next)
                          if (audioRef.current) audioRef.current.volume = next
                        }}
                      />
                    </div>
                  </>
                ) : (
                  <div className="tts-player-controls is-idle">
                    <span className="tts-player-time">00:00 / {fmtDur(duration)}</span>
                    <input className="tts-seek" type="range" min={0} max={1} value={0} disabled aria-label="Vị trí phát" readOnly />
                    <IconSpeaker size={14} />
                    <input
                      className="tts-player-volume"
                      type="range"
                      min={0}
                      max={1}
                      value={1}
                      disabled
                      aria-label="Âm lượng phát"
                      readOnly
                      style={{ background: 'var(--tts-line)' }}
                    />
                  </div>
                )}
              </div>
              <div className="tts-preview-actions">
                <button
                  type="button"
                  className="tts-btn tts-btn-blue"
                  disabled={
                    busy ||
                    !voice ||
                    (inputMode === 'srt' ? !srtRaw.trim() : !text.trim())
                  }
                  onClick={() => void onSynth()}
                >
                  <IconMic size={15} />{' '}
                  {busy
                    ? 'Đang tạo…'
                    : inputMode === 'srt'
                      ? 'Tạo giọng từ SRT'
                      : 'Tạo giọng nói'}
                </button>
                <button
                  type="button"
                  className="tts-btn tts-btn-ghost"
                  disabled={!busy && !jobId}
                  onClick={() => {
                    audioRef.current?.pause()
                    void onCancelJob()
                  }}
                >
                  Dừng / Hủy
                </button>
                <button
                  type="button"
                  className="tts-btn tts-btn-ghost"
                  disabled={!audioUrl}
                  onClick={() => {
                    setAudioUrl(null)
                    setMp3Url(null)
                    setJobId(null)
                    setDuration(0)
                    try {
                      localStorage.removeItem(TTS_ACTIVE_JOB_LS_KEY)
                    } catch {
                      /* ignore */
                    }
                  }}
                >
                  Xóa & Làm mới
                </button>
              </div>
            </div>
          </section>
          </DashPanel>

          <DashPanel
            id="export"
            item={dashLayout.export}
            active={dashActive === 'export'}
            gridRef={dashRef}
            onActive={setDashActive}
            onChange={setDashLayoutSafe}
          >
          <section className="tts-card" id="tts-export">
            <h3 className="tts-card-title"><span className="tts-step">6</span> Xuất kết quả</h3>
            <div className="tts-export-grid">
              {isDesktopApp ? (
                <>
                  <button type="button" className="tts-btn tts-btn-ghost" disabled={!jobId} onClick={() => void revealTtsOutput('wav')}>
                    <IconFile size={14} /> {t('Mở thư mục audio (WAV)', 'Open audio folder (WAV)')}
                  </button>
                  <button type="button" className="tts-btn tts-btn-ghost" disabled={!jobId} onClick={() => void revealTtsOutput('mp3')}>
                    <IconFile size={14} /> {t('Mở thư mục audio (MP3)', 'Open audio folder (MP3)')}
                  </button>
                </>
              ) : (
                <>
                  <a className="tts-btn tts-btn-ghost" href={downloadWavHref(audioUrl)} download={audioUrl ? `${webOutputStem}.wav` : undefined} style={{ pointerEvents: audioUrl ? 'auto' : 'none', opacity: audioUrl ? 1 : 0.5, textDecoration: 'none' }}>
                    <IconDownload size={14} /> {t('Tải audio (WAV)', 'Download audio (WAV)')}
                  </a>
                  <a className="tts-btn tts-btn-ghost" href={mp3Url || undefined} download={mp3Url ? `${webOutputStem}.mp3` : undefined} style={{ pointerEvents: mp3Url ? 'auto' : 'none', opacity: mp3Url ? 1 : 0.5, textDecoration: 'none' }}>
                    <IconDownload size={14} /> {t('Tải audio (MP3)', 'Download audio (MP3)')}
                  </a>
                </>
              )}
              <div className="tts-export-menu-wrap" data-dl-menu>
                <button
                  type="button"
                  className="tts-btn tts-btn-ghost"
                  disabled={!jobId}
                  aria-haspopup="menu"
                  aria-expanded={mainSrtMenuOpen}
                  onClick={() => setMainSrtMenuOpen((open) => !open)}
                >
                  <IconList size={14} /> {t('Xuất SRT cho CapCut', 'Export SRT for CapCut')}
                </button>
                {mainSrtMenuOpen && jobId && (
                  <div className="tts-dl-menu tts-export-srt-menu" role="menu">
                    {SRT_STYLE_OPTIONS.map((opt) => (
                      <button
                        key={opt.id}
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setMainSrtMenuOpen(false)
                          if (isDesktopApp) {
                            void revealTtsOutput('srt', opt.id)
                          } else {
                            triggerDownload(`/api/tts/studio/jobs/${jobId}/subs.srt?style=${opt.id}&t=${Date.now()}`, `${webOutputStem}-${opt.id}.srt`)
                          }
                        }}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              {isDesktopApp ? (
                <button type="button" className="tts-btn tts-btn-ghost" disabled={!jobId} onClick={() => void revealTtsOutput('zip')}>
                  <IconFile size={14} /> {t('Xuất ZIP (Audio + SRT)', 'Export ZIP (Audio + SRT)')}
                </button>
              ) : (
                <a className="tts-btn tts-btn-ghost" href={jobId ? `/api/tts/studio/jobs/${jobId}/bundle.zip?style=hard&t=${Date.now()}` : undefined} download={jobId ? `${webOutputStem}.zip` : undefined} style={{ pointerEvents: jobId ? 'auto' : 'none', opacity: jobId ? 1 : 0.5, textDecoration: 'none' }}>
                  <IconFile size={14} /> {t('Xuất ZIP (Audio + SRT)', 'Export ZIP (Audio + SRT)')}
                </a>
              )}
            </div>
          </section>
          </DashPanel>
        </div>

        <section className="tts-card tts-history-card" id="tts-history">
          <h3 className="tts-card-title"><span className="tts-step">7</span> Lịch sử tạo giọng</h3>
          {historyPanel}
        </section>
        </>
        )}
      </div>

      <ProgressPopup
        active={busy || Boolean(error && error !== 'Đã hủy' && error !== 'cancelled')}
        minimized={progressMinimized}
        running={busy}
        title={busy ? busyTitle : error ? 'Lỗi TTS' : 'TTS'}
        message={busy ? busyMessage : error || undefined}
        progress={busy ? busyProgress : error ? 0 : 100}
        error={!busy && error && error !== 'Đã hủy' ? error : null}
        onMinimize={() => {
          setProgressMinimized(true)
          if (!busy && error) setError('')
        }}
        onRestore={() => setProgressMinimized(false)}
        onCancel={busy ? () => { void onCancelJob() } : undefined}
      />

      {editingVoice && (
        <VoiceMetadataModal
          name={voiceDisplayName(editingVoice.id, voices, editingVoice.name)}
          tags={voiceMetadata(editingVoice).tags.map((tag) => tag.label)}
          language={editingVoice.language}
          onClose={() => setEditingVoice(null)}
          onSave={(name, tags, language, file) =>
            onSaveVoiceMetadata(editingVoice.id, name, tags, language, file)
          }
        />
      )}

      {bulkMoveOpen && (
        <div
          className="tts-modal-backdrop"
          role="presentation"
        >
          <div
            className="tts-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="tts-bulk-move-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="tts-bulk-move-title" className="tts-modal-title">
              Chuyển engine giọng
            </h3>
            <p className="tts-modal-body">
              Chuyển <strong>{selectedVoiceCount}</strong> giọng đã chọn từ{' '}
              <strong>{bulkMoveSourceLabel}</strong> sang <strong>{bulkMoveTargetLabel}</strong>.
            </p>
            <p className="tts-modal-hint">
              File giọng và registry sẽ được di chuyển sang engine đích. Thao tác này không tạo bản
              sao.
            </p>
            <div className="tts-modal-actions">
              <button
                type="button"
                className="tts-btn tts-btn-ghost"
                disabled={busy}
                onClick={closeBulkMoveModal}
              >
                Hủy
              </button>
              <button
                type="button"
                className="tts-btn tts-btn-blue"
                disabled={busy || selectedVoiceCount === 0}
                onClick={() => void confirmBulkMoveVoices()}
              >
                {busy ? t('Đang chuyển…', 'Moving…') : t(`Xác nhận · ${bulkMoveTargetLabel}`, `Confirm · ${bulkMoveTargetLabel}`)}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
