import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'
import type { JobStatus, ProjectSettings } from '@/features/project/project.types'
import { api } from '@/features/project/project.api'
import { localize, useLocale } from '@/app/i18n'
import { normalizeTranslatorForEngine, translatorOptions } from '@/app/appSettings'

type AnalysisRegion = { x: number; y: number; w: number; h: number }
type LiveTranslationModel = { id: string; label: string; free: boolean }
const CLOUD_TRANSLATORS = new Set(['openai', 'gemini', 'deepseek', 'openrouter', 'grok', 'groq', 'nvidia', 'mistral'])

const DEFAULT_ANALYSIS_REGION: AnalysisRegion = { x: 0.05, y: 0.55, w: 0.9, h: 0.28 }
const DEFAULT_BLUR_BAND_REGION: AnalysisRegion = { x: 0.08, y: 0.72, w: 0.84, h: 0.16 }

function clampRegion(r: AnalysisRegion): AnalysisRegion {
  const x = Math.max(0, Math.min(0.95, r.x))
  const y = Math.max(0, Math.min(0.95, r.y))
  const w = Math.max(0.08, Math.min(1 - x, r.w))
  const h = Math.max(0.06, Math.min(1 - y, r.h))
  return { x, y, w, h }
}
import {
  IconArrowRight,
  IconClock,
  IconGlobe,
  IconLangSwap,
  IconLayers,
  IconMic,
  IconPlay,
  IconSpeaker,
  IconTranslate,
  IconTrash,
  IconType,
  IconWand,
} from '@/shared/components/Icons'
import './ProjectSidebar.css'

/** Số hợp lệ (kể cả 0) — tránh `|| default` nuốt mất giá trị 0. */
function numOr(raw: unknown, fallback: number): number {
  const n = Number(raw)
  return Number.isFinite(n) ? n : fallback
}

function inputAspectLabel(width: number, height: number): string {
  if (!width || !height) return ''
  const ratio = width / height
  if (Math.abs(ratio - 9 / 16) < 0.025) return '9:16'
  if (Math.abs(ratio - 16 / 9) < 0.025) return '16:9'
  if (Math.abs(ratio - 1) < 0.025) return '1:1'
  const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : a)
  const divisor = gcd(Math.round(width), Math.round(height)) || 1
  return `${Math.round(width / divisor)}:${Math.round(height / divisor)}`
}

type Props = {
  projectId: string | null
  videoUrl: string | null
  settings: ProjectSettings
  logoDetection?: JobStatus['logoDetection']
  voices: { id: string; name: string }[]
  busy: boolean
  onSettings: (s: ProjectSettings) => void
  onSubtitleApplied?: (segments: import('@/features/project/project.types').Segment[], settings: ProjectSettings) => void
  onUpload: (file: File) => void
  onTranslateAll: () => void
  /** previewSec = số giây từ ô Preview (đã commit draft) */
  onPreview: (previewSec: number) => void
  onCancel: () => void
  onClearCache?: (parts: string[]) => void
  clearingCache?: boolean
}

export const CACHE_CLEAR_OPTIONS: { id: string; label: string }[] = [
  { id: 'covers', label: 'Vùng che / bbox OCR' },
  { id: 'ocr', label: 'OCR cache' },
  { id: 'whisper', label: 'Whisper / ASR' },
  { id: 'subtitle', label: 'Subtitle + đoạn thoại' },
  { id: 'translation', label: 'Translation cache' },
  { id: 'audio', label: 'Audio extract' },
  { id: 'tts', label: 'TTS cache' },
  { id: 'preview', label: 'Preview cache' },
  { id: 'render', label: 'Render / xuất' },
  { id: 'temp', label: 'Temp files' },
  { id: 'backend', label: 'Backend cache' },
  { id: 'frontend', label: 'Frontend cache' },
  { id: 'jobs', label: 'Job xử lý tạm' },
]

const ALL_CACHE_PARTS = CACHE_CLEAR_OPTIONS.map((o) => o.id)

function Field({
  label,
  icon,
  children,
  hint,
  className,
}: {
  label: string
  icon?: ReactNode
  children: ReactNode
  hint?: string
  className?: string
}) {
  return (
    <div className={`field${className ? ` ${className}` : ''}`}>
      <span className="field-label">
        {icon}
        {label}
      </span>
      {children}
      {hint && <em className="field-hint">{hint}</em>}
    </div>
  )
}

export default function Sidebar({
  projectId,
  videoUrl,
  settings,
  logoDetection,
  voices,
  busy,
  onSettings,
  onSubtitleApplied,
  onUpload,
  onTranslateAll,
  onPreview,
  onCancel,
  onClearCache,
  clearingCache = false,
}: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const inputRef = useRef<HTMLInputElement>(null)
  const subtitleInputRef = useRef<HTMLInputElement>(null)
  const previewShellRef = useRef<HTMLDivElement>(null)
  const [portrait, setPortrait] = useState(false)
  const [inputSize, setInputSize] = useState({ width: 0, height: 0 })
  const [previewTime, setPreviewTime] = useState(0)
  const [showCancel, setShowCancel] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const [clearParts, setClearParts] = useState<string[]>(() => [...ALL_CACHE_PARTS])
  const [subtitleSources, setSubtitleSources] = useState<{ name: string; label: string }[]>([])
  const [translationModels, setTranslationModels] = useState<LiveTranslationModel[]>([])
  const [translationModelsLoading, setTranslationModelsLoading] = useState(false)
  const [translationModel, setTranslationModel] = useState('')
  const [ollamaLocalModels, setOllamaLocalModels] = useState<string[]>([])
  const [previewDraft, setPreviewDraft] = useState(
    String(settings.previewSec > 0 ? settings.previewSec : 20),
  )

  useEffect(() => {
    if (!projectId) {
      setSubtitleSources([])
      return
    }
    void api.subtitles(projectId).then((r) => setSubtitleSources(r.items)).catch(() => setSubtitleSources([]))
  }, [projectId])

  useEffect(() => {
    if (!CLOUD_TRANSLATORS.has(settings.translator)) {
      setTranslationModels([])
      return
    }
    let cancelled = false
    setTranslationModelsLoading(true)
    void api.getConfig().then((cfg) => {
      if (!cancelled) setTranslationModel(cfg.cloud?.[settings.translator as keyof typeof cfg.cloud]?.model || '')
    }).catch(() => { if (!cancelled) setTranslationModel('') })
    fetch(`/api/chat/models?provider=${encodeURIComponent(settings.translator)}&includePaid=true`)
      .then((response) => response.ok ? response.json() as Promise<{ models?: unknown }> : Promise.reject(new Error('model discovery failed')))
      .then((data) => {
        const rows = Array.isArray(data.models) ? data.models.flatMap((item) => {
          if (!item || typeof item !== 'object') return []
          const row = item as Record<string, unknown>
          const id = String(row.id || '').trim()
          const capabilities = Array.isArray(row.capabilities) ? row.capabilities.map(String) : []
          if (capabilities.length && !capabilities.includes('text')) return []
          return id ? [{ id, label: String(row.label || id), free: row.free === true }] : []
        }) : []
        if (!cancelled) setTranslationModels(rows)
      })
      .catch(() => { if (!cancelled) setTranslationModels([]) })
      .finally(() => { if (!cancelled) setTranslationModelsLoading(false) })
    return () => { cancelled = true }
  }, [settings.translator])

  useEffect(() => {
    if (settings.translator !== 'ollama' || settings.ollamaMode !== 'local') {
      setOllamaLocalModels([])
      return
    }
    let cancelled = false
    fetch('/api/chat/models?provider=ollama&refresh=true')
      .then((response) => response.ok ? response.json() as Promise<{ models?: Array<{ id?: string }> }> : Promise.reject(new Error('ollama discovery failed')))
      .then((data) => {
        const models = (data.models || []).map((item) => String(item.id || '').trim()).filter(Boolean)
        if (!cancelled) setOllamaLocalModels(models)
      })
      .catch(() => { if (!cancelled) setOllamaLocalModels([]) })
    return () => { cancelled = true }
  }, [settings.ollamaMode, settings.translator])

  useEffect(() => {
    setInputSize({ width: 0, height: 0 })
    setPortrait(false)
    setPreviewTime(0)
  }, [videoUrl])

  // Giống nguyên tắc che của backend: các OCR variant của watermark động
  // @... là một logo; AI生成+ và AI生成十 cũng là một logo.
  const excludedLogoTexts = settings.hiddenLogoTexts || []
  const isLogoExcluded = (text: string) =>
    excludedLogoTexts.includes(text)
    || (text.startsWith('@') && excludedLogoTexts.some((item) => item.startsWith('@')))
    || (text.includes('生成') && excludedLogoTexts.some((item) => item.includes('生成')))
  const visibleLogoMasks = settings.coverLogo
    ? (logoDetection?.tracks || []).filter((track) => {
        const text = (track.text || '').trim()
        const start = Number(track.start || 0)
        const end = Number(track.end || 0)
        return Boolean(track.bbox)
          && !isLogoExcluded(text)
          && previewTime >= start - 0.04
          && previewTime <= end + 0.04
      })
    : []

  async function importSubtitle(file: File | undefined) {
    if (!file || !projectId || busy) return
    try {
      const result = await api.uploadSubtitle(projectId, file)
      setSubtitleSources(result.items)
      const applied = await api.applySubtitle(projectId, result.name)
      onSettings(applied.settings)
      onSubtitleApplied?.(applied.segments, applied.settings)
    } finally {
      if (subtitleInputRef.current) subtitleInputRef.current.value = ''
    }
  }

  async function selectSubtitle(name: string) {
    if (!projectId || busy) return
    const applied = await api.applySubtitle(projectId, name)
    onSettings(applied.settings)
    onSubtitleApplied?.(applied.segments, applied.settings)
  }
  const regionDragRef = useRef<{
    target: 'analysis' | 'blur'
    mode: 'move' | 'nw' | 'ne' | 'sw' | 'se' | 'n' | 's' | 'e' | 'w'
    startX: number
    startY: number
    origin: AnalysisRegion
    boxW: number
    boxH: number
  } | null>(null)

  const showAnalysisRoi =
    Boolean(settings.stableCaptionLocate)
    && Boolean(videoUrl)
    && !busy

  const analysisRegion = clampRegion(
    settings.analysisRegion && typeof settings.analysisRegion === 'object'
      ? {
          // ?? + isFinite: x/y = 0 là giá trị HỢP LỆ (mép trái/trên), `||` nuốt mất
          x: numOr(settings.analysisRegion.x, DEFAULT_ANALYSIS_REGION.x),
          y: numOr(settings.analysisRegion.y, DEFAULT_ANALYSIS_REGION.y),
          w: numOr(settings.analysisRegion.w, DEFAULT_ANALYSIS_REGION.w),
          h: numOr(settings.analysisRegion.h, DEFAULT_ANALYSIS_REGION.h),
        }
      : DEFAULT_ANALYSIS_REGION,
  )
  const showManualBlurRoi = settings.blurBandMode === 'manual' && Boolean(videoUrl) && !busy
  const blurBandRegion = clampRegion(
    settings.blurBandRegion && typeof settings.blurBandRegion === 'object'
      ? {
          x: numOr(settings.blurBandRegion.x, DEFAULT_BLUR_BAND_REGION.x),
          y: numOr(settings.blurBandRegion.y, DEFAULT_BLUR_BAND_REGION.y),
          w: numOr(settings.blurBandRegion.w, DEFAULT_BLUR_BAND_REGION.w),
          h: numOr(settings.blurBandRegion.h, DEFAULT_BLUR_BAND_REGION.h),
        }
      : DEFAULT_BLUR_BAND_REGION,
  )

  useEffect(() => {
    if (!settings.stableCaptionLocate) return
    if (settings.analysisRegion) return
    // Lần đầu bật: gán vùng mặc định (dải hardsub giữa-dưới)
    onSettings({ ...settings, analysisRegion: { ...DEFAULT_ANALYSIS_REGION } })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only seed once when toggled on
  }, [settings.stableCaptionLocate])

  useEffect(() => {
    if (settings.blurBandMode !== 'manual' || settings.blurBandRegion) return
    onSettings({ ...settings, blurBandRegion: { ...DEFAULT_BLUR_BAND_REGION } })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- seed only when manual mode is selected
  }, [settings.blurBandMode])

  function beginRegionDrag(
    target: 'analysis' | 'blur',
    mode: NonNullable<typeof regionDragRef.current>['mode'],
    e: ReactPointerEvent,
  ) {
    e.preventDefault()
    e.stopPropagation()
    const shell = previewShellRef.current
    if (!shell || busy) return
    const rect = shell.getBoundingClientRect()
    regionDragRef.current = {
      target,
      mode,
      startX: e.clientX,
      startY: e.clientY,
      origin: target === 'analysis' ? { ...analysisRegion } : { ...blurBandRegion },
      boxW: Math.max(1, rect.width),
      boxH: Math.max(1, rect.height),
    }
    ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  }

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const drag = regionDragRef.current
      if (!drag) return
      const dx = (e.clientX - drag.startX) / drag.boxW
      const dy = (e.clientY - drag.startY) / drag.boxH
      let { x, y, w, h } = drag.origin
      if (drag.mode === 'move') {
        x += dx
        y += dy
      } else {
        if (drag.mode.includes('e')) w = drag.origin.w + dx
        if (drag.mode.includes('s')) h = drag.origin.h + dy
        if (drag.mode.includes('w')) {
          x = drag.origin.x + dx
          w = drag.origin.w - dx
        }
        if (drag.mode.includes('n')) {
          y = drag.origin.y + dy
          h = drag.origin.h - dy
        }
      }
      const next = clampRegion({ x, y, w, h })
      onSettings(drag.target === 'analysis'
        ? { ...settings, analysisRegion: next }
        : { ...settings, blurBandRegion: next })
    }
    const onUp = () => {
      regionDragRef.current = null
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }
  }, [onSettings, settings])

  useEffect(() => {
    setPreviewDraft(String(settings.previewSec > 0 ? settings.previewSec : 20))
  }, [settings.previewSec])

  useEffect(() => {
    if (!busy) {
      setShowCancel(false)
      return
    }
    // hiện Huỷ sớm — cancel flag arm ngay khi Queued
    const t = window.setTimeout(() => setShowCancel(true), 350)
    return () => window.clearTimeout(t)
  }, [busy])

  const set = <K extends keyof ProjectSettings>(key: K, value: ProjectSettings[K]) => {
    if (busy) return
    onSettings({ ...settings, [key]: value })
  }
  const selectEngine = (engine: ProjectSettings['engine']) => {
    onSettings({ ...settings, engine, translator: normalizeTranslatorForEngine(engine, settings.translator) })
  }

  const selectTranslationModel = (model: string) => {
    if (busy || !CLOUD_TRANSLATORS.has(settings.translator) || !model) return
    setTranslationModel(model)
    void api.saveConfig({ cloud: { [settings.translator]: { model } } }).catch(() => undefined)
  }

  /** Commit ô Preview → settings; trả về số giây đã chốt (dùng khi bấm Preview ngay). */
  const commitPreviewSec = (): number => {
    if (busy) {
      const cur = Math.max(5, Math.min(600, settings.previewSec > 0 ? settings.previewSec : 20))
      setPreviewDraft(String(cur))
      return cur
    }
    const value = Math.max(5, Math.min(600, Number(previewDraft) || 20))
    setPreviewDraft(String(value))
    if (value !== settings.previewSec) {
      onSettings({ ...settings, previewSec: value })
    }
    return value
  }

  const fontSizes = [16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80, 96, 120]
  const fontSizeOptions = settings.subtitleFontSize === 0 || fontSizes.includes(settings.subtitleFontSize)
    ? fontSizes
    : [...fontSizes, settings.subtitleFontSize].sort((a, b) => a - b)

  return (
    <aside className={`sidebar${busy ? ' is-busy' : ''}`}>
      <div
        ref={previewShellRef}
        className={`preview${portrait ? ' portrait' : ''}${busy ? ' locked' : ''}${showAnalysisRoi ? ' has-roi' : ''}`}
        onClick={(e) => {
          // Busy: chỉ xem video, không chọn file mới
          if (busy) return
          // click vào controls video — đừng mở file picker
          if ((e.target as HTMLElement).tagName === 'VIDEO') return
          if ((e.target as HTMLElement).closest('.analysis-roi')) return
          if (videoUrl) return
          inputRef.current?.click()
        }}
        onKeyDown={(e) => {
          if (!busy && !videoUrl && e.key === 'Enter') inputRef.current?.click()
        }}
        role={videoUrl ? undefined : 'button'}
        tabIndex={videoUrl || busy ? -1 : 0}
        aria-disabled={busy && !videoUrl}
      >
        {videoUrl ? (
          <video
            key={videoUrl}
            src={videoUrl}
            poster={projectId ? `/api/projects/${encodeURIComponent(projectId)}/thumbnail` : undefined}
            controls
            playsInline
            preload="auto"
            onTimeUpdate={(e) => setPreviewTime(e.currentTarget.currentTime)}
            onSeeked={(e) => setPreviewTime(e.currentTarget.currentTime)}
            onLoadedMetadata={(e) => {
              const v = e.currentTarget
              setPortrait(v.videoHeight > v.videoWidth)
              setInputSize({ width: v.videoWidth, height: v.videoHeight })
            }}
          />
        ) : (
          <div className="preview-empty">
            <strong>Chọn video</strong>
            <span>MP4 9:16 hoặc 16:9</span>
          </div>
        )}
        {visibleLogoMasks.map((track, index) => {
          const box = track.bbox!
          return (
            <div
              key={`${track.text || 'logo'}-${track.start || 0}-${index}`}
              className="preview-logo-mask"
              aria-label="Vùng logo đã che"
              style={{
                left: `${Math.max(0, box.x) * 100}%`,
                top: `${Math.max(0, box.y) * 100}%`,
                width: `${Math.max(0, box.w) * 100}%`,
                height: `${Math.max(0, box.h) * 100}%`,
              }}
            />
          )
        })}
        {showAnalysisRoi && (
          <div
            className="analysis-roi"
            style={{
              left: `${analysisRegion.x * 100}%`,
              top: `${analysisRegion.y * 100}%`,
              width: `${analysisRegion.w * 100}%`,
              height: `${analysisRegion.h * 100}%`,
            }}
            onPointerDown={(e) => beginRegionDrag('analysis', 'move', e)}
            title={t('Kéo di chuyển — góc/cạnh để co giãn. OCR chỉ quét trong khung này.', 'Drag to move — use edges/corners to resize. OCR scans only inside this frame.')}
          >
            <span className="analysis-roi-label">{t('Vùng định vị chữ', 'Text detection zone')}</span>
            {(['nw', 'ne', 'sw', 'se', 'n', 's', 'e', 'w'] as const).map((h) => (
              <i
                key={h}
                className={`analysis-roi-handle analysis-roi-handle-${h}`}
                onPointerDown={(e) => beginRegionDrag('analysis', h, e)}
              />
            ))}
          </div>
        )}
        {showManualBlurRoi && (
          <div
            className="blur-band-roi"
            style={{
              left: `${blurBandRegion.x * 100}%`,
              top: `${blurBandRegion.y * 100}%`,
              width: `${blurBandRegion.w * 100}%`,
              height: `${blurBandRegion.h * 100}%`,
            }}
            onPointerDown={(e) => beginRegionDrag('blur', 'move', e)}
            title={t('Kéo để di chuyển vùng làm mờ — dùng góc/cạnh để co giãn.', 'Drag to move the blur zone — use edges/corners to resize.')}
          >
            <span className="blur-band-roi-label">{t('Vùng làm mờ', 'Blur zone')}</span>
            {(['nw', 'ne', 'sw', 'se', 'n', 's', 'e', 'w'] as const).map((h) => (
              <i
                key={h}
                className={`blur-band-roi-handle blur-band-roi-handle-${h}`}
                onPointerDown={(e) => beginRegionDrag('blur', h, e)}
              />
            ))}
          </div>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="video/*"
        hidden
        disabled={busy}
        onChange={(e) => {
          const f = e.target.files?.[0]
          e.target.value = ''
          if (f && !busy) onUpload(f)
        }}
      />
      <input
        ref={subtitleInputRef}
        type="file"
        accept=".srt,application/x-subrip,text/plain"
        hidden
        onChange={(e) => void importSubtitle(e.target.files?.[0])}
      />
      {videoUrl && (
        <div className="video-source-meta">
          <button
            type="button"
            className="linkish"
            disabled={busy}
            onClick={() => {
              if (!busy) inputRef.current?.click()
            }}
          >
            {t('Đổi video', 'Change video')}
          </button>
          {inputSize.width > 0 && (
            <span title={`${inputSize.width}×${inputSize.height}`}>
              {t('Đầu vào', 'Input')} · {inputAspectLabel(inputSize.width, inputSize.height)}
            </span>
          )}
        </div>
      )}

      <div className="field-row">
        <Field
          label="Nhận dạng"
          icon={<IconMic size={14} />}
          hint={
            settings.engine === 'paddleocr'
              ? 'Đọc chữ trên khung hình'
              : settings.engine === 'subtitle'
                ? 'Dùng file phụ đề đã chọn'
                : settings.engine === 'capcut'
                  ? undefined
              : 'Faster-Whisper'
          }
        >
          <select
            value={settings.engine}
            disabled={busy}
            onChange={(e) => selectEngine(e.target.value as ProjectSettings['engine'])}
          >
            <option value="whisper">Giọng nói (Whisper)</option>
            <option value="capcut">{t('Giọng nói (CapCut cloud)', 'Speech (CapCut cloud)')}</option>
            <option value="paddleocr">Chữ trên màn (OCR)</option>
            <option value="subtitle">Phụ đề SRT</option>
          </select>
        </Field>
        <Field
          label="Công cụ dịch"
          icon={<IconTranslate size={14} />}
          hint={
            settings.translator === 'google'
              ? 'Google free — nhanh.'
              : settings.translator === 'mymemory'
                ? 'Free — không key (có quota IP)'
                  : settings.translator === 'tiktok'
                    ? 'TikTok translate free'
                    : settings.translator === 'capcut'
                      ? undefined
                    : settings.translator === 'ollama'
                    ? settings.ollamaMode === 'cloud'
                      ? 'Cloud'
                      : 'Dùng model đã tải trên máy'
                    : 'Cấu hình API key tại Cấu hình'
          }
        >
          <select
            value={settings.translator}
            disabled={busy}
            onChange={(e) =>
              set('translator', e.target.value as ProjectSettings['translator'])
            }
          >
            {translatorOptions(settings.engine).map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
          </select>
        </Field>
        {CLOUD_TRANSLATORS.has(settings.translator) && (
          <Field
            className="translation-model-field"
            label={t('Model dịch', 'Translation model')}
            hint={translationModelsLoading
              ? t('Đang tải danh sách model thật…', 'Loading live model list…')
              : undefined}
          >
            <select
              value={translationModel}
              disabled={busy || translationModelsLoading || !translationModels.length}
              onChange={(e) => selectTranslationModel(e.target.value)}
            >
              {!translationModels.length && <option value="">{t('Chưa tải được model', 'No model list loaded')}</option>}
              {translationModels.some((model) => model.free) && (
                <optgroup label={t('Model free dùng được', 'Free models available')}>
                  {translationModels.filter((model) => model.free).map((model) => (
                    <option key={model.id} value={model.id}>{model.label || model.id}</option>
                  ))}
                </optgroup>
              )}
              {translationModels.some((model) => !model.free) && (
                <optgroup label={t('Model cần credit', 'Models requiring credit')}>
                  {translationModels.filter((model) => !model.free).map((model) => (
                    <option key={model.id} value={model.id}>{model.label || model.id} — {t('cần credit', 'credit required')}</option>
                  ))}
                </optgroup>
              )}
            </select>
          </Field>
        )}
      </div>

      {settings.engine === 'subtitle' && (
        <Field className="subtitle-source-field" label="File phụ đề" icon={<IconType size={14} />} hint="Dùng timestamp từ file, không chạy Whisper/OCR">
          <div className="subtitle-source-control">
            <select
              value={settings.subtitleSource || ''}
              disabled={busy}
              onChange={(e) => void selectSubtitle(e.target.value)}
            >
              {!subtitleSources.length && <option value="">Chưa có file SRT</option>}
              {subtitleSources.map((source) => <option key={source.name} value={source.name}>{source.label}</option>)}
            </select>
            <button type="button" disabled={busy || !projectId} onClick={() => subtitleInputRef.current?.click()}>
              + Nhập SRT
            </button>
          </div>
        </Field>
      )}

      {settings.translator === 'ollama' && (
        <div className="field-row">
          <Field label="Ollama">
            <select
              value={settings.ollamaMode}
              disabled={busy}
              onChange={(e) => set('ollamaMode', e.target.value as ProjectSettings['ollamaMode'])}
            >
              <option value="cloud">Cloud Free</option>
              <option value="local">Local</option>
            </select>
          </Field>
          {settings.ollamaMode === 'cloud' ? (
            <Field label="Model Cloud">
              <div className="field-inline">
                <input
                  value={settings.ollamaModel}
                  disabled={busy}
                  onChange={(e) => set('ollamaModel', e.target.value || 'minimax-m3:cloud')}
                />
              </div>
            </Field>
          ) : (
            <Field label="Model">
              <select
                value={ollamaLocalModels.includes(settings.ollamaModel) ? settings.ollamaModel : ''}
                disabled={busy}
                onChange={(e) => set('ollamaModel', e.target.value)}
              >
                {!ollamaLocalModels.length && <option value="">{t('Chưa tìm thấy model Ollama', 'No Ollama model found')}</option>}
                {ollamaLocalModels.map((model) => <option key={model} value={model}>{model}</option>)}
              </select>
            </Field>
          )}
        </div>
      )}

      <div className="field-row">
        <Field label="Ngôn ngữ gốc" icon={<IconGlobe size={14} />}>
          <select
            value={settings.sourceLang}
            disabled={busy}
            onChange={(e) => set('sourceLang', e.target.value)}
          >
            <option value="auto">Tự động nhận diện</option>
            <option value="zh">Tiếng Trung</option>
            <option value="en">Tiếng Anh</option>
            <option value="ja">Tiếng Nhật</option>
            <option value="ko">Tiếng Hàn</option>
            <option value="vi">Tiếng Việt</option>
          </select>
        </Field>
        <Field
          label="Ngôn ngữ dịch"
          icon={<IconGlobe size={14} />}
        >
          <select
            value={settings.targetLang}
            disabled={busy}
            onChange={(e) => set('targetLang', e.target.value)}
          >
            <option value="none">Không dịch (giữ chữ nguồn)</option>
            <option value="vi">Tiếng Việt</option>
            <option value="en">Tiếng Anh</option>
            <option value="zh">Tiếng Trung</option>
            <option value="ja">Tiếng Nhật</option>
            <option value="ko">Tiếng Hàn</option>
          </select>
        </Field>
      </div>
      <div className="field-row">
        <Field label="Khớp thời lượng" icon={<IconClock size={14} />}>
          <select
            value={settings.matchDuration}
            disabled={busy}
            title={
              settings.matchDuration === 'preferVideo'
                ? t('Ưu tiên video gốc, TTS tự nén vào khung', 'Prioritize the original video; TTS is compressed to fit')
                : settings.matchDuration === 'none'
                  ? t('Giữ TTS nguyên tốc độ', 'Keep the original TTS speed')
                  : settings.matchDuration === 'stretch'
                    ? t('Ép TTS đúng khung gốc (nhanh/chậm)', 'Force TTS to the original duration (faster/slower)')
                    : t('TTS dài hơn khung → tăng tốc nhẹ (≤1.25×)', 'TTS longer than the cue → slightly speed up (≤1.25×)')
            }
            onChange={(e) =>
              set('matchDuration', e.target.value as ProjectSettings['matchDuration'])
            }
          >
            <option value="preferVideo">{t('Ưu tiên video gốc', 'Prioritize original video')}</option>
            <option value="none">{t('Giữ nguyên TTS', 'Keep TTS intact')}</option>
            <option value="natural">{t('Tự nhiên, rút gọn nhẹ', 'Natural, slightly shortened')}</option>
            <option value="stretch">{t('Kéo giãn khớp đoạn', 'Stretch to match the cue')}</option>
          </select>
        </Field>
        <Field label="Giọng mặc định" icon={<IconSpeaker size={14} />}>
          <select
            value={
              voices.some((v) => v.id === settings.defaultVoice)
                ? settings.defaultVoice
                : (voices[0]?.id ?? 'el:pNInz6obpgDQGcFmaJgB')
            }
            disabled={busy}
            onChange={(e) => set('defaultVoice', e.target.value)}
          >
            {voices.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div className="field-row">
        <Field label="Phụ đề" icon={<IconLayers size={14} />}>
          <select
            value={
              !settings.burnSubs
                ? 'none'
                : settings.coverHardsubs
                  ? 'cover'
                  : settings.captionPlacement === 'above'
                    ? 'above'
                    : 'below'
            }
            disabled={busy}
            onChange={(e) => {
              const v = e.target.value
              if (v === 'cover') {
                onSettings({
                  ...settings,
                  coverHardsubs: true,
                  burnSubs: true,
                })
              } else if (v === 'below') {
                onSettings({
                  ...settings,
                  coverHardsubs: false,
                  burnSubs: true,
                  captionPlacement: 'below',
                })
              } else if (v === 'above') {
                onSettings({
                  ...settings,
                  coverHardsubs: false,
                  burnSubs: true,
                  captionPlacement: 'above',
                })
              } else {
                onSettings({ ...settings, burnSubs: false })
              }
            }}
          >
            <option value="cover">{settings.targetLang === 'none' ? t('Che chữ cũ + chèn chữ gốc', 'Cover + show source') : t('Che chữ cũ + chèn bản dịch', 'Cover + show translation')}</option>
            <option value="below">{settings.targetLang === 'none' ? t('Chèn chữ gốc phía dưới', 'Show source below') : t('Chèn bản dịch phía dưới', 'Show translation below')}</option>
            <option value="above">{settings.targetLang === 'none' ? t('Chèn chữ gốc phía trên', 'Show source above') : t('Chèn bản dịch phía trên', 'Show translation above')}</option>
            <option value="none">{t('Không chèn chữ', 'No caption')}</option>
          </select>
        </Field>
        <Field label="Cỡ chữ" icon={<IconType size={14} />}>
          <select
            value={String(settings.subtitleFontSize)}
            disabled={busy || !settings.burnSubs}
            onChange={(e) => set('subtitleFontSize', Number(e.target.value))}
            title="Tự động sẽ chọn cỡ lớn nhất vừa từng nhãn, chữ dọc và câu ngang"
          >
            <option value="0">Tự động (khuyên dùng)</option>
            {fontSizeOptions.map((px) => (
              <option key={px} value={px}>
                {px} px
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div className="audio-filter locate-logo-filter">
        <label
          className="audio-filter-toggle"
          title="Chỉ dịch chữ trong khung này. Kéo khung trên video để chọn vùng OCR."
        >
          <span className="field-label">
            <IconLayers size={14} />
            Giới hạn khung định vị
          </span>
          <input
            type="checkbox"
            checked={Boolean(settings.stableCaptionLocate)}
            disabled={busy}
            onChange={(e) => {
              if (busy) return
              const on = e.target.checked
              onSettings({
                ...settings,
                stableCaptionLocate: on,
                analysisRegion: on
                  ? (settings.analysisRegion || { ...DEFAULT_ANALYSIS_REGION })
                  : settings.analysisRegion,
              })
            }}
          />
        </label>
        <label
          className="audio-filter-toggle"
          title={t('Tự động tìm một logo cố định và tái tạo nền để xóa logo khi xuất.', 'Automatically detect a fixed logo and reconstruct its background when exporting.')}
        >
          <span className="field-label">
            <IconWand size={14} />
            {t('Che Logo', 'Remove logo')}
          </span>
          <input
            type="checkbox"
            checked={Boolean(settings.coverLogo)}
            disabled={busy}
            onChange={(e) => {
              if (!busy) onSettings({ ...settings, coverLogo: e.target.checked })
            }}
          />
        </label>
        <div className="blur-zone-control">
          <label
            className="audio-filter-toggle blur-zone-toggle"
            title={t(
              'Thêm vùng làm mờ cố định chạy suốt video (che hardsub / watermark phía dưới).',
              'Add a persistent blur band across the full video (covers hardsubs / bottom watermarks).',
            )}
          >
            <span className="field-label">
              <IconLayers size={14} />
              {t('Vùng làm mờ', 'Blur zone')}
            </span>
            <input
              type="checkbox"
              checked={(settings.blurBandMode ?? 'auto') !== 'off'}
              disabled={busy}
              onChange={(e) => {
                if (!busy) onSettings({ ...settings, blurBandMode: e.target.checked ? 'auto' : 'off' })
              }}
            />
          </label>
          {(settings.blurBandMode ?? 'auto') !== 'off' && (
            <select
              className="blur-zone-select"
              value={settings.blurBandMode ?? 'auto'}
              disabled={busy}
              onChange={(e) => onSettings({ ...settings, blurBandMode: e.target.value as 'auto' | 'manual' })}
            >
              <option value="auto">{t('Tự động (OCR)', 'Auto (OCR)')}</option>
              <option value="manual">{t('Thủ công (kéo khung)', 'Manual (drag frame)')}</option>
            </select>
          )}
        </div>
      </div>

      <div className="audio-filter audio-feature-filter">
        <div className="audio-feature-row">
          <label
            className="audio-filter-toggle"
            title="Bật để xử lý track gốc khi lồng tiếng / xuất (xóa lời Demucs, tắt lời…)"
          >
            <span className="field-label">
              <IconSpeaker size={14} />
              Lọc âm thanh gốc
            </span>
            <input
              type="checkbox"
              checked={settings.processOriginalAudio}
              disabled={busy}
              onChange={(e) => {
                const on = e.target.checked
                onSettings({
                  ...settings,
                  processOriginalAudio: on,
                  originalAudioMode:
                    on && settings.originalAudioMode === 'original'
                      ? 'no_vocals'
                      : settings.originalAudioMode,
                })
              }}
            />
          </label>
          <label
            className="audio-filter-toggle"
            title={settings.engine === 'whisper'
              ? t('Tự phát hiện người nói và gán speaker cho từng câu khi chạy nhận dạng.', 'Automatically detect speakers and assign one to each transcript segment.')
              : t('Tách người nói chỉ dùng với nhận dạng Whisper.', 'Speaker separation is available only with Whisper recognition.')}
          >
            <span className="field-label">
              <IconMic size={14} />
              {t('Tách người nói', 'Separate speakers')}
            </span>
            <input
              type="checkbox"
              checked={Boolean(settings.speakerDiarization)}
              disabled={busy || settings.engine !== 'whisper'}
              onChange={(e) => onSettings({ ...settings, speakerDiarization: e.target.checked })}
            />
          </label>
        </div>
        {settings.speakerDiarization && settings.engine === 'whisper' && (
          <label className="speaker-count-row">
            <span>{t('Số người nói', 'Speaker count')}</span>
            <select
              value={settings.speakerCount || 0}
              disabled={busy}
              onChange={(e) => onSettings({ ...settings, speakerCount: Number(e.target.value) })}
            >
              <option value={0}>{t('Tự phát hiện', 'Auto-detect')}</option>
              {[2, 3, 4, 5, 6, 7, 8].map((count) => <option key={count} value={count}>{locale === 'en' ? `${count} speakers` : `${count} người`}</option>)}
            </select>
          </label>
        )}
        {settings.processOriginalAudio && (
          <>
            <div className="audio-filter-options" role="radiogroup" aria-label="Lọc âm thanh gốc">
              {(
                [
                  ['no_vocals', 'Xóa lời'],
                  ['vocals', 'Chỉ giữ lời'],
                  // ['original', 'Giữ âm gốc'],
                  // ['mute', 'Tắt âm gốc'],
                ] as const
              ).map(([value, label]) => (
                <label
                  key={value}
                  className={settings.originalAudioMode === value ? 'active' : ''}
                >
                  <input
                    type="radio"
                    name="original-audio-mode"
                    value={value}
                    checked={settings.originalAudioMode === value}
                    disabled={busy}
                    onChange={() => set('originalAudioMode', value)}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
            <label
              className="audio-volume"
              title="Âm lượng track gốc / nền sau lọc (0–100%)"
            >
              <span className="audio-volume-label">Âm lượng nền</span>
              <input
                type="range"
                min={0}
                max={100}
                step={1}
                value={
                  settings.originalAudioMode === 'mute'
                    ? 0
                    : Math.max(0, Math.min(200, settings.originalAudioVolume ?? 100))
                }
                disabled={busy || settings.originalAudioMode === 'mute'}
                onChange={(e) =>
                  set('originalAudioVolume', Math.max(0, Math.min(200, Number(e.target.value) || 0)))
                }
              />
              <em className="audio-volume-pct">
                {settings.originalAudioMode === 'mute'
                  ? 0
                  : Math.max(0, Math.min(200, settings.originalAudioVolume ?? 100))}
                %
              </em>
            </label>
          </>
        )}
      </div>

      <div className="preview-run">
        <label
          className="workers-setting"
          title="Tự động tăng/giảm luồng theo CPU, RAM và GPU còn rảnh"
        >
          <span className="preview-run-label">Luồng</span>
          <select
            value={String(
              [0, 1, 2, 4, 6, 8, 12, 16].includes(settings.workers) ? settings.workers : 0,
            )}
            disabled={busy}
            onChange={(e) => set('workers', Number(e.target.value))}
          >
            <option value="0">Tự động</option>
            <option value="1">1</option>
            <option value="2">2</option>
            <option value="4">4</option>
            <option value="6">6</option>
            <option value="8">8</option>
            <option value="12">12</option>
            <option value="16">16</option>
          </select>
        </label>
        <label
          className="preview-len"
          title="Chỉ dùng khi bấm Preview — Dịch cả video vẫn ra full"
        >
          <span className="preview-run-label">Preview(s)</span>
          <input
            type="number"
            min={5}
            max={600}
            step={1}
            value={previewDraft}
            disabled={busy}
            onChange={(e) => setPreviewDraft(e.target.value)}
            onBlur={commitPreviewSec}
            onKeyDown={(e) => {
              if (e.key === 'Enter') e.currentTarget.blur()
            }}
          />
        </label>
        <button
          type="button"
          className="secondary icon-only"
          disabled={busy || !videoUrl}
          onClick={() => {
            // Commit draft trước — đổi 5→10 rồi bấm ngay vẫn dùng 10s
            const sec = commitPreviewSec()
            onPreview(sec)
          }}
          aria-label="Preview"
          title={`Dịch ${previewDraft || settings.previewSec || 20}s đầu (ô Preview) — Xuất cũng theo cửa sổ này`}
        >
          <IconPlay size={14} />
        </button>
      </div>

      <div className="run-actions">
        <button
          type="button"
          className="clear-cache-btn"
          disabled={!videoUrl || busy || clearingCache || !onClearCache}
          onClick={() => setConfirmClear(true)}
          title="Xóa toàn bộ cache dự án (giữ video nguồn)"
        >
          <IconTrash size={14} />
          {clearingCache ? 'Đang xóa…' : 'Xóa cache'}
        </button>
        <button
          type="button"
          className="primary"
          disabled={busy || !videoUrl || clearingCache}
          onClick={onTranslateAll}
          title="Dịch cả video — Xuất bản sẽ ra full (không theo số Preview)"
        >
          {busy ? (
            'Đang xử lý…'
          ) : (
            <>
              <IconLangSwap size={16} />
              Dịch cả video
              <IconArrowRight size={16} />
            </>
          )}
        </button>
        {showCancel && (
          <button type="button" className="cancel" onClick={onCancel}>
            Huỷ
          </button>
        )}
      </div>

      {confirmClear && (
        <div
          className="clear-cache-modal-backdrop"
          role="presentation"
        >
          <div
            className="clear-cache-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="clear-cache-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="clear-cache-title">Xóa cache dự án</h3>
            <p>Chọn mục cần xóa. Video nguồn không bao giờ bị xóa.</p>
            <div className="clear-cache-toolbar">
              <button
                type="button"
                className="clear-cache-link"
                disabled={clearingCache}
                onClick={() => setClearParts([...ALL_CACHE_PARTS])}
              >
                Chọn tất cả
              </button>
              <button
                type="button"
                className="clear-cache-link"
                disabled={clearingCache}
                onClick={() => setClearParts([])}
              >
                Bỏ chọn
              </button>
            </div>
            <div className="clear-cache-checks">
              {CACHE_CLEAR_OPTIONS.map((opt) => {
                const on = clearParts.includes(opt.id)
                return (
                  <label key={opt.id} className="clear-cache-check">
                    <input
                      type="checkbox"
                      checked={on}
                      disabled={clearingCache}
                      onChange={() =>
                        setClearParts((prev) =>
                          on ? prev.filter((id) => id !== opt.id) : [...prev, opt.id],
                        )
                      }
                    />
                    <span>{opt.label}</span>
                  </label>
                )
              })}
            </div>
            <p className="clear-cache-note">Không xóa: video nguồn · settings dự án</p>
            <div className="clear-cache-modal-actions">
              <button
                type="button"
                className="secondary"
                disabled={clearingCache}
                onClick={() => setConfirmClear(false)}
              >
                Hủy
              </button>
              <button
                type="button"
                className="clear-cache-confirm"
                disabled={clearingCache || clearParts.length === 0}
                onClick={() => {
                  onClearCache?.(clearParts)
                  setConfirmClear(false)
                }}
              >
                {clearParts.length === ALL_CACHE_PARTS.length
                  ? 'Xóa tất cả'
                  : `Xóa đã chọn (${clearParts.length})`}
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  )
}
